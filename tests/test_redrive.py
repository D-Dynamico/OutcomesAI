# Design section 5 ("Failed is terminal; redrive is deliberate") and section 6.7: a failed
# job is redriven only while it is still current, with a fresh budget in a new redrive
# generation and the fencing token untouched; otherwise it is marked superseded.
# Endpoints per DECISIONS.md D4 and D26.
import threading

import pytest

from app.summary.client import TransientError
from app.summary.mock import Gate
from tests.factories import make_event, new_id, post
from tests.harness import fetch_attempts, fetch_job, run_in_background


def fail_job(db, job_id, *, completed_at="now()", error_class="ai_unavailable"):
    """Stored state of a job that exhausted its budget in generation 0 (section 5)."""
    for n in range(1, 6):
        db.execute("INSERT INTO job_attempts (job_id, attempt_no, redrive_generation, worker_id, "
                   "outcome, error_class, finished_at) VALUES (%s, %s, 0, 'w-old', "
                   "'transient_error', %s, now())", (job_id, n, error_class))
    db.execute(f"UPDATE summary_jobs SET status = 'failed', attempts = 5, completed_at = {completed_at}, "
               "started_at = accepted_at WHERE job_id = %s", (job_id,))


def failed_job(api, db, **kwargs):
    ev = make_event(version=12, **kwargs)
    post(api, ev)
    job = fetch_job(db, ev["encounter_id"], 12)
    fail_job(db, job["job_id"])
    return ev, job["job_id"]


def redrive(api, job_id):
    return api.post(f"/admin/jobs/{job_id}/redrive")


def redrive_window(api, failed_from, failed_to):
    return api.post("/admin/jobs/redrive", json={"failed_from": failed_from, "failed_to": failed_to})


def db_time(db, sql):
    return db.execute(f"SELECT ({sql})::timestamptz").fetchone()[0].isoformat()


# ---------------------------------------------------------------- single job

def test_redrive_current_failed_job(api, db, mock, make_worker):
    ev, job_id = failed_job(api, db)

    r = redrive(api, job_id)
    assert r.status_code == 200
    assert r.json() == {"job_id": job_id, "outcome": "redriven"}
    job = fetch_job(db, ev["encounter_id"], 12)
    assert (job["status"], job["redrive_generation"], job["completed_at"]) == ("queued", 1, None)
    assert job["attempts"] == 5          # the fencing token is never reset
    assert api.get(f"/encounters/{ev['encounter_id']}/summary").json()["status"] == "processing"

    # Due immediately, with a fresh budget in generation 1
    assert make_worker().run_once() == "ready"
    attempts = fetch_attempts(db, job_id)
    assert [(a[0], a[1], a[3]) for a in attempts] == \
        [(n, 0, "transient_error") for n in range(1, 6)] + [(6, 1, "succeeded")]
    body = api.get(f"/encounters/{ev['encounter_id']}/summary").json()
    assert (body["status"], body["summary_version"]) == ("ready", 12)


def test_redrive_obsolete_failed_job_marks_it_superseded(api, db, mock, make_worker):
    # Paying to summarise it would produce a result the read path can never show
    ev, job_id = failed_job(api, db, transcription="v12")
    post(api, make_event(encounter_id=ev["encounter_id"], version=13, transcription="v13"))
    post(api, make_event(encounter_id=ev["encounter_id"], version=15, transcription="v15"))

    r = redrive(api, job_id)
    assert r.status_code == 200
    assert r.json() == {"job_id": job_id, "outcome": "superseded", "superseded_by_version": 15}
    job = fetch_job(db, ev["encounter_id"], 12)
    assert (job["status"], job["superseded_by_version"], job["redrive_generation"]) == ("superseded", 15, 0)

    worker = make_worker()
    while worker.run_once() != "idle":
        pass
    assert [c.transcription for c in mock.calls] == ["v15"]   # v13 skipped for free; v12 never
    assert len(fetch_attempts(db, job_id)) == 5              # no new attempt on the v12 job


@pytest.mark.parametrize("setup", [
    "status = 'queued'",
    "status = 'processing', attempts = 1, lease_expires_at = now() + interval '1 minute'",
    "status = 'ready', summary = 's', completed_at = now()",
    "status = 'superseded', superseded_by_version = 13, completed_at = now()",
])
def test_redrive_refuses_jobs_that_are_not_failed(api, db, setup):
    ev = make_event()
    post(api, ev)
    job_id = fetch_job(db, ev["encounter_id"], 12)["job_id"]
    db.execute(f"UPDATE summary_jobs SET {setup} WHERE job_id = %s", (job_id,))
    before = fetch_job(db, ev["encounter_id"], 12)

    r = redrive(api, job_id)
    assert r.status_code == 409
    assert r.json() == {"error": "job_not_failed", "job_id": job_id, "status": before["status"]}
    assert fetch_job(db, ev["encounter_id"], 12) == before


def test_redrive_unknown_job(api):
    r = redrive(api, 999999)
    assert (r.status_code, r.json()) == (404, {"error": "job_not_found", "job_id": 999999})


@pytest.mark.parametrize("job_id", ["abc", "-1", "1.5", str(2**63)])
def test_redrive_invalid_job_id(api, job_id):
    r = redrive(api, job_id)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_request"


def test_concurrent_redrives_of_one_job_redrive_it_once(api, db):
    ev, job_id = failed_job(api, db)
    barrier = threading.Barrier(4)
    results = []

    def go():
        barrier.wait()
        results.append(redrive(api, job_id).status_code)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [200, 409, 409, 409]
    assert fetch_job(db, ev["encounter_id"], 12)["redrive_generation"] == 1


# ---------------------------------------------------------------- budget and fencing across generations

def test_redrive_grants_a_fresh_budget_of_five(api, db, mock, make_worker):
    ev, job_id = failed_job(api, db)
    redrive(api, job_id)
    mock.set_default(TransientError("rate_limited"))
    worker = make_worker()
    for _ in range(4):
        assert worker.run_once() == "transient_error"
        db.execute("UPDATE summary_jobs SET next_attempt_at = now() WHERE job_id = %s", (job_id,))
    assert worker.run_once() == "failed"

    job = fetch_job(db, ev["encounter_id"], 12)
    assert (job["status"], job["attempts"], job["redrive_generation"]) == ("failed", 10, 1)
    body = api.get(f"/encounters/{ev['encounter_id']}/summary").json()
    # attempts counts only the current generation; error_class is its latest attempt
    assert (body["status"], body["attempts"], body["error_class"]) == ("failed", 5, "rate_limited")
    assert len(fetch_attempts(db, job_id)) == 10  # the full cost across generations stays visible


def test_worker_from_before_the_failure_cannot_write_after_redrive(api, db, mock, make_worker):
    # Section 5: attempts is never reset, so a hung worker holding an old attempt number can
    # never match a post-redrive claim
    ev = make_event()
    post(api, ev)
    job_id = fetch_job(db, ev["encounter_id"], 12)["job_id"]
    gate = Gate("STALE-SUMMARY")
    mock.push(gate, "FRESH-SUMMARY")
    stale = run_in_background(make_worker("w-stale"))       # holds attempt 1
    assert gate.entered.wait(5)

    # Meanwhile the job exhausts its budget elsewhere and an operator redrives it
    db.execute("UPDATE summary_jobs SET status = 'failed', attempts = 5, completed_at = now(), "
               "lease_expires_at = NULL WHERE job_id = %s", (job_id,))
    assert redrive(api, job_id).json()["outcome"] == "redriven"
    assert make_worker("w-fresh").run_once() == "ready"      # attempt 6

    gate.release()
    assert stale.join() == "discarded"
    job = fetch_job(db, ev["encounter_id"], 12)
    assert (job["status"], job["summary"], job["attempts"]) == ("ready", "FRESH-SUMMARY", 6)


# ---------------------------------------------------------------- bulk, by time window

def test_bulk_redrive_by_window(api, db):
    inside = "now() - interval '30 minutes'"
    outside = "now() - interval '3 hours'"
    jobs = {}
    for name, completed_at in [("current_a", inside), ("current_b", inside),
                               ("obsolete", inside), ("too_old", outside)]:
        ev = make_event(encounter_id=new_id(name.replace("_", "-")))
        post(api, ev)
        job_id = fetch_job(db, ev["encounter_id"], 12)["job_id"]
        fail_job(db, job_id, completed_at=completed_at)
        jobs[name] = (ev, job_id)
    post(api, make_event(encounter_id=jobs["obsolete"][0]["encounter_id"], version=13))
    ready = make_event()
    post(api, ready)
    db.execute("UPDATE summary_jobs SET status = 'ready', summary = 's', completed_at = now() - "
               "interval '30 minutes' WHERE encounter_id = %s", (ready["encounter_id"],))

    r = redrive_window(api, db_time(db, "now() - interval '1 hour'"), db_time(db, "now()"))
    assert r.status_code == 200
    assert r.json() == {"redriven": 2, "superseded": 1}

    def status(name):
        ev, _ = jobs[name]
        return fetch_job(db, ev["encounter_id"], 12)["status"]
    assert [status(n) for n in ("current_a", "current_b", "obsolete", "too_old")] == \
        ["queued", "queued", "superseded", "failed"]
    assert fetch_job(db, ready["encounter_id"], 12)["status"] == "ready"

    # Idempotent in effect: nothing left to do in that window
    r = redrive_window(api, db_time(db, "now() - interval '1 hour'"), db_time(db, "now()"))
    assert r.json() == {"redriven": 0, "superseded": 0}


@pytest.mark.parametrize("body,detail", [
    ({"failed_to": "2026-09-28T10:00:00Z"}, "failed_from must be an ISO 8601 timestamp with a timezone"),
    ({"failed_from": "yesterday", "failed_to": "2026-09-28T10:00:00Z"},
     "failed_from must be an ISO 8601 timestamp with a timezone"),
    ({"failed_from": "2026-09-28T09:00:00", "failed_to": "2026-09-28T10:00:00Z"},
     "failed_from must be an ISO 8601 timestamp with a timezone"),
    ({"failed_from": "2026-09-28T10:00:00Z", "failed_to": "2026-09-28T10:00:00Z"},
     "failed_from must be earlier than failed_to"),
    ([1, 2], "body must be a JSON object"),
])
def test_bulk_redrive_validation(api, body, detail):
    r = api.post("/admin/jobs/redrive", json=body)
    assert (r.status_code, r.json()) == (400, {"error": "invalid_request", "detail": detail})


def test_bulk_redrive_rejects_non_json(api):
    r = api.post("/admin/jobs/redrive", content=b"not json")
    assert (r.status_code, r.json()["detail"]) == (400, "body must be a JSON object")
