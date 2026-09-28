"""Synthetic VS Code Copilot Chat span data (stand-alone: stdlib only, no tokenbill import).

* :func:`write_db` builds an ``agent-traces.db`` from ``DDL.sql`` (the verbatim
  ``otelSqliteStore.ts`` schema) and inserts spans exactly as ``OTelSqliteStore.insertSpan`` does:
  one ``spans`` row with the denormalized attributes, **every** attribute into ``span_attributes``
  (``String(value)``; arrays as JSON) and every event into ``span_events``.
* :func:`readable_span_json` renders a span as the VS Code OTel file exporter does
  (``fileExporters.ts: readableSpanToJson``; HrTime ``[seconds, nanos]``).

Every span carries the content canary in content attributes (``gen_ai.input.messages`` …), in
identity attributes (``CANARY_LOGIN``), in ``status_message``, ``tool_*`` columns, span names and
``span_events`` — places a content-free extract must never copy.

Run as a script to write a sample database and outfile: ``python make_traces.py OUT_DIR``.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CANARY = "TB-CANARY-7f3a91"
CANARY_LOGIN = "tb-canary-login-7f3a91"
DDL_PATH = Path(__file__).with_name("DDL.sql")
#: 2026-09-20T00:00:00Z
BASE_MS = 1_789_862_400_000

#: ``otelSqliteStore.ts`` DENORMALIZED_ATTRS: column → attribute key.
DENORMALIZED = {
    "operation_name": "gen_ai.operation.name",
    "provider_name": "gen_ai.provider.name",
    "agent_name": "gen_ai.agent.name",
    "conversation_id": "gen_ai.conversation.id",
    "request_model": "gen_ai.request.model",
    "response_model": "gen_ai.response.model",
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
    "cached_tokens": "gen_ai.usage.cache_read.input_tokens",
    "reasoning_tokens": "gen_ai.usage.reasoning_tokens",
    "tool_name": "gen_ai.tool.name",
    "tool_call_id": "gen_ai.tool.call.id",
    "tool_type": "gen_ai.tool.type",
    "chat_session_id": "copilot_chat.chat_session_id",
    "turn_index": "copilot_chat.turn.index",
}
CONTENT = {
    "gen_ai.input.messages": json.dumps([{"role": "user", "parts": [
        {"type": "text", "content": f"refactor the billing module {CANARY}"}]}]),
    "gen_ai.output.messages": json.dumps([{"role": "assistant", "parts": [
        {"type": "text", "content": f"Here is the patch {CANARY}"}]}]),
    "gen_ai.system_instructions": f"You are GitHub Copilot {CANARY}",
    "gen_ai.tool.definitions": json.dumps([{"name": "read_file", "description": CANARY}]),
    "copilot_chat.hook_input": f"hook input {CANARY}",
}
IDENTITY = {
    "user.name": CANARY_LOGIN,
    "enduser.pseudo.id": CANARY_LOGIN,
    "process.user.name": CANARY_LOGIN,
    "host.name": f"laptop-{CANARY_LOGIN}",
}


@dataclass
class Span:
    """One completed span (``ICompletedSpanData``)."""

    span_id: str
    trace_id: str
    name: str
    start: int
    end: int
    parent_span_id: str | None = None
    status_code: int = 0
    status_message: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[tuple[str, int, dict[str, Any] | None]] = field(default_factory=list)


def hex_id(n: int, width: int = 16) -> str:
    """A deterministic lowercase hex id (OTel span ids are 16 hex, trace ids 32)."""
    return f"{n:0{width}x}"[-width:]


def chat_span(n: int, trace: str, start: int, *, conv: str, model: str = "claude-sonnet-4.5",
              input_tokens: int = 1200, output_tokens: int = 300, cache_read: int = 800,
              cache_creation: int = 100, nano_aiu: int = 23_284_800_000,
              response_id: str | None = "", parent: str | None = None,
              name: str | None = None, turn: int = 0, canary: bool = True) -> Span:
    """A ``chat`` span as ``chatMLFetcher.ts`` records it (``response_id=None``: a span VS Code
    synthesizes for the in-editor CLI agent — no response id, no cache creation)."""
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": "github",
        "gen_ai.request.model": model,
        "gen_ai.response.model": model,
        "gen_ai.conversation.id": conv,
        "gen_ai.usage.input_tokens": input_tokens,
        "gen_ai.usage.output_tokens": output_tokens,
        "gen_ai.usage.cache_read.input_tokens": cache_read,
        "copilot_chat.copilot_usage_nano_aiu": nano_aiu,
        "copilot_chat.turn.index": turn,
        "copilot_chat.chat_session_id": f"chat-{conv}",
        "copilot_chat.request.max_prompt_tokens": 128_000,
        "copilot_chat.api_type": "chat_completions",
        "copilot_chat.location": "panel",
        "copilot_chat.time_to_first_token": 412,
    }
    if response_id is not None:
        attrs["gen_ai.response.id"] = response_id or f"resp-{hex_id(n, 12)}"
        attrs["gen_ai.usage.cache_creation.input_tokens"] = cache_creation
    if canary:
        attrs.update(CONTENT)
        attrs.update(IDENTITY)
    events = [("exception", start + 5, {"exception.message": f"boom {CANARY}"})] if canary else []
    return Span(span_id=hex_id(n), trace_id=trace, parent_span_id=parent,
                name=name if name is not None else (f"chat {model} {CANARY}" if canary else "chat"),
                start=start, end=start + 900, attributes=attrs, events=events)


def agent_span(n: int, trace: str, start: int, *, conv: str, end: int | None = None,
               nano_aiu: int | None = None) -> Span:
    """An ``invoke_agent`` root span."""
    attrs: dict[str, Any] = {"gen_ai.operation.name": "invoke_agent",
                             "gen_ai.agent.name": "GitHub Copilot Chat",
                             "gen_ai.conversation.id": conv,
                             "copilot_chat.chat_session_id": f"chat-{conv}"}
    if nano_aiu is not None:
        attrs["copilot_chat.copilot_usage_nano_aiu"] = nano_aiu
    attrs.update(CONTENT)
    return Span(span_id=hex_id(n), trace_id=trace, name="invoke_agent", start=start,
                end=end if end is not None else start + 60_000, attributes=attrs,
                events=[("github.copilot.session.compaction_start", start + 7, {
                    "github.copilot.session.summary": CANARY})])


def tool_span(n: int, trace: str, start: int, *, conv: str, parent: str | None = None) -> Span:
    """An ``execute_tool`` span (tool name, call id and arguments are content)."""
    attrs: dict[str, Any] = {"gen_ai.operation.name": "execute_tool",
                             "gen_ai.tool.name": f"read_file {CANARY}",
                             "gen_ai.tool.call.id": f"call_{CANARY}",
                             "gen_ai.tool.type": "function",
                             "gen_ai.tool.call.arguments": json.dumps({"path": f"/src/{CANARY}"}),
                             "gen_ai.tool.call.result": f"file body {CANARY}",
                             "github.copilot.tool.parameters.file_path": f"/src/{CANARY}.py",
                             "gen_ai.conversation.id": conv}
    return Span(span_id=hex_id(n), trace_id=trace, parent_span_id=parent,
                name=f"execute_tool read_file {CANARY}", start=start, end=start + 40,
                status_code=2, status_message=f"tool failed {CANARY}", attributes=attrs)


def conversation(first: int, conv: str, start: int, chats: int = 2, *,
                 trace: str | None = None, **chat_kw: Any) -> list[Span]:
    """One trace: an ``invoke_agent`` root, *chats* chat spans and a tool span after each chat.
    Span ids are ``hex_id(first)``, ``hex_id(first + 1)``, …"""
    trace = trace or hex_id(first, 32)
    root = agent_span(first, trace, start, conv=conv, end=start + (chats + 1) * 1000)
    spans = [root]
    n = first + 1
    for i in range(chats):
        t = start + 10 + i * 1000
        spans.append(chat_span(n, trace, t, conv=conv, parent=root.span_id, turn=i, **chat_kw))
        spans.append(tool_span(n + 1, trace, t + 950, conv=conv, parent=root.span_id))
        n += 2
    return spans


def create_db(path: Path, *, wal: bool = True) -> sqlite3.Connection:
    """A new database with the ``otelSqliteStore.ts`` schema (WAL, foreign keys, like VS Code)."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    if wal:
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(DDL_PATH.read_text(encoding="utf-8"))
    return conn


def _js_string(value: Any) -> Any:
    """``String(value)`` for scalars, JSON for arrays (``insertSpan``)."""
    if isinstance(value, list):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _denorm(value: Any) -> Any:
    """``OTelSqliteStore._attr``: arrays as JSON, booleans 1/0, else the value."""
    if isinstance(value, list):
        return json.dumps(value)
    if isinstance(value, bool):
        return 1 if value else 0
    return value


def insert_spans(conn: sqlite3.Connection, spans: list[Span], *, bulk: bool = False) -> None:
    """``insertSpan`` for each span (one transaction per span, as VS Code does; *bulk*: one
    transaction for all, for large fixtures)."""
    cols = ["span_id", "trace_id", "parent_span_id", "name", "start_time_ms", "end_time_ms",
            "status_code", "status_message", *DENORMALIZED, "ttft_ms"]
    sql = (f"INSERT OR REPLACE INTO spans ({', '.join(cols)}) "
           f"VALUES ({', '.join('?' * len(cols))})")
    if bulk:
        conn.execute("BEGIN")
    for s in spans:
        a = s.attributes
        row = [s.span_id, s.trace_id, s.parent_span_id, s.name, s.start, s.end, s.status_code,
               s.status_message, *(_denorm(a.get(k)) for k in DENORMALIZED.values()),
               a.get("copilot_chat.time_to_first_token")]
        if not bulk:
            conn.execute("BEGIN")
        conn.execute(sql, row)
        conn.executemany("INSERT OR REPLACE INTO span_attributes (span_id, key, value) "
                         "VALUES (?, ?, ?)", [(s.span_id, k, _js_string(v)) for k, v in a.items()])
        conn.executemany("INSERT INTO span_events (span_id, name, timestamp_ms, attributes) "
                         "VALUES (?, ?, ?, ?)",
                         [(s.span_id, n, t, json.dumps(ea) if ea else None)
                          for n, t, ea in s.events])
        if not bulk:
            conn.execute("COMMIT")
    if bulk:
        conn.execute("COMMIT")


def write_db(path: Path, spans: list[Span], *, wal: bool = True, bulk: bool = False) -> Path:
    """Create *path* (a new database) holding *spans*; the connection is closed afterwards."""
    conn = create_db(path, wal=wal)
    try:
        insert_spans(conn, spans, bulk=bulk)
    finally:
        conn.close()
    return path


def delete_spans(path: Path, where: str, params: tuple[Any, ...] = ()) -> None:
    """VS Code's pruning (``DELETE FROM spans WHERE …``; attributes and events cascade)."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(f"DELETE FROM spans WHERE {where}", params)
    finally:
        conn.close()


def _hr(ms: int) -> list[int]:
    return [ms // 1000, (ms % 1000) * 1_000_000]


def readable_span_json(s: Span, *, service: str = "copilot-chat",
                       extra_resource: dict[str, Any] | None = None) -> dict[str, Any]:
    """The VS Code file exporter's JSON for *s* (``readableSpanToJson``)."""
    resource = {"service.name": service, "service.version": "0.68.0",
                "telemetry.sdk.language": "nodejs", "telemetry.sdk.name": "opentelemetry",
                "telemetry.sdk.version": "2.0.1", "host.name": f"laptop-{CANARY_LOGIN}",
                "process.executable.path": f"/Users/{CANARY_LOGIN}/code", **(extra_resource or {})}
    parent = None
    if s.parent_span_id:
        parent = {"traceId": s.trace_id, "spanId": s.parent_span_id, "traceFlags": 1,
                  "isRemote": False}
    out = {
        "traceId": s.trace_id, "spanId": s.span_id, "traceFlags": 1,
        "traceState": f"vendor={CANARY}", "isRemote": False,
        "parentSpanContext": parent, "name": s.name, "kind": 2,
        "startTime": _hr(s.start), "endTime": _hr(s.end), "duration": _hr(s.end - s.start),
        "ended": True, "attributes": s.attributes,
        "status": {"code": s.status_code, "message": s.status_message},
        "events": [{"name": n, "attributes": ea or {}, "time": _hr(t), "droppedAttributesCount": 0}
                   for n, t, ea in s.events],
        "links": [{"context": {"traceId": s.trace_id, "spanId": s.span_id},
                   "attributes": {"link.note": CANARY}, "droppedAttributesCount": 0}],
        "resource": {"attributes": resource, "schemaUrl": None},
        "instrumentationScope": {"name": "copilot-chat", "version": "0.68.0"},
        "droppedAttributesCount": 0, "droppedEventsCount": 0, "droppedLinksCount": 0,
    }
    if parent is None:
        del out["parentSpanContext"]
    return out


def outfile_lines(spans: list[Span], **kw: Any) -> list[bytes]:
    """One JSON line per span (``JSON.stringify(readableSpanToJson(span)) + '\\n'``)."""
    return [(json.dumps(readable_span_json(s, **kw)) + "\n").encode("utf-8") for s in spans]


def log_line() -> bytes:
    """A log record line (``FileLogExporter``): its body is content."""
    rec = {"hrTime": [1_789_862_400, 0], "body": f"user prompt {CANARY}", "severityNumber": 9,
           "attributes": {"user.name": CANARY_LOGIN},
           "resource": {"attributes": {"service.name": "copilot-chat"}}}
    return (json.dumps(rec) + "\n").encode("utf-8")


def metric_line() -> bytes:
    """A ``ResourceMetrics`` line (``FileMetricExporter``)."""
    rec = {"resource": {"attributes": {"service.name": "copilot-chat"}},
           "scopeMetrics": [{"scope": {"name": "copilot-chat"}, "metrics": []}]}
    return (json.dumps(rec) + "\n").encode("utf-8")


def main(argv: list[str]) -> int:
    """Write ``agent-traces.db`` and ``copilot-otel.jsonl`` samples into ``argv[0]``."""
    out = Path(argv[0]) if argv else Path(".")
    out.mkdir(parents=True, exist_ok=True)
    spans = conversation(1, "conv-a", BASE_MS) + conversation(100, "conv-b", BASE_MS + 60_000)
    write_db(out / "agent-traces.db", spans)
    (out / "copilot-otel.jsonl").write_bytes(b"".join(outfile_lines(spans)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
