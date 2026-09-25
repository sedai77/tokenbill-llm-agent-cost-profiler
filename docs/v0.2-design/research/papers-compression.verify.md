# Fact-check: papers-compression.md

Checked on 2026-09-23. I opened every cited source with WebFetch. For local claims I re-read the Token Bill clone (`tokenbill/trace.py`, `tokenbill/pricing.py`, plus a grep), and for derived math I recomputed it in Python.

Tally: 22 confirmed, 3 corrected, 0 unverifiable, 0 refuted.

| id | verdict |
|---|---|
| comp-cache-breakeven | confirmed (derived; math reproduced) |
| capc-compress-then-cache | **corrected** (quality claim) |
| token-not-cost | confirmed |
| history-rewrite-kstar | confirmed (the K* rounding note is below) |
| obs-masking | confirmed |
| agentdiet | confirmed |
| acon | confirmed |
| anthropic-context-mgmt | confirmed |
| compaction-iterations-p0 | confirmed (docs and local code) |
| compaction-quality-tokens-per-task | confirmed |
| hidden-reacquisition | confirmed (scope caveat) |
| llmlingua-family | confirmed |
| compression-in-the-wild | confirmed |
| extractive-rag-pruning | confirmed |
| soft-kv-not-applicable | confirmed |
| code-compression | **corrected** (SWE-Pruner range) |
| tool-output-pruning-2026 | confirmed |
| log-test-reduction | **corrected** (LLM summarizer claim) |
| json-format | confirmed |
| static-prefix-compression | confirmed |
| quality-positive | confirmed |
| tokenizer-exact-counts | confirmed |
| claude-code-otel-fleet | confirmed (span name nit) |
| compaction-timing | confirmed |
| output-compression | confirmed |
| eval-protocol | confirmed |
| dynamic-content-placement | confirmed |

---

## Official pricing and caching parameters (the basis for the derived findings)
- **Pricing doc** (https://platform.claude.com/docs/en/about-claude/pricing, fetched 2026-09-23):
  - 5m cache write is 1.25x and 1h write is 2x.
  - Cache read is 0.1x. Footnotes: "0.025x on Claude Fable 5.1 and Claude Mythos 5.1; 0.05x on Claude Opus 5.5".
  - Opus 5.5 is $4 in / $20 out and Sonnet 4.6 is $3 / $15. Output is 5x input on every listed model.
  - Tokenizer note: "Claude 4.7 and later models and Claude Mythos Preview use a newer tokenizer ... approximately 30% more tokens".
- **Prompt caching doc** (fetched 2026-09-23): the minimum prefix per model is 512 / 1,024 / 2,048 / 4,096, exactly as in report §1.1.
- **Local check:** `tokenbill/pricing.py` has no `claude-opus-5-5` row. Confirmed.

## comp-cache-breakeven: confirmed (derived)
- I recomputed `N/(α+β(N−1))` and got these values:
  - β=0.1: 1.48, 3.03, 4.65, 6.35, 8.13, 8.97
  - β=0.05: 1.54, 3.45, 5.88, 9.09, 13.51, 16.13
  - β=0.025: 1.57, 3.70, 6.78, 11.59, 20.20, 26.85
  - 1h: 0.95, 2.08, 3.45, 5.13, 7.25, 8.40
  - The limits are 1/β, that is 10x, 20x and 40x.
- Insertion-time example: 20000·0.8·(1.25+1.4)·$3e-6 = **$0.1272**.
- One extra turn: 100k·$0.3e-6 + 3k·$3.75e-6 + 500·$15e-6 = **$0.04875**. The ratio is 2.61 turns.
- CAPC ρ_cross values 0.652, 0.797 and 0.942 are reproduced exactly, and the paper states the same values (arXiv 2607.15516v1).
- Caveat: these results rest on the stated assumptions (perfect hits within the TTL, no compressor cost, no minimum-prefix effects).

## capc-compress-then-cache: corrected
- Source: arXiv 2607.15516 (Yan Song, PayPal Inc., v1 17 Jul 2026, claude-sonnet-4-6, 5-min TTL).
- **Confirmed:**
  - LongBench-v2 savings: 89.6% vs vanilla (range 83.6–93.8), 48.5% vs cache-only (23.8–67.4) and 64.4% vs query-aware (41.8–76.3).
  - 94k-token tool schema at r=3: −51.7%, with a 100% hit rate (99.1% on the production path).
  - τ-bench retail: −7.9%, query-aware +40.1% and 36/50 for both arms.
  - Hot tier: about 3,500 tokens. At a 2k-token prefix, ρ(N=30) = 25/30 = 0.833 in all 3 trials. At 4,096 tokens and above, ρ = 1.0.
  - Ratio cap: ⌊|D|/3500⌋. Mutation thresholds: ε_static = 0.05 and ε_quasi = 0.30. Total spend: $98.96.
- **Correction:** "quality within 0.05" holds only *at tier-preserving ratios* (abstract wording). Across the full LongBench-v2 grid the paper reports mean quality of vanilla 0.73, cache-only 0.75, query-aware 0.67 and **CAPC 0.66**, which is −0.07 vs vanilla on average.
- The −51.7% and −7.9% figures are measured against vanilla, not against cache-only.

## token-not-cost: confirmed
- Source: arXiv 2607.12161 (Weinberger and Hozez, PointFive, Jul 2026).
- All numbers match the source:
  - Runs: 2,908 (2,848 after exclusions), 103 tasks, 7 repos.
  - RTK-ML: −38.4% tool-output tokens, +6.8% [+2.8, +11.3] cost.
  - RTK v0.44.1: −2.7% [−5.6, −0.1].
  - Headroom: +48.4% [+42.3, +55.0].
  - Cost composition: 44.3 / 35.4 / 10.4 / 1.3 (residual 8.7).
  - Other figures: content split 74.7% / 3.3%, ceiling about 5%, r = 0.154, 27/40 → 15/40, and 4.46 → 4.69 turns.
- Method: task-clustered bootstrap with 10,000 resamples. Cost-per-success (CPS) is also reported, with Headroom on Opus 4.8 at 1.673.
- Nuance: the paper takes billed cost from the harness `total_cost_usd` field with `cost_source=actual_billed`, cross-checked against the usage fields and the pricing schedule.

## history-rewrite-kstar: confirmed
- Context editing doc (fetched 2026-09-23), quoted verbatim:
  - "Tool result clearing: Invalidates cached prompt prefixes when content is cleared".
  - `clear_at_least` "helps determine if context clearing is worth breaking your prompt cache".
  - Thinking clearing: "the cache is invalidated at the point where clearing occurs".
  - Defaults: trigger 100,000, keep 3, and the `exclude_tools` / `clear_tool_inputs` options.
  - `applied_edits[].cleared_input_tokens` and count_tokens `original_input_tokens` both exist.
- K* recomputed: 17.25, 36.0, 73.5 and 28.5 (1h). The report's "17" rounds 17.25 down, so strict payback is the 18th call. The other cells round consistently.

## obs-masking: confirmed
- Source: arXiv 2508.21433. Lindenbauer et al.; 4th DL4C workshop at NeurIPS 2025; v3 27 Oct 2025.
- M = 10.
- Qwen3-Coder-480B: $1.29 → $0.61, 53.4% → 54.8%.
- Gemini 2.5 Flash: $0.41 → $0.18, 32.8% → 35.6%.
- Savings are 50–56%, and summarization accounts for up to 7.2% of cost, with poor cache reuse.
- Hybrid: 7% below masking and 11% below summarization.
- Detail: 52 vs 44 turns is the Gemini 2.5 Flash case.

## agentdiet: confirmed
- Source: arXiv 2509.23586. Xiao, Gao, Peng and Xiong; FSE 2026 (PACMSE); v1 28 Sep 2025, v2 15 Mar 2026.
- Setup: Trae Agent with Claude 4 Sonnet and Gemini 2.5 Pro, on SWE-bench Verified and Multi-SWE-bench Flash.
- Results: input −39.9% to −59.7%, cost −21.1% to −35.9%, overhead 5.2–14.8%, pass rate −1.0 to +2.0.
- Method details: reflection model GPT-5 mini, a = 2, θ = 500.
- Baseline trajectory: 48.4k tokens over 40 steps, 1.0M accumulated.

## acon: confirmed
- Source: arXiv 2510.00615. ICML 2026; v3 1 Jun 2026.
- Thresholds: 4,096 (history) and 1,024 (observation).
- Peak tokens: "26–54%" in the abstract. The HTML gives AppWorld ">25%", OfficeBench "nearly 30%" and 8-objective QA 54.5%.
- Qwen3-14B gains 45.6% on 8-objective QA, which supports "up to +46%".
- Distilled compressors retain over 95% of the teacher's performance at 99.1% lower cost.
- Latency: 73.24 s → 87.68 s with history compression (101.92 s with observation compression).

## anthropic-context-mgmt: confirmed
- claude.com/blog/context-management (29 Sep 2025): 84% on a 100-turn web-search eval, and +29% / +39% on an internal agentic-search eval.
- Anthropic's effective-context-engineering post (29 Sep 2025) quoted verbatim:
  - "One of the safest lightest touch forms of compaction is tool result clearing".
  - "often 1,000-2,000 tokens".

## compaction-iterations-p0: confirmed
- **Threshold doc:**
  - `compact_20260112`, beta `compact-2026-01-12`, default 150,000, minimum 50,000.
  - "The top-level input_tokens and output_tokens exclude compaction iteration usage ... sum across all entries in the usage.iterations array."
  - It recommends a system-prompt `cache_control` so the prompt stays cached across compaction.
- **On-demand doc:**
  - Beta `compact-2026-09-04` with `compaction: {type: "summarize"}`.
  - "The top-level input_tokens and output_tokens are zero ... sum across usage.iterations".
  - Images, documents and fetched URLs in the summarized range are dropped.
- **Local check:** the `Usage` dataclass (`tokenbill/trace.py:65`) has only four fields. A grep found no handling of iterations, `ephemeral_5m/1h` or `context_management`.

## compaction-quality-tokens-per-task: confirmed
- Source: factory.com/news/evaluating-compression (16 Dec 2025).
- 36,611 messages.
- Compression: 98.6%, 98.7% and 99.3%.
- Overall scores: 3.70, 3.44 and 3.35.
- Artifact tracking: 2.45, 2.33 and 2.19.
- The "tokens per task, not per request" wording is verbatim.

## hidden-reacquisition: confirmed (scope caveat)
- Source: arXiv 2608.16370 (Shuyu Liu, single author, 17 Aug 2026).
- At 5x, no completion change in any of the 6 cells was significant.
- GPT-5.5 retrieval: 21.0 → 63.9 (p = .002). DeepSeek: 22.2 → 55.1 (Table 4). DeepSeek completion at 10x: 83% → 66% (p = .016).
- Full context stayed within the 2.0x budget in 10/10 runs. Sliding 5x and 10x stayed within it in 0/10.
- **Caveat the report omits:** the result comes from one deterministic planning environment with a 24-turn horizon. The paper states that ALFWorld showed **no** retrieval surge (Δ = −0.13), so the effect is environment-dependent.

## llmlingua-family: confirmed
- LLMLingua (EMNLP 2023, v2 6 Dec 2023): "up to 20x compression with little performance loss".
- LongLLMLingua (ACL 2024, v2 12 Aug 2024): 21.4% with about 4x fewer tokens on GPT-3.5-Turbo; 94.0% on LooGLE; 1.4–2.6x on about 10k-token prompts at 2–6x.
- LLMLingua-2 (Findings ACL 2024, v2 12 Aug 2024): XLM-RoBERTa-large / mBERT; 3–6x faster; 1.6–2.9x end-to-end at 2–5x.

## compression-in-the-wild: confirmed
- Source: arXiv 2604.02985 (Kummer et al., ECIR 2026, 3 Apr 2026).
- Rate adherence: LLMLingua error exceeds 0.15 beyond 8k tokens, while LLMLingua-2 adheres tightly.
- Overhead: at most about 3 s at 48k tokens, roughly 7x faster than LLMLingua.
- Task sensitivity: summarization "up to 5.7×"; code about 1.4x; few-shot classification down up to 52%; counting falls from below 20% to below 4.5%.
- Speedup window: above about 5k tokens and above 4x compression.
- Commercial APIs (GPT-3.5 Turbo, GPT-4o mini): "speed-ups dropping below 0.5×" on long prompts.

## extractive-rag-pruning: confirmed
- Jha et al. (Es-FoMo @ ICML 2024): extractive compression reaches up to 10x; token pruning lags; summarization gains are marginal.
- Selective Context (EMNLP 2023): 50%, BERTScore −0.023, faithfulness −0.038.
- RECOMP (ICLR 2024 proceedings page): "as low as 6%".
- Provence (ICLR 2025, 27 Jan 2025): "negligible to no drop".

## soft-kv-not-applicable: confirmed
- Gist tokens: 26x and 40% fewer FLOPs (NeurIPS 2023, v3 12 Feb 2024).
- ICAE: 4x on Llama (ICLR 2024, v4 8 May 2024).
- 500xCompressor: 6–480x, 62.26–72.89% retained (6 Aug 2024).
- AutoCompressors: EMNLP 2023, v2 4 Nov 2023, OPT and Llama-2.
- Context distillation: Snell et al., 30 Sep 2022. The source phrasing is "outperforms directly learning with gradient descent by 9% on SPIDER".
- Compactor: KV memory −68% at full LongBench performance (v2 8 Dec 2025).

## code-compression: corrected
- **Confirmed:**
  - LongCodeZip (ASE 2025, 1 Oct 2025): up to 5.6x.
  - SWE-Pruner: 0.6B skimmer; v4 7 May 2026; 14.84x on LongCodeQA.
  - Hidden Cost of Readability (ICSE'26): −24.5% average input tokens, 10 LLMs, 4 languages. GPT-4o output −27.2% (Java, C++, C#).
- **Correction:**
  - SWE-Pruner's "23–54%" spans two benchmarks. On **SWE-bench Verified** the reduction is **23–38%** (Sonnet 4.5: 23.1%, GLM-4.6: 38.3%). The 54% comes from **SWE-QA** (29–54% across repositories).
  - Rounds −18–26% and success +1.2–1.4 pp are on SWE-bench Verified.
  - The formatting result is on fill-in-the-middle completion. The source's wording is "maintain performance", not literally "no accuracy loss".

## tool-output-pruning-2026: confirmed
- Squeez (Kovács, 4 Apr 2026): 2B Qwen 3.5 with LoRA; −92%; recall 0.86, F1 0.80; +11 recall points over the 35B model.
- CoACT (3 Jul 2026): 33.0% on SWE-bench Verified across 3 models.
- SWE-Pruner Pro (20 Jul 2026): "file-reading commands account for over 70%" in Mini-SWE-Agent on Sonnet 4.5. The method needs last-layer hidden states.
- The 3.3% figure matches arXiv 2607.12161.

## log-test-reduction: corrected
- **Confirmed:**
  - LogDx-CI (Bowen Qin, NUS, 26 May 2026): 11 tools on 35 GitHub Actions failures.
  - The hybrid "hybrid-grep-120k-rtk-tail" scores 0.670 at about 19,844 tokens, "4.5× fewer tokens than grep at same-ballpark quality". Grep alone scores 0.639 at 88,355 tokens.
  - The Claude Code costs doc has the PreToolUse failures-only hook and the ERROR-grep example ("tens of thousands of tokens to hundreds").
  - The RTK README says 60–90% and adds the disclaimer "not the same as cutting your bill".
  - RTK v0.44.1 measured −2.7% in arXiv 2607.12161.
- **Correction:** "LLM map-reduce summarizers do worse at higher cost" is only partly true.
  - In single-shot mode the gpt-5-mini summarizer scored 0.664 against 0.670 for the hybrid, at $0.18 vs $0.03 per case.
  - In **agent-loop mode it ranked #1** (0.749 vs 0.747 for the hybrid).
  - Only the Haiku 4.5 summarizer was both weaker and the most expensive (about $1.75 per case).

## json-format: confirmed
- TOON benchmark page:
  - 4 models, 244 questions, o200k_base tokenizer.
  - JSON: 4,308 tokens at 71.4%. Compact JSON: 2,892 at 69.0%. TOON: 2,474 at 72.2%, "42.6% fewer tokens".
  - I did not see the version label "v4.1.1" in the fetch.
- improvingagents.com (28 Oct 2025): "failed to find circumstances where TOON was the best-performing format". Nested data: 43.1% vs 62.1% for YAML.
- arXiv 2603.03306 (Matveev, 8 Feb 2026): plain JSON is best for generation, and a "prompt tax" applies on short contexts.

## static-prefix-compression: confirmed
- Advanced tool use (24 Nov 2025): −85%; Opus 4 49% → 74%; Opus 4.5 79.5% → 88.1%; does not break caching; programmatic tool calling 43,588 → 27,297 (−37%).
- Code execution with MCP (4 Nov 2025): 150,000 → 2,000 tokens (98.7%).
- SkillReducer (v2 24 Jun 2026): 55,315 skills; −48% descriptions, −39% bodies, +2.8% quality.
- Claude Code docs:
  - The costs doc says MCP tools are deferred by default and to keep CLAUDE.md under 200 lines.
  - The MCP doc gives the 10,000-token warning, the 25,000 default and `MAX_MCP_OUTPUT_TOKENS`.
  - The env-vars doc gives `BASH_MAX_OUTPUT_LENGTH` with default 30000.

## quality-positive: confirmed
- Chroma (14 Jul 2025): 18 models including Claude Opus 4 / Sonnet 4, GPT-4.1 and Gemini 2.5. It reports degradation "even on simple tasks" and that a single distractor hurts.
- Lost in the Middle: TACL, v3 20 Nov 2023, U-shaped curve.
- arXiv 2505.00019 (ICLR 2025 Building Trust workshop) has both quoted claims.
- The compaction overview says: "because response quality degrades as a conversation grows".

## tokenizer-exact-counts: confirmed
- The token counting doc gives the about 30% note and says Fable/Mythos 5.x share the Opus 4.7 tokenizer.
- The endpoint is free, with 5,000, 10,000 and 20,000 RPM by tier.
- Counts are an "estimate" and may include system-added tokens, which are not billed.
- The endpoint does not use caching logic and rejects server tools and the MCP connector.
- Local: `CHARS_PER_TOKEN = 3.7` (`trace.py:52`).
- Nit: "does not bill system-added tokens" should read "counts may include system-added tokens, which are not billed".

## claude-code-otel-fleet: confirmed
- Monitoring doc:
  - `claude_code.token.usage` has type input, output, cacheRead and cacheCreation.
  - Its attributes include query_source (main, subagent, auxiliary), effort, skill.name, plugin.name, mcp_server.name and mcp_tool.name.
  - `claude_code.cost.usage` also exists.
  - `result_tokens` is described as the "Approximate token size of the tool result".
  - Content is redacted by default, including under `OTEL_LOG_TOOL_CONTENT`.
- Nit: the span is named **`claude_code.tool.execution`**, not `claude_code.tool`.
- The costs doc shows `/usage` Prompt cache (main) with hit share, misses with likely cause, and "expected rebuild (compaction or tool-result clearing)". It also gives about $13 per active day and $150–250 per month.

## compaction-timing: confirmed
- The Claude Code prompt-caching doc:
  - The compaction request uses the same system prompt, tools and history, so it reads the cache when warm.
  - After a break it reprocesses everything uncached, so "/compact costs the most when you resume an old session".
  - /rewind returns to an already-cached prefix.
- The costs doc says "/clear costs nothing".
- claude.dev blog (Thariq Shihipar, 30 Apr 2026): "we run alerts on our prompt cache hit rate and declare SEVs if they're too low". The Claude Code docs link the same post under claude.com/blog/.
- OpenHands (Calvin Smith, 9 Apr 2025): up to 2x per turn; 54% vs 53%.
- arXiv 2607.19214 (Khailo, v2 24 Jul 2026): about 4 minutes, up to 12.5x, about 46 minutes.

## output-compression: confirmed
- Chain of Draft (v2 3 Mar 2025): "matches or surpasses CoT ... as little as only 7.6% of the tokens".
- GPT-4o: 27.2% output reduction.
- Output is 5x input in the pricing doc, and output was 10.4% of cost in arXiv 2607.12161.

## eval-protocol: confirmed
- arXiv 2607.12161: task-clustered bootstrap, 95% CIs, and CPS (cost per success).
- Factory: probe-based evaluation.
- CoACT: next-action preservation.
- ACON: "tasks where the agent succeeds with H but fails with H′" (contrastive subset).

## dynamic-content-placement: confirmed
- arXiv 2601.06007 (Lumer et al., v2 31 Jan 2026): 500+ sessions, 10k-token system prompts, 41–80% cost, 13–31% TTFT.
- The abstract says, verbatim, "more consistent benefits than naive full-context caching, which can paradoxically increase latency".
- The Manus blog (Yichao 'Peak' Ji, 18 Jul 2025) supports this: a stable prefix, no timestamps, masking tools rather than removing them, and restorable compression.
