"""Shared helpers of the CP-OTEL tests: fixture paths, ingest options, and a VS Code
``agent-traces.db`` builder over the reconstructed DDL (``fixtures/copilot_otel/vscode/DDL.sql``)
with the content canary planted in every content row, span name, span event and status message."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from tokenbill.core.builders import CANARY, CANARY_LOGIN
from tokenbill.core.ids import key_id
from tokenbill.core.records import Request, to_json
from tokenbill.core.testing import (
    CONFORMANCE_NAME_KEY,
    CONFORMANCE_PRINCIPAL_KEY,
    conformance_ingest_options,
)
from tokenbill.core.types import IngestOptions, IngestResult

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "copilot_otel"
DDL = FIXTURES / "vscode" / "DDL.sql"
VSCODE_DUMP = FIXTURES / "vscode_otel" / "vscode-otel.jsonl"
CLI_FILE = FIXTURES / "cli" / "cli-otel.jsonl"
COPILOT_OTLP = FIXTURES / "otlp" / "copilot-otlp.jsonl"
JETBRAINS = FIXTURES / "jetbrains" / "jetbrains-otlp.jsonl"
MIXED = FIXTURES / "mixed" / "mixed-otlp.jsonl"
GH_AW = FIXTURES / "gh_aw" / "token-usage.jsonl"
GH_AW_MIXED = FIXTURES / "gh_aw" / "token-usage-mixed.jsonl"

T0_MS = 1_790_157_600_000          # 2026-09-23T10:00:00Z
CLI_FLAG = frozenset({"copilot-cli-otel-file"})
JB_FLAG = frozenset({"copilot-jetbrains-otel"})
PRINCIPAL_KEY_ID = key_id(CONFORMANCE_PRINCIPAL_KEY)
NAME_KEY_ID = key_id(CONFORMANCE_NAME_KEY)


def opts(**kw: Any) -> IngestOptions:
    """Conformance ingest options (install mode, fixed keys, clock 2026-09-23) with overrides."""
    return conformance_ingest_options(**kw)


def blob(result: IngestResult) -> str:
    """``repr`` + canonical JSON of a result (for canary scans)."""
    return repr(result) + json.dumps(to_json(result), sort_keys=True)


def no_leak(result: IngestResult) -> None:
    """Neither the content canary nor the canary login appears anywhere in *result*."""
    text = blob(result)
    assert CANARY not in text
    assert CANARY_LOGIN.lower() not in text.lower()


def inference(req: Request) -> Any:
    """The single inference of a Copilot request."""
    (att,) = req.attempts
    (inf,) = att.inferences
    return inf


def by_message(result: IngestResult) -> dict[str | None, Request]:
    """Requests keyed by provider message id."""
    return {r.attempts[0].provider_message_id: r for r in result.requests}


def codes(result: IngestResult) -> dict[str, int]:
    """Data-quality note counts by code."""
    return {n.code: n.count for n in result.notes}


# ---------------------------------------------------------------------------------------------
# VS Code agent-traces.db
# ---------------------------------------------------------------------------------------------

#: Content rows planted in ``span_attributes`` of every span (never to be selected).
CONTENT_ROWS = {
    "gen_ai.input.messages": f'[{{"role":"user","content":"{CANARY}"}}]',
    "gen_ai.output.messages": f'[{{"role":"assistant","content":"{CANARY}"}}]',
    "gen_ai.system_instructions": f"system {CANARY}",
    "gen_ai.tool.definitions": f'[{{"name":"{CANARY}"}}]',
    "copilot_chat.hook_input": f"hook {CANARY}",
    "user.name": CANARY_LOGIN,
    "enduser.pseudo.id": CANARY_LOGIN,
}


@dataclasses.dataclass
class DbSpan:
    """One ``spans`` row plus its attribute rows (allowlisted and not)."""

    span_id: str
    trace_id: str
    parent: str | None
    op: str
    start: int
    end: int
    attrs: dict[str, Any]
    columns: dict[str, Any] = dataclasses.field(default_factory=dict)
    status: int = 1


def chat_span(span_id: str, trace_id: str, parent: str | None, *, conv: str, start: int,
              model: str, resp: str | None, inp: int, read: int, create: int, out: int,
              nano: int | None, turn: int, request_model: str | None = None,
              **extra: Any) -> DbSpan:
    """A VS Code chat span with allowlisted attribute rows (values as VS Code stores them)."""
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": "chat", "gen_ai.provider.name": "github",
        "gen_ai.request.model": request_model or model, "gen_ai.response.model": model,
        "gen_ai.conversation.id": conv, "gen_ai.usage.input_tokens": inp,
        "gen_ai.usage.output_tokens": out, "gen_ai.usage.cache_read.input_tokens": read,
        "gen_ai.usage.cache_creation.input_tokens": create, "copilot_chat.turn.index": turn,
        "copilot_chat.location": "panel", "copilot_chat.api_type": "chat_completions",
    }
    if resp is not None:
        attrs["gen_ai.response.id"] = resp
    if nano is not None:
        attrs["copilot_chat.copilot_usage_nano_aiu"] = nano
    attrs.update(extra)
    columns = {"operation_name": "chat", "provider_name": "github", "conversation_id": conv,
               "request_model": request_model or model, "response_model": model,
               "input_tokens": inp, "output_tokens": out, "cached_tokens": read,
               "chat_session_id": conv, "turn_index": turn, "ttft_ms": 420}
    return DbSpan(span_id, trace_id, parent, "chat", start, start + 2000, attrs, columns)


def standard_spans() -> list[DbSpan]:
    """An agent turn: invoke_agent, two chat spans (one routed ``auto``), one tool call."""
    trace, conv = "b" * 32, "conv-db-1"
    root = DbSpan("b000000000000001", trace, None, "invoke_agent", T0_MS, T0_MS + 9000,
                  {"gen_ai.operation.name": "invoke_agent", "gen_ai.conversation.id": conv},
                  {"operation_name": "invoke_agent", "agent_name": "GitHub Copilot Chat",
                   "conversation_id": conv, "chat_session_id": conv})
    chat1 = chat_span("b000000000000002", trace, root.span_id, conv=conv, start=T0_MS + 100,
                      model="claude-sonnet-4.5", resp="resp_db_001", inp=20000, read=15000,
                      create=3000, out=700, nano=12_345_600_000, turn=0)
    tool = DbSpan("b000000000000003", trace, root.span_id, "execute_tool", T0_MS + 2200,
                  T0_MS + 2300, {"gen_ai.operation.name": "execute_tool"},
                  {"operation_name": "execute_tool", "tool_name": f"read_file_{CANARY}",
                   "tool_arguments": f'{{"path":"{CANARY}"}}', "tool_result": CANARY})
    chat2 = chat_span("b000000000000004", trace, root.span_id, conv=conv, start=T0_MS + 2400,
                      model="gpt-5-mini", request_model="auto", resp="resp_db_002", inp=8000,
                      read=6000, create=0, out=400, nano=1_000_000_000, turn=1)
    return [root, chat1, tool, chat2]


def build_db(path: Path, spans: Sequence[DbSpan], *, meta: Mapping[str, str] | None = None,
             content: bool = True, ddl: str | None = None) -> Path:
    """Create an ``agent-traces.db`` from the DDL with *spans* (canary content rows, span names,
    events and status messages planted unless *content* is False) and an optional
    ``tokenbill_meta`` table (a CP-VSCODE extract)."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(ddl if ddl is not None else DDL.read_text(encoding="utf-8"))
        for s in spans:
            row = {"span_id": s.span_id, "trace_id": s.trace_id, "parent_span_id": s.parent,
                   "name": f"{s.op} {CANARY}" if content else s.op, "kind": 1,
                   "start_time_ms": s.start, "end_time_ms": s.end, "status_code": s.status,
                   "status_message": f"error {CANARY}" if content else None, **s.columns}
            cols = ", ".join(row)
            conn.execute(f"INSERT INTO spans ({cols}) VALUES ({', '.join('?' * len(row))})",
                         tuple(row.values()))
            attrs = dict(s.attrs)
            if content:
                attrs.update(CONTENT_ROWS)
            for key, value in attrs.items():
                conn.execute("INSERT INTO span_attributes (span_id, key, value) VALUES (?, ?, ?)",
                             (s.span_id, key, str(value)))
            if content:
                conn.execute("INSERT INTO span_events (span_id, name, timestamp_ms, attributes) "
                             "VALUES (?, ?, ?, ?)", (s.span_id, f"event {CANARY}", s.start,
                                                     json.dumps({"x": CANARY})))
        if meta is not None:
            conn.execute("CREATE TABLE tokenbill_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            for key, value in meta.items():
                conn.execute("INSERT INTO tokenbill_meta (key, value) VALUES (?, ?)", (key, value))
        conn.commit()
    finally:
        conn.close()
    return path


def extract_meta(principal: str | None = "c_0123456789abcdef0123", *,
                 principal_key_id: str | None = PRINCIPAL_KEY_ID, team: str | None = "payments",
                 schema: str = "tokenbill/vscode-extract@1") -> dict[str, str]:
    """The documented ``tokenbill_meta`` rows of a CP-VSCODE extract (CP-VSCODE brief, build 1)."""
    meta = {"schema": schema, "collector_version": "0.2.0", "name_key_id": NAME_KEY_ID,
            "source_db": "h_0123456789abcdef0123", "window_start_ms": str(T0_MS),
            "window_end_ms": str(T0_MS + 10_000), "spans": "4", "dropped_synthesized": "0"}
    if principal is not None:
        meta["principal"] = principal
    if principal_key_id is not None:
        meta["principal_key_id"] = principal_key_id
    if team is not None:
        meta["team"] = team
    return meta


@contextlib.contextmanager
def sql_trace(monkeypatch: Any) -> Iterator[list[str]]:
    """Record every SQL statement the adapter runs (``sqlite3.connect`` patched to install a
    trace callback on the connection it returns)."""
    statements: list[str] = []
    real = sqlite3.connect

    def connect(*args: Any, **kw: Any) -> sqlite3.Connection:
        conn = real(*args, **kw)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    yield statements
