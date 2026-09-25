# Verification: papers-measurement track

Checked 2026-09-23 by an adversarial fact-checker. I opened every source listed below myself during this pass:
- Anthropic docs pages: pulled as markdown with `curl https://platform.claude.com/docs/en/<path>.md` and `https://code.claude.com/docs/en/<page>.md`, then grepped for the exact claims.
- arXiv papers: abstract pages via `export.arxiv.org`. For the Token Inflation, Total Cost of Agency and Cai et al. papers I also read the PDFs with `pdftotext`, because their numbers needed context.
- Blogs and other web pages: WebFetch, backed by curl plus grep where a number mattered.
- Repo claims: re-checked against `tokenbill/pricing.py` and `tokenbill/trace.py` (v0.1.2).

Local evidence is in `research/verify_tmp/pm/`: fetched docs (`*.md`), arXiv abstracts (`abs_*.html`), and paper and blog text (`tca.txt`, `ti.txt`, `cai.txt`, `insight.txt`, `k2.md`, `otel_gen-ai-spans.md`).

Verdict counts (28 findings): **20 confirmed, 8 corrected, 0 unverifiable, 0 refuted.**

| id | verdict | one-line reason |
|---|---|---|
| anth-usage-schema-exactness | confirmed | Every rate and multiplier matches pricing.md; repo gaps confirmed in `pricing.py` and `trace.py` |
| anth-iterations-undercount | confirmed | Compaction and advisor usage statements match the docs word for word |
| anth-hidden-overhead-tokens | confirmed | All overhead numbers match the pricing tables; the unbilled system-token note is on the token-counting page |
| anth-admin-api-reconciliation | confirmed | Buckets, dimensions, 5-minute latency, cents strings, Priority exclusion, CC Analytics fields and 30-day revision all verbatim |
| claude-code-enterprise-baselines | confirmed | $13/day, $150–250/month, 90% under $30, 7x, <$0.04, API-rate billing and `modelPricing` all verbatim |
| tokenizer-inflation-47plus | confirmed | Anthropic ~30% note; Willison 1.46x; Ray 1.325x, 1.445x, 1.212x, 1.01–1.47x, 4.33→3.60 |
| chars-heuristic-error | confirmed | The −27%/+17% arithmetic holds; VentureBeat 16/21/30%; Langfuse quote |
| count-tokens-exact-attribution | **corrected** | TCA's 27.6% at depth 6 is a share of **cost** (mid tier), not of tokens. The ~12% is an estimate (11.8%) and the study is uncached |
| images-thinking-estimation | confirmed | 28x28 formula, 1,568/4,784 caps, ~3x, $23.92 per 1,000, omitted-thinking billing and cache invalidation all verbatim |
| opus5-thinking-effort-rebaseline | **corrected** | The numbers hold, but the cited whats-new-opus-5 URL now redirects to the Opus 5 overview, which has neither statement. They are on the Opus 5.5 pages |
| hidden-token-audit-research | confirmed | Abstracts match: 3-layer framework; CoIn 94.7%; PALACE estimates from prompt-answer pairs |
| audit-evasion-limits | confirmed | 1,469% (CoIn setting, mean), 247.16% (PALACE), 50.85% (martingale); ICML 2026 oral, v4 2026-05-28 |
| served-model-substitution | **corrected** | In Cai et al., only logprob tests fail because of nondeterminism; classifiers and benchmarks fail on subtle substitution. The 16% was the worst **hour** |
| cache-isolation-gateways | confirmed | Workspace vs org isolation verbatim; Gu 7 providers; KeyPooling 5/5 and 1.7–2.5%; CacheProbe exists (SAGAI '26) |
| multilingual-tokenization | confirmed | 15x; 1.2 / 2.2–2.5 / ~3.1 tokens per word; Ukrainian +15–18%; 1.56x and 2.37x; Claude figures from Ray |
| cache-health-benchmarks-diagnostics | **corrected** | The live page says caching cut cost **2.7x to 5.3x** at 79–90% cache reads. The 2.5–3.7x figure comes from the older bundled skill doc |
| context-tax-residency | confirmed | Every Sonar number matches; Bai 1000x; Tokenomics 53.9% |
| cost-per-completed-task | confirmed | Cost-of-Pass definition and "halved every few months"; $0.54 vs $0.84 at 5x per token; $183 → $63 → $28 |
| variance-tails-statistics | **corrected** | The numbers hold, but two sub-claims come from Anthropic sources the finding does not cite. The 50x5 figure is a recommended bar, not a practice |
| cost-per-merged-pr | **corrected** | Merged code **passed** as few as 12 of 38 tests (failed up to 26), not "failed 12–38 of 38" |
| per-token-price-misleading | **corrected** | The AA page does not break costs out into reasoning, answer and cache-write tokens. Epoch's caveat is specific to reasoning models |
| focus-15-ai-columns | confirmed | 1.4 ratified June 4 with no AI columns; 1.5 scope statuses match; Recommendations decision due Sept 24 |
| finops-token-yield | confirmed | Token-yield definition, cost per outcome, SaaS meter quote and the KPI list are all verbatim |
| otel-genai-normalization | confirmed | `input_tokens` "SHOULD include all types of input tokens, including cached tokens"; Langfuse subtracts; LiteLLM has a bill-mismatch workflow |
| claude-code-telemetry-jsonl | confirmed | OTel attributes and "approximations" quote verbatim; gille.ai 75%, 100–174x, 10–17x, 51–55%; ccusage 130,785 vs 648,562 |
| multi-provider-normalization | confirmed | OpenAI 1.25x, 0.1x, 30m and field names; Gemini full-thought billing, field names, 2,048/4,096 minimums, 258 tokens |
| semantic-agent-profiling | confirmed | AgentPProf 0.764 B³ F1 and up to 56% MAP; Cost-Utility paper's five misalignment classes (the paper gives no numbers) |
| hosted-service-variance | **corrected** | The K2VV schema-accuracy floor is ~72%, and several third parties hit 100%. The "50%" is a trigger-similarity score |

---

## Per-finding notes

### anth-usage-schema-exactness: confirmed
- **Rate card** (pricing.md):
  - 5m writes cost 1.25x and 1h writes 2x ("1-hour cache write | 2x base input price").
  - Cache reads cost 0.1x, "0.025x on Claude Fable 5.1 and Claude Mythos 5.1; 0.05x on Claude Opus 5.5".
  - Opus 5.5 row: $4 input, $5 5m write, $8 1h write, $0.20 read, $20 output.
- **Modifiers** (pricing.md):
  - Batch API: "a 50% discount on both input and output tokens".
  - `inference_geo: "us"` applies "a 1.1x pricing multiplier" for Claude 4.6 and later.
  - Fast mode: Opus 5.5 $8/$40; "Claude Opus 5 / Claude Opus 4.8 | $10 / MTok | $50 / MTok".
  - "Regional and multi-region endpoints include a 10% premium over global endpoints" (Bedrock and Google Cloud, Claude 4.5+).
- **Server tools and runtime** (pricing.md):
  - Web search: "$10 per 1,000 searches".
  - Code execution: "1,550 free hours ... $0.05 USD per hour, per container". Caveat: it is free when `web_search_20260209` or `web_fetch_20260209` is in the request.
  - Managed Agents: "Session runtime | $0.08 per session-hour".
- **Repo** (v0.1.2):
  - The `Usage` dataclass has exactly four fields.
  - `ModelPricing.cache_write_multiplier = 1.25` is applied to all `cache_creation_input_tokens`.
  - The `PRICING` dict has no `claude-opus-5-5` row.
  - Grep finds no `ephemeral`, `iterations`, `server_tool_use`, `inference_geo`, `service_tier` or `speed` handling.

### anth-iterations-undercount: confirmed
- **compaction-threshold.md**: "The top-level `input_tokens` and `output_tokens` do not include compaction iteration usage ... To calculate total tokens consumed and billed for a request, sum across all entries in the `usage.iterations` array."
- **advisor-tool.md**:
  - "Top-level `usage` fields reflect executor tokens only ... Iterations with `type: "advisor_message"` are billed at the advisor model's rates".
  - "The top-level `max_tokens` applies to executor output only. It does not bound advisor sub-inference tokens."

### anth-hidden-overhead-tokens: confirmed
- **Tool-use system prompt table** (pricing.md): Opus 5.5 is 286 (auto/none; the any/tool cell is blank), Sonnet 5 is 354/474 and Opus 4.7 is 675/804.
- **Bash**: 325 tokens (Opus 5, 4.8, 4.7) and 244 tokens (Opus 4.6, Sonnet 4.6 and earlier).
- **Text editor**: `text_editor_20250429` adds 700 tokens.
- **Toolsets**:
  - `computer_toolset_20260801` adds "about 4,500 input tokens".
  - `browser_toolset_20260801` adds "about 6,600".
  - Nuance: both toolset figures already *include* the tool-use system prompt.
- **token-counting.md**: "Token counts may include tokens added automatically by Anthropic for system optimizations. **You are not billed for system-added tokens**."

### anth-admin-api-reconciliation: confirmed
- **Usage API** (usage-cost-api.md):
  - `1m`/`1h`/`1d` buckets with maxima of 1,440, 168 and 31.
  - "Filter by API key, workspace, model, service tier, context window, data residency, or speed (beta), and group results by these dimensions".
  - "typically appears within 5 minutes".
- **Cost API** (usage-cost-api.md):
  - "decimal strings in lowest units (cents)" and "Daily granularity only".
  - "Priority Tier costs ... are not included in the cost endpoint".
  - Code execution appears in the cost endpoint only.
- **Claude Code Analytics** (claude-code-analytics-api.md):
  - Per-user daily sessions, lines, commits, PRs, Edit/MultiEdit/Write/NotebookEdit accept and reject counts, and `estimated_cost` in cents.
  - "up to 1-hour delay".
  - "only tracks Claude Code usage on the Claude API", not Bedrock, Foundry or Google Cloud.
- **Enterprise Analytics** (analytics-api.md):
  - Covers "per-user and organization-level token usage and cost".
  - "Values for a given date can be revised for up to 30 days ... For invoicing-grade totals, query dates at least 30 days in the past."
  - Default limit: 60 RPM.
  - The admin analytics reference lists `chat`, `claude_code` and `cowork` among the product values.

### claude-code-enterprise-baselines: confirmed
- **code.claude.com/docs/en/costs**:
  - "average cost is around $13 per developer per active day and $150-250 per developer per month, with costs remaining below $30 per active day for 90% of users".
  - "Agent teams use approximately 7x more tokens than standard sessions when teammates run in plan mode".
  - "typically under $0.04 per session".
  - "computes the dollar figure locally from token counts at list price, unless a `modelPricing` table is in effect ... The figure is an estimate".
- **Support article 11526368** ("Updated over 3 weeks ago"): "Every token your team consumes is billed separately at standard API rates" on top of the seat fee. It also covers 1.1x for US-only inference.

### tokenizer-inflation-47plus: confirmed
- **Anthropic**:
  - pricing.md: "This tokenizer produces approximately 30% more tokens for the same text."
  - token-counting.md: Fable and Mythos 5 and 5.1 share the Opus 4.7 tokenizer. "don't reuse token counts measured on the older model".
- **Willison (2026-04-20)**: system prompt 1.46x, 30-page PDF 1.08x, image 3.01x (due to high resolution).
- **Ray, Claude Code Camp (2026-04-16)**:
  - 1.325x weighted average; CLAUDE.md 1.445x; code diff 1.212x.
  - Japanese and Chinese 1.01x; English technical docs 1.47x.
  - English chars per token 4.33 → 3.60; TypeScript 3.66 → 2.69.

### chars-heuristic-error: confirmed
- **Arithmetic**: 2.69/3.7 − 1 = −27.3% and 4.33/3.7 − 1 = +17.0%, so the range is correct.
- **VentureBeat (2025-05-01, guest contributor)**: Claude 3.5 Sonnet vs GPT-4o produced "approximately 16% more tokens" (English), "21%" (math) and "30% more tokens" (Python).
- **Langfuse**: "According to Anthropic, their tokenizer is not accurate for Claude 3 models."
- **Bundled token-counting.md** also says tiktoken "undercounts Claude tokens by ~15-20%".
- "No official Claude 3+ tokenizer" is consistent with all of these sources. I found no Anthropic release of one.

### count-tokens-exact-attribution: corrected
- **count_tokens facts all check out** (token-counting.md):
  - GA on Claude API, Claude Platform on AWS, Bedrock, Google Cloud and Foundry.
  - Free, with 5,000 / 10,000 / 20,000 RPM, "separate and independent" from message limits.
  - The count is an "estimate", may include system-added tokens, and "without using caching logic".
  - Server tools (except advisor), the MCP connector and url/file sources are rejected.
- **TCA (arXiv 2609.23790, v1 2026-09-20, Singh, Priyam and Bhowmick)**:
  - "two-pass, non-billable token count" and "about 12 percent of the full billed cost" check out. The body says 11.8% is an estimate; 13.6% is the share of optimizer-controllable variable cost.
  - The 27.6% is **injection as a share of cost at depth six on the mid tier** (PDF: "The share rises monotonically from 8.4 percent at depth two to 27.6 percent at depth six on the mid tier"). It is not a share of tokens.
  - The two counts use "the provider's own tokenizer". The paper does not name the Anthropic `count_tokens` endpoint.
  - "Prompt caching is not evaluated; all figures are for the uncached case."
- **Corrected claim**: TCA measures memory injection at ~12% (11.8% est.) of billed cost, rising to 27.6% **of cost** at depth 6 (mid tier), uncached.

### images-thinking-estimation: confirmed
- **vision.md**:
  - "costs `⌈width / 28⌉ × ⌈height / 28⌉` visual tokens"; the high-resolution tier (Claude 4.7+) caps at 4,784 and standard at 1,568.
  - "roughly three times more visual tokens".
  - "At Claude Opus 5's $5 ... the 4K image about $23.92 USD per thousand".
- **thinking.md**:
  - "billed as output tokens, even when the thinking text isn't returned".
  - "`"omitted"` ... returns thinking blocks with an empty `thinking` field".
  - "The billed output token count does not match the count of tokens you see".
  - Changing thinking or effort "invalidate[s] cache breakpoints".

### opus5-thinking-effort-rebaseline: corrected
- **Numbers verified** on optimizing-for-cost-and-intelligence.md:
  - SWE-bench Pro: "Claude Opus 5 gave up about 2 points at `medium` for half the cost and about 8 points at `low` for a quarter of it".
  - 16,384 cap: it "ended 15% of Claude Opus 5's attempts and 43% of Claude Fable 5.1's ... cost per solved task was about the same as at 64,000 ($21 against $22)". This was on an *internal repository-task benchmark*, not SWE-bench Pro.
- **Source problem**: `https://platform.claude.com/docs/en/models/opus-5/whats-new-opus-5` returns HTTP 307 to `/docs/en/models/opus-5/overview`. That page contains neither a thinking-on-by-default statement nor "re-baseline".
- **Where the supporting text actually lives**:
  - `models/opus-5-5/whats-new-opus-5-5`: "On Claude Opus 5, thinking is on by default".
  - `models/opus-5-5/migration-guide`: "Re-baseline cost and latency at your chosen effort level". For Opus 4.8 or earlier: "Thinking tokens are billed as output tokens, so these workloads can produce more output tokens per request".
  - thinking.md: billing as output.
- **Corrected claim**: same substance, but cite the Opus 5.5 what's-new page and migration guide, not the dead Opus 5 URL.

### hidden-token-audit-research: confirmed
- 2505.18471 (v1 2025-05-24): "quantity inflation", "quality downgrade", and a "modular three-layer auditing framework ... execution, secure logging, and user-facing auditability".
- 2505.13778 (v1 2025-05-19): "verifiable hash tree from token embedding fingerprints ... success rate reaching up to 94.7%".
- 2508.00912 (v1 2025-07-29): PALACE "estimates hidden reasoning token counts from prompt-answer pairs".

### audit-evasion-limits: confirmed
- **2605.30040** (v1 2026-05-28, Hoque et al.):
  - "inflate reported token counts by up to 1,469% on average in the CoIn setting, by 247% in the PALACE setting ... and by 50.85% even when the user can see the full reasoning string". That last figure is against the martingale auditor of Velasco et al.
  - Body: "$100 honest reasoning bill into roughly a $1,569 bill"; PALACE "mean inflation rate of 247.16%" on triggered samples.
  - Remedy: "trusted execution attestation, cryptographic proofs of inference, or third-party re-execution".
- **2505.21627**: "Selected as an oral presentation at ICML 2026"; v4 is dated 2026-05-28. It argues pricing "must price tokens linearly on their character count".

### served-model-substitution: corrected
- **2504.04715** (v1 2025-04-07, v2 2025-09-29; Cai, Shi, Zhao and Song, UC Berkeley):
  - "statistical tests on text outputs are query-intensive and fail against subtle substitutions, while methods using log probabilities are defeated by inherent inference nondeterminism".
  - The PDF shows text classifiers at near-chance (~50%) on quantized vs original models. Benchmark detection has limited power and can be evaded by routing.
  - TEEs are proposed as the robust fix.
  - So nondeterminism specifically defeats the **logprob** audits; the other methods fail for other reasons.
- **2410.20247** (ICLR 2025): "median of 77.4% power ... average of just 10 samples per prompt"; "11 out of 31 endpoints".
- **2506.06975**: exists (v1 2025-06-08, v5 2026-04-09).
- **Anthropic postmortem (Sep 17, 2025)**: "At the worst impacted hour on August 31, 16% of Sonnet 4 requests were affected"; "The evaluations we ran simply didn't capture the degradation". The report body says "worst day", but it was the worst *hour*.

### cache-isolation-gateways: confirmed
- **prompt-caching.md**: "Caches are isolated per workspace ... Claude API, Claude Platform on AWS, and Microsoft Foundry; Bedrock and Google Cloud maintain organization-level cache isolation."
- **Gu et al. (2502.07776, ICML 2025)**: "global cache sharing across users in seven API providers, including OpenAI".
- **KeyPooling (2608.17485, v1 2026-08-18)**:
  - "none bound customers to upstream credentials by default; under a shared credential, all five exposed cross-customer cache reads for both providers".
  - "12 of 28 labels carrying 33.7% of volume".
  - "1.7-2.5% cost increase".
- **CacheProbe (2605.30613)**: a SAGAI '26 OpenRouter audit.

### multilingual-tokenization: confirmed
- Petrov et al.: "differences up to 15 times".
- Ahia et al.: overcharged speakers across 22 languages.
- **Tokenizer Tax (2605.24718)**:
  - "English (1.2 tokens/word) to Greek/Maltese (~3.1)"; "Slavic (2.2-2.5)"; "Ukrainian (2.7) pays 15-18% more"; "rho > 0.97".
  - Caveat: this measures 10 foundation-model tokenizers, and no Claude tokenizer is shown.
- **2608.09046**: Bengali 1.56x, Yoruba 2.37x on GPT-4o o200k; "128k ... 82k".
- The Claude 4.7+ figures (1.21x for a code diff up to 1.47x for English technical docs, 1.01x CJK) come from Ray's single practitioner measurement.

### cache-health-benchmarks-diagnostics: corrected
- **Checked and correct** (optimizing-for-cost-and-intelligence.md):
  - "a median 84% ... top 10% ... 94% or more"; "Below about 80%, look for something breaking the cache".
  - "under 1% of its input".
- **What differs**: the live page, fetched today, says the caching chart's runs "read 79% to 90% of their input tokens from the cache". Its summary table says "Cost cut by a factor of 2.7 to 5.3 on agent loops; 83% on the triage run". The chart shows Fable 5.1 $37.94 → $7.12 and Sonnet 5 $3.20 → $1.20.
- The "2.5 to 3.7 at 81% to 90%" figure is from the older bundled claude-api skill doc (v2.1.280), as `competitors.md` in this workspace also notes.
- **Cache diagnostics**:
  - cache-diagnostics.md: beta `cache-diagnosis-2026-04-07`, Claude API only (all other platforms "not available").
  - Types: model_changed, system_changed, tools_changed, messages_changed, previous_message_not_found, unavailable. `cache_missed_input_tokens` is "a magnitude indicator rather than a billing number".
  - `unavailable` covers `tool_choice`, `thinking`, `context_management`, `output_config`, `output_format` and beta-header changes.
- **Claude Code** (costs.md): "more than 5% and at least 2,000 tokens"; the "Prompt cache (main)" stats need v2.1.251+.
- **Corrected claim**: caching cut agent-loop cost 2.7x to 5.3x at 79–90% cache reads (83% on the triage run).

### context-tax-residency: confirmed
- **Sonar (2026-09-01, Antonio Aversa)**:
  - ~800 lines, 512 round-trips, 106k fresh input, 3.1M cache write, 152.8M cache read, 289k output, ~$41, peak 458.7k.
  - The 618-line file needed ~67 lines and cost ~2.7M cache-read tokens (~$0.54) over ~470 turns.
  - 18 PRs: ~234M context tokens, ~700 round-trips, ~$65 average and ~$52 median.
  - The article does not name the model or agent.
- **Bai et al. (2604.22750)**: "1000x more tokens than code reasoning and code chat, with input tokens ... driving the overall cost".
- **Tokenomics (2601.14470)**: input 53.9% and Code Review 59.4% (ChatDev with GPT-5, 30 tasks).

### cost-per-completed-task: confirmed
- **2504.13359** (v1 2025-04-17, v2 2026-02-26): "the expected monetary cost of generating a correct solution"; frontier cost-of-pass across models and human experts; "cost roughly halved every few months" for complex quantitative tasks.
- **2407.01502**: calls for joint cost-accuracy optimization.
- **optimizing-for-cost-and-intelligence.md**:
  - "Claude Fable 5.1 at `low` effort solved 88.6% of tasks for $0.54 per solved task, against 77.4% for $0.84 from Claude Sonnet 5 ... despite a per-token price five times higher".
  - "cost per solved task falls from $183 to $63 to $28" (Terminal-Bench 3, Opus 4.7 → 4.8 → 5).

### variance-tails-statistics: corrected
- **Verified numbers**:
  - Bai et al.: "up to 30x", "up to 0.39", "accuracy often peaks at intermediate cost".
  - HAL (2510.11977): "21,730 agent rollouts ... about $40,000"; "higher reasoning effort reducing accuracy in the majority of runs"; 2.5B tokens.
  - Miller (2411.00640): statistical recommendations for evals.
  - Smékal (2608.25399): Kimi K3, 2,700 runs, +29.7%, 13–115%, "within 36%".
- **Source gaps**:
  - "Two of 20 tasks carried 43% of spend" is from the Anthropic optimizing page (WideSearch run), which the finding does not cite.
  - "~50 cases x 5 trials" is from the bundled claude-api `cost-optimization.md`: "The published bar - around fifty cases and at least five trials per configuration - is the standard for the production cutover". That is Anthropic's *recommended bar*, not a statement of what Anthropic does, and it is not in the cited sources.
- **Corrected claim**: same numbers. Attribute the 43% to the Anthropic optimizing page, and state the 50x5 as Anthropic's recommended production-cutover bar (claude-api skill cost guidance).

### cost-per-merged-pr: corrected
- **"Token Price Is the Wrong Number"** (Veli-Matti Vanamo, 2026-07-06):
  - 23 model-and-harness arms and 29 runs on an RFC 8628 device flow.
  - The table's totals range from $2.59 (GPT-5.4-mini on Copilot, run 2) to $33.38 (Fable 5, Claude Code). The text says "about $3 to about $33".
  - "The harness alone moved the cost by roughly 2.5×".
  - MAI-Code-1-Flash "costs $2.81 to merge, with upwards of 97% of that being the gate".
- **The error**: the post says the gate merged "code that passed as few as 12 of 38 of the team's own unit tests" (the complex feature scored Opus 30/38, GPT-5.5 18/38, Sonnet 16/38). The finding's "failed 12-38 of 38 tests" is wrong. Merged code **passed** as few as 12 of 38, so it failed up to 26 of 38.
- **Other sources, all confirmed**:
  - Fin: "$0.99 per outcome"; resolution = "customer confirms Fin resolved the issue or does not request more help".
  - DX (2026-05-20, Taylor Bruneaux): "AI spend (both total and per developer)", "Net time gain per developer (time savings minus AI spend)", "Agent hourly rate".
  - METR (2026-02-24): "Some developers self-report very high speedups, though as we documented in our earlier study those estimates can be quite unreliable".

### per-token-price-misleading: corrected
- **Epoch (2025-03-12)**:
  - "ranging from 9x to 900x per year" (median 50x).
  - Its token-efficiency caveat is narrower than the finding says. It excluded reasoning models because they "generate a much larger number of tokens ... misleading to compare reasoning models to other models on price per token". It also reports that evaluation costs "declined similarly to prices per token" for the non-reasoning models it measured.
- **Artificial Analysis methodology** (fetched today):
  - "we use the token counts reported by each model's API provider where available"; "canonical-tokenizer fallback".
  - "we combine these token counts with live measurements of the model's typical cache hit rate".
  - Changelog: "including cache hit rates and cache token pricing".
  - The page does **not** break costs out into reasoning, answer and cache-write tokens. That breakdown could not be verified.
- **Bai et al.**: "Kimi-K2 and Claude-Sonnet-4.5, on average, consume over 1.5 million more tokens than GPT-5".
- **Corrected claim**: Artificial Analysis prices from provider-reported counts (canonical-tokenizer fallback), combined with live cache-hit rates and cache token pricing. Epoch's caveat applies specifically to reasoning models.

### focus-15-ai-columns: confirmed
- **FinOps "Introducing FOCUS 1.4"** (published 2026-06-10): ratified June 4, 2026. It adds Invoice Detail and Billing Period datasets and no AI or token columns. "FOCUS 1.5 is scoped to surface AI model identity and token consumption".
- **focus.finops.org 1.5 scope**:
  - ModelDeveloper, ModelFamily, ModelId and ModelVersion are merged.
  - PrincipalId was merged on Sept 10.
  - TokenCacheAction and TokenDirection are in Member review.
  - Worked examples cover per-token, first-party-via-cloud and marketplace scenarios.
  - The Recommendations dataset is a stretch goal, decided by Sept 24.
  - "Ratified Dec 3".
- **SiliconANGLE (2026-06-08, Jonathan Anthony)** does not itself claim that 1.4 has token columns. That is harmless, because the finding only says "blog claims" exist.

### finops-token-yield: confirmed
- **Token Economics (J.R. Storment, 2026-05-10)**:
  - "Token yield rate. The share of generated tokens that contributed to a downstream business action, after accounting for retries, abandoned sessions, and outputs that failed quality review."
  - "cost per outcome, traced through to cost per inference ...".
  - "the token meter is not exposed to the buyer".
- **FinOps for AI** ("Last updated: February 17, 2026"): Cost Per Inference, Token Consumption Metrics (formula "Cost Per Token = Total Cost / Number of Tokens Used"), Anomaly Detection Rate, ROI, and Cost per API Call.

### otel-genai-normalization: confirmed
- **github.com/open-telemetry/semantic-conventions-genai** (exists; `docs/gen-ai/gen-ai-spans.md`, Status: Development):
  - `gen_ai.usage.input_tokens`: "This value SHOULD include all types of input tokens, including cached tokens."
  - `cache_read.input_tokens` and `cache_write.input_tokens` "SHOULD be included in `gen_ai.usage.input_tokens`"; `reasoning.output_tokens` is included in output tokens.
  - `anthropic` is listed as a provider value.
- **Langfuse**: "Inclusive counts must be converted into exclusive buckets"; "Cache reads and writes are subtracted from `input`".
- **LiteLLM**: tracks spend by key, user, team, tag and end user from `model_prices_and_context_window.json`, and has a section titled "Cost does not match your provider bill?".

### claude-code-telemetry-jsonl: confirmed
- **monitoring-usage.md**:
  - `claude_code.cost.usage` and `claude_code.token.usage`.
  - `query_source` is one of "main", "subagent" or "auxiliary".
  - Attributes include effort, speed, agent.name, skill.name, plugin.name and mcp_server.
  - Counters cover PRs, commits and code_edit_tool decisions. Events carry `prompt.id`. There are VCS attributes and `llm_request` spans.
  - "Cost metrics are approximations."
- **gille.ai (Magnus Gille, 2026-02-24)**: "75% of all JSONL entries have `usage.input_tokens` of 0 or 1"; 100–174x (input) and 10–17x (output); "51–55% of all entries were duplicates".
- **ccusage #888 (opened 2026-03-11)**: first-seen gives 130,785 output tokens vs latest 648,562; 551 of 606 duplicate groups had differing output counts.

### multi-provider-normalization: confirmed
- **OpenAI prompt caching**:
  - "enabled by default"; "Cache writes cost 1.25×"; reads "0.1×"; TTL "`30m`, is also the default".
  - Fields: `usage.input_tokens_details.cached_tokens` and `usage.input_tokens_details.cache_write_tokens`.
- **OpenAI reasoning**: reasoning tokens are "not visible via the API ... billed as output tokens", reported in `output_tokens_details.reasoning_tokens`.
- **Gemini**:
  - Thinking: "Pricing is based on the full thought tokens ... despite only the summary being output"; field `total_thought_tokens`.
  - Caching: "Implicit caching is enabled by default for all Gemini 2.5 and newer models". Minimums are 2,048 (2.5 Flash and Pro) and 4,096 (3.x). Field `usage.total_cached_tokens`.
  - Tokens doc: "Images ≤384 pixels in both dimensions count as 258 tokens". This doc is cited in the report but missing from this finding's source list.

### semantic-agent-profiling: confirmed
- **AgentPProf (2609.20301, v1 2026-09-14)**: "semantic operation stack", "recursive operation segmentation", "0.764 B³ F1 ... on CodeTraceBench", "raises MAP by up to 56%".
- **Cost-Utility Alignment (2608.26195, v1 2026-08-25)**: five misalignment forms, "cognitive and context use, external interaction, recovery-loop control, resource-capability allocation, and multi-agent coordination". The abstract gives no numbers.

### hosted-service-variance: corrected
- **2605.02821 (v1 2026-05-04)**: "listed prices are more anchored than latency, throughput, context length, protocol support, and error semantics"; "routing lowers Qwen3-32B cost by 37.8%".
- **Claude regional endpoints**: pricing.md, "Regional and multi-region endpoints include a 10% premium over global endpoints".
- **K2 Vendor Verifier** (README tables, parsed):
  - Setup: 4,000 requests; the official Moonshot API is at 100% schema accuracy.
  - The lowest third-party schema accuracy is 71.96% (Together, K2-0905). Others: AtlasCloud 72.44%, Baseten 72.49%, Volc 72.86%, SGLang 73.13%, vLLM 76.00%; on K2-thinking, Chutes 83.05% and Together 84.63%.
  - Several third parties reach 100% (Fireworks, DeepInfra, NovitaAI, Groq and Infinigence on K2-0905; Fireworks on K2-thinking).
  - The "50%" in the finding is Nebius's **ToolCall-Trigger Similarity** (50.60%). Its schema accuracy is 84.47%.
- **Corrected claim**: some third-party vendors scored ~72–87% tool-call schema accuracy vs 100% on the official API, while others matched 100%.

---

## Cross-cutting notes for the synthesizer
- The Anthropic docs moved during September 2026. Two URLs in the report (`models/opus-5/whats-new-opus-5`, and the Opus 4.7 what's-new page) now redirect to other pages. Cite the Opus 5.5 what's-new page and migration guide instead.
- Where the report quotes the bundled claude-api skill (v2.1.280), check the live page first. The caching factor (2.5–3.7x vs live 2.7–5.3x) is one known case of drift.
- The TCA paper's attribution method and figures come from an uncached, two-tier benchmark (Haiku and Sonnet). Don't generalize its percentages to cached agent loops.
