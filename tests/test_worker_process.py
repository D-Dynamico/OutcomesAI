# DECISIONS.md D23: when one loop hits a non-provider error, or on SIGTERM, no loop claims a
# new job, loops with a call in flight get a bounded window to finish and write the result,
# then the process exits (non-zero after a crash or an unfinished drain).
import threading
import time

import pytest

from app.summary.mock import Gate, ScriptedSummaryClient
from app.worker.main import run_loops
from tests.factories import make_event, post


def start_run_loops(workers, stop, drain_seconds):
    result = []
    t = threading.Thread(target=lambda: result.append(run_loops(workers, stop, drain_seconds)),
                         daemon=True)
    t.start()
    return t, result


def statuses(db):
    return dict(db.execute("SELECT status::text, count(*) FROM summary_jobs GROUP BY 1").fetchall())


@pytest.fixture
def three_jobs(api):
    for _ in range(3):
        post(api, make_event(version=1))


def crash_while_other_loop_is_in_flight(make_worker, gate):
    """w-1 holds its call on `gate`; w-2 claims another job and hits a bug mid-call."""
    in_flight = ScriptedSummaryClient(default="W1-SUMMARY")
    in_flight.push(gate)

    def bug(_):
        assert gate.entered.wait(5)
        raise KeyError("a bug, not a provider failure")

    crashing = ScriptedSummaryClient(default=bug)
    return [make_worker("w-1", client=in_flight), make_worker("w-2", client=crashing)], in_flight


def test_crash_drains_in_flight_call_then_exits_nonzero(db, make_worker, three_jobs):
    gate = Gate("W1-SUMMARY")
    workers, in_flight = crash_while_other_loop_is_in_flight(make_worker, gate)
    stop = threading.Event()
    t, result = start_run_loops(workers, stop, drain_seconds=5)

    assert stop.wait(5)          # w-2 crashed and stopped claiming for every loop
    time.sleep(0.3)              # w-1 is still mid-call; nothing new may be claimed meanwhile
    assert statuses(db) == {"processing": 2, "queued": 1}
    gate.release()

    t.join(5)
    assert result == [1]
    # w-1's paid call was written; w-2's job waits for its lease; the third was never claimed
    assert statuses(db) == {"ready": 1, "processing": 1, "queued": 1}
    assert in_flight.real_calls == 1
    assert db.execute("SELECT outcome::text, count(*) FROM job_attempts GROUP BY 1 ORDER BY 1").fetchall() == \
        [("in_flight", 1), ("succeeded", 1)]


def test_drain_is_bounded(db, make_worker, three_jobs):
    gate = Gate("W1-SUMMARY")
    workers, _ = crash_while_other_loop_is_in_flight(make_worker, gate)
    stop = threading.Event()
    t, result = start_run_loops(workers, stop, drain_seconds=0.5)

    assert stop.wait(5)
    started = time.monotonic()
    t.join(5)
    assert result == [1]
    assert time.monotonic() - started < 2      # did not wait for the held call
    assert statuses(db) == {"processing": 2, "queued": 1}
    gate.release()                             # let the abandoned thread finish before teardown
    time.sleep(0.3)


def test_sigterm_drains_and_exits_zero(db, make_worker, three_jobs):
    gate = Gate("S")
    client = ScriptedSummaryClient()
    client.push(gate)
    stop = threading.Event()
    t, result = start_run_loops([make_worker("w-1", client=client)], stop, drain_seconds=5)

    assert gate.entered.wait(5)
    stop.set()                   # what the SIGTERM handler does
    time.sleep(0.2)
    gate.release()
    t.join(5)
    assert result == [0]
    assert statuses(db) == {"ready": 1, "queued": 2}


def test_idle_loops_stop_promptly(make_worker):
    stop = threading.Event()
    t, result = start_run_loops([make_worker("w-1"), make_worker("w-2")], stop, drain_seconds=5)
    time.sleep(0.2)
    stop.set()
    t.join(2)
    assert result == [0]
