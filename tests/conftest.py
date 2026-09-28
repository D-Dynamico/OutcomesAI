# Tests run against a real Postgres (the Compose `db` service), in a dedicated database so
# they never touch the running stack's data. Never SQLite, never a fake (CLAUDE.md).
import os

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql

from app.api.main import create_app
from app.config import Config
from app.db import apply_schema, create_pool
from app.hooks import Hooks
from app.summary.mock import ScriptedSummaryClient
from app.worker.main import Worker

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


@pytest.fixture
def db(fresh_db):
    """Autocommit connection for arranging and asserting on stored state."""
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        yield conn


TEST_MAX_BODY_BYTES = 8192


@pytest.fixture
def app_config(fresh_db) -> Config:
    return Config(database_url=fresh_db, db_pool_size=4, test_hooks=True,
                  max_body_bytes=TEST_MAX_BODY_BYTES)


@pytest.fixture
def api(app_config):
    with TestClient(create_app(app_config)) as client:
        yield client


@pytest.fixture
def worker_config(fresh_db) -> Config:
    # Shrunk durations; the lease > AI timeout rule still holds
    return Config(database_url=fresh_db, db_pool_size=10, test_hooks=True,
                  lease_seconds=2, ai_timeout_seconds=1, probe_deadline_seconds=2,
                  worker_poll_seconds=0.05)


@pytest.fixture
def pool(fresh_db):
    p = create_pool(fresh_db, 10)
    yield p
    p.close()


@pytest.fixture
def mock():
    return ScriptedSummaryClient()


@pytest.fixture
def make_worker(pool, mock, worker_config):
    def make(worker_id="w-A", client=None, hooks=None, rng=None, config=None):
        return Worker(pool, client or mock, config or worker_config, worker_id,
                      hooks or Hooks(enabled=True), rng=rng)
    return make
