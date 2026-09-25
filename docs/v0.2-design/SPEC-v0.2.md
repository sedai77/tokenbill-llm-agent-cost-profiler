# Token Bill v0.2 — Internal Specification (authoritative)

Status: build contract, revision 2, 2026-09-23. Supersedes `docs/SPEC.md` (the v0.1 contract survives as the
"legacy engine" appendix, §L). Every builder implements against this document exactly. Where this
document and a work-package brief disagree, **this document wins**; raise a contract-change request
(§21 #3) instead of improvising. Revision 2 resolves every defect raised by the two design critics (the
resolution of each is recorded in the decision log D26–D45 and in §20).

Evidence base: the 467 adversarially verified research findings (IDs in `code`, e.g. `cc-cold-resume`;
where a finding was corrected, the corrected version is used), the empirical Claude Code corpus (one
heavy user, 43,383 de-duplicated billed calls, $11,632.75 at list price over 134 days — used for
**mechanisms and orders of magnitude only, never as a fleet expectation**), and three design
proposals (trust, fleet, savings) merged per three judgments (lab reviewer, FinOps lead, staff engineer).

Hard constraints (product owner): Python ≥ 3.10; **zero runtime dependencies** (stdlib only; dev deps
pytest, ruff, hypothesis, coverage); local-first, no telemetry; content-free ("metadata-only") processing
possible for every fleet source; never rank individuals by spend, team-level aggregation with k-anonymity
by default; every number labeled exact / estimated / measured / verified and never an estimate shown as
billed; `tokenbill/trace@1` files and the `demo` / `analyze` verbs keep working.

---

## 0. Decision log (conflicts resolved by the lead architect)

| # | Conflict | Decision | Why |
|---|---|---|---|
| D1 | Base architecture | Trust proposal's core (attempt ledger, `Figure`, three-layer reconciliation, predictive calibration, block engine), wrapped in the fleet proposal's FinOps/collection layer and the savings proposal's delivery layer (step-algorithm replay, emitters, hook, oracle). | All three judges. |
| D2 | v0.1 engine: freeze vs targeted fixes | `common.py trace.py analyzer.py simulator.py breakers.py report.py demo_traces.py __main__.py` are **never edited**. Known-wrong v0.1 answers are fixed **without editing them**: (a) `cli.py` namespaces colliding `run_id`s across files ($22 not $40); (b) `analyze --engine auto` (default) first runs the frozen strict `read_trace()` and applies `--model-price`, then routes to the v2 engine only when the parsed runs show a v1-unsafe condition: moving `cache_control` markers, `ttl:"1h"` markers, or interleaved models (§15.1). **An unknown model is not a routing trigger**: v1 prints "unavailable" for it, which is honest and pinned by the frozen `tests/test_cli.py`; partial totals for unknown models are available with `--engine v2`. (c) `pricing.py` gains additive rows. Demo and analyze output on inputs without those conditions stays byte-identical to 0.1.2 (golden tests). | Staff: freeze; lab: fix wrong defaults; FinOps: golden bytes; critic 2 blocker (frozen test_cli). |
| D3 | One interchange format vs `usage@1` | **One** format, `tokenbill/trace@2`, with three profiles: `usage` (content-free; what the fleet proposal called usage@1, including its strict validation), `fingerprint` (+ HMAC block hashes), `full` (local only, never exported). | Staff: one format; lab/FinOps: strict content-free collector files. |
| D4 | Keying requests | Transcripts and SDK streams key by `message.id`. `requestId` is only a cross-source join hint, guarded by a collision check (the corpus has one `requestId` mapping to two `message.id`s). | Staff + FinOps. |
| D5 | Identity in transit | Default `central` mode: collectors emit only an **opaque employee/device id** (`r_…`, never an email; validator rejects `@`), central ingest HMACs it with the org key, which never leaves the central host. Optional `two-stage`: laptops HMAC with a collection key (`c_…`), central re-HMACs with the org key. Key rotations are recorded. Two key roles are kept apart in every adapter call: the **name key** (HMACs MCP/skill/plugin names, cwd, repo, api keys; the collection key fleet-wide, so the same name gets the same `h_` on laptops and on the central host) and the **principal key** (pseudonymizes people) — D39. | Lab (org key on laptops is re-identifiable); must-cut raw emails; critic 2 key-role defect. |
| D6 | Money representation | Rates are decimal strings; derivation in `Decimal` under a context that traps `Inexact`; each bucket of each line is rounded half-even **once** to **int nano-USD**; storage is int nano (SQLite `SUM` is exact); JSON money = decimal string + int; receipts int micro-USD. Replay hot loops use exact integer unit rates at a per-key power-of-ten scale, falling back to `Decimal` when the scale would exceed 10⁻²⁴; both paths agree to the nano. No "all pico rates must be integral" load failure. **Exactness is decided per priced line** (D27). | Lab + FinOps + staff. |
| D7 | Calibration | Two gates. **Ledger gate** = reconciliation (§12). **Model gate** = one-step-ahead predictive replay that never reads the predicted transition's billed reads, NMBE/CV(RMSE) vs FEMP thresholds, ≥ 12 periods, simple per-gap-band hit rate ρ with Wilson CIs and day-level 5-fold cross-validation, diagnostics confusion matrix on the `*_changed` labels only (Anthropic's four; OpenAI's reasons mapped per §9.6) (§9.6). "As-is replay equals the bill" is kept only as a code-identity unit test. No multi-dimensional ρ grid, no NNLS density fit. | All three reject the tautological gate; staff trims the extras. |
| D8 | TTL / keepalive replay | Two-way flips (§9.3.1): 5m→1h turns TTL-expiry misses into hits (reads = min(P₍ᵢ₋₁₎, Tᵢ), writes = appended tokens); 1h→5m turns hits into misses (reads fall to the static-prefix floor). The rule "writes are never converted to reads" is cut. | All three. |
| D9 | Keepalive ping count | Non-clairvoyant daemon: `n = min(ceil(gap/κ) − 1, floor(max_idle/κ))` pings (0 if gap ≤ κ); warm iff `gap ≤ τ` or `n·κ + τ ≥ gap`. A 20-minute gap costs 4 pings. Offered only to SDK/API agents, never Claude Code. Break-even idle is `κ(w/r − 1)` (κ = 240 s ping interval) — ≈ 46 min at 0.1× reads. | Lab formula; critic 1 (formula used κ, not τ). |
| D10 | Refused attempts | Declined attempt with 0 output: not billable (documented). With 1–16 output tokens: billable **range** [0, full], ESTIMATED (billing rule `anthropic.refusal.ambiguous`; the threshold is configurable data, default 16; the corpus shows 4/6/9-token declines). Above 16: billable, EXACT (mid-stream decline). | Lab/FinOps. |
| D11 | Cold-resume premium label | The billed rewrite cost is EXACT (`cost_observed`); the premium versus a hypothetical warm read is ESTIMATED. Only rate arithmetic on billed tokens is exact. | Lab must-cut overrides staff. |
| D12 | trace@1 cache writes | EXACT at the 5m rate iff the recorded call carries no `ttl:"1h"` marker; otherwise `cache_write_unknown` with hint `1h` (range [5m, 1h], ESTIMATED). Footnote on every trace@1-derived bill. | Lab + FinOps. |
| D13 | Recorder default | `Recorder(path)` keeps writing trace@1 exactly as v0.1. trace@2 is opt-in via `format="trace@2"`, default content tier `none`, `fingerprint` recommended for API agents. | FinOps + staff (2 of 3). |
| D14 | Detector scope | 20 registered detector classes: 18 usage-level (finding kinds are mostly cause sub-kinds of one mechanism), 1 aggregate-level org scan (`aggregate.org-scan`, works from Admin/Analytics data alone), 1 block-level class emitting 11 breaker kinds (§10). Savings' ~70-detector catalog is cut (reasons per item in §20). | All three ("about 20"); critic 1 (org scan, truncation, edit churn, static prefix, default model, rebaseline). |
| D15 | Estimators | Imputation DiD, CUPED cluster difference-in-means, paired lab A/B (task-clustered bootstrap), and — for org-wide changes that cannot be randomized — a simple event-study interrupted time series with HAC standard errors and a placebo date, whose label is capped at MEASURED (§13.3). Callaway–Sant'Anna, switchbacks, confidence sequences, Wilcoxon, empirical-Bayes RR database, SDID are cut; the realization rate is stored per receipt and the observed mean per class is displayed next to the prior. | Lab + staff; critic 1 (org-wide defaults need a MEASURED path). |
| D16 | Search | Named lever grids evaluated on a seeded lane sample; Shapley over joint replays on the sample (exact ≤ 6 interacting levers per group, 200 seeded permutations with SE above), scaled so Σφ equals one full-scope replay of the selected joint set (§11.2). No coordinate-descent auto-search; trade-off levers are never auto-selected. | Lab must-cut; critic 2 scale defect. |
| D17 | Outputs | result@2 JSON, terminal, HTML, FOCUS 1.4 CSV, SARIF, ccusage-compatible JSON, showback. OTLP metrics export is deferred (§20). | 2 of 3 for ccusage; scope discipline. |
| D18 | Self view | `--self` on `bill`/`findings`/`report`/`scan`; `tokenbill me` is a thin alias for `scan --self` with no logic of its own. | Lab wants `me`; staff wants no separate logic. |
| D19 | FOCUS BilledCost | `export focus` refuses windows whose channels are not all reconciled (exit 3); `--channel` restricts the export to reconciled channels; `--allow-unreconciled` fills `BilledCost` from list/contract with `x_Reconciled=false`. `--role enrichment` zeroes Billed/Effective cost for orgs that also load the provider's own FOCUS/cost feed (double-count guard); every row carries `x_Source`. Seat-allowance usage (`list_equivalent`, D26) has `BilledCost = 0`. | FinOps; critic 1 (per-channel reconciliation). |
| D20 | Foundation size and oracle | The foundation is three packages: **F-CORE** (wave 0: contracts, scaffolding, goldens, facts), then **F-SEM** (shared semantics: conventions, cache rules, transitions, policy grammar, shards, Shapley, detector helpers) and **F-KIT** (fakes, conformance suites, catalogs, keys, k-anonymity) in parallel (wave 1). The ReferenceReplay oracle stays independent (SYNTH-ORACLE, wave 2); its differential test runs at merge gate 1. | Critic 2 (one 7–8k-LOC foundation on the critical path). |
| D21 | Miss threshold | A miss is `M > 0.05·E` **and** `M ≥ 2,000` tokens (Claude Code's documented rule), never 2,048. | Lab + staff. |
| D22 | Content-derived hashes | HMACs of tool inputs/results, attachments or prompt snapshots exist only in the opt-in `fingerprint` tier, never in `none`. The Claude Code importer has no fingerprint tier in v0.2 (D40). | Lab must-cut; critic 2. |
| D23 | OTel raw bodies | `OTEL_LOG_RAW_API_BODIES` events are ignored entirely (counted in data quality, never parsed). | FinOps must-cut. |
| D24 | Provider breadth | Anthropic (1P, Claude Platform on AWS, Foundry, Bedrock, Vertex), OpenAI Responses/Chat usage + pricing (incl. `prompt_cache_diagnostics`), OTel GenAI (incl. legacy names), OpenInference, Claude Code OTel, Claude Code headless/Agent SDK streams, and the **invoice side of the cloud channels**: AWS CUR 2.0 (CSV/CSV.gz) and the GCP billing export (CSV/JSONL) (D31). Gemini, xAI, DeepSeek, Mistral, OpenRouter, LiteLLM SpendLogs, Codex, Cursor, Copilot: facts documented (§19.6), no adapters or rates. The `codex.rollout` convention is registered **disabled**. | All three; critic 1 (Bedrock/Vertex reconciliation blocker). |
| D25 | LOC and partition | 22 work packages + an integration step (~60k LOC incl. tests, ≤ 3k per package except F-CORE at ~3.4k of mostly SPEC transcription). The judges' must-include scope cannot fit 8–14 packages at the 1–3k size both critics insisted on; package count is the lesser risk. Waves: 0 F-CORE → 1 F-SEM ∥ F-KIT → 2 seventeen packages in parallel (incl. WIRING: config + shared pipeline plumbing, so the two CLI packages do not depend on each other) → 3 CLI-LEDGER ∥ CLI-SAVINGS → 4 INTEGRATION (PLAN §1). | Critic 2 sizing; staff (no artificial sequencing inside a wave). |
| D26 | Subscription (seat) usage | Usage inside a seat allowance is not metered in dollars. Inferences whose `PricingContext.billing_path == "subscription"` are priced at API list rates with basis **`list_equivalent`**, which is never billed-eligible (R3), never enters `BilledCost`, and is reported separately as `PricedTotal.allowance`. Overage calls (`quotaLimits.isUsingOverage`, billing path `usage_credits`) are billed at API rates. Savings on subscription cohorts are **allowance headroom**, shown apart from invoice savings (`ActionPlan.allowance_headroom_monthly`). Reconciliation explains it as residual `seat_allowance_unmetered`. | Critic 1 (`billing-path-optimizer`, `cc-seat-vs-usage`, `cc-anthropic-discount-visibility`). |
| D27 | Per-line exactness | A priced inference is split line by line: lines on known billed tokens at a sourced rate are EXACT and enter the exact bill; lines that are ranges (unknown TTL, uncertain billing, placeholder output, unknown endpoint scope) enter the estimated section. So an OTel call's input and output stay exact while only its unknown-TTL writes are a range, and a `MESSAGE_START_ONLY` call's input stays exact while its output line is ESTIMATED `[logged, upper]`. | Critic 1 (placeholder output priced EXACT). |
| D28 | Effort changes and the cache | A top-level effort change invalidates the messages cache on the API. The exemption is conditional (`core.cache_rules.effort_change_keeps_cache`): Claude Code on Opus 5.5 / Fable 5.1 outside Bedrock/Vertex (client ≥ 2.1.260), or an SDK request that carries the per-message effort beta on a supporting model. It is not a property of a rate row; usage-level and block-level engines call the same function. | Critic 1 wrong fact (`anth-invalidation-hierarchy`, `cc-cache-breakers`). |
| D29 | Shared catalogs | The policy grammar (`parse_policy`/`to_spec`/`lane_matches`), the data-only lever catalog, the lifecycle successor/promotion table, the settings allowlist, key-file handling and k-anonymity publishing live in `tokenbill/core/` (F-SEM / F-KIT), so no wave-2 package depends on another wave-2 package's module. | Critic 2 blocker (hidden wave-1 dependencies). |
| D30 | Fleet scale | Analysis runs over **shards** (`core/shards.py`): one shard per team, or per (team, lane kind) when a team exceeds 250k requests in the window. Every detector's cross-lane logic is confined to a (team, lane kind, billing class) cohort, so sharded and unsharded runs are identical (tested). Replays merge additively; calibration runs in two streaming passes over shards; Shapley runs on a seeded sample and is scaled to a full-scope joint replay. `--jobs N` runs shards in a process pool. Fleet budget: `findings` + `plan` over 10⁶ requests ≤ 8 min single-process, peak RSS ≤ 1.5 GB (§17). | Critic 2 scale defect. |
| D31 | Cloud-channel invoices | ADMIN ships `aws-cur` (CUR 2.0 CSV/CSV.gz: `line_item_usage_type`, `line_item_iam_principal`, unblended/net cost) and `gcp-billing` (billing export CSV/JSONL with labels) adapters producing `CostLine`s and token `UsageAggregate`s; the usage-type/SKU → bucket map ships entries marked VERIFY as disabled (they fall to `unmapped_cost_type`). Reconciliation verdicts are **per channel**, so a Bedrock or Vertex fleet without an invoice file blocks only that channel's FOCUS rows and receipts, never the Anthropic API channel. Parquet CUR is not readable with the stdlib (documented: export CSV). | Critic 1 blocker. |
| D32 | Time to first value | `tokenbill scan --org` works from Admin/Analytics (and CUR) data alone: bill, reconciliation and the `aggregate.org-scan` findings (cache-read share vs 84/94/80, write:read thrash, 5m/1h mix, fast/geo/priority premiums, batch share, effective discount) before any collector is rolled out. | Critic 1 (`anth-admin-apis-org-scan`). |
| D33 | Headless and CI traffic | CC ships `claude-code-headless`: claude-code-action `execution_file`, `claude -p --output-format stream-json|json`, and Agent SDK message logs, reusing the transcript de-duplication (message id, max output), the per-step placeholder rule (exact session output residual from the result message) and treating `total_cost_usd` as a provider estimate. `docs/FLEET.md` documents a CI post-step (`upload-artifact` + `tokenbill collect claude-code-headless`). | Both critics (`ci-headless-ingest`, `sdk-accounting-pitfalls`). |
| D34 | Default model and effort levers | PLAN adds `cc.default_model` and `cc.default_effort` (trajectory, `needs_eval`, delivered through managed settings keys marked VERIFY); DETECT adds `model.routing` kinds `default-model`, `default-effort`, `rebaseline` and a `plan-toggle` model-switch sub-cause. | Both critics (`org-defaults-datadog`, `cc-model-effort-mix`). |
| D35 | Recorder retries | Recorder v2 attaches duck-typed httpx event hooks to the wrapped client's HTTP client so SDK-internal retries become separate attempts (status, `x-stainless-retry-count`, `retry-after(-ms)`, `x-should-retry`); when hooks cannot be attached the file carries `dq.sdk_retries_invisible`. | Critic 1 (`fp-recorder-blindspot`, `fp-sdk-retry-after-uncapped`). |
| D36 | Promotional and effective-dated prices | gpt-5.6-sol has two rows: an unverified launch row 2026-07-09 → 2026-08-21 (ships disabled → unpriced) and the promotional $4/$20 row 2026-08-21 → 2026-11-22 (exclusive; "at least through 2026-11-21"); after it, usage is unpriced with `dq.promotion_expired` until a new row is verified. Fable 5.1 / Mythos 5.1 rows are enabled from 2026-09-01 (launch at 0.025× reads). | Critic 1 wrong facts. |
| D37 | Facts single source | F-CORE produces `tokenbill/core/facts.json` in wave 0 (the verified subset of §19 used by fakes, catalogs, evidence constants and FOCUS columns). FakePricer, the allowlist, lifecycle and evidence constants load it; RATES' registry must equal it on every shared row (parity test). One transcription, one place to re-verify. | Critic 2 (facts transcribed four times). |
| D38 | Analyze routing and frozen tests | See D2 (b): routing only after strict `read_trace()` succeeds and `--model-price` has been applied; unknown models are not a trigger. | Critic 2 blocker. |
| D39 | Ingest options | `IngestOptions` carries `name_key`/`principal_key` (with ids), `team_map`, `k_anonymity` and `renormalize`; `SourceInfo` carries the key ids; the store accepts `p_` values only under its org key id and `h_` values only under its name key id. | Critic 2 blocker. |
| D40 | Claude Code fingerprint tier | Deferred: transcripts do not carry full request payloads, and the block engine gets fingerprints from the recorder and trace@1. CC is `none`-tier only. | Critic 2 (hidden CC→TRACE dependency). |
| D41 | Store contract | DDL gains `attr_extra_json`, per-line exact/estimated money columns, `billing_path`, and `LedgerStore` gains cursors, findings, receipts, lane index, lane-first reads and `count_users` (for exact k-anonymous re-scoping). | Critic 2 blocker. |
| D42 | Gate tests and process | Cross-package gate tests exist for every seam (collector → trace@2 → store; SYNTH files through the real adapters; per-plant detector/plan recovery); a named contract owner (the F-KIT agent) may hot-fix core bugs mid-wave; a daily canary merge runs all gate tests (PLAN §1). | Critic 2. |
| D43 | Azure cache scope | Azure OpenAI caches are isolated per subscription (`cache_scope_key = "sub:<id>"`), not per organization. | Critic 1. |
| D44 | Windows | POSIX modes are no-ops on Windows. `open_private` applies a best-effort owner-only ACL via `icacls` and otherwise records `dq.windows_acl_not_enforced`; `docs/FLEET.md` requires collectors on Windows to write into an MDM-restricted directory; CI runs the full suite on Windows × 3.12 with POSIX mode assertions skipped. | Critic 1. |
| D45 | Test layout | pytest's default (prepend) import mode with an `__init__.py` in every `tests/v2/<area>/` test directory; helpers are imported only within their own area; cross-package gates use the producing package's public API or checked-in fixture files, never another area's test helpers. `coverage` is a dev dependency so the 90% line-coverage rule is measurable. | Critic 2. |

---

## 1. Scope

### 1.1 What v0.2 is

A local-first, zero-dependency cost ledger and counterfactual engine for LLM/agent spend. It ingests what
enterprises already have (Claude Code transcripts via an on-device collector, Claude Code headless/CI and
Agent SDK streams, Claude Code OpenTelemetry file exports, recorder traces, OpenAI/Bedrock/Anthropic usage
payloads, Anthropic Admin/Analytics pages, AWS CUR 2.0 and GCP billing exports), normalizes them into one
canonical content-free **attempt ledger**, prices it exactly from a versioned, sourced, effective-dated
**rate registry**, **reconciles** it to provider usage/cost reports per channel, **calibrates** its replay
engine predictively, detects waste by cause, ranks recoverable dollars with **Shapley credit and
realization-rate intervals**, emits **per-cohort managed-settings patches** with projected dollars, and
closes the loop with **measured/verified, signed savings receipts**. An admin with only an Admin API key
gets a reconciled bill and aggregate findings on day one (`scan --org`, D32).

### 1.2 Honesty rules (binding everywhere)

1. Every money value is a `Figure` (§3.4) carrying evidence (`exact | estimated | measured | verified`),
   basis (`list | contract | invoice | provider_estimate | list_equivalent`), finality and calibration.
2. **Exact** = provider-billed usage × a sourced rate row; pure arithmetic, no model. Anything involving a
   counterfactual, assumption or reconstruction is **estimated**. Only the verification module produces
   **measured** or **verified**. Exactness is decided per priced line (D27).
3. Unknown is not zero: unpriced usage is reported as coverage, never as $0.
4. Provider estimates (Claude Code OTel `cost_usd`, Claude Code Analytics `estimated_cost`, headless
   `total_cost_usd`, `/usage` numbers, `cache_missed_input_tokens`, OpenAI `cache_missed_tokens`) are never
   billed numbers.
5. Standalone lever ceilings are never summed or displayed as a total.
6. Planning-assumption dollar figures from the research (e.g. "$12–30k/month") never appear in product
   output. Published benchmarks appear only with their source and date (§3.17).
7. Seat-allowance usage is **list-equivalent**, never billed: it is shown apart from the bill, and savings on
   it are "allowance headroom", never invoice dollars (D26).

### 1.3 Non-goals for v0.2

Not in the inference path (no proxy); not an enforcement system (no caps/budgets/alerts); not a
leaderboard or individual performance tool; not a compressor; not a hosted dashboard (no server, no SSO);
no LLM calls inside Token Bill; no network unless the user passes `--live`; no listening sockets ever.

---

## 2. Architecture

### 2.1 Layers

```
 SOURCES (read-only local files; recorded API pages; live pulls only with --live)
  Claude Code transcripts ─(tokenbill collect claude-code, on-device)──────► trace@2 usage profile ─┐
  CI / headless / Agent SDK streams ─(tokenbill collect claude-code-headless)► trace@2 usage profile ─┤
  trace@1 | trace@2 (Recorder) | OTLP/JSON file exports | OpenAI / Bedrock / Anthropic usage JSONL    │
  Anthropic usage_report / cost_report / CC Analytics / Enterprise Analytics | OpenAI admin |        │
  AWS CUR 2.0 CSV | GCP billing export                                                               │
        │ adapters: convention registry, sum-checks, quarantine, pseudonymization (name / principal keys)
        ▼                                                                                            ▼
  CANONICAL LEDGER  Session ▸ Lane ▸ Request ▸ Attempt ▸ Inference(UsageBuckets, PricingContext)
                    + LaneEvent, UsageAggregate, CostLine, OutcomeAggregate  (content-free, HMAC ids)
        │ store: SQLite WAL, int nano money (exact / estimated / allowance lines), idempotent merge,
        │        rollups, retention; streamed to analysis by SHARD (team or team × lane kind)
        ▼
  PRICING  RateCard = builtin registry ⊕ user rates ⊕ contract overlay → PricedInference ← `bill`
        ├──► LEDGER GATE: reconcile per channel vs provider usage/cost/CUR (3 layers + residuals) ← `reconcile`
        ├──► MODEL GATE: one-step-ahead predictive replay, NMBE/CV(RMSE), ρ, diag matrix ← `calibrate`
        ├──► DETECT: 18 usage detectors + org scan (aggregates) + block breakers (fingerprints) → Findings
        ├──► PLAN: lever grids → sample Shapley → full-scope joint replay → RR intervals → policy pack
        └──► VERIFY: measure plan/run (DiD, CUPED, event-study ITS), ab, receipts (DSSE + ssh-keygen)
  OUTPUTS  result@2 JSON | terminal | HTML | FOCUS 1.4 CSV | SARIF | ccusage JSON | showback | trace@2
```

### 2.2 Module map and owners

Owner IDs are the work packages of PLAN-v0.2.md. **FROZEN** = never edited by anyone (a CI check enforces
it). Every `__init__.py` listed as F-CORE is a docstring-only file created by the foundation.

```
tokenbill/
  __init__.py                    INTEGRATION (version → "0.2.0")
  __main__.py common.py trace.py analyzer.py simulator.py breakers.py report.py demo_traces.py py.typed  FROZEN
  pricing.py                     RATES   (v0.1 API; additive rows only; §6.8)
  instrument.py                  TRACE   (Recorder v1 behavior + v2 mode)
  cli.py                         CLI-LEDGER (static lazy command table for every command, §15)
  config.py                      WIRING
  gate.py                        CLI-SAVINGS
  core/  errors records money labels types protocols registry ids jsonl textsafe secrets models lanes
         evidence facts builders facts.json                                         F-CORE
         conventions cache_rules transitions shapley policy shards findings          F-SEM
         testing catalog keys kanon                                                   F-KIT
  adapters/ __init__ (F-CORE); claude_code cc_collect cc_headless (CC); fingerprint trace_v1 trace_v2 (TRACE);
            conventions_ext otel openai bedrock anthropic_responses (TELEM);
            anthropic_admin openai_admin cloud_billing (ADMIN)
  rates/    __init__ (F-CORE); schema engine contract billing_rules verify; data/*.json, data/snapshots/* (RATES)
  recon/    __init__ (F-CORE); reconcile residuals costmap pull orgscan (RECON)
  store/    __init__ (F-CORE); schema db merge rollups retention pseudonym (STORE)
  sim/      __init__ (F-CORE); usage_replay calibrate (REPLAY); block_replay (BLOCK)
  detect/   __init__ (F-CORE); cache_miss cache_ttl cache_structure (DETECT-CACHE);
            context premium model failure automation tail (DETECT-OTHER); block (BLOCK)
  plan/     __init__ (F-CORE); action_plan realization policy_pack litellm effectiveness;
            templates/tokenbill_session_start.py (PLAN)
  verify/   __init__ (F-CORE); stats estimators its ab rollout label_policy receipts panel (VERIFY)
  outputs/  __init__ (F-CORE); result_json terminal html focus sarif ccusage showback (OUT)
  finops/   __init__ (F-CORE); allocation workload (OUT)
  synth/    __init__ (F-CORE); oracle lanes_gen (SYNTH-ORACLE); fleet truth writers (SYNTH-FLEET)
  pipeline/ __init__ (F-CORE); common (WIRING); ledger (CLI-LEDGER); savings verification (CLI-SAVINGS)
  commands/ __init__ (F-CORE); demo analyze init collect ingest bill reconcile export showback pricing purge
            (CLI-LEDGER); scan me calibrate findings whatif policy measure ab receipt check report (CLI-SAVINGS)
tests/
  conftest.py, v2/__init__.py (F-CORE)   test_*.py existing: FROZEN except tests/test_pricing.py (RATES)
  v2/<area>/…  and  v2/fixtures/<area>/…  one area per package (PLAN §2); v2/golden/ (F-CORE);
  v2/gates/ (F-KIT: gate-1 smoke); v2/e2e/ (INTEGRATION)
OWNERSHIP.toml, scripts/check_ownership.py, scripts/capture_goldens.py, pyproject.toml, uv.lock, .gitignore,
  .github/workflows/ownership.yml                                               F-CORE
README.md DESIGN.md CHANGELOG.md CONTRIBUTING.md SECURITY.md Makefile docs/* .github/* (other files)
  scripts/{sbom.py,check_zero_deps.py,repro_check.sh,perf_gates.py}               INTEGRATION
CODE_OF_CONDUCT.md LICENSE examples/**                                          FROZEN
```

### 2.3 Data flow of the three main paths

**Local review (`tokenbill scan`, one developer, content-free):** discover `~/.claude/projects/**/*.jsonl`
(skip `journal.jsonl`) → Claude Code adapter → temporary SQLite store (deleted unless `--db`) → price →
model gate → detectors → action plan → terminal/HTML/JSON. Self-view only (`--self` implied).

**Org scan (`tokenbill scan --org`, an admin with an Admin key, no collectors yet):** recorded or `--live`
usage/cost/Analytics pages (+ CUR/GCP billing files) → temporary store → reconcile per channel →
`aggregate.org-scan` findings → terminal/HTML/JSON. Aggregate-only (workspace/model/key-level, no people).

**Fleet (thousands of developers):** MDM runs `tokenbill collect claude-code` on each laptop and CI runs
`tokenbill collect claude-code-headless` as a post-step → trace@2 `usage` profile files → org log shipping →
central `tokenbill ingest` (plus OTLP file exports, provider usage exports, admin pages, CUR) → store →
`reconcile`, `calibrate`, `findings`, `policy`, `showback`, `export focus`, `measure`, `receipt`, streamed by
shard. Only k-anonymous team aggregates leave the store.

### 2.4 Engineering conventions

- `from __future__ import annotations`; full type hints; frozen `slots=True` dataclasses for records; stdlib
  `logging`; docstrings on public functions; `ruff` clean (existing config); Python 3.10 syntax (no
  `tomllib`; config is JSON).
- **Enums** subclass `core.records.TBEnum(str, Enum)`, whose `__str__` and `__format__` return `.value`
  (Python 3.12 changed `format()` of mixed-in str enums; f-strings of contract enums must be identical on
  3.10–3.13). `IntEnum`s are only used for `Fidelity`.
- **No floats in money code.** `core/money.py`, `core/labels.py`, `rates/`, `recon/`, `store/`,
  `adapters/cloud_billing.py`, `verify/receipts.py`, `outputs/result_json.py`, `outputs/focus.py` must
  contain no `float(` call and no float literal (AST lint, §8.9). Statistics (`verify/stats.py`,
  `verify/estimators.py`, `verify/its.py`) compute in float and convert results to int nano with an
  ESTIMATED/MEASURED label.
- Determinism: same inputs + same seed ⇒ byte-identical outputs across processes and across shard sizes
  and `--jobs` values. Randomness only via `common.rng(seed, *scope)`. Sorting is total (ties broken by
  stable ids). `--deterministic` removes wall-clock fields.
- No network: an autouse fixture in `tests/conftest.py` fails any socket connect to a non-loopback address
  (loopback and `socketpair`, which asyncio uses on Windows, stay allowed). Only `--live` code paths
  (`reconcile --live`, `scan --org --live`, `pricing verify --live`) open sockets, and they are tested with
  recorded pages through an injected opener.
- Errors: every expected failure raises a `TokenbillError` subclass (§3.1) with a content-free message.
  Parsers quarantine malformed records instead of aborting (unless `--strict`).
- Timestamps are `int` milliseconds since the Unix epoch, UTC. Dates are `YYYY-MM-DD` UTC strings.
- Tests: default (prepend) import mode; `__init__.py` in every `tests/v2/<area>/` test directory (D45);
  fixture directories hold data and optional stand-alone build scripts (run as scripts), no `__init__.py`.

---

## 3. Foundation contracts (`tokenbill/core/`, packages F-CORE, F-SEM, F-KIT; frozen after wave 1)

Everything in this section is implemented exactly as written (signatures are binding; bodies are
described). F-CORE (wave 0) builds §3.1–§3.10, §3.12, §3.13, §3.17 and §3.24–§3.25; F-SEM (wave 1) builds
§3.11, §3.14–§3.16, §3.19, §3.21, §3.22; F-KIT (wave 1) builds §3.18, §3.20, §3.23 and the key/k-anonymity
modules of §8. A field may not be added, removed or renamed after wave 1 except through the
contract-change process (§21 #3). Methods of F-CORE types whose behavior belongs to F-SEM (e.g.
`Policy.spec()`) delegate lazily to the F-SEM module named in their docstring.

### 3.1 `core/errors.py`

```python
from tokenbill.common import TokenbillError   # existing base class

class UsageError(TokenbillError): ...        # CLI exit 2
class GateFailed(TokenbillError): ...        # exit 3 (reconcile/calibrate/check/verify/export gates)
class PrivacyError(TokenbillError): ...      # e.g. group_by principal, export of content tier full
class PricingError(TokenbillError): ...      # registry load/validation failures
class ContractViolation(TokenbillError): ... # a result object fails a construction-time invariant
class SourceError(TokenbillError): ...       # unreadable source; message names file + locator only
```

### 3.2 `core/records.py` — canonical ledger records

All dataclasses `frozen=True, slots=True`. Token counts are `int` in `[0, 2**53]` (validated in
`__post_init__`, raising `ContractViolation`). Money is `int` nano-USD. Enums subclass `TBEnum` (or
`IntEnum`) so they serialize and format as values on every supported Python.

```python
MAX_TOKENS = 2**53

class TBEnum(str, Enum):
    def __str__(self) -> str: return self.value
    def __format__(self, spec: str) -> str: return format(self.value, spec)

class ContentTier(TBEnum):
    NONE = "none"                # numbers, enums, allowlisted names, HMAC ids only
    FINGERPRINT = "fingerprint"  # + per-block HMAC hashes and byte lengths
    FULL = "full"                # + raw content; local only; export refuses it

class LaneKind(TBEnum):
    MAIN = "main"; SUBAGENT = "subagent"; WORKFLOW_AGENT = "workflow_agent"; HELPER = "helper"
    COMPACTION = "compaction"; API_RUN = "api_run"; UNKNOWN = "unknown"

class InferenceKind(TBEnum):
    MESSAGE = "message"                     # ordinary billed inference (iterations[].type "message")
    COMPACTION = "compaction"               # server/client compaction pass
    ADVISOR = "advisor"                     # advisor sub-inference (priced at the advisor model)
    FALLBACK_DECLINED = "fallback_declined" # refused attempt preceding a fallback_message
    FALLBACK = "fallback"                   # iterations[].type "fallback_message"
    KEEPALIVE = "keepalive"                 # counterfactual only (max_tokens 0 cache refresh)
    OUTPUT_RESIDUAL = "output_residual"     # headless/SDK session output not attributable per step (§5.12)
    OTHER = "other"

class UsageSource(TBEnum):
    FINAL = "final"                         # final usage frame / non-streaming response
    MESSAGE_START_ONLY = "message_start_only"  # output is the streaming placeholder (a lower bound)
    PARTIAL_STREAM = "partial_stream"       # aborted stream: message_start input + streamed output
    ESTIMATED = "estimated"                 # reconstructed (e.g. hidden compaction call)
    PROVIDER_ROLLUP = "provider_rollup"     # aggregate source (never per request)

class Outcome(TBEnum):
    OK = "ok"; HTTP_ERROR = "http_error"; ABORTED = "aborted"; TIMEOUT = "timeout"
    REFUSED = "refused"; NETWORK_ERROR = "network_error"; UNKNOWN = "unknown"

class WorkloadClass(TBEnum):
    INTERACTIVE = "interactive"; CI = "ci"; SCHEDULED = "scheduled"; EVAL = "eval"
    BATCH = "batch"; SERVICE = "service"; UNKNOWN = "unknown"

class Fidelity(IntEnum):
    AGGREGATE = 0      # bucket-derived
    ESTIMATED = 1      # placeholder or synthesized usage
    NO_TTL_SPLIT = 2   # final usage without 5m/1h split (Claude Code OTel)
    FULL = 3           # final usage with TTL split and iterations (transcripts, recorder, Anthropic API)

BILLING_PATHS = ("api_key", "subscription", "usage_credits", "bedrock", "vertex", "foundry",
                 "claude_platform_aws", "openai", "azure_openai", "unknown")
def billing_class(billing_path: str | None) -> str      # "allowance" iff billing_path == "subscription", else "billed"

@dataclass(frozen=True, slots=True)
class UsageBuckets:
    """Disjoint billed token buckets. total_input = uncached_input + cache_read + all writes."""
    uncached_input: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_write_other: int = 0              # a single-class write with a known TTL (OpenAI 5.6+: 30m)
    cache_write_other_ttl_s: int | None = None   # required iff cache_write_other > 0 (e.g. 1800)
    cache_write_unknown: int = 0            # writes whose TTL split the source did not report
    output: int = 0                         # billed output INCLUDING thinking/reasoning
    output_reasoning: int | None = None     # informational subset of output; never added
    web_search_requests: int = 0
    web_fetch_requests: int = 0
    # __post_init__: all ints in range; output_reasoning <= output; ttl rule above.
    # properties: cache_write (5m+1h+other+unknown), total_input
    # __add__: bucket-wise; output_reasoning summed only if both not None; mixing two different
    #          non-zero cache_write_other_ttl_s raises ContractViolation.

@dataclass(frozen=True, slots=True)
class PricingContext:
    provider: str            # "anthropic" | "openai"
    channel: str             # "anthropic_api" | "claude_platform_aws" | "foundry" | "bedrock" | "vertex"
                             # | "openai_api" | "azure_openai" | "unknown"
    model: str               # normalized id (core/models.py), e.g. "claude-opus-5-5"; "" if unknown
    model_raw: str           # exactly as reported
    service_tier: str = "standard"   # "standard" | "batch" | "flex" | "priority" | "fast" | "unknown"
    speed: str = "standard"          # Anthropic "standard" | "fast"
    inference_geo: str | None = None # "us" | "global" | None ("not_available" → None)
    endpoint_scope: str = "unknown"  # "global" | "regional" | "multi_region" | "unknown". Only Bedrock/Vertex
                                     # (and OpenAI regional processing) price by scope; "unknown" there is an
                                     # ESTIMATED [global, regional] range (§6.2). 1P ignores it.
    write_ttl_hint: str | None = None  # "5m" | "1h" | None: point estimate for cache_write_unknown
    billing_path: str = "unknown"    # one of BILLING_PATHS; "subscription" ⇒ basis list_equivalent (D26)

@dataclass(frozen=True, slots=True)
class Inference:
    """Atomic priced unit: one element of Anthropic usage.iterations, or the whole usage when absent."""
    inference_id: str
    kind: InferenceKind
    usage: UsageBuckets
    pricing: PricingContext
    usage_source: UsageSource = UsageSource.FINAL
    billable: bool | None = True            # None = billing rule uncertain → priced as a range [0, full]
    billing_rule_id: str | None = None      # e.g. "anthropic.refusal.pre_output"
    output_upper: int | None = None         # MESSAGE_START_ONLY only: adapter's upper estimate of the true
                                            # output tokens (≥ usage.output); the output line is priced as
                                            # ESTIMATED [logged, output_upper] (D27). Tokens, never dollars.
    provider_reported_cost_nano: int | None = None
    provider_reported_cost_basis: str | None = None   # a Basis value; Claude Code OTel = "provider_estimate"

@dataclass(frozen=True, slots=True)
class CacheDiagnostic:
    reason: str      # canonical: "model_changed"|"system_changed"|"tools_changed"|"messages_changed"
                     # |"param_changed"|"key_changed"|"compacted"|"previous_message_not_found"|"unavailable"
    provider_reason: str             # verbatim provider label (e.g. OpenAI "reasoning_effort")
    missed_input_tokens_estimate: int | None   # magnitude only; NEVER priced
    source: str      # "anthropic.cache_diagnostics" | "openai.prompt_cache_diagnostics"

@dataclass(frozen=True, slots=True)
class Attempt:
    attempt_id: str
    attempt_no: int                   # 0-based within the logical request
    ts_start_ms: int                  # request start: the cache TTL is measured from here
    ttft_ms: int | None
    duration_ms: int | None
    outcome: Outcome
    http_status: int | None
    error_type: str | None            # "overloaded"|"rate_limit"|"timeout"|"connection"|"prompt_too_long"
                                      # |"spend_cap"|"thinking_binding"|"invalid_request"|"auth"|other
    retry_layer: str | None           # "sdk" | "agent" | "gateway" | None
    retry_after_ms: int | None
    should_retry: bool | None         # x-should-retry
    provider_request_id: str | None   # "req_…": join hint only (D4)
    provider_message_id: str | None   # "msg_…": the de-duplication key for Anthropic responses
    model_served: str | None
    stop_reason: str | None           # provider stop reason; OpenAI incomplete max_output_tokens → "max_tokens"
    inferences: tuple[Inference, ...]
    diagnostics: CacheDiagnostic | None = None
    applied_edits: tuple[tuple[str, int], ...] = ()   # (edit type, cleared_input_tokens)
    thinking_dropped: int = 0         # count of input_transformations of type "thinking_dropped"
    sdk_retry_count: int | None = None  # x-stainless-retry-count of this HTTP attempt (recorder hooks)
    raw_usage_json: str | None = None # canonical JSON of the provider usage object, numbers and
                                      # allowlisted enum strings only (≤ 8 KiB); enables re-normalization
    convention_id: str | None = None  # convention used to derive `inferences` (§5.2)

@dataclass(frozen=True, slots=True)
class Breakpoint:
    block_index: int                  # index into ContentFingerprint.blocks
    ttl: str                          # "5m" | "1h" | "30m"
    assumed: bool = False             # True when inferred (trace@1 count without positions, §5.5)

@dataclass(frozen=True, slots=True)
class RequestParams:
    """Prompt-affecting parameters (the invalidation hierarchy lives outside rendered bytes)."""
    model_requested: str
    max_tokens: int | None = None
    stream: bool | None = None
    thinking: str | None = None        # "off" | "adaptive" | "enabled:<budget>"
    effort: str | None = None          # effective effort of THIS request: Claude Code perTurnEffort if present,
                                       # else effort; "low"|"medium"|"high"|"xhigh"|"max"
    session_effort: str | None = None  # Claude Code session-level `effort` (sticky default), informational
    tool_choice: str | None = None     # "auto"|"any"|"none"|"tool:<hmac>"
    output_format: str | None = None   # HMAC of output_config.format (fingerprint tier) or "set"/None
    speed: str | None = None
    service_tier_requested: str | None = None
    inference_geo_requested: str | None = None
    betas: tuple[str, ...] = ()        # sorted anthropic-beta values
    breakpoints: tuple[Breakpoint, ...] = ()
    automatic_caching: bool | None = None   # top-level cache_control present
    context_management: str | None = None   # "set" or HMAC (fingerprint tier)
    task_budget: int | None = None
    web_search_enabled: bool | None = None
    citations_enabled: bool | None = None
    has_images: bool | None = None
    advisor_model: str | None = None
    disable_parallel_tool_use: bool | None = None

@dataclass(frozen=True, slots=True)
class BlockRef:
    """One rendered block in wire order, content-free. Hashes: HMAC-SHA256 hex[:32] under key_id."""
    h: str               # wire bytes with every cache_control key removed (marker moves never alter h)
    h_sorted: str | None # key-sorted canonical rendering (serialization-churn detection)
    h_norm: str | None   # h after replacing volatile spans by class placeholders
    tier: str            # "tools" | "system" | "messages"
    kind: str            # "tool_def"|"system_text"|"text"|"tool_use"|"tool_result"|"image"
                         # |"document"|"thinking"|"redacted_thinking"|"compaction"|"other"
    role: str | None
    n_bytes: int
    est_tokens: int | None           # estimate; never billed
    image_px: tuple[int, int] | None = None
    volatile_classes: tuple[str, ...] = ()   # computed BEFORE hashing: "iso_datetime","uuid","unix_ts","counter"
    lookback_pos: int = 0            # position index with tool_use / tool_result runs collapsed
    deferred: bool = False           # tool definition sent with defer_loading (tool search)

@dataclass(frozen=True, slots=True)
class ContentFingerprint:
    key_id: str                      # hashes are comparable only within one key
    blocks: tuple[BlockRef, ...]
    tier_end: tuple[int, int, int]   # index after the last tools / system / messages block

@dataclass(frozen=True, slots=True)
class AppendedItem:
    """Content-free summary of what entered the context since the previous request of the lane."""
    kind: str            # "tool_result" | "user_text" | "attachment" | "image" | "assistant"
    name: str | None     # allowlisted tool/attachment type name, else "h_"+HMAC (MCP/skill/plugin names)
    n_bytes: int
    is_error: bool = False
    images: int = 0

@dataclass(frozen=True, slots=True)
class Attribution:
    principal: str | None = None     # "p_<20hex>" pseudonym (store); in transit also "r_<opaque>" or "c_<20hex>"
    team: str | None = None
    cost_center: str | None = None
    project: str | None = None
    repo: str | None = None          # "h_…" HMAC (name key) unless policy allowlists plain names
    workspace_id: str | None = None  # provider workspace id (not personal); "h_…" when --hash-workspaces
    api_key_id: str | None = None    # "h_…" HMAC (name key)
    agent_product: str | None = None # "claude_code"|"agent_sdk"|"api"|framework name
    agent_type: str | None = None    # "general-purpose"|"Explore"|"workflow-subagent"|custom
    query_source: str | None = None  # "main"|"subagent"|"auxiliary"|"compaction"
    skill: str | None = None         # allowlisted or "h_…"
    mcp_server: str | None = None    # allowlisted or "h_…"
    plugin: str | None = None        # allowlisted or "h_…"
    workload_class: WorkloadClass = WorkloadClass.UNKNOWN
    entrypoint: str | None = None
    client_version: str | None = None
    billing_path: str | None = None  # one of BILLING_PATHS (mirrored into PricingContext.billing_path)
    cwd_key: str | None = None       # "h_…" HMAC(cwd) with the name key: Claude Code prefixes are per
                                     # machine+directory
    arm: str | None = None           # experiment tags (OTEL_RESOURCE_ATTRIBUTES tokenbill.arm/.wave)
    wave: str | None = None
    extra: tuple[tuple[str, str], ...] = ()   # allowlisted keys only (EXTRA_KEYS), values ≤ 128 chars, sorted

EXTRA_KEYS = ("mdm_group", "gateway", "task_id", "workflow", "run_attempt", "department", "environment",
              "endpoint_scope")    # endpoint_scope: "global" | "regional" (--attr; Claude Code on Vertex)     # anything else is dropped at the adapter with dq.unknown_fields

@dataclass(frozen=True, slots=True)
class SourceRef:
    adapter: str          # "claude-code" | "claude-code-headless" | "trace@1" | "trace@2" | "otlp" | …
    source_id: str        # "s_" + HMAC of the source path/name (never the raw path)
    locator: str          # "line:1234" etc.; content-free
    fidelity: Fidelity
    priority: int         # recorder 50, claude-code 40, claude-code-headless 38, anthropic-responses 35,
                          # otlp 20, other 10

@dataclass(frozen=True, slots=True)
class Request:
    """One logical request (what the client meant to send once); ≥ 1 attempts."""
    request_id: str
    session_key: str
    lane_key: str
    seq: int                                  # order within lane (ties in ts broken by seq)
    attribution: Attribution
    params: RequestParams
    attempts: tuple[Attempt, ...]
    fingerprint: ContentFingerprint | None = None
    appended: tuple[AppendedItem, ...] = ()
    source: SourceRef | None = None
    # properties:
    #  ts_start_ms        -> attempts[0].ts_start_ms
    #  final_attempt      -> attempts[-1]
    #  serving_inference  -> last inference of final_attempt whose kind in {MESSAGE, FALLBACK}, or None
    #                        (requests without one — e.g. an OUTPUT_RESIDUAL-only request — are skipped by
    #                        classify_transitions and priced as passthrough)
    #  billable_inferences-> every inference of every attempt whose billable is not False
    #  model              -> serving_inference.pricing.model or params.model_requested

class LaneEventKind(TBEnum):
    COMPACTION = "compaction"; CLEAR = "clear"; MODEL_FALLBACK = "model_fallback"
    MODEL_SWITCH_USER = "model_switch_user"; API_ERROR = "api_error"; CONTEXT_EDIT = "context_edit"
    UPGRADE = "upgrade"; CONTEXT_INJECTION = "context_injection"; HUMAN_PROMPT = "human_prompt"
    SESSION_META = "session_meta"; COST_STATE = "cost_state"; IMAGE_EVICTION = "image_eviction"
    QUOTA_STATE = "quota_state"

# Fixed attrs schema per kind (keys and types are part of the contract):
#  COMPACTION        trigger:str("auto"|"manual"), pre_tokens:int, post_tokens:int, duration_ms:int,
#                    dropped_tokens:int|None
#  CLEAR             —
#  MODEL_FALLBACK    from_model:str, to_model:str, trigger:str("refusal"|"availability"|"unknown"),
#                    credited:bool|None
#  MODEL_SWITCH_USER from_model:str, to_model:str
#  API_ERROR         status:int|None, error_type:str, retry_attempt:int|None, max_retries:int|None,
#                    retry_in_ms:int|None
#  CONTEXT_EDIT      edit_type:str, cleared_input_tokens:int
#  UPGRADE           from_version:str, to_version:str
#  CONTEXT_INJECTION att_type:str, n_bytes:int
#  HUMAN_PROMPT      —
#  SESSION_META      agent_type:str|None, spawn_depth:int|None, model_alias:str|None
#  COST_STATE        reported_total_nano:int, reporter:str   (provider estimate; never billed)
#  IMAGE_EVICTION    n_images:int
#  QUOTA_STATE       status:str|None, rate_limit_type:str|None, using_overage:bool|None,
#                    overage_status:str|None, resets_at_ms:int|None   (Claude Code quotaLimits; **VERIFY**)

@dataclass(frozen=True, slots=True)
class LaneEvent:
    lane_key: str
    ts_ms: int
    kind: LaneEventKind
    attrs: tuple[tuple[str, str | int | bool | None], ...] = ()   # sorted by key

@dataclass(frozen=True, slots=True)
class Lane:
    lane_key: str
    session_key: str
    kind: LaneKind
    parent_lane_key: str | None
    cache_scope_key: str          # provider isolation domain: "ws:<id>" (1P/Claude Platform on AWS/Foundry),
                                  # "org:<channel>:<account>" (Bedrock/Vertex/OpenAI), "sub:<id>" (Azure),
                                  # "unknown"
    requests: tuple[Request, ...] # sorted by (ts_start_ms, seq)
    events: tuple[LaneEvent, ...] = ()   # sorted by ts_ms
    ttl_observed: str = "unknown" # "5m" | "1h" | "mixed" | "unknown"
    lane_exact: bool = True       # False when reconstructed heuristically
    # properties: team (attribution.team of requests[0] or None), billing_class (of requests[0])

@dataclass(frozen=True, slots=True)
class Session:
    session_key: str              # globally unique, source-qualified (fixes the reused-run_id bug)
    source_kind: str
    attribution: Attribution
    lanes: tuple[Lane, ...]
    started_ms: int
    ended_ms: int

@dataclass(frozen=True, slots=True)
class UsageAggregate:
    """Provider-side aggregate: Admin usage_report bucket, Analytics user-day rolled to team, CUR usage
    amount, OTel metric, headless result totals."""
    agg_id: str
    source_kind: str              # "anthropic.usage_report"|"anthropic.cc_analytics"|"anthropic.enterprise_usage"
                                  # |"openai.usage"|"aws.cur2"|"gcp.billing_export"|"otel.metric"
                                  # |"claude_code.headless_result"
    bucket_start_ms: int
    bucket_end_ms: int
    dims: tuple[tuple[str, str], ...]   # sorted: channel, workspace_id, api_key_id, model, service_tier,
                                        # inference_geo, speed, context_window, team, product, endpoint_scope
    usage: UsageBuckets
    reported_cost_nano: int | None = None
    reported_cost_basis: str | None = None   # "invoice"|"contract"|"provider_estimate"|"list"
    list_cost_nano: int | None = None        # Enterprise Analytics list_amount
    finality: str = "provisional"            # "provisional" | "final"
    fetched_ms: int = 0

@dataclass(frozen=True, slots=True)
class CostLine:
    """One row of a provider invoice-side report (Anthropic cost_report: daily cents strings; CUR 2.0 line
    items; GCP billing export rows), aggregated to (date, channel, workspace/account, model, bucket)."""
    line_id: str
    source_kind: str               # "anthropic.cost_report" | "anthropic.enterprise_cost" | "openai.costs"
                                   # | "aws.cur2" | "gcp.billing_export"
    date_utc: str
    channel: str                   # "anthropic_api" | "bedrock" | "vertex" | "openai_api" | …
    workspace_id: str | None       # workspace (Anthropic), linked account (AWS, h_), project (GCP, h_)
    description: str               # provider description (a model/cost-type label, never user content)
    model: str | None
    cost_type: str | None
    token_type: str | None
    sku: str | None                # CUR line_item_usage_type / GCP sku.id (content-free provider codes)
    service_tier: str | None
    inference_geo: str | None
    endpoint_scope: str | None
    amount_nano: int               # half-even from the source decimal string (§3.3); remainder → cents_rounding
    list_amount_nano: int | None = None
    currency: str = "USD"
    finality: str = "provisional"
    principal: str | None = None   # CUR line_item_iam_principal → p_ (principal key) — never exported
    fetched_ms: int = 0

@dataclass(frozen=True, slots=True)
class OutcomeAggregate:
    """Team-level outcome/productivity counts (Claude Code Analytics), aggregated at ingest with k ≥ 5.
    Never stored per principal. Used only as a quality guardrail and for active-developer-day counts."""
    date_utc: str
    team: str
    n_users: int                   # distinct users contributing (≥ k or the row is merged/dropped)
    sessions: int
    commits: int
    pull_requests: int
    lines_added: int
    lines_removed: int
    edits_accepted: int
    edits_rejected: int
    source_kind: str = "anthropic.cc_analytics"

@dataclass(frozen=True, slots=True)
class UsageRecord:
    """Denormalized row, one per billable Inference: storage, group-by, reconcile, export."""
    inference_id: str; request_id: str; attempt_id: str; session_key: str; lane_key: str
    lane_kind: LaneKind; ts_ms: int; date_utc: str
    kind: InferenceKind; usage_source: UsageSource; billable: bool | None; billing_rule_id: str | None
    pricing: PricingContext; usage: UsageBuckets; attribution: Attribution; fidelity: Fidelity
```

Helpers in `records.py`: `to_json(obj) -> dict` and `from_json(cls, d) -> obj` for every record (lossless
round trip; enums by value; tuples as lists), used by trace@2, the store and cross-package fixture dumps.

### 3.3 `core/money.py`

```python
EXACT_CTX = Context(prec=60, rounding=ROUND_HALF_EVEN,
                    traps=[InvalidOperation, DivisionByZero, Overflow, Inexact])
RATIO_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, DivisionByZero, Overflow])
NANO_PER_USD = 10**9
MICRO_PER_USD = 10**6
MTOK = 10**6

def usd(value: str | int | Decimal) -> Decimal      # float/bool → TypeError; NaN/Inf → ValueError
def from_cents(value: str | int) -> Decimal          # "12345.678" cents → Decimal("123.45678") USD, exact
def cents_to_nano(value: str | int) -> tuple[int, Decimal]
    # half-even to 1e-9 USD; returns (nano, remainder_usd) where remainder_usd = exact − nano·1e-9
    # (|remainder| ≤ 5e-10; RECON sums remainders into the `cents_rounding` residual). Never raises on
    # many-decimal cents strings.
def usd_str_to_nano(value: str) -> tuple[int, Decimal]  # same for USD decimal strings (CUR, GCP export)
def decimal_to_nano(amount_usd: Decimal) -> int      # ROUND_HALF_EVEN to 1e-9 USD; the ONLY rounding of
                                                     # computed amounts
def token_amount(tokens: int, usd_per_mtok: Decimal, *factors: Decimal) -> Decimal  # exact under EXACT_CTX
def token_nano(tokens: int, usd_per_mtok: Decimal, *factors: Decimal) -> int       # decimal_to_nano(token_amount(...))
def scaled_to_nano(amount_scaled: int, scale_exp: int) -> int   # amount × 10^-scale_exp USD → nano, half-even
def nano_to_usd_str(nano: int) -> str                # exact decimal string, e.g. 1500000 → "0.0015"
def nano_to_micro(nano: int) -> int                  # half-even (receipts)
def ratio(num: int, den: int) -> Decimal | None      # RATIO_CTX; None when den == 0
def fmt_usd(nano: int | None, places: int = 2) -> str   # "$1,234.57"; None → "unpriced"
```

Rounding rule: **each (inference, bucket) line is rounded once** with `decimal_to_nano`; all further sums
are int sums. Source amounts given as decimal strings are rounded once on parse (`cents_to_nano`,
`usd_str_to_nano`). Micro rounding happens only for receipts. Tests: `token_nano(1_000_000, usd("4.00"),
Decimal("0.05")) == 200_000_000`; `token_nano(1, usd("0.25")) == 250`; `token_nano(3, usd("25"),
Decimal("0.8537")) == 64_028` (64,027.5 → even); `from_cents("12345.678") == Decimal("123.45678")`;
`cents_to_nano("0.00000000001")[0] == 0` with remainder `Decimal("1E-13")`; `usd(0.1)` raises `TypeError`;
an inexact operation in `EXACT_CTX` raises `decimal.Inexact`.

### 3.4 `core/labels.py` — `Figure` and the honesty rules

```python
class Evidence(TBEnum):
    EXACT = "exact"          # provider-billed usage × sourced rate row; pure arithmetic
    ESTIMATED = "estimated"  # any model, inference, reconstruction or assumption
    MEASURED = "measured"    # observational causal estimate on the billed ledger, with CI
    VERIFIED = "verified"    # randomized design with every guard passing, with CI
STRENGTH = {Evidence.EXACT: 3, Evidence.VERIFIED: 2, Evidence.MEASURED: 1, Evidence.ESTIMATED: 0}

class Basis(TBEnum):
    LIST = "list"; CONTRACT = "contract"; INVOICE = "invoice"; PROVIDER_ESTIMATE = "provider_estimate"
    LIST_EQUIVALENT = "list_equivalent"   # seat-allowance usage priced at API list; not metered in dollars
class Finality(TBEnum):
    PROVISIONAL = "provisional"; FINAL = "final"; NA = "n/a"
class Calibration(TBEnum):
    CALIBRATED = "calibrated"; UNCALIBRATED = "uncalibrated"; NA = "n/a"

@dataclass(frozen=True, slots=True)
class Figure:
    nano: int | None
    evidence: Evidence
    basis: Basis
    finality: Finality = Finality.NA
    low_nano: int | None = None
    high_nano: int | None = None
    ci_level_pct: int | None = None       # e.g. 95 for MEASURED/VERIFIED
    calibration: Calibration = Calibration.NA
    upper_bound: bool = False
    provenance: tuple[str, ...] = ()      # rate-row ids, source ids, replay ids, receipt ids
    note: str = ""
    # __post_init__ (raise ContractViolation):
    #  nano is None               -> note must start with "unpriced:"
    #  low/high                   -> both or neither; low <= nano <= high when nano is not None
    #  EXACT                      -> no range, calibration NA, upper_bound False
    #  ESTIMATED                  -> calibration != NA or note non-empty (names the assumption)
    #  MEASURED / VERIFIED        -> range and ci_level_pct required
    #  basis INVOICE              -> evidence EXACT
    # properties: usd -> Decimal | None
    #             is_billed_eligible -> evidence EXACT and basis in {LIST, CONTRACT, INVOICE}
    #             (LIST_EQUIVALENT and PROVIDER_ESTIMATE are never billed-eligible)

def add(a: Figure, b: Figure) -> Figure    # basis must match (else ContractViolation); evidence = weaker by
                                           # STRENGTH; ranges add (a point is its own range); nano None if either
                                           # None; calibration = UNCALIBRATED if either is; provenance union
def sub(a: Figure, b: Figure) -> Figure    # a − b (savings); ranges subtract crosswise (low = a.low − b.high)
def scale(a: Figure, num: int, den: int) -> Figure   # exact rational scaling, half-even per bound
def exact(nano: int, basis: Basis, *, provenance=(), finality=Finality.NA) -> Figure
def estimated(nano: int | None, basis: Basis, *, low=None, high=None, calibration=Calibration.UNCALIBRATED,
              upper_bound=False, note="", provenance=()) -> Figure
def unpriced(reason: str, basis: Basis = Basis.LIST) -> Figure
def zero(basis: Basis) -> Figure           # EXACT 0
```

Rules (each has a test in the owning package):

- **R1 No floats in money.** Rates are JSON strings parsed with `usd()`; `EXACT_CTX` traps `Inexact`;
  rounding only in `decimal_to_nano` / `scaled_to_nano` (once per line bucket), on source parse and in
  display.
- **R2 Unknown is not zero.** Unpriced inferences produce `Figure(nano=None)`; totals report priced nano,
  unpriced inferences, unpriced tokens and coverage.
- **R3 Billed columns are exact.** Renderers accept only `fig.is_billed_eligible` in any column titled
  billed / bill / spend / BilledCost; estimates render in an "estimated" column with their range;
  list-equivalent figures render only in an "allowance (list-equivalent)" column.
- **R4 Provider estimates are not bills.** Basis `PROVIDER_ESTIMATE` figures appear only in reconciliation
  and data-quality context.
- **R5 Unknown-TTL writes are ranges.** `cache_write_unknown` is priced at the 5m rate (low) and 1h rate
  (high); the point uses `write_ttl_hint` when present, else the low; evidence ESTIMATED. Exception (D12):
  trace@1 writes are EXACT at 5m iff the call had no `ttl:"1h"` marker.
- **R6 Estimates declare calibration.** Every ESTIMATED savings figure carries `calibration`; only the model
  gate (§9.6) may set `CALIBRATED`; UNCALIBRATED projections cannot be signed or labeled measured/verified.
- **R7 No summing of overlapping ceilings.** Totals of savings come from joint replays; per-lever credit is
  Shapley (§11.2).
- **R8 Constant prices for ex-post savings.** Before/after comparisons reprice both periods at the baseline
  rate card; the price effect is reported separately as an exact "rate variance".
- **R9 Exactness is per line.** A priced inference contributes its exact lines to the exact total and its
  range lines to the estimated total (D27); a whole-inference condition (billable None, usage source
  ESTIMATED, uncertain billing rule) makes every line a range.
- **R10 Allowance is not bill.** `LIST_EQUIVALENT` figures are summed only with each other (`add` enforces
  basis equality) and never appear in billed columns, `BilledCost`, receipts or invoice savings (D26).

### 3.5 `core/types.py` — shared result types

```python
# ---------- ingest ----------
@dataclass(frozen=True, slots=True)
class DataQualityNote:
    code: str            # dotted, stable: see §5.1 list, e.g. "dq.message_start_only"
    severity: str        # "info" | "warn" | "error"
    count: int
    detail: str          # content-free, ≤ 256 chars
    tokens: int | None = None         # token magnitude where relevant (adapters report tokens, never dollars)
    figure: Figure | None = None      # set only by the pipeline after pricing (adapters leave None)

@dataclass(frozen=True, slots=True)
class QuarantineItem:
    source_id: str; locator: str; reason: str      # never the raw line

@dataclass(frozen=True, slots=True)
class SourceInfo:
    source_id: str; adapter: str; name_hmac: str; sha256: str; bytes: int
    name_key_id: str | None          # key id of every h_ value in the result (None: no h_ values)
    principal_key_id: str | None     # key id of every p_/c_ value in the result (None: none or only r_ refs)

@dataclass(frozen=True, slots=True)
class IngestOptions:
    content_tier: ContentTier = ContentTier.NONE
    identity_mode: str = "install"   # "install" (local self-view: principals → p_ with the install key)
                                     # | "central" (fleet collector: opaque ref → r_; no principal key)
                                     # | "two-stage" (fleet collector: ref → c_ with the collection key)
                                     # | "central-ingest" (central host: raw central identities → p_ with the org key)
    name_key: bytes = b""            # HMACs MCP/skill/plugin/tool names, cwd, repo, api keys, workspaces (h_):
                                     # install key locally; the collection key in both fleet modes, on laptops
                                     # AND on the central host, so the same name has one h_ fleet-wide
    name_key_id: str = ""
    principal_key: bytes | None = None   # install key (install), collection key (two-stage), org key
                                         # (central-ingest); None in central collectors
    principal_key_id: str | None = None
    principal_ref: str | None = None # opaque employee/device id supplied by MDM (collector modes)
    attribution: Attribution = Attribution()   # defaults from --attr / OTEL_RESOURCE_ATTRIBUTES / MDM
    team_map: tuple[tuple[str, str], ...] = ()  # raw actor ref → team, applied at ingest then discarded
                                                # (Analytics user rows, CUR IAM principals, OTel users)
    k_anonymity: int = 5             # used by adapters that aggregate people at ingest (§5.11)
    name_allowlist: frozenset[str] = frozenset()  # skill/MCP/plugin/tool names allowed in clear text
    since_ms: int | None = None
    until_ms: int | None = None
    lenient: bool = True             # quarantine bad records; False = first bad record raises SourceError
    renormalize: bool = False        # trace@2: rebuild inferences from raw_usage with current conventions
    now_ms: int = 0                  # injected clock (determinism)

@dataclass
class IngestResult:
    source: SourceInfo
    requests: list[Request]
    sessions: list[Session]          # lanes may carry no requests; core/lanes.py assembles final Lanes
    events: list[LaneEvent]
    aggregates: list[UsageAggregate]
    cost_lines: list[CostLine]
    outcomes: list[OutcomeAggregate]
    quarantined: list[QuarantineItem]
    notes: list[DataQualityNote]
    stats: dict[str, int]            # "lines", "records", "requests", "duplicate_lines", …
    capabilities: frozenset[str]     # capabilities actually present (§5.1)
    naive_usage: dict[str, UsageBuckets] = field(default_factory=dict)
                                     # Claude Code only: Σ usage over every assistant line, per normalized
                                     # model (the 2.33× self-check; the pipeline prices it)

# ---------- pricing registry format (frozen contract; RATES implements load/validate) ----------
@dataclass(frozen=True, slots=True)
class SourceCitation:
    url: str; retrieved: str; finding: str | None

@dataclass(frozen=True, slots=True)
class RateRow:
    row_id: str                      # "<provider>/<channel>/<model>/<effective_from>"
    provider: str; channel: str; model: str
    aliases: tuple[str, ...]
    generation: str                  # e.g. "5.5", "4.6"; compared numerically by modifiers
    effective_from: str              # "YYYY-MM-DD" inclusive
    effective_to: str | None         # exclusive; None = open
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    cache_read_mult: Decimal | None  # None → provider has no cache reads
    cache_write_5m_mult: Decimal | None
    cache_write_1h_mult: Decimal | None
    cache_write_other_mult: Decimal | None      # OpenAI 5.6+: 1.25 (30m)
    cache_write_other_ttl_s: int | None
    published_absolute: tuple[tuple[str, Decimal], ...]   # bucket → USD/MTok as published (validator)
    min_cacheable_tokens: int | None
    tokenizer_family: str            # "claude-4.7+" | "claude-legacy" | "openai-o200k" | …
    per_request_usd: tuple[tuple[str, Decimal], ...]      # ("web_search", Decimal("0.01"))
    long_context_threshold: int | None
    long_context_usd_per_mtok: tuple[tuple[str, Decimal], ...]   # bucket → rate for the whole request
    supports: tuple[str, ...]        # "fast_mode","inference_geo","1m_context","batch","keepalive",
                                     # "mid_conversation_system","per_message_effort",… (cache-rule behavior
                                     # such as effort exemptions is NOT a rate-row property, D28)
    enabled: bool                    # False = VERIFY row: loaded, never priced (unpriced reason "unverified row")
    verified_on: str
    sources: tuple[SourceCitation, ...]
    promotion: str | None = None     # promotion id (core.catalog.PROMOTIONS) when the row is promotional
    notes: str = ""

@dataclass(frozen=True, slots=True)
class Modifier:
    modifier_id: str                 # "anthropic.batch", "anthropic.inference_geo.us", …
    kind: str                        # "multiply" | "replace_base"
    factor: Decimal | None           # multiply
    base_usd_per_mtok: tuple[tuple[str, Decimal], ...]   # replace_base: ("input", …), ("output", …)
    applies_to: tuple[str, ...]      # bucket names or ("*",)
    when: tuple[tuple[str, str], ...]  # sorted predicates: service_tier, speed, inference_geo, endpoint_scope,
                                       # channel_in (comma list), model_in (comma list), generation_gte
    stacking: str                    # "documented" | "assumed"
    sources: tuple[SourceCitation, ...]

@dataclass(frozen=True, slots=True)
class RateLayer:
    name: str                        # "builtin@2026-09-23" | "user:<file>" | "model-price" | "contract:<name>"
    schema: str                      # "tokenbill/rates@1"
    as_of: str
    rows: tuple[RateRow, ...]
    modifiers: tuple[Modifier, ...]
    sha256: str                      # of the canonical JSON of the layer

@dataclass(frozen=True, slots=True)
class ContractOverlay:
    name: str
    multiplier: Decimal | None       # applied to every bucket after list resolution
    overrides: tuple[tuple[str, tuple[tuple[str, Decimal], ...]], ...]  # model → ((bucket, usd_per_mtok), …)
    effective_from: str
    effective_to: str | None
    derived: bool                    # True when produced by reconcile --suggest-contract
    assumed_fields: tuple[str, ...]  # e.g. ("cache_write_1h",) when derived from modelPricing cacheWrite
    channels: tuple[str, ...] = ()   # channels it applies to (empty = all)
    sha256: str = ""

@dataclass(frozen=True, slots=True)
class ResolvedRates:
    row_id: str; channel: str; model: str
    input: Decimal; output: Decimal; cache_read: Decimal | None
    cache_write_5m: Decimal | None; cache_write_1h: Decimal | None; cache_write_other: Decimal | None
    per_request: tuple[tuple[str, Decimal], ...]
    modifier_ids: tuple[str, ...]
    stacking_assumed: bool
    layer: str                       # "builtin" | "user" | "contract"
    min_cacheable_tokens: int | None
    tokenizer_family: str
    long_context_band: bool          # True when these are band rates
    scope_range: "ResolvedRates | None" = None   # high-side rates when endpoint_scope is unknown (§6.2)

@dataclass(frozen=True, slots=True)
class UnitRates:
    """Exact integer rates for replay hot loops: each bucket rate = numerator × 10^-scale_exp USD/token."""
    scale_exp: int                   # ≤ 24; chosen as the smallest exponent making every bucket integral
    uncached: int; cache_read: int; cache_write_5m: int; cache_write_1h: int
    cache_write_other: int; output: int
    web_search_nano: int
    row_id: str
    def bucket_nano(self, bucket: str, tokens: int) -> int: ...   # scaled_to_nano(tokens × rate, scale_exp)

@dataclass(frozen=True, slots=True)
class PricedLine:
    bucket: str                      # "uncached_input"|"cache_read"|"cache_write_5m"|"cache_write_1h"
                                     # |"cache_write_other"|"cache_write_unknown"|"output"|"web_search"|…
    quantity: int
    unit_usd_per_mtok: str           # decimal string after modifiers (per request for server tools)
    amount_nano: int                 # point, rounded once
    low_nano: int | None             # range lines only (unknown TTL / scope, uncertain billing, placeholder output)
    high_nano: int | None
    exact: bool                      # True iff billed tokens × sourced rate with no range (R9)
    rate_row_id: str
    modifier_ids: tuple[str, ...]
    layer: str

@dataclass(frozen=True, slots=True)
class PricedInference:
    inference_id: str | None
    lines: tuple[PricedLine, ...]
    figure: Figure                   # whole inference: EXACT iff every line is exact; basis LIST/CONTRACT or
                                     # LIST_EQUIVALENT (subscription billing path)
    exact_nano: int                  # Σ amount of exact lines (0 when unpriced)
    estimated: Figure | None         # Σ of range lines (ESTIMATED, same basis), None when every line is exact
    unpriced_reason: str | None

@dataclass(frozen=True, slots=True)
class PricedTotal:
    exact: Figure                    # Σ exact lines on billed bases (LIST/CONTRACT): the billed-eligible number
    estimated: Figure | None         # Σ range lines on billed bases; shown beside, never inside, the bill
    allowance: Figure | None         # Σ every line on basis LIST_EQUIVALENT (EXACT iff all its lines are)
    priced_inferences: int
    unpriced_inferences: int
    unpriced_tokens: int
    coverage: str                    # decimal string: priced billable tokens / all billable tokens

@dataclass(frozen=True, slots=True)
class Discrepancy:
    row_id: str; field: str; ours: str; theirs: str; source: str
    authoritative: bool              # False for LiteLLM/OpenRouter cross-check feeds (warnings only)

@dataclass(frozen=True, slots=True)
class PricingReport:
    kind: str                        # "show" | "verify" | "diff"
    rows: tuple[RateRow, ...]
    modifiers: tuple[Modifier, ...]
    discrepancies: tuple[Discrepancy, ...]
    stale_rows: tuple[str, ...]
    ok: bool

# ---------- simulation ----------
@dataclass(frozen=True, slots=True)
class Policy:
    name: str
    ttl: tuple[tuple[str, str], ...] = ()          # (selector, "5m"|"1h"), e.g. (("lane_kind:main","1h"),)
    keepalive: tuple[str, int, int] | None = None  # (selector, interval_s, max_idle_s)
    compaction_window: tuple[int, int | None] | None = None   # (window_tokens, summary_tokens | None=org median)
    cold_resume: tuple[str, int] | None = None     # ("compact"|"clear", min_context_tokens)
    model_remap: tuple[tuple[str, str], ...] = ()  # (selector, target model id)
    effort: tuple[tuple[str, str, str], ...] = ()  # (selector, max_level, thinking_scale decimal string)
    fast_off: bool = False
    geo_global: bool = False
    regional_to_global: bool = False
    batch: str | None = None                       # predicate id: "eligible"
    repairs: tuple[str, ...] = ()                  # "restore_caching","stagger_fanout","retry_backoff_cap",
                                                   # "fallback_credit","shared_ci_prefix"
    breakpoint_policy: str | None = None           # block-level only: "observed"|"end"|"static_plus_end"|"every_15"
    # methods (delegate to core.policy, F-SEM): observed() -> Policy (all defaults; name "observed");
    # combine(other) -> Policy (union; conflicting scalar fields raise ContractViolation); spec() -> canonical
    # grammar string (§9.5); is_observed() -> bool
# Selector grammar: "all" | "lane_kind:<LaneKind>" | "agent_type:<name>" | "team:<team>" |
#                   "agent_product:<name>" | "billing_path:<path>" | "workload:<class>" | "model:<id>";
#                   comma = AND (§9.5).

@dataclass(frozen=True, slots=True)
class Transition:
    request_id: str; lane_key: str; index: int     # index ≥ 1 within the lane
    gap_ms: int                  # ts_start_i − ts_start_{i−1}
    total: int                   # T_i
    reads: int                   # R_i
    prev_prefix: int             # P_{i−1}
    expected_reuse: int          # E_i
    missed: int                  # M_i
    is_miss_event: bool
    cause: str                   # §3.15 cause slugs; "hit" when not a miss event
    sub_cause: str | None
    ttl_s: int | None            # τ_i in seconds; None when unknown
    ambiguous: bool              # |gap − τ| ≤ 10 s
    predicted_hit: bool | None   # documented one-step-ahead prediction (None: excluded)
    diag_reason: str | None      # canonical CacheDiagnostic.reason

@dataclass(frozen=True, slots=True)
class ReplayRequestOutcome:
    request_id: str
    usage: UsageBuckets          # serving-inference usage under the policy
    extra: tuple[Inference, ...] # keepalive pings, inserted compaction/summary calls
    cost_nano: int | None        # POINT cost of every billable inference of the request (exact lines +
                                 # range-line points), policy applied
    low_nano: int | None
    high_nano: int | None
    changed: bool                # False ⇒ usage and cost identical to the priced ledger

@dataclass(frozen=True, slots=True)
class ReplayResult:
    policy: Policy
    mode: str                    # "documented" | "calibrated"
    baseline: Figure             # observed point cost of the replayed lanes (Σ PricedInference.figure over
                                 # billable inferences; EXACT iff every line exact); basis of the lanes
    cost: Figure                 # policy cost (ESTIMATED unless the policy is observed, then == baseline)
    saving: Figure               # baseline − cost, per request then summed (ESTIMATED; ranges crosswise)
    per_lane: tuple[tuple[str, int], ...]   # lane_key → policy point nano
    outcomes: tuple[ReplayRequestOutcome, ...] | None
    assumptions: tuple[str, ...]
    calibration: Calibration
    added_calls: int
    keepalive_pings: int
    lanes_skipped: tuple[tuple[str, str], ...]   # lane_key → reason (e.g. "keepalive not allowed for claude_code")
    n_lanes: int = 0
    n_requests: int = 0
# Precondition: every replayed lane has the same billing class (billed | allowance); mixed input raises
# UsageError. core.shards.merge_replay adds results of disjoint lane sets.

@dataclass(frozen=True, slots=True)
class CalibrationPartial:
    """Mergeable sums for the two-pass streaming model gate (§9.6); merge = field-wise addition."""
    granularity: str
    period_billed: tuple[tuple[str, int], ...]                 # period → billed point nano
    period_documented: tuple[tuple[str, int], ...]             # period → predicted nano (documented)
    period_calibrated: tuple[tuple[str, int], ...]             # period → out-of-fold calibrated nano (pass 2)
    rho_counts: tuple[tuple[int, str, int, int], ...]          # (fold, gap band, hits, trials)
    confusion: tuple[tuple[str, str, int], ...]                # (predicted cause, canonical server reason, n)
    ttl_corroboration: tuple[int, int]                         # (ttl-expiry predictions labeled
                                                               #  previous_message_not_found, all labeled ttl-expiry)
    unlabeled: int
    no_comparison_labels: int

@dataclass(frozen=True, slots=True)
class CalibrationReport:
    granularity: str             # "day" | "month"
    n_periods: int
    status: str                  # "pass" | "fail" | "insufficient_data"
    mode_used: str | None        # "documented" | "calibrated" | None
    nmbe_pct: str | None         # decimal strings, documented mode
    cvrmse_pct: str | None
    nmbe_pct_calibrated: str | None
    cvrmse_pct_calibrated: str | None
    thresholds: tuple[str, str]  # ("5","15") monthly or ("10","30") daily
    rho: tuple[tuple[str, int, int, str, str], ...]   # (gap band, hits, trials, wilson_low, wilson_high)
    diag_confusion: tuple[tuple[str, str, int], ...]  # (predicted cause, server reason, count)
    diag_precision_recall: tuple[tuple[str, str, str], ...]  # (class, precision, recall)
    unlabeled: int
    no_comparison_labels: int    # previous_message_not_found + unavailable
    ttl_corroboration: tuple[int, int]
    notes: tuple[str, ...]
    def calibration(self) -> Calibration: ...   # CALIBRATED iff status == "pass"

# ---------- shards (D30) ----------
@dataclass(frozen=True, slots=True)
class ShardKey:
    team: str | None             # None = unattributed requests
    lane_kind: str | None        # None = every lane kind of the team (team below the shard cap)

@dataclass(frozen=True, slots=True)
class LaneIndexRow:
    lane_key: str; team: str | None; lane_kind: str; billing_class: str
    requests: int; point_nano: int   # for shard planning and stratified sampling

# ---------- findings ----------
@dataclass(frozen=True, slots=True)
class EvidenceItem:
    kind: str                    # "transition"|"event"|"block_divergence"|"attempt_chain"|"aggregate"
    ref: str                     # request/lane/event id (pseudonymous)
    attrs: tuple[tuple[str, str | int], ...]

@dataclass(frozen=True, slots=True)
class Fix:
    text: str
    config_patch: tuple[tuple[str, str], ...] | None   # flattened key path → JSON value string; keys must be
                                                       # in core.catalog.ALLOWLIST (verified or not)
    target: str | None           # "claude-code-managed-settings"|"litellm"|"code"|"gateway"|"ci"|"sdk"
    doc_url: str | None
    gates: tuple[str, ...] = ()  # applicability, e.g. "claude-code>=2.1.267"

@dataclass(frozen=True, slots=True)
class Scope:
    dims: tuple[tuple[str, str], ...]   # sorted; team/repo/lane_kind/model/agent_type/workspace/cohort/
                                        # billing_class; principal/session only for audience "self"/break-glass

@dataclass(frozen=True, slots=True)
class Finding:
    finding_id: str              # core.findings.finding_id(detector_id, kind, scope) — shard-independent
    detector_id: str             # class id, e.g. "cache.miss-by-cause"
    kind: str                    # emitted kind, e.g. "ttl-expiry" (§10 tables)
    detector_version: str
    category: str                # "breaker"|"lever"|"premium"|"failure"|"attribution"|"data-quality"|"aggregate"
    lever_class: str             # "rate"|"cache_transform"|"trajectory"|"behavioral"|"hygiene"|"none"
    audience: str                # "org" | "self"
    title: str                   # ≤ 120 chars, generated, never content
    summary: str                 # ≤ 400 chars
    scope: Scope
    n_events: int; n_lanes: int; n_users: int
    first_seen_ms: int
    cost_observed: Figure        # what the pattern cost in the window (EXACT where it is billed arithmetic)
    recoverable: Figure | None   # standalone counterfactual (None = no mechanical repair / attribution only)
    recoverable_shapley: Figure | None = None
    projected_monthly: Figure | None = None
    lever_ids: tuple[str, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()   # ≤ 20, sorted by (−magnitude, ref)
    fix: Fix | None = None
    confidence: str = "medium"   # "high"|"medium"|"low"
    validated_against: str | None = None   # e.g. "cache_miss_reason previous_message_not_found: 128/128"
    needs_eval: bool = False
    references: tuple[str, ...] = ()       # research finding ids

@dataclass(frozen=True)
class AnalysisContext:
    pricer: "Pricer"
    rules: "CacheRulesProvider"
    replayer: "Replayer | None"
    calibration: CalibrationReport | None
    window: tuple[int, int]      # [start_ms, end_ms)
    capabilities: frozenset[str]
    thresholds: Mapping[str, str] = field(default_factory=dict)   # detector overrides (decimal strings),
                                                                  # e.g. {"min_usd": "0.10"}
    k_anonymity: int = 5
    self_principal: str | None = None
    break_glass: str | None = None        # reason; enables session+team naming for tail findings only
    now_ms: int = 0
    static_prefix_floor: Mapping[tuple[str, str], int] = field(default_factory=dict)  # (scope, model) → S
    aggregates: tuple[UsageAggregate, ...] = ()   # for aggregate-level detectors (org scan)
    cost_lines: tuple[CostLine, ...] = ()
    shard: ShardKey | None = None                 # informational; detectors must not depend on it

@dataclass(frozen=True, slots=True)
class PolicyEntry:
    key: str                     # e.g. "promptCacheTtl" or "env.CLAUDE_CODE_AUTO_COMPACT_WINDOW"
    value_json: str
    projection: Figure | None
    lever_id: str
    needs_eval: bool
    verified_key: bool           # False → emitted only as a comment (VERIFY key)
    min_version: str | None
    note: str

@dataclass(frozen=True, slots=True)
class PolicyPack:
    target: str                  # "claude-code" | "litellm" | "sdk"
    cohort: str                  # "all" or cohort id (team / MDM group)
    merge_patch_json: str        # RFC 7386 merge patch against --current (canonical JSON)
    rollback_patch_json: str     # merge patch restoring the previous values (null for absent keys)
    entries: tuple[PolicyEntry, ...]
    otel_resource_attributes: str   # "tokenbill.arm=<lever>,tokenbill.wave=<n>"
    readme_md: str
    hooks: tuple[tuple[str, str], ...]   # (relative path, file text) e.g. SessionStart hook

@dataclass(frozen=True, slots=True)
class LeverResult:
    lever_id: str; lever_class: str; params: str           # canonical Policy spec fragment
    basis: Basis                                          # billed basis (LIST/CONTRACT) or LIST_EQUIVALENT
    standalone: Figure                                    # never summed across levers
    shapley: Figure
    projected_monthly: Figure                             # shapley × RR interval, monthly
    needs_eval: bool; upper_bound: bool; group: str
    finding_ids: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class ActionPlan:
    joint_saving: Figure                  # full-scope joint replay of the selected billed-basis set (window)
    headline_monthly: Figure              # Σ shapley_i × RR_i(p50) over billed-basis levers, range at p10/p90
    allowance_headroom_monthly: Figure | None   # same for LIST_EQUIVALENT cohorts (never added to headline)
    levers: tuple[LeverResult, ...]
    groups: tuple[tuple[str, tuple[str, ...]], ...]       # group id → lever ids
    method: str                           # "shapley-exact" | "shapley-mc"
    shapley_se: tuple[tuple[str, int], ...]               # lever → SE nano (mc only)
    sample: str                           # e.g. "shapley on 20000/1481203 lanes (seed 7), scaled to full-scope
                                          #  joint replay"
    observed_rr: tuple[tuple[str, str, int], ...] = ()    # lever class → (observed mean RR, n receipts) when ≥ 3

# ---------- reconciliation ----------
@dataclass(frozen=True, slots=True)
class ReconRow:
    key: tuple[tuple[str, str], ...]      # channel, date, workspace, model, token_type/bucket, service_tier
    ledger_tokens: int | None
    provider_tokens: int | None
    ledger_nano: int | None               # our rate card on OUR ledger (exact + estimated points)
    priced_provider_nano: int | None      # our rate card on PROVIDER usage
    invoice_nano: int | None              # provider cost report / CUR / billing export
    rate_card_error_pct: str | None       # (priced_provider − invoice)/invoice
    coverage_pct: str | None              # ledger / invoice
    status: str                           # "match"|"within_tolerance"|"over"|"under"|"explained"
                                          # |"unexplained"|"provisional"
    residual_code: str | None             # §12.3

@dataclass(frozen=True, slots=True)
class ChannelVerdict:
    channel: str
    verdict: str                          # "reconciled" | "not_reconciled" | "insufficient_data"
    invoice_sources: tuple[str, ...]      # e.g. ("anthropic.cost_report",) or ("aws.cur2",)
    mapping_verified: bool                # False when the cost-type/SKU map rows used are VERIFY rows

@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    window: tuple[str, str]
    tolerance_pct: str
    unexplained_tolerance_pct: str
    rows: tuple[ReconRow, ...]
    token_coverage_pct: str | None
    dollar_coverage_pct: str | None
    rate_card_error: tuple[str, str, str] | None   # p50, p95, max |%| over model-days
    over_count_rows: int
    effective_discount: tuple[tuple[str, str], ...]   # "channel:model:bucket" → 1 − invoice/list
    residuals: tuple[tuple[str, int], ...]             # residual code → nano
    unexplained_nano: int
    channels: tuple[ChannelVerdict, ...]
    verdict: str                          # overall: "reconciled" iff every channel with ledger spend is
                                          # reconciled; "insufficient_data" if none has invoice data
    finality: Finality
    suggested_contract: ContractOverlay | None
    rerun_verdict: str | None             # verdict after applying the suggested contract

# ---------- verification ----------
@dataclass(frozen=True, slots=True)
class GuardResult:
    name: str; passed: bool; value: str; threshold: str

@dataclass(frozen=True, slots=True)
class PanelRow:
    cluster_id: str; date_utc: str
    cost_baseline_nano: int          # repriced at the pre-registered baseline rate card (R8)
    cost_actual_nano: int            # at the actual rate card (rate variance)
    active_dev_days: int
    arm: str | None; wave: str | None
    treated: bool
    outcome_prs: int | None = None   # team-level merged PRs (quality guardrail), None when absent

@dataclass(frozen=True, slots=True)
class MeasurePlan:
    lever_id: str
    design: str                      # "cluster_rct" | "stepped_wedge" | "its"
    cluster_kind: str
    waves: tuple[tuple[int, tuple[str, ...]], ...]   # wave → clusters (seeded order, never by spend)
    holdback: tuple[str, ...]
    washout_hours: int
    looks: tuple[str, ...]
    mde_nano: int | None
    projection: Figure | None
    verification_design: bool        # False when MDE > 0.8 × |projection| (or design "its": MEASURED ceiling)
    clusters_needed: int | None
    assignment_log_sha256: str
    preregistration_sha256: str
    preregistration_json: str
    otel_tags: tuple[tuple[str, str], ...]           # cluster → OTEL_RESOURCE_ATTRIBUTES value
    warnings: tuple[str, ...]        # e.g. "org-wide server-managed settings: randomize via MDM groups or the
                                     #  Claude apps gateway, else design its (MEASURED at best)"

@dataclass(frozen=True, slots=True)
class MeasurementResult:
    lever_id: str
    design: str                  # "ab" | "cluster_rct" | "stepped_wedge" | "its"
    unit: str                    # "cost per active developer-day" | "cost per task" | "cost per success"
    estimate: Figure             # MEASURED or VERIFIED, with CI range
    projected: Figure | None
    realization_rate: tuple[str, str, str] | None   # point, lo, hi (decimal strings)
    guards: tuple[GuardResult, ...]
    scope: tuple[tuple[str, int], ...]              # clusters, units, treated unit-days
    scope_label: str             # "fleet:<window>" or "lab:<task-set>"
    window: tuple[tuple[str, str], ...]
    rate_card_sha256: str
    assignment_log_sha256: str | None
    preregistration_sha256: str | None
    adjustments: tuple[str, ...]
    rate_variance: Figure | None # EXACT price effect, reported separately (R8)
    signable: bool               # False when ESTIMATED, unreconciled or projection uncalibrated

@dataclass(frozen=True, slots=True)
class AbResult:
    verdict: str                 # "cheaper" | "no-difference" | "costlier"
    scope_label: str             # "lab:<sha256 of task ids>[:12]"
    n_tasks: int; trials_per_arm: tuple[int, int]
    randomized_order: bool
    cost_per_success: tuple[Figure, Figure]     # baseline, candidate (MEASURED/VERIFIED with CI)
    paired_difference: Figure                   # candidate − baseline per task (CI)
    token_delta_pct: str; turn_delta_pct: str; read_delta_pct: str; success_delta_pct: str
    measurement: MeasurementResult

@dataclass(frozen=True, slots=True)
class ReceiptRow:
    receipt_id: str; lever_id: str; lever_class: str; label: str
    realization_rate: str | None; created_ms: int; json: str; dsse: str | None

# ---------- privacy / aggregates ----------
@dataclass(frozen=True, slots=True)
class AggRow:
    dims: tuple[tuple[str, str | None], ...]
    n_users: int; n_requests: int
    usage: UsageBuckets
    priced: PricedTotal

@dataclass(frozen=True, slots=True)
class RawAggregate:           # NOT renderable; must pass core.kanon.publish()
    group_by: tuple[str, ...]; rows: tuple[AggRow, ...]; window: tuple[int, int]

@dataclass(frozen=True, slots=True)
class PublishedAggregate:     # the only aggregate type renderers/exporters accept
    group_by: tuple[str, ...]; rows: tuple[AggRow, ...]; window: tuple[int, int]
    k: int; suppressed_rows: int; suppressed_users: int
    token: object              # construction guard: must be core.types._PUBLISH_TOKEN (else ContractViolation);
                               # only core.kanon.publish and core.testing.published_for_tests pass it

# ---------- run-level results (what pipeline returns and outputs/* render) ----------
@dataclass(frozen=True, slots=True)
class LedgerCostRow:
    """Per-bucket ledger cost at export grain; built by the store with exact per-bucket SUMs."""
    date_utc: str; provider: str; channel: str; model: str
    bucket: str                              # a PricedLine bucket name
    team: str | None; cost_center: str | None; project: str | None; workspace_id: str | None
    lane_kind: str; workload_class: str; agent_product: str | None; billing_path: str
    quantity: int                            # tokens (or requests for server tools)
    priced_nano: int                         # Σ exact lines (0 for pure-range buckets)
    estimated_low_nano: int; estimated_high_nano: int   # Σ range lines (0 when none)
    basis: Basis                             # LIST | CONTRACT | LIST_EQUIVALENT
    rate_row_id: str | None; n_users: int

@dataclass(frozen=True, slots=True)
class ClusterDay:
    date_utc: str; cluster_kind: str; cluster_id: str; arm: str | None; wave: str | None
    active_users: int; requests: int; exact_nano: int; allowance_nano: int

@dataclass(frozen=True, slots=True)
class PrivacyInfo:
    content_tier: ContentTier; key_id: str | None; identity_mode: str; k: int; suppressed_groups: int

@dataclass(frozen=True, slots=True)
class RateCardInfo:
    sha256: str; layers: tuple[str, ...]; stale_rows: tuple[str, ...]; contract: str | None; basis: Basis

@dataclass(frozen=True, slots=True)
class BillSummary:
    total: PricedTotal
    esr: str | None                          # Effective Token Savings Rate, exact ratio string (§14.1)
    breakdowns: tuple[tuple[str, PublishedAggregate], ...]   # key = comma-joined dimension list, any of the
                                             # store's whitelisted dims (e.g. "bucket", "team,model", "day")
    naive_ratio: str | None = None           # Claude Code naive line-sum ÷ de-duplicated (priced), §5.3
    footnotes: tuple[str, ...] = ()          # e.g. trace@1 5m-write footnote, placeholder-output range

@dataclass(frozen=True, slots=True)
class CheckViolation:
    rule_id: str       # "TB-CACHE-SHARE" | "TB-NEW-BREAKER" | "TB-COST-REGRESSION" | "TB-SERIALIZATION-CHURN"
    level: str         # "error" | "warning"
    message: str       # content-free
    location: str      # "<file name>#run=<run>#call=<index>" (no content)

@dataclass(frozen=True, slots=True)
class CheckResult:
    passed: bool
    violations: tuple[CheckViolation, ...]
    runs: int
    cache_read_share: str | None             # decimal string
    median_cost_nano: int | None
    baseline_median_cost_nano: int | None
    breaker_kinds: tuple[str, ...]
    summary_md: str

@dataclass(frozen=True)
class RunResult:
    command: str
    window: tuple[int, int]
    inputs: tuple[tuple[SourceInfo, int, int], ...]   # (source, records, quarantined)
    privacy: PrivacyInfo
    rate_card: RateCardInfo | None
    bill: BillSummary | None = None
    data_quality: tuple[DataQualityNote, ...] = ()
    reconciliation: ReconciliationReport | None = None
    calibration: CalibrationReport | None = None
    findings: tuple[Finding, ...] = ()
    action_plan: ActionPlan | None = None
    policy_packs: tuple[PolicyPack, ...] = ()
    replays: tuple[ReplayResult, ...] = ()           # `whatif`
    measure_plan: MeasurePlan | None = None
    measurements: tuple[MeasurementResult, ...] = ()
    ab: AbResult | None = None
    check: CheckResult | None = None
    pricing: PricingReport | None = None
    receipts: tuple[str, ...] = ()           # receipt ids
    synthetic: bool = False                  # demo-data banner
    notes: tuple[str, ...] = ()
```

### 3.6 `core/protocols.py`

```python
class Pricer(Protocol):
    rate_card_sha256: str
    basis: Basis                          # LIST or CONTRACT (inferences on billing_path "subscription" are
                                          # priced on basis LIST_EQUIVALENT regardless, D26)
    def resolve(self, ctx: PricingContext, *, ts_ms: int) -> ResolvedRates | None: ...
    def price_inference(self, inf: Inference, *, ts_ms: int) -> PricedInference: ...
    def price_usage(self, usage: UsageBuckets, ctx: PricingContext, *, ts_ms: int,
                    billable: bool | None = True, usage_source: UsageSource = UsageSource.FINAL,
                    output_upper: int | None = None) -> PricedInference: ...
    def unit_rates(self, ctx: PricingContext, *, ts_ms: int) -> UnitRates | None: ...
    def min_cacheable_tokens(self, ctx: PricingContext, *, ts_ms: int) -> int | None: ...
    def supports(self, ctx: PricingContext, feature: str, *, ts_ms: int) -> bool: ...
    def tokenizer_family(self, ctx: PricingContext, *, ts_ms: int) -> str | None: ...

class Adapter(Protocol):
    name: str                             # registry key, e.g. "claude-code"
    capabilities: frozenset[str]          # declared maximum capabilities (§5.1)
    def sniff(self, path: Path, head: bytes) -> bool: ...
    def read(self, path: Path, opts: IngestOptions) -> IngestResult: ...

class CacheRulesProvider(Protocol):
    def rules_for(self, provider: str, channel: str, model: str) -> "CacheRules": ...

class Replayer(Protocol):
    def replay(self, lanes: Sequence[Lane], policy: Policy, *, mode: str, pricer: Pricer,
               rules: CacheRulesProvider, calibration: CalibrationReport | None,
               static_prefix_floor: Mapping[tuple[str, str], int] | None = None,
               keep_outcomes: bool = False) -> ReplayResult: ...

class Detector(Protocol):
    id: str                               # class id (registry key)
    version: str
    kinds: tuple[str, ...]                # finding kinds it may emit
    requires: frozenset[str]              # capabilities needed
    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]: ...
    # Contract: cross-lane logic is confined to cohorts core.findings.cohort_key(lane) =
    # (team, lane_kind, billing_class) (plus finer keys inside a cohort), so running per shard and
    # concatenating equals running on all lanes (§3.21). Aggregate detectors read ctx.aggregates /
    # ctx.cost_lines and ignore lanes.

class LedgerStore(Protocol):
    def ingest(self, result: IngestResult, *, pricer: Pricer | None = None) -> dict[str, int]: ...
    def reprice(self, pricer: Pricer, *, since_ms: int | None = None, until_ms: int | None = None) -> int: ...
    def iter_lanes(self, *, since_ms: int | None = None, until_ms: int | None = None,
                   where: Mapping[str, str] | None = None,        # keys: team, lane_kind, billing_class,
                                                                  # workspace_id, agent_product, workload_class
                   lane_keys: Collection[str] | None = None) -> Iterator[Lane]: ...
    def iter_requests(self, *, since_ms: int | None = None, until_ms: int | None = None,
                      where: Mapping[str, str] | None = None) -> Iterator[Request]: ...
    def iter_usage_records(self, *, since_ms: int | None = None, until_ms: int | None = None) -> Iterator[UsageRecord]: ...
    def lane_index(self, *, since_ms: int, until_ms: int) -> Iterator[LaneIndexRow]: ...
    def lane_first_reads(self, *, since_ms: int, until_ms: int) -> Iterator[tuple[str, str, int]]: ...
        # (cache_scope_key, model, R of the lane's first request) — for core.transitions.static_prefix_floor
    def count_users(self, *, since_ms: int, until_ms: int, where: Mapping[str, str]) -> int: ...
        # COUNT(DISTINCT principal) over requests matching `where` (team, cost_center, lane_kind, model,
        # workspace_id, billing_class); used for exact k-anonymous re-scoping (§8.4). Never returns ids.
    def aggregates(self, source_kind: str | None = None, **window) -> list[UsageAggregate]: ...
    def cost_lines(self, source_kind: str | None = None, **window) -> list[CostLine]: ...
    def outcomes(self, **window) -> list[OutcomeAggregate]: ...
    def aggregate(self, *, since_ms: int, until_ms: int, group_by: Sequence[str],
                  where: Mapping[str, str] | None = None, pricer: Pricer | None = None) -> RawAggregate: ...
    def cluster_days(self, *, cluster_kind: str, since: str, until: str) -> list[ClusterDay]: ...
    def cost_rows(self, *, since_ms: int, until_ms: int, group_by: Sequence[str]) -> list[LedgerCostRow]: ...
        # group_by ⊆ {date, provider, channel, model, team, cost_center, project, workspace_id, lane_kind,
        #              workload_class, agent_product, billing_path}; one row per group × bucket × basis;
        #              principal never allowed
    def get_cursor(self, source_id: str, unit_hmac: str) -> tuple[int, str, int, int] | None: ...
        # (byte_offset, head_sha, size, mtime_ns)
    def set_cursor(self, source_id: str, unit_hmac: str, *, byte_offset: int, head_sha: str, size: int,
                   mtime_ns: int) -> None: ...
    def put_findings(self, run_id: str, findings: Sequence[Finding]) -> None: ...
    def findings(self, run_id: str | None = None) -> list[Finding]: ...     # latest run when None
    def put_receipt(self, row: ReceiptRow) -> None: ...
    def receipts(self, *, lever_class: str | None = None) -> list[ReceiptRow]: ...
    def purge(self, *, principal: str | None = None, before_ms: int | None = None, actor: str) -> int: ...
    def audit(self, actor: str, action: str, detail: Mapping[str, object]) -> None: ...
    def meta(self) -> dict[str, str]: ...
```

### 3.7 `core/registry.py` — string-map registration (no shared-file edits)

Registration is by dotted-path strings fixed in the foundation; each owning package implements the class
at exactly that path. Lazy import; a missing module (branch not merged yet) raises `ModuleNotFoundError`
only when that entry is requested.

```python
BUILTIN_ADAPTERS: dict[str, str] = {            # sniff order = this order
    "claude-code":           "tokenbill.adapters.claude_code:ClaudeCodeAdapter",
    "claude-code-headless":  "tokenbill.adapters.cc_headless:ClaudeCodeHeadlessAdapter",
    "trace@1":               "tokenbill.adapters.trace_v1:TraceV1Adapter",
    "trace@2":               "tokenbill.adapters.trace_v2:TraceV2Adapter",
    "otlp":                  "tokenbill.adapters.otel:OtlpJsonAdapter",
    "openai":                "tokenbill.adapters.openai:OpenAIUsageAdapter",
    "bedrock":               "tokenbill.adapters.bedrock:BedrockAdapter",
    "anthropic-responses":   "tokenbill.adapters.anthropic_responses:AnthropicResponsesAdapter",
    "anthropic-usage-report":   "tokenbill.adapters.anthropic_admin:UsageReportAdapter",
    "anthropic-cost-report":    "tokenbill.adapters.anthropic_admin:CostReportAdapter",
    "anthropic-cc-analytics":   "tokenbill.adapters.anthropic_admin:ClaudeCodeAnalyticsAdapter",
    "anthropic-enterprise-analytics": "tokenbill.adapters.anthropic_admin:EnterpriseAnalyticsAdapter",
    "openai-usage-buckets":     "tokenbill.adapters.openai_admin:OpenAIUsageBucketsAdapter",
    "openai-costs":             "tokenbill.adapters.openai_admin:OpenAICostsAdapter",
    "aws-cur":                  "tokenbill.adapters.cloud_billing:AwsCurAdapter",
    "gcp-billing":              "tokenbill.adapters.cloud_billing:GcpBillingExportAdapter",
}
BUILTIN_DETECTORS: dict[str, str] = {           # §10 lists kinds per class
    "cache.miss-by-cause":     "tokenbill.detect.cache_miss:MissByCause",
    "cache.switch-churn":      "tokenbill.detect.cache_miss:SwitchChurn",
    "cache.rebuild":           "tokenbill.detect.cache_miss:RebuildEvents",
    "cache.cold-resume":       "tokenbill.detect.cache_ttl:ColdResume",
    "cache.ttl-advisor":       "tokenbill.detect.cache_ttl:TtlAdvisor",
    "cache.gateway-disabled":  "tokenbill.detect.cache_structure:GatewayDisabled",
    "cache.unread-write":      "tokenbill.detect.cache_structure:UnreadWrite",
    "cache.cold-fanout":       "tokenbill.detect.cache_structure:ColdFanout",
    "context.size-tax":        "tokenbill.detect.context:SizeTax",
    "context.compaction-window": "tokenbill.detect.context:CompactionWindow",
    "context.static-prefix":   "tokenbill.detect.context:StaticPrefix",
    "attrib.carry":            "tokenbill.detect.context:Carry",
    "premium.modifiers":       "tokenbill.detect.premium:PremiumModifiers",
    "premium.sticky-escalation": "tokenbill.detect.premium:StickyEscalation",
    "model.routing":           "tokenbill.detect.model:Routing",
    "failure.path":            "tokenbill.detect.failure:FailurePath",
    "automation":              "tokenbill.detect.automation:Automation",
    "tail.runaway":            "tokenbill.detect.tail:Runaway",
    "aggregate.org-scan":      "tokenbill.recon.orgscan:OrgScan",
    "block.breakers":          "tokenbill.detect.block:BlockBreakers",
}
CONVENTION_MODULES: tuple[str, ...] = ("tokenbill.adapters.conventions_ext",)  # import registers conventions

def load(dotted: str) -> type                   # "module:Class" → class
def get_adapter(name: str) -> Adapter
def sniff_adapter(path: Path) -> Adapter | None   # reads ≤ 64 KiB head (decompressing .gz/.zst when possible);
                                                  # first adapter whose sniff() is True, in the fixed order above
def all_detectors() -> list[Detector]           # instantiated, sorted by id; skips unimportable with a DQ note
def run_detectors(lanes: Sequence[Lane], ctx: AnalysisContext, *, only: Sequence[str] | None = None,
                  emit_missing: bool = True) -> list[Finding]
    # For each detector: if not detector.requires <= ctx.capabilities → (when emit_missing) exactly ONE
    # Finding with category "data-quality", kind "missing-capabilities", no dollars (recoverable None,
    # cost_observed unpriced("unpriced: capabilities missing")), summary naming the missing capabilities;
    # else run it. The pipeline calls it per shard with emit_missing=False and once with an empty lane list
    # and emit_missing=True, so the DQ finding appears exactly once per run.
    # Output sorted by (-recoverable p50 or 0, detector_id, finding_id). Findings are returned unpublished;
    # the caller applies core.kanon.rescope_findings.
def load_plugins(enabled: bool) -> list[str]    # entry points "tokenbill.adapters" / "tokenbill.detectors";
                                                 # ONLY when enabled (CLI --plugins); returns names loaded
```

### 3.8 `core/ids.py`

```python
def stable_id(prefix: str, *parts: str | int) -> str     # prefix + "_" + sha256("\x1f".join(map(str, parts)))[:24]
def hmac_hex(key: bytes, data: bytes, n: int = 20) -> str  # HMAC-SHA256 hex[:n]
def pseudonym(key: bytes, prefix: str, value: str) -> str  # prefix + "_" + hmac_hex(key, value.encode())
def key_id(key: bytes) -> str                             # "k_" + sha256(b"tokenbill-key-id\0" + key)[:12]
def request_id_for(provider: str, provider_message_id: str | None, source_id: str, locator: str) -> str
    # "rq_…" = stable_id("rq", provider, message_id) when a message id exists (idempotent across
    # sources/machines); else stable_id("rq", source_id, locator)
def is_opaque_ref(value: str) -> bool     # [A-Za-z0-9._-]{1,64} and no "@" (central identity mode)
```

### 3.9 `core/jsonl.py`

```python
MAX_LINE_BYTES = 16 * 2**20
def head_sha(path: Path) -> str                           # sha256 of the first 4 KiB (rotation detection)
def open_text(path: Path) -> IO[bytes]                    # plain, .gz (gzip), .zst (only when the stdlib has
                                                          # compression.zstd, Python ≥ 3.14; else SourceError
                                                          # "zstd needs Python 3.14; set the collector
                                                          # fileexporter compression to none")
def iter_lines(path: Path, *, start_offset: int = 0) -> Iterator[tuple[int, int, bytes]]
    # (line_no, byte_offset_of_line_start, raw_line); compressed files require start_offset 0;
    # a line over MAX_LINE_BYTES is yielded as b"" (callers quarantine it as oversize_line)
def parse_json_line(raw: bytes) -> dict | None            # None for non-object / invalid JSON; rejects NaN/Infinity
def open_private(path: Path, mode: str = "w") -> IO       # creates with 0600 (POSIX); parent dirs 0700; on Windows
                                                          # applies an owner-only ACL with `icacls` when available
                                                          # (injectable runner) and returns a warning flag
                                                          # otherwise (dq.windows_acl_not_enforced, D44)
def write_jsonl(path: Path, records: Iterable[Mapping]) -> None   # canonical (sorted keys, no spaces), private
```

### 3.10 `core/textsafe.py`, `core/secrets.py`

`sanitize(text: str, limit: int | None = None) -> str` strips C0/C1 control characters and ANSI escapes
(keeps `\n`, `\t`), truncates with `…`. `find_secrets(text) -> list[tuple[str, int, int]]` detects
`anthropic_key` (`sk-ant-`), `openai_key` (`sk-` + 20+ chars), `aws_access_key` (`AKIA[0-9A-Z]{16}`),
`github_token` (`ghp_`, `github_pat_`), `slack_token` (`xox[abp]-`), `jwt`, `pem_private_key`,
`high_entropy` (≥ 32 chars, Shannon entropy ≥ 4.0 bits/char, base64/hex alphabet). `redact(text) ->
tuple[str, Counter[str]]` replaces with `[REDACTED:<type>]`. Every tier reports **counts by type only**.

### 3.11 `core/conventions.py` — convention registry and the Anthropic rules

```python
@dataclass(frozen=True)
class Convention:
    convention_id: str            # e.g. "anthropic.messages"
    provider: str
    inclusive_input: bool         # True: provider "input" includes cached/written tokens
    enabled: bool                 # False: normalize() raises PricingError("convention unverified")
    notes: str

def register_convention(conv: Convention,
                        fn: Callable[[Mapping[str, object]], tuple[UsageBuckets, list[str]]]) -> None
def get_convention(convention_id: str) -> Convention
def normalize(convention_id: str, raw_usage: Mapping[str, object]) -> tuple[UsageBuckets, list[str]]
    # returns (buckets, data-quality codes); imports CONVENTION_MODULES lazily on first unknown id
def sum_check(buckets: UsageBuckets, provider_total_input: int | None,
              provider_output: int | None) -> list[str]       # ["dq.sum_check_failed"] on mismatch

def anthropic_inferences(raw_usage: Mapping[str, object], *, message_model: str, ctx: PricingContext,
                         id_prefix: str, usage_source: UsageSource = UsageSource.FINAL,
                         advisor_model: str | None = None,
                         refusal_ambiguous_max_output: int = 16) -> tuple[list[Inference], list[str]]
```

F-SEM registers `anthropic.messages` (also used for Claude Platform on AWS, Foundry, Vertex
rawPredict, Bedrock InvokeModel, Claude Code transcripts and headless streams, Anthropic responses) and a
disabled `codex.rollout`. All other conventions are registered by `adapters/conventions_ext.py` (TELEM), §5.2.

**`anthropic.messages` normalization:** `uncached_input = input_tokens`; `cache_read =
cache_read_input_tokens`; `cache_write_5m = cache_creation.ephemeral_5m_input_tokens`; `cache_write_1h =
cache_creation.ephemeral_1h_input_tokens`; `cache_write_unknown = cache_creation_input_tokens − 5m − 1h`
when > 0 (note `dq.ttl_split_residual`); a negative residual → note `dq.ttl_split_exceeds_total` and the
split is trusted; `output = output_tokens`; `output_reasoning = output_tokens_details.thinking_tokens`;
`web_search_requests`, `web_fetch_requests` from `server_tool_use`. Missing fields are 0; non-integer or
negative values → quarantine reason `bad_usage`.

**Iterations rule** (in `anthropic_inferences`): if `usage.iterations` is a non-empty list, each element
becomes its own `Inference` (priced at `iterations[].model` or `message_model`) and top-level usage is **not**
priced. Check the invariant: len 1 ⇒ element == top-level; fallback pairs ⇒ last element == top-level;
other shapes ⇒ Σ elements == top-level; a violation emits `dq.iterations_mismatch` and the iterations are
kept. Kinds by `type`: `message` → MESSAGE; `compaction` → COMPACTION; `advisor_message` → ADVISOR (model =
element model, else `advisor_model`, else unpriced with reason "advisor model unknown");
`fallback_message` → FALLBACK. A `message` element immediately followed by a `fallback_message` element
is FALLBACK_DECLINED and priced by the **refusal rule**:

| declined output tokens | billable | billing_rule_id | evidence |
|---|---|---|---|
| 0 | False | `anthropic.refusal.pre_output` | EXACT ($0) |
| 1 … `refusal_ambiguous_max_output` (16) | None (range [0, full]) | `anthropic.refusal.ambiguous` | ESTIMATED |
| > 16 | True | `anthropic.refusal.mid_stream` | EXACT |

Evidence: `anth-iterations-undercount`, `compaction-iterations-p0`, `advisor-iterations-undercount`,
`fp-anth-refusal-iterations`, `cc-iterations-fallback` (corrected); corpus: len 1 sum == top in
56,554/56,554; fallback pairs last == top in 27/27.

When `iterations` is absent: one MESSAGE inference from top-level usage. Inference ids:
`stable_id("inf", id_prefix, index)`.

### 3.12 `core/models.py` — model-id normalization

```python
@dataclass(frozen=True)
class ModelId:
    model: str            # canonical id, "" when not priceable
    channel_hint: str | None   # "bedrock" | "vertex" | None
    endpoint_scope: str   # "global" | "regional" | "unknown" (Vertex ids carry no region; plain ids "unknown")
    reason: str | None    # why not priceable ("synthetic", "config alias")

def normalize_model(model_raw: str, provider_hint: str | None = None) -> ModelId
```

Rules, in order: strip whitespace; `<synthetic>` → not priceable ("synthetic"); config aliases `opus`,
`sonnet`, `haiku`, `fable`, `opusplan`, `default` → not priceable ("config alias"); strip a trailing
`[1m]` (Claude Code 1M-context marker; no price premium); Bedrock form
`[<geo>.]anthropic.<model>-v<N>[:<M>]` → model, channel_hint bedrock, scope `global` when geo is `global`,
else `regional` (in-region and `us.`/`eu.`/`apac.`/`jp.`/`au.` profiles; **VERIFY** per §19.8 #3); Vertex
`<model>@<YYYYMMDD|latest>` → model, channel_hint vertex, scope `unknown` (the region lives in the endpoint
URL or `CLOUD_ML_REGION`, supplied by `--attr endpoint_scope=` or `request_meta.endpoint_scope`); a trailing `-YYYYMMDD` snapshot suffix is removed
(e.g. `claude-haiku-4-5-20251001` → `claude-haiku-4-5`). The result is used as `PricingContext.model`;
`model_raw` keeps the original.

### 3.13 `core/lanes.py`

```python
def group_lanes(requests: Iterable[Request], events: Iterable[LaneEvent],
                sessions: Iterable[Session] = ()) -> list[Lane]
    # groups by lane_key; sorts requests by (ts_start_ms, seq) and events by ts_ms; lane kind, parent,
    # cache scope from the Session/Lane shells when present (adapters emit shells), else UNKNOWN/"unknown";
    # ttl_observed: "1h" if any billed cache_write_1h and no 5m, "5m" if 5m only, "mixed" if both,
    # else "unknown". Deterministic output order: by (session_key, lane_key).
def ttl_of_last_write(lane: Lane, before_index: int) -> int | None   # 3600 / 300 / other TTL s / None
```

### 3.14 `core/cache_rules.py` — cache-rule table (data with sources; F-SEM)

```python
@dataclass(frozen=True, slots=True)
class CacheRules:
    provider: str; channel: str
    ttl_options_s: tuple[int, ...]        # Anthropic (300, 3600); OpenAI 5.6+ (1800,)
    ttl_measured_from: str                # "request_start" (Anthropic) | "last_use" (OpenAI)
    refresh_on_read: bool                 # True
    visible_from: str                     # "first_token" (Anthropic) | "response_end" (OpenAI, conservative)
    lookback_positions: int | None        # 20 (Anthropic); None (OpenAI)
    collapse_tool_runs: bool              # True on Anthropic 1P
    max_breakpoints: int                  # 4
    scope: str                            # "workspace" (1P, Claude Platform on AWS, Foundry) |
                                          # "organization" (Bedrock, Vertex, OpenAI) | "subscription" (Azure)
    tier_params: tuple[tuple[str, tuple[str, ...]], ...]
        # tier → params salted into that tier's hash (the invalidation hierarchy):
        # ("tools", ("model","tool_defs")),
        # ("system", ("speed","web_search_enabled","citations_enabled")),
        # ("messages", ("tool_choice","disable_parallel_tool_use","has_images","thinking","effort","output_format"))
    effort_invalidates_all_tiers_models: tuple[str, ...]   # "on some models" thinking/effort also salt
                                                           # tools/system; empty until verified (§19.8 #14)
    sources: tuple[str, ...]

class RulesTable:  # implements CacheRulesProvider; rows for Anthropic channels, openai_api and azure_openai
    def rules_for(self, provider: str, channel: str, model: str) -> CacheRules: ...

EFFORT_KEEPS_CACHE_CLAUDE_CODE = ("claude-opus-5-5", "claude-fable-5-1")
PER_MESSAGE_EFFORT_BETA = "mid-conversation-output-config-2026-07-01"
PER_MESSAGE_EFFORT_MODELS = ("claude-fable-5-1", "claude-mythos-5-1", "claude-opus-5", "claude-opus-5-5")

def effort_change_keeps_cache(*, agent_product: str | None, model: str, channel: str,
                              client_version: str | None, betas: Sequence[str]) -> bool
    # D28. True iff
    #  (a) agent_product == "claude_code" and model in EFFORT_KEEPS_CACHE_CLAUDE_CODE and channel not in
    #      {"bedrock", "vertex"} and (client_version is None or client_version >= "2.1.260" by numeric
    #      dotted comparison)   — Claude Code sends per-turn effort that way (cc-cache-breakers, corrected); or
    #  (b) PER_MESSAGE_EFFORT_BETA in betas and model in PER_MESSAGE_EFFORT_MODELS and channel in
    #      {"anthropic_api", "claude_platform_aws", "foundry", "vertex"}.
    # Otherwise a top-level effort (or thinking) change invalidates the messages tier
    # (anth-invalidation-hierarchy, anth-effort-rerun-budgets). Used by core.transitions (usage level) and
    # by sim.block_replay (tier salts): both engines must agree.
```

Minimum cacheable tokens come from the rate row (`Pricer.min_cacheable_tokens`), never from this table.

### 3.15 `core/transitions.py` — the shared miss definition (F-SEM)

This is the single definition of a transition, a miss and its cause, used by detectors, replay,
calibration and the oracle.

For a lane with requests ordered by `(ts_start_ms, seq)`, skipping requests that have no serving
inference, for each request `i` use the **serving inference** usage (§3.2): `R_i = cache_read`, `W_i =
cache_write` (all write buckets), `U_i = uncached_input`, `O_i = output`, `T_i = R_i + W_i + U_i`, `P_i =
R_i + W_i` (the cached prefix after `i`).

```python
MISS_MIN_TOKENS = 2000            # Claude Code /usage rule: > 5% AND ≥ 2,000 tokens (never 2,048)
MISS_MIN_FRACTION = Decimal("0.05")
AMBIGUITY_MS = 10_000

def classify_transitions(lane: Lane, *, pricer: Pricer, rules: CacheRulesProvider,
                         static_prefix_floor: Mapping[tuple[str, str], int] | None = None
                         ) -> list[Transition]
def static_prefix_floor(first_reads: Iterable[tuple[str, str, int]], *, min_lanes: int = 5
                        ) -> dict[tuple[str, str], int]
    # input: (cache_scope_key, model, R of a lane's first request) — from LedgerStore.lane_first_reads or
    # lane_first_reads_of(lanes); output: (scope, model) → median R over entries with R > 0, only when
    # ≥ min_lanes such entries
def lane_first_reads_of(lanes: Iterable[Lane]) -> Iterator[tuple[str, str, int]]
```

For `i ≥ 1`: `gap_i = ts_start_i − ts_start_{i−1}`; `E_i = min(P_{i−1}, T_i)`; `M_i = max(0, E_i − R_i)`;
**miss event** iff `M_i > 0.05·E_i` and `M_i ≥ 2,000`. `τ_i` = TTL of the most recent billed write in the
lane before `i` (`ttl_of_last_write`); when unknown, the lane's `write_ttl_hint` from the serving
inference's pricing context, else `None`. `ambiguous = τ_i is not None and |gap_i − τ_i·1000| ≤ 10,000`.

**Cause precedence** (first match wins; slugs are contract):

1. `compaction` — a COMPACTION, CLEAR or CONTEXT_EDIT event with `ts ∈ (ts_{i−1}, ts_i]`, or
   `applied_edits` on attempt `i`, or `thinking_dropped > 0` on attempt `i` (expected rebuild; priced as
   waste only by `cache.rebuild`: cold compaction and edit churn, §10.2).
2. `model-switch` — serving model differs from `i−1`. `sub_cause`, first match: `refusal-fallback`
   (FALLBACK / FALLBACK_DECLINED inference on `i`, or MODEL_FALLBACK event with trigger refusal),
   `availability-fallback` (API_ERROR 529/overloaded in `(ts_{i−1}, ts_i]` and a MODEL_FALLBACK event with
   trigger availability), `plan-toggle` (Claude Code MAIN lane, a HUMAN_PROMPT event in `(ts_{i−1}, ts_i]`,
   and the lane alternates between an Opus-family and a Sonnet-family model at least twice — "likely
   opusplan"), `ping-pong` (model of `i` equals model of `i−2`, and `gap_i ≤ τ`), else `user`.
3. `ttl-expiry` — `τ_i` known and `gap_i > τ_i·1000` (ambiguous flag set when within ±10 s).
4. `param-change` — `sub_cause`, first match: `fast-toggle` (speed differs), `effort-change` (effort or
   thinking of `i` differs from `i−1` and `effort_change_keeps_cache(agent_product, model_i, channel,
   client_version, betas)` is False), `client-upgrade` (client_version differs), `directory-change` (cwd_key
   differs).
5. `diag-<reason>` — server label (canonical `CacheDiagnostic.reason`): `tools_changed` → `tools-changed`,
   `system_changed` → `system-changed`, `messages_changed` → `messages-changed`, `param_changed` →
   `param-change` (sub_cause `diag`), `key_changed` → `unexplained` (sub_cause `cache-key`), `compacted` →
   `compaction`, `model_changed` → `model-switch` (sub_cause `diag`).
6. `context-shrank` — `T_i < 0.9·T_{i−1}`.
7. `unexplained`.

**Documented prediction** (`predicted_hit`, used by the model gate; never reads `R_i`): `True` iff same
serving model, no reset event (rule 1), no param change that invalidates (rule 4 with the same
effort exemption), `τ_i` known, `gap_i ≤ τ_i·1000`, `E_i ≥ min_cacheable_tokens(model)`, and same
`cache_scope_key` as `i−1`; `False` otherwise; `None` when `τ_i` is unknown (excluded from ρ fitting).
Predicted reads `R̂_i = E_i` on hit, else `S = static_prefix_floor[(scope, model)]` (0 when absent).

### 3.16 `core/shapley.py` (F-SEM)

```python
def shapley_exact(players: Sequence[str], value: Callable[[frozenset[str]], int]) -> dict[str, int]
    # exact Fraction weights |S|!(k−|S|−1)!/k!; results are converted to int nano with the
    # largest-remainder rule so Σφ == value(all) exactly; ties broken by player id order.
    # Raises UsageError for k > 10.
def shapley_mc(players: Sequence[str], value: Callable[[frozenset[str]], int], *,
               permutations: int = 200, seed: int = 0) -> tuple[dict[str, int], dict[str, int]]
    # seeded permutation sampling; returns (values normalized to Σ == value(all), standard errors nano)
def scale_credits(credits: Mapping[str, int], target_total: int) -> dict[str, int]
    # proportional rescaling with the largest-remainder rule (Σ == target_total); used to scale sample
    # Shapley credits to the full-scope joint saving (§11.2)
```

Test: players A,B,C with v({})=0, v(A)=10, v(B)=20, v(C)=5, v(AB)=26, v(AC)=15, v(BC)=24, v(ABC)=30 (nano)
→ exact fractions 8, 17.5, 4.5 → integer credit A=8, B=18, C=4 (remainder to B by id order, Σ=30).

### 3.17 `core/evidence.py` — published constants (with sources; F-CORE)

A frozen table of named constants loaded from `core/facts.json` (§3.24), each `(value: Decimal | int | str,
source_url, finding_id, checked_on)`. These are published benchmarks and documented defaults, never fleet
predictions.

| Name | Value | Finding |
|---|---|---|
| `MISS_MIN_TOKENS` / `MISS_MIN_FRACTION` | 2000 / 0.05 | `cc-usage-likely-cause` |
| `CACHE_READ_SHARE_MEDIAN` / `_TOP_DECILE` / `_INVESTIGATE_BELOW` | 0.84 / 0.94 / 0.80 | `anth-cache-health-thresholds` |
| `TTL_RULE_GAP_SHARE_5_60MIN` | 1/20 | `anth-fleet-benchmarks` |
| `KEEPALIVE_INTERVAL_S` / `KEEPALIVE_MAX_IDLE_S` | 240 / 3600 | `anth-ttl-choice-keepalive`, `keepalive-economics` |
| `KEEPALIVE_BREAK_EVEN` | `κ(w/r − 1)` with κ = 240 s: ≈ 46 min at r = 0.1×, ≈ 96 min Opus 5.5, ≈ 196 min Fable 5.1 | `keepalive-economics` (corrected) |
| `CC_AUTOCOMPACT_DEFAULT_TOKENS` | 967000 | `cc-autocompact-window` |
| `COMPACTION_SUMMARY_TOKENS_DEFAULT` | 20283 | `cc-compaction-threshold-sim` |
| `THINKING_SHARE_PRIOR` | 0.505 of output | `cc-output-thinking-effort` |
| `TOKENIZER_BAND` | (1.00, 1.35) between tokenizer families | `anth-tokenizer-inflation` |
| `BATCH_CACHE_HIT_BAND` | (0.30, 0.98) | `anth-batch-stacking` |
| `CC_FLEET_USD_PER_ACTIVE_DAY` / `_P90_UNDER` | 13 / 30 | `cc-enterprise-baseline` |
| `REFUSAL_AMBIGUOUS_MAX_OUTPUT` | 16 | `fp-anth-refusal-iterations` (D10) |
| `CPT_DEFAULT_47PLUS_TOOL_OUTPUT` / `CPT_DEFAULT_LEGACY` | 2.5 / 3.3 bytes per token | `cc-chars-per-token-calibration` |
| `COLD_RESUME_MIN_CONTEXT` | 100000 | `cc-cold-resume` |
| `FEMP_MONTHLY` / `FEMP_HOURLY_DAILY` | (±5%, 15%) / (±10%, 30%) | `ashrae-calibration-gate` |
| `MIN_CALIBRATION_PERIODS` | 12 | D7 |
| `MAX_TOKENS_AGENTIC_RECOMMENDED` / `_XHIGH` | 64000 / 128000 | `max-tokens-truncation` (corrected), `anth-output-hygiene` |
| `TOOL_DEFS_DEFER_THRESHOLD_TOKENS` / `TOOL_SEARCH_REDUCTION_BAND` | 10000 / (0.50, 0.85) (vendor-reported ≥ 85%, band widened) | `anth-tool-search-defer`, `tool-schema-bloat` |
| `EDIT_PAYBACK_FORMULA` | `K* = S(α − β)/(Xβ)` | `history-rewrite-kstar` |
| `CODE_REVIEW_USD_PER_REVIEW` | 15–25 (Anthropic Code Review average) | `ci-review-unit-costs` |
| `STALE_PROMPT_OUTPUT_DELTA` | +0.20 per call triggers the rebaseline note | D34 (design threshold) |

### 3.18 `core/testing.py` — fakes and conformance (F-KIT)

- `FakePricer` (a `Pricer`): loads its rows and modifiers from `core/facts.json` (the same verified values
  RATES must match, D37): claude-opus-5-5, claude-opus-5, claude-opus-4-8, claude-fable-5, claude-fable-5-1,
  claude-mythos-5-1, claude-sonnet-5, claude-sonnet-4-6, claude-haiku-4-5 on `anthropic_api`; claude-opus-5 on
  `bedrock` (global and regional); gpt-5.6-sol on `openai_api` (both effective-dated rows); batch / US-geo /
  fast / Bedrock-regional modifiers; unknown endpoint scope → range; subscription billing path → basis
  LIST_EQUIVALENT; `with_contract(overlay) -> FakePricer` (multiplier and per-model overrides, basis
  CONTRACT). Implements every `Pricer` method exactly per §6.2–§6.4 (Decimal, one rounding per line, per-line
  exactness, placeholder-output range). It is the reference other packages test against until RATES lands;
  the RATES parity test guarantees equal prices for these rows.
- `MemoryStore` (a `LedgerStore` reference implementation, dict-backed): every protocol method including
  §7.3 merge rules, `lane_index`, `lane_first_reads`, `count_users`, cursors, findings, receipts,
  `aggregate` (raises `PrivacyError` on principal/session), `cost_rows`, `cluster_days`, purge with audit.
- `FakeReplayer(table: Mapping[tuple[str, str], int])` keyed by `(lane_key, policy.spec())` → saving nano;
  `replay(lanes, policy)` returns a `ReplayResult` whose `saving` is Σ over the given lanes (so subsets,
  cohorts and shards behave correctly) and whose `baseline` is priced with the given pricer.
  `FakeReplayer.from_function(fn: Callable[[Lane, Policy], int])` for generated cases.
- `published_for_tests(raw: RawAggregate, k: int = 5) -> PublishedAggregate`: drops rows with `n_users < k`
  (no complementary suppression) — test-only, for renderer tests that must not depend on `core.kanon`.
- Conformance suite (each implementing package calls them from its own tests):
  `assert_pricer_conforms(pricer)` (golden subset of §6.9 that the pricer has rows for; per-line exactness;
  `unit_rates` agrees with `price_usage` to the nano on 200 random usages),
  `assert_adapter_conforms(adapter, fixture_path, *, expect_capabilities, opts=None)` (runs read twice →
  identical results; no canary in `repr` or `to_json` of results; every token count in range; `sum_check`
  passes where totals exist; content tier NONE yields no string field > 64 bytes of source text; every `h_`
  value appears only with a matching `SourceInfo.name_key_id`),
  `assert_store_conforms(factory)` (merge rules, idempotence, order independence over 5 sources, every
  protocol method, principal group-by raises),
  `assert_replayer_conforms(replayer, pricer)` (observed policy: `cost == baseline` to the nano and ranges,
  every outcome `changed=False`; unaffected requests unchanged under a TTL policy on a mixed lane set;
  mixed billing classes raise `UsageError`),
  `assert_detector_conforms(detector, lanes, ctx)` (emits only declared kinds; every finding has references,
  a fix or an explicit "no mechanical fix" summary, labeled Figures, deterministic ids, audience rules; and
  **shard invariance**: running separately on each `(team, lane_kind)` group of the lanes — the finest
  split `core.shards.plan_shards` can produce; the helper groups lanes itself so F-KIT does not import
  F-SEM — and concatenating equals one run over all lanes).
- `smoke_pipeline_on_fakes()` exercised by `tests/v2/kit/test_smoke_fakes.py`: builders → MemoryStore →
  FakePricer → classify_transitions → a trivial registered test detector → FakeReplayer → shapley → a
  `RunResult` — proving the contracts compose before any wave-2 code exists.

### 3.19 `core/policy.py` — policy grammar and selectors (F-SEM)

```python
def to_spec(policy: Policy) -> str               # canonical grammar string (§9.5); Policy.spec() delegates
def parse_policy(spec: str) -> Policy            # exact inverse: parse_policy(to_spec(p)) == p; unknown clause
                                                 # or selector → UsageError
def combine(a: Policy, b: Policy) -> Policy      # union; conflicting scalar fields raise ContractViolation
def lane_matches(selector: str, lane: Lane) -> bool      # selector terms evaluated on the lane's first request
def request_matches(selector: str, request: Request, lane: Lane) -> bool
def selector_terms(selector: str) -> tuple[tuple[str, str], ...]
```

### 3.20 `core/catalog.py` — lever catalog, settings allowlist, lifecycle (F-KIT; data only)

```python
@dataclass(frozen=True, slots=True)
class LeverDef:
    lever_id: str; lever_class: str          # rate | cache_transform | trajectory | behavioral
    grid: tuple[str, ...]                    # Policy spec fragments, one per candidate value (§9.5 grammar)
    selector: str                            # lanes the lever touches (interaction groups; core.policy.lane_matches)
    replay: str                              # "usage" | "block" | "none"
    needs_eval: bool; upper_bound: bool; tradeoff: bool
    patch_keys: tuple[str, ...]              # ALLOWLIST keys or snippet ids
    finding_kinds: tuple[str, ...]           # detector kinds that link to it
LEVERS: tuple[LeverDef, ...]                 # the §11.1 table, verbatim
def lever(lever_id: str) -> LeverDef
def levers_for_kind(kind: str) -> tuple[LeverDef, ...]

@dataclass(frozen=True, slots=True)
class AllowedKey:
    key: str; target: str; domain: str; min_version: str | None
    verified: bool; lever_id: str | None; tradeoff: bool; source: str
ALLOWLIST: Mapping[str, AllowedKey]          # the §11.4 table; `verified` flags come from facts.json
def allowed(key: str) -> AllowedKey          # unknown key → UsageError

@dataclass(frozen=True, slots=True)
class Promotion:
    promotion_id: str; model: str; channel: str; start: str; not_before_end: str; source: str
SUCCESSORS: Mapping[str, str]                # same-tier, same-tokenizer successors ONLY (§6.7)
RETIREMENTS: Mapping[str, str]               # model → retirement floor date
PROMOTIONS: tuple[Promotion, ...]
ANNOUNCED_UNPRICED: tuple[str, ...]          # e.g. claude-sonnet-5-5, claude-haiku-5-5
def successor(model: str) -> str | None
def retiring_within(model: str, ts_ms: int, days: int) -> str | None
def promotion_for(model: str, channel: str, date: str) -> Promotion | None

@dataclass(frozen=True, slots=True)
class SkuRule:                               # CUR usage types / GCP SKUs → canonical dims (§5.13)
    source_kind: str; pattern: str; model: str | None; bucket: str; endpoint_scope: str | None
    service_tier: str | None; unit_tokens: int; verified: bool; source: str
SKU_RULES: tuple[SkuRule, ...]               # from facts.json; unverified rules ship verified=False
def map_sku(source_kind: str, sku: str) -> SkuRule | None    # first matching VERIFIED rule, else None
```

### 3.21 `core/shards.py` — fleet-scale streaming (F-SEM, D30)

```python
SHARD_MAX_REQUESTS = 250_000
def plan_shards(index: Iterable[LaneIndexRow], *, max_requests: int = SHARD_MAX_REQUESTS) -> list[ShardKey]
    # one shard per team; a team whose request count exceeds max_requests is split into one shard per
    # lane kind (never further: cohorts are (team, lane_kind, billing_class)); deterministic order
def shard_where(key: ShardKey) -> dict[str, str]            # filter for LedgerStore.iter_lanes(where=…)
def shard_of_lanes(lanes: Sequence[Lane], key: ShardKey) -> list[Lane]   # in-memory equivalent (tests, fakes)
def merge_replay(parts: Sequence[ReplayResult]) -> ReplayResult          # disjoint lane sets: figures add,
                                                                        # counts add, outcomes concatenate
def merge_findings(parts: Iterable[Sequence[Finding]]) -> list[Finding]  # concatenation; duplicate finding_id
                                                                        # across shards raises ContractViolation
def stratified_sample(index: Sequence[LaneIndexRow], *, n: int = 20_000, seed: int = 0) -> frozenset[str]
    # lane keys, stratified by (lane_kind, billing_class, spend decile); the whole index when len ≤ n
```

### 3.22 `core/findings.py` — shared detector helpers (F-SEM)

```python
def cohort_key(lane: Lane) -> tuple[str | None, str, str]      # (team, lane_kind, billing_class)
def finding_id(detector_id: str, kind: str, scope: Scope) -> str   # stable_id("fd", detector_id, kind, dims)
def make_scope(**dims: str | None) -> Scope                     # drops None, sorts
def build_finding(**fields) -> Finding                          # validates title ≤ 120, summary ≤ 400,
                                                                # references non-empty, figure bases
def miss_waste(t: Transition, request: Request) -> tuple[int, int]   # (mw, mu): mw = min(M, W_i),
                                                                     # mu = min(M − mw, U_i)
def min_usd_nano(ctx: AnalysisContext) -> int                   # thresholds["min_usd"] (default "1.00")
def threshold(ctx: AnalysisContext, key: str, default: str) -> Decimal
def fit_cpt(lanes: Iterable[Lane], family: str) -> tuple[Decimal, int]   # bytes-per-token fit (§10.2 carry)
def top_evidence(items: Iterable[EvidenceItem], n: int = 20) -> tuple[EvidenceItem, ...]
def sum_figures(figs: Iterable[Figure], basis: Basis) -> Figure  # add() fold; empty → zero(basis)
def rate_nano(pricer: Pricer, ctx: PricingContext, ts_ms: int, bucket: str, tokens: int) -> int
```

### 3.23 `core/keys.py`, `core/kanon.py` — keys and k-anonymity (F-KIT)

`keys.py`: `load_or_create(path: Path | None = None) -> bytes` (32 random bytes via `secrets.token_bytes`,
file 0600, dir 0700; default `~/.config/tokenbill/key` = install key), `load(path: Path) -> bytes` (org and
collection keys; refuses group/world-readable files on POSIX; on Windows records the ACL warning),
`key_id` re-exported from `core.ids`. `kanon.py`: `publish`, `rescope_findings`,
`require_self_or_aggregate` exactly as §8.4 (the only k-anonymity implementation; ADMIN, STORE and OUT
call it).

### 3.24 `core/facts.json` and `core/facts.py` — the single transcription of verified facts (F-CORE)

`facts.json` (package data, `"schema": "tokenbill/facts@1"`) holds the §19 subset the code depends on:
`rates` (rows in the `tokenbill/rates@1` row shape for the FakePricer models), `modifiers`,
`settings_keys` (every §11.4 key with `verified`, `min_version`, `source`, `verified_on`), `evidence`
(§3.17 constants), `lifecycle` (successors, retirements, promotions, announced models), `focus_columns`
(FOCUS 1.4 names with mandatory/nullable status), `headless_fields` (§19.4), `sku_rules` (CUR usage-type /
GCP SKU rules for `core.catalog.SKU_RULES`, each `verified: false` unless checked, §19.8 #17). Every entry carries `source`,
`finding`, `verified_on` and `verification` (`"primary"` when re-checked against the primary URL during
the wave-0 facts task, else `"research"`). `facts.py`: `load() -> Facts` (cached, typed accessors); no
other module parses the JSON. Changing a fact after wave 0 is a contract change.

### 3.25 `core/builders.py` — builders, canary, flat rates (F-CORE)

- `CANARY = "TB-CANARY-7f3a91"`; `plant_canary(obj)` helper for fixtures; `assert_no_canary(*blobs: bytes |
  str)`.
- Builders: `make_usage(**buckets)`, `make_ctx(model="claude-opus-5-5", **kw)`, `make_inference(...)`,
  `make_attempt(...)`, `make_request(lane_key, seq, ts_ms, usage, model=..., **kw)`, `make_lane(requests,
  kind=MAIN, events=(), scope="ws:test")`, `make_block(...)`, `lane_from_table(rows)` (rows of `(ts_s, R, W5,
  W1, U, O)`), `make_aggregate(...)`, `make_cost_line(...)`.
- `FlatRates` (a `Pricer`): $1 input / $5 output per MTok, read ×0.1, 5m ×1.25, 1h ×2, other ×1.25,
  min cacheable 1,024, exact `unit_rates` (scale 8: the smallest `s ≥ 6` making $0.10 and $1.25 per MTok
  integral), basis LIST, sha "flat"; subscription billing path → LIST_EQUIVALENT.

---

## 4. Interchange format `tokenbill/trace@2` (TRACE owns the codec)

JSONL (`.gz` allowed), one record per line, canonical JSON (sorted keys, no whitespace), files created
`0600`. Every line has `"schema": "tokenbill/trace@2"` and a `"rec"` discriminator. The first line must be
a `header`. Records reference each other by id; order after the header is free except that a `blocks`
record must precede any `request` that references its hashes.

### 4.1 Profiles

| profile | content tier | allowed records | use |
|---|---|---|---|
| `usage` | none | header, session, lane, request (no `fp`), event, aggregate, cost_line, outcome, dq | fleet collector output (`tokenbill collect`), `export --format trace2` default |
| `fingerprint` | fingerprint | + blocks, request.fp | recorder for API agents (block breakers), opt-in |
| `full` | full | + content | local only; `export` refuses it; never shipped |

### 4.2 Records (closed key sets)

```jsonc
{"rec":"header","trace_id":"t_…","profile":"usage","name_key_id":"k_…"|null,"principal_key_id":"k_…"|null,
 "fp_key_id":"k_…"|null,"identity_mode":"central"|"two-stage"|"install",
 "producer":{"name":"tokenbill","version":"0.2.0","adapter":"claude-code"},"created_ms":0,
 "attribution":{…Attribution defaults…},"schema":"tokenbill/trace@2"}
{"rec":"session","session_key":"…","source_kind":"claude-code","attribution":{…},"started_ms":…,"ended_ms":…}
{"rec":"lane","lane_key":"…","session_key":"…","kind":"main","parent_lane_key":null,
 "cache_scope_key":"ws:…","ttl_observed":"1h","lane_exact":true}
{"rec":"blocks","key_id":"k_…","blocks":[{BlockRef fields}]}                       // fingerprint/full only
{"rec":"request","request_id":"rq_…","session_key":"…","lane_key":"…","seq":12,
 "attribution":{…},"params":{…RequestParams…},"appended":[{AppendedItem}],
 "source":{…SourceRef…},
 "fp":{"parent":"rq_…"|null,"keep":123,"append":["<h>",…],"markers":[[i,"5m"],…],"tier_end":[a,b,c]}|null,
 "attempts":[{"attempt_id":…,"attempt_no":0,"ts_start_ms":…,"ttft_ms":null,"duration_ms":…,
   "outcome":"ok","http_status":null,"error_type":null,"retry_layer":null,"retry_after_ms":null,
   "should_retry":null,"provider_request_id":…,"provider_message_id":…,"model_served":…,
   "stop_reason":…,"diagnostics":{…}|null,"applied_edits":[["clear_tool_uses",1234]],
   "thinking_dropped":0,"sdk_retry_count":null,"convention_id":"anthropic.messages","raw_usage":{…}|null,
   "inferences":[{Inference fields incl. output_upper; usage as object; pricing as object}]}]}
{"rec":"event","lane_key":"…","ts_ms":…,"kind":"compaction","attrs":{…fixed schema §3.2…}}
{"rec":"aggregate",…UsageAggregate…}  {"rec":"cost_line",…CostLine…}  {"rec":"outcome",…OutcomeAggregate…}
{"rec":"dq","code":"dq.message_start_only","severity":"info","count":12,"detail":"…"}
{"rec":"content","h":"…","text":"…"}                                                 // full only
```

`inferences` are authoritative for pricing. `raw_usage` (numbers and allowlisted enum strings only:
`service_tier`, `speed`, `inference_geo`, `iterations[].type`, `iterations[].model`) lets a future
convention fix re-normalize old files (`tokenbill ingest --renormalize` → `IngestOptions.renormalize`: the
reader rebuilds `inferences` from `raw_usage` with the current convention and records
`dq.renormalized`). The header's `name_key_id` / `principal_key_id` become `SourceInfo.name_key_id` /
`principal_key_id` (D39); `fp_key_id` is the key of block hashes (fingerprint profile only).

### 4.3 Validation (reader, strict and lenient)

- Unknown `rec` values and unknown keys in any record are rejected (quarantined with reason
  `unknown_key:<name>` in lenient mode; `SourceError` in strict mode). There is no free-form `ext`.
- Any string value longer than 256 characters is rejected (content-smuggling defense), except `content.text`
  in the `full` profile.
- `principal` must match `^(p|c)_[0-9a-f]{20}$` or `^r_[A-Za-z0-9._-]{1,64}$` (`r_` only when the header says
  `identity_mode: central`; `c_` only with `two-stage` and a `principal_key_id`); any value containing `@`
  is rejected.
- `attribution.extra` keys must be in `EXTRA_KEYS` (§3.2).
- Hash-like fields (`repo`, `api_key_id`, `cwd_key`, HMAC'd names) must match `^h_[0-9a-f]{20}$`.
- `profile: usage` files may not contain `blocks`, `fp` or `content`.
- Numbers must be integers in `[0, 2**53]` (money in nano); floats are rejected.
- Lines longer than 16 MiB are quarantined.

### 4.4 Codec API (TRACE, `adapters/trace_v2.py`)

```python
def write_trace_v2(path: Path, *, header: Mapping[str, object], sessions: Iterable[Session] = (),
                   requests: Iterable[Request] = (), events: Iterable[LaneEvent] = (),
                   aggregates: Iterable[UsageAggregate] = (), cost_lines: Iterable[CostLine] = (),
                   outcomes: Iterable[OutcomeAggregate] = (), notes: Iterable[DataQualityNote] = (),
                   content: Mapping[str, str] | None = None) -> int   # returns records written
def iter_trace_v2(path: Path, *, lenient: bool = True) -> Iterator[object]   # yields records or QuarantineItem
class TraceV2Adapter:   # Adapter; name "trace@2"; capabilities from the header profile and content present
```

Round trip: `write → read → write` is byte-identical. Fingerprints are delta-encoded: a request's block
list = the first `keep` blocks of its `parent` request's list + `append`.

### 4.5 `tokenbill/trace@1` (unchanged)

The v0.1 format (docs/SPEC.md v0.1, §L) is read and written by the frozen `trace.py` exactly as before.
The v2 path reads it through `TraceV1Adapter` (§5.5).

---

## 5. Sources and adapters

### 5.1 Adapter framework

- An adapter implements `core.protocols.Adapter`, is registered in `core.registry.BUILTIN_ADAPTERS`, never
  prices, never persists content above `opts.content_tier`, and never raises on a malformed record in
  lenient mode (quarantine with a content-free reason: `bad_json`, `not_object`, `missing:<field>`,
  `bad_usage`, `oversize_line`, `unknown_key:<k>`, `bad_type:<field>`).
- **Capabilities** (strings; detectors declare `requires` from this set): `usage_sequence`, `timing`,
  `ttft`, `ttl_split`, `iterations`, `attempts`, `diagnostics`, `appended`, `events`, `human_prompts`,
  `lanes_exact`, `params`, `blocks`, `attribution.team`, `workload`, `aggregates`, `cost`, `outcomes`,
  `quota_state`.
- **Data-quality codes** (stable): `dq.naive_line_sum_ratio`, `dq.version_histogram`, `dq.unknown_fields`,
  `dq.unknown_entry_type`, `dq.retention_warning`, `dq.duplicate_uuid_lines`, `dq.synthetic_skipped`,
  `dq.split_usage_mismatch`, `dq.message_start_only`, `dq.hidden_compaction_estimated`,
  `dq.rollup_not_spend`, `dq.iterations_mismatch`, `dq.ttl_split_residual`, `dq.ttl_split_exceeds_total`,
  `dq.sum_check_failed`, `dq.convention_mismatch`, `dq.request_id_collision`, `dq.no_ttl_split`,
  `dq.raw_bodies_ignored`, `dq.lanes_inferred`, `dq.quarantined`, `dq.secrets_observed`,
  `dq.unpriced_model`, `dq.model_before_effective_date`, `dq.stale_rate`, `dq.unverified_rate_row`,
  `dq.promotion_expired`, `dq.ttl_1h_markers_in_trace1`, `dq.breakpoint_assumed_end`,
  `dq.missing-capabilities`, `dq.sdk_retries_invisible`, `dq.headless_output_residual`,
  `dq.headless_resumed_totals`, `dq.headless_zeroed_result`, `dq.outcomes_suppressed`,
  `dq.name_key_mismatch`, `dq.cross_source_usage_mismatch`, `dq.renormalized`, `dq.windows_acl_not_enforced`,
  `dq.zstd_unavailable`, `dq.recon_schema_unverified`, `dq.unmapped_sku`, `dq.scope_unknown`,
  `dq.subscription_allowance`.
- **Identity** (D5, D39): with `identity_mode="central"` and `principal_ref` given, requests carry
  `principal = "r_" + ref` (validated `is_opaque_ref`, else `UsageError`); `two-stage` → `principal =
  pseudonym(opts.principal_key, "c", ref)`; `install` → `pseudonym(opts.principal_key, "p", ref)`;
  `central-ingest` (central files carrying raw identities: OTel `user.*`, Bedrock `identity.arn`, CUR
  `line_item_iam_principal`, Analytics actors) → `pseudonym(opts.principal_key, "p", raw)` with the org key,
  raw value dropped immediately. Emails, account UUIDs, OS user names and paths never appear in any record.
  `SourceInfo.principal_key_id` names the key of every `p_`/`c_` value.
- **Names**: tool names from the built-in Claude Code tool list (Bash, Read, Edit, MultiEdit, Write, Glob,
  Grep, WebFetch, WebSearch, Task/Agent, TodoWrite, NotebookEdit, …) and attachment type names are emitted
  in clear; MCP server/tool, skill and plugin names, repos, cwd, API key ids (and workspace ids with
  `--hash-workspaces`) are emitted as `pseudonym(opts.name_key, "h", value)` unless listed in
  `opts.name_allowlist`. `SourceInfo.name_key_id` names the key. Because the name key is the fleet-wide
  collection key on laptops and on the central host, one MCP server has one `h_` value across every source.
- **Team mapping at ingest**: adapters that see raw actors (Analytics user rows, CUR principals, OTel users)
  map them to a team through `opts.team_map` before pseudonymizing, then drop the raw value.

### 5.2 Convention registry (normalizer contract)

Canonical buckets are disjoint and exclusive. Each convention has a golden fixture whose Σ buckets
reproduces the provider's billed total (`ent-usage-normalization`, `fw-telemetry-conventions`).

| convention_id (owner) | Provider semantics | Mapping into `UsageBuckets` | Sum-check |
|---|---|---|---|
| `anthropic.messages` (F-SEM) | `input_tokens` **excludes** cache; `cache_read_input_tokens`; `cache_creation_input_tokens` = Σ `cache_creation.ephemeral_{5m,1h}_input_tokens`; `usage.iterations[]` per-attempt billing record; `server_tool_use`; `output_tokens_details.thinking_tokens` | §3.11 | top-level vs iterations |

| `bedrock.converse` (TELEM) | `inputTokens` excludes cache; `cacheReadInputTokens`; `cacheWriteInputTokens`; `cacheDetails[{ttl, inputTokens}]` | uncached = inputTokens; read; writes by `cacheDetails.ttl` ("5m"/"1h"); remainder → unknown | Σ cacheDetails ≤ cacheWrite |
| `openai.responses` (TELEM) | `input_tokens` **includes** cached and cache-write; `input_tokens_details.{cached_tokens, cache_write_tokens}`; `output_tokens` includes `output_tokens_details.reasoning_tokens` | uncached = input − cached − write (≥ 0 else `dq.sum_check_failed` and quarantine); read = cached; `cache_write_other` = write with ttl 1800 s; output; reasoning subset | cached + write ≤ input |
| `openai.chat` (TELEM) | `prompt_tokens` includes `prompt_tokens_details.cached_tokens` (and `cache_write_tokens` on 5.6+); `completion_tokens` includes reasoning; `rejected_prediction_tokens` billed as output | as above; pre-5.6: write 0 (cached counts are multiples of 128: expected) | cached ≤ prompt |
| `otel.genai` (TELEM) | `gen_ai.usage.input_tokens` **includes** `gen_ai.usage.cache_read.input_tokens` and `gen_ai.usage.cache_write.input_tokens`; `gen_ai.usage.reasoning.output_tokens` subset | uncached = input − read − write; writes → unknown TTL | read + write ≤ input else treated as exclusive for that span with `dq.convention_mismatch` |
| `otel.genai.legacy` (TELEM) | same with `gen_ai.usage.cache_creation.input_tokens` (semconv 1.40–1.41) | same | same |
| `openinference` (TELEM) | `llm.token_count.prompt` includes `llm.token_count.prompt_details.{cache_read, cache_write}`; **LLM-kind spans only** (never AGENT/CHAIN roll-ups) | same as otel.genai | same |
| `claude_code.otel` (TELEM) | `claude_code.api_request`: exclusive `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens` (**no TTL split**); `cost_usd` = client-side list estimate | exclusive; writes → `cache_write_unknown`; `cost_usd` → `provider_reported_cost_nano` basis `provider_estimate` | n/a |
| `anthropic.usage_report` (ADMIN) | uncached input, cache read, `cache_creation` 5m/1h, output, `server_tool_use.web_search_requests` | aggregate buckets | per bucket |
| `anthropic.cc_analytics` (ADMIN) | `model_breakdown[].tokens.{input, output, cache_read, cache_creation}`, `estimated_cost` cents | exclusive (**VERIFY** with a fixture, §19.8 #7); writes → unknown | n/a |
| `openai.usage_buckets` (ADMIN) | disjoint `input_uncached_tokens`, `input_cached_tokens`, `input_cache_write_tokens`, `output_tokens` | direct | n/a |
| `aws.cur2` (ADMIN) | `line_item_usage_amount` per `line_item_usage_type` (token direction, cache read/write, TTL, geo, batch encoded in the usage type; units mix 1K and 1M tokens) | usage-type map (`recon/costmap.py`, VERIFY rows disabled) → one bucket; unmapped → `dq.unmapped_sku` | n/a |
| `gcp.billing_export` (ADMIN) | `usage.amount` per `sku.id`/`sku.description` (Claude token SKUs), `cost`, `credits[]`, `labels` | SKU map → bucket; unmapped → `dq.unmapped_sku` | n/a |
| `codex.rollout` (F-SEM, **disabled**) | convention unverified (`codex-usage-format`) | `normalize()` raises `PricingError("convention unverified")` | — |

A **CrewAI-style defect** (a span reporting 17 input tokens for a call whose other counts imply ~17,119)
must raise `dq.sum_check_failed` (TELEM golden fixture). Other accounting rules: thinking is billed as
output even when hidden; image tokens are inside input counts (informational `ceil(w/28)·ceil(h/28)`);
hidden tool-use system-prompt tokens are inside provider counts; `cache_missed_input_tokens` and OTel
`cost_usd` are never priced.

### 5.3 Claude Code transcript importer (CC, `adapters/claude_code.py`)

`ClaudeCodeAdapter` — name `claude-code`, capabilities `{usage_sequence, timing, ttl_split, iterations,
diagnostics, appended, events, human_prompts, lanes_exact, params, quota_state}`; content tier `none` only
(D40: `--content fingerprint` on a Claude Code source is a `UsageError`). Source of truth: the empirical
traps (`cc-import-dedup-message-id`, `cc-iterations-fallback`, `fp-cc-missing-final-usage`,
`cc-hidden-calls-rollups`, `cc-transcript-format`, `ent-local-transcripts`, `cc-miss-taxonomy-ground-truth`).
The transcript format is internal to Claude Code and changes between versions; the importer degrades by
quarantine and data-quality counters, never by crashing.

**Layout.** Files `~/.claude/projects/<slug>/<session>.jsonl` (MAIN lanes), `<slug>/<session>/subagents/
agent-*.jsonl` (+ `agent-*.meta.json`: `agentType`, `description`, `model`, `parentAgentId`, `spawnDepth`,
`toolUseId`, `worktreePath` — read only `agentType`, `model`, `parentAgentId`, `spawnDepth`, `toolUseId`),
workflow agents under `…/workflows/…` (meta: `agentType`, `spawnDepth`, `workflowPhase`). Skip every
`journal.jsonl`. Lane kind: path contains `/workflows/` → WORKFLOW_AGENT; `/subagents/` → SUBAGENT; else
MAIN. Never read `workflows/*.json` `totalTokens` or `toolUseResult.totalTokens` as spend
(`dq.rollup_not_spend` counts them).

**Algorithm (per file, streaming, memory bounded by open message ids of that file):**

1. Stream lines via `core.jsonl.iter_lines`; bad JSON → quarantine `(file, line)`. Drop lines whose
   `(file, uuid)` was already seen (`dq.duplicate_uuid_lines`).
2. Track the timestamp of the latest `type ∈ {user, attachment}` entry (the **trigger**).
3. `type == "assistant"`: group by `message.id` (one API response is written as ~2.4 lines; a message id
   never spans files). Skip `message.model == "<synthetic>"` (`dq.synthetic_skipped`). Keep the entry with
   the **maximum `usage.output_tokens`** (monotone across split entries; ties → later line). If any other
   usage field differs across entries, keep the max-output entry and add `dq.split_usage_mismatch`.
   `stop_reason` = last non-null across entries. Collect `tool_use` ids from `content[]`.
4. `ts_start_ms` = the trigger timestamp that precedes the **first** entry of the id (fallback: first entry
   timestamp); `duration_ms` = last entry timestamp − ts_start; `ttft_ms` = None.
5. Usage → inferences via `core.conventions.anthropic_inferences` (iterations and refusal rule, §3.11).
   `PricingContext`: provider anthropic; channel `anthropic_api` unless `opts.attribution.billing_path` says
   bedrock/vertex/foundry/claude_platform_aws; model via `normalize_model`; `speed`, `service_tier`,
   `inference_geo` (`"not_available"`/empty → None) from usage; `endpoint_scope` from
   `opts.attribution.extra["endpoint_scope"]` (set by `--attr endpoint_scope=global|regional`; Claude Code on
   Vertex reads its region from `CLOUD_ML_REGION`, which transcripts do not record) else `unknown`; `write_ttl_hint` None (transcripts carry the split);
   `billing_path` per step 8. `RequestParams.effort` = the entry's `perTurnEffort` when present, else
   `effort`; `session_effort` = `effort`. Attempt: `provider_message_id = message.id`,
   `provider_request_id = requestId`, `model_served = message.model`, `stop_reason`, `diagnostics` from
   `message.diagnostics.cache_miss_reason.{type, cache_missed_input_tokens}` (canonical reason = type;
   `provider_reason` = type), `applied_edits` from `message.context_management.applied_edits[]` (`type`,
   `cleared_input_tokens`), `thinking_dropped` = count of `message.input_transformations[].type ==
   "thinking_dropped"`, `raw_usage_json`, `convention_id = "anthropic.messages"`. An entry with
   `isApiErrorMessage` true → attempt outcome `http_error` with `http_status = apiErrorStatus`.
6. **MESSAGE_START_ONLY**: `stop_reason` null for every entry of the id **and** max `output_tokens ≤ 20`
   **and** the next non-assistant entry in the file is a `user` entry whose `tool_result` answers one of this
   id's `tool_use` ids → `usage_source = MESSAGE_START_ONLY` (not an abort). The logged output is a lower
   bound; the adapter sets `Inference.output_upper = max(logged, median output of complete same-(model,
   lane kind) tool_use calls in this file, ceil(visible content bytes / CPT_DEFAULT_*))` (tokens only). The
   pricer prices the input lines EXACT and the output line ESTIMATED `[logged, output_upper]` (D27). Emit
   `dq.message_start_only` with `count` and `tokens = Σ(output_upper − logged)`. (96.4% of no-stop calls in
   the corpus were this pattern; 1.0–2.1% of spend.)
7. **Request id**: `request_id_for("anthropic", message.id, …)`. `requestId` is kept only as
   `provider_request_id`; if one `requestId` maps to two message ids within the run → `dq.request_id_collision`
   and neither is used as a join key.
8. **Events and billing path**: `system.subtype == "compact_boundary"` → COMPACTION
   (`compactMetadata.{trigger, preTokens, postTokens, durationMs, cumulativeDroppedTokens}`) **plus** a
   synthetic request on a COMPACTION lane (`lane_key = <main lane>#compaction`) with one
   `Inference(kind=COMPACTION, usage_source=ESTIMATED, billable=True)`: input `preTokens` as `cache_read` if
   the previous request of the main lane started ≤ τ earlier else as a write at the lane's TTL, output
   `postTokens`; it is excluded from the exact bill (usage_source ESTIMATED ⇒ every line a range) and
   reported as `dq.hidden_compaction_estimated` (compaction calls never appear as assistant entries; 0.2–2.4%
   of the corpus bill). `system.subtype == "model_refusal_fallback"` → MODEL_FALLBACK (`originalModel`,
   `fallbackModel`, trigger refusal); assistant `content[].type == "fallback"` also marks it.
   `system.subtype == "api_error"` → API_ERROR (`error.status`, `retryAttempt`, `maxRetries`, `retryInMs`;
   `error_type` from status: 429 rate_limit, 529/overloaded overloaded, `error.connection` → connection;
   message text dropped). `attachment` entries → CONTEXT_INJECTION (`att_type` = attachment type,
   `n_bytes` = length of the JSON of `content`/`addedLines`/`addedBlocks`/`addedNames`/`entries` values). A
   human prompt (`origin.kind == "human"`, or no origin and not `isMeta`, not `isCompactSummary`, no
   tool_result) on a MAIN lane → HUMAN_PROMPT. `cost-state` entries → COST_STATE (coverage cross-check
   only). A change of `version` between consecutive requests → UPGRADE. An assistant entry carrying
   `quotaLimits` → QUOTA_STATE (`status`, `rateLimitType`, `isUsingOverage`, `overageStatus`, `resetsAt`;
   **VERIFY** semantics, §19.8 #15). **Billing path**: `opts.attribution.billing_path` (MDM/`--attr`); when it
   is `subscription`, each request inherits `usage_credits` while the most recent QUOTA_STATE of the session
   has `using_overage = true`, else `subscription` (list-equivalent, D26; `dq.subscription_allowance` counts
   the requests); when it is unset, `unknown` (priced on the rate card's basis).
9. **Appended items** per request: for each `tool_result` block in the user entries since the previous
   request of the lane: `AppendedItem("tool_result", name=<tool name via tool_use id map>, n_bytes=<UTF-8
   length of text content>, is_error, images=<count of image sub-blocks>)`; attachments `("attachment",
   att_type, n_bytes)`; human text `("user_text", None, n_bytes)`. Only lengths leave the adapter.
10. **Attribution** per request: `attributionAgent` → agent_type (for lanes), `attributionSkill` /
    `attributionMcpServer` / `attributionPlugin` → skill / mcp_server / plugin (clear if allowlisted else
    `h_…` with the name key), `entrypoint`, `version` → client_version, `cwd` → `cwd_key =
    pseudonym(name_key, "h", cwd)` (never the path), `gitBranch` **dropped**, `agent_product =
    "claude_code"`, `query_source` from lane kind, defaults (`team`, `cost_center`, `billing_path`,
    `workspace_id`, `arm`, `wave`, allowlisted `extra`) from `opts.attribution`. The Claude Code prefix scope
    is per machine and directory, so `cache_scope_key = "ws:" + (workspace_id or "unknown")` and
    fan-out/cross-run logic additionally compares `cwd_key`.
11. **Sessions and lanes**: `session_key = stable_id("ses", "claude-code", sessionId)`; `lane_key =
    stable_id("ln", sessionId, agentId or "main")`; subagent lanes link `parent_lane_key` to the main lane of
    the same session; SESSION_META event from meta.json.
12. **Self-checks** (data quality): `IngestResult.naive_usage` = Σ usage over every assistant line (before
    de-duplication) **per normalized model**; the pipeline prices it and reports `dq.naive_line_sum_ratio` =
    naive priced ÷ de-duplicated priced (2.33× on the corpus) in `BillSummary.naive_ratio`;
    `dq.version_histogram` (count per `version`); `dq.unknown_entry_type` counts; `dq.retention_warning`
    when the oldest file's mtime is older than `cleanupPeriodDays − 3` days (default 30 → 27).

**Incremental collection** (`adapters/cc_collect.py`):

```python
@dataclass
class FileCursor:
    path_hmac: str; offset: int; head_sha: str; size: int; mtime_ns: int
    last_trigger_ts_ms: int | None; recent_uuids: list[str]   # bounded, last 2,000
class CollectorState:                                        # JSON file, private (core.jsonl.open_private)
    @staticmethod
    def load(path: Path) -> "CollectorState": ...
    def save(self, path: Path) -> None: ...
def collect_incremental(root: Path, state: CollectorState, opts: IngestOptions, *,
                        now_ms: int) -> Iterator[IngestResult]   # one IngestResult per changed file
```

A file is re-read from `offset` when `size ≥ offset` and `head_sha` is unchanged; otherwise it is re-read
from the start (rotation/truncation). The offset is advanced only to the start of the earliest assistant
group that is not yet closed (closed = has a non-null stop_reason or is followed by a non-assistant entry),
so in-flight calls are re-read next time; re-emitting a message id with a larger output is safe because the
store keeps the max-output row (§7.3).

### 5.4 Fleet collector and identity modes

`tokenbill collect claude-code` (CLI-LEDGER wires CC + TRACE) runs on each laptop under MDM and writes the
trace@2 `usage` profile; `tokenbill collect claude-code-headless` does the same for CI/headless streams
(§5.12). Identity (D5, D39):

| mode | on the laptop / runner | in the file | at central ingest | key custody |
|---|---|---|---|---|
| `central` (default) | reads an opaque employee or device id from `--principal-ref env:VAR\|mdm-file:PATH\|none` (validated: `[A-Za-z0-9._-]{1,64}`, no `@`) | `principal = "r_<ref>"` | `p_ = HMAC(org_key, ref)`; raw ref dropped before any write | org key only on the central host |
| `two-stage` | HMAC with the collection key | `principal = "c_<hmac>"`, header `principal_key_id` | `p_ = HMAC(org_key, "c:" + hex)`; rotation of the collection key is recorded as a `key_events` row | collection key on laptops and central, org key central |

In both fleet modes the MDM-distributed **collection key** is the **name key** everywhere: it HMACs
MCP/skill/plugin names, repos and `cwd_key` on laptops, and the central host uses the same key as
`IngestOptions.name_key` when ingesting OTel, Bedrock, CUR and Analytics files, so the same name has one `h_`
value across sources (not personal data; comparable fleet-wide). The modes differ only in how the principal
travels. Shipped files are org-controlled data in transit (they carry `r_`/`c_` values); the store and every
output hold only `p_` pseudonyms keyed by the org key, which never leaves the central host.
Team comes from MDM (`--team`, `--attr`) or `OTEL_RESOURCE_ATTRIBUTES` (`team.id`, `cost_center`,
`tokenbill.arm`, `tokenbill.wave`), never inferred from content. `--content` is fixed to `none` for
`collect`. The collector writes one file per run (`<out>/tb-<host-hmac>-<created_ms>.jsonl.gz`), never
opens a socket, and prints `dq.retention_warning` when transcripts approach `cleanupPeriodDays`. On Windows
the output directory must be MDM-restricted (D44); the collector applies the best-effort ACL and records
`dq.windows_acl_not_enforced` when it cannot.

### 5.5 trace@1 adapter (TRACE, `adapters/trace_v1.py`)

`TraceV1Adapter` (name `trace@1`, capabilities `{usage_sequence, timing, params}` + `blocks` in the
fingerprint tier): reads line by line (`json.loads` + the frozen `trace._parse_call` per line inside
`try/except TraceError` → quarantine), applies the v0.1 monotonic-index rule per run. Each call → one Request
with one attempt and one MESSAGE inference: uncached = `input_tokens`, read = `cache_read_input_tokens`,
writes = `cache_creation_input_tokens` → `cache_write_5m` EXACT **unless** any `cache_control` in the call's
tools or messages has `ttl == "1h"` → `cache_write_unknown` with `write_ttl_hint="1h"` and
`dq.ttl_1h_markers_in_trace1` (D12). Channel `anthropic_api`, billing path `api_key` (assumptions noted in the
bill footnote). `session_key = stable_id("ses", source_id, run_id)` (two files reusing a run id never merge).

**Breakpoints** (`RequestParams.breakpoints`, the BLOCK dual-engine gate depends on this rule): marker
positions found in tools/messages (`cache_control` keys, with their TTL) become `Breakpoint`s at the
fingerprint block index of the marked block. When the trace's `cache_breakpoints` count is > 0 but fewer
positions are recoverable (the trace stores the system prompt as rendered text, and the v0.1 demo traces
carry only the count), the missing ones become **one** assumed breakpoint at the last message block
(`Breakpoint(last_index, "5m", assumed=True)`, `dq.breakpoint_assumed_end`); a count of 0 with no markers means
no breakpoints and `automatic_caching = False`. Fingerprints via `fingerprint_request` (fingerprint tier).

Lanes: if a run contains more than one model or the fingerprints show non-prefix-extending successors, lanes
are inferred with `infer_lanes` (§5.8) and marked `lane_exact=False` (`dq.lanes_inferred`); else one API_RUN
lane per run. Demo acceptance: ingesting the four demo scenarios yields an exact bill equal to v0.1
`as-billed` dollars to the nano (v0.1 floats compared after rounding to 1e-9).

### 5.6 trace@2 adapter — §4.

### 5.7 Recorder v2 (TRACE, `instrument.py`)

```python
class Recorder:
    def __init__(self, path: str | Path, run_id: str | None = None, *, format: str = "trace@1",
                 content: str = "none", key_file: str | Path | None = None,
                 diagnostics: bool = False, lane: str | None = None, queue_size: int = 10_000,
                 http_hooks: bool = True) -> None: ...
    def wrap(self, client: Any) -> Any: ...
    def close(self) -> None: ...        # flushes the background writer (trace@2)
    dropped: int                        # records dropped by the bounded queue
def recording(path, run_id=None, **kw) -> ContextManager[Recorder]
```

- `format="trace@1"` (default): **exactly the v0.1 behavior** (synchronous append per completed call,
  failed calls record nothing, `create(stream=True)` warns). Existing `tests/test_instrument.py` passes
  unchanged.
- `format="trace@2"`: wraps `messages.create`, `messages.stream`, `beta.messages.create`,
  `beta.messages.stream`, sync and async; snapshots and serializes the request **before** sending (SDK
  objects via `model_dump()` when present, never `repr`); fingerprints blocks with `cache_control` stripped
  (moving markers never look like edits), plus sorted and volatile-normalized hashes, keyed by
  `core.keys.load_or_create(key_file)`; records every prompt-affecting parameter (RequestParams, including
  `betas`, `thinking`, `effort`, `tool_choice`, `speed`, `defer_loading` per tool → `BlockRef.deferred`) and the
  full usage object (`raw_usage_json` + convention `anthropic.messages`, incl. `iterations`,
  `cache_creation`, `server_tool_use`, `service_tier`, `inference_geo`, `speed`); records **every attempt**:
  - **SDK-internal HTTP retries (D35):** when `http_hooks=True` and the wrapped client exposes an httpx-like
    HTTP client (`getattr(client, "_client", None)` with a mutable `event_hooks` mapping of `"request"` /
    `"response"` lists), the recorder appends request/response hooks (sync functions for a sync client, async
    functions for an async one). Each HTTP attempt of a wrapped call becomes an `Attempt` with `http_status`,
    `sdk_retry_count` (`x-stainless-retry-count` request header), `retry_after_ms` (`retry-after-ms`, else
    `retry-after` seconds × 1000), `should_retry` (`x-should-retry`), `retry_layer="sdk"`, timing from the
    hooks, and no usage for failed attempts. The hooks read only status and those headers, never bodies; the
    attribution between hooks and the wrapped call uses a `contextvars.ContextVar`. When hooks cannot be
    attached (unknown client shape), the header's first `dq` record is `dq.sdk_retries_invisible` and a
    call's SDK retries collapse into one attempt as before.
  - exceptions (outcome `http_error`/`network_error`/`timeout`, no usage) and aborted streams
    (`PARTIAL_STREAM`: message_start input usage + count of streamed output tokens).
  `diagnostics=True` injects the `cache-diagnosis-2026-04-07` beta header and
  `diagnostics.previous_message_id` per lane (opt-in because it changes the request; first-party only);
  writes through a bounded background queue (drops with a counter rather than blocking; never raises into the
  caller); content tier `none` by default, `fingerprint` recommended for API agents, `full` local only. Never
  imports `anthropic` or `httpx`.

### 5.8 Fingerprinting (TRACE, `adapters/fingerprint.py`)

```python
def fingerprint_request(*, tools: Sequence[Mapping], system: str | Sequence[Mapping] | None,
                        messages: Sequence[Mapping], key: bytes, tier: ContentTier,
                        tokenizer_family: str = "claude-4.7+"
                        ) -> tuple[ContentFingerprint, tuple[Breakpoint, ...], dict[str, str]]
def volatile_spans(text: str) -> list[tuple[str, int, int]]   # classes from frozen breakers.VOLATILE_PATTERNS
def normalize_volatile(text: str) -> str                      # spans → "<iso_datetime>" etc.
def lookback_positions(blocks: Sequence[BlockRef]) -> list[int]
def infer_lanes(requests: Sequence[Request]) -> dict[str, str]   # request_id → lane_key
```

Blocks in wire order: one block per tool definition; system as one `system_text` block (string) or one
block per system content block; each message content block (a string content is one `text` block).
For each block: remove every `cache_control` key recursively (recording `(block_index, ttl or "5m")` as a
Breakpoint); `wire = json.dumps(block, ensure_ascii=False, separators=(",", ":"))` **preserving key order
as sent**; `h = hmac_hex(key, tier_tag + wire, 32)`; `h_sorted = hmac_hex(key, canonical_json(block))`;
volatile classes computed on text fields **before** hashing; `h_norm` = HMAC of the wire bytes after
`normalize_volatile`; `n_bytes = len(wire)`; `est_tokens = ceil(n_bytes / cpt)` with bytes-per-token 2.5
(tool_result, 4.7+), 3.6 (text, 4.7+), 2.7 (tool_def, 4.7+), ×1.3 for `claude-legacy`; images
`ceil(w/28)·ceil(h/28)` capped at 1,568 (standard) or 4,784 (4.7+ high-res) when dimensions are known;
`deferred = True` for a tool definition carrying `defer_loading: true`.
`lookback_pos`: consecutive `tool_use` blocks share one position; consecutive `tool_result` blocks share
one position. Content map (block hash → text) is returned only for tier FULL. `infer_lanes`: per run, a
request joins the lane whose last request has the longest common block-hash prefix with it (radix index
over `h`), requiring the same model and tools-tier hash; otherwise it opens a new lane.

### 5.9 OTLP/JSON adapter (TELEM, `adapters/otel.py`)

Reads OpenTelemetry Collector `file` exporter output (JSON lines of `ExportLogsServiceRequest`,
`ExportMetricsServiceRequest`, `ExportTraceServiceRequest`; plain, `.gz`, or `.zst` through
`core.jsonl.open_text` — zstd only on Python ≥ 3.14, else `SourceError` naming the fix `compression: none`
and `dq.zstd_unavailable`). OTLP/JSON encodes int64 (`intValue`, `timeUnixNano`) as **JSON strings**: parse
them as ints. Attributes arrive as `[{key, value:{stringValue|intValue|doubleValue|boolValue|arrayValue}}]`.
These files are central (`identity_mode="central-ingest"`): raw user identities are pseudonymized with
`opts.principal_key` (org key) and dropped; names with `opts.name_key` (collection key).

- `claude_code.api_request` log events → one Request, fidelity NO_TTL_SPLIT, attempt from `request_id`
  (provider_request_id), `ts_start_ms = timeUnixNano/10^6 − duration_ms` (the event is emitted when the
  response completes; **VERIFY** with a recorded fixture), `duration_ms`, `model`, `speed`, `effort`,
  `query_source` (→ lane kind main / subagent / auxiliary→HELPER), `cost_usd` → `provider_reported_cost_nano`
  basis `provider_estimate` (never billed), tokens via `claude_code.otel` (writes → `cache_write_unknown`,
  `dq.no_ttl_split`), and `write_ttl_hint` from the billing-path defaults of §19.5 when
  `opts.attribution.billing_path` is known (api_key / cloud / usage_credits → `5m` for every lane;
  subscription → `1h` for main lanes, `5m` otherwise), else None; `PricingContext.billing_path` from
  `opts.attribution`. `session.id` → session; `user.id` / `user.account_uuid` / `user.email` → team through
  `opts.team_map`, then principal `p_` via the org key (raw values dropped); `organization.id`; resource
  attributes `team.id`, `cost_center`, `department`, `tokenbill.arm`, `tokenbill.wave`, `app.entrypoint`,
  `app.version`, `vcs.*` (→ `h_` repo). Lanes: `session.id` + `query_source` (+ `agent_id` from beta
  `claude_code.llm_request` spans when present, which also supply `ttft_ms`); without spans all subagent
  calls of a session share one lane with `lane_exact=False`.
- `claude_code.api_error` → API_ERROR event and a failed attempt when joinable by `request_id`.
- `claude_code.tool_result` → AppendedItem (`tool_name`, `tool_result_size_bytes`, success) on the next
  request of the lane; beta `claude_code.tool` span `result_tokens` recorded in the item.
- Metrics `claude_code.token.usage` / `claude_code.cost.usage` → `UsageAggregate(source_kind="otel.metric")`
  for coverage only, never added to the ledger.
- `api_request_body` / `api_response_body` events (`OTEL_LOG_RAW_API_BODIES`) are **ignored** and counted
  in `dq.raw_bodies_ignored` (D23).
- GenAI spans: `gen_ai.operation.name ∈ {chat, generate_content, text_completion}`; provider from
  `gen_ai.provider.name` (legacy `gen_ai.system`); model from `gen_ai.response.model` or
  `gen_ai.request.model`; key `gen_ai.response.id`; `openai.response.service_tier`; `gen_ai.conversation.id`
  → session; convention `otel.genai` or `.legacy` by attribute names present.
- OpenInference: only spans with `openinference.span.kind == "LLM"`; `llm.model_name`, `llm.provider`,
  `llm.token_count.*`. Lane = (trace id, nearest AGENT-kind ancestor span); a leaf LLM span without an agent
  ancestor is its own lane.

### 5.10 Other usage adapters (TELEM)

- `adapters/openai.py` — JSONL of OpenAI Responses or Chat Completions response objects (or `{request_meta,
  response}` pairs from gateways): conventions `openai.responses` / `openai.chat`; `service_tier` is the
  **served** tier from the response; `provider_request_id` from `id`; `incomplete_details.reason ==
  "max_output_tokens"` → `stop_reason = "max_tokens"`; channel `openai_api`, or `azure_openai` when
  `request_meta.channel` says so (then `cache_scope_key = "sub:" + h_(subscription id)`, D43); long-context
  band flagged by the pricer, not the adapter. **`prompt_cache_diagnostics`** (Responses, GPT-5.6+, present
  when the caller set `prompt_cache_options.comparison_response_id`) → `Attempt.diagnostics` with
  `source = "openai.prompt_cache_diagnostics"`, `provider_reason` verbatim, `missed_input_tokens_estimate =
  cache_missed_tokens` (magnitude only, never priced), and the canonical reason: `model` → `model_changed`;
  `tools` → `tools_changed`; `input` → `messages_changed`; `reasoning_effort`, `text_format`, `verbosity`,
  `service_tier` → `param_changed`; `prompt_cache_key` → `key_changed`; `context_compacted` → `compacted`;
  `comparison_response_not_found` → `previous_message_not_found`; `unavailable` → `unavailable`
  (`oai-cache-diagnostics`; field names **VERIFY**, §19.8 #8).
- `adapters/bedrock.py` — Converse responses and model-invocation log records (`identity.arn` → team via
  `opts.team_map`, then `p_` via the org key; allowlisted `requestMetadata` keys → attribution; cache counts
  from the logged response body `output.outputBodyJson.usage`); model ids via `normalize_model` (scope from
  the id prefix); `billing_path = "bedrock"`.
- `adapters/anthropic_responses.py` — JSONL of Anthropic Messages API response objects or Message Batches
  results (`service_tier` batch); `{request_meta: {ts_ms, lane, session, attribution, channel,
  endpoint_scope, model_raw, billing_path}, response}` pairs supported (Vertex responses carry no model in the
  body: `model_raw` from `request_meta`, the endpoint URL's model id); convention `anthropic.messages`.

### 5.11 Admin and analytics page adapters (ADMIN, `adapters/anthropic_admin.py`, `adapters/openai_admin.py`)

Recorded JSON pages (one file per page or JSONL of pages). Field names per §19.4 (**VERIFY** against
recorded real pages, §19.8 #6–7). These are central files (`identity_mode="central-ingest"`).

- `UsageReportAdapter` (`/v1/organizations/usage_report/messages`) → `UsageAggregate` per bucket and group
  (workspace, api key → `h_` with the name key, model, service_tier, context_window, inference_geo, speed),
  buckets uncached / read / 5m / 1h / output / web_search; `dims` include `channel = "anthropic_api"`.
- `CostReportAdapter` (`/v1/organizations/cost_report`) → `CostLine` (amount cents decimal string →
  `cents_to_nano`, remainder kept for `cents_rounding`; `description`, `cost_type`, `token_type`, `model`,
  `inference_geo`; `workspace_id` null = default workspace; `channel = "anthropic_api"`).
- `ClaudeCodeAnalyticsAdapter` (`/v1/organizations/usage_report/claude_code`) → per user-day records are
  **aggregated at ingest** to `(date, team)` using `opts.team_map` (actor ref → team; unmapped actors →
  `team="(unmapped)"`): `UsageAggregate(source_kind="anthropic.cc_analytics", dims=(team, model))` for token
  coverage and `OutcomeAggregate` for commits / PRs / lines / accepted-rejected edits. Groups with fewer than
  `opts.k_anonymity` distinct users are merged into `team="(other)"` via `core.kanon`; if still < k they are
  dropped with a count in `dq.outcomes_suppressed`. **Nothing is stored per principal**; actor refs never
  leave the adapter (not even pseudonymized).
- `EnterpriseAnalyticsAdapter` (`/v1/organizations/analytics/{usage_report, cost_report, user_*}`) →
  aggregates and cost lines with `amount` and `list_amount` (cents decimal strings, parsed exactly, never
  float), `finality = provisional` for dates within the 30-day revision window relative to `opts.now_ms`;
  `user_*` endpoints are aggregated to team at ingest exactly like the CC Analytics adapter. Seat-based plans
  show only usage credits here (`cc-anthropic-discount-visibility`): allowance usage has no cost line.
- `OpenAIUsageBucketsAdapter`, `OpenAICostsAdapter` — organization usage (`input_uncached_tokens`,
  `input_cached_tokens`, `input_cache_write_tokens`, `output_tokens`; group by project, api key, model,
  batch, service_tier) and costs (project, line_item); `channel = "openai_api"`.

### 5.12 Claude Code headless, CI and Agent SDK streams (CC, `adapters/cc_headless.py`)

`ClaudeCodeHeadlessAdapter` — name `claude-code-headless`, capabilities `{usage_sequence, timing, ttl_split,
iterations, params, workload}`; content tier `none` only. Inputs (sniffed; field names **VERIFY**, §19.8
#16): the claude-code-action `execution_file` (`$RUNNER_TEMP/claude-execution-output.json`: a JSON array of
SDK messages, or JSONL), `claude -p --output-format stream-json` (JSONL of messages), `claude -p
--output-format json` (one result object), and Agent SDK message logs (same message shapes). Messages:
`{"type":"system","subtype":"init","session_id",…}`, `{"type":"assistant","message":{id, model, usage,
stop_reason, content[]},"parent_tool_use_id":str|null,"session_id"}`, `{"type":"user",…}`,
`{"type":"result","subtype","usage","modelUsage":{model:{inputTokens, outputTokens, cacheReadInputTokens,
cacheCreationInputTokens, …}},"total_cost_usd","num_turns","duration_ms","session_id","is_error"}`. Rules
(`ci-headless-ingest`, `sdk-accounting-pitfalls`):

1. De-duplicate assistant messages by `message.id`, keeping the max `output_tokens` (parallel tool calls
   repeat one id with identical usage) — the same rule as §5.3 step 3; skip `<synthetic>`.
2. Lanes: one MAIN lane per `session_id`; assistant messages with a `parent_tool_use_id` go to a SUBAGENT
   lane keyed by that id (parent = the main lane). Request start = the timestamp of the preceding
   user/tool_result message when present, else the message's own (streams without timestamps get
   `ts = file order × 1 ms` and `lane_exact = False`, capability `timing` dropped).
3. **Per-step output is a placeholder.** Step inferences keep their logged output (usage_source FINAL,
   priced exact on logged tokens). The `result` message then supplies the exact session output: for each
   model `m` in `modelUsage` (which includes subagents), `residual_m = modelUsage[m].outputTokens − Σ logged
   step outputs on m`; each `residual_m > 0` becomes an `Inference(kind=OUTPUT_RESIDUAL, output=residual_m)`
   at model `m` on a synthetic request `<main lane>#output-residual` at the result timestamp (no serving
   inference, so it never enters transitions; priced EXACT: billed tokens × rate). Without `modelUsage`, the
   main lane's residual is `result.usage.output_tokens − Σ main-lane logged outputs` (subagents excluded, as
   documented). Negative residuals are dropped with `dq.headless_output_residual` (severity warn).
4. Resumed sessions carry earlier spend in the result: when the result's input totals exceed the Σ of the
   stream's step inputs by > 1% the residual is skipped (`dq.headless_resumed_totals`); a zeroed result
   with non-zero steps is skipped (`dq.headless_zeroed_result`).
5. `total_cost_usd` → COST_STATE event (provider estimate; never billed). `--output-format json` files
   without steps yield only a `UsageAggregate(source_kind="claude_code.headless_result")` per model for
   coverage.
6. Attribution: `agent_product = "claude_code"`, `entrypoint` from `opts.attribution` (the collector sets
   `claude-code-github-action` when `GITHUB_ACTIONS=true`), `workload_class = CI` when `GITHUB_ACTIONS` or
   `--attr workload=ci`; `repo = h_(GITHUB_REPOSITORY)`, `extra.workflow = h_(GITHUB_WORKFLOW)`,
   `extra.run_attempt = GITHUB_RUN_ATTEMPT` — read by the CLI collector from the environment, never from the
   file. Identity: the CI bot identity is a principal like any other (`--principal-ref env:GITHUB_ACTOR` is
   discouraged; default `none`, so CI traffic is attributed to repo/team only).

### 5.13 Cloud billing exports (ADMIN, `adapters/cloud_billing.py`, D31)

- `AwsCurAdapter` (`aws-cur`) — AWS CUR 2.0 **CSV or CSV.gz** (Parquet is not readable with the stdlib;
  `docs/FLEET.md` documents the CSV export and an Athena query that produces it). Rows with
  `line_item_product_code ∈ {"AmazonBedrock", "AmazonBedrockFoundationModels"}` (or containing
  `anthropic` in `line_item_usage_type`) only. Per row: `CostLine(source_kind="aws.cur2", channel="bedrock",
  date_utc = line_item_usage_start_date[:10], workspace_id = h_(line_item_usage_account_id), sku =
  line_item_usage_type, model/bucket/endpoint_scope/service_tier from the usage-type map, amount =
  usd_str_to_nano(line_item_net_unblended_cost or line_item_unblended_cost), list_amount =
  line_item_unblended_cost, principal = p_(line_item_iam_principal) via the org key after team mapping)`
  aggregated to (date, account, usage type, principal team); and a token `UsageAggregate` from
  `line_item_usage_amount` (unit from `pricing_unit`: 1K or 1M tokens → tokens). Log-derived Bedrock cost
  "does not reflect discounts, commitments or provisioned throughput" — CUR is the invoice side
  (`cc-reconciliation-matrix`, `bedrock-attribution`, `ent-cloud-gateway-attribution`).
- `GcpBillingExportAdapter` (`gcp-billing`) — rows exported from the BigQuery billing export as CSV or
  JSONL (`service.description`, `sku.id`, `sku.description`, `usage_start_time`, `usage.amount`,
  `usage.unit`, `cost`, `credits[].amount`, `project.id`, `labels[]`, `location.region`): Claude SKUs only
  (`sku.description` contains "Claude" or the SKU map matches). `CostLine(source_kind="gcp.billing_export",
  channel="vertex", workspace_id = h_(project.id), amount = cost + Σ credits, list_amount = cost,
  endpoint_scope from `location.region` ("global" → global, else regional))` and token aggregates from
  `usage.amount`. Allowlisted labels (e.g. `team`) → attribution; other labels dropped.
- The usage-type / SKU → (model, bucket, scope, tier) map lives in `core.catalog.SKU_RULES` (F-KIT, data
  only, D29) so ADMIN (parse time) and RECON (reconcile time) use the same rules: regex rows `{source_kind,
  pattern, model, bucket, endpoint_scope, service_tier, unit_tokens, verified, source}`; rows marked
  `verified: false` (every row until checked against the Price List offer file / Cloud Billing catalog,
  §19.8 #17) are disabled — `map_sku` returns None for them, the adapter keeps `sku` and leaves `model` /
  `token_type` empty (tokens go to a `UsageAggregate` with dims `sku` and bucket `uncached_input` flagged by
  `dq.unmapped_sku`, used only for channel-total token coverage), and reconciliation falls back to the
  channel-total mode of §12.1. Never guesses.

---

## 6. Pricing registry (RATES, `tokenbill/rates/`)

### 6.1 Data files

JSON package data loaded with `importlib.resources`, numbers as **strings**: `rates/data/anthropic.json`
(channels anthropic_api, claude_platform_aws, foundry), `bedrock.json`, `vertex.json`, `openai.json`
(openai_api), `billing_rules.json`, and the offline verification snapshot
`rates/data/snapshots/pricing-2026-09-23.json` (shipped in the wheel so `pricing verify` works installed).
Lifecycle data (successors, retirements, promotions) lives in `core.catalog` (D29), not here. Schema
`tokenbill/rates@1`:

```json
{
  "schema": "tokenbill/rates@1",
  "provider": "anthropic",
  "as_of": "2026-09-23",
  "rows": [{
    "row_id": "anthropic/anthropic_api/claude-opus-5-5/2026-09-22",
    "channel": "anthropic_api", "model": "claude-opus-5-5", "aliases": [], "generation": "5.5",
    "effective_from": "2026-09-22", "effective_to": null,
    "usd_per_mtok": {"input": "4.00", "output": "20.00"},
    "multipliers": {"cache_read": "0.05", "cache_write_5m": "1.25", "cache_write_1h": "2"},
    "published_absolute": {"cache_read": "0.20", "cache_write_5m": "5.00", "cache_write_1h": "8.00"},
    "min_cacheable_tokens": 512, "tokenizer_family": "claude-4.7+",
    "per_request_usd": {"web_search": "0.01"},
    "long_context": null, "promotion": null,
    "supports": ["fast_mode", "inference_geo", "1m_context", "batch", "keepalive", "per_message_effort"],
    "enabled": true, "verified_on": "2026-09-23",
    "sources": [{"url": "https://platform.claude.com/docs/en/about-claude/pricing",
                 "retrieved": "2026-09-23", "finding": "anth-pricing-table-2026-09"}]
  }],
  "modifiers": [
    {"modifier_id": "anthropic.batch", "kind": "multiply", "factor": "0.5", "applies_to": ["*"],
     "when": {"service_tier": "batch"}, "stacking": "documented", "sources": [{"url": "…/batch-processing"}]},
    {"modifier_id": "anthropic.inference_geo.us", "kind": "multiply", "factor": "1.1", "applies_to": ["*"],
     "when": {"inference_geo": "us", "generation_gte": "4.6",
              "channel_in": "anthropic_api,claude_platform_aws"}, "stacking": "documented"},
    {"modifier_id": "anthropic.fast.opus-5-5", "kind": "replace_base",
     "base_usd_per_mtok": {"input": "8.00", "output": "40.00"},
     "when": {"speed": "fast", "model_in": "claude-opus-5-5", "channel_in": "anthropic_api"},
     "stacking": "documented", "notes": "cache multipliers apply on top of the fast base"}
  ]
}
```

Effective-dated rows that the registry must carry (D36, §19.1, §19.6): `claude-fable-5-1` and
`claude-mythos-5-1` from **2026-09-01** (launch; 0.025× reads), enabled; `gpt-5.6-sol` (a) 2026-07-09 →
2026-08-21 with prices **VERIFY** (`enabled: false` → usage in that window is unpriced, `dq.unverified_rate_row`)
and (b) 2026-08-21 → **2026-11-22** (exclusive) at $4 / $0.40 cached / $5 write / $20 output with
`promotion: "openai.gpt-5.6-sol.2026-08"` (promotional "at least through 2026-11-21"); usage dated on or
after 2026-11-22 is unpriced with `dq.promotion_expired` until a verified row is added (the weekly live
check opens an issue on the promotion's end date).

**Load validation** (`rates/schema.py: load_builtin(provider=None) -> RateLayer`, `load_file(path, name)`;
raise `PricingError` naming the row): every row has ≥ 1 source and a `verified_on`; no overlapping
`[effective_from, effective_to)` for the same `(channel, model)` within a layer; derived cache prices
(`input × multiplier`) equal `published_absolute` exactly for every bucket listed; modifiers reference only
known predicate keys (`service_tier`, `speed`, `inference_geo`, `endpoint_scope`, `channel_in`,
`model_in`, `generation_gte`) and bucket names; a `promotion` id must exist in `core.catalog.PROMOTIONS`;
every Decimal parses under `EXACT_CTX`; any derivation that trips `Inexact` fails the load (with 60 digits
of precision this only happens on malformed input). `enabled: false` rows (the **VERIFY** rows of §19)
load but never price (`unpriced_reason = "unverified rate row"`, `dq.unverified_rate_row`). A `supports`
entry naming cache-rule behavior (e.g. the retired `effort_keeps_cache`) fails the load (D28).

### 6.2 Resolution (`rates/engine.py: RateCard.resolve(ctx, ts_ms)`)

1. `ctx.model` is already normalized by the adapter (§3.12). Empty model → None ("not priceable").
2. Layered lookup, highest first: **contract overlay** (per-model overrides) → **user layers** (`--rates
   FILE`, `--model-price MODEL=IN,OUT`) → **built-in registry**. Within a layer, the enabled row for
   `(ctx.channel, ctx.model)` (or an alias) whose `[effective_from, effective_to)` contains the UTC date of
   `ts_ms`. A channel without its own row falls back to the `anthropic_api` row only for `claude_platform_aws`
   and `foundry` (same list prices; CCU conversion is handled by contract, §6.5). A date before the first
   row → None with `dq.model_before_effective_date`; a date after a promotional row's `effective_to` with no
   successor row → None with `dq.promotion_expired`.
3. `replace_base` modifiers (fast mode) replace input/output base rates; then cache rates = base input ×
   row multipliers; then every matching `multiply` modifier (batch, US geo, regional endpoint +10%, OpenAI
   flex/fast/regional) multiplies every bucket in `applies_to`. Multiplication is exact, so order does not
   matter; each modifier's `stacking` (documented/assumed) is carried to provenance
   (`ResolvedRates.stacking_assumed`).
4. **Unknown endpoint scope** (D24, critic 1): on channels whose modifiers predicate on `endpoint_scope`
   (Bedrock, Vertex; OpenAI regional processing), `ctx.endpoint_scope == "unknown"` resolves the global
   rates as the point/low and attaches the regional rates as `ResolvedRates.scope_range`; every line is then
   a range `[global, regional]` (ESTIMATED, `dq.scope_unknown`). On other channels scope is ignored.
5. Long-context band: when the row has `long_context_threshold` and the request's `total_input` exceeds it,
   the **whole request** is priced at the band rates (OpenAI > 272K; `ResolvedRates.long_context_band`).
   (`resolve` receives `total_input` through `price_usage`; the protocol's `resolve()` without usage uses
   base rates.)
6. Contract overlay last: per-model overrides replace bucket rates; `multiplier` multiplies every bucket;
   basis becomes CONTRACT (only for the overlay's `channels`, all when empty).
7. **Billing path** (D26): `ctx.billing_path == "subscription"` keeps the resolved list rates but the
   resulting figures carry basis `LIST_EQUIVALENT` (contract overlays do not apply: allowance usage is not
   metered). `usage_credits` and every other path use the card's basis.

### 6.3 Pricing an inference (`price_inference` / `price_usage`)

One `PricedLine` per non-zero bucket and per server-tool counter; `amount_nano = token_nano(quantity, rate)`
(one rounding per line). **Exactness is per line** (R9):

| condition | lines affected | line treatment |
|---|---|---|
| `billable is False` | all | EXACT 0 (lines kept with amount 0 for transparency; `billing_rule_id`) |
| model unresolved | — | `unpriced(reason)`; counted in coverage (R2) |
| `billable is None` | all | range [0, full], point = full |
| `usage_source == ESTIMATED` | all | range [point, point] with note naming the reconstruction (e.g. hidden compaction) — ESTIMATED |
| `usage_source == PARTIAL_STREAM` | all | per billing rule `anthropic.abort.client` (unknown → range [0, full]) |
| `cache_write_unknown > 0` | that line | range low = 5m rate, high = 1h rate, point = hint rate (else low) (R5) |
| `endpoint_scope` unknown on a scope-priced channel | all | range [global, regional] (§6.2 #4) |
| `usage_source == MESSAGE_START_ONLY` | output line | range [logged, max(logged, output_upper)] × output rate, point = logged; input lines EXACT |
| otherwise | — | EXACT |

`PricedInference.figure` is EXACT iff every line is exact; `exact_nano` = Σ exact lines; `estimated` = Σ
range lines (ESTIMATED Figure) or None. Basis per §6.2 #7.

`price_total(pricer, items: Iterable[tuple[Inference, int]]) -> PricedTotal` sums exact lines on billed
bases into `exact`, range lines on billed bases into `estimated`, every LIST_EQUIVALENT line into
`allowance` (never mixed), counts unpriced inferences and tokens, and computes coverage.
Provider-reported costs (basis `provider_estimate`) are never added.
`no_cache_equivalent_nano(pricer, inference, ts_ms) -> int` prices the same tokens with every input token at
the uncached list rate and no discounts (the ESR denominator, §14.1).

### 6.4 Unit rates for hot loops

`unit_rates(ctx, ts_ms)` returns `UnitRates` whose bucket numerators are `rate_usd_per_mtok × 10^(s−6)`
for the smallest `s ≥ 6` making every bucket integral; if `s > 24` it returns None and callers use
`price_usage`. `UnitRates.bucket_nano(bucket, tokens) = scaled_to_nano(tokens × numerator, s)` rounds
half-even exactly like `token_nano`, so hot-path and Decimal-path costs agree to the nano (differential
test). Contract multipliers such as 0.8537 are fine (e.g. $25 × 0.8537 = 21.3425 $/MTok → s = 10). Unit
rates are the point (low) rates; range lines are priced through `price_usage` by callers that need bounds.

### 6.5 Contract overlays (`rates/contract.py`)

```python
def load_contract(path: Path) -> ContractOverlay            # Token Bill schema "tokenbill/contract@1"
def from_model_pricing(obj: Mapping[str, object], *, name: str) -> ContractOverlay
def to_model_pricing(overlay: ContractOverlay) -> dict[str, object]
```

Claude Code's managed `modelPricing` (managed-only, ≥ 2.1.242) has a `multiplier` and per-model
`overrides{input, output, cacheRead, cacheWrite}`; `cacheWrite` covers both 5m and 1h writes, so on import
the 1h rate is derived as `cacheWrite × 2/1.25` and listed in `assumed_fields` unless the file states a 1h
rate (**VERIFY** the exact JSON shape, §19.8 #5). `to_model_pricing` emits the block so developers' `/usage`
and OTel `cost_usd` show contract rates. CCU channels (Claude Platform on AWS, Foundry: $0.01 per CCU,
discount applied as fewer CCU) are modeled as a contract multiplier derived by reconciliation.
Round trip: `to_model_pricing(from_model_pricing(x)) == x` for the fixture.

### 6.6 Failure-mode billing rules (`rates/billing_rules.py`, `data/billing_rules.json`)

Rows `(provider, failure_mode) → {billed_input, billed_partial_output, cache_written: "yes"|"no"|"unknown",
confidence: "documented"|"assumed"|"unknown", source}`. `rule_for(provider, failure_mode) -> BillingRule`.
Documented rows price EXACT; `unknown` rows price as a range [0, full], ESTIMATED, and the report shows
"assumed" dollars separately. Seed rows:

| rule_id | billed | confidence | source |
|---|---|---|---|
| `anthropic.refusal.pre_output` | no | documented | refusals-and-fallback |
| `anthropic.refusal.ambiguous` (1–16 output tokens) | range | assumed | D10 |
| `anthropic.refusal.mid_stream` | input + streamed output | documented | refusals-and-fallback |
| `anthropic.batch.errored_canceled_expired` | no | documented | batch-processing |
| `anthropic.web_search.failed` | no | documented | pricing |
| `anthropic.max_tokens` | input + output up to the cap | documented | messages API (stop_reason max_tokens is a completed, billed response) |
| `anthropic.abort.client`, `anthropic.overloaded.mid_stream`, `anthropic.pre_token_429_529` | unknown | unknown | `fp-billing-matrix` |
| `vertex.non_200` | no | documented | Vertex pricing |
| `azure_openai.content_filter_400`, `azure_openai.timeout_408` | yes | documented | Azure docs |
| `openai.flex_429` | no | documented | flex processing |
| `openai.max_output_tokens` | input + reasoning + output | documented | `max-tokens-truncation` (OpenAI can bill reasoning with no visible output) |

### 6.7 Staleness, verification and lifecycle

- `RateCard.sha256` = SHA-256 of the canonical JSON of all layers in use; it appears in every output and
  receipt. Any row used whose `verified_on` is older than 45 days emits `dq.stale_rate`.
- `rates/verify.py`: `verify_snapshot(layer, snapshot_path=None) -> list[Discrepancy]` (offline; default
  snapshot is the packaged `rates/data/snapshots/pricing-2026-09-23.json`, a recorded parse of the pricing
  page), `verify_live(layer, url, *, opener=None) -> list[Discrepancy]` (opt-in `--live`; stdlib `urllib`, TLS
  on, 30 s timeout; parses the model table of the `.md` pricing page), `crosscheck_feed(layer, feed_json) ->
  list[Discrepancy]` (LiteLLM map / OpenRouter models JSON supplied as files; **non-authoritative**:
  discrepancies are warnings, never failures). `tokenbill pricing verify` exits 3 on authoritative
  discrepancies. A weekly CI job (integration) runs `--live` and opens an issue on drift and on any
  promotion within 14 days of its `not_before_end`.
- Lifecycle (`core.catalog`, D29): same-tier **successors** only where the tokenizer family is identical and
  the vendor documents the successor (`claude-fable-5 → claude-fable-5-1`, `claude-opus-5 → claude-opus-5-5`,
  `claude-mythos-5 → claude-mythos-5-1`); **no** `claude-opus-4-8 → claude-opus-5-5` or
  `claude-sonnet-4-6 → claude-sonnet-5` successor (cross-family or equivalence unverified: those moves are
  priced only as trade-off remaps with the tokenizer band). Retirement floors (Sonnet 4.5 2026-09-29, Haiku 4.5
  2026-10-15, Opus 4.5 2026-11-24), promotions, announced-but-unpriced models (Sonnet 5.5, Haiku 5.5). Used for
  same-tier repricing, promotion handling and warnings only.

### 6.8 v0.1 `pricing.py` compatibility (the one sanctioned edit of a v0.1 module)

`pricing.py` stays a standalone table (it must **not** import `rates/`, so the frozen engine stays
decoupled). Additive changes only:

- `ModelPricing` gains `cache_write_1h_multiplier: float = 2.0` (unused by the v0.1 engine; documents the
  rate).
- New rows: `claude-opus-5-5` ($4.00/$20.00, `cache_read_multiplier=0.05`, min 512), `claude-mythos-5-1`
  ($10/$50, read 0.025, min 512), `claude-mythos-5` ($10/$50, min 512), `claude-opus-4-5` ($5/$25, min
  4096), `claude-sonnet-4-5` ($3/$15, min 1024). `claude-opus-4-1` / `claude-opus-4` only if their minimum
  cacheable prefix is verified (§19.8 #1); otherwise not added. (`claude-fable-5-1` at read 0.025 is already
  present and consistent with the registry row from 2026-09-01.)
- `tests/test_pricing.py` `SPEC_TABLE` gains exactly the same rows **in the same commit** (the existing
  `test_pricing_table_matches_spec_exactly` asserts set equality).
- A parity test (`tests/v2/rates/test_legacy_parity.py`) asserts every `PRICING` row equals the matching
  built-in `anthropic_api` registry row effective on 2026-09-23 (input, output, read multiplier, min
  cacheable); a facts parity test (`tests/v2/rates/test_facts_parity.py`) asserts every `core/facts.json`
  rate row and modifier equals the registry's (D37).

### 6.9 Golden billing corpus (acceptance gate; `tests/v2/rates/test_golden_billing.py`)

Each case is hand-computed in the test's comments. USD shown; tests compare int nano.

| # | Case | Expected |
|---|---|---|
| 1 | Opus 5.5 standard: uncached 1,000; read 100,000; 5m write 2,000; 1h write 3,000; output 500 | 0.004 + 0.020 + 0.010 + 0.024 + 0.010 = **0.068** |
| 2 | case 1 with `inference_geo="us"` | **0.0748** |
| 3 | case 1 in batch; batch + US geo | **0.034**; **0.0374** |
| 4 | case 1 with `speed="fast"` (base $8/$40; read 8×0.05; 5m 8×1.25; 1h 8×2) | 0.008 + 0.040 + 0.020 + 0.048 + 0.020 = **0.136** |
| 5 | Fable 5.1 read 1,000,000 vs Fable 5 read 1,000,000 (both dated 2026-09-10) | **0.25** vs **1.00** |
| 6 | Opus 5.5 with 3 web searches | + **0.03** |
| 7 | Opus 5 via Bedrock input 1,000,000: `global.` vs in-region vs scope unknown | **5.00** vs **5.50** vs range **[5.00, 5.50]** ESTIMATED |
| 8 | case 1 with contract multiplier 0.85 | **0.0578**, basis CONTRACT |
| 9 | Opus 5.5 `cache_write_unknown` 1,000,000 (OTel) plus uncached 1,000 and output 500 | exact part **0.014**; estimated range **[5.00, 8.00]**; width **3.00** |
| 10 | gpt-5.6-sol dated 2026-09-10: input 10,000 incl. cached 6,000 and write 2,000; output 1,000 | 0.008 + 0.0024 + 0.010 + 0.020 = **0.0404** |
| 11 | gpt-5.6-sol dated 2026-09-10: 300,000 uncached input, 1,000 output (long-context band) | 300,000 × $8/M + 1,000 × $30/M = **2.43** |
| 12 | fallback iterations: declined attempt output 0 on Fable 5; fallback on Opus 4.8 | declined **0.00** (`anthropic.refusal.pre_output`); fallback priced at Opus 4.8 |
| 13 | declined attempt output 6 on Fable 5 | ESTIMATED range [0, full] (`anthropic.refusal.ambiguous`) |
| 14 | compaction iteration + message iteration | sum of both at the message model |
| 15 | unknown model `claude-foo-9` | unpriced, `unpriced_reason="no rate row"`; totals report coverage < 1 |
| 16 | ids `claude-haiku-4-5-20251001`, `claude-sonnet-4-6@20260101`, `claude-opus-5-5[1m]`, `global.anthropic.claude-opus-5-5-v1:0` | resolve to base rows with correct channel/scope (Vertex id → scope unknown) |
| 17 | Opus 5.5 usage dated 2026-09-21 | unpriced (row effective 2026-09-22) |
| 18 | Opus 5.5, 1,000,000 1h-write tokens, batch + US geo | 4 × 2 × 1.1 × 0.5 = **4.40** |
| 19 | Opus 4.8 output 3 tokens under contract multiplier 0.8537 | 64,027.5 nano → **64,028** nano (half-even) |
| 20 | 10⁶ lines of 1 Haiku 4.5 cache-read token | Σ = 10⁶ × 100 nano = **$0.10** exactly |
| 21 | gpt-5.6-sol dated 2026-08-01; dated 2026-11-22 | unpriced ("unverified rate row"); unpriced (`dq.promotion_expired`) |
| 22 | Fable 5.1 read 1,000,000 dated 2026-08-20 | unpriced (`dq.model_before_effective_date`) |
| 23 | case 1 with `billing_path="subscription"` | **0.068**, basis LIST_EQUIVALENT, `is_billed_eligible` False, lands in `PricedTotal.allowance` |
| 24 | Opus 5.5 MESSAGE_START_ONLY: uncached 0, read 100,000, output logged 3, `output_upper` 403 | exact part **0.020**; output line range **[0.00006, 0.00806]**, point 0.00006 |

Plus: `pricing verify` against the packaged snapshot reports zero discrepancies and one injected
discrepancy with its row id; an overlapping interval fails the load; a row verified 60 days before the run
date emits `dq.stale_rate`; `assert_pricer_conforms(RateCard(...))` passes; the legacy and facts parity
tests pass.

---

## 7. Store (STORE, `tokenbill/store/`)

SQLite via stdlib `sqlite3`; one file per ledger; created `0600` in a `0700` directory (Windows: §3.9
best-effort ACL); `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, `synchronous=NORMAL`; schema version in
`meta`; migrations are forward-only functions `migrate_<n>_to_<n+1>(conn)`. **Money columns are INTEGER
nano-USD**, so `SUM()` is exact (SQLite raises on 64-bit overflow, ≈ $9.2B, far above any ledger).

### 7.1 Schema (`store/schema.py`, DDL is part of the contract)

```sql
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
  -- schema_version, org_key_id, name_key_id, content_tier, created_ms
CREATE TABLE sources(source_id TEXT PRIMARY KEY, adapter TEXT NOT NULL, name_hmac TEXT, sha256 TEXT,
  name_key_id TEXT, principal_key_id TEXT, ingested_ms INTEGER, records INTEGER, quarantined INTEGER,
  stats_json TEXT);
CREATE TABLE cursors(source_id TEXT NOT NULL, unit_hmac TEXT NOT NULL, byte_offset INTEGER NOT NULL,
  head_sha TEXT NOT NULL, size INTEGER, mtime_ns INTEGER, PRIMARY KEY(source_id, unit_hmac));
CREATE TABLE sessions(session_key TEXT PRIMARY KEY, source_kind TEXT, started_ms INTEGER, ended_ms INTEGER,
  attribution_json TEXT);
CREATE TABLE lanes(lane_key TEXT PRIMARY KEY, session_key TEXT NOT NULL, kind TEXT, parent_lane_key TEXT,
  cache_scope_key TEXT, ttl_observed TEXT, lane_exact INTEGER, team TEXT, billing_class TEXT);
CREATE TABLE requests(request_id TEXT PRIMARY KEY, lane_key TEXT NOT NULL, session_key TEXT NOT NULL,
  seq INTEGER NOT NULL, ts_start_ms INTEGER NOT NULL, date_utc TEXT NOT NULL,
  fidelity INTEGER NOT NULL, source_priority INTEGER NOT NULL, adapter TEXT NOT NULL, sources_mask INTEGER NOT NULL,
  principal TEXT, team TEXT, cost_center TEXT, project TEXT, repo TEXT, workspace_id TEXT, api_key_id TEXT,
  agent_product TEXT, agent_type TEXT, query_source TEXT, skill TEXT, mcp_server TEXT, plugin TEXT,
  workload_class TEXT, entrypoint TEXT, client_version TEXT, billing_path TEXT, cwd_key TEXT, arm TEXT, wave TEXT,
  attr_extra_json TEXT,                       -- Attribution.extra (sorted pairs), e.g. mdm_group, gateway, task_id
  attr_prio_json TEXT, params_json TEXT, appended_json TEXT, fp_json TEXT);
CREATE TABLE attempts(attempt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests ON DELETE CASCADE,
  attempt_no INTEGER, ts_start_ms INTEGER, ttft_ms INTEGER, duration_ms INTEGER, outcome TEXT, http_status INTEGER,
  error_type TEXT, retry_layer TEXT, retry_after_ms INTEGER, should_retry INTEGER, sdk_retry_count INTEGER,
  provider_request_id TEXT, provider_message_id TEXT, model_served TEXT, stop_reason TEXT, diag_reason TEXT,
  diag_provider_reason TEXT, diag_missed_tokens INTEGER, diag_source TEXT, applied_edits_json TEXT,
  thinking_dropped INTEGER, raw_usage_json TEXT, convention_id TEXT);
CREATE TABLE inferences(inference_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts ON DELETE CASCADE,
  request_id TEXT NOT NULL, lane_key TEXT NOT NULL, lane_kind TEXT, ts_ms INTEGER NOT NULL, date_utc TEXT NOT NULL,
  kind TEXT, usage_source TEXT, billable INTEGER, billing_rule_id TEXT, output_upper INTEGER,
  provider TEXT, channel TEXT, model TEXT, model_raw TEXT, service_tier TEXT, speed TEXT, inference_geo TEXT,
  endpoint_scope TEXT, write_ttl_hint TEXT, billing_path TEXT,
  uncached_input INTEGER, cache_read INTEGER, cache_write_5m INTEGER, cache_write_1h INTEGER,
  cache_write_other INTEGER, cache_write_other_ttl_s INTEGER, cache_write_unknown INTEGER, output INTEGER,
  output_reasoning INTEGER, web_search_requests INTEGER, web_fetch_requests INTEGER,
  priced_nano INTEGER,                        -- point (exact + estimated points); NULL when unpriced
  exact_nano INTEGER NOT NULL DEFAULT 0,      -- Σ exact lines (R9)
  est_nano INTEGER NOT NULL DEFAULT 0, est_low_nano INTEGER NOT NULL DEFAULT 0, est_high_nano INTEGER NOT NULL DEFAULT 0,
  evidence TEXT, basis TEXT, unpriced_reason TEXT,
  nano_uncached INTEGER, nano_read INTEGER, nano_w5m INTEGER, nano_w1h INTEGER, nano_wother INTEGER,
  nano_wunknown INTEGER, nano_output INTEGER, nano_server_tools INTEGER,   -- per-bucket line amounts (point)
  exact_mask INTEGER,                         -- bit per bucket: 1 = that line is exact
  rate_card_sha TEXT, provider_cost_nano INTEGER, provider_cost_basis TEXT,
  team TEXT, principal TEXT, workspace_id TEXT, workload_class TEXT, agent_product TEXT);
CREATE TABLE blocks(h TEXT PRIMARY KEY, key_id TEXT, h_sorted TEXT, h_norm TEXT, tier TEXT, kind TEXT, role TEXT,
  n_bytes INTEGER, est_tokens INTEGER, image_w INTEGER, image_h INTEGER, volatile_classes TEXT, lookback_pos INTEGER,
  deferred INTEGER);
CREATE TABLE events(event_id TEXT PRIMARY KEY, lane_key TEXT NOT NULL, ts_ms INTEGER NOT NULL, date_utc TEXT NOT NULL,
  kind TEXT NOT NULL, attrs_json TEXT);
CREATE TABLE aggregates(agg_id TEXT PRIMARY KEY, source_kind TEXT, bucket_start_ms INTEGER, bucket_end_ms INTEGER,
  dims_json TEXT, usage_json TEXT, reported_cost_nano INTEGER, reported_cost_basis TEXT, list_cost_nano INTEGER,
  finality TEXT, fetched_ms INTEGER);
CREATE TABLE cost_lines(line_id TEXT PRIMARY KEY, source_kind TEXT, date_utc TEXT, channel TEXT, workspace_id TEXT,
  description TEXT, model TEXT, cost_type TEXT, token_type TEXT, sku TEXT, service_tier TEXT, inference_geo TEXT,
  endpoint_scope TEXT, amount_nano INTEGER NOT NULL, list_amount_nano INTEGER, currency TEXT, finality TEXT,
  principal TEXT, fetched_ms INTEGER);
CREATE TABLE outcomes(date_utc TEXT NOT NULL, team TEXT NOT NULL, source_kind TEXT NOT NULL, n_users INTEGER,
  sessions INTEGER, commits INTEGER, pull_requests INTEGER, lines_added INTEGER, lines_removed INTEGER,
  edits_accepted INTEGER, edits_rejected INTEGER, PRIMARY KEY(date_utc, team, source_kind));
CREATE TABLE message_index(provider_message_id TEXT PRIMARY KEY, request_id TEXT NOT NULL);
CREATE TABLE request_index(provider_request_id TEXT PRIMARY KEY, request_id TEXT, collision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE daily_rollup(date_utc TEXT NOT NULL, team TEXT, cost_center TEXT, workspace_id TEXT, workload_class TEXT,
  lane_kind TEXT, model TEXT, billing_path TEXT, arm TEXT, wave TEXT, active_users INTEGER NOT NULL,
  requests INTEGER NOT NULL, uncached_input INTEGER, cache_read INTEGER, cache_write INTEGER, output INTEGER,
  exact_nano INTEGER NOT NULL,                -- billed-basis exact lines only
  allowance_nano INTEGER NOT NULL,            -- LIST_EQUIVALENT lines (point)
  est_low_nano INTEGER, est_high_nano INTEGER, unpriced_inferences INTEGER, rate_card_sha TEXT,
  PRIMARY KEY(date_utc, team, cost_center, workspace_id, workload_class, lane_kind, model, billing_path, arm, wave));
CREATE TABLE cluster_day(date_utc TEXT NOT NULL, cluster_kind TEXT NOT NULL, cluster_id TEXT NOT NULL,
  arm TEXT, wave TEXT, active_users INTEGER NOT NULL, requests INTEGER NOT NULL, exact_nano INTEGER NOT NULL,
  allowance_nano INTEGER NOT NULL, PRIMARY KEY(date_utc, cluster_kind, cluster_id, arm, wave));
CREATE TABLE findings(run_id TEXT NOT NULL, finding_id TEXT NOT NULL, created_ms INTEGER, json TEXT NOT NULL,
  PRIMARY KEY(run_id, finding_id));
CREATE TABLE receipts(receipt_id TEXT PRIMARY KEY, lever_id TEXT, lever_class TEXT, label TEXT,
  realization_rate TEXT, created_ms INTEGER, json TEXT NOT NULL, dsse TEXT);
CREATE TABLE key_events(ts_ms INTEGER NOT NULL, key_kind TEXT NOT NULL, old_key_id TEXT, new_key_id TEXT);
CREATE TABLE audit(ts_ms INTEGER NOT NULL, actor TEXT, action TEXT NOT NULL, detail_json TEXT);
-- indexes
CREATE INDEX req_lane ON requests(lane_key, ts_start_ms, seq);
CREATE INDEX req_date_team ON requests(date_utc, team);
CREATE INDEX lane_team_kind ON lanes(team, kind, billing_class);
CREATE INDEX att_req ON attempts(request_id);
CREATE INDEX inf_date_model ON inferences(date_utc, model);
CREATE INDEX inf_lane ON inferences(lane_key, ts_ms);
CREATE INDEX inf_req ON inferences(request_id);
CREATE INDEX ev_lane ON events(lane_key, ts_ms);
CREATE INDEX cl_date_channel ON cost_lines(date_utc, channel);
```

### 7.2 `SqliteStore` (`store/db.py`) implements `LedgerStore`

`SqliteStore(path, *, create=True, org_key: bytes | None = None, name_key_id: str | None = None,
pricer: Pricer | None = None, read_only: bool = False)` (read-only instances are what shard worker
processes open). `ingest(result, pricer=…)` runs in 5,000-row transactions with `executemany`;
pseudonymizes `r_`/`c_` principals with the org key before any write (§8.3; a request carrying `r_`/`c_`
without an org key raises `PrivacyError`); accepts `p_` values only when `SourceInfo.principal_key_id`
equals `meta.org_key_id`; accepts `h_` values only when `SourceInfo.name_key_id` equals `meta.name_key_id`
(set by the first ingest), else nulls those fields for that source with `dq.name_key_mismatch`; prices every
billable inference with the given pricer and stores point, exact, estimated and per-bucket columns
(`exact_mask`); re-ingesting a source with the same `sha256` is a no-op; cursors via `get_cursor`/`set_cursor`.
`iter_lanes` streams `requests` ordered by `(lane_key, ts_start_ms, seq)` with a cursor and assembles `Lane`
objects lane by lane (memory bounded by the largest lane), joining attempts, inferences and events;
`where` filters on the `lanes` table (`team`, `kind`, `billing_class` are denormalized there at ingest) and
the request columns listed in §3.6; `lane_keys` restricts to a sample. `lane_index`, `lane_first_reads` and
`count_users` are single SQL queries. `aggregate()` builds parameterized `GROUP BY` SQL over a whitelisted
dimension set (`date`, `team`, `cost_center`, `workspace_id`, `workload_class`, `lane_kind`, `model`,
`agent_type`, `agent_product`, `repo`, `arm`, `wave`, `skill`, `mcp_server`, `billing_path`, `channel`) with
`n_users = COUNT(DISTINCT principal)`; `principal` (or `session_key`) in `group_by` raises `PrivacyError`.
`reprice(pricer)` recomputes priced columns when the rate card changes. `put_findings`/`findings`,
`put_receipt`/`receipts` persist JSON produced by `core.records.to_json`-style encoders.

### 7.3 Merge rules (`store/merge.py`) — idempotent and order-independent

Cross-source identity: the incoming request's `request_id` is replaced by an existing one found through
`message_index[provider_message_id]`, else through `request_index[provider_request_id]` unless that row is
marked `collision` (one `requestId` seen with two message ids ⇒ `collision=1`, `dq.request_id_collision`).

| field group | rule on conflict |
|---|---|
| attempts / inferences / usage / timing (`ts_start_ms`, ttft, duration) | take the incoming set iff `incoming.fidelity > existing.fidelity`; at equal fidelity and same adapter, the set whose serving inference has the larger `output` (split-entry rule); at equal fidelity and different adapters, higher `source_priority`; exact ties keep the existing set. A usage mismatch between merged sources emits `dq.cross_source_usage_mismatch` |
| attribution fields (incl. each `extra` key) | per field: value from the highest-priority source that has a non-null value; equal priority → lexicographically smallest value (per-field priorities kept in `attr_prio_json`) |
| `sources_mask` | bitwise OR (bit per adapter: claude-code 1, trace@1 2, trace@2 4, otlp 8, openai 16, bedrock 32, anthropic-responses 64, claude-code-headless 128) |
| `diag_*` | fill if null |

Invariants (tests): Σ per-request priced nano equals Σ per-inference priced nano; Σ `exact_nano` +
Σ `est_nano` = Σ `priced_nano`; ingesting the same sources twice leaves a byte-identical `iterdump()` of the
data tables; any permutation of 5 sources yields the same dump; two trace@1 files reusing a `run_id` yield
two sessions and the total equals the sum ($22, not $40).

### 7.4 Rollups (`store/rollups.py`)

`refresh_rollups(conn, dates: Iterable[str] | None)` recomputes `daily_rollup` and `cluster_day` for the
given dates (default: dates touched by the last ingest). `active_users = COUNT(DISTINCT principal)` among
requests with ≥ 1 billable inference that day. `cluster_day` rows exist for cluster kinds `team`,
`workspace` and `mdm_group` (from `attr_extra_json` key `mdm_group` when present). Rollups are always
refreshed before identity retention nulls principals, so cost per active developer-day survives for
showback and verification.

### 7.5 Retention and purge (`store/retention.py`)

Defaults (config `retention.*`): `identity_days = 90` (then `principal = NULL` in `requests`, `inferences`
and `cost_lines`, after refreshing rollups for those dates), `request_days = 395`, `event_days = 90`,
`rollup_days = 395`. `apply_retention(conn, now_ms)`; `purge(principal=… | before_ms=…, actor=…)` deletes
matching rows, runs `VACUUM`, and writes an `audit` row (never the raw identity). Every purge, break-glass,
key rotation and retention run writes an audit row. `rotate(conn, old: bytes, new: bytes, kind: str)`
(`store/retention.py`) re-keys stored pseudonyms where the old key is available and records a `key_events`
row.

### 7.6 Pseudonymization (`store/pseudonym.py`)

`Pseudonymizer(org_key).principal(value: str) -> str`: `r_<ref>` → `pseudonym(org_key, "p", ref)`;
`c_<hex>` → `pseudonym(org_key, "p", "c:" + hex)`; `p_…` passes through only under a matching key id
(§7.2). Raw central identities never reach the store: the adapters pseudonymize them at read time with
`IngestOptions.principal_key` (§5.1).

### 7.7 Scale targets (CI perf gates, §17)

Ingest ≥ 20,000 requests/s from trace@2 `usage` files; lane scan of 10⁶ requests ≤ 30 s; `lane_index` over
10⁶ requests ≤ 5 s; peak RSS ≤ 500 MB; store ≈ 1 KB per request.

---

## 8. Privacy, security and supply chain

### 8.1 Content tiers

`none` is the default for scan, collect, ingest, export and every fleet file. `fingerprint` is opt-in
(recorder, trace@1 import, `--content fingerprint` on those sources only) and adds content-derived HMACs only.
`full` is local only: `export` and `collect` refuse it (`PrivacyError`). Redaction is defense in depth; the
design never needs content. Property test: no string field of any record produced in the `none` tier
contains more than 64 bytes of source text (fixtures seeded with long canary strings).

### 8.2 Keys (`core/keys.py`, F-KIT)

`load_or_create(path=None) -> bytes` (32 random bytes via `secrets.token_bytes`, file 0600, dir 0700; default
`~/.config/tokenbill/key` = install key); `load(path) -> bytes` (refuses group/world-readable files on POSIX;
on Windows warns `dq.windows_acl_not_enforced` unless the best-effort ACL succeeded). Key roles:
**install key** (local self-view; name key and principal key for `install` mode; created on first use),
**org key** (central host only; principal key for `central-ingest`; `--key-file`), **collection key**
(distributed by MDM; the fleet-wide **name key** on laptops and on the central host; also the principal
key of `two-stage` collectors; `--collection-key-file`). Hashes are compared only within a `key_id`. Key
rotation: `store.retention.rotate` records a `key_events` row. Pseudonymized data is still personal data
(EDPB 01/2025), hence retention and purge.

### 8.3 Pseudonymization

Adapters pseudonymize at read time (§5.1) with `IngestOptions.principal_key`; the store's
`Pseudonymizer` (§7.6) converts collector `r_`/`c_` values to `p_` with the org key before any write. The
raw value is never written anywhere.

### 8.4 k-anonymity (`core/kanon.py`, F-KIT — the only implementation)

```python
def publish(raw: RawAggregate, *, k: int = 5, parent_of: Callable[[tuple], tuple] | None = None
            ) -> PublishedAggregate
def rescope_findings(findings: Sequence[Finding], *, k: int = 5,
                     count_users: Callable[[Scope], int] | None = None) -> list[Finding]
def require_self_or_aggregate(group_by: Sequence[str], self_principal: str | None) -> None   # raises PrivacyError
def merge_small_groups(rows: Sequence[tuple[str, int, T]], *, k: int, other_label: str = "(other)"
                       ) -> tuple[list[tuple[str, int, T]], int]    # for ingest-time team aggregation (§5.11)
```

`publish`: (1) rows with `n_users < k` merge into an `"(other: <k users)"` row within their parent group
(the group-by prefix without the last dimension); (2) if that merged row still has `< k` users it merges
into the smallest remaining row of the parent (complementary suppression), so no suppressed cell can be
recovered by subtracting published rows from a published total; (3) published totals equal raw totals.
`PublishedAggregate` can only be constructed with `core.types._PUBLISH_TOKEN`, which only `publish` (and the
test helper `core.testing.published_for_tests`) passes; renderers and exporters accept only
`PublishedAggregate`. `rescope_findings`: an org-audience finding whose scope has `n_users < k` is re-scoped
to its parent (team → cost_center → org), merging findings of the same detector and kind; the merged
finding's `n_users` is obtained from `count_users(parent_scope)` (the pipeline passes a closure over
`LedgerStore.count_users`, so the count is an exact distinct count, never a sum of per-child counts that
could double-count people); without `count_users` the merge uses the maximum child count (a lower bound,
which can only suppress more). Findings are never listed by principal. Property test: 1,000 seeded random
tables — no published row has `n_users < k`, totals match, and brute force over subsets cannot recover a
suppressed child; a rescope test where one person appears in two child scopes shows `count_users` prevents
publishing a 4-person parent as 5.

### 8.5 Self-view, break-glass, no ranking

- Individuals see only their own data (`--self`; `tokenbill me`). The self principal is computed locally
  with the install key (local scan) or by the key holder on the central host.
- No command sorts, lists or ranks principals; `--group-by principal` without `--self` is a usage error.
- `findings --break-glass REASON` reveals, for `tail.runaway` findings only, the session pseudonym and team
  (never a person), and writes an audit row.
- API-key dimensions (`api_key_id`) may identify a person when keys are personal (personal and
  service-account keys exist since 2026-08-26): org-scan findings aggregate to workspace unless
  `--group-by api_key` is passed explicitly, and then only keys with ≥ k distinct principals in the ledger
  (or flagged service keys via the allocation rules) are listed.
- No emotion or sentiment inference anywhere.

### 8.6 Secrets

`core/secrets.py` runs in the recorder's `full` tier (redacts before write) and on `collect`/`scan`
(counts only). Outputs report counts by type (`dq.secrets_observed`), never values.

### 8.7 Output hardening

Every terminal string passes `textsafe.sanitize`; HTML escapes all text and ships `<meta
http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">`,
no scripts, no external resources.

### 8.8 Content canary

`CANARY` is planted by fixtures in message text, tool results, tool inputs, `cwd`, `gitBranch`, file paths,
`systemPrompt`/system text, attachment content, headless stream content and emails
(`canary.TB-CANARY-7f3a91@example.com`). It must be absent from: every `IngestResult` (repr and JSON), SQLite
raw bytes (store file read as bytes), trace@2 `usage` and `fingerprint` files, result@2 JSON, HTML, terminal
output, FOCUS CSV, SARIF, ccusage JSON, showback files and policy packs. Each package tests its own outputs;
the end-to-end canary test is owned by integration.

### 8.9 Code-level guards (foundation-owned tests)

- `tests/conftest.py` (F-CORE): autouse fixture patching `socket.socket.connect`/`connect_ex` and
  `socket.create_connection` to raise for any non-loopback address; loopback (127.0.0.0/8, ::1), `AF_UNIX`
  and `socket.socketpair()` stay allowed (asyncio's Windows event loop uses a loopback socketpair; the four
  frozen asyncio tests in `tests/test_instrument.py` must pass on Windows). Tests needing a recorded HTTP
  exchange use in-process fakes (injected `opener`).
- `tests/v2/core/test_no_float_money.py`: AST scan of the money modules listed in §2.4 (skipping paths that
  do not exist yet) for `float(` calls and float literals.
- `scripts/check_ownership.py` + `OWNERSHIP.toml`: CI (`.github/workflows/ownership.yml`, F-CORE, active
  from wave 0) fails a package branch that touches files outside its ownership or any FROZEN file.

### 8.10 Supply chain (integration)

SHA-pinned GitHub Actions with top-level `permissions: {}` and per-job minimal permissions; Dependabot for
actions; zizmor, CodeQL and OpenSSF Scorecard workflows; a zero-runtime-dependency check
(`scripts/check_zero_deps.py`: `[project].dependencies` empty and no non-stdlib import in `tokenbill/`); a
stdlib CycloneDX SBOM script (`scripts/sbom.py`, zero runtime components; dev deps listed separately);
`actions/attest-build-provenance`; PEP 740 attestations (already on in `gh-action-pypi-publish` ≥ 1.11);
`SOURCE_DATE_EPOCH` reproducible builds with a rebuild-and-diff job; immutable releases; a no-egress
end-to-end test (the whole CLI except `--live` paths under the socket guard); hypothesis fuzzers for every
parser (owned by each parser's package; only `TokenbillError` may escape); third-party plugins load only
with `--plugins`; CI matrix Linux + macOS × Python 3.10/3.11/3.12/3.13 and Windows × 3.12 running the full
suite (POSIX mode assertions skipped, ACL path exercised); `coverage` measured per package (≥ 90% of owned
modules). `SECURITY.md` gains a coordinated-disclosure timeline compatible with EU CRA reporting. A signed
container image is deferred (§20).

### 8.11 Governance pack (integration, static docs)

`docs/PRIVACY.md`: data inventory (what each tier contains), flows (laptop/CI → central), lawful-basis
placeholder, retention table (§7.5), access matrix, key custody (install / org / collection keys), k-anonymity,
a DPIA template and one works-council annex ("not for performance evaluation", no individual ranking, no
emotion inference, team-level k ≥ 5, self-view only), and an employee notice template. No jurisdiction
presets. `docs/FLEET.md`: MDM collector rollout, identity modes, key custody, the CI post-step for headless
streams, Windows directory restrictions, the CUR 2.0 CSV export and the reference OpenTelemetry Collector
configuration (OTLP receiver in the collector, not in Token Bill; `redaction` processor with an `allowed_keys`
list matching §19.4 attributes; `file` exporter with `format: json` and `compression: none`), and the
fleet-scale runbook (`--jobs`, shard sizes, nightly schedule).

---

## 9. Simulation

Two replay levels share the cache-rule table (§3.14), the transition definition (§3.15), the `Policy`
type and `ReplayResult`. **Usage-level replay** (REPLAY, `sim/usage_replay.py`) works for every source,
content-free. **Block-level replay** (BLOCK, `sim/block_replay.py`) needs fingerprints.

### 9.1 Principles

1. **Minimal change.** A policy changes only the transitions it affects; every other request keeps its
   billed usage exactly (`ReplayRequestOutcome.changed = False`). Savings are always
   `cost(observed) − cost(policy)`, so model error on unaffected traffic cannot leak into them.
2. **Identity invariant** (unit test only, not calibration): `replay(Policy.observed())` returns `cost ==
   baseline` to the nano, including ranges, where `baseline` is the Σ of `PricedInference.figure` (points:
   exact lines + range-line points; bounds: range-line bounds) over every billable inference of the replayed
   lanes, and every outcome has `changed=False`.
3. **O(n) per lane per policy**, no sorting inside the loop; integer unit rates on the hot path (§6.4),
   `price_usage` fallback when `unit_rates` is None or a line needs bounds.
4. Every counterfactual figure is ESTIMATED, declares its calibration (R6) and its assumptions; trajectory
   levers are flagged `upper_bound=True` and `needs_eval=True`.
5. **One billing class per replay.** Lanes on the subscription path (allowance, D26) and billed lanes are
   never replayed together (mixed input raises `UsageError`); the pipeline partitions by
   `core.records.billing_class`, so allowance headroom never mixes with invoice savings.
6. **Shards.** Replays of disjoint lane sets add (`core.shards.merge_replay`); the pipeline replays per shard
   (optionally in a process pool) and merges in shard order, so results are identical for any shard size
   or `--jobs` value.

### 9.2 Quantities

Per lane, requests `i = 0..n−1` in `(ts_start_ms, seq)` order; from the **serving inference**: `R_i`
(read), `W_i` (all write buckets; `W5_i`, `W1_i`, `Wo_i`, `Wu_i`), `U_i` (uncached), `O_i` (output), `T_i =
R_i + W_i + U_i`, `P_i = R_i + W_i`. All other billable inferences of the request (declined attempts,
advisor, compaction iterations, failed attempts) are **passthrough**: priced unchanged unless a policy
explicitly names them (rate transforms reprice them; `retry_backoff_cap` drops extra attempts). `obs_i` =
the observed `Transition` from `classify_transitions` on the unmodified lane. Primes denote policy values;
`S = static_prefix_floor[(scope, model)]` (0 if absent). `τ_obs,i` = observed TTL (§3.15); `τπ` = the
policy TTL for the lane (`ttl` selector match; else `τ_obs,i`).

**alive_π(i)** (the cached prefix of request `i−1` is readable at `ts_start_i` under the policy): same
serving model (after remap) as `i−1`, no reset event, no un-repaired param change, same cache scope, and:
with a keepalive policy — `gap ≤ 300 s` or `n·κ + 300 s ≥ gap` (n from §9.3.2); otherwise `gap ≤ τπ`.
When `|gap − τπ| ≤ 10 s` the transition is **ambiguous**: the point estimate follows the rule and the range
takes both outcomes (hit for `low`, miss for `high`).

### 9.3 Policy semantics (usage level)

Application order inside each request (fixed, so joint policies compose deterministically):
**(1) rate transforms → (2) context transforms → (3) cache-state transforms → (4) TTL re-rating, min-prefix
gate, batch → (5) pricing.**

#### 9.3.1 TTL policy `ttl=(selector, "5m"|"1h")` — two-way flips (D8)

For lanes matched by the selector (Anthropic channels only; others skipped with a reason):

- **Re-rating:** every write token of the lane (observed buckets incl. unknown, and writes created by the
  policy) is placed in the `τπ` bucket (`cache_write_5m` or `cache_write_1h`).
- **Miss → hit** (`τπ > τ_obs,i`): if `obs_i.is_miss_event` and `obs_i.cause == "ttl-expiry"` and
  `alive_π(i)`: `R'_i = min(E'_i, T'_i − U'_i)`, `W'_i = T'_i − U'_i − R'_i` (the appended tokens), where
  `E'_i = min(P'_{i−1}, T'_i)`.
- **Hit → miss** (`τπ < τ_obs,i`): if `obs_i` is not a miss event and `gap_i > τπ` (and `≤ τ_obs,i`):
  `R'_i = min(S, T'_i − U'_i)`, `W'_i = T'_i − U'_i − R'_i`.
- Otherwise the split is kept. Because `P'_i = T'_i − U'_i` either way, chained transitions stay consistent.

#### 9.3.2 Keepalive `keepalive=(selector, κ, M)` (SDK/API agents only)

Only lanes whose `agent_product != "claude_code"` (Claude Code cannot be configured to ping; such lanes are
skipped with reason "keepalive not allowed for claude_code"). TTL stays 5m. For each transition with
`gap > κ`: the non-clairvoyant daemon sends `n = min(ceil(gap/κ) − 1, floor(M/κ))` pings (D9); each ping is
a KEEPALIVE inference with usage `{cache_read: P'_{i−1}, uncached_input: U'_{i−1}, output: 0}` at the lane's
model. The transition is alive iff `gap ≤ 300 s` or `n·κ + 300 s ≥ gap`; alive TTL-expiry misses flip to
hits as in §9.3.1 (writes at the 5m rate). The ping itself is a non-streaming `max_tokens: 0` re-send; the
lane's own requests may stream (`stream` is not part of the cache key). Lanes whose requests use structured
outputs (`output_format` set), a forced `tool_choice` (`any` or `tool:*`), `thinking = "enabled:*"` (rejected
with `max_tokens: 0`), or batch are skipped with a reason. The hindsight minimum `ceil((gap − 300)/κ)` is
reported in assumptions as a lower bound only. Break-even idle for one gap is `κ(w/r − 1)` (≈ 46 min at 0.1×
reads, ≈ 96 min Opus 5.5, ≈ 196 min Fable 5.1); the replay does not use it (it prices each gap), but the
TTL advisor reports it.

#### 9.3.3 Compaction window `compaction_window=(w, S_c)` (MAIN lanes, models with `supports:1m_context`)

State `removed = 0`. At request `i`: an observed reset (COMPACTION/CLEAR event in `(ts_{i−1}, ts_i]` or
`T_i < 0.5·T_{i−1}`) sets `removed = 0`. `T_eff = T_i − removed`. `S_c` = the org median `post_tokens` of
COMPACTION events, else `COMPACTION_SUMMARY_TOKENS_DEFAULT` (20,283, flagged in assumptions).
`new_i = max(0, T_i − T_{i−1})` (`T_0` for i = 0).

- If `T_eff > w`: insert a COMPACTION inference: when `alive_π(i)`: `cache_read = min(P'_{i−1}, T_eff)`,
  `cache_write(τπ) = T_eff − cache_read`; else `cache_write(τπ) = T_eff`; `output = S_c`. Then the request
  becomes `T'_i = S_c + new_i`, `R'_i = 0`, `U'_i = min(U_i, T'_i)`, `W'_i = T'_i − U'_i`; `removed = T_i −
  T'_i`; `added_calls += 1`. Step (3) is skipped for this request.
- Else: `R_eff = max(0, R_i − removed)`; `W_eff = W_i if R_i ≥ removed else max(0, W_i − (removed − R_i))`;
  `T'_i = R_eff + W_eff + U_i` (removed tokens come out of reads first, then writes).

Label: ESTIMATED, `upper_bound=True` (ignores re-work and quality), trade-off, `needs_eval=True`.
Sanity: `w` above the lane's maximum context changes nothing.

#### 9.3.4 Cold resume `cold_resume=(action, min_ctx)` (MAIN lanes)

At a transition with `not alive_π(i)` (i ≥ 1) and `T_eff > min_ctx` (default 200,000): `compact` inserts an
OTHER inference with `uncached_input = T_eff` (a cold summarization call) and `output = S_c`, then `T'_i =
S_c + new_i`; `clear` inserts nothing and sets `T'_i = T_0 + new_i` (restart from the lane's first context).
Then `R'_i = 0`, `U'_i = min(U_i, T'_i)`, `W'_i = T'_i − U'_i`, `removed = T_i − T'_i`. Trajectory lever.

#### 9.3.5 Rate transforms

- `model_remap=(selector, target)`: every inference of matched lanes is repriced at the target model (same
  channel/tier/speed/geo); when tokenizer families differ, every token quantity is scaled by a band: legacy
  → 4.7+ `[1.00, 1.35]`, 4.7+ → legacy `[1/1.35, 1.00]`, point 1.00 (`TOKENIZER_BAND`); the min-prefix gate
  uses the target's minimum. Price-only; trade-off; `needs_eval`. Same-tier upgrade (target is
  `core.catalog.successor(model)`, same tokenizer family by construction) has no band.
- `effort=((selector, max_level, s), …)`: for requests of matched lanes whose `params.effort` ranks above
  `max_level` (low < medium < high < xhigh < max): `O'_i = O_i − th_i·(1 − s)`, `th_i = output_reasoning` if
  known else `floor(0.505·O_i)`; range uses `s ± 0.25` clipped to [0, 1]. Used by `cc.max_effort` (cap,
  all lanes) and `cc.default_effort` (MAIN Claude Code lanes; a default change affects every request that
  ran at the old default, so it is an upper bound: requests a user deliberately escalated would stay).
  The effort change itself is not a cache event here (a default applies from session start). Trajectory;
  `needs_eval`; upper bound.
- `fast_off`: `speed` → standard for every inference; `param-change`/`fast-toggle` misses flip to hits.
  `geo_global`: `inference_geo` → None. `regional_to_global`: `endpoint_scope` → global. These are exact
  rate arithmetic on identical tokens (the saving is EXACT; the decision to drop the premium is a policy
  choice, lever class `rate`).
- `batch="eligible"`: eligible requests = lane length 1, `service_tier != "batch"`, `speed != "fast"`,
  `workload_class ∈ {ci, eval, scheduled, service}`, no Managed Agents entrypoint. Price at tier batch;
  reads retained at hit band `h`: `R'' = floor(h·R')`, the rest become 5m writes (Bedrock: uncached, no
  caching in Bedrock batch); point `h = 0.64`, low cost uses `h = 0.98`, high cost `h = 0.30`.

#### 9.3.6 Repairs

| repair id | affected transitions | counterfactual |
|---|---|---|
| `restore_caching` | lanes with ≥ 5 requests, median `T ≥ max(min_cacheable, 4096)`, Σ reads = Σ writes = 0 | request 0: `W' = T_0`, `U' = 0`; i ≥ 1 with `gap ≤ τπ` (default 300 s): `R' = min(T'_{i−1}, T_i)`, `W' = T_i − R'`, `U' = 0`; otherwise `W' = T_i` |
| `stagger_fanout` | groups of ≥ 2 lane-first requests in the same (scope, model, cwd_key) starting within 10 s, each `W ≥ 0.8·T` | first member unchanged; others `R' = min(W_j, shared)`, `W' = W_j − R'`, `shared = min_group W`; `upper_bound=True` |
| `retry_backoff_cap` | requests with ≥ 2 attempts whose final attempt started > `τ_obs` after the first and wrote ≥ 0.5·E | final attempt warm (`R' = min(E, T − U)`); attempts beyond 3 dropped |
| `fallback_credit` | `model-switch/refusal-fallback` misses whose first call on the new model wrote ≥ 0.8·E | `R' = min(E, T − U)` at the new model's read rate |
| `shared_ci_prefix` | CI lanes (workload ci) whose first request wrote ≥ 0.8·T, in the same (scope, model), consecutive run starts within `τπ` | first request of each later run reads `min(S_ci, T − U)`, `S_ci = S` if known else `floor(0.8·min first-call W)`; `upper_bound=True` |

#### 9.3.7 Common tail

(4) If a TTL policy matches the lane, all write tokens are re-rated to `τπ`; if a changed request has `T'_i <
min_cacheable(model')`, then `R' = W' = 0`, `U' = T'_i`. (5) Price every serving and extra inference with the
transformed context; passthrough inferences keep their observed pricing unless a rate transform applies.

### 9.4 Calibrated mode

For every transition a policy flips **to a hit** (§9.3.1–9.3.2, `fast_off`, repairs), the calibrated cost is
`ρ(band)·cost_hit + (1 − ρ(band))·cost_nohit`, rounded once to nano per transition, where `cost_nohit` is
the observed split re-rated under the policy and `ρ` comes from the model gate's per-gap-band fit (§9.6).
Without a passing calibration report the mode is unavailable and results are labeled `UNCALIBRATED`.

### 9.5 Policy spec grammar (`core/policy.py`, F-SEM: `parse_policy(spec) -> Policy`, `to_spec(p)`)

A policy spec is a `;`-joined list of clauses; a clause is `name=value[,opt=val…][@selector]` where the
selector (default `all`) is a `,`-joined AND of `key:value` terms (§3.5). Clauses: `ttl=5m|1h@<sel>`,
`keepalive=240s,max=3600s@<sel>`, `compact-window=400000[,post=20283]`, `cold-resume=compact|clear[,min=200000]`,
`model=<model id>@<sel>` (repeatable), `effort=<level>[,scale=0.5][@<sel>]` (repeatable), `fast=off`,
`geo=global`, `regional=global`, `batch=eligible`, `repair=<id>` (repeatable),
`breakpoints=observed|end|static_plus_end|every_15`. Canonical form (`to_spec`; `Policy.spec()` delegates):
clauses in the order just listed, repeated clauses sorted by selector then value, durations in seconds with
an `s` suffix, `@all` omitted, selector terms sorted by key. Example:
`ttl=1h@agent_product:claude_code,lane_kind:main;repair=fallback_credit`. `parse_policy` must satisfy
`parse_policy(to_spec(p)) == p` for every Policy (hypothesis) and reject unknown clauses/selectors with
`UsageError`. Because the grammar lives in core, PLAN, DETECT, REPLAY and the CLI all parse the same way.

### 9.6 Model gate: predictive calibration (`sim/calibrate.py`, REPLAY)

```python
LaneBatches = Callable[[], Iterable[Sequence[Lane]]]      # re-iterable: called once per pass
def calibrate(lane_batches: LaneBatches, *, pricer: Pricer, rules: CacheRulesProvider,
              granularity: str = "day", folds: int = 5, seed: int = 0,
              static_prefix_floor: Mapping[tuple[str, str], int] | None = None) -> CalibrationReport
def calibrate_pass1(lanes: Sequence[Lane], **kw) -> CalibrationPartial   # billed + documented sums, ρ counts
def calibrate_pass2(lanes: Sequence[Lane], rho_by_fold: Mapping[int, Mapping[str, Decimal]], **kw
                    ) -> CalibrationPartial                              # out-of-fold calibrated sums
def finish_calibration(parts1: Sequence[CalibrationPartial], parts2: Sequence[CalibrationPartial], *,
                       granularity: str, folds: int) -> CalibrationReport
```

`calibrate` streams two passes over the lane batches (the pipeline passes store shards; tests pass `lambda:
[lanes]`); partial results merge by addition, so the report is identical for any batching. For
convenience `calibrate_lanes(lanes, **kw) = calibrate(lambda: [lanes], **kw)`.

1. Transitions from `classify_transitions`. Only `i ≥ 1` with `predicted_hit is not None` participate.
2. **Prediction without the transition's own reads:** predicted reads `R̂_i = E_i` if `predicted_hit` else
   `S`; `Ŵ_i = T_i − U_i − R̂_i` in the observed write bucket of request `i` (or `τ_i`'s bucket when `i` had
   no writes); `U_i`, `O_i` unchanged. Price predicted and billed usage (points); aggregate per period `t`
   (UTC day or month) into `s_t` (predicted) and `b_t` (billed).
3. `NMBE = 100·Σ(b_t − s_t) / ((n − p)·mean(b))`, `CV(RMSE) = 100·sqrt(Σ(b_t − s_t)² / (n − p)) /
   mean(b)`, `p = 0`. Thresholds (FEMP Table 4-2 from ASHRAE Guideline 14): month `|NMBE| ≤ 5`,
   `CV ≤ 15`; day `|NMBE| ≤ 10`, `CV ≤ 30` (the daily row reuses the hourly thresholds, a documented choice).
   `n < 12` periods → `status = "insufficient_data"` (projections UNCALIBRATED).
4. **ρ per gap band** `[0, 60 s) [60 s, 300 s) [300 s, 3600 s) [3600 s, ∞)`: over transitions predicted
   hit and not ambiguous, `ρ = hits / trials` where hit = not a miss event; Wilson 95% interval; bands with
   < 30 trials use the pooled ρ. (Pass 1 collects hits/trials per fold and band.)
5. **Calibrated variant with day-level cross-validation:** days are assigned to `folds` folds by
   `day_ordinal mod folds`; ρ is fitted on the other folds (from pass-1 counts) and applied to held-out days
   in pass 2: `ŝ = ρ·cost(R̂=E) + (1−ρ)·cost(R̂=S)` for predicted hits. NMBE/CV(RMSE) on the out-of-fold
   predictions.
6. **Status:** `pass` if the documented variant passes (mode `documented`), else if the calibrated variant
   passes (mode `calibrated`), else `fail`. `CalibrationReport.calibration()` is `CALIBRATED` iff `pass` —
   the only way any figure becomes CALIBRATED.
7. **Diagnostics confusion matrix:** for transitions carrying a `CacheDiagnostic` (Anthropic
   `cache-diagnosis` beta or OpenAI `prompt_cache_diagnostics`), cross-tabulate Token Bill's cause against
   the canonical server reason. Precision/recall classes: `model_changed ↔ model-switch`, `tools_changed ↔
   tools-changed` (and block-level tool divergences when fingerprints exist), `system_changed ↔
   system-changed` (and volatile/system-edit divergences), `messages_changed ↔ messages-changed` (and
   history-rewrite divergences), `param_changed ↔ param-change` (OpenAI reasoning_effort / text_format /
   verbosity / service_tier). `compacted` and `compaction ↔ messages_changed|system_changed` count as an
   expected rebuild, not an error. `previous_message_not_found` and `unavailable` are counted in
   `no_comparison_labels`; `ttl-expiry` predictions labeled `previous_message_not_found` are reported as TTL
   corroboration; `key_changed` is reported separately (no Token Bill equivalent). Precision/recall is
   reported per provider when both are present.
8. The as-is replay identity (§9.1 #2) is **not** part of this gate.

### 9.7 Block-level replay (`sim/block_replay.py`, BLOCK)

`BlockReplayer` implements `Replayer` for policies with `breakpoint_policy` or `repairs` prefixed `block:`;
lanes without fingerprints are skipped with a reason (callers fall back to usage-level replay).

1. **Tier-salted chain hash.** `H_tools = H(model, salt(tools-tier params), tool blocks…)`; `H_sys =
   H(H_tools, salt(system-tier params), system blocks…)`; `H_msg[k] = H(prev, salt(messages-tier params),
   block_k)`. Tier params per `CacheRules.tier_params` (tools: model/tool defs; system: speed, web search,
   citations; messages: tool_choice, disable_parallel_tool_use, image presence, thinking, effort, output
   format; thinking/effort also salt tools+system on models listed in `effort_invalidates_all_tiers_models`).
   **Effort and thinking are left out of the messages salt when `core.cache_rules.effort_change_keeps_cache`
   is True for the request** (D28) — the same predicate as the usage level, so the engines agree. The
   invalidation hierarchy thus becomes plain hash inequality. Block hashes exclude `cache_control`.
2. **Entries** keyed `(cache_scope_key, model, chain_hash_at_block_k)` with `written_ms`, `visible_ms`
   (`ts_start + ttft`, else `ts_start + duration`, else `ts_start + 1,000` — same-timestamp siblings never
   read each other), `expires_ms = ts_start + ttl`, refresh on read `expires = max(expires, reader.ts_start
   + ttl)`.
3. **Breakpoints:** observed markers (≤ 4; automatic caching uses one slot; `assumed` end-of-messages markers
   from trace@1 counts are treated as observed, §5.5) or the policy's placement: `end` (end of messages every
   call — v0.1's optimum), `static_plus_end` (end of the static prefix + end), `every_15` (static + every 15
   collapsed positions + end; lookback-safe). For each breakpoint, look back at most 20 collapsed positions
   (counting the breakpoint position as the first) for the longest live, visible entry; tokens up to the hit
   are reads; from the hit to the last breakpoint are writes at that breakpoint's TTL (mixed TTLs must be
   longest-first, else flagged); after the last breakpoint uncached; a breakpoint whose prefix is below the
   model minimum writes nothing.
4. **Token sizes:** block `est_tokens` rescaled per request so Σ = billed `total_input` (the v0.1 honesty
   invariant); predicted `(R, W)` per request; `agreement = Σ predicted reads / Σ billed reads` when billed
   reads > 0. The dual-engine agreement check (§10.4) uses `predict(lanes, placement="end")`.
5. **First divergence** per request: (tier, block index, cause from hash comparison: `h` differs & `h_norm`
   equal → volatile; `h` differs & `h_sorted` equal → serialization; tier salt differs → param; tools-tier
   hash multiset equal but order differs → tool order; …). Billed-vs-predicted disagreement **without** a
   hash divergence is reported in the `agreement` metric only, never as a breaker.

### 9.8 Oracle and differential testing (SYNTH-ORACLE)

`synth/oracle.py: ReferenceReplay` is a deliberately simple, readable per-request implementation of
§9.2–§9.3 (TTL two-way, keepalive, compaction window, cold resume, model remap, effort, fast_off,
geo_global, batch, restore_caching, stagger_fanout) using `Pricer.price_usage` (Decimal path) only. The fast
engine must agree with it **to the nano** (points and bounds) on ≥ 500 seeded random lanes per policy family
(`tests/v2/synth_oracle/test_differential_replay.py`, a merge-gate test that skips until
`tokenbill.sim.usage_replay` is importable). Both must reproduce every hand-computed fixture of Appendix A.
SYNTH-FLEET's per-plant truth is computed by closed forms at generation time and cross-checked against the
oracle in a gate test (§18).

---

## 10. Detectors

### 10.1 Framework

- A detector implements `core.protocols.Detector` at the dotted path in `BUILTIN_DETECTORS`, is pure
  (lanes + context in, findings out; no I/O), declares `requires` and `kinds`, and is run through
  `core.registry.run_detectors` (which emits exactly one `missing-capabilities` data-quality finding when
  requirements are unmet — no guessing).
- **Cohorts and shards.** Every cross-lane computation is confined to a cohort `core.findings.cohort_key(lane)
  = (team, lane_kind, billing_class)` (or a finer key inside it, e.g. `(scope, model, cwd_key)` for fan-out,
  principal for sticky/heterogeneity counts). Consequently running a detector per shard (§3.21) and
  concatenating equals one run over all lanes; `assert_detector_conforms` tests this. Findings aggregate at
  `(team, lane_kind, kind[, model])` plus `billing_class` when it is `allowance`; `finding_id` is
  `core.findings.finding_id(detector_id, kind, scope)` (no evidence ref, so it is shard-independent).
- Money: `cost_observed` is EXACT only when it is billed arithmetic (e.g. rewrite cost actually paid);
  premiums relative to a hypothetical (warm read, standard speed) are ESTIMATED **except** pure rate
  arithmetic on identical billed tokens (fast/geo/regional premiums, same-tier repricing arithmetic),
  which is EXACT. `recoverable` comes from `ctx.replayer` with the linked lever policy (standalone), or a
  stated formula; None when there is no mechanical repair.
- **Allowance cohorts (D26):** in a cohort whose billing class is `allowance`, every figure carries basis
  `LIST_EQUIVALENT`, the title starts with "Allowance headroom:" and the summary states "list-equivalent,
  not invoice dollars". Detectors never add allowance and billed figures.
- Thresholds: miss rule §3.15; `min_usd` default $1.00 over the window (`ctx.thresholds["min_usd"]`); a
  detector never emits a finding whose recoverable p50 (or, for triage/info kinds, cost_observed) is below
  `min_usd` unless configured. All other thresholds below are defaults overridable by
  `ctx.thresholds["<detector id>.<name>"]`.
- Publication: org findings pass `core.kanon.rescope_findings` with the store-backed `count_users`; self
  findings only with `ctx.self_principal`. Findings reference the research ids given below.
- **Healthy-control guard:** the fleet demo's control team yields no finding with recoverable p50 ≥
  `min_usd` (false-positive gate, §18).
- Rates in formulas: `r` read, `w5`, `w1`, `wτ` (write at TTL τ), `u` uncached input, `o` output, in nano
  per token from `pricer.unit_rates` for the request's context (`core.findings.rate_nano`).

### 10.2 Usage-level detectors (DETECT-CACHE: `cache_miss`, `cache_ttl`, `cache_structure`; DETECT-OTHER: the rest)

| id / kinds | requires | trigger | cost_observed | recoverable / lever | fix | refs |
|---|---|---|---|---|---|---|
| `cache.miss-by-cause`: ttl-expiry, model-switch, param-change, compaction, tools-changed, system-changed, messages-changed, context-shrank, unexplained | usage_sequence, timing | every miss event, cause per §3.15 | rewrite actually billed: `mw·w_billed + mu·u` with `(mw, mu) = core.findings.miss_waste` — EXACT | triage premium `mw·(w − r) + mu·(u − r)` ESTIMATED, `upper_bound`; `compaction` kind has none; levers linked per cause (`core.catalog.levers_for_kind`) | per cause (TTL policy, fallback credit, pin params, stable tools/system) | `cc-miss-taxonomy-ground-truth`, `cc-usage-likely-cause`, `anth-invalidation-hierarchy` |
| `cache.switch-churn`: refusal-fallback-no-credit, availability-ping-pong, plan-toggle, user-model-switch, fast-toggle, effort-change | usage_sequence, timing | miss events with cause model-switch or param-change (sub-causes §3.15; effort-change only when `effort_change_keeps_cache` is False, D28) | rewrite billed EXACT | refusal-fallback: replay `repair=fallback_credit` (free win); fast-toggle: `fast=off`; plan-toggle, user switch and effort change: none (trade-off) | fallback credit beta / same-family fallbacks; switch models at `/clear` or via a subagent; plan-toggle: "opusplan switches models on every plan-mode toggle and rebuilds the cache — prefer one model or the Plan subagent"; `fastModePerSessionOptIn`; per-message effort (beta; Fable 5.1, Mythos 5.1, Opus 5, Opus 5.5) | `cc-model-switch-cost`, `fp-fallback-credit`, `cc-cache-breakers`, `cc-model-effort-mix` |
| `cache.rebuild`: compaction-cold, edit-churn | usage_sequence, timing, events (compaction-cold) / attempts or events (edit-churn) | compaction-cold: COMPACTION event whose preceding request started > τ earlier. edit-churn: a CONTEXT_EDIT event or `applied_edits` (cleared `X` = Σ `cleared_input_tokens`) on request `i` while warm (`gap_i ≤ τ`); `S_i = W_i` (tokens after the edit point, rewritten); `K_rem` = later requests of the lane before the next reset; `K* = S(α − β)/(Xβ)` with α = w/u, β = r/u (`history-rewrite-kstar`) | compaction-cold: compaction input at write rate (estimated line). edit-churn: rebuild billed `S_i·w_billed` — EXACT | compaction-cold: `pre_tokens·(wτ − r)` ESTIMATED. edit-churn: net loss `max(0, S_i·(w − r) − X·r·K_rem)` per edit, ESTIMATED, summed per cohort; `K*` and `K_rem` distribution in evidence; lever class hygiene | compact while warm; `/clear` when cold. Edit churn: `clear_at_least` so each clear removes enough to pay back within the typical remaining calls, a higher trigger, fewer larger clears, prune at task boundaries (context editing is a context-window tool, not a savings lever) | `compaction-timing`, `anth-compaction-iterations-billing`, `context-editing-cost`, `anth-context-editing-cache`, `anth-context-editing-not-savings` |
| `cache.cold-resume`: cold-resume | usage_sequence, timing, ttl_split | MAIN lane, cause ttl-expiry, `W_i ≥ 0.5·P_{i−1}`, `P_{i−1} ≥ 100,000` | rewrite billed `min(W_i, P_{i−1})·w_billed` — EXACT | premium vs warm read `min(W_i,P_{i−1})·(w − r)` ESTIMATED; levers `cc.cold_resume_hook` (behavioral, not projected), `cc.compact_on_resume` (Policy cold_resume, trajectory), `cc.prompt_cache_ttl.main` | SessionStart hook; `/compact` (same task) or `/clear` (new task) before stepping away | `cc-cold-resume`, `compaction-timing`, `channel-code-review-hooks` |
| `cache.ttl-advisor`: ttl-1h-recommended, ttl-5m-recommended, keepalive-recommended, ttl-heterogeneous | usage_sequence, timing | per cohort (team, lane kind ∈ main/subagent/workflow_agent/api_run, billing_path): replay observed, `ttl=5m`, `ttl=1h`, and keepalive(240 s, 3,600 s) only when `agent_product != claude_code`; recommend the argmin **different from the observed TTL** when saving ≥ max(`min_usd`, 2% of cohort spend) | cohort spend EXACT (LIST_EQUIVALENT for allowance cohorts) | saving of the recommended policy (ESTIMATED, calibration from ctx); evidence: gap histogram `[0,1m) [1,5m) [5,10m) [10,30m) [30,60m) ≥60m`, Anthropic 1-in-20 rule verdict (gap share in (5, 60] min > 5% ⇒ 1h) and agreement, keepalive break-even `κ(w/r − 1)` | `promptCacheTtl` / `subagentPromptCacheTtl` (Claude Code; VERIFY value format), SDK keepalive snippet; if < 60% of the cohort's principals (≥ k) are individually cheaper under the recommendation, emit `ttl-heterogeneous` recommending per-cohort (MDM group) delivery, counts only; gateway note "forward anthropic-beta; the Claude apps gateway cannot use 1h" | `cc-ttl-advisor`, `cc-ttl-policy`, `anth-ttl-choice-keepalive`, `keepalive-economics`, `cc-apps-gateway-routing-tax` |
| `cache.gateway-disabled`: no-cache, beta-header-dropped, tool-search-disabled | usage_sequence | (a) lane ≥ 5 requests, median T ≥ max(min_cacheable, 4,096), Σ reads = Σ writes = 0; (b) cohort configured for 1h (`thresholds["policy.ttl.<team>"]`) but ≥ 20 requests with 5m writes only; (c) Claude Code behind a non-first-party base URL (`attribution.extra["gateway"]`) with ≥ 3 tools-changed/param misses per 100 requests | (a) billed uncached input `ΣU·u` EXACT; (b)(c) miss rewrites EXACT | (a) replay `repair=restore_caching`; (b)(c) triage premium; lever `gateway.restore_caching` / `cc.tool_search` | forward `cache_control` and `anthropic-beta` unchanged; do not flatten system blocks; LiteLLM `cache_control_injection_points`; `ENABLE_TOOL_SEARCH=true` | `cc-gateway-marker-stripping`, `gateway-strip`, `cc-gateway-cache-strip` |
| `cache.unread-write`: write-never-read, oversized-ttl, tail-writes (info) | usage_sequence, timing | request i (not last) with `W_i > 0` whose next request has `gap ≤ τ` and `R_j < 0.95·(R_i + W_i)`; single-request lanes that write; 1h writes never followed by a request between 300 s and 3,600 s | write premium over uncached `W·(wτ − u)` ESTIMATED; oversized-ttl `W1·(w1 − w5)` EXACT arithmetic | block placement lever when fingerprints exist; else guidance only | breakpoint at end of the shared portion; don't cache one-shot traffic; 5m for bursty lanes | `write-without-read`, `oai-cache-write-waste` |
| `cache.cold-fanout`: cold-fanout | usage_sequence, timing | ≥ 2 lane-first requests in the same (scope, model, cwd_key) within 10 s, each `W ≥ 1,024`, `R < 0.5·T` | `(N−1)·P·(w − r)`, P ∈ [min, median] of first-call writes — ESTIMATED range, `upper_bound` | replay `repair=stagger_fanout` | send one, await first token, then N−1 | `anth-concurrency-fanout`, `cc-agent-spinup-fanout` |
| `context.size-tax`: context-tax (info) | usage_sequence | always, per (team, lane kind) | exact decomposition `Σ max(0, R_i − X)·r + Σ max(0, min(W_i, T_i − X))·wτ` for X ∈ {100k, 200k, 400k}; context p50/p90; share of calls and $ at ≥ 200k / ≥ 400k — EXACT | none (links `cc.autocompact_window`) | — | `cc-context-size-driver`, `context-tax-residency` |
| `context.compaction-window`: compaction-window | usage_sequence, timing | MAIN lanes of 1M-context models with max T above the smallest grid value | — | replay grid w ∈ {200k, 300k, 400k, 500k, 700k}; guard: projected extra compactions per session ≤ 3 (config); pick best RR-adjusted p50 with w ≥ `min_compaction_window` (default 300k); full curve in evidence; trade-off, `needs_eval`, `upper_bound` | `autoCompactWindow` **and** `env.CLAUDE_CODE_AUTO_COMPACT_WINDOW` | `cc-compaction-threshold-sim`, `cc-autocompact-window` |
| `context.static-prefix`: static-prefix (info), tool-defs-bloat | usage_sequence (+ `blocks` for tool-defs-bloat) | static-prefix: per (team, lane kind, model, scope) with `S = static_prefix_floor[(scope, model)]` known. tool-defs-bloat: requests whose tools-tier `est_tokens` of non-deferred tool definitions exceed 10,000 (`TOOL_DEFS_DEFER_THRESHOLD_TOKENS`), rescaled to billed `total_input`, with no deferred tools | static-prefix: harness cost `Σ min(S, R_i)·r + Σ_{lane-first or miss} min(S, W_i)·wτ` — ESTIMATED (S is estimated); share of cohort spend. tool-defs-bloat: tool-definition tokens × billed read/write mix — ESTIMATED | static-prefix: none (attribution; links `cc.tool_search`). tool-defs-bloat: `tokens_def × band × (r·reads + wτ·writes)` with band `TOOL_SEARCH_REDUCTION_BAND` [0.50, 0.85], point 0.70, ESTIMATED `upper_bound`; lever `cc.tool_search` / `sdk.defer_loading` | tool search / `defer_loading: true` for ≥ 10 tools or > 10k definition tokens; `ENABLE_TOOL_SEARCH=true` behind non-first-party gateways; fewer always-on MCP servers; `skillListingBudgetFraction`, `claudeMdExcludes` | `anth-tool-search-defer`, `tool-schema-bloat`, `static-prefix-compression`, `token-not-cost` |
| `attrib.carry`: tool-output-carry, config-tax | usage_sequence, appended (+events for config-tax) | tool_result appended items; first CONTEXT_INJECTION of each type per lane | tokens `t = ceil(n_bytes / cpt)` (cpt: `core.findings.fit_cpt`, n ≥ 30 per tokenizer family, else 2.5 / 3.3); carry `t·wτ + t·r·(later requests until reset)` ESTIMATED, aggregated by (team, tool) with the top-5% share | none (trajectory; verify with `ab`) | `bashOutputMaxChars`, `MAX_MCP_OUTPUT_TOKENS`, PostToolUse filter hooks; `skillListingBudgetFraction`, `claudeMdExcludes`, fewer always-on MCP servers | `cc-tool-output-carry`, `cc-config-sprawl-tax`, `verified-savings-gap-rtk` |
| `premium.modifiers`: fast-premium, geo-premium, regional-premium, tier-premium | usage_sequence | inferences with speed fast, inference_geo us, regional endpoint scope, priority/fast service tier | premium = cost − cost at standard/global/standard-tier on identical tokens — EXACT | same amount, EXACT, lever class rate (`fast=off`, `geo=global`, `regional=global`); flagged when config `residency_required` covers the scope | `fastModePerSessionOptIn`, `CLAUDE_CODE_DISABLE_FAST_MODE`; global endpoints where policy allows | `anth-modifiers-geo-fast-priority`, `premium-modifiers`, `oai-service-tiers` |
| `premium.sticky-escalation`: sticky-escalation (self), sticky-escalation-count (org) | params | on MAIN lanes, a principal's `session_effort` above org default (`thresholds["defaults.effort"]`) or fast mode on > 5 distinct days | self: own fast premium EXACT; org: count of such users per team only when ≥ k | none | session-only `/effort`; `fastModePerSessionOptIn` | `cc-sticky-escalation` |
| `model.routing`: delegation-routing, same-tier-upgrade, default-model, default-effort, effort-mix (info), rebaseline (info) | usage_sequence (+ params for default-effort / effort-mix) | delegation: SUBAGENT/WORKFLOW_AGENT lanes on Opus/Fable/Mythos. same-tier: models with a `core.catalog.successor`. **default-model**: Claude Code MAIN lanes where Opus/Fable/Mythos-class models serve ≥ 50% of the cohort's main spend. **default-effort**: Claude Code MAIN lanes where ≥ 50% of main output ran at effort ≥ high on an effort-capable model. **rebaseline**: a cohort whose dominant serving model changed (≥ 50% of its requests move to a new model within 7 days); compares the 14 days before vs after (≥ 50 requests each). effort-mix always | spend of affected lanes EXACT; effort-mix: spend share by effort, thinking share; rebaseline: Δ tokens/request (input T, output O, reasoning) and Δ$/request at current rates — ESTIMATED (observational) | delegation: replay `model=claude-sonnet-5@lane_kind:<kind>` (Explore agents also `claude-haiku-4-5`) with band, trade-off, `needs_eval`, "price-only"; same-tier: replay `model=<successor>@…` — rate arithmetic exact on identical tokens, figure ESTIMATED ("behavior unvalidated"), `needs_eval`; **default-model**: replay `model=claude-sonnet-5@agent_product:claude_code,lane_kind:main` with band (lever `cc.default_model`, trade-off, `needs_eval`, gate through `ab`/`measure`); **default-effort**: replay `effort=medium,scale=0.5@agent_product:claude_code,lane_kind:main` (lever `cc.default_effort`, trade-off, `needs_eval`, `upper_bound`); rebaseline: none (behavioral) | `env.CLAUDE_CODE_SUBAGENT_MODEL`, per-agent `model:`; pin successor ids; default-model: managed `model` / `availableModels` (VERIFY keys, trade-off); default-effort: `effortLevel` (VERIFY) or `maxEffortLevel: "medium"`; rebaseline: "tokenizer change expected ×1.0–1.35 on input; thinking on by default on Opus 5; output per call up > 20% suggests stale prompts — audit them (a support-desk audit cut cost 14%)"; retirement warnings | `cc-delegation-model-routing`, `cc-same-tier-upgrade`, `anth-tokenizer-inflation`, `org-defaults-datadog`, `datadog-1m-month-case`, `cc-model-effort-mix`, `anth-prompt-audit-migration`, `opus5-thinking-effort-rebaseline`, `tokenizer-inflation-47plus` |
| `failure.path`: cold-retry, retry-storm, never-succeeding-400, tool-error-loop, max-tokens-truncation | usage_sequence, timing (+attempts) | cold-retry: failed attempt / API_ERROR followed by success starting > τ after the first attempt and writing ≥ 0.5 of the prefix; storm: > 3 attempts per request or attempts at ≥ 2 retry layers (SDK attempts from recorder hooks count, D35); never-succeeding: same error_type ∈ {prompt_too_long, thinking_binding, spend_cap, invalid_request} ≥ 2 times without success, or a retry after `should_retry=false` or a spend-cap 429; tool-error loop: ≥ 3 consecutive requests with `is_error` tool results; **max-tokens-truncation**: ≥ 3 attempts with `stop_reason == "max_tokens"` in the cohort | billed cost of the affected attempts (EXACT; billing-rule ranges when unknown); truncation: billed cost of truncated attempts (rule `anthropic.max_tokens` / `openai.max_output_tokens`, documented) — EXACT | cold-retry: replay `repair=retry_backoff_cap`; truncation: cost of truncated attempts followed within 120 s by a same-lane request with `T ≥ 0.95·T_trunc` (a retry/continuation), ESTIMATED `upper_bound`, lever class hygiene; others triage | one retry owner; backoff capped below TTL minus generation time (the SDK now honors any `retry-after`); 1h TTL for watchdog/CI runs; strip thinking blocks; PreToolUse guards; truncation: raise `max_tokens` to 64k for agentic work (128k at xhigh/max), stop sequences as early exits, output-shape prompts | `fp-ttl-from-request-start`, `fp-nested-retry-amplification`, `fp-never-succeeding-400`, `fp-agent-loop-prevalence`, `fp-sdk-retry-after-uncapped`, `max-tokens-truncation`, `anth-output-hygiene` |
| `automation`: ci-cross-run, scheduled-cadence, batch-eligible, ci-run-cost (info) | usage_sequence, timing | ci-cross-run: workload ci (or entrypoint `claude-code-github-action`) lanes whose first request writes ≥ 0.8·T and reads < 10%, ≥ 2 runs in a scope starting within τ; scheduled: ≥ 4 requests at near-constant intervals (CV of gaps < 0.25), no human prompts, interval > τ; batch: §9.3.5 predicate; ci-run-cost: per (repo `h_`, `extra.workflow`) CI sessions | first-call rewrites EXACT; eligible spend EXACT; ci-run-cost: $ per run p50/p90 EXACT beside the published $15–25 per Anthropic Code Review benchmark (with source), and the team's CI $ vs interactive $ in the window | `repair=shared_ci_prefix` (upper bound); scheduled: TTL/keepalive replay; `batch=eligible` range; ci-run-cost: none | `--exclude-dynamic-system-prompt-sections`, `--bare` with an explicit `--append-system-prompt-file`, pinned CLI version, one CI workspace, 1h TTL for 5–60-min gaps; event triggers; Message Batches / flex; review once after PR creation instead of every push, `concurrency` with cancel-in-progress, `paths` filters | `ci-cross-run-cache`, `sched-cadence-ttl`, `anth-batch-stacking`, `ci-headless-ingest`, `ci-review-unit-costs` |
| `tail.runaway`: runaway-session, idle-loop | usage_sequence, timing (+human_prompts for idle-loop) | session rolling-1h exact $ > max($50, 5 × cohort p99 hourly session cost); ≥ 50 requests with no HUMAN_PROMPT for ≥ 1 h | session $ above the cohort p95 session cost — EXACT | none | existing gateway/Console limits (documented, not enforced); call limits | `runaway-circuit-breaker`, `cc-heavy-tail-concentration` |

Tail findings name the team only; the session pseudonym appears only with `--break-glass` (audited).
Cohort statistics (p95/p99) are computed within the `(team, lane_kind, billing_class)` cohort, keeping the
detector shard-invariant.

### 10.3 Aggregate-level org scan (RECON, `recon/orgscan.py: OrgScan`, requires `aggregates`, D32)

Runs on `ctx.aggregates` / `ctx.cost_lines` alone (lanes ignored), so an admin with only Admin/Analytics
(and CUR) data gets findings on day one. Scope dims: `channel`, `workspace_id` (or `api_key_id` only when
explicitly requested and permitted by §8.5), `model`; no principals. Figures are EXACT when they are rate
arithmetic on provider-reported usage.

| kind | trigger | cost_observed | recoverable / lever | fix |
|---|---|---|---|---|
| `cache-read-share` | per scope, read ÷ total input below `CACHE_READ_SHARE_INVESTIGATE_BELOW` (0.80), with median 0.84 / top decile 0.94 benchmarks shown with sources | priced usage of the scope EXACT | triage: `(0.84·T − R)·(w5 − r)` ESTIMATED `upper_bound` (to the median benchmark) | install collectors / recorder for this scope to find the cause; check gateways |
| `write-read-thrash` | write tokens ÷ read tokens > 1 with read share < 0.80 | write spend EXACT | write premium over read `W·(w − r)` ESTIMATED `upper_bound` | as above; TTL and breakpoint review |
| `ttl-mix` (info) | 1h write share per scope; flags 1h-only scopes with ≥ 20% of spend in writes | write spend EXACT | none | TTL advisor after collection |
| `fast-premium`, `geo-premium`, `priority-share` | usage grouped by `speed`, `inference_geo`, `service_tier` | premium = cost − cost at standard/global/standard on identical provider tokens — EXACT | same amount, EXACT, lever class rate | as `premium.modifiers` |
| `batch-share` (info) | share of spend in batch tier per scope | EXACT | none | batch-eligibility review |
| `effective-discount` (info) | `1 − invoice/list` per model-day from reconciliation inputs | EXACT arithmetic on invoice data | none | `reconcile --suggest-contract`; emit `modelPricing` |

### 10.4 Block-level breakers (BLOCK, `detect/block.py: BlockBreakers`, requires `blocks`)

| kind | trigger | repair priced | fix text (gates) |
|---|---|---|---|
| `volatile-system` | system tier differs, `h_norm` equal, changed blocks carry volatile classes | replace the volatile block by its normalized twin | move the value to a mid-conversation `role:system` message (Opus 5/5.5/4.8, Fable, Mythos; **not Sonnet 5**) or the latest user turn |
| `system-edit` | system tier differs, not purely volatile | none (cost only) | pin system text; per-turn context in the latest user message |
| `serialization-churn` | `h` differs, `h_sorted` equal (tools or messages) | sort keys | `json.dumps(sort_keys=True)`; deterministic schemas |
| `tool-churn` (sub: order, subset, definition) | tools tier: same multiset different order / added-removed / changed definition | restore first-seen order / constant superset | sort by name; `defer_loading`/tool search; `tool_addition` (beta) where supported |
| `history-rewrite` | first divergence in messages at an index < previous message count, **excluding** marker moves (hashes exclude cache_control), compaction blocks, applied context edits, server `thinking_dropped` | none | append-only history; batch clears with `clear_at_least` |
| `param-churn` | tier salt changes from effort/thinking/tool_choice/speed/format/images (effort only when `effort_change_keeps_cache` is False) | pin the parameter | per-message effort (beta) where supported |
| `missing-breakpoint` | stable prefix ≥ min cacheable, **no** markers (explicit or assumed), no automatic caching, billed reads = writes = 0 | add an end-of-messages breakpoint | v0.1 fix text |
| `lookback-overflow` | predicted miss because > 20 collapsed positions separate a breakpoint from the last entry, confirmed by billed reads | intermediate breakpoint every ~15 positions | add an intermediate breakpoint |
| `breakpoint-placement` | a canonical placement policy (§9.7) is cheaper than observed | cheapest policy | move the breakpoint to the end of the shared prefix |
| `write-never-read` | entries written and never read before expiry | drop that breakpoint | explicit breakpoints / no caching for one-shot traffic |
| `fanout` | same prefix hash written by ≥ 2 requests overlapping `[ts_start, visible_ms)` | stagger | send one, await first token, then N−1 |

`recoverable = replay(observed) − replay(repair)`, floored at 0 with the v0.1 wording ("no recovery modeled —
billed caching already beats the simulated fix") when negative; None for kinds without a mechanical repair.
A breaker is emitted only where a hash divergence (or the placement/marker rule) explains it; billed misses
without a divergence are not breakers (§9.7 #5).

**Acceptance (dual-engine agreement):** the four v0.1 demo scenarios, read through `TraceV1Adapter` (whose
`cache_breakpoints` count > 0 becomes one assumed end-of-messages breakpoint, §5.5) and fingerprinted, give
exactly: `timestamp` → `volatile-system` at call 1 (and no other breaker kind); `tool-churn` → `tool-churn`
(order) at the rotation calls, no `volatile-system` and no `missing-breakpoint` (its count is 1); `no-cache`
(count 0) → `missing-breakpoint`; `well-behaved` → none, with `predict(placement="end")` reads equal to the v1
optimal-cache reads within rounding. Codebase-experiment regression fixtures (re-encoded deterministically in
`tests/v2/fixtures/blocksim/`): **exp1** (8-turn loop, marker moved to the newest block each turn, billed
usage of a working cache) → no history-rewrite, agreement ≥ 0.95; **exp2b-a3** (six independent requests
sharing a ~20k-char preamble, differing only in the final question) → `breakpoint-placement` saving ≈ 56% of
input $ (static_plus_end); **exp2b-a2** (five byte-identical requests at the same timestamp) → five predicted
writes, no phantom 65% saving, a `fanout` finding with an upper-bound saving; **exp9-1** (tools alternating
between two key orders, billed cold writes) → `serialization-churn`; **exp6-2** (each turn appends 25 text
blocks, billed full rewrites) → `lookback-overflow` with the every-15 fix. An effort change on Opus 5.5 under
Claude Code (exemption true) salts nothing; the same change from an SDK lane without the beta salts the
messages tier — matching `classify_transitions` on the same lanes.

---

## 11. Action plan and policy pack (PLAN; catalogs in `core.catalog`, F-KIT)

### 11.1 Lever catalog (`core/catalog.py: LEVERS`, data only, D29)

`LeverDef` is defined in §3.20. The catalog (verbatim in code):

| lever_id | class | grid (policy spec fragments) | selector | delivery (`patch_keys`) | needs_eval / tradeoff |
|---|---|---|---|---|---|
| `cc.prompt_cache_ttl.main` | cache_transform | `ttl=5m@…`, `ttl=1h@…` | `agent_product:claude_code,lane_kind:main` | `promptCacheTtl` | no / no |
| `cc.prompt_cache_ttl.subagent` | cache_transform | `ttl=5m@…`, `ttl=1h@…` | `agent_product:claude_code,lane_kind:subagent` (+ workflow_agent) | `subagentPromptCacheTtl` | no / no |
| `sdk.ttl` | cache_transform | `ttl=5m@…`, `ttl=1h@…` | `lane_kind:api_run` | SDK snippet (`cache_control.ttl`) | no / no |
| `sdk.keepalive` | cache_transform | `keepalive=240s,max=3600s@…` | `agent_product:agent_sdk` (and `api`) | SDK snippet | no / no |
| `cc.autocompact_window` | trajectory | `compact-window=` 200000 / 300000 / 400000 / 500000 / 700000 | `agent_product:claude_code,lane_kind:main` | `autoCompactWindow` + `env.CLAUDE_CODE_AUTO_COMPACT_WINDOW` | yes / yes |
| `cc.compact_on_resume` | trajectory (ceiling only) | `cold-resume=compact,min=200000`, `cold-resume=clear,min=200000` | `agent_product:claude_code,lane_kind:main` | behavior; delivered by the hook → **not projected** | yes / yes |
| `cc.cold_resume_hook` | behavioral | threshold $1 | same | SessionStart hook | no (never projected) |
| `cc.default_model` | trajectory | `model=claude-sonnet-5@…` | `agent_product:claude_code,lane_kind:main` | `model` (VERIFY), `availableModels`+`enforceAvailableModels` (trade-off) | yes / yes |
| `cc.default_effort` | trajectory | `effort=medium,scale=0.5@…`, `effort=low,scale=0.25@…` | `agent_product:claude_code,lane_kind:main` | `effortLevel` (VERIFY) or `maxEffortLevel` | yes / yes |
| `cc.subagent_model` | trajectory | `model=claude-sonnet-5@…`, `model=claude-haiku-4-5@…` | `lane_kind:subagent` (+ workflow_agent) | `env.CLAUDE_CODE_SUBAGENT_MODEL` | yes / yes |
| `model.same_tier_upgrade` | trajectory | `model=<core.catalog.successor(m)>@model:<m>` | per model with a successor | `env.ANTHROPIC_DEFAULT_*_MODEL` | yes / yes |
| `cc.max_effort` | trajectory | `effort=high`, `effort=medium` | `all` | `maxEffortLevel` | yes / yes |
| `cc.fast_mode_opt_in` | rate | `fast=off` | `all` | `fastModePerSessionOptIn: true` | no / no |
| `geo.global` | rate | `geo=global` | `all` | policy decision (no key); residency approval | no / no |
| `endpoint.global` | rate | `regional=global` | `all` | gateway/SDK config | no / no |
| `batch.eligible` | rate | `batch=eligible` | `all` | SDK snippet (Message Batches / flex) | no / no |
| `fanout.stagger` | cache_transform | `repair=stagger_fanout` | `all` | SDK snippet | no / no |
| `retry.single_owner` | cache_transform | `repair=retry_backoff_cap` | `all` | SDK/gateway snippet | no / no |
| `fallback.credit` | cache_transform | `repair=fallback_credit` | `all` | beta header snippet | no / no |
| `gateway.restore_caching` | cache_transform | `repair=restore_caching` | `all` | LiteLLM `cache_control_injection_points`; gateway checklist | no / no |
| `ci.shared_prefix` | cache_transform | `repair=shared_ci_prefix` | `workload:ci` | CI snippet | no / no |
| `blocks.breakpoints` | cache_transform | `breakpoints=static_plus_end`, `breakpoints=every_15` | lanes with fingerprints | SDK snippet | no / no |
| `cc.tool_search` | cache_transform | — (no replay; projection from `context.static-prefix` tool-defs-bloat, or None) | `agent_product:claude_code` | `env.ENABLE_TOOL_SEARCH=true` | no / no |
| `sdk.defer_loading` | cache_transform | — (projection from tool-defs-bloat) | lanes with fingerprints | SDK snippet (`defer_loading: true`) | no / no |

### 11.2 Building the plan (`plan/action_plan.py`)

```python
def build_action_plan(findings: Sequence[Finding], index: Sequence[LaneIndexRow],
                      load_lanes: Callable[[Collection[str] | None, ShardKey | None], Sequence[Lane]],
                      shards: Sequence[ShardKey], ctx: AnalysisContext, *, window_days: int,
                      include_tradeoffs: bool = False, seed: int = 0, sample_lanes: int = 20_000,
                      observed_rr: Mapping[str, tuple[str, int]] | None = None,
                      map_shards: Callable[[Callable[[ShardKey], object], Sequence[ShardKey]], list] | None = None
                      ) -> ActionPlan
    # load_lanes(lane_keys, None) returns the sample; load_lanes(None, shard) returns one shard's lanes.
    # map_shards runs a function over shards (the pipeline passes a process-pool mapper for --jobs;
    # default: sequential). Tests pass an in-memory loader built on core.shards.shard_of_lanes.
```

1. **Candidates:** levers linked by findings (`lever_ids`) plus the always-evaluated defaults (TTL levers,
   compaction window, fast/geo) whose selector matches some lane (`core.policy.lane_matches`). Trade-off
   levers are evaluated and shown, but enter the joint set and headline only with `include_tradeoffs`;
   behavioral levers never.
2. **Billing classes:** the plan runs once for billed lanes and once for allowance lanes (D26); allowance
   results go only to `allowance_headroom_monthly` and to `LeverResult`s with basis `LIST_EQUIVALENT`.
3. **Grid choice on a sample:** `core.shards.stratified_sample(index, n=sample_lanes, seed)`; for each lever,
   the grid value with the largest documented saving on the sample (subject to guards: compaction
   extra-compactions ≤ 3 per session; `w ≥ min_compaction_window`) is chosen; values with no saving drop the
   lever. No coordinate descent across levers (D16).
4. **Interaction groups:** levers whose selectors touch overlapping lane sets (on the sample) form a
   connected component; disjoint groups are additive.
5. **Shapley on the sample:** per group with k ≤ 6 levers, all 2^k combined policies (`core.policy.combine`)
   are replayed on the sample; `v(S) = saving(S)` in the gated mode (calibrated if the model gate passed,
   else documented and UNCALIBRATED); `core.shapley.shapley_exact`. k > 6: `shapley_mc` (200 seeded
   permutations) with SE.
6. **Full-scope scaling:** the selected joint policy (all chosen levers) is replayed once on the **full
   scope**, shard by shard (`map_shards`, merged with `core.shards.merge_replay`); each group's sample
   credits are rescaled with `core.shapley.scale_credits` so that Σφ over all groups equals the full-scope
   joint saving (groups keep their sample proportions). Standalone values are scaled by the same factor and
   labeled "sample-scaled". When the scope has ≤ `sample_lanes` lanes the sample **is** the full scope and no
   scaling happens. `ActionPlan.sample` records `"shapley on n/N lanes (seed s), scaled to full-scope joint
   replay"`.
7. **Monthly normalization:** `φ_i × 30 / window_days`.
8. **Realization-rate priors** (`plan/realization.py`, p10 / p50 / p90):

   | class | prior |
   |---|---|
   | rate | 1.0 / 1.0 / 1.0 |
   | cache_transform | 0.8 / 0.9 / 1.0 |
   | trajectory | −0.2 / 0.5 / 1.0 (the p50 is a design judgment; stated in the report) |
   | behavioral | not projected |

   ```python
   PRIORS: Mapping[str, tuple[Decimal, Decimal, Decimal]]
   def project(shapley: Figure, lever_class: str, *, window_days: int, upper_bound: bool,
               calibration: Calibration) -> Figure | None     # None for behavioral
   ```
   `projected_monthly_i` = ESTIMATED Figure, point `φ_i·RR_p50`, range `[φ_i·RR_p10, φ_i·RR_p90]`,
   calibration from the replay, `upper_bound` inherited. When the store holds ≥ 3 receipts for a class
   (`LedgerStore.receipts`), the observed mean realization rate is displayed beside the prior
   (`ActionPlan.observed_rr`; priors are not auto-updated, D15).
9. **Headline:** `Σ φ_i·RR_i(p50)` over billed-basis levers with range `[Σ φ_i·RR_i(p10), Σ φ_i·RR_i(p90)]`
   (comonotone bounds); CALIBRATED only if every included replay was. `allowance_headroom_monthly` is the
   same computation for allowance lanes and is never added to the headline. Standalone ceilings are never
   summed or shown as a total (R7). `ActionPlan.joint_saving` is the full-scope joint replay of the selected
   billed set.

Acceptance: the three-lever game (§3.16) gives exact credits and Σφ = v(ABC); scaling preserves Σφ = the
full-scope joint saving to the nano; a lever with the trajectory prior yields a range crossing zero and is
labeled so; the standalone sum is never rendered; disjoint groups add; allowance levers never enter the
headline; the TTL lever on the Appendix A.1 lane with Shapley alone equals its standalone saving; sharded and
unsharded plans are identical.

### 11.3 Policy pack (`plan/policy_pack.py`, `plan/litellm.py`)

```python
def build_policy_packs(plan: ActionPlan, findings: Sequence[Finding], *, target: str,
                       current: Mapping[str, object] | None, cohort_by: str | None,
                       include_tradeoffs: bool, contract: ContractOverlay | None,
                       model_pricing_emitter: Callable[[ContractOverlay], dict] | None = None,
                       min_client_version: str | None = None
                       ) -> list[PolicyPack]   # CLI passes rates.contract.to_model_pricing
def render_pack(pack: PolicyPack, out_dir: Path) -> list[Path]
def render_injection_points(models: Sequence[str], *, ttl: str | None = None) -> str   # litellm.py; YAML text
def validate_litellm_fragment(text: str) -> None                                       # stdlib mini-parser
```

- One pack per cohort (`--cohort-by team|mdm-group`; TTL heterogeneity findings force per-cohort packs).
- Keys come only from `core.catalog.ALLOWLIST` (§11.4); an unknown key raises `UsageError`. Keys whose
  `verified` flag is False are **never written into JSON**; they appear in `README.md` as commented guidance
  with "VERIFY against settings-reference before applying". A finding's `Fix.config_patch` keys are always
  allowlisted (DETECT validates against the same catalog), so a pack never rejects a finding's patch.
- `managed-settings.patch.json` = RFC 7386 JSON merge patch against `--current` (only changed keys; `env`
  keys nested under `"env"`); `rollback.patch.json` restores previous values (`null` for absent keys);
  `autoCompactWindow` is always paired with `env.CLAUDE_CODE_AUTO_COMPACT_WINDOW` (the `--autocompact` flag
  is not preempted by managed settings).
- Every entry carries its projection Figure, label, `needs_eval`, minimum Claude Code version and a rollout
  note (server-managed settings apply org-wide; per-group delivery via MDM/endpoint files or the Claude apps
  gateway per IdP group; an org-wide change can be measured only with `measure plan --design its`, MEASURED at
  best). `env.OTEL_RESOURCE_ATTRIBUTES` gets `tokenbill.arm=<lever>,tokenbill.wave=<n>` for verification.
- Trade-off keys (`cc.default_model`, `cc.default_effort`, `cc.max_effort`, `cc.subagent_model`,
  `cc.autocompact_window`, `model.same_tier_upgrade`) are excluded unless `--include-tradeoffs`, and their
  README entries require an `ab` or `measure` gate. `modelPricing` is emitted from `--contract` via
  `rates.contract.to_model_pricing` when that key is verified. Nothing is ever applied automatically.
- `litellm-config.patch.yaml`: `litellm_params.cache_control_injection_points: [{location: message, role:
  system}, {location: message, index: -1}]` per affected model (TTL key **VERIFY**), rendered as text and
  validated by the stdlib mini-parser.
- `hooks/tokenbill_session_start.py` copied from `plan/templates/`: stdlib only; reads the SessionStart
  hook JSON from stdin; when `prompt_cache_likely_expired` is true and `estimated_cache_write_usd ≥
  threshold` (default 1.00; env `TOKENBILL_HOOK_THRESHOLD_USD`) prints `{"systemMessage": "…$X.XX to
  rebuild the cache; /compact to continue this task or /clear for a new one"}`; frequency caps ≤ 1 per
  session and ≤ 3 per person per week via `~/.cache/tokenbill/hook_counts.json` (0600); never blocks, always
  exits 0, never logs content; malformed input → no output. (Hook field names **VERIFY**, §19.8 #12.)
- `README.md`: per key the projected monthly figure with label, needs-eval, rollback line, verification
  pointer (`tokenbill measure plan --lever …`), and the post-rollout check.

### 11.4 Settings allowlist (`core/catalog.py: ALLOWLIST`; `verified` flags from `core/facts.json`)

| key (target) | value domain | min version | verified | lever |
|---|---|---|---|---|
| `autoCompactWindow` | int 100000–1000000 | — | yes | cc.autocompact_window |
| `env.CLAUDE_CODE_AUTO_COMPACT_WINDOW` | same, string | — | yes | cc.autocompact_window |
| `env.CLAUDE_CODE_DISABLE_1M_CONTEXT` | "1" | — | yes | (proposed only) |
| `promptCacheTtl` | "5m" \| "1h" | 2.1.242 | key yes; **value format VERIFY** (priority item of the wave-0 facts task) | cc.prompt_cache_ttl.main |
| `subagentPromptCacheTtl` | "5m" \| "1h" | 2.1.242 | key yes; **value format VERIFY** | cc.prompt_cache_ttl.subagent |
| `env.CLAUDE_CODE_PROMPT_CACHE_TTL`, `env.CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL`, `env.ENABLE_PROMPT_CACHING_1H`, `env.FORCE_PROMPT_CACHING_5M` | TTL string / "1" | 2.1.242 | keys yes; **value formats VERIFY** | TTL levers (env delivery; gateway fleets) |
| `model` | model id or alias | — | **VERIFY** (managed default model) | cc.default_model |
| `effortLevel` | low\|medium\|high\|xhigh\|max | — | **VERIFY** (managed default effort) | cc.default_effort |
| `env.CLAUDE_CODE_SUBAGENT_MODEL` | model id | — | yes | cc.subagent_model |
| `env.ANTHROPIC_DEFAULT_OPUS_MODEL` / `_SONNET_MODEL` / `_HAIKU_MODEL` | model id | — | yes | model.same_tier_upgrade |
| `availableModels`, `enforceAvailableModels` | list, bool | — | yes (trade-off) | cc.default_model |
| `maxEffortLevel` | low\|medium\|high\|xhigh\|max | 2.1.267 | yes | cc.max_effort, cc.default_effort |
| `fastModePerSessionOptIn` | bool | — | yes | cc.fast_mode_opt_in |
| `env.CLAUDE_CODE_DISABLE_FAST_MODE` | "1" | — | yes | cc.fast_mode_opt_in |
| `modelPricing` | contract overlay | 2.1.242 | **VERIFY shape** | reporting accuracy |
| `bashOutputMaxChars` | int | — | yes | attrib.carry fix |
| `env.MAX_MCP_OUTPUT_TOKENS` | int | — | yes | attrib.carry fix |
| `env.ENABLE_TOOL_SEARCH` | "true" | — | yes | cc.tool_search |
| `skillListingBudgetFraction`, `claudeMdExcludes` | decimal 0–1, list | — | yes | config-tax / static-prefix fix |
| `cleanupPeriodDays` | int ≥ collector period + 7 | — | yes | collector enabler |
| `env.CLAUDE_CODE_ENABLE_TELEMETRY`, `env.OTEL_RESOURCE_ATTRIBUTES`, `env.OTEL_METRICS_INCLUDE_ENTRYPOINT` | "1" / string / "true" | — | yes | verification tags |
| `hooks.SessionStart` | command → `hooks/tokenbill_session_start.py` | — | **VERIFY shape** | cc.cold_resume_hook |

### 11.5 Post-rollout effectiveness check (`plan/effectiveness.py`)

`check_effect(lanes_after: Sequence[Lane], *, lever_id: str, cohort: str, since_ms: int, days: int = 7) ->
Finding | None`: for TTL levers, the share of `cache_write_1h` among main-lane write tokens of the cohort
within `days` after rollout must exceed 90%, else a finding `setting-not-effective` ("gateway strips the
anthropic-beta header? Claude apps gateway cannot use 1h? client below minimum version?"); for
`cc.autocompact_window`, the median COMPACTION `pre_tokens` must be ≤ window × 1.05; for `cc.default_model`,
the share of main-lane requests on the target model must exceed 80%; for `cc.default_effort` /
`cc.max_effort`, the share of main-lane requests at or below the level must exceed 80%. Exposed as
`tokenbill policy check-effect`.

---

## 12. Reconciliation: the ledger gate (RECON, `recon/`)

```python
def reconcile(ledger: Iterable[UsageRecord], aggregates: Sequence[UsageAggregate],
              cost_lines: Sequence[CostLine], pricer: Pricer, *, tolerance_pct: Decimal = Decimal("0.5"),
              unexplained_pct: Decimal = Decimal("1.0"), closed_only: bool = False, today: str,
              suggest_contract: bool = False,
              rerun_pricer_factory: Callable[[ContractOverlay], Pricer] | None = None,
              rounding_remainders: Mapping[str, Decimal] | None = None) -> ReconciliationReport
    # `ledger` is streamed (the store yields UsageRecords); memory is bounded by the number of
    # (channel, date, workspace, model, bucket, tier) keys, not by requests.
COST_TYPE_MAP: Mapping[tuple[str | None, str | None], str]      # recon/costmap.py: (cost_type, token_type) → bucket
# CUR usage-type / GCP SKU rules: core.catalog.SKU_RULES and core.catalog.map_sku (§3.20), shared with ADMIN
def classify(rows: Sequence[ReconRow], *, ledger_estimated_nano: Mapping[tuple, int],
             allowance_nano: Mapping[tuple, int], provisional_dates: frozenset[str],
             remainders_nano: int) -> tuple[tuple[tuple[str, int], ...], int]    # recon/residuals.py:
             # (residual code → nano, unexplained nano), applying §12.3 in order
```

### 12.1 Three layers (per channel)

Every layer runs **per channel** (`anthropic_api`, `bedrock`, `vertex`, `openai_api`, …) with that
channel's invoice source: Anthropic `cost_report` / Enterprise Analytics cost, AWS CUR 2.0, GCP billing
export, OpenAI costs.

1. **Rate-card check (independent of our traces).** Price the provider usage tokens (usage report / CUR
   usage amounts / billing-export usage) with our rate card per (date, workspace, model, bucket,
   service_tier, inference_geo, speed, endpoint_scope) and join to invoice lines per (date, workspace, model,
   bucket) through `COST_TYPE_MAP` / `core.catalog.map_sku` (unknown pairs → residual `unmapped_cost_type`). Rate-card error
   per model-day = (priced − invoice) / invoice; within tolerance if |error| ≤ `tolerance_pct` (0.5%) or
   |priced − invoice| ≤ $0.01 × rows (cent rounding in the source). This separates price errors from
   coverage errors.
2. **Token coverage.** Ledger tokens vs provider usage tokens on the finest shared key (date, model, and
   workspace/API key/account when the ledger has them). Ledger > provider + max(1%, 1,000 tokens) is an
   **over-count row** (import bug or double count) and fails the channel's verdict.
3. **Dollar coverage.** Ledger dollars (exact + estimated points; allowance excluded) ÷ invoice dollars per
   model-day and in total.

**Channel-total mode.** When a channel's invoice lines cannot be mapped to (model, bucket) because the
cost-type/SKU rules they need are unverified, layer 1 is skipped for those lines and the channel is
reconciled on **channel-day totals**: token coverage on total tokens, dollar coverage of the ledger priced at
list × (1 − the channel's own effective discount, e.g. CUR `1 − net_unblended/unblended`) against the invoice.
The channel can then be `reconciled` with `mapping_verified = False` ("totals only; schema unverified").

### 12.2 Effective discount and contract suggestion

Effective discount = 1 − invoice / priced_list per channel, model and bucket (Enterprise Analytics `1 −
amount/list_amount`; CUR `1 − net_unblended/unblended` where both exist). With `--suggest-contract OUT.json`,
a `ContractOverlay` (per-model multipliers rounded to 4 decimals, `derived=True`, `channels` set) is written
and the reconciliation is **re-run** with it; if the rate-card error is then within tolerance the report
shows `rerun_verdict: reconciled` on CONTRACT basis, otherwise "discount is not a simple multiplier" and
exit 3.

### 12.3 Residual classifier (`recon/residuals.py`, applied in order, each with nano and a code)

`priority_excluded_from_cost_report` (usage-report `service_tier=priority` tokens, priced at burn-down
rates) · `code_execution_cost_report_only` · `revision_window` (dates within 30 days of `today` for revisable
sources → provisional; excluded under `--closed-only`) · `seat_allowance_unmetered` (ledger LIST_EQUIVALENT
dollars on the subscription billing path: seat plans show only usage credits, so allowance usage has no
invoice line — reported, never an error, D26) · `unobserved_traffic` (provider tokens by dims absent from
the ledger, e.g. uninstrumented apps: a coverage gap, not an error) · `implied_discount` (tokens match within
0.5%, dollars differ → effective discount) · `estimated_components` (ledger estimated parts: placeholder
output, hidden compaction lines, unknown-scope ranges) · `ccu_single_line` (Foundry / Claude Platform on AWS)
· `default_workspace_null_id` · `no_reporting_api` (Claude Platform on AWS) · `cents_rounding` (Σ source
parse remainders from `cents_to_nano` / `usd_str_to_nano`, §3.3) · `unmapped_cost_type` (incl. disabled
VERIFY SKU rules) · `cloud_credits` (GCP credits / promotions, from `credits[]`) · otherwise `unexplained`.

### 12.4 Gates and verdict

Per channel: `reconciled` iff every closed model-day's rate-card error is within tolerance, the unexplained
residual is ≤ `unexplained_pct` (1%) per workspace-month on closed periods, and there are **no over-count
rows**; `insufficient_data` without invoice rows for that channel; else `not_reconciled`.
`ChannelVerdict.mapping_verified` is False when any cost-type/SKU rule used is unverified (the verdict then
prints "(schema unverified)" and `dq.recon_schema_unverified`). Overall `verdict`: `reconciled` iff every
channel with ledger spend is reconciled; `insufficient_data` when no channel has invoice data; else
`not_reconciled` → exit 3 (unless `--report-only`). Downstream gates use the **channel** verdicts: FOCUS
`BilledCost` (§14.4) and receipts (§13.4) require the channels they cover to be reconciled, so a Bedrock
fleet without a CUR file blocks only its Bedrock rows. Claude Code Analytics aggregates give team-day token
coverage for first-party Claude Code lanes (team level only; the API excludes Bedrock, Vertex, Foundry and
Claude Platform on AWS); `estimated_cost` is compared as `provider_estimate`.

### 12.5 Live pull (`recon/pull.py`, opt-in)

`pull(kind, *, key_env: str, since: str, until: str, out_dir: Path, opener=None, sleep=time.sleep) ->
list[Path]`: only with `--live --admin-key-env VAR` (else usage error 2); stdlib `urllib` with TLS
verification, 30 s timeout, pagination (`has_more` / `next_page`, **VERIFY** names), 31-day chunking,
client-side rate limit (60 rpm for Enterprise Analytics), retry on 429/5xx with capped backoff honoring
`retry-after` ≤ 60 s. The key is read from the environment only, never logged, never written; recorded pages
have auth headers removed. Tests inject a fake `opener` and `sleep` (the socket guard proves no network).
CUR and GCP exports are files the organization already produces; Token Bill never calls AWS or GCP APIs.

### 12.6 Org scan (`recon/orgscan.py`)

`OrgScan` (§10.3) is a `Detector` registered as `aggregate.org-scan`; RECON owns it because it reuses the
rate-card and effective-discount computations of §12.1–§12.2 on `ctx.aggregates` / `ctx.cost_lines`.

---

## 13. Verification and receipts (VERIFY, `verify/`)

### 13.1 Metric and panel (`verify/panel.py`)

Primary unit: **cost per active developer-day**, intention-to-treat, per cluster-day. Allowance
(LIST_EQUIVALENT) spend is measured separately and never enters invoice savings.

```python
def build_panel(store: LedgerStore, *, cluster_kind: str, since: str, until: str,
                baseline_pricer: Pricer, actual_pricer: Pricer, arms: Mapping[str, str] | None = None,
                billing_class: str = "billed") -> list[PanelRow]
```

Dev-days from `cluster_day` (survives identity retention); costs repriced from inferences at the
**pre-registered baseline rate card** (`cost_baseline_nano`, R8) and at the actual card
(`cost_actual_nano`, for the EXACT rate variance); `outcome_prs` from team-level `OutcomeAggregate` when
present. `PanelRow` is defined in `core.types` (§3.5). Secondary units (per task, per success) only in `ab`.

### 13.2 `measure plan` (`verify/rollout.py`)

```python
def plan(clusters: Sequence[str], *, lever_id: str, cluster_kind: str, design: str, waves: int,
         holdback: Decimal, seed: int, pre_panel: Sequence[PanelRow] | None, projection: Figure | None,
         washout_hours: int, looks: Sequence[str], treated: Mapping[str, str] | None = None,
         org_wide_delivery: bool = False) -> MeasurePlan
```

Clusters are cache-isolation units (workspace, MDM group, or IdP group via the Claude apps gateway), never
individuals inside a shared workspace. Output: seeded random wave order (never ordered by spend), a
10–25% never-treated holdback, washout = max(1 h, p90 session length, settings refresh interval: server-
managed 1 h / MDM 30 min), per-wave MDM payload names and `OTEL_RESOURCE_ATTRIBUTES` arm/wave tags, an
assignment log (JSON lines) and its SHA-256, and a pre-registration `{lever, metric, estimator, looks,
rate_card_sha256, mde, projection}` with its SHA-256. **MDE** from 200 A/A re-randomizations of the
pre-period panel under the planned design: `MDE = 2.8 × SD(A/A estimates)`. If `MDE > 0.8 × |projection|`
the plan sets `verification_design = False` and states the cluster count or window needed. A user-supplied
assignment (`treated`) is checked for correlation with pre-period spend; spend-targeted waves cap the
label at MEASURED with a regression-to-the-mean warning. **Org-wide changes** (`org_wide_delivery=True`,
e.g. a server-managed default model or effort, which applies to the whole org, `stepped-wedge-mdm`): the plan
warns that randomization needs MDM groups or the Claude apps gateway per IdP group; if neither is available
it produces `design = "its"` with `verification_design = False` (MEASURED ceiling) and a pre-registered
change date and placebo date.

### 13.3 `measure run` estimators (`verify/estimators.py`, `verify/its.py`; stdlib; floats inside, results to int nano)

```python
def cuped_cluster_dim(panel: Sequence[PanelRow], *, pre_until: str, boot: int = 2000, seed: int = 0
                      ) -> tuple[int, int, int]          # (ATT nano per dev-day, ci_low, ci_high)
def imputation_did(panel: Sequence[PanelRow], *, washout_days: int = 0, boot: int = 2000, seed: int = 0
                   ) -> tuple[int, int, int]
def event_study_its(series: Sequence[tuple[str, int, int]], *, change_date: str, placebo_date: str,
                    hac_lag: int = 7) -> tuple[int, int, int, bool]   # its.py: (level shift nano per dev-day,
                                                                      # ci_low, ci_high, placebo_passed)
```

- **CUPED cluster difference-in-means** (cluster RCT): `θ = cov(Y_post, X_pre)/var(X_pre)` pooled
  (missing-pre indicator for new clusters); `Ỹ = Y_post − θ(X_pre − mean X_pre)`; ATT = dev-day-weighted
  mean `Ỹ(treated) − Ỹ(control)`; cluster bootstrap B = 2,000 stratified by arm, seeded, percentile 95% CI.
- **Imputation DiD** (stepped wedge / staggered): fit `Y_ct = α_c + λ_t` on untreated cells by alternating
  projections (tolerance 1e-9, ≤ 10,000 iterations); impute `Ŷ0` for treated cells (washout days excluded);
  ATT = weighted mean `(Y − Ŷ0)`; cluster bootstrap CI (B = 2,000).
- **Event-study ITS** (org-wide changes without a comparison group, `its-rdit-org-wide`): daily org-level
  cost per active developer-day (baseline-repriced), regression `Y_t = a + b·t + Σ dow_t + δ·1[t ≥ change]`
  with Newey–West (HAC, lag 7) standard errors for autocorrelation, 95% CI on δ; a placebo level shift at the
  pre-registered placebo date in the pre-period must have a CI including 0. Result label is **MEASURED** at
  most, never VERIFIED (no randomization); SDID with donor pools is deferred (§20).
- No TWFE, no naive pre/post. Rate variance = Σ(actual − baseline cost) in the post period, EXACT (R8).

### 13.4 Guards and labels (`verify/label_policy.py`)

```python
def guards(result_inputs: Mapping[str, object], *, plan: MeasurePlan, reconciliation: ReconciliationReport,
           projection: Figure | None) -> tuple[GuardResult, ...]
def decide(*, design: str, randomized: bool, assignment_hash_matches: bool, guards: Sequence[GuardResult],
           ci: tuple[int, int]) -> Evidence
def signable(label: Evidence, *, reconciled: bool, projection: Figure | None) -> bool
```

Guards (all recorded): **SRM** χ² on active dev-days by arm vs plan (fail if p < 0.001); **placebo** (fake
adoption at the pre-period midpoint, or the ITS placebo date; CI must include 0); **MDE** (≤ 0.8 ×
|projection|); **reconciliation** (the channels covered by the panel are `reconciled` for the window);
**cluster ≥ cache scope** for cache-touching levers (no lane's cache scope spans two clusters); **quality
non-inferiority** when team-level outcome data exist (one-sided 90% lower bound of Δ pull requests per active
dev-day > −5%); **pre-registered looks only**; **washout** respected. `decide`: VERIFIED iff randomized with
a logged seed whose assignment hash matches the pre-registration, every guard passes and the CI excludes 0;
MEASURED iff a comparison group exists (or design `its` with its placebo passing) and a CI was computed
(failing guards listed). `measure` never emits EXACT or ESTIMATED. `signable = label ∈ {MEASURED, VERIFIED}
and reconciliation passed and (no projection or the projection is CALIBRATED)`.

### 13.5 `ab` — paired lab comparison (`verify/ab.py`)

```python
def paired_ab(baseline: Sequence[Request], candidate: Sequence[Request], outcomes: Sequence[Mapping[str, object]],
              *, pricer: Pricer, boot: int = 10_000, seed: int = 0) -> AbResult
```

Two usage sets (trace@2 files or ledgers) with `task_id` (attribution `extra["task_id"]`), plus an outcomes
file (`task_id, arm, trial, success, order`). Per-task paired cost differences; cost per success (failures in
the numerator); task-clustered bootstrap B = 10,000 (seeded); verdict `cheaper | no-difference | costlier`
at 95%; token, turn, read and success deltas reported. Scope label `lab:<sha256 of task ids>[:12]`; VERIFIED
only at that lab scope when arm order was randomized per task and each task-arm has ≥ 5 trials (else
MEASURED); any fleet projection derived from it remains ESTIMATED. Acceptance: an RTK-like fixture (tokens
−38%, turns +14%, cost +7%) → `costlier`.

### 13.6 Receipts (`verify/receipts.py`)

```python
def build_receipt(m: MeasurementResult, *, lever_id: str, patch_sha256: str, shapley_credit: Figure | None,
                  reconciliation_verdict: str, calibration: Calibration, tool_version: str, created: str) -> dict
def canonical_bytes(receipt: Mapping[str, object]) -> bytes
def pae(payload_type: str, body: bytes) -> bytes
def sign(receipt: Mapping[str, object], *, key_path: Path, runner=subprocess.run) -> dict   # DSSE envelope
def verify_envelope(envelope: Mapping[str, object], *, allowed_signers: Path, identity: str,
                    runner=subprocess.run) -> bool
```

- Canonical JSON: sorted keys, `separators=(",", ":")`, ASCII keys, **integers and strings only** (money
  `*_usd_micro`, ratios `*_ppm`, `*_milli`) — byte-identical to RFC 8785 JCS for this subset.
- Body: `_type "urn:tokenbill:receipt:v1"`, `subject {lever_id, patch_sha256}`, `predicate {label, design,
  metric, scope_label, window, estimate_usd_micro, ci_low_usd_micro, ci_high_usd_micro, projected_usd_micro,
  realization_rate_milli, shapley_credit_usd_micro, guards[], adjustments[], rate_card_sha256,
  assignment_log_sha256, preregistration_sha256, reconciliation_verdict, calibration, tool_version, created}`.
- DSSE: `payloadType "application/vnd.tokenbill.receipt+json"`; `PAE = "DSSEv1" SP len(type) SP type SP
  len(body) SP body`; envelope `{payload: base64, payloadType, signatures: [{keyid, sig}]}`.
- Sign: `ssh-keygen -Y sign -f KEY -n tokenbill-receipt FILE` over the PAE bytes (subprocess, no shell);
  verify: `ssh-keygen -Y verify -f allowed_signers -I IDENTITY -n tokenbill-receipt -s SIG < PAE` (OpenSSH ≥
  8.1). `sign` refuses (exit 3) any receipt that is not signable (ESTIMATED/EXACT, unreconciled, allowance
  basis, or uncalibrated projection). Missing `ssh-keygen` → clear error; tests skip (`needs_ssh_keygen`).
- The realization rate is stored per receipt (`LedgerStore.put_receipt`).

---

## 14. Outputs (OUT, `outputs/`, `finops/`)

### 14.1 `tokenbill/result@2` JSON (`outputs/result_json.py`)

```json
{"schema": "tokenbill/result@2", "tool": {"name": "tokenbill", "version": "0.2.0"}, "command": "scan",
 "generated_ms": 0, "window": {"since": "2026-08-24", "until": "2026-09-23"},
 "inputs": [{"source_id": "s_…", "adapter": "claude-code", "records": 43383, "quarantined": 0, "evidence": "exact"}],
 "privacy": {"content_tier": "none", "key_id": "k_…", "identity_mode": "install", "k": 5, "suppressed_groups": 0, "evidence": "exact"},
 "rate_card": {"sha256": "…", "layers": ["builtin@2026-09-23"], "stale_rows": [], "contract": null},
 "bill": {"exact": {MONEY}, "estimated": {MONEY}|null, "allowance": {MONEY}|null,
          "coverage": {"priced_inferences": 1, "unpriced_inferences": 0, "unpriced_tokens": 0, "coverage": "1", "evidence": "exact"},
          "esr": {"ratio": "0.71", "evidence": "exact"}, "naive_ratio": {"ratio": "2.33", "evidence": "exact"},
          "breakdowns": [{"dims": "bucket", "rows": […]}], "footnotes": []},
 "data_quality": [{"code": "dq.naive_line_sum_ratio", "severity": "info", "count": 103607, "detail": "…", "evidence": "exact"}],
 "reconciliation": null, "calibration": null, "findings": [], "action_plan": null, "policy_packs": [],
 "replays": [], "measure_plan": null, "measurements": [], "ab": null, "check": null, "pricing": null,
 "receipts": []}
```

`MONEY` = `{"usd": "<exact decimal string>", "nano": <int>, "evidence": …, "basis": …, "finality": …,
"range": {"low_usd": …, "low_nano": …, "high_usd": …, "high_nano": …} | null, "ci_level_pct": … | null,
"calibration": …, "upper_bound": bool, "provenance": […], "note": "…"}`. Ratios are decimal strings.
**Schema rule (tested):** no JSON float anywhere; every JSON object that contains an integer leaf also
contains an `"evidence"` key; money only as `MONEY`; `basis: "list_equivalent"` never appears under `exact`
or any billed key. `--deterministic` drops `generated_ms` and orders all arrays by stable ids. ESR
(Effective Token Savings Rate, exact) = 1 − exact bill ÷ the same tokens priced with every input token at
the uncached rate and no discounts (rate arithmetic on billed tokens).

### 14.2 Terminal (`outputs/terminal.py`)

≤ 100 columns, sanitized, every money value followed by a label chip, e.g. `$41,203.17 exact·list`,
`$9,880.12 allowance·list-equivalent (not billed)`, `~$6,100/mo est. (p10–p90 $2,900–$8,400) calibrated`,
`unpriced (3 inferences)`. Sections: header (sources, window, sessions/lanes/calls), BILL (exact, basis,
coverage, rate-card id, reconciliation badge per channel; allowance on its own line), DATA QUALITY (incl. the
naive line-sum ratio), CALIBRATION, TOP RECOVERABLE (Shapley-ranked, monthly range,
trade-off/needs-eval/upper-bound tags; allowance headroom listed separately), legend line explaining the
four labels. Every `RunResult` slot has a terminal section: replays (`whatif` table: policy, cost, saving
with range and calibration), measure plan (waves, holdback, MDE, verification_design, warnings),
measurements and `ab` (estimate with CI, guards, label), check (violations), pricing (rows, discrepancies).

### 14.3 HTML (`outputs/html.py`)

Single self-contained file: inline CSS and SVG, no scripts, the CSP meta tag (§8.7), dark/light via
`prefers-color-scheme`, WCAG 2.2 AA contrast, no color-only encoding, every chart paired with a `<table>`
with `<caption>`. Sections (rendered for `scan`, `report`, `demo --fleet`): evidence legend; bill (exact)
with coverage and per-channel reconciliation badges, allowance shown separately; where the money goes
(bucket, lane kind, model, team — published aggregates only; context tax); findings ranked by Shapley credit
with label badges, evidence, fix and config patch; calibration panel (NMBE/CV(RMSE), ρ table, diagnostics
matrix); reconciliation panel (per channel); policy-pack preview; methodology and provenance (rate-card
hash, sources, verification dates, assumptions, threats to validity). ≤ 5 MB for 10⁴ lanes (aggregate
sections; top-N drill-down). The v0.1 `report.py` is untouched.

### 14.4 FOCUS 1.4 CSV (`outputs/focus.py`)

One row per (charge period = UTC day, provider, channel, model, token bucket, basis, allocation group, lane
kind). Columns (names and mandatory/nullable status from `core/facts.json` `focus_columns`, re-verified
against FOCUS 1.4, §19.8 #11): `BillingAccountId`, `BillingCurrency=USD`, `BillingPeriodStart`,
`BillingPeriodEnd` (month), `ChargePeriodStart`, `ChargePeriodEnd` (day), `ChargeCategory=Usage`,
`ChargeFrequency=Usage-Based`, `ChargeDescription` (e.g. "claude-opus-5-5 cache_read tokens"),
`ServiceCategory="AI and Machine Learning"`, `ServiceName`, `ProviderName`, `PublisherName`,
`InvoiceIssuerName` (Anthropic, AWS, Google, OpenAI per channel), `RegionId` (if known), `ResourceId`
(workspace/account), `SkuId` (`rate_row_id#bucket`), `PricingCategory=Standard`, `PricingQuantity` (tokens /
10⁶, decimal string), `PricingUnit="1M Tokens"`, `ConsumedQuantity` (tokens), `ConsumedUnit=Tokens`,
`ListUnitPrice`, `ListCost`, `ContractedUnitPrice`, `ContractedCost`, `EffectiveCost`, `BilledCost`, `Tags`
(JSON: team, cost_center, project, workload_class, agent_product, billing_path), `AllocatedMethodId`,
`AllocatedMethodDetails` (for rule splits). `x_` columns (each matching `^x_[A-Z][A-Za-z0-9]{1,48}$`):
`x_Source` (`tokenbill`), `x_Role` (`primary` | `enrichment`), `x_Channel`, `x_BillingPath`, `x_TokenBucket`,
`x_CacheTtl`, `x_LaneKind`, `x_WorkloadClass`, `x_ModelId`, `x_Evidence`, `x_PriceBasis`, `x_Reconciled`,
`x_ReconciliationDeltaPct`, `x_RateCardSha256`, `x_RecoverableCost` (the scope's RR-p50 Shapley saving
allocated by waste share), `x_TopWasteCause`, `x_FindingIds`, `x_SuppressedUsers`.

**BilledCost honesty rule (D19):** by default the export requires every channel in scope to be
`reconciled` (exit 3 otherwise, naming the channels); `--channel C` restricts the export to reconciled
channels; `--allow-unreconciled` fills `BilledCost` from ContractedCost (contract loaded) or ListCost with
`x_Reconciled=false`. `--role enrichment` sets `BilledCost` and `EffectiveCost` to 0 (ledger amounts remain
in `ListCost`/`ContractedCost`/`x_` columns) for organizations that also load the provider's own FOCUS or
cost feed — the documented guard against double counting. **Seat-allowance rows** (`x_PriceBasis =
list_equivalent`) have `ListCost` = list-equivalent and `BilledCost = EffectiveCost = ContractedCost = 0`
(allowance usage is not metered; seat fees are separate purchases outside Token Bill). `--chargeback`
refuses (exit 3) when allocation coverage < 95%. ESTIMATED lines never enter any cost column (they appear
only in `x_` columns). Rows below k users are merged with `core.kanon.publish` (`x_SuppressedUsers`).

### 14.5 Other outputs

- **SARIF 2.1.0** (`outputs/sarif.py`) for `check`: rules `TB-CACHE-SHARE`, `TB-NEW-BREAKER`,
  `TB-COST-REGRESSION`, `TB-SERIALIZATION-CHURN`; required keys validated in tests.
- **ccusage-compatible JSON** (`outputs/ccusage.py`): `daily` and `monthly` reports in ccusage's `--json`
  shape (`date`, `inputTokens`, `outputTokens`, `cacheCreationTokens`, `cacheReadTokens`, `totalTokens`,
  `totalCost`, `modelsUsed`, `modelBreakdowns[]`), costs from the exact ledger rendered as JSON numbers from
  decimal strings (never Python floats); documented as a compatibility export.
- **Showback** (`outputs/showback.py`): per team, from `PublishedAggregate` only — spend with basis chips,
  cost per active developer-day beside the published $13 / $30 anchors (with source), cache-read share
  against the 84% / 94% / 80% benchmarks (with source), context p50/p90, top findings (Shapley-ranked, with
  labels), allocation coverage, and **per billing path** the list-equivalent spend per active developer-month
  (p50 / p90, k ≥ 5) with the overage share from QUOTA_STATE — the input finance needs for seat-mix
  decisions (the seat optimizer itself is deferred, §20); HTML (CSP, table twins), CSV and JSON; never
  individuals.

### 14.6 Allocation and workload (`finops/allocation.py`, `finops/workload.py`)

- Rules file (JSON, git-versioned, ordered): `{"rules": [{"match": {"workspace_id": {"in": […]},
  "extra.mdm_group": {"eq": "x"}, "api_key_id": {…}, "repo": {…}, "agent_product": {…}, "entrypoint":
  {"prefix": "sdk-"}}, "set": {"team": …, "cost_center": …, "project": …, "workload_class": …},
  "split": {"method": "proportional", "by": "dev_days", "targets": […]}}]}`. Operators `eq`, `in`,
  `prefix`, `regex` (ids only; a safe subset: no backreferences, ≤ 200 chars). First match wins per target
  field; unmatched → `team="(unallocated)"`; splits emit `AllocatedMethodId`/`Details`. `apply_rules(request,
  rules) -> Attribution`; `coverage(rows) -> str` (allocated $ / total $; ≥ 95% is the chargeback gate).
- Workload classifier (`classify(lane) -> (WorkloadClass, confidence)`): entrypoint
  `claude-code-github-action` → CI (0.95); `sdk-py`/`sdk-ts`/`sdk-cli` → SERVICE (0.6); resource tag
  `workload=…` → as tagged (0.99); `service_tier == batch` → BATCH (0.99); session with 0 HUMAN_PROMPT and ≥ 10
  requests → automated SERVICE (0.7); periodic cadence (CV of gaps < 0.25) → SCHEDULED (0.8); rule-set class
  (0.9). Highest confidence wins; ties go to the more automated class.

### 14.7 Renderer API (OUT)

```python
# outputs/result_json.py
def money_json(fig: Figure) -> dict[str, object]
def to_result_json(result: RunResult, *, deterministic: bool = False) -> dict[str, object]
def dumps_result(result: RunResult, *, deterministic: bool = False) -> str      # canonical, no floats
def validate_result_json(doc: Mapping[str, object]) -> list[str]              # schema-rule violations ([] = ok)
# outputs/terminal.py
def render_terminal(result: RunResult, *, width: int = 100) -> str             # every RunResult slot
# outputs/html.py
def render_html(result: RunResult) -> str
# outputs/focus.py
def write_focus(rows: Iterable[LedgerCostRow], out: IO[str], *, reconciled_channels: frozenset[str],
                allow_unreconciled: bool, role: str = "primary", contracted: Mapping[tuple, int] | None = None,
                recoverable_by_scope: Mapping[tuple[str, ...], int] | None = None, rate_card_sha: str,
                k: int = 5, chargeback: bool = False, allocation_coverage: str | None = None,
                channels: frozenset[str] | None = None) -> int
# outputs/sarif.py
def to_sarif(check: CheckResult, *, tool_version: str) -> dict[str, object]
# outputs/ccusage.py
def to_ccusage(rows: Iterable[LedgerCostRow], *, report: str = "daily") -> str   # "daily" | "monthly"
# outputs/showback.py
def render_showback(teams: PublishedAggregate, cluster_days: Sequence[ClusterDay], findings: Sequence[Finding],
                    plan: ActionPlan | None, out_dir: Path, *, formats: Sequence[str] = ("html",),
                    billing_paths: PublishedAggregate | None = None) -> list[Path]
# finops/allocation.py
def load_rules(path: Path) -> "RuleSet"; def apply_rules(req: Request, rules: "RuleSet") -> Attribution
def coverage(rows: Iterable[LedgerCostRow]) -> str
# finops/workload.py
def classify(lane: Lane) -> tuple[WorkloadClass, str]      # (class, confidence decimal string)
```

The ccusage session report is not offered on fleet stores (it would list sessions); `scan --self` may render it
locally.

---

## 15. CLI (WIRING, CLI-LEDGER, CLI-SAVINGS: `config.py`, `pipeline/common.py`, `cli.py`, `gate.py`, `pipeline/*`, `commands/*`)

Global flags: `--config PATH` (JSON; default `./.tokenbill/config.json` then
`~/.config/tokenbill/config.json`; precedence CLI flag > `TOKENBILL_*` env > config file > defaults),
`--db PATH`, `--format {text,json}`, `--deterministic`, `--quiet`, `-v`, `--log-json` (JSON logs to
stderr), `--plugins` (load third-party entry points), `--strict-dq`, `--jobs N` (process pool over shards;
default 1; results identical for any N). Every command is offline unless `--live`.

Exit codes: `0` ok; `1` runtime failure; `2` usage error; `3` a gate failed (reconcile, calibrate
`--require-pass`, check, receipt verify/sign refusal, export gates, pricing verify); `4` completed with
data-quality warnings above threshold under `--strict-dq`.

**Command table.** `cli.py` (CLI-LEDGER) holds a static table `COMMANDS: dict[str, str]` mapping every verb
below to `"tokenbill.commands.<module>"`; each command module exposes `add_parser(subparsers)` and `run(args)
-> int` and is imported lazily only when its verb is parsed (so `--version` stays instant and a missing
module fails only its own verb). CLI-LEDGER writes the full table including CLI-SAVINGS's modules. `demo`
and `analyze` keep their v0.1 handlers and output (§16).

| command (owner) | purpose | flags |
|---|---|---|
| `tokenbill --version` | unchanged | |
| `demo` (L) | **unchanged** v0.1 demo (byte-identical golden) | `-o`, `--seed`, `--scenario` |
| `demo --fleet` (L → S) | synthetic fleet end to end (§18); dispatches to `pipeline.savings.run_demo_fleet` | `-o fleet.html`, `--seed 7`, `--out-dir DIR`, `--format` |
| `analyze TRACE…` (L) | v0.1 behavior on safe trace@1 inputs; engine routing (§15.1) | `-o`, `--model-price MODEL=IN,OUT`, `--engine {auto,v1,v2}`, `--rates FILE`, `--contract FILE`, `--format` |
| `scan` (S) | one-shot local Claude Code review (self-view) | `--claude-dir` (default `~/.claude/projects`), `--since/--until`, `--db` (default temp, deleted), `--rates`, `--contract`, `-o report.html`, `--format`, `--billing-path {api_key,subscription,…}` |
| `scan --org` (S) | aggregate-only org scan from Admin/Analytics/CUR data (D32) | `--usage-report FILE…`, `--cost-report FILE…`, `--cc-analytics FILE…`, `--enterprise-analytics FILE…`, `--aws-cur FILE…`, `--gcp-billing FILE…`, `--live --admin-key-env VAR`, `--team-map FILE`, `--since/--until`, `-o`, `--format` |
| `me` (S) | alias of `scan --self` (no own logic) | as `scan` |
| `init` (L) | create config, store dir (0700), key file (0600), print the privacy notice | `--dir`, `--identity-mode {central,two-stage}`, `--k 5`, `--collector` (creates only collector config) |
| `collect claude-code` (L) | on-device, content-free, incremental → trace@2 `usage` file | `--projects`, `--out DIR`, `--state FILE`, `--since`, `--principal-ref env:VAR\|mdm-file:PATH\|none`, `--identity-mode`, `--collection-key-file`, `--team`, `--attr K=V` (repeatable; also `OTEL_RESOURCE_ATTRIBUTES`) |
| `collect claude-code-headless` (L) | CI/headless/Agent SDK streams → trace@2 `usage` file (§5.12) | `--in FILE…` (execution_file / stream-json / json), `--out DIR`, `--collection-key-file`, `--principal-ref`, `--team`, `--attr K=V`; reads `GITHUB_*` env for repo/workflow/run attempt |
| `ingest SOURCE…` (L) | normalize sources into the store | `--db` (required), `--adapter NAME\|auto`, `--content`, `--key-file` (org key), `--collection-key-file` (name key), `--attr`, `--rules RULES.json`, `--team-map FILE`, `--k`, `--since/--until`, `--strict`, `--renormalize`, `--rates`, `--contract` |
| `bill` (L) | exact priced totals (+ allowance and estimated beside) | `--db`, `--rates`, `--contract`, `--basis {list,contract}`, `--group-by DIMS`, `--since/--until`, `--self`, `--reprice`, `--format {text,json,csv}` |
| `reconcile` (L) | ledger gate, per channel | `--db`, `--usage-report FILE…`, `--cost-report FILE…`, `--cc-analytics FILE…`, `--enterprise-analytics FILE…`, `--openai-usage FILE…`, `--openai-costs FILE…`, `--aws-cur FILE…`, `--gcp-billing FILE…`, `--live --admin-key-env VAR [--record DIR]`, `--tolerance-pct 0.5`, `--unexplained-pct 1.0`, `--closed-only`, `--suggest-contract OUT.json`, `--report-only`, `--format` |
| `calibrate` (S) | model gate (streams shards) | `--db`, `--granularity {day,month}`, `--require-pass`, `--format` |
| `findings` (S) | detectors ranked by Shapley-credited recoverable $ | `--db`, `--detector ID…`, `--min-usd X`, `--group-by team\|repo\|agent_type\|model\|lane_kind`, `--self`, `--break-glass REASON`, `--no-shapley`, `--include-tradeoffs`, `--seed`, `--sample-lanes N`, `--format` |
| `whatif` (S) | counterfactual replays | `--db`, `--policy SPEC` (repeatable), `--mode {documented,calibrated,both}`, `--shapley`, `--sample-lanes N`, `--seed`, `--format` |
| `policy` (S) | per-cohort patch packs | `--db`, `--target {claude-code,litellm,sdk}`, `--current FILE`, `--cohort-by {team,mdm-group}`, `--include-tradeoffs`, `--contract FILE`, `-o DIR` |
| `policy check-effect` (S) | post-rollout effectiveness | `--db`, `--lever ID`, `--cohort C`, `--since DATE`, `--days 7` |
| `measure plan` (S) | randomized rollout plan (or ITS design for org-wide changes) | `--db`, `--clusters FILE`, `--cluster-kind`, `--lever ID`, `--design {cluster_rct,stepped_wedge,its}`, `--waves N`, `--holdback PCT`, `--seed`, `--projection FILE`, `--looks DATES`, `--treated FILE`, `--org-wide`, `-o plan.json` |
| `measure run` (S) | realized savings | `--db`, `--plan plan.json`, `--baseline-rates FILE`, `--seed`, `-o measurement.json` |
| `ab` (S) | paired lab comparison | `--baseline FILE`, `--candidate FILE`, `--outcomes FILE`, `--boot 10000`, `--seed` |
| `receipt create/sign/verify` (S) | receipts | `create --measurement FILE -o receipt.json`; `sign --key PATH receipt.json`; `verify --allowed-signers FILE --identity ID receipt.dsse.json` |
| `export` (L) | interchange | `--db`, `--format {focus,trace2,ccusage}`, `--grain {day,hour}`, `--content {none,fingerprint}` (never full), `--require-reconciled` (default) / `--allow-unreconciled`, `--channel C…`, `--role {primary,enrichment}`, `--chargeback`, `-o FILE` |
| `showback` (L) | per-team pages | `--db`, `--out DIR`, `--format {html,csv,json}`, `--since/--until` |
| `report` (S) | HTML fleet/team/self report | `--db`, `-o report.html`, `--team T`, `--self`, `--reconciliation FILE`, `--calibration FILE` |
| `pricing` (L) | registry | `show [MODEL] [--at DATE]`, `verify [--snapshot FILE] [--live] [--feed FILE…]`, `diff A B`, `emit-model-pricing --contract FILE` |
| `check` (S) | CI gate (§15.2) | `TRACE…` or `--db`, `--min-cache-read-share 0.80`, `--after-turn 2`, `--baseline FILE`, `--max-cost-regression-pct 15`, `--min-runs 3`, `--fail-on {breaker,regression,share,any}`, `--format {text,json,sarif}`, `--summary-md FILE`, `--write-baseline FILE` |
| `purge` (L) | erasure / retention | `--db`, `--principal P` or `--before DATE`, `--yes` |

(L = CLI-LEDGER, S = CLI-SAVINGS.)

**Pipeline** (`tokenbill/pipeline/`, pure functions returning typed results; no printing):

```python
# config.py (WIRING)
@dataclass(frozen=True)
class Config:
    k: int = 5; min_usd: str = "1.00"; jobs: int = 1; shard_max_requests: int = 250_000
    sample_lanes: int = 20_000; identity_mode: str = "central"
    retention: Mapping[str, int] = …   # identity_days 90, request_days 395, event_days 90, rollup_days 395
    thresholds: Mapping[str, str] = …  # detector overrides (§10.1), e.g. "defaults.effort", "policy.ttl.<team>"
    name_allowlist: frozenset[str] = frozenset(); residency_required: tuple[str, ...] = ()
    key_file: str | None = None; collection_key_file: str | None = None; rates: tuple[str, ...] = ()
    contract: str | None = None; strict_dq_threshold: int = 1
def load_config(path: Path | None, environ: Mapping[str, str], overrides: Mapping[str, object]) -> Config
    # precedence: overrides (CLI flags) > TOKENBILL_* env > file (JSON) > defaults; unknown keys → UsageError
# pipeline/common.py (WIRING, wave 2; tested with fakes, gate-tested with RateCard/SqliteStore)
@dataclass(frozen=True)
class Env:                       # resolved config + keys + pricer, built once per command
    config: Config; pricer: Pricer; rules: CacheRulesProvider; k: int; jobs: int
    org_key: bytes | None; name_key: bytes | None; name_key_id: str | None; now_ms: int
def build_env(config: Config, *, rates: Sequence[Path] = (), contract: Path | None = None,
              model_prices: Sequence[tuple[str, str, str]] = (), key_file: Path | None = None,
              collection_key_file: Path | None = None, now_ms: int | None = None) -> Env
    # loads RateCard via registry-style lazy import of tokenbill.rates.engine (FakePricer injectable)
def open_store(path: Path, env: Env, *, create: bool = True) -> LedgerStore
def ingest_paths(store: LedgerStore, paths: Sequence[Path], env: Env, opts: IngestOptions,
                 *, adapter: str = "auto") -> tuple[list[SourceInfo], list[DataQualityNote]]
def map_shards(fn: Callable[[ShardKey], T], shards: Sequence[ShardKey], *, jobs: int, db_path: Path | None) -> list[T]
    # sequential when jobs == 1; otherwise a ProcessPoolExecutor whose workers open the store read-only;
    # results are returned in shard order
def bill_summary(store: LedgerStore, env: Env, *, since_ms: int, until_ms: int, group_by: Sequence[str]) -> BillSummary
# pipeline/ledger.py (CLI-LEDGER)
def run_ingest(...) -> RunResult; def run_bill(...) -> RunResult; def run_reconcile(...) -> RunResult
def run_export(...) -> int; def run_showback(...) -> list[Path]; def run_collect(...) -> Path
def run_collect_headless(...) -> Path; def run_analyze_v2(...) -> RunResult; def run_pricing(...) -> RunResult
# pipeline/savings.py (CLI-SAVINGS)
def run_calibrate(...) -> RunResult; def run_findings(...) -> RunResult; def run_whatif(...) -> RunResult
def run_policy(...) -> RunResult; def run_scan(...) -> RunResult; def run_scan_org(...) -> RunResult
def run_report(...) -> RunResult; def run_demo_fleet(...) -> RunResult; def run_check(...) -> CheckResult
# pipeline/verification.py (CLI-SAVINGS)
def run_measure_plan(...) -> RunResult; def run_measure_run(...) -> RunResult; def run_ab(...) -> RunResult
def run_receipt(...) -> RunResult
```

`run_findings` = `lane_index` → `plan_shards` → static-prefix floor from `lane_first_reads` → calibrate (two
streaming passes) → per shard `run_detectors(emit_missing=False)` (process pool) → once `run_detectors([],
emit_missing=True)` + `aggregate.org-scan` on aggregates → `merge_findings` → `rescope_findings(count_users=
store.count_users…)` → `build_action_plan` (sample + full-scope scaling) → `put_findings`. Every pipeline
function accepts explicit parameters (paths, windows, env) so tests call them without argparse.

### 15.1 `analyze` engine routing (D2, D38)

Order of operations in `--engine auto` (default): (1) apply `--model-price` overrides to the v0.1
`PRICING` table exactly as 0.1.2 does; (2) read every input with the frozen strict `trace.read_trace()`
(errors such as an unpaired surrogate propagate as in 0.1.2: one-line error, exit 1); (3) namespace
colliding `run_id`s (below); (4) inspect the parsed runs for a **v1-unsafe condition**: `cache_control`
marker positions that move between consecutive calls of a run (v1 reports a false history-rewrite, because
it renders the markers), any `cache_control` with `ttl: "1h"` (v1 prices 1h writes at 1.25×), or more than
one model change within a run (interleaved lanes; v1 under-reports redundancy). If none is found, the frozen
v1 pipeline renders byte-identically; otherwise `run_analyze_v2` runs on the same files and one stderr line
reads `note: using the v2 engine because <reason> (--engine v1 forces the legacy engine)`. **An unknown model
is not a trigger**: v1 prints "unavailable" for its dollars (pinned by the frozen
`tests/test_cli.py::test_model_price_override_prices_unknown_model`); `--engine v2` gives partial totals with
"unpriced (N inferences)". In the v1 path `cli.py` renames runs whose `run_id` appears in more than one input
file to `<file name>:<run_id>` (replacing `run_id` on the `Run` and its `Call`s with `dataclasses.replace`)
so totals add ($22, not $40). `--engine v1` forces the legacy engine (still with the run-id namespacing);
`--engine v2` forces the new one (reading through `TraceV1Adapter`, with `--model-price` passed as a
`model_price_layer`).

### 15.2 `check` CI gate (`gate.py`, CLI-SAVINGS)

Inputs: trace@1/trace@2 files of a recorded smoke test (≥ `--min-runs` runs) or a ledger, plus an optional
baseline (a previous `check --format json` output). Checks: (1) cache-read share over requests after turn
`--after-turn` ≥ `--min-cache-read-share`; (2) no new block-breaker kinds versus the baseline (fingerprinted
inputs; trace@1 inputs are fingerprinted on import); usage-only inputs compare miss causes except
ttl-expiry, compaction and directory-change; (3) median cost per run ≤ baseline median × (1 + tolerance)
(medians over ≥ 3 runs; fewer runs → warning, check skipped); (4) serialization churn (fingerprinted). Output
SARIF, a Markdown summary with Δ$ per 1,000 runs, an optional new baseline; exit 3 on failure. Acceptance:
the demo `timestamp` trace against the `well-behaved` baseline fails with `TB-NEW-BREAKER`
(volatile-system) and `TB-CACHE-SHARE`; `well-behaved` against itself passes.

---

## 16. Backward compatibility

1. **Frozen modules:** `tokenbill/__main__.py common.py trace.py analyzer.py simulator.py breakers.py
   report.py demo_traces.py` are never edited (ownership check). New code may import them (e.g.
   `trace._parse_call`, `trace.read_trace`, `breakers.VOLATILE_PATTERNS`, `demo_traces.scenario`).
2. **Golden tests:** `tokenbill demo` (default, `-o report.html`, each `--scenario`, `--seed 7` and `--seed
   11`) and `tokenbill analyze` on the four demo traces (written with the frozen `write_trace`), stdout and
   HTML, are byte-identical to 0.1.2 after normalizing the version string and the report date. The goldens
   are captured by F-CORE from the untouched tree (`scripts/capture_goldens.py` → `tests/v2/golden/`) before
   any other package changes code; CLI-LEDGER owns the comparison test.
3. **All 207 existing tests stay green** in every package branch — including the frozen
   `tests/test_cli.py` (unknown model → "unavailable" through v1; unpaired surrogate → exit 1 before any
   routing, §15.1) and `tests/test_instrument.py` (also on Windows, thanks to the loopback-tolerant socket
   guard); `tests/test_pricing.py` changes only by the added `SPEC_TABLE` rows (RATES, same commit as the
   `pricing.py` rows).
4. **trace@1** files are read and written unchanged; the v2 path reads them through `TraceV1Adapter`.
5. **`pricing.py`** keeps `ModelPricing`, `PRICING`, `pricing_for`, `cost_breakdown`, `price_usd`,
   `CACHE_TTL_SECONDS`, `TTL_REFRESH_ON_READ`, `MAX_BREAKPOINTS`, `RENDER_ORDER`; additive rows only (§6.8).
6. **`Recorder(path).wrap(client)`** keeps writing trace@1 exactly as in v0.1 (D13).
7. **`analyze`** keeps its flags; `--engine auto` routing and run-id namespacing (§15.1) change output only
   for inputs on which v0.1 was demonstrably wrong (moving markers, 1h markers, interleaved models, reused
   run ids).
8. **Dual-engine agreement** on the demo corpus (§10.4) guards the v2 engine against drifting from planted
   truth.

---

## 17. Performance budgets (CI; `perf` marker nightly at full size, PR CI at 1/10 size with scaled budgets)

| operation | budget |
|---|---|
| Claude Code import | ≥ 25,000 assistant lines/s; 200,000 synthetic lines ≤ 30 s; peak RSS ≤ 150 MB (streaming per file) |
| trace@2 `usage` ingest into SQLite | ≥ 20,000 requests/s |
| store lane scan (`iter_lanes`) | 10⁶ requests ≤ 30 s; peak RSS ≤ 500 MB |
| `bill` | 10⁶ inferences ≤ 60 s (SQL `SUM` over int nano) |
| usage-level replay | O(n) per lane per policy; 10⁶ requests × 1 policy ≤ 30 s |
| calibrate (two streaming passes) | 10⁶ requests ≤ 120 s; peak RSS ≤ 1 GB |
| **fleet pipeline** `findings` (all detectors, sharded) + `plan` (sample Shapley + one full-scope joint replay) | 10⁶ requests ≤ 8 min single process, peak RSS ≤ 1.5 GB; 2× requests ≤ 2.3× time; `--jobs 4` ≥ 2.5× faster on a 4-core runner; outputs identical for `--jobs 1` and `--jobs 4` |
| Shapley | exact for ≤ 6 interacting levers per group (≤ 64 joint replays, on the ≤ 20,000-lane sample); else 200 seeded permutations with SE |
| block-level replay | O(new blocks + 4 breakpoints × 20 positions) per request; a 4,000-call run ≤ 2 s (v0.1: 6.3 s, quadratic) |
| HTML report | ≤ 5 MB for 10⁴ lanes |
| `demo --fleet` | ≤ 60 s, keyless, networkless |

Sizing: 5,000 developers ≈ 10⁷ requests per month ≈ 3.3 × 10⁵ per day (~17 s of central ingest per day at
the budget); a monthly `findings` + `plan` run over 10⁷ requests is a nightly batch job of ≈ 80 min single
process, ≈ 25 min with `--jobs 4` (shards are teams, each ≤ 250k requests, so peak RSS stays within the
budget regardless of fleet size).

---

## 18. Synthetic fleet demo (SYNTH-FLEET generator; CLI wiring; INTEGRATION flagship test)

`synth/fleet.py: generate(seed: int = 7, *, devs: int = 61, days: int = 28, out_dir: Path | None = None,
scale_requests: int | None = None) -> FleetWorld` builds a deterministic world:
`FleetWorld(sessions, requests, events, aggregates, cost_lines, outcomes, source_files: dict[str, Path],
truth: FleetTruth, today: str)`. Canonical records are generated directly (fast, used for perf gates with
`scale_requests`, streaming in bounded memory); `source_files` (written by `synth/writers.py`) adds a small
schema-true Claude Code transcript tree for one team (CANARY planted in content fields), claude-code-action
execution files for ci-bots, an OTLP/JSON file for one team, a trace@2 `fingerprint` file for the agents
team, admin pages (usage_report, cost_report, Claude Code Analytics) and an AWS CUR 2.0 CSV for the Bedrock
team. `today` = window end + 25 days (earlier dates closed, the last days provisional). Every planted
quantity's truth is computed **independently of REPLAY and DETECT** by per-plant closed forms at generation
time (`synth/truth.py`, int nano, tolerance per plant), and a gate test cross-checks each against
`synth.oracle.ReferenceReplay`. All prices come from `core.testing.FakePricer` (facts.json), which the RATES
parity test ties to the real registry.

| team (devs) | channel / billing path | plant | expected recovery (tolerance) |
|---|---|---|---|
| platform (6) | anthropic_api / api_key | gateway strips `cache_control` (no reads or writes); tool search disabled behind the gateway | `cache.gateway-disabled` no-cache; recoverable = truth(`repair=restore_caching`) (±5%) |
| payments (7) | anthropic_api / api_key | Claude Code main lanes billed 5m; 12% of gaps in 5–60 min | `cache.ttl-advisor` recommends 1h for main; saving = truth(`ttl=1h`) (±5%); Shapley credit of the TTL lever within ±5% of truth |
| search (6) | anthropic_api / subscription | sessions grow to 900k, no compaction below 967k | `context.size-tax` exact (±0) with basis LIST_EQUIVALENT; `context.compaction-window` 400k point = truth (±5%) reported as allowance headroom; reconcile residual `seat_allowance_unmetered` |
| mobile (6) | anthropic_api / subscription (1h main) | cold resumes after 1–8 h idle at 400–600k; two overage days (QUOTA_STATE using_overage) | `cache.cold-resume`: cost_observed exact (±0), premium (±2%), allowance labels except the overage days (billed, usage_credits) |
| infra (6) | anthropic_api / api_key | 2 devs sticky fast mode, 1 sticky xhigh | `premium.modifiers` fast premium exact (±0); self findings for those devs; org count suppressed (< k) |
| data (6) | anthropic_api / api_key | workflow agents on Opus 5; Fable 5 in main; Claude Code main default Opus 5 at effort high; model migration Opus 4.8 → Opus 5 on day 14 with +35% output per call | `model.routing` delegation (trade-off, needs eval); same-tier repricing exact arithmetic (±0); `default-model` and `default-effort` (trade-off, needs eval, point = truth ±5%); `rebaseline` info with Δ output per request (±2%) |
| ops (5) | bedrock / bedrock (regional endpoint) | one runaway loop session (3 h, 400 requests, no human prompt); CUR 2.0 file with net cost 10% below list | `tail.runaway` (team only); `premium.modifiers` regional-premium exact; reconcile bedrock channel via suggested contract multiplier 0.90 |
| ci-bots (5) | anthropic_api / api_key | claude-code-action execution files with dynamic system sections and CLI version drift; `max_tokens` 16,384 truncating 15% of attempts with retries; single-shot service calls | `automation` ci-cross-run, batch-eligible, ci-run-cost; `failure.path` max-tokens-truncation (cost exact ±0, recoverable ±5%) |
| agents (5) | anthropic_api / api_key | Agent SDK lanes (recorder trace@2, fingerprint profile): 7-minute idle gaps, context-editing clears while warm on short runs, 14k tokens of non-deferred tool definitions | `cache.ttl-advisor` keepalive-recommended (±5%), `cache.rebuild` edit-churn (±5%), `context.static-prefix` tool-defs-bloat (range contains truth), block breakers none |
| core (6) | anthropic_api / api_key | healthy control | **no finding with recoverable ≥ min_usd** |
| tiny (3) | anthropic_api / api_key | normal usage | suppressed/merged in showback and findings |

Across teams: refusal fallbacks with iterations (declined outputs 0 and 6), placeholder-usage calls
(MESSAGE_START_ONLY), hidden compactions, one call on an unknown model (coverage < 1). The reference
Anthropic cost report carries a **15% contract discount**, a Priority-tier bucket (explained residual) and
provisional days; the CUR file a 10% discount on the Bedrock channel.

Flagship acceptance (INTEGRATION, `tests/v2/e2e/test_fleet_flagship.py`, deliberately limited to the ledger
and findings path): every plant recovered in its scope within tolerance; the control team clean;
`reconcile --suggest-contract` recovers multiplier 0.85 on `anthropic_api` and 0.90 on `bedrock`, and both
rerun verdicts are `reconciled`; allowance spend is never in the exact bill or `BilledCost`; k-anonymity holds
in every published output; the canary is absent from every output; result JSON byte-identical across two
processes and across `--jobs 1` / `--jobs 2`; `demo --fleet` keyless < 60 s. Plan/Shapley recovery,
verification (stepped wedge with a known 25% effect: imputation estimate within ±5%, 95% CI coverage ≥ 90%
over 50 seeds, 10 in PR CI), ITS on an org-wide change, and receipt signing are **separate** tests
(`test_plan_recovery.py`, `test_verification_panels.py`, `test_receipts_e2e.py`).

---

## 19. Facts the build depends on (build reference; re-verify before transcription)

All values as verified by the research phase on **2026-09-23** (finding ids in the last column). **F-CORE's
wave-0 facts task** transcribes the subset the code depends on into `tokenbill/core/facts.json` (§3.24),
re-verifying each row against its primary source when the build environment has network access
(`verification: "primary"`) and otherwise keeping the research verification (`verification: "research"`);
every other package reads facts from there (FakePricer, catalogs, evidence, FOCUS columns), and RATES'
registry must equal it on shared rows (D37). Rows marked **VERIFY** were not fully confirmed: they ship
`enabled: false` (unpriced) or as commented guidance until checked.

Primary sources: **PRICING** https://platform.claude.com/docs/en/about-claude/pricing (also `.md`) ·
**CACHING** https://platform.claude.com/docs/en/build-with-claude/prompt-caching · **DIAG**
…/build-with-claude/cache-diagnostics · **BATCH** …/build-with-claude/batch-processing · **COMPACT**
…/build-with-claude/compaction · **EDIT** …/build-with-claude/context-editing · **FALLBACK**
…/build-with-claude/refusals-and-fallback (and `/fallback-credit`) · **TOKCOUNT** …/build-with-claude/
token-counting · **USAGEAPI** https://platform.claude.com/docs/en/manage-claude/usage-cost-api · **CCAPI**
…/manage-claude/claude-code-analytics-api · **EAAPI** …/manage-claude/analytics-api · **RATELIM**
…/api/rate-limits · **CC-COSTS** https://code.claude.com/docs/en/costs · **CC-CACHE** …/prompt-caching ·
**CC-OTEL** …/monitoring-usage · **CC-SETTINGS** …/settings-reference · **CC-ENV** …/env-vars · **CC-MODEL**
…/model-config · **CC-SMS** …/server-managed-settings · **CC-GW** …/claude-apps-gateway-spend-limits ·
**OAI-CACHE** https://developers.openai.com/api/docs/guides/prompt-caching · **OAI-PRICING**
…/api/docs/pricing · **OAI-FAST** …/guides/fast-mode · **OAI-FLEX** …/guides/flex-processing · **BR-CACHE**
https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html · **BR-PRICE**
https://aws.amazon.com/bedrock/pricing/ and the Price List offer file
`https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrockFoundationModels/current/us-east-1/index.json`
· **VX-PRICE** https://cloud.google.com/vertex-ai/generative-ai/pricing · **OTEL-GENAI**
https://github.com/open-telemetry/semantic-conventions-genai · **FOCUS** https://focus.finops.org/focus-specification/
· **FEMP** https://www.energy.gov/sites/default/files/2016/01/f28/mv_guide_4_0.pdf · **DSSE**
https://github.com/secure-systems-lab/dsse/blob/master/protocol.md · **JCS** https://www.rfc-editor.org/rfc/rfc8785
· **SSHKEYGEN** https://man.openbsd.org/ssh-keygen.1

### 19.1 Anthropic first-party model rates (USD per million tokens) — PRICING

| model id | input | output | cache read (mult.) | 5m write (1.25×) | 1h write (2×) | min cacheable | tokenizer | effective / notes | findings |
|---|---|---|---|---|---|---|---|---|---|
| claude-opus-5-5 | 4.00 | 20.00 | 0.20 (0.05×) | 5.00 | 8.00 | 512 | 4.7+ | from 2026-09-22 (launch); default effort medium | `anth-pricing-table-2026-09`, `anth-evidence-drift` |
| claude-fable-5-1, claude-mythos-5-1 | 10.00 | 50.00 | 0.25 (0.025×) | 12.50 | 20.00 | 512 | 4.7+ | effective **2026-09-01** (launch; reads −75% vs Fable 5); no Priority Tier; covered model (30-day retention) | `anth-pricing-table-2026-09`, `anth-evidence-drift` (corrected), `cc-same-tier-upgrade` |
| claude-fable-5, claude-mythos-5 | 10.00 | 50.00 | 1.00 (0.1×) | 12.50 | 20.00 | 512 | 4.7+ | | same |
| claude-opus-5 | 5.00 | 25.00 | 0.50 | 6.25 | 10.00 | 512 | 4.7+ | released 2026-07-24; thinking on by default | same, `opus5-thinking-effort-rebaseline` |
| claude-opus-4-8 | 5.00 | 25.00 | 0.50 | 6.25 | 10.00 | 1024 | 4.7+ | | same |
| claude-opus-4-7 | 5.00 | 25.00 | 0.50 | 6.25 | 10.00 | 2048 (Bedrock 4096) | 4.7+ | default effort xhigh | `anth-cache-hidden-miss-causes`, `bedrock-cache-semantics` |
| claude-opus-4-6 | 5.00 | 25.00 | 0.50 | 6.25 | 10.00 | 4096 | legacy | | same |
| claude-opus-4-5 | 5.00 | 25.00 | 0.50 | 6.25 | 10.00 | 4096 | legacy | retirement floor 2026-11-24 | `cc-timing-calendar` |
| claude-opus-4-1, claude-opus-4 | 15.00 | 75.00 | 1.50 | 18.75 | 30.00 | **VERIFY** | legacy | retired on 1P, billed on partner clouds | `anth-pricing-table-2026-09` |
| claude-sonnet-5 | 2.00 | 10.00 | 0.20 | 2.50 | 4.00 | 1024 | 4.7+ | launch rate permanent (increase cancelled) | `anth-pricing-table-2026-09` |
| claude-sonnet-4-6 | 3.00 | 15.00 | 0.30 | 3.75 | 6.00 | 1024 | legacy | | same |
| claude-sonnet-4-5 | 3.00 | 15.00 | 0.30 | 3.75 | 6.00 | 1024 | legacy | retirement floor 2026-09-29 | same |
| claude-haiku-4-5 | 1.00 | 5.00 | 0.10 | 1.25 | 2.00 | 4096 | legacy | retirement floor 2026-10-15; strips earlier thinking blocks on a plain user turn | same |
| claude-sonnet-4, claude-haiku-3-5 | **VERIFY** | **VERIFY** | 0.1× | | | haiku-3-5: 2048 | legacy | ship disabled unless verified | `cb-pricing-coverage`, `anth-cache-rate-limits` |
| claude-sonnet-5-5, claude-haiku-5-5 | announced 2026-09-22, not priced | | | | | | | `pricing verify` must flag when priced | open question |

Tokenizer: Claude 4.7+ (Opus 4.7/4.8/5/5.5, Sonnet 5, Fable, Mythos) produces ~30% more tokens for the same
text (1.0–1.35× by content; CJK ~1.01×) (`anth-tokenizer-inflation`, `tokenizer-inflation-47plus`).
Same-tier pairs with the same tokenizer: Fable 5→5.1, Opus 5→5.5, Mythos 5→5.1. The 1M context window has
no price premium (`cc-autocompact-window`).

### 19.2 Anthropic modifiers and extra charges

| fact | value | source | findings |
|---|---|---|---|
| Batch | 0.5× on every token category incl. cache reads/writes; stacks with caching and data residency; not for fast mode, fallbacks, `max_tokens` 0, Managed Agents; ≤ 100k requests / 256 MB; 24 h expiry; results 29 days; batch cache hits best effort 30–98% | BATCH | `anth-batch-stacking` |
| US data residency | `inference_geo: "us"` → 1.1× on every category, 4.6+ models, 1P and Claude Platform on AWS (Foundry US Data Zone equivalent, **VERIFY**) | PRICING | `anth-modifiers-geo-fast-priority` |
| Regional cloud endpoints | Bedrock / Vertex regional and multi-region +10% vs global | PRICING, BR-PRICE, VX-PRICE | same, `vertex-claude-geo-labels`, `bedrock-price-list-api` |
| Fast mode (1P) | Opus 5.5 $8/$40; Opus 5 and Opus 4.8 $10/$50; cache multipliers apply on top; separate cache from standard speed; in Claude Code only the first enable breaks the cache | PRICING, CC-CACHE | `anth-modifiers-geo-fast-priority` |
| Stacking | modifiers stack; the exact order/rounding of every combination is unverified against invoices → exact multiplication, `stacking: assumed` where undocumented | PRICING | **VERIFY** |
| Priority Tier | no longer sold; excludes Opus 5.5, Opus 5, Sonnet 5, Fable 5.1, Mythos; commitments burn down at read 0.1, 5m 1.25, 1h 2.0, US 1.1; **absent from cost_report**; `usage.service_tier = "priority"` | PRICING, USAGEAPI | `cc-priority-legacy` |
| Web search | $10 per 1,000 searches plus result tokens; failed searches not billed | PRICING | `anth-web-search-controls` |
| Web fetch | tokens only | PRICING | same |
| Code execution | free with web_search/web_fetch 20260209+; else 1,550 free container-hours per org per month, then $0.05/hour, 5-minute minimum; appears only in cost_report | PRICING | `anth-ptc-code-exec` |
| Managed Agents | $0.08 per running session-hour; no batch | PRICING | `anth-governance-controls` |
| Claude Platform on AWS, Foundry | billed in CCU at $0.01; discounts as fewer CCU; single hourly line; no usage/cost API on Claude Platform on AWS; spend caps at list | PRICING | `claude-marketplace-ccu`, `cc-ccu-private-offers` |
| Refusals | pre-output decline not billed (counts toward rate limits); mid-stream decline bills input + streamed output; `usage.iterations` is the per-attempt billing record | FALLBACK | `fp-anth-refusal-iterations`, `cc-iterations-fallback` |
| Fallback credit | beta `fallback-credit-2026-07-01`; a retry within 5 minutes with an exactly matching body reprices the cached span as reads | FALLBACK | `fp-fallback-credit` |
| Compaction | `compact-2026-01-12` (default trigger 150,000, min 50,000), on-demand `compact-2026-09-04`; top-level usage excludes the compaction iteration; billed total = Σ iterations | COMPACT | `anth-compaction-iterations-billing` |
| Advisor | top-level usage is executor-only; `advisor_message` iterations bill at the advisor model | pricing/advisor docs | `advisor-iterations-undercount` |
| Context editing | `clear_tool_uses_20250919` default trigger 100,000, keep 3; `applied_edits[].cleared_input_tokens`; each clear invalidates the prefix from the clear point | EDIT | `anth-context-editing-not-savings` |
| Rate limits | cache reads don't count toward ITPM (except Haiku 3.5); writes do; spend-cap 429 has no `retry-after` and is terminal | RATELIM | `anth-cache-rate-limits` |
| Undocumented failure billing | client aborts, mid-stream `overloaded_error`, pre-token 429/529 on 1P | — | `fp-billing-matrix` → unknown rows (ranges) |

### 19.3 Cache mechanics

| fact | value | source | findings |
|---|---|---|---|
| TTLs | 5 min (default) and 1 h; writes 1.25× / 2×; reads 0.1× except Opus 5.5 0.05×, Fable/Mythos 5.1 0.025× | CACHING, PRICING | `anth-cache-1h-2x` |
| TTL clock | from the start of the request that writes or reads; generation time counts; reads refresh for free | CACHING | `anth-ttl-start-1h`, `fp-ttl-from-request-start` |
| Visibility | an entry is readable only after the first response token (parallel same-prefix requests all pay) | CACHING | `anth-concurrency-fanout` |
| Lookback | 20 positions per breakpoint; a run of tool_use blocks is one position, likewise tool_result (1P) | CACHING | `anth-lookback-20` |
| Breakpoints | ≤ 4; automatic caching uses one slot; longer TTLs before shorter; usage splits `ephemeral_5m/1h_input_tokens` | CACHING | `anth-ttl-start-1h` |
| Isolation | workspace (1P, Claude Platform on AWS, Foundry); organization (Bedrock, Vertex, OpenAI); **subscription** (Azure OpenAI: caches are not shared across subscriptions); Claude Code prefixes effectively per machine and directory | CACHING, CC-CACHE, Azure docs | `anth-workspace-fleet-scope`, `cache-isolation-gateways`, `azure-openai-caching` (corrected) |
| Invalidation hierarchy | tools or model → all tiers; speed, web search, citations, system → system + messages; tool_choice, disable_parallel_tool_use, images → messages; thinking/effort → messages (all tiers on some models); dropped thinking blocks change messages from that block | CACHING | `anth-invalidation-hierarchy` |
| Server tools | auto-insert 5m cache writes after tool results (not breakers) | CACHING | `anth-web-search-controls` |
| Keepalive | re-send with `max_tokens: 0` within 4 min of the previous start and every 4 min; bills a read; the **ping** is rejected with `stream: true`, structured outputs, forced tool_choice, `thinking.type: "enabled"`, and inside batches (the lane's own requests may stream); break-even idle `κ(w/r − 1)` with κ = the 4-minute ping interval: ≈ 46 min at 0.1×, ≈ 96 min Opus 5.5, ≈ 196 min Fable 5.1 (with τ = 5 min the same formula would give 57.5 min — the published values use κ) | CACHING, cost guide | `anth-ttl-choice-keepalive`, `keepalive-economics` (corrected) |
| TTL rule of thumb | 1h pays when > 1 in 20 gaps fall in 5–60 min; with no pauses 5m was 15% cheaper (Sonnet 5), 11% (Opus 5) | cost guide | `anth-fleet-benchmarks` |
| Health benchmarks | agent loops read a median 84% of input from cache; top decile ≥ 94%; investigate below 80%; Claude Code counts a miss at > 5% and ≥ 2,000 tokens | cost guide, CC-COSTS | `anth-cache-health-thresholds`, `cc-usage-likely-cause` |
| Diagnostics | header `cache-diagnosis-2026-04-07` + `diagnostics.previous_message_id`; `cache_miss_reason.type ∈ {model_changed, system_changed, tools_changed, messages_changed, previous_message_not_found, unavailable}`; `cache_missed_input_tokens` is a byte-derived magnitude, not billing; first (component-level) divergence only; 1P only; ZDR-eligible except Covered Models (Fable, Mythos 5.x) | DIAG | `anth-cache-diagnostics-beta` (corrected), `cc-miss-taxonomy-ground-truth` (corrected) |
| Cache-preserving APIs | mid-conversation `role: system` (Opus 5/5.5/4.8, Fable, Mythos; **not Sonnet 5**); `tool_addition`/`tool_removal` beta `mid-conversation-tool-changes-2026-07-01`; inline tools beta `inline-tools-2026-09-15`; per-message effort beta `mid-conversation-output-config-2026-07-01` (Fable 5.1, Mythos 5.1, Opus 5, Opus 5.5) | CACHING | `anth-cache-preserving-apis` |
| Hidden overhead | tool-use system prompt 286 (Opus 5.5 auto/none), 354/474 (Sonnet 5), 675/804 (Opus 4.7); inside provider counts | tool-use docs | `anth-hidden-overhead-tokens` |
| Images | `ceil(w/28)·ceil(h/28)`; ≤ 1,568 (standard), ≤ 4,784 (4.7+ high-res, long edge 2,576 px) | vision docs | `anth-vision-tokens` |

### 19.4 Field names adapters depend on

| source | fields | findings |
|---|---|---|
| Anthropic `usage` | `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `cache_creation.{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}`, `output_tokens`, `output_tokens_details.thinking_tokens`, `server_tool_use.{web_search_requests, web_fetch_requests}`, `service_tier`, `inference_geo`, `speed`, `iterations[].{type, model, input_tokens, cache_read_input_tokens, cache_creation_input_tokens, cache_creation.*, output_tokens}`; `diagnostics.cache_miss_reason.{type, cache_missed_input_tokens}`; `context_management.applied_edits` | `anth-usage-schema-exactness` |
| Claude Code transcripts (observed v2.1.2xx) | `type ∈ {user, assistant, system, attachment, cost-state, …}`; `uuid`, `parentUuid`, `sessionId`, `requestId`, `timestamp`, `isSidechain`, `agentId`, `version`, `entrypoint`, `effort`, `advisorModel`, `cwd`, `gitBranch` (dropped), `attributionAgent|Skill|McpServer|McpTool|Plugin`, `isApiErrorMessage`, `apiErrorStatus`, `perTurnEffort` (per-message effort; ≈ 7.6k assistant entries in the corpus), `quotaLimits.{status, rateLimitType, isUsingOverage, overageStatus, overageDisabledReason, resetsAt, unifiedRateLimitFallbackAvailable}` (sparse; **VERIFY** semantics); `message.{id, model, usage, stop_reason, content[].type, diagnostics, input_transformations[].type, context_management.applied_edits}`; `system.subtype ∈ {compact_boundary (compactMetadata.{preTokens, postTokens, durationMs, trigger, cumulativeDroppedTokens}), model_refusal_fallback (originalModel, fallbackModel), api_error (error.status, error.connection, retryAttempt, maxRetries, retryInMs)}`; user `origin.kind`, `isMeta`, `isCompactSummary`, `message.content[].{type, tool_use_id, is_error, content}`, `toolUseResult.{totalTokens (rollup, not spend), persistedOutputSize}`; subagent meta `{agentType, model, parentAgentId, spawnDepth, toolUseId}`; workflow meta `{agentType, spawnDepth, workflowPhase}`; retention `cleanupPeriodDays` (default 30). Format is internal and version-dependent. | `cc-transcript-format`, `cc-import-dedup-message-id`, empirical 02/03/04/12 |
| Claude Code OTel | metrics `claude_code.token.usage{type=input\|output\|cacheRead\|cacheCreation}`, `claude_code.cost.usage`; event `claude_code.api_request {model, request_id, client_request_id, duration_ms, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, cost_usd, speed, effort, query_source}`; `claude_code.api_error`; `claude_code.tool_result {tool_name, success, duration_ms, tool_result_size_bytes}`; beta spans `claude_code.llm_request {ttft_ms, agent_id, parent_agent_id, stop_reason}`, `claude_code.tool {result_tokens}`; attributes `session.id`, `organization.id`, `user.id`, `user.account_uuid`, `user.email` (not populated for API-key, Bedrock, Vertex, Foundry), `agent.name`, `skill.name`, `plugin.name`, `mcp_server.name`; `OTEL_RESOURCE_ATTRIBUTES`; opt-in `app.entrypoint`, `vcs.*` (≥ 2.1.269) | CC-OTEL; `cc-otel-schema` (corrected: `ttft_ms` only on the beta span) |
| Usage report | `GET /v1/organizations/usage_report/messages`; buckets 1m (≤ 1,440), 1h (≤ 168), 1d (≤ 31); group by api key, workspace, model, service tier, context window, inference geo, speed; uncached, cache read, `cache_creation` 5m/1h, output, `server_tool_use.web_search_requests`; Admin key; not on Claude Platform on AWS | USAGEAPI; `anth-admin-usage-cost-api` (param spellings **VERIFY**) |
| Cost report | `GET /v1/organizations/cost_report`; daily; USD cents as **decimal strings**; group by workspace or description (model, cost_type, token_type, inference_geo); Priority Tier excluded; code execution only here; default workspace `workspace_id = null` | USAGEAPI (`cost_type`/`token_type` enumerations **VERIFY** from a recorded page) |
| Claude Code Analytics | `GET /v1/organizations/usage_report/claude_code?starting_at=YYYY-MM-DD`; per user per day: sessions, LOC, commits, PRs, tool accept/reject, `model_breakdown[].tokens.{input, output, cache_read, cache_creation}`, `estimated_cost.amount` (cents); ~1 h delay; ≤ 1,000 per page; excludes Bedrock/Vertex/Foundry/Claude Platform on AWS | CCAPI; `cc-admin-analytics-apis` (corrected) |
| Enterprise Analytics | `/v1/organizations/analytics/{usage_report, user_usage_report, cost_report, user_cost_report}`; `read:analytics`; group by product, model, context_window, speed, inference_geo, rbac_group_id (+ cost_type, token_type on cost endpoints); 1m/1h/1d; from 2026-01-01; ≤ 31 days per query; `amount` (post-discount, pre-credit) and `list_amount` in cents decimal strings; revisable 30 days; 60 rpm | EAAPI; `cc-anthropic-discount-visibility` |
| OpenAI usage | Responses `usage.input_tokens` (inclusive), `input_tokens_details.{cached_tokens, cache_write_tokens}`, `output_tokens`, `output_tokens_details.reasoning_tokens`, `service_tier` (served); Chat `prompt_tokens_details.cached_tokens`, `completion_tokens_details.{reasoning_tokens, accepted_prediction_tokens, rejected_prediction_tokens}`; Admin usage buckets `{input_uncached_tokens, input_cached_tokens, input_cache_write_tokens, output_tokens}` by project, user, api key, model, batch, service tier; Costs by project, line item | OAI-CACHE, OAI-PRICING; `oai-usage-inclusive`, `oai-admin-usage-costs` (paths **VERIFY**) |
| OTel GenAI | `gen_ai.usage.input_tokens` (includes cache), `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_write.input_tokens` (≥ 1.42; legacy `cache_creation`), `gen_ai.usage.output_tokens`, `gen_ai.usage.reasoning.output_tokens`, `gen_ai.provider.name` (legacy `gen_ai.system`), `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.response.id`, `gen_ai.conversation.id`, `openai.response.service_tier`, `error.type`; status Development | OTEL-GENAI; `otel-genai-semconv`, `ent-otel-genai-semconv` |
| OpenInference | `openinference.span.kind` (LLM, AGENT, CHAIN, TOOL…), `llm.model_name`, `llm.provider`, `llm.token_count.{prompt, completion}`, `llm.token_count.prompt_details.{cache_read, cache_write}`, `llm.token_count.completion_details.reasoning` | `otel-openinference-importer` |
| OTLP/JSON | int64 (`intValue`, `timeUnixNano`) encoded as JSON **strings**; file exporter format | OTel spec (**VERIFY** with fixtures) |
| Bedrock Converse | `usage.{inputTokens, outputTokens, cacheReadInputTokens, cacheWriteInputTokens, cacheDetails[{ttl, inputTokens}]}`; invocation logs `identity.arn`, `requestMetadata`, cache counts only inside the logged body | BR-CACHE; `bedrock-cache-semantics`, `bedrock-attribution` |
| Gateway hint headers | `x-claude-code-session-id`, `x-claude-code-agent-id`, parent agent id; with `CLAUDE_CODE_GATEWAY_HINT_HEADERS=1`: request class and agent type | CC gateway docs | `cc-gateway-attribution-caps` |
| OpenAI cache diagnostics | request `prompt_cache_options.comparison_response_id`; response `prompt_cache_diagnostics` (`cache_hit`, `cache_miss`, `comparison_response_not_found`, `unavailable`) with a miss reason ∈ {model, prompt_cache_key, tools, text_format, reasoning_effort, verbosity, context_compacted, input, service_tier} and `cache_missed_tokens`; Responses API, GPT-5.6+, free | OAI-CACHE; `oai-cache-diagnostics` (exact field paths **VERIFY**) |
| Claude Code headless / CI / Agent SDK | claude-code-action writes all SDK messages to `$RUNNER_TEMP/claude-execution-output.json` (output `execution_file`), sets `CLAUDE_CODE_ENTRYPOINT=claude-code-github-action`; `claude -p --output-format json` returns `total_cost_usd` + per-model breakdown; `stream-json` gives per-step usage (dedupe by message id; per-step `output_tokens` is a placeholder; totals on the `result` message); `result.usage` excludes subagents, `total_cost_usd`/`modelUsage` include them; resumed sessions carry earlier spend; crash results may be zeroed; `total_cost_usd` is a client estimate; `parent_tool_use_id` links subagent steps | headless docs, Agent SDK cost tracking; `ci-headless-ingest`, `sdk-accounting-pitfalls` (message key names **VERIFY**) |
| AWS CUR 2.0 | CSV/CSV.gz (or Parquet: unsupported); `line_item_usage_start_date`, `line_item_product_code` (AmazonBedrock…), `line_item_usage_type` (model, token direction, cache read/write, TTL, geo `…_global_…` vs regional, batch; units mix 1K and 1M tokens), `line_item_usage_amount`, `pricing_unit`, `line_item_unblended_cost`, `line_item_net_unblended_cost`, `line_item_usage_account_id`, `line_item_iam_principal` (since 2026-04-17), `iamPrincipal/` tags; hourly/daily, no request ids; log-derived cost ignores discounts/commitments/PT | AWS docs, Price List offer files; `ent-cloud-gateway-attribution`, `bedrock-attribution`, `bedrock-price-list-api`, `cc-reconciliation-matrix` (usage-type map **VERIFY**) |
| GCP billing export | BigQuery table exported as CSV/JSONL: `service.description`, `sku.id`, `sku.description`, `usage_start_time`, `usage.amount`, `usage.unit`, `cost`, `credits[].amount`, `project.id`, `labels[]` (≤ 32 per partner-model request; forwarded from `rawPredict` labels), `location.region` | Vertex labels docs; `vertex-claude-geo-labels`, `cc-reconciliation-matrix` (SKU map **VERIFY**) |

### 19.5 Claude Code defaults and managed settings

| key / default | fact | source | findings |
|---|---|---|---|
| Cache TTL default | 5m on API keys, cloud providers and usage credits; 1h for the main conversation on a subscription within plan; subagents, forks, compaction 5m; the Claude apps gateway cannot use 1h; through other gateways 1h needs the `anthropic-beta` header forwarded | CC-CACHE | `cc-ttl-policy`, `cc-ttl-managed-settings`, `cc-apps-gateway-routing-tax` |
| TTL keys | from v2.1.242: `promptCacheTtl`, `subagentPromptCacheTtl`, `CLAUDE_CODE_PROMPT_CACHE_TTL`, `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL`, `ENABLE_PROMPT_CACHING_1H`, `FORCE_PROMPT_CACHING_5M`, per-subagent `experimental.cacheTtl`; all deliverable through the managed `env` block (keys confirmed; value formats **VERIFY**) | CC-SETTINGS, CC-ENV | `cc-ttl-managed-settings`, `cc-ttl-policy` |
| Auto-compaction | ~967K default window on 1M-context models (Sonnet 5, Fable, Opus 4.7+ on the API); `autoCompactWindow` 100,000–1,000,000; `CLAUDE_CODE_AUTO_COMPACT_WINDOW`; `CLAUDE_CODE_DISABLE_1M_CONTEXT`; the `--autocompact` flag is not preempted by managed settings (enforce via the managed env block) | CC-MODEL | `cc-autocompact-window` (corrected) |
| `modelPricing` | managed-only, ≥ 2.1.242; `multiplier` + per-model overrides `{input, output, cacheRead, cacheWrite}`; `cacheWrite` covers 5m and 1h; without it every Claude Code dollar is a list estimate (exact JSON shape **VERIFY**) | CC-SETTINGS, CC-COSTS | `cc-list-price-estimates` |
| Effort / fast | `maxEffortLevel` (≥ 2.1.267, every provider); default-effort key `effortLevel` (**VERIFY**); `fastModePerSessionOptIn`, `CLAUDE_CODE_DISABLE_FAST_MODE`; `/effort` Enter saves the level as default and fast mode persists across sessions without the opt-in; effort changes keep the cache in Claude Code only on Opus 5.5 and Fable 5.1 with an API key or subscription (not Bedrock/Google Cloud), v2.1.260+; on the API a top-level effort change invalidates the messages cache | CC-SETTINGS, CC-CACHE, CACHING | `cc-managed-settings-levers`, `cc-sticky-escalation`, `cc-cache-breakers` (corrected), `anth-effort-rerun-budgets` |
| Models | managed default model key `model` (**VERIFY**); `availableModels` + `enforceAvailableModels`, `ANTHROPIC_DEFAULT_*_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL`; opusplan switches models on each plan-mode toggle (each switch rebuilds the cache) | CC-SETTINGS, CC-MODEL | `cc-managed-settings-levers`, `cc-model-effort-mix` |
| Fleet default changes (evidence, not product output) | Datadog: default model Opus 4.8 → Sonnet 4.6 cut cost 36.7% for an 8% weighted proficiency loss on 140+ internal evals; default effort high → medium saved a further large share — shown only as cited evidence for why the levers are gated by `ab`/`measure` | competitor case study | `org-defaults-datadog`, `datadog-1m-month-case` |
| Seats vs usage | Team Standard $20/month annual ($25 monthly), Premium $100 ($125); Enterprise $20/seat + usage at API rates; usage inside the seat allowance (5-hour and weekly) is not metered in dollars; overage is usage credits at API rates; seat-based plans show only usage credits in Analytics; org-level spreads 2–3.5× between billing paths | CC-COSTS | `cc-seat-vs-usage`, `billing-path-optimizer` (corrected), `cc-anthropic-discount-visibility` |
| Context hygiene | `bashOutputMaxChars` (default 30,000), `MAX_MCP_OUTPUT_TOKENS` (warn 10K, cap 25K), `skillListingBudgetFraction`, `claudeMdExcludes`, `ENABLE_TOOL_SEARCH` (a non-first-party `ANTHROPIC_BASE_URL` disables tool search by default), `workflowSizeGuideline` (advisory only) | CC-SETTINGS, CC-ENV | `cc-tool-output-bloat`, `cc-gateway-cache-strip` |
| Retention / version | `cleanupPeriodDays` (default 30), `requiredMinimumVersion` | CC-SETTINGS | `ent-local-transcripts` |
| Delivery | server-managed settings polled hourly and applied org-wide (no per-group); MDM refresh every 30 min; files reload on change; the self-hosted Claude apps gateway delivers settings per IdP group | CC-SMS | `stepped-wedge-mdm` (corrected) |
| Hooks | SessionStart receives resume-cost fields `context_tokens`, `prompt_cache_likely_expired`, `estimated_cache_write_usd` (exact shape **VERIFY**) | hooks docs | `channel-code-review-hooks` |
| Headless / CI | claude-code-action writes `$RUNNER_TEMP/claude-execution-output.json`, entrypoint `claude-code-github-action`; `--exclude-dynamic-system-prompt-sections`; `claude -p --output-format stream-json` (dedupe by message id) | CC docs | `ci-headless-ingest`, `ci-cross-run-cache` |
| Baselines | ~$13 per developer per active day; $150–250 per developer per month; 90% of users under $30 per active day | CC-COSTS | `cc-enterprise-baseline` |

### 19.6 OpenAI, cloud channels and other providers

| fact | value | source | findings |
|---|---|---|---|
| GPT-5.6+ caching | released 2026-07-09; min 1,024 visible tokens; writes 1.25×, reads 0.1×; `prompt_cache_options.ttl = "30m"` only; free refresh on reuse; implicit mode 1 automatic + ≤ 3 explicit breakpoints; explicit ≤ 4; matching considers the first 2 and latest 50 explicit breakpoints | OAI-CACHE | `oai-56-explicit-cache` (corrected) |
| gpt-5.6-sol | released 2026-07-09 (launch price **VERIFY** → row disabled); repriced 2026-08-21 to $4 input / $0.40 cached / $5 cache write / $20 output, **promotional "at least through November 21, 2026"** (row effective_to 2026-11-22, then unpriced until re-verified); > 272K whole-request rows $8 / $0.80 / $10 / $30 | OAI-PRICING, OpenAI changelog 2026-08-21 | `oai-batch-longctx-residency`, `cc-timing-calendar`, `price-feed-crosscheck` (OpenRouter's $2/$10 listing is non-authoritative) |
| OpenAI tiers | flex = batch rates (0.5×), flex 429s not charged; fast (renamed from priority 2026-07-30) 2×; price at the **served** tier; regional processing +10% for models released ≥ 2026-03-05 | OAI-FAST, OAI-FLEX | `oai-service-tiers` |
| OpenAI pre-5.6 | no write fee; cached counts rounded down to 128; retention `in_memory` vs `24h` (default for non-ZDR since 2026-05-29) | OAI-CACHE | `oai-retention-defaults` |
| Bedrock | Converse `inputTokens` excludes cache; checkpoint minimums 512 (Opus 5/5.5), 1,024 (Sonnet 5, Opus 4.8), 4,096 (Opus 4.7, Haiku 4.5); no caching in Bedrock batch; tiers priority 1.75×, flex 0.5× (served tier in the response); Opus 5 input $5.00 global vs $5.50 regional, read $0.50 vs $0.55, 1h write $10 vs $11, batch input $2.50 | BR-CACHE, BR-PRICE | `bedrock-cache-semantics`, `bedrock-price-list-api`, `bedrock-service-tiers` |
| Bedrock id prefixes | `global.` → global rate; in-region and geo-prefixed profiles → regional (+10%) | BR-PRICE | **VERIFY** per profile against Price List `usagetype` |
| Vertex | regional/multi-region +10%; model id in the URL; no Message Batches and no Usage/Cost API for Claude; labels flow to the billing export; Opus 5.5 batch listed $2.50/$12.50 vs $2/$10 on 1P | VX-PRICE | `vertex-claude-geo-labels` (batch **VERIFY**) |
| Azure OpenAI | in_memory default for gpt-5.4 and older; caches isolated per subscription; latest-50-breakpoint matching like OpenAI; PTU-M lacks breakpoints and cache_write_tokens; bills processed requests incl. 400 content-filter and 408 | Azure docs | `azure-openai-caching` (corrected), `fp-billing-matrix` (Data Zone premium **VERIFY**) |
| Not in v0.2 (documented for 0.3) | Gemini: `promptTokenCount` includes cached, thoughts outside candidates billed as output, 3.8 Flash $0.75 → $1.50 on 2027-01-01; xAI: `cost_in_usd_ticks` (1 USD = 10¹⁰ ticks) is billed truth, ≥ 200K doubles all rates; DeepSeek: hit/miss fields, peak 2×; Mistral: 64-token cached blocks; OpenRouter: `usage.cost` billed truth; Cursor $0.25/MTok fee incl. cached/BYOK; Copilot AI credits (`ai_credits_used` is not a bill); Codex `TokenUsage` convention unverified | vendor docs | `gemini-usage-semantics`, `xai-cost-ticks`, `deepseek-offpeak-cache`, `mistral-optin-cache`, `openrouter-accounting`, `cc-cursor-token-rate`, `copilot-ai-credits`, `codex-usage-format` |

### 19.7 Measurement and integrity standards

| fact | value | source | findings |
|---|---|---|---|
| Calibration thresholds | FEMP M&V 4.0 Table 4-2 (from ASHRAE Guideline 14-2015 §5.3.3.3.10): monthly NMBE ±5%, CV(RMSE) ≤ 15%; hourly ±10%, ≤ 30% | FEMP | `ashrae-calibration-gate` (corrected) |
| Savings definition | adjusted baseline − post (± adjustments); invoice reconciliation ≈ IPMVP Option C, calibrated replay ≈ Option D | FEMP | `mv-adjusted-baseline` |
| Estimators | imputation DiD for staggered adoption; never TWFE or naive pre/post; CUPED variance factor (1 − ρ²) | literature | `staggered-did`, `cuped` |
| Power / pitfalls | MDE ~13% at 1,000 developers, ~4–5% at 10,000; spend-targeted first waves inflate savings ~17–34% (regression to the mean); individual randomization inside a workspace biased −28% | research sims | `heavy-tails-power`, `rtm-targeting`, `cache-interference-cluster` |
| DSSE | PAE = `"DSSEv1" SP len(type) SP type SP len(body) SP body` | DSSE | `signed-receipts` |
| Canonical JSON | integer-only ASCII-key subset is byte-identical to RFC 8785 JCS | JCS | `signed-receipts` |
| Signing | `ssh-keygen -Y sign -f KEY -n NS FILE`; `ssh-keygen -Y verify -f allowed_signers -I ID -n NS -s FILE.sig < FILE` (OpenSSH ≥ 8.1) | SSHKEYGEN | `signed-receipts` |
| FOCUS | 1.4 ratified 2026-06-04 (2 datasets, 47 columns); custom columns MUST use `x_`; ServiceCategory includes "AI and Machine Learning"; Allocated* columns since 1.3; 1.5 (target 2026-12-03, not built) adds model identity in `SkuPriceDetails` | FOCUS | `ent-focus-1-4` (corrected), `focus-15-ai-columns` |
| Supply chain | PEP 740 attestations (on by default in `gh-action-pypi-publish` ≥ 1.11), PEP 770 SBOM location `.dist-info/sboms`, SLSA 1.2, immutable releases GA 2025-10-28, EU CRA manufacturer reporting since 2026-09-11; LiteLLM PyPI compromise March 2026 | PEPs, SLSA, GitHub | `ent-provenance-sbom`, `ent-eu-cra`, `ent-supply-chain-incidents` |
| Pseudonymization | pseudonymized data remains personal data; keyed MACs held by the controller | EDPB Guidelines 01/2025 | `ent-privacy-by-default` |
| Works council / law | BetrVG §87(1)6 (AI tools as monitoring), GDPR Art. 88, EU AI Act Annex III 4(b) → team-level k ≥ 5, self-view, no emotion inference, DPIA | research | `labor-law-constraints`, `aggregation-k5` |

### 19.8 Verify-before-build checklist (owner package in brackets)

The F-CORE facts task handles every item marked [facts] once, in wave 0, and writes the outcome into
`core/facts.json`; packages re-check only what they transcribe beyond facts.json.

1. Every §19.1 row against PRICING on the day of transcription; Opus 4.1/4, Sonnet 4, Haiku 3.5 minimums
   and prices or ship them disabled [facts; RATES for rows outside facts.json].
2. Fable 5.1 / Mythos 5.1 effective date 2026-09-01 (enabled) [facts].
3. Bedrock inference-profile prefixes → global vs regional rate, from Price List `usagetype` [RATES; F-CORE
   for `normalize_model` rule text].
4. Modifier stacking combinations (ship `stacking: assumed` where undocumented) [RATES].
5. `modelPricing` exact JSON schema [facts; PLAN keeps the key VERIFY until done].
6. `cost_report` `cost_type`/`token_type` enumerations and Enterprise Analytics field names from recorded
   pages; build `COST_TYPE_MAP` [ADMIN for field names, RECON for the map]. **Release gate:** at least one
   real, redacted usage_report + cost_report page pair supplied by the adopting organization is added as a
   fixture before 0.2.0 GA; until then `reconcile` prints `dq.recon_schema_unverified`.
7. Claude Code Analytics token convention (exclusive vs inclusive) with a fixture [ADMIN].
8. Claude Code OTel attribute names at the enterprise's Claude Code version (`claude_code.api_request`),
   OTLP/JSON int64-as-string encoding, OpenAI `prompt_cache_diagnostics` field paths [TELEM].
9. Vertex Opus 5.5 batch rate; Azure Data Zone premium; Foundry US Data Zone multiplier [RATES].
10. Pagination field names of the Admin/Analytics APIs [RECON].
11. FOCUS 1.4 column names and mandatory/nullable status [facts; OUT].
12. Claude Code managed-settings value formats for `promptCacheTtl`, `subagentPromptCacheTtl`, TTL env vars,
    `hooks.SessionStart` shape and hook input field names; LiteLLM injection-point TTL key [facts; PLAN].
13. ccusage `--json` daily/session field names [OUT].
14. Models on which thinking/effort also invalidate tools/system (`effort_invalidates_all_tiers_models`,
    empty until verified) [F-SEM].
15. `quotaLimits` semantics (which states mean overage / usage credits) on a real subscription transcript [CC].
16. Headless message keys (`result.usage`, `modelUsage.*`, `parent_tool_use_id`, execution-file container
    shape) against a real claude-code-action execution file [CC].
17. CUR 2.0 Bedrock usage types (and units) and GCP Claude SKU ids → (model, bucket, scope, tier) against the
    Price List offer file and the Cloud Billing catalog; unverified rules ship disabled [RECON].
18. Managed default-model (`model`) and default-effort (`effortLevel`) setting keys [facts; PLAN].
19. gpt-5.6-sol launch price 2026-07-09 → 2026-08-20 [RATES].

---

## 20. Deferred and cut (v0.3 or never), each with its reason

| item | decision | reason |
|---|---|---|
| Recording HTTP proxy / gateway mode | cut (v0.3 at the earliest) | puts Token Bill in the inference path and adds a network listener to the security review; the recorder and collectors capture the same data |
| OTLP HTTP receiver | cut | listening socket; the OpenTelemetry Collector's file exporter plus `ingest` is enough (reference config in `docs/FLEET.md`) |
| `watch` daemon, webhook/alert sinks | cut | long-running network services; enforcement belongs to gateways and the Console |
| Anomaly detection, forecasts, budgets, cap recommendations | deferred | commodity features of FinOps platforms and gateways; they do not remove dollars |
| Hosted dashboard, SSO, RBAC | cut | a server product; static HTML + FOCUS feed existing BI tools |
| Any LLM call inside Token Bill | never | nondeterminism, data egress, cost |
| Live `count_tokens` attribution; live rebaseline recount | deferred | needs network and API keys; the content-free `model.routing` rebaseline kind (§10.2) reports the tokenizer/thinking/prompt drift offline |
| Live effort/model sweeps; live gateway/MCP probes | deferred | network, keys and spend; `ab` evaluates offline recordings |
| Compression/masking/context-editing **transform replays** | deferred | trajectory effects (extra turns, re-reads) cannot be predicted content-free (`token-not-cost`: r = 0.15); `ab` verifies vendor claims; `cache.rebuild` edit-churn prices observed edits |
| Router/cascade simulators | deferred | need quality labels per request |
| Commitment and capacity (PTU/GSU) optimizers | deferred | contract data is not machine-readable; per-provider capacity semantics |
| **Seat-vs-usage optimizer** (per-developer plan assignment) | deferred to v0.3 | seat allowances are not published in dollars (only 5-hour/weekly limits), the only org-level evidence is 2–3.5× from anecdotes, and per-developer plan recommendations are individual-level analysis that works councils scrutinize. v0.2 ships the inputs finance needs: `list_equivalent` basis for allowance usage, overage share from `quotaLimits`, and per-team, per-billing-path list-equivalent spend per active developer-month (k ≥ 5) in showback (§14.5) |
| Embeddings module; semantic or plan caching advice | cut | needs content; quality risk |
| Hidden-token audits | deferred | need live counting |
| Callaway–Sant'Anna, switchbacks, confidence sequences, Wilcoxon, empirical-Bayes RR database | cut | judges: imputation DiD covers staggered adoption; looks are pre-registered; RR stored per receipt and displayed |
| SDID / synthetic control with donor pools | deferred | needs cross-org or cross-workspace donor data; v0.2's event-study ITS gives a MEASURED path for org-wide changes (§13.3) |
| Judge-share detector | cut | needs judge-call fingerprints no source provides |
| Dev/test bleed detector | cut | needs environment tags that are not reliably present |
| Non-streaming double send (D8) | cut | low measured share; recorder attempts cover it |
| Bedrock quota reservation (D9) | cut | needs CloudWatch quota data outside the ledger |
| Framework detectors W01–W03 | cut | framework telemetry defects are data-quality notes (`dq.sum_check_failed`, `dq.convention_mismatch`) |
| Router audit (R09); cost-per-pass detector (R07) | cut | needs routing labels; cost per success lives in `ab` |
| Workspace fragmentation (C12) | cut | low value; workspace scope is visible in the org scan |
| Harness-upgrade detector (C16) | cut | covered by the `client-upgrade` miss sub-cause |
| Reminder inject/delete (C24), image eviction (C25) | cut | covered by miss-by-cause (`messages-changed`, `IMAGE_EVICTION` event → compaction cause) |
| OpenAI-specific breakers (C26/C27) and the OpenAI block-level rule table | deferred | GPT-5.6 explicit-breakpoint semantics differ; usage-level replay and `prompt_cache_diagnostics` cover OpenAI in v0.2 |
| **Review-trigger multiplier** (review on every push) | deferred to v0.3 | needs PR event history (synchronize events per PR, run attempts), which no content-free v0.2 source reads; v0.2 reports `automation` ci-run-cost ($ per CI run by repo/workflow beside the $15–25 per review benchmark) and recommends trigger policy in its fix text |
| No-op fires; velocity alerts (X07) | cut | needs task outcomes; velocity alerts are individual monitoring |
| Duplicate-read counter | cut | 0.6% of reads in the corpus |
| `compress-econ` command and r*/K* panel | cut | K* survives only inside `cache.rebuild` edit-churn |
| Managed Code Review / GitHub billing ingestion | deferred | no verified stable export; execution files cover claude-code-action runs |
| Adapters/rates for Gemini, xAI, DeepSeek, Mistral, OpenRouter, LiteLLM SpendLogs, Codex, Cursor, Copilot | deferred | depth over breadth; facts documented in §19.6 for v0.3 (emitting LiteLLM config stays) |
| FOCUS 1.5 columns | deferred | not ratified (target 2026-12-03) |
| OTLP metrics export | deferred | scope; FOCUS + result JSON feed existing tools |
| Per-user productivity tables; persisted per-user cost × productivity joins | never | works-council/BetrVG §87(1)6 risk; team level only |
| Unit economics per commit/PR as showback KPIs | cut | gaming risk; cost per active developer-day only; merged PRs only as a verification guardrail |
| Jurisdiction presets in the privacy pack | cut | one DPIA + works-council template |
| Keepalive for Claude Code | never | Claude Code cannot be configured to send pings |
| Nudge channels beyond the SessionStart hook | deferred | behavioral, unmeasured |
| Parquet CUR | deferred | the stdlib cannot read Parquet; CSV export documented |
| zstd OTLP files on Python < 3.14 | deferred | no stdlib zstd before 3.14; document `compression: none` |
| Claude Code transcript fingerprint tier | deferred | transcripts do not carry full request payloads (D40) |
| Signed container image (distroless, cosign) | deferred to v0.3 | adds a base-image supply chain; v0.2 ships an attested, reproducible, zero-dependency wheel |
| `privacy dpia` / `privacy-pack` generator commands | cut | static docs suffice |
| `store rebuild-*` maintenance verbs; `pricing contract --from-reconcile` | cut | `ingest --renormalize`, `bill --reprice` and `reconcile --suggest-contract` cover them |

---

## 21. Build protocol (binding on every work package)

1. **Waves.** Wave 0: F-CORE alone (contracts, scaffolding, goldens, the facts task). Wave 1: F-SEM ∥ F-KIT.
   Wave 2: RATES, CC, TRACE, TELEM, ADMIN, RECON, STORE, REPLAY, BLOCK, SYNTH-ORACLE, SYNTH-FLEET, VERIFY,
   DETECT-CACHE, DETECT-OTHER, PLAN, OUT, WIRING in parallel. Merge gate 1. Wave 3: CLI-LEDGER ∥ CLI-SAVINGS. Merge
   gate 2. Wave 4: integration. Adversarial review.
2. **Ownership.** Each package creates or edits only the files listed as its own in `OWNERSHIP.toml`
   (created by F-CORE; mirrors Appendix O). `scripts/check_ownership.py --package <ID> --base <ref>`
   must pass on the branch (CI job `ownership.yml`, active from wave 0). FROZEN files (§2.2) are never
   edited. Docstring-only `__init__.py` files of shared subpackages belong to F-CORE; register
   implementations only through the string maps of `core/registry.py` (already listing your dotted paths —
   implement the class at exactly that path).
3. **Frozen contracts.** `tokenbill/core/*` is read-only after wave 1. If a contract is wrong or missing,
   do not work around it silently: write `CONTRACT-CHANGE-<ID>.md` in your test directory (what, why,
   proposed signature) and implement against the current contract; field/signature changes are applied by
   the contract owner between waves only. **Contract owner** = the F-KIT agent (kept available through wave
   3): it may hot-fix *bugs* in `core/*` implementations and fakes (never fields or signatures) mid-wave on a
   `core-hotfix/<n>` branch that every active worktree merges; each hotfix is time-boxed to 2 hours and
   announced to all builders.
4. **Tests.** Put tests under `tests/v2/<your area>/` (with an `__init__.py`) and fixtures under
   `tests/v2/fixtures/<your area>/`. Default (prepend) import mode; import test helpers only within your
   own area; cross-package fixtures are checked-in files or the producing package's public API, never
   another area's test modules. Existing `tests/test_*.py` (207 tests) must stay green on your branch. Use
   the foundation fakes (`FlatRates`, `FakePricer`, `MemoryStore`, `FakeReplayer`, builders, `CANARY`)
   instead of sibling packages; call the conformance helpers (`assert_*_conforms`) for every implementation
   you provide. A test that needs a sibling package's real implementation is a **gate test**: mark it
   `@pytest.mark.gate` and guard it with `pytest.importorskip("<module>")`; it must pass at merge gate 1. The
   orchestrator runs a **daily canary merge** of every finished branch into `v0.2-canary` and runs all gate
   tests there, routing failures to the owning builder and, for core issues, to the contract owner; builders
   may merge the canary into a scratch branch to run their own gates locally (never committed). Markers:
   `perf` (nightly, full-size budgets; provide a 1/10-size PR variant), `slow`, `gate`, `needs_ssh_keygen`,
   `local_corpus`. No network: the autouse socket guard fails any non-loopback connection.
5. **Definition of done.** Full type hints and docstrings on public functions; ruff clean; ≥ 90% line
   coverage of owned modules (`coverage run -m pytest tests/v2/<area>`); hypothesis property/fuzz tests for
   every parser you own (only `TokenbillError` subclasses may escape); deterministic outputs; the acceptance
   tests of your brief; no float in money modules (§2.4); canary absent from every output you produce; a
   short `tests/v2/<area>/README.md` listing fixtures and their provenance (synthetic; schema from §19.4;
   never real transcripts) and any unverified facts.
6. **Facts.** Read prices, multipliers, setting keys and FOCUS columns from `core.facts` when they are there.
   Before transcribing any other fact, re-verify it against the primary source cited in §19 (if you have
   network access in your build environment) and record `verified_on`; if you cannot verify it, ship it
   disabled/unpriced or as commented guidance and list it in your README under "unverified".
7. **Style.** Existing v0.1 conventions (§2.4); no new runtime dependency; dev dependencies are pytest, ruff,
   hypothesis, coverage only (F-CORE adds them and relocks `uv.lock`; `.hypothesis/` is git-ignored).

---

## Appendix A. Hand-computed fixtures (binding for REPLAY, SYNTH and DETECT tests)

Rates: Opus 5.5 input $4, output $20, read $0.20, 5m write $5, 1h write $8 (per MTok). Sonnet 5: $2 / $10,
read $0.20, 5m $2.50, 1h $4.

**A.1 5m → 1h on an API-key lane (must show savings).** Opus 5.5 main lane, SDK or Claude Code, 4 requests,
gaps 420 s, `U = 0`, `O = 500` each. Observed (5m): `T = 100k, 102k, 104k, 106k`, every request a full
write (`W5 = T`, `R = 0`). Transitions 1–3: `E = 100k, 102k, 104k`, `M = E` → miss events, cause
`ttl-expiry` (420 s > 310 s, not ambiguous). Observed cost = 412k × $5/M + 2k × $20/M = **$2.10**. Policy
`ttl=1h`: request 0 writes 100k at $8/M = $0.80; requests 1–3 read `E` (100k, 102k, 104k → $0.0612 total)
and write the appended 2k each at $8/M ($0.048 total); output $0.04 → **$0.9492**; saving **$1.1508**.

**A.2 Bursty lane (must choose 5m).** Same lane with gaps 30 s billed as hits: request 0 `W5 = 100k`;
requests 1–3 `R = T_{i−1}`, `W5 = 2k`. Observed = 106k × $5/M + 306k × $0.20/M + $0.04 = **$0.6312**; `ttl=1h`
re-rates writes to $8/M → **$0.9492** (+$0.318) ⇒ recommend 5m.

**A.2b Bursty lane billed at 1h (the TTL advisor must recommend 5m).** The A.2 lane billed with 1h writes
(`W1` instead of `W5`): observed = 106k × $8/M + $0.0612 + $0.04 = **$0.9492**; `ttl=5m` (no hit→miss flips:
every gap is 30 s ≤ 300 s) = **$0.6312**; saving **$0.318** ⇒ `ttl-5m-recommended`. Because $0.318 is below
the default `min_usd` ($1.00), detector tests on this lane pass `thresholds={"min_usd": "0.10"}`. The A.2
lane itself (already billed at 5m) must produce **no** TTL recommendation (status quo is optimal). The
premium test (§6.9 case 4 − case 1 = $0.068) likewise passes `{"min_usd": "0.01"}`.

**A.3 1h → 5m (hit → miss).** The A.1 lane billed at 1h (cost $0.9492) under `ttl=5m` with `S = 0`: requests
1–3 become full writes at $5/M → **$2.10** (+$1.1508).

**A.4 Keepalive (SDK lane, κ = 240 s, M = 3,600 s).** The A.1 observed lane: each 420 s gap sends `n =
ceil(420/240) − 1 = 1` ping reading `P_{i−1}` (100k, 102k, 104k → $0.0612) and is warm (240 + 300 ≥ 420):
reads $0.0612 + writes 3 × 2k × $5/M = $0.03; total = $0.50 + $0.0612 + $0.03 + $0.0612 + $0.04 =
**$0.6924**. A 20-minute gap sends **4** pings (warm: 4·240 + 300 = 1,260 ≥ 1,200); a 2-hour gap sends
`min(29, 15) = 15` pings and is cold (15·240 + 300 = 3,900 < 7,200). A Claude Code lane is never pinged.

**A.5 Cold resume.** Opus 5.5 main lane billed at 1h, context 500,000, gaps [30 s, 2 h, 90 s]: exactly one
event (the 2 h gap); `cost_observed` = 500,000 × $8/M = **$4.00** (EXACT); premium vs warm read = 500,000 ×
($8 − $0.20)/M = **$3.90** (ESTIMATED).

**A.6 Compaction window (negative saving example).** Sonnet 5 main lane, TTL 5m, gaps 30 s, `w = 400,000`,
`S_c = 20,000`, outputs 1k. Observed: `T0 = 300k` (W 300k), `T1 = 450k` (R 300k, W 150k), `T2 = 500k` (R 450k,
W 50k) → 500k × $2.50/M + 750k × $0.20/M + 3k × $10/M = **$1.43**. Policy: request 0 unchanged ($0.76);
request 1 triggers: compaction call reads 300k ($0.06), writes 150k ($0.375), outputs 20k ($0.20); the
request becomes `T' = 20k + 150k`, written at $2.50/M ($0.425) + output $0.01; `removed = 280k`; request 2:
`T_eff = 220k`, `R' = 170k` ($0.034), `W' = 50k` ($0.125), output $0.01. Total **$1.999** (+$0.569: the
policy costs more on this short lane — a valid, reported outcome). A window above 500k changes nothing.

**A.7 Shapley.** §3.16: exact fractions 8 / 17.5 / 4.5; integer nano credits 8 / 18 / 4; Σ = 30.

**A.8 Refusal fallback.** `iterations = [{type: message, model: claude-fable-5, output 0, …},
{type: fallback_message, model: claude-opus-4-8, …}]` → declined inference `billable=False` ($0.00,
`anthropic.refusal.pre_output`), fallback priced at Opus 4.8; with declined output 6 → range [0, full],
ESTIMATED; with 2,127 → billable EXACT (`anthropic.refusal.mid_stream`).

**A.9 Miss threshold.** `E = 100,000`: `M = 2,000` with `R = 98,000` is not a miss (2,000 ≤ 5,000);
`M = 5,001` is a miss. `E = 30,000`: `M = 1,999` is not a miss; `M = 2,000` is (2,000 > 1,500 and ≥ 2,000).

**A.10 Edit churn (`cache.rebuild` edit-churn).** Opus 5.5 SDK lane, 5m TTL. Request `i−1`: `T = 120,000`.
A context edit on request `i` (gap 30 s, warm) clears `X = 40,000` tokens with the edit point at token
20,000: request `i` bills `R = 20,000`, `W = S = 62,000`; 5 more requests follow before the lane ends
(`K_rem = 5`). `cost_observed = 62,000 × $5/M =` **$0.31** (EXACT). Rewrite premium `S·(w − r) = 62,000 ×
$4.80/M = $0.2976`; benefit `X·r·K_rem = 40,000 × $0.20/M × 5 = $0.04`; net loss **$0.2576** (ESTIMATED).
`K* = S(α − β)/(Xβ) = 62,000 × (1.25 − 0.05) / (40,000 × 0.05) =` **37.2** calls ( = `S(w − r)/(X·r)`), so an
edit with 5 remaining calls loses money. Tests use `{"min_usd": "0.10"}`.

**A.11 Truncation (`failure.path` max-tokens-truncation).** Sonnet 5 lane; 4 attempts with `stop_reason =
"max_tokens"`, each `R = 48,000`, `W5 = 2,000`, `O = 16,384`: each costs 48,000 × $0.20/M + 2,000 ×
$2.50/M + 16,384 × $10/M = $0.0096 + $0.005 + $0.16384 = $0.17844; `cost_observed =` **$0.71376** (EXACT).
Three are followed within 120 s by a same-lane request with `T ≥ 0.95 × 50,000` ⇒ recoverable upper bound
3 × $0.17844 = **$0.53532** (ESTIMATED). Tests use `{"min_usd": "0.10"}`.

**A.12 Keepalive break-even.** `κ(w/r − 1)` with κ = 240 s: at `r = 0.1×`, `w = 1.25×` → 240 × 11.5 = 2,760 s
(**46 min**); Opus 5.5 (`r = 0.05×`) → 240 × 24 = 5,760 s (**96 min**); Fable 5.1 (`r = 0.025×`) → 240 × 49 =
11,760 s (**196 min**).

**A.13 Effort exemption (`effort_change_keeps_cache`, D28).**

| agent_product | model | channel | client_version | betas | keeps cache |
|---|---|---|---|---|---|
| claude_code | claude-opus-5-5 | anthropic_api | 2.1.270 | — | **True** |
| claude_code | claude-opus-5-5 | bedrock | 2.1.270 | — | False |
| claude_code | claude-opus-5 | anthropic_api | 2.1.270 | — | False |
| claude_code | claude-fable-5-1 | anthropic_api | 2.1.250 | — | False |
| agent_sdk | claude-opus-5-5 | anthropic_api | — | — | False |
| agent_sdk | claude-opus-5 | anthropic_api | — | `mid-conversation-output-config-2026-07-01` | **True** |
| agent_sdk | claude-sonnet-5 | anthropic_api | — | `mid-conversation-output-config-2026-07-01` | False |

**A.14 Subscription basis.** §6.9 case 23: an allowance-path inference priced at list lands in
`PricedTotal.allowance` (basis LIST_EQUIVALENT); `PricedTotal.exact` is unchanged; a FOCUS row for it has
`BilledCost = 0`; reconciliation reports it as `seat_allowance_unmetered`.

---

## Appendix G. Waves, merge gates and release gates (mirrors PLAN-v0.2.md §1)

### G.0 Waves

```
wave 0   F-CORE  (contracts, scaffolding, v0.1.2 goldens, facts task) ─── one agent
            │  merge gate 0: core importable, goldens captured, ownership CI live, 207 tests green
wave 1   F-SEM  ∥  F-KIT   (shared semantics  ∥  fakes, conformance, catalogs, keys, k-anonymity)
            │  merge gate F: tokenbill/core/* frozen; smoke-on-fakes green; contract owner = F-KIT agent
wave 2   RATES CC TRACE TELEM ADMIN RECON STORE REPLAY BLOCK SYNTH-ORACLE SYNTH-FLEET VERIFY
         DETECT-CACHE DETECT-OTHER PLAN OUT WIRING          (17 packages in parallel)
            │  daily canary merges run every gate test; merge gate 1
wave 3   CLI-LEDGER  ∥  CLI-SAVINGS
            │  merge gate 2
wave 4   INTEGRATION (e2e, supply chain, docs, version) → adversarial review → 0.2.0
```

Critical path: F-CORE → max(F-SEM, F-KIT) → slowest wave-2 package → slowest CLI package →
INTEGRATION. No package depends on another package of its own wave: wave-2 packages use only `core/*` and
test against the foundation fakes; cross-package checks are **gate tests** (`@pytest.mark.gate` +
`pytest.importorskip`) that run at the daily canary merge and must pass at merge gate 1. F-SEM and F-KIT
depend only on F-CORE (F-KIT's conformance helpers never import F-SEM; F-KIT gate tests that need F-SEM
modules run at merge gate F).

### G.1 Merge gates

**Gate 0 (after F-CORE):** `import tokenbill.core.{records,money,labels,types,protocols,registry,…}` works;
goldens captured from the untouched tree; ownership CI green; 207 tests green on 3.10 and 3.13.

**Gate F (after F-SEM ∥ F-KIT):** `tests/v2/kit/test_smoke_fakes.py` (SPEC §3.18) green; F-KIT gate tests
that need F-SEM (catalog grids parse with `core.policy.parse_policy`; `assert_detector_conforms` on a
test detector using `core.findings`) green; `core/*` frozen.

**Gate 1 (after wave 2)** — all branches merged; ownership clean per package; `pytest -m "not perf"` green
including every gate test:

| gate test (owner) | proves |
|---|---|
| `tests/v2/gates/test_gate1_smoke.py` (F-KIT) | CC fixture tree → `SqliteStore` priced with `RateCard` → reconcile against ADMIN fixtures → calibrate → `run_detectors` → `build_action_plan`, all real modules, no pipeline code needed |
| `tests/v2/trace/test_gate_collector_roundtrip.py` (TRACE) | CC `collect_incremental` → `write_trace_v2(profile usage)` → `TraceV2Adapter.read` → records equal (`to_json`) → `MemoryStore` ingest equals direct ingest |
| `tests/v2/trace/test_gate_ratecard.py` (TRACE) | demo trace@1 exact bill with the real `RateCard` equals v0.1 as-billed within 1 nano per call |
| `tests/v2/blocksim/test_gate_dual_engine.py` (BLOCK) | dual-engine agreement through TRACE's `TraceV1Adapter` + fingerprints (SPEC §10.4) |
| `tests/v2/synth_oracle/test_differential_replay.py` (SYNTH-ORACLE) | `UsageReplayer` == `ReferenceReplay` to the nano on ≥ 500 random lanes per policy family |
| `tests/v2/synth_fleet/test_gate_files_through_adapters.py` (SYNTH-FLEET) | every written source file (transcripts, execution files, OTLP, trace@2, admin pages, CUR) read by the real adapters reproduces the canonical records' token totals per team; truth vs oracle per plant |
| `tests/v2/store/test_gate_adapters.py` (STORE) | CC/TELEM/TRACE/ADMIN fixture files through the real adapters ingest and merge correctly, priced with `RateCard` |
| `tests/v2/recon/test_gate_ratecard_admin.py` (RECON) | ADMIN's recorded fixtures parsed by the real adapters reconcile with the real `RateCard` |
| `tests/v2/detect_cache/test_gate_*.py` (DETECT-CACHE) | real `UsageReplayer`: A.1 → `ttl-1h-recommended` $1.1508, A.2b → `ttl-5m-recommended` $0.318 (min_usd 0.10), A.2 → none; SYNTH-FLEET plants payments/platform/mobile/agents recovered; control team clean |
| `tests/v2/detect_other/test_gate_*.py` (DETECT-OTHER) | SYNTH-FLEET plants search/infra/data/ops/ci-bots/agents recovered with the real replayer; control team clean |
| `tests/v2/plan/test_gate_*.py` (PLAN) | real replayer: A.1 TTL lever Shapley == standalone; payments TTL lever Shapley within ±5% of truth; sharded == unsharded |
| `tests/v2/verify/test_gate_*.py` (VERIFY) | stepped-wedge panels from `synth.lanes_gen.rollout_panel`; `build_panel` on a real `SqliteStore` |
| `tests/v2/outputs/test_gate_*.py` (OUT) | FOCUS totals equal `SqliteStore.cost_rows` sums; showback from real `publish` |
| `tests/v2/wiring/test_gate_*.py` (WIRING) | `build_env` with the real `RateCard`; `open_store` + `map_shards(jobs=2)` on a real `SqliteStore` equals `jobs=1` |

**Gate 2 (after wave 3):** both CLI packages merged; golden demo/analyze byte-identical; every command's exit
codes; `tokenbill --version`; the full suite green on Python 3.10 and 3.13; the frozen `tests/test_cli.py`
green.

### G.2 Release gates (checked by INTEGRATION before 0.2.0 GA)

1. At least one real, redacted Anthropic usage_report + cost_report page pair from the adopting
   organization is added as an ADMIN/RECON fixture (SPEC §19.8 #6); until then `reconcile` prints
   `dq.recon_schema_unverified`.
2. The opt-in local-corpus test (`tests/v2/e2e/test_local_corpus.py`, marker `local_corpus`, skipped unless
   `TOKENBILL_LOCAL_CORPUS=1`) runs `scan` over the machine's real `~/.claude/projects` and matches, to the
   cent, an independent ~80-line reference computed inside the test (de-duplicate by `message.id` keeping max
   output, skip `<synthetic>`, price at `core/facts.json` rates).
3. `pricing verify` (offline) clean; every VERIFY item of SPEC §19.8 either verified (facts.json
   `verification: "primary"`) or shipped disabled/commented and listed in the release notes.

---

## Appendix O. File ownership (mirrored into `OWNERSHIP.toml` by F-CORE)

Every repository file is owned by exactly one package, by INTEGRATION, or is FROZEN. Reading another
package's files is allowed; writing them is not. Brace groups expand to one path per member.


| package | owns |
|---|---|
| F-CORE | `OWNERSHIP.toml`, `scripts/check_ownership.py`, `scripts/capture_goldens.py`, `pyproject.toml`, `uv.lock`, `.gitignore`, `.github/workflows/ownership.yml`, `tokenbill/core/{__init__,errors,records,money,labels,types,protocols,registry,ids,jsonl,textsafe,secrets,models,lanes,evidence,facts,builders}.py`, `tokenbill/core/facts.json`, `tokenbill/{adapters,rates,recon,store,sim,detect,plan,verify,outputs,finops,synth,pipeline,commands}/__init__.py`, `tests/conftest.py`, `tests/v2/__init__.py`, `tests/v2/core/**`, `tests/v2/golden/**` |
| F-SEM | `tokenbill/core/{conventions,cache_rules,transitions,shapley,policy,shards,findings}.py`, `tests/v2/sem/**` |
| F-KIT | `tokenbill/core/{testing,catalog,keys,kanon}.py`, `tests/v2/kit/**`, `tests/v2/gates/**` |
| RATES | `tokenbill/pricing.py`, `tokenbill/rates/{schema,engine,contract,billing_rules,verify}.py`, `tokenbill/rates/data/**`, `tests/test_pricing.py`, `tests/v2/rates/**`, `tests/v2/fixtures/rates/**` |
| CC | `tokenbill/adapters/{claude_code,cc_collect,cc_headless}.py`, `tests/v2/claude_code/**`, `tests/v2/fixtures/claude_code/**` |
| TRACE | `tokenbill/adapters/{fingerprint,trace_v1,trace_v2}.py`, `tokenbill/instrument.py`, `tests/v2/trace/**`, `tests/v2/fixtures/trace/**` |
| TELEM | `tokenbill/adapters/{conventions_ext,otel,openai,bedrock,anthropic_responses}.py`, `tests/v2/telemetry/**`, `tests/v2/fixtures/telemetry/**` |
| ADMIN | `tokenbill/adapters/{anthropic_admin,openai_admin,cloud_billing}.py`, `tests/v2/admin/**`, `tests/v2/fixtures/admin/**` |
| RECON | `tokenbill/recon/{reconcile,residuals,costmap,pull,orgscan}.py`, `tests/v2/recon/**`, `tests/v2/fixtures/recon/**` |
| STORE | `tokenbill/store/{schema,db,merge,rollups,retention,pseudonym}.py`, `tests/v2/store/**` |
| REPLAY | `tokenbill/sim/{usage_replay,calibrate}.py`, `tests/v2/sim/**` |
| BLOCK | `tokenbill/sim/block_replay.py`, `tokenbill/detect/block.py`, `tests/v2/blocksim/**`, `tests/v2/fixtures/blocksim/**` |
| SYNTH-ORACLE | `tokenbill/synth/{oracle,lanes_gen}.py`, `tests/v2/synth_oracle/**` |
| SYNTH-FLEET | `tokenbill/synth/{fleet,truth,writers}.py`, `tests/v2/synth_fleet/**` |
| VERIFY | `tokenbill/verify/{stats,estimators,its,ab,rollout,label_policy,receipts,panel}.py`, `tests/v2/verify/**` |
| DETECT-CACHE | `tokenbill/detect/{cache_miss,cache_ttl,cache_structure}.py`, `tests/v2/detect_cache/**` |
| DETECT-OTHER | `tokenbill/detect/{context,premium,model,failure,automation,tail}.py`, `tests/v2/detect_other/**` |
| PLAN | `tokenbill/plan/{action_plan,realization,policy_pack,litellm,effectiveness}.py`, `tokenbill/plan/templates/**`, `tests/v2/plan/**` |
| OUT | `tokenbill/outputs/{result_json,terminal,html,focus,sarif,ccusage,showback}.py`, `tokenbill/finops/{allocation,workload}.py`, `tests/v2/outputs/**`, `tests/v2/finops/**` |
| WIRING | `tokenbill/config.py`, `tokenbill/pipeline/common.py`, `tests/v2/wiring/**` |
| CLI-LEDGER | `tokenbill/cli.py`, `tokenbill/pipeline/ledger.py`, `tokenbill/commands/{demo,analyze,init,collect,ingest,bill,reconcile,export,showback,pricing,purge}.py`, `tests/v2/cli_ledger/**` |
| CLI-SAVINGS | `tokenbill/gate.py`, `tokenbill/pipeline/{savings,verification}.py`, `tokenbill/commands/{scan,me,calibrate,findings,whatif,policy,measure,ab,receipt,check,report}.py`, `tests/v2/cli_savings/**` |
| INTEGRATION | `tokenbill/__init__.py`, `README.md`, `DESIGN.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`, `Makefile`, `docs/**`, `.github/**` except `.github/workflows/ownership.yml`, `scripts/{sbom.py,check_zero_deps.py,repro_check.sh,perf_gates.py}`, `tests/v2/e2e/**` |
| FROZEN | `tokenbill/{__main__,common,trace,analyzer,simulator,breakers,report,demo_traces}.py`, `tokenbill/py.typed`, `examples/**`, `CODE_OF_CONDUCT.md`, `LICENSE`, `tests/test_{analyzer,breakers,cli,demo_recovers_planted_waste,demo_traces,examples,instrument,performance,report,simulator,trace}.py` |

---

## Appendix L. Legacy v0.1 engine

The v0.1 contract (the previous `docs/SPEC.md`: trace@1 schema, canonical rendering, analyzer, simulator,
breakers, report, demo scenarios, flagship test) remains authoritative for the frozen modules. Integration
moves it verbatim to `docs/SPEC-v0.1.md` and installs this document as `docs/SPEC.md`.

---

## Appendix E. Errata and contract-owner rulings (binding; maintained by the orchestrator)

E.0 How to use: this appendix overrides any conflicting text above. It is updated only between waves.

### E.1 After merge gate 0 (F-CORE merged into `v0.2` at b0ba621)

Facts corrected against primary sources (see `tokenbill/core/facts.json`, `verification: "primary"`, verified 2026-09-23). **Read facts from `core.facts`, never from the §19 prose, where they differ:**
- FOCUS 1.4 has no `ProviderName` / `PublisherName` columns; use `ServiceProviderName` and `HostProviderName`.
- Claude Code `effortLevel` accepts `low|medium|high|xhigh` — there is no `max`.
- Several §11.4 settings keys have higher `min_version` than §11.4 states; `core.facts` carries the verified values.
- Current Bedrock model ids have no `-vN` suffix (`normalize_model` accepts both).
- The US-geo modifier also applies to `foundry`; the batch modifier applies to Anthropic channels only.
- Modifier ids: `anthropic.fast.opus-5` (Opus 5 and Opus 4.8) and `bedrock.endpoint.regional` (generation ≥ 4.5).

Additive contract items F-CORE shipped (nothing renamed or removed; use them rather than re-implementing):
- `core.records`: `EVENT_ATTRS` (per-kind LaneEvent attrs schema), `DIAG_REASONS`, `BLOCK_KINDS`. Enum-typed fields accept value strings (coerced); list inputs become tuples; sorted fields are normalized; `Lane` sorts requests by `(ts, seq, request_id)` and events by `(ts, kind, attrs)`; `Lane`/`Session` require member keys to match; `principal`, `api_key_id`, `cwd_key` are regex-validated; `Inference.output_upper` only with `MESSAGE_START_ONLY`.
- `core.registry.all_detectors(*, notes=None)`; unimportable detectors produce data-quality code `dq.detector_unavailable`.
- `core.jsonl.open_private(path, mode, *, runner=None, platform=None)`, `acl_warning(handle)`, `ACL_WARNING`, `ZSTD_MESSAGE`; `parse_json_line` returns `None` for number literals that overflow to ±inf; `iter_lines` skips blank lines (line numbers count from 1 at `start_offset`).
- `core.evidence`: `EvidenceConstant` is a NamedTuple `(value, source_url, finding_id, checked_on)`; `TABLE`, `get()`, `keepalive_break_even_s()`.
- `core.builders`: `make_fingerprint`, `unit_rates_from`, `CANARY_EMAIL`, `CANARY_KEYS`, `STRUCTURAL_KEYS`.
- `core.facts`: `Facts` accessor API (`rate_rows`, `modifiers`, `settings_keys`, `evidence`, `successors`, `retirements`, `retired`, `promotions`, `announced`, `focus`, `headless_fields`, `sku_rules`, `rows_for`, `rate_row`, `modifier`, `rate_rows_json`, `modifiers_json`, `entries`) and `parse()`. Settings-key entries carry `target`, `domain`, `lever_id`, `tradeoff`, `managed_only` (F-KIT builds `AllowedKey` from them).
- `money`: every rounding helper raises `ValueError("amount out of range")` for magnitudes ≥ 10**4000 nano. `PublishedAggregate.token` is omitted by `to_json`; `IngestOptions` key bytes have `repr=False`.
- `unpriced()` returns an EXACT-evidence `Figure` with `nano=None` and note `unpriced: …`; renderers must print "unpriced", never a number.
- `UnitRates.bucket_nano` prices `cache_write_unknown` at the 5m (low/point) rate; `uncached`/`input` are aliases of `uncached_input`.

Rulings on open review items:
- **R-E1 (F-KIT, `core.kanon`):** data-quality findings (kinds `dq.*`, including the `run_detectors` missing-capabilities finding with empty scope and `n_users = 0`) carry no personal data and are **exempt** from k-anonymity suppression and re-scoping. Every other finding follows §8.4.
- **R-E2 (renderers):** `Fidelity` is a plain `IntEnum`; render it with `.name.lower()` or `int(...)`, never `str()`/f-string default formatting.
- **R-E3 (TRACE):** trace@2 decoding must use TRACE's own codec on the hot path (from_json costs ≈ 41 µs/request vs the 50 µs §17 ingest budget); `from_json` stays the reference for round-trip tests.
- **R-E4 (Copilot):** a GitHub Copilot addendum (`SPEC-v0.2-COPILOT.md`) is being designed; its core additions (channel/provider ids, billing path for pooled AI Credits, registry string-map entries, facts rows, catalog levers) will be applied by the contract owner **between wave 1 and wave 2** as additive changes. Wave-1 packages: keep enums/maps extensible and do not hard-code closed sets of channels or billing paths in logic where a lookup would do.

### E.2 After merge gate F (F-SEM + F-KIT merged into `v0.2` at 99ba098)

Rulings (contract owner):
- **R-E5 (per-message effort channels):** follow the primary source — `PER_MESSAGE_EFFORT_CHANNELS = {anthropic_api, claude_platform_aws, vertex}`; `foundry` is **not** included (amends §3.14(b)/D28(b)).
- **R-E6 (fast mode):** §3.15 rule 4 stays literal in v0.2 for both engines (every served-speed change is a `fast-toggle` cause when a miss is observed). The Claude Code "only the first enable breaks the cache" refinement is deferred; causes are only assigned to observed misses, so over-attribution is bounded.
- **R-E7 (unattributed team):** in `where` filters and `ShardKey`, `team == ""` means **unattributed (SQL `team IS NULL`)**. Every `LedgerStore` implementation must honor it (MemoryStore does; STORE's SqliteStore must, gate-tested).
- **R-E8 (semantics ratified):** Shapley credits sum to `value(all) − value(∅)`; `Transition.index` is the position among requests that have a serving inference — join transitions to requests on `request_id`, never on `lane.requests[index]`; the iterations invariant also accepts `Σ MESSAGE elements == top-level usage`; `BadUsageError(UsageError)` for malformed usage; `build_finding` enforces one basis per finding, the D26 allowance rule, and SPEC value sets (PROVIDER_ESTIMATE allowed in `dq.*` findings in any cohort).
- **R-E9 (k-anonymity scope):** extends R-E1 — provider-side aggregate findings with no person or API-key dimension (e.g. `aggregate.org-scan` from Admin/Analytics/billing data) are exempt from k-suppression at workspace/org level. Complementary suppression of findings and scrubbing of dropped scope values from finding text (RULINGS K-2 / CONTRACT-CHANGE-KIT-3 (d)) are ratified.
- **R-E10 (users unknown):** `publish()` must **not** suppress rows whose user count is unknown (`n_users == 0` because the source has no principal dimension) when the grouping keys contain no person or person-proxy dimension (`principal`, `session`, `api_key_id`, `cwd_key`); such rows are published with note `users_unknown`. With a person-proxy key they stay suppressed. Audience `self` (`--self`, a single principal's own data) never suppresses. This is a contract-owner fix to `core.kanon` applied in wave 1.5.
- **R-E11 (KIT-1):** no `A or B` selectors in v0.2; `sdk.keepalive` targets `LaneKind.API_RUN` lanes (SDK/API agents) only, never Claude Code lane kinds.
- **R-E12 (KIT-2):** `LeverDef` has no snippet field; PLAN renders snippets from `plan/templates/` keyed by lever id.
- **R-E13:** break-glass naming of `tail.runaway` sessions (§8.5) is done by CLI-SAVINGS outside `kanon`, with an audit-log entry.
- **R-E14:** `assert_store_conforms` accepts `PrivacyError` or `UsageError` for `cost_rows(group_by=["principal"])`; SqliteStore should raise `PrivacyError`.
- **R-E15 (Copilot):** wave 1.5 (between gate F and wave 2 for the Copilot-affected packages) applies the GitHub Copilot core amendments of `SPEC-v0.2-COPILOT.md` (additive fields/enums/registry entries with defaults). Packages started before wave 1.5 build against the gate-F core; additive changes must not break them.


### E.3 After wave 1.5 (GitHub Copilot) — rulings R-E16 … R-E23

The GitHub Copilot addendum is `copilot/SPEC-v0.2-COPILOT.md` (revision 3.1) with the binding change list
`copilot/briefs/CORE-AMENDMENTS.md`; where they differ from this SPEC, for Copilot scope, they win. Precedence
among them: CORE-AMENDMENTS and the Copilot briefs > addendum revision-3 body text.

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

### E.4 After wave 2a (REPLAY, BLOCK, SYNTH-ORACLE, SYNTH-FLEET, VERIFY, CC, ADMIN, TELEM, DETECT-CACHE merged into `v0.2` at d91a5e2)

Rulings (contract owner):
- **R-E24 (replay semantics):** ReplayResult.saving is computed per request and summed; unchanged requests contribute exactly 0 (REPLAY and the oracle agree, O-18). Policies made only of rate transforms (`fast=off`, `geo=global`, `regional=global`) with no flip and no range return EXACT cost and saving (amends the §3.5 comment). `compact-window` without `post=` uses `COMPACTION_SUMMARY_TOKENS_DEFAULT`; the pipeline (WIRING/CLI-SAVINGS/PLAN) must pass `post=<org median>` computed once per run, so sharded and unsharded runs agree. Cross-lane repair groups (`stagger_fanout`, `shared_ci_prefix`) are confined to the D30 cohort (`team`, `lane_kind`) in addition to the §9.3.6 keys — REPLAY's reading is canonical; SYNTH-ORACLE aligns in the gate-1 fixups. The oracle is the reference for **documented** mode only; calibrated-mode weighting follows REPLAY. `CalibrationPartial.confusion` keeps provider-qualified reasons (`"<provider>:<reason>"`). Gap-band labels live in `sim.usage_replay.GAP_BANDS`.
- **R-E25 (policy clause precedence):** v0.2 keeps the implemented canonical-order precedence (both engines agree). Documented; "most specific selector wins" is deferred to v0.3.
- **R-E26 (unpriced requests in replays):** R2 applies — a saving that depends on an unpriced request is unpriced (never a partial number); gate 1 checks it with SYNTH-FLEET's unknown-model plant.
- **R-E27 (trace@1 lanes, TRACE):** `TraceV1Adapter` keys lanes by the trace's `run_id` (v0.1 runs are lanes); the §5.5/§5.8 "same model and tools-tier hash" inference applies only to sources without a lane identity. The v0.1 tool-churn demo run stays one lane (BLOCK's dual-engine gate).
- **R-E28 (store cluster kinds, F-KIT-C / STORE):** `LedgerStore.cluster_days` also accepts cluster kind `"gateway"` (additive; MemoryStore and SqliteStore).
- **R-E29 (VERIFY API):** `label_policy.measure` raises `NotAMeasurement` (a `GateFailed`) when the decision is ESTIMATED; CLI-SAVINGS maps it to exit code 3 with the guard list.
- **R-E30 (k-anonymity of aggregate cells, OUT):** aggregate sources without per-cell user counts (Admin/Analytics/cloud billing) are published at team (or coarser) level only; (team, model) cells from such sources are folded into the team cell unless a per-cell user count ≥ k is known.
- **R-E31 (kanon hotfix, F-KIT-C):** `core.kanon._merge_findings` must not truncate mid-summary: keep the full summary (or truncate at a word boundary without dropping labelling statements such as the D26 "list-equivalent, not invoice dollars" sentence).
- **R-E32 (Detector requirements):** per-kind requirements are deferred to v0.3; detectors document per-kind capability needs in their docstrings and emit their own `dq.*` note when a kind cannot run.
- **R-E33 (Claude Code importer):** `isCompactSummary` user entries are not user-text appended items (fix in gate-1 fixups). Quota/overage state tracked per file and session is a documented v0.2 limitation.
- **Deferred (v0.3):** `AppendedItem.est_tokens` (TELEM-1), ADMIN-1 (a)(b)(d)(e) beyond Copilot's `CostLine` fields, cross-file OTLP log/trace joins, OpenAI `previous_response_id` cross-file chains.

### E.5 After wave 1.5a (F-CORE-C merged into `v0.2` at bb6b50f)

- **R-E34 (Copilot rate coverage):** `facts.copilot.rates` stays the §19.2 subset used by the fakes. CP-RATES' shipped data file (`tokenbill/copilot/data/github_copilot.json`) carries **every** billable interval replayed from the dated GitHub pricing-YAML revisions, including closed intervals for models no longer listed (e.g. Raptor mini, Gemini 2.5 Pro, Gemini 3 Flash, Gemini 3.1 Pro, MAI-Code-1-Flash, GPT-4.1, GPT-5.2, GPT-5.2-Codex), each with its `date_source`. The facts-parity test compares shared rows only.
- **R-E35:** `to_json` omits fields appended in wave 1.5 while at their default (`records.appended()`, `OMIT_DEFAULT` metadata); `from_json` restores defaults; `record_fields(cls)` still lists them. Pre-Copilot documents stay byte-identical.
- **R-E36:** Copilot lane-event value domains (`records.COPILOT_EVENT_VALUE_DOMAINS`) and the `copilot_compliance` extra values are documented, validated by the Copilot adapters that produce them, not by `LaneEvent`.
- **R-E37 (F-KIT-C):** `core.kanon._add_priced` must carry `PricedTotal.pool` (`pool=_add_opt(a.pool, b.pool)`); the `sources_mask` bit table in `core/testing.py` gains the Copilot bits (256/512/1024/2048/4096) per C-30.
- **R-E38 (C-30 document items):** SPEC §5.1 capabilities += `credits`, `licenses`, `activity`, `config`, `copilot_billing`, `ext:<name>`; data-quality codes += every Copilot `dq.*` code named in the Copilot briefs plus `dq.adapter_unavailable`, `dq.extension_unavailable`, `dq.convention_module_unavailable`, `dq.principal_key_mismatch`; §7.3 `sources_mask` bits 256/512/1024/2048/4096 are the Copilot sources (see CORE-AMENDMENTS C-30).

### E.6 After DETECT-OTHER (merged into `v0.2` at 255e04a)

- **R-E39 (compaction window):** `context.compaction-window` reports the best eligible window as `Finding.recoverable` (SPEC §10.2 rule); the full window curve is in the evidence items `compaction-window:<w>k`. Flagship/SYNTH checks of the §18 search plant compare the 400k evidence point, not `recoverable`.
- **R-E40 (org median S_c):** the pipeline (WIRING `pipeline.common`) computes the org median compaction summary size once per run and sets `ctx.thresholds["context.compaction-window.post_tokens"]` (decimal string of tokens); replays of `compact-window` policies pass it as `post=` (R-E24).
- **R-E41 (runaway p99):** `tail.runaway` compares a session against the nearest-rank p99 of the **other** sessions of its cohort (leave-one-out), with a minimum cohort of 20 sessions; SYNTH-FLEET truth aligns (gate-1 fixups).
- **R-E42:** documented v0.2 limitations — `scheduled-cadence` is per lane; `rebaseline` reports the first model change per cohort; unbilled error kinds rarely pass the default `min_usd`.

### E.7 After gate F' (F-SEM-C, F-KIT-C, F-EXT, F-POOL merged into `v0.2` at f2162e4)

- **R-E43 (Copilot billing paths):** every Copilot adapter sets `PricingContext.billing_path` to `copilot_pool` (licensed users' credits) or `copilot_direct` (org-billed usage: no licensed user, `GITHUB_TOKEN` CI, unlicensed code review) on **every** inference it emits — never the default. A Copilot lane with billing class `billed` would share a cohort with Claude Code lanes and escape every Copilot rule.
- **R-E44 (rounding remainders):** `rounding_remainders` passed to reconcilers are keyed by **adapter name** (e.g. `"github-ai-usage"`), the key `core.extensions` uses (CONTRACT-CHANGE-F-EXT-1 item 1).
- **R-E45 (CP-STORE conformance):** CP-STORE's record-store conformance test uses a two-argument factory that opens the ledger first (`SqliteStore(path, org_key=org_key)` then `CopilotRecordStore(path)`), per CONTRACT-CHANGE-KIT-C-1 §3.
- **R-E46 (undecidable report convention):** when CP-RECON decides `convention:<source> = undecidable`, CP-STORE's enricher passes `excl` to `core.pool.build_cells` and emits `dq.copilot_convention_undecidable`; figures depending on it are ESTIMATED.
- **R-E47 (users unknown):** rows kept under R-E10 carry no field; renderers (OUT, CP-OUT) call `core.kanon.row_notes(row, group_by=…)` and print "users unknown" when it returns `USERS_UNKNOWN`.
- **R-E48 (F-POOL):** accepted above 3k LOC; the "seat removal never negative" property holds outside promotion months only (a promo Business seat carried 3,000 credits for a $19 fee); plan resolution uses the deciding source per entity × month (R-E22), no per-seat fill from lower sources.
- **R-E49 (dual engine on Copilot lanes):** the oracle and replay engines do not model `context-tier-change` or the unknown-TTL rule; v0.2 excludes Copilot lanes from the dual-engine/differential gates; Copilot savings use aggregate cell replay (CP-PLAN).
