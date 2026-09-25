# Agent context management and its token cost: research report

**Track:** papers-agent-context
**Prepared for:** Token Bill (github.com/sedai77/tokenbill-llm-agent-cost-profiler, v0.1.2)
**Research date:** 2026-09-23
**Method:** I opened every source cited here during this session (arXiv abstract or HTML pages, official docs, engineering blogs). Numbers are quoted as the sources state them. Where a source is a secondary write-up (a blog or a Reddit-derived news post), the finding is marked `weak`. Anthropic API behavior was cross-checked against the bundled `claude-api` skill docs (`shared/cost-optimization.md`, `shared/prompt-caching.md`, `shared/agent-design.md`). When a bundled snapshot disagreed with the live page, I used the live page.

---

## 0. Executive summary

1. **Agent bills are mostly re-read history, and most of that history is tool output.** Observations (tool results) make up about **84%** of an average SWE-agent turn ([Complexity Trap](https://arxiv.org/abs/2508.21433)). Read-type operations account for **76.1%** of tokens in mini-SWE-agent with Claude Sonnet 4.5 ([SWE-Pruner](https://arxiv.org/html/2601.16746)). Input tokens dominate cost *even with caching*, and **cache reads are the single largest cost category** ([How Do AI Agents Spend Your Money?](https://arxiv.org/abs/2604.22750)). Manus reports a **100:1** input-to-output ratio. In a ChatDev replication, **79.7%** of input tokens had already been sent earlier in the same run, and **34.7%** were redundant *and* sat where no prefix cache could reach them.
2. **Cost grows with the square of the turn count.** Every turn resends the whole conversation. OpenHands measured quadratic growth without condensation and linear growth with it, for **up to 2x lower per-turn cost** ([OpenHands, 2025-04-09](https://www.openhands.dev/blog/openhands-context-condensensation-for-more-efficient-ai-agents)).
3. **Simple interventions work, and cutting context rarely hurts success.**
   - Observation masking (keep the last 10 tool outputs) **halves cost** and matches LLM summarization on SWE-bench Verified.
   - LLM summarization makes trajectories **13–15% longer**, and summary calls cost **up to 7.2%** of instance cost.
   - AgentDiet cuts input tokens **40–60%** at equal performance.
   - SWE-Pruner cuts tokens **23–54%** with higher success.
   - SWE-agent's 100-line file window *beat* full-file viewing (18.0% vs 12.7% resolved).
   - Longer context *itself* degrades accuracy by 13.9–85% even with perfect retrieval ([Du et al., EMNLP'25 Findings](https://arxiv.org/abs/2510.05381)). Less context is often better *and* cheaper.
4. **Under prompt caching, token savings do not equal dollar savings.** Anthropic's own measurements show three things:
   - Context editing (clearing tool results) cost **+74%** on short runs and had no measurable effect on long runs, because each clear rewrites the cached prefix.
   - Server-side compaction saved **32%** on long runs and nothing on short runs.
   - Client-side pruning at natural boundaries saved **39%**, because the prefix stays byte-identical between prunes.
   ([Optimizing for cost and intelligence](https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence)). **A tool that estimates context-policy savings without a cache-exact simulator will be wrong.** This is exactly where Token Bill's replay engine gives it an edge.
5. **Isolation pays only when the offloaded work is bulky.**
   - A subagent pattern was **78% cheaper** on Anthropic's cookbook workload.
   - An orchestrator with 25 Sonnet workers was **47–55% cheaper**, but only on work larger than any context window. On harder, single-chain work it lost: the solo model at lower effort was 22–30% cheaper.
   - Multi-agent systems use about **15x** the tokens of chat. Claude Code agent teams use about **7x** a standard session.
6. **Failures and the cost tail drive the bill.**
   - Failed SWE-agent runs used **4x** the tokens of successes ([SWE-Effi](https://arxiv.org/abs/2509.09853)).
   - Runs of the same task vary **up to 30x**, and accuracy peaks at *intermediate* cost.
   - High-cost failures show **50%** repeated actions on the same file.
   - Two of 20 problems carried **43%** of spend.
   - Filtering out "overthinking" trajectories gave **43%** lower cost with about 30% better performance.
7. **Token Bill has measurement gaps it must close before enterprise use.**
   - Server-side compaction usage is reported **only** in `usage.iterations`; the top-level `input_tokens`/`output_tokens` *exclude* it.
   - Agent SDK per-step `output_tokens` is a placeholder, and parallel tool calls share one message ID, so they must be de-duplicated.
   - The SDK's `usage` field excludes subagents.
   - Context-editing clears appear in `context_management.applied_edits`.
   - v0.1.2's `Usage` has only 4 fields and would silently **undercount** compacted runs.
   - It would also misclassify intentional rewrites (compaction, clearing) as "history-rewrite" breakers.

**Bottom line for Token Bill:** the most differentiated product is a **cache-exact, context-policy counterfactual simulator** fed by a **context-anatomy profiler**. It would answer "what would this fleet's bill have been under masking M=10 / tool-output cap K / pruning-at-boundaries / compaction at T / subagent offload of tool X". It would put a literature-backed quality prior on each answer, and then validate the chosen policy with an outcome-joined A/B harness. Nothing in the research literature or the vendor tools does this end-to-end today.

---

## 1. Where agent tokens go (quantified)

| Measurement | Value | Setting | Source (date) |
|---|---|---|---|
| Observation (tool output) share of an average agent turn | ~84% | SWE-agent, SWE-bench Lite-50 | [Complexity Trap, arXiv 2508.21433](https://arxiv.org/html/2508.21433v3) (v3 2025-10-27) |
| Read operations share of total tokens | 76.1% (Sonnet 4.5), 67.5% (GLM-4.6); execute 12.1%, edit 11.8% | mini-SWE-agent, SWE-bench Verified | [SWE-Pruner, arXiv 2601.16746](https://arxiv.org/html/2601.16746) (v4 2026-05-07) |
| Input vs output | Input dominates cost even with caching; cache reads are the largest cost category; agentic coding uses ~1000x more tokens than code chat/reasoning | OpenHands, 8 frontier LLMs, SWE-bench Verified | [Bai et al., arXiv 2604.22750](https://arxiv.org/abs/2604.22750) (2026-04-24, v2 04-29) |
| Input:output ratio | ~100:1; ~50 tool calls per task | Manus production agent | [Manus blog](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus) (2025-07-18) |
| Input share of tokens | 53.9% avg; Code Review phase 59.4% of tokens | ChatDev + GPT-5, 30 tasks | [Tokenomics, arXiv 2601.14470](https://arxiv.org/abs/2601.14470) (2026-01-20) |
| Re-sent input | 79.7% of input tokens were already sent earlier in the run; 34.7% redundant *and* uncacheable (range 23.7–45.9%); input = 75.6% of tokens | ChatDev replication, qwen2.5-coder:7b, 162 calls | [tokenomics-replication (GitHub)](https://github.com/alaaalzibda/tokenomics-replication) (date not shown; `weak`–`moderate`) |
| Token snowball | SWE-agent + GPT-4o-mini: 8.1M input vs 24.4k output tokens | SWE-bench Verified subset | [SWE-Effi, arXiv 2509.09853](https://arxiv.org/html/2509.09853v2) (2025-09-18) |
| Run-to-run variance | up to 30x more tokens on the same task; models self-predict usage poorly (r ≤ 0.39) | OpenHands | [arXiv 2604.22750](https://arxiv.org/abs/2604.22750) |
| Failure premium | failed runs 8.8M tokens vs 1.8M for successes (~4x+) | SWE-agent + GPT-4o-mini | [SWE-Effi](https://arxiv.org/html/2509.09853v2) |
| Enterprise Claude Code spend | ~$13 per developer per active day; $150–250 per developer per month; <$30/active day for 90% of users | Anthropic, enterprise deployments | [Claude Code costs docs](https://code.claude.com/docs/en/costs) (live 2026-09) |
| Tool definitions | 58 tools / 5 MCP servers ≈ 55K tokens before the conversation starts; 134K observed internally | Anthropic | [Advanced tool use](https://www.anthropic.com/engineering/advanced-tool-use) (2025-11-24) |

**Takeaway for Token Bill:** the product must be able to say "X% of your input dollars went to re-reading tool output that was more than N turns old," split by tool name, MCP server, file, and age. Today Token Bill reports only "redundancy" (the re-sent prefix not served from cache). That is necessary, but it does not reach the dominant cost driver: *cached re-reads of stale observations*. Cached re-reads are cheap per token but enormous in volume.

---

## 2. Interventions: what saves money without hurting success

| Intervention | Cost effect | Quality effect | Caveats | Source |
|---|---|---|---|---|
| Observation masking (keep last M=10 tool outputs, replace older ones with placeholders) | −50.9% to −56.1% cost vs raw agent | Qwen3-Coder 480B +2.6% rel., Gemini 2.5 Flash +8.5% rel., Qwen3-32B −11.8% rel. | Window must be re-tuned per scaffold (OpenHands vs SWE-agent) | [Complexity Trap](https://arxiv.org/html/2508.21433v3); [JetBrains blog 2025-12](https://blog.jetbrains.com/research/2025/12/efficient-context-management/) |
| LLM summarization (summarize 21 turns, keep last 10) | −41.5% to −55.4% | similar to masking | Trajectories 13–15% longer; summary calls up to 7.2% of cost; summaries get poor cache reuse | same |
| Hybrid (masking first, summarize as last resort) | further −7% vs masking, −11% vs summary | +2.6 pp on Qwen3-Coder (50-instance subset) | | same |
| SWE-agent history processor (collapse observations older than last 5); 100-line file viewer; ≤50 search results | 100-line window 18.0% resolved vs full file 12.7% | *better* with less context | 2024 models | [SWE-agent, arXiv 2405.15793](https://arxiv.org/html/2405.15793) (v3 2024-11-11) |
| OpenHands LLMSummarizingCondenser | up to 2x lower per-turn cost; quadratic → linear | ~54% vs 53% solved | | [OpenHands blog 2025-04-09](https://www.openhands.dev/blog/openhands-context-condensensation-for-more-efficient-ai-agents); [docs](https://docs.openhands.dev/sdk/guides/context-condenser) |
| AgentDiet trajectory reduction (remove useless / redundant / expired info) | input −39.9% to −59.7%; total cost −21.1% to −35.9% | same performance | Reflection LLM adds cost | [arXiv 2509.23586](https://arxiv.org/abs/2509.23586) (FSE 2026; rev. 2026-03-15) |
| SWE-Pruner (0.6B goal-conditioned skimmer on read outputs) | −23% to −54% tokens; rounds −18% to −26% | Sonnet 4.5 70.6→72.0%; GLM-4.6 55.4→56.6% | Extra model | [arXiv 2601.16746](https://arxiv.org/abs/2601.16746) |
| SWE-Pruner Pro (agent's own hidden states decide what to prune) | up to −39% prompt+completion tokens | +3.8% resolve (MiMo-V2-Flash) | Needs open weights | [arXiv 2607.18213](https://arxiv.org/abs/2607.18213) (2026-07-20) |
| SparseRead (read-gate before evidence enters context) | up to −92.9% tokens; −89% wall time | preserved or better (incl. Claude Opus 5) | New; single paper | [arXiv 2608.22237](https://arxiv.org/abs/2608.22237) (2026-08-23) |
| DTOC (reversible tool-output compression: full output stored externally, placeholder in context) | cost per solved task 3–3.5x lower (Sonnet 4.6, GPT-5.4) | solve rates 2.5x / 1.5x higher | "disable-only" (irreversible) variants degraded performance | [arXiv 2609.26121](https://arxiv.org/abs/2609.26121) (2026-08-06) |
| ACON (optimized compression guidelines, distilled compressor) | peak tokens −26% to −54% | preserved or improved; up to +46% for small models | Latency up (73s → 88–102s); distilled compressor $0.0004 vs $0.045 per example | [arXiv 2510.00615](https://arxiv.org/html/2510.00615) (ICML 2026) |
| Anthropic context editing (`clear_tool_uses_20250919`) | 84% fewer tokens in a 100-turn web-search eval | +29% (alone), +39% (with memory tool) on the internal eval | **In dollars:** +74% cost on short runs, no measurable change on long runs (cache rewrites) | [Context management 2025-09-29](https://claude.com/blog/context-management); [Cost/intelligence guide](https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence) |
| Anthropic server-side compaction | −32% on long runs, 0 on short; cookbook −16% | n/a | Billed in `usage.iterations` | same; [cookbook 2026-08-09](https://platform.claude.com/cookbook/cost-optimization-cost-optimization) |
| Client-side pruning at natural boundaries ("jagged pruning") | −39% on long runs; cookbook −29% | n/a | Keeps the prefix byte-identical between prunes (89% cache reads right after a boundary) | same |
| Subagent for bulky legwork (Haiku) | cookbook −78% | same pass on that workload | Subagent starts cold (no cache shared with parent) | [cookbook](https://platform.claude.com/cookbook/cost-optimization-cost-optimization) |
| Tool Search (`defer_loading`) | −85% tool-definition tokens (~77K → ~8.7K) | Opus 4 49→74%, Opus 4.5 79.5→88.1% | Pays above ~10K schema tokens | [Advanced tool use 2025-11-24](https://www.anthropic.com/engineering/advanced-tool-use) |
| Programmatic tool calling | −37% tokens (43,588 → 27,297) | accuracy up | Needs code execution | same |
| Code execution with MCP | 150K → 2K tokens (−98.7%) in the example | n/a | Illustrative example | [Code execution with MCP 2025-11-04](https://www.anthropic.com/engineering/code-execution-with-mcp) |
| Concise tool response format | 72 vs 206 tokens (~⅓); Claude Code caps tool responses at 25K tokens | n/a | | [Writing tools for agents 2025-09-11](https://www.anthropic.com/engineering/writing-tools-for-agents) |
| Parallel function calling (LLMCompiler) | up to 6.7x cost savings, 3.7x latency | up to ~9% better | ICML 2024; vs ReAct | [arXiv 2312.04511](https://arxiv.org/abs/2312.04511) |
| Filter "overthinking" trajectories | −43% compute cost | ~+30% performance | Selection among samples | [arXiv 2502.08235](https://arxiv.org/abs/2502.08235) (2025-02-12) |
| Mem0 memory vs full context | >90% token-cost savings; −91% p95 latency | +26% relative LLM-judge score vs OpenAI memory | Conversational memory, not coding | [arXiv 2504.19413](https://arxiv.org/abs/2504.19413) (2025-04-28) |
| Agentic plan caching | −50.31% cost, −27.28% latency | maintained | Repetitive workflows | [arXiv 2506.14852](https://arxiv.org/abs/2506.14852) (NeurIPS 2025) |
| Learned folding (Context-Folding / AgentFold / MEM1 / ReSum) | 10x smaller active context; AgentFold ~7K tokens after 100 turns; MEM1 3.7x less memory | matches or beats ReAct | Requires RL-trained models | [2510.11967](https://arxiv.org/abs/2510.11967), [2510.24699](https://arxiv.org/abs/2510.24699), [2506.15841](https://arxiv.org/abs/2506.15841), [2509.13313](https://arxiv.org/abs/2509.13313) |

---

## 3. Findings (detailed)

Each finding lists: claim and numbers, sources (opened), evidence strength, what Token Bill should do, estimated savings, and build effort (S/M/L/XL).

### F1. Tool output is the dominant component of agent context (`ctx-obs-dominance`)
- **Claim:**
  - About 84% of an average SWE-agent turn is environment observation (Complexity Trap).
  - Read-type actions (cat/grep/head) make up 76.1% of total tokens for mini-SWE-agent with Claude Sonnet 4.5, and 67.5% with GLM-4.6. Execute is 12.1% and edit is 11.8% (SWE-Pruner preliminary study).
  - Coding agents initially focus on exploratory reads, then shift to writes and execution ([Agentic AI Workload Characteristics, IISWC 2026](https://arxiv.org/abs/2605.26297)).
- **Sources:** [arXiv 2508.21433 v3 (2025-10-27)](https://arxiv.org/html/2508.21433v3); [arXiv 2601.16746 HTML (v4 2026-05-07)](https://arxiv.org/html/2601.16746); [arXiv 2605.26297](https://arxiv.org/abs/2605.26297)
- **Evidence:** strong (two independent measurements on SWE-bench).
- **Token Bill:**
  - Add a **context-anatomy profiler**. For every call, split billed input into system, tool schemas, user text, assistant text/thinking, `tool_use` inputs, and `tool_result` (by tool name, MCP server, and file path when derivable), and bucket each by *age in turns*.
  - Scale the split proportionally to the billed total, as v0.1.2 already does.
  - Report "tool-output share of input $" and "stale-observation share (results older than 10 turns)".
- **Savings:** this is diagnostic. It frames the 20–60% levers in F4–F8.
- **Effort:** M.

### F2. Input (mostly cached re-reads) dominates cost even with caching; much re-sent context is uncacheable (`ctx-input-dominates`)
- **Claim:**
  - In OpenHands trajectories across 8 frontier LLMs, input tokens, not output tokens, dominate cost even with caching. Cache reads are the largest cost category, despite output being about 80x more expensive per token than a cache read. Agentic coding uses about 1000x more tokens than code chat.
  - Manus reports a 100:1 input:output ratio.
  - In a ChatDev replication, 79.7% of input tokens had already been sent earlier in the run, and 34.7% were redundant but positioned beyond any prefix cache (for example, content re-inserted after a changed segment).
- **Sources:** [arXiv 2604.22750 (2026-04-24)](https://arxiv.org/abs/2604.22750) and [HTML](https://arxiv.org/html/2604.22750); [Manus (2025-07-18)](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus); [tokenomics-replication](https://github.com/alaaalzibda/tokenomics-replication); [Tokenomics arXiv 2601.14470 (2026-01-20)](https://arxiv.org/abs/2601.14470)
- **Evidence:** strong for input dominance; moderate for the 34.7% uncacheable figure (small open-model replication).
- **Token Bill:**
  - Add a **re-read multiplier**: Σ input tokens processed ÷ Σ unique new tokens introduced. Report it per run, repo, and user.
  - Split re-sent tokens into three buckets: cached (cheap), cacheable-but-missed (the breakers Token Bill already finds), and *structurally uncacheable* (re-sent content that sits after a divergence point). The third bucket is the one only context policies (not caching) can fix.
- **Savings:** directs attention to the largest line item; typically ≥50% of spend is re-reads.
- **Effort:** M.

### F3. Cost grows quadratically with turns; condensation makes it linear (`ctx-quadratic-growth`)
- **Claim:**
  - Every turn resends the entire conversation, so a 40-turn task sends its first turn 40 times and task cost grows with roughly the square of the turn count (Anthropic cost-optimization guidance).
  - OpenHands observed quadratic growth without condensation and linear growth with it: per-turn cost settled at less than half of baseline (up to 2x), with a solve rate of about 54% vs 53%.
  - SWE-Effi calls the same effect the "token snowball".
- **Sources:** [OpenHands blog (2025-04-09)](https://www.openhands.dev/blog/openhands-context-condensensation-for-more-efficient-ai-agents); [SWE-Effi (2025-09-18)](https://arxiv.org/html/2509.09853v2); bundled `claude-api/shared/cost-optimization.md` §2.1; [Claude Code costs: "why usage climbs in a long session"](https://code.claude.com/docs/en/costs)
- **Evidence:** strong.
- **Token Bill:**
  - Fit each run's per-call input size against turn index. Flag superlinear runs and sessions that are never cleared.
  - Show "cost of turn k" curves and the projected saving of a cap or condensation at turn T.
  - For Claude Code, detect unrelated tasks run in one session without `/clear`. Claude Code docs name this, along with Opus left as default, as the usual cause of unexpectedly high spend.
- **Savings:** up to 50% on long sessions (OpenHands).
- **Effort:** S–M.

### F4. Observation masking halves cost and matches LLM summarization (`jb-observation-masking`)
- **Claim:** On SWE-bench Verified (500 instances, up to 250 turns), masking all but the last M=10 observations cut cost by 50.9–56.1% vs the raw agent. It matched or beat LLM summarization.
  - Qwen3-Coder 480B: $1.29 → $0.61, 53.4 → 54.8% solved.
  - Gemini 2.5 Flash: $0.41 → $0.18, 32.8 → 35.6%.
  - Qwen3-32B: the one regression, 17.0 → 15.0%.
  - A hybrid (masking first, summary as last resort) cut a further 7% vs masking and 11% vs summary.
  - The window needed re-tuning for OpenHands.
- **Sources:** [arXiv 2508.21433 (v1 2025-08-29, v3 2025-10-27; NeurIPS'25 DL4Code)](https://arxiv.org/abs/2508.21433); [JetBrains Research blog (2025-12)](https://blog.jetbrains.com/research/2025/12/efficient-context-management/); [SWE-agent: collapse observations older than last 5 (arXiv 2405.15793 v3, 2024-11-11)](https://arxiv.org/html/2405.15793)
- **Evidence:** strong (peer-reviewed workshop, 5 model configurations, 2 scaffolds), though not tested on Claude models.
- **Token Bill:**
  - Add a counterfactual replay, "masking(M)": rewrite each call's history with placeholders for tool results older than M turns, then re-run the *cache-exact* simulator.
  - Masking changes history on every turn once the window slides, so naive masking breaks the cache every turn. Also simulate the cache-friendly variant: mask in batches every K turns, as Anthropic's pruning guidance recommends.
  - Report $ saved for M ∈ {5, 10, 20} with the literature quality prior attached.
- **Savings:** up to about 50% of token cost (literature, without caching). Under caching, expect less; the simulator must quantify it.
- **Effort:** M.

### F5. LLM summarization has hidden costs: longer trajectories, summary calls, cache misses (`summary-hidden-costs`)
- **Claim:** In the same study, LLM-Summary made trajectories 15% longer than masking for Gemini 2.5 Flash (52 vs 44 turns), and 15% longer than raw / 13% longer than masking for Qwen3-Coder 480B. Summary generation cost up to 7.2% of instance cost, because each summary call processes a unique sequence and gets little cache reuse beyond the system prompt.
  - ACON trades cost for latency: API cost drops ($0.331 → $0.272–0.285), but latency rises from 73s to 88–102s.
  - Claude Code's `/compact` is itself a large request. It is cheap when the cache is warm and costs the most after a cold break.
- **Sources:** [arXiv 2508.21433 v3](https://arxiv.org/html/2508.21433v3); [ACON HTML](https://arxiv.org/html/2510.00615); [Claude Code prompt caching docs](https://code.claude.com/docs/en/prompt-caching)
- **Evidence:** strong.
- **Token Bill:**
  - Attribute summarization and compaction calls as an explicit "context-management overhead" line item.
  - Measure turns-per-task and cost per task before and after enabling compaction.
  - Flag "cold compaction" (compaction requested after the cache TTL expired) and "compaction thrash" (more than N compactions per task).
- **Savings:** avoids 7–15% regressions from misconfigured summarization.
- **Effort:** S.

### F6. Anthropic server-side compaction is billed outside top-level usage, so Token Bill undercounts today (`anth-compaction-iterations-billing`)
- **Claim:**
  - With threshold compaction (`compact_20260112`, beta `compact-2026-01-12`, default trigger 150K input tokens, minimum 50K) and on-demand compaction (beta `compact-2026-09-04`), the top-level `usage.input_tokens`/`output_tokens` **do not include** the compaction step.
  - Total billed usage must be summed across `usage.iterations[]` (`type: "compaction"` plus `type: "message"`). In the docs' example, top-level usage shows 24K tokens while the billed total is 207.5K.
  - On-demand compaction responses report top-level usage of 0.
  - Re-sending a compaction block adds no cost.
  - Measured value: compaction saved 32% on long runs and nothing on short runs; on the cookbook workload it saved 16%.
- **Sources:** [Compaction at a token threshold](https://platform.claude.com/docs/en/build-with-claude/compaction-threshold); [Compaction on demand](https://platform.claude.com/docs/en/build-with-claude/compaction-on-demand); [Compaction overview](https://platform.claude.com/docs/en/build-with-claude/compaction); [Optimizing for cost and intelligence](https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence); [cookbook (2026-08-09)](https://platform.claude.com/cookbook/cost-optimization-cost-optimization). All live 2026-09.
- **Evidence:** strong (official docs).
- **Token Bill:**
  - **P0 correctness fix:** extend `Usage` to parse `iterations`, `cache_creation.ephemeral_5m_input_tokens`/`ephemeral_1h_input_tokens`, and `server_tool_use`. Also read `stop_reason: "compaction"`.
  - Treat compaction blocks as intentional history replacement, not a "history-rewrite" breaker.
  - Price compaction iterations separately.
- **Savings:** accuracy fix. Without it, compacted runs are under-reported by the full summarization input (often 100K+ tokens per event).
- **Effort:** S.

### F7. Context editing (tool-result clearing) is a context-window tool, not a savings lever, unless it is batched (`anth-context-editing-cache`)
- **Claim:**
  - `clear_tool_uses_20250919` (defaults: trigger 100K input tokens, keep 3 tool uses) invalidates the cached prefix on every clear.
  - Anthropic's measured runs: context editing cost **+74%** on short runs (20 issues) and had no measurable effect on long runs. A 100K-token prefix re-written at 1.25x costs $1.25 vs $0.03 to read on Fable 5.1 (50x), and $0.63 vs $0.05 on Opus 5 (12.5x).
  - The cookbook measured −12%.
  - The launch post reported 84% fewer *tokens* and +29% task performance (+39% with the memory tool) on an internal 100-turn eval. Tokens and dollars diverge under caching.
  - Mitigation: `clear_at_least` (a minimum amount to clear, so each rewrite is worth it) and high triggers, so clears happen in a few large batches.
  - The response reports `context_management.applied_edits[].cleared_input_tokens`.
- **Sources:** [Context editing docs](https://platform.claude.com/docs/en/build-with-claude/context-editing); [Context management launch (2025-09-29)](https://claude.com/blog/context-management); [Optimizing for cost and intelligence](https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence); [cookbook (2026-08-09)](https://platform.claude.com/cookbook/cost-optimization-cost-optimization)
- **Evidence:** strong.
- **Token Bill:**
  - Parse `applied_edits`.
  - Add a detector for "clearing too often". Measure cache-write $ incurred by clears vs input $ saved, per run, and recommend trigger, `keep`, and `clear_at_least` values from the simulator.
  - Reclassify these rewrites as "expected rebuilds", as Claude Code's `/usage` does, instead of accidental breakers.
- **Savings:** avoids a +74% regression; on long runs, tuning can move the result to the −12% to −39% range.
- **Effort:** M.

### F8. Pruning at natural boundaries beats both clearing and compaction under caching (`jagged-pruning`)
- **Claim:**
  - Collapsing bulky tool results to one-line extracts when a work phase completes, while keeping the message array byte-identical between prunes, saved **39%** on long runs. The cache read rate was 89% on the first request after a boundary and 81% between boundaries.
  - About two-thirds of the advantage over context editing comes from *not* rewriting mid-task content; one-third comes from the smaller context.
  - Cookbook: −29%, vs −12% for editing and −16% for compaction.
  - Manus independently recommends reversible compression (keep URLs and paths, drop content) and treating the file system as memory.
- **Sources:** [Optimizing for cost and intelligence](https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence); [cookbook](https://platform.claude.com/cookbook/cost-optimization-cost-optimization); [Manus (2025-07-18)](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus); bundled `cost-optimization.md` §2.3
- **Evidence:** strong (vendor-measured), moderate on generality.
- **Token Bill:**
  - Detect phase boundaries in traces (for example, a test-pass event, a user turn, a file-set switch).
  - Simulate boundary pruning.
  - Ship a reference middleware that prunes at boundaries and preserves byte-identity between prunes.
- **Savings:** about 29–39% on long agent loops.
- **Effort:** M (simulator), L (middleware).

### F9. Sub-agent context isolation: big wins for bulky legwork, losses for dependent chains (`subagent-isolation`)
- **Claim:**
  - **Where it wins:**
    - A subagent pattern (Haiku does the legwork and returns an extract) was **78% cheaper** on the cookbook workload.
    - An orchestrator (Fable 5.1 lead plus 25 Sonnet 5 workers) was **47–55%** cheaper on a 21.6M-token corpus, but 10–12 points less accurate.
    - On an easy search slice it cost about 50% less on average, and about 67% less at p90.
  - **Where it loses:** on the full, harder BrowseComp set, the solo model at lower effort was 22–30% cheaper. Single dependent chains always favored the solo model.
  - **Overhead:**
    - Subagents start cold, with no cache shared with the parent (a fork shares it). They typically return 1–2K-token summaries.
    - Multi-agent research systems use about **15x** the tokens of chat (agents alone about 4x), and token usage explains 80% of BrowseComp performance variance.
    - Claude Code agent teams use about **7x** the tokens of a standard session.
    - Cognition argues that single-threaded agents are more reliable unless full traces are shared.
- **Sources:** [cookbook](https://platform.claude.com/cookbook/cost-optimization-cost-optimization); [Optimizing for cost and intelligence](https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence); [Multi-agent research system (2025-06-13)](https://www.anthropic.com/engineering/multi-agent-research-system); [Effective context engineering (2025-09-29)](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents); [Claude Code subagents](https://code.claude.com/docs/en/sub-agents); [Claude Code costs](https://code.claude.com/docs/en/costs); [Cognition (2025-06-12)](https://cognition.com/blog/dont-build-multi-agents)
- **Evidence:** strong.
- **Token Bill:**
  - Add **run trees**: link subagent calls to parents (Claude Code OTel `query_source=subagent` and `agent.name`, Agent SDK `parent_tool_use_id`, subagent transcripts).
  - Compute the subagent ROI: tokens the subagent consumed vs tokens it kept out of the parent × the parent's remaining turns × the parent's per-token rate.
  - Flag three patterns: subagents spawned for tiny tasks (cold-start > benefit), subagents returning huge reports, and fan-out on dependent chains.
  - Recommend offloading specific heavy tools (test runs, log reads) to a subagent on a cheaper model.
- **Savings:** up to 78% on qualifying workloads; avoids 15x blowups on non-qualifying ones.
- **Effort:** M–L.

### F10. Bound tool outputs at the source: windows, caps, concise formats, hooks (`tool-output-bounding`)
- **Claim:**
  - Claude Code caps tool responses at 25K tokens by default. Concise vs detailed response formats measured 72 vs 206 tokens. Anthropic recommends pagination, filtering, truncation, and helpful truncation messages.
  - SWE-agent's ablation: a 100-line window (18.0% resolved) beat both 30 lines (14.3%) and full-file viewing (12.7%). Summarized search (≤50 results) beat iterative search (18.0% vs 12.0%).
  - Claude Code recommends PreToolUse hooks that filter test output to failures only ("from tens of thousands of tokens to hundreds").
- **Sources:** [Writing tools for agents (2025-09-11)](https://www.anthropic.com/engineering/writing-tools-for-agents); [SWE-agent (arXiv 2405.15793)](https://arxiv.org/html/2405.15793); [Claude Code costs](https://code.claude.com/docs/en/costs)
- **Evidence:** strong.
- **Token Bill:**
  - Add a per-tool output-size distribution (p50/p95/max tokens) and the $ of each tool's outputs *including every subsequent re-read* (output tokens × remaining turns × rate).
  - Add an "oversized-tool-result" detector: results over 10K or 25K tokens, full-file reads, unfiltered test and log output.
  - Generate concrete fixes: a Claude Code hook snippet, a tool-param limit, or `MAX_MCP_OUTPUT_TOKENS`.
  - Add a counterfactual "cap K tokens per tool result".
- **Savings:** commonly 10–40% of input on coding agents (the SWE-Pruner/AgentDiet range); per-tool fixes are near-free.
- **Effort:** S–M.

### F11. Tool-definition bloat and on-demand loading (`tool-schema-bloat`)
- **Claim:**
  - 58 tools across 5 MCP servers ≈ 55K tokens; 134K observed internally at Anthropic.
  - Tool Search cut tool-definition tokens 85% (≈77K → 8.7K) and raised accuracy (Opus 4: 49 → 74%; Opus 4.5: 79.5 → 88.1%).
  - Deferred tools append rather than swap, so the cache is preserved.
  - Claude Code defers MCP tools by default. It warns when subagent descriptions exceed 15K tokens and recommends keeping CLAUDE.md under 200 lines.
  - Tool search pays once schemas pass roughly 10K tokens.
- **Sources:** [Advanced tool use (2025-11-24)](https://www.anthropic.com/engineering/advanced-tool-use); [Claude Code costs](https://code.claude.com/docs/en/costs); [Claude Code subagents](https://code.claude.com/docs/en/sub-agents); [Claude Code prompt caching](https://code.claude.com/docs/en/prompt-caching); bundled `cost-optimization.md` §2.2
- **Evidence:** strong.
- **Token Bill:**
  - Measure tool-schema tokens per call and per MCP server.
  - Flag runs with more than 10K schema tokens and no deferral, `alwaysLoad` servers, and MCP connect/disconnect cache invalidations. (These overlap with the existing "tool-churn" breaker; extend it with the MCP-specific fix text.)
- **Savings:** up to 85% of schema tokens. The share of the bill depends on the prefix; it is large on short, many-conversation workloads.
- **Effort:** S.

### F12. Keep intermediate results out of context: programmatic tool calling and code execution (`ptc-code-exec`)
- **Claim:**
  - Programmatic tool calling reduced tokens by 37% (43,588 → 27,297) with higher accuracy (GAIA 46.5 → 51.2%).
  - Code execution with MCP reduced an illustrative workflow from 150K to 2K tokens (−98.7%), by filtering data in the sandbox rather than passing, for example, a 50K-token transcript through context twice.
  - The cookbook reported that a CSV via Files API plus code execution was 79% cheaper than pasting it.
- **Sources:** [Advanced tool use (2025-11-24)](https://www.anthropic.com/engineering/advanced-tool-use); [Code execution with MCP (2025-11-04)](https://www.anthropic.com/engineering/code-execution-with-mcp); [cookbook](https://platform.claude.com/cookbook/cost-optimization-cost-optimization)
- **Evidence:** strong (vendor), moderate on generality.
- **Token Bill:**
  - Detect **pass-through data**: a large tool result whose content is copied verbatim into a later `tool_use` input (for example, read → write to another system).
  - Detect chains of three or more sequential tool calls whose intermediate outputs are never referenced in later assistant text.
  - Recommend PTC or code execution, with the $ of intermediate tokens × re-reads.
- **Savings:** 37–98% on the affected workflows.
- **Effort:** M.

### F13. Longer context hurts accuracy, so trimming is usually quality-neutral or positive (`context-rot`)
- **Claim:**
  - "Lost in the middle": U-shaped accuracy by position ([Liu et al., TACL 2023](https://arxiv.org/abs/2307.03172)).
  - Chroma tested 18 LLMs: performance grows increasingly unreliable with input length even on simple tasks. On LongMemEval, focused (~300-token) prompts beat full (~113K-token) prompts for every model family, and Claude Sonnet 4 showed the largest gap.
  - Du et al.: even with *perfect retrieval*, performance drops 13.9–85% as input length grows.
  - SWE-agent's full-file ablation reached the same conclusion.
  - Anthropic frames this as a finite "attention budget".
- **Sources:** [arXiv 2307.03172](https://arxiv.org/abs/2307.03172); [Chroma Context Rot (2025-07-14)](https://www.trychroma.com/research/context-rot); [arXiv 2510.05381 (2025-10-06, EMNLP'25 Findings)](https://arxiv.org/abs/2510.05381); [Effective context engineering (2025-09-29)](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
- **Evidence:** strong.
- **Token Bill:**
  - Report the context-length distribution per call (p50/p95/max) and the share of calls above 100K or 150K tokens.
  - With outcome labels, correlate context length with failure. This gives enterprise reviewers the "safety argument" that context policies are not merely cost-cutting.
- **Savings:** indirect. It de-risks F4–F10 recommendations.
- **Effort:** S.

### F14. Failures, loops and the cost tail drive the bill (`expensive-failures-tail`)
- **Claim:**
  - Failed runs consume far more: SWE-agent + GPT-4o-mini failures used 8.8M tokens vs 1.8M for successes.
  - Accuracy peaks at intermediate cost. High-cost failed runs had about 50% repeated actions on the same file. Same-task runs vary up to 30x.
  - Two of 20 problems carried 43% of spend.
  - Selecting lower-"overthinking" trajectories cut compute cost 43% with about 30% better performance.
  - HAL (21,730 rollouts, ~$40K) found that higher reasoning effort *reduced* accuracy in the majority of runs.
  - Coding agents also revise already-passing solutions and distrust provided tests ([Tokenmaxxing, arXiv 2607.22807](https://arxiv.org/abs/2607.22807), 2026-07-24).
- **Sources:** [SWE-Effi](https://arxiv.org/html/2509.09853v2); [arXiv 2604.22750](https://arxiv.org/html/2604.22750); [Optimizing for cost and intelligence](https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence); [arXiv 2502.08235](https://arxiv.org/abs/2502.08235); [HAL arXiv 2510.11977 (2025-10-13)](https://arxiv.org/abs/2510.11977); [arXiv 2607.22807](https://arxiv.org/abs/2607.22807)
- **Evidence:** strong.
- **Token Bill:**
  - Add loop and stall detectors: the same tool plus near-identical args repeated N times, the same file read repeatedly without an intervening edit, edit→revert cycles, edits after tests already passed, and long streaks with no new files touched.
  - Add a runaway-run detector: cost above the fleet p95 with a failed outcome.
  - Show tail analytics: what % of spend comes from the top 5% of runs.
  - Recommend turn caps, task budgets (F17), and escalate-on-failure policies.
- **Savings:** 20–45% (tail trimming plus loop breaking; the overthinking paper reports 43%).
- **Effort:** M.

### F15. Duplicate reads of unchanged files are a large, addressable waste (`duplicate-reads`)
- **Claim:**
  - High-cost failed runs show about 50% repeated actions on the same file (moderate evidence).
  - A community analysis of 21M tokens across Claude Code, Cursor and Codex sessions reported that 42% (8.8M) went to repeated file reads (weak: Reddit-derived, no data release).
  - Claude Code appends a file-changed `<system-reminder>` instead of rewriting earlier reads. Old reads therefore remain in history and are re-billed as cached input every turn.
- **Sources:** [arXiv 2604.22750 HTML](https://arxiv.org/html/2604.22750); [gotcontext.ai (2026-06-22)](https://gotcontext.ai/news/researcher-finds-42-of-coding-agent-tokens-are-wasted-on-repeated-file-reads); [Claude Code prompt caching](https://code.claude.com/docs/en/prompt-caching)
- **Evidence:** moderate (the mechanism is certain; the magnitude is uncertain).
- **Token Bill:**
  - Hash every `tool_result` body, normalized by path and range. Report duplicate-content tokens, the $ of the first read's lingering re-reads, and the $ of the redundant re-reads.
  - Recommend "unchanged since turn N" stubs (a harness feature) or read-caching hooks.
  - This is cheap and exact. It is also a headline enterprise metric: "X% of read tokens were duplicates".
- **Savings:** 5–40% of read tokens (uncertain; must be measured on enterprise traces).
- **Effort:** S.

### F16. Trajectory-reduction and pruning research gives Token Bill a waste taxonomy and savings priors (`trajectory-reduction-taxonomy`)
- **Claim:**
  - AgentDiet classifies waste as useless, redundant, or expired information. It removes 69–77% of processed content, for 39.9–59.7% fewer input tokens and 21.1–35.9% lower total cost at equal performance (FSE 2026).
  - SWE-Pruner: −23% to −54% tokens with higher success.
  - SWE-Pruner Pro: up to −39%, +3.8% resolve.
  - SparseRead: up to −92.9% tokens.
  - DTOC shows reversibility is critical: storing the full output externally with a restorable placeholder kept accuracy, while irreversible dropping degraded it.
- **Sources:** [arXiv 2509.23586](https://arxiv.org/abs/2509.23586); [arXiv 2601.16746](https://arxiv.org/abs/2601.16746); [arXiv 2607.18213](https://arxiv.org/abs/2607.18213); [arXiv 2608.22237](https://arxiv.org/abs/2608.22237); [arXiv 2609.26121](https://arxiv.org/abs/2609.26121)
- **Evidence:** strong (AgentDiet, SWE-Pruner); moderate (newer 2026 preprints).
- **Token Bill:**
  - Adopt the useless/redundant/expired taxonomy for its waste waterfall.
  - Use these papers' ranges as labeled priors on simulated savings.
  - Recommend *reversible* offload (a file path or handle in context) over deletion.
- **Savings:** 20–36% total cost (AgentDiet); up to 3x lower cost per solved task (DTOC).
- **Effort:** M.

### F17. Task budgets and cost-per-task budgeting (`task-budgets`)
- **Claim:**
  - Anthropic task budgets (beta `task-budgets-2026-03-13`; `output_config.task_budget.total`, minimum 20,000) are advisory. The model sees a countdown and paces itself.
  - Measured on SWE-bench Pro (Fable 5.1): a generous budget cut cost per task 44% for about 3 points of pass rate (within noise); the tightest cut 58% for about 6 points. (The bundled skill snapshot has older figures, 18% and 47%; the live page wins.)
  - Changing the budget mid-task invalidates the cache. Budgets that are too small can cause refusal-like early stops.
  - Not available on Claude Code or Cowork surfaces.
  - Anthropic recommends sizing from the p90/p99 of measured per-task tokens.
- **Sources:** [Task budgets docs](https://platform.claude.com/docs/en/build-with-claude/task-budgets); [Optimizing for cost and intelligence](https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence)
- **Evidence:** strong.
- **Token Bill:**
  - Compute per-workload distributions of task tokens, counted the way the budget counts them: *new* tokens per agentic turn, not re-sent payload.
  - Emit a recommended `task_budget.total`.
  - Flag mid-task budget changes as a cache breaker.
- **Savings:** 44–58% cost per task (with a quality trade that must be validated).
- **Effort:** S.

### F18. Memory and state externalization instead of re-deriving context (`memory-externalization`)
- **Claim:**
  - MemGPT/Letta introduced OS-style virtual context tiers.
  - Mem0 reports >90% token-cost savings and 91% lower p95 latency vs full context, with +26% relative judged quality.
  - Anthropic's memory tool plus context editing gave +39% on its internal eval.
  - Sleep-time compute cuts test-time compute about 5x and cost per query 2.5x when preparation is amortized across related queries.
  - Agentic plan caching cuts cost 50.31%.
  - Anthropic's long-running harness uses progress files and git to bridge context windows, and notes that "compaction alone" is insufficient.
  - Claude Code recommends skills and a codebase-overview skill so agents don't spend tokens re-exploring.
- **Sources:** [MemGPT arXiv 2310.08560](https://arxiv.org/abs/2310.08560); [Mem0 arXiv 2504.19413](https://arxiv.org/abs/2504.19413); [context management (2025-09-29)](https://claude.com/blog/context-management); [Sleep-time compute arXiv 2504.13171](https://arxiv.org/abs/2504.13171); [Agentic Plan Caching arXiv 2506.14852](https://arxiv.org/abs/2506.14852); [Effective harnesses (2025-11-26)](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents); [Claude Code costs](https://code.claude.com/docs/en/costs)
- **Evidence:** moderate (the gains are workload-specific and often non-coding).
- **Token Bill:**
  - Add a cross-session **re-exploration detector**: in the same repo, the first N turns of many sessions read the same files or run the same discovery commands.
  - Quantify $/week spent re-deriving the same context and recommend a skill, CLAUDE.md entry, or memory file with a generated draft.
  - Detect repeated identical plans across tasks, a candidate for plan caching.
- **Savings:** workload-dependent; 10–50% of session-start exploration cost.
- **Effort:** M.

### F19. Learned context folding: future-proofing, not a lever for now (`learned-folding`)
- **Claim:**
  - Context-Folding (FoldGRPO) matches or beats ReAct with a 10x smaller active context on deep-research and SWE tasks.
  - AgentFold keeps about 7K tokens after 100 turns and scales to 500 turns.
  - MEM1: 3.5x better performance with 3.7x less memory.
  - ReSum: +4.5% over ReAct training-free, +8.2% with RL.
  - ContextPilot (EMNLP 2026) learns proactive context editing.
  - All of these require RL-trained models.
- **Sources:** [arXiv 2510.11967](https://arxiv.org/abs/2510.11967); [arXiv 2510.24699](https://arxiv.org/abs/2510.24699); [arXiv 2506.15841](https://arxiv.org/abs/2506.15841); [arXiv 2509.13313](https://arxiv.org/abs/2509.13313); [arXiv 2608.28476](https://arxiv.org/abs/2608.28476)
- **Evidence:** moderate (research agents, mostly open models, not Claude).
- **Token Bill:** track "active context size per turn" as a first-class metric, so that when vendors or harnesses adopt folding, Token Bill can prove its effect. No build is needed beyond the F1 metric.
- **Savings:** unknown for Claude-based enterprise fleets.
- **Effort:** S.

### F20. Cost-aware evaluation: report cost per resolved task and Pareto frontiers, not tokens (`cost-aware-eval`)
- **Claim:**
  - "AI Agents That Matter" shows accuracy-only benchmarks yield needlessly costly agents, and that simple baselines can match state of the art at lower cost; it proposes joint cost-accuracy optimization.
  - HAL standardizes cost-aware evaluation (21,730 rollouts; 2.5B tokens released).
  - SWE-Effi defines effectiveness under token, cost and time budgets.
  - Efficient Agents cut cost-of-pass 28.4% ($0.398 → $0.228) at 96.7% of OWL's performance.
  - Agentless solved 32% of SWE-bench Lite at $0.70 per issue with a fixed pipeline.
  - Anthropic's guidance optimizes cost per completed task, not per token.
- **Sources:** [arXiv 2407.01502 (2024-07-01)](https://arxiv.org/abs/2407.01502); [HAL arXiv 2510.11977](https://arxiv.org/abs/2510.11977); [SWE-Effi arXiv 2509.09853](https://arxiv.org/abs/2509.09853); [Efficient Agents arXiv 2508.02694](https://arxiv.org/abs/2508.02694); [Agentless arXiv 2407.01489](https://arxiv.org/abs/2407.01489); bundled `cost-optimization.md`
- **Evidence:** strong.
- **Token Bill:**
  - Add an **outcome join**: accept per-run success labels (tests passed, PR merged, eval pass, or a user-provided label) and make *cost per resolved task* and *cost-of-pass* the headline metrics, with Pareto plots across configs (model × effort × context policy).
  - Every recommendation should be judged by these metrics, which gives enterprise reviewers a defensible quality guardrail.
- **Savings:** prevents "savings" that are actually regressions. This is the enterprise trust anchor.
- **Effort:** M.

### F21. Claude Code fleet telemetry: what Token Bill can ingest today, and the waste patterns Anthropic documents (`claude-code-telemetry-patterns`)
- **Claim:**
  - Claude Code exports OTel metrics `claude_code.token.usage` (type input/output/cacheRead/cacheCreation) and `claude_code.cost.usage`, with attributes `query_source` (main/subagent/auxiliary), `agent.name`, `skill.name`, `plugin.name`, `mcp_server.name`, `mcp_tool.name`, `effort`, and `speed`.
  - It also exports `api_response` events (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `ttft_ms`) and `tool_result` events.
  - Transcripts live in `~/.claude/projects/<project>/<session-id>.jsonl`, in an internal format that changes between versions, with 30-day retention.
  - `/usage` counts a cache miss when a request re-processed more than 5% and at least 2,000 tokens of cacheable content. It separately counts "expected rebuilds" (compaction or tool-result clearing).
  - Documented cost drivers:
    - long uncleared sessions and Opus left as default;
    - cache misses after breaks (5-min TTL on API keys);
    - scheduled tasks, `/loop`, goal check-ins and cross-session messages that resend full context while idle;
    - model switches, including `opusplan` plan-mode toggles and skill-frontmatter models;
    - effort and fast-mode toggles;
    - MCP connects/disconnects when tools aren't deferred;
    - plugin toggles;
    - image accumulation;
    - `/compact` after a cold break;
    - agent teams (~7x).
  - Enterprise average spend is about $13 per developer per active day.
- **Sources:** [Monitoring usage](https://code.claude.com/docs/en/monitoring-usage); [Manage costs](https://code.claude.com/docs/en/costs); [Prompt caching in Claude Code](https://code.claude.com/docs/en/prompt-caching); [Sessions](https://code.claude.com/docs/en/sessions) (all live 2026-09)
- **Evidence:** strong.
- **Token Bill:**
  - Add a **Claude Code adapter** (OTel ingest plus a best-effort transcript parser, version-guarded).
  - Add fleet rollups by team, user, repo, `query_source`, subagent, skill, and MCP server.
  - Add a detector per documented pattern (idle-loop resends, `opusplan` churn, cold `/compact`, non-deferred MCP churn, uncleared multi-task sessions).
  - Generate managed-settings patches: `promptCacheTtl`, `CLAUDE_CODE_SUBAGENT_MODEL`, hook snippets, and a `modelPricing` table so figures match contracted rates.
  - With about $13 per developer per active day across thousands of developers, the product's value is fleet-level attribution plus coaching.
- **Savings:** workload-dependent. Coaching on clear/compact/model choice is cited by Anthropic as the highest-impact habit change.
- **Effort:** L.

### F22. Accounting pitfalls in agent SDK and trace data (`sdk-accounting-pitfalls`)
- **Claim:** In the Claude Agent SDK:
  - Parallel tool calls produce multiple assistant messages that share one ID and identical usage; they must be de-duplicated.
  - Per-step `output_tokens` is a placeholder; real output totals are on the result message.
  - `usage` on the result excludes subagents; `total_cost_usd` and `modelUsage` include them.
  - Resumed sessions carry earlier spend, so summing results double-counts.
  - Crash results may be zeroed.
  - `total_cost_usd` is a client-side estimate.
  - For the 1.1x data-residency rate, responses report `inference_geo: "us"`.
- **Sources:** [Agent SDK cost tracking](https://code.claude.com/docs/en/agent-sdk/cost-tracking) (live 2026-09)
- **Evidence:** strong.
- **Token Bill:**
  - Build an Agent SDK adapter that implements these rules exactly, with golden tests.
  - Reconcile against the Usage and Cost Admin API for authoritative totals.
  - Surface an "unreconciled delta" so enterprise finance trusts the numbers.
- **Savings:** accuracy; avoids 2x+ double counts.
- **Effort:** S–M.

### F23. The cache lookback window and idle gaps create agent-specific misses the simulator must model (`agent-cache-lookback-idle`)
- **Claim:**
  - Anthropic's cache checks at most 20 block positions back from a breakpoint. When a single agent turn appends 20 or more blocks (many tool calls or results), the previous cache entry falls out of the window and there is no hit. The fix is a second breakpoint.
  - Agents pause for tool execution or approval. Client keepalive (a re-send about every 4 minutes against the 5-minute TTL) cuts post-pause request cost up to 12.5x and breaks even after about 46 minutes of idle on Anthropic.
  - "Don't Break the Cache" (500+ agent sessions, 3 providers) found that caching cuts API cost 41–80%. Excluding dynamic tool results from cached blocks was more consistent than naive full-context caching.
- **Sources:** [Prompt caching docs](https://platform.claude.com/docs/en/build-with-claude/prompt-caching); [Keepalive economics arXiv 2607.19214 (2026-07-21)](https://arxiv.org/abs/2607.19214); [Don't Break the Cache arXiv 2601.06007](https://arxiv.org/abs/2601.06007)
- **Evidence:** strong (docs), moderate (papers).
- **Token Bill:**
  - The v0.1.2 simulator assumes one breakpoint at end-of-messages and no lookback limit. Add the 20-block lookback, so "as-billed vs simulated" validation holds on tool-heavy turns.
  - Add a "lookback-overflow" breaker with a two-breakpoint fix.
  - Add an idle-gap analysis: the $ of misses after gaps longer than the TTL, with a recommendation of 1-hour TTL vs keepalive.
- **Savings:** misses after idle can be 12.5x more expensive than hits. Fleet impact is workload-dependent.
- **Effort:** S–M.

### F24. Parallelizing independent tool calls reduces turns and therefore re-reads (`parallel-tool-calls`)
- **Claim:**
  - LLMCompiler (ICML 2024): parallel function calling gave up to 6.7x cost savings and 3.7x latency speedup vs ReAct, with up to ~9% better accuracy.
  - Anthropic's research system cut research time up to 90% through parallel subagents and tool calls.
  - Each avoided turn removes one full re-read of the context.
- **Sources:** [arXiv 2312.04511](https://arxiv.org/abs/2312.04511); [Multi-agent research system (2025-06-13)](https://www.anthropic.com/engineering/multi-agent-research-system)
- **Evidence:** moderate (2024 baseline models).
- **Token Bill:**
  - Detect runs of sequential single-tool turns with no data dependency (for example, read A, then read B, then read C).
  - Estimate the $ saved if they had been batched into one turn (removed turns × context size × read rate). Note the lookback caveat from F23 when a turn gets large.
- **Savings:** 5–30% on exploration-heavy runs (estimate; `weak` without enterprise data).
- **Effort:** S.

### F25. Monitoring agents with LLMs: compiled state beats raw traces (`observer-compiled-views`)
- **Claim:** For LLM observers of long-horizon agents, a compiled, typed run-state view used about 14–15x fewer input tokens than raw traces, cost 5–7x less, and raised accuracy from 0.48 to 0.85–0.87 ([Parsing the Stream, arXiv 2609.01466, 2026-09-01](https://arxiv.org/abs/2609.01466)). HAL similarly used LLM-aided log inspection to find failures.
- **Sources:** [arXiv 2609.01466](https://arxiv.org/abs/2609.01466); [HAL arXiv 2510.11977](https://arxiv.org/abs/2510.11977)
- **Evidence:** moderate (single recent preprint).
- **Token Bill:** if Token Bill adds an optional "explain this run" LLM feature, feed it a compiled run summary (waterfall, anatomy, detected loops), never raw traces. Otherwise the cost tool itself becomes a cost center.
- **Savings:** keeps Token Bill's own analysis cost to cents per run.
- **Effort:** S.

### F26. Cross-provider context management is converging on server-side compaction (`provider-neutral-compaction`)
- **Claim:**
  - OpenAI's Responses API offers server-side compaction (`context_management` with `compact_threshold`) and a standalone `/responses/compact` endpoint. It returns an opaque, encrypted compaction item that must not be pruned.
  - Anthropic offers threshold and on-demand compaction with signed blocks.
  - Anthropic's docs say compaction replaces older turns with a summary. They warn that images, documents and fetched URLs inside the summarized range are gone.
- **Sources:** [OpenAI Compaction guide](https://developers.openai.com/api/docs/guides/compaction) (live 2026-09); [Anthropic compaction overview](https://platform.claude.com/docs/en/build-with-claude/compaction)
- **Evidence:** strong (docs). OpenAI's docs do not describe compaction billing, so the gap must be verified.
- **Token Bill:** normalize a provider-neutral event model covering `compaction`, `context_edit`, `prune`, `subagent_spawn`, and `cache_write`/`cache_read`, so policies and savings compare across providers and coding agents.
- **Savings:** enables multi-provider enterprise coverage.
- **Effort:** M.

---

## 4. What Token Bill should build

The priorities below follow from the evidence above. They assume the v0.1.2 architecture: JSONL traces, per-call waterfalls from billed usage, the cache replay simulator, and 5 breakers.

### P0: correctness and trust (weeks)
1. **Usage schema v2 (F6, F7, F22).** Parse:
   - `usage.iterations[]` (compaction; the top-level fields exclude it);
   - `cache_creation.ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens` (the 1.25x vs 2x write price);
   - `server_tool_use`, `service_tier`, `inference_geo` (1.1x), and `speed`;
   - `context_management.applied_edits[].cleared_input_tokens`;
   - `stop_reason` values `compaction` and `max_tokens`.
   Add golden tests built from the documented examples: the 207,500-token compaction example must price correctly.
2. **"Intentional vs accidental" rewrites (F7, F21).** Classify compaction blocks, context-editing clears, and client pruning as *expected rebuilds*, as Claude Code does, not "history-rewrite" breakers. Net their rewrite cost against their savings.
3. **Simulator fidelity (F23).** Add the 20-block lookback, the 5-minute vs 1-hour TTL per write, and keepalive modeling. Keep the existing "as-billed vs simulated cache reads" agreement check, and publish it per run as the trust metric.
4. **Adapters with exact accounting rules (F21, F22):**
   - Claude Code OTel (`api_response`, `tool_result`, metrics with `query_source`/`agent.name`/`mcp_server.name`);
   - a Claude Code transcript parser (version-guarded; the docs say the format is internal);
   - Agent SDK streams (ID de-dup, output from the result, subagents via `modelUsage`);
   - Anthropic Admin Usage/Cost API reconciliation.
5. **Privacy by construction.** Transcripts contain source code. Analysis stays local-first: hash tool-result bodies for de-dup, store only sizes, hashes, and tool/file identifiers in fleet rollups, and make redaction the default.

### P1: the context-anatomy profiler (the diagnosis)
6. **Per-call anatomy (F1, F2, F13).** Attribute input $ to system, tool schemas, user, assistant, thinking, and tool_result (by tool, MCP server, file), crossed with *age in turns*. Metrics:
   - re-read multiplier;
   - tool-output share;
   - stale-observation share (older than M turns);
   - p50/p95 context length;
   - active-context curve;
   - superlinear-growth flag.
7. **New detectors ("context breakers").** Each comes with fix text and $ recovered:
   - `duplicate-read` (F15);
   - `oversized-tool-result` by tool, with p95 (F10);
   - `stale-observation-accumulation` (F4);
   - `tool-schema-bloat` / `mcp-not-deferred` (F11);
   - `pass-through-data` / PTC candidate (F12);
   - `loop` / `repeat-action` / `edit-after-pass` (F14);
   - `runaway-run` (F14);
   - `clear-too-often` / `missing-clear_at_least` (F7);
   - `cold-compaction` / `compaction-thrash` (F5);
   - `lookback-overflow` (F23);
   - `idle-gap-miss` (F23);
   - `subagent-cold-start` / `subagent-oversized-return` / `fanout-on-dependent-chain` (F9);
   - `serial-independent-tools` (F24);
   - `uncleared-multi-task-session`, `idle-loop-resend`, `opusplan-churn` (F21);
   - `budget-mutation` (F17);
   - `cross-session-re-exploration` (F18).
8. **Run trees (F9).** Link parents and subagents, and roll up cost per task across the tree. Compute subagent ROI.

### P1/P2: the counterfactual context-policy simulator (the moat)
9. **Policy × cache replay.** Rewrite each trace under a policy, then price it with the cache-exact engine:
   - masking(M) — both a naive per-turn variant and a batch-every-K cache-friendly variant;
   - tool-result cap(K) with a reversible stub;
   - Anthropic `clear_tool_uses` (trigger, keep, clear_at_least, exclude_tools);
   - threshold compaction (trigger; summary ratio as a parameter; compaction iteration cost);
   - boundary pruning;
   - subagent offload of tool X (cold prefix plus return size);
   - duplicate-read stubs;
   - parallel batching.
   Output: $ per task under each policy, labeled **"token-cost counterfactual; quality impact not simulated"**. Attach the literature prior for quality: for example, masking M=10 was quality-neutral on SWE-bench Verified across 5 configurations (with one regression on Qwen3-32B); summarization adds 13–15% turns, applied as a sensitivity band.
10. **Pareto view (F20).** Cost per resolved task vs success across model × effort × policy configurations, once outcome labels exist.

### P2: closed loop and enterprise rollout
11. **Outcome join and A/B harness (F20).** Accept outcome labels (CI results, PR merged, eval pass). Run policy variants on a frozen eval set, with Anthropic's bar of ~50 cases × 5 trials for cutover decisions. Keep or revert automatically; shadow-run before cutover.
12. **Fleet dashboards and alerts (F21).**
    - Spend and waste by org, team, repo, developer, tool, MCP server, skill, and subagent.
    - A cache-hit-rate SLO with alerting (the Claude Code team treats hit-rate drops as SEVs).
    - p95 cost-per-task alerts and top-N runaway runs.
    - Coaching nudges: clear between tasks, compact at natural breaks, and match the model to the job.
13. **Remediation packs (config-as-code).**
    - Claude Code managed-settings patches: `promptCacheTtl`, `subagentPromptCacheTtl`, `CLAUDE_CODE_SUBAGENT_MODEL`, `modelPricing`, and MCP deferral.
    - PreToolUse hook snippets that filter test and log output.
    - API parameter patches: `context_management` edits with tuned trigger/keep/`clear_at_least`, compaction instructions, `task_budget.total` from p90/p99, and a two-breakpoint layout.
    - Each patch carries its simulated $ impact and the evidence link.
14. **Optional middleware (L).** A reference SDK wrapper implementing cache-friendly masking and boundary pruning, reversible tool-output offload (DTOC-style), duplicate-read stubs, and keepalive. Ship it measured by Token Bill itself.

### Metrics Token Bill should make standard
- Cost per resolved task; cost-of-pass.
- Re-read multiplier.
- Tool-output share of input $.
- Stale-observation share.
- Duplicate-read share.
- p95 context length.
- Cache hit rate (main vs subagent).
- Expected-rebuild vs accidental-miss $.
- Context-management overhead $ (compaction plus summaries).
- Subagent ROI.
- Tail concentration (top 5% of runs as a % of $).
- Loop rate.

---

## 5. Open questions and risks
- **No Claude-model evidence for masking.** The masking/summary quality results are on Qwen and Gemini with SWE-agent/OpenHands. Claude Code already clears old tool results and auto-compacts (thresholds not public). Token Bill must measure the residual opportunity on real enterprise transcripts before promising numbers.
- **Unpublished internals.** Claude Code's own tool-result clearing rules and compaction summary sizes are not published; the context-window page uses illustrative numbers only. The simulator needs these as parameters, calibrated from observed traces.
- **Transcript format stability.** Claude Code transcripts use an internal, version-changing format. OTel may be the only stable enterprise ingestion path. Confirm which attributes are available under enterprise privacy settings (for example, `OTEL_LOG_TOOL_DETAILS` / `OTEL_LOG_TOOL_CONTENT`).
- **Outcome labels.** What counts as "resolved" in an enterprise coding fleet (PR merged? CI green? accepted edits via `code_edit_tool.decision`?). Without labels, all tradeoff levers stay proposals.
- **Weak evidence to verify.** The 42% duplicate-read figure (Reddit-derived) and the 34.7% uncacheable figure (small ChatDev replication) must be re-measured on enterprise data.
- **Snapshot vs live disagreement.** The bundled skill snapshot and the live Anthropic page disagree on task-budget and caching-factor numbers (18%/47% vs 44%/58%; 2.5–3.7x vs 2.7–5.3x). The report uses the live page; figures should be re-fetched at build time.
- **OpenAI compaction billing.** OpenAI's compaction docs do not describe how compaction tokens are billed or reported. Verify before multi-provider normalization.
- **Quality under summarization.** Quality effects of summarization vary by scaffold (the masking window had to be re-tuned for OpenHands). Any recommended M or trigger should be treated as a starting point for the A/B harness, not a default.

---

## 6. Source list (all opened 2026-09-23)
- Lindenbauer et al., *The Complexity Trap* — arXiv 2508.21433 (v1 2025-08-29; v3 2025-10-27) — https://arxiv.org/abs/2508.21433 ; HTML https://arxiv.org/html/2508.21433v3
- JetBrains Research blog, *Cutting Through the Noise* (2025-12) — https://blog.jetbrains.com/research/2025/12/efficient-context-management/
- Kang et al., *ACON* — arXiv 2510.00615 (2025-10-01; rev. 2026-06-01, ICML 2026) — https://arxiv.org/abs/2510.00615
- Sun et al., *Context-Folding* — arXiv 2510.11967 (2025-10-13) — https://arxiv.org/abs/2510.11967
- *AgentFold* — arXiv 2510.24699 (2025-10-28) — https://arxiv.org/abs/2510.24699
- *MEM1* — arXiv 2506.15841 (2025-06-18) — https://arxiv.org/abs/2506.15841
- *ReSum* — arXiv 2509.13313 — https://arxiv.org/abs/2509.13313
- *ContextPilot* — arXiv 2608.28476 (2026-08-28, EMNLP 2026) — https://arxiv.org/abs/2608.28476
- Packer et al., *MemGPT* — arXiv 2310.08560 (2023-10-12) — https://arxiv.org/abs/2310.08560
- *Mem0* — arXiv 2504.19413 (2025-04-28) — https://arxiv.org/abs/2504.19413
- *Sleep-time Compute* — arXiv 2504.13171 (2025-04-17) — https://arxiv.org/abs/2504.13171
- *Agentic Plan Caching* — arXiv 2506.14852 (2025-06-17; NeurIPS 2025) — https://arxiv.org/abs/2506.14852
- Zhang, Kraska, Khattab, *Recursive Language Models* — arXiv 2512.24601 (2025-12-31) — https://arxiv.org/abs/2512.24601 (context only; not a finding)
- Liu et al., *Lost in the Middle* — arXiv 2307.03172 (TACL) — https://arxiv.org/abs/2307.03172
- Hong, Troynikov, Huber, *Context Rot* (Chroma, 2025-07-14) — https://www.trychroma.com/research/context-rot
- Du et al., *Context Length Alone Hurts LLM Performance…* — arXiv 2510.05381 (2025-10-06) — https://arxiv.org/abs/2510.05381
- Kapoor et al., *AI Agents That Matter* — arXiv 2407.01502 (2024-07-01) — https://arxiv.org/abs/2407.01502
- Kapoor et al., *Holistic Agent Leaderboard* — arXiv 2510.11977 (2025-10-13) — https://arxiv.org/abs/2510.11977
- *SWE-Effi* — arXiv 2509.09853 (2025-09-18) — https://arxiv.org/abs/2509.09853
- Yang et al., *SWE-agent* — arXiv 2405.15793 (v3 2024-11-11) — https://arxiv.org/html/2405.15793
- Xia et al., *Agentless* — arXiv 2407.01489 (2024-07-01) — https://arxiv.org/abs/2407.01489
- Wang et al., *Efficient Agents* — arXiv 2508.02694 (2025-07-24) — https://arxiv.org/abs/2508.02694
- Cuadron et al., *The Danger of Overthinking* — arXiv 2502.08235 (2025-02-12) — https://arxiv.org/abs/2502.08235
- Salim et al., *Tokenomics* — arXiv 2601.14470 (2026-01-20) — https://arxiv.org/abs/2601.14470
- tokenomics-replication (GitHub) — https://github.com/alaaalzibda/tokenomics-replication
- Bai et al., *How Do AI Agents Spend Your Money?* — arXiv 2604.22750 (2026-04-24) — https://arxiv.org/abs/2604.22750
- Xiao et al., *AgentDiet / Reducing Cost of LLM Agents with Trajectory Reduction* — arXiv 2509.23586 (FSE 2026) — https://arxiv.org/abs/2509.23586
- Wang et al., *SWE-Pruner* — arXiv 2601.16746 (2026-01-23; v4 2026-05-07) — https://arxiv.org/abs/2601.16746
- *SWE-Pruner Pro* — arXiv 2607.18213 (2026-07-20) — https://arxiv.org/abs/2607.18213
- *SparseRead / Read Less, Solve More* — arXiv 2608.22237 (2026-08-23) — https://arxiv.org/abs/2608.22237
- *DTOC* — arXiv 2609.26121 (2026-08-06) — https://arxiv.org/abs/2609.26121
- *Agentic AI Workload Characteristics* — arXiv 2605.26297 (IISWC 2026) — https://arxiv.org/abs/2605.26297
- *Measure Before You Manage* — arXiv 2608.31057 (2026-08-31) — https://arxiv.org/abs/2608.31057
- *The Best Programming Language for Tokenmaxxing* — arXiv 2607.22807 (2026-07-24) — https://arxiv.org/abs/2607.22807
- *Cost-Utility Alignment in LLM Agent Trajectories* — arXiv 2608.26195 (2026-08-25) — https://arxiv.org/abs/2608.26195 (framework; no numbers)
- *Token Economics for LLM Agents* (survey) — arXiv 2605.09104 (2026-05-09) — https://arxiv.org/abs/2605.09104
- *The Long-Horizon Task Mirage?* — arXiv 2604.11978 (2026-04-13) — https://arxiv.org/abs/2604.11978
- *Parsing the Stream* — arXiv 2609.01466 (2026-09-01) — https://arxiv.org/abs/2609.01466
- *Don't Break the Cache* — arXiv 2601.06007 — https://arxiv.org/abs/2601.06007
- Khailo, *Keeping the Cache Warm Pays* — arXiv 2607.19214 (2026-07-21) — https://arxiv.org/abs/2607.19214
- Kim et al., *LLMCompiler* — arXiv 2312.04511 (ICML 2024) — https://arxiv.org/abs/2312.04511
- Anthropic, *Effective context engineering for AI agents* (2025-09-29) — https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
- Anthropic, *Managing context on the Claude Developer Platform* (2025-09-29) — https://claude.com/blog/context-management
- Anthropic, *How we built our multi-agent research system* (2025-06-13) — https://www.anthropic.com/engineering/multi-agent-research-system
- Anthropic, *Writing effective tools for agents* (2025-09-11) — https://www.anthropic.com/engineering/writing-tools-for-agents
- Anthropic, *Introducing advanced tool use* (2025-11-24) — https://www.anthropic.com/engineering/advanced-tool-use
- Anthropic, *Code execution with MCP* (2025-11-04) — https://www.anthropic.com/engineering/code-execution-with-mcp
- Anthropic, *Effective harnesses for long-running agents* (2025-11-26) — https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents
- Claude blog, *Lessons from building Claude Code: prompt caching is everything* (2026-04-30) — https://claude.dev/blog/lessons-from-building-claude-code-prompt-caching-is-everything/
- Claude docs (live 2026-09):
  - Context editing — https://platform.claude.com/docs/en/build-with-claude/context-editing
  - Compaction — https://platform.claude.com/docs/en/build-with-claude/compaction
  - Compaction at a token threshold — https://platform.claude.com/docs/en/build-with-claude/compaction-threshold
  - Compaction on demand — https://platform.claude.com/docs/en/build-with-claude/compaction-on-demand
  - Task budgets — https://platform.claude.com/docs/en/build-with-claude/task-budgets
  - Prompt caching — https://platform.claude.com/docs/en/build-with-claude/prompt-caching
  - Optimizing for cost and intelligence — https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence
  - Cookbook: Cost optimization (2026-08-09) — https://platform.claude.com/cookbook/cost-optimization-cost-optimization
- Claude Code docs (live 2026-09):
  - Costs — https://code.claude.com/docs/en/costs
  - Prompt caching — https://code.claude.com/docs/en/prompt-caching
  - Monitoring usage — https://code.claude.com/docs/en/monitoring-usage
  - Subagents — https://code.claude.com/docs/en/sub-agents
  - Sessions — https://code.claude.com/docs/en/sessions
  - Context window — https://code.claude.com/docs/en/context-window
  - Agent SDK cost tracking — https://code.claude.com/docs/en/agent-sdk/cost-tracking
- OpenHands, *Context condensation* (2025-04-09) — https://www.openhands.dev/blog/openhands-context-condensensation-for-more-efficient-ai-agents ; SDK docs https://docs.openhands.dev/sdk/guides/context-condenser
- Manus, *Context Engineering for AI Agents* (2025-07-18) — https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus
- Cognition, *Don't Build Multi-Agents* (2025-06-12) — https://cognition.com/blog/dont-build-multi-agents
- OpenAI, *Compaction* guide (live 2026-09) — https://developers.openai.com/api/docs/guides/compaction
- gotcontext.ai news post (2026-06-22; weak) — https://gotcontext.ai/news/researcher-finds-42-of-coding-agent-tokens-are-wasted-on-repeated-file-reads
