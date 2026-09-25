# CONTRACT-CHANGE-CP-STORE-1: what the frozen core leaves open for the Copilot record store and enricher

**Raised by:** CP-STORE (wave 2b), for the orchestrator / contract owner. Nothing here blocks
CP-STORE: each item is implemented against the current contract as described; the proposals are
additive changes for a later core window.

## 1. Report conventions are decided per file, but report rows carry no file

CP-RECON decides `convention:<source_id>` per AI usage report file (C-17). `CostLine` has no source
field and the report's cell aggregates (`github.ai_usage_report`) have none either (CP-BILL: one
aggregate per day × team × cost center × org × model × SKU × routing × speed × pseudo, natural ids,
so overlapping exports merge). The only link is the coverage aggregate
`github.ai_usage_report.coverage` (one per file × day, dim `source`).

**Implemented (CP-STORE `tokenbill.copilot.enrich.day_conventions` / `decided_cells`):** each day
takes the decision of the file covering it — the most recently fetched coverage row when exports
overlap (the file whose rows won latest-fetch-wins); a report aggregate's own `source` dim is used
when a day has no coverage row; `excl` without a file or decision; `undecidable` → `excl` plus
`dq.copilot_convention_undecidable` (R-E46). Cells are built with `core.pool.build_cells` once per
convention over the days that carry it, which equals `build_cells(convention=c)` when every day
carries `c`.

**Needed agreement:** CP-BILL's coverage `source` dim must be the `SourceInfo.source_id` that CP-RECON
uses in `convention:<source_id>` (both read the same ledger rows, so this holds when CP-RECON takes
its file ids from those dims). CP-DET-USAGE, which also builds cells from `ctx.aggregates` /
`ctx.cost_lines` / `ctx.recon_decisions`, needs the same day → file rule.

**Proposal:** move `day_conventions` / `decided_cells` (≈ 60 lines, pure) into `core.pool` so every
package reaches one implementation (the pool module already owns `build_cells`).

## 2. The enricher has no data-quality channel

`ExtensionSpec.context_enricher` is `(store, record_stores, ctx, *, today, reconciled_channels,
recon_decisions=()) -> AnalysisContext` and `AnalysisContext` has no notes field, yet R-E46 asks the
enricher to "emit `dq.copilot_convention_undecidable`" and addendum §7.2 names
`dq.copilot_key_id_mixed`.

**Implemented:** the codes are appended to the affected pool months' `PoolMonth.notes` as
`"<code>: <content-free text>"` (both scenario months when a plan is unknown); `decided_cells` also
returns them as `DataQualityNote`s for direct callers.

**Proposal:** `AnalysisContext.notes: tuple[DataQualityNote, ...] = ()` (appended, default empty),
filled by enrichers and surfaced by the pipeline's dq section.

## 3. Pool entity mode has no source

`core.pool` supports `entity_mode="enterprise" | "org"`, but no run flag, `ConfigSnapshot` attr or
`IngestOptions` field says which applies. **Implemented:** `enrich_context(…, entity_mode=
"enterprise")` — an additive keyword the extension host never passes (so the host always gets
`enterprise`). **Proposal:** a `run_flags` key `entity_mode` (`enterprise` | `org`) read by the
enricher.

## 4. Counting people across principal key ids

R-E21 forbids joining principals under different key ids. `core.testing.MemoryRecordStore`
counts distinct principals over every stored row; `CopilotRecordStore.count_users` counts per key id
and returns the **largest** count (a lower bound, like `core.kanon.scope_counter` across stores), so
one person seated under the org key and under an adopted export key is never counted twice. The two
agree whenever the rows of the window share one key id (the handoff and the conformance suite).
**Proposal:** align the fake (F-KIT-C) with the lower-bound rule.

The enricher applies the same rule to seat counting: per month, seats are counted from the licenses
of one key id (the one with the most seat holders) and the month's pool months carry
`dq.copilot_key_id_mixed`; `ctx.licenses` keeps every row (per-person joins by string equality never
match across key ids).

## 5. Smaller readings (documented in the module docstrings and the README)

- `CopilotRecordStore(db_path, *, create=True, now_ms=None)`: `now_ms` (additive) fixes the audit
  timestamp for tests.
- The §7.2 DDL keeps `org TEXT NOT NULL DEFAULT ''`: a license with `org=None` is stored as `''` and
  read back as None (an `org=""` input — not a login — also reads back as None; `record_key` already
  treats both as one seat).
- The addendum's revision-3 `copilot_decisions` table is not created (C-17: decisions are not
  persisted).
- Audit rows go to STORE's `audit` table when the file has it (SPEC §7.1 DDL), else to
  `copilot_audit` (same columns); the brief's `assert_record_store_conforms(lambda p:
  CopilotRecordStore(p))` cannot pass on a fresh file (CONTRACT-CHANGE-KIT-C-1 §3) — the area uses the
  R-E45 two-argument factory, with a SPEC §7.1 `meta` stand-in until STORE merges and the real
  `SqliteStore` in the gate test.
- The enricher adds `licenses` / `activity` / `config` to `ctx.capabilities` for the record rows it
  puts into the context (so the Copilot detectors' any-of kind gates see them on a persistent store),
  besides `ext:copilot`.
