"""SPEC §11.5: post-rollout effectiveness checks (``tokenbill policy check-effect``)."""

from __future__ import annotations

import pytest

from tokenbill.core.errors import UsageError
from tokenbill.core.findings import finding_id
from tokenbill.core.records import LaneEvent, LaneEventKind, LaneKind, UsageBuckets
from tokenbill.plan.effectiveness import CHECKS, DETECTOR_ID, KIND, check_effect

from .helpers import DAY_MS, T0, lane

SINCE = T0


def _writes(w1: int, w5: int) -> UsageBuckets:
    return UsageBuckets(cache_read=10_000, cache_write_1h=w1, cache_write_5m=w5, output=100)


def test_ttl_share_above_ninety_percent_is_effective() -> None:
    lanes = [lane("m1", usage=_writes(95, 5), principal="p_" + "a" * 20),
             lane("m2", usage=_writes(95, 5))]
    assert check_effect(lanes, lever_id="cc.prompt_cache_ttl.main", cohort="all",
                        since_ms=SINCE) is None


def test_ttl_share_of_forty_percent_is_not_effective() -> None:
    lanes = [lane("m1", usage=_writes(40, 60), principal="p_" + "a" * 20),
             lane("m2", usage=_writes(40, 60), principal="p_" + "b" * 20),
             lane("x1", usage=_writes(0, 100), kind=LaneKind.SUBAGENT)]  # not a main lane
    f = check_effect(lanes, lever_id="cc.prompt_cache_ttl.main", cohort="all", since_ms=SINCE)
    assert f is not None and f.kind == KIND == "setting-not-effective"
    assert f.detector_id == DETECTOR_ID and f.lever_ids == ("cc.prompt_cache_ttl.main",)
    assert "40.0%" in f.title and "gateway strips the anthropic-beta header" in f.summary
    assert f.n_lanes == 2 and f.n_users == 2 and f.n_events == 4
    assert f.cost_observed.nano is None and f.recoverable is None
    assert dict(f.evidence[0].attrs)["share_pct"] == "40.0"
    assert f.finding_id == finding_id(DETECTOR_ID, KIND, f.scope)
    assert ("lever", "cc.prompt_cache_ttl.main") in f.scope.dims


def test_exactly_ninety_percent_is_not_enough() -> None:
    lanes = [lane("m1", usage=_writes(90, 10))]
    assert check_effect(lanes, lever_id="cc.prompt_cache_ttl.main", cohort="all",
                        since_ms=SINCE) is not None


def test_ttl_window_cohort_and_target() -> None:
    before = lane("old", usage=_writes(0, 100), ts=SINCE - DAY_MS)
    late = lane("late", usage=_writes(0, 100), ts=SINCE + 8 * DAY_MS)
    good = lane("good", usage=_writes(100, 0), team="payments")
    bad = lane("bad", usage=_writes(0, 100), team="search")
    lanes = [before, late, good, bad]
    assert check_effect(lanes, lever_id="cc.prompt_cache_ttl.main", cohort="payments",
                        since_ms=SINCE) is None
    assert check_effect(lanes, lever_id="cc.prompt_cache_ttl.main", cohort="search",
                        since_ms=SINCE) is not None
    assert check_effect(lanes, lever_id="cc.prompt_cache_ttl.main=5m", cohort="search",
                        since_ms=SINCE) is None
    assert check_effect(lanes, lever_id="cc.prompt_cache_ttl.main", target="5m",
                        cohort="payments", since_ms=SINCE) is not None
    mdm = lane("mdm", usage=_writes(0, 100), team=None, extra=(("mdm_group", "laptops"),))
    assert check_effect([mdm], lever_id="cc.prompt_cache_ttl.main", cohort="laptops",
                        since_ms=SINCE) is not None
    assert check_effect([mdm], lever_id="cc.prompt_cache_ttl.main", cohort="nobody",
                        since_ms=SINCE) is None
    assert check_effect([lane("r", usage=UsageBuckets(cache_read=5, output=1))],
                        lever_id="cc.prompt_cache_ttl.main", cohort="all",
                        since_ms=SINCE) is None                     # no writes: no evidence


def test_subagent_and_sdk_ttl_levers_look_at_their_lanes() -> None:
    sub = lane("s", usage=_writes(10, 90), kind=LaneKind.WORKFLOW_AGENT)
    api = lane("a", usage=_writes(99, 1), kind=LaneKind.API_RUN, product="agent_sdk")
    assert check_effect([sub, api], lever_id="cc.prompt_cache_ttl.subagent", cohort="all",
                        since_ms=SINCE) is not None
    assert check_effect([sub, api], lever_id="sdk.ttl", cohort="all", since_ms=SINCE) is None


def _compactions(key: str, pre: list[int], ts: int = SINCE) -> LaneEvent:
    return [LaneEvent(lane_key=key, ts_ms=ts + i * 1000, kind=LaneEventKind.COMPACTION,
                      attrs=(("post_tokens", 20_000), ("pre_tokens", p), ("trigger", "auto")))
            for i, p in enumerate(pre)]


def test_autocompact_median_check() -> None:
    ok = lane("m1", events=_compactions("m1", [390_000, 400_000, 420_000]))
    assert check_effect([ok], lever_id="cc.autocompact_window=400000", cohort="all",
                        since_ms=SINCE) is None
    edge = lane("m2", events=_compactions("m2", [410_000, 430_000]))     # median 420k = 1.05×
    assert check_effect([edge], lever_id="cc.autocompact_window", target="400000",
                        cohort="all", since_ms=SINCE) is None
    bad = lane("m3", events=_compactions("m3", [900_000, 950_000, 960_000, 400_000]))
    f = check_effect([bad], lever_id="cc.autocompact_window=400000", cohort="all",
                     since_ms=SINCE)
    assert f is not None and "925000" in f.summary and f.n_events == 4
    assert dict(f.evidence[0].attrs)["median_pre_tokens"] == "925000"
    odd = lane("m4", events=_compactions("m4", [900_001, 900_000]))
    f = check_effect([odd], lever_id="cc.autocompact_window=400000", cohort="all",
                     since_ms=SINCE)
    assert f is not None and "900000.5" in f.summary
    none = lane("m5")
    assert check_effect([none], lever_id="cc.autocompact_window=400000", cohort="all",
                        since_ms=SINCE) is None
    with pytest.raises(UsageError):
        check_effect([ok], lever_id="cc.autocompact_window", cohort="all", since_ms=SINCE)
    with pytest.raises(UsageError):
        check_effect([ok], lever_id="cc.autocompact_window=big", cohort="all", since_ms=SINCE)


def test_default_model_share_check() -> None:
    sonnet = [lane(f"s{i}", model="claude-sonnet-5") for i in range(9)]
    opus = [lane("o1")]
    assert check_effect(sonnet + opus, lever_id="cc.default_model", cohort="all",
                        since_ms=SINCE) is None                     # 90% > 80%
    f = check_effect(sonnet[:4] + opus * 1 + [lane(f"o{i}") for i in range(2, 7)],
                     lever_id="cc.default_model", cohort="all", since_ms=SINCE)
    assert f is not None and "40.0%" in f.title
    assert "claude-sonnet-5" in f.summary
    f = check_effect(sonnet, lever_id="cc.default_model=claude-opus-5-5", cohort="all",
                     since_ms=SINCE)
    assert f is not None and "0.0%" in f.title
    sdk = [lane("x", product="agent_sdk")]
    assert check_effect(sdk, lever_id="cc.default_model", cohort="all", since_ms=SINCE) is None
    with pytest.raises(UsageError):
        check_effect(sonnet, lever_id="cc.default_model=bad model!", cohort="all",
                     since_ms=SINCE)


def test_effort_share_checks() -> None:
    low = [lane(f"l{i}", effort="medium") for i in range(4)]
    high = [lane("h1", effort="xhigh"), lane("u1")]           # u1: effort unknown, left out
    f = check_effect(low + high, lever_id="cc.default_effort=medium", cohort="all",
                     since_ms=SINCE)                         # 8 of 10 known: 80% is not > 80%
    assert f is not None and "80.0%" in f.title and dict(f.evidence[0].attrs)["requests"] == 10
    assert check_effect(low + high + [lane("l9", effort="low")],
                        lever_id="cc.default_effort=medium", cohort="all",
                        since_ms=SINCE) is None              # 10 of 12
    f = check_effect(low[:2] + high + [lane("h2", effort="high")],
                     lever_id="cc.max_effort", target="medium", cohort="all", since_ms=SINCE)
    assert f is not None and "50.0%" in f.title
    assert check_effect([lane("u2")], lever_id="cc.max_effort=high", cohort="all",
                        since_ms=SINCE) is None
    with pytest.raises(UsageError):
        check_effect(low, lever_id="cc.default_effort", cohort="all", since_ms=SINCE)
    with pytest.raises(UsageError):
        check_effect(low, lever_id="cc.max_effort=ultra", cohort="all", since_ms=SINCE)


def test_argument_checks() -> None:
    lanes = [lane("m1", usage=_writes(1, 1))]
    assert set(CHECKS) == {"cc.prompt_cache_ttl.main", "cc.prompt_cache_ttl.subagent", "sdk.ttl",
                           "cc.autocompact_window", "cc.default_model", "cc.default_effort",
                           "cc.max_effort"}
    for kw in (dict(lever_id="geo.global"), dict(lever_id=""), dict(cohort=""),
               dict(since_ms=-1), dict(days=0), dict(days=400),
               dict(lever_id="cc.prompt_cache_ttl.main=1h", target="5m"),
               dict(lever_id="cc.prompt_cache_ttl.main=2h"),
               dict(target="  ")):
        args = dict(lever_id="cc.prompt_cache_ttl.main", cohort="all", since_ms=SINCE)
        args.update(kw)
        with pytest.raises(UsageError):
            check_effect(lanes, **args)
