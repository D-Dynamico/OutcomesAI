# Test harness (design section 7): in-process workers driven against the real test database
# with a scriptable mock. Durations are shrunk so tests run in seconds; the logic is unchanged.
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from app.worker.main import Worker


def fetch_job(db, encounter_id, version):
    row = db.execute(
        "SELECT job_id, status::text, attempts, redrive_generation, summary, superseded_by_version, "
        "started_at, completed_at, lease_expires_at FROM summary_jobs "
        "WHERE encounter_id = %s AND version = %s", (encounter_id, version)).fetchone()
    if row is None:
        return None
    keys = ("job_id", "status", "attempts", "redrive_generation", "summary", "superseded_by_version",
            "started_at", "completed_at", "lease_expires_at")
    return dict(zip(keys, row))


def fetch_attempts(db, job_id):
    return db.execute(
        "SELECT attempt_no, redrive_generation, worker_id, outcome::text, error_class, "
        "finished_at IS NOT NULL FROM job_attempts WHERE job_id = %s ORDER BY attempt_no",
        (job_id,)).fetchall()


def expire_lease(db, job_id):
    # Moves the lease into the past on the database clock, as if the owner had stalled past it
    db.execute("UPDATE summary_jobs SET lease_expires_at = now() - interval '1 second' WHERE job_id = %s",
               (job_id,))


@dataclass
class Background:
    thread: threading.Thread
    result: list

    def join(self, timeout=10):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "worker did not finish"
        return self.result[0]


def run_in_background(worker: Worker) -> Background:
    result = []
    thread = threading.Thread(target=lambda: result.append(worker.run_once()), daemon=True)
    thread.start()
    return Background(thread, result)


def drain(workers, max_passes=1000):
    """Run workers concurrently until every one of them finds nothing to claim."""
    barrier = threading.Barrier(len(workers))
    errors = []

    def loop(w):
        try:
            barrier.wait()
            for _ in range(max_passes):
                if w.run_once() == "idle":
                    return
        except Exception as e:  # noqa: BLE001 - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=loop, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors


# ---------------------------------------------------------------- forcing interleavings (design section 7)

WAITING_ON_LOCK = """
SELECT count(*) FROM pg_stat_activity
 WHERE datname = current_database() AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()
"""


class LockHold:
    """Hook for ingest.after_lock. The first transaction to take the encounter row lock holds it
    until Postgres shows another backend waiting on a lock, so the second request is guaranteed
    to arrive while the first is mid-transaction. `forced` records that this happened; tests
    assert it, so an iteration that did not interleave cannot pass silently."""

    def __init__(self, db_url: str, timeout: float = 5.0):
        import psycopg
        self._monitor = psycopg.connect(db_url, autocommit=True)
        self._timeout = timeout
        self._mutex = threading.Lock()
        self.armed = False

    def arm(self):
        """Hold the next transaction to take the lock. Setup requests run disarmed."""
        self.holder = None
        self.forced = False
        self.entered = threading.Event()
        self.armed = True

    def disarm(self):
        self.armed = False

    def __call__(self, event, **_):
        with self._mutex:
            if not self.armed or self.holder is not None:
                return                      # second to arrive: the first has already committed
            self.holder = event.event_id
        self.entered.set()
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            if self._monitor.execute(WAITING_ON_LOCK).fetchone()[0] > 0:
                self.forced = True
                return
            time.sleep(0.002)

    def close(self):
        self._monitor.close()


def released_together(clients, events):
    """Send each event through its own client, all released from one barrier."""
    barrier = threading.Barrier(len(events))
    responses = [None] * len(events)

    def send(i):
        barrier.wait()
        responses[i] = clients[i].post("/encounters/events", json=events[i])

    threads = [threading.Thread(target=send, args=(i,)) for i in range(len(events))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return responses


class Sampler:
    """Polls GET for one encounter in the background, recording (started_at, body) pairs."""

    def __init__(self, client, encounter_id):
        self.client = client
        self.encounter_id = encounter_id
        self.samples = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.is_set():
            started = time.monotonic()
            r = self.client.get(f"/encounters/{self.encounter_id}/summary")
            self.samples.append((started, r.status_code, r.json()))

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(10)


# ---------------------------------------------------------------- a real API process, for crash injection

class ApiProcess:
    """uvicorn serving the API against the test database. With crash_at set, the test-only hook
    kills the process at that named point with os._exit (app/hooks.py)."""

    def __init__(self, db_url: str, crash_at: str = ""):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.env = {**os.environ, "DATABASE_URL": db_url, "TEST_HOOKS": "1", "CRASH_AT": crash_at}
        self.proc = None

    def __enter__(self):
        import httpx
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.api.main:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(self.port)],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if httpx.get(self.url + "/healthz", timeout=1).status_code == 200:
                    return self
            except httpx.TransportError:
                pass
            time.sleep(0.1)
        raise RuntimeError("API process did not become healthy")

    def post(self, event):
        import httpx
        return httpx.post(self.url + "/encounters/events", json=event, timeout=10)

    def __exit__(self, *exc):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(10)
