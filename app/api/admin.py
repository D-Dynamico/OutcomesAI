# Design section 5, "Failed is terminal; redrive is deliberate", and section 6.7 ("After
# exhaustion: inspection and safe redrive"). Endpoints per DECISIONS.md D4 and D26.
#
# A failed job is redriven only if its version still equals the encounter's current_version:
# it goes back to queued with redrive_generation + 1, which is a fresh retry budget. attempts,
# the fencing token, is never touched. A failed job whose encounter has moved on is marked
# superseded instead, since its summary could never be shown.
import logging
from datetime import datetime

from psycopg_pool import ConnectionPool

from app.api.response import Response
from app.db import transaction

log = logging.getLogger("admin")

REDRIVE_ONE = """
UPDATE summary_jobs j
   SET status             = 'queued',
       completed_at       = NULL,                  -- no longer terminal
       next_attempt_at    = now(),
       redrive_generation = j.redrive_generation + 1   -- fresh retry budget
  FROM encounters e
 WHERE j.job_id       = %(job_id)s
   AND e.encounter_id = j.encounter_id
   AND j.status       = 'failed'
   AND e.current_version = j.version
RETURNING j.job_id
"""

# DECISIONS.md D4: "if zero rows, mark it superseded", fenced so that only a failed job whose
# encounter has genuinely advanced is touched
SUPERSEDE_ONE = """
UPDATE summary_jobs j
   SET status                = 'superseded',
       superseded_by_version = e.current_version
  FROM encounters e
 WHERE j.job_id          = %(job_id)s
   AND e.encounter_id    = j.encounter_id
   AND j.status          = 'failed'
   AND e.current_version > j.version
RETURNING e.current_version
"""

JOB_STATUS = "SELECT status::text FROM summary_jobs WHERE job_id = %(job_id)s"

# Bulk: the same two conditional updates, set-based over failed jobs whose completed_at falls
# in [failed_from, failed_to). One transaction; a job redriven by the first statement is no
# longer failed, so the second cannot touch it.
REDRIVE_WINDOW = """
UPDATE summary_jobs j
   SET status             = 'queued',
       completed_at       = NULL,
       next_attempt_at    = now(),
       redrive_generation = j.redrive_generation + 1
  FROM encounters e
 WHERE e.encounter_id = j.encounter_id
   AND j.status       = 'failed'
   AND e.current_version = j.version
   AND j.completed_at >= %(failed_from)s
   AND j.completed_at <  %(failed_to)s
RETURNING j.job_id
"""

SUPERSEDE_WINDOW = """
UPDATE summary_jobs j
   SET status                = 'superseded',
       superseded_by_version = e.current_version
  FROM encounters e
 WHERE e.encounter_id    = j.encounter_id
   AND j.status          = 'failed'
   AND e.current_version > j.version
   AND j.completed_at >= %(failed_from)s
   AND j.completed_at <  %(failed_to)s
RETURNING j.job_id
"""


def redrive_job(pool: ConnectionPool, job_id: int) -> Response:
    params = {"job_id": job_id}
    with transaction(pool) as conn:
        if conn.execute(REDRIVE_ONE, params).fetchone() is not None:
            log.info("redriven", extra={"job_id": job_id})
            return Response(200, {"job_id": job_id, "outcome": "redriven"})

        row = conn.execute(SUPERSEDE_ONE, params).fetchone()
        if row is not None:
            log.info("redrive_superseded", extra={"job_id": job_id, "superseded_by_version": row[0]})
            return Response(200, {"job_id": job_id, "outcome": "superseded",
                                  "superseded_by_version": row[0]})

        status = conn.execute(JOB_STATUS, params).fetchone()
    if status is None:
        return Response(404, {"error": "job_not_found", "job_id": job_id})
    return Response(409, {"error": "job_not_failed", "job_id": job_id, "status": status[0]})


def redrive_window(pool: ConnectionPool, failed_from: datetime, failed_to: datetime) -> Response:
    params = {"failed_from": failed_from, "failed_to": failed_to}
    with transaction(pool) as conn:
        redriven = [r[0] for r in conn.execute(REDRIVE_WINDOW, params).fetchall()]
        superseded = [r[0] for r in conn.execute(SUPERSEDE_WINDOW, params).fetchall()]
    log.info("bulk_redrive", extra={"redriven_job_ids": redriven, "superseded_job_ids": superseded})
    return Response(200, {"redriven": len(redriven), "superseded": len(superseded)})


def parse_window(body) -> tuple[datetime, datetime]:
    """Raises ValueError with a fixed message; the offending value is never echoed."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    bounds = []
    for name in ("failed_from", "failed_to"):
        raw = body.get(name)
        try:
            ts = datetime.fromisoformat(raw) if isinstance(raw, str) else None
        except ValueError:
            ts = None
        if ts is None or ts.tzinfo is None:
            raise ValueError(f"{name} must be an ISO 8601 timestamp with a timezone")
        bounds.append(ts)
    if bounds[0] >= bounds[1]:
        raise ValueError("failed_from must be earlier than failed_to")
    return bounds[0], bounds[1]
