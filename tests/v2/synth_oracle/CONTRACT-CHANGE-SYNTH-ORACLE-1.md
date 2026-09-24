# CONTRACT-CHANGE-SYNTH-ORACLE-1 — usage-replay readings that decide nano-level agreement

Raised by SYNTH-ORACLE (wave 2) under SPEC §21 #3. No field or signature changes are requested.
The merge-gate test `tests/v2/synth_oracle/test_differential_replay.py` compares REPLAY's
`UsageReplayer` with `synth.oracle.ReferenceReplay` to the nano. SPEC §9.2–§9.4 leaves the points
below open; REPLAY and SYNTH-ORACLE were built independently (SYNTH-ORACLE did not read REPLAY's
code), so any of them can surface as a gate failure. Each entry states the SPEC text, what the
oracle implements, and why. **Proposed ruling = the oracle's reading**, unless the arbiter decides
otherwise (PLAN §1.4: the SPEC text decides; ambiguities are ruled by the contract owner and
recorded in `tests/v2/kit/RULINGS.md`). A ruling that differs is a small, local change in
`tokenbill/synth/oracle.py`.

## Ranges

- **O-1 Ranges are scenarios.** Each lane is replayed three times: *point* (every rule as written),
  *low* (ambiguous transitions alive, low tokenizer factor, effort scale `s − 0.25`, batch hit band
  0.98) and *high* (ambiguous transitions expired, high factor, `s + 0.25`, hit band 0.30). A
  request's point is the point scenario's priced point; its bounds are the min / max over the three
  scenarios' priced bounds (so `low ≤ point ≤ high` always holds, and pricing ranges such as
  unknown-TTL writes compose with policy ranges). Ambiguity (`|gap − τπ| ≤ 10 s`, §9.2) applies to
  every rule that consults the `gap ≤ τπ` test: TTL miss→hit, TTL hit→miss, `fast_off` flips,
  compaction-call reads, the cold-resume trigger and `restore_caching`. The keepalive horizon
  (`n·κ + 300 s ≥ gap`) has no ±10 s band. Outcome `low_nano`/`high_nano` are None when a request
  has no range.
- **O-2 Unknown τ.** τπ = the matching TTL clause, else the transition's observed τ (§3.15, incl. the
  `write_ttl_hint`), else the TTL of the request's own writes, else 300 s (the 5m point of R5).
  Hit→miss compares τπ with the transition's τ (unknown → 300 s).

## Where tokens go

- **O-3 Write classes.** When a request's write *total* changes (a flip, a context transform, a
  repair), the writes go to: the TTL clause's bucket; the 5m bucket on keepalive lanes ("writes at
  the 5m rate"); else the request's own single observed write class (unknown-TTL writes stay
  `cache_write_unknown`, i.e. a range, R5); else the class of the observed τ (1h iff 3600 s, else
  5m). When the total is unchanged the observed buckets are kept (and re-rated on TTL lanes). The
  inserted compaction call's writes use the same class.
- **O-4 Passthrough under TTL.** §9.3.1 re-rates "every write token of the lane", but §9.2 and
  §9.3.7 (5) say passthrough inferences keep their observed pricing unless a *rate* transform
  applies. The oracle re-rates serving and inserted inferences only; passthrough inferences
  (declined attempts, compaction / advisor / other iterations, failed attempts) are repriced only
  by rate transforms (remap, fast/geo/regional, batch tier).
- **O-5 Stagger fan-out conserves tokens.** §9.3.6 writes `R' = min(W_j, shared)`, which drops an
  observed `R_j > 0`. The oracle uses `R' = R_j + min(W_j, shared)`, `W' = W_j − min(W_j, shared)`
  (identical when `R_j = 0`, the fan-out case), so `P'` stays `T' − U'`.

## Keepalive

- **O-6** Pings are sent for every transition of a keepalive lane with `gap > κ`, whether or not the
  gap ends warm (non-clairvoyant daemon, A.4's 2-hour example), priced at their send times
  `ts_{i−1} + k·κ` with the previous request's serving context (after rate transforms), and
  attached to request `i`'s outcome (`extra` and cost). Only TTL-expiry misses flip (to 5m writes);
  other requests keep their split and buckets (a 1h keepalive lane is not re-rated). A TTL clause
  that matches a keepalive lane is ignored (skip reason) because keepalive keeps the 5m TTL.
  Keepalive is Anthropic's mechanism (5m TTL from the request start, `max_tokens: 0` pings): lanes
  on other providers are skipped with a reason, like TTL policies (§9.3.1). (Fixed in review: the
  oracle used to ping OpenAI lanes, which only added cost.)

## Context transforms

- **O-7 `new_0 = T_0`.** §9.3.3's "(T_0 for i = 0)" read literally: a first request above the window
  is compacted into `S_c + T_0` tokens. (The alternative `new_0 = 0` gives `S_c`.)
- **O-8 S_c.** `post=` when given, else COMPACTION_SUMMARY_TOKENS_DEFAULT. The replay never derives
  the "org median" from the lanes it is given: that would make results depend on shard size
  (§9.1 #6). Callers pass the org median as `post=`.
- **O-9 Resets.** `removed` resets on §9.3.3's own rule (COMPACTION / CLEAR event in
  `(ts_{i−1}, ts_i]` or `T_i < 0.5·T_{i−1}`); alive_π uses §3.15 rule 1 (COMPACTION / CLEAR /
  CONTEXT_EDIT events, applied edits, dropped thinking). The compaction window is checked before
  cold resume and both share `removed`; `clear` restarts from the lane's first serving request's
  observed `T`. Inserted COMPACTION and OTHER calls count in `added_calls`; pings count only in
  `keepalive_pings`.

## Rate transforms

- **O-10 Tokenizer band.** Quantities are scaled per bucket and rounded half-even (incl.
  `output_upper`) on every inference of a matched lane; a band exists only between `claude-legacy`
  and `claude-4.7+` (other family pairs: no band). The first matching remap clause (policy order)
  wins; `model_raw` becomes the target id.
- **O-11 Effort.** Reduction `floor(th·(1 − s))` with `th = output_reasoning` or `floor(0.505·O)`;
  `output_reasoning` is reduced by the same amount when known; serving inference only; first
  matching clause in policy order.
- **O-12 `fast_off` / `geo_global` / `regional_to_global`** apply to every inference (scope becomes
  `global` on every channel; first-party pricing ignores it). `fast-toggle` misses flip only when
  alive_π holds with the fast toggle repaired. A policy made only of these three, with no flip
  and no range anywhere, returns EXACT cost and saving (§9.3.5 "the saving is EXACT"), although the
  `ReplayResult` comment says "ESTIMATED unless observed" — the two SPEC statements conflict.
- **O-13 Batch.** Every inference of an eligible request is priced at tier batch; on Bedrock all input
  becomes uncached; elsewhere `R'' = floor(h·R')` and the rest are added to the 5m writes (existing
  writes keep their bucket). "Managed Agents entrypoint" = an `entrypoint` containing `managed`
  (the SPEC names no value).

## Tail, repairs, calibration, result shape

- **O-14 Minimum-prefix gate** applies to the serving inference when its usage or pricing context
  differs from the ledger and it caches anything.
- **O-15 Repairs.** `restore_caching` uses only the repair's `gap ≤ τπ` test (with O-1 ambiguity) and
  sets `U' = 0`; `stagger_fanout` groups lane-first requests with `W ≥ 0.8·T` per (scope, observed
  model, cwd_key), anchored at the group's first member (≤ 10 s); `retry_backoff_cap` needs `E > 0`
  and keeps attempts 1, 2 and the final one; `fallback_credit` needs no alive_π (a provider credit);
  `shared_ci_prefix` chains consecutive run starts ≤ τπ (TTL clause, else the first request's write
  TTL, else 300 s) per (scope, model) with `S_ci` = the static floor, else `floor(0.8·min first W)`,
  and sets `R' = min(S_ci, T − U)`. Step-3 order: restore_caching; lane-first repairs; keepalive /
  TTL flips; `fast_off` flips; fallback_credit; TTL hit→miss; retry_backoff_cap.
- **O-16 Calibration.** CALIBRATED iff a passing report and (mode `calibrated`, or the report's
  `mode_used` equals the requested mode); the calibrated mixture is applied per flipped request and
  per bound; the gap band is read from the first integer of its label (lower bound, seconds); bands
  with < 30 trials use the pooled ρ; a report without bands gives ρ = 1.
- **O-17 Result shape.** Outcomes in lane-key order; `per_lane` omits lanes whose point is unpriced
  (`int` field); the observed policy returns an exact zero saving; with several matching TTL
  clauses the first in `Policy.ttl` order wins. `assumptions` and `lanes_skipped` reasons are free
  text (the gate compares neither).
- **O-18 Saving per request, then summed** (§3.5 `saving`, §9.1 #1 "model error on unaffected
  traffic cannot leak into them"). An unchanged request contributes exactly 0 to the saving
  (point and bounds); a changed request contributes `observed − policy` with its ranges crosswise
  (`low = observed low − policy high`). The saving range is therefore never wider than the
  aggregate crosswise range `baseline − cost`, and ranges of unaffected traffic (placeholder
  output, unknown-TTL writes, unknown endpoint scope) do not enter it. `cost` stays the sum of the
  per-request bounds. (Fixed in review; before, the oracle subtracted the aggregate ranges.)
- **O-19 `changed`.** `False` iff the serving usage and pricing context are unchanged, no call is
  inserted, batch does not apply, every kept passthrough inference is unchanged, no dropped
  attempt (`retry_backoff_cap`) carried a billable inference, and the priced `(point, low, high)`
  equals the ledger's. A context-only transform (e.g. `regional=global` turning `endpoint_scope`
  `unknown` into `global` on a first-party request) counts as a change even when the price is
  identical, which matters for O-18 only when that request carries a range.

- **O-20 Unpriced savings.** An unchanged request saves exactly 0 even when it is unpriced (same
  usage and context ⇒ same price), so the observed policy always returns an EXACT zero saving and a
  policy that leaves the unpriced traffic alone returns a priced saving. When the policy *changes*
  an unpriced request (e.g. `ttl=1h` re-rates a lane on an unknown model, or `model=` remaps a
  Bedrock lane to a model with no Bedrock row), the oracle's saving is unpriced (R2: unknown is not
  zero; `labels.sub`). **REPLAY at `4a4da46` differs:** it reports the saving of the requests
  priced on both sides and names the exclusion in the note. Needs a ruling; it only shows on
  unpriced inputs (the gate families are fully priced).
- **O-21 Batch on Bedrock with an unpriced target.** The oracle applies §9.3.5's Bedrock rule by
  channel (`bedrock` ⇒ all input uncached) whatever the model. REPLAY at `4a4da46` applies the
  hit band when the remapped target has no Bedrock row (the cost is unpriced either way; only the
  serving usage differs).

## Black-box status (review, 2026-09-23)

The gate was run locally (SPEC §21 #4: a scratch overlay, never committed; REPLAY's source was not
read) against `pkg/REPLAY` at `4a4da46`: with O-18 and O-19 all 12 families pass at the gate seed
(500 lanes each), and the stress seeds 1–16 (300 lanes per family) report no differing request.
Before the fix every family failed on the saving range only (every request outcome, baseline and
cost already agreed). A joint-policy fuzz (1,350 random combinations of 2–4 clauses over every
family, 40 lanes each) agrees except for O-20 and O-21.
