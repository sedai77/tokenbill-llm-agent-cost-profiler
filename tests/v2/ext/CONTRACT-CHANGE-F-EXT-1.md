# CONTRACT-CHANGE-F-EXT-1 — host decisions where CA-39 / E-1 are silent (for the wave-2 briefs)

Raised by F-EXT (wave 1.5b) under SPEC §21 #3. Nothing here changes a core field or signature; these
are the readings `tokenbill/core/extensions.py` implements where the text leaves a choice, listed so
the orchestrator can paste them into the briefs of the callers (CP-RECON, CP-OUT / OUT, CP-WIRE,
CLI-LEDGER, CLI-SAVINGS, RATES) before wave 2 starts.

1. **`rounding_remainders` keys (CP-RECON, CLI-LEDGER, RECON).** `run_reconcilers` passes, for a
   store implementing `core.protocols.LedgerStats`, a `dict` **adapter name → USD `Decimal`** (exact:
   Σ `stats["rounding_remainder_e18"]` × 1e-18 of that adapter's sources, via
   `source_stats(adapter=<name>)` for every registered adapter with a non-zero remainder); `{}` when
   no source recorded the stat; `None` for a store without `source_stats`. Addendum §12 writes
   `rounding_remainders["github.ai_usage_report"]` (a source kind); the ledger groups stats by
   adapter, so CP-RECON reads `rounding_remainders.get("github-ai-usage")`.
   *Proposed brief text (CP-RECON):* "`copilot_rounding` = `rounding_remainders["github-ai-usage"]`
   (USD `Decimal`, converted with `core.money.decimal_to_nano`), absent key → 0."
2. **`render_sections(result, "json")` (OUT, CP-OUT).** Returns one `{<extension name>: <object>}`
   dict per extension whose renderer returned an object (`None` left out), so OUT can
   `doc.update(item)` for each and the result@2 key equals the `RunResult` slot (`"copilot"`).
   Terminal / HTML return the non-empty strings in extension-name order.
3. **Policy builder call (CP-WIRE, CLI-SAVINGS).** `policy_packs(target, store, record_stores, ctx,
   findings, result, *, out_dir, current, cohort_by, include_tradeoffs, notes=None)` calls the
   target's builder **without** `target`, as the CP-WIRE brief defines the hook:
   `builder(store, record_stores, ctx, findings, result, out_dir=…, current=…, cohort_by=…,
   include_tradeoffs=…)`. A target no extension declares → `UsageError`; an unavailable builder → `[]`
   plus a note.
4. **`notes`** (`list[DataQualityNote] | None = None`) can be passed by keyword everywhere (brief
   item 1) and, where CA-39 lists it positionally, also by position: `extension_rate_files(notes)`,
   `persist(record_stores, result, notes)`, `showback(result, out_dir, formats, notes)`. `persist` has
   no `accepted_key_ids` (withdrawn, CA-47 / R-E21).
5. **Listings vs resolution (CLI-LEDGER, CLI-SAVINGS, RATES).** `command_modules()` (verb = extension
   name → module) and `policy_targets()` only locate modules (`importlib.util.find_spec`), so a parser
   can be built without importing extension code; `rate_verifiers()` resolves each verifier with
   `core.registry.load`, so `pricing verify` can load every path it returns.
6. **Owned FOCUS channels (OUT).** `focus_rows` owns the channels of every extension that declares a
   `focus_rows` hook, also when the hook module is unavailable (OUT then emits no rows for those
   channels instead of collector-lane detail); a row on a channel its extension does not own raises
   `ContractViolation`.
7. **Record-store attribution in `capabilities_present`.** A record store belongs to the extension
   whose name equals the store's `name`; a store named after no extension belongs to every extension
   that declares a `record_store`. CP-STORE's `CopilotRecordStore.name` should be `"copilot"`.
8. **Ledger reads of `capabilities_present` (STORE).** Requests on extension channels are found
   with one `store.aggregate(since_ms=…, until_ms=…, group_by=("channel",))` (SPEC §7.2 whitelists
   `channel` for SqliteStore's `GROUP BY`), not with an `iter_requests(where={"channel": …})`
   filter, which SPEC §7.2 does not promise (`channel` is an inference column). Cost lines and usage
   aggregates are read through `cost_lines(None, …)` / `aggregates(None, …)` (the protocols offer no
   channel filter), each at most once per call and only when the earlier source did not decide.
   `aggregate` groups billable inferences (`billable` not False), so a window whose only data on an
   extension channel is non-billable requests does not by itself add `ext:<name>`.
9. **Wrong result types.** A list-returning hook (`focus_rows`, `showback`, the policy builder, the
   panel builder) that returns `None`, a mapping or a string, and a record store whose `put` counts
   or `retain` / `purge` totals are not non-negative ints, raise `ContractViolation`.
