### CP-STORE — Copilot record store and analysis-context enricher (wave 2)

**Goal.** Persist the three people-level Copilot record types (seats, activity days, configuration incl. the
count rows of aggregate-only handoff bundles) in Copilot-owned tables of the same SQLite file, idempotently and
privately — including bundles pseudonymized with the GitHub admin's export key (R-E21) — count people over them
for k-anonymity, apply retention and purge; and build the enriched `AnalysisContext` every Copilot detector
reads: licenses, activity, config, outcomes, **plan evidence** and **pool months per plan scenario**, reconciled
channels, the reconciler's decisions, `ext:copilot`. Read SPEC §3.6, §7 (esp. §7.1 `meta`, `audit`), §8.3–§8.5;
addendum DC7, DC24, §3.1 (CA-8), §3.2 (CA-11), §3.4 (CA-23), §3.9, §7.2, §8.2, §10.0; `CORE-AMENDMENTS.md`
items C-10, C-14, C-17 and rulings R-E21, R-E22.

**Owns.** `tokenbill/copilot/record_store.py` (`CopilotRecordStore`), `tokenbill/copilot/enrich.py`
(`enrich_context`), `tests/v2/copilot_store/**`.

**Consumes.** `core.protocols` (`ExtRecordStore`, `LedgerStore`), `core.records` (`record_key`, records,
`COUNT_CONFIG_KINDS`), `core.types` (`AnalysisContext`, `PlanEvidence`, `PoolMonth`), `core.ids`, `core.jsonl`,
`core.pool` (`build_cells`, `capped_cost_centers`, `capped_policies`, `billing_modes`, `detect_plans`,
`pool_months`), `core.facts`, `core.testing` (`MemoryStore(adopt_key_ids=…)`, `MemoryRecordStore`,
`assert_record_store_conforms`), `core.builders`; stdlib `sqlite3`. Reads SPEC §7.1's `meta` and writes to its
`audit` table only through the documented DDL (contract), never other STORE tables.

**Provides.**
- `CopilotRecordStore(db_path: Path, *, create: bool = True)` implementing `ExtRecordStore`: addendum §7.2 DDL
  with `copilot_license_snapshots.assigned_via_team INTEGER` **nullable** (activity-report seats) and
  `source_kind` covering `github.copilot_activity_report`; `rec_key = stable_id("rk", record_key(rec))`;
  latest-fetch-wins upsert; key-id check against the accepted key ids in SPEC §7.1 `meta` — `org_key_id` and,
  after R-E21 adoption, `adopted_key_id` (in a keyless store both are the admin's export key id) — else
  `dq.principal_key_mismatch` (a keyless store that has not adopted yet refuses `p_` rows with the same dq,
  never stores them); each row keeps its `principal_key_id`, and per-principal joins (licenses × activity ×
  cost lines) are made only between rows of the same key id (addendum §7.2); `count_users(source="licenses"|"activity", where=…)` with `where` keys
  `team, cost_center, org, plan, bucket, product, date_from, date_to`, falling back — only when no license
  (activity) row exists in the window — to the `n_people` of `seat_counts` (`activity_counts`) summary rows of
  the matching team / org; `retain` deletes license and activity rows older than `identity_days` (count rows are
  team-level and kept); `purge(principal=…)` with an audit row.
- `enrich_context(store, record_stores, ctx, *, today, reconciled_channels, recon_decisions=()) ->
  AnalysisContext` (the `ExtensionSpec.context_enricher`).

**Build.** Addendum §7.2 plus:
1. `enrich_context` reads, for the window, Copilot cost lines and aggregates (monthly grain) from the ledger,
   outcomes via `LedgerStore.outcomes()`, and licenses / activity / config from the record stores (streaming
   cursors, bounded memory).
2. Decisions from CP-RECON arrive as `recon_decisions` (keys `convention:<source_id>`,
   `gross_is_list:<entity>:<month>`): cells are built per source with the decided convention (`excl` when
   none); `gross_is_list` per entity × month goes to `pool_months`. The enricher never re-derives them.
3. Plans: `detect_plans(…)` per month → `ctx.plans` (report-quota evidence exists only when CP-BILL ran under
   `copilot-report-quota`);
   `pool_months(…, plans=…)` → `ctx.pools` (two `PoolMonth`s per entity × month when a plan is unknown);
   `recent_estimates` from `ActivityDay.reported_cost_nano`; promo eligibility, billing modes and cap policies
   from `run_flags` (CLI flags and admin answers; the CLI snapshot wins per key).
4. Sets `reconciled_channels`, `recon_decisions`, `outcomes`, adds `ext:copilot` when Copilot data exists
   (ledger channels or record-store rows), returns a frozen replacement; never prices anything itself and never
   mixes `consumed_estimate_nano` into report sums.

**Acceptance tests.**
- `assert_record_store_conforms(lambda p: CopilotRecordStore(p))` green (idempotent re-ingest including
  `org=None`, latest-fetch-wins, order independence over 5 sources, retention, purge).
- Key ids: a record batch whose `principal_key_id` is neither `meta.org_key_id` nor `meta.adopted_key_id` is
  skipped with the dq code; after `SqliteStore` / `MemoryStore` adopted key id A (R-E21), a batch under A is
  stored and one under B is skipped; an org-keyed store (key id K) that adopted A stores batches under K and A
  and never joins a K principal with an A principal.
- Activity-report licenses (`assigned_via_team=None`) round-trip through SQLite as NULL and back to None.
- `count_users`: a person seated via two orgs counted once per scope; `bucket` / `plan` filters; an
  aggregate-only bundle (no licenses, `seat_counts` summary row `n_people=7` for team t) → `count_users(source=
  "licenses", where={"team": "t"}) == 7`.
- `enrich_context` on `MemoryStore` + `MemoryRecordStore` produces the Appendix C.P9 pool month (regime
  `overage`, ESTIMATED forecast) and adds `ext:copilot`; C.P13 inputs → two scenario pool months and one
  `PlanEvidence` with plan `unknown`; C.P14 inputs → `plan_conflict=True`; `recon_decisions` with
  `convention:<s>=incl` changes the cells' uncached tokens exactly as `build_cells(convention="incl")`; a
  `run_flags` `billing_mode.enterprise=volume` yields `billing_mode="volume"`; a store without Copilot data
  leaves the context unchanged.
- Two `CopilotRecordStore` instances and a `SqliteStore` (gate, `importorskip`) on the same file coexist (WAL);
  the SPEC STORE `iterdump` idempotence of its own tables is unaffected by record-store writes.
- Gate (`@pytest.mark.gate`): CP-BILL and CP-ORGDATA fixture files through the real adapters into a real
  `SqliteStore` + `CopilotRecordStore`, twice, give identical contents; the overlapping exports keep one row per
  natural id; a CP-HANDOFF bundle ingested into a keyless `SqliteStore(adopt_key_ids=True)` keeps every `p_`.

**Size.** ~1.9k LOC including tests.


---
**ORCHESTRATOR ADDITIONS (binding):** SPEC Appendix E.7 R-E45 (two-argument conformance factory; see tests/v2/kit/CONTRACT-CHANGE-KIT-C-1.md §3) and R-E46 (map an undecidable convention to excl + dq note).
