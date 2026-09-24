# tests/v2/sim — REPLAY acceptance tests

Usage-level replay engine and the model gate (wave 2; SPEC §9.1–§9.6, D7–D9, D26, D28, D30).
Owned modules: `tokenbill/sim/usage_replay.py` (`UsageReplayer`, the `Replayer` for every source,
content-free) and `tokenbill/sim/calibrate.py` (`calibrate`, `calibrate_lanes`, `calibrate_pass1`,
`calibrate_pass2`, `finish_calibration`, plus `merge_partials` and `fit_rho_by_fold`).

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/sim` (also on 3.10); full-size budgets:
`… pytest -q -m perf tests/v2/sim/test_perf.py`.
Coverage: `uv run --python 3.12 --extra dev coverage run -m pytest tests/v2/sim && uv run --python
3.12 --extra dev coverage report --include='tokenbill/sim/usage_replay.py,tokenbill/sim/calibrate.py'`
(100% at hand-off).

| file | covers |
|---|---|
| `helpers.py` | area-local builders: timestamps in seconds after 2026-09-23 12:00 UTC, `table` (`lane_from_table`), the Appendix A lanes, `replay` with `FakePricer` + `RulesTable`, `priced_request` (the ledger: Σ `price_inference` figures), and `random_lane(s)` — every inference shape the ledger holds (5m / 1h / mixed / unknown-TTL writes with and without hint, placeholder output, uncertain billing, partial streams, reconstructed usage, refusal fallbacks, compaction and advisor iterations, retries, output-residual-only requests, reset events, fast / US geo / Bedrock scopes / batch tier, efforts, diagnostics, an unpriced model) |
| `test_appendix_a.py` | A.1 ($1.1508), A.2 (+$0.318), A.2b ($0.318), A.3 (+$1.1508, with and without a static floor), A.4 ($0.6924, 4 pings for 20 min, 15 capped pings and cold for 2 h, Claude Code lane skipped, streaming SDK lane not skipped, structured-output / forced tool_choice / thinking-enabled / batch lanes skipped, selector scoping), A.5 (cost_observed $4.00, premium $3.90, cold resume `compact` with exactly one event, `clear`), A.6 ($1.999, windows at and above the lane maximum change nothing, MAIN / 1m-context guards, default `S_c`), passthrough-only requests |
| `test_identity.py` | `assert_replayer_conforms` with `FakePricer` and `FlatRates`; the observed policy is the identity (points and bounds, every outcome unchanged, equal to the ledger) on 200 random lanes; evidence follows the lines; unpriced lanes; minimal change under a scoped TTL; saving = Σ per-request savings; determinism and input-order independence; mixed billing classes → `UsageError`; allowance lanes on `list_equivalent`; empty input; unknown modes and malformed policies → `UsageError`; default rules; block-level policies skipped with a reason |
| `test_rate_transforms.py` | model remap: legacy→4.7+ band [1.00, 1.35], 4.7+→legacy band, same-tier successor and same-family remaps without a band, target minimum gate, clause order; selector-scoped effort (0.505 prior, known reasoning, levels at/below the cap, unknown levels, `s ± 0.25` bounds); `fast=off` flips of fast-toggle misses and plain repricing; `geo=global`; `regional=global` incl. an unknown scope range; batch hit band point/low/high, every predicate exclusion, Bedrock without caching, unsupported model |
| `test_repairs.py` | `restore_caching` (hand-computed lane, policy TTL, eligibility), `stagger_fanout` (group minimum, 10 s anchor, warm members, cohort confinement), `retry_backoff_cap` (warm final attempt, attempts beyond three dropped with their ranges, short backoffs), `fallback_credit` (credited and not), `shared_ci_prefix` (chains, `floor(0.8·min W)`, static floor, non-CI lanes) |
| `test_ranges_and_modes.py` | ±10 s ambiguity (305 s under `ttl=5m` spans hit and miss, 295 s, 3,605 s under `ttl=1h`, unchanged TTL has no range), bounds contain points on random lanes for every family; calibrated mode (ρ-weighted flips, pooled small bands, empty report, UNCALIBRATED without a passing report, documented-mode labels), gap bands; sharding: team shards and arbitrary halves merged with `core.shards.merge_replay` equal one replay for every policy family |
| `test_pricing_paths.py` | the integer hot path equals a Decimal-only pricer to the nano for every policy family; probe rejection of wrong unit rates; failing probes fall back; the gpt-5.6-sol long-context band found by binary search (limit 272,001) and priced through `price_usage` (§6.9 case 11: $2.43); a contract multiplier needing scale > 9; FlatRates; per-day keys; non-billable inferences; web fetch |
| `test_readings.py` | each interpretation listed below, pinned by a hand-computed case |
| `test_calibrate.py` | documented-rule lanes → NMBE 0, CV(RMSE) 0, `pass`; 10% of predicted hits flipped (seeded) → ρ ≈ 0.90 ± 0.03, documented fails, calibrated passes; a cache outliving the documented TTL fails; 11 periods → `insufficient_data`; month granularity; zero billed cost; predictions never read the transition's own reads; one batch == five batches; out-of-fold ρ; pass 2 blending; Wilson intervals; TTL corroboration 128/128 not scored; `model_changed` vs a predicted model switch; OpenAI `param_changed` vs a predicted effort change in the param class; per-provider P/R; `unavailable` as no-comparison; expected rebuilds and `key_changed` unscored; rule-5 labels are never compared with themselves; validation; no floats |
| `test_properties.py` | hypothesis: random lanes × random policy combinations (documented and calibrated) keep every invariant (determinism, no floats, bounds contain points, unchanged outcomes equal the ledger, saving = Σ changed, cost = Σ outcomes, per-lane totals, ping counts); fuzzed malformed `Policy` fields raise only `TokenbillError` |
| `test_edges.py` | probe rejections of inconsistent pricer lines, raising unit rates, unpriced calibrated flips, context-edit resets, un-repaired speed toggles, empty lanes, CI chains under a TTL clause, batch with a calibrated flip, a CI member already reading more than `S_ci`, OpenAI 30-minute writes in replay and in the gate (τ's bucket), passthrough placeholder bounds under the band, gate corner cases |
| `test_hygiene.py` | AST lint: no float in either module; the content canary planted in every free-text field never reaches a `ReplayResult` or a `CalibrationReport`; byte-identical replay and calibration JSON across two processes with different `PYTHONHASHSEED` |
| `test_perf.py` | PR variants (1/10 size, CPU time, best of runs; budgets skipped under coverage or a tracer): 10⁵ requests × `ttl=1h` ≤ 3 s, 2× requests ≤ 2.3× time, calibrate 10⁵ ≤ 12 s; `perf` marker: 10⁶ × 1 policy ≤ 30 s, calibrate 10⁶ ≤ 120 s (both pass: ≈ 15 s and ≈ 25 s CPU) |
| `test_gate_ratecard.py` | **gate** (`importorskip("tokenbill.rates.engine")`): conformance, Appendix A and the hot-path/Decimal agreement with the real `RateCard`; calibrate with it |

Measured on the build machine (Python 3.12, FakePricer): replay ≈ 14 µs/request for `ttl=1h`
(10⁶ requests ≈ 15 s CPU), calibrate ≈ 25 µs/request for both passes (10⁶ ≈ 25 s); about 40% of the
replay is `core.transitions.classify_transitions`.

## Fixtures and provenance

No fixture files. Every record is synthetic, built in the tests with `core.builders` (and
`helpers.py`); usage objects follow the canonical records of SPEC §3.2 / field names of §19.4. No
real transcripts or provider pages. The Appendix A fixtures and their dollar amounts are transcribed
from the SPEC and hand-checked; other expectations are hand-computed in the tests (Opus 5.5: $4 /
$20, read $0.20, 5m $5, 1h $8 per MTok; Sonnet 5 and 4.6, Opus 5 / 4.8, Fable 5, Haiku 4.5,
gpt-5.6-sol and Bedrock rows from `core/facts.json` through `FakePricer`).

## Readings of open SPEC points

The engine follows the readings SYNTH-ORACLE documented as O-1 … O-17
(`tests/v2/synth_oracle/CONTRACT-CHANGE-SYNTH-ORACLE-1.md` on its branch) — a local run of the
differential gate agrees to the nano on every outcome — except the two items raised in
`CONTRACT-CHANGE-REPLAY-1.md` (saving bounds per request; cohort-confined cross-lane repairs). In
short:

- **Ranges** are three deterministic passes (point / low / high: ambiguous transitions alive or
  expired, tokenizer factor, effort `s ± 0.25`, batch hit band 0.98 / 0.30) combined with the
  pricer's own line ranges; request bounds are min/max over the passes. The ±10 s band applies to
  every `gap ≤ τπ` test (TTL flips both ways, `fast_off` flips, compaction-call reads, the
  cold-resume trigger, `restore_caching`), not to the keepalive horizon.
- **τπ** = the TTL clause, else the transition's observed τ (incl. the write hint), else the TTL of
  the request's own writes, else 300 s; hit→miss compares τπ with the observed τ (unknown → 300 s).
  The first TTL / remap / effort clause matching a lane (policy order) applies.
- **Write classes:** unchanged write totals keep their buckets (re-rated on TTL lanes); a changed
  total goes to the TTL clause's bucket, else 5m on keepalive lanes, else the request's own write
  class, else the observed τ's class (1h iff 3600 s, else 5m). Passthrough inferences (declined
  attempts, iterations, failed attempts) are repriced only by rate transforms.
- **Keepalive:** pings for every transition with `gap > κ` at the previous request's context, priced
  at their send times, attached to request `i`; a TTL clause is ignored on keepalive lanes.
- **Context transforms:** `new_0 = T_0`; `S_c` = `post=` else the default (item 3 of the
  contract-change note); `removed` resets on COMPACTION / CLEAR or `T_i < 0.5·T_{i−1}`.
- **Rate transforms:** tokenizer band only between `claude-legacy` and `claude-4.7+` (half-even per
  bucket, incl. `output_upper`); effort reduction `floor(th·(1 − s))` on the serving output (the
  placeholder upper bound is kept); rate-only policies without flips or ranges are EXACT; the batch
  predicate is evaluated after `fast=off`; "Managed Agents" = an entrypoint containing `managed`.
- **Tail and repairs:** the min-prefix gate applies to any changed serving inference that caches;
  step-3 order restore_caching → lane-first repairs → TTL / keepalive flips → `fast_off` flips →
  fallback_credit → TTL hit→miss → retry_backoff_cap; `stagger_fanout` conserves tokens (`R + min(W,
  shared)`); `shared_ci_prefix` chains a run to the previous one when it starts within its own τπ,
  `S_ci` = floor else `floor(0.8·min W)` over the group, never lowering reads; `retry_backoff_cap`
  needs `E > 0` and keeps attempts 0, 1 and the final one.
- **Result shape:** outcomes and `per_lane` in lane-key order; `per_lane` omits lanes with an
  unpriced point; baseline / cost are unpriced when any request is; the saving excludes unpriced
  changed requests (note and assumption); assumptions and notes carry no per-call counts, so shard
  replays merge to the same text.
- **Model gate:** only the serving inference is compared (passthrough inferences are identical on
  both sides); predicted reads are clamped to `[0, T − U]`; ρ counts use non-ambiguous predicted
  hits; the confusion matrix covers every labeled transition (`predicted_hit` may be None), with
  Token Bill's cause computed without the label (rule 5 undone); `unlabeled` counts transitions
  without a label; fold = day ordinal mod `folds` (`seed` is accepted and unused).

## Facts

No new fact was transcribed. Used from `core.evidence` / `core/facts.json`: TOKENIZER_BAND (1.00,
1.35), BATCH_CACHE_HIT_BAND (0.30, 0.98), THINKING_SHARE_PRIOR (0.505),
COMPACTION_SUMMARY_TOKENS_DEFAULT (20,283), FEMP_MONTHLY (5, 15), FEMP_HOURLY_DAILY (10, 30),
MIN_CALIBRATION_PERIODS (12), the miss rule and keepalive defaults (through `core.transitions` /
`core.policy`), the FakePricer rate rows and modifiers. From the SPEC text itself: the batch point
hit band 0.64, the effort range `s ± 0.25`, the ±10 s ambiguity, the gap bands, Wilson 95%
(z = 1.96), the 30-trial pooling threshold, the 10 s fan-out window and the 0.8 / 0.5 thresholds.

### Unverified

- The Managed Agents entrypoint value (no source names one): any `entrypoint` containing
  `managed` is excluded from the batch predicate.
- Tokenizer bands between families other than `claude-legacy` / `claude-4.7+` are not documented:
  no band is applied (price-only remap).
