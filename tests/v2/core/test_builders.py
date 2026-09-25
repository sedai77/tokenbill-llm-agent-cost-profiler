"""SPEC §3.25 builders, the content canary and FlatRates (per-line exactness of SPEC §6.3)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core.builders import (
    CANARY,
    CANARY_EMAIL,
    FlatRates,
    assert_no_canary,
    lane_from_table,
    make_aggregate,
    make_attempt,
    make_block,
    make_cost_line,
    make_ctx,
    make_fingerprint,
    make_inference,
    make_lane,
    make_request,
    make_usage,
    plant_canary,
    unit_rates_from,
)
from tokenbill.core.labels import Basis, Calibration, Evidence
from tokenbill.core.protocols import Pricer
from tokenbill.core.records import (
    Attribution,
    InferenceKind,
    LaneKind,
    UsageBuckets,
    UsageSource,
    to_json,
)
from tokenbill.core.types import ResolvedRates

FR = FlatRates()
CTX = make_ctx()


def test_canary_helpers() -> None:
    assert CANARY == "TB-CANARY-7f3a91"
    assert CANARY_EMAIL == "canary.TB-CANARY-7f3a91@example.com"
    line = {
        "type": "assistant",
        "cwd": "/home/dev/proj",
        "message": {
            "id": "msg_1",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "tool_use", "input": {"cmd": "ls"}},
            ],
        },
        "n": 3,
    }
    planted = plant_canary(line)
    assert planted["type"] == "assistant" and planted["message"]["id"] == "msg_1"
    assert planted["message"]["content"][0]["type"] == "text"
    assert planted["cwd"].endswith(CANARY) and planted["message"]["content"][0]["text"].endswith(
        CANARY
    )
    assert planted["message"]["content"][1]["input"]["cmd"].endswith(CANARY)
    assert planted["n"] == 3 and line["cwd"] == "/home/dev/proj"  # the input is not mutated
    assert plant_canary("text") == f"text {CANARY}"
    assert plant_canary({"custom": "x", "other": "y"}, keys=["custom"]) == {
        "custom": f"x {CANARY}",
        "other": "y",
    }
    assert_no_canary(b"clean", "clean", bytearray(b"also clean"))
    with pytest.raises(AssertionError):
        assert_no_canary("ok", f"leak {CANARY}")
    with pytest.raises(AssertionError):
        assert_no_canary(CANARY.encode())


def test_record_builders_produce_valid_records() -> None:
    assert make_usage(output=3) == UsageBuckets(output=3)
    assert (
        make_ctx("gpt-5.6-sol").provider == "openai"
        and make_ctx("gpt-5.6-sol").channel == "openai_api"
    )
    assert make_ctx(channel="bedrock", endpoint_scope="global").channel == "bedrock"
    inf = make_inference({"output": 2}, kind="compaction")
    assert inf.kind is InferenceKind.COMPACTION and inf.inference_id.startswith("inf_")
    assert make_inference({"output": 2}).inference_id == make_inference({"output": 2}).inference_id
    att = make_attempt([inf], ts_ms=5, retry_layer="sdk", thinking_dropped=1)
    assert (
        att.retry_layer == "sdk" and att.thinking_dropped == 1 and att.attempt_id.startswith("at_")
    )
    req = make_request(
        "L",
        3,
        42_000,
        {"cache_read": 5, "output": 1},
        model="claude-sonnet-5",
        billing_path="subscription",
        message_id="msg_9",
        stop_reason="end_turn",
    )
    assert req.request_id == make_request("L", 3, 0).request_id  # id from (lane, seq)
    assert req.attribution.billing_path == "subscription"
    assert req.serving_inference.pricing.billing_path == "subscription"
    assert req.attempts[0].provider_message_id == "msg_9" and req.model == "claude-sonnet-5"
    req2 = make_request("L", 0, 0, attribution={"team": "pay"})
    assert req2.attribution == Attribution(team="pay")
    req3 = make_request("L", 0, 0, attribution=Attribution(team="x"))
    assert req3.attribution.team == "x"
    lane = make_lane([req], kind="subagent", scope="ws:x")
    assert (
        lane.kind is LaneKind.SUBAGENT and lane.cache_scope_key == "ws:x" and lane.lane_key == "L"
    )
    empty = make_lane([])
    assert empty.lane_key == "lane-0" and empty.session_key == "s_test"
    blk = make_block("h1", tier="tools", kind="tool_def")
    assert blk.h_sorted == "h1" and blk.h_norm == "h1"
    fp = make_fingerprint(
        [
            make_block("a", tier="tools", kind="tool_def"),
            make_block("b", tier="system", kind="system_text"),
            make_block("c"),
        ]
    )
    assert fp.tier_end == (1, 2, 3)
    assert make_fingerprint([make_block("c")]).tier_end == (0, 0, 1)
    agg = make_aggregate({"cache_read": 7}, dims={"model": "m", "channel": "c"})
    assert agg.dims == (("channel", "c"), ("model", "m")) and agg.agg_id.startswith("ag_")
    assert make_aggregate().dims == (("channel", "anthropic_api"), ("model", "claude-opus-5-5"))
    line = make_cost_line(5, token_type="cache_read", list_amount_nano=9)
    assert line.description == "claude-opus-5-5 cache_read" and line.list_amount_nano == 9
    for obj in (inf, att, req, lane, blk, fp, agg, line):
        assert to_json(obj)


def test_lane_from_table() -> None:
    lane = lane_from_table(
        [(0, 0, 100_000, 0, 0, 500), (420, 0, 102_000, 0, 0, 500)],
        attribution={"agent_product": "claude_code"},
        channel="anthropic_api",
    )
    assert lane.requests[0].attribution.agent_product == "claude_code"
    assert [r.ts_start_ms for r in lane.requests] == [0, 420_000]
    u = lane.requests[1].serving_inference.usage
    assert (u.cache_write_5m, u.output) == (102_000, 500)
    assert (
        lane.ttl_observed == "5m"
        and lane.kind is LaneKind.MAIN
        and lane.cache_scope_key == "ws:test"
    )
    one_h = lane_from_table(
        [(0, 0, 0, 10, 0, 1)], lane_key="X", kind="api_run", scope="org:bedrock:1"
    )
    assert one_h.ttl_observed == "1h" and one_h.lane_key == "X" and one_h.kind is LaneKind.API_RUN


# ---------- FlatRates ----------


def test_flat_rates_resolution_and_unit_rates() -> None:
    assert isinstance(FR, Pricer)
    assert FR.rate_card_sha256 == "flat" and FR.basis is Basis.LIST
    r = FR.resolve(CTX, ts_ms=0)
    assert (
        r.input,
        r.output,
        r.cache_read,
        r.cache_write_5m,
        r.cache_write_1h,
        r.cache_write_other,
    ) == (1, 5, Decimal("0.1"), Decimal("1.25"), 2, Decimal("1.25"))
    assert FR.resolve(make_ctx(""), ts_ms=0) is None
    unit = FR.unit_rates(CTX, ts_ms=0)
    assert unit.scale_exp == 8
    assert (
        unit.uncached,
        unit.cache_read,
        unit.cache_write_5m,
        unit.cache_write_1h,
        unit.cache_write_other,
        unit.output,
        unit.web_search_nano,
    ) == (100, 10, 125, 200, 125, 500, 10_000_000)
    assert FR.unit_rates(make_ctx(""), ts_ms=0) is None
    assert FR.min_cacheable_tokens(CTX, ts_ms=0) == 1024
    assert FR.min_cacheable_tokens(make_ctx(""), ts_ms=0) is None
    assert FR.supports(CTX, "keepalive", ts_ms=0) and not FR.supports(CTX, "fast_mode", ts_ms=0)
    assert FR.tokenizer_family(CTX, ts_ms=0) == "claude-4.7+"
    assert FR.tokenizer_family(make_ctx(""), ts_ms=0) is None


def test_unit_rates_from_limits() -> None:
    base = FR.resolve(CTX, ts_ms=0)
    odd = ResolvedRates(
        **{**{f: getattr(base, f) for f in base.__slots__}, "input": Decimal("1E-20")}
    )
    assert unit_rates_from(odd) is None  # scale would exceed 24
    fine = ResolvedRates(
        **{**{f: getattr(base, f) for f in base.__slots__}, "output": Decimal("21.3425")}
    )
    assert unit_rates_from(fine).scale_exp == 10  # $25 × 0.8537 → s = 10 (SPEC §6.4)
    none_rates = ResolvedRates(
        **{
            **{f: getattr(base, f) for f in base.__slots__},
            "cache_read": None,
            "cache_write_other": None,
        }
    )
    assert unit_rates_from(none_rates).cache_read == 0


def test_price_usage_exact_lines() -> None:
    usage = make_usage(
        uncached_input=1000,
        cache_read=100_000,
        cache_write_5m=2000,
        cache_write_1h=3000,
        cache_write_other=10,
        cache_write_other_ttl_s=1800,
        output=500,
        web_search_requests=3,
        web_fetch_requests=2,
    )
    p = FR.price_usage(usage, CTX, ts_ms=0)
    got = {
        ln.bucket: (ln.quantity, ln.amount_nano, ln.exact, ln.unit_usd_per_mtok) for ln in p.lines
    }
    assert got == {
        "uncached_input": (1000, 1_000_000, True, "1"),
        "cache_read": (100_000, 10_000_000, True, "0.1"),
        "cache_write_5m": (2000, 2_500_000, True, "1.25"),
        "cache_write_1h": (3000, 6_000_000, True, "2"),
        "cache_write_other": (10, 12_500, True, "1.25"),
        "output": (500, 2_500_000, True, "5"),
        "web_search": (3, 30_000_000, True, "0.01"),
    }
    assert p.figure.evidence is Evidence.EXACT and p.figure.nano == p.exact_nano == 52_012_500
    assert p.estimated is None and p.unpriced_reason is None and p.figure.is_billed_eligible
    assert all(ln.rate_row_id == "flat" and ln.layer == "builtin" for ln in p.lines)
    assert FR.price_usage(UsageBuckets(), CTX, ts_ms=0).figure.nano == 0


def test_price_usage_range_rules() -> None:
    unknown = make_usage(uncached_input=1000, cache_write_unknown=1_000_000, output=500)
    p = FR.price_usage(unknown, CTX, ts_ms=0)
    assert p.exact_nano == 3_500_000
    assert (p.estimated.nano, p.estimated.low_nano, p.estimated.high_nano) == (
        1_250_000_000,
        1_250_000_000,
        2_000_000_000,
    )
    assert p.figure.evidence is Evidence.ESTIMATED and not p.figure.is_billed_eligible
    assert p.figure.low_nano == 1_253_500_000 and p.figure.high_nano == 2_003_500_000
    hinted = FR.price_usage(unknown, make_ctx(write_ttl_hint="1h"), ts_ms=0)
    assert hinted.estimated.nano == 2_000_000_000  # point uses the hint (R5)

    mso = FR.price_usage(
        make_usage(cache_read=100_000, output=3),
        CTX,
        ts_ms=0,
        usage_source=UsageSource.MESSAGE_START_ONLY,
        output_upper=403,
    )
    assert mso.exact_nano == 10_000_000  # input lines stay exact
    (out_line,) = [ln for ln in mso.lines if ln.bucket == "output"]
    assert (out_line.amount_nano, out_line.low_nano, out_line.high_nano, out_line.exact) == (
        15_000,
        15_000,
        2_015_000,
        False,
    )
    placeholder_zero = FR.price_usage(
        make_usage(cache_read=1),
        CTX,
        ts_ms=0,
        usage_source=UsageSource.MESSAGE_START_ONLY,
        output_upper=10,
    )
    assert placeholder_zero.estimated.high_nano == 50_000

    declined = FR.price_usage(
        make_usage(uncached_input=1000, output=6), CTX, ts_ms=0, billable=False
    )
    assert declined.figure.nano == 0 and declined.figure.evidence is Evidence.EXACT
    assert (
        all(ln.amount_nano == 0 and ln.exact for ln in declined.lines) and len(declined.lines) == 2
    )

    ambiguous = FR.price_usage(
        make_usage(uncached_input=1000, output=6), CTX, ts_ms=0, billable=None
    )
    assert ambiguous.exact_nano == 0
    assert (
        ambiguous.estimated.nano,
        ambiguous.estimated.low_nano,
        ambiguous.estimated.high_nano,
    ) == (1_030_000, 0, 1_030_000)
    assert "billing uncertain" in ambiguous.figure.note

    est = FR.price_usage(
        make_usage(uncached_input=1000), CTX, ts_ms=0, usage_source=UsageSource.ESTIMATED
    )
    assert (est.estimated.nano, est.estimated.low_nano, est.estimated.high_nano) == (1_000_000,) * 3
    assert "reconstructed" in est.figure.note and est.figure.calibration is Calibration.NA

    partial = FR.price_usage(
        make_usage(uncached_input=1000), CTX, ts_ms=0, usage_source=UsageSource.PARTIAL_STREAM
    )
    assert (partial.estimated.low_nano, partial.estimated.high_nano) == (0, 1_000_000)


def test_price_usage_bases_and_unpriced() -> None:
    usage = make_usage(uncached_input=1000, output=500)
    sub = FR.price_usage(usage, make_ctx(billing_path="subscription"), ts_ms=0)
    assert sub.figure.basis is Basis.LIST_EQUIVALENT and not sub.figure.is_billed_eligible
    assert sub.figure.nano == FR.price_usage(usage, CTX, ts_ms=0).figure.nano
    un = FR.price_usage(usage, make_ctx(""), ts_ms=0)
    assert un.figure.nano is None and un.unpriced_reason == "not priceable" and un.lines == ()
    assert un.figure.note.startswith("unpriced:")
    inf = make_inference(
        make_usage(output=3), usage_source="message_start_only", output_upper=5, inference_id="i1"
    )
    priced = FR.price_inference(inf, ts_ms=0)
    assert priced.inference_id == "i1" and priced.estimated.high_nano == 25_000


usages = st.builds(
    UsageBuckets,
    uncached_input=st.integers(0, 10**9),
    cache_read=st.integers(0, 10**9),
    cache_write_5m=st.integers(0, 10**8),
    cache_write_1h=st.integers(0, 10**8),
    output=st.integers(0, 10**8),
    web_search_requests=st.integers(0, 100),
)


@given(usages)
@settings(max_examples=200, deadline=None)
def test_unit_rates_agree_with_price_usage(usage: UsageBuckets) -> None:
    unit = FR.unit_rates(CTX, ts_ms=0)
    priced = FR.price_usage(usage, CTX, ts_ms=0)
    total = sum(unit.bucket_nano(ln.bucket, ln.quantity) for ln in priced.lines)
    assert total == priced.figure.nano
    for ln in priced.lines:
        assert unit.bucket_nano(ln.bucket, ln.quantity) == ln.amount_nano
