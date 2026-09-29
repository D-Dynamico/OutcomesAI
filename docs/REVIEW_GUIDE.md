# Review guide

A hands-on walkthrough for evaluating this service at a terminal. Each step gives the command, the output to expect, and one line on what it proves. The brief's eight failure scenarios, plus payload conflict, each run as one script that prints the HTTP responses and the stored state.

**Every output block below was captured from a real run** on a fresh stack (`docker compose down -v` then `docker compose up --build`), in the order this guide gives. Some values differ between runs:

- timestamps, `job_id`s and worker IDs;
- summary wording and its `Ref N`, because the mock provider is deliberately non-idempotent, as the brief's is;
- the few timing-dependent counts that are flagged where they appear.

Everything else, including status codes, outcomes, versions, job statuses and attempt outcomes, should match exactly.

**Contents:** [1. Before you start](#1-before-you-start) · [2. Start and verify](#2-start-and-verify) · [3. The failure scenarios](#3-the-failure-scenarios) · [4. Running the tests](#4-running-the-tests) · [5. Looking inside](#5-looking-inside) · [6. Troubleshooting](#6-troubleshooting) · [7. Requirement map](#7-requirement-map)

## 1. Before you start

**Prerequisites.** You need Docker with Compose v2.20 or later, plus bash and curl. Nothing else is needed on the host: no Python, no jq, no `make` (the [`Makefile`](../Makefile) only wraps plain commands).

- **Windows:** run everything in **Git Bash or WSL**, not PowerShell.
- **Where to run from:** all commands run from the repository root.

**Free ports.** The stack publishes two ports:

- `8000` for the API;
- `8001` for mock-ai, so the scripts can flip its outage switch.

Check that they're free with `lsof -i :8000 -i :8001` (macOS or Linux) or `netstat -ano | grep -E ':800[01] '` (Git Bash). To use other ports, see [Troubleshooting](#port-already-in-use).

**How long it takes.** These times were measured on the captured run:

| Path | Running commands | With reading |
|---|---|---|
| **5-minute essentials:** steps [2](#2-start-and-verify), scenarios [1](#scenario-1-concurrent-duplicate), [2](#scenario-2-missing-or-late-version), [5](#scenario-5-newer-version-during-processing), [6](#scenario-6-results-out-of-order), [8](#scenario-8-inconsistent-identity-fields) and [9](#scenario-9-extra-payload-conflict), then the [headline tests](#headline-tests-only) | about 2 min | about 5 minutes |
| **Full guide:** everything, including the three slow scenarios (worker interruption, outage, retry exhaustion) and the full suite | about 10 min | about 25 minutes |

The first `docker compose up --build` took 16 s here, but the two base images (`python:3.12-slim`, `postgres:16-alpine`) and Docker's build cache were already present. On a clean machine, it also downloads the images and installs the Python packages, which takes longer depending on your network.

**Reset.** This returns everything to a clean state: it empties the database and restarts mock-ai with its default settings and zeroed counters.

```sh
docker compose down -v && docker compose up --build -d --wait
```

The scenarios don't need a reset between them. Each one uses its own IDs, so they run in any order. Running the same scenario a second time refuses to reuse its IDs: pass `RUN=2 scripts/<name>.sh` to get fresh ones. Scripts that change mock-ai's settings put them back when they exit, including on Ctrl-C.

## 2. Start and verify

```sh
docker compose up --build -d --wait
docker compose ps --format 'table {{.Service}}\t{{.Status}}'
```

```text
SERVICE   STATUS
api       Up 4 seconds (healthy)
db        Up 9 seconds (healthy)
mock-ai   Up 9 seconds (healthy)
worker    Up 4 seconds
```

`--wait` returns only once `db`, `api` and `mock-ai` pass their health checks. The worker has no health check, so it only shows `Up`.

```sh
scripts/verify_stack.sh
```

```text
== Health
GET  /healthz -> 200 {"status":"ok"}

== Schema: five tables
    tablename
------------------
 circuit_breaker
 encounter_events
 encounters
 job_attempts
 summary_jobs


== One encounter, accepted, then summarised in the background
POST v1   evt-verify-1 -> 201 {"outcome":"accepted","encounter_id":"enc-verify-1","version":1,"current_version":1,"job_status":"queued"}
GET  enc-verify-1 -> 200 {"encounter_id":"enc-verify-1","patient_id":"pat-33","encounter_type":"Appointment","current_version":1,"status":"processing","summary":null,"accepted_at":"2026-09-29T04:08:33Z","sla_breached":false}
GET  enc-verify-1 -> 200 {"encounter_id":"enc-verify-1","patient_id":"pat-33","encounter_type":"Appointment","current_version":1,"status":"ready","summary_version":1,"summary":"Visit summarised; follow-up actions noted. Source length: 23 words. Ref 1.","accepted_at":"2026-09-29T04:08:33Z","completed_at":"2026-09-29T04:08:40Z","sla_breached":false}

== Validation happens before any database work
POST version 0   -> 400 {"error":"invalid_request","detail":"version must be a positive integer"}
POST 1.1 MB body -> 413 {"error":"payload_too_large","max_bytes":1048576}
```

**Proves:**

- The schema was applied at startup.
- A POST is acknowledged (`201`, `job_status: queued`) without waiting for the AI.
- GET reports `processing` until a worker writes the summary, then `ready` with the exact `summary_version`.
- Oversized and malformed requests are rejected before any database work.
- The acknowledgement-to-ready time was under the 10 s SLA (`sla_breached: false`).

## 3. The failure scenarios

In the brief's order, plus payload conflict. Each script prints every response and the stored state, then exits.

| # | Brief scenario | Script | Time | Forced by (`pytest -k`) |
|---|---|---|---|---|
| 1 | Concurrent duplicate | `scenario_concurrent_duplicate.sh` | 4 s | `test_1` |
| 2 | Missing or late version | `scenario_late_version.sh` | 10 s | `test_2` |
| 3 | Crash after saving | `scenario_crash_after_saving.sh` | 14 s | `test_4` |
| 4 | Worker interruption | `scenario_worker_interruption.sh` | 65 s | `test_7 or reclaim` |
| 5 | Newer version during processing | `scenario_newer_version_during_processing.sh` | 11 s | `test_in_flight_result_for_obsolete_version` |
| 6 | Results out of order | `scenario_results_out_of_order.sh` | 15 s | `test_6` |
| 7a | AI outage | `scenario_ai_outage.sh` | 173 s | `test_5` |
| 7b | Retry exhaustion, inspect, redrive | `scenario_retry_exhaustion.sh` | 122 s | `redrive_current or redrive_obsolete` |
| 8 | Inconsistent identity fields | `scenario_identity_conflict.sh` | 3 s | `test_3` |
| 9 | Payload conflict (the brief's other invariant) | `scenario_payload_conflict.sh` | 2 s | `payload_conflict` |

The scripts pin mock-ai to no random failures while they run, so they print the same thing every time. Without that, the default 5% failure rate could add a retry to any scenario. The last column selects the pytest tests that forces the same situation deterministically, with barriers, lock holds, crash injection or a scripted mock. Run one with:

```sh
docker compose run --rm api python -m pytest -k '<expression>'
```

### Scenario 1: Concurrent duplicate

> *Two service instances receive the same event concurrently.*

```sh
scripts/scenario_concurrent_duplicate.sh
```

```text
== 10 identical requests at once, brand-new encounter (v1)
      9 200 {"outcome":"duplicate","encounter_id":"enc-dup-1","version":1,"current_version":1}
      1 201 {"outcome":"accepted","encounter_id":"enc-dup-1","version":1,"current_version":1,"job_status":"queued"}

== 10 identical requests at once, existing encounter (v2)
      9 200 {"outcome":"duplicate","encounter_id":"enc-dup-1","version":2,"current_version":2}
      1 201 {"outcome":"accepted","encounter_id":"enc-dup-1","version":2,"current_version":2,"job_status":"queued"}

== Stored state: one event and one job per version
 version | events | jobs
---------+--------+------
       1 |      1 |    1
       2 |      1 |    1

 encounter_id | patient_id | current_version
--------------+------------+-----------------
 enc-dup-1    | pat-58     |               2
```

**Proves:** exactly one of ten simultaneous identical requests is accepted, and the other nine get `200 duplicate` with the current version. This holds both for an encounter that didn't exist yet and for one that did. The `event_id` primary key decides, not a read-then-insert. Nothing is stored twice: one event and one job per version.

**Check stored state yourself:**

```sh
docker compose exec db psql -U outcomes -c "SELECT event_id, version FROM encounter_events WHERE encounter_id = 'enc-dup-1'"
```

**Forced version:**

```sh
docker compose run --rm api python -m pytest tests/test_headline.py -k test_1
```

This test releases two app instances (separate connection pools) from a barrier, and holds the first transaction after it takes the row lock until Postgres shows the second one waiting. That's 200 iterations on each kind of encounter, and each iteration asserts the interleaving really happened. Ten parallel curls against one API are a realistic approximation; the test makes the race certain.

### Scenario 2: Missing or late version

> *Version 12 arrives while version 10 is stored. Version 11 arrives later, or never arrives.*

```sh
scripts/scenario_late_version.sh
```

```text
== v10, then v12 (v11 skipped), then the late v11, then a retry of v10
POST v10  evt-late-1-10 -> 201 {"outcome":"accepted","encounter_id":"enc-late-1","version":10,"current_version":10,"job_status":"queued"}
POST v12  evt-late-1-12 -> 201 {"outcome":"accepted","encounter_id":"enc-late-1","version":12,"current_version":12,"job_status":"queued"}
POST v11  evt-late-1-11 -> 200 {"outcome":"stale","encounter_id":"enc-late-1","version":11,"current_version":12}
POST v10  evt-late-1-10 -> 200 {"outcome":"duplicate","encounter_id":"enc-late-1","version":10,"current_version":12}

== The client only ever sees v12: processing, then ready
GET  enc-late-1 -> 200 {"encounter_id":"enc-late-1","patient_id":"pat-91","encounter_type":"TelephoneTriage","current_version":12,"status":"processing","summary":null,"accepted_at":"2026-09-29T04:08:46Z","sla_breached":false}
GET  enc-late-1 -> 200 {"encounter_id":"enc-late-1","patient_id":"pat-91","encounter_type":"TelephoneTriage","current_version":12,"status":"ready","summary_version":12,"summary":"Visit summarised; follow-up actions noted. Source length: 22 words. Ref 4.","accepted_at":"2026-09-29T04:08:46Z","completed_at":"2026-09-29T04:08:52Z","sla_breached":false}

== Stored state: current_version 12, v11 never stored, v10's job superseded
 encounter_id | current_version
--------------+-----------------
 enc-late-1   |              12

 version |   event_id
---------+---------------
      10 | evt-late-1-10
      12 | evt-late-1-12

 version |   status   | superseded_by_version
---------+------------+-----------------------
      10 | superseded |                    12
      12 | ready      |
```

**Proves:**

- A skipped version is fine: v12 is accepted over v10.
- The late v11 is acknowledged as `200 stale` with `current_version: 12`, and is never stored, so the version never regresses.
- A retry of v10 is still recognised as a `duplicate`.
- v10's summary job is superseded, so the client only ever sees v12.

How v10's job got to `superseded` depends on timing: either it was skipped at the pre-call check (no paid call), or its in-flight result was written as superseded. Either way, it's never shown.

**Check stored state yourself:**

```sh
docker compose exec db psql -U outcomes -c "SELECT version, status, superseded_by_version FROM summary_jobs WHERE encounter_id = 'enc-late-1' ORDER BY version"
```

**Forced version:**

```sh
docker compose run --rm api python -m pytest tests/test_headline.py -k test_2
```

This test races v12 and v13 in both orders for 100 iterations, with a worker running, and samples GET throughout. It asserts that v12 is never shown once v13 is committed.

### Scenario 3: Crash after saving

> *The encounter update is saved, but the service crashes before handing work to the summary worker.*

```sh
scripts/scenario_crash_after_saving.sh
```

```text
== Stop the workers, so nothing can pick the job up
docker compose stop worker: done

== Accept an update, then kill the API with SIGKILL
POST v1   evt-crash-1 -> 201 {"outcome":"accepted","encounter_id":"enc-crash-1","version":1,"current_version":1,"job_status":"queued"}
docker compose kill api: done

== Stored state with the API and workers down: the job was saved with the update
 current_version | version | status | attempts
-----------------+---------+--------+----------
               1 |       1 | queued |        0


== Restart the API and workers; the partner, which may not have seen the 201, retries
docker compose up -d --wait api worker: done
POST v1   evt-crash-1 -> 200 {"outcome":"duplicate","encounter_id":"enc-crash-1","version":1,"current_version":1}
GET  enc-crash-1 -> 200 {"encounter_id":"enc-crash-1","patient_id":"pat-77","encounter_type":"TelephoneTriage","current_version":1,"status":"ready","summary_version":1,"summary":"Conversation condensed; symptoms, history and next steps captured. Source length: 17 words. Ref 5.","accepted_at":"2026-09-29T04:08:58Z","completed_at":"2026-09-29T04:09:08Z","sla_breached":true}

== Stored state: one job, summarised once
 version | status | attempts
---------+--------+----------
       1 | ready  |        1
```

**Proves:** there is no hand-off step to lose. The summary job row commits in the same transaction as the encounter update. So once the API has said `201`, the work is in Postgres, even with the API killed and no worker running. After the restart, the partner's retry is a harmless `duplicate`, and the job is summarised exactly once. `sla_breached` is `true` here because just over 10 s passed between acceptance and the summary while the workers were down and restarting. On a faster restart it can come out `false`. Either way, a breach is reported, not treated as a failure.

**Check stored state yourself:**

```sh
docker compose exec db psql -U outcomes -c "SELECT version, status, attempts FROM summary_jobs WHERE encounter_id = 'enc-crash-1'"
```

**Forced version:** the manual run can only kill the API *after* COMMIT. The test kills a real uvicorn process with `os._exit` at two named points, just **before** and just **after** the ingestion COMMIT, and then retries as the partner would:

```sh
docker compose run --rm api python -m pytest tests/test_headline.py -k test_4
```

Before the COMMIT, nothing is stored and the retry is accepted. After it, the retry is a duplicate. Either way, there is exactly one job and one summary.

### Scenario 4: Worker interruption

> *A worker crashes during processing, or after saving a result but before acknowledging the job.*

```sh
scripts/scenario_worker_interruption.sh
```

```text
== AI calls take 20 s. Post, and wait until a worker is mid-call
POST v1   evt-interrupt-1 -> 201 {"outcome":"accepted","encounter_id":"enc-interrupt-1","version":1,"current_version":1,"job_status":"queued"}
 attempt_no |  outcome  |   status   | lease_live
------------+-----------+------------+------------
          1 | in_flight | processing | t


== Kill the workers with SIGKILL (no graceful drain), make the AI fast again, restart them
docker compose kill worker: done
docker compose up -d worker: done
GET  enc-interrupt-1 -> 200 {"encounter_id":"enc-interrupt-1","patient_id":"pat-33","encounter_type":"Appointment","current_version":1,"status":"processing","summary":null,"accepted_at":"2026-09-29T04:09:10Z","sla_breached":false}

== Waiting for the lease to expire and a worker to reclaim the job (about a minute)
GET  enc-interrupt-1 -> 200 {"encounter_id":"enc-interrupt-1","patient_id":"pat-33","encounter_type":"Appointment","current_version":1,"status":"ready","summary_version":1,"summary":"Conversation condensed; symptoms, history and next steps captured. Source length: 15 words. Ref 7.","accepted_at":"2026-09-29T04:09:10Z","completed_at":"2026-09-29T04:10:13Z","sla_breached":true}

== Stored state: the dead attempt closed as lease_expired, the reclaim succeeded
 attempt_no |    outcome    | error_class
------------+---------------+-------------
          1 | lease_expired | worker_lost
          2 | succeeded     |
```

**Proves:**

- A worker killed mid-call (SIGKILL, so no graceful drain) loses nothing.
- Its 60 s lease expires and a worker reclaims the job, bumping the fencing counter.
- The abandoned attempt is closed as `lease_expired` / `worker_lost`, and counts against the retry budget, so a transcript that crashes every worker can't loop forever.
- The attempt history records both paid calls.

**The second half of the brief's scenario,** "after saving a result but before acknowledging", has no gap to fall into. The result write *is* the acknowledgement: one fenced `UPDATE` sets `status`, `summary` and `completed_at` together.

The dangerous variant is a worker that **stalls** past its lease and then returns. Its late write must be rejected. That can't be timed by hand, so the test does it with a real lease expiry:

```sh
docker compose run --rm api python -m pytest -k "test_7 or reclaim"
```

`test_7_stalled_worker_is_fenced` holds a worker's call past its lease. A second worker reclaims the job and succeeds. The first worker's late result then matches no row (`attempts = my_attempt` fails), so its attempt is marked `discarded` and the stored summary is the second worker's.

**Check stored state yourself:**

```sh
docker compose exec db psql -U outcomes -c "SELECT attempt_no, outcome, error_class, worker_id FROM job_attempts a JOIN summary_jobs j USING (job_id) WHERE j.encounter_id = 'enc-interrupt-1' ORDER BY attempt_no"
```

### Scenario 5: Newer version during processing

> *Version 12 is actively being summarized when version 13 arrives. What happens to version 12's work and result?*

```sh
scripts/scenario_newer_version_during_processing.sh
```

```text
== v12's AI call takes 4 s. Post v12 and wait until it is mid-call
POST v12  evt-newer-1-12 -> 201 {"outcome":"accepted","encounter_id":"enc-newer-1","version":12,"current_version":12,"job_status":"queued"}

== v13 arrives while v12 is being summarised; its call will take 7 s
POST v13  evt-newer-1-13 -> 201 {"outcome":"accepted","encounter_id":"enc-newer-1","version":13,"current_version":13,"job_status":"queued"}
GET  enc-newer-1 -> 200 {"encounter_id":"enc-newer-1","patient_id":"pat-91","encounter_type":"TelephoneTriage","current_version":13,"status":"processing","summary":null,"accepted_at":"2026-09-29T04:10:16Z","sla_breached":false}

== v12's call finishes first: its result is stored as superseded, and GET still waits for v13
GET  enc-newer-1 -> 200 {"encounter_id":"enc-newer-1","patient_id":"pat-91","encounter_type":"TelephoneTriage","current_version":13,"status":"processing","summary":null,"accepted_at":"2026-09-29T04:10:16Z","sla_breached":false}

== v13's call finishes
GET  enc-newer-1 -> 200 {"encounter_id":"enc-newer-1","patient_id":"pat-91","encounter_type":"TelephoneTriage","current_version":13,"status":"ready","summary_version":13,"summary":"Visit summarised; follow-up actions noted. Source length: 22 words. Ref 9.","accepted_at":"2026-09-29T04:10:16Z","completed_at":"2026-09-29T04:10:24Z","sla_breached":false}

== Stored state: v12 paid for once and kept for history, marked superseded by 13
 version |   status   | superseded_by_version | has_summary |  attempt
---------+------------+-----------------------+-------------+------------
      12 | superseded |                    13 | t           | superseded
      13 | ready      |                       | t           | succeeded
```

**Proves:**

- v12's in-flight call is allowed to finish, because cancelling a paid call saves nothing.
- Its result is written with `status = superseded` and `superseded_by_version = 13`, decided in the same statement against the encounter's current version.
- GET resolves the summary through `encounters.current_version`, so it reports v13 as `processing` until v13's own summary is ready. It never shows v12's.

**Check stored state yourself:**

```sh
docker compose exec db psql -U outcomes -c "SELECT version, status, superseded_by_version FROM summary_jobs WHERE encounter_id = 'enc-newer-1' ORDER BY version"
```

**Forced version:** `docker compose run --rm api python -m pytest -k test_in_flight_result_for_obsolete_version`. There is also `test_precall_check_skips_obsolete_job_without_calling`, for when v13 arrives *before* v12's call starts: then no paid call is made at all.

### Scenario 6: Results out of order

> *Version 13 finishes summarizing before version 12.*

```sh
scripts/scenario_results_out_of_order.sh
```

```text
== v12's AI call will take 12 s. Post v12 and wait until it is mid-call
POST v12  evt-order-1-12 -> 201 {"outcome":"accepted","encounter_id":"enc-order-1","version":12,"current_version":12,"job_status":"queued"}

== Make calls take 1 s and post v13: its summary is ready while v12's call is still running
POST v13  evt-order-1-13 -> 201 {"outcome":"accepted","encounter_id":"enc-order-1","version":13,"current_version":13,"job_status":"queued"}
GET  enc-order-1 -> 200 {"encounter_id":"enc-order-1","patient_id":"pat-58","encounter_type":"MedicationRefill","current_version":13,"status":"ready","summary_version":13,"summary":"Encounter reviewed; key concerns and plan documented. Source length: 21 words. Ref 11.","accepted_at":"2026-09-29T04:10:27Z","completed_at":"2026-09-29T04:10:28Z","sla_breached":false}
 version |   status
---------+------------
      12 | processing
      13 | ready


== v12's result arrives last and is stored as superseded; GET is unchanged
GET  enc-order-1 -> 200 {"encounter_id":"enc-order-1","patient_id":"pat-58","encounter_type":"MedicationRefill","current_version":13,"status":"ready","summary_version":13,"summary":"Encounter reviewed; key concerns and plan documented. Source length: 21 words. Ref 11.","accepted_at":"2026-09-29T04:10:27Z","completed_at":"2026-09-29T04:10:28Z","sla_breached":false}

== Stored state
 version |   status   | superseded_by_version |  attempt   | finished
---------+------------+-----------------------+------------+----------
      13 | ready      |                       | succeeded  |        1
      12 | superseded |                    13 | superseded |        2
```

**Proves:** v13's summary becomes current as soon as it's ready. v12's result then arrives about 10 s later and is stored as `superseded`, leaving GET byte-for-byte unchanged. An older result never overwrites a newer one, whatever order the calls finish in.

**How the script forces the order:** mock-ai chooses each call's latency when the call arrives. v12's call is started with a 12 s latency; then the latency is switched to 1 s and v13 is posted.

**Check stored state yourself:**

```sh
docker compose exec db psql -U outcomes -c "SELECT version, status, superseded_by_version, completed_at FROM summary_jobs WHERE encounter_id = 'enc-order-1' ORDER BY version"
```

**Forced version,** with a scripted mock that releases v13's call first:

```sh
docker compose run --rm api python -m pytest tests/test_headline.py -k test_6
```

### Scenario 7: AI outage and retry exhaustion

> *The AI service is unavailable for 20 minutes. Explain backoff and retry behavior, including the per-call cost of retrying a non-idempotent, paid operation; what happens when the retry policy is exhausted; how failed work can be inspected or safely redriven; and what the client sees throughout.*

These are two different situations, so there are two scripts:

- **7a**, a full outage, is handled by the shared circuit breaker, and no job fails.
- **7b**, a job exhausting its own retry budget, ends in `failed`, followed by inspection and redrive.

#### 7a. The outage

```sh
scripts/scenario_ai_outage.sh
```

The script:

1. scales to 3 worker containers (12 loops);
2. switches mock-ai into outage mode and resets its call counters;
3. posts 40 encounters and samples every 30 s for about 90 s;
4. ends the outage and waits for all 40 to be ready.

```text
== Three worker containers: 12 worker loops
docker compose up -d --wait --scale worker=3 worker: done

== Reset mock-ai's call counters and switch it into an outage
{"calls_total":0,"real":0,"probe":0,"results":{}}

== Post 40 encounters
     40 201

== Outage: sample every 30 s. Real calls stop at the trip; probes go up by one per cooldown
t=  6s  breaker: open      probes_sent=0  jobs: queued=40                         mock-ai: {"calls_total":11,"real":11,"probe":0,"results":{"ai_unavailable":11}}
t= 35s  breaker: open      probes_sent=1  jobs: queued=40                         mock-ai: {"calls_total":12,"real":11,"probe":1,"results":{"ai_unavailable":12}}
t= 65s  breaker: open      probes_sent=2  jobs: queued=40                         mock-ai: {"calls_total":13,"real":11,"probe":2,"results":{"ai_unavailable":13}}
t= 95s  breaker: open      probes_sent=3  jobs: queued=40                         mock-ai: {"calls_total":14,"real":11,"probe":3,"results":{"ai_unavailable":14}}

== What a client and the metrics show during the outage
GET  enc-outage-1-1 -> 200 {"encounter_id":"enc-outage-1-1","patient_id":"pat-1","encounter_type":"TelephoneTriage","current_version":1,"status":"processing","summary":null,"accepted_at":"2026-09-29T04:10:45Z","sla_breached":true}
summary_sla_breaching_jobs 40.0
summary_jobs{status="queued"} 40.0
summary_jobs{status="failed"} 0.0
breaker_state{state="closed"} 0.0
breaker_state{state="open"} 1.0
breaker_state{state="half_open"} 0.0
breaker_probes_sent_total 3.0

== End the outage. The next probe closes the breaker, then the queue drains
t= 98s  breaker: open      probes_sent=3  jobs: queued=40                         mock-ai: {"calls_total":14,"real":11,"probe":3,"results":{"ai_unavailable":14}}
t=111s  breaker: open      probes_sent=3  jobs: queued=40                         mock-ai: {"calls_total":14,"real":11,"probe":3,"results":{"ai_unavailable":14}}
t=123s  breaker: half_open probes_sent=4  jobs: queued=40                         mock-ai: {"calls_total":15,"real":11,"probe":4,"results":{"ai_unavailable":14}}
t=136s  breaker: closed    probes_sent=4  jobs: processing=12 queued=13 ready=15  mock-ai: {"calls_total":42,"real":38,"probe":4,"results":{"ai_unavailable":14,"succeeded":16}}
t=148s  breaker: closed    probes_sent=4  jobs: processing=2 ready=38             mock-ai: {"calls_total":55,"real":51,"probe":4,"results":{"ai_unavailable":14,"succeeded":41}}
t=161s  breaker: closed    probes_sent=4  jobs: ready=40                          mock-ai: {"calls_total":55,"real":51,"probe":4,"results":{"ai_unavailable":14,"succeeded":41}}
GET  enc-outage-1-1 -> 200 {"encounter_id":"enc-outage-1-1","patient_id":"pat-1","encounter_type":"TelephoneTriage","current_version":1,"status":"ready","summary_version":1,"summary":"Conversation condensed; symptoms, history and next steps captured. Source length: 5 words. Ref 16.","accepted_at":"2026-09-29T04:10:45Z","completed_at":"2026-09-29T04:12:55Z","sla_breached":true}

== Stored state: every job ready, none failed; real calls per outcome
 status | count
--------+-------
 ready  |    40

     outcome     |  error_class   | count
-----------------+----------------+-------
 succeeded       |                |    40
 transient_error | ai_unavailable |    11


== Back to one worker container
docker compose up -d --wait --scale worker=1 worker: done
```

**Proves:**

- **The cost of an outage is bounded by the breaker, not by the number of waiting jobs.** Real calls stopped as soon as the breaker tripped: 11 here, exactly the threshold. Without the breaker, 40 jobs could each have spent 5 attempts: up to 200 paid calls.
- **While the breaker is open, workers claim nothing**, so no job's retry budget is touched. Exactly one synthetic probe per 30 s cooldown checks the provider, and mock-ai counts it separately (`probe`), so no transcript is sent while the provider is down.
- **Throughout, the client sees `processing`** with `sla_breached: true`, and new POSTs are still accepted. The breach shows in `summary_sla_breaching_jobs` without failing any job.
- **After recovery, every job reaches `ready` and none fail.** The first probe after the outage closes the breaker, and the queue drains.

**Timing-dependent numbers:**

- **Real calls at the trip:** 11 plus any calls already in flight on other loops when it tripped. Across the three runs made for this guide it was 11, 13 and 13. The bound is 11 plus one per busy worker loop.
- **Probe count:** depends on where the cooldown falls when the outage ends.
- **The drain-phase samples:** they vary with the mock's 2–8 s latency.

Every run ends with 40 `ready` and 0 `failed`. The `probes_sent` column is a running total for the stack's lifetime.

**Check stored state yourself:**

```sh
docker compose exec db psql -U outcomes -c "SELECT state, probes_sent, last_probe_outcome, opened_at FROM circuit_breaker"
```

**Forced version:** `docker compose run --rm api python -m pytest tests/test_headline.py -k test_5`. This is 100 jobs and 4 workers with compressed time. It asserts real calls ≤ 11 plus those in flight, that probes are synthetic only, that no job fails, and that all recover.

#### 7b. Retry exhaustion, inspection and redrive

A job fails only when its own budget runs out. That happens when the provider fails *this job* but the breaker correctly stays closed, for example during a partial degradation. Here, two jobs fail 10 calls between them, below the breaker's 11.

```sh
scripts/scenario_retry_exhaustion.sh
```

```text
== The provider fails every call. Post two encounters
POST v1   evt-exhaust-1-a1 -> 201 {"outcome":"accepted","encounter_id":"enc-exhaust-1-a","version":1,"current_version":1,"job_status":"queued"}
POST v1   evt-exhaust-1-b1 -> 201 {"outcome":"accepted","encounter_id":"enc-exhaust-1-b","version":1,"current_version":1,"job_status":"queued"}
GET  enc-exhaust-1-a -> 200 {"encounter_id":"enc-exhaust-1-a","patient_id":"pat-58","encounter_type":"MedicationRefill","current_version":1,"status":"processing","summary":null,"accepted_at":"2026-09-29T04:13:34Z","sla_breached":false}

== Waiting for both jobs to spend their budget of 5 (about 2 minutes)
GET  enc-exhaust-1-a -> 200 {"encounter_id":"enc-exhaust-1-a","patient_id":"pat-58","encounter_type":"MedicationRefill","current_version":1,"status":"failed","summary":null,"attempts":5,"error_class":"rate_limited","accepted_at":"2026-09-29T04:13:34Z","completed_at":"2026-09-29T04:15:02Z","sla_breached":true}

== Inspect: the attempt history, one row per paid call, no patient content
 attempt_no | redrive_generation |     outcome     |  error_class   | backoff_s
------------+--------------------+-----------------+----------------+-----------
          1 |                  0 | transient_error | ai_unavailable |
          2 |                  0 | transient_error | ai_unavailable |         6
          3 |                  0 | transient_error | ai_timeout     |        11
          4 |                  0 | transient_error | rate_limited   |        30
          5 |                  0 | transient_error | rate_limited   |        41


== Fix the provider. Meanwhile enc-exhaust-1-b gets a newer version, which succeeds
POST v2   evt-exhaust-1-b2 -> 201 {"outcome":"accepted","encounter_id":"enc-exhaust-1-b","version":2,"current_version":2,"job_status":"queued"}

== Redrive both failed jobs: enc-exhaust-1-a's is still current, enc-exhaust-1-b's is not
POST /admin/jobs/52/redrive (enc-exhaust-1-a v1) -> 200 {"job_id":52,"outcome":"redriven"}
POST /admin/jobs/53/redrive (enc-exhaust-1-b v1) -> 200 {"job_id":53,"outcome":"superseded","superseded_by_version":2}
GET  enc-exhaust-1-a -> 200 {"encounter_id":"enc-exhaust-1-a","patient_id":"pat-58","encounter_type":"MedicationRefill","current_version":1,"status":"ready","summary_version":1,"summary":"Encounter reviewed; key concerns and plan documented. Source length: 14 words. Ref 67.","accepted_at":"2026-09-29T04:13:34Z","completed_at":"2026-09-29T04:15:33Z","sla_breached":true}

== A job that is not failed cannot be redriven
POST /admin/jobs/52/redrive (enc-exhaust-1-a v1) -> 409 {"error":"job_not_failed","job_id":52,"status":"ready"}

== Stored state: the redrive got a fresh budget (generation 1); enc-exhaust-1-b v1 was never paid for again
  encounter_id   | version |   status   | superseded_by_version | redrive_generation | attempts |    outcomes
-----------------+---------+------------+-----------------------+--------------------+----------+-----------------
 enc-exhaust-1-a |       1 | ready      |                       |                  0 |        5 | transient_error
 enc-exhaust-1-a |       1 | ready      |                       |                  1 |        1 | succeeded
 enc-exhaust-1-b |       1 | superseded |                     2 |                  0 |        5 | transient_error
 enc-exhaust-1-b |       2 | ready      |                       |                  0 |        1 | succeeded
```

**Proves:**

- **Retries are budgeted.** Each job gets 5 paid attempts, spaced by jittered exponential backoff: `uniform(0.5, 1) × 10 s × 2^(n−1)`, so each gap falls in its 5–10, 10–20, 20–40 and 40–80 s band. Then the job is `failed`.
- **The client is told exactly what happened:** GET shows `status: failed` with `attempts: 5` and the last `error_class`. There is no patient content and no raw provider error.
- **`failed` waits for a person.** The attempt history is the inspection tool. It shows every paid call, its outcome and its error class.
- **Redrive is safe.** A failed job whose version is still current gets a fresh budget: `redrive_generation` 1. A failed job whose encounter has moved on is marked `superseded` instead, and is never paid for again. A job that isn't `failed` can't be redriven (`409`).

The `job_id`s depend on how many jobs exist when you run it. The backoff seconds vary within their bands, and which error class each attempt got varies, because mock-ai picks one at random.

**Bulk redrive** of everything that failed in a time window:

```sh
curl -s -X POST localhost:8000/admin/jobs/redrive -H 'content-type: application/json' \
  -d '{"failed_from": "2026-01-01T00:00:00Z", "failed_to": "2030-01-01T00:00:00Z"}'; echo
```

```text
{"redriven":0,"superseded":0}
```

It returns counts. Both are zero here, because the script above already resolved both failed jobs. The bulk redrive applies the same two rules as the single one, in one transaction.

**Forced versions:**

```sh
docker compose run --rm api python -m pytest -k "fifth_failure or redrive_current or redrive_obsolete or fresh_budget"
```

These cover backoff doubling, the fifth failure, redriving a current job, superseding an obsolete one, and the fresh budget.

### Scenario 8: Inconsistent identity fields

> *An event for an existing `encounter_id` arrives with a different `patient_id` or `encounter_type` than previously stored.*

```sh
scripts/scenario_identity_conflict.sh
```

```text
== v1 for patient pat-33, then v2 with a different patient, then with a different type
POST v1   evt-identity-1-1 -> 201 {"outcome":"accepted","encounter_id":"enc-identity-1","version":1,"current_version":1,"job_status":"queued"}
POST v2   evt-identity-1-2 -> 409 {"outcome":"identity_conflict","encounter_id":"enc-identity-1","current_version":1,"conflicting_field":"patient_id","message":"Contradicts stored encounter identity. Retrying will not succeed."}
POST v2   evt-identity-1-2 -> 409 {"outcome":"identity_conflict","encounter_id":"enc-identity-1","current_version":1,"conflicting_field":"encounter_type","message":"Contradicts stored encounter identity. Retrying will not succeed."}

== The same v2 with the stored identity is accepted
POST v2   evt-identity-1-2 -> 201 {"outcome":"accepted","encounter_id":"enc-identity-1","version":2,"current_version":2,"job_status":"queued"}

== Stored state: identity unchanged; the rejected events were never stored
  encounter_id  | patient_id | encounter_type | current_version
----------------+------------+----------------+-----------------
 enc-identity-1 | pat-33     | Appointment    |               2

     event_id     | version
------------------+---------
 evt-identity-1-1 |       1
 evt-identity-1-2 |       2


== The one log line allowed to carry patient IDs, for diagnosing the partner's mapping bug
{"ts": "2026-09-29T04:15:36.776+00:00", "level": "WARNING", "service": "api", "logger": "ingest", "event": "identity_conflict", "event_id": "evt-identity-1-2", "encounter_id": "enc-identity-1", "stored_patient_id": "pat-33", "incoming_patient_id": "pat-99", "stored_encounter_type": "Appointment", "incoming_encounter_type": "Appointment"}
{"ts": "2026-09-29T04:15:36.860+00:00", "level": "WARNING", "service": "api", "logger": "ingest", "event": "identity_conflict", "event_id": "evt-identity-1-2", "encounter_id": "enc-identity-1", "stored_patient_id": "pat-33", "incoming_patient_id": "pat-33", "stored_encounter_type": "Appointment", "incoming_encounter_type": "MedicationRefill"}
```

**Proves:**

- The stored identity is never changed by a later event, and a contradicting event is rejected with `409 identity_conflict`, naming the field.
- The 409 body carries **neither patient ID**, so a caller can't use it to probe which patient an encounter belongs to.
- Nothing from the rejected events is stored, because the whole ingestion transaction rolls back.
- The log line is the one place `patient_id` appears, on purpose: an operator needs both values to diagnose the partner's mapping bug.

**Check stored state yourself:**

```sh
docker compose exec db psql -U outcomes -c "SELECT patient_id, encounter_type, current_version FROM encounters WHERE encounter_id = 'enc-identity-1'"
```

**Forced version:** `docker compose run --rm api python -m pytest tests/test_headline.py -k test_3`. Two *first* events for a brand-new encounter race with different patients, 200 iterations. The first patient to commit wins, and the other gets a 409 that contains no patient ID.

### Scenario 9 (extra): Payload conflict

The brief says the source doesn't send conflicting payloads for the same version, but that "your design should decide what to do if either invariant is ever violated". This scenario covers the payload invariant; scenario 8 covers identity.

```sh
scripts/scenario_payload_conflict.sh
```

```text
== v1 accepted
POST v1   evt-payload-1-1 -> 201 {"outcome":"accepted","encounter_id":"enc-payload-1","version":1,"current_version":1,"job_status":"queued"}

== Same event_id, different transcript
POST v1   evt-payload-1-1 -> 409 {"outcome":"payload_conflict","encounter_id":"enc-payload-1","version":1,"current_version":1,"message":"A different payload is already recorded for this version."}

== New event_id for the same version, different transcript
POST v1   evt-payload-1-1b -> 409 {"outcome":"payload_conflict","encounter_id":"enc-payload-1","version":1,"current_version":1,"message":"A different payload is already recorded for this version."}

== New event_id for the same version, same transcript: a source defect, treated as a duplicate
POST v1   evt-payload-1-1c -> 200 {"outcome":"duplicate","encounter_id":"enc-payload-1","version":1,"current_version":1}

== Stored state: one event with the first transcript's hash, one job
    event_id     | version | hash_of_first_transcript
-----------------+---------+--------------------------
 evt-payload-1-1 |       1 | t

 version | jobs
---------+------
       1 |    1
```

**Proves:**

- A version's content can't be rewritten, whether the same `event_id` or a new one carries a different transcript: both get `409 payload_conflict`.
- The `(encounter_id, version)` unique constraint makes a new `event_id` with the *same* content a harmless duplicate: a source defect, counted in `ingest_duplicate_source_defects_total`.
- The stored event keeps the first transcript's SHA-256.

**Check stored state yourself:**

```sh
docker compose exec db psql -U outcomes -c "SELECT event_id, version, encode(payload_hash, 'hex') FROM encounter_events WHERE encounter_id = 'enc-payload-1'"
```

**Forced version:** `docker compose run --rm api python -m pytest -k payload_conflict`.

## 4. Running the tests

The tests run inside the app image against a separate `outcomes_test` database on the Compose Postgres: a real Postgres, never SQLite or a fake. `mock-ai` must be up, because one smoke test drives a real worker process against it.

### Headline tests only

The seven tests from design §7; test 1 runs twice, once for an existing encounter and once for a brand-new one.

```sh
docker compose run --rm api python -m pytest tests/test_headline.py -v
```

Filtered to the result lines:

```text
tests/test_headline.py::test_1_concurrent_duplicate_accepted_exactly_once[existing_encounter] PASSED [ 12%]
tests/test_headline.py::test_1_concurrent_duplicate_accepted_exactly_once[brand_new_encounter] PASSED [ 25%]
tests/test_headline.py::test_2_version_never_regresses_and_v12_is_never_shown PASSED [ 37%]
tests/test_headline.py::test_3_identity_race_on_a_brand_new_encounter PASSED [ 50%]
tests/test_headline.py::test_4_crash_around_the_ingestion_commit_loses_no_work PASSED [ 62%]
tests/test_headline.py::test_5_outage_costs_bounded_calls_and_loses_no_work PASSED [ 75%]
tests/test_headline.py::test_6_forced_results_out_of_order PASSED        [ 87%]
tests/test_headline.py::test_7_stalled_worker_is_fenced PASSED           [100%]
======================== 8 passed, 1 warning in 45.65s =========================
```

That took 53 s here.

### Full suite

```sh
make test
# without make:
docker compose up -d --build --wait db mock-ai
docker compose run --rm --build api python -m pytest
```

The pytest output, after Compose's image-build lines:

```text
============================= test session starts ==============================
platform linux -- Python 3.12.14, pytest-8.3.4, pluggy-1.6.0
rootdir: /srv
configfile: pytest.ini
testpaths: tests
plugins: anyio-4.15.1
collected 180 items

tests/test_breaker.py ...............                                    [  8%]
tests/test_config.py .........                                           [ 13%]
tests/test_failures.py ............                                      [ 20%]
tests/test_headline.py ........                                          [ 24%]
tests/test_health.py ...                                                 [ 26%]
tests/test_ingest.py ..............................................      [ 51%]
tests/test_mockai.py .......                                             [ 55%]
tests/test_observability.py ........                                     [ 60%]
tests/test_read.py .............                                         [ 67%]
tests/test_redrive.py .....................                              [ 78%]
tests/test_schema.py .....                                               [ 81%]
tests/test_smoke_e2e.py .                                                [ 82%]
tests/test_summary_client.py .................                           [ 91%]
tests/test_worker.py ...........                                         [ 97%]
tests/test_worker_process.py ....                                        [100%]

=============================== warnings summary ===============================
../usr/local/lib/python3.12/site-packages/starlette/testclient.py:40
  /usr/local/lib/python3.12/site-packages/starlette/testclient.py:40: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = typing.Callable[[], typing.ContextManager[anyio.abc.BlockingPortal]]

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
================== 180 passed, 1 warning in 90.57s (0:01:30) ===================
```

That took 110 s here, including the image check. The one warning is a Starlette/anyio deprecation notice from the test client, and is harmless. The suite has passed three consecutive runs with no flakes.

Before running it, let any scenario script finish. The smoke test compares mock-ai's call counts before and after, and a busy dev worker can disturb them.

What each test file covers, and how the harness forces races and crashes, is in [testing.md](testing.md).

## 5. Looking inside

### Metrics

The API serves Prometheus metrics. The queue, SLA and breaker gauges are read from Postgres on each scrape, which is the design's scheduled SLA check.

```sh
curl -s localhost:8000/metrics | grep -E '^(ingest_events_total|summary_sla_breaching_jobs|summary_sla_oldest|summary_queue_depth|summary_jobs|breaker_state|breaker_probes_sent_total)'
```

Here's what that printed straight after scenario 9. If you run it that soon, the last scenarios' jobs may still show as `processing`, and the `ready` and `superseded` totals shift by one or two as they finish.

```text
ingest_events_total{outcome="accepted"} 51.0
ingest_events_total{outcome="duplicate"} 2.0
ingest_events_total{outcome="stale"} 0.0
ingest_events_total{outcome="identity_conflict"} 2.0
ingest_events_total{outcome="payload_conflict"} 2.0
ingest_events_total{outcome="invalid_request"} 0.0
ingest_events_total{outcome="payload_too_large"} 0.0
summary_queue_depth{state="due_now"} 0.0
summary_queue_depth{state="waiting_backoff"} 0.0
summary_queue_depth{state="processing"} 1.0
summary_sla_breaching_jobs 0.0
summary_sla_oldest_unfinished_age_seconds 0.0
summary_jobs{status="queued"} 0.0
summary_jobs{status="processing"} 1.0
summary_jobs{status="ready"} 50.0
summary_jobs{status="failed"} 0.0
summary_jobs{status="superseded"} 6.0
breaker_state{state="closed"} 1.0
breaker_state{state="open"} 0.0
breaker_state{state="half_open"} 0.0
breaker_probes_sent_total 4.0
```

The `ingest_events_total` counters are per API process, so they restarted when scenario 3 killed the API. The Postgres-backed gauges (`summary_jobs`, `breaker_*`) cover everything.

Each worker process serves its own metrics on internal port 9100. The port isn't published, so read them from inside the container:

```sh
docker compose exec worker python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:9100/metrics').read().decode())" | grep -E '^(generate_summary_calls_total|job_attempts_closed_total|summary_jobs_failed_total|breaker_trips_total|worker_)'
```

```text
generate_summary_calls_total{kind="real",result="succeeded"} 22.0
generate_summary_calls_total{kind="real",result="ai_unavailable"} 11.0
generate_summary_calls_total{kind="probe",result="ai_unavailable"} 1.0
generate_summary_calls_total{kind="real",result="rate_limited"} 6.0
generate_summary_calls_total{kind="real",result="ai_timeout"} 1.0
job_attempts_closed_total{error_class="worker_lost",outcome="lease_expired"} 1.0
job_attempts_closed_total{error_class="",outcome="succeeded"} 19.0
job_attempts_closed_total{error_class="",outcome="superseded"} 3.0
job_attempts_closed_total{error_class="ai_unavailable",outcome="transient_error"} 11.0
job_attempts_closed_total{error_class="rate_limited",outcome="transient_error"} 6.0
job_attempts_closed_total{error_class="ai_timeout",outcome="transient_error"} 1.0
summary_jobs_failed_total{path="transient"} 2.0
breaker_trips_total 0.0
worker_slots 4.0
worker_busy_slots 1.0
```

These values depend on which calls this particular worker process made since it last started. `generate_summary_calls_total{kind,result}` is the spend: every paid call, by outcome. The full metric list and the alerts are in [operations.md](operations.md#metrics).

### Logs: no patient content

Every service writes one JSON object per line, and every line carries only IDs, statuses, fixed enums, hashes and durations. This script posts a transcript containing a unique marker, waits for its summary, and searches every service's logs and the API's metrics for the marker, the summary text and the patient ID:

```sh
scripts/check_log_privacy.sh
```

```text
== Post a transcript containing a unique marker, wait for its summary
POST v1   evt-privacy-1 -> 201 {"outcome":"accepted","encounter_id":"enc-privacy-1","version":1,"current_version":1,"job_status":"queued"}

== Search every service's logs and the API's metrics
log lines about enc-privacy-1:     3
log lines containing the transcript: 0
log lines containing the summary:    0
log lines containing the patient ID: 0
metrics lines containing any of them: 0

== What the logs do say about this encounter, in time order
{"ts": "2026-09-29T04:15:43.073+00:00", "level": "INFO", "service": "api", "logger": "ingest", "event": "accepted", "event_id": "evt-privacy-1", "encounter_id": "enc-privacy-1", "version": 1}
{"ts": "2026-09-29T04:15:43.131+00:00", "level": "INFO", "service": "worker", "logger": "worker", "event": "claimed", "job_id": 58, "encounter_id": "enc-privacy-1", "version": 1, "attempt_no": 1, "worker_id": "1c819583a111-1-2", "reclaimed": false}
{"ts": "2026-09-29T04:15:47.440+00:00", "level": "INFO", "service": "worker", "logger": "worker", "event": "result_written", "job_id": 58, "encounter_id": "enc-privacy-1", "version": 1, "attempt_no": 1, "worker_id": "1c819583a111-1-2", "status": "ready", "completion_seconds": 4.369}
```

**Proves:** transcripts, summaries and patient IDs never reach logs or metrics. The only line that carries a `patient_id` is `identity_conflict` (scenario 8). The automated versions are `docker compose run --rm api python -m pytest -k patient_content`.

For a sample of what the logs do contain, here is one encounter's life from scenario 2, sorted by timestamp. Depending on timing, v10's job shows either `skipped_superseded` (no paid call) or a `result_written` with `status: superseded`.

```sh
docker compose logs --no-log-prefix api worker | grep '"enc-late-1"' | sort
```

```text
{"ts": "2026-09-29T04:08:46.109+00:00", "level": "INFO", "service": "api", "logger": "ingest", "event": "accepted", "event_id": "evt-late-1-10", "encounter_id": "enc-late-1", "version": 10}
{"ts": "2026-09-29T04:08:46.204+00:00", "level": "INFO", "service": "api", "logger": "ingest", "event": "accepted", "event_id": "evt-late-1-12", "encounter_id": "enc-late-1", "version": 12}
{"ts": "2026-09-29T04:08:46.283+00:00", "level": "INFO", "service": "api", "logger": "ingest", "event": "stale", "event_id": "evt-late-1-11", "encounter_id": "enc-late-1", "version": 11, "current_version": 12}
{"ts": "2026-09-29T04:08:46.349+00:00", "level": "INFO", "service": "api", "logger": "ingest", "event": "duplicate", "event_id": "evt-late-1-10", "encounter_id": "enc-late-1", "version": 10}
{"ts": "2026-09-29T04:08:46.439+00:00", "level": "INFO", "service": "worker", "logger": "worker", "event": "claimed", "job_id": 4, "encounter_id": "enc-late-1", "version": 10, "attempt_no": 1, "worker_id": "1c819583a111-1-2", "reclaimed": false}
{"ts": "2026-09-29T04:08:46.440+00:00", "level": "INFO", "service": "worker", "logger": "worker", "event": "claimed", "job_id": 5, "encounter_id": "enc-late-1", "version": 12, "attempt_no": 1, "worker_id": "1c819583a111-1-1", "reclaimed": false}
{"ts": "2026-09-29T04:08:46.444+00:00", "level": "INFO", "service": "worker", "logger": "worker", "event": "skipped_superseded", "job_id": 4, "encounter_id": "enc-late-1", "version": 10, "attempt_no": 1, "worker_id": "1c819583a111-1-2"}
{"ts": "2026-09-29T04:08:52.386+00:00", "level": "INFO", "service": "worker", "logger": "worker", "event": "result_written", "job_id": 5, "encounter_id": "enc-late-1", "version": 12, "attempt_no": 1, "worker_id": "1c819583a111-1-1", "status": "ready", "completion_seconds": 6.184}
```

### Attempt history: the cost and audit record

One row per claim, written **before** the paid call starts. For a stuck or late summary, this query plus the job row answer most questions. How to read it is in [operations.md](operations.md#triage-for-a-stuck-or-late-summary).

```sh
docker compose exec db psql -U outcomes -c "SELECT j.encounter_id, j.version, j.status, a.attempt_no, a.redrive_generation, a.outcome, a.error_class, a.finished_at - a.started_at AS took FROM summary_jobs j JOIN job_attempts a USING (job_id) WHERE j.encounter_id = 'enc-exhaust-1-a' ORDER BY a.attempt_no"
```

```text
  encounter_id   | version | status | attempt_no | redrive_generation |     outcome     |  error_class   |      took
-----------------+---------+--------+------------+--------------------+-----------------+----------------+-----------------
 enc-exhaust-1-a |       1 | ready  |          1 |                  0 | transient_error | ai_unavailable | 00:00:00.177874
 enc-exhaust-1-a |       1 | ready  |          2 |                  0 | transient_error | ai_unavailable | 00:00:00.025347
 enc-exhaust-1-a |       1 | ready  |          3 |                  0 | transient_error | ai_timeout     | 00:00:00.067884
 enc-exhaust-1-a |       1 | ready  |          4 |                  0 | transient_error | rate_limited   | 00:00:00.183075
 enc-exhaust-1-a |       1 | ready  |          5 |                  0 | transient_error | rate_limited   | 00:00:00.216879
 enc-exhaust-1-a |       1 | ready  |          6 |                  1 | succeeded       |                | 00:00:01.197364
(6 rows)
```

The error class of each failed attempt varies between runs, because mock-ai picks one at random.

## 6. Troubleshooting

### Port already in use

The two published ports can be moved. Pass the same variables to the scripts:

```sh
API_PORT=18000 MOCK_AI_PORT=18001 docker compose up --build -d --wait
export API_PORT=18000 MOCK_AI_PORT=18001      # the scripts read these
scripts/verify_stack.sh
```

In the hand-typed `curl` commands in this guide, replace `localhost:8000` with `localhost:18000`.

### A script says "API not healthy"

The stack isn't up, or not yet healthy. Run `docker compose up --build -d --wait` and check `docker compose ps`.

### A script says "already exists"

That scenario has already run against this database. Either rerun it with fresh IDs (`RUN=2 scripts/<name>.sh`), or [reset](#1-before-you-start).

### A script times out on a slow machine

The scripts wait up to 30 s for a normal summary, 120 s for the lease reclaim and 240 s for retry exhaustion. On a heavily loaded machine, a wait can occasionally run out. Rerun with the next `RUN` number. If it keeps happening, check `docker compose logs worker` for errors: the logs carry no patient content.

The suite took about 2 minutes here and hasn't been measured on a slower machine. Its concurrency tests force their interleavings with barriers and lock holds rather than relying on timing. The outage and lease tests do run on compressed time, though: leases and cooldowns of a second or less. So heavy load could in principle upset one of them. If a test fails, rerun it on its own with `-k <name>` to tell load from a real failure.

### A script was killed mid-way

A trap restores mock-ai's settings on exit and on Ctrl-C. After a `kill -9`, reset mock-ai with `docker compose restart mock-ai`, which restores its startup defaults and zeroes its counters. If the outage script was interrupted, go back to one worker with `docker compose up -d --scale worker=1`.

### Windows, without make

`make` only wraps these commands:

| `make` | Plain command |
|---|---|
| `make up` | `docker compose up --build -d --wait` |
| `make test` | `docker compose up -d --build --wait db mock-ai` then `docker compose run --rm --build api python -m pytest` |
| `make logs` | `docker compose logs -f` |
| `make down` | `docker compose down` |

Run the scripts from **Git Bash** (installed with Git for Windows) or WSL; PowerShell can't run them. Line endings are pinned to LF by `.gitattributes`, so the scripts work even with `core.autocrlf=true`. If Docker Desktop reports errors like `dialing ... :2376` after being idle, its Resource Saver has stopped the engine. Quit Docker Desktop, run `wsl --shutdown`, and start it again.

### Apple Silicon

Nothing in the stack is x86-specific:

- both base images (`python:3.12-slim`, `postgres:16-alpine`) are published for arm64;
- every Python dependency, including `psycopg[binary]`, ships Linux arm64 wheels: `pip download --only-binary=:all: --platform manylinux2014_aarch64` resolves all 29 packages;
- no `platform:` is pinned in the Compose file.

This guide was captured on x86-64, and the stack hasn't been run on Apple Silicon.

### Resetting state

| To reset | Command |
|---|---|
| Everything: database, mock-ai settings and counters | `docker compose down -v && docker compose up --build -d --wait` |
| Only mock-ai's settings and counters | `docker compose restart mock-ai` |
| Only the test database | Nothing to do: the suite recreates it on every run |

## 7. Requirement map

Every requirement in the brief, and where this guide or the tests demonstrate it. Test names are in `tests/`; each also runs with `pytest -k <name>`.

### Required behavior

| Brief requirement | Demonstrated by | Test |
|---|---|---|
| Accept newer versions without older or repeated updates regressing state | [Scenario 2](#scenario-2-missing-or-late-version) | `test_2_version_never_regresses_and_v12_is_never_shown` |
| Acknowledge a valid request even if it doesn't advance state; distinguish accepted/current, duplicate and stale; report the current version | [Scenarios 1](#scenario-1-concurrent-duplicate) and [2](#scenario-2-missing-or-late-version): `201 accepted`, `200 duplicate`, `200 stale`, each with `current_version` | `test_ingest.py`: `test_accepted_*`, `test_duplicate_*`, `test_stale` |
| State and justify HTTP status codes | The codes are shown above; the reasoning is in [design.md §4](design.md) | `test_ingest.py` asserts each code and exact body |
| Don't wait for AI generation before acknowledging | [Step 2](#2-start-and-verify): `201`, `job_status: queued`, then `processing` | `test_accepted_new_encounter` |
| Accepted work survives service restarts | [Scenario 3](#scenario-3-crash-after-saving) (API killed), [4](#scenario-4-worker-interruption) (worker killed) | `test_4_crash_around_the_ingestion_commit_loses_no_work` |
| Retrieve summary progress and results | [Step 2](#2-start-and-verify): `processing`, then `ready`; [7b](#7b-retry-exhaustion-inspection-and-redrive): `failed` | `test_read.py` |
| A summary is tied to the exact version it describes; an older result never appears as current | [Scenarios 5](#scenario-5-newer-version-during-processing) and [6](#scenario-6-results-out-of-order): `summary_version`, and v12 `superseded` | `test_6_forced_results_out_of_order`, `test_2_*` |
| Immutable input per job; a worker can never summarise the wrong version | Each job freezes its transcript in `summary_jobs.input_transcription` at acceptance; the worker never reads `encounters` | `test_happy_path`, `test_precall_check_skips_obsolete_job_without_calling` |
| Obsolete work: cancelled, superseded or completed, but never current; cost trade-off | [Scenario 5](#scenario-5-newer-version-during-processing): an in-flight call completes and is kept as `superseded`; [scenario 2](#scenario-2-missing-or-late-version): a not-yet-started job is skipped with no paid call | `test_in_flight_result_for_obsolete_version` |
| Store `patient_id` and `encounter_type` with the encounter | [Scenario 8](#scenario-8-inconsistent-identity-fields) stored state; every GET body | `test_identity_conflict_patient`, `test_identity_conflict_encounter_type` |
| SLA under 10 s; detect and handle breaches without treating them as failures | [Step 2](#2-start-and-verify): `sla_breached: false`; [7a](#7a-the-outage): `sla_breached: true` while still `processing`; the [SLA gauges](#metrics) | `test_read.py` (`sla_breached` either side of 10 s), `test_queue_sla_status_and_breaker_gauges` |

### Submission items

| Brief item | Demonstrated by | Test |
|---|---|---|
| API examples for accepted/current, duplicate, stale, processing, ready and failed | Step 2 and scenarios 1, 2 and 7b print each one; the contract is in [README](../README.md#api) | `test_ingest.py`, `test_read.py` |
| Uniqueness for `event_id` and `(encounter_id, version)` | [Scenario 1](#scenario-1-concurrent-duplicate) (`event_id`), [scenario 9](#scenario-9-extra-payload-conflict) (same version, new `event_id`) | `test_duplicate_source_defect_fresh_event_id` |
| Transaction boundaries, concurrency handling, stale-result protection, retry behaviour | Scenarios 1–7; the code map is in [guarantees.md](guarantees.md) | `test_no_transaction_or_lock_held_during_the_call`, `test_concurrent_workers_never_claim_the_same_job` |
| At-least-once execution and the per-call cost of a non-idempotent call | [Scenario 4](#scenario-4-worker-interruption) (a crash costs a second call, recorded), [7a](#7a-the-outage) (bounded spend), [7b](#7b-retry-exhaustion-inspection-and-redrive) (budget of 5) | `test_7_stalled_worker_is_fenced`, `test_budget_counts_crashes_and_transient_errors_together` |

### The eight failure scenarios

| Brief scenario | Guide | Test |
|---|---|---|
| Concurrent duplicate | [Scenario 1](#scenario-1-concurrent-duplicate) | `test_1_concurrent_duplicate_accepted_exactly_once` |
| Missing or late version | [Scenario 2](#scenario-2-missing-or-late-version) | `test_2_version_never_regresses_and_v12_is_never_shown` |
| Crash after saving | [Scenario 3](#scenario-3-crash-after-saving) | `test_4_crash_around_the_ingestion_commit_loses_no_work` |
| Worker interruption | [Scenario 4](#scenario-4-worker-interruption) | `test_7_stalled_worker_is_fenced`, `test_reclaim_closes_abandoned_attempt_and_retries`, `test_poison_job_fails_on_reclaim_after_five_crashes` |
| Newer version during processing | [Scenario 5](#scenario-5-newer-version-during-processing) | `test_in_flight_result_for_obsolete_version_is_superseded`, `test_precall_check_skips_obsolete_job_without_calling` |
| Results out of order | [Scenario 6](#scenario-6-results-out-of-order) | `test_6_forced_results_out_of_order` |
| AI outage and retry exhaustion | [7a](#7a-the-outage), [7b](#7b-retry-exhaustion-inspection-and-redrive) | `test_5_outage_costs_bounded_calls_and_loses_no_work`, `test_backoff_doubles_and_fifth_failure_fails_the_job`, `test_redrive.py` |
| Inconsistent identity fields | [Scenario 8](#scenario-8-inconsistent-identity-fields) | `test_3_identity_race_on_a_brand_new_encounter`, `test_identity_conflict_*` |

### Verification

| Brief item | Demonstrated by |
|---|---|
| 4–6 meaningful test cases, including a concurrency case and a crash/retry case | [The seven headline tests](#headline-tests-only), with concurrency in 1–3 and crash/retry in 4 and 7 |
| Metrics, timestamps, alerts or logs for a stuck or SLA-breaching summary, without patient content | [Looking inside](#5-looking-inside): metrics, the privacy check and the attempt history; the alerts and triage table are in [operations.md](operations.md#alerts) |
| Assumptions and known limitations | [DECISIONS.md](../DECISIONS.md) (every interpretation of the design) and [README](../README.md#known-limitations) |
