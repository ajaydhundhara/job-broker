-- Job Broker schema, version 1 (PRAGMA user_version = 1, set by init_db()).
--
-- Conventions:
--   * Timestamps are INTEGER seconds since the Unix epoch, in SIMULATED time
--     (UTC). Time comes only from the sim_clock table, never the real clock.
--   * IDs are TEXT, e.g. 'job-' / 'node-' + 12 hex chars.
--   * Enumerations are TEXT with CHECK constraints.
--   * Money is never a float: prices are INTEGER micro-dollars (1 USD = 1,000,000).
--
-- Every statement uses IF NOT EXISTS so init_db() can run it on every start.
-- No PRAGMAs or BEGIN/COMMIT here: db.py sets those (see init_db()).


-- The simulated "now". Survives restarts so time resumes where it stopped.
CREATE TABLE IF NOT EXISTS sim_clock (
    id      INTEGER PRIMARY KEY CHECK (id = 1),   -- exactly one row
    now_ts  INTEGER NOT NULL
);


-- The broker's view of rented machines.
-- "GPUs used" is NOT stored. It is derived as SUM(jobs.gpus) of running jobs
-- on the node, so it can never drift.
CREATE TABLE IF NOT EXISTS nodes (
    id                     TEXT PRIMARY KEY,      -- same id the provider returns
    node_type              TEXT NOT NULL CHECK (node_type IN ('gpu-s', 'gpu-m', 'gpu-l')),
    gpus_total             INTEGER NOT NULL,
    interruptible          INTEGER NOT NULL DEFAULT 0 CHECK (interruptible IN (0, 1)),
    -- Micro-dollars; snapshot at launch so later price changes never rewrite history.
    price_per_hour_micros  INTEGER NOT NULL CHECK (price_per_hour_micros > 0),
    -- Owner team; security rule: only this team's jobs run here.
    team                   TEXT NOT NULL,
    state                  TEXT NOT NULL CHECK (state IN
                               ('launching', 'ready', 'terminated', 'failed')),
    launched_at            INTEGER NOT NULL,      -- billing starts here
    ready_at               INTEGER,
    idle_since             INTEGER,               -- set when last job leaves; NULL when busy
    terminated_at          INTEGER,               -- billing ends here
    end_reason             TEXT CHECK (end_reason IN
                               ('idle_reaped', 'launch_failed', 'interrupted', 'manual')
                               OR end_reason IS NULL),

    -- A node type always has the same number of GPUs.
    CHECK ((node_type = 'gpu-s' AND gpus_total = 1) OR
           (node_type = 'gpu-m' AND gpus_total = 4) OR
           (node_type = 'gpu-l' AND gpus_total = 8)),
    CHECK (terminated_at IS NULL OR terminated_at >= launched_at),
    -- A node that has ended must say when, or billing has no end.
    CHECK (state NOT IN ('terminated', 'failed') OR terminated_at IS NOT NULL)
);


-- One row per submitted job.
-- There is no queue table. The queue is jobs WHERE status = 'queued',
-- ordered by the scheduling policy.
CREATE TABLE IF NOT EXISTS jobs (
    id                   TEXT PRIMARY KEY,
    team                 TEXT NOT NULL,
    idempotency_key      TEXT,                    -- client-supplied, optional
    -- sha256 of (team, gpus, estimated_minutes, priority); lets us detect
    -- "same key, different payload" and return 409.
    request_fingerprint  TEXT NOT NULL,
    gpus                 INTEGER NOT NULL CHECK (gpus IN (1, 2, 4, 8)),
    estimated_minutes    INTEGER NOT NULL CHECK (estimated_minutes > 0),
    priority             TEXT NOT NULL CHECK (priority IN ('low', 'normal', 'high')),
    status               TEXT NOT NULL CHECK (status IN
                             ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
    node_id              TEXT REFERENCES nodes(id),
    submitted_at         INTEGER NOT NULL,
    started_at           INTEGER,
    ended_at             INTEGER,
    end_reason           TEXT,                    -- free text, e.g. 'user_cancel', 'node_interrupted'
    requeue_count        INTEGER NOT NULL DEFAULT 0,  -- times put back in queue

    -- NULL keys are allowed many times (SQLite treats NULLs as distinct).
    UNIQUE (team, idempotency_key),
    CHECK (status != 'running' OR (node_id IS NOT NULL AND started_at IS NOT NULL)),
    CHECK (status != 'queued'  OR (node_id IS NULL AND started_at IS NULL)),
    CHECK (status NOT IN ('succeeded', 'failed', 'cancelled') OR ended_at IS NOT NULL),
    CHECK (started_at IS NULL OR started_at >= submitted_at),
    CHECK (ended_at IS NULL OR ended_at >= submitted_at)
);


-- SIMULATOR-ONLY hidden truth (from the trace's actual_minutes).
-- The scheduler must never read this table; only the simulated clock uses it
-- to end jobs. Kept separate on purpose.
CREATE TABLE IF NOT EXISTS sim_job_plans (
    job_id          TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    actual_minutes  INTEGER NOT NULL CHECK (actual_minutes >= 0),
    outcome         TEXT NOT NULL CHECK (outcome IN ('succeeded', 'failed', 'cancelled'))
);


-- The FAKE CLOUD PROVIDER's own state. Separate from `nodes` because the
-- provider is the source of truth for billing: after a restart, its nodes are
-- still alive and billing even if the broker forgot about them.
CREATE TABLE IF NOT EXISTS provider_nodes (
    id                TEXT PRIMARY KEY,
    node_type         TEXT NOT NULL CHECK (node_type IN ('gpu-s', 'gpu-m', 'gpu-l')),
    interruptible     INTEGER NOT NULL DEFAULT 0 CHECK (interruptible IN (0, 1)),
    requested_at      INTEGER NOT NULL,
    ready_at_planned  INTEGER,   -- when it becomes ready (4-8 min after request)
    fails_at_planned  INTEGER,   -- set instead for the ~3% that never become ready
                                 -- (request + 15 min)
    reclaim_at        INTEGER,   -- interruptible only: when it will be taken back
    state             TEXT NOT NULL CHECK (state IN
                          ('pending', 'ready', 'failed', 'terminated')),
    terminated_at     INTEGER,

    -- Exactly one of: becomes ready, or fails.
    CHECK ((ready_at_planned IS NULL) != (fails_at_planned IS NULL))
);


-- Append-only audit log (debugging, the Platform team, the interview).
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           INTEGER NOT NULL,   -- simulated time
    entity_type  TEXT NOT NULL CHECK (entity_type IN ('job', 'node', 'provider', 'system')),
    entity_id    TEXT,
    event_type   TEXT NOT NULL,      -- e.g. 'job_submitted', 'job_started', 'node_launched',
                                     -- 'launch_failed', 'node_reaped'
    details      TEXT                -- JSON string
);


-- Indexes
CREATE INDEX IF NOT EXISTS ix_jobs_queue            ON jobs(status, priority, submitted_at);
CREATE INDEX IF NOT EXISTS ix_jobs_team             ON jobs(team, submitted_at);
CREATE INDEX IF NOT EXISTS ix_jobs_running_on_node  ON jobs(node_id) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS ix_nodes_state           ON nodes(state);
CREATE INDEX IF NOT EXISTS ix_nodes_team            ON nodes(team, state);
CREATE INDEX IF NOT EXISTS ix_provider_state        ON provider_nodes(state);
CREATE INDEX IF NOT EXISTS ix_events_entity         ON events(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS ix_events_ts             ON events(ts);
