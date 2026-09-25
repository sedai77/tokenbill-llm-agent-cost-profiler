# Token Bill v0.2 — Fleet & FinOps design proposal

Status: proposal for build. Lens: **enterprise fleet and FinOps first** (thousands of developers on Claude Code plus API workloads on several providers). The lens sets priorities and breaks ties. The document still covers the whole product.

Evidence convention: `[finding-id]` refers to a finding in the verified research base (`research/_all.json`). Where a finding was corrected by the fact-check pass, this document uses the corrected numbers. Percentages measured on the local Claude Code corpus come from **one heavy user** (about $215 per active day against Anthropic's ~$13 fleet average) `[cc-heavy-tail-concentration]`. They show mechanisms and upper bounds, not fleet averages, and are labelled that way everywhere below.

Honesty vocabulary used throughout (§2.10):

| Label | Meaning | Example |
|---|---|---|
| **exact** | Rate-card arithmetic on provider-reported usage | Opus 5.5 cache reads × $0.20/MTok |
| **estimated** | Replay/simulation, assumption, or estimated usage | "compacting at 400k would save ~$X" |
| **measured** | Quasi-experimental estimate (non-random rollout, ITS) with CI | pre/post DiD on a non-randomized wave |
| **verified** | Randomized design that passed every guard (§3 F12) | stepped-wedge with CUPED, SRM ok, reconciled |

Price basis is a separate, orthogonal label: `list`, `contract`, `invoice`, `provider-reported`.

---

## 0. One-page summary

**What v0.2 is.** A zero-dependency, local-first Python tool that turns the telemetry an enterprise already has (Claude Code transcripts and OpenTelemetry, Anthropic/OpenAI admin usage and cost exports, Bedrock/Vertex/OpenRouter/LiteLLM usage, OTel GenAI and OpenInference spans) into:

1. an **invoice-reconciled ledger**: TTL-exact, iteration-exact, deduplicated, in integer nano-dollars, content-free;
2. **team-level, k-anonymous showback** and a **FOCUS export** so existing FinOps tools show the numbers;
3. **cause-named waste findings**, each with a dollar value, evidence and a concrete fix;
4. a **policy pack**: a managed-settings / gateway-config patch with Shapley-deflated, realization-rate-adjusted dollar projections and a randomized rollout plan;
5. **budgets and anomaly alerts** that name a cause and a $/day;
6. a **CI cost gate**;
7. **savings verification** against the invoice, with signed receipts.

**What it is not.** Not a gateway, not an enforcement layer, not a trace store, not a dashboard server, and never a tool that ranks individuals `[enforcement-primitives]` `[langfuse-clickhouse]` `[leaderboard-goodhart]`.

**Why this ordering.** The biggest durable savings in public enterprise evidence come from changing defaults, not from persuasion: Datadog saved >$1M/month, $975k of it from two default changes (model and effort) `[org-defaults-datadog]` `[datadog-1m-month-case]` `[defaults-beat-nudges-meta]`. Defaults can only be changed safely with (a) a correct ledger, (b) a credible projection, and (c) a verification that finance accepts. v0.1.2 fails (a): it misprices real Claude Code data by −7.3% `[cc-pricing-1h-opus55-gap]`, and a naive importer would overstate it 2.33× `[cc-import-dedup-message-id]`. So the build order is: ledger → ingestion → attribution → reconciliation → detectors → policy what-if → verification.

**Build plan.** One foundation package (`tokenbill/core`, built first), then 18 disjoint work packages built in parallel against frozen interfaces, then one integration wave (§5).

---

## 1. Thesis and positioning

### 1.1 Thesis

Token Bill v0.2 is the vendor-neutral, zero-dependency, local-first layer that makes an enterprise LLM and agent bill smaller. It builds an invoice-reconciled, TTL- and iteration-exact ledger from telemetry the org already has, without storing prompt or code content. It names the causes of spend at team level (context size, cache lifetime, cold resumes, gateway stripping, model, effort and fast-mode defaults, delegation, failures) and prices each cause in dollars. Because defaults, not nudges, durably shrink bills, it compiles findings into a projected managed-settings and gateway policy pack with a randomized rollout plan. It then proves realized savings against the invoice with signed receipts labelled exact, estimated, measured or verified.

### 1.2 What changed in 2026, and what that leaves for Token Bill

The basic layer is now commodity or first-party:

- **Anthropic first-party.** Claude Code `/usage` shows per-session cache hit share, misses and a "likely cause" (v2.1.260+), but for one developer, one machine, the main thread only, at list price `[cc-usage-likely-cause]` `[cc-first-party-visibility]`. The cache-diagnostics beta names the first diverging component, but only on the first-party API, one request at a time, and `cache_missed_input_tokens` is a magnitude estimate, not a billing number `[anth-cache-diagnostics-beta]` `[anth-cache-diagnostics]`. Claude Code OTel exports per-user and per-skill cost at list price `[cc-otel-pipeline]` `[cc-list-price-estimates]`. The Claude apps gateway enforces per-user caps but loses the 1h TTL `[cc-apps-gateway-routing-tax]`. The Admin Usage/Cost, Claude Code Analytics and Enterprise Analytics APIs provide ground truth `[anth-admin-apis-org-scan]`.
- **Datadog Agent Console (Preview)** is the closest enterprise competitor. It attributes coding-agent spend by agent, team and user, detects retry loops and file re-reads, and ships a Fix Library of hooks `[datadog-agent-console-fix-library]`.
- **ccusage** and similar local trackers answer "what", not "why" `[ccusage-local-parity]`.
- **Gateways and trace stores**: LiteLLM (spend by key/tag; its PyPI compromise in March 2026) `[litellm-spend-and-supply-chain]`, Langfuse (now ClickHouse) `[langfuse-clickhouse]`, Helicone (maintenance mode) `[helicone-maintenance-mode]`, Portkey (Palo Alto Networks) `[portkey-panw-gateway-security]`, Cloudflare (free spend limits) `[cloudflare-free-spend-limits]`. Observability platforms price tokens but none explains cache loss `[obs-platforms-no-cache-rca]`. Coding-agent dashboards are commodity `[coding-agent-dashboards-commodity]`.
- **FinOps platforms** (CloudZero, Vantage, Datadog CCM) already own allocation and chargeback and ingest Anthropic data `[finops-platforms-export-target]`.

What nobody sells, per the research `[verified-savings-gap-rtk]` `[ent-market-tokenomics]` `[competitive-landscape]`:

1. **Numbers that reconcile to the invoice.** Dollars that are TTL-exact, iteration-exact and deduplicated, and that reconcile per workspace-day to the provider's cost report `[ent-reconciliation]` `[cc-reconciliation-matrix]`.
2. **Fleet-scale causes with dollars, content-free.** Anthropic's own diagnostics prove breaker detection works on hashes alone `[ent-cache-diagnostics-privacy]`. Claude Code already records `diagnostics.cache_miss_reason` locally `[cc-miss-taxonomy-ground-truth]`.
3. **Policy what-if → deployable patch.** Price last month's real traffic under alternative defaults, emit the managed-settings patch `[org-defaults-datadog]` `[cc-managed-settings-levers]`.
4. **Verified savings.** A paired A/B showed RTK, advertised at 60–90% savings, raised per-task cost 7.6% while its counter claimed 96M tokens saved `[verified-savings-gap-rtk]`. In 2,908 billed Claude Code runs, token reduction barely predicted cost change (r = 0.15) `[token-not-cost]` `[token-reduction-not-cost]`.
5. **Enterprise-safe by construction.** Zero runtime dependencies after the LiteLLM compromise `[ent-supply-chain-incidents]`; no content; team-level k ≥ 5 aggregation consistent with works-council and GDPR constraints `[labor-law-constraints]` `[aggregation-k5]`.

### 1.3 Positioning matrix

| Capability | Anthropic native (/usage, OTel, Admin APIs, diagnostics) | Datadog Agent Console | ccusage | LiteLLM / Langfuse | FinOps platforms | **Token Bill v0.2** |
|---|---|---|---|---|---|---|
| Per-developer usage view | yes (local, list price) | yes | yes (local) | per key/trace | per user (ingested) | **self-view only** (`me`) |
| Fleet cost by team/skill/MCP | OTel (list price) | yes | no | by tag | yes | **yes, k ≥ 5, content-free** |
| TTL-exact (5m/1h) + iteration-exact $ | transcripts only, no fleet view | not documented | partial (5m fallback) | varies (bugs) `[fw-telemetry-conventions]` | from cost report | **yes, reconciled** |
| Invoice reconciliation with residuals | n/a (is the invoice) | no | no | no | yes (their own) | **yes, per day × workspace × model** |
| Cache-miss cause in $ across fleet | per request / per session | re-reads, loops | no | no | no | **yes (diagnostics-validated)** |
| Counterfactual policy replay (TTL, compaction window, model/effort default) | no | savings estimate per repo | no | no | no | **yes, Shapley-deflated** |
| Emits managed-settings / gateway patch | n/a | hooks via PR | no | no | no | **yes, never auto-applied** |
| Randomized rollout plan + verified savings + signed receipt | no | no | no | no | no | **yes** |
| FOCUS export with waste columns | no | FOCUS (cost) | no | no | yes (cost) | **yes (cost + x_ waste)** |
| Content-free by default | OTel yes; transcripts no | vendor-hosted | reads local content | stores content | cost only | **yes** |
| Runtime dependencies / hosting | SaaS | SaaS agent | npm | Python deps / server | SaaS | **none / local** |

Design consequences:

- **Don't duplicate `/usage`**: aggregate the same signals fleet-wide and rank by recoverable dollars `[cc-first-party-visibility]`.
- **Don't rebuild enforcement**: compile policy into the primitives vendors already ship (managed settings, gateway spend limits, workspace caps) `[enforcement-primitives]`.
- **Feed FinOps tools, don't replace them**: FOCUS rows with `x_` waste columns, OTel metrics out `[finops-platforms-export-target]` `[ent-focus-1-4]`.
- **Be the neutral referee** for vendor savings claims (compressors, routers, proxies) with `tokenbill ab` and `verify` `[oss-token-minimizer-ecosystem]` `[routers-vs-cache-net-savings]` `[headroom-compression-cache-aware]`.
- **Deprioritize what the data says is small.** Duplicate file reads were 0.6% of reads and <0.1% of the bill on the local corpus `[cc-redundant-reads-negative]`, although competitors headline them. Context size, TTL and idle behaviour, and model defaults dominate `[cc-context-size-driver]` `[cc-cold-resume]` `[cc-delegation-model-routing]`.

### 1.4 Required README change

The v0.1 README says provider dashboards are "silent on why". That is no longer true and must be rewritten before any enterprise review `[cc-usage-likely-cause]`. The integration WP (§5, WP-I) owns this.

### 1.5 Stakes, as arithmetic on published anchors

Anthropic reports ~$13 per developer per active day, $150–250 per developer per month, and 90% of users under $30 per active day `[cc-enterprise-baseline]`. A 2,000-developer fleet is ≈ $3.6–6.0M/year before API and CI workloads `[ent-forecast-budgets]`. Automated review alone reaches parity with interactive spend at 6–17 review runs per developer per month `[ci-review-unit-costs]`.

---

## 2. Architecture

### 2.1 Principles

1. **Stage pipeline.** ingest → normalize → price → attribute → store → lanes → detect / what-if → rank → publish (k-anon) → render / export. Each stage has a typed interface in `tokenbill/core` `[cb-code-quality]`.
2. **Content-free by default.** Adapters may *read* content to measure lengths, but only enums, counts, token numbers, timestamps, allowlisted names and HMAC digests leave the adapter. Content-bearing analysis (the v0.1 byte-level breakers) stays available on `tokenbill/trace@1` files the user supplies, local only `[ent-privacy-by-default]` `[ent-redaction-limits]`.
3. **Streaming and incremental.** No stage holds a whole corpus in memory. Append-only sources resume from byte offsets. Ingestion is idempotent `[ent-scale-storage]` `[cb-scale-memory]`.
4. **Integer money.** All stored money is `int` nano-USD (`nUSD`, 1e-9 USD), computed in `Decimal` and rounded half-even once per usage line. Worst-case rounding is 0.5 nUSD per line (≤ $0.50 per billion lines) `[ent-reconciliation]`.
5. **No network by default.** Only `pull`, `otlp-receive`, the webhook alert sink and `pricing verify --online` touch the network. Each is explicit and opt-in. A test proves the rest of the CLI opens no sockets `[ent-deployment-modes]`.
6. **Zero runtime dependencies**, Python ≥ 3.10, stdlib only (`sqlite3`, `json`, `decimal`, `hmac`, `http.server`, `urllib`, `gzip`, `csv`, `subprocess` for `ssh-keygen`). No `tomllib` (3.11+), so config is JSON.
7. **Determinism.** Same inputs, same seed → byte-identical JSON output across processes (the v0.1 standard).
8. **Labels travel with numbers.** Every money value in JSON carries `basis` and `price_basis`, and every percentage carries `basis`. A schema test fails if one does not.

### 2.2 Package layout and ownership

`WPn` = owning work package (§5). Files not listed are unchanged. Subpackages shared by several WPs (`ingest/`, `connect/`, `detect/`, `finops/`) get a docstring-only `__init__.py` from WP0 that nobody else edits. Single-owner subpackages own their `__init__.py`. Registration is by dotted-path strings in `core/registry.py`, so owners never touch shared files.

```
tokenbill/
  __init__.py                 version → "0.2.0"                                    [WP-I]
  common.py                   unchanged (errors, rng, canonical_json)
  # ---- v0.1 content-trace path (kept, hardened) ----
  trace.py analyzer.py simulator.py breakers.py instrument.py pricing.py report.py  [WP14]
  demo_traces.py              unchanged                                             [—]
  # ---- foundation ----
  core/__init__.py records.py results.py money.py ids.py protocols.py registry.py
       config.py streams.py sanitize.py labels.py testing.py                         [WP0]
  # ---- pricing ----
  rates/__init__.py card.py engine.py resolve.py contract.py drift.py
        data/anthropic.json data/openai.json data/google.json data/aws.json
        data/others.json data/server_tools.json data/lifecycle.json data/benchmarks.json  [WP1]
  # ---- ingestion (adapters) ----
  ingest/__init__.py                                                                 [WP0]
  ingest/claude_code.py ingest/cc_layout.py                                         [WP2]
  ingest/otlp.py ingest/cc_otel.py ingest/genai.py ingest/conventions.py
  ingest/otlp_receiver.py                                                            [WP3]
  ingest/anthropic_api.py ingest/openai.py ingest/bedrock.py ingest/gemini.py
  ingest/openrouter.py ingest/litellm.py ingest/usage_jsonl.py ingest/trace_v1.py    [WP4]
  # ---- network connectors + reconciliation ----
  connect/__init__.py                                                                [WP0]
  connect/http.py connect/anthropic_admin.py connect/cc_analytics.py
  connect/enterprise_analytics.py connect/openai_admin.py connect/replay.py          [WP5]
  finops/reconcile.py finops/calibrate.py                                            [WP5]
  # ---- storage ----
  store/__init__.py schema.py db.py merge.py views.py aggregate.py retention.py       [WP6]
  # ---- attribution & privacy ----
  attribution/__init__.py rules.py kanon.py coverage.py workload.py secrets.py
               dpia.py templates/*.md                                                [WP7]
  # ---- detectors ----
  detect/__init__.py                                                                 [WP0]
  detect/misses.py cache_health.py gateway.py write_waste.py fanout.py
  detect/context_tax.py config_tax.py tool_carry.py cpt.py                           [WP8a]
  detect/model_switch.py premiums.py effort.py truncation.py failures.py
  detect/automation.py runaway.py                                                    [WP8b]
  detect/ttl_advisor.py compaction_window.py model_reprice.py                        [WP9]
  # ---- what-if engines ----
  whatif/__init__.py engine.py ttl.py compaction.py reprice.py batch.py
         shapley.py priors.py                                                        [WP9]
  # ---- policy ----
  policy/__init__.py catalog.py managed_settings.py litellm_config.py hooks.py
         rollout.py                                                                  [WP10]
  # ---- finops outputs ----
  finops/__init__.py                                                                 [WP0]
  finops/showback.py focus.py budgets.py anomaly.py forecast.py kpis.py alerts.py    [WP11]
  # ---- verification ----
  verify/__init__.py estimators.py guards.py design.py receipts.py ab.py              [WP12]
  # ---- CI gate ----
  ci/__init__.py check.py execution_file.py sarif.py                                 [WP16]
  # ---- CLI, pipeline, renderers ----
  cli.py pipeline.py                                                                  [WP13]
  render/__init__.py theme.py fleet_html.py showback_html.py terminal.py csvout.py
         json_result.py                                                              [WP13]
  # ---- fleet demo ----
  demo_fleet.py                                                                      [WP17]
tests/
  core/ rates/ ingest/ connect/ store/ attribution/ detect/ whatif/ policy/
  finops/ verify/ ci/ render/ fleet/ fuzz/ e2e/  (each dir owned by the matching WP)
  fixtures/<wp>/...                                                                  [owning WP]
  test_*.py (v0.1 tests)                                                             [WP14]
docs/  SPEC.md DESIGN.md README.md CHANGELOG.md (rewritten)                          [WP-I]
       SPEC-0.2-interfaces.md schemas/usage-1.schema.json                            [WP0]
       VERIFY.md THREAT-MODEL.md SECURITY.md                                         [WP15]
.github/  workflows/*.yml dependabot.yml                                             [WP15]
```

### 2.3 Data flow

```
 developer laptops (MDM-scheduled)          central (FinOps host / CI runner / container)
 ────────────────────────────────           ──────────────────────────────────────────────
 ~/.claude/projects/**.jsonl                  usage@1 files ─┐
   └─ tokenbill collect claude-code ─► usage@1 ─(org log shipping)─►  tokenbill ingest usage ─┐
                                                                                             │
 OTel collector (fileexporter JSON) ───────────────────────────────►  tokenbill ingest otlp ──┤
 or  tokenbill otlp-receive (127.0.0.1:4318) ───────────────────────►                          │
 provider exports (OpenAI, Bedrock logs, Vertex, OpenRouter, LiteLLM) ─► tokenbill ingest <k> ─┤
 Admin/Analytics APIs ─► tokenbill pull <src> ─► UsageBucket (reference) ──────────────────────┤
                                                                                             ▼
          adapter → Request/Event ─► rates.Pricer ─► attribution.rules ─► store (sqlite, WAL)
                                                                            │
          ┌───────────────────────────────┬──────────────────────┬─────────┴──────────┐
          ▼                               ▼                      ▼                    ▼
   reconcile/calibrate            detectors (lanes)      what-if engines       finops aggregates
   (ledger vs reference)          → Finding              → Projection          (RawAggregate)
          │                               │                      │                    │
          └──────────────► attribution.kanon.publish (k ≥ 5) ◄───┴────────────────────┘
                                          │
          render: terminal · HTML (fleet, per-team showback) · result@2 JSON · CSV · FOCUS
          · SARIF (CI) · OTLP metrics · managed-settings patch · rollout plan · signed receipt
```

### 2.4 Canonical records (`tokenbill/core/records.py`, WP0)

#### 2.4.1 Conventions

- **Time**: `float` unix seconds, UTC. `ts_start` is the **request start**. Cache TTL is measured from request start and generation time counts against it `[anth-ttl-start-1h]` `[fp-ttl-from-request-start]`. For Claude Code transcripts, request start is the timestamp of the last `user`/`attachment` entry before the first assistant entry of that `message.id` (empirical importer rule).
- **Tokens**: `int ≥ 0`, **disjoint buckets** that sum to billed input: `uncached_input + cache_read + cache_write_5m + cache_write_1h + cache_write_other = total_input`. `output` is billed output **including** thinking/reasoning. `reasoning_output` is an informational subset or `None` `[ent-usage-normalization]`.
- **Money**: `int` nano-USD. JSON renders money as `{"usd": "<decimal string>", "nusd": <int>}`. Receipts use micro-USD integers `[signed-receipts]`.
- **Identifiers**: raw personal identifiers (email, account UUID, OS user, cwd, repo URL, file paths) never enter records. They pass through `core.ids.Pseudonymizer` (HMAC-SHA256, org key) at the adapter boundary, or at central ingest when `identity_mode = "hmac-at-ingest"` (§2.8). Provider request/message IDs are opaque and kept raw for joins.
- **Keys**: `request_key = "<provider>:<id-kind>:<id>"`, e.g. `anthropic:req:req_011CX…` (provider request id; preferred because it joins transcripts, OTel and gateways), `anthropic:msg:msg_01…` (message id when no request id), `otel:creq:<client_request_id>`, `trace1:<source_hash>:<run_id>:<index>`. `lane_key = HMAC(session_id + "/" + (agent_id or "main"))`. `run_id` from trace@1 files is namespaced by source hash, fixing the cross-file collision `[cb-dup-runid]`.

#### 2.4.2 Enums

```python
from enum import Enum

class Basis(str, Enum):          # §2.10
    EXACT = "exact"; ESTIMATED = "estimated"; MEASURED = "measured"; VERIFIED = "verified"

class PriceBasis(str, Enum):
    LIST = "list"; CONTRACT = "contract"; INVOICE = "invoice"; PROVIDER_REPORTED = "provider-reported"

class RequestClass(str, Enum):   # Claude Code gateway header request-class + OTel query_source
    MAIN = "main"; SUBAGENT = "subagent"; WORKFLOW = "workflow"; COMPACTION = "compaction"
    AUXILIARY = "auxiliary"; API = "api"; UNKNOWN = "unknown"

class WorkloadClass(str, Enum):
    INTERACTIVE = "interactive"; CI = "ci"; SCHEDULED = "scheduled"; EVAL = "eval"
    BATCH_JOB = "batch_job"; SERVICE = "service"; UNKNOWN = "unknown"

class IterationKind(str, Enum):  # Anthropic usage.iterations[].type, generalized
    MESSAGE = "message"; FALLBACK_MESSAGE = "fallback_message"; COMPACTION = "compaction"
    ADVISOR = "advisor_message"; UNKNOWN = "unknown"

class UsageSource(str, Enum):
    FINAL = "final"                         # final usage frame
    MESSAGE_START_ONLY = "message_start_only"  # placeholder output (CC no-stop calls)
    ESTIMATED = "estimated"                 # synthesized (e.g. unlogged compaction call)
    AGGREGATED = "aggregated"               # came from a bucketed source

class WriteTtlBasis(str, Enum):
    REPORTED = "reported"        # 5m/1h split reported by provider
    SINGLE_CLASS = "single_class"  # provider has one write class (OpenAI 5.6+ 30m) → cache_write_other
    UNKNOWN = "unknown"          # writes reported without TTL split (Claude Code OTel) → cache_write_other

class Outcome(str, Enum):
    SUCCESS = "success"; HTTP_ERROR = "http_error"; ABORTED = "aborted"; TIMEOUT = "timeout"
    REFUSED_PRE_OUTPUT = "refused_pre_output"; REFUSED_MID_STREAM = "refused_mid_stream"
    UNKNOWN = "unknown"

class EventKind(str, Enum):
    COMPACTION = "compaction"; MODEL_FALLBACK = "model_fallback"; API_ERROR = "api_error"
    TOOL_RESULT = "tool_result"; ATTACHMENT = "attachment"; HUMAN_PROMPT = "human_prompt"
    CONTEXT_EDIT = "context_edit"; COST_STATE = "cost_state"; DATA_QUALITY = "data_quality"
    SESSION_META = "session_meta"

class Confidence(str, Enum):
    HIGH = "high"; MEDIUM = "medium"; LOW = "low"

class Audience(str, Enum):
    ORG = "org"      # publishable after k-anonymity
    SELF = "self"    # only shown to the user it concerns (tokenbill me)

class FixKind(str, Enum):
    SETTING = "setting"; ENV = "env"; HOOK = "hook"; GATEWAY = "gateway"; CODE = "code"
    HABIT = "habit"; COMMERCIAL = "commercial"

class LeverClass(str, Enum):     # realization-rate priors, §3 F8
    RATE = "rate"                          # pure price arithmetic (RR ≈ 1.0)
    DETERMINISTIC_TRANSFORM = "deterministic_transform"   # TTL, cache placement (RR 0.8–1.0)
    TRAJECTORY_CHANGING = "trajectory_changing"           # compaction, model, effort (RR −0.2–1.0)
    BEHAVIOURAL = "behavioural"                            # nudges (field-only)
```

#### 2.4.3 Token buckets and usage lines

```python
@dataclass(frozen=True, slots=True)
class TokenBuckets:
    """Disjoint billed token counts for ONE usage line. Sum of input buckets = billed input."""
    uncached_input: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_write_other: int = 0          # see WriteTtlBasis on the owning UsageRecord
    output: int = 0                     # billed output incl. thinking/reasoning
    reasoning_output: int | None = None # subset of output, informational; None = not reported

    @property
    def cache_write(self) -> int: ...   # 5m + 1h + other
    @property
    def total_input(self) -> int: ...   # uncached + read + cache_write  ("context tokens")
    def __add__(self, other: "TokenBuckets") -> "TokenBuckets": ...
    def validate(self) -> None: ...     # all ≥ 0, ≤ 2**53, reasoning_output ≤ output


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """One billable (or informational) usage line: one iteration of one attempt."""
    model: str                                   # served model id, raw as reported
    tokens: TokenBuckets
    kind: IterationKind = IterationKind.MESSAGE
    billable: bool = True                        # False: pre-output refusal (informational usage)
    billable_confidence: Confidence = Confidence.HIGH  # LOW: tiny-output refusal (4–9 tokens)
    write_ttl_basis: WriteTtlBasis = WriteTtlBasis.REPORTED
    speed: str | None = None                     # "standard" | "fast"
    service_tier: str | None = None              # "standard" | "batch" | "priority" | "flex" | "fast" | "scale" | provider value
    inference_geo: str | None = None             # "us" | "global" | None ("not_available" → None)
    endpoint_scope: str | None = None            # "global" | "regional" | "multi_region" (Bedrock/Vertex)
    batch: bool = False
    long_context_band: bool = False              # request billed at a >threshold band (OpenAI >272K, Gemini >200K, xAI ≥200K)
    web_search_requests: int = 0
    web_fetch_requests: int = 0
    code_exec_seconds: float = 0.0
    provider_reported_cost_nusd: int | None = None   # xAI cost ticks, OpenRouter usage.cost, Cursor chargedCents
    usage_source: UsageSource = UsageSource.FINAL
    output_estimated_extra: int = 0              # output tokens estimated as missing (placeholder usage)
```

#### 2.4.4 Attempts, requests, attribution

```python
@dataclass(frozen=True, slots=True)
class Attempt:
    """One HTTP attempt of a logical request. Most sources only expose the final one."""
    attempt_no: int                          # 0-based
    ts_start: float | None = None
    ts_first_token: float | None = None      # cache entry becomes readable here [anth-concurrency-fanout]
    ts_end: float | None = None
    outcome: Outcome = Outcome.SUCCESS
    http_status: int | None = None
    error_class: str | None = None           # "overloaded" | "rate_limit" | "timeout" | "connection" | "prompt_too_long"
                                             # | "spend_cap" | "thinking_binding" | "invalid_request" | "auth" | provider value
    retry_layer: str | None = None           # "sdk" | "agent" | "gateway"
    retry_after_s: float | None = None
    should_retry: bool | None = None         # x-should-retry
    usage: tuple[UsageRecord, ...] = ()      # iterations; () when the attempt billed nothing known


@dataclass(frozen=True, slots=True)
class Attribution:
    org_id: str | None = None
    workspace_id: str | None = None          # the prompt-cache isolation boundary on 1P / P-AWS / Foundry
    api_key_pid: str | None = None           # HMAC of api key id
    user_pid: str | None = None              # HMAC; never raw; never a group_by in ORG audience
    team: str | None = None
    cost_center: str | None = None
    project: str | None = None
    repo_pid: str | None = None              # HMAC of normalized repo URL
    environment: str | None = None           # "prod" | "dev" | "ci" | ...
    workload_class: WorkloadClass = WorkloadClass.UNKNOWN
    workload_confidence: Confidence = Confidence.LOW
    agent_product: str | None = None         # "claude_code" | "claude_agent_sdk" | "codex" | "api" | framework name
    agent_name: str | None = None            # Claude Code agent.name / attributionAgent
    skill: str | None = None                 # skill.name / attributionSkill
    mcp_server: str | None = None            # mcp_server.name / attributionMcpServer
    plugin: str | None = None
    arm: str | None = None                   # experiment tags (OTEL_RESOURCE_ATTRIBUTES tokenbill.arm/wave)
    wave: str | None = None
    tags: Mapping[str, str] = field(default_factory=dict)  # allowlisted resource attributes only


@dataclass(frozen=True, slots=True)
class Request:
    """One logical model request (one provider response id)."""
    request_key: str
    provider: str                     # "anthropic" | "openai" | "google" | "aws" | "azure" | "openrouter" | "xai" | "deepseek" | "mistral"
    channel: str                      # "1p" | "bedrock" | "vertex" | "foundry" | "claude-platform-aws" | "azure-openai"
                                      # | "gemini-api" | "openrouter" | "gateway:<name>"
    source_kind: str                  # adapter kind that produced the record
    fidelity: int                     # 3 = final usage + TTL split + iterations; 2 = final usage, no split;
                                      # 1 = placeholder / estimated; 0 = bucket-derived
    ts_start: float
    ts_end: float | None = None
    ttft_ms: int | None = None
    duration_ms: int | None = None
    model_requested: str | None = None
    attempts: tuple[Attempt, ...] = ()
    session_pid: str | None = None
    lane_key: str | None = None
    parent_lane_key: str | None = None
    request_class: RequestClass = RequestClass.UNKNOWN
    agent_type: str | None = None     # "general-purpose" | "Explore" | "workflow-subagent" | ...
    stop_reason: str | None = None
    effort: str | None = None         # "low" | "medium" | "high" | "xhigh" | "max"
    thinking: str | None = None       # "adaptive" | "enabled" | "disabled"
    diag_reason: str | None = None    # cache_miss_reason.type (Anthropic) / prompt_cache_diagnostics reason (OpenAI)
    diag_missed_tokens: int | None = None   # magnitude estimate only, never priced [anth-cache-diagnostics-beta]
    provider_request_id: str | None = None
    client_request_id: str | None = None
    agent_version: str | None = None  # Claude Code version etc.
    entrypoint: str | None = None
    cwd_pid: str | None = None        # HMAC(cwd): Claude Code cache scope is per machine+directory
    attribution: Attribution = field(default_factory=Attribution)
    ext: Mapping[str, str | int | float | bool | None] = field(default_factory=dict)  # scalars, ≤128 chars

    @property
    def usage_lines(self) -> tuple[UsageRecord, ...]: ...   # all attempts' iterations, in order
    @property
    def billed_tokens(self) -> TokenBuckets: ...            # Σ tokens over billable lines
    @property
    def model_served(self) -> str | None: ...               # model of the last billable line
```

#### 2.4.5 Events, lanes, sessions, reference buckets

```python
@dataclass(frozen=True, slots=True)
class Event:
    """Content-free side information. `data` schema is fixed per kind (below)."""
    event_key: str                     # HMAC(source, file, uuid) or provider id
    kind: EventKind
    ts: float
    session_pid: str | None = None
    lane_key: str | None = None
    request_key: str | None = None
    data: Mapping[str, str | int | float | bool | None] = field(default_factory=dict)

# data schemas:
#  COMPACTION     {trigger:"auto"|"manual", pre_tokens:int, post_tokens:int, duration_ms:int, dropped_tokens:int|None}
#  MODEL_FALLBACK {from_model:str, to_model:str, trigger:"refusal"|"availability"|"unknown", credited:bool|None}
#  API_ERROR      {status:int|None, error_class:str, retry_attempt:int|None, max_retries:int|None, retry_in_ms:int|None}
#  TOOL_RESULT    {tool:str, mcp_server:str|None, chars:int, images:int, is_error:bool, persisted_bytes:int|None}
#  ATTACHMENT     {att_type:str, chars:int}          # skill_listing, mcp_instructions_delta, deferred_tools_delta, ...
#  HUMAN_PROMPT   {}                                  # a human-typed prompt (for $/prompt)
#  CONTEXT_EDIT   {cleared_input_tokens:int, edit_type:str}
#  COST_STATE     {reported_total_nusd:int, reporter:str}   # e.g. CC cost-state entries (coverage cross-check)
#  DATA_QUALITY   {code:str, count:int, detail:str}  # schema drift, placeholder usage, sum-check failures
#  SESSION_META   {agent_type:str|None, spawn_depth:int|None, parent_tool_use:bool}


@dataclass(frozen=True, slots=True)
class Lane:
    """A cache lineage: consecutive requests that can read each other's prefix cache."""
    lane_key: str
    session_pid: str | None
    parent_lane_key: str | None
    request_class: RequestClass
    agent_type: str | None
    user_pid: str | None
    attribution: Attribution
    first_ts: float
    last_ts: float
    n_requests: int
    models: tuple[str, ...]
    ttl_observed: str                  # "5m" | "1h" | "mixed" | "unknown"
    lane_exact: bool                   # False when reconstructed heuristically (e.g. OTel without agent ids)


@dataclass(frozen=True, slots=True)
class Session:
    session_pid: str
    user_pid: str | None
    lane_keys: tuple[str, ...]
    first_ts: float
    last_ts: float
    n_human_prompts: int
    list_nusd: int


@dataclass(frozen=True, slots=True)
class UsageBucket:
    """Reference (aggregated) usage/cost from a provider report. Used for reconciliation, never double-counted
    with the per-request ledger."""
    ref_key: str
    source: str       # "anthropic.usage_report" | "anthropic.cost_report" | "anthropic.cc_analytics"
                      # | "anthropic.enterprise_analytics" | "openai.usage" | "openai.costs" | "aws.cur" | "file"
    bucket_start: float
    bucket_width_s: int              # 60 | 3600 | 86400
    dims: Mapping[str, str]          # workspace_id, model, api_key_id, service_tier, context_window,
                                     # inference_geo, speed, user_pid, product, cost_type, token_type, description
    tokens: TokenBuckets | None = None
    web_search_requests: int | None = None
    amount_nusd: int | None = None       # post-discount where the source provides it
    list_amount_nusd: int | None = None  # Enterprise Analytics list_amount
    currency: str = "USD"
    provisional: bool = False            # inside a revision window (Enterprise Analytics: 30 days)
    fetched_at: float = 0.0
```

#### 2.4.6 Priced records, findings, projections

```python
@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """nUSD by component for one usage line (list basis unless stated)."""
    uncached: int = 0; cache_read: int = 0; cache_write_5m: int = 0; cache_write_1h: int = 0
    cache_write_other: int = 0; output: int = 0; web_search: int = 0; code_exec: int = 0
    # premium decomposition (already included in the components above; informational):
    premium_fast: int = 0; premium_geo: int = 0; premium_regional: int = 0; premium_long_context: int = 0
    batch_discount: int = 0            # negative or 0
    estimated_output_extra: int = 0    # priced separately, basis ESTIMATED
    @property
    def total(self) -> int: ...        # excludes informational premium/discount fields; excludes estimated extra


@dataclass(frozen=True, slots=True)
class LinePrice:
    line_index: int
    priced: bool                       # False: unknown model/rate → excluded from $ totals, counted in coverage
    list: CostBreakdown | None
    contract_nusd: int | None
    basis: Basis                       # EXACT, or ESTIMATED when TTL split unknown / usage estimated
    write_ttl_uncertainty_nusd: int = 0   # (all-1h − all-5m) pricing of cache_write_other when basis UNKNOWN
    rate_row_id: str | None = None
    reason: str | None = None          # why unpriced / why estimated


@dataclass(frozen=True, slots=True)
class PricedRequest:
    request: Request
    lines: tuple[LinePrice, ...]
    list_nusd: int                     # Σ priced billable lines (exact part)
    contract_nusd: int | None
    estimated_nusd: int                # Σ estimated components (output extra, synthetic requests)
    unpriced_lines: int
    rate_card_id: str


@dataclass(frozen=True, slots=True)
class Window:
    start: float; end: float           # [start, end)

@dataclass(frozen=True, slots=True)
class Finding:
    finding_id: str                    # sha256(detector_id|cause|sorted(scope)|window)[:16]
    detector_id: str                   # e.g. "cache.misses"
    detector_version: str
    cause: str                         # stable slug, e.g. "ttl-expiry", "gateway-no-cache"
    title: str                         # one line; never contains content
    audience: Audience
    scope: Mapping[str, str]           # {"team": "payments", "request_class": "main", "model": "claude-opus-5-5"}
    window: Window
    n_events: int; n_lanes: int; n_users: int
    affected_nusd: int                 # exact spend in scope the finding concerns
    waste_nusd: int                    # point estimate of avoidable spend
    waste_low_nusd: int; waste_high_nusd: int
    basis: Basis                       # ESTIMATED unless pure premium arithmetic (EXACT)
    lever_ids: tuple[str, ...]         # links to policy catalog
    fix: str
    fix_kind: FixKind
    tradeoff: bool                     # True → needs an eval/holdout before rollout
    confidence: Confidence
    evidence: Mapping[str, int | float | str | bool | None]   # content-free
    research_refs: tuple[str, ...]     # finding ids from the research base (audit trail)


@dataclass(frozen=True, slots=True)
class Projection:
    lever_id: str
    params: Mapping[str, str | int | float | bool]
    scope: Mapping[str, str]
    window: Window
    baseline_nusd: int                 # constant-rate-card replay of observed traffic (calibrated if §F6 gate passed)
    projected_nusd: int
    savings_nusd: int                  # baseline − projected (standalone)
    shapley_nusd: int | None           # overlap-deflated credit when projected jointly
    rr_p10_milli: int; rr_p50_milli: int; rr_p90_milli: int   # realization-rate prior (×1000)
    lever_class: LeverClass
    tradeoff: bool
    calibrated: bool                   # replay passed the calibration gate on this scope
    basis: Basis = Basis.ESTIMATED
    notes: tuple[str, ...] = ()
```

#### 2.4.7 Cross-provider cached-token conventions (normalizer contract)

Every adapter maps provider usage into disjoint buckets and runs a **sum-check** (buckets vs provider total). A failed check emits a `DATA_QUALITY` event and the line is kept with `fidelity` lowered, never silently fixed `[ent-usage-normalization]` `[fw-telemetry-conventions]`.

| Source | Provider "input" convention | Mapping to canonical buckets | Reasoning | Evidence |
|---|---|---|---|---|
| Anthropic Messages API (1P, Vertex rawPredict, Bedrock InvokeModel/mantle) | **exclusive** | `uncached=input_tokens`, `read=cache_read_input_tokens`, `w5m=cache_creation.ephemeral_5m_input_tokens`, `w1h=…ephemeral_1h…`, `other = cache_creation_input_tokens − w5m − w1h` (UNKNOWN basis if > 0). If `usage.iterations` present: **sum iterations** (each at its own model), excluding pre-output refusals | thinking ⊂ output_tokens | `[anth-iterations-undercount]` `[anth-cache-1h-2x]` |
| Claude Code transcripts | exclusive (as above) | as above + dedupe (§3 F2) | `output_tokens_details.thinking_tokens` | `[cc-import-dedup-message-id]` |
| Bedrock Converse | **exclusive** | `uncached=inputTokens`, `read=cacheReadInputTokens`, writes split by `cacheDetails[{ttl,inputTokens}]`, else `cacheWriteInputTokens` → other/UNKNOWN | — | `[bedrock-cache-semantics]` |
| OpenAI Responses / Chat (GPT-5.6+) | **inclusive** | `uncached = input − cached_tokens − cache_write_tokens` (must be ≥ 0), `read=cached_tokens`, `other=cache_write_tokens` (SINGLE_CLASS, 30m) | `reasoning_tokens ⊂ output_tokens` | `[oai-usage-inclusive]` `[oai-56-explicit-cache]` |
| OpenAI pre-5.6 | inclusive | as above; `cache_write_tokens` absent → 0; cached rounded to 128 | ⊂ output | `[oai-retention-defaults]` |
| Azure OpenAI | inclusive | as OpenAI | ⊂ output | `[azure-openai-caching]` |
| Gemini generateContent (AI Studio, Vertex) | **inclusive** | `uncached = promptTokenCount − cachedContentTokenCount`, `read=cachedContentTokenCount`; `toolUsePromptTokenCount` added to uncached | `thoughtsTokenCount` is **outside** candidates: `output = candidatesTokenCount + thoughtsTokenCount` | `[gemini-usage-semantics]` |
| Gemini Interactions API | inclusive | `uncached = total_input_tokens − total_cached_tokens` | `output = total_output_tokens + total_thought_tokens` (verify) | `[gemini-usage-semantics]` |
| OpenRouter | inclusive | as OpenAI (`cached_tokens`, `cache_write_tokens`); `usage.cost` → `provider_reported_cost_nusd` | `reasoning_tokens` ⊂ output | `[openrouter-accounting]` |
| xAI | inclusive (OpenAI shape) | as OpenAI; `cost_in_usd_ticks / 1e10` → provider_reported | ⊂ output | `[xai-cost-ticks]` |
| DeepSeek | split | `uncached=prompt_cache_miss_tokens`, `read=prompt_cache_hit_tokens` | — | `[deepseek-offpeak-cache]` |
| Mistral | inclusive | `read = prompt_tokens_details.cached_tokens` (64-token blocks) | — | `[mistral-optin-cache]` |
| OTel GenAI (new repo) | **inclusive** | `uncached = gen_ai.usage.input_tokens − cache_read − cache_write`; accept legacy `cache_creation.input_tokens` | `gen_ai.usage.reasoning.output_tokens` ⊂ output | `[otel-genai-semconv]` `[ent-otel-genai-semconv]` |
| OpenInference | inclusive | `uncached = llm.token_count.prompt − prompt_details.cache_read − prompt_details.cache_write`; count **LLM-kind spans only** | `completion_details.reasoning` | `[otel-openinference-importer]` |
| Claude Code OTel (`claude_code.api_request`, `token.usage`) | **exclusive** | `uncached=input_tokens`, `read=cache_read_tokens`, `other=cache_creation_tokens` (**UNKNOWN** TTL basis) | not split | `[cc-otel-schema]` `[cc-enterprise-telemetry-gap]` |
| Anthropic Admin usage_report | exclusive | uncached / cache_read / cache_creation 5m & 1h / output → UsageBucket | — | `[anth-admin-usage-cost-api]` |
| Claude Code Analytics API | exclusive | `model_breakdown.tokens{input,output,cache_read,cache_creation}` → UsageBucket (other/UNKNOWN) | — | `[cc-admin-analytics-apis]` |
| OpenAI Admin usage | disjoint | `input_uncached_tokens`, `input_cached_tokens`, `input_cache_write_tokens`, `output_tokens` | — | `[oai-admin-usage-costs]` |
| LiteLLM SpendLogs | inclusive (`prompt_tokens` includes cache hits) | uncached = prompt − cache_read − cache_write (fields per export; verify) | — | `[litellm-gateway-injection]` |

The table ships as data in `ingest/conventions.py` keyed by `(source kind or otel.scope.name, version range, provider route)`, with golden fixtures built from the documented examples (e.g. Vertex `3 + 900 + 1,054 = 1,957`) and from the framework bug reproductions (CrewAI 17 vs 17,119; Strands +50%) `[fw-telemetry-conventions]`.

#### 2.4.8 Interchange format `tokenbill/usage@1`

JSONL. One record per line, content-free by construction. This is how laptops, CI runners and the recorder hand data to a central store without Token Bill doing any networking.

```json
{"schema":"tokenbill/usage@1","type":"request","request_key":"anthropic:req:req_011…","provider":"anthropic",
 "channel":"1p","source_kind":"claude-code","fidelity":3,"ts_start":1790000000.123,"ts_end":1790000004.5,
 "session_pid":"h:7f3a…","lane_key":"h:c21e…","request_class":"main","effort":"xhigh",
 "attempts":[{"attempt_no":0,"outcome":"success","usage":[{"model":"claude-opus-5-5","kind":"message",
   "tokens":{"uncached_input":3,"cache_read":412001,"cache_write_5m":0,"cache_write_1h":2210,"cache_write_other":0,
             "output":812,"reasoning_output":400},"speed":"standard","service_tier":"standard","usage_source":"final"}]}],
 "attribution":{"user_pid":"h:0b9e…","team":"payments","workload_class":"interactive","agent_product":"claude_code"},
 "stop_reason":"tool_use","agent_version":"2.1.280","cwd_pid":"h:55aa…"}
{"schema":"tokenbill/usage@1","type":"event","event_key":"h:…","kind":"compaction","ts":1790000100.0,
 "lane_key":"h:c21e…","data":{"trigger":"auto","pre_tokens":986666,"post_tokens":20283,"duration_ms":118000}}
```

Identity transport: in `identity_mode = "hmac-at-ingest"`, a collector may write a raw `attribution.user_ref` (e.g. the git email). `core.records.from_json(line, ids=…)` converts it to `user_pid` at central ingest and drops it, so the store never contains it. In `hmac-at-source` mode `user_ref` is rejected.

Validation, enforced by `core.records.from_json`: unknown top-level keys are rejected. Strings over 256 characters are rejected (defence against content smuggling), except in the `title`/`fix` fields of findings, which are generated by Token Bill. HMAC digests carry an `h:` prefix. A JSON Schema lives at `docs/schemas/usage-1.schema.json`.

### 2.5 Pricing engine (`tokenbill/rates`, WP1)

**Rate card data** (`rates/data/*.json`, schema `tokenbill/ratecard@1`): effective-dated rows, one per `(provider, channel, model)`, each with a source URL and a `verified_on` date `[anth-evidence-drift]` `[gemini-effective-dated-prices]`.

```python
@dataclass(frozen=True, slots=True)
class RateRow:
    row_id: str                        # "anthropic/1p/claude-opus-5-5@2026-09-22"
    provider: str
    channel: str
    model: str                         # canonical id
    effective_from: date
    effective_to: date | None
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    cache_read_mult: Decimal | None    # None → provider has no cache reads
    cache_write_5m_mult: Decimal | None
    cache_write_1h_mult: Decimal | None
    cache_write_other_mult: Decimal | None   # OpenAI 5.6+: 1.25; None → price 'other' by unknown-TTL policy
    min_cacheable_tokens: int | None
    batch_mult: Decimal | None         # None → batch unsupported
    fast_input_per_mtok: Decimal | None
    fast_output_per_mtok: Decimal | None
    tier_mults: Mapping[str, Decimal]  # {"flex": 0.5, "priority": 1.75, "fast": 2.0}
    geo_mults: Mapping[str, Decimal]   # {"us": 1.1}
    regional_mult: Decimal | None      # 1.1 on Bedrock/Vertex regional & multi-region endpoints
    long_context_threshold: int | None # whole request billed at band rates above this input size
    long_context_input_per_mtok: Decimal | None
    long_context_output_per_mtok: Decimal | None
    long_context_cache_read_per_mtok: Decimal | None
    long_context_cache_write_per_mtok: Decimal | None
    time_of_day_mults: tuple[tuple[str, Decimal], ...]   # DeepSeek peak windows (cron-like spec, UTC)
    tokenizer_family: str              # "claude-2026" (4.7+), "claude-legacy", "openai-o200k", ...
    retired_on: date | None
    source_url: str
    verified_on: date
    notes: str = ""
```

**Line pricing algorithm** (`Pricer.price_line`):

1. Resolve the row: `resolve.canonical_model(raw, provider, channel)` strips Bedrock `anthropic.` and `us.`/`eu.`/`apac.`/`global.` prefixes and `-v1:0` suffixes, Vertex `@YYYYMMDD`, dated `-YYYYMMDD` suffixes and `[1m]` markers, and maps OpenRouter `anthropic/claude-sonnet-4.5` → `claude-sonnet-4-5`. Every id listed in `[cb-pricing-coverage]` is a test case. Pick the row whose `[effective_from, effective_to)` contains `ts_start`.
2. If no row exists, return `priced=False, reason="unknown model"`. Totals report "$X priced + N unpriced lines (T tokens)". The org total is never blanked `[cb-pricing-coverage]`.
3. If the line is not billable, return 0 with an informational breakdown.
4. Base rates: `(in, out) = (fast_in, fast_out) if speed == "fast" else (input, output)`. If `long_context_band`, use the band rates for all categories (the whole request is billed at the band).
5. Per-token-class cost in `Decimal` $/MTok = µUSD per token:
   `uncached·in + read·in·read_mult + w5m·in·w5m_mult + w1h·in·w1h_mult + other·in·other_mult + output·out`.
   If `other > 0` and `write_ttl_basis == UNKNOWN`: price `other` with the **unknown-TTL policy** (config `unknown_write_ttl`: `"policy"` default = the lane's configured or observed TTL, else the channel default: Claude Code API-key/cloud main 5m, subscription main 1h, subagents 5m `[cc-ttl-policy]`). Set `basis = ESTIMATED` and `write_ttl_uncertainty_nusd = other·in·(w1h_mult − w5m_mult)`.
6. Multiply by `geo_mults[inference_geo]`, `regional_mult` (if `endpoint_scope ≠ "global"`), `tier_mults[service_tier]` (flex, priority) and `batch_mult` (if batch). Multipliers stack `[anth-batch-stacking]` `[anth-modifiers-geo-fast-priority]`.
7. Server tools: `web_search_requests × $0.01`; code execution at $0.05/container-hour, with the monthly 1,550 free hours applied as a separate credit line at rollup, never per call `[anth-web-search-controls]` `[anth-ptc-code-exec]`.
8. Premium decomposition (informational): `premium_fast = cost − cost_at_standard_rates`, `premium_geo = cost·(1 − 1/1.1)`, and so on. This powers MODEL-02 as **exact** figures.
9. Quantize each component to nUSD (ROUND_HALF_EVEN) once.
10. Contract: if a contract card is loaded, a per-model override **replaces** the list rates (`input`, `output`, `cacheRead`, `cacheWrite`), and a contract `multiplier` multiplies the result. Output both `list` and `contract_nusd`. The contract file mirrors Claude Code's managed `modelPricing` (`multiplier` + `overrides` with `input/output/cacheRead/cacheWrite`, where `cacheWrite` covers both 5m and 1h writes). `tokenbill pricing contract --emit-model-pricing` writes that block, so developers' `/usage` and OTel show contract rates `[cc-list-price-estimates]` `[cc-anthropic-discount-visibility]`.
11. `provider_reported_cost_nusd`, when present, is carried beside the computed cost, and a per-line delta above 1% emits `DATA_QUALITY` (xAI ticks and OpenRouter cost are billed truth for their channels) `[xai-cost-ticks]` `[openrouter-accounting]`.

**Interfaces** (`core/protocols.py`):

```python
class Pricer(Protocol):
    card_id: str                                     # sha256 of canonical card JSON (+ contract hash)
    def price_line(self, line: UsageRecord, *, provider: str, channel: str, at: float,
                   ttl_hint: str | None = None) -> LinePrice: ...
    def price_request(self, req: Request, *, ttl_hint: str | None = None) -> PricedRequest: ...
    def row(self, model: str, *, provider: str, channel: str, at: float) -> RateRow | None: ...
    def token_class_cost(self, model: str, token_class: str, n: int, *, provider: str, channel: str,
                         at: float, speed: str = "standard", geo: str | None = None,
                         batch: bool = False, endpoint_scope: str = "global") -> int | None: ...  # nUSD
```

`token_class_cost` is the primitive used by every what-if and detector for counterfactual rates. `token_class ∈ {"uncached","cache_read","cache_write_5m","cache_write_1h","cache_write_other","output"}`.

**Drift control**: `tokenbill pricing verify` (offline) checks invariants: every row has a source and a `verified_on` date; rows older than 45 days warn; `claude-*` rows have 5m/1h multipliers; no overlapping effective ranges. `--online` fetches `https://platform.claude.com/docs/en/about-claude/pricing.md` and other listed sources, parses the model table, and fails on any disagreement. It is designed for a scheduled CI job. Machine feeds (LiteLLM map, OpenRouter models API) are cross-checks only, never primary, because they disagree with primary sources (OpenRouter listed gpt-5.6-sol at $2/$10 vs OpenAI's $4/$20) `[price-feed-crosscheck]`.

**Legacy bridge**: `tokenbill/pricing.py` (v0.1 API) stays and gains the Opus 5.5 row and a `cache_write_1h_multiplier`. An integration test asserts its rows equal the corresponding `rates/data/anthropic.json` 1P rows.

### 2.6 Storage (`tokenbill/store`, WP6)

SQLite (stdlib), one file per org store, WAL mode, `PRAGMA foreign_keys=ON`, `synchronous=NORMAL`, file mode `0600`, directory `0700` `[ent-scale-storage]`. The schema version lives in `meta` and migrations are forward-only.

```sql
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
  -- schema_version, created_at, tool_version, key_fingerprint (sha256 of HMAC key id, not the key)
CREATE TABLE sources (source_id INTEGER PRIMARY KEY, kind TEXT NOT NULL, locator_hash TEXT NOT NULL,
  label TEXT, first_seen REAL, last_ingest REAL, UNIQUE(kind, locator_hash));
CREATE TABLE cursors (source_id INTEGER NOT NULL REFERENCES sources, unit_hash TEXT NOT NULL,
  size INTEGER, mtime REAL, byte_offset INTEGER NOT NULL, lines INTEGER, quarantined INTEGER,
  head_sha TEXT,                         -- sha256 of first 4 KiB: detects rotation/truncation → re-read
  PRIMARY KEY (source_id, unit_hash));
CREATE TABLE requests (
  request_key TEXT PRIMARY KEY, provider TEXT NOT NULL, channel TEXT NOT NULL, source_kind TEXT NOT NULL,
  fidelity INTEGER NOT NULL, sources_mask INTEGER NOT NULL,
  ts_start REAL NOT NULL, ts_end REAL, ttft_ms INTEGER, duration_ms INTEGER, day TEXT NOT NULL, -- 'YYYY-MM-DD' UTC
  session_pid TEXT, lane_key TEXT, parent_lane_key TEXT, request_class TEXT, agent_type TEXT,
  model_requested TEXT, model_served TEXT, effort TEXT, thinking TEXT, speed TEXT, service_tier TEXT,
  inference_geo TEXT, endpoint_scope TEXT, batch INTEGER, stop_reason TEXT, usage_source TEXT,
  n_attempts INTEGER, outcome TEXT, diag_reason TEXT, diag_missed_tokens INTEGER,
  provider_request_id TEXT, client_request_id TEXT, agent_version TEXT, entrypoint TEXT, cwd_pid TEXT,
  org_id TEXT, workspace_id TEXT, api_key_pid TEXT, user_pid TEXT, team TEXT, cost_center TEXT, project TEXT,
  repo_pid TEXT, environment TEXT, workload_class TEXT, agent_product TEXT, agent_name TEXT, skill TEXT,
  mcp_server TEXT, plugin TEXT, arm TEXT, wave TEXT,
  uncached_input INTEGER, cache_read INTEGER, cache_write_5m INTEGER, cache_write_1h INTEGER,
  cache_write_other INTEGER, write_ttl_basis TEXT, output INTEGER, reasoning_output INTEGER,
  web_search_requests INTEGER, output_estimated_extra INTEGER,
  list_nusd INTEGER, contract_nusd INTEGER, estimated_nusd INTEGER, write_ttl_uncertainty_nusd INTEGER,
  premium_fast_nusd INTEGER, premium_geo_nusd INTEGER, premium_regional_nusd INTEGER,
  provider_reported_nusd INTEGER, unpriced_lines INTEGER, rate_card_id TEXT, ext TEXT);
CREATE INDEX req_lane ON requests(lane_key, ts_start);
CREATE INDEX req_day_team ON requests(day, team);
CREATE INDEX req_day_ws_model ON requests(day, workspace_id, model_served);
CREATE INDEX req_session ON requests(session_pid, ts_start);
CREATE TABLE usage_lines (request_key TEXT NOT NULL REFERENCES requests ON DELETE CASCADE,
  attempt_no INTEGER NOT NULL, iteration_no INTEGER NOT NULL, kind TEXT, model TEXT, billable INTEGER,
  billable_confidence TEXT, uncached_input INTEGER, cache_read INTEGER, cache_write_5m INTEGER,
  cache_write_1h INTEGER, cache_write_other INTEGER, output INTEGER, list_nusd INTEGER, contract_nusd INTEGER,
  basis TEXT, PRIMARY KEY (request_key, attempt_no, iteration_no));
CREATE TABLE events (event_key TEXT PRIMARY KEY, kind TEXT NOT NULL, ts REAL NOT NULL, day TEXT NOT NULL,
  session_pid TEXT, lane_key TEXT, request_key TEXT, data TEXT NOT NULL);
CREATE INDEX ev_lane ON events(lane_key, ts);
CREATE TABLE lanes (lane_key TEXT PRIMARY KEY, session_pid TEXT, parent_lane_key TEXT, request_class TEXT,
  agent_type TEXT, user_pid TEXT, team TEXT, first_ts REAL, last_ts REAL, n_requests INTEGER,
  models TEXT, ttl_observed TEXT, lane_exact INTEGER);            -- derived; rebuilt by `store rebuild-lanes`
CREATE TABLE ref_buckets (ref_key TEXT PRIMARY KEY, source TEXT NOT NULL, bucket_start REAL NOT NULL,
  bucket_width_s INTEGER NOT NULL, day TEXT NOT NULL, dims TEXT NOT NULL, uncached_input INTEGER,
  cache_read INTEGER, cache_write_5m INTEGER, cache_write_1h INTEGER, cache_write_other INTEGER,
  output INTEGER, web_search_requests INTEGER, amount_nusd INTEGER, list_amount_nusd INTEGER,
  currency TEXT, provisional INTEGER, fetched_at REAL);
CREATE TABLE cluster_day (day TEXT NOT NULL, team TEXT, workspace_id TEXT, cost_center TEXT,
  workload_class TEXT, arm TEXT, wave TEXT, active_users INTEGER NOT NULL, requests INTEGER NOT NULL,
  list_nusd INTEGER NOT NULL, contract_nusd INTEGER,
  PRIMARY KEY (day, team, workspace_id, cost_center, workload_class, arm, wave));
  -- materialized by `store rebuild-rollups` and always before retention nulls user_pid;
  -- source of 'cost per active developer-day' for showback, forecasts and verification panels
CREATE TABLE findings (finding_id TEXT, run_id TEXT, json TEXT NOT NULL, PRIMARY KEY (finding_id, run_id));
CREATE TABLE projections (projection_id TEXT, run_id TEXT, json TEXT NOT NULL, PRIMARY KEY (projection_id, run_id));
CREATE TABLE receipts (receipt_id TEXT PRIMARY KEY, created REAL, json TEXT NOT NULL, dsse TEXT);
CREATE TABLE audit_log (ts REAL NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, detail TEXT NOT NULL);
```

**Merge semantics** (`store/merge.py`). Upserts are idempotent and order-independent:

| Field group | Rule on `request_key` conflict |
|---|---|
| token buckets, `usage_lines`, costs, `usage_source`, `stop_reason` | take the incoming row if `incoming.fidelity > existing.fidelity`. At **equal** fidelity **and** the same `source_kind`, take the row with the larger `output` (Claude Code split-entry rule, max output wins `[cc-import-dedup-message-id]`); ties keep the existing row |
| attribution dims (team, skill, mcp_server, agent_name, workload…) | fill if NULL; never overwrite non-NULL (OTel adds skill/MCP/team to a transcript-sourced row) |
| `sources_mask` | bitwise OR (bit per source kind) |
| timestamps | `ts_start = MIN`, `ts_end = MAX` of non-NULL |
| `diag_reason` | fill if NULL |

Cross-source joins work because Claude Code transcripts carry `requestId` and OTel `api_request` carries `request_id` (the same provider id). Both map to `anthropic:req:<id>`. Invariant tests check that the sum of request totals equals the sum of line totals, and that re-ingesting any source is a no-op `[cb-dup-runid]`.

**Cursors.** For append-only units (JSONL), ingestion resumes from `byte_offset` when `size ≥ offset` and `head_sha` is unchanged. Otherwise the whole unit is re-read (the merge makes this safe).

**Retention** (`store/retention.py`): `retention.request_days` (default 400), `retention.event_days` (default 90), `retention.individual_days` (default 90, after which `user_pid` is nulled and rows stay team-attributed; `cluster_day` is refreshed first, so active-developer-day counts survive for verification and forecasting), matching the gateway pattern of 90-day identity and 13-month spend `[ent-privacy-by-default]` `[aggregation-k5]`. `tokenbill privacy purge --user-pid h:… | --before DATE` deletes, runs `VACUUM`, and writes an `audit_log` row.

**Scale targets** (CI-gated, §3 F3): ingest ≥ 20k requests/s from usage@1; 1M-request store; full `findings` run in < 120 s; peak RSS < 400 MB; store size ≈ 1 KB per request.

**Read interfaces** (`core/protocols.py`):

```python
@dataclass(frozen=True, slots=True)
class RequestView:                       # slim priced row for detectors (built from `requests`)
    request_key: str; ts_start: float; ts_end: float | None; ttft_ms: int | None
    provider: str; channel: str; model: str; tokens: TokenBuckets; list_nusd: int
    request_class: RequestClass; effort: str | None; speed: str | None; inference_geo: str | None
    service_tier: str | None; batch: bool; stop_reason: str | None; usage_source: UsageSource
    diag_reason: str | None; iterations: tuple[UsageRecord, ...]; agent_version: str | None
    cwd_pid: str | None; n_attempts: int; outcome: Outcome; write_ttl_basis: WriteTtlBasis

@dataclass(frozen=True, slots=True)
class LaneView:
    lane: Lane
    requests: tuple[RequestView, ...]    # sorted by (ts_start, request_key)
    events: tuple[Event, ...]            # sorted by ts

@dataclass(frozen=True, slots=True)
class RawAggregate:                      # NOT renderable; must pass kanon.publish()
    group_by: tuple[str, ...]; rows: tuple["AggRow", ...]; window: Window
@dataclass(frozen=True, slots=True)
class AggRow:
    dims: Mapping[str, str | None]; n_users: int; n_requests: int; tokens: TokenBuckets
    list_nusd: int; contract_nusd: int | None; estimated_nusd: int; unpriced_lines: int
@dataclass(frozen=True, slots=True)
class PublishedAggregate:                # the only aggregate type renderers/exporters accept
    group_by: tuple[str, ...]; rows: tuple[AggRow, ...]; window: Window; k: int
    suppressed_rows: int; suppressed_users: int

class StoreReader(Protocol):
    def lanes(self, window: Window, where: Mapping[str, str] | None = None) -> Iterator[LaneView]: ...
    def aggregate(self, window: Window, group_by: Sequence[str],
                  where: Mapping[str, str] | None = None) -> RawAggregate: ...   # raises PrivacyError if 'user_pid' in group_by
    def ref_buckets(self, window: Window, source: str) -> list[UsageBucket]: ...
    def events(self, window: Window, kinds: Sequence[EventKind]) -> Iterator[Event]: ...
    def coverage(self, window: Window) -> "Coverage": ...

class StoreWriter(Protocol):
    def upsert(self, priced: Iterable[PricedRequest], source: "SourceRef") -> "UpsertStats": ...
    def add_events(self, events: Iterable[Event]) -> int: ...
    def add_ref_buckets(self, buckets: Iterable[UsageBucket]) -> int: ...
    def cursor(self, source: "SourceRef", unit_hash: str) -> "Cursor | None": ...
    def set_cursor(self, source: "SourceRef", unit_hash: str, cursor: "Cursor") -> None: ...
```

### 2.7 Lanes, sessions and TTL-in-effect

- **Claude Code transcripts**: lane = file (main session file; each `subagents/agent-*.jsonl`; each workflow agent file). `lane_exact = True`. The parent is linked through the subagent `meta.json` `toolUseId` `[cc-delegation-model-routing]`.
- **Claude Code OTel**: lane = `session.id` + `query_source` + (`agent_id` from beta `llm_request` spans if present). Without traces, all subagent calls in a session collapse into one lane with `lane_exact = False`, and detectors that need exact lanes (MISS, TTL) degrade to a `DATA_QUALITY` note `[cc-otel-schema]`.
- **API recorder / trace@1**: lane = namespaced run id. Where no ids exist, lanes are split by `(model, system hash, tools hash)` with longest-prefix assignment, which fixes the 17× interleaving under-count `[cb-lanes]`.
- **OTel GenAI / OpenInference**: lane = (`trace_id`, nearest AGENT-kind ancestor span). A leaf LLM span without a parent agent span becomes a lane of its own.
- **TTL in effect** for a lane: `1h` if any 1h writes; `5m` if only 5m writes; otherwise the configured policy (`config.policy_state`), otherwise the channel default (§2.5 step 5). `Lane.ttl_observed` records which rule applied.

### 2.8 Privacy, identity and data tiers (`attribution/`, WP7; `core/ids.py`, WP0)

- **Tiers**, stamped on every output `[ent-privacy-by-default]`:
  - `aggregate` (default): no content, no per-block fingerprints.
  - `fingerprint`: adds per-block HMAC digests for trace@1/recorder data, enabling exact-divergence localization without content `[cc-prompt-snapshot-diffing]`.
  - `content-local`: v0.1 trace@1 analysis, only on the machine that holds the trace, never exported.
- **Identity modes**:
  - `hmac-at-ingest` (default): collectors emit a raw `user_ref` only into org-controlled shipping (usage@1 files); central ingest HMACs it into `user_pid` and drops the raw value.
  - `hmac-at-source`: the org HMAC key is deployed by MDM and devices emit only digests.
  The key file is 32 random bytes, `0600`, and never logged. The key id fingerprint is stored in `meta`. Rotating the key yields new pseudonyms, and a rotation event is recorded. EDPB 01/2025: pseudonymised data is still personal data, hence retention and purge (§2.6).
- **k-anonymity** (`attribution/kanon.py`), enforced at the type level. Only `PublishedAggregate` and `Finding(audience=ORG)` with `n_users ≥ k` reach renderers and exporters:
  1. Rows with `n_users < k` (default k = 5) merge into an `"(other: <k users)"` row within the same parent group.
  2. If that merged row still has `< k` users, it merges into the smallest remaining row in the parent group (complementary suppression), so no cell can be recovered by subtracting from a total.
  3. `user_pid` is never a publishable `group_by`. `StoreReader.aggregate` raises `PrivacyError`.
  4. Findings whose scope has `n_users < k` are re-scoped to the parent (team → cost center → org) before publication `[aggregation-k5]` `[labor-law-constraints]`.
- **Individuals** see only their own data through `tokenbill me` (local transcripts, local store), compared with published anchors ($13/$30 per active day `[cc-enterprise-baseline]`) and their own history. No peer ranking, and no descriptive peer norms shown to light users `[peer-comparison-outliers]` `[leaderboard-goodhart]`.
- **Break-glass**: a runaway alert (TAIL-01) names a `session_pid` and team, never a person. Resolving a pseudonym requires the key holder, and every such lookup through `tokenbill privacy resolve` writes `audit_log`.
- **Secrets**: `attribution/secrets.py` holds stdlib regexes for provider keys (`sk-ant-`, `sk-`, AWS `AKIA…`, GitHub `ghp_`/`github_pat_`, Slack `xox`), JWTs, PEM headers, and a Shannon-entropy test. It runs in the recorder's opt-in `content-local` mode and in `tokenbill me --secrets-scan`, and reports counts by type and location class (tool_result, attachment, user prompt), never values `[ent-secrets-in-traces]`.
- **DPIA pack**: `tokenbill privacy dpia -o DIR` renders templates with the data inventory, flows, retention, access matrix, a works-council annex ("not for performance evaluation", no emotion inference, team-level k ≥ 5), and an employee notice `[labor-law-constraints]`.

### 2.9 CLI surface (`tokenbill/cli.py` + `pipeline.py`, WP13)

Global flags on every command: `--config PATH` (default `./.tokenbill/config.json`, then `~/.config/tokenbill/config.json`), `--store PATH` (default `./.tokenbill/store.db`), `--format text|json`, `-q/--quiet`, `-v/--verbose`, `--log-json` (JSON logs to stderr). Precedence: CLI flag > `TOKENBILL_*` env > config file > defaults.

**Exit codes**: 0 ok; 1 runtime error (bad input, I/O); 2 usage error; 3 gate failed (`check`, `reconcile --tolerance`, `calibrate`, `pricing verify`); 4 completed with data-quality warnings above threshold (`--strict-dq`).

| Command | Purpose | Key flags |
|---|---|---|
| `tokenbill --version` | unchanged | |
| `tokenbill demo` | **unchanged** v0.1 content-trace demo | `-o`, `--seed`, `--scenario` |
| `tokenbill demo --fleet` | synthetic fleet with planted causes (§3 F15) | `-o fleet.html`, `--seed 7`, `--devs 60`, `--days 28`, `--store` |
| `tokenbill analyze TRACE.jsonl…` | **unchanged** v0.1 behaviour on trace@1; adds `--format json`, `--rate-card PATH` | `-o`, `--model-price` (kept) |
| `tokenbill init` | create config, store, HMAC key (`0600`), print privacy notice | `--dir`, `--identity-mode`, `--k 5` |
| `tokenbill collect claude-code` | on-device, content-free transcript import → usage@1 file or local store | `--projects ~/.claude/projects`, `--out FILE`, `--since DATE`, `--user-id-from env:VAR\|git\|os\|none`, `--team NAME` |
| `tokenbill ingest KIND PATH…` | central ingest. KIND ∈ `usage`, `claude-code`, `otlp`, `anthropic-responses`, `openai`, `bedrock`, `gemini`, `openrouter`, `litellm`, `trace` | `--lenient` (quarantine bad lines to `*.quarantine.jsonl`), `--label`, `--dry-run`, `--rules RULES.json` |
| `tokenbill otlp-receive` | stdlib OTLP/HTTP-JSON receiver (optional, network) | `--host 127.0.0.1`, `--port 4318`, `--max-body 8MiB` |
| `tokenbill pull SRC` | read-only connectors (network). SRC ∈ `anthropic-usage`, `anthropic-cost`, `cc-analytics`, `enterprise-analytics`, `openai-usage`, `openai-costs` | `--start`, `--end`, `--bucket 1d\|1h\|1m`, `--replay DIR`, `--record DIR` |
| `tokenbill reconcile` | ledger vs reference; variance with explained residuals | `--against SRC`, `--period 2026-08`, `--by day,workspace,model`, `--tolerance 1%`, `--closed-only` |
| `tokenbill calibrate` | ASHRAE-G14 style gate on the replay engine | `--period`, `--grain month\|day` |
| `tokenbill findings` | run detectors; rank by waste $ | `--period`, `--team`, `--detector ID…`, `--min-usd 50`, `--format text\|json\|csv` |
| `tokenbill whatif LEVER` | one what-if. LEVER ∈ `ttl`, `compaction`, `model-map`, `batch`, `joint` | `--param k=v…`, `--scope team=…` |
| `tokenbill policy generate` | managed-settings / LiteLLM / hooks patch + projections | `--levers ID…`, `--target managed-settings\|server-managed\|litellm\|hooks`, `--current CURRENT.json`, `-o DIR` |
| `tokenbill policy plan-rollout` | randomized stepped-wedge / cluster plan | `--unit workspace\|mdm-group\|team`, `--waves 4`, `--holdback 0.1`, `--seed`, `-o DIR` |
| `tokenbill report` | fleet HTML report (org overview, levers, teams) | `-o fleet.html`, `--period`, `--k` |
| `tokenbill showback` | per-team pages / CSV / JSON | `--period`, `--out DIR`, `--format html\|csv\|json` |
| `tokenbill export focus` | FOCUS 1.4 CSV with `x_` columns | `--period`, `--grain day\|hour`, `-o focus.csv`, `--price-basis list\|contract` |
| `tokenbill export usage` | usage@1 JSONL (content-free) | `--period`, `-o` |
| `tokenbill export otlp-metrics` | OTLP/JSON metrics file (gen_ai counters + `tokenbill.*`) | `--period`, `-o` |
| `tokenbill budget check` | budgets from config vs actuals + forecast | `--period`, `--format` |
| `tokenbill watch` | anomaly/budget/runaway alerts | `--once`, `--interval 300`, `--sink stdout\|file:PATH\|webhook` |
| `tokenbill forecast` | P50/P90 month-end, with-fixes what-if, cap recommendations | `--horizon 30d`, `--caps` |
| `tokenbill verify` | savings verification, signed receipt | `--design FILE`, `--lever ID`, `--baseline A..B`, `--reporting C..D`, `--sign-key KEY`, `-o receipt.json` |
| `tokenbill receipt verify` | verify a receipt | `--allowed-signers FILE` |
| `tokenbill ab` | paired lab comparison (two usage sets + outcomes) | `--baseline FILE`, `--candidate FILE`, `--outcomes FILE`, `--boot 5000` |
| `tokenbill check` | CI cost gate (trace@1, usage@1, claude-code-action execution file) | `--baseline FILE`, `--max-cost-per-run USD`, `--min-cache-read-share 0.8`, `--fail-on new-breaker,regression`, `--sarif OUT`, `--summary-md OUT` |
| `tokenbill me` | private self-view from local transcripts | `--projects`, `--since`, `--secrets-scan` |
| `tokenbill pricing show\|verify\|contract` | rate card inspection / drift / contract | `--online`, `--emit-model-pricing` |
| `tokenbill privacy dpia\|purge\|resolve` | DPIA pack, erasure, audited pseudonym resolve | |
| `tokenbill store info\|rebuild-lanes\|rebuild-rollups\|vacuum` | maintenance | |

### 2.10 Outputs and labels

- **Terminal**: aligned tables. All text passes `core.sanitize.terminal()` (strips C0/C1 controls and ANSI escapes) `[cb-security-privacy]` `[ent-report-a11y-hardening]`. Each number is followed by a label chip, e.g. `$41,203.17 exact·list`, `~$6,100/mo est. (RR p50; $2.9k–$8.4k)`.
- **JSON** (`tokenbill/result@2`), keys in this order:
  - `schema`, `tool{name,version}`, `generated_at`, `window`
  - `rate_card{id, verified_on, contract_loaded}`
  - `privacy{tier, k, suppressed_rows, identity_mode}`
  - `coverage{requests, priced_share, unpriced_lines, sources[], lanes_exact_share, reconciled{source, delta_pct, status}}`
  - `calibration{status, nmbe, cvrmse}`
  - `totals{…}`, `teams[…]`, `findings[…]`, `projections[…]`, `policy{…}`, `data_quality[…]`

  Every money value is `{"usd":"123.45","nusd":123450000000,"basis":"exact","price_basis":"list"}`. Every percentage is `{"value":0.842,"basis":"exact"}`. A JSON-schema test rejects any numeric leaf without a sibling `basis`.
- **HTML**: one self-contained file, inline CSS and SVG, no scripts, `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">`. Every chart has a `<table>` equivalent with a `<caption>`. Colour is never the only encoding. It meets WCAG 2.2 AA contrast in both themes `[ent-report-a11y-hardening]`.
- **CSV**, **FOCUS CSV**, **SARIF 2.1.0** (for `check`), **OTLP/JSON metrics**, **Markdown** PR summary.
- **Headline sentence rule**: the headline dollar figure is always the **Shapley-deflated, RR-adjusted p50 of projections** with its range, never the sum of findings' waste `[shapley-attribution]` `[realization-rate-backtest]`.

### 2.11 Extension points (`core/protocols.py`, `core/registry.py`, WP0)

```python
@dataclass(frozen=True, slots=True)
class SourceUnit:
    kind: str; locator: str; locator_hash: str; size: int; mtime: float; append_only: bool

@dataclass
class ReadStats:
    lines: int = 0; records: int = 0; quarantined: int = 0; end_offset: int = 0
    dq: dict[str, int] = field(default_factory=dict)   # data-quality counters by code

class Adapter(Protocol):
    kind: ClassVar[str]                    # registry key, e.g. "claude-code"
    content_free: ClassVar[bool]           # True: emits no content (all built-ins)
    capabilities: ClassVar[frozenset[str]] # e.g. {"ttl_split","iterations","lanes_exact","events.compaction","diag"}
    def discover(self, locator: str) -> Iterator[SourceUnit]: ...
    def read(self, unit: SourceUnit, start_offset: int, stats: ReadStats,
             ids: "Pseudonymizer") -> Iterator[Request | Event | UsageBucket]: ...

@dataclass(frozen=True)
class DetectConfig:
    thresholds: Mapping[str, float]        # per-detector overrides from config
    k: int = 5
    policy_state: Mapping[str, str] = field(default_factory=dict)   # known org settings (promptCacheTtl per workload…)
    min_usd: int = 0                       # nUSD

@dataclass(frozen=True)
class DetectContext:
    window: Window
    reader: StoreReader
    pricer: Pricer
    config: DetectConfig
    coverage: "Coverage"                   # capabilities actually present in the data window

class Detector(Protocol):
    id: ClassVar[str]; version: ClassVar[str]
    requires: ClassVar[frozenset[str]]     # capabilities; unmet → one DATA_QUALITY finding, no guessing
    lever_ids: ClassVar[tuple[str, ...]]
    def run(self, ctx: DetectContext) -> Iterator[Finding]: ...

class Exporter(Protocol):
    format: ClassVar[str]
    def export(self, published: PublishedAggregate, findings: Sequence[Finding],
               out: IO[str], *, price_basis: PriceBasis) -> None: ...

class Connector(Protocol):
    source: ClassVar[str]
    def fetch(self, start: float, end: float, *, bucket_s: int,
              http: "HttpClient") -> Iterator[UsageBucket]: ...
```

`core/registry.py` holds `BUILTIN_ADAPTERS`, `BUILTIN_DETECTORS`, `BUILTIN_EXPORTERS`, `BUILTIN_CONNECTORS` as `{key: "module:Class"}` string maps (lazy import) and loads third-party plugins from `importlib.metadata.entry_points(group="tokenbill.adapters" | "tokenbill.detectors" | "tokenbill.exporters")`. Plugins run in-process and are therefore documented as trusted code. Rate cards are data files: `--rate-card PATH` and `rates.extra_cards` in config merge additional rows (later cards win per `row_id`).

### 2.12 Backward compatibility and the content-trace path

- `tokenbill/trace@1` files, `tokenbill demo` and `tokenbill analyze` keep working. `demo` output is byte-identical to v0.1.2 except the version string (the demo traces carry no `cache_control` keys and use claude-sonnet-5 5m writes, so the rendering and pricing changes do not move them).
- trace@1 readers accept new **optional** fields, ignored by v0.1 readers: `usage.cache_creation{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}`, `usage.iterations`, `usage.speed`, `usage.service_tier`, `usage.inference_geo`, `usage.server_tool_use`, `ttft_ms`, `request_params{thinking, effort, tool_choice, max_tokens, speed, inference_geo, betas}`, `diagnostics`, `lane`. The recorder writes them (WP14).
- `ingest/trace_v1.py` bridges trace@1 into the ledger (usage only) so recorded API workloads join fleet reporting.
- Hardening of the v0.1 path (WP14, details in §3 F14): `cache_control` stripped from the diff substrate `[cb-moving-marker]`, 1h pricing and Opus 5.5 `[cb-1h-ttl]`, per-lane comparison `[cb-lanes]`, source-namespaced run ids `[cb-dup-runid]`, partial totals instead of `None` `[cb-pricing-coverage]`, compaction and applied edits treated as expected rebuilds `[cb-breaker-taxonomy]` `[history-rewrite-kstar]`, and a first-token visibility gate when `ttft_ms` is present `[cb-concurrency]`.

---

## 3. v0.2 features, in priority order

Priority is set by fleet dollar leverage, with a dependency override: a feature whose absence makes every other number wrong comes first. Each feature lists: **What**, **Evidence**, **$ impact**, **Algorithm / rules**, **Acceptance tests**. Rates in formulas come from `Pricer.token_class_cost` and are written `r_in(m)`, `r_out(m)`, `r_rd(m)` (cache read), `r_w5(m)`, `r_w1(m)` and `r_w(m, ttl)`, all in nUSD per token for model `m` at the request's timestamp. `ctx_i = tokens_i.total_input`.

### F1. Correct ledger: canonical usage and rate engine v2 (WP0, WP1)

**What.** The records of §2.4 and the pricing engine of §2.5: disjoint buckets, 5m/1h writes, per-model read multipliers, `usage.iterations` priced per iteration at its own model, all modifiers (fast, geo, regional, batch, tiers, long-context bands, server tools), effective-dated rows with sources, contract overrides in Claude Code `modelPricing` shape, Decimal→nUSD, partial totals for unknown models.

**Evidence.** `[cc-pricing-1h-opus55-gap]` `[anth-cache-1h-2x]` `[anth-pricing-table-2026-09]` `[anth-usage-schema-exactness]` `[anth-iterations-undercount]` `[cc-iterations-fallback]` `[fp-anth-refusal-iterations]` `[advisor-iterations-undercount]` `[compaction-iterations-p0]` `[cb-pricing-coverage]` `[cb-modifiers]` `[anth-modifiers-geo-fast-priority]` `[pricing-engine-gaps]` `[ent-reconciliation]` `[ent-usage-normalization]` `[gemini-effective-dated-prices]` `[price-feed-crosscheck]` `[anth-evidence-drift]` `[claude-marketplace-ccu]`.

**$ impact (exact, correctness).** On the real corpus v0.1.2 reports $10,786.64 against $11,632.75 (−7.3%). Of that, $758.63 comes from 1h writes priced at 1.25× instead of 2×, $66.61 from 733 unpriced Opus 5.5 calls, and $20.86 from ignored fallback iterations `[cc-pricing-1h-opus55-gap]`. The 1h write line alone is understated by 37.5%. Batch, geo and fast traffic are mispriced by 10–100%.

**Algorithm.** §2.5 steps 1–11. Iteration rule (one rule covers compaction, advisor and fallback): *when `usage.iterations` has ≥ 1 entries, billed usage = Σ iterations, each priced at `iterations[].model or message.model`; an iteration of `type=message` followed by a `fallback_message` with `output_tokens == 0` is a pre-output refusal, so `billable=False`; with `0 < output ≤ 9` it is `billable=True, billable_confidence=LOW` (shown as a cost range).* Empirically, top-level usage equals the sum for 1-element lists and equals the **last** element for fallback pairs, so summing is right in both cases `[cc-iterations-fallback]`.

**Acceptance tests** (`tests/rates/`):
1. Opus 5.5, 1,000,000 cache-read tokens → exactly 200,000,000 nUSD ($0.20). Output 1M → $20.00.
2. Opus 5: 1M 1h-write → $10.00; 1M 5m-write → $6.25. Fable 5.1: 1M cache read → $0.25.
3. Fast mode Opus 5.5: 1M output → $40.00; 1M cache read → $0.40 (8 × 0.05).
4. Stacking: Opus 5.5 1M 1h-write, batch, `inference_geo="us"` → $4.40 (4 × 2 × 1.1 × 0.5).
5. Iterations: a fixture with a refused `claude-fable-5` attempt (output 2,127) plus a `claude-opus-4-8` fallback prices both. The same fixture with the refused output set to 0 prices only the fallback. A compaction iteration plus a message iteration sum both.
6. Unknown model: 99 priced requests plus 1 unknown yield totals `{priced: $X, unpriced_lines: 1}`, never `None`.
7. Every model id in `[cb-pricing-coverage]` (Bedrock `anthropic.`/`us.`/`global.` forms, `[1m]`, `@2026…`, `-2025…`, OpenRouter dotted ids) resolves to the right row or to an explicit unknown.
8. An OTel-sourced line with `cache_write_other=1M` on Opus 5.5 and unknown TTL gives `basis=estimated` and `write_ttl_uncertainty_nusd = 1M × 4 × 0.75 µUSD` = $3.00.
9. Rounding: 10⁶ lines of 1 Haiku 4.5 cache-read token equal the Decimal sum within 0.5 nUSD × lines.
10. Effective dating: an Opus 5.5 request dated before its `effective_from` is unpriced with `DATA_QUALITY: model-before-effective-date`.
11. Contract: loading a `modelPricing`-shaped file sets `contract_nusd` from overrides, and `--emit-model-pricing` round-trips byte-identically.
12. `pricing verify` (offline) fails on a row without a source, with overlapping dates, or with `verified_on` older than 45 days (warn) or 120 days (fail).
13. Legacy parity: `tokenbill/pricing.py` rows equal the 1P rows in `rates/data/anthropic.json`.

### F2. Claude Code fleet collector (WP2)

**What.** `tokenbill collect claude-code` and `ingest claude-code`: an on-device, content-free, incremental importer for `~/.claude/projects/**` that emits deduplicated `Request`s, `Event`s and lanes, into usage@1 files or a local store.

**Evidence.** `[cc-import-dedup-message-id]` `[cc-transcript-format]` `[ent-local-transcripts]` `[claude-code-telemetry-jsonl]` `[cc-hidden-calls-rollups]` `[fp-cc-missing-final-usage]` `[cc-miss-taxonomy-ground-truth]` `[cc-prompt-snapshot-diffing]` `[ent-secrets-in-traces]` `[cc-enterprise-telemetry-gap]` `[ccusage-local-parity]`.

**$ impact.** It prevents a +133% overstatement: naive per-line summing gives $27,094 against a true $11,633 `[cc-import-dedup-message-id]`. It is the only fleet source that carries the 5m/1h split, iterations, compaction metadata and server-side miss reasons, which are the inputs for F7/F8 levers worth 10–40% of Claude Code spend (upper bounds, one user).

**Algorithm.**
1. **Discovery**: all `*.jsonl` under the root except `journal.jsonl`. Lane kind comes from the path: `/subagents/` → SUBAGENT, `/workflows/` → WORKFLOW, else MAIN. Read the enumerated keys of `subagents/*.meta.json` and workflow meta (`agentType`, `model`, `toolUseId`, `spawnDepth`, `workflowPhase`) → `SESSION_META` events. Never read other keys.
2. **Per file**: stream lines (`core.streams`, 16 MiB line cap, oversize → quarantine). `json.loads`, skip non-dicts, drop duplicate `uuid` lines within the file (444 in the corpus).
3. **Assistant entries**, keyed by `message.id` (never spans files; 1:1 with `requestId`):
   - keep the entry with **max `usage.output_tokens`** (ties → later line) — output grows monotonically across split entries in 44% of calls;
   - `stop_reason` = any non-null seen; `ts_end` = last entry timestamp;
   - `ts_start` = timestamp of the latest `user` or `attachment` entry that precedes the first entry of this id in the same file (fallback: first entry timestamp);
   - fields: `message.model`, `effort`, `perTurnEffort`, `advisorModel`, `attribution{Agent,Skill,McpServer,McpTool,Plugin}` (names only), `entrypoint`, `version`, `cwd` → `cwd_pid`, `agentId` → lane, `requestId`, `isApiErrorMessage`, `apiErrorStatus`, `message.diagnostics.cache_miss_reason.{type, cache_missed_input_tokens}`, `message.input_transformations[].type`;
   - skip `model == "<synthetic>"`.
4. **Usage mapping**: `input_tokens`, `cache_read_input_tokens`, `cache_creation.ephemeral_5m_input_tokens`, `…_1h…`, remainder → `cache_write_other` (UNKNOWN), `output_tokens`, `output_tokens_details.thinking_tokens` → `reasoning_output`, `server_tool_use.{web_search_requests, web_fetch_requests}`, `speed`, `service_tier`, and `inference_geo` (`"not_available"` → None). Iterations per F1.
5. **Placeholder usage**: `stop_reason is None` and max output ≤ 20 and the next entry in the file is a `user` `tool_result` → `usage_source=MESSAGE_START_ONLY`. `output_estimated_extra` = median output of complete requests of the same `(model, request_class)` in the same collection run, priced as ESTIMATED. This is a 1.0–2.1% under-count otherwise `[fp-cc-missing-final-usage]`.
6. **System entries**: `compact_boundary` → `COMPACTION` event (`compactMetadata.preTokens, postTokens, durationMs, trigger, cumulativeDroppedTokens`) **plus** a synthetic `Request` (`request_class=COMPACTION`, `usage_source=ESTIMATED`, fidelity 1, tokens: `cache_read = preTokens`, `output = postTokens`) because compaction calls never appear as assistant entries `[cc-hidden-calls-rollups]`. `model_refusal_fallback` → `MODEL_FALLBACK` (`originalModel`, `fallbackModel`). `api_error` → `API_ERROR` (`error.status`, `retryAttempt`, `maxRetries`, `retryInMs`, class from status/connection code; message text dropped).
7. **User entries**: each `tool_result` block → `TOOL_RESULT {tool: name from the earlier tool_use id, chars: text length, images: count, is_error, persisted_bytes: toolUseResult.persistedOutputSize}`. A human prompt (`origin.kind=="human"`, or not meta, not compact summary, not sidechain, no tool_result) on a MAIN lane → `HUMAN_PROMPT`. `toolUseResult.totalTokens` and `usage` rollups are **ignored as spend** `[cc-hidden-calls-rollups]`.
8. **Attachments**: `ATTACHMENT {att_type, chars = len(json.dumps(value))}` for keys `content, addedLines, addedBlocks, addedNames, entries`. Only the length is kept.
9. **cost-state** → `COST_STATE {reported_total_nusd}` for a coverage cross-check.
10. **Identity**: `session_pid = HMAC(sessionId)`; `lane_key = HMAC(sessionId/agentId|main)`; `user_pid` from `--user-id-from` (never read from content such as `attachment.context.userEmail`).
11. **Emission**: requests are emitted at end of file (memory bounded by message ids per file). Incremental re-reads from a cursor may re-emit a `message.id` with a larger output, and the store's max-output merge makes this idempotent.
12. **Drift**: unknown entry `type`s and a per-`version` field fingerprint go to `DATA_QUALITY`. Files older than `cleanupPeriodDays − 3` (default 27 days) produce a warning to schedule collection or raise `cleanupPeriodDays` `[ent-local-transcripts]`.
13. **Self-check** printed and stored: naive per-line sum versus deduplicated total ("naive $27,094 vs deduplicated $11,633 = 2.33×").

**Acceptance tests** (`tests/ingest/test_claude_code*.py`, schema-true synthetic fixtures under `tests/fixtures/claude_code/` built from the field inventory in `research/02_schema.out`):
1. **Split entries**: three entries for one `message.id` with outputs 3, 3 and 470 → one request with output 470. Cost equals the hand computation, and the naive sum is shown as 3× the input tokens.
2. A duplicate-`uuid` line is ignored.
3. 5m/1h split priced at 1.25/2.0. Missing split → `cache_write_other` with UNKNOWN basis.
4. Fallback iterations per F1 test 5, plus a `model_refusal_fallback` system entry → `MODEL_FALLBACK` event.
5. Placeholder no-stop call followed by a tool_result → `MESSAGE_START_ONLY`, `output_estimated_extra > 0`, `basis=estimated`.
6. `compact_boundary` → `COMPACTION` event plus a synthetic ESTIMATED compaction request.
7. **Incremental**: ingest half a file, append the rest (including a higher-output split entry for an already-emitted id), re-ingest → the store equals a one-shot ingest (byte-identical `requests` table dump).
8. **Content canary**: fixtures embed `TB-CANARY-7f3a91` in message text, tool results, tool inputs, file paths, `cwd`, `gitBranch` and attachments. After `collect` → `ingest` → `findings` → `report` → `export focus`, the canary is absent from every output file **and** from the raw bytes of the SQLite file.
9. Subagent and workflow lanes are recognized from paths and linked to the parent via meta `toolUseId`.
10. **Scale**: 200,000 synthetic assistant lines (2 KB each) in ≤ 30 s on CI, peak RSS ≤ 150 MB (`@pytest.mark.perf`).

### F3. Fleet store and incremental ingest at scale (WP6)

**What.** The SQLite store, merge rules, cursors and retention of §2.6, plus the `StoreReader`/`StoreWriter` implementations and the lane-view builder.

**Evidence.** `[ent-scale-storage]` `[cb-scale-memory]` `[cb-strict-parse]` `[cb-dup-runid]` `[cb-attribution-platform]`.

**$ impact.** An enabler. v0.1 needed 1.16 GB RSS for one 300-call session, and its simulator is O(n²) `[cb-scale-memory]`, so fleet analysis is impossible without this.

**Algorithm.** §2.6. `lanes()` streams `SELECT … FROM requests WHERE day BETWEEN ? AND ? ORDER BY lane_key, ts_start` with a server-side cursor and groups rows in Python. Events are joined by merging on `(lane_key, ts)`. `aggregate()` builds parameterized `GROUP BY` SQL over a whitelisted dimension set, and `n_users = COUNT(DISTINCT user_pid)`. Writes use `executemany` in 5,000-row transactions.

**Acceptance tests:**
1. Idempotence: ingesting the same sources twice leaves the dump unchanged.
2. Order independence: a random permutation of 5 sources (transcripts, OTel, usage@1) gives an identical dump.
3. Cross-source merge: a transcript row (fidelity 3) plus an OTel row (fidelity 2, adds `skill`, `team`) for the same `requestId` → one row with transcript tokens, OTel attribution, and a two-bit `sources_mask`.
4. Two files with the same `run_id` → separate namespaced keys, and totals equal the sum ($22, not $40) `[cb-dup-runid]`.
5. Cursor resume: an appended file is read from its offset. A truncated or rotated file (head hash changed) is fully re-read.
6. `--lenient`: a truncated line among 100 → 99 ingested, 1 in `*.quarantine.jsonl`, exit 0 with a warning (exit 4 under `--strict-dq`) `[cb-strict-parse]`.
7. Invariants: Σ `usage_lines` = Σ `requests` per column. No request has negative tokens.
8. `aggregate(group_by=["user_pid"])` raises `PrivacyError`.
9. Files are `0600` and the directory is `0700`.
10. **Perf gate**: 1,000,000 synthetic requests from `demo_fleet` (usage@1): ingest ≤ 60 s, `lanes()` full scan ≤ 30 s, peak RSS ≤ 400 MB.

### F4. OpenTelemetry ingestion: Claude Code OTel, OTel GenAI, OpenInference (WP3)

**What.** `ingest otlp` reads OTLP/JSON files (the collector `fileexporter` format, plain or gzip JSON lines; zstd needs Python ≥ 3.14 `compression.zstd`, otherwise the file is rejected with a hint to disable compression). The optional `otlp-receive` is a stdlib HTTP OTLP/JSON receiver. The convention registry covers ~30 frameworks without new recorders.

**Evidence.** `[ent-claude-code-otel]` `[cc-otel-schema]` `[cc-otel-pipeline]` `[anth-claude-code-analytics-otel]` `[otel-genai-semconv]` `[ent-otel-genai-semconv]` `[otel-genai-normalization]` `[otel-openinference-importer]` `[fw-telemetry-conventions]` `[claude-code-otel-fleet]` `[cc-enterprise-telemetry-gap]` `[fp-trace-fields-standards]`.

**$ impact.** An enabler. OTel is the only near-real-time per-user cost stream that works on every provider (Bedrock, Vertex, Foundry) `[cc-otel-pipeline]`. It carries skill/MCP/agent attribution and `result_tokens` for fleet tool-carry analysis without content.

**Algorithm.**
- **OTLP/JSON parsing**: `resourceLogs[].scopeLogs[].logRecords[]`, `resourceMetrics[]…`, `resourceSpans[]…`. Attributes are `[{key, value:{stringValue|intValue|doubleValue|boolValue|arrayValue}}]`. **`intValue` is a decimal string in OTLP/JSON (int64) and must be parsed as int.** `timeUnixNano` is a string.
- **`claude_code.api_request` event** → `Request`:
  - `request_key = anthropic:req:<request_id>`, else `otel:creq:<client_request_id>`;
  - fidelity 2; tokens `input_tokens`, `cache_read_tokens`, `cache_creation_tokens` → `cache_write_other` (UNKNOWN TTL), `output_tokens`;
  - `model`, `speed`, `effort`, `query_source` → `request_class`, `duration_ms`;
  - `cost_usd` → `ext.cc_list_cost_usd`, used only for cross-checks (a client list-price estimate, never billed truth `[cc-list-price-estimates]`);
  - `session.id` → `session_pid`; `user.id` / `user.account_uuid` / `user.email` → `user_pid` (HMAC, raw dropped); `organization.id`;
  - resource attributes `team.id`, `cost_center`, `department`, `tokenbill.arm`, `tokenbill.wave` → attribution;
  - `app.version`, `app.entrypoint`; `vcs.repository.url.full` → `repo_pid`.
- `claude_code.tool_result` → `TOOL_RESULT` (`tool_name`, `success`, `duration_ms`, `tool_result_size_bytes` → chars). `claude_code.api_error` → `API_ERROR`.
- **Beta spans**: `claude_code.llm_request` (`agent_id`, `parent_agent_id`, `ttft_ms`, `stop_reason`) enriches the request and sets exact lanes. `claude_code.tool` `result_tokens` → `TOOL_RESULT.tokens`.
- **Metrics**: `claude_code.token.usage` / `claude_code.cost.usage` sums → `UsageBucket(source="cc_otel_metrics")` for coverage and reconciliation only, never added to the ledger (avoids double counting with events).
- **GenAI spans**:
  - `gen_ai.operation.name ∈ {chat, generate_content, text_completion}`;
  - provider from `gen_ai.provider.name` (legacy `gen_ai.system`); model from `gen_ai.response.model` or `gen_ai.request.model`;
  - usage attributes per the §2.4.7 table (accept `cache_write` and legacy `cache_creation`);
  - `gen_ai.response.id` → key; `openai.response.service_tier`; `gen_ai.conversation.id` → session.
- **OpenInference**: only `openinference.span.kind == "LLM"` spans (AGENT/CHAIN roll-ups are ignored to avoid double counting); `llm.model_name`, `llm.provider`, `llm.token_count.*`.
- **Convention registry** (`conventions.py`): `(scope.name, version_range) → inclusive|exclusive|disjoint`. Unknown scope: inclusive per semconv. Sum-check: if `input < cache_read + cache_write`, the source is treated as exclusive for that span and a `DATA_QUALITY: convention-mismatch` event is emitted.
- **Receiver**: `POST /v1/logs|/v1/metrics|/v1/traces`, `Content-Type: application/json` only (protobuf → 415 with a hint to configure the collector's JSON encoding), gzip `Content-Encoding` accepted, body cap (413), bind `127.0.0.1` by default, optional bearer token from `TOKENBILL_OTLP_TOKEN`, writes through `pipeline.ingest_records`.

**Acceptance tests:**
1. Fixtures (built from the documented attribute lists) for `api_request`, `tool_result`, `api_error`, `llm_request` and `tool` spans, and `token.usage` metrics → expected records.
2. `intValue` string parsing.
3. Inclusive/exclusive golden sums for GenAI (new and legacy names), OpenInference, and the OpenLLMetry Anthropic vs Bedrock disagreement `[fw-telemetry-conventions]`.
4. An OTel row merges with the transcript row of the same `request_id` (no double count). The metrics bucket is not added to the ledger.
5. Only LLM spans counted: a trace with 1 AGENT span (rolled-up tokens) + 3 LLM spans → 3 requests.
6. The receiver returns 415 for protobuf and 413 over the cap, binds localhost, and runs with no other network.
7. Without a TTL split, dollars are ESTIMATED and a `DATA_QUALITY: no-ttl-split` note recommends the transcript collector or `OTEL_LOG_RAW_API_BODIES` (the opt-in raw-body path is documented as a privacy trade-off, not implemented in v0.2).

### F5. Attribution, workload classification, k-anonymity and privacy tiers (WP7)

**What.** A git-versioned allocation rules engine, a workload classifier, a coverage KPI, the k-anonymity publisher, the secrets scanner, and the DPIA/works-council pack (§2.8).

**Evidence.** `[ent-allocation-showback]` `[aggregation-k5]` `[labor-law-constraints]` `[leaderboard-goodhart]` `[vendor-leaderboards]` `[ent-privacy-by-default]` `[ent-redaction-limits]` `[ent-secrets-in-traces]` `[detect-signatures]` `[cc-gateway-attribution-caps]` `[machine-spend-separate]` `[showback-chargeback]`.

**$ impact.** A deployment gate. Without it, EU rollouts can be blocked under works-council co-determination, GDPR Art. 88 and EU AI Act Annex III `[labor-law-constraints]`. Allocation coverage ≥ 95% is the FinOps prerequisite for showback and chargeback `[ent-allocation-showback]`.

**Algorithm.**
- **Rules** (`rules.json`, ordered): `{"match": {"workspace_id": {"in": [...]}, "tags.team.id": {"eq": "x"}, "api_key_pid": {...}, "repo_pid": {...}, "agent_product": {...}, "entrypoint": {"prefix": "sdk-"}}, "set": {"team": "...", "cost_center": "...", "project": "...", "environment": "...", "workload_class": "ci"}, "split": {"method": "proportional", "by": "dev_days", "targets": [...]}}`. Operators: `eq`, `in`, `prefix`, `regex` (ids only, compiled with a timeout-safe subset: no backreferences, length-capped). The first match wins per target field. Unmatched → `team="(unallocated)"`. Splits emit `AllocatedMethodId`/`AllocatedMethodDetails` for FOCUS.
- **Coverage** = allocated $ / total $ per period, with a ≥ 95% target shown on every report.
- **Workload classifier**: a weighted signal table, output class plus confidence:
  - `entrypoint == claude-code-github-action` → CI (0.95);
  - `entrypoint ∈ {sdk-py, sdk-ts, sdk-cli}` → SERVICE/CI (0.6);
  - resource tag `workload=…` → as tagged (0.99);
  - `service_tier == batch` → BATCH_JOB (0.99);
  - a session with 0 `HUMAN_PROMPT` events and ≥ 10 requests → automated (0.7);
  - a periodic cadence (AUTO-03) → SCHEDULED (0.8);
  - a rule-set class (0.9).
  Highest confidence wins, and ties go to the more automated class `[detect-signatures]` `[auto-share-proxy]`.
- **k-anonymity**: §2.8 steps 1–4.
- **Secrets**: §2.8.

**Acceptance tests:**
1. Rules fixtures: first-match semantics, splits sum to the original, unallocated line present, coverage KPI correct.
2. **k-anon property test** (1,000 random tables, seeded): no published row has `n_users < k`; published total equals raw total; for every parent group, no suppressed child is recoverable by subtracting published rows from the parent total (brute-force check over subsets).
3. `Finding(audience=ORG)` with 3 users in scope is re-scoped to the parent team/org.
4. The classifier labels the fleet-demo CI, scheduled and interactive traffic at ≥ 95% accuracy.
5. Secrets: planted keys of each type are detected in counts, and no value appears in any output.
6. The DPIA pack contains the required sections (inventory, lawful basis placeholder, retention table, access matrix, "not for performance evaluation", no emotion inference, k-value).

### F6. Reconciliation and calibration against the Admin/Analytics APIs (WP5)

**What.** Read-only connectors (`pull`), with replay fixtures, for the Anthropic Usage & Cost Admin API, the Claude Code Analytics API, the Enterprise Analytics API and the OpenAI Admin Usage/Costs APIs. `reconcile` produces a variance report with explained residuals. `calibrate` is the ASHRAE-G14 gate on the replay engine. Effective-discount discovery feeds the contract card.

**Evidence.** `[ent-reconciliation]` `[ent-anthropic-admin-apis]` `[anth-admin-usage-cost-api]` `[anth-admin-api-reconciliation]` `[cc-admin-analytics-apis]` `[anth-claude-code-analytics-otel]` `[cc-anthropic-discount-visibility]` `[cc-reconciliation-matrix]` `[oai-admin-usage-costs]` `[ashrae-calibration-gate]` `[mv-adjusted-baseline]` `[finops-rate-vs-usage]` `[assignment-telemetry-srm]`.

**$ impact.** The trust anchor. It is the first acceptance test an enterprise lab runs (≤ 1% per day × workspace × model on closed periods) `[ent-reconciliation]`, and it makes every other dollar figure credible. It also computes the Effective Token Savings Rate (exact) = 1 − billed / list-equivalent, decomposed into caching, batch, TTL and discount `[finops-rate-vs-usage]`.

**Algorithm.**
- **HTTP** (`connect/http.py`): `urllib.request`, TLS verification on, timeouts, retry on 429/5xx with capped exponential backoff honouring `retry-after` ≤ 60 s, client-side rate limit (Enterprise Analytics 60 rpm), pagination (`has_more`/`next_page`; verify names), 31-day chunking where documented. Keys come only from env (`ANTHROPIC_ADMIN_KEY`, `ANTHROPIC_ANALYTICS_KEY`, `OPENAI_ADMIN_KEY`) and are never logged or written. `--record DIR` writes responses with auth headers removed. `--replay DIR` serves them, and all tests use replay.
- **Endpoints** (paths to re-verify, §4.6):
  - `GET /v1/organizations/usage_report/messages` (`bucket_width` 1m/1h/1d; `group_by[]` ∈ api_key_id, workspace_id, model, service_tier, context_window, inference_geo, speed);
  - `GET /v1/organizations/cost_report` (daily, amounts in cents as decimal strings, group_by workspace_id/description);
  - `GET /v1/organizations/usage_report/claude_code?starting_at=YYYY-MM-DD` (per user per day);
  - `GET /v1/organizations/analytics/{usage_report,user_usage_report,cost_report,user_cost_report}` (`amount` post-discount, `list_amount`, revisable 30 days, data from 2026-01-01, ≤ 31 days per query);
  - OpenAI `organization.usage.completions` and costs (verify paths).
  - Cents strings → nUSD exactly (1 cent = 10⁷ nUSD). Users → `user_pid` at ingest.
- **Reconcile** (`finops/reconcile.py`):
  1. Choose the reference `R` (`--against`), grain (default day) and dims D (the intersection of dims both sides have, default `workspace_id, model`).
  2. Ledger `L` = Σ requests by `(day, D)` in the requested price basis (contract if loaded, else list).
  3. **Token pass** (vs usage reports), per token class: `coverage = L_tokens / R_tokens`.
  4. **Dollar pass**: `delta = (L − R)/R` per cell and in total.
  5. **Residual classifier**, applied in order, each with $ and a reason string:
     - (a) Priority Tier traffic (usage_report `service_tier=priority`) is excluded from cost_report → explained.
     - (b) Code-execution lines present only in cost_report → explained line.
     - (c) Cells inside the 30-day revision window → `provisional` (excluded under `--closed-only`).
     - (d) Reference tokens not in the ledger → "unobserved traffic" by dims (e.g. Bedrock, apps not instrumented), which lowers coverage.
     - (e) Tokens match within 0.5% but dollars differ → implied discount `1 − R/L_list`, and a contract card is proposed.
     - (f) Estimated components (placeholder output, synthetic compaction) → an explained range.
     - (g) Everything else → **unexplained**.
  6. Status: `pass` if |unexplained| ≤ tolerance on closed cells. Otherwise exit 3.
- **Calibrate** (`finops/calibrate.py`): replay the observed traffic under the observed policy with the what-if engine (F8) and compare to the ledger and, when available, the invoice, per workspace-month and workspace-day. `NMBE = Σ(S−M)/ΣM`. `CV(RMSE) = sqrt(Σ(S−M)²/(n−1)) / mean(M)`. Pass: monthly |NMBE| ≤ 5% and CV(RMSE) ≤ 15%; daily ≤ 10% / ≤ 30%. Component checks: priced share of tokens ≥ 99.5%; chars→tokens calibration error from `detect/cpt.py`; replay-predicted cache reads vs billed reads within 2%. Scopes that pass mark projections `calibrated=True`. Others show "estimated (uncalibrated)" `[ashrae-calibration-gate]`.
- **Effective discount**: `1 − amount/list_amount` per model-day (Enterprise Analytics), or cost_report amount ÷ ledger list for matched tokens → `tokenbill pricing contract --from-reconcile` drafts the contract card `[cc-anthropic-discount-visibility]`.

**Acceptance tests:**
1. Replay fixtures per endpoint (schema from the docs, synthetic values): pagination, 31-day chunking, cents parsing, provisional flag.
2. A planted reconciliation world (from `demo_fleet`): ledger vs a reference with (a) 2% of traffic on Priority Tier, (b) one provisional day, (c) 5% unobserved Bedrock traffic, (d) a 12% discount. Each residual is classified correctly and the unexplained remainder is ≤ 0.1%.
3. The tolerance gate returns exit 3 when a 2% unexplained error is planted.
4. Keys never appear in logs, recorded fixtures or exceptions (grep test). Unit tests run under a socket-blocking fixture.
5. Calibrate: an exact replay passes. A planted 1h→5m mispricing fails NMBE with a component-level explanation.

### F7. Cause-named fleet waste detectors, content-free (WP8a, WP8b)

**What.** Detectors over lanes and events that emit `Finding`s with a cause, dollars, evidence, a fix, and lever links. Each has `requires` capabilities. When data lacks them, the detector emits one `DATA_QUALITY` finding instead of guessing.

Shared rules:
- **Miss definition**: request `i` in a lane misses when `missed = max(0, ctx_{i−1} − cache_read_i) > max(2048, 0.05·ctx_{i−1})`. This is Claude Code's `/usage` definition `[cc-usage-likely-cause]`.
- **Miss waste**: `mw = min(missed, cache_write_i)`, `mu = min(missed − mw, uncached_i)`, `waste = mw·(r_w(m_i, ttl_i) − r_rd(m_i)) + mu·(r_in(m_i) − r_rd(m_i))`. This is the empirical script's rule, with the TTL of the write actually billed.
- **TTL in effect**: §2.7. **Gap** = `ts_start_i − ts_start_{i−1}` (TTL counts from request start).
- **Publication**: findings aggregate at `(team, request_class, cause[, model])` and pass k-anonymity. SELF findings are only for `tokenbill me`.

#### WP8a — cache and context detectors

| ID (module) | Rule | Waste formula | Fix / levers | Evidence, $ |
|---|---|---|---|---|
| **CACHE-01** misses by cause (`misses.py`) | Every miss, cause by precedence: (1) `COMPACTION`/`CONTEXT_EDIT` event in `(ts_{i−1}, ts_i]` → `compaction` (expected rebuild; waste only if the compaction ran cold `[compaction-timing]`); (2) base model changed → `model-switch` (sub-causes `fallback`, `ping-pong`); (3) gap > TTL → `ttl-expiry-5m`/`ttl-expiry-1h`; (4) speed changed → `fast-toggle`; effort changed and not (model ∈ {opus-5-5, fable-5-1} on 1P) → `effort-change`; `agent_version` changed → `client-upgrade`; `cwd_pid` changed → `directory-change` (info); (5) `diag_reason` tools/system/messages/model_changed → `tools-changed` / `system-changed` / `history-edit` / `model-switch`; `unavailable` → `param-change`; (6) `ctx_i < 0.9·ctx_{i−1}` → `context-shrank`; (7) else `unexplained` | miss waste | per cause: TTL-01; switch only at `/clear` or via subagent; fallback credit; per-message effort on supported models; `fastModePerSessionOptIn`; `ENABLE_TOOL_SEARCH` / defer_loading; mid-conversation system message (Opus 5/5.5/4.8, Fable, Mythos; **not Sonnet 5**) `[anth-cache-preserving-apis]` | `[cc-miss-taxonomy-ground-truth]` `[cc-cache-breakers]` `[cc-invalidators-taxonomy]` `[anth-invalidation-hierarchy]`. Corpus: 15.1% of bill was miss premium (52.5% idle > 1h, 33% within-TTL changes) |
| **CACHE-02** cold resume (`misses.py`) | `ttl-expiry` misses on MAIN lanes with `ctx_{i−1} ≥ 200k` (config) | miss waste; plus the summarize-on-resume what-if from F8 | `/clear` or `/compact` before stepping away; SessionStart resume-cost hook (F8 hooks); 1h TTL if gaps are 5–60 min | `[cc-cold-resume]`: 7.95% of bill, $7.22/event (one user); up to ~29% with summarize-on-resume (upper bound, overlaps CTX-04) |
| **CACHE-03** caching disabled (`gateway.py`) | (a) `no-cache`: lane with ≥ 5 requests, median ctx ≥ max(min_cacheable, 4096), Σread = Σwrite = 0, grouped by channel/gateway and team; (b) `beta-header-dropped`: policy says 1h for the scope but ≥ 20 requests show only 5m writes; (c) `tool-search-disabled`: channel `gateway:*` and ≥ 3 `tools-changed` misses per 100 requests | (a) Σ_{i≥1, gap ≤ 300 s} `min(ctx_{i−1}, uncached_i)·(r_in − r_rd) − (uncached_i − min(…))·(r_w5 − r_in)`; (b) and (c) via CACHE-01 waste in scope | forward `cache_control` and `anthropic-beta` unchanged; do not flatten system blocks; `ENABLE_TOOL_SEARCH=true`; LiteLLM injection points (F8) | `[cc-gateway-marker-stripping]` `[gateway-strip]` `[cc-gateway-cache-strip]` `[cc-apps-gateway-routing-tax]`: up to ~90% of input cost on affected routes |
| **CACHE-04** unread-write premium (`write_waste.py`) | A request `i` (not the last in its lane) with writes `W_i > 0` whose next request `j` has `gap ≤ TTL` and `read_j < 0.95·(read_i + W_i)`. Also single-request lanes that write (automatic caching on unique tails). Last-in-lane writes are reported separately as unavoidable "tail writes" | `W_i·(r_w(ttl) − r_in)` (the surcharge over uncached) | put the breakpoint at the end of the shared portion; explicit mode on OpenAI 5.6+; don't cache one-shot traffic | `[write-without-read]` (19% avoidable in the example) `[oai-cache-write-waste]` |
| **CACHE-05** cold fan-out (`fanout.py`) | Bursts of ≥ 2 first-requests-of-lane in the same cache domain (workspace or org, model) starting within 10 s, each with `write ≥ 1024` and `read < 0.5·ctx` | `(N−1)·P·(r_w − r_rd)`, with P between `min(write_first)` (low) and `median(write_first)` (high); labelled an upper bound | stagger: send one, await first token, then N−1 (Claude Code workflows hold siblings ≤ 5 s); never pre-warm batch jobs | `[cc-agent-spinup-fanout]` (1.2% upper bound) `[anth-concurrency-fanout]` `[ci-fanout-stagger]` |
| **CACHE-06** cache health (`cache_health.py`) | Per `(team, request_class, model family)`: `read_share = Σread / Σctx` over non-first requests, graded against 84% median / 94% top decile / <80% investigate | triage signal: `(0.84 − share)·Σctx·(r_nonread − r_rd)`, labelled low-confidence, **excluded from the headline** | points to CACHE-01..05 findings in scope | `[anth-cache-health-thresholds]` `[anth-fleet-benchmarks]` `[practice-hit-rate-slo]` |
| **CTX-01** context tax (`context_tax.py`) | Per team/request_class: ctx p50/p90; share of calls and $ at ≥ 200k and ≥ 400k; $ of cache reads beyond X ∈ {100k, 200k, 400k} | `Σ max(0, read_i − X)·r_rd + Σ max(0, min(write_i, ctx_i − X))·r_w` (addressable pool, not savings) | CTX-04 compaction window; `/clear` between unrelated tasks `[anthropic-habits]` | `[cc-context-size-driver]` (reads beyond 200k = 23.2% of bill, one user) `[context-tax-residency]` `[ctx-quadratic-growth]` |
| **CTX-02** configuration tax (`config_tax.py`) | Per lane: the first `ATTACHMENT` of each type (`skill_listing`, `mcp_instructions_delta`, `deferred_tools_delta`, and similar) → tokens = chars / cpt(model family) | `tokens·(r_w(ttl) + r_rd·n_later_requests_in_lane)`, first listing only (corrected method) | `skillListingBudgetFraction`, prune MCP servers, `claudeMdExcludes`, CLAUDE.md < 200 lines | `[cc-config-sprawl-tax]` (≈ 3–7%, likely low end) `[cc-fixed-context-overhead]` `[static-prefix-compression]` |
| **CTX-03** tool-output carry (`tool_carry.py`) | For each `TOOL_RESULT` (chars/cpt, or OTel `result_tokens`) at time t in a lane: the next request writes it and later requests read it until a compaction or context shrink | `T·(r_w + r_rd·n_later)`, aggregated by `(team, tool, mcp_server)`, plus the share held by the top 5% of results | PreToolUse/PostToolUse output-filter hooks, `bashOutputMaxChars`, `MAX_MCP_OUTPUT_TOKENS`, subagent offload. `tradeoff=True`: trimming can lengthen trajectories (RTK +7.6%) | `[cc-tool-output-carry]` (~12% of bill; Bash + Read ~10%) `[cc-tool-output-bloat]` `[tool-output-bounding]` `[verified-savings-gap-rtk]` |
| **cpt** calibration (`cpt.py`) | For consecutive requests in a lane with exactly one TOOL_RESULT of ≥ 2,000 chars between them and no attachments: `Δ = ctx_i − ctx_{i−1} − output_{i−1}`, `cpt = chars/Δ`; median and IQR per `(model family, tool)` | — | used by CTX-02/03. Defaults when n < 30: 2.5 (Claude 4.7+ family) and 3.3 (legacy), marked ESTIMATED | `[cc-chars-per-token-calibration]` (2.22–2.66 measured) `[chars-heuristic-error]` |

#### WP8b — model, premium, failure, automation and tail detectors

| ID (module) | Rule | Waste formula | Fix / levers | Evidence, $ |
|---|---|---|---|---|
| **MODEL-01** switch and fallback (`model_switch.py`) | CACHE-01 `model-switch` misses split by trigger: refusal (iterations `fallback_message` or `MODEL_FALLBACK trigger=refusal`), availability (`API_ERROR` 529/overloaded then a different model), user/opusplan (periodic A/B alternation); `ping-pong` A→B→A within TTL; `no-credit`: first call on the new model with `write ≥ 0.8·ctx_prev` | rebuild = `ctx_prev·(r_w(new) − r_rd(new))` per switch | switch only at `/clear`/compaction or delegate to a subagent; fallback credit (`fallback-credit-2026-07-01`); same-family fallbacks; 1h TTL | `[cc-model-switch-cost]` ($4.69 per main switch) `[fp-fallback-credit]` `[fp-cc-fallback-chains]` `[anth-orchestrator-advisor]` |
| **MODEL-02** premiums (`premiums.py`) | Exact premium paid from `premium_*` columns: fast mode (flag non-interactive fast and fast/standard flapping); `inference_geo="us"` where config `residency_required` is false for the scope; Bedrock/Vertex regional endpoints; priority/fast service tiers; long-context band crossings (OpenAI > 272K, Gemini > 200K, xAI ≥ 200K) within 10% of the threshold | premium $ (**exact**); recoverable = premium × share judged unnecessary by config (**estimated**) | `fastModePerSessionOptIn` / `CLAUDE_CODE_DISABLE_FAST_MODE`; global endpoints; drop priority on batchable traffic; compact below the band | `[anth-modifiers-geo-fast-priority]` `[premium-modifiers]` `[oai-service-tiers]` `[oai-batch-longctx-residency]` `[vertex-claude-geo-labels]` `[hosted-service-variance]`: 50% of fast $, ~9% of geo/regional $ |
| **MODEL-03** effort and thinking (`effort.py`) | Spend and output share by effort per team/request_class; thinking share where reported; concentration (one level ≥ 50% of spend); **sticky escalation** (SELF): a user's effort or fast mode above the org default for > 5 days; ORG view: count of such users per team if ≥ k | informational upper bound: halving thinking ≈ `0.5·Σ reasoning_output·r_out` | `maxEffortLevel`, per-role default effort, session-only `/effort`; `tradeoff=True` | `[cc-output-thinking-effort]` (halving thinking ≈ 3.6%, one user) `[cc-sticky-escalation]` `[anth-effort-sweep]` `[org-defaults-datadog]` (effort default −$288k/month) |
| **FAIL-01** truncation (`truncation.py`) | `stop_reason == "max_tokens"`, plus an immediate retry (next request in lane within 60 s, same model, `ctx ≥ 0.95·ctx`) | Σ $ of truncated attempts | `max_tokens` 64k for agentic work (128k at xhigh/max), or lower effort | `[anth-output-hygiene]` `[max-tokens-truncation]` |
| **FAIL-02** retries and errors (`failures.py`) | Episode = `API_ERROR` or non-success attempts → first success. `cold-retry`: success after > TTL from the episode start with `write ≥ 0.5·prefix`; `retry-storm`: > 3 attempts per logical request or attempts at ≥ 2 retry layers; `never-succeeding`: same `error_class ∈ {prompt_too_long, thinking_binding, spend_cap}` ≥ 2 times or any retry after `should_retry=False`; `network-abort`: aborts clustered at 180–360 s of silence. Placeholder usage is **not** an abort | cold-retry: `prefix·(r_w − r_rd)`; storms: Σ extra attempts' $ | one retry owner; backoff capped below TTL minus generation time; stream plus TCP keepalive < 350 s; preflight `count_tokens`; strip or drop thinking blocks | `[fp-ttl-from-request-start]` `[fp-cc-retry-semantics]` `[fp-nested-retry-amplification]` `[fp-never-succeeding-400]` `[fp-network-aborts]` `[fp-local-failure-share]` (0.7% direct, 3.1% upper bound on a healthy corpus) |
| **FAIL-03** tool-error loops (`failures.py`) | ≥ 3 consecutive `TOOL_RESULT.is_error` in a lane, or the same `(tool, error)` ≥ 3 times in 10 requests | Σ $ of the requests consuming those results | PreToolUse guards, turn caps | `[fp-agent-loop-prevalence]` `[mast-loop-failures]` |
| **AUTO-01** workload mix and batch eligibility (`automation.py`) | Automated share of spend by class (KPI). Batch-eligible = requests whose workload ∈ {eval, batch_job, service, ci} that are single-shot (lane length 1) or have no human prompt and a configured latency slack; exclude Managed Agents and tool loops; already batched = `service_tier == batch` | `0.5·eligible$`, with cached reads in a 30–98% hit band → range | Batch API / Flex (code change) | `[auto-share-proxy]` `[batch-flex-50]` `[anth-batch-stacking]` `[batch-plus-cache]`: 50% of eligible |
| **AUTO-02** CI cross-run cache (`automation.py`) | CI lanes where the first request writes ≥ 80% of ctx and reads < 10%, while ≥ 2 runs in the same workspace start within the TTL of each other; sub-causes by `agent_version` drift and `cwd_pid` diversity | `(N_runs − 1)·P·(r_w − r_rd)`, P = median first-call ctx × 0.8 | `--exclude-dynamic-system-prompt-sections`, pinned CLI version, one CI workspace, 1h TTL for 5–60 min run gaps | `[ci-cross-run-cache]` (54% in the cookbook) `[ci-headless-ingest]` |
| **AUTO-03** scheduled cadence (`automation.py`) | Lanes or sessions with ≥ 4 requests at near-constant intervals (CV of gaps < 0.25) and no human prompts; flag interval > TTL | `fires·ctx·(r_w − r_rd)` when the interval exceeds the TTL | 1h TTL or keepalive, smaller context, event triggers instead of polling | `[sched-cadence-ttl]` (up to ~12× per fire) `[sched-event-driven]` |
| **TAIL-01** runaway and heavy tail (`runaway.py`) | Session rolling-1h $ > max($50, 5 × team p99 hourly) (config); interactive session with ≥ 50 requests and no `HUMAN_PROMPT` for ≥ 1h (idle loop); OTel traces with LLM spans > 3 × per-agent p95 (framework loops); KPI: top 5% of sessions' share of spend per team | Σ $ above the team p95 session cost | circuit breaker via existing gateway/Console limits (documented, not enforced); `recursion_limit`/call-limit middleware | `[runaway-circuit-breaker]` `[cc-heavy-tail-concentration]` (3 of 29 sessions = 59%) `[expensive-failures-tail]` `[langgraph-recursion-10007]` `[loop-defaults-matrix]` |

**Acceptance tests (both WPs)**, `tests/detect/`, using `core.testing.MemoryDetectContext` built from hand-constructed lanes:
1. One fixture per row with a hand-computed waste value (to the nUSD).
2. Precedence: a compaction plus an idle gap → `compaction`, not `ttl-expiry`. A model switch plus a gap → `model-switch`.
3. Boundaries: missed = 2,048 is not a miss and 2,049 is; 5% of ctx likewise.
4. Diagnostics confusion matrix: CACHE-01 emits `evidence.diag_confusion`, and a fixture with labels checks the counts. Only the four `*_changed` labels count as agreement `[cc-miss-taxonomy-ground-truth]`.
5. Unmet `requires` (e.g. OTel lanes without attachments for CTX-02) → exactly one DATA_QUALITY finding, no dollars.
6. **Healthy control**: the fleet-demo control team produces no finding with `waste ≥ $min_usd` (false-positive guard).
7. k-anon: a finding with n_users < 5 is re-scoped before publication.
8. Determinism: stable `finding_id` and ordering across runs.

### F8. What-if engines and the policy pack (WP9, WP10)

**What.** A single lane-replay engine that composes levers (TTL policy, keepalive, compaction window, summarize-on-resume, model map, batch) with Shapley attribution and realization-rate priors. It drives four what-if detectors and the **policy pack generator**, which emits a managed-settings patch, LiteLLM config, hooks, a projection report and a randomized rollout plan. Nothing is ever auto-applied.

**Evidence.** `[org-defaults-datadog]` `[datadog-1m-month-case]` `[defaults-beat-nudges-meta]` `[nudge-decay-durability]` `[cc-managed-settings-levers]` `[cc-autocompact-window]` `[cc-compaction-threshold-sim]` `[cc-ttl-advisor]` `[cc-ttl-policy]` `[anth-ttl-choice-keepalive]` `[keepalive-economics]` `[cc-ttl-billing-path]` `[cc-delegation-model-routing]` `[cc-same-tier-upgrade]` `[anth-tokenizer-inflation]` `[shapley-attribution]` `[realization-rate-backtest]` `[projection-optimism]` `[llm-token-not-bill]` `[enforcement-primitives]` `[litellm-gateway-injection]` `[stepped-wedge-mdm]` `[cache-interference-cluster]` `[rtm-targeting]` `[channel-code-review-hooks]` `[uber-policy-arc]`.

**$ impact.** This is where the money is:
- Model and effort defaults: −36.7% and −$288k/month at Datadog `[org-defaults-datadog]`.
- Compaction window: an upper bound of −18.4% (700k) to −37.7% (200k) of the total bill on the corpus `[cc-compaction-threshold-sim]`.
- TTL: ~10–12% of main-thread spend for 5m-default API-key fleets with that idle profile `[cc-ttl-advisor]`.
- Delegation to Sonnet 5: 4–31% price-only `[cc-delegation-model-routing]`.
- Same-tier upgrades: 12–21% exact repricing on that corpus `[cc-same-tier-upgrade]`.
- Batch: 50% of eligible traffic.

These overlap, so the headline is the joint Shapley total, RR-adjusted.

**Engine** (`whatif/engine.py`):

```python
# PolicySet lives in core/records.py (WP0) because WP9, WP10 and WP13 all use it.
@dataclass(frozen=True)
class PolicySet:
    ttl_main: str = "observed"          # "observed" | "5m" | "1h" | "keepalive:<interval_s>:<max_idle_s>"
    ttl_subagent: str = "observed"
    compaction_threshold: int | None = None     # tokens; None = observed behaviour
    compaction_summary_tokens: int | None = None  # default: org median COMPACTION.post_tokens, else 20_283
    resume_compaction_min_ctx: int | None = None  # summarize-on-cold-resume variant
    model_map: Mapping[str, str] = field(default_factory=dict)  # canonical model → model
    model_map_classes: frozenset[RequestClass] = frozenset()    # empty = all classes
    batch: bool = False                 # apply to eligible requests only (AUTO-01 predicate)

@dataclass(frozen=True)
class LaneCost:
    nusd: int; low_nusd: int; high_nusd: int      # low/high from tokenizer and batch-hit ranges
    extra_compactions: int; keepalive_pings: int

def simulate_lane(lane: LaneView, policy: PolicySet, pricer: Pricer) -> LaneCost: ...
def simulate(lanes: Iterable[LaneView], policies: Mapping[str, PolicySet], pricer: Pricer) -> dict[str, LaneCost]: ...
```

Application order per lane (fixed so subsets compose deterministically):
1. **Model map**: reprice identical token buckets on the target model. If the tokenizer families differ (e.g. legacy → claude-2026), scale input and output tokens by [1.0, 1.35] for low/high with point 1.0 × the org-calibrated ratio if one exists. Otherwise the math is exact at equal tokens `[anth-tokenizer-inflation]` `[cc-capacity-model-lag]`.
2. **Compaction** (MAIN lanes; empirical algorithm):
   - start each lane with `removed = 0`; at each request, if `ctx_i < 0.5·ctx_{i−1}` (a real compaction or clear happened), reset `removed = 0`;
   - `ctx_eff = ctx_i − removed`; if `ctx_eff > T`: add a compaction call `ctx_eff·r_rd + S·r_out`, set `removed = ctx_i − S`, and the request costs `S·r_w + out·r_out + uncached·r_in`;
   - otherwise `read' = max(0, read − removed)` and `write' = write if read ≥ removed else max(0, write − (removed − read))`.
   - **Summarize-on-resume**: when `gap > TTL` and `ctx_eff > resume_min`, add an uncached summary call `ctx_eff·r_in + S·r_out`, set `removed = ctx_i − S`, and the request writes S plus its new tokens.
3. **TTL / keepalive** (skipped when the lane's TTL policy is `observed`, which keeps the billed read/write split), per request `i ≥ 1` with `g = ts_start_i − ts_start_{i−1}`, policy TTL τ and write rate `r_w(τ)` (keepalive uses the 5m rate). Writes are never converted to reads, so misses caused by breakers stay misses (conservative, as in the empirical replay):
   - `alive = g ≤ τ`;
   - alive: `read'·r_rd + write'·r_w(τ) + uncached·r_in + out·r_out`;
   - not alive: `(read' + write')·r_w(τ) + uncached·r_in + out·r_out`;
   - keepalive `k:<I>:<M>` (5m TTL, a ping every I seconds after the previous request's start while elapsed ≤ M): `n = min(floor(M/I), ceil(g/I) − 1)` pings, each costing `ctx'_{i−1}·r_rd` (max_tokens 0); the request is alive iff `g ≤ 300` or `g ≤ n·I + 300`;
   - if `ctx' < min_cacheable_tokens(m)`, nothing caches and everything is priced uncached;
   - request 0 of a lane: its writes are repriced at `r_w(τ)`.
4. **Batch**: eligible requests × `batch_mult`, with cached reads over a hit band `h ∈ [0.30, 0.98]`: the `(1−h)` share of reads becomes writes. Low/high come from the band ends.

**Calibration invariant**: `simulate_lane(lane, PolicySet())` (all observed) must reproduce the ledger's list $ for the lane within 0.5% where `write_ttl_basis = REPORTED`. That is the F6 `calibrate` gate.

**Shapley** (`whatif/shapley.py`): for a lever set L with k ≤ 10, evaluate `v(S) = baseline − cost(policy with the levers in S)` for all 2^k subsets (on 10% lane samples when lanes × 2^k > 5·10⁶, reported as sampled). `φ_j = Σ_{S⊆L∖{j}} |S|!(k−|S|−1)!/k! · (v(S∪{j}) − v(S))`. Test: Σφ_j = v(L). For k > 10, use a FinOps-order sequential waterfall (usage levers, then rate levers) and label it "order-dependent". **Summing standalone ceilings is never displayed** `[shapley-attribution]`.

**Realization-rate priors** (`whatif/priors.py`), as (p10, p50, p90):

| Lever class | Prior | Basis |
|---|---|---|
| RATE | (1.0, 1.0, 1.0) | exact price arithmetic |
| DETERMINISTIC_TRANSFORM | (0.8, 0.9, 1.0) | TTL, cache placement |
| TRAJECTORY_CHANGING | (−0.2, 0.5, 1.0) | compaction, model, effort, tool-output trimming; p50 is a stated judgment, replaced by the org's empirical-Bayes posterior once receipts exist |
| BEHAVIOURAL | not projected | measured only |

EE and LLM evidence both show projections are optimistic, sometimes with the wrong sign `[projection-optimism]` `[llm-token-not-bill]` `[realization-rate-backtest]`. Projections display `savings × RR` at p10/p50/p90.

**What-if detectors** (WP9, in `detect/`):
- **TTL-01** (`ttl_advisor.py`): per `(team, request_class ∈ {main, subagent, workflow})`, compare observed, 5m, 1h and keepalive(240 s, 3,600 s). Recommend the argmin if savings ≥ max(config min $, 2% of scope spend). Cross-check Anthropic's rule (> 1 in 20 gaps in 5–60 min → 1h) and report agreement. **Heterogeneity**: if < 60% of the scope's users are individually cheaper under the recommendation (the corpus split 15/14), recommend a per-cohort policy via MDM groups instead of org-wide `[cc-ttl-advisor]` `[anth-fleet-benchmarks]`. Settings: `promptCacheTtl`, `subagentPromptCacheTtl` / `CLAUDE_CODE_PROMPT_CACHE_TTL`, `ENABLE_PROMPT_CACHING_1H`, `FORCE_PROMPT_CACHING_5M`. Lever class DETERMINISTIC_TRANSFORM.
- **CTX-04** (`compaction_window.py`): MAIN lanes, T ∈ {200k, 300k, 400k, 500k, 700k}, full curve plus extra-compaction counts. Recommend the candidate with the best RR-adjusted p50 subject to `T ≥ config.min_compaction_window` (default 300k). Enforce via managed `autoCompactWindow` **and** env `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (the `--autocompact` flag is not preempted by managed settings) `[cc-managed-settings-levers]`. Always `tradeoff=True`, labelled an upper bound (ignores re-work) `[cc-compaction-threshold-sim]`.
- **MODEL-04** (`model_reprice.py`, same-tier upgrade): per model in use, reprice on the successor from `rates/data/lifecycle.json` (fable-5 → fable-5-1, opus-5 → opus-5-5, opus-4-8 → opus-5-5, sonnet-4-6 → sonnet-5 with the tokenizer range, opus-4-1 → opus-5-5 on partner clouds). Also flags retirement dates inside the window `[cc-same-tier-upgrade]` `[cc-timing-calendar]`. `tradeoff=True` (behaviour and breaking changes; migration checklist link).
- **MODEL-05** (`model_reprice.py`, delegation): model map restricted to SUBAGENT/WORKFLOW → {sonnet-5, haiku-4-5, opus-5-5}. Emits `CLAUDE_CODE_SUBAGENT_MODEL` and per-agent `model:` suggestions. `tradeoff=True`, labelled "price-only, validate quality" `[cc-delegation-model-routing]` `[plan-execute-subagents]`.

**Policy catalog** (`policy/catalog.py`). Each lever: `lever_id, title, lever_class, tradeoff, settings: dict, env: dict, targets, preconditions (model × platform matrix), evidence, verification {unit, design, metric}`.

| Lever | Delivery (managed settings unless noted) | Class |
|---|---|---|
| L-TTL-MAIN / L-TTL-SUB | `promptCacheTtl` / `subagentPromptCacheTtl` (values per settings reference) | deterministic |
| L-COMPACT | `autoCompactWindow` + `env.CLAUDE_CODE_AUTO_COMPACT_WINDOW` | trajectory |
| L-1M-OFF | `env.CLAUDE_CODE_DISABLE_1M_CONTEXT=1` | trajectory |
| L-SUBAGENT-MODEL | `env.CLAUDE_CODE_SUBAGENT_MODEL` | trajectory |
| L-DEFAULT-MODEL | `availableModels`, `env.ANTHROPIC_DEFAULT_*_MODEL` | trajectory |
| L-EFFORT-CAP | `maxEffortLevel` (v2.1.267+) | trajectory |
| L-FAST-OPTIN | `fastModePerSessionOptIn` / `env.CLAUDE_CODE_DISABLE_FAST_MODE` | rate |
| L-TOOL-SEARCH | `env.ENABLE_TOOL_SEARCH=true` | deterministic |
| L-TOOL-OUTPUT | `bashOutputMaxChars`, `env.MAX_MCP_OUTPUT_TOKENS`, output-filter hook | trajectory |
| L-SKILLS | `skillListingBudgetFraction`, `claudeMdExcludes` | trajectory |
| L-MODEL-UPGRADE | pin successor ids via `env.ANTHROPIC_DEFAULT_*_MODEL` | trajectory |
| L-OTEL | `env.CLAUDE_CODE_ENABLE_TELEMETRY=1`, `OTEL_RESOURCE_ATTRIBUTES` (team.id, cost_center, tokenbill.arm/wave), `OTEL_METRICS_INCLUDE_ENTRYPOINT=true` | enabler |
| L-RETENTION | `cleanupPeriodDays` ≥ collector period + 7 | enabler |
| L-PRICING | `modelPricing` from the contract card (reporting accuracy, $0 savings) | enabler |
| L-GATEWAY-CACHE | LiteLLM `cache_control_injection_points` (system + index −1; TTL from TTL-01) | deterministic |
| L-CI-PREFIX | CI snippet: `--exclude-dynamic-system-prompt-sections`, pinned CLI version, CI workspace | deterministic |
| L-BATCH | code change (documented) | rate |
| L-HOOK-RESUME | SessionStart hook warning when `prompt_cache_likely_expired` and `estimated_cache_write_usd` > threshold; status-line script at contract rates | behavioural |

**Generation** (`policy generate`):
1. Select levers with projections above threshold, or `--levers`.
2. Check preconditions: model × platform availability (e.g. per-message effort only on Fable 5.1, Mythos 5.1, Opus 5, Opus 5.5; mid-conversation system messages not on Sonnet 5) `[anth-cache-preserving-apis]` `[mid-conv-system-tool-addition-support]`.
3. Render the targets:
   - `managed-settings.patch.json`: JSON merge-patch against `--current` containing only the changed keys;
   - `managed-settings.full.json`;
   - `litellm-config.patch.yaml`: template-rendered text, validated by a stdlib mini-parser test;
   - `hooks/`;
   - `PROJECTION.md` and `projection.json`: per lever standalone, Shapley and RR range, tradeoff, calibration status, evidence ids;
   - `ROLLOUT.md` and `assignment.csv`.
4. Every tradeoff lever gets `requires_eval: true` and must have a holdout in the rollout plan. No command applies a patch.

**Rollout plan** (`policy plan-rollout`):
- Units are the cache-isolation clusters: workspace on 1P / Claude Platform on AWS / Foundry; MDM group or team for endpoint-managed settings; the org on Bedrock/Vertex → daily-block switchback. Server-managed settings apply org-wide, so per-group rollout needs MDM or endpoint files, or the Claude apps gateway per IdP group `[stepped-wedge-mdm]` `[cache-interference-cluster]` `[switchback-carryover]`.
- Randomized wave order (seeded), a 10–25% never-treated holdback, washout = max(1 h, p90 session length, settings refresh interval).
- MDE by A/A re-randomization on 8–12 pre-period weeks (`verify.guards.mde`). The plan **refuses to call itself a verification design** when MDE > 0.8 × the projected effect and states the fleet size or window needed `[heavy-tails-power]`.
- Per-wave MDM payloads with `OTEL_RESOURCE_ATTRIBUTES` arm/wave tags, `sha256(assignment.csv)`, and a pre-registration file (lever, unit, estimator, looks, rate-card id) with its hash.
- Warns when waves are chosen by spend (regression to the mean inflates savings by ~17–34%) `[rtm-targeting]`.

**Acceptance tests:**
1. **TTL replay** on a hand-built lane (gaps 30 s, 400 s, 7 h; ctx 100k→300k; Opus 5.5) matches hand-computed nUSD under 5m, 1h and keepalive.
2. The observed-policy replay reproduces the ledger within 0.5% on the fleet demo.
3. **Compaction replay** matches the empirical algorithm on a fixture: extra compactions counted, reads reduced.
4. **Shapley efficiency**: Σφ = v(L) exactly (integer nUSD with a documented remainder rule). Symmetry: identical levers get equal credit.
5. Tokenizer range applied only across families.
6. `policy generate` on the fleet demo emits exactly the expected patch keys for the planted causes, preserves unrelated keys under `--current`, marks tradeoff levers `requires_eval`, and never includes a lever whose preconditions fail.
7. `plan-rollout` is deterministic per seed, has holdback within ±1 unit, hashes the assignment log, and refuses "verification" when the planted MDE is too large.
8. RR ranges appear on every projection. The headline equals the joint Shapley total × RR p50.

### F9. Team showback, unit economics and FOCUS export (WP11)

**What.** Per-team showback (HTML/CSV/JSON), FinOps KPIs, unit economics joined from Claude Code Analytics, and `export focus` (FOCUS 1.4 CSV with `x_` Token Bill columns and FOCUS 1.5 forward-compatible model identity).

**Evidence.** `[ent-focus-1-4]` `[focus-15-ai-columns]` `[finops-platforms-export-target]` `[finops-foundation-focus-tokens]` `[finops-focus-tokenomics]` `[ent-finops-kpis]` `[finops-token-yield]` `[ent-allocation-showback]` `[showback-chargeback]` `[cost-per-merged-pr]` `[units-gaming]` `[outcome-unit-costing]`.

**$ impact.** An enabler: accountability and distribution into the tools finance already uses. Chargeback's price analog is more durable than moral suasion `[showback-chargeback]`.

**Algorithm.**
- **Showback page per team** (k-anon published only):
  - spend (list/contract, with basis chips) and trend;
  - cost per active developer-day vs the $13 / $30 anchors;
  - automated share, cache health grade, context p50/p90;
  - top findings with waste ranges, top projections (Shapley, RR), allocation coverage.
  - Individuals never appear.
- **Unit economics**: cost per active dev-day (primary, ITT) `[units-gaming]`. Secondary: cost per Claude-Code-attributed commit / PR / accepted edit from Claude Code Analytics per-user-day, joined **before** aggregation to team and published only at k ≥ 5. Labelled "activity ratios, gameable, not productivity". No quality weighting in v0.2 (deferred, §6).
- **FOCUS rows**: one per `(day, provider, channel, model, token_class, allocation group)`. Columns:
  - `BillingAccountId`, `BillingCurrency=USD`, `BillingPeriodStart/End` (month), `ChargePeriodStart/End` (day);
  - `ChargeCategory=Usage`, `ChargeFrequency=Usage-Based`, `ChargeDescription` ("claude-opus-5-5 cache_read tokens");
  - `ServiceCategory="AI and Machine Learning"`, `ServiceName` (e.g. "Claude API", "Amazon Bedrock"), `ProviderName`, `PublisherName`, `InvoiceIssuerName`, `RegionId` (if known), `ResourceId` (workspace_id);
  - `SkuId` (`row_id#token_class`), `PricingCategory=Standard`, `PricingQuantity` (tokens/10⁶), `PricingUnit="1M Tokens"`, `ConsumedQuantity` (tokens), `ConsumedUnit="Tokens"`;
  - `ListUnitPrice`, `ListCost`, `ContractedUnitPrice`, `ContractedCost`, `EffectiveCost`, `BilledCost`;
  - `Tags` (JSON: team, cost_center, project, workload, agent_product), `AllocatedMethodId`/`AllocatedMethodDetails` for splits.
  - `x_` columns: `x_TokenClass`, `x_CacheTtl`, `x_RequestClass`, `x_WorkloadClass`, `x_RecoverableCost` (the scope's RR-p50 Shapley savings allocated by waste share), `x_TopWasteCause`, `x_Basis`, `x_PriceBasis`, `x_RateCardId`, `x_Reconciled`, `x_ReconciliationDeltaPct`, `x_ModelDeveloper`, `x_ModelFamily`, `x_ModelId`, `x_ModelVersion`, `x_SuppressedUsers`.
- **BilledCost honesty rule**: BilledCost = ContractedCost if a contract is loaded, else ListCost, **always** with `x_PriceBasis` and `x_Reconciled`. `--require-reconciled` fails the export (exit 3) unless the period passed `reconcile`. This preserves "never present an estimate as billed" `[ent-reconciliation]`.
- Builders verify the mandatory/nullable status of each column against the FOCUS 1.4 spec (§4.8) and move `x_Model*` into `SkuPriceDetails` once 1.5 is ratified.

**Acceptance tests:**
1. FOCUS CSV totals equal ledger totals per day and per team.
2. Every `x_` name matches `^x_[A-Z][A-Za-z0-9]{1,48}$`.
3. No row has `x_SuppressedUsers > 0` with identifiable dims. k-anon holds.
4. `--require-reconciled` gates correctly.
5. The showback HTML has no `user_pid`/pseudonym anywhere (grep), has tables behind charts, and passes the CSP check.
6. Unit-economics joins are published only when k ≥ 5.

### F10. Budgets, anomaly alerts that name a cause, runaway circuit breaker, forecasts (WP11)

**What.** `budget check`, `watch` and `forecast`.

**Evidence.** `[ent-anomaly-detection]` `[ent-forecast-budgets]` `[runaway-circuit-breaker]` `[uber-policy-arc]` `[blunt-caps-collateral]` `[cc-enterprise-baseline]` `[anth-governance-controls]` `[enforcement-primitives]` `[practice-hit-rate-slo]` `[sequential-monitoring]`.

**$ impact.** Keeps anomaly cost under the FinOps green threshold (< 2% of spend). Single runaway incidents cost $10k–$40k+ `[runaway-circuit-breaker]`. Caps chosen by collateral-damage replay avoid cutting valuable use (blunt caps cut 11–14% including value) `[blunt-caps-collateral]`.

**Algorithm.**
- **Budgets** (config): `{"scope": {"team": "x"} | {"workload_class": "ci"} | {}, "period": "month", "amount_usd": "25000", "alerts": [0.5, 0.8, 1.0]}`. Actuals come from the ledger (contract basis if loaded). Month-end is projected by the forecast. Expected-spend alerts fire at 50/80/100% `[uber-policy-arc]`.
- **Anomaly** (`finops/anomaly.py`): cohorts = team × workload_class × model family; hourly and daily series of `log(cost + $1)`. Baseline = trailing 14-day median and MAD (same hour-of-week for hourly). `z = (x − median)/(1.4826·MAD)`. Alert if `z > 3.5` **and** `Δ$ ≥ min_usd` **and** the cohort has ≥ k users (else roll up). Additional signals:
  - cache-read-share drop > 15 pp vs baseline;
  - write-token spike > 3× baseline;
  - output/thinking share jump > 50%;
  - fast/geo share jump;
  - EWMA (λ = 0.25) level shift over 3 periods.
- **Cause attribution** (every alert names one):
  - decompose `Δcost = cost − baseline` into volume (requests), model mix, context size (mean ctx), cache (miss premium by CACHE-01 cause), output/thinking, premiums, failures (FAIL-02), each as a Laspeyres-style contribution in fixed order;
  - name the largest component and the top finding in the window, e.g. "payments/interactive +$4,210/day: cache miss premium (ttl-expiry-5m), since 2026-09-18 09:00; likely cause: promptCacheTtl change or gateway";
  - report $/day and MTTD. FinOps KPI: anomaly $ / total $ (< 2% green, 2–7% yellow, > 7% red).
- **Runaway** (TAIL-01 streaming): session rolling-1h $ over the threshold → an alert naming `session_pid` and team only (break-glass audit if resolved).
- **Sinks**: stdout JSONL, `file:PATH`, and `webhook` (URL from config only, POST JSON via urllib, off by default).
- **Forecast** (`finops/forecast.py`): driver model per cohort = active devs × active days × cost per active dev-day. Bootstrap (2,000 resamples, seeded) over the last 8 weeks of dev-days gives P50/P90 month-end. "With fixes" subtracts RR-adjusted projections. Price-change calendar applied from `lifecycle.json` (promotion cliffs, retirements) `[cc-timing-calendar]`.
- **Cap recommendations**: per cohort P95/P99 of daily per-dev cost, with a collateral replay (how many dev-days in the last 8 weeks would have been blocked, and their share of commits/PRs if joined). Soft tiers and pooled budgets are recommended over hard per-user caps. The output is formatted for the Console/gateway/AWS Budgets, for a human to apply `[blunt-caps-collateral]` `[ent-cloud-gateway-attribution]`.

**Acceptance tests:**
1. Planted anomalies in the fleet demo (a TTL change on one team from day 20; a runaway session): both detected within one period, with the right cause component and $/day ±10%. No alert on the control team over 28 days (A/A false-positive rate ≤ 1 alert per 30 cohort-days at default thresholds).
2. Budget alerts fire exactly at crossing thresholds.
3. Forecast P50 is within ±10% of the planted month on the demo. P90 ≥ P50.
4. The collateral replay counts blocked dev-days correctly on a fixture.
5. The webhook sink is not called unless configured (socket-blocking test).

### F11. CI cost gate (WP16)

**What.** `tokenbill check` for PRs that change prompts, agents or harness config. It fails on cache regressions, new breakers or cost-per-run regressions, and emits SARIF and a Markdown summary.

**Evidence.** `[ent-ci-cost-gate]` `[practice-hit-rate-slo]` `[promptcachelint-overlap]` `[replay-serialization-drift]` `[ci-headless-ingest]` `[ci-cross-run-cache]` `[cb-attribution-platform]`.

**$ impact.** Prevents silent multi-month regressions. A 25-token status line at the front of a system prompt moved one run from $0.59 to $4.24 `[anth-fleet-benchmarks]`.

**Algorithm.**
- Inputs: trace@1 files (a recorded smoke test), usage@1, or a claude-code-action execution file (`$RUNNER_TEMP/claude-execution-output.json`: SDK messages deduplicated by message id, subagents via `parent_tool_use_id`) `[ci-headless-ingest]`.
- Checks:
  - (1) cache-read share over requests after the second ≥ threshold (default 0.80);
  - (2) no new breaker kinds vs the baseline result JSON (trace@1: legacy `breakers.detect`; usage-only: CACHE-01 causes except `ttl-expiry`, `compaction`, `directory-change`);
  - (3) cost per run ≤ baseline × (1 + tol) (default 15%, since same-task runs vary; recommend ≥ 3 runs and compare medians) `[variance-tails-statistics]`;
  - (4) optional prefix stability: render the recorded request twice with key-order-preserving serialization and diff raw bytes (fingerprint tier). A diff with semantic equality → `serialization-churn` `[cb-key-order]`.
- Outputs: SARIF 2.1.0 (rules `TB-CACHE-SHARE`, `TB-NEW-BREAKER`, `TB-COST-REGRESSION`, `TB-SERIALIZATION-CHURN`), a Markdown summary with "Δ$ per 1,000 runs", and a new baseline JSON. Exit 3 on failure. Docs ship a copy-paste GitHub Actions job pinned by SHA.

**Acceptance tests:**
1. The demo `timestamp` scenario vs the `well-behaved` baseline → fail with TB-NEW-BREAKER (volatile-system) and TB-CACHE-SHARE. `well-behaved` vs itself → pass.
2. An execution-file fixture with split messages is deduplicated.
3. SARIF validates against the 2.1.0 structure (required keys).
4. The cost-regression gate uses medians over runs.

### F12. Savings verification and signed receipts (WP12)

**What.** `tokenbill verify`: causal savings estimates on the constant-rate-card ledger, guards, labels (measured or verified), and signed receipts. `tokenbill ab`: paired lab comparisons for vendor claims.

**Evidence.** `[verified-savings-gap-rtk]` `[llm-token-not-bill]` `[token-not-cost]` `[mv-adjusted-baseline]` `[staggered-did]` `[cuped]` `[heavy-tails-power]` `[ratio-delta-cluster-se]` `[sequential-monitoring]` `[cache-interference-cluster]` `[rtm-targeting]` `[units-gaming]` `[assignment-telemetry-srm]` `[signed-receipts]` `[shapley-attribution]` `[realization-rate-backtest]` `[quality-guardrail]` `[eval-protocol]` `[caltrack-fsu]`.

**$ impact.** Avoids paying for false savings: RTK's claimed 60–90% was actually −7.6% to 0% `[verified-savings-gap-rtk]`. Naive pre/post is off by +16.5% or −31%, and TWFE by −9% `[staggered-did]`. Verification is what makes the other features' dollars bankable for finance.

**Algorithm.**
- **Panel**: metric = cost per active developer-day (ITT, arithmetic mean). Every call in both periods is **repriced with the pre-registered rate card** (card id hashed), and the price variance is reported separately as exact `[finops-rate-vs-usage]`. The unit is the randomization cluster (workspace / MDM group / team). Active = ≥ 1 billed request that day.
- **Estimators** (`verify/estimators.py`, stdlib):
  1. **CUPED-adjusted cluster DiM** for cluster RCTs: θ = cov(Y_post, Y_pre)/var(Y_pre) pooled; `Ỹ = Y_post − θ(Y_pre − mean Y_pre)`; ATT = mean Ỹ(treated) − mean Ỹ(control). Clusters are weighted by active dev-days (ratio metric, delta method).
  2. **Staggered adoption**: Callaway–Sant'Anna ATT(g,t) with not-yet-treated controls on CUPED-adjusted cluster outcomes, aggregated by cohort size into an overall ATT and an event-study by weeks since adoption. The washout week weight is 0.
  3. Inference: **cluster bootstrap** (B = 2,000, seeded, percentile CI) and t critical values with df = clusters − 2 when clusters < 50.
- **Guards** (`verify/guards.py`), all recorded in the receipt:
  - SRM χ² on dev-days by arm (p ≥ 0.001);
  - placebo pre-trend (fake adoption at the pre-period midpoint) within ±MDE/2 with a CI covering 0;
  - MDE by A/A re-randomization (200 draws) with the refusal rule MDE > 0.8 × projected;
  - skewness check (≥ 355·s² units per arm, else aggregate);
  - reconciliation delta ≤ 1% per workspace-month (from F6);
  - calibration pass (F6);
  - quality guardrail non-inferiority, if outcome data exists (Claude Code Analytics PRs per dev-day within a pre-set margin);
  - interference: the cluster must be the cache-isolation unit for cache-touching levers `[cache-interference-cluster]`;
  - looks: only pre-registered looks count (anytime-valid sequences deferred) `[sequential-monitoring]`.
- **Labels**: `verified` iff randomized assignment (hash matches the pre-registration) **and** every guard passes. `measured` for non-random or targeted waves (with an RTM warning) and org-wide ITS. Otherwise `estimated`.
- **Realization rate** = verified ÷ projected (from the Projection at pre-registration). It is stored and updates the priors (empirical-Bayes shrinkage toward the class prior).
- **Receipt** (`verify/receipts.py`):
  - the `urn:tokenbill:receipt:v0` schema of the research (§5.9 there): subject digest of the lever payload, predicate with label, design, metric, result (**integer micro-USD**, ratios in milli-units), guards, adjustments, Shapley attribution, windows, tool version;
  - canonical JSON = `json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False)` over ints and strings only (JCS-compatible for this subset);
  - DSSE PAE = `"DSSEv1" SP len(type) SP type SP len(body) SP body`;
  - signing via `subprocess.run(["ssh-keygen","-Y","sign","-f",key,"-n","tokenbill-receipt"])`, verification via `ssh-keygen -Y verify -f allowed_signers -I <identity> -n tokenbill-receipt -s sig`;
  - no network. If `ssh-keygen` is missing, an unsigned receipt is marked `"signature": null` and the label is capped at `measured`.
- **`tokenbill ab`**: two usage sets tagged with `task_id`, plus an outcomes file (`task_id, success`). Per-task paired cost differences; task-clustered bootstrap; cost per success (failures in the numerator); turn-count and re-read deltas; verdict `cheaper | no-difference | costlier` at 95%. Labelled `estimated (lab)`; the result becomes an RR prior `[llm-token-not-bill]` `[eval-protocol]`.

**Acceptance tests** (synthetic worlds from `demo_fleet --experiment`):
1. A stepped-wedge world with a true 20% effect on 40 workspaces → the CS/CUPED estimate is within ±4% of truth with 95% CI coverage ≥ 90% over 100 seeded worlds (reduced sizes in CI; full in the nightly perf job). Naive pre/post under a planted price cut is visibly biased (the test documents why it is not offered).
2. A planted 55/45 assignment imbalance → the SRM guard fails → label ≤ measured.
3. A planted pre-trend → the placebo fails.
4. MDE refusal when the planted effect is below MDE.
5. Receipt round trip: sign then verify passes. A one-byte tamper fails. The test is skipped with a reason if `ssh-keygen` is absent.
6. `ab` on the RTK-like fixture (tokens down 38%, turns up 14%, cost up 7%) → verdict `costlier`.

### F13. Multi-provider usage adapters (WP4)

**What.** Adapters (each `content_free = True`) for:
- Anthropic Messages API responses and Message Batches results (JSONL of responses or `{request_meta, response}` pairs from gateways; `batch=True` for batch results);
- OpenAI Responses and Chat Completions usage (JSONL);
- Bedrock (Converse responses; model-invocation log records from S3/CloudWatch: `identity.arn` → HMAC, allowlisted `requestMetadata` keys, `output.outputBodyJson.usage`);
- Gemini/Vertex (`usageMetadata`, Interactions API totals, Vertex `trafficType`, model from URL);
- OpenRouter (responses with `usage.cost` plus `/generation` records);
- LiteLLM SpendLogs export (JSON/CSV: key/team/user/tags → HMAC or rules; `api_base` → channel);
- usage@1 (pass-through validation);
- trace@1 → ledger bridge.

**Evidence.** `[multi-provider-normalization]` `[oai-usage-inclusive]` `[oai-56-explicit-cache]` `[oai-service-tiers]` `[oai-batch-longctx-residency]` `[gemini-usage-semantics]` `[gemini-tiers]` `[vertex-claude-geo-labels]` `[bedrock-cache-semantics]` `[bedrock-attribution]` `[bedrock-service-tiers]` `[openrouter-accounting]` `[xai-cost-ticks]` `[deepseek-offpeak-cache]` `[mistral-optin-cache]` `[litellm-spend-and-supply-chain]` `[ent-cloud-gateway-attribution]` `[adapter-adoption-ranking]`.

**$ impact.** Coverage. Most enterprises run several metered agents and providers `[copilot-cursor-token-metering]`. OpenAI now bills cache writes (1.25×), so write-waste and breaker economics apply there too; one Codex/Bedrock case paid $1,780 of writes vs $15.71 of reads `[oai-cache-write-waste]`.

**Algorithm.** The §2.4.7 mappings, channel detection from wrapper metadata or the model-id form, and resolved-tier pricing (Gemini priority overflow billed at standard is visible only in `x-gemini-service-tier`, so the adapter reads it when the wrapper includes headers) `[gemini-tiers]`. Provider-reported cost is carried and cross-checked. DeepSeek time-of-day pricing uses the row's peak windows `[deepseek-offpeak-cache]`.

**Acceptance tests:** a golden fixture per adapter covering inclusive→exclusive conversion (fail on negative uncached), reasoning-in-output, cache-write single class, service tier and batch, long-context band flag, provider-reported cost cross-check (1% tolerance), and HMAC of identity fields. The Vertex example `3 + 900 + 1,054 = 1,957` sum-check passes.

### F14. Content-trace path hardening and recorder upgrade (WP14)

**What.** Keep v0.1's unique byte-level analysis for API workloads and make it correct on best-practice traffic. Upgrade the recorder so API workloads produce fleet-grade, optionally content-free data.

**Evidence.** `[cb-moving-marker]` `[cb-lanes]` `[cb-dup-runid]` `[cb-pricing-coverage]` `[cb-1h-ttl]` `[cb-concurrency]` `[cb-breaker-taxonomy]` `[history-rewrite-kstar]` `[cb-key-order]` `[cb-recorder-coverage]` `[fp-recorder-blindspot]` `[anth-cache-diagnostics-beta]` `[cb-cache-diagnostics]` `[cb-report-scale]` `[cb-security-privacy]`.

**$ impact.** Removes false alarms on healthy traffic: the moving-marker pattern produced a false history-rewrite and 0.000 agreement. It stops phantom savings: 65% for concurrent identical requests that the API cannot deliver. It fixes the 17× interleaved-lane under-report and the $40-vs-$22 double count.

**Rules.**
1. `render_segments` strips `cache_control` keys (message, content-block, tool, system-block level) before rendering. Marker positions are recorded separately `[cb-moving-marker]`.
2. Pricing through the updated `pricing.py` (Opus 5.5 row, `cache_write_1h_multiplier = 2.0`, and the optional `usage.cache_creation` split).
3. Redundancy and breakers are computed per lane: an explicit `lane` field, else split by `(model, sha256(system), sha256(tools))` with longest-prefix assignment `[cb-lanes]`.
4. `run_id` is namespaced by source hash in CLI aggregation `[cb-dup-runid]`.
5. Unknown models give partial totals `[cb-pricing-coverage]`.
6. A `compaction` block or `applied_edits` present → the divergence is labelled `expected-rebuild`, not `history-rewrite`. A mid-conversation `tool_addition` block → `tool-addition` (cache-safe), not `tool-churn` `[cb-breaker-taxonomy]`.
7. When `ttft_ms` is present, a simulator cache entry is readable only from `ts + ttft`, so concurrent identical calls are no longer credited with reads, and a `stage-the-fan-out` note is added `[cb-concurrency]`.
8. The `serialization-churn` breaker fires only when a `wire_sha256` per block exists (fingerprint tier) and sorted-key renderings are equal `[cb-key-order]`.
9. HTML report: CSP meta, and tables behind the SVG charts. Terminal: `core.sanitize.terminal` for all trace-derived strings.

Recorder (`instrument.py`):
- (a) wraps `client.beta.messages.*` and `messages.batches.*`;
- (b) deep-copies (snapshots) the payload **before** sending;
- (c) records the full usage object (5m/1h, iterations, speed, tier, geo, server tools) and request params (thinking, effort/output_config, tool_choice, max_tokens, speed, inference_geo, betas, top-level cache_control);
- (d) records failed and aborted attempts to a sidecar `<trace>.attempts.jsonl` in usage@1 format (never raises into the app);
- (e) uses a background writer thread with a bounded queue (drops with a counter rather than blocking the app);
- (f) `Recorder(path, mode="metadata")` writes **usage@1 only** (content-free) for production services;
- (g) `diagnostics=True` adds the `cache-diagnosis-2026-04-07` beta header and `diagnostics.previous_message_id` per lane (first-party only; documented) `[anth-cache-diagnostics-beta]`.

**Acceptance tests:**
1. **All v0.1 tests pass unchanged**, except `test_dict_key_order_never_looks_like_a_prompt_change`, which stays true for semantic diffs. The flagship `test_demo_recovers_planted_waste.py` passes byte-for-byte.
2. New regression fixtures from the codebase experiments: exp1 moving marker (no breaker, agreement ≥ 0.95); exp2b-a2 concurrent identical (no phantom savings with `ttft_ms`); exp2b-b2 interleaved lanes (per-lane redundancy ≈ 0.73); exp3 duplicate run_id ($22); exp4-4 unknown model (partial totals); exp9 compaction and tool_addition labels.
3. Recorder: beta namespace recorded; mutation after send does not change the record; a failed call appears in the attempts sidecar; metadata mode output contains no content (canary test); background writer never raises.

### F15. Fleet demo with planted causes: the calibration certificate (WP17)

**What.** `tokenbill demo --fleet` builds a deterministic synthetic fleet: 60 developers, 9 teams, 28 days, usage@1 plus a small schema-true Claude Code transcript sample. Waste is **planted** with ground truth computed from the construction. It then runs the whole pipeline and shows each planted cause recovered, like v0.1's demo but at fleet scale. `--experiment` generates stepped-wedge worlds for F12 tests. `--scale N` generates N requests for perf gates.

**Evidence.** DESIGN.md §6 (the demo as calibration certificate), plus the enterprise lab checklist items 7–9 `[ent-reconciliation]` `[cb-validation-ci]`.

**Planted world** (each team ≥ 5 devs except where noted):

| Team | Plant | Expected recovery |
|---|---|---|
| platform | gateway strips `cache_control` (channel `gateway:corp`) | CACHE-03 no-cache, $ within ±5% of construction |
| payments | API-key 5m TTL, 12% of gaps in 5–60 min | TTL-01 recommends 1h for main; savings within ±5% |
| search | sessions grow to 900k, no compaction below 967k | CTX-01 pool, CTX-04 curve (400k savings within ±5% of the construction's replay) |
| mobile | cold resumes after 1–8 h idle at 400–600k | CACHE-02, $ within ±5% |
| infra | 2 devs with sticky fast mode, 1 with sticky xhigh | MODEL-02 exact premium; MODEL-03 SELF findings; ORG view shows "(<k users)" suppressed |
| data | workflow agents on Opus 5; Fable 5 in main | MODEL-05 (tradeoff), MODEL-04 exact repricing (fable-5 → 5.1, opus-5 → 5.5) |
| ops | one runaway loop session (3 h, 400 requests, no human prompt) | TAIL-01 alert |
| ci-bots | claude-code-action runs with dynamic system sections, version drift | AUTO-01 CI share; AUTO-02 cross-run misses |
| core | healthy control | no finding ≥ $min_usd |
| tiny (3 devs) | normal usage | suppressed/merged in showback |

Also: refusal fallbacks with iterations (ledger correctness), placeholder-usage calls, and a reference cost report with Priority-tier, provisional-day and 12%-discount residuals (F6).

**Acceptance (flagship v2, `tests/fleet/test_fleet_flagship.py`, run in wave 2):**
1. Every plant is recovered in the right scope with $ within the stated tolerance.
2. The control team is clean.
3. Reconcile passes with every residual classified.
4. Showback suppression holds.
5. The content canary is absent everywhere.
6. JSON output is byte-identical across two processes.
7. `demo --fleet` runs keyless and networkless in < 60 s.

### F16. Enterprise hardening and the private self-view (WP15, WP13)

**What.**
- **Supply chain**: SHA-pinned actions, top-level `permissions: {}`, Dependabot (actions + uv), zizmor, CodeQL, OpenSSF Scorecard, a CycloneDX SBOM (a stdlib script over the package: no runtime deps, dev deps listed), `actions/attest-build-provenance`, immutable releases, `SOURCE_DATE_EPOCH` reproducible builds with a rebuild-and-diff job, `docs/VERIFY.md`, `docs/THREAT-MODEL.md`, and a CVD policy with 24h/72h-compatible SLAs (EU CRA).
- **Robustness**: stdlib fuzzers (seeded byte/field mutation of every adapter fixture; only `TokenbillError` may escape; bounded time and memory); a **no-egress test** (socket creation patched to raise for the whole CLI except `pull`, `otlp-receive`, webhook and `pricing verify --online`); report CSP; ANSI/C0/C1 stripping; WCAG tables.
- **`tokenbill me`**: a private self-view from local transcripts. Own spend per active day vs the $13/$30 anchors, own cache health, own findings (SELF audience: sticky escalations, cold resumes, largest tool outputs), `--secrets-scan`. No peer data.

**Evidence.** `[ent-supply-chain-incidents]` `[ent-provenance-sbom]` `[ent-scorecard-osps]` `[ent-eu-cra]` `[ent-report-a11y-hardening]` `[cb-security-privacy]` `[cb-validation-ci]` `[litellm-spend-and-supply-chain]` `[price-salience-mixed]` `[channel-code-review-hooks]` `[aggregation-k5]`.

**$ impact.** A gate for approval. Self-views and price salience are hygiene with low single-digit effects `[price-salience-mixed]`.

**Acceptance tests:** the fuzz suite passes 10⁴ mutations per adapter in the nightly job (10³ in PR CI); the no-egress test passes; CSP and a11y checks pass on every HTML output; `me` output contains no other user's data; release workflow lint (zizmor) is clean; the SBOM lists zero runtime components.

---

## 4. Pricing, billing and schema facts the build depends on

Status column: **V** = confirmed or corrected by the research fact-check pass (as of 2026-09-23); **R** = not captured with certainty. The builder must fill R items from the primary source and record `verified_on`. Every row goes into `rates/data/*.json` (prices), `rates/data/lifecycle.json` (dates) or `rates/data/benchmarks.json` (published benchmarks used in reports), each with a source URL. **All builders re-verify V items against the primary source before release**: three Anthropic price changes landed within one quarter `[anth-evidence-drift]`.

### 4.1 Anthropic model rates (USD per MTok) — source https://platform.claude.com/docs/en/about-claude/pricing(.md)

| Model | Input | Output | Cache read (mult) | 5m write | 1h write | Min cacheable | Fast (in/out) | Status |
|---|---|---|---|---|---|---|---|---|
| claude-opus-5-5 (from 2026-09-22) | 4.00 | 20.00 | 0.20 (0.05×) | 5.00 (1.25×) | 8.00 (2×) | 512 | 8.00 / 40.00 | V `[anth-pricing-table-2026-09]` |
| claude-fable-5-1 / claude-mythos-5-1 | 10.00 | 50.00 | 0.25 (0.025×) | 1.25× | 2× | 512 | — | V |
| claude-fable-5 / claude-mythos-5 | 10.00 | 50.00 | 1.00 (0.1×) | 1.25× | 2× | 512 | — | V |
| claude-opus-5 | 5.00 | 25.00 | 0.50 (0.1×) | 1.25× | 2× | 512 | 10.00 / 50.00 | V |
| claude-opus-4-8 | 5.00 | 25.00 | 0.1× | 1.25× | 2× | 1,024 | 10.00 / 50.00 | V |
| claude-opus-4-7 | 5.00 | 25.00 | 0.1× | 1.25× | 2× | 2,048 (Bedrock lists 4,096) | — | V `[gemini-bedrock-deepseek-rules]` |
| claude-opus-4-6 / claude-opus-4-5 | 5.00 | 25.00 | 0.1× | 1.25× | 2× | 4,096 | — | V |
| claude-opus-4-1 / claude-opus-4 | 15.00 | 75.00 | 0.1× | 1.25× | 2× | R | — | V price (retired on 1P, still billed on partner clouds) `[cc-timing-calendar]`; min R |
| claude-sonnet-5 | 2.00 | 10.00 | 0.1× | 1.25× | 2× | 1,024 | — | V (the $3/$15 increase was cancelled) |
| claude-sonnet-4-6 / claude-sonnet-4-5 | 3.00 | 15.00 | 0.1× | 1.25× | 2× | 1,024 | — | V |
| claude-sonnet-4 | R | R | 0.1× | 1.25× | 2× | R | — | R `[cb-pricing-coverage]` |
| claude-haiku-4-5 | 1.00 | 5.00 | 0.1× | 1.25× | 2× | 4,096 | — | V |
| claude-haiku-3-5 | R | R | 0.1× | 1.25× | 2× | 2,048 | — | R price; V min. Only model whose cache reads count toward ITPM `[anth-cache-rate-limits]` |

Tokenizer families: **claude-2026** = Opus 4.7/4.8/5/5.5, Sonnet 5, Fable, Mythos (about 30% more tokens for the same text, 1.0–1.35× by content; CJK ~1.01×). **claude-legacy** = older models `[anth-tokenizer-inflation]` `[tokenizer-inflation-47plus]`. The 1M context window carries no price premium `[cc-autocompact-window]`.

### 4.2 Anthropic modifiers and non-token charges

| Fact | Value | Source / status |
|---|---|---|
| Cache write multipliers | 5m 1.25×, 1h 2× base input. Break-even: 5m after 1 read, 1h after 2 reads. Mixed TTLs: longer first | pricing + prompt-caching docs. V `[anth-cache-1h-2x]` |
| Batch | 0.5× on every token category incl. cache reads/writes; stacks with caching and residency; not with fast mode, fallbacks, max_tokens 0 or Managed Agents; batch cache hits best-effort 30–98% | V `[anth-batch-stacking]` |
| Data residency | `inference_geo: "us"` = 1.1× on all token categories (4.6+ models; 1P and Claude Platform on AWS; Foundry US Data Zone equivalent) | V `[anth-modifiers-geo-fast-priority]` `[claude-marketplace-ccu]` |
| Regional cloud endpoints | Bedrock / Vertex regional and multi-region +10% vs global | V `[vertex-claude-geo-labels]` `[bedrock-price-list-api]` |
| Fast mode | 1P only; Opus 5.5 $8/$40, Opus 5 and 4.8 $10/$50; cache multipliers apply on top | V |
| Priority Tier | no longer sold; excludes Opus 5.5, Opus 5, Sonnet 5, Fable 5.1, Mythos; existing commitments burn at read 0.1, 5m 1.25, 1h 2.0, US 1.1; **absent from cost_report**, visible in usage_report `service_tier=priority` | V `[cc-priority-legacy]` `[cc-capacity-model-lag]` |
| Web search | $10 per 1,000 searches plus result tokens; failed searches not billed | V `[anth-web-search-controls]` `[fp-billing-matrix]` |
| Web fetch | tokens only | V |
| Code execution | 1,550 free container-hours/org/month, then $0.05/hour, 5-minute minimum; free when used with web_search/web_fetch 20260209+; appears only in cost_report | V `[anth-ptc-code-exec]` `[anth-admin-usage-cost-api]` |
| Managed Agents | $0.08 per running session-hour; no batch; session budgets pause with `budget_reached` | V `[anth-governance-controls]` |
| CCU (Claude Platform on AWS, Foundry) | $0.01 per unit; discounts applied as fewer units; hourly single line; Claude Platform on AWS has no usage/cost API; spend caps computed at list | V `[cc-ccu-private-offers]` |
| Refusal billing | pre-output decline not billed (still counts for rate limit); mid-stream decline bills input + streamed output; `usage.iterations` is the per-attempt record | V `[fp-anth-refusal-iterations]` |
| Iterations | compaction and advisor iterations are excluded from top-level usage; billed total = Σ iterations; advisor iterations bill at the advisor model's rates | V `[anth-iterations-undercount]` |
| Fallback credit | beta `fallback-credit-2026-07-01`; retry within 5 min with an exactly matching body reprices the cached span as reads | V `[fp-fallback-credit]` |
| Hidden tool-use overhead | tool-use system prompt 286 tokens (Opus 5.5 auto/none), 354/474 (Sonnet 5), 675/804 (Opus 4.7); bash 244–325; text editor ~700; computer use ~4,500; browser ~6,600 | V `[anth-hidden-overhead-tokens]` |
| Images | tokens = ceil(w/28)·ceil(h/28); caps 1,568 (standard) and 4,784 (4.7+ high-res, long edge 2,576 px) | V `[anth-vision-tokens]` |
| Rate limits | cache reads don't count toward ITPM (except Haiku 3.5); cache writes do; OTPM counts actual output; spend caps Start $500 / Build $1,000 / Scale $200,000 | V `[anth-cache-rate-limits]` |
| Org enterprise anchors | ~$13 per developer per active day; $150–250 per developer per month; 90% of users < $30 per active day | V https://code.claude.com/docs/en/costs `[cc-enterprise-baseline]` |

### 4.3 Anthropic prompt-cache rules — sources https://platform.claude.com/docs/en/build-with-claude/prompt-caching, …/cache-diagnostics

| Rule | Value | Status |
|---|---|---|
| TTL | 5 min or 1 h, measured **from request start** (generation time counts); reads refresh the TTL free | V `[anth-ttl-start-1h]` |
| Visibility | an entry is readable only after the first response begins streaming (parallel identical requests all pay) | V `[anth-concurrency-fanout]` |
| Lookback | 20 positions per breakpoint; a run of tool_use (or tool_result) blocks counts as one position | V `[anth-lookback-20]` (used by F14 only in v0.2) |
| Breakpoints | max 4 per request; automatic caching via top-level `cache_control`; server tools auto-insert 5m breakpoints (not breakers) | V `[cb-lookback]` `[anth-web-search-controls]` |
| Isolation | per workspace on 1P, Claude Platform on AWS, Foundry; per organization on Bedrock and Vertex | V `[anth-workspace-fleet-scope]` |
| Invalidation hierarchy | tools or model change → all tiers; speed, web search, citations, system change → system + messages; tool_choice, images → messages; thinking/effort → messages (on some models all) | V `[anth-invalidation-hierarchy]` |
| Cache-preserving APIs | mid-conversation `role:system` messages (Opus 5/5.5/4.8, Fable, Mythos; **not Sonnet 5**); `tool_addition`/`tool_removal` (beta `mid-conversation-tool-changes-2026-07-01`); inline tools (beta `inline-tools-2026-09-15`, 1P); per-message effort (beta `mid-conversation-output-config-2026-07-01`; Fable 5.1, Mythos 5.1, Opus 5, Opus 5.5) | V `[anth-cache-preserving-apis]` |
| Diagnostics | beta header `cache-diagnosis-2026-04-07` + `diagnostics.previous_message_id` on every request; `cache_miss_reason.type ∈ {model_changed, system_changed, tools_changed, messages_changed, previous_message_not_found, unavailable}`; `cache_missed_input_tokens` is a magnitude estimate, not billing; first-party only; only the first divergence; ZDR-eligible except Covered Models (Fable, Mythos 5.x) | V `[anth-cache-diagnostics-beta]` |
| Keepalive | re-send with `max_tokens: 0` within 4 min of the previous start and every 4 min; bills a cache read; rejected with stream:true, structured outputs, forced tool_choice, in batches | V `[anth-ttl-choice-keepalive]` |
| TTL rule of thumb | 1h pays when > 1 in 20 gaps fall between 5 and 60 min; with no pauses, 5m was 15% cheaper (Sonnet 5), 11% (Opus 5) | V `[anth-fleet-benchmarks]` |
| Keepalive break-even | `I_max = τ(w/r − 1)` ≈ 46 min at 0.1× reads, ≈ 96 min Opus 5.5, ≈ 196 min Fable 5.1 (derived) | V `[keepalive-economics]` |
| Cache benchmarks | median 84% of input from cache; top decile ≥ 94%; < 80% → look for a breaker; caching cut agent-loop cost 2.7–5.3× (live guide; the older 2.5–3.7× is stale) | V `[anth-cache-health-thresholds]` `[cache-health-benchmarks-diagnostics]` |
| Compaction API | `compact-2026-01-12` default trigger 150,000 input tokens (min 50,000); on-demand `compact-2026-09-04` | V `[anth-compaction-iterations-billing]` |
| Context editing | `clear_tool_uses_20250919` defaults: trigger 100,000, keep 3; `applied_edits` reports `cleared_input_tokens` | V `[anth-context-editing-not-savings]` |

### 4.4 Claude Code facts — sources https://code.claude.com/docs/en/{costs, monitoring-usage, settings-reference, env-vars, prompt-caching, model-config, managed-settings, server-managed-settings, claude-apps-gateway-spend-limits}

**Transcripts** (internal format, changes between versions; retention `cleanupPeriodDays`, default 30) `[cc-transcript-format]`:
- Paths: `~/.claude/projects/<slug>/<session>.jsonl`; subagents `<session>/subagents/agent-*.jsonl` + `*.meta.json`; workflow agents under `…/workflows/…`.
- Assistant entry fields (observed, v2.1.2xx): `message.{id, model, usage, stop_reason, content[].type, diagnostics.cache_miss_reason.{type, cache_missed_input_tokens}, input_transformations[], context_management.applied_edits}`, `requestId`, `sessionId`, `agentId`, `isSidechain`, `uuid`, `parentUuid`, `timestamp`, `effort`, `perTurnEffort`, `advisorModel`, `attributionAgent|Skill|McpServer|McpTool|Plugin`, `entrypoint`, `version`, `cwd`, `gitBranch`, `isApiErrorMessage`, `apiErrorStatus`.
- `usage`: `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `cache_creation.{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}`, `output_tokens`, `output_tokens_details.thinking_tokens`, `server_tool_use.{web_search_requests, web_fetch_requests}`, `service_tier`, `speed`, `inference_geo` (often `"not_available"`), `iterations[].{type, model, input_tokens, cache_read_input_tokens, cache_creation_input_tokens, cache_creation{…}, output_tokens}`.
- System subtypes: `compact_boundary` (`compactMetadata.{preTokens, postTokens, durationMs, trigger, cumulativeDroppedTokens}`), `model_refusal_fallback` (`originalModel`, `fallbackModel`), `api_error` (`error.status`, `retryAttempt`, `maxRetries`, `retryInMs`). Also `cost-state` entries (`totalCostUSD`, `modelUsage`).
- **Traps** (V, `research/03_dedup_check.out`, `04_semantics.out`): 2.39 lines per `message.id`; output is monotone across split entries, so keep the max; 105–444 duplicate-uuid lines; `requestId` is 1:1 with `message.id`; `message.id` never spans files; fallback pairs have top-level usage = last iteration; ~10% of subagent/workflow calls lack `stop_reason` with placeholder output (p50 3 tokens) `[fp-cc-missing-final-usage]`; `toolUseResult.totalTokens` = the subagent's final context, not spend; compaction summary calls are not logged `[cc-hidden-calls-rollups]`.

**OTel** (enable `CLAUDE_CODE_ENABLE_TELEMETRY=1`; exporters via `OTEL_*_EXPORTER`) `[cc-otel-schema]` (corrected):
- Metrics: `claude_code.token.usage` (`type ∈ {input, output, cacheRead, cacheCreation}`), `claude_code.cost.usage` (USD, list price unless `modelPricing`). Attributes: `model`, `query_source ∈ {main, subagent, auxiliary}`, `speed`, `effort`, `agent.name`, `skill.name`, `plugin.name`, `marketplace.name`, `mcp_server.name`, `mcp_tool.name`. Also session/LOC/commit/PR/edit-decision/active-time counters.
- Standard attributes: `session.id`, `organization.id`, `user.account_uuid`, `user.account_id`, `user.id`, `user.email` (**not populated** for API-key, Bedrock, Vertex and Foundry sessions), `terminal.type`; opt-in `app.version`, `app.entrypoint` (`OTEL_METRICS_INCLUDE_*`), `vcs.*` (`OTEL_METRICS_INCLUDE_REPOSITORY`, v2.1.269+); custom `OTEL_RESOURCE_ATTRIBUTES`.
- Events: `claude_code.api_request` (`model`, `request_id`, `client_request_id`, `duration_ms`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `cost_usd`, `speed`, `effort`, `query_source`, success/status); `claude_code.tool_result` (`tool_name`, `success`, `duration_ms`, `tool_result_size_bytes`); `claude_code.api_error`; `claude_code.user_prompt`; `claude_code.assistant_response`. Opt-in raw bodies `api_request_body`/`api_response_body` (inline truncated at 60 KB; file mode untruncated). **`ttft_ms` is on the beta `llm_request` span, not the event.**
- Beta traces (`CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`): `claude_code.llm_request` (tokens, `agent_id`, `parent_agent_id`, `stop_reason`, `effort`, `ttft_ms`), `claude_code.tool` (`result_tokens`).

**Managed settings keys** (values and format: builder verifies against settings-reference) `[cc-managed-settings-levers]` (corrected):
- Models: `availableModels`, `enforceAvailableModels`, env `ANTHROPIC_DEFAULT_*_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL`.
- Effort and fast: `maxEffortLevel` (v2.1.267+, every provider); `fastModePerSessionOptIn`, `CLAUDE_CODE_DISABLE_FAST_MODE`.
- Context: `autoCompactWindow` (100K–1M; the default ≈ 967K on 1M-context models) + env `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (**needed because the `--autocompact` flag is not preempted by managed settings**), `CLAUDE_CODE_DISABLE_1M_CONTEXT`, `bashOutputMaxChars` (default 30,000), `MAX_MCP_OUTPUT_TOKENS` (warn 10K, cap 25K), `skillListingBudgetFraction`, `claudeMdExcludes`.
- Caching: `promptCacheTtl`, `subagentPromptCacheTtl`, env `CLAUDE_CODE_PROMPT_CACHE_TTL`, `ENABLE_PROMPT_CACHING_1H`, `FORCE_PROMPT_CACHING_5M`. Defaults: subscription main conversation 1h; API key, cloud provider, usage credits and all subagents 5m `[cc-ttl-policy]`.
- Tools: `ENABLE_TOOL_SEARCH` (a non-first-party `ANTHROPIC_BASE_URL` disables tool search by default); `workflowSizeGuideline` (advisory, not enforced).
- Hygiene: `requiredMinimumVersion`, `cleanupPeriodDays`, `companyAnnouncements`, `spinnerTipsOverride`.
- Reporting: `modelPricing` (managed-only, v2.1.242+; `multiplier` + per-model `overrides{input, output, cacheRead, cacheWrite}`; `cacheWrite` covers both 5m and 1h).
- Delivery: server-managed settings are polled hourly and apply org-wide (no per-group). MDM refreshes every 30 min. File-based settings reload on change. Per-IdP-group settings are possible via the Claude apps gateway `[stepped-wedge-mdm]` (corrected).
- Hooks: SessionStart receives resume-cost fields (`context_tokens`, `prompt_cache_likely_expired`, `estimated_cache_write_usd`) `[channel-code-review-hooks]`.

**Claude Code cache breakers** (documented): model switch (incl. opusplan toggles, automatic fallback, skill `model:` frontmatter); effort change (except Opus 5.5 and Fable 5.1 on API key or subscription, not Bedrock/Vertex); first enable of fast mode; MCP connect/disconnect when tools load into the prefix; plugins with MCP servers; deny rules without tool search; `/compact`; image eviction; upgrades. Cache scope is per machine and directory (worktrees don't share). A gateway that strips `cache_control` bills the whole history uncached `[cc-cache-breakers]` `[cc-invalidators-taxonomy]` `[gateway-strip]`. `/usage` counts a miss at > 5% and ≥ 2,000 tokens re-processed `[cc-usage-likely-cause]`.

**Headless and CI**: claude-code-action writes `$RUNNER_TEMP/claude-execution-output.json` and sets `CLAUDE_CODE_ENTRYPOINT=claude-code-github-action`. `claude -p --output-format json|stream-json` (dedupe stream-json by message id). `--exclude-dynamic-system-prompt-sections`. `--bare` `[ci-headless-ingest]` `[ci-cross-run-cache]`.

### 4.5 Other providers

| Provider | Facts | Source / status |
|---|---|---|
| OpenAI GPT-5.6+ | cache writes 1.25×, reads 0.1×; `prompt_cache_options.ttl="30m"` only, refreshed free on reuse; min 1,024 tokens; implicit mode 1 write slot + up to 3 explicit; explicit mode up to 4; matching over the first 2 and latest 50 explicit breakpoints; usage inclusive (`input_tokens` ⊇ `cached_tokens` + `cache_write_tokens`); reasoning ⊂ output; `prompt_cache_diagnostics` via `comparison_response_id` (9 miss reasons, free) | https://developers.openai.com/api/docs/guides/prompt-caching. V `[oai-56-explicit-cache]` `[oai-usage-inclusive]` `[oai-cache-diagnostics]` |
| OpenAI pre-5.6 | no write fee; cached rounded down to 128; retention `in_memory` (5–10 min idle, ≤ 1 h) vs `24h` (default for non-ZDR since 2026-05-29) | V `[oai-retention-defaults]` |
| OpenAI tiers | Flex 0.5× (429s not charged); Fast (ex-Priority, renamed 2026-07-30) 2×; Batch 0.5×; long-context rows > 272K (gpt-5.6-sol $8 in / $0.80 cached / $10 write / $30 out vs $4 / $0.40 / $5 / $20); regional processing +10% for models released ≥ 2026-03-05 | V `[oai-service-tiers]` `[oai-batch-longctx-residency]` |
| OpenAI Admin | usage buckets `input_uncached_tokens`, `input_cached_tokens`, `input_cache_write_tokens`, `output_tokens`, group_by project_id, user_id, api_key_id, model, batch, service_tier; Costs by project_id/line_item/api_key_id | V `[oai-admin-usage-costs]`; endpoint paths R |
| Azure OpenAI | in_memory default for gpt-5.4 and older; no cross-subscription sharing; bills processed requests incl. 400 content-filter and 408 timeouts; PTU-M lacks breakpoints | V `[azure-openai-caching]` `[fp-billing-matrix]` |
| Gemini | `promptTokenCount` includes `cachedContentTokenCount`; `thoughtsTokenCount` outside candidates, billed as output; implicit caching 90% off (cached = 10% of input on current models); explicit caches billed per storage hour; 3.1 Pro $2.00/$12 ≤ 200K and $4.00/$18 above; 3.8 Flash $0.75 input to 2026-12-31 then $1.50 (caching/storage/flex/priority also double); Flex 0.5×, Priority ~1.8× with silent overflow to Standard (header `x-gemini-service-tier`); Vertex `trafficType` | V `[gemini-usage-semantics]` `[gemini-caching]` `[gemini-tiers]` `[gemini-effective-dated-prices]` |
| Bedrock | Converse `inputTokens` excludes cache; `cacheReadInputTokens`, `cacheWriteInputTokens`, `cacheDetails[{ttl, inputTokens}]`; no caching in batch inference; cross-region inference may increase writes; Opus 5 $5.00 global vs $5.50 regional, 1h write $10 vs $11, batch input $2.50; Priority 1.75×, Flex 0.5×; resolved tier in response/CloudWatch; invocation logs carry `identity.arn`, `requestMetadata`, cache counts only inside the logged body; mantle not in logs; CUR 2.0 `line_item_iam_principal` | V `[bedrock-cache-semantics]` `[bedrock-price-list-api]` `[bedrock-service-tiers]` `[bedrock-attribution]` `[ent-cloud-gateway-attribution]` |
| Vertex (Claude) | model id in URL; +10% regional/multi-region; no Message Batches or Usage & Cost API; labels to billing export | V `[vertex-claude-geo-labels]` |
| OpenRouter | `usage.cost`, `cached_tokens`, `cache_write_tokens`, `reasoning_tokens`, `upstream_inference_cost`; `/generation` native tokens, provider, service tier; sticky routing 10 min | V `[openrouter-accounting]` |
| xAI | `usage.cost_in_usd_ticks` (1 USD = 10¹⁰ ticks) is billed truth; all rates double at ≥ 200K; grok-4.7 $2 / $0.50 cached / $6 | V `[xai-cost-ticks]` |
| DeepSeek | `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`; peak 2× (01:00–04:00 and 06:00–10:00 UTC Mon–Fri, excl. Chinese holidays) | V `[deepseek-offpeak-cache]` |
| Mistral | opt-in `prompt_cache_key`; cached in 64-token blocks at 10%; batch 50% | V `[mistral-optin-cache]` |
| Machine price feeds | LiteLLM map, OpenRouter models API, AWS Price List API (`AmazonBedrockFoundationModels`, `AmazonBedrock`): **cross-checks only** | V `[price-feed-crosscheck]` |

### 4.6 Reporting APIs (connectors; field names to re-verify against docs before release)

| API | Endpoint | Key facts | Source / status |
|---|---|---|---|
| Anthropic Usage | `GET /v1/organizations/usage_report/messages` | Admin API key; buckets 1m (≤ 1,440), 1h (≤ 168), 1d (≤ 31); group_by `api_key_ids`/`workspace_ids`/`models`/`service_tiers`/`context_window`/`inference_geos`/`speeds`; uncached, cache read, cache_creation 5m/1h, output, web_search_requests; ~5 min latency; poll ≤ 1/min; not on Claude Platform on AWS | https://platform.claude.com/docs/en/build-with-claude/usage-cost-api. V `[anth-admin-usage-cost-api]`; exact param spellings R |
| Anthropic Cost | `GET /v1/organizations/cost_report` | daily; USD cents as **decimal strings**; group_by workspace_id / description (parsed model, cost_type, token_type, inference_geo); excludes Priority Tier; code execution only here | V |
| Claude Code Analytics | `GET /v1/organizations/usage_report/claude_code?starting_at=YYYY-MM-DD` | per user or API key per day: `core_metrics` (sessions, LOC, commits, PRs), `tool_actions` accept/reject, `model_breakdown[].tokens{input, output, cache_read, cache_creation}`, `estimated_cost.amount` (cents); ≤ 1 h lag; ≤ 1,000 per page; excludes Bedrock/Vertex/Foundry | https://platform.claude.com/docs/en/manage-claude/claude-code-analytics-api. V `[cc-admin-analytics-apis]` |
| Enterprise Analytics | `/v1/organizations/analytics/{usage_report, user_usage_report, cost_report, user_cost_report}` | `read:analytics` key; group_by product, model, context_window, cost_type, token_type, speed, inference_geo, rbac_group_id; 1m/1h/1d; data from 2026-01-01; ≤ 31 days per query; `amount` (post-discount, pre-credit) and `list_amount`; revisable 30 days; ~60 rpm; `/skills` has `attributed_list_price`, `/plugins` counts only | https://platform.claude.com/docs/en/api/beta/organization/analytics. V `[anth-claude-code-analytics-otel]` `[cc-anthropic-discount-visibility]` |
| OpenAI Usage / Costs | `organization.usage.completions`, costs endpoint | see 4.5 | V fields; paths R |

### 4.7 OpenTelemetry GenAI and OpenInference names

- GenAI (repo `open-telemetry/semantic-conventions-genai`, status Development; moved in semconv v1.42.0, 2026-06-12): `gen_ai.usage.input_tokens` (**includes** cached), `gen_ai.usage.output_tokens`, `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_write.input_tokens` (legacy `cache_creation.input_tokens` from v1.40.0), `gen_ai.usage.reasoning.output_tokens`, `gen_ai.provider.name` (anthropic, aws.bedrock, azure.ai.openai, gcp.gemini, gcp.vertex_ai, deepseek, mistral_ai, x_ai, openai, …; legacy `gen_ai.system`), `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.response.id`, `gen_ai.conversation.id`, `gen_ai.conversation.compacted`, `openai.response.service_tier`, `error.type`, `gen_ai.response.finish_reasons`. Counters `gen_ai.client.inference.usage.*` (require `gen_ai.token.modality`); histograms are not for cost totals. Retries: reuse `http.request.resend_count`. V `[otel-genai-semconv]` `[ent-otel-genai-semconv]` `[fp-trace-fields-standards]`.
- OpenInference: `openinference.span.kind` (LLM, AGENT, CHAIN, TOOL…), `llm.model_name`, `llm.provider`, `llm.token_count.prompt`, `llm.token_count.completion`, `llm.token_count.prompt_details.cache_read`, `…cache_write`, `llm.token_count.completion_details.reasoning`. V `[otel-openinference-importer]`.
- OTLP/JSON encoding: int64 fields (`intValue`, `timeUnixNano`) are JSON **strings**. File format: https://opentelemetry.io/docs/specs/otel/protocol/file-exporter/. R (confirm while building fixtures).

### 4.8 FOCUS

FOCUS 1.4 was ratified 2026-06-04 (2 datasets, 47 columns). `ServiceCategory` includes "AI and Machine Learning". Custom columns **must** use the `x_` prefix (PascalCase, ≤ 50 chars). Allocation columns (`AllocatedMethodId`, `AllocatedMethodDetails`, `AllocatedResourceId`, `AllocatedTags`) date from 1.3. FOCUS 1.5 (target 2026-12-03) adds `SkuPriceDetails` model identity properties (`ModelDeveloper`, `ModelFamily`, `ModelId`, `ModelVersion`; merged), `TokenCacheAction`/`TokenDirection` (in review) and `PrincipalId` (merged). Token counts map to `ConsumedQuantity`/`ConsumedUnit` (issue #2018, closed 2026-07-27). V `[ent-focus-1-4]` `[focus-15-ai-columns]`. **R**: the builder checks the mandatory/nullable status of every emitted column at https://focus.finops.org/focus-specification/.

### 4.9 Signing and receipts

DSSE PAE `"DSSEv1" SP len(type) SP type SP len(body) SP body` (https://github.com/secure-systems-lab/dsse/blob/master/protocol.md). `ssh-keygen -Y sign -n <namespace>` / `-Y verify -f allowed_signers -I <id> -n <namespace> -s <sig>` (OpenSSH; tested with 10.3). Canonical JSON over ints and strings is JCS-compatible (RFC 8785). V `[signed-receipts]`.

---

## 5. Work-package partition for parallel builders

### 5.1 Rules for every builder

1. **Frozen foundation.** `tokenbill/core/*` (WP0) is merged before wave 1 starts and is **read-only** during wave 1. A needed change is filed as a "core change request" to the integrator. Wave-1 code must not monkeypatch core.
2. **Disjoint files.** Only the files listed for a WP may be created or edited by it, plus its own `tests/<area>/` and `tests/fixtures/<wp>/`. The `__init__.py` of shared subpackages (`ingest`, `connect`, `detect`, `finops`) belongs to WP0 (docstring only). Registration is via the string maps already present in `core/registry.py`. POSIX-only checks (file modes) are skipped on Windows.
3. **Cross-WP calls** use only the signatures in §5.3. Tests substitute `core.testing` fakes (`MemoryReader`, `MemoryWriter`, `TablePricer`, `MemoryDetectContext`, `make_request`, `make_lane`, `CANARY`) so no wave-1 WP waits on another.
4. **No network** in any test. A session-scoped autouse fixture in `tests/conftest.py` (WP0) blocks `socket.socket`. Connector tests use `--replay` fixtures.
5. **Fixtures are synthetic and schema-true** (field inventories in §4 and `research/02_schema.out`). No real transcripts are committed. Every fixture that can carry content embeds `core.testing.CANARY`.
6. Style (unchanged from v0.1): `from __future__ import annotations`, full type hints, frozen dataclasses, stdlib logging, docstrings, `ruff` clean, determinism via `common.rng`. Python 3.10 compatible (no `tomllib`, no `match` on 3.10-incompatible syntax, `slots=True` is fine).
7. **Money**: no floats for money in any stored or exported value. Use `core.money`.
8. **Labels**: every function that returns money returns it with a `Basis` (dataclass field or tuple). JSON goes through `core.labels.money_json()`.
9. Test markers: `perf` (nightly), `slow`, `needs_ssh_keygen`. PR CI runs everything except `perf`.

### 5.2 Packages, order and sizes

**Wave 0** (1 builder, merged first): WP0.
**Wave 1** (up to 18 builders in parallel): WP1–WP17 (WP8 split into 8a and 8b).
**Wave 2** (1–2 builders): WP-I integration, e2e, docs. Then adversarial review.

| WP | Title | Owns (files) | Provides (key interfaces) | Depends on | Size (LOC incl. tests) |
|---|---|---|---|---|---|
| **WP0** | Foundation | `tokenbill/core/{__init__,records,results,money,ids,labels,protocols,registry,config,streams,sanitize,testing}.py`; `__init__.py` of `ingest/`, `connect/`, `detect/`, `finops/`; `tests/core/*`; `tests/conftest.py`; `docs/SPEC-0.2-interfaces.md`; `docs/schemas/usage-1.schema.json` | all §2.4 records incl. `PolicySet`, `LeverSpec`; §2.6/§2.11 protocols; `results.py` (§5.3.1); usage@1 (de)serializer + validator; `Pseudonymizer`; money helpers; JSONL streaming with offsets, line cap, gzip, quarantine; terminal sanitizer; registry string maps for every built-in; JSON config loader with precedence; fakes for tests | — | ≈2.3k |
| **WP1** | Rate engine v2 | `tokenbill/rates/*` incl. `data/*.json` (anthropic, openai, google, aws, others, server_tools, lifecycle, benchmarks); `tests/rates/*` | `load_pricer`, `Pricer` impl, `canonical_model`, contract import/emit (`modelPricing`), `verify_card` (offline + `--online`), `contract_from_reconcile` | WP0 | ≈2.0k |
| **WP2** | Claude Code collector | `tokenbill/ingest/{claude_code,cc_layout}.py`; `tests/ingest/test_claude_code*`; `tests/fixtures/wp2/**` | `ClaudeCodeTranscripts` adapter; `collect()` | WP0 | ≈2.0k |
| **WP3** | OTel ingest + receiver | `tokenbill/ingest/{otlp,cc_otel,genai,conventions,otlp_receiver}.py`; tests + fixtures | `OtlpJson` adapter; `serve()`; convention registry | WP0 | ≈2.2k |
| **WP4** | Provider adapters | `tokenbill/ingest/{anthropic_api,openai,bedrock,gemini,openrouter,litellm,usage_jsonl,trace_v1}.py`; tests + fixtures | 8 adapters | WP0 | ≈2.2k |
| **WP5** | Connectors, reconcile, calibrate | `tokenbill/connect/{http,anthropic_admin,cc_analytics,enterprise_analytics,openai_admin,replay}.py`; `tokenbill/finops/{reconcile,calibrate}.py`; tests + replay fixtures | 5 `Connector`s; `reconcile()`; `calibrate()` | WP0 (calibrate calls `whatif.engine.simulate` via §5.3; tests use a fake simulate) | ≈2.3k |
| **WP6** | Store | `tokenbill/store/*`; `tests/store/*` | `open_store`, `Store` (Reader+Writer), merge rules, cursors, `rebuild_lanes`, `rebuild_rollups` (`cluster_day`), retention/purge, audit log | WP0 | ≈2.0k |
| **WP7** | Attribution and privacy | `tokenbill/attribution/*` incl. `templates/`; `tests/attribution/*` | `load_rules`/`RuleSet.apply`, `classify_workload`, `publish`, `publish_findings`, coverage, `scan_secrets`, `render_dpia` | WP0 | ≈1.8k |
| **WP8a** | Cache and context detectors | `tokenbill/detect/{misses,cache_health,gateway,write_waste,fanout,context_tax,config_tax,tool_carry,cpt}.py`; `tests/detect/test_cache_*`, `test_ctx_*` | detectors CACHE-01..06, CTX-01..03, cpt calibration | WP0 | ≈2.2k |
| **WP8b** | Model, failure, automation, tail detectors | `tokenbill/detect/{model_switch,premiums,effort,truncation,failures,automation,runaway}.py`; `tests/detect/test_model_*`, `test_fail_*`, `test_auto_*`, `test_tail_*` | detectors MODEL-01..03, FAIL-01..03, AUTO-01..03, TAIL-01 | WP0 | ≈2.0k |
| **WP9** | What-if engines | `tokenbill/whatif/*`; `tokenbill/detect/{ttl_advisor,compaction_window,model_reprice}.py`; `tests/whatif/*`, `tests/detect/test_whatif_*` | `simulate_lane`, `simulate`, `project`, `shapley`, `prior`; detectors TTL-01, CTX-04, MODEL-04, MODEL-05 | WP0 | ≈2.2k |
| **WP10** | Policy pack | `tokenbill/policy/*`; `tests/policy/*` | `CATALOG`, `generate()`, `plan_rollout()`, hook/status-line scripts, LiteLLM config renderer | WP0 (calls `verify.guards.mde` via §5.3; tests use a fake) | ≈1.6k |
| **WP11** | FinOps outputs | `tokenbill/finops/{showback,focus,budgets,anomaly,forecast,kpis,alerts}.py`; `tests/finops/*` (excluding reconcile/calibrate tests) | `showback`, `focus_rows`/`write_focus_csv`, `check_budgets`, `detect_anomalies`, `forecast`, `recommend_caps`, alert sinks, KPIs | WP0 | ≈2.6k |
| **WP12** | Verification and receipts | `tokenbill/verify/*`; `tests/verify/*` | `build_panel`, `verify`, `mde`, guards, `sign_receipt`, `verify_receipt`, `ab` | WP0 | ≈2.0k |
| **WP13** | CLI, pipeline, renderers | `tokenbill/cli.py`, `tokenbill/pipeline.py`, `tokenbill/render/*`; `tests/render/*`, `tests/test_cli_v2.py` | every §2.9 command; `pipeline.ingest`, `pipeline.run_findings`, `pipeline.project`; terminal/JSON/HTML/CSV renderers with labels, CSP and a11y | WP0 (everything else via registry and §5.3; tests use fakes) | ≈2.8k |
| **WP14** | Content-trace hardening + recorder | `tokenbill/{trace,analyzer,simulator,breakers,instrument,pricing,report}.py`; `tests/test_*.py` (v0.1 files) + new `tests/legacy/*` | §3 F14 rules; recorder upgrade (beta, snapshot, full usage, attempts sidecar, background writer, metadata mode, diagnostics opt-in) | WP0 (metadata mode writes usage@1 via `core.records`) | ≈1.8k |
| **WP15** | Supply chain and hardening | `.github/workflows/*`, `.github/dependabot.yml`, `SECURITY.md`, `docs/VERIFY.md`, `docs/THREAT-MODEL.md`, `scripts/sbom.py`, `tests/fuzz/*`, `tests/test_no_egress.py`, `pyproject.toml` | CI matrix (Linux/macOS/Windows × 3.10/3.12/3.13), zizmor, CodeQL, Scorecard, SBOM, provenance, reproducible-build check, fuzzers, no-egress test | WP0 | ≈0.9k + YAML |
| **WP16** | CI cost gate | `tokenbill/ci/*`; `tests/ci/*`; `docs/ci-gate.md` | `run_check`, `to_sarif`, execution-file parser | WP0 (uses v0.1 `breakers.detect`, whose API is unchanged) | ≈1.0k |
| **WP17** | Fleet demo generator | `tokenbill/demo_fleet.py`; `tests/fleet/test_generator.py`; `tests/fleet/test_fleet_flagship.py` (authored now, gated to wave 2) | `generate_fleet()` → usage@1 + transcript sample + reference buckets + `FleetManifest` (plants and ground-truth $); `--experiment` and `--scale` worlds | WP0 | ≈1.6k |
| **WP-I** | Integration (wave 2) | `tokenbill/__init__.py`, `README.md`, `DESIGN.md`, `docs/SPEC.md`, `CHANGELOG.md`, `examples/fleet/*`, `tests/e2e/*` | wires everything; e2e tests; flagship v2 green; docs rewrite ("Related work" per §1.4); release notes | all | ≈1.2k + docs |

Total ≈ 38k LOC. The critical path is WP0 → (longest wave-1 WP ≈ 2.8k LOC, WP13) → WP-I.

### 5.3 Cross-package contracts (signatures wave-1 code may call)

#### 5.3.1 Result types in `core/results.py` (WP0)

```python
@dataclass(frozen=True)
class Coverage:
    requests: int; priced_share: float; unpriced_lines: int; sources: tuple[str, ...]
    lanes_exact_share: float; ttl_split_share: float; capabilities: frozenset[str]

@dataclass(frozen=True)
class ResidualLine:
    kind: str       # "priority_tier" | "code_execution" | "provisional" | "unobserved" | "discount" | "estimated" | "unexplained"
    dims: Mapping[str, str]; delta_nusd: int; reason: str

@dataclass(frozen=True)
class ReconcileResult:
    source: str; window: Window; grain: str; by: tuple[str, ...]; price_basis: PriceBasis
    ledger_nusd: int; reference_nusd: int; token_coverage: float
    residuals: tuple[ResidualLine, ...]; unexplained_pct: float; tolerance_pct: float; passed: bool
    implied_discount: Mapping[str, float]   # model → 1 − R/L_list

@dataclass(frozen=True)
class CalibrationResult:
    grain: str; nmbe: float; cvrmse: float; passed: bool
    components: Mapping[str, float]; scopes_passed: frozenset[str]

@dataclass(frozen=True)
class Alert:
    alert_id: str; kind: str; scope: Mapping[str, str]; ts: float; delta_nusd_per_day: int
    cause_component: str; top_finding_id: str | None; z: float | None; message: str; basis: Basis

@dataclass(frozen=True)
class PolicyBundle:
    target: str; files: Mapping[str, str]          # relative path → content
    levers: tuple[str, ...]; requires_eval: tuple[str, ...]; projections: tuple[Projection, ...]

@dataclass(frozen=True)
class RolloutPlan:
    lever_id: str; unit: str; waves: tuple[tuple[str, ...], ...]; holdback: tuple[str, ...]
    seed: int; washout_s: int; assignment_sha256: str; prereg_sha256: str
    mde_pct: float | None; verification_eligible: bool; warnings: tuple[str, ...]

@dataclass(frozen=True)
class VerifyResult:
    lever_id: str; label: Basis; estimate_nusd_per_unit: int; ci95_nusd: tuple[int, int]
    total_nusd: int; realization_rate: float | None; guards: Mapping[str, bool | float]
    receipt: Mapping[str, Any] | None; dsse: Mapping[str, str] | None

@dataclass(frozen=True)
class TeamShowback:
    team: str; window: Window; spend: AggRow; per_active_dev_day_nusd: int
    cache_read_share: float; automated_share: float; findings: tuple[Finding, ...]
    projections: tuple[Projection, ...]; unit_metrics: Mapping[str, int]; coverage_pct: float
```

`PolicySet` (§3 F8) and `LeverSpec` live in `core/records.py`:

```python
@dataclass(frozen=True)
class LeverSpec:
    lever_id: str; title: str; lever_class: LeverClass; tradeoff: bool
    policy_overrides: Mapping[str, Any]            # PolicySet field → value, for what-if
    settings: Mapping[str, Any]; env: Mapping[str, str]
    targets: tuple[str, ...]                       # "managed-settings" | "server-managed" | "litellm" | "hooks" | "ci" | "code"
    preconditions: Mapping[str, Any]               # {"models": [...], "channels": [...], "min_cc_version": "2.1.267"}
    evidence: tuple[str, ...]; verification: Mapping[str, str]   # unit, design, metric
```

#### 5.3.2 Functions

```python
# WP1  tokenbill.rates
def load_pricer(cards: Sequence[Path] = (), contract: Path | None = None, *,
                unknown_write_ttl: str = "policy") -> Pricer
def canonical_model(raw: str, *, provider: str | None = None) -> tuple[str, str | None]   # (model, channel hint)
def verify_card(*, today: date, online: bool = False) -> list[str]                          # issues; [] = ok
def emit_model_pricing(contract_path: Path) -> dict
def contract_from_reconcile(implied_discount: Mapping[str, float], *, effective_from: date) -> dict

# WP2  tokenbill.ingest.claude_code
def collect(projects: Path, out: Path | StoreWriter, *, since: float | None, user_id: str | None,
            team: str | None, ids: Pseudonymizer) -> ReadStats

# WP3  tokenbill.ingest.otlp_receiver
def serve(host: str, port: int, *, on_records: Callable[[list[Request | Event | UsageBucket]], None],
          max_body: int = 8 << 20, token: str | None = None) -> None

# WP5  tokenbill.finops.reconcile / calibrate
def reconcile(reader: StoreReader, *, against: str, window: Window, by: Sequence[str],
              price_basis: PriceBasis, tolerance_pct: float, closed_only: bool) -> ReconcileResult
def calibrate(reader: StoreReader, pricer: Pricer, window: Window, *, grain: str,
              simulate: Callable[..., Mapping[str, "LaneCost"]]) -> CalibrationResult

# WP6  tokenbill.store.db
def open_store(path: Path, *, create: bool = False, readonly: bool = False) -> "Store"   # Store: StoreReader & StoreWriter
def rebuild_lanes(store: "Store") -> int
def rebuild_rollups(store: "Store", window: Window | None = None) -> int     # refreshes cluster_day
def purge(store: "Store", *, user_pid: str | None = None, before: float | None = None, actor: str) -> int

# WP7  tokenbill.attribution
def load_rules(path: Path | None) -> "RuleSet"          # RuleSet.apply(req: Request) -> Request
def classify_workload(req: Request, session_stats: Mapping[str, float]) -> tuple[WorkloadClass, Confidence]
def publish(raw: RawAggregate, k: int) -> PublishedAggregate
def publish_findings(findings: Iterable[Finding], k: int) -> list[Finding]    # re-scopes / drops SELF
def scan_secrets(text: str) -> Counter[str]
def render_dpia(config: Mapping[str, Any], out_dir: Path) -> list[Path]

# WP9  tokenbill.whatif
def simulate_lane(lane: LaneView, policy: PolicySet, pricer: Pricer) -> LaneCost
def simulate(lanes: Iterable[LaneView], policies: Mapping[str, PolicySet], pricer: Pricer) -> dict[str, LaneCost]
def project(reader: StoreReader, pricer: Pricer, window: Window, levers: Sequence[LeverSpec], *,
            scope_by: Sequence[str] = ("team",), joint: bool = True) -> list[Projection]
def shapley(values: Mapping[frozenset[str], int]) -> dict[str, int]
def prior(lever_class: LeverClass) -> tuple[int, int, int]                        # milli-units

# WP10 tokenbill.policy
CATALOG: Mapping[str, LeverSpec]
def generate(projections: Sequence[Projection], *, target: str, current: Mapping[str, Any] | None = None,
             catalog: Mapping[str, LeverSpec] = CATALOG) -> PolicyBundle
def plan_rollout(units: Sequence[Mapping[str, Any]], *, lever_id: str, waves: int, holdback: float, seed: int,
                 projection: Projection | None, mde_fn: Callable[..., float] | None) -> RolloutPlan

# WP11 tokenbill.finops
def showback(reader: StoreReader, window: Window, *, k: int, findings: Sequence[Finding],
             projections: Sequence[Projection]) -> list[TeamShowback]
def focus_rows(published: PublishedAggregate, *, projections: Sequence[Projection], findings: Sequence[Finding],
               price_basis: PriceBasis, reconciled: ReconcileResult | None) -> Iterator[dict[str, str]]
def write_focus_csv(rows: Iterable[dict[str, str]], out: IO[str]) -> int
def check_budgets(reader: StoreReader, budgets: Sequence[Mapping[str, Any]], window: Window) -> list[Alert]
def detect_anomalies(reader: StoreReader, window: Window, *, config: Mapping[str, Any], k: int) -> list[Alert]
def forecast(reader: StoreReader, window: Window, *, horizon_days: int, projections: Sequence[Projection],
             seed: int = 7) -> Mapping[str, Any]
def recommend_caps(reader: StoreReader, window: Window, *, quantile: float = 0.95) -> list[Mapping[str, Any]]

# WP12 tokenbill.verify
def build_panel(reader: StoreReader, pricer_const: Pricer, window: Window, *, unit: str,
                assignment: Mapping[str, Mapping[str, str]]) -> "Panel"
def mde(panel_pre: "Panel", *, draws: int = 200, alpha: float = 0.05, power: float = 0.8, seed: int = 7) -> float
def verify(design: Mapping[str, Any], panel: "Panel", *, projection: Projection | None,
           recon: ReconcileResult | None, calib: CalibrationResult | None,
           boot: int = 2000, seed: int = 7) -> VerifyResult
def sign_receipt(receipt: Mapping[str, Any], key: Path) -> dict[str, str]
def verify_receipt(envelope: Mapping[str, str], allowed_signers: Path, identity: str) -> bool
def ab(baseline: Sequence[Request], candidate: Sequence[Request], outcomes: Mapping[str, bool], *,
       boot: int = 5000, seed: int = 7) -> Mapping[str, Any]

# WP16 tokenbill.ci
def run_check(inputs: Sequence[Path], *, baseline: Path | None, thresholds: Mapping[str, float],
              pricer: Pricer) -> Mapping[str, Any]
def to_sarif(result: Mapping[str, Any]) -> dict

# WP17 tokenbill.demo_fleet
def generate_fleet(seed: int = 7, devs: int = 60, days: int = 28, *, out_dir: Path,
                   experiment: bool = False, scale: int | None = None) -> Mapping[str, Any]   # manifest
```

Detector registry ids (strings in `core/registry.py`, WP0):

```python
BUILTIN_DETECTORS = {
  "cache.misses": "tokenbill.detect.misses:MissesByCause",          # CACHE-01 (+ CACHE-02 cold resume findings)
  "cache.health": "tokenbill.detect.cache_health:CacheHealth",
  "cache.disabled": "tokenbill.detect.gateway:CachingDisabled",
  "cache.unread_writes": "tokenbill.detect.write_waste:UnreadWrites",
  "cache.fanout": "tokenbill.detect.fanout:ColdFanout",
  "ctx.tax": "tokenbill.detect.context_tax:ContextTax",
  "ctx.config": "tokenbill.detect.config_tax:ConfigTax",
  "ctx.tool_carry": "tokenbill.detect.tool_carry:ToolCarry",
  "model.switch": "tokenbill.detect.model_switch:ModelSwitch",
  "model.premiums": "tokenbill.detect.premiums:Premiums",
  "model.effort": "tokenbill.detect.effort:EffortMix",
  "fail.truncation": "tokenbill.detect.truncation:Truncation",
  "fail.retries": "tokenbill.detect.failures:Retries",
  "fail.tool_loops": "tokenbill.detect.failures:ToolErrorLoops",
  "auto.mix": "tokenbill.detect.automation:WorkloadMix",
  "auto.ci_cache": "tokenbill.detect.automation:CiCrossRun",
  "auto.schedule": "tokenbill.detect.automation:ScheduledCadence",
  "tail.runaway": "tokenbill.detect.runaway:Runaway",
  "whatif.ttl": "tokenbill.detect.ttl_advisor:TtlAdvisor",
  "whatif.compaction": "tokenbill.detect.compaction_window:CompactionWindow",
  "whatif.upgrade": "tokenbill.detect.model_reprice:SameTierUpgrade",
  "whatif.delegation": "tokenbill.detect.model_reprice:DelegationRouting",
}
```

```python
BUILTIN_ADAPTERS = {
  "claude-code": "tokenbill.ingest.claude_code:ClaudeCodeTranscripts",      # WP2
  "otlp": "tokenbill.ingest.otlp:OtlpJson",                                 # WP3 (uses cc_otel, genai, conventions)
  "anthropic-responses": "tokenbill.ingest.anthropic_api:AnthropicResponses",  # WP4
  "openai": "tokenbill.ingest.openai:OpenAIUsage",
  "bedrock": "tokenbill.ingest.bedrock:BedrockUsage",
  "gemini": "tokenbill.ingest.gemini:GeminiUsage",
  "openrouter": "tokenbill.ingest.openrouter:OpenRouterUsage",
  "litellm": "tokenbill.ingest.litellm:LiteLLMSpendLogs",
  "usage": "tokenbill.ingest.usage_jsonl:UsageJsonl",
  "trace": "tokenbill.ingest.trace_v1:TraceV1",
}
BUILTIN_CONNECTORS = {                                                      # WP5
  "anthropic-usage": "tokenbill.connect.anthropic_admin:UsageReport",
  "anthropic-cost": "tokenbill.connect.anthropic_admin:CostReport",
  "cc-analytics": "tokenbill.connect.cc_analytics:ClaudeCodeAnalytics",
  "enterprise-analytics": "tokenbill.connect.enterprise_analytics:EnterpriseAnalytics",
  "openai-usage": "tokenbill.connect.openai_admin:Usage",
  "openai-costs": "tokenbill.connect.openai_admin:Costs",
}
BUILTIN_EXPORTERS = {
  "focus": "tokenbill.finops.focus:FocusExporter",                          # WP11
  "usage": "tokenbill.render.json_result:UsageExporter",                    # WP13
  "otlp-metrics": "tokenbill.render.json_result:OtlpMetricsExporter",       # WP13
  "csv": "tokenbill.render.csvout:CsvExporter",                             # WP13
}
```

### 5.4 Wave-2 integration checklist (WP-I)

1. Resolve all registry strings. `tokenbill findings --detector all` runs on the fleet demo.
2. Turn on `tests/fleet/test_fleet_flagship.py`, the content-canary e2e and the determinism e2e (two processes, byte-identical `result@2`).
3. Pricing parity test (legacy `pricing.py` vs `rates/data`).
4. Replace `core.testing` fakes with real implementations in e2e: real pricer, real store, real kanon.
5. Perf gates (nightly): 1M-request ingest/findings/RSS; 200k-line transcript collect.
6. Docs: README (new positioning, "Related work" rewrite, fleet quickstart: `init` → `collect` → `ingest` → `reconcile` → `findings` → `policy generate` → `plan-rollout` → `verify`), DESIGN.md (labels, ledger, what-if, verification, threats to validity), docs/SPEC.md (points at SPEC-0.2-interfaces), CHANGELOG 0.2.0.
7. A lab-checklist self-assessment table (Appendix A) with evidence links to tests.

---

## 6. Deferred items, risks and mitigations

### 6.1 Explicitly deferred (with reasons)

| Item | Why deferred | Evidence |
|---|---|---|
| Block-level policy-search simulator for content traces (4 breakpoints, 20-block lookback, mixed TTL, workspace-shared replay) | L effort. For fleets, the usage-level detectors and replay (F7/F8) capture most dollars content-free. v0.2 fixes the v0.1 false positives instead. Target v0.3 | `[cb-shared-prefix-sim]` `[cb-lookback]` `[anth-lookback-20]` `[cb-cross-run-cache]` |
| Recording HTTP proxy / gateway mode | Puts Token Bill in the inference path and grows the security-review surface. Gateways are consolidating into security products. Ingest their logs instead | `[ent-supply-chain-incidents]` `[portkey-panw-gateway-security]` `[cc-gateway-attribution-caps]` |
| Shared server / dashboard with SSO, RBAC, SCIM | Static, per-team showback behind the enterprise SSO proxy suffices for phase 1 | `[ent-server-sso-rbac]` |
| `count_tokens`-based exact attribution | Network plus API keys. v0.2 calibrates chars/token from billed deltas (cpt) instead | `[count-tokens-exact-attribution]` `[exact-token-counting]` |
| Raw-body OTel ingestion (`OTEL_LOG_RAW_API_BODIES`) | Contains the full conversation (privacy). Documented as an opt-in path for a later content-local mode | `[cc-enterprise-telemetry-gap]` |
| Commitment and capacity sizing, seat optimizer, contract ledger, renewal calendar, marketplace drawdown | Contract-dependent inputs Token Bill can't observe. v0.2 ships effective-discount discovery, channel premiums and the lifecycle calendar only | `[cc-capacity-parity]` `[cc-sizing-newsvendor]` `[cc-seat-vs-usage]` `[cc-marketplace-drawdown]` `[cc-renewal-traps]` |
| Codex, Gemini CLI, Copilot, Cursor adapters | Formats evolve, and some fields are unverified from primary sources. OTel GenAI covers part of Gemini CLI. Target v0.3 | `[codex-cli-telemetry]` `[gemini-cli-telemetry]` `[copilot-ai-credits]` `[cursor-admin-api]` `[cc-cursor-token-rate]` |
| Live effort sweeps (`tokenbill sweep`), eval sizing (IRT), judge-cost optimizer | Need live API spend and eval harnesses. v0.2 offers `ab` over recorded runs | `[anth-effort-sweep]` `[eval-subset-irt]` `[eval-judge-choice]` `[eval-statistics]` |
| Compression transform library and counterfactual history rewriting on content traces | Needs content, and trajectory effects dominate. `ab` + `verify` judge third-party compressors (Headroom, RTK) instead | `[comp-cache-breakeven]` `[headroom-compression-cache-aware]` `[token-not-cost]` |
| Router audit and cascade simulators | Need outcome labels and content. Routing forfeits caches, and many routers fail simple baselines | `[router-baselines-knn]` `[cache-aware-routing]` `[frugalgpt-cascade]` |
| Cost per quality-adjusted merged PR, throughput alarm | Needs git/CI/incident joins not available content-free in v0.2. Activity ratios are shown labelled as gameable | `[value-adjusted-cost-metric]` `[quality-guardrail]` `[units-gaming]` |
| Anytime-valid confidence sequences, synthetic control / ITS | v0.2 uses pre-registered looks and labels org-wide changes `measured`. Target v0.3 | `[sequential-monitoring]` `[its-rdit-org-wide]` |
| Embeddings ledger and hygiene | Small share of spend. P2 | `[emb-scope-decision]` |
| Semantic or plan caching recommendations | Correctness and security risk; rarely fits coding agents | `[semantic-cache-pitfalls]` `[agentic-plan-caching]` |
| Hidden-token audit signals | Published audits can be evaded. Signals only, low priority | `[audit-evasion-limits]` `[hidden-token-audit-research]` |
| Any LLM-powered explanation feature | Would make the cost tool a cost center and require content | `[observer-compiled-views]` |
| Parquet/DuckDB "scale" extra | Would add an optional dependency. SQLite is adequate to ~10⁸ rows, with CSV/JSONL/FOCUS for lakes | `[ent-scale-storage]` |
| Image/multilingual lenses | Need content dimensions or text. v0.3 on content traces | `[anth-vision-tokens]` `[multilingual-tokenization]` |
| Tokenomics Foundation / FOCUS WG submissions | A process, not code. Prepare the breaker taxonomy and `x_` columns as an open spec after v0.2 | `[ent-market-tokenomics]` |

### 6.2 Risks and mitigations

| # | Risk | Mitigation in this design |
|---|---|---|
| R1 | **Claude Code transcript format drift** (internal, weekly releases) silently breaks the importer | Per-version field fingerprints and `DATA_QUALITY` events; golden fixtures per observed version; reconciliation coverage % catches silent loss; OTel as the second central source; the collector warns before `cleanupPeriodDays` deletes data |
| R2 | **Price drift** (three Anthropic changes in one quarter) makes numbers stale | Effective-dated rows with `verified_on`; `pricing verify --online` in scheduled CI; stale-row warnings on reports; published benchmarks in a dated `benchmarks.json` `[anth-evidence-drift]` |
| R3 | **Overclaimed savings** (projections optimistic; token ≠ bill) | Labels; RR priors; Shapley-deflated headline; calibration gate before "calibrated"; `verify` before "verified"; realization-rate backtest updates priors `[projection-optimism]` `[llm-token-not-bill]` |
| R4 | **Privacy and labor law** (works councils, GDPR Art. 88, EU AI Act Annex III) block rollout | Content-free by default; HMAC identities; k ≥ 5 enforced by type; no individual ranking; self-view only; DPIA/works-council pack; retention and purge; audited break-glass `[labor-law-constraints]` |
| R5 | **Supply-chain compromise** of the tool itself | Zero runtime deps; SHA-pinned actions; `permissions: {}`; Trusted Publishing + PEP 740; SBOM; provenance; reproducible builds; no network by default; no-egress test `[ent-supply-chain-incidents]` |
| R6 | **Scale** (millions of calls) exhausts memory or time | Streaming readers, cursors, SQLite WAL with batched upserts, per-lane processing; perf gates at 1M requests `[cb-scale-memory]` |
| R7 | **Wrong advice** (e.g. compaction flagged as history rewrite; fixes unavailable on the model) | Cause taxonomy aligned to documented invalidators and diagnostics labels; expected rebuilds labelled; fix text gated by the model × platform matrix; `tradeoff=True` levers require evals `[cb-breaker-taxonomy]` `[anth-cache-preserving-apis]` |
| R8 | **Connector API drift / unverifiable offline** | Record/replay fixtures; connectors optional; field names flagged R in §4.6; reconcile works from files too (`--against file`) |
| R9 | **OTel lacks TTL split and exact lanes** → estimated dollars | `write_ttl_basis=UNKNOWN` priced by policy with an explicit uncertainty; capability-gated detectors; recommend the transcript collector for TTL-sensitive levers `[cc-enterprise-telemetry-gap]` |
| R10 | **Parallel-build integration failure** | Frozen WP0 contracts, string registries, `core.testing` fakes, wave-2 e2e plus flagship v2; interface doc in `docs/SPEC-0.2-interfaces.md` |
| R11 | **HMAC key exposure** enables dictionary re-identification | `hmac-at-ingest` default (key only on the central host); key file `0600`; rotation events; key id fingerprint only in `meta` |
| R12 | **Small fleets can't verify small levers** (MDE ~13.6% at 1,000 devs) | MDE check refuses the "verified" label and states the fleet size or window needed `[heavy-tails-power]` |
| R13 | **Commoditization** (Anthropic ships more first-party analysis) | Position on cross-source reconciliation, policy what-if, verification and FOCUS/OTel export, not per-session views; ingest first-party diagnostics as ground truth instead of competing `[cc-first-party-visibility]` |
| R14 | **Single-user corpus bias** in impact estimates | Every corpus-derived % is labelled "one heavy user, upper bound". Projections come from the org's own replay, not corpus percentages `[cc-heavy-tail-concentration]` |
| R15 | **Goodhart and backlash** from cost programmes (leaderboards, mandates) | No spend rankings; nudges end in one-click config; frequency caps documented in the hook templates; team-level guardrails `[leaderboard-goodhart]` `[mandates-gaming]` `[nudge-decay-durability]` |

---

## Appendix A. Enterprise lab checklist → where v0.2 answers it

| Lab test `[ent-*]` | v0.2 answer | Tests |
|---|---|---|
| 1 Provenance/SBOM/reproducible build | WP15 release job, `docs/VERIFY.md` | release workflow + rebuild-diff job |
| 2 Scorecard/OSPS | pinned actions, permissions, CodeQL, Dependabot, Scorecard (second maintainer: governance, outside code) | CI |
| 3 Network egress | no network by default; opt-in commands only | `tests/test_no_egress.py` |
| 4 DPIA / data minimization | content-free tiers, HMAC, retention, purge, DPIA pack | F2 canary, F5 tests |
| 5 Secrets | never stored; scanner counts only | F5 test 5, canary |
| 6 Hostile input | fuzzers, CSP, ANSI stripping, line caps, quarantine | `tests/fuzz/*`, F3 test 6 |
| 7 Accuracy/reconciliation ≤ 1% | F1 + F6 | F6 tests 2–3, flagship v2 |
| 8 Recommendation validity | diagnostics confusion matrix; `verify` receipts | F7 test 4, F12 tests |
| 9 Scale ≥ 10M calls | streaming store; 1M gate in CI, 10M nightly (extrapolated budget) | F3 test 10 |
| 10 Allocation coverage ≥ 95% | F5 coverage KPI | F5 test 1 |
| 11 Standards interop | FOCUS 1.4 + `x_`; OTel GenAI in and out | F9 tests, F4 tests |
| 12 Operability | exit codes, JSON logs, config precedence, runbook in README | `tests/test_cli_v2.py` |
| 13 Accessibility | tables behind charts, contrast, no color-only meaning | render tests |
| 14 Governance | CVD SLA (24h/72h), semver + schema versioning (`usage@1`, `result@2`, `ratecard@1`) | docs |
| 15 Identity (server) | not applicable in v0.2 (no server; static artifacts) | — |
