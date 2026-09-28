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

`encounter-summaries-design.md` was moved to `docs/design.md`, and the submitted report to `docs/submission.pdf`. The original brief is `docs/BE_ClinicalAI_T_Encounters.pdf`. `docs/design.md` is git-ignored, so a fresh clone has only the brief and the submitted report.

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

## D17. Test hooks and crash injection
**Touches:** section 7 (test harness).

`app/hooks.py` defines named points in ingestion: `ingest.after_lock`, `ingest.before_commit` and `ingest.after_commit`. Tests register in-process callbacks on them, which is how the lock-hold hook and in-process failure injection work. Setting `CRASH_AT=<point>` kills the process there with `os._exit`. Both are inert unless `TEST_HOOKS` is set: `fire()` returns before looking at callbacks or `CRASH_AT`.
