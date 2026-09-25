### CP-OTEL — Copilot span mapping: VS Code agent-traces.db and OTel files, JetBrains (experimental), gh-aw token usage (wave 2)

**Goal.** Own the single implementation that turns Copilot client spans into canonical requests, in the
priority order of the adopting organization's editors (owner answer 3): **VS Code first** — its opt-in
`agent-traces.db` (raw, for the self-view, and the content-free extracts written by CP-VSCODE's collector) and
its OTel JSON-lines outfile — then managed OTel through an org collector (OTLP/JSON; CLI, VS Code and, behind
an experimental flag, JetBrains), plus GitHub Agentic Workflows' `token-usage.jsonl`. No double counting (native
over synthesized spans; disjoint resource claims with TELEM's `otlp`), no content read, and mixed OTLP files
keep their Claude spans. Read SPEC §3.2, §5.1, §5.2, §5.9, D23, §8; addendum DC4, DC8, DC21, §3.1 (CA-7),
§3.3 (CA-20), §4, §5.10, §5.11, §5.13, §5.14, §8, §16, §19.3 #18, #21–#23, #27, §19.5 #13, #26;
`CORE-AMENDMENTS.md` items C-8, C-12, C-22, C-23, C-28.

**Owns.** `tokenbill/adapters/copilot_otel.py` (`CopilotOtelAdapter`, `copilot-otel`; the shared chat-span
mapping `map_chat_spans`; registers convention `github_copilot.otel` on import),
`tokenbill/adapters/copilot_vscode.py` (`VsCodeAgentTracesAdapter`, `copilot-vscode-traces`),
`tokenbill/adapters/gh_aw.py` (`GhAwTokenUsageAdapter`, `gh-aw-token-usage`, registers `gh_aw.token_usage`),
`tests/v2/copilot_otel/**`, `tests/v2/fixtures/copilot_otel/**`.

**Consumes (core only).** `core.records`, `core.types` (`IngestOptions.otel_service_names`, `.experimental`),
`core.conventions`, `core.jsonl` (`iter_lines`, `parse_json_line(exact_numbers=True)`), `core.ids`
(`copilot_session_key`, `copilot_lane_key`, `natural_id`), `core.money.nano_aiu_to_nano`, `core.models`
(`normalize_copilot_model`, `is_copilot_resource`), `core.lanes`, `core.facts` (`copilot_vscode_traces()`
allowlist, `utility_models`), `core.builders`, `core.testing`; stdlib `sqlite3` (read-only URI). Never imports
CP-VSCODE, CP-LOCAL or TELEM.

**Provides.** Three registered adapters and two conventions; `map_chat_spans(spans: Iterable[SpanView], opts,
*, source: SourceInfo, dialect: str) ->
IngestResult` (module-internal API used by the three span readers of this
package; `SpanView` = name, ids, times, attributes mapping, resource attributes, events).

**Build.**
1. **Shared mapping** (addendum §5.10 "Mapping"): `chat` spans → one request each (response model else request
   model through `normalize_copilot_model`; request model `auto` → routing auto; `gen_ai.response.id` →
   `provider_message_id`; session key `core.ids.copilot_session_key(gen_ai.conversation.id)`, lane key
   `core.ids.copilot_lane_key(session_key, lane_kind, agent_id)` — the same formulas CP-LOCAL uses, so lanes
   of one conversation merge in STORE); tokens under `github_copilot.otel` (input inclusive of cache read and
   creation; creation → `cache_write_unknown`); `copilot_chat.copilot_usage_nano_aiu` /
   `github.copilot.nano_aiu` → provider estimate (`nano_aiu_to_nano`); `github.copilot.cost` ignored;
   `gen_ai.request.reasoning.level` → effort; `copilot_chat.request.max_prompt_tokens` and the session tier →
   `PricingContext.context_tier` when stated; initiator / agent attrs → lane kind; `invoke_agent` roots → one
   COST_STATE `copilot.otel.invoke_agent` per conversation; span events `github.copilot.session.compaction_*`,
   `…truncation`, `…shutdown` → LaneEvents. **Dedupe:** inside one trace native `github-copilot` spans win over
   synthesized `copilot-chat` spans (`dq.copilot_synthesized_span_skipped`); requests keyed by
   `gen_ai.response.id`, else (conversation id, turn index, span id). Content attributes counted, never parsed
   (`dq.raw_bodies_ignored`); identity attributes → team map → `p_` → dropped; repository attributes → `h_`.
   Utility / BYOK rules of §5.11. Fidelity `NO_TTL_SPLIT`; priority 21 (OTel files) / 22 (agent-traces.db).
2. **Collector metadata.** A resource (JSON-lines) or `tokenbill_meta` table (SQLite) carrying
   `tokenbill.collector = "copilot-vscode-collect@1"` / `schema = "tokenbill/vscode-extract@1"` is a CP-VSCODE
   extract: `tokenbill.principal` (must match `^(r_[A-Za-z0-9._-]{1,64}|c_[0-9a-f]{20})$`, else dropped with
   `dq.copilot_collector_principal_invalid`) → `Attribution.principal`; `principal_key_id` →
   `SourceInfo.principal_key_id`; `team` → `Attribution.team`. Without the marker these keys are ignored.
3. **`copilot-vscode-traces`** (addendum §5.13; primary): read-only; `spans` columns and `span_attributes` rows
   selected **only** with `key IN (<allowlist from core.facts>)`; `span_events` never read; agent product
   `copilot_vscode`. Sniff: SQLite magic and `span_attributes` in the head. Capabilities `{usage_sequence,
   timing, params, credits}`. Reads raw VS Code databases (self-view `copilot me`) and CP-VSCODE extracts
   identically.
4. **`copilot-otel`** (addendum §5.10): Copilot resources by `core.models.is_copilot_resource` with
   `opts.otel_service_names`; `sniff` true only when every resource in the head is Copilot; mixed files read
   only Copilot resources (foreign counted in `stats["foreign_resources"]`). Dialects in priority order: (a) VS
   Code OTel-JS JSON-lines dumps (`readableSpanToJson`; primary); (b) OTLP/JSON collector files (int64 as
   strings); (c) CLI envelope variants only with `"copilot-cli-otel-file" in opts.experimental`, else
   quarantined with reason `experimental:copilot-cli-otel-file`; (d) **JetBrains** resources only with
   `"copilot-jetbrains-otel" in opts.experimental` — recognized only through a service name given by
   `--otel-service-name` (JetBrains `service.name` and attribute names are undocumented, and GitHub's docs
   contradict each other on managed telemetry for JetBrains: the table says supported, the prose says CLI and
   VS Code only), mapped with `agent_product="copilot_jetbrains"`; without the flag such resources are counted
   (`stats["jetbrains_resources_skipped"]`) and skipped with `dq.copilot_jetbrains_otel_experimental`.
   `agent_product` otherwise: `github-copilot` → `copilot_cli`, `copilot-chat` → `copilot_vscode`, other
   configured names → `copilot_other`. Capabilities `{usage_sequence, timing, params, credits, events}`.
5. **`gh-aw-token-usage`** (addendum §5.14, unchanged): only `provider == "copilot"` lines; one request per
   line; `gh_aw.token_usage` convention; `ai_credits_this_response` → provider estimate; last
   `ai_credits_total` →
   COST_STATE `gh_aw.run_total`; one `UsageAggregate(gh_aw.run)` per file (dims `channel`, `repo`,
   `workflow`, `source`); workload ci; billing path `copilot_direct` default (dq); repo / workflow `h_`.
   Capabilities `{credits, timing, aggregates}` (every Copilot lane source declares `credits`, the capability
   CP-DET-LANES requires).

**Facts to verify.** CLI OTel file envelope and input inclusivity; JetBrains service name and attribute names
(the flag stays until a real JetBrains file is a `real-redacted` fixture); Copilot app service name; gh-aw
artifact name. Fixture provenance: VS Code dumps and the `agent-traces.db` DDL (`primary`, VS Code source);
GitHub's gh-aw fixture copied verbatim (`primary`); CLI envelope (`third-party` / docs-derived,
experimental); JetBrains (synthetic, experimental).

**Acceptance tests.**
- VS Code dump fixture: two chat spans → two requests (inclusive input decomposed, creation → unknown-TTL
  write), nano-AIU provider estimate, canary in `gen_ai.input.messages` and span names absent.
- `agent-traces.db` built from the DDL with canary content rows: requests built; a SQL trace hook proves no
  non-allowlisted key was selected and `span_events` was never queried. A CP-VSCODE-shaped extract (built in
  the test from the documented `tokenbill_meta` schema) yields the same requests with `Attribution.principal
  == "c_…"` and `SourceInfo.principal_key_id` from the meta table; an invalid meta principal is dropped with
  the dq code.
- CLI file fixture (flag on): `invoke_agent` + 3 chat spans → 3 requests and one COST_STATE equal to Σ chat
  nano-AIU; `auto` request model → routing auto. Flag off → quarantined with the experimental reason.
- JetBrains fixture (synthetic; service name `copilot-intellij` passed via `otel_service_names`): flag off →
  zero requests and the dq code; flag on → requests with `agent_product="copilot_jetbrains"`.
- Mixed OTLP/JSON file (Claude Code + Copilot resources): `sniff` false; this adapter yields only the Copilot
  requests; a custom `service.name` and a resource recognized only by `github.copilot.` attribute keys are both
  Copilot.
- Native + synthesized spans of one call → one request; session and lane keys equal
  `core.ids.copilot_session_key` / `copilot_lane_key` of the conversation (the same keys CP-LOCAL computes).
- gh-aw fixture (5 lines): 5 requests with provider estimates equal to `ai_credits_this_response` (exact
  decimals), one COST_STATE and one `gh_aw.run` aggregate; a non-copilot line skipped with a count; the
  fixture's `gpt-4o-mini-2024-07-18` stays unpriced while gh-aw's AIC stays a tool estimate (R12).
- `assert_adapter_conforms` on the three adapters; hypothesis fuzz of the three readers.
- Gate (`importorskip` TELEM, WIRING): the mixed OTLP file through `otlp` + deferral re-read → both span sets,
  none duplicated.

**Size.** ~2.8k LOC including tests.


---
**ORCHESTRATOR ADDITIONS (binding):** SPEC Appendix E.7 R-E43 — set `billing_path` to `copilot_pool` or `copilot_direct` on every Copilot inference you emit.
