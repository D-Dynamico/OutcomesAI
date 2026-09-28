# Design section 5, "Worker flow". Three short transactions; generate_summary runs outside
# all of them, holding no connection, lock or open transaction.
#
# The circuit breaker (claim gate and probe) arrives in milestone 6.
import logging
import os
import random
import signal
import socket
import sys
import threading
import time

from psycopg_pool import ConnectionPool

from app.config import Config
from app.db import apply_schema_url, create_pool
from app.hooks import Hooks
from app.summary.client import HttpSummaryClient, SummaryClient, TransientError
from app.worker.claim import claim
from app.worker.precall import precall_supersede
from app.worker.result import guarded_write, mark_attempt_discarded, record_failure

log = logging.getLogger("worker")


class Worker:
    """One worker loop. A process runs WORKER_CONCURRENCY of these, each with its own worker_id."""

    def __init__(self, pool: ConnectionPool, client: SummaryClient, config: Config,
                 worker_id: str, hooks: Hooks | None = None, rng: random.Random | None = None):
        self.pool = pool
        self.client = client
        self.config = config
        self.worker_id = worker_id
        self.hooks = hooks or Hooks(enabled=False)
        self.rng = rng or random.Random()   # backoff jitter

    def run_once(self) -> str:
        """One pass of the loop. Returns what happened, for tests and logs."""
        job = claim(self.pool, self.worker_id, self.config.lease_seconds,
                    self.config.retry_budget)                                   # txn 1
        if job is None:
            return "idle"
        ids = {"job_id": job.job_id, "encounter_id": job.encounter_id, "version": job.version,
               "attempt_no": job.my_attempt, "worker_id": self.worker_id}
        if job.failed_on_reclaim:
            # Budget spent by abandoned attempts: a crash signature, surfaced as worker_lost
            log.warning("failed_on_reclaim", extra=ids)
            return "failed_on_reclaim"
        log.info("claimed", extra={**ids, "reclaimed": job.reclaimed})
        self.hooks.fire("worker.after_claim", worker=self, job=job)

        if precall_supersede(self.pool, job):                                # txn 2
            log.info("skipped_superseded", extra=ids)
            return "skipped"

        self.hooks.fire("worker.before_call", worker=self, job=job)
        try:
            summary = self.client.generate_summary(job.input_transcription)  # no transaction open
        except TransientError as e:
            status = record_failure(self.pool, job, e.error_class,                  # txn 3a
                                    budget=self.config.retry_budget,
                                    backoff_base_seconds=self.config.backoff_base_seconds,
                                    rng=self.rng)
            if status is None:
                log.warning("transient_error_after_lost_ownership",
                            extra={**ids, "error_class": e.error_class})
                return "lost_ownership"
            log.warning("transient_error", extra={**ids, "error_class": e.error_class, "job_status": status})
            return "failed" if status == "failed" else "transient_error"
        self.hooks.fire("worker.after_call", worker=self, job=job)

        status = guarded_write(self.pool, job, summary)                      # txn 3b
        if status is None:
            mark_attempt_discarded(self.pool, job)
            log.warning("discarded", extra=ids)
            return "discarded"
        log.info("result_written", extra={**ids, "status": status})
        return status

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            if self.run_once() == "idle":
                stop.wait(self.config.worker_poll_seconds)


# DECISIONS.md D23: time allowed after the drain window for an in-flight call's result write
DRAIN_WRITE_MARGIN_SECONDS = 5.0


def run_loops(workers: list[Worker], stop: threading.Event, drain_seconds: float) -> int:
    """Run each worker in its own thread until stopped. Returns the process exit code.

    Stops on SIGTERM (stop set by the signal handler) or when one loop hits a non-provider
    error (DECISIONS.md D5, D23). Either way no loop claims a new job once stop is set, and
    loops with a call in flight get up to drain_seconds to finish it and write the result,
    since that call may already be paid for. Loops still running after that are abandoned;
    their jobs are reclaimed when the lease expires.
    """
    crashed = threading.Event()

    def run_loop(worker: Worker) -> None:
        try:
            worker.run(stop)
        except Exception as e:
            # Exception type only, never its message (section 7)
            log.error("worker_loop_crashed", extra={"worker_id": worker.worker_id,
                                                    "error_type": type(e).__name__})
            crashed.set()
            stop.set()

    threads = [threading.Thread(target=run_loop, args=(w,), name=w.worker_id, daemon=True)
               for w in workers]
    for t in threads:
        t.start()

    while not stop.wait(0.2):          # short waits so signal handlers run promptly
        if not any(t.is_alive() for t in threads):
            break

    deadline = time.monotonic() + drain_seconds
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    stragglers = [t.name for t in threads if t.is_alive()]
    if stragglers:
        log.warning("drain_timeout", extra={"worker_ids": stragglers})
    return 1 if crashed.is_set() or stragglers else 0


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = Config.from_env()
    config.validate_worker()
    apply_schema_url(config.database_url)
    pool = create_pool(config.database_url, config.db_pool_size)
    hooks = Hooks(config.test_hooks, config.crash_at)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    base_id = f"{socket.gethostname()}-{os.getpid()}"
    workers = [Worker(pool, HttpSummaryClient(config.mock_ai_url, config.ai_timeout_seconds),
                      config, f"{base_id}-{slot}", hooks)
               for slot in range(config.worker_concurrency)]
    log.info("worker started", extra={"worker_id": base_id, "slots": config.worker_concurrency})

    code = run_loops(workers, stop, config.ai_timeout_seconds + DRAIN_WRITE_MARGIN_SECONDS)

    pool.close(timeout=5)
    log.info("worker stopped", extra={"worker_id": base_id, "exit_code": code})
    sys.exit(code)


if __name__ == "__main__":
    main()
