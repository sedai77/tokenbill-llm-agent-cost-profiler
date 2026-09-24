# tests/v2/detect_cache — DETECT-CACHE acceptance tests

The eight content-free cache detectors (SPEC §10.1, the `cache.*` rows of §10.2, D11, D14, D21,
D26, D28) at the registry paths of §3.7:

| registry id | class | kinds |
|---|---|---|
| `cache.miss-by-cause` | `detect.cache_miss.MissByCause` | ttl-expiry, model-switch, param-change, compaction, tools-changed, system-changed, messages-changed, context-shrank, unexplained |
| `cache.switch-churn` | `detect.cache_miss.SwitchChurn` | refusal-fallback-no-credit, availability-ping-pong, plan-toggle, user-model-switch, fast-toggle, effort-change |
| `cache.rebuild` | `detect.cache_miss.RebuildEvents` | compaction-cold, edit-churn |
| `cache.cold-resume` | `detect.cache_ttl.ColdResume` | cold-resume |
| `cache.ttl-advisor` | `detect.cache_ttl.TtlAdvisor` | ttl-1h-recommended, ttl-5m-recommended, keepalive-recommended, ttl-heterogeneous |
| `cache.gateway-disabled` | `detect.cache_structure.GatewayDisabled` | no-cache, beta-header-dropped, tool-search-disabled |
| `cache.unread-write` | `detect.cache_structure.UnreadWrite` | write-never-read, oversized-ttl, tail-writes (info) |
| `cache.cold-fanout` | `detect.cache_structure.ColdFanout` | cold-fanout |

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/detect_cache` (also on 3.10).
Coverage: `uv run --python 3.12 --extra dev coverage run -m pytest tests/v2/detect_cache && uv run
--python 3.12 --extra dev coverage report --include='tokenbill/detect/cache_*.py'` — 99% at
hand-off (the lines left are defensive branches).

| file | covers |
|---|---|
| `test_miss_by_cause.py` | one hand-computed fixture per cause to the nano (billed rewrite `mw·w_billed + mu·u` EXACT, triage premium ESTIMATED upper bound, none for compaction); cause precedence (compaction + idle gap → compaction, switch + gap → model-switch); Appendix A.9 thresholds; the D28 effort exemption (Claude Code Opus 5.5: not a cause; SDK lane: `effort-change`); lever filtering per cohort; allowance labeling; `min_usd` gate; unknown-TTL writes as a range; unpriced model; server diagnostics as `validated_against`; self view |
| `test_switch_churn.py` | every sub-kind to the nano incl. plan-toggle (opusplan) and ping-pong; fallback-credit and `fast=off` replays; credited fallbacks skipped; effort changes only where they invalidate (with/without the per-message effort beta); the `fastModePerSessionOptIn` patch |
| `test_rebuild_and_cold_resume.py` | Appendix A.10 edit churn ($0.31 EXACT, $0.2576 ESTIMATED, K* 37.2, `min_usd` 0.10), paying-back and cold edits, CONTEXT_EDIT events and `K_rem` up to the next reset, capability gating; compaction-cold (5m and 1h lanes); Appendix A.5 cold resume (one event, $4.00 EXACT, $3.90 ESTIMATED), allowance cohort, thresholds, main lanes only |
| `test_ttl_advisor.py` | with `FakeReplayer`: A.1 → `ttl-1h-recommended` $1.1508, A.2b → `ttl-5m-recommended` $0.318 (min_usd 0.10), A.2 → none; never the observed TTL; 2%-of-spend floor; keepalive for an SDK lane with 7-minute gaps (A.4) and never for Claude Code (nor for Claude Code lane kinds, R-E11); `ttl-heterogeneous` with counts only when < 60% of ≥ k principals benefit; gateway note; allowance saving LIST_EQUIVALENT; cohorts by (team, lane kind, billing path); gap histogram, 1-in-20 rule and agreement, keepalive break-even (A.12: 96 / 196 min) |
| `test_structure.py` | gateway no-cache (a) with the `restore_caching` replay, beta-header-dropped (b) with `policy.ttl.<team>`, tool-search-disabled (c) via `attribution.extra["gateway"]`; write-never-read (block lever only with fingerprints), oversized-ttl EXACT arithmetic, tail-writes; cold fan-out range and upper bound |
| `test_conformance.py` | `assert_detector_conforms` (incl. shard invariance) for all eight classes on a mixed six-team fleet; team-by-team shards through `core.shards.merge_findings`; registry paths and ordering; exactly one `missing-capabilities` finding per unmet `requires` via `run_detectors`; healthy control clean; CANARY absent from every finding; no floats; `core.kanon.rescope_findings` publication; self view |
| `test_edges.py` | range-priced contexts (Bedrock unknown endpoint scope), other-TTL (OpenAI 30m) writes, failing pricers, unpriced models, refusing and mislabeling replayers, compaction events before any request or without a TTL, K* without unit rates, heterogeneity without principals, mixed Claude Code/SDK cohorts |
| `test_properties.py` | hypothesis: random lane sets (teams, kinds, billing classes, products, models incl. an unpriced one, gaps around both TTLs, partial reads, uncached input, fast toggles, edits, reset events) — every detector conforms, never raises, never emits unpriced or negative figures |
| `test_review_fixes.py` | adversarial-review regressions: evidence kinds from the SPEC value set; table-driven billing classes (an additive class such as Copilot's `pool` gets its own scope, list-equivalent basis); every §10.1 threshold override below (and invalid values → `UsageError`); keepalive never replayed on Claude Code lanes of a mixed API_RUN cohort; the D26 statement kept at the end of every allowance summary, also after `core.kanon` re-scoping; team names never cut in titles or summaries; an unpriced repair replay keeps the finding (recoverable "unpriced", gated on cost); unpriced events disclosed; one unpriced lane does not erase a cohort's TTL advice; `K_rem` in one backward pass equals its definition (hypothesis) |
| `test_gate_replay.py` | **gate** (`importorskip("tokenbill.sim.usage_replay")`): the real `UsageReplayer` on A.1 ($1.1508), A.2b ($0.318), A.2 (none), A.4 (keepalive $2.10 − $0.6924), the restore-caching, stagger-fanout and fallback-credit repairs, and an unpriced lane beside A.1 (advice kept, lane disclosed) |
| `test_gate_cc.py` | **gate** (`importorskip("tokenbill.adapters.claude_code")`): CC's checked-in transcript fixtures through the real `ClaudeCodeAdapter` and `MemoryStore` into every cache detector — `assert_detector_conforms` (shard invariance included), `run_detectors`, canary absent, no floats (the replay-free part of the F-KIT gate-1 smoke path) |
| `test_gate_fleet.py` | **gate** (`importorskip` `tokenbill.synth.fleet` and the replayer): every cache plant of `synth.fleet.generate()` (payments 1h, platform no-cache, mobile cold resumes allowance + overage, agents keepalive + edit churn) recovered within its `FleetTruth` tolerance in its exact scope; the core control team produces no cache finding; k-anonymous publication; canary absent |

The gate tests were run on a scratch copy of this branch overlaid with the modules of
`pkg/REPLAY` (8206a5d), `pkg/SYNTH-FLEET` (732301e), `pkg/SYNTH-ORACLE` (e3e574a) and `pkg/CC`
(767f21e) under `.tbscratch/` (never committed): all 15 green, every plant recovered to the nano.
On the scale fleet of `synth.fleet.generate(scale_requests=…)` the eight detectors (real replays
included) take 5.3 s for 100k requests and 15.6 s for 300k (linear; ≈ 52 s per 10⁶ requests,
single process, 0.5 GB peak RSS at 300k).

## Fixtures and provenance

No fixture files. Every lane is synthetic and built in code with `core.builders` in `helpers.py`
(the SPEC Appendix A lanes A.1, A.2, A.2b, A.5, A.10, a healthy control team and a mixed six-team
fleet), dated 2026-09-23 (Opus 5.5 is priced from 2026-09-22). No real transcripts, pages or keys.
The canary test plants `CANARY` in every free-text-capable record field (agent type, entrypoint,
project, cost center, skill, extra values, output format).

## Facts

This package transcribes no fact. Prices come from `core.testing.FakePricer` (`core/facts.json`);
thresholds and constants from `core.evidence` (`COLD_RESUME_MIN_CONTEXT`, `KEEPALIVE_INTERVAL_S`,
`KEEPALIVE_MAX_IDLE_S`, `TTL_RULE_GAP_SHARE_5_60MIN`, `keepalive_break_even_s`) and
`core.transitions` (the D21 miss rule); settings keys, minimum versions and documentation URLs from
`core.catalog.ALLOWLIST`. Unverified items inherited from facts.json: the `hooks.SessionStart`
value shape and resume-input fields are `verified` in facts.json but marked **VERIFY** in SPEC
§19.5; the command path `hooks/tokenbill_session_start.py` is PLAN's template name (§11.3).

## Thresholds (SPEC §10.1: every default is overridable)

`min_usd` (default `"1.00"`) and, as `ctx.thresholds["<detector id>.<name>"]` (decimal strings;
shares in [0, 1], counts non-negative, else `UsageError`):

| key | default | meaning |
|---|---|---|
| `cache.cold-resume.min_context` | 100000 | `P_{i−1}` floor (`COLD_RESUME_MIN_CONTEXT`) |
| `cache.cold-resume.min_write_share` | 0.5 | `W_i ≥ share·P_{i−1}` |
| `cache.ttl-advisor.spend_share` | 0.02 | saving ≥ max(`min_usd`, share × cohort spend) |
| `cache.ttl-advisor.heterogeneity_share` | 0.60 | `ttl-heterogeneous` below this share of ≥ k principals |
| `cache.gateway-disabled.min_requests` | 5 | no-cache lane length |
| `cache.gateway-disabled.min_prompt_tokens` | 4096 | median `T ≥ max(min_cacheable, value)` |
| `cache.gateway-disabled.min_5m_requests` | 20 | beta-header-dropped: requests writing 5m only |
| `cache.gateway-disabled.tool_search_misses_per_100` | 3 | tool-search-disabled rate |
| `cache.unread-write.read_share` | 0.95 | write-never-read: `R_j < share·(R_i + W_i)` |
| `cache.cold-fanout.window_s` | 10 | lane-first requests starting within the window |
| `cache.cold-fanout.min_write_tokens` | 1024 | each member's `W` |
| `cache.cold-fanout.max_read_share` | 0.5 | each member's `R < share·T` |

`policy.ttl.<team>` (`"1h"`, `"3600"` or `"3600s"`) configures beta-header-dropped (read raw, see
`CONTRACT-CHANGE-DETECT-CACHE-1.md` §2).

## Interpretations (where the SPEC is silent)

- **Cohorts and scopes.** Findings aggregate per `core.findings.cohort_key` cohort at (team, lane
  kind[, `billing_class`]); the billing class is in the scope unless it is the default `billed`,
  and the list-equivalent classes are a table (`allowance`, D26; Copilot's `pool`, R-E20), so an
  additive class never shares a finding id with the billed cohort. The TTL advisor splits a cohort by billing path (§10.2) and
  adds a `billing_path` scope dim only when a billed cohort holds more than one path (so the
  common case keeps the plant scope `(team, lane_kind)`). Cold fan-out groups by (cache scope,
  model, cwd key) inside a cohort.
- **`min_usd`.** Miss-by-cause kinds and the triage kinds of the gateway detector (`beta-header-dropped`,
  `tool-search-disabled`) and of unread writes gate on `cost_observed`; every other finding gates
  on its recoverable point (or on `cost_observed` when it has no recoverable, or when its
  replayed recoverable is unpriced — shown as "unpriced", never zero). A finding whose gated
  figure is unpriced is not emitted; unpriced events are counted in `n_events` and evidence,
  never priced as zero, and the summary says how many were left out of the dollars.
- **Exactness.** A priced line is exact exactly when the pricer prices it exactly (R9): unknown-TTL
  writes and unknown endpoint scopes become ESTIMATED ranges, including in `cost_observed`.
  Rewrite tokens are allocated to the billed write buckets 1h → 5m → other → unknown (longer TTLs
  sit first in the prompt, §19.3).
- **Levers.** `lever_ids` are `core.catalog.levers_for_kind(kind)` filtered to levers whose selector
  (or a grid selector) matches an affected lane; `fallback.credit` is linked to `model-switch` only
  for refusal fallbacks and `cc.fast_mode_opt_in` to `param-change` only for fast toggles.
- **TTL advisor.** The observed TTL of a cohort comes from its billed write tokens (unknown-TTL
  writes by their hint). Candidates are the TTLs different from the observed one (both for
  `mixed`/`unknown`) plus keepalive for `api_run` cohorts with a non-Claude-Code lane (§10.2 and
  R-E11). The replayed policies are `ttl=<τ>@lane_kind:<kind>` and
  `keepalive=240s,max=3600s@lane_kind:<kind>` on the cohort's lanes only — keepalive on its
  SDK/API (non-Claude-Code) lanes only, and every replay on the lanes whose inferences are all
  priceable (an unpriced lane would make the whole baseline unpriced; the lanes left out are
  disclosed in the summary). `ttl-heterogeneous`
  replaces the TTL recommendation when fewer than 60% of at least `k` principals are individually
  cheaper (per-lane savings from the observed and policy `per_lane` points), carrying the same
  recoverable and patch with per-cohort delivery text. No replayer, no advice.
- **Unread writes.** `write-never-read` counts `min(W_i, R_i + W_i − R_j)` unread tokens only when
  the next transition is a hit or an unexplained miss (other causes are priced elsewhere);
  `oversized-ttl` is lane-level (1h writes and no gap in (300 s, 3,600 s]); `tail-writes` are
  single-request lanes that write (the trigger/fix pairing of the §10.2 row).
- **Rebuilds.** Compaction-cold uses the TTL of the last billed write before the compaction and the
  event's `pre_tokens` (else the context size); `cost_observed` is ESTIMATED (an estimated line).
  Edit churn takes `X` from `applied_edits`, else from the CONTEXT_EDIT events of the transition;
  `K_rem` stops at the next reset event or edited request; an unknown TTL defaults to 5m.
- **Capabilities.** `requires` is one frozenset, so `cache.rebuild` declares
  `{usage_sequence, timing}` and gates its kinds internally (`events` for compaction-cold,
  `events` or `attempts` for edit churn) — see `CONTRACT-CHANGE-DETECT-CACHE-1.md`.
- **Generated text.** Summaries stay within 330 chars so `core.kanon`'s re-scope prefix fits in
  400 without cutting the D26 statement, which (with any unpriced-events note) is kept whole at
  the end; team names are never cut in half (kanon scrubs whole tokens): a text too long drops the
  team from its label (the scope names it) and is otherwise cut at a word boundary. Evidence items
  use the SPEC kinds only (per-lane items are `aggregate`).
- **Self view.** With `ctx.self_principal` the findings are `self` audience and only lanes of that
  principal (or without a principal) are analyzed.
