"""Context transforms (compaction window, cold resume), rate transforms (model remap with the
tokenizer band, effort, fast_off, geo_global, regional_to_global) and batch (SPEC §9.3.3–§9.3.5,
§9.3.7) on hand-computed lanes. Rates in nano per token (FakePricer, facts.json)."""

from __future__ import annotations

import pytest

from tokenbill.core.labels import Evidence
from tokenbill.core.records import LaneEvent, LaneEventKind, LaneKind, RequestParams, WorkloadClass
from tokenbill.core.types import Policy

from .helpers import EPOCH_MS, bounds, costs, lane, lane_of, replay, req

# Sonnet 5
S_W5, S_READ, S_OUT = 2_500, 200, 10_000
# Opus 5.5
O_W5, O_W1, O_READ, O_OUT, O_IN = 5_000, 8_000, 200, 20_000, 4_000


# ---------------------------------------------------------------------------------------------
# compaction window
# ---------------------------------------------------------------------------------------------


def _sonnet(rows, key="Lc", **kw):
    return lane(rows, lane_key=key, model="claude-sonnet-5",
                attribution={"agent_product": "claude_code"}, **kw)


def test_compaction_when_the_prefix_is_cold_writes_the_whole_effective_context() -> None:
    ln = _sonnet([(0, 0, 300_000, 0, 0, 1_000), (600, 0, 450_000, 0, 0, 1_000)])
    res = replay([ln], "compact-window=400000,post=20000")
    first, second = costs(res)
    assert first == (300_000 * S_W5 + 1_000 * S_OUT,) * 3
    compaction = 450_000 * S_W5 + 20_000 * S_OUT
    assert second == (compaction + 170_000 * S_W5 + 1_000 * S_OUT,) * 3
    extra = res.outcomes[1].extra[0]  # type: ignore[index]
    assert (extra.usage.cache_read, extra.usage.cache_write_5m) == (0, 450_000)


@pytest.mark.parametrize("third,events,expected_usage", [
    # a COMPACTION event before request 2 resets `removed`: the request is unchanged
    ((60, 0, 50_000, 0, 0, 1_000), True, (0, 50_000)),
    # context drop below half of the previous request resets as well
    ((60, 0, 200_000, 0, 0, 1_000), False, (0, 200_000)),
    # no reset: 280k removed tokens come out of reads first, then writes
    ((60, 250_000, 50_000, 0, 0, 1_000), False, (0, 20_000)),
])
def test_compaction_removed_tokens_and_resets(third, events, expected_usage) -> None:
    evs = [LaneEvent("Lc", EPOCH_MS + 45_000, LaneEventKind.COMPACTION,
                     (("post_tokens", 50_000),))] if events else []
    ln = _sonnet([(0, 0, 300_000, 0, 0, 1_000), (30, 300_000, 150_000, 0, 0, 1_000), third],
                 events=evs)
    res = replay([ln], "compact-window=400000,post=20000")
    out = res.outcomes[2]  # type: ignore[index]
    assert (out.usage.cache_read, out.usage.cache_write_5m) == expected_usage
    assert out.changed is (expected_usage == (0, 20_000))


def test_compaction_skips_models_without_1m_context_and_non_main_lanes() -> None:
    haiku = lane([(0, 0, 500_000, 0, 0, 0)], lane_key="Lh", model="claude-haiku-4-5")
    sub = _sonnet([(0, 0, 500_000, 0, 0, 0)], key="Ls", kind=LaneKind.SUBAGENT)
    res = replay([haiku, sub], "compact-window=400000")
    assert res.lanes_skipped == (("Lh", "compaction window needs a model with 1m_context"),)
    assert res.cost.nano == res.baseline.nano and res.added_calls == 0


def test_summary_tokens_default_and_org_median() -> None:
    ln = _sonnet([(0, 0, 300_000, 0, 0, 0), (30, 300_000, 150_000, 0, 0, 0)], key="La")
    res = replay([ln], "compact-window=400000")
    assert res.outcomes[1].extra[0].usage.output == 20_283  # type: ignore[index]
    assert any("COMPACTION_SUMMARY_TOKENS_DEFAULT" in a for a in res.assumptions)

    def with_post(key: str, post: int):
        return _sonnet([(0, 0, 300_000, 0, 0, 0), (30, 300_000, 150_000, 0, 0, 0)], key=key,
                       events=[LaneEvent(key, EPOCH_MS + 10_000, LaneEventKind.COMPACTION,
                                         (("post_tokens", post),))])

    two = replay([with_post("Lb", 10_000), with_post("Lc", 30_000)], "compact-window=400000")
    assert {o.extra[0].usage.output for o in two.outcomes or () if o.extra} == {20_000}
    assert any("median post_tokens" in a for a in two.assumptions)
    three = replay([with_post("Lb", 10_000), with_post("Lc", 30_000), with_post("Ld", 12_000)],
                   "compact-window=400000")
    assert {o.extra[0].usage.output for o in three.outcomes or () if o.extra} == {12_000}


def test_compaction_on_the_first_request_uses_new_equal_to_t0() -> None:
    # literal §9.3.3: new_0 = T_0, so a first request above the window becomes S_c + T_0 (O-6)
    ln = _sonnet([(0, 0, 500_000, 0, 0, 0)])
    res = replay([ln], "compact-window=400000,post=20000")
    out = res.outcomes[0]  # type: ignore[index]
    assert out.usage.cache_write_5m == 520_000
    assert out.extra[0].usage.cache_write_5m == 500_000


# ---------------------------------------------------------------------------------------------
# cold resume
# ---------------------------------------------------------------------------------------------


def _cold_lane(kind=LaneKind.MAIN):
    return lane([(0, 0, 0, 150_000, 0, 0), (30, 150_000, 0, 100_000, 0, 0),
                 (7_230, 0, 0, 260_000, 0, 0), (7_260, 260_000, 0, 10_000, 0, 0)],
                lane_key="Lcr", kind=kind, attribution={"agent_product": "claude_code"})


def test_cold_resume_clear_restarts_from_the_first_context() -> None:
    res = replay([_cold_lane()], "cold-resume=clear,min=200000")
    cr = costs(res)
    assert cr[2] == (160_000 * O_W1,) * 3                      # T_0 + new = 150k + 10k
    assert cr[3] == (160_000 * O_READ + 10_000 * O_W1,) * 3    # removed 100k out of the reads
    assert res.added_calls == 0 and res.saving.upper_bound
    assert res.saving.nano == (260_000 - 160_000) * O_W1 + 100_000 * O_READ


def test_cold_resume_needs_main_lanes_and_the_minimum_context() -> None:
    assert replay([_cold_lane(LaneKind.SUBAGENT)], "cold-resume=compact").saving.nano == 0
    assert replay([_cold_lane()], "cold-resume=compact,min=300000").saving.nano == 0
    assert replay([_cold_lane()], "cold-resume=compact,min=200000").added_calls == 1


# ---------------------------------------------------------------------------------------------
# model remap and the tokenizer band
# ---------------------------------------------------------------------------------------------


def test_same_family_remap_is_pure_repricing_without_a_band() -> None:
    ln = lane([(0, 0, 10_000, 0, 0, 100), (30, 10_000, 1_000, 0, 0, 100)], lane_key="Lr",
              model="claude-opus-5")
    res = replay([ln], "model=claude-opus-5-5")
    assert bounds(res.cost) == (61_000_000,) * 3
    assert res.cost.low_nano is None
    assert res.outcomes[0].usage.cache_write_5m == 10_000  # type: ignore[index]
    assert any("tokenizer band" in a for a in res.assumptions)


def test_legacy_to_new_tokenizer_band_raises_the_high_bound() -> None:
    ln = lane([(0, 0, 10_000, 0, 0, 1_000)], lane_key="Lr", model="claude-sonnet-4-6")
    res = replay([ln], "model=claude-sonnet-5")
    assert costs(res)[0] == (35_000_000, 35_000_000, 13_500 * S_W5 + 1_350 * S_OUT)


def test_new_to_legacy_band_lowers_the_low_bound_and_applies_the_target_minimum() -> None:
    big = lane([(0, 0, 10_000, 0, 0, 1_000)], lane_key="Lbig")
    res = replay([big], "model=claude-haiku-4-5")
    # haiku 4.5: 5m 1,250 / output 5,000 nano per token; low scales 10,000 by 20/27 → 7,407
    assert costs(res)[0] == (17_500_000, 7_407 * 1_250 + 741 * 5_000, 17_500_000)
    small = lane([(0, 0, 3_000, 0, 0, 0)], lane_key="Lsmall")
    gated = replay([small], "model=claude-haiku-4-5")
    usage = gated.outcomes[0].usage  # type: ignore[index]
    assert (usage.uncached_input, usage.cache_write_5m) == (3_000, 0)   # 3,000 < 4,096
    assert costs(gated)[0] == (3_000_000, 2_222_000, 3_000_000)


def test_remap_selector_and_every_inference_of_the_lane() -> None:
    main = lane([(0, 0, 10_000, 0, 0, 0)], lane_key="Lmain", model="claude-opus-5")
    sub = lane([(0, 0, 10_000, 0, 0, 0)], lane_key="Lsub", model="claude-opus-5",
               kind=LaneKind.SUBAGENT)
    res = replay([main, sub], "model=claude-opus-5-5@lane_kind:subagent")
    assert dict(res.per_lane) == {"Lmain": 10_000 * 6_250, "Lsub": 10_000 * O_W5}


# ---------------------------------------------------------------------------------------------
# effort
# ---------------------------------------------------------------------------------------------


def _effort_lane(effort, output=10_000, reasoning=None, kind=LaneKind.MAIN):
    usage = {"cache_write_5m": 1_000, "output": output}
    if reasoning is not None:
        usage["output_reasoning"] = reasoning
    r = req("Le", 0, 0, usage, attribution={"agent_product": "claude_code"},
            params=RequestParams(model_requested="claude-opus-5-5", effort=effort))
    return lane_of([r], kind=kind)


def test_effort_cap_uses_the_thinking_share_prior_with_a_range() -> None:
    res = replay([_effort_lane("max")], "effort=medium")
    base = 1_000 * O_W5
    # th = floor(0.505 · 10,000) = 5,050; reductions floor(5,050·(1 − s)) for s = .5 / .25 / .75
    assert costs(res)[0] == (base + 7_475 * O_OUT, base + 6_213 * O_OUT, base + 8_738 * O_OUT)
    assert res.saving.upper_bound and res.saving.evidence is Evidence.ESTIMATED
    assert any("thinking share 0.505" in a for a in res.assumptions)


def test_effort_cap_uses_known_reasoning_tokens() -> None:
    res = replay([_effort_lane("xhigh", reasoning=4_000)], "effort=high")
    base = 1_000 * O_W5
    assert costs(res)[0] == (base + 8_000 * O_OUT, base + 7_000 * O_OUT, base + 9_000 * O_OUT)
    usage = res.outcomes[0].usage  # type: ignore[index]
    assert (usage.output, usage.output_reasoning) == (8_000, 2_000)


@pytest.mark.parametrize("effort,spec", [("medium", "effort=medium"), (None, "effort=low"),
                                         ("high", "effort=medium@lane_kind:subagent")])
def test_effort_cap_leaves_requests_at_or_below_the_level(effort, spec) -> None:
    res = replay([_effort_lane(effort)], spec)
    assert res.saving.nano == 0 and res.cost.low_nano is None


def test_effort_scale_one_changes_only_the_low_bound() -> None:
    res = replay([_effort_lane("max")], "effort=medium,scale=1")
    base = 1_000 * O_W5
    assert costs(res)[0] == (base + 10_000 * O_OUT, base + 8_738 * O_OUT, base + 10_000 * O_OUT)


# ---------------------------------------------------------------------------------------------
# fast_off, geo_global, regional_to_global
# ---------------------------------------------------------------------------------------------


def test_fast_off_is_exact_rate_arithmetic() -> None:
    ln = lane([(0, 0, 100_000, 0, 0, 0)], lane_key="Lf", speed="fast")
    res = replay([ln], "fast=off")
    assert (res.baseline.nano, res.cost.nano, res.saving.nano) == \
        (1_000_000_000, 500_000_000, 500_000_000)
    assert res.cost.evidence is Evidence.EXACT and res.saving.evidence is Evidence.EXACT
    assert res.calibration.value == "n/a"


def test_fast_off_flips_fast_toggle_misses_to_hits() -> None:
    reqs = [req("Lt", 0, 0, {"cache_write_5m": 100_000}),
            req("Lt", 1, 30, {"cache_write_5m": 102_000}, speed="fast")]
    res = replay([lane_of(reqs)], "fast=off")
    assert costs(res)[1] == (100_000 * O_READ + 2_000 * O_W5,) * 3
    assert res.saving.evidence is Evidence.ESTIMATED
    assert res.baseline.nano == 100_000 * O_W5 + 102_000 * 10_000


def test_geo_global_and_regional_to_global() -> None:
    us = lane([(0, 0, 100_000, 0, 0, 0)], lane_key="Lus", inference_geo="us")
    res = replay([us], "geo=global")
    assert (res.baseline.nano, res.cost.nano) == (550_000_000, 500_000_000)
    assert res.saving.evidence is Evidence.EXACT
    regional = lane([(0, 0, 100_000, 0, 0, 0)], lane_key="Lreg", model="claude-opus-5",
                    channel="bedrock", endpoint_scope="regional")
    res2 = replay([regional], "regional=global")
    assert (res2.baseline.nano, res2.cost.nano) == (687_500_000, 625_000_000)
    unknown = lane([(0, 0, 100_000, 0, 0, 0)], lane_key="Lunk", model="claude-opus-5",
                   channel="bedrock", endpoint_scope="unknown")
    res3 = replay([unknown], "regional=global")
    assert bounds(res3.baseline) == (625_000_000, 625_000_000, 687_500_000)
    assert bounds(res3.cost) == (625_000_000,) * 3
    assert bounds(res3.saving) == (0, 0, 62_500_000)
    assert res3.saving.evidence is Evidence.ESTIMATED


# ---------------------------------------------------------------------------------------------
# batch
# ---------------------------------------------------------------------------------------------


def _single(workload=WorkloadClass.CI, entry=None, **ctx):
    r = req("Lb", 0, 0, {"cache_read": 50_000, "cache_write_5m": 10_000,
                         "uncached_input": 100, "output": 1_000},
            ctx.pop("model", "claude-opus-5-5"),
            attribution={"agent_product": "agent_sdk", "workload_class": workload,
                         "entrypoint": entry}, **ctx)
    return lane_of([r], kind=LaneKind.API_RUN)


def test_batch_retains_reads_at_the_hit_band() -> None:
    res = replay([_single()], "batch=eligible")
    # batch halves every rate: read 100, 5m 2,500, input 2,000, output 10,000
    assert costs(res)[0] == (83_400_000, 42_600_000, 124_200_000)
    usage = res.outcomes[0].usage  # type: ignore[index]
    assert (usage.cache_read, usage.cache_write_5m) == (32_000, 28_000)
    assert any("h = 0.64" in a for a in res.assumptions)


@pytest.mark.parametrize("make", [
    lambda: _single(WorkloadClass.INTERACTIVE),
    lambda: _single(entry="managed-agents"),
    lambda: _single(service_tier="batch"),
    lambda: _single(speed="fast"),
    lambda: lane_of([req("Lb2", 0, 0, {"cache_write_5m": 10_000}),
                     req("Lb2", 1, 30, {"cache_read": 10_000})], kind=LaneKind.API_RUN),
])
def test_batch_predicate_excludes(make) -> None:
    ln = make()
    res = replay([ln], "batch=eligible")
    assert res.saving.nano == 0 and all(not o.changed for o in res.outcomes or ())


def test_fast_off_makes_a_fast_request_batch_eligible() -> None:
    res = replay([_single(speed="fast")], "fast=off;batch=eligible")
    assert costs(res)[0][0] == 83_400_000


def test_batch_on_bedrock_is_uncached() -> None:
    res = replay([_single(model="claude-opus-5", channel="bedrock", endpoint_scope="global")],
                 "batch=eligible")
    # opus 5 on Bedrock (global) at batch: input 2,500, output 12,500 nano per token
    assert costs(res)[0] == (60_100 * 2_500 + 1_000 * 12_500,) * 3
    usage = res.outcomes[0].usage  # type: ignore[index]
    assert (usage.cache_read, usage.cache_write, usage.uncached_input) == (0, 0, 60_100)


def test_observed_policy_object_is_identity() -> None:
    res = replay([_single()], Policy.observed())
    assert res.cost == res.baseline and res.assumptions == ("observed policy: identity replay",)
