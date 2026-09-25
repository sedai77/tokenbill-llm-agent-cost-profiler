### CLI-SAVINGS — Savings, verification and CI-gate verbs (wave 3)

**Goal.** Wire the savings side into the CLI: `scan` (local self view), `scan --org` (aggregate-only org
scan), `me`, `calibrate`, `findings` (sharded, pooled, re-scoped, Shapley-ranked), `whatif`, `policy` and
`policy check-effect`, `measure plan/run`, `ab`, `receipt`, `check` (CI gate), `report`, and `demo --fleet`
(SPEC §15, §15.2, §17, §18, D18, D30, D32).

**Owns.** `tokenbill/gate.py`, `tokenbill/pipeline/{savings,verification}.py`,
`tokenbill/commands/{scan,me,calibrate,findings,whatif,policy,measure,ab,receipt,check,report}.py`,
`tests/v2/cli_savings/**`.

**Consumes.** WIRING (`config`, `pipeline.common`: `Env`, `build_env`, `open_store`, `ingest_paths`,
`map_shards`, `bill_summary`), every wave-2 package through its documented API. Do not import
`pipeline.ledger` (CLI-LEDGER, same wave); `cli.py`'s command table (CLI-LEDGER) already lists your modules —
test your commands by building an argparse parser from your modules' `add_parser` functions, and leave full
`main()` tests to merge gate 2.

**Provides.** `pipeline.savings.run_calibrate`, `run_findings`, `run_whatif`, `run_policy`, `run_scan`,
`run_scan_org`, `run_report`, `run_demo_fleet`, `run_check`; `pipeline.verification.run_measure_plan`,
`run_measure_run`, `run_ab`, `run_receipt`; `gate.run_check(...) -> CheckResult` (SPEC §15.2); the command
modules listed above.

**Build.** `run_findings` exactly as SPEC §15: `lane_index` → `plan_shards` (config `shard_max_requests`) →
static-prefix floor from `lane_first_reads` → calibrate (two streaming passes over shards) → per-shard
`run_detectors(emit_missing=False)` through `map_shards` (`--jobs`) → once `run_detectors([],
emit_missing=True)` plus `aggregate.org-scan` on the store's aggregates/cost lines → `merge_findings` →
`rescope_findings(count_users=…store.count_users…)` → `build_action_plan` (sample + full-scope scaling,
billed/allowance apart) → `put_findings`. `scan` = discover → CC adapter (install identity; `--billing-path`
default from config) → temporary `SqliteStore` → price → the `run_findings` path → render (self view; `me` is a
pure alias). `scan --org` = ADMIN adapters on the page/CUR/GCP files (or `--live` pull) → temporary store →
reconcile → org scan findings → render. `whatif` replays policies (documented/calibrated/both; optional
Shapley). `policy` builds packs with `model_pricing_emitter=rates.contract.to_model_pricing`. `measure plan`
(incl. `--org-wide` → ITS design), `measure run` (panel at the baseline card; DiD/CUPED/ITS; guards with the
per-channel reconciliation), `ab`, `receipt create/sign/verify` (refusals → exit 3). `check` per §15.2 with
SARIF. `report` renders HTML for fleet/team/self. `demo --fleet` = `synth.fleet.generate` → ingest source
files → full pipeline → report, marked synthetic, keyless, networkless.

**Acceptance tests.**
- `scan` on CC's fixture tree prints the exact bill with label chips, the allowance line when the billing path
  is subscription, data-quality lines (naive ratio), calibration status and top recoverable levers;
  `--format json` validates with `validate_result_json`; `me` output equals `scan --self`;
- `scan --org` on ADMIN's recorded pages alone (no transcripts) prints a reconciled bill and org-scan
  findings; `--live` without `--admin-key-env` → exit 2;
- `findings` on a store built from `synth.fleet.generate()` equals across `--jobs 1` and `--jobs 2` and across
  shard caps of 250k and 1 (byte-identical JSON); the tiny team never appears; `--group-by principal` without
  `--self` → exit 2; `--break-glass` writes an audit row;
- `calibrate --require-pass` exit 3 when failing; `whatif --policy ttl=1h@lane_kind:main` reports saving with
  label and calibration; `policy -o DIR` writes the pack files; `measure plan --org-wide` yields an ITS design;
  `receipt sign` of an ESTIMATED receipt → exit 3;
- `check`: demo `timestamp` vs `well-behaved` baseline → exit 3 with `TB-NEW-BREAKER` (volatile-system) and
  `TB-CACHE-SHARE` in SARIF; `well-behaved` vs itself → exit 0; fewer than 3 runs → cost check skipped with a
  warning;
- `demo --fleet` completes keyless in < 60 s, labels output synthetic, and its JSON is byte-identical across two
  processes;
- perf (marker `perf`, nightly): `findings` + plan over a 10⁶-request synthetic store ≤ 8 min single process,
  peak RSS ≤ 1.5 GB, 2× requests ≤ 2.3× time; `--jobs 4` ≥ 2.5× faster on a 4-core runner.

**Size.** ~2.9k LOC including tests.

---


---
**COPILOT AMENDMENT (binding, added by the orchestrator/contract owner after the GitHub Copilot design).** Your item
is **A-11** in the list below. The Copilot core additions (records, types, registry entries, `core.extensions`,
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



---
**ERRATA ROUTING (orchestrator):** read SPEC Appendix E.4 (rulings R-E24 … R-E33) — items addressed to CLI-SAVINGS are binding for you.
