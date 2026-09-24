"""Write the TELEM fixture files (synthetic; schema from SPEC §19.4 and the primary sources in
tests/v2/telemetry/README.md). Run as a script from the repository root:

    python tests/v2/fixtures/telemetry/build_fixtures.py

Deterministic: the same bytes on every run. Content-bearing fields (prompts, tool parameters,
message text, raw API bodies, emails, IAM session names) carry the content canary so tests can
prove nothing leaks. Timestamps start at 2026-09-23T09:00:00Z.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
CANARY = "TB-CANARY-7f3a91"
CANARY_EMAIL = f"canary.{CANARY}@example.com"
T0 = 1_790_154_000_000  # 2026-09-23T09:00:00Z, ms


def ns(ms: int) -> str:
    """OTLP/JSON fixed64 nanoseconds are encoded as decimal strings."""
    return str(ms * 1_000_000)


def attr(key: str, value: Any) -> dict[str, Any]:
    """One OTLP attribute (ints as intValue strings, as the OTLP/JSON encoding requires)."""
    if isinstance(value, bool):
        v: dict[str, Any] = {"boolValue": value}
    elif isinstance(value, int):
        v = {"intValue": str(value)}
    elif isinstance(value, float):
        v = {"doubleValue": value}
    elif isinstance(value, list):
        v = {"arrayValue": {"values": [{"stringValue": x} for x in value]}}
    else:
        v = {"stringValue": value}
    return {"key": key, "value": v}


def attrs(**kw: Any) -> list[dict[str, Any]]:
    return [attr(k.replace("__", "."), v) for k, v in kw.items()]


def event(name: str, t_ms: int, **kw: Any) -> dict[str, Any]:
    return {"timeUnixNano": ns(t_ms), "observedTimeUnixNano": ns(t_ms + 3),
            "severityNumber": 9, "body": {"stringValue": f"claude_code.{name}"},
            "attributes": [attr("event.name", name)] + attrs(**kw)}


def span(name: str, trace: str, span_id: str, parent: str | None, start: int, end: int,
         **kw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"traceId": trace, "spanId": span_id, "name": name, "kind": 3,
                           "startTimeUnixNano": ns(start), "endTimeUnixNano": ns(end),
                           "attributes": attrs(**kw), "status": {}}
    if parent:
        out["parentSpanId"] = parent
    return out


def resource(**kw: Any) -> dict[str, Any]:
    return {"attributes": attrs(**kw)}


def write(name: str, records: list[Any], out: Path = HERE) -> None:
    with open(out / name, "w", encoding="utf-8", newline="\n") as f:
        for rec in records:
            if isinstance(rec, str):
                f.write(rec + "\n")
            else:
                f.write(json.dumps(rec, sort_keys=True, separators=(",", ":")) + "\n")


# ---------------------------------------------------------------------------------------------
# Claude Code OTel export: logs, traces (beta), metrics
# ---------------------------------------------------------------------------------------------

def claude_code() -> list[Any]:
    user = {"session__id": "sess-a", "user__email": CANARY_EMAIL,
            "user__account_uuid": "8d0c5a6e-0000-4000-8000-00000000a11c",
            "user__id": "5f3c9e0d2b7a", "organization__id": "org-uuid-1"}
    res = resource(service__name="claude-code", cost_center="cc-42", department="eng",
                   tokenbill__arm="treatment", tokenbill__wave="w1", app__version="2.1.270",
                   vcs__repository__url__full=f"https://github.com/acme/{CANARY}-repo")
    logs = [
        event("user_prompt", T0, prompt=f"refactor the billing code {CANARY}",
              prompt_length=31, **user),
        event("api_request", T0 + 5_000, model="claude-opus-5-5", request_id="req_a1",
              client_request_id="c-a1", duration_ms=4_000, input_tokens=12, output_tokens=800,
              cache_read_tokens=0, cache_creation_tokens=30_000, cost_usd=0.24756,
              query_source="repl_main_thread", effort="high", attempt=1, stop_reason="tool_use",
              **user),
        event("tool_result", T0 + 30_000, tool_name="Bash", tool_use_id="toolu_1",
              success="true", duration_ms=1_200, tool_result_size_bytes=5_120,
              tool_parameters=f'{{"command": "cat secrets {CANARY}"}}', **user),
        event("tool_result", T0 + 31_000, tool_name="mcp__vault__lookup",
              tool_use_id="toolu_2", success="false", duration_ms=40,
              error=f"denied {CANARY}", **user),
        event("api_error", T0 + 60_200, model="claude-opus-5-5", request_id="req_a2",
              client_request_id="c-a2", duration_ms=200, status_code=529, attempt=1,
              error=f"Overloaded {CANARY}", query_source="repl_main_thread:outputStyle:Concise",
              **user),
        event("api_request", T0 + 64_000, model="claude-opus-5-5", request_id="req_a2",
              client_request_id="c-a2", duration_ms=3_000, input_tokens=8, output_tokens=400,
              cache_read_tokens=30_000, cache_creation_tokens=1_200, cost_usd="0.03",
              query_source="repl_main_thread:outputStyle:Concise", effort="high", attempt=2,
              stop_reason="end_turn", **user),
        event("api_request", T0 + 92_000, model="claude-sonnet-5", request_id="req_a3",
              client_request_id="c-a3", duration_ms=2_000, input_tokens=20, output_tokens=150,
              cache_read_tokens=0, cache_creation_tokens=9_000, cost_usd=0.036,
              query_source="agent:builtin:Explore", attempt=1, **user),
        event("api_request_body", T0 + 1_000, request_id="req_a1",
              api_request_body=json.dumps({"messages": [{"content": CANARY}]}), **user),
        event("api_response_body", T0 + 5_000, request_id="req_a1",
              api_response_body=json.dumps({"content": [{"text": CANARY}]}), **user),
    ]
    trace = "5b8efff798038103d269b633813fc60c"
    spans = [
        span("claude_code.llm_request", trace, "a1a1a1a1a1a1a1a1", None, T0 + 1_000, T0 + 5_000,
             model="claude-opus-5-5", request_id="req_a1", ttft_ms=1_200,
             query_source="repl_main_thread", input_tokens=12, output_tokens=800,
             cache_read_tokens=0, cache_creation_tokens=30_000, session__id="sess-a"),
        span("claude_code.llm_request", trace, "a3a3a3a3a3a3a3a3", None, T0 + 90_000,
             T0 + 92_000, model="claude-sonnet-5", request_id="req_a3", ttft_ms=850,
             agent_id="agent-7", query_source="agent:builtin:Explore", input_tokens=20,
             output_tokens=150, cache_read_tokens=0, cache_creation_tokens=9_000,
             session__id="sess-a"),
        span("claude_code.tool", trace, "b1b1b1b1b1b1b1b1", None, T0 + 28_800, T0 + 30_000,
             tool_name="Bash", tool_use_id="toolu_1", result_tokens=1_300,
             full_command=f"cat secrets {CANARY}", session__id="sess-a"),
    ]
    metrics = []
    for typ, value in (("input", 40), ("output", 1_350), ("cacheRead", 30_000),
                       ("cacheCreation", 40_200)):
        metrics.append({"attributes": attrs(type=typ, model="claude-opus-5-5",
                                            query_source="main", **user),
                        "startTimeUnixNano": ns(T0), "timeUnixNano": ns(T0 + 120_000),
                        "asInt": str(value)})
    return [
        {"resourceLogs": [{"resource": res, "scopeLogs": [
            {"scope": {"name": "com.anthropic.claude_code.events"}, "logRecords": logs}]}]},
        {"resourceSpans": [{"resource": res, "scopeSpans": [
            {"scope": {"name": "com.anthropic.claude_code.tracing"}, "spans": spans}]}]},
        {"resourceMetrics": [{"resource": res, "scopeMetrics": [{"metrics": [
            {"name": "claude_code.token.usage", "unit": "tokens",
             "sum": {"aggregationTemporality": 1, "isMonotonic": True, "dataPoints": metrics}},
            {"name": "claude_code.cost.usage", "unit": "USD",
             "sum": {"aggregationTemporality": 1, "isMonotonic": True, "dataPoints": [
                 {"attributes": attrs(model="claude-opus-5-5", **user),
                  "startTimeUnixNano": ns(T0), "timeUnixNano": ns(T0 + 120_000),
                  "asDouble": 0.31356}]}},
            {"name": "claude_code.session.count", "sum": {"dataPoints": [
                {"attributes": attrs(**user), "timeUnixNano": ns(T0), "asInt": "1"}]}},
        ]}]}]},
        '{"resourceLogs": [truncated',
    ]


# ---------------------------------------------------------------------------------------------
# OTel GenAI spans (semconv >= 1.42 and the 1.40-1.41 legacy names)
# ---------------------------------------------------------------------------------------------

def genai() -> list[Any]:
    t = "0af7651916cd43dd8448eb211c80319c"
    t2 = "1bf7651916cd43dd8448eb211c80319d"
    spans = [
        span("chat gpt-5.6-sol", t, "0000000000000001", None, T0, T0 + 2_000,
             gen_ai__operation__name="chat", gen_ai__provider__name="openai",
             gen_ai__request__model="gpt-5.6-sol", gen_ai__response__model="gpt-5.6-sol",
             gen_ai__response__id="resp_genai_1", gen_ai__conversation__id="conv-1",
             openai__response__service_tier="flex", gen_ai__request__max_tokens=4_096,
             gen_ai__usage__input_tokens=10_000, gen_ai__usage__cache_read__input_tokens=6_000,
             gen_ai__usage__cache_write__input_tokens=2_000, gen_ai__usage__output_tokens=500,
             gen_ai__usage__reasoning__output_tokens=120,
             gen_ai__input__messages=f'[{{"role":"user","content":"{CANARY}"}}]'),
        span("chat claude-sonnet-5", t, "0000000000000002", None, T0 + 60_000, T0 + 63_000,
             gen_ai__operation__name="chat", gen_ai__system="anthropic",
             gen_ai__request__model="claude-sonnet-5", gen_ai__response__id="msg_legacy_1",
             gen_ai__conversation__id="conv-1", gen_ai__usage__input_tokens=5_000,
             gen_ai__usage__cache_read__input_tokens=3_000,
             gen_ai__usage__cache_creation__input_tokens=1_000,
             gen_ai__usage__output_tokens=200),
        span("chat claude-opus-5-5", t2, "0000000000000003", "00000000000000a0", T0 + 120_000,
             T0 + 125_000, gen_ai__operation__name="chat", gen_ai__provider__name="anthropic",
             gen_ai__request__model="claude-opus-5-5", gen_ai__response__id="msg_mismatch_1",
             gen_ai__usage__input_tokens=12, gen_ai__usage__cache_read__input_tokens=40_000,
             gen_ai__usage__cache_write__input_tokens=900, gen_ai__usage__output_tokens=300),
        span("invoke_agent planner", t2, "00000000000000a0", None, T0 + 110_000, T0 + 130_000,
             gen_ai__operation__name="invoke_agent", gen_ai__agent__name="planner",
             gen_ai__usage__input_tokens=99_999, gen_ai__usage__output_tokens=99_999),
        span("embeddings", t2, "0000000000000004", None, T0 + 140_000, T0 + 140_500,
             gen_ai__operation__name="embeddings", gen_ai__provider__name="openai",
             gen_ai__usage__input_tokens=800),
    ]
    return [{"resourceSpans": [{"resource": resource(service__name="support-bot"),
                                "scopeSpans": [{"spans": spans}]}]}]


# ---------------------------------------------------------------------------------------------
# OpenInference: one AGENT span wrapping two LLM spans; a CrewAI-style defect
# ---------------------------------------------------------------------------------------------

def openinference() -> list[Any]:
    t = "2cf7651916cd43dd8448eb211c80319e"
    t2 = "3df7651916cd43dd8448eb211c80319f"
    llm = {"openinference__span__kind": "LLM", "llm__provider": "openai",
           "llm__system": "openai", "llm__model_name": "gpt-5.6-sol"}
    spans = [
        span("Agent.run", t, "00000000000000f0", None, T0, T0 + 30_000,
             openinference__span__kind="AGENT", session__id="oi-session-1",
             llm__token_count__prompt=9_999, llm__token_count__completion=9_999,
             input__value=f"plan the trip {CANARY}"),
        span("ChatCompletion", t, "00000000000000f1", "00000000000000f0", T0 + 1_000,
             T0 + 4_000, session__id="oi-session-1", llm__token_count__prompt=3_000,
             llm__token_count__prompt_details__cache_read=2_048,
             llm__token_count__prompt_details__cache_write=0,
             llm__token_count__completion=200, llm__token_count__total=3_200,
             llm__token_count__completion_details__reasoning=50,
             output__value=f"step one {CANARY}", **llm),
        span("chain", t, "00000000000000f2", "00000000000000f0", T0 + 5_000, T0 + 20_000,
             openinference__span__kind="CHAIN", llm__token_count__prompt=4_000),
        span("ChatCompletion", t, "00000000000000f3", "00000000000000f2", T0 + 6_000,
             T0 + 9_000, session__id="oi-session-1", llm__token_count__prompt=3_300,
             llm__token_count__prompt_details__cache_read=3_072,
             llm__token_count__completion=150, llm__token_count__total=3_450, **llm),
        span("search", t, "00000000000000f4", "00000000000000f0", T0 + 21_000, T0 + 22_000,
             openinference__span__kind="TOOL", tool__name="web_search"),
        span("crew.llm", t2, "00000000000000e1", None, T0 + 60_000, T0 + 64_000,
             openinference__span__kind="LLM", llm__provider="anthropic",
             llm__system="anthropic", llm__model_name="claude-sonnet-5",
             llm__token_count__prompt=17, llm__token_count__prompt_details__cache_read=17_102,
             llm__token_count__completion=350, llm__token_count__total=367),
    ]
    return [{"resourceSpans": [{"resource": resource(service__name="trip-agent"),
                                "scopeSpans": [{"spans": spans}]}]}]


# ---------------------------------------------------------------------------------------------
# OpenAI Responses / Chat Completions
# ---------------------------------------------------------------------------------------------

def openai() -> list[Any]:
    s0 = T0 // 1000
    content = [{"type": "message", "content": [{"type": "output_text", "text": CANARY}]}]
    return [
        {"id": "resp_1", "object": "response", "created_at": s0, "status": "completed",
         "model": "gpt-5.6-sol", "service_tier": "flex", "output": content,
         "instructions": f"be terse {CANARY}", "previous_response_id": None,
         "reasoning": {"effort": "medium"}, "max_output_tokens": 8_192,
         "usage": {"input_tokens": 10_000,
                   "input_tokens_details": {"cached_tokens": 6_000, "cache_write_tokens": 2_000},
                   "output_tokens": 500, "output_tokens_details": {"reasoning_tokens": 100},
                   "total_tokens": 10_500},
         "prompt_cache_diagnostics": {"type": "cache_miss", "reason": "tools_changed",
                                      "comparison_reusable_tokens": 5_629,
                                      "cache_missed_tokens": 5_629}},
        {"id": "resp_2", "object": "response", "created_at": s0 + 120, "status": "incomplete",
         "incomplete_details": {"reason": "max_output_tokens"}, "model": "gpt-5.6-sol",
         "service_tier": "priority", "output": content, "previous_response_id": "resp_1",
         "usage": {"input_tokens": 10_800,
                   "input_tokens_details": {"cached_tokens": 10_240, "cache_write_tokens": 512},
                   "output_tokens": 8_192, "output_tokens_details": {"reasoning_tokens": 0},
                   "total_tokens": 18_992}},
        {"id": "chatcmpl-1", "object": "chat.completion", "created": s0 + 300,
         "model": "gpt-5.4-mini", "service_tier": "default",
         "choices": [{"index": 0, "finish_reason": "stop",
                      "message": {"role": "assistant", "content": CANARY}}],
         "usage": {"prompt_tokens": 2_000, "completion_tokens": 300, "total_tokens": 2_300,
                   "prompt_tokens_details": {"cached_tokens": 1_024},
                   "completion_tokens_details": {"reasoning_tokens": 64,
                                                 "accepted_prediction_tokens": 0,
                                                 "rejected_prediction_tokens": 10}}},
        {"request_meta": {"ts_ms": T0 + 600_000, "channel": "azure_openai",
                          "subscription_id": "sub-0000-azure", "session": "support-42",
                          "attribution": {"team": "search", "principal": CANARY_EMAIL}},
         "response": {"id": "resp_az_1", "object": "response", "created_at": s0 + 600,
                      "status": "completed", "model": "gpt-5.4", "service_tier": "default",
                      "output": content,
                      "usage": {"input_tokens": 4_000,
                                "input_tokens_details": {"cached_tokens": 3_968},
                                "output_tokens": 50, "total_tokens": 4_050}}},
        {"id": "batch_req_1", "custom_id": f"row-{CANARY}",
         "response": {"status_code": 200, "request_id": "req_batch_1",
                      "body": {"id": "chatcmpl-batch-1", "object": "chat.completion",
                               "created": s0 + 900, "model": "gpt-5.6-sol",
                               "choices": [{"index": 0, "finish_reason": "length",
                                            "message": {"role": "assistant",
                                                        "content": CANARY}}],
                               "usage": {"prompt_tokens": 1_000, "completion_tokens": 64,
                                         "total_tokens": 1_064}}},
         "error": None},
        {"id": "resp_bad", "object": "response", "created_at": s0 + 1_000, "status": "completed",
         "model": "gpt-5.6-sol",
         "usage": {"input_tokens": 100,
                   "input_tokens_details": {"cached_tokens": 90, "cache_write_tokens": 20},
                   "output_tokens": 5, "total_tokens": 105}},
        {"id": "resp_3", "object": "response", "created_at": s0 + 1_200, "status": "completed",
         "model": "gpt-5.6-sol", "previous_response_id": "resp_2",
         "usage": {"input_tokens": 11_000,
                   "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                   "output_tokens": 40, "total_tokens": 11_040},
         "prompt_cache_diagnostics": {"type": "comparison_response_not_found"}},
    ]


# ---------------------------------------------------------------------------------------------
# Bedrock invocation logs and Converse responses
# ---------------------------------------------------------------------------------------------

def bedrock() -> list[Any]:
    arn = f"arn:aws:sts::123456789012:assumed-role/DevRole/{CANARY_EMAIL}"
    base = {"schemaType": "ModelInvocationLog", "schemaVersion": "1.0", "accountId":
            "123456789012", "region": "us-east-1", "identity": {"arn": arn}}
    converse_body = {
        "output": {"message": {"role": "assistant", "content": [{"text": CANARY}]}},
        "stopReason": "end_turn", "metrics": {"latencyMs": 2_500},
        "serviceTier": {"type": "default"},
        "usage": {"inputTokens": 100, "outputTokens": 500, "totalTokens": 30_600,
                  "cacheReadInputTokens": 0, "cacheWriteInputTokens": 30_000,
                  "cacheDetails": [{"ttl": "1h", "inputTokens": 20_000},
                                   {"ttl": "5m", "inputTokens": 8_000}]}}
    return [
        {**base, "timestamp": "2026-09-23T09:00:00Z", "requestId": "br-req-1",
         "operation": "Converse", "modelId": "global.anthropic.claude-opus-5",
         "requestMetadata": {"team": "ops", "session": "job-1", "environment": "prod",
                             "ticket_title": f"fix {CANARY}"},
         "input": {"inputContentType": "application/json",
                   "inputBodyJson": {"messages": [{"content": [{"text": CANARY}]}]},
                   "inputTokenCount": 30_100},
         "output": {"outputContentType": "application/json", "outputBodyJson": converse_body,
                    "outputTokenCount": 500}},
        {**base, "timestamp": "2026-09-23T09:02:00.250Z", "requestId": "br-req-2",
         "operation": "InvokeModel", "modelId": "us.anthropic.claude-opus-5",
         "requestMetadata": {"session": "job-1"},
         "input": {"inputBodyJson": {"messages": [{"content": CANARY}]}},
         "output": {"outputBodyJson": {
             "id": "msg_bdrk_1", "type": "message", "role": "assistant",
             "content": [{"type": "text", "text": CANARY}], "stop_reason": "max_tokens",
             "usage": {"input_tokens": 50, "cache_read_input_tokens": 30_000,
                       "cache_creation_input_tokens": 0, "output_tokens": 200}}}},
        {**base, "timestamp": "2026-09-23T09:03:00Z", "requestId": "br-req-3",
         "operation": "Converse", "modelId": "anthropic.claude-opus-5",
         "errorCode": "ThrottlingException"},
        {**base, "timestamp": "2026-09-23T09:04:00Z", "requestId": "br-req-4",
         "operation": "ConverseStream",
         "modelId": "arn:aws:bedrock:us-east-1:123456789012:inference-profile/"
                    "global.anthropic.claude-opus-5",
         "requestMetadata": {"session": "job-2"},
         "output": {"outputBodyJson": [
             {"messageStart": {"role": "assistant"}},
             {"contentBlockDelta": {"delta": {"text": CANARY}}},
             {"messageStop": {"stopReason": "end_turn"}},
             {"metadata": {"usage": {"inputTokens": 30, "outputTokens": 70, "totalTokens": 100},
                           "metrics": {"latencyMs": 900}}}]}},
        {"request_meta": {"ts_ms": T0 + 600_000, "session": "job-2", "model_raw":
                          "global.anthropic.claude-opus-5", "account": "123456789012"},
         "response": {"output": {"message": {"content": [{"text": CANARY}]}},
                      "stopReason": "end_turn", "metrics": {"latencyMs": 800},
                      "usage": {"inputTokens": 40, "outputTokens": 60, "totalTokens": 100}}},
    ]


# ---------------------------------------------------------------------------------------------
# Anthropic Messages responses and batch results
# ---------------------------------------------------------------------------------------------

def anthropic() -> list[Any]:
    text = [{"type": "text", "text": CANARY}]
    return [
        {"request_meta": {"ts_ms": T0, "session": "agent-run-1", "lane": "main",
                          "request_id": "req_011CT0000000000000000001",
                          "attribution": {"team": "agents", "principal": "user-42",
                                          "agent_product": "agent_sdk", "repo": "acme/api"}},
         "response": {"id": "msg_01", "type": "message", "role": "assistant",
                      "model": "claude-opus-5-5", "content": text, "stop_reason": "end_turn",
                      "usage": {"input_tokens": 20, "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 40_000,
                                "cache_creation": {"ephemeral_5m_input_tokens": 0,
                                                   "ephemeral_1h_input_tokens": 40_000},
                                "output_tokens": 900, "service_tier": "standard",
                                "inference_geo": "global"},
                      "diagnostics": {"cache_miss_reason": {"type": "tools_changed",
                                                            "cache_missed_input_tokens": 4_000}},
                      "context_management": {"applied_edits": [
                          {"type": "clear_tool_uses_20250919", "cleared_input_tokens": 1_500}]}}},
        {"request_meta": {"ts_ms": T0 + 300_000, "session": "agent-run-1", "lane": "main"},
         "response": {"custom_id": f"row-{CANARY}", "result": {"type": "succeeded", "message": {
             "id": "msg_batch_1", "type": "message", "role": "assistant",
             "model": "claude-opus-5-5", "content": text, "stop_reason": "end_turn",
             "usage": {"input_tokens": 1_000, "cache_read_input_tokens": 40_000,
                       "cache_creation_input_tokens": 0, "output_tokens": 300,
                       "service_tier": "batch"}}}}},
        {"request_meta": {"ts_ms": T0 + 600_000, "session": "vertex-run", "channel": "vertex",
                          "model_raw": "claude-opus-5-5@20260901", "endpoint_scope": "regional",
                          "billing_path": "vertex", "account": "gcp-project-1"},
         "response": {"id": "msg_vertex_1", "type": "message", "role": "assistant",
                      "content": text, "stop_reason": "end_turn",
                      "usage": {"input_tokens": 300, "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 0, "output_tokens": 50}}},
        {"request_meta": {"ts_ms": T0 + 900_000, "session": "agent-run-1", "lane": "main"},
         "response": {"id": "msg_fallback_1", "type": "message", "role": "assistant",
                      "model": "claude-opus-5-5", "content": text, "stop_reason": "end_turn",
                      "usage": {"input_tokens": 100, "cache_read_input_tokens": 40_000,
                                "cache_creation_input_tokens": 0, "output_tokens": 400,
                                "iterations": [
                                    {"type": "message", "model": "claude-opus-5-5",
                                     "input_tokens": 100, "cache_read_input_tokens": 40_000,
                                     "cache_creation_input_tokens": 0, "output_tokens": 6},
                                    {"type": "fallback_message", "model": "claude-sonnet-5",
                                     "input_tokens": 100, "cache_read_input_tokens": 40_000,
                                     "cache_creation_input_tokens": 0,
                                     "output_tokens": 400}]}}},
        {"custom_id": "row-expired", "result": {"type": "expired"}},
        {"id": "msg_no_meta", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
         "content": text, "usage": {"input_tokens": 1, "output_tokens": 1}},
        {"request_meta": {"ts_ms": T0 + 1_200_000, "session": "agent-run-1", "lane": "main"},
         "response": {"type": "error", "error": {"type": "overloaded_error",
                                                 "message": f"Overloaded {CANARY}"}}},
    ]


# ---------------------------------------------------------------------------------------------
# golden sum-check cases, one or more per TELEM convention (SPEC §5.2)
# ---------------------------------------------------------------------------------------------

def golden() -> dict[str, Any]:
    """provider_total_input is the provider's own input total where the convention defines one;
    buckets list only non-zero fields."""
    cases = [
     {
      "buckets": {
       "cache_read": 6000,
       "cache_write_other": 2000,
       "cache_write_other_ttl_s": 1800,
       "output": 500,
       "output_reasoning": 100,
       "uncached_input": 2000
      },
      "codes": [],
      "convention": "openai.responses",
      "id": "openai.responses/brief",
      "provider_total_input": 10000,
      "raw": {
       "input_tokens": 10000,
       "input_tokens_details": {
        "cache_write_tokens": 2000,
        "cached_tokens": 6000
       },
       "output_tokens": 500,
       "output_tokens_details": {
        "reasoning_tokens": 100
       },
       "total_tokens": 10500
      }
     },
     {
      "buckets": {
       "cache_read": 1024,
       "output": 300,
       "output_reasoning": 64,
       "uncached_input": 976
      },
      "codes": [],
      "convention": "openai.chat",
      "id": "openai.chat/reasoning-subset",
      "provider_total_input": 2000,
      "raw": {
       "completion_tokens": 300,
       "completion_tokens_details": {
        "accepted_prediction_tokens": 0,
        "reasoning_tokens": 64,
        "rejected_prediction_tokens": 10
       },
       "prompt_tokens": 2000,
       "prompt_tokens_details": {
        "cached_tokens": 1024
       },
       "total_tokens": 2300
      }
     },
     {
      "buckets": {
       "cache_read": 3000,
       "cache_write_other": 1500,
       "cache_write_other_ttl_s": 1800,
       "output": 20,
       "uncached_input": 500
      },
      "codes": [],
      "convention": "openai.chat",
      "id": "openai.chat/5.6-cache-write",
      "provider_total_input": 5000,
      "raw": {
       "completion_tokens": 20,
       "prompt_tokens": 5000,
       "prompt_tokens_details": {
        "cache_write_tokens": 1500,
        "cached_tokens": 3000
       }
      }
     },
     {
      "buckets": {
       "cache_read": 6000,
       "cache_write_unknown": 2000,
       "output": 500,
       "output_reasoning": 120,
       "uncached_input": 2000
      },
      "codes": [],
      "convention": "otel.genai",
      "id": "otel.genai/new-names",
      "provider_total_input": 10000,
      "raw": {
       "gen_ai.usage.cache_read.input_tokens": 6000,
       "gen_ai.usage.cache_write.input_tokens": 2000,
       "gen_ai.usage.input_tokens": 10000,
       "gen_ai.usage.output_tokens": 500,
       "gen_ai.usage.reasoning.output_tokens": 120
      }
     },
     {
      "buckets": {
       "cache_read": 3000,
       "cache_write_unknown": 1000,
       "output": 200,
       "uncached_input": 1000
      },
      "codes": [],
      "convention": "otel.genai.legacy",
      "id": "otel.genai.legacy/cache-creation",
      "provider_total_input": 5000,
      "raw": {
       "gen_ai.usage.cache_creation.input_tokens": 1000,
       "gen_ai.usage.cache_read.input_tokens": 3000,
       "gen_ai.usage.input_tokens": 5000,
       "gen_ai.usage.output_tokens": 200
      }
     },
     {
      "buckets": {
       "output": 40,
       "uncached_input": 800
      },
      "codes": [],
      "convention": "otel.genai.legacy",
      "id": "otel.genai.legacy/deprecated-prompt-tokens",
      "provider_total_input": 800,
      "raw": {
       "gen_ai.usage.completion_tokens": 40,
       "gen_ai.usage.prompt_tokens": 800
      }
     },
     {
      "buckets": {
       "cache_read": 40000,
       "cache_write_unknown": 900,
       "output": 300,
       "uncached_input": 12
      },
      "codes": [
       "dq.convention_mismatch"
      ],
      "convention": "otel.genai",
      "id": "otel.genai/read-plus-write-exceeds-input",
      "provider_total_input": None,
      "raw": {
       "gen_ai.usage.cache_read.input_tokens": 40000,
       "gen_ai.usage.cache_write.input_tokens": 900,
       "gen_ai.usage.input_tokens": 12,
       "gen_ai.usage.output_tokens": 300
      }
     },
     {
      "buckets": {
       "cache_read": 2048,
       "output": 200,
       "output_reasoning": 50,
       "uncached_input": 952
      },
      "codes": [],
      "convention": "openinference",
      "id": "openinference/llm-span",
      "provider_total_input": 3000,
      "raw": {
       "llm.token_count.completion": 200,
       "llm.token_count.completion_details.reasoning": 50,
       "llm.token_count.prompt": 3000,
       "llm.token_count.prompt_details.cache_read": 2048,
       "llm.token_count.prompt_details.cache_write": 0,
       "llm.token_count.total": 3200
      }
     },
     {
      "buckets": {
       "output": 350,
       "uncached_input": 17
      },
      "codes": [
       "dq.sum_check_failed"
      ],
      "convention": "openinference",
      "id": "openinference/crewai-17-vs-17119",
      "provider_total_input": None,
      "raw": {
       "llm.token_count.completion": 350,
       "llm.token_count.prompt": 17,
       "llm.token_count.total": 17469
      }
     },
     {
      "buckets": {
       "cache_read": 17102,
       "output": 350,
       "uncached_input": 17
      },
      "codes": [
       "dq.convention_mismatch",
       "dq.sum_check_failed"
      ],
      "convention": "openinference",
      "id": "openinference/crewai-exclusive-cache-read",
      "provider_total_input": None,
      "raw": {
       "llm.token_count.completion": 350,
       "llm.token_count.prompt": 17,
       "llm.token_count.prompt_details.cache_read": 17102,
       "llm.token_count.total": 367
      }
     },
     {
      "buckets": {
       "cache_write_1h": 20000,
       "cache_write_5m": 8000,
       "cache_write_unknown": 2000,
       "output": 500,
       "uncached_input": 100
      },
      "codes": [
       "dq.ttl_split_residual"
      ],
      "convention": "bedrock.converse",
      "id": "bedrock.converse/cache-details-with-residual",
      "provider_total_input": 30100,
      "raw": {
       "cacheDetails": [
        {
         "inputTokens": 20000,
         "ttl": "1h"
        },
        {
         "inputTokens": 8000,
         "ttl": "5m"
        }
       ],
       "cacheReadInputTokens": 0,
       "cacheWriteInputTokens": 30000,
       "inputTokens": 100,
       "outputTokens": 500,
       "totalTokens": 30600
      }
     },
     {
      "buckets": {
       "cache_read": 1000,
       "cache_write_unknown": 5000,
       "output": 60,
       "uncached_input": 40
      },
      "codes": [
       "dq.no_ttl_split"
      ],
      "convention": "bedrock.converse",
      "id": "bedrock.converse/no-cache-details",
      "provider_total_input": 6040,
      "raw": {
       "cacheReadInputTokens": 1000,
       "cacheWriteInputTokens": 5000,
       "inputTokens": 40,
       "outputTokens": 60
      }
     },
     {
      "buckets": {
       "cache_write_5m": 3000,
       "output": 9,
       "uncached_input": 7
      },
      "codes": [],
      "convention": "bedrock.converse",
      "id": "bedrock.converse/exact-split",
      "provider_total_input": 3007,
      "raw": {
       "cacheDetails": [
        {
         "inputTokens": 3000,
         "ttl": "5m"
        }
       ],
       "cacheWriteInputTokens": 3000,
       "inputTokens": 7,
       "outputTokens": 9
      }
     },
     {
      "buckets": {
       "cache_write_unknown": 30000,
       "output": 800,
       "uncached_input": 12
      },
      "codes": [
       "dq.no_ttl_split"
      ],
      "convention": "claude_code.otel",
      "id": "claude_code.otel/api-request",
      "provider_total_input": 30012,
      "raw": {
       "cache_creation_tokens": 30000,
       "cache_read_tokens": 0,
       "input_tokens": 12,
       "output_tokens": 800
      }
     }
    ]
    return {"schema": "tokenbill/telemetry-conventions-golden@1", "notes": NOTES, "cases": cases}


NOTES = ("Synthetic golden sum-check cases per TELEM convention (SPEC §5.2). provider_total_input "
         "is the provider's own input total where the convention defines one; buckets list only "
         "non-zero fields.")


def main(out: Path = HERE) -> None:
    """Write every fixture file into *out* (default: this directory)."""
    write("otlp_claude_code.jsonl", claude_code(), out)
    write("otlp_genai.jsonl", genai(), out)
    write("otlp_openinference.jsonl", openinference(), out)
    write("openai_usage.jsonl", openai(), out)
    write("bedrock_invocations.jsonl", bedrock(), out)
    write("anthropic_responses.jsonl", anthropic(), out)
    with open(out / "conventions_golden.json", "w", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(golden(), indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
