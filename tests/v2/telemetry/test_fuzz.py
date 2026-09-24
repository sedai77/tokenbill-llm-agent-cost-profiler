"""Hypothesis fuzz for every TELEM parser (SPEC §21 #5): random bytes, random JSON, structured
OTLP noise and mutated fixture records. Only ``TokenbillError`` subclasses may escape ``read()``;
whatever it returns must serialize and must not leak the canary."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tests.v2.telemetry import helpers as h
from tokenbill.adapters.anthropic_responses import AnthropicResponsesAdapter
from tokenbill.adapters.bedrock import BedrockAdapter
from tokenbill.adapters.openai import OpenAIUsageAdapter
from tokenbill.adapters.otel import OtlpJsonAdapter
from tokenbill.core.errors import TokenbillError
from tokenbill.core.records import to_json

ADAPTERS = [OtlpJsonAdapter(), OpenAIUsageAdapter(), BedrockAdapter(),
            AnthropicResponsesAdapter()]
FIXTURES = {"otlp": [h.OTLP_CC, h.OTLP_GENAI, h.OTLP_OI], "openai": [h.OPENAI],
            "bedrock": [h.BEDROCK], "anthropic-responses": [h.ANTHROPIC]}
VOCAB = sorted({
    "resourceLogs", "resourceSpans", "resourceMetrics", "scopeLogs", "logRecords", "scopeSpans",
    "spans", "scopeMetrics", "metrics", "attributes", "key", "value", "stringValue", "intValue",
    "doubleValue", "boolValue", "arrayValue", "kvlistValue", "values", "timeUnixNano",
    "startTimeUnixNano", "endTimeUnixNano", "traceId", "spanId", "parentSpanId", "name", "body",
    "sum", "gauge", "dataPoints", "asInt", "asDouble", "aggregationTemporality", "request_meta",
    "response", "usage", "object", "id", "created_at", "created", "status", "model",
    "service_tier", "input_tokens", "output_tokens", "input_tokens_details", "cached_tokens",
    "cache_write_tokens", "prompt_tokens", "completion_tokens", "prompt_cache_diagnostics",
    "type", "reason", "cache_missed_tokens", "schemaType", "timestamp", "modelId", "identity",
    "arn", "requestMetadata", "output", "outputBodyJson", "inputTokens", "outputTokens",
    "cacheDetails", "ttl", "custom_id", "result", "message", "ts_ms", "session", "lane",
    "attribution", "channel", "iterations", "errorCode", "choices", "finish_reason"})
SCALARS = st.one_of(st.none(), st.booleans(), st.integers(-2**60, 2**66),
                    st.floats(allow_nan=False, allow_infinity=False), st.text(max_size=12),
                    st.sampled_from(["claude_code.api_request", "api_request", "LLM", "AGENT",
                                     "chat", "response", "message", "ModelInvocationLog",
                                     "cache_miss", "5m", "1h", "succeeded", "1790154000000000000",
                                     "2026-09-23T09:00:00Z", "msg_1", "session.id",
                                     "input_tokens", "gen_ai.usage.input_tokens"]))
JSON = st.recursive(SCALARS, lambda children: st.one_of(
    st.lists(children, max_size=4),
    st.dictionaries(st.one_of(st.sampled_from(VOCAB), st.text(max_size=6)), children,
                    max_size=5)), max_leaves=25)
ATTR_KEYS = ["event.name", "session.id", "user.email", "request_id", "client_request_id",
             "model", "duration_ms", "input_tokens", "output_tokens", "cache_read_tokens",
             "cache_creation_tokens", "cost_usd", "query_source", "status_code", "attempt",
             "tool_name", "tool_use_id", "success", "agent_id", "ttft_ms", "type", "speed",
             "gen_ai.operation.name", "gen_ai.usage.input_tokens", "openinference.span.kind",
             "llm.token_count.prompt", "llm.token_count.total"]
ANY_VALUE = st.one_of(
    st.builds(lambda v: {"stringValue": v}, st.one_of(st.text(max_size=8), st.sampled_from(
        ["api_request", "api_error", "main", "subagent", "LLM", "AGENT", "chat", "input"]))),
    st.builds(lambda v: {"intValue": v}, st.one_of(st.integers(-10, 2**64).map(str),
                                                   st.integers(-10, 10**7), st.text(max_size=3))),
    st.builds(lambda v: {"doubleValue": v}, st.one_of(st.floats(), st.text(max_size=3))),
    st.builds(lambda v: {"boolValue": v}, st.one_of(st.booleans(), st.text(max_size=3))),
    st.just({"arrayValue": {"values": [{"stringValue": "x"}]}}), JSON)
ATTRS = st.lists(st.builds(lambda k, v: {"key": k, "value": v}, st.sampled_from(ATTR_KEYS),
                           ANY_VALUE), max_size=10)
NANOS = st.one_of(st.integers(0, 2**64).map(str), st.integers(-5, 2**64), st.text(max_size=4))
LOG = st.fixed_dictionaries({"timeUnixNano": NANOS, "attributes": ATTRS},
                            optional={"body": ANY_VALUE, "eventName": st.sampled_from(
                                ["claude_code.api_request", "claude_code.api_error",
                                 "claude_code.tool_result", "claude_code.user_prompt"])})
SPAN = st.fixed_dictionaries({
    "traceId": st.sampled_from(["ab" * 16, "cd" * 16, "zz"]),
    "spanId": st.sampled_from(["01" * 8, "02" * 8, "03" * 8]),
    "startTimeUnixNano": NANOS, "endTimeUnixNano": NANOS, "attributes": ATTRS},
    optional={"parentSpanId": st.sampled_from(["01" * 8, "02" * 8, "ff"]),
              "name": st.sampled_from(["claude_code.llm_request", "claude_code.tool", "x"]),
              "status": st.sampled_from([{"code": 2}, {}, 3])})
POINT = st.fixed_dictionaries({"timeUnixNano": NANOS, "attributes": ATTRS},
                              optional={"startTimeUnixNano": NANOS,
                                        "asInt": st.one_of(st.integers(-5, 2**60).map(str), JSON),
                                        "asDouble": st.one_of(st.floats(), JSON)})
OTLP_LINE = st.one_of(
    st.builds(lambda recs: {"resourceLogs": [{"scopeLogs": [{"logRecords": recs}]}]},
              st.lists(LOG, max_size=5)),
    st.builds(lambda sp: {"resourceSpans": [{"scopeSpans": [{"spans": sp}]}]},
              st.lists(SPAN, max_size=5)),
    st.builds(lambda name, temp, pts: {"resourceMetrics": [{"scopeMetrics": [{"metrics": [
        {"name": name, "sum": {"aggregationTemporality": temp, "dataPoints": pts}}]}]}]},
        st.sampled_from(["claude_code.token.usage", "claude_code.cost.usage"]),
        st.sampled_from([1, 2, "2"]), st.lists(POINT, max_size=4)))

SETTINGS = settings(max_examples=120, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])


def _check(adapter: Any, lines: list[bytes]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fuzz.jsonl"
        path.write_bytes(b"\n".join(lines) + b"\n")
        for lenient in (True, False):
            try:
                result = adapter.read(path, h.central(lenient=lenient))
            except TokenbillError:
                continue
            text = json.dumps(to_json(result), sort_keys=True)
            assert h.CANARY not in text
            assert adapter.sniff(path, path.read_bytes()[:65536]) in (True, False)


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.name)
@SETTINGS
@given(lines=st.lists(st.binary(max_size=64), max_size=5))
def test_fuzz_random_bytes(adapter: Any, lines: list[bytes]) -> None:
    _check(adapter, [ln.replace(b"\n", b" ") for ln in lines])


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.name)
@SETTINGS
@given(records=st.lists(JSON, max_size=5))
def test_fuzz_random_json(adapter: Any, records: list[Any]) -> None:
    _check(adapter, [json.dumps(r).encode() for r in records])


@SETTINGS
@given(records=st.lists(OTLP_LINE, min_size=1, max_size=4))
def test_fuzz_structured_otlp(records: list[Any]) -> None:
    _check(OtlpJsonAdapter(), [json.dumps(r).encode() for r in records])


def _paths(obj: Any, prefix: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    out = [prefix]
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_paths(v, (*prefix, k)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_paths(v, (*prefix, i)))
    return out


def _mutate(obj: Any, path: tuple[Any, ...], action: str, value: Any) -> Any:
    if not path:
        return value if action == "replace" else obj
    parent = obj
    for step in path[:-1]:
        parent = parent[step]
    last = path[-1]
    if action == "delete" and isinstance(parent, dict):
        del parent[last]
    elif action == "delete" and isinstance(parent, list):
        parent.pop(last)
    else:
        parent[last] = value
    return obj


@pytest.mark.parametrize("adapter", ADAPTERS, ids=lambda a: a.name)
@SETTINGS
@given(data=st.data())
def test_fuzz_mutated_fixture_records(adapter: Any, data: st.DataObject) -> None:
    path = data.draw(st.sampled_from(FIXTURES[adapter.name]))
    records = [json.loads(line) for line in path.read_text().splitlines()
               if line.startswith("{") and line.endswith("}")]
    for _ in range(data.draw(st.integers(1, 3))):
        index = data.draw(st.integers(0, len(records) - 1))
        target = data.draw(st.sampled_from(_paths(records[index])))
        action = data.draw(st.sampled_from(["replace", "delete"]))
        value = data.draw(JSON)
        records[index] = _mutate(records[index], target, action, value)
    _check(adapter, [json.dumps(r).encode() for r in records])
