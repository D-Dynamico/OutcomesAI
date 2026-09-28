import threading

import psycopg

from app.db import apply_schema

TABLES = {"encounters", "encounter_events", "summary_jobs", "job_attempts", "circuit_breaker"}
TYPES = {"encounter_type", "job_status", "attempt_outcome", "breaker_state"}
INDEXES = {"idx_jobs_due", "idx_jobs_expired_lease", "idx_attempts_finished",
           "uq_encounter_version", "uq_job_encounter_version"}


def _catalog(url):
    with psycopg.connect(url) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
        types = {r[0] for r in conn.execute(
            "SELECT typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
            "WHERE n.nspname = 'public' AND t.typtype = 'e'")}
        indexes = {r[0] for r in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")}
    return tables, types, indexes


def test_schema_applies_fully(empty_db):
    with psycopg.connect(empty_db) as conn:
        assert apply_schema(conn) is True
    tables, types, indexes = _catalog(empty_db)
    assert TABLES <= tables
    assert TYPES <= types
    assert INDEXES <= indexes


def test_schema_includes_md_only_parts(fresh_db):
    # docs/design.md wins over the PDF: payload_hash and the breaker table must be present
    with psycopg.connect(fresh_db) as conn:
        col = conn.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'encounter_events' AND column_name = 'payload_hash'").fetchone()
        assert col == ("bytea",)
        row = conn.execute(
            "SELECT state, probe_generation, probes_sent FROM circuit_breaker").fetchall()
        assert row == [("closed", 0, 0)]


def test_schema_apply_is_idempotent(fresh_db):
    with psycopg.connect(fresh_db) as conn:
        assert apply_schema(conn) is False
        assert apply_schema(conn) is False
        assert conn.execute("SELECT count(*) FROM circuit_breaker").fetchone()[0] == 1


def test_concurrent_apply_creates_schema_once(empty_db):
    # api and N worker replicas start together; exactly one creates, none error
    n = 8
    barrier = threading.Barrier(n)
    results, errors = [], []

    def run():
        try:
            with psycopg.connect(empty_db) as conn:
                barrier.wait()
                results.append(apply_schema(conn))
        except Exception as e:  # noqa: BLE001 - collected and asserted below
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sorted(results) == [False] * (n - 1) + [True]
    tables, _, _ = _catalog(empty_db)
    assert TABLES <= tables


def test_constraints_from_design_are_enforced(fresh_db):
    with psycopg.connect(fresh_db) as conn:
        conn.execute(
            "INSERT INTO encounters (encounter_id, patient_id, encounter_type, current_version, transcription) "
            "VALUES ('enc-1', 'pat-1', 'Appointment', 1, 't')")
        conn.execute(
            "INSERT INTO encounter_events (event_id, encounter_id, version, payload_hash) "
            "VALUES ('evt-1', 'enc-1', 1, '\\x00')")
        for stmt in (
            # uq_encounter_version: a second event_id for the same (encounter, version)
            "INSERT INTO encounter_events (event_id, encounter_id, version, payload_hash) "
            "VALUES ('evt-2', 'enc-1', 1, '\\x00')",
            # version > 0
            "INSERT INTO encounter_events (event_id, encounter_id, version, payload_hash) "
            "VALUES ('evt-3', 'enc-1', 0, '\\x00')",
            # encounter_type enum
            "INSERT INTO encounters (encounter_id, patient_id, encounter_type) "
            "VALUES ('enc-2', 'pat-1', 'Surgery')",
        ):
            try:
                with conn.transaction():
                    conn.execute(stmt)
            except psycopg.Error:
                continue
            raise AssertionError(f"constraint not enforced: {stmt}")
