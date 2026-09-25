# ruff: noqa: E501  (the SPEC §7.1 DDL is transcribed verbatim)
"""Ledger schema (SPEC §7.1) and forward-only migrations (SPEC §7).

The DDL of SPEC §7.1 is part of the contract and is kept verbatim in :data:`SPEC_DDL`. STORE adds a
small number of **private** columns, one private table and one private index
(:data:`STORE_EXTENSIONS`, documented in ``tests/v2/store/CONTRACT-CHANGE-STORE-1.md``) that the SPEC
text needs but does not name: the round trip of ``Request.source``, the serving-inference dimensions
the ``where`` filters read, the order of attempts / inferences, the priced lines behind
``cost_rows`` and the per-contribution record that keeps merges order-independent. No SPEC column is
renamed, retyped or removed, and no other package reads the private ones.

Versions (``meta.schema_version``):

* ``1`` — SPEC §7.1 plus the STORE extensions;
* ``2`` — the GitHub Copilot amendments of addendum §7.1 (``inferences`` routing / compliance /
  context tier, the ``cost_lines`` GitHub columns, ``outcomes.extra_json``, ``pool_nano`` in both
  rollups, index ``cl_date_cc``).

A new store is created at version 1 and migrated forward with the same functions an old store runs,
so every migration is exercised on every install. Migrations are ``migrate_<n>_to_<n+1>(conn)``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from tokenbill.core.errors import UsageError

__all__ = [
    "BUCKET_COLUMNS",
    "DATA_TABLES",
    "EXACT_MASK_BITS",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "SPEC_DDL",
    "STORE_EXTENSIONS",
    "apply_pragmas",
    "create_schema",
    "migrate",
    "migrate_1_to_2",
    "schema_version",
]

#: The schema version this module creates and understands.
SCHEMA_VERSION = 2
#: Page cache per connection (KiB).
CACHE_KIB = 65_536

#: SPEC §7.1, verbatim except for its SQL comments (the contract).
SPEC_DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sources(source_id TEXT PRIMARY KEY, adapter TEXT NOT NULL, name_hmac TEXT, sha256 TEXT,
  name_key_id TEXT, principal_key_id TEXT, ingested_ms INTEGER, records INTEGER, quarantined INTEGER,
  stats_json TEXT);
CREATE TABLE cursors(source_id TEXT NOT NULL, unit_hmac TEXT NOT NULL, byte_offset INTEGER NOT NULL,
  head_sha TEXT NOT NULL, size INTEGER, mtime_ns INTEGER, PRIMARY KEY(source_id, unit_hmac));
CREATE TABLE sessions(session_key TEXT PRIMARY KEY, source_kind TEXT, started_ms INTEGER, ended_ms INTEGER,
  attribution_json TEXT);
CREATE TABLE lanes(lane_key TEXT PRIMARY KEY, session_key TEXT NOT NULL, kind TEXT, parent_lane_key TEXT,
  cache_scope_key TEXT, ttl_observed TEXT, lane_exact INTEGER, team TEXT, billing_class TEXT);
CREATE TABLE requests(request_id TEXT PRIMARY KEY, lane_key TEXT NOT NULL, session_key TEXT NOT NULL,
  seq INTEGER NOT NULL, ts_start_ms INTEGER NOT NULL, date_utc TEXT NOT NULL,
  fidelity INTEGER NOT NULL, source_priority INTEGER NOT NULL, adapter TEXT NOT NULL, sources_mask INTEGER NOT NULL,
  principal TEXT, team TEXT, cost_center TEXT, project TEXT, repo TEXT, workspace_id TEXT, api_key_id TEXT,
  agent_product TEXT, agent_type TEXT, query_source TEXT, skill TEXT, mcp_server TEXT, plugin TEXT,
  workload_class TEXT, entrypoint TEXT, client_version TEXT, billing_path TEXT, cwd_key TEXT, arm TEXT, wave TEXT,
  attr_extra_json TEXT,
  attr_prio_json TEXT, params_json TEXT, appended_json TEXT, fp_json TEXT);
CREATE TABLE attempts(attempt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests ON DELETE CASCADE,
  attempt_no INTEGER, ts_start_ms INTEGER, ttft_ms INTEGER, duration_ms INTEGER, outcome TEXT, http_status INTEGER,
  error_type TEXT, retry_layer TEXT, retry_after_ms INTEGER, should_retry INTEGER, sdk_retry_count INTEGER,
  provider_request_id TEXT, provider_message_id TEXT, model_served TEXT, stop_reason TEXT, diag_reason TEXT,
  diag_provider_reason TEXT, diag_missed_tokens INTEGER, diag_source TEXT, applied_edits_json TEXT,
  thinking_dropped INTEGER, raw_usage_json TEXT, convention_id TEXT);
CREATE TABLE inferences(inference_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts ON DELETE CASCADE,
  request_id TEXT NOT NULL, lane_key TEXT NOT NULL, lane_kind TEXT, ts_ms INTEGER NOT NULL, date_utc TEXT NOT NULL,
  kind TEXT, usage_source TEXT, billable INTEGER, billing_rule_id TEXT, output_upper INTEGER,
  provider TEXT, channel TEXT, model TEXT, model_raw TEXT, service_tier TEXT, speed TEXT, inference_geo TEXT,
  endpoint_scope TEXT, write_ttl_hint TEXT, billing_path TEXT,
  uncached_input INTEGER, cache_read INTEGER, cache_write_5m INTEGER, cache_write_1h INTEGER,
  cache_write_other INTEGER, cache_write_other_ttl_s INTEGER, cache_write_unknown INTEGER, output INTEGER,
  output_reasoning INTEGER, web_search_requests INTEGER, web_fetch_requests INTEGER,
  priced_nano INTEGER,
  exact_nano INTEGER NOT NULL DEFAULT 0,
  est_nano INTEGER NOT NULL DEFAULT 0, est_low_nano INTEGER NOT NULL DEFAULT 0, est_high_nano INTEGER NOT NULL DEFAULT 0,
  evidence TEXT, basis TEXT, unpriced_reason TEXT,
  nano_uncached INTEGER, nano_read INTEGER, nano_w5m INTEGER, nano_w1h INTEGER, nano_wother INTEGER,
  nano_wunknown INTEGER, nano_output INTEGER, nano_server_tools INTEGER,
  exact_mask INTEGER,
  rate_card_sha TEXT, provider_cost_nano INTEGER, provider_cost_basis TEXT,
  team TEXT, principal TEXT, workspace_id TEXT, workload_class TEXT, agent_product TEXT);
CREATE TABLE blocks(h TEXT PRIMARY KEY, key_id TEXT, h_sorted TEXT, h_norm TEXT, tier TEXT, kind TEXT, role TEXT,
  n_bytes INTEGER, est_tokens INTEGER, image_w INTEGER, image_h INTEGER, volatile_classes TEXT, lookback_pos INTEGER,
  deferred INTEGER);
CREATE TABLE events(event_id TEXT PRIMARY KEY, lane_key TEXT NOT NULL, ts_ms INTEGER NOT NULL, date_utc TEXT NOT NULL,
  kind TEXT NOT NULL, attrs_json TEXT);
CREATE TABLE aggregates(agg_id TEXT PRIMARY KEY, source_kind TEXT, bucket_start_ms INTEGER, bucket_end_ms INTEGER,
  dims_json TEXT, usage_json TEXT, reported_cost_nano INTEGER, reported_cost_basis TEXT, list_cost_nano INTEGER,
  finality TEXT, fetched_ms INTEGER);
CREATE TABLE cost_lines(line_id TEXT PRIMARY KEY, source_kind TEXT, date_utc TEXT, channel TEXT, workspace_id TEXT,
  description TEXT, model TEXT, cost_type TEXT, token_type TEXT, sku TEXT, service_tier TEXT, inference_geo TEXT,
  endpoint_scope TEXT, amount_nano INTEGER NOT NULL, list_amount_nano INTEGER, currency TEXT, finality TEXT,
  principal TEXT, fetched_ms INTEGER);
CREATE TABLE outcomes(date_utc TEXT NOT NULL, team TEXT NOT NULL, source_kind TEXT NOT NULL, n_users INTEGER,
  sessions INTEGER, commits INTEGER, pull_requests INTEGER, lines_added INTEGER, lines_removed INTEGER,
  edits_accepted INTEGER, edits_rejected INTEGER, PRIMARY KEY(date_utc, team, source_kind));
CREATE TABLE message_index(provider_message_id TEXT PRIMARY KEY, request_id TEXT NOT NULL);
CREATE TABLE request_index(provider_request_id TEXT PRIMARY KEY, request_id TEXT, collision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE daily_rollup(date_utc TEXT NOT NULL, team TEXT, cost_center TEXT, workspace_id TEXT, workload_class TEXT,
  lane_kind TEXT, model TEXT, billing_path TEXT, arm TEXT, wave TEXT, active_users INTEGER NOT NULL,
  requests INTEGER NOT NULL, uncached_input INTEGER, cache_read INTEGER, cache_write INTEGER, output INTEGER,
  exact_nano INTEGER NOT NULL,
  allowance_nano INTEGER NOT NULL,
  est_low_nano INTEGER, est_high_nano INTEGER, unpriced_inferences INTEGER, rate_card_sha TEXT,
  PRIMARY KEY(date_utc, team, cost_center, workspace_id, workload_class, lane_kind, model, billing_path, arm, wave));
CREATE TABLE cluster_day(date_utc TEXT NOT NULL, cluster_kind TEXT NOT NULL, cluster_id TEXT NOT NULL,
  arm TEXT, wave TEXT, active_users INTEGER NOT NULL, requests INTEGER NOT NULL, exact_nano INTEGER NOT NULL,
  allowance_nano INTEGER NOT NULL, PRIMARY KEY(date_utc, cluster_kind, cluster_id, arm, wave));
CREATE TABLE findings(run_id TEXT NOT NULL, finding_id TEXT NOT NULL, created_ms INTEGER, json TEXT NOT NULL,
  PRIMARY KEY(run_id, finding_id));
CREATE TABLE receipts(receipt_id TEXT PRIMARY KEY, lever_id TEXT, lever_class TEXT, label TEXT,
  realization_rate TEXT, created_ms INTEGER, json TEXT NOT NULL, dsse TEXT);
CREATE TABLE key_events(ts_ms INTEGER NOT NULL, key_kind TEXT NOT NULL, old_key_id TEXT, new_key_id TEXT);
CREATE TABLE audit(ts_ms INTEGER NOT NULL, actor TEXT, action TEXT NOT NULL, detail_json TEXT);
CREATE INDEX req_lane ON requests(lane_key, ts_start_ms, seq);
CREATE INDEX req_date_team ON requests(date_utc, team);
CREATE INDEX lane_team_kind ON lanes(team, kind, billing_class);
CREATE INDEX att_req ON attempts(request_id);
CREATE INDEX inf_date_model ON inferences(date_utc, model);
CREATE INDEX inf_lane ON inferences(lane_key, ts_ms);
CREATE INDEX inf_req ON inferences(request_id);
CREATE INDEX ev_lane ON events(lane_key, ts_ms);
CREATE INDEX cl_date_channel ON cost_lines(date_utc, channel);
"""

#: STORE-private additions to the version-1 schema (CONTRACT-CHANGE-STORE-1). Column meanings:
#:
#: * ``requests.src_id`` / ``src_locator`` — ``Request.source.source_id`` / ``.locator`` (NULL when
#:   the request has no ``SourceRef``); the SPEC columns keep only its ``adapter`` / ``fidelity`` /
#:   ``source_priority``.
#: * ``requests.req_model`` (``Request.model``), ``eff_billing_path`` (``attribution.billing_path``
#:   or the serving inference's), ``serving_channel`` / ``serving_provider`` (the serving inference's
#:   pricing context), ``serving_cache_read`` (its ``cache_read``, NULL without a serving
#:   inference): the request-level dimensions of ``where`` filters and ``lane_first_reads``.
#: * ``attempts.ord`` / ``inferences.ord`` — position within the request / attempt.
#: * ``inferences.lines_json`` — the priced lines ``[bucket, quantity, amount, low, high, exact,
#:   rate_row_id]`` behind ``cost_rows`` (the SPEC columns hold per-bucket points only).
#: * ``merge_members`` — every contribution of a request merged from ≥ 2 contributions (the
#:   canonical JSON of the cleaned request and its source metadata), so a later source re-merges
#:   the whole set (§7.3 order independence); single-contribution requests are their own row.
#: * index ``inf_att`` on ``inferences(attempt_id)`` — the ``ON DELETE CASCADE`` child key.
STORE_EXTENSIONS = """
ALTER TABLE requests ADD COLUMN src_id TEXT;
ALTER TABLE requests ADD COLUMN src_locator TEXT;
ALTER TABLE requests ADD COLUMN req_model TEXT;
ALTER TABLE requests ADD COLUMN eff_billing_path TEXT;
ALTER TABLE requests ADD COLUMN serving_channel TEXT;
ALTER TABLE requests ADD COLUMN serving_provider TEXT;
ALTER TABLE requests ADD COLUMN serving_cache_read INTEGER;
ALTER TABLE attempts ADD COLUMN ord INTEGER NOT NULL DEFAULT 0;
ALTER TABLE inferences ADD COLUMN ord INTEGER NOT NULL DEFAULT 0;
ALTER TABLE inferences ADD COLUMN lines_json TEXT;
CREATE TABLE merge_members(request_id TEXT NOT NULL, member_key TEXT NOT NULL, orig_request_id TEXT NOT NULL,
  adapter TEXT NOT NULL, source_id TEXT NOT NULL, fidelity INTEGER NOT NULL, priority INTEGER NOT NULL,
  principal TEXT, ts_start_ms INTEGER NOT NULL, doc TEXT NOT NULL, PRIMARY KEY(request_id, member_key));
CREATE INDEX mm_orig ON merge_members(orig_request_id);
CREATE INDEX mm_principal ON merge_members(principal);
CREATE INDEX inf_att ON inferences(attempt_id);
"""

#: Copilot addendum §7.1 (version 2).
_COPILOT_DDL = """
ALTER TABLE inferences ADD COLUMN routing TEXT;
ALTER TABLE inferences ADD COLUMN compliance TEXT;
ALTER TABLE inferences ADD COLUMN context_tier TEXT;
ALTER TABLE cost_lines ADD COLUMN quantity TEXT;
ALTER TABLE cost_lines ADD COLUMN unit TEXT;
ALTER TABLE cost_lines ADD COLUMN cost_center TEXT;
ALTER TABLE cost_lines ADD COLUMN team TEXT;
ALTER TABLE cost_lines ADD COLUMN repo TEXT;
ALTER TABLE cost_lines ADD COLUMN workload TEXT;
ALTER TABLE cost_lines ADD COLUMN workflow TEXT;
ALTER TABLE cost_lines ADD COLUMN routing TEXT;
ALTER TABLE cost_lines ADD COLUMN speed TEXT;
ALTER TABLE cost_lines ADD COLUMN pseudo TEXT;
ALTER TABLE outcomes ADD COLUMN extra_json TEXT;
ALTER TABLE daily_rollup ADD COLUMN pool_nano INTEGER NOT NULL DEFAULT 0;
ALTER TABLE cluster_day ADD COLUMN pool_nano INTEGER NOT NULL DEFAULT 0;
CREATE INDEX cl_date_cc ON cost_lines(date_utc, channel, cost_center);
"""

#: Per-bucket point columns of ``inferences`` and their ``exact_mask`` bits (a set bit = that
#: bucket's line is exact). Server tools (web search and web fetch) share one column and one bit.
BUCKET_COLUMNS: dict[str, str] = {
    "uncached_input": "nano_uncached",
    "cache_read": "nano_read",
    "cache_write_5m": "nano_w5m",
    "cache_write_1h": "nano_w1h",
    "cache_write_other": "nano_wother",
    "cache_write_unknown": "nano_wunknown",
    "output": "nano_output",
    "web_search": "nano_server_tools",
    "web_fetch": "nano_server_tools",
}
EXACT_MASK_BITS: dict[str, int] = {
    "nano_uncached": 1, "nano_read": 2, "nano_w5m": 4, "nano_w1h": 8, "nano_wother": 16,
    "nano_wunknown": 32, "nano_output": 64, "nano_server_tools": 128,
}

#: The tables holding ledger data (everything except ``meta``, ``audit`` and ``key_events``);
#: ``blocks`` is part of the contract DDL and stays empty in v0.2 (fingerprints travel inside
#: ``requests.fp_json``).
DATA_TABLES = ("sources", "cursors", "sessions", "lanes", "requests", "attempts", "inferences",
               "blocks", "events", "aggregates", "cost_lines", "outcomes", "message_index",
               "request_index", "daily_rollup", "cluster_day", "findings", "receipts",
               "merge_members")


def _run_script(conn: sqlite3.Connection, script: str) -> None:
    for stmt in script.split(";"):
        if stmt.strip():
            conn.execute(stmt)


def migrate_1_to_2(conn: sqlite3.Connection) -> None:
    """Copilot addendum §7.1: the GitHub columns, ``outcomes.extra_json``, ``pool_nano`` and the
    cost-center index (``ALTER TABLE``; existing rows get NULL / 0)."""
    _run_script(conn, _COPILOT_DDL)


#: ``MIGRATIONS[n]`` migrates a version-*n* store to version *n + 1*.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {1: migrate_1_to_2}


def apply_pragmas(conn: sqlite3.Connection, *, read_only: bool = False) -> None:
    """``journal_mode=WAL`` (writers), ``foreign_keys=ON``, ``synchronous=NORMAL`` (SPEC §7)."""
    if not read_only:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    # performance only (not persistent): a 64 MiB page cache keeps the random-key index inserts
    # of an ingest in memory; temporary sort trees stay in memory
    conn.execute(f"PRAGMA cache_size=-{CACHE_KIB}")
    conn.execute("PRAGMA temp_store=MEMORY")


def schema_version(conn: sqlite3.Connection) -> int | None:
    """``meta.schema_version`` as an int, or None for a file without a ledger schema."""
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
    if row is None:
        return None
    got = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if got is None:
        return None
    try:
        return int(got[0])
    except ValueError:
        raise UsageError("the ledger's schema_version is not a number") from None


def create_schema(conn: sqlite3.Connection) -> None:
    """Create the version-1 schema (SPEC §7.1 + STORE extensions) and migrate it to
    :data:`SCHEMA_VERSION` in one transaction. The caller has checked the file holds no ledger."""
    conn.execute("BEGIN")
    try:
        _run_script(conn, SPEC_DDL)
        _run_script(conn, STORE_EXTENSIONS)
        conn.execute("INSERT INTO meta(key, value) VALUES ('schema_version', '1')")
        _migrate_locked(conn, 1)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _migrate_locked(conn: sqlite3.Connection, version: int) -> None:
    while version < SCHEMA_VERSION:
        MIGRATIONS[version](conn)
        version += 1
        conn.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(version),))


def migrate(conn: sqlite3.Connection) -> int:
    """Migrate the ledger forward to :data:`SCHEMA_VERSION` (forward-only; one transaction);
    returns the version found. A store newer than this build raises ``UsageError``."""
    version = schema_version(conn)
    if version is None:
        raise UsageError("not a Token Bill ledger (no schema_version)")
    if version > SCHEMA_VERSION:
        raise UsageError(f"ledger schema version {version} is newer than this build supports "
                         f"({SCHEMA_VERSION})")
    if version < 1:
        raise UsageError(f"unknown ledger schema version {version}")
    if version < SCHEMA_VERSION:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _migrate_locked(conn, version)
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    return version
