# Fact-check: competitors track

Checked 2026-09-23. I opened every cited URL with WebFetch or curl, and I pulled star counts and licenses from the GitHub REST API. Web search was unavailable (the session budget was used up), so I checked each claim against the cited pages and the pages they link to. I did not use outside searching.

**Tally (28 findings):** 14 confirmed, 14 corrected, 0 unverifiable, 0 refuted.

The main problems in the report:
- Wrong GitHub star count for RTK.
- Wrong status code for LiteLLM tag budgets.
- Portkey's semantic caching is listed as Enterprise-only, but it is on the $49 Production plan.
- The ProjectDiscovery saving is described as a bill cut, but it was measured against a no-cache counterfactual.
- Langfuse's "spend alerts" are about the Langfuse bill, not LLM spend.
- Three numbers do not appear in the cited sources: Langfuse's ">2,000 paying customers", Not Diamond's "30%+ coding router" and Copilot's "1 credit = $0.01".

---

## Confirmed

### anth-cache-diagnostics
Checked the live docs page https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics (beta header `cache-diagnosis-2026-04-07`) and the bundled `prompt-caching.md` and `platform-availability.md` (v2.1.280). Everything in the finding matches:
- The request field is `diagnostics.previous_message_id`.
- The reason types are `model_changed`, `system_changed`, `tools_changed`, `messages_changed`, `previous_message_not_found` and `unavailable`.
- `cache_missed_input_tokens` is "a magnitude indicator rather than a billing number".
- It reports "the earliest divergence only".
- It is available on the Claude API only. It is not available on Bedrock, Google Cloud, Foundry or Claude Platform on AWS.
- Fingerprints are kept for a limited time and scoped to the organization and workspace.
- It returns `unavailable` when `tool_choice`, `thinking`, `context_management`, `output_config`/`output_format` or the set of beta headers differ, and on very long conversations.

### cc-usage-likely-cause
Checked https://code.claude.com/docs/en/costs. All points match:
- The `Prompt cache (main)` line requires v2.1.251. The likely-cause text requires v2.1.260.
- A miss is counted when a request re-processes more than 5% and at least 2,000 tokens.
- The line covers the main conversation only, not subagents.
- Plan-usage attribution covers skills, subagents, plugins and MCP servers. Behavior flags appear at 10% or more.
- The baseline is "$13 per developer per active day and $150-250 per developer per month".

Two caveats: the plan-usage breakdown appears only on Pro, Max, Team and Enterprise plans, and it is computed from local history on one machine.

### anth-admin-apis-org-scan
Checked three Anthropic doc pages. All points match.
- **Usage and Cost API** (https://platform.claude.com/docs/en/manage-claude/usage-cost-api):
  - `1m`/`1h`/`1d` buckets.
  - Filter and group by API key, workspace, model, service tier, context window, data residency (`inference_geo`) and speed (beta).
  - Data typically appears within 5 minutes.
  - The cost report is daily only and excludes Priority Tier.
  - The API reference response has `cache_creation.ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens`.
- **Claude Code Analytics API**:
  - Per-user daily sessions, lines, commits, PRs, tool accept/reject counts, and tokens and estimated cost by model.
  - Data is delayed by up to about 1 hour.
  - Usage on Bedrock, Foundry, Google Cloud and Claude Platform on AWS is not included.
- **Enterprise Analytics API** (https://platform.claude.com/docs/en/manage-claude/analytics-api):
  - Cost data is typically available within 4 hours, and can take up to 24.
  - Values can be revised for 30 days.
  - Data starts on 2026-01-01.

Caveats:
- Enterprise cost endpoints apply to usage-based Enterprise plans. On seat-based plans they show usage credits only.
- Enterprise Analytics does not return Claude Code activity that runs through Bedrock.

### anth-published-lever-benchmarks
Checked https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence.md. All numbers match:
- Caching cuts cost by a factor of 2.7 to 5.3, on runs that read 79–90% of input from cache.
- Median real traffic reads 84% from cache (ref 17: 84.2% over the 14 days ending 2026-08-23). The top 10% reach 94% or more.
- The 1-hour TTL pays off when about 1 turn in 20 follows a pause of 5 to 60 minutes.
- Batch is 50% off.
- Tool search saves 45% (at 502 tools).
- Medium effort on SWE-bench Pro costs about half for about 2 points.
- Task budgets save 44–58% for 3 to 6 points.
- Context editing on the short run cost 74% more.

The bundled `cost-optimization.md` does say "a factor of 2.5 to 3.7 off at 81% to 90%". Its task-budget numbers are also older: 18% for 2.7 points and 47% for 4.4 points.

### cc-enterprise-breaker-pack
Checked https://code.claude.com/docs/en/prompt-caching. All points match:
- Model switches invalidate the cache, including `opusplan`, automatic fallback and skill frontmatter that names a `model`.
- Effort changes keep the cache on Opus 5.5 and Fable 5.1 "with an API key or a Claude subscription". This does not apply on Bedrock, Google Cloud or the apps gateway, under a HIPAA configuration, or with `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`.
- The fast-mode header invalidates the cache.
- MCP changes invalidate the cache when tools are loaded into the prefix, which happens when tool search is unavailable, "with a custom `ANTHROPIC_BASE_URL` gateway".
- Upgrades, compaction and image eviction invalidate the cache.
- "Removes the markers while returning success: your entire conversation history bills as uncached input on every turn."
- The TTL is 5 minutes for API-key and cloud users. `promptCacheTtl` changes it.
- The cache is scoped by directory, and worktrees do not share it.

### cc-apps-gateway-routing-tax
Checked https://code.claude.com/docs/en/claude-apps-gateway and the `-spend-limits` page. All points match:
- The gateway ships in the `claude` binary (`claude gateway`).
- It uses OIDC and gives model access by IdP group.
- It relays OTLP.
- Caps are daily, weekly or monthly, set per user, `rbac_group` or organization, and return `429` with `billing_error`.
- It supports `pricing.overrides` and `pricing.multiplier`.
- The 1-hour cache TTL is "Not available".
- "First-party-only optimizations such as global cache scope and token-efficient tools" are "Not available".

Clarification: "fails open by default" applies to spend enforcement when Postgres is unreachable. Gateway boot is fail-closed.

### datadog-agent-console-fix-library
Checked https://www.datadoghq.com/blog/claude-code-monitoring/ (2026-06-09, Preview). All points match:
- It covers Claude Code, Cursor and Copilot.
- Spend is broken down by agent, team and user, compared with lines, commits and PRs, and with DORA-aligned metrics.
- It detects skipped checks, retry loops and file re-reads.
- Its Fix Library includes a PreToolUse hook that can be deployed through a single PR.

### datadog-1m-month-case
Checked https://www.datadoghq.com/blog/how-datadog-saves-money-by-optimizing-ai-usage/ (published 2026-08-26). All numbers match:
- Opus 4.8 to Sonnet 4.6 saved $687,000 a month, at "8% loss of proficiency" for a 36.7% cost cut, over "140 different evaluations".
- Changing effort from high to medium saved $288,000 a month.
- Headroom cut cost 47% in evals. In the A/B test: −27.0% cost per user ($156.70 to $114.40), −39.3% input tokens, −35.7% output tokens.
- 768 users were nudged, and spend fell by more than $150,000.

Caveats:
- The Headroom A/B is a one-week snapshot from ">1,000 engineers".
- The $150k compares the week before with the week after, with no control group.

### headroom-compression-cache-aware
Checked the GitHub API and README. All points match:
- Apache-2.0, 73,631 stars.
- It runs as a library, a proxy, a `headroom wrap claude|codex|cursor|...` wrapper, or an MCP server.
- Claims: "20% fewer tokens" for coding agents and 60–95% for JSON and structured data.
- CacheAligner "flags volatile content that would bust a provider KV-cache prefix".
- Datadog measured −27% (see above).

"Leads" is the report's interpretation.

### ccusage-local-parity
Checked the ccusage README, the Claude-Code-Usage-Monitor repo and the Terse comparison. All points match:
- ccusage has 18,709 stars and is MIT. The repo has moved to `ccusage/ccusage`, and the app's LICENSE is MIT.
- It reads 18 sources, including Claude Code, Codex, Copilot CLI and Gemini CLI.
- It reports daily, weekly, monthly, session and 5-hour-block views, with `--json`.
- It tracks cache creation and cache reads separately.
- It has no root-cause analysis.
- Claude-Code-Usage-Monitor has 8,716 stars (MIT) and gives burn-rate predictions.
- The Terse page (updated 2026-08-28) says "answers it after the money is gone".

Note that Terse wrote the comparison and is itself a vendor.

### promptcachelint-overlap
Checked the GitHub API and README. All points match:
- MIT, 0 stars, created 2026-09-20.
- Python 3.11+, with zero core dependencies.
- It wraps the SDK transport and diffs consecutive requests.
- Codes CL001–CL014 cover timestamps (CL012), tool changes (CL002), parameter shifts (CL003), TTL (CL006) and the minimum prefix (CL011).
- It supports Anthropic and OpenAI, and has `--fail-on`.

### openrouter-cache-accounting
Checked the OpenRouter prompt-caching and usage-accounting docs. All points match:
- Usage is always included, with `cost`, `cached_tokens`, `cache_write_tokens` and `cost_details.upstream_inference_cost` (BYOK only).
- The `cache_discount` field in the response body can be negative on Anthropic cache writes.
- Sticky routing lasts for 10 minutes of inactivity, or follows `session_id`.
- Read multipliers: DeepSeek 0.1×, Gemini 0.25×, Groq 0.5×.

### obs-platforms-no-cache-rca
Checked each platform's docs, pricing page or LICENSE:
- **LangSmith:** handles `cache_read`/`cache_creation`, and "does not reflect updates to the model pricing map in the costs for traces already logged". Plus is "$39 / seat per month" (langchain.com/pricing).
- **Braintrust:** Starter is $0, Pro is $249 a month, Enterprise is custom. It has SOC 2 Type II. Loop is available on Pro and Enterprise.
- **Weave:** "The Weave TypeScript doesn't support cost tracking."
- **Phoenix:** has `cache_read` and `cache_write` attributes, rolled up by trace, session and project. Its LICENSE is Elastic License 2.0.
- **New Relic:** the intro doc does not mention cache tokens.

None of the docs describe counterfactual replay.

### finops-foundation-focus-tokens
Checked three sources:
- **Linux Foundation press release** (2026-02-19): 98% manage AI spend, up from 31%. 1,192 respondents, more than $83B in spend.
- **FinOps X keynote** (2026-06-09): "the FinOps Foundation and Linux Foundation announced the intent to form the Tokenomics Foundation".
- **SiliconANGLE** (2026-06-08): covers the FOCUS 1.4 launch. It describes the working group considering per-user, per-session and per-request token data and OpenTelemetry integration.

### outcome-unit-costing
Checked Revenium and Tokenwise:
- **Revenium** (27 Mar 2026): CONVERTED/ESCALATED/DEFLECTED outcomes. The loan example matches: 1,000 jobs, $2,950, 780 approvals, $3.78 per conversion.
- **Tokenwise** (updated 2026-05-29): "1-line proxy swap" and "a weekly plan to cut your bill ~30%". This is a vendor claim.

---

## Corrected

### cc-otel-pipeline
**What is right.** The metrics and attributes match https://code.claude.com/docs/en/monitoring-usage:
- `cost.usage` and `token.usage` with `type` = input, output, cacheRead, cacheCreation.
- `query_source`, `effort`, `speed`, `agent.name`, `skill.name`, `plugin.name`, `mcp_server.name`, `mcp_tool.name`.
- The `api_request` event carries `cost_usd`, the cache token counts, `request_id` and `duration_ms`.
- It can be enabled fleet-wide through managed settings.

**What is wrong.** OTel is not "the only per-user path on cloud providers". The costs page lists three per-user options for Bedrock, Vertex and Foundry: OpenTelemetry, a Claude apps gateway, and an LLM gateway such as LiteLLM. What the page actually says is narrower: OTel "is the only option that streams per-user token and cost metrics into your own observability stack in near real time".

### verified-savings-gap-rtk
**What is right.** The JetBrains post (Denis Shiryaev, July 2026) matches on:
- 86 SkillsBench tasks, claude-sonnet-5, Claude Code 2.1.201.
- 425 billed trials, about $320.
- A median of +7.6% per task at low effort (p=0.004) across 80 clean pairs, with +13.8% turns (p=0.03).
- +0.1% at high effort (p=0.99).
- 96.2M tokens "saved" while the bill went up.
- Cached re-reads bill at a tenth of the price.

**What is wrong.**
- "≈3% of input tokens" is JetBrains' computed *ceiling* on what RTK could possibly save. It is not the share of tokens RTK touched. The post says the hook "only ever sees about a fifth of the tool output".
- "Nobody sells savings reconciled to the invoice" is the report's own market inference. The source does not say it.

### oss-token-minimizer-ecosystem
**What is wrong.** RTK has about **81.6k stars** (GitHub API: 81,564; Apache-2.0), not about 39.5k. RTK's README now also warns that cutting bash output "is not the same as cutting your bill".

**What is right.**
- **token-optimizer:** 2,361 stars. Licensed PolyForm Noncommercial 1.0.0 with a small-business exception (fewer than 5 people and under $20k/month revenue), so it still blocks large enterprises. It audits CLAUDE.md, MCP servers, skills, re-reads, loops and cache expiry. It self-reports "$1,396 in API-equivalent value" over 30 days, of which $1,124 is estimated repeat reads avoided.
- **claude-code-tips:** 78 stars. Claims "~30min → 3h+ sessions".

### langfuse-clickhouse
**What is right.**
- The ClickHouse acquisition on 2026-01-16. Langfuse stays MIT and self-hostable.
- Ingested cost takes priority over inferred cost. Cache usage types and context-dependent tiers (>200K tokens) are supported.
- Pricing is $0 / $29 / $199 / $2,499 a month.
- Monitors (2026-06-19) alert on cost per trace, eval scores and p95 latency through Slack, webhooks or GitHub Actions, on Cloud and self-hosted v4.

**What is wrong.**
- The **">2,000 paying customers"** figure is not in the ClickHouse blog, the Langfuse blog or the SiliconANGLE article. The ClickHouse blog says "19 of the Fortune 50 and 63 of the Fortune 500".
- The **2025-10-10 "spend alerts"** are email alerts on the customer's own Langfuse Cloud bill (set in the Billing tab). They are not LLM spend alerts. LLM-cost alerting comes from Monitors.

### helicone-maintenance-mode
**What is right.**
- Helicone joined Mintlify on 2026-03-03.
- 16,000 organizations and 14.2 trillion tokens.
- Cost comes from the Model Registry v2, or a repo of 300+ model prices. Sessions and properties.
- Caching is exact-match response caching.
- Alerts at 50/80/95%. Weekly reports.
- Apache-2.0, 6,175 stars.

**What is wrong.**
- Maintenance mode still ships "security updates, new models, bug & performance fixes", not only security fixes and new models.
- Services "remain live for the foreseeable future". No shutdown has been announced, so "leaving 16,000 orgs to migrate" overstates the source.

### portkey-panw-gateway-security
**What is right.**
- 2026-03-24 open-sourcing, with 24,000+ organizations, $180M+ in spend and more than 1T tokens a day.
- Budget limits have a $1 cost minimum and a 100-token minimum, weekly or monthly resets, and key expiry on breach. The docs say they are "Enterprise Plan" plus select Pro users.
- Production is $49 a month.
- Palo Alto Networks completed the acquisition on 2026-05-29. The pricing banner reads "Portkey is now PRISMA AIRS AI Gateway".

**What is wrong.** Semantic caching is **not** Enterprise-only. The pricing page lists "Simple & Semantic Caching" on the $49/month Production plan. "Granular Budget & Rate Limits" is what is listed under Enterprise.

### litellm-spend-and-supply-chain
**What is right.**
- Spend is tracked by key, user, team, customer, tag and model, with `/spend/logs/v2`.
- Custom spend-log metadata and `/global/spend/report` are Enterprise features.
- Tag budgets use `max_budget` and `budget_duration`, need Postgres, and are not marked Enterprise.
- The Claude Code costs page names LiteLLM and says it has "not been audited for security".
- The incident: versions 1.82.7 and 1.82.8 on 2026-03-24, live about 40 minutes from 10:39 UTC. The stealer targeted credentials, and 1.82.8 added a `.pth` file.
- The fix: CI/CD v2 from v1.83.0, and cosign-signed GHCR images from v1.83.0-nightly.

**What is wrong.** When a tag budget is exceeded, the error has `"code": "400"` (`budget_exceeded`), not 429.

### cloudflare-free-spend-limits
**What is right.**
- The blog was published 2026-06-05.
- Spend limits are in open beta "for all AI Gateway users across all plans".
- They can be scoped by model, provider or custom attributes.
- Windows are fixed or rolling.
- Identity-driven budgets are in closed beta.

**What is wrong.**
- The blog does not literally say "at no cost". It says limits are on all plans and that "it's free to get started with AI Gateway".
- On breach, the gateway blocks by default or routes "to a fallback model" through Dynamic Routes. The fallback is not necessarily a cheaper model.
- The 20-rules-per-gateway limit comes from developers.cloudflare.com, not from the blog.

### finops-platforms-export-target
**What is right.**
- Anthropic's partner list: CloudZero, Datadog, Grafana Cloud, Harness, Honeycomb, Vantage.
- Datadog CCM (2025-08-18) normalizes to FOCUS and mentions an "abnormally low cache hit ratio" alert.
- Vantage (2026-07-02) ingests per-user Enterprise cost.
- CloudZero offers unit costs and anomaly detection.
- nOps lists "model substitution, cache tuning".

**What is wrong.**
- The **−86.9%** example (GPT-4o $112,456 to Claude 3.5 Haiku $14,721) is **not** on the cited page https://www.nops.io/ai-cost-visibility-and-optimization/. It is on https://www.nops.io/blog/now-supporting-bedrock-claude/, and it is a *projected* saving.
- Vantage charges "no additional fee" for the integration, but Anthropic costs count toward Vantage's pricing-tier quota.

### coding-agent-dashboards-commodity
**What is right.**
- The CloudWatch announcement (2026-07-20) covers Claude Code through the apps gateway, Codex and Copilot.
- It uses standard OTel metric ingestion pricing.
- It shows commit throughput, PR velocity, cost-to-output, and cost attribution by organization, team and user.

**What is wrong.**
- Neither the AWS announcement nor the linked docs (coding-agents-insights.html) mention a **cache-hit-rate** metric. The claim that CloudWatch shows one is unsupported.
- Dash0 is "$10 per monitored developer per month", but the article is dated 2026-08-19.
- Grafana, Honeycomb and SigNoz are listed. SigNoz's guide covers "cache behavior". Datadog has a cache-hit-ratio alert. So "cache hit rate is a standard tile" is only partly supported.

### routers-vs-cache-net-savings
**What is right.**
- Not Diamond's homepage claims "20%+" cost savings. Its calculator shows 1,000 engineers at $300 a month saving $1.2M a year.
- The AWS blog (2024-12-04) says Bedrock prompt routing can cut costs "by up to 30 percent", routing within one model family.
- Martian now presents itself as an interpretability and measurement research lab, with no router mentioned.
- `model_changed` means the cache is per-model.

**What is wrong.** The Not Diamond **"30%+ for its coding router"** figure does not appear on the homepage, the pricing page or the docs landing page.

### copilot-cursor-token-metering
**What is right.**
- The GitHub blog was posted 2026-04-27, effective 2026-06-01, for all plans.
- AI Credits are consumed by input, output and cached tokens.
- Budgets can be set at enterprise, cost-center and user levels.
- Business is $19 and Enterprise $39, each with matching credits.
- Cursor's 2026-05-04 release:
  - Alerts at 50, 80 and 100%.
  - Per-user filtering and product-surface analytics.
  - Model and provider allow and block lists.

**What is wrong.**
- The blog denominates credits in dollars ("$19 in monthly AI Credits") but never states **"1 credit = $0.01"**.
- Cursor added *soft* limits "instead of hard limits". Hard limits already existed.

### projectdiscovery-cache-case
**What is right.** The ProjectDiscovery post (2026-04-10) matches on:
- Hit rate from 7% to 84%.
- −59% overall, −66% after optimization, −70% over the last 10 days.
- 9.8B tokens served from cache.
- Three breakpoints: BP1 system at 1h, BP3 tools at 1h, BP2 sliding window at 5m.
- Moving dynamic content into a trailing `<system-reminder>`.
- Frozen, date-only datetime and provider routing for cache locality.

The arXiv paper 2601.06007 checks out: "Don't Break the Cache" by Lumer et al., v1 2026-01-09, 41–80% cost, 13–31% TTFT, 500+ sessions.

**What is wrong.**
- The 59–70% figures are savings "compared to what the same token volume would have cost at full input rates". That is a no-cache counterfactual, not a before/after cut in spend.
- Deriving "the effective per-token rate from real spend" was how they *measured* the savings, not how they found the problem.
- The 7%-to-84% gain came from the whole caching program. The breakpoints and moving dynamic content took the rate to 74%. Smaller fixes took it from 74% to 84%: stable template variables, frozen datetime, provider routing and part-level marking.
