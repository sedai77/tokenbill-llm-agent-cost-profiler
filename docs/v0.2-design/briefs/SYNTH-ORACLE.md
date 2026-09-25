### SYNTH-ORACLE — Reference replay oracle, synthetic lanes, rollout panels (wave 2)

**Goal.** Independent ground truth for the replay engine and the verification estimators: a deliberately
simple reference implementation of the usage-replay semantics, random and closed-form lane generators, and
panel/campaign generators with known effects (SPEC §9.8, Appendix A, §13). Do not read or copy REPLAY code:
independence is the point.

**Owns.** `tokenbill/synth/{oracle,lanes_gen}.py`, `tests/v2/synth_oracle/**`.

**Consumes.** `core.records`, `core.types` (`Policy`, `ReplayResult`, `ReplayRequestOutcome`, `PanelRow`),
`core.transitions`, `core.cache_rules`, `core.policy`, `core.labels`, `core.money`, `core.evidence`,
`core.protocols`, `core.builders`, `core.testing` (`FakePricer`, `assert_replayer_conforms`).

**Provides.** `synth.oracle.ReferenceReplay` (`Replayer`; Decimal path through `Pricer.price_usage` only;
readable per-request code for TTL two-way flips, keepalive, compaction window, cold resume, model remap,
effort (selector-scoped), fast_off, geo_global, batch, restore_caching, stagger_fanout);
`synth.lanes_gen.random_lanes(seed, n, *, family) -> list[Lane]` (families: ttl, keepalive, compaction,
cold_resume, remap, effort, rates, batch, repairs, placeholder, unknown_ttl), `closed_form(name) ->
tuple[list[Lane], dict[str, int]]` (Appendix A fixtures incl. A.2b, A.10, A.11 with expected nano),
`rollout_panel(*, clusters, weeks, true_effect, waves, holdback, seed, org_wide=False) -> list[PanelRow]`,
`ab_campaign(*, tasks, trials, cost_effect, token_effect, turn_effect, seed) -> tuple[list[Request],
list[Request], list[dict]]`.

**Build.** Oracle per SPEC §9.2–§9.4 written independently from the SPEC (one function per policy family,
no integer hot path, no sharing with REPLAY); `baseline`/`cost`/`saving` Figures with points and bounds.
Generators seeded via `common.rng`; random lanes include every inference shape the ledger can hold (placeholder
output, unknown-TTL writes, refusal iterations, allowance lanes in a separate family).

**Acceptance tests.**
- the oracle reproduces Appendix A.1–A.6, A.2b and the replay-related parts of A.10/A.11 to the nano and
  satisfies `assert_replayer_conforms`;
- `random_lanes` deterministic per seed; `closed_form` expectations match the oracle;
- `rollout_panel` with a 25% effect is deterministic and has the requested waves and holdback; `org_wide=True`
  yields a single-cluster series with a level shift for the ITS tests; `ab_campaign` reproduces the RTK-like
  shape (tokens −38%, turns +14%, cost +7%);
- gate `test_differential_replay.py` (`importorskip("tokenbill.sim.usage_replay")`): the fast
  `UsageReplayer` equals `ReferenceReplay` to the nano (points and bounds) on ≥ 500 random lanes per policy
  family (documented mode), reporting the first differing request on failure.

**Size.** ~2.3k LOC including tests.
