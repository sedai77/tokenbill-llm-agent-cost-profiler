# tests/v2/pool — F-POOL acceptance tests (`tokenbill/core/pool.py`)

Wave 1.5b package F-POOL: the single implementation of the GitHub Copilot pool rule (addendum R11,
DC3, CA-40) and of plan detection (R17, CA-48, ruling R-E22; CORE-AMENDMENTS P-1).

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/pool` (also `--python 3.10`).
Coverage: `uv run --python 3.12 --extra dev coverage run -m pytest tests/v2/pool && uv run --python
3.12 --extra dev coverage report --include='tokenbill/core/pool.py'` (99% at hand-off).

| file | covers |
|---|---|
| `worlds.py` | area-local builders of the Appendix C worlds (report rows, activity-report and seats-API seats, run flags, `plan_quota` / `seat_counts` / `cost_center` / `org_settings` rows, the binding C.P9 daily series) |
| `test_plans.py` | `detect_plans`: every pair of the five sources in both orders (the higher one decides, `conflict=True`, the `dq.copilot_plan_conflict` line), agreeing pairs (no conflict), each source alone, `mixed` from both seat SKUs, a `mixed` statement, `copilot_standalone` flagged as assumed, C.P13 (unknown), C.P14, P14b (org mode), C.P15 and its variants (mixed, July promo quota 7000, 36 of 40, no rows), P15b (report users with/without quota), partial seats-API coverage and latest-snapshot rule, `seat_counts` plans of aggregate-only bundles, per-org `plan_type` with the nearest-snapshot rule, statements (`pool_seats`, enterprise fallback for cost centers) |
| `test_pool_rule.py` | `pool_credits` (C.P1 pool, C.P6 promo, unknown plan raises), `regime`, `overage_total` (C.P7 continue / block / unknown, partitioned or whole pools, shared slack, independent org pools), `invoice_delta` (C.P8), `realize_seat_change` (C.P2–P4, P10, P11, P12, P13b, downgrades, promo months, open-month and unknown-cap-policy ranges), `realize_credit_saving` (C.P5, P13c, ranges, cost increases, unknown regime), `forecast` (C.P9 and edges) |
| `test_pool_months.py` | `build_cells` + `pool_months` end to end: C.P1 closed month (pool draw, overage $4,200, direct $150 with `direct_draws_pool="no"`, invoice $31,150 via `combine_weakest`), C.P9 open month (forecast and $4,200 [$2,400; $6,000] overage forecast), C.P6 promo cliff ($600 ESTIMATED), C.P7 capped cost center (slack 585,000; $350; unknown policy → [$0; $350]; block → blocked demand), C.P13 scenario pair (pools, overage $600 / $0, regimes, notes, only pool-dependent fields differ), C.P14 / P14b single conflicted month, C.P15 pools (156,000 / 96,000 / 148,000 + 156,000), P15b lower bounds (234,000; 114,000 / 234,000), seat sources, unknown pools, passed plans, `gross_is_list` mappings, estimates, month grain, org-mode inference, notes |
| `test_seats_config.py` | `run_flags` (CLI over admin answers per key), `capped_cost_centers` / `capped_policies`, `entity_of`, `billing_modes`, `seat_months` precedence (seat lines → run flags → licenses → `seat_counts` → report users → none), busiest-snapshot counting, multi-org seats |
| `test_cells.py` | `build_cells`: sums and user counts, the token split between pooled and direct rows of one aggregate key, `incl` hand values and clamps, capped cost centers → `cc:<name>`, pseudo-only differences, month grain, filters, tokens without lines; `Cell` validation; `classify_discounts` (pool draw, unclassified, Auto 10% → other); `direct_draws_pool` yes / no / unknown |
| `test_properties.py` | hypothesis: `invoice_delta` monotone and bounded; invoice + headroom == saving at point, low and high per scenario; seat removals / downgrades ≥ 0 on metered entities outside the promo; `pool_credits` raises for `unknown`; forecast bounds; outputs identical under input permutation (cells, plans, pool months) with known plans → one month and scenario pairs differing only in pool fields; fuzz of hostile configuration values (only `TokenbillError` escapes); AST no-float scan |

## Fixtures and provenance

No fixture files. Every record is synthetic and built in the tests with `core.builders`
(`make_ai_usage_row`, `make_seat_line`, `make_license`, `make_config`, `make_pool_month`,
`make_plan_evidence`); the numbers are the hand-computed worked examples of addendum Appendix C.P1–P15
and the F-POOL brief (P9's binding daily series, P13b, P14b, P15b). No real transcripts or reports.

## Interpretations (documented in the module docstring; the text left room)

- **Capped cost centers** are their own entities `cc:<name>` (their pool is the configured cap,
  `pool_target_credits`); the parent keeps the other seats (partition). `overage_total` applies the
  binding shared-pool formula (`max(0, uncapped + Σ min(use_c, cap_c) − pool)` with pool = Σ pools),
  so partitioned and whole pools give the same totals (tested).
- **Scenario overage.** In a scenario `PoolMonth`, `overage_observed_nano` = `max(0, consumed −
  scenario pool)` (the convention of F-CORE-C's `make_pool_month`); with a known plan it is Σ net of
  the pooled rows (data). The note names the observed net.
- **Plan resolution by counts.** Only the deciding (highest) source places seats; seats it cannot place
  stay `unknown`. A lower source *conflicts* when one of the two speaks for every seat and the other
  claims a plan outside it. `seat_counts` rows of aggregate-only bundles carry the seats-API plan and
  count as `seats_api` evidence. Plan statements of an `org_settings` row from
  `tokenbill.admin_answers` / `tokenbill.cli` are not org-billing data (the answers' `plan.<entity>`
  run flag is the statement). A cost center or org without its own `plan.<entity>` statement inherits
  `plan.enterprise`.
- **Seat counting.** Licenses: the snapshot date in the month with the most seats, one seat per person
  per entity (a multi-org seat is billed once); `seat_counts`: the busiest snapshot date, summary rows
  (`bucket="*"`) skipped; report users: distinct principals with `ai_credit.user` rows (a lower bound).
  Org-billing and cost-center state (`org_settings`, `cost_center`) uses the nearest snapshot / the
  latest snapshot (state pulled after a month still describes it).
- **Finality.** `closed` iff the month's last day is at least `report_lag_days` (3) before `today` and
  no day with rows is provisional; days without rows count as observed zeros in the forecast.
- **Estimates.** `recent_estimates` items are `(date, entity id or None, nano)`; None is the enterprise
  (enterprise mode only); only days without a report cell of the entity count.
- **Token split.** An aggregate key shared by pooled and direct rows splits its tokens by the rows'
  gross amounts (same model, date and routing → same rates), largest remainder.

## Unverified facts (VERIFY; shipped as labelled assumptions)

- Seat SKU `quantity` unit = seat-months (addendum §19.5 #10); proration, upfront charges and volume/EA
  seat pricing are not modeled (seat changes are ESTIMATED LIST with that note).
- `copilot_standalone` → Business (§19.5 #35; evidence line says "assumed, unverified").
- `plan_quota_map` values 1,900 / 3,000 → Business, 3,900 / 7,000 → Enterprise (promo quotas only
  2026-06 … 08) — known only from GitHub's parser fixtures (§19.5 #35).
- Whether direct-org usage draws on the pool (§19.5 #7): read from the rows' discounts, else `unknown`.
- Calendar-month pools for volume / Azure entities (§19.5 #29): noted on those pool months; their seat
  savings return None (renewal only).
- The cost-center cap policy block/continue (§19.5 #11): from `capped_policy.<cc>` run flags, else
  `unknown` → ranges.
- All Copilot facts read from `core.facts` carry `verification: "research"` (R-E19).
