# Guarantees, mechanisms and code

How each guarantee in the design is enforced, which test proves it, and where each part of the design lives in the code. For the overview, see the [README](../README.md).

## Guarantee → mechanism → test

Each row is a guarantee from the design, the mechanism that enforces it, and the test that would fail without it. Headline tests 1–7 are in [`tests/test_headline.py`](../tests/test_headline.py).

| Guarantee | Enforced by | Proven by |
|---|---|---|
| Ingestion commits all of it or none of it | One transaction in `ingest()`: every outcome except accepted rolls back | headline test 4 (process killed before and after COMMIT) · `test_failure_before_commit_rolls_back_everything` |
| Duplicates are decided by constraints, never by a read first | `INSERT … ON CONFLICT DO NOTHING` on the `event_id` primary key and `(encounter_id, version)` | headline test 1 (200 forced races × 2) |
| The version never regresses | `UPDATE … WHERE current_version < :version` (compare-and-swap) | headline test 2 |
| First patient wins; the 409 echoes no patient ID | Identity read back under `FOR UPDATE` | headline test 3 |
| `generate_summary` never runs inside a transaction | Three short transactions around the call | `test_no_transaction_or_lock_held_during_the_call` (checks for open transactions and row locks mid-call) |
| Spend is recorded before it happens | The attempt row is committed at claim, and `probes_sent` is committed before the probe call | same test · headline test 5 (`probes_sent` = probe calls) |
| Every worker write is fenced | `attempts = :my_attempt` (and `status = 'processing'`) on every worker write, `probe_generation` on probe reports | headline test 7 · `test_stalled_prober_cannot_overwrite_a_newer_verdict` · `test_worker_from_before_the_failure_cannot_write_after_redrive` |
| An older summary never appears as current | GET joins through `current_version`, and the result write labels obsolete results `superseded` | headline tests 2 and 6 (GET sampled throughout) |
| An outage costs a bounded number of calls, whatever the queue depth | Shared breaker; trip check serialized by a row lock ([D24](../DECISIONS.md)) | headline test 5 · `test_breaker.py` |
| All time comparisons use Postgres `now()` | Leases, backoff, cooldowns and SLA are all computed in SQL | `test_processing_sla_breach_uses_database_clock` · headline test 7 (real lease expiry) |
| No patient content in logs, metrics or error bodies | Fixed-enum errors; the JSON log formatter reduces exceptions to their type | `test_logs_never_carry_patient_content` (a 500 whose exception message *is* the transcript) · `test_metrics_carry_no_patient_content` |
| Response bodies match design §4 exactly | Built in one place per outcome | exact-body tests in `test_ingest.py` and `test_read.py` |

## Design section → code

| Design section | Code |
|---|---|
| §2 Data model, schema | [`app/schema.sql`](../app/schema.sql) (verbatim DDL) · [`app/db.py`](../app/db.py) (applied once, under an advisory lock) |
| §1 Ingestion outcomes · §3 Ingestion logic | [`app/api/ingest.py`](../app/api/ingest.py): validation, then the single transaction, step by step |
| §4 API contract (GET) | [`app/api/read.py`](../app/api/read.py): one statement, resolved through `current_version`, `sla_breached` in SQL |
| §5 Claiming a job | [`app/worker/claim.py`](../app/worker/claim.py): breaker gate, `SKIP LOCKED`, lease, close abandoned attempt, reclaim budget check, open attempt |
| §5 Pre-call check | [`app/worker/precall.py`](../app/worker/precall.py) |
| §5 Worker flow, transaction boundaries | [`app/worker/main.py`](../app/worker/main.py): `Worker.run_once`, with the call outside every transaction; `run_loops` drain (D23) |
| §5 Guarded write · after a transient error · backoff | [`app/worker/result.py`](../app/worker/result.py) |
| §5 Circuit breaker (trip, gate, probe race, report) | [`app/worker/breaker.py`](../app/worker/breaker.py) |
| §5 Failed is terminal; redrive | [`app/api/admin.py`](../app/api/admin.py) |
| §5 SLA detection (scheduled check) · §7 Observability | [`app/metrics.py`](../app/metrics.py) (`DatabaseCollector` runs the SLA query on scrape) · [`app/logs.py`](../app/logs.py) |
| `generate_summary` contract (brief) | [`app/summary/client.py`](../app/summary/client.py) (HTTP client, total deadline, fixed error classes) · [`app/summary/mock.py`](../app/summary/mock.py) (scriptable test mock) · [`app/mockai/main.py`](../app/mockai/main.py) (mock-ai service) |
| §6 Failure scenarios | 6.1: test 1 · 6.2: `test_ingest.py` (gap, stale) · 6.3: test 4 · 6.4: test 7, `test_failures.py` (poison job) · 6.5: test 2, `test_worker.py` · 6.6: test 6 · 6.7: test 5, `test_breaker.py`, `test_redrive.py` · 6.8: test 3, `test_ingest.py` |
| §7 Test harness | [`tests/harness.py`](../tests/harness.py) · [`app/hooks.py`](../app/hooks.py) (named hook points, `CRASH_AT`) |
| Tunables | [`app/config.py`](../app/config.py) |

## Directory layout

```
app/
  config.py            all tunables, design defaults, startup validation
  db.py                pool, transaction helper, schema applied once
  schema.sql           the design's DDL, verbatim
  hooks.py             test-only named hook points and CRASH_AT (inert by default)
  logs.py              JSON logs; exceptions reduced to their type
  metrics.py           Prometheus metrics; database gauges computed on scrape
  api/
    main.py            routes, size limit, /metrics, /healthz
    ingest.py          POST: validation and the single transaction
    read.py            GET: resolved through current_version
    admin.py           redrive, single job and time window
  worker/
    main.py            the worker loop, probing, drain on crash or SIGTERM
    claim.py           txn 1: breaker gate, SKIP LOCKED, lease, reclaim, budget
    precall.py         txn 2: supersede obsolete work before paying
    result.py          txn 3: guarded write, discarded, transient path, backoff
    breaker.py         trip check, probe race, probe report
  summary/
    client.py          HTTP provider client, total deadline, fixed error classes
    mock.py            scriptable in-process mock for tests
  mockai/main.py       the mock-ai service
tests/                 harness.py, factories.py, conftest.py, test_*.py
docs/                  design.md (the spec), the submitted PDF, these docs
```
