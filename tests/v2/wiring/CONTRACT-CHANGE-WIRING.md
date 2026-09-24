# CONTRACT-CHANGE-WIRING — additive items and cross-package assumptions (wave 2b)

WIRING implements SPEC §15 exactly (`Config`, `load_config`, `Env`, `build_env`, `open_store`,
`ingest_paths`, `map_shards`, `bill_summary`) plus Copilot amendment A-9. The items below are
**additive** (keyword arguments with defaults, new helpers, one data-quality code) or assumptions
about sibling packages that the gate tests pin at merge gate 1. No core file was edited.

## 1. Additive keyword arguments (defaults keep the SPEC signatures)

| function | added | why |
|---|---|---|
| `build_env(…)` | `pricer_factory: Callable[..., Pricer] \| None = None`, called as `factory(rates=…, contract=…, model_prices=…)` | the brief's "injectable factory for tests" (FakePricer); default = the lazy RATES `RateCard` |
| `open_store(path, env, *, create=True)` | `adopt_key_ids: bool = False` | A-9 / R-E21 (passed to `SqliteStore` only when True; a store without it → `UsageError`) |
| `ingest_paths(…, adapter="auto")` | `record_stores: Sequence[ExtRecordStore] \| None = None` | A-9: the records must be persisted somewhere; default = `core.extensions.open_record_stores` on the database file of a store opened with `open_store` (only when a result carries records) |

New public helpers (no SPEC counterpart): `ingest_options`, `load_team_map`, `load_rate_card`,
`shard_store`, `analysis_thresholds`, `org_compaction_median`, `compaction_post_tokens`,
`median_tokens`, `with_compaction_post`, `config.config_json`, `config.default_config_paths`,
`config.FrozenMap`.

## 2. New data-quality code

`dq.records_not_persisted` (warn): seat / activity / configuration records were read but no
extension record store was available (a `MemoryStore` without `record_stores`, or the extension's
record-store module is not installed — the latter also yields `dq.extension_unavailable`). Proposed
for the SPEC §5.1 list (next errata pass).

## 3. Assumptions pinned by gate tests (owners: RATES, STORE)

- **RATES:** `rates.schema.load_builtin()`, `load_file(path, name)` (name `user:<file name>`),
  `model_price_layer(specs)`, `rates.contract.load_contract(path)`, `rates.engine.RateCard(layers,
  contract=None)` with **layers lowest precedence first** (builtin, `--rates` files in order,
  `--model-price`), `rates.engine.no_cache_equivalent_nano(pricer, inference, ts_ms)`. SPEC §6.2
  names the precedence but not the list order; `test_gate_rates.py::test_model_price_layer_wins_over_builtin`
  fails if RATES reads the list the other way round (fix: reverse in `load_rate_card`, one line).
- **STORE:** `SqliteStore(path, *, create, org_key, name_key_id, pricer[, adopt_key_ids])` for
  `open_store`, and `SqliteStore(path, create=False, read_only=True)` (no keys, no pricer) in shard
  workers; `aggregate(group_by=())` for the window total (the conformance suite already uses
  `group_by=[]`) and `group_by=("billing_path",)`; `LedgerStats.source_stats()` returns the integer
  `IngestResult.stats` of stored sources (the naive-usage stats below).

## 4. Claude Code naive usage (SPEC §5.3 #12)

`IngestResult.naive_usage` has no store table. `ingest_paths` keeps it as integer source stats
`naive:<model>:<bucket>` (bucket ∈ `uncached_input`, `cache_read`, `cache_write_5m`,
`cache_write_1h`, `cache_write_other`, `cache_write_unknown`, `output`, `web_search_requests`)
before the ledger ingest, and `bill_summary` prices them with the Env pricer at each model's unit
rates (the de-duplicated side: the Claude Code requests of the same models, same rates). The ratio
is therefore **store-wide** (stats are not windowed). A first-class `naive_usage` table is a v0.3
candidate.

## 5. Readings of the SPEC text

- `bill_summary(group_by=…)`: each element is a comma-separated dimension list (one breakdown each;
  key = the list joined by commas, `BillSummary.breakdowns`); dimensions from the SPEC §7.2 whitelist.
- ESR = 1 − Σ exact ÷ Σ `no_cache_equivalent_nano` over the window's billable inferences priced
  EXACT on a billed basis with the Env pricer (so both terms cover the same tokens); None without the
  engine or such inferences. Ratios are plain decimal strings of 28 significant digits.
- "unknown keys → UsageError" is applied to the config file and the CLI overrides; unknown
  `TOKENBILL_*` environment variables are ignored.
- R-E40: `analysis_thresholds(env, store, since_ms=…, until_ms=…)` returns the Config's thresholds
  plus `context.compaction-window.post_tokens` (the median of every COMPACTION `post_tokens` in the
  window, even counts rounded half-even; a configured value wins). CLI-SAVINGS / PLAN put it into
  `AnalysisContext.thresholds` and pass `with_compaction_post(policy, median)` to replays (R-E24).
