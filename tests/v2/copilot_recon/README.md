# tests/v2/copilot_recon — CP-RECON (Copilot reconciliation and verification panels)

Wave-2b package CP-RECON: `tokenbill/copilot/recon.py` (`reconcile_copilot`, the extension's
`ChannelReconciler`) and `tokenbill/copilot/panel.py` (`build_copilot_panel`, the extension's
`panel_builder`). Addendum §12, §12.1, §13; SPEC §12, §13.1–§13.4, R8; CORE-AMENDMENTS C-17; rulings
R-E22, R-E44, R-E46.

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/copilot_recon` (also `--python 3.10`).
Coverage: `uv run --python 3.12 --extra dev coverage run -m pytest tests/v2/copilot_recon && uv run
--python 3.12 --extra dev coverage report --include='tokenbill/copilot/recon.py,tokenbill/copilot/panel.py'`
(≥ 97% at hand-off). Nightly: `-m perf` runs the 600,000-row month.

| file | covers |
|---|---|
| `world.py` | area-local builders: AI usage report rows priced from tokens with `FakePricer` (6-decimal credits), coverage aggregates, seat / Actions / sandbox lines, REST usage-summary lines, client requests with provider costs, `COST_STATE` events, the Appendix C.P1 month (`p1_world`) |
| `test_conventions.py` | the power rule: `excl` and `incl` files named blind; no cache tokens → `undecidable`, "totals only"; a small cache share → undecidable and never gated; three files → three decisions; the fallback source; `report_sources` latest-fetch rule; K-dated boundaries from facts; argument validation |
| `test_l1.py` | Auto rows at list (−10%, explained) and billed with the discount (match); pseudo rows never priced (`copilot_model_undisclosed`); the 2026-08-21 GPT-5.6 Sol boundary (`copilot_rate_boundary`) vs the same mis-billing away from a boundary (fails); compliance uplift (per org and via the run flag); long-context band; utility models; unpriced models; `gross_is_list=false` and the undecidable variant; April / May windows; provisional days and `closed_only`; rate-card error percentiles |
| `test_l0_l2.py` | C.G11 parity (zero mismatch, "published rate observed"), 1% injected mismatch, 2 × input writes, Auto in/out of nano-AIU, band hypotheses A / B (C.G5b), unpriced requests, conversation totals vs `COST_STATE`; L2 over-count (the same session from store rows and OTel spans), unobserved teams, utility calls, lagging report days, overall verdict without report data |
| `test_l3.py` | C.P1 (all three channels reconciled, `gross_is_list=true`, pool-included $26,800, unexplained 0), unclassified discounts while gross is not list, direct pool draws, the Auto tenth, C.P7 capped cost center (within / above its cap), seat proration / contract / unexplained, revision stale rows (with and without a summary), coverage above rows, summary mismatch vs `unexplained_pct`, org summaries with unattributed rows, row identity, Actions coverage and runner rates, sandbox, the `core.pool` pool month, `closed_only`, open-month revision window |
| `test_plan_fit.py` | C.P13: discount 250,000 credits and net 0 → `enterprise`; net $600 → `business`; use above the Business pool without overage; capped cost center / open month / no pool draw → `unknown`; known plan → no key; scenario pool months untouched |
| `test_panel.py` | R8: two teams × 10 days across the Opus 5.5 row of 2026-09-22 (baseline constant, actual moves) and GPT-5.6 Sol's 2026-09-04 price change; clusters (team, cost center, org), developer-days from `ActivityDay`, arms (dated, control, undated), pseudo cells at gross; `incl` files priced under their convention; `core.extensions.panel`; argument validation |
| `test_report.py` | registration as `ChannelReconciler`; `core.extensions.run_reconcilers` with rounding remainders by adapter name (R-E44) and `recon_decisions_of` (conflict-free with a RECON-shaped report; conflicting values raise); residual order; labels and notes; content-free and deterministic report (no `p_`, no canary login, permutation-invariant); record stores of other extensions ignored; hypothesis properties (valid reports on random worlds, permutation invariance, hostile arguments raise only `UsageError`) |
| `test_edges.py` | a pricer without unit rates (resolve fallback), fallback bucket rates, foreign records and `closed_only` filters on every layer, plain L0 match, window clamp |
| `test_perf.py` | reconcile a 20,010-row month (PR) and a 600,000-row month (`perf`, nightly) within budget |
| `test_gate.py` | `@pytest.mark.gate`, `importorskip`: CP-BILL / CP-ORGDATA fixture files through the real adapters and the real `RateCard` (synthetic verdicts, deterministic), C.P1 with the real `RateCard`, RECON's `merge_reports` keeping the decisions |

## Fixtures and provenance

No fixture files (`tests/v2/fixtures/copilot_recon/` stays empty). Every record is **synthetic**, built
in the tests with `core.builders` (`make_ai_usage_row`, `make_seat_line`, `make_actions_line`,
`make_license`, `make_activity`, `make_config`, `make_inference` / `make_request`) in the shapes of
addendum §4 / §5.1–§5.3 (the schemas CP-BILL parses; addendum §19.3 #1–#6). Report gross amounts are
priced from the tokens with `FakePricer` (the `facts.copilot` rate rows, R-E19 `verification:
research`). The numbers are the worked examples of Appendix C (C.G5b, C.G11, C.P1, C.P7, C.P13). No
real reports, transcripts or exports; no network. Per addendum §12.1 every verdict is printed
`… (synthetic; schema unverified)` and `mapping_verified=False` until the release gate adds
`real-redacted` fixtures (an AI usage report with token columns, the matching usage summary and a
detailed report with seat lines).

## Interpretations (documented in the module docstrings)

- **Sources.** `convention:<source_id>` uses the `source` dim of `github.ai_usage_report.coverage`
  aggregates; a day belongs to the latest-fetched coverage source; days without one use
  `convention:github.ai_usage_report` (`CONTRACT-CHANGE-CP-RECON-1.md` asks for a ruling so CP-STORE
  applies the same rule).
- **Power rule.** "Model-day" = date × model × org; a strict majority of the file's model-days must fit
  the winner within tolerance and miss the other by more than 2 × tolerance. A model-day fits when one
  of its price variants (as configured; Auto without its 10%; compliance toggled) is within tolerance,
  so Auto-at-list and compliance files stay decidable. Absolute slack 10 nano per report row (credits
  carry 6 decimals). A cache-free file is undecidable but still evaluated (excl = incl); an
  undecidable file with cache tokens is excluded from the L1 gate ("totals only") and its
  entity-months get `gross_is_list=unknown`.
- **L1 pricing** uses `Pricer.unit_rates` (point rates with modifiers, never a long-context band — a
  cell sums many requests); a card without unit rates falls back to `resolve()` with the SPEC §6.3
  bucket fallbacks. Long-context explanations need a positive gap of at most 2 × base (every band in
  §19.2 is ≤ 2 × base). Utility models (facts `utility_models`) with gross 0 are unbilled.
- **Residual amounts are signed** "reference − ours" (invoice / provider side minus our figure), so the
  Auto 10% at list is +10% at L1 and −10% as a discount class; `unexplained_nano` sums absolute
  unexplained gaps. L0 amounts (provider estimates) never enter the invoice gates; only the L0 codes
  (`copilot_rate_mismatch`, `copilot_write_1h_price`, `copilot_band_hypothesis`) are totalled.
  Revision stale rows are totalled only for months without an AI-credit summary (with one, they
  explain the summary gap instead).
- **Verdicts.** `github_copilot`: every closed, evaluated L1 row fits or is explained, row identities
  hold (`quantity × $0.01 = gross` ±1 nano, `0 ≤ net ≤ gross`), no over-count, no cost center above its
  cap, and each closed month's unexplained gaps ≤ `unexplained_pct` × that month's AI-credit + seat
  gross. The usage summary is compared per product (AI credits, seats, sandbox; Actions as "the Copilot
  share may not exceed the SKU total", because the summary also bills non-Copilot workflows). An open
  month's summary gap is `revision_window`. `github_actions` / `github_sandbox` need the summary
  (else `insufficient_data`). Overall: SPEC §12.4 over the three channels.
- **L2** compares only days the report covers (lagging report days are not over-counts).
- **Plan fit** needs a closed, final month, two scenario pool months and a seat count that is not a
  lower bound (`report_users`); "Business" needs pooled use of at least the Business pool (a pool that
  was never drawn fits neither plan).
- **Panel.** Unpriced cells count 0 in the column of the card that cannot price them (as VERIFY's
  panel); an arm without a date is treated for the whole window; developer-days come only from
  `ActivityDay` rows (0 without them).

## Unverified facts (VERIFY; shipped as labelled assumptions)

- Whether the report's `input` includes cache reads / writes (§19.5 #2) — decided per file, else
  undecidable.
- How the Auto 10% and the compliance 10% appear in gross / discount (§19.5 #3, #4) — explained either
  way.
- Report SKU grouping (§19.5 #1) — per-product summary comparison; per SKU only a diagnostic.
- Credit rounding (§19.5 #6) — 10 nano per row slack and the `copilot_rounding` residual.
- K-dated `effective_from` dates (§19.5 #24) — ±2 day `copilot_rate_boundary`.
- Seat SKU unit (seat-months) and proration (§19.5 #10) — `copilot_seat_proration` /
  `copilot_seat_contract`.
- Volume / Azure pool months (§19.5 #29) — seat gaps become `copilot_seat_contract`.
- Everything priced from `facts.copilot` carries `verification: research` (R-E19).
