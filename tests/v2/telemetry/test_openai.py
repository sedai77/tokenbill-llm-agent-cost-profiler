"""SPEC §5.10 / D43: the OpenAI Responses / Chat Completions adapter (served tier, truncation,
``prompt_cache_diagnostics``, Azure subscription scope)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.v2.telemetry import helpers as h
from tokenbill.adapters.openai import OpenAIUsageAdapter, canonical_diagnostic
from tokenbill.core.ids import pseudonym
from tokenbill.core.records import DIAG_REASONS, Attribution, LaneKind, Outcome
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import IngestResult

ADAPTER = OpenAIUsageAdapter()
S0 = h.T0 // 1000


def read(path: Path, **kw: Any) -> IngestResult:
    return ADAPTER.read(path, h.central(**kw))


def by_id(result: IngestResult) -> dict[str, Any]:
    return {r.final_attempt.provider_request_id: r for r in result.requests}


def ctx_of(req: Any) -> Any:
    return req.final_attempt.inferences[0].pricing


def lanes(result: IngestResult) -> dict[str, Any]:
    return {lane.lane_key: lane for s in result.sessions for lane in s.lanes}


def response(rid: str, created: int = S0, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"id": rid, "object": "response", "created_at": created,
                            "status": "completed", "model": "gpt-5.6-sol",
                            "usage": {"input_tokens": 100, "output_tokens": 10,
                                      "input_tokens_details": {"cached_tokens": 0}}}
    base.update(kw)
    return base


@pytest.fixture(scope="module")
def fixture() -> IngestResult:
    return read(h.OPENAI)


def test_fixture_requests_and_quarantine(fixture: IngestResult) -> None:
    assert len(fixture.requests) == 6
    assert [(q.locator, q.reason) for q in fixture.quarantined] == [("line:6", "bad_usage")]
    codes = {n.code: n.count for n in fixture.notes}
    assert codes["dq.sum_check_failed"] == 1 and codes["dq.quarantined"] == 1
    assert h.CANARY not in h.blob(fixture)


def test_responses_convention_buckets(fixture: IngestResult) -> None:
    req = by_id(fixture)["resp_1"]
    att = req.final_attempt
    usage = att.inferences[0].usage
    assert (usage.uncached_input, usage.cache_read, usage.cache_write_other,
            usage.cache_write_other_ttl_s, usage.output, usage.output_reasoning) == (
        2_000, 6_000, 2_000, 1_800, 500, 100)
    assert att.convention_id == "openai.responses"
    assert req.ts_start_ms == h.T0
    assert req.params.effort == "medium" and req.params.max_tokens == 8_192
    chat = by_id(fixture)["chatcmpl-1"].final_attempt
    assert chat.convention_id == "openai.chat"
    cu = chat.inferences[0].usage
    assert (cu.uncached_input, cu.cache_read, cu.output, cu.output_reasoning) == (976, 1_024, 300,
                                                                                    64)


def test_served_service_tier(fixture: IngestResult) -> None:
    tiers = {rid: ctx_of(r).service_tier for rid, r in by_id(fixture).items()}
    assert tiers == {"resp_1": "flex", "resp_2": "priority", "chatcmpl-1": "standard",
                     "resp_az_1": "standard", "chatcmpl-batch-1": "batch",
                     "resp_3": "standard"}


def test_truncated_response_is_max_tokens(fixture: IngestResult) -> None:
    stops = {rid: r.final_attempt.stop_reason for rid, r in by_id(fixture).items()}
    assert stops["resp_2"] == "max_tokens"          # incomplete_details.reason max_output_tokens
    assert stops["chatcmpl-batch-1"] == "max_tokens"  # finish_reason length
    assert stops["chatcmpl-1"] == "stop" and stops["resp_1"] is None


def test_prompt_cache_diagnostics(fixture: IngestResult) -> None:
    diag = by_id(fixture)["resp_1"].final_attempt.diagnostics
    assert (diag.reason, diag.provider_reason, diag.missed_input_tokens_estimate, diag.source) == (
        "tools_changed", "tools_changed", 5_629, "openai.prompt_cache_diagnostics")
    missing = by_id(fixture)["resp_3"].final_attempt.diagnostics
    assert (missing.reason, missing.provider_reason) == ("previous_message_not_found",
                                                         "comparison_response_not_found")
    assert "diagnostics" in fixture.capabilities


def test_cache_missed_tokens_are_never_priced(fixture: IngestResult) -> None:
    pricer = FakePricer()
    req = by_id(fixture)["resp_1"]
    inf = req.final_attempt.inferences[0]
    priced = pricer.price_inference(inf, ts_ms=req.ts_start_ms)
    plain = pricer.price_usage(inf.usage, inf.pricing, ts_ms=req.ts_start_ms)
    assert priced.figure == plain.figure and priced.lines == plain.lines
    assert priced.figure.nano is not None  # gpt-5.6-sol is priced; 5,629 missed tokens are not


@pytest.mark.parametrize("reason, canonical", [
    ("model_changed", "model_changed"), ("model", "model_changed"),
    ("tools_changed", "tools_changed"), ("tools", "tools_changed"),
    ("input_changed", "messages_changed"), ("input", "messages_changed"),
    ("reasoning_effort_changed", "param_changed"), ("reasoning_effort", "param_changed"),
    ("text_format_changed", "param_changed"), ("text_format", "param_changed"),
    ("verbosity_changed", "param_changed"), ("verbosity", "param_changed"),
    ("service_tier_changed", "param_changed"), ("service_tier", "param_changed"),
    ("prompt_cache_key_changed", "key_changed"), ("prompt_cache_key", "key_changed"),
    ("context_compacted", "compacted"), ("brand_new_reason", "unavailable"),
])
def test_each_diagnostics_reason_maps_to_its_canonical_reason(reason: str,
                                                              canonical: str) -> None:
    diag = canonical_diagnostic({"type": "cache_miss", "reason": reason,
                                 "cache_missed_tokens": 42, "comparison_reusable_tokens": 42})
    assert diag is not None
    assert (diag.reason, diag.provider_reason, diag.missed_input_tokens_estimate) == (
        canonical, reason, 42)
    assert diag.reason in DIAG_REASONS


@pytest.mark.parametrize("payload, expected", [
    ({"type": "comparison_response_not_found"}, ("previous_message_not_found",
                                                 "comparison_response_not_found")),
    ({"type": "unavailable", "cache_missed_tokens": "12"}, ("unavailable", "unavailable")),
    ({"type": "cache_miss"}, ("unavailable", "cache_miss")),
    ({"type": "cache_miss", "reason": "has spaces"}, ("unavailable", "cache_miss")),
    ({"type": "cache_hit"}, None), ({"type": "novel"}, None), ({"type": 3}, None),
    ("cache_miss", None),
])
def test_diagnostic_types(payload: Any, expected: tuple[str, str] | None) -> None:
    diag = canonical_diagnostic(payload)
    assert (None if diag is None else (diag.reason, diag.provider_reason)) == expected


def test_azure_channel_uses_the_subscription_scope(fixture: IngestResult) -> None:
    req = by_id(fixture)["resp_az_1"]
    ctx = ctx_of(req)
    assert (ctx.channel, ctx.billing_path, ctx.provider) == ("azure_openai", "azure_openai",
                                                             "openai")
    lane = lanes(fixture)[req.lane_key]
    assert lane.cache_scope_key == "sub:" + pseudonym(h.NAME_KEY, "h", "sub-0000-azure")
    assert "sub-0000-azure" not in h.blob(fixture)
    assert req.attribution.team == "search"
    assert req.attribution.principal == h.p_of(h.CANARY_EMAIL.lower())
    assert req.ts_start_ms == h.T0 + 600_000


def test_previous_response_chain_is_one_lane(fixture: IngestResult) -> None:
    ids = by_id(fixture)
    chain = {ids[r].lane_key for r in ("resp_1", "resp_2", "resp_3")}
    assert len(chain) == 1
    lane = lanes(fixture)[chain.pop()]
    assert lane.lane_exact and lane.kind is LaneKind.API_RUN
    assert [ids[r].seq for r in ("resp_1", "resp_2", "resp_3")] == [0, 1, 2]
    assert not lanes(fixture)[ids["chatcmpl-1"].lane_key].lane_exact
    assert lanes(fixture)[ids["resp_1"].lane_key].cache_scope_key == "org:openai_api:unknown"


def test_chain_is_rooted_even_when_the_parent_comes_later(tmp_path: Path) -> None:
    path = h.write_lines(tmp_path / "o.jsonl", [
        response("child", S0 + 10, previous_response_id="root"),
        response("root", S0),
        response("grandchild", S0 + 20, previous_response_id="child"),
        response("conv-a", S0 + 30, conversation={"id": "conv_1"}),
        response("conv-b", S0 + 40, conversation="conv_1"),
    ])
    result = read(path)
    ids = by_id(result)
    assert ids["child"].lane_key == ids["root"].lane_key == ids["grandchild"].lane_key
    assert ids["conv-a"].lane_key == ids["conv-b"].lane_key != ids["root"].lane_key
    assert all(lane.lane_exact for lane in lanes(result).values())
    assert "lanes_exact" in result.capabilities


def test_request_meta_overrides(tmp_path: Path) -> None:
    meta = {"ts_ms": h.T0 + 5, "session": "s1", "lane": "l1", "channel": "openai_api",
            "endpoint_scope": "regional", "billing_path": "api_key", "model_raw": "gpt-5.6-sol",
            "duration_ms": 900, "ttft_ms": 120, "account": "org-123",
            "attribution": {"team": "data", "workload_class": "ci", "api_key_id": "key-9",
                            "extra": {"gateway": "litellm", "bogus": "x"}, "nickname": "y"}}
    path = h.write_lines(tmp_path / "o.jsonl", [
        {"request_meta": meta, "response": response("r1", model="gpt-5.6-sol-2026-07-09")},
        {"request_meta": {**meta, "ts_ms": h.T0 + 50}, "response": response("r2")},
    ])
    result = read(path)
    req = by_id(result)["r1"]
    ctx = ctx_of(req)
    assert (ctx.endpoint_scope, ctx.billing_path, ctx.model_raw) == ("regional", "api_key",
                                                                     "gpt-5.6-sol")
    assert req.ts_start_ms == h.T0 + 5
    assert (req.final_attempt.duration_ms, req.final_attempt.ttft_ms) == (900, 120)
    assert req.attribution.team == "data" and req.attribution.workload_class.value == "ci"
    assert req.attribution.api_key_id == pseudonym(h.NAME_KEY, "h", "key-9")
    assert dict(req.attribution.extra) == {"gateway": "litellm"}
    assert req.final_attempt.model_served == "gpt-5.6-sol-2026-07-09"
    assert by_id(result)["r2"].lane_key == req.lane_key
    assert lanes(result)[req.lane_key].cache_scope_key == "org:openai_api:" + pseudonym(
        h.NAME_KEY, "h", "org-123")
    notes = {n.code: n.count for n in result.notes}
    assert notes["dq.unknown_fields"] == 4  # bogus extra key + nickname, twice
    assert {"workload", "ttft"} <= result.capabilities


def test_failed_cancelled_batch_error_and_quarantines(tmp_path: Path) -> None:
    path = h.write_lines(tmp_path / "o.jsonl", [
        response("failed", status="failed", usage=None,
                 error={"code": "rate_limit_exceeded", "message": h.CANARY}),
        response("odd-failure", status="failed", usage=None, error={"code": 7}),
        response("cancelled", status="cancelled", usage=None),
        {"custom_id": "x", "response": {"status_code": 500, "request_id": "req_b",
                                        "body": {"object": "chat.completion", "created": S0,
                                                 "model": "gpt-5.6-sol"}}},
        response("no-usage", usage=None),
        response("no-time", created_at=None),
        {"object": "chat.completion", "model": "gpt-4o"},
        {"custom_id": "y", "response": {"status_code": 200, "body": "oops"}},
        {"request_meta": {"ts_ms": h.T0}, "response": [1]},
        {"id": "inferred", "created": S0, "usage": {"prompt_tokens": 5, "completion_tokens": 1}},
        {"id": "inferred-r", "created_at": S0, "usage": {"input_tokens": 5}},
        {"id": "unknown", "created_at": S0, "usage": {"weird": 1}},
        response("tierless", service_tier="scale", model="mystery-model",
                 prompt_cache_diagnostics={"type": "novel"}),
        response("fractional", created_at=S0 + 0.25),
    ])
    result = read(path)
    ids = by_id(result)
    assert (ids["failed"].final_attempt.outcome, ids["failed"].final_attempt.error_type) == (
        Outcome.HTTP_ERROR, "rate_limit")
    assert ids["failed"].final_attempt.inferences == ()
    assert ids["odd-failure"].final_attempt.error_type == "connection"
    assert ids["cancelled"].final_attempt.outcome is Outcome.ABORTED
    batch = ids["req_b"].final_attempt
    assert (batch.outcome, batch.http_status, batch.error_type) == (Outcome.HTTP_ERROR, 500,
                                                                    "other")
    assert ids["inferred"].final_attempt.convention_id == "openai.chat"
    assert ids["inferred-r"].final_attempt.convention_id == "openai.responses"
    assert ctx_of(ids["tierless"]).service_tier == "unknown"
    assert ctx_of(ids["tierless"]).model == "mystery-model"
    assert ids["fractional"].ts_start_ms == h.T0 + 250
    assert sorted(q.reason for q in result.quarantined) == sorted([
        "missing:usage", "missing:created_at", "missing:created", "missing:response",
        "missing:response", "missing:object"])
    assert {n.code for n in result.notes} >= {"dq.unknown_fields", "dq.quarantined"}
    assert "attempts" in result.capabilities
    assert h.CANARY not in h.blob(result)


def test_window_and_unpriced_model(tmp_path: Path) -> None:
    path = h.write_lines(tmp_path / "o.jsonl", [response("a", S0), response("b", S0 + 100,
                                                                            model="")])
    result = read(path, since_ms=h.T0 + 1)
    assert list(by_id(result)) == ["b"] and result.stats["outside_window"] == 1
    assert any(n.code == "dq.unpriced_model" for n in result.notes)
    assert read(path, attribution=Attribution(billing_path="usage_credits")).requests[
        0].final_attempt.inferences[0].pricing.billing_path == "usage_credits"


@pytest.mark.parametrize("path, expected", [
    (h.OPENAI, True), (h.OTLP_CC, False), (h.BEDROCK, False), (h.ANTHROPIC, False)])
def test_sniff(path: Path, expected: bool) -> None:
    assert ADAPTER.sniff(path, path.read_bytes()[:65536]) is expected


def test_sniff_pairs_batch_lines_and_huge_heads() -> None:
    assert ADAPTER.sniff(Path("x"), b'{"request_meta": {}, "response": {"object": "response"}}\n')
    assert ADAPTER.sniff(Path("x"), b'{"custom_id": "a", "response": {"status_code": 200, '
                                    b'"body": {"object": "chat.completion"}}}\n')
    assert ADAPTER.sniff(Path("x"), b'{"id": "resp_1", "object": "response", "output": "'
                         + b"x" * 70_000)
    assert not ADAPTER.sniff(Path("x"), b'{"id": "x", "output": "' + b"x" * 70_000)
    assert not ADAPTER.sniff(Path("x"), b"garbage")
