### CORE-AMENDMENTS — ordered core changes between wave 1 and wave 2 (Copilot; reconciled with `v0.2` @ 99ba098)

Not a work package: the exact, ordered list of core changes the orchestrator must apply between merge gate F
and wave 2 (SPEC rulings R-E4, R-E15), checked line by line against the merged code of branch `v0.2`
(F-CORE + F-SEM + F-KIT at 99ba098). Items are numbered **O-** (orchestrator), **C-** (F-CORE-C, wave 1.5a),
**S-** (F-SEM-C), **K-** (F-KIT-C), **E-** (F-EXT), **P-** (F-POOL) (wave 1.5b), **A-** (amendments to SPEC
packages) and **T-** (existing tests that change). Where this list and addendum §3 differ, this list wins: it
reflects the merged code and the product-owner answers of 2026-09-23 (no admin access → handoff kit; plan
unknown → detect or show both scenarios; developers on VS Code and IntelliJ).

**Precedence (final consistency pass, 2026-09-23; addendum revision 3.1 states the same rule).** This list and
the package briefs of this directory are binding for every wave-1.5 contract (signatures, fields,
vocabularies, registry entries, rulings, gate F') and for every package boundary and file ownership. The
addendum's revision-3 text — §3 (CA-1 … CA-48), §5.13–§5.18, §7.1–§7.2, §15, §18 team sizes, §22 function
signatures and manifest keys — is the design background: wherever it differs from this list or a brief, or
names a contract that neither contains, the list / brief wins. The revision-3 items that are **not** carried
over are listed under "Addendum revision 3 → this list" below, so no builder has to guess.

**State of the code base (read before applying anything).**
- Merged: `tokenbill/core/*` of F-CORE, F-SEM, F-KIT. There is **no** `core/extensions.py`, `core/pool.py`,
  `tokenbill/copilot/`, `RAW_USAGE_ENUMS`, `ExtensionSpec`, `LicenseSnapshot` or `aggregates_only` yet.
- Wave-2 worktrees already started at 99ba098 (R-E15): ADMIN, BLOCK, CC, DETECT-CACHE, REPLAY, SYNTH-FLEET,
  SYNTH-ORACLE, TELEM, VERIFY. Not started: RATES, TRACE, RECON, STORE, DETECT-OTHER, PLAN, OUT, WIRING, and
  wave 3. Consequence: TELEM's and REPLAY's addendum §21.4 amendments are **late change requests** (A-4, A-6),
  not brief text; everything added below is additive with defaults so the started packages keep building.
- Pinned wave-0/1 tests that constrain the design (all verified in the tree): `tests/v2/kit/test_catalog.py`
  pins `LEVERS` to the 24 SPEC levers in order, `replay ∈ {usage, block, none}`, non-empty grids unless
  `replay == "none"`, `patch_keys ⊆ ALLOWLIST`, `ALLOWLIST == facts.settings_keys` with length 31, and
  `PROMOTIONS == (openai promo,)`; `tests/v2/core/test_facts_evidence.py` pins top-level `settings_keys`
  (every key target `claude-code`), the modifier id set and `when` keys, exactly one top-level promotion, and
  `rows_for(model)` having one row per Anthropic model; `tests/v2/sem/test_findings.py::test_cohort_key` pins
  the 3-tuple; `tests/v2/sem/test_cache_rules.py` pins `RulesTable.channels()`;
  `tests/v2/core/test_registry.py` pins the adapter list, 20 detectors and `CONVENTION_MODULES`;
  `tests/v2/core/test_types.py` pins `PricedTotal` field names and `len(RunResult fields) == 21`. Therefore
  **all Copilot levers, allowlist keys, modifiers, promotions and rate rows live in separate Copilot tables**
  (`facts.json` `copilot` section, `COPILOT_LEVERS`, `COPILOT_ALLOWLIST`), and only the tests listed in T-1 …
  T-3 change.

**Order of application.** O-1 … O-5 → F-CORE-C applies C-1 … C-32 in document order (records → types →
helpers → registry / protocols → facts / builders → lists, packaging, ownership) plus T-1 … T-3, runs the full
suite, merges → F-SEM-C, F-KIT-C, F-EXT, F-POOL branch from that merge and run in parallel → gate F' (§4) →
A-1 … A-12 pasted into the not-yet-started briefs, A-4 / A-6 filed as late change requests → wave 2 starts
(the 19 Copilot briefs of this directory beside the SPEC packages).

**Addendum CA → this list.** CA-1 → C-1; CA-2 → C-2, C-3; CA-3 → C-4; CA-4 → C-5; CA-5 → C-6; CA-6 → C-7;
CA-7 → C-8; CA-8 → C-9, C-10; CA-9 → C-11; CA-10 → C-12; CA-11 → C-14; CA-12 → C-15; CA-13 → C-16;
CA-14 → C-13; CA-15 → C-18; CA-16 → C-19; CA-17 → C-20; CA-18 → C-21; CA-19 → C-22; CA-20 → C-23;
CA-21 → C-24, C-25; CA-22 → C-26; CA-23, CA-24 → C-27; CA-25, CA-26 → C-28; CA-27 → C-29; CA-28 → C-30;
CA-29 → C-31; CA-30 → C-32; CA-31 … CA-34 → S-1 … S-5; CA-35 … CA-38 → K-1 … K-6; CA-39 → E-1; CA-40 → P-1.
New (not in the addendum): C-17, `PlanEvidence` and the plan fields of C-13, the count kinds of C-9, the
handoff registry entries of C-24, `LedgerStats` (C-27), K-4's R-E10 fix, K-5's key-id adoption.

**Addendum revision 3 → this list** (CA-41 … CA-48 and the in-place revision-3 edits of CA-1 … CA-40; items
marked *withdrawn* are not built).
- **CA-41** vocabularies → C-9 (C-9's spellings bind, e.g. `EDITOR_FAMILIES` with `visual_studio`,
  `github_com`, `mobile`); `editor_family(raw)` lives in `core.catalog` (K-3, facts-backed), not in
  `core.records`; `PLAN_SOURCES` = the `PlanEvidence.source` domain of C-13; `assigned_via_team: bool | None` and
  the activity-report / admin-answers source kinds → C-10; `record_fields(cls)` → C-4.
- **CA-42** `core/spans.py` *withdrawn*. CP-OTEL owns the single Copilot span mapping (`copilot_otel.py`,
  `copilot_vscode.py`) and registers `github_copilot.otel`; CP-VSCODE writes content-free extracts in the VS Code
  DDL / OTel-JS dialect that CP-OTEL's adapters read, so no two wave-2 packages import each other.
  `CONVENTION_MODULES` per C-24.
- **CA-43** → C-28 (`skus` with seat plans, `plan_quota_map`, `editor_families`, `vscode_traces`). The
  `handoff` auth facts are *withdrawn*: the permission table is CP-PULL's `AUTH_TABLE` plus CP-HANDOFF's guide
  text, cross-checked by CP-WIRE's gate test.
- **CA-44** → C-24 (`copilot-export` and `github-copilot-activity-report` are appended, not inserted first: the
  export sniff needs `manifest.json` at offset 30 of a zip, which no other adapter claims) and C-29
  (`make_plan_evidence`). No `copilot-admin-answers` adapter: the answers file is read through `--answers` by
  `tokenbill.copilot.admin_answers.parse_answers` (CP-HANDOFF).
- **CA-45** → S-2 (dollar fields LIST or LIST_EQUIVALENT; INVOICE only in `cost_observed`).
- **CA-46** → K-4 (ruling R-E16; the Copilot parent chain has no `editor_family` level and `editor_family` is not
  a scope dim).
- **CA-47** → K-5 and A-2 (ruling R-E21: boolean `adopt_key_ids`, at most one adopted bundle key id per store).
  *Withdrawn:* `core.keys.accepted_key_id`, `IngestOptions.adopt_key_ids`, the `accepted_key_ids` argument of
  `ExtRecordStore.put` / `extensions.persist` (record stores read the accepted key ids from STORE's `meta`).
- **CA-48** → P-1 (`detect_plans(cost_lines, licenses, config, *, month, entity_mode="enterprise") ->
  list[PlanEvidence]`; report-quota evidence = `plan_quota` config rows emitted by CP-BILL under the flag
  `copilot-report-quota`; no `plan_scenarios()`; `copilot_allowance("unknown")` raises `UsageError`).
- **In-place revision-3 edits.** CA-10 flags `copilot-ai-usage-quota` / `copilot-dashboard-ndjson` → C-12
  `EXPERIMENTAL_FLAGS` (`copilot-report-quota`; the dashboard NDJSON export needs no flag and is accepted with dq
  codes by CP-ORGDATA). CA-11 `channel_decisions`, CA-14 `ChannelDecision`, CA-23 `put_decisions` /
  `decisions()` and the `copilot_decisions` table → C-14 `recon_decisions` + C-17
  `ReconciliationReport.decisions` (string pairs, not persisted: every command that enriches a context runs the
  reconcilers first, A-10, A-11). CA-14 `PlanEvidence.hint` and `CopilotSummary.scenarios` are not fields (CP-OUT
  renders the hint; the scenarios are the keys of `plans_by_scenario`). CA-32 `CONSERVATIVE_TTL_CHANNELS` → S-4
  `CacheRules.ttl_semantics_known`. Addendum §2.3 / §21.6 "wave-0/1 tests unedited" → T-1 … T-3 (the registry
  list and the type-field pins make these three edits unavoidable).

---

#### 1. Orchestrator, before wave 1.5a

- **O-1** Record in `tests/v2/kit/RULINGS.md` the one-time grants: the contract owner (F-KIT agent) runs
  F-CORE-C on F-CORE's files (`scripts/check_ownership.py --package F-CORE`) and F-KIT-C on F-KIT's files.
- **O-2** Append a new **E.3 "After wave 1.5 (Copilot)"** to SPEC Appendix E with rulings R-E16 … R-E23. The
  revision-2 labels R-E5 … R-E8 collided with E.2 and are renumbered; this list is the single text of E.3
  (addendum §3.10, revision 3.1, mirrors it verbatim; every brief uses these numbers):
  - **R-E16** (was addendum R-E5; kanon). A finding whose scope has a `product` dim is exempt from
    k-suppression iff its count source (`core.catalog.COUNT_SOURCE`) is `"entity"` and its scope dims are ⊆
    {`product`, `entity`, `org`, `model`, `sku`, `plan_scenario`}; every other `product`-scoped finding uses its
    count source and re-scopes on the Copilot parent chain, **whatever its category** (this overrides R-E9's
    category-`aggregate` exemption for `product`-scoped findings only). Per-user budget facts are published only
    as counts at team (k ≥ 5) or entity level.
  - **R-E17** (was R-E6). Aggregate detectors run once per run (`run_detectors(..., aggregates_only=True)`);
    per-shard calls use `aggregates_only=False`; shard invariance applies to lane detectors only.
  - **R-E18** (was R-E7). Iterating callers (sniffing, detector listing, convention loading, extension hooks,
    extension rate files) skip unimportable modules and missing resources with a dq note; explicit
    single-entry requests raise.
  - **R-E19** (was R-E8). Copilot facts carry `verification: "research"` until the Copilot release gate upgrades
    them to `"primary"` or ships them disabled.
  - **R-E20** (amends E.2 R-E8 for Copilot). For findings whose scope has `product=copilot` or
    `billing_class=pool`, the one-basis rule is replaced by per-field domains: `cost_observed` ∈
    {LIST_EQUIVALENT, LIST, INVOICE}; `recoverable`, `recoverable_shapley`, `projected_monthly` ∈ {LIST,
    LIST_EQUIVALENT}; `headroom` = LIST_EQUIVALENT; CONTRACT never; PROVIDER_ESTIMATE only in data-quality
    findings (R4 unchanged). `headroom` on any other finding is a `ContractViolation`. Non-Copilot findings keep
    R-E8 exactly.
  - **R-E21** (stores; owner answer 1). A store opened with `adopt_key_ids=True` — with or without its own org
    key — adopts the `principal_key_id` and `name_key_id` of the first ingested **`copilot-export` bundle**
    (`SourceInfo.adapter == "copilot-export"`; never a key id of any other source, so a developer's install
    key is never adopted by accident). It records them in `meta` (`adopted_key_id`, `adopted_name_key_id`; a
    store without its own key also sets `org_key_id` / `name_key_id` to them and `org_key_mode = "adopted"`)
    with an `audit` row, and keeps `p_` / `h_` values whose key id is its own or the adopted one; values under
    any other key id are nulled with `dq.principal_key_mismatch` / `dq.name_key_mismatch`, and a second bundle
    under a different key id is refused (`UsageError`; CLI exit 2, hint "use the same export key every month").
    `r_` / `c_` principals (VS Code / CLI collector files) are pseudonymized with the store's own org key and
    raise `PrivacyError` in a store without one. Rows under the two key ids are never joined per person
    (record-store joins require equal key ids; bundle × collector analysis is by team only). Default
    `adopt_key_ids=False` keeps SPEC §7.2 / §7.6 unchanged. The rejected alternative (widening records to `c_`
    and re-keying with the owner's key) is not implemented.
  - **R-E22** (plan; owner answer 2). The Copilot plan is never assumed. `core.pool.detect_plans` decides per
    entity × month with precedence seat SKU lines > seats API `plan_type` > org billing `plan_type` > report
    quota (experimental) > admin statement; data always beats a statement; while any seat's plan is unknown
    every pool-dependent figure is computed and shown for both an all-Business and an all-Enterprise scenario,
    labelled ESTIMATED, side by side, never merged or chosen.
  - **R-E23** (late change requests). TELEM and REPLAY started before wave 1.5 build against the gate-F core;
    after gate F' they rebase onto the new core and apply A-4 / A-6 as recorded change requests
    (`tests/v2/kit/CONTRACT-CHANGE-COPILOT-TELEM.md`, `…-REPLAY.md`), gate-tested at gate 1. No other started
    package needs a Copilot change (DETECT-CACHE consumes `CacheRules`, which only gains a defaulted field).
- **O-3** Done in the final consistency pass (addendum revision 3.1): §3.10 mirrors O-2; §21.1–§21.3 carry the
  briefs' rows, sizes and ownership (incl. CP-HANDOFF, CP-VSCODE, `teammap.py` under CP-ORGDATA,
  `copilot_vscode.py` under CP-OTEL); the precedence rule and the revision-3 mapping above are stated at the top
  of the addendum; §6.1 `long_context.threshold`, the §11.3 hook name, §12.1 numbering, the §20 VS Code row and
  §21.6 release gate (2) were already fixed in revision 3. **Still open for the orchestrator:** regenerate
  `_copilot_state.json` from addendum §21 and this directory (it still holds revision 1).
- **O-4** Copy research inputs into the test-data areas their owners hold (paths relative to the scratchpad
  `copilot/` directory): `raw/yml/*.yml` (38 dated revisions) + `raw/yml/commits.txt` →
  `tests/v2/fixtures/copilot_rates/yml/` (CP-RATES); `src/gh-aw/actions/setup/js/fixtures/awf-v0.28.7-aic-token-usage.jsonl` →
  `tests/v2/fixtures/copilot_otel/gh_aw/` (CP-OTEL); the `CREATE TABLE` statements of
  `src/vscode/extensions/copilot/src/platform/otel/node/sqlite/otelSqliteStore.ts` →
  `tests/v2/fixtures/copilot_otel/vscode/DDL.sql` (CP-OTEL) and `tests/v2/fixtures/copilot_vscode/DDL.sql`
  (CP-VSCODE); the documented activity-report header (`raw/…metrics-data.md`, `last_surface_used` row) →
  `tests/v2/fixtures/copilot_handoff/README.md` provenance note (CP-HANDOFF).
- **O-5** Addendum §21.5 is binding (decided) so that **no package exceeds 3,000 LOC**: move
  `tokenbill/outputs/ccusage.py` + its tests from OUT to CLI-LEDGER (OUT 2,890; CLI-LEDGER 2,895 with A-10) and
  record the move in `OWNERSHIP.toml` (C-32); TRACE's A-3 must be net-neutral (derive the closed key sets with
  `core.records.record_fields` (C-4) and `RAW_USAGE_ENUMS`, deleting its hand-written lists) — the revision-2
  3,015-LOC allowance is withdrawn.

#### Package list after this document (22 addendum packages + 2 from the owner answers)

Wave 1.5a: F-CORE-C. Wave 1.5b: F-SEM-C, F-KIT-C, F-EXT, F-POOL. Wave 2 (19 Copilot packages beside the SPEC
packages): CP-RATES, CP-BILL, CP-ORGDATA, CP-PULL, **CP-HANDOFF** (new, ~2.5k), CP-LOCAL (non-blocking),
CP-OTEL, **CP-VSCODE** (new, ~1.7k), CP-STORE, CP-RECON, CP-DET-SEATS, CP-DET-USAGE, CP-DET-LANES, CP-PLAN,
CP-POLICY, CP-OUT, CP-WIRE, CP-SYNTH, CP-SYNTH-W. Every brief in this directory is ≤ 3k LOC. The admin guide
has one source and one generated copy: the text is CP-HANDOFF's packaged data
(`tokenbill/copilot/handoff_data/admin_guide.md`, printed by `copilot admin-guide`); `docs/COPILOT-ADMIN.md` is
INTEGRATION's byte-identical copy, checked by a test (A-12).

---

#### 2. Wave 1.5a — F-CORE-C (F-CORE's files; ownership check `--package F-CORE`)

All new fields are appended **after** the existing last field and have defaults; frozen/slots dataclasses stay
frozen/slots; no field is renamed or removed; `to_json` / `from_json` round-trip every new or changed record.

`tokenbill/core/records.py`
- **C-1** `BILLING_PATHS = (…existing 10…, "copilot_pool", "copilot_direct")` (appended after `"unknown"`);
  `COPILOT_BILLING_PATHS = ("copilot_pool", "copilot_direct")`; `BILLING_CLASSES = ("billed", "allowance",
  "pool")`; `billing_class(path)` → `"allowance"` iff `"subscription"`, `"pool"` iff `path in
  COPILOT_BILLING_PATHS`, else `"billed"` (docstring updated). `COPILOT_CHANNELS = ("github_copilot",
  "github_actions", "github_sandbox")`. `Lane.billing_class` follows unchanged code.
- **C-2** `EXTRA_KEYS += ("copilot_compliance",)` (values `none|data_residency|fedramp`; readers use
  `dict(attribution.extra).get("copilot_compliance")`).
- **C-3** `PricingContext` += `routing: str = "direct"` (`_one_of {"direct","auto","unknown"}`),
  `compliance: str | None = None` (`{"data_residency","fedramp"}`), `context_tier: str | None = None`
  (`{"default","long_context"}`). `provider` / `channel` stay free strings (`"github"`, `"github_copilot"`
  need no validator change).
- **C-4** New `RAW_USAGE_ENUMS: Mapping[str, frozenset[str]]` (none exists; `Attempt` checks only the 8 KiB
  size): `{"tokenType": {"input","cache_read","cache_write","output"}, "contextTier":
  {"default","long_context"}, "initiator": frozenset(), "interactionType": frozenset()}` where an empty set
  means "any token matching `^[A-Za-z0-9_.-]{1,64}$`"; plus `RAW_USAGE_NUMERIC = frozenset({"totalNanoAiu",
  "batchSize", "costPerBatch", "tokenCount"})`. Declarative (TRACE derives closed key sets from it; A-3); no
  validator reads it in wave 1.5. Also `record_fields(cls: type) -> frozenset[str]` — the JSON field names
  `to_json` emits for a core record dataclass (addendum CA-41), so TRACE derives its closed key sets instead of
  keeping hand lists (A-3, O-5).
- **C-5** `CostLine` += `quantity: str | None = None` (finite decimal string), `unit: str | None = None`,
  `cost_center: str | None = None`, `team: str | None = None`, `repo: str | None = None` (`h_` regex),
  `workload: str | None = None` (∈ `COPILOT_WORKLOADS`), `workflow: str | None = None` (`h_`), `routing: str |
  None = None` (`direct|auto|unknown`), `speed: str | None = None` (`standard|fast`), `pseudo: str | None =
  None` (∈ `COPILOT_PSEUDO`). `principal` keeps `_STORE_PRINCIPAL_RE` (`p_` only; bundles rely on R-E21).
  New constants `GITHUB_COST_TYPES = ("ai_credit.user", "ai_credit.direct", "ai_credit.legacy_pru", "seat",
  "actions", "sandbox", "code_quality.license", "metered.ai_credit", "rest.ai_credit", "rest.summary",
  "rest.usage", "other")`, `COPILOT_WORKLOADS = ("copilot_code_review", "copilot_cloud_agent",
  "agentic_workflow", "code_quality")`, `COPILOT_PSEUDO = ("code_review", "cloud_agent", "auto_unattributed",
  "unknown")`; `cost_type` stays a free string (validators do not close it).
- **C-6** `UsageAggregate`: no field change (`source_kind` and `dims` are free, sorted pairs). Document and
  export `COPILOT_AGG_SOURCE_KINDS = ("github.ai_usage_report", "github.ai_usage_report.coverage",
  "github.agent_tasks", "copilot.cli_rollup", "gh_aw.run")` and `COPILOT_AGG_DIMS = ("channel", "team",
  "cost_center", "organization", "model", "sku", "routing", "speed", "pseudo", "convention", "state",
  "artifact", "repo", "workflow", "source")`.
- **C-7** `OutcomeAggregate` += `extra: tuple[tuple[str, int], ...] = ()` (keys ⊆ `OUTCOME_EXTRA_KEYS =
  ("prs_merged", "prs_created_by_copilot", "prs_merged_created_by_copilot", "prs_reviewed_by_copilot",
  "copilot_suggestions", "copilot_applied_suggestions")`, sorted, non-negative ints), appended after
  `source_kind`; new source kind `"github.copilot_metrics"`.
- **C-8** `EVENT_ATTRS` (types, all may be None): COMPACTION += `copilot_trigger (str, None)`, `system_tokens
  (int, None)`, `tool_definitions_tokens (int, None)`; SESSION_META += `credit_limit_nano (int, None)`,
  `routing_mode (str, None)`, `context_tier (str, None)`. `_EVENT_VALUE_DOMAINS += {(COMPACTION,
  "copilot_trigger"): {"threshold","manual","context_limit_retry","memory_pressure","model_switch"},
  (SESSION_META, "context_tier"): {"default","long_context"}}`; COMPACTION `trigger` stays {`auto`,`manual`}
  (Copilot `manual` → `manual`, every other Copilot trigger → `auto`). CONTEXT_EDIT `edit_type
  "copilot_truncation"` and COST_STATE reporters `copilot.cli.checkpoint`, `copilot.otel.invoke_agent`,
  `copilot.vscode.turn`, `gh_aw.run_total` need no change (both attrs are unconstrained strings today). No new
  `LaneEventKind` (the test pins 13).
- **C-9** Vocabularies (moved here from addendum CA-36 so F-CORE-C's validators never import `core.catalog`,
  which F-KIT-C changes later): `LICENSE_PLANS = ("business", "enterprise", "unknown")`; `LICENSE_BUCKETS =
  ("0-7", "8-30", "31-90", "none_90d")`; `EDITOR_FAMILIES = ("vscode", "jetbrains", "visual_studio", "xcode",
  "eclipse", "neovim", "cli", "github_com", "copilot_app", "mobile", "other")`; `ACTIVITY_KEYS` (the fixed keys
  of addendum §5.5: `interactions, code_generation, code_acceptance, loc_suggested_add, loc_suggested_delete,
  loc_added, loc_deleted, cli_sessions, cli_requests, cli_prompts, cli_prompt_tokens, cli_output_tokens,
  app_sessions, app_requests, app_prompts, app_prompt_tokens, app_output_tokens, mcp_distinct,
  skill_distinct, custom_agent_distinct, plugin_distinct, slash_cmd_distinct, third_party_agent_jobs`) and
  `ACTIVITY_KEY_PREFIXES = ("feature:", "model:", "ide:")` (suffix `^[a-z0-9._-]{1,64}$`); `ACTIVITY_FLAGS =
  ("used_chat", "used_agent", "used_cli", "used_copilot_app", "used_cloud_agent", "used_code_review_active",
  "used_code_review_passive")`; `CONFIG_KINDS = ("budget", "budget_users", "cost_center", "org_settings",
  "run_flags", "seat_counts", "activity_counts", "plan_quota")`; `COUNT_CONFIG_KINDS = ("seat_counts",
  "activity_counts", "plan_quota")`; `CONFIG_SOURCE_KINDS = ("github.budgets", "github.cost_centers",
  "github.org_copilot_settings", "github.ai_usage_report", "tokenbill.cli", "tokenbill.admin_answers",
  "tokenbill.copilot_export")`; `CONFIG_KEYS: Mapping[str, tuple[str, ...]]` per kind — exact keys of addendum
  §5.4 for `budget`, `budget_users`, `cost_center`, `org_settings` (incl. `plan_type`,
  `seat_management_setting`); `run_flags`: `promo_eligible`, `compliance`, `paid_usage_policy`,
  `org_cli_billing_policy` and the prefixes `plan.`, `pool_seats.`, `billing_mode.`, `renewal_date.`,
  `capped_policy.`, `budget_stop.` (a key ending in `.` is a prefix); `seat_counts`: `team, plan, bucket,
  surface, assigned_via_team, pending_cancellation, created_over_30d, zero_cost_30d, n, n_people`;
  `activity_counts`: `team, month, n_people, interactions, cli_requests, cli_prompt_tokens, app_interactions`
  + prefix `ide:`; `plan_quota`: `month, quota, n_users`.
- **C-10** New records (frozen, slots, validators, round trip) exactly as addendum CA-8 with these deltas:
  `LicenseSnapshot.assigned_via_team: bool | None` (None = unknown, e.g. activity report); `source_kind: str
  = "github.copilot_seats"` ∈ {`github.copilot_seats`, `github.copilot_activity_report`};
  `last_authenticated_bucket` ∈ `LICENSE_BUCKETS ∪ {"unknown"}`; `plan` ∈ `LICENSE_PLANS`;
  `last_activity_surface` ∈ `EDITOR_FAMILIES` or None; `principal` must match `^p_[0-9a-f]{20}$`.
  `ActivityDay.counts` keys ∈ `ACTIVITY_KEYS` or a prefix key; `flags` ⊆ `ACTIVITY_FLAGS`.
  `ConfigSnapshot.kind` ∈ `CONFIG_KINDS`, `source_kind` ∈ `CONFIG_SOURCE_KINDS`, attrs keys per `CONFIG_KEYS`,
  values `str | int | bool | None`, no attr value may match `^p_[0-9a-f]{20}$` (R14: user-scope budgets carry
  team / cost center only — this corrects the CA-8 comment "the p_ user only inside attrs"). `entity_id` ∈
  `enterprise` | `org:<login>` | `cc:<name>` | `budget:<id>` | `run` | `admin_answers`.
  `record_key(rec) -> str`: license `(snapshot_date, product, principal, org or "")`; activity `(date_utc,
  product, principal)`; config `(kind, entity_id, date of snapshot_ms)` plus, for `COUNT_CONFIG_KINDS`, the
  sorted non-count attrs (every attr except `n`, `n_people`, `n_users`) joined as `k=v` — so several count rows
  per entity and day never collide. Empty string for None parts.

`tokenbill/core/types.py`
- **C-11** `IngestResult` += `licenses: list[LicenseSnapshot] = field(default_factory=list)`, `activity:
  list[ActivityDay] = …`, `config: list[ConfigSnapshot] = …` (after `naive_usage`).
- **C-12** `IngestOptions` += `cost_center_map: tuple[tuple[str, str], ...] = ()`, `otel_service_names:
  tuple[str, ...] = ()`, `experimental: frozenset[str] = frozenset()` (after `now_ms`). New
  `EXPERIMENTAL_FLAGS = frozenset({"copilot-store", "copilot-cli-otel-file", "copilot-jetbrains-otel",
  "copilot-report-quota"})`; unknown flags → `UsageError` in the CLI (not in the dataclass).
- **C-13** New result types (frozen, slots): `PlanEvidence(entity_id: str, month: str, plan: str (business |
  enterprise | mixed | unknown), source: str (seat_lines | seats_api | org_settings | report_quota |
  admin_statement | none), seats: tuple[tuple[str, int], ...] = (), conflict: bool = False, evidence:
  tuple[str, ...] = ())`; `PoolMonth` exactly as addendum CA-14 plus, appended, `plan_source: str = "none"`,
  `plan_scenario: str | None = None` (`business` | `enterprise` when the plan is unknown), `plan_conflict:
  bool = False`, and `seats_source` domain += `"seat_counts"`, `"report_users"`; `BILL_LINES` = the CA-14
  tuple with `"seats.unknown_plan"` inserted after `"seats.enterprise"`; `CopilotBillLine` + `scenario: str |
  None = None`; `AdminAction` as CA-14; `CopilotSummary` + `plan_status: tuple[PlanEvidence, ...] = ()`,
  `plans_by_scenario: tuple[tuple[str, ActionPlan], ...] = ()`, `editor_split: PublishedAggregate | None =
  None` (VS Code vs JetBrains, k ≥ 5); `FocusRow` as CA-14.
- **C-14** `AnalysisContext` (frozen, not slots) += `licenses: tuple[LicenseSnapshot, ...] = ()`, `activity:
  tuple[ActivityDay, ...] = ()`, `config: tuple[ConfigSnapshot, ...] = ()`, `outcomes:
  tuple[OutcomeAggregate, ...] = ()`, `pools: tuple[PoolMonth, ...] = ()`, `plans: tuple[PlanEvidence, ...] =
  ()`, `reconciled_channels: frozenset[str] = frozenset()`, `recon_decisions: tuple[tuple[str, str], ...] =
  ()` (after `shard`).
- **C-15** `PricedTotal` += `pool: Figure | None = None` (after `coverage`); docstring of `allowance` becomes
  "Σ LIST_EQUIVALENT lines on billing path `subscription`" (unchanged for all pre-Copilot data, since only that
  path produced LIST_EQUIVALENT); `pool` = Σ LIST_EQUIVALENT lines on `COPILOT_BILLING_PATHS`. `ClusterDay` +=
  `pool_nano: int = 0`.
- **C-16** `Finding` += `headroom: Figure | None = None` (after `references`); `ActionPlan` +=
  `pool_headroom_monthly: Figure | None = None` (after `observed_rr`). Documented domains: `Fix.target` +=
  `"github-copilot"`; `PolicyPack.target` += `"github-copilot"`; `Scope.dims` keys += `product`, `entity`,
  `cost_center`, `org`, `plan`, `bucket`, `surface`, `workload`, `plan_scenario`.
- **C-17** `ReconciliationReport` += `decisions: tuple[tuple[str, str], ...] = ()` (after `rerun_verdict`):
  the reconciler's reusable decisions, keys `convention:<source_id>` → `excl|incl|undecidable`,
  `gross_is_list:<entity>:<YYYY-MM>` → `true|false|unknown`, `plan_fit:<entity>:<YYYY-MM>` →
  `business|enterprise|unknown` (diagnostic only, never a label source). `RECON_DECISION_PREFIXES =
  ("convention:", "gross_is_list:", "plan_fit:")`.
- **C-18** `RunResult` += `copilot: CopilotSummary | None = None` (after `notes`).

`tokenbill/core/money.py`, `labels.py`, `jsonl.py`, `ids.py`, `models.py`
- **C-19** `money`: `NANO_USD_PER_CREDIT = 10_000_000`, `NANO_AIU_PER_CREDIT = 10**9`,
  `credits_str_to_nano(value: str) -> tuple[int, Decimal]`, `nano_aiu_to_nano(n: int) -> tuple[int, Decimal]`
  (÷ 100, half-even), `nano_to_credits_str(nano: int) -> str`.
- **C-20** `labels`: `figure_json(fig: Figure) -> dict[str, object]` (canonical MONEY encoder of SPEC §14.1) and
  `combine_weakest(figs: Sequence[Figure], *, note: str) -> Figure` (INVOICE iff every input is INVOICE; else
  basis LIST, EXACT iff every input EXACT, else ESTIMATED with summed range; the note names the non-invoice
  inputs; any LIST_EQUIVALENT input → `ContractViolation`).
- **C-21** `jsonl`: `parse_json_line(raw: bytes, *, exact_numbers: bool = False) -> dict | None` (with
  `exact_numbers` JSON numbers with a fraction or exponent parse as `Decimal`; the default path is unchanged);
  `load_json_exact(path: Path, *, max_bytes: int = 256 * 2**20) -> object`.
- **C-22** `ids`: `natural_id(prefix: str, source_kind: str, *parts: str | int | None) -> str` (=
  `stable_id(prefix, source_kind, *("" if p is None else p for p in parts))`);
  `copilot_session_key(raw_session_id: str) -> str` (= `stable_id("ses", "github_copilot", raw_session_id)`);
  `copilot_lane_key(session_key: str, lane_kind: str, agent_id: str | None) -> str` (= `stable_id("ln",
  session_key, lane_kind, agent_id or "")`). CP-LOCAL, CP-OTEL and CP-VSCODE must use these so one
  conversation seen by two sources merges.
- **C-23** `models`: `CopilotModel(model, routing, speed, pseudo, suffix_stripped)` and
  `normalize_copilot_model(model_raw: str) -> CopilotModel` with the CA-20 rules and test table;
  `normalize_model(raw, provider_hint="github")` returns `ModelId(model=cm.model, channel_hint=None,
  endpoint_scope="unknown", reason="copilot pseudo model" if cm.pseudo else None)` — routing, speed and pseudo
  are only available from `normalize_copilot_model`, which every Copilot adapter must call directly;
  `is_copilot_resource(service_name: str | None, scope_names: Iterable[str], attr_keys: Iterable[str], *,
  extra_service_names: Iterable[str] = ()) -> bool` (addendum §5.10 predicate).

`tokenbill/core/registry.py`, `protocols.py`
- **C-24** `BUILTIN_ADAPTERS`: insert `"copilot-cli": "tokenbill.adapters.copilot_cli:CopilotCliAdapter"` and
  `"copilot-otel": "tokenbill.adapters.copilot_otel:CopilotOtelAdapter"` immediately before `"otlp"`; append, in
  this order, `"copilot-vscode-traces": "tokenbill.adapters.copilot_vscode:VsCodeAgentTracesAdapter"`,
  `"gh-aw-token-usage": "tokenbill.adapters.gh_aw:GhAwTokenUsageAdapter"`, `"github-ai-usage":
  "tokenbill.adapters.github_billing:AiUsageReportAdapter"`, `"github-metered-usage":
  "tokenbill.adapters.github_billing:MeteredUsageAdapter"`, `"github-billing-api":
  "tokenbill.adapters.github_billing:BillingApiAdapter"`, `"github-copilot-config":
  "tokenbill.adapters.github_config:CopilotConfigAdapter"`, `"github-copilot-metrics":
  "tokenbill.adapters.github_metrics:CopilotMetricsAdapter"`, `"github-copilot-seats":
  "tokenbill.adapters.github_seats:CopilotSeatsAdapter"`, `"github-agent-tasks":
  "tokenbill.adapters.github_agent_tasks:AgentTasksAdapter"`, `"github-usage-records":
  "tokenbill.adapters.github_usage_records:UsageRecordsRefusal"`, `"github-copilot-activity-report":
  "tokenbill.adapters.github_activity_report:ActivityReportAdapter"`, `"copilot-export":
  "tokenbill.adapters.copilot_export:CopilotExportAdapter"`. `BUILTIN_DETECTORS` += `"copilot.seats-budgets":
  "tokenbill.detect.copilot_seats:CopilotSeatsBudgets"`, `"copilot.org-scan":
  "tokenbill.detect.copilot_org:CopilotOrgScan"`, `"copilot.lanes":
  "tokenbill.detect.copilot_lanes:CopilotLanes"`.
  `CONVENTION_MODULES = ("tokenbill.adapters.conventions_ext", "tokenbill.adapters.copilot_conventions",
  "tokenbill.adapters.copilot_otel", "tokenbill.adapters.github_billing", "tokenbill.adapters.gh_aw")`.
  `DQ_ADAPTER_UNAVAILABLE = "dq.adapter_unavailable"`; `sniff_adapter(path: Path, *, notes:
  list[DataQualityNote] | None = None)` appends one note per unimportable entry (it already skips them);
  `get_adapter` unchanged (raises).
- **C-25** Detector class attributes read with `getattr` defaults: `extension: str | None = None`, `aggregate:
  bool = False`, `families: frozenset[str] | None = None`. `run_detectors(lanes, ctx, *, only=None,
  emit_missing=True, aggregates_only: bool | None = None)`: `None` = today's behavior plus the gating below;
  `False` = only `aggregate=False` detectors; `True` = only `aggregate=True` detectors plus the
  missing-capabilities pass. A detector with `extension=e` runs only when `f"ext:{e}" in ctx.capabilities` and
  is otherwise skipped silently (no missing-capabilities finding, so Claude-only runs are unchanged). Lanes
  passed to a detector are filtered by `core.findings.product_family(lane)` (imported lazily; missing →
  family `"default"`) against `families` and the detector-level entries of `core.catalog.FAMILY_EXCLUSIONS`
  (imported lazily; missing → empty); findings whose `(detector_id, kind)` is excluded for the family named by
  their scope `product` dim are dropped after the run. Output order unchanged.
- **C-26** `ArgvAlias`, `ExtensionSpec`, `EXTENSIONS = {"copilot": ExtensionSpec(…)}` exactly as addendum CA-22
  with two deltas: the `collect` alias target is `("copilot", "collect", "--source", "cli")`; the documented
  `context_enricher` signature is `(store, record_stores, ctx, *, today, reconciled_channels,
  recon_decisions=()) -> AnalysisContext`.
- **C-27** `protocols`: `ChannelReconciler`, `SectionRenderer`, `ExtRecordStore` as addendum CA-23 **without**
  revision 3's `accepted_key_ids` argument and `put_decisions` / `decisions()` members (see "Addendum revision 3
  → this list"; `put(result, *, principal_key_id)` checks against the key ids in STORE's `meta`, R-E21);
  `LedgerStore.count_users(*, since_ms, until_ms, where, source: str = "requests") -> int` (keyword added;
  `"cost_lines"` counts distinct `CostLine.principal`; `where` keys for cost lines `team, cost_center, channel,
  model, sku, workspace_id, cost_type`). **`source_stats` goes into a new `runtime_checkable` protocol
  `LedgerStats` (`source_stats(*, adapter: str | None = None) -> dict[str, int]`), not into `LedgerStore`**:
  `core.testing.assert_store_conforms` does `isinstance(store, LedgerStore)`, so a new member would break
  `MemoryStore` between 1.5a and 1.5b. Callers use `isinstance(store, LedgerStats)`.

`tokenbill/core/facts.json`, `facts.py`, `builders.py`, lists, packaging, ownership
- **C-28** `facts.json` gains a top-level `copilot` object; top-level `rates`, `modifiers`, `settings_keys` and
  `lifecycle.promotions` are **not touched** (pinned). Sections (each entry with `source`, `finding`,
  `verified_on: "2026-09-23"`, `verification: "research"`): `credit`, `plans` (business $19 / 1,900; enterprise
  $39 / 3,900; promo 3,000 / 7,000 for 2026-06-01 → 2026-09-01), `rates` (every §19.2 current and history row
  in the merged `_rate_row` shape, `long_context.threshold`, plus `date_source`), `modifiers` (`github.auto`
  0.9, `github.compliance` 1.1, `github.fast.opus-4-8` replace_base), `write_1h_rule`, `band_rules`,
  `promotions` (the GPT-5.6 Sol promo and three Gemini rows of CA-36), `skus` (SKU → cost type and, for seat
  SKUs, plan: `copilot_for_business` → business, `copilot_enterprise` → enterprise, `copilot_standalone` →
  business **VERIFY**), `plan_quota_map` (1900 / 3000 → business, 3900 / 7000 → enterprise, **VERIFY**),
  `workflow_paths`, `utility_models`, `model_categories`, `retirements`, `remaps`, `runner_rates`,
  `included_minutes`, `review_estimates`, `dates`, `settings_keys` (§11.4, target `github-copilot`),
  `editor_families` (seat `last_activity_editor` prefixes and activity-report `last_surface_used` prefixes →
  `EDITOR_FAMILIES`, **VERIFY**), `vscode_traces` (span columns, attribute allowlist, content keys, retention
  7 days / 100 sessions, per-OS path templates and extension id **VERIFY**), `cache_ttl_statement`,
  `report_lag_days` 3, `aic_default_cap_per_run` 1,000. `facts.py`: `Facts.copilot: CopilotFacts` and
  accessors `copilot_rates()`, `copilot_modifiers()`, `copilot_plans()`, `copilot_dates()`, `copilot_skus()`,
  `copilot_plan_quota_map()`, `copilot_workflow_paths()`, `copilot_remaps()`, `copilot_retirements()`,
  `copilot_runner_rates()`, `copilot_settings_keys()`, `copilot_editor_families()`, `copilot_vscode_traces()`,
  `copilot_report_lag_days()`; `Facts.entries()` also yields `("copilot.<part>", entry)` so `parse()` checks
  their metadata. `rows_for(model, channel="anthropic_api")` is unchanged (Copilot rows are reached through
  `copilot_rates()` only).
- **C-29** `builders`: `make_copilot_ctx(model="claude-opus-5-5", **kw)`, `make_license`, `make_activity`,
  `make_config`, `make_ai_usage_row(...) -> tuple[CostLine, UsageAggregate]`, `make_seat_line`,
  `make_actions_line`, `make_pool_month`, `make_plan_evidence`; `CANARY_LOGIN = "tb-canary-login-7f3a91"`;
  `FlatRates` prices `COPILOT_BILLING_PATHS` on LIST_EQUIVALENT like `subscription`.
- **C-30** SPEC list amendments (documents + the core no-float test's module list): §5.1 capabilities +=
  `credits`, `licenses`, `activity`, `config`, `copilot_billing`, `ext:<name>`; dq codes += every Copilot code
  in the briefs plus `dq.adapter_unavailable`, `dq.extension_unavailable`, `dq.convention_module_unavailable`,
  `dq.principal_key_mismatch`; no-float modules += `core/pool.py`, `core/extensions.py`,
  `adapters/{github_billing,github_config,github_metrics,github_agent_tasks,copilot_cli,copilot_otel,
  copilot_vscode,gh_aw,copilot_export,github_activity_report,copilot_vscode_collect}.py`,
  `copilot/{rates_verify,recon,panel,enrich,plan,budgets,policy,admin_actions,summary,render,showback,focus,
  record_store,handoff,admin_answers}.py`, `detect/{copilot_seats,copilot_org,copilot_lanes}.py`,
  `synth/{copilot_truth,copilot_world}.py` (absent modules skipped); §7.3 `sources_mask` bits `copilot-cli`
  256, `copilot-otel` 512, `copilot-vscode-traces` 1024, `gh-aw-token-usage` 2048.
- **C-31** `pyproject.toml` `[tool.hatch.build.targets.wheel] artifacts` += `"tokenbill/copilot/data/**"`,
  `"tokenbill/copilot/handoff_data/**"`; new docstring-only `tokenbill/copilot/__init__.py` (owned by F-CORE).
- **C-32** `OWNERSHIP.toml`: exactly the rows of addendum §21.3 (revision 3.1), which equal the **Owns**
  sections of the briefs in this directory: new tables `[packages.F-EXT]`, `[packages.F-POOL]` and one per
  CP-* package (19); `tokenbill/copilot/__init__.py` added to F-CORE; `tokenbill/outputs/ccusage.py` moved from
  OUT to CLI-LEDGER (O-5; its tests move into CLI-LEDGER's existing `tests/v2/cli_ledger/**`). Notably
  `tokenbill/copilot/teammap.py` belongs to CP-ORGDATA and `tokenbill/adapters/copilot_vscode.py` to CP-OTEL
  (CP-VSCODE owns only `copilot_vscode_collect.py` and its test / fixture directories); there is no
  `tokenbill/core/spans.py`. F-SEM-C and F-KIT-C add no rows (their files sit under F-SEM's / F-KIT's existing
  patterns, incl. `tests/v2/gates/**`); INTEGRATION keeps `docs/**` (covers `docs/COPILOT.md` and
  `docs/COPILOT-ADMIN.md`). Checked in the consistency pass with `scripts/check_ownership.py`'s own matcher: every
  new pattern has exactly one owner and every tracked file at 99ba098 still has exactly one.
  `scripts/check_ownership.py` needs no change.
- **C-33** Wave-0/1 test amendments F-CORE-C makes (declared contract changes; no other existing test is
  edited): see T-1 … T-3.

---

#### 3. Wave 1.5b — in parallel, each depending only on F-CORE-C

**F-SEM-C** (`--package F-SEM`; files `core/{findings,cache_rules,transitions,conventions}.py`)
- **S-1** `findings.product_family(lane: Lane) -> str`: `"copilot"` when `lane.billing_class == "pool"` or the
  first request's `pricing.channel == "github_copilot"`, else `"default"`. **`cohort_key` stays the 3-tuple**
  `(team, lane_kind, billing_class)` (pinned by `test_cohort_key`; billing class `pool` already separates
  Copilot cohorts because only Copilot paths map to it). `make_scope(**dims)` adds `product="copilot"` when
  `billing_class == "pool"` and no `product` is given.
- **S-2** `build_finding` / `_validate`: R-E20 per-field basis domains for scopes with `("product",
  "copilot")` or `("billing_class", "pool")` (every other scope: today's R-E8 checks, and `headroom` must be
  None); for `pool` cohorts built by generic detectors the title gets the prefix `"Copilot credits: "` and the
  summary the phrase "list-equivalent AI-credit value" when absent; for `product=copilot` scopes the fix is
  replaced by `core.catalog.fix_for(detector_id, kind, "copilot")` (resolved lazily; missing accessor or
  entry → keep the text, drop every `config_patch` not targeting `github-copilot`, set `target=None`, append
  " (no Copilot setting known)").
- **S-3** `min_usd_gate(finding: Finding, ctx: AnalysisContext) -> bool`: compares `min_usd_nano(ctx)` with
  max(recoverable point, headroom point) for Copilot scopes, with recoverable point otherwise.
- **S-4** `cache_rules.CacheRules` += `ttl_semantics_known: bool = True` (appended; slots dataclass).
  `RulesTable._build` returns, for `channel == "github_copilot"` (any provider), a dedicated row: provider
  `"github"`, `ttl_options_s=()`, `ttl_measured_from="request_start"`, `refresh_on_read=True`,
  `visible_from="response_end"`, `lookback_positions=None`, `collapse_tool_runs=False`, `max_breakpoints=4`,
  `scope="organization"`, `tier_params` = Anthropic's with `"context_tier"` appended to the `messages` tier,
  `effort_invalidates_all_tiers_models=()`, `sources` = the optimize-ai-usage tutorial and cost-levers F8,
  **`ttl_semantics_known=False`**. `channels()` is unchanged (pinned). `transitions.classify_transitions` on
  rows with `ttl_semantics_known=False`: a miss is `ttl-expiry` only when τ is known and `gap > τ +
  prev_duration + 10 s`; `ambiguous` iff τ is known and `|gap − τ| ≤ prev_duration + 10 s`; otherwise the usual
  cause order. `param-change` sub-cause `context-tier-change` when the serving inference's
  `pricing.context_tier` differs from the previous request's, ordered after `effort-change`. (Replaces addendum
  CA-32's `None`-typed fields, which would have widened a frozen type DETECT-CACHE already consumes.)
- **S-5** `conventions._load_extensions` keeps skipping a missing `CONVENTION_MODULES` entry (it already does)
  and records `dq.convention_module_unavailable` (detail = module name) in a module-level list surfaced by
  `conventions.load_notes() -> tuple[DataQualityNote, ...]`.

**F-KIT-C** (`--package F-KIT`; files `core/{catalog,testing,kanon}.py`)
- **K-1** Levers in a **separate** table: `COPILOT_LEVERS: tuple[LeverDef, ...]` (addendum §11.1 rows; aggregate
  levers use `replay="aggregate"` and `grid=()`); `LEVERS` unchanged. `_REPLAY_KINDS` += `"aggregate"`.
  `AGGREGATE_GRIDS: Mapping[str, tuple[str, ...]]`, `AggregateSpec(param, value, scope)`,
  `parse_aggregate_spec(spec: str) -> AggregateSpec`, `to_aggregate_spec(spec: AggregateSpec) -> str` (grammar
  `copilot:<param>=<value>[@<scope>]`, params and scopes of CA-35; unknown → `UsageError`). `lever(lever_id)`
  searches `LEVERS` then `COPILOT_LEVERS`; `levers_for_kind(kind, *, family: str = "default")` returns from
  `LEVERS` for `"default"` (unchanged) and from `COPILOT_LEVERS` for `"copilot"`.
- **K-2** `COPILOT_ALLOWLIST: Mapping[str, AllowedKey]` built from `facts.copilot.settings_keys` (target
  `github-copilot`); `ALLOWLIST` unchanged; `copilot_allowed(key) -> AllowedKey`. `ADMIN_ACTIONS: Mapping[str,
  AdminActionDef]` (every id of the F-KIT-C brief, each with where, docs URL, template text, REST method/path or
  None, auth note from addendum §19.4). Copilot levers' `patch_keys ⊆ COPILOT_ALLOWLIST ∪ ADMIN_ACTIONS`.
- **K-3** Data accessors (reading `core.facts.copilot`; vocabularies re-exported from `core.records`):
  `COPILOT_PROMOTIONS` and `copilot_promotion_for(model, date)` (top-level `PROMOTIONS` unchanged),
  `COPILOT_RETIREMENTS`, `RR_PRIORS`, `copilot_allowance(plan, month, *, promo_eligible=True) ->
  tuple[Decimal, str | None]` (`plan="unknown"` → `UsageError`), `copilot_cost_type(sku, *,
  username_present) -> str`, `copilot_seat_plan(sku) -> str | None`, `copilot_workload(workflow_path)`,
  `copilot_category(model)`, `copilot_remap(model) -> tuple[str, bool] | None`, `runner_rate(sku)`,
  `editor_family(raw: str | None) -> str` (seat editor strings and activity-report surfaces → `EDITOR_FAMILIES`,
  unknown → `"other"`), `agent_family(agent_product)`, `fix_for(detector_id, kind, family) -> Fix | None`,
  `FAMILY_EXCLUSIONS: Mapping[tuple[str, str | None], frozenset[str]]`, `COUNT_SOURCE: Mapping[tuple[str, str],
  str]` (default `"requests"`).
- **K-4** `kanon`: `scope_counter(ledger, record_stores, *, since_ms, until_ms, source_of) -> Callable[[Finding,
  Scope], int]` (CA-37 mapping, plus `plan_scenario` ignored for counting); `rescope_findings(…,
  count_users=…)` accepts `Callable[[Scope], int]` (today) or `Callable[[Finding, Scope], int]` (detected by
  arity); Copilot parent chain for scopes with a `product` dim: `team` → `bucket` → `plan` → `model` →
  `cost_center` → entity root, never removing `product`, `entity`, `org`, `plan_scenario`; `_exempt` implements
  R-E16 (product-scoped findings follow `COUNT_SOURCE`, category no longer exempts them); `_merge_findings`
  sums `headroom` like `recoverable` (`_sum_opt`); **E.2 R-E10 `publish()` fix** applied here:
  `publish(raw, *, k=5, parent_of=None, audience: str = "org")` (keyword appended; the merged signature has no
  audience) publishes rows with `n_users == 0` whose group-by has no person or person-proxy key (`principal`,
  `session`, `session_key`, `api_key_id`, `cwd_key`) with note `users_unknown`, and never suppresses for
  `audience="self"`.
- **K-5** `testing`: `FakePricer` loads every `copilot_rates()` row and Copilot modifier, band hypotheses (A
  priced; A/B range when `ctx.context_tier` is known), the Claude 1h write range, Copilot promotions and
  `dq.promotion_expired`, both Copilot paths on LIST_EQUIVALENT (existing Anthropic / OpenAI behavior
  byte-identical); `fake_price_total` routes Copilot-path LIST_EQUIVALENT lines into `PricedTotal.pool` and
  only `subscription` lines into `allowance`; `MemoryStore(*, org_key=None, name_key_id=None, pricer=None,
  now_ms=0, adopt_key_ids: bool = False)` (R-E21: adopts only a `copilot-export` source's key ids, with or
  without its own org key; one adopted key id; a second bundle key id → `UsageError`; `meta()` reports
  `org_key_id`, `adopted_key_id`, `org_key_mode`), `count_users(…, source=…)`, `source_stats` (so it satisfies
  `LedgerStats`), `ClusterDay.pool_nano`, latest-fetch-wins for cost lines and aggregates; `FakeReplayer`
  accepts billing class `pool` like `allowance` (LIST_EQUIVALENT); `MemoryRecordStore` (`ExtRecordStore`, with
  the key-id check against its `MemoryStore`'s `meta()` — `org_key_id` or `adopted_key_id` — and the
  `seat_counts` fallback of
  `count_users(source="licenses")`); `assert_record_store_conforms(factory)`; `assert_detector_conforms`
  lane-independence for `aggregate=True`; `assert_adapter_conforms` `p_` checks and `CANARY_LOGIN` absence.
- **K-6** Gate F' test `tests/v2/gates/test_gateF_copilot_smoke.py` (below).

**F-EXT** (`--package F-EXT`; new `core/extensions.py`)
- **E-1** CA-39 API with `enrich(store, record_stores, ctx, *, today, reconciled_channels, recon_decisions=(),
  notes) -> AnalysisContext` and `recon_decisions_of(reports: Sequence[ReconciliationReport]) ->
  tuple[tuple[str, str], ...]` (union, sorted; a key with two different values → `ContractViolation`);
  `run_reconcilers(…)` passes `rounding_remainders` from `store.source_stats()` when `isinstance(store,
  LedgerStats)`; `rewrite_argv` honors multi-token alias targets (`collect copilot-cli` → `copilot collect
  --source cli`).

**F-POOL** (`--package F-POOL`; new `core/pool.py`)
- **P-1** CA-40 API plus `detect_plans(cost_lines, licenses, config, *, month, entity_mode="enterprise") ->
  list[PlanEvidence]` (report-quota evidence = `plan_quota` config rows, emitted by CP-BILL only under
  `copilot-report-quota`) and plan scenarios in `pool_months(…, plans=None)`; `pool_credits` raises for plan
  `unknown`; seat sources `seat_counts` and `report_users` (lower bound); the shared `run_flags(config)` merge
  (CLI snapshot over admin answers); Appendix C.P1–P15 of the addendum (P13–P15 as written there, with the
  flag named `copilot-report-quota` and quota evidence from `plan_quota` rows) plus `F-POOL.md`'s extra cases
  P14b and P15b.

#### 4. Gate F' (owner F-KIT-C)

`tests/v2/gates/test_gateF_copilot_smoke.py` (`@pytest.mark.gate`): builders → Copilot cost lines, aggregates,
licenses (one activity-report license with plan `unknown`) and a Copilot lane → `MemoryStore` +
`MemoryRecordStore` → `core.pool.detect_plans` / `build_cells` / `pool_months` (C.P9 month; C.P13 two
scenarios) → `FakePricer` prices a `copilot_pool` inference into `PricedTotal.pool` (C.G1) →
`run_detectors(aggregates_only=False)` then `(True)` with a test aggregate detector (`extension="copilot"`)
and a test lane detector (family filter, `FAMILY_EXCLUSIONS` drop, aggregate detector once, skipped silently
without `ext:copilot`) → `rescope_findings` with `count_users_fn` (cost-line and license sources; R-E16 entity
exemption; a `plan_scenario` dim survives re-scoping) → `build_finding` with a LIST recoverable beside a
LIST_EQUIVALENT `cost_observed` and `headroom` on a `product=copilot` scope (R-E20) → `core.extensions` with
the fake extension (`rewrite_argv` incl. the multi-token target, `render_sections`, `focus_rows` owned channels,
`run_reconcilers`, `recon_decisions_of`, missing-module dq) → `MemoryStore(org_key=K, adopt_key_ids=True)`
ingesting a `copilot-export`-sourced `p_` batch under key id A (adopted, kept), an `r_` batch (pseudonymized
with K) and a `p_` batch of another adapter under key id B (nulled with `dq.principal_key_mismatch`, B not
adopted); `publish()` keeps a `users_unknown` row (R-E10) → a `RunResult` with `copilot` set. Also: every `AGGREGATE_GRIDS` entry
parses and round-trips; every Copilot lever's `patch_keys` resolves; every `fix_for` entry targets
`github-copilot`; `sniff_adapter` with every Copilot adapter module absent raises nothing and records
`dq.adapter_unavailable`; every wave-0/1 test green on Python 3.10 and 3.13 (with only the T-list edits);
`core/*` frozen again.

---

#### 5. Amendments to SPEC packages (A-; paste into briefs not yet started, late change requests otherwise)

| id | package | status @99ba098 | amendment | LOC |
|---|---|---|---|---|
| A-1 | RATES | not started | addendum §21.4 row, reading Copilot rows through `core.facts.copilot_rates()` / `core.extensions.extension_rate_files()` (missing → dq); predicate keys `routing`, `compliance_in`; band hypotheses; both Copilot paths → LIST_EQUIVALENT into `PricedTotal.pool` (`allowance` = subscription lines only); `pricing verify` runs `core.extensions.rate_verifiers()` | +120 |
| A-2 | STORE | not started | addendum §7.1 DDL and §21.4 row **plus** `SqliteStore(…, adopt_key_ids: bool = False)` (R-E21 replaces the §7.1 "Key-id adoption" paragraph: with or without its own org key the store adopts the principal and name key ids of the first `copilot-export` bundle only, one adopted id, `meta.adopted_key_id` / `adopted_name_key_id` / `org_key_mode` + an `audit` row, a second bundle key id → `UsageError`), `count_users(source="cost_lines")`, `source_stats` (implements `core.protocols.LedgerStats`), `purge(principal=…)` also for adopted-key rows, `ReconciliationReport` untouched (not stored) | +190, total 2,990 |
| A-3 | TRACE | not started | addendum §21.4 row; closed key sets derived with `core.records.record_fields` (C-4) and `core.records.RAW_USAGE_ENUMS` / `RAW_USAGE_NUMERIC`, replacing the hand lists (net-neutral, O-5) | ≤ 0 net |
| A-4 | TELEM | **started** | late change request (R-E23): `otlp` skips resources for which `core.models.is_copilot_resource` is true and sets `stats["defer:copilot-otel"]`; rebase onto the gate-F' core first | +40 |
| A-5 | RECON | not started | addendum §21.4 row; `merge_reports` also unions `ReconciliationReport.decisions` (conflicting values → `ContractViolation`) | +90 |
| A-6 | REPLAY | **started** | late change request (R-E23): accept billing class `pool` like `allowance` (LIST_EQUIVALENT; mixed classes still raise) | +10 |
| A-7 | PLAN | not started | addendum §21.4 row; skip `replay == "aggregate"` levers; lever lookup via `core.catalog.lever` (searches both tables) | +40 |
| A-8 | OUT | not started | addendum §21.4 row (`render_sections`, `write_focus(…, extra_rows, owned_channels)`, `money_json` → `figure_json`, BILL line for `PricedTotal.pool`); `outputs/ccusage.py` moves to CLI-LEDGER (O-5) | +40 net, total 2,890 |
| A-9 | WIRING | not started | addendum §21.4 row plus `open_store(…, adopt_key_ids=False)` passed to `SqliteStore`; `ingest_paths` persists records **after** the ledger ingest (so an adopted key id exists before `persist`) and re-reads deferred files | +90 |
| A-10 | CLI-LEDGER (wave 3) | not started | addendum §21.4 row; `reconcile` prints the merged decisions (`recon_decisions_of(reports)`) with its verdicts; nothing is persisted (C-17) | +45 |
| A-11 | CLI-SAVINGS (wave 3) | not started | addendum §21.4 row; because decisions are not persisted, `findings` / `report` (and `scan` on Copilot data) first run `core.extensions.run_reconcilers` + RECON's reconcile over the same window, then call `enrich(…, reconciled_channels=…, recon_decisions=recon_decisions_of(reports))` (without reconcilers, e.g. on a store without Copilot data, `recon_decisions=()`: `excl` convention and unclassified discounts, both labelled) | +65, total 2,965 |
| A-12 | INTEGRATION (wave 4) | not started | `docs/COPILOT.md` (fleet recipe: managed OTel incl. the JetBrains caveat, **VS Code `agent-traces.db` opt-in + daily `copilot collect --source vscode`**, CI post-step, gh-aw artifacts, auth table, privacy); **`docs/COPILOT-ADMIN.md` generated verbatim from `tokenbill/copilot/handoff_data/admin_guide.md`** with `tests/v2/e2e/test_copilot_admin_doc.py` asserting byte equality; flagship §18 incl. the handoff and plan-unknown paths; Copilot release gates (below); weekly YAML check | docs/tests |
| — | CC, ADMIN, BLOCK, VERIFY, DETECT-CACHE, DETECT-OTHER, SYNTH-ORACLE, SYNTH-FLEET | mixed | none | 0 |

#### 6. Gates after wave 2

- **Gate 1 additions** (`@pytest.mark.gate`, `importorskip`): the addendum §21.6 list, plus CP-HANDOFF's
  UI-path vs token-path bundle equality, CP-VSCODE extracts through CP-OTEL's adapters, CP-WIRE's `copilot
  scan --from export.tbx` on a keyless `SqliteStore(adopt_key_ids=True)` and on an org-keyed one that also
  holds CP-VSCODE extracts with `c_` principals (both key spaces kept, joined by team only, R-E21), and the
  plan-unknown world rendered as two scenario columns.
- **Gate 2 additions:** addendum §21.6 plus `copilot export`, `copilot inspect`, `copilot admin-guide`,
  `copilot plan`, `copilot pseudonym` (its `p_` equals the bundle's; `purge --principal` with it removes the
  adopted-key rows), `collect copilot-cli` → `copilot collect --source cli`.
- **Copilot release gates (INTEGRATION, before 0.2.0 GA):** (1) a real, redacted AI usage report with token
  columns, the matching usage summary and a detailed usage report with seat lines are `real-redacted`
  fixtures; (2) **a real, redacted VS Code `agent-traces.db` read through the allowlist** is a fixture (the
  CLI `session-store.db` and CLI OTel file stay optional: without them `copilot-store` and
  `copilot-cli-otel-file` remain experimental and the release notes say so; `copilot-jetbrains-otel` stays
  experimental until a real JetBrains OTel file exists); (3) every `facts.copilot` row re-verified `primary`
  or shipped disabled (R-E19); (4) the weekly Copilot YAML check job is live; (5) **a real `.tbx` produced by
  the adopting admin (or their real UI files, redacted) passes the leak gate and `copilot inspect`**, `copilot
  scan --from` runs on it keyless and networkless, and the admin's corrections of the guide's VERIFY items
  (addendum §19.5 #30–#35) are applied before `docs/COPILOT-ADMIN.md` is regenerated. Numbering as addendum
  §21.6.

#### 7. Existing tests that change (T-) — and nothing else

- **T-1** (F-CORE-C) `tests/v2/core/test_registry.py::test_string_maps_match_the_spec`: expected adapter list
  per C-24, `len(reg.BUILTIN_DETECTORS) == 23`, `CONVENTION_MODULES` per C-24;
  `::test_all_detectors_skips_unimportable_with_notes`: `len(notes) == len(reg.BUILTIN_DETECTORS)`.
- **T-2** (F-CORE-C) `tests/v2/core/test_types.py::test_contract_field_names`: `names(t.PricedTotal)` gains
  `"pool"` at the end; `len(names(t.RunResult)) == 22`.
- **T-3** (F-CORE-C) `tests/v2/core/test_records.py::test_billing_class`: add `billing_class("copilot_pool") ==
  billing_class("copilot_direct") == "pool"` (existing asserts unchanged).
- **T-4** none in `tests/v2/sem/**` or `tests/v2/kit/**`: F-SEM-C and F-KIT-C keep `cohort_key`, `channels()`,
  `LEVERS`, `ALLOWLIST`, `PROMOTIONS` and the facts pins exactly (that is why the Copilot tables are separate).
  `tests/v2/core/strategies.py` (used only by `tests/v2/core/**`) samples the enlarged `BILLING_PATHS`, which
  `FlatRates` (C-29) prices; `tests/v2/sem/test_policy.py` samples `BILLING_PATHS` for selector strings, which
  `core.policy` accepts unchanged. F-CORE-C must not narrow either strategy. F-CORE-C runs the whole suite
  before merging; any failure outside T-1 … T-3 is a defect of the change, not a test to edit.
