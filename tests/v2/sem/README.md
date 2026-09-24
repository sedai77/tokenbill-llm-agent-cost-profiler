# tests/v2/sem — F-SEM acceptance tests

Foundation semantics (wave 1): SPEC §3.11 (conventions), §3.14 (cache rules), §3.15 (transitions),
§3.16 (Shapley), §3.19 (policy grammar), §3.21 (shards), §3.22 (detector helpers), D28–D30.
Owned modules: `tokenbill/core/{conventions,cache_rules,transitions,shapley,policy,shards,findings}.py`.

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/sem` (also on 3.10).
Coverage: `uv run --python 3.12 --extra dev coverage run -m pytest tests/v2/sem && uv run --python
3.12 --extra dev coverage report --include='tokenbill/core/conventions.py,tokenbill/core/cache_rules.py,tokenbill/core/transitions.py,tokenbill/core/shapley.py,tokenbill/core/policy.py,tokenbill/core/shards.py,tokenbill/core/findings.py'`
(99% at hand-off).

| file | covers |
|---|---|
| `helpers.py` | area-local builders (short `usage()`, `req()` in seconds, `lane()`, `two_step()`, `event()`) on top of `core.builders` |
| `test_conventions.py` | `anthropic.messages` mapping (5m/1h split, unknown residual + note, negative residual + note, absent total, null fields, thinking > output), `bad_usage` table, registry (idempotent re-registration, conflicts, lazy import of `CONVENTION_MODULES` via a temp module, broken modules propagate), disabled `codex.rollout` → `PricingError`, `sum_check` (17 vs 17,119), iterations (len 1, fallback last == top, Σ / Σ-messages for compaction, mismatch → `dq.iterations_mismatch`), Appendix A.8 refusal table (0 / 6 / 2,127, boundaries, configurable threshold) priced with `FlatRates`, compaction + message → two inferences, advisor with and without `advisor_model`, server-tool attachment; fuzz (only `TokenbillError` escapes) and totals property |
| `test_cache_rules.py` | every Appendix A.13 row, both D28 branches in detail (numeric dotted versions, unknown/garbage versions, normalized model ids, channels, exact beta matching incl. raw header strings), every table row (Anthropic channels, `openai_api` 5.6+ TTL, Azure scope `subscription`), fallback rows, caching; hostile inputs (digit runs beyond `int()`'s limit) and a fuzz test that the predicate and `rules_for` never crash |
| `test_transitions.py` | Appendix A.9 thresholds, Appendix A.1 lane quantities; one fixture per cause rule in precedence order (reset events / edits / dropped thinking beat model switch and ttl; model switch beats ttl; refusal-fallback from inferences and events; availability-fallback; plan-toggle and its guards; ping-pong; ttl beats param change; fast-toggle; effort change ignored for Claude Code on Opus 5.5 but a cause for an SDK lane without the beta; client upgrade; directory change; each canonical diagnostic label; context-shrank; unexplained); ±10 s ambiguity; τ from 5m / 1h / other / hint; `predicted_hit` (None when τ unknown, min-cacheable gate, never reads `R_i`); OUTPUT_RESIDUAL-only request skipped; linear time on 20k requests; static-prefix floor (≥ 5 lanes, R > 0, even median); random-lane properties incl. τ == `core.lanes.ttl_of_last_write` |
| `test_shapley.py` | Appendix A.7 (8 / 18 / 4, Σ = 30), order independence, one evaluation per coalition, k > 10 raises, efficiency with v(∅) ≠ 0, negative values, brute-force agreement (hypothesis), MC within 3 SE of exact on a 5-player game, MC determinism (also for str-enum players), `scale_credits` exact totals (hand case + property), `largest_remainder` |
| `test_policy.py` | `parse_policy(to_spec(p)) == p` for hypothesis-generated canonical policies, canonical form for any name, grammar fuzz (only `UsageError`), SPEC example, observed policy, defaults and units, full canonical order, every §11.1 / §10.2 grid fragment, invalid-spec table, `to_spec` refusals, `combine` union / conflicts / non-canonical inputs, `selector_terms`, `lane_matches` on every selector key, `request_matches`, billing-path fallback, empty lanes |
| `test_shards.py` | `plan_shards` splits a 300k-request team by lane kind and not a 200k one, cap boundary and order, `shard_where` (incl. the unattributed `""` filter), `shard_of_lanes` partitions, `merge_replay` of halves (and of any contiguous split) equals the whole with an in-test replayer, merge details and refusals, `merge_findings` sort and duplicate id, `stratified_sample` (whole index when small, deterministic per seed, proportional strata, size property), enum- vs str-valued index rows plan and sample identically |
| `test_findings.py` | `cohort_key`, `make_scope`, `finding_id` (stable, independent of evidence and dim order), `build_finding` validation table incl. allowance / provider-estimate bases (a provider-estimate data-quality finding in an allowance cohort), `miss_waste` hand fixtures, `min_usd_nano` / `threshold`, `fit_cpt` (fallback below 30 samples, fits, sample exclusions, half-open reset window), `top_evidence`, `sum_figures`, `rate_nano` on both pricer paths |
| `test_no_float.py` | AST lint: no `float(` and no float literal in the seven F-SEM modules |

## Fixtures and provenance

No fixture files. Every record is synthetic, built in the test with `core.builders` (`FlatRates`,
`make_request`, `make_lane`, `lane_from_table`, …) or with the area-local `helpers.py`; usage objects
follow the Anthropic field names of SPEC §19.4. No real transcripts or provider pages are used. The
Appendix A fixtures (A.1, A.7, A.8, A.9, A.13) are transcribed from the SPEC.

## Facts (verified 2026-09-23 against primary sources)

Cache rules re-checked for this package (`core.cache_rules.VERIFIED_ON`):
- Anthropic prompt caching (<https://platform.claude.com/docs/en/build-with-claude/prompt-caching>):
  5 min / 1 h TTLs from request start, entry readable after the first response token, 20-position
  lookback with tool_use / tool_result runs counted as one position, 4 breakpoints, workspace
  isolation on the Claude API, Claude Platform on AWS and Foundry, organization isolation on Bedrock
  and Google Cloud, the invalidation hierarchy (thinking / effort: messages always, tools / system
  "model-specific").
- Claude Code prompt caching (<https://code.claude.com/docs/en/prompt-caching>): effort changes keep
  the cache on Opus 5.5 and Fable 5.1 with an API key or subscription, not on Bedrock or Google
  Cloud; before v2.1.260 Fable 5.1 invalidated.
- Mid-conversation system messages
  (<https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages>): beta
  `mid-conversation-output-config-2026-07-01`; Fable 5.1, Mythos 5.1, Opus 5.5, Opus 5; Claude API and
  Google Cloud only — **differs from SPEC §3.14 for Foundry** (see `CONTRACT-CHANGE-F-SEM-1.md`).
- OpenAI prompt caching (<https://developers.openai.com/api/docs/guides/prompt-caching>): GPT-5.6+
  `30m` TTL from the most recent write or reuse, organization scope, 4 cache writes per request;
  model, tools, parallel_tool_calls, text.format, reasoning.effort, text.verbosity and
  context_management break the prefix.
- Azure OpenAI (<https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching>):
  caches are not shared across subscriptions (D43).

Thresholds come from `core/facts.json` via `core.evidence` (`MISS_MIN_TOKENS`, `MISS_MIN_FRACTION`,
`REFUSAL_AMBIGUOUS_MAX_OUTPUT`, `KEEPALIVE_MAX_IDLE_S`, `CPT_DEFAULT_*`).

### Unverified (shipped conservatively, listed for the release notes)

- `effort_invalidates_all_tiers_models` is empty: the docs say thinking/effort also invalidate the
  tools and system caches on some models without naming them (§19.8 #14).
- Azure OpenAI TTL for GPT-5.6+: not documented; the Azure row carries no TTL option.
- Per-message effort on Claude Platform on AWS: not named by the source; kept from the SPEC.
- Bedrock / Vertex / Foundry / Claude Platform on AWS lookback and tool-run collapsing: assumed equal
  to the Anthropic API except `collapse_tool_runs` (True on `anthropic_api` only, per §19.3).
- The OpenAI `tier_params` mapping (every invalidating parameter salts the first tier) is a modeling
  choice for the block engine, not a documented hierarchy.
- The Claude Code effort exemption cannot see `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`, HIPAA
  configurations or the Claude apps gateway (all disable it per the docs); the v2.1.260 gate is
  applied to Opus 5.5 as well as Fable 5.1, as the SPEC states (the docs mention it for Fable 5.1).
- `fit_cpt` sample definition (text-like appended items, 1–8 bytes per token plausibility band) is a
  design choice; the 2.5 / 3.3 fallbacks are research-corpus values from `core/facts.json`.

## Contract notes

`CONTRACT-CHANGE-F-SEM-1.md` records the interpretations F-SEM implemented (policy naming and
defaults, `block:` repairs, the unattributed shard filter, iteration invariants, "differs" means
both known, basis checks in `build_finding`, …), the Foundry fact correction, and the additive public
names.
