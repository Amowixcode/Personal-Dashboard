CREATE TABLE schema_migrations (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- One row per source. The registry upserts these at startup.
CREATE TABLE sources (
    id                    INTEGER PRIMARY KEY,
    name                  TEXT NOT NULL UNIQUE,
    interval_s            INTEGER NOT NULL,
    retention             TEXT NOT NULL
                          CHECK (retention IN ('forever','latest_only','days','downsample')),
    retention_days        INTEGER,
    enabled               INTEGER NOT NULL DEFAULT 1,
    last_run_at           TEXT,
    last_ok_at            TEXT,
    last_error            TEXT,
    last_duration_ms      INTEGER,
    consecutive_failures  INTEGER NOT NULL DEFAULT 0
);

-- Raw data as it arrived. Never overwritten, only removed by retention.
CREATE TABLE snapshots (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER NOT NULL REFERENCES sources(id),
    fetched_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,
    payload       TEXT NOT NULL
);
CREATE INDEX idx_snapshots_source_time ON snapshots(source_id, fetched_at DESC);

-- Run history for the debug page.
CREATE TABLE runs (
    id           INTEGER PRIMARY KEY,
    source_id    INTEGER NOT NULL REFERENCES sources(id),
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    ok           INTEGER,
    error        TEXT,
    duration_ms  INTEGER
);
CREATE INDEX idx_runs_source_time ON runs(source_id, started_at DESC);

-- Everything with a date or a checkbox. One table, not two.
CREATE TABLE items (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL
                  CHECK (kind IN ('task','deadline','exam','renewal','birthday')),
    title         TEXT NOT NULL,
    detail        TEXT,
    due_at        TEXT,
    completed_at  TEXT,
    actionable    INTEGER NOT NULL DEFAULT 0,
    section       TEXT NOT NULL
                  CHECK (section IN ('today','studies','applications','finance','ops','calendar')),
    origin        TEXT NOT NULL CHECK (origin IN ('derived','user')),
    source_id     INTEGER REFERENCES sources(id),
    external_id   TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE (source_id, external_id)
);
CREATE INDEX idx_items_due     ON items(due_at) WHERE completed_at IS NULL;
CREATE INDEX idx_items_section ON items(section, due_at);

-- The user's edits to derived rows. These survive reprocessing.
CREATE TABLE user_overrides (
    id           INTEGER PRIMARY KEY,
    entity_type  TEXT NOT NULL,
    source_id    INTEGER NOT NULL REFERENCES sources(id),
    external_id  TEXT NOT NULL,
    field        TEXT NOT NULL,
    value        TEXT,
    created_at   TEXT NOT NULL,
    UNIQUE (entity_type, source_id, external_id, field)
);
