# Gate-1 fixups (orchestrator list; applied after all wave-2 packages merge)
1. CC: `tokenbill/adapters/claude_code.py` `_has_lone_surrogate` is recursive → port the iterative version from `core.jsonl` (F-CORE-C review D7).
2. CC: `isCompactSummary` user entries are not user-text appended items (R-E33).
3. SYNTH-ORACLE: adopt REPLAY's cohort-confined cross-lane repair groups (team, lane_kind) (R-E24, REPLAY-1 item 5).
4. TELEM: late change request A-4 (skip Copilot OTel resources via `core.models.is_copilot_resource`, set `stats['defer:copilot-otel']`) (R-E23).
5. REPLAY: late change request A-6 (billing class `pool` treated like `allowance`; mixed classes raise) (R-E23).
6. DETECT-OTHER + SYNTH-FLEET: `tail.runaway` leave-one-out p99 with a 20-session minimum cohort; align SYNTH-FLEET truth (R-E41).
