# Review guide

A hands-on walkthrough for evaluating this service at a terminal. The brief's eight failure scenarios, plus payload conflict, each run as one script that prints the HTTP responses and the stored state. For each one, this guide gives the command, the output to expect, and what it proves.

**Every output block was captured from a real run on a fresh stack** (`docker compose down -v`, then `docker compose up --build`), in this guide's order. Some values differ between runs:

- timestamps, `job_id`s and summary wording (the mock provider is deliberately non-idempotent);
- a few timing-dependent values, flagged where they appear.

Everything else, including status codes, outcomes, versions and job and attempt statuses, should match exactly.

**Contents:** [1. Before you start](#1-before-you-start) · [2. Start and verify](#2-start-and-verify) · [3. The failure scenarios](#3-the-failure-scenarios) · [4. Running the tests](#4-running-the-tests) · [5. Looking inside](#5-looking-inside) · [6. Troubleshooting](#6-troubleshooting) · [7. Requirement map](#7-requirement-map)

## 1. Before you start

- **Prerequisites:** Docker with Compose v2.20 or later, plus bash and curl. You don't need Python, jq or `make` on the host. On Windows, use Git Bash or WSL, not PowerShell. Run everything from the repository root.
- **Free ports:** 8000 (API) and 8001 (mock-ai, so the scripts can flip its outage switch). To use others, see [Troubleshooting](#6-troubleshooting).
- **Reset:** `docker compose down -v && docker compose up --build -d --wait` empties the database and restarts mock-ai with default settings. You don't need to reset between scenarios: each uses its own IDs, so they run in any order. To rerun one, use `RUN=2 scripts/<name>.sh`, which gives fresh IDs.

**How long it takes** (measured times from the captured run; the first build also downloads images, depending on your network):

| Path | Running commands | With reading |
|---|---|---|
| **5-minute essentials:** [step 2](#2-start-and-verify), scenarios [1](#scenario-1-concurrent-duplicate), [2](#scenario-2-missing-or-late-version), [5](#scenario-5-newer-version-during-processing), [6](#scenario-6-results-out-of-order), [8](#scenario-8-inconsistent-identity-fields) and [9](#scenario-9-extra-payload-conflict), then the [headline tests](#4-running-the-tests) | about 3 min | about 5 minutes |
| **Full guide:** adds scenarios 3, 4 and 7 (worker interruption and the outage are the slow ones) and the full suite | about 12 min | about 20 minutes |

## 2. Start and verify

```sh
docker compose up --build -d --wait     # returns once db, api and mock-ai are healthy
scripts/verify_stack.sh
```

```text
== Health
GET  /healthz -> 200 {"status":"ok"}

== Schema applied at startup
circuit_breaker, encounter_events, encounters, job_attempts, summary_jobs

== One encounter, accepted, then summarised in the background
POST v1   evt-verify-1 -> 201 {"outcome":"accepted","encounter_id":"enc-verify-1","version":1,"current_version":1,"job_status":"queued"}
GET  enc-verify-1 -> 200 status=processing current_version=1 sla_breached=false
GET  enc-verify-1 -> 200 {"encounter_id":"enc-verify-1","patient_id":"pat-33","encounter_type":"Appointment","current_version":1,"status":"ready","summary_version":1,"summary":"Visit summarised; follow-up actions noted. Source length: 23 words. Ref 1.","accepted_at":"2026-09-29T04:28:05Z","completed_at":"2026-09-29T04:28:13Z","sla_breached":false}

== Validation happens before any database work
POST version 0   -> 400 {"error":"invalid_request","detail":"version must be a positive integer"}
POST 1.1 MB body -> 413 {"error":"payload_too_large","max_bytes":1048576}
```

**Proves:**

- The schema was applied at startup.
- A POST is acknowledged (`201`, `job_status: queued`) without waiting for the AI.
- GET reports `processing` until the summary exists, then `ready`, tied to the exact `summary_version`, within the 10 s SLA.
- Invalid and oversized requests are rejected before any database work.

This is the only place the full GET body is printed. The scripts condense GET to the fields that change between states; set `VERBOSE=1` to see the whole body.

## 3. The failure scenarios

These run in the brief's order, plus payload conflict. The scripts switch off mock-ai's random failures while they run, so the output repeats, and they restore its settings on exit, including on Ctrl-C.

The last column of the table below selects the pytest tests that force the same situation deterministically, using barriers, lock holds, crash injection or a scripted mock. Run them with:

```sh
docker compose run --rm api python -m pytest -k '<expression>'
```

| # | Brief scenario | Script | Time | Forced by (`-k`) |
|---|---|---|---|---|
| 1 | Concurrent duplicate | `scenario_concurrent_duplicate.sh` | 4 s | `test_1` |
| 2 | Missing or late version | `scenario_late_version.sh` | 9 s | `test_2` |
| 3 | Crash after saving | `scenario_crash_after_saving.sh` | 23 s | `test_4` |
| 4 | Worker interruption | `scenario_worker_interruption.sh` | 71 s | `test_7 or reclaim` |
| 5 | Newer version during processing | `scenario_newer_version_during_processing.sh` | 20 s | `test_in_flight_result_for_obsolete_version` |
| 6 | Results out of order | `scenario_results_out_of_order.sh` | 17 s | `test_6` |
| 7a | AI outage | `scenario_ai_outage.sh` | 171 s | `test_5` |
| 7b | Retry exhaustion, inspect, redrive | `scenario_retry_exhaustion.sh` | 157 s | `fifth_failure or redrive_current or redrive_obsolete` |
| 8 | Inconsistent identity fields | `scenario_identity_conflict.sh` | 3 s | `test_3` |
| 9 | Payload conflict | `scenario_payload_conflict.sh` | 4 s | `payload_conflict` |

### Scenario 1: Concurrent duplicate

**The situation:** the partner delivers the same event to two API instances at the same moment.

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
```

**Proves:** of ten simultaneous identical requests, exactly one is accepted, for both a brand-new encounter and an existing one. The other nine get `200 duplicate` with the current version, and nothing is stored twice. The `event_id` primary key decides, not a read-then-insert.

Ten parallel requests to one API are an approximation. `test_1` makes the race certain: two app instances are released from a barrier, and the first transaction is held after it takes the row lock until Postgres shows the second one waiting. It runs 200 iterations for each kind of encounter.

### Scenario 2: Missing or late version

**The situation:** a newer version skips ahead of the stored one, and the version in between turns up late or not at all.

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
GET  enc-late-1 -> 200 status=processing current_version=12 sla_breached=false
GET  enc-late-1 -> 200 status=ready current_version=12 summary_version=12 sla_breached=false

== Stored state: current_version 12, v11 never stored, v10's job superseded
 current_version | version |   status   | superseded_by_version
-----------------+---------+------------+-----------------------
              12 |      10 | superseded |                    12
              12 |      12 | ready      |
```

**Proves:**

- A gap is fine: v12 is accepted over v10.
- The late v11 is acknowledged as `200 stale` with `current_version: 12`, and never stored, so the version never regresses.
- A retry of v10 is still a `duplicate`.
- v10's job ends `superseded`, so the client only ever sees v12.

Depending on timing, v10's job was either skipped before any paid call, or its in-flight result was written as superseded.

### Scenario 3: Crash after saving

**The situation:** the API dies right after saving an update, before any worker has picked up the work.

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
GET  enc-crash-1 -> 200 status=ready current_version=1 summary_version=1 sla_breached=true

== Stored state: one job, summarised once
 version | status | attempts
---------+--------+----------
       1 | ready  |        1
```

**Proves:** there is no hand-off to lose. The job row commits in the same transaction as the update, so once the API has said `201`, the work is in Postgres, even with the API killed and no worker running. The partner's retry after the restart is a harmless `duplicate`, and the job is summarised once.

`sla_breached` depends on how long the restart takes. It's reported, not treated as a failure.

**What the script can't do:** kill the API between the job insert and the COMMIT. `test_4` does that. It kills a real uvicorn process with `os._exit` just before and just after COMMIT, then retries. Before COMMIT, nothing is stored and the retry is accepted; after it, the retry is a duplicate. Either way, there is exactly one job and one summary.

### Scenario 4: Worker interruption

**The situation:** a worker dies mid-call, or between writing its result and marking the job done.

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
GET  enc-interrupt-1 -> 200 status=processing current_version=1 sla_breached=true

== Waiting for the lease to expire and a worker to reclaim the job (about a minute)
GET  enc-interrupt-1 -> 200 status=ready current_version=1 summary_version=1 sla_breached=true

== Stored state: the dead attempt closed as lease_expired, the reclaim succeeded
 attempt_no |    outcome    | error_class
------------+---------------+-------------
          1 | lease_expired | worker_lost
          2 | succeeded     |
```

**Proves:** a worker killed mid-call loses nothing. Its 60 s lease expires, and a worker reclaims the job and bumps its fencing counter. The dead attempt is closed as `lease_expired`, and it counts against the retry budget, so a transcript that crashes every worker can't loop forever.

**"After saving a result but before acknowledging"** has no gap to fall into. The result write *is* the acknowledgement: one fenced `UPDATE` sets the status, the summary and `completed_at` together.

**The dangerous variant can't be timed by hand:** a worker that stalls past its lease and then returns. `test_7` does it with a real lease expiry. The late write matches no row (`attempts = my_attempt` fails), so it is marked `discarded`, and the reclaiming worker's summary stands.

### Scenario 5: Newer version during processing

**The situation:** a newer version is accepted while the previous one's summary call is still running.

```sh
scripts/scenario_newer_version_during_processing.sh
```

```text
== v12's AI call takes 4 s. Post v12 and wait until it is mid-call
POST v12  evt-newer-1-12 -> 201 {"outcome":"accepted","encounter_id":"enc-newer-1","version":12,"current_version":12,"job_status":"queued"}

== v13 arrives while v12 is being summarised; its call will take 7 s
POST v13  evt-newer-1-13 -> 201 {"outcome":"accepted","encounter_id":"enc-newer-1","version":13,"current_version":13,"job_status":"queued"}
GET  enc-newer-1 -> 200 status=processing current_version=13 sla_breached=false

== v12's call finishes first: its result is stored as superseded, and GET still waits for v13
GET  enc-newer-1 -> 200 status=processing current_version=13 sla_breached=false

== v13's call finishes
GET  enc-newer-1 -> 200 status=ready current_version=13 summary_version=13 sla_breached=false

== Stored state: v12 paid for once and kept for history, marked superseded by 13
 version |   status   | superseded_by_version | has_summary |  attempt
---------+------------+-----------------------+-------------+------------
      12 | superseded |                    13 | t           | superseded
      13 | ready      |                       | t           | succeeded
```

**Proves:**

- v12's paid call is allowed to finish, since cancelling it saves nothing. Its result is kept, marked `superseded_by_version = 13` in the same statement that writes it.
- GET resolves the summary through `encounters.current_version`, so it reports v13 as `processing` until v13's own summary is ready. It never shows v12's.
- If v13 arrives *before* v12's call starts, no call is made at all: see `test_precall_check_skips_obsolete_job_without_calling`.

### Scenario 6: Results out of order

**The situation:** the newer version's summary finishes before the older one's.

```sh
scripts/scenario_results_out_of_order.sh
```

```text
== v12's AI call will take 12 s. Post v12 and wait until it is mid-call
POST v12  evt-order-1-12 -> 201 {"outcome":"accepted","encounter_id":"enc-order-1","version":12,"current_version":12,"job_status":"queued"}

== Make calls take 1 s and post v13: its summary is ready while v12's call is still running
POST v13  evt-order-1-13 -> 201 {"outcome":"accepted","encounter_id":"enc-order-1","version":13,"current_version":13,"job_status":"queued"}
GET  enc-order-1 -> 200 status=ready current_version=13 summary_version=13 sla_breached=false
 version |   status
---------+------------
      12 | processing
      13 | ready

== v12's result arrives last and is stored as superseded; GET is unchanged
GET  enc-order-1 -> 200 status=ready current_version=13 summary_version=13 sla_breached=false

== Stored state
 version |   status   | superseded_by_version |  attempt   | finished
---------+------------+-----------------------+------------+----------
      13 | ready      |                       | succeeded  |        1
      12 | superseded |                    13 | superseded |        2
```

**Proves:** v13's summary becomes current as soon as it's ready. v12's result arrives about 10 s later and is stored as `superseded`, and GET doesn't change. An older result never overwrites a newer one, whatever order the calls finish in.

The script forces the order through mock-ai, which picks each call's latency as the call arrives: v12's call is given 12 s, then v13's is given 1 s.

### Scenario 7: AI outage and retry exhaustion

**The situation:** the AI provider is down for a long stretch. What do retries cost, what happens when they run out, how is failed work inspected and redriven, and what does the client see meanwhile?

These are two different situations:

- **7a, a full outage:** handled by the shared circuit breaker, and no job fails.
- **7b, one job's budget running out:** the job ends `failed`, then it's inspected and redriven.

#### 7a. The outage

This is 40 jobs with 3 worker containers (12 loops). The provider fails every call for about 90 s, and the timings are production values, not compressed.

```sh
scripts/scenario_ai_outage.sh
```

```text
== Three worker containers: 12 worker loops
docker compose up -d --wait --scale worker=3 worker: done

== Reset mock-ai's call counters and switch it into an outage

== Post 40 encounters
     40 201

== Outage: sample every 30 s. Real calls stop at the trip; probes go up by one per cooldown
t=  7s  breaker=open      probes_sent=0  jobs: queued=40            mock-ai calls: real=13 probe=0
t= 35s  breaker=open      probes_sent=1  jobs: queued=40            mock-ai calls: real=13 probe=1
t= 65s  breaker=open      probes_sent=2  jobs: queued=40            mock-ai calls: real=13 probe=2
t= 95s  breaker=open      probes_sent=3  jobs: queued=40            mock-ai calls: real=13 probe=3

== What a client and the metrics show during the outage
GET  enc-outage-1-1 -> 200 status=processing current_version=1 sla_breached=true
summary_sla_breaching_jobs 40.0
summary_jobs{status="failed"} 0.0
breaker_state{state="open"} 1.0

== End the outage. The next probe closes the breaker, then the queue drains
t=154s  breaker=closed    probes_sent=4  jobs: ready=40             mock-ai calls: real=53 probe=4

== Stored state: every paid call on these 40 jobs, by outcome
     outcome     |  error_class   | count
-----------------+----------------+-------
 succeeded       |                |    40
 transient_error | ai_unavailable |    13

== Back to one worker container
docker compose up -d --wait --scale worker=1 worker: done
```

**Proves:**

- **An outage's cost is bounded by the breaker, not by the size of the queue.** Real calls stopped as soon as the breaker tripped: 13 here, meaning the 11 failures that tripped it plus 2 already in flight. Without the breaker, 40 jobs × 5 attempts could have cost 200 paid calls.
- **While the breaker is open, workers claim nothing**, so no job's budget is spent. One synthetic probe per 30 s cooldown checks the provider. mock-ai counts probes separately, so no transcript is sent during the outage.
- **Throughout, the client sees `processing` with `sla_breached: true`**, and POSTs are still accepted. The breach shows in the metrics without failing anything.
- **After recovery, all 40 jobs reach `ready`, and none fail.**

**Varies between runs:** the real-call count at the trip is 11 plus any calls already in flight; the four runs made for this guide gave 11, 13, 13 and 13. The probe count depends on where the cooldown falls. `probes_sent` is a running total.

#### 7b. Retry exhaustion, inspection and redrive

A job fails only when its own budget runs out, for example when the provider is failing *these* calls but the breaker correctly stays closed. Here, two jobs fail 10 calls between them, which is below the breaker's 11.

```sh
scripts/scenario_retry_exhaustion.sh
```

```text
== The provider fails every call. Post two encounters
POST v1   evt-exhaust-1-a1 -> 201 {"outcome":"accepted","encounter_id":"enc-exhaust-1-a","version":1,"current_version":1,"job_status":"queued"}
POST v1   evt-exhaust-1-b1 -> 201 {"outcome":"accepted","encounter_id":"enc-exhaust-1-b","version":1,"current_version":1,"job_status":"queued"}
GET  enc-exhaust-1-a -> 200 status=processing current_version=1 sla_breached=false

== Waiting for both jobs to spend their budget of 5 (about 2 minutes)
GET  enc-exhaust-1-a -> 200 status=failed current_version=1 attempts=5 error_class=ai_timeout sla_breached=true

== Inspect: the attempt history, one row per paid call, no patient content
 attempt_no | redrive_generation |     outcome     |  error_class   | backoff_s
------------+--------------------+-----------------+----------------+-----------
          1 |                  0 | transient_error | ai_timeout     |
          2 |                  0 | transient_error | ai_unavailable |         7
          3 |                  0 | transient_error | ai_unavailable |        17
          4 |                  0 | transient_error | rate_limited   |        39
          5 |                  0 | transient_error | ai_timeout     |        79

== Fix the provider. Meanwhile enc-exhaust-1-b gets a newer version, which succeeds
POST v2   evt-exhaust-1-b2 -> 201 {"outcome":"accepted","encounter_id":"enc-exhaust-1-b","version":2,"current_version":2,"job_status":"queued"}

== Redrive both failed jobs: enc-exhaust-1-a's is still current, enc-exhaust-1-b's is not
POST /admin/jobs/52/redrive (enc-exhaust-1-a v1) -> 200 {"job_id":52,"outcome":"redriven"}
POST /admin/jobs/53/redrive (enc-exhaust-1-b v1) -> 200 {"job_id":53,"outcome":"superseded","superseded_by_version":2}
GET  enc-exhaust-1-a -> 200 status=ready current_version=1 summary_version=1 sla_breached=true

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

- **Retries are budgeted.** Each job gets 5 paid attempts with jittered exponential backoff: `uniform(0.5, 1) × 10 s × 2^(n−1)`, so each gap falls in the 5–10, 10–20, 20–40 or 40–80 s band. Then the job is `failed`.
- **The client sees exactly what happened:** `failed`, `attempts=5` and the last `error_class`. There's no patient content and no raw provider error.
- **`failed` waits for a person.** The attempt history is the inspection tool: every paid call, its outcome and its error class.
- **Redrive is safe.**
  - A job that is still current gets a fresh budget (`redrive_generation` 1).
  - A job whose encounter has moved on is marked `superseded` and never paid for again.
  - A job that isn't `failed` returns `409`.

**Varies between runs:** the error classes (mock-ai picks one at random), the backoff seconds within their bands, and the `job_id`s.

Bulk redrive, for everything that failed in a time window, takes `POST /admin/jobs/redrive` with `{"failed_from": "...", "failed_to": "..."}`, and applies the same two rules in one transaction.

### Scenario 8: Inconsistent identity fields

**The situation:** a later event for an encounter carries a different patient or encounter type from the stored one.

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
{"ts": "2026-09-29T04:36:08.461+00:00", "level": "WARNING", "service": "api", "logger": "ingest", "event": "identity_conflict", "event_id": "evt-identity-1-2", "encounter_id": "enc-identity-1", "stored_patient_id": "pat-33", "incoming_patient_id": "pat-99", "stored_encounter_type": "Appointment", "incoming_encounter_type": "Appointment"}
{"ts": "2026-09-29T04:36:08.569+00:00", "level": "WARNING", "service": "api", "logger": "ingest", "event": "identity_conflict", "event_id": "evt-identity-1-2", "encounter_id": "enc-identity-1", "stored_patient_id": "pat-33", "incoming_patient_id": "pat-33", "stored_encounter_type": "Appointment", "incoming_encounter_type": "MedicationRefill"}
```

**Proves:**

- A later event never changes the stored identity. A contradicting event is rejected with `409 identity_conflict`, naming the field, and nothing from it is stored.
- The 409 body contains **neither patient ID**. The log line is the one place `patient_id` appears, on purpose: an operator needs both values to diagnose the partner's mapping bug.
- `test_3` races two *first* events with different patients for a brand-new encounter. The first to commit wins.

### Scenario 9 (extra): Payload conflict

The brief asks the design to decide what happens if the source ever violates its "no conflicting payloads for a version" invariant. Scenario 8 covers the identity invariant.

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

- A version's content can't be rewritten. A different transcript gets `409 payload_conflict`, under either the same `event_id` or a new one.
- A new `event_id` carrying the same content is a harmless `duplicate`. The `(encounter_id, version)` unique constraint catches this source defect.

## 4. Running the tests

The tests run in the app image against a separate `outcomes_test` database on the Compose Postgres: a real Postgres, never SQLite or a fake. `mock-ai` must be up, because one smoke test drives a real worker process against it. Let any scenario script finish first, because that test compares mock-ai's call counts.

```sh
docker compose run --rm api python -m pytest tests/test_headline.py -v    # the seven headline tests, 66 s here
```

```text
tests/test_headline.py::test_1_concurrent_duplicate_accepted_exactly_once[existing_encounter] PASSED [ 12%]
tests/test_headline.py::test_1_concurrent_duplicate_accepted_exactly_once[brand_new_encounter] PASSED [ 25%]
tests/test_headline.py::test_2_version_never_regresses_and_v12_is_never_shown PASSED [ 37%]
tests/test_headline.py::test_3_identity_race_on_a_brand_new_encounter PASSED [ 50%]
tests/test_headline.py::test_4_crash_around_the_ingestion_commit_loses_no_work PASSED [ 62%]
tests/test_headline.py::test_5_outage_costs_bounded_calls_and_loses_no_work PASSED [ 75%]
tests/test_headline.py::test_6_forced_results_out_of_order PASSED        [ 87%]
tests/test_headline.py::test_7_stalled_worker_is_fenced PASSED           [100%]
======================== 8 passed, 1 warning in 57.29s =========================
```

(Filtered to the result lines. Test 1 runs twice: once for an existing encounter, once for a brand-new one.)

```sh
make test     # or: docker compose up -d --build --wait db mock-ai && docker compose run --rm --build api python -m pytest
```

The full suite took 148 s here and ended with:

```text
================== 180 passed, 1 warning in 124.03s (0:02:04) ==================
```

The warning is a harmless Starlette/anyio deprecation notice. What each file covers, and how the harness forces races and crashes, is in [testing.md](testing.md).

## 5. Looking inside

**Logs carry no patient content.** This script posts a transcript containing a unique marker, waits for its summary, and then searches every service's logs and the metrics:

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

== What the logs do say: IDs, statuses and durations
{"ts": "2026-09-29T04:36:20.669+00:00", "level": "INFO", "service": "worker", "logger": "worker", "event": "result_written", "job_id": 58, "encounter_id": "enc-privacy-1", "version": 1, "attempt_no": 1, "worker_id": "8f008e3251ec-1-2", "status": "ready", "completion_seconds": 4.263}
```

Every line is one JSON object, carrying only IDs, statuses, fixed enum values and durations. `patient_id` appears only in the `identity_conflict` line (scenario 8).

**Metrics.** `curl -s localhost:8000/metrics` serves Prometheus metrics. The queue, SLA and breaker gauges are read from Postgres on every scrape, which is the design's scheduled SLA check:

```sh
curl -s localhost:8000/metrics | grep -E '^(summary_jobs|summary_sla_breaching_jobs |breaker_state)'
```

```text
summary_sla_breaching_jobs 0.0
summary_jobs{status="queued"} 0.0
summary_jobs{status="processing"} 1.0
summary_jobs{status="ready"} 50.0
summary_jobs{status="failed"} 0.0
summary_jobs{status="superseded"} 6.0
breaker_state{state="closed"} 1.0
breaker_state{state="open"} 0.0
breaker_state{state="half_open"} 0.0
```

Captured straight after scenario 9, so a job or two may still show as `processing`. Each worker process also serves its own metrics, including `generate_summary_calls_total`, which counts spend by outcome, on internal port 9100. [operations.md](operations.md#metrics) lists every metric and alert, the command to read the worker metrics, and a triage table for a stuck or late summary.

## 6. Troubleshooting

| Problem | Fix |
|---|---|
| Port 8000 or 8001 in use | `API_PORT=18000 MOCK_AI_PORT=18001 docker compose up --build -d --wait`, then `export API_PORT=18000 MOCK_AI_PORT=18001` so the scripts use the same ports |
| A script says "already exists" | The scenario has already run on this database. Rerun it with `RUN=2 scripts/<name>.sh`, or reset |
| A script times out (slow machine) | Rerun it with the next `RUN` number, and check `docker compose logs worker`. The scripts wait up to 30 s for a normal summary, 120 s for a reclaim and 240 s for retry exhaustion |
| A test fails on a loaded machine | The outage and lease tests use compressed time (sub-second leases and cooldowns). Rerun the failing test alone with `-k <name>` |
| A script was killed with `kill -9` | `docker compose restart mock-ai` restores its defaults. After the outage script, also run `docker compose up -d --scale worker=1` |
| No `make` (Windows) | `make up` = `docker compose up --build -d --wait`. `make test` = the command in [section 4](#4-running-the-tests). The scripts need Git Bash or WSL, and `.gitattributes` keeps them LF |
| Docker Desktop: `dialing ... :2376` after idling | Resource Saver stopped the engine. Quit Docker Desktop, run `wsl --shutdown`, and restart it |
| Apple Silicon | Should work, but hasn't been run on one. Both base images publish arm64 builds, and all 29 Python packages resolve as Linux arm64 wheels (`pip download --only-binary=:all: --platform manylinux2014_aarch64`) |

## 7. Requirement map

Where each requirement in the brief is demonstrated. Tests are in `tests/`, and each can be run with `pytest -k <name>`.

| Brief requirement | Guide | Test |
|---|---|---|
| Newer versions accepted; older or repeated ones never regress state | [2](#scenario-2-missing-or-late-version) | `test_2_version_never_regresses_and_v12_is_never_shown` |
| Accepted/current, duplicate and stale told apart; current version reported; status codes | [1](#scenario-1-concurrent-duplicate), [2](#scenario-2-missing-or-late-version); reasoning in [design.md §4](design.md) | `test_ingest.py` (exact codes and bodies) |
| Acknowledge without waiting for the AI | [Step 2](#2-start-and-verify) | `test_accepted_new_encounter` |
| Accepted work survives restarts | [3](#scenario-3-crash-after-saving), [4](#scenario-4-worker-interruption) | `test_4_crash_around_the_ingestion_commit_loses_no_work` |
| Progress and results retrievable: processing, ready, failed | [Step 2](#2-start-and-verify), [7b](#7b-retry-exhaustion-inspection-and-redrive) | `test_read.py` |
| Summary tied to its exact version; an older result is never current | [5](#scenario-5-newer-version-during-processing), [6](#scenario-6-results-out-of-order) | `test_6_forced_results_out_of_order` |
| Immutable job input; a worker never summarises the wrong version | Transcript frozen in `summary_jobs.input_transcription`; the worker never reads `encounters` | `test_precall_check_skips_obsolete_job_without_calling` |
| Obsolete work handled, and its cost trade-off | [5](#scenario-5-newer-version-during-processing) (completes, kept as superseded), [2](#scenario-2-missing-or-late-version) (skipped, never paid) | `test_in_flight_result_for_obsolete_version_is_superseded` |
| `patient_id` and `encounter_type` stored with the encounter; conflicts handled | [8](#scenario-8-inconsistent-identity-fields) | `test_3_identity_race_on_a_brand_new_encounter` |
| Uniqueness of `event_id` and `(encounter_id, version)` | [1](#scenario-1-concurrent-duplicate), [9](#scenario-9-extra-payload-conflict) | `test_1_*`, `test_duplicate_source_defect_fresh_event_id` |
| SLA under 10 s; breach detected but not a failure | [Step 2](#2-start-and-verify), [7a](#7a-the-outage), [metrics](#5-looking-inside) | `test_queue_sla_status_and_breaker_gauges` |
| Worker crash, reclaim, stale write fenced | [4](#scenario-4-worker-interruption) | `test_7_stalled_worker_is_fenced` |
| Outage: backoff, per-call cost, exhaustion, inspection, redrive, what the client sees | [7a](#7a-the-outage), [7b](#7b-retry-exhaustion-inspection-and-redrive) | `test_5_*`, `test_backoff_doubles_and_fifth_failure_fails_the_job`, `test_redrive.py` |
| 4–6 test cases, including concurrency and crash/retry | [Section 4](#4-running-the-tests) | `test_headline.py` (1–3 concurrency, 4 and 7 crash) |
| Investigating a stuck or SLA-breaching summary without patient content | [Section 5](#5-looking-inside), [operations.md](operations.md#triage-for-a-stuck-or-late-summary) | `test_logs_never_carry_patient_content` |
| Assumptions and known limitations | [DECISIONS.md](../DECISIONS.md), [README](../README.md#known-limitations) | |
