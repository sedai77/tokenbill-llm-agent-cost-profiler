# Fact-check: empirical track (`empirical.md`), 2026-09-23

## Method

I checked every finding two ways.

**External sources.** I opened each web source myself on 2026-09-23:
- Pricing: https://platform.claude.com/docs/en/about-claude/pricing
- Model configuration: https://code.claude.com/docs/en/model-config (raw `.md` saved to `vemp/model-config.md`)
- Costs: https://code.claude.com/docs/en/costs
- Prompt caching: https://code.claude.com/docs/en/prompt-caching
- Monitoring usage: https://code.claude.com/docs/en/monitoring-usage (raw `.md` saved to `vemp/monitoring-usage.md`)
- Claude Code Analytics API: https://platform.claude.com/docs/en/build-with-claude/claude-code-analytics-api. It now serves from `/docs/en/manage-claude/claude-code-analytics-api`.
- Cache diagnostics: https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics
- Blog post: https://claude.dev/blog/lessons-from-building-claude-code-prompt-caching-is-everything/ (Thariq Shihipar, Apr 30 2026)

I also read the bundled claude-api docs (2.1.280): `prompt-caching.md`, `cost-optimization.md`, `models.md` and `model-migration.md`.

**Local scripts.** I re-ran all the pickle-based scripts: 05, 06 (as a copy that does not write the pickle), 07, 09, 10, 15, 19 and 20. Their outputs are **byte-identical** to the saved `.out` files. Script 10 differs only by a Python deprecation warning line.

Side effect: re-running `05_analyze.py` rewrote `recs.pkl`. It was regenerated deterministically from the unchanged `cache_load.pkl`, and every downstream output was still identical afterwards.

I also ran extra checks in `vemp/`:
- `v18.py`: skill-listing carry.
- Decomposition of the Token Bill pricing gap.
- Iteration token counts on fallback calls.
- A search for compaction summary calls.
- Second-call agent misses.
- Model-switch breakdown.

**Live data.** The corpus changed while it was being measured. `03_dedup_check.out` and `04_semantics.out` were regenerated at 12:08 on a larger corpus (43,776 message ids), so some counts in the report come from an earlier run. For those I checked the shape of the result, not the exact count.

## Verdicts

| id | verdict | notes |
|---|---|---|
| cc-import-dedup-message-id | confirmed | See below. |
| cc-pricing-1h-opus55-gap | corrected | The 1h component is $758.63 (6.5%), not $766. |
| cc-iterations-fallback | corrected | The structure is right. The billing rule needs a caveat. |
| cc-context-size-driver | corrected (minor) | 2 of 20 compactions fired at about 168k. |
| cc-compaction-threshold-sim | confirmed | Reproduced exactly. |
| cc-cold-resume | confirmed | Reproduced. The median is an upper median. |
| cc-miss-taxonomy-ground-truth | corrected | Calling the labels "ground truth" overstates them. |
| cc-ttl-advisor | confirmed | |
| cc-delegation-model-routing | confirmed | |
| cc-same-tier-upgrade | confirmed | |
| cc-tool-output-carry | corrected | "Median tokens/result" is actually a mean. |
| cc-chars-per-token-calibration | corrected | The undercount percentage is miscomputed. |
| cc-config-sprawl-tax | corrected | Main threads are double counted. |
| cc-agent-spinup-fanout | corrected | The second-call miss costs about $22, not $40. |
| cc-output-thinking-effort | corrected | "Halving thinking" is about 3.6%, not 7%. |
| cc-model-switch-cost | corrected | At most 5 of the 22 switches are Fable 5 → Opus 4.8. |
| cc-hidden-calls-rollups | confirmed | |
| cc-heavy-tail-concentration | confirmed | |
| cc-enterprise-telemetry-gap | corrected | OTel does expose tool-result sizes, and raw bodies can carry usage. |
| cc-gateway-marker-stripping | confirmed | |
| cc-redundant-reads-negative | confirmed | |
| cc-prompt-snapshot-diffing | corrected (minor) | The current output is 449/449, not 447/448. |

## Details

### cc-import-dedup-message-id: confirmed
**Reproduced:**
- `05_analyze.out`: 103,607 entries and 43,383 unique calls, which is 2.39 entries per call.
- NAIVE $27,093.93 against dedup $11,632.75, which is 2.33×.
- 444 duplicate-uuid lines.

**From `03_dedup_check.out` (later, larger corpus):**
- 0 message ids span more than one file.
- Each message id has exactly one requestId.
- Usage differs across split entries only in `output_tokens` (19,403 of 43,776, 44.3%); the input and cache fields differ 1–2 times.

**From `04_semantics.out`:** output never decreased across entries (61,011 comparisons).

The report's 18,989 and 60,394 come from an earlier snapshot. The share (44%) and the monotonicity both hold.

**Primary support:** the monitoring-usage docs say "An API response is persisted as one transcript entry per content block".

### cc-pricing-1h-opus55-gap: corrected
**Confirmed:**
- `19_tokenbill_pricing_gap.py` re-run gives $10,786.64 vs $11,632.75, a gap of $846.10 (7.3%).
- `tokenbill/pricing.py` has no `claude-opus-5-5` row and a single `cache_write_multiplier=1.25`.
- The pricing page confirms 1h writes at 2×.
- 1h tokens are 44.6% of writes, and all of them are main-thread (05).

**Corrected decomposition:** the components must sum to $846.10.

| Component | Report | Recomputed |
|---|---:|---:|
| 1h write premium on priced models | $766 (6.6%) | $758.63 (6.5%) |
| Unpriced Opus 5.5 | $66.61 (733 calls) | $66.61 (733 calls) |
| First fallback iterations | $20.74 | $20.86 |
| **Total** | $853.35 (does not reconcile) | $846.10 |

The fallback component is itself conditional; see the next finding.

**Default model.** The model-config docs say Opus 5.5 is the default for "Pro, Max, Team, Enterprise, and Anthropic API" and for AWS, Bedrock and Google Cloud. Microsoft Foundry defaults to Sonnet 4.5, so the report's "on every plan" (§1.4 rule 5) is slightly wrong.

### cc-iterations-fallback: corrected
**Confirmed:**
- 27 entries with two-element `iterations` (9 unique calls), each with top-level usage equal to the last iteration.
- The first iteration has type `message` and model `claude-fable-5`; the second has type `fallback_message` and model `claude-opus-4-8`.
- 9 content `fallback` blocks.
- 7 `model_refusal_fallback` events.
- $20.74 difference when only top-level usage is priced.
- Model-config docs: cyber-flagged Fable 5 requests re-run on Opus 4.8.

**Caveat missing from the finding.** Bundled `model-migration.md` says:
- `usage.iterations` is the per-attempt billing source of truth.
- "Declined-before-output attempts are reported but not billed".
- A mid-stream decline bills the output already streamed.
- Server-side fallback applies credit-style repricing. The data shows this: fallback iterations read 54k–803k tokens from cache on Opus 4.8 immediately after Fable 5.

In this corpus all 9 first iterations had output tokens (4 to 2,127), so billing them is plausible. However, a rule of "always price every iteration" would over-bill declines that happen before any output. The corrected rule is to price each iteration except declined attempts with zero output.

One small detail: 1 of the 9 deduplicated calls records the top-level model as `claude-fable-5`, not Opus 4.8.

### cc-context-size-driver: corrected (minor)
**Confirmed:**
- Spend split: 55.1 / 17.6 / 14.4 / 12.6 / 0.2%.
- Hit ratio 96.95%.
- Main-thread context p50 444,866 and p90 869,358.
- Reads beyond 200k are 23.2% of the bill; beyond 100k, 34.9%.
- The documented auto-compact window is about 967K (model-config).

**Corrections:**
- Calls at ≥400k are 17.9% of calls (not 17.8%) and 45.8% of dollars.
- "Auto-compaction only fired near 1M" is true for 18 of 20 compactions. Two fired at about 168k (168,108 and 168,949) on `claude-opus-4-6` threads, which run a 200k window. The p10 statistic hides them.

### cc-compaction-threshold-sim: confirmed
- `09_compaction_sim.py` re-run is identical: −18.4%, −23.3%, −29.8% (main −53.0%), −33.3% and −37.7% of the total at 700k, 500k, 400k, 300k and 200k, with a 1M sanity check of −0.2%.
- The simulation logic prices a warm read plus 20,283 output tokens, then rewrites the summary. It is correctly labelled a price-only upper bound.
- `/autocompact`, `autoCompactWindow` and `CLAUDE_CODE_AUTO_COMPACT_WINDOW` all exist (model-config).
- The prompt-caching docs confirm that the summary request reads the prefix from cache while it is warm.

### cc-cold-resume: confirmed
Recomputed from `miss_events.pkl`:
- 128 events, all of them `previous_message_not_found`.
- Median gap 6.96 h; p90 42.3 h.
- $924.33 of waste, which is 7.95% of the bill and $7.22 per event.
- Context p90 916,451.

Minor: 568,409 is the upper median (index n//2). The true median context is 563,004, and the median of re-processed (missed) tokens is about 527k.

Session span: p50 22.0 h, max 819.5 h (34 days).

`15_cold_resume_policy.py` re-run (TTL=60m, min_ctx=200k) gives −51.4% main and −29.2% total.

The costs docs confirm that cache misses follow a break and that the resume-from-summary offer applies on Pro and Max.

Note: the public cache-diagnostics docs say `previous_message_not_found` "is not evidence that your request changed" and typically means too much time has passed. That is consistent with TTL expiry, but it is not a direct confirmation of it.

### cc-miss-taxonomy-ground-truth: corrected
**All numbers reproduce** (06 re-run identical):
- 802 miss events, 1.92% of 41,809 transitions.
- $1,761.17, 15.1% of the bill.
- Shares: 52.5 / 33.0 / 7.5 / 5.9 / 0.6%.
- Labels across all calls: 177 / 97 / 78 / 72 / 19 / 17.
- Main-thread within-TTL `messages_changed`: 46 events, $332.76.

**"Ground truth" and "maps one-to-one" are overstated:**
- Only 341 of the 802 miss events (42.5%) carry any label, although those events hold 92.5% of the waste dollars.
- `previous_message_not_found` and `unavailable` together are 274 of the 460 labelled calls. The cache-diagnostics docs call them cases where "no comparison was produced"; they are not root causes.
- `unavailable` also covers changes to `thinking`, `output_config`, `context_management` and beta headers, which fits effort or fast-mode toggles.
- The docs describe `cache_missed_input_tokens` as a byte-derived "magnitude indicator rather than a billing number".

**Corrected claim:** only the four `*_changed` labels map to breakers (model, system, tools, messages). Validate the heuristics against those four and treat the other two as unknown.

### cc-ttl-advisor: confirmed
- Gap bins: 88.9 / 7.1 / 2.9 / 1.2%.
- Policy replay: +11.8% (all-5m), +5.5% (keep-alive), −10.8% (oracle, flagged as optimistic).
- Subagents and workflow agents on 1h: +18.3% and +17.1%.
- Per session: 15 of 29 favour 1h and 14 favour 5m; p10 −25.6%, p90 +30.5%.
- The prompt-caching docs' TTL table confirms that main conversations on usage credits, an API key or a cloud provider default to 5m. `promptCacheTtl` and `CLAUDE_CODE_PROMPT_CACHE_TTL` both exist.
- Note that the 12% is relative to main-thread spend, which is about 6.6% of the total bill.

### cc-delegation-model-routing: confirmed
- Workflow agents: $4,330.39 (37.2%). Subagents: $761.88 (6.5%).
- 1,178 and 264 threads with calls; median 10 and 14 calls; $1.71 and $1.51 per agent.
- `defaultModel` ∈ {opus-4-8, fable-5, opus-5, fable-5-1}.
- 53 of 264 subagent meta files set `model`.
- Re-pricing savings: 25.5%, 19.7% and 4.4%.
- Per-call models confirm that workflow agents run almost entirely on Opus or Fable. A small exception: 704 workflow calls ran on Opus 5.5.
- The costs docs recommend Sonnet for teammates and `model: haiku` for simple subagents.

### cc-same-tier-upgrade: confirmed
- Re-pricing: $5,403.20 → $2,961.34 (21.0%); $3,077.37 → $1,722.09 (11.7%); Opus 4.8 → 5.5, 7.2%.
- The pricing page confirms the rates: Fable 5 reads at $1, Fable 5.1 reads at $0.25 (0.025×), and Opus 5.5 at $4/$20 with reads at $0.20 (0.05×).
- `models.md` says Fable 5.1 uses the "Same tokenizer as Claude Fable 5" and Opus 5.5 the "Same ... tokenizer ... as Claude Opus 5".

### cc-tool-output-carry: corrected
**Confirmed** (07 re-run identical):
- Bash $677.76 (49.3%, 45.1M appended tokens).
- Read $512.81 (37.3%, 31.7M tokens).
- Total carry $1,374.81, 21.4% of reads.
- 60,208 results totalling 198.6M chars, with p50 881, p90 7,846 and p99 42,015; the top 5% hold 45.0% of chars.
- Static prefix $710.10 (11.1%).

**Correction.** "Median 4.4k tokens/result" (and the report's "Median tokens per result" column: 1,366 and 4,408) is a mean: `app_tok / n_app`. For example, 31,720,994 / 7,195 = 4,409. The median Read result is 4,801 chars.

Also, the $710 static-prefix figure stops at each thread's first cache reset, so it is a lower bound.

### cc-chars-per-token-calibration: corrected
**Confirmed:**
- Medians: 2.32 (IQR 2.09–2.55, n=5,689), 2.54, 2.49, 2.22 and 2.66.
- The pricing page says the Claude 4.7+ tokenizer "produces approximately 30% more tokens".
- `CHARS_PER_TOKEN = 3.7` in `tokenbill/trace.py`.

**Correction.** Estimating tokens as chars/3.7 when the true ratio is 2.22–2.66 chars/token gives 60–72% of the true count. That is an undercount of about 28–40%. Equivalently, true tokens are 1.39–1.67× the estimate. "35–60%" matches neither framing.

**Method caveat.** The measured delta includes tool-result wrapper and reminder overhead, which biases chars/token low. Thinking that is dropped from the next context biases it high.

### cc-config-sprawl-tax: corrected
**Confirmed from 07, 16 and 17:**
- Attachment counts and averages: deferred_tools_delta 1,958 × 7.9k; mcp_instructions_delta 716 × 6.3k; task_reminder 965 × 3.5k.
- `prompt_snapshot.tools`: p50 135,956 chars, 39 tools.
- `18_skill_listing_carry.py` reproduces $551–772 (4.7–6.6%).

**Correction.** Script 18 sums every `skill_listing` in a thread and charges the total from the first call onward. Main threads average 2.38 listings each (53k chars, re-injected), so the main-thread share ($289–404) is double counted.

Charging only the first listing gives **$338–473 (2.9–4.1%)**. A defensible range is therefore about 3–7%, and the lower end is more likely because prose tokenizes at nearer 3–3.5 chars/token.

"30.8k" is the average total per thread. The initial listing is about 30.4k chars (p50).

### cc-agent-spinup-fanout: corrected
**Confirmed:**
- First-call prefix p50: 49,201 (workflow) and 32,793 (subagents).
- Cache-read share of the first call: 42.4% (workflow) and 37.5% (subagents).
- First calls cost $352.22 across 1,442 agents (3.0%).
- 780 fan-out first calls wrote 15.8M tokens (p50 17,729) for a $136.99 premium (1.2%).
- 241 second-call misses with a median of 13,345 tokens.
- The prompt-caching docs confirm the 5 s hold for workflow fan-outs, and the bundled concurrency section confirms the timing behaviour.

**Correction.** The second-call misses cost about **$22** of avoidable premium ($21.61; $23.49 paid on the re-processed prefix), not "$40". I could not reproduce $40 with any definition; the total cost of those 241 calls is $52.71.

The $137 fan-out premium treats every write as avoidable, so it is an upper bound.

### cc-output-thinking-effort: corrected
**Confirmed:**
- Output is 14.4% of spend.
- Thinking is 50.5% of output where it is reported (24,176 calls).
- Output tokens per call: p50 408, p90 2,386, p99 12,406. No max_tokens stops.
- Effort shares: 69.3 / 20.1 / 9.8 / 0.8%.
- `cost-optimization.md`: "`medium` matched the default's accuracy at 70% to 85% of its cost" (research runs on Fable 5).
- Model-config: Opus 5.5 defaults to medium. Other models default to high, except Opus 4.7, which defaults to xhigh.

**Correction.** Halving thinking saves about 14.4% × 0.505 × 0.5 ≈ **3.6%**. Priced on the reporting calls only, reported thinking dollars are 5.1% of the bill, so halving them saves about 2.5%. The "~7%" is the value for removing all thinking.

It is also not a true upper bound. Effort changes the number of turns and tool calls, not only thinking; Anthropic's 70–85% figure is total cost per task.

### cc-model-switch-cost: corrected
**Confirmed:**
- 22 main-thread switch misses, median previous context 467,397 (about 468k), $103.16.
- The prompt-caching docs confirm that caches are model-scoped and that `/model` asks for confirmation only while the cache is warm.

**Corrections:**
- Only 5 of the 22 are Fable 5 → Opus 4.8 transitions, and only 3 of those carry fallback iterations. They cannot "include 7 automatic safety-classifier fallbacks"; the 7 `model_refusal_fallback` events span all thread kinds, and some fallbacks show cache reads because of credit repricing, so they register no miss.
- The cost per main-thread switch is $4.69. The ~$4.50 figure only holds for all 23 switch events ($103.43 / 23).

### cc-hidden-calls-rollups: confirmed
- 07 re-run: 20 auto-compactions, 18.09M → 0.42M tokens, median 118 s, $26.14–274.83 (0.22–2.36%).
- My own check found no summary-shaped assistant call in the window before any compaction. The only calls with more than 5k output tokens there were normal `tool_use` turns that ended before compaction began.
- Cost-state: haiku-4-5 cost $0.026544 of $0.7242 total; the reconstructed figure is $0.6977, which matches the Opus 5.5 portion exactly.
- Agent rollups: 40 exact and 48 approximate (nearest of three candidates) out of 96.
- The OTel docs independently say the subagent event's `total_tokens` "covers only the final request".
- Workflow `totalTokens`: the processed-to-rollup ratio is 25.19.
- Caveat: the $275 upper estimate prices a cold compaction at 2×, but the docs say a cold compaction is billed as uncached input (1×).

### cc-heavy-tail-concentration: confirmed
- Top 1, 3 and 5 sessions: 26.0 / 59.1 / 77.1% of spend. Median session $36.74.
- 615 human prompts; $18.92 and 70.4 calls per prompt.
- Per active day: mean $215.42, p50 $146.70, p90 $540.62, max $1,286.96.
- The costs docs: "around $13 per developer per active day ... below $30 per active day for 90% of users".

### cc-enterprise-telemetry-gap: corrected
**Confirmed:**
- `claude_code.cost.usage` and `claude_code.token.usage` have type ∈ {input, output, cacheRead, cacheCreation}, with no TTL split, plus the attributes model, query_source, speed, effort and agent/skill/plugin/mcp.
- The Analytics API returns daily per-user `model_breakdown` tokens and `estimated_cost`, with about a 1-hour delay. It excludes Bedrock, Google Cloud and Foundry, and Claude Platform on AWS as well.
- Neither surface has miss diagnostics or a 5m/1h split in its metrics.

**Corrections:**
- Cost and tokens are on the `claude_code.api_request` event (`cost_usd`, input, output, cache_read and cache_creation tokens). There is no `api_response` event carrying tokens; the report's F18 attribution is wrong.
- OTel **does** expose tool-result size: `tool_result_size_bytes` on the `claude_code.tool_result` event and `result_tokens` on the `claude_code.tool` span.
- The opt-in `OTEL_LOG_RAW_API_BODIES` (`api_response_body`) exports the full response JSON, including `usage`. That means the `cache_creation` 5m/1h split, and diagnostics if present. However, the bodies include the full conversation, so this is privacy-sensitive.

### cc-gateway-marker-stripping: confirmed
- The prompt-caching docs, "Where the cache lives", say a gateway that "Removes the markers while returning success" causes "your entire conversation history bills as uncached input on every turn". Converting system blocks to a string does the same.
- Uncached input is 0.05% of input tokens (05).

### cc-redundant-reads-negative: confirmed
08 output:
- 50 of 7,749 Read calls (0.6%), $1.93.
- 35 identical Bash commands.
- 479 identical input-and-output repeats, $10.52 (0.09%).

Caveat: only Edit, Write, MultiEdit and NotebookEdit count as mutations. Changes made through Bash are not detected.

### cc-prompt-snapshot-diffing: corrected (minor)
**Confirmed:**
- 39 tools, about 136k chars.
- Main threads: 13 of 16 had a constant tool set.
- Main `tools_changed` misses: $35.69.
- The prompt-caching docs confirm that deferred MCP tools only append.
- The blog post (Thariq Shihipar, Apr 30 2026) confirms `defer_loading` stubs and "Don't change tools or models mid-conversation".

**Correction.** The current `17_prompt_snapshot.out` shows **449/449** workflow-agent threads with a single tool-set variant, not 447/448. The report's number is not reproducible from any saved output.

Also note that across all thread kinds `tools_changed` is the most common label in subagents (48). After a ToolSearch call, 66 of 428 non-main within-TTL misses occurred, so "rare" holds for main threads only.
