# CP-OUT tests — Copilot summary, section renderer, showback, FOCUS rows

Package CP-OUT owns `tokenbill/copilot/summary.py` (`assemble_summary`), `tokenbill/copilot/render.py`
(`CopilotSection`), `tokenbill/copilot/showback.py` (`render_copilot_showback`) and
`tokenbill/copilot/focus.py` (`focus_rows`).

## Test files

| file | what it checks |
|---|---|
| `test_summary.py` | bill lines and labels (addendum §14.1, R16): C.P1 closed + reconciled → INVOICE $31,150 + Actions; unreconciled → EXACT LIST "unreconciled"; no seat lines → seats ESTIMATED, total naming the seats; open month → provisional LIST overage, no INVOICE; DC22 discounts (unclassified / pool / Auto other); C.P13 scenario blocks (Business $1,900 + $600 = $2,500; Enterprise $3,900 + $0 = $3,900, ESTIMATED, never combined); C.P14 conflict (one block, seat-line fees); k-anonymous teams / seat counts / editor split |
| `test_render.py` | terminal goldens (`snapshots/terminal_two_entities.txt`, `snapshots/terminal_p13.txt`), width ≤ 40 … 132, stacked scenarios below 80 columns, PLAN line and hint before the first `$`, conflict wording, sanitizing, HTML rules (no script, no URL scheme, balanced tags, every `<svg>` followed by a `<table>` with `<caption>`), two-column plan-unknown table, JSON rules (local mirror of SPEC §14.1 in `checks.py`), scenario figures only under `scenarios`, the `core.extensions` host path |
| `test_showback.py` | team of 3 merged, pro-rata overage allocation summing to the entity overage to the nano (per scenario), editor split rows, labels, CSV / JSON / HTML formats, users unknown (R-E47), canary absence, WCAG AA palette contrast |
| `test_focus.py` | ListCost − discounts = BilledCost per AI-credit row, pool classification with `gross_is_list`, unreconciled channel → no rows and the channel named (notes, `skipped_channels`, log), `allow_unreconciled`, `role="enrichment"`, k-merge with complementary suppression and `x_SuppressedUsers`, org-level row for identities below k, unknown plan → no seat rows and the note, seat VERIFY fallback, cross-check sources excluded |
| `test_properties.py` | hypothesis over random record worlds: summary + three renderers + allocation + FOCUS balance + showback files; only `TokenbillError` may escape (CP-OUT owns no parser — its inputs are records) |
| `test_gate.py` (`@pytest.mark.gate`) | OUT's `render_terminal` / `render_html` / `to_result_json` include the section via `core.extensions.render_sections` (OUT's own `validate_result_json` / `rule_violations`); `write_focus` on a real `SqliteStore` with Copilot collector lanes and report rows writes no ledger row on the Copilot channels and its Copilot BilledCost / ListCost totals equal GitHub's lines |

Regenerate goldens with `TOKENBILL_UPDATE_SNAPSHOTS=1`.

## Fixtures and provenance

All fixtures are synthetic and built in code (`worlds.py`) with the `core.builders` Copilot
builders (`make_ai_usage_row`, `make_seat_line`, `make_actions_line`, `make_license`,
`make_activity`, `make_config`); no real billing data, no real logins (principals are
`make_principal` fixture pseudonyms, teams are made-up labels). The worlds reproduce the addendum's
hand-computed examples: Appendix C.P1 (1,000 Business + 200 Enterprise seats, 3,100,000 pooled
credits of which 2,680,000 discounted, 15,000 direct review credits), C.P13 (100 activity-report
seats of unknown plan, 250,000 pooled credits) and C.P14 (seat lines Enterprise vs seats API
Business vs an admin statement). Pools and plan evidence come from the real `core.pool`
(`build_cells`, `pool_months`, `detect_plans`). The canary (`core.builders.CANARY`) is planted in
`CostLine.description`, a field no CP-OUT output reads.

## Unverified facts (shipped as documented fallbacks)

- **FOCUS values for seat purchases** (addendum §19.5 #20): `ChargeCategory=Purchase`,
  `ChargeFrequency=Recurring` could not be re-verified against the FOCUS 1.4 specification (network
  egress to focus.finops.org is blocked in the build environment, 2026-09-25). Seat and Code Quality
  licence rows use the fallback `Usage` / `Usage-Based` with a note in `x_Notes`;
  `tokenbill.copilot.focus.SEAT_CHARGE_VERIFIED = True` switches to `Purchase` / `Recurring`.
- **`PricingUnit` / `ConsumedUnit` strings** `AI Credits`, `Seats`, `Licenses`, `Minutes`, `Units`
  (§19.5 #20) — not checked against a FOCUS allowed-values list.
- **Display names** `GitHub Copilot`, `GitHub Actions`, `GitHub Copilot cloud sandboxes` and the
  provider / invoice issuer `GitHub` in FOCUS rows — display names, not billing facts.
- Seat list prices ($19 / $39) and included credits come from `core.facts` (`verification:
  "research"`, R-E19); editor families of `ide:*` keys from `core.catalog.editor_family` (JetBrains
  patterns `verified: false`).

## Contract notes (code beats prose)

- `PlanEvidence` has no `hint` field and `CopilotSummary` no `scenarios` field (C-13 vs addendum
  CA-14): CP-OUT renders the hint (`summary.PLAN_HINT`) and derives the scenarios from the pools'
  `plan_scenario` and the keys of `plans_by_scenario`.
- `assemble_summary(..., plans_by_scenario=...)` takes CP-PLAN's `plan_copilot_scenarios` result
  (`(("known", plan),)` or one plan per scenario); `plans` are `PlanEvidence` records. `pricer` is
  accepted for the hook signature and unused (every amount is GitHub's own line or a `core.pool`
  figure). Optional keywords have defaults (a superset of the brief's signature).
- `focus_rows` returns a `FocusRowList` (a `list` of `FocusRow` with `notes` and
  `skipped_channels`), which satisfies the `ExtensionSpec` "Iterable[FocusRow]" hook; it accepts an
  additive `gross_is_list` keyword. See `CONTRACT-CHANGE-CP-OUT.md` for the two host gaps (decisions
  not passed to the hook; hook notes dropped by the host).
- Discount lines are emitted once per scenario (not shared) when the two scenarios classify the
  discounts differently (DC22's "Σ discount ≤ pool" test depends on the scenario's pool). While a
  plan is unknown the observed pooled net (GitHub's own per-row net) is one shared
  `ai_credits.overage` line; the plan-dependent overage is one ESTIMATED line per scenario, and
  only the latter enters that scenario's `total.invoice`.
- Small teams (1 … k−1 people) are merged into one `(other: <k users)` group **before**
  `core.kanon.publish`, so a single small team does not force the complementary suppression of a
  large team; `publish` still applies its full rule to the result.
- The showback's "credits per active developer-month p50 / p90" are nearest-rank percentiles of the
  team's published month cells (interactive workload, cells ≥ k users), weighted by developers —
  never a single person's value (the summary carries only published aggregates).
