# tests/v2/copilot_store — CP-STORE (Copilot record store and analysis-context enricher)

Wave 2b package CP-STORE: `tokenbill/copilot/record_store.py` (`CopilotRecordStore`, the Copilot
`ExtRecordStore`) and `tokenbill/copilot/enrich.py` (`enrich_context`, the
`ExtensionSpec.context_enricher`). Binding text: CP-STORE brief, CORE-AMENDMENTS C-10 / C-14 / C-17,
rulings R-E21, R-E22, R-E45, R-E46, addendum §7.2, §8.2, §10.0, Appendix C.P9 / P13 / P14.

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/copilot_store` (also `--python 3.10`).
Coverage: `uv run --python 3.12 --extra dev coverage run -m pytest tests/v2/copilot_store && uv run
--python 3.12 --extra dev coverage report --include='tokenbill/copilot/record_store.py,tokenbill/copilot/enrich.py'`
(100% / 100% at hand-off). Full-size perf: add `-m perf`.

| file | covers |
|---|---|
| `support.py` | area helpers: a SPEC §7.1 `meta` (and `audit`) stand-in for the not-yet-merged `SqliteStore`, ingest-result builders, report rows, coverage aggregates, the binding C.P9 series, activity-report / seats-API seats, run flags |
| `test_record_store.py` | `assert_record_store_conforms` with the R-E45 two-argument factory (and the one-argument form refused as CONTRACT-CHANGE-KIT-C-1 §3 says); §7.2 DDL (nullable `assigned_via_team`, `org NOT NULL DEFAULT ''`, no `copilot_decisions`, WAL); round trips (activity-report seat `assigned_via_team=None` ↔ SQL NULL, activity counts / flags / estimates, config attr types); latest-fetch-wins and ties equal to the fake in every order; key ids (unaccepted key id → `dq.principal_key_mismatch`, no `meta`, keyless not adopted, adoption of A accepts A and refuses B, org-keyed K + adopted A keeps both and never joins them); `count_users` (two orgs counted once, `bucket` / `plan` / `org=""` / surface / dates filters, activity filters, aggregate-only `seat_counts` fallback `n_people=7`, `activity_counts`, no fallback when person rows exist, privacy and argument errors); windows; retention and purge (audit rows without identities, STORE `audit` vs `copilot_audit`, the purged `p_` gone from the file bytes); private file mode; two instances on one file; STORE tables untouched by record writes; bad batches roll back; error paths |
| `test_enrich.py` | C.P9 pool month (regime overage, forecast 3,100,000 [2,920,000; 3,280,000] credits ESTIMATED, overage $4,200 [$2,400; $6,000]) and `ext:copilot`; C.P13 (one `PlanEvidence` unknown / none / 100 unknown seats, two scenario pool months $600 / $0); C.P14 (`plan_conflict=True`, pool 195,000); `billing_mode.enterprise=volume` from the CLI snapshot over the admin answers; unchanged context without Copilot data (empty store, Claude-only data, other months, empty window); lanes-only Copilot data; `convention:<s>=incl` equals `build_cells(convention="incl")`, per-day mixed files, overlapping exports, tagged aggregates, `undecidable` → excl + `dq.copilot_convention_undecidable`; estimates kept out of report sums; `gross_is_list` decisions; merged channels / decisions (conflict → `ContractViolation`); outcomes and existing context rows; org entity mode; configuration as of the window; `CopilotRecordStore` gives the same context as `MemoryRecordStore`; store selection; seats under one key id per month (`dq.copilot_key_id_mixed`); the registered hook through `core.extensions.enrich`; order independence; argument checks; extreme windows |
| `test_properties.py` | hypothesis: the SQLite store equals the fake on random batches in any order (contents, counts, `count_users` for several filters); fuzz of `count_users`, windows, retention and purge, and of `enrich_context` (today, decisions, windows, entity mode) — only `TokenbillError` escapes; enricher invariants (scenario pairs exactly when seats are unknown, report consumption only from the report, estimates only for unreported days, input-order independence); per-day conventions equal `build_cells` per group |
| `test_gates.py` | `@pytest.mark.gate` (merge gate 1, `importorskip`): conformance with `SqliteStore(path, org_key=…)` then `CopilotRecordStore(path)`; two record stores and a `SqliteStore` on one WAL file with STORE's `iterdump` unaffected; purge through STORE's `audit`; keyless adopting `SqliteStore` + record store; CP-BILL / CP-ORGDATA fixtures through the real adapters twice (and reversed) → identical contents, one row per natural id; overlap fixtures keep the latest version; the enricher over the real adapters' records (scenario pairs exactly for unknown seats); a CP-HANDOFF bundle in a keyless `SqliteStore(adopt_key_ids=True)` keeps every `p_` |
| `test_perf.py` | addendum §17 (`copilot scan`, 5,000 seats, one month): put 5,000 seats + 150,000 activity days, count, enrich ≤ 60 s CPU (`perf`, ~7 s measured); PR variant at 1/10 size ≤ 6 s |
| `CONTRACT-CHANGE-CP-STORE-1.md` | what the frozen core leaves open (file ↔ row mapping for conventions, no dq channel for enrichers, entity mode source, counting across key ids) and the readings implemented |

## Fixtures and provenance

No fixture files. Every record is synthetic and built in the tests with `core.builders`
(`make_license`, `make_activity`, `make_config`, `make_ai_usage_row`, `make_seat_line`,
`make_aggregate`, `make_principal`) and HMAC pseudonyms from `core.ids.pseudonym` under test keys;
the numbers are the hand-computed worked examples of addendum Appendix C (P9 binding series, P13,
P14). The SPEC §7.1 `meta` / `audit` tables written by `support.ledger_meta` are the documented DDL
(the contract), used as a stand-in for `SqliteStore` until STORE merges. Gate tests read the CP-BILL,
CP-ORGDATA and CP-HANDOFF fixture directories (their provenance is in those areas) and are skipped
until those packages and STORE are merged. The gate tests were exercised before hand-off against the
sibling packages' in-progress adapters, fixtures and bundle writer with a scratch `SqliteStore`
stand-in (nothing of it committed): all eight passed.

## Interpretations (details in the module docstrings)

- **Record store.** `rec_key = stable_id("rk", record_key(rec))`; latest `fetched_ms` wins, ties go to
  the canonically larger JSON (as the fake); person rows only under the ledger `meta` key ids
  (`org_key_id`, `adopted_key_id`), read at every `put`; configuration rows always; `count_users` is
  the largest per-key-id distinct count; the count-row fallback applies only without person rows in
  the window; `retain` / `purge` overwrite deleted content (`secure_delete`) and checkpoint the WAL,
  and write one audit row per run that removed rows (purge always), naming no person; `purge`
  accepts only `p_` pseudonyms. No reconciler decision is stored (C-17).
- **Enricher window (monthly grain).** The window is widened to the calendar months it touches (the
  pool resets on the 1st): whole-month Copilot cost lines / aggregates and `github.copilot_metrics`
  outcomes are added to the context (rows already there are kept, ids deduplicated); `licenses`,
  `activity`, `config`, `plans` and `pools` are set from the record stores and `core.pool`;
  configuration "as of the window": count rows of the months, every `run_flags`
  snapshot up to the later of the window end and the end of *today* (the CLI snapshot is taken today),
  per other kind and entity the snapshots inside the months plus the latest one before them (else the
  earliest after).
- **Conventions** per report file through the coverage aggregates' `source` dim (CONTRACT-CHANGE §1).
- **Plans and pools.** `core.pool.detect_plans` for each month with seat or usage data, then
  `core.pool.pool_months(…, plans=…, gross_is_list=<decision pairs>, recent_estimates=…)`; estimates
  are `ActivityDay.reported_cost_nano` per day (capped cost centers as `cc:<name>`, else the
  enterprise; non-positive values ignored). Data-quality codes go into `PoolMonth.notes`.
- **Capabilities.** `ext:copilot` when the widened window holds any Copilot cost line, aggregate,
  outcome, request, license, activity day or configuration row; also `licenses` / `activity` /
  `config` for the rows added. Without Copilot data the context is returned unchanged (same object).

## Unverified facts

None introduced here: every price, plan, SKU, quota and lag fact is read through `core.pool` from
`core.facts` (their VERIFY flags — seat-line unit, `copilot_standalone` plan, plan-quota map,
editor families — are F-CORE-C's / F-POOL's and are listed in `tests/v2/pool/README.md`).
