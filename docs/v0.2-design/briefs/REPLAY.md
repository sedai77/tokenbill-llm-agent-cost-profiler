### REPLAY — Usage-level replay engine and the model gate (wave 2)

**Goal.** The counterfactual engine for every source (content-free) with minimal-change semantics, the
two-way TTL flips, keepalive (streaming-safe rules), compaction window, cold resume, rate transforms incl.
selector-scoped effort, repairs, and the streaming two-pass predictive calibration gate that alone can mark
projections CALIBRATED (SPEC §9.1–§9.6, D7–D9, D26, D28, D30).

**Owns.** `tokenbill/sim/{usage_replay,calibrate}.py`, `tests/v2/sim/**`.

**Consumes.** `core.transitions` (`classify_transitions`, `static_prefix_floor`, `lane_first_reads_of`),
`core.cache_rules`, `core.policy` (`parse_policy`, `lane_matches`, `request_matches`), `core.catalog`
(`successor`), `core.shards.merge_replay`, `core.types` (`Policy`, `ReplayResult`, `ReplayRequestOutcome`,
`CalibrationPartial`, `CalibrationReport`, `Transition`, `UnitRates`), `core.labels`, `core.money`,
`core.evidence`, `core.protocols` (`Replayer`, `Pricer`), `core.builders` (`FlatRates`, `lane_from_table`),
`core.testing` (`FakePricer`, `assert_replayer_conforms`).

**Provides.** `sim.usage_replay.UsageReplayer` (`Replayer`); `sim.calibrate.calibrate(lane_batches, *, pricer,
rules, granularity="day", folds=5, seed=0, static_prefix_floor=None) -> CalibrationReport`,
`calibrate_lanes(lanes, **kw)`, `calibrate_pass1`, `calibrate_pass2`, `finish_calibration`.

**Build.** Exactly §9.2–§9.4 with the fixed application order (rate transforms → context transforms →
cache-state transforms → TTL re-rating/min-prefix/batch → pricing); `baseline`/`cost`/`saving` Figures
per §3.5 (points and bounds; savings per request then summed); passthrough inferences priced unchanged unless a
rate transform applies; ambiguity ranges; keepalive only for non-Claude-Code lanes and skipped only for
structured outputs / forced tool_choice / `thinking=enabled:*` / batch (streaming lanes are allowed);
`effort` clauses scoped by selector; mixed billing classes → `UsageError`; integer unit rates on the hot path
with the `price_usage` fallback for range lines; calibrated mode using the report's ρ. Model gate exactly §9.6:
two streaming passes over `lane_batches()`, mergeable partials, one-step-ahead predictions that never read `R_i`,
FEMP thresholds, ≥ 12 periods, ρ per gap band with Wilson CIs (pooled below 30 trials), day-level 5-fold
cross-validation, diagnostics confusion matrix with the canonical-reason classes (Anthropic and OpenAI),
no-comparison labels and TTL corroboration counted separately.

**Acceptance tests.**
- Appendix A.1–A.6 and A.2b exactly to the nano with `FakePricer` (5m→1h saves $1.1508; bursty lane → 1h
  costs +$0.318; A.2b 1h→5m saves $0.318; 1h→5m on A.1 costs +$1.1508; keepalive $0.6924, 4 pings for 20
  minutes, 15 capped pings and cold for 2 hours, Claude Code lane skipped; a streaming SDK lane is **not**
  skipped; a structured-output lane is; compaction window $1.999 and "no change" above the lane maximum);
- identity: `replay(Policy.observed())` has `cost == baseline` (points and bounds) and `saving == 0` on 200
  random lanes including placeholder and unknown-TTL inferences (`assert_replayer_conforms`); minimal change:
  requests a policy does not affect have `changed=False`;
- ambiguity: a 305 s gap under `ttl=5m` yields a range spanning hit and miss;
- model remap applies the tokenizer band only across families; same-tier upgrade has no band; effort scaling
  with a selector touches only matched lanes; `fast=off` flips fast-toggle misses; batch hit band low/high;
  each repair (restore_caching, stagger_fanout, retry_backoff_cap, fallback_credit, shared_ci_prefix) against a
  hand-computed fixture; allowance and billed lanes in one call → `UsageError`;
- sharding: replaying two lane halves and merging with `core.shards.merge_replay` equals one replay;
- calibrated mode multiplies flips by ρ; without a passing report the result is UNCALIBRATED;
- calibrate: lanes generated from the documented rules → NMBE 0, CV(RMSE) 0, `pass`; the same lanes with 10% of
  predicted hits flipped to misses (seeded) → ρ ≈ 0.90 ± 0.03 and the calibrated variant passes; 11 periods →
  `insufficient_data`; 128 idle > 1 h transitions labeled `previous_message_not_found` → TTL corroboration
  128/128 and not counted in precision/recall; `model_changed` label vs predicted model switch counted
  correctly; an OpenAI `param_changed` label vs a predicted effort change counted in the param class;
  `unavailable` counted as no-comparison; the report is identical for one batch vs five batches;
- perf (marker `perf`): 10⁶ requests × 1 policy ≤ 30 s, O(n) scaling check (2× requests ≤ 2.3× time);
  calibrate over 10⁶ requests ≤ 120 s.

**Size.** ~2.8k LOC including tests.
