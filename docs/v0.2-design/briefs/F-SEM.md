### F-SEM — Foundation semantics (wave 1)

**Goal.** The shared semantics that several wave-2 packages must compute identically: Anthropic usage
normalization with the iterations/refusal rules, the cache-rule table with the conditional effort exemption,
the single transition/miss/cause definition, Shapley, the policy grammar and selectors, the shard contract,
and the detector helpers (SPEC §3.11, §3.14–§3.16, §3.19, §3.21, §3.22, D28–D30). Five packages depend on
`transitions.py`; implement it precisely.

**Owns.** `tokenbill/core/{conventions,cache_rules,transitions,shapley,policy,shards,findings}.py`,
`tests/v2/sem/**`.

**Consumes (F-CORE).** `core.records`, `core.types`, `core.labels`, `core.money`, `core.ids`, `core.lanes`,
`core.models`, `core.evidence`, `core.protocols`, `core.builders` (`FlatRates`, `make_*`, `lane_from_table`).
Do not import `core.testing` (F-KIT, same wave).

**Provides.** `core.conventions` (`Convention`, `register_convention`, `get_convention`, `normalize`,
`sum_check`, `anthropic_inferences`; registers `anthropic.messages` and a disabled `codex.rollout`);
`core.cache_rules` (`CacheRules`, `RulesTable`, `effort_change_keeps_cache`, constants); `core.transitions`
(`classify_transitions`, `static_prefix_floor`, `lane_first_reads_of`, constants); `core.shapley`
(`shapley_exact`, `shapley_mc`, `scale_credits`); `core.policy` (`to_spec`, `parse_policy`, `combine`,
`lane_matches`, `request_matches`, `selector_terms`); `core.shards` (`plan_shards`, `shard_where`,
`shard_of_lanes`, `merge_replay`, `merge_findings`, `stratified_sample`, `SHARD_MAX_REQUESTS`);
`core.findings` (`cohort_key`, `finding_id`, `make_scope`, `build_finding`, `miss_waste`, `min_usd_nano`,
`threshold`, `fit_cpt`, `top_evidence`, `sum_figures`, `rate_nano`).

**Build.** Exactly the SPEC sections above. Conventions: the §3.11 mapping, residual rules, iterations
invariant checks and the refusal table (0 → not billable; 1–16 → `billable=None`; > 16 → billable). Cache
rules: rows for anthropic_api, claude_platform_aws, foundry, bedrock, vertex (workspace/organization scope),
openai_api (organization), azure_openai (subscription), each with sources; `effort_change_keeps_cache` exactly
per D28 with numeric dotted version comparison. Transitions: precedence incl. `plan-toggle`, the effort
exemption, canonical diagnostic reasons (`param_changed`, `key_changed`, `compacted`), requests without a
serving inference skipped. Policy grammar: canonical ordering, repeatable `model=` and `effort=` clauses with
selectors, `model:<id>` selector term. Shards: team shards split by lane kind above the cap; merges add figures
and counts deterministically.

**Acceptance tests** (`tests/v2/sem/`):
- conventions: 5m/1h split, unknown residual with note, negative residual note; iterations: len-1 sum == top,
  fallback last == top, mismatch → `dq.iterations_mismatch`; SPEC Appendix A.8 (0 → `billable=False`
  `anthropic.refusal.pre_output`; 6 → `None` `anthropic.refusal.ambiguous`; 2,127 → `True`
  `anthropic.refusal.mid_stream`); compaction + message iterations → two inferences; advisor with and without
  `advisor_model`; `codex.rollout` raises `PricingError`; `sum_check` flags a 17-vs-17,119 defect;
- cache rules: every row of SPEC Appendix A.13; Azure scope `subscription`;
- transitions: Appendix A.9 thresholds; one fixture per cause rule in precedence order (compaction beats ttl;
  model switch beats ttl; refusal-fallback; plan-toggle; ping-pong; effort change ignored for Claude Code on
  Opus 5.5 but a cause for an SDK lane on Opus 5.5 without the beta; each canonical diag label;
  context-shrank; unexplained); ±10 s ambiguity; `predicted_hit is None` when τ unknown;
  `static_prefix_floor` needs ≥ 5 lanes; a request with only an OUTPUT_RESIDUAL inference is skipped;
- shapley: Appendix A.7 (credits 8/18/4, Σ = 30); `shapley_mc` within 3 SE of exact on a 5-player game;
  k > 10 raises; `scale_credits` preserves Σ exactly;
- policy: `parse_policy(to_spec(p)) == p` for hypothesis-generated policies; unknown clause/selector →
  `UsageError`; `combine` conflict raises; `lane_matches` on each selector key;
- shards: `plan_shards` splits a 300k-request team by lane kind and not a 200k one; `merge_replay` of two halves
  equals the whole (using a trivial in-test replayer); `merge_findings` raises on a duplicate id;
  `stratified_sample` deterministic per seed and the whole index when small;
- findings helpers: `finding_id` independent of evidence order; `miss_waste` on hand fixtures; `fit_cpt` falls
  back below 30 samples;
- no-float lint clean on `core/`.

**Size.** ~2.9k LOC including tests.
