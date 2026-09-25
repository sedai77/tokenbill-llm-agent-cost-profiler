# CONTRACT-CHANGE-CP-RECON-1 — the `<source_id>` of `convention:` decisions (request for a ruling)

**Status:** implemented by CP-RECON against the current contract; no core file changes. Needs a ruling
(orchestrator / contract owner) so CP-STORE's enricher, CP-BILL's adapter and CP-SYNTH-W's writer agree.

## What

CORE-AMENDMENTS C-17 defines the decision key `convention:<source_id>` "per AI usage report file", and
CP-STORE builds cells "per source with the decided convention (`excl` when none)". Nothing in the core
says how a reader maps a stored report row back to its file: `CostLine` and `UsageAggregate` carry no
source id, and report rows use natural ids so overlapping exports supersede each other (latest fetch
wins). The only per-file record is the coverage aggregate of addendum §5.1 rule 7: one
`UsageAggregate(source_kind="github.ai_usage_report.coverage")` per (file, day) with dims `channel` and
`source`.

## Rule CP-RECON implements (`tokenbill.copilot.recon.report_sources`)

1. `<source_id>` is the value of the `source` dim of the file's coverage aggregates (CP-BILL writes it;
   no control characters).
2. A report **day** belongs to the source whose coverage aggregate for that day has the largest
   `fetched_ms` (ties: the lexicographically larger source id). Every report row and token aggregate of
   that day is read under that source's decided convention.
3. Report days without any coverage aggregate (builder-made data, pre-coverage exports) belong to the
   pseudo source `github.ai_usage_report` (`recon.FALLBACK_SOURCE_ID`), whose decision key is
   `convention:github.ai_usage_report`.
4. Undecidable → `excl` (ruling R-E46).

## Why a ruling

CP-STORE must not import CP-RECON (wave-2 packages never import each other), so it has to apply the
same rule on its own. Proposed ruling text for SPEC Appendix E (number assigned by the orchestrator):
"**(report sources):** the `source_id`
of `convention:` decisions is the `source` dim of `github.ai_usage_report.coverage` aggregates; a
report day belongs to the latest-fetched coverage source of that day (ties: larger id); days without a
coverage aggregate use `convention:github.ai_usage_report`."

## Two further notes (no change requested for v0.2)

- `ReconciliationReport` has no data-quality notes field. The Copilot reconciler's
  `dq.recon_schema_unverified` / `dq.copilot_convention_undecidable` are derived from the report by
  `recon.report_notes(report)` and printed labels by `recon.verdict_label(report, channel)`
  (`reconciled (synthetic; schema unverified)`, `… (totals only; …)`); renderers can call them, or
  derive the same from `ChannelVerdict.mapping_verified` and the `convention:*=undecidable` decisions.
  A `notes` field on the report would be the cleaner v0.3 contract.
- `build_copilot_panel(…, arms)` has `arms: Mapping[str, str] | None = None` (the brief lists it
  without a default); `core.extensions.panel` forwards keyword arguments unchanged, so both call forms
  work.
