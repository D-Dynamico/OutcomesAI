# Encounter summaries

A deployable implementation of [`docs/design.md`](docs/design.md): versioned clinical encounter updates are ingested exactly once, the latest state is stored, and an AI summary is generated in the background through a paid, non-idempotent, sometimes-failing `generate_summary` call.

> Work in progress. This README is completed in the final milestone. Deviations from the design are listed in [`DECISIONS.md`](DECISIONS.md).

## Services

| Service | Role | Host port |
|---|---|---|
| `db` | Postgres 16. All state, including the job queue and the circuit breaker. | none |
| `api` | FastAPI: ingestion, reads, admin redrive, `/healthz`, `/metrics`. | 8000 |
| `worker` | Claims and processes summary jobs. Scales with `--scale worker=N`. | none |
| `mock-ai` | Mock `generate_summary` provider. Owns the outage switch, latency and failure settings, and real/probe call counts. Never logs request bodies. | 8001 |

## Run

```sh
docker compose up --build          # or: make up
curl localhost:8000/healthz        # {"status":"ok"}
docker compose up -d --scale worker=3
```

The schema is applied automatically at startup. No manual steps are needed.

## Test

```sh
make test
# without make:
docker compose run --rm --build api python -m pytest
```

The tests run inside the app image against a separate `outcomes_test` database on the Compose Postgres. They never use SQLite or an in-memory fake.

## Observability note

The design's scheduled SLA check (section 5) is implemented as gauges computed from the database on each scrape of `/metrics`. The scrape interval is the schedule. See `DECISIONS.md` D10.
