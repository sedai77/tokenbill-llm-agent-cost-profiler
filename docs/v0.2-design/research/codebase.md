# Token Bill v0.1.2: codebase deep-read, architecture map, and gap analysis

Track: **codebase** (internal). Author: research subagent. Date: 2026-09-23.
Repo read: `tokenbill/` @ v0.1.2 (released 2026-09-13 per `CHANGELOG.md:10`), git HEAD `a63c84b`, working tree clean.
Test suite: `uv run --python 3.12 --extra dev pytest -q` → **207 passed in 2.49 s** (176 test functions, some parametrized).

All experiments are reproducible scripts in
`/private/tmp/claude-501/-Users-shyamsedai-Library-Application-Support-Claude-scratch-workspaces-6ca7e323-9883-4573-b4f5-6b954b9105d0-24cd3ab5-9c38-4bd2-9927-e0d5ba6ed36e-scratch-2026-09-13-3536c7/0268c3f3-9470-4bd9-93db-0269a23905e9/scratchpad/research/exp/`
(`exp1_*.py` … `exp10_*.py`, shared helpers in `common_exp.py`). They import the repo read-only (`sys.dont_write_bytecode`), and nothing in the repo was modified.

Sources for this track are `file:line` references into the repo, plus the official Anthropic docs I opened to check behavior:

- [P] Pricing: https://platform.claude.com/docs/en/about-claude/pricing (no date on the page; accessed 2026-09-23)
- [PC] Prompt caching: https://platform.claude.com/docs/en/build-with-claude/prompt-caching (accessed 2026-09-23)
- [CD] Cache diagnostics (beta `cache-diagnosis-2026-04-07`): https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics (accessed 2026-09-23)
- [UC] Usage & Cost Admin API: https://platform.claude.com/docs/en/manage-claude/usage-cost-api (accessed 2026-09-23)
- [CCM] Claude Code monitoring (OTel): https://code.claude.com/docs/en/monitoring-usage (accessed 2026-09-23)
- [SK] The bundled Anthropic `claude-api` skill docs (Claude Code 2.1.280), which are authoritative for API behavior: `shared/prompt-caching.md`, `shared/cost-optimization.md`, `shared/models.md`, `shared/admin-api.md` under `/private/tmp/claude-501/bundled-skills/2.1.280/6e8d156f7bd7fe08b59df5bfb0afb6cd/claude-api/` (accessed 2026-09-23)

---

## 0. Executive summary

Token Bill v0.1.2 is a carefully built, honest, well-tested **single-machine, single-provider, cache-only post-hoc profiler**. The code quality is high: frozen dataclasses, precise errors, render caching, an explicit exact-vs-approximate policy, and a flagship planted-waste test. But its **core model of prompt caching is too simple for real enterprise traffic**. I demonstrated wrong answers on patterns that Anthropic itself recommends or documents:

| # | Demonstrated defect (script) | What Token Bill says | Reality |
|---|---|---|---|
| 1 | Anthropic's recommended **moving `cache_control` marker** (exp1) | false `history-rewrite` breaker; simulator agreement **0.000**; optimal-cache = no-cache ($0.040 vs billed $0.0148) | the cache is working |
| 2 | **Shared prefix, varying suffix** (RAG, classification, batch) (exp2b a3) | optimal = fixed = billed; false `history-rewrite` | an explicit breakpoint saves **~56%** |
| 3 | **Concurrent identical requests** (best-of-n) (exp2b a2) | optimal-cache is **65%** cheaper | docs: none can read what the others are still writing |
| 4 | **Interleaved lanes** (main loop + subagent / Haiku helper) in one run (exp2b b2) | redundancy **4.2%**, only `model-switch` | each lane re-sends **~73%** |
| 5 | **Same run_id in two files** (exp3, exp2b c2) | org billed **$40.00** | true **$22.00** |
| 6 | **Non-deterministic JSON key order** in tools (exp9-1) | no breaker; agreement 0.000 | top documented silent invalidator |
| 7 | **Human-in-the-loop gaps > 5 min** (exp6-1) | no breaker | a 1h TTL is **~68%** cheaper |
| 8 | **One-shot requests with `cache_control`** (exp9-4) | 0 breakers | **19%** of spend is avoidable write premium |
| 9 | **1h-TTL cache writes** (exp4-3) | priced at 1.25x ($6.25/MTok on Opus 5) | 2x ($10/MTok); local Claude Code logs show **100%** of writes at 1h TTL |
| 10 | **Opus 5.5, Mythos, Opus 4.5, Sonnet 4.5, Bedrock/Vertex ids** (exp4-2) | dollars omitted; one unpriced call blanks the org total | all are published |
| 11 | **Base64 image** in a message (exp4-1) | image = **99.6%** of the call | ~47% (≈1.6k of 3.4k tokens) |
| 12 | **Compaction / MCP tool added / mode switch** (exp9) | `history-rewrite` "append instead"; `tool-churn` "sort your tools"; `system-edit` "$0.046 recoverable" | wrong advice in all three |
| 13 | **Scale** (exp7/8/10) | one 300-call Claude-Code-shaped session = **184 MB trace, 1.25 GB RAM**; simulate is **O(n²)** (4k calls → 6.3 s, 4x per doubling) | enterprise volume needs streaming and columnar storage |

The recorder also misses the `client.beta.messages` namespace, top-level (automatic) `cache_control`, 1h/5m and server-tool usage, and every prompt-affecting parameter (thinking, effort, tool_choice, speed, inference_geo). It writes SDK content objects as `repr()` strings and records payloads that the caller mutated after sending (exp5). There is **no ingestion** for Claude Code transcripts, Claude Code OTel, or the Admin Usage/Cost API. There are **no attribution dimensions** (user/team/project), no persistence, no budgets or alerts, no machine-readable output, and no redaction mode.

The good news: the honesty architecture (billed = exact, attributed = approx, rescaled to billed) is the right foundation. Anthropic's own cache-diagnostics feature validates the design direction: it works on **hash fingerprints and token estimates, never raw content** [CD]. Token Bill should rebuild its substrate the same way: block-level, content-free hash chains in wire order. That fixes correctness, privacy, and scale in one move.

---

## 1. Architecture map (as built)

```
                         ┌───────────────────────────── cli.py ─────────────────────────────┐
 trace.jsonl ──read_trace──►  list[Run]  ──► for run in runs:                                 │
 (tokenbill/trace@1)        (all in RAM)       analyzer.profile_run(run)   → RunProfile         │
                                               breakers.detect(run)        → [Breaker]          │
                                                 └─ per breaker: repaired_calls + simulator._replay
                                               breakers.repaired_calls(run, found)              │
                                               simulator.simulate(run, fixed_calls)            │
                                                 └─ _as_billed, _no_cache, _replay×2 → [ScenarioResult]
                                            report.render_text_summary(...) → stdout          │
                                            report.render_report(...)       → one HTML file   │
                         └──────────────────────────────────────────────────────────────────┘
 instrument.Recorder.wrap(client) ──patches client.messages.create/.stream──► appends trace@1 lines
 demo_traces.scenario(name)  ──► synthetic Calls whose usage is derived from trace.approx_tokens
 pricing.py: PRICING table, pricing_for(), cost_breakdown(), CACHE_TTL_SECONDS=300, MAX_BREAKPOINTS=4
 common.py: TokenbillError/TraceError, rng(), canonical_json() (sort_keys=True)
```

| Module | LOC | Role | Key structures / complexity |
|---|---:|---|---|
| `common.py` | 46 | errors, seeded RNG, `canonical_json` (sorted keys, `common.py:44-46`) | n/a |
| `trace.py` | 455 | schema `tokenbill/trace@1`, JSONL IO, canonical rendering, LCP | `Usage` has 4 ints (`trace.py:64-76`); `Call` (`:79-93`); render caches keyed by `id()` with weakref finalizers (`:120-170`); `CHARS_PER_TOKEN=3.7` (`:52`); `read_trace` reads the whole file into memory (`:388`) and is strict (`:393-421`) |
| `pricing.py` | 160 | 9-model table, snapshot-suffix resolution, cost breakdown | `ModelPricing` (`:30-43`), `PRICING` (`:49-73`), TTL 300 s (`:78`), `_SNAPSHOT_SUFFIX` (`:95`) |
| `analyzer.py` | 187 | per-call waterfalls, segment char attribution, redundancy vs **previous call** | O(Σ messages) `SegmentShare` objects (`:104-111`), unused by the report |
| `simulator.py` | 313 | 4 scenarios; `_replay` two-pass with retrospective write accounting, clamp at no-cache | entries = full renderings (`:206-216`); `sorted(entries)` on every call (`:180`) → O(n² log n) per run |
| `breakers.py` | 369 | consecutive-pair classifier, 6 kinds, repairs, per-breaker $ | evidence computed for every pair (`:281-286`); one extra `_replay` per breaker (`:301,322`) |
| `report.py` | 787 | self-contained HTML + text summary | per-run sections; headline $ only when breakers exist (`:265-267`) |
| `cli.py` | 267 | `demo`, `analyze`, `--model-price` | results keyed by `run_id` (`:144-151`); mutates the global `PRICING` (`:207-216`) |
| `instrument.py` | 310 | duck-typed SDK recorder | wraps `client.messages` only (`:138-186`); `json.dumps(default=repr)` (`:249`) |
| `demo_traces.py` | 558 | 4 planted-waste scenarios | usage derived from the same 3.7 heuristic (docstring lines 21-60) |

Design strengths worth keeping:

- Exact/approx separation with rescaling to billed totals (`DESIGN.md` §1).
- Precise, typed trace validation, including NaN, surrogate, and 2^53 guards (`trace.py:254-375`).
- No network and no dependencies (`pyproject.toml:39`).
- Retrospective write accounting plus a no-cache clamp, so "optimal" never exceeds no-cache (`simulator.py:221-256`).
- Honest threats-to-validity section (`DESIGN.md:296-338`).

---

## 2. Findings (gap analysis, ranked by impact on "is this trustworthy enterprise-grade bill reduction")

Each finding lists evidence (file:line, experiment, doc), what Token Bill should build, a savings estimate, and build effort.

### F1. The recommended moving-breakpoint pattern yields a false `history-rewrite` and 0.000 simulator agreement [correctness, strong]

- **Evidence.** `render_segments` serializes each message with `canonical_json(message)` and includes any `cache_control` keys (`trace.py:150-154`). The recorder stores messages as sent (`instrument.py:199`). Anthropic's own guidance is the opposite: "Strip `cache_control` markers before diffing: the moving marker always differs between adjacent requests and is not an invalidator" ([SK] prompt-caching.md "Finding the invalidator"). The multi-turn placement pattern puts the marker on the last block of the most recent turn ([SK] "Multi-turn conversations").
- **Experiment exp1** (8-turn loop, marker moved each turn, billed usage consistent with a working cache):
  - breakers `[('history-rewrite', 1, None)]`
  - optimal-cache **$0.0403** = no-cache, versus as-billed **$0.0148**
  - note: "billed cache reads 14997 vs simulated 0 (agreement 0.000)"
- **Impact.** Any team following Anthropic's documented best practice gets a false alarm and a "validation" number that says the simulator is broken. That destroys trust on first contact.
- **Build.** Strip `cache_control` (and other non-content keys) from the prefix-comparison substrate. Record marker *positions and TTLs* as separate structured fields, and use them for the as-billed breakpoint model.
- **Savings.** n/a (a correctness prerequisite). **Effort:** S.

### F2. The simulator can only read at full-call boundaries, so "optimal-cache" is not optimal for shared-prefix/varying-suffix traffic [correctness, strong]

- **Evidence.** `_replay` stores one entry per call containing the *entire* rendered text (`simulator.py:206-216`) and matches with `text.startswith(entry.text)` (`:181-187`). A call can therefore only read a previous call's whole rendering. It cannot place a breakpoint "at the end of the shared portion", which Anthropic documents as the correct pattern for shared preambles ([SK] "Shared prefix, varying suffix").
- **Experiment exp2b (a3).** Six requests share a ~20k-char context and differ only in the question:
  - as-billed $0.0820 = optimal $0.0820 = fixed $0.0820
  - breakers `['history-rewrite']` (a false positive: independent requests are treated as rewrites)
  - explicit-breakpoint ideal ≈ **$0.0360 (56% cheaper)**
- **Also exp10.** A batch job recorded as one run (4,000 independent tickets) is labeled `history-rewrite`.
- **Build.** Block-level simulation:
  - candidate breakpoints at every block boundary;
  - the documented constraints (≤4 breakpoints, 20-position lookback, per-block TTL ordering);
  - a policy search that finds the cheapest *feasible* placement.
  
  Report "optimal (feasible policy)" rather than "optimal (end of messages)".
- **Savings.** Up to ~56% of input $ on RAG/classification workloads in this synthetic case. Anthropic's cookbook measured that one explicit breakpoint on the static prefix "roughly halved cost per task" ([SK] cost-optimization.md §2.1). **Effort:** L.

### F3. The simulator ignores concurrency and overstates achievable savings for fan-out [correctness, strong]

- **Evidence.** Calls are replayed in `(ts, index)` order, and an entry is readable by any later call regardless of in-flight status (`simulator.py:164, 180-189`). Docs: "a cache entry only becomes available after the first response begins" [PC], and "N parallel requests with identical prefixes all pay full price" ([SK] "Concurrent-request timing").
- **Experiment exp2b (a2).** Five byte-identical requests with the same `ts` (best-of-n / self-consistency): as-billed $0.0682, optimal **$0.0242**. The simulator claims **65%** savings the API cannot deliver.
- **Build.**
  - Record response-start time or TTFT, and duration.
  - Model entry visibility at `ts + ttft`.
  - Add a "stage the fan-out" recommendation: send 1, await the first token, then send N-1 ([SK]). It is a real lever, and it should be priced separately.
- **Savings.** For fan-out patterns, staging turns (N-1) cold writes into reads (≈90% off those prefixes at 0.1x). **Effort:** M.

### F4. Previous-call comparison conflates independent lanes (subagents, helper models, parallel tool loops) [correctness, strong]

- **Evidence.** Redundancy uses `common_prefix_chars(prev, call)` against the immediately preceding call (`analyzer.py:147-154`). Breakers classify consecutive pairs (`breakers.py:281-286`). The recorder allocates `index` in *completion* order (`instrument.py:245-247`), while the simulator sorts by `ts` (`simulator.py:164`), so components disagree on ordering.
- **Experiment exp2b (b2).** A main loop on Sonnet 5 interleaved with a Haiku subagent in one run: redundancy **0.042** versus per-lane **0.739 / 0.727**, a ~17x under-report. The only breaker is `model-switch` at call 1; every other cause is masked by priority.
- **Context.** Claude Code emits exactly this shape: `isSidechain` subagent messages, and OTel `query_source` = main/subagent/auxiliary [CCM].
- **Build.** Lane/thread reconstruction:
  - by explicit `parent_call_id` / `agent_id`;
  - else by (model, system hash, tools hash) plus longest-prefix match against *any* earlier call (a prefix-tree / radix index);
  - compute redundancy and breakers per lane.
- **Savings.** It fixes under-reporting. On lane-heavy agents the headline waste can be off by an order of magnitude. **Effort:** M.

### F5. Runs from different files that share a `run_id` overwrite each other and double-count [correctness bug, strong]

- **Evidence.** `cli._profile_and_render` stores `breakers[run.run_id]` and `scenarios[run.run_id]` in dicts (`cli.py:144-151`). `read_trace` is called per file and the lists are concatenated (`cli.py:218-220`). The report looks scenarios up by `run_id` (`report.py:228-236`).
- **Experiment exp3/exp2b (c2).** `day1.jsonl` ($2.00) + `day2.jsonl` ($20.00), both `run_id="nightly"`: each run section shows as-billed $20.00, and the overview billed total is **$40.00** (true **$22.00**).
- **Related risk.** Two processes appending to one file with the same fixed `run_id` both start at index 0, so `read_trace` rejects the whole file (`trace.py:414-419`). The recorder lock is per-process only (`instrument.py:127, 245`).
- **Build.** Globally unique run keys (source file + run_id, or UUIDv7), a dedupe policy, per-process shards, and a merge step.
- **Savings.** n/a (a correctness prerequisite for any finance use). **Effort:** S.

### F6. Pricing catalog coverage: current and cloud models missing, and one unknown model blanks org totals [correctness, strong]

- **Evidence.** `PRICING` has 9 rows (`pricing.py:49-73`). The live pricing page [P] also lists:
  - Opus 5.5 ($4/$20; **cache hits 0.05x = $0.20/MTok**)
  - Mythos 5.1 / Mythos 5
  - Opus 4.5 ($5/$25), Opus 4.1 and Opus 4 ($15/$75)
  - Sonnet 4.5 and Sonnet 4 ($3/$15)
  - Haiku 3.5 ($0.80/$4)
- **Experiment exp4-2.** `pricing_for()` returns `None` for all of the above and for:
  - `claude-sonnet-4-5-20250929`
  - Bedrock `anthropic.claude-sonnet-4-5-20250929-v1:0`, `us.anthropic.…`, `global.anthropic.…`
  - `claude-opus-4-8[1m]`
  
  Only the `-YYYYMMDD` / `@YYYYMMDD` suffixes are normalized (`pricing.py:95-110`).
- **Aggregation.** `_scenario_sum` returns `None` if *any* run lacks a price (`report.py:228-236`), and `_run_view` blanks a run with any unpriced call (`report.py:171-195`). In exp4-4, 99 priced runs ($198) plus 1 Opus 5.5 run produced a headline with no dollars at all.
- **Other limits.** `--model-price` can set only input/output (`cli.py:37-62, 207-216`); it cannot set cache multipliers or the min-cacheable prefix, which default to 1.25/0.10/1024.
- **Build.**
  - A versioned pricing catalog as data (JSON with effective-dated rows), refreshed from [P] each release.
  - Model-id normalization for 1P/Bedrock/Vertex/Foundry/`[1m]`.
  - Partial totals: "priced $X across N calls + M unpriced calls/tokens" instead of `None`.
  - Contract-rate overrides (negotiated discounts; CCU conversion for AWS/Foundry marketplaces [P]).
- **Savings.** n/a (accuracy). Without it, dollars are wrong or missing for current models. **Effort:** S.

### F7. No 1-hour TTL: write costs are understated and a major lever goes unrecommended [correctness + opportunity, strong]

- **Evidence.**
  - `Usage` has a single `cache_creation_input_tokens` (`trace.py:64-71`), priced at `cache_write_multiplier=1.25` (`pricing.py:41, 134-139`).
  - The API reports `usage.cache_creation.{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens}`, and 1h writes cost **2x** base [P][PC].
  - The recorder drops the breakdown (`instrument.py:101-108`; exp5 shows usage keys limited to 4).
  - The simulator has one TTL (`pricing.py:78`).
- **Experiment exp4-3.** 1M 1h-write tokens on Opus 5: Token Bill **$6.25**, published **$10.00** (−37.5%).
- **Observation from this machine's local Claude Code transcripts** (29 files, aggregates only, no content read): **100.0% of cache-write tokens were `ephemeral_1h`**. So every Claude Code cost Token Bill computes today would understate write dollars by 37.5%.
- **Experiment exp6-1.** Human-in-the-loop chat with 7-minute gaps: as-billed $0.1702, optimal/fixed $0.1374, **no breaker**. A 1h-TTL counterfactual is ≈ **$0.0539 (−68%)** and is never shown.
- **Guidance to encode** ([SK] prompt-caching.md "Choosing the TTL"):
  - The start-to-start gap decides: <5 min → 5m; 5–60 min → 1h; >1h → re-warm.
  - For Fable 5.1 (0.025x reads), a `max_tokens: 0` keep-alive is "usually cheaper than the 1-hour TTL".
- **Build.**
  - Per-TTL usage fields and per-TTL pricing.
  - Simulate 5m / 1h / keep-alive policies per lane from the gap distribution.
  - A "TTL-expiry" breaker with a priced fix.
- **Savings.** exp6: ~68% on HITL-style traffic. [SK] says the 1h TTL "pays for itself on the first prevented miss". **Effort:** M.

### F8. No pricing modifiers: batch, data residency, fast mode, priority, server tools [correctness + opportunity, strong]

- **Evidence.** Neither the pricing model nor the trace carries a service tier, geo, or speed (`pricing.py:30-43`, `trace.py:79-93`). Published modifiers [P]:
  - Batch: **50%** on input and output, stacks with cache multipliers.
  - `inference_geo: "us"`: **1.1x** on all categories for 4.6+.
  - Fast mode: Opus 5.5 **$8/$40**, Opus 5 / Opus 4.8 **$10/$50**, stacking with caching and geo.
  - Bedrock/Vertex regional endpoints: **+10%**.
  - Web search: **$10 per 1,000**; code execution **$0.05/container-hour** after 1,550 free hours.
  - Managed Agents runtime: **$0.08/session-hour**.
- **Observed.** Local Claude Code usage objects carry `speed`, `service_tier`, `inference_geo`, and `server_tool_use` (exp: key scan).
- **Build.**
  - Carry these fields.
  - Price exactly.
  - Detect levers: batchable non-interactive traffic ([SK]: "second-largest free lever"), fast-mode spend and its premium, US-geo premium, web-search spend.
- **Savings.**
  - Batch: 50% of eligible traffic.
  - Fast mode: premium avoided = 50% of fast-mode $ on Opus 5/4.8 at standard rates.
  - Geo: 9% of geo-pinned $ where residency is not required.
  
  **Effort:** M.

### F9. A single chars/3.7 constant is wrong across tokenizers, images, and hidden overheads [accuracy, strong]

- **Evidence.**
  - `CHARS_PER_TOKEN = 3.7` for all models (`trace.py:52`). It is used for segment attribution (`analyzer.py:104-111`), the min-cacheable gates (`simulator.py:185, 200`; `breakers.py:252-256`), and the simulated read/remainder split (`simulator.py:198`).
  - [P]: "Claude 4.7 and later models … produce approximately 30% more tokens for the same text." [SK] models.md says the same for Sonnet 5 versus 4.6.
  - Tool use adds a hidden system prompt of **286–675 tokens** per model [P], which is not in the rendered text.
- **Experiment exp4-1.** A 1 MB base64 PNG plus a text prompt: the image segment is attributed **99.58%** of billed input, and approx tokens of the rendering = **271,642** versus a billed total of 3,400. Real images cost roughly pixel-area/750; [SK] says 1280×720 caps near ~1,200 tokens.
- **Build.**
  - Calibrate a per-model, per-segment-kind density from the trace itself (the billed `total_input` per call versus rendered bytes by kind, via least squares).
  - Price images and PDFs by dimensions/pages, not base64 length.
  - Add per-model tool-overhead constants.
  - Optionally use the free `count_tokens` endpoint on a sample.
  - Report attribution error bars.
- **Savings.** n/a (accuracy of "where the money goes" and of threshold gates). **Effort:** M.

### F10. Canonicalization hides a top documented invalidator: non-deterministic key order [correctness, strong]

- **Evidence.** `canonical_json` sorts keys (`common.py:44-46`). `DESIGN.md:54-57` presents this as a feature ("a dict-ordering difference … can never masquerade as a prompt change"). But the provider hashes wire bytes:
  - [SK] silent invalidators: "`json.dumps(d)` without `sort_keys=True` / iterating a `set`: Non-deterministic serialization → prefix bytes differ".
  - [CD] `tools_changed`: "tool `input_schema` JSON was serialized non-deterministically".
- **Experiment exp9-1.** Tools alternate between two key orders, and billed shows a cold write every call. Token Bill reports **no breaker** and "billed cache reads 0 vs simulated 9367 (agreement 0.000)".
- **Build.** Hash blocks in *wire order* (as sent) for cache modeling, and also in sorted order. When the wire hashes differ but the sorted hashes match, raise a `serialization-churn` breaker with the fix "sort keys / deterministic serialization". This requires the recorder or proxy to keep wire order (see F14).
- **Savings.** Full cache recovery for affected services (the difference between broken caching and 0.1x reads on the prefix). **Effort:** S–M.

### F11. No 20-block lookback, automatic-caching, or 4-breakpoint semantics [correctness, strong]

- **Evidence.** The simulator assumes unlimited lookback and one ideal end-of-messages breakpoint (`simulator.py:24-29, 179-189`). The docs specify:
  - "The lookback window is 20 blocks … consecutive `tool_use` blocks count as one position, as do consecutive `tool_result` blocks" [PC];
  - automatic caching via a top-level `cache_control` [PC][P];
  - longer TTLs must precede shorter ones [PC].
- **Experiment exp6-2.** Each turn appends 25 text blocks, and billed shows full rewrites. Token Bill: optimal $0.0158 versus billed $0.0464, **no breaker**, agreement 0.000. The fix ("intermediate breakpoint every ~15 positions", [SK]) is never suggested.
- **Build.** A block-position model with run-collapsing, a lookback-overflow breaker, automatic-vs-explicit placement analysis (the "automatic breakpoint after unique tail = pure surcharge" signature, [SK]), and validation of breakpoint count and TTL ordering.
- **Savings.** Workload-dependent. Lookback overflow turns every turn into a full rewrite (1.25x or 2x on the whole context). **Effort:** M.

### F12. The breaker taxonomy misses the causes that dominate enterprise waste, and mislabels several [correctness + opportunity, strong]

- **Evidence.** Six kinds are hard-coded (`breakers.py:78-113`), with repairs at `:334-369`.
  - (a) **Compaction / context editing** (exp9-2) is labeled `history-rewrite` with "append new messages instead". But Anthropic reports compaction cut a long triage run's bill **a further 38%** ([SK] cost-optimization.md §2.3). A compaction is one cold miss traded for a smaller context, and it should be *priced*, not scolded.
  - (b) **Tool added mid-session** (exp9-3, e.g. an MCP server connecting) is labeled `tool-churn` with "sort them once". The documented fixes are `tool_addition`/`tool_removal` blocks or `defer_loading` tool search ([SK] prompt-caching.md "Invalidation hierarchy"; cost-optimization.md §2.2).
  - (c) **Legitimate system change (mode switch)** (exp9-5) is priced as $0.0462 "recoverable" by pinning the first call's system text (`breakers.py:362-363`). That contradicts `DESIGN.md:268-270`. The documented escape hatch is a mid-conversation `role: "system"` message on supported models ([SK]).
  - (d) **Write-never-read** (exp9-4): 20 one-shot requests with `cache_control` have **19% avoidable** spend and **0 breakers**. `detect()` returns `[]` for single-call runs (`breakers.py:277-278`), and the headline shows dollars only when breakers exist (`report.py:265-267`).
  - (e) **Invisible to the schema:** thinking/effort toggles ("always invalidate the messages cache"), `tool_choice` or image changes, thinking-block stripping on older models (Haiku 4.5 and earlier Opus/Sonnet), cross-workspace splits, and fork/subagent prefixes that don't copy the parent's `system`/`tools`/`model` ([SK] "Architectural guidance").
- **Build.** A pluggable detector registry, each detector carrying:
  - evidence;
  - a documented fix with its doc link;
  - a priced counterfactual;
  - confidence;
  - a "lever type" (free win versus tradeoff).
  
  Seed it with every row of [SK]'s anti-pattern tables and every [CD] `cache_miss_reason`.
- **Savings.** Covers the long tail that the current six kinds miss. **Effort:** M.

### F13. The trace schema lacks the envelope an enterprise optimizer needs [architecture, strong]

- **Evidence.** `Call` carries run_id, index, ts, model, system (flattened str), tools, messages, `cache_breakpoints` (a count), 4 usage ints, and stop_reason (`trace.py:79-93`). Missing:
  - **Request params:** `thinking`, `output_config.effort`, `tool_choice`, `max_tokens`, `speed`, `inference_geo`, `service_tier`, top-level `cache_control` + TTL, marker positions and TTLs, `betas`/`anthropic-beta`, `context_management`, `metadata.user_id`.
  - **Response:** `id`/request-id, served model, `usage.cache_creation{5m,1h}`, `server_tool_use`, `service_tier`, `inference_geo`, `speed`, `iterations`, `diagnostics`, TTFT, duration, error status.
  - **Attribution:** user, team, project/repo/branch, agent/lane/parent, workspace, api-key id (hashed).
  
  Several of these are exactly the "prompt-affecting parameters" behind [CD]'s `unavailable` reason (`tool_choice`, `thinking`, `context_management`, `output_config`, betas).
- **Build.** A `tokenbill/trace@2` schema: a content-free core (per-block hash chain in wire order, byte length, block kind, token estimate), an optional redacted content layer, full envelope and usage, delta encoding (parent reference + appended blocks), and a forward-compatible `extensions` map. Keep an `@1` reader.
- **Savings.** Enabler for F1–F12 and F16–F19. **Effort:** M.

### F14. Recorder coverage and fidelity gaps [correctness, strong]

- **Evidence (exp5, fake SDK double):**
  1. `client.beta.messages.create` is **not recorded** (4 calls made, 3 lines). `wrap` patches only `client.messages` (`instrument.py:138-186`). Beta features such as cache diagnostics, context management, and mid-conversation tool changes live there.
  2. A top-level `cache_control` is **not counted**: `cache_breakpoints` was 1 while the request carried 2 markers (`instrument.py:201-203` scans only system/tools/messages).
  3. System blocks are flattened to text (`"S1S2"`, `instrument.py:59-71`), which loses block boundaries and marker positions.
  4. Usage is reduced to 4 ints: cache_creation TTL split, `server_tool_use`, `service_tier`, `inference_geo`, and `speed` are dropped (`:101-108`).
  5. None of `thinking`, `output_config`, `tool_choice`, `speed`, `inference_geo`, `metadata`, or `max_tokens` is kept (`:194-204`).
  6. Passing `response.content` (SDK objects, the standard agent-loop idiom) is recorded as `repr` strings (`"TextBlock(citations=None, text='hi', type='text')"`, via `json.dumps(default=repr)` at `:249`). The trace is then no longer wire JSON.
  7. Payloads are serialized **after** the response. A caller that mutates message dicts inside a `with client.messages.stream(...)` block gets the *mutated* payload recorded (`"MUTATED-after-send"`), because `_capture_request` does a shallow `list(...)` copy (`:199`).
- **Further gaps (code reading):**
  - Raw `create(stream=True)` calls are skipped (documented, `:206-224`).
  - Streams whose consumer raises are not recorded even though input was billed (`:282-290`).
  - Batches, `with_raw_response`, and `count_tokens` are not covered.
  - Every call opens and appends the file under a global lock in the request path (`:245-250`), so multi-MB lines add latency and serialize threads.
  - There is no multi-process safety (F5).
- **Build.**
  - Serialize at call time (deep snapshot via `model_dump`/`to_dict`).
  - Wrap `beta.*` and batches.
  - Capture the full envelope.
  - Use a background writer with rotation.
  - Record failed or aborted attempts with partial usage from `message_start`.
  - Provide a **recording HTTP proxy** (`ANTHROPIC_BASE_URL`) as the primary enterprise path: language-agnostic, wire-exact bytes (fixes F10), no code changes.
- **Savings.** n/a (fidelity). **Effort:** M.

### F15. Scale: memory is quadratic in session length, and the simulator is quadratic in calls per run [scalability, strong]

- **Evidence.**
  - `read_trace` loads the whole file text and all parsed dicts (`trace.py:388-422`).
  - The CLI profiles every run while holding all runs (`cli.py:143-151`).
  - Render caches hold each call's full rendered text for the lifetime of the `Call` (`trace.py:120-170`).
  - The analyzer builds one `SegmentShare` per message per call (`analyzer.py:104-111`), which the report never uses (grep: `report.py` does not read `.segments`).
  - `_replay` re-sorts all entries per call (`simulator.py:180`).
  - `detect` builds evidence strings for every pair (`breakers.py:281-286`).
- **Experiments:**
  - exp7: peak RSS ≈ **2.5 bytes per rendered char**. A 400-call session (319M rendered chars, ~86M billed input tokens) needs 803 MB ("good" mode) and 1.16 GB ("volatile" mode).
  - exp8 (end-to-end CLI): a single 300-call Claude-Code-shaped session is a **184 MB** trace and takes 1.48 s and **1.25 GB** max RSS. Twenty 60-call sessions: 188 MB, 1.16 GB, 434 KB HTML.
  - exp10: one run of independent calls, `simulate` 1k → 0.55 s, 2k → 1.59 s, **4k → 6.26 s** (~4x per doubling, so O(n²)).
- **Extrapolation (my arithmetic, not measured):** at ~9.4 MB per 60-call session, 1,000 developers × 10 sessions/day ≈ **94 GB/day** of trace@1 JSONL. The current in-memory design would need roughly 6.5x that in RAM.
- **Build.**
  - Delta-encoded, content-free traces (hash chain, so linear size).
  - Streaming, per-lane processing with bounded memory.
  - A prefix-tree/hash index instead of per-call sorts.
  - A columnar store (Parquet/DuckDB locally; ClickHouse/BigQuery for org scale).
  - Incremental daily jobs.
  - Benchmarks at 10^6 calls in CI.
- **Savings.** n/a (feasibility at enterprise scale). **Effort:** L.

### F16. No ingestion for the enterprise's existing telemetry (Claude Code transcripts, Claude Code OTel, Admin API) [opportunity, strong]

- **Evidence.** `trace@1` is the only input format (`trace.py:378-422`), and it *requires* system/tools/messages (`trace.py:363-375`). The richest enterprise sources don't have them:
  - **Claude Code local transcripts** (`~/.claude/projects/*/*.jsonl`; key scan of this machine, no content read): lines carry `sessionId`, `requestId`, `isSidechain`, `gitBranch`, `cwd`, `timestamp`, `parentUuid`, and `message.usage` with `cache_creation{ephemeral_1h_input_tokens, ephemeral_5m_input_tokens}`, `inference_geo`, `service_tier`, `speed`, `server_tool_use{web_search_requests, web_fetch_requests}`, and `iterations`, plus `message.diagnostics`. **No system prompt or tool definitions.** Streamed responses repeat the same usage on several lines (**8,204 of 12,795** requestIds appear on >1 line), so naive summation double-counts. Dedupe by `message.id`/`requestId`.
  - **Claude Code OTel** [CCM]: `claude_code.token.usage` (type = input/output/cacheRead/cacheCreation) and `claude_code.cost.usage`, with attributes `model`, `query_source` (main/subagent/auxiliary), `speed`, `effort`, `agent.name`, `skill.name`, `plugin.name`, `mcp_server.name`, `mcp_tool.name`, `user.email`/`user.account_uuid`, `organization.id`, `session.id`, and optional `vcs.*`. The `claude_code.api_request` event carries `request_id`/`duration_ms`. Prompt content is redacted by default.
  - **Admin Usage & Cost API** [UC]:
    - `usage_report/messages` in 1m/1h/1d buckets, grouped and filtered by `api_key_id`, `workspace_id`, `model`, `service_tier`, `context_window`, `inference_geo`, and `speed`.
    - `cost_report` (daily USD, `group_by` description/workspace).
    - Priority Tier is *not* in the cost endpoint.
    - A separate Claude Code Analytics API provides per-user estimated costs.
- **Build.** A "usage-first" analysis mode that works without payloads:
  - TTL-gap simulation from timestamps ([SK]: "simulate the TTL walk … hit rate ~ 1 - e^(-λ·TTL)");
  - 1h-vs-5m economics;
  - hit-rate SLOs;
  - per-user/team/agent/skill/MCP attribution;
  - reconciliation against `cost_report`.
  
  Payload-level forensics (breakers) apply where the proxy or recorder exists.
- **Savings.** This is where enterprise-wide dollars are measured. Anthropic reports caching alone cut agent-loop cost **2.5–3.7x at 81–90% hit rates**, and a triage agent's bill fell **83%** ([SK] cost-optimization.md §2.1). **Effort:** M–L.

### F17. The server already tells you why the cache missed; Token Bill neither ingests nor validates against it [trust, strong]

- **Evidence.** [CD]: with beta `cache-diagnosis-2026-04-07`, responses carry `diagnostics.cache_miss_reason.type`:
  - `model_changed`, `system_changed`, `tools_changed`, and `messages_changed` (these four also carry `cache_missed_input_tokens`);
  - `previous_message_not_found`;
  - `unavailable` (which covers differing `tool_choice`/`thinking`/`context_management`/`output_config`/betas).
  
  Fingerprints are "only hashes and token-count estimates". Local Claude Code transcripts already contain these; tally over 29 files: previous_message_not_found 408, messages_changed 176, unavailable 92, model_changed 35, system_changed 31, tools_changed 27. The current "validation" is aggregate billed-vs-simulated reads per run only (`simulator.py:286-298`). The flagship test is circular, because demo usage is derived from the same 3.7 heuristic (`demo_traces.py` docstring lines 21-60; `DESIGN.md:209-246`).
- **Build.**
  - Ingest diagnostics as ground-truth labels.
  - Publish a confusion matrix of Token Bill breakers against server reasons.
  - Use the "diagnostics null + reads low → TTL expiry" rule from [CD]'s matrix.
  - Add a paid, opt-in live calibration suite that plants each breaker against the real API and asserts detection and agreement bounds.
- **Savings.** n/a (trust). This is the single strongest credibility feature for an enterprise review. **Effort:** S–M.

### F18. Cross-run and workspace-level cache sharing is not modeled [correctness, moderate]

- **Evidence.** `simulate(run)` replays each run with an empty cache (`simulator.py:159, 266-276`). Caches are shared across requests within a workspace (1P, AWS, Foundry) or organization (Bedrock/Vertex) [PC]. Thousands of Claude Code sessions sharing a large static system+tools prefix can hit each other's entries.
- **Consequences.**
  - Call 0 of each run is simulated cold even when billed shows reads, which deflates "agreement".
  - "Billed caching already beats the simulated fix" appears on healthy traffic (`report.py:274-281`).
  - Cross-workspace splits of identical traffic ([SK]: "check this before blaming a low hit rate on the prompt") and per-user system-prompt interpolation ("no cross-user sharing", [SK]) cannot be detected.
- **Build.** A workspace-scoped global replay keyed by the prefix hash chain; detectors for cross-workspace split and per-user prefix; and savings from "global static prefix" breakpoints.
- **Savings.** Workload-dependent. For fleets sharing a large static prefix, every session's first call can read instead of write. **Effort:** M. (Moderate: from code reading plus docs; not demonstrated with a live trace.)

### F19. No attribution dimensions, persistence, budgets, alerts, or regression gates [enterprise platform, strong]

- **Evidence.**
  - The only grouping key is `run_id` (`trace.py:96-101`).
  - The CLI is post-hoc and one-shot (`cli.py:98-122`) with no store, no time-series, no `--since`, and no diff between periods.
  - There are no budgets or alerts anywhere in the package.
  - The README lists "No live proxy yet" (`README.md:284-285`).
  - [SK] says the costliest failure is a *silent regression* after a prompt-assembly change and recommends "a standing check … or monitoring on the usage fields" (prompt-caching.md "Verifying cache hits").
- **Build.**
  - Attribution: user, team, cost center, project, repo, branch, agent, skill, MCP server, api key, workspace.
  - A persistent store with daily rollups.
  - Hit-rate and $/task SLOs per service.
  - Anomaly/regression detection (cache hit-rate drop after deploy).
  - Budgets and alerts (Slack/webhook/email).
  - A CI gate (`tokenbill check --min-hit-rate 0.8 --max-redundancy 0.1` with a nonzero exit).
  - Chargeback exports.
- **Savings.** Prevents regressions from persisting "for months" ([SK]). **Effort:** L.

### F20. Output formats and report scalability [usability, strong]

- **Evidence.**
  - HTML is one section per run with an SVG waterfall per run (`report.py:497-553`); exp8 shows ~21 KB per run, so 10k runs ≈ 210 MB of HTML.
  - The text summary prints ~15 lines per run (`report.py:704-779`).
  - The only outputs are stdout text and HTML (`cli.py:153-163`): no JSON, CSV, or Parquet, and no OTel or Prometheus export.
  - The headline shows dollars only when breakers exist (`report.py:265-267`), so pure savings like write-never-read are never headlined.
- **Build.**
  - `--format json|csv|parquet` with a stable result schema.
  - An executive summary: total $, measured hit rate, top-N levers ranked by $ with overlap deflation per [SK] ("ceilings that claim the same tokens are mutually exclusive").
  - Top-N offenders by user/service.
  - Paginated drill-down or an app.
  - Separate "measured" and "simulated" ceilings, labeled as [SK] prescribes.
- **Savings.** n/a (adoption). **Effort:** M.

### F21. Strict, all-or-nothing parsing and limited input handling [robustness, strong]

- **Evidence.**
  - Any bad line raises `TraceError` and aborts (`trace.py:393-421`). In exp4-5, one truncated line among 100 gave exit 1 and **zero output**.
  - There is no gzip, stdin, or glob/recursive input; files must be listed (`cli.py:101-103`).
  - Every run must be monotonic in file order (`trace.py:414-419`), so sorted merges from multiple sources fail.
- **Build.**
  - `--lenient`: quarantine bad lines to a sidecar file with counts, and continue.
  - Compressed and streamed input.
  - Directory/glob ingestion.
  - Sort-and-merge by (run, ts).
- **Savings.** n/a. **Effort:** S.

### F22. Validation, test, and CI gaps [quality, strong]

- **Evidence.**
  - There are no tests for images, 1h TTL, the beta namespace, concurrency, same-ts calls, duplicate run_ids, thinking/effort, Bedrock ids, lookback, compaction, key order, `server_tool_use`, or recorder thread safety (grep over `tests/*.py`).
  - There is no property-based or fuzz testing, no coverage measurement, and no type checking (mypy/pyright), despite `py.typed` (`pyproject.toml:41-67`).
  - CI runs Ubuntu only on Python 3.10 and 3.13 (`.github/workflows/ci.yml:28-39`), although the code has Windows cp1252 handling (`cli.py:230-244`).
  - The performance test's budget is a "loose" 20 s for 800 calls (`tests/test_performance.py:82-91`), with no memory budget.
- **Build.**
  - Golden traces from real API runs covering each breaker, and a corpus from Claude Code.
  - Hypothesis fuzzing of parser and renderer invariants.
  - Coverage ≥90% as a gate.
  - `mypy --strict`.
  - A macOS/Windows/3.12/3.14 matrix.
  - Memory and throughput benchmarks at 10^5–10^6 calls.
  - The live calibration suite (F17).
- **Savings.** n/a (enterprise acceptance). **Effort:** M.

### F23. Security and privacy posture is not enterprise-ready [security, strong]

- **Evidence.**
  - Traces contain full prompts by design (`SECURITY.md:3-8, 28-32`).
  - There is no redaction or hashing mode.
  - Report evidence embeds raw excerpts (`breakers.py:172-177`, `report.py:485`).
  - The terminal summary prints `run_id` and evidence verbatim. Terminal-escape injection is acknowledged but unfixed (`SECURITY.md:63-70`; `report.py:705, 122-124` only strips `\r\n`).
  - The recorder writes traces with default file permissions (`instrument.py:248`).
  - There are no SBOM, signing, RBAC, or audit concepts (a CLI today).
- **Build.**
  - Content-free hashing by default (matching [CD]'s own design).
  - Opt-in excerpts with PII/secret scrubbing.
  - Sanitize control characters on every text sink.
  - 0600 file permissions.
  - Keep the no-network guarantee for the CLI.
  - For the server edition: SSO/RBAC, audit logs, retention policies, SBOM, and signed releases.
- **Savings.** n/a (it's a gate for enterprise approval). **Effort:** M.

### F24. Code-quality issues that matter as the codebase grows [maintainability, moderate]

- **Private cross-module coupling.** `breakers` imports `simulator._as_billed/_replay` (`breakers.py:43-47`).
- **Process-global mutable state.**
  - `--model-price` writes into the module-level `PRICING` (`cli.py:207-216`).
  - `_warned_models` is global (`pricing.py:92`).
  - Library callers running concurrent analyses with different prices will interfere.
- **Defensive duck-typing of the package's own contracts.** `_DOLLAR_ALIASES` (`report.py:66-73`) and `_segment_parts` (`analyzer.py:84-94`) hide contract drift instead of failing.
- **Duplicated constants.** `SCHEMA` and `_USAGE_FIELDS` exist in both `instrument.py:49-56` and `trace.py:43, 247-252`.
- **Wasted work.** `CallProfile.segments` is O(n²) and unused by the report (`analyzer.py:104-111`); evidence strings are built for every pair then discarded (`breakers.py:281-286`); `_changed_window` is a pure-Python per-character loop (`breakers.py:145-155`).
- **Unconventional render caches.** The `id()`-keyed render caches with weakref finalizers (`trace.py:120-170`) are clever but fragile. A content-hash-keyed cache or precomputed hash chain is simpler, and it survives multiprocessing.
- **Build.** A result schema (pydantic-free dataclasses to JSON), dependency injection of a `PricingCatalog`, and a stage-pipeline architecture (ingest → normalize → lane → simulate → detect → rank → render).
- **Effort:** S.

### F25. The tool is cache-only, but enterprise bills are also driven by batchability, effort, model mix, output, tool schemas, media, and loop hygiene [scope, strong]

- **Evidence.** Token Bill covers caching and prefix redundancy only (`docs/SPEC.md:8-25`). Anthropic's cost workflow ranks free wins, then tradeoffs, with measured effects ([SK] cost-optimization.md):
  - caching: **2.5–3.7x** on agent loops;
  - batch: **50%**;
  - compaction: **−38%** on a long triage run;
  - prompt audit: **14%** cheaper, and more accurate;
  - programmatic tool calling: **24%** fewer input tokens;
  - tool search with `defer_loading` pays past **~10K** schema tokens;
  - image downscaling: 1280×720 caps an image near **~1,200 tokens**;
  - effort: `low` plus re-running failures at default gave ~93% pass at **~$0.70/task** versus 91.7% at **$1.39** all-default;
  - `max_tokens` truncation: a 16,384-token cap ended 15% of Opus 5 attempts unsolved.
- **Build.** Detectors computable from traces and telemetry:
  - tool-schema token share (and the tool-use overhead from [P]);
  - image/PDF bytes and dimensions;
  - tool-result accumulation (→ pruning, subagent, compaction);
  - `stop_reason=max_tokens` retries;
  - duplicate requests;
  - non-interactive traffic → batch;
  - fast-mode and geo premiums;
  - output/thinking share by effort;
  - P90/P99 session tail ("two problems carried 43% of the spend", [SK]).
  
  Tradeoff levers are labeled "needs an eval" and never auto-applied (per [SK]).
- **Savings.** Additive to caching; see the per-lever figures above. **Effort:** L.

### F26. No realized-savings ledger or cost-per-completed-task metric [enterprise value proof, moderate]

- **Evidence.** Reports are counterfactual snapshots (`simulator.py`, `report.py`). Nothing tracks before/after per lever, "cost per completed task", or post-cutover confirmation. [SK] frames the unit as "cost per completed task, not cost per token" and prescribes measuring each lever one diff at a time, then confirming "in the usage and cost reports after cutover".
- **Build.**
  - A lever lifecycle: detected → proposed → applied (linked commit/PR) → measured (pre/post windows, same traffic class) → realized $ with a confidence interval.
  - Org dashboards of realized versus projected savings.
  - Optional task/outcome joins (PR merged, tests pass, ticket closed; Claude Code OTel has `commit.count`/`pull_request.count` [CCM]).
- **Savings.** This is how the tool *proves* savings to finance. **Effort:** M.

---

## 3. What Token Bill should build (prioritized roadmap)

### Phase 0: stop being wrong (1–2 weeks, all S-effort, each demonstrable by a regression test from this report's experiments)

1. Strip `cache_control` from the diff substrate, and record marker positions and TTLs separately (F1).
2. Globally unique run keys; dedupe; per-process shard files (F5).
3. Pricing catalog v2:
   - add Opus 5.5 (0.05x reads), Mythos 5/5.1, Opus 4.5/4.1/4, Sonnet 4.5/4, Haiku 3.5;
   - add 1h writes at 2x;
   - normalize Bedrock/Vertex/`[1m]` ids;
   - report partial totals instead of `None` (F6, F7).
4. Recorder:
   - serialize at call time;
   - `model_dump` SDK objects;
   - wrap `beta.messages` and batches;
   - count top-level `cache_control`;
   - keep the full `usage` (TTL split, server tools, tier, geo, speed) and the request envelope (F14).
5. `--lenient` quarantine; control-character sanitization (F21, F23).
6. Headline all simulated savings, not only breaker-attributed savings (F12d, F20).

### Phase 1: a faithful simulator and taxonomy (1–2 months)

7. `trace@2`: a content-free, wire-order, block-level hash chain with delta encoding; full envelope and attribution; keep the `@1` reader (F13, F10, F23).
8. A block-level policy simulator:
   - ≤4 breakpoints; 20-position lookback with run-collapsing;
   - 5m / 1h / keep-alive (`max_tokens: 0`);
   - automatic-vs-explicit placement;
   - concurrency visibility at TTFT;
   - workspace-scoped cross-run cache;
   - a policy search yielding the cheapest *feasible* configuration (F2, F3, F7, F11, F18).
9. Lane reconstruction via a prefix-tree index (F4).
10. Calibrated token model: per-model and per-kind density from billed totals; pixel-based image pricing; tool-overhead constants; error bars (F9).
11. A detector registry covering every documented invalidator and [CD] reason, including serialization churn, TTL expiry, write-never-read, lookback overflow, compaction economics, tool additions (`tool_addition`/`defer_loading`), and thinking/effort toggles (F12).
12. Pricing modifiers: batch, geo, fast, priority, server tools, cloud regional +10%, contract discounts, CCU (F8).

### Phase 2: enterprise ingestion and platform (2–4 months)

13. Ingest adapters:
    - Claude Code transcripts (dedupe by message id/requestId; `isSidechain` lanes; `gitBranch`/`cwd`);
    - an OTLP receiver for Claude Code OTel;
    - Admin Usage/Cost API pullers;
    - the Claude Code Analytics API;
    - a recording HTTP proxy (`ANTHROPIC_BASE_URL`);
    - OpenAI/Gemini/Bedrock/Vertex adapters (F16, F14).
14. Ground truth: ingest `diagnostics.cache_miss_reason`; publish a breaker-versus-server confusion matrix; add the paid live calibration suite (F17, F22).
15. Streaming engine plus a columnar store (DuckDB/Parquet locally, ClickHouse/BigQuery at org scale); 10^6-call benchmarks with memory budgets in CI (F15).
16. Attribution and FinOps:
    - user/team/project/repo/agent/skill/MCP dimensions;
    - budgets, alerts, and anomaly detection;
    - a CI gate;
    - chargeback exports;
    - JSON/CSV/Parquet and OTel metric outputs;
    - an executive report with an overlap-deflated, ranked lever shortlist (F19, F20).
17. Beyond-cache levers:
    - batch eligibility, tool-schema bloat, image downscaling;
    - loop hygiene (pruning, compaction, subagents);
    - truncation retries, output shape;
    - effort and model tradeoffs, flagged "needs eval" (F25).
18. A realized-savings ledger and cost per completed task (F26).
19. Hardening: `mypy --strict`, coverage gate, Hypothesis fuzzing, multi-OS CI, SBOM, signed releases, redaction-by-default, RBAC/SSO/audit for any server component (F22, F23).

---

## 4. Open questions (need a decision or a live experiment)

1. **Key-order sensitivity in practice.** Does the Python/TS SDK always preserve dict insertion order on the wire? The docs say key order breaks caching; a two-request live probe would confirm before shipping the `serialization-churn` detector.
2. **Aborted streams.** Are cancelled or aborted streams billed for input and partial output? That decides whether the recorder should log partial usage from `message_start`.
3. **Claude Code TTL default.** Is the 100%-1h-write pattern seen in this machine's Claude Code transcripts the default for all Claude Code plans and versions? It was observed on one machine only.
4. **Enterprise platform.** Which platform does the target enterprise use (1P API, Claude Platform on AWS, Bedrock, Vertex, Foundry)? Cache isolation, cache diagnostics, the Admin API, and fast mode all differ by platform.
5. **Content policy.** Will the enterprise permit storing any prompt content? If not, the content-free hash-chain design is mandatory, and evidence excerpts must be opt-in.
6. **Contract pricing.** What are the negotiated discounts, EDP, or CCU conversions? Without them, dollars are list-price estimates, not invoice-reconciled.
7. **Priority Tier.** It is absent from the cost endpoint [UC]. How should it be priced and reconciled?
8. **TTFT capture.** Can response-start timestamps be captured (proxy or OTel `duration_ms`) to model concurrency visibility precisely?
9. **Accuracy target.** What accuracy bar does the enterprise lab need for "trustworthy", e.g. trace-derived $ within ±1% of `cost_report`, and breaker precision/recall against `cache_miss_reason`?
10. **Scope of "best bill optimizer".** Should the tool stay analysis-only, or also *act*? Examples: a proxy that rewrites requests (sorts keys, adds breakpoints, stages fan-out), or a CI bot that opens PRs. Acting raises safety and approval requirements; tradeoff levers must never be auto-applied ([SK]).
