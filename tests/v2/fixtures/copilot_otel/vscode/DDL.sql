-- VS Code Copilot Chat agent-traces.db schema (otelSqliteStore.ts), RECONSTRUCTED for CP-OTEL tests.
-- Provenance: the typed `spans` columns, `span_attributes(span_id, key, value)` and `span_events`
-- tables are transcribed from SPEC-v0.2-COPILOT.md §5.13 / §19.3 #23 (which cite
-- microsoft/vscode extensions/copilot/src/platform/otel/node/sqlite/otelSqliteStore.ts). The
-- orchestrator's verbatim copy (RULINGS G-2: scratchpad copilot/inputs/vscode-otel-DDL.sql) was not
-- available in this build environment, so column types, the `tool_*` names, `kind`,
-- `status_message` and the `span_events` layout are assumptions (VERIFY against the primary file).
CREATE TABLE IF NOT EXISTS spans (
  span_id TEXT PRIMARY KEY,
  trace_id TEXT NOT NULL,
  parent_span_id TEXT,
  name TEXT NOT NULL,
  kind INTEGER,
  start_time_ms INTEGER NOT NULL,
  end_time_ms INTEGER,
  status_code INTEGER,
  status_message TEXT,
  operation_name TEXT,
  provider_name TEXT,
  agent_name TEXT,
  conversation_id TEXT,
  request_model TEXT,
  response_model TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  cached_tokens INTEGER,
  reasoning_tokens INTEGER,
  tool_name TEXT,
  tool_call_id TEXT,
  tool_arguments TEXT,
  tool_result TEXT,
  chat_session_id TEXT,
  turn_index INTEGER,
  ttft_ms INTEGER
);
CREATE TABLE IF NOT EXISTS span_attributes (
  span_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value TEXT,
  PRIMARY KEY (span_id, key)
);
CREATE TABLE IF NOT EXISTS span_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  span_id TEXT NOT NULL,
  name TEXT NOT NULL,
  timestamp_ms INTEGER NOT NULL,
  attributes TEXT
);
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_start ON spans(start_time_ms);
CREATE INDEX IF NOT EXISTS idx_span_attributes_span ON span_attributes(span_id);
CREATE INDEX IF NOT EXISTS idx_span_events_span ON span_events(span_id);
