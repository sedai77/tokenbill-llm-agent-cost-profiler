### WIRING — Config and shared pipeline plumbing (wave 2)

**Goal.** The small shared layer both CLI packages build on, so they can run in parallel in wave 3: config
loading with precedence, the per-command `Env` (pricer, rules, keys), store opening, the adapter ingestion
loop, the shard mapper with a process pool, and the bill summary (SPEC §15 "Pipeline", §3.21, D30).

**Owns.** `tokenbill/config.py`, `tokenbill/pipeline/common.py`, `tests/v2/wiring/**`.

**Consumes.** `core.types`, `core.protocols`, `core.registry` (`get_adapter`, `sniff_adapter`, `load`),
`core.keys`, `core.ids`, `core.shards`, `core.kanon.publish`, `core.cache_rules.RulesTable`, `core.builders`,
`core.testing` (`FakePricer`, `MemoryStore`). Real `RateCard` and `SqliteStore` are loaded lazily by dotted path
(`tokenbill.rates.engine:RateCard`, `tokenbill.store.db:SqliteStore`) so this package tests with fakes.

**Provides.** `config.Config`, `config.load_config(path, environ, overrides) -> Config`;
`pipeline.common.Env`, `build_env(...)`, `open_store(...)`, `ingest_paths(...)`, `map_shards(...)`,
`bill_summary(...)` (SPEC §15 signatures).

**Build.** Config precedence CLI overrides > `TOKENBILL_*` env > JSON file (`./.tokenbill/config.json`, then
`~/.config/tokenbill/config.json`) > defaults; unknown keys → `UsageError`. `build_env` resolves keys
(`core.keys.load`; org key and collection key), the rate card (built-in + `--rates` + `--model-price` layers +
contract; injectable factory for tests) and the rules table. `ingest_paths` sniffs or selects adapters, builds
`IngestOptions` (identity mode, name/principal keys, team map, k, allowlist, window, renormalize), ingests,
and returns sources and DQ notes. `map_shards` runs a function per `ShardKey` sequentially or in a
`ProcessPoolExecutor` (workers open the store read-only by path), returning results in shard order.
`bill_summary` builds a `BillSummary` (exact/estimated/allowance totals, ESR via
`rates.engine.no_cache_equivalent_nano` when available, published breakdowns for arbitrary whitelisted
dimension lists, naive ratio from the CC `naive_usage` priced with the Env pricer).

**Acceptance tests.**
- config precedence (every layer), unknown key rejected, defaults per SPEC §15;
- `build_env` with an injected FakePricer factory; `ingest_paths` into `MemoryStore` with a fake adapter
  registered for the test; `map_shards` with jobs=1 and jobs=2 (a picklable top-level function) returns identical
  ordered results; `bill_summary` on MemoryStore publishes breakdowns (k enforced) and keeps allowance apart;
- gate (`importorskip` RATES engine and STORE db): `build_env` loads the real `RateCard`; `open_store` +
  `map_shards(jobs=2)` over a real `SqliteStore` equals jobs=1.

**Size.** ~1.3k LOC including tests.


---
**COPILOT AMENDMENT (binding, added by the orchestrator/contract owner after the GitHub Copilot design).** Your item
is **A-9** in the list below. The Copilot core additions (records, types, registry entries, `core.extensions`,
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
**ERRATA ROUTING (orchestrator):** read SPEC Appendix E.4 (rulings R-E24 … R-E33) — items addressed to WIRING are binding for you.


---
**ORCHESTRATOR ADDITIONS (binding):** SPEC Appendix E.6 R-E40 — compute the org median compaction summary size once per run and set `ctx.thresholds["context.compaction-window.post_tokens"]`; E.4 R-E24 (pass `post=` to compact-window replays).
