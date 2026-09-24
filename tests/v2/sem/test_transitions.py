"""SPEC §3.15: the shared transition / miss / cause definition (Appendix A.9, one fixture per cause
rule in precedence order, ambiguity, the documented prediction, the static-prefix floor)."""

from __future__ import annotations

import time
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core.builders import FlatRates, lane_from_table, make_inference
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.lanes import ttl_of_last_write
from tokenbill.core.records import (
    Attribution,
    CacheDiagnostic,
    InferenceKind,
    LaneKind,
    RequestParams,
    UsageBuckets,
)
from tokenbill.core.transitions import (
    AMBIGUITY_MS,
    CAUSES,
    DIAG_CAUSES,
    HIT,
    MISS_MIN_FRACTION,
    MISS_MIN_TOKENS,
    classify_transitions,
    is_miss_event,
    lane_first_reads_of,
    static_prefix_floor,
)

from .helpers import event, lane, req, two_step, usage

PRICER = FlatRates()
RULES = RulesTable()
BETA = "mid-conversation-output-config-2026-07-01"


def classify(lane_obj: Any) -> list:
    return classify_transitions(lane_obj, pricer=PRICER, rules=RULES)


def one(lane_obj: Any):
    (t,) = classify(lane_obj)
    return t


def rewrite(total: int = 100_000, **kw: Any) -> tuple[UsageBuckets, UsageBuckets]:
    """A warm-able first request followed by a full rewrite (a miss of ``total``)."""
    return usage(w5=total), usage(w5=total, **kw)


def test_constants() -> None:
    assert MISS_MIN_TOKENS == 2000 and str(MISS_MIN_FRACTION) == "0.05"
    assert AMBIGUITY_MS == 10_000
    assert CAUSES[0] == "compaction" and CAUSES[-1] == "unexplained"


# ---------------------------------------------------------------------------------------------
# Appendix A.9: the miss threshold
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("expected", "reads", "miss"), [
    (100_000, 98_000, False),   # M = 2,000 ≤ 5,000
    (100_000, 94_999, True),    # M = 5,001 > 5,000
    (100_000, 95_000, False),   # M = 5,000 is not > 5% of E
    (30_000, 28_001, False),    # M = 1,999 < 2,000
    (30_000, 28_000, True),     # M = 2,000 > 1,500 and ≥ 2,000
])
def test_appendix_a9_threshold(expected: int, reads: int, miss: bool) -> None:
    t = one(two_step(usage(w5=expected), usage(r=reads, w5=expected - reads)))
    assert (t.expected_reuse, t.reads, t.missed) == (expected, reads, expected - reads)
    assert t.is_miss_event is miss
    assert is_miss_event(expected - reads, expected) is miss
    assert (t.cause == HIT) is (not miss)


def test_is_miss_event_never_uses_2048() -> None:
    assert is_miss_event(2000, 10_000)
    assert not is_miss_event(1999, 10_000)
    assert not is_miss_event(0, 0)


# ---------------------------------------------------------------------------------------------
# Appendix A.1 lane: ttl-expiry, quantities
# ---------------------------------------------------------------------------------------------


def a1_lane():
    rows = [(0, 0, 100_000, 0, 0, 500), (420, 0, 102_000, 0, 0, 500),
            (840, 0, 104_000, 0, 0, 500), (1260, 0, 106_000, 0, 0, 500)]
    return lane_from_table(rows)


def test_appendix_a1_ttl_expiry() -> None:
    ts = classify(a1_lane())
    assert [t.index for t in ts] == [1, 2, 3]
    assert [t.expected_reuse for t in ts] == [100_000, 102_000, 104_000]
    assert [t.missed for t in ts] == [100_000, 102_000, 104_000]
    assert [t.prev_prefix for t in ts] == [100_000, 102_000, 104_000]
    assert [t.total for t in ts] == [102_000, 104_000, 106_000]
    for t in ts:
        assert t.is_miss_event and t.cause == "ttl-expiry" and t.sub_cause is None
        assert t.ttl_s == 300 and t.gap_ms == 420_000 and t.ambiguous is False
        assert t.predicted_hit is False and t.diag_reason is None
        assert t.lane_key == "lane-0"


# ---------------------------------------------------------------------------------------------
# rule 1: compaction (beats model switch and ttl)
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["compaction", "clear", "context_edit"])
def test_reset_event_beats_ttl(kind: str) -> None:
    attrs: dict[str, Any] = {}
    if kind == "compaction":
        attrs = {"trigger": "auto", "pre_tokens": 100_000, "post_tokens": 20_000,
                 "duration_ms": 1000, "dropped_tokens": None}
    elif kind == "context_edit":
        attrs = {"edit_type": "clear_tool_uses_20250919", "cleared_input_tokens": 40_000}
    first, second = rewrite()
    t = one(two_step(first, second, gap_s=400, events=[event(kind, 200, **attrs)]))
    assert t.cause == "compaction" and t.predicted_hit is False


def test_compaction_beats_model_switch() -> None:
    first, second = rewrite()
    t = one(two_step(first, second, model2="claude-sonnet-5",
                     events=[event("clear", 10)]))
    assert t.cause == "compaction"


def test_reset_window_is_half_open() -> None:
    first, second = rewrite()
    at_prev = one(two_step(first, second, gap_s=30, events=[event("clear", 0)]))
    assert at_prev.cause == "unexplained"
    at_cur = one(two_step(first, second, gap_s=30, events=[event("clear", 30)]))
    assert at_cur.cause == "compaction"


def test_applied_edits_and_dropped_thinking_are_resets() -> None:
    first, second = rewrite()
    t = one(two_step(first, second, kw2={"applied_edits": [("clear_tool_uses_20250919", 5)]}))
    assert t.cause == "compaction"
    t = one(two_step(first, second, kw2={"thinking_dropped": 2}))
    assert t.cause == "compaction"


# ---------------------------------------------------------------------------------------------
# rule 2: model switch and its sub-causes
# ---------------------------------------------------------------------------------------------


def test_model_switch_beats_ttl() -> None:
    first, second = rewrite()
    t = one(two_step(first, second, gap_s=400, model2="claude-sonnet-5"))
    assert (t.cause, t.sub_cause) == ("model-switch", "user")
    assert t.predicted_hit is False


def test_refusal_fallback_from_inference_kinds() -> None:
    declined = make_inference(usage(u=100_000, o=0), model="claude-fable-5",
                              kind=InferenceKind.FALLBACK_DECLINED, billable=False,
                              billing_rule_id="anthropic.refusal.pre_output")
    r0 = req("L", 0, 0, usage(w5=100_000), "claude-fable-5")
    r1 = req("L", 1, 30, usage(w5=100_000), "claude-opus-4-8", kind=InferenceKind.FALLBACK,
             extra_inferences=[declined])
    t = one(lane([r0, r1]))
    assert (t.cause, t.sub_cause) == ("model-switch", "refusal-fallback")


def test_refusal_fallback_from_event() -> None:
    first, second = rewrite()
    fb = event("model_fallback", 20, from_model="claude-fable-5", to_model="claude-opus-4-8",
               trigger="refusal", credited=None)
    t = one(two_step(first, second, model="claude-fable-5", model2="claude-opus-4-8",
                     events=[fb]))
    assert (t.cause, t.sub_cause) == ("model-switch", "refusal-fallback")


def test_availability_fallback_needs_error_and_fallback_event() -> None:
    first, second = rewrite()
    err = event("api_error", 10, status=529, error_type="overloaded_error", retry_attempt=1,
                max_retries=10, retry_in_ms=500)
    fb = event("model_fallback", 20, from_model="claude-opus-5-5", to_model="claude-sonnet-5",
               trigger="availability", credited=None)
    t = one(two_step(first, second, model2="claude-sonnet-5", events=[err, fb]))
    assert (t.cause, t.sub_cause) == ("model-switch", "availability-fallback")
    err2 = event("api_error", 10, status=None, error_type="overloaded")
    t = one(two_step(first, second, model2="claude-sonnet-5", events=[err2, fb]))
    assert t.sub_cause == "availability-fallback"
    # an error alone (no fallback event) is a user switch
    t = one(two_step(first, second, model2="claude-sonnet-5", events=[err]))
    assert t.sub_cause == "user"
    # a 500 with an availability fallback is not the documented pattern either
    err500 = event("api_error", 10, status=500, error_type="api_error")
    t = one(two_step(first, second, model2="claude-sonnet-5", events=[err500, fb]))
    assert t.sub_cause == "user"


CC_MAIN = {"agent_product": "claude_code"}


def plan_lane(models: list[str], *, prompts: bool = True, kind: LaneKind = LaneKind.MAIN,
              product: str = "claude_code"):
    reqs = [req("L", i, 30 * i, usage(w5=100_000 + i), m, attribution={"agent_product": product})
            for i, m in enumerate(models)]
    events = [event("human_prompt", 30 * i - 5) for i in range(1, len(models))] if prompts else []
    return lane(reqs, events=events, kind=kind)


def test_plan_toggle() -> None:
    ts = classify(plan_lane(["claude-opus-5-5", "claude-sonnet-5", "claude-opus-5-5"]))
    assert [(t.cause, t.sub_cause) for t in ts] == [("model-switch", "plan-toggle")] * 2


def test_plan_toggle_needs_alternation_prompt_and_claude_code_main() -> None:
    once = classify(plan_lane(["claude-opus-5-5", "claude-sonnet-5", "claude-sonnet-5"]))
    assert once[0].sub_cause == "user"          # alternates only once
    no_prompt = classify(plan_lane(["claude-opus-5-5", "claude-sonnet-5", "claude-opus-5-5"],
                                   prompts=False))
    assert [t.sub_cause for t in no_prompt] == ["user", "ping-pong"]
    sub = classify(plan_lane(["claude-opus-5-5", "claude-sonnet-5", "claude-opus-5-5"],
                             kind=LaneKind.SUBAGENT))
    assert sub[0].sub_cause == "user"
    sdk = classify(plan_lane(["claude-opus-5-5", "claude-sonnet-5", "claude-opus-5-5"],
                             product="agent_sdk"))
    assert sdk[0].sub_cause == "user"
    # a switch to a third family on an alternating lane is not a plan toggle
    third = classify(plan_lane(["claude-opus-5-5", "claude-sonnet-5", "claude-opus-5-5",
                                "claude-haiku-4-5"]))
    assert third[2].sub_cause == "user"


def test_ping_pong() -> None:
    reqs = [req("L", i, 30 * i, usage(w5=100_000 + i), m, attribution={"agent_product": "api"})
            for i, m in enumerate(["claude-opus-5-5", "claude-sonnet-5", "claude-opus-5-5"])]
    ts = classify(lane(reqs))
    assert [t.sub_cause for t in ts] == ["user", "ping-pong"]
    # beyond τ it is not ping-pong (the cache would be cold anyway)
    reqs = [req("L", i, 400 * i, usage(w5=100_000 + i), m)
            for i, m in enumerate(["claude-opus-5-5", "claude-sonnet-5", "claude-opus-5-5"])]
    assert [t.sub_cause for t in classify(lane(reqs))] == ["user", "user"]


# ---------------------------------------------------------------------------------------------
# rule 3/4: ttl-expiry beats param change; param-change sub-causes and the effort exemption
# ---------------------------------------------------------------------------------------------


def test_ttl_beats_param_change() -> None:
    first, second = rewrite()
    t = one(two_step(first, second, gap_s=400, kw2={"speed": "fast"}))
    assert t.cause == "ttl-expiry"


def test_fast_toggle() -> None:
    first, second = rewrite()
    t = one(two_step(first, second, kw2={"speed": "fast"}))
    assert (t.cause, t.sub_cause) == ("param-change", "fast-toggle")
    assert t.predicted_hit is False


def effort_lane(product: str, model: str = "claude-opus-5-5", *, betas: tuple[str, ...] = (),
                channel: str = "anthropic_api", version: str | None = "2.1.270",
                param: str = "effort", values: tuple[str, str] = ("high", "low")):
    first, second = rewrite()
    attr = Attribution(agent_product=product, client_version=version)
    p0 = RequestParams(model_requested=model, betas=betas, **{param: values[0]})
    p1 = RequestParams(model_requested=model, betas=betas, **{param: values[1]})
    return two_step(first, second, model=model,
                    kw1={"attribution": attr, "params": p0, "channel": channel},
                    kw2={"attribution": attr, "params": p1, "channel": channel})


def test_effort_change_ignored_for_claude_code_on_opus_5_5() -> None:
    t = one(effort_lane("claude_code"))
    assert t.cause == "unexplained" and t.sub_cause is None   # not a param change
    assert t.predicted_hit is True                             # the documented model: warm


def test_effort_change_is_a_cause_for_sdk_on_opus_5_5_without_the_beta() -> None:
    t = one(effort_lane("agent_sdk"))
    assert (t.cause, t.sub_cause) == ("param-change", "effort-change")
    assert t.predicted_hit is False


def test_effort_change_variants() -> None:
    assert one(effort_lane("agent_sdk", "claude-opus-5", betas=(BETA,))).cause == "unexplained"
    assert one(effort_lane("claude_code", channel="bedrock")).sub_cause == "effort-change"
    assert one(effort_lane("claude_code", "claude-fable-5-1",
                           version="2.1.250")).sub_cause == "effort-change"
    assert one(effort_lane("agent_sdk", param="thinking",
                           values=("adaptive", "off"))).sub_cause == "effort-change"
    # unknown (None) is never a change
    assert one(effort_lane("agent_sdk", values=("high", None))).cause == "unexplained"  # type: ignore[arg-type]


def test_client_upgrade_and_directory_change() -> None:
    first, second = rewrite()
    t = one(two_step(first, second, kw1={"attribution": {"client_version": "2.1.260"}},
                     kw2={"attribution": {"client_version": "2.1.270"}}))
    assert (t.cause, t.sub_cause) == ("param-change", "client-upgrade")
    t = one(two_step(first, second, kw1={"attribution": {"cwd_key": "h_" + "a" * 20}},
                     kw2={"attribution": {"cwd_key": "h_" + "b" * 20}}))
    assert (t.cause, t.sub_cause) == ("param-change", "directory-change")
    t = one(two_step(first, second, kw2={"attribution": {"cwd_key": "h_" + "b" * 20}}))
    assert t.cause == "unexplained"


# ---------------------------------------------------------------------------------------------
# rule 5: canonical diagnostic labels
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("reason", "cause", "sub"), [
    ("tools_changed", "tools-changed", None),
    ("system_changed", "system-changed", None),
    ("messages_changed", "messages-changed", None),
    ("param_changed", "param-change", "diag"),
    ("key_changed", "unexplained", "cache-key"),
    ("compacted", "compaction", None),
    ("model_changed", "model-switch", "diag"),
    ("previous_message_not_found", "unexplained", None),
    ("unavailable", "unexplained", None),
])
def test_diag_labels(reason: str, cause: str, sub: str | None) -> None:
    diag = CacheDiagnostic(reason=reason, provider_reason=reason,
                           missed_input_tokens_estimate=1000,
                           source="anthropic.cache_diagnostics")
    first, second = rewrite()
    t = one(two_step(first, second, kw2={"diagnostics": diag}))
    assert (t.cause, t.sub_cause, t.diag_reason) == (cause, sub, reason)
    assert reason not in DIAG_CAUSES or DIAG_CAUSES[reason] == (cause, sub)


def test_diag_reason_recorded_on_hits_and_below_param_change() -> None:
    diag = CacheDiagnostic(reason="tools_changed", provider_reason="tools_changed",
                           missed_input_tokens_estimate=None, source="anthropic.cache_diagnostics")
    hit = one(two_step(usage(w5=100_000), usage(r=100_000, w5=2000), kw2={"diagnostics": diag}))
    assert hit.cause == HIT and hit.diag_reason == "tools_changed"
    first, second = rewrite()
    t = one(two_step(first, second, kw2={"diagnostics": diag, "speed": "fast"}))
    assert t.cause == "param-change"


# ---------------------------------------------------------------------------------------------
# rules 6/7: context-shrank, unexplained
# ---------------------------------------------------------------------------------------------


def test_context_shrank() -> None:
    t = one(two_step(usage(w5=100_000), usage(w5=80_000)))
    assert t.cause == "context-shrank" and t.missed == 80_000
    t = one(two_step(usage(w5=100_000), usage(w5=90_000)))     # exactly 0.9·T: not shrunk
    assert t.cause == "unexplained"


def test_unexplained() -> None:
    t = one(two_step(usage(w5=100_000), usage(w5=100_000)))
    assert (t.cause, t.sub_cause) == ("unexplained", None)
    assert t.predicted_hit is True


# ---------------------------------------------------------------------------------------------
# ambiguity, τ, prediction
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("gap_s", "ambiguous", "cause"), [
    (310, True, "ttl-expiry"),
    (310.001, False, "ttl-expiry"),
    (300.5, True, "ttl-expiry"),
    (300, True, "unexplained"),
    (290, True, "unexplained"),
    (289.999, False, "unexplained"),
])
def test_ambiguity_window(gap_s: float, ambiguous: bool, cause: str) -> None:
    first, second = rewrite()
    t = one(two_step(first, second, gap_s=gap_s))
    assert t.ambiguous is ambiguous and t.cause == cause
    assert t.predicted_hit is (gap_s <= 300)


def test_ambiguity_is_flagged_on_hits_too() -> None:
    t = one(two_step(usage(w5=100_000), usage(r=100_000, w5=500), gap_s=305))
    assert t.cause == HIT and t.ambiguous is True and t.ttl_s == 300


def test_predicted_hit_none_when_ttl_unknown() -> None:
    t = one(two_step(usage(u=100_000), usage(u=100_000)))
    assert t.ttl_s is None and t.predicted_hit is None and t.ambiguous is False
    assert t.expected_reuse == 0 and not t.is_miss_event


def test_ttl_from_hint_and_one_hour_writes() -> None:
    hinted = one(two_step(usage(cache_write_unknown=100_000),
                          usage(cache_write_unknown=100_000), gap_s=400,
                          kw1={"write_ttl_hint": "1h"}, kw2={"write_ttl_hint": "1h"}))
    assert hinted.ttl_s == 3600 and hinted.cause == "unexplained"
    # hint on the current request only (no known write TTL before it)
    hint_only = one(two_step(usage(cache_write_unknown=100_000),
                             usage(cache_write_unknown=100_000), kw2={"write_ttl_hint": "5m"}))
    assert hint_only.ttl_s == 300
    one_hour = one(two_step(usage(w1=100_000), usage(w1=100_000), gap_s=400))
    assert one_hour.ttl_s == 3600 and one_hour.cause == "unexplained"
    other = one(two_step(usage(cache_write_other=100_000, cache_write_other_ttl_s=1800),
                         usage(cache_write_other=100_000, cache_write_other_ttl_s=1800),
                         gap_s=2000))
    assert other.ttl_s == 1800 and other.cause == "ttl-expiry"


def test_predicted_hit_needs_min_cacheable() -> None:
    small = one(two_step(usage(w5=1000), usage(r=1000, w5=10)))     # E = 1,000 < 1,024
    assert small.predicted_hit is False
    big = one(two_step(usage(w5=1024), usage(r=1024, w5=10)))
    assert big.predicted_hit is True


def test_predicted_hit_ignores_the_transitions_own_reads() -> None:
    warm = one(two_step(usage(w5=100_000), usage(r=100_000, w5=10)))
    cold = one(two_step(usage(w5=100_000), usage(w5=100_010)))
    assert warm.predicted_hit is cold.predicted_hit is True


class CountingPricer(FlatRates):
    def __init__(self) -> None:
        self.calls = 0

    def min_cacheable_tokens(self, ctx: Any, *, ts_ms: int) -> int | None:
        self.calls += 1
        return None


def test_unknown_min_cacheable_counts_as_zero_and_is_memoized() -> None:
    pricer = CountingPricer()
    reqs = [req("L", i, 10 * i, usage(r=100 * i, w5=100)) for i in range(50)]
    ts = classify_transitions(lane(reqs), pricer=pricer, rules=RULES)
    assert all(t.predicted_hit for t in ts)
    assert pricer.calls == 1


# ---------------------------------------------------------------------------------------------
# skipped requests and edge cases
# ---------------------------------------------------------------------------------------------


def test_output_residual_only_request_is_skipped() -> None:
    residual = req("L", 1, 20, usage(o=900), kind=InferenceKind.OUTPUT_RESIDUAL)
    reqs = [req("L", 0, 0, usage(w5=100_000)), residual, req("L", 2, 40, usage(r=100_000, w5=5))]
    ts = classify(lane(reqs))
    assert len(ts) == 1
    t = ts[0]
    assert t.index == 1 and t.request_id == reqs[2].request_id and t.gap_ms == 40_000


def test_short_lanes_have_no_transitions() -> None:
    assert classify(lane([])) == []
    assert classify(lane([req("L", 0, 0, usage(w5=10))])) == []


def test_writes_of_skipped_requests_still_set_ttl() -> None:
    compaction_only = req("L", 1, 10, usage(w1=5000), kind=InferenceKind.COMPACTION)
    reqs = [req("L", 0, 0, usage(u=100_000)), compaction_only, req("L", 2, 400, usage(u=100_000))]
    (t,) = classify(lane(reqs))
    assert t.ttl_s == 3600


def test_linear_on_long_lanes() -> None:
    reqs = [req("L", i, i, usage(u=50_000)) for i in range(20_000)]
    long_lane = lane(reqs)
    start = time.perf_counter()
    ts = classify(long_lane)
    assert len(ts) == 19_999
    assert time.perf_counter() - start < 20   # an O(n²) τ scan would take minutes


# ---------------------------------------------------------------------------------------------
# static-prefix floor
# ---------------------------------------------------------------------------------------------


def test_static_prefix_floor_needs_five_lanes() -> None:
    reads = [("ws:a", "m", r) for r in (10, 20, 30, 40)]
    assert static_prefix_floor(reads) == {}
    assert static_prefix_floor([*reads, ("ws:a", "m", 50)]) == {("ws:a", "m"): 30}
    assert static_prefix_floor([*reads, ("ws:a", "m", 0), ("ws:a", "m", 0)]) == {}  # R = 0 ignored
    even = [("ws:a", "m", r) for r in (10, 20, 30, 40, 50, 61)]
    assert static_prefix_floor(even) == {("ws:a", "m"): 35}
    assert static_prefix_floor(reads, min_lanes=4) == {("ws:a", "m"): 25}
    mixed = reads + [("ws:b", "m", 1)] * 5
    assert static_prefix_floor(mixed) == {("ws:b", "m"): 1}


def test_lane_first_reads_of() -> None:
    residual = req("A", 0, 0, usage(o=9), kind=InferenceKind.OUTPUT_RESIDUAL)
    a = lane([residual, req("A", 1, 5, usage(r=700, w5=10))], lane_key="A", scope="ws:x")
    b = lane([req("B", 0, 0, usage(r=0, w5=10), "claude-sonnet-5")], lane_key="B")
    c = lane([req("C", 0, 0, usage(o=9), kind=InferenceKind.OUTPUT_RESIDUAL)], lane_key="C")
    assert list(lane_first_reads_of([a, b, c])) == [("ws:x", "claude-opus-5-5", 700),
                                                    ("ws:test", "claude-sonnet-5", 0)]


# ---------------------------------------------------------------------------------------------
# properties over random lanes
# ---------------------------------------------------------------------------------------------

row = st.tuples(st.integers(0, 900), st.integers(0, 60_000), st.integers(0, 60_000),
                st.integers(0, 60_000), st.integers(0, 60_000),
                st.sampled_from(["claude-opus-5-5", "claude-sonnet-5"]),
                st.sampled_from([InferenceKind.MESSAGE, InferenceKind.OUTPUT_RESIDUAL,
                                 InferenceKind.COMPACTION]),
                st.sampled_from([None, "5m", "1h"]))


@settings(max_examples=150, deadline=None)
@given(st.lists(row, max_size=12))
def test_properties(rows: list) -> None:
    reqs = []
    ts_s = 0
    for seq, (gap, r, w5, w1, u, model, kind, hint) in enumerate(rows):
        ts_s += gap
        extra = {"cache_write_unknown": w1} if hint else {}
        u_obj = usage(r=r, w5=w5, w1=0 if hint else w1, u=u, **extra)
        reqs.append(req("L", seq, ts_s, u_obj, model, kind=kind, write_ttl_hint=hint))
    lane_obj = lane(reqs)
    ts = classify(lane_obj)
    serving = [(idx, r) for idx, r in enumerate(lane_obj.requests)
               if r.serving_inference is not None]
    assert len(ts) == max(0, len(serving) - 1)
    for t, (pos, request) in zip(ts, serving[1:], strict=True):
        assert t.request_id == request.request_id
        assert t.index == serving.index((pos, request))
        assert t.is_miss_event is is_miss_event(t.missed, t.expected_reuse)
        assert (t.cause == HIT) is (not t.is_miss_event)
        assert t.cause == HIT or t.cause in CAUSES
        if t.cause == HIT:
            assert t.sub_cause is None
        assert (t.predicted_hit is None) is (t.ttl_s is None)
        expected_tau = ttl_of_last_write(lane_obj, pos)
        if expected_tau is None:
            hint = request.serving_inference.pricing.write_ttl_hint
            expected_tau = {"5m": 300, "1h": 3600}.get(hint or "")
        assert t.ttl_s == expected_tau
        assert 0 <= t.missed <= t.expected_reuse <= t.total
    # determinism
    assert classify(lane_obj) == ts
