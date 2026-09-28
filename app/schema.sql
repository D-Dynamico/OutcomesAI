-- Design section 2 (schema) and section 5 (circuit_breaker), verbatim.
-- Applied once, inside one transaction, by app/db.py:apply_schema.

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

-- Design section 5: circuit breaker
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
