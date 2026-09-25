# Token Bill research track: other providers, gateways and usage-accounting formats

Track: `provider-others`. Research date: 2026-09-23. Scope: cost levers, cache semantics and usage JSON of
OpenAI, Google Gemini (AI Studio + Vertex / "Gemini Enterprise Agent Platform"), AWS Bedrock, Claude on
Vertex / Bedrock / Microsoft Foundry / Claude Platform on AWS, Azure OpenAI (Foundry), DeepSeek, Mistral,
xAI, OpenRouter, plus the cross-cutting standards (OpenTelemetry GenAI semconv, community price maps,
token-counting endpoints) needed to build provider adapters for an enterprise's whole LLM estate.

Every claim below was read from a page or file opened during this session. "Accessed 2026-09-23" means
the page carries no visible date; where a page shows an updated/publication date it is quoted.
Numbers are USD per 1M tokens (MTok) unless stated.

---

## 0. Executive summary (what changed in 2026 and why it matters for Token Bill)

1. **OpenAI now bills cache writes.** Since GPT-5.6 (released 2026-07-09) OpenAI charges **1.25x** input for
   cache writes, reads at **0.1x**, a **30-minute minimum TTL refreshed on reuse**, up to **4 cache writes per
   request**, and explicit `prompt_cache_breakpoint` markers. This makes OpenAI's economics structurally
   identical to Anthropic's, so Token Bill's core insight (cache breakers cost real money) now applies to
   the second-largest provider too. The first public field data is dramatic: an OpenAI Codex user measured
   **$1,780 of cache writes vs $15.71 of cache reads (90% of GPT-5.6 spend)**; adding one explicit
   breakpoint raised the hit rate from **0% to 98.6%**.
2. **Providers now tell you why the cache missed.** OpenAI's Responses API returns
   `prompt_cache_diagnostics` with nine miss reasons (model, key, tools, text format, reasoning effort,
   verbosity, compaction, input, service tier) at no cost; Anthropic has an equivalent first-party beta.
   Token Bill should ingest these as ground truth and use them to validate its own breaker classifier.
3. **"Tokens x list price" is no longer a correct cost model.** A single call's price now depends on:
   provider, *channel* (1P vs Bedrock vs Vertex vs Foundry vs OpenRouter), endpoint geography
   (global vs regional/multi-region/data-zone: **+10%** on Bedrock, Vertex, Azure Data Zone, OpenAI
   regional processing, Anthropic `inference_geo: "us"`), **service tier** (flex 0.5x, priority 1.75x–2x,
   ultrafast), batch (0.5x on most; 0.8x on some xAI), long-context band (>200K or >272K; xAI and OpenAI
   bill the whole request at the long rate), **time of day** (DeepSeek peak/off-peak 2x), **effective date**
   (Gemini 3.8 Flash promo prices double on 2027-01-01; GPT-5.6 Sol repriced 2026-08-21), cache TTL
   (5m/1h/30m/24h), and negotiated discounts (CCU private offers, PTU reservations). A tool that ignores the
   tier already produces 50% errors (documented in a competing tool on 2026-09-17).
4. **Usage JSON semantics are inconsistent in a way that silently double-counts.** Anthropic and Bedrock
   Converse report `input_tokens` *excluding* cache; OpenAI, Azure, Gemini, DeepSeek, xAI, Mistral,
   OpenRouter and the OpenTelemetry GenAI convention report input *including* cache. A canonical usage model
   with explicit inclusive/exclusive normalization is the foundation of every adapter.
5. **Enterprise attribution is now native on every hyperscaler** (Bedrock `requestMetadata` + CUR 2.0 IAM
   principal, Vertex request `labels`, Foundry project tags, OpenAI Usage API `group_by`
   project/user/api_key/service_tier, OpenRouter `workspace_id`/`session_id`), but each covers different
   endpoints and none gives per-request dollars on the invoice. Token Bill can own the join: per-request
   cost from traces, reconciled to invoice totals.
6. **Machine-readable prices exist and should replace hand-typed tables**: AWS Price List API (free,
   public, encodes global vs regional, batch, 1h writes, tiers), OpenRouter `/api/v1/models` (public, per-
   token, cache read/write, long-context overrides), and the LiteLLM price map (4,214 entries, includes
   tier and off-peak fields). They disagree with primary sources in places, so they must be cross-checked.

---

## 1. OpenAI (direct API)

### F1. GPT-5.6+ explicit prompt caching with billed cache writes (new, July 2026)
- **Facts.** For GPT-5.6 and later: minimum **1,024 visible input tokens**; cache writes billed at
  **1.25x** uncached input; reads at **0.1x**; `prompt_cache_options.ttl` = `"30m"` (only value, default),
  entry "remains eligible for reuse for 30 minutes after its most recent write or reuse" and reuse refreshes
  it without another write charge. `prompt_cache_options.mode`: `implicit` (default; OpenAI places one
  breakpoint at the latest message and writes up to the latest three explicit breakpoints) or `explicit`
  (only developer breakpoints, up to four writes; **no breakpoints means no caching and no write charge**).
  Breakpoints are `"prompt_cache_breakpoint": {"mode": "explicit"}` on `input_text`/`input_image`/
  `input_file` blocks (Responses) or `text`/`image_url`/`input_audio`/`file` (Chat Completions); top-level
  `instructions` cannot hold one. For matching, OpenAI "considers up to the latest 80 breakpoints in the
  conversation, without a content-block lookback limit" (SDK docstring). Pre-5.6 models: no write charge.
  "Writing a prefix once and fully reusing it once costs 1.35x ... compared with 2x" (guide).
- **Usage fields.** Responses: `usage.input_tokens_details.cached_tokens`, `...cache_write_tokens`;
  Chat Completions: `usage.prompt_tokens_details.cached_tokens`, `...cache_write_tokens`
  (SDK types, commit 2026-09-02).
- **Current prices (Standard, per MTok; input / cached / cache write / output):** gpt-6-astra
  10 / 1.00 / 12.50 / 50; gpt-6-sol 2 / 0.20 / 2.50 / 10; gpt-6-luna 0.10 / 0.01 / 0.125 / 0.50;
  gpt-5.6-sol 4 / 0.40 / 5 / 20; gpt-5.6-terra 2 / 0.20 / 2.50 / 12; gpt-5.6-luna 0.20 / 0.02 / 0.25 / 1.20.
  Older: gpt-5.5 5 / 0.50 / – / 30; gpt-5.4 2.50 / 0.25 / – / 15; gpt-5 1.25 / 0.125 / – / 10;
  gpt-4.1 2 / 0.50 (75% off) / – / 8; o1 15 / 7.50 (50% off) / – / 60.
- Sources: https://developers.openai.com/api/docs/guides/prompt-caching (accessed 2026-09-23);
  https://developers.openai.com/api/docs/pricing (accessed 2026-09-23);
  https://developers.openai.com/api/docs/changelog (entries 2026-07-09 GPT-5.6, 2026-08-21 Sol reprice,
  2026-09-03 GPT-6 Astra, 2026-09-22 GPT-6 Sol/Luna);
  https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_create_params.py
  and .../response_usage.py, .../completion_usage.py (commit 6f0da165, 2026-09-02);
  https://aihubmix.com/blog/gpt-5-6-is-live-prompt-caching-billing-changes-explained (2026-07-31).

### F2. Field evidence: implicit mode on agents writes caches that are never read
- **Facts.** openai/codex issue #35300 (opened 2026-07-25): Codex cannot emit `prompt_cache_breakpoint`.
  Measured: unmodified Codex **0% hit rate, ~9,060 cache-write tokens per request**; with one explicit
  breakpoint **98.6% hit rate, 123–128 write tokens per request**. Provider billing for July 2026:
  **258.8M cache-write tokens ($1,780) vs 28.6M cache-read tokens ($15.71)**; writes were **~90% of
  GPT-5.6 spend**. A second agent framework (NousResearch hermes-agent #70382, 2026-07-23, closed not
  planned) confirms other agents also never emit explicit breakpoints.
- Evidence strength: moderate (single-user measurement, but consistent with documented mechanics: the
  implicit breakpoint lands after the unique tail, the same failure mode Anthropic documents for automatic
  caching).
- Sources: https://github.com/openai/codex/issues/35300 (2026-07-25);
  https://github.com/NousResearch/hermes-agent/issues/70382 (2026-07-23).

### F3. Prompt cache diagnostics (server-side miss reasons)
- **Facts.** Set `prompt_cache_options.comparison_response_id` to a baseline response; the response carries
  `prompt_cache_diagnostics` of type `cache_hit | cache_miss | comparison_response_not_found | unavailable`.
  A miss has `reason` in {`model_changed`, `prompt_cache_key_changed`, `tools_changed`,
  `text_format_changed`, `reasoning_effort_changed`, `verbosity_changed`, `context_compacted`,
  `input_changed`, `service_tier_changed`}, plus `cache_missed_tokens` (estimated tokens after the first
  divergence) and `comparison_reusable_tokens`. Available on Responses API for GPT-5.6+; "no additional cost
  and do not count separately toward rate limits". The guide also lists settings that break reuse:
  tools (names, descriptions, schemas, ordering), reasoning effort, structured-output schema, verbosity.
  Anthropic parity: first-party beta header `cache-diagnosis-2026-04-07` with
  `diagnostics.previous_message_id` (must be sent on every request) - first-party only.
- Sources: https://developers.openai.com/api/docs/guides/prompt-caching/diagnostics (accessed 2026-09-23);
  openai-python `src/openai/types/responses/response.py` (main, accessed 2026-09-23);
  bundled claude-api skill `shared/prompt-caching.md` and `shared/platform-availability.md` (skill 2.1.280).

### F4. Pre-5.6 retention semantics, and the 2026-05-29 default flip
- **Facts.** `prompt_cache_retention`: `in_memory` ("around 5 to 10 minutes of inactivity, up to one hour")
  or `24h` ("typically ... around 30 minutes and can retain them for up to 24 hours"; KV tensors offloaded
  to GPU-local storage). Since **2026-05-29**, orgs without ZDR default to `24h`; ZDR orgs default to
  `in_memory`. GPT-5.5 supports only `24h` ("Caching for GPT-5.5 only works with extended prompt caching",
  changelog 2026-04-24). On earlier models "reported `cached_tokens` is ... rounded down to the nearest
  multiple of 128". `prompt_cache_key` (replaces `user` for routing) - aim for about **15 requests per
  minute** per prefix+key; on GPT-5.6+ it is only for "separate cache accounting". Caches "are not shared
  across organizations and cannot be reused across regional processing boundaries".
- Sources: prompt-caching guide (accessed 2026-09-23); changelog (2026-04-24, 2026-05-29);
  response_create_params.py docstrings.

### F5. OpenAI usage semantics (inclusive), reasoning and predicted-output tokens
- **Facts.** OpenAI's own cost example: `ordinaryInputTokens = input_tokens - cached_tokens -
  cache_write_tokens` - i.e. `input_tokens` **includes** cache reads and writes (opposite of Anthropic).
  Reasoning tokens "are billed as output tokens" and reported in `output_tokens_details.reasoning_tokens`
  (Chat: `completion_tokens_details.reasoning_tokens`). Chat Completions also reports
  `rejected_prediction_tokens` which "are still counted in the total completion tokens for purposes of
  billing". Reasoning effort values: `none, minimal, low, medium, high, xhigh, max` (model-dependent;
  GPT-5.5 defaults to medium; GPT-6 Astra rejects `none`). If limits hit before visible output you "could
  incur costs for input and reasoning tokens without receiving a visible response". `previous_response_id`:
  "all previous input tokens for responses in the chain are billed as input tokens"; server-side
  compaction via `context_management` / `compact_threshold` or `/responses/compact`.
- Sources: prompt-caching guide; https://developers.openai.com/api/docs/guides/reasoning;
  https://developers.openai.com/api/docs/guides/conversation-state (both accessed 2026-09-23);
  openai-python completion_usage.py (2026-09-02).

### F6. Service tiers: Flex, Fast (ex-Priority), Ultrafast - and the tier-blind pricing bug
- **Facts.** `service_tier`: `auto | default | flex | fast/priority | ultrafast`. Flex = Batch rates
  (50% off) with occasional `429 Resource Unavailable` ("You will not be charged"), 10–15 min timeouts.
  **Priority processing was renamed Fast mode on 2026-07-30**; for GPT-5.6 Sol it costs **2x Standard**
  ($8/$40 short context); the response's `service_tier` reports the tier actually used (e.g. `default` after
  ramp-limit downgrade; `priority` regardless of whether you asked for `fast` or `priority`). `ultrafast` is
  an access-controlled tier for gpt-5.6-sol (SDK, 2026-08-14). A competing local cost tool (tokscale #1347,
  2026-09-17) under-billed **~3,011 Codex priority entries by 50%** because it ignored `service_tier`, which
  Codex does record in `token_usage_record` / `token_count` events, and because the LiteLLM cache it used
  had only standard rates at the time.
- Sources: https://developers.openai.com/api/docs/guides/flex-processing;
  https://developers.openai.com/api/docs/guides/fast-mode (accessed 2026-09-23);
  openai-python `responses/response.py` service_tier docstring and commit f38355ec (2026-08-14);
  https://github.com/junhoyeo/tokscale/issues/1347 (2026-09-17).

### F7. Batch, long-context, and data-residency modifiers
- **Facts.** Batch: 50% off, 24h window, separate higher rate-limit pool; the Batch price table lists cached
  and cache-write rates (e.g. gpt-5.6-sol batch 2 / 0.20 / 2.50 / 10). Long-context rows exist per model
  (gpt-5.6-sol long context 8 / 0.80 / 10 / 30; gpt-6-sol 4 / 0.40 / 5 / 15; gpt-6-astra 20 / 2 / 25 / 75);
  models marked "<272K" do not support extended context. "Regional processing (data residency) endpoints
  are charged a **10% uplift** for models released on or after March 5, 2026".
- Sources: https://developers.openai.com/api/docs/guides/batch; pricing page (accessed 2026-09-23).

### F8. OpenAI Admin Usage and Costs APIs (invoice-grade reconciliation)
- **Facts.** `organization.usage.completions.result` buckets carry `input_tokens`, `input_cached_tokens`,
  `input_cache_write_tokens`, `input_uncached_tokens`, per-modality cached/uncached counts, `output_*`,
  `num_model_requests`; `group_by` any of `project_id, user_id, api_key_id, model, batch, service_tier`.
  Costs endpoint groups by `project_id, line_item, api_key_id` and (since 2026-09-02) filters by exact
  `line_items` such as `"gpt-6-astra, input_tokens"`.
- Sources: openai-python `types/admin/organization/usage_completions_response.py`,
  `usage_completions_params.py`, `usage_costs_params.py` (main, commit 6f0da165 2026-09-02).

### F9. OpenAI Codex CLI usage records (coding-agent estate)
- **Facts.** Codex's protocol `TokenUsage` = {`input_tokens`, `cached_input_tokens`,
  `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens`, `total_tokens`};
  `TokenUsageRecord` ties each Responses `response_id` to `thread_id`, `turn_id`, `session_id` with
  per-response, per-turn and per-thread usage; `TokenCountEvent` carries `total_token_usage`,
  `last_token_usage`, `model_context_window` and rate-limit snapshots. Records also carry `service_tier`
  (per tokscale #1347).
- Sources: https://github.com/openai/codex/blob/main/codex-rs/protocol/src/protocol.rs (accessed
  2026-09-23); tokscale #1347.

---

## 2. Google Gemini (AI Studio / Gemini API) and Vertex ("Gemini Enterprise Agent Platform")

### F10. Implicit vs explicit caching; storage-priced explicit caches
- **Facts (Gemini API).** Implicit caching "enabled by default for all Gemini 2.5 and newer models", with
  "no cost saving guarantee"; minimum input **4,096** tokens for Gemini 3.5–3.8 Flash and 3.1 Pro Preview,
  **2,048** for 2.5 Flash/Pro (updated 2026-09-02). Explicit caching (`caches.create`, generateContent
  only; not supported in the Interactions API) defaults to **TTL 1 hour**; billed on cached token count
  plus **storage duration**. Prices: Gemini 3.1 Pro Preview input $2.00 (<=200K) / $4.00 (>200K), context
  caching $0.20 / $0.40 (**90% off**), storage **$4.50 per MTok per hour**; Gemini 3.8 Flash input $0.75,
  cached $0.075, storage $0.50/MTok/hr (promo through 2026-12-31).
- **Facts (Vertex).** Implicit caching "enabled by default" with a **90% discount**; explicit caching 90% on
  Gemini 2.5+ (75% on 2.0); cache creation is billed at standard input price; explicit caches also pay
  storage; default TTL 60 minutes, no maximum; minimum 4,096 tokens for Gemini 3 family but **6,144** for
  3.0 Flash Preview, 3.1 Pro Preview, 3.7 Flash, 3.8 Flash (implicit), 2,048 for Gemini 2; "Caches work
  across traffic types" (PT and PayGo).
- **Derived break-even (not from a source, arithmetic on the prices above):** an explicit cache pays for
  its storage only if reused more than **2.5 times per hour per cached MTok** on 3.1 Pro
  (1.80 saved per use vs 4.50/hr) and more than **0.74 times per hour** on 3.8 Flash (0.675 vs 0.50).
- Sources: https://ai.google.dev/gemini-api/docs/caching (updated 2026-09-02);
  https://ai.google.dev/gemini-api/docs/generate-content/caching (updated 2026-09-11);
  https://ai.google.dev/gemini-api/docs/pricing (updated 2026-09-23);
  https://docs.cloud.google.com/vertex-ai/generative-ai/docs/context-cache/context-cache-overview
  (accessed 2026-09-23).

### F11. Gemini usage semantics (two APIs, inclusive prompt count, separate thoughts)
- **Facts.** generateContent `usageMetadata`: `promptTokenCount` "includes the number of tokens in the
  cached content"; `cachedContentTokenCount`; `candidatesTokenCount`; `thoughtsTokenCount`;
  `toolUsePromptTokenCount`; `totalTokenCount`; per-modality `promptTokensDetails`, `cacheTokensDetails`,
  `candidatesTokensDetails`, `toolUsePromptTokensDetails`; plus `modelVersion`, `responseId`. The Vertex
  example `promptTokenCount 3, candidatesTokenCount 900, thoughtsTokenCount 1054, totalTokenCount 1957`
  shows thoughts are *not* inside candidates; "Response pricing is the sum of output tokens and thinking
  tokens". Vertex adds `usageMetadata.trafficType` (e.g. `ON_DEMAND_FLEX`). The newer Interactions API
  uses `usage.total_input_tokens`, `total_cached_tokens`, `total_output_tokens`, `total_thought_tokens`,
  `total_tool_use_tokens`, `total_tokens`, `*_by_modality`, `grounding_tool_count`, `service_tier`, and
  `previous_interaction_id`. Thinking control: `thinkingLevel` (minimal/low/medium/high; 3.x defaults
  mostly `medium`) and legacy `thinkingBudget`; thought signatures must be resent in stateless mode.
- Sources: https://ai.google.dev/api/generate-content; https://ai.google.dev/api/interactions-api
  (accessed 2026-09-23); https://ai.google.dev/gemini-api/docs/thinking (updated 2026-09-17);
  https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/flex-paygo (accessed 2026-09-23).

### F12. Gemini service tiers: Flex (0.5x), Priority (~1.8x) with silent downgrade; Vertex Flex PayGo
- **Facts.** Flex and Priority launched 2026-04-02. `service_tier: "flex"` = 50% off, synchronous,
  1–15 min target latency, no server-side fallback. Priority is "75-100% more than the standard API"
  (published rows: 3.1 Pro $3.60 vs $2.00 input = 1.8x; 3.8 Flash $1.35 vs $0.75 = 1.8x); when limits are
  exceeded requests **downgrade to Standard and are billed at Standard**, signalled only by the
  `x-gemini-service-tier` response header. Batch = 50%. Vertex Flex PayGo = 50% off, preview, **global
  endpoint only**, selected with headers `X-Vertex-AI-LLM-Shared-Request-Type: flex` (and
  `X-Vertex-AI-LLM-Request-Type: shared` to skip Provisioned Throughput), timeout up to 30 min.
- Sources: https://blog.google/innovation-and-ai/technology/developers-tools/introducing-flex-and-priority-inference/
  (2026-04-02); https://ai.google.dev/gemini-api/docs/flex-inference (updated 2026-09-17);
  https://ai.google.dev/gemini-api/docs/priority-inference (updated 2026-09-02); Gemini pricing page;
  Vertex Flex PayGo page.

### F13. Long-context bands and effective-dated promotional prices
- **Facts.** Gemini 3.1 Pro bills "$2.00, prompts <= 200k tokens" / "$4.00, prompts > 200k tokens" (output
  $12/$18). Gemini 3.8 Flash input "$0.75/1M (through 12/31/26); $1.50/1M (from 1/1/27)", with output,
  caching, storage, Flex and Priority all doubling on 2027-01-01.
- Source: https://ai.google.dev/gemini-api/docs/pricing (updated 2026-09-23).

### F14. Vertex request labels for billing attribution
- **Facts.** `labels` on `generateContent` and `rawPredict` calls are "forwarded to the billing system" and
  usable in billing reports and BigQuery billing exports; up to **64 labels (Google models) / 32 (partner
  models)**, lowercase keys <=63 chars, each key up to **1,000 unique values** over the billing account's
  life (excess keys "might be dropped without notice"); no PII.
- Source: https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/capabilities/add-labels-to-api-calls
  (accessed 2026-09-23).

---

## 3. Claude on third-party clouds (Vertex, Bedrock, Foundry, Claude Platform on AWS)

### F15. Claude on Vertex: endpoint-geography premium and URL-borne model id
- **Facts.** "Regional and multi-region endpoints include a **10% pricing premium** over global endpoints"
  (Sonnet 4.5, Haiku 4.5, Opus 4.5 and all later models). On Vertex "`model` is not passed in the request
  body" (it is in the URL `.../publishers/anthropic/models/{MODEL_ID}:rawPredict`) and
  `anthropic_version: "vertex-2023-10-16"` is in the body. Newer models use global or multi-region (`us`,
  `eu`) endpoints only; provisioned throughput requires regional endpoints. Not supported on Vertex:
  Message Batches, Files, Usage and Cost API, server-side fallbacks, code execution, web fetch. Usage JSON
  is Anthropic-shaped.
- Sources: https://platform.claude.com/docs/en/build-with-claude/claude-on-vertex-ai;
  https://platform.claude.com/docs/en/about-claude/pricing (both accessed 2026-09-23).

### F16. Claude on Bedrock and Foundry / Claude Platform on AWS pricing mechanics
- **Facts.** Bedrock offers global and regional endpoints; regional costs 10% more (see F21 for the price-list
  proof). Claude Platform on AWS and Claude in Microsoft Foundry bill in **Claude Consumption Units** ($0.01
  per CCU; discounts applied as fewer CCUs; hourly metering; the invoice shows one CCU line). `inference_geo:
  "us"` (Claude 4.6+) and Foundry "US Data Zone Standard" apply **1.1x** to all token categories. Caching
  multipliers stack with batch and data residency. Batch (Message Batches) is not available on Bedrock or
  Vertex (Bedrock has its own batch inference). Caches are isolated per workspace on Claude API / Claude
  Platform on AWS / Foundry and per organization on Bedrock and Google Cloud. Also note for the core pricing
  table: Claude Opus 5.5 ($4/$20, cache reads **0.05x** = $0.20) and the 4.7+ tokenizer "produces
  approximately 30% more tokens for the same text".
- Sources: Anthropic pricing page (accessed 2026-09-23); bundled claude-api skill
  `shared/platform-availability.md`, `shared/claude-platform-on-aws.md`, `shared/prompt-caching.md`.

---

## 4. AWS Bedrock

### F17. Bedrock caching semantics: exclusive `inputTokens`, per-TTL write detail, platform-specific minimums
- **Facts.** Converse `usage` = {`inputTokens`, `outputTokens`, `totalTokens`, `cacheReadInputTokens`,
  `cacheWriteInputTokens`, `cacheDetails:[{ttl, inputTokens}]` sorted 1h before 5m}. "When prompt caching is
  enabled, the `inputTokens` field represents only the non-cached input tokens ... total input tokens =
  inputTokens + cacheReadInputTokens + cacheWriteInputTokens". Converse uses `cachePoint: {type:"default",
  ttl:"5m|1h"}`; InvokeModel uses Anthropic `cache_control`. Checkpoints processed tools -> system ->
  messages; a single checkpoint looks back ~20 content blocks. Per-model minimums on Bedrock: Opus 5/5.5,
  Fable 5/5.1 = 512; Opus 4.8, Sonnet 4.5–5 = 1,024; **Opus 4.7 = 4,096** (Anthropic's own table says
  2,048 - a platform discrepancy the simulator must model per channel), Opus 4.5/4.6 and Haiku 4.5 = 4,096.
  "Prompt caching is only supported for on-demand inference ... not supported with the batch inference API."
  With cross-region inference, "at times of high demand, these optimizations may lead to increased cache
  writes." OpenAI GPT-5.6 Sol/Terra/Luna on Bedrock support explicit caching (1,024 min, 4 checkpoints,
  30-minute TTL, 1.25x writes, 90% reads, cached tokens do not count toward TPM) via the Responses API on
  `bedrock-runtime` and `bedrock-mantle`.
- Sources: https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html;
  https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_TokenUsage.html;
  https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html (accessed 2026-09-23);
  https://aws.amazon.com/blogs/machine-learning/introducing-explicit-prompt-caching-for-openai-gpt-5-6-models-on-amazon-bedrock/
  (2026-07-30).

### F18. Bedrock service tiers (Reserved / Priority / Standard / Flex) with resolved-tier reporting
- **Facts.** `service_tier` (Converse: `serviceTier: {type}`) = `reserved | priority | default | flex`.
  Priority is a **75% premium**, Flex a **50% discount** (price list rows for gpt-oss-120b: 0.15 std,
  0.2625 priority, 0.075 flex per 1K tokens). The served tier "is visible in API response and AWS CloudTrail
  Events" and CloudWatch has `ResolvedServiceTier`. Reserved: 1- or 3-month TPM reservations (min 100K
  input TPM / 10K output TPM), overflow to Standard; reserved TPM consumption counts `InputTokenCount` +
  `CacheWriteInputTokens` (cache reads do not consume it). `performanceConfig.latency` selects
  latency-optimized inference.
- Sources: https://docs.aws.amazon.com/bedrock/latest/userguide/service-tiers-inference.html (accessed
  2026-09-23); AWS Price List API AmazonBedrock us-east-1 (publication 2026-09-22T21:21:39Z).

### F19. Bedrock intelligent prompt routing (limited relevance today)
- **Facts.** Routes between two models of one family by predicted quality (`responseQualityDifference`),
  English-optimized; supported models are older (Claude 3/3.5, Llama 3.x, Nova Lite/Pro); response
  `trace.promptRouter.invokedModelId` names the model used; the pricing page quotes "$1 per 1,000 requests".
- Evidence: weak for current-generation value (no Claude 4.x/5 support listed).
- Sources: https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-routing.html; Converse API reference;
  https://aws.amazon.com/bedrock/pricing/ (accessed 2026-09-23).

### F20. Bedrock enterprise attribution: invocation logs, requestMetadata, CUR 2.0 caller identity
- **Facts.** Invocation log schema (`schemaType: ModelInvocationLog`): `timestamp, accountId, region,
  requestId, operation, modelId, identity.arn, requestMetadata, input.inputTokenCount,
  output.outputTokenCount`, bodies inline up to 100 KB (larger to S3). Top-level counts do **not** break out
  cache read/write (they live only inside `outputBodyJson.usage`). Logging covers `bedrock-runtime` only -
  "the same APIs on `bedrock-mantle` are not currently captured". `requestMetadata`: up to **16**
  string pairs, logs only, never on the bill. Billing-side: CUR 2.0 caller-identity column (IAM principal
  attribution), Application Inference Profiles (runtime, per model), Projects (mantle, multi-model). "Both
  classic CUR and CUR 2.0 aggregate cost by usage type over an hour or a day ... Per-prompt detail lives only
  in your model invocation logs"; AWS's own guidance: join logs to CUR "at the model/usage-type/day grain",
  and per-log computed cost "does not reflect discounts, commitments, batch pricing, free tier, or
  provisioned throughput unless you model them".
- Sources: https://docs.aws.amazon.com/bedrock/latest/userguide/model-invocation-logging.html;
  https://docs.aws.amazon.com/bedrock/latest/userguide/cost-mgmt-faq.html; Converse API reference
  (all accessed 2026-09-23).

### F21. AWS Price List API: free, machine-readable Bedrock rates (global vs regional, batch, 1h writes, tiers)
- **Facts.** `https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/index.json` lists offers
  `AmazonBedrock`, `AmazonBedrockFoundationModels`, `AmazonBedrockService`, `AmazonBedrockAgentCore`.
  The us-east-1 FoundationModels file (published 2026-09-22T16:44:18Z) has usage types such as
  `USE1_input_tokens_global_standard`, `USE1_input_tokens_standard` (regional),
  `USE1_cache_write_tokens_1h_global_standard`, `USE1_input_tokens_global_batch`. Examples (per MTok):
  Claude Opus 5 input 5.00 global vs **5.50 regional**, cache read 0.50 vs 0.55, 1h write 10.00 vs 11.00,
  batch input 2.50 global / 2.75 regional; Opus 5.5 input 4.00 / 4.40, cache read 0.20 / 0.22; Sonnet 5
  input 2.00 / 2.20. Units vary (`1M tokens` vs `1K tokens`) and some model rows carry a `service_tier`
  attribute (`standard|priority|flex|batch|global-*`).
- Evidence: strong (primary, machine-readable; fetched and parsed in this session).
- Source: https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrockFoundationModels/20260922164418/us-east-1/index.json
  and .../AmazonBedrock/20260922212139/us-east-1/index.json (publication 2026-09-22).

---

## 5. Azure OpenAI in Microsoft Foundry

### F22. Azure caching differs from OpenAI's in defaults, isolation, PTU treatment and lookback
- **Facts.** For gpt-5.4 and older on Azure the default retention is **`in_memory`** (OpenAI direct defaults
  non-ZDR orgs to 24h); `24h` available on gpt-5.5/5.4/5.3-codex/5.2/5.1*/5/4.1; "The system doesn't share
  prompt caches between Azure subscriptions". GPT-5.6 explicit breakpoints are supported on Standard
  pay-as-you-go, but Azure considers "up to the latest **50** breakpoints" (OpenAI: 80). **PTU-M
  deployments do not support breakpoints or expose `cache_write_tokens`**; cached tokens get "up to 100%
  discount" on Provisioned and "don't consume PTU capacity". Pre-5.6 hits after the first 1,024 tokens come
  in 128-token increments. Extended retention keeps data inside Data Zone / regional boundaries.
- **Field issue.** A Microsoft Q&A thread (July 2026) reports GPT-5.6 on Azure returning `cached_tokens: 0`
  consistently on the Responses API while Chat Completions hit ~90–95%, and Luna broken on both; moderator
  said on 2026-07-13 it "should resolve now", users still reported failures on 2026-07-15/23. (Weak:
  community thread, but exactly the class of silent provider-side regression Token Bill should alarm on.)
- Sources: https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching (ms.date
  2026-08-11, updated 2026-08-12);
  https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/provisioned-throughput (2026-07-15);
  https://learn.microsoft.com/en-in/answers/questions/5942997/gpt-5-6-implicit-prompt-caching-and-explicit-promp
  (July 2026).

### F23. Azure deployment-type pricing and cost attribution
- **Facts.** Deployment categories: Standard (Global / Data Zone / Regional), Priority processing, Batch,
  Provisioned (hourly $/PTU/hr or 1-month/1-year reservations; spillover to Standard via
  `x-ms-spillover-deployment`). PTU sizing counts output at the model's output:input price ratio and cached
  tokens as free capacity. Foundry projects auto-tag usage for Cost Management (models sold by Azure only, not
  Marketplace models); cost records arrive with ingestion delay. A secondary source reports the Data Zone
  meter is 10% above Global Standard (e.g. GPT-5.5 $5.50/$33 vs $5/$30) - treat as moderate until confirmed on
  the Azure pricing page.
- Sources: PTU concept page (2026-07-15); https://learn.microsoft.com/en-us/azure/foundry/concepts/manage-costs
  (ms.date 2026-08-27); Azure prompt-caching page.

---

## 6. DeepSeek, xAI, Mistral

### F24. DeepSeek: disk cache on by default, cache hits ~2% of miss price, and time-of-day pricing
- **Facts.** Models `deepseek-flash` (V4.1-Flash) and `deepseek-v4-pro`, 1M context, 384K max output.
  Off-peak / peak per MTok: Flash cache-hit input 0.003 / 0.006, cache-miss 0.15 / 0.30, output 0.60 / 1.20;
  Pro 0.022 / 0.044, 0.66 / 1.32, 1.98 / 3.96. "Peak hours are 01:00 - 04:00 and 06:00 - 10:00 UTC, Monday
  through Friday, excluding Chinese public holidays"; off-peak rates are half. Context caching on disk is
  "enabled by default for all users", best-effort, built in seconds, cleared "within a few hours to a few
  days"; usage fields `prompt_cache_hit_tokens`, `prompt_cache_miss_tokens`.
- Sources: https://api-docs.deepseek.com/quick_start/pricing; https://api-docs.deepseek.com/guides/kv_cache
  (accessed 2026-09-23).

### F25. xAI: billed cost in every response (`cost_in_usd_ticks`), whole-request long-context doubling
- **Facts.** Every response's `usage` includes `cost_in_usd_ticks` (1 USD = 10^10 ticks) - the amount billed
  after discounts including caching; also on Batch with `cost_breakdown`; not surfaced by the Vercel AI SDK.
  Caching is automatic; improve routing with `x-grok-conv-id` (Chat) or `prompt_cache_key` (Responses);
  cached tokens in `prompt_tokens_details.cached_tokens` / `input_tokens_details.cached_tokens`. Prices
  (grok-4.7/4.6): $2.00 input, $0.50 cached (75% off), $6.00 output; ">=200k: 2x all rates"; Batch "20% off"
  only on grok-4.3 / 4.20 family; tools $5 per 1k web-search / code-execution calls.
- Sources: https://docs.x.ai/developers/cost-tracking; https://docs.x.ai/developers/pricing;
  https://docs.x.ai/developers/advanced-api-usage/prompt-caching and .../usage-and-pricing (accessed
  2026-09-23).

### F26. Mistral: opt-in caching keyed by `prompt_cache_key`, 64-token blocks, 90% off; Batch 50%
- **Facts.** Caching is opt-in through a stable `prompt_cache_key`; cached tokens reported in multiples of
  **64** in `usage.prompt_tokens_details.cached_tokens`; prompts under 64 tokens never hit; cached price
  "10% of the standard input token price". Batch API: "50% discount" (chat, embeddings, FIM, OCR,
  classification, conversations, transcription).
- Sources: https://docs.mistral.ai/studio-api/conversations/advanced/prompt-caching;
  https://docs.mistral.ai/capabilities/batch/ (accessed 2026-09-23).

---

## 7. OpenRouter (gateway)

### F27. OpenRouter usage accounting, response caching, sticky routing, tier billing
- **Facts.** Usage is now included in every response (the `usage: {include: true}` flag is deprecated and
  has no effect): `prompt_tokens`, `completion_tokens`, `prompt_tokens_details.cached_tokens` /
  `cache_write_tokens`, `completion_tokens_details.reasoning_tokens`, `cost`, and
  `cost_details.upstream_inference_cost` (BYOK). `GET /generation?id=` returns `total_cost`,
  `cache_discount`, `upstream_inference_cost`, `provider_name`, `native_tokens_prompt/completion/cached/
  reasoning`, `service_tier`, `data_region`, `workspace_id`, `session_id`, `external_user`, `router`,
  `provider_responses` (incl. fallbacks) and `response_cache_source_id`. Provider sticky routing pins a
  session to the same upstream for 10 minutes after activity to preserve caches (`session_id` up to 256
  chars). Response caching (`X-OpenRouter-Cache: true`): exact-request cache keyed on API key + model +
  endpoint + stream mode + SHA-256 of body; TTL default 300 s, 1–86,400 s; hits are free ("all billable
  usage counters are reported as 0"); headers `X-OpenRouter-Cache-Status/Age/Source-Id`. Service tiers:
  `flex | priority/fast | default` for OpenAI, Anthropic, Vertex, AI Studio, xAI; "you are billed at that
  tier's rate" for the tier that actually served.
- Sources: https://openrouter.ai/docs/guides/guides/usage-accounting;
  https://openrouter.ai/docs/api/api-reference/generations/get-generation;
  https://openrouter.ai/docs/guides/best-practices/prompt-caching;
  https://openrouter.ai/docs/guides/features/response-caching;
  https://openrouter.ai/docs/guides/features/service-tiers (all accessed 2026-09-23).

---

## 8. Cross-cutting standards and data sources

### F28. OpenTelemetry GenAI semantic conventions: the inclusive convention and provider discriminators
- **Facts.** `gen_ai.usage.input_tokens` "SHOULD include all types of input tokens, including cached tokens";
  `gen_ai.usage.cache_read.input_tokens` and `gen_ai.usage.cache_write.input_tokens` SHOULD be included in
  it; `gen_ai.usage.reasoning.output_tokens`; per-modality `gen_ai.usage.{text,image,audio}.*`; instrumentations
  should report the *billed* count when a provider exposes both. `gen_ai.provider.name` well-known values:
  `anthropic, aws.bedrock, azure.ai.inference, azure.ai.openai, cohere, deepseek, gcp.gemini, gcp.gen_ai,
  gcp.vertex_ai, groq, ibm.watsonx.ai, mistral_ai, moonshot_ai, openai, perplexity, x_ai`. Also
  `gen_ai.conversation.id`, `gen_ai.conversation.compacted`, `openai.request.service_tier`,
  `openai.response.service_tier`, `openai.api.type`. All attributes are "Development" stability; the
  conventions moved to a dedicated repo (last commit 2026-09-22).
- Source: https://github.com/open-telemetry/semantic-conventions-genai (docs/registry/attributes/gen-ai.md,
  openai.md; accessed 2026-09-23).

### F29. Price feeds exist but disagree - cross-validation is required
- **Facts.** LiteLLM `model_prices_and_context_window.json` (4,214 entries, last commit 2026-09-23) encodes
  `cache_creation_input_token_cost(_above_1hr)`, `*_flex`, `*_priority`, `*_batches`, `*_above_200k_tokens`,
  `*_above_272k_tokens`, `prompt_cache_min_tokens`, DeepSeek `off_peak_pricing.windows`, and Vertex
  `regional_endpoint_uplift_multiplier: 1.1`. OpenRouter `GET /api/v1/models` (public, 456 models) returns
  per-token `prompt`, `completion`, `input_cache_read`, `input_cache_write`, `input_cache_write_1h`,
  `internal_reasoning`, and `overrides` with `min_prompt_tokens` (e.g. 272,000). Observed discrepancies on
  2026-09-23: OpenRouter lists `openai/gpt-5.6-sol` at $2/$10 while OpenAI's pricing page lists $4/$20;
  `x-ai/grok-4.7` at $1.6/$4.8 vs xAI's $2/$6 (cause not established - channel discount or stale data).
- Sources: https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json;
  https://openrouter.ai/api/v1/models; OpenAI and xAI pricing pages (all fetched 2026-09-23).

### F30. Exact token counting endpoints replace the chars/3.7 heuristic
- **Facts.** OpenAI `POST /v1/responses/input_tokens` accepts the full Responses input (messages, images,
  files, tools, conversations) and returns the exact count. Bedrock `CountTokens` "doesn't incur charges"
  and "will match the token count that would be charged" (Converse or InvokeModel bodies; Claude models
  that are CRIS-only use Anthropic `count_tokens` on `bedrock-mantle`). Gemini `countTokens` returns
  input-only counts including system instructions and tools (Gemini's own heuristic: ~4 chars per token).
  Anthropic has `count_tokens` (bundled skill `shared/token-counting.md`).
- Sources: https://developers.openai.com/api/docs/guides/token-counting;
  https://docs.aws.amazon.com/bedrock/latest/userguide/count-tokens.html;
  https://ai.google.dev/gemini-api/docs/tokens (updated 2026-09-17).

---

## 9. Normalization reference (for adapter authors)

### 9.1 Usage field mapping to a canonical record

| Source | Uncached input | Cache read | Cache write | Output (incl. reasoning) | Reasoning | Input convention |
|---|---|---|---|---|---|---|
| Anthropic Messages (1P, Vertex rawPredict, Bedrock InvokeModel / mantle, Foundry, P-AWS) | `input_tokens` | `cache_read_input_tokens` | `cache_creation_input_tokens` (+ `cache_creation.ephemeral_5m/1h_input_tokens`) | `output_tokens` | not split | exclusive |
| Bedrock Converse | `inputTokens` | `cacheReadInputTokens` | `cacheWriteInputTokens` (+ `cacheDetails[{ttl,inputTokens}]`) | `outputTokens` | n/a | exclusive |
| OpenAI / Azure Responses | `input_tokens - cached - cache_write` | `input_tokens_details.cached_tokens` | `input_tokens_details.cache_write_tokens` | `output_tokens` | `output_tokens_details.reasoning_tokens` | inclusive |
| OpenAI / Azure Chat Completions | `prompt_tokens - cached - cache_write` | `prompt_tokens_details.cached_tokens` | `prompt_tokens_details.cache_write_tokens` | `completion_tokens` (incl. rejected predictions) | `completion_tokens_details.reasoning_tokens` | inclusive |
| Gemini generateContent (AI Studio, Vertex) | `promptTokenCount - cachedContentTokenCount` (+`toolUsePromptTokenCount`) | `cachedContentTokenCount` | none billed as premium (explicit cache: storage/hr) | `candidatesTokenCount + thoughtsTokenCount` | `thoughtsTokenCount` | inclusive (thoughts separate) |
| Gemini Interactions | `total_input_tokens - total_cached_tokens` | `total_cached_tokens` | none | `total_output_tokens` (+`total_thought_tokens`, verify) | `total_thought_tokens` | inclusive |
| DeepSeek | `prompt_cache_miss_tokens` | `prompt_cache_hit_tokens` | none | `completion_tokens` | if present | inclusive (`prompt_tokens` = hit + miss) |
| xAI | `prompt_tokens - cached` | `prompt_tokens_details.cached_tokens` / `input_tokens_details.cached_tokens` | none documented | `completion_tokens` / `output_tokens` | reasoning details | inclusive; **billed $ in `cost_in_usd_ticks`** |
| Mistral | `prompt_tokens - cached` | `prompt_tokens_details.cached_tokens` (x64) | none | `completion_tokens` | n/a | inclusive |
| OpenRouter | `prompt_tokens - cached - cache_write` | `prompt_tokens_details.cached_tokens` | `prompt_tokens_details.cache_write_tokens` | `completion_tokens` | `completion_tokens_details.reasoning_tokens` | inclusive; **billed $ in `usage.cost`** |
| Codex CLI rollout | `input_tokens - cached_input_tokens - cache_write_input_tokens` (verify) | `cached_input_tokens` | `cache_write_input_tokens` | `output_tokens` | `reasoning_output_tokens` | inclusive (verify) |
| OTel GenAI | `input_tokens - cache_read - cache_write` | `gen_ai.usage.cache_read.input_tokens` | `gen_ai.usage.cache_write.input_tokens` | `gen_ai.usage.output_tokens` | `gen_ai.usage.reasoning.output_tokens` | inclusive |

"verify" = inferred from field names, not stated by the source; the adapter test suite should pin it with
recorded fixtures.

### 9.2 Cache rule matrix (what each provider-specific simulator must model)

| Channel / model family | Activation | Min prefix | Granularity | TTL | Write price | Read price |
|---|---|---|---|---|---|---|
| Anthropic (1P/P-AWS/Foundry/Vertex/Bedrock) | explicit or top-level automatic | 512–4,096 by model (Bedrock: Opus 4.7 = 4,096) | block boundaries, 4 breakpoints, 20-block lookback | 5m or 1h, refresh on read | 1.25x / 2x | 0.1x (0.05x Opus 5.5, 0.025x Fable/Mythos 5.1) |
| OpenAI GPT-5.6+ (direct, Azure PAYG, Bedrock) | implicit breakpoint + up to 3 explicit, or explicit-only | 1,024 visible | breakpoints; lookback 80 (Azure 50) | >=30 min, refresh on reuse | 1.25x | 0.1x |
| OpenAI GPT-5.5 | implicit only | varies | model-dependent | 24h policy only | none | 0.1x |
| OpenAI <= GPT-5.4 | implicit | 1,024 | 128-token increments | in_memory (5–10 min idle, <=1h) or 24h; default 24h (OpenAI non-ZDR) / in_memory (Azure) | none | 0.1x (GPT-5.x), 0.25x (4.1, o3, o4-mini), 0.5x (o1, o3-mini) |
| Gemini implicit (AI Studio / Vertex) | automatic | 2,048 / 4,096 / 6,144 (Vertex some 3.x) | prefix | not published | none | 0.1x (Gemini 2.5+) |
| Gemini explicit | `caches.create` | same | whole cached object | default 60 min, settable | standard input + **storage $/MTok/hr** | 0.1x |
| DeepSeek | automatic (disk) | not published | fixed intervals + request boundaries | hours to days | none | ~0.02x (Flash), ~0.033x (Pro) |
| xAI | automatic, `x-grok-conv-id` / `prompt_cache_key` | not published | prefix | not published | none documented | 0.25x (grok-4.6/4.7), 0.15x (4.5) |
| Mistral | opt-in `prompt_cache_key` | 64 | 64-token blocks | not published | none documented | 0.1x |
| OpenRouter response cache | `X-OpenRouter-Cache: true` | n/a (exact body) | whole request | 1 s–24 h (default 5 min) | normal first call | **free** |

### 9.3 Price modifiers Token Bill must represent

| Modifier | Values found | Where |
|---|---|---|
| Endpoint geography | +10% regional / multi-region / data-zone / `inference_geo: us` / OpenAI regional processing | Bedrock, Vertex, Azure (secondary source), Anthropic 1P & Foundry, OpenAI |
| Service tier | flex 0.5x; priority 1.75x (Bedrock), ~1.8x (Gemini), 2x (OpenAI Fast); ultrafast (OpenAI, access-controlled); reserved/PTU (capacity) | OpenAI, Gemini, Vertex, Bedrock, Azure, OpenRouter |
| Batch | 0.5x (OpenAI, Anthropic, Gemini, Bedrock, Mistral); 0.8x (xAI, some models) | all |
| Long-context band | >200K (Gemini 3.1 Pro, xAI 2x all rates), >272K (OpenAI long-context rows) | Gemini, xAI, OpenAI |
| Time of day | peak 2x vs off-peak (01:00–04:00, 06:00–10:00 UTC Mon–Fri peak) | DeepSeek |
| Effective date | promo through 2026-12-31 then 2x (Gemini 3.8 Flash); repricings (GPT-5.6 Sol 2026-08-21) | Gemini, OpenAI, Anthropic |
| Negotiated | CCU discounts, private offers, PTU reservations, Bedrock Reserved | Anthropic marketplaces, Azure, AWS |
| Tool fees | web search $10/1k (OpenAI, Anthropic), $5/1k (xAI), $14/1k grounding after free 5,000/month (Gemini 3.x) | per provider |

---

## 10. What Token Bill should build

The goal is to turn Token Bill from an Anthropic cache profiler into the estate-wide "bill explainer and
bill reducer" that enterprises cannot get from any single provider console. Ordered by value per effort.

### 10.1 Foundation: canonical usage record and adapters (build first)
1. **`tokenbill/trace@2` canonical usage** with explicit, exclusive buckets:
   `input_uncached`, `cache_read`, `cache_write_{5m,1h,30m,other}`, `output_visible`, `output_reasoning`,
   `tool_use_prompt`, `rejected_prediction`, plus the modifiers that change price: `provider`, `channel`
   (1p | bedrock-runtime | bedrock-mantle | vertex | azure | foundry | p-aws | openrouter | ...),
   `api_type` (messages | converse | invoke | responses | chat | generateContent | interactions),
   `endpoint_geo` (global | regional | multi-region | data-zone | us), `service_tier_requested`,
   `service_tier_resolved`, `batch`, `context_band`, `ts_utc`, `billed_cost_reported` (xAI ticks, OpenRouter
   `cost`), and the **raw provider usage object preserved verbatim**. Keep `trace@1` readable.
2. **Adapters** (pure-stdlib parsers + optional SDK recorders), each with recorded-fixture tests that pin the
   inclusive/exclusive convention of section 9.1: Anthropic (existing), Bedrock Converse / InvokeModel /
   mantle, Bedrock invocation-log S3/CloudWatch JSON, OpenAI Responses + Chat (+ Azure), OpenAI Batch output
   files, Gemini generateContent + Interactions (AI Studio and Vertex, deriving model from the Vertex URL),
   DeepSeek, xAI, Mistral, OpenRouter (response + `/generation`), Codex CLI rollouts, and **OTel GenAI spans
   (import and export)** so Token Bill plugs into any existing observability pipeline.
3. **Invariant checks per adapter**: e.g. OpenAI `cached + cache_write <= input_tokens`; Gemini
   `total = prompt + candidates + thoughts (+ toolUse)`; flag records that violate them rather than guessing.

### 10.2 Pricing engine v2 (effective-dated, multi-dimensional, verifiable)
4. Rate cards keyed by (provider, channel, model, geo, tier, batch, context band, TTL, time window) with
   `effective_from/effective_to`, each row carrying its source URL and verification date.
5. **Auto-sync from machine-readable feeds**: AWS Price List API (Bedrock), OpenRouter `/api/v1/models`,
   LiteLLM map as fallback; a `tokenbill pricing verify` command/CI job that diffs feeds against the primary
   table and fails on disagreement (the gpt-5.6-sol $2 vs $4 case is a live example).
6. **Enterprise overrides**: negotiated discount multipliers per provider/channel (CCU private offers, AWS/Azure
   discounts), PTU/Reserved capacity costs, currency.
7. **Tier-aware cost**: always price at `service_tier_resolved` (OpenAI response `service_tier`, Gemini
   `x-gemini-service-tier` header, Bedrock `serviceTier`, Vertex `trafficType`, OpenRouter `service_tier`).

### 10.3 Provider-specific cache simulators (extend the existing replay engine)
8. Implement the rule sets in section 9.2 as pluggable `CacheModel`s: OpenAI <=5.4 (128-token rounding,
   in_memory vs 24h, no write fee), OpenAI 5.6+ (30-min refresh-on-reuse TTL, 1.25x writes, implicit
   breakpoint + <=3 explicit, 80/50 lookback), Gemini implicit (per-platform minimums) and explicit
   (storage-hour economics), Bedrock (Anthropic rules with Bedrock's per-model minimums; no caching in
   batch inference), DeepSeek, xAI, Mistral (64-token blocks, key-scoped). Scenario set per call:
   as-billed, no-cache, optimal-cache, fixed-cache, **plus cheapest-eligible-tier and cheapest-channel**.

### 10.4 New detectors (each with evidence span, fix text, dollars recovered)
9. **cache-write-waste (OpenAI 5.6+/Anthropic automatic)**: write tokens per request ~ whole unique tail and
   write:read ratio >> 1 (Codex: 9.1x). Fix: explicit breakpoint at end of stable prefix / explicit mode.
10. **Parameter-churn breakers** mapped to OpenAI's taxonomy: `reasoning_effort_changed`,
    `service_tier_changed`, `text_format_changed` (schema), `verbosity_changed`, `prompt_cache_key_changed`,
    `context_compacted` - Token Bill already has model-switch/tool-churn/history-rewrite.
11. **Cache fragmentation across isolation scopes**: identical prefixes written separately across
    workspaces (Anthropic), subscriptions (Azure), orgs/regional boundaries (OpenAI), API keys (OpenRouter
    response cache), or `prompt_cache_key` over-sharding / >15 RPM per key.
12. **Tier mis-fit**: priority/fast traffic with no latency SLO (2x cost), async work not on flex/batch
    (0.5x), priority requests silently downgraded (billed at standard - verify invoices).
13. **Geography premium**: regional/data-zone/`inference_geo` traffic (+10%) from apps with no residency
    requirement; conversely flag CRIS-induced extra cache writes.
14. **Long-context cliff**: prompts just over 200K/272K where the whole request is billed at 2x (xAI,
    Gemini 3.1 Pro, OpenAI long-context rows); fix via compaction/truncation below the band.
15. **Explicit-cache storage waste (Gemini)**: storage-hours vs reads/hour below break-even (2.5/h on 3.1
    Pro, 0.74/h on 3.8 Flash); recommend TTL shortening or implicit caching.
16. **Time-shift opportunity (DeepSeek)**: peak-window batchable traffic, 50% saving by shifting.
17. **Provider-side cache regression alarm**: eligible prefixes (>= min tokens, repeated within TTL) with
    `cached_tokens == 0` over N requests on a channel (the Azure GPT-5.6 Responses case).
18. **Reasoning spend**: reasoning share of output and effort level per task class; flag max/xhigh on
    classification-style calls; flag `max_output_tokens` truncations that billed reasoning with no visible
    output.

### 10.5 Ground truth and reconciliation (the enterprise trust story)
19. **Use provider diagnostics as labels**: ingest OpenAI `prompt_cache_diagnostics` and Anthropic cache
    diagnostics; report Token Bill classifier precision/recall against them.
20. **Replace chars/3.7** with exact counts from the free count-tokens endpoints (opt-in online mode,
    cached by content hash) for segment attribution; keep the heuristic as offline fallback.
21. **Invoice reconciliation**: join per-request traces to OpenAI Usage/Costs API (group_by
    project/user/api_key/service_tier, line_items), AWS CUR 2.0 (caller identity, AIP/Projects tags), GCP
    billing export (Vertex `labels`), Azure Cost Management (project tag, meter), OpenRouter `/generation`,
    xAI ticks. Report an "unexplained delta" per provider/day; that single number is what makes finance
    teams trust the tool.
22. **Attribution dimensions** carried through from Bedrock `requestMetadata`/`identity.arn`, Vertex
    `labels`, OpenAI `prompt_cache_key`/`safety_identifier`/project, Foundry project tags, OpenRouter
    `workspace_id`/`session_id`/`external_user`, Codex `thread_id/turn_id`.
23. **Capacity mode for PTU / Bedrock Reserved**: express cache savings as freed TPM/PTU (cached tokens do
    not consume PTU; Bedrock Reserved counts input + cache writes) and compute utilization vs committed spend.

---

## 11. Open questions and caveats
- OpenAI GPT-5.6+ exact semantics of the implicit breakpoint when explicit ones exist (which write slot is
  consumed first) and whether `cached_tokens` still rounds on 5.6 (Azure says rounding does not apply).
- Gemini: whether `toolUsePromptTokenCount` is inside `promptTokenCount`, and whether implicit caching has a
  published TTL. Neither is stated in the pages read.
- Azure Data Zone +10%: only a secondary source was read; confirm on the Azure pricing page.
- xAI and DeepSeek do not publish minimum cacheable length or TTL; simulators must fit these from data.
- OpenRouter vs OpenAI price discrepancy for gpt-5.6-sol and grok-4.7 discount: unresolved cause.
- Bedrock vs Anthropic minimum-cache-length disagreement for Opus 4.7 (4,096 vs 2,048).
- Codex rollout inclusive/exclusive convention for `input_tokens` should be pinned with a real fixture.
- Whether Bedrock invocation logs will add cache counts to top-level metadata and cover `bedrock-mantle`.
