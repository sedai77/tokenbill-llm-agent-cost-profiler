"""SPEC §5.10: the Bedrock adapter (invocation logs, Converse responses, identity.arn, scopes)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.v2.telemetry import helpers as h
from tokenbill.adapters.bedrock import BedrockAdapter, bedrock_model
from tokenbill.core.ids import pseudonym
from tokenbill.core.records import Fidelity, InferenceKind, Outcome
from tokenbill.core.types import IngestResult

ADAPTER = BedrockAdapter()
ARN = "arn:aws:sts::123456789012:assumed-role/DevRole/" + h.CANARY_EMAIL


def read(path: Path, **kw: Any) -> IngestResult:
    return ADAPTER.read(path, h.central(**kw))


def by_rid(result: IngestResult) -> dict[str, Any]:
    return {r.final_attempt.provider_request_id: r for r in result.requests}


def lanes(result: IngestResult) -> dict[str, Any]:
    return {lane.lane_key: lane for s in result.sessions for lane in s.lanes}


def log(rid: str, ts: str = "2026-09-23T09:00:00Z", **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schemaType": "ModelInvocationLog", "schemaVersion": "1.0", "timestamp": ts,
        "accountId": "123456789012", "requestId": rid, "operation": "Converse",
        "modelId": "anthropic.claude-opus-5", "identity": {"arn": ARN},
        "output": {"outputBodyJson": {"usage": {"inputTokens": 10, "outputTokens": 20}}}}
    base.update(kw)
    return base


@pytest.fixture(scope="module")
def fixture() -> IngestResult:
    return read(h.BEDROCK)


def test_cache_details_split_with_residual(fixture: IngestResult) -> None:
    req = by_rid(fixture)["br-req-1"]
    att = req.final_attempt
    usage = att.inferences[0].usage
    assert (usage.uncached_input, usage.cache_write_1h, usage.cache_write_5m,
            usage.cache_write_unknown, usage.output) == (100, 20_000, 8_000, 2_000, 500)
    assert att.convention_id == "bedrock.converse"
    assert (att.duration_ms, att.stop_reason) == (2_500, "end_turn")
    notes = {n.code: (n.count, n.tokens) for n in fixture.notes}
    assert notes["dq.ttl_split_residual"] == (1, 2_000)
    assert req.source.fidelity is Fidelity.NO_TTL_SPLIT  # a residual left an unknown TTL


def test_global_and_regional_profiles(fixture: IngestResult) -> None:
    req = by_rid(fixture)
    glob = req["br-req-1"].final_attempt.inferences[0].pricing
    assert (glob.channel, glob.model, glob.endpoint_scope, glob.billing_path, glob.provider) == (
        "bedrock", "claude-opus-5", "global", "bedrock", "anthropic")
    assert glob.model_raw == "global.anthropic.claude-opus-5"
    assert req["br-req-2"].final_attempt.inferences[0].pricing.endpoint_scope == "regional"
    arn_profile = req["br-req-4"].final_attempt.inferences[0].pricing
    assert (arn_profile.model, arn_profile.endpoint_scope, arn_profile.model_raw) == (
        "claude-opus-5", "global", "global.anthropic.claude-opus-5")


def test_invoke_model_body_uses_the_anthropic_convention(fixture: IngestResult) -> None:
    att = by_rid(fixture)["br-req-2"].final_attempt
    assert att.convention_id == "anthropic.messages"
    usage = att.inferences[0].usage
    assert (usage.uncached_input, usage.cache_read, usage.output) == (50, 30_000, 200)
    assert att.stop_reason == "max_tokens"
    assert by_rid(fixture)["br-req-2"].source.fidelity is Fidelity.FULL


def test_identity_arn_team_map_and_pseudonym(fixture: IngestResult) -> None:
    text = h.blob(fixture)
    assert h.CANARY not in text and "DevRole" not in text and "123456789012" not in text
    principals = {r.attribution.principal for r in fixture.requests if r.attribution.principal}
    assert principals == {h.p_of(ARN)}
    teams = {rid: r.attribution.team for rid, r in by_rid(fixture).items()}
    # the role form of the ARN is in the team map, which outranks requestMetadata "team"
    assert teams["br-req-1"] == teams["br-req-2"] == "platform"
    unmapped = read(h.BEDROCK, team_map=())
    assert {r.final_attempt.provider_request_id: r.attribution.team
            for r in unmapped.requests}["br-req-1"] == "ops"
    assert all(r.attribution.billing_path == "bedrock" for r in fixture.requests)


def test_request_metadata_allowlist(fixture: IngestResult) -> None:
    req = by_rid(fixture)["br-req-1"]
    assert dict(req.attribution.extra) == {"environment": "prod"}
    assert {n.code: n.count for n in fixture.notes}["dq.unknown_fields"] == 1  # ticket_title
    same = by_rid(fixture)["br-req-2"]
    assert same.lane_key == req.lane_key and [same.seq, req.seq] == [1, 0]
    lane = lanes(fixture)[req.lane_key]
    assert lane.lane_exact
    assert lane.cache_scope_key == "org:bedrock:" + pseudonym(h.NAME_KEY, "h", "123456789012")


def test_error_code_stream_body_and_pairs(fixture: IngestResult) -> None:
    req = by_rid(fixture)
    throttled = req["br-req-3"].final_attempt
    assert (throttled.outcome, throttled.http_status, throttled.error_type) == (
        Outcome.HTTP_ERROR, 429, "rate_limit")
    assert throttled.inferences == ()
    assert not lanes(fixture)[req["br-req-3"].lane_key].lane_exact
    stream = req["br-req-4"].final_attempt
    assert (stream.inferences[0].usage.output, stream.duration_ms, stream.stop_reason) == (
        70, 900, "end_turn")
    pair = next(r for r in fixture.requests if r.final_attempt.provider_request_id is None)
    assert pair.ts_start_ms == h.T0 + 600_000 and pair.lane_key == req["br-req-4"].lane_key
    assert pair.final_attempt.inferences[0].pricing.endpoint_scope == "global"
    assert {"attempts", "attribution.team"} <= fixture.capabilities


def test_variants(tmp_path: Path) -> None:
    anthropic_stream = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 5,
                                                        "cache_read_input_tokens": 100,
                                                        "output_tokens": 1}}},
        {"type": "content_block_delta", "delta": {"text": h.CANARY}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 42}}]
    path = h.write_lines(tmp_path / "b.jsonl", [
        log("s1", operation="InvokeModelWithResponseStream",
            output={"outputBodyJson": anthropic_stream}),
        log("o1", modelId="global.openai.gpt-5.6-sol", output={"outputBodyJson": {
            "usage": {"input_tokens": 1_000, "output_tokens": 5,
                      "input_tokens_details": {"cached_tokens": 512,
                                               "cache_write_tokens": 0}}}}),
        log("o2", modelId="openai.gpt-5.4", output={"outputBodyJson": {
            "usage": {"prompt_tokens": 10, "completion_tokens": 2}}}),
        log("n1", modelId="us.amazon.nova-pro-v1:0"),
        log("app", modelId="arn:aws:bedrock:us-east-1:123456789012:"
                           "application-inference-profile/abc123"),
        log("tier", output={"outputBodyJson": {"usage": {"inputTokens": 1, "outputTokens": 1},
                                               "serviceTier": {"type": "priority"}}}),
        log("odd-tier", output={"outputBodyJson": {"usage": {"inputTokens": 1,
                                                             "outputTokens": 1},
                                                   "serviceTier": {"type": "reserved"}}}),
        log("weird", output={"outputBodyJson": {"usage": {"tokens": 3}}}),
        log("nousage", output={"outputBodyJson": {"stopReason": "end_turn"}}),
        log("unknown-error", output=None, errorCode="SomethingNewException"),
        log("bad-ts", ts="last tuesday"),
        {"request_meta": {"session": "x"}, "response": {"usage": {"inputTokens": 1}}},
        {"request_meta": {"ts_ms": h.T0}, "response": "text"},
        {"usage": {"inputTokens": 1, "outputTokens": 2}, "ResponseMetadata": {
            "RequestId": "boto-1"}},
    ])
    result = read(path)
    req = by_rid(result)
    assert (req["s1"].final_attempt.inferences[0].usage.output,
            req["s1"].final_attempt.stop_reason) == (42, "end_turn")
    o1 = req["o1"].final_attempt
    assert (o1.convention_id, o1.inferences[0].pricing.provider,
            o1.inferences[0].pricing.model, o1.inferences[0].pricing.endpoint_scope) == (
        "openai.responses", "openai", "gpt-5.6-sol", "global")
    assert req["o2"].final_attempt.convention_id == "openai.chat"
    assert req["o2"].final_attempt.inferences[0].pricing.endpoint_scope == "regional"
    nova = req["n1"].final_attempt.inferences[0].pricing
    assert (nova.provider, nova.model) == ("amazon", "")
    assert req["app"].final_attempt.inferences[0].pricing.model_raw == "abc123"
    assert req["tier"].final_attempt.inferences[0].pricing.service_tier == "priority"
    assert req["odd-tier"].final_attempt.inferences[0].pricing.service_tier == "unknown"
    unknown_error = req["unknown-error"].final_attempt
    assert (unknown_error.outcome, unknown_error.error_type) == (Outcome.UNKNOWN, "other")
    assert sorted(q.reason for q in result.quarantined) == sorted([
        "bad_usage", "missing:output.outputBodyJson.usage", "missing:timestamp",
        "missing:request_meta.ts_ms", "missing:response", "missing:request_meta.ts_ms"])
    assert {n.code: n.count for n in result.notes}["dq.unpriced_model"] == 2
    assert h.CANARY not in h.blob(result)


def test_iterations_and_ttl_split_capabilities(tmp_path: Path) -> None:
    body = {"usage": {"input_tokens": 10, "output_tokens": 30, "cache_read_input_tokens": 0,
                      "cache_creation_input_tokens": 1_000,
                      "cache_creation": {"ephemeral_5m_input_tokens": 1_000},
                      "iterations": [{"type": "compaction", "input_tokens": 500,
                                      "output_tokens": 100},
                                     {"type": "message", "input_tokens": 10,
                                      "cache_creation_input_tokens": 1_000,
                                      "cache_creation": {"ephemeral_5m_input_tokens": 1_000},
                                      "output_tokens": 30}],
                      "service_tier": "priority", "speed": "fast", "inference_geo": "us"}}
    path = h.write_lines(tmp_path / "b.jsonl", [
        log("it", operation="InvokeModel", output={"outputBodyJson": body},
            requestMetadata={"session": "s", "lane": "l", "team": "infra"})])
    result = read(path)
    (req,) = result.requests
    kinds = [i.kind for i in req.final_attempt.inferences]
    assert kinds == [InferenceKind.COMPACTION, InferenceKind.MESSAGE]
    ctx = req.final_attempt.inferences[1].pricing
    assert (ctx.service_tier, ctx.speed, ctx.inference_geo) == ("priority", "fast", "us")
    assert {"iterations", "ttl_split", "lanes_exact", "attribution.team"} <= result.capabilities


def test_bedrock_model_helper() -> None:
    assert bedrock_model("global.anthropic.claude-opus-5")[2].endpoint_scope == "global"
    assert bedrock_model("eu.anthropic.claude-sonnet-5")[2].endpoint_scope == "regional"
    assert bedrock_model("anthropic.claude-opus-4-6-v1")[2].model == "claude-opus-4-6"
    assert bedrock_model(None)[0] == "unknown"
    assert bedrock_model("titan")[2].model == ""


@pytest.mark.parametrize("path, expected", [
    (h.BEDROCK, True), (h.OPENAI, False), (h.OTLP_CC, False), (h.ANTHROPIC, False)])
def test_sniff(path: Path, expected: bool) -> None:
    assert ADAPTER.sniff(path, path.read_bytes()[:65536]) is expected


def test_sniff_converse_pairs_and_huge_logs() -> None:
    assert ADAPTER.sniff(Path("x"), b'{"request_meta": {}, "response": {"usage": '
                                    b'{"inputTokens": 1, "outputTokens": 2}}}\n')
    assert ADAPTER.sniff(Path("x"), b'{"schemaType": "ModelInvocationLog", "input": "'
                         + b"x" * 70_000)
    assert not ADAPTER.sniff(Path("x"), b'{"usage": {"input_tokens": 1}}\n')
