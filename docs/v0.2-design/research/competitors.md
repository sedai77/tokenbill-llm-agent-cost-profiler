# Token Bill: competitive landscape and gap analysis for LLM and coding-agent cost tools

Research track: **competitors**. Date: **2026-09-23**. Subject: Token Bill v0.1.2 (github.com/sedai77/tokenbill-llm-agent-cost-profiler, PyPI `tokenbill`, MIT).

Method: I opened every source cited below on 2026-09-23 with WebFetch/WebSearch, plus Anthropic's official `claude-api` skill docs bundled on this machine (v2.1.280). Where a page shows no publication date, the entry says "accessed 2026-09-23". A number given without a primary source is marked *secondary* or *vendor claim*. Evidence strength follows the rubric: **strong** means a primary doc or first-party data, **moderate** means a vendor page or a reputable secondary source, **weak** means a single secondary source or an unclear claim.

---

## 0. Summary

1. **Anthropic now covers the basic "why did my cache miss" question, but only one conversation at a time.** Three first-party features shipped in 2026:
   - The Messages API's **cache diagnostics** beta (`cache-diagnosis-2026-04-07`). It names the first divergence (model, system, tools or messages) and estimates `cache_missed_input_tokens`.
   - Claude Code's `/usage` **"Prompt cache (main)" line**. It shows hit share, miss count and a "likely cause", and requires v2.1.260 or later.
   - Claude Code's **plan-usage attribution** to skills, subagents, plugins and MCP servers.

   None of the three aggregates across a fleet, prices a fix in dollars, checks the saving against the invoice, or works on Bedrock/Vertex/Foundry (diagnostics is first-party API only). Token Bill's README ("Related work") still says provider tooling is "silent on *why*". That is no longer true, and an enterprise reviewer will notice.
2. **Tracking, attribution, budgets and hit-rate dashboards are commodities, and often free.** Examples: Anthropic Admin/Analytics APIs, Claude apps gateway spend limits, Cloudflare AI Gateway spend limits (free beta, June 2026), LiteLLM tag budgets (OSS), Langfuse (MIT), CloudWatch Coding Agent Insights, Datadog. Token Bill should **integrate with these, not rebuild them**.
3. **Consolidation has left few neutral vendors.**
   - Langfuse went to ClickHouse (2026-01-16).
   - Helicone went to Mintlify and is in maintenance mode (2026-03-03).
   - Traceloop went to ServiceNow (Mar 2026).
   - Portkey went to Palo Alto Networks (closed 2026-05-29) and is now "Prisma AIRS AI Gateway".

   Enterprise buyers are left with platform-owned tools. A **vendor-neutral, local-first, zero-dependency** analysis layer is a real opening.
4. **The closest enterprise competitor to Token Bill's "cause, fix, dollars" idea is Datadog Agent Console** (Preview, June 2026). It attributes spend by agent, team and user, detects waste patterns (retry loops, file re-reads, skipped checks), and offers a **Fix Library** of PreToolUse hooks that can be deployed through a PR. Datadog also published its own savings: **>$1M/month**, mostly from changing default policy (Opus to Sonnet: $687k/month; effort high to medium: $288k/month), plus Headroom context compression and automated nudges.
5. **Savings claims are not credible, and that is Token Bill's biggest opening.**
   - JetBrains' paired A/B (425 billed trials) found that RTK, which advertises 60–90% savings, made Claude Code runs **7.6% *more* expensive** at low effort (p=0.004) and made no difference at high effort.
   - RTK's own counter reported 96.2M tokens "saved" while the bill went up.
   - Nobody in the market sells **verified savings that reconcile to the invoice**.
6. **The large levers for a fleet of thousands of developers are fleet policy, not single prompts.** They include default model, default effort, prompt-cache TTL (`promptCacheTtl`), gateway configuration, and MCP tool loading, which a custom `ANTHROPIC_BASE_URL` silently disables. Claude Code exposes all of these as **managed settings**. No tool simulates "what would our bill have been under policy X" from real fleet telemetry and emits the managed-settings patch.
7. **Recommended positioning: "the verification and root-cause layer for AI spend."** Every recoverable dollar comes with a cause, a deployable fix (a code diff, hook or managed setting), and a before/after verification against billed usage. It reads from whatever the enterprise already runs (Claude Code OTel, Admin/Analytics APIs, gateways, Langfuse and others) and writes to their FinOps and observability stack (FOCUS-style rows, OTLP metrics).
8. **Enterprise basics Token Bill still needs:**
   - Ingestion at fleet scale (OTel, Admin API, session logs).
   - Attribution by user, team, repo, agent, skill and MCP server.
   - Contracted-rate pricing and invoice reconciliation.
   - Multi-provider support (OpenAI, Bedrock/Vertex, OpenRouter, Copilot/Cursor exports).
   - Alerting on waste regressions.
   - Supply-chain hygiene (signed builds, SBOM). The LiteLLM PyPI compromise of 2026-03-24 makes this a real buying criterion.
   - Privacy modes (hash-only fingerprints).
   - Exports to FinOps and observability tools.

---

## 1. Market shape as of 2026-09-23

### 1.1 Consolidation: observability and gateway vendors were acquired

| Company | Acquirer | Date | What happened after | Source |
|---|---|---|---|---|
| Langfuse (OSS LLM observability, MIT) | ClickHouse | 2026-01-16 | Stays MIT, self-hostable, roadmap unchanged; >2,000 paying customers, 19 of the Fortune 50 | https://langfuse.com/blog/joining-clickhouse ; https://siliconangle.com/2026/01/16/database-maker-clickhouse-raises-400m-acquires-ai-observability-startup-langfuse/ (2026-01-16) |
| Helicone (OSS gateway + observability, Apache-2.0) | Mintlify | 2026-03-03 | **Maintenance mode** (security fixes, new models); 16,000 orgs, 14.2T tokens | https://www.helicone.ai/blog/joining-mintlify (2026-03-03) |
| Traceloop (OpenLLMetry) | ServiceNow | Mar 2026, est. $60–80M | Folded into ServiceNow AI Control Tower; the article says nothing about OpenLLMetry's future | https://www.calcalistech.com/ctechnews/article/sjghwiqf11e (Mar 2026) |
| Portkey (AI gateway) | Palo Alto Networks | completed 2026-05-29 (per PANW's "completes acquisition" release; the announcement date was not confirmed) | Becomes the core gateway of Prisma AIRS; pricing page now reads "Portkey is now PRISMA AIRS AI Gateway" | https://www.paloaltonetworks.com/company/press/2026/palo-alto-networks-completes-acquisition-of-portkey-to-secure-ai-agents ; https://portkey.ai/pricing (accessed 2026-09-23) |

### 1.2 First-party encroachment by Anthropic

Anthropic ships cost telemetry, spend limits, cache diagnostics and cache-miss "likely cause" in its own products. See F1 through F6.

### 1.3 Commoditization

Budgets, alerts, per-user attribution and cache-hit-rate tiles are free or bundled at every layer:
- Provider: OpenAI org/project hard limits; Anthropic workspace, Enterprise and apps-gateway spend limits.
- Gateway: Cloudflare, LiteLLM, Bifrost, Portkey.
- Observability: Langfuse monitors, Datadog, CloudWatch, Grafana.
- Agent vendor: Cursor soft/hard limits; GitHub Copilot cost-center budgets.

### 1.4 Credibility gap in savings claims

Tools report their own counterfactual savings, and independent paired tests contradict them (F9). FinOps practitioners now own AI spend: 98% manage it, up from 31% two years earlier (F26). They will ask for invoice-reconciled numbers.

---

## 2. Competitor matrix

Legend: ● full, ◐ partial or limited, ○ none or not documented. "Cache analysis" means explaining *why* prompt-cache reads were lost, not just showing cache token counts.

| Product | Category | Cost tracking | Attribution | Budgets / enforcement | Cache analysis (root cause) | Recommendations | Automated optimization | OSS / license | Deployment | Pricing (as documented) |
|---|---|---|---|---|---|---|---|---|---|---|
| Anthropic Console + Usage & Cost Admin API | Provider | ● billed (1m/1h/1d usage; daily cost) | ◐ workspace, API key, model, tier, context window, geo, speed | ● workspace spend limits | ○ token splits only (5m/1h writes, reads) | ○ | ○ | proprietary | SaaS | free |
| Claude Code Analytics API / Enterprise Analytics API | Provider | ● per user per day (estimated cost by model) | ● user; product (Enterprise) | ● Enterprise spend limits: org, group, member | ○ | ○ | ○ | proprietary | SaaS | free |
| **Anthropic cache diagnostics (beta)** | Provider API | – | – | – | ● **first divergence type + `cache_missed_input_tokens`** per request; first-party API only | ◐ fix text in docs | ○ | proprietary | API | free |
| **Claude Code `/usage` + OTel + managed settings** | Agent | ● per session; OTel per request | ● user, skill, subagent, plugin, MCP server, effort, speed | ◐ `--max-budget-usd` | ◐ **per-session miss count + "likely cause"**, main conversation only | ◐ behavior flags ≥10% of usage | ○ | proprietary | local | included |
| Claude apps gateway | Agent gateway | ● USD-estimate meter | ● OIDC user, IdP group | ● daily/weekly/monthly caps (429) | ○ (1h TTL not available through it) | ○ | ○ | proprietary (ships in `claude`) | self-hosted | included |
| Datadog LLM Obs + CCM (Anthropic) + **Agent Console** | Observability | ● estimated + actual (CCM, FOCUS-normalized) | ● tags, team, user | ● monitors/alerts | ◐ "abnormally low cache hit ratio" monitor templates (blog) | ● **Agent Console waste detection + Fix Library** (Preview) | ◐ hooks deployed via PR | proprietary | SaaS | usage-based; per *secondary* source, from ~$240/mo per 100K spans |
| AWS CloudWatch Coding Agent Insights | Observability | ● tokens, cost | ● user, team | ○ | ◐ cache-hit-rate tile | ○ | ○ | proprietary | AWS | OTel metric ingestion pricing |
| Dash0 AI Coding Insights | Observability | ● | ● | ○ | ○ documented | ◐ | ○ | proprietary | SaaS | $10/monitored developer/month |
| Langfuse | LLM observability | ● ingested or inferred, cache types, tiered pricing | ● user, tag, model, env | ◐ spend alerts + Monitors (Slack/webhook/GitHub Actions) | ○ | ○ | ○ | **MIT** | self-host or cloud | $0 / $29 / $199 / $2,499 per month |
| LangSmith | LLM observability | ● incl. `cache_read`/`cache_creation` | ● | ◐ | ○ | ○ | ○ | proprietary | cloud; self-host on Enterprise | Plus $39/seat/mo |
| Braintrust | Evals + observability | ● per span | ● | ◐ | ○ | ◐ "Loop" assistant (prompt/scorer suggestions) | ○ | proprietary | cloud; on-prem on Enterprise | $0 / $249/mo / custom |
| W&B Weave | Observability | ◐ Python only | ◐ | ○ | ○ | ○ | ○ | Apache-2.0 SDK | cloud / self-host | vendor pricing |
| Arize Phoenix / AX | Observability | ● incl. cache_read/write | ● trace, session, project | ○ | ○ | ○ | ○ | **ELv2** (not OSI) | self-host / cloud | free OSS; AX paid |
| New Relic AI Monitoring | APM | ◐ token counts | ◐ | ● (APM alerts) | ○ documented | ◐ model compare | ○ | proprietary | SaaS | usage-based |
| Helicone | Gateway + observability | ● (300+ model price repo) | ● sessions, properties | ◐ cost alerts at 50/80/95% | ○ (exact-match *response* cache only) | ◐ weekly report | ◐ cheapest-provider routing | Apache-2.0 | self-host / cloud | **maintenance mode** |
| Portkey → Prisma AIRS AI Gateway | Gateway | ● | ● | ● cost/token budgets (Enterprise) | ○ | ○ | ◐ semantic cache (Enterprise) | gateway OSS (Mar 2026) | self-host / SaaS | $49/mo Production; Enterprise custom |
| LiteLLM proxy | Gateway | ● spend logs | ● key, user, team, customer, tag | ● budgets incl. tag budgets (429) | ○ | ○ | ◐ routing | OSS core + Enterprise | self-host | OSS free; Enterprise license |
| Cloudflare AI Gateway | Gateway | ● | ● metadata dims | ● **spend limits (free, open beta)** | ○ | ○ | ◐ fallback to cheaper model | proprietary | SaaS | free beta |
| Bifrost (Maxim) | Gateway | ● | ● virtual keys, teams, customers | ● hierarchical budgets | ○ | ○ | ◐ semantic cache | Apache-2.0 (enterprise extras) | self-host | OSS; enterprise custom |
| OpenRouter | Router / gateway | ● per response (`cost`, `cache_discount`) | ◐ key | ◐ | ○ | ○ | ◐ sticky routing to keep cache | proprietary | SaaS | margin on credits |
| CloudZero / Vantage / Finout / nOps / Harness | FinOps | ● from Admin/Analytics APIs, CUR | ● virtual tags, unit costs | ● budgets, anomalies | ○ (nOps lists "cache tuning") | ◐ model substitution (nOps) | ○ | proprietary | SaaS | platform pricing |
| Not Diamond / Bedrock prompt routing | Routers | – | – | – | ○ (routing *causes* `model_changed` misses) | ● route per prompt | ● | proprietary | SaaS / AWS | per-token fee / model cost |
| ccusage, Claude-Code-Usage-Monitor | Local OSS | ● from local logs | ◐ session, day, 5-hour block | ◐ burn-rate forecast | ○ | ○ | ○ | MIT | local | free |
| Headroom, RTK, token-optimizer | Local OSS optimizers | ◐ self-reported | ○ | ○ | ◐ Headroom "CacheAligner" flags volatile content | ◐ | ● compression / rewriting | Apache-2.0 / OSS / **PolyForm-NC** | local proxy/wrap | free |
| promptcachelint | Local OSS | ◐ | ○ | ○ | ● prefix diff, CL001–CL014 codes, CI `--fail-on` | ◐ | ○ | MIT (0 stars) | local | free |
| **Token Bill v0.1.2** | Local OSS analyzer | ● from billed usage | ○ run only | ○ | ● replay + 5 breakers + $ per fix | ● fix text | ○ | MIT, stdlib-only | local | free |

---

## 3. Findings

Each finding lists what exists, the numbers, the sources, and what Token Bill should do about it.

### F1. Anthropic cache diagnostics (beta) localizes the cache break server-side, per request [strong]

**What exists**
- Send header `cache-diagnosis-2026-04-07` on every request.
- Pass the previous response `id` as `diagnostics.previous_message_id`.
- The response's `diagnostics.cache_miss_reason.type` is one of: `model_changed`, `system_changed`, `tools_changed`, `messages_changed`, `previous_message_not_found`, `unavailable`.
- The `*_changed` types carry `cache_missed_input_tokens`. This is a byte-derived magnitude estimate, "not a billing number".

**Limitations**
- Reports only the earliest divergence.
- First-party Claude API only: not Bedrock, Vertex, Foundry or Claude Platform on AWS.
- Fingerprints are kept for a short time and scoped to one workspace.
- Returns `unavailable` when `tool_choice`, `thinking`, `context_management`, output format or the beta-header set differ, and on very long conversations.
- ZDR-eligible: stores hashes and token-count estimates only.

**Sources:** https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics (accessed 2026-09-23; beta header dated 2026-04-07); bundled `claude-api/shared/prompt-caching.md` and `platform-availability.md` (v2.1.280).

**Implication for Token Bill.** This commoditizes the *single-request* part of breaker detection. Token Bill should:
- (a) accept `diagnostics` in the trace schema and prefer it over the char heuristic when present;
- (b) roll `cache_miss_reason` up across thousands of sessions and **price it in dollars** with real rates;
- (c) cover the gaps diagnostics leaves: other clouds, `unavailable` cases, second and later divergences, TTL expiry versus change, and dollar impact after fixing;
- (d) add an "enable diagnostics fleet-wide" recommendation, with a snippet for the SDK wrapper and recorder.

### F2. Claude Code already reports per-session cache misses with a likely cause, and attributes plan usage [strong]

**`/usage` "Prompt cache (main)" line**
- Shows requests, % of input from cache, misses, expected rebuilds, and warm/cold state with the TTL in effect.
- Counts a request as a miss when it re-processed more than 5% and at least 2,000 tokens of cacheable content.
- Names a likely cause (e.g. "tool definitions changed") from v2.1.260.
- Covers **the main conversation only, not subagents**, and one machine.

**Plan-usage breakdown**
- Attributes usage to skills, subagents, plugins and individual MCP servers.
- Flags behaviors (long context, cache misses) that account for ≥10% of recent usage.
- Lists the heaviest `/loop` and scheduled tasks.

**Admin controls**
- The `modelPricing` managed setting makes the displayed cost use contracted rates (multiplier and/or per-model overrides; v2.1.242+).
- Published fleet baseline: about **$13 per developer per active day, $150–250 per developer per month**, and below $30/active day for 90% of users.

**Source:** https://code.claude.com/docs/en/costs (accessed 2026-09-23).

**Implication for Token Bill.** The per-session view is taken. Token Bill has to be the **fleet view**: all users, all subagents, dollars, fixes and trend. It should also support **`modelPricing`-compatible contracted rates** so its dollars match what developers see. For an enterprise with thousands of developers, the published baseline gives the sizing arithmetic: 2,000 developers × $150–250/month ≈ $3.6M–6.0M/year (illustrative, from Anthropic's averages).

### F3. Claude Code's OpenTelemetry export is the enterprise data pipe, and Token Bill does not read it yet [strong]

**Metrics**
- `claude_code.cost.usage` (USD) and `claude_code.token.usage` (with `type` = input, output, cacheRead, cacheCreation).
- Attributes: `user.id`, `user.email`, `organization.id`, `session.id`, `model`, `query_source` (main/subagent/auxiliary), `effort`, `speed`, `agent.name`, `skill.name`, `plugin.name`, `mcp_server.name`, `mcp_tool.name`.

**Events**
- `claude_code.api_request` carries `cost_usd`, the four token counts, `request_id`, `duration_ms` and `model`.
- `OTEL_LOG_RAW_API_BODIES=1` exports full response bodies.

**Deployment**
- Admins enable it fleet-wide through managed settings. OTel is also the *only* per-user path for Bedrock, Vertex and Foundry.

**Sources:** https://code.claude.com/docs/en/monitoring-usage ; https://code.claude.com/docs/en/costs (accessed 2026-09-23).

**Implication for Token Bill.** Build an **OTLP importer**, reading an OTLP/JSON file export or acting as a tiny collector sink. It gives per-request cache splits, timing (for TTL-expiry inference), and skill/MCP/subagent attribution with no code changes in developer repos. Breaker localization would use body export where privacy policy allows, and hashed fingerprints where it does not.

### F4. Anthropic's Admin Usage & Cost API, Claude Code Analytics API and Enterprise Analytics API give billed truth for reconciliation [strong]

**Usage report** (`/v1/organizations/usage_report/messages`)
- `1m`, `1h` and `1d` buckets.
- Filter and group by API key, workspace, model, service tier, context window, `inference_geo`, and speed (beta).
- Splits uncached, cache read and cache creation, with `ephemeral_5m` and `ephemeral_1h`.
- Data appears in about 5 minutes; polling once per minute is supported.

**Cost report**
- Daily only, USD in cents.
- **Priority Tier costs are excluded.**

**Claude Code Analytics API**
- Per user per day: sessions, lines, commits, PRs, tool accept/reject, and tokens and estimated cost by model.
- About 1-hour delay.
- **Excludes Bedrock, Vertex, Foundry and Claude Platform on AWS.**

**Enterprise Analytics API**
- Per-user cost across chat, Claude Code and Cowork.
- Usually 4 hours fresh, up to 24 hours; values can be revised for 30 days.
- Data from 2026-01-01.

**Sources:** https://platform.claude.com/docs/en/manage-claude/usage-cost-api ; https://platform.claude.com/docs/en/build-with-claude/claude-code-analytics-api ; https://platform.claude.com/docs/en/manage-claude/analytics-api (all accessed 2026-09-23).

**Implication for Token Bill.** Add a read-only **"org scan" mode** that pulls these APIs with an admin or analytics key and needs no traces. It would compute:
- fleet cache-read share by workspace, key and user;
- cache-write-to-read ratios (the thrash signature);
- 5m versus 1h write mix;
- batch-eligible share;
- a **reconciliation check** of Token Bill's modeled dollars against the cost report.

This is how Token Bill gets to "first value in 10 minutes" for an enterprise admin.

### F5. Anthropic's measured cost levers give Token Bill published benchmarks to score teams against [strong]

The live "optimizing for cost and intelligence" page reports the following.

**Caching**
- Caching cuts agent-loop cost **2.7×–5.3×** at 79–90% hit rates.
- **Median real traffic reads 84% of input from cache; the top 10% of harnesses reach 94%+.**
- The 1-hour TTL pays off when more than 1 in 20 turns follows a 5-minute to 1-hour pause.

**Batch**
- 50% off.

**Input trimming**
- Tool search with 500 tools: −45%.
- GitHub MCP deferral: −20%.
- Pruning stale results on long runs: −39%. Context editing on short loops cost 74% *more*.

**Prompt audit and task budgets**
- Auditing prompts against the current model: −14%.
- Task budgets: −44–58% at a cost of 3–6 points.

**Effort**
- `medium` effort on SWE-bench Pro: −50% for 2 points.
- Re-running failures at higher effort: same 91.7% pass rate at about half the cost.

Note that the bundled skill doc (v2.1.280) cites an older range, "2.5 to 3.7 at 81% to 90%". Use the live page.

**Sources:** https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence.md (accessed 2026-09-23); bundled `claude-api/shared/cost-optimization.md`.

**Implication for Token Bill.**
- Show each team's cache-read share **against Anthropic's published distribution** (median 84%, p90 94%). This is a useful executive number.
- Build a **lever catalog** with the published expected ranges, and mark each recommendation as "measured by Anthropic on benchmark X" versus "measured on your traffic".
- Anthropic also ships an agentic `/claude-api cost-optimize` workflow in the bundled skill: scope, baseline, ranked levers, eval hill-climb. That is a first-party "AI consultant" competitor at the *code* level. Token Bill's edge is fleet-level evidence and verification.

### F6. Claude Code's cache-invalidation rules add enterprise-only breakers Token Bill does not model [strong]

The Claude Code prompt-caching doc lists these invalidators:
- **Model switch**, including `opusplan` toggles, automatic safety fallback, and a skill whose frontmatter names another `model`.
- **Effort change** on most models. The cache is kept on Opus 5.5 and Fable 5.1 first-party, but *not* on Bedrock, Vertex or the apps gateway.
- **Fast-mode header**: one-time miss.
- **MCP connect/disconnect** when tools are *loaded into the prefix*. This happens when tool search is unavailable, **including behind a custom `ANTHROPIC_BASE_URL` gateway**.
- **Plugin MCP servers**, bare-tool deny rules, **Claude Code upgrades**, compaction, and **image eviction** batches.

Two further traps:
- A **gateway that strips `cache_control` while returning success** makes "your entire conversation history bill as uncached input on every turn".
- TTL defaults to **5 minutes for API-key and cloud users**. It can be set org-wide with the `promptCacheTtl` / `subagentPromptCacheTtl` managed settings. The cache is scoped per machine and directory, and worktrees do not share it.

**Source:** https://code.claude.com/docs/en/prompt-caching (accessed 2026-09-23).

**Implication for Token Bill.** Add a **Claude Code breaker pack** to go with the 5 generic breakers:
- `gateway-strips-cache-control`: fleet-wide zero reads behind one base URL.
- `tool-search-disabled-by-gateway`: MCP schemas in the prefix and churn.
- `opusplan-thrash`, `effort-toggle`, `fast-mode-late-enable`.
- `idle-ttl-expiry`, with a 5m-versus-1h what-if per user.
- `upgrade-rebuild`, `image-eviction`, `worktree-scope`.

Each should come with a **managed-settings fix** (for example `promptCacheTtl: "1h"` for team X; forward the `anthropic-beta` header at the gateway) and a dollar estimate. This is fleet configuration auditing that per-session `/usage` cannot do.

### F7. The Claude apps gateway gives per-user spend caps, and routing through it costs cache features [strong]

**What it is**
- Self-hosted; ships inside the `claude` binary (`claude gateway`).
- Uses OIDC SSO and per-IdP-group model access.
- Relays OTLP telemetry and needs Postgres.

**Spend limits**
- `daily`, `weekly` and `monthly` caps per user, RBAC group or org, returning 429 when exceeded.
- Pricing overrides and multiplier; unknown models charged at $5/$25.
- Aborted streams are billed at a floor estimate.
- Fail-open by default.

**Cache features lost behind it**
- **The 1-hour cache TTL is not available through the gateway.**
- First-party-only optimizations (global cache scope, token-efficient tools) are not enabled.

**Sources:** https://code.claude.com/docs/en/claude-apps-gateway ; https://code.claude.com/docs/en/claude-apps-gateway-spend-limits (accessed 2026-09-23).

**Implication for Token Bill.**
- Do **not** build spend enforcement for Claude Code; Anthropic ships it.
- Do quantify the **routing tax**: what the fleet pays for losing the 1h TTL and global cache scope when routed through the gateway, Bedrock or Vertex, versus first-party. No one else computes this.
- Ingest the gateway's OTLP relay.

### F8. Datadog Agent Console is the closest enterprise competitor, and Datadog's own savings case shows where the money is [strong]

**Agent Console** (Preview, blog 2026-06-09)
- Spend across coding agents (Claude Code, Cursor, Copilot and others) by agent, team and user.
- Compares spend with lines, commits and PRs, plus DORA impact metrics.
- **Detects waste patterns (skipped checks, retry loops, file re-reads) and offers a "Fix Library" including PreToolUse hooks deployable org-wide via PR.**

**Datadog's internal program** (blog 2026-08-26): more than **$1M/month** saved.

| Change | Saving | Notes |
|---|---|---|
| Default model Opus 4.8 → Sonnet 4.6 | **$687k/month** | 36.7% cost cut for an **8% proficiency loss**, measured with 140+ internal evals |
| Claude Code default effort high → medium | **$288k/month** | |
| Headroom tool-output compression | −47% in eval | Pilot A/B: **−27% cost per user**, −39.3% input and −35.7% output tokens |
| Automated guardrail nudges (Cloud Cost monitors → Slack DM) | **$150k in the first week** | 768 users |

**Sources:** https://www.datadoghq.com/blog/claude-code-monitoring/ (2026-06-09); https://www.datadoghq.com/blog/how-datadog-saves-money-by-optimizing-ai-usage/ (2026-08-26).

**Implication for Token Bill.** The biggest dollars are in **default policy** (model and effort) and **behavior nudges**, not individual prompt fixes. Token Bill should add:
- (a) a **policy what-if simulator** that reprices real fleet usage under alternative defaults;
- (b) a quality gate hook, so savings are always paired with an eval result;
- (c) a **fix library as code** (hooks and managed-settings patches);
- (d) nudge digests per user and team.

The differentiators against Datadog: open source, local-first, no lock-in, deeper cache economics, and **verified** savings.

### F9. Independent A/B testing shows self-reported token savings can be wrong in sign [strong]

**JetBrains test** (SkillsBench, 86 tasks, claude-sonnet-5, Claude Code 2.1.201, paired A/B, 425 billed trials, about $320)
- RTK, advertised at 60–90% savings, was **+7.6% more expensive per task at low effort (p=0.004)** with +13.8% more turns.
- At high effort the difference was +0.1% (p=0.99).
- RTK could touch only about 3% of input tokens. Claude Code's `Read`/`Grep` bypass the Bash hook, and cached re-reads, which dominate input, are already billed at 0.1×.
- **RTK's scoreboard claimed 96.2M tokens saved while billed cost rose.**

**Source:** https://blog.jetbrains.com/ai/2026/07/rtk-claude-code-token-savings/ (July 2026).

**Implication for Token Bill.** This is the clearest single gap in the market. Token Bill should ship **"verify"**:
- paired or pre/post comparison of *billed* cost per task (or per session, PR or merged PR);
- bootstrap confidence intervals and turn-count drift;
- a cache-aware counterfactual;
- output "claimed vs. verified" for any optimizer (RTK, Headroom, token-optimizer, routers, model switches).

Enterprise buyers need this before rolling any optimizer out to thousands of seats.

### F10. Headroom leads open-source context compression, and it is cache-aware [moderate]

- Apache-2.0; about 73.6k GitHub stars.
- Runs as a library, proxy, `headroom wrap claude|codex|cursor…`, or MCP server.
- Claims about 20% fewer tokens for coding agents and 60–95% for JSON and logs (vendor benchmarks).
- Its **CacheAligner** flags volatile content that would bust a provider prefix cache.
- Datadog measured −27% cost per user in a pilot (F8).

**Sources:** https://github.com/headroomlabs-ai/headroom (accessed 2026-09-23); Datadog blog above.

**Implication for Token Bill.** Don't rebuild compression. **Recommend and verify** Headroom as a remediation, and detect when compression *breaks* caching (rewriting earlier tool results causes `messages_changed`). A "compression × cache" interaction analysis is unique to Token Bill's replay engine.

### F11. The Claude Code "token-minimizing" open-source ecosystem is large, noisy and sometimes carries restrictive licenses [moderate]

- **RTK:** about 39.5k stars; Bash output compression.
- **token-optimizer:** 2.4k stars. Licensed **PolyForm Noncommercial**, which blocks enterprise use. Finds "ghost tokens" in CLAUDE.md, MCP schemas and skills, re-reads, loops and cache expiry, and self-reports "~$1,396 API-equivalent value" saved in 30 days.
- **Other stacks:** context-mode, caveman, codebase-memory-mcp. One bundle claims 30-minute sessions become 3-hour-plus.

**Sources:** https://github.com/rtk-ai/rtk ; https://github.com/alexgreensh/token-optimizer ; https://github.com/sgaabdu4/claude-code-tips (accessed 2026-09-23).

**Implication for Token Bill.**
- The MIT license and zero dependencies are selling points to state explicitly.
- Token Bill can be the **neutral referee**: static audit of CLAUDE.md, MCP and skill size (costed at the *cached* rate, not the list rate) plus verified before/after for each tool.

### F12. ccusage and other local trackers are the de facto developer tools: they tell you *what*, not *why* [moderate]

**ccusage**
- About 18.7k stars, MIT.
- Reads local logs from 18 agent CLIs, including Claude Code, Codex, Copilot CLI, Gemini CLI and OpenCode.
- Daily, weekly, monthly, session and 5-hour-block reports, JSON output, offline pricing.
- Tracks cache create/read separately, but does **no cache-breakage analysis**.

**Claude-Code-Usage-Monitor**
- 8.7k stars, MIT.
- Burn-rate forecasts and P90 plan limits.

**Terse comparison** (updated 2026-08-28)
- Most trackers "answer after the money is gone". Only LiteLLM and Terse enforce limits before the call.

**Sources:** https://github.com/ryoppippi/ccusage ; https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor ; https://www.terseai.org/claude-code-cost-tracker (2026-08-28).

**Implication for Token Bill.**
- Parity requirement: read **the same local session logs**. The Claude Code session-log importer is already on the roadmap and should be P0. Add Codex and Copilot CLI logs next.
- Add a `tokenbill ccusage-compatible --json` mode, so users who already run ccusage can adopt it with no friction.

### F13. promptcachelint is a direct open-source overlap with Token Bill's breaker detection, but has no traction [weak]

- MIT, Python 3.11+, zero core dependencies.
- Records requests (SDK transport wrap), prefix-diffs consecutive requests, and names the block, offset and characters.
- Detection codes CL001–CL014 cover timestamps, tool changes, parameter shifts, TTL expiry and the minimum-prefix gate.
- CI exit codes (`--fail-on`); Anthropic and OpenAI.
- 0 GitHub stars when accessed.

**Source:** https://github.com/OsmnvAslan/promptcachelint (accessed 2026-09-23).

**Implication for Token Bill.** The idea is replicable. Token Bill should quickly ship a **CI gate** (`tokenbill check --fail-on breaker`) and an **OpenAI adapter**, and win on dollars, fleet scale and verification.

### F14. Langfuse, now owned by ClickHouse, is the leading open-source trace store [strong]

**Cost tracking**
- Ingested usage and cost take priority over inferred.
- Cache usage types are supported, with "mutually exclusive buckets".
- Context-dependent pricing tiers (e.g. >200K-token rates).
- Custom model prices.

**Alerts**
- Spend alerts (2025-10-10).
- **Monitors** (2026-06-19): cost-per-trace ceilings, eval floors and p95 latency, sent to Slack, webhooks or GitHub Actions. Available on Cloud and self-hosted v4+.

**Pricing:** Hobby $0, Core $29, Pro $199, Enterprise $2,499/month. Self-host free under MIT.

**Gap:** no cache root cause, no counterfactual, no fix pricing.

**Sources:** https://langfuse.com/docs/observability/features/token-and-cost-tracking ; https://langfuse.com/pricing ; https://langfuse.com/changelog/2025-10-10-spend-alerts ; https://langfuse.com/changelog/2026-06-19-monitors ; https://clickhouse.com/blog/clickhouse-acquires-langfuse-open-source-llm-observability (2026-01-16).

**Implication for Token Bill.** Add a **Langfuse importer** (public API or ClickHouse export). That makes Token Bill the analysis layer on top of the most widely deployed OSS trace store, with no re-instrumentation needed.

### F15. Helicone is in maintenance mode, which leaves 16,000 orgs looking for a new home [strong]

- Joined Mintlify on 2026-03-03. It gets security fixes and new models, but no roadmap.
- Features: cost via Model Registry v2 (gateway) or a 300+-model open price repo; sessions and custom properties; **exact-match response caching** (its example shows "73% hit rate saving $1,247"); cost alerts at 50/80/95%; weekly email/Slack reports with optimization suggestions.
- Apache-2.0; about 6.2k stars.

**Sources:** https://www.helicone.ai/blog/joining-mintlify (2026-03-03); https://docs.helicone.ai/guides/cookbooks/cost-tracking ; https://github.com/Helicone/helicone (accessed 2026-09-23).

**Implication for Token Bill.** A **Helicone export importer** is a low-cost way to acquire users. Note that Helicone's "caching" is response caching, not provider prompt caching. Token Bill should explain the difference in its docs, because buyers confuse the two.

### F16. Portkey's gateway went fully open source and was then acquired by Palo Alto Networks [strong]

- **2026-03-24:** unified Gateway and MCP Gateway made fully open source, including governance, observability, auth and cost controls. Reported scale: 24,000+ orgs, $180M+ annualized AI spend managed, 1T+ tokens/day.
- **Budget limits:** cost (≥$1) or token (≥100) limits, weekly or monthly resets, email alerts, key expiry on breach. Documented as Enterprise (and select Pro).
- **Pricing page:** semantic caching and "granular budget & rate limits" are Enterprise; Production is $49/month.
- **2026-05-29:** Palo Alto Networks completed the acquisition. Portkey becomes the Prisma AIRS AI Gateway.

**Sources:** https://www.globenewswire.com/news-release/2026/03/24/3261574/0/en/portkey-s-gateway-is-now-fully-open-source-processing-over-1-trillion-tokens-every-day.html (2026-03-24); https://portkey.ai/docs/product/ai-gateway/virtual-keys/budget-limits ; https://portkey.ai/pricing ; Palo Alto Networks press release (2026-05-29).

**Implication for Token Bill.** Gateways are becoming security products. Token Bill should stay **gateway-agnostic** and ingest gateway logs (Portkey, LiteLLM, Cloudflare, Bifrost). It should not become a gateway, except possibly an optional thin recording proxy.

### F17. LiteLLM is the default enterprise proxy for spend by key and tag; its 2026 supply-chain compromise raises the security bar [strong]

**Spend tracking**
- Spend by key, user, team, customer, tag and model.
- `/spend/logs/v2`, `/global/spend/report`, `/user/daily/activity`.
- **Tag budgets**: `max_budget` plus `budget_duration`, 429 on breach; OSS, requires Postgres.
- Custom spend-log metadata and some reports are Enterprise-licensed.
- Anthropic's Claude Code docs name LiteLLM as the per-key spend option on cloud providers, with the caveat that it is unaudited.

**Supply-chain incident**
- On **2026-03-24**, PyPI releases **1.82.7 and 1.82.8** carried a credential-stealer (a `.pth` auto-exec in 1.82.8). They were live for about 40 minutes and downloaded tens of thousands of times.
- LiteLLM moved to a "CI/CD v2" pipeline and signed Docker images from v1.83.0.

**Sources:** https://docs.litellm.ai/docs/proxy/cost_tracking ; https://docs.litellm.ai/docs/proxy/tag_budgets ; https://docs.litellm.ai/blog/security-update-march-2026 (2026-03-24); https://code.claude.com/docs/en/costs.

**Implication for Token Bill.**
- Being pure-stdlib with zero dependencies is an **enterprise security feature**. Add Sigstore-signed releases, PyPI Trusted Publishing, an SBOM and reproducible builds, and state them in SECURITY.md.
- Add a LiteLLM spend-log importer.
- Add a detector for LiteLLM configurations that strip `cache_control` or disable tool search (F6).

### F18. Cloudflare AI Gateway made dollar spend limits free for everyone [strong]

- Spend limits launched in open beta on **2026-06-05**, at no additional cost on all plans.
- Scoped by model, provider or custom metadata (user, team, app).
- Fixed or rolling windows; block, or fall back to a cheaper model via Dynamic Routes.
- Up to 20 rules per gateway. Identity-driven budgets through Cloudflare Access are in closed beta.

**Source:** https://blog.cloudflare.com/ai-gateway-spend-limits/ (2026-06-05); https://developers.cloudflare.com/ai-gateway/features/spend-limits/.

**Implication for Token Bill.** Enforcement is free and everywhere, so it is not a differentiator. Token Bill should flag when a **fallback-to-cheaper-model rule causes `model_changed` cache loss**, and net that loss against the rule's savings.

### F19. OpenRouter exposes per-response cache economics and uses sticky routing to keep caches warm [strong]

- The `usage` object carries cost, `cached_tokens`, `cache_write_tokens`, reasoning tokens, and `cost_details.upstream_inference_cost` (BYOK only).
- The per-generation `cache_discount` can be negative on write turns.
- Sticky routing sends a conversation back to the same provider for 10 minutes of inactivity, or by `session_id`.
- Automatic-caching read multipliers vary by provider (e.g. DeepSeek 0.1×, Gemini 2.5+ 0.25×, Groq 0.5×).

**Sources:** https://openrouter.ai/docs/guides/best-practices/prompt-caching ; https://openrouter.ai/docs/use-cases/usage-accounting (accessed 2026-09-23).

**Implication for Token Bill.**
- Build an OpenRouter adapter: map the fields, and simulate provider-switch cache loss when stickiness breaks.
- Make pricing multi-provider. The current table is Anthropic-only.

### F20. General LLM observability platforms price tokens but none explains cache loss [moderate]

**LangSmith**
- Automatic or manual cost, with `cache_read` and `cache_creation` subtypes, a regex model-price map and activation dates.
- Does not retro-apply price changes.
- Plus is $39/seat/month; Enterprise can be self-hosted.

**Braintrust**
- Per-span cost; "Loop" assistant proposes prompt and scorer changes.
- $0 / $249/month / Enterprise (on-prem available); SOC 2 Type II; SAML on Pro.

**W&B Weave**
- `add_cost` with `effective_date`. **Cost tracking is Python-only; the TypeScript SDK does not support it.**

**Arize Phoenix**
- Cost with `cache_read`/`cache_write`, rolled up by trace, session and project. **ELv2**, about 11.6k stars.

**New Relic**
- Token counts and model comparison; its intro doc does not describe cache-token handling.

**Sources:** https://docs.langchain.com/langsmith/cost-tracking ; https://www.langchain.com/pricing ; https://www.braintrust.dev/pricing ; https://www.braintrust.dev/articles/best-tools-tracking-llm-costs-2026 (2026-06-21, vendor-authored) ; https://docs.wandb.ai/weave/guides/tracking/costs ; https://arize.com/docs/phoenix/tracing/how-to-tracing/cost-tracking ; https://github.com/Arize-ai/phoenix ; https://docs.newrelic.com/docs/ai-monitoring/intro-to-ai-monitoring/ (accessed 2026-09-23).

**Implication for Token Bill.** An **OTel GenAI semantic-conventions importer** plus a LangSmith importer covers most of these. They are sources of traces, not competitors on diagnosis.

### F21. FinOps platforms own allocation and chargeback, and already ingest Anthropic data [strong]

**Anthropic's partner list:** CloudZero, Datadog, Grafana Cloud, Harness, Honeycomb, Vantage.

**Datadog CCM** (2025-08-18)
- Normalizes Claude usage and cost to **FOCUS**.
- Its blog describes monitor templates for **"abnormally low cache hit ratios"**. The integration docs list no predefined monitors, so this needs checking.

**Vantage** (2026-07-02)
- Ingests Claude Enterprise daily per-user cost by product, model, region, context window and speed. The integration is free.

**CloudZero**
- Unit economics (cost per feature, workspace, model) and anomaly detection from the Admin API at 1m, 1h or 1d.

**nOps**
- Hourly Bedrock/Claude allocation from CUR, anomaly detection, and recommendations for "model substitution", **"cache tuning"** and provisioned throughput.
- Model-switch example: GPT-4o $112,456/month → Claude 3.5 Haiku on Bedrock $14,721/month (−86.9%, vendor claim).

**Finout**
- Virtual tags and MegaBill; also in the space (vendor blog only, not verified further).

**Sources:** https://platform.claude.com/docs/en/manage-claude/usage-cost-api (partner list) ; https://www.datadoghq.com/blog/anthropic-usage-and-costs/ (2025-08-18) ; https://docs.datadoghq.com/integrations/anthropic-usage-and-costs/ ; https://www.vantage.sh/blog/anthropic-analytics (2026-07-02) ; https://www.cloudzero.com/blog/cloudzero-anthropic/ ; https://www.nops.io/ai-cost-visibility-and-optimization/ ; https://www.nops.io/blog/now-supporting-bedrock-claude/ (accessed 2026-09-23).

**Implication for Token Bill.** Don't compete on allocation. **Export recoverable waste as line items**: FOCUS-shaped CSV/Parquet with `x_TokenBill*` columns for breaker, recoverable USD, fix ID and verification status. Then CloudZero, Vantage and Datadog can show "waste" next to "spend". Token Bill becomes a data source these platforms want.

### F22. Cloud and observability vendors now ship coding-agent dashboards; cache hit rate is a standard tile [strong]

**AWS CloudWatch Coding Agent Insights** (2026-07-20)
- Covers Claude Code (via the Claude apps gateway), Codex and GitHub Copilot.
- Shows tokens, cost, active users, sessions, **cache hit rate**, commits and PR velocity.
- Priced at standard OTel metric ingestion.

**Dash0 AI Coding Insights:** $10 per monitored developer per month.

**Also available:** Grafana Cloud, Honeycomb and SigNoz (OSS) Claude Code dashboards.

**Sources:** https://aws.amazon.com/about-aws/whats-new/2026/07/cloudwatch-coding-agent-insights/ (2026-07-20) ; https://www.dash0.com/comparisons/claude-code-monitoring-tools (accessed 2026-09-23).

**Implication for Token Bill.** A hit-rate number alone has no value now. Token Bill's output must answer three questions: which breaker, how many dollars, and what exact fix. It should also ship **Grafana, Datadog and CloudWatch dashboard JSON** driven by Token Bill's own OTLP output metrics.

### F23. Model routers save money but break caches, and no tool measures the net effect [moderate]

- **Not Diamond** claims 20%+ savings (30%+ for its coding router) and 5%+ accuracy gains. SOC 2 and ISO 27001. Its calculator shows 1,000 engineers at $300/month → about $1.2M/year saved (vendor claim).
- **Amazon Bedrock Intelligent Prompt Routing:** "up to 30%" cost reduction (AWS, 2024-12-04). It routes within one model family.
- **Martian's** site now presents it as a research lab (measurement, interpretability), with no router product details. Status unclear.

**Sources:** https://www.notdiamond.ai/ ; https://aws.amazon.com/blogs/aws/reduce-costs-and-latency-with-amazon-bedrock-intelligent-prompt-routing-and-prompt-caching-preview/ (2024-12-04) ; https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-routing.html ; https://www.withmartian.com/ (accessed 2026-09-23).

**Implication for Token Bill.** Per-request routing inside a conversation triggers `model_changed` and full rebuilds (F1, F6). Token Bill can uniquely compute **net routing savings** (router savings minus cache rebuild cost), and recommend routing at conversation or task boundaries.

### F24. Agent vendors meter by token and offer budgets; enterprises run several agents at once [strong]

**GitHub Copilot**
- All plans moved to **usage-based billing on 2026-06-01**. **GitHub AI Credits** (1 credit = $0.01) are consumed by input, output and **cached** tokens at listed rates.
- Business ($19) and Enterprise ($39) include matching credits.
- Budget controls at **enterprise, cost-center and user** levels.

**Cursor** (2026-05-04 release)
- Soft and hard limits, alerts at 50/80/100%.
- Per-user and product-category analytics (clients, Cloud Agents, Bugbot and others).
- Model and provider allow/block lists; Admin/Analytics APIs.

**Sources:** https://github.blog/news-insights/company-news/github-copilot-is-moving-to-usage-based-billing/ ; https://cursor.com/changelog/05-04-26 (2026-05-04).

**Implication for Token Bill.** "Enterprise-wide" means Claude Code, Copilot, Cursor and Codex. Add importers for their usage exports and APIs so leadership can compare **cost per merged PR by agent**. Cache analysis may be shallow there, since the data is aggregate-only, but a normalized comparison is still valuable.

### F25. Real production cache fixes are large, and the root cause is usually volatile content in the prefix [strong]

**ProjectDiscovery** (2026-04-10)
- Cache hit rate went **7% → 84%**: overall **−59%** cost, −66% after the fix, −70% over the last 10 days, with 9.8B tokens served from cache.
- Root cause: dynamic working memory in the system prompt.
- Fixes: move volatile content to a tail user message; three breakpoints (static system 1h, tools 1h, sliding window 5m); date-only timestamps; sticky provider routing.
- They discovered the problem by **deriving effective per-token rates from real spend**.

**"Don't Break the Cache"** (arXiv 2601.06007, 2026-01-09)
- 500+ agent sessions across OpenAI, Anthropic and Google.
- Prompt caching gives **41–80%** cost reduction and 13–31% faster TTFT.
- Excluding dynamic tool results and placing dynamic content at the end beats naive full-context caching.

**Sources:** https://projectdiscovery.io/blog/how-we-cut-llm-cost-with-prompt-caching (2026-04-10) ; https://arxiv.org/abs/2601.06007 (2026-01-09).

**Implication for Token Bill.**
- Token Bill's `volatile-system` breaker targets the most common real-world failure, which is good.
- Use ProjectDiscovery-like patterns as **demo scenarios**: a multi-breakpoint layout and mixed TTLs.
- Add a **multi-breakpoint and 1h-TTL simulator**, since the current simulator models only a single breakpoint at the end of messages.

### F26. FinOps is now responsible for AI spend and is standardizing token data [strong]

**State of FinOps 2026** (2026-02-19)
- **98%** of practitioners manage AI spend, up from 31% two years earlier.
- 1,192 respondents representing $83B+ in cloud spend.
- "FinOps for AI" is the top forward-looking priority.
- 78% of FinOps teams report to the CTO or CIO.

**FinOps X 2026** (2026-06-08/09)
- **FOCUS 1.4** launched.
- The FinOps Foundation and Linux Foundation announced intent to form a **Tokenomics Foundation**.
- The FOCUS working group is weighing per-user, per-session and per-request token data and possible OpenTelemetry alignment.

**Sources:** https://www.linuxfoundation.org/press/state-of-finops-survey-ai-value-and-skills-top-priorities-as-finops-matures-across-technology-value-98-manage-ai-90-saas-64-licensing-48-data-center-1 (2026-02-19) ; https://www.finops.org/insights/finops-x-2026-day-1-keynote/ (2026-06-09) ; https://siliconangle.com/2026/06/08/ai-token-economics-focus-specification-updates-finopsx/ (2026-06-08).

**Implication for Token Bill.** The buyer and reviewer inside "enterprise labs" will increasingly be FinOps as well as platform engineering. Token Bill should:
- speak FOCUS (exports) and OTel (ingestion and emission);
- consider joining the FOCUS token-economy workstream, which also brings visibility;
- frame reports as **recoverable waste plus verified savings**, which maps directly to FinOps "optimize" KPIs.

### F27. Startups are pushing cost toward business outcomes [moderate]

- **Revenium "AI Outcomes"** (2026-03-27) attaches outcomes (CONVERTED, ESCALATED, DEFLECTED) to agent runs. Loan example: 1,000 jobs, $2,950, 780 approvals → $3.78 per conversion. It also sells a Tool Registry and runtime enforcement.
- **Tokenwise** (page updated 2026-05-29) is a one-line proxy with attribution and "a weekly plan to cut your bill ~30%" (vendor claim).

**Sources:** https://www.revenium.ai/post/revenium-launches-ai-outcomes (2026-03-27) ; https://tokenwisehq.com/alternatives/new-relic-ai-monitoring (2026-05-29).

**Implication for Token Bill.** For coding agents, the natural outcome unit is **the merged PR, or the passed CI run**. Join Claude Code Analytics (commits, PRs) or git metadata with cost to report **$ per merged PR** per team and agent. Datadog Agent Console and CloudWatch do something similar, so treat it as expected rather than a differentiator.

### F28. OpenAI offers org/project hard limits and Usage/Costs APIs, the baseline for a multi-provider tool [strong]

- Spend alerts (notify only) and **hard spend limits** at org or project level, returning 429 with `organization_spend_limit_exceeded` / `project_spend_limit_exceeded`.
- Enforcement is not instantaneous; recorded spend can overshoot slightly.
- Usage (`/v1/organization/usage/…`) and Costs (`/v1/organization/costs`) APIs.

**Sources:** https://developers.openai.com/api/docs/guides/spend-limits ; https://developers.openai.com/cookbook/examples/completions_usage_api (accessed 2026-09-23).

**Implication for Token Bill.** Build an OpenAI adapter with an org-scan equivalent (Usage/Costs API) plus per-request `cached_tokens`. Other research tracks cover OpenAI's cache semantics in detail.

---

## 4. The gap Token Bill can own

After removing everything that is commoditized (tracking, attribution, budgets, alerts, hit-rate tiles) or first-party (per-request diagnostics, per-session likely cause), the open space is:

1. **Fleet-scale cache forensics in dollars.** Aggregate cache-loss causes across thousands of developers, agents, gateways and clouds. Rank them by recoverable dollars at contracted rates. Explain the fix. Diagnostics works per request on first-party only; `/usage` works per session on the main conversation only; Datadog and CloudWatch show a ratio. Nobody ranks the fleet's breakers by dollars.
2. **Policy what-ifs from real telemetry.** "If team X used Sonnet by default / `effort=medium` / `promptCacheTtl=1h` / first-party instead of Bedrock / tool search on / batch for nightly jobs, last month would have cost $Y (CI ±Z)." The output is a **ready-to-deploy managed-settings or config patch**. Datadog did this by hand for $975k/month.
3. **Verified savings.** Paired or pre/post, statistically tested, cache-aware, and reconciled against the Anthropic Cost API or invoice. Claims are then labeled "verified" or "not verified" (the RTK lesson). No competitor does this, and it is what a CFO and an enterprise lab reviewer will trust.
4. **Interaction effects others ignore.** Compression × cache (Headroom, RTK), routing × cache (Not Diamond, Bedrock IPR, Cloudflare fallbacks, `opusplan`), gateway × cache (stripped `cache_control`, disabled tool search, lost 1h TTL and global scope), context editing × cache (Anthropic measured +74% on short loops).
5. **Vendor-neutral, local-first, zero-dependency, MIT.** With Langfuse, Helicone, Traceloop and Portkey inside larger companies, and LiteLLM's supply-chain incident, a small auditable stdlib tool that never sends data anywhere is easy for security review to approve across thousands of seats.

**What *not* to build:** another trace UI, another gateway with budgets, another FinOps allocation engine, another compression proxy. Integrate with those and verify their savings.

---

## 5. Enterprise table stakes: parity checklist

| # | Requirement | Current v0.1.2 | Build / integrate / delegate | Priority |
|---|---|---|---|---|
| 1 | Correct pricing for every billed dimension: 5m/1h cache writes, reads, batch 50%, Priority Tier (not in Cost API), fast mode, data residency 1.1×, long-context tiers, server tools (web search, code execution) | Partial (5m, reads) | **Build** | P0 |
| 2 | Contracted rates and invoice reconciliation (match `modelPricing` shape: multiplier + overrides; compare to Cost API) | No | **Build** | P0 |
| 3 | Ingestion at fleet scale: Claude Code session logs, Claude Code OTel, Admin Usage/Cost API, Claude Code Analytics and Enterprise Analytics APIs, cache-diagnostics field | SDK recorder only | **Build** | P0 |
| 4 | Attribution: user, team (IdP group), repo/worktree, workspace, API key, model, agent/subagent, skill, MCP server, tag | Run only | **Build** (from OTel attributes and log metadata) | P0 |
| 5 | Scale: millions of calls per day; incremental, streaming parse; stdlib `sqlite3` store | Single-file JSONL | **Build** | P0 |
| 6 | Privacy: redaction and hash-only fingerprint mode (like Anthropic diagnostics), no network by default, retention controls | Local, no network | **Build** (hash mode) | P0 |
| 7 | Supply-chain: signed releases (Sigstore), PyPI Trusted Publishing, SBOM (CycloneDX), reproducible builds, pinned CI actions | Unknown | **Build** | P0 |
| 8 | Alerting on regressions (cache-read share drop, breaker spike, waste above a threshold), sent to Slack/webhook/email | No | **Integrate** (emit OTLP metrics and webhooks; let Datadog/Grafana/Langfuse alert) | P1 |
| 9 | Budgets and enforcement | No | **Delegate** (Anthropic spend limits, apps gateway, LiteLLM, Cloudflare, Copilot/Cursor); Token Bill *reads* them and recommends caps | — |
| 10 | Multi-provider: OpenAI, Bedrock, Vertex, Foundry, OpenRouter; Gemini later | Anthropic only | **Build** adapters | P1 |
| 11 | Multi-agent: Copilot AI Credits and Cursor usage exports, Codex logs | No | **Build** importers | P1 |
| 12 | Exports: CSV, FOCUS-shaped, Parquet (optional), JSON Schema'd outputs, OTLP metrics out, dashboard JSON | HTML/terminal | **Build** | P1 |
| 13 | CI gate: fail a PR that introduces a cache breaker in prompt assembly | No | **Build** | P1 |
| 14 | Anomaly detection (spend spikes by user/team vs. baseline) | No | **Integrate** (FinOps tools) + a simple built-in z-score for waste | P2 |
| 15 | Hosted UI with SSO/RBAC/audit logs | No | **Avoid** at first; ship static HTML + Grafana/Datadog apps; revisit | P2 |
| 16 | Docs: threat model, accuracy statement (what is exact vs. approximate), reproducibility, support policy | Partial (DESIGN.md) | **Build** | P0 |

---

## 6. What Token Bill should build

Effort: S ≤1 week, M 1–3 weeks, L 3–8 weeks, XL >8 weeks, for one strong engineer.

### P0: pilot-ready for an enterprise lab (target: 6–8 weeks)

1. **Claude Code importers (L)**
   - Session JSONL from `~/.claude/projects/**`.
   - OTLP JSON/protobuf file sink for `claude_code.api_request`, `token.usage` and `cost.usage`.
   - Optional raw-body ingestion under `OTEL_LOG_RAW_API_BODIES`.
   - Attribute everything by user, session, `query_source`, `agent.name`, `skill.name`, `mcp_server.name`, effort and speed. (F2, F3, F12)
2. **Org scan (M).** Read-only pulls from the Usage & Cost Admin API, Claude Code Analytics API and Enterprise Analytics API. Produce a 10-minute executive report: fleet cache-read share versus Anthropic's published median and p90 (84% / 94%), cache write/read thrash by key, workspace and user, 5m/1h write mix, batch-eligible share, and Priority Tier caveats. (F4, F5)
3. **Cache-diagnostics ingestion and fleet aggregation (M).**
   - Accept `diagnostics.cache_miss_reason` in `trace@2`.
   - Aggregate by type and cause.
   - Recommend enabling the header in SDK/recorder paths.
   - Fall back to Token Bill's own diffing on Bedrock, Vertex, Foundry and `unavailable`. (F1)
4. **Claude Code and gateway breaker pack (L).**
   - `gateway-strips-cache-control`, `tool-search-disabled`, `mcp-prefix-churn`.
   - `opusplan/fallback/skill-model switch`, `effort-toggle`, `fast-mode-late`.
   - `idle-ttl-expiry` with a 5m/1h simulation, `upgrade-rebuild`, `image-eviction`, `compaction` (expected versus avoidable), `worktree/cwd scope`.
   - Each with a managed-settings or config fix and dollars. (F6, F7)
5. **Pricing and reconciliation v2 (M).**
   - 1h writes (2×), batch, fast mode, data residency 1.1×, long-context, server tools.
   - `--rates contract.json` in a `modelPricing`-compatible shape.
   - Reconcile modeled dollars to the Cost API per day, model and workspace, with a tolerance report. (F2, F4)
6. **Fleet store and scale (M).** Stdlib `sqlite3` store with an incremental ingest watermark; tested at 10M calls; per-user, per-team and per-repo rollups; team mapping from IdP-group CSV.
7. **Enterprise hygiene (S–M).** Sigstore-signed wheels, Trusted Publishing, SBOM, a hash-only privacy mode (no raw prompt text persisted), and an "exact vs. approximate" accuracy doc. Update the README's "Related work" (F1, F2, F8) so reviewers don't find stale claims. (F17)

### P1: the differentiators (target: +8–12 weeks)

8. **`tokenbill verify` (L).** Pre/post or A/B comparison of billed cost per task, session or merged PR, with paired bootstrap CIs, turn-count drift and a cache-aware counterfactual. Output: "claimed vs. verified" for any change or third-party optimizer. Emit a signed JSON "savings receipt". (F9, F10, F11)
9. **Policy what-if simulator (L).** Reprice real fleet usage under alternative defaults: model, effort, TTL, first-party versus gateway/cloud, batch for non-interactive `-p`/cron traffic, tool search, task budgets. Print expected dollars with ranges and quality caveats, and emit a **managed-settings patch**. Integrate with an eval result file (pass rates) so a quality delta is always shown with the dollar delta. (F5, F8)
10. **Fix library as code, plus a PR bot (M–L).** Versioned fixes: PreToolUse hooks, managed-settings snippets, SDK diffs for tool ordering, system-prompt stabilization and breakpoint placement. Add a GitHub Action with `tokenbill check --fail-on breaker` for prompt-assembly code. (F8, F13)
11. **Multi-provider and multi-agent adapters (L).** OpenAI (per-request plus Usage/Costs API), OpenRouter (`cache_discount`, sticky routing), Bedrock/Vertex/Foundry model IDs and cache availability, Langfuse/LiteLLM/Helicone/LangSmith importers, and Copilot AI Credits and Cursor usage exports. (F14–F20, F24, F28)
12. **Outputs for other tools (M).** FOCUS-shaped waste export (`x_TokenBillBreaker`, `x_RecoverableUSD`, `x_FixId`, `x_Verified`); OTLP metrics out (`tokenbill.recoverable.usd`, `tokenbill.cache_read_share`, by team and breaker); Grafana, Datadog and CloudWatch dashboard JSON; Slack/webhook weekly digest per team. (F21, F22, F26)
13. **Interaction analyses (M).** Routing × cache (net router savings), compression × cache (does Headroom/RTK rewrite history?), context-editing × cache. (F10, F23)

### P2: expansion

14. **Cost per outcome (M).** Join cost with commits, PRs and CI results (Claude Code Analytics API or git) to report $ per merged PR by team and agent. (F27)
15. **Opt-in thin recording proxy (M–L)** for HTTP-only stacks. Pass-through by default, with an optional "cache guard" mode (deterministic tool ordering, volatile-field relocation) that is always verified with `tokenbill verify`.
16. **Multi-breakpoint and 1h-TTL simulator, and keep-alive analysis (M)**, e.g. Anthropic's Fable 5.1 keep-alive recommendation. (F5, F25)
17. **Optional self-hosted team UI with SSO (XL).** Only if pilots demand it. Otherwise static reports plus dashboards in existing tools.

---

## 7. Positioning and wording

- **One-liner:** "Token Bill finds the dollars your AI agents waste, tells you the exact fix, and proves the saving on your invoice."
- **Against first-party tools:** "Anthropic tells you *this request* missed its cache. Token Bill tells you *which 40 causes cost your org $X last month*, gives the managed-settings patch, and verifies the saving."
- **Against Datadog/CloudWatch:** "Your dashboard shows the hit rate. Token Bill explains it, prices it, fixes it and verifies the fix, locally, with no vendor lock-in."
- **Against optimizers (RTK, Headroom and others):** "Don't trust the counter. Verify against billed usage."
- **README corrections needed now:** the "Related work" section claims provider dashboards are "silent on *why*" and that no tool "prices a specific fix". Rewrite it to acknowledge cache diagnostics, Claude Code `/usage` likely-cause, and Datadog Agent Console's Fix Library, and to state Token Bill's fleet, dollars and verification difference.

---

## 8. Risks

- **Anthropic keeps moving up the stack.** Diagnostics could go GA with fleet aggregation, or `/usage` could gain an org view. Mitigation: be the multi-provider, multi-agent, verification-first layer, and treat Anthropic's signals as inputs.
- **Datadog Agent Console reaches GA** with a broad fix library. Mitigation: open source, local-first, cache depth, verified savings, and exports *into* Datadog.
- **Beta API churn.** Cache diagnostics field names "may change". Version-pin the schema mapping.
- **Privacy review.** Payload capture may be refused. Design for metadata-only operation (OTel counts, diagnostics, hashes) with degraded but honest localization.
- **Accuracy disputes.** Any mismatch with the invoice undermines trust. Reconciliation (P0 #5) is mandatory before claiming dollars at enterprise scale.

---

## 9. Open questions

1. How does the target enterprise buy Claude Code: Claude Enterprise seats (Enterprise Analytics API; seat allowance plus usage credits), Console API keys (Admin + Claude Code Analytics APIs), or Bedrock/Vertex/Foundry (OTel or apps gateway only)? This decides the P0 ingestion order and which levers apply. The 1h TTL is the default only for subscription users within plan usage.
2. Is Claude Code OTel already exported fleet-wide, and to which backend? Will security allow `OTEL_LOG_RAW_API_BODIES` or any payload capture?
3. Is there an LLM gateway (LiteLLM, Portkey/Prisma AIRS, Cloudflare, apps gateway) in the path? If so, does it forward `cache_control` and `anthropic-beta` unchanged? (F6, F7)
4. Which other agents are in use (Copilot, Cursor, Codex), and can their usage exports be accessed?
5. Are there contracted or discounted rates? Without them, dollar figures will not match finance's numbers.
6. Is Datadog Agent Console (Preview) or CloudWatch Coding Agent Insights already licensed? If so, position Token Bill as complementary (exporting into them).
7. Is there an internal eval harness (like Datadog's 140+ evals)? Policy what-ifs on model and effort need a quality signal.
8. Unresolved in the sources:
   - Datadog's blog describes cache-hit-ratio monitor templates, but its integration docs list none.
   - Martian's current router product status is unclear.
   - Future of Helicone self-hosting and of OpenLLMetry under ServiceNow.
   - Retention window of cache-diagnostics fingerprints ("short period").
   - Bundled versus live Anthropic caching multipliers (2.5–3.7× vs. 2.7–5.3×).

---

## 10. Source index (all opened 2026-09-23 unless a date is given)

**Anthropic / Claude (primary)**
- Cache diagnostics (beta `cache-diagnosis-2026-04-07`): https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics
- Usage and Cost API: https://platform.claude.com/docs/en/manage-claude/usage-cost-api
- Claude Code Analytics API: https://platform.claude.com/docs/en/build-with-claude/claude-code-analytics-api
- Analytics APIs (Enterprise): https://platform.claude.com/docs/en/manage-claude/analytics-api
- Optimizing for cost and intelligence: https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence.md
- Claude Code costs: https://code.claude.com/docs/en/costs
- Claude Code monitoring (OTel): https://code.claude.com/docs/en/monitoring-usage
- Claude Code prompt caching: https://code.claude.com/docs/en/prompt-caching
- Claude apps gateway: https://code.claude.com/docs/en/claude-apps-gateway
- Apps gateway spend limits: https://code.claude.com/docs/en/claude-apps-gateway-spend-limits
- Bundled skill docs (v2.1.280): `/private/tmp/claude-501/bundled-skills/2.1.280/.../claude-api/shared/{admin-api,cost-optimization,prompt-caching,platform-availability,live-sources}.md`

**Observability and coding-agent monitoring**
- Datadog Anthropic CCM (2025-08-18): https://www.datadoghq.com/blog/anthropic-usage-and-costs/
- Datadog LLM Obs cost: https://docs.datadoghq.com/llm_observability/monitoring/cost/
- Datadog Anthropic Usage & Costs integration: https://docs.datadoghq.com/integrations/anthropic-usage-and-costs/
- Datadog Agent Console (2026-06-09): https://www.datadoghq.com/blog/claude-code-monitoring/
- Datadog saves $1M/month (2026-08-26): https://www.datadoghq.com/blog/how-datadog-saves-money-by-optimizing-ai-usage/
- CloudWatch Coding Agent Insights (2026-07-20): https://aws.amazon.com/about-aws/whats-new/2026/07/cloudwatch-coding-agent-insights/
- Dash0 comparison: https://www.dash0.com/comparisons/claude-code-monitoring-tools
- Langfuse cost tracking: https://langfuse.com/docs/observability/features/token-and-cost-tracking ; pricing https://langfuse.com/pricing ; spend alerts (2025-10-10) https://langfuse.com/changelog/2025-10-10-spend-alerts ; monitors (2026-06-19) https://langfuse.com/changelog/2026-06-19-monitors
- ClickHouse acquires Langfuse (2026-01-16): https://clickhouse.com/blog/clickhouse-acquires-langfuse-open-source-llm-observability ; https://siliconangle.com/2026/01/16/database-maker-clickhouse-raises-400m-acquires-ai-observability-startup-langfuse/
- LangSmith cost tracking: https://docs.langchain.com/langsmith/cost-tracking ; pricing https://www.langchain.com/pricing
- Braintrust pricing: https://www.braintrust.dev/pricing ; comparison (2026-06-21) https://www.braintrust.dev/articles/best-tools-tracking-llm-costs-2026
- W&B Weave costs: https://docs.wandb.ai/weave/guides/tracking/costs
- Arize Phoenix cost tracking: https://arize.com/docs/phoenix/tracing/how-to-tracing/cost-tracking ; repo https://github.com/Arize-ai/phoenix
- New Relic AI monitoring: https://docs.newrelic.com/docs/ai-monitoring/intro-to-ai-monitoring/
- Traceloop → ServiceNow (Mar 2026): https://www.calcalistech.com/ctechnews/article/sjghwiqf11e

**Gateways and routers**
- Helicone joins Mintlify (2026-03-03): https://www.helicone.ai/blog/joining-mintlify ; cost docs https://docs.helicone.ai/guides/cookbooks/cost-tracking ; repo https://github.com/Helicone/helicone
- Portkey OSS gateway (2026-03-24): https://www.globenewswire.com/news-release/2026/03/24/3261574/0/en/portkey-s-gateway-is-now-fully-open-source-processing-over-1-trillion-tokens-every-day.html ; budgets https://portkey.ai/docs/product/ai-gateway/virtual-keys/budget-limits ; pricing https://portkey.ai/pricing
- Palo Alto Networks completes Portkey acquisition (2026-05-29): https://www.paloaltonetworks.com/company/press/2026/palo-alto-networks-completes-acquisition-of-portkey-to-secure-ai-agents
- LiteLLM spend tracking: https://docs.litellm.ai/docs/proxy/cost_tracking ; tag budgets https://docs.litellm.ai/docs/proxy/tag_budgets ; security incident (2026-03-24) https://docs.litellm.ai/blog/security-update-march-2026
- Cloudflare AI Gateway spend limits (2026-06-05): https://blog.cloudflare.com/ai-gateway-spend-limits/
- Bifrost: https://github.com/maximhq/bifrost
- OpenRouter prompt caching: https://openrouter.ai/docs/guides/best-practices/prompt-caching ; usage accounting https://openrouter.ai/docs/use-cases/usage-accounting
- Not Diamond: https://www.notdiamond.ai/
- Martian: https://www.withmartian.com/
- Bedrock intelligent prompt routing (2024-12-04): https://aws.amazon.com/blogs/aws/reduce-costs-and-latency-with-amazon-bedrock-intelligent-prompt-routing-and-prompt-caching-preview/ ; https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-routing.html

**FinOps**
- Vantage Claude Enterprise (2026-07-02): https://www.vantage.sh/blog/anthropic-analytics
- CloudZero + Anthropic: https://www.cloudzero.com/blog/cloudzero-anthropic/
- nOps AI cost: https://www.nops.io/ai-cost-visibility-and-optimization/ ; https://www.nops.io/blog/now-supporting-bedrock-claude/
- State of FinOps 2026 (2026-02-19): https://www.linuxfoundation.org/press/state-of-finops-survey-ai-value-and-skills-top-priorities-as-finops-matures-across-technology-value-98-manage-ai-90-saas-64-licensing-48-data-center-1
- FinOps X 2026 keynote (2026-06-09): https://www.finops.org/insights/finops-x-2026-day-1-keynote/
- FOCUS and token economics (2026-06-08): https://siliconangle.com/2026/06/08/ai-token-economics-focus-specification-updates-finopsx/

**Local and OSS tools, benchmarks, case studies**
- ccusage: https://github.com/ryoppippi/ccusage
- Claude-Code-Usage-Monitor: https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor
- Terse tracker comparison (2026-08-28): https://www.terseai.org/claude-code-cost-tracker
- Headroom: https://github.com/headroomlabs-ai/headroom
- RTK: https://github.com/rtk-ai/rtk ; JetBrains benchmark (Jul 2026) https://blog.jetbrains.com/ai/2026/07/rtk-claude-code-token-savings/
- token-optimizer: https://github.com/alexgreensh/token-optimizer
- Claude Code token-optimization stack: https://github.com/sgaabdu4/claude-code-tips
- promptcachelint: https://github.com/OsmnvAslan/promptcachelint
- ProjectDiscovery (2026-04-10): https://projectdiscovery.io/blog/how-we-cut-llm-cost-with-prompt-caching
- "Don't Break the Cache" (arXiv 2601.06007, 2026-01-09): https://arxiv.org/abs/2601.06007
- Revenium AI Outcomes (2026-03-27): https://www.revenium.ai/post/revenium-launches-ai-outcomes
- Tokenwise (2026-05-29): https://tokenwisehq.com/alternatives/new-relic-ai-monitoring

**Agent vendors**
- GitHub Copilot usage-based billing (effective 2026-06-01): https://github.blog/news-insights/company-news/github-copilot-is-moving-to-usage-based-billing/
- Cursor enterprise controls (2026-05-04): https://cursor.com/changelog/05-04-26
- OpenAI spend limits: https://developers.openai.com/api/docs/guides/spend-limits ; Usage/Costs API cookbook https://developers.openai.com/cookbook/examples/completions_usage_api
