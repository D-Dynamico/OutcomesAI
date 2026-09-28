# Early concurrency checks on the ingestion transaction (design section 3, scenarios 6.1 and 6.8).
# Two app instances with separate pools stand in for two service instances. The headline
# versions with a lock-hold hook are in the milestone 9 suite.
import threading
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from app.api.main import create_app
from tests.factories import make_event, new_id, post

ITERATIONS = 25


@pytest.fixture
def two_instances(app_config):
    with TestClient(create_app(app_config)) as a, TestClient(create_app(app_config)) as b:
        yield a, b


def race(clients, events):
    barrier = threading.Barrier(len(events))
    results = [None] * len(events)

    def send(i):
        barrier.wait()
        results[i] = post(clients[i], events[i])

    threads = [threading.Thread(target=send, args=(i,)) for i in range(len(events))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


@pytest.mark.parametrize("brand_new", [False, True])
def test_concurrent_duplicate_accepted_once(two_instances, db, brand_new):
    for _ in range(ITERATIONS):
        enc = new_id("enc")
        if not brand_new:
            post(two_instances[0], make_event(encounter_id=enc, version=10))
        ev = make_event(encounter_id=enc, version=12)

        results = race(two_instances, [ev, ev])

        assert sorted((r.status_code, r.json()["outcome"]) for r in results) == \
            [(200, "duplicate"), (201, "accepted")]
        assert all(r.json()["current_version"] == 12 for r in results)
        assert db.execute("SELECT count(*) FROM summary_jobs WHERE encounter_id = %s AND version = 12",
                          (enc,)).fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM encounters WHERE current_version = 0").fetchone()[0] == 0


def test_concurrent_versions_never_regress(two_instances, db):
    for i in range(ITERATIONS):
        enc = new_id("enc")
        post(two_instances[0], make_event(encounter_id=enc, version=10))
        v12 = make_event(encounter_id=enc, version=12, transcription="v12")
        v13 = make_event(encounter_id=enc, version=13, transcription="v13")
        events = [v12, v13] if i % 2 else [v13, v12]

        results = {r.json()["version"]: r for r in race(two_instances, events)}

        assert results[13].status_code == 201
        assert (results[12].status_code, results[12].json()["outcome"]) in \
            [(201, "accepted"), (200, "stale")]
        assert db.execute("SELECT current_version, transcription FROM encounters WHERE encounter_id = %s",
                          (enc,)).fetchone() == (13, "v13")


def test_identity_race_on_brand_new_encounter(two_instances, db):
    outcomes = Counter()
    for _ in range(ITERATIONS):
        enc = new_id("enc")
        a = make_event(encounter_id=enc, version=1, patient_id="pat-77")
        b = make_event(encounter_id=enc, version=2, patient_id="pat-99")

        results = race(two_instances, [a, b])

        codes = sorted(r.status_code for r in results)
        assert codes == [201, 409]
        loser = next(r for r in results if r.status_code == 409)
        assert loser.json()["conflicting_field"] == "patient_id"
        assert "pat-77" not in loser.text and "pat-99" not in loser.text
        winner = next(e for e, r in zip([a, b], results) if r.status_code == 201)
        outcomes[winner["patient_id"]] += 1
        assert db.execute("SELECT patient_id FROM encounters WHERE encounter_id = %s",
                          (enc,)).fetchone() == (winner["patient_id"],)
        assert db.execute("SELECT count(*) FROM encounter_events WHERE encounter_id = %s",
                          (enc,)).fetchone()[0] == 1
