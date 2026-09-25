# CONTRACT-CHANGE-CP-DET-LANES

Builder: CP-DET-LANES (wave 2). Implemented against the current contract; nothing in `core/*` was
edited. Two items for the contract owner / orchestrator.

## 1. Addendum §10.0 / §10.3 text vs the brief (documentation only)

**What.** Addendum §10.0 says `copilot.lanes` declares `requires=frozenset()` with a module
`KIND_REQUIRES` capability map (`long-context-band` needs `usage_sequence`, `compaction-cost`
`events`, …) and names skipped kinds in one `copilot.<detector>/skipped-kinds` data-quality finding.
The binding brief says `requires={"credits"}` and per-kind **input** checks per lane.

**Implemented.** The brief (revision 3.1 precedence): `requires = frozenset({"credits"})`,
`extension = "copilot"`, `families = {"copilot"}`, per-lane input checks (`KIND_INPUTS`), no
skipped-kinds finding (a lane detector's output must not depend on which inputs a shard holds;
`core.catalog.COUNT_SOURCE` lists no `dq.skipped-kinds` for `copilot.lanes`).

**Proposed.** Align addendum §10.0's `copilot.lanes` sentence with the brief at the next
consistency pass.

## 2. `core.findings.product_family` for lanes without requests

**What.** `product_family(lane)` decides from the first request (billing class `pool` or channel
`github_copilot`); a lane with no requests is `default`. CP-LOCAL's collector emits sessions that a
VS Code extract already covers as **LaneEvents only** (brief CP-LOCAL step 5: compaction triggers,
static token counts, model switches — no money). Those lanes never reach `copilot.lanes`
(`run_detectors` filters by family; a request-less lane has no team either), so their compaction
triggers are not counted in the forced share of the covering VS Code conversation.

**Why it matters.** Small: counts only, no money. Recorded so the gap is known at gate 1.

**Proposed signature (additive, v0.3).** `product_family(lane)` also returns `"copilot"` for a lane
without requests whose events carry a Copilot-only attr (`COMPACTION.copilot_trigger`,
`SESSION_META.credit_limit_nano` / `routing_mode` / `context_tier`), and `Lane` gains no field;
the detector would then join such event-only lanes to the covering conversation by
`session_key` inside the cohort once the team is attributable (e.g. via a SESSION_META team attr).
