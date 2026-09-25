### CP-VSCODE — VS Code Copilot collector: agent-traces.db extract and OTel outfile tail (wave 2)

**Goal.** The developers use VS Code and IntelliJ (owner answer 3). VS Code's opt-in `agent-traces.db`
carries, per chat span, input / output / cache-read / **cache-creation** tokens and
`copilot_chat.copilot_usage_nano_aiu` (fact-checked in `otelSqliteStore.ts` and `chatMLFetcher.ts`), so it is
the best per-request Copilot source on developer machines — better than the CLI's events-only data. Give
fleets and volunteers a collector, `tokenbill copilot collect --source vscode`, that reads it (and the VS Code
OTel JSON-lines outfile when configured) incrementally, content-free, retention-aware, and writes
content-free extracts that the `copilot-vscode-traces` / `copilot-otel` adapters (CP-OTEL) read at central
ingest. This package never maps spans to requests (that is CP-OTEL's single implementation); it selects,
filters and copies allowlisted rows. JetBrains has no local per-request store (addendum §1.3 stands); its
coverage comes from org data (CP-ORGDATA) and experimental OTel (CP-OTEL). Read SPEC §4, §5.4 (collector
identity modes), §5.12, §8.1–§8.3; addendum DC8, DC18, §5.10, §5.13, §8.1, §19.3 #22–#23; `CORE-AMENDMENTS.md`
items C-12, C-22, C-28.

**Owns.** `tokenbill/adapters/copilot_vscode_collect.py` (`VsCodeCollectorState`,
`collect_vscode_extracts`, `vscode_paths`, `VSCODE_META_TABLE_DDL`), `tests/v2/copilot_vscode/**`,
`tests/v2/fixtures/copilot_vscode/**`.

**Consumes (core only).** `core.facts` (`copilot_vscode_traces()`: the `spans` column list, the
`span_attributes` key allowlist of addendum §5.13, content-attribute key list, retention 7 days / 100
sessions, per-OS path templates marked VERIFY), `core.ids` (`copilot_session_key`, `hmac_hex`, `key_id`,
`stable_id`), `core.jsonl` (`open_private`, `iter_lines`, `head_sha`, `parse_json_line`), `core.keys`,
`core.types` (`IngestOptions` identity fields, `DataQualityNote`), `core.secrets.find_secrets`,
`core.builders` (`CANARY`, `CANARY_LOGIN`); stdlib `sqlite3` (read-only URI `mode=ro&immutable=0`), `os`,
`platform`. Never imports CP-OTEL, CP-LOCAL or CC's collector.

**Provides.**
- `vscode_paths(*, platform: str | None = None, env: Mapping[str, str] | None = None, home: Path | None =
  None) -> list[Path]` — candidate `agent-traces.db` paths in order: `<user data>/User/globalStorage/
  <copilot chat extension id>/agent-traces.db` for `Code` then `Code - Insiders` (macOS `~/Library/Application
  Support/<product>`, Linux `$XDG_CONFIG_HOME` or `~/.config/<product>`, Windows `%APPDATA%\<product>`), then
  `<tmpdir>/copilot-agent-traces.db`. The extension id and product folder names come from facts (**VERIFY**;
  `--vscode-traces PATH` always overrides).
- `collect_vscode_extracts(sources: VsCodeSources, state: VsCodeCollectorState, *, out_dir: Path, identity:
  CollectorIdentity, now_ms: int, cli_session_ids: frozenset[str] = frozenset()) -> CollectResult` where
  `VsCodeSources(traces_dbs: tuple[Path, ...], otel_outfiles: tuple[Path, ...])`,
  `CollectorIdentity(principal: str | None, principal_key_id: str | None, team: str | None, name_key: bytes,
  name_key_id: str)` (built by CP-WIRE from the same identity modes as the CC collector: `install`,
  `two-stage` `c_`, MDM `r_`), and `CollectResult(files: tuple[Path, ...], covered_session_ids:
  frozenset[str], notes: tuple[DataQualityNote, ...], stats: Mapping[str, int])`.
- `VsCodeCollectorState.load(path) / .save(path)` (private JSON: per database `(start_time_ms, span_id)`
  high-water mark and the database's `head_sha`; per outfile byte cursor and `head_sha`; last run time).

**Build.**
1. **agent-traces.db extract.** Open read-only; probe `PRAGMA table_info(spans)` and
   `table_info(span_attributes)` (missing required columns → skip the database with `dq.copilot_vscode_schema`).
   Select new spans ordered by `(start_time_ms, span_id)` strictly after the high-water mark, only the
   allowlisted `spans` columns (never `tool_*`, never `span_events`), and their `span_attributes` rows **only**
   with `key IN (<allowlist>)` as a parameterized list. Write them into a new private SQLite file
   `vscode-<collector id>-<start>-<end>.db` with **the same DDL** as `otelSqliteStore.ts` for `spans` and
   `span_attributes` (content-free subset) plus `tokenbill_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)`
   holding exactly `schema = "tokenbill/vscode-extract@1"`, `collector_version`, `principal` (an `r_` / `c_`
   value or absent), `principal_key_id`, `team`, `name_key_id`, `source_db = h_(path)`, `window_start_ms`,
   `window_end_ms`, `spans`, `dropped_synthesized`. Span names are kept only when they equal an operation
   name in the allowlist (`chat`, `invoke_agent`, `execute_tool`); others become `"other"`.
2. **Retention awareness.** VS Code keeps 7 days / 100 sessions. When the oldest span still in the database is
   newer than the high-water mark (spans were pruned before we read them) → `dq.copilot_vscode_gap` (warn) with
   the gap in hours; when unread spans are more than 5 days old → `dq.copilot_vscode_retention_risk` (warn):
   the collector must run at least daily (README and `CollectResult.notes`). A database whose `head_sha`
   changed below the mark (recreated) restarts from its first span.
3. **OTel outfile tail** (`github.copilot.chat.otel.outfile`; path from `--vscode-otel-outfile`; the collector
   never reads VS Code `settings.json`): byte cursors with head-sha rotation like `core.jsonl.iter_lines`; each
   line parsed; content attributes (`gen_ai.input.messages`, `gen_ai.output.messages`,
   `gen_ai.system_instructions`, `gen_ai.tool.definitions`, tool arguments / results,
   `copilot_chat.hook_input|hook_output`) and identity attributes (`enduser.*`, `user.*`, `process.user.*`,
   `host.name`) removed; attributes outside the §5.13 allowlist removed; resource gains
   `tokenbill.collector = "copilot-vscode-collect@1"`, `tokenbill.principal`, `tokenbill.principal_key_id`,
   `tokenbill.team`. Output: `vscode-otel-<collector id>-<n>.jsonl` (canonical, 0600), still the VS Code
   OTel-JS dialect so `copilot-otel` reads it (primary).
4. **Dedupe against the in-VS Code CLI agent (owner answer 3).** `cli_session_ids` are the directory names
   under `~/.copilot/session-state/` (CP-WIRE lists them; no Copilot file is opened). For a conversation id in
   that set the extract keeps VS Code's spans (native `github-copilot` spans, else synthesized `copilot-chat`
   spans, which have no response id but carry input, cache-read, output and nano-AIU) and reports the id in
   `covered_session_ids`; CP-WIRE then passes that set to CP-LOCAL, which emits no requests or inferences for
   those sessions. Inside one trace, synthesized spans are dropped when native spans of the same conversation
   exist (`dropped_synthesized` counted; CP-OTEL applies the same rule again at read time).
5. **Privacy.** Content tier `none` only (`--content fingerprint|full` → `UsageError`, checked by CP-WIRE);
   before each file is renamed into `out_dir`, `core.secrets.find_secrets` and a canary scan over its bytes
   (SQLite pages / JSON text) → any hit aborts that file (`dq.copilot_vscode_extract_aborted`); `source_db`
   and paths are `h_`; the source database and outfile are never written (mtime unchanged).
6. **Developer opt-in text** (returned by `optin_snippet() -> str`, used by CP-POLICY's pack and `copilot me`):
   the user-level setting `"github.copilot.chat.otel.dbSpanExporter.enabled": true` (user settings only, not
   enforceable by managed settings), with the retention note and the privacy statement.

**Facts to verify.** Copilot Chat extension id and globalStorage folder after the move into
`microsoft/vscode` `extensions/copilot`; Insiders folder names; whether `span_attributes` rows of synthesized
spans carry `gen_ai.response.id`; whether CLI `requestId` equals any span attribute (if yes, STORE's merge
joins them and the skip set becomes a fallback). Each has a README fallback.

**Acceptance tests.**
- Fixture databases built by a script from the `otelSqliteStore.ts` DDL (primary) with canary text in content
  `span_attributes` rows, span names and `span_events`: the extract contains only allowlisted keys (SQL trace
  hook proves `span_events` was never queried and every `span_attributes` query used the `IN` list); canary
  and `CANARY_LOGIN` absent from the extract bytes; `tokenbill_meta` exactly as specified.
- Incremental: second run with no new spans writes no file; new spans after the mark are extracted once;
  ties on `start_time_ms` resolved by `span_id`; a pruned range → `dq.copilot_vscode_gap`; unread spans aged 6
  days → `dq.copilot_vscode_retention_risk`; a recreated database restarts; the source file's bytes and mtime
  are unchanged; state file 0600 (POSIX).
- Outfile: rotation (truncated file) re-read from 0; a line with content attributes → attributes removed,
  numbers kept; a partial last line is left for the next run.
- Dedupe: a conversation id in `cli_session_ids` with synthesized spans only → kept and reported in
  `covered_session_ids`; with native + synthesized spans → synthesized dropped and counted.
- `vscode_paths` table per platform (macOS, Linux with and without `XDG_CONFIG_HOME`, Windows, Insiders,
  tmpdir fallback) with injected env and home.
- Perf (`perf` marker): 10⁵ spans extracted ≤ 10 s.
- Gate (`@pytest.mark.gate`, `importorskip` CP-OTEL): every extract file is read by the real
  `copilot-vscode-traces` / `copilot-otel` adapters into requests whose tokens and nano-AIU equal the fixture
  spans; the `tokenbill_meta` principal (`c_`) reaches `Attribution.principal`; no request is lost or doubled
  across two incremental extracts.

**Size.** ~1.7k LOC including tests.


---
**ORCHESTRATOR ADDITIONS (binding):** SPEC Appendix E.7 R-E43 — set `billing_path` to `copilot_pool` or `copilot_direct` on every Copilot inference you emit.
