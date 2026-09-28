# End-to-end smoke test (DECISIONS.md D3): a real worker process, over HTTP, against the
# mock-ai container. `make test` starts mock-ai before running the suite.
#
# mock-ai is shared with the dev stack, so call counts are compared as before/after deltas;
# avoid running this while the dev stack's workers are busy.
import os
import signal
import subprocess
import sys
import time

import httpx
import pytest

from tests.factories import make_event, post

MOCK_AI_URL = os.environ.get("MOCK_AI_URL", "http://mock-ai:8001")


@pytest.fixture
def mock_ai():
    try:
        client = httpx.Client(base_url=MOCK_AI_URL, timeout=5)
        original = client.get("/admin/settings").json()
    except httpx.TransportError:
        pytest.fail(f"mock-ai is not reachable at {MOCK_AI_URL}; run the suite with `make test`")
    client.post("/admin/settings", json={"latency_min_seconds": 0.05, "latency_max_seconds": 0.2,
                                         "slow_rate": 0, "failure_rate": 0, "outage": False})
    yield client
    client.post("/admin/settings", json=original)
    client.close()


def test_real_worker_process_against_mock_ai(api, db, fresh_db, mock_ai):
    before = mock_ai.get("/admin/stats").json()
    encounters = []
    for i in range(3):
        ev = make_event(version=1, transcription=f"Nurse: smoke test {i}. Patient: fine.")
        assert post(api, ev).status_code == 201
        encounters.append(ev["encounter_id"])

    env = {**os.environ, "DATABASE_URL": fresh_db, "MOCK_AI_URL": MOCK_AI_URL,
           "WORKER_CONCURRENCY": "2", "DB_POOL_SIZE": "3", "WORKER_POLL_SECONDS": "0.1"}
    worker = subprocess.Popen([sys.executable, "-m", "app.worker.main"], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 30
        statuses = []
        while time.monotonic() < deadline:
            statuses = [api.get(f"/encounters/{e}/summary").json()["status"] for e in encounters]
            if statuses == ["ready"] * 3:
                break
            time.sleep(0.2)
        assert statuses == ["ready"] * 3
    finally:
        worker.send_signal(signal.SIGTERM)
        assert worker.wait(timeout=15) == 0   # graceful shutdown

    after = mock_ai.get("/admin/stats").json()
    assert after["real"] - before["real"] == 3
    assert after["probe"] - before["probe"] == 0
    summaries = [api.get(f"/encounters/{e}/summary").json()["summary"] for e in encounters]
    assert all(summaries) and len(set(summaries)) == 3
