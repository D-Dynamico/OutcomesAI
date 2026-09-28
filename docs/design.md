# Reliable Encounter Updates & AI Summaries

**Author:** Dayanand Kori
**Status:** submitted (this is the full design; the submitted PDF is a condensed version of it)

---

## 0. Assumptions

- Code is Postgres SQL plus language-neutral pseudocode; the design does not depend on the service's language or framework.
- Postgres as the datastore. The design leans on four relational features: unique constraints as the arbiter of duplicates, conditional updates for compare-and-swap, a transaction spanning state and work, and `FOR UPDATE SKIP LOCKED` so concurrent workers can claim jobs from a table without blocking each other. Any RDBMS with all four works with minor syntax changes; the last is the one not every database supports. A document store would be a poor fit here, since race-safe duplicate detection and single-transaction ingestion are exactly what it makes awkward.
- The partner retries failed deliveries, reusing the same `event_id` (stated in the brief). Its exact retry policy is not specified, so response codes are chosen so that any conventional policy converges: 2xx means the event has been dealt with, 4xx means resending will not help, and 5xx means a transient server-side failure the partner should retry. The design remains correct if the partner retries more aggressively than that, since duplicate handling is idempotent.
- `generate_summary` is an external call with no cancellation API. Once a call is in flight, its cost is already incurred.
- The worker can enforce a client-side timeout on `generate_summary` (30 seconds, section 5) and abandon a call that exceeds it. Abandoning a call does not stop the provider from completing it, so an abandoned call is assumed to be billed. The lease rule in section 5 (lease > AI timeout + margin) depends on this timeout being enforceable.
- One clock. Every time comparison (leases, backoff, breaker cooldowns, SLA) uses the database's `now()`, never a worker's local clock, so correctness does not depend on worker clocks agreeing. `sla_breached` is computed from the same clock at read time.
- One shared AI dependency. All workers call the same `generate_summary` provider under a shared quota, so an outage or rate limit affects them all at once. This is what justifies a single, shared circuit breaker (section 5) rather than one per worker.
- The first example payload in the brief is labelled "Appointment (annual follow-up visit)" but carries `encounter_type: "TelephoneTriage"`. Treated as a formatting slip in the document; the design does not depend on which is correct.
- Transcripts are large (hundreds of lines). Storage decisions are made on that basis. Two further consequences:
  - **Ingestion enforces a request body size limit** (for example 1 MB), returning `413 Payload Too Large` above it. The transcript is accepted inline rather than by reference, since the brief passes it inline; a by-reference upload is the upgrade path if real transcripts outgrow the limit.
  - **`generate_summary` latency scales with transcript length.** Long transcripts are the most likely cause of an SLA breach, and a run of them backs up the queue faster than average-case timings suggest. Worker concurrency and queue depth are therefore first-class metrics (section 7).
- Authentication and authorization are out of scope. The brief specifies no access-control model, so both endpoints are designed as if the caller is already authorized: the GET returns `patient_id` and `encounter_type` alongside the summary because a clinical UI needs them, and the POST accepts any well-formed event. In a real deployment these would sit behind authenticated, authorized, audited access, and the response shape would be narrowed to whatever the caller's role permits. See section 8.

**Known limitations:** listed in section 8.

---

## 1. Ingestion outcomes

Every well-formed incoming event resolves to exactly one of five outcomes. Everything else in the design exists to make this classification correct under concurrency. (A request that is not well-formed never reaches classification: an oversized body gets `413`, and a missing field, a non-positive `version`, or an unknown `encounter_type` gets `400`. Nothing is stored for either.)

| Outcome | Condition | Stored state changes? | Job created? |
|---|---|---|---|
| Accepted / current | `version > current_version` and event not seen before | Yes | Yes |
| Duplicate | `event_id` or `(encounter_id, version)` seen before, **with the same payload hash** | No | No |
| Payload conflict | `event_id` or `(encounter_id, version)` seen before, **with a different payload hash** | No | No |
| Stale | `version <= current_version` and event not seen before | No | No |
| Identity mismatch | `patient_id` or `encounter_type` contradicts stored values | No | No |

### Duplicate has two doors

The brief distinguishes two ways an event can be a duplicate, and both are caught by constraints rather than application logic:

1. **Plain retry** — same `event_id` arrives again. Normal, expected.
2. **Source defect** — different `event_id`, same `(encounter_id, version)`. The partner re-sent the same logical update under a fresh ID. Signals an upstream bug.

Both return `duplicate` to the client, provided the payload matches what was recorded (see below). The second is logged at a higher severity because it indicates a partner defect rather than normal retry behaviour.

### Precedence

The classification checks identity first, then event/version uniqueness (duplicate or payload conflict, decided by payload hash), then the version comparison.

**Duplicate beats stale.** These two can overlap. If v12 is accepted, then v13 is accepted, then a retry of v12 arrives, the version comparison says stale but the event has genuinely been seen before. `duplicate` tells the caller "you already delivered this successfully"; `stale` tells them "this is old news you never delivered". Different facts, different debugging paths.

Boundary case: `version == current_version` with an unseen `event_id` is a duplicate by the `(encounter_id, version)` rule, not an accept (or a payload conflict, if its transcript differs).

### Identity mismatch: why the check exists

The brief states the source does not send conflicting `patient_id` / `encounter_type` for an encounter, then immediately asks what the design does if that invariant is violated. The check exists *because* the brief says it shouldn't be needed.

An upstream mapping bug that rebinds an encounter to a different patient would attach a clinical transcript to the wrong patient record. "The partner promised it won't happen" is not a safety property.

**Policy: first patient wins.** Once `enc-42` is bound to `pat-77`, nothing can rebind it. A contradicting event is rejected permanently, never accepted-with-alert.

`encounter_type` mismatch is treated identically to `patient_id` mismatch. Any contradiction of fixed identity means the partner's mapping is wrong; splitting the handling adds complexity without adding safety.

Rejected events are logged (structured entry with both identity values and all IDs) rather than quarantined in a table. A log line is enough to debug a partner bug without building a second storage path. The rejection also increments a dedicated metric so it surfaces separately from ordinary validation noise.

### Payload conflict: the second invariant

The brief states a second invariant and asks the same question of it: the source does not send conflicting payloads for the same `(encounter_id, version)`. The same policy applies: **the first recorded payload is the one of record.** A later event for that version with different content is rejected with `409 Conflict`. The first may already have been summarized and served, so replacing it would silently change what a clinician may already have read.

**Detection.** Each event row stores `payload_hash`, the SHA-256 of the transcription. On a duplicate hit, the incoming hash is compared with the stored one: same hash is a genuine duplicate (`200`), different hash is a payload conflict (`409`).

- **Hash the transcription string, not the raw JSON body.** A re-serialised but identical body (key order, whitespace) would otherwise raise a false `409`. Identity is already compared before this point and `version` is part of the key, so the transcription is the only field that can conflict.
- **The event table still holds no patient content.** A hash is not reversible to the transcript.
- **Separate outcome value.** `payload_conflict`, not `identity_conflict`: the two point to different partner bugs, so they get separate outcome values and metrics.
- **One undetectable case.** A conflicting payload for a version that was rejected as stale cannot be detected, because stale events are not stored. This is harmless, since that version never affects state.

---

## 2. Data model

### Why four tables

Each table is forced by a distinct requirement, and each has a distinct cardinality and mutability.

| Table | Cardinality | Mutability | Forced by |
|---|---|---|---|
| `encounters` | 1 per encounter | Mutable (in place) | "Store latest state; older updates must not regress it" |
| `encounter_events` | Many per encounter | Append-only | "Events may arrive more than once, **concurrently**" |
| `summary_jobs` | 1 per accepted version | Controlled lifecycle | "Do not wait for AI before acknowledging" + "accepted work must survive restarts" |
| `job_attempts` | Many per job | Append-only (outcome filled in once) | "`generate_summary` incurs a cost on every invocation, including retries" |

**How the model evolved.** The design started with three tables. A single `attempts` counter on `summary_jobs` was carrying three roles at once: fencing token, retry budget, and count of paid calls. Working through redrive showed those roles conflict (redrive must refill the budget but must never move the fencing token backwards), and that per-attempt cost auditing had no home. `job_attempts` resolves both. It passes the same test as the other three: attempts are genuinely many-per-job, a cardinality no existing table had.

**How identity relates to encounters and summaries.** `patient_id` and `encounter_type` are stored once, on `encounters`, keyed by `encounter_id`, and written only when the encounter is first accepted. Events and jobs do not repeat them; a summary reaches its patient through `summary_jobs.encounter_id → encounters`. Because these fields can never change (section 6.8), every summary for an encounter belongs to the same patient and type.

**Operational table.** A fifth, single-row table, `circuit_breaker`, holds shared breaker state. It is infrastructure rather than domain data (no relationship to any encounter or job) and is defined with the retry logic in section 5.

`encounter_events` exists specifically because duplicates can arrive *concurrently*. An application-level "have I seen this?" check races: two instances both read "no" and both proceed. The only reliable arbiter is a unique constraint, and a constraint needs a row to sit on.

### Alternatives considered and rejected

- **`encounter_versions` table (full history).** The brief offers this explicitly. Rejected: payloads are complete snapshots so superseded versions carry no business value, and transcripts are large so history is expensive. The immutable job snapshot already provides the correctness property history would have given. If audit or retention rules later require it, an append-only archive table can be added without changing any logic.
- **Separate `summaries` table.** One-to-one with jobs, written once, never queried independently. Splitting adds a join on every read plus a failure mode where a job says `ready` but its summary row is missing.
- **`patients` table.** The brief gives no patient attributes beyond the ID, so the table would hold a single column duplicating a foreign key. Would be added the moment patient attributes enter scope.
- **External queue (Redis / SQS).** Breaks the transaction boundary. Committing the encounter update to Postgres and then pushing to a broker loses work permanently if the process dies in between. Keeping jobs in the same database makes both writes one atomic commit. A transactional outbox to a broker is the upgrade path if throughput demands it.
- **`dead_letter` table.** `status = 'failed'` on the job plus its `job_attempts` history already makes failed work queryable and redrivable.

### Schema

```sql
CREATE TYPE encounter_type AS ENUM ('Appointment', 'MedicationRefill', 'TelephoneTriage');
CREATE TYPE job_status     AS ENUM ('queued', 'processing', 'ready', 'failed', 'superseded');

-- One row per encounter. The only mutable table.
CREATE TABLE encounters (
    encounter_id     TEXT PRIMARY KEY,
    patient_id       TEXT           NOT NULL,   -- fixed for life of encounter
    encounter_type   encounter_type NOT NULL,   -- fixed for life of encounter
    current_version  INTEGER        NOT NULL DEFAULT 0 CHECK (current_version >= 0),
                                                -- 0 only inside an ingestion txn (shell row); never committed
    transcription    TEXT,                      -- snapshot at current_version; NULL only while version is 0
    created_at       TIMESTAMPTZ    NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ    NOT NULL DEFAULT now()
);

-- Append-only. Exists to make duplicates a constraint violation, not a race.
CREATE TABLE encounter_events (
    event_id      TEXT PRIMARY KEY,             -- catches plain retries
    encounter_id  TEXT    NOT NULL REFERENCES encounters(encounter_id),
    version       INTEGER NOT NULL CHECK (version > 0),
    payload_hash  BYTEA   NOT NULL,             -- sha256 of transcription; detects payload conflicts
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_encounter_version UNIQUE (encounter_id, version)  -- catches source defects
);

-- One row per accepted version. Input frozen at creation.
CREATE TABLE summary_jobs (
    job_id              BIGSERIAL PRIMARY KEY,
    encounter_id        TEXT    NOT NULL REFERENCES encounters(encounter_id),
    version             INTEGER NOT NULL,
    event_id            TEXT    NOT NULL REFERENCES encounter_events(event_id),
    input_transcription TEXT    NOT NULL,       -- immutable snapshot; worker never reads encounters
    status              job_status  NOT NULL DEFAULT 'queued',
    attempts            INTEGER     NOT NULL DEFAULT 0,  -- fencing token; only ever increases
    redrive_generation  INTEGER     NOT NULL DEFAULT 0,  -- bumped on redrive; scopes the retry budget
    summary             TEXT,                   -- written once on success
    superseded_by_version INTEGER,              -- current_version at the moment this job was marked superseded
    accepted_at         TIMESTAMPTZ NOT NULL DEFAULT now(),  -- SLA clock starts here
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    next_attempt_at     TIMESTAMPTZ NOT NULL DEFAULT now(),  -- backoff scheduling
    lease_expires_at    TIMESTAMPTZ,            -- reclaims jobs from crashed workers
    CONSTRAINT uq_job_encounter_version UNIQUE (encounter_id, version)
);

-- One partial index per branch of the claim query's OR (section 5)
CREATE INDEX idx_jobs_due ON summary_jobs (next_attempt_at)
    WHERE status = 'queued';                   -- queued and due

CREATE INDEX idx_jobs_expired_lease ON summary_jobs (lease_expires_at)
    WHERE status = 'processing';               -- processing with an expired lease

CREATE TYPE attempt_outcome AS ENUM (
    'in_flight',        -- claimed, not yet finished
    'succeeded',        -- result written as ready
    'superseded',       -- result written, but a newer version is current
    'skipped',          -- pre-call check found the job obsolete; no AI call made
    'transient_error',  -- timeout / rate limit / unavailable; job requeued with backoff
    'lease_expired',    -- owner presumed dead; job reclaimed by another worker
    'discarded'         -- call returned after losing ownership; result thrown away
);

-- One row per claim. The per-attempt audit and cost record.
CREATE TABLE job_attempts (
    job_id              BIGINT  NOT NULL REFERENCES summary_jobs(job_id),
    attempt_no          INTEGER NOT NULL,        -- equals summary_jobs.attempts at claim time
    redrive_generation  INTEGER NOT NULL,        -- copied from the job at claim time
    worker_id           TEXT    NOT NULL,
    outcome             attempt_outcome NOT NULL DEFAULT 'in_flight',
    error_class         TEXT,                    -- fixed enum value only, never patient content
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    PRIMARY KEY (job_id, attempt_no)
);
```

### Relationships

- `encounters` → `encounter_events` is one-to-many. One encounter accumulates many events, but only accepted ones: duplicate, payload-conflict, stale, and mismatched events roll back with their transaction, so no row is kept for them (conflicts and mismatches are logged instead, see section 1).
- `encounters` → `summary_jobs` is one-to-many, but fewer than events: only accepted events produce a job.
- `encounter_events` → `summary_jobs` is one-to-one. The `event_id` FK on the job is the audit trail: given any summary, the exact event that caused it is recoverable.
- `summary_jobs` → `job_attempts` is one-to-many. Every claim produces one attempt row, so the (slightly conservative) count of paid calls for a job is its attempts excluding `skipped`.

### Notable choices

**`input_transcription` duplicates `encounters.transcription`.** Deliberate. The worker reads only its own job row, so it structurally cannot summarize the wrong version even if the encounter has advanced several versions since. Cost: one extra copy per accepted version. If transcript size became a problem, the column could be swapped for an object-storage key plus content hash with no change to the logic.

**`(encounter_id, version)` is unique in two places.** The `encounter_events` constraint prevents a duplicate from ever being processed. The `summary_jobs` constraint is a last line of defence against a buggy redrive path double-spending on the AI. It also serves the read path: Postgres backs every unique constraint with an index, so the lookup of the job at `(encounter_id, current_version)` needs no separate index. A dedicated `(encounter_id, version)` index was considered and removed as a duplicate that would only add write cost to every job insert.

**No pointer from `encounters` to the current job.** The read path resolves the current summary by looking up the job where `(encounter_id, version) = (encounter_id, current_version)`. Deriving the link rather than storing it makes "an older result must never appear as current" structural: a v12 job simply cannot be found once `current_version` is 13.

**`lease_expires_at` handles worker crashes.** A worker claims a job by setting `processing` and stamping a lease. If it dies, the lease expires and another worker reclaims it. Without this, a crashed worker's job sits in `processing` forever.

**`accepted_at` vs `started_at`.** The SLA is measured from acceptance, so queue wait counts against the 10 seconds. This means an SLA breach can be caused by backlog rather than a slow AI call, and the metrics separate the two.

**Two partial indexes for claiming.** The claim query selects jobs that are `queued` and due, **or** `processing` with an expired lease. Each branch filters on a different column, so each gets its own small partial index, and Postgres combines them for the `OR`. A single index on `next_attempt_at` would serve only the first branch, leaving expired-lease reclaims to scan every `processing` row. Both indexes stay small because they cover only live work; `ready`, `failed`, and `superseded` jobs, the bulk of the table over time, are excluded.

**No `sla_breached` column.** Derivable from `accepted_at`, `completed_at`, and now. Storing it would create a second source of truth that can drift. Exposed as a computed field in API responses.

---

## 3. Ingestion logic

### Transaction boundary

**All of it is one transaction.** The encounter upsert, the event insert, the conditional version update, and the job creation commit together or not at all.

If the encounter update and the job insert were separate commits, a crash in between would leave an encounter at v12 with no work to summarize it, permanently. Redelivery would not help: the event is already recorded, so it classifies as a duplicate and creates nothing. Silent, unrecoverable loss of work.

This is also the argument for jobs living in Postgres rather than an external broker.

### Before the transaction

Two checks run before any database work, and both reject without storing anything:

- **Body size.** Above the limit (1 MB), return `413 Payload Too Large` before parsing.
- **Validation.** A missing field, a `version` that is not a positive integer, or an `encounter_type` outside the three allowed values returns `400 Bad Request`.

### Order of operations

1. **Upsert the encounter, then lock it and read back identity.**

   ```sql
   INSERT INTO encounters (encounter_id, patient_id, encounter_type, current_version, transcription)
   VALUES (:encounter_id, :patient_id, :encounter_type, 0, NULL)
   ON CONFLICT (encounter_id) DO NOTHING;

   SELECT patient_id, encounter_type, current_version
     FROM encounters
    WHERE encounter_id = :encounter_id
      FOR UPDATE;
   ```

   For a new encounter this creates a **shell row at version 0 with no transcript**. For an existing one it is a no-op.
   *Why version 0:* the conditional update in step 4 accepts only when `current_version < :version`. If the shell row were written at the incoming version, the first event would compare `1 < 1`, match zero rows, and be misclassified as **stale**, so no new encounter would ever be summarized. Version 0 lets the first event pass the same comparison as every later one, with no special case.
   *Why it never leaks:* the shell row only survives if the event is accepted. Every other outcome (duplicate, payload conflict, stale, mismatch) rolls back, taking the shell with it. So no committed row ever sits at version 0 or has a null transcript.
   *Why `FOR UPDATE`:* locking the encounter row for the rest of the transaction serializes concurrent events for the same encounter. The identity comparison in step 2 therefore reads a value no other transaction can change before this one commits. Events for *different* encounters are unaffected, so throughput is only serialized per encounter, where ordering matters anyway.
   *Why first:* `encounter_events.encounter_id` has a foreign key to `encounters`. On the first event for an encounter, inserting the event first fails on the FK rather than reporting anything useful.
   *Alternatives considered:*
   - **Drop the foreign key on `encounter_events`.** Defensible, since the table is really an ingestion log. Rejected because it removes a safety net for no real gain.
   - **Make the foreign key `DEFERRABLE INITIALLY DEFERRED`**, so it is only checked at commit. Works, and would allow the event insert to go first. Rejected as the fiddliest option to reason about and explain, for the same outcome.
   Upsert-first is the most conventional of the three and keeps every constraint checked immediately.
2. **Compare identity.** Stored `patient_id` / `encounter_type` vs incoming. Mismatch → reject with `409 identity_conflict`, roll back.
   The read-back matters here. Two events for a brand-new encounter arriving simultaneously with different `patient_id` values: one creates the row, the other's `ON CONFLICT DO NOTHING` silently does nothing. Without re-reading, the second instance would proceed against a row it did not write. Comparing against what is *actually stored* closes that race.
3. **Insert the event, with its payload hash.** If the `event_id` PK or `uq_encounter_version` is hit, read the stored hash of every matching row and roll back:

   ```sql
   INSERT INTO encounter_events (event_id, encounter_id, version, payload_hash)
   VALUES (:event_id, :encounter_id, :version, :hash)   -- hash = sha256(transcription)
   ON CONFLICT DO NOTHING
   RETURNING event_id;

   -- zero rows: event_id or (encounter_id, version) already recorded
   SELECT payload_hash
     FROM encounter_events
    WHERE event_id = :event_id
       OR (encounter_id = :encounter_id AND version = :version);
   ```

   Every matching row has the same hash → `200 duplicate`. Any row differs → `409 payload_conflict`, logged with IDs and both hashes. Both matches are checked because a conflict can come through either door: a retried `event_id` with changed content, or a fresh `event_id` for an existing version.
4. **Conditional version update.** Zero rows → stale, roll back. One row → accepted, continue.
5. **Create the job** with `input_transcription` copied from the incoming payload. Commit. The commit is the hand-off to the worker.

### The conditional update

```sql
UPDATE encounters
   SET current_version = :version,
       transcription   = :transcription,
       updated_at      = now()
 WHERE encounter_id    = :encounter_id
   AND current_version < :version
RETURNING current_version;
```

One statement, one row lock, no read-then-write gap. Two instances racing with v12 and v13 serialize on the row lock; whichever lands second with the lower version matches zero rows and classifies as stale.

This compare-and-swap pattern appears again in the worker's result write.

---

## 4. API contract

### POST — accept an update

Every response to a well-formed event carries `encounter_id` and `current_version`, including conflicts. A partner debugging a stale event is helped by seeing what version the service actually holds.

| Outcome | Code | Rationale |
|---|---|---|
| Accepted / current | `201 Created` | A job resource was created. |
| Duplicate | `200 OK` | The caller's goal was "make sure you have this event". The service has it. Returning an error would make correct retry behaviour look like failure in the partner's dashboards. |
| Stale | `200 OK` | The brief: "a valid request may be acknowledged successfully even if it does not advance current state". A well-formed event that arrived late is not the caller's fault and there is nothing to fix. (`409` is defensible; the brief's phrasing points at `200`.) |
| Identity mismatch | `409 Conflict` | Permanent and caller-side. Retrying will never succeed, so a 5xx or 429 would be wrong: it would invite an infinite retry loop. The request conflicts with stored state rather than being malformed in isolation, which is why `409` over `422`. |
| Payload too large | `413 Payload Too Large` | The body is over the size limit. It is rejected before parsing. Resending the same body cannot succeed. |
| Payload conflict | `409 Conflict` | The `(encounter_id, version)` already has a different payload recorded. This is a source defect: the stored version is kept and retrying will not succeed. |
| Malformed request | `400 Bad Request` | A required field is missing, `version` is not a positive integer, or `encounter_type` is not one of the three allowed values. Nothing is stored. |

The general rule: **2xx means the event has been dealt with, stop sending it. 4xx means the event itself is wrong and resending will not help.** Duplicate and stale are both "dealt with", which is why they share a code and differ only in the body.

**Privacy note:** the mismatch response must not echo the stored `patient_id`. The caller has just demonstrated it holds incorrect identity data; confirming the real value would leak patient linkage. Both values are logged internally; neither is returned.

### GET — retrieve progress and summary

All three job states return `200 OK`: the resource exists and its state is being reported. A failed job is a successful read of a failed thing.

- **Processing** — status, version being worked on, `accepted_at`, computed `sla_breached`. No summary.
- **Ready** — status, summary text, and the version it describes (always equal to `current_version`), completion timestamp, `sla_breached`.
- **Failed** — status, attempt count, error classification (`ai_timeout`, `rate_limited`, etc). Never the raw provider error, which could echo transcript content.
- **Unknown encounter** — `404 Not Found`. An encounter whose current version has no job is impossible given single-transaction ingestion, but has a defined response rather than a crash.

**The read always resolves via `current_version`.** If v13 is processing, the client sees `processing`, not v12's summary. This is the "older result must never appear as current" guarantee made structural rather than enforced by careful writes.

Optionally, a separate clearly-labelled field can carry the last ready summary with its version attached, for clients that prefer stale-but-labelled over nothing. It must never occupy the main summary field.

---

### Example requests and responses

Field naming is consistent across every response: `outcome` names the ingestion outcome on POST, `status` names the job state on GET. On POST, `version` is the version the caller submitted and `current_version` is what the service holds.

**Request (identical shape for all POST outcomes)**

```json
POST /encounters/events
Content-Type: application/json

{
  "event_id": "evt-102",
  "encounter_id": "enc-42",
  "patient_id": "pat-77",
  "encounter_type": "TelephoneTriage",
  "version": 12,
  "payload": {
    "transcription": "Nurse: Thanks for calling, how can I help today? Patient: ..."
  }
}
```

**Accepted / current**

```json
201 Created
Location: /encounters/enc-42/summary

{
  "outcome": "accepted",
  "encounter_id": "enc-42",
  "version": 12,
  "current_version": 12,
  "job_status": "queued"
}
```

**Duplicate** (retry of `evt-102`, or another `event_id` for `enc-42` v12)

```json
200 OK

{
  "outcome": "duplicate",
  "encounter_id": "enc-42",
  "version": 12,
  "current_version": 13
}
```

**Stale** (v11 arrives after v12 is stored)

```json
200 OK

{
  "outcome": "stale",
  "encounter_id": "enc-42",
  "version": 11,
  "current_version": 12
}
```

**Identity mismatch** (`enc-42` is bound to a different patient)

```json
409 Conflict

{
  "outcome": "identity_conflict",
  "encounter_id": "enc-42",
  "current_version": 12,
  "conflicting_field": "patient_id",
  "message": "Contradicts stored encounter identity. Retrying will not succeed."
}
```

The stored `patient_id` is deliberately absent. The field name is returned so the partner can locate the bug; the value is not, because the caller has just demonstrated it holds incorrect identity data. The message states plainly that retrying is futile.

**Payload conflict** (v12 already stored with a different transcript)

```json
409 Conflict

{
  "outcome": "payload_conflict",
  "encounter_id": "enc-42",
  "version": 12,
  "current_version": 13,
  "message": "A different payload is already recorded for this version."
}
```

**GET — processing** (queued jobs are also reported as processing)

```json
GET /encounters/enc-42/summary

200 OK

{
  "encounter_id": "enc-42",
  "patient_id": "pat-77",
  "encounter_type": "TelephoneTriage",
  "current_version": 12,
  "status": "processing",
  "summary": null,
  "accepted_at": "2026-09-18T10:04:11Z",
  "sla_breached": false
}
```

**GET — ready**

```json
200 OK

{
  "encounter_id": "enc-42",
  "patient_id": "pat-77",
  "encounter_type": "TelephoneTriage",
  "current_version": 12,
  "status": "ready",
  "summary_version": 12,
  "summary": "Patient reported sore throat and fever ...",
  "accepted_at": "2026-09-18T10:04:11Z",
  "completed_at": "2026-09-18T10:04:17Z",
  "sla_breached": false
}
```

`summary_version` always equals `current_version` by construction; it is returned so clients can assert that rather than trust it.

**GET — failed**

```json
200 OK

{
  "encounter_id": "enc-42",
  "patient_id": "pat-77",
  "encounter_type": "TelephoneTriage",
  "current_version": 12,
  "status": "failed",
  "summary": null,
  "attempts": 5,
  "error_class": "ai_unavailable",
  "accepted_at": "2026-09-18T10:04:11Z",
  "completed_at": "2026-09-18T10:22:48Z",
  "sla_breached": true
}
```

`error_class` is a fixed enum (`ai_timeout`, `rate_limited`, `ai_unavailable`, `worker_lost`, `internal`). The raw provider error is never returned, since it may echo transcript content.

Both `attempts` and `error_class` in this response are derived from `job_attempts`, not read from the job row: `attempts` is the count of non-skipped attempts in the current redrive generation (the same count the retry budget uses), and `error_class` is taken from the most recent attempt. `summary_jobs.attempts` is the fencing token and is never exposed, since it spans redrive generations and can skip values.

**GET — unknown encounter**

```json
404 Not Found

{
  "error": "encounter_not_found",
  "encounter_id": "enc-999"
}
```

Field-level notes:

- `job_status` on accept is always `queued`. `job_id` is kept internal (logs and support tickets); the client polls by encounter through the `Location` header, never by job. Polling by job would let a client hold a handle to a superseded version, which is exactly what the read path is designed to prevent.
- A job in `queued` is reported as `processing` on GET. From the client's side both mean "accepted, summary not ready yet"; whether a worker currently holds the job, or it is waiting out a backoff or an open circuit breaker, is internal scheduling the client cannot act on.
- `sla_breached` is computed at read time from `accepted_at`, `completed_at`, and the current clock. It is always present, including when false, so clients do not have to distinguish absent from false.
- On `stale`, the gap between `version` and `current_version` is the useful information for the partner.

## 5. Worker logic

### Which jobs are claimable

| Status | Claimable? | Condition |
|---|---|---|
| `queued` | Yes | Only once `next_attempt_at <= now()`. A job waiting out a retry backoff sits in `queued` with a future `next_attempt_at` and must not be picked up early. |
| `processing` | Yes, conditionally | Only once `lease_expires_at < now()`. A crashed worker and a slow worker are indistinguishable from outside, so the lease is the only signal. An expired lease means the owner is presumed dead and the job is reclaimed. |
| `ready` | No | Terminal. |
| `superseded` | No | Terminal. |
| `failed` | No | Terminal for automation. Only an explicit redrive returns it to the queue (see below). |

In short: **queued and due, or processing with an expired lease.**

### Claiming a job

A claim is one transaction: check the circuit breaker, take the job, close out any abandoned previous attempt, check the retry budget if this is a reclaim, and open a new attempt row.

```sql
BEGIN;

-- 0. Circuit breaker: if not closed, do not claim (see retries section)
SELECT state FROM circuit_breaker WHERE name = 'generate_summary';
-- state <> 'closed' → COMMIT, sleep briefly, try again later

-- 1. Take the job
UPDATE summary_jobs
   SET status           = 'processing',
       attempts         = attempts + 1,
       lease_expires_at = now() + interval '60 seconds',
       started_at       = COALESCE(started_at, now())
 WHERE job_id = (
         SELECT job_id
           FROM summary_jobs
          WHERE (status = 'queued'     AND next_attempt_at  <= now())
             OR (status = 'processing' AND lease_expires_at <  now())
          ORDER BY accepted_at
          FOR UPDATE SKIP LOCKED
          LIMIT 1
       )
RETURNING job_id, encounter_id, version, input_transcription,
          attempts AS my_attempt, redrive_generation;

-- 2. If this was a reclaim, mark the previous owner's attempt as abandoned
UPDATE job_attempts
   SET outcome = 'lease_expired', error_class = 'worker_lost', finished_at = now()
 WHERE job_id = :job_id AND attempt_no < :my_attempt AND outcome = 'in_flight'
RETURNING attempt_no;

-- 3. Reclaims only (step 2 returned a row): check the retry budget
SELECT count(*) AS used
  FROM job_attempts
 WHERE job_id = :job_id
   AND redrive_generation = :redrive_generation
   AND outcome <> 'skipped';

-- If used >= 5: fail the job instead of starting a new attempt
UPDATE summary_jobs
   SET status = 'failed', completed_at = now(), lease_expires_at = NULL
 WHERE job_id = :job_id AND attempts = :my_attempt;
-- → COMMIT and claim the next job; steps 4 onwards are skipped

-- 4. Open this attempt
INSERT INTO job_attempts (job_id, attempt_no, redrive_generation, worker_id)
VALUES (:job_id, :my_attempt, :redrive_generation, :worker_id);

COMMIT;
```

**Why the budget is checked on reclaim.** A worker that crashes never reaches the transient-error write, which is the other place the budget is checked. Without this step, a transcript that crashes the worker every time would be reclaimed forever, paying each time the crash happens after the call goes out. The reclaiming worker is the first live process to learn of the crash, so it is where the check belongs. The check runs only on reclaims: a job arriving from `queued` was last handled by the transient-error path, which already applied the budget before requeuing it.

**A side effect: gaps in attempt numbers.** When a reclaim ends in `failed`, `attempts` has already been bumped in step 1 but no attempt row is opened, so `attempt_no` skips a value. This is harmless: fencing only needs the counter to increase, never to be contiguous. Reordering to check the budget before bumping would need the budget query to run before the job is locked, reopening a race for a purely cosmetic gain.

**What the claim sets, and why each matters:**

- `status = 'processing'` tells other workers the job is taken.
- `attempts + 1` moves the fencing token. The worker carries `my_attempt` through to the result write; if anyone reclaims the job in the meantime, the token moves on and this worker's write is rejected.
- `lease_expires_at` is the worker's promise: "if I haven't finished by then, assume I'm dead". It is the only way a crashed worker's job ever becomes claimable again.
- `started_at` is set on the first claim only. With `accepted_at`, it splits SLA time into queue wait (`started_at - accepted_at`) and processing time (`completed_at - started_at`), which tells an operator whether a breach means "add workers" or "the AI was slow".

**`FOR UPDATE SKIP LOCKED`** is the standard Postgres pattern for a table-backed queue. The subquery locks the row it selects; a concurrent worker's subquery skips locked rows instead of waiting and takes the next one. No two workers claim the same job, and none block each other.

**Lease duration is set relative to the AI timeout.** The worker enforces its own timeout on `generate_summary` (30 seconds, since the brief notes calls can exceed 10). The lease must be comfortably longer than that timeout. If it were shorter, a live worker waiting on a slow call would have its job reclaimed, and the same version would be paid for twice. Rule: **lease > AI timeout + safety margin**. Fencing guarantees correctness if this is ever violated; the margin is what protects cost.

`ORDER BY accepted_at` serves the oldest work first, protecting the SLA of jobs that have waited longest.

### Pre-call check

Immediately after claiming, and before calling `generate_summary`, the worker checks one thing: **is this job's version still the encounter's current version?** This is the only point in the job's life where money can be saved (see supersession policy below), and it is what keeps the retry budget honest: a skip must be recorded before any call is made, or the budget cannot tell free attempts from paid ones.

The check and the skip are a single conditional update, not a read followed by a write:

```sql
BEGIN;

-- Mark the job superseded only if (a) this worker still owns it and (b) a newer version is current
UPDATE summary_jobs j
   SET status                = 'superseded',
       superseded_by_version = e.current_version,
       completed_at          = now()
  FROM encounters e
 WHERE j.job_id          = :job_id
   AND e.encounter_id    = j.encounter_id
   AND j.status          = 'processing'
   AND j.attempts        = :my_attempt
   AND e.current_version > j.version
RETURNING j.job_id;

-- If one row: close this attempt as free
UPDATE job_attempts
   SET outcome = 'skipped', finished_at = now()
 WHERE job_id = :job_id AND attempt_no = :my_attempt;

COMMIT;
```

If the update returns a row, the worker stops: no call, attempt `skipped`, job `superseded` with `superseded_by_version` set, budget untouched. If it returns zero rows, the version is still current and the worker proceeds to the call.

Three details make this hold up:

- **One statement, not read-then-write.** A separate read of `current_version` followed by a write could act on a value that has already changed. The conditional update decides and records in one step.
- **Fenced by `attempts = :my_attempt`**, exactly like the result write. A worker whose lease has expired cannot mark a job it no longer owns. (Zero rows could in principle also mean lost ownership, but the check runs milliseconds after a claim that stamped a 60-second lease, so in practice zero rows means "still current". If ownership had somehow been lost, the result write's fence rejects the result anyway.)
- **The attempt is marked `skipped` in the same transaction.** The skip and its budget exemption commit together; there is no state in which the job is superseded but the attempt still looks like it might have paid.

**The remaining window.** A newer version can be accepted after the check passes and before the request goes out. This cannot be closed without holding a lock across the AI call, which the design rules out. It is acceptable: the job becomes the in-flight case, the call completes, and the guarded write labels the result `superseded`. The check narrows the window to milliseconds; the guarded write makes whatever falls through it harmless.

**Worker flow:**

```
loop forever:
    job = claim()                     -- txn 1: breaker check, SKIP LOCKED, attempts+1 (fencing token),
                                      --        60s lease, close any abandoned attempt as lease_expired,
                                      --        reclaim budget check, open attempt row
    if job is None:                   -- nothing due, or breaker not closed
        try_probe_if_cooldown_passed()
        sleep briefly; continue
    if job.failed_on_reclaim:         -- budget exhausted on reclaim; txn 1 marked it failed
        continue

    if precall_supersede(job):        -- txn 2: fenced conditional update
        continue                      -- job superseded, attempt skipped, no call, no cost

    try:
        summary = generate_summary(job.input_transcription, timeout = 30s)   -- no txn open
    except TransientError as e:       -- timeout, rate limit, unavailable
        record_failure(job, e.error_class)   -- txn 3a, fenced: queued + backoff,
                                             -- or failed at 5 attempts; breaker trip check
        continue

    if not guarded_write(job, summary):      -- txn 3b, fenced: ready or superseded
        mark_attempt_discarded(job)          -- lost ownership; paid call, result unused
```

#### Why there is no "result already exists" check

The obvious second pre-call check, "has an earlier attempt already saved a result?", was considered and left out because it can never be true.

The guarded write sets `summary` and moves `status` out of `processing` (to `ready` or `superseded`) in one statement and one commit. The claim query only selects jobs that are `queued`, or `processing` with an expired lease. So once a result is saved, the job is no longer claimable, and no worker can ever reach a pre-call check for it.

This is how the "worker crashes after saving a result but before acknowledging" scenario is handled: in a table-backed queue there is no separate acknowledgement step. **The commit that saves the result is the acknowledgement.** A crash after it loses nothing and causes no rework; a crash before it leaves the job `processing` with no result, and the expired lease makes it claimable again.

Keeping the check as a defensive guard was also rejected because of what it would record. Marking such an attempt `succeeded` would break the cost table, where `succeeded` means a paid call was made; it would need its own outcome value for a branch that cannot execute.

#### Where the check lives: alternatives considered

**Chosen: a separate step immediately after the claim (option A).** Simple, and each transaction does one job: the claim takes ownership, the check decides whether the work is still worth paying for.

The two alternatives below were considered and left out. Note that none of the three saves an AI call over the others; in all of them the check runs before the call. They differ only in where the database work happens.

- **Fold the check into the claim transaction (option B).** Saves one database round trip and removes the few milliseconds between claim and check. Left out because that gap is not the one that matters: the window between the check and the call going out exists either way, and is covered by the guarded write. B buys almost nothing and makes the claim transaction do two jobs.
- **Supersede queued jobs at ingestion (option C).** When v13 is accepted, the ingestion transaction also marks any `queued` older jobs for that encounter as `superseded`, so workers never claim them. This has a real benefit: no wasted claims, lease, or attempt rows for obsolete work, and queue depth stays accurate during a burst of versions. Left out because it does not replace the worker check (jobs already claimed still need it), so it is extra work in the ingestion path on top of A rather than instead of it, and it widens the ingestion transaction to touch other jobs' rows. It is the upgrade path if bursts of versions ever make queue depth misleading.

### Transaction boundaries around the AI call

The worker uses three short transactions, and the AI call sits outside all of them:

| Step | Transaction | Commits |
|---|---|---|
| Claim | txn 1 | job `processing`, `attempts + 1`, lease stamped, attempt row opened |
| Pre-call check | txn 2 | if obsolete: job `superseded`, attempt `skipped`; otherwise nothing |
| `generate_summary` | **none** | — |
| Result or failure | txn 3 | job `ready` / `superseded` / `queued` / `failed`, attempt closed |

**Why the call cannot sit inside a transaction.**

- **A crash would erase the record of spend.** If the claim and the call shared a transaction and the worker crashed mid-call, the rollback would undo the claim *and* the attempt row. The provider has already done and billed the work, but the database would show the job `queued` and untouched, and the retry budget would never count that attempt. A worker crashing repeatedly mid-call during an outage would spend without limit while the budget read zero. This is the same principle as the pre-call skip: **the attempt row must be committed before any money is spent.**
- **It would hold database resources for the length of the call.** An open transaction holds a connection and the claim's row lock for up to 30 seconds. With N concurrent AI calls, N connections sit idle-in-transaction, so the pool is exhausted long before worker capacity is. Long-running transactions also stop Postgres from cleaning up old row versions, bloating the tables.

**What the worker holds while it waits.** Nothing in the database: no connection, no lock, no open transaction. Ownership is carried by two things:

- **The lease**, a timestamp committed in the job row. The claim query does not select a `processing` job whose lease has not expired, so no other worker can pick the job up while the lease is live.
- **The fencing token** (`my_attempt`), held in the worker's memory and checked by every later write.

The lease prevents two workers paying for the same job in normal operation, but not absolutely. If a worker stalls beyond 60 seconds (hung process, network stall), its lease expires, another worker reclaims the job, and both calls may be paid for. Fencing guarantees only one result is written; the 30-second AI timeout sitting well under the 60-second lease is what keeps this cost leak rare.

**Alternative considered: one transaction from claim to result write.** Its appeal is that a crash rolls back the claim automatically, so the job is instantly claimable again and leases are unnecessary. Rejected for the two reasons above: it loses the record of paid calls on a crash, and it ties up a connection and a lock per in-flight call.

**The trade-off accepted.** Leases cost recovery time. After a worker crash, the job stays unclaimable until the lease expires, up to 60 seconds, so that job breaches the 10-second SLA. A crash is rare and the delay is bounded; unrecorded spend and pool exhaustion are neither.

**Alternative considered: lease renewal (heartbeating).** The worker could stamp a short lease (say 10 seconds) and extend it every few seconds while the call runs, so a crash is detected in seconds rather than a minute. Left out because it adds a background renewal loop per worker and a write every few seconds per in-flight job, to shorten a recovery delay that only occurs on crashes. With the AI timeout fixed at 30 seconds, a fixed 60-second lease never needs renewing. It is the upgrade path if crash recovery time ever matters to the SLA.

### Retry budget

The budget is counted from `job_attempts`, scoped to the current redrive generation, and counts every attempt **except those marked `skipped`**:

```sql
SELECT count(*) FROM job_attempts
 WHERE job_id = :job_id
   AND redrive_generation = :current_generation
   AND outcome <> 'skipped';
```

When that reaches the limit (5), the job moves to `failed`. The budget is **per job, per redrive generation**: one encounter version gets up to 5 attempts, and a redrive grants a fresh 5. It does not cap system-wide spend; that is the circuit breaker's job (see retries).

**Why exclude only `skipped`.** The budget exists to cap spend, so it should count spend. The pre-call check runs immediately after the claim, so every attempt not marked `skipped` either made a paid call or failed on the way to making one. Counting all of them errs toward *overcounting*, which is the safe direction for a cost cap: a worker that crashed just before sending the request is counted as if it had paid. Skipped attempts are guaranteed to have cost nothing, so they never consume budget.

**What each outcome says about cost** (why no separate "was a call made" flag is needed):

| Outcome | Paid call made? |
|---|---|
| `succeeded`, `superseded`, `transient_error`, `discarded` | Yes, definitely |
| `skipped` | No, definitely |
| `lease_expired` | Ambiguous (crashed before or during the call); counted as paid |

**Poison jobs fail on their own.** A transcript that reliably crashes the worker before it reaches the AI call produces a run of `lease_expired` attempts. Because those count toward the budget, the reclaiming worker's budget check (see claiming) moves the job to `failed` after 5, and it surfaces to operators with `error_class = 'worker_lost'` recorded on each abandoned attempt. Five consecutive `lease_expired` outcomes is an unmistakable crash signature, so the attempt history still distinguishes "the AI kept failing" from "our worker kept dying" without any extra mechanism.

**Alternative considered: an explicit `ai_called` flag.** *This was the most deliberated decision in the design, and went back and forth before settling.*

The alternative adds `ai_called BOOLEAN` to `job_attempts`, set and committed immediately before the request is sent (never after it returns, or a crash mid-call would leave real spend unrecorded). The budget then counts only attempts with `ai_called = true`, so "5 attempts" means precisely "5 calls we attempted to send".

It was rejected because the precision it adds is narrow and the complexity it adds is not:

- **Narrow gain.** The outcome column already says whether a call happened for every outcome except `lease_expired`. The flag only helps there, and even then only partially: `ai_called = true` means "we were about to send", not "the provider received and billed it".
- **Real cost.** An extra commit on every attempt, and, more importantly, it **opens the poison-job hole**. Pre-call crashes leave `ai_called = false`, so they never consume budget, and a crashing job is reclaimed forever. Closing that required a second cap (a looser ceiling on total claims per generation, its own error class, and a check in the claim transaction). The flag forced an entire extra mechanism into existence to patch a problem the simpler approach never had.

Counting non-skipped attempts keeps almost all of the auditability (carried by the outcome enum, not the flag), removes a column and a mechanism, and handles poison jobs with no special case.

`summary_jobs.attempts` is never reset, including on redrive. Resetting it would reopen a fencing hole: a worker from before the failure, hung but alive and holding attempt 1, could see a post-redrive claim also numbered 1 and have its stale write accepted.

**Alternatives considered for the retry budget.** Both keep the fencing token monotonic and are cheaper than a fourth table; both were left out because they record *how many* paid calls happened but not *which*, *when*, *by whom*, or *with what outcome*.

- **`retries_remaining` column on `summary_jobs`.** Decremented on each failed attempt, refilled on redrive, while `attempts` only climbs. The smallest change that keeps every correctness guarantee. Rejected in favour of `job_attempts` because it gives no per-attempt history, so investigating an outage or reconciling AI spend against a specific job is impossible from the database alone.
- **`attempts_at_redrive` column on `summary_jobs`.** Record the value of `attempts` at the moment of redrive, and check `attempts - attempts_at_redrive < 5`. Functionally identical to `retries_remaining`, avoiding decrement logic in favour of arithmetic. Rejected for the same reason: correct, but not auditable.

Either would be a reasonable choice if write volume on the attempts table ever became a concern, since they turn one insert per attempt into a column update.

### Retries, backoff, and the circuit breaker

Cost is controlled in three layers, each capping something different:

| Layer | Caps | Scope |
|---|---|---|
| Retry budget (5) | How much one job can spend | Per job, per redrive generation |
| Exponential backoff + jitter | How fast one job spends it | Per job |
| Circuit breaker | How much the whole system spends while the AI is down | System-wide |

The budget alone does not protect against an outage: with hundreds of queued jobs, each discovering the outage for itself by paying for a failed call, total spend is budget × queue depth. The breaker is what makes an outage cost a bounded number of calls regardless of queue size.

#### After a transient error: attempt fails, job does not

A timeout, rate limit, or availability error fails **the attempt**, not the job. The job becomes `failed` only when its budget is exhausted. Until then the job goes back to `queued` with `next_attempt_at` pushed into the future; the claim query's `queued` branch honours that column, which is how backoff is enforced.

```sql
BEGIN;

-- Budget used in this generation, including this attempt (still 'in_flight', so counted)
SELECT count(*) AS used
  FROM job_attempts
 WHERE job_id = :job_id
   AND redrive_generation = :redrive_generation
   AND outcome <> 'skipped';

-- Fenced: only the current owner may requeue or fail the job
UPDATE summary_jobs
   SET status           = CASE WHEN :used >= 5 THEN 'failed' ELSE 'queued' END,
       next_attempt_at  = now() + :backoff,      -- ignored if failed
       completed_at     = CASE WHEN :used >= 5 THEN now() END,  -- terminal time if failed
       lease_expires_at = NULL
 WHERE job_id   = :job_id
   AND status   = 'processing'
   AND attempts = :my_attempt
RETURNING status;

-- If one row: close the attempt
UPDATE job_attempts
   SET outcome = 'transient_error', error_class = :error_class, finished_at = now()
 WHERE job_id = :job_id AND attempt_no = :my_attempt;

-- Breaker trip check (below), same transaction

COMMIT;
```

If the job update returns zero rows, this worker lost ownership; its attempt was already closed as `lease_expired` by the reclaiming worker, and it stops.

**Every error is treated as transient.** The brief's contract for `generate_summary` describes only transient failures (timeouts, rate limits, availability errors), so the design handles those and does not attempt to classify errors that retrying cannot fix, such as a transcript the service rejects as too large. Such an input costs up to 5 paid calls before its job fails, bounded by the budget. See section 8 for the consequences and how it would be handled.

**Why `queued` and not `processing`.** `processing` means a worker owns the job. Leaving it `processing` after the worker has given up would make the database claim an owner that does not exist; the claim query would ignore the job until the lease happened to expire (an accidental 60-second backoff), and the reclaim would appear in metrics as a worker crash.

**Why `attempts` is not touched.** It is the fencing token and already moved at claim time. The failure is recorded on the attempt row (`transient_error` plus `error_class`), and the budget reads from those rows.

#### Backoff: exponential with jitter

`next_attempt_at = now() + random(0.5, 1.0) × 10s × 2^(used − 1)`, giving waits of roughly 10s, 20s, 40s, 80s after the 1st to 4th failures. The 5th failure moves the job to `failed`. Because `used` is scoped to the redrive generation, a redriven job starts again at 10s.

- **Growing, not constant or shrinking.** One failure is probably a blip, so an early retry is reasonable. Several in a row almost certainly mean the service is down or rate-limiting; retrying faster sends more paid calls into a service that is refusing them, and prolongs rate limiting.
- **Jitter.** In an outage, many jobs fail within the same few seconds. A fixed schedule would return them all at the same instants, hitting the service in synchronised waves as it recovers. Randomising each wait spreads them out.

**Alternative considered: shorter waits, so jobs reach `failed` sooner and stop spending.** The instinct (stop paying for a service that is failing) is right, but shorter waits do not achieve it. The budget allows 5 calls whatever the spacing; shorter waits spend the same 5 calls sooner, all inside the outage, with no chance of any landing after recovery. It also turns short blips into terminal failures that need manual redrive. Nor does urgency justify it: the 10-second SLA is already breached after the first failed attempt, so faster retries cannot rescue it. The lever for spending less in an outage is to stop calling altogether, which is the breaker.

#### Circuit breaker

The breaker watches **the service, not a job**. Its state is a single shared row in Postgres, read by every worker before claiming.

```sql
CREATE TYPE breaker_state AS ENUM ('closed', 'open', 'half_open');

CREATE TABLE circuit_breaker (
    name           TEXT PRIMARY KEY,           -- 'generate_summary'
    state          breaker_state NOT NULL DEFAULT 'closed',
    open_until     TIMESTAMPTZ,                -- open: no calls before this
    probe_deadline TIMESTAMPTZ,                -- half_open: probe presumed lost after this
    probe_generation   INTEGER NOT NULL DEFAULT 0,  -- fencing token for probers; only ever increases
    probes_sent        INTEGER NOT NULL DEFAULT 0,  -- running count of paid probe calls
    last_probe_at      TIMESTAMPTZ,
    last_probe_outcome TEXT,                   -- 'succeeded' / 'transient_error'; fixed values only
    opened_at      TIMESTAMPTZ,                -- start of the current outage, for metrics
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_attempts_finished ON job_attempts (finished_at DESC);
```

**Tripping (closed → open).** In the same transaction that records a `transient_error`, the worker looks at the last 20 attempts that reached the service within the past 10 minutes:

```sql
SELECT count(*) FILTER (WHERE outcome = 'transient_error') AS failures
  FROM (SELECT outcome FROM job_attempts
         WHERE outcome IN ('succeeded', 'superseded', 'discarded', 'transient_error')
           AND finished_at > now() - interval '10 minutes'
         ORDER BY finished_at DESC
         LIMIT 20) recent;

-- If failures >= 11 (a strict majority of the last 20):
UPDATE circuit_breaker
   SET state = 'open', open_until = now() + interval '30 seconds',
       opened_at = now(), updated_at = now()
 WHERE name = 'generate_summary' AND state = 'closed';
```

The evidence for tripping is the existing `job_attempts` history; the breaker keeps no failure counters of its own. Probe calls are not in `job_attempts` (see below), so they never feed the trip decision. Only outcomes that reflect the service's behaviour are counted: `discarded` counts as a success (the call returned; the worker had simply lost ownership), while `skipped` (no call) and `lease_expired` (a worker problem, not a service one) are excluded. Requiring 11 failures means the breaker cannot trip on the first error after startup.

**Why a 10-minute window.** Without a time bound, failures from before a quiet period would still count, and one fresh failure hours later could trip the breaker on stale evidence. Ten minutes rather than two because timeout-style failures are slow: each takes 30 seconds to fail, so with one or two workers a short window would never hold 11 failures and the breaker could not trip. At very low traffic the breaker may still not trip at all, which is acceptable: with few calls happening, per-job budgets already bound spend.

**Why 11 of 20, not 10.** In a full outage the difference is negligible: nearly every call fails, so the breaker trips one failure later. The threshold matters when the service is partly degraded, for example rate-limiting about half of calls. At exactly 50% failure, a 10-of-20 threshold would trip, the probe would then have a coin-flip chance of succeeding, and the breaker would flap between open and closed. With 11, a half-working service stays in normal operation, where the retry budget and backoff handle individual failures; the breaker steps in only once failures outnumber successes.

**While open: workers stop claiming.** The claim transaction reads the breaker row first; if it is not closed, the worker does not claim and sleeps briefly. Jobs stay `queued` with `next_attempt_at` untouched: no fencing token moved, no lease, no attempt row, no budget consumed. When the breaker closes they are picked up in `accepted_at` order as normal. Calls already in flight when the breaker trips complete normally, and their outcomes count as usual.

**Probing (open → half_open → closed or open).** When `open_until` passes, exactly one worker is allowed to send a test call. Nobody chooses the prober; idle workers race on a conditional update and Postgres guarantees one winner:

```sql
UPDATE circuit_breaker
   SET state            = 'half_open',
       probe_deadline   = now() + interval '60 seconds',
       probe_generation = probe_generation + 1,
       probes_sent      = probes_sent + 1,
       last_probe_at    = now(),
       updated_at       = now()
 WHERE name = 'generate_summary'
   AND (   (state = 'open'      AND open_until     <= now())
        OR (state = 'half_open' AND probe_deadline <  now()))   -- previous prober died
RETURNING probe_generation AS my_probe;
```

The first worker's update takes the row lock and moves the state to `half_open`. Concurrent updates wait on that lock; once it commits, Postgres re-evaluates their `WHERE` against the new row, the condition is false, and they update zero rows. Only the winner receives `my_probe` from `RETURNING`. This is the same compare-and-swap pattern used at ingestion, in the fenced result write, and in redrive. The second branch of the `WHERE` uses the same race to pick a replacement if a prober dies.

`probes_sent` is incremented in this commit, before the call goes out, for the same reason attempt rows are committed at claim: the record of spend must exist before the money is spent.

**The probe sends a synthetic transcript, not a real job.** The winner claims no job. It calls `generate_summary` with a fixed, short synthetic transcript containing no patient data, then reports the outcome back to the breaker row, fenced by its probe generation:

```sql
-- Probe succeeded
UPDATE circuit_breaker
   SET state = 'closed', opened_at = NULL,
       last_probe_outcome = 'succeeded', updated_at = now()
 WHERE name = 'generate_summary' AND state = 'half_open' AND probe_generation = :my_probe;

-- Probe failed with a transient error
UPDATE circuit_breaker
   SET state = 'open', open_until = now() + interval '30 seconds',
       last_probe_outcome = 'transient_error', updated_at = now()
 WHERE name = 'generate_summary' AND state = 'half_open' AND probe_generation = :my_probe;
```

- Probe succeeds → `closed`, and normal claiming resumes.
- Probe fails → `open` again with a fresh 30-second cooldown.
- Prober crashes or stalls → `probe_deadline` (equal to the lease length) passes and another worker wins the race to probe.

**Prober crashes after the response arrives, before reporting it.** The breaker stays `half_open` and no jobs are claimed. When `probe_deadline` passes, another worker wins the race and sends a fresh probe, whose result is recorded normally. The cost is one extra paid probe, correctly counted in `probes_sent` since each probe is counted before it is sent, and up to 60 seconds of extra pause if the lost response was a success. Correctness is unaffected: a prober that was only stalled, not dead, has its late write rejected by the `probe_generation` fence. One gap in the record: the lost probe's outcome is never known, so `probes_sent` can exceed the number of probe outcomes reported in metrics. That difference is itself a count of lost probes.

**Why the probe needs its own fencing.** A real job would have been protected by the job's fencing token; a synthetic probe has no job, so the breaker carries its own. Without it: prober A stalls past its deadline, prober B takes over and succeeds, the breaker closes; then A's call returns a failure and reopens the breaker on stale information. With `probe_generation`, A's write matches zero rows because B's win bumped the number.

**Where probe cost is recorded.** Every job-related paid call maps to a `job_attempts` row, but a probe has no job, so it cannot go there without inventing a fake one. Instead the breaker row keeps `probes_sent`, `last_probe_at`, and `last_probe_outcome`, and each probe emits a metric. Together with `job_attempts`, that answers "how much did the outage cost?" completely: job calls from `job_attempts`, probe calls from the breaker.

**Alternative considered: probe with a real job from the queue.** Its advantage is that a success proves the service handles a genuine, possibly long, transcript, and the call is not wasted: a successful probe produces a real summary. Left out because the probe job pays for every failed probe out of its own budget. During a 20-minute outage the oldest queued job would fail after about 5 probes, then the next oldest, so a handful of patients' summaries would end up `failed` and needing manual redrive purely for having been first in line. It would also send patient content into a service already known to be failing. With a synthetic probe, no job's budget is touched for the whole outage.

**The accepted weakness.** A success on a short synthetic transcript does not prove long real transcripts will go through (for example if the service recovers only partially, or is timing out on large inputs). The cost of being wrong is bounded: if real calls start failing again, the breaker re-trips after 11 failures, and the jobs involved pay only from their normal budgets.

**Why a shared breaker rather than one per worker.** Not for per-claim efficiency (every claim now reads one extra row), but for consistency and cost. All workers act on one verdict; evidence is pooled from every worker's calls, so an outage is detected faster; and there is one paid test call per cooldown instead of one per worker. The row is written only on state transitions, which are rare, and reads do not block, so it does not become a contention point.

**Alternatives considered for the breaker.**

- **Per-worker breaker in memory.** Simplest, with no shared state. Left out because each worker trips independently on its own smaller sample, and with N workers every cooldown costs N paid test calls.
- **Trip when a job exhausts its budget.** Slow and misleading. With exponential backoff a job takes about 5 minutes to use up 5 attempts, during which every other job keeps paying for failed calls. And a job can exhaust its budget for reasons unrelated to an outage, such as a transcript long enough to time out every time; that would pause the whole system over one bad input while the service is healthy for everyone else.
- **Workers keep claiming and decline to call while open.** Each decline would move the fencing token, stamp a lease, and write an attempt row for a job that was never going to be called, and those attempts would need an outcome. `skipped` means "obsolete, no call needed"; reusing it would make the cost history misstate why calls did not happen.

**Cost of the 20-minute outage.** Each probe cycle is a 30-second cooldown plus a call of up to 30 seconds, so the breaker spends one paid call roughly every 30 to 60 seconds: on the order of 20 to 40 calls for the whole outage, independent of queue depth. Without it, every queued job would spend up to its full budget.

### Failed is terminal; redrive is deliberate

Workers never pick up `failed` jobs automatically. A job reaches `failed` only after exhausting its retry budget, and from there the only way back is an explicit redrive: an operator or admin action that moves the job from `failed` to `queued` with a fresh retry budget.

**Why not let workers retry failed jobs on their own?** Every `generate_summary` call is paid, including retries. If workers automatically resumed failed jobs, `failed` would mean "retrying slowly forever", and the AI spend during a prolonged outage would have no ceiling. Making redrive explicit puts a human decision, "the outage is over", between exhaustion and further spend. It also gives failed work a clear inspection point: failed jobs and their `job_attempts` history can be queried by `error_class`, attempt count, and time window before anyone decides to redrive them.

**Redrive safety check.** A failed job is redriven only if its version still equals `encounters.current_version`. If a newer version arrived while the job sat failed, redriving it would pay for a summary the read path can never surface. Such jobs are moved to `superseded` instead.

```sql
-- Redrive: only jobs still describing the current version
UPDATE summary_jobs j
   SET status             = 'queued',
       completed_at       = NULL,                  -- no longer terminal
       next_attempt_at    = now(),
       redrive_generation = j.redrive_generation + 1   -- fresh retry budget
  FROM encounters e
 WHERE j.job_id       = :job_id
   AND e.encounter_id = j.encounter_id
   AND j.status       = 'failed'
   AND e.current_version = j.version
RETURNING j.job_id;

-- If zero rows: the job is obsolete; mark it superseded rather than redriving
```

Redrive bumps `redrive_generation`, which gives the job a fresh retry budget without touching `attempts`. The attempt history from before the redrive stays in `job_attempts`, so the full cost of the job across all generations remains visible.

### Writing the result (guarded write)

A result is valid for the current encounter only if the job's version **equals** `encounters.current_version`. Jobs are only ever created by accepting a version, so a job's version can never exceed current; equality is the whole condition.

```sql
UPDATE summary_jobs j
   SET status       = CASE WHEN e.current_version = j.version
                           THEN 'ready' ELSE 'superseded' END,
       superseded_by_version = CASE WHEN e.current_version = j.version
                           THEN NULL ELSE e.current_version END,
       summary      = :summary,
       completed_at = now()
  FROM encounters e
 WHERE j.job_id        = :job_id
   AND e.encounter_id  = j.encounter_id
   AND j.status        = 'processing'
   AND j.attempts      = :my_attempt
RETURNING j.status;
```

**Two layers of stale-result protection.** The read path already resolves the summary via `current_version`, so a v12 result is unreachable once v13 is current, regardless of what the worker writes. The guarded write is the second layer: it ensures obsolete results are labelled `superseded` rather than `ready`, so job status is truthful for operators and metrics.

**The other two guards:**

- `status = 'processing'` prevents overwriting a job that has already finished. This covers at-least-once delivery: a worker saved its result and crashed before acknowledging, so another worker picked the job up.
- `attempts = :my_attempt` is a fencing token. If worker A hangs on a slow AI call, its lease expires, and worker B reclaims the job and increments `attempts`. When A's call eventually returns, its write matches zero rows, and A learns it has lost ownership. Without this, a slow worker could overwrite a newer attempt's claim.

The result write and the attempt close-out happen in one transaction:

```sql
-- same transaction as the UPDATE above
UPDATE job_attempts
   SET outcome = :resulting_status,   -- 'succeeded' or 'superseded'
       finished_at = now()
 WHERE job_id = :job_id AND attempt_no = :my_attempt;
```

A zero-row return from the job update means the worker lost ownership or the job already completed. The worker discards its result, marks its own attempt row `discarded` (recording that a paid call produced nothing usable), and moves on. It does not retry the write.

### Supersession policy and the cost/complexity trade-off

The brief offers three options for obsolete work: cancel it, mark it superseded, or let it complete. The deciding constraint is that **`generate_summary` has no cancellation API, and its cost is incurred the moment a call is issued.** "Cancel" therefore means different things depending on timing:

| When v13 is accepted, v12's job is... | What happens | Cost impact |
|---|---|---|
| **Not yet called** (`queued`, or claimed but before the call) | The worker's pre-call check sees `version < current_version`, makes no call, marks the job `superseded` and its attempt `skipped`. | **Saves the full call.** This is the only point where money can actually be saved. |
| **In flight** (call already issued) | The call cannot be recalled. It runs to completion; the guarded write labels the result `superseded` and it is retained. | **Saves nothing.** The cost is already spent. Correctness is still preserved, since the read path never surfaces it. |

**Why not attempt true cancellation of in-flight work?** Abandoning the call (dropping the connection or killing the worker) would not refund the call, since the provider has already done the work, and it would add a cross-worker signalling mechanism, one more failure mode, and the loss of a result that was already paid for. All complexity, no saving.

**The trade-off in one line:** the design spends its complexity where it pays off, a single version comparison before the call, and accepts that in-flight work on an obsolete version is a sunk cost. Correctness never depends on cancellation succeeding, because stale-result protection sits in the guarded write and the read path, not in stopping the work.

### Superseded results are retained

Obsolete summaries are kept with `status = 'superseded'` rather than discarded, and each records `superseded_by_version`: the encounter's `current_version` at the moment the job was marked superseded. Every path that marks a job superseded (guarded write, pre-call skip, rejected redrive) sets it.

Note that this is the version that was current *when the decision was made*, not necessarily the immediate successor. Versions can be skipped, and several may arrive while a job is in flight. That is the fact an auditor actually needs: "this result was not shown because v15 was current at the time".

A superseded summary was never shown to anyone as current, so this is not audit of what a clinician saw. Its value is:

- **Cost traceability.** Every paid `generate_summary` call maps to a stored artefact.
- **Debugging.** Summaries across versions of the same encounter can be compared to investigate model behaviour.
- **Completeness.** Any later question about why a summary looked as it did can be answered from the full sequence, not only the survivor.

The cost is that each retained summary is additional patient content to protect, and any retention or deletion policy must cover superseded rows as well as current ones.

### SLA detection and handling

The target is under 10 seconds from `accepted_at` to a `ready` summary. A breach is operational information, not a workflow event: nothing about a job's status, retries, or budget changes because it is late. A job can breach and still go on to `ready`; it becomes `failed` only by exhausting its retry budget.

**The clock is per job.** Each job is measured from its own `accepted_at`. A superseded job is excluded: it never needed to become ready, because it stopped describing the current encounter when the newer version arrived. Example: v12 accepted at t=0, v13 at t=3, v13 ready at t=9. Nothing breached; v12 was superseded, and v13 was ready 6 seconds after its own acceptance.

#### Detection: a scheduled check

A breaching job is by definition one where nothing is happening: it is waiting in the queue, blocked on a slow AI call, sitting behind an open breaker, or held by a crashed worker's lease. None of these write anything, so there is no event to react to. Detection therefore has to actively compare the clock against `accepted_at`.

A scheduled check runs every few seconds:

```sql
SELECT count(*)                                   AS breaching_jobs,
       count(*) FILTER (WHERE status = 'queued')  AS breaching_queued,
       count(*) FILTER (WHERE status = 'processing') AS breaching_processing,
       max(now() - accepted_at)                   AS oldest_unfinished_age
  FROM summary_jobs
 WHERE status IN ('queued', 'processing')
   AND accepted_at < now() - interval '10 seconds';
```

It publishes these as gauges and alerts if breaching work persists (for example, `oldest_unfinished_age` above 60 seconds for 2 minutes). Superseded, ready, and failed jobs fall out of the count by the `status` filter, with no special case. The check is read-only, so running it on more than one instance is harmless.

For completed work, a histogram of `completed_at - accepted_at` for jobs reaching `ready` gives the breach rate over time.

#### Visibility: a computed field on GET

The scheduled check tells operators that *some* jobs are late; it does not tell a client that *their* summary is late. A client polling a `processing` job otherwise cannot tell "5 seconds in, normal" from "3 minutes in, something is wrong". The GET response therefore includes `sla_breached`, computed at read time (see section 4):

- `queued` / `processing`: `now() - accepted_at > 10s`
- `ready`: `completed_at - accepted_at > 10s`
- `failed`: true (a failed job never met the target)

Superseded jobs never reach the GET, since the read path resolves the summary through `current_version`.

#### Handling: diagnose, don't intervene

The workflow does not react to a breach; operators do, and the timestamps already in the schema tell them where the time went:

| Signal | Meaning | Response |
|---|---|---|
| `breaching_queued` high, queue wait (`started_at - accepted_at`) high | Backlog: not enough workers | Scale workers |
| `breaching_processing` high, processing time (`completed_at - started_at`) high | AI service slow | Nothing to fix locally; monitor the provider |
| Breaker `open` or `half_open` | AI outage; jobs deliberately held in `queued` | Wait; breaches are expected and resolve on recovery |
| Isolated jobs past 60s with `lease_expired` attempts | Worker crashes | Investigate the worker, and the transcript if it is always the same job |

**Why a breach does not change the workflow.** Every tempting reaction to lateness spends money or loses work: retrying a slow job early pays twice for the same version, and failing a late job discards a summary that may be seconds from ready. The brief is explicit that a breach is not a failure, and the design keeps the two separate: lateness is observed and alerted on, while failure is decided only by the retry budget.

**Alternatives considered.**

- **Detect on read only (GET computes it, nothing else).** Rejected as the detection mechanism: if nobody polls a late job, nobody learns it is late. Kept alongside the scheduled check for client visibility.
- **Scheduled check writes a flag onto each late job.** Rejected: it reintroduces the stored `sla_breached` column the data model deliberately avoids, a second source of truth that can drift from the timestamps.
- **Return `accepted_at` and let clients compute breach themselves.** Rejected: every client would need to know the 10-second target, and all would be wrong if it changed. Deriving it on the server keeps the rule in one place.

---

## 6. Failure scenarios

Each scenario gives the stored state afterwards, what the client sees, and how processing continues or recovers.

### 6.1 Concurrent duplicate

*Two service instances receive the same event (same `event_id`) at the same moment.*

**What happens.** Neither instance checks whether the event exists; a lookup would race, with both reading "not there". Both attempt the ingestion transaction and the database arbitrates:

1. Both run the encounter upsert and `SELECT ... FOR UPDATE`. Instance A takes the row lock; instance B waits on it.
2. A compares identity, inserts the event, advances `current_version`, creates the job, and commits.
3. The commit releases the lock. B re-reads the row, identity matches, and its event insert hits the `event_id` primary key. The stored hash matches B's, so B classifies the event as duplicate and rolls back.

The row lock only orders the two; the primary key is the guarantee. Without the lock, B's insert would still be rejected by the constraint.

**Stored state.** One encounter update, one `encounter_events` row, one job. Nothing from B.

**Client sees.** A returns `201 Created` (accepted/current). B returns `200 OK` with `"outcome": "duplicate"` and the current version. Both are 2xx: the event has been dealt with, stop sending it.

**Recovery: A crashes after taking the lock, before committing.** Postgres detects the dropped connection and rolls back A's transaction: the event row, version update, job, and (for a brand-new encounter) the shell row all disappear, and the lock is released. B proceeds as if A never existed, inserts the event successfully, and returns `201`. The partner's request to A received no response, so it retries; that retry is now a plain duplicate and gets `200`. The event is accepted exactly once, by whichever instance commits. Because ingestion is a single transaction, a crash can leave all of it or none of it, never part.

### 6.2 Missing or late version

*Version 12 arrives while version 10 is stored. Version 11 arrives later, or never arrives.*

**v12 arrives.** The conditional update compares `10 < 12`, matches, and v12 is accepted: `current_version` becomes 12, the transcript is replaced, and a job is created for v12. The missing v11 does not matter: the brief allows versions to be skipped, and each payload is a complete snapshot, so v12 depends on nothing before it.

**v11 arrives later.** Identity matches and the event insert succeeds (no event exists for `(enc, 11)`), but the conditional update compares `12 < 11`, matches zero rows, and the transaction rolls back, event row included. v11 is classified stale.

**v11 never arrives.** Nothing happens. The design never waits for gaps, so there is nothing to time out or clean up.

**Stored state.** Encounter at v12 with v12's transcript; one job, for v12. No trace of v11: stale events are not stored.

**Client sees.** The partner receives `201 Created` for v12, and `200 OK` with `"outcome": "stale"`, `version: 11` and `current_version: 12` for v11. A GET shows the v12 job (`processing`, then `ready`). No summary is ever generated for v11, which is correct: it would describe an older snapshot than the one stored.

**Recovery.** None needed. If the partner retries v11, it is classified stale again rather than duplicate, because the first delivery was rolled back and never recorded. The partner gets the same answer every time.

**Alternative considered: hold v12 until v11 arrives.** Buffer out-of-order versions and apply them in sequence. Rejected: payloads are complete snapshots, so there is nothing to gain from applying v11 first, and if v11 never arrives (which the brief explicitly allows) v12 would be held forever, or until an arbitrary timeout. It would add a buffer, a timeout, and a new failure mode, to reach the same final state.

### 6.3 Crash after saving

*The encounter update is saved, but the service crashes before handing work to the summary worker.*

**Why this cannot happen here.** There is no separate hand-off step. The job row *is* the hand-off: it is created in the same ingestion transaction as the encounter update, and workers discover work by polling `summary_jobs`. "Encounter saved, job not created" is not a reachable state; both commit or neither does (section 3).

The crash can land in two places:

- **Before commit.** Postgres rolls back everything: encounter update, event row, job. The partner received no response and retries; the retry is accepted fresh.
- **After commit, before the HTTP response is sent.** Encounter, event, and job are all stored. A worker claims the job normally. The partner received no response and retries; the retry hits the `event_id` primary key and is classified duplicate.

**Stored state.** Either nothing (crash before commit, until the retry lands) or the complete accepted state: encounter at the new version, event row, `queued` job.

**Client sees.** The partner's original request fails with no response in both cases. Its retry gets `201 Created` (crash before commit) or `200 OK` duplicate (crash after commit). A GET shows the job progressing normally. The summary is generated exactly once either way.

**Recovery.** Automatic, through the partner's retry and the job's normal claim. No sweeper or reconciliation job is needed, because there is no gap to reconcile.

**The design this scenario describes, and why it was rejected.** The failure is real when the encounter is committed to the database and the work is then pushed to a separate queue (Redis, SQS). A crash between the two leaves the encounter updated with no work queued, and redelivery cannot repair it: the event is already recorded, so the retry is a duplicate and creates nothing. The summary is silently never produced. Keeping jobs in the same database as the encounter removes the gap entirely. A transactional outbox is the upgrade path if an external broker is ever needed for throughput (section 2).

### 6.4 Worker interruption

*A worker crashes during processing, or after saving a result but before acknowledging the job.*

#### Crash during processing

**What happens.** The worker dies mid-attempt, for example while waiting on `generate_summary`. Nothing is written: the job stays `processing`, its attempt row stays `in_flight`, and its lease runs on. Note that it is the **lease** (60s) that frees the job, not the AI timeout (30s): the timeout is enforced by the worker itself, and a dead worker enforces nothing.

Nobody moves the job back to `queued`; no one is alive to do it. Once `lease_expires_at` passes, the claim query's second branch (`processing` with an expired lease) makes it claimable as it stands. The reclaiming worker, in its claim transaction, bumps `attempts` (moving the fencing token), stamps a new lease, marks the dead worker's attempt `lease_expired`, and opens its own attempt.

**Stored state.** Job `processing` under the new owner; the crashed attempt recorded as `lease_expired`; `started_at` unchanged, since it records the first claim.

**Client sees.** `processing` throughout. `sla_breached` becomes true after 10 seconds, since recovery alone can take up to 60. The summary arrives late but normally.

**Cost.** The crashed attempt counts toward the retry budget, because the call may already have been sent and paid for (section 5, retry budget). If the call did go out, its cost is sunk.

**If the worker was stalled, not dead.** A worker that hangs past its lease (process pause, network stall) and later receives its response attempts the guarded result write with its old `my_attempt`. The fence matches zero rows; the worker discards the result and stops. Two calls may have been paid for, but only one result is written.

**A transcript that crashes the worker every time** produces a run of `lease_expired` attempts. On the reclaim after the 5th, the claim transaction's budget check moves the job to `failed` instead of starting a 6th attempt, and it surfaces to operators with `error_class = 'worker_lost'`.

#### Crash after saving a result, before acknowledging

**Why this is already safe.** In a table-backed queue there is no separate acknowledgement: **the commit that saves the result is the acknowledgement.** The guarded write sets `summary` and moves the job to `ready` (or `superseded`) in one statement and one commit, and the claim query selects only `queued` jobs or `processing` jobs with an expired lease. A job with a saved result is neither, so no worker can reclaim it and no second paid call is made.

**Stored state.** Job `ready` with its summary and `completed_at`; attempt `succeeded`.

**Client sees.** `ready` with the summary. The crash is invisible.

**Recovery.** None needed. This is also why the worker has no "result already exists" pre-call check: the state it would guard against cannot be claimed (section 5, pre-call check).

### 6.5 Newer version during processing

*Version 12 is actively being summarized when version 13 arrives.*

**What happens.**

1. v13 is accepted at ingestion, which advances `current_version` to 13 and creates v13's own job in the same transaction. No hand-over is needed; any free worker claims it.
2. v12's `generate_summary` call is already in flight. There is no cancellation API, so it runs to completion.
3. v12's guarded write finds `current_version = 13 ≠ 12`, so it sets the job to `superseded` (not `ready`), stores the summary, and sets `superseded_by_version = 13`. The attempt is closed as `superseded`.

v12 does **not** become `failed`: `failed` means the retry budget was exhausted, and nothing went wrong here. The work simply became obsolete.

**If v13 arrives slightly earlier,** before v12's call is sent (job still `queued`, or claimed but not yet called), the pre-call check sees the newer version, makes no call, and marks the job `superseded` with its attempt `skipped`. That is the only point at which the cost can be saved.

**Stored state.** Encounter at v13. v12 job `superseded` with its summary retained and `superseded_by_version = 13`. v13 job `queued` or `processing`, then `ready`.

**Client sees.** From the moment v13 is accepted, GET resolves the summary through `current_version`, so it reports the v13 job: `processing`, then `ready` with the v13 summary. The v12 summary is never returned as current, at any point, whether or not v12 finishes first.

**Cost.** One paid call for v12 whose result is never shown. This is the accepted sunk cost of in-flight work on an obsolete version; see the supersession trade-off in section 5 for why true cancellation was rejected.

### 6.6 Results out of order

*Version 13 finishes summarizing before version 12.*

**What happens.**

1. **v13's guarded write lands first.** `current_version = 13` equals the job's version, so the job becomes `ready` with its summary.
2. **v12's guarded write lands later.** `current_version = 13 ≠ 12`, so the v12 job becomes `superseded`, its summary retained, `superseded_by_version = 13`.

Terminology: v12 here is **superseded**, not stale. *Stale* is an ingestion outcome (an event older than the stored version, rejected before any job exists). *Superseded* is a job whose version stopped being current. Different tables, different code paths.

**Why arrival order does not matter.** Each result is written onto its own job row, keyed by `(encounter_id, version)`. v12's write can only ever touch the v12 job; it has no path to the v13 row. The two writes share nothing, so whichever lands first, the outcome is the same. And the read path never consults write order at all: it looks up the job whose version equals `current_version`.

**Stored state.** Encounter at v13. v13 job `ready`. v12 job `superseded` with summary retained.

**Client sees.** v13 `processing`, then v13 `ready` with the v13 summary. The v12 result is never visible, before or after it is written.

**The design this scenario tests for.** If the summary were stored on the encounter row (a single `summary` column, overwritten by each worker), the last write would win regardless of version: v12 finishing after v13 would replace the newer summary with an older one. Guarding that write with a version comparison would fix it, but the per-job result, combined with a read path derived from `current_version`, makes the regression structurally impossible rather than dependent on a guard being present (section 2).

### 6.7 AI outage and retry exhaustion

*The AI service is unavailable for 20 minutes.*

#### Timeline

**Minute 0 to trip (seconds to a few minutes).** Calls start failing. Each failed attempt is closed as `transient_error` with its `error_class`, and its job returns to `queued` with a backoff of about 10 seconds (jittered). Each of these writes also runs the breaker trip check. How quickly 11 of the last 20 calls (within 10 minutes) fail depends on the failure mode: fast rejections (service unavailable, rate limited) trip the breaker within seconds; timeouts take 30 seconds each to fail, so tripping takes longer. By the time the breaker opens, a job has used about one attempt, two at most.

**Breaker open (the bulk of the outage).** Workers stop claiming. Queued jobs wait with `next_attempt_at` untouched: no attempt rows, no fencing-token movement, no budget consumed. Calls already in flight when the breaker tripped finish normally and are recorded as usual. Every 30 seconds after a failed probe, one worker wins the race to send the synthetic probe; each failure reopens the breaker for another 30 seconds. Spend for the whole outage is roughly 20 to 40 probe calls, independent of queue depth.

**Recovery (minute 20).** The next probe succeeds and the breaker closes. Workers resume claiming in `accepted_at` order, so the oldest work is summarized first. Throughput is bounded by worker concurrency, so the backlog drains over the following minutes rather than all at once; SLA breaches continue until it clears. Jobs that failed an attempt before the trip resume with 3 or 4 of their 5 attempts remaining; jobs accepted during the outage have all 5. Both complete normally. Jobs whose encounter advanced during the outage are skipped at the pre-call check at no cost.

#### Does any job reach `failed`?

In this scenario, almost certainly none. A job uses at most one or two attempts before the breaker trips and none while it is open. The breaker protects the budgets as well as the spend: without it, jobs would exhaust all 5 attempts in the first few minutes (10 + 20 + 40 + 80 seconds of backoff plus call time) and end the outage `failed`, needing manual redrive.

**When retry exhaustion does happen** is when the breaker cannot protect a job:

- **Partial degradation.** Fewer than 11 of 20 calls fail, so the breaker correctly stays closed, and an unlucky job fails 5 times in a row.
- **False recovery.** The short synthetic probe succeeds but long real transcripts keep failing. Jobs spend attempts until real failures re-trip the breaker.
- **Poison input.** A transcript that crashes the worker, or one the service always rejects (every error is treated as transient; see section 8).

#### After exhaustion: inspection and safe redrive

The job is `failed` with `completed_at` set. It is never retried automatically, so failed work costs nothing further until someone decides it should.

- **Inspect.** Failed jobs are queryable by time window, and each job's `job_attempts` rows show every attempt's outcome, `error_class`, worker, and timings. A run of `transient_error` with `ai_unavailable` points at the provider; a run of `lease_expired` with `worker_lost` points at the worker or the transcript. No patient content is needed to tell them apart.
- **Redrive.** An operator action moves the job back to `queued` with `redrive_generation + 1` (a fresh budget of 5), clearing `completed_at`. The attempt history from earlier generations is kept, so the job's total cost stays visible.
- **Safety.** Redrive applies only if the job's version still equals `current_version`. A failed job whose encounter has since advanced is marked `superseded` instead, since paying to summarize it would produce a result the read path can never show. Redriving in bulk after an outage is therefore safe: obsolete jobs are filtered out by the same check.

#### What the client sees throughout

| Phase | GET response |
|---|---|
| Before the job's 10 seconds are up | `processing`, `sla_breached: false` |
| Rest of the outage | `processing`, `sla_breached: true` (queued jobs are reported as `processing`) |
| After recovery | `ready` with the summary; `sla_breached: true`, since it finished late |
| If the job exhausted its budget | `failed`, with the attempt count and `error_class` of the last attempt, `sla_breached: true` |

The partner's POSTs are unaffected throughout: ingestion does not depend on the AI service, so new events are accepted, and their jobs queue behind the breaker.

**Operators see** the breaker state change to `open`, rising `breaching_queued` and `oldest_unfinished_age`, and the SLA alert firing after 2 minutes. The diagnosis table in section 5 maps this combination to "AI outage; wait".

### 6.8 Inconsistent identity fields

*An event for an existing `encounter_id` arrives with a different `patient_id` or `encounter_type` than previously stored.* Example: `enc-42` is stored against `pat-77`; an event for `enc-42` arrives carrying `pat-99`.

**What happens.** Policy is first patient wins (section 1).

1. Ingestion step 1 locks the encounter row with `FOR UPDATE`, so the stored identity cannot change while it is compared.
2. Step 2 compares stored against incoming. `pat-99 ≠ pat-77`: the transaction rolls back. No event row, no version change, no job.
3. The partner receives `409 Conflict` with `"outcome": "identity_conflict"` and the conflicting field name. The response does not echo the stored `patient_id`: the caller has just shown it holds incorrect identity data, and confirming the real value would leak patient linkage.
4. A structured log entry records both identity values with the event and encounter IDs, and a dedicated metric increments so identity conflicts surface separately from ordinary validation errors.

An `encounter_type` mismatch is handled identically: any contradiction of fixed identity means the partner's mapping is wrong.

**Stored state.** Unchanged. `enc-42` remains bound to `pat-77` with its existing version, transcript, and jobs.

**Client sees.** The partner gets `409`, and the same `409` on every retry, since nothing was recorded. A GET for `enc-42` is unaffected: it continues to show `pat-77`'s encounter and summary, and any in-progress job continues normally.

**Recovery.** None is automatic, deliberately. (If the *stored* binding is the wrong one, the design cannot correct it through the API; see section 8.) A `4xx` tells the partner that resending will not help; the fix is in the partner's mapping. The metric and log give operators what they need to raise it with the partner without reading any transcript.

**Race on a brand-new encounter.** If the first two events for a new `encounter_id` arrive at the same time carrying different patients, one instance creates the row and the other's upsert does nothing. Because each instance re-reads the stored identity under the row lock rather than trusting its own insert, the second compares against what was actually stored and is rejected with `409` (section 3, step 2).

**Related: conflicting payload for the same version.** The brief's second invariant is handled the same way. If an event arrives for an `(encounter_id, version)` already stored, but with a different transcript, the payload hashes differ. The transaction rolls back and the partner gets `409 payload_conflict`. The stored version, its job, and its summary are untouched, because the first recorded payload wins, as with identity. A log entry with IDs and both hashes, plus a dedicated metric, flag it as a source defect (section 1).

---

## 7. Verification

### Test harness

The guarantees in this design rest on Postgres itself (unique constraints, row locks, conditional updates, single-transaction commits), so the tests run against a **real Postgres instance**, never an in-memory fake. A fake would pass every concurrency test while proving nothing about the constraints that actually do the work.

- **`generate_summary` is a controllable mock.** It counts every call, records whether each input was a real transcript or the synthetic probe, and can be set to succeed, fail with a chosen error class, delay, or hang.
- **Concurrency is forced, not hoped for.** Requests are released together from a barrier, and a test hook can hold one transaction open after it takes the encounter row lock, so the second request is guaranteed to arrive while the first is mid-transaction. Each concurrency test also repeats many times (for example 200 iterations, a fresh `encounter_id` each time) to shake out interleavings the hook does not force.
- **Crashes are injected at named points** (for example "after job insert, before COMMIT"), by killing the service process there.
- **Time is controllable.** Backoff, lease, and breaker cooldown durations are configurable, so an outage test compresses minutes into seconds without changing the logic.

Assertions are made on stored state, HTTP responses, and the mock's call counter. Never on logs, since the point of several tests is that what matters is in the database.

### The five tests

Coverage against the brief's two required types: tests 1, 2 and 3 are concurrency cases; test 4 is the crash/retry case; test 5 covers retry behaviour and cost under an outage.

#### Test 1 — Concurrent duplicate is accepted exactly once

*Proves: the `event_id` primary key, not an application check, decides duplicates (section 3, 6.1).*

- **Setup.** Encounter `enc-42` at version 10.
- **Action.** Two service instances receive the identical `evt-102` (`enc-42`, version 12) at the same moment. The hook holds instance A's transaction after the row lock, so B waits on it.
- **Expected.**
  - Exactly one `201 Created` with `"outcome": "accepted"`, and one `200 OK` with `"outcome": "duplicate"`. Both report `current_version: 12`.
  - One `encounter_events` row for `evt-102`, one `summary_jobs` row for `(enc-42, 12)`, encounter at version 12.
  - Repeated with a brand-new encounter (no row before the event): same outcome, and no orphan shell row at version 0.

#### Test 2 — The stored version never regresses, and v12 is never shown as current

*Proves: the conditional update is a true compare-and-swap with no read-then-write gap (section 3), and the read path never surfaces an obsolete summary (section 4, 6.5, 6.6).*

- **Setup.** Encounter `enc-42` at version 10.
- **Action.** v12 and v13 (different `event_id`s) arrive concurrently. Repeated many times, alternating which is released first. Workers then process whatever jobs exist, while GET is sampled throughout.
- **Expected, on every iteration.**
  - Final `current_version = 13`, and the stored transcript is v13's.
  - v13 always gets `201`. v12 gets `201` if it landed first (and was then overtaken) or `200 stale` if it landed second. There is never an iteration in which the encounter ends at 12.
  - v13's job ends `ready`. A v12 job exists only if v12 got `201`, and it ends `superseded` (either skipped at the pre-call check or labelled by the guarded write).
  - No sampled GET ever returns v12's summary, or reports version 12 once v13 is committed.

#### Test 3 — Identity conflict on a brand-new encounter

*Proves: the read-back under `FOR UPDATE` closes the upsert race, and first patient wins (section 3 step 2, 6.8).*

- **Setup.** No row for `enc-900`.
- **Action.** Two first events for `enc-900` arrive concurrently: one with `pat-77` at version 1, one with `pat-99` at version 2. Different versions, so identity is the only thing in conflict.
- **Expected.**
  - Exactly one `201` and one `409` with `"outcome": "identity_conflict"` and `"conflicting_field": "patient_id"`. Which patient wins is not deterministic; the test asserts the invariant, not the winner.
  - The `409` body contains neither patient ID.
  - `enc-900` is bound to the winner's patient, with exactly one event row and one job, both the winner's. Nothing from the loser is stored.
  - The identity-conflict metric has incremented by one.
  - Retrying the losing event returns the same `409`.

#### Test 4 — A crash around the ingestion commit loses no work

*Proves: the job row is the hand-off; "encounter saved, job not created" is unreachable (section 3, 6.3).*

- **Setup.** Encounter `enc-42` at version 10.
- **Action A: crash before commit.** Kill the service after the job insert, before `COMMIT`. The partner receives no response and retries `evt-102`.
  - After the crash: no event row, no job, encounter still at 10.
  - The retry gets `201`. Exactly one job for version 12.
- **Action B: crash after commit, before the response.** Kill the service after `COMMIT`, before the HTTP response is sent. The partner retries.
  - After the crash: encounter at 12, one event row, one `queued` job.
  - The retry gets `200 duplicate`. Still exactly one job.
- **Expected in both.** A worker then processes the job: the mock records exactly one call for version 12 and the job reaches `ready`. Across both cases, the count of encounters whose `current_version` has no matching job is zero.

#### Test 5 — An outage costs a bounded number of calls, and no work is lost

*Proves: the circuit breaker caps system-wide spend independent of queue size; the synthetic probe spends no job's budget; an SLA breach is not a failure (section 5, 6.7).*

- **Setup.** 100 jobs `queued`, 4 workers, breaker `closed`. The mock returns `ai_unavailable` immediately for every call during the simulated 20-minute outage, then succeeds.
- **Expected during the outage.**
  - Real-transcript calls stop once 11 of the last 20 counted attempts have failed: near 11 plus the calls already in flight (up to 4). Not the roughly 500 that 100 jobs × 5 attempts would allow.
  - Once the breaker is open, no new `job_attempts` rows are created and no job's `attempts` changes.
  - Each probe call carries the synthetic transcript, never a real one; `probes_sent` equals the mock's count of probe calls, at about one per cooldown.
  - No job reaches `failed`. GET for any job returns `processing` with `sla_breached: true` once past 10 seconds.
- **Expected after recovery.**
  - A probe succeeds and the breaker is `closed`.
  - All 100 jobs reach `ready`. Each job's counted attempts are within its budget of 5.
  - GET returns `ready` with `sla_breached: true` for jobs that finished late.

### Not covered by these five

Test 2 checks the read path and supersession, but it does not force a specific result order or a lease expiry. The first two tests to add:

- **Forced results out of order** (6.6): the mock is scripted so v13's call returns before v12's. Expected: v13 `ready`, v12 `superseded` with `superseded_by_version = 13`, and GET never returns v12's summary.
- **Stalled worker fencing** (6.4): worker A's call is held past the 60-second lease, worker B reclaims and succeeds, then A's call returns. Expected: A's write matches zero rows and its attempt is `discarded`; B's result stands; the attempt history shows `lease_expired` then `succeeded`, and the budget counts both.

The five chosen instead concentrate on the guarantees the partner and the budget depend on directly: no double acceptance, no regression, no identity rebinding, no lost work, bounded spend.

### Observability: investigating a stuck or late summary

The design does not prevent a summary from being late. It bounds how late it can be and makes the cause visible. A summary can legitimately sit for minutes during an AI outage, a backlog, a slow provider, repeated worker crashes, or encounter-level starvation (section 8). The job of observability is to tell those apart quickly, without anyone reading a transcript.

#### Two sources, split by purpose

- **Metrics** are published continuously, can be alerted on, and show trends, but they are aggregates. Anything an operator would want to be woken up by, or to see over time, is a metric.
- **Database queries** give full per-job detail, but only when someone asks. Anything needed only once the specific job is known stays in the database.

**Metrics.** The SLA gauges and completion histogram (section 5) are not repeated here; this list adds to them.

| Metric | Type | What it answers |
|---|---|---|
| Breaker state, `probes_sent` | Gauge, counter | Is the AI service considered down? How much has probing cost? |
| Queue depth, split into *due now* (`queued`, `next_attempt_at <= now()`) and *waiting on backoff* | Gauge | Is work piling up because nobody is free to take it, or because it is deliberately waiting? |
| Workers busy vs worker slots | Gauge | Is worker capacity saturated? Due-now depth rising with all slots busy means "scale workers". |
| `breaching_jobs`, `breaching_queued`, `breaching_processing`, `oldest_unfinished_age` | Gauge | How many jobs are late, and where are they waiting? (section 5) |
| Queue wait (`started_at − accepted_at`) and processing time (`completed_at − started_at`) | Histogram | Is lateness coming from the backlog or from the AI call? |
| `generate_summary` call latency and call count | Histogram, counter | Is the provider slow? What is the spend rate? |
| Attempts closed, by `outcome` and `error_class` | Counter | Is the failure mix shifting (for example a sudden rise in `lease_expired`)? |
| Jobs reaching `failed` | Counter | Has any work exhausted its budget and now needs a human decision? |
| Ingestion outcomes by type (`accepted`, `duplicate`, `stale`, `identity_conflict`, `payload_conflict`), with source-defect duplicates counted separately | Counter | Is the partner behaving? (section 1) |

**Queries.** For one job: its row in `summary_jobs` (status, `accepted_at`, `started_at`, `completed_at`, `lease_expires_at`, `next_attempt_at`) and its `job_attempts` rows (outcome, `error_class`, worker, start and finish times). This is the per-job story no metric can tell.

#### Alerts

- **SLA:** `oldest_unfinished_age` above 60 seconds for 2 minutes (section 5).
- **Breaker open** for more than a few minutes: an outage long enough for someone to know about, even though the system handles it without intervention.
- **Any job reaches `failed`:** failed work is never retried automatically, so it waits for a person until someone looks.
- **Identity conflicts above zero:** a partner mapping bug with clinical-safety consequences (section 6.8).

#### Triage order

1. **One job or many?** Check the breaching gauges and breaker state first. If many jobs are late, the cause is system-wide, and the diagnosis table in section 5 maps the combination of signals to its cause: backlog, slow provider, or outage. Looking at an individual job first would send the investigation down the wrong path.
2. **Where did this job's time go?** If it is only this job, its timestamps split the wait: no `started_at` means it was never claimed; a large queue wait means it waited in the queue; a large processing time means the time went on the AI call or on recovery from a crash.
3. **What do its attempts say?** The `job_attempts` history identifies the case by its signature:

| Attempt history | Meaning |
|---|---|
| No attempts | Never claimed: waiting in the queue, on a backoff, or behind an open breaker |
| A run of `transient_error` | An unlucky job in a partly degraded service; retrying within its budget |
| A run of `lease_expired` / `worker_lost` | Worker crashes. If it is always this job, a poison transcript |
| One `in_flight` attempt, long-running | A call currently waiting on the provider; the lease and the AI timeout bound it |

#### No patient content anywhere in the telemetry

Metrics, logs, and alerts carry only opaque identifiers (`encounter_id`, `job_id`, `event_id`, `worker_id`; `patient_id` appears only in the identity-conflict log, where both values are needed to diagnose the partner's mapping bug, section 1), statuses, fixed enum values (`outcome`, `error_class`), payload hashes, timestamps, and durations. Transcripts and summaries are never logged, and neither is the raw error text from `generate_summary`, since a provider error may echo part of the input. Every question in the triage above is answered from those fields alone, which is what makes this possible: diagnosing a stuck summary never requires opening the transcript.


---

## 8. Known limitations

**No access control.** The largest gap. Every response returns patient linkage to any caller who knows an encounter ID, and any caller can submit events for any encounter. For a system holding clinical transcripts this is not shippable as-is. It is excluded because the brief specifies no auth model, not because it is unimportant. What would be needed: authenticated callers, authorization scoped to the patients a caller may see, an audit trail of every read, and a narrower GET response for roles that need progress but not content.

**A wrong first binding cannot be corrected through the API.** First patient wins assumes the first event's identity was correct. If the partner's first event for an encounter carried the wrong `patient_id`, that encounter's transcripts and summaries are attached to the wrong patient, and every later, correct event is rejected with `409`: the policy that prevents accidental rebinding also blocks the legitimate fix. The `409` metric and logs make the situation visible, but correcting it needs a manual operator action outside the API: rebind the encounter after verifying the correct patient with the partner, record who made the change and why, and decide what happens to summaries already generated under the wrong identity. Left out because the brief states the source does not send conflicting identity, and an automated correction path would reopen exactly the rebinding risk the policy exists to close.

**Encounter-level starvation is not detected.** The SLA clock is per job and superseded jobs are excluded from breach counts. If an encounter's versions keep arriving faster than summaries can be generated (for example a new snapshot every 8 seconds while each summary takes 10), every job is superseded before it finishes: no job ever breaches, yet the client never sees a ready summary for that encounter. Accepted as a limitation because a partner re-sending snapshots faster than summaries can be produced is unusual. Detecting it would need a second, per-encounter measure (time since the encounter's first accepted version with no `ready` summary), with its own alert.

**Non-retryable errors are not distinguished.** The `generate_summary` contract in the brief describes only transient failures, so the design treats every error as transient. Two consequences, both bounded:

- An input the service will always reject (oversized or malformed transcript) is retried until its budget is spent: up to 5 wasted paid calls, then `failed`.
- A burst of such rejections can trip the circuit breaker while the service is healthy, pausing all work. The synthetic probe then succeeds and the breaker closes after one cooldown, so the cost is a pause of roughly 30 to 60 seconds.

What it would take, once the real provider's error codes are known: the worker maps each error to `transient` (for example HTTP 429, 503, timeouts) or `permanent` (for example 400, 413). A new attempt outcome, `permanent_error`, moves the job straight to `failed` and still counts as a paid call. The breaker counts `permanent_error` as the service having responded, not as a failure, since a correct rejection says the service is up. The remaining design decision is the default for an error on neither list: treating it as transient caps the waste at the budget, while treating it as permanent risks failing a job over a blip.

**Summaries are not validated.** `ready` means `generate_summary` returned a summary, not that the summary is correct. Nothing checks the output against its transcript before it is served, so a hallucinated medication, a wrong dose, or an invented symptom would be shown to a clinician as the current summary. The brief scopes the model out of the exercise, and a meaningful check needs clinical input on what must be verified, so the design treats the output as opaque. What it would take:

- A validation step between the call and the guarded write, for example checking that every medication, dose, and vital sign named in the summary also appears in the transcript.
- A new job status such as `needs_review` for summaries that fail the check, served to clients as flagged rather than as `ready`. A validation failure is not transient, so it must not trigger a retry: regenerating would pay again for a non-idempotent call with no reason to expect a better answer.
- Independently of any check, clients labelling every summary as AI-generated and to be verified against the source.

**No retention or deletion policy.** Patient content lives in three columns: `encounters.transcription`, `summary_jobs.input_transcription`, and `summary_jobs.summary`. Every accepted version keeps its own transcript copy in its job row, and superseded summaries are retained (section 5), so content accumulates with every version and is never removed. Clinical records typically carry both minimum retention periods and deletion obligations that vary by jurisdiction, and the design defines neither. The other tables (`encounter_events`, `job_attempts`, `circuit_breaker`) hold no patient content, so a policy would touch only those three columns. What it would take: a scheduled purge that clears `input_transcription` and `summary` on terminal jobs that are no longer current once a retention period passes, a rule for the current summary set by clinical record-keeping requirements, and a deletion path for an encounter as a whole.


---

## Review feedback considered and rejected

### Taking `FOR SHARE` on the encounter row in the result write

**Feedback.** The guarded write reads `encounters` without locking it. If ingestion commits v13 at the same moment, the worker can read `current_version = 12`, mark v12 `ready`, and commit just before v13 lands, leaving a `ready` job that is no longer current. Suggested fix: take `FOR SHARE` on the encounter row inside the result-write transaction, so it serialises against ingestion's `FOR UPDATE`.

**Why rejected.** The lock only changes which way that one narrow window resolves; it does not change what `ready` means. Any `ready` job stops being current the moment a later version is accepted, whether that is a millisecond or ten minutes after its result was written, and it keeps its `ready` status either way. So `ready` means *the version was current when its result was saved*, with or without the lock. Whether a summary is current *now* is answered only by the read path, which always resolves through `current_version`, and that layer is unaffected by the race: the client can never be shown v12 once v13 is committed. Adding the lock would introduce waits between workers and ingestion without making any status more accurate in general or protecting any client.

### Superseding older queued jobs at ingestion

**Feedback.** When v13 is accepted, a v12 job waiting in backoff stays `queued` until a worker claims it and hits the pre-call check. Adding `UPDATE summary_jobs SET status = 'superseded' WHERE encounter_id = $1 AND version < $2 AND status = 'queued'` to the ingestion transaction would make status accurate immediately and save a claim and an attempt row. (Same as option C in section 5.)

**Why rejected.**

- **It does not replace the pre-call check.** Jobs already claimed are `processing`, not `queued`, so the worker check must stay. The result would be two mechanisms doing the same job, in two different code paths.
- **It widens the ingestion transaction.** Ingestion currently touches only the encounter row and the rows it creates. Superseding other jobs means writing rows workers also lock, so an ingestion can end up waiting on a concurrent claim. Keeping ingestion scoped to its own rows keeps the partner-facing path simple and its latency independent of worker activity.
- **What it saves costs no money.** A stale claim costs one database round trip and a `skipped` attempt, which never consumes budget and never calls the AI. The pre-call check already guarantees no paid call is made for obsolete queued work.
- **The status lag is invisible to clients.** GET resolves through `current_version`, so an obsolete `queued` job is never reported. The only effect is that queue depth briefly counts work that will be skipped, which matters only under bursts of versions.

Kept as the upgrade path if bursts of versions ever make queue depth misleading.

## Additional decisions

### Ingestion keeps accepting events during an AI outage

**Decision.** POSTs are accepted normally while the AI service is down, even though the summary queue grows for the length of the outage.

**Why.**

- **Storing encounter state is the service's core job and does not depend on the AI.** The brief asks for the latest state to be stored and the summary generated in the background. Rejecting events would turn an outage of the summariser into an outage of the whole service.
- **Rejecting does not remove the work, it moves it to the partner.** A `503` makes the partner hold and retry the events, and if its retries give up, updates are lost permanently. Queued in Postgres, they are durable.
- **Waiting jobs are cheap.** A queued job costs no AI calls and no retry budget while the breaker is open, only a row.
- **Much of the backlog removes itself.** If an encounter receives several versions during the outage, the older jobs are skipped at the pre-call check with no call made. Only the latest version of each encounter is summarised.

**Costs accepted.**

- **Slow drain after recovery.** The backlog clears at worker speed, so SLA breaches continue for a while after the service returns. Scaling workers is the lever; queue depth and worker-saturation metrics (section 7) show when it is needed.
- **Storage.** Each queued job carries a copy of its transcript, which matters only for very long outages.

**Upgrade path: backpressure.** Return `503` with `Retry-After` once queue depth passes a high threshold. This protects the database in an extreme outage, at the cost of pushing work back to the partner. Not needed for an outage of the scale in the brief (20 minutes).
