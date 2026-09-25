### CP-DET-SEATS — `copilot.seats-budgets`: plan status, pool regime, forecast, promo cliff, seats, budgets (wave 2)

**Goal.** Tell a Copilot admin — and a product owner who only holds an export bundle — per billing entity and
month: which plan the entity is on and how we know (or that it is unknown, in which case every pool figure is
shown for both plans), whether it is inside its prepaid AI-credit pool or paying overage, what the month will
cost, which seats are idle and which of those a GitHub setting can actually remove, and where budgets are
missing or misread — with invoice dollars only where the pool rule and the billing mode allow them. Works on
every handoff shape: API recordings, UI files (activity report seats with unknown plan and assignment),
and aggregate-only bundles (seat counts, no per-person rows). Read SPEC §3.5, §3.6, §3.22, §8.4–§8.5, §10.1;
addendum DC3, DC7, DC19, DC20, DC25, §1.2 (R11–R16), §3.9, §8.2, §10.0, §10.1, §19.1 #3, #9–#12, #24–#27, §19.5
#10, #11, #25, #27, #28, Appendix C.P1–P12; `F-POOL.md` (P13–P15); rulings R-E16, R-E20, R-E22.

**Owns.** `tokenbill/detect/copilot_seats.py` (`CopilotSeatsBudgets`, id `copilot.seats-budgets`),
`tests/v2/copilot_det_seats/**`.

**Consumes.** `core.pool` (`realize_seat_change`, `regime`, `overage_total`), `core.findings` (`build_finding`,
`finding_id`, `make_scope`, `min_usd_gate`), `core.catalog` (`levers_for_kind(…, family="copilot")`,
`fix_for`, `ADMIN_ACTIONS`, `COUNT_SOURCE`, `CONFIG_KEYS`, `copilot_allowance`), `core.labels`, `core.types`
(`PoolMonth` with `plan_scenario`, `PlanEvidence`), `core.records`, `core.facts` (plans, dates),
`core.testing` (`assert_detector_conforms`), `core.builders`.

**Provides.** A registered detector with `aggregate=True`, `extension="copilot"`, `families={"copilot"}`,
**`requires=frozenset()`** (it runs whenever `ext:copilot` is present; each kind group checks its own inputs,
see Build 1) and kinds: `plan-status`, `pool-regime`, `overage-forecast`, `promo-cliff`, `idle-seat`,
`seat-auto-assign`, `completions-only-seat`, `plan-mix`, `duplicate-seat`, `budget-paid-usage-uncapped`,
`budget-stop-usage-off`, `budget-zero-user-budget`, `budget-ulb-gap`, `budget-org-multi-org-seats`,
`budget-no-cost-center-pool`, `budget-enterprise-misread`, `dq.skipped-kinds`.

**Build.**
1. **Gating per kind group** (replaces `requires={"licenses"}`, which switched off pool and budget kinds for
   a no-token handoff without a seats list): pool kinds (`pool-regime`, `overage-forecast`, `promo-cliff`)
   need `ctx.pools`; budget kinds need `ctx.config` budget / cost-center / org-settings snapshots; seat kinds
   need `ctx.licenses` or `seat_counts` config rows; `plan-status` always runs. Each skipped group adds to one
   `dq.skipped-kinds` finding (category `data-quality`, entity scope, no dollars) naming the missing input.
2. **`plan-status`** (info, entity scope, count source `entity`): one per entity × month from `ctx.plans` —
   detected plan with source and evidence lines, or "plan unknown: both scenarios shown" with the
   how-to-find-out steps (seats API `plan_type`, seat SKU in the detailed usage report, licensing page,
   `answers.json`), or the conflict and which source won. Fix: `admin:plan_confirm`.
3. **Scenarios (R-E22).** `ctx.pools` holds two `PoolMonth`s per entity × month when the plan is unknown.
   `pool-regime`, `overage-forecast`, `promo-cliff`, `idle-seat` / `seat-auto-assign` projections and
   `budget-paid-usage-uncapped` / `budget-ulb-gap` are emitted **once per scenario** with scope dim
   `plan_scenario=business|enterprise`, labelled ESTIMATED, titles prefixed "If Business:" / "If
   Enterprise:"; never one merged figure. `plan-mix` and the "Business instead of Enterprise" text of
   `completions-only-seat` are suppressed while the entity's plan is unknown or `mixed`-unknown.
4. **Labels (R16).** Overage / direct net INVOICE only for closed, final, reconciled months
   (`ctx.reconciled_channels`, `PoolMonth.finality == "closed"`), else EXACT LIST with "unreconciled" /
   "provisional"; seat fees for specific seats are count × list, ESTIMATED LIST with the proration / upfront /
   volume note; forecasts and promo-cliff ESTIMATED ("at unchanged use"). Bases follow R-E20 (credits
   LIST_EQUIVALENT in `cost_observed` or `headroom`, dollars LIST / INVOICE; never added in one figure).
   Categories: seat kinds `lever`, all others `aggregate` (k-anonymity is decided by the count source, R-E16).
5. **`idle-seat`** splits per team: **removable** (direct assignment in an `assign_selected` org),
   **team-assigned**, **auto-assigned** (`assign_all` org) and **assignment unknown** (`assigned_via_team is None`, activity-report
   seats or aggregate-only counts). Removable seats get `realize_seat_change` per scenario; unknown-assignment
   seats get the same projection as an ESTIMATED upper bound with `needs_eval=True` and the note "assumes direct
   assignment in an assign_selected org; confirm with the seats API `assigning_team`"; team-assigned → None;
   volume / azure → None with the renewal note; deadline per billing mode. Criteria per addendum §10.1; with
   `seat_counts` rows (aggregate-only bundles) the per-seat criteria were pre-evaluated by the exporter
   (`bucket`, `pending_cancellation`, `created_over_30d`, `zero_cost_30d`). `seat-auto-assign` per org with
   `assign_all`.
6. Budget kinds from `ConfigSnapshot` exactly as addendum §10.1; `budget-zero-user-budget` counts per team (k)
   or at entity level — never a `p_`; cost-center kinds only for metered entities.
7. Scope dims: `product: copilot`, `entity`, `team` / `cost_center`, `plan`, `bucket`, `plan_scenario`; count
   sources per `COUNT_SOURCE` (`plan-status` and `dq.skipped-kinds` → `entity`); fixes from `fix_for` and
   `ADMIN_ACTIONS` (REST request text uses placeholders, never names); expiry (`promo-cliff` 2026-11-30).

**Acceptance tests** (builders, FakePricer, no store).
- Appendix C.P1–P4 through the detector: 50 idle removable Business seats → invoice $0 in P2's regime
  (finding still emitted with count and regime note), $950 in P3, $300 in P4; P10 volume → projection None;
  P11 `assign_all` → `seat-auto-assign` $190, `idle-seat` removable 0; P12 → removable 12 ($228),
  team-assigned 8 (None).
- **P13 / P13b** (plan unknown, activity-report seats): two `pool-regime` findings (`plan_scenario` business:
  overage $600 ESTIMATED; enterprise: slack, $0), two `idle-seat` projections for 10 unknown-assignment seats
  ($0 and $390, ESTIMATED upper bounds, `needs_eval`); one `plan-status` "unknown"; no `plan-mix`; no finding
  without a `plan_scenario` dim carries a pool figure.
- **P14** → `plan-status` names the conflict and that seat lines won; one scenario-free `pool-regime`.
- `pool-regime` labels: a closed, final, reconciled month → overage INVOICE; the same month unreconciled →
  EXACT LIST "unreconciled"; the open month → LIST provisional plus an ESTIMATED forecast (C.P9 range).
- `promo-cliff` (C.P6) → $600/month ESTIMATED; not emitted after 2026-11-30 (injected clock).
- Unknown cap policy on a capped cost center → `overage-forecast` range widened per C.P7.
- `budget-zero-user-budget` with 5 $0 budgets in one team → count 5 at team scope; with 3 → only at entity
  scope; no `p_` in any scope or evidence.
- Gating: a context with pools and config but no licenses (UI files without a seats list) → pool and budget
  kinds emitted, seat kinds skipped with one `dq.skipped-kinds`; an aggregate-only context (seat counts only)
  → `idle-seat` per team from counts, `plan-mix` / `completions-only-seat` / `duplicate-seat` listed as skipped.
- `assert_detector_conforms` (aggregate lane-independence); identical findings across input permutations.
- Gate: CP-SYNTH world through real adapters, record store and enricher → platform / infra / ops seat plants and
  budget plants recovered exactly; the `plan_unknown` variant → both scenario findings with the truth values.

**Size.** ~2.4k LOC including tests.
