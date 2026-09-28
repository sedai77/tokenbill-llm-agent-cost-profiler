# CONTRACT-CHANGE-CP-OTEL — notes against the frozen core (wave 2, CP-OTEL)

CP-OTEL builds against the current core; nothing here blocks the package. Each item names what the
code does today and the proposed change for the contract owner.

1. **No lane-event kind for a session shutdown.** The brief maps span events
   `github.copilot.session.shutdown` to lane events, but `core.records.LaneEventKind` (pinned at 13
   kinds, C-8) has no shutdown kind and no SESSION_META attr that describes an end of session.
   *Today:* a shutdown span event becomes `SESSION_META` with no attrs (a session-end marker);
   compaction → `COMPACTION` (`trigger` auto/manual + `copilot_trigger`), truncation →
   `CONTEXT_EDIT` `edit_type="copilot_truncation"`. *Proposed (v0.3):* either a `SESSION_END`
   kind or a documented `SESSION_META` attr `ended: bool`.

2. **The VS Code attribute allowlist lacks the effort attribute.** `core.facts`
   `copilot.vscode_traces.attribute_allowlist` (addendum §5.13) does not contain
   `gen_ai.request.reasoning.level` (non-content; the brief maps it to `RequestParams.effort`) nor
   `gen_ai.agent.id`. *Today:* requests read from `agent-traces.db` carry `effort=None`; subagent
   lanes come from span ancestry and the typed `agent_name` column. OTel files keep both
   attributes. *Proposed:* after verifying the key names in `otelSqliteStore.ts`, append both keys to
   the allowlist (C-28 facts; CP-VSCODE's collector would select them too).

3. **`SourceRef.priority` of gh-aw is unspecified.** The addendum gives 21 (OTel files) and 22
   (VS Code DB) only. *Today:* `gh-aw-token-usage` uses 20 (a proxy log; it never shares request
   ids with the client sources). *Proposed:* record 20 in the addendum §5.14 text.

4. **TELEM's late change request A-4 is not on the wave base (16539c9).** `otlp` still maps
   Copilot GenAI chat spans itself and sets no `stats["defer:copilot-otel"]`
   (`docs/v0.2-design/gate1-fixups.md` #4). *Today:* the gate test
   `test_gate_mixed_otlp_through_otlp_and_wiring_deferral` passes in both states (every span once);
   `test_gate_telem_defers_copilot_resources` xfails until A-4 lands, then asserts the disjoint
   passes. A scratch simulation of A-4 through WIRING's loop gave `['otlp', 'copilot-otel']` with 2
   Claude + 3 Copilot requests and no duplicate.
