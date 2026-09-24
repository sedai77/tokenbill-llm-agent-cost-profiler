# CONTRACT-CHANGE-KIT-1 — lever selectors cannot express "A or B"

**What.** SPEC §11.1 gives several levers a selector that is a union of lane sets:
`cc.prompt_cache_ttl.subagent` and `cc.subagent_model` ("subagent (+ workflow_agent)"),
`sdk.keepalive` ("agent_sdk (and api)"), `blocks.breakpoints` / `sdk.defer_loading` ("lanes with
fingerprints"), `model.same_tier_upgrade` ("per model with a successor"). `LeverDef.selector` is one
string of the §9.5 selector grammar, whose terms are AND-ed; there is no OR, and `Policy.keepalive` is
a single `(selector, κ, M)` triple, so one grid value cannot target `agent_sdk` and `api` lanes.

**Current implementation (against the current contract).** Grids repeat repeatable clauses per lane
kind (`ttl=…@…lane_kind:subagent;ttl=…@…lane_kind:workflow_agent`, same for `model=`); `selector`
holds the first alternative (`…lane_kind:subagent`); `sdk.keepalive` targets `agent_product:agent_sdk`
only; fingerprint-only and per-model levers use `all`. PLAN's interaction groups (§11.2 #4) therefore
under-approximate the lanes touched by the two subagent levers (workflow-agent lanes are missed) and
over-approximate the `all` levers (safe: groups only get larger).

**Proposal (additive).** Add `LeverDef.selectors: tuple[str, ...] = ()` — the union of selectors the
lever touches (empty = `(selector,)`) — and let PLAN use `any(lane_matches(s, lane) for s in
lv.selectors or (lv.selector,))`; or allow `|` between selector alternatives in the §9.5 grammar.
Make `Policy.keepalive` a tuple of `(selector, κ, M)` like `ttl`, or allow a union selector there, so
`sdk.keepalive` can cover `api` lanes. Affected: F-KIT (catalog data), F-SEM (grammar, if `|`), PLAN.
