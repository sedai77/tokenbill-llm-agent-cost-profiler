# Verification: papers-caching track

Checked 2026-09-23 by an adversarial fact-checker. I opened every source listed below myself. I used WebFetch, and I used curl plus pdftotext for the arXiv PDFs and HTML so I could grep the exact numbers. Repo claims were re-checked against `tokenbill/pricing.py` and `tokenbill/trace.py`. Local evidence (PDF text extracts) is in `research/verify_tmp/`.

Verdict counts: **16 confirmed, 9 corrected, 0 unverifiable, 0 refuted.**

| id | verdict | one-line reason |
|---|---|---|
| anth-invalidation-hierarchy | confirmed | Matches both Anthropic invalidation tables and the mid-conversation page |
| anth-ttl-start-1h | confirmed | TTL-from-start quote, A/B/C billing, 5m/1h split, and 5m server-tool breakpoints all verbatim |
| anth-lookback-20 | confirmed | Claude API text and Bedrock "approximately 20 content blocks" both verbatim |
| anth-concurrency-fanout | confirmed | "only becomes available after the first response begins"; CC 5 s hold verbatim |
| anth-workspace-fleet-scope | confirmed | Workspace vs org isolation; CC "one machine and directory" verbatim |
| anth-pricing-2026 | confirmed | Every price and multiplier matches pricing.md; repo gaps confirmed in pricing.py |
| anth-fleet-benchmarks | confirmed | All four numbers verbatim on the optimizing-for-cost page |
| anth-cache-diagnostics | confirmed | Header, reason types, `unavailable` scope, null + low reads, ZDR, API-only all verbatim |
| gu-audit-icml25 | confirmed | 8/17, 7 global, per-org for Claude 3 Haiku and GPT-4o mini, 25 victim requests, ≥5 providers changed |
| capc-two-tier-rho | **corrected** | The 51.7% is vs **vanilla**, not vs cache-only |
| dont-break-cache-agents | confirmed | All percentages found in the PDF tables and text |
| practice-hit-rate-slo | confirmed | Manus (2025-07-18) and Shihipar (2026-04-30) statements match |
| cc-invalidators-taxonomy | confirmed | Every item is on code.claude.com (CLAUDE.md edits also apply after /compact) |
| cc-ttl-managed-settings | confirmed | Default-TTL table, controls, managed settings, and beta-header forwarding all verbatim |
| gateway-strip | **corrected** | The block that bills uncached after a 400 is the appended mid-conversation system-context block, not the system prompt; the conversation stays cached |
| nondeterministic-tools | **corrected** | Issue #49038 shows **56,296 → 32**; 56,370 is the post-fix write |
| keepalive-economics | **corrected** | Paper and derivations check out; the Fable 5.1 keepalive advice is in the bundled skill doc, not the public page |
| token-reduction-not-cost | **corrected** | v5 is dated **2026-08-12** (v1 2026-07-13), not "July 2026" |
| context-editing-cost | confirmed | 74% / 39% / 32% verbatim; batching advice on the public page; Leyline quote verbatim |
| write-without-read | confirmed | $2.06 vs $2.03, 512,521 writes, $0.374 (5.4x); Anthropic "same trap"; GPT-5.6 1.25x |
| openai-2026-caching | confirmed | Modes, ≤4 writes, 1.25x/0.1x, 30m TTL, fields, 15 RPM, 60→87%, 40–80% |
| gemini-bedrock-deepseek-rules | **corrected** | Gemini cached input is **10%** of input for every listed model, not "10–20%" |
| gateway-isolation-keypooling | confirmed | KeyPooling numbers verbatim; BYOK isolation supported by KeyPooling (0/5 on its route) and CacheProbe |
| coding-agent-traffic-serving | **corrected** | llm-d's 57x is vs approximate routing; ~2x throughput is vs cache-blind routing (+25% vs approximate) |
| non-prefix-reuse | **corrected** | RAGCache is order-sensitive (a prefix tree), not non-prefix reuse |
| semantic-cache-pitfalls | **corrected** | Injection success is "over 0.82", not 81% |
| batch-plus-cache | confirmed | Stacking in pricing.md; "best-effort basis" in the batch-processing doc; Bedrock and OpenAI Flex numbers verbatim |

---

## Per-finding notes

### anth-invalidation-hierarchy — confirmed
- **prompt-caching** "What invalidates the cache" table:
  - Tool definitions invalidate all tiers.
  - Web search, citations and the speed setting invalidate system and messages.
  - `tool_choice` and images invalidate messages only.
  - Thinking and effort: "always invalidates message blocks", and tools/system too "on models that render the configuration ahead of them".
- **tool-use-with-prompt-caching** adds `disable_parallel_tool_use` (messages only). Its per-tool table covers web fetch: "Enabling or disabling invalidates the system and messages caches".
- **Thinking stripping** happens on "earlier Opus/Sonnet models and all Haiku models" (earlier than Opus 4.5 / Sonnet 4.6). The finding's wording "Haiku 4.5 and older Opus/Sonnet" is consistent.
- **mid-conversation-system-messages** documents these alternatives:
  - `tool_addition` and `tool_removal` blocks (beta `mid-conversation-tool-changes-2026-07-01`)
  - `clear_at` (beta `mid-conversation-system-clear-at-2026-08-21`)
  - per-message effort (beta `mid-conversation-output-config-2026-07-01`)
- None of these alternatives works on Sonnet 5.
- **Side note:** the prompt-caching page also names `inline-tools-2026-09-15` for `tool_addition`.

### anth-ttl-start-1h — confirmed
- Verbatim: "measured from the start of the request that writes or reads the cache entry, not from the end of its response". Its 4-minute example is on the page.
- Verbatim: "refreshed for no additional cost".
- pricing.md: 5m writes are 1.25x and 1h writes are 2x.
- The A/B/C positions and the rule that longer TTLs must come first are verbatim.
- The `ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens` usage fields are documented.
- tool-use-with-prompt-caching: the automatic breakpoint on server-tool results "always uses the default 5-minute TTL". It applies only when the request already has a `cache_control` marker.

### anth-lookback-20 — confirmed
- Verbatim: "checks at most 20 positions per breakpoint, counting the breakpoint itself as the first".
- The tool_use/tool_result run rule is verbatim, and the block 15 / 35 example is on the page.
- "In a growing conversation the final block works as long as each turn adds fewer than 20 blocks."
- Bedrock: "looking back up to approximately 20 content blocks".

### anth-concurrency-fanout — confirmed
- Verbatim: "a cache entry only becomes available after the first response begins … wait for the first response before sending subsequent requests."
- The bundled `shared/prompt-caching.md` says to "await the first streamed token".
- code.claude.com: "holds all but the first for up to 5 seconds by default".

### anth-workspace-fleet-scope — confirmed
- Workspace isolation applies on the Claude API, Claude Platform on AWS and Foundry. "Bedrock and Google Cloud maintain organization-level cache isolation."
- code.claude.com, verbatim: "the cache is effectively scoped to one machine and directory". It lists cwd, platform, shell, OS version, auto-memory paths and the git-status snapshot.
- The worktree miss and the Agent SDK per-machine suppression are both documented.

### anth-pricing-2026 — confirmed
- pricing.md:
  - Opus 5.5 is $4/$20, with cache reads at $0.20 (footnote: 0.05x).
  - Fable 5.1 and Mythos 5.1 read at 0.025x.
  - 1h writes are 2x.
  - Verbatim: "These multipliers stack with other pricing modifiers, including the Batch API discount and data residency."
  - `inference_geo:"us"` is "1.1x … on all token pricing categories" for 4.6+ models.
  - Fast mode: Opus 5.5 $8/$40; Opus 5 / 4.8 $10/$50.
  - "approximately 30% more tokens for the same text" for 4.7+ models.
- Minimum prefix list: 512, 1,024, 2,048 or 4,096 depending on model. It is non-monotonic across generations.
- Repo check:
  - `pricing.py` has no rows for Opus 5.5, Mythos 5.1, Opus 4.5 or Sonnet 4.5.
  - There is only one write multiplier (1.25), and no batch, geo or fast-mode modifiers.
  - `trace.py` hard-codes `CHARS_PER_TOKEN = 3.7`.

### anth-fleet-benchmarks — confirmed
optimizing-for-cost-and-intelligence.md, verbatim:
- "median 84% … top 10% … 94% or more"
- "$37.94 to $7.12 … $3.20 to $1.20"
- "25-token status line … $4.24 per run instead of $0.59, more than running with caching off"
- "more than about 1 gap in 20 … 15% less … Sonnet 5 and 11% less on Claude Opus 5"

### anth-cache-diagnostics — confirmed
- Beta header `cache-diagnosis-2026-04-07` on every turn, with `diagnostics.previous_message_id`.
- Reason types: `model_changed`, `system_changed`, `tools_changed`, `messages_changed`, `previous_message_not_found`, `unavailable`.
- `unavailable` covers differences in `tool_choice`, `thinking`, `context_management`, `output_config`, `output_format` or the beta-header set.
- The `*_changed` types carry `cache_missed_input_tokens`, described as an estimate "derived from byte lengths".
- A null result with low reads means "requests match but the cache entry was no longer available".
- Fingerprints are hashes and token estimates only; the feature is "ZDR eligible (qualified)".
- Available on the Claude API only.

### gu-audit-icml25 — confirmed
- Checked the arXiv HTML (v2) and PMLR v267 pp. 20477–20496.
- "prompt caching in 8 out of 17 API providers"; "global cache sharing in 7 providers".
- Anthropic Claude 3 Haiku and OpenAI GPT-4o mini showed per-organization sharing.
- OpenAI and Azure text-embedding-3-small "required NumVictimRequests=25 … multiple servers with separate caches … randomly routed".
- Disclosed October 2024; "at least five providers made changes".
- The text-embedding-3-small issue was patched.
- Dates: v1 2025-02-11, v2 2025-07-13.

### capc-two-tier-rho — corrected
**Correct claim:** CAPC cut cost a mean 49% vs cache-only on LongBench-v2 (range 24–67%). On the 94k-token tools prefix it cut **51.7% vs vanilla** in the simulator run. The measured end-to-end result was −45.5% vs vanilla and −16% vs cache-only.

What checks out (PDF, Yan Song, PayPal Inc., arXiv 2607.15516v1, 2026-07-17):
- Table 2: at 2,053 tokens ρ is 0.53 / 0.63 / 0.83 at N = 5 / 10 / 30. At ≥4,096 tokens ρ = 1.0 "from the first subsequent call". T ≈ 3,500.
- Billing within 1%.
- A prepended space still hit; the paper attributes this to leading/trailing whitespace normalization.
- Implicit caching of large `tools=` arrays: about 106k cache reads with no markers.
- τ-bench: +40.1% vs vanilla for query-aware compression.
- Formula (6): ρ_cross = (α−1/r)/(α−β).

What's wrong in the finding: it says the 51.7% cut is on top of the "vs cache-only" baseline. Table 10 compares against vanilla. There, cache-only is itself −18.3% vs vanilla, because Anthropic already implicitly caches the tools array.

### dont-break-cache-agents — confirmed
- Checked the PDF v2 (Lumer et al., PricewaterhouseCoopers U.S.).
- Savings and TTFT by model:

  | model | cost saving | TTFT improvement |
  |---|---|---|
  | GPT-5.2 | 79.6% | 13.0% |
  | Sonnet 4.5 | 78.5% | 22.9% |
  | Gemini 2.5 Pro | 41.4% | 6.1% |
  | GPT-4o | 45.9% | 30.9% |

- Regressions: GPT-4o full-context caching −8.8%; Gemini with tool results excluded −2.9%.
- Savings are 10–45% at 500 tokens and 54–89% at 50k tokens. There are TTFT regressions of 10–18% below the provider minimums.
- The three recommendations are in the paper.
- The abstract says "over 500 agent sessions".
- Dates: v1 2026-01-09, v2 2026-01-31.

### practice-hit-rate-slo — confirmed
- Manus, by Yichao 'Peak' Ji, 2025-07-18. It calls KV-cache hit rate "the single most important metric". Other points in the post:
  - input:output ≈ 100:1
  - $0.30 vs $3 per MTok
  - avoid timestamps at the start of the prompt
  - append-only context and deterministic serialization
  - session-ID routing on vLLM
  - mask tools rather than remove them
- claude.dev blog, by Thariq Shihipar, 2026-04-30. It says "We run alerts on our prompt cache hit rate and declare SEVs". Other points in the post:
  - use `<system-reminder>` instead of editing the system prompt
  - use subagents instead of switching models
  - implement plan mode as EnterPlanMode/ExitPlanMode tools
  - use `defer_loading` stubs
  - cache-safe compaction

### cc-invalidators-taxonomy — confirmed
- code.claude.com lists every invalidator named in the finding:
  - `/model`, `opusplan` toggles, automatic fallback, skill frontmatter `model`
  - effort changes, except on Opus 5.5 / Fable 5.1 with an API key or subscription (Bedrock, Vertex, the apps gateway and `DISABLE_EXPERIMENTAL_BETAS` still invalidate)
  - the first turn with fast mode on
  - MCP changes when tools sit in the prefix (tool search off, custom `ANTHROPIC_BASE_URL`, `alwaysLoad`, threshold loading)
  - plugin MCP servers
  - bare deny rules without tool search
  - `/compact`
  - image eviction in batches
  - upgrades
- The cache-safe list matches.
- **Nit:** CLAUDE.md edits also take effect after `/compact`, not only after `/clear` or a restart.

### cc-ttl-managed-settings — confirmed
- Default-TTL table: the main conversation gets 5 minutes with usage credits, an API key or a cloud provider. It gets 1 hour on a subscription within plan usage. Everything else gets 5 minutes.
- Controls: `promptCacheTtl` / `CLAUDE_CODE_PROMPT_CACHE_TTL`, `subagentPromptCacheTtl`, `ENABLE_PROMPT_CACHING_1H`, `FORCE_PROMPT_CACHING_5M`. The page says to put them "in the env block of managed settings".
- The `anthropic-beta` header must be forwarded unchanged.
- "The one-hour TTL isn't available through the Claude apps gateway."

### gateway-strip — corrected
- The strip quote is verbatim, and the block-form-to-string conversion is on the page.
- The custom `ANTHROPIC_BASE_URL` gateway disabling tool search is documented.
- **Correction:** after a 400, Claude Code moves the marker off the *appended mid-conversation system-context block* (for example, file-change notices) and onto the last conversation message. That block bills uncached, but "your conversation stays cached". It does not mean the whole system prompt bills uncached.

### nondeterministic-tools — corrected
**Correct claim:** issue #49038 shows a full-prefix miss writing 56,296 tokens before the fix and 32 tokens after it.

- Codex PR #2611: opened 2025-08-23, merged 2025-08-25. It covers the HashMap ordering, the "falling below 1%" hit rate, and the fix of sorting tools by name.
- Issue #49038: opened 2026-04-16, closed as not planned. `tools[0]` (the Agent tool) listed sub-agents in nondeterministic order, and every resumed session's first call missed the prefix.
- Its table shows cache creation of **56,296 → 32**. The 56,370 in the finding is the post-fix call 1, the initial write.
- The diagnostics page names non-deterministic `input_schema` serialization as a `tools_changed` cause.

### keepalive-economics — corrected
**Correct claim:** the recommendation to use a `max_tokens:0` keepalive instead of the 1h TTL on Fable 5.1 comes from Anthropic's bundled claude-api skill (`shared/prompt-caching.md`, v2.1.280). The public prompt-caching page does not say this. It advises re-sending a pre-warm every 5 minutes and using the 1-hour TTL "for longer gaps".

- Paper checked: Khailo, arXiv 2607.19214, 2026-07-21, revised 2026-07-24.
  - I_max ≈ τ(w/r − 1): ≈46 min for Anthropic, ≈36 min for OpenAI and DeepSeek.
  - τ* ≈ 240 s. The 30 s convention "spends 8× more": $3.60/h vs $0.45/h for a 100k prefix.
  - At 600 s idle: 0/48 warm without keepalive vs 40/40 with it.
  - Post-pause cost up to 12.5× lower.
- The derivations are correct: 4 × (1.25/0.05 − 1) = 96 min, and 4 × (1.25/0.025 − 1) = 196 min. The rates come from pricing.md, where "cache hits and refreshes" are 0.05x and 0.025x.
- **Attribution problem:** I grepped the full public page (`prompt-caching.md`). It has no "keep-alive" or "cheaper" TTL advice for Fable 5.1.

### token-reduction-not-cost — corrected
**Correct claim:** arXiv 2607.12161 v5 is dated **2026-08-12**. v1 is 2026-07-13.

- Checked the HTML: Weinberger and Hozez, affiliation PointFive.
- 5,493 billed executions, 7 repos, 3 tiers, 103 tasks.
- "~87% of the reconstructed four-component cost" (about 80% of the actual bill). Tool outputs 3.3%.
- RTK-ML: 38.4% fewer tool-output tokens but +6.8% cost. Headroom proxy +48.4%. r = 0.154.
- The only error is the date.

### context-editing-cost — confirmed
- optimizing-for-cost page, verbatim: "On the 20-issue run they saved nothing, and context editing cost 74% more. On the long run the prune saved 39% and compaction 32%, while context editing changed nothing."
- Also verbatim: "clear in a few large batches rather than many small ones". It prunes "at each task boundary".
- code.claude.com recommends `/rewind` over mid-task `/compact`.
- Leyline (arXiv 2606.01065, 2026-05-31): "Production agentic harnesses fall back to re-prefill on every edit". It reports +11.2 pp.

### write-without-read — confirmed
- Parsing the Stream (Pakhomov and Nijkamp, Salesforce AI Research, 2609.01466v1, 2026-09-01): "0 cache reads and 512,521 cache-write tokens at the 1.25× premium, costing $2.06 versus $2.03 uncached". The append-only form cost $0.374 (5.4×).
- The public Anthropic page describes paying a fresh write on every request that is never read, and says automatic caching "hits the same trap". The "pure surcharge" wording is from the bundled skill.
- OpenAI GPT-5.6 writes cost 1.25×.

### openai-2026-caching — confirmed
- OpenAI guide:
  - `prompt_cache_options.mode`
  - "Each request can create up to four cache writes"
  - writes at 1.25×, reads at 0.1×
  - `ttl` "30m" is the only value and the default
  - `cached_tokens` and `cache_write_tokens` fields
  - earlier models round down to multiples of 128
  - "Traffic above 15 requests per minute can lead to overflow routing"
- Bedrock: 4 checkpoints for GPT-5.6, a 30-minute minimum TTL, and 1,024 tokens for earlier models.
- Cookbook (undated): 60% → 87% hit rate with `prompt_cache_key`. It reports "40-80% better cache utilization", which the cookbook attributes to persisted reasoning tokens via `previous_response_id`.

### gemini-bedrock-deepseek-rules — corrected
**Correct claim:** on the current Gemini pricing page, cached input is 10% of the input price for every listed model: 2.5 Pro, 2.5 Flash, 2.5 Flash-Lite, 3.x Flash, 3.x Flash-Lite and 3.1 Pro. It is not "10–20%".

What checks out:
- **Gemini caching page** (updated 2026-09-02): implicit caching on "all Gemini 2.5 and newer models". Minimums are 2,048 (2.5) and 4,096 (3.5–3.8 Flash, 3.1 Pro).
- **Gemini pricing page** (updated 2026-09-23): storage is $4.50/MTok/h for 2.5 Pro, $1.00 for 2.5 Flash, and $0.50 for 3.8 Flash through 2026-12-31.
- **Bedrock:**
  - implicit plus explicit caching, with a maximum of 4 checkpoints and a cumulative minimum
  - 1h TTL on Claude models
  - "may lead to increased cache writes" with cross-region inference
  - "not supported with the batch inference API"
  - Opus 4.7 listed at 4,096 tokens, while Anthropic says 2,048 "on every platform"
- **DeepSeek:** on by default, best effort, full match of a prefix unit required, "construction takes seconds", cleared in hours to days.

### gateway-isolation-keypooling — confirmed
- KeyPooling (arXiv 2608.17485v2, 2026-08-23, JHU/CUHK), verbatim:
  - five gateways, "none bound customers to upstream credentials by default"
  - "all five exposed cross-customer cache reads"
  - OpenRouter: 12 of 28 labels with a positive route. They carry "33.7% of the volume for eligible models" in the weekly frame, whose tests covered 80.5% of that volume. So "of tested volume" is slightly loose.
  - "All 17 public Claude Code, Cursor, and OpenCode prefixes … exceed 1,024 tokens" (Grok-2 tokenizer)
  - "1.7–2.5% cost increase"
- BYOK isolation is supported twice:
  - KeyPooling: "BYOK crossed 0/5 on this route".
  - CacheProbe (2605.30613v1, 2026-05-28, Northeastern): cross-account BYOK tests showed "no statistical significance".

### coding-agent-traffic-serving — corrected
**Correct claim:** llm-d's precise routing cut P90 TTFT 57× vs approximate routing, and raised throughput 25% vs approximate. It roughly doubled throughput vs cache-blind routing (load or random: 4,429 → 8,730 tokens/s). The finding pairs the 57× and "doubled" as if they share a baseline.

- CacheWise (2606.16824, 2026-06-15, PDF), on Claude Code traces:
  - tool-completion requests are 20× more frequent than user-initiated ones at the median
  - sessions run 36 min at the median and >2.6 h at the tail
  - prefill:decode ≈ 21× higher than chat
  - example tool durations: 49 ms and 83,333 ms (pytest)
  - evictions cut 2–2.6×
- Continuum (v1 2025-11-04, v7 2026-09-08): ">8x" job completion time.
- KVFlow (2025-07-10): 1.83× / 2.19×.
- vLLM metrics page lists `prefix_cache_queries`, `prefix_cache_hits` and `kv_block_reuse_gap_seconds`.

### non-prefix-reuse — corrected
**Correct claim:** RAGCache is order-sensitive prefix reuse, not a non-prefix technique. It caches retrieved-document KV "sensitive to the referred order" in a prefix tree, so it belongs with prefix caching.

- Prompt Cache (MLSys 2024; v1 2023-11-07, v2 2024-04-25): 8× GPU / 60× CPU.
- CacheBlend (v3 2025-04-03): 2.2–3.3× TTFT.
- EPIC (v3 2025-05-27): up to 8× TTFT / 7× throughput.
- CacheGen (SIGCOMM'24): 3.5–4.3× smaller KV.
- RAGCache: up to 4× TTFT.
- vLLM hashes chain on the parent block hash, and it supports `cache_salt`.

### semantic-cache-pitfalls — corrected
**Correct claim:** the key-collision attack (arXiv 2601.23088v1) reached an average hit rate over 0.86 and an injection success rate over **0.82**, not 81%.

What checks out:
- **Key-collision paper:** 90.6% hit rate on the agent tool-hijack scenario. Best salting reduces hit rate by 21.0 pp (semantic cache) and 10.8 pp (semantic KV cache).
- **vCache** (ICLR 2026, v5 2026-02-21): static thresholds, "up to 12.5× higher cache hit and 26× lower error".
- **Category-aware caching** (2025-10-29): hit rates 40–60% vs 5–15%; "fixed thresholds cause false positives in dense spaces".
- **Redis paper** (EMNLP 2026 Industry, v4 2026-09-01): "models with the highest PR-AUC are often the worst in operation".
- **APC** (NeurIPS 2025, v2 2026-01-26): 50.31% cost and 27.28% latency reductions.
- **LaCache** (2026-08-03): attack hit rate 0.95 → 0.01, benign hit rate about 0.93.

### batch-plus-cache — confirmed
- pricing.md: the cache multipliers stack with the Batch discount.
- The batch-processing doc, verbatim: "cache hits are provided on a best-effort basis". It is also in bundled `cost-optimization.md` § 2.5, which says "50% off every token … including cache reads and writes".
- Bedrock: "not supported with the batch inference API".
- OpenAI cookbook: "8.5% increase in cache hit rate compared to the Batch job … 23% reduction in input token cost". This was Flex with extended caching plus `prompt_cache_key`.
