### F-POOL — Copilot pool ledger and plan detection in the core (wave 1.5b)

**Goal.** Build `tokenbill/core/pool.py`, the single implementation of the Copilot pool rule (addendum R11,
DC3) and of **plan detection** (owner answer 2: the product owner does not know whether the company is on
Copilot Business or Enterprise). Turn GitHub billing records into cells; detect each entity's plan from data
with explicit precedence and conflicts; compute each pool entity's monthly pool, consumption, discount
classes, overage (with cost-center cap policies), billing mode, forecast and regime — **once per plan
scenario when the plan is unknown** (all-Business and all-Enterprise, never a guess); and convert
list-equivalent credit savings and seat changes into invoice dollars. Every Copilot package depends on these
functions. Read SPEC §0 (D6, D26, D27), §3.2–§3.5; addendum §0 (DC2, DC3, DC19, DC20, DC22, DC25), §1.1–§1.2,
§3.1–§3.2, §3.9 (CA-40), §9.2, §12 (L3), §14.1, §19.1 #9–#12, #24–#25, §19.5 #7, #10, #11, #29, Appendix
C.P1–P15 (P13–P15 added in revision 3); `CORE-AMENDMENTS.md` items C-10, C-13, C-28 (plan / SKU facts), P-1
and ruling R-E22.

**Owns.** `tokenbill/core/pool.py`, `tests/v2/pool/**`.

**Consumes (F-CORE-C only).** `core.records` (`UsageBuckets`, `UsageAggregate`, `CostLine` with `quantity`,
`cost_center`, `team`, `routing`, `speed`, `pseudo`, `workload`, `GITHUB_COST_TYPES`, `LicenseSnapshot`,
`ConfigSnapshot` incl. kinds `org_settings`, `run_flags`, `plan_quota`, `seat_counts`), `core.types`
(`PoolMonth`, `PlanEvidence`), `core.labels`, `core.money`, `core.facts` (`copilot_plans()` incl. promo,
`copilot_skus()` SKU → cost type and plan, `copilot_dates()`, `copilot_report_lag_days()`,
`copilot_plan_quota_map()`), `core.builders`. Do not import `core.catalog` (plans and SKUs come from
`core.facts`).

**Provides.** Exactly CA-40 plus plan detection: `Cell`, `entity_of`, `build_cells`, `capped_cost_centers`,
`capped_policies`, `billing_modes`, `detect_plans`, `seat_months`, `pool_credits`, `direct_draws_pool`,
`classify_discounts`, `forecast`, `pool_months`, `regime`, `overage_total`, `invoice_delta`,
`realize_credit_saving`, `realize_seat_change`. Pure, deterministic, no floats, integer nano; `Decimal` only
under `core.money.EXACT_CTX`.

**Build.**
1. `build_cells(aggregates, cost_lines, *, grain="day", convention="excl", capped={}, entity_mode=
   "enterprise")` joins on (date, team, cost_center, org, sku, model, routing, speed, pseudo) using the CA-4
   fields (never `description`); `convention="incl"` re-derives uncached from stored `excl` buckets, clamping
   negatives and returning the clamp count; `final` = every contributing row final. `convention` is the value
   CP-RECON decided (`AnalysisContext.recon_decisions` key `convention:<source_id>`, read by CP-STORE); the
   default `excl` applies only when no decision exists.
2. **`detect_plans(cost_lines, licenses, config, *, month: str, entity_mode="enterprise") ->
   list[PlanEvidence]`** — one per entity (enterprise, `org:<o>`, capped
   `cc:<n>`) with seats in the month. Sources in precedence order (first that has evidence decides; every
   other source that disagrees sets `conflict=True` and adds an evidence line):
   (1) `seat_lines` — `cost_type="seat"` lines whose SKU maps to a plan in `facts.copilot.skus`
   (`copilot_for_business` → business, `copilot_enterprise` → enterprise, `copilot_standalone` → business,
   **VERIFY**); (2) `seats_api` — `LicenseSnapshot(source_kind="github.copilot_seats")` with `plan ≠
   unknown` (latest snapshot in the month per principal and org); (3) `org_settings` — `ConfigSnapshot(kind=
   "org_settings")` attr `plan_type`; (4) `report_quota` — `ConfigSnapshot(kind="plan_quota")` rows, which CP-BILL emits only under the
   experimental flag `copilot-report-quota` (so their presence is the opt-in; `detect_plans` takes no flag)
   (month × org × `total_monthly_quota` → `n_users`) mapped by
   `facts.copilot.plan_quota_map` (1,900 / 3,000 → business, 3,900 / 7,000 → enterprise, **VERIFY**: the
   column is known only from GitHub's parser fixtures); (5) `admin_statement` — run flag `plan.<entity>`
   (answers.json or `--plan`). **Data always beats a statement**: a statement that disagrees with (1)–(4) sets
   `conflict=True` and `dq.copilot_plan_conflict`; a statement is used only when (1)–(4) are silent.
   `plan` ∈ {`business`, `enterprise`, `mixed`, `unknown`}; `seats` = plan → count known for the month
   (including `unknown` for activity-report seats); `source` ∈ {`seat_lines`, `seats_api`, `org_settings`,
   `report_quota`, `admin_statement`, `none`}.
3. `seat_months(cost_lines, licenses, config, month) -> tuple[dict[tuple[str, str], Decimal], str]`
   precedence: seat SKU cost lines (seat-months; unit **VERIFY**, document the assumption) → run flags
   `pool_seats.<entity>.<plan>` → licenses (max count in the month, plan `unknown` allowed) → `seat_counts`
   config (aggregate-only bundles) → `report_users` (distinct principals with `ai_credit.user` rows: a
   **lower bound**, noted) → `none`. `billing_modes`: seat lines → `metered`; run flag `billing_mode.<entity>`
   wins; otherwise `unknown`. Run flags are read from two `run_flags` snapshots, the CLI one (`entity_id="run"`)
   overriding the admin answers (`entity_id="admin_answers"`) per key (one shared helper
   `run_flags(config) -> dict[str, str | int | bool]`, also exported).
4. `classify_discounts` and `direct_draws_pool` exactly as CA-40 (unclassified stays unclassified);
   `gross_is_list` comes from CP-RECON's decision `gross_is_list:<entity>:<month>` (None when absent).
5. `forecast` (binding rule): observed days = days ≤ today − lag (lag = `report_lag_days`, 3); split the month
   into business days (Mon–Fri) and weekend days; for the remaining days use the nearest-rank p10 / p50 / p90
   of the **last 10 observed business days** and the **last 4 observed weekend days**; point = observed +
   Σ remaining × p50, low = observed + Σ × p10, high = observed + Σ × p90. Nearest rank: index
   `ceil(q × n) − 1` on the sorted series.
6. `pool_months(cells, cost_lines, licenses, config, *, today, promo_eligible=True, recent_estimates=(),
   gross_is_list=None, plans: Sequence[PlanEvidence] | None = None) ->
   list[PoolMonth]`: per entity × month; `plans` None → `detect_plans`. **Known plan** (no `unknown` seats):
   one `PoolMonth` with `plan_scenario=None`, `plan_source`, `plan_conflict`. **Unknown seats present:** two
   `PoolMonth`s with `plan_scenario="business"` and `"enterprise"` in which only the unknown seats vary (known
   seats keep their plan), identical consumption, each with its own pool, overage, forecast and regime and the
   note "plan unknown: scenario <x>"; never one merged figure. `consumed_report_nano` from pooled report rows;
   `consumed_estimate_nano` from `recent_estimates` (never mixed into report sums); `overage_observed_nano` = Σ
   net of pooled rows; `finality="closed"` only when the month ended and every day is final; open months get
   `forecast` and `overage_forecast` (ESTIMATED; widened by unknown cap policies). A `report_users` seat source
   makes the pool a lower bound: overage figures become ESTIMATED upper bounds with that note.
7. `pool_credits(seats, month, *, promo_eligible)` raises `UsageError` for plan `unknown` (callers split into
   scenarios first); `overage_total` returns (low, high) per the policy rules; `realize_credit_saving`
   (invoice ESTIMATED LIST + headroom ESTIMATED LIST_EQUIVALENT, note names the regime and the scenario);
   `realize_seat_change` returns None for volume / azure entities and carries the scenario in its note.

**Acceptance tests** (`tests/v2/pool/`).
- Appendix C.P1–P12 exactly (nano integers and labels): P2 $0, P3 $950, P4 $300, P5 pairs, P6 $600
  ESTIMATED, P7 continue $350 / block $0 / unknown range [$0; $350] with slack 585,000 credits, P8 $1,000 /
  $0 / $200, P9 point 3,100,000 low 2,920,000 high 3,280,000 regime overage (binding series: business days
  1–4 at 130,000 credits, the last 10 observed business days [110k, 110k, 120k, 120k, 130k, 130k, 140k, 140k,
  150k, 150k] in any order, weekend Sep 5–6 at 30,000 each and the last 4 weekend days [20k, 30k, 30k, 40k]),
  P10 None, P11 $190, P12 $228.
- **P13 (plan unknown; binding).** 2026-10 (standard month, closed, final), 100 seats from the activity report
  (plan `unknown`), pooled use 250,000 credits, metered. Two `PoolMonth`s: business — pool 190,000, overage
  60,000 credits = **$600**, regime `overage`; enterprise — pool 390,000, slack 140,000, overage **$0**,
  regime `slack`. Seat fees (count × list, ESTIMATED LIST, computed by consumers): $1,900 / $3,900.
  **P13b:** removing 10 idle seats next month at unchanged use → business: fees −$190, overage +19,000 credits
  (+$190) → saving **$0**; enterprise: fees −$390, still slack → saving **$390**; both returned, neither chosen.
- **P14 and P15 exactly as addendum Appendix C** (binding; there the quota evidence arrives as `plan_quota`
  config rows emitted by CP-BILL under the flag `copilot-report-quota`, which the addendum text calls
  `copilot-ai-usage-quota`): P14 — 50 seats, seats API `business`, seat lines `copilot_enterprise` 50
  seat-months, statement `plan.E=business` → plan `enterprise`, source `seat_lines`, `conflict=True`, pool
  **195,000**; P15 — 40 activity-report seat holders, quota 3900 for all 40 → `enterprise`, source
  `report_quota`, pool **156,000**; variants 30 × 1900 + 10 × 3900 → `mixed`, pool **96,000**; July 7000 →
  enterprise (promo quota); only 36 of 40 with a quota → seats `{enterprise: 36, unknown: 4}` and a scenario
  pair with pools **156,000** / **148,000**; no `plan_quota` rows → P13 behaviour.
- **P14b (mirror conflict).** Org A, 2026-10: seats API 40 seats `plan_type="enterprise"`; detailed report seat
  lines `copilot_for_business` 40 seat-months; run flag `plan.org:A=enterprise` → plan `business`, source
  `seat_lines`, `conflict=True`, evidence names all three, `dq.copilot_plan_conflict`; one `PoolMonth` (pool
  76,000 credits) with `plan_conflict=True`.
- **P15b (report only, seats lower bound).** AI usage report only, 2026-10, 60 distinct users with
  `total_monthly_quota=3900`: with the `plan_quota` row (month 2026-10, quota 3900, n_users 60) → plan
  `enterprise`, source `report_quota`, seats lower bound 60 (`report_users`), pool ≥ 234,000 credits, note
  "seats lower bound"; without that row → plan `unknown`, two scenarios with pools 114,000 and 234,000 (both
  lower bounds).
- `detect_plans` precedence table: every pair of sources in both orders; a statement alone → used; a statement
  plus agreeing data → no conflict; `mixed` when seat lines show both SKUs.
- `build_cells` on builder-made report rows reproduces gross / discount / net sums and user counts; `incl`
  conversion equals hand values; capped cost-center rows land on `cc:<name>`; rows differing only in `pseudo`
  stay separate cells.
- `classify_discounts`: `gross_is_list=True` and Σ discount ≤ pool → pool draw; `None` → unclassified; Auto
  rows whose discount is 10% of gross with net > 0 → "other". `direct_draws_pool`: "yes" / "no" / "unknown".
- Property tests: `invoice_delta` monotone and bounded by the saving; invoice + headroom == saving at point,
  low and high, per scenario; `realize_seat_change` ≥ 0 for removals and downgrades on metered entities;
  outputs identical under input permutation; `pool_credits` with plan `unknown` raises.
- No float literal or `float(` call (the core no-float test covers the module).

**Size.** ~2.1k LOC including tests.
