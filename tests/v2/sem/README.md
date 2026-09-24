# tests/v2/sem — F-SEM acceptance tests

Foundation semantics (wave 1): SPEC §3.11 (conventions), §3.14 (cache rules), §3.15 (transitions),
§3.16 (Shapley), §3.19 (policy grammar), §3.21 (shards), §3.22 (detector helpers), D28–D30.
GitHub Copilot additions (wave 1.5b, package F-SEM-C, checked as `--package F-SEM`):
CORE-AMENDMENTS S-1 … S-5 and ruling R-E20 — see "GitHub Copilot (F-SEM-C)" below.
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
| `test_copilot_findings.py` | S-1: a Claude Code (`api_key`) and a Copilot (`copilot_pool`, `copilot_direct`) lane of one team never share a cohort (3-tuple unchanged); `product_family` by billing class, by the first request's channel (also without a serving inference), first request decides; `make_scope(billing_class="pool")` adds `product: copilot` (explicit product kept); `run_detectors`' lane filter uses it. S-2 / R-E20: the brief's valid finding (cost LE, recoverable ESTIMATED LIST, headroom LE), rejections (headroom on LIST / INVOICE / CONTRACT, CONTRACT anywhere, INVOICE dollar fields, PROVIDER_ESTIMATE outside data-quality or as headroom), INVOICE cost on an entity scope, pool-regime / idle-seat / auto-adoption shapes, `billing_class=pool` alone is a Copilot scope, headroom on billed / allowance / other-product scopes raises, R-E8 unchanged; hypothesis property over every basis combination on both scope kinds; pool labels (prefix, phrase, idempotence, 120 / 400 limits with word-boundary cuts, only with a LIST_EQUIVALENT figure, never on Claude or aggregate Copilot scopes); fixes (stub `fix_for` substitution, strip + "(no Copilot setting known)" without an entry or without the accessor, non-Copilot answers ignored, Copilot fixes kept, a None fix gets the catalog fix (stays None without one), Claude scopes untouched, errors propagate; a `gate` test over the real `fix_for` once F-KIT-C lands). S-3: `min_usd_gate` on Claude and Copilot findings (headroom, unpriced values, cost fallback, thresholds) |
| `test_copilot_cache_rules.py` | S-4: `ttl_semantics_known` is the appended defaulted last field (frozen, slots); the `github_copilot` row for every provider and model (every value of S-4, sources, `context_tier` only in the messages tier); `channels()` unchanged; every other row `ttl_semantics_known=True`; caching; effort changes invalidate on Copilot; hypothesis over providers / models |
| `test_copilot_transitions.py` | S-4: τ + prev_duration + 11 s → `ttl-expiry`, τ + prev_duration + 10 s → ambiguous, never expiry; ambiguity window ± (prev_duration + 10 s) with boundaries; the previous request's duration (not the current one), retries and unknown durations; τ unknown → never `ttl-expiry` (up to 2 days); τ from a 5m write class; Claude lanes unchanged whatever the durations; `rules=None` → built-in table; row-driven (a provider marking `anthropic_api` unknown, providers whose rows lack the field, one lookup per context); `context-tier-change` (order after `effort-change`, before `client-upgrade`, beaten by fast-toggle / model switch / expiry, unobserved tiers never a change, `predicted_hit` False); hypothesis properties for Copilot and Claude lanes; linear time on a 20k-request Copilot lane |
| `test_copilot_conventions.py` | S-5: a missing `CONVENTION_MODULES` entry is skipped with one `dq.convention_module_unavailable` note (detail = module name) while other conventions normalize; notes in registry order incl. a missing parent package; a broken module still propagates (earlier notes kept); a new load replaces old notes; known ids never load; the real registry list notes exactly the modules not installed in this tree |

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

## GitHub Copilot (F-SEM-C, wave 1.5b)

Builds CORE-AMENDMENTS S-1 … S-5 exactly (they win over addendum §3.6 CA-31 … CA-34: no
`is_copilot_scope` / `CONSERVATIVE_TTL_CHANNELS` contract, `context_tier` in the messages tier, no
"one basis B" rule across the dollar fields). Public additions: `findings.product_family`,
`findings.min_usd_gate`, the constants `DEFAULT_FAMILY`, `COPILOT_FAMILY`, `COPILOT_CHANNEL`,
`COPILOT_TITLE_PREFIX`, `COPILOT_SUMMARY_PHRASE`, `COPILOT_FIX_TARGET`, `NO_COPILOT_SETTING`;
`cache_rules.CacheRules.ttl_semantics_known` (appended, default True), `COPILOT_CACHE_CHANNEL`;
`transitions.PARAM_CHANGE_SUB_CAUSES` gains `context-tier-change` after `effort-change`;
`conventions.load_notes`, `DQ_CONVENTION_MODULE_UNAVAILABLE`. No existing test was edited (T-4).

Interpretations where the brief leaves a detail open (all documented in the docstrings):
- **Copilot scope** = a `product=copilot` or a `billing_class=pool` dim (R-E20). Fix substitution
  applies to both (a hand-built pool scope without `product` must not keep Claude Code keys, DC14).
- **Pool labels** apply to `billing_class=pool` scopes with at least one LIST_EQUIVALENT figure
  (addendum CA-31's condition: a provider-estimate data-quality finding is not an AI-credit value),
  whatever the detector (CP-DET-LANES relies on it for `copilot.lanes`). The phrase is appended as
  " (list-equivalent AI-credit value)"; the text before a label is cut at a word boundary with "…"
  (never mid-token, so `core.kanon` scrubbing still finds whole scope values). Idempotent.
- **Fixes:** a fix already targeting `github-copilot` is kept (the catalog is not consulted); any
  other fix, and a None fix, is replaced by the catalog's Copilot fix when one exists (on Copilot
  lanes the fix text comes from `fix_for`, addendum §10.4); without one a None fix stays None; a
  `fix_for` answer that is not a `github-copilot` `Fix` counts as "none"; the stripped fix keeps
  text and `doc_url`, drops `config_patch`, `target` and the applicability `gates` (they qualify
  the dropped Claude Code patch); exceptions from `fix_for` propagate.
- **R-E20:** PROVIDER_ESTIMATE is accepted in a data-quality finding's money fields except
  `headroom`; no cross-field basis constraint.
- **`min_usd_gate`:** the compared figures are the recoverable (Copilot scopes: and `headroom`);
  a priced one ≥ `min_usd_nano(ctx)` passes; when none is present or one is unpriced (the maximum
  is unknown, never zero: R2) the `cost_observed` point decides, as DETECT-CACHE's own gate does;
  every compared figure priced and below fails; an unpriced `cost_observed` never passes.
- **Unknown-TTL rule:** the row is `rules.rules_for(provider, channel, model)` of request `i`'s
  serving inference (memoized per lane; `rules=None` → the built-in `RulesTable`; a row without
  the attribute counts as known). `prev_duration` = from request `i−1`'s first attempt start to its
  latest attempt end (`ts_start_ms + duration_ms`, an unknown duration counting 0). The
  documented prediction (`predicted_hit`) and the ping-pong sub-cause keep `gap ≤ τ`.
- **`context-tier-change`:** "differs" means both serving inferences state a tier (the F-SEM rule
  for every parameter); it is channel-independent.
- **`load_notes()`** is a pure accessor (empty until an unknown convention id triggered the load);
  each load replaces the notes; a module that exists but fails to import still propagates (pinned
  by `test_broken_extension_module_propagates`).

Outputs added by this package carry no content: note details are registry module names, row
sources and label / fix suffixes are constants.

### Unverified (Copilot; research-verified 2026-09-23, shipped conservatively)

- GitHub publishes no per-model cache TTL for its proxy, nor where the TTL clock starts or whether
  reads refresh it (addendum §19.3 #26, §19.5 #14): hence `ttl_options_s=()` and
  `ttl_semantics_known=False`; τ comes only from write-TTL hints. The tutorial's "24 hours for
  OpenAI models and 1 hour for most others" is display text (`facts.copilot.cache_ttl_statement`).
- The Copilot row's `refresh_on_read=True`, `visible_from="response_end"`, `lookback_positions=None`,
  `max_breakpoints=4` and `scope="organization"` are the S-4 values (the merged conservative
  default), not GitHub-documented; the VS Code Responses path places up to 20 + 2 breakpoints and
  the 1h TTL is gated by model (`modelSupportsExtendedCacheTtl` in
  `extensions/copilot/src/platform/networking/common/anthropic.ts`, read in the research mirror of
  `microsoft/vscode`; cost-levers F8 fact-check) — not modelled here.
- Salting `context_tier` into the messages tier follows S-4 and the tutorial ("changing … context
  size" invalidates the cache); the tier GitHub actually invalidates is not documented.

## Contract notes

`CONTRACT-CHANGE-F-SEM-1.md` records the interpretations F-SEM implemented (policy naming and
defaults, `block:` repairs, the unattributed shard filter, iteration invariants, "differs" means
both known, basis checks in `build_finding`, …), the Foundry fact correction, and the additive public
names.
