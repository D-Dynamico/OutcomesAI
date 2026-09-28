# Decisions

Every place the implementation interprets, extends, or deviates from `docs/design.md`.
Each entry names the design section it touches. D1–D10 were agreed with the author before
implementation began; later entries were made during implementation.

## D1. A stalled worker's lost write overwrites `lease_expired` with `discarded`
**Touches:** section 5 (claiming step 2; writing the result), section 7 ("Stalled worker fencing").

The design has the reclaiming worker mark the stalled worker's attempt `lease_expired`, and also has the stalled worker mark "its own attempt row `discarded`" when its guarded write matches zero rows. Both refer to the same row. If `lease_expired` were never overwritten, `discarded` could never be written at all, since every way of losing the fence goes through a reclaim.

**Decision.** When a guarded write after a *successful* call matches zero rows, the worker runs:

```sql
UPDATE job_attempts
   SET outcome = 'discarded', finished_at = now()          -- error_class stays 'worker_lost'
 WHERE job_id = :job_id AND attempt_no = :my_attempt AND outcome = 'lease_expired';
```

The transient-error path is unchanged: a stalled worker whose call *failed* leaves its row `lease_expired`, as the design states. Test 7 asserts `lease_expired` right after the reclaim, `discarded` after A returns, and B's attempt `succeeded`. The retry budget counts both attempts either way.

## D2. Breaker trip check ignores attempts that finished before the breaker last closed
**Touches:** section 5 ("Tripping"), section 6.7, section 7 test 5.

The trip query counts the last 20 service-reaching attempts within 10 minutes. After an outage shorter than the lookback ends, the failures that tripped the breaker are still inside the window, so the first ordinary failure after the probe closes it would re-trip it immediately. That contradicts "the breaker cannot trip on the first error".

**Decision.** The trip query adds `finished_at > greatest(now() - lookback, breaker.updated_at)`. While the breaker is `closed`, `updated_at` is the moment it closed. This relies on nothing else writing the breaker row while it is closed (a comment in the code says so). A unit test checks that one `transient_error` after a probe closes the breaker does not re-trip it.

## D3. The mock provider is a fourth Compose service, `mock-ai`
**Touches:** section 7 (test harness); CLAUDE.md (stack, mock).

An in-process mock can't be switched into outage mode from an admin endpoint once workers are scaled to several containers, and it can't give one call count across them. `mock-ai` is an HTTP service that owns the outage switch, latency and failure settings, and the real/probe call counters. It never logs request bodies. Workers call it over HTTP, so the 30-second client timeout is a real network timeout. Tests drive in-process workers with a scriptable mock for determinism, plus one end-to-end smoke test of a real worker against the `mock-ai` container.

## D4. Redrive endpoints
**Touches:** section 5 ("Failed is terminal; redrive is deliberate"), section 6.7.

The design gives the redrive SQL but no API. Endpoints:

- `POST /admin/jobs/{job_id}/redrive`
- `POST /admin/jobs/redrive` with `{"failed_from": ..., "failed_to": ...}`. The window is over `completed_at`, and the response is `{"redriven": n, "superseded": m}`.

The design's "if zero rows, mark it superseded" has no SQL. It is implemented as a conditional update fenced by `status = 'failed' AND e.current_version > j.version`, which sets `superseded_by_version`. A job that is simply not `failed` is never marked superseded.

## D5. `error_class = 'internal'`
**Touches:** section 4 (failed GET, `error_class` enum), section 8 (non-retryable errors).

`internal` is in the enum, but no design path produces it. It is used only for unexpected exceptions raised by the provider client call, and is treated as transient like every other provider error. Database errors or bugs elsewhere in the worker are not provider failures: they crash the loop, so the lease expires and the job is reclaimed (recorded as `lease_expired` / `worker_lost`).

## D6. Failed GET: "most recent attempt"
**Touches:** section 4 (GET failed).

`error_class` comes from the attempt with the highest `attempt_no` in the job's current redrive generation, excluding `skipped`.

## D7. Duplicate requires an exact match of encounter, version and hash
**Touches:** section 1 (duplicate, payload conflict), section 3 step 3, section 4 (POST examples).

A retried `event_id` can match an event recorded for a *different* encounter or version. Comparing only hashes would call that a duplicate.

**Decision.** On a PK or unique hit, the outcome is `duplicate` only if every matching row has the same `encounter_id`, `version` and `payload_hash` as the incoming event. Anything else is `409 payload_conflict`. For a brand-new encounter (the shell row is about to be rolled back), the response reports `current_version: 0`, meaning "no version stored". Tests cover `event_id` reuse on a different existing encounter and on a brand-new encounter.

## D8. Validation details
**Touches:** section 1 (well-formedness), section 3 ("Before the transaction").

In addition to the design's rules:

- `event_id`, `encounter_id` and `patient_id` must be non-empty strings of at most 255 characters.
- `version` must fit in a Postgres `INTEGER`.
- `payload.transcription` must be a string (not null; empty is allowed).
- The payload hash is SHA-256 of the transcription's UTF-8 bytes.
- Strings containing NUL, or lone surrogates that cannot be encoded as UTF-8, return `400`. Postgres `TEXT` cannot store them, so they would otherwise be a `500`.
- Unknown extra fields in the body or in `payload` are ignored.

Anything else returns `400`.

## D9. Worker concurrency and pool size
**Touches:** section 5 (worker flow), section 7 ("Workers busy vs worker slots").

Each worker process runs `WORKER_CONCURRENCY` independent loops (default 4), each with its own `worker_id`. `WORKER_POLL_SECONDS` (the design's "sleep briefly") defaults to 0.5. Startup fails unless `DB_POOL_SIZE >= WORKER_CONCURRENCY + 1`.

## D10. Where metrics are served
**Touches:** section 5 (SLA detection), section 7 (observability).

Gauges derived from the database (queue depth, SLA breaching counts, `oldest_unfinished_age`, breaker state, `probes_sent`) are computed from the database on each scrape of the API's `/metrics`. This satisfies the design's "scheduled check", with the scrape interval as the schedule. Worker-local counters and histograms are served on each worker's own internal metrics port, which is not published to the host.

## D11. Schema application: verbatim DDL, applied once under an advisory lock
**Touches:** section 2 (schema), section 5 (`circuit_breaker` DDL).

`app/schema.sql` is the design's DDL verbatim, without `IF NOT EXISTS`, so it maps line for line to the document (Postgres 16 has no `CREATE TYPE IF NOT EXISTS` anyway). Idempotence comes from `app/db.py:apply_schema`. In one transaction it takes `pg_advisory_xact_lock`, applies the file only if `encounters` does not exist, and commits. Because the whole file is one transaction, a partial schema can't exist. The api and every worker replica call it at startup, so there is no startup-order dependency.

## D12. The breaker row is seeded at startup
**Touches:** section 5 (circuit breaker).

The design reads and updates the `'generate_summary'` row but never inserts it. Startup runs `INSERT ... ON CONFLICT DO NOTHING` after applying the schema.

## D13. Document locations
**Touches:** CLAUDE.md (source of truth).

`encounter-summaries-design.md` was moved to `docs/design.md`, and the submitted report to `docs/submission.pdf`. The original brief is `docs/BE_ClinicalAI_T_Encounters.pdf`. All three are committed, so a reviewer can check every section reference against the spec.

## D14. Error bodies for 400, 413 and 500
**Touches:** section 3 ("Before the transaction"), section 4 (POST).

The design gives status codes for these but no bodies. They use the same shape as the design's `404` (`{"error": "<fixed code>", ...}`):

- `400`: `{"error": "invalid_request", "detail": "<rule broken>"}`. `detail` names the field and rule, never the submitted value.
- `413`: `{"error": "payload_too_large", "max_bytes": N}`. The declared `Content-Length` is checked first, then the streamed byte count, so a chunked body is also cut off at the limit before parsing.
- `500`: `{"error": "internal"}`. The log line carries only the exception type and path, since exception messages can contain request data. A 5xx tells the partner to retry, which is safe because ingestion is one transaction.

## D15. `conflicting_field` when both identity fields differ
**Touches:** section 4 (identity mismatch body), section 6.8.

The body has room for one field. If both `patient_id` and `encounter_type` contradict the stored values, it reports `patient_id`. The log line records both pairs.

## D16. `Location` header percent-encodes the encounter ID
**Touches:** section 4 (accepted).

`Location: /encounters/{encounter_id}/summary`, with the ID percent-encoded (`quote(id, safe="")`) so IDs containing `/`, spaces or `?` still produce a valid path.

Starlette decodes `%2F` to `/` before routing, so a plain `{encounter_id}` segment would not match an ID containing `/`. The GET route is declared as `/encounters/{encounter_id:path}/summary`. `tests/test_read.py::test_get_via_location_header_with_encoded_id` checks the round trip: it POSTs an event with encounter ID `enc/1 x?`, then GETs the returned `Location`.

## D17. Test hooks and crash injection
**Touches:** section 7 (test harness).

`app/hooks.py` defines named points in ingestion (`ingest.after_lock`, `ingest.before_commit`, `ingest.after_commit`) and in the worker (`worker.after_claim`, `worker.before_call`, `worker.after_call`). Tests register in-process callbacks on them, which is how the lock-hold hook and in-process failure injection work. Setting `CRASH_AT=<point>` kills the process there with `os._exit`. Both are inert unless `TEST_HOOKS` is set: `fire()` returns before looking at callbacks or `CRASH_AT`.

## D18. An encounter whose current version has no job returns 404
**Touches:** section 4 (unknown encounter).

Section 4 says this case is impossible, given single-transaction ingestion, but "has a defined response rather than a crash", without naming the response. It returns the same `404 encounter_not_found` body, the only defined response in that section, and logs `current_version_without_job` with the encounter ID so the inconsistency is visible. A `superseded` job at `current_version` would contradict the guarded write and pre-call check, so it is treated as a bug and returns `500 internal`.

## D19. Timestamp format in responses
**Touches:** section 4 (GET examples).

`accepted_at` and `completed_at` are RFC 3339 in UTC with a `Z` suffix, truncated to whole seconds (`2026-09-18T10:04:11Z`), matching the design's examples. `sla_breached` is computed in SQL from full-precision timestamps and Postgres `now()`, so truncation never affects it.

## D20. Small SQL changes to the worker statements
**Touches:** section 5 (claiming, guarded write).

- **Guarded write:** the `CASE ... THEN 'ready' ELSE 'superseded' END` is cast to `::job_status`. Postgres types a `CASE` of string literals as `text`, which can't be assigned to an enum column. `RETURNING j.status::text` returns a plain string.
- **Claim:** the lease is `now() + make_interval(secs => :lease_seconds)` instead of the literal `interval '60 seconds'`, so `LEASE_SECONDS` configures it. The default is still 60.
- **Transient-error path:** the `queued`/`failed` `CASE` gets the same `::job_status` cast. The budget and backoff are parameters (`RETRY_BUDGET`, and `next_attempt_at = now() + make_interval(secs => :backoff_seconds)`) instead of the literal `5` and `:backoff`. The reclaim budget check in the claim compares against `RETRY_BUDGET` too.

Everything else matches the design's text.

## D21. How the 30-second client timeout is enforced
**Touches:** section 0 (assumptions), section 5 (lease > AI timeout).

The lease > AI timeout rule depends on the timeout bounding the *whole* call, so `HttpSummaryClient` enforces it in two layers:

- **A total deadline:** the request runs under `asyncio.timeout(AI_TIMEOUT_SECONDS)`. When the deadline passes, the request is cancelled and the connection dropped, wherever it is: connecting, sending, waiting for headers, or reading the body.
- **httpx's per-phase timeouts,** at the same value, underneath.

Tests use real sockets. A server that hangs, one that sends the body a byte at a time, and one that sends the status line and headers a byte at a time each time out at the deadline. Every byte arrives well within the per-phase timeout, so only the total deadline can stop them. Each call uses its own short-lived client, so nothing is held between calls.

Status mapping: 429 → `rate_limited`, 503 and other 5xx → `ai_unavailable`, 504 or the client deadline → `ai_timeout`, a connection failure → `ai_unavailable`. Any other status, a malformed body, or an unexpected exception from the call → `internal` (D5). The provider's error text is discarded, and the original exception is suppressed from tracebacks.

## D22. The mock-ai interface
**Touches:** section 7 (test harness); CLAUDE.md (mock).

- **`POST /generate_summary`** takes `{"transcription": ...}` and returns `{"summary": ...}`.
  - Latency is 2–8 s, with a share of slow calls at 12 s.
  - Failures are split evenly across 429, 503 and a hang past the worker's 30 s timeout.
  - Outage mode returns 503 for every call.
  - The wording varies per call and uses only the transcript's word count, never its content.
- **Admin endpoints:** `GET/POST /admin/settings` for partial updates, e.g. `{"outage": true}`, plus `GET /admin/stats` and `POST /admin/reset`.
- **Configuration:** defaults come from `MOCK_*` environment variables.
- **Probe detection:** a call is counted as a probe when its transcription equals the shared `PROBE_TRANSCRIPT` constant. The real provider would get no flag either.

## D23. A crashed worker loop drains, then stops the worker process
**Touches:** section 5 (worker flow), section 6.4.

Under D5, a non-provider error (a database error, a bug) crashes its loop. The process then:

1. Stops claiming: no loop takes a new job.
2. Gives loops with a call in flight up to `AI_TIMEOUT_SECONDS` + 5 s to finish it and write the result. That call may already be paid for, so killing it would throw the result away. The call itself is bounded by `AI_TIMEOUT_SECONDS` (D21), and the 5 s margin covers the result-write transaction that follows.
3. Exits non-zero. Compose restarts it (`restart: unless-stopped`).

Loops still running after the window are abandoned, and their jobs are reclaimed when their leases expire, as the design describes for a crashed worker. The crashed loop's own job is reclaimed the same way.

SIGTERM uses the same drain and exits zero if it completes. The worker's `stop_grace_period` is 40 s so `docker compose stop` doesn't cut the drain short. The log line records only the exception type. `app/worker/main.py:run_loops` implements it, and `tests/test_worker_process.py` covers the crash drain, the bounded drain, SIGTERM, and idle shutdown.

## D24. The breaker trip check locks the breaker row before counting
**Touches:** section 5 ("Tripping"), section 7 test 5.

The design's trip check is a plain `SELECT` of recent failures inside the transaction that records a `transient_error`. Under concurrency it undercounts. Each worker's count sees only *committed* failures plus its own, so when several workers record failures at the same moment, several can each count 10 and none trips at 11. The repeated outage test showed it: with 4 workers, real calls reached 15–16, against the design's "near 11 plus the calls already in flight (up to 4)".

**Decision.** Before counting, the trip check takes `SELECT state FROM circuit_breaker WHERE name = 'generate_summary' FOR UPDATE`. That serializes concurrent trip checks: each count statement runs after the previous failure-recording transaction has committed, so it sees every failure. If the state isn't `closed`, it returns straight away, since the trip `UPDATE` could match nothing.

- **No deadlocks:** failure-recording transactions lock the breaker row last, and the probe transactions lock only the breaker row.
- **D2 still holds:** a row lock isn't a write, so `updated_at` doesn't move.
- **Cost:** the lock is held only while a failure is being recorded, which is rare outside an outage and brief during one.

The count query itself is unchanged apart from D2's cutoff. After the change, the outage test stayed within 11 + 3 in-flight calls on 25 of 25 repeated runs.

## D25. The worker probes only when the claim found the breaker not closed
**Touches:** section 5 ("Worker flow").

The design's loop calls `try_probe_if_cooldown_passed()` whenever the claim returns no job. The worker runs it only when the claim's step 0 found the breaker not `closed`. When the breaker is closed, the probe race's `WHERE` (open with the cooldown passed, or half-open with the deadline passed) can never match, so skipping it changes nothing and saves a statement on every idle poll. The claim returns a distinct `BREAKER_NOT_CLOSED` value for this case. A non-provider exception during a probe call crashes the loop like any other (D5, D23). The breaker stays `half_open` until `probe_deadline` passes, and then another worker probes.

The lookback is passed as `make_interval(secs => BREAKER_LOOKBACK_MINUTES * 60)`, since `make_interval`'s `mins` argument only takes integers. The cooldown and probe deadline are parameters too.

## D26. Redrive endpoint details
**Touches:** section 5 ("Failed is terminal; redrive is deliberate"), section 6.7; extends D4.

**`POST /admin/jobs/{job_id}/redrive`**
- `200 {"job_id": N, "outcome": "redriven"}`: the job was `failed` and still current. It is now `queued`, due immediately, with `redrive_generation + 1` (a fresh budget) and `completed_at` cleared. `attempts`, the fencing token, is untouched.
- `200 {"job_id": N, "outcome": "superseded", "superseded_by_version": V}`: the job was `failed` but its encounter has since moved on to version V. The job keeps its original `completed_at`.
- `404 {"error": "job_not_found", "job_id": N}`.
- `409 {"error": "job_not_failed", "job_id": N, "status": "<status>"}`: redrive applies only to `failed` jobs. A second, concurrent redrive of the same job gets this, because the row lock makes the conditional updates see it as `queued`.
- `400 invalid_request`: `job_id` is not a positive 64-bit integer.

**`POST /admin/jobs/redrive`** with `{"failed_from": ..., "failed_to": ...}`
- The window is half-open, `[failed_from, failed_to)`, over `completed_at`.
- Both bounds must be ISO 8601 timestamps with a timezone, and `failed_from` must be earlier. A timestamp without a timezone is rejected, not guessed.
- The redrive and supersede updates run set-based, in one transaction, and return `{"redriven": n, "superseded": m}`. Running it again over the same window returns zeros.
- The IDs of the jobs affected are logged. The response carries only counts.

Job status is operational metadata and carries no patient content. Like every endpoint, these have no authentication (section 8). In a real deployment they would be operator-only.

## D27. Metrics and logs in detail
**Touches:** section 5 ("SLA detection"), section 7 ("Observability", "No patient content anywhere in the telemetry"); extends D10.

**API `/metrics`, per process:**
- `ingest_events_total{outcome}`: the five ingestion outcomes, plus `invalid_request` and `payload_too_large`.
- `ingest_duplicate_source_defects_total`.
- `ingest_identity_conflicts_total`: the dedicated metric from section 6.8.
- `ingest_payload_conflicts_total`.

**API `/metrics`, read from Postgres on each scrape** (`app/metrics.py:DatabaseCollector`):
- `summary_queue_depth{state=due_now|waiting_backoff|processing}`.
- The section 5 SLA check, verbatim: `summary_sla_breaching_jobs`, `_queued` and `_processing`, and `summary_sla_oldest_unfinished_age_seconds`.
  - Following the design's SQL, the age is taken over breaching jobs only, so it is 0 when none breach.
  - The scrape interval is the design's "every few seconds".
- `summary_jobs{status}`: the failed count backs the "any job reaches failed" alert.
- `breaker_state{state}`, `breaker_open_seconds` (for the "breaker open for more than a few minutes" alert), and `breaker_probes_sent_total`.
- `summary_db_up`: if the database can't be read, the scrape still succeeds with `summary_db_up 0`.

`summary_jobs{status}` counts the whole table on every scrape. That's fine at this scale. At high volume, a partial index or a sampled count would replace it.

**Worker `:WORKER_METRICS_PORT` (9100), per process, never published to the host:**
- `generate_summary_calls_total{kind=real|probe, result}` and `generate_summary_call_seconds{kind}`: spend rate and latency.
- `job_attempts_closed_total{outcome, error_class}`: counts transitions, so an attempt closed as `lease_expired` by the reclaimer and later rewritten to `discarded` (D1) counts once for each.
- `summary_jobs_failed_total{path=transient|reclaim}` and `breaker_trips_total`.
- `breaker_probes_total{result=succeeded|failed|fenced}`.
- `worker_slots` and `worker_busy_slots`.
- For jobs reaching `ready`: `summary_queue_wait_seconds`, `summary_processing_seconds` and `summary_completion_seconds`. These three durations come from the result write's `RETURNING`, so they use database timestamps (the guarded write's `RETURNING` gains these three expressions; its `WHERE` and `SET` are unchanged). Call latency is a local duration, not a time comparison.

With `--scale worker=N`, each replica is scraped separately: Docker's DNS returns every replica for the `worker` service name.

Labels are fixed enum values only, with no IDs.

**Logs:** one JSON object per line, from every service and from third-party loggers (uvicorn is routed through the same formatter). Each line has `ts`, `level`, `service`, `logger`, and `event` (the message), plus the structured fields.
- **Exceptions:** only the exception type (`error_type`) is written. The message and traceback never are, because exception text can carry request data. This covers uvicorn's own "Exception in ASGI application".
- **httpx:** its per-request INFO lines are silenced.
- **Tests:** `tests/test_observability.py` drives every ingestion outcome, a 500 whose exception message *is* the transcript, a transient error and a success, with a distinctive transcript and summary. It checks that neither appears in any log line, and that patient IDs appear only in the `identity_conflict` line.
