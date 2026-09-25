# Fact-check: gap-agent-frameworks-mcp-harness

Checked 2026-09-23. Each cited GitHub issue or PR was opened with `gh api`, which returned its title, created, merged and closed dates, state and body. Framework source was read from `main` with `gh api .../contents`. arXiv abstracts and HTML tables were fetched directly. Docs pages were read with WebFetch or curl. PyPI and npm numbers were re-queried. Both local measurement scripts were re-run. Scratch files are in `research/vtmp_gapfw/`.

## Tally

| Verdict | Count | Finding ids |
|---|---|---|
| confirmed | 20 | fw-telemetry-conventions, langgraph-recursion-10007, loop-defaults-matrix, crewai-no-tail-cache, history-trim-cache-breakers, tool-subset-churn, replay-serialization-drift, pydantic-cache-observability-reference, adk-anthropic-cache-late, group-chat-broadcast-reread, mast-loop-failures, mas-pruning-research, agentic-plan-caching, mcp-sampling-deprecated-shadow-spend, mcp-spec-cache-hints, litellm-gateway-injection, openai-explicit-breakpoints-frameworks, otel-openinference-importer, edit-format-output-multiplier, predicted-outputs-niche |
| corrected | 5 | openai-agents-sdk-cache-gaps, dynamic-system-injection, component-cost-multipliers, mid-conv-system-tool-addition-support, adapter-adoption-ranking |
| unverifiable | 0 | |
| refuted | 0 | |

25 findings in total.

The most important corrections:

1. **component-cost-multipliers.** The "$0.228 vs $0.398" in the Efficient Agents abstract conflicts with the paper's own Table 7.
   - Efficient Agents costs **$0.285** per task overall. $0.228 is its Level-1 cost.
   - The 28.4% figure is the drop in **cost** (0.398 → 0.285).
   - Cost-of-pass fell 0.75 → 0.55, a 26.7% improvement.
2. **dynamic-system-injection.** Most cited cases are **already fixed**. ADK moved `preload_memory` out of `system_instruction` on 2026-08-12. The report writes them in the present tense.
3. **openai-agents-sdk-cache-gaps.** OpenAI's guide contains no "oversplitting disperses traffic" warning.

---

## Per-finding notes

### fw-telemetry-conventions: confirmed
- **crewAI #6788.** Created 2026-08-03, closed 2026-08-05. Its table shows call 3 with `input_tokens` 9 and output 8, so `total_tokens` = 17 while 17,119 tokens moved.
- **crewAI #7259.** Created 2026-09-04, open. The repro gives 155 real tokens against 310 reported.
- **strands #3546.** Created 2026-07-29, open. Sum 19,403 vs accumulated 12,946: an over-count of 6,457 (+50%).
- **MAF #6823.** Created 2026-06-30, closed 2026-09-15. .NET Foundry Hosting reported `cached_tokens=0` while OTel showed 34,304 of 34,847 cached.
- **MAF convention split.** Checked in Python source:
  - `agent_framework_anthropic/_chat_client.py:1303` sets `input_token_count = usage.input_tokens`, Anthropic's exclusive count.
  - `agent_framework_openai/_chat_client.py:3915` uses OpenAI's inclusive `usage.input_tokens`.
- **openllmetry #4449.** Created 2026-09-01, open. The Anthropic package folds cache into input tokens; the Bedrock package does not.
- **langchain #37761** (2026-05-29, open) and **langgraph #8094** (2026-06-16, open) both report v3 streaming dropping the cache details.
- **ADK #5835.** Created 2026-05-24. Closed 2026-08-19 by commit d0b33a0, "fix: cache read write token counts in LiteLLM and Anthropic models".
- **pydantic-ai #8665.** Created 2026-09-23, open. The 5m/1h write split is lost.
- **OTel semconv `anthropic.md`.** Note [27] states the inclusive rule. Last commit 2026-09-01 (5ca9052b).
- **"At least 14" bugs.** The count holds. Beyond the cited issues I found more dated 2026 bugs: MAF #6639 and #7589, openinference #3610 and #3487, langchain #40668/#40666, openai-agents #3772, vercel #18905, llama_index #20506. That makes about 18.

### langgraph-recursion-10007: confirmed
- **PR #6676.** Merged 2026-01-12 18:43Z. The diff changes `"25"` to `"10000"`, and the quote "burden really should be on the user" is accurate.
  - Oddity: the PR body says "bumping up to 1000", but the code says 10000.
- **Tag `1.0.6`.** Published 2026-01-12 20:33Z. The tagged `_config.py` contains 10000.
- **PR #7355.** Merged 2026-03-30 and changes the value to 10007. `main` still shows 10007.
- **LangChain docs.** `ModelCallLimitMiddleware` and `ToolCallLimitMiddleware` both say "Defaults to no limit".
- **"About 5,000 model calls".** This is an inference (about 2 supersteps per ReAct iteration), not a measured figure.

### loop-defaults-matrix: confirmed
All values were checked in source on `main`:

| Framework | Default | Where |
|---|---|---|
| ADK | `_DEFAULT_MAX_LLM_CALLS = 500` | source |
| MAF | `DEFAULT_MAX_ITERATIONS: Final[int] = 40`; workflows `_const.py` = 100 | source |
| Pydantic AI | `request_limit = 50` | source |
| OpenAI Agents SDK | `DEFAULT_MAX_TURNS = 10` | source |
| CrewAI | `max_iter` default 25; `max_retry_limit` default 2 | `base_agent.py` |
| LlamaIndex | `DEFAULT_MAX_ITERATIONS = 20` | source |
| AutoGen | `max_tool_iterations` "When set to 1 (default)" | docstring |
| Vercel `ToolLoopAgent` | `isStepCount(20)` | docs |
| Vercel `WorkflowAgent` | "does not apply a default step limit" | docs |

- **crewAI #3836** (2025-11-05): "~138K tokens per call vs ~13K expected".
- **Efficient Agents Table 3.** Max steps 8 gives cost-of-pass 0.70 and accuracy 52.73; 12 gives 0.98 and 53.33 (+0.6 pt).

### crewai-no-tail-cache: confirmed
- **PR #5774.** Created 2026-05-11, merged 2026-05-12.
- **`crew_agent_executor.py` on `main`.** `mark_cache_breakpoint` is applied only to the system message and the initial user/task prompt.
- **`anthropic/completion.py` on `main`.**
  - Stamps only `{"type":"ephemeral"}`: no TTL option.
  - Matches by content, so only the initial task message is stamped.
  - Comments say markers on assistant or tool messages are ignored.
  - No tail breakpoint and no top-level automatic caching.
- **RFC #5921** (2026-05-25, closed 2026-08-27): "Each agent's system prompt is treated as a separate cache key".
- **#5886** (2026-05-21, closed 2026-08-28): Groq `BadRequestError`, "property 'cache_breakpoint' is unsupported".

### history-trim-cache-breakers: confirmed
- **Strands #4176** (2026-09-04, open) has the quote verbatim.
- **Strands window.** `sliding_window_conversation_manager.py:38` sets `window_size: int = 40`.
- **langchain #37815** (2026-06-01, open) shows eviction re-firing every turn with a checkpointer. The repro uses trigger 10k and keep 5; the documented defaults are 100000 and 3, and the report states those correctly. The cache consequence is an inference, and the report says so.
- **AG2 `views_and_skills.mdx`.** Last commit 2026-06-27. `NamedWindowedSummary` is the N-party default and prepends `CompactionSummary` "...elided N turns".
- **deepagents #5319** (2026-08-05, open): summarization bypasses middleware and changes the prefix.
- **Efficient Agents Table 5.** Summarized memory: 367K tokens at 51.52%. Without extra memory: 243K at 53.33%. Simple memory: 194K at 56.36%.

### openai-agents-sdk-cache-gaps: corrected
Confirmed:
- **Handoffs docs** say the new agent "gets to see the entire previous conversation history". `nest_handoff_history` is an "opt-in beta", disabled by default.
- **#5085** (2026-09-18, open): 0 of 42 subagent calls cached; the parent hit 78–90%.
- **#2784** (2026-03-26, closed not_planned): 96.7% → 0.0% hit rate on the first file input.
- **Auto cache key.** `run_internal/prompt_cache_key.py` and `run_grouping.py` produce `agents-sdk:run:{uuid4().hex}` when no conversation, session or group is set.
- **#3008** (2026-04-23). The maintainer rejected a `ModelSettings.cache_system_prompt` and pointed to LiteLLM `cache_control_injection_points`. The issue is labelled "completed", but in substance it was declined.

Correction:
- **What the guide says.** OpenAI's caching guide does say routing uses a hash of the prefix plus `prompt_cache_key`, which "helps optimize cache routing on models before GPT-5.6". It also says "aim for about 15 requests per minute in total across all prefixes using each key".
- **What it does not say.** It contains no warning that "oversplitting keys disperses traffic". Its wording is: "Keep related requests on the same prompt_cache_key so they can reuse its cache", and "Partition higher-volume traffic across multiple keys using a stable, deterministic mapping".
- **So:** the cost of per-run keys is an inference from those lines, not a stated warning.
- **GPT-5.6 and later.** "OpenAI handles cache routing automatically; the key is not needed". The per-run-key concern applies only to earlier models.

### tool-subset-churn: confirmed
- **vercel #8166** (2025-08-20, open): "activeTools simply removes redundant tools".
- **vercel #14170** (2026-04-06, open): `prepareStep` `activeTools` "invalidates the entire prompt cache".
- **pydantic #6794** (2026-07-28, closed 2026-08-06) has two verbatim quotes:
  - "measured as a full cache miss on Anthropic"
  - "Deferred tool definitions are outside Anthropic's cache key entirely"
- **PR #6793** merged 2026-08-03. **LangChain PR #40758** merged 2026-09-23.
- **MCP 2026-07-28 changelog:** "Servers SHOULD return tools from tools/list in a deterministic order to enable client-side caching and improve LLM prompt cache hit rates."

### dynamic-system-injection: corrected
All the cited issues exist with the stated dates, but most describe behaviour that is **already fixed**. The report presents it as current.

- **ADK #3227.** Created 2025-10-20, closed "completed" 2026-07-21.
  - Commit be103fb (2026-08-12, "fix: keep preloaded context turn-scoped") moved `PreloadMemoryTool` output out of `system_instruction`. The current `preload_memory_tool.py` uses `llm_request._insert_transient_user_content(...)`.
  - `load_artifacts_tool.py` now uses `_append_dynamic_instructions` instead of `append_instructions`.
- **ADK #6216 and #6062.** Closed "completed" on 2026-06-27 and 2026-06-22 (#6062 via commit 7c7f1e7).
- **MAF #7700.** Created 2026-08-17, fixed 2026-08-19.
- **deepagents #1356.** Created 2026-02-17, fixed 2026-03-19.
- **deepagents #3639.** Created 2026-05-27 but **closed not_planned** 2026-05-28. The maintainer said the post-memory breakpoint "is deliberate" and is "not a correctness bug as described". Calling it "misplaced" is the reporter's view, not an established bug.
- **openai-agents #5085.** The instructions do contain "Today's date: Fri, 18 Sep 2026 (UTC)". Confirmed.

Corrected claim: several frameworks *had* dynamic text in the system prompt (ADK before about 2026-08-12, MAF before 2026-08-19, Deep Agents before 2026-03). Most are fixed on current `main`. These should be **version-gated** detector fingerprints, not current defaults. User-authored dated instructions, as in #5085, remain common.

### replay-serialization-drift: confirmed
- **vercel #18193** (2026-07-30, closed 2026-08-03): "turn 2 read only 3031" out of about 9,521 tokens of cached context, "even if i cache with 1h".
- **pydantic #6653** (2026-07-22, closed): Mistral chunk-array reconstruction busts the cache.
- **pydantic #6528** (2026-07-15, open) supports three claims:
  - about 1,000 request comparisons
  - structured-object fingerprinting "false-positives"
  - the only trustworthy signals are wire bytes and `cache_read_tokens`, with about 4 legitimate busts

### pydantic-cache-observability-reference: confirmed
- **PR #6529.** Merged 2026-07-17. `usage.py:206` defines `cache_hit_ratio`, with a docstring saying "`input_tokens` includes cached reads for every provider".
- **PR #6534.** Open. It adds `pydantic_ai.cache.hit_ratio`, `established_tokens` (per provider and model), `collapsed` and `wasted_tokens`. Alerts are suppressed for `ttl-expired` and model switches.
- **#7250** (2026-08-06, open): an `on_cache_bust` hook requested by a production user (escape.tech).
- "Reference design" is the report's opinion.

### adk-anthropic-cache-late: confirmed
- **#5395.** Created 2026-04-19. `cache_config` was ignored and cache tokens were dropped. The issue closed 2026-05-12, but the fix commit came later.
- **Commit "fix: emit Anthropic prompt cache breakpoints for ContextCacheConfig"** (2026-08-24T20:29Z, "Fixes #5395"). Current `_apply_cache_breakpoints` marks `tools[-1]`, the system block and the last cacheable message. It uses `ttl="1h"` when `use_one_hour_ttl(cache_config)`.
- **Cache-write counts.** Added by d0b33a0 on 2026-08-19.
- **`context_cache_config.py`.** `cache_intervals=10`, `ttl_seconds=1800`, `min_tokens=0`. adk.dev confirms these and "ADK Python v1.15.0".
- **Default `max_llm_calls`** is 500.

### group-chat-broadcast-reread: confirmed
- **MS Learn group-chat page.** `ms.date` and `updated_at` are 2026-09-21. It contains:
  - "All agents see the full conversation history"
  - "The orchestrator broadcasts the response to all other agents after each agent's turn"
  - For Python, function calls and results "are filtered before broadcast"
  - `UpdateHistory` is documented for Go
- **AutoGen `SelectorGroupChat` docs.** Model-based selection using "the conversation history and participants' name and description", with the response "broadcasted to all other participants".
- **Anthropic engineering post** (Jun 13, 2025):
  - "about 4× more tokens than chat interactions, and multi-agent systems use about 15× more tokens"
  - "token usage by itself explains 80% of the variance"

### mast-loop-failures: confirmed
- **arXiv 2503.13657.** v3 dated 2025-10-26. 1600+ traces, 7 frameworks, 14 modes, kappa 0.88.
- **The 7 frameworks** (v3 HTML): ChatDev, MetaGPT, HyperAgent, AppWorld, AG2, Magentic-One, OpenManus.
- **Loop-type failure rates:**

| Code | Failure mode | Share |
|---|---|---|
| FM-1.3 | Step repetition | 15.7% |
| FM-1.4 | Loss of history | 2.80% |
| FM-1.5 | Unaware of stopping conditions | 12.4% |
| FM-2.1 | Conversation reset | 2.20% |

- **arXiv 2512.08296.** v3 dated 2026-04-08. The range "+80.8% ... to −70.0%" is a **relative performance** change against a single-agent baseline, not a token or cost change. The finding is ambiguous on this point; the numbers are correct.

### mas-pruning-research: confirmed
All numbers are verbatim from the abstracts:

| Paper | arXiv | Date | Result |
|---|---|---|---|
| AgentPrune | 2410.02506 | 2024-10-03 | 28.1%–72.8% fewer tokens; $5.6 vs $43.7 |
| AgentDropout | 2503.18891 | 2025-03-24 | −21.6% prompt tokens, −18.4% completion tokens, "performance improvement of 1.14" |
| S²-MAD | 2502.04790 | v2 2025-04-10, NAACL 2025 Main | up to 94.5% fewer tokens, under 2.0% degradation |
| SupervisorAgent | 2510.26585 | v2 2026-03-02, ICLR 2026 | −29.68% tokens on GAIA/Smolagents with an LLM-free filter |
| Optima | 2410.08115 | v2 2025-02-18 | up to 2.8× performance with under 10% of tokens; needs SFT/DPO on Llama 3 8B |

### component-cost-multipliers: corrected
Efficient Agents (2508.02694, v1 2025-07-24), from the HTML tables:

- **Best-of-N (Table 2).** N=4 used 325K tokens vs 243K at N=1 (+34%). Accuracy 53.94 vs 53.33 (+0.6 pt). Confirmed.
- **Summarized memory (Table 5).** 367K vs 243K tokens (+51%). Accuracy 51.52 vs 53.33 (−1.8 pt). Confirmed.
- **Final configuration.** This is where the report is wrong. The abstract says "$0.398 to $0.228 ... 28.4% improvement in cost-of-pass". Table 7 says:

| Agent | Cost-of-pass | Accuracy | Cost (all levels) | Cost (Level 1) |
|---|---|---|---|---|
| OWL | 0.75 | 53.33 | $0.398 | — |
| Efficient Agents | 0.55 | 51.52 | $0.285 | $0.228 |

  - So $0.228 is Efficient Agents' **Level-1** cost.
  - 28.4% = (0.398 − 0.285) / 0.398, a **cost** reduction.
  - Cost-of-pass improves 26.7%. Retained performance is 96.6%.
  - Corrected claim: "96.6–96.7% of OWL's accuracy at $0.285 vs $0.398 per task (−28.4% cost; cost-of-pass 0.75 → 0.55, −26.7%). The abstract's $0.228 is the Level-1 figure."
- **ReWOO (2305.18323).** "5x token efficiency and 4% accuracy improvement on HotpotQA". Confirmed.
- **2505.18286.** "improves accuracy by 1.1-12% while reducing deployment costs by up to 20%". Confirmed.

### agentic-plan-caching: confirmed (with caveat)
- **arXiv 2506.14852.** v2 dated 2026-01-26, NeurIPS 2025. "reduce costs by 50.31% and latency by 27.28% on average while maintaining performance".
- **API use.** The experiments use GPT-4o via the OpenAI API as the planner, so API use is supported.
- **Caveat:**
  - Part of the saving comes from adapting cached plans with a small model (LLaMa-3.1-8B planner and actor).
  - Matching uses keyword extraction, not embeddings.
  - So the saving is not pure "reuse".

### mcp-sampling-deprecated-shadow-spend: confirmed
- **MCP 2026-07-28 changelog.** Deprecates Roots, Sampling and Logging (SEP-2577) with the advice "integrate directly with LLM provider APIs instead of Sampling". `includeContext` `thisServer`/`allServers` are deprecated.
- **Sampling page.** Marked "Deprecated ... as of protocol version 2026-07-28" and still has "Tools in Sampling".
- **2025-11-25 changelog.** "Add tool calling support to sampling via tools and toolChoice parameters (SEP-1577)".
- **claude-code #1785.** Created 2025-06-08, open, 58 comments. Motivated by pay-as-you-go costs for server LLM calls.
- **VS Code 1.101** (June 12, 2025). Supports sampling, with model access and a request log via "MCP: List Servers".
- **vscode #299336** (2026-03-05) shows `"model":"copilotcli/claude-sonnet-4.6"`.
- "Claude Code never supported" rests on #1785 still being open. That is moderate evidence.

### mcp-spec-cache-hints: confirmed
Every item appears in the 2026-07-28 changelog:
- deterministic `tools/list` order (minor change)
- `CacheableResult` with `ttlMs` and `cacheScope` (SEP-2549, minor change)
- OTel `traceparent`/`tracestate`/`baggage` in `_meta` (SEP-414, minor change)

One locator is off: "list endpoints no longer vary per-connection" is a **Major** change (SEP-2567), not one of the minor changes.

### litellm-gateway-injection: confirmed
- **LiteLLM tutorial:**
  - `cache_control_injection_points` works with `location: message`, `role: system` or `index: -1`, per model in the proxy `config.yaml`.
  - Supported providers include Anthropic, Bedrock, Vertex and OpenRouter.
  - The same injection points map to `prompt_cache_breakpoint` for OpenAI GPT-5.6+.
  - `anthropic_prompt_caching_ttl: '1h'` is supported.
- **LiteLLM completion docs:** `prompt_tokens` = "all prompt tokens including cache-miss and cache-hit input tokens".
- **pypistats `litellm` last_month** = 104,729,425.
- **Framework routes:** CrewAI has an `is_litellm` route, the Agents SDK has `extensions/models/litellm_model.py`, and ADK has `models/lite_llm.py`.

### openai-explicit-breakpoints-frameworks: confirmed
- **OpenAI guide:**
  - GPT-5.6+ cache writes cost 1.25×.
  - `prompt_cache_options.ttl` "only supported value, 30m".
  - Minimum is 1,024 tokens.
  - Explicit mode: content after the last breakpoint is processed "without a cache-write charge".
  - About 15 RPM per key on earlier models.
- **Agents SDK** `docs/models/index.md:457` shows `"prompt_cache_breakpoint": {"mode": "explicit"}`.
- **MAF sample** `client_prompt_caching.py` uses `prompt_cache_breakpoint` plus `prompt_cache_key`.
- **pydantic #7128** (2026-08-04, open).
- **Code search hits:** `langchain_openai/chat_models/base.py`; Vercel `packages/openai/.../convert-to-openai-*.ts` (`promptCacheBreakpoint`).
- **Zero hits** in crewAI, adk-python, harness-sdk and llama_index.

### otel-openinference-importer: confirmed
- **OpenInference `python/instrumentation`** lists 38 packages. They include ag2, agent-framework, autogen, autogen-agentchat, crewai, google-adk, langchain, llama-index, openai-agents, pydantic-ai, smolagents, strands-agents, claude-agent-sdk, mcp and litellm.
- **OpenLLMetry `packages`** includes crewai, langchain, llamaindex, openai-agents, mcp and litellm.
- **OpenInference spec** defines the span kinds LLM, CHAIN, TOOL and AGENT, plus `llm.token_count.prompt_details.cache_read` and `cache_write`.
- **Semconv inclusive rule:** as in the first finding.
- "~30 frameworks" is loose: the 38 packages include providers.

### mid-conv-system-tool-addition-support: corrected
- **Model list.** The bundled `prompt-caching.md` lists mid-conversation system and `tool_addition` for Opus 5, Opus 4.8, Fable 5, Fable 5.1, Mythos 5 and Mythos 5.1, not Sonnet 5.
- **Vercel PR #15674** merged 2026-05-28. Its title is "add support for claude-opus-4-8"; the body adds inline mid-conversation system messages and the beta header.
- **LangChain PR #40622** merged 2026-09-20. **PR #40758** merged 2026-09-23. **Pydantic PR #6793** merged 2026-08-03.
- **Absence in other frameworks.** Checked by grep of the Anthropic provider source: zero `tool_addition` or mid-conversation hits in CrewAI, ADK, MAF, Strands and LlamaIndex. The OpenAI Agents SDK has no native Anthropic model.
- **Correction:** Pydantic PR #6765 was **opened 2026-07-27 and merged 2026-07-30**. The finding gives 07-27 as if it were the ship date.

### edit-format-output-multiplier: confirmed
- **Re-run.** Re-ran `21_write_vs_edit.py` and `22_whole_file_multiplier.py`. The corpus has grown slightly with today's sessions (1,606 files), and the results match the stored `.out` files:
  - 949 edits, 13.6× aggregate, median 15.4×, IQR 6.4–36.6×.
  - Write-after-Read: 47 files, about 104K tokens.
  - Overwrite ratio 0.956.
- **Method caveat.** Only edits to files the agent itself created earlier in the thread are sampled.
- **Aider:** "slow and costly because the LLM has to return the entire file".
- **Diff-XYZ** (2510.12487, v2 2025-11-17): "search-replace format performs best for larger models".
- **Cursor blog** (2024-05-14): "fully rewritten file", "~1000 tokens". Vendor source.

### predicted-outputs-niche: confirmed
- **Models:** gpt-4o, gpt-4o-mini, gpt-4.1, gpt-4.1-mini, gpt-4.1-nano. Chat Completions `prediction` only.
- **Billing:** "Any rejected tokens are still billed like other completion tokens ... can introduce higher costs".
- **Incompatible with:** tools, n>1, logprobs, `max_completion_tokens`, and others.
- **Usage fields:** `accepted_prediction_tokens` and `rejected_prediction_tokens`.

### adapter-adoption-ranking: corrected
- **PyPI** (re-queried 2026-09-23): every number matches:

| Package | Downloads | Package | Downloads |
|---|---|---|---|
| mcp | 219,257,578 | langchain | 173,736,562 |
| langchain-core | 146,568,201 | litellm | 104,729,425 |
| langsmith | 79,571,527 | fastmcp | 49,844,419 |
| langgraph | 44,030,243 | strands-agents | 36,218,993 |
| claude-agent-sdk | 28,875,065 | pydantic-ai-slim | 25,824,399 |
| langchain-anthropic | 17,374,792 | openai-agents | 12,830,090 |
| google-adk | 9,902,757 | llama-index-core | 6,760,455 |
| pydantic-ai | 5,463,303 | deepagents | 5,064,593 |
| crewai | 3,492,375 | agent-framework-core | 952,119 |
| autogen-agentchat | 403,832 | semantic-kernel | 355,449 |
| ag2 | 191,248 | | |

- **npm** (2026-08-23 to 2026-09-21): @modelcontextprotocol/sdk 195.4M, ai 87.8M, @anthropic-ai/claude-agent-sdk 45.9M, @langchain/langgraph 12.1M, @openai/agents 6.16M, @mastra/core 5.53M. All match.
- **GitHub stars:** langchain 146,938; autogen 61,125; crewAI 58,950. All match.
- **Correction:**
  - The AutoGen README does say "Maintenance Mode".
  - The Semantic Kernel README says it "is now Microsoft Agent Framework" and that MAF is its "successor". It does **not** say "maintenance mode".
  - Corrected claim: "AutoGen is in maintenance mode; Semantic Kernel's README names MAF 1.0 as its successor."
