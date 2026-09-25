# tests/v2/copilot_det_seats — CP-DET-SEATS acceptance tests

`copilot.seats-budgets` (addendum §10.1; brief CP-DET-SEATS) at the registry path
`tokenbill.detect.copilot_seats:CopilotSeatsBudgets`: an aggregate detector (`aggregate=True`,
`extension="copilot"`, `families={"copilot"}`, `requires=frozenset()`) that ignores lanes and reads
`ctx.plans`, `ctx.pools`, `ctx.licenses`, `ctx.activity`, `ctx.config`, `ctx.cost_lines` and
`ctx.reconciled_channels`. Every pool figure comes from `core.pool` (the pool months themselves,
`realize_seat_change` for seat projections, `detect_plans` when the enricher supplied no plans).

| kind | scope (besides `product: copilot`) | count source | figures |
|---|---|---|---|
| `plan-status` | `entity` | entity | seat fees: seat lines (R16) or count × list (ESTIMATED LIST); unknown seats as a range [n × $19; n × $39] with no point |
| `pool-regime` | `entity`, `plan_scenario` | entity | overage: INVOICE (closed + reconciled), EXACT LIST "unreconciled" / "provisional", ESTIMATED in a scenario; pool, consumption (LIST_EQUIVALENT), direct net, regime, forecast in evidence |
| `overage-forecast` | `entity`, `plan_scenario` | entity | `PoolMonth.overage_forecast` (ESTIMATED LIST p10–p90, widened by an unknown cap policy) |
| `promo-cliff` | `entity`, `plan_scenario` | entity | latest promo month's use against the later standard pool, ESTIMATED "at unchanged use"; suppressed after 2026-11-30 |
| `idle-seat` | `entity`, `team`, `plan_scenario` | licenses | seat fees count × list (ESTIMATED LIST); `realize_seat_change` for removable seats; unknown assignment → the same projection as an ESTIMATED upper bound, `needs_eval` |
| `seat-auto-assign` | `entity`, `org`, `plan_scenario` | licenses | idle seat fees; projection ESTIMATED, `needs_eval`, trade-off |
| `completions-only-seat` | `entity`, `team` | licenses | count only |
| `plan-mix` | `entity`, `team`, `plan=enterprise` | licenses | $20 × n (ESTIMATED LIST); `realize_seat_change(enterprise→business)` |
| `duplicate-seat` | `entity`, `team` | licenses | count only |
| `budget-paid-usage-uncapped` | `entity`, `plan_scenario` | entity | exposure = overage forecast p90 (ESTIMATED LIST) |
| `budget-stop-usage-off` | `entity` | entity | count only |
| `budget-zero-user-budget` | `entity` [, `team` when ≥ k] | licenses | count only; never a `p_` |
| `budget-ulb-gap` | `entity`, `plan_scenario` | entity | Σ user-level caps − pool − Σ metered budgets (ESTIMATED LIST) |
| `budget-org-multi-org-seats` | `entity` | entity | count only |
| `budget-no-cost-center-pool` | `entity`, `cost_center`, `plan_scenario` | cost_lines | excess draw over the licences' allowance (ESTIMATED LIST_EQUIVALENT) |
| `budget-enterprise-misread` | `entity=enterprise` | entity | count only |
| `dq.skipped-kinds` | `entity` (when one) | entity | none (data-quality, R-E1) |

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/copilot_det_seats` (also `--python 3.10`).
Coverage: `uv run --python 3.12 --extra dev coverage run -m pytest tests/v2/copilot_det_seats &&
uv run --python 3.12 --extra dev coverage report --include='tokenbill/detect/copilot_seats.py'` —
98% at hand-off (the lines left are defensive branches).

| file | covers |
|---|---|
| `test_seats_appendix_c.py` | C.P2 / P3 / P4 through the detector (50 idle removable Business seats → $0 / $950 / $300, the finding kept in overage with count and regime), P10 (volume → projection None, renewal date from the run flag), P11 (`assign_all` → `seat-auto-assign` $190 `needs_eval`, removable 0; stated vs pulled seat policy), P12 (removable 12 → $228, team-assigned 8 → None), teams, plan prices, the seat-fee `min_usd` gate |
| `test_plan_scenarios.py` | C.P13 / P13b via the real `core.pool` (two `pool-regime` findings: Business $600 ESTIMATED, Enterprise slack $0; two `idle-seat` projections $0 / $390, ESTIMATED upper bounds, `needs_eval`; one `plan-status` "unknown" with fees [$1,900; $3,900]; no `plan-mix`; no finding without `plan_scenario` carries a pool figure), seat age from two reports, activity-report seats under a stated `assign_all`; C.P14 (conflict named, seat lines won, fees from the seat lines, INVOICE when reconciled, one scenario-free `pool-regime`); known plans, the `detect_plans` fallback, earlier months, provisional seat lines |
| `test_pool_kinds.py` | `pool-regime` labels: C.P1 closed + reconciled → INVOICE, unreconciled → EXACT LIST, C.P9 open month → LIST provisional plus the ESTIMATED forecast (2,920,000 / 3,100,000 / 3,280,000 credits; overage $4,200 [$2,400; $6,000]); `overage-forecast` and its p90 gate; C.P7 unknown cap policy widens the range ([$0; $350] vs [$350; $350]); C.P6 `promo-cliff` $600/month ESTIMATED, expiry on 2026-11-30 (injected clock), per scenario; regime texts; report-user seats as upper bounds |
| `test_budgets.py` | $0 user budgets: 5 in one team → team scope, 3 → entity scope only, mixed teams and a universal $0 budget, no `p_` anywhere, `kanon` publication; stop-usage off; paid usage uncapped (exposure = p90; capped by a stopping budget, a `budget_stop` statement or the paid-usage policy); user-level-cap gap per scenario and with cost-center caps; org budgets with multi-org seats; cost-center pool off (211% draw → $210 list-equivalent; not at 150%; not with the pool on; suppressed for volume billing with a dq note); enterprise budget misread (every scenario) |
| `test_gating.py` | kind tables; pools + config without licenses → pool and budget kinds run, seat kinds skipped in one `dq.skipped-kinds`; aggregate-only bundle → `idle-seat` per team from `seat_counts` (removable, team-assigned, unknown with caveats; pending / young rows dropped), `plan-mix` / `completions-only-seat` / `duplicate-seat` skipped; plans only → everything else skipped (exempt dq finding); org-mode dq scope; registry path and `run_detectors` phases (`aggregates_only`, no `ext:copilot` → silent) |
| `test_seat_kinds.py` | idle criteria (pending cancellation, seat age, activity bucket, reported cost incl. metrics estimates, zero cost unverified), unknown org policy → unknown assignment, multi-org seat counted once, seats API over the activity report, unknown-plan seats in single-plan and mixed entities, no pool month → unpriced; `completions-only-seat` (Enterprise advice; no plan advice while unknown; Business); `plan-mix` (3 closed months, heavy users, metrics estimates, a 2-month history with low confidence, idle seats left to `idle-seat`, suppressed while the plan is unknown) |
| `test_conformance.py` | `assert_detector_conforms` (aggregate lane independence, determinism) on three worlds; identical findings across input permutations; `core.kanon.rescope_findings` never merges scenarios and scrubs a small team; entity kinds exempt (R-E16); the content canary; the self-view context |
| `test_properties.py` | hypothesis (60 examples per run; 3,000 run once at hand-off): random seats, activity, malformed budget / seat-count / org-settings / run-flag values, report rows, pool months and plan evidence → no exception, conformance, no `p_`, scenario dims only on scenario kinds with ESTIMATED figures, `kanon` publication without a privacy error, permutation invariance |
| `test_plant_table.py` | the addendum §18 seat and budget plants (platform removable 6, plan-mix 4, $0 budgets 5; infra team-assigned 5; ops `assign_all` 5) on a builder-made world shaped like CP-SYNTH's, through the gate test's enrichment and assertions |
| `test_gate_synth.py` | **gate** (`importorskip("tokenbill.synth.copilot_world")`, and CP-SYNTH-W / CP-STORE for the adapter path): the CP-SYNTH world's plants from canonical records and through the real adapters, `core.testing` stores and the Copilot enricher; the `plan_unknown` variant's scenario pairs (truth values compared when `CopilotWorld.truth` carries them) |

## Interpretations (brief > addendum §10.1 wording; documented here for review)

1. **No month dim.** `plan-status` and `pool-regime` describe each entity's **latest** month; earlier
   months are evidence items (`month:<YYYY-MM>`). A month dim is not in the brief's scope-dim list
   and `core.kanon`'s Copilot parent chain would drop it and merge months into one figure.
2. **Gating by inputs** (brief Build 1): `KIND_REQUIRES` maps each kind to alternative sets of input
   names (`pools`, `plans`, `licenses`, `seat_counts`, `activity`, `cost_lines`, `budgets`,
   `cost_centers`, `org_settings`), not capabilities; the dq kind is `dq.skipped-kinds` (brief), not
   the addendum's `copilot.<detector>/skipped-kinds`. `seat-auto-assign` also needs an org seat
   policy; `budget-no-cost-center-pool` also needs licenses (the cost center's seats) and is
   suppressed with a dq entry for volume / azure / unknown billing.
3. **Assignment unknown** covers activity-report seats (`assigned_via_team is None`) and directly
   assigned seats in an org whose seat policy is not known; `assign_all` orgs make every seat
   auto-assigned. When a team has unknown-assignment seats, `recoverable` is the projection for
   removable + unknown seats (ESTIMATED upper bound, `needs_eval`); the removable-only projection is
   the `assignment:removable` evidence item.
4. **Seat age** without `seat_created`: "listed ≥ 30 days earlier in another report", else counted
   with `needs_eval` and the caveat. **Zero cost** is checked against report rows and metrics
   estimates; with neither loaded it is counted as unverified (`needs_eval`).
5. **plan-status fees** for unknown seats are a range without a point (`nano=None`, note
   "unpriced: plan unknown …"): a point would pick a scenario (R17).
6. **plan-mix** evaluates the last 3 closed months; with a 2-month history it runs with
   confidence `low` and says so (the §18 world has only July and August closed on 2026-09-23);
   idle Enterprise seats are left to `idle-seat`.
7. **Budgets**: Copilot budgets are those whose `sku` names AI credits or Copilot, or have no
   `sku` (**VERIFY** against real budget exports); metered scopes are enterprise / organization /
   cost_center / repository; user-level scopes are user / multi_user_customer /
   multi_user_cost_center. The user-level-cap check applies only when every seat has a cap (a
   universal budget, or individual + cost-center budgets covering all seats; overlaps
   approximated). `budget-enterprise-misread` fires only when the budget is below the seat fees in
   every scenario. `budget-zero-user-budget` publishes a team count only when it is ≥ k and puts
   the rest (and multi-user $0 budgets) at entity level.
8. **Count-only kinds** carry `cost_observed = unpriced("count only …")` (no dollar figure).
9. **Deadlines**: metered → before the next 1st 00:00 UTC after `ctx.now_ms` (or the latest data
   date); volume / azure → the `renewal_date.<entity>` run flag, else "renewal date unknown".

## Fixtures and provenance

No fixture files: every record is synthetic and built in code with `core.builders` (`helpers.py`);
pool months come from `core.builders.make_pool_month` (Appendix C states) or from the real
`core.pool` on builder-made AI usage report rows, seat lines, licenses and configuration. No real
exports, logins or content.
