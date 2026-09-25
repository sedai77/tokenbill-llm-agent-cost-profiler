# Fact-check: papers-routing-output

Checked by: adversarial fact-checker, 2026-09-23.
Report checked: `papers-routing-output.md` (29 findings).

## How I checked

- **Anthropic cost guide** (optimizing-for-cost-and-intelligence). I downloaded the public page's raw markdown (`.md` URL) and searched it for every number, in addition to two WebFetch passes.
  - The page has no visible last-updated date.
  - Its reference 14 says the support-desk evaluation was reported on 2026-08-08.
  - Its reference 20 dates the Terminal-Bench 3 runs to 2026-08-27/28.
- **Other Anthropic docs**, fetched live: effort, prompt caching, steering thinking, thinking, pricing, fast mode, advisor tool, batch processing, structured outputs, task budgets, usage-cost API, and the Claude Code Analytics API.
- **Claude Code docs**, fetched live: model-config, sub-agents, costs, and monitoring-usage.
- **Bundled Anthropic skill**, used where a claim traces to it: `claude-api` skill v2.1.280, `shared/cost-optimization.md` and `shared/prompt-caching.md`.
- **arXiv papers:** abstract pages via WebFetch, plus the arXiv export API for exact abstract text, plus HTML full text for Self-Route, TALE, "2+3" and Miller.
- **Third-party pages**, fetched live: OpenAI reasoning, flex, and GPT-5 cookbook; the Gemini Batch API; AWS Bedrock IPR doc and product page; Microsoft Foundry model router; GitHub changelog; OpenRouter; Aider; dottxt; Artificial Analysis.
- **Local code:** Token Bill v0.1.2 `tokenbill/pricing.py`, `tokenbill/trace.py` and `tokenbill/instrument.py`, re-read (read-only).

Tally (29 findings): 21 confirmed, 8 corrected, 0 unverifiable, 0 refuted.

---

## Verdicts

### anth-effort-sweep: CORRECTED
- **Fable 5 research work, low effort.** The live cost guide says `low` "gave up 1 to 3 points for **a third to a half off** the cost per task". So `low` costs about 50-67% of the default, not "1/3-1/2 the cost" as the finding says. The finding (and report §0/E2) misread "off" as "of".
- **Fable 5 research work, medium effort.** `medium` "matched the default's accuracy at about 70% to 87% of its cost". Confirmed.
- **Opus 5 on SWE-bench Pro.** About -2 points at `medium` "for half the cost" and about -8 points at `low` "for a quarter of it". Confirmed.
- **Artificial Analysis, Opus 5.5 page.** Cost per task is $0.55 (low), $1.34 (medium), $1.82 (high), $3.46 (xhigh) and $5.98 (max). 5.98 / 0.55 = 10.9x, so "about 11x" is confirmed.
  - The page header says "Released September 2026".
  - The methodology defines cost per task as the weighted-average USD cost to complete one Intelligence Index task.
- **Corrected claim:** On Fable 5 research work, `low` cost 1/2 to 2/3 of the default (a third to a half *off*) for -1 to -3 points. The rest of the finding stands.

### anth-rerun-failures: CONFIRMED
- Verbatim on the live page: "With Claude Opus 5 at `low`, 16% of tasks failed; with those re-run at the default, about 93% passed for about $0.45 each, against 91.7% for $0.93 ... Starting at `medium` instead solved about 94% for about $0.61."
- The bundled skill copy has older figures: $0.70 against $1.39, and $0.95. The report correctly uses the live page.

### thinking-billing-invisible: CONFIRMED
- **Steering thinking, Pricing section.**
  - Thinking tokens are "billed as output tokens".
  - The billed amount is the same for `display: summarized` and `omitted`.
  - Warning: "The billed output token count does **not** match the visible token count".
  - `usage.output_tokens_details.thinking_tokens` exists, and "When streaming, this breakdown appears only on the final `message_delta` event."
- **Thinking page.** Keep-all models are "Claude Opus 4.5 and later Opus models, Claude Sonnet 4.6 and later Sonnet models, Claude Fable 5.1, Claude Mythos 5.1, Claude Fable 5, Claude Mythos 5, and Claude Mythos Preview". Their prior blocks "are billed as input tokens".
- **OpenAI reasoning guide.** Uses `output_tokens_details.reasoning_tokens`, billed as output.

### advisor-iterations-undercount: CONFIRMED
- **Blog (2026-04-09).**
  - Sonnet with an Opus advisor: "+2.7 percentage point ... SWE-bench Multilingual ... reducing cost per agentic task by 11.9%".
  - Haiku with an Opus advisor scored 41.2% against 19.7% solo, and "trails Sonnet solo by 29% in score but costs 85% less per task".
- **Advisor tool doc.** "Top-level `usage` fields reflect executor tokens only. Advisor tokens are not rolled into the top-level totals". Iterations with `type: "advisor_message"` are billed at the advisor model's rates.
- **Cost guide.** The advisor pairing cost "about 2.6 times the cost per task, because the advisor was consulted on nearly every task". The comparison was Opus 5 executor with a Fable 5.1 advisor against Fable 5.1 alone at `medium`. The finding says "every task"; "nearly every" is the exact wording.
- **Code.**
  - `instrument.py` `_usage_dict` reads only the four top-level fields.
  - `trace.py` `_USAGE_FIELDS` has the same four.
  - There is no `iterations` handling anywhere in the package.
  - So the undercount is real.

### pricing-engine-gaps: CONFIRMED
- **Pricing page.**
  - Opus 5.5 is $4 / $20, with cache hits at 0.05x ($0.20).
  - 1h cache writes are 2x.
  - Batch is 50%.
  - Fast mode is $8/$40 (Opus 5.5) and $10/$50 (Opus 5 / 4.8), which is 2x standard.
  - `inference_geo: "us"` is 1.1x on all token categories, for Claude 4.6+.
  - Web search is $10 per 1,000 searches.
  - "Claude 4.7 and later models ... tokenizer produces approximately 30% more tokens for the same text".
- **Claude Code model-config.** Opus 5.5 is the default for Pro, Max, Team, Enterprise and API users.
- **Code.**
  - `pricing.py` `PRICING` has no `claude-opus-5-5` row.
  - The cache-write multiplier is fixed at 1.25 (5-minute writes only).
  - There are no batch, fast, geo, or server-tool fees.
  - An unknown model returns `None`, so tokens are reported without dollars.

### cache-aware-routing: CONFIRMED (with a sourcing note)
- **Prompt-caching table.**
  - Speed: "Switching between `speed: "fast"` and standard speed invalidates system and message caches".
  - Thinking config: "changing it always invalidates message blocks".
  - Effort: "Changing the `output_config.effort` value always invalidates message blocks".
  - Per-message effort in a system message "leaves the cached prefix intact".
- **Structured outputs doc.** "Changing the `output_config.format` parameter will invalidate any prompt cache".
- **Effort doc.** Per-message effort (beta `mid-conversation-output-config-2026-07-01`; Fable 5.1, Mythos 5.1, Opus 5.5, Opus 5) keeps the cache. The alternative, a top-level change, "starts the cache over".
- **Foundry model router** (updated_at 2026-09-10). "By default, model router evaluates each request independently and might select a different underlying model for a later turn ... Session affinity can improve the opportunity for prompt-cache reuse". It has a 30-minute expiry and is best-effort.
- **GitHub changelog** (2026-05-20). "Auto routes along natural cache boundaries to avoid unnecessary cache related costs."
- **Cost guide.** "a 25-token status line at the front of the system prompt cost $4.24 per run instead of $0.59".
- **Sourcing note:** "Caches are per model" is not stated on the public prompt-caching page. It is stated in the bundled skill: `shared/prompt-caching.md` ("caches are model-scoped") and `shared/cost-optimization.md` ("caches are per-model").

### frugalgpt-cascade: CONFIRMED
- arXiv 2305.05176, Chen, Zaharia and Zou, submitted 2023-05-09.
- Abstract: "match the performance of the best individual LLM (e.g. GPT-4) with up to 98% cost reduction or improve the accuracy over GPT-4 by 4% with the same cost."

### routellm: CONFIRMED
- arXiv 2406.18665: Ong et al., v1 2024-06-26, v4 2025-02-23.
- **LMSYS blog.**
  - "cost reductions of over 85% on MT Bench, 45% on MMLU, and 35% on GSM8K" at 95% of GPT-4 performance.
  - 14% of calls went to GPT-4 on MT Bench.
  - The routers were "over 40% cheaper" than Martian and Unify at matched performance.
  - They generalized to Claude 3 Opus and Llama 3 8B without retraining.

### hybrid-automix-mot: CONFIRMED
- **Hybrid LLM** (arXiv 2404.14618, 2024-04-22, ICLR 2024): "up to 40% fewer calls to the large model, with no drop in response quality."
- **AutoMix** (arXiv 2310.12963, v5 2025-01-19, NeurIPS 2024): few-shot self-verification plus a POMDP router, "reducing computational cost by over 50% for comparable performance".
- **MoT cascade** (arXiv 2310.03094, v3 2024-02-08, ICLR 2024): answer consistency with a CoT and PoT mix. The weak model is GPT-3.5-turbo and the strong model is GPT-4, and the cascade needs "only 40% of its cost".

### router-baselines-knn: CONFIRMED
- **LLMRouterBench** (arXiv 2601.07206, 2026-01-12).
  - Scope: "over 400K instances from 21 datasets and 33 models", 10 routing baselines.
  - "several recent approaches, including commercial routers, fail to reliably outperform a simple baseline".
  - The gap to the oracle is "driven primarily by persistent model-recall failures".
- **kNN paper** (arXiv 2505.12601, v2 2026-05-14): "a well-tuned k-Nearest Neighbors (kNN) approach not only matches but often outperforms state-of-the-art learned routers".
- **RouterBench** (arXiv 2403.12031, v2 2024-03-28): "over 405k inference outcomes".
- **Survey** (arXiv 2603.04445): TMLR 2026, v3 2026-08-30.

### production-routers: CONFIRMED
- **AWS product page:** "can reduce costs by up to 30% without compromising on accuracy".
- **AWS doc.**
  - "You must choose exactly two models within the same family".
  - "only optimized for English prompts".
  - "can't adjust routing decisions or responses based on application-specific performance data".
- **Copilot:** a 10% discount on the model multiplier when using auto.
- **OpenRouter.** A classifier with about 30 task types, then "which models the OpenRouter community actually spends on over a trailing 7-day window".
- **Foundry:** "Before you send production traffic to model router, benchmark it against your current model".

### router-attack: CONFIRMED
- arXiv 2501.01818: Shafran, Schuster, Ristenpart and Shmatikov, 2025-01-03.
- The abstract describes "query-independent token sequences" that work "both in white-box and black-box settings against a variety of open-source and commercial routers".
- "confounding queries do not affect the quality of LLM responses".
- "perplexity-based filtering is not an effective defense".

### agent-trajectory-routing: CORRECTED
- **SWE-Router** (arXiv 2607.00053, Son et al., 2026-06-30). Confirmed: "a cheap model run for a few exploratory turns", a Bayes-optimality theorem showing that conditioning on the partial trajectory "never harms routing and is strictly better whenever exploration is informative", "maintaining the majority of the performances of the stronger model", and a released dataset.
- **Triage** (arXiv 2604.07494, Madeyski, 2026-04-08). The abstract says the authors "design an evaluation" and "analytically derived two falsifiable conditions". It presents an evaluation protocol with no reported empirical SWE-bench Lite results. The condition is also narrower than stated: "the light-tier pass rate **on healthy code** must exceed the inter-tier cost ratio", together with a code-health effect-size condition (p̂ ≥ 0.56).
- **AgentRouter** (arXiv 2609.22951, Paul and Nandy, 2026-09-19). Confirmed:
  - 72% cost reduction.
  - 97.3% of frontier-only quality.
  - A 12M-parameter classifier over 4 tiers.
  - Per-step RouteLLM and FrugalGPT reach only 31% and 44%.

  However, the arXiv page says it was accepted at the AgenticUQ Workshop at ICML 2026, so "unreviewed" should read "workshop paper, 4 days old".
- **Efficient Agents** (arXiv 2508.02694, 2025-07-24): 96.7% of OWL, $0.398 to $0.228, 28.4% cost-of-pass improvement. Confirmed.
- **Corrected claim:** Triage is a proposed evaluation protocol. Its break-even rule (light-tier pass rate on healthy code > inter-tier cost ratio) is analytically derived, not measured on SWE-bench Lite. AgentRouter is a very recent workshop paper (ICML 2026 AgenticUQ).

### plan-execute-subagents: CORRECTED
- **Model-config.** `opusplan` uses Opus in plan mode and switches to Sonnet in execution. `CLAUDE_CODE_SUBAGENT_MODEL` is documented.
- **Sub-agents doc.**
  - Per-subagent `model` and `effort` frontmatter exist.
  - Explore "inherits the main conversation's model ... On the Claude API, the inherited model is capped at Opus".
  - Plan inherits the main conversation's model.
- **Costs doc.** "Agent teams use approximately 7x more tokens than standard sessions when teammates run in plan mode".
- **Aider (2024-09-26).** The state-of-the-art 85% came from o1-preview as architect with o1-mini or DeepSeek as editor.
  - **80.5% was Sonnet as architect plus Sonnet as editor, not "Sonnet alone".**
  - Sonnet alone scored 77.4%.
- **Corrected claim:** The architect/editor split reached 85.0% (o1-preview with o1-mini or DeepSeek), against 80.5% for Sonnet+Sonnet and 77.4% for Sonnet alone. The report body (B3) has this right; the finding summary does not.

### orchestrator-shape: CONFIRMED
- Live page: "a Claude Fable 5.1 lead over 25 Claude Sonnet 5 workers, cost about half as much as those settings (47% to 55% less) and scored 10 to 12 points below them, in about 2.3 hours per episode against 15 to 20".
- The corpus is 21.6M tokens.
- "When the work is one dependent chain, or fits in a single context ... In every such case measured, the coordinator's model alone at lower effort came out ahead."

### overthinking: CONFIRMED
- **"2+3" paper** (arXiv 2412.21187v2, 2025-02-01).
  - o1-like models "consumed 1,953% more tokens".
  - "In more than 92% of cases, the initial round of solutions produces the correct answer".
  - On MATH500, QwQ went from 2,407.9 to 1,330.7 tokens (-44.8%), with accuracy 93.0% to 92.8%.
- **Wu et al.** (arXiv 2502.07266, v3 2025-05-27): "inverted U-shaped curve with CoT length".
- **Gema et al.** (arXiv 2507.14417, TMLR, v2 2025-12-15): inverse scaling, including counting tasks with distractors.
- **OptimalThinkingBench** (arXiv 2508.13141, v2 2025-10-04): 33 models, "no model is able to optimally think".
- **Stop Overthinking survey:** TMLR 2025, v4 2025-08-21.

### agent-overthinking: CONFIRMED
- arXiv 2502.08235, Cuadron et al., 2025-02-12.
- Scope: 4,018 trajectories on SWE-bench Verified.
- "higher overthinking scores correlate with decreased performance".
- The three patterns are named as in the finding.
- Selecting the lower-overthinking solution "can improve model performance by almost 30% while reducing computational costs by 43%".

### tale-token-elasticity: CONFIRMED
- TALE (arXiv 2412.18547v1, 2024-12-24): "68.64% reduction in token usage ... with less than 5% decrease".
- Token elasticity: a 10-token budget produced 157 tokens, against 86 at a 50-token budget.
- The Cost-of-Pass abstract: "TALE-EP shows some promise".

### concise-reasoning-prompts: CONFIRMED
- **Chain of Draft** (arXiv 2502.18600, v2 2025-03-03): "matches or surpasses CoT in accuracy while using as little as only 7.6% of the tokens".
- **Concise CoT** (arXiv 2401.05618, v3 2024-10-19).
  - "reduced average response length by 48.70%".
  - "GPT-3.5 with CCoT incurs a performance penalty of 27.69%" on math.
  - "average per-token cost reduction of 22.67%".
- **NoThinking** (arXiv 2504.09858, 2025-04-14): "51.3 vs. 28.9 on ACM [sic: AMC] 23 with 700 tokens", across seven datasets.

### task-budgets: CONFIRMED
- Task-budgets doc.
  - The beta header is `task-budgets-2026-03-13`.
  - Supported: Fable 5.1, Mythos 5.1, Opus 5.5, Opus 5, Fable 5, Mythos 5, Opus 4.8 and Opus 4.7.
  - Not supported: Sonnet 5.
  - "not supported on Claude Code or Cowork surfaces".
  - The minimum is 20,000 tokens.
  - "too small ... can cause refusal-like behavior".
  - A changed value "does not match cache entries created under the old one".
- Live cost guide: "A generous budget cut cost per task 44% for about 3 points ... the tightest allowed budget cut it 58% for 6 points."
- The bundled skill copy has older figures (18% and 47%).

### max-tokens-truncation: CORRECTED
- The live page says a 16,384-token cap "ended 15% of Claude Opus 5's attempts and 43% of Claude Fable 5.1's ... and **only 9 of the 117 capped Fable attempts still passed**." So "none solved" is wrong for the live page. "None of them solved" comes from an older bundled-skill snapshot that described Fable 5, not Fable 5.1.
- Cost per solved task was "about the same as at 64,000 ($21 against $22)".
- At 64K, 2 of about 14,000 turns were cut off. At 128K, Fable 5.1 solved 60.0% "for the same cost per solved task".
- **OpenAI.** You may "incur costs for input and reasoning tokens without receiving a visible response"; the guide advises "reserving at least 25,000 tokens". Confirmed.
- **Corrected claim:** A 16,384 cap ended 15% of Opus 5 and 43% of Fable 5.1 attempts. Only 9 of the 117 capped Fable attempts passed, and cost per solved task did not improve ($21 against $22 at 64K).

### output-shape-verbosity: CONFIRMED
- **Live cost guide.**
  - Chart: "one-line format $0.49 per run, original two-line format $0.57, memo $1.40, all 78% to 85% correct".
  - "The one-line answer used 39% fewer output tokens than the two-line original".
  - Removing "verify twice" cut cost "by a third".
  - Prompts cost 36% more on Opus 5; the audit made them 14% cheaper; accuracy went from 92% to 97%.
- **Effort doc.** "on Claude Opus 5, changing effort does not reliably shorten responses, so prompt for length".
- **OpenAI GPT-5 cookbook:** low/medium/high verbosity gave 560 → 849 → 1288 output tokens.

### structured-outputs: CONFIRMED
- **Anthropic doc.**
  - "guarantee schema-compliant responses through constrained decoding".
  - The additional system prompt means "Your input token count is slightly higher".
  - Format change invalidates the cache.
  - "Compiled grammars are cached for 24 hours from last use".
- **Tam et al.** (arXiv 2408.02442, v3 2024-10-14): "significant decline in LLMs reasoning abilities under format restrictions".
- **dottxt** (author Will Kurt): GSM8K 0.78 vs 0.77, Last Letter 0.77 vs 0.73, Shuffle Object 0.44 vs 0.41.
- Minor: the dottxt page shows no publication date (only an org-mode export stamp of 2026-06-23), so the "(2024)" date could not be confirmed from the page.

### batch-flex-tiers: CORRECTED
- **Anthropic batch doc.**
  - "All usage is charged at 50% of the standard API prices".
  - Caching discounts "can stack".
  - "most batches finishing in less than 1 hour"; expiry at 24h.
  - "cache hit rates ranging from 30% to 98%"; use the 1-hour TTL.
- **Pricing page.** Managed Agents has no batch mode. Confirmed.
- **OpenAI flex:** "Tokens are priced at Batch API rates"; 429 resource-unavailable errors are not charged. Confirmed.
- **Gemini Batch:** "50% of the standard cost", 24h target, context caching supported, last updated 2026-09-17. Confirmed.
- **Wrong detail:** "Batch is single-shot (no tool loop)" overstates it.
  - The batch doc lists "Tool use, including all server tools (web search, web fetch, code execution, MCP connectors, advisor, and tool search)" and "Multi-turn conversations" as batchable.
  - Each batch item is one independent Messages call. A client-side tool loop (like Claude Code's) can't continue inside one batch item, but server-side tool loops do run.
- **Corrected claim:** Batch items are independent single Messages requests. Client-side agent tool loops can't run inside a batch, but server tools (and multi-turn history as input) are supported.

### premium-modifiers: CONFIRMED
- **Fast mode doc.**
  - Pricing: $8/$40 (Opus 5.5) and $10/$50 (Opus 5 / 4.8).
  - "Up to 2.5x higher output tokens per second".
  - "Requests at different speeds do not share cached prefixes".
  - "not available with the Batch API".
- **Pricing page.** The 1.1x rate applies "on all token pricing categories", and Bedrock/Vertex "Regional and multi-region endpoints include a 10% premium over global endpoints".
- **Usage-cost API.**
  - Filtering and grouping cover service tier, data residency (`inference_geo`), and speed (beta).
  - "Priority Tier costs ... are not included in the cost endpoint."

### cost-of-pass-metric: CORRECTED (minor quote fidelity)
- **Cost-of-Pass** (arXiv 2504.13359, v2 2026-02-26). The abstract, via the arXiv export API, confirms:
  - "the expected monetary cost of generating a correct solution";
  - lightweight, large and reasoning models each win on different task types;
  - "the cost roughly halved every few months" for complex quantitative tasks.
- **Misquote.** The finding quotes majority voting and self-refinement as "rarely justify their added costs". The abstract says the paper assessed "common inference-time techniques (majority voting and self-refinement)" and found that "performance-oriented methods with marginal performance gains rarely justify the costs".
- **Adaptive-Consistency** (arXiv 2305.11860, EMNLP 2023): "reduces sample budget by up to 7.9 times with an average accuracy drop of less than 0.1%". Confirmed.
- **Anthropic.** The public page says "Compare on cost per completed task, not per token"; the bundled skill says "API spend is optimized in units of cost per completed task". "two problems carried 43% of the spend" on a 20-problem WideSearch run. Confirmed.
- **Corrected claim:** Use the verbatim wording "performance-oriented methods with marginal performance gains rarely justify the costs" (referring to majority voting and self-refinement). The substance is unchanged.

### eval-statistics: CORRECTED (source attribution)
- **Miller** (arXiv 2411.00640, 2024-11-01, Anthropic affiliation per the HTML). The recommendations are:
  - CLT standard errors;
  - clustered SEs;
  - resampling and next-token probabilities to reduce variance;
  - question-level paired differences;
  - power analysis.

  Confirmed.
- **Zheng et al.** (NeurIPS 2023 D&B): "over 80% agreement"; biases named are "position, verbosity, and self-enhancement". Confirmed.
- **Wrong source.** The public cost guide page does not contain "about 20-30 frozen real requests per lever decision", "about 50 cases x 5 trials" or "never keep or revert on a one-case swing". I checked by searching the raw markdown. These figures are in the bundled Anthropic skill `claude-api/shared/cost-optimization.md` (lines 183 and 186): "around fifty cases and at least five trials per configuration", "~20-30 real requests", "Never keep or revert on a one-case swing".
- **Corrected claim:** Same content, but cite the bundled `claude-api` skill `shared/cost-optimization.md` (v2.1.280), not the public optimizing-for-cost page, for the 20-30 / 50x5 / one-case-swing guidance.

### fleet-telemetry: CONFIRMED
- **Monitoring-usage.** `claude_code.cost.usage` and `claude_code.token.usage` carry `model`, `query_source` (main/subagent/auxiliary), `speed`, `effort`, `agent.name`, `skill.name` and `mcp_server.name`, plus the standard `session.id` and `user.account_uuid`.
- **Analytics API.**
  - Per-user daily `edit_tool.accepted/rejected`, commits, and PRs.
  - A model breakdown with tokens and `estimated_cost`.
  - Up to a 1-hour delay.
- **Costs doc.**
  - "around $13 per developer per active day and $150-250 per developer per month ... below $30 per active day for 90% of users".
  - The `modelPricing` managed setting is documented.

### self-route-rag-vs-lc: CORRECTED
- arXiv 2407.16833v2 (2024-10-17), EMNLP 2024 Industry.
- **Confirmed:**
  - Token share relative to LC: 38.39% on Gemini-1.5-Pro and 61.40% on GPT-4o.
  - "the cost is reduced by 65% for Gemini-1.5-Pro and 39% for GPT-4O".
  - Gemini lost 2.2%.
- **GPT-4o sign conflict.** The paper's own text says "there is a slight performance drop for GPT-4O (-0.2%)". Its table shows Self-Route 48.89 against LC 48.67 (+0.22 points). The finding's "+0.2%" matches the table but contradicts the authors' stated figure. Either way it is about zero.
- A third model, GPT-3.5-Turbo, gained 1.7% using 38.85% of the tokens.
- **Corrected claim:** GPT-4o used 61.40% of LC tokens for a 39% cost cut at essentially unchanged score. The paper text reports -0.2%; the table implies +0.2 points.

---

## Cross-cutting notes for the report author
1. **Two stale-snapshot traps.** The bundled skill (v2.1.280) has older numbers than the live cost guide:
   - re-run cost: $0.70 / $1.39 in the skill, against $0.45 / $0.93 live;
   - task budgets: 18% / 47% in the skill, against 44% / 58% live;
   - max_tokens: "none solved" in the skill, against "9 of 117 passed" live.

   The report mostly used the live page but carried "none solved" over from the skill.
2. **Local code claims are accurate.**
   - Token Bill v0.1.2 reads only four usage fields.
   - It has no `iterations`, `thinking_tokens`, `speed`, `service_tier` or `inference_geo` handling.
   - It has no `claude-opus-5-5` price row.
   - It prices every cache write at 1.25x.
