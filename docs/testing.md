# Testing

How to run the suite, what each file covers, and how the harness forces the races and crashes. The seven headline tests are summarised in the [README](../README.md#tests), and how they're built is recorded in [D28](../DECISIONS.md).

## Running

```sh
make test
# without make:
docker compose up -d --build --wait db mock-ai
docker compose run --rm --build api python -m pytest
```

180 tests, all against a real Postgres: a separate `outcomes_test` database on the Compose `db`, never SQLite or a fake. The guarantees live in Postgres constraints and locks, so a fake would pass every concurrency test while proving nothing. A full run takes about 2 minutes. The suite passed three consecutive runs at every milestone, and the headline file passed seven consecutive runs on its own.

`make test` starts `mock-ai` first, because one smoke test runs a real worker process against it.

## Coverage by file

| File | Covers |
|---|---|
| [`test_headline.py`](../tests/test_headline.py) | The seven headline tests from design §7 |
| [`test_ingest.py`](../tests/test_ingest.py) | Every POST outcome with its exact body and stored state, 20 malformed inputs, the size limit (exact limit, one byte over, chunked), rollback, the test hooks being inert |
| [`test_read.py`](../tests/test_read.py) | Processing, ready, failed and 404 bodies; `sla_breached` either side of 10 s; `attempts`/`error_class` across redrive generations; older summaries never resurfacing |
| [`test_worker.py`](../tests/test_worker.py) | Happy path, oldest-first, what is never claimable, 6 workers draining 30 jobs, pre-call skip, in-flight supersede, stalled worker discarded |
| [`test_failures.py`](../tests/test_failures.py) | Transient errors, backoff doubling (10/20/40/80 s), the fifth failure, a poison job failing on reclaim, a worker bug crashing its loop |
| [`test_breaker.py`](../tests/test_breaker.py) | Tripping on exactly the 11th failure, the lookback, which outcomes count, the claim gate, the probe race (8 workers → 1 probe), the D2 recovery case, a small outage end to end |
| [`test_redrive.py`](../tests/test_redrive.py) | Single and bulk redrive, superseding obsolete jobs, a fresh budget, concurrent redrives, fencing across a redrive |
| [`test_summary_client.py`](../tests/test_summary_client.py) | Error-class mapping; the total deadline against real hanging, body-trickling and header-trickling servers |
| [`test_worker_process.py`](../tests/test_worker_process.py) | Draining in-flight calls after a crash or SIGTERM, and the bounded drain |
| [`test_observability.py`](../tests/test_observability.py) | Every metric's value, gauges read on scrape, and no patient content in metrics or logs |
| [`test_smoke_e2e.py`](../tests/test_smoke_e2e.py) | A real worker process against the real mock-ai container, including its metrics port |
| `test_schema.py`, `test_config.py`, `test_health.py`, `test_mockai.py` | Schema applied once under concurrency, config validation, health checks, mock-ai behaviour |

## The harness

The pieces live in [`tests/harness.py`](../tests/harness.py), with the hook points in [`app/hooks.py`](../app/hooks.py).

- **`LockHold`** is registered on the `ingest.after_lock` hook. The first transaction to take the encounter row lock holds it until `pg_stat_activity` shows another backend waiting on a lock, so the second request is guaranteed to arrive mid-transaction. It records whether that happened (`forced`), and every iteration asserts it, so an iteration that failed to interleave can't pass silently.
- **`released_together`** sends requests through separate app instances (separate connection pools, standing in for separate service instances) from one barrier.
- **`Sampler`** polls GET in a background thread throughout a scenario, so tests can assert on everything a client could have seen, not just the end state.
- **`ApiProcess`** runs a real uvicorn process against the test database. With `CRASH_AT` set to a named hook point (`ingest.before_commit`, `ingest.after_commit`), the process kills itself there with `os._exit`, so the partner gets no response and Postgres sees a dropped connection.
- **Scriptable mock.** [`app/summary/mock.py`](../app/summary/mock.py) counts real and probe calls separately, and can be scripted per call: succeed, fail with a chosen error class, or hold on a `Gate` until released (used to stall a worker past its lease).
- **Compressed time.** Leases, backoff, cooldowns and the SLA are shrunk through configuration, so a 20-minute outage runs in about 3 seconds with unchanged logic.

All hooks, including `CRASH_AT`, do nothing unless `TEST_HOOKS` is set; a test checks this.

Assertions are made on stored state, HTTP responses and mock call counts, never on log text. The one exception is the log-privacy test, which asserts that content is *absent* from logs.

## Caveat: shared mock-ai counters

`mock-ai` is shared with the dev stack, so the end-to-end smoke test compares its call counts before and after. If the dev stack's workers are busy while it runs (for example while [`scripts/scenario_ai_outage.sh`](../scripts/scenario_ai_outage.sh) runs), the counts can be disturbed. Let the scenario finish, or stop the dev workers, before running `make test`.
