### CP-LOCAL — Copilot CLI events adapter, experimental session-store reader, conventions, collector (wave 2; non-blocking)

**Priority (owner answer 3).** The adopting developers use VS Code and IntelliJ, not the standalone CLI. The
org-data path (CP-BILL, CP-ORGDATA, CP-HANDOFF) is primary and VS Code per-request data comes from CP-OTEL /
CP-VSCODE, so this package is **not release-blocking**: it serves CI runners (`GITHUB_TOKEN` CLI, the
`--ci` post-step) and developers who use the CLI or the CLI agent inside VS Code. The store reader stays
experimental; release gate (2) no longer requires a real `session-store.db`.

**Goal.** Give Token Bill content-free Copilot data from developer machines and CI runners: always from
`session-state/*/events.jsonl` (schema published in GitHub's SDK), and — behind the experimental flag
`copilot-store` until a real fixture exists — per-request usage from `session-store.db`. Normalize into
canonical requests, compaction inferences, events and rollups on channel `github_copilot`, and collect them
incrementally for the fleet. Read SPEC §3.2, §3.11, §3.13, §3.15, §4, §5.1–§5.4, §5.12, §8; addendum DC2, DC6,
DC8, DC18, §3.1 (CA-2, CA-7), §4, §5.9, §5.11, §8, §19.3 #18–#20, §19.5 #12, #18, #19, #23, Appendix C.G11–G12.

**Owns.** `tokenbill/adapters/copilot_cli.py` (`CopilotCliAdapter`, name `copilot-cli`),
`tokenbill/adapters/copilot_collect.py` (`CopilotCollectorState`, `collect_incremental_copilot`),
`tokenbill/adapters/copilot_conventions.py` (registers `github_copilot.shutdown_rollup`,
`github_copilot.session_store`, `github_copilot.token_details` on import), `tests/v2/copilot_local/**`,
`tests/v2/fixtures/copilot_local/**`.

**Consumes.** `core.records`, `core.types` (`IngestOptions.experimental`), `core.conventions`, `core.ids`,
`core.jsonl`, `core.ids.copilot_session_key` / `copilot_lane_key` (the formulas CP-OTEL uses too, so a
conversation seen by both sources lands on the same session and lanes), `core.money.nano_aiu_to_nano`,
`core.models.normalize_copilot_model`, `core.lanes`,
`core.facts` (`utility_models`), `core.builders`, `core.testing`; stdlib `sqlite3` (read-only URI). Never
import CC's collector.

**Provides.** The registered adapter and conventions; `collect_incremental_copilot(home, state, opts, *,
now_ms, skip_session_ids: frozenset[str] = frozenset()) -> Iterator[IngestResult]` (CP-WIRE writes trace@2
through an injected writer); `session_ids(home) -> frozenset[str]` (directory names under
`session-state/`, no file opened) for CP-WIRE's VS Code dedupe.

**Build.**
1. File policy per addendum §5.9 (never open `config.json`, `mcp-*`, `providers.json`, `logs/`, `session.db`,
   `plan.md`, `checkpoints/`, `files/`, `apps.json`); `COPILOT_HOME` honored.
2. events.jsonl per §5.9: lenient reader with last-`{"type":` recovery; `session.start` / `resume` (incl.
   `contextTier` → SESSION_META and `PricingContext.context_tier`), `assistant.message` outputs,
   `session.compaction_complete` → COMPACTION event (`trigger` auto/manual + `copilot_trigger`, static token
   attrs) and an exact COMPACTION inference via `github_copilot.token_details`, model changes, auto-mode
   resolution, truncation → CONTEXT_EDIT, checkpoints → COST_STATE `copilot.cli.checkpoint` and
   `write_ttl_hint`, errors, tool completions (sizes only), shutdown rollups under
   `github_copilot.shutdown_rollup` with the resume-leg / compaction-reset / unclean-shutdown rules. Content
   fields never leave the parser.
3. Session store only when `"copilot-store" in opts.experimental`: column probe, required / optional columns,
   `github_copilot.session_store` convention, `sessions.summary` never selected, `cwd` / `repository` → `h_`,
   join to events (session, turn index, model, created_at ± 2 s), unjoined output → `OUTPUT_RESIDUAL`, locked
   DB → one retry then skip.
4. Pricing context per request: provider `github`, channel `github_copilot`, routing, speed, `context_tier`,
   compliance from `dict(opts.attribution.extra).get("copilot_compliance")`, billing path from attribution
   else `copilot_pool` (dq); utility / BYOK rules (§5.11); fidelity `NO_TTL_SPLIT`; priority 39; lanes
   MAIN / SUBAGENT / COMPACTION; `agent_product="copilot_cli"`; `cache_scope_key="unknown"`.
5. Collector: sessions whose raw id is in `skip_session_ids` (conversations CP-VSCODE's extract already
   covers with per-request VS Code spans) emit **no requests, inferences or rollup aggregates**, only their
   LaneEvents (compaction triggers, static token counts, model switches — no money), counted as
   `dq.copilot_session_covered_by_vscode`; byte cursors per `events.jsonl` with head-sha rotation; high-water
   mark on `assistant_usage_events.id` when the store flag is on; in-flight sessions re-read; state file
   private; content tier fixed to `none`.

**Facts to verify.** Session-store schema at a released CLI; `initiator` values; `host_type` / `producer`
values; whether `outputTokens` repeats per chunk. Fixture provenance: events from the SDK schema (`primary`);
store DDL from tokscale / codeburn (`third-party`, experimental only).

**Acceptance tests.**
- Events-only fixture (two sessions, canary in every content field): exact COMPACTION inference whose
  tokenDetails sum-check equals `totalNanoAiu`; subagent lane; model switch; truncation; checkpoint
  COST_STATE and `write_ttl_hint="5m"` after a 300 s TTL; context tier on requests; rollup aggregates; no
  `usage_sequence` capability; canary absent from `repr` / `to_json`.
- With the flag: store rows → requests with inclusive input decomposed; negative uncached → quarantine; store
  without `total_nano_aiu` / `output_tokens` → dq and residual outputs; locked DB → retry then skip. Without
  the flag the store file is never opened (patched `sqlite3.connect` asserts).
- Appendix C.G11 through `github_copilot.token_details` → 232,848,000 nano provider estimate; C.G12 utility
  call billable False.
- Concatenated line recovered; 3-leg resumed session differenced; missing shutdown dq.
- Collector: a session id in `skip_session_ids` yields LaneEvents but no request, inference or aggregate, and
  the dq count; session and lane keys equal `core.ids.copilot_session_key` / `copilot_lane_key`; second run
  emits only new data; rotated file re-read; state file 0600 (POSIX); Copilot files'
  mtimes unchanged.
- `assert_adapter_conforms`; hypothesis fuzz of the events parser and the row mapper.

**Size.** ~2.85k LOC including tests.


---
**ORCHESTRATOR ADDITIONS (binding):** SPEC Appendix E.7 R-E43 — set `billing_path` to `copilot_pool` or `copilot_direct` on every Copilot inference you emit.
