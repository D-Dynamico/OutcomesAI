# Design section 5: claiming a job (txn 1).
#
# One transaction: take the oldest claimable job with SKIP LOCKED, move the fencing token,
# stamp the lease, close out any abandoned previous attempt, and open this attempt. The
# attempt row is committed here, before any paid call (section 5, "Why the call cannot sit
# inside a transaction").
from dataclasses import dataclass

from psycopg_pool import ConnectionPool

from app.db import transaction

# Step 1: queued and due, or processing with an expired lease
TAKE_JOB = """
UPDATE summary_jobs
   SET status           = 'processing',
       attempts         = attempts + 1,
       lease_expires_at = now() + make_interval(secs => %(lease_seconds)s),
       started_at       = COALESCE(started_at, now())
 WHERE job_id = (
         SELECT job_id
           FROM summary_jobs
          WHERE (status = 'queued'     AND next_attempt_at  <= now())
             OR (status = 'processing' AND lease_expires_at <  now())
          ORDER BY accepted_at
          FOR UPDATE SKIP LOCKED
          LIMIT 1
       )
RETURNING job_id, encounter_id, version, input_transcription,
          attempts AS my_attempt, redrive_generation
"""

# Step 2: if this was a reclaim, mark the previous owner's attempt as abandoned
CLOSE_ABANDONED = """
UPDATE job_attempts
   SET outcome = 'lease_expired', error_class = 'worker_lost', finished_at = now()
 WHERE job_id = %(job_id)s AND attempt_no < %(my_attempt)s AND outcome = 'in_flight'
RETURNING attempt_no
"""

# Step 3, reclaims only: the retry budget. A worker that crashed never reached the
# transient-error write, so the reclaiming worker is where the budget is applied.
BUDGET_USED = """
SELECT count(*) AS used
  FROM job_attempts
 WHERE job_id = %(job_id)s
   AND redrive_generation = %(redrive_generation)s
   AND outcome <> 'skipped'
"""

# If used >= budget: fail the job instead of starting a new attempt. attempt_no then skips a
# value, which fencing tolerates (section 5, "A side effect: gaps in attempt numbers").
FAIL_ON_RECLAIM = """
UPDATE summary_jobs
   SET status = 'failed', completed_at = now(), lease_expires_at = NULL
 WHERE job_id = %(job_id)s AND attempts = %(my_attempt)s
"""

# Step 4: open this attempt
OPEN_ATTEMPT = """
INSERT INTO job_attempts (job_id, attempt_no, redrive_generation, worker_id)
VALUES (%(job_id)s, %(my_attempt)s, %(redrive_generation)s, %(worker_id)s)
"""


@dataclass(frozen=True)
class ClaimedJob:
    job_id: int
    encounter_id: str
    version: int
    input_transcription: str
    my_attempt: int            # fencing token carried to every later write
    redrive_generation: int
    reclaimed: bool            # step 2 closed out a previous owner's attempt
    failed_on_reclaim: bool = False   # step 3 found the budget spent; the job is now failed


def claim(pool: ConnectionPool, worker_id: str, lease_seconds: float, budget: int) -> ClaimedJob | None:
    with transaction(pool) as conn:
        row = conn.execute(TAKE_JOB, {"lease_seconds": lease_seconds}).fetchone()
        if row is None:
            return None
        job_id, encounter_id, version, input_transcription, my_attempt, redrive_generation = row
        params = {"job_id": job_id, "my_attempt": my_attempt,
                  "redrive_generation": redrive_generation, "worker_id": worker_id}

        reclaimed = bool(conn.execute(CLOSE_ABANDONED, params).fetchall())

        if reclaimed and conn.execute(BUDGET_USED, params).fetchone()[0] >= budget:
            conn.execute(FAIL_ON_RECLAIM, params)
            failed_on_reclaim = True     # steps 4 onwards are skipped
        else:
            conn.execute(OPEN_ATTEMPT, params)
            failed_on_reclaim = False

    return ClaimedJob(job_id, encounter_id, version, input_transcription, my_attempt,
                      redrive_generation, reclaimed, failed_on_reclaim)
