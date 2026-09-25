# Model routing, cascades, and output/reasoning-token reduction: research for Token Bill

**Track:** papers-routing-output
**Prepared:** 2026-09-23
**Scope:** model routing and cascades (academic and production), reasoning-token efficiency (overthinking, token budgets, concise reasoning, effort/adaptive thinking), output-length control, structured outputs, batch vs realtime tiers, and the telemetry and evals needed to recommend a routing or effort change safely.
**Method:** Primary sources only where possible: arXiv abstract/HTML pages, official vendor docs (Anthropic, OpenAI, Google, AWS, Microsoft, GitHub), and Anthropic's bundled `claude-api` skill references on this machine (treated as authoritative for Anthropic API behavior; the public doc URL is cited next to it). Every source below was opened during this pass. Vendor doc pages without a visible date are marked "accessed 2026-09-23".

**Evidence legend.** *strong*: peer-reviewed paper or first-party vendor documentation of its own billing/behavior. *moderate*: preprint with a clear method, a vendor-run benchmark, or a production vendor's announcement. *weak*: very recent, unreviewed preprint, or a figure seen only in a summary.

---

## 0. Executive summary

1. **Output tokens are where routing and effort pay off.** On every current Claude model, output costs 5x input (for example Opus 5 is $5 in / $25 out, Opus 5.5 is $4 / $20, Sonnet 5 is $2 / $10). Thinking tokens are billed as output even when you never see them: the `display` setting changes what is shown, not what is billed. Token Bill's current trace schema can't tell thinking tokens apart from visible output.
2. **Lowering effort gives the biggest measured savings at the lowest risk.** Anthropic's published runs on its current models:
   - Research-type work: `medium` matches the default's accuracy at 70-87% of the cost.
   - Long-horizon coding on Opus 5: `medium` costs 50% at about -2 points; `low` costs 25% at about -8 points.
   - "Run at `low`, re-run failures at default": about 93% pass at about $0.45/task, against 91.7% at $0.93 running everything at the default.
   - Third-party cost per task for Opus 5.5 ranges from $0.55 at low effort to $5.98 at max, about 11x within one model.
3. **Academic routers and cascades claim large savings, but only against quality metrics they can check.** Published claims:
   - FrugalGPT: up to 98% cost reduction while matching GPT-4.
   - RouteLLM: more than 85% on MT-Bench, 45% on MMLU, 35% on GSM8K, at 95% of GPT-4 quality.
   - Hybrid LLM: 40% fewer large-model calls with no quality drop.
   - AutoMix: more than 50% saved.
   - Mixture-of-Thoughts cascade: GPT-4 quality at 40% of the cost.

   However, LLMRouterBench (2026; 400K instances, 33 models) found that many routers, including commercial ones, "fail to reliably outperform a simple baseline", and a well-tuned kNN router often matches learned routers.
4. **Production routers now route at cache boundaries.** A router that switches models turn by turn destroys prompt caching, because caches are model-scoped. Changing effort, speed, or thinking config between requests also invalidates the cache.
   - GitHub Copilot's auto selection routes "along natural cache boundaries".
   - Azure's model router added "session affinity" to preserve cache reuse.
   - AWS Bedrock's router claims "up to 30%" savings but can't use your own performance data.

   Token Bill already detects `model-switch` as a cache breaker. It should extend that detector into a routing-aware cost model.
5. **Agent-level routing is where coding-agent money is.**
   - Anthropic advisor pattern: Sonnet + Opus advisor scored +2.7 points on SWE-bench Multilingual at 11.9% lower cost per task. Haiku + Opus advisor cost 85% less per task than Sonnet solo on BrowseComp.
   - Orchestrator plus cheap workers: 47-55% cheaper on work larger than a context window, at 10-12 points lower score.
   - `opusplan` in Claude Code, and Aider's architect/editor split (SOTA 85% in 2024), use the same plan-strong, execute-cheap idea.
   - Trajectory-level routing (SWE-Router, 2026) beats routing on the task description alone.
6. **The research on overthinking is consistent.** Reasoning models:
   - spend 1,953% more tokens than conventional models on "2+3", and their first solution round is already correct in more than 92% of cases;
   - follow an inverted-U accuracy curve as chain-of-thought length grows;
   - show inverse scaling on some tasks.

   In agents, choosing lower-overthinking trajectories gave about 30% better performance and 43% lower cost on SWE-bench Verified. Prompt-level fixes cut tokens substantially: TALE -68.6%, Chain of Draft down to 7.6% of CoT tokens, concise CoT -48.7% length. The caveats are real: CCoT cost 27.7% accuracy on GPT-3.5 math, and too-small budgets *increase* tokens ("token elasticity").
7. **Output-shape prompts are a free win.**
   - One-line vs two-line vs five-section memo output: 39% fewer output tokens, no accuracy difference (Anthropic triage agent).
   - OpenAI `verbosity`: 560 / 849 / 1,288 output tokens for low / medium / high on the same prompt.
   - On Opus 5, effort does *not* reliably shorten visible responses, so length must be prompted.
   - Structured outputs guarantee parseable JSON but add a system prompt and invalidate the cache when the format changes. The "format hurts reasoning" claim is disputed.
8. **Batch and flex tiers are a flat 50% off, and they stack with caching.** Anthropic, OpenAI (Batch and flex), and Gemini all offer it. Anthropic batches mostly finish in under 1 hour; batch cache hit rates are 30-98%. Premium tiers go the other way: fast mode costs 2x ($10/$50 on Opus 5, $8/$40 on Opus 5.5), and US-only inference costs 1.1x. Token Bill prices neither today.
9. **"Safe to recommend" requires cost per completed task plus an eval with error bars.** Recommended practice:
   - Compare configurations on cost-of-pass or cost per solved task, not price per token.
   - Price the tail: 2 of 20 problems carried 43% of one run's spend.
   - Use paired comparisons with clustered standard errors.
   - Size the eval: about 20-30 frozen cases per lever decision; about 50 cases x 5 trials before a production cutover.

   Majority voting and self-refinement "rarely justify their added costs". Adaptive-Consistency cuts samples by up to 7.9x at under 0.1% accuracy loss.
10. **Enterprise telemetry already exists.**
    - Claude Code's OpenTelemetry cost and token metrics carry `model`, `effort`, `speed`, `query_source` (main/subagent), and `agent.name`.
    - The Claude Code Analytics API gives per-user edit accept/reject counts, commits, and PRs, which serve as outcome proxies.
    - The Admin usage API groups by `speed`, `service_tier`, `inference_geo`, and `context_window`.
    - Anthropic's published baseline is about $13 per developer per active day, $150-250 per developer per month, with 90% of users under $30 per active day.

    Token Bill's per-call parser, however, would **undercount advisor spend** (advisor tokens appear only in `usage.iterations`, not top-level usage). Its pricing table has no entry for `claude-opus-5-5`, Claude Code's current default model, so those calls get tokens but no dollars.

---

## 1. Why this track matters for the bill

| Fact | Number | Source |
|---|---|---|
| Output/input price ratio on every current Claude model | 5x (Opus 5 $5/$25; Opus 5.5 $4/$20; Sonnet 5 $2/$10; Haiku 4.5 $1/$5; Fable 5.1 $10/$50) | [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing) (accessed 2026-09-23) |
| Thinking billing | Thinking tokens are billed as output. `display: "omitted"` / `"summarized"` change what is visible, not what is billed. "The billed output token count does **not** match the visible token count." | [Steering thinking: Pricing](https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost) (accessed 2026-09-23) |
| Thinking telemetry | `usage.output_tokens_details.thinking_tokens` (streaming: final `message_delta` only). OpenAI equivalent: `output_tokens_details.reasoning_tokens`. | same; [OpenAI reasoning guide](https://developers.openai.com/api/docs/guides/reasoning) (accessed 2026-09-23) |
| Prior-turn thinking | On Opus 4.5+, Sonnet 4.6+, and Fable/Mythos, prior-turn thinking blocks stay in context and are **billed as input**. Older models and Haiku strip them. | [Thinking: block preservation](https://platform.claude.com/docs/en/build-with-claude/thinking) (accessed 2026-09-23) |
| Tokenizer drift | Claude 4.7+ tokenizer "produces approximately 30% more tokens for the same text". Cross-generation per-token price comparisons therefore mislead. | [Anthropic pricing](https://platform.claude.com/docs/en/about-claude/pricing) |
| Enterprise Claude Code baseline | about $13 per developer per active day; $150-250 per developer per month; below $30 per active day for 90% of users | [Claude Code: Manage costs](https://code.claude.com/docs/en/costs) (accessed 2026-09-23) |

Implication: for coding agents, which are heavily cached and output- and thinking-heavy, the levers in this track (effort, model tier, output shape, and which tier the traffic runs on) act on the most expensive token category. Caching acts on the cheapest input category.

---

## 2. Findings

### A. Routing and cascades: the academic evidence

**A1. FrugalGPT (LLM cascade).** Chen, Zaharia, Zou. arXiv 2305.05176, submitted 2023-05-09. A cascade with a learned scorer that stops at the first "good enough" answer "can match the performance of the best individual LLM (e.g. GPT-4) with up to 98% cost reduction or improve the accuracy over GPT-4 by 4% with the same cost." Evidence: *moderate*. The work is seminal, but the numbers come from 2023 models on classification/QA-style tasks with checkable answers. Source: https://arxiv.org/abs/2305.05176

**A2. RouteLLM (preference-trained router).** Ong et al. (LMSYS/Berkeley). arXiv 2406.18665, v1 2024-06-26, v4 2025-02-23; LMSYS blog 2024-07-01.
- The router is trained on Chatbot Arena preference data plus augmentation.
- Cost reductions against GPT-4 only: "over 85%" on MT-Bench, 45% on MMLU, 35% on GSM8K, while achieving 95% of GPT-4's performance.
- On MT-Bench, only 14% of calls went to GPT-4.
- The routers were "over 40% cheaper" than the Martian and Unify commercial routers at comparable quality.
- They transferred to new model pairs, including Claude 3 Opus/Llama 3 8B, without retraining.

Evidence: *moderate-strong* (open source, reproducible). Sources: https://arxiv.org/abs/2406.18665 ; https://lmsys.org/blog/2024-07-01-routellm/

**A3. Hybrid LLM (quality-aware small/large routing).** Ding et al. (Microsoft). ICLR 2024; arXiv 2404.14618 (2024-04-22). "Up to 40% fewer calls to the large model, with no drop in response quality." The quality threshold is tunable at test time. Evidence: *strong*. Source: https://arxiv.org/abs/2404.14618

**A4. AutoMix (self-verification + POMDP router).** Aggarwal, Madaan et al. NeurIPS 2024; arXiv 2310.12963 (v1 2023-10-19, v5 2025-01-19). Few-shot self-verification of the small model's answer drives escalation, "reducing computational cost by over 50% for comparable performance". Evidence: *strong*. Source: https://arxiv.org/abs/2310.12963

**A5. Mixture-of-Thoughts cascade (answer consistency as the difficulty signal).** Yue et al. ICLR 2024; arXiv 2310.03094. If the weak model's sampled answers (CoT and Program-of-Thought) agree, it answers; otherwise the question escalates. The cascade gives "performance comparable to using solely the stronger LLM but require only 40% of its cost". Evidence: *strong*. It needs no logprobs, so it works on any API. Source: https://arxiv.org/abs/2310.03094

**A6. Self-Route (route between RAG and long-context).** Li et al., EMNLP 2024 Industry; arXiv 2407.16833. The model first judges whether RAG context is enough and only falls back to full long-context when it isn't.
- Token use was 38.39% of long-context for Gemini-1.5-Pro (65% cost cut) and 61.40% for GPT-4o (39% cut).
- Average score changed by -2.2% (Gemini) and +0.2% (GPT-4o).

Evidence: *strong*. Relevance: agents that stuff whole files or repositories into context. Source: https://arxiv.org/html/2407.16833v2

**A7. Routers often fail to beat simple baselines.**
- LLMRouterBench (Li et al., arXiv 2601.07206, 2026-01-12): over 400K instances, 21 datasets, 33 models, 10 routers. "Many routing methods exhibit similar performance... several recent approaches, including commercial routers, fail to reliably outperform a simple baseline." There is a large gap to the oracle, driven by "model-recall failures".
- Li (arXiv 2505.12601, rev. 2026-05-14): "a well-tuned k-Nearest Neighbors (kNN) approach not only matches but often outperforms state-of-the-art learned routers."
- RouterBench (Hu et al., arXiv 2403.12031): more than 405K precomputed outcomes, the standard offline harness.
- Arch-Router (Tran et al., arXiv 2506.16655, 2025-06-19): a 1.5B *preference-aligned* router maps queries to user-defined domain/action policies and can add new models without retraining. Enterprises often want *policy* routing (for example "refactors go to tier X"), not only benchmark-optimal routing.
- Moslem and Kelleher, TMLR 2026 survey (arXiv 2603.04445, final 2026-08-30): taxonomy of 7 routing paradigms along when/what/how axes.

Evidence: *strong* (converging independent results). Sources: https://arxiv.org/abs/2601.07206 ; https://arxiv.org/abs/2505.12601 ; https://arxiv.org/abs/2403.12031 ; https://arxiv.org/abs/2506.16655 ; https://arxiv.org/abs/2603.04445

**A8. Cascades built on token-level uncertainty.** Gupta et al. (Google), arXiv 2404.10136 (2024-04-15). Naive aggregation of sequence uncertainty has a "length bias"; learned deferral rules over token-level uncertainty improve cost/quality trade-offs (FLAN-T5). Evidence: *moderate*. Practical note: this family needs token log-probabilities, which Token Bill should not assume every provider exposes. Signals Token Bill *can* get from traces: self-consistency, tests passing, schema validation, and `stop_reason`. Source: https://arxiv.org/abs/2404.10136

**A9. Routers can be attacked into spending more.** Shafran, Schuster, Ristenpart, Shmatikov, arXiv 2501.01818 (2025-01-03). Query-independent "confounder gadgets" appended to any prompt force routers to the strong model in white-box and black-box settings, including commercial routers. Response quality is unaffected, so the only symptom is cost. Low-perplexity gadgets evade perplexity filters. Evidence: *moderate*. Enterprise implication: a routing deployment needs *routing-distribution drift monitoring* as a cost-security control. Source: https://arxiv.org/abs/2501.01818

### B. Routing inside agents and coding workflows

**B1. Anthropic's measured multi-model shapes (current models).** From Anthropic's platform guide *Optimizing for cost and intelligence* (accessed 2026-09-23) and the bundled `shared/cost-optimization.md`:
- **Advisor** (cheap executor consults a frontier model):
  - Opus 5 executor + Fable 5.1 advisor: +3.5 points over Opus 5 alone at slightly lower cost (about 2 consults per task).
  - On chart-reading the pairing matched Fable 5.1 alone at `medium` but cost **2.6x more**, because it consulted on nearly every task.
  - "The consult rate is the fragile variable."
- **Orchestrator** (frontier lead + cheap workers):
  - On a 21.6M-token corpus task: 47-55% cheaper than Fable 5.1 solo, scoring 10-12 points lower, with latency of 2.3h instead of 15-20h.
  - When work "is one dependent chain, or fits in a single context", the coordinator's own model at lower effort came out ahead in every case measured.
- **Model upgrades can cut cost per solved task:** Sonnet 4.6 to Sonnet 5 was 15% cheaper per solved task and +5 points; Fable 5 to Fable 5.1 was 43% cheaper per solved task at the same score.
- **A bigger model at lower effort can beat a smaller model:** Fable 5.1 at `low` scored 88.6% at $0.54 per solved task, against Sonnet 5's 77.4% at $0.84.

Evidence: *strong* (first-party, current models; vendor-run). Sources: https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence ; https://platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool

**B2. Advisor tool launch numbers.** Anthropic blog, 2026-04-09:
- Sonnet + Opus advisor: +2.7 points on SWE-bench Multilingual, 11.9% lower cost per agentic task.
- Haiku + Opus advisor on BrowseComp: 41.2%, against 19.7% for Haiku solo.
- Haiku + Opus advisor trails Sonnet solo by 29% in score but costs 85% less per task.
- The advisor typically generates 400-700 text tokens per call.

**Billing trap:** "Top-level `usage` fields reflect executor tokens only. Advisor tokens are not rolled into the top-level totals"; they appear in `usage.iterations[]` with `type: "advisor_message"` and are billed at the advisor model's rates. Advisor-side caching breaks even at about 3 calls per conversation. Evidence: *strong*. Sources: https://claude.com/blog/the-advisor-strategy ; https://platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool

**B3. Plan-strong / execute-cheap in coding tools.**
- Claude Code ships `opusplan`: "Opus for planning, auto-switches to Sonnet for execution". It also offers per-subagent `model`/`effort` frontmatter and `CLAUDE_CODE_SUBAGENT_MODEL`.
- Built-in *Explore* and *Plan* subagents **inherit the main conversation's model** (Explore is capped at Opus on the Claude API). A developer on a premium model therefore pays premium rates for exploration unless a subagent model is set.
- Agent teams use "approximately 7x more tokens than standard sessions" when teammates run in plan mode.
- Aider (2024-09-26): splitting an "architect" reasoning model from an "editor" formatting model reached SOTA 85.0% on its editing benchmark (o1-preview + DeepSeek or o1-mini), against 80.5% for Sonnet + Sonnet.

Evidence: *strong* (product docs) / *moderate* (Aider benchmark). Sources: https://code.claude.com/docs/en/model-config ; https://code.claude.com/docs/en/sub-agents ; https://code.claude.com/docs/en/costs ; https://aider.chat/2024/09/26/architect.html

**B4. Trajectory-level and step-level routing for coding agents (2026 preprints).**
- **SWE-Router** (Son et al., arXiv 2607.00053, 2026-06-30): run a cheap model for a few exploratory turns, then decide to continue cheaply or escalate. The authors prove trajectory-informed routing "never harms routing and is strictly better whenever exploration is informative", and release a multi-LLM trajectory dataset. *moderate*.
- **Triage** (Madeyski, arXiv 2604.07494, 2026-04-08): on SWE-bench Lite, routing to the cheapest tier whose output passes the same verification gate is cost-effective only if "the lower-tier pass rate... exceed[s] the inter-tier cost ratio". This gives a crisp break-even rule Token Bill can compute. *moderate*.
- **AgentRouter** (Paul and Nandy, arXiv 2609.22951, 2026-09-19): a 12M-parameter per-step classifier over 4 tiers reports 72% cost reduction at 97.3% of frontier-only quality. Per-step RouteLLM and FrugalGPT reach only 31% and 44% there, "due to missing trajectory-level dependencies". *weak* (4 days old, unreviewed, 2 authors).
- **Efficient Agents** (Wang et al., arXiv 2508.02694, 2025-07-24): design choices (LLM, framework, test-time scaling) on GAIA reach 96.7% of OWL's performance while cost fell from $0.398 to $0.228, a 28.4% cost-of-pass improvement. *moderate*.

Sources: https://arxiv.org/abs/2607.00053 ; https://arxiv.org/abs/2604.07494 ; https://arxiv.org/abs/2609.22951 ; https://arxiv.org/abs/2508.02694

### C. Production routers (2024-2026) and the caching interaction

**C1. AWS Bedrock Intelligent Prompt Routing.**
- Claims: routes within a model family between exactly two models using a predicted "response quality difference" threshold against a fallback model; "can reduce costs by up to 30% without compromising on accuracy" (AWS product page).
- Documented limitations: "only optimized for English prompts"; "can't adjust routing decisions or responses based on application-specific performance data"; and effectiveness "depends on the initial training data".

Evidence: *moderate*. Sources: https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-routing.html (accessed 2026-09-23); https://aws.amazon.com/bedrock/intelligent-prompt-routing (accessed 2026-09-23)

**C2. Microsoft Foundry model router.** The doc was updated 2026-09-10.
- Routes across OpenAI, Anthropic (Opus 4.6-4.8, Haiku 4.5), xAI, DeepSeek, and Llama models in Balanced/Quality/Cost modes. `reasoning_effort` is passed through.
- Microsoft had to add **session affinity**: "By default, model router evaluates each request independently and might select a different underlying model for a later turn... Session affinity can improve the opportunity for prompt-cache reuse." It is best-effort, with a 30-minute stickiness window.
- Microsoft publishes no savings percentage in the doc. Its guidance is to benchmark against your current model before sending production traffic.

Evidence: *strong* for the mechanism. Source: https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/model-router

**C3. GitHub Copilot auto model selection.** Changelog 2026-05-20. Routes by task dimensions (reasoning, code-generation complexity, bug-diagnosis difficulty, tool orchestration) and model health, and explicitly "along natural cache boundaries to avoid unnecessary cache related costs". Paid users get a **10% discount** on model multipliers when using auto. No quantified quality data. Evidence: *moderate*. Source: https://github.blog/changelog/2026-05-20-auto-model-selection-now-routes-based-on-your-task-in-vs-code/

**C4. OpenRouter Auto Router.** Now a ~30-task-type classifier plus a ranking by "which models the OpenRouter community actually spends on over a trailing 7-day window". It uses `cost_tier` bands, charges no router fee, and reports the served `model`. It replaced the earlier Not Diamond-based router. Evidence: *moderate*. It optimizes popularity, not *your* quality. Source: https://openrouter.ai/docs/guides/routing/routers/auto-router

**C5. Cache-invalidation rules make naive per-request routing expensive.** Anthropic's prompt-caching doc:
- "Changing the `output_config.effort` value always invalidates message blocks."
- The thinking configuration "is rendered into the prompt, so changing it always invalidates message blocks."
- "Switching between `speed: \"fast\"` and standard speed invalidates system and message caches."
- Caches are per model (switching models mid-conversation starts cold). They are also isolated per workspace on the Claude API.

Per-message effort (beta `mid-conversation-output-config-2026-07-01`, on Fable 5.1, Mythos 5.1, Opus 5.5, and Opus 5) is the only way to change effort mid-conversation without a cache reset. Anthropic's measured cost of one cache break: a 25-token status line placed at the front of the system prompt made a triage run cost $4.24 instead of $0.59. On Fable 5.1 a broken turn on a 100K prefix costs $1.25 instead of $0.03.

Evidence: *strong*. Sources: https://platform.claude.com/docs/en/build-with-claude/prompt-caching ; https://platform.claude.com/docs/en/build-with-claude/effort ; https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost ; https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence

> Synthesis: the academic router literature mostly prices single-shot queries, where caching doesn't matter. For cached, multi-turn coding agents, **a router's savings must be netted against the cache it forfeits.** Production vendors (Microsoft, GitHub) have independently converged on routing per session or per cache boundary. Nobody else sells a "cache-aware routing simulator" computed from real billed traces. Token Bill's replay simulator is the natural place to build it.

### D. Reasoning-token efficiency: research results

**D1. Overthinking is large and measurable.** Chen et al., "Do NOT Think That Much for 2+3=?", arXiv 2412.21187 (v2 2025-02-01).
- o1-like models "consumed 1,953% more tokens than conventional models" on "2+3".
- They ran 3.2-4.5 solution rounds on ASDIV and MATH500, yet "in more than 92% of cases, the initial round of solutions produces the correct answer."
- Self-training (SimPO with First-Correct Solutions and reflection) cut QwQ tokens on MATH500 from 2,407.9 to 1,330.7 (-44.8%) at accuracy 93.0% to 92.8%. On GSM8K, tokens fell 46% while accuracy rose from 94.8% to 96.0%.

Evidence: *strong*. Source: https://arxiv.org/html/2412.21187v2

**D2. Survey taxonomy.** Sui et al., "Stop Overthinking", TMLR 2025 (arXiv 2503.16419, v4 2025-08-21) groups efficient-reasoning methods as (i) model-based (train shorter reasoners), (ii) reasoning-output-based (dynamically limit steps and length at inference), and (iii) input-prompt-based (difficulty- and length-aware prompting). Only (ii)-via-API-knobs and (iii) are available to an API customer like Token Bill's users. Evidence: *strong*. Source: https://arxiv.org/abs/2503.16419

**D3. TALE: token-budget-aware prompting, and "token elasticity".** Han et al., arXiv 2412.18547 (v1 2024-12-24, v5 2025-06-02).
- Estimating a per-question budget and putting it in the prompt gives "68.64% reduction in token usage" against vanilla CoT with "less than 5% decrease" in accuracy (GSM8K accuracy rose to 84.46%).
- **Token elasticity:** too-small budgets *increase* tokens. A 10-token budget produced 157 tokens, against 86 at a 50-token budget.
- Cost-of-Pass (below) identifies TALE-style budget-aware methods as promising.

Evidence: *moderate-strong*. Source: https://arxiv.org/html/2412.18547v1

**D4. Chain of Draft.** Xu et al. (Zoom), arXiv 2502.18600 (2025-02-25). Minimal "draft" reasoning steps match CoT accuracy "while using as little as only 7.6% of the tokens." Evidence: *moderate* (prompting technique, benchmark-specific). Source: https://arxiv.org/abs/2502.18600

**D5. Concise CoT, with a warning.** Renze and Guven, arXiv 2401.05618 (final 2024-10-19).
- Response length fell "48.70% for both GPT-3.5 and GPT-4", with average per-token cost down 22.67%.
- **But** "GPT-3.5 with CCoT incurs a performance penalty of 27.69%" on math.

Evidence: *strong*. This is why output-shortening must be gated per workload by an eval. Source: https://arxiv.org/abs/2401.05618

**D6. Longer reasoning can be worse, not just costlier.**
- Wu et al., arXiv 2502.07266: accuracy "follows an inverted U-shaped curve with CoT length". The optimal length falls as model capability rises and grows with task difficulty.
- Gema, Hägele et al., "Inverse Scaling in Test-Time Compute", TMLR (arXiv 2507.14417, rev. 2025-12-15): longer reasoning degrades accuracy on distractor-counting, spurious-regression, and constraint-tracking tasks. Claude models "become increasingly distracted by irrelevant information".
- OptimalThinkingBench (Aggarwal et al., arXiv 2508.13141): across 33 models, "no model is able to optimally think". Thinking models "overthink for hundreds of tokens on the simplest user queries".
- Anthropic's own effort guidance: `max` "on some structured-output or less intelligence-sensitive tasks... can lead to overthinking."

Evidence: *strong*. Sources: https://arxiv.org/abs/2502.07266 ; https://arxiv.org/abs/2507.14417 ; https://arxiv.org/abs/2508.13141 ; https://platform.claude.com/docs/en/build-with-claude/effort

**D7. Overthinking in agentic coding.** Cuadron et al., "The Danger of Overthinking", arXiv 2502.08235 (2025-02-12). Across 4,018 SWE-bench Verified trajectories:
- "Higher overthinking scores correlate with decreased performance."
- Named failure patterns: analysis paralysis, rogue actions, premature disengagement.
- Selecting lower-overthinking solutions gave about 30% better performance and cut compute cost 43%.

Evidence: *moderate-strong*. This is directly relevant to Claude Code fleets. Source: https://arxiv.org/abs/2502.08235

**D8. Skipping thinking can win under a fixed budget.** Ma, He, Snell, Griggs, Min, Zaharia, arXiv 2504.09858 (2025-04-14). Prompting a reasoning model to skip thinking ("NoThinking") beats thinking at matched token budgets on 7 benchmarks (51.3% vs 28.9% on AMC 23 at 700 tokens). Parallel NoThinking + best-of-N matches thinking at up to 9x lower latency. Evidence: *moderate*. Source: https://arxiv.org/abs/2504.09858

**D9. Train-time length control (for self-hosted or fine-tuned models).**
- L1/LCPO (Aggarwal and Welleck, COLM 2025, arXiv 2503.04697) makes reasoning length follow a prompted target.
- "Concise Reasoning via RL" (Fatemi et al., arXiv 2504.05185) shows RL losses on unsolvable problems push models toward verbosity. A second RL phase on solvable problems "significantly reduces response length while preserving or improving accuracy".
- Distilling step-by-step (Hsieh et al., Findings of ACL 2023, arXiv 2305.02301): a 770M T5 beat few-shot 540B PaLM using 80% of the data. This is the long-term "route to a distilled small model" option for very high-volume, narrow tasks.

Evidence: *strong* for the research. The option is out of scope for API-only enterprises. Sources: https://arxiv.org/abs/2503.04697 ; https://arxiv.org/abs/2504.05185 ; https://arxiv.org/abs/2305.02301

### E. Vendor reasoning and effort controls, with measured trade-offs

**E1. Anthropic effort: supported levels and defaults.**
- `output_config.effort` takes `low | medium | high | xhigh | max`. It is GA on current models and the Bedrock, Vertex, and Foundry clouds.
- It "affects **all tokens** in the response", including text, tool calls, and thinking. Lower effort means "fewer and terser tool calls".
- Defaults: `high` on most models, `medium` on Opus 5.5. In Claude Code the default is `xhigh` for Opus 4.7 and `medium` for Opus 5.5.
- Effort is "a behavioral signal, not a strict token budget."
- On Opus 5, "changing effort does not reliably shorten responses, so prompt for length."
- Changing top-level effort between requests invalidates the cache; per-message effort (beta) doesn't.

Evidence: *strong*. Sources: https://platform.claude.com/docs/en/build-with-claude/effort ; https://code.claude.com/docs/en/model-config

**E2. Anthropic measured effort trade-offs (current models).** From *Optimizing for cost and intelligence*, accessed 2026-09-23. This page supersedes older snapshots in the bundled skill.
- Research and knowledge work (Fable 5): `low` gives up 1-3 points for 1/3-1/2 the cost; `medium` matches the default at 70-87% of the cost; the default buys "no measurable gain over `medium`".
- Long coding on SWE-bench Pro (Opus 5): `medium` costs 50% at about -2 points; `low` costs 25% at about -8 points.
- DeepResearch Bench II (Fable 5.1): scores are nearly the same at low, medium, and high, while cost per task rises from $4.66 to $7.12.
- Terminal-Bench 3 hard tasks: Opus 4.7 at $183 per solved task, Opus 4.8 at $63, Opus 5 at $28.

Evidence: *strong* (vendor-run, current models). Source: https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence

**E3. "Run cheap, re-run failures at default": the cheapest cascade with a checker.**
- SWE-bench Pro, Opus 5: everything at `low` gives 16% failures. Re-running those failures at the default gives "~93% pass rate at ~$0.45 each", against "91.7% for $0.93" running everything at the default, so the same pass rate at half the cost.
- Starting at `medium` instead gives about 94% at about $0.61.
- Needs a usable failure signal (tests, validator), and wall-clock doubles on the failed subset.

Evidence: *strong*. Source: same page.

**E4. Task budgets (the model sees a countdown).**
- Beta `task-budgets-2026-03-13` on Opus 4.7-5.5 and Fable 5/5.1; not on Sonnet 5 or in Claude Code. The minimum is 20,000 tokens.
- Measured on SWE-bench Pro (Fable 5.1): a generous budget saved 44% for about -3 points pass@1; the tightest budget saved 58% for -6 points.
- Too-tight budgets "can cause refusal-like behavior". A mid-task budget change breaks the cache.

Evidence: *strong*. Sources: https://platform.claude.com/docs/en/build-with-claude/task-budgets ; https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence

**E5. `max_tokens` is a backstop, not a saver.**
- A 16,384 cap ended 15% of Opus 5 attempts and 43% of Fable 5.1 attempts, none of them solved. Cost per *solved* task didn't improve.
- A 64K cap cut off 2 of about 14,000 turns. A 128K cap had the same cost per solved task as 64K.
- The same pattern shows up on OpenAI: when `max_output_tokens` is reached, you may pay for reasoning and receive no visible output. OpenAI recommends reserving at least 25,000 tokens.

Evidence: *strong*. Sources: https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence ; https://developers.openai.com/api/docs/guides/reasoning

**E6. Adaptive thinking replaces fixed budgets on current Claude models.**
- `budget_tokens` is rejected with a 400 on Opus 4.7+, Sonnet 5, and Fable. It is deprecated on 4.6.
- On Opus 5.5 and Fable, thinking can't be disabled, so effort is the only depth control.
- Claude decides per request whether to think. Follow-up requests that only process tool results "can skip thinking", and thinking per request "tends to decrease as a conversation grows longer".
- Claude Code's `MAX_THINKING_TOKENS` is ignored (non-zero values) on adaptive models.

Evidence: *strong*. Sources: https://platform.claude.com/docs/en/build-with-claude/extended-thinking ; https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost ; https://code.claude.com/docs/en/costs

**E7. Other providers have the same knobs.**
- OpenAI `reasoning_effort` spans `none/minimal/low/medium/high/xhigh/max` (model-dependent; GPT-5.5 defaults to `medium`). Reasoning tokens are billed as output and reported in `output_tokens_details.reasoning_tokens`.
- OpenAI's GPT-5 `verbosity` example: 560 / 849 / 1,288 output tokens (poem) and 575 / 943 / 2,381 (code) for low / medium / high. "Output tokens scale roughly linearly with verbosity."
- Gemini also bills thinking tokens at output rates. A FinOps vendor analysis (CloudZero, 2026-07-16, updated 2026-09-04) estimates thinking "can account for 70-85% of the total output bill" on complex reasoning tasks (*weak*: secondary source; no percentage savings from Gemini thinking-budget settings was verified this pass).

Evidence: *strong* (OpenAI) / *weak* (Gemini share estimate). Sources: https://developers.openai.com/api/docs/guides/reasoning ; https://developers.openai.com/cookbook/examples/gpt-5/gpt-5_new_params_and_tools ; https://www.cloudzero.com/blog/gemini-pricing/ (secondary, 2026-09-04)

**E8. Independent benchmark: effort spread within one model.** The Artificial Analysis Intelligence Index page for Claude Opus 5.5 (Sep 2026) lists cost per task from $0.55 at low effort to $5.98 at max, about 11x, for the same model. Artificial Analysis defines cost per task as tokens consumed times per-category prices, weighted by index weights. Evidence: *moderate* (third party; figure seen through page summary). Sources: https://artificialanalysis.ai/models/releases/claude-opus-5-5 ; https://artificialanalysis.ai/methodology

### F. Output-length control and structured outputs

**F1. Prompting the output shape is a free output-token win.**
- Anthropic's triage agent tested three output formats: one line at $0.49 per run (39% fewer output tokens), the original two lines at $0.57, and a 5-section memo at $1.40. Accuracy was 78-85% for all three, with no significant difference.
- Separately, removing a "verify twice" instruction cut cost by about 1/3, and removing "be maximally thorough" gave a similar cut.
- Prompts written for Opus 4.8 cost 36% more on Opus 5 at the same accuracy. After a prompt audit they were 14% cheaper, and accuracy rose from 92% to 97%.

Evidence: *strong* (vendor-run). Source: https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence

**F2. Stop sequences as content-aware early exits.** Register a sentinel such as `<CANNOT_REVIEW>` so the model stops instead of spending tokens explaining. Also treat `stop_reason: "max_tokens"` as a failed attempt, not something to retry at the same cap. Evidence: *strong* (vendor guidance; no % given). Source: bundled `claude-api/shared/cost-optimization.md` §2.4, mirrored in the public guide above.

**F3. Structured outputs: reliability win, small input cost, cache hazard, disputed quality effect.**
- Anthropic's `output_config.format` uses constrained decoding. It adds an injected system prompt (more input tokens), and "Changing the `output_config.format` parameter will invalidate any prompt cache". Compiled grammars are cached for 24 hours.
- Quality: Tam et al. ("Let Me Speak Freely?", arXiv 2408.02442, 2024) found "a significant decline in LLMs reasoning abilities under format restrictions".
- dottxt's rebuttal ("Say What You Mean", 2024) re-ran the tasks with matched prompts. Structured output scored *higher*: GSM8K 0.78 vs 0.77, Last Letter 0.77 vs 0.73, Shuffle Objects 0.44 vs 0.41. It attributed the reported drop to prompt mismatch and to conflating JSON-mode with constrained generation.
- Practical rule: put a reasoning field before the answer field, keep schemas stable per route, and eval before switching.

Evidence: *moderate* (disputed). Sources: https://platform.claude.com/docs/en/build-with-claude/structured-outputs ; https://arxiv.org/abs/2408.02442 ; https://blog.dottxt.ai/say-what-you-mean.html

### G. Batch vs realtime vs premium tiers

**G1. 50% off for anything nobody is waiting on.**
- **Anthropic Message Batches:** "All usage is charged at 50% of the standard API prices". Caching discounts stack. "Most batches finish in less than 1 hour"; batches expire at 24h and expired requests aren't billed. Batch cache hits are best-effort: "cache hit rates ranging from 30% to 98%". Use the 1-hour TTL for shared context. Batches can't run a tool loop (single-shot) and aren't available for Managed Agents.
- **OpenAI flex:** `service_tier: "flex"` is "priced at Batch API rates". A 429 `resource_unavailable` response isn't charged.
- **Gemini Batch API:** "50% of the standard interactive API cost", 24h target, and context caching is supported (page updated 2026-09-17).

Evidence: *strong*. Sources: https://platform.claude.com/docs/en/build-with-claude/batch-processing ; https://developers.openai.com/api/docs/guides/flex-processing ; https://ai.google.dev/gemini-api/docs/batch-api

**G2. Premium modifiers that inflate the bill.**
- Fast mode (Opus 5.5 at $8/$40, Opus 5 and 4.8 at $10/$50, which is 2x standard) runs the same model at up to 2.5x output tokens per second. It doesn't share cache with standard speed and isn't available in batch.
- US-only `inference_geo` costs 1.1x on all token categories. Bedrock and Vertex regional endpoints carry a 10% premium over global.
- Web search costs $10 per 1,000 searches.
- All of these stack with caching multipliers.
- Usage reports expose them: `usage.speed`; the Admin usage API groups by `speed`, `service_tier`, and `inference_geo`; Claude Code OTel exposes `speed`.

Evidence: *strong*. Sources: https://platform.claude.com/docs/en/build-with-claude/fast-mode ; https://platform.claude.com/docs/en/about-claude/pricing ; https://platform.claude.com/docs/en/manage-claude/usage-cost-api

### H. Measurement: what makes a routing or effort recommendation safe

**H1. Optimize cost per completed task, and price the tail.**
- Anthropic: "API spend is optimized in units of **cost per completed task, not cost per token**". Free wins (caching, input hygiene, output hygiene, batch) come before trade-offs (effort, budgets, model, multi-model), and tradeoffs are never applied silently.
- "Price the tail, not the median": on one 20-problem run, 2 problems carried 43% of the spend.
- Plot score against cost per task and take the Pareto frontier.

Evidence: *strong*. Sources: bundled `shared/cost-optimization.md`; https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence

**H2. Cost-of-Pass as the formal metric.** Erol, El, Suzgun, Yuksekgonul, Zou, arXiv 2504.13359 (rev. 2026-02-26). Defines "the expected monetary cost of generating a correct solution".
- Lightweight models dominate basic quantitative tasks, large models knowledge tasks, and reasoning models complex quantitative tasks.
- Frontier cost-of-pass on complex quantitative tasks roughly halves every few months, which argues for periodic re-evaluation.
- "Majority voting and self-refinement rarely justify their added costs", while budget-aware methods like TALE show promise.

Evidence: *strong*. Source: https://arxiv.org/abs/2504.13359

**H3. If you sample multiple times, sample adaptively.** Adaptive-Consistency (Aggarwal, Madaan, Yang, Mausam; EMNLP 2023, arXiv 2305.11860) "reduces sample budget by up to 7.9 times with an average accuracy drop of less than 0.1%" across 17 datasets. Evidence: *strong*. Source: https://arxiv.org/abs/2305.11860

**H4. Statistics for keep/revert decisions.**
- Miller (Anthropic), "Adding Error Bars to Evals", arXiv 2411.00640 (2024-11-01): report standard errors, cluster them when questions are related, use *paired* differences when comparing configurations on the same questions, run power analysis, and reduce variance by resampling.
- Anthropic's cost workflow: about 20-30 frozen real requests per lever decision; about 50 cases with at least 5 trials per configuration before production cutover; "Never keep or revert on a one-case swing".

Evidence: *strong*. Sources: https://arxiv.org/abs/2411.00640 ; bundled `shared/cost-optimization.md` Step 3

**H5. LLM-as-judge is usable but biased, including toward verbosity.** Zheng et al., NeurIPS 2023 D&B (arXiv 2306.05685): GPT-4 judges reach "over 80% agreement" with humans, the same level as human-human agreement. Known biases: position, **verbosity**, and self-enhancement. This matters here: a judge that prefers longer answers will systematically penalize the concise outputs Token Bill recommends. Use pairwise order swaps and length-controlled rubrics. Evidence: *strong*. Source: https://arxiv.org/abs/2306.05685

**H6. Outcome and attribution telemetry that already exists at enterprise scale.**
- **Claude Code OTel** metrics `claude_code.cost.usage` and `claude_code.token.usage` carry `model`, `effort`, `speed`, `query_source` (main/subagent/auxiliary), `agent.name`, `skill.name`, `mcp_server.name`, `session.id`, and `user.account_uuid`.
- **Outcome proxies** from OTel: `claude_code.lines_of_code.count`, `commit.count`, `pull_request.count`, `code_edit_tool.decision`.
- **Claude Code Analytics API**: per-user daily `edit_tool.accepted/rejected`, commits, PRs, and per-model tokens and estimated cost (up to 1h delay).
- **Admin Usage API**: filter and group by `model`, `api_key_id`, `workspace_id`, `service_tier`, `context_window`, `inference_geo`, and `speed` (beta), in 1m/1h/1d buckets. The cost API excludes Priority Tier.
- **Claude Code `modelPricing` managed setting** reports spend at contracted rates.

Evidence: *strong*. Sources: https://code.claude.com/docs/en/monitoring-usage ; https://platform.claude.com/docs/en/build-with-claude/claude-code-analytics-api ; https://platform.claude.com/docs/en/manage-claude/usage-cost-api ; https://code.claude.com/docs/en/costs

---

## 3. Quantified lever table (what the evidence supports)

| Lever | Typical savings reported | Quality cost reported | Needs eval? | Best evidence |
|---|---|---|---|---|
| Batch / flex tier for non-interactive traffic | 50% of that traffic (stacks with cache) | none (latency only) | no | Anthropic/OpenAI/Gemini docs |
| Output-shape prompt (terse format) | up to 39% fewer output tokens | none measured on triage agent | light | Anthropic cost guide |
| Remove over-instruction ("verify twice", "be maximally thorough") | ~1/3 of task cost | none measured | light | Anthropic cost guide |
| Effort `high` to `medium` (research-type work) | 13-30% | about 0 points | yes | Anthropic cost guide |
| Effort to `medium` (long coding, Opus 5) | ~50% | about -2 points | yes | Anthropic cost guide |
| Low effort, re-run failures at default | ~50% | same pass rate | needs a checker | Anthropic cost guide |
| Task budgets (Fable 5.1, coding) | 44-58% | -3 to -6 points | yes | Anthropic cost guide |
| Advisor (cheap executor + frontier advisor) | 11.9% (Sonnet+Opus) to 85% (Haiku+Opus vs Sonnet) | +2.7 to -29% score | yes | Anthropic blog/docs |
| Orchestrator + cheap workers (work larger than context) | 47-55% | -10 to -12 points | yes | Anthropic cost guide |
| Router/cascade (single-shot, checkable) | 35-98% (paper-dependent) | about 0 at the chosen threshold | yes, and a baseline comparison | FrugalGPT, RouteLLM, AutoMix, MoT |
| Managed router (Bedrock) | up to 30% | "without compromising accuracy" (vendor) | yes | AWS |
| Prompt-level concise reasoning (TALE / CoD / CCoT) | 49-92% of reasoning tokens | 0 to -27.7% (math, weak model) | yes | TALE, CoD, CCoT papers |
| Avoid fast mode where no one needs the speed | 50% of that traffic's token cost | none | no | Anthropic pricing |
| Subagents on a cheaper model | model price ratio (e.g. Opus 5.5 to Haiku = 4x cheaper per token) | task-dependent | yes | Claude Code docs |

---

## 4. What Token Bill should build (from this track)

Today Token Bill is an excellent *input-side* and cache forensics tool. This track shows that the largest remaining dollars for a coding-agent fleet sit in **output/thinking tokens and in the model, effort, and tier choice**. Those are also the areas where a wrong recommendation damages quality. The design principle: **exact where billing is exact, measured where behavior changes, and never silently trade quality.**

### 4.1 Correctness prerequisites (build first; size S-M)

1. **Trace schema v2.** Capture per call:
   - `usage.output_tokens_details.thinking_tokens`
   - `usage.iterations[]` (advisor sub-inferences, refusal fallbacks), because advisor tokens are *not* in top-level usage
   - `usage.speed`, `service_tier` (batch / standard / priority), and `inference_geo`
   - the cache-write TTL split (`ephemeral_5m` / `ephemeral_1h`)
   - `usage.server_tool_use` (web search count)
   - the **served** `model` (fallbacks and routers change it) as well as the requested one
   - request knobs: `output_config.effort`, `thinking` config (type/display), `max_tokens`, `output_config.format` hash, `task_budget`, `stop_sequences`, and the request id
   - an optional `outcome` block (pass/fail, tests, accepted edit, human rating)

   Update the duck-typed recorder to capture all of these.
2. **Pricing engine v2:**
   - Add `claude-opus-5-5` ($4/$20; cache read 0.05x). It is Claude Code's current default and currently prices as "unknown".
   - Add 1h cache writes at 2x, batch at 0.5x, fast-mode rates, the 1.1x data-residency multiplier, web-search per-request fees, and per-iteration pricing at each iteration's model.
   - Add a **contracted-rate override**, mirroring Claude Code's `modelPricing`.
   - Treat tokenizer changes (4.7+ ~30% more tokens) as a first-class caveat in any cross-model comparison.

### 4.2 Output and reasoning analytics (size M)

3. **Output waterfall:** split output dollars into thinking, visible text, and tool-call arguments. Show thinking share per route, per model, and per effort level, and flag routes where thinking is more than X% of output on turns that produced short answers ("overthinking on trivial turns", per D1/D6/OptimalThinkingBench).
4. **Truncation-waste detector:** find `stop_reason == max_tokens` calls, price them as wasted spend, and recommend raising the cap (64K for agentic work, 128K at xhigh/max) or lowering effort, per E5.
5. **Verbosity and over-instruction detector:** track visible output tokens per turn type, with trend and outlier detection. Flag system-prompt phrases known to inflate cost ("verify twice", "be maximally thorough") using a pattern list mirroring Anthropic's `prompt-audit`. Suggest a terse output-shape template (F1).
6. **Preserved-thinking bloat:** on keep-all models, measure input dollars spent re-reading prior-turn thinking blocks. Surface `clear_thinking` and compaction trade-offs, noting that the cost guide says context editing cost more than it saved on short runs.

### 4.3 New cache breakers tied to routing and effort (size S; extends `breakers.py`)

7. Add these breakers to the existing five, each with fix text and dollars recovered from the replay simulator:
   - `effort-churn`: top-level effort changed mid-run. Fix: hold effort constant or use per-message effort (beta).
   - `thinking-config-churn`.
   - `speed-churn`: fast/standard switching.
   - `format-churn`: structured-output schema changed.
   - `task-budget-churn`.
   - `router-switch`: a served-model change inside a session, meaning a router or fallback broke the cache.
   - `advisor-cache-toggle`.

### 4.4 Tier and premium detectors (size S-M)

8. **Batch-eligibility detector:** classify runs as non-interactive (no human inter-turn gaps, CI or cron API keys and workspaces, single-shot calls, large fan-out bursts). Report "50% of $X is batchable", adjusted for best-effort batch cache hit rates of 30-98%.
9. **Premium-spend report:** fast-mode dollars and the premium portion paid, US-geo 1.1x dollars, Priority Tier usage, and web-search fees. Flag fast mode on non-interactive traffic.

### 4.5 Routing and effort recommendation engine (size L; the differentiator)

10. **Cache-aware counterfactual pricing.** For "what if this route ran on model M at effort E", Token Bill can compute an exact *lower bound on token prices* only for the same tokens. Real token counts change with model and effort, so the tool must say that plainly and show three numbers:
    - (a) the same-tokens price delta (exact);
    - (b) the published-curve estimate (a range, labeled "estimate", from E2/E3/E4);
    - (c) **measured** figures from a replay sweep.

    Always net in the **cache forfeited** by switching: models don't share caches, and effort/speed/thinking changes break message caches. Simulate per-request, per-session, and per-cache-boundary routing policies against the real TTL walk. Production routers already route at cache boundaries (C2, C3); no academic router models this.
11. **`tokenbill sweep`: an eval-gated replay harness.**
    - Sample N frozen real requests per route. Stub side-effecting tools, or restrict the sample to replay-safe requests.
    - Run the effort and model grid in a stable order.
    - Grade with the user's checker (tests, schema, golden answers, or an LLM judge with swap-order and length controls, per H5).
    - Report **cost per pass with paired CIs and clustered SEs** (H4), and a Pareto frontier plot.
    - Emit keep/revert verdicts only when significant, plus a "needs more trials" estimate from power analysis.
    - Pre-compute and display the dollar cost of the sweep and require approval, because sweeps spend real money.
12. **Break-even calculators:**
    - Cascade/escalation: a downgrade pays only if `p_cheap > c_cheap / c_strong`, net of cache loss (Triage, B4).
    - Re-run-failures-at-higher-effort: from the observed failure rate and per-attempt costs (E3).
    - Advisor consult-rate sensitivity (B1).
13. **Trajectory-aware routing insights for coding agents:** use the first K turns' features (files touched, tool errors, thinking volume) to predict tail cost, following SWE-Router (B4). Start with a **kNN baseline** before anything learned (A7). Report where cheap-first-then-escalate would have saved money on historical traces.
14. **Router-integrity monitor:** track the distribution of served models per route and alert on drift toward premium tiers. This detects confounder-gadget attacks (A9) and silent vendor router changes (C4).

### 4.6 Fleet mode for thousands of developers (size L)

15. **Ingest fleet telemetry:** Claude Code OTel metrics (`effort`, `speed`, `query_source`, `agent.name`), the Claude Code Analytics API, and the Admin usage/cost API. From these, compute:
    - cost per accepted edit, per commit, per PR, per active developer-day, benchmarked against the published $13 per active day and 90%-under-$30 figures;
    - effort and model mix per team;
    - **subagent spend on premium models** (Explore/Plan inherit the main model);
    - agent-team multipliers;
    - tail developers and sessions, where 10% of cases often carry most of the spend.
16. **Policy-as-code output:** generate reviewed diffs for Claude Code managed settings: per-model `modelSettings.effort`, `CLAUDE_CODE_SUBAGENT_MODEL` (with a force flag only where the evals support it), `modelPricing` for contracted rates, and API request templates (effort, `max_tokens`, output-shape prompt). Label each diff *free win* or *trade-off: needs eval*, following Anthropic's workflow in H1.

### 4.7 Trust rails (size S; necessary for enterprise sign-off)

17. Every recommendation carries a label (**exact / estimated / measured**), its data source, its evidence link, and whether quality was validated. Trade-offs are never auto-applied. Report "no change recommended" as a valid outcome. State threats to validity: vendor-run curves, tokenizer changes, workload drift, and re-sweep triggers on model migration.

---

## 5. Open questions and threats to validity

- The largest-savings numbers for current Claude models (effort curves, re-run policy, task budgets, advisor/orchestrator) are **vendor-run** on vendor-chosen benchmarks. They are directional. Token Bill must re-measure them on each customer's traffic.
- The academic router results (2023-2025) are mostly single-shot QA with checkable answers and old model pairs. Their transfer to cached, multi-turn coding agents on 2026 models is unproven. The 2026 agent-routing preprints (SWE-Router, AgentRouter) are unreviewed.
- Traces alone can't give counterfactual token counts for another model or effort level. Any "switch model" savings figure without a replay sweep is an estimate.
- Outcome signals for coding (edit acceptance, commits, PRs) are proxies. They can be gamed and don't measure correctness. They need pairing with tests or review data.
- The structured-output quality impact is disputed (F3). Treat it as workload-specific.
- No primary-source percentage was found for Gemini thinking-budget savings or for Azure model-router savings.
- Per-message effort, task budgets, and advisor features are beta and model-gated. Availability differs on Bedrock, Vertex, and Foundry.

## 6. Source list (all opened during this pass)

Academic (date = arXiv submission or latest revision shown):
- FrugalGPT, arXiv 2305.05176 (2023-05-09) https://arxiv.org/abs/2305.05176
- RouteLLM, arXiv 2406.18665 (2024-06-26; v4 2025-02-23) https://arxiv.org/abs/2406.18665 ; LMSYS blog (2024-07-01) https://lmsys.org/blog/2024-07-01-routellm/
- Hybrid LLM, ICLR 2024, arXiv 2404.14618 (2024-04-22) https://arxiv.org/abs/2404.14618
- AutoMix, NeurIPS 2024, arXiv 2310.12963 (v5 2025-01-19) https://arxiv.org/abs/2310.12963
- MoT cascades, ICLR 2024, arXiv 2310.03094 (2024-02-08) https://arxiv.org/abs/2310.03094
- Self-Route, EMNLP 2024 Industry, arXiv 2407.16833 (2024-10-17) https://arxiv.org/html/2407.16833v2
- RouterBench, arXiv 2403.12031 (2024-03-28) https://arxiv.org/abs/2403.12031
- kNN routers, arXiv 2505.12601 (rev. 2026-05-14) https://arxiv.org/abs/2505.12601
- LLMRouterBench, arXiv 2601.07206 (2026-01-12) https://arxiv.org/abs/2601.07206
- Routing/cascading survey, TMLR 2026, arXiv 2603.04445 (2026-08-30) https://arxiv.org/abs/2603.04445
- Token-level cascades, arXiv 2404.10136 (2024-04-15) https://arxiv.org/abs/2404.10136
- Rerouting LLM Routers, arXiv 2501.01818 (2025-01-03) https://arxiv.org/abs/2501.01818
- Arch-Router, arXiv 2506.16655 (2025-06-19) https://arxiv.org/abs/2506.16655
- SWE-Router, arXiv 2607.00053 (2026-06-30) https://arxiv.org/abs/2607.00053
- Triage, arXiv 2604.07494 (2026-04-08) https://arxiv.org/abs/2604.07494
- AgentRouter, arXiv 2609.22951 (2026-09-19) https://arxiv.org/abs/2609.22951
- Efficient Agents, arXiv 2508.02694 (2025-07-24) https://arxiv.org/abs/2508.02694
- Overthinking "2+3", arXiv 2412.21187 (v2 2025-02-01) https://arxiv.org/html/2412.21187v2
- Stop Overthinking survey, TMLR 2025, arXiv 2503.16419 (v4 2025-08-21) https://arxiv.org/abs/2503.16419
- TALE, arXiv 2412.18547 (v1 2024-12-24; v5 2025-06-02) https://arxiv.org/html/2412.18547v1
- Chain of Draft, arXiv 2502.18600 (2025-03-03) https://arxiv.org/abs/2502.18600
- Concise CoT, arXiv 2401.05618 (2024-10-19) https://arxiv.org/abs/2401.05618
- When More is Less (CoT length), arXiv 2502.07266 (v3 2025-05-27) https://arxiv.org/abs/2502.07266
- Inverse Scaling in Test-Time Compute, TMLR, arXiv 2507.14417 (2025-12-15) https://arxiv.org/abs/2507.14417
- OptimalThinkingBench, arXiv 2508.13141 (2025-10-04) https://arxiv.org/abs/2508.13141
- Danger of Overthinking, arXiv 2502.08235 (2025-02-12) https://arxiv.org/abs/2502.08235
- NoThinking, arXiv 2504.09858 (2025-04-14) https://arxiv.org/abs/2504.09858
- L1/LCPO, COLM 2025, arXiv 2503.04697 (2025-10-03) https://arxiv.org/abs/2503.04697
- Concise Reasoning via RL, arXiv 2504.05185 (2025-11-21) https://arxiv.org/abs/2504.05185
- Distilling step-by-step, Findings ACL 2023, arXiv 2305.02301 https://arxiv.org/abs/2305.02301
- Cost-of-Pass, arXiv 2504.13359 (rev. 2026-02-26) https://arxiv.org/abs/2504.13359
- Adaptive-Consistency, EMNLP 2023, arXiv 2305.11860 https://arxiv.org/abs/2305.11860
- Adding Error Bars to Evals, arXiv 2411.00640 (2024-11-01) https://arxiv.org/abs/2411.00640
- LLM-as-a-Judge (MT-Bench), NeurIPS 2023, arXiv 2306.05685 https://arxiv.org/abs/2306.05685
- Let Me Speak Freely?, arXiv 2408.02442 (2024-10-14) https://arxiv.org/abs/2408.02442

Vendor and production (accessed 2026-09-23 unless dated):
- Anthropic, Optimizing for cost and intelligence https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence
- Anthropic, Effort https://platform.claude.com/docs/en/build-with-claude/effort
- Anthropic, Steering thinking (pricing) https://platform.claude.com/docs/en/build-with-claude/thinking-steering-and-cost
- Anthropic, Thinking https://platform.claude.com/docs/en/build-with-claude/thinking
- Anthropic, Extended thinking https://platform.claude.com/docs/en/build-with-claude/extended-thinking
- Anthropic, Task budgets https://platform.claude.com/docs/en/build-with-claude/task-budgets
- Anthropic, Advisor tool https://platform.claude.com/docs/en/agents-and-tools/tool-use/advisor-tool ; blog "The advisor strategy" (2026-04-09) https://claude.com/blog/the-advisor-strategy
- Anthropic, Fast mode https://platform.claude.com/docs/en/build-with-claude/fast-mode
- Anthropic, Pricing https://platform.claude.com/docs/en/about-claude/pricing
- Anthropic, Batch processing https://platform.claude.com/docs/en/build-with-claude/batch-processing
- Anthropic, Prompt caching https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- Anthropic, Structured outputs https://platform.claude.com/docs/en/build-with-claude/structured-outputs
- Anthropic, Usage and Cost API https://platform.claude.com/docs/en/manage-claude/usage-cost-api
- Anthropic, Claude Code Analytics API https://platform.claude.com/docs/en/build-with-claude/claude-code-analytics-api
- Claude Code, Manage costs https://code.claude.com/docs/en/costs ; Model config https://code.claude.com/docs/en/model-config ; Subagents https://code.claude.com/docs/en/sub-agents ; Monitoring usage https://code.claude.com/docs/en/monitoring-usage
- Bundled Anthropic skill reference: `claude-api/shared/cost-optimization.md` (skill v2.1.280; local)
- CloudZero, Gemini pricing analysis (2026-07-16, updated 2026-09-04) https://www.cloudzero.com/blog/gemini-pricing/
- OpenAI, Reasoning models https://developers.openai.com/api/docs/guides/reasoning ; Flex processing https://developers.openai.com/api/docs/guides/flex-processing ; GPT-5 params cookbook https://developers.openai.com/cookbook/examples/gpt-5/gpt-5_new_params_and_tools
- Google, Gemini Batch API (updated 2026-09-17) https://ai.google.dev/gemini-api/docs/batch-api
- AWS, Bedrock intelligent prompt routing https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-routing.html ; product page https://aws.amazon.com/bedrock/intelligent-prompt-routing
- Microsoft, Foundry model router (updated 2026-09-10) https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/model-router
- GitHub, Auto model selection routes based on task (2026-05-20) https://github.blog/changelog/2026-05-20-auto-model-selection-now-routes-based-on-your-task-in-vs-code/
- OpenRouter, Auto Router https://openrouter.ai/docs/guides/routing/routers/auto-router
- Aider, architect/editor (2024-09-26) https://aider.chat/2024/09/26/architect.html
- dottxt, Say What You Mean (2024) https://blog.dottxt.ai/say-what-you-mean.html
- Artificial Analysis, methodology https://artificialanalysis.ai/methodology ; Claude Opus 5.5 page https://artificialanalysis.ai/models/releases/claude-opus-5-5

Local code facts (Token Bill v0.1.2, read-only inspection):
- `tokenbill/trace.py` `Usage` has only `input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, and `output_tokens`. `Call` has no effort, thinking, speed, tier, served-model, iterations, or `max_tokens` fields.
- `tokenbill/pricing.py` `PRICING` has no `claude-opus-5-5` entry, prices all cache writes at the 5-minute 1.25x rate, and has no batch, fast-mode, or data-residency modifiers.
