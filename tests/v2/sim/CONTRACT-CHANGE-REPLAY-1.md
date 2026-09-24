# CONTRACT-CHANGE-REPLAY-1 — replay and model-gate points for the contract owner

Raised by REPLAY (wave 2) under SPEC §21 #3. REPLAY implements against the current contract; no
field or signature was changed. Items 1 and 5 decide the merge-gate differential test
(`tests/v2/synth_oracle/test_differential_replay.py`); the others are documentation or additive
proposals.

REPLAY ran that differential locally against `pkg/SYNTH-ORACLE` at `fff302e` (a scratch export,
nothing committed): on the oracle's 12 lane families × their policies, seeds 20260923 / 1 / 7 / 99,
**every request outcome (points and bounds), serving usage, inserted call, per-lane total, count,
baseline and cost agree to the nano**. The only remaining difference is item 1 (the saving's
bounds). REPLAY adopted the oracle's documented readings O-1 … O-17 of
`CONTRACT-CHANGE-SYNTH-ORACLE-1.md` everywhere except item 1 and item 5 below.

## 1. `ReplayResult.saving` bounds: per request, unaffected requests contribute 0 (disagreement)

SPEC §3.5: `saving: baseline − cost, per request then summed (ESTIMATED; ranges crosswise)`;
§9.1 #1: "every other request keeps its billed usage exactly … Savings are always cost(observed) −
cost(policy), so model error on unaffected traffic cannot leak into them."

- **REPLAY:** for a *changed* request the saving is `base − cost` with crosswise bounds
  (`low = base.low − cost.high`); an **unchanged** request's saving is exactly 0 (its policy cost is
  its ledger figure, the same number). The saving is the sum.
- **Oracle (current):** `sub(baseline, cost)` of the totals, i.e. crosswise over *every* request, so
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
