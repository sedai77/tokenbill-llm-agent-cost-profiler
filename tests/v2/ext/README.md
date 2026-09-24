# tests/v2/ext — F-EXT acceptance tests

Channel-extension host `tokenbill/core/extensions.py` (wave 1.5b; SPEC-v0.2-COPILOT §2.4, §3.8
CA-39; CORE-AMENDMENTS E-1; rulings R-E17, R-E18). Every generic caller reaches extension code (today
only GitHub Copilot) through this module; a missing extension module or resource degrades to one
`dq.extension_unavailable` note per hook and a normal return.

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/ext` (also on 3.10).
Coverage: `uv run --python 3.12 --extra dev coverage run -m pytest tests/v2/ext && uv run --python
3.12 --extra dev coverage report --include='tokenbill/core/extensions.py'` (100% at hand-off).

Every test that touches hooks registers extensions itself (`install(...)` fixture, a `monkeypatch` of
`core.registry.EXTENSIONS`), so the results do not depend on which wave-2 Copilot modules are merged.
Only the pure readers (`extensions`, `delegated_channels`, `rewrite_argv`) are also tested on the
shipped table (the Copilot extension as declared by F-CORE-C, C-26).

| file | covers |
|---|---|
| `test_host.py` | with the test-only extension `fake`: every host function resolves its hook lazily and calls it with the documented arguments (`open_record_stores` → `cls(db_path, create=…)`; `persist` → `put(result, principal_key_id=source.principal_key_id)` per store, counts summed, skipped without records; `retain` / `purge` sums; `run_reconcilers` with the `ChannelReconciler` keywords; `enrich` forwards `recon_decisions` **unchanged** (identity) and chains enrichers; `summarize`; `render_sections` in the three formats (`{name: object}` for JSON, empty sections left out); `focus_rows` owned channels include a channel with no rows; `showback`; `policy_packs` / `panel` dispatch); `rounding_remainders` = `None` for a store without `source_stats`, adapter → exact USD `Decimal` from `rounding_remainder_e18` for a `LedgerStats` store (`{}` without remainders; stats read only when a reconciler runs); `recon_decisions_of` merges two reports and raises on a conflicting key; every contract check (wrong return types, malformed entries, duplicate policy targets, unknown target / panel); one pass through every hook in pipeline order |
| `test_capabilities.py` | `capabilities_present`: `ext:<name>` from a cost line, an aggregate `channel` dim or a request on an extension channel, and from record-store licenses / activity / config in the window — incl. the brief's **activity-report-only handoff** (only record-store licenses, plan unknown) → `ext:copilot`; record-store attribution by name; cost lines / aggregates read once per call, request probes `LIMIT 1` style (iterator closed) and short-circuited |
| `test_missing.py` | R-E18: an extension whose every hook module is absent → exactly one note per hook per call and normal returns; several missing entries of one hook → one note; a missing extension never hides a present one; missing resource, a directory resource, a missing attribute in an existing module, a missing parent package, a module injected without a spec; `notes=None` (logged); wrong `notes` type; errors other than an unavailable module propagate (import-time `RuntimeError`, an `ImportError` raised by a *running* hook, hook exceptions) |
| `test_argv.py` | `rewrite_argv` on the shipped aliases: the brief's table (`scan --copilot --since X` → `copilot scan --since X`; `collect copilot-cli --out D` → `copilot collect --source cli --out D`; `me --copilot` → `copilot me`; `collect claude-code` and `scan --org` unchanged; `--copilot=1` not matched) plus `--` end-of-options, repeated triggers, first-position rule, input never mutated; first match wins in extension-name then declared order; **hypothesis**: never raises, idempotent, unmatched argv unchanged, every other token kept in order |
| `test_decisions.py` | **hypothesis**: `recon_decisions_of` is the sorted union, order independent and idempotent, and raises `ContractViolation` exactly when two reports disagree on a key |
| `test_count_users.py` | `count_users_fn` delegates to a stub `core.kanon.scope_counter` (ledger, record stores, window, `source_of` from `core.catalog.COUNT_SOURCE`, default `requests`); without `COUNT_SOURCE` → note `catalog:COUNT_SOURCE` and `requests`; without `scope_counter` → note `kanon:scope_counter` and a counter returning 0 (one- and two-argument forms) |
| `test_import_isolation.py` | a fresh interpreter: importing `tokenbill.core.extensions` imports only `tokenbill`, `tokenbill.common` and `tokenbill.core.*` (none of `pool`, `kanon`, `catalog`, `findings`, `testing`); the pure readers import nothing more; listing command modules / policy targets never imports a hook module |
| `test_gate_ext.py` | **gate** (`@pytest.mark.gate`, skipped until F-KIT-C is merged): `count_users_fn` with the real `scope_counter` / `COUNT_SOURCE`; `run_reconcilers` on the real `MemoryStore` once it implements `LedgerStats` (both passed on a throw-away overlay with F-KIT-C's in-progress `kanon` / `catalog` / `testing`, 2026-09-24; never committed) |
| `conftest.py`, `support.py` | the `install` fixture, call-log reset; the `fake`, `bare` and `ghost` extension specs, ledger stand-ins (`PlainLedger` without `source_stats`, `StatsLedger` with it), builders for contexts, run results and ingest results |
| `fake_ext/` | the test-only extension (addendum §3.8): `hooks.py` (one hook per `ExtensionSpec` field, recording its calls, plus deliberately wrong variants), `commands.py` (command module), `broken.py` (raises `RuntimeError` on import), `fake_rates.json` (rate-file resource) |

## Interpretations recorded here (the module docstring states them for the wave-2 callers; `CONTRACT-CHANGE-F-EXT-1.md` lists them for the wave-2 briefs)

- `notes` is keyword-only on every function that can resolve a module (brief: CA-39's positional
  `notes` becomes a keyword); `persist`, `retain`, `purge` and `capabilities_present` accept it for a
  uniform signature. `persist` takes no `accepted_key_ids` (withdrawn by CA-47 / R-E21).
- `rounding_remainders` keys are **adapter names** (`github-ai-usage`), the grouping
  `LedgerStats.source_stats(adapter=…)` offers; addendum §12's `rounding_remainders["github.ai_usage_report"]`
  names the source kind instead. Values are USD `Decimal`s (`rounding_remainder_e18` × 1e-18, exact).
- `policy_packs` calls the target's builder **without** the target argument (CP-WIRE brief:
  `policy_packs(store, record_stores, ctx, findings, result, *, out_dir, current, cohort_by,
  include_tradeoffs)`).
- `render_sections(…, "json")` returns `{<extension name>: <object>}` per extension.
- `focus_rows` owns an extension's channels whenever it declares a `focus_rows` hook, even when the
  hook is unavailable; a row on a channel the extension does not own is a `ContractViolation`.
- `command_modules` and `policy_targets` locate modules with `importlib.util.find_spec` (no import);
  `rate_verifiers` resolves each verifier with `core.registry.load`.
- `count_users_fn`'s dq details for missing core pieces are `kanon:scope_counter` and
  `catalog:COUNT_SOURCE` (not an extension name).

## Fixtures and provenance

No real data. Every record is synthetic, built in code with `core.builders` (`make_license`,
`make_activity`, `make_config`, `make_cost_line`, `make_aggregate`, `make_request`) and the area
helpers in `support.py`, dated September 2026. `fake_ext/fake_rates.json` is a three-key stand-in for
a `tokenbill/rates@1` file (never priced). No network, no keys, no transcripts.

## Unverified facts

None: F-EXT transcribes no prices, multipliers or field names. The stats key
`rounding_remainder_e18` (1e-18 USD units) is taken from addendum §5.1 / the CP-BILL brief.
