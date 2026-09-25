# Token Bill v0.2 — "Numbers that reconcile" (trust-first design proposal)

Status: proposal for build, 2026-09-23. Lens: **correctness and trust first**. Where this lens and a
feature idea conflict, the lens wins: a smaller set of numbers that reconcile to the invoice beats a
larger set of numbers nobody can audit.

Evidence base: the 467 adversarially verified findings of the research phase (IDs in `code` refer to
`research/_all.json`; where a finding was *corrected* by verification, the corrected version is what is
used here). The empirical corpus (one heavy Claude Code user, 43,383 de-duplicated billed calls, $11,632.75
at list price over 134 days) is used for **mechanisms and orders of magnitude, never as a fleet average**
(`cc-heavy-tail-concentration`: that user spends ~$215 per active day against Anthropic's ~$13 fleet
average).

Hard constraints honored throughout: Python ≥3.10, **zero runtime dependencies** (stdlib only; dev deps
pytest, ruff, hypothesis); local-first; no telemetry; content-free processing possible for every fleet
source; never rank individuals, team-level aggregation with k-anonymity by default; every number labeled
**exact / estimated / measured / verified**; `tokenbill/trace@1` files and the `demo` / `analyze` verbs keep
working; buildable by parallel agents with disjoint file ownership.

---

## 0. Summary on one page

**What v0.2 is.** A local-first, zero-dependency cost ledger and counterfactual engine for LLM/agent
spend. It ingests what enterprises already have (Claude Code transcripts, Claude Code OpenTelemetry,
recorder traces, OpenAI/Bedrock usage payloads, Anthropic Admin/Analytics exports), normalizes them into
one canonical, content-free **attempt ledger**, prices it with a **versioned, sourced, effective-dated
rate registry** in exact decimal money, **reconciles** it to the provider's own usage and cost reports,
**calibrates** its cache simulator against the provider's server-side `cache_miss_reason` labels, ranks
recoverable dollars with **overlap-aware (Shapley) credit and realization-rate intervals**, emits
deployable **managed-settings patches**, and closes the loop with **measured/verified savings receipts**
signed with stdlib + OpenSSH.

**Why this wins by a wide margin.** Everybody now shows tokens, cost tiles and cache hit rate
(`coding-agent-dashboards-commodity`, `cc-first-party-visibility`). The research found no product that
sells numbers reconciled to the invoice or savings verified against the bill (`verified-savings-gap-rtk`,
`anth-admin-api-reconciliation`, `competitive-landscape`). Meanwhile the dominant cost physics (re-read context, cache TTLs,
compaction windows, model/effort defaults) make naive "tokens saved" claims wrong in sign
(`token-not-cost`, `llm-token-not-bill`). The only defensible product is the one that can prove its
numbers.

**The v0.2 build** is 13 work packages (≈32k LOC incl. tests) in three phases: a foundation package of
frozen interfaces (phase 0), eight parallel packages (phase 1), four parallel packages (phase 2), then
integration and adversarial review. Every package ships against recorded fixtures; nothing requires a
network or an API key at test time.

---

## 1. Thesis and positioning

### 1.1 The problem, in verified numbers

1. **The bill is re-read context.** Cache writes + reads are ~80–87% of coding-agent cost in a
   provider-billed Claude Code study (2,908 runs: cache creation 44.3%, reads 35.4%, tool outputs only
   3.3%; ~87% of reconstructed cost across 5,493 executions in its later version) (`token-not-cost`,
   `token-reduction-not-cost`). On the local corpus: reads 55.1%, 1h writes
   17.6%, 5m writes 12.6%, output 14.4%, uncached 0.2%, with a 97% hit ratio (`cc-context-size-driver`).
   Calls with ≥400k context were 17.9% of calls and 45.8% of dollars.
2. **Token reduction is not bill reduction.** A tool-output compressor cut tool tokens 38.4% and raised
   the bill 6.8%; a proxy raised it 48.4%; token reduction predicted cost change at r=0.154
   (`token-not-cost`). JetBrains' paired, billed A/B found RTK (advertised 60–90% savings) +7.6% more
   expensive while its own counter claimed 96.2M tokens saved (`verified-savings-gap-rtk`). Anthropic
   measured context editing at +74% cost on short runs (`anth-context-editing-not-savings`).
3. **Today's tools (including Token Bill v0.1.2) misstate the bill.**
   - v0.1.2 under-reports the real corpus by −7.3%: $758.63 from pricing 1h cache writes at 1.25× instead
     of 2×, $66.61 from the missing Opus 5.5 row, $20.86 from ignored fallback iterations
     (`cc-pricing-1h-opus55-gap`, `ashrae-calibration-gate`).
   - Summing Claude Code transcript lines instead of de-duplicating by `message.id` overstates spend
     2.33× ($27,094 vs $11,633) (`cc-import-dedup-message-id`).
   - Top-level usage excludes compaction and advisor sub-inferences (`anth-iterations-undercount`,
     `compaction-iterations-p0`, `advisor-iterations-undercount`).
   - A tier-blind tool under-billed Codex priority traffic by 50% (`oai-service-tiers`).
   - At least 14 framework telemetry bugs miscount cache tokens by 0.03×–2× (`fw-telemetry-conventions`).
   - One unpriced model blanks org totals in v0.1.2 (`cb-pricing-coverage`); reusing a `run_id` across
     files reports $40 for a true $22 (`cb-dup-runid`).
4. **Projections are optimistic, sometimes with the wrong sign.** Energy-efficiency programs realize
   25–40% of engineering projections; weatherization projections were ~2.5× actual
   (`projection-optimism`); a simulated LLM fleet rollout realized 0.67 (CI 0.34–0.98) of the replay
   projection (`realization-rate-backtest`). Standalone lever ceilings overcount joint savings by 4.6%
   and one lever's sequential credit swings 69% by ordering (`shapley-attribution`).
5. **The stakes.** At Anthropic's published ~$13 per developer per active day and $150–250 per developer
   per month, 2,000 developers is ~$3.6–6.0M/year (`cc-enterprise-baseline`, `ent-forecast-budgets`).
   Datadog's own program saved >$1M/month, mostly from default changes (Opus→Sonnet $687k/month at an
   8% eval-proficiency cost; effort high→medium $288k/month) (`org-defaults-datadog`,
   `datadog-1m-month-case`).

### 1.2 Thesis

Enterprise LLM bills are dominated by re-read context, so the levers that shrink them (cache TTL,
compaction windows, cache-breaker fixes, model and effort defaults) interact non-linearly with prompt
caching, and naive token savings routinely fail to reduce, or even increase, the bill. The tool that
survives an enterprise lab is therefore not the one with the most detectors but the one whose numbers
reconcile: a billing-exact, content-free attempt ledger priced from a versioned, sourced rate registry in
exact decimal money, reconciled to the provider's usage/cost/analytics APIs, with a cache simulator
calibrated against server-side `cache_miss_reason` labels. On that trusted core Token Bill ranks
recoverable dollars (Shapley-deduplicated, realization-adjusted), emits deployable managed-settings
patches, and closes the loop with measured or verified, signed savings receipts. Every number carries an
evidence label and a cost basis, and nothing estimated is ever shown as billed.

### 1.3 Positioning

| Alternative | What it does well (verified) | What it cannot do | Token Bill stance |
|---|---|---|---|
| **Anthropic cache diagnostics** (beta `cache-diagnosis-2026-04-07`) | Server-side miss reason per request from hash-only fingerprints (`anth-cache-diagnostics-beta`) | First-party API only; first divergence only; component-level; `cache_missed_input_tokens` is a byte-derived magnitude, **not a billing number**; `unavailable`/`previous_message_not_found` produce no comparison (`anth-cache-diagnostics`, `cc-miss-taxonomy-ground-truth`) | **Consume as ground truth.** Validate Token Bill's classifier against it and publish the confusion matrix; cover Bedrock/Vertex/Foundry, second divergences and TTL expiry; never price with `cache_missed_input_tokens`. |
| **Claude Code `/usage`, status line, `/insights`** | Per-session hit share, misses, "likely cause", attribution to skills/MCP (`cc-usage-likely-cause`, `cc-first-party-visibility`) | One developer, one machine, main conversation only, list price | **Be the fleet view**: all developers, all subagents, dollars, verified fixes. README "Related work" must be rewritten (it says provider tools are "silent on why", now false). |
| **Anthropic Admin Usage/Cost, Claude Code Analytics, Enterprise Analytics APIs** | Billed ground truth: 1-minute usage buckets with 5m/1h split; daily cost in cents; per-user `amount` vs `list_amount` (`anth-admin-usage-cost-api`, `anth-claude-code-analytics-otel`) | Say *what*, not *why*; no counterfactuals | **Reconcile to them** (coverage %, ledger error per model-day, effective discount). |
| **Datadog Agent Console** (Preview) | Closest competitor: spend by agent/team/user, waste patterns, deployable Fix Library (`datadog-agent-console-fix-library`) | SaaS; savings are estimates (per repo); the research found no invoice reconciliation or cache-exact counterfactual; Datadog publishes no separate Agent Console savings | Complement: export waste line items into Datadog (FOCUS/OTLP); win on reconciliation, cache economics, verification, local-first. |
| **ccusage** (18.7k★) and local trackers | Careful de-duplication, 18 agents, JSON output (`ccusage-local-parity`) | Descriptive; no causes, no counterfactuals, no org rollup | ccusage-compatible JSON export; reuse the same local files; add why and how much is recoverable. |
| **LiteLLM, Langfuse (ClickHouse), Helicone (maintenance mode), Portkey (Palo Alto)** | Spend by key/tag, trace stores, gateways, budgets (`litellm-spend-and-supply-chain`, `langfuse-clickhouse`, `helicone-maintenance-mode`, `portkey-panw-gateway-security`) | Cost is an attribute; no cache root cause; LiteLLM PyPI was backdoored in March 2026 | Treat as **sources**, not competitors; zero dependencies as a supply-chain selling point; optionally emit LiteLLM `cache_control_injection_points` config as a fix. |
| **LangSmith, Braintrust, Weave, Phoenix, New Relic** | Token pricing with cache subtypes (`obs-platforms-no-cache-rca`) | None explains cache loss or does counterfactual replay | Import via OTel GenAI/OpenInference. |
| **FinOps platforms** (CloudZero, Vantage, Harness, Datadog CCM) | Allocation, chargeback, FOCUS ingestion (`finops-platforms-export-target`) | No waste root cause | Feed them FOCUS rows with `x_` waste columns; do not rebuild allocation. |
| **promptcachelint, RTK, Headroom, token-optimizer** | Breaker linting, compression (`promptcachelint-overlap`, `headroom-compression-cache-aware`, `oss-token-minimizer-ecosystem`) | Self-reported savings; some non-commercial licenses | Be the neutral referee: price any optimizer's effect in billed dollars and issue verified/measured receipts. |
| **Gateways with spend limits** (Claude apps gateway, Cloudflare, Cursor, Copilot) | Enforcement is free or bundled (`enforcement-primitives`, `cloudflare-free-spend-limits`) | Blunt caps cut valuable use (`blunt-caps-collateral`); the Claude apps gateway loses the 1h TTL (`cc-apps-gateway-routing-tax`) | **Do not enforce.** Configure existing primitives via generated policy; quantify the routing tax. |

### 1.4 The five trust properties (what an enterprise lab can check)

1. **Reconciliation.** Every ledger has a reconciliation report against provider data: token coverage,
   rate-card error per model-day (target ≤0.5%), dollar coverage, explained residuals (Priority Tier
   excluded from the cost report, 30-day revision window, CCU aggregation), provisional vs final
   (`ent-reconciliation`, `cc-reconciliation-matrix`).
2. **Labels.** Every number is a `Figure` with evidence (exact/estimated/measured/verified), basis
   (list/contract/invoice/provider-estimate), finality and provenance (rate-row IDs, source IDs). The
   type system prevents an estimate from being rendered in a billed column.
3. **Calibration.** The replay engine publishes its predictive error on the customer's own traffic
   (ASHRAE Guideline 14 NMBE and CV(RMSE), `ashrae-calibration-gate`) and its breaker classifier's
   confusion matrix against server `cache_miss_reason` (`cb-cache-diagnostics`). Uncalibrated
   projections are labeled as such and cannot be signed.
4. **Receipts.** Savings claims are adjusted-baseline counterfactuals at constant prices
   (`mv-adjusted-baseline`, `staggered-did`), deduplicated with Shapley credit, adjusted by realization
   rates, and exported as DSSE-enveloped, `ssh-keygen`-signed receipts (`signed-receipts`).
5. **Privacy and supply chain.** Content-free by default (HMAC fingerprints, no prompt text), team-level
   k≥5 aggregation, no individual ranking (`aggregation-k5`, `leaderboard-goodhart`,
   `labor-law-constraints`); zero runtime dependencies, SHA-pinned CI, SBOM and provenance
   (`ent-supply-chain-incidents`, `ent-provenance-sbom`).

### 1.5 Non-goals for v0.2

- Not in the inference path (no default proxy; `ent-supply-chain-incidents`, `portkey-panw-gateway-security`).
- Not an enforcement system (caps/budgets exist everywhere; `enforcement-primitives`).
- Not a leaderboard or individual performance tool (`leaderboard-goodhart`, `labor-law-constraints`).
- Not a compressor (recommend and verify others; `headroom-compression-cache-aware`).
- Not a hosted multi-tenant dashboard (static HTML behind the enterprise SSO proxy suffices;
  `ent-server-sso-rbac`).

---

## 2. Architecture

### 2.1 Layers

```
  SOURCES (read-only, local files or recorded API pages; live pulls opt-in)
  claude-code transcripts | trace@1 | trace@2 (recorder) | OTLP JSON (Claude Code OTel, GenAI, OpenInference)
  OpenAI Responses/Chat usage | Codex rollouts | Bedrock Converse usage
  Anthropic usage_report / cost_report / claude_code analytics / enterprise analytics | OpenAI usage/costs
        │  adapters (convention registry + sum-check invariants + quarantine)
        ▼
  CANONICAL LEDGER  Session ▸ Lane ▸ Request ▸ Attempt ▸ Inference(UsageBuckets, PricingContext)
                    + LaneEvent, UsageAggregate, CostLine        (content-free; HMAC ids)
        │  store (SQLite, idempotent upserts, source precedence, retention/purge)
        ▼
  PRICING  RateCard = builtin registry ⊕ user rates ⊕ contract overlay (effective-dated, sourced)
           exact Decimal lines → Figure(exact, basis)                  ← `bill`
        │
        ├──► RECONCILE vs aggregates/cost lines → coverage, rate-card error, residuals ← `reconcile`
        │
        ├──► SIMULATE  usage-level replay (all sources) | block-level replay (fingerprints)
        │    documented rules vs calibrated expectation; ASHRAE gate; diag confusion  ← `calibrate`
        │
        ├──► DETECT & PLAN  detectors + levers → Findings → Shapley over joint replays
        │    → realization-rate intervals → action plan → policy pack           ← `findings`, `whatif`, `policy`
        │
        └──► MEASURE & RECEIPT  constant-price DiD / A/B / stepped-wedge + guards
             → Receipt (canonical JSON) → DSSE + ssh-keygen signature           ← `measure`, `receipt`
  OUTPUTS  result@2 JSON | terminal | HTML (fleet/team/self) | FOCUS CSV | SARIF | ccusage JSON | trace@2 export
```

The legacy v0.1 engine (`trace.py`, `analyzer.py`, `simulator.py`, `breakers.py`, `report.py`,
`demo_traces.py`) is **frozen** and continues to serve `demo` and `analyze --engine v1` (the default for
trace@1 in 0.2.0) byte-for-byte. The v2 engine runs beside it and must agree with it on the demo corpus
(acceptance test in §3, F9).

### 2.2 Package layout and ownership

`WP-x` = owning work package (§5). **Frozen** = nobody edits. All `__init__.py` files are empty and owned
by WP-0.

```
tokenbill/
  __init__.py                 WP-Q (version bump only, phase 2)
  __main__.py                 frozen
  common.py                   frozen (TokenbillError, TraceError, rng, canonical_json)
  trace.py analyzer.py simulator.py breakers.py report.py demo_traces.py      frozen (v1 engine)
  pricing.py                  WP-P  (compat shim: PRICING/pricing_for/cost_breakdown/price_usd built from rates/)
  instrument.py               WP-A1 (recorder v2; keeps v0.1 API and behavior switches)
  cli.py                      WP-O  (argparse root; delegates to commands/)
  money.py labels.py jsonable.py secrets.py textsafe.py testing.py     WP-0
  ledger/records.py ledger/ids.py ledger/invariants.py                  WP-0
  contracts/pricing.py contracts/ingest.py contracts/sim.py contracts/findings.py
  contracts/recon.py contracts/verify.py contracts/store.py              WP-0
  adapters/base.py            WP-0  (Adapter protocol + registry)
  adapters/conventions.py     WP-0  (convention registry + `anthropic.messages` incl. the iterations rule)
  adapters/trace_v1.py adapters/trace_v2.py fingerprint.py              WP-A1
  adapters/claude_code.py                                              WP-A2
  adapters/conventions_ext.py adapters/otel.py adapters/openai.py adapters/bedrock.py   WP-A3
  adapters/anthropic_admin.py adapters/openai_admin.py                  WP-R
  adapters/all.py             WP-O  (imports every adapter module so they self-register)
  rates/registry.py rates/engine.py rates/models.py rates/contract.py rates/billing_rules.py rates/verify.py
  rates/data/*.json           WP-P
  store.py privacy.py         WP-S
  reconcile.py                WP-R
  sim/cache_rules.py sim/lanes.py sim/usage_replay.py sim/calibrate.py  WP-C1
  sim/block_replay.py detect/block.py                                   WP-C2
  detect/base.py              WP-0  (Detector protocol + registry)
  detect/usage.py detect/failure.py levers/ttl.py levers/compaction.py levers/routing.py
  levers/batch.py levers/plan.py policy_pack.py                          WP-D
  verify/stats.py verify/estimators.py verify/shapley.py verify/receipts.py verify/realization.py
  verify/label_policy.py verify/rollout.py verify/data/realization_priors.json   WP-V
  outputs/json_out.py outputs/terminal.py outputs/html.py outputs/focus.py outputs/sarif.py
  outputs/ccusage.py pipeline.py commands/*.py                           WP-O
  demo_fleet.py               WP-Q
tests/
  conftest.py                 WP-0 (shared fixtures only; subdirectories may add their own conftest.py)
  test_*.py (existing)        frozen; must stay green in every WP
  foundation/ rates/ adapters/trace/ adapters/claude_code/ adapters/telemetry/ adapters/admin/
  store/ reconcile/ sim/ blocksim/ verify/ detect/ outputs/ e2e/        one directory per WP
  fixtures/<same names>/      owned by the WP that owns the matching test directory
pyproject.toml                WP-0 in phase 0 (pytest --import-mode=importlib, dev deps), WP-Q in phase 2
.github/ docs/ README.md DESIGN.md SECURITY.md CHANGELOG.md   WP-Q (phase 2)
```

### 2.3 Data flow (one `tokenbill scan` run, content-free)

1. **Discover** sources (`~/.claude/projects/**.jsonl`, excluding `journal.jsonl`) and sniff adapters.
2. **Adapt** each file into `IngestResult` (requests, events, aggregates, cost lines, quarantine,
   data-quality notes). Adapters never price and never persist content above the configured tier.
3. **Store** with idempotent upserts keyed by stable IDs; cross-source duplicates (the same provider
   request seen in a transcript and in OTel) merge by source precedence (§2.8).
4. **Assemble lanes** (`sim/lanes.py`): per session, one lane per cache-continuity chain (main thread,
   each subagent, each workflow agent, helper models); requests ordered by `(ts_start_ms, seq)`.
5. **Price** every billable inference with the rate card effective at its request-start date → exact
   `PricedInference` lines; unknown models stay unpriced (never zero) and are counted in coverage.
6. **Reconcile** (when provider reports are present) → `ReconciliationReport`.
7. **Calibrate**: replay observed traffic under documented rules (one-step-ahead) → NMBE, CV(RMSE),
   per-band hit probabilities ρ, token-density fits, diagnostics confusion matrix → `CalibrationReport`.
8. **Detect** with every registered detector whose required capabilities are present → `Finding`s.
9. **Plan**: joint replays over interacting levers → Shapley credit → realization intervals → ranked
   action plan → optional policy pack.
10. **Render** result@2 JSON, terminal summary, HTML; enforce privacy (k≥5, no individual ranking)
    at the rendering boundary as well as in queries.

### 2.4 Canonical records (foundation, `tokenbill/ledger/records.py`)

Conventions: all dataclasses `frozen=True, slots=True`; all token counts `int` in `[0, 2**53]`; all
timestamps `int` milliseconds since Unix epoch UTC; all money `decimal.Decimal` USD (never `float`);
identifiers are opaque strings produced by `ledger/ids.py`; enums are `str` subclasses so they serialize
as their value. Pseudonymous identifiers are HMAC-SHA256 with an org or install key (§2.9).

```python
from __future__ import annotations
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

class ContentTier(str, Enum):
    NONE = "none"                # numbers, enums, HMAC ids only
    FINGERPRINT = "fingerprint"  # + per-block HMAC hashes and byte lengths (content-free)
    FULL = "full"                # + raw content, local-only, never exported

class LaneKind(str, Enum):
    MAIN = "main"; SUBAGENT = "subagent"; WORKFLOW_AGENT = "workflow_agent"; HELPER = "helper"
    COMPACTION = "compaction"; API_RUN = "api_run"; UNKNOWN = "unknown"

class InferenceKind(str, Enum):
    MESSAGE = "message"                  # ordinary billed inference (Anthropic iterations[].type == "message")
    COMPACTION = "compaction"            # server-side compaction pass (iterations[].type == "compaction")
    ADVISOR = "advisor"                  # advisor sub-inference, priced at the advisor model
    FALLBACK_DECLINED = "fallback_declined"  # refused attempt preceding a fallback_message
    FALLBACK = "fallback"                # iterations[].type == "fallback_message"
    KEEPALIVE = "keepalive"              # counterfactual-only: max_tokens=0 cache refresh
    OTHER = "other"

class UsageSource(str, Enum):
    FINAL = "final"                      # final usage frame / non-streaming response
    MESSAGE_START_ONLY = "message_start_only"  # output is the streaming placeholder (under-logged)
    PARTIAL_STREAM = "partial_stream"    # aborted stream: input from message_start + streamed output count
    ESTIMATED = "estimated"              # reconstructed (e.g. hidden compaction call from compact_boundary)
    PROVIDER_ROLLUP = "provider_rollup"  # aggregate source (never per request)

class Outcome(str, Enum):
    OK = "ok"; HTTP_ERROR = "http_error"; ABORTED = "aborted"; TIMEOUT = "timeout"
    REFUSED = "refused"; NETWORK_ERROR = "network_error"; UNKNOWN = "unknown"

@dataclass(frozen=True, slots=True)
class UsageBuckets:
    """Disjoint billed token buckets. total_input = uncached + read + all writes.
    Provider conventions are normalized INTO this shape by adapters (see §2.5)."""
    uncached_input: int = 0          # Anthropic input_tokens; OpenAI input - cached - write
    cache_read: int = 0
    cache_write_5m: int = 0          # Anthropic ephemeral_5m (1.25x)
    cache_write_1h: int = 0          # Anthropic ephemeral_1h (2x)
    cache_write_30m: int = 0         # OpenAI GPT-5.6+ writes (1.25x, 30-minute TTL)
    cache_write_unknown: int = 0     # writes whose TTL split the source did not report
    output: int = 0                  # billed output INCLUDING thinking/reasoning
    output_reasoning: int | None = None   # informational subset of output (never added)
    web_search_requests: int = 0
    web_fetch_requests: int = 0
    # __post_init__: every int >= 0 and <= 2**53; output_reasoning <= output when not None.
    # properties: cache_write (sum of 4 write buckets), total_input.
    # __add__: bucket-wise sum; output_reasoning summed only if both sides are not None.

@dataclass(frozen=True, slots=True)
class PricingContext:
    """Everything besides token counts that selects a price. Filled by adapters, read by rates/."""
    provider: str            # "anthropic" | "openai" | "google" | ...
    channel: str             # "anthropic_api" | "bedrock" | "vertex" | "foundry" | "claude_platform_aws"
                             # | "openai_api" | "azure_openai" | "openrouter" | "unknown"
    model: str               # normalized served model id (rates/models.py), e.g. "claude-opus-5-5"
    model_raw: str           # exactly as reported
    service_tier: str = "standard"   # "standard" | "batch" | "flex" | "priority" | "fast" | "unknown"
    speed: str = "standard"          # Anthropic "standard" | "fast"
    inference_geo: str | None = None # "us" | "global" | "not_available" | None
    endpoint_scope: str = "global"   # "global" | "regional" | "multi_region" (Bedrock/Vertex)
    write_ttl_hint: str | None = None  # for cache_write_unknown: "5m" | "1h" | None (see §2.6 R5)

@dataclass(frozen=True, slots=True)
class Inference:
    """Atomic priced unit: one element of Anthropic usage.iterations, or the whole usage when absent."""
    inference_id: str
    kind: InferenceKind
    usage: UsageBuckets
    pricing: PricingContext
    usage_source: UsageSource = UsageSource.FINAL
    billable: bool | None = True     # None = billing rule uncertain (priced as a range)
    billing_rule_id: str | None = None   # e.g. "anthropic.refusal.pre_output" (rates/data/billing_rules.json)
    provider_reported_cost: Decimal | None = None   # e.g. xAI ticks, OpenRouter cost, OTel cost_usd
    provider_reported_cost_basis: str | None = None # Basis value; OTel/Claude Code = "provider_estimate"

@dataclass(frozen=True, slots=True)
class CacheDiagnostic:
    reason: str                      # "model_changed"|"system_changed"|"tools_changed"|"messages_changed"
                                     # |"previous_message_not_found"|"unavailable"|provider-specific
    missed_input_tokens_estimate: int | None  # magnitude only; NEVER priced
    source: str                      # "anthropic.cache_diagnostics" | "openai.prompt_cache_diagnostics"

@dataclass(frozen=True, slots=True)
class Attempt:
    attempt_id: str
    attempt_no: int                  # 0-based within the logical request
    ts_start_ms: int                 # request start (cache TTL is measured from here)
    ttft_ms: int | None
    duration_ms: int | None
    outcome: Outcome
    http_status: int | None
    error_type: str | None
    retry_layer: str | None          # "sdk" | "agent" | "gateway" | None
    retry_after_ms: int | None
    provider_request_id: str | None  # "req_..." (joins OTel <-> transcripts <-> recorder)
    provider_message_id: str | None  # "msg_..." (Claude Code de-duplication key)
    model_served: str | None
    stop_reason: str | None
    inferences: tuple[Inference, ...]
    diagnostics: CacheDiagnostic | None = None
    applied_edits: tuple[tuple[str, int], ...] = ()   # (edit type, cleared_input_tokens)

@dataclass(frozen=True, slots=True)
class Breakpoint:
    block_index: int                 # index into ContentFingerprint.blocks
    ttl: str                         # "5m" | "1h" | "30m"

@dataclass(frozen=True, slots=True)
class RequestParams:
    """Prompt-affecting parameters (the invalidation hierarchy lives outside rendered bytes)."""
    model_requested: str
    max_tokens: int | None = None
    stream: bool | None = None
    thinking: str | None = None      # normalized: "off" | "adaptive" | "enabled:<budget>" | hash
    effort: str | None = None        # "low"|"medium"|"high"|"xhigh"|"max"
    tool_choice: str | None = None   # normalized or HMAC hash
    output_format: str | None = None # HMAC hash of output_config.format
    speed: str | None = None
    service_tier_requested: str | None = None
    inference_geo_requested: str | None = None
    betas: tuple[str, ...] = ()      # sorted anthropic-beta values
    breakpoints: tuple[Breakpoint, ...] = ()
    automatic_caching: bool | None = None   # top-level cache_control present
    context_management: str | None = None   # hash of the context_management config
    task_budget: int | None = None
    web_search_enabled: bool | None = None
    citations_enabled: bool | None = None
    has_images: bool | None = None
    advisor_model: str | None = None

@dataclass(frozen=True, slots=True)
class BlockRef:
    """One rendered block in wire order, content-free. Hashes are HMAC-SHA256 hex[:32] with key_id."""
    h: str                           # wire bytes with cache_control keys stripped (marker moves never alter h)
    h_sorted: str | None             # key-sorted canonical rendering (serialization-churn detection)
    h_norm: str | None               # h after replacing volatile spans by class placeholders
    tier: str                        # "tools" | "system" | "messages"
    kind: str                        # "tool_def"|"system_text"|"text"|"tool_use"|"tool_result"|"image"
                                     # |"document"|"thinking"|"redacted_thinking"|"compaction"|"other"
    role: str | None
    n_bytes: int
    est_tokens: int | None           # estimate (calibrated density); never billed
    image_px: tuple[int, int] | None = None
    volatile_classes: tuple[str, ...] = ()   # computed BEFORE hashing: "iso_datetime","uuid","unix_ts","counter"
    lookback_pos: int = 0            # position index with tool_use / tool_result runs collapsed

@dataclass(frozen=True, slots=True)
class ContentFingerprint:
    key_id: str                      # hashes are only comparable within one key
    blocks: tuple[BlockRef, ...]     # BlockRef objects are interned and shared across requests
    tier_end: tuple[int, int, int]   # index after last tools / system / messages block

@dataclass(frozen=True, slots=True)
class AppendedItem:
    """Content-free summary of what entered the context since the previous request in the lane."""
    kind: str                        # "tool_result" | "user_text" | "attachment" | "image" | "assistant"
    name: str | None                 # tool name / attachment type (config identifiers, not content)
    n_bytes: int
    is_error: bool = False
    images: int = 0

@dataclass(frozen=True, slots=True)
class Attribution:
    principal: str | None = None     # HMAC pseudonym "p_<20hex>"; only rendered in self-view
    team: str | None = None
    cost_center: str | None = None
    project: str | None = None
    repo: str | None = None          # HMAC unless policy allows plain names
    workspace_id: str | None = None
    api_key_id: str | None = None    # HMAC
    agent_product: str | None = None # "claude_code" | "codex" | "agent_sdk" | "api" | ...
    agent_type: str | None = None    # "general-purpose" | "Explore" | "workflow-subagent" | custom
    query_source: str | None = None  # "main" | "subagent" | "auxiliary" | "compaction"
    skill: str | None = None
    mcp_server: str | None = None
    workload_class: str | None = None  # "interactive"|"ci"|"scheduled"|"eval"|"batch"|"unknown"
    entrypoint: str | None = None
    client_version: str | None = None
    billing_path: str | None = None  # "api_key"|"subscription"|"usage_credits"|"bedrock"|"vertex"|"foundry"|"claude_platform_aws"
    extra: tuple[tuple[str, str], ...] = ()

@dataclass(frozen=True, slots=True)
class SourceRef:
    adapter: str                     # "claude-code" | "trace@1" | "trace@2" | "otel" | ...
    source_id: str                   # stable id of the input (HMAC of path or supplied name)
    locator: str                     # "line:1234" etc.; content-free
    priority: int                    # source precedence (§2.8)

@dataclass(frozen=True, slots=True)
class Request:
    """One logical request (what the client meant to send once). >=1 attempts."""
    request_id: str
    session_key: str
    lane_key: str
    seq: int                         # order within lane
    attribution: Attribution
    params: RequestParams
    attempts: tuple[Attempt, ...]
    fingerprint: ContentFingerprint | None = None
    appended: tuple[AppendedItem, ...] = ()
    source: SourceRef | None = None
    # properties: ts_start_ms (attempts[0]), final_attempt (last attempt), billable_inferences
    # (every inference in every attempt whose billable is not False).

class LaneEventKind(str, Enum):
    COMPACTION = "compaction"; CLEAR = "clear"; RESUME = "resume"; MODEL_SWITCH_USER = "model_switch_user"
    FALLBACK_REFUSAL = "fallback_refusal"; FALLBACK_AVAILABILITY = "fallback_availability"
    API_ERROR = "api_error"; CONTEXT_EDIT = "context_edit"; UPGRADE = "upgrade"
    CONTEXT_INJECTION = "context_injection"; IMAGE_EVICTION = "image_eviction"

@dataclass(frozen=True, slots=True)
class LaneEvent:
    lane_key: str
    ts_ms: int
    kind: LaneEventKind
    attrs: tuple[tuple[str, str | int], ...] = ()  # e.g. ("pre_tokens", 993070), ("post_tokens", 20283)

@dataclass(frozen=True, slots=True)
class Lane:
    lane_key: str
    session_key: str
    kind: LaneKind
    parent_lane_key: str | None
    cache_scope_key: str             # cache isolation domain: workspace (1P/Claude Platform on AWS/Foundry)
                                     # or organization (Bedrock/Vertex); "unknown" if not derivable
    requests: tuple[Request, ...]
    events: tuple[LaneEvent, ...] = ()

@dataclass(frozen=True, slots=True)
class Session:
    session_key: str                 # globally unique: source-qualified (fixes cb-dup-runid)
    source_kind: str
    attribution: Attribution
    lanes: tuple[Lane, ...]
    started_ms: int
    ended_ms: int

@dataclass(frozen=True, slots=True)
class UsageAggregate:
    """Provider-side aggregate (Admin usage_report bucket, Claude Code Analytics user-day, OTel metric)."""
    agg_id: str
    source_kind: str                 # "anthropic.usage_report" | "anthropic.cc_analytics" |
                                     # "anthropic.enterprise_usage" | "openai.usage" | "otel.metric"
    bucket_start_ms: int
    bucket_end_ms: int
    dims: tuple[tuple[str, str], ...]   # sorted; workspace_id, api_key_id, model, service_tier,
                                        # inference_geo, speed, context_window, principal, product
    usage: UsageBuckets
    reported_cost: Decimal | None = None
    reported_cost_basis: str | None = None   # "invoice"|"contract"|"provider_estimate"|"list"
    list_cost: Decimal | None = None         # Enterprise Analytics list_amount
    finality: str = "provisional"            # "provisional" | "final"
    fetched_ms: int = 0

@dataclass(frozen=True, slots=True)
class CostLine:
    """One row of a provider cost report (Anthropic cost_report: daily, cents as decimal strings)."""
    line_id: str
    source_kind: str                 # "anthropic.cost_report" | "openai.costs" | ...
    date_utc: str                    # "YYYY-MM-DD"
    workspace_id: str | None
    description: str
    model: str | None
    cost_type: str | None
    token_type: str | None
    service_tier: str | None
    inference_geo: str | None
    amount: Decimal                  # USD, exact (from cents string / 100)
    currency: str = "USD"
    finality: str = "provisional"
    fetched_ms: int = 0

@dataclass(frozen=True, slots=True)
class UsageRecord:
    """Denormalized row (one per billable Inference) for storage, group-by and export."""
    inference_id: str; request_id: str; attempt_id: str; session_key: str; lane_key: str
    lane_kind: LaneKind; ts_ms: int; date_utc: str
    kind: InferenceKind; usage_source: UsageSource; billable: bool | None; billing_rule_id: str | None
    pricing: PricingContext; usage: UsageBuckets; attribution: Attribution
```

`ledger/ids.py`:

```python
def stable_id(prefix: str, *parts: str | int) -> str      # prefix + "_" + sha256("\x1f".join(parts))[:24]
def hmac_id(key: bytes, prefix: str, value: str) -> str    # prefix + "_" + HMAC-SHA256(key, value)[:20]
def hmac_block(key: bytes, data: bytes) -> str             # HMAC-SHA256(key, data).hexdigest()[:32]
def key_id(key: bytes) -> str                              # "k_" + sha256(b"tokenbill-key-id\0" + key)[:12]
def request_id_for(provider_message_id: str | None, provider_request_id: str | None,
                   source_id: str, locator: str) -> str    # prefers provider ids so re-imports are idempotent
```

`ledger/invariants.py` (every adapter test calls these):

```python
def check_usage(u: UsageBuckets) -> list[str]                  # structural problems, empty if fine
def check_request(r: Request) -> list[str]                     # attempt order, ids, ts monotonic, iteration kinds
def check_lane(l: Lane) -> list[str]                           # seq strictly increasing, ts non-decreasing
def sum_check(u: UsageBuckets, provider_total_input: int | None,
              provider_output: int | None) -> list[str]        # normalized buckets reproduce provider totals
def ledger_totals_equal(requests, aggregates_by_run) -> bool   # finance invariant: Σ run totals == Σ call totals
```

### 2.5 Cached-token accounting across providers (the normalizer contract)

Canonical buckets are **disjoint** and **exclusive**: `total_input = uncached_input + cache_read +
cache_write_5m + cache_write_1h + cache_write_30m + cache_write_unknown`; output includes reasoning and
thinking. Adapters convert every source into this shape via the **convention registry**
(`adapters/conventions.py`), keyed by `(source, version range, provider route)`. Each convention has a
golden fixture whose Σ buckets equals the provider's billed total (`ent-usage-normalization`,
`fw-telemetry-conventions`).

| Source (convention id) | Provider semantics | Mapping into buckets | Sum-check |
|---|---|---|---|
| `anthropic.messages` (1P, Claude Platform on AWS, Foundry, Vertex rawPredict, Bedrock InvokeModel, Claude Code transcripts) | `input_tokens` **excludes** cache; `cache_read_input_tokens`; `cache_creation_input_tokens` = Σ `cache_creation.ephemeral_{5m,1h}_input_tokens`; `usage.iterations[]` is the per-attempt billing record; `server_tool_use.{web_search_requests,web_fetch_requests}`; `output_tokens_details.thinking_tokens` | uncached=`input_tokens`; read=`cache_read_input_tokens`; 5m/1h from `cache_creation`; unknown = `cache_creation_input_tokens − 5m − 1h` if >0; reasoning=`thinking_tokens`; iterations rule below | top-level vs iterations (below) |
| `bedrock.converse` | `inputTokens` excludes cache; `cacheReadInputTokens`; `cacheWriteInputTokens`; `cacheDetails[{ttl,inputTokens}]` | uncached=`inputTokens`; read; writes by `cacheDetails.ttl`; remainder→unknown | Σ cacheDetails ≤ cacheWrite |
| `openai.responses` (GPT-5.6+) | `input_tokens` **includes** cached and cache-write; `input_tokens_details.cached_tokens`, `.cache_write_tokens`; `output_tokens` includes `output_tokens_details.reasoning_tokens` | uncached = input − cached − write; read=cached; 30m=write; output=output; reasoning subset | cached + write ≤ input |
| `openai.chat` | `prompt_tokens` includes `prompt_tokens_details.cached_tokens`; `completion_tokens` includes reasoning; `rejected_prediction_tokens` billed | as above; write=0 before GPT-5.6 (no write fee; cached rounded down to 128) | cached ≤ prompt |
| `otel.genai` (semantic-conventions-genai ≥1.42) | `gen_ai.usage.input_tokens` **includes** `cache_read.input_tokens` and `cache_write.input_tokens`; `reasoning.output_tokens` subset | uncached = input − read − write; writes→unknown TTL | read + write ≤ input |
| `otel.genai.legacy` (semconv 1.40–1.41) | same with `gen_ai.usage.cache_creation.input_tokens` | same | same |
| `openinference` | `llm.token_count.prompt` includes `prompt_details.cache_read/cache_write`; count **only LLM-kind spans** (never AGENT/CHAIN roll-ups) | same | same |
| `claude_code.otel` (`claude_code.api_request` event) | Anthropic semantics (exclusive) with `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens` (**no TTL split**); `cost_usd` is a client-side list estimate unless `modelPricing` is set | exclusive; writes→unknown TTL; `cost_usd`→`provider_reported_cost` with basis `provider_estimate` | n/a |
| `anthropic.usage_report` (aggregate) | uncached input, cache read, `cache_creation` 5m/1h, output, `server_tool_use.web_search_requests` | aggregate buckets | per bucket |
| `anthropic.cc_analytics` (aggregate) | `model_breakdown[].tokens.{input,output,cache_read,cache_creation}`, `estimated_cost` cents (list estimate) | exclusive (verify with fixture); writes→unknown | n/a |
| `codex.rollout` | `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens` | **convention unverified**: adapter refuses to price until a pinned fixture confirms inclusive vs exclusive (`codex-usage-format`) | fixture-gated |
| `tokenbill.trace@1` (legacy files) | the four v0.1 fields, Anthropic exclusive semantics; no TTL split, no iterations | uncached, read, output as-is; `cache_creation_input_tokens` → `cache_write_5m` (pinned by the v0.1 format contract, rule R5) | Σ equals v0.1 `total_input` |

**Iterations rule (Anthropic).** If `usage.iterations` is a non-empty list, price **each iteration** as
its own `Inference` and ignore top-level usage for pricing; check the invariant that top-level equals
either the last iteration (fallback), the sum of iterations, or the single iteration, else emit a
data-quality note and keep iterations. Kind by `type`: `message`→MESSAGE, `compaction`→COMPACTION,
`advisor_message`→ADVISOR, `fallback_message`→FALLBACK; a `message` iteration followed by a
`fallback_message` is FALLBACK_DECLINED. Model = `iterations[].model` or the message model; ADVISOR
iterations with no model use `params.advisor_model`, else unpriced with reason. FALLBACK_DECLINED with
`output_tokens == 0` is `billable=False, billing_rule_id="anthropic.refusal.pre_output"`; with output > 0
it is billable under `"anthropic.refusal.mid_stream"` (input plus streamed output)
(`fp-anth-refusal-iterations`, `cc-iterations-fallback` corrected). Evidence: `anth-iterations-undercount`,
`compaction-iterations-p0`, `advisor-iterations-undercount`, empirical 04_semantics (len 1: sum == top in
56,554/56,554; len 2: last == top in 27/27).

Other accounting rules: thinking is billed as output even when hidden (`thinking-billing-invisible`);
image tokens are inside input counts (informational `ceil(w/28)·ceil(h/28)` estimates only,
`anth-vision-tokens`); hidden per-request tool-use system prompt tokens (286–804 depending on model) are
inside provider counts and attributed to a `provider_overhead` segment, not smeared
(`anth-hidden-overhead-tokens`); OpenAI pre-5.6 cached counts are multiples of 128 and Mistral of 64
(quantization is expected, not an error); `cache_missed_input_tokens` and OTel `cost_usd` are never
billed numbers.

### 2.6 Money and label rules (enforced in code)

`tokenbill/money.py`:

```python
EXACT_CTX = Context(prec=60, rounding=ROUND_HALF_EVEN,
                    traps=[InvalidOperation, DivisionByZero, Overflow, Inexact])   # rounding raises
RATIO_CTX = Context(prec=28, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, DivisionByZero, Overflow])
MTOK = Decimal(1_000_000)

def usd(value: str | int | Decimal) -> Decimal           # float/bool/NaN/Inf -> TypeError/ValueError
def from_cents(value: str | int | Decimal) -> Decimal    # "12345.678" cents -> Decimal("123.45678")
def from_ticks(ticks: int, per_usd: int = 10**10) -> Decimal
def mul(*factors: Decimal | int) -> Decimal              # exact under EXACT_CTX
def token_cost(tokens: int, usd_per_mtok: Decimal, *multipliers: Decimal) -> Decimal
def dsum(values: Iterable[Decimal]) -> Decimal           # exact
def ratio(num: Decimal, den: Decimal) -> Decimal | None  # RATIO_CTX; None when den == 0
def to_micros(value: Decimal) -> int                     # ROUND_HALF_EVEN to 1e-6 USD (output/interchange only)
def from_micros(micros: int) -> Decimal
def fmt_usd(value: Decimal | None, places: int = 2) -> str   # "$1,234.57"; "unpriced" for None

@dataclass(frozen=True, slots=True)
class MoneyRange:
    low: Decimal
    high: Decimal     # low <= high; __add__ adds bounds
```

`tokenbill/labels.py`:

```python
class Evidence(str, Enum):
    EXACT = "exact"          # provider-billed usage x sourced rate row; pure arithmetic, no model
    ESTIMATED = "estimated"  # anything involving a model, inference or assumption
    MEASURED = "measured"    # observational causal estimate on the billed ledger, with CI
    VERIFIED = "verified"    # randomized design with every guard passing, with CI
STRENGTH = {Evidence.EXACT: 3, Evidence.VERIFIED: 2, Evidence.MEASURED: 1, Evidence.ESTIMATED: 0}

class Basis(str, Enum):
    LIST = "list"; CONTRACT = "contract"; INVOICE = "invoice"; PROVIDER_ESTIMATE = "provider_estimate"
class Finality(str, Enum):
    PROVISIONAL = "provisional"; FINAL = "final"; NA = "n/a"
class Calibration(str, Enum):
    CALIBRATED = "calibrated"; UNCALIBRATED = "uncalibrated"; NA = "n/a"

@dataclass(frozen=True, slots=True)
class Figure:
    usd: Decimal | None
    evidence: Evidence
    basis: Basis
    finality: Finality = Finality.NA
    range: MoneyRange | None = None
    ci_level: Decimal | None = None
    calibration: Calibration = Calibration.NA
    upper_bound: bool = False
    provenance: tuple[str, ...] = ()   # rate-row ids, source ids, replay ids, receipt ids
    note: str = ""
    # __post_init__ rules (raise ValueError):
    #  usd is None            -> note required ("unpriced: <reason>")
    #  EXACT                  -> range None, calibration NA, upper_bound False
    #  ESTIMATED              -> calibration != NA or note names the assumption (e.g. TTL hint)
    #  MEASURED / VERIFIED    -> range and ci_level required
    #  basis INVOICE          -> evidence EXACT

def add(a: Figure, b: Figure) -> Figure      # basis must match; evidence = weaker; ranges add;
                                             # usd None if either None (coverage handled separately)
```

Rules (each has a test in WP-0 or WP-O):

- **R1 No floats in money.** Rates are JSON strings, parsed with `usd()`; `EXACT_CTX` traps `Inexact`, so any
  silent rounding in pricing raises. Rounding happens only in `to_micros` and `fmt_usd`.
- **R2 Unknown is not zero.** Unpriced inferences produce `Figure(usd=None)`; totals report
  `priced_usd`, `unpriced_inferences`, `unpriced_tokens`, `coverage` (fixes `cb-pricing-coverage`).
- **R3 Billed columns are exact.** Renderers accept only `Evidence.EXACT` with basis LIST, CONTRACT or
  INVOICE in any column titled billed/bill/spend; estimates render in an "estimated" column with range.
- **R4 Provider estimates are not bills.** OTel `cost_usd`, Claude Code Analytics `estimated_cost`, `/usage`
  numbers and `cache_missed_input_tokens` carry basis `provider_estimate` and are shown only in
  reconciliation context (`cc-list-price-estimates`).
- **R5 Unknown TTL writes are ranges.** `cache_write_unknown` is priced at the 5m rate (low) and the 1h rate
  (high); the point estimate uses `write_ttl_hint` when present; evidence ESTIMATED. Exception: trace@1
  pins writes to 5m by its format contract (v0.1 SPEC), so trace@1 dollars stay EXACT under that
  contract, and the output carries the footnote that trace@1 cannot represent 1h writes.
- **R6 Estimates declare calibration.** Every ESTIMATED savings figure says whether its replay passed the
  calibration gate (§3 F7); uncalibrated projections cannot be signed or labeled measured/verified.
- **R7 No summing of overlapping ceilings.** Totals of savings come from joint replays; per-lever credit
  is Shapley (§3 F10).
- **R8 Constant prices for ex-post savings.** Before/after comparisons re-price both periods at the
  baseline rate card; price changes are reported separately as an exact "rate variance"
  (`staggered-did`, `finops-rate-vs-usage`).

### 2.7 Pricing registry (`tokenbill/rates/`)

**Data model.** Rates are JSON package data (`rates/data/anthropic.json`, `openai.json`, `bedrock.json`,
`vertex.json`, `billing_rules.json`), loaded with `importlib.resources`, numbers as strings.

```json
{
  "schema": "tokenbill/rates@1",
  "provider": "anthropic",
  "as_of": "2026-09-23",
  "rows": [
    {
      "row_id": "anthropic/anthropic_api/claude-opus-5-5/2026-09-22",
      "channel": "anthropic_api",
      "model": "claude-opus-5-5",
      "aliases": [],
      "effective_from": "2026-09-22",
      "effective_to": null,
      "usd_per_mtok": {"input": "4.00", "output": "20.00"},
      "multipliers": {"cache_read": "0.05", "cache_write_5m": "1.25", "cache_write_1h": "2"},
      "published_absolute": {"cache_read": "0.20", "cache_write_5m": "5.00", "cache_write_1h": "8.00"},
      "min_cacheable_tokens": 512,
      "tokenizer_family": "claude-4.7+",
      "per_request_usd": {"web_search": "0.01"},
      "verified_on": "2026-09-23",
      "sources": [{"url": "https://platform.claude.com/docs/en/about-claude/pricing",
                   "retrieved": "2026-09-23", "finding": "anth-pricing-table-2026-09"}]
    }
  ],
  "modifiers": [
    {"modifier_id": "anthropic.batch", "kind": "multiply", "factor": "0.5",
     "applies_to": ["*"], "when": {"service_tier": "batch"},
     "stacking": "documented", "sources": [{"url": "https://platform.claude.com/docs/en/build-with-claude/batch-processing"}]},
    {"modifier_id": "anthropic.inference_geo.us", "kind": "multiply", "factor": "1.1",
     "applies_to": ["*"], "when": {"inference_geo": "us", "model_generation_gte": "4.6",
     "channel_in": ["anthropic_api", "claude_platform_aws"]}, "stacking": "documented"},
    {"modifier_id": "anthropic.fast.opus-5-5", "kind": "replace_base",
     "usd_per_mtok": {"input": "8.00", "output": "40.00"},
     "when": {"speed": "fast", "model_in": ["claude-opus-5-5"], "channel_in": ["anthropic_api"]},
     "stacking": "documented", "note": "cache multipliers apply on top of the fast base"}
  ]
}
```

**Resolution algorithm** (`rates/engine.py`, `resolve(ctx, ts_ms) -> ResolvedRates | None`):

1. `rates/models.py` normalizes `ctx.model_raw` → `(channel, model, endpoint_scope)`: strip `-YYYYMMDD`
   and Vertex `@YYYYMMDD` snapshot suffixes; strip Claude Code's `[1m]` suffix (no long-context premium on
   Anthropic, `cc-autocompact-window`); parse Bedrock ids (`anthropic.claude-…-v1:0` → in-region,
   `global.anthropic.…` → global, geo prefixes `us.`/`eu.`/`apac.`/`jp.` → regional per the Bedrock price
   list; see §4 "verify"); `<synthetic>` → skip; config aliases (`opus`, `sonnet`, `haiku`) → never priced.
2. Layered lookup, highest layer first: **contract overlay** (per-model overrides) → **user rates files**
   (`--rates`, `--model-price`) → **built-in registry**. Within a layer, the row whose half-open interval
   `[effective_from, effective_to)` contains the UTC date of `ts_ms`. Overlapping intervals for the same
   `(channel, model)` fail validation at load time.
3. Apply `replace_base` modifiers (fast mode), then derive cache rates = base input × row multipliers
   (validator checks derived == `published_absolute` for the base tier), then apply every matching
   `multiply` modifier (batch, geo, regional endpoint, Priority burn-down). Multiplication is exact, so
   stacking order does not change the result; the stacking assumption is recorded per modifier
   (`documented` or `assumed`; the research could not confirm every combination, research track `papers-measurement` open
   question) and surfaced in provenance.
4. Contract overlay last: `multiplier` (e.g. `0.85`) on every bucket, or per-model overrides of
   `input/output/cacheRead/cacheWrite5m/cacheWrite1h`; basis becomes CONTRACT.

**Pricing an inference** (`price_inference`): one `PricedLine` per non-zero bucket and per server-tool
counter, `amount = token_cost(tokens, base, multipliers…)` under `EXACT_CTX`; `total = dsum(lines)`;
evidence EXACT unless `cache_write_unknown > 0` (range per R5), `billable is None` (range `[0, total]`,
ESTIMATED), or `usage_source in {MESSAGE_START_ONLY, PARTIAL_STREAM, ESTIMATED}` (see §3 F4). Unknown
model → `total=None`, `unpriced_reason`. Long-context bands (OpenAI >272K, Gemini >200K, xAI ≥200K) price
the **whole request** at the band row when `total_input` exceeds the threshold.

**Contract overlay** (`rates/contract.py`) accepts Token Bill's own schema and imports Claude Code's
`modelPricing` managed-setting JSON (multiplier + overrides for input/output/cacheRead/cacheWrite;
`cacheWrite` covers both 5m and 1h writes per the settings reference, so on import 1h is derived as
`cacheWrite × 2/1.25` and flagged `assumed` unless the contract states it). It can also **emit** a
`modelPricing` block so developer-facing `/usage` and OTel `cost_usd` match the contract
(`cc-list-price-estimates`, `cc-anthropic-discount-visibility`). CCU channels (Claude Platform on AWS,
Foundry: $0.01 per CCU, discount applied at conversion) are modeled as a contract multiplier derived by
reconciliation (`claude-marketplace-ccu`, `cc-ccu-private-offers`).

**Failure-mode billing rules** (`rates/data/billing_rules.json`, `fp-billing-matrix`): rows
`(provider, failure_mode) → {billed_input, billed_partial_output, cache_written}` each `yes|no|unknown`,
with `confidence` (`documented|assumed|unknown`) and source. Documented rows price EXACT; `unknown` rows
price as a range `[0, full]`, ESTIMATED, and the report shows "assumed" dollars separately. Seed rows:
Anthropic pre-output refusal = not billed (documented); mid-stream refusal = input + streamed output
(documented); Batch item error/cancel/expire = not billed (documented); failed web search = not billed
(documented); client abort / mid-stream `overloaded_error` / pre-token 429/529 = unknown; Vertex non-200 =
not billed; Gemini 400/500 = not billed; Azure OpenAI 400 content-filter and 408 = billed; OpenAI Flex 429
= not billed.

**Registry identity and staleness.** `RateCard.sha256` = SHA-256 of the canonical JSON of all layers in
use; it appears in every output and receipt. Rows carry `verified_on`; any row used whose `verified_on` is
older than 45 days emits a `stale-rate` data-quality note. `tokenbill pricing verify` diffs the registry
against (a) a recorded snapshot fixture offline and (b) optionally the live pricing page
(`--live`, opt-in network), and (c) optionally secondary feeds (LiteLLM map, OpenRouter) as **non-authoritative
cross-checks** because they disagree with primary sources (`price-feed-crosscheck`: OpenRouter listed
gpt-5.6-sol at $2/$10 vs OpenAI's $4/$20). A weekly CI job runs the live check and opens an issue on
drift (`anth-evidence-drift`: three Anthropic price moves in one quarter).

**Lifecycle calendar** (`rates/data/lifecycle.json`): retirement floors (Sonnet 4.5 2026-09-29, Haiku 4.5
2026-10-15, Opus 4.5 2026-11-24), promotion cliffs (Gemini 3.8 Flash doubles 2027-01-01), announced
launches (Sonnet 5.5, Haiku 5.5 "in the coming weeks") (`cc-timing-calendar`). Used only for warnings.

### 2.8 Storage (`tokenbill/store.py`, SQLite via stdlib `sqlite3`)

One file per ledger, created `0600`, WAL mode, `PRAGMA foreign_keys=ON`. Money columns are TEXT holding
exact decimal strings (aggregation happens in Python with `Decimal`; SQL never sums money). Schema
version in `meta`; migrations are forward-only functions.

```sql
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);       -- schema_version, key_id, content_tier
CREATE TABLE sources(source_id TEXT PRIMARY KEY, adapter TEXT NOT NULL, name_hmac TEXT, sha256 TEXT,
  ingested_ms INTEGER, records INTEGER, quarantined INTEGER, stats_json TEXT);
CREATE TABLE sessions(session_key TEXT PRIMARY KEY, source_kind TEXT, attribution_json TEXT,
  started_ms INTEGER, ended_ms INTEGER);
CREATE TABLE lanes(lane_key TEXT PRIMARY KEY, session_key TEXT NOT NULL REFERENCES sessions,
  kind TEXT, parent_lane_key TEXT, cache_scope_key TEXT);
CREATE TABLE requests(request_id TEXT PRIMARY KEY, lane_key TEXT NOT NULL REFERENCES lanes,
  session_key TEXT NOT NULL, seq INTEGER NOT NULL, ts_start_ms INTEGER NOT NULL, date_utc TEXT NOT NULL,
  attribution_json TEXT, params_json TEXT, appended_json TEXT, source_json TEXT, source_priority INTEGER);
CREATE TABLE attempts(attempt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests,
  attempt_no INTEGER, ts_start_ms INTEGER, ttft_ms INTEGER, duration_ms INTEGER, outcome TEXT,
  http_status INTEGER, error_type TEXT, retry_layer TEXT, retry_after_ms INTEGER,
  provider_request_id TEXT, provider_message_id TEXT, model_served TEXT, stop_reason TEXT,
  diag_reason TEXT, diag_missed_tokens INTEGER, diag_source TEXT, applied_edits_json TEXT);
CREATE TABLE inferences(inference_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES attempts,
  request_id TEXT NOT NULL, lane_key TEXT NOT NULL, ts_ms INTEGER NOT NULL, date_utc TEXT NOT NULL,
  kind TEXT, usage_source TEXT, billable INTEGER, billing_rule_id TEXT,
  provider TEXT, channel TEXT, model TEXT, model_raw TEXT, service_tier TEXT, speed TEXT,
  inference_geo TEXT, endpoint_scope TEXT, write_ttl_hint TEXT,
  uncached_input INTEGER, cache_read INTEGER, cache_write_5m INTEGER, cache_write_1h INTEGER,
  cache_write_30m INTEGER, cache_write_unknown INTEGER, output INTEGER, output_reasoning INTEGER,
  web_search_requests INTEGER, web_fetch_requests INTEGER,
  provider_cost TEXT, provider_cost_basis TEXT,
  team TEXT, principal TEXT, workspace_id TEXT, api_key_id TEXT, repo TEXT, agent_product TEXT,
  query_source TEXT, workload_class TEXT, lane_kind TEXT);
CREATE TABLE blocks(h TEXT PRIMARY KEY, key_id TEXT, h_sorted TEXT, h_norm TEXT, tier TEXT, kind TEXT,
  role TEXT, n_bytes INTEGER, est_tokens INTEGER, image_w INTEGER, image_h INTEGER, volatile_classes TEXT);
CREATE TABLE request_fingerprints(request_id TEXT PRIMARY KEY REFERENCES requests,
  parent_request_id TEXT, keep INTEGER, append_json TEXT, markers_json TEXT, tier_end_json TEXT);
CREATE TABLE events(lane_key TEXT NOT NULL, ts_ms INTEGER NOT NULL, kind TEXT NOT NULL, attrs_json TEXT);
CREATE TABLE aggregates(agg_id TEXT PRIMARY KEY, source_kind TEXT, bucket_start_ms INTEGER,
  bucket_end_ms INTEGER, dims_json TEXT, usage_json TEXT, reported_cost TEXT, reported_cost_basis TEXT,
  list_cost TEXT, finality TEXT, fetched_ms INTEGER);
CREATE TABLE cost_lines(line_id TEXT PRIMARY KEY, source_kind TEXT, date_utc TEXT, workspace_id TEXT,
  description TEXT, model TEXT, cost_type TEXT, token_type TEXT, service_tier TEXT, inference_geo TEXT,
  amount TEXT NOT NULL, currency TEXT, finality TEXT, fetched_ms INTEGER);
CREATE TABLE message_index(provider_message_id TEXT PRIMARY KEY, request_id TEXT NOT NULL);
CREATE TABLE audit(ts_ms INTEGER NOT NULL, actor TEXT, action TEXT NOT NULL, detail_json TEXT);
-- indexes: inferences(date_utc, model), inferences(lane_key, ts_ms), requests(lane_key, seq),
-- attempts(request_id), attempts(provider_request_id), events(lane_key, ts_ms)
```

**Idempotency and cross-source de-duplication.** Upsert key is `request_id`. Before insert, the store
looks up `provider_message_id` / `provider_request_id`; if the same provider request already exists from
another source, the record with the higher `source_priority` wins for usage (recorder trace@2 = 50,
Claude Code transcript = 40, OTel api_request = 20, other = 10), the loser is kept as corroboration in
`sources.stats_json`, and any usage mismatch between them becomes a data-quality note. Re-ingesting the
same file is a no-op (source sha256 recorded). Blocks are content-addressed (`h`), request fingerprints
are delta-encoded (parent + kept count + appended hashes), which keeps storage linear in session length
(`ent-scale-storage`: ~100× smaller than full-payload traces; `cb-scale-memory`).

**Retention and erasure.** `tokenbill purge --principal P` and `--before DATE`; `--retention-days N` on
ingest; every purge writes an `audit` row (`ent-privacy-by-default`).

### 2.9 Privacy model (`tokenbill/privacy.py` + foundation primitives)

- **Content tiers** (`ContentTier`): `fingerprint` is the default for local scans and recorder traces;
  `none` is the default for anything exported off a machine; `full` is local-only and refused by
  `export`. Redaction is defense in depth, not the privacy story: code cannot be reliably redacted
  (`ent-redaction-limits`), so the design never needs content (`ent-cache-diagnostics-privacy`).
- **Keys.** An install key (`~/.config/tokenbill/key`, 32 random bytes, 0600) is created on first use;
  fleets distribute an org key file (`--key-file`) so hashes and pseudonyms join across machines. Hashes
  are only compared within a `key_id`. Per EDPB guidance, pseudonymized data is still personal data, so
  retention and purge apply (`ent-privacy-by-default`).
- **Volatile-pattern classes are computed before hashing** (ISO date-times, UUIDs, Unix timestamps,
  monotonic counters; the v0.1 regex list is reused), producing `h_norm` so the volatile-system breaker
  works without content.
- **Secrets.** `tokenbill/secrets.py` (foundation) detects provider keys (`sk-ant-`, `sk-`, `AKIA`, `ghp_`,
  `xox[bp]-`), PEM blocks, JWTs and high-entropy tokens; the recorder in `full` tier redacts before write;
  in every tier the count of secrets observed by type (never values) is reported (`ent-secrets-in-traces`).
- **Aggregation.** Fleet views group by team, repo, agent type, lane kind, model; any group with fewer than
  k=5 distinct principals is suppressed, with complementary suppression so a suppressed value cannot be
  derived from totals (`aggregation-k5`). Per-principal views exist only as a **self-view**
  (`--self PRINCIPAL`); no command sorts or lists principals by spend (`leaderboard-goodhart`,
  `vendor-leaderboards`). A logged **break-glass** (`--break-glass REASON`) reveals pseudonyms only for
  runaway-spend findings and writes an audit row (`runaway-circuit-breaker`).
- **Governance pack.** `docs/PRIVACY.md` with data inventory, DPIA draft, works-council annex ("not for
  performance evaluation", access matrix, retention) and employee notice template
  (`labor-law-constraints`). No emotion or sentiment inference anywhere.
- **Output hardening.** `textsafe.sanitize()` strips C0/C1 control characters from every terminal
  string; HTML is escaped and ships a CSP meta tag (`default-src 'none'; style-src 'unsafe-inline';
  img-src data:`) (`ent-report-a11y-hardening`, `cb-security-privacy`).

### 2.10 Simulation engine (`tokenbill/sim/`)

Two replay levels share one cache-rules table and one output type.

**Cache rules** (`sim/cache_rules.py`, dataclass `CacheRules` in `contracts/sim.py`), seeded from the
documentation and sourced per field:

| Field | Anthropic (1P / Claude Platform on AWS / Foundry / Bedrock / Vertex) | OpenAI GPT-5.6+ |
|---|---|---|
| TTL options | 300 s, 3600 s | 1800 s |
| TTL measured from | request start; generation time counts (`anth-ttl-start-1h`) | last use |
| Refresh on read | yes, free | yes, free |
| Entry visible | after the first response token (`anth-concurrency-fanout`) | undocumented → `response_end` (conservative) |
| Lookback | 20 positions per breakpoint; runs of tool_use / tool_result count as one (`anth-lookback-20`) | first 2 + latest 50 explicit breakpoints (`oai-56-explicit-cache` corrected) |
| Max breakpoints | 4; longer TTLs before shorter | 4 explicit (implicit mode: 1 automatic + up to 3) |
| Min prefix | per model from the rate row (512–4096; Bedrock Opus 4.7 = 4096) | 1,024 visible tokens |
| Scope | workspace (1P, Claude Platform on AWS, Foundry); organization (Bedrock, Vertex) | organization, not across regional boundaries |
| Invalidation hierarchy | tools change → all tiers; speed / web-search / citations toggles / system change → system+messages; tool_choice, images → messages; thinking/effort → messages (some models all) (`anth-invalidation-hierarchy`) | model, prompt_cache_key, tools, text_format, reasoning_effort, verbosity, context_compacted, input, service_tier |
| Other | server tools auto-insert 5m breakpoints (not breakers); Haiku ≤4.5 and older models strip thinking blocks on a plain user turn | writes 1.25×, reads 0.1× |

**Usage-level replay** (`sim/usage_replay.py`, works for every source including content-free OTel and
transcripts). Per lane, ordered requests `i` with billed buckets `R_i` (read), `W_i` (writes by TTL),
`U_i` (uncached), `O_i` (output), `T_i = R_i + W_i + U_i`. Define the lane's cached prefix after `i` as
`P_i = R_i + W_i` (Claude Code and most harnesses put the last breakpoint at the end of messages; lanes
where this is false are flagged by the block-level engine when fingerprints exist).

- *Expected reuse* at transition `i-1 → i`: `E_i = min(P_{i-1}, T_i)` when the transition is a
  continuation (same model, no compaction/clear event between, `T_i ≥ T_{i-1} − 2,000`); else `E_i = 0`.
- *Observed miss*: `M_i = E_i − R_i`; a **miss event** when `M_i > 0.05·E_i` and `M_i ≥ 2,000` (Claude
  Code's own definition, `cache-health-benchmarks-diagnostics`).
- *Documented prediction* (without looking at `R_i`): hit if the entry written/refreshed at the start of
  `i-1` is alive at the start of `i` (gap `≤ τ`, `τ` = TTL class of the lane's most recent billed write:
  1h if it was an `ephemeral_1h` write, else 5m; for sources without a split, the lane's `write_ttl_hint`,
  and the transition is excluded from ρ fitting when the hint is absent), same model, same cache scope, no
  invalidating parameter change, and `E_i ≥` min prefix. Gaps within ±10 s of `τ` are marked ambiguous. Predicted `R̂_i = E_i` on hit, else `R̂_i = S` where `S` is the scope's static-prefix floor
  (median read on first calls of lanes in the same scope and model).
- *Cause classification* (priority order): model switch (sub-kinds from iterations/events: refusal
  fallback, availability fallback, user switch) → compaction/clear event → context shrank → TTL expiry
  (gap > τ) → server diagnostic label (`*_changed`) → unexplained prefix change.
- *Minimal-change counterfactual*: a policy only changes the transitions it affects; unaffected requests
  keep their billed usage exactly. Affected transitions get rule-predicted usage; the **documented**
  saving is the priced difference; the **calibrated** saving multiplies each affected transition's
  predicted change by the fitted hit probability `ρ(band)` from §3 F7. Reported savings are always
  "calibrated counterfactual − observed", so model error on unaffected traffic cannot leak into them.

Policies (`contracts/sim.py: Policy`) implemented at usage level: `ttl` per lane kind (5m / 1h),
`keepalive(interval_s, max_idle_s)`, `compaction_window(tokens, post_tokens)`, `model_remap` per lane
kind or agent type (price-only), `batch` (eligible set), `repair` (named detector repairs: avoid model
switch, fallback credit, stagger fan-out, restore caching on gateway-stripped lanes). Formulas are in §3 F8.

**Block-level replay** (`sim/block_replay.py`, requires `ContentFingerprint`): maintains cache entries
keyed by `(cache_scope_key, model, chained_hash_at_block_k)`, each with `write_ts`, `ttl`, `visible_from`
(`ts_start + ttft`, or response end if unknown), `tokens`. The chained hash mixes a **tier salt** at each
tier boundary (tools tier salted with model and tool-affecting params; system tier with speed / web-search
/ citations; messages tier with tool_choice, thinking, effort, output format, image set), which turns the
invalidation hierarchy into plain hash inequality. For each request and each breakpoint (observed or
policy-chosen) it looks back ≤20 collapsed positions for the longest live, visible entry, reads it,
writes the remainder if it meets the min prefix, and refreshes TTLs. Output per request: predicted
read/write tokens (block `est_tokens` rescaled so each request's total matches billed `total_input`) and
the first divergence (tier, block index, cause). Canonical placement policies evaluated: observed;
end-of-messages every call (v0.1 optimum); static-prefix + end; static + intermediate every 15 positions +
end (lookback-safe); cheapest wins (`cb-shared-prefix-sim`, `cb-lookback`).

### 2.11 Shared contracts (foundation, `tokenbill/contracts/`)

```python
# contracts/pricing.py
@dataclass(frozen=True, slots=True)
class PricedLine:
    bucket: str                   # "uncached_input" | "cache_read" | ... | "web_search"
    quantity: int                 # tokens or requests
    unit_usd: Decimal             # per MTok for tokens, per request for server tools (after modifiers)
    amount: Decimal               # exact
    rate_row_id: str
    modifier_ids: tuple[str, ...]
    layer: str                    # "builtin" | "user" | "contract"

@dataclass(frozen=True, slots=True)
class PricedInference:
    inference_id: str | None
    lines: tuple[PricedLine, ...]
    total: Decimal | None         # None = unpriced
    evidence: Evidence
    basis: Basis
    range: MoneyRange | None
    unpriced_reason: str | None

@dataclass(frozen=True, slots=True)
class ResolvedRates:
    row_id: str; channel: str; model: str
    input: Decimal; output: Decimal; cache_read: Decimal
    cache_write_5m: Decimal | None; cache_write_1h: Decimal | None; cache_write_30m: Decimal | None
    per_request: tuple[tuple[str, Decimal], ...]
    modifier_ids: tuple[str, ...]; min_cacheable_tokens: int | None; tokenizer_family: str

class Pricer(Protocol):
    rate_card_id: str
    rate_card_sha256: str
    basis: Basis
    def resolve(self, ctx: PricingContext, *, ts_ms: int) -> ResolvedRates | None: ...
    def price_inference(self, inf: Inference, *, ts_ms: int) -> PricedInference: ...
    def price_usage(self, usage: UsageBuckets, ctx: PricingContext, *, ts_ms: int) -> PricedInference: ...

@dataclass(frozen=True, slots=True)
class PricedTotal:
    figure: Figure                # EXACT unless any unknown-TTL / uncertain-billing lines
    priced_inferences: int
    unpriced_inferences: int
    unpriced_tokens: int
    coverage: Decimal             # priced tokens / all billable tokens (RATIO_CTX)

# contracts/ingest.py
@dataclass(frozen=True, slots=True)
class IngestOptions:
    content_tier: ContentTier
    hmac_key: bytes
    attribution: Attribution = Attribution()   # collector-supplied defaults (team, cost_center, ...)
    since_ms: int | None = None
    until_ms: int | None = None
    lenient: bool = True          # quarantine bad records instead of failing the file
    now_ms: int = 0               # injected clock for determinism

@dataclass(frozen=True, slots=True)
class SourceInfo:
    source_id: str; adapter: str; name_hmac: str; sha256: str; bytes: int

@dataclass(frozen=True, slots=True)
class QuarantineItem:
    source_id: str; locator: str; reason: str   # never the raw line

@dataclass(frozen=True, slots=True)
class DataQualityNote:
    code: str                     # "dq.message_start_only", "dq.naive_line_sum_ratio", "dq.iterations_mismatch", ...
    severity: str                 # "info" | "warn" | "error"
    count: int
    detail: str                   # content-free
    figure: Figure | None = None  # e.g. estimated under-logged output dollars

@dataclass
class IngestResult:
    source: SourceInfo
    requests: list[Request]
    sessions: list[Session]       # lanes may be partial; sim/lanes.py assembles final Lane objects
    events: list[LaneEvent]
    aggregates: list[UsageAggregate]
    cost_lines: list[CostLine]
    quarantined: list[QuarantineItem]
    notes: list[DataQualityNote]
    stats: dict[str, int]

class Adapter(Protocol):
    name: str                     # registry key, e.g. "claude-code"
    capabilities: frozenset[str]  # "usage_sequence","timing","ttft","blocks","attempts","diagnostics","ttl_split","aggregates","cost"
    def sniff(self, path: Path, head: bytes) -> bool: ...
    def read(self, path: Path, opts: IngestOptions) -> IngestResult: ...
# adapters/base.py: register_adapter(adapter), get_adapter(name), sniff_adapter(path) -> Adapter | None

# contracts/store.py
class LedgerStore(Protocol):
    def ingest(self, result: IngestResult) -> dict[str, int]: ...          # idempotent upserts
    def iter_requests(self, *, since_ms: int | None = None, until_ms: int | None = None,
                      where: Mapping[str, str] | None = None) -> Iterator[Request]: ...
    def iter_sessions(self, **filters) -> Iterator[Session]: ...
    def events(self, lane_key: str) -> list[LaneEvent]: ...
    def aggregates(self, source_kind: str | None = None, **window) -> list[UsageAggregate]: ...
    def cost_lines(self, source_kind: str | None = None, **window) -> list[CostLine]: ...
    def purge(self, *, principal: str | None = None, before_ms: int | None = None) -> int: ...
    def audit(self, actor: str, action: str, detail: Mapping[str, object]) -> None: ...
# tokenbill/testing.py ships MemoryStore (reference implementation), FakePricer (tiny sourced-looking
# table: claude-opus-5-5, claude-sonnet-5, claude-haiku-4-5, gpt-5.6-sol), builders make_usage(),
# make_inference(), make_request(), make_lane(), and FakeReplayer.

# contracts/sim.py
@dataclass(frozen=True, slots=True)
class CacheRules: ...             # fields of the table in §2.10
class CacheRulesProvider(Protocol):
    def rules_for(self, provider: str, channel: str, model: str) -> CacheRules: ...

@dataclass(frozen=True, slots=True)
class Policy:
    name: str
    ttl_by_lane_kind: tuple[tuple[str, str], ...] = ()      # (("main","1h"),)
    keepalive: tuple[int, int] | None = None                # (interval_s, max_idle_s)
    compaction_window: tuple[int, int] | None = None        # (window_tokens, post_tokens)
    model_remap: tuple[tuple[str, str], ...] = ()           # (("subagent","claude-sonnet-5"),) or (("agent_type:Explore","claude-haiku-4-5"),)
    batch_eligible: str | None = None                       # predicate name
    repairs: tuple[str, ...] = ()                           # detector repair ids
    breakpoint_policy: str | None = None                    # block-level only
    @staticmethod
    def observed() -> Policy: ...
    def combine(self, other: Policy) -> Policy: ...         # for joint replays (Shapley)

@dataclass(frozen=True, slots=True)
class ReplayRequestOutcome:
    request_id: str
    usage: UsageBuckets
    extra: tuple[Inference, ...]  # keepalive pings, inserted compaction calls
    cost: Decimal | None
    changed: bool                 # False => identical to billed

@dataclass(frozen=True, slots=True)
class ReplayResult:
    policy: Policy
    mode: str                     # "documented" | "calibrated"
    cost: Figure                  # ESTIMATED (as-billed replay of Policy.observed() is EXACT)
    per_lane: tuple[tuple[str, Decimal], ...]
    outcomes: tuple[ReplayRequestOutcome, ...] | None
    assumptions: tuple[str, ...]
    calibration: Calibration

class Replayer(Protocol):
    def replay(self, lanes: Sequence[Lane], policy: Policy, *, mode: str, pricer: Pricer,
               rules: CacheRulesProvider, calibration: CalibrationReport | None,
               keep_outcomes: bool = False) -> ReplayResult: ...

@dataclass(frozen=True, slots=True)
class CalibrationReport:
    granularity: str              # "hour" | "day" | "month"
    n_periods: int
    nmbe_pct: Decimal
    cvrmse_pct: Decimal
    thresholds: tuple[Decimal, Decimal]
    passes: bool
    read_agreement: tuple[Decimal, Decimal, Decimal]   # p10, p50, p90 of predicted/billed reads
    rho: tuple[tuple[str, int, int], ...]              # (band, hits, trials)
    density: tuple[tuple[str, Decimal, int, Decimal], ...]  # (family:kind, bytes/token, n, MAPE)
    diag_confusion: tuple[tuple[str, str, int], ...]   # (predicted cause, server reason, count)
    diag_precision_recall: tuple[tuple[str, Decimal, Decimal], ...]
    unlabeled: int
    notes: tuple[str, ...]

# contracts/findings.py
@dataclass(frozen=True, slots=True)
class EvidenceItem:
    kind: str                     # "transition" | "event" | "block_divergence" | "attempt_chain" | "aggregate"
    ref: str                      # request/lane/event id (pseudonymous)
    attrs: tuple[tuple[str, str | int], ...]   # gap_ms, tokens, diag reason, tier, block index...

@dataclass(frozen=True, slots=True)
class Fix:
    text: str
    config_patch: Mapping[str, object] | None   # e.g. {"env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "400000"}}
    target: str | None            # "claude-code-managed-settings" | "litellm" | "code" | "gateway"
    doc_url: str | None
    gates: tuple[str, ...] = ()   # applicability, e.g. "model in {opus-5,opus-5-5,fable-5-1}", "claude-code>=2.1.267"

@dataclass(frozen=True, slots=True)
class Scope:
    dims: tuple[tuple[str, str], ...]   # team/repo/lane_kind/model/agent_type/workspace; principal only in self-view

@dataclass(frozen=True, slots=True)
class Finding:
    finding_id: str               # stable_id(detector_id, scope, first evidence ref)
    detector_id: str              # e.g. "cc.ttl-expiry"
    detector_version: str
    kind: str                     # "breaker" | "lever" | "premium" | "data-quality" | "failure"
    lever_class: str              # "free-win" | "trade-off" | "rate" | "hygiene"
    title: str
    summary: str
    scope: Scope
    events: int
    first_seen_ms: int
    cost_observed: Figure         # what the pattern cost in the window
    recoverable: Figure | None    # counterfactual saving (None = no mechanical repair)
    recoverable_shapley: Figure | None = None   # filled by levers/plan.py
    projected_monthly: Figure | None = None     # realization-adjusted range
    evidence: tuple[EvidenceItem, ...] = ()
    fix: Fix | None = None
    confidence: str = "medium"    # "high" | "medium" | "low"
    validated_against: str | None = None   # e.g. "cache_miss_reason: 128/128 agree"
    needs_eval: bool = False
    references: tuple[str, ...] = ()       # research finding ids

@dataclass(frozen=True)
class AnalysisContext:
    pricer: Pricer
    rules: CacheRulesProvider
    replayer: Replayer | None
    calibration: CalibrationReport | None
    window: tuple[int, int]
    k_anonymity: int = 5
    self_principal: str | None = None
    now_ms: int = 0

# detect/base.py
class Detector(Protocol):
    id: str
    version: str
    requires: frozenset[str]      # adapter capabilities needed
    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]: ...
# register_detector(d), all_detectors(), applicable(capabilities) -> list[Detector]

# contracts/recon.py
@dataclass(frozen=True, slots=True)
class ReconRow:
    key: tuple[tuple[str, str], ...]   # date, workspace, model, token_type, service_tier
    ledger_tokens: int | None
    provider_tokens: int | None
    ledger_usd: Decimal | None         # our rate card applied to OUR ledger
    priced_provider_usd: Decimal | None  # our rate card applied to PROVIDER usage
    invoice_usd: Decimal | None        # provider cost report
    rate_card_error_pct: Decimal | None  # (priced_provider - invoice) / invoice
    coverage_pct: Decimal | None       # ledger_usd / invoice_usd
    status: str                        # "match" | "within_tolerance" | "over" | "under" | "explained" | "unexplained" | "provisional"
    explanations: tuple[str, ...]      # "priority_excluded_from_cost_report", "revision_window", ...

@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    window: tuple[str, str]
    tolerance_pct: Decimal
    rows: tuple[ReconRow, ...]
    token_coverage_pct: Decimal | None
    dollar_coverage_pct: Decimal | None
    rate_card_error: tuple[Decimal, Decimal, Decimal] | None   # p50, p95, max |%| over model-days
    over_count_rows: int               # ledger tokens > provider tokens: import bug or double count
    effective_discount: tuple[tuple[str, Decimal], ...]        # model:token_type -> 1 - invoice/list
    residuals: tuple[tuple[str, Decimal], ...]                 # reason -> USD
    verdict: str                       # "reconciled" | "not_reconciled" | "insufficient_data"
    finality: Finality

# contracts/verify.py
@dataclass(frozen=True, slots=True)
class GuardResult:
    name: str; passed: bool; value: str; threshold: str

@dataclass(frozen=True, slots=True)
class MeasurementResult:
    lever_id: str
    design: str                   # "ab" | "did" | "stepped_wedge" | "its"
    unit: str                     # "cost per active developer-day" | "cost per task" | "cost per success"
    estimate: Figure              # MEASURED or VERIFIED, with CI range
    projected: Figure | None      # the ex-ante estimate being checked
    realization_rate: tuple[Decimal, Decimal, Decimal] | None   # point, lo, hi
    guards: tuple[GuardResult, ...]
    scope: Mapping[str, int]      # clusters, units, treated unit-days
    window: Mapping[str, str]
    rate_card_sha256: str
    assignment_log_sha256: str | None
    adjustments: tuple[str, ...]  # non-routine adjustments (model launches, re-orgs)
    rate_variance: Figure | None  # exact price effect, reported separately (R8)
```

### 2.12 CLI surface

Exit codes: `0` success; `1` runtime failure; `2` usage error; `3` a gate failed (reconciliation
tolerance, calibration gate when required, `check` failure, receipt verification failure). Global flags on
every command: `--format {text,json}` (where output exists), `--deterministic` (no wall-clock fields;
fixed ordering), `--quiet`, `-v`. Every command is offline unless `--live` is passed.

| Command | Purpose | Flags |
|---|---|---|
| `tokenbill --version` | (existing) | |
| `tokenbill demo` | (existing, byte-identical default output) | `-o report.html`, `--seed N`, `--scenario NAME`, **new**: `--fleet` (synthetic fleet end to end: ingest → bill → reconcile → calibrate → findings → policy → receipt), `--format` |
| `tokenbill analyze TRACE…` | (existing) trace@1 / trace@2 | `-o`, `--model-price MODEL=IN,OUT`, **new**: `--engine {v1,v2}` (default v1 for trace@1, v2 for trace@2), `--rates FILE`, `--contract FILE`, `--format` |
| `tokenbill scan` | one-shot local Claude Code review (the 5-minute path) | `--claude-dir PATH` (default `~/.claude/projects`), `--since DATE`, `--until DATE`, `--db PATH` (default temp, deleted), `--rates FILE`, `--contract FILE`, `-o report.html`, `--format`, `--content {none,fingerprint}` |
| `tokenbill ingest SOURCE…` | normalize sources into a ledger | `--db PATH` (required), `--adapter NAME\|auto`, `--content {none,fingerprint,full}`, `--key-file PATH`, `--attr KEY=VALUE` (repeatable; also reads `OTEL_RESOURCE_ATTRIBUTES`), `--principal NAME`, `--since/--until`, `--strict` (fail on first bad record), `--retention-days N` |
| `tokenbill bill` | exact priced ledger totals | `--db`, `--rates`, `--contract`, `--basis {list,contract}`, `--group-by DIMS` (`day,team,model,lane_kind,bucket,…`; `principal` only with `--self`), `--since/--until`, `--format {text,json,csv}` |
| `tokenbill reconcile` | reconcile ledger and rate card to provider reports | `--db`, `--usage-report FILE…`, `--cost-report FILE…`, `--cc-analytics FILE…`, `--enterprise-analytics FILE…`, `--openai-usage FILE…`, `--openai-costs FILE…`, `--live --admin-key-env VAR` (opt-in pull), `--tolerance-pct 0.5`, `--closed-only`, `--suggest-contract OUT.json`, `--format` |
| `tokenbill calibrate` | replay validation and ρ/density fits | `--db`, `--granularity {hour,day,month}`, `--require-pass` (exit 3 if gate fails), `--format` |
| `tokenbill findings` | detectors and levers ranked by recoverable dollars | `--db`, `--detector ID…`, `--min-usd X`, `--group-by team\|repo\|agent_type\|model`, `--self PRINCIPAL`, `--break-glass REASON`, `--no-shapley`, `--format` |
| `tokenbill whatif` | counterfactual replays and joint Shapley | `--db`, `--policy SPEC` (repeatable), `--mode {documented,calibrated,both}`, `--shapley`, `--format`. SPEC grammar: `ttl=1h[:main\|:subagent\|:workflow_agent]`, `keepalive=240s[:max=60m]`, `compact-window=400000[:post=20283]`, `model:<lane_kind\|agent_type:X>=<model>`, `batch=eligible`, `repair=<id>`, `breakpoints=<policy>` |
| `tokenbill policy` | managed-settings / gateway patch from the action plan | `--db`, `--target {claude-code,litellm}`, `--include-tradeoffs`, `--contract FILE` (emits `modelPricing`), `-o patch.json` |
| `tokenbill measure plan` | randomized rollout plan | `--clusters FILE` (workspace/team list), `--waves N`, `--holdback PCT`, `--seed N`, `--lever ID`, `-o plan.json` (includes assignment log sha256 and pre-registration) |
| `tokenbill measure run` | estimate realized savings | `--db`, `--lever ID`, `--plan plan.json` or `--treated FILE --adoption DATE`, `--design {ab,did,stepped-wedge}`, `--outcomes FILE` (optional quality guardrail data), `--baseline-rates FILE`, `--seed N`, `-o measurement.json` |
| `tokenbill receipt create/sign/verify` | savings receipts | `create --measurement FILE -o receipt.json`; `sign --key PATH [--namespace tokenbill-receipt] receipt.json` → `receipt.dsse.json`; `verify --allowed-signers FILE --identity ID receipt.dsse.json` |
| `tokenbill export` | interchange | `--db`, `--format {focus,trace2,jsonl,ccusage}`, `--grain {request,hour,day}`, `--content {none,fingerprint}` (never full), `-o FILE` (`.gz` supported) |
| `tokenbill pricing` | registry | `show [MODEL] [--at DATE]`, `verify [--snapshot FILE] [--live] [--feeds]`, `diff A.json B.json` |
| `tokenbill check` | CI gate | `TRACE…` or `--db`, `--min-cache-read-share 0.80`, `--after-turn 2`, `--baseline FILE`, `--max-cost-regression-pct 10`, `--fail-on {breaker,regression,any}`, `--format {text,json,sarif}` |
| `tokenbill report` | HTML fleet/team/self report | `--db`, `-o report.html`, `--team T`, `--self PRINCIPAL`, `--reconciliation FILE`, `--calibration FILE` |
| `tokenbill purge` | erasure / retention | `--db`, `--principal P` or `--before DATE`, `--yes` |

### 2.13 Outputs

**JSON (`tokenbill/result@2`)**, stable, versioned, validated by a schema test. Money is always
`{"usd": "<exact decimal string>", "usd_micro": <int>, "evidence": …, "basis": …, "finality": …,
"range": {"low_micro": …, "high_micro": …} | null, "calibration": …, "upper_bound": bool,
"provenance": […], "note": "…"}`; never a float.

```json
{
  "schema": "tokenbill/result@2",
  "tool": {"name": "tokenbill", "version": "0.2.0"},
  "command": "scan",
  "window": {"since": "2026-08-24", "until": "2026-09-23"},
  "inputs": [{"source_id": "s_…", "adapter": "claude-code", "records": 43383, "quarantined": 0}],
  "privacy": {"content_tier": "fingerprint", "key_id": "k_…", "k_anonymity": 5, "suppressed_groups": 0},
  "rate_card": {"sha256": "…", "layers": ["builtin@2026-09-23"], "stale_rows": []},
  "bill": {"total": {"usd": "11632.75", "evidence": "exact", "basis": "list", "…": "…"},
           "coverage": {"priced_inferences": 43383, "unpriced_inferences": 0, "coverage": "1"},
           "by_bucket": [], "by_lane_kind": [], "by_model": [], "by_team": []},
  "data_quality": [{"code": "dq.naive_line_sum_ratio", "severity": "info", "count": 103607, "detail": "summing lines would report 2.33x"}],
  "reconciliation": null,
  "calibration": {"granularity": "day", "nmbe_pct": "…", "cvrmse_pct": "…", "passes": true, "diag_confusion": []},
  "findings": [{"detector_id": "cc.ttl-expiry", "cost_observed": {}, "recoverable": {}, "recoverable_shapley": {}, "projected_monthly": {}, "fix": {}, "needs_eval": false, "references": ["cc-cold-resume"]}],
  "action_plan": {"joint_saving": {}, "levers": []},
  "policy_pack": null
}
```

**Terminal** (`outputs/terminal.py`): sanitized, ≤100 columns, labels inline. Example shape:

```
Token Bill scan · 29 sessions · 1,481 lanes · 43,383 billed calls · 2026-05-12 → 2026-09-23
BILL (exact, list)          $11,632.75   reads 55.1% · 1h writes 17.6% · output 14.4% · 5m writes 12.6%
  coverage 100% priced · rate card 3f2a…(verified 2026-09-23) · reconciliation: not run
DATA QUALITY  naive line-sum would be 2.33× ($27,094) · 4,497 calls with under-logged output: est. +$112–$246
CALIBRATION  daily NMBE −1.8% · CV(RMSE) 9.4% → PASS (±10%/30%) · server labels agree 128/128 on TTL expiry
TOP RECOVERABLE (estimated, calibrated, Shapley; monthly range after realization)
 1 compaction window 400k (main)   trade-off · needs eval   $X–$Y /mo   upper bound
 2 TTL policy main=1h (API-key)    free win                 $X–$Y /mo
 …
Labels: exact = billed usage × sourced rates · estimated = replay/assumption · measured/verified = causal, with CI
```

**HTML** (`outputs/html.py`, single self-contained file, inline CSS/SVG, CSP meta, WCAG 2.2 AA: every
chart has a `<table>` twin with `<caption>`, no color-only encoding): header with evidence legend; bill
(exact) with coverage and reconciliation badge; where the money goes (bucket, lane kind, model, team with
k-suppression; context-size tax: dollars of reads beyond 100k/200k/400k); findings ranked by Shapley
credit with label badges, evidence, fix and config patch; calibration panel (NMBE/CV(RMSE), confusion
matrix vs `cache_miss_reason`, ρ table); reconciliation panel; policy-pack preview; methodology and
provenance (rate-card hash, sources, verification dates, assumptions, threats to validity). The v0.1
report (`report.py`) is untouched and still used by `demo`/`analyze --engine v1`.

**FOCUS 1.4 CSV** (`outputs/focus.py`, `ent-focus-1-4`, `finops-platforms-export-target`): one row per
(charge period, provider, model, token bucket, team/cost center, lane kind). Columns: `BillingPeriodStart`,
`BillingPeriodEnd`, `ChargePeriodStart`, `ChargePeriodEnd`, `BilledCost`, `EffectiveCost`, `ListCost`,
`ContractedCost`, `BillingCurrency`=`USD`, `ServiceCategory`=`AI and Machine Learning`, `ServiceName`,
`ProviderName`, `PublisherName`, `InvoiceIssuerName`, `ChargeCategory`=`Usage`, `ChargeDescription`,
`ConsumedQuantity` (tokens), `ConsumedUnit`=`Tokens`, `PricingQuantity`, `PricingUnit`=`1M Tokens`,
`SkuId` (rate row id), `Tags` (JSON: team, repo, lane_kind, agent_type), plus `x_TokenBucket`, `x_Model`,
`x_Evidence`, `x_Basis`, `x_ReconciliationStatus`, `x_RecoverableCost`, `x_FindingIds`,
`x_CacheBreakerType`. Ledger-derived rows set `BilledCost = ContractedCost` (or `ListCost` without a
contract) and say so in `x_Evidence`/`x_Basis`; invoice rows imported from cost reports are exported
separately with `x_Source=provider_cost_report`. Column names must be re-verified against the 1.4 spec;
FOCUS 1.5 (model identity in `SkuPriceDetails`, target 2026-12-03, `focus-15-ai-columns`) is tracked,
not built.

**SARIF 2.1.0** for `check` (`ent-ci-cost-gate`); **ccusage-compatible JSON** daily/session reports
(`ccusage-local-parity`); **trace@2** content-free export for fleet collectors.

### 2.14 Extension points

- **Adapters**: implement `Adapter`, call `register_adapter()` at import; list the module in
  `adapters/all.py`. Third-party plugins via `importlib.metadata.entry_points(group="tokenbill.adapters")`
  load **only** with `--plugins` (supply-chain opt-in).
- **Detectors**: implement `Detector` with a `requires` capability set; register in `detect/base.py`'s
  registry; must emit `Finding` with evidence, fix, references and a validation note.
- **Rate sources**: extra JSON rate files (`--rates`), contract overlays (`--contract`), `modelPricing`
  import; `rates/verify.py` feed checkers are pluggable functions `(registry) -> list[Discrepancy]`.
- **Cache rules and billing rules**: data rows with sources; new providers add rows, not code.
- **Policies**: `Policy` fields; SPEC grammar parser in `commands/whatif.py`.
- **Exporters**: `outputs/*` modules take `result@2` dataclasses only.

### 2.15 Backward compatibility

- `tokenbill/trace@1` reading, writing and rendering are untouched (`trace.py` frozen); the trace@1
  adapter wraps them into ledger records (writes pinned to 5m per the format contract, R5).
- `tokenbill demo` and `tokenbill analyze` (v1 engine) produce byte-identical output to 0.1.2 except the
  version string, verified by golden-file tests; the flagship `test_demo_recovers_planted_waste.py` is
  unchanged and green.
- `tokenbill.pricing` keeps `ModelPricing`, `PRICING`, `pricing_for`, `cost_breakdown`, `price_usd`,
  `CACHE_TTL_SECONDS`, `TTL_REFRESH_ON_READ`, `MAX_BREAKPOINTS`, `RENDER_ORDER`; `PRICING` is now built
  from the registry pinned to its `as_of` date (never wall clock), gains rows (Opus 5.5, Mythos, Opus 4.5,
  Sonnet 4.5, …) and a defaulted `cache_write_1h_multiplier` field. Existing `tests/test_pricing.py` stays
  green.
- `Recorder(path).wrap(client)` keeps working; its default output becomes trace@2 `fingerprint`
  (content-free); `Recorder(path, format="trace@1")` restores v0.1 output. `analyze` accepts both.

### 2.16 Performance budgets (CI-enforced on synthetic corpora)

| Operation | Budget |
|---|---|
| Claude Code import | ≥25k assistant lines/s; 300k calls < 2 min; peak RSS < 500 MB (streaming per file) |
| `bill` | 10⁶ inferences < 60 s; RSS < 1 GB (streamed by day) |
| usage-level replay | O(n) per lane per policy; 10⁶ requests × 1 policy < 30 s |
| Shapley | exact for ≤6 interacting levers per lane-kind group (64 joint replays); otherwise seeded permutation sampling (200 permutations) with reported SE |
| block-level replay | O(new blocks + 4 breakpoints × 20 positions) per request; 4k-call run < 2 s (v0.1: 6.3 s, quadratic, `cb-scale-memory`) |
| HTML | ≤ 5 MB for 10⁴ lanes (aggregated sections, paginated drill-down) (`cb-report-scale`) |

---

## 3. v0.2 features in priority order

Priority follows the lens: first make every billed number exact and reconcilable, then make every
counterfactual calibrated, then rank and verify savings. Dollar impacts quoted from the local corpus are
**mechanism sizes on one heavy user**, not fleet predictions; fleet sizing arithmetic uses Anthropic's
published $13/developer/active-day baseline (2,000 developers ≈ $3.6–6.0M/year, `cc-enterprise-baseline`).

### F1. Exact money, evidence labels and honesty enforcement (WP-0)

**What.** `money.py`, `labels.py`, `jsonable.py` (dataclass/Decimal/Enum → JSON, Decimal as string, float
rejected), rules R1–R8 (§2.6).

**Evidence.** Enterprise Analytics amounts are cents decimal strings and the docs warn against binary
floats (`ent-reconciliation`); receipts need integer micro-USD (`signed-receipts`); FinOps separates exact
rate savings from causal usage savings (`finops-rate-vs-usage`); `Inexact`-trapping arithmetic makes silent
rounding impossible.

**$ impact.** Enabler; prevents presentation errors that disqualify the tool (`anth-usage-schema-exactness`).

**Acceptance tests.**
- `token_cost(1_000_000, usd("4.00"), Decimal("0.05")) == Decimal("0.2000000")`; `token_cost(1, usd("0.25"))
  == Decimal("2.5E-7")`; `from_cents("12345.678") == Decimal("123.45678")`.
- `usd(0.1)` raises `TypeError`; `usd("NaN")` raises `ValueError`; an intentionally inexact operation inside
  `EXACT_CTX` raises `decimal.Inexact`.
- `to_micros(Decimal("0.0000005")) == 0`, `to_micros(Decimal("0.0000015")) == 2` (half-even).
- `Figure(usd=None, …)` without note raises; `Figure(evidence=EXACT, range=…)` raises; MEASURED without CI
  raises; `add(exact, estimated).evidence == ESTIMATED`; adding LIST to CONTRACT raises.
- `jsonable` never emits a JSON number with a fraction part for money (property test with hypothesis).

### F2. Canonical ledger, convention registry and invariants (WP-0 records + Anthropic convention; WP-A3 other conventions)

**What.** The records of §2.4 and the normalization contract of §2.5; every adapter produces disjoint
buckets and passes sum-checks; malformed records are quarantined with a content-free reason instead of
aborting the file (`cb-strict-parse`); sessions are source-qualified so reused `run_id`s never merge
(`cb-dup-runid`); lanes separate interleaved subagents/helpers (`cb-lanes`: v0.1 under-reported
redundancy 17× on interleaved lanes).

**Evidence.** Cache tokens are counted four different ways across providers and telemetry
(`ent-usage-normalization`, `otel-genai-normalization`, `oai-usage-inclusive`, `gemini-usage-semantics`,
`bedrock-cache-semantics`); 14 dated framework bugs miscount cache tokens (`fw-telemetry-conventions`);
trace@1 lacks the request/response/attribution envelope (`cb-schema-envelope`); OTel has `error.type` but
no attempt attribute (`fp-trace-fields-standards`).

**$ impact.** Prevents baselines wrong by 0.03×–2× (`fw-telemetry-conventions`).

**Acceptance tests.** For each convention id in §2.5 a golden fixture reproduces the provider total
(e.g. OpenAI Responses `input_tokens=10000, cached_tokens=6000, cache_write_tokens=2000` → uncached 2000,
read 6000, write_30m 2000); OpenInference fixture with an AGENT span wrapping two LLM spans counts only the
LLM spans; the CrewAI-style defect fixture (reported 17 for a 17,119-token call) is flagged
`dq.sum_check_failed`; `ledger_totals_equal` holds on every fixture; two files with the same `run_id`
produce two sessions and the org total equals the sum of calls ($22, not $40).

### F3. Versioned, sourced, effective-dated pricing registry with contract overlays (WP-P)

**What.** §2.7: `rates/` registry, resolver, exact engine, model-id normalization, modifiers (batch 0.5×,
US `inference_geo` 1.1×, fast-mode base replacement, Bedrock/Vertex regional +10%, Priority burn-down,
OpenAI flex/fast/long-context/regional), server-tool charges, contract overlays and `modelPricing`
import/export, failure billing rules, staleness, `pricing verify`, lifecycle calendar, and the `pricing.py`
compat shim.

**Evidence.** `anth-pricing-table-2026-09`, `anth-cache-1h-2x`, `anth-modifiers-geo-fast-priority`,
`anth-batch-stacking`, `anth-usage-schema-exactness`, `cb-pricing-coverage`, `cb-1h-ttl`, `cb-modifiers`,
`pricing-engine-gaps`, `tokenbill-pricing-gaps`, `oai-service-tiers`, `oai-batch-longctx-residency`,
`gemini-effective-dated-prices`, `bedrock-price-list-api`, `vertex-claude-geo-labels`,
`claude-marketplace-ccu`, `price-feed-crosscheck`, `anth-evidence-drift`, `cc-list-price-estimates`,
`cc-anthropic-discount-visibility`, `fp-billing-matrix`, `cc-timing-calendar`.

**$ impact.** Fixes the −7.3% misstatement on the real corpus (1h writes −6.5%, Opus 5.5 −0.6%, fallback
iterations −0.2%; `cc-pricing-1h-opus55-gap`); 1h write lines are understated 37.5% without it
(`anth-cache-1h-2x`); tier-blind pricing misstates by 50% (`oai-service-tiers`); US-geo and regional
premiums are 10%. Contract overlays are the difference between list-price estimates and invoice-grade
numbers.

**Rules.** §2.7. Registry load fails on: overlapping effective intervals; derived cache price ≠
`published_absolute`; a row without a source; a modifier referencing unknown dimensions.

**Acceptance tests: the golden billing corpus** (`tests/rates/test_golden_billing.py`), each case
hand-computed in comments:

| # | Case | Expected USD |
|---|---|---|
| 1 | Opus 5.5 standard: uncached 1,000; read 100,000; 5m write 2,000; 1h write 3,000; output 500 | 0.004 + 0.020 + 0.010 + 0.024 + 0.010 = **0.068** |
| 2 | case 1 with `inference_geo="us"` | **0.0748** |
| 3 | case 1 in batch | **0.034**; batch + US geo **0.0374** |
| 4 | case 1 with `speed="fast"` (base $8/$40; read 8×0.05; 5m 8×1.25; 1h 8×2) | 0.008 + 0.040 + 0.020 + 0.048 + 0.020 = **0.136** |
| 5 | Fable 5.1 read 1,000,000 vs Fable 5 read 1,000,000 | **0.25** vs **1.00** |
| 6 | Opus 5.5 with 3 web searches | + **0.03** |
| 7 | Opus 5 via Bedrock, input 1,000,000, global vs in-region | **5.00** vs **5.50** |
| 8 | case 1 with contract multiplier 0.85 | **0.0578**, basis CONTRACT |
| 9 | Opus 5.5 `cache_write_unknown` 1,000,000 (OTel) | range **[5.00, 8.00]**, ESTIMATED |
| 10 | gpt-5.6-sol: input 10,000 incl. cached 6,000 and write 2,000; output 1,000 | 0.008 + 0.0024 + 0.010 + 0.020 = **0.0404** |
| 11 | gpt-5.6-sol request with 300,000 input tokens (long-context band) | whole request at $8/$0.80/$10/$30 |
| 12 | fallback iterations: declined attempt output 0 on Fable 5; fallback on Opus 4.8 | declined **0.00** (rule `anthropic.refusal.pre_output`), fallback priced at Opus 4.8 |
| 13 | compaction iteration + message iteration | sum of both at the message model |
| 14 | unknown model `claude-foo-9` | `total=None`, `unpriced_reason="no rate row"`; totals report coverage < 1 |
| 15 | model id `claude-haiku-4-5-20251001`, `claude-sonnet-4-6@20260101`, `claude-opus-5-5[1m]`, `global.anthropic.claude-opus-5-5-v1:0` | resolve to base rows with correct channel/scope |
| 16 | Opus 5.5 usage dated 2026-09-21 | unpriced (row effective 2026-09-22) |

Also: `tests/test_pricing.py` (existing) passes unchanged; `pricing verify` against a recorded snapshot
fixture reports zero diffs and one injected diff is reported with row id; loading a registry with an
overlapping interval raises; a row with `verified_on` 60 days old emits `stale-rate`.

### F4. Claude Code transcript importer (WP-A2)

**What.** On-device, streaming, content-free importer for `~/.claude/projects/<slug>/<session>.jsonl`,
`<session>/subagents/agent-*.jsonl` and workflow-agent transcripts. It is the single most valuable source
for Claude Code fleets because only the transcript carries the 5m/1h write split, iterations, per-call
diagnostics and tool-result sizes without raw-body capture (`cc-enterprise-telemetry-gap`).

**Evidence.** `cc-import-dedup-message-id`, `cc-iterations-fallback`, `fp-cc-missing-final-usage`,
`cc-transcript-format`, `ent-local-transcripts`, `cc-hidden-calls-rollups`, `claude-code-telemetry-jsonl`,
`cc-miss-taxonomy-ground-truth`, `cc-prompt-snapshot-diffing`, `sdk-accounting-pitfalls`; empirical
scripts `03_dedup_check`, `04_semantics`, `13`, `14`, `22`.

**$ impact.** Prevents a +133% overstatement (naive line summing) and a 1–2% understatement (placeholder
output); enables every Claude Code lever in F8 (the largest enterprise spend line).

**Algorithm.**
1. Walk the tree (skip `journal.jsonl`); lane kind from path (`/workflows/` → WORKFLOW_AGENT,
   `/subagents/` → SUBAGENT, else MAIN); lane parent from `meta.json` (`toolUseId`, `parentAgentId`,
   `agentType`, `model`, `spawnDepth`) and `isSidechain`.
2. Stream lines; JSON errors → quarantine (file, line number, reason); drop repeated `(file, uuid)` lines.
3. `type == "assistant"`: group by `message.id` (the de-duplication key; one API response is written as
   ~2.4 lines; no id spans two files); skip `message.model == "<synthetic>"` (zero usage); keep the entry
   with the **maximum `usage.output_tokens`** (monotone across split entries; ties → last); if any other
   usage field differs across entries, keep the max-output entry and emit `dq.split_usage_mismatch`;
   `stop_reason` = last non-null across entries; collect tool_use ids.
4. Request start time = timestamp of the latest preceding `user`/`attachment` entry in the same file
   (the trigger); response end = last assistant entry timestamp for the id; `ttft_ms` unknown. TTL math
   uses request start (`anth-ttl-start-1h`).
5. Usage → buckets via `anthropic.messages` convention with the iterations rule (§2.5); `speed`,
   `service_tier`, `inference_geo` (`not_available`/empty → None) into `PricingContext`;
   `server_tool_use` counters; `output_tokens_details.thinking_tokens` → `output_reasoning`;
   `message.diagnostics.cache_miss_reason` → `CacheDiagnostic` (`cache_missed_input_tokens` kept as a
   magnitude only).
6. `usage_source`: `MESSAGE_START_ONLY` when `stop_reason` is null for every entry of the id and the next
   entry in the thread is a `tool_result` answering one of its tool_use ids (96.4% of no-stop calls,
   `fp-cc-missing-final-usage`); the priced output is the logged count (an exact lower bound) and a
   `dq.message_start_only` note carries an ESTIMATED range for the missing output (lower: median output of
   complete same-model tool_use calls; upper: visible-content bytes ÷ per-model bytes-per-token ratio).
7. Events: `system.compact_boundary` → COMPACTION (`preTokens`, `postTokens`, `durationMs`, `trigger`);
   `system.model_refusal_fallback` and `content[].type == "fallback"` → FALLBACK_REFUSAL; API error entries
   → API_ERROR (status); `attachment` entries (skill listing, MCP instructions, deferred-tools deltas,
   task reminders) → CONTEXT_INJECTION with type and byte length only.
8. `appended`: for each request, AppendedItem per tool_result (tool name via tool_use id map, bytes of
   text content, `is_error`, image count), per attachment (type, bytes), per human text (bytes).
9. Hidden calls: compaction summary requests never appear as assistant entries; synthesize an
   `Inference(kind=COMPACTION, usage_source=ESTIMATED)` from each compact_boundary (input = `preTokens`
   priced as read or write depending on TTL state, output = `postTokens`) as a **separate estimated line**
   excluded from the exact bill and shown in data quality (`cc-hidden-calls-rollups`: 0.2–2.4% of bill).
10. Never read rollups as spend: `toolUseResult.totalTokens` and workflow `totalTokens` are final context
    size, not cost (`cc-hidden-calls-rollups`: workflow totals 25× below processed tokens).
11. Attribution: `attributionSkill/McpServer/Plugin/Agent`, `effort`, `entrypoint`, `version`; `cwd`
    hashed to a project key; `gitBranch` dropped; principal from `--principal` or OS user, HMAC'd; team
    etc. from `--attr`/`OTEL_RESOURCE_ATTRIBUTES`.
12. Self-checks reported as data quality: naive per-line sum ratio (2.33× on the corpus); version histogram;
    retention warning when the oldest file is near the 30-day cleanup; unknown-field counts per version.

**Acceptance tests** (synthetic fixtures under `tests/fixtures/claude_code/`, no real content):
one message split over 3 lines with outputs 3/250/470 → one inference with output 470; a duplicated uuid
line is dropped; `<synthetic>` skipped; two-element iterations (declined output 0 on `claude-fable-5`,
fallback on `claude-opus-4-8`) → two inferences, declined `billable=False`; declined with output 6 →
billable; compaction iteration priced; 1h/5m split preserved (1h 3,000 + 5m 2,000 on Opus 5.5 → $0.034);
`cache_creation_input_tokens` 6,000 with split 2,000+3,000 → `cache_write_unknown` 1,000 and a note;
no-stop placeholder followed by tool_result → `MESSAGE_START_ONLY` and a note with a range; subagent file
becomes a SUBAGENT lane linked to the main lane; diagnostics captured; naive ratio reported; a truncated
last line is quarantined and the rest imported; the same file ingested twice yields no new rows;
throughput ≥25k lines/s on a 200k-line synthetic file.

### F5. Reconciliation against provider usage, cost and analytics data (WP-R)

**What.** `tokenbill reconcile`: parsers for Anthropic `usage_report/messages`, `cost_report`,
`usage_report/claude_code`, Enterprise Analytics (`usage_report`, `user_usage_report`, `cost_report`,
`user_cost_report`) and OpenAI usage/costs pages (recorded JSON; opt-in live pull with an admin key from an
environment variable, paginated, rate-limited, 31-day chunking, never logged); three-layer comparison;
residual explanation; effective discount and contract suggestion.

**Evidence.** `ent-reconciliation`, `anth-admin-api-reconciliation`, `anth-admin-usage-cost-api`,
`ent-anthropic-admin-apis`, `cc-admin-analytics-apis`, `anth-claude-code-analytics-otel`,
`cc-reconciliation-matrix`, `cc-anthropic-discount-visibility`, `cc-priority-legacy`,
`oai-admin-usage-costs`, `assignment-telemetry-srm` (±1% ledger ↔ cost_report), `audit-evasion-limits`
(compare the three provider-reported sources for internal consistency).

**$ impact.** Trust gate for everything else; exposes realized discounts and uncredited spend
(`cc-ccu-private-offers`: discounts not applied before offer acceptance).

**Algorithm.**
1. *Rate-card check (independent of our traces).* Price the provider's usage-report tokens with our rate
   card per (date, workspace, model, bucket, service_tier, inference_geo, speed); join to cost-report lines
   per (date, workspace, model, token_type/cost_type) through a mapping table
   (`reconcile.py: COST_TYPE_MAP`, unknown values → residual `unmapped_cost_type`). Rate-card error per
   model-day = (priced − invoice)/invoice. Within tolerance if |error| ≤ `tolerance_pct` (default 0.5%) or
   |priced − invoice| ≤ $0.01 × rows (cent rounding in the source).
2. *Token coverage.* Ledger tokens vs usage-report tokens on the finest shared key (date, model, and
   workspace/key when the ledger has them). Ledger > provider + max(1%, 1,000 tokens) is an **over-count
   row** (import bug or double count) and fails the verdict.
3. *Dollar coverage.* Ledger dollars ÷ invoice dollars per model-day: the share of the invoice explained
   by the ledger.
4. *Residual explanation codes:* `priority_excluded_from_cost_report` (usage report `service_tier=priority`
   priced at burn-down rates), `code_execution_cost_report_only`, `default_workspace_null_id`,
   `revision_window` (Enterprise Analytics revisable 30 days → provisional), `ccu_single_line`,
   `no_reporting_api` (Claude Platform on AWS), `cents_rounding`.
5. *Effective discount* = 1 − invoice/priced_list per model and token type; with `--suggest-contract`,
   write a contract overlay (per-model multipliers rounded to 4 decimals, flagged `derived`); rerunning
   with it must bring the rate-card error within tolerance, else the discount is not a simple multiplier
   and is reported as such.
6. *Claude Code Analytics:* per user-day token coverage for Claude Code lanes (joined on the HMAC'd
   principal; rendered only as team aggregates with k≥5 or self-view); `estimated_cost` compared as
   `provider_estimate`.
7. *Verdict:* `reconciled` if every closed model-day is within tolerance and there are no over-count
   rows; `not_reconciled` otherwise; `insufficient_data` without invoice rows. Exit 3 when not reconciled
   (unless `--report-only`).

**Acceptance tests** (recorded fixtures shaped like the documented responses): usage + cost fixtures priced
exactly → rate-card error 0 and verdict reconciled; cost report 15% below list on every model →
effective discount 0.15, `--suggest-contract` overlay, rerun reconciled on CONTRACT basis; a Priority Tier
bucket → residual explained, not an error; ledger tokens 3% above provider on one day → over-count row,
verdict not_reconciled, exit 3; Enterprise Analytics day within 30 days → provisional; cents strings with
many decimals parsed exactly; `--live` without `--admin-key-env` → usage error 2; no network access
occurs without `--live` (test with a socket guard).

### F6. Privacy, pseudonymization and aggregation governance (WP-S + foundation primitives)

**What.** §2.9: content tiers, install/org HMAC keys, volatile-class-before-hash, secret scanning,
k≥5 suppression with complementary suppression, self-view, audited break-glass, retention and purge,
sanitized output, DPIA and works-council pack.

**Evidence.** `ent-privacy-by-default`, `ent-cache-diagnostics-privacy`, `ent-redaction-limits`,
`ent-secrets-in-traces`, `aggregation-k5`, `labor-law-constraints`, `leaderboard-goodhart`,
`vendor-leaderboards`, `cb-security-privacy`, `ent-report-a11y-hardening`.

**$ impact.** Deployment gate; without it EU rollout can be blocked (`labor-law-constraints`).

**Acceptance tests.** No string field of any record produced in `none` tier contains more than 64 bytes of
source text (property test over fixtures); in `fingerprint` tier hashes differ across keys and match
within a key; a fixture with an `sk-ant-…` key in a tool result: `full` tier writes `[REDACTED:anthropic_key]`
and every tier reports one secret of type `anthropic_key`; a group of 4 principals is suppressed and a
second group is complementarily suppressed so the total does not reveal it; `findings --group-by principal`
without `--self` is a usage error; `--break-glass` writes an audit row; `purge --principal` removes every
row for that pseudonym and writes an audit row; terminal output of a run id containing `\x1b[2J` is
sanitized; `export --content full` is refused; HTML contains the CSP meta tag.

### F7. Calibrated simulation and server-truth validation (WP-C1; block-level part in WP-C2)

**What.** `tokenbill calibrate` and the calibration gate that every counterfactual inherits:
lane assembly, usage-level replay (§2.10), one-step-ahead predictive validation, ρ fitting, token-density
fitting, and the confusion matrix against server `cache_miss_reason`.

**Evidence.** `ashrae-calibration-gate` (FEMP Table 4-2 from ASHRAE G14: monthly NMBE ±5%, CV(RMSE)
≤15%; hourly ±10%, ≤30%; v0.1.2 fails on the real corpus), `capc-two-tier-rho` (Sonnet 4.6 ~2k-token
prefixes hit 0.53–0.83, ≥4,096 hit 1.0), `gu-audit-icml25` (multi-server caches), `cb-cache-diagnostics`,
`cc-miss-taxonomy-ground-truth` (corrected: only the four `*_changed` labels map to breakers;
`previous_message_not_found` and `unavailable` mean no comparison), `cc-chars-per-token-calibration`
(2.2–2.7 bytes per billed token on tool output, vs the 3.7 constant), `chars-heuristic-error`,
`anth-ttl-start-1h`, `anth-concurrency-fanout`, `cb-concurrency`, `cb-cross-run-cache`,
`realization-rate-backtest`.

**$ impact.** Prevents the systematic overclaims that make savings tools lose credibility
(`projection-optimism`: 1.5–4× overclaims in the analog; `cb-concurrency`: 65% savings the API cannot
deliver in a fan-out case).

**Algorithm.**
1. *Lane assembly* (`sim/lanes.py`): explicit lane keys from adapters; otherwise (trace sources without
   lane ids) split a run into lanes by `(model, tools-tier hash, system-tier hash)` and assign each request
   to the lane whose last request has the longest common block prefix (radix index on block hashes); order
   by `(ts_start_ms, seq)`. Cache scope = workspace (1P/Claude Platform on AWS/Foundry) or org
   (Bedrock/Vertex) from attribution; `unknown` otherwise.
2. *Predictive replay*: for every transition, predict hit/miss and reads from documented rules without
   using the transition's own reads (§2.10). Price predicted usage; aggregate billed vs predicted cost per
   period (hour/day/month) and lane kind.
3. *Metrics*: with `b_t` billed and `s_t` predicted cost in period `t`, `n` periods, `p` fitted parameters
   (0 for documented; calibrated uses 5-fold cross-validation by day so held-out `p = 0`):
   `NMBE = 100·Σ(b_t − s_t) / ((n − p)·mean(b))`, `CV(RMSE) = 100·sqrt(Σ(b_t − s_t)² / (n − p)) / mean(b)`.
   Gate thresholds: month ±5% / 15%; hour and day ±10% / 30% (day uses the hourly row, a documented
   choice because Table 4-2 has no daily row). Minimum 12 periods, else `insufficient_data`.
4. *ρ fitting*: bands = prefix `[0,2k) [2k,4k) [4k,32k) [32k,200k) [200k,∞)` × gap `[0,60s) [60s,300s)
   [300s,3600s) [3600s,∞)` × TTL state (alive/expired) × model family × channel; `ρ = hits/trials` for
   transitions the documented rules call hits, Wilson 95% interval; cells with <30 trials pool into the
   parent band. Calibrated expectation = documented × ρ.
5. *Density fitting*: on pure-append transitions (hit, no events), regress `ΔT_i = T_i − T_{i-1}` on
   appended bytes by kind (tool_result, user text, attachment, assistant) with non-negative least squares
   (coordinate descent, stdlib) per tokenizer family; report bytes/token and MAPE; defaults when data is
   thin: 2.5 bytes/token (4.7+ family, tool output), 3.3 (pre-4.7); images by `ceil(w/28)·ceil(h/28)`
   capped at 1,568 (standard) or 4,784 (4.7+ high-res) (`anth-vision-tokens`).
6. *Server-truth confusion matrix*: for transitions carrying a `CacheDiagnostic`, cross-tabulate
   Token Bill's predicted cause against the server reason. Mapped pairs: `ttl_expiry ↔
   previous_message_not_found` (only when gap > τ), `model_switch ↔ model_changed`, block-level
   `system/tools/messages` divergences ↔ `*_changed`, `compaction ↔ messages_changed|system_changed`
   (expected rebuild). Report precision/recall per mapped class, the unlabeled count, and the
   `unavailable`/`previous_message_not_found` share as data quality (not errors).
7. *Gate consequence*: if the documented replay fails and the calibrated replay passes, projections use
   the calibrated mode; if both fail, every projection is `Calibration.UNCALIBRATED`, is ranked below
   calibrated ones, cannot be signed, and the report says why.

**Acceptance tests.** Synthetic lanes generated from the documented rules (fixture generator in
`tests/sim/`) → documented NMBE = 0 and CV(RMSE) = 0; the same lanes with 10% of should-hit transitions
randomly flipped to misses (seeded) → ρ ≈ 0.90 ± 0.03 and the calibrated replay passes while the
documented one reports the bias; the corpus-shaped fixture of 128 idle>1h transitions labeled
`previous_message_not_found` → TTL-expiry precision = recall = 1.0; a model switch labeled
`model_changed` → correct; `unavailable` rows counted as unlabeled; a fan-out of 5 requests with the same
prefix at the same timestamp predicts 5 writes, not 1 write + 4 reads (`cb-concurrency`); two sessions
sharing a static prefix in one workspace predict a read on the second session's first call
(`cb-cross-run-cache`); a lane with 11 periods yields `insufficient_data`.

### F8. Usage-level detectors and policy levers (WP-D; replay primitives in WP-C1)

**What.** The dollar engine for Claude Code and any source with ordered usage (no content needed).
Findings carry an observed cost, a recoverable counterfactual, a fix with a config patch, a validation note
and research references. Levers are policy replays (§2.10) whose credit is later Shapley-adjusted (F9).

Notation: `r`, `w5`, `w1`, `u`, `o` are the resolved $/token rates of the lane's model and context;
`E_i`, `M_i`, `τ`, `S` as in §2.10; "est." = ESTIMATED.

| Id | Trigger (content-free) | `cost_observed` | `recoverable` | Fix / patch | Evidence (corpus size) |
|---|---|---|---|---|---|
| `cc.ttl-expiry` | miss event with cause TTL expiry (gap > τ) | Σ `M_i·(w_i − r)`, est. (M inferred; validated vs `previous_message_not_found`) | via levers `ttl-policy`, `keepalive`, compact/clear-before-idle | TTL policy; SessionStart hook warning using `prompt_cache_likely_expired`/`estimated_cache_write_usd` | `cc-cold-resume` (7.95% of bill, $7.22/event), `cc-miss-taxonomy-ground-truth` (52.5% of miss $), `channel-code-review-hooks` |
| `cache.model-switch` | model changes between consecutive requests in a lane | Σ `E_i·(w_new − r_new)`, est. | refusal fallback with no credit shift (first call on the new model writes ≥80% of prefix): full premium recoverable (free win); A→B→A within one TTL (availability ping-pong): second rebuild; user/opusplan switches: trade-off | enable fallback credit (`fallback-credit-2026-07-01`) or `fallbacks: 'default'`; switch at `/clear`/compaction or delegate to a subagent | `cc-model-switch-cost` ($4.69 per main-thread switch), `fp-fallback-credit`, `fp-cc-fallback-chains` |
| `cache.gateway-disabled` | lane with ≥5 requests, `T ≥` min prefix, Σ reads = Σ writes = 0 | Σ `U·u`, exact (it is billed uncached input) | replay with healthy-lane ρ: reads `ρ·E_i`, writes appended tokens at 5m; est. | forward `cache_control` and `anthropic-beta` unchanged; `ENABLE_TOOL_SEARCH=true` behind custom base URLs | `cc-gateway-marker-stripping`, `cc-gateway-cache-strip`, `gateway-strip` (up to ~90% of affected input) |
| `cache.concurrent-fanout` | ≥2 lanes' first requests in the same scope and model start within 10 s, each writing ≥ min prefix | `(N−1)·shared·(w − r)`, `shared = min(W_first)`; est., upper bound | same, range `[0, value]` | stagger: send one, await first token, then N−1 (Claude Code workflows hold 5 s) | `cc-agent-spinup-fanout` (1.2%), `anth-concurrency-fanout`, `ci-fanout-stagger` |
| `cache.write-heavy` | lane with ≥10 requests, writes on ≥80% of them and Σ W > Σ R | Σ `W·(w − u)` write premium, est. as waste | block-level placement when fingerprints exist; else "explicit breakpoint at end of stable prefix / explicit mode" upper bound | per provider | `write-without-read`, `oai-cache-write-waste` (Codex on Bedrock Mantle: writes ~90% of GPT-5.6 spend for independent requests sharing a startup prefix; causality not established) |
| `cc.compaction-cold` | COMPACTION event whose preceding request is > τ earlier | compaction input priced as write minus as read, est. | same | compact while warm; `/clear` when cold | `compaction-timing`, `anth-compaction-iterations-billing` ($0.21 vs $0.04) |
| `premium.modifiers` | inferences with `speed=fast`, `inference_geo=us`, regional endpoint, Priority | cost − cost at standard, **exact** | same amount × RR 1.0, est.; trade-off (latency/residency) unless sticky | `fastModePerSessionOptIn`, `CLAUDE_CODE_DISABLE_FAST_MODE`; global endpoints where policy allows | `anth-modifiers-geo-fast-priority`, `cc-sticky-escalation`, `premium-modifiers` |
| `output.max-tokens` | attempts with `stop_reason=max_tokens` | their cost, exact | cost of truncated attempts followed by a retry, est. | `max_tokens` 64k (128k at xhigh/max) or lower effort | `anth-output-hygiene`, `max-tokens-truncation` |
| `context.size-tax` | always (informational) | reads beyond 100k/200k/400k: Σ `max(0, R_i − X)·r`, **exact** decomposition of billed reads | n/a (see `compaction-window`) | — | `cc-context-size-driver` (reads beyond 200k = 23.2% of bill), `context-tax-residency` |
| `attrib.tool-output-carry` | appended tool results | per tool: `tokens·w + tokens·r·(later requests until reset)`, est. | none (trajectory-changing; measure) | `bashOutputMaxChars`, `MAX_MCP_OUTPUT_TOKENS`, PreToolUse/PostToolUse filter hooks | `cc-tool-output-carry` (Bash+Read 87% of appended carry, ~10% of bill), `tool-output-bounding` |
| `attrib.config-tax` | CONTEXT_INJECTION events | injected bytes → tokens × reads, est. | none | `skillListingBudgetFraction`, `claudeMdExcludes`, fewer always-on MCP servers | `cc-config-sprawl-tax` (~3–7%), `cc-fixed-context-overhead` |
| `usage.effort-mix` | always (informational) | spend share by effort; thinking share of output | none (trajectory-changing) | `maxEffortLevel`; eval-gated sweep | `cc-output-thinking-effort`, `anth-effort-sweep` |

Levers (policy replays; recoverable = observed − counterfactual, documented and calibrated):

| Lever | Counterfactual (minimal change) | Label | Evidence and size |
|---|---|---|---|
| `ttl-policy` (per lane kind × billing path) | switch τ; every write in the lane repriced (5m 1.25× ↔ 1h 2×); transitions with `300 s < gap ≤ 3600 s` flip (5m→1h: miss→hit with `R' = E_i`, writes = appended; 1h→5m: hit→miss with `R' = S`) | est., calibrated via ρ; free win | `cc-ttl-advisor` (5m default +11.8% main-thread vs 1h; subagents on 1h +17–18%), `anth-ttl-choice-keepalive` (1h wins at ~1 turn in 30 after a pause; use 1-in-20), `cc-ttl-policy`, `cc-ttl-billing-path` |
| `keepalive` (`κ` = 240 s, max idle `M` = 60 min) | for TTL-expiry transitions with gap ≤ M: `floor(gap/κ)` KEEPALIVE inferences each reading `P_{i-1}` (output 0), then the transition hits | est.; free win (not allowed with stream, structured outputs, forced tool_choice, batch) | `keepalive-economics` (break-even idle `τ(w/r − 1)`: ~46 min at 0.1× reads, ~96 min Opus 5.5, ~196 min Fable 5.1), `cc-ttl-advisor` (+5.5% vs 1h here) |
| `compaction-window` (w ∈ 200k/300k/400k/500k/700k, main lanes) | when `T_i > w` insert a COMPACTION inference at `ts_i` (input `T_i` as read if warm else write; output = lane median `postTokens`, default 20,283 flagged), then shift later contexts by `Δ = T_i − post`, scaling their billed buckets by `T'_j/T_j` and making the first post-compaction request a cold write; repeat when `T'` exceeds `w` | est., **upper bound** (ignores re-work and quality), trade-off, needs eval | `cc-compaction-threshold-sim` (−18.4% of bill at 700k … −29.8% at 400k, upper bound; 1M sanity −0.2%), `cc-autocompact-window`, `hidden-reacquisition` |
| `delegation-routing` | re-price subagent/workflow lanes (or an `agent_type`) at a target model; token counts × [1.0, 1.35] when tokenizer families differ | est., price-only, trade-off, needs eval | `cc-delegation-model-routing` (workflow agents → Sonnet 5: −25.5% of bill at equal tokens), `cc-subagents-teams-workflows` |
| `same-tier-upgrade` | re-price at the same-tier successor with the same tokenizer (Fable 5→5.1, Opus 5→5.5) | est. (rate arithmetic exact; behavior unvalidated), light eval | `cc-same-tier-upgrade` (−21.0% and −11.7% of bill), `anth-prompt-audit-migration` |
| `batch-eligible` | eligible = workload_class ∈ {ci, eval, scheduled} or single-shot non-streaming API lanes, not already batch, not Managed Agents; price ×0.5; reads kept at a 30–98% band (misses become 5m writes; on Bedrock batch there is no caching, so reads become uncached) | est. range; free win for eligible traffic | `anth-batch-stacking`, `batch-flex-50`, `batch-plus-cache` |
| `repair:*` | `fallback-credit`, `stagger-fanout`, `restore-gateway-caching`, `avoid-model-switch` as defined in the detector table | est. | as above |

**Acceptance tests.** One synthetic lane per detector with a planted pattern and hand-computed dollars;
e.g. a Opus 5.5 main lane with context 500,000 tokens, a 2-hour idle gap and a 1h-TTL rewrite:
`M = 500,000`, `cost_observed = 500,000 × ($8 − $0.20)/MTok = $3.90`; `ttl-policy` with gaps of 7 minutes
on a 5m lane flips exactly those transitions and matches a hand-computed delta; `keepalive` with a 20-minute
gap adds 5 pings; `compaction-window` with `w` above the lane maximum changes nothing (sanity test from
the corpus); `delegation-routing` from Opus 4.8 (pre-4.7 tokenizer) to Sonnet 5 yields a range; every
finding has references, a fix and a label; `premium.modifiers` equals the exact difference of two golden
cases (F3 #4 − #1 = $0.068).

### F9. Overlap-aware action plan, realization intervals and policy pack (WP-D; Shapley math in WP-V)

**What.** `findings` ranks by Shapley credit; `whatif` exposes joint replays; `policy` emits a
deployable patch. Never sums standalone ceilings (R7).

**Evidence.** `shapley-attribution` (standalone ceilings overcount 4.6% on the real corpus; sequential
credit swings 69% by order), `realization-rate-backtest`, `projection-optimism`, `llm-token-not-bill`,
`org-defaults-datadog`, `defaults-beat-nudges-meta` (defaults d=0.68 vs small nudges at scale),
`cc-managed-settings-levers`, `enforcement-primitives`, `litellm-gateway-injection`,
`stepped-wedge-mdm` (server-managed settings are org-wide; per-group via MDM files or the apps gateway).

**$ impact.** Datadog's defaults program: 20–40% of affected spend (`org-defaults-datadog`); prevents
~5% overcount and misattributed credit.

**Algorithm.**
1. Group levers that touch the same lanes (interaction groups by lane kind); disjoint groups add.
2. For each group of k ≤ 6 levers compute all 2^k joint replays (`Policy.combine`), value
   `v(S) = cost(observed) − cost(S)` in the gated mode; Shapley
   `φ_i = Σ_{S ⊆ N∖{i}} |S|!(k−|S|−1)!/k! · (v(S ∪ {i}) − v(S))` with exact rational weights
   (`fractions.Fraction`) converted to Decimal; k > 6 → 200 seeded permutations, SE reported.
3. Monthly normalization: `φ_i × 30 / window_days`; realization priors
   (`verify/data/realization_priors.json`): rate levers 1.0/1.0/1.0; deterministic cache fixes (TTL,
   keepalive, breakpoints, serialization, gateway, fallback credit, stagger) 0.8/0.9/1.0;
   trajectory-changing (compaction window, routing, effort, output caps, compression) −0.2/0.5/1.0;
   behavioral nudges: not projected. `projected_monthly` = Figure(ESTIMATED, range p10–p90, point p50,
   calibration from the replay, `upper_bound` inherited).
4. Rank by p50; tag free-win vs trade-off; the plan's headline total is the joint replay of the selected
   levers, never a sum.
5. Policy pack (`policy_pack.py`): map levers to verified keys. Claude Code managed settings:
   `promptCacheTtl`, `subagentPromptCacheTtl` (or `CLAUDE_CODE_PROMPT_CACHE_TTL`,
   `ENABLE_PROMPT_CACHING_1H`, `FORCE_PROMPT_CACHING_5M`), `env.CLAUDE_CODE_AUTO_COMPACT_WINDOW`
   (100,000–1,000,000; the `--autocompact` flag is not preempted by `autoCompactWindow`, so enforce via the
   managed env block), `env.CLAUDE_CODE_SUBAGENT_MODEL`, `availableModels`/`enforceAvailableModels`,
   `maxEffortLevel` (≥2.1.267), `fastModePerSessionOptIn`, `env.ENABLE_TOOL_SEARCH`,
   `cleanupPeriodDays` (keep transcripts for analysis), `modelPricing` (from the contract overlay),
   `env.OTEL_RESOURCE_ATTRIBUTES` (team/cost-center/arm tags), `env.OTEL_METRICS_INCLUDE_ENTRYPOINT`.
   LiteLLM: `cache_control_injection_points` per model (system + index −1) with TTL. Each key carries its
   projected monthly Figure, label, needs-eval flag and rollout note (org-wide vs per-group delivery).
   Trade-off keys are excluded unless `--include-tradeoffs`. Nothing is applied automatically.

**Acceptance tests.** Three levers with a hand-built value function (`v({})=0, v(A)=10, v(B)=20, v(C)=5,
v(AB)=26, v(AC)=15, v(BC)=24, v(ABC)=30`) give exactly φ_A = 8, φ_B = 17.5, φ_C = 4.5 (weights 1/3,
1/6, 1/6, 1/3; sum 30); the standalone sum (35) is never displayed as a total; a lever with RR prior −0.2…1.0
yields a range crossing zero and is labeled so; the policy pack for a TTL lever contains
`promptCacheTtl: "1h"` and its Figure; `--include-tradeoffs` off → no model/effort keys; the pack
validates against a key allow-list with minimum versions.

### F10. Measurement, verification guards and signed receipts (WP-V)

**What.** `measure plan`, `measure run`, `receipt create/sign/verify`: adjusted-baseline savings at
constant prices, designs matched to how enterprises roll out, guards that decide the label, and
tamper-evident receipts.

**Evidence.** `mv-adjusted-baseline`, `staggered-did`, `cuped`, `rtm-targeting` (targeting top spenders
inflates by ~17–23% from regression to the mean alone), `stepped-wedge-mdm`,
`cache-interference-cluster` (individual randomization inside a workspace biased −28%),
`switchback-carryover`, `heavy-tails-power` (MDE ~13.6% at 1,000 developers, ~4.5% at 10,000),
`ratio-delta-cluster-se`, `sequential-monitoring` (daily peeking triples false positives),
`units-gaming`, `long-run-holdback`, `assignment-telemetry-srm`, `signed-receipts`,
`verified-savings-gap-rtk`, `eval-statistics` (~50 cases × 5 trials for cutovers), `quality-guardrail`,
`value-adjusted-cost-metric`.

**$ impact.** The differentiator: no one sells invoice-reconciled, verified savings; prevents the 7%
negative-ROI rollouts seen in paired billed studies (`llm-token-not-bill`).

**Algorithm.**
- *Primary unit:* cost per active developer-day, intention-to-treat, at the **baseline rate card** (R8);
  rate variance reported separately as EXACT. Secondary units (per merged PR, per task) only with outcome
  data and delta-method or cluster-bootstrap variance.
- *Plan:* seeded random wave order over clusters (default cluster = cache scope, i.e. workspace; team if
  the workspace is shared and the lever does not touch shared prefixes), 10–25% never-treated holdback,
  assignment log (JSON lines, SHA-256), pre-registration (lever, metric, MDE, looks), per-wave MDM payload
  names and `OTEL_RESOURCE_ATTRIBUTES` arm tags.
- *Estimators:* (a) paired A/B per task (campaign file of runs with task id, arm, success, cost from the
  ledger): cost per success with task-clustered bootstrap (B = 10,000, seeded); (b) 2×2
  difference-in-differences on CUPED-adjusted outcomes (`θ = cov(Y_post, X_pre)/var(X_pre)`, missing-pre
  indicator for new hires) with cluster bootstrap (B = 2,000, stratified by arm); (c) staggered /
  stepped-wedge imputation estimator: fit `Y_it = α_i + λ_t` on untreated cells by alternating
  projections (tolerance 1e-9), impute `Ŷ0` for treated cells, ATT = mean(Y − Ŷ0), cluster bootstrap. No
  TWFE, no naive pre/post. Statistics run in float64; resulting dollar estimates are converted to Decimal
  micro-USD (they are estimates, not accounting).
- *Guards* (`verify/label_policy.py`): SRM χ² on active dev-days by arm (fail p < 0.001); placebo at a fake
  adoption date in the pre-period (CI must include 0); pre-trend check; MDE by 200 A/A re-randomizations
  of the pre-period (verified requires MDE ≤ 0.8 × |projected|); reconciliation verdict `reconciled` for
  the window (F5); cluster unit ≥ cache scope for cache-touching levers; quality guardrail for trade-off
  levers (one-sided 90% lower bound of merged PRs per dev-day change > −5% when outcome data is supplied);
  pre-registered looks only (no continuous peeking); burn-in ≥ max(TTL, session length, settings refresh).
- *Labels:* VERIFIED iff randomized assignment with a logged seed, every guard passes, and the CI
  excludes zero. MEASURED iff a comparison group exists, CI computed and reconciliation passes (failing
  guards listed). `measure` never emits EXACT or ESTIMATED.
- *Receipts:* canonical JSON (sorted keys, no whitespace, ASCII keys, integers only: `*_usd_micro`,
  `*_milli`, `*_ppm`; this subset is byte-identical to RFC 8785 JCS), `_type
  "urn:tokenbill:receipt:v1"`, subject = lever id + SHA-256 of the lever/policy patch, predicate = label,
  metric, design, scope, window, result (estimate, CI, projected, realization rate, Shapley credit), guards,
  adjustments, rate-card SHA-256, assignment-log SHA-256, tool version. Envelope: DSSE
  (`payloadType "application/vnd.tokenbill.receipt+json"`, PAE = `"DSSEv1" SP len(type) SP type SP
  len(body) SP body`), signed and verified by shelling out to `ssh-keygen -Y sign/verify -n
  tokenbill-receipt` with an `allowed_signers` file (no crypto dependency). `receipt sign` refuses
  ESTIMATED receipts and receipts whose reconciliation guard failed.

**Acceptance tests.** Simulated stepped-wedge fleet with a known 25% effect (fixture generator ported
from the research's `causal_sim`, seeded): imputation estimate within ±5% of truth and 95% CI covers it in
≥90% of 50 seeds (CI-light: 10 seeds in default test run, 50 in the slow suite); naive pre/post with a
simultaneous 20% price cut is flagged by R8 (constant-price estimate unaffected, rate variance reported);
targeting top spenders without randomization cannot reach VERIFIED; SRM injected (60/40 when planned
50/50) fails the guard; receipt canonical bytes are identical across two processes; sign then verify
succeeds with a generated ed25519 key (test skipped when `ssh-keygen` is absent); flipping one byte of the
payload fails verification (exit 3); signing an ESTIMATED receipt is refused.

### F11. Fleet telemetry and multi-provider adapters (WP-A3)

**What.** OTLP/JSON file ingestion (collector `file` exporter output; `.gz` supported): Claude Code
`claude_code.api_request` and `api_error` events → requests/attempts (writes with unknown TTL,
`cost_usd` as provider estimate, `query_source`, `effort`, `speed`, `agent/skill/plugin/mcp_server`
attributes, `session.id`, HMAC'd `user.id`/`user.email`, resource attributes as attribution);
`claude_code.token.usage`/`cost.usage` metrics → aggregates (coverage checks); beta `llm_request` spans →
`ttft_ms`; opt-in raw request bodies (`OTEL_LOG_RAW_API_BODIES`) → fingerprints computed on ingest and the
body discarded unless `--content full`. OTel GenAI spans and OpenInference LLM spans via the convention
registry (LLM-kind only). OpenAI Responses/Chat usage JSONL (from SDK logs or gateways) and Codex rollout
`token_count` events (pricing gated on the pinned convention fixture). Bedrock Converse usage and
invocation-log records (`identity.arn`, `requestMetadata` → attribution; cache counts from the logged
response body).

**Evidence.** `cc-otel-schema` (corrected: `ttft_ms` only on the beta span; inline bodies truncated at
60 KB), `ent-claude-code-otel`, `cc-otel-pipeline`, `claude-code-otel-fleet`, `otel-openinference-importer`,
`otel-genai-semconv`, `ent-otel-genai-semconv` (both `cache_creation` and `cache_write` names),
`codex-usage-format`, `bedrock-cache-semantics`, `bedrock-attribution`, `cc-enterprise-telemetry-gap`,
`adapter-adoption-ranking`.

**$ impact.** Fleet coverage without touching developer machines; the only per-user path on some cloud
channels besides gateways (`cc-otel-pipeline` corrected).

**Acceptance tests.** An OTLP fixture with 3 `api_request` events, 1 `api_error`, and token metrics:
3 requests, 1 failed attempt, aggregates equal the event sums; `cost_usd` never appears in a billed
column; `user.email` is HMAC'd; a request seen both in OTel and in a transcript (same `request_id`) is
stored once with transcript usage (higher priority) and the OTel record as corroboration; both
`gen_ai.usage.cache_creation.input_tokens` and `gen_ai.usage.cache_write.input_tokens` parse; a Codex
fixture without a pinned convention yields `unpriced_reason="convention unverified"`.

### F12. trace@2 and recorder v2 (WP-A1)

**What.** A content-free, delta-encoded, attempt-aware trace format and a recorder that captures the full
envelope at call time.

trace@2 (JSONL, `.gz` allowed), one record per line, `schema: "tokenbill/trace@2"`:
- `header`: `trace_id`, `content_tier`, `key_id`, `producer`, `created_ms`, default attribution.
- `blocks`: definitions of new `BlockRef`s (content-addressed by `h`).
- `request`: ids, session, lane, seq, `ts_start_ms`, `parent` request id, `keep` (blocks kept from parent),
  `append` (new block hashes), breakpoint markers, `params`, `attribution`, `appended` summary.
- `attempt`: request id, attempt number, timing (`ts_start_ms`, `ttft_ms`, `duration_ms`), outcome,
  HTTP status, error type, retry layer, `retry_after_ms`, `x-stainless-retry-count`, provider ids, served
  model, stop reason, **raw provider usage object verbatim (numbers only)** plus its convention id,
  diagnostics, applied edits. Keeping raw usage means a later convention fix can re-normalize old traces.
- `content` (tier `full` only, never exported): block hash → text.

Recorder v2 (`instrument.py`): wraps `messages.create`, `messages.stream`, `beta.messages.create`,
`beta.messages.stream` (sync and async); snapshots and serializes the request **before** sending (SDK
objects via `model_dump()` when present; fixes the mutation and `repr` bugs of `cb-recorder-coverage`);
hashes blocks in wire order with `cache_control` stripped (so moving markers never look like edits,
`cb-moving-marker`), plus key-sorted and volatile-normalized hashes; records the top-level
`cache_control` (automatic caching), every prompt-affecting parameter, the full usage object including
`iterations`, `cache_creation`, `server_tool_use`, `service_tier`, `inference_geo`, `speed`; records
**every attempt including exceptions and aborted streams** (message_start usage + streamed output count,
`usage_source=PARTIAL_STREAM`) (`fp-recorder-blindspot`); optional `diagnostics=True` injects the
`cache-diagnosis-2026-04-07` beta header and `diagnostics.previous_message_id` per lane (opt-in because it
changes the request; first-party only); writes through a bounded background queue so file I/O never holds
a lock in the request path; never raises into the caller; optional `httpx_event_hooks()` for users who
pass their own httpx client to capture SDK-internal retries. `Recorder(path, format="trace@1")` keeps v0.1
behavior.

**Evidence.** `cb-schema-envelope`, `cb-recorder-coverage`, `fp-recorder-blindspot`,
`fp-trace-fields-standards`, `fp-sdk-retry-after-uncapped`, `cb-key-order`, `cb-moving-marker`,
`ent-cache-diagnostics-privacy`, `anth-cache-diagnostics-beta`, `ent-scale-storage`, `cb-scale-memory`.

**$ impact.** Enables block-level breakers (F13) and failure-path accounting (F14) for API agents;
~100× smaller traces than full-payload trace@1 (`ent-scale-storage`, illustrative).

**Acceptance tests.** Fake SDK doubles (no `anthropic` import): beta namespace recorded; a payload mutated
inside the stream context is recorded as sent; an exception produces an attempt with `http_error` and no
usage; an aborted stream produces `PARTIAL_STREAM`; moving `cache_control` from message 3 to message 4
leaves every block hash unchanged; reordering dict keys changes `h` but not `h_sorted`; trace@2 round-trips
through read/write byte-identically; a 300-call session with 150k-token context is < 2% of its trace@1 size;
existing `tests/test_instrument.py` passes with `format="trace@1"`.

### F13. Block-level replay engine and cache-breaker suite (WP-C2)

**What.** The v2 engine for fingerprinted traffic (trace@2, trace@1 via adapter, OTel raw bodies): the
block replay of §2.10 plus breakers, each with a priced repair.

| Breaker | Trigger | Repair (for pricing) | Fix text gates |
|---|---|---|---|
| `block.volatile-system` | system tier differs, `h_norm` equal, changed blocks carry volatile classes | replace volatile block by its normalized twin | move value to a mid-conversation `role:system` message (Opus 5/5.5/4.8, Fable, Mythos; not Sonnet 5) or the latest user turn (`anth-cache-preserving-apis`, `mid-conv-system-tool-addition-support`) |
| `block.system-edit` | system tier differs, not purely volatile | none (report cost only) | pin system text; reminders via mid-conversation system messages |
| `block.serialization-churn` | `h` differs, `h_sorted` equal (tools or messages) | sort keys | `json.dumps(sort_keys=True)`; deterministic schemas (`cb-key-order`, `nondeterministic-tools`) |
| `block.tool-churn` | tools tier: same multiset/different order (order), added/removed (subset), changed definition | restore first-seen order / constant superset | sort by name; `defer_loading`/tool search; `tool_addition` (beta) where supported (`tool-subset-churn`) |
| `block.history-rewrite` | first divergence in messages at an index < previous message count, **excluding** marker moves (hashes exclude `cache_control`), compaction blocks, applied context edits (reported by `context-edit-churn`), server `thinking_dropped` (reported as `thinking-strip`) | none | append-only history; batch clears with `clear_at_least` (`history-rewrite-kstar`, `cb-breaker-taxonomy`) |
| `block.param-churn` | tier salt changes from effort/thinking/tool_choice/speed/format/images | pin parameter | per-message effort (beta) where supported (`anth-invalidation-hierarchy`, `cache-aware-routing`) |
| `block.missing-breakpoint` | stable prefix ≥ min cacheable, no markers, no automatic caching, billed reads = writes = 0 | add end-of-messages breakpoint | v0.1 fix text |
| `block.lookback-overflow` | predicted miss because >20 collapsed positions separate a breakpoint from the last entry, confirmed by billed reads | intermediate breakpoint every ~15 positions | `anth-lookback-20`, `cb-lookback` |
| `block.breakpoint-placement` | a canonical placement policy is cheaper than observed | cheapest policy | "move breakpoint to end of shared prefix" (`cb-shared-prefix-sim`: ~56% on a shared-context workload) |
| `block.write-never-read` | written entries never read before expiry | drop that breakpoint / explicit mode | `write-without-read` |
| `block.fanout` | same prefix hash written by ≥2 requests overlapping `[ts_start, ts_start+ttft)` | stagger | `anth-concurrency-fanout` |

Each breaker's `recoverable` = replay(observed) − replay(repair), floored at 0 with the v0.1 wording when
billed caching beats the simulated fix; `None` for kinds without a mechanical repair.

**Evidence.** `cb-moving-marker`, `cb-shared-prefix-sim`, `cb-concurrency`, `cb-key-order`, `cb-lookback`,
`cb-breaker-taxonomy`, `anth-invalidation-hierarchy`, `anth-cache-hidden-miss-causes`,
`projectdiscovery-cache-case` (volatile system content fix: hit rate 7% → 84%), `nondeterministic-tools`
(a sort fix cut cache creation from 56,296 to 32 tokens), `dynamic-system-injection`,
`history-trim-cache-breakers`, `replay-serialization-drift`.

**$ impact.** ProjectDiscovery-class fixes are 59–70% vs a no-cache counterfactual on affected services;
gateway/serialization fixes restore hit rates from ~0% to the 84–94% band (`nondeterministic-tools`).

**Acceptance tests (dual-engine agreement).** On the four v0.1 demo scenarios, via the trace@1 adapter,
the v2 engine finds exactly: `timestamp` → `block.volatile-system` at call 1; `tool-churn` →
`block.tool-churn` (order) at the rotation calls and no volatile-system false positive; `no-cache` →
`block.missing-breakpoint`; `well-behaved` → none; predicted reads agree with v1's optimal-cache reads
within rounding on `well-behaved`. Plus: the codebase track's exp1 (moving marker, 8 turns) → no
history-rewrite and agreement ≥ 0.95; exp2b-a3 (shared 20k-char preamble, 6 questions) → placement finding
worth ~56%; exp2b-a2 (5 identical same-timestamp requests) → no 65% phantom saving; exp9-1 (alternating key
order) → serialization churn; exp6-2 (25 blocks per turn) → lookback overflow. These experiments are
re-encoded as deterministic fixtures in `tests/blocksim/`.

### F14. Failure-path accounting (WP-D detectors; WP-A1/A3 capture; WP-P rules)

**What.** Attempt chains, the billing-rules table, and detectors: `fail.cold-retry` (API_ERROR event or
failed attempt followed by a successful attempt more than τ after the first attempt started, with a
rewrite ≥50% of the prefix: premium = prefix × (w − r)); `fail.retry-storm` (>3 attempts per logical
request, or retries at ≥2 layers); `fail.never-succeeding-400` (same error type and body hash ≥2 times,
or any retry after `x-should-retry:false` or a spend-cap 429); `fail.abort-waste` (aborted attempts with
partial usage, priced per billing rule, range when unknown); `fail.tool-error-loop` (≥3 consecutive
`is_error` tool results in `appended`). Fix texts: one retry owner, backoff capped below TTL minus
generation time, 1h TTL for watchdog/CI runs, streaming + keepalive, resume-from-partial, fallback credit.

**Evidence.** `fp-billing-matrix`, `fp-ttl-from-request-start` (a cold retry is 4.0× a successful call
with a 5m write, 6.0× with 1h, on a 150k Opus 5 prefix), `fp-sdk-retry-after-uncapped`,
`fp-cc-retry-semantics`, `fp-nested-retry-amplification` (~44 upstream attempts behind a retrying
gateway), `fp-incident-duration` (all 33 incidents >5 min, median 1.07 h), `fp-never-succeeding-400`,
`fp-local-failure-share` (0.7% direct, 3.1% upper bound on the healthy corpus), `fp-agent-loop-prevalence`.

**$ impact.** Small on healthy 1P fleets (≤3%), larger in fleets with gateways, NAT timeouts, nested
retries and 5m TTLs; mostly an incident-tax visibility feature.

**Acceptance tests.** Chain fixtures for each detector with hand-computed dollars; the cold-retry fixture
reproduces the 4.0×/6.0× ratios; an unknown billing rule produces an ESTIMATED range with "assumed"
labeling.

### F15. Outputs, CLI and the first-five-minutes experience (WP-O)

**What.** §2.12 and §2.13: `scan` (the default experience), `ingest`, `bill`, `reconcile`, `calibrate`,
`findings`, `whatif`, `policy`, `measure`, `receipt`, `export`, `pricing`, `check`, `report`, `purge`;
result@2 JSON, terminal, fleet/team/self HTML, FOCUS 1.4, SARIF, ccusage JSON; `demo --fleet`.

**Evidence.** `cb-report-scale`, `ent-focus-1-4`, `focus-15-ai-columns`, `finops-platforms-export-target`,
`ccusage-local-parity`, `ent-report-a11y-hardening`, `anth-admin-apis-org-scan` (first value in ~10
minutes), `ent-deployment-modes`.

**Acceptance tests.** `scan` on the fixture tree prints the exact bill, data quality and calibration lines;
result@2 validates against the schema in `tests/outputs/`; JSON contains no float for any money field;
`demo` and `analyze` (v1) golden outputs byte-identical to 0.1.2 except the version; HTML self-contained
(no external URLs), has CSP, and every chart has a table; FOCUS CSV has the required columns and `x_`
prefixes; exit codes as specified; `demo --fleet` runs keyless in < 60 s.

### F16. CI cost gate (WP-O command; WP-C2 detectors)

**What.** `tokenbill check`: on a recorded smoke-test trace or a ledger, assert cache-read share ≥ X after
turn N, no new block-level breaker compared with a baseline file, and cost per task within baseline ×
(1 + tolerance); SARIF for code scanning; a render-twice prefix-stability mode for harness repos.

**Evidence.** `ent-ci-cost-gate`, `practice-hit-rate-slo` (a 25-token status line took a run from $0.59 to
$4.24), `replay-serialization-drift` (a CI prefix invariant found ~4 legitimate busts in ~1,000
comparisons), `promptcachelint-overlap`, `anth-cache-health-thresholds` (median 84%, top decile ≥94%,
investigate below 80%).

**$ impact.** Prevents silent multi-month regressions (`cb-attribution-platform`).

**Acceptance tests.** The demo `timestamp` trace fails with `block.volatile-system` in SARIF; the
`well-behaved` trace passes; a baseline with the same breaker passes with `--fail-on regression`.

### F17. Supply-chain and release hardening; synthetic fleet demo (WP-Q)

**What.** SHA-pinned GitHub Actions with Dependabot, top-level `permissions: {}`, required reviewers on
the `pypi` environment, tag protection, zizmor and harden-runner (egress block); SOURCE_DATE_EPOCH
reproducible builds with a rebuild-and-diff job; CycloneDX SBOM embedded per PEP 770 and attached; build
provenance attestation (SLSA L2 → L3); PEP 740 attestations (already present); immutable releases;
OpenSSF Scorecard ≥ 8, CodeQL, fuzz targets (hypothesis) for every adapter parser; CI matrix
Linux/macOS/Windows × 3.10/3.12/3.13/3.14; coverage ≥ 90% on new packages; `docs/VERIFY.md`,
`docs/PRIVACY.md`, `SECURITY.md` with CVD timeline compatible with EU CRA reporting; a second maintainer
(recruitment is outside the build). `demo_fleet.py`: a seeded synthetic fleet (8 teams × 6 developers, 30
days) in source formats (Claude Code transcripts, OTLP file, usage/cost report pages) with planted waste:
5m TTL on API-key main lanes with 5–60 minute gaps, idle cold resumes, one gateway-stripped team, refusal
fallbacks without credit, a concurrent fan-out, sticky fast mode, Opus subagents, and a 15% contract
discount in the cost report.

**Evidence.** `ent-supply-chain-incidents`, `ent-provenance-sbom`, `ent-scorecard-osps`, `ent-eu-cra`,
`litellm-spend-and-supply-chain`, `cb-validation-ci`.

**Acceptance tests (flagship, `tests/e2e/test_fleet_demo_recovers_planted_waste.py`).** The full
pipeline on the synthetic fleet: every planted pattern is detected with cost within ±2% of its
construction; reconciliation is `reconciled` after `--suggest-contract` recovers the 15% discount; the
calibration gate passes; the TTL lever's Shapley credit is within ±5% of the constructed truth; a
simulated stepped-wedge rollout of the TTL lever yields a VERIFIED receipt that verifies; the run is
byte-deterministic across two processes with `--deterministic`.

---

## 4. Pricing and billing facts the build depends on

All values as verified by the research phase on **2026-09-23** (finding IDs in the last column). Builders
must re-verify every row against the primary source before transcribing it into `rates/data/*.json`,
record `verified_on`, and add the source URL to the row. Rows marked **VERIFY** were not fully confirmed
and must not ship as EXACT until checked (they may ship with `stacking: "assumed"` or as unpriced).

Primary sources (short names used below):
- **PRICING** https://platform.claude.com/docs/en/about-claude/pricing (also `.md`)
- **CACHING** https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- **DIAG** https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics
- **BATCH** https://platform.claude.com/docs/en/build-with-claude/batch-processing
- **COMPACT** https://platform.claude.com/docs/en/build-with-claude/compaction (+ `compaction-threshold`, `compaction-on-demand`)
- **EDIT** https://platform.claude.com/docs/en/build-with-claude/context-editing
- **FALLBACK** https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback and `/fallback-credit`
- **TOKCOUNT** https://platform.claude.com/docs/en/build-with-claude/token-counting
- **USAGEAPI** https://platform.claude.com/docs/en/manage-claude/usage-cost-api
- **CCAPI** https://platform.claude.com/docs/en/manage-claude/claude-code-analytics-api
- **EAAPI** https://platform.claude.com/docs/en/manage-claude/analytics-api
- **RATELIM** https://platform.claude.com/docs/en/api/rate-limits ; **ERRORS** https://platform.claude.com/docs/en/api/errors
- **CC-COSTS** https://code.claude.com/docs/en/costs ; **CC-CACHE** https://code.claude.com/docs/en/prompt-caching ;
  **CC-OTEL** https://code.claude.com/docs/en/monitoring-usage ; **CC-SETTINGS** https://code.claude.com/docs/en/settings-reference ;
  **CC-ENV** https://code.claude.com/docs/en/env-vars ; **CC-MODEL** https://code.claude.com/docs/en/model-config ;
  **CC-SMS** https://code.claude.com/docs/en/server-managed-settings ; **CC-GW** https://code.claude.com/docs/en/claude-apps-gateway-spend-limits
- **OAI-CACHE** https://developers.openai.com/api/docs/guides/prompt-caching ; **OAI-PRICING** https://developers.openai.com/api/docs/pricing ;
  **OAI-FAST** https://developers.openai.com/api/docs/guides/fast-mode ; **OAI-FLEX** https://developers.openai.com/api/docs/guides/flex-processing
- **BR-CACHE** https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html ; **BR-PRICE** https://aws.amazon.com/bedrock/pricing/ and the Price List offer file `https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrockFoundationModels/current/us-east-1/index.json`
- **VX-PRICE** https://cloud.google.com/vertex-ai/generative-ai/pricing
- **OTEL-GENAI** https://github.com/open-telemetry/semantic-conventions-genai (`docs/gen-ai/anthropic.md`, `gen-ai-spans.md`, `gen-ai-token-metrics.md`)

### 4.1 Anthropic first-party model rates (USD per million tokens)

| Model id | Input | Output | Cache read (mult.) | 5m write (1.25×) | 1h write (2×) | Min cacheable | Tokenizer | Effective / notes | Findings |
|---|---|---|---|---|---|---|---|---|---|
| claude-opus-5-5 | 4.00 | 20.00 | 0.20 (0.05×) | 5.00 | 8.00 | 512 | 4.7+ | from 2026-09-22 (launch; −20% list, −60% reads vs Opus 5); default effort medium | `anth-pricing-table-2026-09`, `anth-evidence-drift` |
| claude-fable-5-1, claude-mythos-5-1 | 10.00 | 50.00 | 0.25 (0.025×) | 12.50 | 20.00 | 512 | 4.7+ | 0.025× reads from 2026-09-01 (**VERIFY** launch date and any earlier read price) | `anth-pricing-table-2026-09`, `cc-same-tier-upgrade` |
| claude-fable-5, claude-mythos-5 | 10.00 | 50.00 | 1.00 (0.1×) | 12.50 | 20.00 | 512 | 4.7+ | | `anth-pricing-table-2026-09` |
| claude-opus-5 | 5.00 | 25.00 | 0.50 | 6.25 | 10.00 | 512 | 4.7+ | released 2026-07-24; thinking on by default | `anth-pricing-table-2026-09`, `opus5-thinking-effort-rebaseline` |
| claude-opus-4-8 | 5.00 | 25.00 | 0.50 | 6.25 | 10.00 | 1024 | 4.7+ | | same |
| claude-opus-4-7 | 5.00 | 25.00 | 0.50 | 6.25 | 10.00 | 2048 (Bedrock 4096) | 4.7+ | default effort xhigh | `anth-cache-hidden-miss-causes`, `bedrock-cache-semantics` |
| claude-opus-4-6 | 5.00 | 25.00 | 0.50 | 6.25 | 10.00 | 4096 | pre-4.7 | | same |
| claude-opus-4-5 | 5.00 | 25.00 | 0.50 | 6.25 | 10.00 | 4096 | pre-4.7 | retirement floor 2026-11-24 | `cc-timing-calendar` |
| claude-opus-4-1, claude-opus-4 | 15.00 | 75.00 | 1.50 | 18.75 | 30.00 | **VERIFY** | pre-4.7 | retired on 1P; still billed on partner clouds | `anth-pricing-table-2026-09`, `cc-timing-calendar` |
| claude-sonnet-5 | 2.00 | 10.00 | 0.20 | 2.50 | 4.00 | 1024 | 4.7+ | launch rate made permanent (increase cancelled; locked 2026-08-10) | `anth-pricing-table-2026-09` |
| claude-sonnet-4-6 | 3.00 | 15.00 | 0.30 | 3.75 | 6.00 | 1024 | pre-4.7 | | same |
| claude-sonnet-4-5 | 3.00 | 15.00 | 0.30 | 3.75 | 6.00 | 1024 | pre-4.7 | retirement floor 2026-09-29 | same |
| claude-haiku-4-5 | 1.00 | 5.00 | 0.10 | 1.25 | 2.00 | 4096 | pre-4.7 | retirement floor 2026-10-15; strips earlier thinking blocks on a plain user turn | same, `anth-cache-hidden-miss-causes` |
| claude-haiku-3-5 | **VERIFY** | **VERIFY** | **VERIFY** | | | 2048 | pre-4.7 | cache reads count toward ITPM (exception) | `anth-cache-rate-limits` |
| claude-sonnet-5-5, claude-haiku-5-5 | announced 2026-09-22, not priced | | | | | | | add at launch; `pricing verify` must flag | `anth-pricing-table-2026-09` open question |

Tokenizer: Claude 4.7+ (Opus 4.7/4.8/5/5.5, Sonnet 5, Fable, Mythos) produces ~30% more tokens for the
same text (1.0–1.35× by content; English technical docs up to 1.47×, CJK ~1.01×) (`anth-tokenizer-inflation`,
`tokenizer-inflation-47plus`). Same-tier pairs with the same tokenizer: Fable 5→5.1, Opus 5→5.5.

### 4.2 Anthropic modifiers and extra charges

| Fact | Value | Source | Findings |
|---|---|---|---|
| Batch discount | 0.5× on every token category including cache reads and writes; stacks with caching and data residency; not for fast mode, fallbacks, `max_tokens` 0, Managed Agents; ≤100k requests or 256 MB; 24h expiry; results kept 29 days; batch cache hits best effort 30–98% | BATCH | `anth-batch-stacking`, `batch-plus-cache` |
| US data residency | `inference_geo: "us"` → 1.1× on every token category, Claude 4.6+ models, 1P API and Claude Platform on AWS; Foundry US Data Zone equivalent | PRICING | `anth-modifiers-geo-fast-priority`, `claude-marketplace-ccu` |
| Regional cloud endpoints | Bedrock and Vertex regional / multi-region endpoints +10% vs global (Vertex: Sonnet 4.5, Haiku 4.5, Opus 4.5 and later) | PRICING, BR-PRICE, VX-PRICE | `anth-modifiers-geo-fast-priority`, `vertex-claude-geo-labels`, `bedrock-price-list-api` |
| Fast mode (1P only) | Opus 5.5 $8/$40; Opus 5 and Opus 4.8 $10/$50; cache multipliers apply on top; up to 2.5× output speed; does not share cache with standard speed; Claude Code: only the first enable breaks the cache | PRICING, CC-CACHE | `anth-modifiers-geo-fast-priority`, `premium-modifiers`, `anth-claude-code-cost-physics` |
| Stacking | Docs say modifiers stack; exact order and rounding in every combination unverified against invoices → engine uses exact multiplication (order-independent), rows marked `stacking: assumed` where not documented | PRICING | research track `papers-measurement`, open question — **VERIFY** |
| Priority Tier | no longer sold; excludes Opus 5.5, Opus 5, Sonnet 5, Fable 5.1, Mythos; existing commitments burn down at read 0.1, 5m 1.25, 1h 2.0, US geo 1.1; excluded from `cost_report`; `usage.service_tier = "priority"` | PRICING, USAGEAPI | `anth-modifiers-geo-fast-priority`, `cc-priority-legacy`, `cc-capacity-model-lag` |
| Web search | $10 per 1,000 searches plus result tokens; failed searches not billed | PRICING | `anth-web-search-controls`, `fp-billing-matrix` |
| Web fetch | tokens only | PRICING | `anth-web-search-controls` |
| Code execution | free with `web_search`/`web_fetch` 20260209+; otherwise 1,550 free hours per org per month, then $0.05 per container-hour, 5-minute minimum; appears only in `cost_report` | PRICING | `anth-ptc-code-exec`, `anth-usage-schema-exactness` |
| Managed Agents | $0.08 per running session-hour; no batch; session budgets pause with `budget_reached` | PRICING | `anth-governance-controls` (corrected) |
| Claude Platform on AWS, Foundry | billed in CCU at $0.01 each; discounts applied as fewer CCU; single hourly line; no usage/cost API on Claude Platform on AWS; spend caps computed at list | PRICING, marketplace docs | `claude-marketplace-ccu`, `cc-ccu-private-offers` |
| Refusals | pre-output decline not billed (counts toward rate limits); mid-stream decline bills input + streamed output; `usage.iterations` is the per-attempt billing record | FALLBACK | `fp-anth-refusal-iterations`, `cc-iterations-fallback` (corrected) |
| Fallback credit | beta `fallback-credit-2026-07-01`; retry within 5 minutes with an exactly matching body reprices the cached span as reads (e.g. 150k-token Opus 4.8 prefix $0.9375 → $0.075) | FALLBACK | `fp-fallback-credit`, `anth-governance-controls` |
| Compaction billing | threshold `compact-2026-01-12` (default trigger 150,000, minimum 50,000) and on-demand `compact-2026-09-04`; top-level input/output exclude the compaction iteration; billed total = Σ `usage.iterations` | COMPACT | `anth-compaction-iterations-billing`, `compaction-iterations-p0` |
| Advisor | top-level usage is executor-only; `advisor_message` iterations bill at the advisor model's rates | pricing / advisor docs | `advisor-iterations-undercount` |
| Context editing | `clear_tool_uses_20250919` default trigger 100,000 input tokens, keep 3 tool uses, optional `clear_at_least`; `applied_edits[].cleared_input_tokens`; each clear invalidates the prefix from the clear point | EDIT | `anth-context-editing-not-savings`, `history-rewrite-kstar` |
| Rate limits | cache reads do not count toward ITPM (except Haiku 3.5); cache writes do; OTPM counts actual output; spend-cap 429 has no `retry-after` and is terminal | RATELIM | `anth-cache-rate-limits`, `fp-ratelimit-feedback` |
| Failure modes not documented | client aborts, mid-stream `overloaded_error`, pre-token 429/529 billing on 1P | — | `fp-billing-matrix` → `unknown` rows (ranges) |

### 4.3 Cache mechanics (simulator constants)

| Fact | Value | Source | Findings |
|---|---|---|---|
| TTLs | 5 minutes (default) and 1 hour; writes 1.25× / 2×; reads 0.1× except Opus 5.5 0.05×, Fable/Mythos 5.1 0.025× | CACHING, PRICING | `anth-cache-1h-2x`, `anth-ttl-start-1h` |
| TTL clock | measured from the start of the request that writes or reads; generation time counts; reads refresh at no cost | CACHING | `anth-ttl-start-1h`, `fp-ttl-from-request-start` |
| Visibility | an entry is readable only after the first response begins streaming (parallel same-prefix requests all pay) | CACHING | `anth-concurrency-fanout` |
| Lookback | each breakpoint checks at most 20 positions back; a run of `tool_use` blocks counts as one position and so does a run of `tool_result` blocks (1P) | CACHING | `anth-lookback-20` (corrected) |
| Breakpoints | at most 4; automatic (top-level) caching uses one slot; longer TTLs must precede shorter; usage splits writes into `ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens` | CACHING | `anth-ttl-start-1h`, provider-anthropic A8 |
| Isolation | per workspace on 1P, Claude Platform on AWS, Foundry; per organization on Bedrock and Vertex; Claude Code prefixes are effectively per machine and directory | CACHING, CC-CACHE | `anth-workspace-fleet-scope`, `cache-isolation-gateways` |
| Invalidation hierarchy | tool definitions or model → all tiers; speed, web-search or citations toggles, system → system + messages; `tool_choice`, `disable_parallel_tool_use`, image add/remove → messages; thinking or `output_config.effort` → messages (on some models tools and system too) | CACHING | `anth-invalidation-hierarchy` |
| Server tools | auto-insert 5m cache writes after tool results (not breakers) | CACHING | `anth-web-search-controls` |
| Keep-alive | re-send with `max_tokens: 0` within 4 minutes of the previous request start and every 4 minutes; bills a cache read; rejected with `stream: true`, structured outputs, forced `tool_choice`, inside batches | CACHING / cost guide | `anth-ttl-choice-keepalive`, `keepalive-economics` |
| TTL choice rule | 1h wins once ~1 turn in 30 follows a 5–60 minute pause (Anthropic recommends 1-in-20 for margin); with no pauses 5m is 11–15% cheaper | cost guide | `anth-ttl-choice-keepalive`, `anth-fleet-benchmarks` |
| Health benchmarks | agent loops read a median 84% of input from cache; top decile ≥94%; investigate below 80%; Claude Code counts a miss when >5% and ≥2,000 tokens are reprocessed | cost guide, CC-COSTS | `anth-cache-health-thresholds`, `cache-health-benchmarks-diagnostics` |
| Diagnostics | header `cache-diagnosis-2026-04-07` on every request plus `diagnostics.previous_message_id`; `cache_miss_reason.type` ∈ {model_changed, system_changed, tools_changed, messages_changed, previous_message_not_found, unavailable}; `cache_missed_input_tokens` is a byte-derived magnitude, not billing; first divergence only; Claude API only; hash-only fingerprints, short-lived, same-workspace; ZDR-eligible except Covered Models (Fable, Mythos 5.x) | DIAG | `anth-cache-diagnostics-beta` (corrected), `competitors/anth-cache-diagnostics` |
| Cache-preserving APIs | mid-conversation `role: system` messages (GA; Opus 5/5.5/4.8, Fable, Mythos; **not Sonnet 5**); `tool_addition`/`tool_removal` beta `mid-conversation-tool-changes-2026-07-01`; inline tools beta `inline-tools-2026-09-15` (1P only); per-message effort beta `mid-conversation-output-config-2026-07-01` (Fable 5.1, Mythos 5.1, Opus 5, Opus 5.5); turn-scoped reminders with `clear_at` | CACHING | `anth-cache-preserving-apis`, `mid-conv-system-tool-addition-support` |
| Hidden overhead tokens | tool-use system prompt 286 (Opus 5.5 auto/none), 354/474 (Sonnet 5), 675/804 (Opus 4.7); bash 244–325; text editor ~700; computer use ~4,500; browser ~6,600 | PRICING / tool-use docs | `anth-hidden-overhead-tokens` |
| Images | `ceil(w/28)·ceil(h/28)` tokens; standard tier long edge 1,568 px, ≤1,568 tokens; 4.7+ high-res long edge 2,576 px, ≤4,784 tokens | vision docs | `anth-vision-tokens`, `images-thinking-estimation` |
| count_tokens | free, separate RPM limits 5k/10k/20k by tier; an estimate; may include unbilled system tokens; ignores caching; rejects server tools and URL/file sources | TOKCOUNT | `count-tokens-exact-attribution` (corrected) |

### 4.4 Field names adapters depend on

| Source | Fields | Findings |
|---|---|---|
| Anthropic response `usage` | `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `cache_creation.{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}`, `output_tokens`, `output_tokens_details.thinking_tokens`, `server_tool_use.{web_search_requests, web_fetch_requests}`, `service_tier`, `inference_geo`, `speed`, `iterations[].{type, model, input_tokens, cache_read_input_tokens, cache_creation_input_tokens, cache_creation.*, output_tokens}`; response `diagnostics.cache_miss_reason.{type, cache_missed_input_tokens}`; `context_management.applied_edits` | `anth-usage-schema-exactness`, empirical `02_schema.out` |
| Claude Code transcripts | `type` ∈ {user, assistant, system, attachment, cost-state}; `uuid`, `parentUuid`, `sessionId`, `requestId`, `timestamp`, `isSidechain`, `version`, `entrypoint`, `effort`, `attributionSkill/McpServer/McpTool/Plugin/Agent`; `message.{id, model, usage, stop_reason, content[], diagnostics, input_transformations}`; `system.subtype` ∈ {compact_boundary (`compactMetadata.{preTokens, postTokens, durationMs, trigger}`), model_refusal_fallback (`originalModel`, `fallbackModel`), …}; `toolUseResult.{totalTokens, usage, agentId}` (rollups, **not spend**); subagent `meta.json` {agentType, model, parentAgentId, toolUseId, spawnDepth}; files `~/.claude/projects/<slug>/<session>.jsonl`, `<session>/subagents/agent-*.jsonl`, workflow agents under `/workflows/`; default retention `cleanupPeriodDays` 30; format is internal and changes between versions | `cc-transcript-format`, `cc-import-dedup-message-id`, empirical `03`, `04`, `12` |
| Claude Code OTel | metrics `claude_code.token.usage{type=input|output|cacheRead|cacheCreation}`, `claude_code.cost.usage`; event `claude_code.api_request` {model, request_id, client_request_id, duration_ms, input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens, cost_usd, speed, effort, query_source}; `claude_code.api_error`; `tool_result` {tool_result_size_bytes}; beta `llm_request` span {ttft_ms}; tool spans {result_tokens, agent_id, parent_agent_id}; attributes `session.id`, `user.id`, `user.email` (not populated for API key, Bedrock, Vertex, Foundry), `agent.name`, `skill.name`, `plugin.name`, `mcp_server.name`; `OTEL_RESOURCE_ATTRIBUTES`; opt-in `vcs.*` (`OTEL_METRICS_INCLUDE_REPOSITORY`, ≥2.1.269), `app.entrypoint` (`OTEL_METRICS_INCLUDE_ENTRYPOINT`); raw bodies via `OTEL_LOG_RAW_API_BODIES` (inline truncated at 60 KB; file mode untruncated) | CC-OTEL; `cc-otel-schema` (corrected), `anth-claude-code-analytics-otel` (corrected), `detect-signatures` |
| Gateway hint headers | `x-claude-code-session-id`, `x-claude-code-agent-id`, parent agent id; with `CLAUDE_CODE_GATEWAY_HINT_HEADERS=1`: request class (main/subagent/workflow/compaction/auxiliary), agent type | CC gateway docs | `cc-gateway-attribution-caps` |
| Usage report | `GET /v1/organizations/usage_report/messages`: buckets `1m` (≤1,440), `1h` (≤168), `1d` (≤31); filter/`group_by` api_key_ids, workspace_ids, models, service_tiers, context_window, inference_geos, speeds (beta); uncached input, cache read, `cache_creation` 5m/1h, output, `server_tool_use.web_search_requests`; ~5 min latency; poll ≤ once per minute; Admin key (`sk-ant-admin…`) or `org:admin` OAuth; not on Claude Platform on AWS | USAGEAPI | `anth-admin-usage-cost-api`, `ent-anthropic-admin-apis` |
| Cost report | `GET /v1/organizations/cost_report`: daily; USD as decimal strings in cents; `group_by` workspace_id or description (parsed model, cost_type, token_type, inference_geo); Priority Tier excluded; code execution only here; default workspace `workspace_id = null` | USAGEAPI | same; **VERIFY** exact `cost_type`/`token_type` enumerations from a real page |
| Claude Code Analytics | `GET /v1/organizations/usage_report/claude_code?starting_at=YYYY-MM-DD`: per user per day sessions, LOC, commits, PRs, tool accept/reject, `model_breakdown[].tokens.{input,output,cache_read,cache_creation}`, `estimated_cost.amount` (cents); ~1h delay; `limit` ≤1000; excludes Bedrock/Vertex/Foundry/Claude Platform on AWS | CCAPI | `cc-admin-analytics-apis` (corrected) |
| Enterprise Analytics | `/v1/organizations/analytics/{usage_report,user_usage_report,cost_report,user_cost_report}`; `read:analytics`; products claude_code, cowork, chat; `group_by` product, model, context_window, speed, inference_geo, rbac_group_id (cost endpoints add cost_type, token_type); buckets 1m/1h/1d; data from 2026-01-01; ≤31 days per query; `amount` (post-discount, pre-credit) and `list_amount` in cents decimal strings ("avoid binary floating-point parsing"); typically 4h, up to 24h; revisable 30 days; 60 rpm; seat-based plans show usage credits only | EAAPI | `anth-claude-code-analytics-otel`, `cc-anthropic-discount-visibility`, `ent-anthropic-admin-apis` |
| OpenAI usage | Responses `usage.input_tokens` (inclusive), `input_tokens_details.{cached_tokens, cache_write_tokens}`, `output_tokens`, `output_tokens_details.reasoning_tokens`, `service_tier` (served tier); Chat `prompt_tokens_details.cached_tokens`, `completion_tokens_details.{reasoning_tokens, accepted_prediction_tokens, rejected_prediction_tokens}`; Admin `organization.usage.completions` buckets {input_cached_tokens, input_cache_write_tokens, input_uncached_tokens, output_tokens} grouped by project_id, user_id, api_key_id, model, batch, service_tier; Costs grouped by project_id, line_item, api_key_id | OAI-CACHE, OAI-PRICING | `oai-usage-inclusive`, `oai-admin-usage-costs` |
| OTel GenAI | `gen_ai.usage.input_tokens` (includes cache), `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_write.input_tokens` (≥1.42; legacy `cache_creation`), `gen_ai.usage.output_tokens`, `gen_ai.usage.reasoning.output_tokens`, `gen_ai.provider.name`, `gen_ai.response.finish_reasons`, `error.type`; counters `gen_ai.client.inference.usage.*` (require `gen_ai.token.modality`); status Development | OTEL-GENAI | `otel-genai-semconv`, `ent-otel-genai-semconv` (corrected) |
| Bedrock Converse | `usage.{inputTokens, outputTokens, cacheReadInputTokens, cacheWriteInputTokens, cacheDetails[{ttl, inputTokens}]}`; invocation logs `identity.arn`, `requestMetadata`, cache counts only inside the logged response body; bedrock-mantle calls not logged | BR-CACHE | `bedrock-cache-semantics`, `bedrock-attribution` |
| Codex | `TokenUsage {input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens, reasoning_output_tokens, total_tokens}`; `TokenUsageRecord` links response/turn/thread/session; `service_tier` on session/turn settings, not on usage records | codex `protocol.rs` | `codex-usage-format` (corrected) — inclusive/exclusive **VERIFY** with a fixture |

### 4.5 Claude Code defaults and managed-settings keys (policy pack)

| Key / default | Fact | Source | Findings |
|---|---|---|---|
| Cache TTL default | 5m on API keys, cloud providers and usage credits; 1h for the main conversation on a subscription within plan; subagents, forks and compaction 5m; the Claude apps gateway cannot use 1h | CC-CACHE | `cc-ttl-policy`, `cc-ttl-billing-path`, `cc-apps-gateway-routing-tax` |
| `promptCacheTtl`, `subagentPromptCacheTtl`, `CLAUDE_CODE_PROMPT_CACHE_TTL`, `ENABLE_PROMPT_CACHING_1H`, `FORCE_PROMPT_CACHING_5M` | TTL overrides, managed-settings capable; through a gateway the 1h TTL needs the `anthropic-beta` header forwarded | CC-SETTINGS, CC-ENV | `cc-ttl-managed-settings` |
| Auto-compaction | ~967K default window on 1M-context models (Sonnet 5, Fable, Opus 4.7+ on the API); `autoCompactWindow` 100,000–1,000,000; `CLAUDE_CODE_AUTO_COMPACT_WINDOW`; `CLAUDE_CODE_DISABLE_1M_CONTEXT`; the `--autocompact` flag is not preempted by managed settings (enforce via managed env) ; no long-context price premium | CC-MODEL | `cc-autocompact-window` (corrected) |
| `modelPricing` | managed-only, ≥2.1.242 (markup ≥2.1.271); multiplier and per-model overrides for input/output/cacheRead/cacheWrite; `cacheWrite` covers both 5m and 1h writes; without it every Claude Code dollar figure is a list estimate | CC-SETTINGS, CC-COSTS | `cc-list-price-estimates`, `ent-reconciliation` — **VERIFY** exact JSON shape |
| `maxEffortLevel` | ≥2.1.267, works on every provider | CC-SETTINGS | `cc-managed-settings-levers` |
| Models | `availableModels` + `enforceAvailableModels`, `ANTHROPIC_DEFAULT_*`, `CLAUDE_CODE_SUBAGENT_MODEL`; Explore inherits the session model since 2.1.198 (capped at Opus on the API) | CC-SETTINGS, CC-MODEL | `cc-managed-settings-levers`, `plan-execute-subagents` |
| Fast mode | `fastModePerSessionOptIn`, `CLAUDE_CODE_DISABLE_FAST_MODE`; `/effort` Enter saves the level as default; fast mode persists across sessions without the opt-in setting | CC-SETTINGS | `cc-sticky-escalation` |
| Context hygiene | `bashOutputMaxChars` (default 30,000), `MAX_MCP_OUTPUT_TOKENS` (warn 10K, cap 25K), `skillListingBudgetFraction`, `claudeMdExcludes`, `ENABLE_TOOL_SEARCH`, `workflowSizeGuideline` (advisory only) | CC-SETTINGS, CC-ENV | `cc-tool-output-bloat`, `cc-managed-settings-levers` (corrected) |
| Retention / version | `cleanupPeriodDays` (default 30), `requiredMinimumVersion`, `companyAnnouncements`, `spinnerTipsOverride` | CC-SETTINGS | `ent-local-transcripts`, `education-channels` |
| Delivery | server-managed settings are polled hourly and apply uniformly org-wide (no per-group); MDM every 30 minutes; files reloaded on change; the self-hosted Claude apps gateway delivers settings per IdP group | CC-SMS | `stepped-wedge-mdm` (corrected) |
| Baselines | ~$13 per developer per active day; $150–250 per developer per month; 90% of users under $30 per active day | CC-COSTS | `cc-enterprise-baseline` |

### 4.6 OpenAI, cloud channels and other providers

| Fact | Value | Source | Findings |
|---|---|---|---|
| GPT-5.6+ caching | released 2026-07-09; min 1,024 visible tokens; writes 1.25×, reads 0.1×; `prompt_cache_options.ttl = "30m"` only; free refresh on reuse; implicit mode 1 automatic + ≤3 explicit breakpoints; explicit mode ≤4; explicit with no breakpoints disables caching; matching considers the first 2 and latest 50 explicit breakpoints | OAI-CACHE | `oai-56-explicit-cache` (corrected) |
| gpt-5.6-sol | $4 input / $0.40 cached / $5 cache write / $20 output; >272K long-context rows $8 / $0.80 / $10 / $30 (whole request) | OAI-PRICING | `oai-batch-longctx-residency`, `price-feed-crosscheck` (OpenRouter lists $2/$10: non-authoritative) |
| OpenAI tiers | flex = batch rates (0.5×), flex 429s not charged; fast (renamed from priority 2026-07-30) 2× ($8/$40 for 5.6 Sol); price at the **served** tier in `service_tier` | OAI-FAST, OAI-FLEX | `oai-service-tiers` |
| OpenAI residency | regional processing +10% for models released on or after 2026-03-05 | OAI-PRICING | `oai-batch-longctx-residency` |
| OpenAI pre-5.6 | no write fee; `cached_tokens` rounded down to 128; retention `in_memory` (5–10 min idle, ≤1h) vs `24h` (default for non-ZDR orgs since 2026-05-29); ~15 RPM per `prompt_cache_key` | OAI-CACHE | `oai-retention-defaults` (corrected) |
| OpenAI diagnostics | `prompt_cache_options.comparison_response_id` → `prompt_cache_diagnostics` (nine miss reasons), Responses API 5.6+, free | OAI-CACHE | `oai-cache-diagnostics` |
| Bedrock | Converse `inputTokens` excludes cache; checkpoint minimums 512 (Opus 5/5.5), 1,024 (Sonnet 5, Opus 4.8), 4,096 (Opus 4.7, Haiku 4.5); no caching in Bedrock batch; cross-region inference may increase cache writes; tiers reserved / priority 1.75× / default / flex 0.5×, served tier in the response and CloudWatch `ResolvedServiceTier`; Price List example Opus 5 input $5.00 global vs $5.50 regional, read $0.50 vs $0.55, 1h write $10 vs $11, batch input $2.50 | BR-CACHE, BR-PRICE | `bedrock-cache-semantics`, `bedrock-price-list-api`, `bedrock-service-tiers` |
| Bedrock id prefixes | `global.` → global rate; in-region and geo-prefixed inference profiles → regional (+10%) | BR-PRICE | **VERIFY** per profile against Price List `usagetype` |
| Vertex | regional/multi-region +10%; model id in the URL; `anthropic_version: vertex-2023-10-16`; no Message Batches for Claude and no Usage/Cost API; labels flow to the billing export; Opus 5.5 batch listed $2.50/$12.50 vs Anthropic $2/$10 (possible documentation lag) | VX-PRICE | `vertex-claude-geo-labels`, `cc-channel-divergence` — **VERIFY** batch |
| Azure | GPT-5.4 and older default `in_memory`; no cache sharing across subscriptions; PTU-M lacks breakpoints; Data Zone +10% (secondary source) | Azure docs | `azure-openai-caching` (corrected), `azure-deployment-costs` — **VERIFY** |
| Gemini (not in v0.2 rates) | `promptTokenCount` includes cached; thoughts billed as output; cached input 10% of input; 3.1 Pro $2.00 ≤200K / $4.00 above; 3.8 Flash $0.75 through 2026-12-31 then $1.50 | Gemini pricing | `gemini-usage-semantics`, `gemini-effective-dated-prices`, `gemini-bedrock-deepseek-rules` (corrected) |
| xAI (not in v0.2 rates) | `usage.cost_in_usd_ticks` (1 USD = 10¹⁰ ticks) is billed truth; ≥200K doubles all rates | xAI docs | `xai-cost-ticks` |
| Cursor / Copilot (not in v0.2) | Cursor $0.25/MTok fee on third-party models incl. cached and BYOK; Copilot AI credits (Business $19 → 1,900; Enterprise $39 → 3,900), `ai_credits_used` is not a billed total | vendor docs | `cc-cursor-token-rate`, `cc-copilot-credits` |

### 4.7 Measurement and integrity standards

| Fact | Value | Source | Findings |
|---|---|---|---|
| Calibration thresholds | ASHRAE Guideline 14 via FEMP M&V Guidelines 4.0 Table 4-2: monthly NMBE ±5%, CV(RMSE) ≤15%; hourly ±10%, ≤30% | https://www.energy.gov/sites/default/files/2016/01/f28/mv_guide_4_0.pdf | `ashrae-calibration-gate` (corrected citation: G14-2015 §5.3.3.3.10) |
| Savings definition | savings = adjusted baseline − post (± routine and non-routine adjustments); invoice reconciliation ≈ IPMVP Option C, calibrated replay ≈ Option D | FEMP M&V 4.0 | `mv-adjusted-baseline` |
| Estimators | Callaway–Sant'Anna / imputation for staggered adoption; never TWFE or naive pre/post; CUPED variance factor (1−ρ²) | literature | `staggered-did`, `cuped` |
| DSSE | PAE = `"DSSEv1" SP len(type) SP type SP len(body) SP body` | https://github.com/secure-systems-lab/dsse/blob/master/protocol.md | `signed-receipts` |
| Canonical JSON | RFC 8785 (JCS); Token Bill's integer-only, ASCII-key subset is byte-identical to JCS | https://www.rfc-editor.org/rfc/rfc8785 | `signed-receipts` |
| Signing | `ssh-keygen -Y sign -f KEY -n NAMESPACE FILE`; `ssh-keygen -Y verify -f allowed_signers -I IDENTITY -n NAMESPACE -s FILE.sig < FILE` (OpenSSH ≥8.1) | https://man.openbsd.org/ssh-keygen.1 | `signed-receipts` (demo verified with OpenSSH 10.3; 1-byte tamper fails) |
| in-toto | Statement has `subject`; the receipt is in-toto-shaped, not a conforming Statement v1 | https://github.com/in-toto/attestation/blob/main/spec/README.md | `signed-receipts` (corrected) |
| FOCUS | 1.4 ratified 2026-06-04 (2 datasets, 47 columns); custom columns MUST use `x_`; ServiceCategory includes "AI and Machine Learning"; 1.5 (target 2026-12-03) adds model identity in `SkuPriceDetails`, tokens via `ConsumedQuantity`/`ConsumedUnit` | https://focus.finops.org/focus-specification/ | `ent-focus-1-4` (corrected), `focus-15-ai-columns` |
| Supply chain | PEP 740 attestations (on by default in `gh-action-pypi-publish` ≥1.11), PEP 770 SBOM location `.dist-info/sboms`, SLSA 1.2, GitHub immutable releases GA 2025-10-28, EU CRA manufacturer reporting since 2026-09-11 | PEPs, SLSA, GitHub | `ent-provenance-sbom`, `ent-eu-cra` |
| Pseudonymization | pseudonymized data remains personal data; use keyed MACs held by the controller | EDPB Guidelines 01/2025 | `ent-privacy-by-default` |

### 4.8 Verify-before-build checklist (owned by WP-P and the adapter WPs)

1. Every §4.1 row against PRICING on the day of transcription; add Haiku 3.5 and Opus 4.1/4 minimums or
   leave them unpriced with a reason.
2. Fable 5.1 launch/effective date and any earlier read price.
3. Bedrock inference-profile prefixes → global vs regional rate, from Price List `usagetype`.
4. Modifier stacking combinations against a real invoice (reconciliation will surface errors; ship
   `stacking: assumed` where undocumented).
5. `modelPricing` exact JSON schema.
6. `cost_report` `cost_type`/`token_type` enumerations and Enterprise Analytics field names from recorded
   pages; build `COST_TYPE_MAP` from them.
7. Claude Code Analytics token convention (exclusive vs inclusive) with a fixture.
8. Codex rollout convention with a fixture before enabling pricing.
9. OTel attribute names at the enterprise's Claude Code version (`claude_code.api_request`).
10. Vertex Opus 5.5 batch rate; Azure Data Zone premium.
11. FOCUS 1.4 column names and required-ness.

---

## 5. Work-package partition for parallel builders

### 5.1 Rules of engagement

- **Phases.** Phase 0: WP-0 alone. Phase 1: WP-P, WP-A1, WP-A2, WP-A3, WP-S, WP-R, WP-C1, WP-V in
  parallel. Phase 2: WP-C2, WP-D, WP-O, WP-Q in parallel. Phase 3: integration and adversarial review.
- **Disjoint ownership.** `OWNERSHIP.toml` (repo root, WP-0) lists every file glob per WP per phase;
  `scripts/check_ownership.py` (WP-0) runs in CI and fails a WP branch that touches anything else.
  Frozen files (§2.2) belong to no WP. New test directories and fixture directories are per WP.
- **Frozen contracts.** Everything in `tokenbill/contracts/`, `ledger/`, `money.py`, `labels.py` is frozen
  at the end of phase 0. A change needs a written contract-change request, applied by the WP-0 owner
  between phases, never mid-phase.
- **Conformance suite.** `tokenbill/testing.py` provides `assert_pricer_conforms(pricer)`,
  `assert_adapter_conforms(adapter, fixture)`, `assert_store_conforms(factory)`,
  `assert_replayer_conforms(replayer)`, `assert_detector_conforms(detector)`; every implementing WP calls
  them from its own tests. Fakes (`FakePricer`, `MemoryStore`, `FakeReplayer`, builders) let each WP test
  without the others.
- **Always green.** Every WP keeps the 207 existing tests green, adds no runtime dependency, uses no
  network in tests (a socket-guard fixture in `tests/conftest.py` fails any connection attempt), and
  passes `ruff` and the no-float-money lint (`tests/foundation/test_no_float_money.py`: AST scan of
  `money.py`, `rates/`, `reconcile.py`, `verify/receipts.py` for `float(` calls and float literals).
- **Definition of done** for every WP: owned modules have full type hints and docstrings; ≥90% line
  coverage of owned modules; hypothesis fuzz tests for every parser; deterministic outputs; the
  acceptance tests listed below; a short entry in the WP's test directory `README` (not a report) listing
  fixtures and their provenance.

### 5.2 Package table

| WP | Phase | Owns (files) | Consumes | LOC (code + tests) |
|---|---|---|---|---|
| **WP-0 Foundation** | 0 | `money.py`, `labels.py`, `jsonable.py`, `secrets.py`, `textsafe.py`, `testing.py`, `ledger/*`, `contracts/*`, `adapters/base.py`, `adapters/conventions.py` (registry + `anthropic.messages` incl. iterations rule), `detect/base.py`, every `__init__.py`, `tests/conftest.py`, `tests/foundation/*`, `OWNERSHIP.toml`, `scripts/check_ownership.py`, `pyproject.toml` (phase 0: `--import-mode=importlib`, dev deps `hypothesis`) | — | 2,300 |
| **WP-P Pricing** | 1 | `rates/*` (incl. `data/*.json`, `lifecycle.json`, `billing_rules.json`), `pricing.py` (shim), `tests/rates/*`, `tests/fixtures/rates/*` | WP-0 | 2,700 |
| **WP-A1 Traces & recorder** | 1 | `adapters/trace_v1.py`, `adapters/trace_v2.py`, `fingerprint.py`, `instrument.py`, `tests/adapters/trace/*`, `tests/fixtures/trace/*` | WP-0 | 2,600 |
| **WP-A2 Claude Code** | 1 | `adapters/claude_code.py`, `tests/adapters/claude_code/*`, `tests/fixtures/claude_code/*` | WP-0 | 2,100 |
| **WP-A3 Telemetry adapters** | 1 | `adapters/conventions_ext.py`, `adapters/otel.py`, `adapters/openai.py`, `adapters/bedrock.py`, `tests/adapters/telemetry/*`, `tests/fixtures/telemetry/*` | WP-0 | 2,400 |
| **WP-S Store & privacy** | 1 | `store.py`, `privacy.py`, `tests/store/*` | WP-0 | 2,100 |
| **WP-R Reconciliation** | 1 | `reconcile.py`, `adapters/anthropic_admin.py`, `adapters/openai_admin.py`, `tests/reconcile/*`, `tests/adapters/admin/*`, `tests/fixtures/admin/*` | WP-0 | 2,300 |
| **WP-C1 Usage sim & calibration** | 1 | `sim/cache_rules.py`, `sim/lanes.py`, `sim/usage_replay.py`, `sim/calibrate.py`, `tests/sim/*` | WP-0 | 2,800 |
| **WP-V Verification & receipts** | 1 | `verify/*` (incl. `data/realization_priors.json`), `tests/verify/*` | WP-0 | 2,600 |
| **WP-C2 Block sim & breakers** | 2 | `sim/block_replay.py`, `detect/block.py`, `tests/blocksim/*`, `tests/fixtures/blocksim/*` | WP-0, WP-A1 (`fingerprint`), WP-C1 (`lanes`, `cache_rules`), WP-P | 2,600 |
| **WP-D Detectors, levers, policy** | 2 | `detect/usage.py`, `detect/failure.py`, `levers/*`, `policy_pack.py`, `tests/detect/*` | WP-0, WP-C1, WP-V (`shapley`, `realization`), WP-P | 2,800 |
| **WP-O Outputs & CLI** | 2 | `cli.py`, `pipeline.py`, `commands/*`, `outputs/*`, `adapters/all.py`, `tests/outputs/*`, `tests/cli2/*` | all phase-1 packages | 3,000 |
| **WP-Q Quality, demo fleet, docs, CI** | 2 | `demo_fleet.py`, `tests/e2e/*`, `.github/*`, `docs/*`, `README.md`, `DESIGN.md`, `SECURITY.md`, `CHANGELOG.md`, `pyproject.toml` (phase 2), `tokenbill/__init__.py` (version), `scripts/*` (except ownership check) | all | 1,900 |

Total ≈ 32k LOC including tests.

### 5.3 Interfaces each package provides (beyond the frozen contracts)

**WP-0** — §2.4 records, §2.6 money/labels, §2.11 contracts, plus:

```python
# adapters/conventions.py
@dataclass(frozen=True)
class Convention:
    convention_id: str; provider: str; inclusive_input: bool; version_range: str; notes: str
def register_convention(conv: Convention, fn: Callable[[Mapping[str, object]], tuple[UsageBuckets, list[str]]]) -> None
def normalize(convention_id: str, raw_usage: Mapping[str, object]) -> tuple[UsageBuckets, list[str]]  # (buckets, notes)
def anthropic_inferences(raw_usage: Mapping[str, object], *, message_model: str, ctx: PricingContext,
                         id_prefix: str, advisor_model: str | None = None) -> tuple[list[Inference], list[str]]
    # applies the iterations rule of §2.5; returns inferences and data-quality notes
# contracts/pricing.py also holds ContractOverlay (so WP-R can emit suggestions without WP-P):
@dataclass(frozen=True, slots=True)
class ContractOverlay:
    name: str; multiplier: Decimal | None
    overrides: tuple[tuple[str, tuple[tuple[str, Decimal], ...]], ...]   # model -> ((bucket, usd_per_mtok), ...)
    effective_from: str; effective_to: str | None; derived: bool; sha256: str
# contracts/sim.py also holds Transition (so WP-D can build against WP-C1):
@dataclass(frozen=True, slots=True)
class Transition:
    request_id: str; lane_key: str; gap_ms: int; expected_reuse: int; reads: int; missed: int
    is_miss_event: bool; cause: str; predicted_hit: bool | None; diag_reason: str | None; ttl_s: int
# contracts/findings.py also holds ActionPlan and PolicyPack:
@dataclass(frozen=True, slots=True)
class ActionPlan:
    joint_saving: Figure; levers: tuple[Finding, ...]; groups: tuple[tuple[str, ...], ...]; method: str  # "shapley-exact" | "shapley-mc"
@dataclass(frozen=True, slots=True)
class PolicyPack:
    target: str; patch: Mapping[str, object]; entries: tuple[tuple[str, Figure, bool, str], ...]  # key, projection, needs_eval, note
# secrets.py
def find_secrets(text: str) -> list[tuple[str, int, int]]      # (type, start, end)
def redact(text: str) -> tuple[str, Counter[str]]
# textsafe.py
def sanitize(text: str, limit: int | None = None) -> str       # strips C0/C1 controls, keeps \n\t
# jsonable.py
def to_jsonable(obj: object) -> object                          # Decimal -> str, Enum -> value, dataclass -> dict
def dumps_canonical(obj: object) -> bytes                       # sorted keys, no whitespace, ensure_ascii
```

**WP-P**

```python
# rates/registry.py
@dataclass(frozen=True) class RateRow: ...            # fields of the JSON row in §2.7, Decimal-typed
@dataclass(frozen=True) class Modifier: ...
@dataclass(frozen=True) class RateLayer: name: str; rows: tuple[RateRow, ...]; modifiers: tuple[Modifier, ...]; sha256: str; as_of: str
def load_builtin(provider: str | None = None) -> RateLayer
def load_file(path: Path, name: str = "user") -> RateLayer
def model_price_layer(specs: Sequence[tuple[str, Decimal, Decimal]]) -> RateLayer   # --model-price
# rates/models.py
def normalize_model(model_raw: str, provider_hint: str | None = None) -> tuple[str, str, str] | None  # (channel, model, scope)
# rates/contract.py
def load_contract(path: Path) -> ContractOverlay
def from_model_pricing(obj: Mapping[str, object]) -> ContractOverlay
def to_model_pricing(overlay: ContractOverlay) -> dict[str, object]
# rates/engine.py
class RateCard:            # implements contracts.pricing.Pricer
    def __init__(self, layers: Sequence[RateLayer], contract: ContractOverlay | None = None) -> None: ...
def price_total(pricer: Pricer, items: Iterable[tuple[Inference, int]]) -> PricedTotal
# rates/billing_rules.py
@dataclass(frozen=True) class BillingRule: rule_id: str; billed_input: str; billed_partial_output: str; cache_written: str; confidence: str; source: str
def rule_for(provider: str, failure_mode: str) -> BillingRule
# rates/verify.py
def verify_snapshot(layer: RateLayer, snapshot: Path) -> list[Discrepancy]
def verify_live(layer: RateLayer, *, url: str) -> list[Discrepancy]      # opt-in network
```

Acceptance: F3 golden corpus (16 cases); registry validation failures; `tests/test_pricing.py` green;
`assert_pricer_conforms(RateCard(...))`; `pricing verify` offline fixture; stale-row warning; contract
round-trip `to_model_pricing(from_model_pricing(x)) == x` for the fixture.

**WP-A1**

```python
# fingerprint.py
def fingerprint_request(*, tools: Sequence[Mapping], system: str | Sequence[Mapping], messages: Sequence[Mapping],
                        key: bytes, tier: ContentTier, density: Callable[[str, str], Decimal] | None = None
                        ) -> tuple[ContentFingerprint, tuple[Breakpoint, ...], dict[str, str]]   # fp, markers, content (full tier)
def volatile_spans(text: str) -> list[tuple[str, int, int]]
def normalize_volatile(text: str) -> str
def lookback_positions(blocks: Sequence[BlockRef]) -> list[int]
# adapters/trace_v1.py
class TraceV1Adapter:      # Adapter; wraps frozen trace.read_trace; writes pinned to cache_write_5m (R5)
# adapters/trace_v2.py
class TraceV2Adapter:      # Adapter
def write_trace_v2(path: Path, sessions: Iterable[Session], *, key_id: str, tier: ContentTier) -> None
# instrument.py
class Recorder:
    def __init__(self, path: str | Path, run_id: str | None = None, *, format: str = "trace@2",
                 content: str = "fingerprint", key_file: str | Path | None = None,
                 diagnostics: bool = False, lane: str | None = None) -> None: ...
    def wrap(self, client: Any) -> Any: ...
    def httpx_event_hooks(self) -> dict[str, list[Callable]]: ...
    def close(self) -> None: ...
def recording(path, run_id=None, **kw) -> ContextManager[Recorder]
```

Acceptance: F12 tests; trace@1 adapter conformance; demo trace@1 files ingest to ledgers whose exact bill
equals v0.1 `as-billed` dollars to the micro-dollar.

**WP-A2** — `class ClaudeCodeAdapter` (Adapter, `capabilities = {usage_sequence, timing, ttl_split,
diagnostics, appended, events}`), `def iter_claude_files(root: Path) -> Iterator[Path]`. Acceptance: F4
tests; conformance; fuzz (random line truncation and field deletion never crash, always quarantine).

**WP-A3** — `adapters/conventions_ext.py` registers `openai.responses`, `openai.chat`, `otel.genai`,
`otel.genai.legacy`, `openinference`, `claude_code.otel`, `bedrock.converse`, `codex.rollout`
(pricing-gated); `class OtlpJsonAdapter`, `class OpenAIUsageAdapter`, `class CodexRolloutAdapter`,
`class BedrockAdapter`. Acceptance: F2 convention goldens (non-Anthropic), F11 tests, conformance.

**WP-S**

```python
class SqliteStore:        # implements contracts.store.LedgerStore
    def __init__(self, path: str | Path, *, create: bool = True) -> None: ...
    def meta(self) -> dict[str, str]: ...
class KeyStore:
    @staticmethod
    def load_or_create(path: Path | None = None) -> bytes: ...          # 0600, 32 random bytes
def kanon(rows: Sequence[Mapping[str, object]], group_keys: Sequence[str], principal_key: str,
          k: int = 5) -> tuple[list[Mapping[str, object]], int]            # suppressed rows removed, with complementary suppression
def require_self_or_aggregate(group_by: Sequence[str], self_principal: str | None) -> None   # raises UsageError
```

Acceptance: `assert_store_conforms`; idempotent re-ingest; cross-source precedence and corroboration
note; delta-encoded fingerprints round-trip; purge + audit; file mode 0600; 10⁶ inference upserts within
budget; F6 aggregation tests.

**WP-R**

```python
class UsageReportAdapter, CostReportAdapter, ClaudeCodeAnalyticsAdapter, EnterpriseAnalyticsAdapter,
      OpenAIUsageBucketsAdapter, OpenAICostsAdapter          # Adapters producing UsageAggregate / CostLine
def pull_live(kind: str, *, key_env: str, since: str, until: str, out_dir: Path) -> list[Path]   # opt-in network; writes pages to files
def reconcile(ledger: Iterable[UsageRecord], aggregates: Sequence[UsageAggregate], cost_lines: Sequence[CostLine],
              pricer: Pricer, *, tolerance_pct: Decimal = Decimal("0.5"), closed_only: bool = False,
              today: str) -> ReconciliationReport
def suggest_contract(report: ReconciliationReport, *, name: str) -> ContractOverlay
COST_TYPE_MAP: Mapping[tuple[str | None, str | None], str]   # (cost_type, token_type) -> bucket
```

Acceptance: F5 tests; fixtures shaped from the documented responses; socket guard proves offline default.

**WP-C1**

```python
class RulesTable:          # implements CacheRulesProvider (seeded from §2.10 / §4.3 with sources)
def assemble_lanes(sessions: Iterable[Session], events: Iterable[LaneEvent]) -> list[Lane]
def classify_transitions(lane: Lane, rules: CacheRulesProvider) -> list[Transition]
class UsageReplayer:       # implements Replayer for Policy fields ttl/keepalive/compaction_window/model_remap/batch/repairs
def calibrate(lanes: Sequence[Lane], pricer: Pricer, rules: CacheRulesProvider, *, granularity: str = "day",
              folds: int = 5, seed: int = 0) -> CalibrationReport
def fit_density(lanes: Sequence[Lane]) -> tuple[tuple[str, Decimal, int, Decimal], ...]
```

Acceptance: F7 tests; `assert_replayer_conforms`; `Policy.observed()` replay equals the exact bill to the
micro-dollar; each policy's minimal-change property (unaffected requests byte-identical in outcomes).

**WP-V**

```python
# verify/stats.py
def bootstrap_ci(values, stat, *, b: int, seed: int, level: float = 0.95) -> tuple[float, float]
def cluster_bootstrap_ci(rows, cluster_key, estimator, *, b: int, seed: int, strata_key=None) -> tuple[float, float]
def cuped(y: Sequence[float], x: Sequence[float]) -> tuple[list[float], float]      # adjusted, theta
def srm_pvalue(observed: Mapping[str, int], planned: Mapping[str, float]) -> float
def wilson(hits: int, n: int, z: float = 1.96) -> tuple[float, float]
# verify/estimators.py
def paired_ab(runs: Sequence[Mapping], *, seed: int) -> MeasurementResult
def did_cuped(panel: Sequence[Mapping], *, adoption: str, seed: int) -> MeasurementResult
def imputation_did(panel: Sequence[Mapping], *, seed: int) -> MeasurementResult
# verify/shapley.py
def shapley(players: Sequence[str], value: Callable[[frozenset[str]], Decimal]) -> dict[str, Decimal]
def shapley_mc(players, value, *, permutations: int = 200, seed: int = 0) -> tuple[dict[str, Decimal], dict[str, Decimal]]  # values, SE
# verify/realization.py
def priors() -> Mapping[str, tuple[Decimal, Decimal, Decimal]]
def project(credit: Decimal, lever_class: str, window_days: int, *, calibration: Calibration,
            upper_bound: bool, basis: Basis) -> Figure
# verify/label_policy.py
def decide(design: str, randomized: bool, guards: Sequence[GuardResult], ci: tuple[Decimal, Decimal]) -> Evidence
# verify/rollout.py
def plan(clusters: Sequence[str], *, waves: int, holdback: Decimal, seed: int, lever_id: str) -> Mapping[str, object]
# verify/receipts.py
def build_receipt(m: MeasurementResult, *, lever_patch_sha256: str, shapley_credit: Decimal | None,
                  tool_version: str, created: str) -> dict[str, object]
def canonical_bytes(receipt: Mapping[str, object]) -> bytes
def pae(payload_type: str, body: bytes) -> bytes
def sign(receipt: Mapping[str, object], *, key: Path, namespace: str = "tokenbill-receipt") -> dict[str, object]
def verify(envelope: Mapping[str, object], *, allowed_signers: Path, identity: str,
           namespace: str = "tokenbill-receipt") -> bool
```

Acceptance: F10 tests; the F9 Shapley example (A = 8, B = 17.5, C = 4.5); `shapley_mc` within 3 SE of
exact on a 5-player game.

**WP-C2** — `class BlockReplayer` (Replayer for `breakpoint_policy` and block repairs, falls back to
usage-level for lanes without fingerprints), detectors in `detect/block.py` registered with
`requires={"blocks"}`. Acceptance: F13 dual-engine and codebase-experiment fixtures; performance budget.

**WP-D** — detectors (`detect/usage.py`, `detect/failure.py`), levers (`levers/ttl.py`,
`levers/compaction.py`, `levers/routing.py`, `levers/batch.py`), `levers/plan.py: build_action_plan(
findings, lanes, ctx) -> ActionPlan`, `policy_pack.py: build_policy_pack(plan, *, target, include_tradeoffs,
contract: ContractOverlay | None) -> PolicyPack`, `SETTINGS_ALLOWLIST` (key → minimum Claude Code version,
value validator). Acceptance: F8, F9, F14 tests; `assert_detector_conforms` for every detector.

**WP-O** — `pipeline.py` (`run_scan`, `run_ingest`, `run_bill`, … returning result@2 dataclasses; no
printing), `commands/<name>.py` each with `add_parser(sub)` and `run(args) -> int`, `outputs/*`,
`adapters/all.py`. Acceptance: F15, F16 tests; golden v0.1 outputs; exit codes; schema validation; HTML
self-containment and CSP; FOCUS columns; no float money in JSON; `--deterministic` byte-stability.

**WP-Q** — F17. Acceptance: the flagship e2e test; CI workflows lint-clean under zizmor; release dry run
produces SBOM, provenance and attestations; `docs/VERIFY.md` commands work on the dry-run artifacts.

### 5.4 Dependency order and critical path

```
WP-0 ─┬─► WP-P ──────────────┐
      ├─► WP-A1 ─────────────┤
      ├─► WP-A2 ─────────────┤
      ├─► WP-A3 ─────────────┼─► (merge gate 1) ─┬─► WP-C2 ─┐
      ├─► WP-S ──────────────┤                   ├─► WP-D  ──┼─► (merge gate 2) ─► integration + adversarial review ─► 0.2.0
      ├─► WP-R ──────────────┤                   ├─► WP-O  ──┤
      ├─► WP-C1 ─────────────┤                   └─► WP-Q  ──┘
      └─► WP-V ──────────────┘
```

Merge gate 1: all phase-1 branches merged into `v0.2-integration`; full test suite green; conformance
suite green for every implementation; a smoke script ingests the Claude Code fixture tree into SQLite,
prices it with `RateCard`, reconciles against the admin fixtures and calibrates. Merge gate 2: phase-2
branches merged; the e2e flagship and dual-engine tests green; performance budgets green. Critical path:
WP-0 → WP-C1 → WP-D → WP-O (the CLI cannot finish until detectors and replays exist; WP-O builds against
fakes until gate 1 and switches to real implementations after).

---

## 6. Deferred items, risks and mitigations

### 6.1 Explicitly deferred (and why)

| Item | Why deferred | Findings |
|---|---|---|
| Recording HTTP proxy (`ANTHROPIC_BASE_URL`) | Puts Token Bill in the inference path (security review, availability risk); the recorder, OTel raw bodies and transcripts cover v0.2 | `cb-recorder-coverage`, `ent-supply-chain-incidents`, `portkey-panw-gateway-security` |
| OTLP receiver server, scheduled signed container, collector reference config | Server surface; v0.2 reads collector `file` exporter output | `ent-deployment-modes` |
| Hosted dashboard with SSO/RBAC/SCIM | Static HTML behind the enterprise SSO proxy meets phase-1 needs; ASVS scope avoided | `ent-server-sso-rbac` |
| Gemini, xAI, DeepSeek, Mistral, OpenRouter, Azure OpenAI rates/adapters; Cursor, Copilot connectors | Conventions and facts are documented (§4.6); Claude-heavy enterprise first; add as data + adapters in 0.3 | `gemini-usage-semantics`, `xai-cost-ticks`, `openrouter-accounting`, `cursor-admin-api`, `copilot-ai-credits`, `adapter-adoption-ranking` |
| `count_tokens` exact attribution and the tokenizer "rebaseline" auditor | Needs network, API keys and rate-limit budgeting; the endpoint is itself an estimate; billed-delta density fits suffice for v0.2 | `count-tokens-exact-attribution` (corrected), `tokenizer-inflation-47plus`, `exact-token-counting` |
| Compression, masking, pruning and context-editing transform replays | Trajectory-changing: token savings do not predict bills (r=0.154); v0.2 verifies vendor tools with `measure` instead of projecting them | `token-not-cost`, `obs-masking`, `agentdiet`, `jagged-pruning`, `hidden-reacquisition` |
| Capacity, commitment and seat optimizers; contract ledger and renewal calendar | Contract data required; capacity is not a discount at full utilization; high stakes, lower urgency than reconciliation | `cc-capacity-parity`, `cc-sizing-newsvendor`, `cc-seat-vs-usage`, `cc-renewal-traps` |
| CI/eval workload modules (review trigger replay, $ per addressed finding, eval subset sizing, judge choice) | Need GitHub/eval outcome joins; v0.2 ships `workload_class` attribution and the batch lever | `ci-trigger-multiplier`, `ci-review-addressed-rate`, `eval-subset-irt` |
| Embeddings ledger | Rarely tops the bill; P2 per the research | `emb-scope-decision` |
| Router audit and cache-aware routing simulator | Needs outcome labels and eval harness | `router-baselines-knn`, `cache-aware-routing`, `routers-vs-cache-net-savings` |
| `tokenbill watch` anomaly detection, forecasts and cap recommendations | Needs live polling; v0.2 is batch/offline | `ent-anomaly-detection`, `ent-forecast-budgets`, `runaway-circuit-breaker` |
| Callaway–Sant'Anna, switchbacks, synthetic control, confidence sequences | v0.2 ships imputation DiD, CUPED DiD, paired A/B and pre-registered looks; the rest adds risk without changing v0.2 decisions | `staggered-did`, `switchback-carryover`, `its-rdit-org-wide`, `sequential-monitoring` |
| FOCUS 1.5 export | Not ratified (target 2026-12-03); 1.4 with `x_` columns now | `focus-15-ai-columns` |
| Hooks, status line, nudge delivery, PR bot | Policy pack generates config; delivery channels and frequency caps are v0.3 | `channel-code-review-hooks`, `nudge-decay-durability`, `education-channels` |
| Cross-workspace cache fragmentation detector | Requires org-key fingerprints across workspaces; enable once fleets deploy org keys | `cache-isolation-gateways`, `anth-workspace-fleet-scope` |
| Hidden-token audit signals | Audits are evadable; v0.2 limits itself to cross-source consistency in reconciliation | `hidden-token-audit-research`, `audit-evasion-limits` |
| LLM-assisted "explain this run" | Would make the cost tool a cost center; not needed for trust | `observer-compiled-views` |
| Multi-agent pruning, plan caching, semantic caching | Research-grade or risky for coding agents | `mas-pruning-research`, `agentic-plan-caching`, `semantic-cache-pitfalls` |

### 6.2 Risks and mitigations

| Risk | Consequence | Mitigation in this design |
|---|---|---|
| Claude Code transcript format drifts (documented as internal) | Silent miscounts | Version histogram and unknown-field counters; golden fixtures per version; quarantine instead of crash; OTel and Admin reconciliation as independent checks; retention warning before the 30-day cleanup |
| Prices change often (three Anthropic moves in one quarter) or are transcribed wrong | Wrong bills | Effective-dated rows with sources and `verified_on`; stale-row warnings; offline golden corpus; weekly live `pricing verify`; reconciliation rate-card error ≤0.5% per model-day as the acceptance gate |
| Undocumented billing (aborts, stacking combinations, CCU) | False precision | Billing-rules table with confidence; ranges instead of points; residual codes in reconciliation; `stacking: assumed` provenance |
| Simulator overclaims or gets the sign wrong | Lost credibility, negative-ROI rollouts | Minimal-change counterfactuals; one-step-ahead calibration with ASHRAE gate; ρ from the org's own data; upper-bound flags; realization priors including negative values; Shapley; only `measure` can produce measured/verified numbers, and only reconciled windows can be signed |
| Evidence comes from one heavy user | Wrong expectations | Corpus percentages are labeled mechanism sizes; fleet numbers come from the org's own calibrated ledger; `demo --fleet` is synthetic and says so |
| Privacy, works-council or GDPR objections | Deployment blocked | Content-free tiers, HMAC keys held by the org, k≥5 with complementary suppression, no individual ranking, audited break-glass, purge/retention, DPIA and works-council pack |
| Supply-chain compromise | Credential theft in the finance toolchain | Zero runtime dependencies; SHA-pinned CI with minimal permissions; SBOM, provenance, PEP 740 attestations, reproducible builds; plugins only with `--plugins`; no network without `--live` |
| Scale (thousands of developers) | Memory blow-ups, slow runs | Content-addressed blocks and delta-encoded fingerprints; streaming adapters; per-lane processing; SQLite with indexes; CI performance budgets |
| Parallel build integration failures | Late breakage | Frozen contracts; fakes; conformance suite; ownership check; two merge gates; the WP-O pipeline built against fakes until gate 1 |
| Provider estimates mistaken for bills (OTel `cost_usd`, Analytics `estimated_cost`, `cache_missed_input_tokens`) | Double counting or false precision | `Basis.PROVIDER_ESTIMATE`; R3/R4 enforced by renderers; never priced |
| Server diagnostics are partial (first divergence, 1P only, `unavailable`) | Over-trusting labels | Only the four `*_changed` labels (and not-found with gap > TTL) enter the confusion matrix; unlabeled share reported; fallback to Token Bill's own classification elsewhere |
| Inferred request start times in transcripts | TTL misclassification near the 5m/1h boundary | Transitions with gaps within ±10 s of τ are marked ambiguous and excluded from ρ fitting; sensitivity reported |
| Small fleets cannot detect small effects (MDE ~13.6% at 1,000 developers) | "Verified" claims that are noise | A/A-based MDE guard refuses verified labels; the report states "not verifiable at this fleet size" and recommends longer windows or pooling |
| Goodhart effects on cost-per-PR style metrics | Gaming, quality loss | Primary unit is cost per active developer-day (ITT); quality guardrail for trade-off levers; no leaderboards |
| Lab has no Admin API key | Reconciliation impossible | Offline import of recorded pages and exports; `insufficient_data` verdict stated plainly rather than implying reconciliation |
| v1 and v2 engines diverge | Contradictory reports | Dual-engine agreement test on the demo corpus; v1 remains default only for trace@1 in 0.2.0; switch after the agreement holds on real traces |
| `Inexact` trap fires on unexpected inputs | Crash in pricing | Precision 60 digits; integer token bounds (2⁵³); property tests over extreme inputs; clear error naming the row and bucket |

---

## Appendix A. Lens traceability (what the trust lens asked for, and where it is)

| Lens requirement | Design element | Where |
|---|---|---|
| Canonical usage/attempt ledger, billing-exact across providers and modifiers | Session ▸ Lane ▸ Request ▸ Attempt ▸ Inference; disjoint `UsageBuckets`; convention registry with sum-checks; iterations rule; source precedence | §2.4, §2.5, §2.8, F2, F4, F11 |
| Versioned pricing/rate-card registry with provenance and effective dates | `rates/` JSON rows with sources, `verified_on`, effective intervals, modifiers, contract overlays, `modelPricing` import/export, staleness, `pricing verify` | §2.7, §4, F3 |
| Decimal / micro-USD money | `EXACT_CTX` traps `Inexact`; Decimal everywhere; micro-USD only at output/interchange; no-float lint | §2.6, F1 |
| Reconciliation against Admin/Analytics APIs | three-layer reconciliation (rate card vs invoice, token coverage, dollar coverage), residual codes, effective discount, contract suggestion, provisional vs final | F5, §4.4 |
| Calibrated simulation (documented vs calibrated), validated against `cache_miss_reason` | one-step-ahead predictive replay, ASHRAE NMBE/CV(RMSE) gate, ρ bands, density fits, confusion matrix on mapped labels | §2.10, F7, F13 |
| Savings labels exact / estimated / measured / verified | `Figure` type with construction-time rules; renderer rules R3/R4; `label_policy.decide` | §2.6, F9, F10 |
| Signed savings receipts | canonical JSON (JCS subset), DSSE PAE, `ssh-keygen -Y` sign/verify, refusal to sign estimates or unreconciled windows | F10, §4.7 |
| Privacy (content-free fleet processing, k-anonymity, no individual ranking) | content tiers, HMAC keys, volatile-before-hash, k≥5 with complementary suppression, self-view, audited break-glass, purge | §2.9, F6 |
| Backward compatibility | frozen v1 engine, trace@1 adapter, golden outputs, `pricing.py` shim, recorder `format="trace@1"` | §2.15 |
| Parallel buildability | frozen contracts, fakes, conformance suite, ownership manifest, phases and merge gates | §5 |

