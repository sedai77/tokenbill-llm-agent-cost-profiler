### BLOCK — Block-level replay and cache-breaker suite (wave 2)

**Goal.** For fingerprinted API traffic (recorder trace@2, trace@1 via adapter): a documented-rules block
cache model (tier salts with the conditional effort exemption, 4 breakpoints, 20-position lookback with run
collapsing, first-token visibility, workspace/org scope) and the 11 block breakers with priced repairs,
agreeing with the v0.1 engine on its demo (SPEC §9.7, §10.4, D28).

**Owns.** `tokenbill/sim/block_replay.py`, `tokenbill/detect/block.py`, `tests/v2/blocksim/**`,
`tests/v2/fixtures/blocksim/**`.

**Consumes.** `core.records` (`BlockRef`, `ContentFingerprint`, `Breakpoint`, `Request`, `Lane`),
`core.cache_rules` (incl. `effort_change_keeps_cache`), `core.findings`, `core.types`, `core.protocols`
(`Replayer`, `Detector`, `Pricer`), `core.labels`, `core.builders` (`make_block`), `core.testing`
(`FakePricer`, `assert_replayer_conforms`, `assert_detector_conforms`).

**Provides.** `sim.block_replay.BlockReplayer` (`Replayer` for `breakpoint_policy` and `block:` repairs;
lanes without fingerprints skipped with a reason), `BlockReplayer.predict(lanes, *, pricer, rules,
placement="observed") -> list[ReplayRequestOutcome]`, `first_divergence(prev, cur) -> tuple[str, int, str] |
None` (tier, block index, cause); `detect.block.BlockBreakers` (registry `block.breakers`, `requires =
{"blocks"}`, kinds §10.4).

**Semantics.** Minimal change: `replay(policy)` returns, per lane, billed cost − (model(observed placement) −
model(policy)), so `replay(Policy.observed())` equals the billed ledger exactly and model error cancels;
breaker `recoverable` = model(observed) − model(repair), floored at 0 with the v0.1 wording, None for kinds
without a mechanical repair. Assumed end-of-messages breakpoints (from trace@1 counts) count as observed
markers; `missing-breakpoint` fires only with no markers at all. Billed-vs-predicted disagreement without a
hash divergence is an agreement metric, never a breaker.

**Fixtures.** Build `ContentFingerprint`s in tests with `core.builders.make_block` and a small test-local
hashing helper that follows SPEC §5.8 (hashes exclude `cache_control`). Re-encode the codebase experiments
deterministically: exp1 (8-turn loop, marker moved to the newest block each turn, billed usage of a working
cache), exp2b-a3 (six independent requests sharing a ~20k-char preamble), exp2b-a2 (five byte-identical
requests at one timestamp), exp9-1 (tools alternating between two key orders, billed cold writes), exp6-2 (25
text blocks appended per turn, billed full rewrites).

**Acceptance tests.**
- tier salts: an effort change invalidates only the messages tier for an SDK lane without the per-message
  beta, and nothing for a Claude Code Opus 5.5 lane (SPEC Appendix A.13 rows); a tool definition change
  invalidates all tiers; a speed toggle invalidates system + messages;
- lookback: an entry 21 collapsed positions back is not found; 25 consecutive `tool_result` blocks count as
  one position; TTL refresh on read; entries written at the same timestamp are not visible to siblings;
  breakpoint prefixes below the model minimum write nothing; block tokens rescaled to billed `total_input`;
- exp1 → no history-rewrite, read agreement ≥ 0.95; exp2b-a3 → `breakpoint-placement` saving ≈ 56% of input $
  (static_plus_end); exp2b-a2 → five predicted writes, no phantom 65% saving, a `fanout` finding with an
  upper-bound saving; exp9-1 → `serialization-churn`; exp6-2 → `lookback-overflow` with the every-15 fix;
- one fixture per breaker kind (11) with hand-computed recoverable; negative recoverable floored with the
  v0.1 wording; an assumed end breakpoint suppresses `missing-breakpoint`;
- `assert_replayer_conforms(BlockReplayer())`, `assert_detector_conforms(BlockBreakers())` (incl. shard
  invariance);
- perf: a 4,000-call run ≤ 2 s;
- gate `test_gate_dual_engine.py` (`importorskip("tokenbill.adapters.trace_v1")`): the four v0.1 demo
  scenarios read through `TraceV1Adapter` (fingerprint tier) → `timestamp`: exactly `volatile-system` at call
  1; `tool-churn`: exactly `tool-churn` (order) at the rotation calls, no `volatile-system`, no
  `missing-breakpoint`; `no-cache`: exactly `missing-breakpoint`; `well-behaved`: none, with
  `predict(placement="end")` reads equal to v1 optimal-cache reads within rounding; and the effort-exemption
  agreement with `core.transitions` on the same lanes.

**Size.** ~2.6k LOC including tests.
