# Design section 4: GET resolves through current_version; processing, ready, failed, 404;
# computed sla_breached; attempts and error_class derived from job_attempts.
import re

from app.api.response import iso_utc
from tests.factories import TRANSCRIPT, make_event, post

ISO_Z = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def get(client, encounter_id):
    return client.get(f"/encounters/{encounter_id}/summary")


def job_times(db, encounter_id, version):
    return db.execute(
        "SELECT accepted_at, completed_at FROM summary_jobs WHERE encounter_id = %s AND version = %s",
        (encounter_id, version)).fetchone()


def set_job(db, encounter_id, version, sql, *params):
    db.execute(f"UPDATE summary_jobs SET {sql} WHERE encounter_id = %s AND version = %s",
               (*params, encounter_id, version))


def job_id(db, encounter_id, version):
    return db.execute("SELECT job_id FROM summary_jobs WHERE encounter_id = %s AND version = %s",
                      (encounter_id, version)).fetchone()[0]


def add_attempt(db, job, attempt_no, outcome, error_class=None, generation=0):
    db.execute(
        "INSERT INTO job_attempts (job_id, attempt_no, redrive_generation, worker_id, outcome, "
        "error_class, finished_at) VALUES (%s, %s, %s, 'w-test', %s, %s, now())",
        (job, attempt_no, generation, outcome, error_class))


# ---------------------------------------------------------------- 404

def test_unknown_encounter_is_404(api):
    r = get(api, "enc-999")
    assert r.status_code == 404
    assert r.json() == {"error": "encounter_not_found", "encounter_id": "enc-999"}


def test_encounter_without_current_job_is_404_not_a_crash(api, db):
    # Section 4: impossible given single-transaction ingestion, but defined (DECISIONS.md D18)
    ev = make_event()
    post(api, ev)
    db.execute("DELETE FROM summary_jobs WHERE encounter_id = %s", (ev["encounter_id"],))
    r = get(api, ev["encounter_id"])
    assert r.status_code == 404
    assert r.json() == {"error": "encounter_not_found", "encounter_id": ev["encounter_id"]}


# ---------------------------------------------------------------- processing

def test_queued_job_is_reported_as_processing(api, db):
    ev = make_event()
    post(api, ev)
    accepted_at, _ = job_times(db, ev["encounter_id"], 12)

    r = get(api, ev["encounter_id"])
    assert r.status_code == 200
    assert r.json() == {
        "encounter_id": ev["encounter_id"],
        "patient_id": "pat-77",
        "encounter_type": "TelephoneTriage",
        "current_version": 12,
        "status": "processing",
        "summary": None,
        "accepted_at": iso_utc(accepted_at),
        "sla_breached": False,
    }
    assert ISO_Z.match(r.json()["accepted_at"])


def test_processing_job_is_reported_as_processing(api, db):
    ev = make_event()
    post(api, ev)
    set_job(db, ev["encounter_id"], 12,
            "status = 'processing', attempts = 1, lease_expires_at = now() + interval '60 seconds'")
    assert get(api, ev["encounter_id"]).json()["status"] == "processing"


def test_processing_sla_breach_uses_database_clock(api, db):
    ev = make_event()
    post(api, ev)
    set_job(db, ev["encounter_id"], 12, "accepted_at = now() - interval '9 seconds'")
    assert get(api, ev["encounter_id"]).json()["sla_breached"] is False
    set_job(db, ev["encounter_id"], 12, "accepted_at = now() - interval '11 seconds'")
    assert get(api, ev["encounter_id"]).json()["sla_breached"] is True


# ---------------------------------------------------------------- ready

def make_ready(db, encounter_id, version, summary, seconds_taken):
    set_job(db, encounter_id, version,
            "status = 'ready', summary = %s, completed_at = accepted_at + make_interval(secs => %s)",
            summary, seconds_taken)


def test_ready(api, db):
    ev = make_event()
    post(api, ev)
    make_ready(db, ev["encounter_id"], 12, "Patient reported sore throat and fever.", 6)
    accepted_at, completed_at = job_times(db, ev["encounter_id"], 12)

    r = get(api, ev["encounter_id"])
    assert r.status_code == 200
    assert r.json() == {
        "encounter_id": ev["encounter_id"],
        "patient_id": "pat-77",
        "encounter_type": "TelephoneTriage",
        "current_version": 12,
        "status": "ready",
        "summary_version": 12,
        "summary": "Patient reported sore throat and fever.",
        "accepted_at": iso_utc(accepted_at),
        "completed_at": iso_utc(completed_at),
        "sla_breached": False,
    }


def test_ready_late_is_breached(api, db):
    ev = make_event()
    post(api, ev)
    make_ready(db, ev["encounter_id"], 12, "s", 11)
    body = get(api, ev["encounter_id"]).json()
    assert (body["status"], body["sla_breached"]) == ("ready", True)


# ---------------------------------------------------------------- failed

def test_failed_derives_attempts_and_error_class_from_job_attempts(api, db):
    ev = make_event()
    post(api, ev)
    job = job_id(db, ev["encounter_id"], 12)
    # Generation 0: history before a redrive; must not count
    for n in range(1, 6):
        add_attempt(db, job, n, "transient_error", "ai_unavailable", generation=0)
    # Generation 1: skipped attempts never count; latest non-skipped gives error_class
    add_attempt(db, job, 6, "skipped", generation=1)
    add_attempt(db, job, 7, "transient_error", "ai_unavailable", generation=1)
    add_attempt(db, job, 8, "lease_expired", "worker_lost", generation=1)
    add_attempt(db, job, 9, "transient_error", "rate_limited", generation=1)
    # Fencing token (attempts) deliberately differs from the reported count, and skips a value
    set_job(db, ev["encounter_id"], 12,
            "status = 'failed', attempts = 10, redrive_generation = 1, "
            "completed_at = accepted_at + interval '3 seconds'")
    accepted_at, completed_at = job_times(db, ev["encounter_id"], 12)

    r = get(api, ev["encounter_id"])
    assert r.status_code == 200
    assert r.json() == {
        "encounter_id": ev["encounter_id"],
        "patient_id": "pat-77",
        "encounter_type": "TelephoneTriage",
        "current_version": 12,
        "status": "failed",
        "summary": None,
        "attempts": 3,
        "error_class": "rate_limited",
        "accepted_at": iso_utc(accepted_at),
        "completed_at": iso_utc(completed_at),
        # Section 5: a failed job never met the target, even if it failed fast
        "sla_breached": True,
    }


def test_failed_on_reclaim_reports_worker_lost(api, db):
    # Section 5: budget exhausted on reclaim; attempt_no skips a value, no new attempt row
    ev = make_event()
    post(api, ev)
    job = job_id(db, ev["encounter_id"], 12)
    for n in range(1, 6):
        add_attempt(db, job, n, "lease_expired", "worker_lost")
    set_job(db, ev["encounter_id"], 12, "status = 'failed', attempts = 6, completed_at = now()")
    body = get(api, ev["encounter_id"]).json()
    assert (body["status"], body["attempts"], body["error_class"]) == ("failed", 5, "worker_lost")


# ---------------------------------------------------------------- resolution through current_version

def test_newer_version_hides_older_ready_summary(api, db):
    # Scenario 6.5: v12 ready, v13 accepted; GET reports v13 processing, never v12's summary
    v12 = make_event(version=12)
    post(api, v12)
    make_ready(db, v12["encounter_id"], 12, "OLD-V12-SUMMARY", 5)
    post(api, make_event(encounter_id=v12["encounter_id"], version=13, transcription="v13"))

    r = get(api, v12["encounter_id"])
    body = r.json()
    assert (body["status"], body["current_version"], body["summary"]) == ("processing", 13, None)
    assert "OLD-V12-SUMMARY" not in r.text


def test_older_result_landing_late_never_appears(api, db):
    # Scenario 6.6: v13 ready first, then v12's result is written; GET still shows v13's
    enc = make_event(version=12)
    post(api, enc)
    post(api, make_event(encounter_id=enc["encounter_id"], version=13))
    make_ready(db, enc["encounter_id"], 13, "V13-SUMMARY", 4)
    make_ready(db, enc["encounter_id"], 12, "V12-SUMMARY", 9)

    body = get(api, enc["encounter_id"]).json()
    assert (body["summary_version"], body["summary"]) == (13, "V13-SUMMARY")


def test_get_via_location_header_with_encoded_id(api):
    ev = make_event(encounter_id="enc/1 x?")
    location = post(api, ev).headers["location"]
    r = api.get(location)
    assert r.status_code == 200
    assert r.json()["encounter_id"] == "enc/1 x?"


def test_get_never_returns_transcript(api):
    ev = make_event()
    post(api, ev)
    assert TRANSCRIPT not in get(api, ev["encounter_id"]).text
