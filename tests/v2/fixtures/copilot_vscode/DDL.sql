-- VS Code Copilot Chat agent-traces.db schema, copied verbatim from the `db.exec` block of
-- microsoft/vscode extensions/copilot/src/platform/otel/node/sqlite/otelSqliteStore.ts
-- (branch main, fetched 2026-09-25, file sha256 efdd2627790f92a151eb48dee4c35b380a3087ccd6967b94937d4418f655f9a2;
-- SCHEMA_VERSION = 1 substituted for the `${SCHEMA_VERSION}` template). Provenance: primary.
-- VS Code opens the file with PRAGMA journal_mode = WAL, busy_timeout = 3000, foreign_keys = ON.

CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
INSERT OR REPLACE INTO schema_version (version) VALUES (1);

CREATE TABLE IF NOT EXISTS spans (
	span_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_span_id TEXT,
	name TEXT NOT NULL, start_time_ms INTEGER NOT NULL, end_time_ms INTEGER NOT NULL,
	status_code INTEGER NOT NULL DEFAULT 0, status_message TEXT,
	operation_name TEXT, provider_name TEXT, agent_name TEXT, conversation_id TEXT,
	request_model TEXT, response_model TEXT,
	input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER, reasoning_tokens INTEGER,
	tool_name TEXT, tool_call_id TEXT, tool_type TEXT,
	chat_session_id TEXT, turn_index INTEGER, ttft_ms REAL
);

CREATE TABLE IF NOT EXISTS span_attributes (
	span_id TEXT NOT NULL REFERENCES spans(span_id) ON DELETE CASCADE,
	key TEXT NOT NULL, value TEXT,
	PRIMARY KEY (span_id, key)
);

CREATE TABLE IF NOT EXISTS span_events (
	id INTEGER PRIMARY KEY AUTOINCREMENT,
	span_id TEXT NOT NULL REFERENCES spans(span_id) ON DELETE CASCADE,
	name TEXT NOT NULL, timestamp_ms INTEGER NOT NULL, attributes TEXT
);

CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_conversation ON spans(conversation_id);
CREATE INDEX IF NOT EXISTS idx_spans_chat_session ON spans(chat_session_id);
CREATE INDEX IF NOT EXISTS idx_spans_operation ON spans(operation_name);
CREATE INDEX IF NOT EXISTS idx_spans_start_time ON spans(start_time_ms);
CREATE INDEX IF NOT EXISTS idx_span_events_span ON span_events(span_id);

-- Session view: derives session boundaries from span data.
-- No separate sessions table needed — invoke_agent spans define session lifecycle.
CREATE VIEW IF NOT EXISTS sessions AS
SELECT
	COALESCE(conversation_id, chat_session_id) AS session_id,
	agent_name,
	response_model AS model,
	MIN(start_time_ms) AS started_at,
	MAX(end_time_ms) AS ended_at,
	MAX(end_time_ms) - MIN(start_time_ms) AS duration_ms,
	COUNT(*) AS span_count,
	SUM(CASE WHEN operation_name = 'chat' THEN 1 ELSE 0 END) AS llm_calls,
	SUM(CASE WHEN operation_name = 'execute_tool' THEN 1 ELSE 0 END) AS tool_calls,
	SUM(CASE WHEN operation_name = 'chat' THEN input_tokens ELSE 0 END) AS total_input_tokens,
	SUM(CASE WHEN operation_name = 'chat' THEN output_tokens ELSE 0 END) AS total_output_tokens,
	SUM(CASE WHEN operation_name = 'chat' THEN cached_tokens ELSE 0 END) AS total_cached_tokens
FROM spans
WHERE COALESCE(conversation_id, chat_session_id) IS NOT NULL
GROUP BY COALESCE(conversation_id, chat_session_id);
