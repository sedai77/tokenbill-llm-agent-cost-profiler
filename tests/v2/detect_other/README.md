# tests/v2/detect_other — DETECT-OTHER acceptance tests

The ten non-cache usage-level detectors (SPEC §10.1, the non-cache rows of §10.2, D14, D26, D34)
at the registry paths of §3.7:

| registry id | class | kinds |
|---|---|---|
| `context.size-tax` | `detect.context.SizeTax` | context-tax (info) |
| `context.compaction-window` | `detect.context.CompactionWindow` | compaction-window |
| `context.static-prefix` | `detect.context.StaticPrefix` | static-prefix (info), tool-defs-bloat |
| `attrib.carry` | `detect.context.Carry` | tool-output-carry, config-tax |
| `premium.modifiers` | `detect.premium.PremiumModifiers` | fast-premium, geo-premium, regional-premium, tier-premium |
| `premium.sticky-escalation` | `detect.premium.StickyEscalation` | sticky-escalation (self), sticky-escalation-count (org) |
| `model.routing` | `detect.model.Routing` | delegation-routing, same-tier-upgrade, default-model, default-effort, effort-mix (info), rebaseline (info) |
| `failure.path` | `detect.failure.FailurePath` | cold-retry, retry-storm, never-succeeding-400, tool-error-loop, max-tokens-truncation |
| `automation` | `detect.automation.Automation` | ci-cross-run, scheduled-cadence, batch-eligible, ci-run-cost (info) |
| `tail.runaway` | `detect.tail.Runaway` | runaway-session, idle-loop |

Detectors with per-kind capability needs also declare `missing-capabilities` (see
"Capabilities" below). The shared machinery (prices, money, cohorts, finding assembly, thresholds,
levers, replays) lives in the first half of `tokenbill/detect/context.py` and is internal to this
package (D29: no import of DETECT-CACHE's helpers).

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/detect_other` (also on 3.10).
Coverage: `uv run --python 3.12 --extra dev coverage run -m pytest tests/v2/detect_other && uv run
--python 3.12 --extra dev coverage report --include='tokenbill/detect/context.py,tokenbill/detect/premium.py,tokenbill/detect/model.py,tokenbill/detect/failure.py,tokenbill/detect/automation.py,tokenbill/detect/tail.py'`
— 96% at hand-off (every module ≥ 94%; the lines left are defensive branches).

| file | covers |
|---|---|
| `test_context.py` | size-tax exact decomposition to the nano for X ∈ {100k, 200k, 400k} (p50/p90, shares of calls and $), threshold override, the allowance cohort (LIST_EQUIVALENT, "Allowance headroom:", the D26 statement), unknown-TTL writes as a range; the compaction-window curve with the extra-compactions guard via a counting `FakeReplayer` (300k excluded at 4 per session, 500k chosen; threshold overrides move the choice; `post=` from the threshold); no replayer / small contexts / non-1M models / subagents → none; static-prefix harness cost with `S` known (lane-first and miss writes) and absent; tool-defs-bloat range on a 14k-token non-deferred tools tier ([0.50, 0.85] × the billed read/write mix) and none when a tool is deferred or the rescaled tier is ≤ 10k; carry with the published 2.5 bytes/token, a reset, a 31-sample cpt fit (3.00), config-tax first listing only, capability gating and notes |
| `test_premium.py` | SPEC §6.9 case 4 − case 1 = fast premium $0.068 EXACT (`min_usd` 0.01) with the `fastModePerSessionOptIn` patch; US-geo ($0.0068) and Bedrock regional ($0.50) premiums; a residency policy flags instead of recovering; no premium on standard traffic, the unpriced priority tier or an unknown model; allowance basis; range lines → ESTIMATED; sticky escalation: fast on 6 days (self, exact premium) vs 5 days (not sticky), effort xhigh on 7 days (self, escalated spend), `defaults.effort` override and validation, the org count only at ≥ k (never who), main lanes and principals only |
| `test_model.py` | delegation on top-tier subagents (replayed saving, upper bound, price-only, Explore on Haiku 4.5 with its 2026-10-15 retirement), workflow agents, none without a replayer; same-tier Opus 5 → Opus 5.5 = $0.042 exact arithmetic labeled ESTIMATED ("behavior unvalidated"), Fable 5 → 5.1, **none for Opus 4.8** (no successor) or Opus 5.5, unpriced successor on Bedrock; default-model and default-effort on Claude Code main lanes only (share thresholds, SDK lanes excluded, `params` gating); effort mix and thinking share; rebaseline on a synthetic Opus 4.8 → Opus 5 migration (+300 output tokens per call, +30%, stale-prompt flag, thinking-default note, Δ$ 1.05), a tokenizer-family note (Sonnet 4.6 → Sonnet 5) with a negative Δ$, no move / too few requests |
| `test_failure.py` | Appendix A.11 truncation ($0.71376 EXACT, $0.53532 ESTIMATED upper bound, `min_usd` 0.10), the ≥ 3 rule and the retry window; cold retry by attempts (with the `retry_backoff_cap` replay, or the triage premium without a replayer) and by API_ERROR events; retry storms from SDK attempts recorded by the recorder's hooks (a billing range), two retry layers, `x-stainless-retry-count`, `attempts` gating; never-succeeding 400s (same error twice, retry after `should_retry=false`, spend cap, error-event runs broken by a success); tool-error loops (≥ 3 consecutive) |
| `test_automation.py` | ci-cross-run chains within the TTL (later runs' first-call rewrites, the `shared_ci_prefix` replay, version drift), ci-run-cost per (repo, workflow) with p50/p90, the $15–25 benchmark with its source and CI vs interactive $; scheduled cadence (keepalive beats `ttl=1h` for an SDK agent; Claude Code only gets the TTL replay) and its exclusions (irregular, short, human prompts, < 4 requests); batch eligibility (§9.3.5 predicate) |
| `test_tail.py` | runaway session among 121 sessions (limit max($50, 5 × p99) with nearest-rank p99 = $0.03, cost above p95 = $59.97 EXACT), team-only scope without break-glass (no session or lane key anywhere in the finding), one finding per session with `ctx.break_glass`, limits and overrides, lane-kind and billing-class scopes, idle loops (human prompts needed, broken by a mid-run prompt), unpriced requests disclosed |
| `test_conformance.py` | `assert_detector_conforms` (incl. shard invariance) for all ten classes on a mixed nine-team fleet planting every kind, also in self and break-glass views; team shards through `core.shards.merge_findings`; registry paths; exactly one `missing-capabilities` finding per unmet `requires` via `run_detectors` and the per-kind notes of the lane-free call; the healthy control clean; CANARY absent (planted in every free-text-capable attribution field); no float in code (AST) or output; `core.kanon.rescope_findings` publication; ordering; invalid thresholds → `UsageError` |
| `test_properties.py` | hypothesis: random lane sets (teams, kinds, billing classes, products, workloads, models incl. an unpriced one, contexts to 250k appends, efforts incl. an unknown level, fast, US geo, truncations, tool errors, events, multi-attempt retries) — every detector conforms and never raises; without a replayer no replay-based kind appears; threshold fuzzing (random strings, huge and tiny exponents) raises only `UsageError` |
| `test_gate_replay.py` | **gate** (`importorskip("tokenbill.sim.usage_replay")`): the real `UsageReplayer` on hand-computed lanes — delegation Opus 5 → Sonnet 5 saves $0.11628 exactly, the same-tier arithmetic equals the replay to the nano, default effort halves thinking ($0.016), batch halves single calls ($0.045), a scheduled SDK cadence prefers keepalive ($0.88), Appendix A.6's negative 400k window (−$0.569) is never recommended |
| `test_gate_fleet.py` | **gate** (`importorskip` `tokenbill.synth.fleet` and the replayer): every DETECT-OTHER plant of `synth.fleet.generate()` — search (size tax to the nano; the 400k compaction point), infra (fast premium to the nano; self findings for the three sticky devs, the org count suppressed), data (delegation, three same-tier upgrades to the nano, default model, default effort, rebaseline Δ output per request to the digit, change date and models), ops (regional premium to the nano, runaway team-only), ci-bots (truncation cost to the nano and recoverable, cross-run, batch, run cost) and agents (tool-defs-bloat range contains the truth) — recovered in its exact scope; the core control team clean; k-anonymous publication with store-style user counts; canary absent |

The gate tests run against the modules merged into `v0.2` at d91a5e2 (`sim.usage_replay`,
`synth.fleet`); all green. On the synthetic fleet (26,104 requests) the ten detectors take ≈ 3 s
including every replay.

## Fixtures and provenance

No fixture files. Every lane is synthetic and built in code with `core.builders` in `helpers.py`
(the SPEC Appendix A lanes A.6 and A.11, the §6.9 case 1 usage, hand-computed lanes per kind, a
mixed nine-team fleet and a healthy control), dated from 2026-09-23 (Opus 5.5 is priced from
2026-09-22). No real transcripts, pages or keys. The canary test plants `CANARY` in agent type,
entrypoint, project, cost center, skill, extra values and output format.

## Facts

This package transcribes no priced fact. Prices come from `core.testing.FakePricer`
(`core/facts.json`); constants from `core.evidence` (`TOOL_DEFS_DEFER_THRESHOLD_TOKENS`,
`TOOL_SEARCH_REDUCTION_BAND`, `CC_AUTOCOMPACT_DEFAULT_TOKENS`, `STALE_PROMPT_OUTPUT_DELTA`,
`TOKENIZER_BAND`, `MAX_TOKENS_AGENTIC_RECOMMENDED(_XHIGH)`, `CODE_REVIEW_USD_PER_REVIEW`, the CPT
defaults through `core.findings.fit_cpt`); successors and retirement floors from `core.catalog`;
settings keys, minimum versions and documentation URLs from `core.catalog.ALLOWLIST`; lever grids
(`cc.default_model`, `cc.default_effort`, `cc.subagent_model`) from `core.catalog.LEVERS`.
Two small tables restate SPEC text: `detect.model.THINKING_ON_BY_DEFAULT = {"claude-opus-5"}`
(facts.json Opus 5 row note "thinking on by default"; SPEC §10.2 rebaseline fix) and the default
org effort `medium` for sticky escalation (SPEC §19.1: Opus 5.5 "default effort medium"; override
with `defaults.effort`).

Unverified items inherited from facts.json / SPEC §19: `CODE_REVIEW_USD_PER_REVIEW` ($15–25),
`TOOL_SEARCH_REDUCTION_BAND` and `TOOL_DEFS_DEFER_THRESHOLD_TOKENS` carry `verification:
research`; the managed-settings keys `model` and `effortLevel` are marked **VERIFY** in SPEC §19.5
but `verified: true` in facts.json (the patches use them; PLAN emits unverified keys as comments).

## Thresholds (SPEC §10.1: every default is overridable)

`min_usd` (default `"1.00"`) and, as `ctx.thresholds["<detector id>.<name>"]` (decimal strings;
shares in [0, 1], counts non-negative, magnitudes ≤ 2**53, else `UsageError`):

| key | default | meaning |
|---|---|---|
| `context.size-tax.threshold_tokens` | 200000 | the `X` of `cost_observed` |
| `context.compaction-window.min_window` | 300000 | smallest recommendable window |
| `context.compaction-window.max_extra_compactions` | 3 | extra compactions per session |
| `context.compaction-window.post_tokens` | — | the org median `S_c`, passed as `post=` (R-E24) |
| `context.static-prefix.tool_defs_threshold` | 10000 | rescaled non-deferred tool tokens |
| `premium.sticky-escalation.min_days` | 5 | sticky when on more days than this |
| `model.routing.default_model_share` | 0.5 | top-tier share of CC main spend |
| `model.routing.default_effort_share` | 0.5 | share of CC main output at effort ≥ high |
| `model.routing.min_requests` | 50 | rebaseline requests on each side |
| `model.routing.move_share` | 0.5 | share that moves to the new model |
| `failure.path.storm_attempts` | 3 | a storm has more attempts |
| `failure.path.min_truncations` | 3 | truncated attempts per cohort |
| `failure.path.retry_window_s` | 120 | retry / continuation window |
| `failure.path.retry_share` | 0.95 | follower `T ≥ share·T_trunc` |
| `failure.path.min_loop` | 3 | consecutive tool-error requests |
| `failure.path.cold_write_share` | 0.5 | cold retry wrote `≥ share·E` |
| `automation.first_write_share` / `first_read_share` | 0.8 / 0.1 | CI first-call rewrite |
| `automation.min_scheduled_requests` / `cadence_cv` | 4 / 0.25 | scheduled cadence |
| `tail.runaway.min_hourly_usd` / `p99_multiple` | 50 / 5 | runaway limit |
| `tail.runaway.idle_min_requests` / `idle_min_s` | 50 / 3600 | idle loop |

Policy values read raw (not decimals): `defaults.effort` (a level, default `medium`) and
`policy.residency_required[.<team>]` (`1`/`true`) — see `CONTRACT-CHANGE-DETECT-OTHER.md` §5.

## Interpretations (where the SPEC is silent)

- **Cohorts and scopes.** Findings aggregate per `core.findings.cohort_key` cohort at (team, lane
  kind[, `billing_class` unless `billed`]); `same-tier-upgrade` adds `model`; `static-prefix` adds
  `model` and `cache_scope`; tail findings drop `lane_kind` for main lanes and add `session` only
  under break-glass (`CONTRACT-CHANGE-DETECT-OTHER.md` §3). List-equivalent billing classes are a
  table (`allowance`, Copilot's `pool`).
- **Exactness.** A priced line is exact exactly when the pricer prices it exactly (R9); a bucket
  takes the integer unit-rate path only after two probes (alone and beside 1M input tokens, so a
  long-context band cannot hide) proved `price_usage` agrees. Premiums and the same-tier saving
  are rate arithmetic on identical tokens (the same-tier figure is labeled ESTIMATED, with the
  replay's min-prefix gate on the successor). Unpriceable events are counted and disclosed, never
  priced as zero; an unpriced successor gives an "unpriced" recoverable.
- **`min_usd`.** Info and triage kinds (size tax, static prefix, carry, sticky escalation, effort
  mix, rebaseline, retry storm, never-succeeding 400, tool-error loop, ci-run-cost, tail) gate on
  `cost_observed`; the others on their recoverable point (or `cost_observed` when it is unpriced
  or absent). The rebaseline Δ$ is signed and gates on its absolute value.
- **Compaction window.** Eligible windows need `w ≥ min_window`, ≤ `max_extra_compactions`
  (replay `added_calls` ÷ sessions) and a positive saving; the argmax of the saving × the
  trajectory RR p50 (a constant, so the argmax of the saving; ties → larger window) is
  recommended; `cost_observed` is the replay baseline of the qualifying lanes.
- **Rebaseline.** The change day is the first UTC day on which a model serves ≥ 50% of the
  cohort's requests that day and over the next 7 days, having served < 50% of the 14 days before,
  whose plurality model differs; the comparison windows are the 14 UTC days before and after that
  midnight. "Δ$ per request at current rates" prices every request with the run's rate card.
- **Effort.** An effort-capable model is one whose requests report an effort (`params.effort`);
  default effort uses the output-weighted share of requests at effort ≥ high. Sticky escalation
  uses `params.session_effort` and "more than 5 distinct UTC days" for fast mode and effort
  alike; a fast-sticky principal's figure is the exact fast premium, an effort-sticky one's the
  spend of the escalated requests.
- **Failure paths.** Cold retry: a multi-attempt request whose first attempt failed and whose
  success started more than `τ` (the transition's TTL, else 300 s) after it, or an API_ERROR event
  between two requests more than `τ` apart, when the success wrote ≥ half of `E`. Retry storms
  count recorded attempts and `sdk_retry_count + 1`, and the layers of `retry_layer`. Truncation
  followers are the next attempt with a serving inference in lane order.
- **Automation.** CI chains are consecutive qualifying runs of one (cache scope, model) starting
  within the previous run's write TTL; `cost_observed` counts the later runs of each chain. A CI
  run is a session; repo and workflow appear as their `h_` pseudonyms or an opaque stable id.
- **Carry.** An item costs `t·w` at the request's billed write bucket plus `t·r` per later request
  of the lane before the next reset (COMPACTION / CLEAR / CONTEXT_EDIT event or an edited request);
  a context injection lands on the first request at or after it.
- **Tail.** Sessions are the lanes of one session key inside the cohort; rolling-hour spend uses
  request start times; p95 / p99 are nearest rank inside the cohort (shard-invariant).
- **Capabilities.** `requires` is one frozenset, so per-kind needs are gated internally and
  reported by the lane-free call (`CONTRACT-CHANGE-DETECT-OTHER.md` §4).
- **Generated text.** Summaries stay within 330 chars and keep the D26 statement whole at the end
  (so `core.kanon`'s re-scope prefix fits); team names are never cut; no free-text attribution
  value (agent type, entrypoint, project, skill, extra values) is ever echoed.
