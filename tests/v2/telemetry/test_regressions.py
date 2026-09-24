"""Regression tests for defects found in the adversarial review of TELEM (Claude Code
``query_source`` subsystem values, identity as reported, channels named by Bedrock/Vertex model
ids, lane/session consistency, request order, range clamping, undated spans, nested model spans,
team-level metric rows, path/email labels)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from tests.v2.telemetry import helpers as h
from tokenbill.adapters import conventions_ext as ext
from tokenbill.adapters.anthropic_responses import AnthropicResponsesAdapter
from tokenbill.adapters.bedrock import BedrockAdapter
from tokenbill.adapters.openai import OpenAIUsageAdapter
from tokenbill.adapters.otel import OtlpJsonAdapter, _query_source
from tokenbill.core.models import normalize_model
from tokenbill.core.records import MAX_TOKENS, Attribution, LaneKind
from tokenbill.core.types import IngestResult

OTLP = OtlpJsonAdapter()
SESSION = {"session.id": "sess-r", "user.email": "Dev@Example.com"}


def cc_request(t_ms: int, rid: str, **kw: Any) -> dict[str, Any]:
    attrs = {**SESSION, "model": "claude-opus-5-5", "request_id": rid, "duration_ms": 1_000,
             "input_tokens": 10, "output_tokens": 100, "cache_read_tokens": 0,
             "cache_creation_tokens": 0, "query_source": "main", **kw}
    return h.event("api_request", t_ms, attrs)


def lanes(result: IngestResult) -> dict[str, Any]:
    return {lane.lane_key: lane for s in result.sessions for lane in s.lanes}


def ctx_of(req: Any) -> Any:
    return req.final_attempt.inferences[0].pricing


# ---------------------------------------------------------------------------------------------
# Claude Code query_source: events carry the requesting subsystem, metrics the category
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, kind, category", [
    ("repl_main_thread", LaneKind.MAIN, "main"),
    ("repl_main_thread:outputStyle:Concise", LaneKind.MAIN, "main"),
    ("sdk", LaneKind.MAIN, "main"),
    ("agent:builtin:Explore", LaneKind.SUBAGENT, "subagent"),
    ("agent:reviewer", LaneKind.SUBAGENT, "subagent"),
    ("compact", LaneKind.COMPACTION, "compaction"),
    ("away_summary", LaneKind.HELPER, "auxiliary"),
    ("prompt_suggestion", LaneKind.HELPER, "auxiliary"),
    ("web_search_tool", LaneKind.HELPER, "auxiliary"),
    ("main", LaneKind.MAIN, "main"), ("subagent", LaneKind.SUBAGENT, "subagent"),
    ("auxiliary", LaneKind.HELPER, "auxiliary"), ("compaction", LaneKind.COMPACTION, "compaction"),
    (None, LaneKind.UNKNOWN, None), ("", LaneKind.UNKNOWN, None),
])
def test_query_source_vocabularies(value: str | None, kind: LaneKind,
                                   category: str | None) -> None:
    assert _query_source(value) == (kind, category)


def test_real_query_sources_give_lanes_ttl_hints_and_agent_types(tmp_path: Path) -> None:
    path = h.write_lines(tmp_path / "o.jsonl", [h.logs([
        cc_request(h.T0, "m1", query_source="repl_main_thread", cache_creation_tokens=500),
        cc_request(h.T0 + 10, "m2", query_source="repl_main_thread:outputStyle:Concise",
                   cache_creation_tokens=500),
        cc_request(h.T0 + 20, "s1", query_source="agent:builtin:Explore",
                   cache_creation_tokens=100),
        cc_request(h.T0 + 30, "s2", query_source="agent:secret-reviewer"),
        cc_request(h.T0 + 40, "x1", query_source="away_summary"),
        cc_request(h.T0 + 50, "c1", query_source="compact"),
        cc_request(h.T0 + 60, "u1", query_source=""),
    ])])
    result = OTLP.read(path, h.central(attribution=Attribution(billing_path="subscription")))
    req = {r.final_attempt.provider_request_id: r for r in result.requests}
    lane_map = lanes(result)
    kinds = {rid: lane_map[r.lane_key].kind for rid, r in req.items()}
    assert kinds == {"m1": LaneKind.MAIN, "m2": LaneKind.MAIN, "s1": LaneKind.SUBAGENT,
                     "s2": LaneKind.SUBAGENT, "x1": LaneKind.HELPER,
                     "c1": LaneKind.COMPACTION, "u1": LaneKind.UNKNOWN}
    assert req["m1"].lane_key == req["m2"].lane_key and lane_map[req["m1"].lane_key].lane_exact
    assert req["s1"].lane_key == req["s2"].lane_key  # no spans: one shared, inexact lane
    assert not lane_map[req["s1"].lane_key].lane_exact
    hints = {rid: ctx_of(r).write_ttl_hint for rid, r in req.items()}
    assert hints["m1"] == hints["m2"] == "1h" and hints["s1"] == hints["c1"] == "5m"
    assert {rid: r.attribution.query_source for rid, r in req.items()} == {
        "m1": "main", "m2": "main", "s1": "subagent", "s2": "subagent", "x1": "auxiliary",
        "c1": "compaction", "u1": None}
    assert req["s1"].attribution.agent_type == "Explore"  # built-in agent, in clear
    assert req["s2"].attribution.agent_type.startswith("h_")  # a custom agent name is hashed
    text = h.blob(result)
    assert "secret-reviewer" not in text and "Concise" not in text and "away_summary" not in text


# ---------------------------------------------------------------------------------------------
# identity: pseudonym of the value as reported (SPEC §5.1; the ADMIN adapters do the same)
# ---------------------------------------------------------------------------------------------

def test_principal_is_the_pseudonym_of_the_identity_as_reported(tmp_path: Path) -> None:
    path = h.write_lines(tmp_path / "o.jsonl", [h.logs([cc_request(h.T0, "r1")])])
    (req,) = OTLP.read(path, h.central(team_map=(("dev@example.com", "core"),))).requests
    assert req.attribution.principal == h.p_of("Dev@Example.com")
    assert req.attribution.principal != h.p_of("dev@example.com")
    assert req.attribution.team == "core"  # the team map still matches case-insensitively
    assert "Dev@Example.com" not in h.blob(OTLP.read(path, h.central()))


def test_normalize_identity_and_case_insensitive_team_map() -> None:
    assert ext.normalize_identity("  Alice@Example.COM ") == "Alice@Example.COM"
    assert ext.normalize_identity("") is None and ext.normalize_identity(3) is None
    opts = h.central(team_map=(("ALICE@example.com", "payments"), ("bob", "infra")))
    assert ext.team_for(opts, ["alice@EXAMPLE.com"]) == "payments"
    assert ext.team_for(opts, ["bob"]) == "infra"
    assert ext.team_for(opts, [None, "", "carol"]) is None


# ---------------------------------------------------------------------------------------------
# channels: a Bedrock / Vertex model id is never priced on the Claude API
# ---------------------------------------------------------------------------------------------

def test_model_channel_helper() -> None:
    bedrock = normalize_model("us.anthropic.claude-opus-5")
    vertex = normalize_model("claude-opus-5@20260101")
    plain = normalize_model("claude-opus-5-5")
    assert ext.model_channel("anthropic_api", bedrock) == "bedrock"
    assert ext.model_channel("unknown", vertex) == "vertex"
    assert ext.model_channel("anthropic_api", plain) == "anthropic_api"
    assert ext.model_channel("foundry", bedrock) == "foundry"  # an explicit channel wins


def test_claude_code_bedrock_model_id_without_billing_path(tmp_path: Path) -> None:
    path = h.write_lines(tmp_path / "o.jsonl", [h.logs([
        cc_request(h.T0, "r1", model="us.anthropic.claude-opus-5")])])
    result = OTLP.read(path, h.central())
    (req,) = result.requests
    ctx = ctx_of(req)
    assert (ctx.channel, ctx.model, ctx.endpoint_scope) == ("bedrock", "claude-opus-5",
                                                            "regional")
    assert ctx.billing_path == "unknown" and ctx.write_ttl_hint is None  # only opts name these
    assert lanes(result)[req.lane_key].cache_scope_key == "org:bedrock:unknown"
    # a known 1P billing path keeps the Claude API
    api = OTLP.read(path, h.central(attribution=Attribution(billing_path="api_key")))
    assert ctx_of(api.requests[0]).channel == "anthropic_api"


def test_span_and_response_channels_follow_the_model_id(tmp_path: Path) -> None:
    span_attrs = {"gen_ai.operation.name": "chat", "gen_ai.provider.name": "anthropic",
                  "gen_ai.request.model": "global.anthropic.claude-opus-5",
                  "gen_ai.usage.input_tokens": 10, "gen_ai.usage.output_tokens": 2}
    oi_attrs = {"openinference.span.kind": "LLM", "llm.model_name": "claude-opus-5@20260101",
                "llm.token_count.prompt": 5}
    path = h.write_lines(tmp_path / "s.jsonl", [h.spans([
        h.span("chat", "0000000000000001", h.T0, h.T0 + 5, span_attrs),
        h.span("llm", "0000000000000002", h.T0, h.T0 + 5, oi_attrs, trace="cd" * 16)])])
    result = OTLP.read(path, h.central())
    by_model = {ctx_of(r).model_raw: ctx_of(r) for r in result.requests}
    genai = by_model["global.anthropic.claude-opus-5"]
    assert (genai.channel, genai.endpoint_scope, genai.billing_path) == ("bedrock", "global",
                                                                         "bedrock")
    oi = by_model["claude-opus-5@20260101"]
    assert (oi.provider, oi.channel, oi.model) == ("anthropic", "vertex", "claude-opus-5")
    response = h.write_lines(tmp_path / "a.jsonl", [{
        "request_meta": {"ts_ms": h.T0, "model_raw": "claude-opus-5@20260101"},
        "response": {"id": "msg_v", "type": "message", "usage": {"input_tokens": 3}}}])
    (req,) = AnthropicResponsesAdapter().read(response, h.central()).requests
    assert (ctx_of(req).channel, ctx_of(req).billing_path) == ("vertex", "vertex")


# ---------------------------------------------------------------------------------------------
# lanes, sessions and request order
# ---------------------------------------------------------------------------------------------

def test_requests_of_a_lane_share_the_lane_session(tmp_path: Path) -> None:
    llm = {"openinference.span.kind": "LLM", "llm.provider": "openai",
           "llm.model_name": "gpt-5.6-sol", "llm.token_count.prompt": 100,
           "llm.token_count.completion": 5}
    path = h.write_lines(tmp_path / "s.jsonl", [h.spans([
        h.span("agent", "00000000000000a1", h.T0, h.T0 + 100,
               {"openinference.span.kind": "AGENT", "session.id": "s-1"}),
        h.span("llm", "00000000000000a2", h.T0 + 1, h.T0 + 10, {**llm, "session.id": "s-1"},
               parent="00000000000000a1"),
        h.span("llm", "00000000000000a3", h.T0 + 20, h.T0 + 30, {**llm, "session.id": "s-2"},
               parent="00000000000000a1")])])
    result = OTLP.read(path, h.central())
    assert len(result.requests) == 2 and len({r.lane_key for r in result.requests}) == 1
    lane = lanes(result)[result.requests[0].lane_key]
    assert {r.session_key for r in result.requests} == {lane.session_key}


def test_seq_follows_the_first_attempt_of_each_request(tmp_path: Path) -> None:
    path = h.write_lines(tmp_path / "o.jsonl", [h.logs([
        cc_request(h.T0 + 2_000, "a"),                                  # starts at T0 + 1,000
        h.event("api_error", h.T0 + 600, {**SESSION, "request_id": "b", "status_code": 529,
                                          "duration_ms": 100}),         # starts at T0 + 500
        cc_request(h.T0 + 3_000, "b"),                                  # retried at T0 + 2,000
    ])])
    result = OTLP.read(path, h.central())
    ordered = sorted(result.requests, key=lambda r: r.seq)
    assert [r.final_attempt.provider_request_id for r in ordered] == ["b", "a"]
    assert [r.ts_start_ms for r in ordered] == [h.T0 + 500, h.T0 + 1_000]


# ---------------------------------------------------------------------------------------------
# lenient reads never abort on one record's values
# ---------------------------------------------------------------------------------------------

def test_huge_durations_are_clamped_not_fatal(tmp_path: Path) -> None:
    openai = h.write_lines(tmp_path / "o.jsonl", [{
        "request_meta": {"ts_ms": h.T0, "duration_ms": MAX_TOKENS},
        "response": {"id": "resp_1", "object": "response", "created_at": 1,
                     "usage": {"input_tokens": 1}}}])
    anthropic = h.write_lines(tmp_path / "a.jsonl", [{
        "request_meta": {"ts_ms": MAX_TOKENS - 1, "duration_ms": MAX_TOKENS},
        "response": {"id": "msg_1", "type": "message", "usage": {"input_tokens": 1}}}])
    bedrock = h.write_lines(tmp_path / "b.jsonl", [{
        "schemaType": "ModelInvocationLog", "timestamp": "2026-09-23T09:00:00Z",
        "requestId": "br-1", "modelId": "anthropic.claude-opus-5",
        "output": {"outputBodyJson": {"usage": {"inputTokens": 1, "outputTokens": 1},
                                      "metrics": {"latencyMs": MAX_TOKENS}}}}])
    for adapter, path in ((OpenAIUsageAdapter(), openai), (AnthropicResponsesAdapter(), anthropic),
                          (BedrockAdapter(), bedrock)):
        result = adapter.read(path, h.central())
        assert len(result.requests) == 1, adapter.name
        assert result.sessions[0].ended_ms == MAX_TOKENS


def test_boundary_numbers_never_abort_a_lenient_read(tmp_path: Path) -> None:
    """Every numeric leaf of every fixture record, set to a range boundary."""
    adapters = {h.OTLP_CC: OTLP, h.OTLP_GENAI: OTLP, h.OTLP_OI: OTLP,
                h.OPENAI: OpenAIUsageAdapter(), h.BEDROCK: BedrockAdapter(),
                h.ANTHROPIC: AnthropicResponsesAdapter()}

    def leaves(obj: Any, prefix: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        if isinstance(obj, dict):
            return [p for k, v in obj.items() for p in leaves(v, (*prefix, k))]
        if isinstance(obj, list):
            return [p for i, v in enumerate(obj) for p in leaves(v, (*prefix, i))]
        numeric = (type(obj) in (int, float)) or (isinstance(obj, str) and obj.isdigit())
        return [prefix] if numeric else []

    target = tmp_path / "f.jsonl"
    for fixture, adapter in adapters.items():
        records = [json.loads(line) for line in fixture.read_text().splitlines()
                   if line.startswith("{") and line.endswith("}")]
        for index, record in enumerate(records):
            for path in leaves(record):
                for value in (MAX_TOKENS, MAX_TOKENS - 1, str(2**63 - 1)):
                    mutated = copy.deepcopy(records)
                    node = mutated[index]
                    for step in path[:-1]:
                        node = node[step]
                    node[path[-1]] = value
                    target.write_text("\n".join(json.dumps(r) for r in mutated) + "\n")
                    adapter.read(target, h.central())  # must not raise


def test_an_out_of_float_range_double_is_one_bad_value_not_a_bad_line(tmp_path: Path) -> None:
    rec = h.event("api_request", h.T0, {"session.id": "s", "request_id": "r1", "input_tokens": 1})
    rec["attributes"].append({"key": "cost_usd", "value": {"doubleValue": "@HUGE@"}})
    other = h.event("api_request", h.T0, {"session.id": "s", "request_id": "r2",
                                          "input_tokens": 2})
    line = json.dumps(h.logs([rec, other])).replace('"@HUGE@"', "1" + "0" * 400)
    path = tmp_path / "o.jsonl"
    path.write_text(line + "\n")
    result = OTLP.read(path, h.central())
    assert sorted(r.final_attempt.provider_request_id for r in result.requests) == ["r1", "r2"]
    assert result.quarantined == []
    r1 = next(r for r in result.requests if r.final_attempt.provider_request_id == "r1")
    assert r1.final_attempt.inferences[0].provider_reported_cost_nano is None


def test_spans_without_a_start_time_are_quarantined(tmp_path: Path) -> None:
    genai = {"gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai",
             "gen_ai.request.model": "gpt-5.6-sol", "gen_ai.usage.input_tokens": 10}
    chat = h.span("chat", "0000000000000001", h.T0, h.T0 + 5, genai)
    llm = h.span("claude_code.llm_request", "0000000000000002", h.T0, h.T0 + 5,
                 {"request_id": "r1", "input_tokens": 1, "model": "claude-opus-5-5"})
    for sp in (chat, llm):
        del sp["startTimeUnixNano"]
    result = OTLP.read(h.write_lines(tmp_path / "s.jsonl", [h.spans([chat, llm])]), h.central())
    assert result.requests == []  # never dated 1970-01-01
    assert [q.reason for q in result.quarantined] == ["missing:startTimeUnixNano"] * 2


# ---------------------------------------------------------------------------------------------
# no double counting: nested model-call spans
# ---------------------------------------------------------------------------------------------

def test_a_model_span_wrapping_another_is_not_counted_twice(tmp_path: Path) -> None:
    oi = {"openinference.span.kind": "LLM", "llm.provider": "openai",
          "llm.model_name": "gpt-5.6-sol", "llm.token_count.prompt": 100,
          "llm.token_count.completion": 5}
    genai = {"gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai",
             "gen_ai.request.model": "gpt-5.6-sol", "gen_ai.usage.input_tokens": 100,
             "gen_ai.usage.output_tokens": 5}
    path = h.write_lines(tmp_path / "s.jsonl", [h.spans([
        # a framework LLM span around the SDK's LLM span (same tokens twice)
        h.span("ChatOpenAI", "00000000000000b1", h.T0, h.T0 + 100, oi),
        h.span("chain", "00000000000000b2", h.T0, h.T0 + 100,
               {"openinference.span.kind": "CHAIN"}, parent="00000000000000b1"),
        h.span("ChatCompletion", "00000000000000b3", h.T0 + 1, h.T0 + 99, oi,
               parent="00000000000000b2"),
        # a GenAI chat span around an instrumented SDK chat span, in another trace
        h.span("chat", "00000000000000c1", h.T0, h.T0 + 100, genai, trace="cd" * 16),
        h.span("chat", "00000000000000c2", h.T0 + 1, h.T0 + 99, genai, trace="cd" * 16,
               parent="00000000000000c1"),
        # two sibling LLM spans under an AGENT stay two requests
        h.span("agent", "00000000000000d1", h.T0, h.T0 + 100,
               {"openinference.span.kind": "AGENT"}, trace="ef" * 16),
        h.span("llm", "00000000000000d2", h.T0 + 1, h.T0 + 10, oi, trace="ef" * 16,
               parent="00000000000000d1"),
        h.span("llm", "00000000000000d3", h.T0 + 20, h.T0 + 30, oi, trace="ef" * 16,
               parent="00000000000000d1"),
    ])])
    result = OTLP.read(path, h.central())
    assert len(result.requests) == 4  # b3, c2, d2, d3
    assert result.stats["nested_model_spans"] == 2
    assert sum(ctx_of(r) is not None for r in result.requests) == 4
    kinds = {lanes(result)[r.lane_key].kind for r in result.requests}
    assert kinds == {LaneKind.API_RUN}


# ---------------------------------------------------------------------------------------------
# metrics: team rows, never one person's series
# ---------------------------------------------------------------------------------------------

def test_metric_series_of_several_users_sum_into_one_team_row(tmp_path: Path) -> None:
    def pt(user: str, start: int, end: int, value: int) -> dict[str, Any]:
        return h.point(start, end, value, {"user.email": user, "model": "claude-sonnet-5",
                                            "type": "input"})

    delta = {"name": "claude_code.token.usage", "sum": {"aggregationTemporality": 1,
                                                        "dataPoints": [
        pt("a@example.com", h.T0, h.T0 + 60_000, 100),
        pt("b@example.com", h.T0 + 7_000, h.T0 + 67_000, 30),
        pt("c@example.com", h.T0 + 3_600_000, h.T0 + 3_660_000, 5)]}}
    team_map = (("a@example.com", "core"), ("b@example.com", "core"), ("c@example.com", "core"))
    result = OTLP.read(h.write_lines(tmp_path / "m.jsonl", [h.metrics([delta])]),
                       h.central(team_map=team_map))
    rows = [(a.bucket_start_ms, a.bucket_end_ms, a.usage.uncached_input, dict(a.dims)["team"])
            for a in result.aggregates]
    assert rows == [(h.T0, h.T0 + 3_600_000, 130, "core"),
                    (h.T0 + 3_600_000, h.T0 + 7_200_000, 5, "core")]
    assert "example.com" not in h.blob(result)


# ---------------------------------------------------------------------------------------------
# labels never carry paths or emails
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value, expected", [
    ("claude-opus-5@20260101", "claude-opus-5@20260101"),
    ("claude-opus-4@latest", "claude-opus-4@latest"),
    ("global.anthropic.claude-opus-5", "global.anthropic.claude-opus-5"),
    ("arn:aws:bedrock:us-east-1:1:inference-profile/x", "arn:aws:bedrock:us-east-1:1:"
                                                         "inference-profile/x"),
    ("/home/alice/models/llama.gguf", None), ("~/models/q.gguf", None), ("./m", None),
    ("../m", None), ("alice@example.com", None), ("x@corp.example", None),
])
def test_clean_label_refuses_paths_and_emails(value: str, expected: str | None) -> None:
    assert ext.clean_label(value) == expected


def test_a_local_model_path_never_reaches_a_record(tmp_path: Path) -> None:
    oi = {"openinference.span.kind": "LLM", "llm.model_name": "/home/alice/models/q.gguf",
          "llm.token_count.prompt": 5}
    path = h.write_lines(tmp_path / "s.jsonl", [h.spans([
        h.span("llm", "0000000000000001", h.T0, h.T0 + 5, oi)])])
    result = OTLP.read(path, h.central())
    (req,) = result.requests
    assert ctx_of(req).model_raw == "" and "/home/alice" not in h.blob(result)
    assert any(n.code == "dq.unpriced_model" for n in result.notes)
