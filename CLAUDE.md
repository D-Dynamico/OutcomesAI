# CLAUDE.md

## What this project is

A working, deployable implementation of a backend design I submitted for a Backend Engineer / Clinical AI take-home. The reviewer has asked for code that is "fully deployable and ready for testing", so the bar is: `docker compose up` works on a clean machine, the tests pass against real Postgres, and the behaviour matches the design document.

The service ingests versioned clinical encounter updates from a partner (duplicates, out-of-order, concurrent, crashes), stores the latest state, and generates an AI summary in the background through a paid, non-idempotent, sometimes-failing `generate_summary` call.

## Source of truth

- `docs/design.md` is the spec. Read it fully before writing code. It has the exact schema, SQL for every state transition, the API contract with example bodies, and the reasoning behind each decision.
- `docs/brief.pdf` is the original exercise, for context only (not currently in the repo).
- The submitted PDF (`docs/submission.pdf`) is a condensed version of `docs/design.md`. Where they differ, **`docs/design.md` wins**. Known differences: the PDF's DDL omits `payload_hash` and the `circuit_breaker` table, and its scenario G omits the breaker's 10-minute window. Implement the md.

If the design is ambiguous, contradictory, or missing something you need, do not improvise silently. Pick the option most consistent with the rest of the design, implement it, and record it in `DECISIONS.md` (what, why, which section of the design it touches). If the choice would change the API contract or a correctness guarantee, stop and ask me instead.

## Stack

- Python 3.12, FastAPI, Uvicorn
- Postgres 16, accessed with **psycopg 3 and raw SQL**. No ORM. The design's SQL is the implementation; keep it recognisable so a reviewer can map code to the document section by section.
- Schema applied from a plain `.sql` migration file at startup (idempotent). No Alembic.
- pytest, run against a real Postgres (the Compose service or a testcontainer). Never SQLite, never an in-memory fake: the guarantees live in Postgres constraints and locks.
- Docker Compose with four services: `db`, `api`, `worker`, `mock-ai` (the mock `generate_summary` provider, see DECISIONS.md D3). The worker must work when scaled (`docker compose up --scale worker=3`).

## Suggested layout

```
app/
  config.py          # all tunables from env (see below)
  db.py              # connection pool, transaction helper
  schema.sql         # exact DDL from design section 2 and the breaker table
  api/
    main.py          # FastAPI app, routes
    ingest.py        # POST logic, design section 3
    read.py          # GET logic, design section 4
    admin.py         # redrive endpoint
  worker/
    main.py          # loop from design section 5 "Worker flow"
    claim.py
    precall.py
    result.py        # guarded write, transient-error path
    breaker.py       # trip check, probe race, probe report
    sla.py           # scheduled breach check, publishes gauges
  summary/
    client.py        # generate_summary interface
    mock.py          # scriptable in-process mock for tests
  mockai/
    main.py          # mock-ai HTTP service: outage switch, latency/failures, call counts
  metrics.py         # Prometheus counters/gauges/histograms
tests/
  conftest.py
  harness.py         # barrier, lock-hold hook, crash injection, scripted mock
  test_*.py
docker-compose.yml
Dockerfile
Makefile
README.md
DECISIONS.md
```

Adjust if something else is cleaner, but keep ingestion, worker, and breaker logic in clearly separate modules.

## Non-negotiables (these are what the reviewer will check)

1. **Ingestion is one transaction**: encounter upsert and `FOR UPDATE`, identity check, event insert with payload hash, conditional version update, job insert. Commit or roll back together.
2. **Duplicates are decided by constraints**, never by a "does it exist?" read first.
3. **The version update is a compare-and-swap** (`WHERE current_version < :version`), not read-then-write.
4. **`generate_summary` is never called inside a transaction.** No connection or lock is held during the call.
5. **Every worker write is fenced** by `attempts = :my_attempt` (and `status = 'processing'` where the design says so).
6. **The attempt row is committed before any paid call.** Probes increment `probes_sent` before the call.
7. **GET resolves the summary through `current_version`.** There is no stored pointer to the current job.
8. **All time comparisons use Postgres `now()`**, never the worker's clock.
9. **No patient content in logs, metrics, or error responses**: no transcripts, no summaries, no raw provider errors. `patient_id` appears only in the identity-conflict log line. Error bodies use the fixed `error_class` enum.
10. **Response bodies and status codes match design section 4 exactly**, including field names (`outcome` on POST, `status` on GET) and the rule that queued jobs are reported as `processing`.

## The mock `generate_summary`

The real provider is out of scope. Build a mock that behaves like the brief describes, configurable by env for manual runs and scriptable per-call in tests:

- Latency: configurable distribution (default 2 to 8 seconds, occasional calls over 10).
- Failures: configurable rate of `ai_timeout`, `rate_limited`, `ai_unavailable`.
- Outage mode: fail everything until switched off (an admin endpoint or env flag), so the 20-minute outage scenario can be demoed.
- Non-idempotent: output wording varies between calls on the same input.
- Counts every call and records whether the input was a real transcript or the synthetic probe.
- Output must not be derived from real content in a way that ends up in logs.
- Runs as the `mock-ai` Compose service (DECISIONS.md D3), which owns the outage switch, latency and failure settings, and the real/probe call counts. It must never log request bodies.

The worker enforces its own 30-second client-side timeout regardless of the mock.

Tests use in-process workers with a scriptable in-process mock for determinism, plus one smoke test that runs a real worker against the `mock-ai` container end to end.

## Configuration (env, with design defaults)

`LEASE_SECONDS=60`, `AI_TIMEOUT_SECONDS=30`, `RETRY_BUDGET=5`, `BACKOFF_BASE_SECONDS=10`, `BREAKER_WINDOW=20`, `BREAKER_THRESHOLD=11`, `BREAKER_LOOKBACK_MINUTES=10`, `BREAKER_COOLDOWN_SECONDS=30`, `PROBE_DEADLINE_SECONDS=60`, `SLA_SECONDS=10`, `MAX_BODY_BYTES=1048576`, `WORKER_POLL_SECONDS`, plus mock settings. Tests shrink the durations so an outage runs in seconds without changing any logic.

## Build order

Work in milestones. At the end of each: tests for that milestone pass, `make test` is green, and you commit with a clear message. Do not start the next milestone with failing tests.

1. **Scaffold.** Compose, Dockerfile, Makefile (`up`, `down`, `test`, `logs`), config, DB pool, `schema.sql` applied on startup, `/healthz`. Check: `docker compose up` on a clean checkout gives a healthy API and the full schema.
2. **Ingestion (POST).** Size limit (413), validation (400), then the transaction in design section 3 for accepted, duplicate, payload conflict, stale, identity conflict. Check: unit and integration tests for each outcome and its exact body.
3. **Read (GET).** Resolve via `current_version`; processing, ready, failed, 404; computed `sla_breached`; `attempts` and `error_class` derived from `job_attempts` for failed jobs.
4. **Worker happy path.** Claim with `SKIP LOCKED` and lease, pre-call supersede check, call outside a transaction, guarded write, attempt close-out, `discarded` on lost ownership.
5. **Failure handling.** Transient-error path with budget and jittered backoff, reclaim of expired leases with `lease_expired` and the reclaim budget check, `failed` at budget exhaustion.
6. **Circuit breaker.** Trip check (11 of last 20 within 10 minutes), claim gate, probe race with `probe_generation` fencing, synthetic probe, close or reopen.
7. **Redrive.** Admin endpoint: redrive only if still current (new `redrive_generation`), otherwise mark `superseded`. Supports a single job and bulk by time window.
8. **Observability.** `/metrics` (Prometheus) with the metrics in design section 7, the scheduled SLA check publishing its gauges, structured JSON logs that obey non-negotiable 9.
9. **The test suite** (below).
10. **README and demo.** Run instructions, curl examples for every outcome, how to trigger and watch an outage, how to redrive, how the code maps to design sections.

## Tests

Implement the five tests in design section 7 plus the two listed under "Not covered by these five", so seven in total:

1. Concurrent duplicate accepted exactly once (also on a brand-new encounter, no orphan shell row)
2. Version never regresses; v12 never shown as current
3. Identity race on a brand-new encounter (409 body contains no patient ID)
4. Crash before and after the ingestion commit loses no work
5. AI outage: bounded paid calls, probes synthetic only, no job fails, all reach ready
6. Forced results out of order
7. Stalled worker is fenced (`lease_expired` then `succeeded`, late write `discarded`)

Harness requirements are in design section 7: release concurrent requests from a barrier, a hook that holds a transaction after it takes the row lock, crash injection at named points (a test-only hook that kills the process, for example `os._exit` in a subprocess), and a scriptable mock. Assert on database state, HTTP responses, and mock call counts, never on log text. Concurrency tests loop many iterations (a fresh `encounter_id` each time) and must be reliable, not flaky. Test-only hooks must be inert unless a test env flag is set.

Also add ordinary unit tests as you go; the seven above are the headline set.

## Working rules

- Read the relevant design section before implementing each part, and reference it in a short comment at the top of the module (for example `# Design section 5: claiming a job`).
- Keep SQL close to the design's text. If you must change a statement, note why in `DECISIONS.md`.
- No features beyond the design: no auth, no UI, no external queue, no ORM.
- Run the tests yourself before saying a milestone is done, and report what you ran and the result. If something fails, fix it or tell me plainly; do not mark it done.
- Keep commits small and per milestone.

## Definition of done

- `git clone` then `docker compose up --build` works with no manual steps.
- `make test` passes, including all seven headline tests, with no flakes across three consecutive runs.
- README explains setup, the API with examples, the outage demo, redrive, and a section-by-section map from design to code.
- `DECISIONS.md` lists every place the implementation had to interpret or deviate from the design.
