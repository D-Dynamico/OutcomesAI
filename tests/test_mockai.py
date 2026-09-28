# The mock-ai service (DECISIONS.md D3): latency, failures, outage switch, non-idempotent
# output not derived from content, and separate real/probe call counts.
import random

import pytest
from fastapi.testclient import TestClient

from app.mockai.main import MockSettings, create_app
from app.summary.client import PROBE_TRANSCRIPT

SECRET = "Patient Zanzibar reports chest pain"


@pytest.fixture
def slept():
    return []


@pytest.fixture
def mock_ai(slept):
    async def fake_sleep(seconds):
        slept.append(seconds)

    settings = MockSettings(failure_rate=0, slow_rate=0)
    with TestClient(create_app(settings, random.Random(7), sleep=fake_sleep)) as client:
        yield client


def call(client, transcription=SECRET):
    return client.post("/generate_summary", json={"transcription": transcription})


def test_success_varies_wording_and_never_echoes_content(mock_ai):
    first, second = call(mock_ai), call(mock_ai)
    assert first.status_code == second.status_code == 200
    assert first.json()["summary"] != second.json()["summary"]
    for word in ("Zanzibar", "chest", "pain"):
        assert word not in first.text


def test_latency_is_within_configured_range(mock_ai, slept):
    call(mock_ai)
    assert 2.0 <= slept[-1] <= 8.0
    mock_ai.post("/admin/settings", json={"slow_rate": 1, "slow_seconds": 12})
    call(mock_ai)
    assert slept[-1] == 12


def test_counts_real_and_probe_calls(mock_ai):
    call(mock_ai)
    call(mock_ai)
    call(mock_ai, PROBE_TRANSCRIPT)
    stats = mock_ai.get("/admin/stats").json()
    assert (stats["calls_total"], stats["real"], stats["probe"]) == (3, 2, 1)
    assert stats["results"] == {"succeeded": 3}
    mock_ai.post("/admin/reset")
    assert mock_ai.get("/admin/stats").json()["calls_total"] == 0


def test_outage_fails_everything_until_switched_off(mock_ai):
    mock_ai.post("/admin/settings", json={"outage": True})
    assert [call(mock_ai).status_code for _ in range(5)] == [503] * 5
    assert call(mock_ai, PROBE_TRANSCRIPT).status_code == 503
    mock_ai.post("/admin/settings", json={"outage": False})
    assert call(mock_ai).status_code == 200
    stats = mock_ai.get("/admin/stats").json()
    assert (stats["real"], stats["probe"], stats["results"]["ai_unavailable"]) == (6, 1, 6)


def test_failures_use_the_three_classes(mock_ai, slept):
    mock_ai.post("/admin/settings", json={"failure_rate": 1, "timeout_hang_seconds": 90})
    statuses = {call(mock_ai).status_code for _ in range(60)}
    assert statuses == {429, 503, 504}
    assert 90 in slept   # a timeout hangs past the worker's client timeout


def test_invalid_body_is_400(mock_ai):
    assert mock_ai.post("/generate_summary", content=b"nope").status_code == 400
    assert mock_ai.post("/generate_summary", json={"transcription": 5}).status_code == 400


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("MOCK_OUTAGE", "true")
    monkeypatch.setenv("MOCK_LATENCY_MAX_SECONDS", "0.2")
    s = MockSettings.from_env()
    assert s.outage is True and s.latency_max_seconds == 0.2
