### F-CORE-C — Copilot core contracts (wave 1.5a; amendment of F-CORE's files)

**Goal.** Apply the additive core contract changes that let every Copilot package build against a frozen core
— including what the product-owner answers need: plan evidence and plan scenarios, the handoff source kinds and
registry entries, optional seat assignment kind, count-type configuration rows, the reconciler's decision
carrier, and the id formulas shared by the VS Code and CLI readers. Nothing is renamed or removed; every new
field has a default; only the three declared test amendments T-1 … T-3 touch existing tests. The exact list
(file, symbol, signature, default) is `CORE-AMENDMENTS.md` §2 items **C-1 … C-33**, reconciled with the merged
code at `v0.2` @ 99ba098; this brief is its work order. Read SPEC §2.4, §3.2–§3.12, §3.24–§3.25, Appendix E
(E.1, E.2); addendum §0, §1, §2.3–§2.4, §3.1–§3.5, §4, §5 (field names), §19.2 (the rate rows you transcribe),
§21.3; `CORE-AMENDMENTS.md` §0–§2 and §7.

**Owns.** Edits within F-CORE's files: `tokenbill/core/{records,types,money,labels,jsonl,ids,models,registry,
protocols,facts,builders}.py`, `tokenbill/core/facts.json`, `OWNERSHIP.toml`, `pyproject.toml`; new
`tokenbill/copilot/__init__.py` (docstring only); new tests `tests/v2/core/test_copilot_*.py`; the T-1 … T-3
edits of `tests/v2/core/test_{registry,types,records}.py`. Ownership check `--package F-CORE` under the
orchestrator's recorded grant (O-1).

**Consumes.** The merged core of gates 0 and F (E.1 additions: `EVENT_ATTRS`, `Facts` accessors,
`all_detectors` dq behavior, `open_private`, …). No wave-1.5b or wave-2 module; `core.findings.product_family`
and `core.catalog.FAMILY_EXCLUSIONS` are imported lazily inside `run_detectors` and tolerated missing.

**Provides.** Details in C-n.
- `records`: Copilot billing paths and class `pool` (C-1), `copilot_compliance` extra key (C-2),
  `PricingContext.routing/compliance/context_tier` (C-3), `RAW_USAGE_ENUMS` / `RAW_USAGE_NUMERIC` / `record_fields` (C-4),
  `CostLine` Copilot fields and closed vocabularies (C-5), aggregate source kinds and dims (C-6),
  `OutcomeAggregate.extra` (C-7), event attrs (C-8), the Copilot vocabularies incl. `EDITOR_FAMILIES`,
  `ACTIVITY_KEYS`, `CONFIG_KINDS` (+ `seat_counts`, `activity_counts`, `plan_quota`), `CONFIG_KEYS` (C-9),
  `LicenseSnapshot` (`assigned_via_team: bool | None`, source kinds seats / activity report),
  `ActivityDay`, `ConfigSnapshot` (source kinds incl. `tokenbill.admin_answers`, `tokenbill.copilot_export`),
  `record_key` (C-10).
- `types`: `IngestResult` / `IngestOptions` additions incl. `EXPERIMENTAL_FLAGS` (C-11, C-12); `PlanEvidence`,
  `PoolMonth` (+ `plan_source`, `plan_scenario`, `plan_conflict`), `BILL_LINES` (+ `seats.unknown_plan`),
  `CopilotBillLine` (+ `scenario`), `AdminAction`, `CopilotSummary` (+ `plan_status`, `plans_by_scenario`,
  `editor_split`), `FocusRow` (C-13); `AnalysisContext` += licenses, activity, config, outcomes, pools, plans,
  reconciled_channels, recon_decisions (C-14); `PricedTotal.pool`, `ClusterDay.pool_nano` (C-15);
  `Finding.headroom`, `ActionPlan.pool_headroom_monthly`, target / scope domains (C-16);
  `ReconciliationReport.decisions` (C-17); `RunResult.copilot` (C-18).
- Helpers: money (C-19), `figure_json` / `combine_weakest` (C-20), exact JSON (C-21), `natural_id`,
  `copilot_session_key`, `copilot_lane_key` (C-22), `normalize_copilot_model`, `is_copilot_resource` (C-23).
- `registry`: 14 Copilot adapters incl. `copilot-export` and `github-copilot-activity-report`, 3 detectors,
  4 convention modules, `sniff_adapter(…, notes=…)` (C-24); detector attributes and
  `run_detectors(…, aggregates_only=…)` with extension gating and family filtering (C-25); `ArgvAlias`,
  `ExtensionSpec`, `EXTENSIONS["copilot"]` (C-26).
- `protocols`: `ChannelReconciler`, `SectionRenderer`, `ExtRecordStore`, `LedgerStore.count_users(source=…)`
  and the separate `LedgerStats` protocol for `source_stats` (C-27).
- `facts.json` top-level `copilot` section and `facts.py` accessors (C-28); builders incl. `CANARY_LOGIN`,
  `make_plan_evidence` (C-29); SPEC list amendments (C-30); packaging (C-31); ownership rows incl.
  CP-HANDOFF and CP-VSCODE (C-32).

**Build.**
1. Records and types exactly as C-1 … C-18, with `__post_init__` validators (closed vocabularies, `p_` / `h_`
   regexes, sorted tuples, finite decimal strings) and `to_json` / `from_json` round trips; `ConfigSnapshot`
   refuses any attr value shaped like a `p_` pseudonym.
2. `run_detectors`: `aggregates_only=None` keeps today's behavior for every existing detector (they have no
   `extension`, `aggregate` or `families` attributes); the two phases, the silent `ext:<name>` gating and the
   family filter per C-25.
3. `sniff_adapter(path, *, notes=None)`: unchanged skipping plus one `dq.adapter_unavailable` note per
   unimportable entry; `get_adapter` still raises for an explicit request.
4. `facts.json`: transcribe §19.2 (current + history) row by row in the merged `_rate_row` shape
   (`long_context.threshold`), plus every other `copilot` key of C-28, each entry with `source`, `finding`,
   `verified_on: "2026-09-23"`, `verification: "research"` (R-E19). Top-level sections stay byte-identical.
5. Apply T-1 … T-3 and nothing else to existing tests; run the full suite (3.10 and 3.13) before handing over.

**Acceptance tests** (`tests/v2/core/test_copilot_*.py`).
- Round trips and validators for every new or changed record, incl. `LicenseSnapshot(assigned_via_team=None,
  source_kind="github.copilot_activity_report", plan="unknown")`, count-kind `ConfigSnapshot`s whose
  `record_key`s differ by attrs, and a `run_flags` snapshot with `plan.enterprise`.
- `billing_class` table; `PricingContext` domain checks; `CostLine` rejects a non-`h_` repo and an unknown
  pseudo; `ConfigSnapshot` rejects a `p_`-shaped attr value.
- `normalize_copilot_model` test table (addendum CA-20) and `normalize_model(…, "github")` delegation;
  `is_copilot_resource` truth table (default names, configured names, scope prefix, attribute prefix, foreign).
- `credits_str_to_nano("42.726213") == (427262130, 0)`, `nano_aiu_to_nano(23_284_800_000) == (232_848_000,
  0)`; `combine_weakest` cases (all INVOICE → INVOICE; INVOICE + EXACT LIST → EXACT LIST; any ESTIMATED →
  ESTIMATED; LIST_EQUIVALENT → `ContractViolation`); `natural_id`, `copilot_session_key`, `copilot_lane_key`
  stability.
- `run_detectors` with toy detectors: phase selection; an `extension="copilot"` detector silent without
  `ext:copilot`; family filter with a stub `product_family`; missing `core.catalog.FAMILY_EXCLUSIONS` tolerated.
- `sniff_adapter` with a registry entry pointing at a missing module → dq note, no exception.
- `isinstance(MemoryStore(), LedgerStore)` still true (no new `LedgerStore` member); `LedgerStats` is
  `runtime_checkable`.
- Facts: loads; every `copilot.*` entry has the provenance fields; `rows_for("claude-opus-5-5")` still one row;
  no float anywhere; the no-float scan covers the C-30 module list (absent modules skipped).
- `OWNERSHIP.toml` contains every C-32 row; `scripts/check_ownership.py --package F-CORE` clean; the 207 v0.1
  tests and every wave-0/1 test green with only T-1 … T-3 edited.

**Size.** ~2.3k LOC including tests.
