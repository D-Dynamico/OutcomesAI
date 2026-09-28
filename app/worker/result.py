# Design section 5: writing the result (txn 3b, the guarded write) and, on lost ownership,
# marking the attempt discarded. The transient-error path (txn 3a) is added in milestone 5.
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
