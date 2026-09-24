"""SPEC §5.9 / D23: the OTLP/JSON adapter (Claude Code events, metrics, beta spans, GenAI and
OpenInference spans)."""

from __future__ import annotations

import dataclasses
import gzip
import importlib.util
from pathlib import Path
from typing import Any

import pytest

from tests.v2.telemetry import helpers as h
from tokenbill.adapters.otel import OtlpJsonAdapter, _decode
from tokenbill.core.errors import SourceError
from tokenbill.core.ids import key_id, request_id_for, stable_id
from tokenbill.core.labels import Basis
from tokenbill.core.records import (
    Attribution,
    Fidelity,
    InferenceKind,
    LaneEventKind,
    LaneKind,
    Outcome,
)
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import IngestResult

ADAPTER = OtlpJsonAdapter()
SESSION = {"session.id": "sess-x", "user.email": "Dev@Example.com"}


def read(path: Path, **kw: Any) -> IngestResult:
    return ADAPTER.read(path, h.central(**kw))


def by_rid(result: IngestResult) -> dict[str, Any]:
    return {r.final_attempt.provider_request_id: r for r in result.requests}


def lanes(result: IngestResult) -> dict[str, Any]:
    return {lane.lane_key: lane for s in result.sessions for lane in s.lanes}


def note(result: IngestResult, code: str) -> Any:
    return next((n for n in result.notes if n.code == code), None)


def api_request(t_ms: int, rid: str, **kw: Any) -> dict[str, Any]:
    attrs = {**SESSION, "model": "claude-opus-5-5", "request_id": rid, "duration_ms": 1_000,
             "input_tokens": 10, "output_tokens": 100, "cache_read_tokens": 0,
             "cache_creation_tokens": 0, "query_source": "main", **kw}
    return h.event("api_request", t_ms, attrs)


# ---------------------------------------------------------------------------------------------
# the Claude Code fixture (brief acceptance tests)
# ---------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def cc() -> IngestResult:
    return read(h.OTLP_CC)


def test_three_api_requests_one_api_error_and_metrics(cc: IngestResult) -> None:
    assert len(cc.requests) == 3
    req = by_rid(cc)
    failed, ok = req["req_a2"].attempts
    assert (failed.outcome, failed.http_status, failed.error_type) == (
        Outcome.HTTP_ERROR, 529, "overloaded")
    assert failed.inferences == () and ok.outcome is Outcome.OK
    assert [a.attempt_no for a in req["req_a2"].attempts] == [0, 1]
    assert all(len(r.attempts) == 1 for k, r in req.items() if k != "req_a2")
    errors = [e for e in cc.events if e.kind is LaneEventKind.API_ERROR]
    assert len(errors) == 1
    assert dict(errors[0].attrs) == {"status": 529, "error_type": "overloaded",
                                     "retry_attempt": 1}
    assert errors[0].lane_key == req["req_a2"].lane_key
    # metric aggregates exist but never become requests
    assert [a.source_kind for a in cc.aggregates] == ["otel.metric"]
    agg = cc.aggregates[0]
    assert (agg.usage.uncached_input, agg.usage.output, agg.usage.cache_read,
            agg.usage.cache_write_unknown) == (40, 1_350, 30_000, 40_200)
    assert agg.reported_cost_nano == 313_560_000 and agg.reported_cost_basis == "provider_estimate"
    assert "aggregates" in cc.capabilities
    assert cc.stats["requests"] == 3 and cc.stats["aggregates"] == 1


def test_int_values_encoded_as_strings_are_parsed(cc: IngestResult) -> None:
    usage = by_rid(cc)["req_a1"].final_attempt.inferences[0].usage
    assert (usage.uncached_input, usage.cache_read, usage.cache_write_unknown, usage.output) == (
        12, 0, 30_000, 800)
    assert by_rid(cc)["req_a1"].ts_start_ms == h.T0 + 1_000  # time − duration_ms
    assert by_rid(cc)["req_a1"].final_attempt.duration_ms == 4_000


def test_no_ttl_split_and_fidelity(cc: IngestResult) -> None:
    for r in cc.requests:
        att = r.final_attempt
        assert att.convention_id == "claude_code.otel"
        assert r.source is not None and r.source.fidelity is Fidelity.NO_TTL_SPLIT
        assert r.source.priority == 20 and r.source.adapter == "otlp"
        inf = att.inferences[0]
        assert inf.usage.cache_write_5m == inf.usage.cache_write_1h == 0
        assert inf.pricing.write_ttl_hint is None  # billing path unknown
    no_split = note(cc, "dq.no_ttl_split")
    assert no_split.count == 3 and no_split.tokens == 40_200


def test_cost_usd_is_only_a_provider_estimate(cc: IngestResult) -> None:
    pricer = FakePricer()
    costs = {}
    for r in cc.requests:
        inf = r.final_attempt.inferences[0]
        assert inf.provider_reported_cost_basis == "provider_estimate"
        costs[r.final_attempt.provider_request_id] = inf.provider_reported_cost_nano
        priced = pricer.price_inference(inf, ts_ms=r.ts_start_ms)
        stripped = pricer.price_inference(
            dataclasses.replace(inf, provider_reported_cost_nano=None,
                                provider_reported_cost_basis=None), ts_ms=r.ts_start_ms)
        assert priced == stripped  # the estimate never enters a priced (billed) figure
        assert priced.figure.basis is not Basis.PROVIDER_ESTIMATE
    assert costs == {"req_a1": 247_560_000, "req_a2": 30_000_000, "req_a3": 36_000_000}


def test_user_email_is_pseudonymized_and_dropped(cc: IngestResult) -> None:
    text = h.blob(cc)
    assert h.CANARY_EMAIL not in text and h.CANARY_EMAIL.lower() not in text
    assert "example.com" not in text and "8d0c5a6e" not in text and "5f3c9e0d2b7a" not in text
    assert {r.attribution.principal for r in cc.requests} == {h.p_of(h.CANARY_EMAIL.lower())}
    assert cc.source.principal_key_id == key_id(h.ORG_KEY)
    assert h.CANARY not in text


def test_team_from_team_map_and_resource_attributes(cc: IngestResult) -> None:
    for r in cc.requests:
        a = r.attribution
        assert a.team == "payments"
        assert (a.cost_center, a.arm, a.wave, a.client_version) == (
            "cc-42", "treatment", "w1", "2.1.270")
        assert a.agent_product == "claude_code"
        assert a.repo is not None and a.repo.startswith("h_")
        assert dict(a.extra) == {"department": "eng"}
    assert dict(cc.aggregates[0].dims)["team"] == "payments"
    assert "attribution.team" in cc.capabilities


def test_raw_body_events_are_counted_and_ignored(cc: IngestResult) -> None:
    assert note(cc, "dq.raw_bodies_ignored").count == 2
    assert h.CANARY not in h.blob(cc)


def test_beta_llm_request_span_sets_ttft_and_exact_lanes(cc: IngestResult) -> None:
    req = by_rid(cc)
    assert req["req_a1"].final_attempt.ttft_ms == 1_200
    assert req["req_a3"].final_attempt.ttft_ms == 850
    lane_map = lanes(cc)
    sub = lane_map[req["req_a3"].lane_key]
    main = lane_map[req["req_a1"].lane_key]
    assert sub.kind is LaneKind.SUBAGENT and sub.lane_exact
    assert main.kind is LaneKind.MAIN and main.lane_exact
    assert sub.parent_lane_key == main.lane_key
    assert req["req_a1"].lane_key == req["req_a2"].lane_key
    assert req["req_a3"].attribution.query_source == "subagent"
    assert {"ttft", "lanes_exact"} <= cc.capabilities
    assert note(cc, "dq.lanes_inferred") is None


def test_tool_results_and_prompts(cc: IngestResult) -> None:
    appended = by_rid(cc)["req_a2"].appended
    assert [(a.kind, a.name, a.n_bytes, a.is_error) for a in appended] == [
        ("tool_result", "Bash", 5_120, False),
        ("tool_result", appended[1].name, 0, True)]
    assert appended[1].name.startswith("h_")  # MCP tool names are hashed
    assert cc.stats["tool_result_tokens"] == 1_300
    prompts = [e for e in cc.events if e.kind is LaneEventKind.HUMAN_PROMPT]
    assert len(prompts) == 1 and prompts[0].lane_key == by_rid(cc)["req_a1"].lane_key
    assert {"appended", "events", "human_prompts"} <= cc.capabilities


def test_quarantine_and_stats(cc: IngestResult) -> None:
    assert [(q.locator, q.reason) for q in cc.quarantined] == [("line:4", "bad_json")]
    assert note(cc, "dq.quarantined").count == 1
    assert cc.stats["ignored_metrics"] == 1


def test_sessions_and_scope(cc: IngestResult) -> None:
    assert [s.session_key for s in cc.sessions] == [stable_id("ses", "otlp", "sess-a")]
    session = cc.sessions[0]
    assert session.source_kind == "claude_code.otel"
    assert session.started_ms == h.T0 + 1_000 and session.ended_ms == h.T0 + 92_000
    assert {lane.cache_scope_key for lane in session.lanes} == {"ws:unknown"}


# ---------------------------------------------------------------------------------------------
# variants built in tmp_path
# ---------------------------------------------------------------------------------------------

def test_without_spans_subagent_calls_share_an_inexact_lane(tmp_path: Path) -> None:
    path = h.write_lines(tmp_path / "o.jsonl", [h.logs([
        api_request(h.T0 + 1_000, "r1"),
        api_request(h.T0 + 2_000, "r2", query_source="subagent"),
        api_request(h.T0 + 3_000, "r3", query_source="subagent"),
        api_request(h.T0 + 4_000, "r4", query_source="auxiliary"),
    ])])
    result = read(path)
    req = by_rid(result)
    assert req["r2"].lane_key == req["r3"].lane_key != req["r1"].lane_key
    lane_map = lanes(result)
    assert lane_map[req["r2"].lane_key].kind is LaneKind.SUBAGENT
    assert not lane_map[req["r2"].lane_key].lane_exact
    assert lane_map[req["r4"].lane_key].kind is LaneKind.HELPER
    assert note(result, "dq.lanes_inferred").count == 3
    assert "lanes_exact" not in result.capabilities


def test_orphan_llm_request_span_becomes_a_request(tmp_path: Path) -> None:
    attrs = {"model": "claude-opus-5-5", "request_id": "r9", "ttft_ms": 300, "agent_id": "ag-1",
             "parent_agent_id": "ag-0", "input_tokens": 5, "output_tokens": 50,
             "cache_read_tokens": 1_000, "cache_creation_tokens": 0, "session.id": "s9"}
    path = h.write_lines(tmp_path / "t.jsonl", [h.spans([
        h.span("claude_code.llm_request", "0000000000000009", h.T0, h.T0 + 2_000, attrs)])])
    result = read(path)
    (req,) = result.requests
    assert req.ts_start_ms == h.T0 and req.final_attempt.duration_ms == 2_000
    assert req.final_attempt.ttft_ms == 300
    lane = lanes(result)[req.lane_key]
    assert lane.kind is LaneKind.SUBAGENT and lane.lane_exact
    assert lane.parent_lane_key == stable_id("ln", "otlp", "s9", "agent", "ag-0")


def test_api_error_joins_by_client_request_id_or_stays_an_event(tmp_path: Path) -> None:
    path = h.write_lines(tmp_path / "o.jsonl", [h.logs([
        h.event("api_error", h.T0 + 500, {**SESSION, "client_request_id": "c1",
                                          "duration_ms": 100}),
        api_request(h.T0 + 2_000, "r1", client_request_id="c1", attempt=2),
        h.event("api_error", h.T0 + 9_000, {**SESSION, "request_id": "gone",
                                            "status_code": 429, "query_source": "subagent"}),
    ])])
    result = read(path)
    (req,) = result.requests
    failed = req.attempts[0]
    assert (failed.outcome, failed.http_status, failed.error_type) == (
        Outcome.NETWORK_ERROR, None, "connection")
    assert failed.ts_start_ms == h.T0 + 400
    assert req.final_attempt.sdk_retry_count == 1
    assert len([e for e in result.events if e.kind is LaneEventKind.API_ERROR]) == 2
    assert result.stats["api_errors_unjoined"] == 1
    assert "attempts" in result.capabilities


def test_duplicate_events_and_subscription_ttl_hints(tmp_path: Path) -> None:
    path = h.write_lines(tmp_path / "o.jsonl", [h.logs([
        api_request(h.T0 + 1_000, "r1", cache_creation_tokens=500),
        api_request(h.T0 + 1_000, "r1", cache_creation_tokens=500),
        api_request(h.T0 + 5_000, "r2", query_source="subagent", cache_creation_tokens=100),
    ])])
    opts_attr = Attribution(billing_path="subscription", team="core")
    result = read(path, attribution=opts_attr)
    assert len(result.requests) == 2 and result.stats["duplicate_records"] == 1
    hints = {r.final_attempt.provider_request_id:
             r.final_attempt.inferences[0].pricing.write_ttl_hint for r in result.requests}
    assert hints == {"r1": "1h", "r2": "5m"}
    assert {r.final_attempt.inferences[0].pricing.billing_path for r in result.requests} == {
        "subscription"}
    api = read(path, attribution=Attribution(billing_path="api_key"))
    assert {r.final_attempt.inferences[0].pricing.write_ttl_hint for r in api.requests} == {"5m"}
    cloud = read(path, attribution=Attribution(billing_path="bedrock"))
    ctx = cloud.requests[0].final_attempt.inferences[0].pricing
    assert (ctx.channel, ctx.write_ttl_hint) == ("bedrock", "5m")
    assert {lane.cache_scope_key for s in cloud.sessions for lane in s.lanes} == {
        "org:bedrock:unknown"}


def test_fast_speed_effort_unknown_model_and_endpoint_scope(tmp_path: Path) -> None:
    path = h.write_lines(tmp_path / "o.jsonl", [h.logs([
        api_request(h.T0, "r1", speed="fast", effort="xhigh"),
        api_request(h.T0 + 10, "r2", model="opus"),
    ])])
    result = read(path, attribution=Attribution(extra=(("endpoint_scope", "regional"),)))
    req = by_rid(result)
    ctx = req["r1"].final_attempt.inferences[0].pricing
    assert (ctx.speed, ctx.endpoint_scope) == ("fast", "regional")
    assert req["r1"].params.effort == "xhigh" and req["r1"].params.speed == "fast"
    assert req["r2"].final_attempt.inferences[0].pricing.model == ""
    assert note(result, "dq.unpriced_model").count == 1


def test_tool_result_follows_the_tool_span_lane(tmp_path: Path) -> None:
    span_attrs = {"request_id": "r2", "agent_id": "ag", "model": "claude-sonnet-5",
                  "input_tokens": 1, "output_tokens": 1, "session.id": "sess-x"}
    path = h.write_lines(tmp_path / "o.jsonl", [
        h.logs([api_request(h.T0 + 1_000, "r1"),
                api_request(h.T0 + 9_000, "r2", query_source="subagent"),
                h.event("tool_result", h.T0 + 5_000, {**SESSION, "tool_name": "Read",
                                                      "tool_use_id": "tu", "success": True,
                                                      "tool_result_size_bytes": 99}),
                h.event("tool_result", h.T0 + 99_000, {**SESSION, "tool_name": "Read"})]),
        h.spans([h.span("claude_code.llm_request", "0000000000000002", h.T0 + 8_000,
                        h.T0 + 9_000, span_attrs),
                 h.span("claude_code.tool", "0000000000000003", h.T0 + 4_000, h.T0 + 5_000,
                        {"tool_use_id": "tu", "agent_id": "ag", "tool_name": "Read"})]),
    ])
    result = read(path)
    req = by_rid(result)
    assert [(a.name, a.n_bytes) for a in req["r2"].appended] == [("Read", 99)]
    assert req["r1"].appended == ()
    assert result.stats["tool_results_unattached"] == 1


def test_metrics_only_file_and_cumulative_temporality(tmp_path: Path) -> None:
    user = {"user.email": "a@example.com", "model": "claude-sonnet-5"}
    cumulative = {"name": "claude_code.token.usage", "sum": {
        "aggregationTemporality": 2, "dataPoints": [
            h.point(h.T0, h.T0 + 60_000, 100, {**user, "type": "input"}),
            h.point(h.T0, h.T0 + 120_000, 250, {**user, "type": "input"}),
            h.point(h.T0, h.T0 + 120_000, 7, {**user, "type": "output"}),
            h.point(h.T0, h.T0 + 120_000, 7, {**user, "type": "mystery"}),
            h.point(h.T0, h.T0 + 120_000, 7.5, {**user, "type": "output", "speed": "fast"})]}}
    cost = {"name": "claude_code.cost.usage", "gauge": {"dataPoints": [
        h.point(h.T0, h.T0 + 120_000, 0.5, user),
        h.point(h.T0, h.T0 + 120_000, -1.0, {**user, "speed": "fast"}),
        {"attributes": [], "asDouble": 1.0}]}}
    path = h.write_lines(tmp_path / "m.jsonl", [h.metrics([cumulative, cost,
                                                           {"name": "claude_code.token.usage"}])])
    result = read(path, team_map=(("a@example.com", "mobile"),))
    assert result.requests == []
    (agg,) = [a for a in result.aggregates if a.usage.uncached_input]
    assert agg.usage.uncached_input == 250 and agg.usage.output == 7  # latest cumulative point
    assert agg.reported_cost_nano == 500_000_000
    assert dict(agg.dims)["team"] == "mobile" and (agg.bucket_start_ms, agg.bucket_end_ms) == (
        h.T0, h.T0 + 120_000)
    assert result.stats["bad_metric_points"] == 3
    assert [q.reason for q in result.quarantined] == ["missing:timeUnixNano", "missing:sum"]
    assert result.capabilities == frozenset({"aggregates"})


def test_zst_needs_python_314(tmp_path: Path) -> None:
    if importlib.util.find_spec("compression") is not None and importlib.util.find_spec(
            "compression.zstd") is not None:  # pragma: no cover - Python >= 3.14
        pytest.skip("compression.zstd available")
    path = tmp_path / "otel.jsonl.zst"
    path.write_bytes(b"(\xb5/\xfd\x00\x00")
    with pytest.raises(SourceError, match="compression: none"):
        read(path)


def test_gzip_and_plain_give_the_same_records(tmp_path: Path) -> None:
    gz = tmp_path / "otel.jsonl.gz"
    gz.write_bytes(gzip.compress(h.OTLP_CC.read_bytes(), mtime=0))
    plain, zipped = read(h.OTLP_CC), read(gz)
    assert [r.request_id for r in zipped.requests] != []
    assert [r.final_attempt.inferences[0].usage for r in plain.requests] == [
        r.final_attempt.inferences[0].usage for r in zipped.requests]
    assert ADAPTER.sniff(gz, gzip.decompress(gz.read_bytes())[:65536])


def test_timestamps_numbers_and_event_names(tmp_path: Path) -> None:
    no_time = h.event("api_request", 0, {**SESSION, "request_id": "r0", "input_tokens": 1,
                                         "event.timestamp": "2026-09-23T09:00:05Z"})
    del no_time["timeUnixNano"]
    by_attr = api_request(h.T0, "r1")
    del by_attr["body"]
    by_event_name = api_request(h.T0, "r2")
    by_event_name["eventName"] = "claude_code.api_request"
    by_event_name["attributes"] = by_event_name["attributes"][1:]
    del by_event_name["body"]
    as_numbers = api_request(h.T0, "r3")
    as_numbers["timeUnixNano"] = h.T0 * 1_000_000
    for kv in as_numbers["attributes"]:
        if "intValue" in kv["value"]:
            kv["value"]["intValue"] = int(kv["value"]["intValue"])
        if kv["key"] == "output_tokens":
            kv["value"] = {"doubleValue": 100.0}
    lost = h.event("api_request", 0, {**SESSION, "request_id": "r4"})
    del lost["timeUnixNano"]
    unknown = h.event("tool_decision", h.T0, SESSION)
    path = h.write_lines(tmp_path / "o.jsonl", [h.logs([no_time, by_attr, by_event_name,
                                                        as_numbers, lost, unknown, "x"])])
    result = read(path)
    req = by_rid(result)
    assert set(req) == {"r0", "r1", "r2", "r3"}
    assert req["r0"].ts_start_ms == h.T0 + 5_000
    assert req["r3"].final_attempt.inferences[0].usage.output == 100
    assert [q.reason for q in result.quarantined] == ["missing:timeUnixNano"]
    assert result.stats["ignored_log_records"] == 1


def test_bad_usage_and_missing_usage_are_quarantined(tmp_path: Path) -> None:
    path = h.write_lines(tmp_path / "o.jsonl", [
        h.logs([api_request(h.T0, "r1", input_tokens="many"),
                h.event("api_request", h.T0, {**SESSION, "request_id": "r2"})]),
        {"something": "else"}, [1, 2], "not json",
    ])
    result = read(path)
    assert result.requests == []
    assert sorted(q.reason for q in result.quarantined) == sorted([
        "bad_usage", "missing:input_tokens", "missing:resource", "not_object", "bad_json"])
    with pytest.raises(SourceError, match="line:2: missing:resource"):
        read(path, lenient=False)
    only_bad = h.write_lines(tmp_path / "b.jsonl", [h.logs([
        api_request(h.T0, "r1", input_tokens="many")])])
    with pytest.raises(SourceError, match="line:1#1: bad_usage"):
        read(only_bad, lenient=False)


def test_since_until_window(tmp_path: Path) -> None:
    path = h.write_lines(tmp_path / "o.jsonl", [h.logs([
        api_request(h.T0 + 1_000, "early"), api_request(h.T0 + 50_000, "late"),
        h.event("user_prompt", h.T0 - 5, SESSION)])])
    result = read(path, since_ms=h.T0 + 10_000, until_ms=h.T0 + 100_000)
    assert set(by_rid(result)) == {"late"} and result.stats["outside_window"] == 1
    assert result.events == []


def test_central_collector_modes_never_emit_raw_ids(tmp_path: Path) -> None:
    path = h.write_lines(tmp_path / "o.jsonl", [h.logs([api_request(h.T0, "r1")])])
    central = read(path, identity_mode="central", principal_ref="dev-01", principal_key=None,
                   principal_key_id=None)
    assert central.requests[0].attribution.principal == "r_dev-01"
    assert central.source.principal_key_id is None
    no_key = read(path, principal_key=None)
    assert no_key.requests[0].attribution.principal is None


# ---------------------------------------------------------------------------------------------
# GenAI and OpenInference spans
# ---------------------------------------------------------------------------------------------

def test_genai_fixture_new_and_legacy_names() -> None:
    result = read(h.OTLP_GENAI)
    assert len(result.requests) == 3  # embeddings and the invoke_agent roll-up are not requests
    by_model = {r.final_attempt.inferences[0].pricing.model: r for r in result.requests}
    new = by_model["gpt-5.6-sol"]
    assert new.final_attempt.convention_id == "otel.genai"
    u = new.final_attempt.inferences[0].usage
    assert (u.uncached_input, u.cache_read, u.cache_write_unknown, u.output,
            u.output_reasoning) == (2_000, 6_000, 2_000, 500, 120)
    ctx = new.final_attempt.inferences[0].pricing
    assert (ctx.provider, ctx.channel, ctx.service_tier) == ("openai", "openai_api", "flex")
    assert new.final_attempt.provider_request_id == "resp_genai_1"
    assert new.params.max_tokens == 4_096
    legacy = by_model["claude-sonnet-5"]
    assert legacy.final_attempt.convention_id == "otel.genai.legacy"
    assert legacy.final_attempt.inferences[0].usage.cache_write_unknown == 1_000
    assert legacy.final_attempt.provider_message_id == "msg_legacy_1"
    assert legacy.request_id == request_id_for("anthropic", "msg_legacy_1", "", "")
    assert legacy.lane_key == new.lane_key  # one conversation, one lane
    mismatch = by_model["claude-opus-5-5"]
    mu = mismatch.final_attempt.inferences[0].usage
    assert (mu.uncached_input, mu.cache_read, mu.cache_write_unknown) == (12, 40_000, 900)
    assert note(result, "dq.convention_mismatch").count == 1
    lane = lanes(result)[mismatch.lane_key]
    assert lane.lane_exact and lane.kind is LaneKind.API_RUN  # lane of the invoke_agent span
    assert h.CANARY not in h.blob(result)
    assert all(r.source.fidelity is Fidelity.NO_TTL_SPLIT for r in result.requests)


def test_openinference_agent_wrapping_two_llm_spans_gives_two_requests() -> None:
    result = read(h.OTLP_OI)
    trace_requests = [r for r in result.requests
                      if r.final_attempt.inferences[0].pricing.model == "gpt-5.6-sol"]
    assert len(trace_requests) == 2  # the AGENT/CHAIN/TOOL spans' roll-ups are never requests
    assert len({r.lane_key for r in trace_requests}) == 1
    lane = lanes(result)[trace_requests[0].lane_key]
    assert lane.lane_exact and lane.kind is LaneKind.API_RUN
    assert [r.seq for r in sorted(trace_requests, key=lambda r: r.ts_start_ms)] == [0, 1]
    first = trace_requests[0].final_attempt.inferences[0].usage
    assert (first.uncached_input, first.cache_read, first.output, first.output_reasoning) == (
        952, 2_048, 200, 50)
    assert result.stats["openinference_non_llm_spans"] == 3
    assert trace_requests[0].session_key == stable_id("ses", "otlp", "oi-session-1")


def test_crewai_style_span_fails_the_sum_check() -> None:
    result = read(h.OTLP_OI)
    crew = next(r for r in result.requests
                if r.final_attempt.inferences[0].pricing.model == "claude-sonnet-5")
    usage = crew.final_attempt.inferences[0].usage
    assert (usage.uncached_input, usage.cache_read, usage.total_input) == (17, 17_102, 17_119)
    assert note(result, "dq.sum_check_failed").count == 1
    assert not lanes(result)[crew.lane_key].lane_exact  # a leaf LLM span is its own lane
    assert note(result, "dq.lanes_inferred").count == 1


def test_span_errors_and_missing_usage(tmp_path: Path) -> None:
    base = {"gen_ai.operation.name": "chat", "gen_ai.provider.name": "openai",
            "gen_ai.request.model": "gpt-5.6-sol"}
    path = h.write_lines(tmp_path / "s.jsonl", [h.spans([
        h.span("chat", "0000000000000001", h.T0, h.T0 + 10, {**base, "error.type": "429"},
               error=True),
        h.span("chat", "0000000000000002", h.T0, h.T0 + 10,
               {**base, "error.type": "RateLimitError"}, error=True),
        h.span("chat", "0000000000000003", h.T0, h.T0 + 10, base),
        h.span("chat", "0000000000000004", h.T0, h.T0 + 10,
               {"gen_ai.operation.name": "text_completion", "gen_ai.provider.name": "mistral_ai",
                "gen_ai.usage.input_tokens": 3, "gen_ai.usage.output_tokens": 4}),
        h.span("llm", "0000000000000005", h.T0, h.T0 + 10,
               {"openinference.span.kind": "LLM", "llm.provider": "aws",
                "llm.model_name": "us.anthropic.claude-opus-5", "llm.token_count.prompt": 9}),
        h.span("llm", "0000000000000006", h.T0, h.T0 + 10,
               {"openinference.span.kind": "LLM", "llm.system": "cohere",
                "llm.model_name": "command", "llm.token_count.prompt": 9}),
        {"name": "no ids", "attributes": []},
    ])])
    result = read(path)
    outcomes = sorted((r.final_attempt.outcome.value, r.final_attempt.http_status,
                       r.final_attempt.error_type) for r in result.requests
                      if not r.final_attempt.inferences)
    assert outcomes == [("http_error", 429, "rate_limit"), ("unknown", None, "RateLimitError")]
    assert [q.reason for q in result.quarantined] == ["missing:usage"]
    ctxs = {r.final_attempt.inferences[0].pricing.provider: r.final_attempt.inferences[0].pricing
            for r in result.requests if r.final_attempt.inferences}
    assert ctxs["mistral_ai"].channel == "unknown"
    bedrock = ctxs["anthropic"]
    assert (bedrock.channel, bedrock.model, bedrock.endpoint_scope) == (
        "bedrock", "claude-opus-5", "regional")
    assert ctxs["cohere"].channel == "unknown"
    assert result.stats["ignored_spans"] == 1


# ---------------------------------------------------------------------------------------------
# sniffing and decoding
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path, expected", [
    (h.OTLP_CC, True), (h.OTLP_GENAI, True), (h.OTLP_OI, True), (h.OPENAI, False),
    (h.BEDROCK, False), (h.ANTHROPIC, False)])
def test_sniff(path: Path, expected: bool) -> None:
    assert ADAPTER.sniff(path, path.read_bytes()[:65536]) is expected


def test_sniff_huge_first_line() -> None:
    head = b'{"resourceSpans": [' + b"x" * 70_000
    assert ADAPTER.sniff(Path("x"), head[:65536])
    assert not ADAPTER.sniff(Path("x"), b'{"other": [' + b"x" * 70_000)
    assert not ADAPTER.sniff(Path("x"), b"")


def test_decode_any_value() -> None:
    assert _decode({"intValue": "-5"}) == -5
    assert _decode({"intValue": "x"}) is None
    assert _decode({"intValue": 3}) == 3
    assert _decode({"doubleValue": 1.5}) == 1.5
    assert _decode({"doubleValue": "NaN"}) is None
    assert _decode({"boolValue": "true"}) is None
    assert _decode({"stringValue": 3}) is None
    assert _decode({"arrayValue": {"values": [{"stringValue": "a"}, {"intValue": "2"}]}}) == [
        "a", 2]
    assert _decode({"arrayValue": {}}) == []
    assert _decode({"kvlistValue": {"values": [{"key": "k", "value": {"boolValue": True}}]}}) == {
        "k": True}
    assert _decode({"bytesValue": "AAAA"}) is None
    assert _decode([1]) is None
    deep: Any = {"stringValue": "x"}
    for _ in range(12):
        deep = {"arrayValue": {"values": [deep]}}
    assert "x" not in repr(_decode(deep))


def test_adapter_surface() -> None:
    assert ADAPTER.name == "otlp"
    assert {"usage_sequence", "timing", "ttft", "aggregates"} <= ADAPTER.capabilities
    assert InferenceKind.MESSAGE.value == "message"


def test_resource_labels_never_carry_emails_or_paths(tmp_path: Path) -> None:
    resource = {"team.id": "someone@example.com", "cost_center": "/srv/finance",
                "department": "Data Platform", "tokenbill.arm": "control"}
    path = h.write_lines(tmp_path / "o.jsonl", [h.logs([api_request(h.T0, "r1")], resource)])
    (req,) = read(path, team_map=()).requests
    a = req.attribution
    assert (a.team, a.cost_center, a.arm) == (None, None, "control")
    assert dict(a.extra) == {"department": "Data Platform"}
    assert "someone" not in h.blob(read(path, team_map=())) and "/srv" not in repr(a)
