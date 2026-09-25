### CP-RECON — Copilot reconciliation (ledger gate) and verification panels (wave 2)

**Goal.** Make the Copilot numbers trustworthy: reconcile our rate card and ledger against GitHub's AI usage
report and billing usage API per entity, cost center, month, SKU / product and model, for channels
`github_copilot`, `github_actions` and `github_sandbox`; decide the report's token convention with a power
rule instead of assuming it; classify discounts from data; and build the team × day panels VERIFY uses to
measure Copilot levers. Read SPEC §3.5 (`ReconRow`, `ChannelVerdict`, `ReconciliationReport`, `PanelRow`),
§12 (all), §13.1–§13.4, R8; addendum DC3, DC4, DC15, DC22, §1.1–§1.2, §3.4 (CA-23 `ChannelReconciler`), §3.9,
§12 (all, incl. §12.1), §13, §19.5 #1–#7, #10, #17, #24, #29, Appendix C.P1–P12, G11; `F-POOL.md` (P13–P15);
`CORE-AMENDMENTS.md` item C-17 and ruling R-E22.

**Owns.** `tokenbill/copilot/recon.py` (`reconcile_copilot`), `tokenbill/copilot/panel.py`
(`build_copilot_panel`), `tests/v2/copilot_recon/**`, `tests/v2/fixtures/copilot_recon/**`.

**Consumes.** `core.pool` (`build_cells`, `classify_discounts`, `direct_draws_pool`, `pool_months`,
`invoice_delta`), `core.protocols` (`Pricer`, `LedgerStore`, `ExtRecordStore`), `core.types`,
`core.records`, `core.labels`, `core.money`, `core.catalog` (`runner_rate`, `copilot_cost_type`),
`core.facts` (K-dated rate-change dates via `copilot_rates()`), `core.testing` (`FakePricer`,
`MemoryStore`, `MemoryRecordStore`), `core.builders`. Never import CP-BILL / CP-ORGDATA modules (fixtures
of other areas only in gate tests).

**Provides.**
- `reconcile_copilot(ledger, record_stores, pricer, *, since_ms, until_ms, tolerance_pct, unexplained_pct,
  closed_only, today, rounding_remainders=None) -> ReconciliationReport` (the `ChannelReconciler`; one
  report with the three channels). Its reusable decisions go into `ReconciliationReport.decisions` — the
  only carrier the enricher reads (through `core.extensions.recon_decisions_of`):
  `convention:<source_id>` → `excl|incl|undecidable` per AI usage report file,
  `gross_is_list:<entity>:<YYYY-MM>`
  → `true|false|unknown`, and the diagnostic `plan_fit:<entity>:<YYYY-MM>` → `business|enterprise|unknown`.
- `build_copilot_panel(store, record_stores, *, cluster_kind="team", since, until, baseline_pricer,
  actual_pricer, arms) -> list[PanelRow]`.

**Build.**
1. L0 parity per request and per conversation (COST_STATE `copilot.otel.invoke_agent` totals), with the
   diagnostics `copilot_auto_discount`, `copilot_write_1h_price`, `copilot_band_hypothesis`; residual
   `copilot_rate_mismatch`.
2. L1 per (day, model, sku, routing) cells excluding pseudo cells, both conventions, the **power rule** (Σ
   read + write ≥ 5% of input; winner within tolerance; loser > 2 × tolerance; else `undecidable`),
   `gross_is_list`, boundary days ±2 of K-dated rate changes → `copilot_rate_boundary` (excluded from the
   tolerance test), Auto / compliance / long-context residuals, `copilot_rounding` from
   `rounding_remainders`.
3. L2 token coverage with the SPEC over-count rule.
4. L3 identities per entity × month: row identity; DC22 discount classes (`copilot_discount_unclassified` →
   `copilot_pool_included`); `copilot_direct_pool_draw`; Σ net vs usage summary **per product** (per SKU
   only as a diagnostic); capped cost centers vs `target_amount`; seats vs count × list
   (`copilot_seat_proration`, `copilot_seat_contract` for volume / azure); Actions and sandbox lines vs the
   summary → `github_actions` and `github_sandbox` verdicts; revision check against coverage aggregates →
   `copilot_revision_stale_rows`.
5. Residual order: `revision_window` → `copilot_directional_report` → `copilot_preview_columns` →
   `copilot_rate_boundary` → `copilot_discount_unclassified` / `copilot_pool_included` →
   `copilot_direct_pool_draw` → `copilot_model_undisclosed` → `copilot_auto_discount` →
   `copilot_compliance_uplift` → `copilot_long_context_band` → `copilot_utility_unbilled` →
   `copilot_unattributed_org` → `unobserved_traffic` → `copilot_seat_proration` / `copilot_seat_contract` →
   `copilot_revision_stale_rows` → `copilot_rounding` → `copilot_plan_inferred` (informational, amount 0) →
   `unexplained`.
6. Verdicts per addendum §12; `mapping_verified=False` and `dq.recon_schema_unverified` until the release
   fixtures exist; synthetic inputs print "(synthetic; schema unverified)".
7. **Plan-fit diagnostic (owner answer 2).** For an entity whose plan is unknown (two scenario pool months),
   compare the observed pooled discounts with each scenario's pool: when Σ discount of a closed, final month
   exceeds the Business pool by more than the tolerance, only Enterprise fits (`plan_fit=enterprise`); when
   pooled usage exceeded the Business pool yet overage net stayed 0, likewise; when overage net > 0 while usage
   is below the Enterprise pool, only Business fits; otherwise `unknown`. Residual `copilot_plan_inferred`
   (nano 0, informational) and the decision key — **never** used to pick a scenario or a label: the detectors and the
   summary keep both scenarios and only print "the discounts are consistent with <plan>; confirm with the
   admin".
8. Panels per addendum §13. No floats; deterministic ordering.

**Acceptance tests.**
- Builder-made month for Appendix C.P1 (report rows, seat lines, usage summary): identities hold; discounts
  classified pool-included only when `gross_is_list` is true; `unexplained` 0; verdicts `reconciled` for all
  three channels.
- Convention power rule: a report generated under `excl` is decided `excl`; the same totals generated under
  `incl` are decided `incl` (blind); a report without cache tokens is `undecidable` and never "reconciled"
  beyond "totals only".
- Auto rows at list show −10% and are explained; pseudo rows never enter L1; a cell on 2026-08-21 (±2 days of
  a K-dated GPT-5.6 Sol change) is `copilot_rate_boundary`, not a failure.
- L0: Appendix C.G11 → zero mismatch; 1% injected mismatch → `copilot_rate_mismatch`; requests with writes
  priced at the published rate raise the `copilot_write_1h_price` diagnostic "published rate observed".
- L2: the same session from store rows and OTel spans with different ids → over-count, `not_reconciled`.
- P7 capped cost center and a seat proration case give the named residuals; a revised export whose later
  file lacks a row → `copilot_revision_stale_rows`.
- Panel: two teams × 10 days across the Opus 5.5 row of 2026-09-22 keep the baseline constant while
  `cost_actual_nano` moves (R8).
- Decisions: an `excl` file and an `incl` file yield `convention:<id>=excl` / `…=incl`, the undecidable file
  `…=undecidable`; `gross_is_list:enterprise:2026-09=true` after the C.P1 month; `recon_decisions_of` over
  this report and a RECON report is conflict-free.
- Plan fit: C.P13 with pooled discount 250,000 credits and overage net 0 → `plan_fit=enterprise`, residual
  `copilot_plan_inferred`, both scenario pool months untouched; with overage net $600 → `plan_fit=business`.
- Gate: CP-BILL / CP-ORGDATA fixtures through the real adapters and real `RateCard` reconcile (synthetic);
  RECON's `merge_reports` combines this report with a RECON report for `anthropic_api` and keeps `decisions`.

**Size.** ~2.85k LOC including tests.


---
**ORCHESTRATOR ADDITIONS (binding):** SPEC Appendix E.7 R-E44 (rounding_remainders keyed by adapter name) and R-E46 (undecidable convention).
