# CONTRACT-CHANGE-DETECT-OTHER — compaction summary size, the compaction-window plant, tail scopes, per-kind capability notes

Raised by DETECT-OTHER (wave 2) under SPEC §21 #3. Nothing here blocks the package: the code
implements the current contract (SPEC + Appendix E) with the interpretations below. The contract
owner decides whether to fold them into Appendix E.

## 1. No `AnalysisContext` field carries the org median compaction summary size `S_c`

R-E24: "`compact-window` without `post=` uses `COMPACTION_SUMMARY_TOKENS_DEFAULT`; the pipeline
(WIRING/CLI-SAVINGS/PLAN) must pass `post=<org median>` computed once per run". PLAN builds its
own policies, but `context.compaction-window` builds its grid inside the detector, and
`AnalysisContext` (§3.5) has no field for the org median; computing it from the lanes of one call
would break shard invariance (§9.1 #6).

Implemented: the detector appends `post=<n>` when `ctx.thresholds["context.compaction-window.
post_tokens"]` is set (a decimal string, validated like every threshold) and otherwise lets the
replayer use the default (20,283, flagged in the replay's assumptions).

Proposed (additive): the pipeline sets that threshold key from the org median `post_tokens` of
COMPACTION events (`LedgerStore` query, once per run) — or a dedicated
`AnalysisContext.compaction_summary_tokens: int | None = None`.

## 2. The search compaction-window plant is a curve point, not the recommendation

SPEC §18 says "`context.compaction-window` 400k point = truth (±5%)", while §10.2 recommends "the
best RR-adjusted p50 with w ≥ `min_compaction_window` (300k)" and at most 3 extra compactions per
session. On the synthetic fleet (seed 7) the best eligible window of the search team is **500k**
(saving $54.15, 1 extra compaction per session) while the plant's `recoverable_nano` is the
**400k** point ($46.12, 2 per session). Every trajectory grid value shares one realization prior,
so the RR adjustment cannot change the argmax.

Implemented: the finding's `recoverable` is the recommended (best eligible) window; the full
curve is in the evidence (`compaction-window:<w>k` items with `saving_nano`, `added_compactions`,
`per_session`, `eligible`, `recommended`). `tests/v2/detect_other/test_gate_fleet.py` checks the
400k curve point against the plant (±5%) — which it matches to the nano — and that the
recommendation saves at least as much.

Proposed: SYNTH-FLEET / INTEGRATION check `PlantTruth("search.compaction-window")` against the
evidence item `compaction-window:400k` (`saving_nano`), not `Finding.recoverable` (or the plant
records `"window": "recommended"` with the best eligible window's truth).

## 3. Tail findings: "team only" vs the §10.1 aggregation `(team, lane_kind, kind)`

§10.2 says tail findings name the team only; §10.1 aggregates findings at (team, lane kind,
kind); SYNTH-FLEET's runaway plant has scope `(team=ops)`. Shard invariance (§3.21) forbids two
cohorts of one team emitting the same finding id.

Implemented: `tail.runaway` scopes are `team` plus `lane_kind` only when it is not `main`, plus
`billing_class` when not `billed` (so `(team=ops)` for the main-lane plant, unique ids per
cohort), plus `session` only under `ctx.break_glass` (one finding per session). Without
break-glass the evidence refs are ordinals (`tail:runaway-session:1`), never session or lane keys.

## 4. Per-kind capability notes (R-E32)

R-E32 defers per-kind requirements and asks detectors to "emit their own `dq.*` note when a kind
cannot run". A detector can only return findings, and a per-shard note would duplicate finding
ids across shards (`merge_findings` raises).

Implemented: each DETECT-OTHER detector with a per-kind capability need declares
`kind_requires` (e.g. `tool-defs-bloat` → `blocks`, `config-tax` → `events`, `default-effort` /
`effort-mix` → `params`, `retry-storm` → `attempts`, `tool-error-loop` → `appended`, `idle-loop`
→ `human_prompts`) and gates the kind on `ctx.capabilities`; called with **no lanes** — the
pipeline's single `run_detectors([], ctx, emit_missing=True)` call — it returns one data-quality
finding of kind `missing-capabilities` per kind whose capability is missing (scope
`kind=<kind>`, unpriced cost, no recoverable). Per-shard calls never emit them.

Proposed: fold this pattern (or `Detector.requires_by_kind`, see CONTRACT-CHANGE-DETECT-CACHE-1
§1) into Appendix E so PLAN/OUT expect these notes.

## 5. Policy values in `ctx.thresholds`

Like DETECT-CACHE's `policy.ttl.<team>` (CONTRACT-CHANGE-DETECT-CACHE-1 §2), two §10.2 inputs are
not decimals and are read raw: `defaults.effort` (an effort level, default `medium`; an unknown
level raises `UsageError`) and `policy.residency_required[.<team>]` (`1`/`true`/`yes`/`on`).
