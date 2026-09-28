# Design section 5: writing the result (txn 3b, the guarded write), marking the attempt
# discarded on lost ownership, and the transient-error path (txn 3a).
import random

from psycopg_pool import ConnectionPool

from app.db import transaction
from app.worker.claim import ClaimedJob

# Fenced by status = 'processing' and attempts = :my_attempt. The CASE results are cast to
# job_status because Postgres types a CASE of string literals as text (DECISIONS.md D20).
GUARDED_WRITE = """
UPDATE summary_jobs j
   SET status       = CASE WHEN e.current_version = j.version
                           THEN 'ready' ELSE 'superseded' END::job_status,
       superseded_by_version = CASE WHEN e.current_version = j.version
                           THEN NULL ELSE e.current_version END,
       summary      = %(summary)s,
       completed_at = now()
  FROM encounters e
 WHERE j.job_id        = %(job_id)s
   AND e.encounter_id  = j.encounter_id
   AND j.status        = 'processing'
   AND j.attempts      = %(my_attempt)s
RETURNING j.status::text
"""

# Same transaction as the guarded write
CLOSE_ATTEMPT = """
UPDATE job_attempts
   SET outcome = %(outcome)s,   -- 'succeeded' or 'superseded'
       finished_at = now()
 WHERE job_id = %(job_id)s AND attempt_no = %(my_attempt)s
"""

# DECISIONS.md D1: the reclaiming worker already closed this attempt as lease_expired;
# a paid call returned after ownership was lost, so it becomes discarded (error_class kept)
MARK_DISCARDED = """
UPDATE job_attempts
   SET outcome = 'discarded', finished_at = now()
 WHERE job_id = %(job_id)s AND attempt_no = %(my_attempt)s AND outcome = 'lease_expired'
"""


def guarded_write(pool: ConnectionPool, job: ClaimedJob, summary: str) -> str | None:
    """Returns 'ready' or 'superseded', or None if this worker no longer owns the job."""
    params = {"job_id": job.job_id, "my_attempt": job.my_attempt, "summary": summary}
    with transaction(pool) as conn:
        row = conn.execute(GUARDED_WRITE, params).fetchone()
        if row is None:
            return None
        status = row[0]
        conn.execute(CLOSE_ATTEMPT, {**params, "outcome": "succeeded" if status == "ready" else "superseded"})
    return status


def mark_attempt_discarded(pool: ConnectionPool, job: ClaimedJob) -> None:
    with transaction(pool) as conn:
        conn.execute(MARK_DISCARDED, {"job_id": job.job_id, "my_attempt": job.my_attempt})


# ---------------------------------------------------------------- txn 3a: after a transient error
# Section 5, "After a transient error: attempt fails, job does not".

# Budget used in this generation, including this attempt (still 'in_flight', so counted)
BUDGET_USED = """
SELECT count(*) AS used
  FROM job_attempts
 WHERE job_id = %(job_id)s
   AND redrive_generation = %(redrive_generation)s
   AND outcome <> 'skipped'
"""

# Fenced: only the current owner may requeue or fail the job
REQUEUE_OR_FAIL = """
UPDATE summary_jobs
   SET status           = CASE WHEN %(used)s >= %(budget)s THEN 'failed' ELSE 'queued' END::job_status,
       next_attempt_at  = now() + make_interval(secs => %(backoff_seconds)s),   -- ignored if failed
       completed_at     = CASE WHEN %(used)s >= %(budget)s THEN now() END,     -- terminal time if failed
       lease_expires_at = NULL
 WHERE job_id   = %(job_id)s
   AND status   = 'processing'
   AND attempts = %(my_attempt)s
RETURNING status::text
"""

CLOSE_TRANSIENT = """
UPDATE job_attempts
   SET outcome = 'transient_error', error_class = %(error_class)s, finished_at = now()
 WHERE job_id = %(job_id)s AND attempt_no = %(my_attempt)s
"""


def backoff_seconds(used: int, base_seconds: float, rng: random.Random) -> float:
    """random(0.5, 1.0) x base x 2^(used - 1): roughly 10, 20, 40, 80 s after failures 1 to 4."""
    return rng.uniform(0.5, 1.0) * base_seconds * 2 ** (max(used, 1) - 1)


def record_failure(pool: ConnectionPool, job: ClaimedJob, error_class: str, *, budget: int,
                   backoff_base_seconds: float, rng: random.Random) -> str | None:
    """Returns 'queued' or 'failed', or None if this worker no longer owns the job.

    On None the reclaiming worker has already closed this attempt as lease_expired, and it is
    left that way (DECISIONS.md D1).
    """
    params = {"job_id": job.job_id, "my_attempt": job.my_attempt,
              "redrive_generation": job.redrive_generation, "error_class": error_class,
              "budget": budget}
    with transaction(pool) as conn:
        used = conn.execute(BUDGET_USED, params).fetchone()[0]
        params.update(used=used, backoff_seconds=backoff_seconds(used, backoff_base_seconds, rng))
        row = conn.execute(REQUEUE_OR_FAIL, params).fetchone()
        if row is None:
            return None
        conn.execute(CLOSE_TRANSIENT, params)
        # Breaker trip check (milestone 6) runs here, in the same transaction
    return row[0]
