# Tests run against a real Postgres (the Compose `db` service), in a dedicated database so
# they never touch the running stack's data. Never SQLite, never a fake (CLAUDE.md).
import os

import psycopg
import pytest
from psycopg import sql

from app.db import apply_schema

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://outcomes:outcomes@db:5432/outcomes_test"
)


def _admin_url(url: str) -> str:
    # Same server, the Compose default database, for CREATE DATABASE
    return url.rsplit("/", 1)[0] + "/outcomes"


@pytest.fixture(scope="session")
def db_url() -> str:
    dbname = TEST_DATABASE_URL.rsplit("/", 1)[1]
    with psycopg.connect(_admin_url(TEST_DATABASE_URL), autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (dbname,)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    return TEST_DATABASE_URL


def reset_schema(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


@pytest.fixture
def empty_db(db_url) -> str:
    """A test database with no schema at all."""
    reset_schema(db_url)
    return db_url


@pytest.fixture
def fresh_db(db_url) -> str:
    """A test database with the schema freshly applied and no rows."""
    reset_schema(db_url)
    with psycopg.connect(db_url) as conn:
        apply_schema(conn)
    return db_url
