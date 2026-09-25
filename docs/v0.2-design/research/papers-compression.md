# Prompt and context compression on per-token-billed APIs: research report (track: papers-compression)

Prepared for: Token Bill (tokenbill v0.1.2), enterprise-readiness research
Research date: 2026-09-23 (all sources below were opened on this date unless noted)
Scope: academic and industry work on prompt and context compression (LLMLingua family, Selective Context, RECOMP, gist tokens, ICAE, 500xCompressor, AutoCompressors, context distillation, observation and tool-output compression, code, log and JSON minification, retrieval-based pruning, agent compaction), 2025-2026 follow-ups, and above all the **interaction between compression and prompt caching** on closed APIs billed per token (Anthropic first, with OpenAI where it matters).

Evidence conventions: "strong" means peer-reviewed or official provider documentation, or several independent sources agreeing. "Moderate" means a single preprint with a sound design, or a vendor study with a clear method. "Weak" means a vendor claim, small sample, or unreplicated result. Numbers are the sources' own. Anything I derived myself is labeled **derived**, and its inputs are cited.

---

## 0. Executive summary: what matters most for Token Bill

1. **Compression and caching work against each other, and the arithmetic has a closed form.** A cache read costs 0.1x the base input rate on most Claude models, 0.05x on Opus 5.5 and 0.025x on Fable/Mythos 5.1. So compressing a *stable, reused* prefix differently on every call beats plain caching only when the compression ratio is above about `N / (1.25 + β(N-1))`, which approaches 10x, 20x or 40x respectively. The winning strategy for static content is **compress once, then cache** (query-agnostic and deterministic). For dynamic content, compress **at insertion time**, before the content is ever written to cache, and **never rewrite history** unless the rewrite pays back within a computable number of turns.
2. **Fewer tokens does not mean a smaller bill.** In a 2,908-run study of Claude Code billed by the provider (Jul 2026), the most aggressive tool-output compressor cut delivered tool-output tokens by 38.4%, yet **raised billed cost by 6.8%**. One API-boundary optimizer raised cost by **48.4%**. Cache writes and reads made up about 80% of the bill, and tool outputs only about 3.3%. Token Bill's core value is to be the **neutral, billed-dollar, success-adjusted arbiter** of compression claims, which no one else is today.
3. **Where compression does work, in agents: pruning old observations.** Examples: masking old observations (−50% cost with the solve rate unchanged, on SWE-bench Verified), AgentDiet (−21% to −36% net cost), ACON (−26% to −54% peak tokens), Anthropic context editing (−84% tokens on a 100-turn eval) and OpenHands condensation (up to 2x per turn). All of these rewrite history, so they are **cache-breaking events**. They must be batched (Anthropic's `clear_at_least`) and timed well: when the cache is cold, and early enough in a long session to pay back.
4. **The size of the opportunity depends heavily on the workload.** In short Claude Code tasks (about 4.5 turns), the system prompt plus tool definitions made up 74.7% of cost and tool outputs 3.3%. In Mini-SWE-Agent on Sonnet 4.5, file reads made up more than 70% of tokens. Token Bill must **measure the content composition of each workload, weighted by billing category**, before recommending any compression.
5. **Classic academic compressors (LLMLingua and its successors) are mature, but they mostly apply to RAG and document context, not to agent loops.** They deliver 2-20x with small loss on QA and summarization. Losses are large on few-shot classification (up to −52%) and on synthetic counting tasks. Code tolerates only about 1.4x under generic compressors, while code-specific ones reach 5.6x. For closed APIs the useful ones are **black-box, extractive or classifier-based** methods (LLMLingua-2, extractive rerankers, Provence, RECOMP-extractive, LongCodeZip, SWE-Pruner). Soft-prompt and KV methods (gist, ICAE, 500x, AutoCompressors, KV-cache compression) need model weights and **do not reduce a per-token API bill**.
6. **Quality can improve.** Long contexts degrade models (Chroma "context rot": 18 models, Jul 2025; "Lost in the Middle", TACL). LongLLMLingua gained +21.4% with about 4x fewer tokens. ACON gained up to +46% for small models. Token Bill can present compression as "cheaper *and* often better", but only with measured guardrails, because compression also causes hidden re-acquisition: retrieval calls roughly tripled under 5x sliding compression while the completion rate looked unchanged.
7. **Correctness gap in Token Bill today.** Anthropic's server-side compaction reports its summarization call in `usage.iterations`, and the top-level `input_tokens`/`output_tokens` **exclude it**. Token Bill reads only the top-level fields, so it **undercounts any trace that uses compaction**. It also ignores `context_management.applied_edits` (tokens cleared by context editing) and the 5m/1h split of cache writes.

---

## 1. The economics of compression versus caching (the core analysis)

### 1.1 Price parameters (Anthropic, verified 2026-09-23)

Source: https://platform.claude.com/docs/en/about-claude/pricing (live doc, fetched 2026-09-23); https://platform.claude.com/docs/en/build-with-claude/prompt-caching (live doc, fetched 2026-09-23).

| Symbol | Meaning | Value |
|---|---|---|
| α (5m) | 5-minute cache write, as a multiple of base input | 1.25x |
| α (1h) | 1-hour cache write | 2.0x |
| β | cache read (hit/refresh) | 0.1x standard; **0.05x Opus 5.5**; **0.025x Fable 5.1 / Mythos 5.1** |
| min prefix | smallest cacheable prefix | 512 (Opus 5.5/5, Fable), 1,024 (Sonnet 5/4.6, Opus 4.8), 2,048 (Opus 4.7), 4,096 (Haiku 4.5, Opus 4.6/4.5) |

Note for Token Bill: its `pricing.py` has no row for **claude-opus-5-5** ($4 in / $20 out, cache read 0.05x). Also, the Claude 4.7+ tokenizer produces about **30% more tokens** for the same text (see F22).

### 1.2 Scenario A: a stable prefix reused N times within the TTL (**derived**)

Cost in units of `P · p_in` (P = prefix tokens):

- Cache only: `α + β(N−1)`
- Compress on every call (query-aware, never cacheable, ratio r): `N / r` (plus compressor cost)
- **Compress once, then cache** (query-agnostic, deterministic): `(α + β(N−1)) / r` (plus a one-time compressor cost)

Per-call compression beats cache-only only if **r > N / (α + β(N−1))**:

| N reuses | β = 0.1 (most models) | β = 0.05 (Opus 5.5) | β = 0.025 (Fable 5.1) | β = 0.1 with 1h writes (α = 2) |
|---|---|---|---|---|
| 2 | r > 1.48 | 1.54 | 1.57 | 0.95 |
| 5 | 3.03 | 3.45 | 3.70 | 2.08 |
| 10 | 4.65 | 5.88 | 6.78 | 3.45 |
| 20 | 6.35 | 9.09 | 11.59 | 5.13 |
| 50 | 8.13 | 13.51 | 20.20 | 7.25 |
| 100 | 8.97 | 16.13 | 26.85 | 8.40 |
| ∞ | **10** | **20** | **40** | 10 |

Interpretation: as providers cut cache-read prices, per-query compression of reused content becomes steadily less attractive. Compress-then-cache still wins over cache-only by a factor of r whenever quality holds. This matches the independent derivation in the Cache-Aware Prompt Compression paper (F2): its steady-state threshold `ρ_cross(r) = (α − 1/r)/(α − β)` gives 0.652 at r=2, 0.797 at r=3 and 0.942 at r=6 for Sonnet 4.6. My own calculation reproduces those values exactly.

### 1.3 Scenario B: rewriting history (clearing, masking, compaction) (**derived**)

Clearing X tokens from the middle of the context, with S tokens after the edit point that now have to be re-written to cache, costs `S·(α−β)·p_in` once. Each later call then saves `X·β·p_in`. Break-even is at **K\* = S(α−β) / (Xβ)** calls, counting from the edit:

| Case | 5m, β=0.1 | 5m, Opus 5.5 | 5m, Fable 5.1 | 1h, β=0.1 |
|---|---|---|---|---|
| X=40k cleared, S=60k after the edit | **17 calls** | 36 | 74 | 29 |
| X=40k, S=20k | 6 | 12 | 25 | 10 |
| X=30k, S=150k (edit near the start) | 58 | 120 | 245 | 95 |

Consequences:
- **Clear late in the prefix, clear a lot at once, and clear early in long sessions.** This is exactly why Anthropic added `clear_at_least` (F8) and why OpenHands condenses only at a threshold (F24).
- **Clear when the cache is cold.** If the cache has already expired (idle longer than the TTL), the baseline would re-write everything anyway, so clearing saves `X·α` immediately at no invalidation cost. For LLM-summary compaction the opposite holds: the summarization call is cheap when the cache is warm, because it reads the cached prefix, and expensive when cold (Claude Code docs, F24).
- This break-even does not include the quality and re-acquisition cost of losing information (F11, F3). Token Bill should report K\* next to a sensitivity figure for extra turns.

### 1.4 Scenario C: compressing at insertion time (new tool output, before its first transmission) (**derived**)

Compressing a new block of T tokens at ratio r, which will then stay in context for K more calls, saves `T(1−1/r)·(α + β(K−1))·p_in`. This is always positive, and it never invalidates cache because the bytes were never cached. Worked example (Sonnet 4.6, $3/MTok): a 20k-token tool output compressed 5x and kept for 15 calls saves **$0.127**. One extra agent turn at 100k context (reads plus 3k new tokens plus 500 output tokens) costs **$0.049**. So the compression pays only if it adds **fewer than about 2.6 turns**. That sensitivity is the number Token Bill should print.

---

## 2. Findings

### F1 — Break-even laws for compression versus caching (derived from official pricing) — strong
- **Claim:** see Section 1. Per-call (query-aware) compression of reused content needs r > 10x, 20x or 40x (β = 0.1, 0.05, 0.025) as reuse grows. Compress-then-cache dominates. Insertion-time compression is always cache-safe. Rewriting history has a finite payback horizon K\*.
- **Sources:** https://platform.claude.com/docs/en/about-claude/pricing (fetched 2026-09-23; Opus 5.5 0.05x and Fable 5.1 0.025x footnotes); https://platform.claude.com/docs/en/build-with-claude/prompt-caching (fetched 2026-09-23); https://arxiv.org/abs/2607.15516 (Jul 17 2026, confirms the ρ_cross formula).
- **Token Bill:** add a "compression economics" panel to every report: r\* for each prefix segment given its observed reuse count N and the model's β; K\* for each observed or hypothetical history rewrite; insertion-time savings for each tool output, with the extra-turn sensitivity.

### F2 — Cache-Aware Prompt Compression (CAPC): compress once, then cache — moderate
- **Claim:** One author (PayPal), measured on the Claude API with claude-sonnet-4-6 and 5-minute ephemeral caching. Method: compress documents offline with query-agnostic sentence selection, put a cache_control marker on the compressed block, and append per-query content uncached. On LongBench-v2 it cut cost **89.6%** against vanilla, **48.5%** against cache-only and **64.4%** against query-aware compression, with quality within 0.05 of the uncompressed baseline. On a 94k-token enterprise tool schema at r=3: −51.7% cost with a 100% hit rate. On τ-bench retail: −7.9% cost at identical quality (36/50), while **query-aware compression cost +40.1%**, more than no compression at all. The paper also reports an empirical "hot tier": prefixes below about 3.5k tokens had a hit rate near 0.83, versus about 1.0 above that. It therefore proposes capping the ratio at `⌊|D|/3500⌋` and an AdaptiveCacheBoundary that classifies segments as STATIC (≤5% mutation), QUASI (5-30%) or DYNAMIC (>30%). The whole study cost $98.96.
- **Caveats:** single-author v1 preprint. The hot-tier threshold is not documented by Anthropic.
- **Sources:** https://arxiv.org/abs/2607.15516 and https://arxiv.org/html/2607.15516v1 (v1, Jul 17 2026).
- **Token Bill:** (a) a detector for **query-aware compression that breaks caching**: consecutive calls whose large prefix block changes each time with a small edit distance (compressed variants of one source) and gets zero cache reads. (b) Segment mutation classification (STATIC/QUASI/DYNAMIC) across calls, to recommend where the breakpoint should go. (c) Optional validation of the hot-tier hit rate against billed data: Token Bill already records billed cache reads, so it can test this claim on enterprise traffic at scale.

### F3 — "Token Reduction Is Not Cost Reduction" (Claude Code, 2,908 provider-billed runs) — moderate (large paired sample, single preprint)
- **Claim:** 103 tasks in 7 repos, on Haiku 4.5, Sonnet 5 and Opus 4.8 at several effort levels. Billing came from the provider's usage fields.
  - RTK-ML cut tool-output tokens **−38.4%** but changed billed cost by **+6.8%** [+2.8, +11.3].
  - RTK v0.44.1 changed cost by −2.7% [−5.6, −0.1].
  - The Headroom proxy changed cost by **+48.4%** [+42.3, +55.0]: +47% to +53% on every model, and 67% more per successful execution on Opus 4.8.
  - Cost breakdown: cache creation 44.3%, cache reads 35.4%, output 10.4%, uncached input 1.3%, residual 8.7%.
  - Cost by content: system prompt plus tool definitions 74.7%, tool outputs 3.3%. The authors put the ceiling for visible-token compression at about 5% of cost.
  - Per-task correlation between tool-output reduction and cost change: r = 0.154.
  - Aggressive compression cut patch applicability from 27/40 to 15/40 by damaging edit anchors.
  - Turns rose from 4.46 to 4.69.
- **Sources:** https://arxiv.org/html/2607.12161 (Jul 2026, PointFive authors).
- **Token Bill:** this is the justification for Token Bill's reason to exist. Build (a) **success-adjusted billed cost per task** as the main metric; (b) a **paired A/B mode** with bootstrap confidence intervals; (c) a "trajectory elongation" check (turns, re-reads and searches per task, before versus after); (d) an "edit-anchor safety" check that flags compression applied to files that are edited later.

### F4 — Rewriting history is a cache-breaking event; batch it (Anthropic context editing docs) — strong
- **Claim:** Anthropic's docs say that clearing tool results **invalidates cached prompt prefixes**. They recommend `clear_at_least`, so that each clearing event removes enough tokens to justify the cache-write cost. Clearing thinking blocks likewise invalidates the cache at the point of clearing. Defaults for `clear_tool_uses_20250919`: trigger at 100,000 input tokens, keep 3 tool uses, plus `exclude_tools` and `clear_tool_inputs` options. The response reports `context_management.applied_edits[].cleared_input_tokens`. The token-counting endpoint can preview the effect (`original_input_tokens` versus `input_tokens`).
- **Sources:** https://platform.claude.com/docs/en/build-with-claude/context-editing (live doc, fetched 2026-09-23).
- **Token Bill:** (a) parse `applied_edits` and attribute the cache-write spike on the following call to a "context-edit rebuild" rather than a breaker. (b) Simulate `clear_tool_uses` policies over recorded traces, sweeping trigger, keep and clear_at_least and scoring each with the cache replay engine. Output the cheapest policy with its K\* check. (c) A new breaker type, **edit-churn**: frequent small clearings where each rebuild costs more than it saves.

### F5 — Observation masking matches LLM summarization at about half the cost (JetBrains, NeurIPS'25 DL4C) — strong for the direction, moderate for the exact magnitudes
- **Claim:** On SWE-bench Verified with SWE-agent, rolling-window masking of old observations (keep the last M=10) cut cost about 50-56% against the raw agent. Qwen3-Coder-480B: $1.29 to $0.61 with the solve rate going from 53.4% to 54.8%. Gemini 2.5 Flash: $0.41 to $0.18 with the solve rate going from 32.8% to 35.6%. LLM summarization gave similar or smaller savings but made trajectories about 15% longer (52 versus 44 turns), because summaries encouraged the agent to keep going. Summarization calls got poor cache reuse, since each one processes a unique sequence of turns, and accounted for up to 7.2% of instance cost. A hybrid saved a further 7% over masking and 11% over summarization.
- **Sources:** https://arxiv.org/abs/2508.21433 (v3 Oct 27 2025); https://arxiv.org/html/2508.21433v3.
- **Token Bill:** add an **observation-masking counterfactual** (keep the last M tool results in full and replace older ones with a placeholder) and replay it through the cache simulator, reporting net dollars including rebuild writes. Recommend M and the masking cadence. Also flag LLM-summarization compactions that come before longer trajectories.

### F6 — AgentDiet trajectory reduction: −39.9% to −59.7% input tokens, −21.1% to −35.9% net cost (FSE 2026) — strong
- **Claim:** Integrated into Trae Agent with Claude 4 Sonnet and Gemini 2.5 Pro, on SWE-bench Verified and Multi-SWE-bench Flash. It removes **useless** content (cache files, verbose build output), **redundant** content (code repeated in str_replace calls) and **expired** content (searches that are no longer needed). A cheap reflection model (GPT-5 mini) edits step s−a, with a=2 steps of delay, only for steps above 500 tokens, so the cache stays valid up to that point. The reflection overhead is 5.2-14.8%. Pass rate changed by −1.0 to +2.0 points. The baseline trajectory was 48.4k tokens over 40 steps and 1.0M accumulated tokens per issue.
- **Sources:** https://arxiv.org/abs/2509.23586 (v1 Sep 28 2025, revised Mar 15 2026); https://arxiv.org/html/2509.23586.
- **Token Bill:** implement the three waste classes as **trace detectors** that need no LLM: (1) useless: build and log noise patterns; (2) redundant: repeated n-gram or hash spans across tool_use inputs and tool_results; (3) expired: tool results never lexically referenced by any later assistant text or tool input. Report each with the dollars they cost (lifetime reads plus writes).

### F7 — ACON: optimized compression guidelines plus distilled compressors (ICML 2026) — strong
- **Claim:** It compresses history once history exceeds about 4,096 tokens and observations once they exceed about 1,024 (the thresholds it found best). Guidelines are refined by contrasting failed compressed trajectories with successful uncompressed ones. Results: peak tokens −26% on AppWorld, about −30% on OfficeBench and −54.5% on 8-objective QA. Small models gained up to +46%. Distilled compressors (Qwen3-14B) retain over 95% of the GPT-4.1 teacher's performance and cut compressor cost by **99.1%**. Wall-clock time rose from 73.2 s to 87.7 s because of the extra compressor calls.
- **Sources:** https://arxiv.org/abs/2510.00615 (v3 Jun 1 2026); https://arxiv.org/html/2510.00615v3.
- **Token Bill:** (a) "peak context" is not the bill. Token Bill should always convert peak-token claims into billed dollars, including compressor calls. (b) Recommend a **small or cheap model for any LLM-based compressor** and account for its cost as a separate line (query_source = auxiliary).

### F8 — Anthropic context editing and memory: −84% tokens, +29% to +39% task performance (official) — strong
- **Claim:** On a 100-turn web-search evaluation, context editing cut token use by 84% and let workflows finish that otherwise would have failed. Context editing alone improved agentic search by 29%, and combined with the memory tool by 39%.
- **Sources:** https://claude.com/blog/context-management (Sep 29 2025); https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents (Sep 29 2025). The latter calls tool-result clearing one of the safest, lightest-touch forms of compaction and recommends sub-agents that return condensed 1,000-2,000-token summaries.
- **Token Bill:** a recommendation engine that suggests `context_management` configs from observed tool-result sizes and lifetimes, with projected dollars from the replay engine (see F4).

### F9 — Server-side compaction billing lives in `usage.iterations`, and Token Bill currently undercounts it — strong
- **Claim:** Two compaction modes exist. Threshold compaction (`compact_20260112`, beta `compact-2026-01-12`) has a default trigger of 150,000 input tokens and a minimum of 50,000. On-demand compaction (beta `compact-2026-09-04`) uses a `compaction: {type: "summarize"}` request and returns a signed block. In both, the summarization call is billed and reported as a `compaction` entry in `usage.iterations`, and **the top-level input_tokens/output_tokens exclude it**. For on-demand requests the top level is zero. The docs say to sum across iterations. A `cache_control` breakpoint on the compaction block caches the summary. The docs also recommend a breakpoint at the end of the system prompt so it stays cached across compaction events. Images, documents and fetched URLs inside the summarized range are dropped.
- **Sources:** https://platform.claude.com/docs/en/build-with-claude/compaction-threshold; https://platform.claude.com/docs/en/build-with-claude/compaction-on-demand; https://platform.claude.com/docs/en/build-with-claude/compaction (all live docs, fetched 2026-09-23).
- **Local check:** `tokenbill/trace.py` `Usage` has only input, cache_read, cache_creation and output. There is no handling of `iterations`, `cache_creation.ephemeral_{5m,1h}` or `context_management`. A grep for "compaction" and "iteration" in the package found nothing relevant.
- **Token Bill:** P0 fix. Parse and price `usage.iterations`, show compaction as its own cost category, and add a **compaction-efficiency** metric: compaction cost ÷ (tokens removed × expected remaining reads). Add a breaker for **compaction without a system-prompt breakpoint** (the system prompt gets re-written after every compaction).

### F10 — Compaction quality: roughly 99% compression, but artifact tracking is the weak spot; optimize tokens per task (Factory) — moderate (vendor study)
- **Claim:** Probe-based evaluation on 36,000+ production messages. Compression ratios: Factory 98.6%, Anthropic 98.7%, OpenAI 99.3%. Overall scores out of 5: Factory 3.70, Anthropic 3.44, OpenAI 3.35. Artifact-tracking scores were the lowest for everyone (2.19-2.45). Factory argues that the right target is **tokens per task, not tokens per request**, because lost details cause files to be re-read and work to be redone.
- **Sources:** https://factory.com/news/evaluating-compression (Dec 16 2025; redirected from factory.ai).
- **Token Bill:** a **post-compaction re-acquisition** metric: after each compaction event, count re-reads of files or URLs that were read before it, and price them. This directly measures "tokens per task" damage.

### F11 — Compression hides re-acquisition costs that completion metrics miss — moderate
- **Claim:** At 5x sliding-window compression, the completion rate did not change significantly in any of six model-by-regime cells, but **retrieval calls rose sharply**. GPT-5.5 went from 21.0 to 63.9 calls (p=.002). DeepSeek went from 22.2 to 55.1. At 10x, DeepSeek's completion fell from 83% to 66%. Every compressed run exceeded a 2x tool-budget reference, while full context stayed within budget in 10 of 10 runs.
- **Sources:** https://arxiv.org/abs/2608.16370 and https://arxiv.org/html/2608.16370 (Aug 17 2026).
- **Token Bill:** a guardrail report for any compression policy that shows "tool calls per task" and "re-read rate" next to dollars, with a warning when they rise.

### F12 — The LLMLingua family: 2-20x compression with small loss on text; the most mature black-box compressors — strong
- **Claim:**
  - LLMLingua (EMNLP 2023): up to **20x** with little loss on GSM8K, BBH, ShareGPT and Arxiv.
  - LongLLMLingua (ACL 2024): **+21.4%** on NaturalQuestions with about **4x** fewer tokens (GPT-3.5-Turbo), **94.0%** cost reduction on LooGLE, and 1.4-2.6x lower latency at 2-6x compression of about 10k-token prompts.
  - LLMLingua-2 (Findings of ACL 2024): a task-agnostic token classifier (XLM-RoBERTa, mBERT), **2-5x** compression, 3-6x faster than earlier compressors, 1.6-2.9x lower end-to-end latency.
- **Applicability:** LLMLingua-2 runs locally on CPU or GPU and needs no access to the target model, so it can be used with Claude. The query-aware variants (LongLLMLingua) produce a different compressed prefix for each query, which conflicts with caching (F2).
- **Sources:** https://arxiv.org/abs/2310.05736 (v2 Dec 6 2023); https://arxiv.org/abs/2310.06839 (v2 Aug 12 2024); https://arxiv.org/abs/2403.12968 (v2 Aug 12 2024).
- **Token Bill:** an optional plugin (extra dependency, off by default) to estimate the **compressibility** of RAG and document blocks offline with LLMLingua-2 at a target rate. Only recommend it **query-agnostically, at insertion time or offline-then-cached**.

### F13 — Compression in the wild: overheads, rate adherence, and task sensitivity — moderate
- **Claim:**
  - LLMLingua-2 hits the requested compression rate reliably. LLMLingua's error exceeds 0.15 beyond 8k tokens.
  - LLMLingua-2 is about 7x faster than LLMLingua, under 3 s of overhead at 48k tokens.
  - Summarization stays stable up to **5.7x**. Code generation shows minimal degradation but only reaches about **1.4x**.
  - **Few-shot classification drops by up to 52%.** Synthetic counting collapses (from under 20% to under 4.5%).
  - End-to-end speedups appear only above about 5k-token prompts and above 4x compression. With commercial APIs, long-prompt speedups fell below 0.5x, meaning compression made the whole call slower.
- **Sources:** https://arxiv.org/html/2604.02985v1 (Apr 3 2026).
- **Token Bill:** a content-type risk table that says **never** compress instructions, few-shot examples or exemplars, and should compress observations, retrieved documents and logs. Present latency as a trade-off, not a win, on hosted APIs.

### F14 — Extractive and relevance-based pruning beat token pruning; RAG context can shrink 2-16x — strong
- **Claim:**
  - Jha et al. (Es-FoMo @ ICML 2024): extractive (reranker) compression reached **up to 10x** with minimal accuracy loss. Token pruning underperformed, and abstractive summarization gave only marginal gains.
  - Selective Context (EMNLP 2023): **50%** context reduction for −0.023 BERTScore and −0.038 faithfulness.
  - RECOMP (ICLR 2024): retrieved documents compressed **to 6%** of their length with minimal loss.
  - Provence (ICLR 2025): question-conditioned sentence pruning plus reranking with a negligible drop across domains.
- **Sources:** https://arxiv.org/abs/2407.08892 (Jul 11 2024); https://arxiv.org/abs/2310.06201 (Oct 9 2023); https://arxiv.org/abs/2310.04408 (Oct 6 2023) with https://proceedings.iclr.cc/paper_files/paper/2024/hash/bda88ed2892f5e61c9a9bf215c566913-Abstract-Conference.html; https://arxiv.org/abs/2501.16214 (Jan 2025, ICLR 2025).
- **Token Bill:** for RAG workloads, a "retrieved-context efficiency" metric: the share of retrieved tokens that later answers never reference, estimated by lexical overlap. Recommend extractive pruning (sentence-level, deterministic given the query) at insertion time.

### F15 — Soft-prompt and KV methods do not reduce a closed-API bill — strong
- **Claim:**
  - Gist tokens (NeurIPS 2023): up to **26x** compression and 40% fewer FLOPs, but they require training the model.
  - ICAE (ICLR 2024): **4x** on Llama.
  - 500xCompressor: **6-480x** while retaining **62-73%** of capability.
  - AutoCompressors (EMNLP 2023): fine-tuned OPT and Llama-2 to produce summary vectors.
  - Context distillation (Snell, Klein, Zhong 2022): instructions and scratchpads are internalized through fine-tuning, +9% on SPIDER.
  - KV-cache compression (for example Compactor: 68% less KV memory at full LongBench performance) lowers serving memory, not billed tokens.

  All of these need model weights or a self-hosted server.
- **Sources:** https://arxiv.org/abs/2304.08467 (v3 Feb 12 2024); https://arxiv.org/abs/2307.06945 (v4 May 8 2024); https://arxiv.org/abs/2408.03094 (Aug 6 2024); https://arxiv.org/abs/2305.14788 (Nov 4 2023); https://arxiv.org/abs/2209.15189 (Sep 30 2022); https://arxiv.org/abs/2507.08143 (rev. Dec 8 2025); survey taxonomy (hard versus soft prompts) at https://arxiv.org/abs/2410.12388 (v2 Oct 17 2024).
- **Token Bill:** document explicitly that these are **out of scope for API bills**, so the tool keeps its credibility. The one API-side analogue of context distillation is *fine-tuning or routing to a cheaper model*, which belongs to other tracks. For self-hosted fleets, Token Bill could later model GPU cost, but that is a different product.

### F16 — Code-specific compression: 5.6x (LongCodeZip), 23-54% of agent tokens (SWE-Pruner), −24.5% by removing formatting — strong
- **Claim:**
  - LongCodeZip (ASE 2025): up to **5.6x** with no loss on completion, summarization and QA, using function-level and then block-level perplexity selection.
  - SWE-Pruner (Jan-May 2026): a 0.6B goal-conditioned line skimmer cut agent tokens **23-54%** on SWE-bench Verified with success equal or better, cut rounds 18-26%, and reached up to 14.84x on single-turn LongCodeQA.
  - "The Hidden Cost of Readability" (ICSE'26): removing code formatting (indentation, whitespace, newlines) cuts input tokens **24.5% on average** without an accuracy loss across 10 LLMs and 4 languages. Unformatted output saves about 27% on GPT-4o for Java, C++ and C#.
- **Sources:** https://arxiv.org/abs/2510.00446 (Oct 1 2025); https://arxiv.org/abs/2601.16746 (v4 May 7 2026); https://arxiv.org/abs/2508.13666 (Aug 19 2025).
- **Token Bill:** a **whitespace and formatting overhead** metric for code-bearing tool results (file reads, diffs). Count the tokens spent on indentation, blank lines and comments, and price them over each block's lifetime. Warn that stripping must be **lossless for edits**: edit tools match exact strings, and F3 found aggressive compression damaged edit anchors. The safe variant is to strip only in read-only contexts such as search results, and never in files that are about to be edited.

### F17 — Tool-output pruning for coding agents (2026 wave) — moderate
- **Claim:**
  - Squeez (Apr 2026): a LoRA-tuned Qwen 3.5 2B removes **92%** of tool-output tokens while keeping 0.86 recall and 0.80 F1 of the needed evidence, beating a zero-shot 35B model by 11 recall points and all heuristic baselines.
  - CoACT (Jul 2026): "next-action-preserving" compression cut total tokens **33%** on SWE-bench Verified at near-baseline effectiveness.
  - SWE-Pruner Pro (Jul 2026): **file-reading commands account for over 70%** of tokens in Mini-SWE-Agent on Claude Sonnet 4.5. Its method needs model internals, so it does not apply to closed APIs.
  - Active Context Compression (Jan 2026, only 5 instances, Haiku 4.5): −22.7% tokens. Weak evidence.
- **Sources:** https://arxiv.org/abs/2604.04979 (Apr 4 2026); https://arxiv.org/abs/2607.02911 (Jul 3 2026); https://arxiv.org/abs/2607.18213 and https://arxiv.org/html/2607.18213 (Jul 20 2026); https://arxiv.org/abs/2601.07190 (Jan 12 2026).
- **Token Bill:** note the contrast with F3, where tool outputs were 3.3% of cost. Whether tool outputs or the harness prefix dominates depends on the harness and on trajectory length. Token Bill must compute **cost share by content class for each workload**, and only then rank compression opportunities.

### F18 — Logs and test output: simple routers beat LLM summarizers; vendor token claims do not show up on bills — moderate
- **Claim:**
  - LogDx-CI (May 2026) compared 11 reducers on 35 real GitHub Actions failures. A hybrid rule (use grep if it yields ≤120k tokens, otherwise an error-category extraction or tail-200) scored best (0.670 at about 19.8k tokens per case), **4.5x fewer tokens** than grep alone at the same quality. LLM map-reduce summarizers did worse and cost more ($0.18 to $1.75 per case).
  - Claude Code's docs show a PreToolUse hook that filters test output to failures only, and a hook that greps a 10k-line log for ERROR lines. They describe the reduction as going from tens of thousands of tokens to hundreds.
  - RTK's README claims 60-90% token reduction on common dev commands. The README itself says this is *bash-output* reduction, not bill reduction. The independent billed study (F3) measured RTK v0.44.1 at **−2.7% billed cost**.
- **Sources:** https://arxiv.org/html/2605.28876 (May 26 2026); https://code.claude.com/docs/en/costs (live doc, fetched 2026-09-23); https://github.com/rtk-ai/rtk (README opened 2026-09-23; vendor claim, weak).
- **Token Bill:** detect **log and test dumps** in tool results (repeated line templates, ANSI escapes, passing-test lines, progress bars). Estimate reducible tokens with deterministic reducers such as grep+tail and failure-only output. Generate a **ready-to-install hook** config as the fix text. Always show both the token reduction and the replayed billed-dollar effect.

### F19 — Structured-data formats: compact JSON is a safe win; TOON is contested — moderate/weak
- **Claim:**
  - TOON's own benchmark (4 models, 244 questions, o200k tokenizer, not Claude's): pretty JSON used 4,308 tokens at 71.4% accuracy, **compact JSON 2,892 tokens (−33%)** at 69.0%, and TOON 2,474 tokens (−42.6% against pretty) at 72.2%.
  - An independent benchmark (Oct 28 2025) found TOON was **never** the best format. It did worse on nested data (43.1% against 62.1% for YAML).
  - A Feb 2026 preprint found plain JSON best for *generation*, and TOON's savings are cancelled by prompt overhead on short contexts.
- **Sources:** https://toonformat.dev/guide/benchmarks (v4.1.1, fetched 2026-09-23); https://www.improvingagents.com/blog/toon-benchmarks/ (Oct 28 2025); https://arxiv.org/abs/2603.03306 (Feb 8 2026).
- **Token Bill:** detect **pretty-printed JSON** in tool results and tool definitions (the ratio of indentation whitespace to total characters). Estimate the savings from minifying it at insertion time, which is lossless. Do not recommend exotic formats by default. Offer them as an A/B candidate for tabular payloads only.

### F20 — Compressing the static prefix (tools, skills, CLAUDE.md, MCP) pays twice, on writes and on every read — strong
- **Claim:**
  - Anthropic's Tool Search Tool: **85%** fewer tokens for tool definitions, and Opus 4 accuracy rose from 49% to 74% (Opus 4.5: 79.5% to 88.1%). Deferred tools **do not break caching** because they are left out of the initial prefix.
  - Programmatic tool calling: −37% tokens (43,588 to 27,297).
  - Code execution with MCP: 150,000 to **2,000** tokens (−98.7%) by filtering results in the execution environment.
  - SkillReducer (Mar-Jun 2026, 55k skills analyzed): skill descriptions −48%, bodies −39%, and **+2.8% quality**.
  - Claude Code defers MCP tool definitions by default and advises keeping CLAUDE.md under 200 lines and moving workflows into on-demand skills. Its MCP output warning fires at 10,000 tokens and the default cap is 25,000 (`MAX_MCP_OUTPUT_TOKENS`). Bash output is capped at 30,000 characters by default (`BASH_MAX_OUTPUT_LENGTH`).
- **Sources:** https://www.anthropic.com/engineering/advanced-tool-use (Nov 24 2025); https://www.anthropic.com/engineering/code-execution-with-mcp (Nov 4 2025); https://arxiv.org/abs/2603.29919 (rev. Jun 24 2026); https://code.claude.com/docs/en/costs, https://code.claude.com/docs/en/mcp, https://code.claude.com/docs/en/env-vars (live docs, fetched 2026-09-23).
- **Token Bill:** a **prefix-bloat analyzer** that ranks tool definitions, system-prompt sections, skills and memory files by token cost × calls × (α on write, β on read). It flags defer_loading candidates (tools never called in the trace) and oversized descriptions. This is "compress-then-cache" in its most reliable form. It fits F3's finding that the harness prefix is about 75% of cost in short Claude Code tasks.

### F21 — Longer context degrades quality, so compression can be quality-positive — strong
- **Claim:**
  - Chroma "Context Rot" (Jul 14 2025): 18 models including Claude 4, GPT-4.1 and Gemini 2.5 degrade as input grows, even on simple tasks, and a single distractor already hurts.
  - "Lost in the Middle" (TACL): performance follows a U-shape over where the relevant information sits.
  - LongLLMLingua gained +21.4% (F12). The Empirical Study at the ICLR'25 workshop found that moderate compression *improves* LongBench results, while compression hurts more on long contexts than on short ones.
  - Anthropic's compaction docs cite quality degradation as conversations grow as a reason to compact.
- **Sources:** https://www.trychroma.com/research/context-rot (Jul 14 2025); https://arxiv.org/abs/2307.03172 (final Nov 20 2023); https://arxiv.org/abs/2505.00019 (Apr 24 2025); https://platform.claude.com/docs/en/build-with-claude/compaction (fetched 2026-09-23).
- **Token Bill:** frame compression recommendations as "cost down, quality risk bounded", backed by the guardrails in F3, F10 and F11. Include a context-length histogram per workload so reviewers can see the share of calls above 100k or 200k tokens.

### F22 — Token counts are model-specific: the new tokenizer adds about 30%; exact counting is free — strong
- **Claim:** Claude 4.7+ and the Fable/Mythos models produce about **30% more tokens** for the same text, and the increase depends on content. The count_tokens endpoint is **free** (rate limits of 5,000, 10,000 and 20,000 RPM by tier), gives an estimate, does not bill system-added tokens, does not use caching logic, and rejects server tools and the MCP connector. Anthropic's rule of thumb is about 4 characters per token for English, which is only a rough estimate. Token Bill uses 3.7 characters per token.
- **Sources:** https://platform.claude.com/docs/en/build-with-claude/token-counting (fetched 2026-09-23); https://platform.claude.com/docs/en/about-claude/pricing (tokenizer note, fetched 2026-09-23).
- **Token Bill:** compression ratios must be measured in *the target model's tokens*, not characters. Add (a) per-trace, per-model calibration of characters per token from billed totals, which Token Bill already partly does by scaling; (b) an opt-in exact mode that calls count_tokens on segments (the user's key, rate-limited, with a privacy notice); (c) a warning when a trace mixes tokenizer generations.

### F23 — Claude Code telemetry lets Token Bill size compression opportunities across a fleet without logging content — strong
- **Claim:**
  - OTel metrics: `claude_code.token.usage` with type input/output/cacheRead/cacheCreation and attributes model, query_source (main/subagent/auxiliary), effort, skill.name, plugin.name, mcp_server.name and mcp_tool.name. Also `claude_code.cost.usage`.
  - With enhanced tracing, the `claude_code.tool` span carries **result_tokens** (the approximate size of each tool result). Content is redacted unless enabled with flags such as OTEL_LOG_TOOL_CONTENT.
  - Claude Code's `/usage` now shows the cache hit rate, misses with likely causes, and "expected rebuilds" (compaction or tool-result clearing).
  - The documented enterprise average is about **$13 per developer per active day** and **$150-250 per developer per month**.
- **Sources:** https://code.claude.com/docs/en/monitoring-usage; https://code.claude.com/docs/en/costs; https://code.claude.com/docs/en/prompt-caching (live docs, fetched 2026-09-23).
- **Token Bill:** an **OTel ingestion adapter** that ranks, for each tool and MCP server, result_tokens × lifetime × price. That produces a fleet-wide "top compression targets" list for thousands of developers from sizes alone, with no prompt content. It is a much easier privacy review for an enterprise lab. It should also separate "expected rebuilds" (compaction and clearing) from breakers.

### F24 — When to compact: warm for summarizing, cold for clearing, at thresholds, and at natural breaks — moderate
- **Claim:**
  - Claude Code's compaction sends the *same* system prompt, tools and history plus a summarization instruction, so when the cache is warm the summarization call reads the cached prefix. After an idle gap it reprocesses everything uncached, which is why `/compact` costs most when resuming an old session. `/rewind` returns to a prefix that is already cached, and `/clear` costs nothing.
  - Claude's engineering blog (Apr 30 2026) calls this "cache-safe forking". It says Claude Code monitors cache hit rate like uptime and declares SEVs when it drops.
  - OpenHands condenses only at a size threshold, achieving up to 2x lower per-turn cost with SWE-bench resolution of 54% against 53%.
  - A keepalive-economics preprint puts the optimal Anthropic ping at about every 4 minutes, with post-pause cost up to 12.5x lower and keepalive breaking even at about 46 minutes of idle time.
- **Sources:** https://code.claude.com/docs/en/prompt-caching (fetched 2026-09-23); https://claude.dev/blog/lessons-from-building-claude-code-prompt-caching-is-everything/ (Apr 30 2026); https://www.openhands.dev/blog/openhands-context-condensensation-for-more-efficient-ai-agents (Apr 9 2025); https://arxiv.org/abs/2607.19214 (rev. Jul 24 2026).
- **Token Bill:** a **compaction-timing** breaker that flags compactions run cold (full uncached summarization), compactions run moments before a task boundary (wasted), and client-side summarizers that change the system prompt or tools (no cache-safe fork). Price each against the best alternative.

### F25 — Output-side compression (adjacent to this track) — moderate
- **Claim:** Chain of Draft matches chain-of-thought accuracy with **as little as 7.6%** of the reasoning tokens. Prompting for unformatted code output saves about 27% of output tokens on GPT-4o for Java, C++ and C# (F16). Output tokens cost 5x input (for example Sonnet 4.6 at $15 against $3). In the Claude Code study, output was 10.4% of cost (F3).
- **Sources:** https://arxiv.org/abs/2502.18600 (rev. Mar 3 2025); https://arxiv.org/abs/2508.13666; https://arxiv.org/html/2607.12161.
- **Token Bill:** report the output share of cost and the output-to-input ratio for each workload. Hand off to the effort and verbosity recommendations owned by other tracks.

### F26 — How to evaluate a compression policy properly — moderate
- **Claim:** Four complementary protocols exist, and each catches failures the others miss:
  - Paired, provider-billed A/B runs with bootstrap confidence intervals and success-adjusted cost (F3).
  - Probe-based evaluation of summaries along accuracy, artifact, continuity and instruction axes (F10).
  - Next-action preservation: the compressed observation must induce the same next action (CoACT, F17).
  - Contrastive failure analysis between compressed and uncompressed runs (ACON, F7).
- **Sources:** https://arxiv.org/html/2607.12161; https://factory.com/news/evaluating-compression; https://arxiv.org/abs/2607.02911; https://arxiv.org/abs/2510.00615.
- **Token Bill:** a `tokenbill ab` command that takes two sets of traces (baseline and policy) with optional per-run success labels. It outputs billed $/task and $/success with confidence intervals, turn-count deltas, re-read deltas and a verdict. This is the evidence an enterprise lab will ask for.

### F27 — Where dynamic and compressed content goes matters: exclude it from the cached block — moderate
- **Claim:** Across OpenAI, Anthropic and Google on DeepResearch Bench (500+ sessions, 10k-token system prompts), prompt caching cut cost **41-80%** and time to first token 13-31%. Controlling the cache blocks strategically (dynamic content at the end, no dynamic function lists, excluding dynamic tool results) gave more consistent benefits than naive full-context caching, which could *increase* latency.
- **Sources:** https://arxiv.org/abs/2601.06007 (rev. Jan 31 2026).
- **Token Bill:** when a trace shows compressed or query-dependent content placed *before* a stable segment, the fix text should say to move it after the breakpoint. This extends the existing breaker detectors to cover content whose bytes change from call to call.

---

## 3. Compression ratio versus quality: consolidated table

| Method (venue/date) | Access needed | Typical ratio | Quality effect reported | Cache-compatible? |
|---|---|---|---|---|
| LLMLingua (EMNLP'23) | small local LM | up to 20x | little loss (GSM8K/BBH) | only if query-agnostic, compressed once |
| LongLLMLingua (ACL'24) | small local LM | ~4x | **+21.4%** NQ; −94% cost on LooGLE | no (query-aware) |
| LLMLingua-2 (ACL Findings'24) | local classifier | 2-5x | robust across LLMs; reliable rate adherence | yes if applied once (offline or at insertion) |
| Selective Context (EMNLP'23) | small local LM | 2x | −0.023 BERTScore, −0.038 faithfulness | yes if applied once |
| RECOMP (ICLR'24) | trained compressor | ~16x (to 6%) | minimal loss (LM, ODQA) | per query (RAG suffix), so fine after the breakpoint |
| Extractive reranker (Jha'24) | reranker | up to 10x | minimal loss | per query, after the breakpoint |
| Provence (ICLR'25) | local pruner | adaptive | negligible drop | per query, after the breakpoint |
| LongCodeZip (ASE'25) | small local LM | up to 5.6x | no degradation | yes if applied once |
| SWE-Pruner (2026) | 0.6B skimmer | 23-54% agent tokens; 14.84x single-turn | success equal or better | insertion-time |
| Squeez (2026) | 2B pruner | ~12x (−92%) | 0.86 evidence recall | insertion-time |
| CoACT (2026) | trained compressor | −33% total tokens | near baseline | insertion-time |
| Code formatting removal (ICSE'26) | none (deterministic) | −24.5% input | no accuracy loss | yes (deterministic) |
| Compact JSON (TOON bench) | none (deterministic) | −33% vs pretty | 69.0 vs 71.4 acc. (within CI) | yes (deterministic) |
| Log router grep+tail (LogDx-CI'26) | none (deterministic) | 4.5x vs grep | same quality | yes (deterministic) |
| Observation masking M=10 (NeurIPS-W'25) | none | ~−50% cost | solve rate equal or better | rewrites history, so batch it |
| AgentDiet (FSE'26) | cheap LLM | −40 to −60% input | pass −1 to +2 pts | edits s−2 only, which limits invalidation |
| ACON (ICML'26) | LLM or distilled | −26 to −54% peak | up to +46% (small models) | rewrites history |
| Compaction (Factory/Anthropic/OpenAI) | provider LLM | 98.6-99.3% | 3.35-3.70 / 5; artifact tracking weak | a one-time rebuild; summary is cacheable |
| Gist / ICAE / 500x / AutoCompressors | model weights | 4x-480x | 62-100% retained | not applicable to closed APIs |

---

## 4. What Token Bill should build

The positioning: **"the only tool that tells you, in billed dollars and with confidence intervals, whether a compression or context policy actually saves money on your workload, and the cheapest cache-safe way to do it."** Every competitor sells compression by the token (RTK 60-90%, proxies). The best independent evidence says token reduction is a weak predictor of bill reduction (r = 0.15) and can even raise the bill (+6.8%, +48.4%). That gap is Token Bill's opening.

### P0: correctness and trust (needed before an enterprise lab review)
1. **Complete the usage model.** Parse and price `usage.iterations` (compaction); `cache_creation.ephemeral_5m_input_tokens` and `ephemeral_1h_input_tokens` (1h writes cost 2x); `context_management.applied_edits[].cleared_input_tokens`; `server_tool_use`. Flag traces where only top-level fields exist but compaction blocks are present, because those are undercounted. Add the claude-opus-5-5 pricing row (β = 0.05). (F9, F4, §1.1) — effort **S-M**
2. **Tokenizer-aware attribution.** Calibrate characters per token for each model and trace from billed totals. Add an opt-in `--exact-tokens` mode using the free count_tokens endpoint, and warn when a trace mixes tokenizer generations. All compression ratios should be reported in target-model tokens. (F22) — effort **S-M**

### P1: the differentiator, a cache-aware compression simulator
3. **Content classifier for every block in every call.** Classes: system prompt sections, tool definitions (per tool), skills and memory, user text, assistant text, thinking, and tool_result split by tool name and subtype (file read, search/grep, test/build output, logs, JSON/API payload, web fetch, diff). For each block record tokens, **lifetime** (how many calls it stays in context), and **dollars by billing category** (written at α, read at β × lifetime, uncached). This yields "cost share by content class", the number that separates F3-type workloads from F17-type ones. — effort **M**
4. **Waste detectors that need no LLM** (each priced with the replay engine):
   - *duplicate reads* (the same file or URL hash read more than once);
   - *expired or unreferenced tool results* (never lexically referenced later, as in AgentDiet's "expired" class);
   - *pretty-printed JSON*;
   - *code formatting overhead* (in read-only contexts);
   - *log and test dumps* (template repetition, ANSI codes, passing-test lines);
   - *oversized tool results* (>10k tokens, Claude Code's own warning threshold);
   - *prefix bloat* (unused tool definitions that should be deferred, oversized CLAUDE.md or skills);
   - *query-aware compression that breaks caching* (from F2);
   - *edit-churn* (from F4);
   - *cold compaction* and *compaction without a system-prompt breakpoint* (F9, F24).

   (F6, F16, F18, F19, F20) — effort **M-L**
5. **Counterfactual transform library plus replay.** Deterministic transforms applied either at **insertion time** (JSON minify, whitespace strip in read-only contexts, log router grep+tail, failure-only test output, head/tail truncation with a pointer, duplicate-read elision) or at **rewrite time** (observation masking with window M, a `clear_tool_uses` emulator with trigger/keep/clear_at_least, a compaction emulator with a threshold and a configurable summary ratio defaulting to about 98.6-99.3%). Replay each through the existing cache simulator and produce new scenario columns such as `insert-compress`, `mask-M10` and `clear-policy*` next to as-billed, no-cache, optimal and fixed. Print r\*, K\* and the extra-turn break-even sensitivity (§1.2-1.4) for each recommendation. — effort **L**
6. **Policy optimizer.** Grid-search the context-editing and compaction parameters over the recorded traces for the lowest replayed cost, subject to guardrails. Emit ready-to-paste configs: `context_management` JSON, Claude Code hooks (PreToolUse filters, as in the docs), `MAX_MCP_OUTPUT_TOKENS` / `BASH_MAX_OUTPUT_LENGTH` settings, and compaction instructions. (F4, F8, F18, F20) — effort **M**

### P2: enterprise scale and proof
7. **`tokenbill ab`: success-adjusted paired evaluation.** Baseline versus policy trace sets, with per-run success labels (from test pass or fail, or user-provided). Outputs billed $/task and $/success with bootstrap confidence intervals, plus deltas in turns per task, re-reads and retrieval calls, and an edit-anchor safety check. This is how an enterprise can hold vendors' "60-90%" claims to account. (F3, F10, F11, F26) — effort **M-L**
8. **Fleet ingestion without content.** Adapters for Claude Code OTel (`claude_code.token.usage`, `claude_code.tool` result_tokens, query_source, mcp_server.name) and Claude Code session logs (already on the SPEC roadmap). Roll up by team, repo, tool and MCP server into "top compression targets" and "expected rebuilds versus breakers". Use sizes and hashes only by default. (F23) — effort **L**
9. **Optional runtime (later, behind a flag).** A **cache-safe** insertion-time compressor with only deterministic transforms, which never rewrites history or bytes that are already cached, and has a kill switch. An optional LLMLingua-2 or extractive plugin for RAG and document blocks under compress-then-cache. Ship it only after (7) shows positive, success-adjusted dollars on the customer's own traffic. (F2, F12, F14) — effort **XL**

### Guardrails to build into every recommendation
- Never compress instructions, few-shot examples or exemplars (up to −52%, F13). Never alter bytes that a later edit will match (F3 patch applicability 27/40 → 15/40).
- Only query-agnostic, deterministic transforms may touch anything before the cache breakpoint (F2).
- Always report extra turns, re-reads and retrieval calls next to dollars (F10, F11).
- Always convert "peak tokens" and "tokens" claims into billed dollars, including compressor and summarizer calls (F7, F5).

---

## 5. Threats to validity and caveats
- Most academic compression results use GPT-3.5/4-era or open models. Few test 2026 Claude models directly; exceptions are F2 (Sonnet 4.6), F3 (Haiku 4.5, Sonnet 5, Opus 4.8) and F6 (Claude 4 Sonnet). Magnitudes may differ.
- Several key 2026 results are single-author or v1 preprints (F2, F11, F17, F18, F24's keepalive paper). Treat their numbers as hypotheses that Token Bill can **test on customer data**.
- The derived formulas (§1) assume perfect cache hits within the TTL, a single breakpoint, and no quality-induced change in the trajectory. Token Bill's replay engine can relax the first two assumptions. The third needs A/B data (P2 item 7).
- Vendor numbers (RTK 60-90%, Factory scores, TOON benchmark) are labeled weak or moderate. The independent billed study (F3) contradicts vendor token claims for bill impact.

## 6. Open questions
1. Does Anthropic's server-side compaction iteration report cache reads? In other words, does the summarization pass bill the prefix at β when warm, as Claude Code's client-side fork does? The docs say only that it is "billed like any other request".
2. Is the ~3.5k-token "hot tier" hit-rate gap (F2) real and general across models and regions? Token Bill could measure it from billed data at enterprise scale.
3. How do OpenAI's encrypted `/responses/compact` items bill, and do they interact with OpenAI's automatic prefix caching? The docs found do not say.
4. How should task success be labeled in enterprise traces without ground truth? Candidate proxies: tests passing, PR merged, no retry within N minutes, no re-reads.
5. Once cache reads fall to 0.025x (Fable 5.1), rewriting history needs 74+ calls to pay back in a typical case. Will providers change how compaction is priced, or will clearing become mainly a quality and context-window tool rather than a cost tool?
6. Does the harness prefix dominate cost in short sessions (74.7% in F3) because the cache is scoped per directory and per machine? What share could cross-session or cross-user prefix sharing recover? This is mostly a caching-track question.

## 7. Source list (all opened 2026-09-23)
- Anthropic pricing — https://platform.claude.com/docs/en/about-claude/pricing (live doc)
- Anthropic prompt caching — https://platform.claude.com/docs/en/build-with-claude/prompt-caching (live doc)
- Anthropic context editing — https://platform.claude.com/docs/en/build-with-claude/context-editing (live doc)
- Anthropic compaction overview / on-demand / threshold — https://platform.claude.com/docs/en/build-with-claude/compaction ; …/compaction-on-demand ; …/compaction-threshold (live docs)
- Anthropic token counting — https://platform.claude.com/docs/en/build-with-claude/token-counting (live doc)
- Claude blog, context management — https://claude.com/blog/context-management (Sep 29 2025)
- Anthropic, effective context engineering — https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents (Sep 29 2025)
- Anthropic, advanced tool use — https://www.anthropic.com/engineering/advanced-tool-use (Nov 24 2025)
- Anthropic, code execution with MCP — https://www.anthropic.com/engineering/code-execution-with-mcp (Nov 4 2025)
- Claude blog, prompt caching is everything — https://claude.dev/blog/lessons-from-building-claude-code-prompt-caching-is-everything/ (Apr 30 2026)
- Claude Code docs: costs, prompt caching, monitoring, MCP, env vars — https://code.claude.com/docs/en/costs ; /prompt-caching ; /monitoring-usage ; /mcp ; /env-vars (live docs)
- OpenAI compaction guide — https://developers.openai.com/api/docs/guides/compaction (live doc, undated)
- Manus, context engineering — https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus (Jul 18 2025): KV-cache hit rate is its key metric; 10x cached/uncached price gap on Sonnet; ~100:1 input:output; restorable compression (drop the content, keep the URL or path); ~50 tool calls per task
- LLMLingua — https://arxiv.org/abs/2310.05736 (EMNLP 2023; v2 Dec 6 2023)
- LongLLMLingua — https://arxiv.org/abs/2310.06839 (ACL 2024; v2 Aug 12 2024)
- LLMLingua-2 — https://arxiv.org/abs/2403.12968 (Findings ACL 2024; v2 Aug 12 2024)
- Selective Context — https://arxiv.org/abs/2310.06201 (EMNLP 2023; Oct 9 2023)
- RECOMP — https://arxiv.org/abs/2310.04408 (Oct 6 2023; ICLR 2024 proceedings page above)
- Gist tokens — https://arxiv.org/abs/2304.08467 (NeurIPS 2023; v3 Feb 12 2024)
- ICAE — https://arxiv.org/abs/2307.06945 (ICLR 2024; v4 May 8 2024)
- 500xCompressor — https://arxiv.org/abs/2408.03094 (Aug 6 2024)
- AutoCompressors — https://arxiv.org/abs/2305.14788 (EMNLP 2023; rev. Nov 4 2023)
- Context distillation — https://arxiv.org/abs/2209.15189 (Sep 30 2022)
- Prompt compression survey — https://arxiv.org/abs/2410.12388 (v2 Oct 17 2024)
- Characterizing prompt compression — https://arxiv.org/abs/2407.08892 (Jul 11 2024)
- Empirical study on prompt compression — https://arxiv.org/abs/2505.00019 (Apr 24 2025)
- Prompt compression in the wild — https://arxiv.org/html/2604.02985v1 (Apr 3 2026)
- Linguistic-rule compressors — https://arxiv.org/abs/2607.25335 (Jul 28-29 2026): deterministic, CPU-only, comparable to learned compressors at light-to-moderate ratios
- Provence — https://arxiv.org/abs/2501.16214 (Jan 2025; ICLR 2025)
- Complexity Trap (observation masking) — https://arxiv.org/abs/2508.21433 (v3 Oct 27 2025)
- AgentDiet — https://arxiv.org/abs/2509.23586 (rev. Mar 15 2026; FSE 2026)
- ACON — https://arxiv.org/abs/2510.00615 (v3 Jun 1 2026; ICML 2026)
- Active Context Compression — https://arxiv.org/abs/2601.07190 (Jan 12 2026)
- Factory compression evaluation — https://factory.com/news/evaluating-compression (Dec 16 2025)
- What does context compression cost an agent — https://arxiv.org/abs/2608.16370 (Aug 17 2026)
- Token Reduction Is Not Cost Reduction — https://arxiv.org/html/2607.12161 (Jul 2026)
- Cache-Aware Prompt Compression — https://arxiv.org/abs/2607.15516 (Jul 17 2026)
- Don't Break the Cache — https://arxiv.org/abs/2601.06007 (rev. Jan 31 2026)
- Keepalive economics — https://arxiv.org/abs/2607.19214 (rev. Jul 24 2026)
- LongCodeZip — https://arxiv.org/abs/2510.00446 (Oct 1 2025; ASE 2025)
- SWE-Pruner — https://arxiv.org/abs/2601.16746 (v4 May 7 2026)
- SWE-Pruner Pro — https://arxiv.org/abs/2607.18213 (Jul 20 2026)
- CoACT — https://arxiv.org/abs/2607.02911 (Jul 3 2026)
- Squeez — https://arxiv.org/abs/2604.04979 (Apr 4 2026)
- Hidden cost of readability — https://arxiv.org/abs/2508.13666 (Aug 19 2025; ICSE'26)
- LogDx-CI — https://arxiv.org/html/2605.28876 (May 26 2026)
- SkillReducer — https://arxiv.org/abs/2603.29919 (rev. Jun 24 2026)
- TOON benchmarks — https://toonformat.dev/guide/benchmarks (v4.1.1); independent: https://www.improvingagents.com/blog/toon-benchmarks/ (Oct 28 2025); https://arxiv.org/abs/2603.03306 (Feb 8 2026)
- Chroma context rot — https://www.trychroma.com/research/context-rot (Jul 14 2025)
- Lost in the Middle — https://arxiv.org/abs/2307.03172 (TACL; final Nov 20 2023)
- Chain of Draft — https://arxiv.org/abs/2502.18600 (rev. Mar 3 2025)
- Compactor (KV compression) — https://arxiv.org/abs/2507.08143 (rev. Dec 8 2025)
- OpenHands condensation — https://www.openhands.dev/blog/openhands-context-condensensation-for-more-efficient-ai-agents (Apr 9 2025)
- RTK (vendor claim) — https://github.com/rtk-ai/rtk (README, undated; opened 2026-09-23)
