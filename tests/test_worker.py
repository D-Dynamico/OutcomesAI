# Design section 5, worker happy path: claim with SKIP LOCKED and lease, pre-call supersede
# check, call outside any transaction, guarded write, attempt close-out, discarded on lost
# ownership. Scenarios 6.4 to 6.6.
from app.summary.mock import Gate, default_summary
from tests.factories import TRANSCRIPT, make_event, new_id, post
from tests.harness import drain, expire_lease, fetch_attempts, fetch_job, run_in_background


def test_happy_path(api, db, mock, make_worker):
    ev = make_event(version=12)
    post(api, ev)

    assert make_worker("w-A").run_once() == "ready"

    job = fetch_job(db, ev["encounter_id"], 12)
    assert job["status"] == "ready"
    assert job["summary"] == default_summary(TRANSCRIPT, 1)
    assert job["attempts"] == 1
    assert job["superseded_by_version"] is None
    assert job["started_at"] is not None and job["completed_at"] is not None
    assert fetch_attempts(db, job["job_id"]) == [(1, 0, "w-A", "succeeded", None, True)]
    assert (mock.real_calls, mock.probe_calls) == (1, 0)

    body = api.get(f"/encounters/{ev['encounter_id']}/summary").json()
    assert (body["status"], body["summary_version"], body["summary"]) == ("ready", 12, job["summary"])


def test_idle_when_nothing_to_claim(make_worker, mock):
    assert make_worker().run_once() == "idle"
    assert mock.calls == []


def test_oldest_accepted_first(api, mock, make_worker):
    events = [make_event(version=1, transcription=f"t{i}") for i in range(3)]
    for ev in events:
        post(api, ev)
    worker = make_worker()
    for _ in events:
        worker.run_once()
    assert [c.transcription for c in mock.calls] == ["t0", "t1", "t2"]


def test_jobs_that_are_not_claimable(api, db, mock, make_worker):
    # Section 5 table: backoff not yet due, live lease, and every terminal status
    setups = [
        "next_attempt_at = now() + interval '1 hour'",
        "status = 'processing', attempts = 1, lease_expires_at = now() + interval '1 hour'",
        "status = 'ready', summary = 's', completed_at = now()",
        "status = 'failed', completed_at = now()",
        "status = 'superseded', completed_at = now()",
    ]
    for setup in setups:
        ev = make_event()
        post(api, ev)
        db.execute(f"UPDATE summary_jobs SET {setup} WHERE encounter_id = %s", (ev["encounter_id"],))
    assert make_worker().run_once() == "idle"
    assert mock.calls == []


def test_concurrent_workers_never_claim_the_same_job(api, db, mock, make_worker):
    # FOR UPDATE SKIP LOCKED: every job claimed exactly once, no worker blocks another
    encounters = [new_id("enc") for _ in range(30)]
    for enc in encounters:
        post(api, make_event(encounter_id=enc, version=1, transcription=enc))

    drain([make_worker(f"w-{i}") for i in range(6)])

    assert db.execute("SELECT count(*), min(attempts), max(attempts) FROM summary_jobs "
                      "WHERE status = 'ready'").fetchone() == (30, 1, 1)
    assert db.execute("SELECT count(*) FROM job_attempts WHERE outcome = 'succeeded'").fetchone()[0] == 30
    assert sorted(c.transcription for c in mock.calls) == sorted(encounters)


def test_precall_check_skips_obsolete_job_without_calling(api, db, mock, make_worker):
    # Scenario 6.5, early case: v13 accepted before v12's call is sent
    v12 = make_event(version=12, transcription="v12")
    post(api, v12)
    post(api, make_event(encounter_id=v12["encounter_id"], version=13, transcription="v13"))
    worker = make_worker()

    assert worker.run_once() == "skipped"          # v12 is older, so claimed first
    job = fetch_job(db, v12["encounter_id"], 12)
    assert (job["status"], job["superseded_by_version"], job["summary"]) == ("superseded", 13, None)
    assert job["completed_at"] is not None
    assert fetch_attempts(db, job["job_id"]) == [(1, 0, "w-A", "skipped", None, True)]
    assert mock.calls == []                       # no call, no cost

    assert worker.run_once() == "ready"
    assert [c.transcription for c in mock.calls] == ["v13"]


def test_in_flight_result_for_obsolete_version_is_superseded(api, db, mock, make_worker):
    # Scenario 6.5: v12 in flight when v13 arrives; the guarded write labels it superseded
    v12 = make_event(version=12, transcription="v12")
    post(api, v12)
    gate = Gate("V12-SUMMARY")
    mock.push(gate)
    running = run_in_background(make_worker())
    assert gate.entered.wait(5)

    post(api, make_event(encounter_id=v12["encounter_id"], version=13, transcription="v13"))
    gate.release()

    assert running.join() == "superseded"
    job = fetch_job(db, v12["encounter_id"], 12)
    assert (job["status"], job["superseded_by_version"], job["summary"]) == ("superseded", 13, "V12-SUMMARY")
    assert fetch_attempts(db, job["job_id"])[0][3] == "superseded"
    body = api.get(f"/encounters/{v12['encounter_id']}/summary").json()
    assert (body["status"], body["current_version"]) == ("processing", 13)
    assert "V12-SUMMARY" not in str(body)


def test_results_out_of_order(api, db, mock, make_worker):
    # Scenario 6.6: v13 finishes before v12
    enc = new_id("enc")
    post(api, make_event(encounter_id=enc, version=12, transcription="v12"))
    gate_v12 = Gate("V12-SUMMARY")
    mock.push(gate_v12)
    first = run_in_background(make_worker("w-A"))    # claims v12, holds in the call
    assert gate_v12.entered.wait(5)

    post(api, make_event(encounter_id=enc, version=13, transcription="v13"))
    mock.push("V13-SUMMARY")
    assert make_worker("w-B").run_once() == "ready"  # v13 lands first
    gate_v12.release()
    assert first.join() == "superseded"              # v12 lands later

    assert fetch_job(db, enc, 13)["status"] == "ready"
    v12_job = fetch_job(db, enc, 12)
    assert (v12_job["status"], v12_job["superseded_by_version"]) == ("superseded", 13)
    body = api.get(f"/encounters/{enc}/summary").json()
    assert (body["summary_version"], body["summary"]) == (13, "V13-SUMMARY")


def test_no_transaction_or_lock_held_during_the_call(api, db, mock, make_worker):
    # Non-negotiables 4 and 6: the attempt row is committed before the call, and nothing
    # is held in the database while the call runs
    ev = make_event()
    post(api, ev)
    observed = {}

    def during_call(transcription):
        job = fetch_job(db, ev["encounter_id"], 12)
        observed["job_status"] = job["status"]
        observed["attempts"] = fetch_attempts(db, job["job_id"])
        observed["open_txns"] = db.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND state LIKE 'idle in transaction%'").fetchone()[0]
        observed["row_locks"] = db.execute(
            "SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
            "WHERE c.relname IN ('summary_jobs', 'job_attempts', 'encounters') "
            "AND l.pid <> pg_backend_pid()").fetchone()[0]
        return "ok"

    mock.push(during_call)
    assert make_worker().run_once() == "ready"
    assert observed == {
        "job_status": "processing",
        "attempts": [(1, 0, "w-A", "in_flight", None, False)],
        "open_txns": 0,
        "row_locks": 0,
    }


def test_stalled_worker_loses_ownership_and_its_result_is_discarded(api, db, mock, make_worker):
    # Scenario 6.4 and DECISIONS.md D1: A stalls past its lease, B reclaims and succeeds,
    # A's late write is fenced out and its attempt becomes discarded
    ev = make_event()
    post(api, ev)
    gate = Gate("A-LATE-SUMMARY")
    mock.push(gate, "B-SUMMARY")
    a = run_in_background(make_worker("w-A"))
    assert gate.entered.wait(5)
    job_id = fetch_job(db, ev["encounter_id"], 12)["job_id"]

    expire_lease(db, job_id)
    assert make_worker("w-B").run_once() == "ready"
    assert fetch_attempts(db, job_id) == [
        (1, 0, "w-A", "lease_expired", "worker_lost", True),
        (2, 0, "w-B", "succeeded", None, True),
    ]

    gate.release()
    assert a.join() == "discarded"

    job = fetch_job(db, ev["encounter_id"], 12)
    assert (job["status"], job["summary"], job["attempts"]) == ("ready", "B-SUMMARY", 2)
    assert fetch_attempts(db, job_id) == [
        (1, 0, "w-A", "discarded", "worker_lost", True),
        (2, 0, "w-B", "succeeded", None, True),
    ]
    assert mock.real_calls == 2   # both calls were paid for; only one result written


def test_precall_check_is_fenced(api, db, pool, mock, make_worker):
    # A worker whose token has moved on cannot mark the job superseded
    from app.worker.claim import claim
    from app.worker.precall import precall_supersede

    ev = make_event(version=12)
    post(api, ev)
    job = claim(pool, "w-A", 60)
    db.execute("UPDATE summary_jobs SET attempts = attempts + 1 WHERE job_id = %s", (job.job_id,))
    post(api, make_event(encounter_id=ev["encounter_id"], version=13))

    assert precall_supersede(pool, job) is False
    assert fetch_job(db, ev["encounter_id"], 12)["status"] == "processing"
