### F-EXT — Channel extension host `core/extensions.py` (wave 1.5b)

**Goal.** One small core module through which every generic caller (RATES, WIRING, OUT, RECON, CLI-LEDGER,
CLI-SAVINGS) reaches channel-extension code with one-line calls, degrading to a data-quality note when an
extension module or resource is missing. This keeps SPEC packages' Copilot increments tiny, prevents two
implementations of the same host logic, and is the only path by which the Copilot reconciler's decisions (the
report convention per file, `gross_is_list` per entity × month) reach the context enricher. Read SPEC §3.7,
§14.4, §15; addendum DC9, DC23, DC24, §2.4, §3.4 (CA-21 … CA-24), §3.8 (CA-39), §14.3, §15, §21.4;
`CORE-AMENDMENTS.md` item E-1 and rulings R-E17, R-E18.

**Owns.** `tokenbill/core/extensions.py`, `tests/v2/ext/**` (including the test-only fake extension under
`tests/v2/ext/fake_ext/`).

**Consumes.** F-CORE-C (`EXTENSIONS`, `ExtensionSpec`, `ArgvAlias`, protocols `ExtRecordStore`,
`ChannelReconciler`, `SectionRenderer`, `LedgerStore`, `LedgerStats`, types `FocusRow`, `RunResult`,
`ReconciliationReport` with `decisions`, `AnalysisContext` with `recon_decisions`, `DataQualityNote`, registry
`load`); `core.kanon.scope_counter` (F-KIT-C) imported lazily inside `count_users_fn`.

**Provides.** The CA-39 API — `extensions`, `delegated_channels`, `extension_rate_files`, `rewrite_argv`,
`command_modules`, `open_record_stores`, `persist`, `retain`, `purge`, `capabilities_present`,
`run_reconcilers`, `enrich`, `summarize`, `render_sections`, `focus_rows`, `showback`, `policy_targets`,
`policy_packs`, `panel`, `rate_verifiers`, `count_users_fn` — with these deltas:
- `enrich(store, record_stores, ctx, *, today, reconciled_channels, recon_decisions: tuple[tuple[str, str],
  ...] = (), notes) -> AnalysisContext` passes `recon_decisions` to each `context_enricher`;
- new `recon_decisions_of(reports: Sequence[ReconciliationReport]) -> tuple[tuple[str, str], ...]`: sorted
  union of every report's `decisions`; the same key with two different values → `ContractViolation`;
- `run_reconcilers(…)` computes `rounding_remainders` from `store.source_stats()` only when
  `isinstance(store, LedgerStats)` (else None).

**Build.**
1. Every function iterates `extensions()` in name order, resolves dotted paths with `core.registry.load`
   lazily, catches `ModuleNotFoundError` / `ImportError` / missing `importlib.resources` resources and appends
   one `DataQualityNote(code="dq.extension_unavailable", severity="warn", count=1, detail="<ext>:<hook>")`.
   No other exception is swallowed. Every function that resolves an extension module takes `notes:
   list[DataQualityNote] | None = None` as a keyword — CA-39 omitted it for `command_modules`,
   `capabilities_present`, `policy_targets`, `panel`, `rate_verifiers` and `count_users_fn`; they get it too.
   The pure data functions `extensions`, `delegated_channels` and `rewrite_argv` import nothing and take none.
2. `rewrite_argv`: first matching `ArgvAlias` wins; `position="first"` requires `argv[1] == trigger`;
   `position="any"` matches the trigger token anywhere after the verb (not inside a `--flag=value`); the
   trigger is removed and the target prefix (one or more tokens, e.g. `("copilot", "collect", "--source",
   "cli")`) replaces the verb; no match → argv unchanged.
3. `focus_rows` returns the rows and the set of channels its extensions own (`ExtensionSpec.channels`), so
   OUT can drop ledger rows of those channels even when an extension produced zero rows for a channel.
4. `capabilities_present` adds `"ext:<name>"` when any extension channel has cost lines, aggregates or
   requests in the window, or any record store holds licenses / activity / config in the window (cheap
   `LIMIT 1` style queries through the protocols).
5. `open_record_stores` instantiates each `record_store` class with `(db_path, create=…)`; `persist` routes
   `IngestResult.licenses/activity/config` and returns counts (callers persist **after** the ledger ingest,
   so a store that adopted a key id (R-E21) has it before the record stores check it).

**Acceptance tests.** With the fake extension registered by `monkeypatch`: every hook is called with the
documented arguments; missing module / missing resource → exactly one dq note per hook and a normal return;
`rewrite_argv` table (`scan --copilot --since X` → `copilot scan --since X`; `collect copilot-cli --out D` →
`copilot collect --source cli --out D`; `me --copilot` → `copilot me`; `collect claude-code` unchanged; `scan
--org` unchanged; `--copilot=1` not matched); `focus_rows` owned channels include a channel with no rows;
`count_users_fn` delegates to a stub scope counter; `enrich` forwards `recon_decisions` unchanged;
`recon_decisions_of` merges two reports and raises on a conflicting key; `run_reconcilers` with a store lacking
`source_stats` passes `rounding_remainders=None`; `capabilities_present` sees `ext:copilot` for a store with
only record-store licenses (an activity-report-only handoff); importing `core.extensions` imports no wave-2
module (`sys.modules` check).

**Size.** ~0.85k LOC including tests.
