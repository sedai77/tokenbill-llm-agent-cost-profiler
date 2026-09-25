# Fact-check: research track "claude-code" (claude-code.md)

Verifier pass on 2026-09-23. I checked every finding against its primary source. Official Claude Code and platform docs were downloaded fresh as raw markdown (`code.claude.com/docs/en/<page>.md`, `platform.claude.com/docs/en/<page>.md`) into `scratchpad/research/vcc/`. arXiv abstracts came from export.arxiv.org. News, blog and vendor pages were fetched with curl and WebFetch. GitHub metadata came from `gh api`. I re-ran every local-sample number with independent scripts (`vcc/local_verify.py`, `vcc/local_verify_glob.py`, `vcc/local_verify2.py`); outputs are in `vcc/*.out`. I did not modify the repo.

## Verdict summary

| ID | Verdict | One-line reason |
|---|---|---|
| cc-enterprise-baseline | corrected | The 18.6× figure is Jellyfish's, not Faros AI's. Everything else checks out. |
| cc-context-reread-dominates | corrected | Docs and papers confirmed. The local shares only reproduce on main + direct-subagent transcripts; the 1,297 workflow-agent transcripts (37% of cost) were left out. |
| cc-autocompact-window | corrected | Docs confirmed. The true median of preTokens is 986,666; 993,070 is the upper-middle value. |
| cc-cache-breakers | corrected | Docs and blog confirmed. The local 1.7% / 24% becomes 1.3% / 17% on the full sample. Two causes are overstated. |
| cc-ttl-policy | confirmed | TTL table, controls, precedence and prices all match. |
| cc-gateway-cache-strip | confirmed | Docs say this nearly verbatim. |
| cc-otel-schema | corrected | TTFT is on the beta `llm_request` span, not the `api_request` event. The raw-body events aren't gated at v2.1.274; only their pairing IDs are. Inline bodies are truncated at 60 KB. |
| cc-admin-analytics-apis | corrected | List-price attribution exists on the skills endpoint only; the plugins endpoint has no price fields. |
| cc-list-price-estimates | confirmed | modelPricing (v2.1.242, markup v2.1.271), the SDK warning, 1.1×, fast-mode prices, +10% regional and CCU at $0.01 all verified. |
| cc-transcript-format | confirmed | Fields and dedupe reproduce (main thread 28,271 → 12,817, 2.2×). ccusage dedupe key confirmed in its source. |
| cc-managed-settings-levers | corrected | All keys exist, but two aren't enforceable: `workflowSizeGuideline` is advisory, and `--autocompact` overrides managed `autoCompactWindow`. |
| cc-model-effort-mix | confirmed | Prices, tokenizer note, effort defaults, opusplan, advisor and the July 7 blog all verified. |
| cc-subagents-teams-workflows | corrected | Docs confirmed. The local "subagents 10.3%" excludes workflow agents; all non-main agents were about 44% of cost. |
| cc-tool-output-bloat | corrected | Papers, hooks, limits and rtk confirmed. Read 31.5% and 36.9% re-reads are main-thread-only figures. The claude-context README date is wrong. |
| cc-fixed-context-overhead | confirmed | Every number matches the docs. |
| cc-first-party-visibility | confirmed | /usage, /insights and status line fields verified. |
| cc-gateway-attribution-caps | confirmed | Headers and spend-limit behavior verified (minor nuances noted). |
| codex-cli-telemetry | corrected | OTel and config keys confirmed. Per-user credits/tokens by day or week is supported only by the secondary source. |
| gemini-cli-telemetry | confirmed | Every attribute name is present in telemetry.md (last commit 2026-06-18). |
| copilot-ai-credits | confirmed | Blog (published 2026-04-27, effective 2026-06-01) and 2026-06-19 changelog verified. |
| cursor-admin-api | confirmed | Fields, the $0.25/MTok Cursor Token Rate, limits and the spend-limit endpoints all verified. |
| competitive-landscape | confirmed | Star counts, versions, the Datadog features, Dash0's $10 and the Lineman claim all verified. |
| research-variance-spec | confirmed | Bai et al. and Smékal abstracts match exactly. |
| research-context-compression | corrected | The headline "26–60%" contradicts CORVUS's 9% low end. ACON's success claim is relative to other compression baselines. |
| finops-focus-tokenomics | confirmed | LF release (2026-08-04, "30 initial members"; 28 named) and FOCUS 1.3 columns verified. |
| billing-path-optimizer | corrected | 12–40× compares individual Max plans with API-equivalent cost. The only org-level figures in the article are 3.5× and "roughly 3×". |
| tokenbill-pricing-gaps | corrected | The code gaps are confirmed. The "28,119 of 28,146" 1-hour share covers the main thread only; char/3.7 is mostly rescaled to billed totals. |

## Critical methodology issue (affects F2, F3, F4, F13, F14 and F26 local numbers)

`~/.claude/projects` on this machine holds three kinds of transcript:

| Kind | Files | Share of list-price cost |
|---|---|---|
| Main (`<proj>/<uuid>.jsonl`) | 29 | 56.0% |
| Direct subagents (`<uuid>/subagents/agent-*.jsonl`) | 264 | 6.5% |
| Workflow agents (`<uuid>/subagents/workflows/wf_*/agent-*.jsonl`) | 1,297 | 37.4% |

The file counts are as of this pass; the report counted 1,577 files. The report's Appendix A glob (`*/*/subagents/*.jsonl`) matches only the 264 direct subagents. Yet §1 says the sample covers 1,549 subagent files. With that glob I reproduce the report's numbers almost exactly:

- cost split: reads 60.5%, writes 31.0%, 1-hour writes 27.8%, output 8.2%, uncached input 0.2%
- context > 100K / 200K / 400K / 800K: 94.4 / 85.1 / 69.9 / 27.1% of cost
- rebuild-like requests: 1.72% of requests, 24.2% of cost
- subagents: 10.5% of cost
- total: $7,282, or $134.85 per active day

On all 1,590 transcripts (43,603 priced requests, $11,639):

- cost split: reads 55.2%, writes 30.1%, output 14.5%, uncached input 0.2%
- median context 154K, p90 590K
- context > 100K / 200K / 400K / 800K: 85.0 / 64.9 / 45.6 / 17.0% of cost
- rebuild-like requests: 1.34% of requests, 17.4% of cost
- non-main agents: 43.9% of cost
- $215.53 per active day

The qualitative conclusions still hold. The quoted percentages are the main-thread-heavy view and should say so. Separately, the tool-output figures (Read 31.5% of characters, 36.9% re-reads) come from the 29 main transcripts only (I get 31.5% and 36.5% there).

## Per-finding notes

**cc-enterprise-baseline (corrected).**
- costs.md line 11 says, verbatim, "$13 per developer per active day and $150-250 per developer per month … below $30 per active day for 90% of users".
- The TPM/RPM table is present: 1–5 users get 200k–300k TPM, 500+ users get 10k–15k TPM.
- TechCrunch (Rebecca Bellan, 2026-06-05): "Uber blew through its entire 2026 AI coding budget by April."
- The 18.6× figure belongs to Nicholas Arcolano of **Jellyfish**: "per-developer consumption rising about 18.6x in nine months". The article mentions Faros AI only for a separate two-year study of 20,000 developers (bugs and rewrites rising).
- Correction: attribute 18.6× to Jellyfish only.

**cc-context-reread-dominates (corrected).**
- costs.md "Why usage climbs" says "sends your full conversation with every request".
- arXiv 2604.22750 (Bai, Huang, Wang, Sun, Mihalcea, Brynjolfsson, Pentland, Pei; v1 2026-04-24) says "1000x more tokens than code reasoning and code chat, with input tokens … driving the overall cost".
- arXiv 2601.14470 (Salim et al., 2026-01-20) reports 53.9% input and 59.4% Code Review. Note that this is ChatDev with GPT-5, not Claude Code.
- The local numbers reproduce only on main + direct subagents (see the methodology section). On the full sample: reads 55.2%, writes 30.1%, output 14.5%; median context 154K; > 200K context is 64.9% of cost.

**cc-autocompact-window (corrected, minor).**
- model-config.md lines 681 and 715 say "at about 967K tokens by default" for "Sonnet 5, the Fable models, and Opus 4.7 and later on the Anthropic API".
- settings-reference confirms `autoCompactWindow` ranges 100000–1000000.
- `CLAUDE_CODE_DISABLE_1M_CONTEXT` holds sessions to a 200K window.
- pricing.md: "A 900k-token request is billed at the same per-token rate as a 9k-token request."
- best-practices: "performance degrades as it fills".
- Chroma Context Rot (Hong, Troynikov, Huber; 2025-07-14) tested 18 LLMs.
- Local: 20 of 20 compactions are `auto`. The median `preTokens` is **986,666**; 993,070 is `median_high`.
- Nuance: the `--autocompact` launch flag "isn't preempted by … managed settings", so a managed `autoCompactWindow` can be bypassed per launch.

**cc-cache-breakers (corrected).**
- Every listed cause appears in prompt-caching.md "Actions that invalidate the cache": model switch (opusplan, automatic fallback, skill `model`), effort (Opus 5.5 and Fable 5.1 with an API key or subscription keep the cache), fast mode, MCP when not deferred, plugins with MCP servers, deny rules, compaction, images, upgrades.
- Two causes are overstated:
  - "Denying a whole tool" invalidates the cache only when tool search is unavailable or disabled.
  - "Changing directory/worktree" is a cache-scope fact (different directories build different prefixes). It is not in the invalidation list.
- costs.md confirms the miss definition: "more than 5% and at least 2,000 tokens". Versions: v2.1.251, and v2.1.260 for the likely cause.
- The blog (claude.dev, Thariq Shihipar, 2026-04-30) says "we run alerts on our prompt cache hit rate and declare SEVs if they're too low" and "Monitor your cache hit rate like you monitor uptime."
- Local: 1.72% of requests and 24.2% of cost with the report's glob; **1.34% and 17.4%** on all transcripts.

**cc-ttl-policy (confirmed).**
- prompt-caching.md "Which TTL each request gets" matches: main conversation gets 1 hour on a subscription within plan usage and 5 minutes otherwise; everything else gets 5 minutes, except server-controlled helpers.
- `promptCacheTtl`, `subagentPromptCacheTtl` and the `CLAUDE_CODE_(SUBAGENT_)PROMPT_CACHE_TTL` env vars need v2.1.242.
- The precedence list includes `FORCE_PROMPT_CACHING_5M` and `ENABLE_PROMPT_CACHING_1H`. The doc says these go in the managed-settings `env` block.
- pricing.md: 1-hour writes 2×, 5-minute writes 1.25×, reads 0.1×, 0.05× on Opus 5.5, 0.025× on Fable 5.1.
- The transcript fields `ephemeral_1h_input_tokens` and `ephemeral_5m_input_tokens` are observed locally.

**cc-gateway-cache-strip (confirmed).**
- prompt-caching.md: "Removes the markers while returning success: your entire conversation history bills as uncached input on every turn."
- mcp.md line 1429: "Claude Code disables it when `ANTHROPIC_BASE_URL` points to a non-first-party host."
- prompt-caching.md lists MCP reconnects as the most common invalidation when tools load into the prefix.
- The gateway doc confirms that the 1-hour TTL through a custom base URL needs `extended-cache-ttl` in `anthropic-beta`, forwarded verbatim.

**cc-otel-schema (corrected).**
- Metric names and attributes verified, including `claude_code.token.usage` type input/output/cacheRead/cacheCreation, `query_source` main/subagent/auxiliary, speed, effort, and agent/skill/plugin/marketplace/MCP attribution.
- Also verified: `cost.usage`, `session.count`, LOC/commit/PR counts, `code_edit_tool.decision` and `active_time.total`.
- Beta spans verified: `llm_request` with agent_id and parent_agent_id; the tool span with `result_tokens`.
- Resource attributes verified: `OTEL_RESOURCE_ATTRIBUTES` and `OTEL_METRICS_INCLUDE_REPOSITORY` (`vcs.*`, v2.1.269).
- Corrections:
  - The `api_request` event carries model, cost, duration, input/output/cache tokens, request_id, speed, query_source and effort. **`ttft_ms` is only on the beta `llm_request` span.**
  - The `api_request_body` / `api_response_body` events are not gated at v2.1.274. Only `request_body_id`, `message.id` and `message.uuid` on them need v2.1.274.
  - Inline bodies are truncated at 60 KB by default; only `file:<dir>` mode is untruncated.
  - Doc version gates go up to v2.1.280.

**cc-admin-analytics-apis (corrected).**
- Claude Code Analytics API: daily per-user records, `estimated_cost.amount` in cents, "up to 1-hour delay", at most 1000 per page, not available on Claude Platform on AWS.
- Enterprise Analytics: `read:analytics` scope; `bucket_width` 1d/1h/1m; `starting_at` no earlier than 2026-01-01; "at most 31 days".
- `group_by` accepts product, model, context_window, inference_geo, speed and rbac_group_id. `cost_type` and `token_type` exist as group-bys on the cost endpoints.
- **`attributed_list_price` and `estimated_overage_spend` appear on the /skills endpoint only.** The /plugins response has install, invocation, session and user counts and no price fields.
- The Usage & Cost API has 1m/1h/1d buckets.
- The CC Analytics doc confirms Bedrock, Foundry, Vertex and Claude Platform on AWS are not covered.

**cc-list-price-estimates (confirmed).**
- modelPricing is managed-only: v2.1.242 per costs.md, markup v2.1.271.
- The SDK cost-tracking page says "Do not bill end users or trigger financial decisions from these fields."
- inference_geo "us" is 1.1× on all token categories (Claude 4.6+).
- Fast mode: Opus 5.5 $8/$40; Opus 5 and 4.8 $10/$50.
- Bedrock and Vertex regional endpoints carry a 10% premium.
- CCU is $0.01 (100 CCU = $1) on Claude Platform on AWS and Foundry.

**cc-transcript-format (confirmed).**
- sessions.md: path `~/.claude/projects/<project>/<session-id>.jsonl`, "entry format is internal … changes between versions", 30-day retention via `cleanupPeriodDays`.
- All listed fields are observed locally: usage `cache_creation.ephemeral_1h/5m`, `output_tokens_details.thinking_tokens`, speed, service_tier, inference_geo, server_tool_use; effort, perTurnEffort and attribution*; `compact_boundary` with preTokens/postTokens; 3 `cost-state` entries with totalCostUSD and modelUsage.
- Main-thread duplication today: 28,271 → 12,817 (2.2×). Across all transcripts it is 104,653 → 43,706 (2.4×).
- ccusage `rust/adapters/claude/src/lib.rs` dedupes on `usage_dedupe_hash(message_id, request_id, session_id)`, prefers non-sidechain entries and handles /btw replays.
- SDK doc: "Per-step `output_tokens` is a placeholder."

**cc-managed-settings-levers (corrected).**
- Every key named has a settings-reference heading or env-vars entry.
- `maxEffortLevel`: "holds on every provider … Requires Claude Code v2.1.267".
- Server-managed settings "polls for updates hourly".
- The consumption guide (support article, updated this week) matches: Sonnet as "daily driver", restricting Opus to roles or capping effort below Max, RBAC group limits plus per-user limits.
- Correction on "enforceable":
  - `workflowSizeGuideline` is "sent … as advice, not an enforced cap".
  - The `--autocompact` flag isn't preempted by managed settings, so enforcing the window needs `CLAUDE_CODE_AUTO_COMPACT_WINDOW` in the managed `env` block.

**cc-model-effort-mix (confirmed).**
- The pricing table matches: Fable 5.1 $10/$50 with $0.25 read; Opus 5.5 $4/$20 with $0.20 read (0.05×); Opus 5 $5/$25; Sonnet 5 $2/$10 (now standard); Haiku 4.5 $1/$5.
- "Approximately 30% more tokens" for 4.7+ is confirmed.
- costs.md: "Thinking tokens are billed as output tokens."
- model-config line 538 gives the effort defaults: high everywhere, medium on Opus 5.5, xhigh on Opus 4.7.
- prompt-caching.md: "each plan-mode toggle is a model switch".
- advisor.md: "typically costs less than running the stronger model throughout".
- Blog (Lydia Hallie, 2026-07-07): "the total cost per task can come out lower."

**cc-subagents-teams-workflows (corrected).**
- Docs verified:
  - Subagents get 5-minute TTLs and their own prefix.
  - Explore inherits the model since v2.1.198, capped at Opus on the Claude API.
  - costs.md says agent teams use "approximately 7x more tokens … when teammates run in plan mode".
  - Workflows run "up to 16 concurrent agents" and flag "more than 25 agents, or … 1.5 million".
  - Loops, cross-session messages and goal check-ins each send the full context while idle.
  - Gateway headers carry agent ID and request class.
- Local: 10.3% (10.5% today) covers the 264 direct subagents only. Workflow agents add 37.4%, so **non-main agents were about 44% of list-price cost**. That strengthens the finding's thesis; the number should be corrected.

**cc-tool-output-bloat (corrected).**
- bashOutputMaxChars defaults to 30,000.
- MCP output warns at 10,000 tokens and caps at 25,000.
- Hooks: PostToolUse `updatedToolOutput` "Replaces the tool's output"; costs.md has a PreToolUse test-output filter example.
- AgentDiet (Xiao, Gao, Peng, Xiong; v1 2025-09-28, v2 2026-03-15; FSE 2026): "39.9%-59.7%" input and "21.1%-35.9%" total cost at the same performance.
- CORVUS (Zheng et al., 2026-07-20): 9–50% input tokens, up to 37% fewer reasoning cycles.
- rtk: 81,564 stars, pushed 2026-09-21. The description claims 60–90%. The rtk README itself warns that cutting 90% of bash output "is not the same as cutting your bill by 90%".
- claude-context evaluation: −39.4% on 30 SWE-bench Verified instances is confirmed. The evaluation/README.md was last changed **2025-08-26**; 2026-07-14 is the repo push date.
- Local: Read 31.5% and re-reads 36.5–36.9% are **main-thread only**. Including subagents: Read 56.5% of characters, re-reads 24.3%. All transcripts: 58.4% and 16.5%.

**cc-fixed-context-overhead (confirmed).**
- context-window.md: "Keep it under 200 lines"; auto memory "first 200 lines or 25KB"; skill bodies "capped at 5,000 tokens per skill and 25,000 tokens total".
- `skillListingBudgetFraction` defaults to 0.01.
- mcp.md: descriptions are truncated at 2,048 characters, and tools are deferred by default.
- costs.md: "Prefer CLI tools when available".
- large-codebases.md covers claudeMdExcludes, sparsePaths and code intelligence (LSP).
- token-counting.md: "free to use", with separate rate limits.

**cc-first-party-visibility (confirmed).**
- costs.md: Prompt cache (main) line (v2.1.251, likely cause v2.1.260), plan attribution by skill, subagent, plugin and MCP server, behavior flags at ≥ 10%, Loops rows, /insights HTML report.
- statusline.md exposes cost.*, context_window.*, prompt_cache, rate_limits.*, effort.level and fast_mode.
- "computed from local session history on this machine" is confirmed.

**cc-gateway-attribution-caps (confirmed).**
- `x-claude-code-session-id` is on every request.
- `x-claude-code-agent-id` is on subagent requests. `parent-agent-id` appears **only for nested agents**, which is a minor imprecision in the finding.
- Hint headers need v2.1.273. They are on by default for direct Anthropic API connections, and need `CLAUDE_CODE_GATEWAY_HINT_HEADERS=1` for a custom base URL.
- The Claude apps gateway is self-hosted. It supports daily/weekly/monthly caps per user, rbac_group or organization; returns 429 `billing_error`; warns at 75% and 95%; bills aborts at about 4 characters per token; prices unknown models at $5/$25.

**codex-cli-telemetry (corrected).**
- config-advanced shows `[otel]` with otlp-http/otlp-grpc exporters.
- Events: codex.conversation_starts, codex.api_request, codex.sse_event ("token counts on response.completed") and codex.tool_result.
- Metrics: `turn.token_usage` by token_type and `task.compact`.
- config-reference lists `model_auto_compact_token_limit`, `model_reasoning_effort` (low…max/ultra), `service_tier` ("fast maps to the request value priority"), `tool_output_token_limit`, `model_verbosity` and requirements.toml.
- The ccusage Codex guide confirms `~/.codex/sessions` (and archived_sessions) JSONL with `token_count` events.
- Correction: the official Analytics API page does not describe per-user credits or tokens, or day/week buckets. It defers to the Admin API reference and names a unified Daily Usage Analytics API (scope `enterprise.analytics.usage.read`; "codex.enterprise.analytics.read doesn't grant access"). The per-user credits/tokens with day/week buckets, the 90-day lookback and the 12-hour lag all come from the secondary source (danielvaughan.com, 2026-05-11, updated 2026-09-23). Treat all of it as weak evidence.

**gemini-cli-telemetry (confirmed).**
- telemetry.md (last commit 2026-06-18): `api_response` has input, output, cached_content, thoughts, tool and total token counts.
- `gemini_cli.token.usage` has type input/output/thought/cache/tool.
- `chat_compression` has tokens_before and tokens_after.
- `gen_ai.client.token.usage` is emitted.
- The ccusage Gemini guide covers `~/.gemini/tmp/*/chats/` and says "`input` values can include cached prompt tokens".

**copilot-ai-credits (confirmed).**
- The GitHub blog post (published 2026-04-27, effective 2026-06-01) says credits are consumed on "input, output, and cached tokens, according to the published API rates".
- Business gets $19 and Enterprise $39, with promotional $30 and $70 through August.
- Budgets can be set at enterprise, cost-center and user level. Completions and NES remain free.
- Changelog 2026-06-19: `ai_credits_used` is "an overall per-user total … not currently broken down by feature, model, or surface", and is "a metrics signal …, not a billed total". It appears in single-day and 28-day reports.
- The ccusage Copilot guide covers session-state `events.jsonl` and OTel JSONL.

**cursor-admin-api (confirmed).**
- `/teams/filtered-usage-events` returns inputTokens, outputTokens, cacheWriteTokens, cacheReadTokens, totalCents, chargedCents, cursorTokenFee, kind and maxMode. It allows 60 requests per minute and aggregates hourly.
- `daily-usage-data` and audit endpoints allow 20 requests per minute. The date range can't exceed 30 days.
- `/teams/user-spend-limit(s)` exists.
- The Cursor Token Rate is $0.25/MTok on third-party models for Teams and Enterprise.
- Cursor is absent from ccusage's 18-source table.

**competitive-landscape (confirmed).**
- ccusage: 18,709 stars, v20.0.24 on 2026-09-21. It lists 18 sources, and its README sponsor line says "Lineman.io … 40% lower token usage".
- Claude-Code-Usage-Monitor: 8,716 stars, pushed 2026-07-05.
- anthropics/claude-code-monitoring-guide was pushed 2025-07-29.
- Datadog Agent Console (2026-06-09, Tunnell, Bujnowski, Sobolik):
  - spend by agent, team and user
  - detects skipped checks, retry loops and file rereads
  - a Fix Library with PreToolUse hooks
  - "a per-repository sidebar that estimates how much spend could be saved"; the monthly figure is org-wide per pattern
- Dash0 (2026-08-19) charges "$10 per monitored developer per month".

**research-variance-spec (confirmed).**
- Bai et al. say, verbatim: "up to 30x in total tokens", "weak-to-moderate correlations, up to 0.39", "accuracy often peaks at intermediate cost and saturates".
- Smékal (2026-08-26, single author, Kimi K3, 2,700 runs): "29.7%", "13% to 115%", a predictor "from a single cheap probe on an unseen task within 36%".
- best-practices: "corrected Claude more than twice … Run `/clear`"; plan mode is also recommended.

**research-context-compression (corrected).**
- ACON (Kang et al., ICML 2026; v3 2026-06-01) reports "26-54%" peak-token reduction "while improving task success **over existing compression baselines**", and distills into smaller models.
- AgentDiet reports 39.9–59.7%. CORVUS reports **9–50%** with "comparable pass rates" and up to 37% fewer reasoning cycles. Chroma tested 18 models.
- Correction: the headline range should be 9–60%, not 26–60%. "Without accuracy loss" is too strong for ACON, whose comparison is against other compressors.

**finops-focus-tokenomics (confirmed).**
- The LF press release of 2026-08-04 states "30 initial members"; 28 are named in the text. JPMorganChase, IBM, SAP, Vantage and Finout are among them. It mentions "FOCUS v1.5 and beyond".
- FOCUS v1.3 defines all ten columns cited.
- The spec site now shows v1.4 as the latest version.

**billing-path-optimizer (corrected).**
- costs.md confirms the per-seat 5-hour and weekly allowances, usage credits with org/group/member limits, and "Usage inside the seat allowance isn't metered in dollars."
- Quesma ("Claude Code pricing: same tokens, same model, up to 40x the price", Jacek Migdal, **2026-08-11**) takes its 12–40× figures from **individual Claude Max plans**, e.g. SemiAnalysis, Max 20x at $200 vs about $8,000 API-equivalent.
- The article's only org-level figure is Pylon: Team vs Enterprise at $400K/yr vs about $1.4M/yr projected, or **3.5×**.
- Leaders who moved from seats to per-token Enterprise saw the bill "at least double. Most reported roughly 3x."
- Correction: for enterprises, the documented seat-vs-token spread is about 2–3.5×, not an order of magnitude.

**tokenbill-pricing-gaps (corrected).**
- Code check of `tokenbill/pricing.py`:
  - There is no Opus 5.5 row.
  - All writes use `cache_write_multiplier = 1.25`, so there is no 1-hour pricing.
  - There is no speed/fast-mode or inference_geo handling.
  - `CACHE_TTL_SECONDS = 300`.
  - The simulator assumes "a single breakpoint … at the end of messages".
- prompt-caching.md confirms that mid-conversation system blocks are cached.
- count_tokens is free, and the 30% tokenizer note comes from pricing.md.
- Corrections:
  - "28,119 of 28,146 usage entries with cache writes were 1-hour" covers **main-thread entries only**; subagent and workflow writes are all 5-minute. Across all transcripts, 28,127 of 104,432 cache-write entries were 1-hour. 1-hour writes were 27.8% of cost on main + subagents and 17.4% on all transcripts.
  - In Token Bill, char/3.7 attribution is rescaled to billed totals (report.py footnote; `analyzer.py`), so a uniform tokenizer density shift cancels in proportions. Unscaled char/3.7 is still used for the min-cacheable-prefix gate (`simulator.py` lines 185 and 200) and breaker `prefix_tokens` (`breakers.py` line 255), and there it undercounts on 4.7+ models.
