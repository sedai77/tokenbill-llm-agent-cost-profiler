### DETECT-CACHE — Cache detectors (wave 2)

**Goal.** The eight content-free cache detector classes that name each cache-waste mechanism, its billed
cost, its recoverable dollars (through the replayer with the linked lever) and a deployable fix (SPEC §10.1,
the `cache.*` rows of §10.2, D11, D14, D21, D26, D28).

**Owns.** `tokenbill/detect/{cache_miss,cache_ttl,cache_structure}.py`, `tests/v2/detect_cache/**`.

**Consumes.** `core.transitions`, `core.cache_rules.effort_change_keeps_cache`, `core.findings` (helpers),
`core.policy` (`parse_policy`), `core.catalog` (`levers_for_kind`, `ALLOWLIST`), `core.types` (`Finding`,
`Fix`, `Scope`, `EvidenceItem`, `AnalysisContext`, `Policy`), `core.labels`, `core.money`, `core.evidence`,
`core.protocols` (`Detector`), `core.registry.run_detectors`, `core.builders` (`lane_from_table`, builders),
`core.testing` (`FakePricer`, `FakeReplayer`, `assert_detector_conforms`).

**Provides.** Detector classes at the registry paths of SPEC §3.7: `detect.cache_miss.{MissByCause,
SwitchChurn, RebuildEvents}`, `detect.cache_ttl.{ColdResume, TtlAdvisor}`, `detect.cache_structure.{
GatewayDisabled, UnreadWrite, ColdFanout}`.

**Build.** Every `cache.*` row of SPEC §10.2: trigger, `cost_observed` (EXACT only for billed arithmetic),
`recoverable` (`ctx.replayer.replay` with the linked policy on the cohort's lanes, or the stated formula; None
where no mechanical repair), kinds (incl. `plan-toggle`, `edit-churn` with the K* evidence, TTL advisor that
recommends only a policy different from the observed TTL and reports the keepalive break-even), fix text and
config patch (keys only from `core.catalog.ALLOWLIST`), lever ids from `core.catalog.levers_for_kind`,
references, `needs_eval`/`upper_bound`, audience rules, allowance-cohort labeling (basis LIST_EQUIVALENT,
"Allowance headroom:" titles), `min_usd` and other thresholds from `ctx.thresholds`, deterministic ids
(`core.findings.finding_id`) and ordering. Cross-lane logic stays inside `core.findings.cohort_key` cohorts
(shard invariance). Findings are returned unpublished.

**Acceptance tests.**
- one hand-computed fixture per kind (to the nano); cause precedence (compaction + idle gap → compaction;
  switch + gap → model-switch); Appendix A.9 thresholds; Appendix A.5 cold resume (exactly one event; $4.00 EXACT
  observed; $3.90 ESTIMATED premium); Appendix A.10 edit churn ($0.31 EXACT, $0.2576 ESTIMATED, K* 37.2) with
  `{"min_usd": "0.10"}`; an effort change on a Claude Code Opus 5.5 lane is not a miss cause, on an SDK lane it
  is (`effort-change` kind);
- TTL advisor with `FakeReplayer` recommends per the replayed values, emits `ttl-heterogeneous` with counts
  only when < 60% of ≥ k principals benefit, never proposes keepalive for Claude Code lanes, proposes it for an
  SDK lane with 7-minute gaps, adds the gateway note when the lane is behind a gateway, labels an allowance
  cohort's saving LIST_EQUIVALENT;
- gateway no-cache (a), beta-header-dropped (b) with a `policy.ttl.<team>` threshold, tool-search-disabled (c)
  via `attribution.extra["gateway"]`; write-never-read; oversized-ttl EXACT arithmetic; cold fan-out (upper
  bound); switch-churn sub-kinds incl. plan-toggle; compaction-cold;
- unmet `requires` → exactly one `missing-capabilities` finding via `run_detectors`; healthy-control lanes →
  no finding with recoverable ≥ `min_usd`; `assert_detector_conforms` for all 8 classes (incl. shard
  invariance); CANARY absent;
- gate (`importorskip("tokenbill.sim.usage_replay")`): with the real `UsageReplayer`, the Appendix A.1 lane
  yields `ttl-1h-recommended` with saving $1.1508; the A.2b lane with `{"min_usd": "0.10"}` yields
  `ttl-5m-recommended` with $0.318; the A.2 lane (already 5m) yields no TTL finding;
- gate (`importorskip` `tokenbill.synth.fleet` and the replayer): on `synth.fleet.generate()` the payments,
  platform, mobile and agents (keepalive, edit-churn) plants are recovered within their `FleetTruth`
  tolerances and the core team produces no cache finding ≥ `min_usd`.

**Size.** ~2.8k LOC including tests.
