# Verification notes: papers-agent-context

Verifier: adversarial fact-check pass, 2026-09-23. I opened every cited source with WebFetch and re-checked the Token Bill code locally. Verdicts: 20 confirmed, 6 corrected, 0 unverifiable, 0 refuted.

## Summary table

| id | verdict | key issue |
|---|---|---|
| ctx-obs-dominance | confirmed | The 84% figure comes from SWE-bench Lite-50 (App. D.4) |
| ctx-input-dominates | confirmed | |
| ctx-quadratic-growth | confirmed | |
| jb-observation-masking | confirmed | |
| summary-hidden-costs | confirmed | |
| anth-compaction-iterations-billing | confirmed | Code check: `Usage` has only 4 fields and no `iterations` |
| anth-context-editing-cache | confirmed | |
| jagged-pruning | confirmed | "Two-thirds" is of the gap versus context editing |
| subagent-isolation | **corrected** | The 22-30% BrowseComp result is not described as "lower effort, single-chain" |
| tool-output-bounding | confirmed | |
| tool-schema-bloat | confirmed | |
| ptc-code-exec | confirmed | |
| context-rot | confirmed | |
| expensive-failures-tail | **corrected** | The ~50% repeat figure is about higher-cost *models* on the shared *success* subset, not failed runs |
| duplicate-reads | **corrected** | Same mis-attribution of the ~50% figure |
| trajectory-reduction-taxonomy | confirmed | DTOC results are mixed on other models |
| task-budgets | confirmed | |
| memory-externalization | confirmed | |
| learned-folding | **corrected** | AgentFold uses SFT, not RL; ReSum's +4.5% needs no training |
| cost-aware-eval | confirmed | |
| claude-code-telemetry-patterns | confirmed | Nuance on effort changes |
| sdk-accounting-pitfalls | confirmed | |
| agent-cache-lookback-idle | **corrected** | The lookback counts *positions*; consecutive tool_use or tool_result runs count as one |
| parallel-tool-calls | confirmed | |
| observer-compiled-views | confirmed | |
| provider-neutral-compaction | confirmed | The image/document/URL-loss warning is on the on-demand page, not the overview |

## Per-finding notes

### ctx-obs-dominance: confirmed
- Complexity Trap (arXiv 2508.21433, v3 2025-10-27) says "observation tokens make up around 84% of an average SWE-agent turn". This is measured on SWE-bench Lite-50 (App. D.4).
- SWE-Pruner (arXiv 2601.16746, v1 2026-01-23, v4 2026-05-07) gives read shares of 76.1% (Sonnet 4.5) and 67.5% (GLM-4.6).
  - For Sonnet, execute is 12.1% and edit is 11.8%.
  - For GLM, execute is 14.0% and edit is 18.5%, so "about 12% each" holds only for Sonnet.
- arXiv 2605.26297, "Agentic AI Workload Characteristics" (Yuan, Nayak, Kundu, Talati; IISWC 2026; v1 2026-05-25), confirms agents shift from read/explore behavior early to execute/write behavior later.

### ctx-input-dominates: confirmed
- Bai et al., arXiv 2604.22750 (v1 2026-04-24, v2 2026-04-29), OpenHands, 8 frontier LLMs, SWE-bench Verified. The paper states:
  - agents use "1000x more tokens than code reasoning and code chat";
  - input, not output, drives cost;
  - "cache-read input tokens are the largest category by a wide margin";
  - output is about 80x the per-token price of a cache read.
- The Manus blog (2025-07-18) gives an input:output ratio of about 100:1 and about 50 tool calls per task.
- The tokenomics-replication README (GitHub) reports:
  - 79.7% of input already sent earlier in the run;
  - 34.7% redundant and uncacheable (range 23.7-45.9%);
  - input at 75.6% of tokens;
  - setup: qwen2.5-coder:7b, 10 of 30 tasks, 162 calls.
- Tokenomics (arXiv 2601.14470, 2026-01-20) reports input at 53.9% and Code Review at 59.4%.

### ctx-quadratic-growth: confirmed
- The OpenHands blog (2025-04-09) describes quadratic per-turn cost growth for the baseline and linear growth with condensation. Per-turn cost drops to "less than half" of baseline, with about 54% vs 53% solved.
- SWE-Effi (arXiv 2509.09853v2, 2025-09-18) defines the "token snowball" as monotonic prompt growth.
- The Claude Code costs page says the full conversation is sent with every request.
- "Roughly the square of the turn count" is a derivation from these facts, not a quoted number. It is reasonable.

### jb-observation-masking: confirmed
- Complexity Trap v3 reports masking savings of 50.9% to 56.1% across configurations (57.1% for Gemini Flash thinking).
- Per-model results:
  - Qwen3-Coder 480B: $1.29 → $0.61, 53.4 → 54.8% solved.
  - Gemini 2.5 Flash: $0.41 → $0.18, 32.8 → 35.6%.
  - Qwen3-32B: 17.0 → 15.0% (cost $1.12 → $0.55).
- The hybrid saved 7% more than masking and 11% more than summarization.
- The JetBrains blog (Dec 2025) confirms the masking window had to be tuned per scaffold (OpenHands).
- SWE-agent v3 (2024-11-11) collapses observations before the last 5 turns.

### summary-hidden-costs: confirmed
- Complexity Trap v3 reports trajectories 13-15% longer, and 52 vs 44 turns on Gemini 2.5 Flash.
- It says the direct cost of summaries is "up to 7.2%", and that cache reuse is limited "to the LLM-Summary system prompt".
- ACON (arXiv 2510.00615; v1 2025-10-01, v3 2026-06-01; ICML 2026) on AppWorld:
  - cost $0.331 → $0.285 (history compression) or $0.272 (observation compression);
  - latency 73.24s → 87.68s or 101.92s, measured on a single A100.
- Claude Code's prompt-caching page says a cold `/compact` reprocesses the full history as uncached input and "costs the most" after a break.

### anth-compaction-iterations-billing: confirmed
- The compaction-threshold page confirms:
  - strategy `compact_20260112`, beta `compact-2026-01-12`;
  - default trigger 150,000 input tokens, minimum 50,000;
  - top-level `input_tokens`/`output_tokens` "exclude compaction iteration usage";
  - the example shows 23K input + 1K output at top level against a 207,500-token total;
  - re-sending a compaction block adds no compaction cost.
- The on-demand page (beta `compact-2026-09-04`) confirms the top-level input and output are 0 and the call is reported in `usage.iterations`.
- The optimizing guide gives 32% saved on the long run and nothing on the 20-issue run. The cookbook (2026-08-09) gives 16%.
- Code check: `tokenbill/trace.py` `Usage` has only input, cache_read, cache_creation and output. `instrument.py` `_USAGE_FIELDS` records only those 4. No code reads `iterations`, so the undercount claim holds.

### anth-context-editing-cache: confirmed
- The context-editing docs confirm:
  - `clear_tool_uses_20250919`, default trigger 100,000 input tokens, keep 3;
  - `clear_at_least` exists;
  - clearing "invalidates cached prompt prefixes";
  - `applied_edits[].cleared_input_tokens` is reported.
- The optimizing guide confirms:
  - +74% on the 20-issue run and no change on the long run;
  - $1.25 vs $0.03 on a 100K prefix (Fable 5.1 / Mythos 5.1), and $0.63 vs $0.05 on Opus 5.
- The cookbook gives -12%.
- The launch post (2025-09-29) reports 84% fewer tokens in a 100-turn web-search eval, +29% alone and +39% with the memory tool. These percentages are task-performance gains, not cost.

### jagged-pruning: confirmed
- The optimizing guide confirms three results:
  - the prune saved 39% on the long run;
  - cache reads were 89% on the first request after a boundary and 81% between boundaries;
  - "context editing rewrites content mid-task that the prune deletes (about two thirds of the gap) and ... keeps the context about half the size (the other third)".
- Strictly, "two-thirds" refers to the gap versus context editing. Context editing changed nothing on the long run, so that gap is effectively the prune's whole gain, and the claim holds.
- The cookbook gives -29% for pruning, -12% for editing and -16% for compaction.
- Manus recommends restorable compression: keep the URL or path and drop the content.

### subagent-isolation: corrected
- **Confirmed:**
  - The cookbook subagent on Haiku was 78% cheaper, on one bulky 5,000-line ledger result.
  - Fable 5.1 lead plus 25 Sonnet 5 workers was 47-55% cheaper and scored 10-12 points lower, on a 21.6M-token corpus "too large for any context window". The guide says the orchestrator wins on reading cost only when no single context can hold the work.
  - The multi-agent research post (2025-06-13) gives about 15x the tokens of chat (agents alone about 4x).
  - Claude Code subagents start with a fresh context, and the prompt-caching page says their first request doesn't read the parent's cache.
  - Cognition (2025-06-12) makes the single-thread argument.
- **Correction 1:** The guide's full-BrowseComp result is "Claude Fable 5 alone reached the coordinator configuration's accuracy at 22% to 30% lower cost." The guide does not say that run used lower effort. "Single dependent chain" and "same model at lower effort was cheaper every time" are separate general statements.
- **Correction 2:** The ~7x figure for Claude Code agent teams applies "when teammates run in plan mode".
- **Corrected claim:** On the full, harder BrowseComp set, a solo Fable 5 matched the coordinator's accuracy at 22-30% lower cost. Separately, the guide says single dependent chains, and work that fits in one context, favor a single model.

### tool-output-bounding: confirmed
- Writing tools for agents (2025-09-11) says "For Claude Code, we restrict tool responses to 25,000 tokens by default", and gives 72 vs 206 tokens for concise vs detailed formats.
- SWE-agent v3 ablation:
  - file window: 100 lines 18.0%, 30 lines 14.3%, full file 12.7%;
  - search: summarized 18.0% vs iterative 12.0%.
- The Claude Code costs page has a PreToolUse hook example that filters test output to failures, and describes logs going "from tens of thousands of tokens to hundreds".

### tool-schema-bloat: confirmed
- Advanced tool use (2025-11-24) confirms:
  - 58 tools across 5 servers (GitHub, Slack, Sentry, Grafana, Splunk) ≈ 55K tokens;
  - 134K tokens observed internally;
  - ~77K → ~8.7K tokens, an 85% cut;
  - Opus 4 49 → 74%, Opus 4.5 79.5 → 88.1%;
  - use tool search when definitions exceed 10K tokens.
- Claude Code's prompt-caching page says deferred tools "only appends new content and doesn't disturb anything already cached".

### ptc-code-exec: confirmed
- Advanced tool use gives 43,588 → 27,297 tokens (37%) and GAIA 46.5 → 51.2%.
- Code execution with MCP (2025-11-04) gives 150,000 → 2,000 tokens (98.7%).
- The cookbook gives 79% cheaper: 135,201 → 19,764 input tokens, $0.6824 → $0.1436. Sandbox container time is billed separately.

### context-rot: confirmed
- Lost in the Middle (arXiv 2307.03172; v1 2023-07-06, v3 2023-11-20; TACL) reports U-shaped accuracy by position.
- Chroma Context Rot (2025-07-14) tested 18 LLMs. Focused prompts of ~300 tokens beat full prompts of ~113K tokens, and the Claude family showed the largest gap.
- Du et al. (arXiv 2510.05381, 2025-10-06, EMNLP'25 Findings) report a 13.9-85% drop even with perfect retrieval.
- Anthropic's effective context engineering post (2025-09-29) uses the "attention budget" framing.

### expensive-failures-tail: corrected
- **Confirmed:**
  - SWE-Effi: failed runs used 8.8M tokens vs 1.8M for resolved runs (SWE-agent + GPT-4o-mini).
  - arXiv 2604.22750: up to 30x variance, and "agent performance peaks at the intermediate-cost run".
  - The optimizing guide: "On a 20-problem WideSearch run, two problems carried 43% of the spend". This is a WideSearch run, not a coding run.
  - Overthinking (arXiv 2502.08235, 2025-02-12): ~30% better performance and 43% lower compute cost.
  - HAL (arXiv 2510.11977, 2025-10-13): "higher reasoning effort reducing accuracy in the majority of runs", 21,730 rollouts, about $40K.
- **Correction:** In arXiv 2604.22750, the "around 50% ... repeated actions on the same file" sentence is in Section 4. It says higher-cost *models* (Qwen3-Coder-480B, Claude Sonnet 4, Kimi-K2) perform more file actions, and about 50% of those are repeats, on the shared-*success* subset. The Section 3 analysis of expensive *failed* runs says only that repeated viewing and editing "sharply increases in the more expensive runs". It gives no 50% figure.

### duplicate-reads: corrected
- **Correction:** The same mis-attribution of the ~50% figure applies here (see above). The figure is about higher-cost models on successful tasks, not about high-cost failed runs.
- **Confirmed:**
  - gotcontext.ai (2026-06-22) reports 42%, 8.8M of 21M tokens, across Claude Code, Cursor and Codex, sourced from a Reddit post. The analysis is gated behind an account, so "weak" is the right label.
  - Claude Code's prompt-caching page says editing a previously read file "does not retroactively change the earlier read"; Claude Code appends a `<system-reminder>` instead.

### trajectory-reduction-taxonomy: confirmed
- AgentDiet (arXiv 2509.23586; v1 2025-09-28, v2 2026-03-15; FSE 2026) covers useless, redundant and expired information. It reports input -39.9 to -59.7% and total cost -21.1 to -35.9% at the same performance.
- SWE-Pruner reports a 23-54% token cut with higher success.
- SWE-Pruner Pro (arXiv 2607.18213, 2026-07-20) reports up to 39% fewer prompt and completion tokens and +3.8% on MiMo-V2-Flash.
- SparseRead, "Read Less, Solve More" (arXiv 2608.22237, 2026-08-23), reports up to 92.9% fewer tokens and 89.0% less wall time, across six models including Claude Opus 5.
- DTOC (arXiv 2609.26121, submitted 2026-08-06; Discovery Science 2026):
  - cost per solved task is 3x and 3.5x lower (Sonnet 4.6, GPT-5.4);
  - "disable-only" (irreversible) variants degraded performance;
  - results were mixed on other models.

### task-budgets: confirmed
- The task-budgets page confirms:
  - beta `task-budgets-2026-03-13`, minimum 20,000 tokens;
  - it is an advisory soft hint and the model sees a countdown;
  - a changed value misses the cache;
  - a budget that is too small "can cause refusal-like behavior";
  - it is not supported on Claude Code or Cowork surfaces.
- The optimizing guide gives the SWE-bench Pro results with Claude Fable 5.1: -44% for about 3 points, and -58% for 6 points.
- The guide says to start sizing near p90; the task-budgets page says p99.

### memory-externalization: confirmed
- Mem0 (arXiv 2504.19413, 2025-04-28) reports >90% token-cost savings, 91% lower p95 latency and +26% relative LLM-judge score, on the LOCOMO conversational benchmark.
- MemGPT (arXiv 2310.08560, 2023-10-12) is confirmed.
- Sleep-time compute (arXiv 2504.13171, 2025-04-17) reports about 5x less test-time compute and 2.5x lower amortized per-query cost.
- Agentic Plan Caching (arXiv 2506.14852; NeurIPS 2025) reports 50.31% lower cost and 27.28% lower latency.
- The context-management blog gives +39% for memory plus editing.
- Effective harnesses (2025-11-26) uses claude-progress.txt and git, and says "compaction isn't sufficient".

### learned-folding: corrected
- **Confirmed:**
  - Context-Folding (arXiv 2510.11967) uses FoldGRPO (RL) and roughly a 10x smaller active context.
  - AgentFold (arXiv 2510.24699, 2025-10-28) grows from about 3.5k to 7k tokens over 100 turns and stays mostly under 20k up to 500 turns.
  - MEM1 (arXiv 2506.15841, RL) reports 3.5x performance and 3.7x less memory.
  - ReSum (arXiv 2509.13313, 2025-09-16) reports +4.5% and +8.2%.
  - ContextPilot (arXiv 2608.28476, 2026-08-28, EMNLP 2026, fine-grained RL) is confirmed.
- **Correction:** "All of these require models trained with reinforcement learning" is wrong.
  - AgentFold was trained with "simple supervised fine-tuning (without continual pre-training or RL)".
  - ReSum's +4.5% is "in training-free settings". Only the further +8.2% comes from ReSum-GRPO (RL).
- **Corrected claim:** most require fine-tuned models (RL for Context-Folding, MEM1, ReSum-GRPO and ContextPilot; SFT for AgentFold). ReSum-style summarization gives +4.5% without training.

### cost-aware-eval: confirmed
- AI Agents That Matter (arXiv 2407.01502, 2024-07-01) is confirmed.
- HAL reports 21,730 rollouts and 2.5B tokens released.
- The Efficient Agents abstract (arXiv 2508.02694, 2025-07-24) says "retains 96.7% of the performance of OWL ... reducing operational costs from $0.398 to $0.228, resulting in a 28.4% improvement in cost-of-pass".
  - The finding's "cost per success down 28.4%" matches the abstract.
  - The report body's pairing "cost-of-pass -28.4% ($0.398 → $0.228)" is arithmetically inconsistent, because $0.398 → $0.228 is a 42.7% cut. The abstract itself is ambiguous here.
- Agentless (arXiv 2407.01489, v2 2024-10-29) reports 32.00% on SWE-bench Lite at $0.70.
- SWE-Effi is confirmed.

### claude-code-telemetry-patterns: confirmed
- The monitoring-usage page confirms:
  - `claude_code.token.usage` (type input/output/cacheRead/cacheCreation) and `claude_code.cost.usage`;
  - `query_source` main/subagent/auxiliary, plus agent.name, skill.name, plugin.name, mcp_server.name, mcp_tool.name, effort and speed;
  - `api_response` events carrying cache_read_tokens and cache_creation_tokens.
- The sessions page confirms transcripts at `~/.claude/projects/<project>/<session-id>.jsonl`, in an internal format that changes between versions, with 30-day retention.
- The costs page confirms:
  - the `/usage` miss rule (more than 5% and at least 2,000 tokens) and the "expected rebuild" category;
  - $13 per developer per active day;
  - uncleared sessions and Opus as default;
  - scheduled tasks, goal check-ins and cross-session messages resending full context while idle;
  - agent teams at ~7x.
- The prompt-caching page covers model, effort, fast-mode and MCP invalidations, and cold `/compact`.
- Nuance: changing effort keeps the cache on Opus 5.5 and Fable 5.1 with an API key or subscription. MCP connect/disconnect invalidates the cache only for non-deferred tools.

### sdk-accounting-pitfalls: confirmed
The Agent SDK cost-tracking page states each of these:
- parallel tool calls share an ID and must be de-duplicated;
- per-step `output_tokens` is a placeholder;
- `usage` excludes subagents, while `total_cost_usd` and `modelUsage` include them;
- resumed sessions carry earlier spend;
- crash results may be zeroed;
- `total_cost_usd` is a client-side estimate;
- `inference_geo: "us"` triggers the 1.1x rate.

### agent-cache-lookback-idle: corrected
- **Correction:** The prompt-caching docs say "The lookback window is 20 blocks ... checks at most 20 positions per breakpoint ... On the Claude API, a run of consecutive `tool_use` blocks counts as one position, and so does a run of consecutive `tool_result` blocks, so a turn with many parallel tool calls doesn't push the previous request's entry out of the window on its own." So "a turn that appends 20 or more tool blocks misses the previous cache entry" is wrong for parallel tool calls. The miss happens when the breakpoint moves 20 or more *positions* past the last cache write, and the documented fix is a second breakpoint.
- **Confirmed:** Keeping the Cache Warm Pays (Khailo, arXiv 2607.19214, 2026-07-21):
  - ~4-minute pings;
  - up to 12.5x cheaper post-pause requests;
  - break-even about 46 minutes on Anthropic (36 on OpenAI/DeepSeek).
- **Confirmed:** Don't Break the Cache (arXiv 2601.06007, 2026-01-09):
  - 41-80% cost reduction over 500+ sessions on 3 providers;
  - strategic block control was more consistent than naive full-context caching.

### parallel-tool-calls: confirmed
- LLMCompiler (arXiv 2312.04511, ICML 2024) reports up to 3.7x lower latency, up to 6.7x cost savings and up to ~9% better accuracy versus ReAct.
- Anthropic's multi-agent post says parallelism "cut research time by up to 90%". That figure is time, not cost.

### observer-compiled-views: confirmed
- Parsing the Stream (Pakhomov and Nijkamp, arXiv 2609.01466, 2026-09-01) reports about 14x and 15x fewer input tokens and 5-7x lower cost than a budget-capped single-call read of the raw trace, with accuracy 0.85-0.87 vs 0.48.
- HAL used LLM-aided log analysis.

### provider-neutral-compaction: confirmed
- The OpenAI compaction guide confirms:
  - server-side compaction via `context_management` with `compact_threshold`;
  - a standalone `/responses/compact` endpoint;
  - an encrypted, opaque compaction item, with the instruction "do not prune `/responses/compact` output";
  - no mention anywhere of usage, billing or cost (checked with a second targeted fetch).
- Anthropic's on-demand compaction returns a signed block.
- Citation fix: the warning about lost content is on the compaction-on-demand page, not the overview. It reads: "Images, documents, `container_upload` blocks, and fetched URLs inside the summarized messages are gone once the block replaces them".
