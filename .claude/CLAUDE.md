# Personal Dashboard

A local personal dashboard. One Python process that collects data from my own
sources, owns a SQLite database, and serves a web interface on `127.0.0.1:8080`.
Single user, never deployed, never shared.

This document is the contract. Every issue points here. Deviations from this
document are discussed in the issue, not decided mid-pull-request.

---

## Non-goals

No multi-user, no login, no sharing, no mobile app, no cloud deploy,
no external access, no AI features.

---

## Workflow: one issue at a time

When you are asked to do issue N, you do issue N. Nothing else.

1. Read the issue in full, plus the sections of this file it references.
2. Branch from the default branch as `issue-N-short-slug`.
3. Implement only what is listed under that issue's **Scope**.
4. Verify every line under **Acceptance criteria** by running it. Do not assert
   that a criterion holds by reading the code.
5. Open one pull request that closes that issue and nothing else.
6. Stop. Do not begin issue N+1, and do not ask to.

Hard rules:

- Never implement, refactor, or "prepare the ground for" work belonging to
  another issue, even when it is one line and obviously next. That change goes
  in that issue's pull request.
- If something outside this issue is genuinely missing and blocks you, say so
  and stop. Do not build it as a bonus.
- If you notice a bug, a missing test, or a better structure outside the scope
  you were given, open a new issue describing it and continue with the issue you
  are on. Do not fix it here.
- Do not touch files outside the paths named in the issue's Scope. The
  exceptions are tests covering that scope, and `pyproject.toml` when a
  dependency is genuinely required.
- One pull request per issue. Do not combine two issues into one pull request,
  however small they are.
- If the issue and this file contradict each other, stop and ask. Do not pick
  one and proceed.

The reason is reviewability. Every pull request must stand on its own, and a
rejected one must not drag unrelated work down with it.

---

## Commits and pull requests

Do not add any attribution to commits or pull requests. No `Co-Authored-By: Claude`
trailer, no "Generated with Claude Code" footer, no session link, no tool name
anywhere in a commit message, branch name, pull request title or body.

Commit messages describe the change and nothing else. This is enforced in
`.claude/settings.json` with `includeCoAuthoredBy: false`, and it also applies to
anything written by hand.

---

## Stack

Python 3.12, FastAPI, uvicorn, APScheduler, SQLite (stdlib `sqlite3`),
httpx, Jinja2, pydantic-settings, pytest.

No frontend toolchain. No npm, no build step, no CDN.

---

## Directory layout

    app/
      main.py               FastAPI app, lifespan, mounts
      config.py             pydantic-settings, reads .env
      db/
        connection.py       connection, WAL, serialized writes
        migrate.py          migration runner
        migrations/         001_init.sql, 002_....sql
      collect/
        base.py             Collector protocol, Retention
        runner.py           scheduling, timeout, retry, run rows
        registry.py         discovers and registers collectors
        sources/            one file per source
      project/
        base.py             Projector protocol, DerivedItem
        registry.py
        reproject.py        reprocessing from snapshots
      api/
        summary.py
        items.py
        sources.py
      web/
        templates/          Jinja2
        static/             css and js, served as files
      retention.py
      backup.py
    cli.py                  command line: migrate, serve, reproject, backup
    tests/

---

## Database schema

SQLite in a single file, WAL mode. Schema changes are numbered SQL files in
`app/db/migrations/`, applied in order at startup.

Migrations are append-only. A migration that has been applied is never edited.

### 001_init.sql

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

`UNIQUE (source_id, external_id)` works for user rows because SQLite treats NULL
as distinct from NULL. User rows have both as NULL.

The domain tables `transactions`, `workouts`, `service_checks` and `applications`
come in later migrations, not in the core.

---

## Three classes of data

1. `origin='derived'` — written by a projector from a snapshot.
   Can be deleted and rebuilt freely.
2. `origin='user'` — written by the user in the interface, with
   `source_id IS NULL`. Never touched by reprocessing.
3. `user_overrides` — the user's edits to derived rows, for example checking off
   an assignment that came from NTNU. Keyed on external id, not on row id.

---

## Collector protocol

A collector fetches raw data. It interprets nothing and does not touch the database.

    class Retention(StrEnum):
        FOREVER     = "forever"       # irreplaceable: bank CSV, workout export
        LATEST_ONLY = "latest_only"   # source is its own archive: calendar, GitHub
        DAYS        = "days"          # ephemeral: Entur departures
        DOWNSAMPLE  = "downsample"    # measurements: response time, quotas

    class Collector(Protocol):
        name: str
        interval_s: int
        retention: Retention
        retention_days: int | None
        timeout_s: float

        async def fetch(self) -> Any:
            """Returns a JSON-serializable raw payload. Raises on failure."""

Rules:

- `fetch` performs no database access and no translation into domain concepts.
- `fetch` raises on failure. The runner catches.
- Secrets are read from `config`, never from the database, never hardcoded.

## Projector protocol

A projector reads one snapshot and returns derived rows. It is pure: no network,
no writes, same input gives same output.

    @dataclass(frozen=True)
    class DerivedItem:
        external_id: str
        kind: str
        title: str
        section: str
        detail: str | None = None
        due_at: datetime | None = None
        actionable: bool = False

    class Projector(Protocol):
        source_name: str
        def project(self, snapshot: Snapshot) -> Sequence[DerivedItem]:
            ...

The runner writes the rows, upserting on `(source_id, external_id)`.

---

## Reprocessing

`python cli.py reproject <source>` does the following in one transaction:

1. Delete every `items` row for that source with `origin='derived'`.
2. Read the source's snapshots in ascending `fetched_at`.
3. Run the projector on each, upserting the results.
4. Run `apply_overrides()`.

`apply_overrides()` writes the values in `user_overrides` onto matching rows.
Because it runs after every projection, reads are always a plain query with no
join against overrides. The front page must stay a single query.

Reprocessing reaches only as far back as retention has kept snapshots. That is a
deliberate choice per source.

---

## Runner

- One APScheduler job per source, interval taken from the collector.
- `coalesce=True`, `max_instances=1`, jitter between 0 and 30 seconds. This is
  required because the machine is a laptop that sleeps, and on resume every
  timer comes due at once.
- Every run has a timeout. On timeout or exception: write a `runs` row with
  `ok=0` and the error, increment `consecutive_failures`, leave the other
  sources alone.
- Retry twice with exponential backoff, only on network errors and 5xx.
- On success, hash the payload. If the hash equals the newest snapshot for that
  source, update `last_seen_at` instead of inserting a new row.
- After inserting, run the source's projector on the new snapshot, then
  `apply_overrides()`.
- The last successful result is always kept, however many failures follow.

## Retention

A nightly job registered through the same runner, so that it shows up on the
debug page if it stops working.

- `forever` — delete nothing.
- `latest_only` — keep the newest snapshot per source.
- `days` — delete snapshots older than `retention_days`.
- `downsample` — keep one row per hour older than 7 days, one per day older
  than 90.

---

## HTTP endpoints

    GET    /                       front page
    GET    /debug                  source status
    GET    /health                 liveness, no database access
    GET    /summary                front page data, one call
    GET    /api/sources
    POST   /api/sources/{name}/run run one source now
    GET    /api/items              filters: section, kind, due_before, include_done
    POST   /api/items              create a user item
    PATCH  /api/items/{id}         complete or edit
    DELETE /api/items/{id}         only origin='user'

For derived rows, PATCH and DELETE write to `user_overrides`, never directly to
`items`.

### /summary

    {
      "generated_at": "2026-09-11T19:40:00Z",
      "next_event": {
        "title": "IT1901 lecture",
        "start_at": "2026-09-12T08:15:00Z",
        "location": "R1",
        "leave_home_at": "2026-09-12T07:42:00Z",
        "source": "gcal",
        "as_of": "2026-09-11T19:35:00Z"
      },
      "focus": { "id": 41, "title": "Finish the project description" },
      "today":     [ { "id": 12, "kind": "deadline", "title": "...", "due_at": "...", "actionable": true } ],
      "this_week": [ ],
      "sections": [
        { "key": "ops", "label": "Projects and operations",
          "status": "attention", "message": "2 pull requests waiting on you",
          "as_of": "2026-09-11T19:30:00Z" }
      ],
      "stale_sources": ["entur"]
    }

`status` is one of `ok`, `attention`, `stale`, `error`.

---

## Time

Everything is stored as UTC in ISO 8601 with `Z`. Display is in Europe/Oslo.

`/summary` sends absolute timestamps only. Countdowns and "when do I have to
leave home" are computed in the client. This is what allows `/summary` to be
cached and the front page to paint immediately.

---

## Frontend

Plain HTML, plain CSS, plain JavaScript. No framework, no bundler, no CDN.
Everything is served from `app/web/static/`.

- The front page renders from one call to `/summary`. No other request in the
  render path.
- No call to an external service in the render path, ever. If data is old it is
  shown greyed out with its age. The page never waits for a collector.
- One accent colour is reserved for something that needs action today.
  Everything else is neutral.
- Every field shows when it was last updated and greys out when it is too old.
- Slash focuses the command field. The field both adds items and jumps between
  sections.
- Polling stops when the tab is not visible. Use `visibilitychange`.

---

## Conventions

- Configuration through `pydantic-settings` from `.env`. `.env` is in `.gitignore`.
- Logging to a rotating file in `logs/` as well as stdout. Windows Task Scheduler
  hides stdout, so in practice the file log is the only one that exists.
- SQLite: WAL, `busy_timeout=5000`, `foreign_keys=ON`. One write connection
  behind a lock. Reads may run concurrently.
- The server binds to `127.0.0.1` only. Never `0.0.0.0`.
- Tests run with pytest against a temporary database file, never the real one.

## Rules for working in this repository

- Do not add a collector for a real source unless the issue asks for it.
- Do not introduce a build step, npm dependencies or CDN links in the frontend.
- Do not add new Python dependencies without justifying it in the pull request.
- Do not edit a migration that has already been applied. Add a new one.
- Do not store secrets in the database.
- Keep the pull request inside its issue. If you see something else that should
  be fixed, open a new issue.