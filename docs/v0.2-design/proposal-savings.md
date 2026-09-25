# Token Bill v0.2: design proposal, savings lens

- **Date:** 2026-09-23
- **Status:** proposal for parallel build, then adversarial review
- **Lens:** the product is the dollars it removes from the bill. When two designs tie, pick the one that removes more *verifiable* dollars sooner.
- **Evidence base:** 467 adversarially verified findings across 18 research tracks. Finding ids appear in square brackets, e.g. [cc-compaction-threshold-sim]. Where a finding was corrected, this document uses the corrected numbers.
- **Repo baseline:** Token Bill v0.1.2. It is about 4.6k LOC of stdlib Python (trace@1 JSONL, a char-heuristic analyzer, a single-breakpoint 5-minute cache simulator, 5 breaker kinds, an HTML/terminal report, and an SDK recorder).

How to read this document:
- §1 argues what to build.
- §2 is the architecture contract. It contains the canonical records builders code against.
- §3 is the ordered feature list, with algorithms and acceptance tests.
- §4 lists every external fact the build depends on, with sources.
- §5 splits the work into packages for parallel builders.
- §6 lists what is deferred and why, plus the risks.

---

## 0. Executive summary

On the one real corpus the research could measure ($11,632.75 of Claude Code spend, 43,383 deduplicated API calls; [cc-context-size-driver]), the bill is not driven by what most tools watch:
- The cache hit ratio was **97%**.
- Yet cache **reads** were 55% of spend. The main thread's median prompt was 445k tokens, and calls at ≥400k context were 18% of calls but 46% of dollars.

The largest measured levers are all *policy* choices, and every one can be set in Claude Code managed settings:

| Lever | Corpus evidence (one heavy user, price-only upper bounds) |
|---|---|
| Compact earlier | −18% to −30% of the total bill [cc-compaction-threshold-sim] |
| Handle cold resumes | 7.95% of the bill directly [cc-cold-resume] |
| Pick the right cache TTL | ~12% of main-thread spend for 5m-default fleets [cc-ttl-advisor] |
| Route delegated agents and same-tier models | 12–31% of the bill [cc-delegation-model-routing], [cc-same-tier-upgrade] |
| Change fleet defaults (external precedent) | Datadog saved $975k/month through model and effort defaults [org-defaults-datadog] |

v0.2 therefore turns Token Bill into a **counterfactual policy engine with receipts**:
1. **Ingest** what enterprises already have, content-free (Claude Code transcripts, Claude Code OTel, Anthropic Admin/Analytics exports, OTel GenAI, trace@1/@2). Price it exactly (5m/1h writes, iterations, modifiers, contract rates).
2. **Replay** every lane under candidate policies with a cache-exact engine, calibrated against the bill.
3. **Search** the policy space. Deflate overlapping levers with Shapley and realization-rate priors, and **emit** deployable managed-settings, LiteLLM and hook patches with projected dollars.
4. **Verify** each rollout causally. The toolkit is paired A/B with bootstrap CIs, randomized stepped wedges with Callaway–Sant'Anna/imputation DiD at constant prices plus CUPED, SRM and placebo guards, and signed receipts.

Every dollar figure carries one of four labels: **exact**, **estimated**, **measured** or **verified**.

The build is **15 work packages** with disjoint files. Two foundation packages (WP0a contracts, WP0b test harness with a reference replay oracle) freeze the records and interfaces first. Twelve packages then build in parallel against them, and one integration package (WP10) merges last.

---

## 1. Thesis and positioning

### 1.1 Thesis

Enterprise LLM bills are dominated by re-read context and by fleet defaults, not by individual prompts:
- cache reads and writes are 80–87% of reconstructed agent cost [token-not-cost], [token-reduction-not-cost];
- context size and idle resumes dominate a real Claude Code bill even at a 97% hit ratio [cc-context-size-driver];
- the largest published enterprise saving came from changing defaults [org-defaults-datadog].

Removing dollars at fleet scale therefore needs four things:
- a cache-exact counterfactual of how each **policy** would have billed on the org's own traffic;
- a deployable config for that policy;
- a causal measurement proving the dollars came off the invoice;
- honesty about uncertainty.

Nothing else on the market does all four [verified-savings-gap-rtk], [datadog-agent-console-fix-library], [competitive-landscape]. Token Bill v0.2 is that engine: local-first, zero-dependency, metadata-only by default, with every number labeled exact / estimated / measured / verified.

### 1.2 Where the dollars are (ranked by the research)

| # | Lever | Mechanism | Strongest evidence | Lever class |
|---|---|---|---|---|
| 1 | **Context-size governor** | Lower `autoCompactWindow` from its ~967K default | Replay at 400k: −53% of main thread, −29.8% of the whole bill (price-only upper bound) [cc-compaction-threshold-sim]. Calls ≥400k: 46% of dollars [cc-context-size-driver]. Uber set 400K org-wide [uber-policy-arc] | trajectory |
| 2 | **Model / effort routing, priced per solved task** | Delegated agents inherit Opus/Fable; same-tier upgrades reprice cache reads; effort defaults | Workflow agents → Sonnet 5 saves 25.5% of the bill, subagents 4.4% [cc-delegation-model-routing]. Fable 5→5.1 saves 21.0%, Opus 5→5.5 11.7% on identical tokens [cc-same-tier-upgrade]. Datadog: $687k/mo (model default) plus $288k/mo (effort) [org-defaults-datadog]. Opus 5 medium: −50% cost for about −2 pts [anth-effort-sweep] | trajectory (needs eval) |
| 3 | **Cold-resume advisor** | A post-TTL resume of a huge context re-writes it at the write rate | 128 events at a median of 568k tokens: 7.95% of the bill; summarize-on-cold-resume replay −29% of total (overlaps #1) [cc-cold-resume] | trajectory / behavioral |
| 4 | **TTL advisor (5m / 1h / keep-alive)** | Match TTL to each lane's gap distribution | API-key and cloud fleets default to 5m and would pay +11.8% on the main thread here [cc-ttl-advisor]. Anthropic measured 11–15% either way [anth-fleet-benchmarks]. Keep-alive break-even I_max = τ(w/r−1) [keepalive-economics] | cache transform |
| 5 | **Cache breakers, expanded from 5 kinds to about 30** (C01–C27, prompt/param/infra/gateway/framework causes) | Gateway strips `cache_control`; tool search disabled; serialization churn; lookback overflow; fan-out; param churn | A stripping gateway turns 0.1× reads into 1× on the whole history, i.e. up to about 90% of affected input [cc-gateway-cache-strip]. One sort fix cut writes 56,296 → 32 tokens [nondeterministic-tools]. Within-TTL prefix changes were 33% of miss premium, about 5% of the bill [cc-miss-taxonomy-ground-truth]. ProjectDiscovery: 59–70% of spend vs no-cache [projectdiscovery-cache-case] | cache transform |
| 6 | **Automation, CI and eval** | Batch at 0.5×; cross-run cache; trigger policy; cadence vs TTL; fan-out stagger | Batch 50% of eligible traffic [anth-batch-stacking]. Shared stable prefix −54% [ci-cross-run-cache]. Review cost multiplies with pushes [ci-trigger-multiplier]. Up to ~12× per scheduled fire when a TTL miss becomes a hit [sched-cadence-ttl] | rate / cache transform |
| 7 | **Carry cost and configuration tax** | Tool output and always-on context re-read every turn | Bash+Read carry ~10–12% of the bill [cc-tool-output-carry]. Skill/MCP/CLAUDE.md config tax ~3–7% [cc-config-sprawl-tax]. AgentDiet −21–36% net [agentdiet] | trajectory |
| 8 | **Premium drift** | Fast mode 2×, US geo 1.1×, regional +10%, sticky `/effort` and fast-mode escalations | 50% of fast-mode spend and about 9% of geo-pinned spend recoverable where not needed [anth-modifiers-geo-fast-priority], [cc-sticky-escalation] | rate |
| 9 | **Failure-path waste** | Cold retries after backoff > TTL; retry storms; fallback cache loss | Cold retry costs 4–6× the successful call [fp-ttl-from-request-start]. Local share: 0.7% direct, 3.1% upper bound; higher behind gateways and with 5m TTLs [fp-local-failure-share] | cache transform |
| 10 | **Compression economics** | r* and K* guardrails stop negative-ROI "optimizations" | Compressors raised billed cost by +6.8% and +48.4% [token-not-cost]. RTK was +7.6% per task in a paired A/B [verified-savings-gap-rtk]. Closed forms: r* = N/(α+β(N−1)), K* = S(α−β)/(Xβ) [comp-cache-breakeven], [history-rewrite-kstar] | decision support |

Two cautions travel with every number above:
- The corpus is **one heavy user** at $215 per active day, against Anthropic's fleet average of about $13 [cc-heavy-tail-concentration], [cc-enterprise-baseline]. The percentages show mechanisms, not fleet averages.
- Trajectory-changing levers are price-only **ceilings** until verified [llm-token-not-bill], [projection-optimism]. That is why verification (§3 F9) is a first-class feature, not an afterthought.

### 1.3 Positioning

| Alternative | What it does well | What it does not do | Token Bill v0.2's answer |
|---|---|---|---|
| **Anthropic first-party:** Claude Code `/usage` (v2.1.251+), status line, `/insights` | Per-session cache hit share, misses with a "likely cause", attribution to skills/subagents/MCP [cc-first-party-visibility], [cc-usage-likely-cause] | One developer, one machine, main thread only, list price. No counterfactual, no fleet policy, no verification | Use the same signals across thousands of developers. Rank by recoverable dollars. Emit the org policy patch. Verify it. The v0.1 README claim that provider tools are "silent on why" is outdated and must be removed [cc-usage-likely-cause] |
| **Anthropic cache-diagnostics beta** | Server-side `cache_miss_reason`, content-free [anth-cache-diagnostics-beta] | 1P API only, first divergence only, `cache_missed_input_tokens` is not a billing figure; `unavailable` covers config changes; no dollars | Ingest it as **ground truth labels**, publish the confusion matrix of Token Bill's breakers against it, price it in dollars, and cover Bedrock/Vertex/Foundry plus TTL-expiry vs change [cc-miss-taxonomy-ground-truth] |
| **Anthropic Admin Usage/Cost, Claude Code Analytics, Enterprise Analytics APIs** | Billed ground truth: 1-minute buckets, 5m/1h split, `amount` vs `list_amount` [anth-admin-usage-cost-api], [cc-admin-analytics-apis] | Descriptive only; no root cause, no counterfactual | Reconciliation target (`tokenbill reconcile`), the calibration gate, outcome units (PRs, commits) for verification |
| **Datadog Agent Console** (Preview) | Closest competitor: cost by agent/team/user, retry-loop and re-read detection, a Fix Library of hooks [datadog-agent-console-fix-library] | SaaS, not local-first. No cache-exact replay, no Shapley-deflated policy search, no causal receipts. Its own $1M/month program has no control group for nudges [datadog-1m-month-case] | Open, zero-dependency, runs inside the enterprise boundary. Deeper cache economics (TTL, lookback, fan-out, gateway) and verified receipts. **Exports into** Datadog/Vantage/CloudZero via FOCUS and OTLP rather than competing [finops-platforms-export-target] |
| **ccusage** and local trackers | Careful dedup across 18 agent CLIs, daily/session/5-hour blocks [ccusage-local-parity] | "What", not "why"; no fleet, no fix, no dollars-recoverable | Same local files, plus causes, 5m/1h-correct pricing, counterfactuals and fixes. Offer ccusage-compatible JSON for adoption |
| **LiteLLM / Langfuse / Helicone / Portkey / LangSmith / Phoenix** | Gateways and trace stores; spend by key, tag or team; cost alerts [litellm-spend-and-supply-chain], [langfuse-clickhouse], [obs-platforms-no-cache-rca] | Cost as an attribute; no cache counterfactual or fix pricing. Framework telemetry frequently miscounts cache tokens [fw-telemetry-conventions]. LiteLLM was backdoored on PyPI in March 2026 [ent-supply-chain-incidents] | Treat them as **sources** (OTel GenAI/OpenInference import with a convention registry). Stay zero-dependency; emit LiteLLM `cache_control_injection_points` configs as a fix [litellm-gateway-injection] |
| **Compression and "token saver" tools** (RTK, Headroom, token-optimizer) | Large token reductions on bash, JSON and log output [headroom-compression-cache-aware], [oss-token-minimizer-ecosystem] | Self-reported token savings can have the wrong sign on the bill (RTK: 60–90% claimed, +7.6% measured) [verified-savings-gap-rtk], [token-not-cost] | Be the **neutral referee**: `tokenbill ab` with billed, success-adjusted, paired CIs; r*/K* guardrails |
| **Coding-agent dashboards** (CloudWatch Coding Agent Insights, Dash0, Grafana) | Commodity attribution tiles [coding-agent-dashboards-commodity] | A hit rate alone is worth nothing now [coding-agent-dashboards-commodity] | Every output names the breaker, the dollars and the fix, or it is not shown |
| **promptcachelint** | Zero-dependency prefix-diff linter, CI exit codes [promptcachelint-overlap] | No dollars, fleet view or verification | `tokenbill check` CI gate plus fleet dollars plus receipts |
| **Enforcement** (Claude apps gateway caps, Cloudflare spend limits, GitHub/Cursor budgets) | Hard caps, now free or bundled [cloudflare-free-spend-limits], [enforcement-primitives] | Blunt caps cut valuable use along with waste [blunt-caps-collateral] | Don't rebuild enforcement. Emit cap recommendations and a collateral-damage estimate; configure existing primitives |

### 1.4 Product principles (binding on every work package)

1. **Dollars, not tokens.** Every recommendation is priced in billed dollars, net of cache effects. Token reductions are never shown as savings [token-not-cost], [llm-token-not-bill].
2. **Difference of replays.** A projected saving is `replay(as_is) − replay(policy)`. Both arms run through the same engine, so model error cancels. The replay of the as-is policy must reconcile to the bill (the calibration gate, [ashrae-calibration-gate]) before a projection loses its "uncalibrated" tag.
3. **Four labels on every dollar.** `exact` / `estimated` / `measured` / `verified`, as defined in [signed-receipts] and [mv-adjusted-baseline]. No estimate is ever presented as billed. List, contract and invoice bases are shown separately [cc-list-price-estimates].
4. **Never sum standalone ceilings.** Overlapping levers are deflated with Shapley over joint replays [shapley-attribution]. Projections are published as replay × a realization-rate interval [realization-rate-backtest].
5. **Defaults over nudges.** Rank fixes by mechanism: default/config > budget/price > targeted private comparison > education [defaults-beat-nudges-meta], [nudge-decay-durability]. Every nudge ends in a one-click config change.
6. **Free wins vs trade-offs.** A lever that can change model behavior (model, effort, compaction, output caps) is flagged `requires_eval` and is never auto-applied [anth-effort-rerun-budgets], [eval-statistics].
7. **Metadata-only by default; never rank individuals.** Views are team-level with k ≥ 5 and complementary suppression; individuals see only their own data. There is no emotion or sentiment inference and no leaderboards [leaderboard-goodhart], [aggregation-k5], [labor-law-constraints].
8. **Local-first, zero runtime dependencies, no telemetry.** The only network paths are opt-in fetchers run by the user with their own credentials. They are built against recorded fixtures [ent-supply-chain-incidents].
9. **Ground truth over heuristics.** Prefer provider-reported fields (diagnostics, iterations, TTL split, xAI ticks, OpenRouter cost) over inference, and report agreement where both exist [cb-cache-diagnostics].

### 1.5 Non-goals for v0.2

- Not a gateway or proxy in the inference path. The recording proxy is deferred (§6) [portkey-panw-gateway-security], [ent-supply-chain-incidents].
- Not an enforcement system (caps, blocking) [enforcement-primitives].
- Not a compressor. Token Bill measures and verifies compressors; it does not ship one [headroom-compression-cache-aware].
- Not a hosted multi-tenant dashboard: no SSO or RBAC server. Static per-team showback files suffice [ent-server-sso-rbac].
- No semantic caching recommendations for coding agents [semantic-cache-pitfalls].

---
## 2. Architecture

### 2.1 Data flow

```text
 SOURCES (read-only, local files; optional opt-in fetchers)
  Claude Code transcripts ─┐   Claude Code OTel (OTLP JSON) ─┐   OTel GenAI / OpenInference spans ─┐
  trace@1 / trace@2 ───────┤   Anthropic Admin usage/cost ────┤   CC Analytics / Enterprise Analytics┤
  SDK recorder v2 ─────────┘   OpenAI usage, LiteLLM logs ────┘   outcomes.jsonl (tests/PRs) ────────┘
            │  ingest/*  (adapters: normalize to DISJOINT token buckets, dedupe, HMAC-pseudonymize,
            ▼            strip content; lanes reconstruction)
 CANONICAL RECORDS (core/records.py): Session ▸ Lane ▸ Request ▸ Attempt ▸ UsageRecord (+ Block hashes)
            │                         UsageBucket / InvoiceLine / ActivityDay (aggregates)
            ▼
 LEDGER (ledger/store.py: SQLite, 0600)  ──►  rates/ (RateCard: exact nano-USD, list|contract)
            │                                        │
            ▼                                        ▼
 REPLAY ENGINES  sim/usage_replay.py (Mode U: usage-level, all sources)
                 sim/block_replay.py (Mode B: block-level cache rules, when shapes exist)
            │   replay(as_is) vs billed ──► CALIBRATION GATE (NMBE / CV(RMSE))
            ▼
 DETECTORS  detect/* (registry; each finding priced as a single-lever replay difference)
            ▼
 OPTIMIZER  optimize/* (lever catalog → joint replays → Shapley credit → RR intervals → per-cohort
            PolicySpec) ──► EMITTERS (managed-settings.json, env block, LiteLLM config, hooks)
            ▼
 OUTPUTS    out/* : scan@1 JSON, single-file HTML, terminal, SARIF; ledger/*: FOCUS CSV, OTLP JSON
            ▼
 VERIFICATION LOOP  verify/*: ab (lab), plan-rollout → (org deploys patch by MDM wave) → ingest →
            verify (CS / imputation DiD + CUPED, guards) → signed receipt → RR database ──┐
            ▲────────────────────── realization-rate priors feed back into OPTIMIZER ◄──┘
```

### 2.2 Evidence levels (what works with what data)

Every Session carries `evidence_level`. Each feature declares the minimum level it needs. **All of the top-4 levers run at the `usage` level, which is content-free.**

| Level | What is present | Typical sources | Features enabled |
|---|---|---|---|
| `aggregate` | Time-bucketed usage/cost by dimension; no per-request order | Admin usage/cost reports, Analytics APIs | Ledger, reconcile, ESR, premium drift (speed / geo / tier), cache-read-share benchmark, batch share, calibration target |
| `usage` | Per-attempt disjoint usage, timestamps, model, params, lane membership, events, tool-result *sizes* | CC transcripts (metadata mode), CC OTel `api_request`, OTel GenAI spans, recorder v2 | **Mode U replay**: context governor, cold resume, TTL, routing / repricing, fast / geo / sticky, fan-out, failure path, CI / scheduled, usage-signature breakers, carry cost (by tool), config tax |
| `blocks` | + `PromptShape`: per-block HMAC hashes (wire, sorted, volatile-masked), sizes, marker positions | Recorder v2, trace@2, trace@1 (hashed on import), CC `prompt_snapshot` attachments | **Mode B replay**: block breakers (volatile-system, tool churn, serialization churn, history rewrite, lookback overflow, breakpoint placement, write-never-read exact), exact carry per block |
| `content` | Raw text | trace@1 files, v0.1 `analyze` | Local-only evidence excerpts (v0.1 report). **Never** written to the ledger or fleet outputs |

### 2.3 Package layout and file ownership

Existing v0.1 modules stay in place so that `demo`, `analyze` and trace@1 keep working byte-for-byte. New code lives in subpackages. **WP0a creates every `__init__.py` listed below as an empty file**, so later packages only add modules and never touch a shared file.

```text
tokenbill/
  __init__.py            (WP10: version bump to 0.2.0 only)
  __main__.py            (unchanged)
  common.py              (unchanged; TokenbillError/TraceError, rng, canonical_json)
  trace.py, analyzer.py, simulator.py, breakers.py, report.py, demo_traces.py   (v0.1, unchanged)
  pricing.py             (WP1: v0.1 API kept; rows added; delegates to rates/ for new models)
  instrument.py          (WP3a: v0.1 Recorder kept; RecorderV2 added)
  cli.py                 (WP10: existing verbs kept; new verbs wired)
  scan.py                (WP10: the pipeline orchestrator used by scan/optimize/check)
  demo_fleet.py          (WP10: synthetic-fleet demo with planted levers)
  privacy_pack.py        (WP11)
  core/        (WP0a) __init__.py records.py money.py labels.py ids.py interfaces.py registry.py
                      jsonio.py config.py text.py mathx.py
               (WP0b) shapes.py synth.py testing.py   (prompt shapes, synthetic fleet, FlatRates, ReferenceReplay oracle)
  rates/       (WP1)  catalog.py resolve.py modifiers.py contract.py billing_rules.py
                      evidence.py verify.py modelpricing.py data/{anthropic_1p,bedrock,vertex,
                      foundry,claude_platform_aws,openai,azure_openai,gemini,xai,deepseek,mistral,
                      evidence,billing_rules}.json
  ingest/      (WP2)  claude_code.py claude_code_otel.py
               (WP3a) trace_v1.py trace_v2.py lanes.py
               (WP3b) providers.py otel_genai.py anthropic_admin.py openai_admin.py litellm_logs.py
                      outcomes.py conventions.py fetch.py
               (WP0a) __init__.py (first-party module list; see note below)
  sim/         (WP4a) usage_replay.py policies.py calibrate.py
               (WP5)  block_replay.py cachemodels.py tokens.py
  optimize/    (WP4b) levers.py search.py shapley_run.py realization.py emit.py cohorts.py
                      templates/tokenbill_session_start.py (package-data hook template)
  detect/      (WP6)  context.py routing.py carry.py compression.py
               (WP7)  breakers_usage.py failure.py automation.py frameworks.py
               (WP5)  breakers_blocks.py
  verify/      (WP8)  stats.py ab.py did.py cuped.py sequential.py rollout.py rr_db.py receipts.py
  ledger/      (WP9)  store.py reconcile.py aggregate.py focus.py otlp_out.py esr.py
  out/         (WP10) schema.py terminal.py html.py sarif.py
tests/v2/<package>/test_*.py           owned by the WP that owns the package
tests/fixtures/<wp>/...                owned per WP (golden files)
docs/, .github/, SECURITY.md, README.md, DESIGN.md, CHANGELOG.md, Makefile   (WP11)
pyproject.toml                         (WP0a only: pytest --import-mode=importlib, dev deps)
```

**Registration without shared-file edits.**
- WP0a writes `ingest/__init__.py` and `detect/__init__.py` with the **fixed list of first-party module names** from this layout.
- Both call `core.registry.autoload(__name__, names)`. That imports each module and skips *only* `ModuleNotFoundError` for a listed name, so a branch that lacks a sibling package's module still imports cleanly.
- Adding a new first-party module after WP0a is a one-line change to that list, owned by WP0a.

### 2.4 Canonical records (`tokenbill/core/records.py`, WP0a; frozen contract)

These dataclasses are the contract between all packages. Change control: after WP0a merges, a field may be **added** with a default by any WP through a one-line PR to WP0a's file, reviewed by the integrator. Fields are never renamed or removed in v0.2.

```python
"""Canonical, content-free records. Units: tokens are ints of the SERVED model's tokenizer;
timestamps are float unix seconds (UTC); money is int nano-USD (1 USD = 10**9)."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal, Mapping

Nano = int                                            # integer nano-USD
Label = Literal["exact", "estimated", "measured", "verified"]
Basis = Literal["list", "contract", "invoice"]
Provider = Literal["anthropic", "openai", "google", "xai", "deepseek", "mistral", "other"]
Platform = Literal["anthropic_api", "claude_platform_aws", "bedrock", "vertex", "foundry",
                   "openai_api", "azure_openai", "gemini_api", "openrouter", "unknown"]
EndpointScope = Literal["global", "regional", "multi_region", "unknown"]
ServiceTier = Literal["standard", "batch", "flex", "priority", "fast", "reserved", "unknown"]
Speed = Literal["standard", "fast"]
UsageSource = Literal["final", "message_start_only", "estimated", "aggregate"]
EvidenceLevel = Literal["aggregate", "usage", "blocks", "content"]
LaneKind = Literal["main", "subagent", "workflow_agent", "auxiliary", "compaction", "judge",
                   "ci", "scheduled", "batch", "other"]
Workload = Literal["interactive", "ci_agent", "pr_review", "headless", "scheduled", "eval",
                   "prompt_regression", "offline_batch", "embedding", "unknown"]   # W1..W9
AttemptOutcome = Literal["success", "error", "aborted", "refused", "timeout"]


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """Billed usage of ONE inference in DISJOINT buckets.
    Invariant: provider_reported_total_input ==
        uncached_input + cache_read + cache_write_5m + cache_write_1h + cache_write_other
        + cache_write_unknown   (adapters assert this; see §2.5)."""
    uncached_input: int = 0            # billed at the base input rate
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_write_other: int = 0         # a provider TTL other than 5m/1h ...
    cache_write_other_ttl_s: int = 0   # ... e.g. 1800 for OpenAI GPT-5.6+
    cache_write_unknown: int = 0       # TTL not reported (priced as a 5m..1h range)
    output: int = 0                    # ALL billed output INCLUDING thinking/reasoning
    output_thinking: int | None = None # subset of output; None = not reported
    input_image_tokens: int | None = None  # subset of input buckets; None = unknown
    web_search_requests: int = 0
    web_fetch_requests: int = 0
    code_exec_container_s: float = 0.0
    source: UsageSource = "final"

    @property
    def cache_write(self) -> int:      # all write buckets
        return (self.cache_write_5m + self.cache_write_1h + self.cache_write_other
                + self.cache_write_unknown)
    @property
    def total_input(self) -> int:      # the prompt ("context") size the model saw
        return self.uncached_input + self.cache_read + self.cache_write
    def plus(self, other: "UsageRecord") -> "UsageRecord": ...  # bucket-wise sum; source = worst


@dataclass(frozen=True, slots=True)
class Iteration:
    """One element of Anthropic usage.iterations[] (billing record per sub-inference)."""
    kind: Literal["message", "compaction", "advisor_message", "fallback_message", "other"]
    model: str | None                  # None -> the attempt's served model
    usage: UsageRecord
    declined_pre_output: bool = False  # refusal before any output: informational, NOT billed


@dataclass(frozen=True, slots=True)
class CacheDiagnostics:
    reason: Literal["model_changed", "system_changed", "tools_changed", "messages_changed",
                    "previous_message_not_found", "unavailable", "hit", "other"]
    missed_tokens_estimate: int | None # magnitude indicator ONLY; never used as billing
    source: Literal["anthropic_beta", "claude_code_transcript", "openai"]
    provider_reason: str | None = None # raw provider reason (e.g. one of OpenAI's nine)


@dataclass(frozen=True, slots=True)
class CacheMarker:
    block_index: int                   # index into PromptShape.blocks
    ttl_s: int                         # 300 | 3600 (| provider-specific)


@dataclass(frozen=True, slots=True)
class RequestParams:
    """Prompt-affecting parameters, content-free (hashes for free-form objects)."""
    max_tokens: int | None = None
    effort: str | None = None          # low|medium|high|xhigh|max|None(default)
    thinking: str | None = None        # "adaptive" | "enabled:<budget>" | "disabled" | None
    tool_choice_h: str | None = None
    output_format_h: str | None = None
    speed_requested: Speed = "standard"
    inference_geo_requested: str | None = None
    service_tier_requested: str | None = None
    betas_h: str | None = None
    context_management_h: str | None = None
    task_budget: int | None = None
    stream: bool | None = None
    cache_markers: tuple[CacheMarker, ...] = ()
    auto_cache: bool = False           # top-level cache_control
    diagnostics_enabled: bool = False
    fallback_credit_redeemed: bool | None = None
    base_url_class: Literal["first_party", "gateway", "cloud", "unknown"] = "unknown"
    advisor_model: str | None = None   # advisor tool model (advisor tokens bill via iterations)


BlockKind = Literal["tools", "tool_def", "system", "user_text", "assistant_text", "tool_use",
                    "tool_result", "thinking", "image", "document", "reminder",
                    "compaction_summary", "other"]

@dataclass(frozen=True, slots=True)
class Block:
    """Content-free prompt element in WIRE order. Hashes are HMAC-SHA256(org_key, bytes)[:32 hex].
    Hash inputs EXCLUDE cache_control keys (a moving marker is not a content change)."""
    kind: BlockKind
    role: Literal["tools", "system", "user", "assistant"]
    h: str                             # wire-order bytes (what the provider hashes)
    h_sorted: str                      # key-sorted canonical JSON (semantics); h!=h_sorted drift => serialization churn
    h_masked: str                      # volatile spans (dates, UUIDs, counters) replaced before hashing
    volatile_classes: tuple[str, ...] = ()   # e.g. ("iso_datetime",) computed locally pre-hash
    n_chars: int = 0
    n_tokens: float = 0.0              # estimate, rescaled per request to billed total_input
    token_basis: Literal["calibrated", "default_cpt", "pixels", "count_tokens"] = "default_cpt"
    msg_index: int = -1                # message index (-1 for tools/system)
    position: int = -1                 # lookback position after tool_use/tool_result run-collapsing
    tool_name_h: str | None = None     # HMAC of tool name (plain name only if allow-listed)
    tool_name: str | None = None
    is_error: bool | None = None
    image_px: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class PromptShape:
    shape_id: str                      # hash of the block-hash chain
    blocks: tuple[Block, ...]
    parent_shape_id: str | None = None # delta encoding in storage (trace@2 / sqlite)


@dataclass(frozen=True, slots=True)
class ToolResultMeta:
    tool_name_h: str
    tool_name: str | None              # only if allow-listed (built-ins like Bash, Read are)
    n_chars: int
    n_tokens_est: float
    is_error: bool
    images: int = 0
    input_h: str | None = None         # HMAC(tool name + canonical args) for loop detection
    result_h: str | None = None
    error_class: str | None = None     # permission|not_found|test_failure|mcp|other


@dataclass(frozen=True, slots=True)
class LaneEvent:
    kind: Literal["compaction", "clear", "context_edit", "model_fallback", "resume", "upgrade",
                  "mcp_change", "directory_change", "api_error", "user_interrupt", "image_eviction",
                  "thinking_dropped", "attachment", "prompt_snapshot"]
    ts: float
    pre_tokens: int | None = None
    post_tokens: int | None = None
    trigger: str | None = None         # auto|manual|threshold|...
    detail: Mapping[str, str | int | float] = field(default_factory=dict)  # numbers/enums only


@dataclass(frozen=True, slots=True)
class Attempt:
    attempt_id: str
    request_id: str
    attempt_no: int                    # 1-based within the logical request
    provider: Provider
    platform: Platform
    endpoint_scope: EndpointScope
    model_requested: str | None
    model_served: str                  # raw id as reported; rates.resolve() normalizes
    ts_start: float                    # request sent (TTL counts from here)
    ts_first_token: float | None
    ts_end: float | None
    outcome: AttemptOutcome
    usage: UsageRecord | None          # None = not billed / unknown (see billing_rule_id)
    iterations: tuple[Iteration, ...] = ()   # when present, iterations ARE the billing record
    service_tier: ServiceTier = "standard"   # RESOLVED tier actually served
    speed: Speed = "standard"
    inference_geo: str | None = None   # resolved ("us", "global", None)
    stop_reason: str | None = None
    http_status: int | None = None
    error_type: str | None = None
    retry_layer: Literal["none", "sdk", "agent", "gateway", "unknown"] = "unknown"
    retry_after_s: float | None = None
    should_retry: bool | None = None
    diagnostics: CacheDiagnostics | None = None
    provider_request_id: str | None = None
    provider_billed_nano: Nano | None = None # provider-reported cost (xAI ticks, OpenRouter cost)
    tokens_streamed_before_abort: int | None = None
    billing_rule_id: str | None = None


@dataclass(frozen=True, slots=True)
class Request:
    """One logical step (one agent turn) = a chain of attempts sharing a client request id."""
    request_id: str
    lane_id: str
    seq: int                           # 0-based order in lane by first attempt ts_start
    attempts: tuple[Attempt, ...]      # len >= 1
    params: RequestParams = RequestParams()
    shape: PromptShape | None = None   # None at evidence level 'usage'
    tool_results: tuple[ToolResultMeta, ...] = ()  # results appended in THIS request's input
    human_turn: bool = False           # triggered by a human prompt (vs tool completion)
    events_before: tuple[LaneEvent, ...] = ()      # events between previous request and this one
    visible_output_chars: int | None = None        # for message_start_only output estimation

    @property
    def final(self) -> Attempt: ...    # last attempt with outcome=='success', else last attempt


@dataclass(frozen=True, slots=True)
class Lane:
    """A sequence of requests sharing one cache lineage (main thread, one subagent, ...)."""
    lane_id: str
    session_id: str
    kind: LaneKind
    requests: tuple[Request, ...]
    parent_lane_id: str | None = None
    agent_type: str | None = None      # e.g. general-purpose, Explore, workflow-subagent
    cache_domain: str = "unknown"      # HMAC(workspace) on 1P/AWS/Foundry; HMAC(org) on Bedrock/Vertex
    ttl_hint_s: int | None = None      # configured TTL if known (300|3600)


@dataclass(frozen=True, slots=True)
class Attribution:
    """Dimensions for grouping. All identifiers are HMAC pseudonyms except org-configured labels."""
    org: str | None = None
    team: str | None = None
    cost_center: str | None = None
    user_h: str | None = None
    repo_h: str | None = None
    branch_h: str | None = None
    project: str | None = None
    env: str | None = None             # prod|nonprod|ci|...
    workload: Workload = "unknown"
    workspace_h: str | None = None
    api_key_h: str | None = None
    agent_name: str | None = None
    skill: str | None = None
    plugin: str | None = None
    mcp_server: str | None = None
    experiment_arm: str | None = None  # from OTEL_RESOURCE_ATTRIBUTES tokenbill.arm
    experiment_wave: str | None = None
    extra: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Harness:
    name: str                          # claude_code|codex|agent_sdk|langgraph|crewai|custom|...
    version: str | None = None
    entrypoint: str | None = None      # cli|sdk-py|claude-code-github-action|claude-desktop|...
    framework_scope: str | None = None # OTel instrumentation scope name for framework fingerprinting
    settings_fingerprint: str | None = None  # hash of effective managed settings, if known
    billing_path: Literal["subscription", "api_key", "usage_credits", "bedrock", "vertex",
                          "foundry", "gateway", "unknown"] = "unknown"


@dataclass(frozen=True, slots=True)
class SessionOutcome:
    success: bool | None = None
    signal: Literal["tests_pass", "pr_merged", "ci_green", "eval_pass", "ticket_closed",
                    "none"] = "none"
    task_id: str | None = None         # pairing key for A/B
    pr_merged: bool | None = None
    reverted_within_30d: bool | None = None
    human_prompts: int | None = None


@dataclass(frozen=True, slots=True)
class SourceRef:
    adapter_id: str
    adapter_version: str
    source_h: str                      # HMAC of file path / endpoint (never the raw path)
    imported_at: float


@dataclass(frozen=True, slots=True)
class Session:
    session_id: str                    # namespaced: f"{adapter_id}:{HMAC(raw session id)}"
    source: SourceRef
    attribution: Attribution
    harness: Harness
    lanes: tuple[Lane, ...]
    ts_start: float
    ts_end: float
    evidence_level: EvidenceLevel
    outcome: SessionOutcome | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UsageBucket:
    """Aggregate usage (Admin usage_report, Analytics APIs, OTel metric sums)."""
    source: SourceRef
    bucket_start: float
    bucket_end: float
    provider: Provider
    platform: Platform
    dims: Mapping[str, str]            # workspace_h, api_key_h, model, service_tier, context_window,
                                       # inference_geo, speed, user_h, team, ...
    usage: UsageRecord                 # source='aggregate'
    requests: int | None = None


@dataclass(frozen=True, slots=True)
class InvoiceLine:
    """Provider cost report row (e.g. Anthropic cost_report: daily, decimal-cent strings)."""
    source: SourceRef
    day: str                           # YYYY-MM-DD (UTC)
    dims: Mapping[str, str]            # workspace_h, model, cost_type, token_type, inference_geo, ...
    amount_nano: Nano                  # parsed from decimal cents EXACTLY (Decimal, no float)
    list_amount_nano: Nano | None = None   # Enterprise Analytics list_amount when available
    provisional: bool = True           # within the provider's revision window (30 days)


@dataclass(frozen=True, slots=True)
class ActivityDay:
    """Per user-day activity/outcomes (Claude Code Analytics API or git/CI joins); content-free."""
    user_h: str
    day: str
    active: bool
    sessions: int = 0
    commits: int = 0
    prs_merged: int = 0
    lines_added: int = 0
    edits_accepted: int = 0
    edits_rejected: int = 0
    estimated_cost_nano: Nano | None = None
    attribution: Attribution = Attribution()


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """Output of RateCard.price(). Point estimate plus a low/high range; all nano-USD."""
    uncached_input: Nano = 0
    cache_read: Nano = 0
    cache_write_5m: Nano = 0
    cache_write_1h: Nano = 0
    cache_write_other: Nano = 0
    cache_write_unknown: Nano = 0      # point estimate uses the TTL hint; range below
    output: Nano = 0
    server_tools: Nano = 0
    runtime: Nano = 0                  # code-exec container hours, managed-agent session hours
    iteration_costs: tuple[tuple[str, Nano], ...] = ()   # (iteration kind, nano) e.g. ("compaction", ...)
    total: Nano = 0
    total_low: Nano = 0
    total_high: Nano = 0
    label: Label = "exact"             # 'estimated' if any usage is estimated/unknown-TTL/unpriced
    basis: Basis = "list"
    rate_row_ids: tuple[str, ...] = ()
    modifiers: tuple[str, ...] = ()    # e.g. ("batch:0.5", "geo_us:1.1", "fast_mode")
    unpriced_reason: str | None = None # set => all fields 0, label 'estimated', counted separately


@dataclass(frozen=True, slots=True)
class Money:
    nano: Nano
    label: Label
    basis: Basis = "list"


LeverClass = Literal["rate", "cache_transform", "trajectory", "behavioral", "commercial"]

@dataclass(frozen=True, slots=True)
class SavingsEstimate:
    window_days: float                 # the observation window the dollars refer to
    baseline_nano: Nano                # billed cost of in-scope traffic (exact)
    replay_as_is_nano: Nano            # replay of the as-is policy (for agreement)
    replay_policy_nano: Nano           # replay under the lever
    ceiling_nano: Nano                 # replay_as_is - replay_policy (difference of replays)
    ceiling_low_nano: Nano
    ceiling_high_nano: Nano
    label: Label                       # 'exact' only for rate levers on identical tokens
    method: str                        # "replay:usage" | "replay:blocks" | "reprice" | "arith"
    lever_class: LeverClass
    rr: tuple[float, float, float]     # realization-rate prior (p10, p50, p90) used
    expected_nano: tuple[Nano, Nano, Nano]  # ceiling * rr quantiles (p10, p50, p90)
    calibrated: bool                   # scope passed the calibration gate
    monthly_run_rate_nano: Nano        # expected p50 scaled to 30 days
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PolicyPatch:
    target: Literal["claude_code_managed_settings", "claude_code_env", "litellm_proxy",
                    "claude_code_hook", "sdk_snippet", "gateway_config", "ci_workflow"]
    payload: Mapping[str, object]      # JSON-serializable
    cohort: str = "all"                # org | MDM group | team | repo selector
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Fix:
    text: str                          # one or two sentences, imperative
    doc_url: str | None = None
    gates: Mapping[str, str] = field(default_factory=dict)   # e.g. {"platform":"anthropic_api",
                                                             # "models":"opus-5,opus-5-5,fable-5*"}
    patch: PolicyPatch | None = None
    tradeoff: bool = False
    requires_eval: bool = False


@dataclass(frozen=True, slots=True)
class Finding:
    finding_id: str                    # sha256(detector_id|scope_key|window)[:16]
    detector_id: str                   # e.g. "cache.gateway_strip"
    category: Literal["context", "routing", "cache", "failure", "automation", "carry",
                      "premium", "compression", "commercial", "correctness"]
    title: str
    scope_key: str                     # canonical group key, e.g. "team=payments|lane_kind=main"
    session_ids: tuple[str, ...]       # capped at 50; count in occurrences
    occurrences: int
    evidence: Mapping[str, int | float | str]   # numbers/enums only; never content
    lever_id: str | None               # lever that fixes it (joins to optimizer)
    savings: SavingsEstimate | None
    fix: Fix
    confidence: Literal["high", "medium", "low"]
    ground_truth: Mapping[str, int] | None = None   # diagnostics reason counts over its events
```

The policy records (`PolicySpec` and its components) and the replay results (`LaneReplay`, `ReplayResult`) also live in `core/records.py`:

```python
TtlChoice = Literal["as_is", "5m", "1h", "keepalive"]

@dataclass(frozen=True, slots=True)
class TtlPolicy:
    by_lane_kind: tuple[tuple[LaneKind, TtlChoice], ...] = ()
    default: TtlChoice = "as_is"
    keepalive_interval_s: int = 240    # Anthropic: re-send within 4 min of the previous start
    keepalive_max_idle_s: int = 3600

@dataclass(frozen=True, slots=True)
class CompactionPolicy:
    window_tokens: int                 # compact when effective context > window
    summary_tokens: int | None = None  # None -> org median CC postTokens; fallback 20_283
    lane_kinds: tuple[LaneKind, ...] = ("main",)

@dataclass(frozen=True, slots=True)
class ColdResumePolicy:
    action: Literal["compact", "clear"] = "compact"
    min_context_tokens: int = 200_000
    summary_tokens: int | None = None
    lane_kinds: tuple[LaneKind, ...] = ("main",)

@dataclass(frozen=True, slots=True)
class ModelMapRule:
    to_model: str
    lane_kinds: tuple[LaneKind, ...] = ()        # empty = any
    agent_types: tuple[str, ...] = ()
    from_models: tuple[str, ...] = ()            # base ids; empty = any
    tokenizer_factor: tuple[float, float, float] | None = None  # (low, point, high); None -> rates

@dataclass(frozen=True, slots=True)
class ModelMapPolicy:
    rules: tuple[ModelMapRule, ...]              # first match wins

@dataclass(frozen=True, slots=True)
class EffortPolicy:
    max_effort: Literal["low", "medium", "high", "xhigh", "max"]
    thinking_scale: tuple[float, float, float]   # (low, point, high) multiplier on thinking tokens
                                                 # of calls above max_effort; from evidence registry

@dataclass(frozen=True, slots=True)
class BatchPolicy:
    workloads: tuple[Workload, ...] = ("eval", "offline_batch", "prompt_regression", "headless")
    single_shot_only: bool = True
    cache_hit_band: tuple[float, float] = (0.30, 0.98)   # batch cache hits are best-effort

@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_total_attempts: int = 3
    max_backoff_s: float = 60.0
    single_owner: bool = True

@dataclass(frozen=True, slots=True)
class ToolOutputCapPolicy:
    max_result_tokens: int
    tools: tuple[str, ...] = ()        # empty = all tools

@dataclass(frozen=True, slots=True)
class BreakpointPolicy:                # Mode B only
    mode: Literal["as_is", "auto_end", "static_plus_tail", "every_k_positions", "search"]
    k_positions: int = 15
    max_breakpoints: int = 4

@dataclass(frozen=True, slots=True)
class PolicySpec:
    policy_id: str
    ttl: TtlPolicy | None = None
    compaction: CompactionPolicy | None = None
    cold_resume: ColdResumePolicy | None = None
    model_map: ModelMapPolicy | None = None
    effort: EffortPolicy | None = None
    fast_mode_off: bool = False
    geo_global: bool = False
    batch: BatchPolicy | None = None
    stagger_fanout: bool = False
    retry: RetryPolicy | None = None
    tool_output_cap: ToolOutputCapPolicy | None = None
    breakpoints: BreakpointPolicy | None = None
    repairs: tuple[str, ...] = ()      # Mode B: detector ids whose block repairs apply
    restore_caching: bool = False      # gateway-strip / beta-header fix
    shared_ci_prefix: bool = False     # exclude dynamic sections + single workspace for CI lanes
    def compose(self, other: "PolicySpec") -> "PolicySpec": ...  # component-wise; conflict -> ValueError

AS_IS = PolicySpec(policy_id="as_is")

@dataclass(frozen=True, slots=True)
class LaneReplay:
    lane_id: str
    cost: Nano
    cost_low: Nano
    cost_high: Nano
    usage: UsageRecord                 # simulated usage summed over the lane
    added_calls: int = 0               # simulated compactions / keep-alive pings
    cold_events: int = 0

@dataclass(frozen=True, slots=True)
class ReplayResult:
    policy_id: str
    mode: Literal["usage", "blocks"]
    lanes: tuple[LaneReplay, ...]
    total: Nano
    total_low: Nano
    total_high: Nano
    billed_total: Nano                 # exact billed cost of the same lanes
```

### 2.5 Cached-token accounting conventions (adapters MUST implement exactly)

The core rule: **buckets are disjoint, and `uncached_input` never contains cached tokens.** Each adapter declares its source convention in `ingest/conventions.py` (a registry keyed by source, framework, version range and provider route [fw-telemetry-conventions]). Each adapter also sum-checks every record against the source's own total when one exists.

| Source | Source fields | Mapping into `UsageRecord` | Evidence |
|---|---|---|---|
| Anthropic Messages (1P, Claude Platform AWS, Foundry, Vertex rawPredict, Bedrock InvokeModel) | `input_tokens` (exclusive), `cache_read_input_tokens`, `cache_creation_input_tokens`, `cache_creation.ephemeral_5m_input_tokens`, `cache_creation.ephemeral_1h_input_tokens`, `output_tokens`, `output_tokens_details.thinking_tokens`, `server_tool_use.{web_search_requests, web_fetch_requests}`, `service_tier`, `speed`, `inference_geo`, `iterations[]` | `uncached_input = input_tokens`. `cache_read = cache_read_input_tokens`. `cache_write_5m` and `cache_write_1h` come from the split. If the split is missing or doesn't sum to `cache_creation_input_tokens`, the residual goes to `cache_write_unknown`. **If `iterations` has >1 element, price each iteration and ignore top-level**: top-level equals the LAST iteration only (27/27 cases). `inference_geo == "not_available"` maps to None | [anth-cache-1h-2x], [cc-iterations-fallback], [anth-compaction-iterations-billing], [advisor-iterations-undercount] |
| Bedrock Converse | `inputTokens` (exclusive), `cacheReadInputTokens`, `cacheWriteInputTokens`, `cacheDetails[{ttl, inputTokens}]` | Split writes by `cacheDetails.ttl`; any residual goes to `cache_write_unknown` | [bedrock-cache-semantics] |
| OpenAI Responses/Chat (GPT-5.6+) | `input_tokens` (**inclusive**), `input_tokens_details.cached_tokens`, `cache_write_tokens`, `output_tokens`, `output_tokens_details.reasoning_tokens`, `service_tier` | `uncached = input − cached − cache_write`. `cache_read = cached`. `cache_write_other = cache_write_tokens` with `ttl_s = 1800`. `output_thinking = reasoning_tokens`. Assert `cached + write ≤ input` | [oai-usage-inclusive], [oai-56-explicit-cache] |
| OpenAI pre-5.6 | `cached_tokens` (rounded down to 128), no write fee | Same mapping, with `cache_write_* = 0` | [oai-retention-defaults] |
| Gemini generateContent | `promptTokenCount` (**inclusive** of `cachedContentTokenCount`), `candidatesTokenCount`, `thoughtsTokenCount` (separate, billed as output), `toolUsePromptTokenCount` | `uncached = prompt − cached (+ toolUse, flagged)`. `cache_read = cached`. `output = candidates + thoughts`. `output_thinking = thoughts` | [gemini-usage-semantics] |
| OTel GenAI semconv (≥1.40; the repo moved in 1.42) | `gen_ai.usage.input_tokens` (**inclusive**), `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_write.input_tokens` (legacy name `cache_creation`), `gen_ai.usage.output_tokens`, `gen_ai.usage.reasoning.output_tokens` | `uncached = input − read − write`. Writes go to `cache_write_unknown`. **Count only LLM-kind spans**: OpenInference `openinference.span.kind == LLM`, never AGENT/CHAIN rollups | [otel-genai-semconv], [ent-otel-genai-semconv], [otel-openinference-importer] |
| Claude Code OTel `claude_code.api_request` | `input_tokens` (exclusive), `cache_read_tokens`, `cache_creation_tokens`, `output_tokens`, `cost_usd`, `speed`, `effort`, `query_source`, `request_id`, `client_request_id`, `duration_ms`, `attempt`, `success` | Anthropic mapping. Writes go to `cache_write_unknown` (**OTel has no TTL split**) unless an `api_response_body` event carries `usage.cache_creation`: then parse the usage object only and discard the body. Ignore `cost_usd` except as the "harness estimate" comparator | [cc-otel-schema], [cc-enterprise-telemetry-gap] |
| xAI | `usage.cost_in_usd_ticks` (1 USD = 1e10 ticks) | `provider_billed_nano = ticks // 10` (exact integer division: 1 tick = 0.1 nano-USD; record the remainder) | [xai-cost-ticks] |
| OpenRouter | `usage.cost`, `cached_tokens`, `cache_write_tokens` | `provider_billed_nano` from `cost`. Tokens are inclusive, so OpenAI-style mapping | [openrouter-accounting] |
| DeepSeek | `prompt_cache_hit_tokens`, `prompt_cache_miss_tokens` | `cache_read = hit`; `uncached = miss` | [deepseek-offpeak-cache] |
| Anthropic Admin usage_report | uncached input, cache read, cache_creation 5m/1h, output, web_search_requests per bucket | `UsageBucket` with `source='aggregate'` | [anth-admin-usage-cost-api] |

Every adapter test includes a fixture whose buckets sum to the source's billed total. Mis-mapping inclusive and exclusive fields is the most common way cost tools double-count [ent-usage-normalization].

### 2.6 Money, rounding and labels

- **Unit.** Money is `int` nano-USD everywhere.
- **Rates.** Rate-card JSON stores `$ per million tokens` as decimal strings. At load time they convert to **int pico-USD per token**: `$X/MTok` becomes exactly `X × 10^6` pico/token, so `$4/MTok` is `4_000_000`. Multipliers are applied at load time into per-bucket integer rates in `UnitRates` (e.g. Opus 5.5 read = 4,000,000 × 0.05 = 200,000 pico/token).
- **Exactness assertion.** `catalog.py` asserts every derived rate is an exact integer. Any published rate with ≤ 6 decimals in $/MTok, times multipliers with ≤ 3 decimals, satisfies this. If a combination is not exact, loading the card raises.
- **Computation.** Per attempt and per bucket, `cost_pico = tokens × rate_pico`: an int product, with no Decimal and no float on the hot path. Convert to nano with round-half-even `// 1000` **once per bucket per attempt**. The maximum error is 0.5 nano-USD per bucket. Sums are int sums, so there is no float drift.
- **Speed.** The replay engine uses the same int rates, which keeps Mode U fast (§2.15).
- **Exports.** Receipts and FOCUS exports convert to integer micro-USD or decimal strings [signed-receipts], [ent-reconciliation].
- **Parsing.** Invoice amounts in decimal-cent strings are parsed with `Decimal`, never `float` [ent-reconciliation].
- **Labels.** These apply to a `(number, scope)` pair.

  | Label | Meaning | Minimum evidence |
  |---|---|---|
  | `exact` | Arithmetic on billed usage × versioned rate card, with no behavioral counterfactual. Examples: the ledger; Effective Token Savings Rate; repricing identical tokens under another rate; reconciliation deltas | Rate card sha256 recorded; `source=final` usage |
  | `estimated` | Replay or simulation of an undeployed lever (IPMVP Option D), always shown as replay × RR interval | The calibration flag is shown; "uncalibrated" if the gate fails |
  | `measured` | Observed change vs an adjusted baseline without randomized assignment (ITS, self-selected DiD, synthetic control) | Placebo/pre-trend passed; CI reported |
  | `verified` | Randomized or randomized-order design with every guard passing (§3 F9); receipt signed | All §3 F9 guards |

  A lab A/B (`tokenbill ab`) can be `verified` only at the scope `lab:<task-set>`. Any fleet projection derived from it is `estimated`.
- **Bases.** `list` (published rates), `contract` (rate card overlay mirroring Claude Code `modelPricing`), `invoice` (reconciled to provider cost reports) [cc-list-price-estimates]. They are never mixed in a sum; the output shows them side by side.

### 2.7 Storage

**Interchange: `tokenbill/trace@2` JSONL (WP3a).** Content-free. One record per line, with a `rec` discriminator:

```jsonc
{"schema":"tokenbill/trace@2","rec":"session", "session_id":"...", "source":{...}, "attribution":{...},
 "harness":{...}, "ts_start":..., "ts_end":..., "evidence_level":"usage", "outcome":{...}}
{"schema":"tokenbill/trace@2","rec":"lane", "lane_id":"...", "session_id":"...", "kind":"main", ...}
{"schema":"tokenbill/trace@2","rec":"shape", "shape_id":"...", "parent":"<shape_id>|null",
 "keep":123, "append":[{Block}, ...]}                         // delta: first `keep` parent blocks + append
{"schema":"tokenbill/trace@2","rec":"request", "request_id":"...", "lane_id":"...", "seq":0,
 "shape_id":"...|null", "params":{...}, "attempts":[{Attempt incl. usage, iterations}],
 "tool_results":[...], "events_before":[...], "human_turn":false}
{"schema":"tokenbill/trace@2","rec":"bucket"|"invoice"|"activity", ...}
```

Rules:
- Unknown keys go under `"ext"` and are preserved on round trip.
- The reader streams line by line with a 16 MiB line cap.
- `--lenient` sends bad lines to `<file>.quarantine.jsonl` and prints a count instead of aborting [cb-strict-parse].
- gzip (`.gz`) and stdin (`-`) are supported.
- Files are created with mode `0600` [cb-security-privacy].
- Delta encoding makes size linear in session length. v0.1 traces grow quadratically: a 300-call session was 184 MB [cb-scale-memory], [ent-scale-storage].

**Ledger: SQLite (WP9, `ledger/store.py`).** Stdlib `sqlite3`, WAL, `0600`, schema version stored in `meta`. Idempotent upserts are keyed by `attempt_id` = `HMAC(provider_request_id or adapter-specific stable key)`, so re-ingesting the same transcripts never double counts [cb-dup-runid].

```sql
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);           -- schema_version, hmac_key_id, k_min
CREATE TABLE sources(source_h TEXT PRIMARY KEY, adapter TEXT, adapter_version TEXT, imported_at REAL,
  n_sessions INT, n_attempts INT, warnings TEXT);
CREATE TABLE sessions(session_id TEXT PRIMARY KEY, source_h TEXT, evidence_level TEXT, ts_start REAL, ts_end REAL,
  org TEXT, team TEXT, cost_center TEXT, user_h TEXT, repo_h TEXT, workspace_h TEXT, api_key_h TEXT,
  workload TEXT, harness TEXT, harness_version TEXT, entrypoint TEXT, billing_path TEXT,
  arm TEXT, wave TEXT, outcome_json TEXT, attrib_json TEXT);
CREATE TABLE lanes(lane_id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, parent_lane_id TEXT, agent_type TEXT,
  cache_domain TEXT, ttl_hint_s INT);
CREATE TABLE requests(request_id TEXT PRIMARY KEY, lane_id TEXT, seq INT, shape_id TEXT, params_json TEXT,
  human_turn INT, tool_results_json TEXT, events_json TEXT, visible_output_chars INT);
CREATE TABLE attempts(attempt_id TEXT PRIMARY KEY, request_id TEXT, attempt_no INT, provider TEXT, platform TEXT,
  endpoint_scope TEXT, model_requested TEXT, model_served TEXT, model_base TEXT, ts_start REAL, ts_first_token REAL,
  ts_end REAL, outcome TEXT, http_status INT, error_type TEXT, retry_layer TEXT, retry_after_s REAL,
  service_tier TEXT, speed TEXT, inference_geo TEXT, stop_reason TEXT,
  u_uncached INT, u_read INT, u_w5m INT, u_w1h INT, u_wother INT, u_wother_ttl INT, u_wunknown INT,
  u_output INT, u_thinking INT, u_web_search INT, u_web_fetch INT, u_source TEXT, iterations_json TEXT,
  diag_reason TEXT, diag_missed INT, provider_billed_nano INT,
  cost_nano INT, cost_low_nano INT, cost_high_nano INT, cost_label TEXT, rate_card_sha TEXT);
CREATE TABLE shapes(shape_id TEXT PRIMARY KEY, parent_shape_id TEXT, keep INT, blocks_json TEXT);
CREATE TABLE buckets(id INTEGER PRIMARY KEY, source_h TEXT, t0 REAL, t1 REAL, provider TEXT, platform TEXT,
  dims_json TEXT, usage_json TEXT, requests INT);
CREATE TABLE invoice_lines(id INTEGER PRIMARY KEY, source_h TEXT, day TEXT, dims_json TEXT, amount_nano INT,
  list_amount_nano INT, provisional INT);
CREATE TABLE activity_days(user_h TEXT, day TEXT, active INT, sessions INT, commits INT, prs_merged INT,
  lines_added INT, edits_accepted INT, edits_rejected INT, est_cost_nano INT, attrib_json TEXT,
  PRIMARY KEY(user_h, day));
CREATE TABLE findings(finding_id TEXT PRIMARY KEY, run_id TEXT, json TEXT);
CREATE TABLE experiments(plan_id TEXT PRIMARY KEY, plan_json TEXT, prereg_sha256 TEXT, created REAL);
CREATE TABLE assignments(plan_id TEXT, unit_id TEXT, arm TEXT, wave INT, PRIMARY KEY(plan_id, unit_id));
CREATE TABLE receipts(receipt_id TEXT PRIMARY KEY, plan_id TEXT, label TEXT, json TEXT, sig BLOB);
CREATE INDEX ix_att_req ON attempts(request_id); CREATE INDEX ix_req_lane ON requests(lane_id, seq);
CREATE INDEX ix_att_ts ON attempts(ts_start); CREATE INDEX ix_sess_team ON sessions(team, ts_start);
```

Retention:
- `tokenbill purge --before DATE` deletes rows older than a date.
- `tokenbill purge --user USER_H` deletes one pseudonymous user's rows (DSAR).
- Defaults recommended in the privacy pack: 90 days for individual-level rows, 13 months for aggregates [ent-privacy-by-default], [aggregation-k5].

### 2.8 Replay engines (the savings engine)

**Principle: difference of replays.** For any lever on scope S:

```
ceiling = replay(S, AS_IS) − replay(S, policy)
counterfactual cost = billed(S) − ceiling
agreement = replay(S, AS_IS) / billed(S) − 1
```

Agreement feeds the calibration gate. Model error in the engine appears in both arms and cancels to first order [mv-adjusted-baseline].

#### Mode U: usage replay (`sim/usage_replay.py`, WP4a)

Mode U works at evidence level `usage` and reproduces the corpus methodology that produced the headline numbers ([cc-compaction-threshold-sim], [cc-cold-resume], [cc-ttl-advisor]).

**Per-lane inputs.** Requests are sorted by `(final.ts_start, seq)`. For request k:
- `U` = billed usage of the successful attempt, with iterations summed.
- `ctx = U.total_input`, `cr = U.cache_read`, `cw = U.cache_write`, `un = U.uncached_input`, `out = U.output`, `th = U.output_thinking`.
- `t = final.ts_start`, `m` = base model, `d = lane.cache_domain`.
- `gap = t − t_prev`, measured start to start: TTL counts from request start and reads refresh it [anth-ttl-start-1h].
- Failed attempts are priced as-is and added separately unless `RetryPolicy` applies.

**TTL resolution.**
- `as_is` means: `3600` if the lane billed any `cache_write_1h`; else `lane.ttl_hint_s`; else `300`.
- For `cache_write_unknown`, the point estimate uses the hint from `Harness.billing_path` (subscription → 1h for main lanes; API key, cloud or credits → 5m [cc-ttl-policy]). `cost_low` and `cost_high` use 5m and 1h.

**Pricing.** Pricing logic lives only in `rates/`.
- **Hot path** (`UsageReplay`, WP4a). The engine fetches `ctx.rates.unit_rates(model, tier, speed, geo, scope, ts)` once per distinct key: the original attempt's values, after policy transforms. It then does int pico arithmetic per step. The `price_read`, `price_write(ttl)`, `price_uncached` and `price_out` helpers below are `tokens × UnitRates.<bucket>`.
- **Reference path** (`ReferenceReplay`, WP0b). Builds a `UsageRecord` per step and calls `ctx.rates.price_usage(...)`.
- **Equivalence.** The two paths must agree to the nano. That is WP4a's differential test.
- **Transitions.** A cold transition within the same model family keeps the attempt's tier, speed and geo. Model-map transforms use the target model's rates with the same tier, speed and geo.

**Step algorithm.** State per lane: `removed=0, prev_ctx_obs=None, prev_ctx_eff=0, prev_t=None, prev_model=None, P0=ctx of request 0`.

```
for k, req in enumerate(requests):
    # 0. transforms that change the model or tokens of this call
    m2, f = model_map(req) ; out2 = out
    if effort policy applies to req.params.effort > max_effort:
        th_k = th if th is not None else evidence.thinking_share_prior * out   # 0.505 [cc-output-thinking-effort]
        out2 = out - th_k * (1 - thinking_scale)      # (low, point, high) -> cost_low/high
    scale token counts by tokenizer factor f when m2's tokenizer generation != m's
    # 1. observed real reset (compaction/clear actually happened in the bill)
    if prev_ctx_obs is not None and ctx < 0.5 * prev_ctx_obs: removed = 0
    # 2. effective tokens after counterfactual removals
    ctx_eff = ctx - removed
    cr_eff = max(0, cr - removed)
    cw_eff = cw if cr >= removed else max(0, cw - (removed - cr))
    # 3. is the cached prefix alive under the policy?
    ttl = resolve_ttl(policy, lane.kind)                # 300 | 3600 | keepalive
    warm = k > 0 and m2 == prev_model and gap <= ttl_seconds(ttl)      # ttl_seconds(keepalive) = 300
    # (simulated) param invalidation: speed flip; effort or thinking change on a model whose
    # rate-card `supports` lacks "effort_keeps_cache" (Opus 5.5 / Fable 5.1 on 1P) [cc-cache-breakers]
    if warm and param_invalidates(prev_sim_params, sim_params(req)): warm = False
    if ttl == keepalive and k > 0 and 300 < gap:
        pings = floor(min(gap, max_idle) / interval) ; cost += pings * price_read(prev_ctx_eff)
        warm = gap <= max_idle ; added_calls += pings
    # 4. cold-resume policy (only when not warm)
    if not warm and k > 0 and cold_resume and lane.kind in kinds and ctx_eff > min_ctx:
        S = summary_tokens ; new = max(0, ctx - prev_ctx_obs)
        if action == "compact":  cost += price_uncached(ctx_eff) + price_out(S)   # cold summarization call
        if action == "clear":    S = P0                                            # restart from static prefix
        removed = ctx - S ; cost += price_write(S + new, ttl) + price_uncached(un) + price_out(out2)
        added_calls += (action == "compact") ; cold_events += 1 ; goto next
    # 5. compaction-window policy
    if compaction and lane.kind in kinds and ctx_eff > window:
        S = summary_tokens
        cost += (price_read(ctx_eff) if warm else price_write(ctx_eff, ttl)) + price_out(S)   # [compaction-timing]
        removed = ctx - S ; cost += price_write(S, ttl) + price_uncached(un) + price_out(out2)
        added_calls += 1 ; goto next
    # 6. ordinary step
    if warm: cost += price_read(cr_eff) + price_write(cw_eff, ttl) + price_uncached(un) + price_out(out2)
    else:    cost += price_write(cr_eff + cw_eff, ttl) + price_uncached(un) + price_out(out2); cold_events += (k > 0)
  next:
    prev_ctx_obs = ctx ; prev_ctx_eff = ctx - removed ; prev_t = t ; prev_model = m2
```

**Other transforms in Mode U.**
- **`fast_mode_off`, `geo_global`**: reprice attempts at standard speed or the global scope. These are exact rate levers.
- **Fast-mode flapping**: speed changes within a lane force `warm=False`, because fast and standard don't share a cache [premium-modifiers].
- **`batch`**: eligible requests are priced at tier `batch`. `cost_high` retains only `band_low` (30%) of reads as reads and turns the rest into writes; `cost_low` retains `band_high` (98%) [anth-batch-stacking].
- **`stagger_fanout`**: find groups of ≥2 lane-first requests in the same `(cache_domain, model)` whose `ts_start` values fall within `ttft_window` of each other. The window is the observed p50 `ts_first_token − ts_start`, or 5 s. Each group member must have `cw ≥ 0.8·ctx`. Under the policy, all but the first convert `min(cw_i, shared)` write tokens to reads, where `shared = min_i ctx_i × s_session`. `s_session` is the session's observed share of first-call prefix read from cache by *non-concurrent* sibling lanes; it is 0 if unobservable, and then the finding says "needs block evidence" [cc-agent-spinup-fanout], [anth-concurrency-fanout].
- **`retry`**: for a logical request whose successful attempt started more than `ttl` after the first attempt's start and wrote ≥50% of the prefix, the policy (backoff capped below TTL) re-prices that attempt as warm. Attempts beyond `max_total_attempts` are dropped [fp-ttl-from-request-start].
- **`tool_output_cap`**: for each `ToolResultMeta` over the cap, `removed += n_tokens_est − cap` from the request that appends it. This is a trajectory lever: label `estimated`, with a wide RR.
- **`restore_caching`**: lanes flagged by the gateway-strip detector are re-priced as if every token of the previous request's context were a read. The healthy-loop signature holds: writes ≈ the previous output plus new input [cc-gateway-cache-strip].
- **`shared_ci_prefix`**: CI lanes in the same cache domain whose first call wrote ≥ P tokens become readers of the first writer's entry, as long as that entry is alive under the TTL (start-to-start gaps across runs) [ci-cross-run-cache].

**Complexity.** O(requests) per lane per policy, with no sorting inside the loop. The v0.1 simulator re-sorted per call and was O(n²) [cb-scale-memory].

#### Mode B: block replay (`sim/block_replay.py`, `sim/cachemodels.py`, WP5)

Mode B works at evidence level `blocks` and implements the documented Anthropic rules, which v0.1 missed [cb-lookback], [cb-concurrency], [cb-shared-prefix-sim], [cb-cross-run-cache]:

1. **Tiered hash chain.**
   - `H_tools = H(model, tools-tier params, tool blocks...)`
   - `H_sys = H(H_tools, system-tier params, system blocks...)`
   - `H_msg[i] = H(H_msg[i-1] or H_sys, messages-tier params, block_i)`

   The invalidation hierarchy is expressed by which parameters enter which tier [anth-invalidation-hierarchy]:
   - **Tools tier**: tool definitions, model.
   - **System tier**: speed, web-search and citations toggles, system.
   - **Messages tier**: tool_choice, `disable_parallel_tool_use`, image presence, thinking, effort. Thinking and effort also enter all tiers on models the rule table marks.

   Hashes use `Block.h` (wire order). The repair transforms swap in `h_sorted` or `h_masked` as their counterfactual.
2. **Entries** are keyed `(cache_domain, model, chain_hash_at_position)` with `written_ts`, `visible_ts = writer.ts_first_token` (or `ts_start + ttft_default`), `expires = ts_start + ttl`, and a refresh on read: `expires = max(expires, reader.ts_start + ttl)` [anth-ttl-start-1h], [anth-concurrency-fanout].
3. **Breakpoints.** There are at most 4, taken from `params.cache_markers` or `auto_cache`, or from `BreakpointPolicy`. For each breakpoint, look back at most **20 positions**, counting the breakpoint as the first. A run of consecutive `tool_use` blocks is one position, and likewise for `tool_result` [anth-lookback-20], [agent-cache-lookback-idle]. The longest visible, alive hit wins.
4. **Charges.**
   - Tokens up to the hit are `cache_read`.
   - Tokens from the hit to the last breakpoint are writes, charged per segment at that segment's breakpoint TTL. Mixed TTLs must be ordered longest first, or the ordering error is flagged.
   - Tokens after the last breakpoint are `uncached_input`.
   - A breakpoint whose prefix is below the model's `min_cacheable_tokens` writes nothing [anth-cache-hidden-miss-causes].
5. **Token sizes.** Each block's `n_tokens` is rescaled per request so that Σ = billed `total_input` (the v0.1 honesty invariant) [DESIGN.md §1].
6. **Rule tables** (`cachemodels.py`) are data per `(provider, platform)`:
   - Anthropic 1P: workspace scope, 20-position lookback, 4 breakpoints.
   - Bedrock: org scope, per-channel minimums, no caching in batch [bedrock-cache-semantics].
   - OpenAI GPT-5.6+: 30m TTL, 1.25× write, 1,024 minimum, implicit plus ≤3 explicit breakpoints, matching over the first 2 and latest 50 explicit breakpoints [oai-56-explicit-cache].
7. **Validation.** When billed usage shows cache activity, per-request predicted `(cr, cw)` vs billed gives an agreement ratio. When `diagnostics` exist, the predicted first-divergence tier vs `*_changed` gives a confusion matrix. Only the four `*_changed` reasons map to breakers [cc-miss-taxonomy-ground-truth].

#### Calibration gate (`sim/calibrate.py`, WP4a; surfaced by `tokenbill calibrate`)

The gate follows ASHRAE Guideline 14 via FEMP M&V 4.0 [ashrae-calibration-gate]. For each period i in the scope (month, or day for the daily gate):
- `m_i` = billed cost, exact from usage. If `reconcile` has invoice lines for the scope, those are used and the basis becomes `invoice`.
- `s_i` = `replay(AS_IS)` cost.

```
NMBE     = Σ(m_i − s_i) / ((n − 1) · mean(m))
CV(RMSE) = sqrt( Σ(m_i − s_i)² / (n − 1) ) / mean(m)
```

If n < 2, use n in place of n − 1 and flag `insufficient_periods`.

Pass criteria:
- Monthly: `|NMBE| ≤ 5%` and `CV(RMSE) ≤ 15%`.
- Daily: `|NMBE| ≤ 10%` and `CV(RMSE) ≤ 30%`.

Component checks, reported alongside:
- priced share of attempts;
- `usage_source != final` share;
- chars-per-token residual per tool class (Mode B);
- cache hit/miss classification agreement vs diagnostics.

Until the gate passes for a scope, every projection there is tagged `uncalibrated`. v0.1.2 fails this gate on the real corpus: it under-prices by 7.3%, so |NMBE| = 7.3% > 5% [ashrae-calibration-gate]. The sign convention is measured − simulated, per ASHRAE. A positive NMBE means the replay under-predicts.

### 2.9 Detector framework (`core/interfaces.py`, `core/registry.py`)

```python
class Detector(Protocol):
    detector_id: str                    # "cache.gateway_strip"
    category: str
    requires: EvidenceLevel             # minimum evidence level
    lever_id: str | None                # the optimizer lever that fixes it
    def run(self, sessions: Iterable[Session], ctx: AnalysisContext) -> Iterator[Finding]: ...

@dataclass
class AnalysisContext:
    rates: RateCard
    replay: ReplayEngine                # Mode U
    block_replay: ReplayEngine | None   # Mode B (None if no shapes)
    evidence: EvidenceRegistry          # published benchmarks with source+date (rates/evidence.py)
    config: Config
    window: tuple[float, float]
    calibrated: Mapping[str, bool]      # scope_key -> passed gate

def register_detector(cls: type[Detector]) -> type[Detector]: ...   # decorator; import-time registration
```

Rules:
- Detectors are pure: iterables in, `Finding`s out. No I/O, no global state.
- Each `Finding.savings` is computed as a **single-lever** replay difference on the finding's scope. That value is a standalone ceiling. The optimizer computes joint effects and Shapley credit; the report ranks by Shapley, never by standalone sums [shapley-attribution].
- Detectors must not emit findings under $1 or 3 occurrences in the window unless the config says so. The noise budget is Tricorder-style [channel-code-review-hooks].
- `ground_truth`: whenever `diagnostics` exist on events a detector claims, it records the counts of `diagnostics.reason`. Aggregate precision is published in the report.
- The full catalog of about 70 detectors (29 cache-breaker rows, 9 failure, 9 automation, 3 framework, 19 context/routing/compression) is in Appendix A.

### 2.10 Optimizer (`optimize/*`, WP4b)

1. **Lever catalog** (`levers.py`). Each `LeverDef` has:
   - `lever_id` (e.g. `cc.autocompact_window`);
   - `lever_class`;
   - a `grid` of candidate parameter values;
   - `to_policy(value) -> PolicySpec`;
   - `to_patch(value, cohort) -> PolicyPatch`;
   - `requires_eval`;
   - `applies(session) -> bool`.

   The catalog is in Appendix B.
2. **Candidate generation.** Levers referenced by findings, plus always-on defaults (TTL, compaction window, fast/geo) for scopes that meet `requires`.
3. **Search** (`search.py`). Coordinate descent per cohort:
   1. For each lever, pick the grid value minimizing `replay(policy_so_far ∘ value)`, subject to `allow_tradeoffs`. Trade-off levers are only proposed, never auto-selected, when `--allow-tradeoffs` is off.
   2. Repeat until there is no improvement (at most 3 sweeps).
   3. Search runs on a spend-stratified lane sample of at most 20k lanes (seeded). The **final chosen set is re-replayed on the full scope**.
4. **Shapley credit** (`shapley_run.py`, using `core/mathx.shapley`).
   - Exact over 2^k joint replays for k ≤ 10.
   - Above that, 400 seeded permutation samples.
   - Levers acting on disjoint lanes are additive: the engine only replays the interacting subset.
   - The report shows Shapley credit and a FinOps-order waterfall (usage levers, then rate levers). The sum of standalone ceilings is never shown as a total [shapley-attribution].
5. **Realization-rate intervals** (`realization.py`). Expected savings = Shapley credit × RR (p10, p50, p90) per lever class. The priors (Appendix C) come from [realization-rate-backtest] and [projection-optimism], and are replaced by the org's own receipts database (`verify/rr_db.py`) once ≥3 verified rollouts exist for a class. The update is empirical Bayes: a normal prior on RR, truncated to [−1, 1.5], with mean and variance pooled across the class's receipts and each receipt weighted by 1/CI-variance.
6. **Emitters** (`emit.py`, `cohorts.py`). They write `patches/<cohort>/`:
   - `managed-settings.json` (settings keys plus an `env` block);
   - `litellm-config.yaml` fragment;
   - `hooks/*.py` (stdlib-only scripts);
   - `README.md` with projected dollars and label per setting, risk notes, a rollback line, and `OTEL_RESOURCE_ATTRIBUTES` arm tags for verification.

   The autocompact window is emitted **both** as `autoCompactWindow` and as `env.CLAUDE_CODE_AUTO_COMPACT_WINDOW`, because the `--autocompact` flag is not preempted by managed settings [cc-managed-settings-levers].

   Cohorts come from `--cohort-by` (team or MDM group). Server-managed settings cannot target groups, so per-group rollout goes through endpoint files or MDM, or through the Claude apps gateway for IdP groups [stepped-wedge-mdm].

### 2.11 CLI surface (WP10 wires; each subcommand's logic lives in its WP's module)

Global options apply to every command:
- `--config FILE` (JSON; see `core/config.py`)
- `--log-level {warning,info,debug}`
- `--no-color`
- `--plugins` (load `tokenbill.*` entry points; **off by default**)
- `--jobs N` (process pool over sessions; default 1)

Exit codes:
- `0` ok
- `1` runtime or data error, printed as one tidy line
- `2` usage error
- `3` a gate failed (`check`, `reconcile --fail`, `calibrate --fail`, `verify` guard failure)

| Command | Arguments and flags | Owner |
|---|---|---|
| `tokenbill demo` | `[-o HTML] [--seed 7] [--scenario NAME]` (**unchanged**) `[--fleet] [--format text\|json\|html]` | v0.1 / WP10 |
| `tokenbill analyze` | `TRACE... [-o HTML] [--model-price M=IN,OUT]` (**unchanged**) `[--format text\|json] [--v2]` (`--v2` runs `scan` on the trace@1 files via the trace1 adapter) | v0.1 / WP10 |
| `tokenbill ingest` | `SRC... --from {auto,claude-code,claude-code-otel,otel-genai,trace1,trace2,anthropic-usage,anthropic-cost,claude-code-analytics,enterprise-analytics,openai-usage,litellm-spendlogs,outcomes} [--db tokenbill.sqlite] [--privacy metadata\|hashes\|content] [--hmac-key-file F] [--since DATE] [--until DATE] [--lenient] [--attr-map FILE] [--billing-path {subscription,api_key,usage_credits,bedrock,vertex,foundry,gateway}] [--source-tag TAG] [--dry-run]` | WP2/WP3 (+WP9 store) |
| `tokenbill import-claude-code` | `[--root ~/.claude/projects] [--db] [--privacy] [--hmac-key-file] [--since] [--billing-path]` (shortcut for `ingest --from claude-code`; prints the naive vs deduped self-check) | WP2 |
| `tokenbill scan` | `[SRC... --from A \| --db F] [--window 30d \| --since/--until] [--group-by team,repo,workload,model,lane_kind,harness] [--levers LIST] [--allow-tradeoffs] [--min-usd 25] [--k 5] [--rate-card FILE] [--contract FILE] [--format text\|json\|html] [-o OUT] [--emit-dir DIR] [--self-view USER_H]` | WP10 (`scan.py`) |
| `tokenbill simulate` | `--policy FILE.json [SRC... \| --db] [--scope 'team=x,lane_kind=main'] [--mode usage\|blocks] [--format]` | WP4a |
| `tokenbill optimize` | `[--db] [--levers LIST] [--allow-tradeoffs] [--cohort-by team\|mdm_group\|none] [--emit managed-settings,litellm,hooks] [--out-dir patches/] [--max-levers 10] [--seed 7]` | WP4b |
| `tokenbill ledger` | `[--db] [--window] [--group-by ...] [--basis list\|contract] [--format]` | WP9 |
| `tokenbill reconcile` | `--db F --cost-report FILE... [--usage-report FILE...] [--enterprise-analytics FILE...] [--by day,workspace,model] [--tolerance 0.01] [--fail] [--format]` | WP9 |
| `tokenbill calibrate` | `--db F [--granularity monthly\|daily] [--against billed\|invoice] [--fail] [--format]` | WP4a |
| `tokenbill pricing` | `show [--model M] [--platform P] [--at DATE]` \| `verify [--feed FILE ...] [--fetch]` \| `modelpricing --contract FILE [-o settings.json]` | WP1 |
| `tokenbill ab` | `--baseline SRC --candidate SRC [--from A] [--outcomes FILE] [--pair-key task_id] [--metric cost_per_task\|cost_per_success] [--resamples 10000] [--seed 7] [--format]` | WP8 |
| `tokenbill plan-rollout` | `--lever PATCH_DIR\|LEVER_ID --units-file FILE --unit-kind workspace\|mdm_group\|repo [--waves 4] [--wave-days 7] [--holdback 0.15] [--pre-weeks 6] [--washout-hours 24] [--projected-pct X] [--seed] [--db] [--out-dir rollout/]` | WP8 |
| `tokenbill verify` | `--plan rollout/plan.json --db F [--estimator cs\|imputation\|cuped_dim\|switchback] [--look N \| --final] [--sign-key FILE --signer ID] [--format]` | WP8 |
| `tokenbill receipt` | `verify FILE --allowed-signers FILE` \| `show FILE` | WP8 |
| `tokenbill export` | `--db F --format focus\|otlp-metrics\|csv\|json [--grain day\|hour] [--window] [-o OUT]` | WP9 |
| `tokenbill check` | `SRC... [--from A] [--min-read-share 0.80] [--after-turn 2] [--fail-on breaker,read-share,cost] [--baseline FILE] [--tolerance 0.10] [--sarif OUT]` | WP10 |
| `tokenbill compress-econ` | `--model M [--reuse N] [--ttl 5m\|1h] [--ratio r] [--rewrite-tokens X --tail-tokens S]` | WP6 |
| `tokenbill privacy-pack` | `--out-dir DIR [--jurisdiction eu,de,uk,us-ny]` | WP11 |
| `tokenbill purge` | `--db F (--before DATE \| --user USER_H)` | WP9 |
| `tokenbill --version` | | v0.1 |

### 2.12 Outputs

**`scan` JSON (`tokenbill/scan@1`, `out/schema.py`, WP10).** Every money value is `{"nano": int, "usd": "123.456789", "label": ..., "basis": ...}`.

```jsonc
{"schema":"tokenbill/scan@1","tool":{"name":"tokenbill","version":"0.2.0"},"generated_at":"...",
 "window":{"start":"...","end":"...","days":30.0},
 "privacy":{"mode":"metadata","k_min":5,"suppressed_groups":3,"hmac_key_id":"a1b2c3d4"},
 "rate_card":{"id":"anthropic_1p@2026-09-23","sha256":"...","basis":"list|contract"},
 "coverage":{"sources":[...],"sessions":N,"attempts":N,"priced_share":0.998,
   "unpriced":[{"model":"x","attempts":12}],"estimated_usage_share":0.02,
   "naive_vs_dedup_ratio":2.33,"reconcile":{"status":"pass|fail|not_run","variance_pct":0.4}},
 "calibration":{"status":"calibrated|uncalibrated|not_run","granularity":"monthly","nmbe_pct":-1.2,"cvrmse_pct":6.0},
 "ledger":{"total":Money,"by_category":{...},"by_model":{...},"by_lane_kind":{...},"by_workload":{...},
   "effective_savings_rate":{"value":0.71,"label":"exact","decomposition":{"caching":..,"batch":..,"discount":..}}},
 "kpis":{"cache_read_share":0.93,"benchmark":{"p50":0.84,"p90":0.94,"alert_below":0.80,"evidence":"anth-fleet-benchmarks"},
   "context_tax":{"reads_beyond_100k":Money,"reads_beyond_200k":Money,"reads_beyond_400k":Money},
   "context_p50_p90":[445000,869000],"cold_resume":Money,"failure_path_share":0.007,"automated_share":0.12,
   "fast_mode_premium":Money,"geo_premium":Money},
 "opportunities":[{"rank":1,"lever_id":"cc.autocompact_window","value":400000,"title":"...",
   "lever_class":"trajectory","requires_eval":true,"label":"estimated","calibrated":true,
   "ceiling":Money,"shapley":Money,"expected":{"p10":Money,"p50":Money,"p90":Money},
   "monthly_run_rate_p50":Money,"scope":{"cohort":"team=payments"},"patch":"patches/team=payments/managed-settings.json",
   "findings":["f3a9..."],"evidence":["cc-compaction-threshold-sim"],"verification":{"design":"stepped_wedge","mde_pct":13.6}}],
 "findings":[Finding...],
 "groups":[{"key":{"team":"payments"},"n_users":12,"spend":Money,"cache_read_share":0.91,"top_opportunities":[...]}],
 "patches":[{"target":"claude_code_managed_settings","cohort":"all","path":"patches/all/managed-settings.json"}],
 "warnings":["..."]}
```

**Terminal** (`out/terminal.py`). Headline first. The sentence shape is fixed:

```
Fleet spend (30 days, list, exact): $412,380.12 across 1,944 developers · priced 99.8% · calibrated (NMBE −1.2%)
Recoverable (estimated, realization-adjusted p50): $61,900/month (p10 $18,200 – p90 $98,400); ceiling $118,000
 1. Compact at 400k tokens (managed setting)            p50 $22,100/mo  [trajectory · needs eval]  patch: patches/all
 2. Delegated agents → claude-sonnet-5                  p50 $17,800/mo  [trajectory · needs eval]
 3. promptCacheTtl=1h for API-key main threads           p50  $9,400/mo  [cache transform]
 4. Gateway 'llm-proxy-2' strips cache_control           p50  $6,100/mo  [cache transform · free]
 ...
Cache read share 93% (Anthropic median 84%, top decile 94%) · context tax >200k: $96,500 (23%) · cold resumes $31,700
Labels: exact = billed×rates · estimated = replay×realization prior (not billed) · see `tokenbill verify` to prove savings
```

**HTML** (`out/html.py`). A single self-contained file:
- CSP meta `default-src 'none'; style-src 'unsafe-inline'`;
- inline SVG with a `<table>` fallback under every chart;
- dark and light themes;
- no external resources;
- C0/C1 control characters stripped [ent-report-a11y-hardening].

Sections:
1. Headline and labels legend
2. Coverage and calibration
3. Ledger waterfall (read / write-5m / write-1h / uncached / output / thinking / server tools)
4. Opportunities table with the Shapley waterfall and RR fan
5. Context tax curve (reads beyond X)
6. Gap histogram per lane kind with the TTL decision
7. Cold-resume events
8. Breaker catalog with diagnostics agreement
9. Failure-path table
10. Automation share
11. Per-team showback (k-anonymous)
12. Methodology and evidence registry (source URL and date for every benchmark)

**FOCUS export** (`ledger/focus.py`). CSV with FOCUS 1.4 columns plus the 1.5-draft `SkuPriceDetails` model properties:
- `BilledCost`, `EffectiveCost`, `ListCost`, `ContractedCost`
- `ConsumedQuantity`, `ConsumedUnit=Tokens`
- `PricingUnit`, `ServiceCategory="AI and Machine Learning"`, `ServiceName`, `SkuId`
- `ChargePeriodStart`, `ChargePeriodEnd`, `Tags`
- `x_TokenType`, `x_CacheTTL`, `x_Model`, `x_LaneKind`, `x_Workload`
- `x_TokenBillLever`, `x_RecoverableCost`, `x_RecoverableLabel`

The grain is day × team/cost_center × model × token type [ent-focus-1-4], [focus-15-ai-columns].

**OTLP metrics JSON** (`ledger/otlp_out.py`):
- `tokenbill.cost.usd` (sum, attrs: model, token_type, team)
- `tokenbill.recoverable.usd` (attrs: lever, label)
- `tokenbill.cache.read_share`

These feed Datadog, Grafana and CloudWatch collectors [ent-otel-genai-semconv].

**SARIF** (`check`). One result per breaker or regression, with ruleId = detector id.

### 2.13 Extension points

| Point | Interface | Registration |
|---|---|---|
| Adapter | `Adapter.sniff(path)->float`, `Adapter.read(paths, IngestOptions)->Iterator[Session \| UsageBucket \| InvoiceLine \| ActivityDay]` | `@register_adapter("id")`; entry point group `tokenbill.adapters` (only with `--plugins`) |
| Detector | `Detector` protocol (§2.9) | `@register_detector`; `tokenbill.detectors` |
| Lever | `LeverDef` (§2.10) | `register_lever(LeverDef)`; `tokenbill.levers` |
| Rate source | `RateCard` protocol; rate-card JSON files; contract overlay JSON (`modelPricing` shape) | `--rate-card`, `--contract`, config `rates.overlays[]` |
| Emitter | `Emitter.emit(patches, out_dir)->list[Path]` | `register_emitter("target")` |
| Output | `Formatter.render(ScanResult)->bytes` | `register_formatter("name")` |
| Convention | `(scope_name, version_range, route) -> inclusive\|exclusive\|disjoint` | `ingest/conventions.py` table |

`RateCard` protocol (WP0a defines, WP1 implements):

```python
class RateCard(Protocol):
    card_id: str; sha256: str; basis: Basis
    def resolve(self, raw_model: str, platform: Platform = "anthropic_api") -> ModelInfo | None: ...
    def price(self, attempt: Attempt) -> CostBreakdown: ...          # sums iterations when present
    def price_usage(self, model: str, usage: UsageRecord, *, platform: Platform = "anthropic_api",
                    tier: ServiceTier = "standard", speed: Speed = "standard",
                    geo: str | None = None, scope: EndpointScope = "global",
                    ts: float | None = None, ttl_hint_s: int | None = None) -> CostBreakdown: ...
    def tokenizer_factor(self, from_model: str, to_model: str) -> tuple[float, float, float]: ...
    def unit_rates(self, model: str, *, platform: Platform = "anthropic_api",
                   tier: ServiceTier = "standard", speed: Speed = "standard",
                   geo: str | None = None, scope: EndpointScope = "global",
                   ts: float | None = None) -> UnitRates | None: ...  # used by the replay hot loop

@dataclass(frozen=True, slots=True)
class UnitRates:                       # int pico-USD per token, all modifiers already applied
    uncached: int; output: int; read: int; write_5m: int; write_1h: int
    write_other: int; write_other_ttl_s: int
    row_id: str; modifiers: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class ModelInfo:
    base_id: str; provider: Provider; family: str          # "opus" | "sonnet" | "haiku" | "fable" | ...
    generation: str                                        # "5.5", "4.8", ...
    tokenizer_gen: Literal["claude_legacy", "claude_47plus", "other"]
    min_cacheable_tokens: int; read_multiplier: Decimal
    supports: frozenset[str]   # {"mid_conv_system","tool_addition","per_message_effort","fast_mode","1h_ttl",
                               #  "effort_keeps_cache","1m_context","regional_premium",...}
    successor: str | None      # this model's same-tier successor for F7 (opus-5 -> opus-5-5), None if none
```

### 2.14 Privacy and security architecture

- **Pseudonymization.** HMAC-SHA256 with an org key: 32 random bytes in a `0600` file (`--hmac-key-file` or config). EDPB guidance: pseudonymized data stays personal data, so the key is held separately [ent-privacy-by-default]. The database stores only `key_id = sha256(key)[:8]`. Without a key, a per-run ephemeral key is generated: IDs are unlinkable across runs and a warning says so.
- **Metadata mode (the default for every fleet source).** Never persisted:
  - message text, tool inputs and outputs, file paths, commands, URLs, cwd, branch names, emails, system prompts.
  - Only counts, lengths, token numbers, enums, timestamps and HMAC hashes are kept.

  Volatile-class detection (dates, UUIDs) runs locally **before** hashing, and only class names are stored [ent-cache-diagnostics-privacy], [cc-prompt-snapshot-diffing]. Tool names: built-in Claude Code tool names (Bash, Read, Edit, Write, Grep, Glob, WebFetch, WebSearch, Agent, ...) are allow-listed. MCP server and tool names are HMAC'd unless allow-listed in config.
- **Canary tests.** Every adapter test plants `TOKENBILL_CANARY_<uuid>` strings in content fields and asserts they appear in **no** output byte stream: SQLite, trace@2, JSON, HTML, terminal.
- **Hashes mode.** Like metadata mode, plus block shapes.
- **Content mode.** Local-only v0.1 behavior. A stdlib secret scanner covers provider key regexes, JWTs, PEM blocks and entropy. It redacts before any write, and the report summarizes secret *types and locations* only [ent-secrets-in-traces]. Code is treated as unredactable, so the design never needs content [ent-redaction-limits].
- **Aggregation.**
  - Team-level views require k ≥ 5 distinct `user_h`. Smaller groups are suppressed, and **complementary suppression** hides the smallest sibling when exactly one group in a total is suppressed.
  - Individuals only via `--self-view USER_H`, which outputs that user's own numbers.
  - No output ranks users; the test suite greps for user-keyed arrays.
  - No sentiment or emotion fields exist anywhere in the schema [aggregation-k5], [labor-law-constraints], [leaderboard-goodhart].
- **Output hardening.**
  - C0/C1 control characters are stripped in terminal output and SARIF.
  - HTML escaping plus CSP.
  - Files are written `0600` and directories `0700`.
  - The JSON writer refuses NaN or Infinity [cb-security-privacy], [ent-report-a11y-hardening].
- **Network.** The only paths are `ingest --fetch` (Admin, Analytics and OpenAI usage APIs) and `pricing verify --fetch`. Both are opt-in, read credentials from env vars (`ANTHROPIC_ADMIN_KEY`, `OPENAI_ADMIN_KEY`), never log them, use `urllib` with TLS verification, and are tested only against recorded fixtures [ent-anthropic-admin-apis].
- **Supply chain** (WP11). Zero runtime dependencies. SHA-pinned actions, a top-level `permissions: {}`, a CycloneDX SBOM, PEP 740 attestations, SLSA provenance, immutable releases, Scorecard [ent-supply-chain-incidents], [ent-provenance-sbom], [ent-scorecard-osps].

### 2.15 Scale and performance budgets

- **Streaming.** Adapters yield one `Session` at a time. Peak memory is bounded by the largest session, not the fleet. Mode U state per lane is O(1) beyond the request list.
- **Targets** (asserted by `tests/v2/perf`, marked `slow` and run in CI nightly):
  - ingest ≥ 20k attempts/s into SQLite;
  - Mode U replay ≥ 100k requests/s per policy (int pico rates, no per-step object allocation);
  - `scan` of 10^6 attempts with default levers ≤ 15 min at `--jobs 1` and ≤ 1.5 GB RSS;
  - a 10^5-attempt scan ≤ 90 s in default CI.
- **Why the budget works.** The optimizer searches on a sample and does the full replay once per chosen set, and Shapley replays only the lanes where levers interact.
- **Headroom.** A 1,000-developer fleet at the research's assumed ~10 sessions per developer-day is ~10^4 sessions/day [cb-scale-memory]. Metadata-mode trace@2 is O(calls), so no quadratic blow-up.

### 2.16 Backward compatibility

- `tokenbill/trace@1` files read unchanged. `demo` and `analyze` defaults are byte-identical to v0.1.2, and all v0.1 tests pass. The one exception: `tests/test_pricing.py::SPEC_TABLE` gains the new rows (WP1 edits that test, listing each added row with its source).
- `tokenbill.pricing` keeps its public API: `ModelPricing`, `PRICING`, `pricing_for`, `cost_breakdown`, `price_usd`, and the constants.
  - New rows are added: Opus 5.5, Mythos 5/5.1, Opus 4.5/4.1/4, Sonnet 4.5/4, Haiku 3.5.
  - trace@1 `Usage` has no TTL split, so `cost_breakdown` keeps pricing writes at 5m. The v2 path prices exactly.
- `Recorder(path)` keeps writing trace@1 with full content. `Recorder(path, format="v2", privacy="metadata")` and `RecorderV2` are new.
- New verbs are additive. `analyze --v2` and `demo --fleet` opt into the new pipeline.

---

## 3. Feature list for v0.2 (priority order)

### 3.0 How the dollar estimates in this section are stated

For each feature, dollars are given three ways:
1. **Corpus evidence** from the single measured Claude Code corpus: $11,632.75, one heavy user at $215 per active day [cc-heavy-tail-concentration].
2. **Published evidence.**
3. A **planning assumption** for a 2,000-developer Claude Code fleet. The Anthropic averages of $150–250 per developer per month put that fleet at **$3.6–6.0M per year** (arithmetic from [cc-enterprise-baseline]; midpoint **B ≈ $400k per month**).

Planning assumptions are this document's priors. The organization's week-1 `tokenbill scan` replaces them. They are expressed as:
- a ceiling (% of affected spend);
- times the lever class's realization-rate prior (Appendix C);
- giving an expected p50.

**Never add rows together.** The optimizer's joint replay and Shapley credit give the combined number. On the corpus, standalone ceilings overcounted the joint effect by 4.6%, and one lever's credit swung by 69% depending on order [shapley-attribution].

Priority rules:
- F1–F3 are **prerequisites**: without them every downstream dollar is wrong by 7–133% [cc-import-dedup-message-id], [cc-pricing-1h-opus55-gap].
- F4–F9 are the **core savings loop**.
- F10–F19 **broaden coverage** and make it ship-ready.

---

### F1. Exact ledger: canonical usage and rate engine v2 (WP1, with WP0a records)

**What it does.** Prices every attempt and iteration exactly, in integer nano-USD, from disjoint usage buckets and an effective-dated, sourced rate card. It handles:
- 5m vs 1h writes;
- per-model read multipliers;
- batch, flex, priority and fast tiers;
- US geo and regional endpoints;
- long-context bands;
- time-of-day pricing;
- server tools and runtime;
- contract overlays.

It reports list, contract and invoice bases separately and never blanks a total because one model is unknown.

**Evidence.** v0.1.2 misprices the real corpus by −7.3%: $758.63 from pricing 1h writes at 1.25× instead of 2×, $66.61 of unpriced Opus 5.5, and $20.86 of ignored fallback iterations [cc-pricing-1h-opus55-gap], [ashrae-calibration-gate]. Other gaps:
- top-level usage excludes compaction and advisor iterations [anth-compaction-iterations-billing], [advisor-iterations-undercount];
- one unpriced model sets org totals to `None` [cb-pricing-coverage];
- modifiers are missing [anth-modifiers-geo-fast-priority], [cb-modifiers];
- rates move quarterly [anth-evidence-drift].

**$ impact.** This is a correctness fix, not a lever: it corrects a 7.3% understatement on the corpus and up to 37.5% on the write line. Every other feature's dollars are multiplied off it.

**Algorithm and rules** (`rates/catalog.py`, `resolve.py`, `modifiers.py`, `contract.py`, `billing_rules.py`):

1. **Rate rows** (JSON; §4 lists the values). One row per `(platform, model, effective_from)`, holding:
   - input, output, cache_read, cache_write_5m, cache_write_1h;
   - optional `cache_write_other{ttl_s, rate}`;
   - `min_cacheable_tokens`, `tokenizer_gen`, family, generation;
   - `fast{input, output}`;
   - `bands[{above_tokens, input, output, cache_read, cache_write}]`;
   - `peak_windows` for DeepSeek;
   - `supports[]`;
   - and `source`, `verified` and `finding` on every row.
2. **Model id resolution** (`resolve.py`), in order:
   1. Lowercase.
   2. Strip a `[1m]` suffix.
   3. Strip the Bedrock region prefix `^(global|us|eu|apac|jp|au)\.`, then `^anthropic\.`.
   4. Strip the version suffix `-v\d+(:\d+)?$`.
   5. Strip snapshot suffixes `-\d{8}$` or `@\d{8}$`.
   6. Look up the exact id, else the base id, else `None`.

   Unknown ids return `None` and increment a counter. They are never guessed.
3. **Modifier stack.** Applied in this fixed order and recorded in `CostBreakdown.modifiers`:
   1. speed row (fast mode uses the `fast` rates; cache multipliers stay on top);
   2. per-bucket cache multipliers;
   3. tier (batch 0.5 on **all** buckets including cache [anth-batch-stacking]; flex 0.5; priority per the provider row);
   4. `inference_geo=="us"` × 1.1 on models with `generation ≥ 4.6` on `anthropic_api` / `claude_platform_aws` [anth-modifiers-geo-fast-priority];
   5. `endpoint_scope in {regional, multi_region}` × 1.1 on Bedrock/Vertex rows marked `regional_premium` [vertex-claude-geo-labels];
   6. long-context band (the whole request is re-rated when prompt tokens exceed `above_tokens`; OpenAI >272K, Gemini >200K, xAI ≥200K) [oai-batch-longctx-residency], [gemini-effective-dated-prices], [xai-cost-ticks];
   7. time-of-day (DeepSeek peak 2×, from `ts`) [deepseek-offpeak-cache];
   8. contract overlay (global multiplier, then per-model overrides, mirroring Claude Code `modelPricing`) [ent-reconciliation].

   Multiplicative stacking is documented, but its order and rounding are **not verified against invoices** [papers-measurement open question]. `reconcile` reports the residual.
4. **Iterations.**
   - If `attempt.iterations` is non-empty, price each at `iteration.model or attempt.model_served` and **ignore top-level usage**.
   - `declined_pre_output=True` prices at 0 [fp-anth-refusal-iterations].
   - A `message` iteration followed by `fallback_message` with `0 < output ≤ 16` is ambiguous: point = billed, low = 0, label `estimated`.
   - Record per-kind costs in `iteration_costs`: compaction is "context maintenance", advisor gets its own line [anth-iterations-undercount].
5. **Unknown-TTL writes.** Point = TTL hint (§2.8); low = 5m rate; high = 1h rate; label `estimated`.
6. **Server tools and runtime.**
   - Web search: $10 per 1,000 requests.
   - Code execution: container-seconds × $0.05/h, with a 5-minute minimum per container. The 1,550 free hours per org per month are applied as a monthly credit line in the ledger (WP9), not per call.
   - Managed Agents: $0.08 per session-hour when the source reports it [anth-ptc-code-exec], [anth-governance-controls].
7. **Provider-billed ground truth.** When `provider_billed_nano` exists (xAI ticks, OpenRouter cost), `price()` still computes its own figure and records the delta in `evidence`. The ledger uses the provider figure with label `exact`, basis `invoice`.
8. **Unpriced handling.** `CostBreakdown.unpriced_reason` is set and all fields are 0. Totals come out as "$X priced + N unpriced attempts across M models", never `None` [cb-pricing-coverage].
9. **Billing rules for failure modes** (`billing_rules.json`), keyed `(provider, platform, failure_mode)`. Fields: `bills_input`, `bills_partial_output`, `writes_cache`, `confidence ∈ {documented, implied, assumed}`, `source`. Rows [fp-billing-matrix]:

   | Provider / platform | Failure mode | Billing |
   |---|---|---|
   | Anthropic | pre-output refusal | $0, documented |
   | Anthropic | mid-stream refusal | input + streamed output, documented |
   | Anthropic | client abort / mid-stream overloaded | **assumed** input + streamed output, range 0..full |
   | Anthropic | pre-token 429/529 | $0, implied |
   | Anthropic | batch item errored / cancelled / expired | $0, documented |
   | Vertex | non-200 | $0, documented |
   | Gemini API | 400/500 | $0, documented |
   | Azure OpenAI | 400 content-filter, 408 | **billed**, documented |
   | Azure OpenAI | 401/429 | $0 |
   | OpenAI | flex 429 | $0 |
10. **Evidence registry** (`rates/evidence.py`, `data/evidence.json`). Each published benchmark used anywhere in the product is stored with id, value(s), unit, model, source URL, checked date and finding id. Examples:
    - Anthropic cache-read median 0.84, p90 0.94, alert below 0.80;
    - effort curves;
    - keep-alive interval of 240 s;
    - the batch cache band 0.30–0.98;
    - tokenizer factor (1.00, 1.30, 1.35);
    - thinking share prior 0.505;
    - Claude Code compaction summary median 20,283.

    Reports cite `evidence_id`. A stale check warns when `checked` is more than 90 days old [anth-evidence-drift].
11. **`pricing verify`.** Diffs card rows against user-supplied feed files (LiteLLM price map JSON, OpenRouter `/models` JSON; `--fetch` optional). It flags disagreements above 1% and exits 3. The primary source always wins, because feeds are sometimes wrong: OpenRouter listed gpt-5.6-sol at $2/$10 vs OpenAI's $4/$20 [price-feed-crosscheck].
12. **`pricing modelpricing --contract`.** Emits Claude Code's managed `modelPricing` block, so developer-facing `/usage`, OTel `cost_usd` and SDK `total_cost_usd` match contract rates [cc-list-price-estimates], [cc-anthropic-discount-visibility].

**Acceptance tests** (`tests/v2/rates/`):
- `test_opus_5_5_rates`: 1M tokens each on `claude-opus-5-5` →
  - uncached $4.000000000
  - output $20
  - write_5m $5
  - write_1h $8
  - read $0.20

  (exact nano ints).
- `test_fable_5_1_read_0_025x`: 1M read = $0.25. `test_opus_5_read_0_1x`: $0.50.
- `test_1h_vs_5m_write_sonnet_5`: 1M w1h = $4.00, 1M w5m = $2.50.
- `test_batch_halves_cache_buckets`: Opus 5.5 batch read 1M = $0.10; w1h 1M = $4.00.
- `test_geo_us_only_on_46_plus`: Opus 5.5 geo=us ×1.1; `claude-sonnet-4-5` geo=us unchanged, with a warning.
- `test_fast_mode_with_cache`: Opus 5.5 fast, 1M w5m = $10.00 ($8 × 1.25); Opus 5 fast output 1M = $50.
- `test_resolve_ids`: every one of these resolves to its base row:
  - `claude-opus-5-5[1m]`
  - `claude-haiku-4-5-20251001`
  - `claude-sonnet-4-6@20260101`
  - `anthropic.claude-opus-5-5-v1:0`
  - `us.anthropic.claude-sonnet-5-v1:0`
  - `global.anthropic.claude-opus-4-8-v1:0`
- `test_iterations_fallback_priced_per_model`: top-level equals the last iteration; the total equals the sum of iterations at their own models. The golden reproduces [cc-iterations-fallback]'s structure.
- `test_declined_pre_output_is_free`, `test_small_output_refusal_is_range`.
- `test_unknown_ttl_range`: 1M `cache_write_unknown` on Sonnet 5 → low $2.50, high $4.00, point per hint, label `estimated`.
- `test_unpriced_partial_totals`: 99 priced runs worth $198 plus 1 unknown model → total $198 plus unpriced count 1, never `None`. This is the [cb-pricing-coverage] exp4-4 scenario.
- `test_openai_long_context_band`: gpt-5.6-sol at 300K input is priced on the long-context row.
- `test_deepseek_peak_window`: ts in the Mon 07:00 UTC window gives 2×.
- `test_int_pico_exactness`: loading any card with a non-integral derived pico rate raises.
- `test_contract_overlay`: multiplier 0.8 with a per-model override → expected nano ints. `modelpricing` output validates against the documented shape (fixture).
- `test_v01_api_compat`: `tokenbill.pricing.PRICING` contains the old rows unchanged plus the new ones. The v0.1 suite passes with the updated `SPEC_TABLE`.

---

### F2. Claude Code ingestion: transcripts and OTel, content-free (WP2)

**What it does.** Turns `~/.claude/projects/**` transcripts and Claude Code OpenTelemetry exports into canonical Sessions, Lanes, Requests and Attempts, with **no content retained**. It avoids every known importer trap and prints a self-check.

**Evidence.**
- Each API call appears on about 2.4 lines. Summing per line overstates spend 2.33× ($27,094 vs $11,633) [cc-import-dedup-message-id].
- Fallback iterations [cc-iterations-fallback].
- No-stop-reason calls are complete calls whose output is under-logged, not aborts: 96.4% are followed by a tool_result, and output is under-counted by 1–2% of spend [fp-cc-missing-final-usage].
- Rollup `totalTokens` is not spend [cc-hidden-calls-rollups].
- Transcripts carry the 5m/1h split and `diagnostics.cache_miss_reason` [cc-transcript-format], [cc-miss-taxonomy-ground-truth].
- OTel is the fleet path (every provider, including Bedrock/Vertex/Foundry), but it has no TTL split without raw bodies [cc-otel-schema], [cc-enterprise-telemetry-gap].
- Claude Code is Anthropic's largest enterprise spend line, at about $13 per developer per active day [cc-enterprise-baseline].

**$ impact.** Enabler for F4–F7. It prevents a +133% overstatement and a 1–2% understatement.

**Transcript algorithm** (`ingest/claude_code.py`). Field paths were verified on 103,474 real entries (research scripts `02_schema.out`, `04_semantics.out`).

1. **Discovery.** Walk `--root` (default `~/.claude/projects`) for `*.jsonl`, excluding `journal.jsonl`. Assign lane kind by path: `/subagents/` → `subagent`, `/workflows/` → `workflow_agent`, otherwise `main`.
   - One file is one lane.
   - The session is the main file's `sessionId`. Sidechain files under `<session>/...` join that session.
   - Subagent meta JSON (`agentType`, `model`, `parentAgentId`, `toolUseId`, `spawnDepth`) and workflow-agent meta (`agentType`, `workflowPhase`) set `agent_type` and `parent_lane_id`.
2. **Line hygiene.** Skip non-dict lines. Drop duplicate `(file, uuid)` lines: 444 in the corpus.
3. **Assistant entries.**
   - Group by `message.id`. **Keep the entry with maximum `usage.output_tokens`** (ties go to the later line). `output_tokens` is monotone across split entries; the corpus had 60,394/60,394 checks pass.
   - `stop_reason` is taken from any entry where it is non-null.
   - `model == "<synthetic>"` becomes a `LaneEvent(api_error)`, not an Attempt: it is unbilled.
   - No `message.id` ever spans two files, so dedup can be per file.
4. **Timestamps.**
   - `ts_start` = timestamp of the last `user` or `attachment` entry before the first entry of this `message.id` (the request submission).
   - `ts_first_token` = first entry timestamp.
   - `ts_end` = last entry timestamp.
   - If there is no preceding entry, `ts_start = ts_first_token` and the request is flagged.
5. **Usage.** Map per §2.5 from `message.usage`: `input_tokens`, `cache_read_input_tokens`, `cache_creation.ephemeral_5m_input_tokens`, `cache_creation.ephemeral_1h_input_tokens`, `output_tokens`, `output_tokens_details.thinking_tokens`, `server_tool_use.web_search_requests`, `server_tool_use.web_fetch_requests`, `service_tier`, `speed`, `inference_geo` (`"not_available"` → None), and `iterations[]`.
   - **With >1 iteration**, iterations are the billing record. Top-level usage equals the last iteration only.
   - Set `declined_pre_output` when a `message` iteration with `output_tokens == 0` precedes a `fallback_message` [cc-iterations-fallback].
6. **Missing final usage.** No entry has a `stop_reason` **and** the next thread event is a `tool_result`:
   - `usage.source = "message_start_only"`, outcome `success`.
   - Estimated output = `visible_output_chars × r(model, lane_kind)`, where `r` is the median `output_tokens / visible_chars` over complete `tool_use` calls with the same `(model, lane_kind)` in this import. Fall back to the evidence prior of 0.9 tokens per char.
   - The logged output (p50 = 3) is kept as the low bound. Label `estimated`.
   - If a user-interrupt marker follows instead, outcome is `aborted`.
   - `visible_output_chars` is computed from the text and tool_use-input lengths **and the text is then discarded** [fp-cc-missing-final-usage].
7. **Request params.** Entry `effort` → `params.effort`; `advisorModel` → `params.advisor_model`.
8. **Attribution.** `attributionSkill`, `attributionMcpServer`, `attributionPlugin`, `attributionAgent`:
   - Skill and plugin names are allow-listed only via config.
   - MCP server names are HMAC'd.
   - `entrypoint` and `version` go to `Harness`.
   - `cwd` and `gitBranch` go to `repo_h` and `branch_h` (HMAC).
9. **Diagnostics.** `message.diagnostics.cache_miss_reason.{type, cache_missed_input_tokens}` → `CacheDiagnostics(source="claude_code_transcript")`.
10. **Events.**
    - `message.context_management.applied_edits` → `LaneEvent(context_edit)` with cleared tokens.
    - `message.input_transformations[type=thinking_dropped]` → `LaneEvent(thinking_dropped)`.
11. **System entries.**
    - `compact_boundary.compactMetadata{preTokens, postTokens, durationMs, trigger}` → `LaneEvent(compaction)`.
    - `model_refusal_fallback{originalModel, fallbackModel}` → `LaneEvent(model_fallback)`.
    - `api_error{status, retryAttempt, maxRetries, retryInMs}` → an Attempt with `outcome=error`, `usage=None`, `retry_layer="agent"`, prepended to the next request's attempt chain.
12. **User entries.**
    - Each `tool_result` block becomes a `ToolResultMeta`: tool name via the tool_use id map, char length, image count, `is_error`, `input_h` = HMAC(tool name + canonical input JSON), `result_h` = HMAC(content). It attaches to the next request.
    - Human turns are `origin.kind=="human"`, or not meta / compact summary / sidechain / tool_result. They set `human_turn` on the next request.
    - Interrupt markers become `LaneEvent(user_interrupt)`: detected by prefix match, then the text is discarded.
13. **Attachment entries** → `LaneEvent(attachment, detail={type, chars, count})` for `skill_listing`, `deferred_tools_delta`, `mcp_instructions_delta`, `task_reminder`, `total_tokens_reminder`, `environment`, `date`. `prompt_snapshot` becomes `LaneEvent(prompt_snapshot, detail={system_h, tools_h, tools_n, tools_chars})`: the HMAC of `systemPrompt` and `tools` [cc-prompt-snapshot-diffing], [cc-config-sprawl-tax].
14. **Never used:** `toolUseResult.totalTokens` and workflow JSON `totalTokens`. These are final-context rollups, 25× below processed tokens [cc-hidden-calls-rollups].
15. **Hidden calls.** Each `compact_boundary` implies an unlogged compaction request. Book an estimated Attempt (`usage.source="estimated"`): `pre_tokens` read at the read rate if the previous request was within TTL, else written, plus `post_tokens` of output. `cost-state` entries (`totalCostUSD`) are used only for the coverage self-check.
16. **Harness and billing path.** `billing_path` comes from `--billing-path` or config (default `unknown`). This matters because TTL defaults depend on it: 1h for the subscription main thread, 5m for API key, cloud or credits [cc-ttl-policy].
17. **Self-check report.** Lines vs unique `message.id`; naive per-line $ vs deduped $; the share of `message_start_only` usage; estimated hidden-call $; unknown entry types (warn if >1% of lines); Claude Code version histogram.

**OTel algorithm** (`ingest/claude_code_otel.py`).
- Input: OTLP JSON (collector `fileexporter` lines with `resourceLogs`, `resourceMetrics` or `resourceSpans`; gzip allowed).
- Each `claude_code.api_request` log record becomes one Attempt:
  - `ts_start = time − duration_ms/1000`;
  - `model`, `request_id`, `client_request_id` (the logical request key), `attempt`, `success`, `speed`, `effort`, `query_source` (lane kind `main`, `subagent` or `auxiliary`);
  - tokens `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, with writes going to `cache_write_unknown`;
  - `cost_usd` kept only as `evidence["harness_estimate_usd"]`.
- `claude_code.api_error` becomes a failed attempt.
- Beta traces: `claude_code.llm_request` spans give `ttft_ms`, `agent_id` and `parent_agent_id` (lane tree); `claude_code.tool` spans give `result_tokens`, which become `ToolResultMeta` [cc-otel-schema].
- **Opt-in** `api_response_body` events: parse **only** `usage` (TTL split, iterations, diagnostics); the body is dropped unread beyond `usage` [cc-enterprise-telemetry-gap].
- Resource attributes: `session.id`, `user.account_uuid` / `user.id` / `user.email` (HMAC'd; email is never stored), `organization.id`, plus configured `OTEL_RESOURCE_ATTRIBUTES` keys (`team.id`, `cost_center`, `tokenbill.arm`, `tokenbill.wave`, `workload`) → `Attribution`.
- Metrics (`claude_code.token.usage`, `claude_code.cost.usage`) become `UsageBucket`s. They are used only for coverage when events are absent.

**Acceptance tests** (`tests/v2/ingest_cc/`, golden fixtures written by hand to mirror the verified schema):
- `test_split_entries_dedup`: one `message.id` on 3 lines with outputs 3, 150, 470 → one Attempt with output 470. `naive_vs_dedup_ratio` is reported.
- `test_duplicate_uuid_dropped`.
- `test_ttl_split_priced`: `ephemeral_1h` becomes `cache_write_1h`. A missing split becomes `cache_write_unknown`.
- `test_iterations_fallback_billing`: a 2-iteration fixture (fable-5 `message` with output 2127, then opus-4-8 `fallback_message`) → both priced. A 0-output first iteration is priced at 0.
- `test_message_start_only_estimated`: no stop_reason and next event tool_result → `source="message_start_only"`, estimated output > logged, outcome `success`.
- `test_compact_boundary_event_and_hidden_call`: `compaction` LaneEvent with pre/post, plus one estimated hidden Attempt.
- `test_api_error_attempt_chain`: `api_error` (529, retryAttempt 3) → failed attempts precede the next billed attempt in the same Request.
- `test_rollup_totaltokens_ignored`: a fixture with `toolUseResult.totalTokens` changes nothing in the ledger.
- `test_privacy_canary`: canaries in message text, `tool_result` content, `cwd`, `gitBranch`, file paths, `systemPrompt` and emails → **zero occurrences** in the SQLite bytes, trace@2 bytes, scan JSON and HTML.
- `test_otel_api_request_mapping`: fixture OTLP JSON → Attempts with `cache_write_unknown`, the lane kind from `query_source`, `ts_start` from duration, and attribution from resource attributes.
- `test_otel_response_body_usage_only`: a response-body event gives the TTL split. A canary in its content never appears.
- `test_corpus_shape_regression` (skipped unless `TOKENBILL_CC_CORPUS` is set): on a real local corpus, the deduped attempt count equals the number of unique `message.id`s (excluding synthetic), and the naive/dedup ratio is > 1.5.

---

### F3. Counterfactual replay engine (Mode U) and calibration gate (WP4a)

**What it does.** Implements §2.8 Mode U and the calibration gate. It is the engine every lever and finding uses.

**Evidence.**
- The corpus levers were computed exactly this way [cc-compaction-threshold-sim], [cc-cold-resume], [cc-ttl-advisor].
- The simulator must pass NMBE ±5% and CV(RMSE) ≤15% monthly before projections count [ashrae-calibration-gate].
- Model error must cancel in differences [mv-adjusted-baseline].
- v0.1's O(n²) replay does not scale [cb-scale-memory].

**$ impact.** Enabler. Prevents about 7% systematic misstatement and labels uncalibrated projections.

**Algorithm.** As §2.8, plus:
- `simulate` CLI: `--policy` JSON is the `PolicySpec` serialization.
- `calibrate` CLI: NMBE/CV(RMSE) per org×month and workspace×month (monthly gate) and day (daily gate), plus component checks.
- The engine exposes `replay(lanes, policy, ctx) -> ReplayResult` and `ceiling(lanes, policy, ctx) -> SavingsEstimate` (§2.4).

**Acceptance tests** (`tests/v2/sim/`; synthetic lanes from `core.synth` with `core.testing.FlatRates`: input $1/MTok, output $5, read ×0.1, w5m ×1.25, w1h ×2):
- `test_as_is_identity`: lanes generated by `synth.healthy_loop(ttl=300)` → `replay(AS_IS)` equals billed to the nano.
- `test_ttl_closed_form`:
  - Lane: 3 requests at ctx 100k, gaps [60 s, 600 s], each appending 1k tokens, 500 output tokens.
  - 5m cost = (100k·w5)+(100k·r + 1k·w5)+((101k+1k)·w5) + outputs.
  - 1h cost uses w1h and a warm third call.
  - The test asserts both numbers exactly.
- `test_keepalive_break_even`: `I_max(interval=240, w=1.25, r=0.1) == 2760 s` (46 min); r=0.05 → 5760 s; r=0.025 → 11760 s [keepalive-economics]. The replay with keep-alive for a 30-minute gap costs `floor(1800/240)=7` reads of the context.
- `test_compaction_window_closed_form`: a lane growing +20k per call to 960k, compaction at 400k with S=20k → cost equals the hand formula, `added_calls` = the expected count. A window of 1M makes zero changes.
- `test_cold_resume_compact`: ctx 500k, gap 7200 s, min_ctx 200k → one summarization (uncached 500k + S output), then S + new tokens written.
- `test_observed_reset_respected`: an observed ctx drop >50% resets `removed`.
- `test_model_map_switch_forces_cold` and `test_tokenizer_band` (low/point/high differ by the factor).
- `test_fast_off_and_geo_global_exact` (label exact).
- `test_stagger_fanout`: 5 lane-first requests within 2 s, shared prefix observed at 40k → 4 × 40k converted from w5m to reads.
- `test_calibration_gate`: hand-computed NMBE/CV(RMSE) on 6 monthly pairs. Pass/fail thresholds at ±5%/15% and ±10%/30%. n=1 is flagged.
- `test_linear_time`: 200k synthetic requests replay in < 3 s (CI budget), and doubling n at most doubles time ± 30%.

---

### F4. Context-size governor: `autoCompactWindow` simulator and managed-settings patch (WP6 detector, WP4b lever)

**What it does.**
- Measures the **context tax**: dollars spent re-reading prompt tokens beyond 100k, 200k and 400k, per team and lane kind, plus context p50/p90.
- Replays each Claude Code main lane under compaction windows {150k, 200k, 300k, 400k, 500k, 700k}.
- Recommends the cheapest window that passes the risk guard, per cohort, and emits the managed setting with projected dollars.

**Evidence.**
- Context size, not cache misses, drives cost: 97% hit ratio yet reads are 55% of spend; ≥400k-context calls are 18% of calls and 46% of dollars; reads beyond 200k are 23.2% of the bill [cc-context-size-driver].
- Replayed compaction at 700k/500k/400k/300k/200k saves 18.4/23.3/29.8/33.3/37.7% of the total bill, price-only, excluding re-work [cc-compaction-threshold-sim].
- The default is ~967K on 1M-context models. It is adjustable via `autoCompactWindow` (100K–1M) or `CLAUDE_CODE_AUTO_COMPACT_WINDOW` [cc-autocompact-window].
- Uber runs auto-compaction at 400K [uber-policy-arc].
- Long context degrades quality too [context-rot], [quality-positive].
- Compaction loses artifact details [compaction-quality-tokens-per-task], and hidden re-acquisition can erase savings [hidden-reacquisition].

**$ impact.**
- Corpus ceiling: 18–30% of the total bill (upper bound).
- Planning assumption for the fleet: ceiling 6–15% of Claude Code spend, since the average developer runs smaller contexts than the corpus user. With the trajectory RR prior (−0.2, 0.5, 0.9), p50 is 3–7.5%, i.e. **about $12–30k per month on B**.
- This is the single largest lever in the evidence.

**Algorithm.**
1. **Context tax (label `exact`; arithmetic on billed tokens).** For each request:
   - `beyond_X_read = max(0, cache_read − X) × read_rate`
   - `beyond_X_write = max(0, min(cache_write, ctx − X)) × write_rate` when `ctx > X`

   Aggregate by group. Report context p50/p90 per lane kind [cc-context-size-driver].
2. **Eligibility.** Claude Code harness; lane kind in {main} by default (workflow agents optional: `--include-agents`); model with a ≥1M window (rates `supports:"1m_context"`); `max(ctx) > window`.
3. **Summary size S.** The org's median `postTokens` from `compaction` events; fallback 20,283 [cc-compaction-threshold-sim].
4. **Replay.** `CompactionPolicy(window, S)` per §2.8 step 5 (warm compaction reads, cold rewrites) → ceiling per window.
5. **Choosing a window.** Pick the largest-savings window whose risk guard passes:
   - **Guard A:** projected extra compactions per session ≤ `config.max_compactions_per_session` (default 3).
   - **Guard B:** if post-compaction re-acquisition data exists (repeat `input_h` of Read/WebFetch within 20 requests after a compaction vs before), the re-read increase stays ≤ 15% [compaction-quality-tokens-per-task].

   The report shows the full window curve, not just the pick.
6. **Patch.** `{"autoCompactWindow": N, "env": {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "N"}}`. The env copy is required because `--autocompact` is not preempted by managed settings [cc-managed-settings-levers].
   - Cohort patches go by team or MDM group.
   - The optional alternative `CLAUDE_CODE_DISABLE_1M_CONTEXT=1` is replayed and priced as its own candidate.
7. **Labels.** Lever class `trajectory`, `requires_eval=true`, with this verification design attached: stepped wedge by MDM group; primary metric cost per active dev-day; guardrails merged PRs per dev-day and revert rate (F9).

**Acceptance tests** (`tests/v2/detect/test_context.py`):
- `test_context_tax_exact`: hand-computed beyond-100k/200k/400k $ on a 3-request fixture.
- `test_governor_picks_curve`: `synth.growing_context(to=960k)` with 10 sessions → the window curve is monotone, the chosen window equals the closed-form argmin under guards, and the finding `lever_id` is `cc.autocompact_window`.
- `test_no_finding_below_window`: sessions whose max ctx < 150k produce no finding.
- `test_patch_has_setting_and_env`: the emitted JSON has both keys, the value is within the 100k–1M range, and cohort files exist per team.
- `test_label_and_rr`: `savings.label=="estimated"`, `lever_class=="trajectory"`, `rr == evidence prior`.

---

### F5. Cold-resume advisor and SessionStart nudge (WP6 detector, WP4b lever, WP4b hook emitter)

**What it does.**
- Detects and prices every return to a large context after the cache expired: the re-write premium over a warm read.
- Aggregates the cost by team; each developer sees their own cost only.
- Ships a **SessionStart hook** that warns at the decision point, using Claude Code's resume-cost fields, and recommends `/compact` (same task) or `/clear` (new task).
- Replays "summarize on cold resume" for the ceiling.

**Evidence.**
- 128 main-thread resumes after >1h idle (median gap 7.0h) re-wrote a median 568k-token context: $924, **7.95% of the bill**, $7.22 per event.
- The summarize-on-resume replay gives −51% on main and −29% on total, overlapping with F4 [cc-cold-resume].
- Idle >1h is 52.5% of all miss premium [cc-miss-taxonomy-ground-truth].
- `/compact` after a break is the most expensive case [compaction-timing].
- SessionStart hooks receive `context_tokens`, `prompt_cache_likely_expired` and `estimated_cache_write_usd` [channel-code-review-hooks].
- Nudges must be at the decision point and end in a one-click action [nudge-decay-durability].

**$ impact.**
- Corpus: 7.95% of the bill direct, up to 29% with summarization (overlapping F4).
- Planning ceiling: 2–6% of Claude Code spend.
- The hook is a behavioral lever (RR prior 0.05/0.2/0.4), so p50 is about 0.4–1.2%, **~$2–5k per month on B**. The ceiling is realized fully only if Claude Code adds auto-summarize-on-resume. That is noted as an upstream feature request with the dollar figure attached.

**Algorithm.**
1. **Event rule.** Lane kind main; `gap > ttl_eff`, where `ttl_eff` = 3600 if the lane billed 1h writes, else 300 (or the hint); billed `cache_write ≥ 0.5 × prev_ctx`; `prev_ctx ≥ 100k`. Cross-check against `diagnostics.reason=="previous_message_not_found"`, which is expected on 100% of >1h events in the corpus [cc-cold-resume].
2. **Premium.** `rewritten = min(cw, prev_ctx)` and `premium = rewritten × (write_rate − read_rate)`. Label `exact`: it is arithmetic on billed tokens relative to a warm read. Gap distribution and events per developer (self-view).
3. **Ceiling.** Replay with `ColdResumePolicy(action="compact", min_context_tokens=200k)` and also `action="clear"`.
4. **Hook** (`optimize/emit.py` copies `optimize/templates/tokenbill_session_start.py` to `patches/<cohort>/hooks/`; stdlib only):
   - Read hook JSON from stdin.
   - If `prompt_cache_likely_expired` and `estimated_cache_write_usd ≥ threshold` (config, default 1.00), print JSON `{"systemMessage": "..."}` naming the dollar cost and suggesting `/compact` or `/clear`.
   - Never block, never exit non-zero, never log content.
   - Include a frequency cap: at most 1 interruptive nudge per session, ≤3 per person per week, via a local counter file [nudge-decay-durability].
5. **Enhancers.** The TTL advisor (F6) will often remove most sub-hour events. Shapley accounts for the overlap.

**Acceptance tests.**
- `test_cold_resume_event_rule`: a fixture with gaps [30 s, 2 h, 90 s] at 500k ctx gives exactly 1 event, and the premium equals the formula.
- `test_ttl_aware`: the same gap of 30 min on a 1h lane gives no event; on a 5m lane it gives an event.
- `test_hook_script`: run the hook as a subprocess:
  - input with likely_expired=true and estimated $3.20 → valid JSON with `systemMessage` containing "$3.20";
  - below threshold → empty output;
  - malformed input → empty output, exit 0.
- `test_hook_frequency_cap`.
- `test_self_view_only`: output keyed by user only when `--self-view` matches; team output requires k≥5.

---

### F6. TTL advisor: 5m / 1h / keep-alive per lane kind and cohort (WP6 detector, WP4b lever)

**What it does.**
- Builds each cohort's start-to-start gap histogram per lane kind (main, subagent, workflow agent, CI, scheduled).
- Replays 5m, 1h and keep-alive.
- Recommends the cheapest per `(cohort, lane kind)` and emits `promptCacheTtl` / `subagentPromptCacheTtl` or the env equivalents with dollar deltas.
- Flags 1h writes wasted on short bursts, and TTL loss caused by the gateway path.

**Evidence.**
- Main-thread gaps: 88.9% under 1 minute, 2.9% in 5–60 minutes, 1.2% over 60 minutes. All-5m costs +11.8% on main vs as-billed 1h; 5m with keep-alive costs +5.5%. Subagents and workflow agents would pay +17–18% under 1h [cc-ttl-advisor].
- Defaults: 5m on API key, cloud and usage credits; 1h on subscription main threads [cc-ttl-policy].
- Anthropic's rule of thumb: 1h when more than 1 in 20 gaps fall in 5–60 minutes. With no pauses, 5m is 15% cheaper on Sonnet 5 and 11% on Opus 5 [anth-fleet-benchmarks].
- Keep-alive pays up to `I_max = τ(w/r − 1)`: 46/96/196 minutes on 0.1/0.05/0.025 read multipliers [keepalive-economics].
- The 1h TTL is unavailable through the Claude apps gateway [cc-apps-gateway-routing-tax] and needs the `anthropic-beta` header forwarded [cc-gateway-cache-strip].

**$ impact.**
- Corpus: 10–12% of main-thread spend for 5m-default fleets.
- Planning ceiling: 3–8% of Claude Code spend on API-key or cloud fleets, ~0–2% on subscription fleets.
- Class `cache_transform` (RR 0.8/0.9/1.0) → p50 **~$10–29k per month on B** if the fleet is on API keys or cloud.
- Verifiable at small fleet sizes via switchback-free cluster designs.

**Algorithm.**
1. **Gap buckets.** Per lane kind and cohort: [0,1m), [1,5m), [5,10m), [10,30m), [30,60m), ≥60m.
2. **Replay.** For each `(cohort, lane kind)`, replay `TtlPolicy` with {5m, 1h, keepalive(240 s, I_max(model))}. `I_max` is computed per model from the rate card.
   - Keep-alive is offered only to SDK/custom agents, as an SDK snippet: `max_tokens: 0` is rejected with stream, structured outputs, forced tool_choice and in batches [anth-ttl-choice-keepalive].
   - Claude Code gets TTL settings only.
3. **Hindsight bound.** Report the per-transition oracle as a "hindsight bound; mechanism unverified" and never as a recommendation [cc-ttl-advisor].
4. **Recommendation.** The argmin per `(cohort, lane kind)`, requiring savings ≥ max($25/month, 2%). Emit:
   - `{"promptCacheTtl": "1h"}` for main and/or `{"subagentPromptCacheTtl": "5m"}`;
   - equivalent env for gateway fleets (`CLAUDE_CODE_PROMPT_CACHE_TTL`, `ENABLE_PROMPT_CACHING_1H`, `FORCE_PROMPT_CACHING_5M`);
   - a gateway checklist line: "forward `anthropic-beta` unchanged".
5. **Wasted 1h.** Lanes whose 1h writes were never read after 5 minutes: `premium = w1h_tokens × (2.0 − 1.25) × input_rate` [write-without-read].
6. **Post-rollout check** (`verify` guard). The fraction of `cache_write_1h` among main-lane writes rises above 90% within one week, otherwise the finding reads "setting not effective (gateway strips beta header?)" [cc-ttl-managed-settings].

**Acceptance tests.**
- `test_gap_histogram` on a known gap list.
- `test_ttl_replay_choices`: bursty lanes (all gaps < 60 s) choose 5m; lanes with 10% of gaps in 10–30 minutes choose 1h; the dollar deltas match closed form.
- `test_imax_by_model`.
- `test_keepalive_not_for_claude_code`: a Claude Code harness never gets a keep-alive patch.
- `test_patch_keys` (JSON validated against the known-key list in §4).
- `test_wasted_1h_premium`.
- `test_gateway_ttl_warning` when the harness path is `gateway` and 1h is recommended.

---

### F7. Model, effort and tier routing, priced per solved task; premium drift (WP6)

**What it does.**
- **Reprices identical tokens.** Same-tier upgrades: Fable 5→5.1, Opus 5→5.5, and Opus 4.x→5.5 with a tokenizer band. Delegated lanes to cheaper models.
- **Measures model and effort mix.** Spend by effort, thinking share, xhigh/max concentration, and cost per solved task where outcomes exist.
- **Detects premium drift and sticky escalations.** Fast mode, US geo, regional endpoints, and `/effort` or fast-mode settings that persisted across sessions.
- **Detects truncation waste.**

Every trade-off lever is `requires_eval`.

**Evidence.**
- Delegated agents are 44% of spend and inherit Opus/Fable. Workflow agents → Sonnet 5 saves 25.5%, subagents → Sonnet 5 4.4% (price-only) [cc-delegation-model-routing].
- Same-tier repricing: Fable 5→5.1 −21.0%, Opus 5→5.5 −11.7% of the bill [cc-same-tier-upgrade].
- xhigh drives 69% of spend. Halving thinking ≈ −3.6% [cc-output-thinking-effort].
- Effort: medium −50% for about −2 pts on Opus 5 coding; low+re-run failures $0.45 vs $0.93 at the same pass rate [anth-effort-sweep], [anth-rerun-failures].
- Datadog: $687k/month (model default) plus $288k/month (effort) [org-defaults-datadog].
- Sticky `/effort` and fast mode [cc-sticky-escalation].
- Premiums: fast 2×, geo 1.1×, regional +10% [anth-modifiers-geo-fast-priority], [premium-modifiers].
- Per solved task, not per token [anth-cost-per-solved-task], [cost-of-pass-metric].
- Routers must net out cache loss [cache-aware-routing], [routers-vs-cache-net-savings].
- 16K caps waste attempts [max-tokens-truncation].

**$ impact.**
- Corpus: 12–31% of the bill (price-only).
- Datadog: 36.7% from the model default alone.
- Planning ceiling: 10–30%, trajectory RR → p50 5–15%, **~$20–60k per month on B**. That makes it the largest planning lever, but it is gated by eval.
- Fast and geo premiums are rate-class and exact once enabled.

**Algorithm.**
1. **Same-tier reprice** (`routing.same_tier`).
   - For each model in use with a successor in the rate card (the row's `successor` field), reprice the lane's billed tokens on the successor.
   - `tokenizer_factor` is (1, 1, 1) when both share `tokenizer_gen`, otherwise the band.
   - Price arithmetic is `exact` on identical tokens. The realized saving is `estimated`: behavior may change tokens.
   - The fix links to the migration checklist and the prompt-audit guidance [anth-prompt-audit-migration].
2. **Delegation routing** (`routing.delegation`).
   - Per `agent_type` × lane kind: spend on premium families (opus, fable, mythos).
   - Candidates: {claude-sonnet-5, claude-haiku-4-5, claude-opus-5-5}.
   - `ModelMapPolicy(apply at lane start)` replay → ceiling.
   - Patch: `env.CLAUDE_CODE_SUBAGENT_MODEL`, or a per-agent `model:` frontmatter note. Explore inherits the session model since v2.1.198 [cc-subagents-teams-workflows].
3. **Effort mix** (`routing.effort`).
   - Spend and thinking share by `params.effort`; xhigh/max share per team.
   - Lever `cc.max_effort` ∈ {high, medium} with `thinking_scale` from the evidence registry (point 0.5 for medium relative to high on Opus 5 coding; band 0.3–0.8).
   - Patch: `{"maxEffortLevel": "high"}`, which needs v2.1.267+ [cc-managed-settings-levers].
   - Effort changes invalidate the messages cache except on Opus 5.5 and Fable 5.1 via API key or subscription [cc-cache-breakers]. The replay forces a cold start at the change point when the rate card says the model does not keep the cache.
4. **Sticky escalation** (`routing.sticky`).
   - Per `user_h`: count the days where ≥80% of the user's requests ran at an effort above the cohort's modal effort, or at `speed=fast`.
   - Flag if that is ≥7 of the last 10 active days **and** the task mix is small: median of Edit/Write tool calls ≤ 5 per session, or ActivityDay `lines_added` ≤ 50.
   - Self-view only; the team aggregate is k-anonymous.
   - Patch: `{"fastModePerSessionOptIn": true}`, plus a per-role effort cap [cc-sticky-escalation].
5. **Fast premium** (exact).
   - `Σ(price(attempt) − price(attempt with speed=standard))`, split by workload.
   - Fast mode on non-interactive workloads is flagged `high` confidence.
   - Fast/standard flapping within a lane is a cache breaker, reported under F10 as `cache.param_churn`.
   - Patch: `fastModePerSessionOptIn: true` or `CLAUDE_CODE_DISABLE_FAST_MODE=1`.
6. **Geo and regional premium** (exact). `Σ(price − price with geo=None, scope=global)`. It is a recommendation only if `config.residency_required` is false for that cohort. The default is to ask: the report shows the dollars as "policy decision".
7. **Cost per solved task** (`routing.cost_per_pass`). When `SessionOutcome.success` exists:
   - cost per success = total cost of all sessions (failures included) ÷ successes, by model × effort, with bootstrap CIs (F9 stats);
   - a Pareto table;
   - "low effort then re-run failures" projection for lanes with a checker, using the evidence prior (cost ratio 0.48, pass rate unchanged), label `estimated`, `requires_eval`.
8. **Truncation waste.** Attempts with `stop_reason=="max_tokens"` plus their immediate retries are priced as waste. Recommend a 64K cap, or 128K at xhigh/max [max-tokens-truncation].
9. **Router audit.** Served-model changes within a lane without a human turn or compaction are router switches. Net savings = model price delta − `cache_rebuild` (the lane replay with the switch vs without) [routers-vs-cache-net-savings].

**Acceptance tests.**
- `test_same_tier_reprice_exact`: 1M read tokens on fable-5 = $1.00 → fable-5-1 = $0.25; the corpus-like fixture's delta matches.
- `test_tokenizer_band_cross_gen`.
- `test_delegation_patch_env`.
- `test_effort_change_cold_on_opus5_not_opus55`.
- `test_sticky_escalation_rule` (7/10 days, small diffs) and `test_sticky_is_self_view_only`.
- `test_fast_premium_exact`: fast Opus 5.5 1M output = $40, standard $20 → premium $20.
- `test_geo_premium_requires_policy`.
- `test_cost_per_success_includes_failures`.
- `test_truncation_waste`.
- `test_router_switch_net_negative`: a router that saves 10% on price but switches every 3rd turn on a 200k context is net negative.

---

### F8. Policy optimizer: joint replay, Shapley credit, realization intervals, deployable patches (WP4b)

**What it does.** Implements §2.10:
1. Turns findings plus default levers into a per-cohort `PolicySpec` via coordinate search on joint replays.
2. Assigns Shapley credit.
3. Applies realization-rate intervals.
4. Emits deployable patches with a README per cohort.

This is the "counterfactual policy simulator that searches policies and emits deployable config".

**Evidence.**
- Defaults beat nudges [org-defaults-datadog], [defaults-beat-nudges-meta].
- A policy pack is the right output [cc-managed-settings-levers].
- Shapley is required to avoid overlap overcount [shapley-attribution].
- Projections are optimistic [projection-optimism]; RR intervals [realization-rate-backtest].
- LiteLLM injection points fix caching for every LiteLLM-routed framework [litellm-gateway-injection].

**$ impact.** This is the delivery mechanism for F4–F7 and F10–F13. Its job is to make the combined number honest, e.g. 4.6% lower than the naive sum on the corpus.

**Algorithm.** §2.10 steps 1–6, plus:
- **Stop rule.** Levers with Shapley p50 below $25/month are dropped from patches but kept in the JSON.
- **Conflict rules.** A cohort gets one TTL choice per lane kind. Model-map and effort compose (effort applies to the mapped model). `restore_caching` and `breakpoints` compose.
- **Determinism.** The same inputs and `--seed` give byte-identical patches and JSON.
- **Patch validation.** Every emitted key is in the known-key table (§4.6). Unknown keys raise.
- **Patch README** per cohort: each setting, its Shapley p50/p10/p90, label, `requires_eval`, the verification design suggested by `plan-rollout`'s MDE preview (F9), and a rollback line (the previous value).
- **LiteLLM emitter.** For gateway lanes with `restore_caching` or TTL changes:

  ```yaml
  model_list:
    - model_name: <name>
      litellm_params:
        model: <provider/model>
        cache_control_injection_points:
          - {location: message, role: system}
          - {location: message, index: -1}
  ```

  plus a comment carrying the TTL recommendation. Key names are **VERIFY** (§4.6).

**Acceptance tests.**
- `test_shapley_exact_3_levers`: a synthetic value function with a known interaction (L1 and L2 overlap on the same lanes) → Shapley values match hand arithmetic, and their sum equals the joint effect.
- `test_standalone_sum_not_reported`: the JSON has no field summing standalone ceilings.
- `test_coordinate_search_finds_known_optimum` on a planted fleet (compaction 400k, TTL 1h for team A, 5m for team B).
- `test_tradeoffs_gated`: without `--allow-tradeoffs`, model/effort levers appear only as `proposed`.
- `test_patch_determinism` (two runs byte-identical).
- `test_patch_known_keys`.
- `test_rr_interval_math`: ceiling $1,000 × (−0.2, 0.5, 0.9) → (−$200, $500, $900).
- `test_sample_then_full_replay`: the final numbers come from the full scope, not the sample.

---

### F9. Savings verification: lab A/B, rollout planning, causal verification, signed receipts (WP8)

**What it does.** Proves which dollars actually came off the bill:
- `tokenbill ab` for paired lab campaigns, e.g. judging a compressor or a prompt change;
- `plan-rollout` for randomized stepped-wedge / cluster designs with pre-registration;
- `verify` for CS/imputation DiD at constant prices with CUPED, guards and labels;
- `receipt` for DSSE-signed, OpenSSH-verifiable receipts;
- an RR database that feeds F8.

**Evidence.**
- Token reduction ≠ bill reduction, and the sign can flip (+6.8%, +7.6%) [token-not-cost], [verified-savings-gap-rtk], [llm-token-not-bill].
- IPMVP/FEMP framing [mv-adjusted-baseline].
- Staggered DiD estimator results: naive pre/post is +16.5% or −31% wrong, TWFE −9%, CS −3 to −4%, imputation −1.8% [staggered-did].
- Targeting the top spenders inflates the estimate by +17–34% [rtm-targeting].
- Cluster at the cache boundary: individual randomization is biased −28% [cache-interference-cluster].
- CUPED cuts variance by 88% [cuped].
- MDE is ~13.6% at 1,000 devs and ~4.5% at 10,000 [heavy-tails-power].
- Peeking triples false positives; confidence sequences fix it [sequential-monitoring].
- Switchback carryover [switchback-carryover]; SRM [assignment-telemetry-srm].
- Signed receipts [signed-receipts]; long holdback [long-run-holdback].
- Quality guardrails [value-adjusted-cost-metric], [quality-guardrail].
- Units: cost per active dev-day [units-gaming].
- Eval sizing: ~50 cases × 5 trials [eval-statistics].

**$ impact.** Prevents paying for negative-ROI levers (−7% observed) and 1.5–4× overclaims. It turns "estimated" into "verified" for finance.

**Algorithms** (all stdlib; `verify/*`):

**`ab`: paired lab comparison** (`ab.py`).
1. **Inputs.** Two session sets (baseline, candidate), each with `outcome.task_id`, and optionally success.
2. **Unit costs.** Cost per session at a **constant rate card** (the baseline's card hash).
3. **Pairing.** By `task_id`. Each task has trials `a_1..a_n` and `b_1..b_m`.
4. **Statistic.** `Δ% = Σ_t mean(b_t) / Σ_t mean(a_t) − 1` (ratio of task-mean sums). With `--metric cost_per_success`: `Σ cost / Σ successes` per arm, failures included in the numerator.
5. **Confidence interval.** Task-clustered bootstrap: resample tasks with replacement, keep each task's trials intact, B = `--resamples` (default 10,000), seeded; percentile 95% CI [llm-token-not-bill].
6. **Wilcoxon signed-rank** on per-task mean differences: normal approximation with tie correction; p-value reported.
7. **Drift diagnostics.** Turns per task, tool calls per task, re-read rate (repeat `input_h`), cache-read share, with bootstrap CIs. Warn when turns rise >10% [hidden-reacquisition].
8. **Power.** Warn if `tasks < 50` or `trials < 5` per arm [eval-statistics]. Print a realized noise floor ≈ `1/sqrt(n·R)` [eval-stats-trials].
9. **Label.** `verified` at scope `lab:<task-set>` if trials were randomized in order (flag `--randomized`) and the CI excludes 0; otherwise `measured`. The fleet projection is always `estimated`.

**`plan-rollout`** (`rollout.py`).
1. **Units** from `--units-file`: workspace, MDM group or repo; must be the cache-isolation boundary [cache-interference-cluster]. Refuse `user` units for cache or prefix levers.
2. **Randomized wave order** (seeded; `random.Random(sha256(seed|plan))`), `--waves` waves `--wave-days` apart, and a `--holdback` never-treated share (default 15%, allowed range 10–25%) [stepped-wedge-mdm], [long-run-holdback].
   - **Never target by recent spend.** If `--target` is forced, use a window disjoint from the baseline and cap the label at `measured` [rtm-targeting].
3. **Washout.** `max(1h TTL, p90 session length, settings refresh)`. Server-managed settings are polled hourly, MDM every 30 minutes, and file settings reload on change [stepped-wedge-mdm]. The adoption week gets weight 0.5.
4. **MDE preview**, two ways:
   - **A/A re-randomization** on the org's last `--pre-weeks` of cost per active dev-day (500 re-randomizations of the same design, CUPED-adjusted; the MDE at 80% power ≈ 2.8 × SD of placebo estimates).
   - **Closed form.** `n_per_arm = 16·CV²·(1−ρ²)·DEFF/δ²` × 1.5 safety [heavy-tails-power].

   Refuse to call the design "verification" if `MDE > 0.8 × projected effect`. Say what fleet size or duration would suffice. Require ≥20 clusters per arm, preferring 50.
5. **Outputs.**
   - `plan.json`: lever, design, units, waves, holdback, unit metric, estimator, looks, rate card hash, seed, washout.
   - `assignment.csv`.
   - `prereg.sha256` = sha256 of the canonical JSON of plan + assignment, to be committed before launch.
   - Per-wave `patches/wave-<k>/` with the lever patch, plus `OTEL_RESOURCE_ATTRIBUTES` fragments `tokenbill.arm=treat|control,tokenbill.wave=k`, so telemetry carries assignment [assignment-telemetry-srm].

**`verify`** (`did.py`, `cuped.py`, `sequential.py`, `stats.py`).
1. **Panel.** Unit × day: cost per active dev-day (`active` = at least one billed request that day). Costs are **repriced at the pre-registered rate card** (constant prices). The price effect is reported separately as `exact` price variance [staggered-did].
2. **Estimators.**
   - **CS ATT(g,t)** with not-yet-treated controls: `ATT(g,t) = E[Y_t − Y_{g−1} | G=g] − E[Y_t − Y_{g−1} | G>t or never]`. Aggregated as the cohort-size-weighted mean over post-periods and as an event study by weeks since adoption.
   - **BJS imputation.** Unit and time FE fit on untreated observations by alternating projections (converge to 1e-9 or 200 iterations); impute Y(0) for treated observations; ATT = mean(Y − Ŷ0).
   - **CUPED-DiM** for cluster RCTs: `Y_adj = Y − θ(X − X̄)` with `θ = cov(X,Y)/var(X)` pooled at the cluster level. X is the unit's pre-period mean over 4–8 weeks, with a missing-pre indicator [cuped].
   - **Switchback lag-p Hajek** for org-wide reversible changes: daily blocks, burn-in ≥ carryover [switchback-carryover].
3. **Inference.** Stratified cluster bootstrap by wave (B=2000, seeded), percentile CI. Delta-method SE for ratio metrics [ratio-delta-cluster-se]. Report both cluster count and observation count.
4. **Monitoring looks.** Report only at pre-registered looks. Between looks, report the asymptotic confidence sequence:
   - `half-width = σ̂·sqrt( 2(tρ²+1)/(t²ρ²) · ln( sqrt(tρ²+1)/α ) )`
   - `ρ² = (−2 ln α + ln(−2 ln α + 1)) / t*`, with t* = the planned sample size [sequential-monitoring].
5. **Guards.** All are required for `verified`:
   - pre-registration hash matches;
   - SRM χ² p ≥ 0.001 on active dev-days and developers by arm;
   - placebo (fake adoption in the pre-period) within ±MDE/2 with a CI covering 0;
   - reconciliation within ±1% per workspace-month when invoice lines exist (F15);
   - interference guard (units = cache boundary; washout applied);
   - 95% CI lower bound > 0 at a pre-registered look;
   - quality non-inferiority: the one-sided 90% lower bound of the change in merged PRs per active dev-day > −5%, and revert-rate increase ≤ 2 pp [value-adjusted-cost-metric];
   - the realization rate recorded.

   A guard failure means label ≤ `measured` and exit 3.
6. **Receipt** (`receipts.py`).
   - Canonical JSON: sorted keys, `(",",":")` separators, integers only (micro-USD, milli-ratios).
   - Format: DSSE PAE `"DSSEv1 " + len(type) + " " + type + " " + len(body) + " " + body`.
   - Signing: `ssh-keygen -Y sign -n tokenbill-receipt -f KEY`. Verification: `ssh-keygen -Y verify -f allowed_signers -I ID -n tokenbill-receipt -s SIG`, run via `subprocess`. The tests skip if `ssh-keygen` is absent.
   - The schema is v0 of [signed-receipts] (§5.9 of that report): subject digest of the patch, label, design, assignment hash, rate-card hash, result, CI, RR, guards, Shapley credit and adjustments.
   - Label it "in-toto-shaped", not a conforming Statement [signed-receipts].
7. **RR database** (`rr_db.py`): JSON lines of `(lever_class, lever_id, projected, verified, ci)`. An empirical-Bayes prior update per class after ≥3 receipts feeds F8.

**Acceptance tests.**
- `test_ab_known_effect`: synthetic paired tasks with a true +7.6% → the CI covers +7.6% and excludes 0; same seed, same bytes.
- `test_ab_cost_per_success_failures_in_numerator`.
- `test_rollout_randomized_not_targeted`: wave order is independent of pre-period spend (the rank correlation test p > 0.05 over 200 seeds).
- `test_rollout_refuses_underpowered`: 10 workspaces with a 3% projected effect → refused, with a suggested fleet size.
- `test_did_estimators_coverage` (`synth_rollout`, 60 seeds, true ATT 25%): CS and imputation bias within ±5% and 95% CI coverage ≥ 0.85. TWFE is not offered.
- `test_cuped_variance_reduction`: ρ≈0.9 panel → variance reduction ≥ 60%.
- `test_srm_detects_60_40`.
- `test_placebo_guard`.
- `test_cs_boundary_formula`: the half-width matches the reference formula to 1e-12.
- `test_receipt_roundtrip_and_tamper`: sign, then verify OK; flip one byte and verify fails. Skipped without ssh-keygen.
- `test_guard_failure_downgrades_label`.

---

### F10. Cache-breaker registry, about 30 kinds, validated against server diagnostics (WP7 usage-level, WP5 block-level)

**What it does.**
- Replaces v0.1's 5 generic breakers with a registry covering prompt, parameter, infra, gateway and framework causes.
- Each breaker comes with evidence, a model- and platform-gated fix, a dollar figure, and **agreement with `diagnostics.cache_miss_reason` where present**.
- Usage-signature breakers work on OTel-only fleets. Block breakers need shapes.

**Evidence.**
- Documented invalidators [anth-invalidation-hierarchy], [cc-cache-breakers], [cc-invalidators-taxonomy].
- Gateway stripping [cc-gateway-cache-strip], [gateway-strip].
- Nondeterministic serialization [nondeterministic-tools], [cb-key-order].
- Lookback [anth-lookback-20]; fan-out [anth-concurrency-fanout]; write-never-read [write-without-read].
- The moving marker produced a false history-rewrite [cb-moving-marker].
- Compaction and MCP tool additions got the wrong advice [cb-breaker-taxonomy].
- Framework causes [crewai-no-tail-cache], [history-trim-cache-breakers], [tool-subset-churn], [dynamic-system-injection], [replay-serialization-drift].
- OpenAI write waste [oai-cache-write-waste].
- Ground-truth labels: only the four `*_changed` reasons map to breakers [cc-miss-taxonomy-ground-truth].
- Cache-preserving fixes by model [anth-cache-preserving-apis], [mid-conv-system-tool-addition-support].

**$ impact.**
- Corpus: within-TTL prefix changes were ~5% of the bill (33% of the 15.1% miss premium).
- Gateway strip: up to ~90% of affected input.
- ProjectDiscovery's hit rate went 7% → 84%, for −59–70% vs no-cache [projectdiscovery-cache-case].
- Planning ceiling: 1–10% of spend depending on custom harnesses and gateways. `cache_transform` RR → p50 **~$4–36k per month on B**. The spread is widest here; the week-1 scan decides it.

**Algorithm.** The detector catalog is Appendix A, rows C01–C27. Shared rules:
1. **Miss event (usage level).** Request k is a miss if `missed = prev_ctx − cr_k > max(2048, 0.05·prev_ctx)`. This is Claude Code's own definition [cc-cache-breakers].
2. **Miss premium.**
   - `mw = min(missed, cw_k)` and `mu = min(missed − mw, un_k)`.
   - `premium = mw·(w − r) + mu·(in − r)` [cc-miss-taxonomy-ground-truth].
3. **Cause precedence** (first match):
   1. `compaction`/`context_edit`/`clear` event between k−1 and k → **expected rebuild**. Not a breaker, but priced in F13 / compaction economics.
   2. Served model change → C05/C06.
   3. Speed, effort, thinking or tool_choice param change → C07.
   4. `gap > ttl` → C01 (feeds F6/F5).
   5. `thinking_dropped` event → C14.
   6. `mcp_change` or `prompt_snapshot.tools_h` change → C04/C18.
   7. Harness version change → C16.
   8. Block-level diff available → the Mode B classifier (C17–C25).
   9. Otherwise `unexplained_prefix_change`.
4. **Ground truth.** For each event with `diagnostics`:
   - `model_changed` ↔ C05/C06;
   - `system_changed` ↔ C17/C23;
   - `tools_changed` ↔ C04/C18/C19(tools);
   - `messages_changed` ↔ C20/C19(messages)/C24/C25/C07;
   - `previous_message_not_found` ↔ C01 (expected);
   - `unavailable` ↔ C07 (config) or unknown.

   Publish the confusion matrix and precision per kind in the report.
5. **Mode B classifier** (WP5), per consecutive pair in a lane: the first diverging block by `h`.
   - `h_sorted` equal → **serialization churn** (C19).
   - `h_masked` equal and `volatile_classes` non-empty → **volatile** (C17 if system, C23 if earlier than stable content).
   - Tools: a permutation → **order churn** (C18a); set difference → **subset churn** (C18b; fix `tool_addition`/`defer_loading`, not "sort"); same names with a changed def hash → **definition drift** (C18c).
   - A message index < the previous count changed, with no event → **history rewrite** (C20), with framework attribution from `Harness.framework_scope`.

   Hashes exclude `cache_control`, so moving markers never fire [cb-moving-marker].
6. **Dollars.** Single-lever replay with the matching `repairs` (Mode B) or `restore_caching`/TTL (Mode U).
7. **Fix text** is gated by `(platform, model family, harness, version)` from the rate card's `supports`. Examples:
   - mid-conversation `role:system` only on Opus 5/5.5/4.8, Fable 5/5.1, Mythos; not Sonnet 5;
   - `tool_addition` via beta;
   - otherwise `defer_loading` or moving text to the user turn [anth-cache-preserving-apis], [mid-conv-system-tool-addition-support].

**Acceptance tests.**
- One planted-fixture test per C-row: fires on planted lanes only (precision = recall = 1 on synthetic), and dollars within 1% of closed form. Required cases:
  - `test_moving_marker_no_false_rewrite` (v0.1 exp1);
  - `test_key_order_serialization_churn` (exp9-1);
  - `test_mcp_tool_addition_is_subset_churn_not_sort` (exp9);
  - `test_compaction_is_expected_rebuild` (exp9);
  - `test_lookback_overflow_25_blocks` (exp6-2);
  - `test_concurrent_fanout_not_breaker_but_stagger` (exp2b-a2);
  - `test_shared_prefix_breakpoint_misplacement_56pct` (exp2b-a3: ideal ≈ 56% cheaper).
- `test_gateway_strip_signature`: ≥3 turns with `cr = cw = 0` and ctx ≥ min → C02 with premium formula.
- `test_ground_truth_confusion_matrix` on a fixture with diagnostics labels.
- `test_fix_gating_sonnet5_no_mid_conv_system`.

---

### F11. Failure-path and retry waste (WP7)

**What it does.**
- Models each logical request as a chain of attempts.
- Prices failed attempts from the billing-rules table, showing documented vs assumed dollars separately.
- Detects retry storms, cold-retry-after-backoff, fallback cache loss, abort waste, never-succeeding 400s, tool-error loops, non-streaming double sends, and quota-reservation throttling.
- Emits retry-owner, backoff and TTL config fixes.

**Evidence.**
- TTL runs from request start, so a retry after the TTL re-writes the prefix: 4–6× the successful call [fp-ttl-from-request-start].
- The SDK honors an uncapped retry-after [fp-sdk-retry-after-uncapped].
- Claude Code retries 10× (300 under the watchdog) [fp-cc-retry-semantics].
- Nested retries amplify to (1+N)², ~44 attempts [fp-nested-retry-amplification].
- Incident median is 1.07h [fp-incident-duration].
- Fallback credit [fp-fallback-credit], [fp-cc-fallback-chains]; never-succeeding 400s [fp-never-succeeding-400].
- Tool-error loops appear in 14% of production trajectories, loops in 5% [fp-agent-loop-prevalence].
- The local share is small: 0.7% direct, 3.1% upper bound [fp-local-failure-share].

**$ impact.**
- Corpus: 0.7–3.1%, a healthy baseline. It is expected to be higher behind gateways, with 5m TTLs, or with CI watchdogs.
- Planning: 0.5–3% ceiling, `cache_transform` RR → **~$2–11k per month on B**.

**Algorithm.** Detectors D1–D9 (Appendix A, failure-path table):
- **Logical request** = attempts sharing `client_request_id` (Claude Code) or recorder `logical_request_id`, else the same `(lane, shape parent, new-input hash)` within 15 minutes.
- **D2 cold retry.** `retry.ts_start − first.ts_start > ttl` and `retry.cw ≥ 0.5·prefix` → `prefix·(w − r)`.
- **D5 fallback cache loss.** A model switch where the new model's first `cw ≥ 0.8·prefix` and there was no credit shift → `prefix·(w_B − r_B)`. A→B→A ping-pong within the TTL doubles it.
- **D6 tool-error loop.** ≥3 consecutive `is_error`, or the same `input_h` failing ≥2× → the cost of consuming calls plus the error-text carry.
- **D1 retry storm.** >3 attempts per logical request, or retries at ≥2 layers.
- **D4 never-succeeding 400.** Same error class and `input_h` ≥2, or a retry after `x-should-retry:false` or a spend-cap 429.
- **D3 abort waste.** `outcome in {aborted, timeout}` with sub-causes: network (silence 300–360 s, Bedrock NAT 350 s) [fp-network-aborts], watchdog, user.
- **D7 missing final usage** is a correctness row (F2), not waste.
- **D8** is a non-streaming double send. **D9** is Bedrock `max_tokens` over-reservation.

Fix text follows [gap-failure-path-spend §5]:
- one retry owner;
- backoff budget < TTL − generation time;
- 1h TTL for watchdog or CI runs;
- stream plus TCP keepalive;
- resume from partial text;
- redeem the fallback credit or use `fallbacks:"default"`;
- terminal errors are terminal.

An optional incident join (`--status-feed statuspage.json`) prices an "incident tax" [fp-incident-duration].

**Acceptance tests.**
- One test per D-row on synthetic attempt chains, e.g. `test_cold_retry_after_400s_backoff_5m_ttl`: 150k prefix on Opus 5 → the premium equals the formula; the documented example ratio is 4.0× [fp-ttl-from-request-start].
- `test_billing_rule_assumed_vs_documented_split`.
- `test_fallback_ping_pong`.
- `test_tool_error_loop_min3`.
- `test_missing_final_usage_not_abort`.

---

### F12. Automation, CI, eval and scheduled workloads; batch eligibility (WP7)

**What it does.**
- Classifies traffic into workloads W1–W9 with confidence.
- Reports the automated share of spend, $ per CI run / reviewed PR / eval run / scheduled fire.
- Detects:
  - review-trigger multipliers, superseded runs and re-runs;
  - cross-run cache loss from dynamic system sections;
  - cold fan-outs;
  - cadence > TTL and no-op fires;
  - batch-eligible traffic;
  - judge share and eval over-powering;
  - dev/test bleed.
- Emits CI workflow snippets, `claude -p` flags and batch migration notes.

**Evidence.**
- 77% of business API usage shows automation patterns [auto-share-proxy].
- Review at $15–25 per run vs $150–250 per developer per month interactive [ci-review-unit-costs]; every-push triggers multiply cost [ci-trigger-multiplier].
- Dynamic sections block cross-run caching; the fix gave −54% [ci-cross-run-cache].
- Fan-out stagger [ci-fanout-stagger]; batch 50% [batch-flex-50]; cadence vs TTL up to ~12× per fire [sched-cadence-ttl].
- Judge choice and batching [eval-judge-choice], [eval-batch-judges]; trials sized to the decision [eval-stats-trials].
- Headless ingest [ci-headless-ingest]; classification signals [detect-signatures]; dev/test bleed [dev-test-bleed].

**$ impact.** Planning: 20–40% of automated spend.
- The automated share is unknown until measured.
- Batch and cross-run caching are `rate`/`cache_transform` with high RR.
- Illustrative: if 15% of API spend is automated, 25% of it is recoverable.

**Algorithm.**
1. **Workload classifier.** Rules in priority order, with confidence:
   - `CLAUDE_CODE_ENTRYPOINT=="claude-code-github-action"` / OTel `app.entrypoint` → W2/W3 by `GITHUB_EVENT_NAME` (pull_request → W3/W7, issue_comment → W2, schedule → W5);
   - `entrypoint in {sdk-py, sdk-ts, sdk-cli}` with no human turns → W4;
   - `ActivityDay.active_time user≈0` → automated;
   - periodic series with jitter ≤ max(15%, 30 min) → W5;
   - judge rubric fingerprint (identical system and tools, one varying slot, a "score" or "grade" output format hash) → W6;
   - embeddings endpoints → W9;
   - otherwise W1.

   Attribution-kit lint: automated traffic without `OTEL_RESOURCE_ATTRIBUTES` workload tags is flagged [detect-signatures].
2. **Cross-run cache (C13).** CI lanes in one cache domain whose first requests each write ≥ P (the static prefix) within TTL of each other → `shared_ci_prefix` replay. Fix: `--exclude-dynamic-system-prompt-sections`, one CI workspace, a pinned CLI version, 1h TTL when inter-run gaps are 5–60 minutes [ci-cross-run-cache].
3. **Scheduled fires.**
   - Detect series (≥4 fires, CV of the interval < 0.3).
   - $ per fire, and cadence vs TTL: if cadence > ttl, then every fire is cold; replay with 1h TTL or a smaller context.
   - No-op fires are sessions where no tool writes files and output < 200 tokens; flagged.
4. **Batch eligibility.**
   - Eligible: `single_shot` (lane with 1 request and no tool loop) **and** no human waiting (workload not W1) **and** tolerates ≥1h latency (workload ∈ {W4, W6, W7, W8, W9}).
   - Price at the batch tier with the cache band. Exclude Managed Agents and tool loops [anth-batch-stacking].
   - Report what already runs in batch (`service_tier`).
5. **Review triggers.** Given PR event metadata in `outcomes.jsonl` (`pr_id`, `event`, `run_attempt`, `sha`), compute runs per PR, superseded runs (a newer sha exists) and re-runs (`run_attempt > 1`). Replay the "once on ready_for_review + opt-in" policy → dollars [ci-trigger-multiplier].
6. **Judges.** Judge share of eval spend; judge model vs model under test; recommend Haiku or a panel for ranking gates, and batch judges [eval-judge-choice].
7. **Caps.** Per workflow p50/p95/p99 of $ and turns; recommend `--max-turns` and `--max-budget-usd` at p99 as damage limits, not savings [ci-caps-budgets].

**Acceptance tests.**
- `test_workload_classifier_rules` (table-driven).
- `test_ci_cross_run_cache_replay`: 10 runs, 40k prefix, 20-minute gaps → savings formula.
- `test_scheduled_cadence_gt_ttl`: 30-minute cadence, 5m TTL, 150k ctx, Opus 5 → per-fire cold vs warm is ~$0.94 vs ~$0.075, reproducing [sched-cadence-ttl]'s illustration within 5%.
- `test_batch_eligibility_excludes_tool_loops`.
- `test_superseded_runs`.
- `test_judge_share`.

---

### F13. Carry cost and configuration tax (WP6)

**What it does.**
- **Carry cost.** Prices each tool result and each piece of always-on context over its lifetime: its write, plus a read on every later request in the lane.
- **Ranking.** By tool, MCP server, skill and command family, in dollars.
- **Config tax.** Skill listing, MCP instructions, deferred-tool deltas, CLAUDE.md and memory.
- **Fixes and replays.** Output caps, filter hooks, tool search and pruning settings, each replayed as a trajectory lever.

**Evidence.**
- Bash carries 49.3% and Read 37.3% of appended-context carry, ~12% of the bill [cc-tool-output-carry].
- Config tax is 3–7%, likely nearer the low end [cc-config-sprawl-tax].
- Tool-output bounding [tool-output-bounding]; tool schema bloat and tool search (−85% definition tokens) [tool-schema-bloat], [anth-tool-search-defer].
- AgentDiet gives −21–36% net [agentdiet].
- Context anatomy [ctx-obs-dominance].
- Duplicate reads are *not* a meaningful lever in the corpus: 0.6% of reads [cc-redundant-reads-negative]. Keep only a cheap counter.
- Log reducers [log-test-reduction].

**$ impact.**
- Corpus: carry ~12% (Bash+Read ~10%) and config tax 3–7%.
- Planning ceiling: 3–8%, trajectory RR → p50 **~$6–16k per month on B**.

**Algorithm.**
1. **Carry per tool result** appended at request j in a lane with requests j..n. The read counts only within warm transitions.
   - `carry = tokens · (w_j + Σ_{k>j} r_k)`
   - Tokens come from `ToolResultMeta.n_tokens_est`: chars ÷ the calibrated chars-per-token for (tool class, tokenizer gen); default 2.5 for 4.7+ tool output [cc-chars-per-token-calibration].
   - The estimate is rescaled so that Σ appended tokens between consecutive requests equals the observed `Δctx`.
2. **Config tax.** For `attachment` events of type skill_listing, mcp_instructions_delta, deferred_tools_delta and task_reminder: count only the **first** listing per lane plus actual deltas. This avoids the double count found in the correction [cc-config-sprawl-tax].
3. **Fixes** (lever `cc.tool_output_cap`, `ToolOutputCapPolicy`):
   - `bashOutputMaxChars` (default 30,000);
   - `env.MAX_MCP_OUTPUT_TOKENS` (default 25,000);
   - a PreToolUse/PostToolUse filter hook for test output (failures only) and logs (grep then tail) [log-test-reduction];
   - `env.ENABLE_TOOL_SEARCH=true` when tool-definition tokens exceed 10k or the base URL is a gateway [anth-tool-search-defer], [cc-gateway-cache-strip];
   - `skillListingBudgetFraction`;
   - `claudeMdExcludes`.
4. **Residency profiler** (Mode B, exact when shapes exist): per block, `tokens × turns resident × read price` [context-tax-residency].

**Acceptance tests.**
- `test_carry_formula`: a 10k-token result at request 2 of a 10-request warm lane.
- `test_config_tax_first_listing_only`.
- `test_tool_cap_replay_estimated`.
- `test_tool_search_recommendation_gateway`.
- `test_duplicate_read_counter_small`.

---

### F14. Compression economics guardrail: r*, K*, net-of-cache evaluation (WP6)

**What it does.** Stops recommendations and vendor claims that would raise the bill:
- a per-report panel with r* (break-even compression ratio for a reused prefix);
- K* (payback horizon for history rewrites);
- the extra turns an insertion-time compression can afford;
- a `compress-econ` calculator;
- routing of every compressor or proxy claim to `tokenbill ab`.

**Evidence.**
- `r* = N/(α + β(N−1))`, approaching 10/20/40× [comp-cache-breakeven].
- `K* = S(α−β)/(Xβ)`: ~17 calls on 0.1× models, 36 on Opus 5.5, 74 on Fable 5.1 [history-rewrite-kstar].
- Query-aware compression costs +40.1% [capc-compress-then-cache].
- Context editing costs +74% on short runs, while boundary pruning saves 39% [anth-context-editing-not-savings], [jagged-pruning].
- Token reduction is not cost reduction [token-not-cost].

**$ impact.** Avoids regressions of +7% to +48%. It is decision support; it has no ceiling of its own.

**Algorithm.**
- **Per lane or segment:** reuse count N from Mode U/B, α = write multiplier, β = read multiplier (per model) → r*.
- **Per `context_edit` or compaction event:** X = cleared tokens, S = tokens after the edit → K*. Compare with the remaining requests in the lane: if remaining < K*, the event lost money. This drives the `cache.context_edit_churn` detector (C09).
- **Insertion-time compression** of a tool output with ratio r over its remaining reads: `saved = tokens·(1 − 1/r)·(w + Σr_k)`. Affordable extra turns = `saved / (mean per-turn cost)`.
- **CLI.**

  ```
  tokenbill compress-econ --model claude-opus-5-5 --reuse 20
  ```

  prints r*, and K* for the given X and S.

**Acceptance tests.**
- `test_rstar_values`: N=5 → 3.03; N=20 → 6.35 at α=1.25, β=0.1; the limits are 10, 20 and 40.
- `test_kstar_example`: X=40k, S=60k → 17 (β=0.1), 36 (β=0.05), 74 (β=0.025).
- `test_context_edit_churn_detector`.

---

### F15. Reconciliation, effective savings rate, FOCUS and OTLP export (WP9)

**What it does.**
- `reconcile` compares the ledger against Anthropic `cost_report`, the usage report and Enterprise Analytics per day × workspace × model, and explains residuals.
- `ledger` shows exact spend and the **Effective Token Savings Rate** (`1 − billed / list-equivalent-uncached`, decomposed into caching, batch and discount).
- `export` writes FOCUS-shaped CSV and OTLP metrics JSON.

**Evidence.**
- Reconciliation is the enterprise's first acceptance test [ent-reconciliation], [anth-admin-api-reconciliation].
- The cost report is daily, in decimal cents, excludes Priority Tier, and is revisable for 30 days [anth-admin-usage-cost-api], [cc-admin-analytics-apis].
- Effective discount = `1 − amount/list_amount` [cc-anthropic-discount-visibility].
- ESR [finops-rate-vs-usage]; FOCUS [ent-focus-1-4], [focus-15-ai-columns]; reconciliation gaps by channel [cc-reconciliation-matrix].

**$ impact.** Trust, not a lever. It also surfaces the **realized contract discount**, including leaks when realized is below the contract [cc-ccu-private-offers].

**Algorithm.**
1. Join ledger attempts (priced at the contract basis when `--contract` is given) with `InvoiceLine`s by `(day UTC, workspace_h, model_base)`.
2. `variance = ledger − invoice`, reported absolute and as a percentage.
3. Residual explanations:
   - Priority Tier: usage rows with `service_tier=priority`;
   - the provisional window (<30 days);
   - unpriced models;
   - estimated usage;
   - code-exec free-hours credit;
   - traffic not captured (coverage = ledger ÷ invoice).
4. Tolerance: default ±1% per workspace-month on closed periods; `--fail` exits 3.
5. **ESR** is `exact`. The decomposition is sequential in FinOps order: usage levers, then rate levers.
6. **FOCUS columns** as in §2.12. Money is written as decimal strings from nano.

**Acceptance tests.**
- `test_decimal_cents_parse_exact` ("123.456789" cents → nano exactly).
- `test_reconcile_within_tolerance` and `test_reconcile_priority_tier_explained`.
- `test_provisional_flag`.
- `test_esr_decomposition_sums`.
- `test_focus_columns_present_and_x_prefixed`.
- `test_otlp_metric_names`.

---

### F16. Fleet aggregation, privacy and showback (WP9)

**What it does.**
- k-anonymous team rollups with complementary suppression, HMAC pseudonyms, and a self-view.
- Per-team showback files: a static HTML per team for distribution behind SSO without a server.
- `purge` for retention and DSAR.
- `privacy-pack` generates DPIA, works-council and employee-notice drafts (WP11).

**Evidence.**
- Legal constraints [labor-law-constraints]; aggregation defaults [aggregation-k5].
- Leaderboards failed [leaderboard-goodhart].
- Privacy by default [ent-privacy-by-default].
- Showback first, then chargeback gated on ≥95% allocation coverage [showback-chargeback], [ent-allocation-showback].
- Uber's shared tier and expected-spend alerts [uber-policy-arc].

**$ impact.** Deployment enabler: without it, an EU rollout can be blocked.

**Algorithm.**
- Groups are keyed by any attribution dimension.
- Suppress a group if its distinct `user_h` < k (default 5).
- If exactly one sibling is suppressed within a parent total, also suppress the smallest remaining sibling (complementary suppression).
- Totals always include suppressed mass as "other (suppressed)".
- **Allocation coverage KPI** = allocated $ ÷ total $. Chargeback exports are refused below 95%.
- Showback per team lists that team's opportunities and patches. It never contains per-person numbers.

**Acceptance tests.**
- `test_k_anonymity_suppression`.
- `test_complementary_suppression`.
- `test_no_user_ranked_arrays` (a schema walk: no list of objects keyed by `user_h` except under `--self-view`).
- `test_purge_user`.
- `test_showback_contains_no_user_h`.

---

### F17. Remaining adapters and recorder v2 (WP3a, WP3b)

**What it does.**
- **trace@1 adapter.** Renders Calls into `PromptShape` via `core.shapes`, strips content in metadata mode, and reconstructs lanes, fixing the interleaved-lane under-report [cb-lanes].
- **trace@2 reader/writer.**
- **RecorderV2.** Records every attempt, including exceptions and aborts, with the full envelope; wraps `beta.messages`; serializes at call time; offers opt-in diagnostics injection; writes content-free output through a background writer with per-process shards [cb-recorder-coverage], [fp-recorder-blindspot].
- **OTel GenAI / OpenInference importer** with the convention registry, counting LLM-kind spans only [otel-openinference-importer], [fw-telemetry-conventions].
- **Provider normalizers** for Anthropic, OpenAI Responses/Chat, Gemini, Bedrock Converse, OpenRouter, xAI and DeepSeek.
- **Anthropic Admin usage/cost and Analytics importers**, with an optional `--fetch` doing pagination and 31-day chunking.
- **OpenAI Admin usage/costs importer.**
- **LiteLLM SpendLogs importer.**
- **`outcomes.jsonl` importer** (task ids, success, PR events).

**Evidence.**
- Adapter priority [adapter-adoption-ranking]; ingestion gap [cb-ingestion].
- Admin APIs [anth-admin-usage-cost-api], [oai-admin-usage-costs]; LiteLLM [litellm-spend-and-supply-chain].

**$ impact.** Coverage of non-Claude-Code API spend. Framework and gateway breakers become visible.

**Algorithm notes.**
- **Lanes** (`ingest/lanes.py`). Explicit ids first (agent_id / parent, `isSidechain`, `query_source`, `x-claude-code-agent-id`). Otherwise assign each request to the lane whose last shape is a prefix of this shape: a radix index over `H_msg` chain hashes, longest match. Else match by `(model, H_sys)`. Else open a new lane.
- **Run keys** are namespaced by source hash, and identical inputs are de-duplicated. The invariant "Σ run totals == Σ call totals" is tested [cb-dup-runid].
- **Recorder.**
  - Never raises into the app: all recorder exceptions are logged and swallowed.
  - Snapshots the request with `copy.deepcopy` / `model_dump` **before** sending.
  - Records failed attempts with `message_start` usage when a stream began.
  - Writes via a `queue.Queue` and daemon thread to `trace-<pid>-<uuid4>.jsonl`, mode 0600.
  - `diagnostics=True` adds the beta header and `diagnostics.previous_message_id`. It is opt-in because it modifies requests [anth-cache-diagnostics-beta].

**Acceptance tests.**
- `test_trace1_roundtrip_to_v2_preserves_costs`.
- `test_interleaved_lanes_redundancy` (exp2b-b2: per-lane redundancy ≈ 0.73, not 0.04).
- `test_dup_runid_across_files` ($22, not $40).
- `test_recorder_records_exceptions_and_beta`.
- `test_recorder_snapshot_before_mutation`.
- `test_recorder_never_raises`.
- `test_otel_genai_inclusive_to_disjoint`.
- `test_openinference_counts_llm_spans_only`.
- `test_crewai_17_token_bug_detected_as_telemetry_defect` (fixture from [fw-telemetry-conventions]).
- `test_admin_usage_report_parse`, `test_cost_report_parse`.
- `test_fetch_uses_env_key_and_never_logs_it` (recorded HTTP fixture via a local `http.server`).

---

### F18. CI gate: `tokenbill check` (WP10)

**What it does.** Runs in CI over a recorded smoke-test trace (trace@1/@2 or a recorder run). It fails the PR when:
- cache-read share after turn N drops below a threshold;
- a new breaker appears versus a baseline file;
- cost per task rises beyond tolerance.

It emits SARIF for PR annotation.

**Evidence.**
- Silent regressions after prompt-assembly changes [cb-attribution-platform], [practice-hit-rate-slo], [ent-ci-cost-gate].
- Prefix-stability CI invariant [replay-serialization-drift].
- promptcachelint overlap [promptcachelint-overlap].

**$ impact.** Prevents multi-month silent regressions. The $0.59 → $4.24 per-run example is a 7× cost [anth-fleet-benchmarks].

**Acceptance tests.**
- `test_check_passes_healthy_demo`.
- `test_check_fails_on_timestamp_breaker_exit3`.
- `test_check_baseline_new_breaker_only`.
- `test_sarif_schema_minimal`.
- `test_status_line_regression_fixture`: a 25-token volatile line at the front of the system prompt fails.

---

### F19. Reports, fleet demo and flagship test v2 (WP10)

**What it does.**
- `scan` orchestration (`scan.py`) and the JSON, HTML and terminal outputs of §2.12.
- `demo --fleet` runs the full pipeline on `core.synth` with **planted levers**: growing contexts, cold resumes, a 5m API-key cohort, Opus subagents, sticky fast mode, a stripping gateway, a cold fan-out, a cold retry, CI per-machine prefixes, a scheduled cadence above TTL, Bash carry, and a healthy control team.
- **Flagship test v2** asserts every planted lever is found, priced within 1% of the generator's closed-form truth, and ranked. The healthy team yields zero findings. Output is byte-deterministic across processes (in the style of the v0.1 flagship).

**Acceptance tests.**
- `test_flagship_v2_recovers_planted_levers`.
- `test_scan_json_schema_valid` (a stdlib validator in `out/schema.py`).
- `test_html_self_contained_csp_tables` (no `http` in `src`/`href`; every `<svg>` has a sibling `<table>`).
- `test_terminal_labels_present`.
- `test_demo_v01_unchanged` (golden bytes).
- `test_scan_perf_1e5`.

---

## 4. Pricing and billing facts the build depends on

Rules for builders:
- Every value below goes into data files (`rates/data/*.json`, `rates/data/evidence.json`, `rates/data/billing_rules.json`) or named constants, **never inline in logic**.
- Every row carries `source`, `verified` (date) and `finding`.
- All values were checked by the research on **2026-09-23** unless noted.
- **VERIFY** marks a value the research did not confirm from a primary source. The builder must re-check it, or ship the row disabled with `status: "unverified"`, which makes `price()` return unpriced.
- A weekly CI job runs `tokenbill pricing verify --fetch` against the primary pages (§6 risk R4).

### 4.1 Anthropic first-party list prices (USD per million tokens)

Source: <https://platform.claude.com/docs/en/about-claude/pricing> [anth-pricing-table-2026-09], [anth-pricing-2026], [cb-pricing-coverage].

| Model id (base) | Input | Output | 5m write (1.25×) | 1h write (2×) | Cache read | Read mult. | Min cacheable prefix | Tokenizer gen |
|---|---|---|---|---|---|---|---|---|
| claude-opus-5-5 | 4.00 | 20.00 | 5.00 | 8.00 | 0.20 | 0.05 | 512 | 4.7+ |
| claude-fable-5-1 | 10.00 | 50.00 | 12.50 | 20.00 | 0.25 | 0.025 | 512 | 4.7+ |
| claude-mythos-5-1 | 10.00 | 50.00 | 12.50 | 20.00 | 0.25 | 0.025 | 512 | 4.7+ |
| claude-fable-5 | 10.00 | 50.00 | 12.50 | 20.00 | 1.00 | 0.10 | 512 | 4.7+ |
| claude-mythos-5 | 10.00 | 50.00 | 12.50 | 20.00 | 1.00 | 0.10 | 512 | 4.7+ |
| claude-opus-5 | 5.00 | 25.00 | 6.25 | 10.00 | 0.50 | 0.10 | 512 | 4.7+ |
| claude-opus-4-8 | 5.00 | 25.00 | 6.25 | 10.00 | 0.50 | 0.10 | 1,024 | 4.7+ |
| claude-opus-4-7 | 5.00 | 25.00 | 6.25 | 10.00 | 0.50 | 0.10 | 2,048 | 4.7+ |
| claude-opus-4-6 | 5.00 | 25.00 | 6.25 | 10.00 | 0.50 | 0.10 | 4,096 | legacy |
| claude-opus-4-5 | 5.00 | 25.00 | 6.25 | 10.00 | 0.50 | 0.10 | 4,096 | legacy |
| claude-opus-4-1, claude-opus-4 (retired on 1P; still billed on partner clouds) | 15.00 | 75.00 | 18.75 | 30.00 | 1.50 | 0.10 | **VERIFY** | legacy |
| claude-sonnet-5 (the $3/$15 increase was cancelled) | 2.00 | 10.00 | 2.50 | 4.00 | 0.20 | 0.10 | 1,024 | 4.7+ |
| claude-sonnet-4-6 | 3.00 | 15.00 | 3.75 | 6.00 | 0.30 | 0.10 | 1,024 | legacy |
| claude-sonnet-4-5 | 3.00 | 15.00 | 3.75 | 6.00 | 0.30 | 0.10 | 1,024 | legacy |
| claude-sonnet-4 | **VERIFY** (listed as existing; price not captured) | | | | | 0.10 | **VERIFY** | legacy |
| claude-haiku-4-5 | 1.00 | 5.00 | 1.25 | 2.00 | 0.10 | 0.10 | 4,096 | legacy |
| claude-haiku-3-5 | **VERIFY** (price not captured) | | | | | 0.10 | 2,048 | legacy |

Notes:
- **Upcoming models.** Sonnet 5.5 and Haiku 5.5 were announced on 2026-09-22 for "the coming weeks" and are not yet priced [anth-pricing-table-2026-09]. They are the first test of the pricing-drift CI job.
- **Tokenizer.** Claude 4.7+ (Opus 4.7/4.8/5/5.5, Sonnet 5, Fable, Mythos) produces about 30% more tokens for the same text: a 1.0–1.35× band, 1.01× on CJK, up to 1.47× on English technical docs. Evidence registry id `tokenizer.47plus.factor` = (1.00, 1.30, 1.35) [anth-tokenizer-inflation], [tokenizer-inflation-47plus].
- **Successor map** (for F7 same-tier repricing; the `successor` field in the rows):
  - fable-5 → fable-5-1 (same tokenizer)
  - mythos-5 → mythos-5-1
  - opus-5 → opus-5-5 (same tokenizer)
  - opus-4-8 → opus-5-5 (tokenizer equivalence **unverified**, so use the band) [cc-same-tier-upgrade]

### 4.2 Anthropic price modifiers, server tools and runtime

| Fact | Value | Source | Finding |
|---|---|---|---|
| Cache write multipliers | 1.25× input (5m TTL), 2.0× input (1h TTL). Usage reports `cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens` | pricing; <https://platform.claude.com/docs/en/build-with-claude/prompt-caching> | [anth-cache-1h-2x] |
| Batch | 0.5× on **all** buckets including cache reads and writes; stacks with caching and data residency. Not available: fast mode, fallbacks, `max_tokens:0`, Managed Agents. Cache hits in batch are best-effort, 30–98%. Limits: 100k requests or 256 MB per batch, 24h expiry | <https://platform.claude.com/docs/en/build-with-claude/batch-processing> | [anth-batch-stacking], [batch-plus-cache] |
| US data residency | `inference_geo: "us"` → 1.1× on every token bucket, models 4.6+, on the 1P API and Claude Platform on AWS; the Foundry US Data Zone equivalent | <https://platform.claude.com/docs/en/manage-claude/data-residency> | [anth-modifiers-geo-fast-priority], [claude-marketplace-ccu] |
| Regional cloud endpoints | Bedrock/Vertex regional or multi-region +10% vs global (Vertex: Sonnet 4.5, Haiku 4.5, Opus 4.5 and later) | pricing; Vertex docs | [vertex-claude-geo-labels], [hosted-service-variance] |
| Fast mode | Opus 5.5 $8/$40; Opus 5 and Opus 4.8 $10/$50 (2×). 1P only. Cache multipliers apply on top. Doesn't share cache with standard speed | <https://platform.claude.com/docs/en/build-with-claude/fast-mode> | [anth-modifiers-geo-fast-priority], [premium-modifiers] |
| Priority Tier | No longer sold. Legacy commitments burn down at read 0.1, 5m write 1.25, 1h write 2.0, US geo 1.1. **Absent from `cost_report`**, so it is reconciled via usage `service_tier=priority`. Excludes Opus 5.5, Opus 5, Sonnet 5, Fable 5.1, Mythos | pricing; usage-cost-api | [anth-modifiers-geo-fast-priority], [cc-priority-legacy], [cc-capacity-model-lag] |
| Web search | $10 per 1,000 searches plus result tokens; failed searches are not billed | <https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool> | [anth-web-search-controls], [fp-billing-matrix] |
| Web fetch | Tokens only | same | [anth-web-search-controls] |
| Code execution | Free with web_search/web_fetch 20260209+. Otherwise 1,550 free hours per org per month, then $0.05 per hour per container, 5-minute minimum. Appears only in `cost_report` | pricing | [anth-ptc-code-exec], [anth-admin-usage-cost-api] |
| Managed Agents | $0.08 per running session-hour; no batch discount. Session budgets pause with `budget_reached` | pricing | [anth-governance-controls], [sched-cadence-ttl] |
| CCU | Claude Platform on AWS and Foundry bill in Claude Consumption Units of $0.01. Discounts apply at conversion; hourly metering; single invoice line; no usage API on Claude Platform on AWS | marketplace docs | [claude-marketplace-ccu], [cc-ccu-private-offers] |
| Spend caps | Org monthly caps: Start $500, Build $1,000, Scale $200,000 (Custom none). A spend-cap 429 has no `retry-after` and is terminal | rate-limits doc | [anth-cache-rate-limits], [fp-ratelimit-feedback] |
| Rate limits | Cache reads do **not** count toward ITPM (exception: Haiku 3.5); cache writes do. OTPM counts actual output | <https://platform.claude.com/docs/en/api/rate-limits> | [anth-cache-rate-limits] |

### 4.3 Anthropic prompt-cache rules (drive Mode U and Mode B)

Primary sources: <https://platform.claude.com/docs/en/build-with-claude/prompt-caching>, the Claude Code doc <https://code.claude.com/docs/en/prompt-caching>, and <https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics>.

| Rule | Value | Finding |
|---|---|---|
| TTL | 300 s (5m) or 3,600 s (1h). **Measured from request start**; generation time counts. Reads refresh the TTL at no cost | [anth-ttl-start-1h], [fp-ttl-from-request-start] |
| Visibility | An entry is readable only after the first response **begins streaming**, so N parallel same-prefix requests all pay full price. Fix: send 1, await the first token, send N−1. Claude Code holds fan-out siblings up to 5 s | [anth-concurrency-fanout] |
| Lookback | Each breakpoint checks ≤ 20 positions, counting itself. A run of consecutive `tool_use` blocks is one position; same for `tool_result`. Fix: an intermediate breakpoint about every 15 positions | [anth-lookback-20], [agent-cache-lookback-idle] |
| Breakpoints | ≤ 4 per request; mixed TTLs ordered longest first; top-level `cache_control` = automatic caching | [cb-lookback], [anth-cache-1h-2x] |
| Minimum prefix | See §4.1. Shorter prefixes silently don't cache. Bedrock lists Opus 4.7 at 4,096 vs Anthropic's 2,048: keep a **per-channel** minimum table | [anth-cache-hidden-miss-causes], [bedrock-cache-semantics] |
| Isolation | Per **workspace** on the 1P API, Claude Platform on AWS and Foundry; per **organization** on Bedrock and Vertex. Claude Code prefixes are effectively per machine and directory (cwd, OS, git snapshot in the system prompt) | [anth-workspace-fleet-scope], [ci-cross-run-cache] |
| Invalidation hierarchy | Tools or model change → all tiers. Speed, web-search toggle, citations toggle, system → system + messages. `tool_choice`, `disable_parallel_tool_use`, image add/remove → messages. Thinking or effort → messages (and all tiers on some models). Dropped thinking blocks → messages from that block. Older models and Haiku ≤4.5 strip earlier thinking on a plain user turn | [anth-invalidation-hierarchy] |
| Cache-preserving alternatives | Mid-conversation `role:system` (GA; Opus 5/5.5/4.8, Fable, Mythos; **not Sonnet 5**). `tool_addition` / `tool_removal` (beta `mid-conversation-tool-changes-2026-07-01`). Inline tools (beta `inline-tools-2026-09-15`, 1P). Per-message effort (beta `mid-conversation-output-config-2026-07-01`; Fable 5.1, Mythos 5.1, Opus 5, Opus 5.5). `clear_at: next_user_message` reminders (beta) | [anth-cache-preserving-apis], [mid-conv-system-tool-addition-support] |
| Keep-alive | Re-send the previous request with `max_tokens: 0` within 4 minutes of its start, then every 4 minutes; bills a cache read. Rejected with `stream:true`, structured outputs, forced `tool_choice`, and inside batches. Break-even idle `I_max = interval·(w/r − 1)`: 46 min at r=0.1, 96 min at r=0.05, 196 min at r=0.025 | [anth-ttl-choice-keepalive], [keepalive-economics] |
| TTL rule of thumb | Use 1h when > 1 in 20 gaps fall in 5–60 min. With no pauses, 5m is 15% cheaper on Sonnet 5 and 11% on Opus 5 | [anth-fleet-benchmarks] |
| Diagnostics beta | Header `cache-diagnosis-2026-04-07` plus `diagnostics.previous_message_id` on every turn. Response `diagnostics.cache_miss_reason.type` ∈ {`model_changed`, `system_changed`, `tools_changed`, `messages_changed`, `previous_message_not_found`, `unavailable`}. `cache_missed_input_tokens` is a **magnitude estimate, not billing**. First divergence only. 1P only. `unavailable` covers tool_choice, thinking, context_management, output_config, beta-set and output_format changes, and divergences beyond the horizon | [anth-cache-diagnostics-beta], [cc-miss-taxonomy-ground-truth] |
| Healthy-loop signature | Warm loop: reads ≈ the whole prior prefix; writes ≈ previous output + new input. `cache_creation` ≈ the full conversation on every turn means an upstream rewrite, thinking strip, or lookback overflow | [papers-caching F9] |

### 4.4 Anthropic usage and billing semantics

| Fact | Value | Source | Finding |
|---|---|---|---|
| Server-side compaction | `compact_20260112` (default trigger 150,000 input tokens, minimum 50,000) and on-demand `compact-2026-09-04`. **Top-level `input_tokens`/`output_tokens` exclude the compaction iteration; sum `usage.iterations`** | <https://platform.claude.com/docs/en/build-with-claude/compaction> | [anth-compaction-iterations-billing], [compaction-iterations-p0] |
| Advisor tool | Advisor tokens appear only in `usage.iterations[type=advisor_message]`, billed at the advisor model's rates | tool docs | [advisor-iterations-undercount] |
| Refusal billing | A pre-output decline is not billed but counts against rate limits. A mid-stream decline bills input plus the output streamed. `usage.iterations` is the per-attempt billing record | <https://platform.claude.com/docs/en/build-with-claude/refusals-and-fallback> | [fp-anth-refusal-iterations] |
| Fallback credit | Beta `fallback-credit-2026-07-01`. Re-bills the previously cached span at read rates on the fallback model if the retry comes within 5 min with an exactly matching body | <https://platform.claude.com/docs/en/build-with-claude/fallback-credit> | [fp-fallback-credit], [anth-governance-controls] |
| Context editing | `clear_tool_uses_20250919`: trigger 100,000, keep 3. `applied_edits[].cleared_input_tokens`. Each clear invalidates the prefix from the clear point; measured +74% cost on short runs | <https://platform.claude.com/docs/en/build-with-claude/context-editing> | [anth-context-editing-not-savings] |
| Thinking | Billed as output even when not displayed. `usage.output_tokens_details.thinking_tokens` (streaming: final `message_delta` only). Prior-turn thinking is billed again as input on Opus 4.5+, Sonnet 4.6+, Fable, Mythos | extended-thinking docs | [thinking-billing-invisible] |
| Effort defaults | Opus 5.5: medium. Opus 4.7: xhigh. Others: high. A top-level effort change invalidates the messages cache | model docs | [cc-output-thinking-effort], [anth-effort-rerun-budgets] |
| Task budgets | Beta, ≥ 20,000 tokens, advisory. Changing one mid-task busts the cache. Not on Sonnet 5 or in Claude Code | cost guide | [task-budgets] |
| Vision | tokens = ceil(w/28) × ceil(h/28); capped at 1,568 (standard, long edge 1,568 px) or 4,784 (4.7+ high-res tier, long edge 2,576 px) | <https://platform.claude.com/docs/en/build-with-claude/vision> | [anth-vision-tokens], [images-thinking-estimation] |
| Tool-use overhead | Tool-use system prompt: 286 tokens (Opus 5/5.5 auto/none), 354/474 (Sonnet 5), 675/804 (Opus 4.7). Bash 244–325. Text editor ~700. Computer use ~4,500. Browser ~6,600 | pricing / tool docs | [anth-hidden-overhead-tokens] |
| count_tokens | Free; GA on all Claude platforms; separate RPM limits (5k/10k/20k by tier). An estimate. Ignores caching. Rejects server tools and URL/file sources | <https://platform.claude.com/docs/en/build-with-claude/token-counting> | [count-tokens-exact-attribution] |
| Measured chars per token (4.7+ tool output) | Bash 2.32, Read 2.54, WebSearch 2.49, WebFetch 2.66. English 3.60 (new tokenizer) vs 4.33 (old) | corpus + count_tokens studies | [cc-chars-per-token-calibration], [chars-heuristic-error] |
| Errors | The preserved-thinking binding 400 (accounts created on or after 2026-08-31) never clears on retry; fix is `drop_block`. "Prompt is too long" is a 400 on every model. Gateway spend-cap 429 has `x-should-retry: false` | api/errors; bundled skill `shared/error-codes.md` | [fp-never-succeeding-400] |
| SDK retries | Python SDK: 10-minute default timeout, 2 retries. Honors **any** positive `retry-after` (main, 2026-09-19). Backoff 0.5 s doubling to 8 s. Retries 408, 409, 429, 5xx. Sends `x-stainless-retry-count` | anthropic-sdk-python `_base_client.py` | [fp-sdk-retry-after-uncapped] |
| Published benchmarks (evidence registry) | Agent loops read a median **84%** of input from cache; top decile ≥ **94%**; investigate below **80%**. Caching cut agent-loop cost 2.7–5.3× (live guide; the bundled 2.5–3.7× is stale). Effort on SWE-bench Pro Opus 5: medium −50% cost for ~−2 pts; low −75% for ~−8 pts. Low+re-run failures ~93% pass at $0.45 vs 91.7% at $0.93. Task budgets on Fable 5.1: −44% / −58% cost for ~−3 / −6 pts. Output shape −39% output, −14% cost. A 16,384 `max_tokens` cap ended 15% of Opus 5 and 43% of Fable 5.1 attempts. Context editing +74% on a short run. Compaction −32% on a long run; pruning −39% | <https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence> | [anth-cache-health-thresholds], [anth-effort-rerun-budgets], [anth-output-hygiene], [anth-evidence-drift] |

### 4.5 Claude Code facts

Sources: <https://code.claude.com/docs/en/costs>, `/prompt-caching`, `/model-config`, `/settings-reference`, `/env-vars`, `/managed-settings`, `/server-managed-settings`, `/monitoring-usage`, `/sub-agents`, `/claude-apps-gateway-spend-limits`.

| Fact | Value | Finding |
|---|---|---|
| Fleet baselines | About $13 per developer per active day; $150–250 per developer per month; 90% of users < $30 per active day | [cc-enterprise-baseline], [claude-code-enterprise-baselines] |
| Default TTL | Subscription within plan, main conversation: **1h**. API key, cloud provider, usage credits, all subagents, forks and compaction: **5m**. The 1h TTL is unavailable via the Claude apps gateway and needs `anthropic-beta` forwarded through other gateways | [cc-ttl-policy], [cc-ttl-billing-path], [cc-apps-gateway-routing-tax] |
| Auto-compact | Default ~**967K** on 1M-native models (Sonnet 5, Fable, Opus 4.7+ on the API). Control via `autoCompactWindow` (100,000–1,000,000) or `CLAUDE_CODE_AUTO_COMPACT_WINDOW`; `CLAUDE_CODE_DISABLE_1M_CONTEXT` forces 200K. The `--autocompact` flag is **not preempted** by managed settings, so enforce via the managed `env` block | [cc-autocompact-window], [cc-managed-settings-levers] |
| Documented cache breakers | Model switches (opusplan toggles, automatic fallback, skill `model:` frontmatter). Effort changes (except Opus 5.5 and Fable 5.1 on API key or subscription). The first fast-mode enable. MCP connect/disconnect when tools are in the prefix (tool search off, custom `ANTHROPIC_BASE_URL`, `alwaysLoad`). Plugins with MCP servers. Bare deny rules without tool search. `/compact`. Image-eviction batches. Upgrades. Cwd/worktree is a cache-scope fact | [cc-cache-breakers], [cc-invalidators-taxonomy] |
| Gateway effects | A gateway stripping `cache_control` while returning success bills the whole history uncached every turn. A non-first-party `ANTHROPIC_BASE_URL` disables tool search by default | [cc-gateway-cache-strip], [gateway-strip] |
| `/usage` miss rule | A request re-processing > 5% of the prefix **and** ≥ 2,000 tokens; names a likely cause (v2.1.260+) | [cc-usage-likely-cause] |
| Transcripts | `~/.claude/projects/<slug>/<session>.jsonl`, `<session>/subagents/agent-*.jsonl`, workflow agents. Internal format that changes between versions. Deleted after `cleanupPeriodDays` (default 30) | [cc-transcript-format], [ent-local-transcripts] |
| OTel | Enable with `CLAUDE_CODE_ENABLE_TELEMETRY=1`. Metrics `claude_code.token.usage` (type input/output/cacheRead/cacheCreation) and `claude_code.cost.usage`, with attributes `model`, `query_source` (main/subagent/auxiliary), `speed`, `effort`, `agent.name`, `skill.name`, `plugin.name`, `mcp_server.name`. Event `claude_code.api_request` (tokens, `cost_usd`, `duration_ms`, `request_id`, `client_request_id`, `attempt`, `success`, `stop_reason`). `api_error`. `tool_result` (`tool_result_size_bytes`). Beta traces via `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1` (`claude_code.llm_request` with `ttft_ms`, `agent_id`, `parent_agent_id`; `claude_code.tool` with `result_tokens`). Raw bodies via `OTEL_LOG_RAW_API_BODIES` (inline truncated at 60 KB; file mode untruncated). `OTEL_RESOURCE_ATTRIBUTES` for team and arm tags. `user.email` is **not** populated for API key, Bedrock, GCP and Foundry sessions. `vcs.*` needs `OTEL_METRICS_INCLUDE_REPOSITORY` (v2.1.269+); entrypoint needs `OTEL_METRICS_INCLUDE_ENTRYPOINT` | [cc-otel-schema], [anth-claude-code-analytics-otel], [detect-signatures] |
| Cost fields are list-price estimates | `/usage`, status line, SDK `total_cost_usd`, OTel cost and `--max-budget-usd` are all local list-price estimates unless the managed `modelPricing` (v2.1.242+) is set | [cc-list-price-estimates] |
| Hooks | SessionStart hooks receive `context_tokens`, `prompt_cache_likely_expired`, `estimated_cache_write_usd`. PreToolUse `updatedInput`; PostToolUse `updatedToolOutput` | [channel-code-review-hooks], [cc-tool-output-bloat] |
| Output limits | `bashOutputMaxChars` default 30,000. MCP output warns at 10K tokens and caps at 25K (`MAX_MCP_OUTPUT_TOKENS`) | [cc-tool-output-bloat] |
| Settings propagation | Server-managed settings are polled hourly and **cannot target groups**. MDM refreshes every 30 min. File settings reload on change. The Claude apps gateway can deliver per-IdP-group settings | [stepped-wedge-mdm] |
| Headless / CI | `claude -p --output-format json` returns `total_cost_usd`. `stream-json` (dedupe by message id). `--bare`. `--exclude-dynamic-system-prompt-sections` (SDK `excludeDynamicSections`). `claude-code-action` writes `$RUNNER_TEMP/claude-execution-output.json` with `CLAUDE_CODE_ENTRYPOINT=claude-code-github-action` | [ci-headless-ingest], [ci-cross-run-cache], [detect-signatures] |
| Agent SDK accounting | Parallel tool calls share one message id (dedupe). Per-step `output_tokens` is a placeholder. The result `usage` excludes subagents (`total_cost_usd` includes them) | [sdk-accounting-pitfalls] |

### 4.6 Settings and config keys the emitters may write (known-key table)

Emitters raise on any key not in this table. Rows marked **VERIFY** must be checked against `settings-reference`, `env-vars` or the LiteLLM docs before WP4b ships them; until then they are emitted as commented guidance only.

| Emitter target | Key | Value domain | Lever | Finding |
|---|---|---|---|---|
| managed-settings | `autoCompactWindow` | int 100000–1000000 | F4 | [cc-autocompact-window] |
| managed env | `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | same, as string | F4 | [cc-managed-settings-levers] |
| managed env | `CLAUDE_CODE_DISABLE_1M_CONTEXT` | "1" | F4 alternative | [cc-autocompact-window] |
| managed-settings | `promptCacheTtl` | "5m" \| "1h" (**VERIFY** value format) | F6 | [cc-ttl-policy] |
| managed-settings | `subagentPromptCacheTtl` | "5m" \| "1h" (**VERIFY**) | F6 | [cc-ttl-policy] |
| managed env | `CLAUDE_CODE_PROMPT_CACHE_TTL`, `ENABLE_PROMPT_CACHING_1H`, `FORCE_PROMPT_CACHING_5M` | **VERIFY** value formats | F6 | [cc-ttl-managed-settings] |
| managed env | `CLAUDE_CODE_SUBAGENT_MODEL` | model id | F7 | [plan-execute-subagents] |
| managed-settings | `maxEffortLevel` | low\|medium\|high\|xhigh\|max (v2.1.267+) | F7 | [cc-managed-settings-levers] |
| managed-settings | `fastModePerSessionOptIn` | bool | F7 | [cc-sticky-escalation] |
| managed env | `CLAUDE_CODE_DISABLE_FAST_MODE` | "1" | F7 | [cc-managed-settings-levers] |
| managed-settings | `availableModels`, `enforceAvailableModels` | list, bool | F7 (proposed only) | [cc-managed-settings-levers] |
| managed-settings | `modelPricing` | contract overlay (**VERIFY** exact shape) | F1 | [cc-list-price-estimates] |
| managed-settings | `bashOutputMaxChars` | int | F13 | [cc-tool-output-bloat] |
| managed env | `MAX_MCP_OUTPUT_TOKENS` | int | F13 | [cc-tool-output-bloat] |
| managed env | `ENABLE_TOOL_SEARCH` | "true" | F10/F13 | [cc-gateway-cache-strip] |
| managed-settings | `skillListingBudgetFraction`, `claudeMdExcludes` | float, list | F13 | [cc-managed-settings-levers] |
| managed-settings | `cleanupPeriodDays` | int (raise so importers run before deletion) | F2 | [cc-transcript-format] |
| managed env | `OTEL_RESOURCE_ATTRIBUTES` | `tokenbill.arm=..,tokenbill.wave=..,team.id=..` | F9 | [assignment-telemetry-srm] |
| managed env | `OTEL_METRICS_INCLUDE_ENTRYPOINT` | "true" | F12 | [detect-signatures] |
| hooks | `SessionStart` command → `hooks/tokenbill_session_start.py` | | F5 | [channel-code-review-hooks] |
| LiteLLM | `litellm_params.cache_control_injection_points` (`location`, `role`, `index`); TTL key (**VERIFY**) | | F10 | [litellm-gateway-injection] |
| CI workflow | `concurrency: {group, cancel-in-progress: true}`, `paths`, `paths-ignore`, `if: !github.event.pull_request.draft` | | F12 | [ci-trigger-multiplier] |

### 4.7 Other providers and channels

| Provider | Facts | Finding |
|---|---|---|
| OpenAI GPT-5.6+ (released 2026-07-09) | Cache writes 1.25×, reads 0.1×. `prompt_cache_options.ttl='30m'` is the only value; reuse refreshes it for free. Minimum 1,024 visible tokens. Implicit mode = 1 automatic plus ≤ 3 explicit breakpoints; explicit mode ≤ 4; explicit mode with no breakpoints disables caching. Matching considers the first 2 and latest 50 explicit breakpoints. Usage is **inclusive**: ordinary = `input − cached − cache_write`. `reasoning_tokens` are billed as output. `prompt_cache_diagnostics` via `comparison_response_id` (nine miss reasons, free) | [oai-56-explicit-cache], [oai-usage-inclusive], [oai-cache-diagnostics] |
| OpenAI prices and tiers | gpt-5.6-sol $4 in / $0.40 cached / $5 write / $20 out; above 272K: $8 / $0.80 / $10 / $30. Flex 0.5× (flex 429s not charged). Fast (renamed from Priority 2026-07-30) 2×. Batch 0.5×. Regional processing +10% for models released on or after 2026-03-05. Sol promotional pricing runs at least through 2026-11-21 and was repriced 2026-08-21, so rows need **effective dates (VERIFY)** | [oai-service-tiers], [oai-batch-longctx-residency], [cc-openai-scale-tier], [gemini-effective-dated-prices] |
| OpenAI pre-5.6 | `in_memory` ~5–10 min idle (up to 1h). 24h retention is the default for non-ZDR orgs since 2026-05-29. `cached_tokens` rounded down to 128. No write fee. ~15 RPM per key | [oai-retention-defaults] |
| OpenAI Admin | `organization.usage.completions` buckets (`input_cached_tokens`, `input_cache_write_tokens`, `input_uncached_tokens`, `output`), grouped by project, user, key, model, batch, service_tier. The Costs endpoint groups by project, line item or key | [oai-admin-usage-costs] |
| Azure OpenAI | gpt-5.4 and older default to `in_memory`. No cache sharing across subscriptions. 50-breakpoint lookback. PTU-M lacks breakpoints and `cache_write_tokens`. Cached tokens don't consume PTU. Bills 400 content-filter and 408 timeouts; not 401/429 | [azure-openai-caching], [fp-billing-matrix] |
| Gemini | Implicit caching on 2.5+; minimum 2,048 (2.5), 4,096 (3.x), 6,144 (some 3.x on Vertex). Cached input = 10% of input. Explicit caches bill storage per MTok-hour: 3.1 Pro $4.50, 2.5 Flash $1.00, 3.8 Flash $0.50 promo to 2026-12-31. 3.1 Pro $2/$12 ≤200K, $4/$18 above. 3.8 Flash $0.75 input through 2026-12-31, then $1.50 from 2027-01-01 (caching, storage, Flex and Priority also double). Flex 0.5×, Priority ~1.8×, Batch 0.5×. `promptTokenCount` includes cached; `thoughtsTokenCount` is separate and billed as output. Doesn't bill 400/500 | [gemini-caching], [gemini-usage-semantics], [gemini-tiers], [gemini-effective-dated-prices], [gemini-bedrock-deepseek-rules] |
| Bedrock | Converse usage: `inputTokens` exclusive, `cacheReadInputTokens`, `cacheWriteInputTokens`, `cacheDetails[{ttl, inputTokens}]`. Minimums: 512 (Opus 5/5.5), 1,024 (Sonnet 5, Opus 4.8), 4,096 (Opus 4.7, Haiku 4.5). No caching in batch. Cross-region inference may increase writes. Tiers: reserved, priority (1.75×), default, flex (0.5×). The Price List API offer file `AmazonBedrockFoundationModels` has global vs regional usage types (e.g. Opus 5 input $5.00 global / $5.50 regional; 1h write $10 / $11; batch input $2.50). CUR 2.0 `line_item_iam_principal` since 2026-04-17. Invocation logs lack top-level cache counts and don't cover bedrock-mantle | [bedrock-cache-semantics], [bedrock-price-list-api], [bedrock-service-tiers], [bedrock-attribution], [ent-cloud-gateway-attribution] |
| Vertex (Claude) | Model id in the URL; `anthropic_version=vertex-2023-10-16`. No Message Batches and no Usage/Cost API. Request labels flow to the billing export. Opus 5.5 batch listed at $2.50/$12.50 vs $2/$10 on the 1P API (possible documentation lag; **VERIFY**) | [vertex-claude-geo-labels], [cc-channel-divergence] |
| xAI | `usage.cost_in_usd_ticks` (1 USD = 1e10 ticks) is billed ground truth. grok-4.7/4.6: $2 in / $0.50 cached / $6 out. At or above 200K, all rates double. Batch 20% off only on the grok-4.3 / 4.20 family | [xai-cost-ticks] |
| DeepSeek | deepseek-flash $0.003 hit / $0.15 miss / $0.60 out off-peak. v4-pro $0.022 / $0.66 / $1.98. Peak (01:00–04:00 and 06:00–10:00 UTC, Mon–Fri, excluding Chinese holidays) doubles every rate. Fields `prompt_cache_hit_tokens`, `prompt_cache_miss_tokens` | [deepseek-offpeak-cache] |
| Mistral | Opt-in caching via `prompt_cache_key`; 64-token blocks; cached = 10% of input; Batch 50% | [mistral-optin-cache] |
| OpenRouter | `usage.cost` is billed truth; `cache_discount` can be negative. Sticky routing 10 min | [openrouter-accounting] |
| OTel GenAI | `gen_ai.usage.input_tokens` **inclusive**; `gen_ai.usage.cache_read.input_tokens`; `gen_ai.usage.cache_write.input_tokens` (was `cache_creation` before the v1.42 repo move); `gen_ai.usage.reasoning.output_tokens`. Counters `gen_ai.client.inference.usage.*` require `gen_ai.token.modality`. Status: Development | [otel-genai-semconv], [ent-otel-genai-semconv] |
| Cursor | Token fee $0.25/MTok on third-party models (input, output **and cached**, including BYOK); Admin API `/teams/filtered-usage-events` | [cc-cursor-token-rate], [cursor-admin-api] |
| Copilot | AI Credits since 2026-06-01. Business $19 = 1,900 credits; Enterprise $39 = 3,900. Pooled; $0.01 overage. `ai_credits_used` is a metrics signal, not a bill | [cc-copilot-credits], [copilot-ai-credits] |

### 4.8 Anthropic reporting APIs (reconciliation and outcome sources)

| API | Facts | Finding |
|---|---|---|
| Usage report | `GET /v1/organizations/usage_report/messages`. Buckets 1m (max 1,440), 1h (max 168), 1d (max 31). Group/filter by `api_key_ids`, `workspace_ids`, `models`, `service_tiers`, `context_window`, `inference_geos`, `speeds`. Fields: uncached input, cache read, `cache_creation` 5m/1h, output, `web_search_requests`. Data within ~5 min; poll ≤ 1/min. Admin key. Not available on Claude Platform on AWS | [anth-admin-usage-cost-api] |
| Cost report | `GET /v1/organizations/cost_report`. Daily. USD in **cents as decimal strings**. Grouped by `workspace_id` or `description` (parsed model, cost_type, token_type, inference_geo). Priority Tier excluded. Code execution appears only here | [anth-admin-usage-cost-api], [ent-reconciliation] |
| Claude Code Analytics | `GET /v1/organizations/usage_report/claude_code?starting_at=YYYY-MM-DD`. Per user per day: sessions, LOC, commits, PRs, Edit/MultiEdit/Write/NotebookEdit accept and reject, per-model tokens, `estimated_cost` in cents. ~1h delay. 1P only | [anth-claude-code-analytics-otel], [cc-admin-analytics-apis] |
| Enterprise Analytics | `/v1/organizations/analytics/{usage_report,user_usage_report,cost_report,user_cost_report}` with scope `read:analytics`. `amount` (post-discount, pre-credit) and `list_amount`. Group by product, model, context_window, speed, inference_geo, rbac_group_id. Revisable for 30 days. Data from 2026-01-01. ≤ 31 days per query. 60 RPM | [anth-claude-code-analytics-otel], [cc-anthropic-discount-visibility] |

### 4.9 Verification constants

| Constant | Value | Finding |
|---|---|---|
| Calibration tolerances (FEMP Table 4-2 / ASHRAE G14) | Monthly `|NMBE| ≤ 5%`, `CV(RMSE) ≤ 15%`. Hourly/daily ±10% / ≤ 30% | [ashrae-calibration-gate] |
| CalTRACK FSU thresholds | ≤ 15% (NWA) / 25% (pay-for-performance); portfolio FSU = sqrt(ΣΔU²)/ΣU | [caltrack-fsu] |
| Sample-size rule | `n = 16·CV²·(1−ρ²)·DEFF/δ²` × 1.5 safety. 355·s² per arm for skewness s. ≥ 20 clusters per arm | [heavy-tails-power], [staggered-did] |
| SRM | χ², block if p < 0.001 | [assignment-telemetry-srm] |
| Confidence sequence | half-width = σ̂·sqrt(2(tρ²+1)/(t²ρ²)·ln(sqrt(tρ²+1)/α)); ρ² = (−2 ln α + ln(−2 ln α + 1))/t* | [sequential-monitoring] |
| Holdback | 10–25% never-treated; 5% long-run for 8–12 weeks | [stepped-wedge-mdm], [long-run-holdback] |
| Quality guardrail | One-sided 90% LB of Δ(merged PRs per dev-day) > −5%; revert rate +2 pp max | [value-adjusted-cost-metric] |
| Lab A/B sizing | ≈ 50 cases × 5 trials before a production cutover (bundled claude-api skill `cost-optimization.md`) | [eval-statistics] |
| Receipt signing | DSSE PAE; `ssh-keygen -Y sign -n tokenbill-receipt`; integer micro-USD | [signed-receipts] |

---

## 5. Work-package partition for parallel builders

### 5.1 Build protocol

- **15 packages:** WP0a and WP0b (foundation), then 13 parallel packages. Each package gets its own git worktree and branch (`wp/<id>`), and ~1–3k LOC including tests.
- **Disjoint ownership.** A package writes only the files listed under it. Read-only imports of other packages' modules are allowed **only through the WP0a protocols**. At test time, use the WP0b fakes (`FlatRates`, `ReferenceReplay`) instead of another package's implementation, so every package's tests pass on its own branch.
- **Phases.**

  | Phase | Packages | Notes |
  |---|---|---|
  | A | WP0a ∥ WP0b | Both code against §2.4 verbatim. Merge together |
  | B | WP1, WP2, WP3a, WP3b, WP4a, WP4b, WP5, WP6, WP7, WP8, WP9, WP11 in parallel | Merge in any order once green |
  | C | WP10 | Starts in phase B against fakes. Merges last and runs the cross-package integration and flagship v2 tests |
  | D | All | Adversarial review. Fixes go to the owning package branch |
- **Style.**
  - `from __future__ import annotations`, full type hints, frozen dataclasses, stdlib `logging`.
  - Every stochastic step is seeded via `common.rng` or an explicit seed.
  - `ruff` passes. No new runtime dependencies (a CI check greps `pyproject.toml`).
  - **No network in tests**: an autouse `pytest` fixture blocks `socket.connect` except to localhost fixture servers.
- **Definition of done** for every package:
  1. The listed acceptance tests pass on py3.10 and py3.13.
  2. The v0.1 test suite still passes.
  3. The WP0a conformance helpers pass for every implementation the package provides.
  4. No content canary leaks (for any package that touches ingest or output).
  5. Every public function has a docstring.
  6. Every dollar field is labeled.

### 5.2 Packages

#### WP0a: Core contracts (foundation)

- **Files.**
  - `tokenbill/core/{__init__,records,money,labels,ids,interfaces,registry,config,jsonio,text,mathx}.py`
  - Empty `__init__.py` for `rates/`, `sim/`, `optimize/`, `detect/`, `verify/`, `ledger/`, `out/`, `ingest/`, including `ingest/__init__.py`'s adapter-module list.
  - `pyproject.toml`: `--import-mode=importlib`; dev extras `pytest`, `ruff`; a `slow` marker.
  - `tests/v2/core/*`, `tests/v2/contracts/helpers.py`, `tests/conftest.py` (network block).
- **Provides.**
  - §2.4 records verbatim, plus `.to_json()` / `.from_json()` helpers.
  - `money.py`: nano ↔ micro ↔ Decimal-string conversions, `fmt_usd`, pico→nano rounding.
  - `labels.py`: `Label` ordering and combination rules; combining labels gives the weakest.
  - `ids.py`: `HmacKey` (load/generate, 0600, `key_id`), `pseudonym(kind, value)`, `stable_id(*parts)`.
  - `interfaces.py`: the `RateCard`, `ReplayEngine`, `Detector`, `Adapter`, `Emitter`, `Formatter`, `EvidenceRegistry`, `BillingRules` (`lookup(provider, platform, failure_mode) -> BillingRule`) and `PanelSource` (`panel(unit_kind, metric, window, rate_card) -> list[PanelRow]`) protocols, plus `AnalysisContext`, `IngestOptions`, `PanelRow(unit_id, day, cost_nano, active, prs_merged, reverts, arm, wave, pre_mean)` and `CalibrationStatus`.
  - `registry.py`: `register_*` decorators; `load_plugins()` only when enabled.
  - `config.py`: JSON config schema and loader with defaults (k=5, privacy=metadata, residency flags per cohort, TTL hints, thresholds).
  - `jsonio.py`: streaming JSONL reader (line cap, gzip, stdin, lenient quarantine), canonical writer, 0600 creation.
  - `text.py`: C0/C1 stripping, HTML escape.
  - `mathx.py`: exact `shapley(value_fn, players)` for k ≤ 10 and seeded permutation sampling above that; `quantile`; `weighted_median`; `bootstrap_indices(seed)`.
  - `tests/v2/contracts/helpers.py`: `assert_rate_card_conformance`, `assert_adapter_conformance`, `assert_detector_conformance`, `assert_no_canary(bytes_list)`.
- **Consumes.** Nothing new.
- **Size.** ~2.0k LOC.
- **Acceptance.**
  - Records round-trip to and from JSON losslessly.
  - `UsageRecord.total_input` and `.plus`.
  - Label combination.
  - HMAC determinism and key-file mode 0600.
  - Lenient JSONL quarantine.
  - Shapley exactness on a 3-player game (values (1,2,3) with a pairwise interaction → hand values).
  - Network-block fixture works.

#### WP0b: Core test harness and prompt shapes (foundation)

- **Files.** `tokenbill/core/{shapes,synth,testing}.py`, `tests/v2/core_harness/*`.
- **Provides.**
  - **`shapes.py`**: `shape_from_anthropic_request(system, tools, messages, *, key, token_estimator) -> PromptShape`. It:
    - renders wire-order bytes with `json.dumps(obj, ensure_ascii=False, separators=(",",":"))`, **not** key-sorted;
    - computes `h_sorted` via `common.canonical_json`;
    - computes `h_masked` using v0.1 `breakers.VOLATILE_PATTERNS` and records `volatile_classes`;
    - strips `cache_control` from hash inputs and extracts `CacheMarker`s;
    - collapses `tool_use`/`tool_result` runs into positions;
    - estimates tokens with a default chars-per-token table by (tokenizer gen, block kind): 4.7+ tool_result 2.5, text 3.6, tools JSON 2.7; legacy ×1.3. Images use pixels.
  - **`synth.py`**:
    - `synth_fleet(SynthSpec) -> list[Session]` plus `planted_truth(spec) -> dict[pattern, Truth]` with closed-form expected nano values under `FlatRates`. Patterns are listed in F19.
    - `synth_rollout(n_units, devs_per_unit, weeks, true_effect, waves, holdback, seed) -> panel rows` for WP8.
    - `synth_ab(tasks, trials, effect, seed)`.
  - **`testing.py`**:
    - `FlatRates` (a `RateCard` with $1/$5 per MTok, ×0.1 read, ×1.25/×2 writes, exact `unit_rates`);
    - **`ReferenceReplay`**: a deliberately simple, readable Mode U implementation of §2.8 steps 0–6 for TTL, keep-alive, compaction, cold resume, model map, `fast_mode_off` and `geo_global`. It is the **test oracle**;
    - canary injection helpers;
    - builders `mk_attempt`, `mk_request`, `mk_lane`, `mk_session`.
- **Size.** ~2.2k LOC.
- **Acceptance.**
  - The shape of a v0.1 demo `timestamp` call has `h_masked` equal across calls while `h` differs.
  - A moving `cache_control` marker does not change any hash (a regression test for [cb-moving-marker]).
  - A key-order swap changes `h` but not `h_sorted`.
  - Run collapsing puts 25 consecutive `tool_result` blocks in 1 position.
  - `synth_fleet` is byte-deterministic per seed.
  - `ReferenceReplay` reproduces the closed forms in F3's tests.

#### WP1: Rates, pricing catalog, billing rules, evidence registry

- **Files.** `tokenbill/rates/*.py`, `tokenbill/rates/data/*.json`, `tokenbill/pricing.py` (compat: rows added, API unchanged), `tests/test_pricing.py` (only `SPEC_TABLE` rows added), `tests/v2/rates/*`.
- **Provides.**
  - `RateCard` implementation(s): `load_card(paths, contract=None) -> RateCard`, `default_card()`.
  - `resolve()`.
  - `billing_rules.lookup(provider, platform, failure_mode)`.
  - `EvidenceRegistry` with `get(id)` and `stale()`.
  - CLI functions `cmd_pricing_show`, `cmd_pricing_verify`, `cmd_pricing_modelpricing`.
- **Consumes.** WP0a.
- **Size.** ~2.5k LOC, including ~600 lines of JSON data.
- **Acceptance.** F1 test list; `assert_rate_card_conformance(default_card())`; §4 values reproduced from data files by a table-driven test that reads §4.1 rows.

#### WP2: Claude Code ingestion (transcripts and OTel)

- **Files.**
  - `tokenbill/ingest/claude_code.py`, `tokenbill/ingest/claude_code_otel.py`
  - `tests/v2/ingest_cc/*`, `tests/fixtures/cc/*.jsonl`, `tests/fixtures/cc_otel/*.json`
- **Provides.** Adapters `claude-code` and `claude-code-otel` (registered); `cmd_import_claude_code`; self-check report dataclass.
- **Consumes.** WP0a/0b. Pricing is only used for the self-check `$`, via the injected `RateCard`; tests use `FlatRates`.
- **Size.** ~2.5k LOC.
- **Acceptance.** F2 test list, including the privacy canary test and `assert_adapter_conformance`.

#### WP3a: Trace formats, lanes, recorder v2

- **Files.**
  - `tokenbill/ingest/{trace_v1,trace_v2,lanes}.py`
  - `tokenbill/instrument.py` (v0.1 `Recorder` untouched; `RecorderV2` and `format=` added)
  - `tests/v2/ingest_trace/*`, `tests/v2/instrument_v2/*`
- **Provides.** Adapters `trace1` and `trace2`; `write_trace_v2(path, sessions)`; `reconstruct_lanes(requests) -> list[Lane]`; `RecorderV2`.
- **Consumes.** WP0a/0b (`core.shapes`); v0.1 `trace.read_trace`.
- **Size.** ~2.5k LOC.
- **Acceptance.** F17's trace, lanes and recorder tests; the v0.1 `test_instrument.py` still passes.

#### WP3b: Provider, OTel GenAI, Admin and gateway adapters

- **Files.** `tokenbill/ingest/{providers,otel_genai,anthropic_admin,openai_admin,litellm_logs,outcomes,conventions,fetch}.py`, `tests/v2/ingest_misc/*`, `tests/fixtures/providers/*`.
- **Provides.** Adapters:
  - `otel-genai`;
  - `anthropic-usage`, `anthropic-cost`, `claude-code-analytics`, `enterprise-analytics`;
  - `openai-usage`;
  - `litellm-spendlogs`;
  - `outcomes`.

  Also `normalize_usage(provider_payload, convention) -> UsageRecord`, the convention registry, and opt-in `fetch_*` functions (urllib, pagination, 31-day chunking, env-var keys) tested against a localhost fixture server.
- **Consumes.** WP0a.
- **Size.** ~2.5k LOC.
- **Acceptance.** F17's adapter tests, plus the §2.5 conventions table as table-driven sum-check tests.

#### WP4a: Mode U replay engine and calibration

- **Files.** `tokenbill/sim/{usage_replay,policies,calibrate}.py`, `tests/v2/sim/*`.
- **Provides.**
  - `UsageReplay` (`ReplayEngine`): fast int-pico implementation of §2.8 Mode U, including `stagger_fanout`, `batch`, `retry`, `tool_output_cap`, `restore_caching` and `shared_ci_prefix`.
  - `policy_from_json` / `policy_to_json`.
  - `calibrate(scope_rows) -> CalibrationReport`.
  - `ceiling(lanes, policy, ctx) -> SavingsEstimate`.
  - CLI functions `cmd_simulate`, `cmd_calibrate`.
- **Consumes.** WP0a/0b. Tests use `FlatRates`. **Differential test:** `UsageReplay` equals `ReferenceReplay` to the nano on 500 random synthetic lanes for all shared policies.
- **Size.** ~2.0k LOC.
- **Acceptance.** F3 test list plus the differential test plus the perf test.

#### WP4b: Optimizer, lever catalog, emitters

- **Files.** `tokenbill/optimize/{levers,search,shapley_run,realization,emit,cohorts}.py`, `tokenbill/optimize/templates/tokenbill_session_start.py`, `tests/v2/optimize/*`.
- **Provides.**
  - The lever catalog (Appendix B).
  - `optimize(sessions, findings, ctx, opts) -> OptimizeResult`, containing per-cohort `PolicySpec`, Shapley credit, RR intervals and `PolicyPatch`es.
  - Emitters `claude_code_managed_settings`, `litellm_proxy`, `claude_code_hook` (copies `optimize/templates/tokenbill_session_start.py` to `<out-dir>/<cohort>/hooks/`) and `ci_workflow`.
  - `cmd_optimize`.
- **Consumes.** The WP0a `ReplayEngine` protocol. Tests use `ReferenceReplay`; integration uses `UsageReplay`.
- **Size.** ~2.5k LOC.
- **Acceptance.** F8 tests, the F5 hook tests, and `test_patch_known_keys` against §4.6.

#### WP5: Mode B block replay, cache rule tables, block breakers, token calibration

- **Files.** `tokenbill/sim/{block_replay,cachemodels,tokens}.py`, `tokenbill/detect/breakers_blocks.py`, `tests/v2/blocks/*`.
- **Provides.**
  - `BlockReplay` (`ReplayEngine`, mode `blocks`): 4 breakpoints, 20-position lookback, TTL from start with refresh, first-token visibility, workspace/org scope, tiered invalidation, per-channel minimums, the OpenAI GPT-5.6 rule table, and `BreakpointPolicy` search (greedy over block boundaries, ≤ 4).
  - `tokens.calibrate_cpt(lanes) -> table`: least squares of observed Δctx vs Σ appended chars per (tool class, tokenizer gen); publishes the MAPE.
  - Detectors C15, C17–C25 with block repairs.
- **Consumes.** WP0a/0b (`shapes`, `FlatRates`).
- **Size.** ~3.0k LOC.
- **Acceptance.** The Mode B rows of F10, including the v0.1 codebase experiment regressions (exp1, exp2b-a2, exp2b-a3, exp6-2, exp9-1) rebuilt as fixtures. Agreement ≥ 0.99 on the v0.1 demo `well-behaved` scenario.

#### WP6: Detectors for context, routing, carry and compression

- **Files.** `tokenbill/detect/{context,routing,carry,compression}.py`, `tests/v2/detect_a/*`.
- **Provides.** Detectors X01–X09, R01–R09, K01 (Appendix A) and the `cmd_compress_econ` function.
- **Consumes.** WP0a/0b. Tests use `FlatRates` plus `ReferenceReplay` with `synth` patterns.
- **Size.** ~2.8k LOC.
- **Acceptance.** F4, F5 (detector part), F6, F7, F13 and F14 tests. Each detector passes `assert_detector_conformance` and the precision/recall = 1 planted-pattern test.

#### WP7: Detectors for usage-level breakers, failure path, automation and frameworks

- **Files.** `tokenbill/detect/{breakers_usage,failure,automation,frameworks}.py`, `tests/v2/detect_b/*`.
- **Provides.** Detectors C01–C14, C16, C26, C27; D1–D9; A01–A09; W01–W03 (Appendix A); the workload classifier `classify_workload(session) -> (Workload, confidence)`.
- **Consumes.** WP0a/0b. Billing rules come via the `RateCard`-adjacent `BillingRules` protocol in WP0a; tests use a fake.
- **Size.** ~2.8k LOC.
- **Acceptance.** F10 (usage rows), F11 and F12 tests, plus the diagnostics confusion-matrix test.

#### WP8: Verification suite

- **Files.** `tokenbill/verify/{stats,ab,did,cuped,sequential,rollout,rr_db,receipts}.py`, `tests/v2/verify/*`.
- **Provides.** `cmd_ab`, `cmd_plan_rollout`, `cmd_verify`, `cmd_receipt`; `rr_db.update/prior(lever_class)`; the pure-function estimators (CS ATT(g,t), BJS imputation, CUPED, lag-p Hajek, cluster bootstrap, Wilcoxon, SRM χ², CS boundary).
- **Consumes.** WP0a/0b (`synth_rollout`, `synth_ab`). Panel inputs are plain dataclasses, so there is no dependency on the ledger.
- **Size.** ~3.0k LOC. The research sims implement the same estimators in ~600 lines (`research/causal_sim/*.py`) and serve as a reading reference.
- **Acceptance.** F9 test list.

#### WP9: Ledger store, reconciliation, aggregation, exports

- **Files.** `tokenbill/ledger/{store,reconcile,aggregate,focus,otlp_out,esr}.py`, `tests/v2/ledger/*`.
- **Provides.**
  - `Ledger(path)`: `upsert_sessions`, `upsert_buckets`, `upsert_invoice`, `upsert_activity`, `iter_sessions(filters)` (streaming), `panel(unit, metric, rate_card)` (for WP8), `purge`.
  - `reconcile(...) -> ReconcileReport`.
  - `aggregate(groups, k) -> SuppressedGroups`.
  - `esr(...)`.
  - `export_focus`, `export_otlp`.
  - `cmd_ledger`, `cmd_reconcile`, `cmd_export`, `cmd_purge`.
- **Consumes.** WP0a. Tests use `FlatRates`.
- **Size.** ~2.5k LOC.
- **Acceptance.** F15 and F16 test lists; idempotent re-ingest (ingest twice, then totals unchanged); `Σ run totals == Σ call totals`; 20k attempts/s insert benchmark (slow marker).

#### WP10: Pipeline, outputs, CLI, `check`, fleet demo, flagship v2

- **Files.**
  - `tokenbill/scan.py`, `tokenbill/cli.py`, `tokenbill/demo_fleet.py`, `tokenbill/__init__.py` (version)
  - `tokenbill/out/{schema,terminal,html,sarif}.py`
  - `tests/v2/pipeline/*`, `tests/v2/cli/*`, `tests/v2/flagship/test_flagship_v2.py`
- **Provides.** `run_scan(opts) -> ScanResult`, which runs ingest → ledger → price → calibrate → detectors → optimize → aggregate → render. Also every CLI verb in §2.11 wired to its owner's `cmd_*` function; `check`; `demo --fleet`; `analyze --v2`.
- **Consumes.** Every package's public functions, at merge time.
- **Size.** ~3.0k LOC.
- **Acceptance.**
  - F18 and F19 test lists.
  - v0.1 `demo` / `analyze` golden bytes unchanged.
  - The CLI exit-code matrix.
  - An end-to-end `ingest → scan → optimize → plan-rollout → verify` run on synthetic data produces a signed receipt (ssh-keygen present) and label `verified`.
  - The same run with a planted SRM gives exit 3 and label `measured`.

#### WP11: Supply chain, docs, privacy pack

- **Files.**
  - `.github/workflows/*`: SHA-pinned actions, top-level `permissions: {}`, py3.10/3.12/3.13/3.14 × Linux/macOS/Windows matrix, nightly slow tests, weekly `pricing verify --fetch` job opening an issue on drift, CycloneDX SBOM (`uv export`), `attest-build-provenance`, immutable release, Scorecard, CodeQL, zizmor.
  - `tokenbill/privacy_pack.py` and templates under `tokenbill/privacy_pack_data/`: DPIA draft, works-council annex ("not for performance evaluation", access matrix, retention), employee notice, data inventory [labor-law-constraints], [ent-eu-cra].
  - `SECURITY.md` (supply-chain posture, CRA-compatible vulnerability-handling SLAs), `docs/VERIFY.md`, `docs/PRIVACY.md`, `docs/TRACE_V2.md`, `README.md` (rewrite Related work [cc-usage-likely-cause]), `DESIGN.md` (v0.2 methodology), `CHANGELOG.md`, `Makefile`.
- **Consumes.** None at build time. Docs describe the CLI of §2.11.
- **Size.** ~1.2k LOC plus docs.
- **Acceptance.**
  - `actionlint`-clean workflows. Every `uses:` is pinned to a 40-hex SHA (test greps it).
  - `privacy-pack` generates 4 files with jurisdiction presets.
  - README contains no "silent on why" claim.
  - SBOM job produces a CycloneDX file in CI.

### 5.3 Dependency graph and interfaces at a glance

```text
WP0a ─┬─► all
WP0b ─┘ (shapes, synth, FlatRates, ReferenceReplay)
WP1  (RateCard, EvidenceRegistry, BillingRules) ─────────────┐
WP4a (UsageReplay) ──► differential-tested vs ReferenceReplay │
WP5  (BlockReplay, block detectors) ──────────────────────────┤
WP2, WP3a, WP3b (Adapters → Session/Bucket/Invoice/Activity) ─┤──► WP10 scan.py wires via AnalysisContext
WP6, WP7 (Detectors → Finding) ───────────────────────────────┤
WP4b (optimize → PolicySpec, PolicyPatch) ────────────────────┤
WP9  (Ledger, reconcile, aggregate, exports) ─────────────────┤
WP8  (verify: panel in → Receipt out; reads Ledger.panel) ────┘
WP11 (CI, docs, privacy pack) — independent
```

Cross-package runtime calls happen **only** in `scan.py` and `cli.py` (WP10), except:
- WP4b calls `ctx.replay` (protocol);
- WP6/WP7 call `ctx.replay` and `ctx.rates` (protocol);
- WP8 calls `Ledger.panel` through the `PanelSource` protocol defined in WP0a.

### 5.4 Size summary

| WP | Title | LOC incl. tests | Phase |
|---|---|---|---|
| 0a | Core contracts | 2.0k | A |
| 0b | Core harness and shapes | 2.2k | A |
| 1 | Rates | 2.5k | B |
| 2 | Claude Code ingestion | 2.5k | B |
| 3a | Trace formats, lanes, recorder v2 | 2.5k | B |
| 3b | Provider / OTel / Admin adapters | 2.5k | B |
| 4a | Mode U engine and calibration | 2.0k | B |
| 4b | Optimizer and emitters | 2.5k | B |
| 5 | Mode B engine and block breakers | 3.0k | B |
| 6 | Detectors: context / routing / carry / compression | 2.8k | B |
| 7 | Detectors: breakers / failure / automation | 2.8k | B |
| 8 | Verification | 3.0k | B |
| 9 | Ledger, reconcile, exports | 2.5k | B |
| 10 | Pipeline, outputs, CLI, demo | 3.0k | C |
| 11 | Supply chain, docs, privacy | 1.2k + docs | B |
| | **Total** | **≈ 37k** | |

If capacity forces cuts, drop in this order (lowest dollar impact per LOC first). The Mode U levers (F4–F9), the ledger (F1, F2) and privacy (F16) are never cut.
1. WP3b's OpenAI Admin and LiteLLM importers
2. WP5's OpenAI rule table
3. WP7's framework detectors W01–W03
4. WP6's R09 router audit
5. WP8's switchback estimator (CS, imputation and CUPED-DiM stay)

---

## 6. Deferred items, risks and mitigations

### 6.1 Explicitly deferred (and why)

| # | Item | Why deferred | What v0.2 does instead | Finding |
|---|---|---|---|---|
| 1 | Recording HTTP proxy (`ANTHROPIC_BASE_URL`) | Sits in the inference path: SSE fidelity, availability and security-review burden. Gateways are becoming security products | RecorderV2 in-process; OTel and transcript ingestion; LiteLLM log import. The proxy interface is designed in trace@2 | [cb-recorder-coverage], [portkey-panw-gateway-security], [ent-supply-chain-incidents] |
| 2 | Hosted dashboard, SSO/RBAC/SCIM | Server attack surface; ASVS scope | Static per-team showback HTML behind the enterprise SSO proxy; FOCUS/OTLP into existing FinOps tools | [ent-server-sso-rbac], [finops-platforms-export-target] |
| 3 | Live `count_tokens` exact attribution and `rebaseline --to MODEL` | Needs API keys and network; costs rate limit | Chars-per-token calibrated from billed deltas (WP5 `tokens.py`); `token_basis` field ready for `count_tokens`; tokenizer band in what-ifs | [count-tokens-exact-attribution], [tokenizer-inflation-47plus] |
| 4 | Paid live calibration suite (plant each breaker against the real API) | Needs keys and money; must run under the org's control | Recorded-fixture regression tests of every v0.1 experiment; diagnostics confusion matrix on real traffic | [cb-cache-diagnostics] |
| 5 | Effort / model sweep runner (live frozen-request replays) | Live spend; needs a grader and approvals | `tokenbill ab` over org-run campaigns; published effort curves as labeled priors | [anth-effort-sweep], [eval-statistics] |
| 6 | Commercial commitment optimizer (seat assignment, capacity sizing, contract ledger, renewal calendar, marketplace drawdown) | Contract data is org-private and heterogeneous; the lever is contract-dependent | Realized discount (`reconcile`), channel/geo premium, Priority-Tier reconciliation | [cc-sizing-newsvendor], [cc-seat-vs-usage], [cc-renewal-traps], [cc-marketplace-drawdown], [cc-capacity-parity] |
| 7 | Codex, Gemini CLI, Copilot and Cursor adapters | Claude Code is the largest line item; OTel GenAI covers part of the rest | Convention registry and adapter protocol ready; OTel GenAI import | [codex-cli-telemetry], [gemini-cli-telemetry], [copilot-ai-credits], [cursor-admin-api] |
| 8 | Bedrock CUR 2.0, Vertex billing export and Azure Cost Management reconcilers | Per-cloud formats; no request ids in CUR | Anthropic Admin reconcile; Bedrock/Vertex pricing and regional premium; the invocation-log adapter is designed | [bedrock-attribution], [cc-reconciliation-matrix] |
| 9 | Streaming anomaly `watch` and quantile forecasts | Needs a scheduler and alert sinks | Batch `scan` with deltas; a runaway detector (X07) sized for gateway caps | [ent-anomaly-detection], [ent-forecast-budgets], [runaway-circuit-breaker] |
| 10 | Semantic, response and plan caching recommendations | Correctness and security risk; rarely fits coding agents | Exact-duplicate counter only | [semantic-cache-pitfalls], [agentic-plan-caching] |
| 11 | Learned or kNN routers | Need outcome labels at scale; many routers don't beat baselines | Router audit (R09) nets price against cache loss | [router-baselines-knn], [routellm] |
| 12 | Eval subset sizing (IRT), embeddings module, multi-agent pruning, intent flame graphs, multilingual lens | P2 by dollar share, or research-grade | Judge share and batch eligibility for evals; hooks left in the schema | [eval-subset-irt], [emb-scope-decision], [mas-pruning-research], [semantic-agent-profiling], [multilingual-tokenization] |
| 13 | Content-based detectors (prompt-cruft linter, PTC/Files opportunities, image downscale by pixels) | Need content, or image header parsing in the importer | Output-shape and truncation detectors; image-eviction breaker; tool-schema bloat | [anth-prompt-audit-migration], [anth-ptc-code-exec], [anth-vision-tokens] |
| 14 | Live gateway and MCP conformance probes | Send live traffic | Usage-signature detectors C02, C03, C04; `tools_h` churn per MCP change | [gateway-strip], [mcp-spec-cache-hints] |
| 15 | Hidden-token audit signals | Statistical at best; evadable | Three-source consistency check (response usage vs usage report vs cost report) in reconcile | [hidden-token-audit-research], [audit-evasion-limits] |
| 16 | Cap collateral-damage simulator; Slack DMs; status-line script | Behavioral, low and decaying effect; delivery channels are org-specific | SessionStart hook (decision point); showback; cap recommendations as text | [blunt-caps-collateral], [peer-comparison-outliers], [price-salience-mixed] |
| 17 | Any LLM call inside Token Bill ("explain this run") | The cost tool must not become a cost center; privacy | Deterministic, content-free analysis only | [observer-compiled-views] |

### 6.2 Risks and mitigations

| # | Risk | Impact | Mitigation in this design |
|---|---|---|---|
| R1 | **Overclaiming.** Price-only replays are ceilings, engineering projections are optimistic (2.5× in energy efficiency), and token cuts can raise bills | Loss of finance trust; negative-ROI rollouts | Difference-of-replays principle; calibration gate with "uncalibrated" tags; RR intervals per lever class; Shapley credit only; `estimated` label on every projection; `verify` with guards before `verified`; RR database learns from receipts [projection-optimism], [llm-token-not-bill], [shapley-attribution] |
| R2 | **One-user corpus bias.** $215/day vs a $13/day fleet average | Planning numbers are wrong for light users | Every corpus % is presented as a mechanism. Planning priors are explicitly labeled. The week-1 `scan` replaces them. Recommend a 20–50-volunteer pilot across subscription, API-key and Bedrock users [cc-heavy-tail-concentration], [cc-enterprise-telemetry-gap] |
| R3 | **Claude Code format drift.** Transcripts are internal and version-changing; OTel attribute renames | Silent miscounts | Versioned golden fixtures; unknown-type counters with warnings; version histogram; OTel as the second path; `cleanupPeriodDays` guidance; dedup and max-output rules covered by tests [cc-transcript-format] |
| R4 | **Pricing drift.** Three price moves in one quarter | Wrong dollars | Effective-dated, sourced rows; VERIFY rows disabled; weekly `pricing verify --fetch` CI opens an issue; evidence staleness warnings; reconcile residuals [anth-evidence-drift], [price-feed-crosscheck] |
| R5 | **Privacy and labor law.** Works councils, GDPR Art. 88, EU AI Act Annex III | Rollout blocked | Metadata-only default; HMAC with a separate key; k ≥ 5 with complementary suppression; self-view only; no ranking and no emotion inference; canary tests; purge; privacy pack [labor-law-constraints], [aggregation-k5], [ent-privacy-by-default] |
| R6 | **Quality regressions from trajectory levers** (compaction, model, effort, output caps) | Cheaper but worse output; hidden re-acquisition | `requires_eval` and never auto-applied; quality guardrails in `verify` (merged PRs, reverts); a post-compaction re-acquisition metric; long holdback [quality-guardrail], [hidden-reacquisition], [compaction-quality-tokens-per-task] |
| R7 | **Interference and carryover** bias verification | Savings under- or over-stated by 28–40% | Randomize at the cache-isolation boundary; washout ≥ max(TTL, session p90, settings refresh); daily switchback blocks only; refuse user-level units for cache levers [cache-interference-cluster], [switchback-carryover] |
| R8 | **Underpowered fleets.** MDE ~13.6% at 1,000 developers | "Verified" claims impossible for small levers | The A/A MDE preview refuses verification designs with MDE > 0.8× the projection; `measured` via ITS/synthetic control for org-wide changes; CUPED by default [heavy-tails-power], [cuped], [its-rdit-org-wide] |
| R9 | **Parallel-build integration.** 15 packages merging | Broken interfaces, duplicated semantics | Frozen WP0a records; protocols plus fakes; `ReferenceReplay` oracle with differential tests; conformance helpers; the v0.1 suite on every branch; WP10 merges last with end-to-end tests |
| R10 | **Scale.** Thousands of developers, 10^6+ attempts per month | Timeouts or OOM | Streaming per session; int-pico math; O(n) replay; sampled search with a full final replay; Shapley only on interacting lanes; SQLite WAL; perf tests in CI [cb-scale-memory] |
| R11 | **Supply chain.** The LiteLLM precedent | Security rejection | Zero runtime deps; plugins off by default; network off by default with a test-time socket block; SHA-pinned CI; SBOM; attestations; Scorecard [ent-supply-chain-incidents], [ent-provenance-sbom] |
| R12 | **Modifier stacking and rounding unverified** against invoices | Small reconciliation residuals | Fixed documented order; residual shown by cause; tolerance-based pass; basis labels [papers-measurement open questions] |
| R13 | **Diagnostics labels are not 1:1 ground truth.** In the corpus, 341 of 802 misses were labeled; `unavailable` / `not_found` are non-comparisons | Misleading precision claims | Map only the `*_changed` reasons; publish label coverage next to agreement [cc-miss-taxonomy-ground-truth] |
| R14 | **Settings don't take effect.** `--autocompact` isn't preempted; gateways drop the beta header; server-managed settings can't target groups | Projected savings never realized | Env-block duplication; post-rollout effectiveness checks (e.g. share of 1h writes); per-cohort MDM or endpoint files; gateway checklist in patches [cc-managed-settings-levers], [stepped-wedge-mdm] |
| R15 | **Goodhart on units** (cost per PR) | Gamed metrics | Primary unit is cost per active dev-day, ITT; per-PR and per-task units secondary and marked gameable; quality weighting [units-gaming], [value-adjusted-cost-metric] |
| R16 | **Abort and 5xx billing is undocumented** on Anthropic | Failure-path $ uncertain | The billing-rules table carries confidence; assumed dollars are shown separately as a range [fp-billing-matrix] |
| R17 | **Leaderboard misuse** of Token Bill output | Induced waste (Meta, Amazon precedents) | No per-person arrays in any output; team views only; a "Goodhart audit" note in the privacy pack [leaderboard-goodhart], [vendor-leaderboards] |

---

## Appendix A: Detector catalog (v0.2)

Column key:
- **Req**: minimum evidence level. U = usage, B = blocks.
- **$**: pricing method.
- **GT**: diagnostics reason used as ground truth.

**Cache breakers**

| ID | detector_id | Req | Trigger | $ | Fix (gated) | Class | GT | WP |
|---|---|---|---|---|---|---|---|---|
| C01 | cache.ttl_expiry | U | `gap > ttl` and `cw ≥ 0.5·prev_ctx` | `min(cw, prev_ctx)·(w−r)` | TTL advisor / keep-alive (SDK) / cold-resume hook | cache_transform | previous_message_not_found | 7 |
| C02 | cache.gateway_strip | U | ≥3 consecutive requests with `cr = cw = 0`, `ctx ≥ min_cacheable`, route = gateway or unknown | replay `restore_caching` | Forward `cache_control` and block-form system unchanged; LiteLLM injection points | cache_transform | — | 7 |
| C03 | cache.ttl_header_dropped | U | 1h configured or recommended, but only 5m writes on a gateway route | replay 1h | Forward `anthropic-beta`; the Claude apps gateway can't do 1h | cache_transform | — | 7 |
| C04 | cache.mcp_prefix_churn | U | `mcp_change` or `tools_h` change followed by a miss within TTL; gateway base URL (tool search off) | rebuild premium | `ENABLE_TOOL_SEARCH=true`; deferred MCP; avoid `alwaysLoad` | cache_transform | tools_changed | 7 |
| C05 | cache.model_switch | U | Served model changes mid-lane with no compaction or clear | rebuild premium | Switch at `/clear` or compaction; use a subagent; avoid opusplan on large contexts | trajectory | model_changed | 7 |
| C06 | cache.fallback_cache_loss | U | `model_fallback` / `fallback_message` and the new model writes ≥80% of the prefix without a credit | `prefix·(w_B − r_B)` | Fallback credit or `fallbacks:"default"`; same-family, sticky fallbacks | cache_transform | model_changed | 7 |
| C07 | cache.param_churn | U | effort / thinking / speed / tool_choice / output_format / task_budget changes, then a miss | invalidated-tier rebuild | Per-message effort (Fable 5.1, Mythos 5.1, Opus 5, Opus 5.5); speed per session; `fastModePerSessionOptIn` | cache_transform | unavailable, messages_changed | 7 |
| C08 | cache.compaction_cold | U | Compaction event with the preceding gap > ttl | `pre·(w−r)` | Compact while warm; `/clear` when cold | behavioral | — | 7 |
| C09 | cache.context_edit_churn | U | `context_edit` with remaining requests < K* | rebuild − savings | `clear_at_least`; prune at task boundaries | cache_transform | messages_changed | 6 |
| C10 | cache.write_never_read | U | Written prefix not read before expiry or lane end | `tokens·(α−1)·in` | Move the breakpoint; explicit mode; 5m for short bursts | cache_transform | — | 7 |
| C11 | cache.cold_fanout | U | ≥2 lane-first requests within the TTFT window, same domain and model, all writing | `(N−1)·shared·(w−r)` | Send 1, await the first token, send N−1 | cache_transform | — | 7 |
| C12 | cache.workspace_fragmentation | U/B | Same first-call prefix signature written in ≥2 cache domains within TTL | duplicate writes | Consolidate (namespace after the public prefix; about +2% cost) | cache_transform | — | 7 |
| C13 | cache.ci_per_machine_prefix | U | CI lanes rewrite the static prefix every run; runs < TTL apart | replay `shared_ci_prefix` | `--exclude-dynamic-system-prompt-sections`; one workspace; pinned CLI; 1h TTL | cache_transform | system_changed | 7 |
| C14 | cache.thinking_strip | U | `thinking_dropped` or older-model user-turn strip, then a miss | rebuild premium | Upgrade model; keep thinking blocks | trajectory | messages_changed | 7 |
| C15 | cache.lookback_overflow | B (U: suspicion) | Breakpoint >20 positions past the last write, prefix identical | rewrite premium | Intermediate breakpoint about every 15 positions | cache_transform | — | 5 |
| C16 | cache.harness_upgrade | U | Harness version changes in-lane, then a miss | informational | Upgrade at session boundaries (`requiredMinimumVersion` timing) | — | any | 7 |
| C17 | cache.volatile_system | B | System `h` differs, `h_masked` equal, `volatile_classes` non-empty | repair replay | Move to the user turn / mid-conversation system (not Sonnet 5) / date-only | cache_transform | system_changed | 5 |
| C18a | cache.tool_order_churn | B | Same tool set, different order | repair replay | Deterministic sort (MCP spec SHOULD) | cache_transform | tools_changed | 5 |
| C18b | cache.tool_subset_churn | B | Tool set add/remove | repair replay (constant superset) | `tool_addition` (beta, gated) / `defer_loading` / `allowed_tools` | cache_transform | tools_changed | 5 |
| C18c | cache.tool_definition_drift | B | Same names, definition hash changed | repair replay | Pin schemas | cache_transform | tools_changed | 5 |
| C19 | cache.serialization_churn | B | `h` differs, `h_sorted` equal | repair replay | `sort_keys=True`; deterministic schema serialization | cache_transform | tools/messages_changed | 5 |
| C20 | cache.history_rewrite | B | Earlier message block changed with no compaction/edit/clear event; framework attribution | repair replay | Append-only; trim in fixed blocks; server-side `clear_tool_uses` | cache_transform | messages_changed | 5 |
| C21 | cache.breakpoint_misplacement | B | Breakpoint search beats as-is markers by ≥10% | replay | Explicit breakpoint at the end of the shared portion; static + tail | cache_transform | — | 5 |
| C22 | cache.missing_breakpoint | B | No markers, stable prefix ≥ min, zero cache activity | replay | Add a breakpoint or automatic caching | cache_transform | — | 5 |
| C23 | cache.dynamic_before_stable | B | A per-call-changing block precedes stable blocks | repair replay | Move dynamic content after the breakpoint | cache_transform | system/messages_changed | 5 |
| C24 | cache.reminder_inject_delete | B | Injected reminder later deleted | repair replay | `clear_at` reminders (beta) | cache_transform | messages_changed | 5 |
| C25 | cache.image_eviction | B/U | `image_eviction` event, then a rebuild | informational | Fewer or downscaled screenshots | — | messages_changed | 5 |
| C26 | cache.openai_write_waste | U | OpenAI 5.6: writes per request ≈ unique tail and write:read > 5 | replay explicit | Explicit breakpoint at the end of the stable prefix | cache_transform | openai reasons | 7 |
| C27 | cache.openai_key_oversplit | U | Per-run `prompt_cache_key` (`agents-sdk:run:`) with a shared prefix, pre-5.6 | replay | `RunConfig(group_id)` | cache_transform | — | 7 |

**Failure path (WP7)**

| ID | detector_id | Rule and dollars | Fix |
|---|---|---|---|
| D1 | failure.retry_storm | >3 attempts per logical request, or retries at ≥2 layers, or ≥5 `api_error` per thread in 10 min. $: billed failed attempts per billing rule | One retry owner; ≤3 attempts; jitter |
| D2 | failure.cold_retry_after_backoff | Retry start − first start > ttl and retry `cw ≥ 0.5·prefix`. $: `prefix·(w−r)` | Backoff budget < TTL − generation; defer on long `retry-after`; 1h for watchdog/CI; stream |
| D3 | failure.abort_waste | Aborted or timeout; sub-causes network (300–360 s silence), watchdog, user. $: input + streamed per rule, plus the re-send | Keepalive; stream; resume from partial text |
| D4 | failure.never_succeeding_400 | Same error class and `input_h` ≥2, or a retry after `x-should-retry:false` or a spend-cap 429 | Treat as terminal; `drop_block`; preflight; `/rewind` |
| D5 | failure.fallback_cache_loss | = C06, with A→B→A ping-pong | Fallback credit; sticky fallback |
| D6 | failure.tool_error_loop | ≥3 consecutive `is_error`, or the same `input_h` failing ≥2. $: consuming calls plus error-text carry | PreToolUse stop-after-2; filter test output; turn caps |
| D7 | correctness.missing_final_usage | `message_start_only` followed by a tool_result (not waste) | Accounting fix; reconcile |
| D8 | failure.nonstreaming_double_send | Two attempts with the same body, the first with 0 events | Pass SSE unmodified through the gateway |
| D9 | failure.bedrock_quota_reservation | Bedrock 429 and `max_tokens ≥ 4×` p99 output | Right-size `max_tokens` |

**Automation (WP7)**

| ID | detector_id | Rule | Fix |
|---|---|---|---|
| A01 | auto.workload_share | Classifier KPI and the attribution-kit lint | `OTEL_RESOURCE_ATTRIBUTES` workload tags; `OTEL_METRICS_INCLUDE_ENTRYPOINT` |
| A02 | auto.review_trigger_multiplier | Runs per PR, superseded runs, `run_attempt > 1`, drafts and bots | Review once on ready + opt-in re-review; `concurrency` with cancel-in-progress; path filters |
| A03 | auto.ci_cross_run | = C13 | As C13 |
| A04 | auto.scheduled_cadence_gt_ttl | Periodic series with interval > ttl | 1h TTL; smaller context; event triggers |
| A05 | auto.noop_fires | A fire with no state change | Event-driven triggers |
| A06 | auto.batch_eligible | Single-shot, no human waiting, ≥1h slack | Message Batches / flex (0.5×) |
| A07 | auto.judge_share | Judge calls ≥ model-under-test cost | Haiku or panel judges; batch the judges |
| A08 | auto.dev_test_bleed | Automated traffic on prod keys; frontier models in test envs | Key scoping; workload budgets |
| A09 | auto.caps_from_p99 | Workflow p99 of $ and turns | `--max-turns` / `--max-budget-usd` at p99 (damage limit, not savings) |

**Frameworks (WP7)**

| ID | detector_id | Rule | Fix |
|---|---|---|---|
| W01 | fw.runaway_loop_ceiling | Calls per run > 3× the agent's p95, or ends in GraphRecursionError / MaxTurns (LangGraph ≥1.0.6 defaults to 10,007) | Explicit `recursion_limit`; call-limit middleware [langgraph-recursion-10007], [loop-defaults-matrix] |
| W02 | fw.step_repetition | Same tool and `input_h` ≥3 in a run (MAST FM-1.3) | Loop guard [mast-loop-failures] |
| W03 | correctness.telemetry_defect | Convention mismatch or sum-check failure (e.g. the CrewAI 17-token bug, multiplied usage) | Recompute from provider buckets [fw-telemetry-conventions] |

**Context, routing, carry, compression (WP6)**

| ID | detector_id | Rule | Lever |
|---|---|---|---|
| X01 | context.tax | $ of reads/writes beyond 100k/200k/400k (exact) | — |
| X02 | context.governor | §F4 | cc.autocompact_window |
| X03 | context.cold_resume | §F5 | cc.cold_resume_hook |
| X04 | context.ttl_advisor | §F6 | cc.prompt_cache_ttl.* / sdk.keepalive |
| X05 | carry.tool_output | §F13 carry by tool | cc.tool_output_cap |
| X06 | carry.config_tax | §F13 attachments (first listing only) | cc.tool_search / skill budget |
| X07 | context.heavy_tail | Top-5% session share; spend velocity > 5× the personal hourly baseline (private alert) | — |
| X08 | context.agent_spinup_tax | First-call prefix $ per agent type | fanout.stagger / shared spawn prefix |
| X09 | carry.duplicate_reads | Repeat `result_h` of Read on an unchanged file (counter only) | — |
| R01 | routing.same_tier | §F7-1 | model.same_tier_upgrade |
| R02 | routing.delegation | §F7-2 | cc.subagent_model |
| R03 | routing.effort | §F7-3 | cc.max_effort |
| R04 | routing.sticky_escalation | §F7-4 (self-view) | cc.fast_mode_opt_in, cc.max_effort |
| R05 | premium.fast_mode | §F7-5 (exact) | cc.fast_mode_opt_in |
| R06 | premium.geo | §F7-6 (exact; policy decision) | geo.global |
| R07 | routing.cost_per_pass | §F7-7 | (eval-gated) |
| R08 | output.truncation_waste | §F7-8 | max_tokens 64K |
| R09 | routing.router_audit | §F7-9 | — |
| K01 | compression.economics | r*, K*, affordable extra turns | — |

## Appendix B: Lever catalog (`optimize/levers.py`)

| lever_id | Class | Grid | Policy component | Patch target and keys | requires_eval |
|---|---|---|---|---|---|
| cc.autocompact_window | trajectory | 150k, 200k, 300k, 400k, 500k, 700k | CompactionPolicy | managed `autoCompactWindow` + env `CLAUDE_CODE_AUTO_COMPACT_WINDOW` | yes |
| cc.disable_1m_context | trajectory | on (window ≈ 168k, the observed 200k-model trigger) | CompactionPolicy | env `CLAUDE_CODE_DISABLE_1M_CONTEXT=1` | yes |
| cc.prompt_cache_ttl.main | cache_transform | 5m, 1h | TtlPolicy(main) | `promptCacheTtl` | no |
| cc.prompt_cache_ttl.subagent | cache_transform | 5m, 1h | TtlPolicy(subagent, workflow_agent) | `subagentPromptCacheTtl` | no |
| sdk.keepalive | cache_transform | 240 s up to I_max | TtlPolicy(keepalive) | sdk_snippet | no |
| cc.cold_resume_hook | behavioral | threshold $0.50, $1, $2 | ColdResumePolicy × adoption prior | hook `SessionStart` | no |
| cc.subagent_model | trajectory | claude-sonnet-5, claude-haiku-4-5, claude-opus-5-5 | ModelMapPolicy(subagent, workflow_agent) | env `CLAUDE_CODE_SUBAGENT_MODEL` | yes |
| model.same_tier_upgrade | trajectory | successor map (§4.1) | ModelMapPolicy | proposed: `availableModels` / `ANTHROPIC_DEFAULT_*` | yes |
| cc.max_effort | trajectory | high, medium | EffortPolicy | `maxEffortLevel` | yes |
| cc.fast_mode_opt_in | rate | on | fast_mode_off | `fastModePerSessionOptIn: true` | no |
| geo.global | rate | on | geo_global | policy decision (no key) | residency approval |
| batch.eligible | rate | on | BatchPolicy | sdk_snippet (Batches / flex) | no |
| fanout.stagger | cache_transform | on | stagger_fanout | sdk_snippet / harness setting | no |
| retry.single_owner | cache_transform | 3 attempts, 60 s budget | RetryPolicy | sdk_snippet / gateway config | no |
| cc.tool_output_cap | trajectory | 10k, 20k tokens | ToolOutputCapPolicy | `bashOutputMaxChars`, env `MAX_MCP_OUTPUT_TOKENS`, PostToolUse filter hook | yes |
| cc.tool_search | cache_transform | on | restore MCP-stable prefix | env `ENABLE_TOOL_SEARCH=true` | no |
| gateway.restore_caching | cache_transform | on | restore_caching | gateway_config / LiteLLM injection points | no |
| ci.shared_prefix | cache_transform | on | shared_ci_prefix | ci_workflow flags | no |
| ci.trigger_once | rate (coverage trade-off noted) | once + opt-in | drop superseded/re-run requests | ci_workflow | no (trade-off: coverage) |
| blocks.breakpoints | cache_transform | static_plus_tail, every_k=15, search | BreakpointPolicy | sdk_snippet | no |
| blocks.repair.<detector> | cache_transform | on | repairs | sdk_snippet | no |

## Appendix C: Realization-rate priors and evidence-registry seeds

**RR priors** (p10, p50, p90), replaced by the org's RR database after ≥3 verified receipts per class:

| Class | Prior | Basis |
|---|---|---|
| rate | (0.95, 1.00, 1.00) | Identical tokens repriced; allows small usage shifts. Research prior "rate 1.0" [realization-rate-backtest] |
| cache_transform | (0.80, 0.90, 1.00) | Deterministic transforms, research prior 0.8–1.0 [realization-rate-backtest] |
| trajectory | (−0.20, 0.50, 0.90) | Research prior −0.2–1.0. The worked example realized 0.67 (0.34–0.98) against a true 0.908 [realization-rate-backtest]. The p50 of 0.5 is **this design's choice**; energy-efficiency priors are 0.25–0.4 [projection-optimism] |
| behavioral | (0.05, 0.20, 0.40) | Nudges ~1.4 pp at scale and decaying [defaults-beat-nudges-meta], [nudge-decay-durability] |
| commercial | (1.00, 1.00, 1.00) once executed | Contract-dependent |

**Evidence registry seeds** (`rates/data/evidence.json`; each entry carries a source URL, check date and finding id):

| id | Value | Finding |
|---|---|---|
| `anthropic.cache_read_share.p50` / `.p90` / `.alert_below` | 0.84 / 0.94 / 0.80 | [anth-cache-health-thresholds] |
| `anthropic.caching_factor.agent_loops` | 2.7–5.3× (live); 2.5–3.7× (stale bundled) | [anth-evidence-drift] |
| `anthropic.ttl_rule.gap_share_5_60` | 1/20 | [anth-fleet-benchmarks] |
| `keepalive.interval_s` | 240 | [anth-ttl-choice-keepalive] |
| `effort.opus5.medium` | cost 0.50, Δpts −2 | [anth-effort-sweep] |
| `effort.opus5.low` | cost 0.25, Δpts −8 | [anth-effort-sweep] |
| `effort.low_then_rerun` | $0.45 vs $0.93; pass 93% vs 91.7% | [anth-rerun-failures] |
| `task_budget.fable51` | −44% / −3 pts; −58% / −6 pts | [task-budgets] |
| `thinking_share_prior` | 0.505 of output | [cc-output-thinking-effort] |
| `cc.compaction.summary_tokens.median` | 20,283 | [cc-compaction-threshold-sim] |
| `cc.autocompact.default` | ~967,000 | [cc-autocompact-window] |
| `tokenizer.47plus.factor` | (1.00, 1.30, 1.35) | [anth-tokenizer-inflation] |
| `batch.cache_hit_band` | (0.30, 0.98) | [anth-batch-stacking] |
| `cc.miss_rule` | >5% and ≥2,000 tokens | [cc-usage-likely-cause] |
| `cc.fleet.per_active_day` | $13; p90 < $30; $150–250/month | [cc-enterprise-baseline] |
| `output.shape.one_line` | −39% output, −14% cost | [anth-output-hygiene] |
| `max_tokens.16k_truncation` | 15% (Opus 5), 43% (Fable 5.1) | [max-tokens-truncation] |
| `context_editing.short_run` | +74% cost | [anth-context-editing-not-savings] |
| `compaction.long_run` | −32%; pruning −39% | [anth-compaction-iterations-billing], [jagged-pruning] |
| `compression.rtk_ab` | +7.6% cost at low effort (p = 0.004) | [verified-savings-gap-rtk] |
| `compression.pointfive` | −38.4% tool tokens → +6.8% cost | [token-not-cost] |
| `chars_per_token.47plus.tool_output` | 2.2–2.7 | [cc-chars-per-token-calibration] |
| `datadog.defaults` | −36.7% model default ($687k/mo); effort $288k/mo | [org-defaults-datadog] |
