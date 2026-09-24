# tests/v2/copilot_plan — CP-PLAN acceptance tests (`tokenbill/copilot/plan.py`)

Wave-2b package CP-PLAN: the GitHub Copilot aggregate plan — cell replay of the aggregate levers,
delivery reach (managed `model` does not reach JetBrains), exact / Monte Carlo Shapley and the pool
rule's conversion to invoice dollars, once per plan scenario while the plan is unknown (addendum
§9.2, §9.3, §11.2; DC3, DC10, DC19, DC20; R7, R11, R17; rulings R-E20, R-E22).

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/copilot_plan` (also `--python 3.10`).
Coverage: `uv run --python 3.12 --extra dev coverage run -m pytest tests/v2/copilot_plan && uv run
--python 3.12 --extra dev coverage report --include='tokenbill/copilot/plan.py'` (99% at hand-off).

| file | covers |
|---|---|
| `worlds.py` | area-local builders: a `World` of report rows, seat lines, Actions lines, activity-report seats, metrics activity and run flags / cost-center caps, turned into cells and pool months by the real `core.pool.build_cells` / `pool_months`; valid Copilot findings (`build_finding`, `product=copilot` scopes) and seat findings with evidence counts |
| `test_pool_conversion.py` | C.P2 (seat lever in overage: Shapley and invoice 0, headroom 0), C.P3 ($950), C.P4 ($300), C.P5 pairs ($1,000 / 0; $600 / $400; 0 / $1,000) through the Auto lever, the slack variant (seats $950, credit levers invoice 0 with headroom = their list-equivalent saving), Auto + model policy in overage (Σφ == v(both) to the nano; hand values 57,000 / 307,000 credits), the three-lever game against `core.shapley.shapley_exact` over sub-plan values, headline = Σ projected invoice credits |
| `test_seats.py` | C.P10 volume ($0 + renewal note), azure, C.P11 seat policy ($190, `needs_eval`, trade-off, `@org:<o>`), C.P12 ($228), the 30d / 60d idle thresholds, unknown-assignment seats as an upper bound, Enterprise seats, unknown-plan seats, plan-mix downgrade ($400 slack / $0 overage), scope matching, the evidence reader `seat_counts` |
| `test_reach.py` | 20% JetBrains → 0.8 × the 10% saving; no activity → point 0, high full; cloud agent reach 1; code-review / Auto-routed cells ineligible; excluded families, CLI and app counts; month selection; reach from `activity_counts` == reach from the `ActivityDay`s (and identical plans); `model:auto` shares without Auto labels; direct org rows (net share invoice, discount share headroom); unattributed team |
| `test_scenarios.py` | C.P13 scenario pools; C.P13b (idle seats $0 if Business, $390 upper bound if Enterprise); C.P13c (credit saving $500 invoice vs $500 headroom); `copilot.seat_downgrade` absent from both with "plan unknown"; no figure outside a scenario ("If Business:" / "If Enterprise:" on every figure, scenario in `sample`); scenario findings; the known plan keyed `"known"`; `plan_copilot` refuses an unknown plan without a scenario; a known and an unknown org side by side |
| `test_caps_forecast.py` | C.P7 capped cost center (continue $145, block $0 with headroom, unknown → headline range spanning both), shared-pool seat removal, C.P9 forecast levels (seat saving point 0, range [0; $950]), open month without forecast, day vs month grain, the larger-runner range, unknown pools (whole plan unpriced; direct rows kept; one entity excluded and named) |
| `test_game.py` | trade-off gating, behavioral / `replay="none"` levers never play, linking by `lever_ids`, non-Copilot findings ignored, C.G8 fast premium (145,000,000 nano), remap band (GPT-5.5 → Terra, [$13.80; $18]), unpriced remap targets, remap pairs from finding models, compliance ×1.1, Monte Carlo above 6 players (seeded, SE per spec, efficient), params round-trip, result groups, threshold ties, empty inputs, determinism under shuffled inputs, input validation |
| `test_properties.py` | hypothesis: invoice + headroom == the credit saving for any regime; Shapley efficiency and seat-saving bounds; more idle seats never save less; order independence; fuzz of the seat-count reader and of the plan over hostile findings (only `TokenbillError` escapes) |
| `test_edges.py` | pricer errors, unpriced fast cells, December month grain, linked levers with nothing to act on, wrong-kind seat findings, org names outside the grammar, org-mode entity resolution, other months and legacy rows, `None` sequences, helpers |
| `test_gate.py` | `@pytest.mark.gate` (skipped until merged): CP-DET-SEATS findings on the C.P12 and C.P13b worlds give $228 and $0 / $390; CP-DET-USAGE `auto-adoption` findings give 0.8 × the Auto saving |
| `CONTRACT-CHANGE-CP-PLAN-1.md` | seams the SPEC leaves open: seat-count evidence names (to CP-DET-SEATS), `activity_counts` sums (to CP-HANDOFF), the `"known"` key, per-lever headroom and plan notes (proposed fields), additive keywords, the headroom formula |

## Fixtures and provenance

No fixture files. Every record is synthetic, built in the tests with `core.builders`
(`make_ai_usage_row`, `make_seat_line`, `make_actions_line`, `make_license`, `make_activity`,
`make_config`) and priced with `core.testing.FakePricer` over `core/facts.json`; the numbers are the
hand-computed worked examples of addendum Appendix C (P1–P5, P7, P9's binding daily series, P10–P13,
P13b, P13c, G8) and the CP-PLAN brief. No real exports, logins or transcripts.

## Interpretations (the module docstring states them; CONTRACT-CHANGE-CP-PLAN-1 lists the gaps)

- **Players** are canonical aggregate specs: one per remap pair (`copilot.model_policy` may appear
  several times), the better of the seat thresholds (30d = buckets `31-90` + `none_90d`; 60d =
  `none_90d`; a finding without a bucket counts only at 30d), `@org:<o>` for the seat policy when
  one org is linked. Levers linked but with nothing to act on are named in the notes, not played.
- **Value** per billing group (the enterprise with its capped cost centers, or each org):
  `v(S) = Σ overage(C₀, P₀) − overage(C_S, P_S) − Δfees + direct net saving + runner saving`, with
  `core.pool.overage_total` and `core.pool.pool_credits` (Δpool of seat changes, promo when the pool
  month has it). Consumption = the pool month's (forecast) consumption scaled by the cells' relative
  change. Direct org rows save their net share in dollars; their discount share is headroom.
- **Headroom** = pooled consumption saving − what the pool rule realizes at the coalition's own
  pools, so seat-only coalitions free none (brief acceptance over the addendum's literal formula).
- **States**: the point uses consumption p50, cap policy *continue* for unknown policies (as
  `core.pool` does), reach 0 for teams without activity, band 1.00, runner low; ranges are the
  envelope over every varying dimension's extremes (p10/p90, block, reach 1, band 1.35, runner
  high). Projections: `RR_PRIORS` by lever class, interval product; the headline sums projected
  invoice credits with comonotone bounds; it is an upper bound when any included lever is.
- **Pricing** of remaps and fast mode through `Pricer.unit_rates` on the cell's day (month cells:
  the month's last day, else its first), so an aggregate never trips a per-request long-context
  band; the remap target at standard speed and the cell's routing; the `compliance` run flag applies
  the ×1.1 modifier. Unpriced cells are counted in the notes ("unknown, not zero").
- **Reach**: `ActivityDay` counts of the month (else every day); `activity_counts` only when there is
  no `ActivityDay` at all. Excluded families `jetbrains`, `visual_studio`, `xcode`, `eclipse`; every
  other family (incl. neovim, `other`) and CLI / app interactions count as reached (brief). Team names
  never appear in notes.
- **Unknown pools** (`PoolMonth.regime == "unknown"`): the entity's pooled cells and seats are left
  out and named; its direct rows stay (metered dollars). When every entity's pool is unknown and
  pooled cells exist, every figure is unpriced.
- **Month**: `forecast=False` on an open month uses month-to-date consumption (noted);
  `forecast=True` uses the pool month's forecast p10/p50/p90; direct rows and Actions lines stay
  observed month-to-date.

## Unverified facts (VERIFY; shipped as labelled assumptions)

- Every `facts.copilot` value the plan uses carries `verification: "research"` (R-E19): seat prices
  $19 / $39 and allowances 1,900 / 3,900 (promo 3,000 / 7,000), the Auto ×0.9 modifier, the
  compliance ×1.1 modifier (stacking assumed), Copilot rate rows, runner rates (`actions_linux`
  $0.006/min), the remap pairs ("design candidates", `tokenizer_same` from the rows' families).
- Managed `model` reach: JetBrains not reached (fact-checked); Visual Studio / Xcode / Eclipse
  unverified → treated as not reached; JetBrains product names → `jetbrains` via
  `facts.copilot.editor_families` (VERIFY).
- `TOKENIZER_BAND` (1.00–1.35) is Anthropic's published tokenizer band, applied to remaps between
  different tokenizers as the addendum §9.2 says.
- Without `Auto:` labels in the report the Auto share per team comes from metrics `model:auto`
  interaction shares (addendum §9.2, VERIFY).
- Seat changes: proration, upfront charges from 2026-10-01 and volume / EA pricing are not modeled;
  savings are "at unchanged use, effective next month".
- The larger-runner low bound assumes no included minutes are left (point); the high bound assumes
  they cover the standard minutes.
