# tests/v2/core — F-CORE acceptance tests

Foundation core (wave 0): the contracts of SPEC §3.1–§3.10, §3.12, §3.13, §3.17, §3.24, §3.25, the
suite-wide guards (§8.9), the ownership tooling (§21 #2) and the v0.1.2 goldens (§16.2).

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/core` (also on 3.10).
Coverage: `uv run --extra dev coverage run -m pytest tests/v2/core && uv run --extra dev coverage report
--include='tokenbill/core/*'` (99% at hand-off).

| file | covers |
|---|---|
| `strategies.py` | hypothesis strategies that build valid instances of every §3.2 record (area-local helper) |
| `test_records.py` | record validation, enum formatting (3.12 trap), `UsageBuckets` rules and `__add__`, `Request`/`Lane` properties, lane-event attrs schema, lossless `to_json`/`from_json` for every record and several §3.5 types (hypothesis), malformed-document fuzz |
| `test_money.py` | §3.3 acceptance values, half-even rounding, remainders, hostile exponents bounded, scaled-vs-Decimal agreement (hypothesis), display |
| `test_labels.py` | every `Figure` construction rule, `add`/`sub`/`scale`, billed eligibility, associativity (hypothesis) |
| `test_types.py` | publish token, `Policy` observed/delegation to `core.policy`, `UnitRates`, report helpers, field-name spot checks, runtime-checkable protocols |
| `test_registry.py` | exact string maps, lazy loading, sniffing (64 KiB head, `.gz`, broken sniffers), unimportable detectors, exactly one missing-capabilities finding, `only=`, plugins only when enabled |
| `test_ids.py` | stable ids, HMAC pseudonyms, key ids, request ids, opaque refs |
| `test_jsonl.py` | head sha, plain/gzip/zstd guard, the `.zst` branch through a stand-in `compression.zstd` (and the real codec on Python ≥ 3.14), corrupt streams → `SourceError`, offsets, oversize lines, `parse_json_line` (fuzz; overflowing number literals refused like Infinity), 0600/0700 modes, the Windows ACL path through a fake runner, canonical/deterministic `write_jsonl` |
| `test_textsafe_secrets.py` | ANSI/C0/C1/bidi stripping and truncation (hypothesis), every secret type, the SPEC `high_entropy` rule (with the path exception), PEM spans equal to the reference regex (hypothesis) in linear time, redaction counts |
| `test_models_lanes.py` | SPEC §6.9 case 16 ids, Bedrock/Vertex/ARN forms, `<synthetic>`, config aliases (fuzz), `group_lanes`, `ttl_of_last_write` |
| `test_facts_evidence.py` | facts.json loads, every entry carries `source`/`finding`/`verified_on`/`verification`, FakePricer rows and modifiers, §6.9 cases 1–4 and 18 recomputed from facts, settings keys, lifecycle, FOCUS 1.4 columns, evidence constants, Appendix A.12 |
| `test_builders.py` | canary helpers, record builders, `lane_from_table`, `FlatRates` (scale 8, per-line exactness table of §6.3, unit rates == `price_usage` on 200 random usages) |
| `test_no_float_money.py` | AST scan of the §2.4 money paths (paths not merged yet are skipped) |
| `test_guards.py` | socket guard (non-loopback refused; loopback, `socketpair`, `AF_UNIX`, asyncio allowed), ownership glob/TOML/table, a temp git repo where an out-of-package file and a FROZEN file are flagged |
| `test_goldens.py` | goldens exist, are non-empty, match `manifest.json`, carry placeholders, survive a `core.autocrlf=true` checkout byte-identical |

## Fixtures and provenance

No fixture files: every record is synthetic, built in the test with `core.builders` or the strategies
module. `tests/v2/golden/` holds the **v0.1.2 golden outputs** captured by `scripts/capture_goldens.py` from
the untouched tree before any other change (commit `e36816d`): `demo` (default, `-o`, each `--scenario`,
`--seed 7`, `--seed 11`) and `analyze` on the four demo scenarios written with the frozen
`trace.write_trace` (each, and all four together), stdout and HTML, with the version replaced by
`{{TOKENBILL_VERSION}}` and the report date by `{{REPORT_DATE}}` (`normalize()` in the script;
`manifest.json` lists argv, input hashes and file hashes). `python scripts/capture_goldens.py --check`
re-captures and diffs. The comparison against the new CLI belongs to CLI-LEDGER.

## Facts (core/facts.json) — verification on 2026-09-23

Re-checked against the primary sources (`verification: "primary"`): every FakePricer Anthropic rate row
(prices, cache multipliers, minimum cacheable tokens, tokenizer family) on the pricing and prompt-caching
pages; batch 0.5×, US data residency 1.1× (4.6+, Claude API, Claude Platform on AWS **and Foundry US Data
Zone**), fast mode bases, web search $10/1,000, the regional +10% rule; gpt-5.6-sol promotional row ($4 /
$0.40 / $5 / $20, long context > 272K at $8 / $0.80 / $10 / $30, "at least through November 21, 2026"),
OpenAI 5.6+ caching (30m, writes 1.25×, reads 0.1×, min 1,024); retirement floors; every §11.4 settings
key incl. the priority items (`promptCacheTtl`/`subagentPromptCacheTtl` = `"5m"`|`"1h"` from 2.1.242;
`model`; `effortLevel` = low|medium|high|xhigh; `modelPricing` shape; `hooks.SessionStart` shape and hook
input fields); FOCUS 1.4 Cost and Usage columns (65, with feature level and nullability, from the v1.4 tag of
the FOCUS_Spec repository); headless/SDK result-field semantics; Bedrock CUR usage-type text (Price List
offer file).

Corrections to the SPEC found by the facts task (recorded in facts.json notes):
- FOCUS 1.4 removed `ProviderName` and `PublisherName` (deprecated in 1.3); use `ServiceProviderName` and
  `HostProviderName`. "2 datasets, 47 columns" are the *new* datasets/columns of 1.4.
- `effortLevel` has no `"max"` value (only `maxEffortLevel` does).
- `bashOutputMaxChars` needs Claude Code 2.1.261; `enforceAvailableModels` 2.1.175;
  `OTEL_METRICS_INCLUDE_ENTRYPOINT` 2.1.152; a `modelPricing` multiplier above 1 needs 2.1.271.
- Current Bedrock model ids carry no `-v<N>` suffix (`anthropic.claude-opus-5-5`); `normalize_model`
  accepts both forms.

### Unverified (shipped as `verification: "research"` or disabled)

- Rate-row `effective_from` dates other than Opus 5.5 / Opus 5 / Fable 5.1 / Mythos 5.1 are derived from the
  documented retirement floor minus one year (Anthropic's launch + 1 year pattern).
- Bedrock `claude-opus-5` global row: output $25 and the 512-token minimum were not re-checked (the AWS
  pricing page is script-rendered; the model is absent from the offer file).
- gpt-5.6-sol launch row (2026-07-09 → 2026-08-21): $5 / $30 derived from the changelog's percentages; cache
  multipliers assumed; long-context band unknown — **ships `enabled: false`**.
- OpenAI tokenizer family (`openai-o200k`) for gpt-5.6-sol is assumed.
- Evidence constants from the Anthropic cost guide / research corpus (cache-read share 84/94/80, TTL 1-in-20
  rule, keepalive 240 s / 3,600 s, compaction summary, thinking share, CPT defaults, cold-resume minimum,
  max-tokens, tool-search band, code-review cost, FEMP thresholds) and the design thresholds (D7, D10, D34).
- Lifecycle successors and the announced-but-unpriced Sonnet 5.5 / Haiku 5.5.
- claude-code-action execution-file path and container shape (SPEC §19.8 #16).
- All CUR 2.0 / GCP SKU rules: `verified: false` (usage-type text checked; CUR usage-amount units not
  confirmed against a real export). No GCP SKU ids are known yet.

## F-CORE-C — GitHub Copilot core contracts (wave 1.5a)

Applies `copilot/briefs/CORE-AMENDMENTS.md` C-1 … C-33 and T-1 … T-3 to F-CORE's files (ownership check
`--package F-CORE`, RULINGS G-1). Every new field is appended with a default; nothing is renamed or
removed.

| file | covers |
|---|---|
| `test_copilot_records.py` | C-1 … C-10: `billing_class` table (`pool`), `COPILOT_*` constants, `PricingContext` routing / compliance / context tier, `RAW_USAGE_ENUMS`, `record_fields`, `CostLine` Copilot fields (`h_` repo / workflow, pseudo, workload, decimal quantity), `OutcomeAggregate.extra`, Copilot lane-event attrs, the C-9 vocabularies, `LicenseSnapshot` (incl. the activity-report shape), `ActivityDay`, `ConfigSnapshot` (keys per kind, prefixes, no `p_` values, entity ids), `record_key` (count rows differ by attrs), appended fields omitted from `to_json` at their default, hypothesis round trips and mutation fuzz |
| `test_copilot_types.py` | C-11 … C-18: ingest / context carriers, `EXPERIMENTAL_FLAGS`, `PlanEvidence`, `PoolMonth` (scenarios), `BILL_LINES`, `CopilotBillLine`, `AdminAction`, `CopilotSummary`, `FocusRow`, `PricedTotal.pool`, `ClusterDay.pool_nano`, `Finding.headroom`, `ActionPlan.pool_headroom_monthly`, `ReconciliationReport.decisions`, `RunResult.copilot` — validators and JSON round trips |
| `test_copilot_helpers.py` | C-19 … C-23: credit and nano-AIU money (addendum C.G11 / G17), `figure_json`, `combine_weakest`, `parse_json_line(exact_numbers=True)`, `load_json_exact` (plain, gzip, limits, fuzz), `natural_id` / Copilot session and lane keys, the CA-20 `normalize_copilot_model` table (+ fuzz), `normalize_model(…, "github")`, `is_copilot_resource` truth table |
| `test_copilot_registry.py` | C-24 … C-27: registry entries and order, `sniff_adapter(notes=…)` (one `dq.adapter_unavailable` per unimportable entry; every Copilot module absent), `run_detectors` phases (`aggregates_only`), silent `ext:<name>` gating, product-family lane filter and `FAMILY_EXCLUSIONS` drops (stubbed), missing F-SEM-C / F-KIT-C modules tolerated, `ExtensionSpec` / `ArgvAlias` / `EXTENSIONS`, registry import loads no wave-2 module, `LedgerStore` unchanged for `MemoryStore`, `LedgerStats` / `ExtRecordStore` / `SectionRenderer` / `ChannelReconciler` |
| `test_copilot_facts.py` | C-28: the `copilot` section (provenance, `verification: research`, R-E19), top-level sections unchanged, §19.2 current rows, history rows and intervals, Appendix C.G1/G5/G5b/G6/G7/G8/G9/G10/G11/G14/G16 rate arithmetic from the rows, every other table, accessors, malformed sections → `ContractViolation` (+ fuzz) |
| `test_copilot_builders.py` | C-29: `CANARY_LOGIN`, `make_copilot_ctx`, record builders, `make_ai_usage_row` on GitHub's parser test row, seat / Actions lines, `make_pool_month` (C.P1, P13), `FlatRates` bases on every billing path |
| `test_copilot_no_float.py` | C-30: the amended §2.4 no-float module list (absent modules skipped) plus F-CORE-C's own modules |
| `test_copilot_ownership.py` | C-31 / C-32: packaging of `tokenbill/copilot/{data,handoff_data}/**`, docstring-only `tokenbill/copilot/__init__.py`, every §21.3 ownership row |

Existing tests edited (declared contract changes): T-1 `test_registry.py`, T-2 `test_types.py`, T-3
`test_records.py`, and — as the orchestrator's wave-1.5a notes direct for C-32 — the Appendix-O mirror
of `test_guards.py` (44 packages; `ccusage.py` → CLI-LEDGER; `tokenbill/copilot/__init__.py` → F-CORE).

### Copilot facts (`core/facts.json` → `copilot`) and provenance

Transcribed on 2026-09-23 from addendum §19 (research tracks and fact-checks, `verification:
"research"` until the Copilot release gate, R-E19) and from local copies of the primary sources in the
research snapshot. The 46 rate rows are the §19.2 current and history rows; the generator replayed the 38
dated revisions of `github/docs:data/tables/copilot/models-and-pricing.yml` (`copilot/raw/yml`) and
asserted every row's prices on every day-final revision inside its interval; each row cites the first
revision carrying its prices. No fixture files: every record is built in the tests.

### Unverified (shipped as `research`, disabled or flagged `verified: false`)

- K-dated `effective_from` dates (docs merge dates, not billing dates) and the long-context threshold
  reading (272K = 272,000; hypothesis A vs B); GPT-5.6 rows 2026-07-09 → 08-03 have no write price (write
  bucket disabled).
- Claude 1-hour write price = 2 × input (`write_1h_rule`, `verified: false`); tokenizer families of
  non-Claude rows; `github.auto` / `github.compliance` stacking (`assumed`).
- SKU `copilot_standalone` → Business; `spark_ai_credits` vs `spark_ai_credit`; quota → plan map (1,900 /
  3,000 / 3,900 / 7,000); billing `workflow_path` patterns (incl. `.lock.yml`); JetBrains / Visual Studio
  / Xcode / CLI editor and surface strings; VS Code `agent-traces.db` per-OS directories and extension id.
- `remaps` are design candidates (same vendor; `tokenizer_same` from the rows' families), not facts.

### Deviations from the literal C-list (forced by unedited wave-0/1 tests)

- C-8: the `copilot_trigger` / `context_tier` value domains are published as
  `records.COPILOT_EVENT_VALUE_DOMAINS` but not enforced by `LaneEvent`, because `strategies.py`
  (unedited, T-4) draws free strings for every nullable str attr. Likewise `copilot_compliance` values are
  documented, not enforced (the strategies draw free extra values).
- `to_json` leaves fields appended in wave 1.5 out while they hold their default (`records.appended`,
  `OMIT_DEFAULT`), so every pre-Copilot document — e.g. BLOCK's checked-in fixtures on the v0.2 canary — is
  byte-identical; `from_json` restores the defaults.
