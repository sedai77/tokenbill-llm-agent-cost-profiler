# Verification: provider-anthropic track (checked 2026-09-23)

## Method

- I fetched every cited `platform.claude.com` and `code.claude.com` page as raw markdown with `curl <url>.md` and grepped for each number and phrase, so the checks don't depend on a summarizer. Copies are saved in `scratchpad/research/vfa/`.
- Both Anthropic news posts were fetched as HTML and converted to text (`vfa/news_*.html.txt`).
- I also opened pages the findings did not cite when a claim needed them: fallback-credit, refusals-and-fallback, tool-use-with-prompt-caching, the migration guides, and the cost-report and usage-report API references.
- The bundled `claude-api` skill files were read in place.

### Cross-cutting problem: the bundled-skill date is wrong

The report and several findings cite the bundled skill (build 2.1.280) as "cached 2026-06-24". That date can't be right. The bundled files reference:
- Claude Opus 5, released 2026-07-24 (`cost-optimization.md`, `prompt-audit.md`)
- Claude Fable 5.1 and its 0.025x cache read, released 2026-09-01. `cost-optimization.md` says whether Mythos 5.1 shares that rate "is open at launch".
- Opus 5.5 (in `models.md`, `prompt-caching.md` and `model-migration.md`)
- `compact-2026-09-04` and the date 2026-08-31

The snapshot is therefore from around September 2026. The files' mtime is today's extraction time. Every "(cached 2026-06-24)" label should be replaced with "skill build 2.1.280, content dated ~Sept 2026".

## Verdicts

### 1. anth-pricing-table-2026-09 — CONFIRMED
- **Pricing page:**
  - Opus 5.5: $4 input, $5 5m write, $8 1h write, $0.20 read, $20 output. Footnote: reads are 0.05x.
  - Fable 5.1 and Mythos 5.1: $10/$50 with $0.25 reads (0.025x).
  - Fable 5 and Mythos 5: $1 reads (0.1x).
  - Opus 5, 4.8, 4.7, 4.6 and 4.5: $5/$25 with $0.50 reads.
  - Sonnet 5: $2/$10. Footnote 3 says the planned Sept 1 rise to $3/$15 will not happen.
  - Sonnet 4.6 and 4.5: $3/$15. Haiku 4.5: $1/$5.
  - Opus 4.1: $15/$75, retired except on Bedrock and Google Cloud. Opus 4: $15/$75, retired except on Google Cloud.
  - All other models use 0.1x reads.
- **Release notes:** Opus 5.5 launched 2026-09-22 at $4/$20. The Sonnet 5 price was locked 2026-08-10. Fable 5.1 launched 2026-09-01 with reads cut to $0.25.
- **Opus 5.5 post (dated September 22, 2026):** input and output are 20% below Opus 5, and cache reads are $0.20, 60% below Opus 5.

### 2. anth-cache-1h-2x — CONFIRMED
- The pricing page gives multipliers of 1.25x for a 5m write, 2x for a 1h write and 0.1x for a read, with model exceptions. It says caching pays off after one read for 5m and after two reads for 1h.
- The prompt-caching page shows `usage.cache_creation.ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens`.
- Its "Mixing different TTLs" section says longer-TTL entries must appear before shorter ones.

### 3. anth-cache-health-thresholds — CONFIRMED
All of these numbers were found verbatim in the raw markdown of the cost guide:
- a factor of 2.7 to 5.3 on agent loops
- 83% for the triage agent, 88% with input trimming
- a median of 84% cache reads
- the top 10% at 94% or more
- under 1% of input at full price deep in a task
- "Below about 80%" as the point to look for a cache breaker

Ref 17 says the production figures cover 14 days ending 2026-08-23, from direct first-party API traffic only.

### 4. anth-invalidation-hierarchy — CONFIRMED (attribution note)
The prompt-caching "What invalidates the cache" table matches the claim:
- Tool definitions invalidate every tier.
- Web search, citations and speed changes keep the tools cache but invalidate system and messages.
- `tool_choice` and images keep tools and system but invalidate messages.
- Thinking and effort changes always invalidate messages, and on some models also tools and system.
- Dropped thinking blocks (Fable 5.1, Mythos 5.1, Opus 5.5) change the cache from that block onward.
- A system-prompt change invalidates system and messages, following the hierarchy statement.

Attribution note:
- **Model changes:** neither cited page says a model change invalidates the cache. The fact is documented in cache-diagnostics (`model_changed`: "The cache is per-model") and in the Claude Code prompt-caching doc. Cite those pages.
- **Speed changes:** the fast-mode page says only that switching speed invalidates the prompt cache, without naming tiers. The per-tier split comes from the prompt-caching table.

### 5. anth-cache-hidden-miss-causes — CONFIRMED
- **Lookback:** 20 positions, counting the breakpoint itself. On the Claude API, a run of `tool_use` blocks or a run of `tool_result` blocks counts as one position.
- **Minimum cacheable prefix:**
  - 512: Fable 5.1, Mythos 5.1, Opus 5.5, Opus 5, Fable 5, Mythos 5
  - 1,024: Opus 4.8, Sonnet 5, Sonnet 4.6, Sonnet 4.5
  - 2,048: Opus 4.7, Mythos Preview, Haiku 3.5
  - 4,096: Opus 4.6, Opus 4.5, Haiku 4.5

  Shorter prefixes are processed without caching and return no error.
- **Concurrency:** an entry becomes available only after the first response begins.
- **Isolation:** per workspace on the Claude API, Claude Platform on AWS and Foundry; per organization on Bedrock and Google Cloud.
- **Thinking blocks:** earlier Opus and Sonnet models and all Haiku models strip them on a non-tool-result user turn.

The bundled `prompt-caching.md` agrees on all of these. Its cache date is wrong (see the cross-cutting note).

### 6. anth-cache-diagnostics-beta — CORRECTED
Confirmed:
- Header `cache-diagnosis-2026-04-07`.
- `diagnostics.previous_message_id` is sent on every turn, as null on the first.
- The reasons are `model_changed`, `system_changed`, `tools_changed`, `messages_changed`, `previous_message_not_found` and `unavailable`.
- Fingerprints are hashes and token-count estimates only. They expire after a short period and must come from the same organization and workspace.
- Claude API only. Claude Platform on AWS, Bedrock, Google Cloud and Foundry are all "not available".

Corrections:
1. **`cache_missed_input_tokens` is not an exact or billing figure.** The docs say it is derived from byte lengths before tokenization and is a "magnitude indicator rather than a billing number". It can even exceed `usage.input_tokens`. Token Bill must not use it as dollar ground truth.
2. **The divergence point is reported per component.** It is the first of model, system, tools or messages that differs, not a byte offset. Only the earliest divergence is reported.
3. **`unavailable` also covers `output_format`, and conversations whose divergence lies beyond the comparison horizon.**
4. **ZDR eligibility is qualified.** It excludes Covered Models (Fable and Mythos 5.x).

### 7. anth-ttl-choice-keepalive — CONFIRMED (important context omitted)
Confirmed from the guide and the prompt-caching page:
- The 1-hour cache becomes cheaper at about 1 turn in 30 after a pause, and Anthropic recommends a 1-in-20 rule.
- The keep-alive re-sends the previous request with `max_tokens: 0` within 4 minutes of the previous request's start, then every 4 minutes, and drops `stream`. It bills only the cache read.
- On Fable 5.1, keep-alive is cheaper for pauses of minutes, and the 1-hour cache wins near 45 minutes.
- `max_tokens: 0` is rejected with `stream: true`, `thinking.type: "enabled"` (the claim omits this one), structured outputs, forced `tool_choice`, and inside batches.

Omitted context that matters for a simulator:
- On Sonnet 5 and Opus 5, keep-alive saved nothing measurable over the 1-hour TTL, and cost more when a pause came before every turn.
- Opus 5.5 (0.05x reads) was not measured. The report's P2 "keep-alive scheduler (Fable 5.1 / Opus 5.5 economics)" is therefore an extrapolation for Opus 5.5.

### 8. anth-cache-rate-limits — CONFIRMED
The rate-limits page confirms:
- Only `input_tokens` and `cache_creation_input_tokens` count toward ITPM. Haiku 3.5 (footnote 4) is the exception.
- The docs' own example: 2M ITPM at an 80% hit rate gives 10M effective input tokens per minute.
- OTPM counts actual output, and `max_tokens` doesn't factor in.
- Monthly spend caps are $500 / $1,000 / $200,000 for Start / Build / Scale. Custom has no cap.

Rate-limit pools, from the footnotes:
- Opus 5.5 and Opus 5 each have their own limit.
- Opus 4.8, 4.7, 4.6 and 4.5 share one combined limit.
- Fable 5.1 and Fable 5 share one limit; Mythos has a separate shared limit.
- Sonnet 5 has its own limit.

### 9. anth-cache-preserving-apis — CONFIRMED (minor gaps)
Confirmed from the mid-conversation system messages page:
- Mid-conversation `role: "system"` messages need no header. They work on Fable 5.1, Mythos 5.1, Fable 5, Mythos 5, Opus 5.5, Opus 4.8 and Opus 5, but not Sonnet 5. They are available on the Claude API, Bedrock and Google Cloud.
- Tool changes use the beta header `mid-conversation-tool-changes-2026-07-01`.
- Inline tool definitions use `inline-tools-2026-09-15` and are Claude API only.
- `clear_at: "next_user_message"` uses `mid-conversation-system-clear-at-2026-08-21`. A cleared message costs no input tokens.

The effort page confirms that per-message effort uses `mid-conversation-output-config-2026-07-01` on Fable 5.1, Mythos 5.1, Opus 5.5 and Opus 5.

Release-note dates confirmed: tool changes 2026-07-24, inline tools 2026-09-22.

Gaps:
- Deleting or editing an earlier system message also invalidates later thinking blocks on **Opus 5.5**, not only on Fable 5.1.
- Inline tools have an exception: when the `tools` array has no non-deferred tool, the first tool defined inline costs one full cache miss.

### 10. anth-batch-stacking — CONFIRMED
- **Batch page:**
  - All usage is charged at 50%, and cache discounts stack with it.
  - Cache hit rates are best-effort, typically 30% to 98%. Use identical `cache_control` blocks and consider the 1h TTL.
  - Limits: 100,000 requests or 256 MB per batch. Most batches finish within 1 hour, the expiry is 24h, and results are kept 29 days.
  - `stream`, `speed` and `max_tokens: 0` are not supported in batches.
  - The `output-300k-2026-03-24` beta allows 300k-token outputs.
- **Pricing page:** the multipliers stack with batch and data residency. Managed Agents have no batch mode.
- **refusals-and-fallback page:** `fallbacks` is not supported in batches. That page was not cited.

### 11. anth-modifiers-geo-fast-priority — CONFIRMED
- **Data residency:** `inference_geo: "us"` costs 1.1x on every token category for 4.6+ models, on the 1P API and Claude Platform on AWS. Foundry's US Data Zone Standard is equivalent.
- **Regional endpoints:** the pricing page's 10% regional and multi-region premium on Bedrock and Google Cloud applies to Claude 4.5+ models.
- **Fast mode:**
  - up to 2.5x output tokens per second
  - $8/$40 on Opus 5.5 and $10/$50 on Opus 5 and 4.8
  - first-party only
  - cache and residency multipliers stack on top of it
- **Priority Tier:** no longer available for purchase. Burndown is 0.1 for cache reads, 1.25 for 5m writes, 2.00 for 1h writes and 1.1 for US geo.
- The fact that the cost endpoint omits Priority Tier costs is on the usage-cost-api page, which is not among this finding's sources.

### 12. anth-compaction-iterations-billing — CONFIRMED
- **compaction-threshold page:** the `compact_20260112` trigger defaults to 150000 input tokens, with a minimum of 50,000. Top-level `input_tokens` and `output_tokens` exclude compaction iterations, so totals must be summed over `usage.iterations`.
- **Release notes:** on-demand compaction (`compact-2026-09-04`) shipped 2026-09-14 as a separate request.
- **Guide:** compaction saved 32% on the long run and nothing on the 20-issue run. The summarization pass after the changes cost $0.21 against $0.04.

### 13. anth-context-editing-not-savings — CONFIRMED
- Defaults: trigger 100,000 input tokens, keep 3 tool uses. `clear_at_least` exists, and `applied_edits[].cleared_input_tokens` is reported.
- Clearing invalidates the cached prefix.
- Guide: context editing cost 74% more on the 20-issue run and "changed nothing" on the long run. Pruning at natural boundaries saved 39%. The guide recommends clearing in a few large batches.

### 14. anth-tool-search-defer — CONFIRMED
- **Tool search page:**
  - A typical multi-server setup uses about 55k tokens of definitions. Tool search cuts this by over 85%, loading 3–5 tools.
  - Selection accuracy degrades past 30–50 tools. Use tool search at 10 or more tools or over 10k tokens of definitions.
  - Discovered tools are appended and the prefix is untouched.
  - Maximum 10,000 deferred tools, and it isn't metered separately.
- **Guide:** cost stayed flat with tool search, 45% less at 502 tools, and 20% less with the GitHub MCP server. Accuracy was 15 to 18 of 20 either way.

### 15. anth-ptc-code-exec — CONFIRMED
- **Programmatic tool calling page:**
  - An average 11% higher score with 24% fewer input tokens on BrowseComp and DeepSearchQA.
  - About 38% fewer billed input tokens on a 75-tool agent.
  - Typical savings of 20% to 40% at 10 to 49 tools.
  - About 8% more cost on τ²-bench.
  - Tool results from programmatic invocations don't count toward input or output tokens.
  - Not available on Bedrock or Google Cloud.
- **Pricing page:** code execution is free with web_search or web_fetch 20260209+. Otherwise there is a 5-minute minimum, 1,550 free hours per month, then $0.05/hour per container.
- **Guide:** the CSV case was 25/25 at $0.40 versus 6/25 at $5.01.

### 16. anth-web-search-controls — CONFIRMED (context note)
- Web search costs $10 per 1,000 searches, and result tokens stay in context on later turns.
- `web_search_20260209` adds dynamic filtering, with no extra charge for code execution.
- `web_search_20260318` adds `response_inclusion: "excluded"`, which drops consumed pairs from the response.
- `max_uses` caps searches.
- Web fetch: a 10 kB page is about 2,500 tokens, a 500 kB PDF about 125,000, and `max_content_tokens` limits it.

Context note: the automatic 5-minute cache write on server-tool results happens only when the request already has prompt caching enabled. It is documented on the tool-use-with-prompt-caching page, which was not cited.

### 17. anth-effort-rerun-budgets — CONFIRMED
- **Guide:**
  - On Opus 5, `medium` gave up about 2 points for half the cost, and `low` about 8 points for a quarter.
  - Running at `low` and re-running failures at the default reached about 93% at about $0.45, versus 91.7% at $0.93.
  - On Fable 5.1, task budgets cut cost 44% for about 3 points and 58% for 6 points.
  - Opus 5.5 defaults to `medium`; `high` is the default on most other models.
- **Task budgets page:**
  - minimum 20,000 tokens
  - advisory
  - a changed budget value breaks the cache
  - not supported on Sonnet 5, Opus 4.6, Sonnet 4.6, Haiku 4.5, or in Claude Code and Cowork
- **Prompt-caching page:** an effort change always invalidates messages.

### 18. anth-cost-per-solved-task — CORRECTED
Confirmed:
- SWE-bench Pro subset: Fable 5.1 at `low` solved 88.6% at $0.54 per solved task, versus Sonnet 5 at 77.4% for $0.84.
- Terminal-Bench 3 cost per solved task: $183, $63 and $28.
- Fable 5 → 5.1 was 43% less per solved task.
- Fable 5.1 post: about 25% cheaper on typical workloads and up to about 45% on agentic ones, measured over four weeks of actual August 2026 usage.

Corrections:
1. **The Opus 5.5 quote is spliced.** The post says it "costs 40% less to run than Opus 5". Elsewhere it says that "at default settings" it will cost 40% less on typical workloads.
2. **The same guide gives counter-evidence the finding leaves out.**
   - On DeepResearch Bench II, Fable 5 → 5.1 costs 41% more per task at `high` and 79% more at `low`.
   - At Fable 5.1's default effort, it is 41% more per solved task than Sonnet 5.
   - The guide explicitly says to measure on your own workload.

   The principle holds, but the direction of these specific savings does not generalize.

### 19. anth-tokenizer-inflation — CONFIRMED
- **Pricing page:** Claude 4.7 and later models (and Mythos Preview) produce about 30% more tokens.
- **Opus 5.5 migration guide:** roughly 1x to 1.35x.
- **Token counting page:**
  - free
  - 5,000 / 10,000 / 20,000 RPM, separate from message creation
  - rejects server tools except the advisor tool
  - rejects the MCP connector
  - rejects url and file image or document sources
  - returns an estimate

### 20. anth-output-hygiene — CONFIRMED (attribution and scope notes)
All numbers were found in the guide:
- The one-line format used 39% fewer output tokens and cost 14% less. The memo cost $1.40 versus $0.57 for the original two-line format ($0.49 for one-line).
- A 16,384 cap ended 15% of Opus 5 attempts and 43% of Fable 5.1 attempts.
- At 64,000, 2 of about 14,000 turns were cut.
- Fable 5.1 solved 58.5% versus 36.3%.
- At 128k, cost per solved task was the same.

Scope:
- The 58.5% vs 36.3% result comes from an internal repository-task benchmark. On the SWE-bench Pro subset the cap made no difference (94 of 100 at either cap).
- The capped runs' cost per solved task was about the same ($21 versus $22), so the loss is solves, not dollars.

Attribution: the statement that thinking bills as output regardless of display is not in the cited guide. It is on the adaptive-thinking ("Steering thinking") page, which says billing is the same regardless of the `display` setting.

### 21. anth-prompt-audit-migration — CONFIRMED
- The guide and the bundled `cost-optimization.md` both give the same results:
  - +36% per ticket on Opus 5 with prompts written for Opus 4.8
  - audited prompts 14% cheaper, with accuracy 97% versus 92%
  - Sonnet 4.6 → Sonnet 5 audit 14% cheaper
- The guide lists "verify twice" as an example of an over-specific instruction.
- The bundled date is wrong (see the cross-cutting note).

### 22. anth-vision-tokens — CONFIRMED
- **Vision page:** tokens = ⌈w/28⌉ × ⌈h/28⌉. There are two tiers:

  | Tier | Models | Max long edge | Max visual tokens |
  |---|---|---|---|
  | High-resolution | Claude 4.7+ | 2576 px | 4784 |
  | Standard | All other models | 1568 px | 1568 |

  A 1920×1080 image costs 1560 tokens on standard and 2691 on high-res. High-res uses up to about 3x the visual tokens.
- **Prompt-caching page:** adding or removing images invalidates messages.
- **Claude Code prompt-caching doc:** Claude Code removes old images in batches, and the next request reprocesses from the earliest affected message.

### 23. anth-admin-usage-cost-api — CONFIRMED
- **Usage-cost-api page:**
  - Bucket maximums: 1m = 1,440, 1h = 168, 1d = 31.
  - Filter and group by API key, workspace, model, service tier, context window, `inference_geo` and speed. Speed needs the fast-mode beta header.
  - Cost is daily only, in USD decimal strings in cents.
  - Code execution appears only in the cost endpoint. Priority Tier costs are excluded.
  - Data typically arrives within 5 minutes, with polling once per minute.
  - Requires an Admin key, `org:admin` OAuth, or an unscoped personal or service key.
  - Not available on Claude Platform on AWS.
- **API reference:**
  - The usage report has `uncached_input_tokens`, `cache_creation.ephemeral_1h/5m_input_tokens` and `server_tool_use.web_search_requests`.
  - The cost report has `cost_type`, `token_type`, `inference_geo` and `model`.

### 24. anth-claude-code-analytics-otel — CORRECTED
Confirmed:
- **Claude Code Analytics API:**
  - per user per day: sessions, lines of code, commits, PRs, and accept and reject counts for Edit, MultiEdit, Write and NotebookEdit
  - per-model tokens, including `cache_read` and `cache_creation`, and `estimated_cost` in cents
  - up to 1h delay, free, and Claude API only
- **Enterprise Analytics API:**
  - endpoints `usage_report`, `user_usage_report`, `cost_report` and `user_cost_report`
  - products include `claude_code`, `cowork` and `chat`
  - `rbac_group_id`
  - `amount` is post-discount and pre-credit; `list_amount` is also returned
  - about 1-day lag
- **OpenTelemetry:** `claude_code.cost.usage`, and `claude_code.token.usage` with type input, output, cacheRead or cacheCreation, `model`, `query_source`, `effort` and `speed`.

Corrections:
1. **`user.email` is not populated for API-key, Bedrock, Google Cloud or Foundry sessions.** Only `user.id` and `session.id` are set there, and admins must attach identity themselves via `OTEL_RESOURCE_ATTRIBUTES`. It is populated with Claude-account sign-in or through the Claude apps gateway.
2. **There is no plain "repo" attribute.** Repository identity is the opt-in `vcs.*` attribute set: `OTEL_METRICS_INCLUDE_REPOSITORY`, default false, Claude Code v2.1.269+.
3. **Attribute names are `skill.name` and `mcp_server.name`.**
4. **OTel cost metrics are list-price approximations** unless `modelPricing` is set.

### 25. anth-claude-code-cost-physics — CORRECTED
Confirmed:
- **costs doc:** about $13 per developer per active day, $150–250 per month, and under $30 per active day for 90% of users.
- **modelPricing:** a managed setting for contract rates (v2.1.242+).
- **Claude Code prompt-caching doc:**
  - The TTL is 5 minutes for API keys, cloud providers and usage credits, and 1 hour for the main conversation on a subscription within plan.
  - Overrides: `promptCacheTtl`, `CLAUDE_CODE_PROMPT_CACHE_TTL`, `ENABLE_PROMPT_CACHING_1H`, `FORCE_PROMPT_CACHING_5M`.
  - `opusplan` toggles, automatic fallback on Fable, Opus 5.5 and Opus 5, and skill `model:` frontmatter are each a model switch.
  - Effort changes keep the cache on Opus 5.5 and Fable 5.1 with an API key or subscription (not on Bedrock or Google Cloud).
  - Only the first fast-mode enable is a miss.
  - Other cache breakers: MCP servers loaded into the prefix, `/compact`, image eviction and upgrades.
  - A gateway that strips markers bills the entire history as uncached on every turn.
  - Worktrees don't share a cache.

Correction: the ~7x figure is conditional. Agent teams use about 7x more tokens *when teammates run in plan mode*. The finding states it unconditionally; the report body correctly says "(plan mode)".

### 26. anth-orchestrator-advisor — CONFIRMED (attribution note)
Confirmed in the guide:
- A Fable 5.1 lead with 25 Sonnet 5 workers cost 47%–55% less and scored 10–12 points lower, in about 2.3h versus 15–20h.
- On a deliberately easy slice of BrowseComp, a Fable 5 coordinator with one Sonnet 5 worker cut the p90 cost from $33 to $12.
- The advisor's value depends on the consult rate. At low effort, the executor can stop consulting and score below the executor alone.

Attribution: "caches are model-scoped" is not in the guide. It is stated on the cache-diagnostics page ("The cache is per-model") and the fallback-credit page ("Prompt caches are per-model").

### 27. anth-governance-controls — CORRECTED
Confirmed:
- Org spend caps of $500 / $1,000 / $200,000, with none on Custom.
- Workspace spend and rate limits, which can't be set on the default workspace.
- The Claude Code workspace is limited separately.
- Managed Agents cost $0.08 per session-hour while running, with no batch mode.
- The fallback credit uses `fallback-credit-2026-07-01`. The retry is billed as if the conversation had been on the new model, and `cache_read` rises by the amount `cache_creation` falls.

Correction: session budgets (2026-08-07) are priced at public list rates. A session that reaches its budget **pauses** with `budget_reached`, and changing or removing the budget resumes it. It is not terminated.

### 28. anth-evidence-drift — CORRECTED
The drift itself is real. The bundled `cost-optimization.md` says:
- caching: a factor of 2.5 to 3.7
- re-run failures: $0.70 versus $1.39
- task budgets: 18% and 47%
- compaction: 38%

The live guide says 2.7–5.3, $0.45 versus $0.93, 44% and 58% (on Fable 5.1), and 32%. The price moves are also confirmed:
- Sonnet 5 locked 2026-08-10
- Fable 5.1 reads cut 75% on 2026-09-01
- Opus 5.5 on 2026-09-22 at 20% lower list prices and 60% lower reads than Opus 5

Corrections:
1. **The bundled guide is not a "June 2026" document.** It references Opus 5 (2026-07-24) and Fable 5.1 (2026-09-01), so these figures changed within weeks, not across a quarter. That makes the case for versioning even stronger.
2. **Some of the pairs aren't like-for-like re-measurements.** The bundled task-budget figures ("measured on coding") are not attributed to Fable 5.1, and the compaction figures come from different runs (a long triage run versus the long SWE run). Treat them as different evidence snapshots, not necessarily the same experiment re-run.

## Summary

- 22 confirmed, several with attribution or scope notes (#4, 7, 9, 11, 16, 20, 26).
- 6 corrected:
  - #6 `anth-cache-diagnostics-beta`
  - #18 `anth-cost-per-solved-task`
  - #24 `anth-claude-code-analytics-otel`
  - #25 `anth-claude-code-cost-physics`
  - #27 `anth-governance-controls`
  - #28 `anth-evidence-drift`
- 0 unverifiable.
- 0 refuted.

Implementation-relevant takeaways for Token Bill:
- Don't treat `cache_missed_input_tokens` as billing truth.
- Don't assume `user.email` exists in OTel for API-key fleets.
- Keep-alive helps only when cache reads are cheap (Fable 5.1). It was not measured on Opus 5.5.
- Fix the bundled-skill date label throughout the report.
