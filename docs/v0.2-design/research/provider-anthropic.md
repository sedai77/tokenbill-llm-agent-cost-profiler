# Provider track: Anthropic — every cost lever, as of 2026-09-23

Research track `provider-anthropic` for **Token Bill** (github.com/sedai77/tokenbill-llm-agent-cost-profiler, v0.1.2).
Goal: an exhaustive, source-backed checklist of every lever that makes an enterprise's Anthropic / Claude Code bill smaller, with exact numbers, and a concrete "what Token Bill should build" plan.

---

## 0. Method and source hygiene

- **Primary sources only for numbers.** Every price, multiplier, limit and measured result below was read on an official page on 2026-09-23: `platform.claude.com/docs/*` (API docs), `code.claude.com/docs/*` (Claude Code docs), the platform release notes, and `anthropic.com` launch posts. Anthropic's bundled `claude-api` skill docs (skill build 2.1.280, cached 2026-06-24) were used as a cross-check and for mechanics.
- **Doc pages carry no "last updated" stamp.** Dates below are either the release-note entry date or "accessed 2026-09-23".
- **Summarizer risk handled.** Pages were fetched through a summarizing reader, so every headline number from the cost-optimization guide was re-fetched and confirmed as a **verbatim quote** (section 3.F). Numbers I could not confirm verbatim are marked `weak` or omitted.
- **Important finding about the sources themselves.** The bundled skill snapshot (June 2026) quotes older measured results (caching "2.5x-3.7x", re-run-failures "$0.70 vs $1.39", task budgets "18%/47%", compaction "38%"). The live guide now says **2.7x-5.3x, $0.45 vs $0.93, 44%/58%, 32%**. Anthropic re-measures on new models, so **Token Bill must never hard-code published savings figures**: it needs a versioned "evidence registry" with a source URL and a check date for each one.

---

## 1. Executive summary (the ten things that matter most)

1. **Prompt caching is by far the biggest lever, and cache *reads* now dominate agentic bills.** Anthropic measured caching cutting agent-loop cost by **2.7x-5.3x**, and a triage agent's bill by **83% (88% with input trimming)**. On real traffic, agent loops read a **median 84%** of input from cache and the **top 10% read ≥94%**. Anthropic says to look for a cache breaker **below ~80%**.
2. **Cache reads are no longer uniformly 0.1x.** Fable 5.1 / Mythos 5.1 reads cost **0.025x** ($0.25/MTok). Opus 5.5 reads cost **0.05x** ($0.20/MTok). Every other model is 0.1x. So a cache break costs **50x** a read on Fable 5.1, **25x** on Opus 5.5 and **12.5x** on Opus 5. Break detection is worth more every release.
3. **The 1-hour TTL (2x write) vs 5-minute TTL (1.25x write) choice is measurable.** Anthropic found the 1-hour TTL becomes cheaper once about **1 turn in 30 follows a pause** (they recommend a "1-in-20" rule). On Fable 5.1, a `max_tokens: 0` **keep-alive** sent every **4 minutes** usually beats the 1-hour TTL. Token Bill can replay real timestamps and choose the TTL per workload.
4. **Discounts stack multiplicatively.** Batch is 50% off *every* token category, including cache reads and writes. Batch cache hits are best-effort (**30%-98%**). Data residency (`inference_geo: "us"`) is **1.1x on everything**. Fast mode is **2x** list ($8/$40 Opus 5.5; $10/$50 Opus 5 and Opus 4.8). Cache multipliers apply on top of fast mode and residency.
5. **Many cache breakers are silent and now enumerable.** Anthropic publishes an invalidation hierarchy (tools → system → messages). Changes to tool_choice, images, thinking, effort, speed, web-search or citations toggles, model, tool list and history edits each break a different tier. The **20-block lookback**, **per-model minimum prefix (512-4096)**, **concurrent fan-out timing**, **workspace isolation** and **TTL expiry** also cause misses. The server-side **cache diagnostics beta** returns `cache_miss_reason` plus `cache_missed_input_tokens`.
6. **New cache-preserving API forms replace the old breakers.** Mid-conversation `role:"system"` messages (GA), `tool_addition`/`tool_removal` (beta), inline tools (beta, 2026-09-22), per-message effort (beta), and turn-scoped `clear_at` reminders. A cleared reminder costs **0 input tokens**. Token Bill can recommend exactly which one fixes each break.
7. **Cost per solved task beats cost per token.** Fable 5.1 at `low` effort solved **88.6% at $0.54/solved** vs Sonnet 5 at **77.4% at $0.84/solved**, despite a 5x per-token price. Effort sweeps, "run low then re-run failures" (**$0.45 vs $0.93**, same 91.7-93% pass), and task budgets (**-44% cost for ~3 pts** on Fable 5.1) are the next biggest levers.
8. **Input hygiene levers have hard numbers:**
   - tool search: >85% fewer tool-definition tokens; **45% cheaper at 502 tools**
   - programmatic tool calling: **-24% input tokens** on agentic search; **-38%** on a 75-tool agent; **20-40%** typical at 10-49 tools; but **+8%** on sequential tasks
   - Files API + code execution instead of pasting data: **$5.01 → $0.40**, with accuracy **6/25 → 25/25**
   - image downscaling: the high-res tier costs up to ~3x the visual tokens
   - prompt audit when migrating models: stale prompts cost **+36%**; audited prompts were **14% cheaper** and more accurate
9. **Claude Code is where enterprise money goes, and it has its own cost physics.** Anthropic reports averages of about **$13 per developer per active day** and **$150-250 per developer per month**, with 90% of users under $30/day. Default cache TTL is **5 minutes on API keys and cloud providers** vs 1 hour on subscriptions. Other cost drivers:
   - `opusplan`, automatic model fallback, and skill `model:` frontmatter each count as model switches, which break the cache
   - agent teams use **~7x** the tokens
   - worktrees and different directories do not share cache
   - `promptCacheTtl` and `modelPricing` are managed settings
   - OpenTelemetry exports per-user cost and cache tokens
10. **Data for an enterprise optimizer now exists at org scale:**
    - Usage & Cost Admin API: 1-minute buckets; grouping by api_key, workspace, model, service_tier, context_window, inference_geo and speed
    - Claude Code Analytics API: per user per day, with tokens, estimated cost and edit acceptance
    - Enterprise Analytics API: per-user **`amount` (post-discount) vs `list_amount`**
    - Claude Code OpenTelemetry metrics
    - The `anthropic-workspace-id` response header

    Token Bill can move from reading JSONL files to an org-wide FinOps and optimization product.

---

## 2. Price sheet (verified 2026-09-23)

Source: https://platform.claude.com/docs/en/about-claude/pricing (accessed 2026-09-23). Prompt-cache minimums come from https://platform.claude.com/docs/en/build-with-claude/prompt-caching (accessed 2026-09-23).

| Model | Input | 5m write (1.25x) | 1h write (2x) | Cache read | Output | Batch in/out | Min cacheable prefix | Notes |
|---|---|---|---|---|---|---|---|---|
| Fable 5.1 / Mythos 5.1 | $10 | $12.50 | $20 | **$0.25 (0.025x)** | $50 | $5 / $25 | 512 | Launched 2026-09-01. No Priority Tier. Covered model: needs 30-day retention (no ZDR). |
| Fable 5 / Mythos 5 | $10 | $12.50 | $20 | $1.00 | $50 | $5 / $25 | 512 | |
| **Opus 5.5** | **$4** | **$5** | **$8** | **$0.20 (0.05x)** | **$20** | $2 / $10 | 512 | Launched 2026-09-22. Default effort `medium`. Thinking can't be disabled. Fast mode $8/$40. |
| Opus 5 | $5 | $6.25 | $10 | $0.50 | $25 | $2.50 / $12.50 | 512 | Fast mode $10/$50. No Priority Tier. |
| Opus 4.8 | $5 | $6.25 | $10 | $0.50 | $25 | $2.50 / $12.50 | 1,024 | Fast mode $10/$50. |
| Opus 4.7 | $5 | $6.25 | $10 | $0.50 | $25 | $2.50 / $12.50 | 2,048 | Fast mode removed (errors). |
| Opus 4.6 / 4.5 | $5 | $6.25 | $10 | $0.50 | $25 | $2.50 / $12.50 | 4,096 | 4.6 fast mode silently runs at standard speed. |
| Sonnet 5 | $2 | $2.50 | $4 | $0.20 | $10 | $1 / $5 | 1,024 | $2/$10 made permanent 2026-08-10. New tokenizer. |
| Sonnet 4.6 / 4.5 | $3 | $3.75 | $6 | $0.30 | $15 | $1.50 / $7.50 | 1,024 | |
| Haiku 4.5 | $1 | $1.25 | $2 | $0.10 | $5 | $0.50 / $2.50 | **4,096** | Old tokenizer. |
| Opus 4.1 / Opus 4 (retired on 1P) | $15 | $18.75 | $30 | $1.50 | $75 | $7.50 / $37.50 | 1,024 | Still served on Bedrock / Google Cloud. |
| Haiku 3.5 (retired on 1P) | $0.80 | $1 | $1.60 | $0.08 | $4 | $0.40 / $2 | 2,048 | Cache reads count toward ITPM. |

All prices are USD per million tokens.

### Price modifiers and other line items

- **Tokenizer:** Claude 4.7+ (Opus 4.7/4.8/5/5.5, Sonnet 5, Fable, Mythos) produce **~30% more tokens** for the same text. The migration guide gives 1x-1.35x. Every token-denominated baseline must be re-measured with `count_tokens` against the target model.
- **Long context:** none. All 4.6+ models bill the full 1M window at standard rates ("a 900k-token request is billed at the same per-token rate as a 9k-token request").
- **Data residency:** `inference_geo: "us"` is **1.1x on all categories** (input, output, cache writes, cache reads) for 4.6+ models.
  - Applies on the 1P API and Claude Platform on AWS.
  - Also applies on Foundry's "US Data Zone Standard" deployment type.
  - Bedrock and Vertex regional or multi-region endpoints carry a **10% premium** over global endpoints.
  - Rate limits are shared across geos.
- **Fast mode** (research preview, 1P API only): up to 2.5x output tokens per second at **2x** price. It does not work with Batch or Priority Tier. On the API, switching speed breaks the cache.
- **Batch:** 50% off everything.
  - Limits: 100,000 requests or 256 MB per batch. Most batches finish in under 1 hour; the hard expiry is 24h. Results are kept for 29 days.
  - Cache hits are best-effort, at **30%-98%**. Anthropic recommends the 1h TTL for batches.
  - `max_tokens: 0` is not allowed inside a batch.
  - The `output-300k-2026-03-24` beta allows 300k-token outputs in batch.
- **Priority Tier:** **no longer sold**. Existing commitments run to contract end.
  - Burndown weights: cache read = 0.1, 5m write = 1.25, 1h write = 2.0, US geo = 1.1.
  - Not supported on Fable 5.1, Mythos 5.x, Opus 5, Opus 5.5 or Sonnet 5.
  - Priority costs do **not** appear in the cost report.
- **Tool-use system prompt overhead** (per request when tools are present, in addition to your tool schemas):
  - Opus 5.5: 286 tokens. Opus 5: 286 (406 with forced tool_choice). Opus 4.8: 290.
  - **Opus 4.7: 675 / 804.** Opus 4.6: 497 / 589. Sonnet 5: 354 / 474. Haiku 4.5: 496 / 588.
  - Bash tool adds 325 tokens (Opus 5/4.8/4.7). Text editor adds 700. Computer toolset about 4,500. Browser toolset about 6,600.
- **Server tools:**
  - Web search: **$10 per 1,000 searches**, plus result tokens.
  - Web fetch: tokens only. Use `max_content_tokens`.
  - Code execution: **free when used with web_search/web_fetch 20260209+**. Otherwise 5-minute minimum, **1,550 free hours per org per month**, then **$0.05/hour per container**. It bills even if the tool is not called when files are preloaded.
- **Managed Agents:** tokens at list price plus **$0.08 per session-hour** while `running`. No batch discount.
- **Files API:** free (upload, list, delete, download). Content is billed as input tokens when referenced. Limits: 500 MB per file, 1 TB per org, about 500 RPM.
- **Token counting:** free. Separate rate limit: 5k / 10k / 20k RPM by tier. It returns an estimate, and system-added tokens are not billed. It rejects server tools, the MCP connector, and url/file sources.
- **Vision:** tokens = `ceil(w/28) × ceil(h/28)`.
  - High-res tier (Claude 4.7+): long edge 2,576 px, max **4,784** visual tokens.
  - Standard tier: 1,568 px, max 1,568 tokens.
  - A 1920×1080 image costs 1,560 tokens on standard vs **2,691** on high-res.
- **Marketplace billing:** Claude Platform on AWS and Foundry bill in **CCUs** ($0.01 per CCU, discounts applied as fewer CCUs). The usage and cost API endpoints are **not available** on Claude Platform on AWS.

---

## 3. Exhaustive lever checklist

Each lever lists the mechanism, exact numbers, and the Token Bill angle. "Detect" means something Token Bill can find in traces or reports. "Sim" means it can be simulated. "Fix" means Token Bill can recommend or auto-apply the change.

### A. Prompt caching mechanics (free win, and the largest lever)

| # | Lever / rule | Exact facts | Token Bill angle |
|---|---|---|---|
| A1 | Prefix-match invariant; render order tools → system → messages | Any byte change at position N invalidates everything at or after N. | Already the core of v0.1.2. Keep it. |
| A2 | **Invalidation hierarchy** | Tool definitions or model change: invalidates everything. **`speed`, web-search toggle, citations toggle, system prompt**: tools cache survives; system and messages break. **`tool_choice`, images**: tools and system survive; messages break. **thinking / effort change**: messages always break; tools and system break on some models. Message edit: messages break. | Detect each kind separately. The dollar impact depends on the tier. Most of these breakers are missing today. |
| A3 | Max 4 breakpoints; **20-block lookback** | Each breakpoint walks back at most 20 positions. A run of `tool_use` blocks, or a run of `tool_result` blocks, counts as 1 position. Fix: add an intermediate breakpoint every ~15 positions. | Sim: the replay assumes an unlimited lookback. It must model the 20-position walk and flag turns that append >20 positions. |
| A4 | **Per-model minimum cacheable prefix** | 512 (Fable, Mythos, Opus 5, Opus 5.5). 1,024 (Opus 4.8, Sonnet 5/4.6/4.5). 2,048 (Opus 4.7, Haiku 3.5). **4,096 (Opus 4.6/4.5, Haiku 4.5)**. Below the minimum, nothing caches and no error is raised. | Detect: "cache_control present but 0 read/0 write". Recommend a model whose minimum fits, or pad or merge the prefix. |
| A5 | **5m vs 1h TTL** | Writes cost 1.25x (5m) vs 2x (1h). Read cost is the same. A read refreshes the timer for free. The lifetime is measured from the **request start**, so generation time counts. Longer TTL entries must come before shorter ones. Breakeven per the pricing page: 5m pays after 1 read, 1h after 2 reads. Anthropic measured the **1h TTL as cheaper once ~1 turn in 30 follows a pause**. | Sim both TTLs from real start-to-start gaps. Recommend per route. Detect the 1h-after-5m ordering error. |
| A6 | **Keep-alive (`max_tokens: 0`)** | Re-send the previous request with `max_tokens: 0` **within 4 minutes of the previous request's start and every 4 minutes after**, dropping `stream`. It bills only a cache read. Anthropic's guidance: on Fable 5.1, keep the 5-minute cache warm through pauses of minutes, and buy the 1-hour duration when pauses run toward an hour. Rejected together with `stream: true`, structured outputs, forced tool_choice, `thinking.type: "enabled"`, and inside Batches. | Sim a "keep-alive" scenario. Emit a ready-to-paste keep-alive policy. |
| A7 | **Pre-warming** | Send `max_tokens: 0` with an explicit breakpoint on the shared prefix, **not** automatic caching (which would key the cache to the placeholder message). Match the real traffic's thinking and effort settings. | Detect cold-start first requests after deploy or idle. Recommend pre-warm only for latency-sensitive routes. |
| A8 | **Automatic (top-level) caching** | Uses 1 of the 4 slots. Returns a 400 if all 4 explicit slots are used, or if the last block's TTL differs. **A pure surcharge when the prompt ends in unique per-request content**: it writes every request and never reads. Robust pattern: an explicit breakpoint on the static system prefix plus automatic caching for the tail. | Detect the signature: `cache_creation` on every request while reads never cover the shared prefix. |
| A9 | **Concurrent fan-out timing** | A cache entry is readable only after the first response **begins streaming**. N parallel identical-prefix requests all pay full price. Fix: send 1, await the first token, then send N-1. | Sim: requests overlapping within the first-token latency cannot hit. Detect fan-out bursts with N writes. |
| A10 | **Workspace / org isolation** | Caches are isolated per **workspace** on the 1P API, Claude Platform on AWS and Foundry, and per **org** on Bedrock and Google Cloud. Traffic split across workspaces writes separate entries. The `anthropic-workspace-id` header was added 2026-08-11. | Detect the same prefix hash appearing in more than one workspace. Recommend consolidation. |
| A11 | **Thinking-block stripping on older models** | On Opus/Sonnet before 4.5/4.6 and on all Haiku through 4.5, a plain user message after tool use strips earlier thinking blocks. Everything after that point falls out of cache, showing as a write spike. Toggling thinking on or off breaks messages on every model. | Detect: Haiku 4.5 or older model + thinking + non-tool-result user turn + write spike. |
| A12 | **Preserved thinking (Fable 5.1, Opus 5.5)** | Editing history invalidates thinking blocks. Accounts created **on or after 2026-08-31** get a **400** on edited history. Dropped blocks change the messages cache from that point. Only Fable 5.1 / Mythos 5.1 read Opus 5.5 blocks, so a fallback to another model runs without them. | Detect harnesses that aren't append-only. Enforcing append-only improves cache hits and avoids the 400. |
| A13 | **Cache-preserving replacements** | Mid-conversation `role:"system"` messages (GA; Opus 5/5.5/4.8, Fable, Mythos; **not Sonnet 5**). `tool_addition`/`tool_removal` (beta `mid-conversation-tool-changes-2026-07-01`). **Inline tools** (beta `inline-tools-2026-09-15`, 1P only). Per-message effort (beta `mid-conversation-output-config-2026-07-01`; Fable 5.1, Mythos 5.1, Opus 5, Opus 5.5). `clear_at: "next_user_message"` reminders: **0 input tokens once cleared** (beta `mid-conversation-system-clear-at-2026-08-21`). | Map each detected breaker to its cache-preserving fix, gated by model and platform availability. |
| A14 | **Cache diagnostics beta** | Header `cache-diagnosis-2026-04-07`. Send `diagnostics.previous_message_id` on every turn. Response gives `cache_miss_reason` ∈ {model_changed, system_changed, tools_changed, messages_changed, previous_message_not_found, unavailable} plus `cache_missed_input_tokens`. 1P only; fingerprints expire quickly. `unavailable` covers changes to tool_choice, thinking, context_management, output_config or the beta-header set. | Recorder: inject the header and ID and store the result. Use it as ground truth for breaker classification. Report agreement between Token Bill's classification and the server's. |
| A15 | **Server tools auto-write** | Web search inserts a 5-minute cache write after tool results when the request already uses caching. | Don't flag these writes as breakers. |
| A16 | **Rate-limit effect** | **Cache reads do not count toward ITPM** (except Haiku 3.5). Cache writes and uncached input do. Example from the docs: 2M ITPM at 80% hit rate gives 10M effective input tokens per minute. OTPM counts actual output, not `max_tokens`. | Report the throughput headroom gained, so the cache ROI includes avoided rate-limit tier upgrades. |

### B. Batch and service tiers

- **B1 Batch 50%.** It stacks with caching. Batch cache hits are best-effort (30-98%); use identical `cache_control` blocks and the 1h TTL.
  - Limits: 100k requests / 256 MB; <1h typical, 24h expiry; results kept 29 days.
  - Batch rate limits: Start 1,000 RPM / 200k queued, Build 2,000 / 300k, Scale 4,000 / 500k.
  - Not available: Managed Agents, fast mode, `fallbacks`, `max_tokens: 0`.
  - *Token Bill:* classify traffic as "nobody waited" (non-streaming, offline cron, eval, backfill) and price the batch counterfactual. Ask the Usage API `group_by service_tier` what already runs in batch.
- **B2 Priority Tier** is discontinued for new buyers. Remove it from recommendations. For existing commitments, compute utilization from the `anthropic-priority-*` headers and `service_tier=priority` usage.

### C. Input-token hygiene (free wins)

- **C1 Tool search (`defer_loading`).**
  - A 5-server MCP setup costs ~55k tokens of definitions; tool search cuts this by **>85%**.
  - Accuracy degrades past **30-50 tools**. Use tool search at **≥10 tools or >10k definition tokens**.
  - Measured: **45% cheaper at 502 tools** and 20% with the GitHub MCP server, with no accuracy change.
  - Max 10,000 deferred tools. Not metered separately. A deferred tool can't carry `cache_control`. Discovered tools are *appended*, so the cache survives.
- **C2 Programmatic tool calling** (`allowed_callers: ["code_execution_20260120"]`).
  - Results: **-24% input and +11% score** on BrowseComp/DeepSearchQA; **-38% billed input** on a 75-tool agent; **20-40%** typical at 10-49 tools; **+8% cost** on τ²-bench (sequential single-call tasks).
  - Intermediate tool results are **not billed** as tokens. Code execution pricing applies.
  - Not on Bedrock or Vertex. Incompatible with `strict`, forced `tool_choice` and `disable_parallel_tool_use`.
- **C3 Files API + code execution** for tabular data: a 1,862-row CSV cost **$5.01 → $0.40** and accuracy went **6/25 → 25/25**.
- **C4 Web search dynamic filtering** (`web_search_20260209+`): code filters results before they enter context, and code execution is free in this mode. **`response_inclusion: "excluded"`** (`web_search_20260318`) drops consumed result blocks from the response. Set **`max_uses`**. For web fetch, set **`max_content_tokens`** (a 10 kB page is ~2.5k tokens; a 500 kB PDF is ~125k).
- **C5 Images:** pre-downscale. High-res models can use ~3x the visual tokens. Use the Files API `file_id` to keep payloads small (tokens are still billed).
- **C6 Prompt audit on model migration:**
  - Prompts written for Opus 4.8 cost **+36% per ticket** on Opus 5 with no accuracy change.
  - Audited prompts were **14% cheaper** than unaudited, with accuracy **97% vs 92%**.
  - The Sonnet 4.6 → 5 audit took **14%** off.
- **C7 Tokenizer migration tax:** plan for ~30% more tokens (1x-1.35x) moving to 4.7+ models. Per-token price cuts can be offset by the higher count.
- **C8 Tool-overhead awareness:** Opus 4.7 adds 675-804 tool-prompt tokens per request vs 286 on Opus 5/5.5. This is uncached overhead when tools churn.

### D. Agent-loop / context lifecycle

- **D1 Compaction.**
  - Threshold compaction (`compact-2026-01-12`, `compact_20260112`): default trigger **150,000**, minimum **50,000** input tokens.
  - On-demand compaction (`compact-2026-09-04`, 2026-09-14): a separate summarize call.
  - Measured **-32%** on long runs; **zero savings** on short runs.
  - **Billing gotcha:** top-level `usage.input_tokens/output_tokens` **exclude** the compaction iteration. You must **sum `usage.iterations`**.
  - A compaction request right after a cache-invalidating change re-processes the context at the write price: $0.21 vs $0.04 in Anthropic's measurement.
- **D2 Context editing** (`clear_tool_uses_20250919`, default trigger 100k input tokens, keep 3 tool uses, `clear_at_least`). It is a context-window tool, not a savings lever: **+74% cost** on a short run and neutral on a long run. Clear in a few large batches. Response gives `applied_edits[].cleared_input_tokens`.
- **D3 Client-side pruning at natural boundaries:** **-39%** on long runs, with cache reads back to 89% at task boundaries.
- **D4 Subagents / forks.** A subagent starts a fresh prefix. A fork that copies the parent's system, tools and model exactly reuses the parent's cache.

### E. Output tokens

- **E1** Specify the output shape: a one-line format used **39% fewer output tokens** and cost **14% less** per run at the same accuracy. A memo format cost $1.40 vs $0.57.
- **E2** `max_tokens` is a backstop, not a savings knob.
  - A 16,384 cap ended **15%** of Opus 5 and **43%** of Fable 5.1 attempts.
  - At 64k, 2 of ~14,000 turns were cut and Fable 5.1 solved 58.5% vs 36.3%.
  - 128k had the same cost per solved task.
  - Treat `stop_reason: max_tokens` as wasted spend.
- **E3** Stop-sequence sentinels for early exit.

### F. Effort, thinking, budgets (trade-offs; need an eval)

All verified verbatim on https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence (accessed 2026-09-23).

- **F1 Effort sweep.**
  - SWE-bench Pro on Opus 5: `medium` gave up ~2 pts for **half** the cost; `low` gave up ~8 pts for **a quarter**.
  - Research and knowledge work is nearly flat (bundled guide: `low` -1 to -3 pts for a third to a half off; `medium` matched the default at 70-85% of cost).
  - **Opus 5.5 defaults to `medium`**; every other model defaults to `high`.
  - A top-level effort change invalidates the messages cache. Use per-message effort instead.
- **F2 Re-run failures at higher effort.** On Opus 5, run everything at `low` and re-run failures at the default: **~93% pass for ~$0.45/task** vs **91.7% at $0.93**.
- **F3 Task budgets** (beta `task-budgets-2026-03-13`, minimum total 20,000, advisory).
  - Fable 5.1: a generous budget gave **-44%** cost for ~3 pts; the tightest gave **-58%** for 6 pts.
  - Set once. Changing the budget mid-task busts the cache.
  - Not supported on Sonnet 5, Opus 4.6, Sonnet 4.6, Haiku 4.5, or Claude Code / Cowork.
- **F4** Thinking tokens bill as **output**. `display: "omitted"` does **not** reduce cost.

### G. Model choice and multi-model routing (last, deliberate)

- **G1 Price cost per solved task.**
  - Fable 5.1 `low`: $0.54/solved at 88.6%. Sonnet 5: $0.84/solved at 77.4%.
  - Terminal-Bench 3: Opus 4.7 $183, Opus 4.8 $63, Opus 5 **$28 per solved task**.
  - Fable 5 → Fable 5.1: **-43%** per solved task.
  - Opus 5.5: **"costs 40% less to run than Opus 5 on typical workloads"**.
  - Fable 5.1: **~25% less** than Fable 5 on typical workloads, **up to ~45%** on agentic workloads (measured on August 2026 usage).
- **G2 Orchestrator.** Fable 5.1 lead plus 25 Sonnet 5 workers: **-47 to -55%** cost for **-10 to -12 pts**. On routine BrowseComp, p90 cost was $12 vs $33.
- **G3 Advisor.** Gains depend on the consult rate. At low executor effort the executor can stop consulting and score below the solo model.
- **G4 Caches are model-scoped.** A mid-conversation model switch (routers, A/B tests, fallbacks, `opusplan`) forfeits the cache.
  - Refusal-fallback **credit** (beta `fallback-credit-2026-07-01`) re-bills the previously cached span at the read rate on the retry (bundled skill doc).
  - Server-side `fallbacks` (beta) are rejected on Batches.

### H. Commercial, platform and governance

- **H1** Negotiated discounts. The Enterprise Analytics cost reports return **`amount` (post-discount) and `list_amount`**. The Claude Code `modelPricing` managed setting makes local estimates match contract rates.
- **H2 Geo:** avoid `inference_geo: "us"` (+10%) unless compliance requires it. Audit with Usage API `group_by inference_geo`.
- **H3 Fast mode:** 2x price for ≤2.5x output speed. Audit with `group_by speed`.
- **H4 Spend and rate controls.**
  - Org monthly spend caps: Start $500, Build $1,000, Scale $200,000; Custom tier has none.
  - Workspace spend and rate limits.
  - Managed Agents **session budgets**: hard dollar cap, `budget_reached` (2026-08-07).
  - Claude Code workspace limits.
  - Claude Code per-user TPM guidance: 100-500 users → 15-20k TPM/user; 500+ → 10-15k.
- **H5 Retired and deprecated models.** Opus 4.1 retired on 1P on 2026-08-05; Opus 4 and Sonnet 4 on 2026-06-15. Traffic still on the $15/$75 Opus 4.x models on Bedrock or Vertex is a 3x-over-Opus-5 cost red flag.

### I. Measurement surfaces (what Token Bill can ingest)

| Surface | Endpoint / mechanism | Key facts |
|---|---|---|
| Usage report | `GET /v1/organizations/usage_report/messages` | Buckets 1m (max 1,440), 1h (max 168), 1d (max 31). Filter and `group_by`: `api_key_ids`, `workspace_ids`, `models`, `service_tiers`, `context_window`, `inference_geos`, `speeds` (fast-mode beta header). Fields: uncached input, cache read, `cache_creation.ephemeral_5m/1h`, output, `server_tool_use.web_search_requests`. ~5 min latency; poll ≤1/min. |
| Cost report | `GET /v1/organizations/cost_report` | Daily only. USD in cents as decimal strings. `group_by` workspace_id / description, which yields model, cost_type, token_type, inference_geo. Code execution appears here only. **Priority Tier is not included.** Does not take api-key filters. |
| Claude Code Analytics | `GET /v1/organizations/usage_report/claude_code?starting_at=YYYY-MM-DD` | Per user per day. Sessions, LOC, commits, PRs, edit/write accept and reject counts, per-model tokens and `estimated_cost` (cents). 1h delay. Free. **1P only** (no Bedrock, Vertex, Foundry or Claude Platform on AWS). |
| Enterprise Analytics (claude.ai orgs) | `/v1/organizations/analytics/{usage_report,user_usage_report,cost_report,user_cost_report}` | `read:analytics` key. Products include `claude_code`, `cowork`, `chat`. `group_by rbac_group_id`. **`amount` vs `list_amount`**. ~1 day lag, refreshed every 4h, final at ~30 days. |
| Claude Code OpenTelemetry | `CLAUDE_CODE_ENABLE_TELEMETRY=1` | `claude_code.cost.usage`, `claude_code.token.usage{type=input\|output\|cacheRead\|cacheCreation}`. Attributes include model, `query_source` (main / subagent / auxiliary), effort, speed, skill, plugin, `mcp_server`, user.email, repo. Event `claude_code.api_request` carries cost_usd, cache_read/creation tokens and request_id. Works on every provider. |
| Claude Code `/usage` | Local | `Prompt cache (main)` line (v2.1.251+): hit %, misses (>5% and ≥2,000 tokens re-processed), expected rebuilds, likely cause (v2.1.260+), warm or cold with TTL. |
| Response headers | Every API call | `anthropic-ratelimit-*`, `anthropic-priority-*`, `anthropic-fast-*`, `anthropic-workspace-id`. |
| Per-response usage | Every API call | `usage.cache_creation.ephemeral_5m/1h_input_tokens`, `service_tier`, `inference_geo`, `speed`, `server_tool_use`, `iterations` (compaction, fallback). |

### J. Claude Code-specific cost physics (critical for thousands of developers)

Source: https://code.claude.com/docs/en/costs and https://code.claude.com/docs/en/prompt-caching (accessed 2026-09-23).

- **Baseline:** about **$13 per developer per active day**, **$150-250 per developer per month**, 90% under $30 per active day. Background usage is under ~$0.04 per session.
- **Default TTL:** **5 minutes on API key, cloud provider or usage credits**; 1 hour for the main conversation on a subscription within plan.
  - Override with `promptCacheTtl` / `CLAUDE_CODE_PROMPT_CACHE_TTL` and `subagentPromptCacheTtl` (v2.1.242+).
  - Also `ENABLE_PROMPT_CACHING_1H=1` and `FORCE_PROMPT_CACHING_5M=1`.
  - Subagents get 5 minutes unless set otherwise.
- **Cache breakers in Claude Code:**
  - `/model` switches. **`opusplan`** switches the model on each plan-mode toggle.
  - **Automatic model fallback** on Fable, Opus 5.5 and Opus 5.
  - A skill or command with `model:` frontmatter.
  - Effort changes (except Opus 5.5 and Fable 5.1 on API key or subscription, v2.1.260+).
  - Enabling fast mode (the first time).
  - MCP servers whose tools are loaded into the prefix (tool search off, `alwaysLoad`, a custom `ANTHROPIC_BASE_URL` gateway, Azure-hosted Foundry).
  - Denying a whole tool when tool search is off.
  - `/compact`; image eviction batches; Claude Code upgrades.
  - **Gateways that strip `cache_control` bill the whole history uncached on every turn.**
- **Cache-safe actions:** editing files, editing CLAUDE.md (applies after `/clear`), changing permission mode, output style, skills, `/recap`, `/rewind`, subagents, forks.
- **Cache scope is per machine and directory.** Different worktrees do not share the cache. The Agent SDK can suppress per-machine sections to share cache across a fleet.
- **Agent teams use ~7x tokens** (plan mode).
- **`modelPricing`** managed setting (v2.1.242+) lets `/usage`, the status line and OpenTelemetry report contract rates. `/usage` applies the 1.1x residency multiplier since v2.1.239.

---

## 4. Timeline: Jul-Sep 2026 (release notes + launch posts)

| Date | Event | Cost relevance |
|---|---|---|
| 2026-06-26 | Rate limits consolidated to Start/Build/Scale. Sonnet and Haiku limits raised to match Opus. | Fewer 429-driven retries and fallbacks. |
| 2026-06-30 | Claude Sonnet 5 at $2/$10. New tokenizer (~30% more tokens). No Priority Tier. | Re-baseline token counts. |
| 2026-07-08 | API key expiration. | Key hygiene for attribution. |
| 2026-07-15 | Mid-conversation system messages GA (Fable 5, Mythos 5, Opus 4.8). | Cache-preserving instruction changes. |
| 2026-07-24 | Claude Opus 5 ($5/$25, 512 minimum cache prefix). Mid-conversation tool changes beta. `fallbacks: "default"`. Opus 4.7 fast mode removed. | Tool churn without cache loss. |
| 2026-08-05 | Opus 4.1 retired on 1P. | Migrate off $15/$75 pricing. |
| 2026-08-07 | Managed Agents session budgets, advisor, inference_geo. | Hard dollar caps. |
| 2026-08-10 | Sonnet 5 $2/$10 made permanent (the planned rise to $3/$15 on 2026-09-01 was cancelled). | Pricing table correctness. |
| 2026-08-11 | `anthropic-workspace-id` response header. | Workspace attribution and cache-isolation diagnosis. |
| 2026-08-19 | Files API GA. Agent Skills GA. Computer/browser toolsets. | Progressive disclosure levers. |
| 2026-08-26 / 27 | Admin API in all SDKs. Personal and service-account keys. | Per-user attribution. |
| 2026-09-01 | **Fable 5.1**: cache reads $0.25 (0.025x, **-75%**). ~25% cheaper on typical workloads, up to ~45% on agentic ones. Preserved thinking. Per-message effort. `clear_at`. | Cache break cost = 50x a read. |
| 2026-09-03 | Per-message effort on Google Cloud. | |
| 2026-09-14 | On-demand compaction (`compact-2026-09-04`). | New compaction billing path via `usage.iterations`. |
| 2026-09-22 | **Opus 5.5**: $4/$20, reads $0.20 (0.05x), writes $5; "40% less to run than Opus 5 on typical workloads"; default effort `medium`; fast mode $8/$40. **Inline tools beta.** Sonnet 5.5 and Haiku 5.5 "in the coming weeks". | Largest price drop of the quarter. The pricing table must add it now. |

---

## 5. What Token Bill should build

Ordered by (enterprise dollars moved) × (confidence) ÷ (effort). "P0" means required before submitting to enterprise labs, because a FinOps tool that misprices is disqualified.

### P0 — Correctness and credibility (must fix before enterprise review)

1. **Pricing table v2 as versioned data, not code.**
   - Add Opus 5.5 ($4/$20, **read 0.05x**, 512), Mythos 5.1, Mythos 5, Opus 4.5 (4,096), Sonnet 4.5, Haiku 3.5 (read counts toward ITPM), Opus 4.1/4 ($15/$75, partner-only).
   - Put each row's source URL and check date in the data.
   - Add a CI job that fetches `pricing.md` and fails on drift.
2. **Price 1h cache writes at 2x.** Today `cost_breakdown` bills all `cache_creation_input_tokens` at 1.25x, so traces with `ephemeral_1h_input_tokens` are under-priced by 37.5% on that line. Extend `Usage` with `cache_creation.ephemeral_5m/1h`.
3. **Price every modifier:**
   - `service_tier` (batch 0.5x; priority counted separately)
   - `inference_geo` (1.1x)
   - `speed` (fast mode 2x, model-specific)
   - `server_tool_use` (web search $10/1k; code execution hours after 1,550 free)
   - Managed Agents session-hours ($0.08)
   - **`usage.iterations`**: compaction and fallback iterations are **excluded** from the top-level `input_tokens` and `output_tokens`, so Token Bill must sum them
4. **Reconcile with Anthropic's own numbers.** Pull the Usage & Cost Admin API for the same window and show Token Bill's trace-priced total vs Anthropic's `cost_report`. Enterprise buyers will trust a tool that reconciles to the invoice. Use `amount` vs `list_amount` from Enterprise Analytics for the effective discount.
5. **Tokenizer-aware counting.** Stop relying only on `chars/3.7` for attribution when exactness matters: offer optional `count_tokens` calibration per model. It is free, 5k-20k RPM. It rejects server tools and url/file sources, so fall back to the heuristic there. Label heuristic figures `approx`.

### P1 — Detection: go from 5 breakers to the full published taxonomy

Add these detectors. Each one prices the lost cache span at the model's write-vs-read gap.

| New breaker | Signal |
|---|---|
| `tool-choice-change` | Differs between adjacent calls. Only the messages tier is lost. |
| `image-added-or-evicted` | Image count or bytes change inside the prefix (the Claude Code eviction batches). |
| `thinking-toggle` / `effort-change` | Differs between calls. Fix: per-message effort where the model and platform support it. |
| `speed-change` | Fast / standard flip. |
| `web-search-or-citations-toggle` | Differs between calls. |
| `below-min-prefix` | Breakpoint present, 0 read and 0 write, prefix < model minimum. |
| `lookback-overflow` | >20 positions appended since the previous breakpoint. |
| `ttl-expiry` | Start-to-start gap > TTL. Recommend 1h TTL, keep-alive or accept. |
| `concurrent-fanout` | ≥2 identical-prefix calls started before the first one's first token. |
| `workspace-split` | Same prefix hash across workspaces or API keys in different workspaces. |
| `auto-cache-surcharge` | Automatic breakpoint lands after a unique tail; writes on every call. |
| `ttl-order-error` | 1h breakpoint after a 5m one. |
| `thinking-strip` | Older model + thinking + non-tool-result user turn. |
| `history-edit` / `preserved-thinking` | Non-append-only history (a 400 risk on new accounts). |
| `reminder-inject-delete` | Per-turn reminder deleted on the next request. Fix: `clear_at`. |
| `gateway-strips-cache-control` | `cache_control` sent but 0 reads and 0 writes across the whole session. |
| `fallback-model-switch` / `router-switch` | Model differs mid-run. Suggest fallback credit or a subagent. |
| `compaction-cold` | Compaction pass on a cold cache, or right after an invalidating change. |
| `context-editing-churn` | Many small `applied_edits`. Recommend `clear_at_least` or bigger batches. |

Also:

- **Ingest server truth.** Have the recorder inject `cache-diagnosis-2026-04-07` + `diagnostics.previous_message_id` (1P only) and store `cache_miss_reason` and `cache_missed_input_tokens`. Report agreement between Token Bill's classifier and Anthropic's.
- **Health thresholds from Anthropic's own data.** Flag runs with cache-read share **<80%**; median real-traffic loops are at 84% and the top decile at ≥94%. Show each team's percentile.

### P1 — Simulation: make "what you'd pay" match the real rules

- Model the **1h TTL** (2x writes), **keep-alive pings** (`max_tokens: 0` every 4 minutes, read-priced), and **pre-warm** scenarios. Pick the cheapest policy per route from real timestamps. Anthropic's "1 in 30 paused turns" crossover is the sanity check.
- Model the **20-block lookback**, the **4-breakpoint cap** with realistic placement (static-prefix breakpoint plus automatic tail), **first-token readability** for concurrency, and **workspace isolation**.
- Add **counterfactual pricing scenarios**:
  - batch (0.5x, cache hit rates 30-98% as a range)
  - global vs US geo
  - fast vs standard
  - model migration with **tokenizer inflation** (1x-1.35x from pre-4.7 models) and per-model cache read multipliers (0.1 / 0.05 / 0.025x)
  - Opus 5 → Opus 5.5 "what-if"
- Model **compaction** (sum iterations) and **context editing** (invalidation at the clear point) so lifecycle levers are priced honestly. Anthropic measured **+74%** for context editing on short runs.

### P1 — Recommendations engine (ranked, gated, honest)

- Rank levers by dollar ceiling, deflated for overlap. Split into **free wins** (caching, batch, input hygiene, output shape) and **trade-offs** (effort, task budgets, model, advisor, orchestrator). Mark trade-offs "needs eval", as Anthropic's own workflow does.
- Gate every recommendation on **model × platform availability**:
  - cache diagnostics, fast mode and inline tools: 1P only
  - PTC and code execution: not on Bedrock or Vertex
  - mid-conversation system messages: not on Sonnet 5
  - task budgets: not on Sonnet 5 or in Claude Code
- Emit **copy-paste fixes**: sorted-keys tool serialization; moving timestamps below the breakpoint; the explicit breakpoint + automatic tail pattern; a keep-alive snippet; `response_inclusion: "excluded"`; `defer_loading`; `allowed_callers`; `promptCacheTtl: "1h"` in Claude Code managed settings.

### P1 — Claude Code fleet module (the enterprise wedge)

- **Ingest:** Claude Code Analytics API (per user per day), Enterprise Analytics API (`user_cost_report`, `amount`/`list_amount`), the OpenTelemetry stream (`claude_code.token.usage` by `query_source`, effort, speed, `mcp_server`, skill), and local transcripts.
- **Detect:**
  - 5-minute TTL on API-key fleets where developers pause more than 5 minutes (recommend a `promptCacheTtl` policy per team)
  - `opusplan` toggling
  - frequent `/model` or effort switches
  - MCP servers loaded into the prefix (tool search off, `alwaysLoad`, gateway)
  - gateways stripping `cache_control`
  - agent-teams usage (~7x)
  - sessions never cleared (long-context drag)
  - worktree-fragmented caches
  - `max_tokens` truncations
  - fast mode spend
  - US-geo spend
- **Benchmarks:** show each developer and team against Anthropic's published fleet averages ($13 per active day; $150-250/month; 90th percentile < $30/day), and ROI versus accepted edits, LOC and PRs from the Analytics API.
- **Push policy:** generate `managed-settings.json` fragments (`promptCacheTtl`, `modelPricing`, default model and effort, `autoContinueAtUsageLimit`), with a projected dollar impact for each.

### P2 — Automation and enforcement (turn the report into savings)

- **Optional proxy / SDK middleware** that applies fixes live:
  - canonical JSON ordering
  - breakpoint placement (static prefix plus automatic tail, intermediate breakpoints every ~15 positions)
  - keep-alive scheduler for idle sessions (Fable 5.1 / Opus 5.5 economics)
  - staggered fan-out (first token, then N-1)
  - cache-diagnostics header injection
  - per-message effort instead of top-level changes
  - `clear_at` reminders
  - batch routing for non-interactive traffic

  The proxy must forward `cache_control` and `anthropic-beta` unchanged; Anthropic warns that gateways that don't forward them lose caching.
- **Governance:**
  - budgets and alerts from the Usage API at 1-minute buckets
  - anomaly detection (cache-read share drop, write spike, Opus 4.x on partner clouds, fast-mode drift)
  - chargeback by workspace, API key, RBAC group and user
  - reconcile Priority Tier usage, which is not in the cost report
- **Eval harness for trade-offs:** effort sweep; "low then re-run failures"; task-budget sizing from p90/p99 spend; plotted as a Pareto frontier of cost per solved task vs pass rate. This is how Anthropic itself tells users to decide.

---

## 6. Open questions / gaps to confirm

1. Can the cache-diagnostics fingerprint window support offline analysis, or only live recorders? The docs say fingerprints expire "after a short period".
2. Does the keep-alive refresh work identically for a 1h entry? A read refreshes on either TTL, but keep-alive with the 1h TTL is untested in the docs.
3. What cache hit rates does Batch get on Opus 5.5 / Fable 5.1 specifically? The docs give only the 30-98% range.
4. Sonnet 5.5 / Haiku 5.5 pricing and cache multipliers are not yet published ("coming weeks" as of 2026-09-22).
5. Whether Claude Code's cache-TTL defaults change for Enterprise API-key fleets as Anthropic tunes them. Re-check `code.claude.com/docs/en/prompt-caching` monthly.
6. Bedrock and Vertex regional pricing for Opus 5.5 / Fable 5.1 (10% regional premium; partner-set list prices) must be pulled from the AWS and Google pricing pages. This is outside this track.
7. The Enterprise Analytics API earliest date is 2026-01-01, with ~30-day finality. How should Token Bill show data that is still being revised?

---

## 7. Sources (all opened 2026-09-23 unless a date is given)

Official API docs (platform.claude.com):
- Pricing — https://platform.claude.com/docs/en/about-claude/pricing
- Prompt caching — https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- Cache diagnostics (beta) — https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics
- Rate limits — https://platform.claude.com/docs/en/api/rate-limits
- Usage and Cost API — https://platform.claude.com/docs/en/manage-claude/usage-cost-api
- Claude Code Analytics API — https://platform.claude.com/docs/en/manage-claude/claude-code-analytics-api
- Enterprise Analytics API — https://platform.claude.com/docs/en/api/admin/analytics
- Optimizing for cost and intelligence — https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence
- Release notes — https://platform.claude.com/docs/en/release-notes/overview (entries 2026-06-09 through 2026-09-22)
- Effort — https://platform.claude.com/docs/en/build-with-claude/effort
- Batch processing — https://platform.claude.com/docs/en/build-with-claude/batch-processing
- Fast mode — https://platform.claude.com/docs/en/build-with-claude/fast-mode
- Data residency — https://platform.claude.com/docs/en/manage-claude/data-residency
- Service tiers — https://platform.claude.com/docs/en/api/service-tiers
- Token counting — https://platform.claude.com/docs/en/build-with-claude/token-counting
- Context editing — https://platform.claude.com/docs/en/build-with-claude/context-editing
- Compaction overview / on demand / threshold — https://platform.claude.com/docs/en/build-with-claude/compaction , /compaction-on-demand , /compaction-threshold
- Task budgets — https://platform.claude.com/docs/en/build-with-claude/task-budgets
- Tool search — https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool
- Programmatic tool calling — https://platform.claude.com/docs/en/agents-and-tools/tool-use/programmatic-tool-calling
- Mid-conversation system messages / tool changes / inline tools — https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages
- Web search tool — https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool
- Vision — https://platform.claude.com/docs/en/build-with-claude/vision
- Files API — https://platform.claude.com/docs/en/build-with-claude/files

Claude Code docs:
- Manage costs effectively — https://code.claude.com/docs/en/costs
- How Claude Code uses prompt caching — https://code.claude.com/docs/en/prompt-caching
- Monitoring usage (OpenTelemetry) — https://code.claude.com/docs/en/monitoring-usage

Anthropic news:
- Introducing Claude Opus 5.5 — https://www.anthropic.com/news/claude-opus-5-5 (2026-09-22)
- Introducing Claude Fable 5.1 and Claude Mythos 5.1 — https://www.anthropic.com/claude-fable-and-mythos-5-1 (2026-09-01)

Bundled reference (cross-check only): Anthropic `claude-api` skill 2.1.280 — `shared/prompt-caching.md`, `shared/cost-optimization.md`, `shared/model-migration.md`, `shared/admin-api.md`, `shared/platform-availability.md`, `shared/models.md` (cached 2026-06-24).

Benchmark papers cited by the PTC doc (not independently re-read): BrowseComp arXiv:2504.12516; τ²-bench arXiv:2506.07982.
