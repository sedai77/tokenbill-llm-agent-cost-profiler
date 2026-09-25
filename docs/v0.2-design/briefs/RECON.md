### RECON — Reconciliation ledger gate, residuals, cost maps, live pull, org scan (wave 2)

**Goal.** The trust anchor: prove (or disprove) per channel that the ledger and rate card match the
provider's own usage and invoice data, explain residuals, derive contract discounts, and give an admin
aggregate findings from Admin data alone (SPEC §12, §10.3, D19, D26, D31, D32). Works fully offline from
recorded pages; live pulls are opt-in.

**Owns.** `tokenbill/recon/{reconcile,residuals,costmap,pull,orgscan}.py`, `tests/v2/recon/**`,
`tests/v2/fixtures/recon/**`.

**Consumes.** `core.records` (`UsageAggregate`, `CostLine`, `UsageRecord`), `core.types` (`ReconRow`,
`ChannelVerdict`, `ReconciliationReport`, `ContractOverlay`, `Finding`, `AnalysisContext`), `core.catalog`
(`map_sku`), `core.money`,
`core.labels`, `core.findings` (finding helpers), `core.protocols` (`Pricer`, `Detector`), `core.builders`
(`make_aggregate`, `make_cost_line`), `core.testing` (`FakePricer.with_contract`, `assert_detector_conforms`).

**Provides.** `recon.reconcile.reconcile(...)` (signature §12); `recon.costmap.COST_TYPE_MAP`;
`recon.residuals.classify(...)`; `recon.pull.pull(kind, *, key_env, since, until,
out_dir, opener=None, sleep=time.sleep)`; `recon.orgscan.OrgScan` (registry `aggregate.org-scan`, requires
`{"aggregates"}`, kinds §10.3).

**Build.** Reconciliation per §12.1–§12.4 **per channel**: rate-card check priced from provider usage
(independent of the ledger), token coverage with the over-count rule, dollar coverage (allowance excluded),
the residual classifier in order (incl. `seat_allowance_unmetered`, `cents_rounding` from parse remainders,
`cloud_credits`, `unmapped_cost_type` for disabled SKU rules), effective discount, `--suggest-contract` overlay
with `channels` and **re-run** with `rerun_pricer_factory`, channel verdicts with `mapping_verified`, overall
verdict, and the **channel-total mode** of §12.1 when SKU/cost-type rules are unverified. `COST_TYPE_MAP` is
data from documented `cost_type`/`token_type` values; CUR/GCP rules come from `core.catalog.map_sku` (F-KIT). The ledger
is streamed (`Iterable[UsageRecord]`), memory bounded by keys. Live pull per §12.5. Org scan per §10.3: EXACT
premiums via `pricer.price_usage` on aggregate buckets, cache-read share vs the 0.84/0.94/0.80 benchmarks with
sources, write:read thrash, TTL mix, batch share, effective discount; workspace/model scopes only.

**Facts to verify.** `cost_type`/`token_type` enumerations, pagination names (checklist §19.8 #6, #10), SKU
rules (#17). Unverified → README and disabled rules.

**Acceptance tests** (build aggregates and cost lines with `core.builders`; do not import ADMIN):
- usage + cost records priced exactly → rate-card error 0, channel and overall verdict `reconciled`;
- cost report 15% below list on every model → effective discount 0.15; suggested overlay multiplier 0.85; the
  re-run is `reconciled` on CONTRACT basis; a non-uniform discount → "not a simple multiplier";
- two channels: `anthropic_api` reconciled and `bedrock` without invoice lines → bedrock `insufficient_data`,
  overall `not_reconciled`, and the report lists which channels a FOCUS export may include;
- a CUR-shaped channel with net cost 10% below unblended → effective discount 0.10 and a suggested bedrock
  overlay; with every SKU rule disabled the channel reconciles in channel-total mode ("totals only; schema
  unverified", `mapping_verified = False`), and a 3% unexplained gap on totals fails it;
- a Priority-tier usage bucket → residual `priority_excluded_from_cost_report`, not an error; code execution
  line → `code_execution_cost_report_only`; dates inside the revision window → provisional, excluded with
  `closed_only`; provider tokens absent from the ledger → `unobserved_traffic`; subscription-path ledger
  dollars → `seat_allowance_unmetered`; parse remainders → `cents_rounding`; an unknown cost type →
  `unmapped_cost_type`;
- ledger tokens 3% above provider on one day → over-count row, verdict `not_reconciled`; no invoice rows →
  `insufficient_data`; no-float lint clean on `recon/`;
- `pull` with a fake opener and fake sleep: pagination, 31-day chunking, rate limit, `retry-after` capped at 60 s;
  the key string never appears in recorded pages, logs or exception messages (grep test); missing `key_env` →
  `UsageError`; the socket guard proves no real network;
- org scan on hand-built aggregates: fast/geo/priority premiums exact to the nano; cache-read share 0.60 →
  finding with benchmark sources; share 0.90 → none; `assert_detector_conforms(OrgScan())`;
- gate `test_gate_ratecard_admin.py` (`importorskip` RATES engine and ADMIN adapters): ADMIN's recorded
  fixtures parsed by the real adapters reconcile with the real `RateCard`.

**Size.** ~2.7k LOC including tests.


---
**COPILOT AMENDMENT (binding, added by the orchestrator/contract owner after the GitHub Copilot design).** Your item
is **A-5** in the list below. The Copilot core additions (records, types, registry entries, `core.extensions`,
`core.pool`, Copilot catalog tables) are merged into `v0.2` before you start — read SPEC Appendix E.3 and
`/private/tmp/claude-501/-Users-shyamsedai-Library-Application-Support-Claude-scratch-workspaces-6ca7e323-9883-4573-b4f5-6b954b9105d0-24cd3ab5-9c38-4bd2-9927-e0d5ba6ed36e-scratch-2026-09-13-3536c7/0268c3f3-9470-4bd9-93db-0269a23905e9/scratchpad/copilot/briefs/CORE-AMENDMENTS.md` (§2–§5) for the exact contracts, and the Copilot addendum
`/private/tmp/claude-501/-Users-shyamsedai-Library-Application-Support-Claude-scratch-workspaces-6ca7e323-9883-4573-b4f5-6b954b9105d0-24cd3ab5-9c38-4bd2-9927-e0d5ba6ed36e-scratch-2026-09-13-3536c7/0268c3f3-9470-4bd9-93db-0269a23905e9/scratchpad/copilot/SPEC-v0.2-COPILOT.md` only as background. Do not implement Copilot adapters/detectors yourself
(the CP-* packages do); implement only your amendment item so the extension points work.

#### 5. Amendments to SPEC packages (A-; paste into briefs not yet started, late change requests otherwise)

| id | package | status @99ba098 | amendment | LOC |
|---|---|---|---|---|
| A-1 | RATES | not started | addendum §21.4 row, reading Copilot rows through `core.facts.copilot_rates()` / `core.extensions.extension_rate_files()` (missing → dq); predicate keys `routing`, `compliance_in`; band hypotheses; both Copilot paths → LIST_EQUIVALENT into `PricedTotal.pool` (`allowance` = subscription lines only); `pricing verify` runs `core.extensions.rate_verifiers()` | +120 |
| A-2 | STORE | not started | addendum §7.1 DDL and §21.4 row **plus** `SqliteStore(…, adopt_key_ids: bool = False)` (R-E21 replaces the §7.1 "Key-id adoption" paragraph: with or without its own org key the store adopts the principal and name key ids of the first `copilot-export` bundle only, one adopted id, `meta.adopted_key_id` / `adopted_name_key_id` / `org_key_mode` + an `audit` row, a second bundle key id → `UsageError`), `count_users(source="cost_lines")`, `source_stats` (implements `core.protocols.LedgerStats`), `purge(principal=…)` also for adopted-key rows, `ReconciliationReport` untouched (not stored) | +190, total 2,990 |
| A-3 | TRACE | not started | addendum §21.4 row; closed key sets derived with `core.records.record_fields` (C-4) and `core.records.RAW_USAGE_ENUMS` / `RAW_USAGE_NUMERIC`, replacing the hand lists (net-neutral, O-5) | ≤ 0 net |
| A-4 | TELEM | **started** | late change request (R-E23): `otlp` skips resources for which `core.models.is_copilot_resource` is true and sets `stats["defer:copilot-otel"]`; rebase onto the gate-F' core first | +40 |
| A-5 | RECON | not started | addendum §21.4 row; `merge_reports` also unions `ReconciliationReport.decisions` (conflicting values → `ContractViolation`) | +90 |
| A-6 | REPLAY | **started** | late change request (R-E23): accept billing class `pool` like `allowance` (LIST_EQUIVALENT; mixed classes still raise) | +10 |
| A-7 | PLAN | not started | addendum §21.4 row; skip `replay == "aggregate"` levers; lever lookup via `core.catalog.lever` (searches both tables) | +40 |
| A-8 | OUT | not started | addendum §21.4 row (`render_sections`, `write_focus(…, extra_rows, owned_channels)`, `money_json` → `figure_json`, BILL line for `PricedTotal.pool`); `outputs/ccusage.py` moves to CLI-LEDGER (O-5) | +40 net, total 2,890 |
| A-9 | WIRING | not started | addendum §21.4 row plus `open_store(…, adopt_key_ids=False)` passed to `SqliteStore`; `ingest_paths` persists records **after** the ledger ingest (so an adopted key id exists before `persist`) and re-reads deferred files | +90 |
| A-10 | CLI-LEDGER (wave 3) | not started | addendum §21.4 row; `reconcile` prints the merged decisions (`recon_decisions_of(reports)`) with its verdicts; nothing is persisted (C-17) | +45 |
| A-11 | CLI-SAVINGS (wave 3) | not started | addendum §21.4 row; because decisions are not persisted, `findings` / `report` (and `scan` on Copilot data) first run `core.extensions.run_reconcilers` + RECON's reconcile over the same window, then call `enrich(…, reconciled_channels=…, recon_decisions=recon_decisions_of(reports))` (without reconcilers, e.g. on a store without Copilot data, `recon_decisions=()`: `excl` convention and unclassified discounts, both labelled) | +65, total 2,965 |
| A-12 | INTEGRATION (wave 4) | not started | `docs/COPILOT.md` (fleet recipe: managed OTel incl. the JetBrains caveat, **VS Code `agent-traces.db` opt-in + daily `copilot collect --source vscode`**, CI post-step, gh-aw artifacts, auth table, privacy); **`docs/COPILOT-ADMIN.md` generated verbatim from `tokenbill/copilot/handoff_data/admin_guide.md`** with `tests/v2/e2e/test_copilot_admin_doc.py` asserting byte equality; flagship §18 incl. the handoff and plan-unknown paths; Copilot release gates (below); weekly YAML check | docs/tests |
| — | CC, ADMIN, BLOCK, VERIFY, DETECT-CACHE, DETECT-OTHER, SYNTH-ORACLE, SYNTH-FLEET | mixed | none | 0 |

