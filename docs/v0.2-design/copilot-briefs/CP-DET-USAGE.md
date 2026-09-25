### CP-DET-USAGE — `copilot.org-scan`: model, Auto, fast mode, review, agents, direct and runner costs (wave 2)

**Goal.** Name the causes of Copilot AI-credit and Actions spend from GitHub's own billing data (100% of billed
usage, no collectors — the primary path for an organization whose developers use VS Code and IntelliJ):
premium-model share, fast mode, missed Auto discount, forced model migrations, compliance uplift, cache health,
code-review cost and its drivers, cloud agent and agentic workflows, direct org usage, larger runners,
unattributed spend, MCP sprawl and heavy CLI context — each with dollars that respect the pool rule (per plan
scenario when the plan is unknown), delivery reach (managed `model` does not reach JetBrains) and label
discipline. Read SPEC §3.5, §3.6, §3.22, §10.1, §10.3; addendum DC3, DC12, DC21, DC22, DC25, §1.2, §3.9, §8.2,
§9.2–§9.3, §10.0, §10.2, §19.1 #7, #8, #13–#19, #21, #23, §19.5 #3, #16, Appendix C.G2–G4, G8, P5; rulings
R-E16, R-E20, R-E22.

**Owns.** `tokenbill/detect/copilot_org.py` (`CopilotOrgScan`, id `copilot.org-scan`),
`tests/v2/copilot_det_usage/**`.

**Consumes.** `core.pool` (`build_cells`, `realize_credit_saving`), `core.findings` (`build_finding`,
`make_scope`, `min_usd_gate`), `core.catalog` (`copilot_category`, `copilot_remap`, `COPILOT_RETIREMENTS`,
`runner_rate`, `levers_for_kind(…, family="copilot")`, `fix_for`, `COUNT_SOURCE`, `editor_family`,
`ACTIVITY_KEYS`), `core.labels` (`combine_weakest` never used for credits), `core.types` (`AnalysisContext`
with `outcomes`, `pools`, `plans`, `recon_decisions`), `core.records`, `core.facts` (dates, review estimates for
quoted text only, `aic_default_cap_per_run`), `core.protocols.Pricer` (cell repricing), `core.testing`
(`FakePricer`, `assert_detector_conforms`), `core.builders`.

**Provides.** A registered detector with `aggregate=True`, `extension="copilot"`, `families={"copilot"}`,
`requires={"aggregates"}` and exactly the kinds of addendum §10.2 plus `dq.skipped-kinds`.

**Build.**
1. Cells from `ctx.aggregates` + `ctx.cost_lines` (`build_cells` with the convention decided by CP-RECON, read
   from `ctx.recon_decisions`); pool months from `ctx.pools`; activity from `ctx.activity` or, in
   aggregate-only bundles, `ConfigSnapshot(kind="activity_counts")` (reach only); outcomes from `ctx.outcomes`
   (`cloud-agent-cost` reads the enterprise `OutcomeAggregate.extra`); lanes are never read.
   `agentic-workflow-cost` uses Actions lines with workload `agentic_workflow`, direct cells of the same
   repositories and, when present, `UsageAggregate(gh_aw.run)` rows. Kinds whose inputs are absent
   (`mcp-sprawl` and `context-heavy-cli` without per-user activity; `cloud-agent-cost` PR ratio without
   outcomes) are listed in one `dq.skipped-kinds` finding.
2. Premium-model share: price-only replay of affected cells at the remap target's Copilot rates (FakePricer or
   the ctx pricer), `needs_eval`, trade-off. Fast mode: exact rate arithmetic on identical tokens. Compliance
   uplift: observed / 11, ESTIMATED. Larger runner: the low/high range of §10.2.
3. **Auto adoption and reach (owner answer 3).** 10% × credits × reach per team; reach = 1 − share of the
   team's interactions on editor families the managed `model` key does not reach (`jetbrains`, and
   `visual_studio`, `xcode`, `eclipse` as unverified), from `ide:*` activity counts through
   `core.catalog.editor_family`; cloud-agent cells reach 1; no metrics → point 0, range to full ("reach
   unknown"). For teams whose JetBrains share is ≥ 50% the fix text recommends the server-side model policy
   (reach 1, lever `copilot.model_policy`) and Auto-tier communication instead of the managed key.
4. **Pool conversion per scenario.** Every credit saving goes through `realize_credit_saving` with each
   applicable `PoolMonth`; when an entity has two scenario pool months the finding is emitted once per
   scenario (`plan_scenario` scope dim, ESTIMATED, "If Business:" / "If Enterprise:"), never merged.
5. Review kinds: `review-cost` shows review credits (`cost_observed`, LIST_EQUIVALENT) and review Actions
   dollars (evidence attrs, R16 label in the summary); `review-default-balanced` never projected, quotes the
   published ranges with source and date and the personal-default caveat; `review-drivers` info with the MCP
   default, PR size / instructions and personal-trigger items.
6. `agent-failed-sessions` (R12): `cost_observed = unpriced("unpriced: provider estimate only")`; the
   agent-task provider-estimate credits go into evidence attrs (`provider_estimate_nano`) — never a figure,
   because R-E20 / R4 reject PROVIDER_ESTIMATE outside data-quality findings.
7. Labels per R16 and bases per R-E20; credits and dollars never added; `min_usd_gate`; categories `aggregate`
   (k-anonymity from `COUNT_SOURCE`, R-E16); scopes `product: copilot`, `entity`, `team` / `cost_center`,
   `model`, `plan_scenario`.

**Acceptance tests** (builders, FakePricer).
- Appendix C.P5 through `premium-model-share` and `auto-adoption`: invoice / headroom pairs per regime; reach
  0.8 from metrics (20% `ide:intellij` interactions) scales the Auto figure exactly; no metrics → point 0 with
  range to full; a 60% JetBrains team gets the model-policy fix text.
- Plan unknown (C.P13 pool months): `premium-model-share` for 100,000 saved credits → two findings, business
  scenario invoice $600 + headroom $400, enterprise scenario invoice $0 + headroom $1,000 (P5 arithmetic per
  regime), both ESTIMATED.
- `fast-mode` premium on the C.G8 tokens = 145,000,000 nano EXACT; with unknown-TTL writes (G8b) the premium is
  ESTIMATED [176,250,000; 195,000,000].
- `forced-migration` Δ for GPT-5.4 cells at GPT-5.6 Sol vs GPT-5.6 Terra equals hand arithmetic; expires after
  2026-10-19.
- `compliance-uplift` on 1,100 observed credits → 100 credits ($1.00) ESTIMATED.
- `direct-org-usage` = Σ net of `ai_credit.direct` rows with the R16 label; its pool draw reported from their
  discounts.
- `agentic-workflow-cost`: Actions lines on a `.lock.yml` workflow plus direct cells in the same repo → two
  figures; exposure = runs × 1,000 AIC when no cap is known.
- `larger-runner` on `linux_16_core` minutes → range [net − minutes × $0.006; net].
- `review-default-balanced` emitted before 2026-10-28 with `recoverable is None`, not after.
- `agent-failed-sessions` builds (no `ContractViolation`) with the provider estimate only in evidence;
  `cloud-agent-cost` reads `ctx.outcomes`.
- `assert_detector_conforms`; privacy: a 3-person team re-scoped (category `aggregate` does not exempt it);
  no principal in any finding.
- Gate: CP-SYNTH world → payments / mobile / data / ci plants recovered within tolerance; the jetbrains team's
  Auto reach equals its truth; control team clean.

**Size.** ~2.85k LOC including tests.
