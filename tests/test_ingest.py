# Design sections 1, 3 and 4: every ingestion outcome, its exact body, and what is stored.
import hashlib

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from tests.conftest import TEST_MAX_BODY_BYTES
from tests.factories import TRANSCRIPT, make_event, new_id, post


def stored(db, encounter_id):
    enc = db.execute(
        "SELECT patient_id, encounter_type::text, current_version, transcription "
        "FROM encounters WHERE encounter_id = %s", (encounter_id,)).fetchone()
    events = db.execute(
        "SELECT event_id, version, payload_hash FROM encounter_events "
        "WHERE encounter_id = %s ORDER BY version", (encounter_id,)).fetchall()
    jobs = db.execute(
        "SELECT version, event_id, status::text, input_transcription FROM summary_jobs "
        "WHERE encounter_id = %s ORDER BY version", (encounter_id,)).fetchall()
    return enc, events, jobs


def no_uncommitted_shells(db):
    # Section 3: no committed row ever sits at version 0 or has a null transcript
    return db.execute(
        "SELECT count(*) FROM encounters WHERE current_version = 0 OR transcription IS NULL"
    ).fetchone()[0] == 0


# ---------------------------------------------------------------- accepted

def test_accepted_new_encounter(api, db):
    ev = make_event(version=1)
    r = post(api, ev)

    assert r.status_code == 201
    assert r.headers["location"] == f"/encounters/{ev['encounter_id']}/summary"
    assert r.json() == {
        "outcome": "accepted",
        "encounter_id": ev["encounter_id"],
        "version": 1,
        "current_version": 1,
        "job_status": "queued",
    }
    enc, events, jobs = stored(db, ev["encounter_id"])
    assert enc == ("pat-77", "TelephoneTriage", 1, TRANSCRIPT)
    assert events == [(ev["event_id"], 1, hashlib.sha256(TRANSCRIPT.encode()).digest())]
    assert jobs == [(1, ev["event_id"], "queued", TRANSCRIPT)]


def test_accepted_newer_version_with_gap(api, db):
    # Scenario 6.2: v12 accepted while v10 is stored; v11 never needed
    first = make_event(version=10, transcription="v10")
    post(api, first)
    ev = make_event(encounter_id=first["encounter_id"], version=12, transcription="v12")
    r = post(api, ev)

    assert r.status_code == 201
    assert r.json()["current_version"] == 12
    enc, events, jobs = stored(db, ev["encounter_id"])
    assert enc[2:] == (12, "v12")
    assert [j[0] for j in jobs] == [10, 12]
    assert jobs[1][3] == "v12"  # each job keeps its own input snapshot
    assert jobs[0][3] == "v10"


def test_location_header_encodes_encounter_id(api):
    ev = make_event(encounter_id="enc/1 x?")
    r = post(api, ev)
    assert r.status_code == 201
    assert r.headers["location"] == "/encounters/enc%2F1%20x%3F/summary"


# ---------------------------------------------------------------- duplicate

def test_duplicate_plain_retry(api, db):
    ev = make_event()
    post(api, ev)
    before = stored(db, ev["encounter_id"])

    r = post(api, ev)
    assert r.status_code == 200
    assert r.json() == {
        "outcome": "duplicate",
        "encounter_id": ev["encounter_id"],
        "version": 12,
        "current_version": 12,
    }
    assert stored(db, ev["encounter_id"]) == before


def test_duplicate_beats_stale(api, db):
    # Section 1: v12 accepted, v13 accepted, then a retry of v12 is duplicate, not stale
    v12 = make_event(version=12)
    post(api, v12)
    post(api, make_event(encounter_id=v12["encounter_id"], version=13, transcription="v13"))

    r = post(api, v12)
    assert r.status_code == 200
    assert r.json() == {
        "outcome": "duplicate",
        "encounter_id": v12["encounter_id"],
        "version": 12,
        "current_version": 13,
    }


def test_duplicate_source_defect_fresh_event_id(api, db):
    # Section 1, second door: different event_id, same (encounter_id, version), same payload
    ev = make_event()
    post(api, ev)
    before = stored(db, ev["encounter_id"])

    r = post(api, {**ev, "event_id": new_id("evt")})
    assert r.status_code == 200
    assert r.json()["outcome"] == "duplicate"
    assert stored(db, ev["encounter_id"]) == before


def test_same_version_unseen_event_id_is_duplicate_not_accept(api, db):
    # Section 1 boundary case: version == current_version with an unseen event_id
    ev = make_event(version=5)
    post(api, ev)
    r = post(api, {**ev, "event_id": new_id("evt")})
    assert (r.status_code, r.json()["outcome"]) == (200, "duplicate")
    assert len(stored(db, ev["encounter_id"])[2]) == 1


# ---------------------------------------------------------------- payload conflict

def test_payload_conflict_same_event_id_changed_transcript(api, db):
    ev = make_event()
    post(api, ev)
    before = stored(db, ev["encounter_id"])

    changed = {**ev, "payload": {"transcription": "something else entirely"}}
    r = post(api, changed)
    assert r.status_code == 409
    assert r.json() == {
        "outcome": "payload_conflict",
        "encounter_id": ev["encounter_id"],
        "version": 12,
        "current_version": 12,
        "message": "A different payload is already recorded for this version.",
    }
    assert "something else" not in r.text
    assert stored(db, ev["encounter_id"]) == before  # first recorded payload wins


def test_payload_conflict_fresh_event_id_for_existing_version(api, db):
    ev = make_event(version=12)
    post(api, ev)
    post(api, make_event(encounter_id=ev["encounter_id"], version=13))

    r = post(api, {**ev, "event_id": new_id("evt"), "payload": {"transcription": "different"}})
    assert r.status_code == 409
    assert r.json()["outcome"] == "payload_conflict"
    assert r.json()["current_version"] == 13


def test_event_id_reused_on_different_existing_encounter(api, db):
    # DECISIONS.md D7: same event_id and same transcript, but another encounter: not a duplicate
    a = make_event(version=12)
    post(api, a)
    b = make_event(version=5, patient_id="pat-5")
    post(api, b)
    before = stored(db, b["encounter_id"])

    r = post(api, {**b, "event_id": a["event_id"], "version": 6, "payload": a["payload"]})
    assert r.status_code == 409
    assert r.json() == {
        "outcome": "payload_conflict",
        "encounter_id": b["encounter_id"],
        "version": 6,
        "current_version": 5,
        "message": "A different payload is already recorded for this version.",
    }
    assert stored(db, b["encounter_id"]) == before


def test_event_id_reused_on_brand_new_encounter(api, db):
    # DECISIONS.md D7: current_version 0 means "no version stored"; the shell row rolls back
    a = make_event(version=12)
    post(api, a)
    fresh = new_id("enc")

    r = post(api, {**a, "encounter_id": fresh})
    assert r.status_code == 409
    assert r.json() == {
        "outcome": "payload_conflict",
        "encounter_id": fresh,
        "version": 12,
        "current_version": 0,
        "message": "A different payload is already recorded for this version.",
    }
    assert stored(db, fresh) == (None, [], [])
    assert no_uncommitted_shells(db)


# ---------------------------------------------------------------- stale

def test_stale(api, db):
    # Scenario 6.2: v11 arrives after v12
    v12 = make_event(version=12)
    post(api, v12)
    before = stored(db, v12["encounter_id"])
    v11 = make_event(encounter_id=v12["encounter_id"], version=11, transcription="old")

    for _ in range(2):  # a retry of a stale event is stale again: it was never recorded
        r = post(api, v11)
        assert r.status_code == 200
        assert r.json() == {
            "outcome": "stale",
            "encounter_id": v12["encounter_id"],
            "version": 11,
            "current_version": 12,
        }
        assert stored(db, v12["encounter_id"]) == before


# ---------------------------------------------------------------- identity conflict

def test_identity_conflict_patient(api, db):
    ev = make_event(patient_id="pat-77")
    post(api, ev)
    before = stored(db, ev["encounter_id"])

    wrong = make_event(encounter_id=ev["encounter_id"], version=13, patient_id="pat-99")
    for _ in range(2):  # the same 409 on every retry
        r = post(api, wrong)
        assert r.status_code == 409
        assert r.json() == {
            "outcome": "identity_conflict",
            "encounter_id": ev["encounter_id"],
            "current_version": 12,
            "conflicting_field": "patient_id",
            "message": "Contradicts stored encounter identity. Retrying will not succeed.",
        }
        assert "pat-77" not in r.text and "pat-99" not in r.text
        assert stored(db, ev["encounter_id"]) == before


def test_identity_conflict_encounter_type(api, db):
    ev = make_event(encounter_type="TelephoneTriage")
    post(api, ev)
    r = post(api, make_event(encounter_id=ev["encounter_id"], version=13, encounter_type="Appointment"))
    assert r.status_code == 409
    assert r.json()["conflicting_field"] == "encounter_type"
    assert stored(db, ev["encounter_id"])[0][2] == 12


def test_identity_checked_before_duplicate(api, db):
    # Section 1 precedence: identity first, then event/version uniqueness
    ev = make_event()
    post(api, ev)
    r = post(api, {**ev, "patient_id": "pat-99"})
    assert (r.status_code, r.json()["outcome"]) == (409, "identity_conflict")


# ---------------------------------------------------------------- 400 and 413

@pytest.mark.parametrize("mutate", [
    lambda e: e.pop("event_id"),
    lambda e: e.pop("encounter_id"),
    lambda e: e.pop("patient_id"),
    lambda e: e.pop("encounter_type"),
    lambda e: e.pop("version"),
    lambda e: e.pop("payload"),
    lambda e: e["payload"].pop("transcription"),
    lambda e: e.update(version=0),
    lambda e: e.update(version=-3),
    lambda e: e.update(version="12"),
    lambda e: e.update(version=12.5),
    lambda e: e.update(version=True),
    lambda e: e.update(version=2**31),
    lambda e: e.update(encounter_type="Surgery"),
    lambda e: e.update(event_id=""),
    lambda e: e.update(patient_id="p" * 256),
    lambda e: e.update(encounter_id=42),
    lambda e: e.update(payload={"transcription": None}),
    lambda e: e.update(payload="text"),
    lambda e: e.update(payload={"transcription": "bad \x00 byte"}),
])
def test_malformed_request_is_400_and_stores_nothing(api, db, mutate):
    ev = make_event()
    mutate(ev)
    r = post(api, ev)
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "invalid_request"
    assert TRANSCRIPT not in r.text
    assert db.execute("SELECT count(*) FROM encounters").fetchone()[0] == 0


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b"", b"\xff\xfe"])
def test_unparseable_body_is_400(api, raw):
    r = api.post("/encounters/events", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert r.json() == {"error": "invalid_request", "detail": "body must be a JSON object"}


def _body_of_size(size: int) -> bytes:
    import json
    ev = make_event(transcription="")
    base = len(json.dumps(ev).encode())
    ev["payload"]["transcription"] = "x" * (size - base)
    raw = json.dumps(ev).encode()
    assert len(raw) == size
    return raw


def test_body_at_limit_is_accepted(api):
    r = api.post("/encounters/events", content=_body_of_size(TEST_MAX_BODY_BYTES))
    assert r.status_code == 201


def test_body_over_limit_is_413(api, db):
    r = api.post("/encounters/events", content=_body_of_size(TEST_MAX_BODY_BYTES + 1))
    assert r.status_code == 413
    assert r.json() == {"error": "payload_too_large", "max_bytes": TEST_MAX_BODY_BYTES}
    assert db.execute("SELECT count(*) FROM encounters").fetchone()[0] == 0


def test_size_is_checked_before_parsing(api):
    r = api.post("/encounters/events", content=b"{" * (TEST_MAX_BODY_BYTES + 1))
    assert r.status_code == 413


def test_oversized_chunked_body_without_content_length_is_413(api):
    def chunks():
        for _ in range(TEST_MAX_BODY_BYTES // 1024 + 2):
            yield b"x" * 1024
    r = api.post("/encounters/events", content=chunks())
    assert "content-length" not in r.request.headers
    assert r.status_code == 413


# ---------------------------------------------------------------- one transaction; test hooks

def test_failure_before_commit_rolls_back_everything(app_config, db):
    # Section 3: a failure after the job insert leaves nothing, not even the shell row
    app = create_app(app_config)

    def boom(**_):
        raise RuntimeError("injected")

    app.state.hooks.register("ingest.before_commit", boom)
    ev = make_event(version=1)
    with TestClient(app, raise_server_exceptions=False) as client:
        r = post(client, ev)
        assert r.status_code == 500
        assert r.json() == {"error": "internal"}
        assert stored(db, ev["encounter_id"]) == (None, [], [])

        app.state.hooks.clear()
        assert post(client, ev).status_code == 201  # the partner's retry is accepted fresh
    assert len(stored(db, ev["encounter_id"])[2]) == 1


def test_hooks_are_inert_unless_enabled(app_config, db):
    from dataclasses import replace
    app = create_app(replace(app_config, test_hooks=False, crash_at="ingest.after_lock"))
    called = []
    app.state.hooks.register("ingest.after_lock", lambda **_: called.append(1))
    with TestClient(app) as client:
        assert post(client, make_event()).status_code == 201
    assert called == []


def test_hooks_fire_at_named_points(api, db):
    seen = []
    for point in ("ingest.after_lock", "ingest.before_commit", "ingest.after_commit"):
        api.app.state.hooks.register(point, lambda point=point, **_: seen.append(point))
    post(api, make_event())
    assert seen == ["ingest.after_lock", "ingest.before_commit", "ingest.after_commit"]
