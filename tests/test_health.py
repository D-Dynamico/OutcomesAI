import psycopg
from fastapi.testclient import TestClient

from app.api.main import create_app
from app.config import Config
from app.mockai.main import create_app as create_mock_app


def test_healthz_applies_schema_and_reports_ok(empty_db):
    # Startup applies the schema on an empty database; no manual step
    with TestClient(create_app(Config(database_url=empty_db, db_pool_size=2))) as client:
        r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_healthz_reports_missing_schema(fresh_db):
    with TestClient(create_app(Config(database_url=fresh_db, db_pool_size=2))) as client:
        with psycopg.connect(fresh_db, autocommit=True) as conn:
            conn.execute("DROP TABLE circuit_breaker")
        r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable"}


def test_mock_ai_healthz():
    with TestClient(create_mock_app()) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
