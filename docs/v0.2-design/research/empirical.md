# Token Bill research: what real Claude Code session logs say about cost (empirical track)

Snapshot taken 2026-09-23. Research only: the Token Bill repo was not modified.

## 0. Scope, privacy, method

**Corpus.** All `*.jsonl` session logs under `~/.claude/projects` on this machine, snapshotted 2026-09-23:
- 1,481 transcript files ("threads"): 29 main sessions, 264 subagent threads and 1,188 workflow-agent threads.
- 9 project directories, 133.7 days of history, 54 active days.
- 103,607 `assistant` entries, which collapse to **43,383 unique billed API responses**.
- 10.29 B input-side tokens and 48.4 M output tokens.
- **$11,632.75 at API list price.** The account looks like a subscription: the main thread wrote 1-hour-TTL cache entries, which Claude Code requests only for subscriptions within plan usage. The dollar figures are therefore "API-equivalent" spend.

**Caveat: one developer.** This is one power user, at $215 per active day against Anthropic's stated enterprise average of about $13 per developer per active day. Read the percentages as mechanisms and shapes, not as fleet averages. Every mechanism below can be measured per developer by the importer described in §1.

**Privacy rules followed.**
- No prompt, message, tool input, tool output, code or content-derived file path was printed, copied or written.
- Scripts kept only field names, enums, ids, counts, character lengths, token counts and timestamps.
- Tool inputs and outputs were compared only through in-memory SHA-1 hashes (`08_dupes_fanout.py`, `17_prompt_snapshot.py`).
- Project and thread names were hashed.

**Pricing.** All pricing uses the published rates, fetched 2026-09-23 from https://platform.claude.com/docs/en/about-claude/pricing:
- 5-minute cache writes at 1.25× input and 1-hour writes at 2×.
- Cache reads at 0.1×, except 0.025× on Fable 5.1 / Mythos 5.1 and 0.05× on Opus 5.5.
- Fast mode: Opus 5.5 at $8/$40; Opus 5 and 4.8 at $10/$50.
- `inference_geo:"us"` at 1.1×.
- Web search at $10 per 1,000 searches.

**Scripts.** All scripts are in the research directory (`…/scratchpad/research/`), and each has a matching `.out` file:

| Script | What it does |
|---|---|
| `cc_import.py` | Prototype importer: dedup, iteration-aware and TTL-aware pricing, request-start proxy |
| `01_layout.py` | Masked file-layout patterns |
| `02_schema.py` → `02_schema.out` | Every key path, type and count per entry type, plus enum values of whitelisted fields |
| `03_dedup_check.py` | How API calls split across entries and files |
| `04_semantics.py` | Semantics of `iterations`, `diagnostics`, effort, fallback and `input_transformations` |
| `05_analyze.py` | Spend by category, model, thread kind, effort and TTL |
| `06_cache_misses.py` | Miss events, causes, waste, cold starts, gap distributions and TTL-policy replay |
| `07_context.py` | Context-size distribution, output and thinking, per-tool carry cost, tool-result sizes, attachments, compactions, sessions, subagents, fallback iterations |
| `08_dupes_fanout.py` | Hash-only redundant-work detection, chars-per-token calibration, fan-out concurrency, first-call sharing |
| `09_compaction_sim.py` | Earlier-compaction counterfactual and long-context carry |
| `10_repricing_daily.py` | Model re-pricing counterfactuals, spend per active day, session spans |
| `11_thinking_images.py` | `thinking_dropped` transformations and image load vs misses |
| `12_meta.py` | Sidecar `.meta.json` and workflow JSON key names; sessions per file |
| `13_validate_workflow_tokens.py` | Workflow-JSON `totalTokens` vs deduplicated sums |
| `14_agent_rollup_semantics.py` | What the Agent tool's `toolUseResult.totalTokens` means |
| `15_cold_resume_policy.py` | Summarize-on-cold-resume counterfactual |
| `16_attach_err.py` | Attachment key names per type; API error codes |
| `17_prompt_snapshot.py` | System-prompt and tool sizes and churn, by hash |
| `18_skill_listing_carry.py` | Skill-listing carry-cost estimate |
| `19_tokenbill_pricing_gap.py` | Token Bill v0.1.2 pricing vs corrected pricing on the same calls |
| `20_ttl_per_session.py` | Per-session 5m vs 1h preference |

---

## 1. Importer spec: Claude Code logs → Token Bill

### 1.1 File discovery (verified layout, `01_layout.py`, `12_meta.py`)

```
~/.claude/projects/<cwd-slug>/
  <sessionId>.jsonl                                      main thread (1 sessionId per file: 29/29)
  <sessionId>/subagents/agent-<agentId>.jsonl             Task/Agent subagents (isSidechain=true)
  <sessionId>/subagents/agent-<agentId>.meta.json         {agentType, description*, model?, parentAgentId?, spawnDepth, toolUseId, worktreeBranch?, worktreePath*, requestShape?, requestNonInteractive?}
  <sessionId>/subagents/workflows/wf_<runId>/agent-<agentId>.jsonl       workflow agents
  <sessionId>/subagents/workflows/wf_<runId>/agent-<agentId>.meta.json   {agentType, spawnDepth, workflowPhase?, description*}
  <sessionId>/subagents/workflows/wf_<runId>/journal.jsonl               {type: started|launched|result|failed, agentId, key, result*}
  <sessionId>/workflows/wf_<runId>.json                   {runId, workflowName, defaultModel, agentCount, totalTokens, totalToolCalls, durationMs, status, phases, ...}
  <sessionId>/tool-results/*.txt|*.pdf|pdf-*/*.jpg        tool outputs persisted to disk (436 files; p50 44 KB, max 6.5 MB)
  memory/*.md                                             auto-memory (not cost data)
```

Fields marked `*` contain user content: hash them or drop them.

**Size.** Workflow-agent files are 694 MB of the 1.2 GB total (58%). Stream the files; never load them whole.

**Linking a subagent to its parent.** `agent-*.meta.json.toolUseId` is the `tool_use.id` of the parent's Agent/Task call. That lets Token Bill charge a subagent's entire dollar cost to the exact parent tool call that spawned it.

### 1.2 Entry types (`type`) and what matters for cost (`02_schema.out`)

| type | count (log files) | cost relevance |
|---|---:|---|
| `assistant` | 103,474 | **The only billed-usage source.** Holds `message.{id, model, usage, stop_reason, content[], diagnostics, context_management, input_transformations}`, plus `requestId`, `timestamp`, `isSidechain`, `agentId`, `apiBlockIndex`, `effort`, `perTurnEffort`, `advisorModel`, `attribution{Agent,Skill,Plugin,McpServer,McpTool}`, `isApiErrorMessage`, `quotaLimits`, `version`, `entrypoint`, `slug`, `parentUuid`, `uuid` |
| `user` | 62,989 | Tool results: `message.content[].{type: tool_result, tool_use_id, content, is_error}` and `toolUseResult` (tool-specific metadata, including subagent rollups `{totalTokens, usage, totalDurationMs, totalToolUseCount, toolStats}` and persisted-output info `{persistedOutputPath, persistedOutputSize}`). Also `origin.kind` ∈ {human, task-notification, coordinator}, `isMeta`, `isCompactSummary`, `promptId`, `sourceToolAssistantUUID` |
| `attachment` | 38,314 | Injected context. Types include `skill_listing` (about 30.8k chars each), `deferred_tools_delta`, `mcp_instructions_delta`, `task_reminder`, `agent_listing_delta`, `nested_memory`, `hook_additional_context`, `environment`, `date`, `model`, `total_tokens_reminder` (`{text,type}` only), and **`prompt_snapshot` = `{systemPrompt, tools?, cliPrefix?}`**, which is sensitive: hash it for diffing |
| `system` | 784 | `subtype` ∈ {stop_hook_summary, api_error (`error.status`, `retryAttempt`, `maxRetries`), **compact_boundary** (`compactMetadata.{trigger, preTokens, postTokens, durationMs, cumulativeDroppedTokens, preservedSegment, preCompactDiscoveredTools}`, `logicalParentUuid`), **model_refusal_fallback** (`originalModel`, `fallbackModel`, `apiRefusalCategory`), turn_duration, local_command, away_summary} |
| `cost-state` | 3 | Claude Code's own session snapshot `{totalCostUSD, modelUsage{<model incl. "[1m]" suffix>: {inputTokens, outputTokens, cacheReadInputTokens, cacheCreationInputTokens, thinkingTokens, webSearchRequests, costUSD}}, totalAPIDuration, …}`. Rare, but useful for reconciliation. It also reveals **auxiliary calls, such as haiku-4-5, that never appear as `assistant` entries** |
| `queue-operation`, `mode`, `permission-mode`, `custom-title`, `ai-title`, `agent-name`, `last-prompt`*, `pr-link`, `frame-link`, `bridge-session`, `atis-latch`, `relocated`, `file-history-*`, `artifact-*` | — | Not billed. `last-prompt.lastPrompt` and `queue-operation.content` hold user text: never ingest them |

### 1.3 Usage schema actually observed (`02_schema.out`, `04_semantics.out`)

```
message.usage = {
  input_tokens, cache_creation_input_tokens, cache_read_input_tokens, output_tokens,
  cache_creation: {ephemeral_5m_input_tokens, ephemeral_1h_input_tokens},     # present on 100% of entries
  output_tokens_details: {thinking_tokens}                                     # 33,000 entries; thinking = 50.5% of output where reported
  server_tool_use: {web_search_requests, web_fetch_requests},
  service_tier: "standard", speed: "standard"|…, inference_geo: "not_available"|""|"us"…,
  iterations: [ {type: "message"|"fallback_message", model?, input_tokens, cache_creation_input_tokens,
                 cache_creation{…}, cache_read_input_tokens, output_tokens}, … ]
}
message.diagnostics.cache_miss_reason = {type, cache_missed_input_tokens?}     # 1,207 entries (Claude Code opts in to cache diagnostics)
     type ∈ {previous_message_not_found, unavailable, tools_changed, messages_changed, system_changed, model_changed}
message.input_transformations[] = {type: "thinking_dropped", reason: "model_binding_mismatch", path*}
message.content[].type ∈ {tool_use (caller.type="direct"), thinking (70% redacted/empty), text, fallback{from.model,to.model}}
```

### 1.4 Deduplication and pricing rules (all verified; each prevents a real error in this data)

1. **One API response = one `message.id`, not one line.** Claude Code writes one `assistant` entry per content block, sharing `message.id` and `requestId`, and repeats the usage on each. There are 2.39 entries per response (1 entry: 11,160; 2: 15,136; 3: 12,360; up to 10+).
   - Summing usage per line gives **$27,094, 2.33× the true $11,633** (`05_analyze.out`, NAIVE line).
   - No `message.id` spans two files, so subagent files never duplicate main-thread calls (`03_dedup_check.py`).
2. **Take the entry with the maximum `output_tokens`.** In 18,989 responses (44%), output tokens differ across the split entries because earlier entries are streaming snapshots. Output was non-decreasing in 60,394/60,394 comparisons. The input and cache fields are stable, apart from 1–2 anomalies.
3. **Drop repeated `uuid` lines.** 444 lines were re-written copies, for example on resume.
4. **Price each `iterations[]` element with its own model.** When `iterations` has 2 elements (type `fallback_message`), the top-level usage equals only the **last** iteration (27/27 cases, `04_semantics.out`).
   - The refused first attempt was billed on the original model (Fable 5), with `iterations[].model` = `claude-fable-5`.
   - Pricing top-level usage alone misses $20.74 across 9 calls (`07_context.out`).
   - Related signals: `content[].type == "fallback"` and `system.subtype == "model_refusal_fallback"` (7 events, Fable 5 → Opus 4.8).
5. **Price 5m and 1h writes separately.** 44.6% of all cache-write tokens were 1-hour writes. All of them came from the main thread; subagents and workflow agents wrote 5m only (`05_analyze.out`).
   - Token Bill v0.1.2 prices every write at 1.25× and has no `claude-opus-5-5` row. On identical calls it reports **$10,786.64 vs $11,632.75, a 7.3% underestimate** (`19_tokenbill_pricing_gap.out`): $766 from 1h writes and $67 unpriced Opus 5.5.
   - Opus 5.5 is now Claude Code's default model on every plan (model-config docs).
6. **Normalize model ids.** Strip `-YYYYMMDD` (`claude-haiku-4-5-20251001`) and `[1m]` (seen in `cost-state.modelUsage` keys). `<synthetic>` (103 responses, zero usage, `isApiErrorMessage`) is not billed.
7. **Apply multipliers from the usage fields.**
   - `usage.speed=="fast"` → fast rates.
   - `usage.inference_geo=="us"` → ×1.1.
   - `server_tool_use.web_search_requests` → $0.01 each.
   - Observed here: all `standard`, geo `not_available`/empty, and 0 web searches. This user's web work goes through client-side WebFetch/WebSearch tools.
8. **Never treat rollups as cost.**
   - An Agent tool's `toolUseResult.totalTokens` equals the subagent's **last call's input + cache + output** (exact match in 40/96, approximate in 48/96, `14_agent_rollup_semantics.out`). That is its final context footprint, not its spend.
   - `workflows/wf_*.json.totalTokens` summed to 139.6 M tokens, while the same agents processed 3.52 B tokens. Real volume is 25× the rollup; the rollup is roughly input + write + output with reads excluded, at a ratio of 1.22.
9. **Order calls by request start, not response time.** `timestamp` on the first assistant entry comes after TTFT and the first block (median +5.7 s, p99 +120 s). Use the timestamp of the last `user`/`attachment` entry before the call as the request-start proxy (`req_ts` in `cc_import.py`). TTL gap arithmetic depends on it, because a TTL runs from request start.
10. **Estimate calls the transcript cannot see.**
    - Compaction summary requests are not written as `assistant` entries. Only `compact_boundary.preTokens/postTokens/durationMs` survive.
    - Title and other auxiliary calls, such as haiku-4-5, appear only in `cost-state`.
    - Estimate compaction as `preTokens` read (warm) or uncached (cold, after a TTL gap) plus about `postTokens` of output. Reconcile against OTel `claude_code.cost.usage` or the Claude Code Analytics API where available.
11. **Treat about 10% of responses as interrupted.** 4,492 responses (10.4%, $510) have no `stop_reason` on any entry, but they are still billed. Keep them.
12. **Use Claude Code's own miss definition.** A request is a miss when it "re-processed more than 5% and at least 2,000 tokens of what it could have read from cache" (code.claude.com/docs/en/costs). `06_cache_misses.py` uses the same rule, so Token Bill numbers will match `/usage`.

### 1.5 Canonical per-call record (what the importer should emit)

`{call_id=message.id, request_id, thread_id(hash), session_id(hash), project(hash), kind∈{main,subagent,workflow_agent}, parent_tool_use_id (from meta.json), agent_type, workflow_run/phase, spawn_depth, model, iterations[{model,type,usage}], usage{in, w5m, w1h, read, out, thinking}, speed, geo, service_tier, web_search_n, req_start_ts, first_token_ts, last_block_ts, stop_reason, effort, per_turn_effort, advisor_model, attribution{skill,plugin,mcp_server,mcp_tool,agent}, tool_uses[{id,name}], tool_result_chars_by_id, images_by_id, diag_miss{type,missed_tokens}, thinking_dropped_n, entrypoint, cc_version}`

The per-thread event stream adds:
- compactions `{ts, pre, post, dur, trigger}`
- human-prompt timestamps (`origin.kind=="human"`)
- model-refusal fallbacks
- API errors and retries
- hashed prompt-snapshot changes (system prompt and tool set)

---

## 2. Where the money goes (this corpus)

| Category | $ | share |
|---|---:|---:|
| Cache reads | 6,415.38 | **55.1%** |
| 1h cache writes | 2,043.95 | 17.6% |
| Output (incl. thinking) | 1,677.26 | 14.4% |
| 5m cache writes | 1,467.66 | 12.6% |
| Uncached input | 28.50 | 0.2% |

The token-level cache hit ratio is **96.95%**, yet reads are still the largest line. "Hit rate" is therefore the wrong headline KPI for Claude Code. The cost is driven by **context size × number of calls**.

**By thread kind:**
- main: $6,540 (56.2%); 61.6% reads and 31.3% 1h writes.
- workflow-agent: $4,330 (37.2%).
- subagent: $762 (6.5%).

**By model:**
- Fable 5: 46.4%
- Opus 5: 26.5%
- Opus 4.8: 16.6%
- Fable 5.1: 9.3%
- Everything else: under 1%

**By effort:**
- xhigh: 69.3%
- unset: 20.1%
- high: 9.8%
- max: 0.8%

---

## 3. Findings (ranked roughly by dollar impact on this corpus)

Each finding lists the sources it relies on. The "Local" sources are scripts and fields in this directory.

### F1 — Context size, not cache misses, is the dominant driver
- The main-thread prompt is **p50 444,866 tokens** (p10 119,783; p90 869,358; p99 974,741).
  - Subagents: p50 93,365.
  - Workflow agents: p50 125,408.
- Calls with ≥400k context are 17.8% of calls but **45.8% of dollars**. Calls at ≥700k are 7.1% of calls and 23.8% of dollars.
- Cache reads of prompt tokens beyond 200k cost 23.2% of the whole bill; reads beyond 100k cost 34.9%.
- Auto-compaction fired only at about 1M. Observed `preTokens` were p10 967,084 and p50 993,070. The documented default auto-compact window for 1M-context models is about 967K (`autoCompactWindow`, `CLAUDE_CODE_AUTO_COMPACT_WINDOW`).

Sources:
- Local: `07_context.py`, `09_compaction_sim.py`; fields `usage.{input_tokens,cache_creation_input_tokens,cache_read_input_tokens}` and `system.compactMetadata.preTokens`.
- https://code.claude.com/docs/en/model-config (accessed 2026-09-23)

### F2 — Earlier compaction is the largest single lever (upper bound)
Replay of the 29 main threads, compacting whenever context exceeds T. The model:
- Cost of each compaction = warm read of the context plus a summary of 20,283 tokens (the measured median `postTokens`).
- The summary is then written, and later growth is unchanged.

| Compact at T | Main-thread cost change | Share of total bill | Extra compactions |
|---|---:|---:|---:|
| 1M (sanity) | −0.3% | −0.2% | 0 |
| 700k | −32.7% | −18.4% | 21 |
| 500k | −41.4% | −23.3% | 31 |
| **400k** | **−53.0%** | **−29.8%** | 46 |
| 300k | −59.2% | −33.3% | 73 |
| 200k | −67.0% | −37.7% | 116 |

This excludes re-work after compaction (re-reading files, lost state) and any change in quality. Treat it as an upper bound to be validated by A/B, for example with `/autocompact 400k` on a pilot group.

Sources:
- Local: `09_compaction_sim.py`
- https://code.claude.com/docs/en/prompt-caching ("Compacting the conversation") and https://code.claude.com/docs/en/model-config (accessed 2026-09-23)

### F3 — Cold resumes of huge contexts after long idle cost about 8% of the bill
- 128 main-thread transitions came after more than 1 hour idle (median gap 7.0 h, p90 42.3 h).
- Every one was confirmed by Claude Code's own diagnostic, `cache_miss_reason.type == "previous_message_not_found"`.
- Each rewrote the whole context at the 1h rate: median context at resume 568,409 tokens, p90 916,451.
- Avoidable premium: **$924 (7.95% of the bill), $7.22 per event**.
- Main sessions stay open for a median 22 h of wall-clock time (p90 715 h, max 34 days).
- Replay of "summarize on cold resume" (compact before continuing when context >200k after cache expiry): **−51% main, −29% total** at 1h TTL. This overlaps with F2 and is not additive.

Sources:
- Local: `06_cache_misses.py` (cause `idle>1h_ttl_expiry`), `15_cold_resume_policy.py`, `10_repricing_daily.py`
- https://code.claude.com/docs/en/costs, section "Why usage climbs in a long session" (accessed 2026-09-23)

### F4 — Miss taxonomy with ground truth: 802 misses (1.9% of transitions), $1,761 of avoidable premium (15.1%)

| Cause | Events | Premium | Share of miss waste |
|---|---:|---:|---:|
| Idle > 1h TTL (main) | 128 | $924.33 | 52.5% |
| Prefix change within TTL | 517 | $581.38 | 33.0% |
| Idle > 5m TTL (workflow agents and subagents) | 107 | $132.18 | 7.5% |
| Model switch | 23 | $103.43 | 5.9% |
| Compaction rebuild | 23 | $11.02 | 0.6% |
| Context edit/clear | 4 | $8.83 | 0.5% |

**Main-thread within-TTL misses.** There were 91 events costing $541.86. Claude Code's `diagnostics.cache_miss_reason` labels 84 of them:

| Diagnostic label | Events | Cost |
|---|---:|---:|
| messages_changed | 46 | $332.76 |
| unavailable | 15 | $93.56 |
| system_changed | 8 | $57.72 |
| tools_changed | 11 | $35.69 |
| model_changed | 3 | $17.67 |

Across all threads, the diagnostic labels are: `previous_message_not_found` 177, `unavailable` 97, `tools_changed` 78, `messages_changed` 72, `system_changed` 19, `model_changed` 17.

**This is ground truth.** It maps one-to-one onto Token Bill's breakers: history-rewrite, tool-churn, volatile-system and model-switch. Token Bill can validate its heuristics against these labels instead of guessing.

Sources:
- Local: `06_cache_misses.py` (cross-tab), `04_semantics.out`; field `message.diagnostics.cache_miss_reason.{type,cache_missed_input_tokens}`
- Bundled claude-api `shared/prompt-caching.md` (cache diagnostics beta `cache-diagnosis-2026-04-07`)

### F5 — TTL choice is per-developer and per-gap; the 5-minute default hurts API-key fleets
**Start-to-start gaps between requests:**
- main: 88.9% under 1 min, 7.1% at 1–5 min, 2.9% at 5–60 min, 1.2% over 60 min.
- subagents: 97.3% under 1 min.
- workflow agents: 94.4% under 1 min.

**Replay of the main threads (relative to as-billed 1h):**

| Policy | Main-thread cost change |
|---|---:|
| All-5m | **+11.8%** |
| All-1h | +1.1% (replay baseline) |
| 5m + keep-alive pings every 270 s for up to 60 min | +5.5% |
| Hindsight oracle (TTL chosen per transition) | −10.8% |

- Keep-alive loses here because abandoned idles re-read about 570k tokens 13 times.
- The oracle is an optimistic bound. It assumes a cheap way to "promote" a live 5m prefix to 1h, which is unverified.

**Subagents and workflow agents:** switching them to 1h would cost +17–18%, so 5m is correct there.

**Per session:** 1h was cheaper in 15 of 29 sessions and 5m in 14. The spread was p10 −25.6% to p90 +30.5%. Choosing the better static TTL per session saves only 1.3%.

**Why this matters for enterprise.** On an API key or cloud provider, Claude Code's main conversation defaults to **5m** (subscriptions get 1h). A developer with this profile would overpay about 12% of main-thread spend until `promptCacheTtl=1h` or `CLAUDE_CODE_PROMPT_CACHE_TTL=1h` is set.

Sources:
- Local: `06_cache_misses.py` (gap bins, policies), `20_ttl_per_session.py`
- https://code.claude.com/docs/en/prompt-caching ("Which TTL each request gets", "Choose the TTL yourself"; accessed 2026-09-23)
- https://platform.claude.com/docs/en/about-claude/pricing (1.25×/2× writes)

### F6 — Token Bill's pricing math is wrong on real Claude Code data today (−7.3%)
The same deduplicated calls priced with `tokenbill.pricing` v0.1.2 come to $10,786.64, against $11,632.75 correct:
- 1h writes are priced at 1.25× instead of 2×: $766, 6.6% of the bill.
- `claude-opus-5-5` is unknown, so 733 calls go unpriced ($66.61), and Opus 5.5 is Claude Code's current default model.
- Fallback iterations are ignored: $20.74.

**Note on per-line usage.** If an importer also summed usage per JSONL line, the error would flip to **+133%**.

Sources:
- Local: `19_tokenbill_pricing_gap.py`, `03_dedup_check.py`; repo `tokenbill/pricing.py` (read-only)
- https://platform.claude.com/docs/en/about-claude/pricing (accessed 2026-09-23)

### F7 — Delegated agents are 44% of spend and inherit expensive models
- Workflow agents: $4,330 across 1,178 threads. Subagents: $762 across 264 threads.
- Per agent: median 10–14 calls and $1.5–1.7; p90 $7–9.
- Every workflow's `defaultModel` was Opus 4.8, Opus 5, Fable 5 or Fable 5.1.
- Only 53 of 264 subagent meta files set `model` (sonnet 31, opus 14, haiku 6, fable 2). The rest inherit the main model.
- Re-pricing the same billed tokens (quality and tokenizer not modeled):

| Workflow agents moved to | Saving as share of total bill |
|---|---:|
| Sonnet 5 | 25.5% |
| Opus 5.5 | 19.7% |
| Haiku 4.5 | 31.4% |

Moving all subagents to Sonnet 5 saves 4.4%.

Sources:
- Local: `05_analyze.py`, `07_context.py`, `10_repricing_daily.py`, `12_meta.py`; fields `meta.json.{agentType, model, spawnDepth, toolUseId}` and `workflows/*.json.defaultModel`
- https://code.claude.com/docs/en/costs ("Choose the right model", "Agent team token costs"; accessed 2026-09-23)

### F8 — Newer model versions in the same tier are dramatically cheaper for cache-read-heavy agents
Re-pricing identical billed tokens (models.md states the tokenizer is the same across each pair):
- **Fable 5 → Fable 5.1** (cache reads $1.00 → $0.25/MTok): $5,403 → $2,961, **saving 21.0% of the total bill**.
- **Opus 5 → Opus 5.5** ($5/$25 → $4/$20; reads $0.50 → $0.20): $3,077 → $1,722, **saving 11.7%**.
- Opus 4.8 → Opus 5.5 would save 7.2%, but tokenizer equivalence is unverified.

Caveats: behavior differs, and each pair has breaking API changes. The saving is pure price math.

Sources:
- Local: `10_repricing_daily.py`
- https://platform.claude.com/docs/en/about-claude/pricing; bundled `shared/models.md` (accessed 2026-09-23)

### F9 — Tool output carry cost: Bash and Read are 87% of it
Appended context is re-read on every later call. Measured from consecutive-call context deltas minus the previous call's output:

| Tool | Share of appended-context read cost | Dollars | Appended tokens | Median tokens per result |
|---|---:|---:|---:|---:|
| Bash | 49.3% | $678 | 45.1 M | 1,366 |
| Read | 37.3% | $513 | 31.7 M | 4,408 |

Total appended-context carry is $1,375, which is 21.4% of all read dollars.

**Result sizes:**
- 60,208 tool results totalling 198.6 M chars.
- p50 881 chars, p90 7,846, p99 42,015.
- The top 5% of results hold 45% of all characters.
- Read results: p90 35k chars, p99 68k.

**Static prefix:** the first-call prefix (system, tools and instructions) re-read on every call costs $710, 11.1% of reads.

Sources:
- Local: `07_context.py`, `08_dupes_fanout.py`; fields `tool_use.{id,name}`, `tool_result.{tool_use_id, content}` (lengths only), `toolUseResult.persistedOutputSize`
- https://code.claude.com/docs/en/costs ("Offload processing to hooks and skills")

### F10 — Measured characters per token is about 2.3–2.7, not 3.7
Calibration uses single-tool transitions with text-only results of at least 2k chars. Median characters per billed appended token:

| Tool | Median chars/token | n |
|---|---:|---:|
| Bash | 2.32 (IQR 2.09–2.55) | 5,689 |
| Read | 2.54 | 1,919 |
| WebSearch | 2.49 | 65 |
| Browser | 2.22 | 47 |
| WebFetch | 2.66 | 36 |

The pricing page says Claude 4.7+ tokenizers produce about 30% more tokens. Token Bill's `CHARS_PER_TOKEN = 3.7` therefore **undercounts attributed tokens by about 35–60%** on current models.

Fix: calibrate per model family from billed deltas, or call `count_tokens`.

Sources:
- Local: `08_dupes_fanout.py` (calibration block); repo `tokenbill/trace.py` (`CHARS_PER_TOKEN`)
- https://platform.claude.com/docs/en/about-claude/pricing (tokenizer note; accessed 2026-09-23)

### F11 — Configuration sprawl tax: the skill listing alone is about 5–7% of the bill
- Every thread starts with a `skill_listing` attachment of about 30.8k chars: 1,484 threads, across main, subagents and workflow agents.
- Written once and re-read on every call, it costs an estimated **$551–772 (4.7–6.6%)** at 3.5 or 2.5 chars/token.
- Other injected context:

| Attachment | Count | Average chars |
|---|---:|---:|
| `deferred_tools_delta` | 1,958 | 7.9k |
| `mcp_instructions_delta` | 716 | 6.3k |
| `task_reminder` | 965 | 3.5k |

- `prompt_snapshot.tools` JSON is about 136k chars (39 tools).

Sources:
- Local: `07_context.py` (attachments), `17_prompt_snapshot.py`, `18_skill_listing_carry.py`; fields `attachment.{type, content, isInitial, skillCount}`
- https://code.claude.com/docs/en/costs ("Reduce MCP server overhead", "Move instructions from CLAUDE.md to skills")

### F12 — Output and thinking: 14.4% of the bill, half of it thinking
- Output tokens per call: p50 408, p90 2,386, p99 12,406. No `max_tokens` stops.
- Where `thinking_tokens` is reported, it is 50.5% of output.
- xhigh effort accounts for 69.3% of spend. Opus 5.5 defaults to medium; other models default to high.
- Upper bound for an effort lever on this corpus: about 7% (halving thinking). Anthropic's cost guide reports `medium` matching the default's accuracy at 70–85% of its cost on its research benchmarks.

Sources:
- Local: `05_analyze.py` (by effort), `07_context.py`; fields `usage.output_tokens_details.thinking_tokens`, `effort`, `perTurnEffort`
- Bundled `shared/cost-optimization.md`; https://code.claude.com/docs/en/model-config (accessed 2026-09-23)

### F13 — Model switches on large contexts cost about $4.50 each
- 22 main-thread switches with median context 468k cost $103.
- They include 7 automatic safety-classifier fallbacks, Fable 5 → Opus 4.8 (`system.model_refusal_fallback`).
- Caches are model-scoped. Claude Code asks before `/model` only while the cache is warm.

Sources:
- Local: `06_cache_misses.py` (`model_switch`), `07_context.py`
- https://code.claude.com/docs/en/prompt-caching ("Switching models"; accessed 2026-09-23)

### F14 — Agent spin-up and fan-out tax: about 3% plus 1.2%
**Cold starts.** Every agent's first call writes a prefix of median 49k tokens (workflow agents; subagents 33k). About 42% of it is already served from a cache shared across agents in the same directory. First calls cost $352 across 1,442 agents (3.0%).

**Concurrent fan-out.** 780 agent first calls began ≤10 s after a sibling's. They wrote 15.8 M tokens (median 17.7k per call), a $137 premium over reads (1.2%).
- Claude Code holds same-prefix workflow fan-outs for 5 s by default.
- Divergent spawn prompts still pay the premium.

**Second-call miss.** There is also a small, recurring miss on an agent's second call: 241 events of about 13k tokens each, around $40 total.

Sources:
- Local: `06_cache_misses.py` (cold starts), `08_dupes_fanout.py` (fan-out)
- https://code.claude.com/docs/en/prompt-caching ("Workflow fan-outs")
- Bundled `shared/prompt-caching.md` ("Concurrent-request timing")

### F15 — Heavy tail: a few sessions dominate, and 70 API calls per human prompt
- The top 1, 3 and 5 of 29 sessions account for 26.0%, 59.1% and 77.1% of spend. The median session cost $36.74.
- 615 human prompts, at **$18.92 per human prompt** and **70.4 API calls per prompt**, counting delegated agents.
- $215 per active day at list price (p50 $147, p90 $541, max $1,287). The documented enterprise average is about $13, with 90% of users under $30.

Sources:
- Local: `07_context.py`, `10_repricing_daily.py`; field `user.origin.kind=="human"`
- https://code.claude.com/docs/en/costs (accessed 2026-09-23)

### F16 — Hidden (unlogged) calls: compaction and auxiliary requests
- 20 auto-compactions compressed 18.1 M tokens down to 0.42 M (−97.7%). Median time was 118 s.
- The summarization request is **not in the transcript**. Estimated cost: $26 (warm cache) to $275 (cold), 0.2–2.4% of the bill.
- `cost-state` shows haiku-4-5 auxiliary usage that has no `assistant` entries. On the one comparable thread, Claude Code reported $0.724 against $0.70 reconstructed, about 3% unlogged.
- Anthropic documents background summarization at under $0.04 per session.

Sources:
- Local: `07_context.py` (compactions, cost-state); fields `system.compactMetadata.*`, `cost-state.modelUsage`
- https://code.claude.com/docs/en/costs ("Background token usage"); https://code.claude.com/docs/en/prompt-caching ("Compacting the conversation")

### F17 — Negative result: duplicate reads and repeated commands are not a lever here
- Unchanged re-reads (same Read input, no Edit/Write to that path since) are 50 of 7,749 Read calls (0.6%), carrying $1.93.
- Identical repeated Bash commands: 35.
- Identical input-and-output repeats of any tool: 479, carrying $10.52 (0.09%).

Don't build dedup heuristics first: the money is in F1–F8.

Sources:
- Local: `08_dupes_fanout.py` (hash-only)

### F18 — Enterprise ingestion surfaces and their gaps
**OpenTelemetry.**
- Metrics: `claude_code.cost.usage` and `claude_code.token.usage`. Token `type` ∈ {input, output, cacheRead, cacheCreation}. Attributes: `model`, `query_source` (main/subagent/auxiliary), `speed`, `effort`, `agent.name`, `skill.name`, `plugin.name`, `mcp_server.name`, `mcp_tool.name`.
- Events: `claude_code.api_request` (`cost_usd`, `input_tokens`, `cache_creation_tokens`, `request_id`, …) and `api_response` (`output_tokens`, `cache_read_tokens`, `stop_reason`).
- Standard attributes include `session.id`, `user.account_uuid`, `organization.id`, `prompt.id` and `message.uuid`.

**Claude Code Analytics API.** `GET /v1/organizations/usage_report/claude_code` returns daily per-user `model_breakdown.tokens.{input, output, cache_read, cache_creation}` and `estimated_cost`. Data is about 1 hour delayed, and Bedrock, Vertex and Foundry are not covered.

**The gap.** Neither surface, as summarized, splits cache writes by TTL, and neither carries tool-result sizes or miss diagnostics. The local JSONL carries all three, so an endpoint collector is needed for TTL-accurate dollars and root-cause attribution.

**TTL caveat.** The claim that OTel has no TTL split comes from a summarized fetch of the monitoring page. Verify it before relying on it.

Sources:
- https://code.claude.com/docs/en/monitoring-usage (accessed 2026-09-23)
- https://platform.claude.com/docs/en/build-with-claude/claude-code-analytics-api (accessed 2026-09-23)

### F19 — Gateways can silently disable caching
Per Claude Code docs, an LLM gateway that strips `cache_control` markers while returning success bills **the entire history as uncached input every turn**. Converting system blocks to a string does the same.

Detection signature: `cache_read_input_tokens == 0` and `cache_creation_input_tokens == 0` with large `input_tokens`, persisting across turns. This corpus is healthy (uncached input is 0.05% of input tokens), so the rule is cheap to add as a fleet-wide alarm.

Sources:
- https://code.claude.com/docs/en/prompt-caching ("Where the cache lives"; accessed 2026-09-23)
- Local: `05_analyze.out` (uncached share)

### F20 — Tool set and system prompt are stable in Claude Code; diffing is possible but content is sensitive
- `prompt_snapshot` attachments carry the full `systemPrompt` and `tools` (39 tools).
- By hash, the tool set was constant in 447/448 workflow-agent threads and in 13/16 main threads with snapshots.
- Claude Code defers MCP tools behind tool search. That explains why `tools_changed` misses are relatively rare ($35.69 in main).
- Hash these blocks and store only hashes and lengths. Diff positions let Token Bill localize a breaker exactly without keeping content.

Sources:
- Local: `17_prompt_snapshot.py`, `16_attach_err.py`
- https://code.claude.com/docs/en/prompt-caching ("Connecting or disconnecting an MCP server")
- https://claude.dev/blog/lessons-from-building-claude-code-prompt-caching-is-everything/ (2026-04-30)

### F21 — Keep-alive vs 1h depends on the read price
With realistic idle behavior (13 pings of about 570k tokens before a user abandons a session), keep-alive lost to 1h on Fable 5 and Opus-class read prices (+5.5% main).

Anthropic's guidance says keep-alive usually beats the 1h TTL on Fable 5.1, whose reads are 4× cheaper. Token Bill should decide keep-alive vs 1h per model and per developer from their measured gap distribution, not with a global rule.

Sources:
- Local: `06_cache_misses.py` (policies)
- Bundled `shared/prompt-caching.md` ("Choosing the TTL"); https://platform.claude.com/docs/en/about-claude/pricing

---

## 4. What Token Bill should build (prioritized from this evidence)

**P0: correctness. Without it, enterprise numbers are wrong by 7% to 133%.**
1. **Claude Code importer** (`tokenbill import claude-code [--root ~/.claude/projects] [--since]`). Implements every rule in §1.4:
   - message.id dedup with max output tokens, and uuid dedup.
   - Iteration-aware pricing.
   - 5m/1h split, fast, geo and web-search pricing.
   - Request-start timestamps.
   - Main, subagent and workflow thread kinds, with subagent → parent `toolUseId` linkage.
   - Compaction and auxiliary estimates.

   Ship it with a golden-test fixture built from synthetic logs that exercise each pitfall. `cc_import.py` in this directory is a working prototype, about 250 lines of stdlib Python.
2. **Pricing v2.**
   - Add Opus 5.5 (0.05× reads), fast-mode tables, the 1h write multiplier, the geo multiplier and web search.
   - Add a `modelPricing`-compatible contracted-rate override, mirroring Claude Code's managed setting, so reports match invoices.
   - Keep the "verified on" date per row.
3. **Tokenizer calibration.** Replace the global `CHARS_PER_TOKEN=3.7` with per-model-family ratios learned from billed deltas (2.3–2.7 here), or use `count_tokens`.

**P1: the biggest measured levers, with dollar-denominated recommendations.**

4. **Context-size governor.**
   - Report "dollars spent re-reading context beyond X" (23% of the bill beyond 200k here).
   - Simulate an `autoCompactWindow` threshold per developer (F2) and emit a managed-settings snippet (`autoCompactWindow` / `CLAUDE_CODE_AUTO_COMPACT_WINDOW`) with predicted savings.
   - Include an A/B harness: pilot group vs control, comparing cost per human prompt and quality proxies (turns per task, commits or lines accepted from the Analytics API).
5. **Cold-resume detector** (F3). Flag sessions whose context exceeds N tokens at an idle gap beyond the TTL, priced per event. Recommend `/clear`, `/compact` or resume-from-summary, and nudge before a large session goes idle.
6. **TTL advisor** (F5, F21).
   - Build each developer's start-to-start gap histogram and replay 5m, 1h and keep-alive using their models' read prices.
   - Output `promptCacheTtl` / `subagentPromptCacheTtl` recommendations with dollar deltas.
   - This matters most for API-key and cloud fleets, where the main thread defaults to 5m (+11.8% here).
7. **Delegation and model-routing report** (F7, F8).
   - Report spend per agent type, workflow and phase, and the cost of each parent Agent tool call.
   - Re-price agents and models (same tier, newer version; Sonnet or Haiku for agents), with explicit "price-only, validate quality" labeling.
   - Emit `model:` frontmatter or `ANTHROPIC_DEFAULT_*` suggestions.
8. **Miss root-cause** (F4, F13, F20). Use `diagnostics.cache_miss_reason` as ground truth, falling back to Token Bill's byte-diff breakers. Add Claude Code-specific causes: model switch or fallback, effort change, fast-mode toggle, MCP connect/disconnect, plugin reload, image-batch eviction, upgrade, and gateway marker stripping (F19).

**P2: attribution and hygiene.**

9. **Carry-cost attribution per tool, skill, MCP server and attachment** (F9, F11). A token's lifetime cost is its write plus reads × remaining calls. Rank the heaviest tool outputs and the injected listings, then recommend output-filtering hooks, fewer skills or MCP servers, and CLI over MCP. Show the dollar value of each skill listing or MCP instruction per developer.
10. **Spin-up and fan-out tax** (F14): per-workflow cold-start and concurrent-write premium, with shared-prefix spawn prompts suggested.
11. **Effort and thinking share** (F12): spend by effort level, thinking share of output, and a pointer to effort-ladder experiments.

**P3: fleet scale for thousands of developers.**

12. **Privacy-preserving endpoint collector.** Run the importer locally (in CI or on a schedule), emit only the canonical per-call record from §1.5 with hashed ids, and ship it to a central store.
13. **Reconciliation.** Join with OTel `api_request` events (`request_id`), the Claude Code Analytics API (daily per user) and the Admin cost report, to prove the numbers match the invoice within X%.
14. **Heavy-tail alerts** (F15): per-session and per-developer anomaly detection on $/prompt, context p90 and miss $/day.

---

## 5. Threats to validity

- **Single developer.** One power user of Claude Code desktop, heavy on workflows and Fable/Opus. Fleet distributions will differ; the mechanisms and importer rules will not.
- **Counterfactuals.** Compaction, cold-resume and model-routing results are **price-only upper bounds**. They do not model quality loss, re-work or behavior differences. Validate each with a controlled pilot.
- **TTL replay.**
  - Replays reuse billed `cache_read` as the reusable prefix, and approximate request start by the last pre-call entry's timestamp.
  - Keep-alive assumes 270-second pings that each read the whole prefix.
  - The oracle assumes TTL promotion that is not verified.
- **Unlogged calls.** Compaction and auxiliary costs are estimates (§1.4 rule 10).
- **Live data.** The corpus was live while measured. All analyses after `05_analyze.py` share one pickled snapshot (`recs.pkl`: 43,383 calls, $11,632.75).

## 6. Sources

**External, all accessed 2026-09-23 unless dated:**
- Anthropic pricing: https://platform.claude.com/docs/en/about-claude/pricing
- Claude Code, "How Claude Code uses prompt caching": https://code.claude.com/docs/en/prompt-caching
- Claude Code, "Manage costs effectively": https://code.claude.com/docs/en/costs
- Claude Code, "Monitoring usage" (OpenTelemetry): https://code.claude.com/docs/en/monitoring-usage
- Claude Code, "Model configuration" (auto-compact window, effort defaults, fallback): https://code.claude.com/docs/en/model-config
- Claude Code Analytics API: https://platform.claude.com/docs/en/build-with-claude/claude-code-analytics-api
- T. Shihipar, "Lessons from building Claude Code: Prompt caching is everything" (2026-04-30): https://claude.dev/blog/lessons-from-building-claude-code-prompt-caching-is-everything/

**Local Anthropic references** (bundled claude-api skill, Claude Code 2.1.280): `shared/prompt-caching.md`, `shared/cost-optimization.md`, `shared/models.md`, `shared/model-migration.md`.

**Local scripts and outputs:** see §0. Field names are cited inline.

## 7. Open questions

1. Can an existing warm 5m prefix be extended to 1h cheaply, for example by a `max_tokens:0` request with a `ttl:"1h"` breakpoint at the end, billing only the delta? If yes, a "promote on end_turn" policy approaches the −10.8% oracle. This is testable with 3 API calls.
2. How much quality and re-work does compacting at 300–500k cost on real tasks? F2 and F3 need a pilot A/B before being claimed as savings.
3. What causes the recurring about-13k-token miss on subagents' second call (241 events)? Is it a Claude Code prefix-assembly artifact worth reporting upstream?
4. What exactly is `diagnostics.cache_miss_reason.type == "unavailable"` (97 events, $94 in main)? Is it server-side eviction or diagnostics missing?
5. Does OTel really lack a 5m/1h split? This must be verified against the live docs, because it determines whether a local collector is mandatory for TTL-accurate dollars.
6. How representative is this corpus? Running `cc_import.py` on 20–50 volunteer developers across plans (subscription vs API key vs Bedrock) would give fleet distributions for F1–F15.
