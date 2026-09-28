# Design section 5, failure handling: transient errors fail the attempt, not the job; backoff
# grows with jitter; the budget of 5 counts non-skipped attempts in the current redrive
# generation; reclaims close out abandoned attempts and check the budget. Scenario 6.4, 6.7.
import pytest

from app.summary.client import TransientError
from app.summary.mock import Gate
from app.worker.claim import claim
from tests.factories import make_event, post
from tests.harness import expire_lease, fetch_attempts, fetch_job, run_in_background


class FixedJitter:
    """Stands in for random.Random so backoff is exact: uniform(a, b) always returns `factor`."""

    def __init__(self, factor):
        self.factor = factor

    def uniform(self, a, b):
        assert (a, b) == (0.5, 1.0)
        return self.factor


def backoff_of_last_failure(db, job_id):
    # next_attempt_at and finished_at both come from now() in the same transaction
    return db.execute(
        "SELECT extract(epoch FROM j.next_attempt_at - a.finished_at)::float "
        "FROM summary_jobs j JOIN job_attempts a ON a.job_id = j.job_id "
        "WHERE j.job_id = %s ORDER BY a.attempt_no DESC LIMIT 1", (job_id,)).fetchone()[0]


def make_due(db, job_id):
    db.execute("UPDATE summary_jobs SET next_attempt_at = now() WHERE job_id = %s", (job_id,))


def budget_used(db, job_id, generation=0):
    return db.execute(
        "SELECT count(*) FROM job_attempts WHERE job_id = %s AND redrive_generation = %s "
        "AND outcome <> 'skipped'", (job_id, generation)).fetchone()[0]


@pytest.fixture
def posted(api, db):
    ev = make_event()
    post(api, ev)
    return ev, fetch_job(db, ev["encounter_id"], 12)["job_id"]


def test_transient_error_requeues_with_backoff(db, mock, make_worker, posted):
    ev, job_id = posted
    mock.push(TransientError("rate_limited"))

    assert make_worker().run_once() == "transient_error"

    job = fetch_job(db, ev["encounter_id"], 12)
    assert (job["status"], job["attempts"], job["lease_expires_at"], job["completed_at"]) == \
        ("queued", 1, None, None)
    assert fetch_attempts(db, job_id) == [(1, 0, "w-A", "transient_error", "rate_limited", True)]
    assert 5 <= backoff_of_last_failure(db, job_id) <= 10   # 0.5-1.0 x 10 s
    # The backoff is honoured: not claimable until next_attempt_at
    assert make_worker().run_once() == "idle"


def test_backoff_doubles_and_fifth_failure_fails_the_job(api, db, mock, make_worker, posted):
    ev, job_id = posted
    worker = make_worker(rng=FixedJitter(1.0))
    mock.set_default(TransientError("ai_unavailable"))

    backoffs = []
    for _ in range(4):
        assert worker.run_once() == "transient_error"
        backoffs.append(round(backoff_of_last_failure(db, job_id), 3))
        make_due(db, job_id)
    assert backoffs == [10, 20, 40, 80]

    assert worker.run_once() == "failed"
    job = fetch_job(db, ev["encounter_id"], 12)
    assert (job["status"], job["lease_expires_at"]) == ("failed", None)
    assert job["completed_at"] is not None
    assert [a[3] for a in fetch_attempts(db, job_id)] == ["transient_error"] * 5
    assert mock.real_calls == 5

    # Failed is terminal for automation: never claimed again
    make_due(db, job_id)
    assert worker.run_once() == "idle"
    assert mock.real_calls == 5

    body = api.get(f"/encounters/{ev['encounter_id']}/summary").json()
    assert (body["status"], body["attempts"], body["error_class"], body["sla_breached"]) == \
        ("failed", 5, "ai_unavailable", True)


def test_jitter_stays_within_bounds(db, mock, make_worker, posted):
    _, job_id = posted
    worker = make_worker()   # real random jitter
    mock.set_default(TransientError("ai_timeout"))
    for n in range(1, 5):
        worker.run_once()
        assert 0.5 * 10 * 2 ** (n - 1) <= backoff_of_last_failure(db, job_id) <= 10 * 2 ** (n - 1)
        make_due(db, job_id)


def test_job_recovers_after_transient_errors(api, db, mock, make_worker, posted):
    ev, job_id = posted
    mock.push(TransientError("ai_timeout"), TransientError("rate_limited"), "RECOVERED")
    worker = make_worker()
    for _ in range(2):
        assert worker.run_once() == "transient_error"
        make_due(db, job_id)
    assert worker.run_once() == "ready"
    assert [a[3] for a in fetch_attempts(db, job_id)] == ["transient_error", "transient_error", "succeeded"]
    body = api.get(f"/encounters/{ev['encounter_id']}/summary").json()
    assert (body["status"], body["summary"]) == ("ready", "RECOVERED")


def test_internal_error_class_is_recorded_as_transient(db, mock, make_worker, posted):
    _, job_id = posted
    mock.push(TransientError("internal"))
    assert make_worker().run_once() == "transient_error"
    assert fetch_attempts(db, job_id)[0][3:5] == ("transient_error", "internal")


def test_budget_is_scoped_to_the_redrive_generation(db, mock, make_worker, posted):
    # Five failures in generation 0 do not count once the job is in generation 1
    ev, job_id = posted
    for n in range(1, 6):
        db.execute("INSERT INTO job_attempts (job_id, attempt_no, redrive_generation, worker_id, outcome, "
                   "error_class, finished_at) VALUES (%s, %s, 0, 'w-old', 'transient_error', "
                   "'ai_unavailable', now())", (job_id, n))
    db.execute("UPDATE summary_jobs SET attempts = 5, redrive_generation = 1 WHERE job_id = %s", (job_id,))
    mock.push(TransientError("ai_unavailable"))

    assert make_worker().run_once() == "transient_error"
    assert fetch_job(db, ev["encounter_id"], 12)["status"] == "queued"
    assert budget_used(db, job_id, generation=1) == 1
    assert fetch_attempts(db, job_id)[-1][:2] == (6, 1)


def test_transient_error_after_lost_ownership_changes_nothing(db, mock, make_worker, posted):
    # The stalled worker's failure write is fenced out; its attempt stays lease_expired (D1)
    ev, job_id = posted
    gate = Gate(TransientError("ai_timeout"))
    mock.push(gate, "B-SUMMARY")
    a = run_in_background(make_worker("w-A"))
    assert gate.entered.wait(5)
    expire_lease(db, job_id)
    assert make_worker("w-B").run_once() == "ready"

    gate.release()
    assert a.join() == "lost_ownership"
    job = fetch_job(db, ev["encounter_id"], 12)
    assert (job["status"], job["summary"]) == ("ready", "B-SUMMARY")
    assert fetch_attempts(db, job_id) == [
        (1, 0, "w-A", "lease_expired", "worker_lost", True),
        (2, 0, "w-B", "succeeded", None, True),
    ]


# ---------------------------------------------------------------- reclaim and the budget

def crash_after_claim(pool, db, worker_id="w-crash"):
    """A worker claims and dies: its attempt stays in_flight until the lease expires."""
    job = claim(pool, worker_id, 60, 5)
    assert job is not None and not job.failed_on_reclaim
    expire_lease(db, job.job_id)
    return job


def test_reclaim_closes_abandoned_attempt_and_retries(db, pool, mock, make_worker, posted):
    ev, job_id = posted
    crash_after_claim(pool, db)

    assert make_worker("w-B").run_once() == "ready"
    assert fetch_attempts(db, job_id) == [
        (1, 0, "w-crash", "lease_expired", "worker_lost", True),
        (2, 0, "w-B", "succeeded", None, True),
    ]
    job = fetch_job(db, ev["encounter_id"], 12)
    assert job["attempts"] == 2
    assert budget_used(db, job_id) == 2   # the crashed attempt counts as paid


def test_poison_job_fails_on_reclaim_after_five_crashes(api, db, pool, mock, make_worker, posted):
    # Scenario 6.4: a transcript that crashes the worker every time
    ev, job_id = posted
    started_at = None
    for _ in range(5):
        crash_after_claim(pool, db)
        started_at = started_at or fetch_job(db, ev["encounter_id"], 12)["started_at"]

    assert make_worker("w-B").run_once() == "failed_on_reclaim"

    job = fetch_job(db, ev["encounter_id"], 12)
    assert (job["status"], job["lease_expires_at"]) == ("failed", None)
    assert job["completed_at"] is not None
    assert job["started_at"] == started_at          # set on the first claim only
    assert job["attempts"] == 6                     # token moved; no 6th attempt row (a gap)
    assert [a[:1] + a[3:5] for a in fetch_attempts(db, job_id)] == \
        [(n, "lease_expired", "worker_lost") for n in range(1, 6)]
    assert mock.calls == []                         # crashes before the call: nothing paid

    body = api.get(f"/encounters/{ev['encounter_id']}/summary").json()
    assert (body["status"], body["attempts"], body["error_class"]) == ("failed", 5, "worker_lost")


def test_fourth_crash_still_gets_a_fifth_attempt(db, pool, mock, make_worker, posted):
    ev, job_id = posted
    for _ in range(4):
        crash_after_claim(pool, db)
    assert make_worker("w-B").run_once() == "ready"
    assert budget_used(db, job_id) == 5


def test_budget_counts_crashes_and_transient_errors_together(db, pool, mock, make_worker, posted):
    ev, job_id = posted
    worker = make_worker()
    mock.set_default(TransientError("rate_limited"))
    for _ in range(3):
        assert worker.run_once() == "transient_error"
        make_due(db, job_id)
    crash_after_claim(pool, db)
    crash_after_claim(pool, db)

    assert worker.run_once() == "failed_on_reclaim"
    assert fetch_job(db, ev["encounter_id"], 12)["status"] == "failed"
    assert mock.real_calls == 3


def test_non_provider_error_crashes_the_loop_and_the_job_is_reclaimed(db, mock, make_worker, posted):
    # DECISIONS.md D5: a bug is not recorded as a provider failure
    ev, job_id = posted
    def bug(_):
        raise KeyError("not a provider failure")
    mock.push(bug)

    with pytest.raises(KeyError):
        make_worker("w-A").run_once()
    assert fetch_attempts(db, job_id) == [(1, 0, "w-A", "in_flight", None, False)]
    assert fetch_job(db, ev["encounter_id"], 12)["status"] == "processing"

    expire_lease(db, job_id)
    assert make_worker("w-B").run_once() == "ready"
    assert fetch_attempts(db, job_id)[0][3:5] == ("lease_expired", "worker_lost")
