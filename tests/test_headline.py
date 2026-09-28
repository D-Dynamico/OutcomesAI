# The seven headline tests: design section 7's five, plus the two under "Not covered by
# these five". Each asserts on database state, HTTP responses and mock call counts, never on
# log text. Concurrency is forced (barrier plus a hook holding the first transaction after it
# takes the row lock) and repeated with a fresh encounter each iteration. Durations are shrunk
# so the outage runs in seconds; the logic is unchanged.
import threading
import time
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.metrics import API_REGISTRY
from app.summary.client import PROBE_TRANSCRIPT, TransientError
from app.summary.mock import Gate
from app.worker.main import run_loops
from tests.factories import make_event, new_id, post
from tests.harness import (ApiProcess, LockHold, Sampler, fetch_attempts, fetch_job,
                           released_together, run_in_background)

ITERATIONS = 200          # tests 1 and 3 (design section 7: "for example 200 iterations")
ITERATIONS_WITH_WORKERS = 100   # test 2 also runs the worker and samples GET every iteration


def summary_of(transcription):
    return f"SUMMARY-OF-{transcription}"


@pytest.fixture
def lock_hold(fresh_db):
    hold = LockHold(fresh_db)
    yield hold
    hold.close()


@pytest.fixture
def instances(app_config, lock_hold):
    """Two service instances (separate apps, separate pools), each with the lock-hold hook."""
    apps = [create_app(app_config) for _ in range(2)]
    for app in apps:
        app.state.hooks.register("ingest.after_lock", lock_hold)
    with TestClient(apps[0]) as a, TestClient(apps[1]) as b:
        yield a, b


@pytest.fixture
def reader(app_config):
    """A third instance used only for sampling GET, so reads never share a client with writes."""
    with TestClient(create_app(app_config)) as client:
        yield client


def count(db, sql, *params):
    return db.execute(sql, params).fetchone()[0]


# ================================================================ Test 1
# Concurrent duplicate is accepted exactly once (section 3, scenario 6.1). The event_id primary
# key decides, not an application check.

@pytest.mark.parametrize("brand_new", [False, True], ids=["existing_encounter", "brand_new_encounter"])
def test_1_concurrent_duplicate_accepted_exactly_once(instances, lock_hold, db, brand_new):
    for i in range(ITERATIONS):
        enc = new_id("enc")
        if not brand_new:
            lock_hold.disarm()
            assert post(instances[0], make_event(encounter_id=enc, version=10)).status_code == 201
        ev = make_event(encounter_id=enc, version=12, event_id=f"evt-102-{enc}")

        lock_hold.arm()
        responses = released_together(instances, [ev, ev])

        assert lock_hold.forced, f"iteration {i}: second request never waited on the lock"
        assert sorted((r.status_code, r.json()["outcome"]) for r in responses) == \
            [(200, "duplicate"), (201, "accepted")]
        assert [r.json()["current_version"] for r in responses] == [12, 12]
        assert count(db, "SELECT count(*) FROM encounter_events WHERE event_id = %s", ev["event_id"]) == 1
        assert count(db, "SELECT count(*) FROM summary_jobs WHERE encounter_id = %s AND version = 12", enc) == 1
        assert count(db, "SELECT current_version FROM encounters WHERE encounter_id = %s", enc) == 12

    # No orphan shell row, ever
    assert count(db, "SELECT count(*) FROM encounters WHERE current_version = 0 OR transcription IS NULL") == 0


# ================================================================ Test 2
# The stored version never regresses, and v12 is never shown as current (section 3's
# compare-and-swap; section 4's read path; scenarios 6.5, 6.6). Alternates which version takes
# the lock first; workers then process whatever jobs exist while GET is sampled throughout.

def test_2_version_never_regresses_and_v12_is_never_shown(instances, lock_hold, reader, db, mock,
                                                         make_worker):
    mock.set_default(summary_of)
    worker = make_worker()
    for i in range(ITERATIONS_WITH_WORKERS):
        enc = new_id("enc")
        lock_hold.disarm()
        post(instances[0], make_event(encounter_id=enc, version=10, transcription=f"{enc}-v10"))
        while worker.run_once() != "idle":
            pass                                  # v10 ready before the race: GET has something to show
        v12 = make_event(encounter_id=enc, version=12, transcription=f"{enc}-v12")
        v13 = make_event(encounter_id=enc, version=13, transcription=f"{enc}-v13")
        first, second = (v12, v13) if i % 2 == 0 else (v13, v12)

        responses = {}
        v13_returned_at = []

        def send(client, ev):
            r = post(client, ev)
            if ev is v13:
                v13_returned_at.append(time.monotonic())
            responses[ev["version"]] = r

        with Sampler(reader, enc) as sampler:
            lock_hold.arm()
            t1 = threading.Thread(target=send, args=(instances[0], first))
            t1.start()
            assert lock_hold.entered.wait(5)      # `first` holds the row lock
            t2 = threading.Thread(target=send, args=(instances[1], second))
            t2.start()
            t1.join(10)
            t2.join(10)
            assert lock_hold.forced
            while worker.run_once() != "idle":
                pass
            time.sleep(0.01)                       # at least one sample after the final write

        # Stored state
        assert db.execute("SELECT current_version, transcription FROM encounters WHERE encounter_id = %s",
                          (enc,)).fetchone() == (13, f"{enc}-v13")
        assert (responses[13].status_code, responses[13].json()["outcome"]) == (201, "accepted")
        v12_expected = (201, "accepted") if first is v12 else (200, "stale")
        assert (responses[12].status_code, responses[12].json()["outcome"]) == v12_expected
        assert fetch_job(db, enc, 13)["status"] == "ready"
        v12_job = fetch_job(db, enc, 12)
        if first is v12:
            assert (v12_job["status"], v12_job["superseded_by_version"]) == ("superseded", 13)
        else:
            assert v12_job is None                 # stale events create no job

        # What clients saw
        assert sampler.samples, "no GET samples taken"
        for started, status, body in sampler.samples:
            assert status == 200
            assert body.get("summary") != summary_of(f"{enc}-v12")
            assert body.get("summary_version") != 12
            if started > v13_returned_at[0]:
                assert body["current_version"] == 13, body
        assert sampler.samples[-1][2]["summary"] == summary_of(f"{enc}-v13")


# ================================================================ Test 3
# Identity conflict on a brand-new encounter (section 3 step 2, scenario 6.8): the read-back
# under FOR UPDATE closes the upsert race, and the first patient wins.

def identity_conflicts():
    return API_REGISTRY.get_sample_value("ingest_identity_conflicts_total")


def test_3_identity_race_on_a_brand_new_encounter(instances, lock_hold, db):
    winners = set()
    for i in range(ITERATIONS):
        enc = f"enc-900-{new_id('x')}"
        a = make_event(encounter_id=enc, version=1, patient_id="pat-77")
        b = make_event(encounter_id=enc, version=2, patient_id="pat-99")
        conflicts_before = identity_conflicts()

        lock_hold.arm()
        responses = released_together(instances, [a, b])

        assert lock_hold.forced, f"iteration {i}: second request never waited on the lock"
        assert sorted(r.status_code for r in responses) == [201, 409]
        (winner, _), (loser, loser_response) = sorted(zip([a, b], responses),
                                                      key=lambda pair: pair[1].status_code)
        winners.add(winner["patient_id"])
        body = loser_response.json()
        assert (body["outcome"], body["conflicting_field"]) == ("identity_conflict", "patient_id")
        assert "pat-77" not in loser_response.text and "pat-99" not in loser_response.text

        assert count(db, "SELECT patient_id FROM encounters WHERE encounter_id = %s", enc) == winner["patient_id"]
        assert db.execute("SELECT event_id FROM encounter_events WHERE encounter_id = %s",
                          (enc,)).fetchall() == [(winner["event_id"],)]
        assert db.execute("SELECT event_id FROM summary_jobs WHERE encounter_id = %s",
                          (enc,)).fetchall() == [(winner["event_id"],)]
        assert identity_conflicts() - conflicts_before == 1

        lock_hold.disarm()
        retry = post(instances[0], loser)
        assert (retry.status_code, retry.json()) == (409, body)
    # Which patient wins is not deterministic; the test asserts the invariant, not the winner
    assert winners <= {"pat-77", "pat-99"}


# ================================================================ Test 4
# A crash around the ingestion commit loses no work (section 3, scenario 6.3): the job row is the
# hand-off, so "encounter saved, job not created" is unreachable. The service process is killed
# with os._exit at the named point (app/hooks.py, CRASH_AT).

ORPHANS = """
SELECT count(*) FROM encounters e
  LEFT JOIN summary_jobs j ON j.encounter_id = e.encounter_id AND j.version = e.current_version
 WHERE j.job_id IS NULL
"""


def crash_while_posting(db_url, crash_at, event):
    with ApiProcess(db_url, crash_at=crash_at) as doomed:
        with pytest.raises(httpx.TransportError):   # the partner receives no response
            doomed.post(event)
        assert doomed.proc.wait(10) == 17           # killed at the hook, not a clean exit


def test_4_crash_around_the_ingestion_commit_loses_no_work(fresh_db, db, mock, make_worker):
    mock.set_default(summary_of)
    enc_a, enc_b = new_id("enc-a"), new_id("enc-b")
    with ApiProcess(fresh_db) as service:
        for enc in (enc_a, enc_b):
            assert service.post(make_event(encounter_id=enc, version=10, transcription=f"{enc}-v10")).status_code == 201
    ev_a = make_event(encounter_id=enc_a, version=12, event_id="evt-102-a", transcription="A-v12")
    ev_b = make_event(encounter_id=enc_b, version=12, event_id="evt-102-b", transcription="B-v12")

    # A: crash after the job insert, before COMMIT
    crash_while_posting(fresh_db, "ingest.before_commit", ev_a)
    assert count(db, "SELECT count(*) FROM encounter_events WHERE event_id = 'evt-102-a'") == 0
    assert db.execute("SELECT version FROM summary_jobs WHERE encounter_id = %s", (enc_a,)).fetchall() == [(10,)]
    assert count(db, "SELECT current_version FROM encounters WHERE encounter_id = %s", enc_a) == 10

    # B: crash after COMMIT, before the HTTP response
    crash_while_posting(fresh_db, "ingest.after_commit", ev_b)
    assert count(db, "SELECT current_version FROM encounters WHERE encounter_id = %s", enc_b) == 12
    assert count(db, "SELECT count(*) FROM encounter_events WHERE event_id = 'evt-102-b'") == 1
    assert fetch_job(db, enc_b, 12)["status"] == "queued"

    # The partner retries against a healthy instance
    with ApiProcess(fresh_db) as service:
        r = service.post(ev_a)
        assert (r.status_code, r.json()["outcome"]) == (201, "accepted")
        r = service.post(ev_b)
        assert (r.status_code, r.json()["outcome"], r.json()["current_version"]) == (200, "duplicate", 12)
    for enc in (enc_a, enc_b):
        assert count(db, "SELECT count(*) FROM summary_jobs WHERE encounter_id = %s AND version = 12", enc) == 1

    # A worker processes the work: exactly one paid call per version 12, and both reach ready
    worker = make_worker()
    while worker.run_once() != "idle":
        pass
    calls = [c.transcription for c in mock.calls]
    assert (calls.count("A-v12"), calls.count("B-v12")) == (1, 1)
    assert [fetch_job(db, enc, 12)["status"] for enc in (enc_a, enc_b)] == ["ready", "ready"]
    assert count(db, ORPHANS) == 0


# ================================================================ Test 5
# An outage costs a bounded number of calls, and no work is lost (section 5, scenario 6.7): the
# breaker caps system-wide spend independent of queue size; the synthetic probe spends no job's
# budget; an SLA breach is not a failure. A 20-minute outage is compressed to a few seconds.

OUTAGE_SECONDS = 2.5
JOBS = 100
WORKERS = 4


def test_5_outage_costs_bounded_calls_and_loses_no_work(app_config, worker_config, db, mock, make_worker):
    config = replace(worker_config, breaker_cooldown_seconds=0.2, backoff_base_seconds=0.2)
    fail = TransientError("ai_unavailable")
    with TestClient(create_app(replace(app_config, sla_seconds=0.5))) as client:
        events = [make_event(version=1, transcription=f"job-{i}") for i in range(JOBS)]
        for ev in events:
            assert post(client, ev).status_code == 201

        mock.set_default(fail)                                    # the outage begins
        stop = threading.Event()
        runner = threading.Thread(target=run_loops, daemon=True,
                                  args=([make_worker(f"w-{i}", config=config) for i in range(WORKERS)], stop, 5))
        runner.start()
        deadline = time.monotonic() + 10
        while count(db, "SELECT state::text FROM circuit_breaker") == "closed" and time.monotonic() < deadline:
            time.sleep(0.01)
        opened = time.monotonic()
        assert count(db, "SELECT state::text FROM circuit_breaker") != "closed"

        # Calls already in flight when it tripped finish and are recorded; then the snapshot
        time.sleep(0.3)
        real_after_trip = mock.real_calls
        attempts_rows = count(db, "SELECT count(*) FROM job_attempts")
        tokens = dict(db.execute("SELECT job_id, attempts FROM summary_jobs").fetchall())
        # ~11 plus the calls already in flight (up to 4); not the ~500 that 100 x 5 would allow
        assert 11 <= real_after_trip <= 11 + WORKERS

        time.sleep(OUTAGE_SECONDS)
        # While open: no real calls, no new attempt rows, no fencing token moved
        assert mock.real_calls == real_after_trip
        assert count(db, "SELECT count(*) FROM job_attempts") == attempts_rows
        assert dict(db.execute("SELECT job_id, attempts FROM summary_jobs").fetchall()) == tokens
        # Probes: synthetic only, counted before they are sent, about one per cooldown
        probes, open_for = mock.probe_calls, time.monotonic() - opened
        assert 2 <= probes <= open_for / config.breaker_cooldown_seconds + 2
        assert 0 <= count(db, "SELECT probes_sent FROM circuit_breaker") - probes <= 1
        assert all(c.transcription == PROBE_TRANSCRIPT for c in mock.calls if c.kind == "probe")
        # No job fails; clients see processing, breached
        assert count(db, "SELECT count(*) FROM summary_jobs WHERE status = 'failed'") == 0
        for ev in events[::10]:
            body = client.get(f"/encounters/{ev['encounter_id']}/summary").json()
            assert (body["status"], body["sla_breached"]) == ("processing", True)

        mock.set_default(summary_of)                              # the service recovers
        deadline = time.monotonic() + 30
        while count(db, "SELECT count(*) FROM summary_jobs WHERE status = 'ready'") < JOBS \
                and time.monotonic() < deadline:
            time.sleep(0.05)
        stop.set()
        runner.join(10)

        breaker = db.execute("SELECT state::text, last_probe_outcome, probes_sent FROM circuit_breaker").fetchone()
        assert breaker[:2] == ("closed", "succeeded")
        assert breaker[2] == mock.probe_calls
        assert count(db, "SELECT count(*) FROM summary_jobs WHERE status = 'ready'") == JOBS
        assert count(db, "SELECT max(n) FROM (SELECT count(*) AS n FROM job_attempts "
                         "WHERE outcome <> 'skipped' GROUP BY job_id) t") <= 5
        assert mock.real_calls == real_after_trip + JOBS          # one successful call per job
        for ev in events:
            body = client.get(f"/encounters/{ev['encounter_id']}/summary").json()
            assert (body["status"], body["summary"], body["sla_breached"]) == \
                ("ready", summary_of(ev["payload"]["transcription"]), True)


# ================================================================ Test 6
# Forced results out of order (scenario 6.6): v13's call returns before v12's. v13 is ready, v12
# is superseded with superseded_by_version = 13, and GET never returns v12's summary.

def test_6_forced_results_out_of_order(api, reader, db, mock, make_worker):
    mock.set_default(summary_of)
    for _ in range(20):
        enc = new_id("enc")
        post(api, make_event(encounter_id=enc, version=12, transcription=f"{enc}-v12"))
        gate_v12 = Gate(summary_of(f"{enc}-v12"))
        mock.push(gate_v12)

        with Sampler(reader, enc) as sampler:
            a = run_in_background(make_worker("w-A"))            # v12's call is in flight, held
            assert gate_v12.entered.wait(5)
            post(api, make_event(encounter_id=enc, version=13, transcription=f"{enc}-v13"))
            assert make_worker("w-B").run_once() == "ready"       # v13 returns first
            gate_v12.release()
            assert a.join() == "superseded"                       # v12 returns later
            time.sleep(0.01)

        v12 = fetch_job(db, enc, 12)
        assert (v12["status"], v12["superseded_by_version"], v12["summary"]) == \
            ("superseded", 13, summary_of(f"{enc}-v12"))         # retained, never current
        assert fetch_job(db, enc, 13)["status"] == "ready"
        assert all(body.get("summary") != summary_of(f"{enc}-v12") for _, _, body in sampler.samples)
        final = reader.get(f"/encounters/{enc}/summary").json()
        assert (final["summary_version"], final["summary"]) == (13, summary_of(f"{enc}-v13"))


# ================================================================ Test 7
# A stalled worker is fenced (scenario 6.4; DECISIONS.md D1): A's call is held past its lease,
# B reclaims when the lease really expires and succeeds, then A's call returns. A's write matches
# zero rows and its attempt becomes discarded; B's result stands; the budget counts both.

def test_7_stalled_worker_is_fenced(api, db, mock, make_worker, worker_config):
    config = replace(worker_config, lease_seconds=1.0, ai_timeout_seconds=0.5, probe_deadline_seconds=1.0)
    for _ in range(3):
        ev = make_event()
        post(api, ev)
        gate = Gate("A-LATE-SUMMARY")
        mock.push(gate, "B-SUMMARY")
        calls_before = mock.real_calls

        a = run_in_background(make_worker("w-A", config=config))
        assert gate.entered.wait(5)
        job_id = fetch_job(db, ev["encounter_id"], 12)["job_id"]
        b = make_worker("w-B", config=config)
        assert b.run_once() == "idle"                              # the lease is live: nothing to claim

        deadline = time.monotonic() + 5
        outcome = "idle"
        while outcome == "idle" and time.monotonic() < deadline:  # wait for the real lease expiry
            time.sleep(0.05)
            outcome = b.run_once()
        assert outcome == "ready"
        assert [a_[:1] + a_[2:5] for a_ in fetch_attempts(db, job_id)] == [
            (1, "w-A", "lease_expired", "worker_lost"),
            (2, "w-B", "succeeded", None),
        ]

        gate.release()
        assert a.join() == "discarded"
        assert [a_[:1] + a_[2:5] for a_ in fetch_attempts(db, job_id)] == [
            (1, "w-A", "discarded", "worker_lost"),
            (2, "w-B", "succeeded", None),
        ]
        job = fetch_job(db, ev["encounter_id"], 12)
        assert (job["status"], job["summary"], job["attempts"]) == ("ready", "B-SUMMARY", 2)
        assert count(db, "SELECT count(*) FROM job_attempts WHERE job_id = %s AND redrive_generation = 0 "
                         "AND outcome <> 'skipped'", job_id) == 2     # both calls count against the budget
        assert mock.real_calls - calls_before == 2                     # both were paid for
        body = api.get(f"/encounters/{ev['encounter_id']}/summary").json()
        assert (body["status"], body["summary"]) == ("ready", "B-SUMMARY")
