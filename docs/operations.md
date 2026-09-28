# Operations

Metrics, alerts, logs, triage, configuration, and a walkthrough of retry exhaustion and redrive. For the overview, see the [README](../README.md). Metric and log design decisions are in [D10 and D27](../DECISIONS.md).

## Metrics

Scrape `localhost:8000/metrics` (Prometheus format).

- **Read from Postgres on each scrape.** This is the design's scheduled SLA check (§5), with the scrape interval as the schedule. It stays correct however many processes run:
  - `summary_sla_breaching_jobs`, `_queued` and `_processing`, and `summary_sla_oldest_unfinished_age_seconds`;
  - `summary_queue_depth{state=due_now|waiting_backoff|processing}` and `summary_jobs{status}`;
  - `breaker_state{state}`, `breaker_open_seconds`, `breaker_probes_sent_total` and `summary_db_up`.
- **Ingestion, API process:** `ingest_events_total{outcome}`, `ingest_identity_conflicts_total`, `ingest_payload_conflicts_total` and `ingest_duplicate_source_defects_total`.
- **Per worker process, internal port 9100, not published to the host:**
  - `generate_summary_calls_total{kind,result}` and `generate_summary_call_seconds{kind}`: spend rate and latency;
  - `job_attempts_closed_total{outcome,error_class}`, `summary_jobs_failed_total` and `breaker_trips_total`;
  - `breaker_probes_total{result}`, `worker_slots` and `worker_busy_slots`;
  - the `summary_queue_wait_seconds`, `summary_processing_seconds` and `summary_completion_seconds` histograms.

  Read them with `docker compose exec worker python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:9100/metrics').read().decode())"`. With `--scale worker=N`, each replica serves its own; Docker's DNS returns every replica for the `worker` service name.

## Alerts

From design §7:

- `summary_sla_oldest_unfinished_age_seconds > 60` for 2 minutes;
- `breaker_open_seconds` above a few minutes;
- any increase in `summary_jobs{status="failed"}`: failed work waits for a person;
- `increase(ingest_identity_conflicts_total) > 0`: a partner mapping bug with clinical-safety consequences.

## Logs

`docker compose logs -f` (or `make logs`). Every line from every service is one JSON object: `ts`, `level`, `service`, `logger`, `event`, plus structured fields.

- Lines carry only opaque IDs, statuses, fixed enum values, payload hashes, timestamps and durations.
- **Exceptions are reduced to their type:** no message and no traceback, including uvicorn's own.
- Transcripts, summaries and raw provider errors are never logged.
- `patient_id` appears only in the `identity_conflict` line, where both values are needed to diagnose the partner's mapping bug.

## Triage for a stuck or late summary

First, check whether one job or many are late: look at the breaching gauges and the breaker. If many are late, the cause is system-wide:

| Signal | Meaning | Response |
|---|---|---|
| `breaching_queued` high, long queue wait | Backlog: not enough workers | Scale workers |
| `breaching_processing` high, long processing time | The AI service is slow | Monitor the provider |
| Breaker `open` or `half_open` | AI outage; jobs held in `queued` on purpose | Wait; breaches resolve on recovery |
| Isolated jobs past 60 s with `lease_expired` attempts | Worker crashes | Investigate the worker, and the transcript if it's always the same job |

If it's one job, its row and attempts tell the story:

```sql
SELECT status, accepted_at, started_at, completed_at, next_attempt_at, lease_expires_at
  FROM summary_jobs WHERE encounter_id = 'enc-42' ORDER BY version DESC LIMIT 1;
SELECT attempt_no, redrive_generation, worker_id, outcome, error_class, started_at, finished_at
  FROM job_attempts WHERE job_id = :job_id ORDER BY attempt_no;
```

How to read the attempt history:

- **No attempts:** still queued, in backoff, or waiting behind an open breaker.
- **A run of `transient_error`:** the provider is failing.
- **A run of `lease_expired` / `worker_lost`:** the worker is crashing, or the transcript is poison.
- **One long `in_flight`:** a call currently waiting on the provider.

## Configuration

Every value can be set from the environment, and Compose passes them through from the host, e.g. `BREAKER_COOLDOWN_SECONDS=5 docker compose up`. The defaults are the design's.

| Variable | Default | Meaning (design section) |
|---|---|---|
| `LEASE_SECONDS` | 60 | Worker lease on a claimed job. Must exceed `AI_TIMEOUT_SECONDS`, or startup fails (§5) |
| `AI_TIMEOUT_SECONDS` | 30 | Total client-side deadline per `generate_summary` call (§5, D21) |
| `RETRY_BUDGET` | 5 | Counted attempts per job per redrive generation (§5) |
| `BACKOFF_BASE_SECONDS` | 10 | Backoff `uniform(0.5,1) × base × 2^(n−1)`: about 10, 20, 40, 80 s (§5) |
| `BREAKER_WINDOW` / `BREAKER_THRESHOLD` | 20 / 11 | Trip on 11 failures among the last 20 service-reaching attempts (§5) |
| `BREAKER_LOOKBACK_MINUTES` | 10 | Ignore attempts older than this (§5) |
| `BREAKER_COOLDOWN_SECONDS` | 30 | Open time before a probe (§5) |
| `PROBE_DEADLINE_SECONDS` | 60 | A probe not reported by then is presumed lost (§5) |
| `SLA_SECONDS` | 10 | Acceptance-to-ready target (§5) |
| `MAX_BODY_BYTES` | 1048576 | POST size limit, returns 413 (§3) |
| `WORKER_CONCURRENCY` / `WORKER_POLL_SECONDS` | 4 / 0.5 | Loops per worker process / idle poll interval (D9) |
| `DB_POOL_SIZE` | 10 | Must be at least `WORKER_CONCURRENCY + 1` for workers |
| `WORKER_METRICS_PORT` | 9100 | Worker metrics port, internal only; 0 disables it |
| `MOCK_LATENCY_MIN_SECONDS` / `MAX` | 2 / 8 | mock-ai latency; `MOCK_SLOW_RATE` (0.05) of calls take 12 s |
| `MOCK_FAILURE_RATE` | 0.05 | Split evenly across 429, 503 and a hang past the client timeout |
| `MOCK_OUTAGE` | false | Fail every call. Toggle at runtime with `POST :8001/admin/settings` |

## Limitations

- `summary_jobs{status}` counts the whole table on every scrape. That's fine at this scale; at high volume it would need an index or a sampled count (D27).
- Worker metrics are per process, so a Prometheus deployment would discover the replicas through the `worker` service's DNS name.

## Demo: retry exhaustion and redrive

A job fails only when its own retry budget runs out, for example when the service is only partly degraded and the breaker correctly stays closed. To see it in seconds instead of minutes, shrink the backoff:

```sh
docker compose down -v
BACKOFF_BASE_SECONDS=1 docker compose up -d --build
curl -X POST localhost:8001/admin/settings -H 'content-type: application/json' \
  -d '{"failure_rate": 1, "latency_min_seconds": 0.1, "latency_max_seconds": 0.3, "timeout_hang_seconds": 0}'
curl -X POST localhost:8000/encounters/events -H 'content-type: application/json' -d '{"event_id":"evt-500","encounter_id":"enc-500","patient_id":"pat-5","encounter_type":"MedicationRefill","version":1,"payload":{"transcription":"Nurse: Hi, this is the refill line. Patient: I need to refill my metformin."}}'
sleep 25
curl localhost:8000/encounters/enc-500/summary
```

The job has exhausted its budget. `attempts` and `error_class` come from `job_attempts`, counting only the current redrive generation:

```json
{"encounter_id":"enc-500","patient_id":"pat-5","encounter_type":"MedicationRefill","current_version":1,"status":"failed","summary":null,"attempts":5,"error_class":"rate_limited","accepted_at":"2026-09-28T13:18:53Z","completed_at":"2026-09-28T13:19:02Z","sla_breached":true}
```

The attempt history shows the cost of every call, with no patient content:

```sh
docker compose exec db psql -U outcomes -c \
  "select attempt_no, redrive_generation, outcome, error_class from job_attempts where job_id = 1 order by attempt_no"
#  1 | 0 | transient_error | rate_limited
#  2 | 0 | transient_error | ai_timeout
#  3 | 0 | transient_error | ai_unavailable
#  4 | 0 | transient_error | ai_unavailable
#  5 | 0 | transient_error | rate_limited
```

Fix the provider, then redrive:

```sh
curl -X POST localhost:8001/admin/settings -H 'content-type: application/json' -d '{"failure_rate": 0}'
curl -X POST localhost:8000/admin/jobs/1/redrive      # {"job_id":1,"outcome":"redriven"}
curl localhost:8000/encounters/enc-500/summary        # "status":"ready" ... "sla_breached":true
#  (job_attempts now adds)  6 | 1 | succeeded |
```
