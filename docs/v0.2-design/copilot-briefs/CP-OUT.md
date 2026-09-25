### CP-OUT — Copilot summary, bill / plan sections, showback, FOCUS rows (wave 2)

**Goal.** Show the Copilot bill the way a finance lead and a GitHub admin need it — seats, AI credits (gross,
classified discounts, overage, direct-org), Code Quality, Actions and sandboxes per entity and month, pool vs
consumption and forecast, the Copilot plan in invoice dollars with pool headroom beside it, and the admin
checklist — in the terminal, HTML and result JSON, per-team showback pages and FOCUS 1.4 rows, all
label-honest (R16) and k-anonymous, without double counting against ledger rows. When the plan is unknown
(owner answer 2) the bill, pool bar and plan are shown **side by side as "if Business" / "if Enterprise"**,
each ESTIMATED, with the plan status and how to find out — never one number; showback adds a VS Code vs
JetBrains split (owner answer 3). Read SPEC §3.4 (R3, R10),
§3.5, §8.4, §8.7, §14 (all), D19; addendum DC3, DC7, DC22, DC25, §1.2, §3.2 (`CopilotBillLine`, `BILL_LINES`,
`CopilotSummary`, `FocusRow`), §3.3 (`figure_json`, `combine_weakest`), §3.8 (`focus_rows` owned channels),
§14 (all), §19.5 #20; `CORE-AMENDMENTS.md` items C-13, C-20 and ruling R-E22.

**Owns.** `tokenbill/copilot/summary.py` (`assemble_summary`), `tokenbill/copilot/render.py`
(`CopilotSection`), `tokenbill/copilot/showback.py` (`render_copilot_showback`), `tokenbill/copilot/focus.py`
(`focus_rows`), `tests/v2/copilot_out/**`.

**Consumes.** `core.types`, `core.labels` (`Figure`, `figure_json`, `combine_weakest`, `exact`, `Basis`),
`core.money`, `core.kanon.publish`, `core.pool` (`build_cells`), `core.textsafe.sanitize`, `core.facts`
(`load().focus`, the FOCUS 1.4 `FocusSpec`), `core.protocols` (`LedgerStore`, `ExtRecordStore`, `SectionRenderer`), `core.testing`
(`MemoryStore`, `MemoryRecordStore`, `published_for_tests`), `core.builders`; stdlib `html`. Never import
OUT's modules.

**Provides.**
- `assemble_summary(*, cost_lines, aggregates, licenses, activity, pools, plans, plans_by_scenario, actions,
  channel_verdicts, window, k, pricer) -> CopilotSummary` (bill lines per entity × month — and per scenario
  when `PoolMonth.plan_scenario` is set, `CopilotBillLine.scenario` filled — with the §14.1 label rules;
  `plan_status` from `plans`; per-team credits via `core.kanon.publish`; seat counts per (team, plan:bucket)
  k-anonymous; `editor_split` = interactions and credits per editor family (`vscode`, `jetbrains`, `other`) via
  `publish`, rows < k merged).
- `CopilotSection` (`SectionRenderer`: `terminal`, `html`, `json`).
- `render_copilot_showback(result, out_dir, formats=("html",)) -> list[Path]`.
- `focus_rows(store, record_stores, *, since_ms, until_ms, reconciled_channels, k, allow_unreconciled, role)
  -> Iterator[FocusRow]`.

**Build.**
1. Bill lines exactly per addendum §14.1: seat lines INVOICE / EXACT LIST / ESTIMATED (count × list with the
   note); gross and pool discount LIST_EQUIVALENT; other / unclassified discounts EXACT LIST; overage and
   direct-org by R16 with open-month provisional values; adjacent lines with their own channel verdicts;
   `total.invoice` via `combine_weakest` over dollar lines only, `components` filled. **Unknown plan:** the
   known lines (gross, discounts, overage observed, direct-org, Actions, sandbox) are shared; `seats.business` /
   `seats.enterprise` for known seats plus `seats.unknown_plan` (quantity = unknown seats) are emitted once per
   scenario with `scenario` set and the unknown seats priced at that scenario's list price (ESTIMATED LIST, note
   "plan unknown: priced as <plan>"); pool, overage forecast and `total.invoice` exist once per scenario, each
   ESTIMATED; nothing is summed across scenarios.
2. Terminal (≤ 100 columns, sanitized): "COPILOT BILL" per entity × month with label chips, a pool bar (pool,
   consumed, forecast p10–p90, cap-policy range) with regime, billing mode and direct-draw status, verdict
   badges, "COPILOT PLAN" (headline, pool headroom separately, levers with Shapley, reach, tags; never a
   standalone sum), "WHAT TO CHANGE IN GITHUB" with deadlines and auth notes. A "PLAN" line precedes every
   entity block: the detected plan and its source, the conflict when any, or "unknown — shown as Business and
   as Enterprise" plus the how-to-find-out hint (seats API `plan_type`, detailed-report seat SKU
   `copilot_for_business` / `copilot_enterprise`, licensing page, `answers.json`); unknown-plan entities
   render the bill and plan as two adjacent columns (≤ 100 columns total; stacked blocks below 80).
3. HTML: one escaped `<section id="copilot">`, no scripts or external resources, inline SVG paired with a
   `<table>` with `<caption>`, WCAG AA contrast, no color-only encoding.
4. JSON: `{"pools", "lines", "teams", "seat_counts", "plan", "actions", "channel_verdicts", "evidence"}` —
   MONEY via `figure_json`, no JSON floats, `list_equivalent` never under a billed key.
5. Showback per team (published aggregates only) incl. pro-rata overage allocation labelled ESTIMATED beside
   GitHub's per-row net (R16 label); credits and dollars never summed.
6. FOCUS rows per addendum §14.3 (AI credits, seats with the Purchase/Recurring VERIFY fallback, Actions,
   sandbox); `x_DiscountPool` / `x_DiscountOther` / `x_DiscountUnclassified`, `x_CreditsQuantity`,
   `x_PriceBasis`, `x_Reconciled`; BilledCost only for reconciled channels unless `allow_unreconciled`;
   `role="enrichment"` zeroes Billed / Effective; teams < k merged. Unknown plan: seat rows are emitted only
   from seat SKU lines (invoice-side facts); scenario-priced seats never become FOCUS rows (a FOCUS row is a
   charge, not a scenario), and the export notes "seat charges unknown: plan not detected".
7. JSON additions: `"plan_status"` (per entity × month: plan, source, conflict, evidence) and, when any plan is
   unknown, `"scenarios": {"business": {...}, "enterprise": {...}}` holding the scenario lines, pools and
   plans; the top-level `"lines"` then carry only scenario-free lines.

**Acceptance tests.**
- C.P1 month with seat lines, reconciled and closed: seats and overage INVOICE, total INVOICE $31,150 +
  Actions; the same month unreconciled → EXACT LIST with "unreconciled"; without seat lines → seats ESTIMATED
  and the total ESTIMATED naming "seats"; an open month → overage LIST provisional and no INVOICE anywhere.
- Unclassified discount → `discount_unclassified` line (never "pool included"); after classification →
  `discount_pool` LIST_EQUIVALENT, never rendered in a billed column.
- Terminal golden snapshot for a two-entity fixture; HTML parses, has no `<script`, every `<svg>` has a
  sibling `<table>` with `<caption>`; JSON passes a local mirror of the SPEC §14.1 rules.
- **Plan unknown (C.P13):** two scenario blocks — business: seats 100 × $19 = $1,900 ESTIMATED, overage $600,
  `total.invoice` $2,500 ESTIMATED; enterprise: seats $3,900, overage $0, total $3,900 ESTIMATED; the PLAN line
  and the hint appear before the first number; the terminal golden has both columns; JSON has
  `scenarios.business` / `scenarios.enterprise` and no scenario figure at top level; FOCUS has no seat rows.
- Plan known with conflict (C.P14): one block, the PLAN line names the conflict and the winning source.
- Showback: team of 3 merged; pro-rata allocation sums to the entity overage to the nano (per scenario when
  unknown); the editor split shows VS Code and JetBrains rows for teams ≥ k and merges the rest.
- FOCUS: ListCost − discounts = BilledCost per AI-credit row; unreconciled channel without
  `allow_unreconciled` → no rows and the channel named; enrichment → Billed / Effective 0.
- Gate: OUT's renderers include the section via `core.extensions.render_sections`; `write_focus` on a real
  `SqliteStore` with Copilot collector lanes and report rows emits no ledger rows on Copilot channels and
  totals equal the report lines.

**Size.** ~2.9k LOC including tests (at the limit: the unknown-plan rendering reuses the per-entity block
renderer once per scenario instead of a second layout).


---
**ORCHESTRATOR ADDITIONS (binding):** SPEC Appendix E.7 R-E47 (print "users unknown" via core.kanon.row_notes).
