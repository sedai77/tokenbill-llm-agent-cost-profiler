"""Hypothesis fuzz of the three CP-OTEL readers (SPEC §21 #5): only ``TokenbillError`` subclasses
may escape, results are deterministic, token counts stay in range, and planted content never
leaks. ``TB_OTEL_FUZZ_EXAMPLES`` raises the example count (default 40 per property)."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.adapters.copilot_otel import CopilotOtelAdapter, sniff_copilot_otel
from tokenbill.adapters.copilot_vscode import VsCodeAgentTracesAdapter
from tokenbill.adapters.gh_aw import GhAwTokenUsageAdapter
from tokenbill.core.builders import CANARY
from tokenbill.core.errors import TokenbillError
from tokenbill.core.records import MAX_TOKENS, to_json
from tokenbill.core.types import IngestResult

from .helpers import DDL, opts

EXAMPLES = int(os.environ.get("TB_OTEL_FUZZ_EXAMPLES", "40"))
FUZZ = settings(max_examples=EXAMPLES, deadline=None,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
FLAGS = frozenset({"copilot-cli-otel-file", "copilot-jetbrains-otel"})

ATTR_KEYS = ("gen_ai.operation.name", "gen_ai.request.model", "gen_ai.response.model",
             "gen_ai.response.id", "gen_ai.conversation.id", "gen_ai.usage.input_tokens",
             "gen_ai.usage.output_tokens", "gen_ai.usage.cache_read.input_tokens",
             "gen_ai.usage.cache_creation.input_tokens", "gen_ai.usage.reasoning.output_tokens",
             "copilot_chat.copilot_usage_nano_aiu", "github.copilot.nano_aiu",
             "copilot_chat.request.max_prompt_tokens", "copilot_chat.turn.index",
             "gen_ai.request.reasoning.level", "gen_ai.agent.id", "gen_ai.agent.name",
             "copilot_chat.interaction_type", "copilot_chat.endpoint_type", "user.name",
             "vcs.repository.name", "gen_ai.input.messages", "gen_ai.usage.cache_read_input_tokens",
             "copilot_chat.context_tier", "error.type", "tokenbill.ttft_ms")
SCALARS = st.one_of(
    st.none(), st.booleans(), st.integers(min_value=-2**64, max_value=2**64),
    st.decimals(allow_nan=False, allow_infinity=False, places=2),
    st.sampled_from(["chat", "invoke_agent", "execute_tool", "auto", "gpt-4o-mini",
                     "claude-sonnet-4.5", "byok", "conversation-background", "subagent",
                     "long_context", "medium", "12", "-3", "", CANARY]),
    st.text(max_size=12))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    return value


ATTRS = st.dictionaries(st.sampled_from(ATTR_KEYS), SCALARS.map(_jsonable), max_size=10)
IDS = st.sampled_from(["00000000000000a1", "00000000000000a2", "00000000000000a3", "zz", "", 7])
HRTIME = st.one_of(st.lists(st.integers(min_value=-5, max_value=2_000_000_000), min_size=2,
                            max_size=2), st.integers(min_value=0, max_value=2**64),
                   st.text(max_size=8), st.none())
EVENT = st.fixed_dictionaries({
    "name": st.sampled_from(["github.copilot.session.compaction_complete",
                             "github.copilot.session.truncation", "session.shutdown", "x"]),
    "time": HRTIME,
    "attributes": st.dictionaries(st.sampled_from(["trigger", "tokens_removed", "pre_tokens",
                                                   "systemTokens"]), SCALARS.map(_jsonable),
                                  max_size=3)})
SERVICE = st.sampled_from(["copilot-chat", "github-copilot", "copilot-intellij", "claude-code",
                           "acme"])
JS_SPAN = st.fixed_dictionaries({
    "traceId": st.sampled_from(["ab" * 16, "cd" * 16]), "spanId": IDS, "name": st.text(max_size=8),
    "startTime": HRTIME, "endTime": HRTIME, "attributes": ATTRS,
    "events": st.lists(EVENT, max_size=2),
    "resource": st.fixed_dictionaries({"attributes": st.fixed_dictionaries(
        {"service.name": SERVICE})}),
}, optional={"parentSpanContext": st.fixed_dictionaries({"spanId": IDS}),
             "type": st.sampled_from(["span", "log"]), "status": st.fixed_dictionaries(
                 {"code": st.sampled_from([0, 1, 2, "ERROR"])})})


def _otlp_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    return {"stringValue": str(value)}


def _otlp(line: dict[str, Any]) -> dict[str, Any]:
    span = {"traceId": line["traceId"], "spanId": line["spanId"], "name": line["name"],
            "startTimeUnixNano": str(line["startTime"]), "endTimeUnixNano": line["endTime"],
            "attributes": [{"key": k, "value": _otlp_value(v)}
                           for k, v in line["attributes"].items()],
            "events": [{"name": e["name"], "timeUnixNano": e["time"],
                        "attributes": [{"key": k, "value": _otlp_value(v)}
                                       for k, v in e["attributes"].items()]}
                       for e in line["events"]]}
    service = line["resource"]["attributes"]["service.name"]
    return {"resourceSpans": [{"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": service}}]},
        "scopeSpans": [{"scope": {"name": "s"}, "spans": [span]}]}]}


def _check(result: IngestResult) -> None:
    assert isinstance(result, IngestResult)
    for req in result.requests:
        for att in req.attempts:
            for inf in att.inferences:
                assert 0 <= inf.usage.total_input <= 4 * MAX_TOKENS
                assert inf.pricing.billing_path in ("copilot_pool", "copilot_direct")
    blob = json.dumps(to_json(result))
    assert CANARY not in blob


def _read_twice(adapter: Any, path: Path, **kw: Any) -> None:
    try:
        first = adapter.read(path, opts(**kw))
    except TokenbillError:
        return
    second = adapter.read(path, opts(**kw))
    assert json.dumps(to_json(first), sort_keys=True) == json.dumps(to_json(second),
                                                                    sort_keys=True)
    _check(first)


@FUZZ
@given(st.binary(max_size=512))
def test_otel_random_bytes(data: bytes) -> None:
    assert isinstance(sniff_copilot_otel(data), bool)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "x.jsonl"
        path.write_bytes(data)
        _read_twice(CopilotOtelAdapter(), path)


@FUZZ
@given(st.lists(JS_SPAN, min_size=1, max_size=6), st.booleans(), st.booleans())
def test_otel_random_span_documents(lines: list[dict[str, Any]], as_otlp: bool,
                                    flags: bool) -> None:
    docs = [_otlp(line) if as_otlp else line for line in lines]
    text = "".join(json.dumps(d, default=str) + "\n" for d in docs)
    head = text.encode()
    assert isinstance(CopilotOtelAdapter().sniff(Path("x"), head), bool)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "spans.jsonl"
        path.write_text(text)
        _read_twice(CopilotOtelAdapter(), path,
                    experimental=FLAGS if flags else frozenset(),
                    otel_service_names=("copilot-intellij=copilot_jetbrains", "acme"))


DB_VALUES = st.one_of(st.none(), st.integers(min_value=-2**63, max_value=2**63 - 1),
                      st.text(max_size=10), st.sampled_from(["chat", "invoke_agent", "12", "2"]),
                      st.binary(max_size=4), st.floats(allow_nan=False, allow_infinity=False))
DB_ROW = st.fixed_dictionaries({
    "span_id": st.one_of(st.sampled_from(["s1", "s2", "s3"]), st.integers(0, 3)),
    "trace_id": st.sampled_from(["t1", "t2"]),
    "parent_span_id": st.one_of(st.none(), st.sampled_from(["s1", "s2", ""])),
    "start_time_ms": DB_VALUES, "end_time_ms": DB_VALUES, "status_code": DB_VALUES,
    "operation_name": DB_VALUES, "input_tokens": DB_VALUES, "output_tokens": DB_VALUES,
    "cached_tokens": DB_VALUES, "conversation_id": DB_VALUES, "response_model": DB_VALUES,
    "ttft_ms": DB_VALUES,
})
DB_ATTR = st.tuples(st.sampled_from(["s1", "s2", "s3"]), st.sampled_from(ATTR_KEYS), DB_VALUES)
META = st.dictionaries(st.sampled_from(["schema", "principal", "principal_key_id", "team"]),
                       st.one_of(st.just("tokenbill/vscode-extract@1"), st.text(max_size=24),
                                 st.just("c_0123456789abcdef0123"), st.just("k_abcdef012345")),
                       max_size=4)


@FUZZ
@given(st.lists(DB_ROW, max_size=5), st.lists(DB_ATTR, max_size=12), st.one_of(st.none(), META))
def test_vscode_db_random_rows(rows: list[dict[str, Any]], attrs: list[tuple[str, str, Any]],
                               meta: dict[str, str] | None) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "agent-traces.db"
        conn = sqlite3.connect(path)
        conn.executescript(DDL.read_text(encoding="utf-8").replace("NOT NULL", "")
                           .replace("PRIMARY KEY (span_id, key)", "UNIQUE (span_id, key, value)")
                           .replace("span_id TEXT PRIMARY KEY", "span_id TEXT"))
        for row in rows:
            conn.execute(f"INSERT INTO spans ({', '.join(row)}, name) VALUES "
                         f"({', '.join('?' * len(row))}, ?)", (*row.values(), f"chat {CANARY}"))
        for span_id, key, value in attrs:
            conn.execute("INSERT OR IGNORE INTO span_attributes VALUES (?, ?, ?)",
                         (span_id, key, value))
        conn.execute("INSERT INTO span_attributes VALUES ('s1', 'gen_ai.input.messages', ?)",
                     (CANARY,))
        if meta is not None:
            conn.execute("CREATE TABLE tokenbill_meta (key TEXT PRIMARY KEY, value TEXT)")
            conn.executemany("INSERT INTO tokenbill_meta VALUES (?, ?)", list(meta.items()))
        conn.commit()
        conn.close()
        _read_twice(VsCodeAgentTracesAdapter(), path)


GH_KEYS = ("_schema", "timestamp", "event", "request_id", "provider", "model", "path", "status",
           "streaming", "input_tokens", "output_tokens", "cache_read_tokens",
           "cache_write_tokens", "reasoning_tokens", "duration_ms", "ai_credits_this_response",
           "ai_credits_total", "input_tokens_include_cache")
GH_VALUES = st.one_of(SCALARS.map(_jsonable), st.sampled_from([
    "token-usage/v1", "token_usage", "copilot", "2026-09-23T10:00:00Z", "/v1/messages",
    "1e999", "0.000000001", "-1"]))
GH_LINE = st.dictionaries(st.sampled_from(GH_KEYS), GH_VALUES, max_size=14)


@FUZZ
@given(st.lists(GH_LINE, max_size=6), st.binary(max_size=64))
def test_gh_aw_random_lines(lines: list[dict[str, Any]], junk: bytes) -> None:
    base = {"_schema": "token-usage/v1", "event": "token_usage", "provider": "copilot",
            "timestamp": "2026-09-23T10:00:00Z"}
    text = "".join(json.dumps({**base, **line}) + "\n" for line in lines).encode() + junk
    adapter = GhAwTokenUsageAdapter(env={"GITHUB_REPOSITORY": CANARY})
    assert isinstance(adapter.sniff(Path("x"), text), bool)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "token-usage.jsonl"
        path.write_bytes(text)
        _read_twice(adapter, path)
