# tests/v2/copilot_det_usage — CP-DET-USAGE acceptance tests

Wave-2b package CP-DET-USAGE: the aggregate detector `copilot.org-scan`
(`tokenbill/detect/copilot_org.py: CopilotOrgScan`, addendum §10.2) — the causes of GitHub Copilot
AI-credit and Actions spend from GitHub's own billing data (AI usage report, detailed usage report,
usage metrics, agent tasks, gh-aw runs), with no collectors and no lanes.

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/copilot_det_usage` (also `--python 3.10`).
Coverage: `uv run --python 3.12 --extra dev coverage run -m pytest tests/v2/copilot_det_usage && uv run
--python 3.12 --extra dev coverage report --include='tokenbill/detect/copilot_org.py'` (97% at
hand-off; the rest are defensive branches).

| file | covers |
|---|---|
| `worlds.py` | area-local builders: report rows (`make_ai_usage_row`), Actions lines, `ide:*` activity days, agent-task / gh-aw aggregates, C.P1 and C.P13 pool months, the `AnalysisContext` |
| `test_pool_kinds.py` | **C.P5 through `premium-model-share` and `auto-adoption`** (invoice / headroom $1,000 / $0, $600 / $400, $0 / $1,000 per regime); **plan unknown (C.P13)**: two findings, business $600 + $400, enterprise $0 + $1,000, both ESTIMATED, "If Business:" / "If Enterprise:", never merged; a known month used in both scenarios; no pool month / no token columns → unpriced (R2); the GPT-5.5 → Terra tokenizer band [1.00, 1.35]; direct-org credits (invoice unless their discounts show a pool draw); **`fast-mode` C.G8 = 145,000,000 nano EXACT, C.G8b ESTIMATED [176,250,000; 195,000,000]**, undecided convention → ESTIMATED; the remap excludes the fast premium; **reach 0.8 from 20% `ide:intellij` scales the Auto figure exactly**, no metrics → point 0 with range to full, **a 60% JetBrains team gets the model-policy fix**, cloud-agent cells reach 1, Auto-routed and review cells excluded, reach from `activity_counts` equals reach from the days; `cache-health` against the org median (upper bound) |
| `test_entity_kinds.py` | **`forced-migration` GPT-5.4 at GPT-5.6 Sol vs Terra = hand arithmetic ($2.90 per row), expires after 2026-10-19**, no cheaper option / retired models; **`compliance-uplift` 1,100 credits → 100 credits ($1.00) ESTIMATED**; `review-cost` credits + Actions dollars (R16 list vs invoice in the summary), **`review-default-balanced` before 2026-10-28 with `recoverable is None`, not after**, `review-drivers`; **`direct-org-usage` = Σ net with the R16 label (INVOICE only final + closed + reconciled; mixed → `combine_weakest`) and the pool draw from discounts**; **`agentic-workflow-cost`: Actions lines + same-repo direct credits → two figures, exposure = runs × 1,000 AIC**, per-run p50/p90 of our price; **`larger-runner` on `linux_16_core` → [net − minutes × $0.006; net]**; **`cloud-agent-cost` reads `ctx.outcomes`** (PR ratio; without outcomes the ratio is a skipped kind); **`agent-failed-sessions` builds with the provider estimate only in evidence**; `unattributed-spend`; no reference date → dated kinds skipped |
| `test_activity_kinds.py` | `mcp-sprawl` (per-user max of daily distinct counts, team median and heavy share, thresholds), `context-heavy-cli` (p50 / p90 prompt tokens per request), `editor-mix` (metrics, `activity_counts`, seat-surface fallback with reach unknown), aggregate-only bundles skip per-user kinds, capability gates (`KIND_REQUIRES`) name every skipped kind once in `dq.skipped-kinds` |
| `test_conformance.py` | `assert_detector_conforms` on a world planting all 18 kinds (known plan and plan unknown), class attributes and registry path, `run_detectors` phases (once with `aggregates_only=True`, silent without `ext:copilot`, `missing-capabilities` without `aggregates`), no `p_`, canary, login or repo hash in any finding, **a 3-person team re-scoped by `core.kanon.rescope_findings` whatever its category (R-E16; `aggregate` and `premium` alike)** while entity findings pass, R-E20 bases, permutation invariance, `min_usd`, org entity mode |
| `test_properties.py` | hypothesis: random worlds (models incl. pseudo and unpriced labels, teams, direct rows, discounts, Actions SKUs, activity, pool regimes / scenarios / open months / org mode, compliance, gh-aw runs, agent tasks, conventions, dates) conform, never raise, keep invoice + headroom = saving (fast mode), emit scenario findings only in complete pairs, never leak a person, are permutation invariant; threshold fuzzing raises only `UsageError` |
| `test_gate_synth.py` | **gate** (`importorskip("tokenbill.synth.copilot_world")`, CP-SYNTH): payments / mobile / data / ci plants recovered, the jetbrains team's Auto reach equals its truth, control team `core` clean; truth attribute names read defensively (CP-SYNTH is built in parallel; the contract owner aligns names at gate 1) |

## Fixtures and provenance

No fixture files. Every record is synthetic and built in code with `core.builders`; the numbers are
the hand-computed worked examples of addendum Appendix C (G8, G8b, P1, P5, P13) and hand arithmetic
on the §19.2 rates as `core.testing.FakePricer` loads them from `core/facts.json`. No real export,
report or login.

## Kinds, labels and gates (what the tests pin)

| kind | count source | scope | cost_observed | recoverable / headroom |
|---|---|---|---|---|
| premium-model-share | cost_lines | team | credits on Powerful models with a remap target, LE EXACT | price-only remap saving → `realize_credit_saving` per scenario |
| fast-mode | cost_lines | team | fast premium on identical tokens, LE EXACT (ESTIMATED range with unknown-TTL writes) | same, pool-converted |
| auto-adoption | cost_lines | team | direct-routed eligible credits, LE EXACT | 10% × credits × reach, pool-converted |
| cache-health | cost_lines | team × model | team × model spend | upper bound to the org median read share, pool-converted |
| forced-migration | entity | model | successor vs the cheaper same-vendor option (or vs today's model) on identical tokens at today's rates; `projected_monthly` ESTIMATED | none |
| compliance-uplift | entity | entity | observed / 11, ESTIMATED | none |
| review-cost, review-drivers, review-default-balanced | entity | entity | review credits (Actions $ in evidence, R16 label in the summary) | none (R13) |
| direct-org-usage, unattributed-spend | entity | entity | Σ net, R16 | none |
| agentic-workflow-cost | entity | entity | Actions $ (R16); AI credits, exposure and per-run stats in evidence | none |
| larger-runner | entity | entity | Actions net $ (R16) | ESTIMATED LIST range |
| cloud-agent-cost | entity | entity | credits (Actions $ and per-PR ratio in evidence) | none |
| agent-failed-sessions | entity | entity | `unpriced("provider estimate only")`, provider estimate in evidence (R12) | none |
| mcp-sprawl, context-heavy-cli, editor-mix | activity | team | unpriced (count-only) | none |

Thresholds (`ctx.thresholds["copilot.org-scan.<name>"]`, bounded; out of range → `UsageError`):
`jetbrains_policy_share` 0.5, `mcp_heavy_distinct` 5, `mcp_heavy_share` 0.25, `cli_heavy_tokens`
100000, `review_window_days` 30; plus the shared `min_usd`.

## Interpretations (where the texts left room; see the module docstring)

- **`requires = {"aggregates"}`** as the brief states (the addendum's revision-3 §10.0 text says
  `frozenset()`; the brief wins); per-kind gates are `KIND_REQUIRES` (alternative capability sets,
  each all-of) plus input checks, all reported in one `dq.skipped-kinds` finding.
- **Cell repricing uses the pricer's default-tier rates** (`Pricer.resolve` / `unit_rates`, which
  apply the Auto, compliance and fast-mode modifiers): a report cell aggregates many requests, so
  pricing it through `price_usage` would wrongly trigger per-request long-context bands. Unknown-TTL
  writes span the 5m (point) and 1h (high) write rates, rounded once per bucket.
- **Remap savings price the source at standard speed**, so the fast premium is counted only by
  `fast-mode` (the two findings never overlap).
- **Direct-org credits** (`ai_credit.direct`) are metered to the organization: their savings are
  invoice dollars (ESTIMATED LIST, an upper bound while `direct_draws_pool` is unknown) unless the
  pool month says they draw the pool (then they are realized with the pooled credits).
- **Forced migration** prices at today's rates (the move happens in the future); the cheaper
  same-vendor option is `copilot_remap(successor)`; without one the comparison is the successor vs
  the retiring model today (emitted only when the successor costs more).
- **Reach** uses `ide:*` counts (`core.catalog.editor_family`); a team with only CLI / Copilot app
  activity is reached (1); no activity → point 0, range [0, 1]. Seat / activity-report surfaces are
  shown by `editor-mix` but never used for reach (the brief ties reach to metrics).
- **Count-only kinds** (`mcp-sprawl`, `context-heavy-cli`, `editor-mix`) carry an unpriced
  `cost_observed` and are gated by their count thresholds instead of `min_usd`;
  `agent-failed-sessions` is emitted whenever a failed session consumed credits (its only money is
  a provider estimate); `review-cost`, `cloud-agent-cost`, `direct-org-usage` and
  `agentic-workflow-cost` pass `min_usd` on any of their two figures (or the exposure).
- **Scope** never carries `editor_family` (CORE-AMENDMENTS CA-46: not a scope dim); editor shares
  are evidence attrs. An unattributed team has no `team` dim (as generic detectors do).
- **Research reference ids** (`copilot-billing-*`, `copilot-cost-levers-*`, `copilot-data-apis-*`)
  name the addendum's research tracks; no catalog of Copilot finding ids exists yet.

## Facts

This package transcribes no priced fact: rates and modifiers come from the pricer
(`core.testing.FakePricer` over `core/facts.json`), retirements, remaps, categories, runner rates,
editor families and fixes from `core.catalog`, dates, review estimates, the report lag and the
1,000 AIC default cap from `core.facts` — all `verification: "research"` until the Copilot release
gate (R-E19). Unverified items inherited from there and used here: the Auto discount's report
representation and stacking with compliance (§19.5 #3, #4), whether direct usage draws the pool
(#7, read from discounts), `.lock.yml` workflow paths for agentic workflows (#16), JetBrains editor
strings (#22), GPT-5.x write prices before 2026-08-03 (#15).
