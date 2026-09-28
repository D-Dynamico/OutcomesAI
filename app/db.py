# Connection pool, transaction helper, and startup schema application.
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg_pool import ConnectionPool

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Arbitrary constant; serialises schema application across api and worker replicas starting together
SCHEMA_LOCK_KEY = 7_420_001


def create_pool(database_url: str, size: int) -> ConnectionPool:
    pool = ConnectionPool(database_url, min_size=1, max_size=size, open=False)
    pool.open(wait=True, timeout=30)
    return pool


@contextmanager
def transaction(pool: ConnectionPool) -> Iterator[psycopg.Connection]:
    """One transaction on a pooled connection: commit on success, roll back on any exception."""
    with pool.connection() as conn:
        with conn.transaction():
            yield conn


def apply_schema(conn: psycopg.Connection) -> bool:
    """Apply schema.sql once. Returns True if this call created the schema.

    The DDL is the design's text verbatim (no IF NOT EXISTS), so idempotence comes from
    running it only when absent. All of it runs in one transaction under an advisory lock:
    concurrent callers wait, then see the schema already there. See DECISIONS.md D11.
    """
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEMA_LOCK_KEY,))
        exists = conn.execute("SELECT to_regclass('public.encounters') IS NOT NULL").fetchone()[0]
        if not exists:
            conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
        # Seed the single breaker row (not in the design's DDL; DECISIONS.md D12)
        conn.execute(
            "INSERT INTO circuit_breaker (name) VALUES ('generate_summary') ON CONFLICT DO NOTHING"
        )
    return not exists


def apply_schema_url(database_url: str) -> bool:
    with psycopg.connect(database_url) as conn:
        return apply_schema(conn)
