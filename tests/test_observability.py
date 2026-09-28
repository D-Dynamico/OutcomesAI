# Design section 7 (observability) and section 5 (SLA detection): metrics values, the
# database gauges computed on scrape, and structured logs that never carry patient content
# (design section 7, "No patient content anywhere in the telemetry").
import io
import json
import logging

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families
from psycopg_pool import ConnectionPool

from app.api.main import create_app
from app.logs import JsonFormatter
from app.metrics import WORKER_REGISTRY, DatabaseCollector
from app.summary.client import TransientError
from tests.factories import make_event, new_id, post
from tests.harness import fetch_job


def scrape(client) -> dict:
    r = client.get("/metrics")
    assert r.status_code == 200
    samples = {}
    for family in text_string_to_metric_families(r.text):
        for s in family.samples:
            samples[(s.name, tuple(sorted(s.labels.items())))] = s.value
    return samples


def value(samples, name, **labels):
    return samples.get((name, tuple(sorted(labels.items()))), 0.0)


def worker_value(name, **labels):
    return WORKER_REGISTRY.get_sample_value(name, labels) or 0.0


# ---------------------------------------------------------------- ingestion counters (API)

def test_ingestion_outcome_counters(api):
    before = scrape(api)
    ev = make_event()
    post(api, ev)                                                            # accepted
    post(api, ev)                                                            # duplicate
    post(api, {**ev, "event_id": new_id("evt")})                             # duplicate, source defect
    post(api, make_event(encounter_id=ev["encounter_id"], version=11))       # stale
    post(api, make_event(encounter_id=ev["encounter_id"], version=13, patient_id="pat-99"))  # identity
    post(api, {**ev, "payload": {"transcription": "changed"}})               # payload conflict
    post(api, {**ev, "version": 0})                                          # 400
    api.post("/encounters/events", content=b"x" * 10_000)                    # 413
    after = scrape(api)

    def delta(name, **labels):
        return value(after, name, **labels) - value(before, name, **labels)

    assert {o: delta("ingest_events_total", outcome=o) for o in (
        "accepted", "duplicate", "stale", "identity_conflict", "payload_conflict",
        "invalid_request", "payload_too_large")} == {
        "accepted": 1, "duplicate": 2, "stale": 1, "identity_conflict": 1, "payload_conflict": 1,
        "invalid_request": 1, "payload_too_large": 1}
    assert delta("ingest_duplicate_source_defects_total") == 1
    assert delta("ingest_identity_conflicts_total") == 1       # the dedicated metric (section 6.8)
    assert delta("ingest_payload_conflicts_total") == 1


# ---------------------------------------------------------------- database gauges, on scrape

def test_queue_sla_status_and_breaker_gauges(api, db):
    def job(sql):
        ev = make_event()
        post(api, ev)
        job_id = fetch_job(db, ev["encounter_id"], 12)["job_id"]
        if sql:
            db.execute(f"UPDATE summary_jobs SET {sql} WHERE job_id = %s", (job_id,))

    job(None)                                                               # queued, due, fresh
    job("next_attempt_at = now() + interval '1 minute'")                    # waiting on backoff
    job("accepted_at = now() - interval '45 seconds'")                      # queued, breaching
    job("status = 'processing', attempts = 1, lease_expires_at = now() + interval '1 minute', "
        "accepted_at = now() - interval '20 seconds'")                      # processing, breaching
    job("status = 'failed', completed_at = now(), accepted_at = now() - interval '1 hour'")
    job("status = 'ready', summary = 's', completed_at = now(), accepted_at = now() - interval '1 hour'")
    db.execute("UPDATE circuit_breaker SET state = 'open', open_until = now() + interval '30 seconds', "
               "opened_at = now() - interval '90 seconds', probes_sent = 3")

    m = scrape(api)
    assert value(m, "summary_db_up") == 1
    assert [value(m, "summary_queue_depth", state=s) for s in ("due_now", "waiting_backoff", "processing")] == \
        [2, 1, 1]
    assert value(m, "summary_sla_breaching_jobs") == 2
    assert value(m, "summary_sla_breaching_queued") == 1
    assert value(m, "summary_sla_breaching_processing") == 1
    assert value(m, "summary_sla_oldest_unfinished_age_seconds") == pytest.approx(45, abs=3)
    assert {s: value(m, "summary_jobs", status=s) for s in ("queued", "processing", "ready", "failed")} == \
        {"queued": 3, "processing": 1, "ready": 1, "failed": 1}
    assert [value(m, "breaker_state", state=s) for s in ("closed", "open", "half_open")] == [0, 1, 0]
    assert value(m, "breaker_open_seconds") == pytest.approx(90, abs=3)
    assert value(m, "breaker_probes_sent_total") == 3


def test_sla_gauges_are_zero_when_nothing_breaches(api):
    post(api, make_event())
    m = scrape(api)
    assert value(m, "summary_sla_breaching_jobs") == 0
    assert value(m, "summary_sla_oldest_unfinished_age_seconds") == 0


def test_database_outage_does_not_break_the_scrape(fresh_db):
    closed_pool = ConnectionPool(fresh_db, open=False)
    families = {f.name: f for f in DatabaseCollector(closed_pool, 10).collect()}
    assert list(families) == ["summary_db_up"]
    assert families["summary_db_up"].samples[0].value == 0


def test_metrics_carry_no_patient_content(api, mock, make_worker):
    post(api, make_event(patient_id="pat-SECRET", transcription="SECRET transcript text"))
    mock.push("SECRET summary text")
    make_worker().run_once()
    text = api.get("/metrics").text
    assert "SECRET" not in text and "pat-" not in text


# ---------------------------------------------------------------- worker metrics

def test_worker_metrics(api, db, mock, make_worker):
    names = [("generate_summary_calls_total", {"kind": "real", "result": "succeeded"}),
             ("generate_summary_calls_total", {"kind": "real", "result": "rate_limited"}),
             ("job_attempts_closed_total", {"outcome": "succeeded", "error_class": ""}),
             ("job_attempts_closed_total", {"outcome": "transient_error", "error_class": "rate_limited"}),
             ("job_attempts_closed_total", {"outcome": "skipped", "error_class": ""}),
             ("summary_completion_seconds_count", {}),
             ("generate_summary_call_seconds_count", {"kind": "real"})]
    before = {(n, tuple(l.items())): worker_value(n, **l) for n, l in names}

    ok = make_event()
    post(api, ok)
    flaky = make_event()
    post(api, flaky)
    old = make_event(version=12)
    post(api, old)
    post(api, make_event(encounter_id=old["encounter_id"], version=13))
    mock.push("s1", TransientError("rate_limited"))
    worker = make_worker()
    outcomes = [worker.run_once() for _ in range(4)]
    assert outcomes == ["ready", "transient_error", "skipped", "ready"]

    after = {(n, tuple(l.items())): worker_value(n, **l) for n, l in names}
    deltas = [after[k] - before[k] for k in before]
    assert deltas == [2, 1, 2, 1, 1, 2, 3]
    assert worker_value("worker_busy_slots") == 0


# ---------------------------------------------------------------- structured logs

def test_json_formatter_keeps_fields_and_reduces_exceptions_to_their_type():
    record = logging.LogRecord("ingest", logging.WARNING, __file__, 1, "payload_conflict", None, None)
    record.event_id, record.version = "evt-1", 12
    line = json.loads(JsonFormatter("api").format(record))
    assert {k: line[k] for k in ("level", "service", "logger", "event", "event_id", "version")} == \
        {"level": "WARNING", "service": "api", "logger": "ingest", "event": "payload_conflict",
         "event_id": "evt-1", "version": 12}

    try:
        raise ValueError("Patient Jane Doe said something private")
    except ValueError:
        import sys
        record = logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1,
                                   "Exception in ASGI application", None, sys.exc_info())
    out = JsonFormatter("api").format(record)
    assert json.loads(out)["error_type"] == "ValueError"
    assert "Jane" not in out and "Traceback" not in out


@pytest.fixture
def captured_logs():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("test"))
    root = logging.getLogger()
    root.addHandler(handler)
    old_level = root.level
    root.setLevel(logging.INFO)
    yield stream
    root.removeHandler(handler)
    root.setLevel(old_level)


def test_logs_never_carry_patient_content(app_config, db, mock, make_worker, captured_logs):
    transcript = "SECRET-TRANSCRIPT Nurse: how are you? Patient: fine"
    app = create_app(app_config)

    def boom(event, **_):
        raise RuntimeError(event.transcription)   # an exception whose message is patient content

    with TestClient(app, raise_server_exceptions=False) as client:
        ev = make_event(patient_id="pat-SECRET-1", transcription=transcript)
        post(client, ev)                                                    # accepted
        post(client, ev)                                                    # duplicate
        post(client, {**ev, "event_id": new_id("evt")})                     # source defect
        post(client, {**ev, "payload": {"transcription": "SECRET-OTHER"}})  # payload conflict
        post(client, make_event(encounter_id=ev["encounter_id"], version=13,
                                patient_id="pat-SECRET-2", transcription=transcript))  # identity
        post(client, make_event(encounter_id=ev["encounter_id"], version=11,
                                patient_id="pat-SECRET-1", transcription=transcript))  # stale
        post(client, {**ev, "version": "SECRET-TRANSCRIPT"})                # 400
        app.state.hooks.register("ingest.before_commit", boom)
        assert post(client, make_event(patient_id="pat-SECRET-1",
                                       transcription=transcript)).status_code == 500
        app.state.hooks.clear()

        mock.push(TransientError("ai_unavailable"), "SECRET-SUMMARY")
        worker = make_worker()
        worker.run_once()
        db.execute("UPDATE summary_jobs SET next_attempt_at = now()")
        worker.run_once()
        assert client.get(f"/encounters/{ev['encounter_id']}/summary").json()["summary"] == "SECRET-SUMMARY"

    lines = [json.loads(l) for l in captured_logs.getvalue().splitlines()]
    events = {l["event"] for l in lines}
    assert {"accepted", "duplicate", "duplicate_source_defect", "payload_conflict", "identity_conflict",
            "stale", "unhandled_error", "transient_error", "result_written"} <= events
    for line in lines:
        text = json.dumps(line)
        assert "SECRET-TRANSCRIPT" not in text and "SECRET-SUMMARY" not in text and "SECRET-OTHER" not in text
        if line["event"] != "identity_conflict":
            assert "pat-" not in text, line
    identity = next(l for l in lines if l["event"] == "identity_conflict")
    assert (identity["stored_patient_id"], identity["incoming_patient_id"]) == ("pat-SECRET-1", "pat-SECRET-2")
    assert next(l for l in lines if l["event"] == "unhandled_error")["error_type"] == "RuntimeError"
