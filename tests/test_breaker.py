# Design section 5, the circuit breaker: trip on 11 of the last 20 service-reaching attempts
# within the lookback, claim gate, probe race fenced by probe_generation, synthetic probe,
# close or reopen. DECISIONS.md D2: attempts from before the last close never count.
import threading
import time
from dataclasses import replace

import pytest

from app.summary.client import PROBE_TRANSCRIPT, TransientError
from app.summary.mock import Gate
from app.worker.main import run_loops
from tests.factories import make_event, post
from tests.harness import fetch_attempts, fetch_job

FAIL = TransientError("ai_unavailable")


@pytest.fixture
def config(worker_config):
    return replace(worker_config, breaker_cooldown_seconds=0.2, backoff_base_seconds=0.2)


@pytest.fixture
def worker(make_worker, config):
    return make_worker("w-A", config=config)


def breaker(db):
    row = db.execute(
        "SELECT state::text, probe_generation, probes_sent, last_probe_outcome, opened_at, "
        "open_until > now(), extract(epoch FROM open_until - updated_at)::float "
        "FROM circuit_breaker WHERE name = 'generate_summary'").fetchone()
    keys = ("state", "probe_generation", "probes_sent", "last_probe_outcome", "opened_at",
            "open_until_in_future", "cooldown")
    return dict(zip(keys, row))


def post_jobs(api, n):
    events = [make_event(version=1, transcription=f"job {i}") for i in range(n)]
    for ev in events:
        post(api, ev)
    return events


def set_breaker(db, sql):
    db.execute(f"UPDATE circuit_breaker SET {sql} WHERE name = 'generate_summary'")


def open_breaker(db, *, cooldown_passed):
    until = "now() - interval '1 second'" if cooldown_passed else "now() + interval '1 hour'"
    set_breaker(db, f"state = 'open', open_until = {until}, opened_at = now(), updated_at = now()")


def seed_attempts(db, job_id, outcomes, seconds_ago):
    """Synthetic attempt history, oldest first, finishing `seconds_ago` down to just before now."""
    start = db.execute("SELECT coalesce(max(attempt_no), 0) FROM job_attempts WHERE job_id = %s",
                       (job_id,)).fetchone()[0]
    for i, outcome in enumerate(outcomes):
        db.execute(
            "INSERT INTO job_attempts (job_id, attempt_no, redrive_generation, worker_id, outcome, "
            "finished_at) VALUES (%s, %s, 0, 'w-seed', %s, now() - make_interval(secs => %s))",
            (job_id, start + i + 1, outcome, seconds_ago - i * 0.01))


@pytest.fixture
def history_job(api, db):
    """A job to hang synthetic attempts on; the breaker's last change is pushed an hour back
    so seeded attempts are not excluded by the D2 cutoff."""
    ev = make_event(encounter_id="enc-history", version=1)
    post(api, ev)
    db.execute("UPDATE summary_jobs SET status = 'ready', summary = 's', completed_at = now() "
               "WHERE encounter_id = 'enc-history'")
    set_breaker(db, "updated_at = now() - interval '1 hour'")
    return fetch_job(db, "enc-history", 1)["job_id"]


# ---------------------------------------------------------------- tripping

def test_trips_on_the_eleventh_failure(api, db, mock, worker):
    events = post_jobs(api, 12)
    mock.set_default(FAIL)

    for _ in range(10):
        assert worker.run_once() == "transient_error"
    assert breaker(db)["state"] == "closed"

    assert worker.run_once() == "transient_error"
    b = breaker(db)
    assert (b["state"], b["open_until_in_future"]) == ("open", True)
    assert b["opened_at"] is not None
    assert b["cooldown"] == pytest.approx(0.2)

    # While open, workers stop claiming: the 12th job is untouched
    assert worker.run_once() == "breaker_open"
    last = fetch_job(db, events[11]["encounter_id"], 1)
    assert (last["status"], last["attempts"]) == ("queued", 0)
    assert fetch_attempts(db, last["job_id"]) == []
    assert mock.real_calls == 11


def test_threshold_is_a_strict_majority_of_the_last_twenty(api, db, mock, worker):
    post_jobs(api, 21)
    mock.push(*(["ok"] * 10), *([FAIL] * 10))
    for _ in range(20):
        worker.run_once()
    assert breaker(db)["state"] == "closed"         # 10 of 20: a half-working service stays closed

    mock.push(FAIL)
    worker.run_once()
    assert breaker(db)["state"] == "open"           # last 20 = 9 successes + 11 failures


def test_failures_outside_the_lookback_do_not_count(api, db, mock, worker, history_job):
    seed_attempts(db, history_job, ["transient_error"] * 11, seconds_ago=11 * 60)
    post_jobs(api, 2)
    mock.set_default(FAIL)
    worker.run_once()
    assert breaker(db)["state"] == "closed"         # only this one failure is recent

    seed_attempts(db, history_job, ["transient_error"] * 9, seconds_ago=5)
    worker.run_once()
    assert breaker(db)["state"] == "open"           # 9 seeded + 2 real, all within 10 minutes


def test_discarded_and_superseded_count_as_the_service_responding(api, db, mock, worker, history_job):
    seed_attempts(db, history_job, ["transient_error"] * 10, seconds_ago=20)
    seed_attempts(db, history_job, ["discarded"] * 5 + ["superseded"] * 5, seconds_ago=10)
    post_jobs(api, 1)
    mock.push(FAIL)
    worker.run_once()
    # Last 20: this failure, 10 responses, 9 older failures = 10 failures
    assert breaker(db)["state"] == "closed"


def test_skipped_and_lease_expired_are_not_evidence(api, db, mock, worker, history_job):
    seed_attempts(db, history_job, ["transient_error"] * 10, seconds_ago=20)
    seed_attempts(db, history_job, ["skipped"] * 10 + ["lease_expired"] * 10 + ["in_flight"] * 5,
                  seconds_ago=10)
    post_jobs(api, 1)
    mock.push(FAIL)
    worker.run_once()
    assert breaker(db)["state"] == "open"           # 11 failures among the counted outcomes


# ---------------------------------------------------------------- the claim gate

@pytest.mark.parametrize("state_sql", [
    "state = 'open', open_until = now() + interval '1 hour'",
    "state = 'half_open', probe_deadline = now() + interval '1 hour'",
])
def test_no_claims_unless_closed(api, db, mock, worker, state_sql):
    ev = post_jobs(api, 1)[0]
    set_breaker(db, state_sql)
    assert worker.run_once() == "breaker_open"
    job = fetch_job(db, ev["encounter_id"], 1)
    assert (job["status"], job["attempts"], job["started_at"]) == ("queued", 0, None)
    assert fetch_attempts(db, job["job_id"]) == []
    assert mock.calls == []


# ---------------------------------------------------------------- probing

def test_no_probe_before_the_cooldown(db, mock, worker):
    open_breaker(db, cooldown_passed=False)
    assert worker.run_once() == "breaker_open"
    assert (breaker(db)["probes_sent"], mock.calls) == (0, [])


def test_successful_probe_closes_the_breaker(api, db, mock, worker):
    ev = post_jobs(api, 1)[0]
    open_breaker(db, cooldown_passed=True)

    assert worker.run_once() == "probe_succeeded"
    b = breaker(db)
    assert (b["state"], b["opened_at"], b["last_probe_outcome"]) == ("closed", None, "succeeded")
    assert (b["probe_generation"], b["probes_sent"]) == (1, 1)
    # The probe is synthetic: no job claimed, no attempt row, no budget used
    assert [c.transcription for c in mock.calls] == [PROBE_TRANSCRIPT]
    assert (mock.probe_calls, mock.real_calls) == (1, 0)
    assert db.execute("SELECT count(*) FROM job_attempts").fetchone()[0] == 0

    assert worker.run_once() == "ready"             # claiming resumes
    assert fetch_job(db, ev["encounter_id"], 1)["status"] == "ready"


def test_failed_probe_reopens_for_another_cooldown(api, db, mock, worker):
    ev = post_jobs(api, 1)[0]
    open_breaker(db, cooldown_passed=True)
    mock.push(TransientError("ai_timeout"))

    assert worker.run_once() == "probe_failed"
    b = breaker(db)
    assert (b["state"], b["open_until_in_future"], b["last_probe_outcome"]) == ("open", True, "transient_error")
    assert b["probes_sent"] == 1
    assert fetch_job(db, ev["encounter_id"], 1)["attempts"] == 0   # no job's budget touched


def test_exactly_one_worker_wins_the_probe_race(db, mock, make_worker, config):
    open_breaker(db, cooldown_passed=True)
    gate = Gate("probe ok")
    mock.push(gate)
    workers = [make_worker(f"w-{i}", config=config) for i in range(8)]
    barrier = threading.Barrier(len(workers))
    results = []

    def race(w):
        barrier.wait()
        results.append(w.run_once())

    threads = [threading.Thread(target=race, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    assert gate.entered.wait(5)
    time.sleep(0.3)               # the losers finish while the winner is still mid-probe
    assert sorted(results) == ["breaker_open"] * 7
    gate.release()
    for t in threads:
        t.join(5)

    assert sorted(results) == ["breaker_open"] * 7 + ["probe_succeeded"]
    assert mock.probe_calls == 1
    assert (breaker(db)["probes_sent"], breaker(db)["probe_generation"]) == (1, 1)


def test_dead_prober_is_replaced_after_its_deadline(db, mock, worker):
    set_breaker(db, "state = 'half_open', probe_deadline = now() - interval '1 second', "
                    "probe_generation = 3, probes_sent = 3")
    assert worker.run_once() == "probe_succeeded"
    b = breaker(db)
    assert (b["state"], b["probe_generation"], b["probes_sent"]) == ("closed", 4, 4)


def test_stalled_prober_cannot_overwrite_a_newer_verdict(db, mock, make_worker, config):
    # Prober A stalls past its deadline; B takes over and closes the breaker; A's late
    # failure must not reopen it
    open_breaker(db, cooldown_passed=True)
    gate = Gate(TransientError("ai_timeout"))
    mock.push(gate, "probe ok")
    result_a = []
    a = threading.Thread(target=lambda: result_a.append(make_worker("w-A", config=config).run_once()))
    a.start()
    assert gate.entered.wait(5)

    set_breaker(db, "probe_deadline = now() - interval '1 second'")
    assert make_worker("w-B", config=config).run_once() == "probe_succeeded"
    gate.release()
    a.join(5)

    assert result_a == ["probe_lost"]
    b = breaker(db)
    assert (b["state"], b["last_probe_outcome"], b["probe_generation"]) == ("closed", "succeeded", 2)
    assert b["probes_sent"] == mock.probe_calls == 2   # the lost probe is still counted as paid


# ---------------------------------------------------------------- D2

def test_recovery_is_not_undone_by_the_failures_that_tripped_it(api, db, mock, worker):
    post_jobs(api, 30)
    mock.set_default(FAIL)
    for _ in range(11):
        worker.run_once()
    assert breaker(db)["state"] == "open"

    time.sleep(0.25)                                # cooldown passes
    mock.set_default(None)
    mock.push("probe ok")
    assert worker.run_once() == "probe_succeeded"

    # One ordinary failure right after recovery: the 11 pre-close failures no longer count
    mock.push(FAIL)
    assert worker.run_once() == "transient_error"
    assert breaker(db)["state"] == "closed"

    # A genuinely new run of 11 failures still trips it
    mock.set_default(FAIL)
    for _ in range(10):
        worker.run_once()
    assert breaker(db)["state"] == "open"


# ---------------------------------------------------------------- an outage end to end, in miniature

def test_outage_is_bounded_and_every_job_recovers(api, db, mock, make_worker, config):
    # Design section 6.7 at small scale (the full version is headline test 5)
    events = post_jobs(api, 20)
    mock.set_default(FAIL)
    workers = [make_worker(f"w-{i}", config=config) for i in range(4)]
    stop = threading.Event()
    runner = threading.Thread(target=run_loops, args=(workers, stop, 5), daemon=True)
    runner.start()

    deadline = time.monotonic() + 10
    while breaker(db)["state"] == "closed" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert breaker(db)["state"] != "closed"

    # A worker whose claim read 'closed' just before the trip committed still makes its call:
    # that is the in-flight allowance. Let those land, then take the snapshot.
    time.sleep(1.0)                                 # several cooldowns pass; probes keep failing
    real_after_trip = mock.real_calls
    assert 11 <= real_after_trip <= 11 + 3          # 11, plus at most one in flight per other worker
    attempts_during = db.execute("SELECT count(*) FROM job_attempts").fetchone()[0]
    time.sleep(0.5)
    assert db.execute("SELECT count(*) FROM job_attempts").fetchone()[0] == attempts_during
    assert mock.real_calls == real_after_trip       # no real call while open
    assert mock.probe_calls >= 2
    # probes_sent is committed before the call, so one probe may be between the two
    assert 0 <= breaker(db)["probes_sent"] - mock.probe_calls <= 1
    assert db.execute("SELECT count(*) FROM summary_jobs WHERE status = 'failed'").fetchone()[0] == 0

    mock.set_default(None)                          # the service recovers
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if db.execute("SELECT count(*) FROM summary_jobs WHERE status = 'ready'").fetchone()[0] == 20:
            break
        time.sleep(0.05)
    stop.set()
    runner.join(10)

    assert db.execute("SELECT count(*) FROM summary_jobs WHERE status = 'ready'").fetchone()[0] == 20
    assert breaker(db)["probes_sent"] == mock.probe_calls     # every probe counted, none lost
    assert all(c.transcription == PROBE_TRANSCRIPT for c in mock.calls if c.kind == "probe")
    worst = db.execute("SELECT max(n) FROM (SELECT count(*) AS n FROM job_attempts "
                       "WHERE outcome <> 'skipped' GROUP BY job_id) t").fetchone()[0]
    assert worst <= 5
    assert breaker(db)["state"] == "closed"
