-- Synthetic session-store.db schema for the experimental CP-LOCAL store reader.
-- Provenance: third-party. The column set is the one addendum §5.9 lists (required: id, session_id,
-- model, input_tokens, cache_read_tokens, cache_write_tokens, created_at; optional: turn_index,
-- copilot_usage_model, output_tokens, reasoning_tokens, total_nano_aiu, duration_ms, initiator,
-- request_multiplier), taken from the third-party readers tokscale and codeburn
-- (getagentseal/codeburn docs/providers/copilot.md, read 2026-09-25). GitHub does not document this
-- schema (cli-config-dir-reference: "automatically managed and should not be edited"); column
-- types and the sessions table layout are assumptions. Never a copy of a real user's database.
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    cwd TEXT,
    repository TEXT,
    branch TEXT,
    summary TEXT,
    host_type TEXT,
    producer TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE assistant_usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    turn_index INTEGER,
    model TEXT NOT NULL,
    copilot_usage_model TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER,
    reasoning_tokens INTEGER,
    total_nano_aiu INTEGER,
    duration_ms INTEGER,
    initiator TEXT,
    request_multiplier REAL,
    created_at TEXT NOT NULL
);
