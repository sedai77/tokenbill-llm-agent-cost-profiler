### STORE — SQLite store, merge, rollups, retention, pseudonymization (wave 2)

**Goal.** A fleet-scale, content-free ledger with exact integer money (exact, estimated and allowance lines
kept apart), idempotent order-independent merges, streaming lane access for sharded analysis, rollups that
survive identity retention, and privacy enforced at write time (SPEC §7, §8.1–§8.3, D26, D27, D39, D41).

**Owns.** `tokenbill/store/{schema,db,merge,rollups,retention,pseudonym}.py`, `tests/v2/store/**`.

**Consumes.** `core.records`, `core.types` (`IngestResult`, `RawAggregate`, `AggRow`, `LedgerCostRow`,
`ClusterDay`, `Finding`, `ReceiptRow`, `LaneIndexRow`), `core.protocols` (`LedgerStore`, `Pricer`), `core.ids`,
`core.lanes.group_lanes`, `core.errors.PrivacyError`, `core.jsonl.open_private`, `core.builders`, `core.testing`
(`FakePricer`, `MemoryStore` as the reference, `assert_store_conforms`, `CANARY`).

**Provides.** `store.db.SqliteStore` (the full `LedgerStore` of SPEC §3.6: `ingest`, `reprice`,
`iter_lanes(where, lane_keys)`, `iter_requests`, `iter_usage_records`, `lane_index`, `lane_first_reads`,
`count_users`, `aggregates`, `cost_lines`, `outcomes`, `aggregate`, `cluster_days`, `cost_rows`,
`get_cursor`/`set_cursor`, `put_findings`/`findings`, `put_receipt`/`receipts`, `purge`, `audit`, `meta`;
`read_only=True` mode for shard workers); `store.schema` (DDL §7.1, forward-only migrations); `store.merge`
(§7.3); `store.rollups.refresh_rollups`; `store.retention.apply_retention`, `purge`, `rotate`;
`store.pseudonym.Pseudonymizer`.

**Build.** DDL exactly as §7.1 (int nano money incl. `exact_nano`, `est_*`, per-bucket columns and
`exact_mask`; `attr_extra_json`; `billing_path`; denormalized `lanes.team/billing_class`; WAL; 0600 file,
0700 dir; indexes). `ingest` in 5,000-row transactions; pseudonymize `r_`/`c_` with the org key before any
write (`PrivacyError` without a key); accept `p_` only under the store's org key id and `h_` only under its
name key id (else null those fields with `dq.name_key_mismatch`); price with the given pricer and store point,
exact, estimated and per-bucket nano; same-sha source re-ingest is a no-op. Merge rules §7.3 including
per-field attribution priority (and per `extra` key) and the `request_index` collision flag. `iter_lanes`
streams by lane with bounded memory. `aggregate` over the whitelisted dimensions with `COUNT(DISTINCT
principal)`; principal/session in `group_by` → `PrivacyError`. `count_users` is an exact distinct count that
never returns ids. Rollups §7.4 (refreshed before identity retention; `allowance_nano` kept apart).
Retention/purge/audit/rotation §7.5.

**Acceptance tests.**
- `assert_store_conforms(SqliteStore)`;
- idempotence: ingesting the same sources twice leaves an identical `iterdump()` of the data tables; order
  independence over all permutations of 5 small sources (transcript-, OTel-, trace@2-, trace@1- and
  responses-shaped `IngestResult`s built with foundation builders);
- cross-source merge: a FULL-fidelity transcript request and a NO_TTL_SPLIT OTel request joined through
  `provider_request_id` → one row with transcript usage, OTel-only attribution filled (incl. an `extra` key),
  two bits in `sources_mask`; a `requestId` seen with two message ids is never used as a join key;
- two trace@1 sessions reusing `run_id` → separate sessions and a total of $22, not $40;
- invariants Σ request priced nano == Σ inference priced nano and Σ exact + Σ est == Σ priced; a placeholder
  inference stores its input lines as exact and its output line as estimated; subscription inferences land in
  `allowance_nano` rollups and never in `exact_nano`; `cost_rows` per-bucket sums equal ledger totals exactly;
- `attr_extra_json` round-trips (`mdm_group` → `cluster_day` kind `mdm_group`; `task_id` survives);
- file mode 0600 / dir 0700 (POSIX); `r_`/`c_` principals stored only as `p_`; the raw ref and CANARY never
  appear in the SQLite file bytes; ingest without an org key but with `r_` principals → `PrivacyError`; a source
  whose `name_key_id` differs from the store's → names nulled, `dq.name_key_mismatch`;
- `aggregate(group_by=["principal"])` → `PrivacyError`; `count_users` equals a brute-force distinct count;
  rollups give correct `active_users`; retention nulls principals only after rollups refresh and `cluster_day`
  survives; `purge` deletes, vacuums and writes an audit row without the raw identity; key rotation writes
  `key_events`;
- `lane_index`, `lane_first_reads`, `iter_lanes(where={"team": …, "lane_kind": …})` and `lane_keys` sampling
  are consistent with `MemoryStore`; two read-only connections scan concurrently;
- perf (marker `perf`): ingest ≥ 20,000 requests/s; `iter_lanes` over 10⁶ requests ≤ 30 s; `lane_index` over
  10⁶ ≤ 5 s; peak RSS ≤ 500 MB; PR variants at 10⁵;
- gate `test_gate_adapters.py` (`importorskip` CC/TELEM/TRACE/ADMIN adapters and the RATES engine): their
  checked-in fixture files read through the real adapters ingest, merge and price correctly.

**Size.** ~2.8k LOC including tests.


---
**COPILOT AMENDMENT (binding, added by the orchestrator/contract owner after the GitHub Copilot design).** Your item
is **A-2** in the list below. The Copilot core additions (records, types, registry entries, `core.extensions`,
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
**ERRATA ROUTING (orchestrator):** read SPEC Appendix E.4 (rulings R-E24 … R-E33) — items addressed to STORE are binding for you.
