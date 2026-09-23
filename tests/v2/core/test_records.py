"""SPEC §3.2 records: validation, enum formatting, bucket arithmetic, JSON round trip."""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.core import records as r
from tokenbill.core.builders import (
    make_attempt,
    make_inference,
    make_lane,
    make_request,
    make_usage,
)
from tokenbill.core.errors import ContractViolation
from tokenbill.core.labels import Basis

from .strategies import RECORD_STRATEGIES, TYPE_STRATEGIES

MAX = r.MAX_TOKENS
FAST = settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])


# ---------- enums ----------


def test_enum_format_is_value_on_every_python() -> None:
    assert f"{r.LaneKind.MAIN}" == "main"
    assert str(Basis.LIST) == "list"
    assert format(r.UsageSource.FINAL, ">7") == "  final"
    assert f"{r.ContentTier.NONE!s}-{r.Outcome.OK}" == "none-ok"
    assert r.LaneKind("subagent") is r.LaneKind.SUBAGENT
    assert r.LaneKind.MAIN == "main"


def test_enum_values_match_spec() -> None:
    assert [k.value for k in r.LaneKind] == [
        "main",
        "subagent",
        "workflow_agent",
        "helper",
        "compaction",
        "api_run",
        "unknown",
    ]
    assert [k.value for k in r.InferenceKind] == [
        "message",
        "compaction",
        "advisor",
        "fallback_declined",
        "fallback",
        "keepalive",
        "output_residual",
        "other",
    ]
    assert [k.value for k in r.UsageSource] == [
        "final",
        "message_start_only",
        "partial_stream",
        "estimated",
        "provider_rollup",
    ]
    assert [k.value for k in r.Outcome] == [
        "ok",
        "http_error",
        "aborted",
        "timeout",
        "refused",
        "network_error",
        "unknown",
    ]
    assert [k.value for k in r.WorkloadClass] == [
        "interactive",
        "ci",
        "scheduled",
        "eval",
        "batch",
        "service",
        "unknown",
    ]
    assert [int(f) for f in r.Fidelity] == [0, 1, 2, 3]
    assert [k.value for k in r.ContentTier] == ["none", "fingerprint", "full"]
    assert len(r.LaneEventKind) == 13 and set(r.EVENT_ATTRS) == set(r.LaneEventKind)
    assert r._BASIS_VALUES == {b.value for b in Basis}


def test_billing_class() -> None:
    assert r.billing_class("subscription") == "allowance"
    for bp in ("api_key", "usage_credits", "unknown", None):
        assert r.billing_class(bp) == "billed"
    assert r.BILLING_PATHS[0] == "api_key" and "subscription" in r.BILLING_PATHS


# ---------- UsageBuckets ----------


@pytest.mark.parametrize(
    "field",
    [
        "uncached_input",
        "cache_read",
        "cache_write_5m",
        "cache_write_1h",
        "cache_write_unknown",
        "output",
        "web_search_requests",
        "web_fetch_requests",
    ],
)
def test_usage_rejects_negatives_and_overflow(field: str) -> None:
    with pytest.raises(ContractViolation):
        r.UsageBuckets(**{field: -1})
    with pytest.raises(ContractViolation):
        r.UsageBuckets(**{field: MAX + 1})
    with pytest.raises(ContractViolation):
        r.UsageBuckets(**{field: True})
    with pytest.raises(ContractViolation):
        r.UsageBuckets(**{field: 1.0})  # type: ignore[arg-type]
    assert getattr(r.UsageBuckets(**{field: MAX}), field) == MAX


def test_usage_reasoning_and_ttl_rules() -> None:
    with pytest.raises(ContractViolation):
        r.UsageBuckets(output=5, output_reasoning=6)
    with pytest.raises(ContractViolation):
        r.UsageBuckets(output=5, output_reasoning=-1)
    with pytest.raises(ContractViolation):
        r.UsageBuckets(cache_write_other=10)
    with pytest.raises(ContractViolation):
        r.UsageBuckets(cache_write_other=10, cache_write_other_ttl_s=0)
    ok = r.UsageBuckets(
        cache_write_other=10, cache_write_other_ttl_s=1800, output=5, output_reasoning=5
    )
    assert ok.cache_write == 10
    assert (
        r.UsageBuckets(cache_write_other_ttl_s=1800).cache_write_other == 0
    )  # ttl alone is allowed


def test_usage_properties() -> None:
    u = r.UsageBuckets(
        uncached_input=1,
        cache_read=10,
        cache_write_5m=100,
        cache_write_1h=1000,
        cache_write_other=7,
        cache_write_other_ttl_s=1800,
        cache_write_unknown=10000,
        output=3,
    )
    assert u.cache_write == 11107
    assert u.total_input == 11118


def test_usage_add_rules() -> None:
    a = r.UsageBuckets(uncached_input=1, output=5, output_reasoning=2, web_search_requests=1)
    b = r.UsageBuckets(cache_read=2, output=7, output_reasoning=3, web_fetch_requests=2)
    s = a + b
    assert (s.uncached_input, s.cache_read, s.output, s.output_reasoning) == (1, 2, 12, 5)
    assert (s.web_search_requests, s.web_fetch_requests) == (1, 2)
    assert (a + r.UsageBuckets(output=1)).output_reasoning is None
    assert sum([a, b]) == s
    c = r.UsageBuckets(cache_write_other=1, cache_write_other_ttl_s=1800)
    assert (c + r.UsageBuckets()).cache_write_other_ttl_s == 1800
    assert (r.UsageBuckets() + c).cache_write_other_ttl_s == 1800
    with pytest.raises(ContractViolation):
        c + r.UsageBuckets(cache_write_other=1, cache_write_other_ttl_s=3600)
    with pytest.raises(ContractViolation):
        r.UsageBuckets(output=MAX) + r.UsageBuckets(output=1)
    assert a.__add__(3) is NotImplemented
    assert a.__radd__(3) is NotImplemented
    with pytest.raises(TypeError):
        a + 3  # type: ignore[operator]


@given(RECORD_STRATEGIES[r.UsageBuckets], RECORD_STRATEGIES[r.UsageBuckets])
@FAST
def test_usage_add_is_bucketwise(a: r.UsageBuckets, b: r.UsageBuckets) -> None:
    try:
        s = a + b
    except ContractViolation:
        ttl_clash = (
            a.cache_write_other_ttl_s is not None
            and b.cache_write_other_ttl_s is not None
            and a.cache_write_other_ttl_s != b.cache_write_other_ttl_s
        )
        overflow = any(getattr(a, f) + getattr(b, f) > MAX for f in r._BUCKET_COUNTS)
        assert ttl_clash or overflow
        return
    assert s.total_input == a.total_input + b.total_input
    assert s.output == a.output + b.output


# ---------- other records ----------


def _ctx(**kw: object) -> r.PricingContext:
    base = dict(
        provider="anthropic",
        channel="anthropic_api",
        model="claude-opus-5-5",
        model_raw="claude-opus-5-5",
    )
    base.update(kw)
    return r.PricingContext(**base)  # type: ignore[arg-type]


def test_pricing_context_validation() -> None:
    assert _ctx().billing_path == "unknown"
    with pytest.raises(ContractViolation):
        _ctx(billing_path="seat")
    with pytest.raises(ContractViolation):
        _ctx(endpoint_scope="planet")
    with pytest.raises(ContractViolation):
        _ctx(write_ttl_hint="30m")
    with pytest.raises(ContractViolation):
        _ctx(model=None)


def test_inference_validation_and_coercion() -> None:
    inf = r.Inference(
        "i",
        "message",
        r.UsageBuckets(output=3),
        _ctx(),
        usage_source="message_start_only",
        output_upper=10,
    )
    assert (
        inf.kind is r.InferenceKind.MESSAGE and inf.usage_source is r.UsageSource.MESSAGE_START_ONLY
    )
    with pytest.raises(ContractViolation):
        r.Inference("i", "bogus", r.UsageBuckets(), _ctx())
    with pytest.raises(ContractViolation):  # output_upper only for message_start_only
        r.Inference("i", "message", r.UsageBuckets(output=3), _ctx(), output_upper=10)
    with pytest.raises(ContractViolation):  # below logged output
        r.Inference(
            "i",
            "message",
            r.UsageBuckets(output=30),
            _ctx(),
            usage_source="message_start_only",
            output_upper=10,
        )
    with pytest.raises(ContractViolation):
        r.Inference("i", "message", r.UsageBuckets(), _ctx(), provider_reported_cost_basis="guess")
    with pytest.raises(ContractViolation):
        r.Inference("i", "message", r.UsageBuckets(), _ctx(), billable="yes")  # type: ignore[arg-type]
    with pytest.raises(ContractViolation):
        r.Inference("i", "message", {"output": 1}, _ctx())  # type: ignore[arg-type]


def test_attempt_validation() -> None:
    att = make_attempt(
        [make_inference(make_usage(output=1))], applied_edits=[["clear", 5]], retry_layer="sdk"
    )
    assert att.applied_edits == (("clear", 5),) and isinstance(att.inferences, tuple)
    with pytest.raises(ContractViolation):
        make_attempt(retry_layer="proxy")
    with pytest.raises(ContractViolation):
        make_attempt(applied_edits=(("clear", -1),))
    with pytest.raises(ContractViolation):
        make_attempt(applied_edits=(("clear",),))
    with pytest.raises(ContractViolation):
        make_attempt(raw_usage_json="x" * (8 * 1024 + 1))
    with pytest.raises(ContractViolation):
        make_attempt(raw_usage_json=7)
    assert make_attempt(raw_usage_json="é" * 2000).raw_usage_json  # 4000 bytes: fine
    with pytest.raises(ContractViolation):
        make_attempt(raw_usage_json="é" * 4097)  # 8194 bytes
    with pytest.raises(ContractViolation):
        make_attempt(outcome="exploded")
    with pytest.raises(ContractViolation):
        make_attempt(ts_ms=-1)
    with pytest.raises(ContractViolation):
        r.Attempt(
            "a",
            0,
            0,
            None,
            None,
            "ok",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            ("not an inference",),
        )  # type: ignore[arg-type]
    with pytest.raises(ContractViolation):
        r.Attempt(
            "a",
            0,
            0,
            None,
            None,
            "ok",
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            "not a tuple",
        )  # type: ignore[arg-type]


def test_small_records_validation() -> None:
    with pytest.raises(ContractViolation):
        r.CacheDiagnostic("because", "x", None, "s")
    with pytest.raises(ContractViolation):
        r.Breakpoint(0, "2h")
    with pytest.raises(ContractViolation):
        r.AppendedItem("video", None, 1)
    with pytest.raises(ContractViolation):
        r.BlockRef("h", None, None, "footer", "text", None, 1, None)
    with pytest.raises(ContractViolation):
        r.BlockRef("h", None, None, "tools", "gif", None, 1, None)
    with pytest.raises(ContractViolation):
        r.BlockRef("h", None, None, "tools", "text", None, 1, None, image_px=(1,))  # type: ignore[arg-type]
    assert r.BlockRef(
        "h", None, None, "tools", "image", None, 1, None, image_px=[2, 3]
    ).image_px == (2, 3)
    blocks = (r.BlockRef("h", None, None, "tools", "tool_def", None, 1, None),)
    assert r.ContentFingerprint("k", blocks, (1, 1, 1)).tier_end == (1, 1, 1)
    with pytest.raises(ContractViolation):
        r.ContentFingerprint("k", blocks, (1, 0, 1))
    with pytest.raises(ContractViolation):
        r.ContentFingerprint("k", blocks, (1, 1, 2))
    params = r.RequestParams("m", betas=("b", "a"))
    assert params.betas == ("a", "b")
    with pytest.raises(ContractViolation):
        r.RequestParams("m", stream="yes")  # type: ignore[arg-type]
    with pytest.raises(ContractViolation):
        r.SourceRef("a", "s", "l", 7, 1)  # type: ignore[arg-type]
    assert r.SourceRef("a", "s", "l", 3, 1).fidelity is r.Fidelity.FULL


def test_attribution_rules() -> None:
    a = r.Attribution(principal="p_" + "0" * 20, extra=[("workflow", "ci"), ("gateway", "g")])
    assert a.extra == (("gateway", "g"), ("workflow", "ci"))
    assert r.Attribution(principal="r_emp-42.x").principal == "r_emp-42.x"
    assert r.Attribution(principal="c_" + "a" * 20).principal
    for bad in ("alice@example.com", "p_short", "r_", "r_a@b", "x_" + "0" * 20, "P_" + "0" * 20):
        with pytest.raises(ContractViolation):
            r.Attribution(principal=bad)
    with pytest.raises(ContractViolation):
        r.Attribution(extra=(("color", "blue"),))
    with pytest.raises(ContractViolation):
        r.Attribution(extra=(("gateway", "x" * 129),))
    with pytest.raises(ContractViolation):
        r.Attribution(extra=(("gateway", "a"), ("gateway", "b")))
    with pytest.raises(ContractViolation):
        r.Attribution(extra=(("gateway", 1),))  # type: ignore[list-item]
    with pytest.raises(ContractViolation):
        r.Attribution(api_key_id="apikey_01abc")
    with pytest.raises(ContractViolation):
        r.Attribution(cwd_key="/home/alice/project")
    with pytest.raises(ContractViolation):
        r.Attribution(billing_path="seat")
    assert r.Attribution(workload_class="ci").workload_class is r.WorkloadClass.CI
    assert r.Attribution(extra=[]).extra == ()


def _req(seq: int, ts: int, lane: str = "L", **kw: object) -> r.Request:
    return make_request(lane, seq, ts, make_usage(cache_write_5m=10, output=1), **kw)  # type: ignore[arg-type]


def test_request_properties() -> None:
    comp = make_inference(make_usage(output=1), kind="compaction", inference_id="c")
    declined = make_inference(
        make_usage(output=0), kind="fallback_declined", billable=False, inference_id="d"
    )
    req = make_request(
        "L",
        0,
        5000,
        make_usage(output=9),
        model="claude-opus-4-8",
        extra_inferences=(comp, declined),
    )
    assert req.ts_start_ms == 5000
    assert req.final_attempt is req.attempts[-1]
    assert req.serving_inference is not None and req.serving_inference.usage.output == 9
    assert [i.inference_id for i in req.billable_inferences] == [
        "c",
        req.serving_inference.inference_id,
    ]
    assert req.model == "claude-opus-4-8"
    residual_only = replace(req, attempts=(make_attempt([make_inference(kind="output_residual")]),))
    assert residual_only.serving_inference is None
    assert residual_only.model == "claude-opus-4-8"
    unknown_model = make_request("L", 1, 1, make_usage(), model="")
    assert unknown_model.model == ""
    with pytest.raises(ContractViolation):
        replace(req, attempts=())
    with pytest.raises(ContractViolation):
        replace(req, seq=-1)
    fallback = make_inference(
        make_usage(output=2), kind="fallback", inference_id="f", model="claude-x"
    )
    req2 = replace(req, attempts=(make_attempt([fallback, comp]),))
    assert req2.serving_inference is fallback and req2.model == "claude-x"


def test_lane_sorts_and_validates() -> None:
    a, b, c = _req(0, 3000), _req(1, 1000), _req(2, 1000)
    lane = make_lane([a, b, c])
    assert [x.seq for x in lane.requests] == [1, 2, 0]
    ev1 = r.LaneEvent("L", 20, "clear")
    ev0 = r.LaneEvent("L", 10, "human_prompt")
    lane = make_lane([a], events=[ev1, ev0])
    assert lane.events == (ev0, ev1)
    with pytest.raises(ContractViolation):
        make_lane([a, _req(0, 1, lane="OTHER")], lane_key="L")
    with pytest.raises(ContractViolation):
        make_lane([a], events=[r.LaneEvent("OTHER", 1, "clear")])
    with pytest.raises(ContractViolation):
        replace(lane, ttl_observed="2h")
    assert lane.team is None and lane.billing_class == "billed"
    sub = make_request(
        "S", 0, 0, make_usage(output=1), billing_path="subscription", attribution={"team": "pay"}
    )
    assert (
        make_lane([sub]).billing_class == "allowance"
    )  # no attribution path → pricing context path
    sub2 = make_request("S", 0, 0, make_usage(output=1), billing_path="subscription")
    assert make_lane([sub2]).billing_class == "allowance"
    via_ctx = replace(sub, attribution=r.Attribution(team="pay"))
    assert make_lane([via_ctx]).team == "pay"
    assert (
        make_lane([], lane_key="E").billing_class == "billed"
        and make_lane([], lane_key="E").team is None
    )


def test_lane_event_schema() -> None:
    ev = r.LaneEvent(
        "L", 1, "compaction", [("trigger", "auto"), ("pre_tokens", 5), ("dropped_tokens", None)]
    )
    assert ev.attrs == (("dropped_tokens", None), ("pre_tokens", 5), ("trigger", "auto"))
    for bad in (
        [("color", "red")],
        [("trigger", "sometimes")],
        [("pre_tokens", "5")],
        [("pre_tokens", True)],
        [("pre_tokens", 5), ("pre_tokens", 6)],
        [("dropped_tokens", 1.5)],
    ):
        with pytest.raises(ContractViolation):
            r.LaneEvent("L", 1, "compaction", bad)
    assert r.LaneEvent("L", 1, "model_fallback", [("credited", True), ("trigger", "refusal")]).attrs
    with pytest.raises(ContractViolation):
        r.LaneEvent("L", 1, "model_fallback", [("credited", 1)])
    with pytest.raises(ContractViolation):
        r.LaneEvent("L", 1, "clear", [("x", 1)])
    with pytest.raises(ContractViolation):
        r.LaneEvent("L", 1, "cost_state", [("reported_total_nano", MAX + 1)])
    with pytest.raises(ContractViolation):
        r.LaneEvent("L", 1, "clear", "not pairs")  # type: ignore[arg-type]
    with pytest.raises(ContractViolation):
        r.LaneEvent("L", 1, "clear", [(1, "x")])  # type: ignore[list-item]
    assert r.LaneEvent(
        "L", 1, "quota_state", [("using_overage", None), ("status", "allowed")]
    ).attrs


def test_session_and_aggregates() -> None:
    lane = make_lane([_req(0, 1)], session_key="s_test")
    s = r.Session("s_test", "claude-code", r.Attribution(), (lane,), 1, 2)
    assert s.lanes == (lane,)
    with pytest.raises(ContractViolation):
        r.Session("other", "claude-code", r.Attribution(), (lane,), 1, 2)
    with pytest.raises(ContractViolation):
        r.Session("s_test", "claude-code", r.Attribution(), (lane,), 5, 2)
    agg = r.UsageAggregate(
        "a", "anthropic.usage_report", 0, 10, [("model", "m"), ("channel", "c")], r.UsageBuckets()
    )
    assert agg.dims == (("channel", "c"), ("model", "m"))
    with pytest.raises(ContractViolation):
        r.UsageAggregate("a", "k", 10, 0, (), r.UsageBuckets())
    with pytest.raises(ContractViolation):
        r.UsageAggregate("a", "k", 0, 1, (), r.UsageBuckets(), finality="maybe")
    with pytest.raises(ContractViolation):
        r.UsageAggregate("a", "k", 0, 1, (), r.UsageBuckets(), reported_cost_basis="guess")
    line = r.CostLine(
        "l",
        "aws.cur2",
        "2026-09-10",
        "bedrock",
        None,
        "d",
        None,
        None,
        None,
        None,
        None,
        None,
        "regional",
        amount_nano=-5,
    )
    assert line.amount_nano == -5
    with pytest.raises(ContractViolation):
        replace(line, date_utc="2026/09/10")
    with pytest.raises(ContractViolation):
        replace(line, principal="arn:aws:iam::1:user/alice")
    with pytest.raises(ContractViolation):
        replace(line, endpoint_scope="orbital")
    with pytest.raises(ContractViolation):
        r.OutcomeAggregate("2026-09-10", "t", -1, 0, 0, 0, 0, 0, 0, 0)


def test_usage_record_validation() -> None:
    rec = r.UsageRecord(
        "i",
        "rq",
        "at",
        "s",
        "L",
        "main",
        0,
        "2026-09-10",
        "message",
        "final",
        True,
        None,
        _ctx(),
        r.UsageBuckets(),
        r.Attribution(),
        3,
    )
    assert rec.lane_kind is r.LaneKind.MAIN and rec.fidelity is r.Fidelity.FULL
    with pytest.raises(ContractViolation):
        replace(rec, date_utc="today")


# ---------- JSON round trip ----------


@pytest.mark.parametrize("cls", list(RECORD_STRATEGIES), ids=lambda c: c.__name__)
def test_every_record_round_trips(cls: type) -> None:
    @given(RECORD_STRATEGIES[cls])
    @settings(max_examples=25, deadline=None, suppress_health_check=list(HealthCheck))
    def check(obj: object) -> None:
        doc = r.to_json(obj)
        text = json.dumps(doc, sort_keys=True, allow_nan=False)
        back = r.from_json(cls, json.loads(text))
        assert back == obj
        assert r.to_json(back) == doc

    check()


@pytest.mark.parametrize("cls", list(TYPE_STRATEGIES), ids=lambda c: c.__name__)
def test_types_round_trip(cls: type) -> None:
    @given(TYPE_STRATEGIES[cls])
    @settings(max_examples=25, deadline=None, suppress_health_check=list(HealthCheck))
    def check(obj: object) -> None:
        back = r.from_json(cls, json.loads(json.dumps(r.to_json(obj))))
        assert back == obj

    check()


def test_to_json_shapes() -> None:
    req = make_request("L", 0, 1, make_usage(output=1))
    doc = r.to_json(req)
    assert doc["attempts"][0]["inferences"][0]["kind"] == "message"
    assert doc["attempts"][0]["outcome"] == "ok"
    assert isinstance(doc["attempts"], list) and isinstance(doc["attribution"]["extra"], list)
    assert r.to_json(r.SourceRef("a", "s", "l", r.Fidelity.FULL, 1))["fidelity"] == 3
    with pytest.raises(TypeError):
        r.to_json(r.UsageBuckets)
    with pytest.raises(TypeError):
        r.to_json({"a": 1})


def test_to_json_rejects_floats_and_odd_values() -> None:
    from dataclasses import dataclass, field

    @dataclass
    class Holder:
        value: object
        mapping: dict = field(default_factory=dict)

    with pytest.raises(TypeError):
        r.to_json(Holder(1.5))
    with pytest.raises(TypeError):
        r.to_json(Holder(Decimal("NaN")))
    with pytest.raises(TypeError):
        r.to_json(Holder(1, {1: "x"}))
    assert r.to_json(Holder(frozenset({"b", "a"}), {r.LaneKind.MAIN: 1})) == {
        "value": ["a", "b"],
        "mapping": {"main": 1},
    }


def test_from_json_rejects_malformed_documents() -> None:
    good = r.to_json(r.UsageBuckets(output=1))
    with pytest.raises(ContractViolation):
        r.from_json(r.UsageBuckets, {**good, "surprise": 1})
    with pytest.raises(ContractViolation):
        r.from_json(r.UsageBuckets, {**good, "output": 1.0})
    with pytest.raises(ContractViolation):
        r.from_json(r.UsageBuckets, {**good, "output": "1"})
    with pytest.raises(ContractViolation):
        r.from_json(r.UsageBuckets, {**good, "output": -1})
    with pytest.raises(ContractViolation):
        r.from_json(r.UsageBuckets, [])  # type: ignore[arg-type]
    with pytest.raises(ContractViolation):
        r.from_json(r.PricingContext, {"provider": "anthropic"})
    ctx = r.to_json(_ctx())
    with pytest.raises(ContractViolation):
        r.from_json(r.PricingContext, {**ctx, "model": None})
    with pytest.raises(ContractViolation):
        r.from_json(r.PricingContext, {**ctx, "inference_geo": 5})
    ev = r.to_json(r.LaneEvent("L", 1, "clear"))
    with pytest.raises(ContractViolation):
        r.from_json(r.LaneEvent, {**ev, "kind": "earthquake"})
    with pytest.raises(ContractViolation):
        r.from_json(r.LaneEvent, {**ev, "attrs": "x"})
    with pytest.raises(ContractViolation):
        r.from_json(r.LaneEvent, {**ev, "attrs": [["a", "b", "c"]]})
    with pytest.raises(ContractViolation):
        r.from_json(r.LaneEvent, {**ev, "attrs": [["pre_tokens", {"x": 1}]]})
    with pytest.raises(TypeError):
        r.from_json(dict, {})  # type: ignore[arg-type]
    assert r.from_json(r.UsageBuckets, {}) == r.UsageBuckets()


def test_from_json_decimal_and_nested_types() -> None:
    from tokenbill.core.types import ContractOverlay, IngestOptions, IngestResult, SourceInfo

    overlay = ContractOverlay(
        "c", Decimal("0.85"), (("m", (("input", Decimal("2.4")),)),), "2026-01-01", None, False, ()
    )
    doc = r.to_json(overlay)
    assert doc["multiplier"] == "0.85"
    assert r.from_json(ContractOverlay, doc) == overlay
    assert r.from_json(ContractOverlay, {**doc, "multiplier": 1}).multiplier == Decimal(1)
    for bad in ("abc", "NaN", 1.5):
        with pytest.raises(ContractViolation):
            r.from_json(ContractOverlay, {**doc, "multiplier": bad})
    opts = IngestOptions(name_allowlist=frozenset({"Bash", "Read"}), team_map=(("u1", "pay"),))
    with pytest.raises(TypeError):  # key material (bytes) is never serialized
        r.to_json(opts)
    res = IngestResult(
        SourceInfo("s", "a", "h", "0" * 64, 1, None, None),
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        {"lines": 3},
        frozenset({"timing"}),
        {"m": r.UsageBuckets(output=1)},
    )
    back = r.from_json(IngestResult, json.loads(json.dumps(r.to_json(res))))
    assert back == res


def test_from_json_unsupported_annotations() -> None:
    from tokenbill.core.types import AnalysisContext

    with pytest.raises(TypeError):
        r.from_json(AnalysisContext, {})


@given(
    st.dictionaries(
        st.text(max_size=5), st.integers() | st.text(max_size=5) | st.none(), max_size=6
    )
)
@FAST
def test_from_json_fuzz_only_contract_violations(doc: dict) -> None:
    for cls in (r.UsageBuckets, r.LaneEvent, r.Attribution, r.PricingContext):
        try:
            r.from_json(cls, doc)
        except ContractViolation:
            pass
