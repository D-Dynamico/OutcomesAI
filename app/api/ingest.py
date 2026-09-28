# Design section 3: ingestion logic. Outcomes from section 1, response bodies from section 4.
#
# Everything from the encounter upsert to the job insert is one transaction. Every outcome
# other than "accepted" rolls back, so a shell row at version 0 is never committed.
import hashlib
import json
import logging
from dataclasses import dataclass, field
from urllib.parse import quote

from psycopg_pool import ConnectionPool

from app.hooks import Hooks

log = logging.getLogger("ingest")

ENCOUNTER_TYPES = ("Appointment", "MedicationRefill", "TelephoneTriage")
MAX_ID_LENGTH = 255          # DECISIONS.md D8
PG_INTEGER_MAX = 2**31 - 1   # encounter_events.version is INTEGER


class InvalidRequest(ValueError):
    """400. `detail` names the rule broken, never the offending value."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Event:
    event_id: str
    encounter_id: str
    patient_id: str
    encounter_type: str
    version: int
    transcription: str

    @property
    def payload_hash(self) -> bytes:
        # Section 1: hash the transcription string, not the raw JSON body
        return hashlib.sha256(self.transcription.encode("utf-8")).digest()


@dataclass
class Response:
    status_code: int
    body: dict
    headers: dict = field(default_factory=dict)


# ---------------------------------------------------------------- validation (section 3, "Before the transaction")

def _string(value, name: str, *, max_length: int | None = None, allow_empty: bool = False) -> str:
    rule = f"{name} must be a non-empty string of at most {max_length} characters" if max_length \
        else f"{name} must be a string"
    if not isinstance(value, str):
        raise InvalidRequest(rule)
    if (not allow_empty and value == "") or (max_length and len(value) > max_length):
        raise InvalidRequest(rule)
    # Postgres TEXT rejects NUL, and lone surrogates cannot be encoded: both would otherwise be a 500
    if "\x00" in value:
        raise InvalidRequest(f"{name} must not contain NUL characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidRequest(f"{name} must be valid UTF-8") from None
    return value


def parse_event(body: bytes) -> Event:
    try:
        data = json.loads(body)
    except ValueError:
        raise InvalidRequest("body must be a JSON object") from None
    if not isinstance(data, dict):
        raise InvalidRequest("body must be a JSON object")

    ids = {name: _string(data.get(name), name, max_length=MAX_ID_LENGTH)
           for name in ("event_id", "encounter_id", "patient_id")}

    encounter_type = data.get("encounter_type")
    if not isinstance(encounter_type, str) or encounter_type not in ENCOUNTER_TYPES:
        raise InvalidRequest("encounter_type must be one of " + ", ".join(ENCOUNTER_TYPES))

    version = data.get("version")
    # bool is a subclass of int; 12.0 and "12" are rejected too
    if type(version) is not int or not 1 <= version <= PG_INTEGER_MAX:
        raise InvalidRequest("version must be a positive integer")

    payload = data.get("payload")
    if not isinstance(payload, dict):
        raise InvalidRequest("payload must be an object")
    transcription = _string(payload.get("transcription"), "payload.transcription", allow_empty=True)

    return Event(encounter_type=encounter_type, version=version, transcription=transcription, **ids)


# ---------------------------------------------------------------- the transaction (section 3, "Order of operations")

UPSERT_SHELL = """
INSERT INTO encounters (encounter_id, patient_id, encounter_type, current_version, transcription)
VALUES (%(encounter_id)s, %(patient_id)s, %(encounter_type)s, 0, NULL)
ON CONFLICT (encounter_id) DO NOTHING
"""

LOCK_ENCOUNTER = """
SELECT patient_id, encounter_type, current_version
  FROM encounters
 WHERE encounter_id = %(encounter_id)s
   FOR UPDATE
"""

INSERT_EVENT = """
INSERT INTO encounter_events (event_id, encounter_id, version, payload_hash)
VALUES (%(event_id)s, %(encounter_id)s, %(version)s, %(payload_hash)s)
ON CONFLICT DO NOTHING
RETURNING event_id
"""

# DECISIONS.md D7: encounter_id and version are read alongside the hash, so an event_id reused
# on another encounter or version is a conflict rather than a duplicate
MATCHING_EVENTS = """
SELECT event_id, encounter_id, version, payload_hash
  FROM encounter_events
 WHERE event_id = %(event_id)s
    OR (encounter_id = %(encounter_id)s AND version = %(version)s)
"""

ADVANCE_VERSION = """
UPDATE encounters
   SET current_version = %(version)s,
       transcription   = %(transcription)s,
       updated_at      = now()
 WHERE encounter_id    = %(encounter_id)s
   AND current_version < %(version)s
RETURNING current_version
"""

INSERT_JOB = """
INSERT INTO summary_jobs (encounter_id, version, event_id, input_transcription)
VALUES (%(encounter_id)s, %(version)s, %(event_id)s, %(transcription)s)
"""


def ingest(pool: ConnectionPool, event: Event, hooks: Hooks) -> Response:
    params = {
        "event_id": event.event_id,
        "encounter_id": event.encounter_id,
        "patient_id": event.patient_id,
        "encounter_type": event.encounter_type,
        "version": event.version,
        "transcription": event.transcription,
        "payload_hash": event.payload_hash,
    }
    with pool.connection() as conn:
        try:
            # Step 1: upsert the encounter (shell row at version 0 if new), then lock it and read back identity
            conn.execute(UPSERT_SHELL, params)
            stored_patient, stored_type, current_version = conn.execute(LOCK_ENCOUNTER, params).fetchone()
            hooks.fire("ingest.after_lock", event=event)

            # Step 2: compare identity against what is actually stored (first patient wins)
            conflicting_field = (
                "patient_id" if stored_patient != event.patient_id
                else "encounter_type" if stored_type != event.encounter_type
                else None
            )
            if conflicting_field:
                conn.rollback()
                # The only log line that carries patient_id (section 7, "No patient content")
                log.warning("identity_conflict", extra={
                    "event_id": event.event_id, "encounter_id": event.encounter_id,
                    "stored_patient_id": stored_patient, "incoming_patient_id": event.patient_id,
                    "stored_encounter_type": stored_type, "incoming_encounter_type": event.encounter_type,
                })
                return _identity_conflict(event, current_version, conflicting_field)

            # Step 3: insert the event; a PK or unique hit is a duplicate or a payload conflict
            if conn.execute(INSERT_EVENT, params).fetchone() is None:
                matches = conn.execute(MATCHING_EVENTS, params).fetchall()
                conn.rollback()
                return _classify_seen_event(event, current_version, matches)

            # Step 4: conditional version update (compare-and-swap); zero rows means stale
            if conn.execute(ADVANCE_VERSION, params).fetchone() is None:
                conn.rollback()
                log.info("stale", extra={"event_id": event.event_id, "encounter_id": event.encounter_id,
                                         "version": event.version, "current_version": current_version})
                return _plain_outcome("stale", event, current_version)

            # Step 5: create the job with its immutable input snapshot; the commit is the hand-off
            conn.execute(INSERT_JOB, params)
            hooks.fire("ingest.before_commit", event=event)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    hooks.fire("ingest.after_commit", event=event)
    log.info("accepted", extra={"event_id": event.event_id, "encounter_id": event.encounter_id,
                                "version": event.version})
    return Response(
        201,
        {
            "outcome": "accepted",
            "encounter_id": event.encounter_id,
            "version": event.version,
            "current_version": event.version,
            "job_status": "queued",
        },
        {"Location": f"/encounters/{quote(event.encounter_id, safe='')}/summary"},
    )


def _classify_seen_event(event: Event, current_version: int, matches: list[tuple]) -> Response:
    if not matches:
        # The conflicting row committed before our re-read, so it must be visible; anything
        # else is a bug. 500 makes the partner retry, which is safe.
        raise RuntimeError("event insert conflicted but no matching event was found")

    expected = (event.encounter_id, event.version, event.payload_hash)
    ids = {"event_id": event.event_id, "encounter_id": event.encounter_id, "version": event.version}

    if all((enc, ver, h) == expected for _, enc, ver, h in matches):
        if any(eid != event.event_id for eid, *_ in matches):
            # Section 1: same (encounter_id, version) under a fresh event_id is a partner defect
            log.warning("duplicate_source_defect", extra={
                **ids, "recorded_event_ids": [eid for eid, *_ in matches]})
        else:
            log.info("duplicate", extra=ids)
        return _plain_outcome("duplicate", event, current_version)

    log.warning("payload_conflict", extra={
        **ids,
        "incoming_hash": event.payload_hash.hex(),
        "recorded": [{"event_id": eid, "encounter_id": enc, "version": ver, "payload_hash": bytes(h).hex()}
                     for eid, enc, ver, h in matches],
    })
    return Response(409, {
        "outcome": "payload_conflict",
        "encounter_id": event.encounter_id,
        "version": event.version,
        "current_version": current_version,
        "message": "A different payload is already recorded for this version.",
    })


def _plain_outcome(outcome: str, event: Event, current_version: int) -> Response:
    return Response(200, {
        "outcome": outcome,
        "encounter_id": event.encounter_id,
        "version": event.version,
        "current_version": current_version,
    })


def _identity_conflict(event: Event, current_version: int, conflicting_field: str) -> Response:
    # Section 4 privacy note: the stored patient_id is never echoed
    return Response(409, {
        "outcome": "identity_conflict",
        "encounter_id": event.encounter_id,
        "current_version": current_version,
        "conflicting_field": conflicting_field,
        "message": "Contradicts stored encounter identity. Retrying will not succeed.",
    })
