# tests/v2/synth_oracle — SYNTH-ORACLE acceptance tests

Reference replay oracle, synthetic lanes, closed forms, rollout panels and A/B campaigns (wave 2;
SPEC §9.2–§9.4, §9.8, Appendix A, §13). Owned modules: `tokenbill/synth/oracle.py`,
`tokenbill/synth/lanes_gen.py`. The oracle was written from the SPEC alone (REPLAY's code was not
read); its readings of open SPEC points are listed in `CONTRACT-CHANGE-SYNTH-ORACLE-1.md` (O-1 …
O-21) for the arbiter of the differential gate.

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/synth_oracle` (also on 3.10).
Coverage: `uv run --python 3.12 --extra dev coverage run -m pytest tests/v2/synth_oracle && uv run
--python 3.12 --extra dev coverage report --include='tokenbill/synth/oracle.py,tokenbill/synth/lanes_gen.py'`
(≈ 99% at hand-off).

| file | covers |
|---|---|
| `helpers.py` | area-local builders (`lane`, `req`, timestamps in seconds from 2026-09-23 00:00 UTC), `replay` with `FakePricer` + `RulesTable`, and `first_difference` — the differential comparator (points and bounds of every outcome and of baseline / cost / saving, priced serving usage, inserted calls summed per kind, per-lane totals, counts; names the first differing request) |
| `test_oracle_appendix.py` | Appendix A.1–A.6, A.2b to the nano via `closed_form` (A.4 incl. the 20-minute / 2-hour / Claude Code variants, A.5 compact and clear, A.6 below and above the window), the replay-related parts of A.10 (rewrite premium, benefit, net loss from the oracle's outcome usage) and A.11 (per-attempt, truncated and recoverable costs from the oracle's outcomes); `assert_replayer_conforms` with `FakePricer` and `FlatRates` |
| `test_oracle_ttl_keepalive.py` | TTL hit→miss with the ±10 s ambiguity range (305 / 310 / 295 / 311 s), static-prefix floor, hits beyond the observed TTL, miss→hit blocked by an effort change and by a model switch, non-Anthropic skip, serving / unknown-TTL re-rating (passthrough untouched), hint as observed τ, selector scoping; keepalive max-idle cap, no ping under κ, ping usage, every skip reason, allowed streaming / auto tool choice / adaptive thinking, TTL clause ignored on keepalive lanes |
| `test_oracle_transforms.py` | compaction window cold call, `removed` chain and both resets, 1m_context / lane-kind guards, S_c from `post=` or the default, first-request literal reading; cold resume `clear` and guards; remap without band (successor), legacy→4.7+ and 4.7+→legacy bands, target minimum gate, selector; effort cap with the 0.505 prior and with known reasoning, levels at or below the cap, scale 1; `fast_off` exact saving and fast-toggle flips; `geo_global`; `regional_to_global` incl. unknown scope; batch hit band, every exclusion of the predicate, `fast_off` making a request eligible, Bedrock uncached |
| `test_oracle_repairs_misc.py` | `restore_caching` (warm rebuild, gap and ambiguity, policy TTL, predicate), `stagger_fanout` (group minimum, 10 s anchor, other cwd, warm first request, lanes without a serving request), `retry_backoff_cap` (warm final attempt, dropped attempts with ranges), `fallback_credit`, `shared_ci_prefix` (chained starts, floor); calibrated mode (mixture, pooled bands, empty bands, labels); mixed billing classes, unknown mode, allowance basis, unpriced usage, passthrough-only requests, placeholder ranges, block-level fields, counts, determinism, helpers, client-upgrade / directory-change blocks, non-billable serving inference |
| `test_lanes_gen.py` | `random_lanes` deterministic per seed for every family, fully priced, one billing class per family, well-formed for `classify_transitions`; together the families hold every inference kind (except counterfactual KEEPALIVE), every usage source (except PROVIDER_ROLLUP), billable True / False / None, events, multi-attempt and > 3-attempt requests, effort, subscription, unknown-TTL writes with and without hint, web search, reasoning, placeholder upper bounds, fast, US geo, Bedrock, OpenAI, 1h writes; ambiguous gaps; the repairs family's no-cache lanes, fan-out groups and CI runs each move their repair; `family_policies`; every closed form valid and deterministic |
| `test_rollout_ab.py` | stepped-wedge panel: deterministic per seed, 4 holdback clusters, 4 waves of 4 starting at weeks 2/4/6/8, monotone treatment, true ATT ≈ −25% of the untreated level and a holdback DiD near 0.75; `org_wide`: one `org` series with a level shift at the middle day; `price_change` only moves `cost_actual_nano`; named clusters, count holdback, no holdback, errors. A/B campaign: RTK-like shape (tokens −38%, turns +14%, cost +7%) campaign-wide **and in every task**, outcomes with randomized arm order, tags, determinism, errors |
| `test_differential_replay.py` | **gate** (`@pytest.mark.gate`, `slow`, `importorskip("tokenbill.sim.usage_replay")`): `UsageReplayer` == `ReferenceReplay` on 500 lanes per family under each family policy; harness self-tests (the oracle agrees with itself; every kind of planted difference is reported; inserted calls compare by billed tokens, not object count) |
| `test_properties.py` | hypothesis: identity on random lanes of every family, `low ≤ point ≤ high`, the saving per request then summed (unchanged requests save exactly 0, changed ones crosswise), Σ outcomes = Σ per-lane = cost, determinism, shard additivity through `core.shards.merge_replay`, `round_half_even` against Decimal, the ping formula, panel shapes; **fuzz** of the generators' free-form inputs (effects, holdback, base level, noise, family and closed-form names: only `TokenbillError` subclasses escape; huge or tiny decimals are refused before any arithmetic) |
| `test_no_float_canary.py` | AST lint: no float in the oracle, none in the generators' money functions, no `float(` anywhere; the canary planted in every free-text attribution field never reaches a replay result; generated data is content-free; no network |

Runtime: the area runs in ≈ 5 s; the gate adds ≈ 20 s of oracle work plus the fast engine's.

Review cross-checks (2026-09-23, scratch overlays per SPEC §21 #4, never committed; REPLAY's source
not read): the gate passes against `pkg/REPLAY` at `4a4da46` for every family (and on stress seeds
1–16); SYNTH-FLEET's independent plant closed forms (`pkg/SYNTH-FLEET`
`test_gate_truth_vs_oracle.py`) agree with the oracle on all 12 replay-based plants. Remaining
REPLAY differences on joint policies over unpriced inputs are O-20 / O-21 in the contract-change
note.

## Lane families (`lanes_gen.FAMILIES`) and their policies (`family_policies`)

`ttl`, `keepalive`, `compaction`, `cold_resume`, `remap`, `effort`, `rates`, `batch`, `repairs`,
`placeholder`, `unknown_ttl` (the brief's families) and `allowance` (subscription lanes: one billing
class per replay, SPEC §9.1 #5). Each family's policies start with the observed policy; the rest are
listed in `lanes_gen._FAMILY_SPECS`.

## Fixtures and provenance

No fixture files. Every record is synthetic: built in the tests with `core.builders`, the area
helpers, or generated by `tokenbill/synth/lanes_gen.py` from a seed (`common.rng`). Usage objects
follow the canonical ledger records of SPEC §3.2 (no real transcripts, no provider pages). The
Appendix A fixtures are transcribed from the SPEC; their expectations are hand-computed in
`lanes_gen.closed_form` (int nano) and checked against the oracle, never produced by it. Prices come
from `core.testing.FakePricer` (`core/facts.json`).

## Facts

No new fact was transcribed. Used from `core.evidence` / `core.facts`: COMPACTION_SUMMARY_TOKENS_DEFAULT
(20,283), THINKING_SHARE_PRIOR (0.505), TOKENIZER_BAND (1.00, 1.35), BATCH_CACHE_HIT_BAND (0.30,
0.98), KEEPALIVE_* defaults (through `core.policy`), CC_FLEET_USD_PER_ACTIVE_DAY ($13, only as the
synthetic panel's default level), the FakePricer rate rows and modifiers. From the SPEC text
itself: the batch point hit band 0.64 (§9.3.5), the effort range `s ± 0.25`, the ±10 s ambiguity,
the 10 s fan-out window and the 0.8 / 0.5 repair thresholds (§9.3.6).

### Unverified

Nothing new. The oracle inherits the SPEC's own open items (listed as O-1 … O-21 in
`CONTRACT-CHANGE-SYNTH-ORACLE-1.md`); the "Managed Agents entrypoint" value is not named anywhere,
so the batch predicate treats any `entrypoint` containing `managed` as Managed Agents.
