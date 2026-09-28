# Prometheus metrics (design section 7, "Observability"; section 5, "SLA detection").
#
# Three groups (DECISIONS.md D10, D27):
#   API_REGISTRY     process-local ingestion counters, served on the API's /metrics
#   DatabaseCollector  gauges read from Postgres on every scrape of the API's /metrics: queue
#                    depth, SLA breaches (the design's scheduled check; the scrape interval is
#                    the schedule), job counts, breaker state and probes_sent. Correct however
#                    many API or worker processes run, since the database is the source.
#   WORKER_REGISTRY  per-worker-process call, attempt and timing metrics, served on each
#                    worker's internal WORKER_METRICS_PORT
#
# Labels are fixed enum values only. No IDs, no patient content.
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from psycopg_pool import ConnectionPool

# ---------------------------------------------------------------- API (ingestion)

API_REGISTRY = CollectorRegistry()

INGEST_EVENTS = Counter(
    "ingest_events_total", "POST outcomes, including rejected requests",
    ["outcome"], registry=API_REGISTRY)
INGEST_SOURCE_DEFECTS = Counter(
    "ingest_duplicate_source_defects_total",
    "Duplicates that arrived under a fresh event_id for a recorded (encounter_id, version)",
    registry=API_REGISTRY)
INGEST_IDENTITY_CONFLICTS = Counter(
    "ingest_identity_conflicts_total", "Events contradicting an encounter's stored identity",
    registry=API_REGISTRY)
INGEST_PAYLOAD_CONFLICTS = Counter(
    "ingest_payload_conflicts_total", "Events contradicting a recorded payload for the same version",
    registry=API_REGISTRY)

INGEST_OUTCOMES = ("accepted", "duplicate", "stale", "identity_conflict", "payload_conflict",
                   "invalid_request", "payload_too_large")
for _outcome in INGEST_OUTCOMES:
    INGEST_EVENTS.labels(_outcome)   # present at zero, so rates and alerts work from the start


# ---------------------------------------------------------------- database-derived gauges (on scrape)

QUEUE_SQL = """
SELECT count(*) FILTER (WHERE status = 'queued' AND next_attempt_at <= now()) AS due_now,
       count(*) FILTER (WHERE status = 'queued' AND next_attempt_at >  now()) AS waiting_backoff,
       count(*) FILTER (WHERE status = 'processing')                           AS processing
  FROM summary_jobs
 WHERE status IN ('queued', 'processing')
"""

# Design section 5, "Detection: a scheduled check"
SLA_SQL = """
SELECT count(*)                                      AS breaching_jobs,
       count(*) FILTER (WHERE status = 'queued')     AS breaching_queued,
       count(*) FILTER (WHERE status = 'processing') AS breaching_processing,
       coalesce(extract(epoch FROM max(now() - accepted_at)), 0)::float AS oldest_unfinished_age
  FROM summary_jobs
 WHERE status IN ('queued', 'processing')
   AND accepted_at < now() - make_interval(secs => %(sla_seconds)s)
"""

STATUS_SQL = "SELECT status::text, count(*) FROM summary_jobs GROUP BY status"

BREAKER_SQL = """
SELECT state::text, probes_sent, coalesce(extract(epoch FROM now() - opened_at), 0)::float
  FROM circuit_breaker WHERE name = 'generate_summary'
"""

JOB_STATUSES = ("queued", "processing", "ready", "failed", "superseded")
BREAKER_STATES = ("closed", "open", "half_open")


class DatabaseCollector:
    def __init__(self, pool: ConnectionPool, sla_seconds: float):
        self.pool = pool
        self.sla_seconds = sla_seconds

    def collect(self):
        up = GaugeMetricFamily("summary_db_up", "1 if the database gauges below could be read")
        try:
            with self.pool.connection(timeout=5) as conn:
                due_now, waiting, processing = conn.execute(QUEUE_SQL).fetchone()
                breaching, b_queued, b_processing, oldest = conn.execute(
                    SLA_SQL, {"sla_seconds": self.sla_seconds}).fetchone()
                statuses = dict(conn.execute(STATUS_SQL).fetchall())
                state, probes_sent, open_for = conn.execute(BREAKER_SQL).fetchone()
        except Exception:
            up.add_metric([], 0)
            yield up
            return
        up.add_metric([], 1)
        yield up

        queue = GaugeMetricFamily("summary_queue_depth",
                                  "Queued jobs due now vs waiting on backoff; jobs being processed",
                                  labels=["state"])
        queue.add_metric(["due_now"], due_now)
        queue.add_metric(["waiting_backoff"], waiting)
        queue.add_metric(["processing"], processing)
        yield queue

        for name, value, doc in (
            ("summary_sla_breaching_jobs", breaching, "Unfinished jobs older than the SLA"),
            ("summary_sla_breaching_queued", b_queued, "Breaching jobs waiting in the queue"),
            ("summary_sla_breaching_processing", b_processing, "Breaching jobs held by a worker"),
            ("summary_sla_oldest_unfinished_age_seconds", oldest,
             "Age of the oldest breaching job (0 when none breach)"),
        ):
            g = GaugeMetricFamily(name, doc)
            g.add_metric([], value)
            yield g

        jobs = GaugeMetricFamily("summary_jobs", "Jobs by status", labels=["status"])
        for status in JOB_STATUSES:
            jobs.add_metric([status], statuses.get(status, 0))
        yield jobs

        breaker = GaugeMetricFamily("breaker_state", "1 for the breaker's current state", labels=["state"])
        for s in BREAKER_STATES:
            breaker.add_metric([s], 1 if s == state else 0)
        yield breaker
        opened = GaugeMetricFamily("breaker_open_seconds", "Time since the current outage began (0 if closed)")
        opened.add_metric([], open_for)
        yield opened
        probes = CounterMetricFamily("breaker_probes_sent", "Paid probe calls, from the breaker row")
        probes.add_metric([], probes_sent)
        yield probes


class Combined:
    """Lets generate_latest render several collectors as one exposition."""

    def __init__(self, *collectors):
        self.collectors = collectors

    def collect(self):
        for c in self.collectors:
            yield from c.collect()


# ---------------------------------------------------------------- worker (per process)

WORKER_REGISTRY = CollectorRegistry()

DURATION_BUCKETS = (0.5, 1, 2, 5, 8, 10, 15, 20, 30, 60, 120, 300, 600, 1800)

AI_CALLS = Counter(
    "generate_summary_calls_total", "Paid generate_summary calls by kind and result",
    ["kind", "result"], registry=WORKER_REGISTRY)
AI_CALL_SECONDS = Histogram(
    "generate_summary_call_seconds", "generate_summary latency", ["kind"],
    buckets=DURATION_BUCKETS, registry=WORKER_REGISTRY)
ATTEMPTS_CLOSED = Counter(
    "job_attempts_closed_total", "Attempts closed, by outcome and error_class",
    ["outcome", "error_class"], registry=WORKER_REGISTRY)
JOBS_FAILED = Counter(
    "summary_jobs_failed_total", "Jobs that exhausted their retry budget and need a human decision",
    ["path"], registry=WORKER_REGISTRY)
BREAKER_TRIPS = Counter(
    "breaker_trips_total", "Times this process's failure opened the breaker", registry=WORKER_REGISTRY)
PROBES = Counter(
    "breaker_probes_total", "Probes sent by this process, by result", ["result"], registry=WORKER_REGISTRY)
WORKER_SLOTS = Gauge("worker_slots", "Worker loops in this process", registry=WORKER_REGISTRY)
WORKER_BUSY = Gauge("worker_busy_slots", "Loops currently holding a claimed job", registry=WORKER_REGISTRY)
QUEUE_WAIT = Histogram(
    "summary_queue_wait_seconds", "started_at - accepted_at, for jobs reaching ready",
    buckets=DURATION_BUCKETS, registry=WORKER_REGISTRY)
PROCESSING_TIME = Histogram(
    "summary_processing_seconds", "completed_at - started_at, for jobs reaching ready",
    buckets=DURATION_BUCKETS, registry=WORKER_REGISTRY)
COMPLETION_TIME = Histogram(
    "summary_completion_seconds", "completed_at - accepted_at (the SLA measure), for jobs reaching ready",
    buckets=DURATION_BUCKETS, registry=WORKER_REGISTRY)
