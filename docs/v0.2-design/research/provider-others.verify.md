# Verification notes: track `provider-others`

Verifier run: 2026-09-23. Method: every cited source was re-fetched (curl + text extraction, WebFetch, `gh api` for GitHub issues/commits, raw SDK source files, AWS/Azure price APIs, local repo read). Working copies of fetched pages are in `scratchpad/research/verify_tmp/po/`.

Tally: 28 findings. 21 confirmed, 7 corrected, 0 unverifiable, 0 refuted.

---

## OpenAI

### oai-56-explicit-cache: CORRECTED
- Guide (developers.openai.com/api/docs/guides/prompt-caching, fetched 2026-09-23) confirms: 1,024 visible input tokens for GPT-5.6+; write 1.25x, read 0.1x; `ttl` "30m" is the only value and the default; reuse "refreshes its lifetime without another cache-write charge"; up to four cache writes per request; in implicit mode "an implicit breakpoint uses one of the four cache write slots to leave three usable explicit cache write slots"; explicit mode with no breakpoints means "the request does not use prompt caching or create cache writes"; the cost example reads `input_tokens_details.cached_tokens` and `cache_write_tokens`.
- SDK `response_create_params.py` (openai-python main) docstring agrees on implicit/explicit write counts and ttl.
- Changelog: GPT-5.6 released Jul 9 2026 with explicit prompt caching. aihubmix blog exists, dated July 31 2026, and matches 1.25x / 0.1x / 30m.
- **Error:** "Matching looks back over the latest 80 breakpoints" comes only from the SDK docstring ("considers up to the latest 80 breakpoints"). The current guide says the lookup boundaries are **"the first 2 and latest 50 explicit breakpoints"**. Implicit mode adds the implicit breakpoint, up to 20 earlier eligible message endings, and the end of the initial developer block. The two primary sources disagree, and the guide is the more detailed and more specific. Corrected claim: OpenAI's guide gives the first 2 plus latest 50 explicit breakpoints. The SDK docstring says 80. Treat the value as unsettled.

### oai-cache-write-waste: CORRECTED
- openai/codex#35300 exists, was opened 2026-07-25 by davelindo, and is OPEN. The numbers match exactly: 0% hits and ~9,060 writes (9,060-9,065) without a breakpoint; 98.6% hits and 123-128 writes with one explicit breakpoint. The billing table shows `cache_write_tokens_30m_standard` 258.8M at $1,780.33 (90.0%) and `cache_read_tokens_standard` 28.6M at $15.71.
- NousResearch/hermes-agent#70382 exists, dated 2026-07-23, CLOSED as NOT_PLANNED.
- **Over-generalization, which needs correction:**
  1. The measurements and the billing came from **AWS Bedrock Mantle** (AWS Cost Explorer, "OpenAI GPT-5.6 Sol (Amazon Bedrock Edition)", 2026-07-01 to 07-23), not from api.openai.com. The author says the results "should also be reproduced against api.openai.com before being treated as universal".
  2. The 0% hit rate applies only to *independent requests sharing a stable startup prefix with a different first user turn*. Append-only turns within one session reached ~98% hits unmodified.
  3. The author explicitly says the evidence "does not establish that this defect caused every one of the 258.8M cache-write tokens". Stable `prompt_cache_key` was also required on Mantle.
  4. The hermes issue says "No production telemetry here shows a hit-rate problem" and calls it "not a correctness bug". It confirms only the missing field, not any waste.
- Corrected claim: on Bedrock Mantle, one Codex user measured writes at 90% of GPT-5.6 Sol spend (Jul 1-23). A controlled replay showed that a single explicit breakpoint takes cross-session startup-prefix reuse from 0% to 98.6%. What share of the invoice is attributable to the missing breakpoint is not established. hermes-agent lacks the field but reports no measured waste.

### oai-cache-diagnostics: CONFIRMED
- The diagnostics guide confirms `prompt_cache_options.comparison_response_id` and the types `cache_hit`, `cache_miss`, `comparison_response_not_found` and `unavailable`. It also confirms `cache_missed_tokens` / `comparison_reusable_tokens`, "Responses API for GPT-5.6 and later supported models", and "no additional cost and do not count separately toward rate limits".
- The SDK `response.py` Literal lists exactly 9 reasons (model, prompt_cache_key, tools, text_format, reasoning_effort, verbosity, context_compacted, input, service_tier `_changed`).
- Bundled skill 2.1.280 `prompt-caching.md` confirms the Anthropic beta header `cache-diagnosis-2026-04-07` with `diagnostics.previous_message_id`. `platform-availability.md` says "First-party API only".
- The changelog marks diagnostics GA on Sep 8 2026. The guide also notes that diagnostics are best-effort and report only the first classified reason.

### oai-retention-defaults: CORRECTED
- Confirmed: in_memory is "around 5 to 10 minutes of inactivity, up to one hour". 24h is "around 30 minutes and can retain them for up to 24 hours". Non-ZDR orgs default to 24h and ZDR orgs to in_memory, per the changelog entry of May 29 2026. GPT-5.5 supports only 24h (Apr 24 changelog: "In-memory prompt caching is not supported"). Earlier models round down to a multiple of 128 and have no write charge. Caches are "not shared across organizations and cannot be reused across regional processing boundaries".
- **Error:** the guide says "aim for about 15 requests per minute **in total across all prefixes using each key**". That is a per-key budget, not per prefix+key. Separately, "traffic above 15 requests per minute can lead to overflow routing" per machine.

### oai-usage-inclusive: CONFIRMED
- The guide's cost function has `ordinaryInputTokens = inputTokens - cachedTokens - cacheWriteTokens`. The reasoning guide says reasoning tokens "are billed as output tokens". `completion_usage.py` says rejected_prediction_tokens "are still counted in the total completion tokens for purposes of billing". The conversation-state guide says "all previous input tokens for responses in the chain are billed as input tokens".

### oai-service-tiers: CONFIRMED
- SDK `ServiceTier` = auto/default/flex/scale/priority/fast/ultrafast. The Flex guide says tokens are "priced at Batch API rates" and that for a 429 Resource Unavailable "You will not be charged".
- Changelog Jul 30 2026: "Fast mode ... replaces our Priority Processing offering ... twice the price ... requests tagged priority will automatically use Fast mode."
- Fast-mode guide: GPT-5.6 Sol costs $8/$40 for short context. The response shows `priority` for both fast and priority requests. A ramp-limit downgrade returns `service_tier: "default"` and is charged at standard rates.
- The SDK docstring confirms that ultrafast is access-controlled and for gpt-5.6-sol.
- tokscale#1347 was created 2026-09-17 and counts 3,011 priority entries undercharged by 50%.
- Nuance: the 2x figure is stated for GPT-5.6 Sol. GPT-6 Astra rows are also 2x.

### oai-batch-longctx-residency: CONFIRMED
- Batch guide: "50% lower costs, a separate pool of significantly higher rate limits, and a clear 24-hour turnaround".
- The pricing page data has a Batch row for gpt-5.6-sol of 2 / 0.2 / 2.5 / 10. Column headers are "≤272K input tokens" and ">272K input tokens". The gpt-5.6-sol long-context row is 8 / 0.8 / 10 / 30 against short-context 4 / 0.4 / 5 / 20.
- "Regional processing (data residency) endpoints are charged a 10% uplift for models released on or after March 5, 2026".

### oai-admin-usage-costs: CONFIRMED
- `usage_completions_response.py` has `input_cached_tokens`, `input_cache_write_tokens` and `input_uncached_tokens` ("excluding cache-write tokens"). `usage_completions_params.py` group_by = project_id, user_id, api_key_id, model, batch, service_tier. `usage_costs_params.py` group_by = project_id, line_item, api_key_id.
- `line_items` ("exact line item names") was added in commit 6f0da165 on 2026-09-02 (checked via `gh api` diff).
- Minor citation note: the last commit touching `usage_completions_response.py` is bc4f8efd (2026-08-25), not 6f0da165.

### codex-usage-format: CORRECTED
- `codex-rs/protocol/src/protocol.rs` (main) confirms `TokenUsage` {input_tokens, cached_input_tokens, cache_write_input_tokens, output_tokens, reasoning_output_tokens, total_tokens} and `TokenUsageRecord` {thread_id, turn_id, session_id, root_turn_id, response_id, usage, turn_token_usage, thread_token_usage}.
- **Correction:** `TokenUsageRecord` and `TokenUsage` have **no** `service_tier` field. In protocol.rs, `service_tier` lives on session and turn settings: `SessionConfiguredEvent`, `ThreadSettingsSnapshot` and `TurnSettingsUpdate`. A code search of openai/codex found no service_tier on usage records. Per-record service_tier rests only on tokscale#1347's third-party observation of rollout files. Adapters should take the tier from the session/turn context events and not assume it is on each usage record.

## Google Gemini / Vertex

### gemini-caching: CONFIRMED
- The caching page (updated 2026-09-02) says implicit caching is on by default for Gemini 2.5+. Minimums are 4,096 for 3.5-3.8 Flash and 3.1 Pro Preview, and 2,048 for 2.5.
- The explicit caching page (updated 2026-09-11) says "no cost saving guarantee" for implicit caching and that the TTL "defaults to 1 hour".
- The Vertex overview (updated 2026-09-22) says implicit caching gets a 90% discount and explicit caching 90% on 2.5+ (75% on 2.0). It says creation is billed "at the standard input token price", with storage charged only for explicit caches. It lists 6,144 "(implicit caching only)" for 3.0 Flash Preview, 3.1 Pro Preview, 3.7 Flash and 3.8 Flash, and a default TTL of 60 minutes.
- The pricing page (updated 2026-09-23) lists 3.1 Pro storage at $4.50/MTok/hr (cached $0.20 vs $2.00) and 3.8 Flash at $0.50/MTok/hr through 2026-12-31 (cached $0.075 vs $0.75).
- Break-even arithmetic is correct: 4.50/1.80 = 2.5 and 0.50/0.675 = 0.74. It ignores the one-time creation cost.

### gemini-usage-semantics: CONFIRMED
- The UsageMetadata reference says promptTokenCount "includes the number of tokens in the cached content". It also lists toolUsePromptTokenCount and thoughtsTokenCount, and gives totalTokenCount as "prompt + thoughts + response candidates".
- The Vertex Flex PayGo page example shows promptTokenCount 3, candidatesTokenCount 900 and thoughtsTokenCount 1054, which sum to totalTokenCount 1957, with trafficType ON_DEMAND_FLEX.
- The Thinking page says "response pricing is the sum of output tokens and thinking tokens". It now shows "Last updated 2026-09-23", which may be a re-edit since 09-17.
- The Interactions API usage has total_input/cached/output/thought/tool_use tokens and a `service_tier` field.

### gemini-tiers: CONFIRMED
- The blog post is dated April 2 2026. It says Flex is "half the price" and that Priority overflow is "automatically served at the Standard tier".
- The Flex page (updated 09-23) says 50%, "1–15 min target" and "No server-side fallback".
- The Priority page (updated 09-23) says "75-100% more than Standard" and that downgraded requests are "billed at the standard rate", and tells developers to "monitor the x-gemini-service-tier header".
- The pricing page lists 3.1 Pro Priority at $3.60 against $2.00 (1.8x) and 3.8 Flash at $1.35 against $0.75.
- Vertex Flex PayGo: 50% discount, "global endpoint only", header `X-Vertex-AI-LLM-Shared-Request-Type: flex`, trafficType ON_DEMAND_FLEX.
- Minor caveat: the docs do not say that the header is the *only* signal. The Interactions response object also carries `service_tier`.

### gemini-effective-dated-prices: CONFIRMED
- 3.1 Pro is $2.00 at ≤200K and $4.00 above, with output $12/$18. 3.8 Flash is "$0.75 through December 31, 2026. $1.50 starting January 1, 2027". Output, caching, storage, Flex and Priority rows all double on the same date.
- OpenAI changelog Aug 21 2026: GPT-5.6 Sol "now costs $4 ... and $20", promotional "at least through November 21, 2026". That makes a second effective date worth modelling.

## Claude on third-party clouds

### vertex-claude-geo-labels: CONFIRMED
- The Anthropic pricing page says regional and multi-region endpoints "include a 10% premium over global", applying to "Claude Sonnet 4.5, Haiku 4.5, Opus 4.5, and all future models".
- The Claude on Vertex page says model is "not passed in the request body" and gives `anthropic_version` as `vertex-2023-10-16`. Its unsupported list includes Message Batches and Usage and Cost.
- The labels page supports generateContent/rawPredict and allows 64 labels for Google models and 32 for partner models. Each key may have up to 1000 unique values and "might be dropped without notice". Labels are "forwarded to the billing system" and appear in billing data exports.

### claude-marketplace-ccu: CONFIRMED
- The pricing page confirms CCU at $0.01 for P-AWS and Foundry. Discounts are "Applied as fewer CCUs metered", reported hourly, and appear as "a single CCU line item". `inference_geo: "us"` for Claude 4.6+ applies 1.1x to "all token pricing categories". The Foundry US Data Zone Standard type applies the same 1.1x. Caching multipliers "stack with ... Batch API discount and data residency". Opus 5.5 is $4/$20 with cache hits at 5% ($0.20).
- The bundled skill `prompt-caching.md` confirms isolation per workspace on the Claude API, P-AWS and Foundry, and per organization on Bedrock and Google Cloud.
- The local repo `tokenbill/pricing.py` PRICING dict has no `claude-opus-5-5` entry, so the gap is confirmed.

## AWS Bedrock

### bedrock-cache-semantics: CONFIRMED
- The Bedrock prompt-caching page says `inputTokens` "represents only the non-cached input tokens" and gives the total as a sum.
- The TokenUsage reference describes `cacheDetails` as "breakdown of cache writes by TTL ... Sorted by TTL duration (1h before 5m)".
- Bedrock's model table lists Opus 5.5 and Opus 5 at 512, Opus 4.8 at 1,024, Opus 4.7 at **4,096**, Opus 4.6 at 4,096, Haiku 4.5 at 4,096, and Sonnet 5 at 1,024 per the prose. The same page says caching is "not supported with the batch inference API" and that cross-region inference "may lead to increased cache writes".
- GPT-5.6 Sol/Terra/Luna are listed at 1,024 with 4 checkpoints and a 30-minute TTL.
- AWS ML blog (30 JUL 2026) says writes cost "1.25 times" and reads get a 90% discount.
- Extra context: the bundled skill (2.1.280) says Opus 4.7 = 2048 and that "no per-platform exception remains". The discrepancy the finding describes is a genuine conflict between documentation sources.

### bedrock-price-list-api: CONFIRMED
- Both offer files were downloaded and parsed. AmazonBedrockFoundationModels (version 20260922164418) lists "Claude Opus 5 (Amazon Bedrock Edition)" with these rows:
  - input_tokens_global_standard 5.00 and input_tokens_standard 5.50
  - cache read 0.50 and 0.55
  - 1h write 10.00 and 11.00
  - input_tokens_global_batch 2.50 and input_tokens_batch 2.75, all per 1M tokens
- Opus 5.5 is 4.00/4.40 input and 0.20/0.22 cache read. Sonnet 5 is 2.00/2.20.
- AmazonBedrock (version 20260922212139) lists gpt-oss-120b input at 0.00015 standard, 0.0002625 priority (1.75x) and 0.000075 flex (0.5x), per **1K tokens**, with a `service_tier` attribute.

### bedrock-service-tiers: CONFIRMED
- The tiers page lists four tiers: Reserved, Priority, Standard and Flex, with the "service_tier" parameter taking reserved/priority/default/flex. It says the served tier "is visible in API response and AWS CloudTrail Events" and that CloudWatch `ResolvedServiceTier` "shows the actual tier that served your requests".
- Reserved is 1 or 3 months with a minimum of 100,000 input TPM and 10,000 output TPM, and "consumption includes both InputTokenCount and CacheWriteInputTokens".
- The Converse reference has `serviceTier {type}`.
- Note: the 1.75x and 0.5x multipliers come from the Price List rows for gpt-oss-120b. The tiers page itself only says "price premium" and "pricing discount".

### bedrock-attribution: CONFIRMED
- The invocation-logging page shows schema fields identity.arn, requestMetadata, input.inputTokenCount and output.outputTokenCount, with bodies up to 100 KB. It has no top-level cache fields. It says "the same APIs on bedrock-mantle, are not currently captured".
- The Converse reference says requestMetadata "Maximum number of 16 items".
- The cost FAQ says CUR "aggregate[s] cost by usage type over an hour or a day ... neither carries a per-request identifier". It advises "join logs to CUR at the model/usage-type/day grain", and says the log-computed figure "does not reflect discounts, commitments, batch pricing, free tier, or provisioned throughput". It notes that the caller-identity column exists only in CUR 2.0.

## Azure

### azure-openai-caching: CORRECTED
- The Azure page (ms.date 2026-08-11, updated_at 2026-08-12) confirms:
  - "For gpt-5.4 and older models ... the default is in_memory"
  - "doesn't share prompt caches between Azure subscriptions"
  - "considers up to the latest 50 breakpoints"
  - PTU-M "don't support prompt cache breakpoints or expose cache_write_tokens"
  - Provisioned gets "up to 100% discount"
- The PTU page (ms.date 2026-07-15) says "Cached tokens don't consume PTU capacity".
- The Q&A thread 5942997 is genuine. Posts on 2026-07-10 report Responses returning cached_tokens 0 against Chat Completions at ~95.8%. A moderator said "should resolve now" on 07-13. On 07-15 a user reported Luna failing on both endpoints while Sol and Terra worked.
- **Error:** the contrast "(OpenAI: 80)" is not supported by OpenAI's own current guide, which says "first 2 and latest 50 explicit breakpoints", so the lookback is effectively the same as Azure's 50. The 80 appears only in the openai-python docstring. Corrected claim: Azure documents a 50-breakpoint read lookback, and OpenAI's guide documents first-2 plus latest-50. There is no confirmed Azure/OpenAI difference in lookback. The other Azure differences stand: the in_memory default, per-subscription isolation and the PTU-M limits.

### azure-deployment-costs: CONFIRMED (upgraded evidence)
- The PTU page confirms $/PTU/hr hourly billing and "1-month or 1-year commitment" reservations. It confirms spillover via `x-ms-spillover-deployment`, sizing where "Cached tokens don't consume PTU capacity", and an output-to-input ratio.
- The manage-costs page (ms.date 2026-08-27) says "Every Foundry project is automatically tagged". It adds that this is "not yet supported for models served through Azure Marketplace", and that records "can appear with delay".
- The finding called Data Zone +10% "secondary-source only". I confirmed it from the primary **Azure Retail Prices API** (prices.azure.com, eastus2, product "Azure OpenAI GPT5"): "5.5 ShortCo inp Dz" $5.50 against "Gl" $5.00, and "opt Dz" $33 against "Gl" $30. Batch and cached rows show the same 1.1x.

## DeepSeek, xAI, Mistral

### deepseek-offpeak-cache: CONFIRMED
- The pricing page gives deepseek-flash and deepseek-v4-pro rates as off-peak / peak:

  | | Flash | Pro |
  |---|---|---|
  | Cache hit | 0.003 / 0.006 | 0.022 / 0.044 |
  | Cache miss | 0.15 / 0.30 | 0.66 / 1.32 |
  | Output | 0.60 / 1.20 | 1.98 / 3.96 |

- Peak hours are "01:00 - 04:00 and 06:00 - 10:00 UTC, Monday through Friday, excluding Chinese public holidays".
- The KV cache page says caching is "enabled by default for all users" and "best-effort", with entries cleared "within a few hours to a few days". It names `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`.
- Nuance: a hit costs 2% of a miss for Flash but ~3.3% for Pro.

### xai-cost-ticks: CONFIRMED
- The cost-tracking page (last updated Sep 3 2026) confirms `cost_in_usd_ticks` on chat completions, Responses, image and video. It says 1 USD = 10^10 ticks and that the amount is after discounts. It says the "Vercel AI SDK ... does not currently surface" it, and that batch results include per-request costs plus `cost_breakdown`.
- The pricing page lists grok-4.7 at $2.00 / $0.50 / $6.00, with the long context row "≥ 200k tokens" at $4 / $1 / $12 (2x). Batch "20%" applies to grok-4.3 and the grok-4.20 family, and "Models not listed above have no batch discount".
- The caching pages say caching is automatic and describe x-grok-conv-id / prompt_cache_key.
- Extra modifiers on the same page that the finding omits: US regional endpoint at 1.1x and Priority at 2x.

### mistral-optin-cache: CONFIRMED
- The caching page says cached tokens cost "10% of the standard input token price". It says "Cache blocks contain 64 tokens ... cached_tokens is a multiple of 64. Prompts with fewer than 64 prompt tokens do not have cache hits", and describes enabling via `prompt_cache_key`, which "doesn't guarantee" a hit.
- The batch page says "50% discount", with endpoints including embeddings, chat, fim, moderations, ocr and classifications.

## Gateways and standards

### openrouter-accounting: CONFIRMED
- The usage-accounting page says `usage: {include: true}` is "deprecated and have no effect" and that usage is "always included". It shows cost, cost_details.upstream_inference_cost and cache_write_tokens.
- The get-generation page includes native_tokens_*, cache_discount, provider_name, service_tier, provider_responses, workspace_id, session_id, data_region, external_user and response_cache_source_id. I confirmed these in the page HTML.
- The prompt-caching page says "Sticky sessions expire after 10 minutes of inactivity" and that session_id has a limit of 256 characters.
- The response-caching page says `X-OpenRouter-Cache`, a default TTL of 300 s with a range of 1-86400 s, and "no billing (all billable usage counters are reported as 0)".
- The service-tiers page says "you are billed at that tier's rate".

### otel-genai-semconv: CONFIRMED
- In repo open-telemetry/semantic-conventions-genai, the last commit to main was 2026-09-22T06:40Z.
- `gen-ai.md` says input_tokens "SHOULD include all types of input tokens, including cached tokens", and that cache_read and cache_write "SHOULD be included in gen_ai.usage.input_tokens". It defines reasoning.output_tokens, per-modality attributes and gen_ai.conversation.compacted, and says instrumentations "SHOULD report the billed count".
- The provider enum exactly matches the report.
- `openai.md` has openai.request/response.service_tier. All are marked Development.

### price-feed-crosscheck: CONFIRMED
- The LiteLLM map has 4,214 entries, and its last commit was 2026-09-23T16:52Z. It includes keys `*_flex`, `*_priority`, `*_batches`, `*_above_200k_tokens`, `*_above_272k_tokens`, `cache_creation_input_token_cost_above_1hr`, `prompt_cache_min_tokens`, `off_peak_pricing` and `regional_endpoint_uplift_multiplier`.
- OpenRouter `/api/v1/models` has 456 models:

  | Model | OpenRouter | Primary source |
  |---|---|---|
  | openai/gpt-5.6-sol | prompt 0.000002, completion 0.00001 ($2/$10) | OpenAI pricing: $4/$20 |
  | x-ai/grok-4.7 | $1.6/$4.8 | xAI pricing: $2/$6 |

- Both discrepancies are reproduced.

### exact-token-counting: CORRECTED
- Confirmed that OpenAI `/v1/responses/input_tokens` "accepts the same input format as the Responses API ... images, files, tools, or conversations" and returns "the exact count".
- Confirmed that Bedrock CountTokens "doesn't incur charges" and "will match the token count that would be charged". It confirms that CRIS-only Claude models use Anthropic count_tokens on bedrock-mantle.
- The Anthropic token-counting page says "Token counting is free to use but subject to requests per minute rate limits".
- **Corrections:**
  1. "Free" is documented only for Bedrock and Anthropic. The OpenAI token-counting guide does not state that it is free or unbilled.
  2. The Gemini tokens page (updated 2026-09-23) demonstrates `count_tokens` over `contents` only. It demonstrates system-instruction and tool token counts through the *post-call* `interaction.usage` (total_input_tokens / total_tool_use_tokens), not through countTokens.

  Corrected claim: exact pre-call counting endpoints exist for OpenAI, Bedrock, Gemini and Anthropic. Bedrock and Anthropic document them as free. OpenAI's cost is not stated. Gemini's page does not show countTokens covering system instructions or tools, although post-call usage does.
