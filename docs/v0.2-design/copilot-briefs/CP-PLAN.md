### CP-PLAN — Copilot aggregate plan: cell replay, reach, Shapley, pool conversion (wave 2)

**Goal.** Turn Copilot findings into one honest, ranked plan in invoice dollars for the whole billed Copilot
usage: which GitHub-side levers (Auto default, model policy, fast mode off, seat reclaim / seat policy /
downgrade, runner type) save how much money this month given the pool regime, how much only frees pool
headroom, and how the joint saving splits fairly across levers — **per plan scenario** when the plan is unknown
(owner answer 2: the headline is then two columns, "if Business" / "if Enterprise", never one number), with
delivery reach that reflects the adopting developers' editors (VS Code reached by managed `model`, JetBrains
not). Read SPEC §3.5 (`ActionPlan`, `LeverResult`),
§3.16, §3.20, §11.1–§11.2, R7; addendum DC3, DC10, DC19, DC20, §1.2 (R11), §3.7 (CA-35 aggregate grammar),
§3.9, §9.2, §9.3, §11.1, §11.2, Appendix C.P1–P5, P7, P10–P12; `F-POOL.md` (P13, P13b); rulings R-E20, R-E22.

**Owns.** `tokenbill/copilot/plan.py` (`plan_copilot`), `tests/v2/copilot_plan/**`.

**Consumes.** `core.pool` (`Cell`, `overage_total`, `invoice_delta`, `realize_credit_saving`,
`realize_seat_change`), `core.shapley` (`shapley_exact`, `shapley_mc`), `core.catalog` (`COPILOT_LEVERS`,
`lever`, `levers_for_kind(…, family="copilot")`, `AGGREGATE_GRIDS`, `editor_family`, `parse_aggregate_spec`,
`to_aggregate_spec`, `copilot_remap`, `runner_rate`,
`RR_PRIORS`), `core.labels`, `core.types` (`ActionPlan`, `LeverResult`, `PoolMonth` with `plan_scenario`,
`PlanEvidence`, `ConfigSnapshot` `activity_counts`), `core.protocols.Pricer`,
`core.facts`, `core.testing` (`FakePricer`), `core.builders`.

**Provides.** `plan_copilot(cells, pools, findings, pricer, *, lines, activity, month, include_tradeoffs=False,
forecast=False) -> ActionPlan` with `LeverResult.params` = canonical aggregate spec, `headline_monthly`
(invoice, ESTIMATED, LIST), `pool_headroom_monthly` (LIST_EQUIVALENT), `method` `shapley-exact` (≤ 6 levers)
or `shapley-mc` (200 seeded permutations), `sample="copilot cells, <n> cells, month <m>"`, per-lever reach in
the lever note; and `plan_copilot_scenarios(cells, pools, findings, pricer, *, lines, activity, config, month,
include_tradeoffs=False, forecast=False) -> tuple[tuple[str, ActionPlan], ...]` — `(("known", plan),)` when
every entity's plan is known, else `(("business", plan_b), ("enterprise", plan_e))`, each computed on that
scenario's `PoolMonth`s (CP-WIRE stores it in `CopilotSummary.plans_by_scenario`; `CopilotSummary.plan` is the
known plan or None).

**Build.**
1. Candidate levers: aggregate levers linked by Copilot findings (`levers_for_kind(kind, family="copilot")`);
   trade-off levers only with `include_tradeoffs`; behavioral and `replay="none"` levers never enter the joint
   set.
2. Transforms in the fixed order of addendum §9.2 on monthly cells: remap (price-only, tokenizer band only
   when `tokenizer_same` is False), fast off, Auto × reach, seats (removable and seat-policy seats only;
   unknown-assignment seats only as an ESTIMATED upper bound with `needs_eval`; volume / azure entities none),
   runners (range). **Reach** (§9.3): per team, 1 − share of interactions on editor families the managed
   `model` key does not reach (`jetbrains`; `visual_studio`, `xcode`, `eclipse` unverified → excluded), from
   `ActivityDay` `ide:*` counts or, in aggregate-only bundles, `activity_counts` rows; cloud-agent cells reach
   1; no activity → point 0, high 1. The lever note names the JetBrains share.
3. **Plan unknown.** The whole game is played once per scenario on that scenario's pool months (identical
   cells, different pools); the `copilot.seat_downgrade` lever (`copilot:plan=business`) is excluded in both
   (it presupposes Enterprise seats that may not exist) and listed with the reason "plan unknown"; results are
   never averaged or summed across scenarios.
4. `invoice_e(S)` = fees + overage (`overage_total` low / high, cap policies) + direct + Actions; `v(S)` at
   point / low / high; exact Shapley with the largest-remainder rule; `headroom(S)` LIST_EQUIVALENT; RR priors
   by lever class (`project()` implemented locally); standalone values never summed (R7).

**Acceptance tests.**
- C.P1–P5: in the overage entity the seat lever's Shapley credit and invoice value are 0 and headroom 0; in
  the slack variant it saves $950/month and credit levers have invoice 0 with headroom equal to their
  list-equivalent saving; Auto + model policy in the overage entity: Σφ == v(both) to the nano.
- C.P7 unknown cap policy → headline range spans the block and continue results.
- C.P10 volume entity → seat levers have value 0 with the renewal note; C.P11 seat-policy lever
  $190/month `needs_eval`; C.P12 seat reclaim $228.
- Reach: Auto on a team with 20% JetBrains interactions → 0.8 × the 10% saving; no activity data → point 0,
  high = full saving.
- Plan unknown (C.P13 pool months, one idle-seat finding for 10 unknown-assignment seats, one Auto finding):
  two plans; the seat lever is worth $0 in the business plan and ≤ $390 (upper bound, `needs_eval`) in the
  enterprise plan (C.P13b); `copilot.seat_downgrade` absent from both with the reason; no figure appears
  outside a scenario.
- Reach from `activity_counts` (aggregate-only bundle) equals reach from the per-user `ActivityDay`s they were
  built from.
- A trade-off lever excluded without `include_tradeoffs`; behavioral levers absent; three-lever game matches
  `shapley_exact` fractions; determinism across input permutations; `LeverResult.params` round-trip through
  `parse_aggregate_spec`.

**Size.** ~2.0k LOC including tests.
