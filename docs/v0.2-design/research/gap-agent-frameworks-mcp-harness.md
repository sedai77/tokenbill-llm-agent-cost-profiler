# Token Bill gap research: agent frameworks, MCP beyond tools, harness output levers

Track: `gap-agent-frameworks-mcp-harness`. Snapshot date: **2026-09-23**. Research only; the Token Bill repo was not modified.

## 0. Method, and how to read this report

**Sources.**
- Every framework claim was checked against the framework's current source on `main` (read with `gh api`) or its official docs, fetched 2026-09-23.
- Every GitHub issue or PR was opened, and its creation or merge date is given.
- Paper numbers come from the arXiv abstract or HTML page of the version cited.
- WebSearch was unavailable for this track because the session search budget was exhausted. Discovery used GitHub issue and code search (`gh search`), official docs, and arXiv.

**Two local measurements** follow the privacy rules of `empirical.md`: only lengths and counts, and paths hashed in memory. The scripts are `21_write_vs_edit.py` and `22_whole_file_multiplier.py`, each with a `.out` file in the research directory. They cover one developer's Claude Code corpus of 1,601 transcript files and 44,702 unique responses.

**Evidence labels.**
- *strong*: primary source such as code, official docs, a merged PR, or a paper abstract.
- *moderate*: an open issue with a reproduction, a paper without independent replication, or an inference from code.
- *weak*: a vendor claim, or an inference not checked at runtime.

Anthropic API behaviour follows the bundled `claude-api` skill docs (`shared/prompt-caching.md`). Those docs cover:
- automatic top-level `cache_control`
- the 1h TTL at a 2× write cost
- mid-conversation `role:"system"` messages
- `tool_addition` / `tool_removal` behind `mid-conversation-tool-changes-2026-07-01`
- the 20-block lookback window
- the invalidation hierarchy

**Current versions (2026-09-23):**

| Framework | Version | Date |
|---|---|---|
| langchain | 1.4.2 | 2026-09-18 |
| langchain-anthropic | 1.7.4 | 2026-09-23 |
| langgraph | 1.2.12 | 2026-09-21 |
| crewai | 1.15.22 | 2026-09-16 |
| openai-agents (Python) | v0.22.3 | 2026-09-17 |
| pydantic-ai | v2.48.0 | 2026-09-23 |
| Vercel `ai` | 7.0.112 | 2026-09-23 |
| google-adk | 2.9.2 | 2026-09-18 |
| strands-agents | 1.57.0 | 2026-09-22 |
| llama-index | v0.14.25 | 2026-09-21 |
| Microsoft Agent Framework (Python) | 1.19.0 | 2026-09-18 |
| ag2 | v1.0.6 | 2026-09-21 |

AutoGen and Semantic Kernel are in maintenance mode, superseded by Microsoft Agent Framework (MAF) 1.0 (both READMEs).

---

## 1. Executive summary (what matters for Token Bill)

1. **Framework defaults, more than models, set the cache hit rate.** Across 10 frameworks there are ≥25 dated GitHub issues from 2025–2026 in which a framework silently breaks provider caching. The recurring causes:
   - dynamic text in the system prompt (ADK `preload_memory` timestamps, "Today's date" in Agents SDK instructions)
   - per-step tool subsets (Vercel `activeTools`, Pydantic dynamic toolsets)
   - cache-unaware context managers (Strands sliding window, LangChain `ClearToolUsesEdit` with a checkpointer, AG2 `WindowedSummary`)
   - history replay whose serialization differs from the wire (Vercel code execution, Pydantic/Mistral)
   - missing tail breakpoints (CrewAI)
   - structured system blocks flattened to strings (MAF `SkillsProvider`, LangChain #40311)

   Token Bill's five generic breakers catch the *symptom*. The enterprise value is naming the **framework, version and exact config line** that caused it.
2. **Framework token telemetry is unreliable as a cost source.** At least 14 dated bugs in 2026 miscount cache tokens:
   - CrewAI `total_tokens` omitted cache reads and writes, reporting about 3% of real input on a cache-heavy workload. Its `usage_metrics` is multiplied by the number of agents that share an LLM (2× with 2 agents).
   - Strands mixes inclusive and exclusive conventions and over-counts cache hits by 50% in the example given.
   - MAF puts two opposite conventions into one field depending on the connector.
   - OpenLLMetry's Anthropic and Bedrock packages disagree on `gen_ai.usage.input_tokens` for the same Claude call.
   - LangChain's and LangGraph's v3 streaming paths drop cache details entirely.

   Token Bill must recompute from provider-native buckets using a per-source convention registry and sum-check tests, and reconcile against provider billing.
3. **Loop ceilings rose.** LangGraph's default `recursion_limit` went from **25 to 10,000 on 2026-01-12** (PR #6676, released in 1.0.6), then to 10,007. LangChain `create_agent` sets no model-call limit by default. The other defaults: ADK 500 calls, MAF 40 function-loop iterations, Pydantic AI 50 requests, CrewAI 25 iterations with 2 task retries, Vercel `ToolLoopAgent` 20 steps (`WorkflowAgent` has none), OpenAI Agents SDK 10 turns, LlamaIndex 20. A runaway-loop detector and per-framework cap recommendations are cheap, high-trust wins. MAST attributes **15.7%** of multi-agent failures to step repetition and **12.4%** to being unaware of stopping conditions.
4. **Support for cache-preserving Anthropic primitives is arriving framework by framework.**
   - Mid-conversation system messages: Vercel AI SDK (2026-05-28), Pydantic AI (2026-07-27), LangChain (merged 2026-09-20).
   - `tool_addition`: Pydantic AI (2026-08-03), LangChain (merged 2026-09-23).
   - CrewAI, OpenAI Agents SDK, ADK, MAF, Strands and LlamaIndex support neither.

   Token Bill can recommend "upgrade to X ≥ version Y and use Z" with a replayed dollar number.
5. **Multi-agent patterns multiply tokens.**
   - Anthropic measured multi-agent systems at about **15×** the tokens of chat, and single agents at about **4×**.
   - Group chat broadcasts the full history to every participant: MAF docs say this explicitly, and AutoGen `SelectorGroupChat` also makes one selector LLM call per turn over the full history.
   - OpenAI Agents SDK handoffs pass the entire history by default.

   Black-box pruning research reports 18–95% token cuts: AgentPrune 28.1–72.8%, AgentDropout 21.6% of prompt tokens, S²-MAD up to 94.5% for debate, SupervisorAgent 29.68% on GAIA. Token Bill can measure the "re-read factor" and simulate these policies offline.
6. **MCP sampling is deprecated** as of spec 2026-07-28 (SEP-2577). The migration advice is that servers call LLM provider APIs directly. That moves spend **outside** the client's telemetry, creating a new "shadow spend" attribution problem.
   - Claude Code never supported sampling (issue #1785, open since 2025-06-08).
   - VS Code has supported it since 1.101 (2025-06-12).
   - The same spec revision tells servers to return `tools/list` in deterministic order "to improve LLM prompt cache hit rates", and adds OTel `traceparent` propagation in `_meta`.
7. **Output side.** On 948 Claude Code edits whose file content was known, a whole-file format would have emitted **13.5× more output characters** than `Edit` (median 15.3× per edit). In this corpus Claude Code already uses `Edit` well: Write-over-existing-file waste is about 0.2% of output tokens. The lever therefore belongs to custom agents and frameworks that use whole-file edit formats.

   OpenAI Predicted Outputs bills rejected prediction tokens, works only on the gpt-4o and gpt-4.1 families, and does not work with tools. That makes it a niche lever.

---

## 2. Per-framework matrix

Legend:
- ● = supported by default or first-class.
- ◐ = opt-in or partial.
- ○ = not supported.
- "incl." = the framework's `input_tokens` includes cache reads and writes.
- "excl." = it is Anthropic's native uncached remainder.

### 2.1 Caching support (Anthropic path unless noted)

| Framework (version) | Where `cache_control` is emitted | 1h TTL | Automatic (top-level) | Mid-conv `role:system` | `tool_addition` | OpenAI `prompt_cache_breakpoint` |
|---|---|---|---|---|---|---|
| **LangChain / LangGraph** (`langchain-anthropic` 1.7.4) | `AnthropicPromptCachingMiddleware`: last system block + last tool + top-level `cache_control` (automatic tail). Also `model.invoke(..., cache_control=...)` and per-block `cache_control`. | ● `ttl="1h"` | ● via `model_settings` / invoke kwarg | ● PR #40622 merged 2026-09-20 | ● PR #40758 merged 2026-09-23 (as `non_standard` blocks on `SystemMessage`) | ● in `langchain_openai` |
| **CrewAI** (1.15.22) | Native Anthropic provider: end of system + the initial task user message only (PR #5774, 2026-05-12). **No breakpoint on the growing ReAct tail, no tools breakpoint** (tools render before system, so the system breakpoint covers them). LiteLLM route: none unless the gateway injects. | ○ (5m only) | ○ | ○ | ○ | ○ |
| **OpenAI Agents SDK** (v0.22.3) | OpenAI: automatic plus a runner-generated `prompt_cache_key`. `prompt_cache_breakpoint` can be passed on input items. Anthropic via `LitellmModel`: **none**; the converter strips extra keys, and the feature request was declined (#3008, 2026-04-23). | n/a | n/a | ○ | ○ | ◐ manual on input items; `prompt_cache_retention` in `ModelSettings` |
| **Pydantic AI** (v2.48.0) | `anthropic_cache_instructions`, `anthropic_cache_tool_definitions`, `anthropic_cache_messages`, `anthropic_cache` (automatic), and `CachePoint()`. Keeps at most 4 breakpoints and drops the oldest message ones first. | ● `'1h'` | ● `anthropic_cache` (falls back to per-block on Bedrock/Vertex) | ● PR #6765 (2026-07-27) | ● `ToolAvailabilityDeltaPart` → `tool_addition` / OpenAI `additional_tools`, PR #6793 merged 2026-08-03 | ◐ proposed (#7128, open) |
| **Vercel AI SDK** (`ai` 7.x) | `providerOptions.anthropic.cacheControl` on message parts, system messages and tools. | ● `ttl:'1h'` | ○ (not documented) | ● PR #15674 merged 2026-05-28 (second system block sent inline, beta header added) | ○ (tool registry design open, #18021) | ● `promptCacheBreakpoint` (Chat and Responses) |
| **Google ADK** (2.9.2) | Gemini: explicit `ContextCacheConfig` (`min_tokens=0`, `ttl_seconds=1800`, `cache_intervals=10`). Anthropic (`AnthropicLlm`): **none until 2026-08-24**; now last tool + system + last cacheable message when `ContextCacheConfig` is set (commits 2026-08-24; #5395). | ● when TTL maps to 1h | ○ | ○ | ○ | ○ |
| **Strands** (1.57.0) | `CacheConfig(strategy="auto")`: last user message plus a tools `cachePoint`. The system prompt was **never** cached under `auto` (#3144, fixed 2026-07). Otherwise manual `cachePoint` blocks. | ◐ per-section TTLs (#3758: ordering Bedrock rejects) | ○ (#3573 open) | ○ | ○ | ○ |
| **LlamaIndex** (0.14.25) | `cache_idx`: stamps `cache_control` on every message up to an index (bug #20854: 29 blocks → 400; closed 2026-03). Tools cached only when the legacy `anthropic-beta: prompt-caching` header is passed. | ○ | ○ | ○ | ○ | ○ |
| **MAF** (Python 1.19.0) | Manual: pass Anthropic-native structured `instructions` blocks carrying `cache_control`. Later provider text is appended as extra blocks, after fix #7700. No automatic tail. | ● manual | ○ | ○ | ○ | ● sample shows `prompt_cache_breakpoint` plus `prompt_cache_key` |
| **AutoGen / Semantic Kernel** | Maintenance mode; no Anthropic caching feature work (#3636, 2024). Migrate to MAF. | ○ | ○ | ○ | ○ | ○ |
| **AG2** (1.0.6) | New `ag2.network` API. No caching features found. | ○ | ○ | ○ | ○ | ○ |

### 2.2 Known cache breakers (dated, with measured numbers where given)

| Framework | Breaker | Evidence |
|---|---|---|
| LangChain | `ClearToolUsesEdit` (context-editing middleware, default trigger 100k tokens, keep 3) runs on a deepcopy. With a checkpointer, it **re-fires every turn after the first crossing** and clears a different, newer tool result each turn. That rewrites the middle of history every turn. (The cache consequence is our inference; the re-firing is shown in the repro.) | #37815 (2026-06-01, open) |
| LangChain | `AnthropicPromptCachingMiddleware` silently **drops the system prompt** when the last block is a string | #40311 (2026-09-09, open); #40089 (2026-09-01) |
| LangChain | `cache_control` on a `tool_use` block silently dropped | #38398 (2026-06-24, open) |
| LangChain / Deep Agents | Volatile middleware sections (memory, skills) placed *before* stable ones in the system prompt | deepagents #1356 (2026-02-17, fixed) |
| Deep Agents | `MemoryMiddleware add_cache_control=True` put a breakpoint after a volatile memory block | deepagents #3639 (2026-05-27) |
| Deep Agents | The summarization fork does not reuse the parent prefix | deepagents #5319 (2026-08-05, open) |
| CrewAI | No tail breakpoint: ReAct iterations re-bill the whole growing tool history at full input price | code: `providers/anthropic/completion.py` (`_stamp_cache_control_on_message` matches only the initial task text) |
| CrewAI | Per-agent system prompts (role, goal, backstory) give N disjoint cache prefixes; no shared preamble | RFC #5921 (2026-05-25, closed) |
| CrewAI | `cache_breakpoint` key leaked to Groq and OpenAI-compatible providers, returning a 400 | #5886 (2026-05-21) |
| OpenAI Agents SDK | `Agent.as_tool()` nested runs: **0 of 42 calls** returned any `cached_tokens` despite byte-identical prefixes and the same `prompt_cache_key`. The parent in the same run hit **78–90%**. | #5085 (2026-09-18, open) |
| OpenAI Agents SDK | Base64 file input dropped the cache to **0%** on first use (96.7% without files) | #2784 (2026-03-26) |
| OpenAI Agents SDK | The runner auto-generates `prompt_cache_key="agents-sdk:run:<uuid4>"` when no session, conversation or group id is set. Independent runs sharing a system prompt get distinct keys. OpenAI routes on prefix hash plus key and says oversplitting keys disperses traffic (pre-GPT-5.6). | code `run_internal/prompt_cache_key.py`; OpenAI caching guide (moderate) |
| OpenAI Agents SDK | Handoff: "the new agent … gets to see the entire previous conversation history". Its different instructions sit before that history, so every handoff is a full-prefix miss (our inference from the Anthropic and OpenAI prefix rules). | handoffs docs; `nest_handoff_history` is opt-in beta (#2211, 2025-12-19) |
| Vercel AI SDK | `activeTools` / `prepareStep` remove tools instead of restricting them, so the tools block changes and the whole cache is invalidated | #8166 (2025-08-20, open); #14170 (2026-04-06, open) |
| Vercel AI SDK | Replayed code-execution blocks serialize differently from the wire. Turn 2 read only **3,031** of about 9,521 cached tokens even with a 1h TTL. | #18193 (2026-07-30) |
| Pydantic AI | Dynamic toolsets, `prepare_tools`, MCP `tools/list_changed` and in-tool `add_function` re-send a different `tools[]`: "measured as a full cache miss on Anthropic" | #6794 (2026-07-28), fixed by the delta part |
| Pydantic AI | Mistral assistant-chunk reconstruction broke the exact-prefix cache | #6653 (2026-07-22) |
| Google ADK | `preload_memory` and `load_artifacts` append dynamic text (timestamps, artifact lists) to the **system instruction** | #3227 (2025-10-20) |
| Google ADK | The documented `static_instruction` + `instruction` pattern produced a permanently unstable Gemini cache fingerprint (never hit) | #6216 (2026-06-25), #6062 (2026-06-10) |
| Google ADK | `AnthropicLlm` emitted no `cache_control` at all until 2026-08-24 | #5395 (2026-04-19) |
| MAF | `SkillsProvider` f-string-joined structured Anthropic instructions into a plain string, **disabling caching** | #7700 (2026-08-17, fixed) |
| MAF | Group chat: "The orchestrator broadcasts the response to all other agents after each agent's turn"; "All agents see the full conversation history". Python filters tool content before broadcast. | MS Learn, updated 2026-09-21 |
| Strands | `SlidingWindowConversationManager(window_size=40)` trims the head once past 40 messages, so every later turn rewrites the prefix. The offload strategies (truncate, drop, summarize) are "cache-unaware … negates the cost savings". | code; #4176 (2026-09-04, open) |
| Strands | `auto` never cached the system prompt: the varying last-user breakpoint was written on every call and never read | #3144 (2026-07-09, fixed) |
| Strands | Wide parallel tool fan-outs fall out of the 20-block lookback | #3348 (2026-07-20, open) |
| AG2 | Default N-party view `NamedWindowedSummary(recent_n=N)` keeps the last N envelopes and prepends a `CompactionSummary` that *counts elided turns*. Past N the head changes every turn (our inference from docs). | AG2 docs `views_and_skills.mdx` (2026-06-27) |
| LlamaIndex | `cache_idx` stamped every block (29 > 4 limit, returning a 400); system-prompt `cache_control` discarded | #20854 (2026-03-03) |

### 2.3 Default loop limits and cost-inflating defaults

| Framework | Default ceiling | Other token-inflating defaults | Source |
|---|---|---|---|
| LangGraph | `DEFAULT_RECURSION_LIMIT = 10007` (env `LANGGRAPH_DEFAULT_RECURSION_LIMIT`). **Was 25 until PR #6676 (2026-01-12, ≥1.0.6).** One ReAct iteration is about 2 supersteps, so there are about 5,000 model calls before `GraphRecursionError`. | — | `_internal/_config.py`; PRs #6676, #7355 |
| LangChain `create_agent` | `ModelCallLimitMiddleware` / `ToolCallLimitMiddleware`: "Defaults to no limit" (and are opt-in) | `ModelRetryMiddleware max_retries=2`; `SummarizationMiddleware keep=("messages",20)` with character-based counting | built-in middleware docs |
| CrewAI | `max_iter=25` | `max_retry_limit=2` (the task re-runs); `respect_context_window=True` (summarizes, which rewrites history); `inject_date=False` (`%Y-%m-%d`); `planning=False`. A missing `stop_sequences` sync with Anthropic caused **~138K vs ~13K tokens per call (10×)**. | `agents/agent_builder/base_agent.py`, `agent/core.py`; #3836 (2025-11-05) |
| OpenAI Agents SDK | `DEFAULT_MAX_TURNS = 10` | Handoffs forward the full history; `nest_handoff_history` is opt-in | `run_config.py`; handoffs docs |
| Pydantic AI | `UsageLimits.request_limit = 50` | `tool_calls_limit` and token limits are `None` | `usage.py` |
| Vercel AI SDK | `ToolLoopAgent`: `isStepCount(20)`; `WorkflowAgent`: **no default limit** | — | loop-control docs |
| Google ADK | `max_llm_calls = 500` (env `ADK_MAX_LLM_CALLS`) | `preload_memory` into the system prompt | `agents/run_config.py` |
| LlamaIndex | `DEFAULT_MAX_ITERATIONS = 20` | ReAct parse failure appends the bad output plus a format reminder and retries (retry-on-parse-failure) | `agent/workflow/base_agent.py`, `react_agent.py` |
| MAF | Function loop `max_iterations = 40`; workflows `DEFAULT_MAX_ITERATIONS = 100` | Group chat broadcast; orchestrator agent LLM call per round | `_tools.py`, `_workflows/_const.py` |
| AutoGen AgentChat | `max_tool_iterations=1`; `reflect_on_tool_use` False unless structured output | `SelectorGroupChat`: one selector LLM call per turn over the full history, plus broadcast | source; selector-group-chat docs |
| Strands | Sliding window of 40 messages | Cache-unaware offload | code; #4176 |

### 2.4 Usage fields and counting convention (for importers without new recorders)

| Source | Fields | `input` convention for Anthropic | Known miscounts |
|---|---|---|---|
| LangChain `AIMessage.usage_metadata` / LangSmith | `input_tokens`, `output_tokens`, `total_tokens`, `input_token_details{cache_read, cache_creation, ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}`, `output_token_details{reasoning}`. LangSmith adds `input_cost`, `output_cost`, `total_cost` and `*_cost_details`, and prices greedily from most to least specific type. | **incl.** ("input_tokens represents total input tokens including cached reads") | v3 streaming drops `input_token_details` so cache counts are always 0 (langchain #37761, 2026-05-29; langgraph #8094, 2026-06-16). Cache writes double-counted for priority and flex tiers (#40668/#40666, 2026-09-19). |
| CrewAI `UsageMetrics` | `total_tokens`, `prompt_tokens`, `cached_prompt_tokens`, `cache_creation_tokens`, `completion_tokens`, `successful_requests` | Native Anthropic was **excl.** and total omitted cache, so a 17,119-token call reported 17 (#6788, fixed 2026-08). LiteLLM route **incl.** | `crew.usage_metrics` × number of agents sharing an LLM (#7259, 2026-09-04, open) |
| OpenAI Agents SDK `Usage` | `requests`, `input_tokens`, `output_tokens`, `total_tokens`, `input_tokens_details.cached_tokens`, `.cache_write_tokens`, `output_tokens_details.reasoning_tokens`, `request_usage_entries`, `ModelSettings.preserve_raw_usage` | **incl.** (from OpenAI or LiteLLM `prompt_tokens`) | `LitellmModel` never fills `raw_usage` (docs); crash when `cache_write_tokens` became required (#3772, 2026-07-09) |
| Pydantic AI `RequestUsage` / `RunUsage` | `requests`, `input_tokens`, `cache_read_tokens`, `cache_write_tokens`, `output_tokens`, `details`, `cache_hit_ratio`, `cost`. OTel: `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_creation.input_tokens` (legacy name). | **incl.**, normalized for all providers ("input_tokens includes cached reads for every provider") | 5m/1h write split lost, so 1h writes are priced at the 5m rate (#8665, 2026-09-23, open) |
| Vercel `LanguageModelUsage` | `inputTokens`, `inputTokenDetails{noCacheTokens, cacheReadTokens, cacheWriteTokens}`, `outputTokens`, `outputTokenDetails{textTokens, reasoningTokens}`, `totalTokens`, `raw` | **incl.**, with disjoint details (the best shape of any framework) | OpenAI-compatible top-level cache hits dropped (#18905, fixed) |
| Google ADK `usage_metadata` | `prompt_token_count`, `candidates_token_count`, `cached_content_token_count`, plus a custom `cache_creation_input_tokens` since 2026-08-19 | Anthropic path maps `input_tokens` → `prompt_token_count` (**excl.**) | Cache writes dropped until 2026-08-19 (#5835) |
| MAF `UsageDetails` | `input_token_count`, `output_token_count`, `cache_read_input_token_count`, `cache_creation_input_token_count`, plus provider-prefixed keys | **Mixed by connector**: Anthropic `input_tokens` (excl.), OpenAI `input_tokens` (incl.) | Bedrock dropped cache counts (#6639); Foundry hosting reported `cached_tokens=0` while OTel showed 34,304 of 34,847 cached (#6823); Mistral (#7589) |
| Strands `Usage` | `inputTokens`, `outputTokens`, `totalTokens`, `cacheReadInputTokens`, `cacheWriteInputTokens` | **Mixed**: Bedrock/Anthropic excl., OpenAI/Gemini/LiteLLM incl. The sum over-counts by **+50%** in the #3546 example. | #3546 (2026-07-29, open) |
| LlamaIndex | Raw provider usage in `ChatResponse.raw` | Provider-native | Anthropic input tokens `None` in the final response (#20506, 2026-01-20) |
| OpenInference | `llm.token_count.prompt` (incl.), `.prompt_details.cache_read/.cache_write`, `llm.cost.*`; span kinds `LLM`, `AGENT`, `CHAIN`, `TOOL`, … | **incl.** by spec | claude-agent-sdk instrumentor excluded cache (#3610, 2026-08-25, fixed); streaming folded cache in without the details (#3487) |
| OpenLLMetry | `gen_ai.usage.input_tokens`, `gen_ai.usage.cache_read_input_tokens`, `…cache_creation_input_tokens` | Anthropic package **incl.**, Bedrock package **excl.** for the same Claude call | #4449 (2026-09-01, open) |
| OTel GenAI semconv (`anthropic.md`) | `gen_ai.usage.input_tokens`, `gen_ai.usage.cache_read.input_tokens`, `gen_ai.usage.cache_write.input_tokens` | Rule: "Anthropic `input_tokens` excludes cached tokens. Compute: `gen_ai.usage.input_tokens = input_tokens + cache_read_input_tokens + cache_write_input_tokens`" | — |
| LiteLLM | `prompt_tokens` (all prompt tokens incl. hits), `prompt_tokens_details.cached_tokens`, `cache_creation_input_tokens` | **incl.** | — |

### 2.5 Recommended config fixes (fix-text library)

| Framework | Fix |
|---|---|
| LangChain / LangGraph | Use `AnthropicPromptCachingMiddleware(ttl="1h")` when user think-time exceeds 5 min. Pass the system prompt as content blocks (avoids #40311). Set `recursion_limit` explicitly (for example 50–150) and add `ModelCallLimitMiddleware(run_limit=…)`. With a checkpointer, don't use `ClearToolUsesEdit`; use Anthropic server-side `context_management` `clear_tool_uses_20250919` or summarization at a fixed boundary. Upgrade to langchain-anthropic with PR #40622/#40758 and move per-turn reminders into mid-conversation `SystemMessage`s. Order Deep Agents middleware stable → volatile. |
| CrewAI | Add a tail breakpoint: subclass the Anthropic LLM, or route through a LiteLLM proxy with `cache_control_injection_points: [{location: message, role: system}, {location: message, index: -1}]`. Share one crew-wide preamble ahead of per-role text. Lower `max_iter` (default 25) per task. Give each agent its own `LLM` instance, or read usage from OTel rather than `crew.usage_metrics` (#7259). |
| OpenAI Agents SDK | Set `RunConfig(group_id=…)` or a session so `prompt_cache_key` is stable across runs sharing a prefix (pre-GPT-5.6 models). Add an `input_filter` (for example `remove_all_tools`) or `nest_handoff_history=True` on handoffs. For Claude, put caching at the LiteLLM layer. Avoid `as_tool()` sub-agents for hot paths until #5085 is resolved. |
| Pydantic AI | `anthropic_cache=True` or `anthropic_cache_instructions='1h'`. Use `ToolAvailabilityDeltaPart` or deferred tools instead of dynamic toolsets. Keep `request_limit` explicit. Put a date-only timestamp in instructions, or a dynamic tail after the `CachePoint`. |
| Vercel AI SDK | Keep `tools` constant and use `toolChoice`/allowed tools instead of `activeTools` removal. Put `cacheControl` on the last system part and the last message part (1h where think-time > 5 min). Set `stopWhen` on `WorkflowAgent`. |
| Google ADK | Set `ContextCacheConfig` (it now drives Anthropic breakpoints). Don't use `preload_memory` / `load_artifacts` in the system instruction for cached agents; move dynamic content to user turns. Set `RunConfig(max_llm_calls=…)`. |
| Strands | Replace `SlidingWindowConversationManager` with summarize-at-boundary, or raise `window_size`. Use `CacheConfig(strategy="auto")` on a version with the #3144 fix. Keep TTL ordering 1h before 5m. |
| MAF | Pass structured Anthropic instruction blocks with `cache_control` (1h where useful). Set `function_invocation_configuration["max_iterations"]`. In group chat, filter the broadcast history via `UpdateHistory` (Go/.NET) and prefer handoff or sequential when agents don't need all turns. |
| LlamaIndex | Move off `cache_idx` to explicit per-block `cache_control` on the system block and tail. Pass tools caching explicitly. Lower `max_iterations`. |
| Any LiteLLM-routed framework | Gateway-level `cache_control_injection_points`, with `anthropic_prompt_caching_ttl: 1h` where think-time > 5 min. |

---

## 3. Findings

### F1. Framework token telemetry disagrees with itself; recompute, never trust totals (strong)
- **Facts.**
  - CrewAI's native Anthropic `total_tokens = input_tokens + output_tokens`. On a fully cached call that reported **17** tokens for a request that moved **17,119**. The reporter's own traffic had cache reads and writes at 97% of input tokens (#6788, 2026-08-03).
  - `crew.usage_metrics` multiplies the shared LLM's lifetime counter by the number of agents: 155 real tokens were reported as 310 (#7259, 2026-09-04, open).
  - Strands' sum over-counts by 6,457 tokens (+50%) on one cached OpenAI call (#3546).
  - MAF Foundry hosting reported `cached_tokens=0` while OTel showed 34,304 of 34,847 cached (#6823).
  - OpenLLMetry's Anthropic and Bedrock packages emit different `gen_ai.usage.input_tokens` for the same Claude call (#4449).
  - OpenInference's claude-agent-sdk instrumentor excluded 17,024 cache-read tokens from the prompt count (#3610).
  - LangChain and LangGraph v3 streaming drop cache details (#37761, #8094).
  - LangChain double-counts writes on priority and flex tiers (#40668).
  - ADK dropped cache writes until 2026-08-19 (#5835).
  - Pydantic AI loses the 5m/1h split (#8665).
- **Token Bill opportunity.**
  - Build a **convention registry** keyed by (instrumentation scope or framework, version, provider route) → incl./excl./disjoint.
  - Every importer recomputes disjoint buckets (uncached, cache_read, cache_write_5m, cache_write_1h, output) and runs sum-checks. Examples: `cache_read ≤ input` under incl.; `total == Σ buckets`.
  - Flag "telemetry defects" as findings in their own right: "your LangSmith dashboards under-report cache reads because of v3 streaming".
  - Reconcile totals against Admin API / Usage & Cost reports.
- **Savings.** Indirect. Avoids wrong baselines; mis-accounting ranges from 0.03× to 2× of real usage.

### F2. LangGraph's default recursion limit went from 25 to 10,000 (strong)
- **Facts.**
  - PR #6676, merged 2026-01-12 and shipped in langgraph 1.0.6, changed `DEFAULT_RECURSION_LIMIT` from `"25"` to `"10000"`. The PR says "burden really should be on the user to enforce this".
  - PR #7355 (2026-03-30) made it 10007.
  - LangChain `create_agent` model and tool call limits default to "no limit".
- **Token Bill opportunity.**
  - Detect LangGraph ≥1.0.6 runs (from `ls_integration`/metadata, or the OTel scope `openinference.instrumentation.langchain`) with more LLM calls than the fleet p99.
  - Recommend explicit `recursion_limit` plus `ModelCallLimitMiddleware(run_limit=…)`.
  - Price the tail: tokens of calls beyond the p95 call count for the same agent.
- **Savings.** Unknown fleet-wide. Caps the worst case at about 5,000 calls per run instead of 12.

### F3. Default loop ceilings and retry defaults differ 500× across frameworks (strong)
- **Facts.** See §2.3: ADK 500, MAF 40/100, Pydantic 50, CrewAI 25 (+2 task retries), Vercel 20 (`WorkflowAgent` unlimited), LlamaIndex 20, Agents SDK 10, AutoGen 1 tool iteration. The CrewAI stop-sequence bug produced about 138K vs 13K tokens per call (10×; #3836).
- **Token Bill opportunity.**
  - A `loop-ceiling` detector: runs that end exactly at a framework cap, or on `MaxTurnsExceeded` / `GraphRecursionError` / `UsageLimitExceeded`, are pure waste when the task fails.
  - Detect `stop_reason=max_tokens` with fabricated `Observation:` text (ReAct stop-sequence failure).
  - Recommend per-framework caps from the observed success-vs-steps curve.
- **Savings.** Efficient Agents (2508.02694): raising max steps from 8 to 12 raised cost-of-pass from 0.70 to 0.98 (+40%) for +0.6 pt accuracy.

### F4. CrewAI caches only the system prompt and the task prompt, never the ReAct tail (strong)
- **Facts.**
  - PR #5774 (2026-05-12) added `mark_cache_breakpoint` at end-of-system and end-of-initial-user.
  - The Anthropic provider stamps `{"type":"ephemeral"}` only on those. It has no 1h option and no tail marker.
  - Per-agent role/goal/backstory prompts make each agent a separate cache prefix (RFC #5921).
  - The marker leaked to non-Anthropic providers and caused 400s (#5886).
- **Token Bill opportunity.**
  - Detector `missing-tail-breakpoint` (CrewAI signature): `cache_read` stays flat at about system+task size while `input_tokens` grows per iteration.
  - Replay savings with an optimal tail breakpoint (the existing optimal-cache scenario).
  - Emit the LiteLLM injection config or an LLM subclass patch.
- **Savings.** For an iteration adding T tokens over K iterations, the uncached tail costs about 1.0×·ΣT(k) vs about 0.1× with tail caching. Up to about 90% of tail input cost on long loops (from pricing; not measured on CrewAI).

### F5. Context managers that trim or edit history are cache breakers by design (moderate–strong)
- **Facts.**
  - Strands says its offload strategies are "cache-unaware … negates the cost savings" (#4176). Its default sliding window is 40 messages.
  - LangChain `ClearToolUsesEdit` re-fires every turn with a checkpointer (#37815).
  - AG2's default N-party view is a sliding window with an elided-turn count at the head.
  - The Deep Agents summarization fork does not reuse the parent prefix (#5319).
  - Efficient Agents found **"Summarized Memory" used 367K tokens vs 243K for no extra memory (+51%) at lower accuracy (51.5% vs 53.3%)**. "Simple memory" used 194K at 56.4%.
- **Token Bill opportunity.** Detector `sliding-rewrite`:
  - Consecutive calls where the messages prefix diverges at an early index while the tail is appended.
  - Each call writes about the full history (`cache_creation ≈ history`).

  Fix text names the manager and a cache-aligned alternative: trim in blocks at a fixed boundary, server-side `clear_tool_uses`, or summarize once per N turns. Token Bill's history-rewrite breaker gains a framework attribution.
- **Savings.** On an affected run, reads that should be about 0.1× are billed at 1.25× writes. That is roughly **10–12×** the input cost of the history portion per turn, from Anthropic pricing.

### F6. OpenAI Agents SDK: handoffs, nested agents and auto cache keys (moderate–strong)
- **Facts.**
  - Handoffs pass the full history by default. `nest_handoff_history` is an opt-in beta (rolled back from default in 0.7, #2211).
  - `as_tool()` sub-agent runs got 0 of 42 cache hits while the parent got 78–90% (#5085, open).
  - Adding a file dropped the cache to 0% (#2784).
  - The runner sets `prompt_cache_key="agents-sdk:run:<uuid4>"` when no session, conversation or group is set (source).
  - OpenAI's guide says keys combine with the prefix hash for routing (pre-GPT-5.6) and that oversplitting disperses traffic. It suggests about 15 RPM per key.
  - Claude via LiteLLM gets no `cache_control` from the SDK (#3008, declined 2026-04-23).
- **Token Bill opportunity.** Detectors:
  - `handoff-prefix-swap`: the instructions hash changes while the history hash is unchanged.
  - `nested-zero-cache`: child spans at 0% hit while the parent is above 50%.
  - `cache-key-oversplit`: number of distinct keys per shared-prefix hash.

  Fix text: `group_id`, `input_filter`, LiteLLM injection.
- **Savings.** The parent-vs-child hit gap in #5085 (78–90% vs 0%) is the order of the recoverable input cost on nested runs.

### F7. Changing the tool set per step invalidates the whole cache; cache-safe primitives now exist (strong)
- **Facts.**
  - Vercel `activeTools` / `prepareStep` (#8166, #14170) and Pydantic dynamic toolsets, MCP `list_changed` and `prepare_tools` (#6794: "measured as a full cache miss on Anthropic").
  - Pydantic AI merged `ToolAvailabilityDeltaPart` → Anthropic `tool_addition` / OpenAI `additional_tools` (PR #6793, 2026-08-03). #6794 notes that deferred tool definitions are outside Anthropic's cache key: "1 deferred tool and 20 with 400-character descriptions cost the same prefix".
  - LangChain added tool changes on `SystemMessage` (PR #40758, 2026-09-23).
  - MCP spec 2026-07-28: servers SHOULD return `tools/list` in deterministic order "to improve LLM prompt cache hit rates".
- **Token Bill opportunity.** Extend the `tool-churn` detector:
  - Distinguish *subset churn* (tools removed or added per step) from *order churn*.
  - Recommend `tool_addition` / `defer_loading` (Anthropic), `allowed_tools` (OpenAI), or "declare all, restrict with tool_choice".
  - Include the framework version that supports each.
- **Savings.** A full-prefix miss per step, up to about 90% of the input cost of the churned steps.

### F8. Frameworks inject dynamic text into the system prompt (strong)
- **Facts.**
  - ADK `preload_memory` and `load_artifacts` append timestamps, memories and artifact lists to the system instruction (#3227).
  - ADK's documented `static_instruction` + dynamic `instruction` pattern never hit the Gemini cache (#6216, #6062).
  - MAF `SkillsProvider` turned structured `cache_control` blocks into a string (#7700).
  - Deep Agents put volatile memory and skills sections before stable ones (#1356) and marked a volatile memory block (#3639).
  - An Agents SDK user's instructions contained "Today's date: Fri, 18 Sep 2026 (UTC)" (#5085).
  - CrewAI `inject_date` (default False) uses day granularity.
- **Token Bill opportunity.** Detector `volatile-system` gains framework fingerprints:
  - Diff system text between consecutive calls.
  - Classify the changed span: date/time regex, `<PAST_CONVERSATIONS>`, artifact list, skills block.
  - Map it to the responsible middleware or tool.

  Fix: move it to a mid-conversation `role:system` message (Anthropic Opus 4.8+/5 family, LangChain, Pydantic, Vercel), to the user turn, or behind the last breakpoint.
- **Savings.** A full miss on the whole prefix for every request with the dynamic text.

### F9. Replaying stored history must be byte-identical to the wire (strong)
- **Facts.**
  - Vercel's code-execution replay differs from the model-generated blocks. On turn 2 the replay read only 3,031 of about 9,521 cached tokens, even with a 1h TTL and within 5 min (#18193).
  - Pydantic/Mistral chunk-array reconstruction broke the cache (#6653).
  - Pydantic AI's tracking issue #6528 found structured-object fingerprinting "false-positives" and settled on two signals: wire bytes and the provider's `cache_read_tokens`. It prototyped a CI prefix invariant over about 1,000 request comparisons, with only about 4 legitimate busts.
- **Token Bill opportunity.** Detector `resume-drift`:
  - The first call after a session resume, or a gap under the TTL, has `cache_read` < the previous call's (read+write), while the logical history is unchanged.
  - Recommend storing raw provider blocks, not framework-normalized ones.
  - Also ship a **CI "prefix-stability" check** (as Pydantic does) that customers run on recorded request bodies. This is an enterprise-grade guard.
- **Savings.** Every resumed conversation re-writes its history: 1.25× instead of 0.1× on that prefix.

### F10. Pydantic AI is the reference design for cache observability (moderate; partly in flight)
- **Facts.**
  - `cache_hit_ratio` on usage objects (PR #6529, merged 2026-07-17).
  - Inclusive normalization across providers.
  - Proposed default-on `pydantic_ai.cache.*` span attributes: `hit_ratio`, `established_tokens`, `collapsed`, `wasted_tokens`, plus WARN events that exclude TTL expiry and model switches (PR #6534, open).
  - An `on_cache_bust` hook was requested by a production user (#7250).
- **Token Bill opportunity.** Adopt the same semantics (`established_tokens` high-water mark per provider and model; collapse classified as ttl-expired / model-switched / unexplained). Import `pydantic_ai.cache.*` when present; it is a zero-recorder integration.
- **Savings.** Enables detection; savings are indirect.

### F11. Google ADK: Anthropic caching only since 2026-08-24; Gemini explicit cache defaults (strong)
- **Facts.**
  - `AnthropicLlm` ignored `cache_config` (#5395, 2026-04-19). The commit "emit Anthropic prompt cache breakpoints for ContextCacheConfig" (2026-08-24) marks the last tool, the system prompt and the last cacheable block, with 1h when the TTL maps to it.
  - Gemini `ContextCacheConfig`: `min_tokens=0`, `ttl_seconds=1800`, `cache_intervals=10` (since ADK Python 1.15.0).
  - `max_llm_calls=500` default.
- **Token Bill opportunity.**
  - A version check: ADK builds before 2026-08-24 on Claude get zero caching.
  - Fix text: set `ContextCacheConfig`.
  - For Gemini explicit caches, add storage-hour cost to the replay model. Gemini charges cache storage; see provider-others.md.

### F12. Group chat and handoff topologies re-read the transcript per agent; measure a "re-read factor" (moderate)
- **Facts.**
  - MAF group chat broadcasts every response to all participants, and "all agents see the full conversation history" (docs updated 2026-09-21). Python strips tool content before broadcast.
  - AutoGen `SelectorGroupChat` makes an LLM call per turn using "conversation history and participants' name and description" to pick the next speaker, then broadcasts.
  - Anthropic measured "agents typically use about 4× more tokens than chat interactions" and "multi-agent systems use about 15× more tokens than chats". Token usage alone "explains 80% of the variance" on BrowseComp (2025-06-13).
- **Token Bill opportunity.**
  - Compute a **re-read factor** per trace: Σ input tokens / unique content tokens.
  - Compute a per-agent prefix-fragmentation count: distinct system hashes sharing a common preamble.
  - Recommend a shared preamble first, then per-role text via mid-conversation system messages. Other options: filtered broadcast (MAF `UpdateHistory`), handoff or sequential orchestration, and artifacts on a filesystem instead of copying outputs through history (Anthropic's own advice).
- **Savings.** Analytic. Round-robin with N agents and R rounds reads about R²/2 message-units per agent; the selector adds about one full-history read per round.

### F13. Multi-agent failure modes that burn tokens are common (strong)
- **Facts.** MAST (arXiv 2503.13657, v3 2025-10-26): 1,600+ traces, 7 frameworks (ChatDev, MetaGPT, HyperAgent, AppWorld, AG2, Magentic-One, OpenManus), 14 failure modes, κ=0.88. The modes that directly waste tokens:
  - FM-1.3 step repetition: **15.7%**
  - FM-1.5 unaware of stopping conditions: **12.4%**
  - FM-2.1 conversation reset: 2.2%
  - FM-1.4 loss of history: 2.8%

  "Towards a Science of Scaling Agent Systems" (2512.08296, rev. 2026-04-08) found multi-agent vs single-agent ranging from **+80.8% to −70.0%**, with "tool-heavy tasks appear to incur multi-agent overhead".
- **Token Bill opportunity.**
  - A `step-repetition` detector: the same tool name plus an argument hash three or more times, or near-duplicate assistant turns.
  - A `no-stop` detector: runs hitting caps.
  - Label them with MAST codes so enterprise reviewers can trust the taxonomy.
- **Savings.** Unknown per fleet. About 28% of annotated failures are loop-type.

### F14. Black-box communication pruning for multi-agent systems (moderate)
- **Facts.**
  - AgentPrune (2410.02506, 2024-10-03): "28.1%∼72.8% token reduction"; comparable results "at merely $5.6 cost compared to their $43.7". It prunes the message-passing graph and integrates into existing frameworks.
  - AgentDropout (2503.18891, 2025-03-24): −21.6% prompt tokens and −18.4% completion tokens with +1.14 points of performance. It optimizes adjacency matrices; whether it runs on closed APIs is not stated in the abstract.
  - S²-MAD (2502.04790, NAACL 2025): up to **94.5%** token cost reduction in debate with under 2.0% degradation.
  - SupervisorAgent (2510.26585, rev. 2026-03-02): **−29.68% tokens on GAIA** (Smolagents) "without compromising its success rate". It uses an LLM-free adaptive filter and needs no change to the base agent.
  - Optima (2410.08115, v2 2025-02-18): up to 2.8× performance with under 10% of tokens, but it **requires training** (SFT/DPO), so it does not apply to closed APIs.
- **Token Bill opportunity.** Offline "what-if" simulation on captured multi-agent traces:
  - Drop messages from agents whose outputs were never referenced downstream (a proxy for AgentPrune-style pruning).
  - Sparsify debate rounds.
  - Report "communication-redundant tokens".

  This is a differentiator, since no competitor measures inter-agent redundancy.
- **Savings.** 18–95% in papers, on research benchmarks.

### F15. Agent-component cost multipliers, measured (moderate)
- **Facts.** Efficient Agents (2508.02694, 2025-07-24, GAIA, GPT-4.1):
  - Best-of-N: N=4 used 325K vs 243K tokens (+34%) for +0.6 pt.
  - Summarized memory: +51% tokens and −1.8 pt.
  - Max steps 4→12: cost-of-pass 0.48→0.98.
  - The final "Efficient Agents" config kept 96.7% of OWL's performance at $0.228 vs $0.398, a 28.4% better cost-of-pass.

  Other measurements:
  - ReWOO (2305.18323): "5x token efficiency" vs ReAct on HotpotQA (a planner-executor instead of a verbose ReAct scratchpad).
  - SAS/MAS hybrid cascading (2505.18286): up to 20% cost reduction with +1.1–12% accuracy. MAS benefits shrink as models improve.
- **Token Bill opportunity.**
  - Detect BoN/self-consistency: N parallel calls with the same prefix and different samples.
  - Detect summarized-memory rewriting.
  - Detect ReAct text scratchpads when native tool calling is available.
  - Attach paper-backed multipliers as "expected effect" ranges, labelled research-grade.
- **Savings.** 20–50% on the affected workflows (paper numbers).

### F16. Agentic Plan Caching: a black-box, application-level cache for agent plans (moderate)
- **Facts.** 2506.14852 (rev. 2026-01-26): plan templates extracted from completed runs gave **50.31% cost reduction** and 27.28% lower latency "while maintaining performance". It complements serving infrastructure and works with API models.
- **Token Bill opportunity.** Detect repeated planner calls with semantically similar tasks (embedding clusters of first user turns, and similar planner outputs). Estimate plan-cache savings as a recommendation.
- **Savings.** About 50% on planner-heavy workloads (paper).

### F17. MCP sampling: deprecated; Claude Code never supported it; spend moves to server-side keys (strong)
- **Facts.**
  - Spec 2025-11-25 added `tools`/`toolChoice` to sampling (SEP-1577), so servers can run multi-turn tool loops through the client's LLM. Requests carry `modelPreferences` with `costPriority` hints.
  - Spec **2026-07-28 deprecates Sampling, Roots and Logging** (SEP-2577). Its advice: "integrate directly with LLM provider APIs instead of Sampling". `includeContext` `thisServer`/`allServers` are deprecated.
  - Claude Code: no sampling support (issue #1785, 2025-06-08, 58 comments, still open). Its motivation was that server LLM calls are billed pay-as-you-go instead of under the subscription.
  - VS Code: sampling since 1.101 (2025-06-12). Model access and a per-server request log are under "MCP: List Servers"; requests are served by Copilot models (issue #299336 shows `"model":"copilotcli/claude-sonnet-4.6"`).
- **Token Bill opportunity.**
  - (a) An **MCP shadow-spend** attribution: OpenInference and OpenLLMetry have MCP instrumentors. Join MCP server spans with LLM spans made by the server process, and flag servers that call LLM APIs with their own keys.
  - (b) For VS Code, treat sampled calls as Copilot premium usage.
  - (c) Use the new `_meta` `traceparent` propagation (SEP-414) to link server-side LLM calls to the client tool call.
- **Savings.** Visibility only. Sampling itself is low priority now that it is deprecated.

### F18. MCP spec 2026-07-28 adds caching hints Token Bill can use (strong)
- **Facts.**
  - `tools/list` SHOULD be deterministic in order.
  - `CacheableResult` requires `ttlMs` and `cacheScope` on list and read results.
  - List endpoints no longer vary per connection (stateless).
  - OTel trace context is carried in `_meta`.
- **Token Bill opportunity.**
  - Check MCP server conformance: are `tools/list` responses order-stable across sessions? Hash them over time.
  - Report non-conforming servers, with the dollar value of the cache misses they cause, attributed through the tool-churn detector.
  - This gives enterprises a vendor-neutral criterion for approving MCP servers.

### F19. LiteLLM gateway injection fixes caching for every LiteLLM-routed framework (moderate)
- **Facts.**
  - `cache_control_injection_points` (by role `system` or `index: -1`) can be set per model in the proxy `config.yaml`. It covers Anthropic, Bedrock, Vertex, OpenRouter and OpenAI GPT-5.6+.
  - 1h via `anthropic_prompt_caching_ttl: 1h`.
  - LiteLLM `prompt_tokens` includes cache hits.
  - LiteLLM has 104.7M PyPI downloads per month.
  - The CrewAI LiteLLM route, OpenAI Agents SDK `LitellmModel` and the ADK LiteLLM path all pass through it.
- **Token Bill opportunity.** Generate a ready-to-apply LiteLLM proxy config from the replay: which models get system/tail injection and which TTL. Validate it after rollout with before/after hit rates.
- **Savings.** This is the path to the optimal-cache scenario for frameworks that can't emit breakpoints. Typically 50–90% of input cost on cacheable workloads (from pricing).

### F20. OpenAI explicit breakpoints and cache writes change the math for OpenAI-heavy fleets (strong)
- **Facts.**
  - GPT-5.6+ charges cache writes at 1.25×, has a single `30m` TTL via `prompt_cache_options.ttl`, and needs a 1,024-token minimum.
  - `prompt_cache_breakpoint:{mode:"explicit"}` avoids write charges on changing suffixes.
  - Pre-5.6 models: about 15 RPM per `prompt_cache_key` guidance.
  - Framework support: Agents SDK (manual on input items, docs), LangChain OpenAI, Vercel (`promptCacheBreakpoint`; bug #19921 fixed 2026-08), MAF (sample). Pydantic AI has an open proposal (#7128).
- **Token Bill opportunity.** OpenAI replay scenarios with explicit vs automatic breakpoints, a write-surcharge detector (writes on every request, reads never covering the shared prefix), and a key-sharding advisor.

### F21. Import through OTel/OpenInference first: one importer covers about 30 frameworks (strong)
- **Facts.**
  - OpenInference ships Python instrumentors for ag2, agent-framework, autogen, autogen-agentchat, crewai, google-adk, langchain, llama-index, openai-agents, pydantic-ai, smolagents, strands-agents, claude-agent-sdk, mcp, litellm, and more.
  - OpenLLMetry covers crewai, langchain, llamaindex, openai-agents, mcp, litellm and the providers.
  - OpenInference span kinds include `LLM`, `AGENT`, `CHAIN` and `TOOL`.
  - OTel GenAI `anthropic.md` defines the inclusive rule.
- **Token Bill opportunity.**
  - An OTLP/JSON and Phoenix/Langfuse export importer that counts tokens **only on LLM-kind spans** (or `gen_ai.operation.name ∈ {chat, generate_content, …}`). This avoids double-counting agent and chain roll-ups.
  - Apply the F1 convention registry keyed by `otel.scope.name` and version.
  - Reconstruct per-call cache replay only where message payloads are captured; otherwise run usage-only diagnostics.
- **Savings.** Adoption enabler: no new recorders.

### F22. Support for cache-preserving Anthropic primitives, by framework (strong)
- **Facts.** See the §2.1 columns.
  - Mid-conversation `role:system` (Anthropic: Opus 5/4.8, Fable 5/5.1 and Mythos 5/5.1; not Sonnet 5): supported by Vercel (2026-05-28), Pydantic (2026-07-27) and LangChain (2026-09-20).
  - `tool_addition`: supported by Pydantic (2026-08-03) and LangChain (2026-09-23).
  - Not supported by CrewAI, the Agents SDK, ADK, MAF, Strands or LlamaIndex.
- **Token Bill opportunity.** When the volatile-system or tool-churn detector fires, the fix text is conditional on (framework, version, model):
  - "Upgrade to langchain-anthropic ≥ the release with #40622 and send this as `SystemMessage` after the first user turn."
  - Or: "your framework can't; use gateway injection or move the text into the user turn".

### F23. Output side, measured: whole-file edits cost 13.5× the output of targeted edits (moderate; one-developer corpus)
- **Facts.**
  - Local Claude Code corpus (`21_*.out`, `22_*.out`), 49.7M output tokens: `Edit` inputs ≈ 1.47M tokens (**3.0%** of output) and `Write` inputs ≈ 3.12M (**6.3%**), at chars/3.7.
  - Of 1,332 Writes, 1,222 created new files. 47 rewrote a file read earlier (≈104K tokens, 0.2%). 63 overwrote a file written earlier in the thread, and those changed **95.6%** of characters, so they were genuine rewrites.
  - On 948 Edits whose file content was known, whole-file output would have been **13.5×** the Edit payload (median 15.3×, IQR 6.4–36.6×; median file 9,504 chars vs a 546-char edit).
  - Aider documents "whole" as "slow and costly because the LLM has to return the entire file", while diff and udiff are efficient.
  - Diff-XYZ (2510.12487) finds search-replace performs best for larger models.
  - Cursor rewrites whole files with a fast-apply model at about 1,000 tok/s (2024-05-14). That is a speed claim, not a token saving (*weak*, vendor).
  - Morph claims "98% accuracy, sub-second latency" (*weak*, vendor).
- **Token Bill opportunity.** An `edit-format` detector for custom agents: output tokens in file-writing tools where the target already existed. Estimate savings as (file size − diff size), and recommend a search/replace or `str_replace` tool.

  For Claude Code fleets, report it but expect a small saving: in this corpus, Write-over-existing-file waste is at most about 0.2% of output tokens.
- **Savings.** Up to about 13× on edit output for whole-file agents. About 0% to 0.2% for Claude Code in this corpus.

### F24. OpenAI Predicted Outputs is a niche lever and can raise cost (strong)
- **Facts.**
  - Chat Completions only, on gpt-4o, gpt-4o-mini, gpt-4.1, gpt-4.1-mini and gpt-4.1-nano.
  - "Any rejected tokens are still billed like other completion tokens … can introduce higher costs".
  - Incompatible with tools, `n>1`, logprobs and `max_completion_tokens`.
  - Usage exposes `accepted_prediction_tokens` and `rejected_prediction_tokens`.
- **Token Bill opportunity.** When importing OpenAI usage, surface the rejected-prediction ratio and flag it when rejected > accepted (net cost increase). Do not recommend it for tool-using agents.

### F25. Adoption ranking for adapter priority (moderate; downloads include CI and transitive installs)
- **Facts.** PyPI last-month downloads via pypistats on 2026-09-23:

| Package | Downloads |
|---|---|
| mcp | 219.3M |
| langchain | 173.7M |
| langchain-core | 146.6M |
| litellm | 104.7M |
| langsmith | 79.6M |
| fastmcp | 49.8M |
| langgraph | 44.0M |
| strands-agents | 36.2M |
| claude-agent-sdk | 28.9M |
| pydantic-ai-slim | 25.8M |
| langchain-anthropic | 17.4M |
| openai-agents | 12.8M |
| google-adk | 9.90M |
| llama-index-core | 6.76M |
| pydantic-ai | 5.46M |
| deepagents | 5.06M |
| crewai | 3.49M |
| agent-framework-core | 0.95M |
| autogen-agentchat | 0.40M |
| semantic-kernel | 0.36M |
| ag2 | 0.19M |

  npm, 2026-08-23 to 2026-09-21:

| Package | Downloads |
|---|---|
| @modelcontextprotocol/sdk | 195.4M |
| ai (Vercel) | 87.8M |
| @anthropic-ai/claude-agent-sdk | 45.9M |
| @langchain/langgraph | 12.1M |
| @openai/agents | 6.16M |
| @mastra/core | 5.53M |
| @strands-agents/sdk | 1.72M |

  GitHub stars: langchain 146.9K, autogen 61.1K, crewAI 58.9K, llama_index 52.3K, langgraph 42.2K, openai-agents-python 29.7K, semantic-kernel 28.6K, mastra 28.3K, vercel/ai 26.9K, adk-python 21.6K, pydantic-ai 20.1K, agent-framework 13.8K, strands 7.8K, ag2 5.0K.
- **Token Bill opportunity.** See §5 for the ranked adapter plan.

---

## 4. Framework-specific detector and fix-text specs

Every detector:
- works on normalized per-call records (F1 buckets plus optional payload hashes)
- emits `{detector_id, framework, framework_version, evidence_calls[], tokens_wasted_by_bucket, usd_recoverable, fix_text, fix_config_snippet, confidence}`
- computes dollars through the existing replay engine (as-billed vs fixed-cache)

| ID | Name | Signal (fields) | Framework fingerprint | Fire when | $ model | Fix text (template) |
|---|---|---|---|---|---|---|
| FW-01 | runaway-loop-ceiling | LLM calls per run; terminal error type (`GraphRecursionError`, `MaxTurnsExceeded`, `UsageLimitExceeded`, max_iter hit); final status | `ls_integration`/`langgraph_version`, OTel scope, `crewai` agent executor spans | Calls ≥ 0.9×cap, or > 3× the p95 for the same agent name, **and** the run failed or produced no final answer | Tokens of all calls after the p95 call index | LangGraph ≥1.0.6: "default recursion_limit is 10,007 (was 25). Set `config={"recursion_limit": N}` and `ModelCallLimitMiddleware(run_limit=M)`." CrewAI: "`max_iter` default 25; set per task." ADK: "`RunConfig(max_llm_calls=…)` (default 500)." Vercel `WorkflowAgent`: "set `stopWhen`." |
| FW-02 | step-repetition (MAST FM-1.3) | Sequence of (tool name, sha1(canonical args)); assistant text shingles | any | The same (tool, args) ≥3× in a run, or a Jaccard similarity of 0.9 or more between consecutive assistant turns | Tokens of repeated calls | "Loop detected: add a duplicate-call guard / tool-result memoization; lower the iteration cap." |
| FW-03 | handoff-prefix-swap | Per call: hash(system/instructions), hash(tools), hash(messages[:-k]) | OpenAI Agents SDK handoff spans; MAF handoff; LangGraph supervisor | The system hash changes while the history prefix hash is equal to the previous call's, and `cache_read` < 50% of the history | History tokens × (write or uncached rate − read rate) | "Handoff re-sends the full history under new instructions, so nothing is cached. Use `input_filter=remove_all_tools` or a summary filter, or `nest_handoff_history=True`; or keep one shared instruction preamble and deliver per-agent instructions as a mid-conversation system message (Anthropic Opus 4.8+/5)." |
| FW-04 | per-agent-prefix-fragmentation | Distinct system hashes in a trace; longest common prefix (tokens) among them | CrewAI, AutoGen/MAF group chat, LangGraph multi-agent | ≥2 agents share an LCP of at least the model's min cacheable prefix, but each writes its own cache | (N−1) × LCP × write premium, per TTL window | "Put the shared crew/team preamble first with a breakpoint after it; append role text after it." |
| FW-05 | broadcast-reread | Re-read factor = Σ input tokens / unique message tokens per trace; selector-call count | MAF group chat, AutoGen `SelectorGroupChat`, AG2 network | Re-read factor > 5 and the selector calls use the full history | Tokens re-read beyond the needed window | "Filter broadcast history (MAF `UpdateHistory`), use a windowed view, pass artifacts by reference (file path) instead of copying output into history, or switch to sequential/handoff." |
| FW-06 | sliding-rewrite | First divergence index between consecutive payloads (hash per block); `cache_creation` ≈ history each turn after a threshold | Strands `SlidingWindowConversationManager`, AG2 `WindowedSummary`, LangChain `ClearToolUsesEdit` + checkpointer, CrewAI `respect_context_window`, SummarizationMiddleware | Divergence index is in the head (< 50% of prefix) on ≥3 consecutive turns | (Write − read rate) × rewritten tokens | "Trimming the head every turn rewrites the cache every turn. Trim in large blocks at fixed boundaries, use server-side `context_management clear_tool_uses`, or summarize once per N turns (Strands #4176 `cache_aware`, when released)." |
| FW-07 | tool-subset-churn | hash(tools) per step; set difference | Vercel `activeTools`/`prepareStep`, Pydantic dynamic toolsets, MCP `list_changed` | The tools set differs between steps of one run | Full-prefix miss tokens | "Keep `tools` constant; restrict with `tool_choice`/allowed tools; use `defer_loading` + `tool_addition` (Pydantic ≥ PR #6793; LangChain ≥ PR #40758)." |
| FW-08 | volatile-system (framework-attributed) | Diff of system text; regexes for date/time, UUID, `<PAST_CONVERSATIONS>`, artifact lists, skills blocks | ADK `preload_memory`/`load_artifacts`; Deep Agents memory/skills; Agents SDK dated instructions; MAF SkillsProvider (string system) | The changed span matches a pattern and invalidates ≥1 min-cacheable prefix | Prefix tokens × (write − read) | "Move the dynamic text to a mid-conversation system message or the user turn. For ADK, don't use preload_memory in cached agents. For MAF, upgrade past #7700." |
| FW-09 | missing-tail-breakpoint (CrewAI signature) | `cache_read` flat across iterations while `input_tokens` rises | CrewAI native Anthropic; any manual integration | Across ≥3 iterations, `cache_read` stays within ±2% while uncached input grows ≥20% per iteration | Replay with a tail breakpoint | "CrewAI marks only system and task. Add a tail breakpoint (LiteLLM `cache_control_injection_points: index -1`) or subclass the Anthropic LLM." |
| FW-10 | resume-drift | First call after resume: `cache_read` < prev (read+write) with gap < TTL; message count unchanged | Vercel code-execution replay, Pydantic/Mistral, any persisted transcripts | Deficit ≥ min cacheable prefix | Deficit × (write − read) | "Stored history is re-serialized differently from what was sent. Persist raw provider blocks; upgrade (Vercel #18193 fix)." |
| FW-11 | telemetry-convention | Sum-checks per source (§2.4) | Scope name and version | `cache_read > input` under incl., totals mismatch > 1%, or known-bug version ranges | none (data quality) | "Your {framework} version reports {field} with {convention}/{bug}; Token Bill recomputed from provider buckets. Upgrade to {fixed_version}." |
| FW-12 | span-double-count | Token attributes on non-LLM spans equal to Σ children | OpenInference AGENT/CHAIN, LangSmith parent runs | Sum of spans > Σ LLM spans | none | Importer rule (automatic). |
| FW-13 | nested-zero-cache | Child-run hit rate vs parent hit rate with the same key and model | OpenAI Agents SDK `as_tool()` | Parent > 50%, child = 0% over ≥5 eligible calls | Child input × (1 − read rate) | "Nested `as_tool()` runs don't hit the cache (openai-agents #5085). Run sub-agents via `Runner.run` with the parent session or group_id, or inline the tool." |
| FW-14 | cache-key-oversplit | Distinct `prompt_cache_key` per shared-prefix hash per minute; pattern `agents-sdk:run:` | OpenAI Agents SDK on pre-5.6 models | More than 10 keys per prefix and less than 1 RPM per key | Modelled miss rate | "Set `RunConfig(group_id=…)` so runs sharing instructions share a key (aim for about 15 RPM per key)." |
| FW-15 | react-blowup / parse-retry | `stop_reason=max_tokens` + fabricated `Observation:`; "Error while parsing the output" retry messages | CrewAI (text ReAct), LlamaIndex `ReActAgent`, LangChain classic | Output > p99 or parse-retries ≥2 per run | Output tokens of failed calls + retries | "Use native tool calling; ensure stop sequences reach the provider (CrewAI #3836); lower temperature on format-critical steps." |
| FW-16 | edit-format | Output tokens inside file-write tool inputs where the target existed | Custom agents, Aider `whole`, framework file tools | Existing-file writes are ≥5% of output | (Payload − diff size) × output rate | "Use search/replace (`str_replace`/`Edit`) instead of whole-file writes; expected about 13× less edit output (median in our corpus)." |
| FW-17 | mcp-shadow-spend | MCP server spans (OpenInference or OpenLLMetry MCP instrumentor) with child LLM spans, or LLM calls from server processes | MCP servers | Any | Their LLM cost, attributed to the server | "MCP server X calls LLM APIs directly (sampling is deprecated in spec 2026-07-28); its spend is outside your client budget. Route it through the gateway with a tag." |
| FW-18 | bon-selfconsistency | ≥2 parallel calls with an identical prefix and different outputs, followed by a selection | any | Always report; multiplier from N | (N−1) × call cost | "Best-of-N at N=4 cost +34% tokens for +0.6 pt on GAIA (2508.02694); gate it on uncertainty." |

---

## 5. Adapter priority (ranked by enterprise adoption × leverage)

1. **OTel GenAI / OpenInference / OpenLLMetry importer** (OTLP JSON, Phoenix and Langfuse exports).
   - Covers LangChain, LlamaIndex, CrewAI, Agents SDK, ADK, Pydantic AI, Strands, MAF, AG2, AutoGen, smolagents, MCP and LiteLLM with one parser (F21).
   - Must implement the F1 convention registry and LLM-span-only counting.
   - Effort M.
2. **LangChain / LangGraph / LangSmith** (173.7M + 44.0M PyPI; 12.1M npm; 146.9K stars).
   - A native `usage_metadata` importer.
   - A LangSmith run-export importer.
   - Detectors FW-01 (recursion 10,007), FW-06 (`ClearToolUsesEdit`) and FW-08 (Deep Agents middleware order).
   - Effort M.
3. **LiteLLM** (104.7M PyPI; the common gateway under CrewAI, ADK, Agents SDK and AutoGen).
   - Import proxy spend logs.
   - Generate `cache_control_injection_points` configs (F19).
   - Effort M.
4. **Vercel AI SDK** (87.8M npm): the main TypeScript surface. `LanguageModelUsage` has the cleanest disjoint shape, and `raw` keeps the 5m/1h split. Effort S–M.
5. **OpenAI Agents SDK** (12.8M PyPI + 6.2M npm).
   - `request_usage_entries` plus tracing export.
   - Detectors FW-03, FW-13 and FW-14.
   - Effort S–M.
6. **Claude Agent SDK** (28.9M + 45.9M). Mostly covered by the Claude Code and Agent SDK tracks. Ensure the OpenInference claude-agent-sdk instrumentor is a version after the #3610 fix.
7. **Strands** (36.2M PyPI, likely AWS-transitive; Bedrock-heavy enterprises). Correct the #3546 mixed convention on import. Detector FW-06 (sliding window).
8. **Google ADK** (9.9M). Version gate for Anthropic caching (before or after 2026-08-24). Gemini explicit-cache storage costs.
9. **Pydantic AI** (5.5M + 25.8M slim). Cheapest high-quality adapter: inclusive normalized usage, `cache_hit_ratio`, future `pydantic_ai.cache.*`.
10. **LlamaIndex** (6.8M).
11. **CrewAI** (3.5M, 58.9K stars; heavy in enterprise pilots). Prefer OTel over `usage_metrics` because of #7259 and #6788. Detectors FW-04, FW-09 and FW-15.
12. **MAF** (0.95M, rising; the AutoGen and Semantic Kernel successor). Detectors FW-05 (broadcast) and FW-11 (connector convention). AutoGen, SK and AG2 via OTel only.

---

## 6. What Token Bill should build (from this track)

1. **Usage normalizer with a convention registry and golden tests** (F1). Keys: framework or instrumentation scope, version range, provider route. Rules: incl./excl./disjoint, plus known-bug corrections (CrewAI ≤ Aug 2026 native Anthropic, Strands #3546, MAF connector split, OpenLLMetry Bedrock vs Anthropic, LangChain v3 streaming zeros). Each rule has a golden fixture from the issue's reproduction numbers (for example 9 / 17,102 / 17,119 from CrewAI #6788; the +50% from Strands #3546).
2. **OTel/OpenInference importer** that counts only LLM spans and maps `gen_ai.*` and `llm.token_count.*` to Token Bill buckets. When payloads are present, feed them to the existing cache replay; otherwise run usage-only detectors (FW-09, FW-11, FW-13).
3. **Framework fingerprinting.** Detect framework and version from span scope, LangSmith metadata, the `x-stainless`/user-agent strings available in gateway logs, and system-prompt signatures (CrewAI's "You are {role}…", Deep Agents middleware headings). Every finding then carries a framework-specific fix.
4. **Detector pack FW-01…FW-18** (§4), with a fix-text library conditioned on (framework, version, model):
   - "upgrade to X" for LangChain #40622/#40758, Pydantic #6793 and ADK 2026-08-24
   - "configure Y" for recursion_limit, group_id, ContextCacheConfig and LiteLLM injection
   - "restructure Z" for shared preamble and filtered broadcast
5. **Gateway config generator** (LiteLLM first): system and tail injection plus a TTL chosen from observed inter-request gaps. Pair it with a post-rollout verification report.
6. **CI "prefix-stability" check** customers run on recorded request bodies: each request's canonical block list is a prefix of the next, with an allow-list for legitimate busts. This is the Pydantic #6528 design. Output: the first divergent block and the responsible middleware. It turns silent cost regressions into failing builds, a strong enterprise selling point.
7. **Multi-agent analytics:** re-read factor, per-agent prefix fragmentation, MAST-coded loop failures, and what-if pruning (AgentPrune-style drop of unreferenced messages; S²-MAD-style debate sparsification). Report "communication-redundant tokens".
8. **MCP analytics:** a `tools/list` order-stability conformance check per server, MCP shadow-spend attribution, and sampling usage (VS Code) as Copilot-billed.
9. **Output-side analytics:** the edit-format detector (13.5× multiplier evidence), Predicted Outputs rejected-token ratio, and BoN detection.
10. **Loop-ceiling policy advisor:** from observed step-count vs success, recommend per-agent caps (cost-of-pass curves as in Efficient Agents).

---

## 7. Open questions

- CrewAI: does any released version add a tail breakpoint or 1h TTL after 1.15.22? This needs a check of release notes. Only `main` code was read.
- LangGraph: how much fleet spend comes from runs beyond 25 steps now that the cap is 10,007? This needs customer traces.
- OpenAI Agents SDK #5085: the root cause of 0% caching on `as_tool()` runs is unknown and may be server-side.
- Does the Agents SDK's per-run `prompt_cache_key` measurably reduce cross-run hits on pre-GPT-5.6 models? This is plausible from OpenAI's routing docs but unmeasured.
- MCP resources and prompts: no source quantifies how much context clients inject.
  - Claude Code's `MAX_MCP_OUTPUT_TOKENS` (default 25,000; warning at 10,000) is documented only for tool outputs.
  - Whether `@`-mentioned resources are capped is undocumented.
- VS Code: do MCP sampling requests count as Copilot premium requests? The 1.101 notes and current docs don't say.
- AG2 `WindowedSummary`: confirm at runtime that the elided-count head changes the prefix each turn.
- LangChain `ClearToolUsesEdit`: confirm with real cache-usage numbers that the re-firing causes a per-turn miss. The issue shows re-firing, not billing.
- Do the Vercel AI SDK Anthropic provider and Strands plan automatic top-level caching? (Strands #3573 is open.)
- Adoption numbers are download counts, which include CI and transitive installs. Enterprise share per framework needs survey data.

---

## 8. Sources (all opened 2026-09-23 unless noted)

**Anthropic**
- Bundled claude-api skill, `shared/prompt-caching.md` (local, v2.1.280; authoritative per task): automatic caching, 1h TTL, mid-conversation system, `tool_addition`, invalidation hierarchy, 20-block lookback.
- https://www.anthropic.com/engineering/multi-agent-research-system (2025-06-13)

**LangChain / LangGraph / LangSmith / Deep Agents**
- Docs:
  - https://docs.langchain.com/oss/python/langchain/middleware/built-in (undated)
  - https://docs.langchain.com/oss/python/integrations/middleware/anthropic (undated)
  - https://docs.langchain.com/oss/python/integrations/chat/anthropic (undated)
  - https://docs.langchain.com/langsmith/cost-tracking (undated)
- Code:
  - https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/_internal/_config.py
  - https://github.com/langchain-ai/langchain/blob/master/libs/partners/anthropic/langchain_anthropic/middleware/prompt_caching.py
- langgraph PRs and issues:
  - https://github.com/langchain-ai/langgraph/pull/6676 (merged 2026-01-12)
  - https://github.com/langchain-ai/langgraph/pull/7355 (merged 2026-03-30)
  - https://github.com/langchain-ai/langgraph/issues/7314 (2026-03-27)
  - https://github.com/langchain-ai/langgraph/issues/8094 (2026-06-16)
- langchain issues and PRs:
  - https://github.com/langchain-ai/langchain/issues/37815 (2026-06-01)
  - https://github.com/langchain-ai/langchain/issues/40311 (2026-09-09)
  - https://github.com/langchain-ai/langchain/issues/40089 (2026-09-01)
  - https://github.com/langchain-ai/langchain/issues/38398 (2026-06-24)
  - https://github.com/langchain-ai/langchain/issues/37761 (2026-05-29)
  - https://github.com/langchain-ai/langchain/issues/40668 (2026-09-19)
  - https://github.com/langchain-ai/langchain/issues/35219 (2026-02-14)
  - https://github.com/langchain-ai/langchain/issues/38651 (2026-07-04)
  - https://github.com/langchain-ai/langchain/pull/40622 (merged 2026-09-20)
  - https://github.com/langchain-ai/langchain/pull/40758 (merged 2026-09-23)
- deepagents issues:
  - https://github.com/langchain-ai/deepagents/issues/1356 (2026-02-17)
  - https://github.com/langchain-ai/deepagents/issues/3639 (2026-05-27)
  - https://github.com/langchain-ai/deepagents/issues/5319 (2026-08-05)

**CrewAI**
- Code:
  - https://github.com/crewAIInc/crewAI/blob/main/lib/crewai/src/crewai/llms/cache.py
  - https://github.com/crewAIInc/crewAI/blob/main/lib/crewai/src/crewai/agents/crew_agent_executor.py
  - https://github.com/crewAIInc/crewAI/blob/main/lib/crewai/src/crewai/llms/providers/anthropic/completion.py
  - https://github.com/crewAIInc/crewAI/blob/main/lib/crewai/src/crewai/agent/core.py
- Issues and PRs:
  - https://github.com/crewAIInc/crewAI/pull/5774 (merged 2026-05-12)
  - https://github.com/crewAIInc/crewAI/issues/5886 (2026-05-21)
  - https://github.com/crewAIInc/crewAI/issues/6788 (2026-08-03)
  - https://github.com/crewAIInc/crewAI/issues/7259 (2026-09-04)
  - https://github.com/crewAIInc/crewAI/issues/3836 (2025-11-05)
  - https://github.com/crewAIInc/crewAI/issues/5921 (2026-05-25)

**OpenAI Agents SDK / OpenAI**
- Docs:
  - https://openai.github.io/openai-agents-python/usage/ (undated)
  - https://openai.github.io/openai-agents-python/handoffs/ (undated)
  - https://openai.github.io/openai-agents-python/running_agents/ (undated)
- Code:
  - https://github.com/openai/openai-agents-python/blob/main/src/agents/run_config.py
  - https://github.com/openai/openai-agents-python/blob/main/src/agents/run_internal/prompt_cache_key.py
  - https://github.com/openai/openai-agents-python/blob/main/docs/models/index.md
- Issues:
  - https://github.com/openai/openai-agents-python/issues/5085 (2026-09-18)
  - https://github.com/openai/openai-agents-python/issues/2784 (2026-03-26)
  - https://github.com/openai/openai-agents-python/issues/3008 (2026-04-23)
  - https://github.com/openai/openai-agents-python/issues/2211 (2025-12-19)
- OpenAI guides:
  - https://developers.openai.com/api/docs/guides/prompt-caching (undated)
  - https://developers.openai.com/api/docs/guides/predicted-outputs (undated)

**Pydantic AI**
- Docs: https://pydantic.dev/docs/ai/models/anthropic/ and https://pydantic.dev/docs/ai/core-concepts/agent/ (undated)
- Code: https://github.com/pydantic/pydantic-ai/blob/main/pydantic_ai_slim/pydantic_ai/usage.py
- Issues and PRs:
  - https://github.com/pydantic/pydantic-ai/issues/6528 (2026-07-15)
  - https://github.com/pydantic/pydantic-ai/issues/6794 (2026-07-28)
  - https://github.com/pydantic/pydantic-ai/pull/6793 (merged 2026-08-03)
  - https://github.com/pydantic/pydantic-ai/pull/6529 (merged 2026-07-17)
  - https://github.com/pydantic/pydantic-ai/pull/6534 (open)
  - https://github.com/pydantic/pydantic-ai/pull/6765 (2026-07-27)
  - https://github.com/pydantic/pydantic-ai/issues/7250 (2026-08-06)
  - https://github.com/pydantic/pydantic-ai/issues/8665 (2026-09-23)
  - https://github.com/pydantic/pydantic-ai/issues/7128 (2026-08-04)
  - https://github.com/pydantic/pydantic-ai/issues/6653 (2026-07-22)

**Vercel AI SDK**
- Docs: https://ai-sdk.dev/providers/ai-sdk-providers/anthropic and https://ai-sdk.dev/docs/agents/loop-control (undated)
- Code: https://github.com/vercel/ai/blob/main/packages/ai/src/types/usage.ts
- Issues and PRs:
  - https://github.com/vercel/ai/issues/8166 (2025-08-20)
  - https://github.com/vercel/ai/issues/14170 (2026-04-06)
  - https://github.com/vercel/ai/issues/18193 (2026-07-30)
  - https://github.com/vercel/ai/pull/15674 (merged 2026-05-28)
  - https://github.com/vercel/ai/issues/15681 (2026-05-28)

**Google ADK**
- Code:
  - https://github.com/google/adk-python/blob/main/src/google/adk/agents/run_config.py
  - https://github.com/google/adk-python/blob/main/src/google/adk/models/anthropic_llm.py (commits 2026-08-19 and 2026-08-24)
- Docs: https://adk.dev/context/caching/ (undated)
- Issues:
  - https://github.com/google/adk-python/issues/5395 (2026-04-19)
  - https://github.com/google/adk-python/issues/5835 (2026-05-24)
  - https://github.com/google/adk-python/issues/3227 (2025-10-20)
  - https://github.com/google/adk-python/issues/6216 (2026-06-25)
  - https://github.com/google/adk-python/issues/6062 (2026-06-10)

**Microsoft Agent Framework / AutoGen / Semantic Kernel**
- Docs:
  - https://learn.microsoft.com/en-us/agent-framework/user-guide/workflows/orchestrations/group-chat (updated 2026-09-21)
  - https://microsoft.github.io/autogen/stable/user-guide/agentchat-user-guide/selector-group-chat.html (undated)
- Code:
  - https://github.com/microsoft/agent-framework/blob/main/python/packages/core/agent_framework/_tools.py
  - https://github.com/microsoft/agent-framework/blob/main/python/packages/anthropic/README.md
- Issues:
  - https://github.com/microsoft/agent-framework/issues/7700 (2026-08-17)
  - https://github.com/microsoft/agent-framework/issues/6823 (2026-06-30)
  - https://github.com/microsoft/agent-framework/issues/6639 (2026-06-20)
- READMEs: https://github.com/microsoft/autogen and https://github.com/microsoft/semantic-kernel (maintenance and successor notices)

**AG2**
- https://github.com/ag2ai/ag2/blob/main/website/docs/user-guide/network/views_and_skills.mdx (last commit 2026-06-27)
- https://github.com/ag2ai/ag2/blob/main/website/docs/user-guide/network/migration_from_group_chat.mdx

**Strands**
- Code: https://github.com/strands-agents/harness-sdk/blob/main/strands-py/src/strands/agent/conversation_manager/sliding_window_conversation_manager.py
- Issues:
  - https://github.com/strands-agents/harness-sdk/issues/4176 (2026-09-04)
  - https://github.com/strands-agents/harness-sdk/issues/3546 (2026-07-29)
  - https://github.com/strands-agents/harness-sdk/issues/3144 (2026-07-09)
  - https://github.com/strands-agents/harness-sdk/issues/3348 (2026-07-20)
  - https://github.com/strands-agents/harness-sdk/issues/3758 (2026-08-11)

**LlamaIndex**
- Code:
  - https://github.com/run-llama/llama_index/blob/main/llama-index-integrations/llms/llama-index-llms-anthropic/llama_index/llms/anthropic/base.py
  - https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/agent/workflow/base_agent.py
- Issues:
  - https://github.com/run-llama/llama_index/issues/20854 (2026-03-03)
  - https://github.com/run-llama/llama_index/issues/20506 (2026-01-20)

**Telemetry**
- https://github.com/open-telemetry/semantic-conventions-genai/blob/main/docs/gen-ai/anthropic.md (last commit 2026-09-01)
- https://github.com/Arize-ai/openinference/blob/main/spec/semantic_conventions.md
- https://github.com/Arize-ai/openinference/issues/3610 (2026-08-25)
- https://github.com/Arize-ai/openinference/issues/3487 (2026-08-04)
- https://github.com/traceloop/openllmetry/issues/4449 (2026-09-01)
- https://docs.litellm.ai/docs/tutorials/prompt_caching and https://docs.litellm.ai/docs/completion/prompt_caching (undated)

**MCP**
- https://modelcontextprotocol.io/specification/2026-07-28/changelog
- https://modelcontextprotocol.io/specification/2026-07-28/client/sampling
- https://modelcontextprotocol.io/specification/2025-11-25/changelog
- https://github.com/anthropics/claude-code/issues/1785 (2025-06-08)
- https://code.visualstudio.com/updates/v1_101 (2025-06-12)
- https://github.com/microsoft/vscode/issues/299336 (2026-03-05)
- https://code.claude.com/docs/en/mcp (undated)
- https://code.visualstudio.com/docs/copilot/customization/mcp-servers (dated 2026-09-16)

**Papers**
- AgentPrune, arXiv 2410.02506 (2024-10-03)
- Optima, arXiv 2410.08115 (v2 2025-02-18)
- MAST, arXiv 2503.13657 (v3 2025-10-26), https://arxiv.org/html/2503.13657v3
- AgentDropout, arXiv 2503.18891 (2025-03-24)
- S²-MAD, arXiv 2502.04790 (NAACL 2025; rev. 2025-04-10)
- Agentic Plan Caching, arXiv 2506.14852 (rev. 2026-01-26)
- Efficient Agents, arXiv 2508.02694 (2025-07-24), https://arxiv.org/html/2508.02694
- SupervisorAgent, arXiv 2510.26585 (rev. 2026-03-02)
- Towards a Science of Scaling Agent Systems, arXiv 2512.08296 (rev. 2026-04-08)
- Single-agent or Multi-agent? Why not both, arXiv 2505.18286 (2025-05-23)
- ReWOO, arXiv 2305.18323 (2023-05-23)
- Diff-XYZ, arXiv 2510.12487 (rev. 2025-11-17)

**Output side**
- https://aider.chat/docs/more/edit-formats.html (undated)
- https://cursor.com/blog/instant-apply (2024-05-14; vendor)
- https://docs.morphllm.com/quickstart (undated; vendor, weak)

**Adoption data**
- https://pypistats.org/api/packages/{pkg}/recent (queried 2026-09-23)
- https://api.npmjs.org/downloads/point/last-month/{pkg} (2026-08-23 to 2026-09-21)
- GitHub API star counts (2026-09-23)

**Local measurements**
- `21_write_vs_edit.py` / `.out`
- `22_whole_file_multiplier.py` / `.out` (one-developer corpus; privacy rules as in empirical.md)
