## 4. Integration step (wave 4, after merge gate 2)

Owned files: `tokenbill/__init__.py`, `README.md`, `DESIGN.md`, `CHANGELOG.md`, `CONTRIBUTING.md`,
`SECURITY.md`, `Makefile`, `docs/**`, `.github/**` (except `.github/workflows/ownership.yml`),
`scripts/{sbom.py,check_zero_deps.py,repro_check.sh,perf_gates.py}`, `tests/v2/e2e/**`.

1. **Version**: bump `__version__` to `0.2.0`; resolve any merge-gate contract-change notes with the contract
   owner.
2. **End-to-end tests** (`tests/v2/e2e/`, each a separate file so failures localize):
   `test_fleet_flagship.py` (SPEC §18: every plant within tolerance, control clean, per-channel reconciliation
   via suggested contracts 0.85 / 0.90, allowance never billed, k-anonymity, canary absent, byte determinism
   across two processes and across `--jobs 1/2`, `demo --fleet` < 60 s); `test_plan_recovery.py` (payments TTL
   lever Shapley within ±5%, headline range, allowance headroom apart); `test_verification_panels.py` (stepped
   wedge 25% effect; ITS org-wide change, MEASURED only); `test_receipts_e2e.py` (measure → receipt → sign →
   verify, `needs_ssh_keygen`); `test_canary_e2e.py` (every output: SQLite bytes, trace@2, result JSON, HTML,
   terminal, FOCUS, SARIF, ccusage, showback, policy packs); `test_no_egress.py` (the whole CLI except `--live`
   paths under the socket guard); `test_dual_engine_cli.py` (analyze routing on the demo corpus);
   `test_local_corpus.py` (marker `local_corpus`, release gate SPEC Appendix G.2 #2); perf gates at full size
   (nightly) via `scripts/perf_gates.py`.
3. **Supply chain** (SPEC §8.10): SHA-pinned actions, top-level `permissions: {}`, Dependabot, zizmor, CodeQL,
   Scorecard workflows; CI matrix Linux/macOS × 3.10–3.13 plus Windows × 3.12 running the full suite; per-package
   coverage report; zero-runtime-dependency check; stdlib CycloneDX SBOM with zero runtime components; build
   provenance (`actions/attest-build-provenance`), PEP 740 attestations, reproducible-build rebuild-and-diff;
   weekly `pricing verify --live` job that opens an issue on drift and 14 days before a promotion's end; release
   workflow kept on Trusted Publishing.
4. **Docs**: move the v0.1 contract to `docs/SPEC-v0.1.md` and install SPEC-v0.2 as `docs/SPEC.md`;
   `docs/PRIVACY.md` (§8.11 DPIA + one works-council template + employee notice), `docs/FLEET.md` (MDM collector
   rollout, identity modes and key custody, the CI post-step with `actions/upload-artifact` + `tokenbill collect
   claude-code-headless --in "$RUNNER_TEMP/claude-execution-output.json" --attr workload=ci`, Windows directory
   restrictions, the CUR 2.0 CSV export with an Athena query, the GCP billing-export query, the reference
   OpenTelemetry Collector config with `redaction` + `file` exporter `format: json`, `compression: none`, and
   the fleet-scale runbook with `--jobs`), `docs/VERIFY.md` (verifying releases and receipts; ITS vs randomized
   designs), `docs/THREAT-MODEL.md`; rewrite README (fleet quick start, `scan --org` first-day path, labels and
   honesty rules incl. list-equivalent allowance; **rewrite "Related work"**: provider tools are no longer "silent
   on why" — Claude Code `/usage` names likely causes and Anthropic and OpenAI ship cache diagnostics; position
   Token Bill as the fleet-wide, reconciled, verified layer); DESIGN.md sections for the ledger, gates, replay
   semantics, shards and threats to validity; CHANGELOG 0.2.0; SECURITY.md CVD timeline; CONTRIBUTING
   (ownership, gates, contract owner).
5. **Demo refresh**: regenerate README terminal excerpts from `tokenbill demo --fleet` (synthetic, labeled);
   keep the v0.1 demo excerpt unchanged.
6. **Release gates** (SPEC Appendix G.2 = PLAN §1.6): real redacted admin page pair added (or the release notes state the
   `dq.recon_schema_unverified` limitation), local-corpus test run on a maintainer machine, `pricing verify`
   offline clean, VERIFY items listed; full suite on the matrix, perf gates, ownership check; adversarial
   review hand-off.


---
**COPILOT AMENDMENT (binding, added by the orchestrator/contract owner after the GitHub Copilot design).** Your item
is **A-12** in the list below. The Copilot core additions (records, types, registry entries, `core.extensions`,
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
**ORCHESTRATOR ADDITIONS (binding):** SPEC Appendix E.6 R-E39 — the flagship check of the search compaction-window plant compares the 400k evidence point (`compaction-window:400k`), not `Finding.recoverable`.
