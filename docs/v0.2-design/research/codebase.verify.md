# Fact-check: research track "codebase" (Token Bill v0.1.2)

Verifier: adversarial fact-check subagent. Date: 2026-09-23.
Report checked: `research/codebase.md`.

## Method

- **Code claims.** Re-read every cited `file:line` in the read-only clone (`tokenbill/*.py`, `DESIGN.md`, `SECURITY.md`, `README.md`, `docs/SPEC.md`, `pyproject.toml`, `.github/workflows/*.yml`, `tests/*.py`).
- **Experiments.** Re-ran each experiment from `research/exp/` unmodified: exp1, exp2, exp2b, exp3, exp4, exp5, exp6, exp7 (1x400 good/volatile), exp9, and exp10 (n = 50/1k/2k/4k). Re-ran the CLI on `s300.jsonl` and `m20x60.jsonl` under `/usr/bin/time -l`, with Python 3.12 and 3.13. Outputs went to `research/vcb_tmp/`.
- **Local Claude Code transcripts.** Aggregate-only rescan of the same 29 files (`~/.claude/projects/*/*.jsonl`), reading keys, numeric usage, and categorical `cache_miss_reason` only, with no content. Script: `research/vcb_cc_agg.py`.
- **Extra probes (not in the report).**
  - Overview totals with and without the unpriced run.
  - A two-session cross-run cache case.
  - A key-presence scan for `system`/`tools`.
  - PyPI provenance for tokenbill 0.1.2.
- **Web sources (all opened 2026-09-23).** None shows a page date unless noted.
  - Pricing: https://platform.claude.com/docs/en/about-claude/pricing
  - Prompt caching: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
  - Cache diagnostics (beta `cache-diagnosis-2026-04-07`): https://platform.claude.com/docs/en/build-with-claude/cache-diagnostics
  - Usage & Cost API: https://platform.claude.com/docs/en/manage-claude/usage-cost-api
  - Claude Code monitoring: https://code.claude.com/docs/en/monitoring-usage
  - Optimizing for cost and intelligence: https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence (raw `.md` downloaded)
  - Cost optimization cookbook: https://platform.claude.com/cookbook/cost-optimization-cost-optimization (published 2026-08-09 per page)
  - Vision: https://platform.claude.com/docs/en/build-with-claude/vision (raw `.md`)
  - https://github.com/pypa/gh-action-pypi-publish
  - PyPI JSON and integrity endpoints for tokenbill 0.1.2
- **Bundled skill.** `/private/tmp/claude-501/bundled-skills/2.1.280/.../claude-api/shared/{prompt-caching,cost-optimization,live-sources}.md`.

## Headline cautions for the synthesizer

1. **The bundled skill's measured-lever numbers are stale relative to the live public guide.** "Optimizing for cost and intelligence" (raw .md, fetched today) reports different numbers from `shared/cost-optimization.md` (Claude Code 2.1.280):

   | Lever | Bundled skill | Live guide |
   |---|---|---|
   | Caching | 2.5–3.7x at 81–90% hit rates | 2.7–5.3x, runs reading 79–90% of input from cache |
   | Compaction on a long run | "a further 38%" | 32% (the cookbook's own compaction demo shows 16%) |
   | Effort escalation | ~93% at ~$0.70 vs 91.7% at $1.39 | ~93% at ~$0.45 vs 91.7% at $0.93 (same "half the cost" ratio) |
   | 16,384-token cap, Fable | "a third of Fable 5's" attempts | 43% of Fable 5.1's attempts |

   These figures are unchanged in the live guide: 83% triage-agent cut, batch 50%, prompt audit 14%, programmatic tool calling 24%, 16,384 cap ending 15% of Opus 5 attempts, and two problems carrying 43% of spend. The "~10K tokens" tool-search threshold appears only in the bundled skill. The live guide instead reports tool search cutting 45% of cost with 500 tool definitions and 20% with a GitHub MCP server. Cite the live guide numbers.
2. **The synthetic experiments are real, but their "billed" usage is hand-constructed.** For example: "consistent with a working cache", "cold write every call", "~1.6k image tokens". They show how Token Bill reacts to a given billed pattern. They do not show real API behavior.
3. **Several correctness gaps are partly acknowledged in the repo.** README "Known limitations" says the simulator models "no concurrency races". `DESIGN.md` §6 says the demo "does not prove" production cache behavior. The findings are still valid.

## Per-finding verdicts

### cb-moving-marker: CONFIRMED
- `trace.py:150-154`: `_render_messages` uses `canonical_json(message)`, which includes `cache_control` keys.
- The recorder stores messages as sent. The `list(messages)` copy is at `instrument.py:200`; line 199 is tools, a trivial off-by-one.
- Re-ran exp1:
  - `[('history-rewrite', 1, None)]`
  - optimal = no-cache = $0.040274, versus as-billed $0.0148494
  - "billed cache reads 14997 vs simulated 0 (agreement 0.000)"
- The skill's prompt-caching.md "Finding the invalidator" says: "Strip `cache_control` markers before diffing: the moving marker always differs … and is not an invalidator."
- The multi-turn pattern puts the marker on `messages[-1].content[-1]`.
- The public prompt-caching page does not state the strip rule; the skill does.

### cb-shared-prefix-sim: CONFIRMED
- `simulator.py:179-216`: one full-rendering entry per call, matched by `startswith`, with a single end-of-messages breakpoint.
- Re-ran exp2b a3:
  - as-billed = optimal = fixed = $0.0820
  - breakers `['history-rewrite']`
  - hand-computed explicit-breakpoint ideal ≈ $0.0360 (56%). This is an estimate that ignores gates.
- exp10 labels a 50–4,000-call batch as `history-rewrite`.
- The skill's "Shared prefix, varying suffix" section supports the claim. So does §2.1 ("roughly halved"): the cookbook reports "54% cheaper vs variable-first" (published 2026-08-09).

### cb-concurrency: CONFIRMED
- `simulator.py:164`: sorts by `(ts, index)`. Entries are readable immediately (`:180-189`).
- Re-ran exp2b a2: as-billed $0.0682, optimal $0.0242 (65%).
- The prompt-caching page says: "a cache entry only becomes available after the first response begins." The skill says "N parallel requests … all pay full price".
- README acknowledges "no concurrency races" as a limitation, but the report still presents the 65% as achievable.

### cb-lanes: CONFIRMED
- `analyzer.py:147-154`: redundancy is measured against the previous call.
- `breakers.py:281-286`: classification works on consecutive pairs.
- `instrument.py:245-247`: index is allocated at completion, while `ts` is taken before the request (`:152`, `:162`).
- Re-ran exp2b b2: 0.042 versus 0.739 / 0.727 (≈17x). exp2 (b): breakers `[('model-switch', 1)]` only.
- The monitoring docs list `query_source` = main/subagent/auxiliary.
- The local transcripts carry `isSidechain`.

### cb-dup-runid: CONFIRMED (note)
- `cli.py:144-151`: dicts keyed by `run_id`. `cli.py:218-220`: per-file reads are concatenated. `report.py:228-236`: `_scenario_sum`.
- Re-ran exp3: each run's scenario row shows as-billed $20.00, while its "billed dollars" line correctly shows $2.00 and $20.00.
- exp2b c2: `_overview.billed_usd` = $40.00 against a true $22.00.
- **Note.** `billed_usd` is only printed in the headline when breakers exist (`report.py:265-289`). In exp3, with no breakers, the $40 is computed but not displayed; the visible symptom is the duplicated $20 rows.
- Same-run_id multi-process appends: each Recorder starts `_index=0` behind a `threading.Lock` (`instrument.py:126-127`), and `read_trace` raises on the non-monotonic index (`trace.py:414-419`), aborting the whole file. Confirmed by code. The default `run_id` is a random UUID, so this needs a fixed `run_id`, as the finding states.

### cb-pricing-coverage: CORRECTED
- **Confirmed parts.**
  - `PRICING` has 9 rows.
  - `pricing_for` returns None for every listed id. exp4-2 re-run showed None for opus-5-5, mythos-5-1, opus-4-5, sonnet-4-5, opus-4-1, `claude-sonnet-4-5-20250929`, all three Bedrock forms, and `[1m]`. Only the `@YYYYMMDD` Vertex form resolves.
  - The pricing page confirms: Opus 5.5 $4/$20 with 0.05x hits; Mythos 5/5.1; Opus 4.5 $5/$25; Opus 4.1/4 $15/$75; Sonnet 4.5/4 $3/$15; Haiku 3.5 $0.80/$4.
  - `--model-price` is IN,OUT only (`cli.py:37-62, 207-216`), so cache multipliers and min-prefix stay at defaults.
  - The org `_scenario_sum` becomes None. My probe on `unknown.jsonl`: `billed_usd=None` with the Opus 5.5 run, $198.00 without it.
- **Correction.** The exp4-4 evidence is confounded. The headline "no dollars" happens because zero breakers were detected, and the headline never shows dollars without breakers (`report.py:266-267`). It would have lacked dollars even with all 100 runs priced. The real effect of an unpriced run: org `billed_usd`/`recovered_usd` become None. When breakers exist, the headline says "(dollar impact unknown: unpriced model)". Per-run dollars for the other 99 runs are still shown.

### cb-1h-ttl: CONFIRMED
- `Usage` has 4 fields. The write multiplier is 1.25 and the TTL is 300 s. `_usage_dict` drops the `cache_creation` breakdown, as exp5 shows.
- The pricing page gives 1h writes at 2x, with Opus 5 at $10 versus $6.25, so understated by 37.5%.
- exp4-3 re-run: $6.25.
- exp6-1 re-run: as-billed $0.1702, optimal/fixed $0.1374, no breakers. The hand-computed 1h counterfactual is $0.0539 (68%).
- Local rescan of the same 29 files, deduped by requestId: `ephemeral_1h` 137,696,532 versus `ephemeral_5m` 29,428 tokens, so **99.98%** are 1h. "100%" is rounding.
- The skill's "Choosing the TTL" section covers the Fable 5.1 `max_tokens: 0` keep-alive. The live guide also shows keep-alive 13–20% cheaper than 1h on Fable 5.1 for minute-scale pauses, and says it saved nothing over 1h on Sonnet 5/Opus 5.

### cb-modifiers: CONFIRMED
- `ModelPricing` (`pricing.py:30-43`) has no tier, geo, or speed fields.
- The pricing page confirms:
  - batch 50%, stacking with caching;
  - `inference_geo: "us"` 1.1x on 4.6+ models;
  - fast mode at $8/$40 on Opus 5.5 and $10/$50 on Opus 5 and 4.8;
  - regional/multi-region +10% on Bedrock/Vertex for Claude 4.5+ models;
  - web search $10 per 1,000;
  - code execution $0.05 per container-hour after 1,550 free hours per org per month, and free when used with web search/fetch 20260209+.
- The local transcript usage keys include `speed`, `service_tier`, `inference_geo`, `server_tool_use`, `cache_creation`, and `iterations`.

### cb-chars-per-token: CORRECTED
- **Confirmed parts.**
  - `CHARS_PER_TOKEN = 3.7` (`trace.py:52`).
  - Pricing page: "approximately 30% more tokens" on 4.7+. The tool-use system prompt is 286 (Opus 5.5/5) to 675 (Opus 4.7) tokens for tool_choice auto/none. It rises to 804 with any/tool, and Haiku 3.5, which is retired, is 264.
  - exp4-1 re-run: image segment share 0.9958; approx 271,642 versus a synthetic billed 3,400.
  - Vision docs: ⌈w/28⌉×⌈h/28⌉ tokens, so 1280×720 ≈ 1,196, consistent with ~1.2k.
- **Correction.** The 3.7 constant does **not** drive segment attribution or the simulated read/remainder split. Both are pure char fractions:
  - `analyzer.py:106-111`: `total_input * chars/rendered_chars`
  - `simulator.py:198`: `billed_input * matched_chars / chars`
  
  `approx_tokens`/3.7 is used only for the min-cacheable gates (`simulator.py:185, 200`; `breakers.py:255`) and for display. The image misattribution comes from the uniform chars-per-token assumption behind char-fraction attribution, not from the 3.7 value. A uniform ~30% tokenizer shift therefore does not change attribution, which is rescaled to billed totals. It only shifts the gates.

### cb-key-order: CONFIRMED (note)
- `common.py:44-46`: `sort_keys=True`.
- `DESIGN.md:54-57` presents this as a feature.
- exp9-1 re-run: breakers `[]`, "billed cache reads 0 vs simulated 9367 (agreement 0.000)".
- Cache diagnostics says `tools_changed` includes "tool `input_schema` JSON was serialized non-deterministically". The skill's silent-invalidators table lists `json.dumps(d)` without `sort_keys`.
- **Notes.**
  - "The provider hashes wire bytes" is an inference; the docs only say non-deterministic serialization changes the prefix.
  - The repo has a test that locks in the current behavior: `tests/test_trace.py:117`, `test_dict_key_order_never_looks_like_a_prompt_change`.
  - The exp's billed pattern is synthetic.

### cb-lookback: CONFIRMED
- `simulator.py:24-29, 179-189`: single end breakpoint, and no lookback limit in code.
- The prompt-caching page says: "The lookback window is 20 blocks … a run of consecutive `tool_use` blocks counts as one position, and so does a run of consecutive `tool_result` blocks". It also covers automatic caching via a top-level `cache_control`, and says longer TTLs must appear before shorter ones.
- exp6-2 re-run: as-billed $0.0464, optimal $0.0158, breakers `[]`, agreement 0.000.

### cb-breaker-taxonomy: CORRECTED
- **Confirmed parts.**
  - There are six kinds (`breakers.py:78-113`).
  - Single-call runs are skipped (`:277-278`). The headline shows dollars only with breakers (`report.py:265-267`).
  - exp9 re-run:
    - compaction → `history-rewrite` with "append new messages instead…";
    - tool added → `tool-churn` with the sort fix;
    - mode switch → `system-edit` $0.0462, repaired by pinning `calls[0].system` (`breakers.py:362-363`), which is in tension with `DESIGN.md:268-270`;
    - write-never-read: $0.3382 versus $0.2726 (19%), 0 breakers.
  - Thinking/effort/tool_choice are not in the schema.
  - `tool_addition`/`defer_loading` and `role:"system"` are the documented fixes, per the skill's "Invalidation hierarchy" and cost-optimization §2.2.
- **Correction 1.** "Compaction cut a long run a further 38%" comes only from the bundled skill. The live guide says compaction saved 32% on the long run (pruning 39%), and the cookbook demo shows 16%.
- **Correction 2.** `tool_addition` blocks and mid-conversation `role:"system"` are **not available on Claude Sonnet 5**, the model used in exp9. They work on Opus 5/4.8, Fable 5/5.1, and Mythos 5/5.1 only. For the exp's Sonnet 5 traffic, the applicable escape hatch is `defer_loading` / tool search, or keeping the mode as message content.

### cb-schema-envelope: CONFIRMED
- `trace.py:64-101`: Usage/Call/Run fields exactly as described.
- Cache diagnostics: `unavailable` covers `tool_choice`, `thinking`, `context_management`, `output_config`, `output_format`, and the set of `anthropic-beta` headers.

### cb-recorder-coverage: CONFIRMED (note)
- exp5 re-run:
  - 3 lines for 4 calls; beta is not recorded;
  - `cache_breakpoints` 1 while the request carried 2 markers;
  - system `'S1S2'`;
  - 4 usage keys;
  - no request params kept;
  - content recorded as a `"TextBlock(…)"` repr;
  - `"MUTATED-after-send"`.
- Code: `wrap` covers `client.messages` only. `_count_cache_control` scans only system/tools/messages. Streams that raise record nothing (`:282-290`). Raw `stream=True` is skipped (`:206-224`).
- **Notes.**
  - The lock is a per-`Recorder` `threading.Lock` (effectively process-wide for one recorder), not a module-global lock.
  - exp5 uses a stand-in TextBlock class, but real pydantic SDK blocks also fall to `default=repr`.

### cb-scale-memory: CORRECTED
- **Confirmed.** The mechanisms:
  - full payload per call;
  - whole-file `read_text`;
  - render caches;
  - O(Σ messages) `SegmentShare` objects, never read by `report.py`;
  - `sorted(entries)` per call, plus a linear `existing` scan, with entries never pruned.

  exp10 still labels the batch run `history-rewrite`.
- **Numbers from my re-runs differ from the report.**

  | Measurement | Report | Re-run |
  |---|---|---|
  | s300 (184 MB trace) CLI max RSS | 1.25 GB | 1,162,592,256 B (py3.13) and 1,172,455,424 B (py3.12), i.e. ≈1.16–1.17 GB |
  | simulate(), 1k / 2k / 4k calls | 0.55 / 1.59 / 6.26 s | 0.46 / 1.29 / 5.20 s (still ≈2.8–4x per doubling) |
  | exp7, 400-call session peak RSS | ~2.5 B/char | 828 MiB ("good") and 1,128 MiB ("volatile") for 318.9M chars, ≈2.7–3.7 B/char |

- The 94 GB/day figure is a labeled extrapolation. The arithmetic (9.4 MB × 10 × 1,000) is right, but the sessions/day and shape are assumptions.

### cb-ingestion: CONFIRMED
- `trace.py:363-375`: system/tools/messages are required keys (`_get` raises "missing field").
- Local rescan:
  - 29 files; 12,795 requestIds, 8,204 on more than one line (exact match);
  - keys present: `sessionId`, `requestId`, `isSidechain`, `gitBranch`, `cwd`, `parentUuid`, `message.diagnostics`;
  - usage includes the 1h/5m split, `speed`, `service_tier`, `inference_geo`, `server_tool_use`;
  - no `system`/`tools` keys anywhere.
- The monitoring docs confirm:
  - `token.usage` and `cost.usage`, with `model`, `query_source`, `speed`, `effort`, `agent.name`, `skill.name`, `plugin.name`, `mcp_server.name`, and `mcp_tool.name`;
  - `user.email` and `user.account_uuid`;
  - prompts redacted by default.
- The Usage & Cost API page confirms:
  - `usage_report/messages` in 1m/1h/1d buckets, filtered or grouped by API key, workspace, model, service tier, context window, `inference_geo`, and speed (speed needs a beta header);
  - `cost_report` daily in USD;
  - "Priority Tier costs … are not included in the cost endpoint".
- **Extra facts worth carrying.**
  - The Admin API is unavailable on Claude Platform on AWS.
  - Claude Enterprise orgs use the Analytics API instead.

### cb-cache-diagnostics: CONFIRMED
- The docs confirm:
  - the beta header;
  - the 6 reason types, with `cache_missed_input_tokens` on the four `*_changed` types;
  - fingerprints "contain only hashes and token-count estimates".
- Local tally, exact match: `previous_message_not_found` 408, `messages_changed` 176, `unavailable` 92, `model_changed` 35, `system_changed` 31, `tools_changed` 27.
- Validation is a single aggregate ratio (`simulator.py:286-298`).
- Demo usage is derived from `int(len/3.7)` (`demo_traces.py` docstring; `DESIGN.md:209-246`). The circularity is partly acknowledged in DESIGN §6.
- Note: cache diagnostics is available on the Claude API only (not AWS, Bedrock, Vertex, or Foundry).

### cb-cross-run-cache: CORRECTED
- **Confirmed.** Each `_replay` starts with `entries = []` (`simulator.py:159`), and `simulate` is per-run. The prompt-caching page confirms workspace-level isolation on the Claude API, AWS, and Foundry, and org-level isolation on Bedrock and Google Cloud.
- My probe: two healthy sessions sharing a static prefix, with session B's call 0 billed as a read of A's entry. Session B showed "agreement 0.855", and simulated optimal ($0.01152) came out above as-billed ($0.00915).
- **Correction.** The "billed caching already beats the simulated fixed-cache policy" wording (`report.py:274-281`) appears only when at least one breaker is detected. On healthy traffic with no breakers, the headline says "no cache breakers detected". The symptom there is deflated agreement and optimal > as-billed.

### cb-attribution-platform: CONFIRMED
- The `Run` key is `run_id` only.
- The CLI options have no store, `--since`, budgets, or alerts (package-wide grep).
- README says "No live proxy yet."
- The skill's "Verifying cache hits" section describes a silent regression that "goes unnoticed for months" and recommends "a standing check … or monitoring on the usage fields".

### cb-report-scale: CORRECTED (minor)
- **Correction.** The m20x60 HTML is **433,609 bytes** for 20 runs (~21.7 KB/run), not 434,609. Per-run size depends on calls per run: the one-run, 300-call s300 HTML is 94,822 bytes.
- **Confirmed.**
  - The text summary is 304 lines for 20 runs (≈15/run).
  - Outputs are only stdout text and HTML (`cli.py:153-163`).
  - The headline shows dollars only with breakers.

### cb-strict-parse: CONFIRMED
- The strict loop raises on the first bad line (`trace.py:393-421`). exp4-5 re-run: exit 1, 0 stdout lines.
- Input is `nargs="+"` Path only, with no gzip, stdin, or glob (package grep).
- The monotonic-index check is at `:414-419`.

### cb-validation-ci: CORRECTED
- **Correction.** "No cases for … key order" is wrong. `tests/test_trace.py:117` (`test_dict_key_order_never_looks_like_a_prompt_change`) exists, but it asserts that key order is ignored, which is the opposite of the detection the report wants. Keyword grep false positives: "beta" in `test_instrument.py` is prompt text, not the beta namespace; "ttl" matches are 5m-TTL tests only.
- **Confirmed.**
  - No tests for images, 1h, concurrency, duplicate run_id, thinking/effort, Bedrock ids, lookback, compaction, `server_tool_use`, or threads.
  - Dev dependencies are pytest and ruff only. There is no hypothesis, coverage, or mypy/pyright, although `py.typed` exists.
  - CI runs `ubuntu-latest` on Python 3.10 and 3.13.
  - cp1252 handling is at `cli.py:230-244`.
  - The perf test has a 20 s budget and no memory assertion.

### cb-security-privacy: CORRECTED
- **Confirmed.**
  - `SECURITY.md` treats traces as secrets.
  - No redaction or hash mode (package grep).
  - Evidence excerpts are embedded, escaped in HTML.
  - The terminal prints `run_id` raw (`report.py:705`), and `_one_line` strips only `\r`/`\n` (`:122-124`).
  - Traces are opened with `path.open("a")` at default permissions.
  - The cache diagnostics fingerprints are hashes only.
- **Correction.** "No … signing" is inaccurate. `release.yml` publishes with `pypa/gh-action-pypi-publish@release/v1` via Trusted Publishing, which by default generates Sigstore-signed PEP 740 attestations. PyPI's integrity API returns HTTP 200 provenance for both `tokenbill-0.1.2-py3-none-any.whl` and `tokenbill-0.1.2.tar.gz`. No SBOM, RBAC, or audit features exist.

### cb-code-quality: CONFIRMED
All cited items were verified in code:
- `breakers.py:43-47` imports `_as_billed`/`_replay`.
- `cli.py:207-216` mutates `PRICING`.
- `pricing.py:92` has a global `_warned_models`.
- `report.py:66-73` defines `_DOLLAR_ALIASES`; `analyzer.py:84-94` defines `_segment_parts`.
- `SCHEMA`/`_USAGE_FIELDS` are duplicated in `instrument.py:49-56` and `trace.py:43, 247-252`.
- `SegmentShare` objects are unused by `report.py`.
- Evidence is built for every classified pair, and `setdefault` keeps only the first per kind.
- `_changed_window` is a per-char Python loop.
- Render caches are `id()`-keyed and use `weakref.finalize`.

Calling these "debt" is a judgment call.

### cb-non-cache-levers: CORRECTED
- **Confirmed.**
  - `SPEC.md:8-25` scopes the tool to caching and redundancy.
  - These live-guide figures match: batch 50%; prompt audit 14%; programmatic tool calling 24%; 16,384 cap ended 15% of Opus 5 attempts; two problems carried 43%.
  - Image ≈1.2k at 1280×720, per the Vision formula.
- **Corrections, from the live guide fetched 2026-09-23:**
  - Compaction saved **32%** on the long run, not 38%.
  - Effort escalation: **~93% at ~$0.45 vs 91.7% at $0.93**, not $0.70 vs $1.39. The ratio is the same.
  - The "~10K schema tokens" tool-search threshold appears only in the bundled skill. The live guide reports 45% savings with 500 tools and 20% with the GitHub MCP server.

### cb-realized-savings: CONFIRMED
- Outputs are snapshot-only.
- The skill's cost-optimization.md frames optimization as "cost per completed task", with "one lever per diff" and savings confirmed "in the usage and cost reports **after** cutover".
- The monitoring docs list `claude_code.commit.count` and `claude_code.pull_request.count`.
