# Track: Claude Code (and other coding agents) cost in the enterprise

Research pass for Token Bill, dated **2026-09-23**. Scope: what an org-wide optimizer for Claude Code (plus Codex CLI, Gemini CLI, GitHub Copilot and Cursor) has to ingest, measure, recommend and enforce to make the bill smaller.

---

## 0. TL;DR for the Token Bill roadmap

1. **Claude Code cost is mostly the context re-read on every request, not the code it writes.** Every request re-sends the full conversation. On my one-developer sample (details in §2), the list-price cost broke down as **60.5% cache reads, 31.0% cache writes (27.9% of that 1-hour writes), 8.2% output and 0.2% uncached input**. Requests carrying more than 200K tokens of context made up **85% of cost**. The biggest levers are context length × request count, and not rebuilding the cache.
2. **Defaults now let sessions grow to about 1M tokens before compacting.** On 1M-native models (Sonnet 5, Fable, Opus 4.7 and later), Claude Code auto-compacts at **about 967K tokens** by default. In my sample, all 20 compactions were automatic, with a median of **993K tokens** before compaction. The admin controls are `autoCompactWindow` / `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (100K–1M) and `CLAUDE_CODE_DISABLE_1M_CONTEXT`. None of the current tools simulates what a lower window would have saved. Token Bill should be the first.
3. **Cache rebuilds are rare but expensive.** In my sample, 1.7% of requests (large-context requests where more than half the context was re-written to cache) accounted for **24% of cost**. Anthropic's docs list the Claude Code causes: model switch, effort change on most models, fast-mode first turn, MCP connect/disconnect when tools aren't deferred, plugins that carry MCP servers, denying a whole tool, compaction, image eviction, upgrades, and gateways that strip `cache_control`. Token Bill's 5 generic breakers need Claude Code-specific causes.
4. **The data is already there. Nobody turns it into dollars saved.** Claude Code emits OTel metrics, events and traces (exact names are in F7). It also writes local JSONL transcripts and exposes a Claude Code Analytics API, an Enterprise Analytics API (with per-user cost by product, model, context-window tier, speed and token type), gateway attribution headers, and hooks that can rewrite tool input and output. ccusage (18.7K stars) only aggregates. Datadog Agent Console (June 2026) is the closest competitor: it detects retry loops, re-reads and skipped checks, and estimates monthly savings per repo.
5. **An enterprise product needs four layers.** Token Bill should build: (a) ingestion from OTel, the Admin/Analytics APIs and transcripts; (b) counterfactual simulation of policy changes (compaction window, TTL, model/effort caps, subagent model, tool-output caps); (c) a generated **managed-settings policy pack** with a projected dollar impact per key; (d) before/after verification, variance-aware because the same task can vary 30× in tokens.

---

## 1. Method and caveats

- **Primary sources.** I downloaded the official Claude Code docs as raw markdown from `code.claude.com/docs/en/*.md`: costs, monitoring-usage, prompt-caching, model-config, settings-reference, env-vars, sessions, context-window, statusline, hooks, mcp, sub-agents, workflows, agent-teams, fast-mode, advisor, analytics, managed-settings, server-managed-settings, llm-gateway-protocol, claude-apps-gateway-spend-limits, agent-sdk/cost-tracking, best-practices and large-codebases. I also pulled the platform docs (pricing, Claude Code Analytics API, Usage & Cost API, Enterprise Analytics API, token counting). These are living documents with no publication date. I accessed all of them on 2026-09-23; the version gates they reference go up to about v2.1.280.
- **Other agents.** Official Codex docs (learn.chatgpt.com), the Gemini CLI `docs/cli/telemetry.md` (last commit 2026-06-18), the GitHub changelog and blog, Cursor's Admin API docs, and ccusage's source and docs.
- **Papers.** arXiv abstracts, each opened.
- **Local empirical sample (n = 1 developer; illustrative only).** I read field names and token counts, never content, from the Claude Code transcripts on this machine (`~/.claude/projects`):
  - 1,577 JSONL files, of which 1,549 are subagent files.
  - 55 active days between 2026-05-13 and 2026-09-23.
  - 18,180 unique requests after de-duplication.
  - Priced at list rates from the pricing page.
  - This is a heavy user on a subscription with a 1-hour main-conversation TTL, at about $132 of list-price-equivalent per active day. That is roughly 10× Anthropic's enterprise average, so treat the percentages as the shape of a power user's bill, not a fleet average. It is exactly the kind of top-decile user Token Bill must find.
  - Tool-result sizes are measured in characters, and base64 images inflate them, so the tool-output shares are approximate.

---

## 2. Findings

Each finding lists the claim, the numbers, the sources (with dates), what Token Bill should do, estimated savings, build effort and evidence strength.

### F1. Enterprise baseline: about $13 per developer per active day, $150–250 per month. The heavy tail is where the money is.
- **Claim.** Anthropic states that across enterprise deployments the average is about **$13 per developer per active day and $150–250 per developer per month**, with **90% of users below $30 per active day**. It recommends piloting to set a baseline. For the Claude Console it recommends per-user TPM/RPM by team size, from 200–300K TPM for 1–5 users down to 10–15K TPM for 500+ users. News coverage shows the tail blowing up budgets:
  - Uber exhausted its 2026 AI-coding budget by April.
  - One engineer reportedly spent $40K in a month.
  - Faros AI and Jellyfish data: per-developer token consumption up **18.6× in nine months**; top users about 2× as productive at 10× the tokens.
  - Amazon reportedly shut down an internal AI-usage leaderboard over "tokenmaxxing" (AndroidHeadlines, June 2026; only the search snippet was seen, not opened).
- **Sources.**
  - https://code.claude.com/docs/en/costs (living doc, accessed 2026-09-23)
  - https://techcrunch.com/2026/06/05/the-token-bill-comes-due-inside-the-industry-scramble-to-manage-ais-runaway-costs/ (2026-06-05)
- **Token Bill should:** report cost per active developer-day against the $13 / $30 anchors. Rank the p90 and p99 developers, sessions and repos. Alert on outliers, for example a developer-day over $30 or 3× their own baseline. Frame every report as the heavy tail's share of spend.
- **Savings:** not applicable (this is a baseline). **Effort:** S. **Evidence:** strong for Anthropic's numbers, moderate for the news.

### F2. The Claude Code bill is dominated by re-reading context (cache reads and writes), not output
- **Claim.** Claude Code "sends your full conversation with every request", and each tool round-trip is another request. Research agrees that input dominates agentic cost:
  - Bai et al. (2026): agentic coding uses about **1000× the tokens of code chat**, and input tokens dominate.
  - Tokenomics (2026): input tokens are **53.9%** of consumption, and iterative code review is **59.4%** of tokens.
- **Local sample (list price).**
  - Cost split: cache reads 60.5%, cache writes 31.0%, output 8.2%, uncached input 0.2%.
  - Context per request: median 269K tokens, p90 822K.
  - Share of cost by context size per request:

    | Context per request | Share of cost |
    |---|---|
    | > 100K | 94.6% |
    | > 200K | 85.3% |
    | > 400K | 70.1% |
    | > 800K | 27.2% |
- **Sources.**
  - https://code.claude.com/docs/en/costs ("Why usage climbs in a long session", accessed 2026-09-23)
  - https://arxiv.org/abs/2604.22750 (2026-04-24)
  - https://arxiv.org/abs/2601.14470 (2026-01-20)
- **Token Bill should:**
  - Make **context carried per request** and **requests per task** first-class metrics.
  - Break cost down as cache-read, cache-write-5m, cache-write-1h, uncached input and output (split thinking out of output where `thinking_tokens` is present).
  - Add a "context tax" chart: cost against context-size buckets.
  - Rank sessions by cost-weighted context size.
- **Savings:** unknown until simulated (see F3). **Effort:** M. **Evidence:** strong for the docs and papers; the local numbers are weak (n = 1).

### F3. Auto-compaction defaults to about 967K on 1M-context models. Lowering the window is the single largest lever.
- **Claim.**
  - When no window is set, Claude Code compacts at the model's limit. For native 1M-window models (Sonnet 5, Fable, Opus 4.7 and later on the Anthropic API), that is **about 967K tokens**. Sonnet 4.6 / Opus 4.6 without extended context compact at 200K.
  - Admins can set `autoCompactWindow` (100,000–1,000,000) in any settings file, including managed settings. Precedence is `--autocompact`, then `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, which overrides both.
  - `CLAUDE_CODE_DISABLE_1M_CONTEXT=1` holds 1M-native models to 200K.
  - The 1M window has no long-context price premium, so nothing on the invoice discourages running at 900K. You just pay for 900K cached reads on every request.
  - Quality argument: the Claude Code best-practices page says "performance degrades as [the context window] fills", and Chroma's *Context Rot* shows degradation across 18 models as input grows.
- **Local sample.** 20 of 20 compactions were `auto`, with median `preTokens` of **993,070**. `compact_boundary` entries carry `compactMetadata.{trigger, preTokens, postTokens, durationMs, cumulativeDroppedTokens}`.
- **Sources.**
  - https://code.claude.com/docs/en/model-config (auto-compaction section, accessed 2026-09-23)
  - https://code.claude.com/docs/en/settings-reference#autocompactwindow
  - https://code.claude.com/docs/en/best-practices
  - https://www.trychroma.com/research/context-rot (2025-07-14)
  - https://platform.claude.com/docs/en/about-claude/pricing (long-context pricing)
- **Token Bill should:** build a **compaction-window counterfactual simulator** that replays each session's request sequence under windows W ∈ {150K, 200K, 300K, 500K}:
  - When context passes W, insert a compaction request: one cache read of the prefix while warm, plus summary output.
  - Reset context to `postTokens`. Use the observed median post/pre ratio, or the session's own ratio.
  - Re-accumulate context at the session's observed per-turn growth.
  - Output $ saved per developer and org-wide, plus a recommended `autoCompactWindow` for managed settings.
  - Flag the quality risk: more compactions mean more summary loss. Mitigations are compact instructions in CLAUDE.md and SessionStart hooks on `compact`.
- **Savings:** estimated 30–60% of the cost of long sessions. This is my estimate from the context-share table in F2, not published; verify with the simulator. **Effort:** M. **Evidence:** strong for the mechanism, weak for the magnitude.

### F4. Claude Code-specific cache breakers, and how much they cost
- **Claim.** The official list of actions that invalidate the Claude Code cache:
  - **Model switch.** This includes `opusplan` plan-mode toggles, automatic model fallback on classifier flags, and a skill or command whose frontmatter names a different model.
  - **Effort change.** Most models keep one cache per effort level. Opus 5.5 and Fable 5.1 on the API or a subscription keep the cache across effort changes, but not on Bedrock, Vertex (Agent Platform) or the Claude apps gateway.
  - **Turning fast mode on.** The header is part of the cache key, and the first fast turn is billed at fast rates on uncached context.
  - **MCP connect or disconnect** when its tools are loaded into the prefix rather than deferred.
  - **Plugins that provide MCP servers.**
  - **Denying a whole tool** when tool search is off.
  - **Compaction.**
  - **Batch eviction of old images** once request limits are reached.
  - **Upgrading Claude Code**, which changes the system prompt.
  - **Changing directory or worktree.** The cache is effectively scoped per machine and directory, because the prompt carries cwd, platform, OS and the git status snapshot.
  - `/usage` now counts a **miss** when a request re-processes more than 5% of the cacheable prefix and at least 2,000 tokens (v2.1.251). It names a "likely cause" (v2.1.260) and separates out "expected rebuilds" (compaction or tool-result clearing).
  - Claude Code's own team treats cache hit rate like uptime: "alerts on prompt cache hit rate" and SEVs when it is too low (Anthropic blog, 2026-04-30).
- **Local sample.** Full-rebuild-like requests (context over 50K and cache write over 50% of context) were 1.7% of requests and **24.2% of cost**. At Fable 1-hour write rates ($20/MTok), rebuilding an 800K context costs about $16 each time. Transcripts also contain `model_refusal_fallback` system entries, which are automatic model switches.
- **Sources.**
  - https://code.claude.com/docs/en/prompt-caching (accessed 2026-09-23)
  - https://code.claude.com/docs/en/costs#prompt-cache-statistics
  - https://claude.dev/blog/lessons-from-building-claude-code-prompt-caching-is-everything/ (2026-04-30)
- **Token Bill should:** extend `breakers.py` with Claude Code causes that can be inferred from transcripts and OTel. Each rebuild event should be tagged with one of:

  | Cause | Signal |
  |---|---|
  | model change | `message.model` differs between consecutive main-thread requests |
  | effort change | `effort` / `perTurnEffort` fields; OTel `effort` attribute |
  | fast-mode on | `usage.speed` flips to `fast` |
  | version upgrade | `version` changes across a resume |
  | resume after TTL | timestamp gap greater than the TTL, taken from `cache_creation.ephemeral_1h/5m` |
  | compaction | `compact_boundary` entry |
  | cwd change | `relocated` / `cwd` entries |
  | MCP/tool-set change | attachments / `preCompactDiscoveredTools` |
  | unexplained | none of the above |

  Report each cause's $ and share, with a fix: pick model and effort at session start, turn fast mode on at the start not mid-session, use `/rewind` rather than `/compact` to abandon a path, avoid switching mid-task, and add a PreModelSwitch hook that shows the rebuild cost.
- **Savings:** up to about 20% of cost for users with frequent rebuilds. This is the local sample's 24% upper bound; not published. **Effort:** M. **Evidence:** strong for the mechanism, weak for the magnitude.

### F5. Cache TTL policy is an admin decision with direct dollar impact
- **Claim.**
  - Default TTLs:

    | Request type | Subscription, within plan usage | API key or cloud provider |
    |---|---|---|
    | Main conversation | 1 hour | 5 minutes |
    | Subagents, workflows, teammates, compaction, titles | 5 minutes (except some server-controlled helpers) | 5 minutes |

  - When a subscription starts drawing on usage credits, the main conversation drops to 5 minutes.
  - Controls, from v2.1.242: `promptCacheTtl` and `subagentPromptCacheTtl` settings, the `CLAUDE_CODE_PROMPT_CACHE_TTL` and `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL` env vars, `FORCE_PROMPT_CACHING_5M`, `ENABLE_PROMPT_CACHING_1H`, and a per-subagent `experimental.cacheTtl`. All of these can go in the `env` block of managed settings.
  - Pricing: 1-hour writes cost **2×** base input and 5-minute writes **1.25×**. Reads cost 0.1× (0.05× on Opus 5.5, 0.025× on Fable 5.1). Breakeven is 1 read for 5-minute writes and 2 reads for 1-hour writes.
  - Transcripts record which TTL was written, in `usage.cache_creation.ephemeral_1h_input_tokens` and `ephemeral_5m_input_tokens`.
- **Sources.**
  - https://code.claude.com/docs/en/prompt-caching#cache-lifetime
  - https://platform.claude.com/docs/en/about-claude/pricing (accessed 2026-09-23)
- **Token Bill should:** build a **TTL optimizer**. From each developer's distribution of gaps between requests (start to start), compute the cost under 5-minute vs 1-hour TTL, for the main conversation and subagents separately. Recommend `promptCacheTtl: "1h"` for API-key fleets whose developers idle for 5–60 minutes between turns (think time, reviewing diffs, meetings). Also flag 1-hour writes wasted on short bursts. The simulator already models a sliding TTL, so it needs a 1-hour mode and 2× write pricing.
- **Savings:** estimated 5–15% for API-key fleets with bursty usage (estimate, unverified). **Effort:** S–M. **Evidence:** strong for the mechanism.

### F6. Enterprise gateways can silently destroy caching and tool deferral
- **Claim.**
  - If an LLM gateway or custom `ANTHROPIC_BASE_URL` **removes `cache_control` markers but returns success**, "your entire conversation history bills as uncached input on every turn". Gateways that convert block-form system content to a string drop the marker the same way. A gateway that rejects the marker with a 400 makes Claude Code move the marker onto the last message.
  - **Tool search is disabled by default when `ANTHROPIC_BASE_URL` is a non-first-party host.** All MCP tool definitions then load upfront into the cached prefix, and every MCP reconnect invalidates the cache. Override with `ENABLE_TOOL_SEARCH`.
  - The 1-hour TTL through a gateway needs the `anthropic-beta` header forwarded unchanged. The Claude apps gateway doesn't support the 1-hour TTL.
  - On a gateway with an unrecognized model ID, Claude Code may assume the wrong context window. The fix is `CLAUDE_CODE_MAX_CONTEXT_TOKENS`.
- **Sources.**
  - https://code.claude.com/docs/en/prompt-caching#where-the-cache-lives
  - https://code.claude.com/docs/en/mcp#configure-tool-search
  - https://code.claude.com/docs/en/llm-gateway-protocol (accessed 2026-09-23)
- **Token Bill should:** ship a **gateway conformance check**. Signals: `cache_read_input_tokens == 0` across multi-turn sessions, a large first-request prefix that repeats uncached, and cache-write spikes after MCP reconnects. Emit a P0 finding with the dollar loss, for example: "your gateway strips caching; you are paying 10× for history". Recommend `ENABLE_TOOL_SEARCH=true`, forwarding `anthropic-beta`, and passing `anthropic-*` headers and body fields through unchanged.
- **Savings:** up to about 90% of input-side cost where caching is fully stripped. Cache reads are 0.1× of input, so this follows arithmetically from the pricing page. **Effort:** S. **Evidence:** strong.

### F7. Claude Code OpenTelemetry: exact schema to ingest
- **Enable.** `CLAUDE_CODE_ENABLE_TELEMETRY=1`, with exporters from `OTEL_METRICS_EXPORTER` / `OTEL_LOGS_EXPORTER` / `OTEL_TRACES_EXPORTER` (`otlp` | `prometheus` | `console`), set through managed settings `env`. `otelHeadersHelper` provides dynamic auth. Export intervals default to 60 s for metrics and 5 s for logs and traces.
- **Metrics.**
  - `claude_code.token.usage` (tokens):
    - `type` ∈ {input, output, cacheRead, cacheCreation}
    - `model`
    - `query_source` ∈ {main, subagent, auxiliary}
    - `speed`
    - `effort`
    - attribution labels: `agent.name`, `skill.name`, `plugin.name`, `marketplace.name`, `mcp_server.name`, `mcp_tool.name`
  - `claude_code.cost.usage` (USD), with the same attributes.
  - `claude_code.session.count` (`start_type`: fresh / resume / continue / agents_view).
  - `claude_code.lines_of_code.count`
  - `claude_code.commit.count`
  - `claude_code.pull_request.count`
  - `claude_code.code_edit_tool.decision`
  - `claude_code.active_time.total` (`type`: user / cli)
- **Standard attributes.**
  - `session.id`, `organization.id`, `user.account_uuid`, `user.account_id`, `user.id`, `user.email`, `terminal.type`
  - Optional, via `OTEL_METRICS_INCLUDE_*`: `app.version`, `app.entrypoint`
  - `vcs.repository.url.full` / `vcs.owner.name` / `vcs.repository.name` (`OTEL_METRICS_INCLUDE_REPOSITORY`, v2.1.269+)
  - Custom `OTEL_RESOURCE_ATTRIBUTES`, for example `department=…,team.id=…,cost_center=…`, the documented multi-team pattern.
- **Events (logs).**
  - `claude_code.user_prompt`
  - `claude_code.assistant_response` (v2.1.193+)
  - `claude_code.tool_result` (`tool_name`, `success`, `duration_ms`, `error_type`)
  - `claude_code.api_request`: `model`, `request_id`, `client_request_id`, `duration_ms`, `ttft_ms`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `speed`, `effort`, `query_source`, success/status
  - `claude_code.api_error`
  - `claude_code.tool_decision`
  - `claude_code.api_request_body` / `api_response_body`: full Messages API JSON, gated by `OTEL_LOG_RAW_API_BODIES` (inline truncated, or `file:<dir>` untruncated), v2.1.274+
  - Correlation attributes on all events: `prompt.id`, `event.sequence`, `message.uuid`, `client_request_id`
- **Traces (beta, `CLAUDE_CODE_ENHANCED_TELEMETRY_BETA=1`).** Span tree `claude_code.interaction` → `claude_code.llm_request` / `claude_code.tool` → subagent spans. `llm_request` carries `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `agent_id`, `parent_agent_id`, `stop_reason` and `effort`. `claude_code.tool` carries `result_tokens` per tool call.
- **Privacy defaults.** Prompts, tool details and content are redacted unless the `OTEL_LOG_*` gates are enabled.
- **Sources.** https://code.claude.com/docs/en/monitoring-usage (accessed 2026-09-23; version gates up to v2.1.274)
- **Token Bill should:**
  - Ship an **OTLP receiver** (gRPC and HTTP) or a collector exporter that normalizes these into a canonical call record (§4).
  - Use `api_request` events for per-request waterfalls.
  - Use trace `tool.result_tokens` for tool-output attribution.
  - Use `api_request_body` (where an enterprise opts in) to feed Token Bill's existing byte-level redundancy and breaker analysis. That is the only path to exact prefix diffs org-wide.
  - Treat resource attributes as the chargeback keys.
- **Savings:** none directly (this is the data layer). **Effort:** M. **Evidence:** strong.

### F8. Programmatic org data: three Anthropic APIs, each covering a different population
- **Claude Code Analytics API (Console/API orgs).**
  - `GET /v1/organizations/usage_report/claude_code?starting_at=YYYY-MM-DD` with an Admin API key.
  - One record per user or API key per day: `core_metrics` (sessions, LOC added/removed, commits, PRs), `tool_actions` accept/reject, and `model_breakdown[]` with tokens `{input, output, cache_read, cache_creation}` and `estimated_cost.amount` (cents).
  - Up to 1-hour delay; limit ≤ 1000 per page. Not available on Claude Platform on AWS.
- **Enterprise Analytics API (claude.ai Enterprise).**
  - Endpoints: `/v1/organizations/analytics/{usage_report, user_usage_report, cost_report, user_cost_report, users, skills, plugins, connectors, …}` with a `read:analytics` key.
  - `group_by` ∈ {product, model, context_window (0-200k / 200k-1M), cost_type, token_type, speed, inference_geo, rbac_group_id, …}.
  - `bucket_width` 1m / 1h / 1d. Data from 2026-01-01; at most 31 days per query.
  - Skills and plugins carry `attributed_list_price` and `estimated_overage_spend`.
- **Usage & Cost Admin API.**
  - `/v1/organizations/usage_report/messages`: 1m / 1h / 1d buckets; group by model, workspace, API key, service tier, context window, `inference_geo`, speed.
  - `/v1/organizations/cost_report`: daily USD by workspace and description.
- **Cloud (Bedrock, Vertex, Foundry).** Not covered by Anthropic analytics. Use OTel, the Claude apps gateway or an LLM gateway.
- **Sources.**
  - https://platform.claude.com/docs/en/manage-claude/claude-code-analytics-api
  - https://platform.claude.com/docs/en/api/beta/organization/analytics
  - https://platform.claude.com/docs/en/build-with-claude/usage-cost-api
  - https://code.claude.com/docs/en/costs#manage-costs-for-your-organization (all accessed 2026-09-23)
- **Token Bill should:**
  - Ship **pull connectors** for all three, with cursor pagination, rate-limit backoff and 31-day chunking.
  - Store the data in a local warehouse (DuckDB or SQLite).
  - **Reconcile** OTel and transcript estimates against the Admin API or invoice. Show the drift percentage and never present client estimates as billing truth.
  - Enterprise Analytics' `context_window` and `speed` groupings give an instant org-level "long-context share" and "fast-mode share" without any client install.
- **Savings:** none directly (enabling layer). **Effort:** M. **Evidence:** strong.

### F9. Every Claude Code dollar figure is a list-price estimate unless `modelPricing` is set
- **Claim.**
  - `/usage`, the status line, the SDK's `total_cost_usd`, OTel `cost.usage` and `--max-budget-usd` all compute cost locally at list price. The exception is when the managed-only `modelPricing` setting (multiplier and/or per-model overrides, v2.1.242+; markup v2.1.271+) supplies contracted rates.
  - The SDK docs say these fields are estimates and warn not to bill users or trigger financial decisions from them. The authority is the Usage & Cost API or the Console.
  - Modifiers the estimators must handle:
    - Data residency (`inference_geo: "us"`) is **1.1×** on all token categories. Claude Code applies it from v2.1.239.
    - Fast mode: Opus 5.5 at $8/$40; Opus 5 and 4.8 at $10/$50. Cache multipliers stack on top.
    - Bedrock and Vertex regional endpoints add a 10% premium.
    - Claude Platform on AWS and Foundry bill in CCUs ($0.01 each).
- **Sources.**
  - https://code.claude.com/docs/en/settings-reference#modelpricing
  - https://code.claude.com/docs/en/agent-sdk/cost-tracking
  - https://platform.claude.com/docs/en/about-claude/pricing (all accessed 2026-09-23)
- **Token Bill should:**
  - Accept a **contract rate card** (multiplier or overrides) and **emit the matching `modelPricing` managed-settings block**, so developer-facing numbers match the bill.
  - Price every modifier: 1-hour vs 5-minute writes, fast mode, the 1.1× geo multiplier, regional premium, Batch, web search at $10 per 1,000.
  - Tag every figure with its cost basis: list, contract or invoice.
- **Savings:** none directly (trust layer). **Effort:** S. **Evidence:** strong.

### F10. On-disk transcript format: what to parse, and why a naive parser double-counts
- **Location.** `~/.claude/projects/<cwd-slug>/<sessionId>.jsonl`. Subagents are in `<sessionId>/subagents/agent-*.jsonl`, with `isSidechain: true` and `agentId`. A slug over 200 characters is truncated with a hash appended. `CLAUDE_CONFIG_DIR` / `CLAUDE_CODE_PROJECT_DIR_NAME` move the directory. Retention is **30 days** (`cleanupPeriodDays`). Anthropic says the entry format is **internal and changes between versions** and recommends `/export`, `claude -p --output-format json`, hooks' `transcript_path` or the SDK instead.
- **Fields observed on this machine** (key names only).
  - `assistant` entries: `requestId`, `message.id`, `message.model`, `message.usage` with:
    - `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`
    - `cache_creation.{ephemeral_1h_input_tokens, ephemeral_5m_input_tokens}`
    - `output_tokens`, `output_tokens_details.thinking_tokens`
    - `server_tool_use.{web_search_requests, web_fetch_requests}`
    - `speed`, `service_tier`, `inference_geo`, `iterations`
  - Also on `assistant` entries: `effort` and `perTurnEffort`; `attributionSkill` / `attributionMcpServer` / `attributionMcpTool` / `attributionPlugin`; `advisorModel`; `isApiErrorMessage`; `version`; `gitBranch`; `cwd`; `entrypoint`; `isSidechain`; `timestamp`.
  - `system` entries with `subtype`: `compact_boundary` (with `compactMetadata`), `api_error`, `model_refusal_fallback`, `stop_hook_summary`, `turn_duration`.
  - `cost-state` entries: `totalCostUSD` and per-model `modelUsage{inputTokens, outputTokens, cacheReadInputTokens, cacheCreationInputTokens, thinkingTokens, webSearchRequests, costUSD}`.
- **Duplication.** In the sample, 28,247 assistant entries carrying usage collapsed to **12,804 unique `message.id`s (2.2× duplication)**. One API response is split across several entries (thinking, text, and parallel tool_use), each repeating the same usage. ccusage de-duplicates on (message.id, requestId, sessionId), prefers non-sidechain copies, and handles `/btw` sidechain replays that re-emit parent messages with new request IDs. The Agent SDK docs warn that per-step `output_tokens` on streamed assistant messages is a **placeholder from `message_start`**, and that the authoritative output count is on the result message. On-disk entries in this sample showed realistic output counts (median 511), but the importer must not assume that.
- **Sources.**
  - https://code.claude.com/docs/en/sessions#where-transcripts-are-stored
  - https://code.claude.com/docs/en/agent-sdk/cost-tracking (accessed 2026-09-23)
  - https://github.com/ryoppippi/ccusage (`rust/adapters/claude/src/lib.rs`, main at 2026-09-23)
- **Token Bill should:**
  - Build a **versioned Claude Code transcript importer**: dedupe key (message.id, requestId, sessionId); a sidechain/subagent join via `agentId` and `parentUuid`; compaction segmentation; fixture-based golden tests per Claude Code version; and a "schema drift" warning when unknown `type` values appear.
  - Ship a **SessionEnd hook** (or managed `cleanupPeriodDays` increase) that archives transcripts centrally before the 30-day sweep. This is the only way to get per-request prefix detail without the OTel raw-body opt-in.
- **Savings:** none directly (enabling layer; avoids 2× overcount). **Effort:** M. **Evidence:** strong for the docs and ccusage code; the observed fields are one machine on v2.1.28x.

### F11. Managed-settings controls an optimizer can recommend and generate
- **Claim.** Anthropic documents these as org-enforceable policy (managed-settings file, MDM/registry, server-managed settings for Teams/Enterprise, or a `policyHelper`). Server-managed settings are polled hourly and MDM every 30 minutes. Cost-relevant keys:
  - **Model.**
    - `availableModels` + `enforceAvailableModels`, and the `ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU,FABLE}_MODEL` pins.
    - An org default model or role-based model restrictions (Enterprise admin console).
    - `fallbackModel`: each fallback costs a cold-cache turn.
    - `CLAUDE_CODE_SUBAGENT_MODEL`.
  - **Effort and thinking.**
    - `maxEffortLevel` (v2.1.267, works on every provider) and per-model `modelSettings.maxEffortLevel`.
    - Enterprise per-role effort caps.
    - `effortLevel`, `alwaysThinkingEnabled`, `MAX_THINKING_TOKENS`.
    - `ultracode` (xhigh plus workflows) and `workflowSizeGuideline` (small < 5 agents).
  - **Fast mode.** `fastModePerSessionOptIn`, `CLAUDE_CODE_DISABLE_FAST_MODE`.
  - **Context.**
    - `autoCompactWindow`, `CLAUDE_CODE_DISABLE_1M_CONTEXT`.
    - `claudeMdExcludes`.
    - `skillListingBudgetFraction` (default 1% of the window).
    - `bashOutputMaxChars` (default 30,000; range 4,000–128,000).
    - `MAX_MCP_OUTPUT_TOKENS` (warns at 10K; cap 25K).
    - `CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH` (2,048).
  - **Caching.** `promptCacheTtl`, `subagentPromptCacheTtl`.
  - **Tools.** `ENABLE_TOOL_SEARCH`, `allowedMcpServers` / `deniedMcpServers`.
  - **Hygiene.**
    - `requiredMinimumVersion` / `requiredMaximumVersion`: pin versions to avoid cache resets from upgrade churn and keep features like cache stats.
    - `autoContinueAtUsageLimit`.
    - `cleanupPeriodDays`.
    - `companyAnnouncements` for cost tips at startup.
  - **Reporting.** `modelPricing`.
  - The Claude Enterprise consumption guide recommends an org default of Sonnet, Opus restricted to specific roles, Max effort restricted for non-power users, and group-level spend caps with per-user overrides.
- **Sources.**
  - https://code.claude.com/docs/en/settings-reference
  - https://code.claude.com/docs/en/model-config
  - https://code.claude.com/docs/en/managed-settings
  - https://code.claude.com/docs/en/server-managed-settings
  - https://code.claude.com/docs/en/env-vars (all accessed 2026-09-23)
  - https://support.claude.com/en/articles/14782391-claude-enterprise-consumption-guide (updated September 2026)
- **Token Bill should:** generate a **policy pack**: a `managed-settings.json` or server-managed JSON with one entry per key, a projected monthly $ impact (from the simulators), a risk note and a rollout cohort. Emit it as a diff against the org's current managed settings. OTel `OTEL_LOG_MANAGED_SETTINGS` (v2.1.274+) can confirm which policy each session actually ran with.
- **Savings:** the sum of the levers; see F3, F5, F12–F16. **Effort:** M. **Evidence:** strong.

### F12. Model and effort mix: price spread and cache economics are large and non-obvious
- **Claim.**
  - List prices per MTok (input / output; cache read):

    | Model | Input / output | Cache read |
    |---|---|---|
    | Fable 5.1 | $10 / $50 | $0.25 (0.025×) |
    | Fable 5 | $10 / $50 | $1 |
    | Opus 5.5 | $4 / $20 | $0.20 (0.05×) |
    | Opus 5 / 4.8 | $5 / $25 | $0.50 |
    | Sonnet 5 | $2 / $10 | $0.20 |
    | Sonnet 4.6 | $3 / $15 | $0.30 |
    | Haiku 4.5 | $1 / $5 | $0.10 |

  - Claude 4.7+ and Sonnet 5 use a new tokenizer that produces about **30% more tokens** for the same text.
  - Thinking is billed as output. Default effort is `high`, except `medium` on Opus 5.5 and `xhigh` on Opus 4.7. Opus 5.5 and Fable always think.
  - `opusplan` flips between Opus and Sonnet on every plan-mode toggle, and each flip is a cache miss.
  - The **advisor tool** (for example, a Sonnet or Haiku main model with an Opus advisor) is documented as typically cheaper than running the stronger model throughout.
  - Anthropic's July 2026 guidance: smaller models for routine work; larger models can be cheaper per task on complex problems because they finish in fewer steps.
  - Bai et al.: models differ greatly in tokens per task (Kimi-K2 and Claude-Sonnet-4.5 used more than 1.5M more tokens than GPT-5 on comparable tasks), and accuracy peaks at intermediate spend.
- **Sources.**
  - https://platform.claude.com/docs/en/about-claude/pricing
  - https://code.claude.com/docs/en/model-config#adjust-effort-level
  - https://code.claude.com/docs/en/advisor
  - https://claude.com/blog/claude-model-and-effort-level-in-claude-code (2026-07-07)
  - https://arxiv.org/abs/2604.22750 (2026-04-24)
- **Token Bill should:**
  - Show the model and effort mix per developer and team, with $ per task (session, PR or commit via `lines_of_code` / `commit` / `pull_request`).
  - Run a **re-pricing counterfactual**: same tokens at another model's rates, clearly labeled "price-only; token counts would change".
  - Detect `opusplan` flip storms and effort churn.
  - Recommend org defaults (Sonnet default; Opus or Fable by role), `maxEffortLevel`, and advisor pairing.
- **Savings:** repricing Opus 5 to Sonnet 5 is 60% cheaper per token (arithmetic from list prices); realized savings depend on task mix and are unknown. **Effort:** M. **Evidence:** strong for prices, moderate for routing outcomes.

### F13. Subagents, agent teams, workflows and background activity multiply cost
- **Claim.**
  - Subagents start their own prefix. Their first request can't read the parent's cache. They default to a 5-minute TTL, and their descriptions warn above 15,000 tokens.
  - Since v2.1.198, Explore **inherits the session model** (capped at Opus on the API) instead of always running on Haiku. A user or project `Explore` agent with `model: haiku` restores the cheaper behavior.
  - `CLAUDE_CODE_SUBAGENT_MODEL` sets the default subagent, teammate and workflow model.
  - Agent teams use "approximately **7×** more tokens than standard sessions when teammates run in plan mode".
  - Workflows default to 16 concurrent agents and flag "Large workflow" above **25 agents or 1.5M projected tokens**.
  - Idle costs: scheduled `/loop` tasks, cross-session messages and `/goal` check-ins (capped at 3 idle check-ins from v2.1.246) each send full context. Background summarization is under $0.04 per session.
  - Headers identify the source of each request: `x-claude-code-agent-id` / `parent-agent-id`, and `x-claude-code-request-class` (main / subagent / workflow / compaction / auxiliary).
- **Local sample.** Subagents were **10.3%** of list-price cost across 1,549 subagent transcripts.
- **Sources.**
  - https://code.claude.com/docs/en/sub-agents
  - https://code.claude.com/docs/en/workflows
  - https://code.claude.com/docs/en/costs (agent-team and idle sections)
  - https://code.claude.com/docs/en/llm-gateway-protocol (all accessed 2026-09-23)
- **Token Bill should:** build an agent-tree attribution view (parent → subagents → nested) with $ per agent type. Flag subagents running on Opus or Fable for Explore-type work, loops that fire while idle, and teammates idling with a warm context. Recommend `CLAUDE_CODE_SUBAGENT_MODEL=haiku|sonnet`, per-agent `model:`, `workflowSizeGuideline: small`, and `subagentPromptCacheTtl`.
- **Savings:** Haiku vs Opus 5 is 80% cheaper per token (list-price arithmetic); unknown in practice. **Effort:** M. **Evidence:** strong for the docs.

### F14. Tool-output bloat and file re-reads are the main growth driver inside a session
- **Claim.**
  - Bash output returns inline up to 30,000 characters by default. MCP output warns at 10K tokens and caps at 25K.
  - Hooks can shrink what enters context:
    - PreToolUse `updatedInput`: Anthropic's own example filters test output to failures.
    - PostToolUse `updatedToolOutput`: replaces what Claude sees.
  - Research on trimming agent trajectories:
    - AgentDiet (FSE 2026): **39.9–59.7% fewer input tokens and 21.1–35.9% lower total cost** at equal task performance.
    - CORVUS (2026): 9–50% fewer input tokens by replacing stale file snapshots with current contents.
  - Community tools:
    - rtk (81.5K GitHub stars) rewrites Bash commands via a PreToolUse hook. It claims 60–90% fewer tokens on common dev commands; this is a vendor claim, not independently verified.
    - Zilliz claude-context reports a **39.4% token reduction** on 30 SWE-bench Verified tasks via semantic search.
- **Local sample.** `Read` was 31.5% of tool-result characters, browser MCP tools about 48% (image-heavy), and Bash 12.4%. **36.9% of Read calls re-read a file already read in the same session.** Datadog Agent Console also flags "file rereads" and "retry loops".
- **Sources.**
  - https://code.claude.com/docs/en/costs#offload-processing-to-hooks-and-skills
  - https://code.claude.com/docs/en/hooks (PostToolUse `updatedToolOutput`)
  - https://code.claude.com/docs/en/settings-reference#bashoutputmaxchars
  - https://code.claude.com/docs/en/mcp#mcp-output-limits-and-warnings
  - https://arxiv.org/abs/2509.23586 (v1 2025-09-28, v2 2026-03-15)
  - https://arxiv.org/abs/2607.22711 (2026-07-20)
  - https://github.com/rtk-ai/rtk (pushed 2026-09-21)
  - https://github.com/zilliztech/claude-context evaluation/README.md (pushed 2026-07-14)
  - https://www.datadoghq.com/blog/claude-code-monitoring/ (2026-06-09)
- **Token Bill should:**
  - Build a **tool-output profiler**: tokens by tool, command family (`bash_argv0` / `bash_command_class` from OTel traces), MCP server and file.
  - Count **carry cost**: a tool result's tokens × the number of later requests that re-read it. This is the real price of a large output.
  - Detect re-reads, and oversized or repeated test logs.
  - **Generate hooks** (a test-failure filter, a log grep, an output summarizer, a re-read guard) as a Claude Code plugin, and measure before and after.
- **Savings:** 20–35% of total cost in the literature (AgentDiet); vendor claims are higher. **Effort:** M–L. **Evidence:** moderate.

### F15. Fixed context overhead (CLAUDE.md, memory, skills, MCP, agent descriptions) is paid on every request
- **Claim.**
  - CLAUDE.md loads at session start. Anthropic says to keep it **under 200 lines** and move workflow instructions into skills, which load on demand.
  - Auto memory loads its first 200 lines or 25KB.
  - The skill listing is capped at 1% of the context window.
  - After compaction, invoked skill bodies are re-injected: up to 5,000 tokens each and 25,000 in total.
  - MCP tools are deferred by default, so only names and server instructions load. Tool and server descriptions are truncated at 2,048 characters. A tool marked `alwaysLoad` sits in the prefix.
  - Prefer CLIs (`gh`, `aws`, `gcloud`) over MCP where possible.
  - In monorepos, use `claudeMdExcludes`, per-package CLAUDE.md files, `worktree.sparsePaths` and code-intelligence (LSP) plugins to reduce reads.
  - Built-in Explore and Plan skip CLAUDE.md; custom agents can set `omitClaudeMd`.
  - Enterprise Analytics exposes the list-price value attributed to each skill and plugin.
- **Sources.**
  - https://code.claude.com/docs/en/costs#move-instructions-from-claudemd-to-skills
  - https://code.claude.com/docs/en/context-window
  - https://code.claude.com/docs/en/mcp#scale-with-mcp-tool-search
  - https://code.claude.com/docs/en/large-codebases
  - https://code.claude.com/docs/en/sub-agents (all accessed 2026-09-23)
- **Token Bill should:** build a **repo context linter** (`tokenbill lint-context`). Count CLAUDE.md, AGENTS.md, rules, skills and agent-description tokens with the free `count_tokens` endpoint, never char/3.7, because the Claude 4.7+ tokenizer is about 30% denser. Multiply by the org's requests per day for that repo to get "$/month of always-on context". Rank files and emit PR-ready suggestions.
- **Savings:** about 3K tokens × requests; small per request, but it multiplies across fleet traffic. Unknown until measured. **Effort:** S. **Evidence:** strong.

### F16. First-party per-developer visibility now exists, so Token Bill's value must be org-level and prescriptive
- **Claim.**
  - `/usage` (v2.1.251+) shows a `Prompt cache (main)` line with hit share, misses, likely cause (v2.1.260) and warm/cold state.
  - Plan users also see attribution by skill, subagent, plugin and MCP server; behavior flags (long context, cache misses) when one accounts for at least 10% of usage; and the heaviest `/loop` tasks.
  - `/insights` writes an HTML report of friction patterns across up to 200 sessions.
  - Status line JSON exposes `cost.*`, `context_window.*`, a `prompt_cache` object, `rate_limits.*`, `effort.level` and `fast_mode`.
  - All of it is local to one machine and one developer, and computed at list price.
- **Sources.**
  - https://code.claude.com/docs/en/costs#track-your-costs
  - https://code.claude.com/docs/en/statusline#available-data (accessed 2026-09-23)
- **Token Bill should:** not rebuild `/usage`. Instead it should:
  - Aggregate the same signals across thousands of developers.
  - Rank by recoverable dollars.
  - Generate org policy (F11).
  - Distribute a **Token Bill status line and plugin** via managed settings. It would show "this session's context tax / cache misses / recommended action" and send anonymized per-session summaries to the org store, a lighter path than full OTel.
- **Savings:** none directly (positioning). **Effort:** S–M. **Evidence:** strong.

### F17. Gateways give real-time attribution and hard caps
- **Claim.**
  - Claude Code sends `x-claude-code-session-id` on every request. It also sends `x-claude-code-agent-id` / `parent-agent-id` for subagents.
  - With `CLAUDE_CODE_GATEWAY_HINT_HEADERS=1` it adds hint headers:
    - `x-claude-code-request-class` (main / subagent / workflow / compaction / auxiliary)
    - `x-claude-code-agent-type`
    - `x-claude-code-compaction` (auto / manual / reactive)
    - `x-claude-code-context-compacted`
    - `x-claude-code-prev-tool-durations`
  - Anthropic's self-hosted **Claude apps gateway** enforces per-user, per-RBAC-group and org **daily / weekly / monthly spend caps**. It returns 429 once a cap is exceeded, warns in Claude Code at 75% and 95%, and bills aborted streams at about 4 characters per token. Unknown models are priced at $5/$25.
  - Several large enterprises reportedly use LiteLLM for spend by key.
- **Sources.**
  - https://code.claude.com/docs/en/llm-gateway-protocol
  - https://code.claude.com/docs/en/claude-apps-gateway-spend-limits
  - https://code.claude.com/docs/en/costs#cloud-providers (accessed 2026-09-23)
- **Token Bill should:**
  - Offer a **gateway mode**: a LiteLLM/Envoy plugin or standalone proxy that records a per-request canonical record with those headers, the full request body (enabling exact prefix diffing) and billed usage.
  - Provide an optional real-time policy engine for soft or hard budgets and routing.
  - Take care to pass `anthropic-*` headers and body fields through unchanged (F6).
- **Savings:** caps bound worst-case spend; unknown. **Effort:** L. **Evidence:** strong.

### F18. OpenAI Codex CLI: OTel and local rollouts, with compaction and effort controls
- **Claim.**
  - Codex OTel lives under `[otel]` in `~/.codex/config.toml` (exporter `otlp-http` / `otlp-grpc`; `log_user_prompt` opt-in).
  - Events: `codex.conversation_starts`, `codex.api_request`, `codex.sse_event` (token counts on completion), `codex.user_prompt`, `codex.tool_decision` and `codex.tool_result`.
  - Metrics include `turn.token_usage` by token type, TTFT, e2e durations and `task.compact`.
  - Cost levers:
    - `model_auto_compact_token_limit` (+ `_scope`)
    - `model_reasoning_effort` (low … max / ultra)
    - `service_tier` (fast maps to `priority`, which ccusage prices at about 2×)
    - `tool_output_token_limit`
    - `model_verbosity`
    - `history.persistence`
    - admin-enforced `requirements.toml`
  - Local rollouts are `~/.codex/sessions` (and `archived_sessions`) JSONL, with cumulative `token_count` events.
  - ChatGPT Enterprise has a Codex Analytics API (`codex.enterprise.analytics.read`, daily or weekly per-user credits and tokens). A secondary source reports a 90-day lookback and up to 12 h latency, covering ChatGPT-authenticated sessions only.
- **Sources.**
  - https://learn.chatgpt.com/docs/config-file/config-advanced
  - https://learn.chatgpt.com/docs/config-file/config-reference
  - https://learn.chatgpt.com/docs/enterprise/governance (accessed 2026-09-23)
  - https://github.com/ryoppippi/ccusage/blob/main/docs/guide/codex/index.md (main, 2026-09)
  - https://codex.danielvaughan.com/2026/05/11/codex-enterprise-analytics-compliance-apis-governance-dashboards/ (2026-05-11, updated 2026-09-23; secondary)
- **Token Bill should:** add a Codex adapter (rollout JSONL deltas and OTel) mapped onto the canonical record: `cached_input` becomes cache_read, and reasoning is split out. Apply the same compaction and effort counterfactuals using Codex's `requirements.toml` keys.
- **Savings:** unknown. **Effort:** M. **Evidence:** moderate. The official docs are thin on attribute names, and the analytics limits come from a secondary source.

### F19. Gemini CLI: documented OTel token metrics including cache, thoughts and compression
- **Claim.**
  - `gemini_cli.api_response` carries `input_token_count`, `output_token_count`, `cached_content_token_count`, `thoughts_token_count`, `tool_token_count` and `total_token_count`.
  - The metric `gemini_cli.token.usage` has `type` ∈ {input, output, thought, cache, tool}.
  - `gemini_cli.chat_compression` logs `tokens_before` / `tokens_after`.
  - GenAI semantic-convention metrics: `gen_ai.client.token.usage`.
  - Local chats are in `~/.gemini/tmp/*/chats/*.json[l]`. Gemini's `input` includes cached tokens, which must be subtracted.
- **Sources.**
  - https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/telemetry.md (last commit 2026-06-18)
  - https://github.com/ryoppippi/ccusage/blob/main/docs/guide/gemini/index.md
- **Token Bill should:** add a Gemini adapter. Use `chat_compression` before/after counts to measure compaction savings directly. Normalize the OTel GenAI semconv fields.
- **Savings:** unknown. **Effort:** S. **Evidence:** strong.

### F20. GitHub Copilot moved to token-based "AI Credits"
- **Claim.**
  - From **2026-06-01**, Copilot replaced premium requests with **GitHub AI Credits** computed from input, output and **cached** tokens at per-model API rates.
  - Plans include credits equal to the seat price: Business $19 and Enterprise $39, with promotional bonus credits through August 2026.
  - Budgets can be set at enterprise, cost-center and user level. Completions and Next Edit suggestions stay free.
  - Since **2026-06-19**, the Copilot usage metrics API reports `ai_credits_used` per user per day. It is **not broken down by model, feature or surface**, and it is a metrics signal, not a billed total.
  - Copilot CLI writes `~/.copilot/session-state/*/events.jsonl` (cumulative `session.shutdown` model metrics) and optional OTel JSONL.
- **Sources.**
  - https://github.blog/news-insights/company-news/github-copilot-is-moving-to-usage-based-billing/ (effective 2026-06-01)
  - https://github.blog/changelog/2026-06-19-ai-credits-consumed-per-user-now-in-the-copilot-usage-metrics-api/ (2026-06-19)
  - https://github.com/ryoppippi/ccusage/blob/main/docs/guide/copilot/index.md
- **Token Bill should:** pull `ai_credits_used` and the Billing Usage API for org totals. Use Copilot CLI local and OTel files for model-level detail. Show "credits burn vs included allotment" per user and cost center. Because Copilot now bills cached tokens, the same cache and context detectors apply.
- **Savings:** unknown. **Effort:** M. **Evidence:** strong.

### F21. Cursor: a per-request usage-events API with cache tokens and spend limits
- **Claim.**
  - Cursor Admin API endpoints:
    - `/teams/filtered-usage-events` returns per-request `inputTokens`, `outputTokens`, `cacheReadTokens`, `cacheWriteTokens`, `totalCents`, `chargedCents`, `cursorTokenFee` ($0.25/MTok on third-party models for Teams and Enterprise), `model`, `kind` and `maxMode`.
    - `/teams/daily-usage-data` and `/teams/spend`.
    - `/teams/user-spend-limit(s)` to set per-user monthly limits.
  - Limits: date range at most 30 days; 20 or 60 requests per minute; data aggregated hourly.
  - ccusage does **not** support Cursor, which is an opening for Token Bill.
- **Sources.**
  - https://cursor.com/docs/account/teams/admin-api (accessed 2026-09-23)
  - https://github.com/ryoppippi/ccusage (supported-sources table)
- **Token Bill should:** add a Cursor connector. Detect `maxMode` overuse, expensive models on trivial requests, and low cache-read ratios. Recommend per-user spend limits through the same API.
- **Savings:** unknown. **Effort:** S–M. **Evidence:** strong.

### F22. Competitive landscape: plenty of dashboards, few prescriptive optimizers
- **ccusage** (18.7K stars; v20.0.24 on 2026-09-21).
  - Does: local-only reports (daily / weekly / monthly / session / 5-hour blocks); a status line; 18 agent sources including Codex, Gemini CLI and Copilot CLI; LiteLLM pricing with overrides; careful dedupe.
  - Doesn't: optimization, counterfactuals, org rollup or Cursor.
  - Its sponsor Lineman.io markets "40% lower token usage" for Claude Code teams (vendor claim).
- **Claude-Code-Usage-Monitor** (8.7K stars; last push 2026-07-05). Real-time burn rate, P90 limit prediction and plan limits. Individual only.
- **Anthropic claude-code-monitoring-guide and Grafana/Prometheus dashboards.** Cost, tokens, cache ratio and leaderboards. No recommendations.
- **Datadog Agent Console** (2026-06-09). Covers Claude Code, Copilot, Cursor and more. Cost by agent, team and user; DORA-style impact; **detects skipped checks, retry loops and file rereads**; a Fix Library (for example, PreToolUse hooks); estimated monthly savings per repo. The closest competitor.
- **Dash0** ($10 per developer per month). Cost and adoption by model, team and developer; MCP tracking.
- **Honeycomb, SigNoz, CloudWatch.** OTel dashboards.
- **Sources.**
  - https://github.com/ryoppippi/ccusage
  - https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor
  - https://www.datadoghq.com/blog/claude-code-monitoring/ (2026-06-09)
  - https://www.dash0.com/comparisons/claude-code-monitoring-tools (2026-08-19)
  - https://github.com/anthropics/claude-code-monitoring-guide (ROI guide; last push 2025-07-29)
- **Token Bill should differentiate on:**
  1. Cache-rule-accurate counterfactual replay: compaction window, TTL, model and effort, subagent model, tool caps, with dollars per policy.
  2. Byte-level prefix diffing to explain *why* the cache broke (already Token Bill's core).
  3. Generated, versioned managed-settings policy packs and hooks, with A/B verification.
  4. Invoice reconciliation.
  5. Open-source and local-first, so it passes enterprise security review more easily than SaaS.
  6. FOCUS export (F24).
- **Savings:** none directly (positioning). **Effort:** none (analysis only). **Evidence:** moderate.

### F23. Token spend is highly variable and badly predicted by the models; specification quality matters
- **Claim.**
  - The same task can differ by **up to 30× in total tokens** across runs. Frontier models "systematically underestimate" their own token cost (correlation at most 0.39), and accuracy saturates at higher spend (Bai et al., 2026).
  - Cutting a full task spec down to a bare user story **raises token spend by 29.7%**, with task-type sensitivity from 13% to 115%. A probe-based predictor lands within 36% on new tasks (Smékal, 2026-08-26).
  - Anthropic's cost page says the same: vague prompts trigger broad scanning. Use plan mode and course-correct early. The best-practices page says to `/clear` after correcting Claude more than twice on the same issue.
- **Sources.**
  - https://arxiv.org/abs/2604.22750 (2026-04-24)
  - https://arxiv.org/abs/2608.25399 (2026-08-26)
  - https://code.claude.com/docs/en/costs#write-specific-prompts
  - https://code.claude.com/docs/en/best-practices
- **Token Bill should:**
  - Use **variance-aware baselines** (medians and bootstrap intervals, not means) when claiming savings, and require enough sessions per cohort.
  - Normalize cost per outcome ($ per merged PR or commit, using OTel `commit` / `pull_request` counts or the analytics APIs).
  - Add behavioral detectors: correction loops (repeated user corrections in one session), vague-first-prompt sessions with high exploration read volume, and long sessions that were never cleared.
  - Deliver coaching nudges through `companyAnnouncements` or the status line.
- **Savings:** up to about 30% on under-specified tasks, per Smékal. **Effort:** M. **Evidence:** moderate (single-author preprint and a benchmark setting).

### F24. FinOps standardization: Tokenomics Foundation and FOCUS
- **Claim.**
  - The Linux Foundation launched the **Tokenomics Foundation** on **2026-08-04** with 30 founding members, including JPMorganChase, IBM, SAP, Vantage and Finout. It is chartered to standardize token-economics definitions and cost-to-serve methods, and to improve AI cost reporting in **FOCUS v1.5 and beyond**.
  - FOCUS v1.3 already has `ConsumedQuantity`, `ConsumedUnit`, `PricingQuantity`, `PricingUnit`, `ListCost`, `EffectiveCost`, `BilledCost`, `ServiceCategory`, `SkuId` and `Tags`. Enterprises' FinOps teams ingest FOCUS.
- **Sources.**
  - https://www.linuxfoundation.org/press/linux-foundation-launches-the-tokenomics-foundation-to-define-the-economics-and-roi-of-ai-value (2026-08-04)
  - https://focus.finops.org/docs/specification/v1-3/columns (accessed 2026-09-23)
- **Token Bill should:** export **FOCUS-shaped rows**: one row per (day, user or cost center, agent, model, token type), with ConsumedUnit = tokens, ListCost at list rates, EffectiveCost at contract rates, and Tags for team, repo and agent. Track FOCUS 1.5's token schema. This is what lets the enterprise's FinOps platform consume Token Bill's output.
- **Savings:** none directly (adoption enabler). **Effort:** S. **Evidence:** strong.

### F25. Billing path matters as much as tokens: seat allowance, usage credits and per-token API diverge widely
- **Claim.**
  - Team and Enterprise seats draw on a per-seat allowance with 5-hour and weekly windows. Past it, usage credits apply, with org, group or member spend limits. Console and cloud usage bills per token.
  - "Usage inside the seat allowance isn't metered in dollars." The spend-report CSV covers usage-credit spend only.
  - A Quesma analysis (August 2026) reports **12× to 40×** differences between subscription and API-equivalent cost for the same usage. For example, SemiAnalysis measured about $8,000 of API-equivalent usage against $200 of subscription spend.
- **Sources.**
  - https://code.claude.com/docs/en/costs#claude-for-teams-and-enterprise (accessed 2026-09-23)
  - https://quesma.com/blog/claude-code-pricing-for-enterprise/ (August 2026)
- **Token Bill should:** add a per-developer **billing-path optimizer**:
  - Compute each developer's list-price-equivalent monthly usage.
  - Compare seat tiers (Standard or Premium) plus usage credits against per-token API/Console.
  - Recommend moving heavy users to seats and light users off Premium.
  - Recommend usage-credit caps per group.
  - Keep the analysis at list price, labeled clearly, because contract terms vary.
- **Savings:** potentially the largest single lever for heavy users, per the blog's 12–40×, but contract-dependent and unknown. **Effort:** M. **Evidence:** moderate for the blog, strong for the Anthropic mechanics.

### F26. Gaps in Token Bill v0.1.2 for Claude Code
- **Claim.** Against the current pricing page, `tokenbill/pricing.py` is missing:
  - **Claude Opus 5.5** ($4/$20, cache hits 0.05×).
  - **1-hour cache writes** (2× input). This is the dominant write type in Claude Code subscription sessions: in the sample, 28,119 of the 28,146 usage entries with cache writes were 1-hour writes.
  - Fast-mode rates and the `speed` field.
  - The `inference_geo` 1.1× multiplier and CCU billing.
  - Fable 5's cache read already uses the default 0.1× and is fine.
- **Other gaps.**
  - The **char/3.7 token attribution** is miscalibrated for the Claude 4.7+ tokenizer, which is about 30% denser. `count_tokens` is free and rate-limited separately, so use it when available.
  - The simulator models one breakpoint at the end of messages with a 5-minute TTL. Claude Code uses 1-hour TTLs for the main thread on subscriptions, 5-minute TTLs for subagents, and mid-conversation cached system blocks.
- **Sources.**
  - https://platform.claude.com/docs/en/about-claude/pricing
  - https://platform.claude.com/docs/en/build-with-claude/token-counting (accessed 2026-09-23)
  - Local repo `tokenbill/pricing.py`
- **Token Bill should:** add a pricing schema v2 with per-TTL write multipliers, per-model read multipliers, a speed tier, a geo multiplier and a source/verified date per row. Add a CI check that diffs against the pricing page. Add a `count_tokens` calibration mode.
- **Savings:** none directly (accuracy; 1-hour writes are about 28% of cost in the sample). **Effort:** S. **Evidence:** strong.

---

## 3. What an org-wide Claude Code cost optimizer must ingest

| Source | Population covered | Granularity | Carries | Gaps |
|---|---|---|---|---|
| Claude Code OTel metrics | Every provider (incl. Bedrock, Vertex, Foundry) | Per session, model, query_source, agent, skill, plugin, MCP server | tokens by type, cost (list or `modelPricing`), LOC, commits, PRs, active time | no per-request prefix detail; needs a collector |
| OTel events: `api_request`, `tool_result`, `assistant_response` | same | per request / tool call | tokens incl. cache, duration, ttft, effort, speed, request_id | — |
| OTel `api_request_body` (opt-in, v2.1.274+) | same | per request | full request JSON, enabling exact prefix diffs | sensitive; opt-in; large |
| OTel traces (beta) | same | per interaction → llm_request → tool | tool `result_tokens`, agent tree, bash_argv0 | beta; attributes still changing |
| Local transcripts `~/.claude/projects` | every surface on that machine | per API response (dedupe needed) | usage incl. 1h/5m writes, thinking, speed, geo; compaction metadata; subagent tree; effort; attribution fields | internal format, changes per version; 30-day retention; no central copy |
| Hooks (`transcript_path`, SessionEnd, PreCompact/PostCompact, PreModelSwitch, PostToolUse) | same | event | trigger points to archive, measure or enforce | install via managed settings / plugin |
| Status line JSON | same | live | cost, context_window, prompt_cache, rate_limits, effort | UI only; list price |
| Claude Code Analytics API (Admin key) | Console/API orgs | user × day × model | tokens, estimated cost, LOC, commits, PRs, tool accept/reject | daily; up to 1h lag; not on Claude Platform on AWS |
| Enterprise Analytics API (`read:analytics`) | claude.ai Enterprise seats | user × product × model × context-window × speed × token-type; 1m/1h/1d | cost and usage, skills and plugins attributed list price | from 2026-01-01; 31-day windows |
| Usage & Cost API | API orgs | 1m/1h/1d; model, workspace, key, tier, context window, geo, speed | authoritative usage and cost | no per-user identity for Claude Code except via workspace/key |
| Gateway (LiteLLM / Claude apps gateway / custom) | whatever routes through it | per request | headers: session, agent, request-class, compaction; full body; billed usage; caps | must not strip `cache_control` / `anthropic-beta` |
| Codex, Gemini CLI, Copilot CLI local + OTel; Cursor Admin API; Copilot metrics API | other agents | varies | tokens incl. cached and reasoning; credits | formats evolve (ccusage marks them beta) |

---

## 4. Canonical record (proposal)

One normalized record per API call, across all agents:

```
call_id, request_id, agent_product (claude_code|codex|gemini_cli|copilot|cursor|api),
agent_version, org_id, user_id/email_hash, team/cost_center (resource attrs), repo (vcs.*),
session_id, parent_session_id, agent_id, parent_agent_id, request_class (main|subagent|workflow|compaction|auxiliary),
ts_start, duration_ms, ttft_ms, model, provider (anthropic|bedrock|vertex|foundry|gateway), speed, effort, inference_geo, service_tier,
input_uncached, cache_read, cache_write_5m, cache_write_1h, output, thinking, server_tool_calls{web_search,...},
context_total = input_uncached+cache_read+cache_write_*,
cost_list, cost_contract, cost_basis, source (otel|transcript|analytics_api|gateway), dedupe_key,
compaction{trigger, pre, post}, tool_results[{tool, mcp_server, bash_argv0, result_tokens}], breaker_cause
```

Derived per session: requests, peak and median context, cost-weighted context, rebuild events with causes, compactions, idle gaps (for TTL), and outcome joins (commits, PRs, LOC).

---

## 5. Detector catalog for Claude Code (to add to `breakers.py` and the analyzer)

| ID | Detects | Signal / formula | Fix emitted | Source finding |
|---|---|---|---|---|
| CC-CTX-TAX | Long-context tax | cost share of requests with context > W; simulate W ∈ {150K, 200K, 300K, 500K} | `autoCompactWindow`, `/clear` habits, `CLAUDE_CODE_DISABLE_1M_CONTEXT` | F2, F3 |
| CC-REBUILD-* | Cache rebuild by cause | write > 50% of prefix and > 2K tokens; cause = model / effort / fast / version / cwd / TTL-expiry / compaction / MCP / unknown | cause-specific | F4 |
| CC-TTL | Wrong TTL | gap distribution vs 5m/1h breakeven, per bucket | `promptCacheTtl`, `subagentPromptCacheTtl` | F5 |
| CC-GW-NOCACHE | Gateway strips caching | multi-turn sessions with cache_read ≈ 0 | forward `cache_control` and `anthropic-beta`; `ENABLE_TOOL_SEARCH` | F6 |
| CC-MODEL-MIX | Expensive model on routine work | $ per task by model; Opus/Fable share on sessions with small diffs | `availableModels`, default model, advisor | F12 |
| CC-EFFORT | High or xhigh/max effort share | thinking tokens / output; effort attr | `maxEffortLevel` | F12 |
| CC-FAST | Fast mode enabled mid-session | speed flips after turn 1 | `fastModePerSessionOptIn` or disable | F4, F9 |
| CC-SUBAGENT | Subagents on top-tier models / Explore inheriting Opus | agent tree × model | `CLAUDE_CODE_SUBAGENT_MODEL`, agent `model:` | F13 |
| CC-TEAM-IDLE | Agent teammates or loops burning while idle | requests with no user prompt in window; loop cadence | shut down teammates; loop limits | F13 |
| CC-TOOL-BLOAT | Tool output carry cost | result_tokens × subsequent requests | hooks (filter / summarize), `bashOutputMaxChars`, `MAX_MCP_OUTPUT_TOKENS` | F14 |
| CC-REREAD | Same file re-read | Read(path) count > 1 per session (37% in sample) | re-read guard hook, code-intelligence plugin | F14 |
| CC-FIXED-CTX | Always-on context overhead | count_tokens(CLAUDE.md + rules + skill listing + agent descriptions) × requests | trim CLAUDE.md < 200 lines, skills, `claudeMdExcludes` | F15 |
| CC-COLD-RESUME | Resume after TTL of a huge session | first request after gap > TTL with context > X | "resume from summary", `/clear` | F4, F13 |
| CC-CORRECTION-LOOP | Accumulated failed attempts | ≥ 3 user corrections in one session (heuristic) | `/clear` + better spec | F23 |
| CC-BILLING-PATH | Seat vs API mismatch | list-equivalent monthly usage per developer | seat tier / usage-credit caps | F25 |

---

## 6. What Token Bill should build

### P0: make it true for Claude Code (4–6 weeks)
1. **Pricing v2 (F26, F9).** Opus 5.5; 1-hour and 5-minute write multipliers; per-model read multipliers; fast mode; geo 1.1×; regional premium; contract rate card that emits a `modelPricing` block; `cost_basis` on every figure; a CI job that diffs against the pricing page.
2. **Claude Code transcript importer (F10).** Dedupe on (message.id, requestId, sessionId); subagent tree; compaction segments; `cost-state` cross-check; schema-drift warnings; golden fixtures per Claude Code version. `tokenbill import claude-code ~/.claude/projects`.
3. **Claude Code breaker causes (F4)** and **long-context tax (F2)** in the existing HTML report, with dollars per cause.
4. **Compaction-window and TTL counterfactual simulators (F3, F5)**, reusing the existing simulator with a 1-hour mode.
5. **Gateway/no-cache and tool-search sanity check (F6)** as a P0 alarm.

### P1: org scale (6–10 weeks)
6. **Ingestion service.** OTLP receiver or collector exporter (F7); Claude Code Analytics, Enterprise Analytics and Usage & Cost API connectors (F8); DuckDB or Postgres store; invoice reconciliation with drift percentage.
7. **Policy-pack generator (F11)** with projected $ per key, rollout cohorts, and verification via `OTEL_LOG_MANAGED_SETTINGS`.
8. **Claude Code plugin distributed via managed settings (F14–F16).** SessionEnd archiver; the Token Bill status line; generated PreToolUse/PostToolUse output filters and a re-read guard; a PreModelSwitch hook that shows rebuild cost; compact instructions.
9. **Verification harness (F23).** Before/after cohorts, bootstrap confidence intervals on $ per developer-day and $ per PR. Never claim savings without an interval.
10. **FOCUS export (F24).**

### P2: multi-agent and real-time (10+ weeks)
11. Adapters for Codex, Gemini CLI, Copilot (metrics and billing APIs, CLI files) and Cursor (Admin API), all normalized into the canonical record (F18–F21).
12. **Gateway mode (F17).** A LiteLLM/Envoy plugin for exact per-request capture, prefix diffing and optional budget enforcement and routing.
13. **Repo context linter (F15)** and **billing-path optimizer (F25)**.
14. **Behavioral coaching (F23).** Correction loops, vague specs, uncleared sessions, delivered via `companyAnnouncements` or the status line.

### Enterprise-readiness requirements seen across sources
- Privacy by default: work with redacted OTel (no prompts), hash emails, and make `api_request_body` ingestion an explicit opt-in.
- Label every dollar figure as list, contract or invoice.
- Handle multiple providers (Anthropic API, Bedrock, Vertex, Foundry, gateways): Anthropic's own analytics don't cover cloud providers.
- Stay version-aware: Claude Code ships features behind v2.1.x gates weekly.

---

## 7. Open questions

- Anthropic does not document whether on-disk transcript `output_tokens` is always final or can be the `message_start` placeholder described for SDK stream messages. The local sample looked final. This needs version-matrix testing.
- How often do real enterprise gateways strip `cache_control` or disable tool search? There is no public prevalence data; field telemetry would be needed.
- The quality impact of lowering `autoCompactWindow`, for example to 200–300K, on real coding outcomes has no published A/B. It needs a pilot with outcome metrics (PR merge rate, rework).
- Whether Opus 5.5 / Fable 5.1 effort changes keep the cache on Bedrock or Vertex (documented as no) and on Anthropic-hosted Foundry.
- The Codex Analytics API's exact fields, lookback and latency come from a secondary source; they need confirming in official docs.
- The Copilot `ai_credits_used` field has no model or surface breakdown yet, which limits model-mix recommendations for Copilot.
- The Claude apps gateway doesn't support the 1-hour TTL: for gateway-routed API fleets, is 5 minutes plus keep-alive cheaper?
- Vendor claims (rtk 60–90%, Lineman 40%) are unverified. Token Bill's verification harness could benchmark them independently, which would also be good marketing.

---

## 8. Sources (all opened; living docs accessed 2026-09-23)

**Anthropic, Claude Code docs** (living; version gates up to about v2.1.280):
- https://code.claude.com/docs/en/costs
- https://code.claude.com/docs/en/monitoring-usage
- https://code.claude.com/docs/en/prompt-caching
- https://code.claude.com/docs/en/model-config
- https://code.claude.com/docs/en/settings-reference
- https://code.claude.com/docs/en/env-vars
- https://code.claude.com/docs/en/sessions
- https://code.claude.com/docs/en/context-window
- https://code.claude.com/docs/en/statusline
- https://code.claude.com/docs/en/hooks
- https://code.claude.com/docs/en/mcp
- https://code.claude.com/docs/en/sub-agents
- https://code.claude.com/docs/en/workflows
- https://code.claude.com/docs/en/agent-teams
- https://code.claude.com/docs/en/fast-mode
- https://code.claude.com/docs/en/advisor
- https://code.claude.com/docs/en/analytics
- https://code.claude.com/docs/en/managed-settings
- https://code.claude.com/docs/en/server-managed-settings
- https://code.claude.com/docs/en/llm-gateway-protocol
- https://code.claude.com/docs/en/claude-apps-gateway-spend-limits
- https://code.claude.com/docs/en/agent-sdk/cost-tracking
- https://code.claude.com/docs/en/best-practices
- https://code.claude.com/docs/en/large-codebases

**Anthropic, platform docs** (living):
- https://platform.claude.com/docs/en/about-claude/pricing
- https://platform.claude.com/docs/en/manage-claude/claude-code-analytics-api
- https://platform.claude.com/docs/en/build-with-claude/usage-cost-api
- https://platform.claude.com/docs/en/api/beta/organization/analytics
- https://platform.claude.com/docs/en/build-with-claude/token-counting

**Anthropic, blog and support:**
- https://claude.dev/blog/lessons-from-building-claude-code-prompt-caching-is-everything/ (2026-04-30)
- https://claude.com/blog/claude-model-and-effort-level-in-claude-code (2026-07-07)
- https://support.claude.com/en/articles/14782391-claude-enterprise-consumption-guide (updated September 2026)

**Other agents:**
- https://learn.chatgpt.com/docs/config-file/config-advanced
- https://learn.chatgpt.com/docs/config-file/config-reference
- https://learn.chatgpt.com/docs/enterprise/governance
- https://codex.danielvaughan.com/2026/05/11/codex-enterprise-analytics-compliance-apis-governance-dashboards/ (2026-05-11, updated 2026-09-23; secondary)
- https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/telemetry.md (2026-06-18)
- https://github.blog/news-insights/company-news/github-copilot-is-moving-to-usage-based-billing/ (effective 2026-06-01)
- https://github.blog/changelog/2026-06-19-ai-credits-consumed-per-user-now-in-the-copilot-usage-metrics-api/ (2026-06-19)
- https://cursor.com/docs/account/teams/admin-api

**Community and competitors:**
- https://github.com/ryoppippi/ccusage (v20.0.24, 2026-09-21), including docs/guide/{claude, codex, gemini, copilot, cost-modes}.md and rust/adapters/claude/src/lib.rs
- https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor (2026-07-05)
- https://github.com/rtk-ai/rtk (2026-09-21)
- https://github.com/zilliztech/claude-context (evaluation/README.md; 2026-07-14)
- https://github.com/anthropics/claude-code-monitoring-guide (ROI guide; last push 2025-07-29)
- https://www.datadoghq.com/blog/claude-code-monitoring/ (2026-06-09)
- https://www.dash0.com/comparisons/claude-code-monitoring-tools (2026-08-19)

**Research:**
- https://arxiv.org/abs/2604.22750 (Bai et al., 2026-04-24)
- https://arxiv.org/abs/2608.25399 (Smékal, 2026-08-26)
- https://arxiv.org/abs/2601.14470 (Salim et al., 2026-01-20)
- https://arxiv.org/abs/2509.23586 (Xiao et al., AgentDiet, FSE 2026; v2 2026-03-15)
- https://arxiv.org/abs/2510.00615 (Kang et al., ACON; v3 2026-06-01)
- https://arxiv.org/abs/2607.22711 (Zheng et al., CORVUS, 2026-07-20)
- https://www.trychroma.com/research/context-rot (2025-07-14)

**News and standards:**
- https://techcrunch.com/2026/06/05/the-token-bill-comes-due-inside-the-industry-scramble-to-manage-ais-runaway-costs/ (2026-06-05)
- https://quesma.com/blog/claude-code-pricing-for-enterprise/ (August 2026)
- https://www.linuxfoundation.org/press/linux-foundation-launches-the-tokenomics-foundation-to-define-the-economics-and-roi-of-ai-value (2026-08-04)
- https://focus.finops.org/docs/specification/v1-3/columns

---

## Appendix A: how the local sample numbers were computed (reproducible)

- **Files:** `~/.claude/projects/*/*.jsonl` (main) plus `~/.claude/projects/*/*/subagents/*.jsonl`. Only `type == "assistant"` entries with `message.usage`.
- **De-duplication:** keep the last entry per (file, message.id, requestId).
- **Cost:** `input × in + ephemeral_5m × 1.25·in + ephemeral_1h × 2·in + cache_read × read_rate + output × out`, using pricing-page list rates per model. `<synthetic>` models were excluded.
- **Context per request:** `input + cache_read + cache_creation`.
- **"Full-rebuild-like":** context > 50K and cache_creation > 0.5 × context.
- **Re-reads:** repeated `Read` tool_use `file_path` within one transcript file.
- **Tool-output shares:** characters of `tool_result` content, joined by tool_use_id. Images inflate characters.
- **Results:** 18,180 requests; 55 active days; list-equivalent total about $7.27K; subagents 10.3%; 20 compactions (all auto, median preTokens 993,070).
