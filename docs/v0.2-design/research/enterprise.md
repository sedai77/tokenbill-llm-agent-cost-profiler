# Token Bill: enterprise-readiness research track

Research date: 2026-09-23. Scope: what an internal LLM/agent cost-optimization tool needs before an enterprise lab will approve it for thousands of developers. Covers FinOps and FOCUS, attribution, budgets, anomaly detection, forecasting, privacy and security, supply chain, deployment, scale, OpenTelemetry, SSO/RBAC and accessibility. Every source below was opened during this pass. "Accessed" means the page is a living document with no publication date.

Token Bill today (v0.1.2, inspected in the local clone) is a zero-dependency Python CLI and library. It reads JSONL traces that contain the full request payload of each call, then analyzes one run at a time in memory and prices it at Anthropic list prices using floats. It ships through PyPI Trusted Publishing and makes no network calls.

---

## 1. Executive summary

1. **The first question an enterprise lab asks is whether the numbers match the bill.** Anthropic's Cost API and Enterprise Analytics API return money as decimal strings in cents. Enterprise Analytics values can be revised for up to 30 days. Contracted rates, the 1.1× data-residency rate, 2× one-hour cache writes, the 50% batch discount and Priority Tier all move the real bill away from list price. Token Bill's claim that "every dollar total is exact" (usage × list price, 5-minute cache writes only, floats) would not survive a reconciliation test. A rate card, Decimal money and a reconciliation report against the provider's cost data are the top enterprise must-haves.
2. **Privacy decides whether the tool can be deployed at all.** Token Bill's trace format stores every prompt, tool result and file content, in full, on every call. That is sensitive, and storage grows quadratically with run length. The ecosystem has already moved to content-free telemetry:
   - Claude Code's OTel export redacts prompts by default.
   - The Claude apps gateway "doesn't log or store prompt or completion content".
   - OTel GenAI content attributes are Opt-In.
   - Anthropic's **cache diagnostics beta** (`cache-diagnosis-2026-04-07`) now reports `system_changed`, `tools_changed`, `messages_changed` and `model_changed`, using only hashes on the server. These four cases match 4 of Token Bill's 5 cache breakers.

   Token Bill can offer a **metadata-only / hash-only mode as the default** that still detects breakers and prices waste. That combination is a real differentiator.
3. **Most enterprise usage data will come from telemetry that already exists, not from a new recorder.** For thousands of developers on Claude Code, the sources are:
   - Claude Code native OTel metrics and events (`claude_code.token.usage` type=input/output/cacheRead/cacheCreation, `claude_code.api_request` with per-call `cost_usd`, team tags through `OTEL_RESOURCE_ATTRIBUTES`).
   - The Claude Code Analytics API (per user per day, including commits, PRs and lines of code).
   - The Usage & Cost API (1-minute buckets, grouped by api_key/workspace/model/service_tier).
   - The Enterprise Analytics API.
   - Bedrock CUR 2.0 `line_item_iam_principal`.
   - The Claude apps gateway spend-limit API.

   Token Bill should ingest all of these and reserve raw-payload traces for deep dives.
4. **Standards to adopt now:**
   - **FOCUS 1.4** (ratified 2026-06-04). Tokens go in `ConsumedQuantity`/`ConsumedUnit` plus `x_`-prefixed custom columns; FOCUS 1.5 is scoped to add native AI model identity and token consumption.
   - **OTel GenAI semantic conventions.** These moved to their own repository with semconv v1.42.0 (2026-06-12). The cache attribute is now `gen_ai.usage.cache_write.input_tokens`, while older instrumentation emits `...cache_creation...`. Anthropic's `input_tokens` excludes cache tokens but OTel's includes them, so the normalizer has to avoid double counting.
   - The Linux Foundation **Tokenomics Foundation** (launched 2026-08-04) is writing "Token Cost Telemetry schemas". Token Bill should align with it and contribute its waste taxonomy.
5. **Supply chain is not hypothetical for this category.** LiteLLM, the most-cited LLM cost proxy, shipped backdoored PyPI releases (1.82.7 and 1.82.8) on 2026-03-24 after a compromised Trivy dependency in its CI exfiltrated a long-lived `PYPI_PUBLISH` token. Token Bill already uses Trusted Publishing, and PyPI shows a PEP 740 attestation for 0.1.2 (verified through the Integrity API). The remaining gaps are:
   - CI actions pinned to tags rather than SHAs.
   - No top-level `permissions:` block in `ci.yml`.
   - No SBOM, no signed or immutable GitHub release assets.
   - No Scorecard, SAST, dependency-update tool or fuzzing.
   - A single maintainer. OSPS Baseline Level 2 expects 2 or more maintainers.
6. **FinOps capabilities the lab will expect:**
   - Allocation coverage KPI (% of spend allocated).
   - Showback before chargeback.
   - Unit economics: cost per inference, per API call, per successful outcome, per PR.
   - Anomaly management with MTTD and an "anomaly cost as % of spend" target (under 2% green, 2–7% yellow, over 7% critical).
   - Forecasting with accuracy and drift KPIs.

   Token Bill's edge is to **attach a cause and a dollar-denominated fix to each anomaly**. AWS Cost Anomaly Detection can take up to 24 hours and needs 10 days of history, and it cannot say why spend moved. Research shows that token use on the same agentic task can vary by up to 30×, so baselines must be robust and quantile-based, not mean-based.
7. **Stakes, as arithmetic on Anthropic's published figures.** Anthropic reports that enterprise Claude Code costs average about $13 per developer per active day and $150–250 per developer per month. At 2,000 developers that is roughly $3.6M–$6M a year. A tool that credibly recovers even a few percent of that pays for its review many times over, but only if its numbers reconcile and its privacy story passes a DPIA.

---

## 2. Current Token Bill gaps seen in the repo (v0.1.2)

| Area | Observation | Why the enterprise lab cares |
|---|---|---|
| Money type | `pricing.py` uses `float` for $/MTok and results | Cost APIs return cents as decimal strings. Anthropic's Analytics docs warn against binary floats for large amounts. |
| Rate coverage | Only list price and 5-minute cache writes (1.25×). No 1h writes (2×), batch (50%), data residency (1.1×), Priority Tier, or contracted rates | Totals will not reconcile with the invoice or Cost API |
| Trace content | Every line stores full `system`, `tools`, `messages` | Contains prompts, code and secrets. Storage is quadratic in run length. |
| Scale | `read_trace()` returns `list[Run]` and loads everything into memory | Cannot handle org-wide volume |
| CI | `ci.yml` has no `permissions:`. Actions pinned to tags (`actions/checkout@v4`, `astral-sh/setup-uv@v5`, `pypa/gh-action-pypi-publish@release/v1`) | Fails the Scorecard Token-Permissions and Pinned-Dependencies checks. This is the exact attack class seen in tj-actions and Trivy→LiteLLM. |
| Releases | PyPI PEP 740 attestation present. GitHub releases not immutable, with no `.sigstore`/`.intoto.jsonl`/SBOM assets | Fails Scorecard Signed-Releases and SBOM. Lab cannot verify the GitHub-downloaded artifact. |
| Repo hygiene | No Dependabot/Renovate, CodeQL/SAST, fuzzing, Scorecard workflow or Best Practices badge. 1 maintainer (1 star, created 2026-07-27) | Scorecard (Maintained, Contributors, SAST, Fuzzing) and OSPS Baseline L2 governance |
| Output safety | SECURITY.md admits terminal output prints IDs verbatim (ANSI injection). The HTML report has no CSP `<meta>` | Standard hostile-input tests |
| Accessibility | SVGs have `role="img"` and `aria-label` (good), but there are no tabular equivalents | WCAG 2.2 AA (1.1.1, 1.3.1, 1.4.1) |
| Strengths to keep | Zero runtime dependencies, no network, offline demo, planted-waste correctness test, labeled exact-vs-approx line, Trusted Publishing | These already remove whole classes of enterprise risk (no proxy, no egress, tiny SBOM) |

---

## 3. Findings

### A. Standards and the data model

#### A1. FOCUS 1.4 is the current FinOps billing schema. AI tokens use existing columns now, and native AI columns are planned for 1.5 (`ent-focus-1-4`)
- **Facts.**
  - The FOCUS Steering Committee ratified FOCUS 1.4 on **2026-06-04**. It adds 2 datasets (Invoice Detail, Billing Period), 47 columns, 6 attributes and 17 glossary entries.
  - The FinOps Foundation says **FOCUS 1.5** will add "native AI support" to "surface AI model identity and token consumption (input and output) in the Cost and Usage dataset", plus a Price Sheet dataset.
  - GitHub issue #2018 (opened 2026-02-24, updated 2026-07-23, milestone v1.5) deliberately proposes **no new columns**. It maps model identity and input/output token counts onto existing `ConsumedQuantity`/`ConsumedUnit`, with "service-specific opt-in".
  - `ServiceCategory` already has the allowed value **"AI and Machine Learning"**.
  - Custom columns **must** use the `x_` prefix (PascalCase, 50 characters or fewer).
  - Split cost allocation columns (`AllocatedMethodId`, `AllocatedMethodDetails`, `AllocatedResourceId`, `AllocatedTags`) were introduced in 1.3 (2025-12-08).
  - The FinOps AI framework page notes that token charges appear as SKUs with "Consumed Units", and that FOCUS 1.3 has no AI-specific columns.
  - At FinOps X, FOCUS maintainers named cardinality as the hard problem: "per user … per session, per request, per operation — the farther down this ladder you go, the larger the cardinality".
- **Sources.**
  - https://www.finops.org/insights/introducing-focus-1-4/ — Introducing FOCUS 1.4 (June 2026)
  - https://github.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS_Spec/issues/2018 — [FR] Surface AI model identity and token consumption (opened 2026-02-24, updated 2026-07-23)
  - https://github.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS_Spec (release tags: v1.4 2026-06-04, v1.3 2025-12-08). Spec files `specification/datasets/cost_and_usage/columns/servicecategory.md` and `specification/attributes/custom_column_handling.md` at tag v1.4.
  - https://focus.finops.org/focus-specification/ — FOCUS Specification 1.4 (accessed 2026-09-23)
  - https://www.finops.org/framework/technology-categories/ai/ — FinOps for AI technology category (accessed)
  - https://siliconangle.com/2026/06/08/ai-token-economics-focus-specification-updates-finopsx/ — SiliconANGLE (2026-06-08)
- **Opportunity.**
  - Add `tokenbill export --format focus`, producing CSV or Parquet Cost and Usage rows at call, session or day grain. Use `ServiceCategory="AI and Machine Learning"`, `ConsumedUnit="Tokens"`, one row per token class (or the #2018 mapping once 1.5 lands), `BilledCost`/`EffectiveCost`/`ListCost` from the rate card, and `Tags` from attribution.
  - Add Token Bill's unique signal as custom columns: `x_CacheReadTokens`, `x_CacheWriteTokens`, `x_UncachedInputTokens`, `x_RedundantInputCost`, `x_CacheBreakerType`, `x_RecoverableCost`, `x_SessionId`.
  - Waste then flows into Vantage, CloudZero, Flexera and similar tools, and into the enterprise's FinOps lake, without a custom integration.
  - Let the user choose the grain so the cardinality warning is handled explicitly.
- **Effort:** M. **Evidence:** strong.

#### A2. OpenTelemetry GenAI semantic conventions moved repositories and renamed the cache-write attribute (`ent-otel-genai-semconv`)
- **Facts.**
  - Semconv **v1.40.0 (2026-02-19)** added `gen_ai.usage.cache_read.input_tokens` and `gen_ai.usage.cache_creation.input_tokens`.
  - **v1.42.0 (2026-06-12)** moved all `gen_ai.*` conventions to `open-telemetry/semantic-conventions-genai` (repo created 2026-05-05). The opentelemetry.io registry marks the old attributes "Moved".
  - In the new repo (status **Development**, `schema_url` still "TODO"), the attributes are:
    - `gen_ai.usage.input_tokens` (includes cached tokens)
    - `gen_ai.usage.output_tokens`
    - `gen_ai.usage.cache_read.input_tokens`
    - **`gen_ai.usage.cache_write.input_tokens`**
    - `gen_ai.usage.reasoning.output_tokens`
    - per-modality `gen_ai.usage.{text,image,audio}.*`
  - The Anthropic page says: "Anthropic `input_tokens` excludes cached tokens. Compute: `gen_ai.usage.input_tokens = input_tokens + cache_read_input_tokens + cache_write_input_tokens`". It also says `gen_ai.provider.name` MUST be `"anthropic"`.
  - For output tokens: "instrumentations SHOULD report the billed count".
  - Token **metrics** are now monotonic counters: `gen_ai.client.inference.usage.input_tokens`, `.output_tokens`, `.cache_read.input_tokens`, `.cache_write.input_tokens`, `.reasoning.output_tokens` (unit `{token}`, required `gen_ai.token.modality` with an `unknown` value).
  - Per-operation histograms (`gen_ai.client.inference.operation.input_tokens`/`output_tokens`) are for percentiles only: "should not be used for total usage or cost calculations".
  - The spec also defines agent and conversation identity: `gen_ai.conversation.id` (must not be a synthetic UUID or content hash), `gen_ai.agent.name`, `gen_ai.conversation.compacted`, and `gen_ai.request.reasoning.level` (maps to Anthropic `output_config.effort`).
  - Content attributes (`gen_ai.input.messages`, `gen_ai.system_instructions`, `gen_ai.tool.definitions`) are **Opt-In**.
- **Sources.**
  - https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/anthropic.md — Anthropic client conventions (accessed 2026-09-23)
  - https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/gen-ai-token-metrics.md — inference token metrics (accessed)
  - https://raw.githubusercontent.com/open-telemetry/semantic-conventions-genai/main/docs/gen-ai/non-normative/token-metrics-design.md — design rationale (accessed)
  - https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/ — registry showing "Moved" (semconv 1.44.0 site, accessed)
  - https://github.com/open-telemetry/semantic-conventions/blob/main/CHANGELOG.md — v1.40.0 (2026-02-19) added the cache attributes; v1.42.0 (2026-06-12) moved GenAI
- **Opportunity.**
  - Build a first-class OTel ingest (OTLP/HTTP JSON receiver and OTLP-JSON file reader) that accepts old and new names.
  - Emit enriched `gen_ai.*` plus `tokenbill.*` attributes (for example `tokenbill.waste.cost_usd`, `tokenbill.breaker.type`) so Token Bill can run inside an existing collector pipeline.
  - Emit the counters, not the histograms, for cost.
  - Track the new repo's release and `schema_url` and ship a compatibility matrix.
- **Effort:** M. **Evidence:** strong.

#### A3. The same cache tokens are counted four different ways across schemas. Token Bill should own the canonical normalization and its conformance tests (`ent-usage-normalization`)
- **Facts.**
  - Anthropic API: `input_tokens` excludes cache. `cache_read_input_tokens` and `cache_creation_input_tokens` are separate, and `usage.cache_creation.{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}` break writes down by TTL.
  - OTel GenAI: `input_tokens` includes cache.
  - OpenInference: `llm.token_count.prompt` includes `llm.token_count.prompt_details.cache_read`/`cache_write` and says Anthropic cache tokens should be "folded back". It also defines `llm.cost.prompt_details.cache_read`/`cache_write`.
  - Langfuse `usage_details`: mutually exclusive buckets; "each token must be counted in exactly one key".
  - Claude Code OTel metric: `claude_code.token.usage{type=input|output|cacheRead|cacheCreation}`. Claude Code event `claude_code.api_request`: `input_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `cost_usd`.
  - Claude Code Analytics API: `tokens.input/output/cache_read/cache_creation`.
  - OpenAI (GPT-5.6+): `usage.input_tokens_details.cached_tokens` and `cache_write_tokens`, with cache writes billed at 1.25× and reads at 0.1×.
- **Sources.**
  - https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md — OpenInference semantic conventions (accessed)
  - https://langfuse.com/docs/observability/features/token-and-cost-tracking — Langfuse token and cost tracking (accessed)
  - https://code.claude.com/docs/en/monitoring-usage — Claude Code monitoring (accessed)
  - https://platform.claude.com/docs/en/build-with-claude/prompt-caching — prompt caching (accessed; `cache_creation` breakdown, 1.25×/2× write, 0.1× read)
  - https://developers.openai.com/api/docs/guides/prompt-caching — OpenAI prompt caching (accessed)
  - the A2 sources
- **Opportunity.**
  - Build one `CanonicalUsage` model with disjoint buckets: uncached_input, cache_read, cache_write_5m, cache_write_1h, output, reasoning_output, plus modality.
  - Write adapters for each schema, with golden-file conformance tests that enforce the rule "sum of buckets = provider billed total".
  - A mis-mapping here silently double-counts cache tokens, and cache tokens often dominate agent spend. Publishing the mapping table (section 5) is useful to the whole ecosystem.
- **Effort:** S–M. **Evidence:** strong.

### B. Where enterprise usage data actually comes from

#### B1. Claude Code's native OTel export is the richest, content-free, per-call source for a coding-agent fleet (`ent-claude-code-otel`)
- **Facts.**
  - Telemetry is enabled with `CLAUDE_CODE_ENABLE_TELEMETRY=1`.
  - Metrics:
    - `claude_code.token.usage` (type input/output/cacheRead/cacheCreation)
    - `claude_code.cost.usage` (USD), with attributes `model`, `query_source` (main/subagent/auxiliary), `speed`, `effort`, `agent.name`, `skill.name`, `plugin.name`, `mcp_server.name`, `mcp_tool.name`
    - `claude_code.session.count`, `lines_of_code.count`, `commit.count`, `pull_request.count`, `active_time.total`
  - Events: `claude_code.api_request` (per-call tokens including cache, `duration_ms`, `cost_usd`, `request_id`) and `claude_code.api_error`.
  - Standard attributes include `session.id`, `user.account_uuid`, `user.email`, `organization.id`, `terminal.type`, and optional `vcs.repository.*`.
  - Team and cost-center tags come from `OTEL_RESOURCE_ATTRIBUTES="department=engineering,team.id=platform,cost_center=eng-123"`.
  - **Privacy defaults.** Prompts, responses, tool details and raw API bodies are all off unless `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_TOOL_DETAILS`, `OTEL_LOG_RAW_API_BODIES` and similar variables are set.
  - Admins can push the configuration through managed settings, which lock the destinations (v2.1.217+).
  - Cost figures are list-price estimates unless the `modelPricing` managed setting (multiplier and overrides for input/output/cacheRead/cacheWrite) is deployed.
  - Claude Code's own `/usage` now shows per-session prompt-cache stats. A miss is counted when a request re-processes more than 5% and at least 2,000 tokens it could have read from cache. The line includes "likely cause: tool definitions changed" (v2.1.260+), but only for the main conversation on one machine.
- **Sources.**
  - https://code.claude.com/docs/en/monitoring-usage — Monitoring usage (accessed 2026-09-23)
  - https://code.claude.com/docs/en/costs — Manage costs effectively (accessed 2026-09-23)
  - https://code.claude.com/docs/en/settings-reference — `modelPricing` (accessed)
- **Opportunity.**
  - Make "point your collector at Token Bill" (OTLP/HTTP receiver or collector `file` exporter output) a flagship enterprise path.
  - From `api_request` events plus `session.id`/`prompt.id`, compute fleet-wide cache-miss cost by team, repo, agent, skill and MCP server, with no prompt content.
  - Rank misses by dollars and recommend fixes. Examples: an MCP server whose results churn the prefix; subagent or auxiliary queries on an expensive model; `effort=max` concentrated on a few users.
  - Claude Code shows each developer their own misses. Token Bill aggregates across the org, prioritizes by money, and verifies fixes.
- **Effort:** M. **Evidence:** strong.

#### B2. Anthropic's admin reporting APIs are the ground truth for reconciliation and org-level baselines (`ent-anthropic-admin-apis`)
- **Facts.**
  - **Usage API** `GET /v1/organizations/usage_report/messages`:
    - buckets `1m` (up to 1,440), `1h` (up to 168), `1d` (up to 31)
    - filter and `group_by` by api key, workspace, model, service tier, context window, `inference_geo`, and `speed` (beta)
    - data appears in about 5 minutes; polling once per minute is supported
  - **Cost API** `GET /v1/organizations/cost_report`:
    - daily only, USD "decimal strings in lowest units (cents)"
    - groups by workspace or description
    - "Priority Tier costs … are not included"
    - default workspace = `null` `workspace_id`; playground usage has `api_key_id=null`
  - **Claude Code Analytics API** `GET /v1/organizations/usage_report/claude_code`:
    - per user per day: sessions, LOC added and removed, commits, PRs, tool accept/reject, `model_breakdown[].tokens.{input,output,cache_read,cache_creation}`, `estimated_cost.amount` (cents)
    - about 1 hour delay, `limit` up to 1000, free
    - excludes Bedrock, Vertex, Foundry and Claude Platform on AWS
  - **Claude Enterprise Analytics API** (`/v1/organizations/analytics/…`, `read:analytics` key):
    - per-user cost and usage on usage-based Enterprise plans
    - "typically available within four hours … may take up to 24 hours. Values … can be revised for up to 30 days"
    - amounts are cents decimal strings, and the docs say "Avoid binary floating-point parsing"
    - 60 requests per minute org-wide
  - Admin credentials are separate (`sk-ant-admin…` or an `org:admin` OAuth token). CI can use workload identity federation.
- **Sources.**
  - https://platform.claude.com/docs/en/manage-claude/usage-cost-api — Usage and Cost API (accessed)
  - https://platform.claude.com/docs/en/manage-claude/claude-code-analytics-api — Claude Code Analytics API (accessed)
  - https://platform.claude.com/docs/en/manage-claude/analytics-api — Analytics APIs (accessed)
  - Local authoritative copy: bundled `claude-api/shared/admin-api.md` (Admin API; SDK availability as of 2026-08-26)
- **Opportunity.**
  - Build read-only connectors (`tokenbill pull anthropic-usage|anthropic-cost|claude-code-analytics|enterprise-analytics`) using least-privilege keys from environment variables or a secrets manager. Never log the keys.
  - Use a ≤30-day "provisional" window and a "closed" window for invoice-grade numbers.
  - Join Claude Code Analytics (commits, PRs, LOC) with cost to get unit economics (C3).
- **Effort:** M. **Evidence:** strong.

#### B3. Cloud and gateway attribution: Bedrock IAM-principal CUR columns, the Claude apps gateway spend limits, and LiteLLM spend logs (`ent-cloud-gateway-attribution`)
- **Facts.**
  - **Bedrock (2026-04-17):** "Amazon Bedrock now automatically attributes inference costs to the IAM principal that made the call". CUR 2.0 has `line_item_iam_principal` and `line_item_usage_type` (model and token direction). Principal tags appear with the prefix `iamPrincipal/`. Claude Code with federated identity is "Scenario 3". Application inference profiles carry cost-allocation tags for InvokeModel/Converse.
  - **Claude apps gateway** (self-hosted, built into the `claude` binary, OIDC SSO, OTLP/HTTP telemetry):
    - It "doesn't log or store prompt or completion content".
    - Per-user, per-group and per-org **spend caps** are daily, weekly or monthly, set through `/v1/organizations/spend_limits`. It returns 429 `billing_error`, and Claude Code warns at 75% and 95%.
    - Metering uses overrides, then list price, then an unknown-model fallback of $5/$25 per MTok. Client aborts are billed at a floor of about 4 characters per output token.
    - Enforcement fails open by default.
    - Default retention: spend 13 months, audit 365 days, identity 90 days.
  - **LiteLLM** stores `LiteLLM_SpendLogs` per key, user, team, org, end-user and tags, with `max_budget`/`budget_duration`, and exposes `litellm_zero_cost_requests_total` for unpriced models. The FinOps tokenomics paper names proxies (LiteLLM, Portkey, Helicone) as the "proxy layer" for workload tagging.
- **Sources.**
  - https://aws.amazon.com/blogs/machine-learning/introducing-granular-cost-attribution-for-amazon-bedrock/ — AWS ML Blog (2026-04-17)
  - https://code.claude.com/docs/en/claude-apps-gateway-spend-limits — gateway spend limits (accessed)
  - https://code.claude.com/docs/en/claude-apps-gateway — Claude apps gateway (accessed)
  - https://docs.litellm.ai/docs/proxy/cost_tracking — LiteLLM spend tracking (accessed)
  - https://www.finops.org/wg/token-economics-saas/ — Tokenomics: Managing AI Value in SaaS Model Token Costs (2026-06-03)
- **Opportunity.**
  - Build importers for CUR 2.0 (Bedrock rows), the gateway `/spend_limits/effective` endpoint and OTLP stream, and LiteLLM SpendLogs, all normalized into FOCUS plus CanonicalUsage.
  - Add an **unpriced-usage detector**: fallback-tier rows and zero-cost rows.
  - Add **cap recommendations**: suggested per-group daily and monthly caps from observed p95 and p99 (C4/C5), exported as ready-to-POST JSON for the gateway API. Token Bill proposes and a human applies.
- **Effort:** M–L. **Evidence:** strong.

#### B4. Claude Code local transcripts: a useful source, but full-content, unencrypted and auto-deleted after 30 days (`ent-local-transcripts`)
- **Facts.**
  - Transcripts live at `~/.claude/projects/<project>/<session>.jsonl` and contain "every message … every tool call … file contents, command output, and pasted text".
  - The docs say: "Transcripts and history are not encrypted at rest. OS file permissions are the only protection. If a tool reads a `.env` file or a command prints a credential, that value is written".
  - `cleanupPeriodDays` defaults to **30**.
  - **ccusage** already parses these logs offline, supports 18 agent CLIs (Claude Code, Codex, Gemini CLI, Copilot CLI and others), and prices with the LiteLLM and models.dev catalogs. Its docs admit that when 5m/1h breakdowns are missing, cache writes are priced at the standard rate. Local cost reporting is therefore table stakes.
- **Sources.**
  - https://code.claude.com/docs/en/claude-directory — `.claude` directory (accessed)
  - https://ccusage.com/guide/cost-modes — ccusage cost modes (accessed)
- **Opportunity.** Build the roadmap's "Claude Code session-log importer" as an **on-device agent**:
  - Parse locally.
  - Compute breakers, redundancy and waste locally.
  - Emit only aggregates and HMAC'd identifiers (D1) to the central store.
  - Run on a schedule shorter than the cleanup period, and never upload content.

  This gives the depth of the payload diff without centralizing source code.
- **Effort:** M. **Evidence:** strong.

### C. Trust: accuracy, unit economics, anomalies, forecasts

#### C1. Invoice reconciliation is the first acceptance test. Token Bill needs a rate card, Decimal money and a variance report (`ent-reconciliation`)
- **Facts.**
  - Cache writes are "1.25 times" the base input price for 5 minutes and "2 times" for 1 hour. These "multipliers stack with other pricing modifiers such as the Batch API discount and data residency".
  - Claude Code bills responses at the "1.1× data residency rate". Before v2.1.239 it did not apply that multiplier, so "the session cost figure was lower than the bill".
  - Claude Code's own figures are list-price estimates unless `modelPricing` is set.
  - Priority Tier is absent from the Cost API.
  - Enterprise Analytics revises values for up to 30 days and returns cents decimal strings, with the warning against binary floats.
  - The FinOps Invoicing and Chargeback, and Forecasting capabilities define accuracy KPIs as `(Forecast − Actual)/Forecast`.
- **Sources.**
  - https://platform.claude.com/docs/en/build-with-claude/prompt-caching (accessed)
  - https://code.claude.com/docs/en/costs (accessed)
  - https://platform.claude.com/docs/en/manage-claude/usage-cost-api (accessed)
  - https://platform.claude.com/docs/en/manage-claude/analytics-api (accessed)
  - https://www.finops.org/framework/capabilities/forecasting/ (accessed)
- **Opportunity.**
  1. **Versioned rate card:**
     - list price
     - contract overrides and multiplier, accepting the same JSON shape as Claude Code's `modelPricing`
     - 5m and 1h write rates
     - batch, data residency, fast mode and priority modifiers
     - effective-dated entries
  2. **Decimal or integer micro-cents** everywhere money is computed. Floats only for display.
  3. `tokenbill reconcile`: compare Token Bill totals against the Cost API or Enterprise Analytics by day, workspace and model. Report absolute and percentage variance and explain residuals (unpriced models, priority tier, provisional window). Fail CI if variance exceeds a configurable tolerance.
  4. Relabel the claim: "exact at your rate card, reconciled to provider cost data within X%".
- **Effort:** M. **Evidence:** strong. The labs will test this first.

#### C2. Anthropic cache diagnostics detects breakers server-side from hashes only, which lets a no-content mode keep its core value (`ent-cache-diagnostics-privacy`)
- **Facts.**
  - Beta header `cache-diagnosis-2026-04-07`. The request passes `diagnostics.previous_message_id`, and the response carries `diagnostics.cache_miss_reason.type`, one of:
    - `model_changed`
    - `system_changed`
    - `tools_changed`
    - `messages_changed`
    - `previous_message_not_found`
    - `unavailable`

    Each also carries `cache_missed_input_tokens`, which is described as a magnitude, not a billing number.
  - "Fingerprints contain only hashes and token-count estimates (never raw prompt content)". The feature is ZDR-eligible and scoped to organization and workspace, with short retention.
  - It is first-party Claude API only. It is not available on Bedrock, Google Cloud, Foundry or Claude Platform on AWS.
  - It must be sent on **every** request, or you get `previous_message_not_found`.
  - These map one-to-one to Token Bill's model-switch, volatile-system, tool-churn and history-rewrite breakers.
- **Sources.**
  - https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics — Cache diagnostics (beta header dated 2026-04-07; accessed 2026-09-23)
  - bundled `claude-api/shared/prompt-caching.md` and `cost-optimization.md` (authoritative local copies)
- **Opportunity.**
  - The recorder gets a `--metadata-only` mode that opts into diagnostics and records usage, `diagnostics` and structural fingerprints (per-block HMAC and length). It stores no text.
  - Breakers are classified from diagnostics where available and from local fingerprints elsewhere, with evidence of the form "system block 2 changed at approx token 96; pattern class: timestamp". The pattern class is computed before hashing, and the text is discarded.
  - Report `unavailable`/`previous_message_not_found` rates as data-quality metrics.
- **Effort:** S–M. **Evidence:** strong.

#### C3. FinOps for AI unit economics and levers: the KPIs an enterprise will ask Token Bill to report (`ent-finops-kpis`)
- **Facts.**
  - The FinOps for AI Overview (2026-02-17) defines Cost per Inference, Cost per Token, Cost per API Call, ROI and Time to Business Value, and recommends showback before chargeback.
  - The FinOps AI Value WG "Tokenomics" paper (2026-06-03) recommends cost per query, cost per user per month, **cost per successful outcome** and cost per business transaction. Its levers table gives:
    - model right-sizing 60–90%
    - Batch API 50%
    - prompt caching 50–90% of the cached portion
    - context-window management 20–60%
    - output-length control 10–40%
    - commitments 10–30%
  - It lists waste sources: frontier-model defaults, runaway agent loops, verbose system prompts, history accumulation, and dev/test bleed.
  - Its maturity model ends at "Run (Month 9+): Chargeback, dynamic routing, commitment negotiations, **CI/CD cost integration**".
- **Sources.**
  - https://www.finops.org/wg/finops-for-ai-overview/ — FinOps for AI Overview (2026-02-17)
  - https://www.finops.org/wg/token-economics-saas/ — Tokenomics (2026-06-03)
- **Opportunity.**
  - Report the Foundation's KPIs as named columns, including cost per session, per PR, per commit and per accepted edit. Claude Code Analytics supplies PRs, commits, LOC and accept rates per user per day.
  - Map each detected waste to a Foundation lever, so FinOps teams read Token Bill output in their own vocabulary.
  - Report "recoverable $" per lever alongside the Foundation's benchmark range.
- **Effort:** M. **Evidence:** strong.

#### C4. Anomaly management: detect in minutes, not the 24 hours of cloud-billing tools, and attach the cause (`ent-anomaly-detection`)
- **Facts.**
  - The FinOps Anomaly Management capability defines a detect, notify, analyze, resolve lifecycle. Its KPIs are MTTD, time to notify, unresolved duration, and **anomaly cost as % of spend: <2% green, 2–7% yellow, >7% critical**. Crawl-stage detection is described as "week-long".
  - AWS Cost Anomaly Detection runs "approximately three times a day", "can take up to 24 hours to detect", and needs "10 days of historical service usage data".
  - OWASP LLM10:2025 names "Denial of Wallet" and recommends quotas, rate limits and "continuously monitor resource usage".
  - Research:
    - Agentic coding tasks consume about **1000×** more tokens than code chat. Runs on the same task "can differ by up to 30x in total tokens". Models' self-predictions correlate weakly (≤0.39) and systematically underestimate (arXiv 2604.22750).
    - RecurGuard shows reasoning-token consumption attacks can amplify output about 22.8×, and runtime monitoring caught 99% of OverThink and 92% of ExtendAttack (arXiv 2606.07968).
  - NIST's EWMA chart (λ≈0.2–0.3, ±3σ limits) is a simple, explainable detector that needs only the standard library.
- **Sources.**
  - https://www.finops.org/framework/capabilities/anomaly-management/ (accessed)
  - https://docs.aws.amazon.com/cost-management/latest/userguide/manage-ad.html (accessed)
  - https://genai.owasp.org/llmrisk/llm102025-unbounded-consumption/ (2025)
  - https://arxiv.org/abs/2604.22750 — Bai et al., "How Do AI Agents Spend Your Money?" (2026-04-24)
  - https://arxiv.org/abs/2606.07968 — Aziz & Kibria, RecurGuard (2026-06-06)
  - https://www.itl.nist.gov/div898/handbook/pmc/section3/pmc324.htm — NIST/SEMATECH e-Handbook, EWMA
- **Opportunity.** Build `tokenbill watch`, a streaming or scheduled detector over OTel or Usage API 1-minute buckets:
  - per-cohort robust baselines (team × model × query_source), using median/MAD and EWMA on log-cost
  - detectors for spend step-changes, cache-hit-ratio collapse, runaway loops (calls per session and tokens per minute), and output/reasoning blow-ups
  - **every alert carries a probable cause** (breaker type, model switch, new MCP server, effort change) and "$ at risk per day"
  - output as webhook, Slack-compatible JSON or an OTel log event
  - FinOps KPIs (MTTD, anomaly cost %) tracked automatically
- **Effort:** M. **Evidence:** strong.

#### C5. Forecasting and budgets: use quantiles and drivers, and show how optimizations change the forecast (`ent-forecast-budgets`)
- **Facts.**
  - FinOps Forecasting KPIs are Forecast Accuracy Rate (spend and usage) and Forecast Drift Rate. Methods run from historical (Crawl) to rolling and trend-based (Walk) to a mix including driver-based (Run). Each organization sets its own variance target.
  - The FinOps "Effect of Optimization on AI Forecasting" paper (updated 2026-03-17) argues that optimizations reshape forecasts. It cites a 99% token reduction from hashing and caching in one user story, up to 85% from routing, and 40–60% compounding from layered tactics.
  - Anthropic publishes Claude Code budgeting anchors: about **$13 per developer per active day, $150–250 per developer per month, below $30 per active day for 90% of users**. It also publishes per-user TPM/RPM guidance by org size (for example 10k–15k TPM per user at 500+ users).
  - Budget enforcement points exist at several layers: workspace spend limits (Console), org, group and member spend limits (Teams/Enterprise), gateway daily, weekly and monthly caps, Bedrock tag-based AWS Budgets, and Claude Code `--max-budget-usd`.
- **Sources.**
  - https://www.finops.org/framework/capabilities/forecasting/ (accessed)
  - https://www.finops.org/wg/effect-of-optimization-on-ai-forecasting/ (2026-03-17)
  - https://code.claude.com/docs/en/costs (accessed)
  - https://code.claude.com/docs/en/claude-apps-gateway-spend-limits (accessed)
  - https://arxiv.org/abs/2604.22750 (2026-04-24)
- **Opportunity.**
  - A driver-based forecast: active developers × sessions × cost-per-session distribution, giving P50/P90 forecasts and a variance report.
  - A **"what-if"** forecast that applies each recommended fix's recoverable dollars.
  - Budget recommendations per cohort from P95 daily spend, exported for gateway, Console or AWS Budgets.
  - A pre-rollout planner for pilots, since Anthropic itself recommends piloting "to establish a baseline before wider rollout".
- **Effort:** M. **Evidence:** strong.

#### C6. Allocation, showback and chargeback need a coverage KPI and a tag schema (`ent-allocation-showback`)
- **Facts.**
  - FinOps Allocation KPIs include "% of technology costs allocated directly", "% that cannot be categorized", and "% of costs with appropriate allocation metadata". Shared costs are split by fixed, proportional or proxy-metric methods.
  - FinOps for AI proposes tag keys Project, Environment, Workload, Team, CostCenter, UsageType, Purpose and Criticality.
  - The Tokenomics paper names API-key governance as the minimum control and says harness costs are 40–60% of feature spend.
  - Anthropic attribution dimensions are api_key, workspace, the "Claude Code" workspace (auto-created and exclusive), per-user Claude Code analytics, and OTel resource attributes.
- **Sources.**
  - https://www.finops.org/framework/capabilities/allocation/ (accessed)
  - https://www.finops.org/wg/finops-for-ai-overview/ (2026-02-17)
  - https://www.finops.org/wg/token-economics-saas/ (2026-06-03)
  - https://code.claude.com/docs/en/costs (accessed)
- **Opportunity.** Build an allocation engine:
  - rules map (api_key | workspace | user email or IdP group | OTel resource attribute | repo) → (team, cost_center, env, project), versioned in git
  - an "unallocated $" line item and allocation-coverage % KPI
  - split rules for shared keys and shared gateway credentials
  - showback reports per team (HTML, CSV, FOCUS) with each team's recoverable waste, then chargeback exports once coverage reaches the threshold
- **Effort:** M. **Evidence:** strong.

### D. Privacy and data protection

#### D1. Default to no content. Use HMAC pseudonymization with separated keys, retention limits and erasure (`ent-privacy-by-default`)
- **Facts.**
  - EDPB Guidelines 01/2025 on Pseudonymisation (adopted 2025-01-16) say pseudonymised data "is to be considered information on an identifiable natural person … and is therefore personal". The transformation "needs to involve information that the pseudonymising controller keeps secret". Suitable algorithms include "Message Authentication Codes (MACs)", and secrets "should have sufficient entropy".
  - The ecosystem norm is content-off by default: OTel GenAI content attributes are Opt-In, Claude Code redacts prompts and tool details by default, and the Claude apps gateway stores no prompt or completion content.
  - The gateway's retention design is a template: spend 13 months, audit 365 days, identity 90 days, an explicit DSAR delete, and a warning that `q=`/`user_ids[]` query strings end up in proxy logs.
- **Sources.**
  - https://www.edpb.europa.eu/our-work-tools/documents/public-consultations/2025/guidelines-012025-pseudonymisation_en — EDPB Guidelines 01/2025 (adopted 2025-01-16; PDF read)
  - https://code.claude.com/docs/en/monitoring-usage (accessed)
  - https://code.claude.com/docs/en/claude-apps-gateway-spend-limits (accessed)
  - A2 sources
- **Opportunity.** Three explicit data-handling tiers, each printed in every report header and machine-checkable:
  1. **aggregate** (default for central use): usage and costs only.
  2. **fingerprint**: per-block HMAC-SHA256 with an org-held key, lengths and pattern classes. Enough for breaker detection.
  3. **full-content**: local-only deep dive, with an explicit flag and TTL auto-purge.

  Also:
  - Keep user identity as HMAC'd IDs by default, with a separately stored key for authorized re-identification.
  - Provide `--retention-days`, `tokenbill purge --user`, and a documented data-flow diagram for the DPIA.
  - Never put identifiers in URLs or query strings.
- **Effort:** M. **Evidence:** strong.

#### D2. Traces from coding agents contain secrets at measurable rates, so Token Bill should scan and redact before anything persists (`ent-secrets-in-traces`)
- **Facts.** GitGuardian State of Secrets Sprawl 2026 (2026-03-17):
  - 28.65M new hardcoded secrets on public GitHub in 2025 (+34% year over year)
  - AI-service secrets up 81% to 1,275,105
  - "Claude Code-assisted commits showed a 3.2% secret-leak rate, versus a 1.5% baseline"
  - 24,008 unique secrets in MCP config files, 2,117 of them valid
  - internal repos about 6× more likely than public ones to contain hardcoded secrets
  - 28% of incidents originate outside repositories

  Claude Code transcripts capture `.env` reads and printed credentials verbatim (B4). OWASP LLM02:2025 (Sensitive Information Disclosure) rose to #2.
- **Sources.**
  - https://blog.gitguardian.com/the-state-of-secrets-sprawl-2026/ — GitGuardian (2026-03-17)
  - https://code.claude.com/docs/en/claude-directory (accessed)
  - https://genai.owasp.org/resource/owasp-top-10-for-llm-applications-2025/ — OWASP Top 10 for LLM Apps 2025
- **Opportunity.**
  - Put a built-in, stdlib-only secret detector (high-signal regexes for provider keys, JWTs and private keys, plus an entropy check) in the recorder and importers. It redacts before writing, even in full-content mode.
  - Report a "secrets observed in agent context" count by type and location, never values. That is a security finding the lab will value, delivered by a cost tool.
- **Effort:** S–M. **Evidence:** strong.

#### D3. Redaction alone does not protect proprietary code, which is why content-free analysis is the right default (`ent-redaction-limits`)
- **Facts.**
  - LLM-Redactor (arXiv 2604.12064, 2026-04-13) evaluated eight techniques on a benchmark of 1,300 samples and 4,014 annotations. The best practical combination (local routing plus redaction plus rephrasing) still leaked **31.3% on proprietary code**, against 0.6% on PII. "No single technique dominates."
  - Presidio (a common PII redactor) says "there is no guarantee that Presidio will find all sensitive information".
- **Sources.**
  - https://arxiv.org/abs/2604.12064 — Owusu Agyemang et al. (2026-04-13)
  - https://github.com/microsoft/presidio (accessed)
- **Opportunity.** Do not market "we redact your prompts" as the privacy story. Market "we never need your prompts" (C2/D1). Treat redaction as a second line of defense, and treat code as unredactable.
- **Effort:** S. **Evidence:** moderate (single recent preprint).

### E. Scale and storage

#### E1. The current trace format is O(n²) and holds all content. It needs a content-addressed, streaming, columnar pipeline (`ent-scale-storage`)
- **Facts.**
  - Token Bill writes the full conversation on every line, and `read_trace()` materializes all runs in memory.
  - Illustrative arithmetic, using Token Bill's own 3.7 characters per token: a 200-call session whose context grows linearly to 150k tokens writes about 55 MB of trace. A content-addressed store (each block stored once, calls reference hashes) needs about 0.56 MB, roughly 100× less. These are assumptions, not a measurement.
  - SQLite ships in the standard library. Its own guidance puts it comfortably at 100K hits/day (demonstrated at 10× that) and a 281 TB maximum, and recommends client/server databases only for many concurrent writers.
  - DuckDB reads newline-delimited JSON with globbing, automatic schema detection and gzip/zstd, and works directly with Parquet.
  - The OTLP file format is JSONL with one `TracesData`/`MetricsData`/`LogsData` per line (status Development). The collector `fileexporter` (alpha) supports JSON or proto, rotation, zstd and `group_by` resource attribute.
- **Sources.**
  - local repo `tokenbill/trace.py`, `tokenbill/instrument.py`
  - https://www.sqlite.org/whentouse.html (accessed)
  - https://duckdb.org/docs/current/data/json/loading_json.html (accessed; references DuckDB v1.3.0)
  - https://opentelemetry.io/docs/specs/otel/protocol/file-exporter/ (accessed)
  - https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/exporter/fileexporter/README.md (accessed)
- **Opportunity.**
  - Trace schema v2: a blocks table keyed by HMAC, plus a calls table with ordered block-hash lists and usage.
  - A streaming line reader with per-line size limits.
  - Incremental processing with watermarks and idempotent upserts keyed by `request_id`.
  - Storage tiers: SQLite (stdlib) for a laptop or single job, and an optional `tokenbill[scale]` extra (DuckDB/Parquet) for millions of calls a day. Partition by day and org unit.
  - Publish benchmarks (calls per second, peak RSS) in CI as regression gates. `tests/test_performance.py` is the seed.
- **Effort:** L. **Evidence:** moderate (engineering inference plus primary docs).

### F. Supply chain and project security

#### F1. LLM cost tooling is already a target. LiteLLM was backdoored through CI in March 2026, and tj-actions showed that tag-pinned actions are unsafe (`ent-supply-chain-incidents`)
- **Facts.**
  - **LiteLLM, 2026-03-24:** versions 1.82.7 and 1.82.8 were published with a credential stealer (environment variables, SSH keys, cloud and Kubernetes credentials) and removed from PyPI. LiteLLM: "the compromise originated from the Trivy dependency used in our CI/CD security scanning workflow". Snyk: the compromised component "exfiltrated the `PYPI_PUBLISH` token". Persistence used a `.pth` file that runs on every interpreter start. LiteLLM then rebuilt CI ("CI/CD v2") and began cosign-signing images.
  - **tj-actions/changed-files (CVE-2025-30066, March 2025):** tags v1 through v45.0.7 were repointed to a malicious commit. CISA advised rotating secrets and pinning.
  - **GitHub (2025-08-15):** admins can enforce full-SHA pinning. Unpinned workflows fail, and `!owner/action` blocks an action.
  - **PyPI Trusted Publishing** uses tokens that "expire no more than 15 minutes" after OIDC. PyPI recommends a dedicated environment with **required reviewers**, tag protection, job-level permissions, and a two-step publish job.
  - **Token Bill status (verified 2026-09-23):** the PyPI Integrity API shows a publish attestation for `tokenbill-0.1.2-py3-none-any.whl` from publisher `{repository: sedai77/tokenbill-llm-agent-cost-profiler, workflow: release.yml, environment: pypi}`. The publish job has only download and publish steps. All actions are tag-pinned, and `ci.yml` has no `permissions:`.
- **Sources.**
  - https://docs.litellm.ai/blog/security-update-march-2026 — LiteLLM security update (March 2026)
  - https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/ — Snyk (March 2026)
  - https://www.cisa.gov/news-events/alerts/2025/03/18/supply-chain-compromise-third-party-tj-actionschanged-files-cve-2025-30066-and-reviewdogaction — CISA (2025-03-18, rev. 2025-03-26)
  - https://github.blog/changelog/2025-08-15-github-actions-policy-now-supports-blocking-and-sha-pinning-actions/ (2025-08-15)
  - https://docs.pypi.org/trusted-publishers/security-model/ (accessed)
  - https://pypi.org/integrity/tokenbill/0.1.2/tokenbill-0.1.2-py3-none-any.whl/provenance (queried 2026-09-23)
- **Opportunity.**
  - Keep Token Bill **out of the inference path by default**. The roadmap "recording proxy" should be opt-in, isolated and fail-open.
  - Pin every action to a SHA with Dependabot updates. Add top-level `permissions: {}` or `contents: read`. Require reviewers on the `pypi` environment. Protect `v*` tags.
  - Add harden-runner in egress-block mode on the release job, and zizmor in CI (A3).
  - Document all of this in SECURITY.md as a "supply-chain posture" section the lab can verify.
- **Effort:** S. **Evidence:** strong.

#### F2. Provenance, SBOM and reproducibility: attach verifiable artifacts to every release (`ent-provenance-sbom`)
- **Facts.**
  - PEP 740 attestations have been on by default in `pypa/gh-action-pypi-publish` since **v1.11.0**: "every project making use of Trusted Publishing will start producing and publishing digital attestations". Launch-day adoption was over 20,000 attestations (PyPI blog, 2024-11-14).
  - Consumers verify with `pypi-attestations verify pypi --repository <github-url> <wheel-url>`.
  - GitHub artifact attestations give **SLSA v1.0 Build L2** by default and **L3** with reusable workflows, verified by `gh attestation verify`.
  - SLSA **v1.2** (announced November 2025) adds a Source Track and is backward compatible with v1.1.
  - GitHub **immutable releases** (GA 2025-10-28) lock assets and tags and add release attestations.
  - **PEP 770** (Final, resolved 2025-04-11) reserves `.dist-info/sboms/` for CycloneDX or SPDX SBOMs in wheels.
  - `uv export --format cyclonedx1.5` generates a CycloneDX 1.5 SBOM ("in preview").
  - Hatch builds are reproducible by default and honor `SOURCE_DATE_EPOCH`.
  - Scorecard's **Signed-Releases** check looks for `*.sig`/`*.intoto.jsonl` release assets, and its **SBOM** check looks for SBOM artifacts.
- **Sources.**
  - https://github.com/pypa/gh-action-pypi-publish/releases/tag/v1.11.0 (released 2024-10-30)
  - https://blog.pypi.org/posts/2024-11-14-pypi-now-supports-digital-attestations/ (2024-11-14)
  - https://docs.pypi.org/attestations/consuming-attestations/ (accessed)
  - https://peps.python.org/pep-0740/
  - https://docs.github.com/en/actions/concepts/security/artifact-attestations (accessed)
  - https://slsa.dev/blog/2025/11/announce-slsa-v1.2 (November 2025)
  - https://github.blog/changelog/2025-10-28-immutable-releases-are-now-generally-available/ (2025-10-28)
  - https://peps.python.org/pep-0770/ (Final, 2025-04-11)
  - https://docs.astral.sh/uv/concepts/projects/export/ (accessed)
  - https://hatch.pypa.io/latest/config/build/ (accessed)
  - https://github.com/ossf/scorecard/blob/main/docs/checks.md (accessed)
- **Opportunity.** The release job:
  1. builds with `SOURCE_DATE_EPOCH` from the tag commit
  2. generates a CycloneDX SBOM and embeds it in `.dist-info/sboms/`
  3. runs `actions/attest-build-provenance` (SLSA L2, then L3 through a reusable workflow) and `actions/attest` for the SBOM
  4. publishes an **immutable** GitHub release with wheel, sdist, SBOM and `.intoto.jsonl`
  5. publishes to PyPI with PEP 740

  Add a `docs/VERIFY.md` with copy-paste `pypi-attestations verify` and `gh attestation verify` commands. Add a CI job that rebuilds and diffs hashes as a reproducibility check.
- **Effort:** S. **Evidence:** strong.

#### F3. OpenSSF Scorecard and OSPS Baseline give the lab a checklist, and Token Bill's gaps are cheap to close (`ent-scorecard-osps`)
- **Facts.**
  - Scorecard checks include:
    - Dangerous-Workflow (Critical)
    - Branch-Protection, Code-Review, Maintained, Token-Permissions, Signed-Releases, Vulnerabilities, Dependency-Update-Tool (High)
    - Pinned-Dependencies, SAST, SBOM, Fuzzing, Security-Policy, Packaging (Medium)
    - CI-Tests, License, Contributors, CII-Best-Practices (Low)
  - The **OSPS Baseline 2026-02-19** has 20 Level-1, 16 Level-2 and 24 Level-3 controls. Level 2 includes:
    - OSPS-AC-04.01, least-privilege CI
    - OSPS-BR-06.01, signed releases or signed hash manifest
    - OSPS-GV-01.01, list of members with sensitive access
    - OSPS-LE-01.01, DCO/CLA
    - OSPS-QA-03.01, required checks
    - OSPS-SA-03.01, security assessment
    - OSPS-VM-01.01, CVD policy with timeframe

    Level 2 is described as "for any code project that has at least 2 maintainers".
  - The OpenSSF Best Practices Badge is self-certified and free.
  - `api.securityscorecards.dev` has **no data** for Token Bill yet (queried 2026-09-23).
- **Sources.**
  - https://github.com/ossf/scorecard/blob/main/docs/checks.md (accessed)
  - https://baseline.openssf.org/versions/2026-02-19.html (2026-02-19)
  - https://www.bestpractices.dev/en (accessed)
- **Opportunity.**
  - Add the `ossf/scorecard-action` workflow and publish the badge.
  - Add CodeQL (SAST), Dependabot (actions and uv), and an Atheris or Hypothesis fuzz target for `read_trace` and each importer.
  - Add branch protection, CODEOWNERS, a DCO, and a second maintainer.
  - Complete the Best Practices "passing" badge.
  - Target Scorecard ≥8 and OSPS L2. Add a `docs/SECURITY-ASSESSMENT.md` threat model to satisfy OSPS-SA-03.01.
- **Effort:** S–M. **Evidence:** strong.

#### F4. Regulatory context: EU CRA reporting has applied to manufacturers since 2026-09-11. Labs will ask for a vulnerability-handling process and SBOM (`ent-eu-cra`)
- **Facts.**
  - CRA Article 14 reporting obligations apply to manufacturers **from 11 September 2026**:
    - early warning within 24h
    - notification within 72h
    - final report within 14 days of a corrective measure (vulnerabilities) or 1 month (incidents)
  - Reports go through ENISA's Single Reporting Platform.
  - Open-source stewards' reporting obligations start 11 December 2027.
  - PEP 770 explicitly cites the CRA and NIST SSDF as drivers for SBOMs. NIST SP 800-218 SSDF v1.1 (February 2022) remains the current final version.
- **Sources.**
  - https://digital-strategy.ec.europa.eu/en/policies/cra-reporting (updated 2026-09-11)
  - https://peps.python.org/pep-0770/
  - https://csrc.nist.gov/pubs/sp/800/218/final (February 2022)
- **Opportunity.**
  - Publish a vulnerability-handling policy with SLAs aligned with 24h and 72h timelines.
  - Publish SBOMs (F2) and an SSDF practice mapping in the docs.
  - An internal-only tool may be out of CRA scope, but enterprises integrating it into products will ask.
- **Effort:** S. **Evidence:** strong (moderate relevance).

### G. Deployment, operations, server and user experience

#### G1. Deployment modes the lab expects: CLI, scheduled job, CI gate, collector pipeline and signed container. Keep the core zero-dependency and offline (`ent-deployment-modes`)
- **Facts.**
  - Enterprise telemetry travels over OTLP. The Claude apps gateway fans out OTLP/HTTP only, not gRPC, in protobuf or JSON.
  - The OTel Collector `redactionprocessor` (alpha) fails closed with `allowed_keys` and can hash values.
  - The `fileexporter` writes OTLP JSON lines with rotation and zstd.
  - After its compromise, LiteLLM started signing Docker images with cosign.
  - The Tokenomics paper's "Run" stage includes "CI/CD cost integration".
- **Sources.**
  - https://code.claude.com/docs/en/claude-apps-gateway (accessed)
  - https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/processor/redactionprocessor/README.md (accessed)
  - fileexporter README (accessed)
  - https://docs.litellm.ai/blog/security-update-march-2026
  - https://www.finops.org/wg/token-economics-saas/ (2026-06-03)
- **Opportunity.**
  1. `pip install tokenbill` stays stdlib-only.
  2. `tokenbill[otlp]`: a tiny OTLP/HTTP-JSON receiver (stdlib `http.server` is sufficient) or a reader for collector file output.
  3. A scheduled job container: distroless, non-root, read-only filesystem, cosign-signed, SBOM attached, one image digest per release.
  4. A GitHub Action or reusable workflow for CI (G2).
  5. A reference collector config: receivers, then redaction processor, then Token Bill.

  Also document exit codes, JSON logs, configuration precedence (flags > env > file), proxy and air-gap install (wheel only), and Windows/macOS/Linux support.
- **Effort:** M. **Evidence:** moderate.

#### G2. A cost regression gate in CI: fail the pull request that introduces a cache breaker, before it costs anything (`ent-ci-cost-gate`)
- **Facts.**
  - Anthropic's guidance: "Verify from usage, not from code review — and re-verify after every prompt-assembly change". On a warmed loop, `cache_read_input_tokens` should dominate.
  - Cache diagnostics gives a server-side verdict per turn (C2).
  - Token Bill already has a deterministic planted-waste harness (the flagship test).
  - The FinOps Run stage calls for CI/CD cost integration.
- **Sources.**
  - bundled `claude-api/shared/cost-optimization.md` §2 (authoritative local copy)
  - https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics (accessed)
  - https://www.finops.org/wg/token-economics-saas/ (2026-06-03)
- **Opportunity.** `tokenbill check` for CI:
  - runs the team's agent smoke test, or replays a recorded fixture in metadata-only mode
  - asserts budgets: cache-read share ≥ X% after turn 2, no new breaker, cost per task ≤ baseline + tolerance
  - emits SARIF and a PR comment showing Δ$/1k runs

  It turns Token Bill from a report into a guardrail that runs on every PR, which is where enterprise-scale savings compound.
- **Effort:** M. **Evidence:** moderate.

#### G3. If a shared server or dashboard is built, it must meet enterprise identity and application-security baselines. Local-first remains the lower-risk default (`ent-server-sso-rbac`)
- **Facts.**
  - The Claude apps gateway pattern: OIDC SSO, RBAC by IdP group, per-mutation audit rows attributed to `admin-key:<id>` or `oidc:<sub>`, fail-open versus fail-closed as an explicit setting, and retention tables.
  - SCIM 2.0 protocol is RFC 7644 (September 2015).
  - OAuth 2.0 Security BCP is RFC 9700 (January 2025).
  - OWASP ASVS **5.0.0** was released in May 2025.
- **Sources.**
  - https://code.claude.com/docs/en/claude-apps-gateway-spend-limits (accessed)
  - https://www.rfc-editor.org/rfc/rfc7644 (2015-09)
  - https://www.rfc-editor.org/rfc/rfc9700 (2025-01)
  - https://github.com/OWASP/ASVS (5.0.0, May 2025)
- **Opportunity.**
  - Phase 1: a static, self-contained HTML showback site generated per team, with no server. Host it behind the enterprise's existing SSO proxy.
  - Phase 2 (optional): a read-only API and dashboard with OIDC login, RBAC mapped from IdP groups (viewer/team-lead/finops/admin, with team-scoped row-level filtering), SCIM or JIT provisioning, an append-only audit log, and an ASVS L2 self-assessment.
  - Do not build password auth.
- **Effort:** L. **Evidence:** strong (standards).

#### G4. Reports must meet WCAG 2.2 AA and resist hostile input (`ent-report-a11y-hardening`)
- **Facts.**
  - WCAG 2.2 is a W3C Recommendation, updated 2024-12-12. The AA-relevant criteria for a data-heavy report are 1.1.1 Non-text Content, 1.3.1 Info and Relationships, 1.4.1 Use of Color, 1.4.3 Contrast (4.5:1), 1.4.11 Non-text Contrast (3:1), 1.4.10 Reflow, 2.1.1 Keyboard and 2.5.8 Target Size (24×24 px).
  - Token Bill's SVGs have `role="img"` and `aria-label`, but there are no data tables behind the charts, and color is the only series distinction.
  - SECURITY.md documents possible ANSI escape injection in terminal output. The report has no CSP `<meta>`.
- **Sources.**
  - https://www.w3.org/TR/WCAG22/ (Recommendation, updated 2024-12-12)
  - local repo `tokenbill/report.py`, `SECURITY.md`
- **Opportunity.**
  - Add a visually-hidden or toggleable `<table>` with `<caption>` for every chart, pattern or label encodings in addition to color, a verified contrast palette in both themes, and keyboard-reachable details.
  - Add an axe-core check in CI (optional dev dependency).
  - Add `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">`.
  - Strip C0/C1 control characters from all terminal output.
  - Fuzz the parsers (F3).
- **Effort:** S. **Evidence:** strong.

#### G5. The market and the new Tokenomics Foundation: where Token Bill can lead (`ent-market-tokenomics`)
- **Facts.**
  - Anthropic lists usage and cost partners: CloudZero, Datadog, Grafana Cloud, Harness, Honeycomb, Vantage.
  - Gateways (LiteLLM, Portkey, Helicone), observability tools (Langfuse and others) and local CLIs (ccusage) all show tokens and cost.
  - Claude Code now shows per-session cache misses with a likely cause.
  - None of the documentation read claims fleet-level **causal waste attribution with recoverable dollars and fix verification**, which is Token Bill's thesis. (This is a reading of product docs, not an exhaustive market audit.)
  - The Linux Foundation announced the **Tokenomics Foundation** (intent 2026-06-03, launch 2026-08-04, governing board convened 2026-07-30). It has 30 founding organizations, including Vantage, Finout, Flexera, Cast.ai, Kion, IBM, JPMorganChase, SAP and ServiceNow. Its scope includes "Token value/density definitions (input, output, reasoning, cache)", "Cost-to-serve measurement methodology" and "**Token Cost Telemetry schemas**", with planned input into "FOCUS v1.5 and beyond".
- **Sources.**
  - https://platform.claude.com/docs/en/manage-claude/usage-cost-api (partner list, accessed)
  - https://www.linuxfoundation.org/press/linux-foundation-launches-the-tokenomics-foundation-to-define-the-economics-and-roi-of-ai-value (2026-08-04)
  - https://www.ciodive.com/news/foundation-tackle-ai-token-cost-management/822839/ (2026-06-12)
  - https://ccusage.com/guide/cost-modes (accessed)
  - https://code.claude.com/docs/en/costs (accessed)
- **Opportunity.**
  - Position Token Bill as the **open reference implementation of "waste" and "recoverable cost" metrics** on top of FOCUS and OTel.
  - Propose the breaker taxonomy and the redundancy metric to the Tokenomics Foundation's telemetry-schema work.
  - Publish a validator that other vendors can run.
  - This is also the exposure path the user is looking for.
- **Effort:** S. **Evidence:** moderate.

---

## 4. What an enterprise lab will test before approving (checklist with pass criteria)

| # | Test the lab runs | Pass criterion Token Bill should target | Findings |
|---|---|---|---|
| 1 | Install from PyPI in an isolated or air-gapped environment, then verify provenance | `pypi-attestations verify pypi --repository https://github.com/sedai77/tokenbill-llm-agent-cost-profiler <wheel-url>` succeeds. `gh attestation verify` succeeds for GitHub assets. SBOM present. Rebuild is byte-identical. | F1, F2 |
| 2 | Supply-chain scorecard | Scorecard ≥8/10 (Token-Permissions, Pinned-Dependencies, Signed-Releases, SBOM, SAST, Dependency-Update-Tool, Branch-Protection all passing). OSPS Baseline L2. Best Practices "passing". | F3 |
| 3 | Network egress | No sockets opened by core commands (verify under a network-deny sandbox). Connectors only reach documented Anthropic, AWS or OTLP endpoints and only when invoked. | G1 |
| 4 | Data-protection review (DPIA) | Default tier stores no prompt or code content. Identifiers HMAC'd with a separate key. Retention settings. Erasure command. Data-flow diagram. | D1, C2 |
| 5 | Secrets handling | Planted secrets in a test transcript never reach disk or reports. Secret-exposure summary produced. | D2 |
| 6 | Hostile input | Fuzzed JSONL, OTLP and CSV inputs produce clean errors and bounded memory. No XSS in the report (CSP present). No ANSI injection in the terminal. | G4, F3 |
| 7 | **Accuracy and reconciliation** | For a closed 30-day window, Token Bill totals by day × workspace × model are within an agreed tolerance (for example ≤1%) of the Cost API or Enterprise Analytics. Residuals are explained. | C1, B2 |
| 8 | Recommendation validity | For each breaker, the counterfactual is reproducible and a before/after (A/B or cache diagnostics) confirms the realized savings. False-positive rate reported on a labeled set. | C2, G2 |
| 9 | Scale | Ingest ≥10M calls (or 30 days of org OTel) with stable RSS. Incremental re-runs are idempotent. Throughput published. | E1 |
| 10 | Attribution coverage | ≥95% of spend allocated to a team or cost center. Unallocated spend shown explicitly. | C6 |
| 11 | Standards interop | FOCUS 1.4 export passes the column requirements (x_ naming, ServiceCategory). OTel output uses current GenAI names with old names accepted. | A1–A3 |
| 12 | Operability | Documented config precedence, exit codes, JSON logs, container signed and non-root, scheduled-job runbook, supported OS and Python matrix. | G1 |
| 13 | Accessibility | WCAG 2.2 AA spot-check of HTML reports (tables behind charts, contrast, keyboard). | G4 |
| 14 | Governance and support | ≥2 maintainers, CVD policy with SLA, semver plus schema versioning, deprecation policy, changelog. | F3, F4 |
| 15 | Identity (only if a server exists) | OIDC SSO, IdP-group RBAC, SCIM or JIT, audit log, ASVS L2 self-assessment. | G3 |

---

## 5. Canonical usage mapping (for the normalizer and its tests)

| Canonical bucket (disjoint) | Anthropic Messages API | OTel GenAI (new repo) | OTel semconv ≤1.41 (legacy) | OpenInference | Claude Code OTel | Claude Code Analytics API |
|---|---|---|---|---|---|---|
| uncached_input | `input_tokens` | `gen_ai.usage.input_tokens` − cache_read − cache_write | same, with `cache_creation` | `llm.token_count.prompt` − `prompt_details.cache_read` − `prompt_details.cache_write` | `type=input` / `input_tokens` | `tokens.input` |
| cache_read | `cache_read_input_tokens` | `gen_ai.usage.cache_read.input_tokens` | `gen_ai.usage.cache_read.input_tokens` | `llm.token_count.prompt_details.cache_read` | `type=cacheRead` / `cache_read_tokens` | `tokens.cache_read` |
| cache_write (5m + 1h) | `cache_creation_input_tokens` = Σ `cache_creation.ephemeral_{5m,1h}_input_tokens` | `gen_ai.usage.cache_write.input_tokens` | `gen_ai.usage.cache_creation.input_tokens` | `llm.token_count.prompt_details.cache_write` | `type=cacheCreation` / `cache_creation_tokens` | `tokens.cache_creation` |
| output (incl. reasoning) | `output_tokens` | `gen_ai.usage.output_tokens` (billed count) | same | `llm.token_count.completion` | `type=output` / `output_tokens` | `tokens.output` |
| reasoning_output (subset) | not split separately in usage | `gen_ai.usage.reasoning.output_tokens` | same | `llm.token_count.completion_details.reasoning` | none | none |
| counter metrics | none | `gen_ai.client.inference.usage.{input_tokens,output_tokens,cache_read.input_tokens,cache_write.input_tokens,reasoning.output_tokens}` | `gen_ai.client.token.usage` histogram (legacy) | none | `claude_code.token.usage`, `claude_code.cost.usage` | none |

Rule: Anthropic `input_tokens` **excludes** cache. OTel and OpenInference `input`/`prompt` **include** it. Langfuse `usage_details` buckets are mutually exclusive. Each adapter needs a golden test showing that the sum of buckets equals the provider's billed total, and that only the 5m/1h split changes the price.

---

## 6. What Token Bill should build

The principle: **stay local-first, content-free by default and out of the inference path. Ingest the telemetry enterprises already have, and be the tool whose numbers reconcile with the invoice and whose every alert comes with a cause and a dollar value.**

### Phase 0: "passes security review" (1–2 weeks, effort S)
1. CI hardening:
   - SHA-pin all actions and add Dependabot for actions and uv
   - `permissions: {}` at the top with per-job grants
   - zizmor in CI
   - harden-runner (egress block) on release
   - required reviewers on the `pypi` environment, `v*` tag protection, branch protection with CODEOWNERS
2. Release artifacts: CycloneDX SBOM (embedded per PEP 770 and attached), SLSA provenance through `actions/attest-build-provenance` (then reusable-workflow L3), **immutable** GitHub releases, `docs/VERIFY.md`, and a reproducibility check.
3. Scorecard action and badge, CodeQL, fuzz targets for `read_trace`, OpenSSF Best Practices badge, threat model (`docs/SECURITY-ASSESSMENT.md`), CVD SLA, a second maintainer.
4. Report and CLI hardening: CSP meta, control-character stripping, WCAG 2.2 AA fixes (tables behind charts, non-color encodings).

### Phase 1: "numbers the CFO trusts" (3–6 weeks, effort M)
5. **Rate card and money:** Decimal or micro-cents; effective-dated list prices; contract `multiplier` and `overrides` compatible with Claude Code `modelPricing`; 5m and 1h writes; batch, data-residency, fast and priority modifiers; unknown-model policy (flag, never silently $0).
6. **CanonicalUsage plus adapters** (section 5) with golden conformance tests.
7. **Connectors (read-only, least-privilege):** Anthropic Usage API, Cost API, Claude Code Analytics API, Enterprise Analytics API (with the 30-day provisional window), Bedrock CUR 2.0, gateway `/spend_limits/effective`, LiteLLM SpendLogs.
8. **`tokenbill reconcile`** with variance report and CI tolerance.
9. **FOCUS 1.4 export** with `x_` Token Bill columns. Track FOCUS 1.5 and #2018.

### Phase 2: "privacy-safe at fleet scale" (4–8 weeks, effort M–L)
10. **Data tiers** (aggregate / fingerprint / full-content-local-only), HMAC identity, retention and purge, DPIA pack.
11. **Metadata-only recorder** that opts into Anthropic cache diagnostics and records `diagnostics.cache_miss_reason` plus usage and block fingerprints. Falls back to local fingerprints on Bedrock and Vertex.
12. **Claude Code on-device importer** (hash-only, scheduled before the 30-day cleanup) and a **secret scanner** that redacts before persisting.
13. **OTel ingest and emit:** OTLP/HTTP-JSON receiver and collector file reader. Accept `claude_code.*` and old and new `gen_ai.*` names. Emit `gen_ai.client.inference.usage.*` plus `tokenbill.*` waste attributes. Ship a reference collector config with the redaction processor.
14. **Trace schema v2:** content-addressed blocks, streaming reader, incremental watermarks, SQLite default, optional `tokenbill[scale]` DuckDB/Parquet, published throughput and RSS benchmarks.

### Phase 3: "FinOps-native" (4–8 weeks, effort M)
15. **Allocation engine** (rules in git, coverage KPI, shared-cost split) and **showback** per team (static HTML/CSV/FOCUS), then chargeback exports.
16. **Unit economics:** cost per session, PR, commit, accepted edit and successful task, joined from Claude Code Analytics. Levers mapped to the FinOps Foundation table.
17. **`tokenbill watch`:** robust per-cohort anomaly detection (EWMA plus MAD on log-cost, cache-hit collapse, runaway loops, reasoning blow-ups), every alert with probable cause and $ per day, MTTD and anomaly-cost-% KPIs, webhook/OTel outputs.
18. **Forecasts and budgets:** driver-based P50/P90 forecasts, a what-if forecast with fixes applied, cap recommendations exported for the gateway, Console or AWS Budgets (human-applied).
19. **`tokenbill check` CI gate:** SARIF and PR comment with Δ$/1k runs. Fails on a new breaker or cache-share regression.

### Phase 4: optional shared service (effort L)
20. Read-only dashboard and API behind OIDC with IdP-group RBAC, SCIM or JIT, audit log, ASVS L2 self-assessment. Signed distroless container. Helm chart.
21. Engage the **Tokenomics Foundation** and FOCUS working groups: contribute the waste and breaker taxonomy and a conformance validator.

---

## 7. Open questions
- Which Anthropic surfaces does the enterprise use: Console API, Claude Enterprise (seat-based or usage-based), Bedrock, Vertex, Foundry, or the Claude apps gateway? This decides which connectors matter. On seat-based Enterprise plans, the Analytics cost endpoints reflect usage credits only.
- Will the enterprise's DPIA allow any prompt or code content to be processed centrally, or only aggregates and fingerprints? Is an org-held HMAC key acceptable?
- What reconciliation tolerance will the lab accept (for example ≤1% per day × workspace × model on closed periods)?
- When will FOCUS 1.5 ship, and will it add dedicated token columns or keep the #2018 ConsumedQuantity mapping? The release date was not confirmed from a primary source.
- Will the OTel GenAI repo keep `cache_write` (renamed from `cache_creation`) when it publishes a `schema_url` and stabilizes?
- Cache diagnostics is beta and first-party only. How will breaker detection work equally well on Bedrock and Vertex without content (local fingerprints only)?
- Does the enterprise expect a hosted multi-tenant service, or will local-first plus static showback plus OTel integration suffice?
- Can Token Bill recruit a second maintainer or organizational steward before submission (OSPS L2, Scorecard Contributors)?
- Is Token Bill "placed on the market" under the EU CRA in the enterprise's context, or purely internal?

---

## 8. Source index (all opened during this pass)
- FOCUS 1.4 intro: https://www.finops.org/insights/introducing-focus-1-4/ (June 2026)
- FOCUS spec site: https://focus.finops.org/focus-specification/ (accessed 2026-09-23)
- FOCUS repo and v1.4 spec files: https://github.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS_Spec (v1.4 2026-06-04; v1.3 2025-12-08)
- FOCUS issue #2018: https://github.com/FinOps-Open-Cost-and-Usage-Spec/FOCUS_Spec/issues/2018 (2026-02-24, updated 2026-07-23)
- FinOps for AI Overview: https://www.finops.org/wg/finops-for-ai-overview/ (2026-02-17)
- FinOps AI technology category: https://www.finops.org/framework/technology-categories/ai/ (accessed)
- FinOps Tokenomics (AI Value WG): https://www.finops.org/wg/token-economics-saas/ (2026-06-03)
- FinOps AI forecasting: https://www.finops.org/wg/effect-of-optimization-on-ai-forecasting/ (2026-03-17)
- FinOps capabilities (anomaly, forecasting, allocation): https://www.finops.org/framework/capabilities/anomaly-management/ , /forecasting/ , /allocation/ (accessed)
- SiliconANGLE FOCUS and AI tokens: https://siliconangle.com/2026/06/08/ai-token-economics-focus-specification-updates-finopsx/ (2026-06-08)
- Tokenomics Foundation launch: https://www.linuxfoundation.org/press/linux-foundation-launches-the-tokenomics-foundation-to-define-the-economics-and-roi-of-ai-value (2026-08-04)
- CIO Dive on Tokenomics Foundation: https://www.ciodive.com/news/foundation-tackle-ai-token-cost-management/822839/ (2026-06-12)
- OTel GenAI semconv repo: https://github.com/open-telemetry/semantic-conventions-genai (created 2026-05-05); docs anthropic.md, gen-ai-token-metrics.md, non-normative/token-metrics-design.md (accessed)
- OTel registry (moved notice): https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/ (accessed)
- OTel semconv changelog: https://github.com/open-telemetry/semantic-conventions/blob/main/CHANGELOG.md (v1.40.0 2026-02-19; v1.42.0 2026-06-12)
- OTel GenAI blog: https://opentelemetry.io/blog/2026/genai-observability/ (2026)
- OTLP file exporter spec: https://opentelemetry.io/docs/specs/otel/protocol/file-exporter/ (accessed)
- Collector fileexporter and redactionprocessor READMEs: https://github.com/open-telemetry/opentelemetry-collector-contrib (accessed)
- OpenInference semconv: https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md (accessed)
- Claude Code monitoring: https://code.claude.com/docs/en/monitoring-usage (accessed)
- Claude Code costs: https://code.claude.com/docs/en/costs (accessed)
- Claude Code settings (`modelPricing`): https://code.claude.com/docs/en/settings-reference (accessed)
- Claude Code `.claude` directory: https://code.claude.com/docs/en/claude-directory (accessed)
- Claude apps gateway: https://code.claude.com/docs/en/claude-apps-gateway (accessed)
- Claude apps gateway spend limits: https://code.claude.com/docs/en/claude-apps-gateway-spend-limits (accessed)
- Anthropic Usage and Cost API: https://platform.claude.com/docs/en/manage-claude/usage-cost-api (accessed)
- Claude Code Analytics API: https://platform.claude.com/docs/en/manage-claude/claude-code-analytics-api (accessed)
- Analytics APIs (Enterprise): https://platform.claude.com/docs/en/manage-claude/analytics-api (accessed)
- Prompt caching: https://platform.claude.com/docs/en/build-with-claude/prompt-caching (accessed)
- Cache diagnostics: https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics (beta 2026-04-07; accessed)
- Bundled Anthropic docs (authoritative local): claude-api/shared/admin-api.md, cost-optimization.md, prompt-caching.md, platform-availability.md
- OpenAI prompt caching: https://developers.openai.com/api/docs/guides/prompt-caching (accessed)
- AWS Bedrock granular cost attribution: https://aws.amazon.com/blogs/machine-learning/introducing-granular-cost-attribution-for-amazon-bedrock/ (2026-04-17)
- AWS Cost Anomaly Detection: https://docs.aws.amazon.com/cost-management/latest/userguide/manage-ad.html (accessed)
- LiteLLM spend tracking: https://docs.litellm.ai/docs/proxy/cost_tracking (accessed)
- LiteLLM incident: https://docs.litellm.ai/blog/security-update-march-2026 (March 2026); Snyk: https://snyk.io/blog/poisoned-security-scanner-backdooring-litellm/ (March 2026)
- Langfuse cost tracking: https://langfuse.com/docs/observability/features/token-and-cost-tracking (accessed)
- ccusage: https://ccusage.com/guide/cost-modes (accessed)
- CISA tj-actions alert: https://www.cisa.gov/news-events/alerts/2025/03/18/supply-chain-compromise-third-party-tj-actionschanged-files-cve-2025-30066-and-reviewdogaction (2025-03-18)
- GitHub SHA-pinning policy: https://github.blog/changelog/2025-08-15-github-actions-policy-now-supports-blocking-and-sha-pinning-actions/ (2025-08-15)
- GitHub immutable releases GA: https://github.blog/changelog/2025-10-28-immutable-releases-are-now-generally-available/ (2025-10-28)
- GitHub artifact attestations: https://docs.github.com/en/actions/concepts/security/artifact-attestations (accessed)
- PyPI Trusted Publishing security model: https://docs.pypi.org/trusted-publishers/security-model/ (accessed)
- PyPI attestations blog: https://blog.pypi.org/posts/2024-11-14-pypi-now-supports-digital-attestations/ (2024-11-14)
- PyPI consuming attestations: https://docs.pypi.org/attestations/consuming-attestations/ (accessed)
- gh-action-pypi-publish v1.11.0: https://github.com/pypa/gh-action-pypi-publish/releases/tag/v1.11.0 (2024-10-30)
- PEP 740: https://peps.python.org/pep-0740/ ; PEP 770: https://peps.python.org/pep-0770/ (Final, 2025-04-11)
- SLSA v1.2: https://slsa.dev/blog/2025/11/announce-slsa-v1.2 (November 2025)
- uv export (CycloneDX): https://docs.astral.sh/uv/concepts/projects/export/ (accessed)
- Hatch reproducible builds: https://hatch.pypa.io/latest/config/build/ (accessed)
- OpenSSF Scorecard checks: https://github.com/ossf/scorecard/blob/main/docs/checks.md (accessed)
- OSPS Baseline: https://baseline.openssf.org/versions/2026-02-19.html (2026-02-19)
- OpenSSF Best Practices: https://www.bestpractices.dev/en (accessed)
- harden-runner: https://github.com/step-security/harden-runner (accessed)
- zizmor: https://github.com/zizmorcore/zizmor (accessed)
- EU CRA reporting: https://digital-strategy.ec.europa.eu/en/policies/cra-reporting (updated 2026-09-11)
- NIST SSDF SP 800-218: https://csrc.nist.gov/pubs/sp/800/218/final (February 2022)
- EDPB Guidelines 01/2025 pseudonymisation: https://www.edpb.europa.eu/our-work-tools/documents/public-consultations/2025/guidelines-012025-pseudonymisation_en (adopted 2025-01-16)
- GitGuardian Secrets Sprawl 2026: https://blog.gitguardian.com/the-state-of-secrets-sprawl-2026/ (2026-03-17)
- OWASP LLM Top 10 2025 and LLM10: https://genai.owasp.org/resource/owasp-top-10-for-llm-applications-2025/ ; https://genai.owasp.org/llmrisk/llm102025-unbounded-consumption/
- Presidio: https://github.com/microsoft/presidio (accessed)
- OWASP ASVS: https://github.com/OWASP/ASVS (5.0.0, May 2025)
- RFC 7644 SCIM: https://www.rfc-editor.org/rfc/rfc7644 (2015-09); RFC 9700 OAuth BCP: https://www.rfc-editor.org/rfc/rfc9700 (2025-01)
- WCAG 2.2: https://www.w3.org/TR/WCAG22/ (updated 2024-12-12)
- SQLite appropriate uses: https://www.sqlite.org/whentouse.html (accessed)
- DuckDB JSON loading: https://duckdb.org/docs/current/data/json/loading_json.html (accessed)
- NIST/SEMATECH EWMA: https://www.itl.nist.gov/div898/handbook/pmc/section3/pmc324.htm (accessed)
- arXiv 2604.22750: Bai et al., "How Do AI Agents Spend Your Money? Analyzing and Predicting Token Consumption in Agentic Coding Tasks" (2026-04-24): https://arxiv.org/abs/2604.22750
- arXiv 2604.12064: Owusu Agyemang et al., "LLM-Redactor: An Empirical Evaluation of Eight Techniques for Privacy-Preserving LLM Requests" (2026-04-13): https://arxiv.org/abs/2604.12064
- arXiv 2606.07968: Aziz & Kibria, "RecurGuard: Runtime Monitoring for Reasoning-Token Consumption Attacks" (2026-06-06): https://arxiv.org/abs/2606.07968
