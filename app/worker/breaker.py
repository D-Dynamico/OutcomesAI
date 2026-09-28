# Design section 5: the circuit breaker. One shared row in Postgres, read by every worker
# before claiming. It watches the service, not a job: the evidence for tripping is the
# existing job_attempts history, and probes are synthetic calls recorded on the breaker row.
from dataclasses import dataclass

import psycopg
from psycopg_pool import ConnectionPool

from app.config import Config
from app.db import transaction


@dataclass(frozen=True)
class BreakerSettings:
    window: int                   # last N service-reaching attempts (20)
    threshold: int                # failures among them that trip it (11, a strict majority)
    lookback_seconds: float       # ignore attempts older than this (10 minutes)
    cooldown_seconds: float       # open: no calls before open_until (30 s)
    probe_deadline_seconds: float # half_open: probe presumed lost after this (60 s)

    @classmethod
    def from_config(cls, config: Config) -> "BreakerSettings":
        return cls(config.breaker_window, config.breaker_threshold,
                   config.breaker_lookback_minutes * 60, config.breaker_cooldown_seconds,
                   config.probe_deadline_seconds)


# ---------------------------------------------------------------- tripping (closed -> open)

# Only outcomes that reflect the service's behaviour count: discarded is a success (the call
# returned), skipped (no call) and lease_expired (a worker problem) are excluded. Probes are
# not in job_attempts, so they never feed this.
#
# DECISIONS.md D2: attempts that finished before the breaker last closed are ignored, so the
# failures that tripped it cannot re-trip it after recovery. While the breaker is closed,
# updated_at is the moment it closed. This relies on nothing else writing the breaker row
# while it is closed: the only writers are the trip below (closed -> open), the probe race
# (open/half_open only), and the probe report (half_open only).
RECENT_FAILURES = """
SELECT count(*) FILTER (WHERE outcome = 'transient_error') AS failures
  FROM (SELECT outcome FROM job_attempts
         WHERE outcome IN ('succeeded', 'superseded', 'discarded', 'transient_error')
           AND finished_at > greatest(now() - make_interval(secs => %(lookback_seconds)s),
                                      (SELECT updated_at FROM circuit_breaker
                                        WHERE name = 'generate_summary'))
         ORDER BY finished_at DESC
         LIMIT %(window)s) recent
"""

# DECISIONS.md D24: serialises concurrent trip checks. Without it, workers recording failures
# at the same moment each count only committed failures plus their own, so several can see
# 10 and none trips at 11. Lock order is safe: these transactions take the breaker row last,
# and the probe transactions take only the breaker row. A lock is not a write, so D2 holds.
LOCK_BREAKER = """
SELECT state::text FROM circuit_breaker WHERE name = 'generate_summary' FOR UPDATE
"""

TRIP = """
UPDATE circuit_breaker
   SET state = 'open', open_until = now() + make_interval(secs => %(cooldown_seconds)s),
       opened_at = now(), updated_at = now()
 WHERE name = 'generate_summary' AND state = 'closed'
RETURNING name
"""


def trip_if_failing(conn: psycopg.Connection, settings: BreakerSettings) -> bool:
    """Runs inside the transaction that records a transient_error. True if this call tripped it."""
    params = {"lookback_seconds": settings.lookback_seconds, "window": settings.window,
              "cooldown_seconds": settings.cooldown_seconds}
    if conn.execute(LOCK_BREAKER).fetchone()[0] != "closed":
        return False        # already open or probing; the trip UPDATE would match nothing
    # A fresh statement after the lock: sees every failure committed before we got it
    failures = conn.execute(RECENT_FAILURES, params).fetchone()[0]
    if failures < settings.threshold:
        return False
    return conn.execute(TRIP, params).fetchone() is not None


# ---------------------------------------------------------------- the claim gate

BREAKER_STATE = "SELECT state::text FROM circuit_breaker WHERE name = 'generate_summary'"


def breaker_state(conn: psycopg.Connection) -> str:
    return conn.execute(BREAKER_STATE).fetchone()[0]


# ---------------------------------------------------------------- probing (open -> half_open -> closed or open)

# Idle workers race on this conditional update; Postgres guarantees one winner. probes_sent
# is incremented in this commit, before the call goes out: the record of spend exists first.
START_PROBE = """
UPDATE circuit_breaker
   SET state            = 'half_open',
       probe_deadline   = now() + make_interval(secs => %(probe_deadline_seconds)s),
       probe_generation = probe_generation + 1,
       probes_sent      = probes_sent + 1,
       last_probe_at    = now(),
       updated_at       = now()
 WHERE name = 'generate_summary'
   AND (   (state = 'open'      AND open_until     <= now())
        OR (state = 'half_open' AND probe_deadline <  now()))   -- previous prober died
RETURNING probe_generation AS my_probe
"""

# Both reports are fenced by probe_generation: a stalled prober cannot overwrite a newer verdict
PROBE_SUCCEEDED = """
UPDATE circuit_breaker
   SET state = 'closed', opened_at = NULL,
       last_probe_outcome = 'succeeded', updated_at = now()
 WHERE name = 'generate_summary' AND state = 'half_open' AND probe_generation = %(my_probe)s
RETURNING name
"""

PROBE_FAILED = """
UPDATE circuit_breaker
   SET state = 'open', open_until = now() + make_interval(secs => %(cooldown_seconds)s),
       last_probe_outcome = 'transient_error', updated_at = now()
 WHERE name = 'generate_summary' AND state = 'half_open' AND probe_generation = %(my_probe)s
RETURNING name
"""


def start_probe(pool: ConnectionPool, settings: BreakerSettings) -> int | None:
    """Win the probe race. Returns this prober's fencing token, or None if another worker won
    or the cooldown has not passed."""
    with transaction(pool) as conn:
        row = conn.execute(START_PROBE,
                           {"probe_deadline_seconds": settings.probe_deadline_seconds}).fetchone()
    return None if row is None else row[0]


def report_probe(pool: ConnectionPool, my_probe: int, succeeded: bool, settings: BreakerSettings) -> bool:
    """False if this prober was superseded (its deadline passed and another worker probed)."""
    sql = PROBE_SUCCEEDED if succeeded else PROBE_FAILED
    with transaction(pool) as conn:
        row = conn.execute(sql, {"my_probe": my_probe,
                                 "cooldown_seconds": settings.cooldown_seconds}).fetchone()
    return row is not None
