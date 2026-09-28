# Design section 4: GET, retrieve progress and summary.
#
# The summary is resolved through encounters.current_version; there is no stored pointer to
# the current job. A v12 job simply cannot be found once current_version is 13 (section 2).
# sla_breached is computed with Postgres now(), never the API's clock (section 0).
import logging

from psycopg_pool import ConnectionPool

from app.api.response import Response, iso_utc

log = logging.getLogger("read")

# One statement, one snapshot. attempts and error_class come from job_attempts, scoped to the
# job's current redrive generation and excluding skipped attempts (section 4, DECISIONS.md D6);
# summary_jobs.attempts is the fencing token and is never exposed.
READ_CURRENT = """
SELECT e.encounter_id, e.patient_id, e.encounter_type::text, e.current_version,
       j.status::text, j.version, j.summary, j.accepted_at, j.completed_at,
       CASE j.status
            WHEN 'ready'  THEN j.completed_at - j.accepted_at > make_interval(secs => %(sla)s)
            WHEN 'failed' THEN true
            ELSE now() - j.accepted_at > make_interval(secs => %(sla)s)
       END AS sla_breached,
       att.attempts, att.error_class
  FROM encounters e
  LEFT JOIN summary_jobs j
         ON j.encounter_id = e.encounter_id
        AND j.version      = e.current_version
  LEFT JOIN LATERAL (
        SELECT count(*) AS attempts,
               (array_agg(a.error_class ORDER BY a.attempt_no DESC))[1] AS error_class
          FROM job_attempts a
         WHERE a.job_id             = j.job_id
           AND a.redrive_generation = j.redrive_generation
           AND a.outcome           <> 'skipped'
       ) att ON j.status = 'failed'
 WHERE e.encounter_id = %(encounter_id)s
"""


def not_found(encounter_id: str) -> Response:
    return Response(404, {"error": "encounter_not_found", "encounter_id": encounter_id})


def read_summary(pool: ConnectionPool, encounter_id: str, sla_seconds: float) -> Response:
    with pool.connection() as conn:
        row = conn.execute(READ_CURRENT, {"encounter_id": encounter_id, "sla": sla_seconds}).fetchone()

    if row is None:
        return not_found(encounter_id)
    (enc_id, patient_id, encounter_type, current_version, status, job_version, summary,
     accepted_at, completed_at, sla_breached, attempts, error_class) = row

    if status is None:
        # Impossible given single-transaction ingestion; defined response rather than a crash
        # (section 4, DECISIONS.md D18)
        log.error("current_version_without_job", extra={"encounter_id": enc_id,
                                                        "current_version": current_version})
        return not_found(encounter_id)

    body = {
        "encounter_id": enc_id,
        "patient_id": patient_id,
        "encounter_type": encounter_type,
        "current_version": current_version,
    }

    if status in ("queued", "processing"):
        # Queued is reported as processing: internal scheduling the client cannot act on
        body.update({
            "status": "processing",
            "summary": None,
            "accepted_at": iso_utc(accepted_at),
            "sla_breached": sla_breached,
        })
    elif status == "ready":
        body.update({
            "status": "ready",
            "summary_version": job_version,
            "summary": summary,
            "accepted_at": iso_utc(accepted_at),
            "completed_at": iso_utc(completed_at),
            "sla_breached": sla_breached,
        })
    elif status == "failed":
        body.update({
            "status": "failed",
            "summary": None,
            "attempts": attempts,
            "error_class": error_class,
            "accepted_at": iso_utc(accepted_at),
            "completed_at": iso_utc(completed_at),
            "sla_breached": sla_breached,
        })
    else:
        # A superseded job at current_version would contradict the guarded write and pre-call
        # check, which only supersede when current_version > version
        raise RuntimeError("job at current_version is superseded")

    return Response(200, body)
