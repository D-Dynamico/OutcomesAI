# Test harness (design section 7): in-process workers driven against the real test database
# with a scriptable mock. Durations are shrunk so tests run in seconds; the logic is unchanged.
import threading
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
