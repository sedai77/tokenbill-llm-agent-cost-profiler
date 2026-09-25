# tests/v2/wiring — WIRING (config and shared pipeline plumbing)

Package WIRING (wave 2; SPEC §15 "Pipeline", §3.21, D30; Copilot amendment A-9; rulings R-E21,
R-E24, R-E40). Owned modules: `tokenbill/config.py`, `tokenbill/pipeline/common.py`.

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/wiring` (also `--python 3.10`).
Coverage: `uv run --python 3.12 --extra dev coverage run -m pytest tests/v2/wiring && uv run --python
3.12 --extra dev coverage report --include='tokenbill/config.py,tokenbill/pipeline/common.py'`
(100% of both at hand-off; the pool workers' own lines are also run in-process by
`test_worker_initializer`, since coverage does not follow spawned workers).

Everything here runs on the foundation fakes (`FakePricer`, `MemoryStore`, `MemoryRecordStore`,
builders). The real RATES `RateCard` and STORE `SqliteStore` are reached only by the gate tests.

| file | covers |
|---|---|
| `test_config.py` | defaults per SPEC §15; precedence of **every layer** (overrides > `TOKENBILL_*` > file > defaults; `./.tokenbill/config.json` before `~/.config/tokenbill/config.json`, an explicit `--config` path before both, files never merged); per-key merge of `retention` / `thresholds`; sequences replace; `None` overrides = flag not given; every environment variable; unknown file / override / retention keys → `UsageError` (unknown `TOKENBILL_*` ignored); invalid values per field and per layer; malformed, non-object and oversized files; errors never echo a value; JSON decimals; relative file paths resolve against the file; `config_json` round trip; `Config` frozen, hashable, picklable, validated on direct construction; `FrozenMap` |
| `test_config_fuzz.py` | **hypothesis**: random file objects, raw file bytes, environment values and overrides only raise `UsageError`; every success is a valid hashable `Config` |
| `test_env.py` | `build_env` with an injected FakePricer factory (arguments forwarded, explicit beat Config), key loading (org key names too without a collection key; collection key = name key), `repr` without key bytes, 0644 key refused, bad inputs; the default factory with stand-in `tokenbill.rates.*` modules: layers **lowest precedence first** (builtin, `--rates` in order, `--model-price`), contract overlay, missing RATES → `PricingError`; `open_store` forwards org key, name key id, pricer, `create`, and `adopt_key_ids` only when asked (a store without adoption → `UsageError`), `create=False` on a missing file, missing store module |
| `test_ingest.py` | `ingest_paths` into `MemoryStore` with fake adapters registered for the test: auto sniff, explicit adapter, unknown / not-installed adapter, unrecognized file, directories read whole by their one adapter, per file when mixed or when the adapter opens files only (hidden members skipped), same file once, merged notes (counts summed, quarantine note added once, key-id mismatch noted), a non-`IngestResult` → `ContractViolation`; **deferral** re-read once (no loop back) and deferral to a missing adapter as `dq.adapter_unavailable`; **records persisted after the ledger ingest** (a `MemoryStore(adopt_key_ids=True)` adopts the bundle key first, and the same `persist` before the ingest would drop the licenses), records under another key id noted, no record store → `dq.records_not_persisted`, record stores opened lazily on the ledger file of an `open_store` store (only when records exist), missing record-store module; naive usage kept as `naive:<model>:<bucket>` source stats; `IngestOptions` completed from the Env; `ingest_options` |
| `test_shards.py` | `map_shards` jobs=1 vs jobs=2 identical and in shard order; the pool really uses other processes; workers open the store **read-only by path** and results equal the sequential run (also through `functools.partial`); the sequential store is scoped, closed and restored for nested calls; the worker initializer; empty / invalid arguments; unpicklable functions with a pool; exceptions propagate |
| `test_bill.py` | `bill_summary` on `MemoryStore`: exact, allowance (subscription only) and pool (both Copilot paths) apart, also over a store that lumps list-equivalent lines; empty store and windows; contract basis; **k-anonymous breakdowns** (the 1-user team never appears, complementary suppression keeps totals), `k = 1`, audience `self` never suppresses (R-E10), group-by strings and duplicates, person dims → `PrivacyError`, unknown dims → `UsageError`; **ESR** hand-computed (17/38) over exact billed inferences with a stand-in `no_cache_equivalent_nano` (placeholder, unpriced, non-billable and subscription usage excluded), None without the engine or billed usage; **naive ratio** hand-computed (26/12), unpriced models and failing unit rates skipped, None without stats; footnotes |
| `test_thresholds.py` | R-E40: the org median `post_tokens` over the whole window (a per-shard median would differ), median rules (even counts half-even) and a **hypothesis** order-independence property; `analysis_thresholds` (`min_usd` from its Config field; a configured median wins; none without COMPACTION events); R-E24 `with_compaction_post` |
| `test_team_map.py` | `load_team_map` valid / malformed / missing files, no echo; **hypothesis** fuzz (only `UsageError`) |
| `test_guards.py` | no float literal or `float()` call in the owned modules (AST); the content canary (`core.builders.CANARY`, `CANARY_EMAIL`) absent from sources, notes and the bill summary; importing the modules imports no sibling package; `bill_summary` output byte-identical across two processes |
| `test_gate_rates.py` | **gate** (`importorskip tokenbill.rates.engine`): `build_env` loads the real `RateCard` (`assert_pricer_conforms`); a `--model-price` layer wins over the builtin row (pins the layer order); ESR through the real `no_cache_equivalent_nano` |
| `test_gate_store.py` | **gate** (`importorskip tokenbill.store.db`): `open_store` + `ingest_paths` + `map_shards(jobs=2)` over a real `SqliteStore` equals `jobs=1`; `bill_summary` on SQLite (totals, allowance, breakdowns, naive ratio from `source_stats`); `adopt_key_ids` reaches the store. Dry-run once against a temporary stand-in module (never committed): green |
| `conftest.py`, `support.py` | the `fake_adapters` fixture (fake adapters inserted into `core.registry.BUILTIN_ADAPTERS` for one test); the test source format, `PathStore` (a `MemoryStore` openable by path, like `SqliteStore`), `PathRecordStore`, `SlotStore`, top-level shard functions, `make_env` |

## Decisions where the SPEC is silent (also in the module docstrings; see `CONTRACT-CHANGE-WIRING.md`)

- **Config:** mappings merge per key across layers, sequences replace; unknown `TOKENBILL_*`
  variables are ignored (the prefix is shared with test switches such as `TOKENBILL_LOCAL_CORPUS`);
  relative paths in a config file resolve against the file's directory; `identity_mode` ∈
  {`central`, `two-stage`} (the collector modes of SPEC §5.4).
- **build_env:** explicit arguments win over the Config; without a collection key the org key
  doubles as the name key; `RateCard(layers, contract=…)` gets its layers lowest precedence first.
- **ingest_paths:** options are completed from the Env (name key, org key as principal key for
  `install` / `central-ingest`, `k = max(opts, env.k)`, allowlist union, clock); a directory is read
  whole when one adapter claims its files (file by file when several do, or when that adapter opens
  files only); an unrecognized file is a `UsageError`; the naive usage is
  kept as integer source stats so `bill_summary` can price it with the Env pricer later.
- **bill_summary:** totals from the store's priced columns; `allowance` / `pool` rebuilt from a
  `billing_path` grouping so a store that lumps list-equivalent lines still reports them apart; ESR
  over inferences priced EXACT on a billed basis; the naive ratio is store-wide (like the stats it
  reads) and prices both sides at each model's unit rates at its latest Claude Code context.
- **map_shards:** spawn start method on every platform; `fn` reaches the read-only store through
  `shard_store()`.

## Fixtures and provenance

No fixture files: every input is synthetic and written by the tests into `tmp_path` in a tiny
test-only JSON-lines format (`support.write_fake`), dated September 2026, with public test keys
(`bytes(range(…))`). No real transcript, export, key or network is involved (socket guard on).

## Unverified facts

None: WIRING transcribes no price, multiplier or field name. The ESR stand-in used by
`test_bill.py` (every input token at the uncached rate) is test-only; the real definition is RATES'
`no_cache_equivalent_nano`, exercised by the gate test.
