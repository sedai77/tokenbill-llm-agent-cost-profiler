"""SPEC §5.10: the Anthropic responses / Message Batches results adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.v2.telemetry import helpers as h
from tokenbill.adapters.anthropic_responses import AnthropicResponsesAdapter
from tokenbill.core.ids import pseudonym, request_id_for
from tokenbill.core.records import Fidelity, InferenceKind, Outcome
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import IngestResult

ADAPTER = AnthropicResponsesAdapter()


def read(path: Path, **kw: Any) -> IngestResult:
    return ADAPTER.read(path, h.central(**kw))


def by_msg(result: IngestResult) -> dict[str, Any]:
    return {r.final_attempt.provider_message_id: r for r in result.requests}


def lanes(result: IngestResult) -> dict[str, Any]:
    return {lane.lane_key: lane for s in result.sessions for lane in s.lanes}


def message(mid: str, **usage: Any) -> dict[str, Any]:
    return {"id": mid, "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": h.CANARY}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5, **usage}}


def pair(ts: int, response: Any, **meta: Any) -> dict[str, Any]:
    return {"request_meta": {"ts_ms": ts, **meta}, "response": response}


@pytest.fixture(scope="module")
def fixture() -> IngestResult:
    return read(h.ANTHROPIC)


def test_fixture_requests(fixture: IngestResult) -> None:
    assert len(fixture.requests) == 5
    assert fixture.stats["batch_results_not_billed"] == 1
    assert [(q.locator, q.reason) for q in fixture.quarantined] == [
        ("line:6", "missing:request_meta.ts_ms")]
    assert h.CANARY not in h.blob(fixture)


def test_message_response_fields(fixture: IngestResult) -> None:
    req = by_msg(fixture)["msg_01"]
    att = req.final_attempt
    usage = att.inferences[0].usage
    assert (usage.uncached_input, usage.cache_write_1h, usage.output) == (20, 40_000, 900)
    assert att.convention_id == "anthropic.messages"
    assert req.request_id == request_id_for("anthropic", "msg_01", "any", "any")
    assert att.provider_request_id == "req_011CT0000000000000000001"
    ctx = att.inferences[0].pricing
    assert (ctx.provider, ctx.channel, ctx.service_tier, ctx.inference_geo) == (
        "anthropic", "anthropic_api", "standard", "global")
    diag = att.diagnostics
    assert (diag.reason, diag.provider_reason, diag.missed_input_tokens_estimate, diag.source) == (
        "tools_changed", "tools_changed", 4_000, "anthropic.cache_diagnostics")
    assert att.applied_edits == (("clear_tool_uses_20250919", 1_500),)
    assert req.source.fidelity is Fidelity.FULL and req.source.priority == 35
    a = req.attribution
    assert (a.team, a.agent_product) == ("agents", "agent_sdk")
    assert a.principal == h.p_of("user-42")
    assert a.repo == pseudonym(h.NAME_KEY, "h", "acme/api")


def test_batch_results_carry_the_batch_tier(fixture: IngestResult) -> None:
    ctx = by_msg(fixture)["msg_batch_1"].final_attempt.inferences[0].pricing
    assert ctx.service_tier == "batch"
    assert FakePricer().price_inference(by_msg(fixture)["msg_batch_1"].final_attempt.inferences[0],
                                        ts_ms=h.T0).figure.nano is not None


def test_vertex_request_meta_supplies_model_and_scope(fixture: IngestResult) -> None:
    req = by_msg(fixture)["msg_vertex_1"]
    ctx = req.final_attempt.inferences[0].pricing
    assert (ctx.channel, ctx.model, ctx.model_raw, ctx.endpoint_scope, ctx.billing_path) == (
        "vertex", "claude-opus-5-5", "claude-opus-5-5@20260901", "regional", "vertex")
    assert lanes(fixture)[req.lane_key].cache_scope_key == "org:vertex:" + pseudonym(
        h.NAME_KEY, "h", "gcp-project-1")


def test_iterations_and_refusal_rule(fixture: IngestResult) -> None:
    infs = by_msg(fixture)["msg_fallback_1"].final_attempt.inferences
    assert [i.kind for i in infs] == [InferenceKind.FALLBACK_DECLINED, InferenceKind.FALLBACK]
    assert (infs[0].billable, infs[0].billing_rule_id) == (None, "anthropic.refusal.ambiguous")
    assert infs[1].pricing.model == "claude-sonnet-5"
    assert "iterations" in fixture.capabilities


def test_error_response_is_a_failed_attempt(fixture: IngestResult) -> None:
    failed = [r for r in fixture.requests if r.final_attempt.outcome is not Outcome.OK]
    (req,) = failed
    att = req.final_attempt
    assert (att.outcome, att.http_status, att.error_type, att.inferences) == (
        Outcome.HTTP_ERROR, 529, "overloaded", ())
    assert "attempts" in fixture.capabilities


def test_lanes_from_request_meta(fixture: IngestResult) -> None:
    run = [r for r in fixture.requests if r.final_attempt.inferences
           and r.final_attempt.inferences[0].pricing.channel == "anthropic_api"]
    assert len({r.lane_key for r in run}) == 1
    assert [r.seq for r in sorted(run, key=lambda r: r.ts_start_ms)] == [0, 1, 2]
    assert all(lane.lane_exact for lane in lanes(fixture).values())
    assert {"lanes_exact", "ttl_split", "diagnostics"} <= fixture.capabilities


def test_variants(tmp_path: Path) -> None:
    path = h.write_lines(tmp_path / "a.jsonl", [
        pair(h.T0, message("m1", output_tokens=5)),
        pair(h.T0, message("m1", output_tokens=50)),       # split entry: larger output wins
        pair(h.T0, message("m1", output_tokens=7)),
        pair(h.T0 + 5, message("m2", cache_creation_input_tokens=100,
                               service_tier="priority", speed="fast"),
             channel="bedrock", model_raw="global.anthropic.claude-opus-5",
             billing_path="nonsense"),
        pair(h.T0 + 6, {**message("m3"), "model": None}),
        pair(h.T0 + 7, {"type": "message", "id": "m4"}),
        pair(h.T0 + 8, {"type": "error", "error": {"type": "brand_new_error"}}, http_status=418),
        pair(h.T0 + 9, {"type": "error"}),
        pair(h.T0 + 10, {"custom_id": "c", "result": {"type": "errored"}}),
        pair(h.T0 + 11, {**message("m5"), "diagnostics": {"cache_miss_reason": {
            "type": "novel_reason"}}}),
        pair(h.T0 + 12, {**message("m6"), "diagnostics": {"cache_miss_reason": {"type": 5}},
                         "context_management": {"applied_edits": [
                             {"type": "clear_thinking"}, "junk", {"cleared_input_tokens": 3}]}}),
        pair(h.T0 + 13, "not an object"),
        {"request_meta": {"ts": "2026-09-23T09:00:00.500Z"}, "response": message("m7")},
        pair(h.T0 + 14, message("m8", usage_bad=1, input_tokens=-5)),
    ])
    result = read(path)
    msgs = by_msg(result)
    assert msgs["m1"].final_attempt.inferences[0].usage.output == 50
    assert result.stats["duplicate_records"] == 2
    m2 = msgs["m2"].final_attempt.inferences[0]
    assert (m2.pricing.channel, m2.pricing.endpoint_scope, m2.pricing.billing_path,
            m2.pricing.service_tier, m2.pricing.speed) == (
        "bedrock", "global", "bedrock", "priority", "fast")
    assert m2.usage.cache_write_unknown == 100
    assert msgs["m3"].final_attempt.inferences[0].pricing.model == ""
    assert msgs["m5"].final_attempt.diagnostics.reason == "unavailable"
    assert msgs["m5"].final_attempt.diagnostics.provider_reason == "novel_reason"
    assert msgs["m6"].final_attempt.diagnostics is None
    assert msgs["m6"].final_attempt.applied_edits == (("clear_thinking", 0),)
    assert msgs["m7"].ts_start_ms == h.T0 + 500
    errors = sorted((r.final_attempt.http_status or 0, r.final_attempt.error_type)
                    for r in result.requests if r.final_attempt.outcome is Outcome.HTTP_ERROR)
    assert errors == [(0, "other"), (418, "other")]
    assert sorted(q.reason for q in result.quarantined) == sorted([
        "missing:usage", "missing:type", "bad_usage"])
    notes = {n.code: n.count for n in result.notes}
    assert notes["dq.unpriced_model"] == 1 and notes["dq.lanes_inferred"] >= 1
    assert "ttl_split" not in result.capabilities
    assert h.CANARY not in h.blob(result)


def test_window(tmp_path: Path) -> None:
    path = h.write_lines(tmp_path / "a.jsonl", [pair(h.T0, message("m1")),
                                                pair(h.T0 + 1_000, message("m2"))])
    result = read(path, until_ms=h.T0 + 1)
    assert list(by_msg(result)) == ["m1"] and result.stats["outside_window"] == 1


@pytest.mark.parametrize("path, expected", [
    (h.ANTHROPIC, True), (h.OPENAI, False), (h.BEDROCK, False), (h.OTLP_GENAI, False)])
def test_sniff(path: Path, expected: bool) -> None:
    assert ADAPTER.sniff(path, path.read_bytes()[:65536]) is expected


def test_sniff_bare_messages_batch_lines_and_huge_heads() -> None:
    line = b'{"id": "msg_1", "type": "message", "usage": {"input_tokens": 1}}\n'
    assert ADAPTER.sniff(Path("x"), line)
    assert ADAPTER.sniff(Path("x"), b'{"custom_id": "a", "result": {"type": "succeeded"}}\n')
    assert not ADAPTER.sniff(Path("x"), b'{"custom_id": "a", "result": {"type": "novel"}}\n')
    assert ADAPTER.sniff(Path("x"), b'{"id": "msg_01", "content": "' + b"x" * 70_000)
    assert not ADAPTER.sniff(Path("x"), b'{"type": "assistant", "message": {}}\n')
    assert not ADAPTER.sniff(Path("x"), b'{"request_meta": {}, "response": 3}\n')


def test_attribution_billing_path_mirrors_the_pricing_context(fixture: IngestResult) -> None:
    for req in fixture.requests:
        infs = req.final_attempt.inferences
        if infs and infs[0].pricing.billing_path != "unknown":
            assert req.attribution.billing_path == infs[0].pricing.billing_path
    assert by_msg(fixture)["msg_vertex_1"].attribution.billing_path == "vertex"
    assert by_msg(fixture)["msg_01"].attribution.billing_path is None  # unknown stays unset
