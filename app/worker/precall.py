# Design section 5: pre-call check (txn 2).
#
# Is this job's version still current? Decided and recorded by one fenced conditional
# update, not a read followed by a write. If obsolete, the job becomes superseded and the
# attempt skipped in the same commit: no call, no cost, budget untouched.
from psycopg_pool import ConnectionPool

from app.db import transaction
from app.worker.claim import ClaimedJob

SUPERSEDE_IF_OBSOLETE = """
UPDATE summary_jobs j
   SET status                = 'superseded',
       superseded_by_version = e.current_version,
       completed_at          = now()
  FROM encounters e
 WHERE j.job_id          = %(job_id)s
   AND e.encounter_id    = j.encounter_id
   AND j.status          = 'processing'
   AND j.attempts        = %(my_attempt)s
   AND e.current_version > j.version
RETURNING j.job_id
"""

MARK_SKIPPED = """
UPDATE job_attempts
   SET outcome = 'skipped', finished_at = now()
 WHERE job_id = %(job_id)s AND attempt_no = %(my_attempt)s
"""


def precall_supersede(pool: ConnectionPool, job: ClaimedJob) -> bool:
    """True if the job was obsolete and has been superseded; the worker must not call."""
    params = {"job_id": job.job_id, "my_attempt": job.my_attempt}
    with transaction(pool) as conn:
        if conn.execute(SUPERSEDE_IF_OBSOLETE, params).fetchone() is None:
            return False
        conn.execute(MARK_SKIPPED, params)
    return True
