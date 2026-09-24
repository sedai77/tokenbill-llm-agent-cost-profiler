# CONTRACT-CHANGE-REPLAY-1 — replay and model-gate points for the contract owner

Raised by REPLAY (wave 2) under SPEC §21 #3. REPLAY implements against the current contract; no
field or signature was changed. Items 1 and 5 decide the merge-gate differential test
(`tests/v2/synth_oracle/test_differential_replay.py`); the others are documentation or additive
proposals.

REPLAY ran that differential locally against `pkg/SYNTH-ORACLE` (scratch exports, nothing
committed). At `fff302e` everything but the saving's bounds (item 1) agreed. At **`619a8b6`** (the
oracle adopted item 1 in `42e0f15`, and made an unpriced *changed* request's saving unpriced,
item 7) the merge-gate test passes: on the oracle's 12 lane families × their policies, seeds
20260923 / 1–7 / 99, **every request outcome (points and bounds), serving usage, inserted call,
per-lane total, count, baseline, cost and saving agree to the nano**. REPLAY follows the oracle's
documented readings O-1 … O-17 of `CONTRACT-CHANGE-SYNTH-ORACLE-1.md` everywhere except item 5
below (and item 8, calibrated mode, which the gate does not compare).

## 1. `ReplayResult.saving` bounds: per request, unaffected requests contribute 0 (resolved)

**Resolved:** SYNTH-ORACLE adopted this reading in `42e0f15`; kept here for the record.

SPEC §3.5: `saving: baseline − cost, per request then summed (ESTIMATED; ranges crosswise)`;
§9.1 #1: "every other request keeps its billed usage exactly … Savings are always cost(observed) −
cost(policy), so model error on unaffected traffic cannot leak into them."

- **REPLAY:** for a *changed* request the saving is `base − cost` with crosswise bounds
  (`low = base.low − cost.high`); an **unchanged** request's saving is exactly 0 (its policy cost is
  its ledger figure, the same number). The saving is the sum.
- **Oracle (before `42e0f15`):** `sub(baseline, cost)` of the totals, i.e. crosswise over *every* request, so
  an unchanged request with a priced range (unknown-TTL writes, placeholder output, uncertain
  billing, unknown endpoint scope) adds `[low − high, high − low]` to the saving.

Why REPLAY's reading: "per request then summed" only differs from `baseline − cost` in the bounds,
and only through unchanged requests — the phrase exists to make them contribute nothing. With the
aggregate reading, a lever touching 1% of an OTel-sourced fleet (every call has unknown-TTL writes)
gets saving bounds as wide as the whole fleet's TTL uncertainty, e.g. "$500 [−$30k, +$31k]", which
makes PLAN's realization intervals meaningless. Shard additivity holds under both readings.
**Proposed ruling:** REPLAY's reading; the oracle change is local (sum per-request crosswise savings
over changed requests only). Pinned by `test_readings.py::test_unchanged_requests_add_nothing_…`.

## 2. `CalibrationPartial.confusion` carries a provider-qualified reason

§9.6 #7 asks for precision/recall "per provider when both are present", but a partial's confusion
entry is `(predicted cause, canonical server reason, n)` and partials must merge by addition for
any batching. REPLAY's partials therefore store the reason as `"<provider>:<reason>"`
(`anthropic:model_changed`, from `CacheDiagnostic.source`); `finish_calibration` strips it for
`CalibrationReport.diag_confusion` and names P/R classes `"<provider>:<class>"` only when both
providers occur. **Proposed additive change:** a 4-tuple `(cause, reason, provider, n)` in
`CalibrationPartial.confusion`, or ratify the encoding.

## 3. `compact-window` without `post=`: the org median belongs to the caller

§9.3.3: "`S_c` = the org median `post_tokens` of COMPACTION events, else the default". One replay
call sees one shard, so a median over its lanes would make sharded and unsharded replays differ
(§9.1 #6). REPLAY (like the oracle, O-8) uses `post=` when given, else
COMPACTION_SUMMARY_TOKENS_DEFAULT (flagged in `assumptions`). **Proposed:** PLAN / the pipeline
computes the org median once (e.g. from `LedgerStore` events) and passes `compact-window=W,post=M`;
document this in §9.3.3.

## 4. Rate-only policies are EXACT (§9.3.5) — amend the `ReplayResult.cost` comment

§9.3.5 says `fast_off` / `geo_global` / `regional_to_global` savings are EXACT rate arithmetic;
§3.5's comment says the cost is "ESTIMATED unless the policy is observed". REPLAY (and the oracle,
O-12) return EXACT cost and saving for a policy made only of those three when no flip happened and
no line is a range; anything else is ESTIMATED with its calibration label. **Proposed:** amend the
§3.5 comment accordingly.

## 5. Cross-lane repairs stay inside the replay cohort (differs from the oracle's O-15 keys)

`stagger_fanout` groups by (scope, model, cwd_key) and `shared_ci_prefix` by (scope, model) in
§9.3.6. REPLAY adds the cohort keys (team, lane kind) (billing class is one per call), because D30
requires sharded and unsharded replays to be identical and shards are teams (split by lane kind
above 250k requests). The oracle's random lanes never put one group across teams or lane kinds,
so the differential agrees; a fleet with a fan-out group spanning two teams would differ.
**Proposed ruling:** the cohort-confined groups (a one-line key change in the oracle).

## 6. Gap-band labels are contract strings defined by REPLAY

`CalibrationReport.rho` rows are keyed by band labels `"0s-60s"`, `"60s-300s"`, `"300s-3600s"`,
`"3600s+"` (`sim.usage_replay.GAP_BANDS`); `sim.usage_replay.rho_from_report` derives ρ per band
(pooled below 30 trials, 1 when empty). Consumers other than REPLAY parse the label's first
integer (the oracle does). **Proposed additive change:** move `GAP_BANDS` to `core` next to
`CalibrationReport` in the next contract window.

## 7. An unpriced *changed* request makes the saving unpriced (R2; aligned with the oracle)

SPEC R2 ("unknown is not zero") and `core.labels.add` (None if either operand is None): the saving
of a request the policy changes is unknown when its observed or its policy cost is unpriced (an
unknown model, or a remap target without a rate row on the lane's channel), so the per-request sum
is unpriced (`nano=None`, note `unpriced: …`, ESTIMATED, with the replay's calibration label).
An *unchanged* request saves exactly 0 even when it is unpriced. REPLAY's hand-off version dropped
unpriced changed requests from the sum and kept a number with an "excludes …" note; this review
changed it to the R2 reading, which is also SYNTH-ORACLE's (`619a8b6`). Shard merges stay equal to
one replay (`add` propagates None). Consumers (PLAN's Shapley values, the CLI) must treat an unpriced
saving as unknown, and PLAN should scope remap levers with selectors that have rate rows.
**Proposed:** document it in the `ReplayResult.saving` comment of §3.5.

## 8. Calibrated mode: lane-first repairs are ρ-weighted (differs from the oracle; not gated)

§9.4 lists repairs among the flips to a hit that calibrated mode weights by ρ. REPLAY weights
`stagger_fanout` and `shared_ci_prefix` too, with the gap band of the offset from the fan-out
group's first member and of the gap to the previous CI run respectively (`restore_caching`,
`fallback_credit` and `retry_backoff_cap` use the transition's gap, as the oracle does). The oracle
applies ρ only to transitions with a gap (`i ≥ 1`), so on lane-first repairs it keeps the
documented hit. The merge-gate differential runs in documented mode only, so this does not decide
the gate. **Proposed ruling:** REPLAY's reading (the SPEC names repairs without exception).

## 9. The model gate cannot report how many transitions it could not price (additive proposal)

A transition whose billed or predicted serving usage is unpriced (no rate row) is left out of the
cost comparison and of the ρ counts (§9.6 #2 prices both sides; there is nothing to compare), but
`CalibrationPartial` / `CalibrationReport` have no field to count them, and a count must merge by
addition for any batching, so it cannot live in the free-text `notes` of a partial. REPLAY skips
them (documented in the README). **Proposed additive change:** `unpriced: int = 0` on both
dataclasses (summed by `merge_partials`, echoed in `notes` when non-zero).
