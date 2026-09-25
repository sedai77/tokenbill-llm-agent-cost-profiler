# Token Bill research track: measuring and auditing LLM bills

Track: papers-measurement. Research date: 2026-09-23.
Scope: how an enterprise-grade tool should measure, attribute, reconcile, and audit LLM and agent spend, and how accurate each measurement can be. Topics covered: hidden reasoning-token audits, model-substitution audits, tokenizer inflation across model generations, multilingual tokenization cost, token-counting APIs vs heuristics, cost-per-task metrics, LLM FinOps standards, and unit economics (cost per merged PR, cost per resolved ticket).

Method: every claim below comes from a source I opened during this pass (URL plus publication date or retrieval date). Anthropic documentation pages carry no publication date, so they are cited as "retrieved 2026-09-23". Evidence strength: **strong** = primary source (official docs, peer-reviewed paper, or arXiv paper with the number in its abstract or body), **moderate** = primary source with small n or a single practitioner measurement, **weak** = secondary report or unverified.

I also read Token Bill v0.1.2's source (`tokenbill/pricing.py`, `trace.py`, `analyzer.py`, `simulator.py`, `DESIGN.md`) to tie each finding to a concrete gap.

---

## 0. Executive summary

1. **Token Bill's "exact" billed ledger isn't exact on current Anthropic billing.** The `Usage` dataclass keeps only four fields. It prices every cache write at 1.25x, but 1-hour writes cost 2x. It has no row for Claude Opus 5.5 ($4/$20, cache reads at 0.05x). It ignores the Batch discount (50%), US-only inference (1.1x), fast-mode rates, the Bedrock/Vertex regional premium (+10%), web search ($10 per 1,000), code-execution hours, and Managed Agents session-hours. It also ignores `usage.iterations`. When compaction or the advisor tool is on, top-level usage **excludes** compaction and advisor tokens, and advisor tokens bill at a different model's rates. Fix this first. An enterprise lab will reconcile against the invoice on day one.
2. **The Admin APIs are the ground truth, and Token Bill should reconcile against them.** The Usage API (1m/1h/1d buckets, grouped by key, workspace, model, service tier, context window, inference_geo, speed), the Cost API (daily, decimal-cent strings), the Claude Code Analytics API (per user per day: PRs, commits, lines, estimated cost) and the Enterprise Analytics API (cost and usage revisable for 30 days) give authoritative numbers. Token Bill's unique value is explaining them per run, per task and per cause. A "percent of invoice explained" coverage metric is the enterprise trust anchor.
3. **chars/3.7 is too weak for enterprise attribution, and an exact alternative is free.** Measured chars-per-token on Claude spans roughly 2.7 (TypeScript, new tokenizer) to 4.3 (English, old tokenizer). The Claude 4.7-and-later tokenizer yields about 30% more tokens: 1.01x on CJK, up to 1.47x on English technical docs. Images are priced by 28x28 patches, not bytes. `count_tokens` is free (5k to 20k RPM) and model-specific. Cumulative-prefix differencing gives near-exact per-segment attribution. A 2026 paper (TCA) uses this "two-pass, non-billable count" approach.
4. **Tokenizer inflation is the biggest silent bill driver of 2026.** Per-token prices held flat or fell, yet Opus 4.7+ and the Fable and Mythos models bill ~30% more tokens for the same text. Opus 5 turns thinking on by default, and thinking bills as output. A "migration re-baseline" auditor that recounts real prompts under the target model is cheap to build and has direct value.
5. **Hidden-token auditing has hard limits and should be marketed honestly.** CoIn (94.7% inflation detection) and PALACE (predictive auditing) exist. A May 2026 paper then showed that all three published audit styles can be evaded, with up to 1,469% inflation against CoIn. Model-substitution audits from software alone also fail against production nondeterminism (Berkeley, 2025). Token Bill should provide statistical anomaly signals (reasoning ratio, tokens-per-char drift, served-vs-requested model) and label them as signals, not proof.
6. **Cost per completed task is the unit, and it needs distributions and error bars.** The same agent task varies up to 30x in tokens across runs. Two of 20 tasks carried 43% of spend in one Anthropic run. Failed runs still bill. Models cannot predict their own token usage (r ≤ 0.39). Savings claims need paired designs and confidence intervals: Anthropic uses about 50 cases x 5 trials for cutover decisions.
7. **Cache health now has published benchmarks and a first-party diagnostic.** Across a day of real agent-loop traffic, the median share of input served from cache is 84% and the top decile is ≥94%. Below 80%, something is breaking the cache. Anthropic's `cache-diagnosis-2026-04-07` beta returns `cache_miss_reason` (model, system, tools, messages changed). It works only on the first-party API, so Token Bill's breaker detection still matters for Bedrock, Vertex and Foundry, and for config-change breakers the diagnostic reports as `unavailable`.
8. **"Context tax" is the agentic cost driver.** In one instrumented 800-line PR (512 turns, ~$41), cache reads were 98% of billed tokens. One unnecessary 618-line file read cost ~2.7M cache-read tokens (~$0.54) because it rode along for 470 more turns. Token Bill can compute this residency cost for each content block exactly from traces. No competitor surfaces it.
9. **Interop standards arrive in Q4 2026.** FOCUS 1.5 (target ratification 2026-12-03) adds model identity (ModelDeveloper, ModelFamily, ModelId, ModelVersion), token properties (TokenCacheAction, TokenDirection), PrincipalId, and possibly a Recommendations dataset. The OpenTelemetry GenAI conventions define `gen_ai.usage.*`, and their `input_tokens` **includes** cached tokens, unlike Anthropic's. Claude Code emits OTel metrics, events and traces with attribution dimensions (query_source, agent, skill, MCP server, repo). Token Bill should import the OTel conventions and export in FOCUS 1.5 shape.

---

## 1. Gaps found in Token Bill v0.1.2 (from reading the code against current docs)

| # | Gap in v0.1.2 | Current rule (source) | Impact |
|---|---|---|---|
| G1 | `ModelPricing.cache_write_multiplier = 1.25` applied to all `cache_creation_input_tokens` | 1h writes cost 2x; usage reports `cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens` separately (prompt-caching doc) | 1h-TTL writes under-billed by 37.5% of write cost. Claude Code on subscriptions uses a 1h TTL (Claude Code costs doc) |
| G2 | No `claude-opus-5-5` row | Opus 5.5: $4 input, $5 5m-write, $8 1h-write, $0.20 read (0.05x), $20 output (pricing doc) | Unknown-model path returns no dollars for the current flagship |
| G3 | No batch, priority, fast, or inference_geo handling | Batch 50% off; `inference_geo:"us"` 1.1x on all categories for 4.6+; fast mode Opus 5.5 $8/$40, Opus 5/4.8 $10/$50; multipliers stack (pricing doc) | Dollar totals wrong for any org using these |
| G4 | No `server_tool_use` pricing | Web search $10/1k; code execution $0.05/container-hour after 1,550 free hours/month; web fetch free (pricing doc) | Missing line items |
| G5 | Ignores `usage.iterations` | Compaction: top-level usage excludes compaction iterations; sum `iterations` for the total. Advisor: top-level usage covers executor tokens only; `advisor_message` iterations bill at the advisor model's rates (compaction and advisor docs) | Systematic under-count on long agent sessions |
| G6 | Char heuristic applied to all message JSON, including base64 images | Image cost = ceil(w/28) x ceil(h/28) visual tokens, capped at 1,568 (standard) or 4,784 (4.7+ high-res) (vision doc) | Image segments over-attributed by orders of magnitude |
| G7 | Tool-use system-prompt overhead is invisible and spread proportionally | Per-model fixed overhead: 286 tokens (Opus 5.5, auto/none) to 675/804 (Opus 4.7); bash +244/325; text editor +700; computer toolset ~4,500; browser toolset ~6,600 (pricing doc) | Attribution error, and missed "tool overhead" savings |
| G8 | Thinking blocks with `display:"omitted"` render as empty text | Billed output = full thinking, not the visible summary; the billed count won't match the visible count (thinking doc) | Output attribution and "history" segments mis-sized |
| G9 | Floats for money | Admin APIs return decimal strings in cents; Enterprise cost data is revisable for 30 days (analytics docs) | Rounding and reconciliation drift at enterprise scale |
| G10 | No Managed Agents runtime | $0.08 per session-hour of `running` status (pricing doc) | Missing line item |
| G11 | Cache simulator lacks the 20-block lookback and config-change invalidation | Lookback ≤20 positions per breakpoint; thinking, effort, tool_choice, speed and image changes invalidate parts of the cache (prompt-caching and thinking docs) | Optimal-cache scenario over-promises savings in some runs |

---

## 2. Findings

### A. Billing ground truth and exactness

**F1. The Anthropic usage object has grown well past four counters (strong).**
A correct per-call cost now needs: `input_tokens`, `cache_read_input_tokens`, `cache_creation.ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens` (5m at 1.25x, 1h at 2x), `output_tokens`, `server_tool_use.{web_search_requests, web_fetch_requests, code_execution_requests}`, the request's service tier (batch 50%), `inference_geo` (1.1x for Claude 4.6+), `speed` (fast-mode rate card), and `usage.iterations` (compaction and advisor sub-inferences). Cache reads cost 0.1x on most models, 0.05x on Opus 5.5 and 0.025x on Fable/Mythos 5.1. Anthropic's own cost guide prices "five priced token counts in each response's usage at their own rates", summed across the task.
Sources: https://platform.claude.com/docs/en/about-claude/pricing (retrieved 2026-09-23); https://platform.claude.com/docs/en/build-with-claude/prompt-caching (retrieved 2026-09-23); https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence (retrieved 2026-09-23).

**F2. Top-level usage undercounts when server-side sub-inferences run (strong).**
With compaction, top-level `input_tokens` and `output_tokens` exclude compaction iterations, and the docs say to sum `usage.iterations` for the billed total. With the advisor tool, top-level usage reflects executor tokens only. `advisor_message` iterations bill at the advisor model's rates, and the top-level `max_tokens` does not bound advisor output. Any tool that reads only top-level usage (Token Bill today, and most proxies) under-reports these sessions.
Sources: https://platform.claude.com/docs/en/build-with-claude/compaction-threshold (retrieved 2026-09-23); https://platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool (retrieved 2026-09-23).

**F3. Invisible, fixed per-request overhead tokens are documented per model (strong).**
Whenever tools are present, the API adds a tool-use system prompt. It is 286 tokens on Opus 5.5 (auto/none), 354/474 on Sonnet 5 and 675/804 on Opus 4.7. Bash adds 244 to 325 tokens, the text editor ~700, the computer-use toolset ~4,500 and the browser toolset ~6,600. Web search results bill as input on later turns. Token-counting results may include system-added tokens that are **not** billed. These should be separate attribution segments, not smeared across the request.
Sources: https://platform.claude.com/docs/en/about-claude/pricing (retrieved 2026-09-23); https://platform.claude.com/docs/en/build-with-claude/token-counting (retrieved 2026-09-23).

**F4. Authoritative reconciliation sources exist, with known latencies and quirks (strong).**
- Usage API `/v1/organizations/usage_report/messages`: buckets of 1m (up to 1,440), 1h (up to 168) or 1d (up to 31). Filter and group by API key, workspace, model, service tier, context window, inference_geo, and speed (beta). Data lands within about 5 minutes.
- Cost API `/v1/organizations/cost_report`: daily only, USD as decimal strings in cents. It excludes Priority Tier costs. Code execution appears there but not in the usage endpoint.
- Claude Code Analytics API: per user per day. Sessions, lines added and removed, commits, PRs, Edit/Write tool accept and reject counts, and per-model tokens with `estimated_cost`. Up to 1h delay. Covers first-party API usage only, not Bedrock, Vertex or Foundry.
- Enterprise Analytics API: per-user cost and usage across chat, Claude Code and Cowork. Values can be revised for 30 days, and Anthropic advises querying dates at least 30 days old for invoicing-grade totals. Default rate limit is 60 RPM.

Sources: https://platform.claude.com/docs/en/manage-claude/usage-cost-api ; https://platform.claude.com/docs/en/manage-claude/claude-code-analytics-api ; https://platform.claude.com/docs/en/manage-claude/analytics-api (all retrieved 2026-09-23).

**F5. Claude Enterprise is now usage-billed at API rates, so API-rate modeling covers IDE and agent usage (strong).**
Usage-based Enterprise plans charge seats plus usage "at standard API rates", with org-level and per-user spend caps. US-only inference costs 1.1x. Claude Code's own `/usage` figures are computed locally at list price unless an admin deploys a `modelPricing` managed setting with contracted rates. Anthropic states the figure is an estimate and that the Console is authoritative.
Sources: https://support.claude.com/en/articles/11526368-how-am-i-billed-for-my-enterprise-plan (retrieved 2026-09-23); https://code.claude.com/docs/en/costs (retrieved 2026-09-23).

**F6. Enterprise baseline: about $13 per developer per active day for Claude Code (strong, vendor-reported).**
Anthropic reports an enterprise average of about $13 per developer per active day, $150 to $250 per developer per month, with 90% of users below $30 per active day. Agent teams use about 7x the tokens of a standard session when teammates run in plan mode. Background usage is typically under $0.04 per session. Per-user TPM guidance runs from 200k to 300k (1 to 5 users) down to 10k to 15k (500+ users).
Source: https://code.claude.com/docs/en/costs (retrieved 2026-09-23).

### B. Token counting and attribution accuracy

**F7. The Claude 4.7-and-later tokenizer yields about 30% more tokens for the same text, unevenly by content (strong).**
Anthropic's pricing and token-counting docs say Claude 4.7 and later (including Fable and Mythos) produce about 30% more tokens, varying by content. They tell users to recount against the target model rather than reuse old counts. Independent measurements with `count_tokens` agree:
- Simon Willison: a system prompt came out at 1.46x on Opus 4.7 vs 4.6, and a 30-page PDF at 1.08x. A large image came out at 3.01x, driven by the high-res image tier.
- Abhishek Ray: real Claude Code content averaged 1.325x (CLAUDE.md 1.445x, code diff 1.212x). Synthetic samples ranged from Japanese and Chinese prose at 1.01x to English technical docs at 1.47x. English chars-per-token fell from 4.33 to 3.60 and TypeScript from 3.66 to 2.69. He estimated an 80-turn session at +20% to +30% cost.

Sources: https://platform.claude.com/docs/en/about-claude/pricing (retrieved 2026-09-23); https://platform.claude.com/docs/en/build-with-claude/token-counting (retrieved 2026-09-23); https://simonwillison.net/2026/apr/20/claude-token-counts/ (2026-04-20); https://www.claudecodecamp.com/p/i-measured-claude-4-7-s-new-tokenizer-here-s-what-it-costs-you (2026-04-16).

**F8. chars/3.7 carries roughly ±25% error or worse by content class, and other vendors' tokenizers are wrong for Claude (strong for the range, moderate for cross-vendor numbers).**
The measured chars-per-token range above (2.69 to 4.33) gives about -27% to +17% error against 3.7 before counting CJK, base64 or images. Claude 3.5 Sonnet produced about 16% more tokens than GPT-4o on English, 21% more on math and 30% more on Python. Anthropic's bundled claude-api skill says tiktoken undercounts Claude by 15% to 20% on typical text, and by more on code and non-English input. Anthropic has not published a Claude 3+ tokenizer, and Langfuse notes that the legacy Anthropic tokenizer is inaccurate for Claude 3+. Anthropic's own rule of thumb is ~4 characters per token for English, and Gemini's is also ~4.
Sources: https://venturebeat.com/ai/hidden-costs-in-ai-deployment-why-claude-models-may-be-20-30-more-expensive-than-gpt-in-enterprise-settings (2025-05-01); https://langfuse.com/docs/observability/features/token-and-cost-tracking (retrieved 2026-09-23); https://ai.google.dev/gemini-api/docs/tokens (retrieved 2026-09-23); https://github.com/javirandor/anthropic-tokenizer (retrieved 2026-09-23; a reverse-engineered approximation, not official).

**F9. `count_tokens` is free, fast, model-specific, and good enough for exact-by-construction attribution (strong).**
`POST /v1/messages/count_tokens` accepts system, tools, images, PDFs and thinking blocks. It is free, and its rate limits (5,000 / 10,000 / 20,000 RPM by tier) are separate from message limits. It is GA on Claude API, AWS, Bedrock, Vertex and Foundry. Caveats: the count is an "estimate" that may differ by a small amount, it may include unbilled system-added tokens, it ignores caching, and it rejects server tools and URL/file-sourced images. A September 2026 paper (Total Cost of Agency) uses a two-pass, non-billable token count to attribute memory-injection cost exactly rather than from word-count proxies. It measured memory injection at about 12% of billed cost, rising to 27.6% of tokens at workflow depth 6.
Sources: https://platform.claude.com/docs/en/build-with-claude/token-counting (retrieved 2026-09-23); https://arxiv.org/abs/2609.23790 (2026-09-20).

**F10. Images and thinking break byte-based estimation (strong).**
Images cost ceil(w/28) x ceil(h/28) visual tokens. The cap is 1,568 on the standard tier and 4,784 on the high-res tier (Claude 4.7+), so up to ~3x more for the same image. A 4K screenshot costs about $23.92 per thousand images on Opus 5. Thinking bills as output even when `display:"omitted"` returns an empty block. Anthropic says the billed output count won't match what you see. Prior-turn thinking counts as input on "keep-all" models and is stripped on "last-turn-only" models. Changing thinking or effort settings invalidates the cache.
Sources: https://platform.claude.com/docs/en/build-with-claude/vision (retrieved 2026-09-23); https://platform.claude.com/docs/en/build-with-claude/thinking (retrieved 2026-09-23).

**F11. Model-generation changes shift output spend as well as input (strong).**
Opus 5 turns adaptive thinking on by default, and thinking tokens bill as output. Anthropic tells teams to "re-baseline cost" for workloads that ran without thinking on Opus 4.8. Default responses also run longer. Effort now ranges from low to max. On SWE-bench Pro, `medium` gave up about 2 points for 50% lower cost and `low` about 8 points for 75% lower cost. A 16,384-token `max_tokens` cap truncated 15% of Opus 5 attempts and 43% of Fable 5.1 attempts, with no gain in cost per solved task.
Sources: https://platform.claude.com/docs/en/models/opus-5/whats-new-opus-5 (retrieved 2026-09-23; served when requesting the Opus 4.7 page); https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence (retrieved 2026-09-23).

### C. Hidden tokens and provider auditing (what can and cannot be verified)

**F12. Hidden reasoning tokens are most of the bill, and auditing them is an active research area (strong).**
- *Invisible Tokens, Visible Bills* (position paper, 2025-05-24) names two risks, quantity inflation and quality downgrade. It proposes a three-layer audit framework: execution, secure logging, and user-facing auditability.
- *CoIn* (2025-05-19) verifies hidden-token counts using hash trees over token-embedding fingerprints plus relevance matching. It detects inflation with success up to 94.7%.
- *PALACE* (2025-07-29) predicts hidden reasoning-token counts from prompt-answer pairs, using GRPO-tuned domain adapters with a router.

Sources: https://arxiv.org/abs/2505.18471 (2025-05-24); https://arxiv.org/abs/2505.13778 (2025-05-19); https://arxiv.org/abs/2508.00912 (2025-07-29).

**F13. Published audits can be evaded, so a client-side tool can offer signals, not guarantees (strong).**
*Token Inflation* (2026-05-28) shows three attack types:
- CoIn evaded with up to 1,469% inflation, so a $100 honest bill becomes about $1,569.
- PALACE manipulated, with 247% mean inflation on triggered samples.
- A statistical (martingale) auditor evaded with 50.85% over-reporting.

The authors conclude that trustworthy billing needs evidence the provider does not control: TEEs, cryptographic proofs of inference, or third-party re-execution. *Is Your LLM Overcharging You?* (ICML 2026 oral, v4 2026-05-28) proves that pay-per-token creates an incentive to misreport tokenization. Its fix is pricing linear in character count, and it shows how providers can keep margins under that scheme.
Sources: https://arxiv.org/abs/2605.30040 (2026-05-28); https://arxiv.org/abs/2505.21627 (v1 2025-05-27, v4 2026-05-28).

**F14. Model-substitution and quality-drift audits from software alone are weak, but served-model accounting is not (strong).**
- Cai et al. (UC Berkeley, 2025-04-07, rev. 2025-09-29): text classifiers, benchmarks and logprob tests fail against production nondeterminism. TEEs are the robust answer.
- Model Equality Testing (ICLR 2025): MMD tests reach 77.4% median power with about 10 samples per prompt. 11 of 31 endpoints serving Llama models had different output distributions from the reference weights.
- A rank-based uniformity test (2025-06-08, rev. 2026-04-09) improves query efficiency.
- Anthropic's September 2025 postmortem: a routing bug hit up to 16% of Sonnet 4 requests on its worst day, and about 30% of Claude Code users saw at least one degraded message. Anthropic's evals "didn't capture" it, and user reports did.
- Chen, Zaharia and Zou (2023) documented large behavior and verbosity drift in the "same" API model.

For Token Bill, the verifiable part is accounting: requested vs served `model`, server-side `fallbacks` (a beta that can route a request to a different model), cache diagnostics' `model_changed`, and tokens-per-task drift after vendor changes.
Sources: https://arxiv.org/abs/2504.04715 (2025-04-07); https://arxiv.org/abs/2410.20247 (2024-10-26; ICLR 2025); https://arxiv.org/abs/2506.06975 (2025-06-08); https://www.anthropic.com/engineering/a-postmortem-of-three-recent-issues (2025-09-17); https://arxiv.org/abs/2307.09009 (2023-07); https://platform.claude.com/docs/en/models/opus-5/whats-new-opus-5 (retrieved 2026-09-23).

**F15. Prompt-cache behavior and isolation are auditable, and gateway designs change both cost and risk (strong).**
- Gu et al. (ICML 2025) used timing audits to detect prompt caching. They found global cross-user cache sharing at 7 API providers, including OpenAI.
- Anthropic isolates caches per workspace on the Claude API, Claude Platform on AWS and Foundry. Bedrock and Vertex isolate per organization.
- KeyPooling (2026-08-18) found that none of five open-source gateways isolated customers' upstream credentials by default. All five exposed cross-customer cache reads for OpenAI and Anthropic. On OpenRouter, 12 of 28 labels, carrying 33.7% of volume, showed cross-account reads. Namespace splits placed after public prefixes kept most reuse at a 1.7% to 2.5% cost increase.
- CacheProbe (SAGAI '26) audits OpenRouter cache isolation.

Sources: https://arxiv.org/abs/2502.07776 (2025-02-11); https://platform.claude.com/docs/en/build-with-claude/prompt-caching (retrieved 2026-09-23); https://arxiv.org/abs/2608.17485 (2026-08-18); https://arxiv.org/abs/2605.30613 (2026-05-28).

### D. Multilingual tokenization cost

**F16. Language is a first-order cost driver, and the Claude 4.7+ tokenizer reshuffles it (strong).**
- Petrov et al. (NeurIPS 2023): tokenized length for the same text differs up to 15x across languages.
- Ahia et al. (2023): speakers of many languages are overcharged on commercial APIs while getting worse results.
- The Tokenizer Tax across 25 European languages (2026-05-23): English at about 1.2 tokens per word, Romance 1.5 to 1.7, Slavic 2.2 to 2.5, Greek and Maltese about 3.1, and Ukrainian 15% to 18% above related Slavic languages. Rankings hold across domains (rho > 0.97).
- The Tokenization Equity Audit (2026-08-10): Bengali at 1.56x and Yoruba at 2.37x English on GPT-4o's tokenizer. A 128k window becomes about 82k English-equivalent for Bengali.
- The Claude 4.7+ tokenizer inflates English and code by 1.2x to 1.47x but CJK only 1.01x (F7). Relative costs across languages therefore changed with the model generation.

Sources: https://arxiv.org/abs/2305.15425 (2023-05-17; NeurIPS 2023); https://arxiv.org/abs/2305.13707 (2023-05-23); https://arxiv.org/abs/2605.24718 (2026-05-23); https://arxiv.org/abs/2608.09046 (2026-08-10).

### E. Cache measurement

**F17. Cache hit rate now has published benchmarks and a first-party miss diagnosis (strong).**
Anthropic reports a median of 84% of input served from cache over a day of real agent-loop traffic. The top 10% of harnesses reach ≥94%, and below 80% signals a breaker. Well-built loops pay full price on under 1% of input deep in a task. Caching cut agent-loop cost 2.5x to 3.7x at 81% to 90% hit rates, and 83% on a triage agent.

The cache-diagnostics beta (`cache-diagnosis-2026-04-07`, Claude API only) compares a request with `previous_message_id`. It returns `cache_miss_reason.type`: model_changed, system_changed, tools_changed, messages_changed, previous_message_not_found, or unavailable. The `*_changed` types carry `cache_missed_input_tokens`, a byte-derived magnitude estimate that is not a billing number. Config changes (tool_choice, thinking, output_config, beta headers) come back as `unavailable`.

Claude Code v2.1.251+ reports main-thread cache stats. It counts a miss when a request reprocesses more than 5% and at least 2,000 tokens of cacheable content, and it tracks compaction and tool-result clearing separately as "expected rebuilds".
Sources: https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence (retrieved 2026-09-23); https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics (retrieved 2026-09-23); https://code.claude.com/docs/en/costs (retrieved 2026-09-23).

**F18. The "context tax" dominates agentic coding bills and can be measured per content block (strong for the trace, moderate for generality).**
Sonar instrumented one 800-line PR (2026-09-01):
- 512 round-trips, 106k fresh input tokens, 3.1M cache-write tokens, 152.8M cache-read tokens and 289k output tokens, about $41 in total.
- Peak context was 458.7k tokens.
- Reading a full 618-line file instead of the 67 lines needed cost about 2.7M cache-read tokens (about $0.54), because the content stayed in context for about 470 more turns.
- Across 18 PRs: an average of about 234M context tokens, 700 round-trips and $65 per PR (median $52).

The academic literature agrees that input tokens dominate agentic cost:
- Bai et al. (2026-04-24): agentic tasks use about 1000x the tokens of code chat, input dominates even with caching, and the same task varies up to 30x across runs.
- Tokenomics (2026-01-20): input was 53.9% of tokens on average, and code review took 59.4%.

Sources: https://www.sonarsource.com/blog/stop-the-context-tax/ (2026-09-01); https://arxiv.org/abs/2604.22750 (2026-04-24); https://arxiv.org/abs/2601.14470 (2026-01-20).

### F. Cost per task and unit economics

**F19. Cost per completed task is the correct unit (strong).**
Cost-of-Pass (2025, rev. 2026-02) defines the expected cost of producing a correct solution, plus a frontier cost-of-pass across models and human experts. The frontier roughly halved every few months on complex quantitative tasks. Majority voting and self-refinement rarely justified their cost. *AI Agents That Matter* (2024) argues that accuracy-only benchmarking produced needlessly costly agents and calls for joint cost-accuracy optimization. Anthropic's guide says to compare "cost per completed task" and to price the tail. Results:
- SWE-bench Pro: Fable 5.1 at `low` effort solved 88.6% at $0.54 per solved task vs Sonnet 5 at 77.4% and $0.84, despite a 5x per-token price.
- Terminal-Bench 3: cost per solved task was $183 on Opus 4.7, $63 on Opus 4.8 and $28 on Opus 5.
- Re-running failures: start at `low` and retry failures at default. This kept about 93% pass at about 50% of the cost ($0.45 vs $0.93 per solved task).

Sources: https://arxiv.org/abs/2504.13359 (2025-04-17, rev. 2026-02-26); https://arxiv.org/abs/2407.01502 (2024-07-01); https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence (retrieved 2026-09-23).

**F20. Agent cost is heavy-tailed, noisy, and poorly self-predicted, so measurement needs distributions and error bars (strong).**
- Same-task token use varies up to 30x across runs, frontier models predict their own token use at r ≤ 0.39 and underestimate it, and accuracy peaks at intermediate cost (Bai et al.).
- In one 20-problem run, 2 problems carried 43% of spend and the cheapest half 10% (Anthropic).
- HAL (21,730 rollouts across 9 models and 9 benchmarks, about $40k, 2.5B tokens) found that higher reasoning effort *reduced* accuracy in most runs. Log inspection turned up agents searching for benchmark answers.
- Miller (2024) recommends CLT standard errors, clustered SEs, paired differences and power analysis for evals. Anthropic uses about 50 cases x 5 trials for production cutovers.
- A single cheap probe forecast token distributions for unseen tasks within 36%. Minimal user stories instead of detailed specs raised spend 29.7%, with task sensitivity from 13% to 115% (Smékal, 2026-08-26, 2,700 runs).
- Incorrect reasoning traces run longer (Su et al., 2025), and o1-style models overthink easy problems (Chen et al., 2024).

Sources: https://arxiv.org/abs/2604.22750 ; https://arxiv.org/abs/2510.11977 (2025-10-13); https://arxiv.org/abs/2411.00640 (2024-11-01); https://arxiv.org/abs/2608.25399 (2026-08-26); https://arxiv.org/abs/2505.00127 (2025-04-30); https://arxiv.org/abs/2412.21187 (2024-12-30); Anthropic cost guide (retrieved 2026-09-23).

**F21. Cost per merged PR and cost per resolved ticket are the business units, and harness choice matters as much as model (moderate).**
- Sonar measured a median of $52 per PR in one repo (F18).
- A July 2026 test of 23 model-and-harness combos on one production feature (RFC 8628 device flow) found:
  - Cost to merge ranged from about $2.59 to $33.38.
  - The same model cost about 2.5x more on one CLI harness than another.
  - A cheap coder shifted cost onto the review gate, where 97% of one $2.81 merge was review.
  - "Merged" didn't mean "correct": approved code failed 12 to 38 of 38 unit tests.
- Support agents are already sold per outcome: Intercom Fin charges $0.99 per outcome, with a resolution defined as the customer confirming or not asking for more help.
- The DX AI Measurement Framework (2026-05-20) recommends tracking AI spend per developer, net time gain (time saved minus AI spend), and agent hourly rate.
- METR's randomized trials warn that self-reported speedups are unreliable. Early-2025 developers were 19% slower while believing they were 20% faster. Late-2025 estimates were -18% (CI -38% to +9%) and -4% (CI -15% to +9%), with strong selection-bias caveats.

Sources: https://blog.insight-services-apac.dev/2026/07/06/cost-to-a-merged-feature (2026-07-06); https://fin.ai/help/en/articles/13975800-fin-pricing-outcomes (retrieved 2026-09-23); https://getdx.com/blog/ai-measurement-framework-guide/ (2026-05-20); https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/ (2025-07-10); https://metr.org/blog/2026-02-24-uplift-update/ (2026-02-24).

**F22. Per-token prices mislead and the fair comparisons use provider-reported counts (strong).**
Epoch AI finds inference price for fixed capability falling 9x to 900x per year, and notes that per-token price ignores differences in token efficiency between models. Artificial Analysis prices input, cache hits, cache writes, reasoning and answer tokens from provider-reported counts. It combines them with live typical cache-hit rates and falls back to a canonical tokenizer only when counts are missing. On the same SWE-bench tasks, Kimi-K2 and Claude Sonnet 4.5 used over 1.5M more tokens than GPT-5 (Bai et al.).
Sources: https://epoch.ai/data-insights/llm-inference-price-trends (2025-03-12); https://artificialanalysis.ai/methodology/intelligence-benchmarking (retrieved 2026-09-23); https://arxiv.org/abs/2604.22750 (2026-04-24).

### G. FinOps standards, telemetry and interop

**F23. FOCUS 1.5 (target 2026-12-03) will carry model identity and token semantics (strong).**
FOCUS 1.4 (ratified 2026-06-04) added invoice and commitment datasets but **no** token columns. Several blogs claim otherwise and are wrong. The 1.5 scope includes:
- `SkuPriceDetails` properties ModelDeveloper, ModelFamily, ModelId and ModelVersion (merged)
- TokenCacheAction and TokenDirection (in review)
- a `PrincipalId` column (merged)
- worked examples for per-token billing, first-party models served by clouds, and marketplace invoices
- a possible Recommendations dataset (stretch)

The FinOps Foundation's token-economics guidance (2026-05-10) adds "token yield rate": the share of generated tokens that led to a downstream business action after retries, abandonment and failed QA. FinOps for AI (2026-02-17) lists cost per inference, cost per token, cost per API call, anomaly detection rate and ROI.
Sources: https://www.finops.org/insights/introducing-focus-1-4/ (2026-06); https://focus.finops.org/focus-1-5-release-scope/ (retrieved 2026-09-23); https://www.finops.org/insights/token-economics-the-atomic-unit-of-ai-value/ (2026-05-10); https://www.finops.org/wg/finops-for-ai-overview/ (2026-02-17); https://siliconangle.com/2026/06/08/ai-token-economics-focus-specification-updates-finopsx/ (2026-06-08).

**F24. OpenTelemetry GenAI conventions define token attributes, with a normalization trap (strong).**
The conventions define `gen_ai.usage.input_tokens`, which **should include cached tokens**, and `output_tokens`. Also defined: `cache_read.input_tokens`, `cache_write.input_tokens` and `reasoning.output_tokens` as subsets, plus per-modality splits. All are in Development status, and `anthropic` is a well-known `gen_ai.provider.name`. Anthropic's native `input_tokens` excludes cache reads and writes, so a naive mapping undercounts. Langfuse normalizes inclusive counts into exclusive buckets to avoid double counting. It also says tokenizer-based inference is unsupported for reasoning models and that ingested usage should be used for Claude 3+. LiteLLM attributes spend by key, user, team, tag and end user from a public price map, and ships a "cost doesn't match your bill" workflow.
Sources: https://github.com/open-telemetry/semantic-conventions-genai (docs/gen-ai/gen-ai-spans.md, retrieved 2026-09-23); https://langfuse.com/docs/observability/features/token-and-cost-tracking (retrieved 2026-09-23); https://docs.litellm.ai/docs/proxy/cost_tracking (retrieved 2026-09-23).

**F25. Claude Code exposes rich attribution telemetry, and its local JSONL logs are a trap (strong for OTel, moderate for the JSONL findings).**
With `CLAUDE_CODE_ENABLE_TELEMETRY=1`, Claude Code emits:
- `claude_code.cost.usage` and `claude_code.token.usage` (types input, output, cacheRead, cacheCreation)
- attributes: model, `query_source` (main, subagent, auxiliary), speed, effort, agent.name, skill.name, plugin.name, mcp_server.name and mcp_tool.name
- counters for sessions, lines of code, PRs, commits, edit decisions and active time
- events: user_prompt, assistant_response, tool_result, api_request and api_error, with `prompt.id`, `client_request_id` and `request_id` for correlation
- optional VCS repository attributes (v2.1.269+) and trace spans (`claude_code.llm_request`)

The docs say cost there is an approximation. A February 2026 analysis found that in Claude Code transcript JSONL, 75% of entries carried streaming-placeholder `input_tokens` of 0 or 1. Input was undercounted 100x to 174x and output 10x to 17x, and 51% to 55% of entries were duplicates per `requestId`. Log-reading tools such as ccusage have hit both over- and under-counting dedup bugs. ccusage issue #888 (2026-03-11) found that keeping the first-seen entry per `message.id:requestId` gave 130,785 output tokens for one day, against 648,562 when keeping the latest (about 5x under). In 551 of 606 duplicate groups, the output counts differed.
Sources: https://code.claude.com/docs/en/monitoring-usage (retrieved 2026-09-23); https://gille.ai/en/blog/claude-code-jsonl-logs-undercount-tokens/ (2026-02-24); https://github.com/ryoppippi/ccusage/issues/888 (2026-03-11).

**F26. Multi-provider usage semantics differ, so normalization is mandatory (strong).**
- OpenAI GPT-5.6+: caching is automatic; cache writes are 1.25x and reads 0.1x; TTL is 30 minutes. Usage fields are `input_tokens_details.cached_tokens` and `cache_write_tokens`. Reasoning tokens are hidden, billed as output, and reported in `output_tokens_details.reasoning_tokens`.
- Gemini: bills the full thought tokens, not the visible summary. Usage fields are `total_thought_tokens` and `total_cached_tokens`. Implicit caching is on for 2.5+, with minimums of 2,048 to 4,096 tokens. An image of 384px or less costs 258 tokens.
- Anthropic: has no separate reasoning-token field; thinking is folded into `output_tokens`.

Sources: https://developers.openai.com/api/docs/guides/prompt-caching (retrieved 2026-09-23); https://developers.openai.com/api/docs/guides/reasoning (retrieved 2026-09-23); https://ai.google.dev/gemini-api/docs/thinking (retrieved 2026-09-23); https://ai.google.dev/gemini-api/docs/caching (retrieved 2026-09-23); https://ai.google.dev/gemini-api/docs/tokens (retrieved 2026-09-23).

**F27. Semantic cost profiling of long-horizon agents is an emerging research area (moderate).**
AgentPProf (2026-09-14) attributes resource use to task intents rather than code paths, using a semantic operation stack and recursive segmentation of trajectories. It reports 0.764 B³ F1 against human annotations and up to 56% MAP gains on localization benchmarks. The Cost-Utility Alignment framework (2026-08-25) runs profiling, then utility attribution, then misalignment diagnosis in five categories, then adaptation, then evaluation. It gives no numbers. Together they point toward "flame graphs by intent" for agent spend.
Sources: https://arxiv.org/abs/2609.20301 (2026-09-14); https://arxiv.org/abs/2608.26195 (2026-08-25).

**F28. The same model differs as a service across hosts (moderate).**
A May 2026 measurement study of hosted open-weight APIs shows the same model varies across providers in latency, throughput, context length and error semantics. Pricing was the most stable dimension. Routing cut Qwen3-32B cost 37.8%. Kimi's K2 Vendor Verifier (about 4,000 requests per provider, 15 to 17 providers) found the official API at 100% tool-call schema accuracy, while some third-party vendors scored 50% to 84%. For Claude, Bedrock and Vertex regional endpoints carry a 10% premium over global, and some features differ by platform (for example, cache diagnostics is first-party only).
Sources: https://arxiv.org/abs/2605.02821 (2026-05-04); https://github.com/MoonshotAI/K2-Vendor-Verifier (retrieved 2026-09-23); https://platform.claude.com/docs/en/about-claude/pricing (retrieved 2026-09-23).

---

## 3. What Token Bill should build (measurement track)

Priorities: **P0** is required before enterprise submission, because it is billing correctness and trust. **P1** is the differentiators no competitor has. **P2** covers standards and research-grade features.

### P0: a billing ledger an auditor can sign off

1. **Usage schema v2 (trace@2).** Store the full usage object: `cache_creation.ephemeral_5m/1h`, `server_tool_use.*`, `service_tier`, `inference_geo`, `speed`, `usage.iterations[]` with type and model, `stop_reason`, request_id, response `model` (served) next to request `model`, and workspace_id or api_key_id. Keep backward compatibility with trace@1.
2. **Rate engine v2.**
   - Effective-dated price rows (`valid_from`, `valid_to`, source URL, retrieval date), with Opus 5.5 and every active or legacy model added.
   - Multipliers for 5m/1h writes, per-model read rates, batch 50%, inference_geo 1.1x, fast-mode tables, Bedrock/Vertex regional +10%, web search $10/1k, code-execution hours, Managed Agents $0.08/session-hour, and CCU conversion ($0.01/CCU) for AWS and Foundry marketplaces.
   - Contract rate cards: a multiplier plus per-model overrides, mirroring Claude Code's `modelPricing`.
   - `Decimal` math end to end.
   - A CI job that diffs the table against the live pricing page.
3. **Iteration-aware pricing.** Sum `usage.iterations` whenever present and price `advisor_message` iterations at the advisor model's rates. Flag compaction iterations as their own category ("context maintenance").
4. **Reconciliation service.** Pull the Usage API (1h buckets), Cost API (daily), Claude Code Analytics and Enterprise Analytics. Match them against trace-derived totals by model, workspace, key and day. Publish:
   - **Coverage**: % of invoice dollars explained by traces.
   - **Ledger error**: trace-priced vs cost_report per model-day, with a target of ≤0.5% where coverage is ~100%.
   - **Effective realized rate**: cost ÷ tokens per model and token type, which exposes negotiated discounts.
   - Handling for the 30-day revision window, `null` workspace (default) and `null` api_key (playground).
5. **Importer hardening.** Add importers for Claude Code OTel metrics, events and traces (the preferred source), with dedup by `request_id` and `client_request_id`. Add a Claude Code JSONL importer only with requestId/message.id dedup and a "placeholder usage" detector that refuses to price entries with input 0 or 1. Add Anthropic Batch results, Bedrock/Vertex usage, and an OpenAI/Gemini normalizer (F26).

### P1: attribution and diagnosis that competitors can't match

6. **Exact segment attribution via `count_tokens`.** For each call, count cumulative prefixes at segment boundaries (tools, system, each message, each content block) under the call's model. Differences give per-segment tokens. Memoize by content hash, because agent loops re-send prefixes and only new blocks need counting. Batch within the 5k to 20k RPM free limit. Subtract documented fixed overheads (F3) as a separate "provider overhead" segment. Keep chars/3.7 only as an offline fallback, labeled with a calibrated error band.
7. **Calibrated offline estimator.** Fit per-model, per-content-class chars-per-token (prose, code by language, JSON, CJK, logs) from the org's own billed usage and count_tokens samples. Report MAPE per class. Compute images from dimensions (F10), never from base64 length.
8. **Tokenizer and model migration auditor** (`tokenbill rebaseline --to claude-opus-5-5`). Sample real requests from traces and recount them under the target model. Report inflation per content class, cache minimum changes (512 on Opus 5/5.5 vs 1,024 to 4,096 on older models), tool-overhead change (675 → 286 tokens), thinking-default output shift (Opus 5), and projected $ per task with CIs. This directly answers "why did our bill go up 30% when the price didn't change?"
9. **Context-tax (residency) profiler.** For each content block, compute tokens × turns it stays in context × read (or uncached) price, then rank the top over-reads (whole-file reads, huge tool results, repeated schemas). This is the Sonar finding generalized and computed exactly from traces. Pair it with a turn-count tax curve (cost vs turns), since cache reads grow roughly quadratically with turns.
10. **Cache health scorecard.**
    - Hit rate = cache_read ÷ total input, benchmarked against 84% median, ≥94% top decile, and <80% as a breaker alarm.
    - Adopt Claude Code's miss definition (>5% and ≥2,000 tokens) and its "expected rebuild" class.
    - Ingest `diagnostics.cache_miss_reason` where present as ground truth for first-party traffic, and keep Token Bill's detectors for Bedrock, Vertex and Foundry.
    - New breakers: config-change (thinking, effort, tool_choice, speed, images, beta headers), lookback-overrun (>20 blocks since the last write), TTL-expiry (diagnostics null plus low reads), 1h-vs-5m TTL advice from inter-call gap distributions, and cross-workspace fragmentation (the same prefix hash written in several workspaces, F15).
11. **Cost per outcome.** Join traces to outcomes: PRs, commits and accepted edits from the Claude Code Analytics API, VCS attributes from OTel, ticket resolutions, test pass/fail. Report:
    - cost per merged PR, per accepted edit, per resolved ticket, and cost-of-pass per workload
    - token yield rate
    - failed-run spend share and retry spend
    - p50/p90/p99 and tail concentration (share of spend in the top 10% of tasks)
    - the split by query_source (main vs subagent vs auxiliary), agent, skill and MCP server
12. **Statistically honest savings.** Every "saved $X" claim must come with a paired before/after comparison and a bootstrap or clustered-SE CI. Mark it "verified" only when the CI excludes zero, otherwise "projected". Provide a power calculator (how many tasks and trials are needed to detect a 10% change given observed variance). This is what makes an enterprise lab trust the tool.

### P2: standards, audit signals, forecasting

13. **FOCUS 1.5-shaped export** (ModelDeveloper, ModelFamily, ModelId, ModelVersion, TokenCacheAction, TokenDirection, PrincipalId). Also emit recommendations in the draft Recommendations dataset shape if it ships. Add OTel GenAI export with correct inclusive/exclusive mapping (F24).
14. **Audit-signal panel, labeled as signals and not proof** (F12 to F14):
    - hidden-reasoning ratio (output tokens minus visible-text tokens, via count_tokens) by model and effort, with drift alarms
    - tokens-per-character drift by model and content class (detects tokenizer changes and anomalies)
    - served-vs-requested model and fallback accounting
    - usage-vs-usage_report reconciliation deltas
    - Document the limits plainly, citing the Token Inflation and Cai et al. results.
15. **Multilingual cost lens.** Report tokens per char or word by detected language vs English, per model, and flag workflows where language is the main multiplier (F16).
16. **Budget forecasting.** Set per-task budgets from p90 of observed tokens. Use a cheap-probe forecaster (Smékal-style) for new task types, with forecast error reported. Add per-team budget burn-down reconciled to Admin API data.
17. **Intent-level flame graphs** (AgentPProf-style) for long sessions: attribute spend to plan, explore, edit, test and review phases.

### Metric dictionary (proposed, with accuracy class)

| Metric | Definition | Accuracy class |
|---|---|---|
| Billed cost | Σ over calls and iterations of the priced token categories, server tools and runtime | Exact, given correct usage and rate card, reconciled to cost_report |
| Coverage | Trace-explained $ ÷ cost_report $ (same scope and day) | Exact |
| Effective rate | cost_report $ ÷ usage_report tokens per model and type | Exact, after the 30-day revision window |
| Segment tokens | count_tokens prefix differencing, minus documented overhead | Near-exact (count_tokens is an "estimate") |
| Cache hit rate | cache_read ÷ (uncached + cache_read + cache_write) | Exact |
| Cache miss $ | Σ over misses (per diagnostics or detector) of re-written tokens × (write − read price) | Exact tokens, attributed cause |
| Context tax | Σ over blocks of tokens × turns-resident × read price | Exact with count_tokens, approximate with fallback |
| Cost per outcome | Billed cost ÷ outcomes (PR, ticket, pass) with CI | Exact numerator, outcome-quality dependent |
| Token yield rate | Tokens in outcome-producing runs ÷ all tokens | Depends on outcome labels |
| Tokenizer inflation | count(new model) ÷ count(old model) per content class | Near-exact |
| Hidden-reasoning ratio | (output − visible) ÷ output | Signal only |

---

## 4. Open questions

1. How large is `count_tokens` error in practice ("may differ by a small amount")? This needs an empirical calibration against billed usage per model before claiming "near-exact".
2. Do Bedrock and Vertex usage payloads carry the 5m/1h cache-creation breakdown and iteration arrays with the same fidelity as the first-party API?
3. The Claude Code OTel `api_request` event's full attribute list was truncated in the docs I could fetch. Confirm whether it carries per-request cost and all four token types for exact per-request joins.
4. Anthropic's usage has no separate thinking-token field. Can visible-vs-billed output be estimated reliably enough, via count_tokens on returned summaries, to be useful when `display:"omitted"` is the default?
5. Will FOCUS 1.5's TokenCacheAction and TokenDirection survive review, and will the Recommendations dataset ship (decision was expected around 2026-09-24)?
6. Cross-workspace cache fragmentation: how much could consolidation save, and what does it cost in isolation risk (KeyPooling-type leakage)? This needs org data.
7. Rate-limit capacity (for example, whether cache reads count toward ITPM) wasn't covered here but matters for enterprise sizing.
8. Is there a public, stable source for negotiated enterprise discounts, or must effective rates always be inferred as cost_report ÷ usage_report?

---

## 5. Source index (all opened during this pass)

Anthropic (retrieved 2026-09-23 unless dated):
- Pricing: https://platform.claude.com/docs/en/about-claude/pricing
- Token counting: https://platform.claude.com/docs/en/build-with-claude/token-counting
- Prompt caching: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- Cache diagnostics (beta): https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics
- Usage and Cost API: https://platform.claude.com/docs/en/manage-claude/usage-cost-api
- Claude Code Analytics API: https://platform.claude.com/docs/en/manage-claude/claude-code-analytics-api
- Analytics APIs: https://platform.claude.com/docs/en/manage-claude/analytics-api
- Optimizing for cost and intelligence: https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence
- Thinking: https://platform.claude.com/docs/en/build-with-claude/thinking
- Compaction threshold: https://platform.claude.com/docs/en/build-with-claude/compaction-threshold
- Advisor tool: https://platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool
- Vision: https://platform.claude.com/docs/en/build-with-claude/vision
- What's new in Opus 5: https://platform.claude.com/docs/en/models/opus-5/whats-new-opus-5
- Claude Code costs: https://code.claude.com/docs/en/costs
- Claude Code monitoring: https://code.claude.com/docs/en/monitoring-usage
- Enterprise billing: https://support.claude.com/en/articles/11526368-how-am-i-billed-for-my-enterprise-plan
- Postmortem (2025-09-17): https://www.anthropic.com/engineering/a-postmortem-of-three-recent-issues

Papers:
- CoIn: https://arxiv.org/abs/2505.13778 (2025-05-19)
- Invisible Tokens, Visible Bills: https://arxiv.org/abs/2505.18471 (2025-05-24)
- PALACE: https://arxiv.org/abs/2508.00912 (2025-07-29)
- Token Inflation: https://arxiv.org/abs/2605.30040 (2026-05-28)
- Is Your LLM Overcharging You?: https://arxiv.org/abs/2505.21627 (2025-05-27; v4 2026-05-28; ICML 2026)
- Auditing Model Substitution: https://arxiv.org/abs/2504.04715 (2025-04-07)
- Model Equality Testing: https://arxiv.org/abs/2410.20247 (2024-10-26; ICLR 2025)
- Rank-Based Uniformity Test: https://arxiv.org/abs/2506.06975 (2025-06-08)
- Auditing Prompt Caching: https://arxiv.org/abs/2502.07776 (2025-02-11; ICML 2025)
- KeyPooling: https://arxiv.org/abs/2608.17485 (2026-08-18)
- CacheProbe: https://arxiv.org/abs/2605.30613 (2026-05-28)
- Hosted open-weight service study: https://arxiv.org/abs/2605.02821 (2026-05-04)
- Petrov et al.: https://arxiv.org/abs/2305.15425 (NeurIPS 2023)
- Ahia et al.: https://arxiv.org/abs/2305.13707 (2023-05-23)
- Tokenizer Tax (25 EU languages): https://arxiv.org/abs/2605.24718 (2026-05-23)
- Tokenization Equity Audit: https://arxiv.org/abs/2608.09046 (2026-08-10)
- Cost-of-Pass: https://arxiv.org/abs/2504.13359 (2025-04-17)
- AI Agents That Matter: https://arxiv.org/abs/2407.01502 (2024-07-01)
- HAL: https://arxiv.org/abs/2510.11977 (2025-10-13)
- How Do AI Agents Spend Your Money?: https://arxiv.org/abs/2604.22750 (2026-04-24)
- Tokenomics: https://arxiv.org/abs/2601.14470 (2026-01-20)
- Task specs and token spend: https://arxiv.org/abs/2608.25399 (2026-08-26)
- Total Cost of Agency: https://arxiv.org/abs/2609.23790 (2026-09-20)
- AgentPProf: https://arxiv.org/abs/2609.20301 (2026-09-14)
- Cost-Utility Alignment: https://arxiv.org/abs/2608.26195 (2026-08-25)
- Adding Error Bars to Evals: https://arxiv.org/abs/2411.00640 (2024-11-01)
- Overthinking o1-like LLMs: https://arxiv.org/abs/2412.21187 (2024-12-30)
- Under/Overthinking: https://arxiv.org/abs/2505.00127 (2025-04-30)
- ChatGPT behavior drift: https://arxiv.org/abs/2307.09009 (2023-07)

Standards and industry:
- FOCUS 1.4: https://www.finops.org/insights/introducing-focus-1-4/ (2026-06)
- FOCUS 1.5 scope: https://focus.finops.org/focus-1-5-release-scope/
- Token economics: https://www.finops.org/insights/token-economics-the-atomic-unit-of-ai-value/ (2026-05-10)
- FinOps for AI overview: https://www.finops.org/wg/finops-for-ai-overview/ (2026-02-17)
- FinOps X 2026 keynote: https://www.finops.org/insights/finops-x-2026-day-1-keynote/ (2026-06-09)
- SiliconANGLE on FOCUS: https://siliconangle.com/2026/06/08/ai-token-economics-focus-specification-updates-finopsx/ (2026-06-08)
- OTel GenAI semconv: https://github.com/open-telemetry/semantic-conventions-genai
- OpenAI prompt caching: https://developers.openai.com/api/docs/guides/prompt-caching
- OpenAI reasoning: https://developers.openai.com/api/docs/guides/reasoning
- Gemini thinking: https://ai.google.dev/gemini-api/docs/thinking
- Gemini caching: https://ai.google.dev/gemini-api/docs/caching
- Gemini tokens: https://ai.google.dev/gemini-api/docs/tokens
- Langfuse cost tracking: https://langfuse.com/docs/observability/features/token-and-cost-tracking
- LiteLLM cost tracking: https://docs.litellm.ai/docs/proxy/cost_tracking
- Artificial Analysis methodology: https://artificialanalysis.ai/methodology/intelligence-benchmarking
- Epoch AI price trends: https://epoch.ai/data-insights/llm-inference-price-trends (2025-03-12)
- Simon Willison token counts: https://simonwillison.net/2026/apr/20/claude-token-counts/ (2026-04-20)
- Claude Code Camp tokenizer measurement: https://www.claudecodecamp.com/p/i-measured-claude-4-7-s-new-tokenizer-here-s-what-it-costs-you (2026-04-16)
- VentureBeat tokenizer comparison: https://venturebeat.com/ai/hidden-costs-in-ai-deployment-why-claude-models-may-be-20-30-more-expensive-than-gpt-in-enterprise-settings (2025-05-01)
- Sonar context tax: https://www.sonarsource.com/blog/stop-the-context-tax/ (2026-09-01)
- Cost to a merged feature: https://blog.insight-services-apac.dev/2026/07/06/cost-to-a-merged-feature (2026-07-06)
- Claude Code JSONL undercount: https://gille.ai/en/blog/claude-code-jsonl-logs-undercount-tokens/ (2026-02-24)
- Fin pricing: https://fin.ai/help/en/articles/13975800-fin-pricing-outcomes
- DX framework: https://getdx.com/blog/ai-measurement-framework-guide/ (2026-05-20)
- METR 2025: https://metr.org/blog/2025-07-10-early-2025-ai-experienced-os-dev-study/ (2025-07-10)
- METR 2026: https://metr.org/blog/2026-02-24-uplift-update/ (2026-02-24)
- Kimi vendor verifier: https://github.com/MoonshotAI/K2-Vendor-Verifier
- Anthropic tokenizer approximation: https://github.com/javirandor/anthropic-tokenizer
