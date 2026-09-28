"""VS Code Copilot ``agent-traces.db`` adapter (addendum §5.13; package CP-OTEL; registry name
``copilot-vscode-traces``).

VS Code writes ``agent-traces.db`` only when the developer enables
``github.copilot.chat.otel.dbSpanExporter.enabled`` (user setting, default off). The database has
typed ``spans`` columns and one ``span_attributes(span_id, key, value)`` row per span attribute —
including content attributes when capture is on. This adapter opens the file **read-only**
(``file:…?mode=ro``) and selects

* from ``spans`` only the allowlisted columns of ``core.facts`` ``vscode_traces.span_columns``
  (never ``tool_*``),
* from ``span_attributes`` only rows with ``key IN (<allowlist>)`` — one parameterized query over
  ``vscode_traces.attribute_allowlist`` —
* and never touches ``span_events``.

Rows become :class:`~tokenbill.adapters.copilot_otel.SpanView` objects (attribute rows win over the
typed columns, which fill gaps: ``input_tokens`` → ``gen_ai.usage.input_tokens``,
``cached_tokens`` → ``…cache_read.input_tokens``, ``conversation_id`` → ``gen_ai.conversation.id``,
``chat_session_id`` as the conversation fallback, …) and are mapped by the shared
:func:`~tokenbill.adapters.copilot_otel.map_chat_spans` rules (``agent_product="copilot_vscode"``,
priority 22). Span names are only compared with the operation names ``chat`` / ``invoke_agent`` /
``execute_tool``, never kept.

The same code reads the content-free extracts CP-VSCODE's collector writes (same DDL plus
``tokenbill_meta(key, value)`` with ``schema = "tokenbill/vscode-extract@1"``): the meta
``principal`` (``r_…`` / ``c_<20 hex>``; anything else is dropped with
``dq.copilot_collector_principal_invalid``) becomes ``Attribution.principal``, ``principal_key_id``
``SourceInfo.principal_key_id`` and ``team`` ``Attribution.team``. Without the schema marker the
meta keys are ignored. An empty database yields ``dq.copilot_vscode_no_spans``; one without the
required columns ``dq.copilot_vscode_schema``. Capabilities ``{usage_sequence, timing, params,
credits}``.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from tokenbill.adapters.copilot_otel import (
    COLLECTOR_MARKER,
    EXTRACT_SCHEMA,
    K_CHAT_SESSION,
    K_TTFT,
    VSCODE_ATTRIBUTE_ALLOWLIST,
    Scan,
    SpanView,
    run_mapping,
)
from tokenbill.core.errors import SourceError
from tokenbill.core.facts import load as load_facts
from tokenbill.core.types import IngestOptions, IngestResult

__all__ = ["ADAPTER_NAME", "SQLITE_MAGIC", "VsCodeAgentTracesAdapter"]

ADAPTER_NAME = "copilot-vscode-traces"
SQLITE_MAGIC = b"SQLite format 3\x00"
DQ_NO_SPANS = "dq.copilot_vscode_no_spans"
DQ_SCHEMA = "dq.copilot_vscode_schema"

#: Allowlisted ``spans`` columns (addendum §5.13; ``tool_*`` never selected).
SPAN_COLUMNS: tuple[str, ...] = tuple(c for c in load_facts().copilot.vscode_traces.span_columns
                                      if not c.startswith("tool_"))
_REQUIRED_SPAN_COLUMNS = ("span_id", "trace_id", "start_time_ms")
_REQUIRED_ATTR_COLUMNS = ("span_id", "key", "value")
#: Typed columns → the attribute they stand in for when no attribute row carries it.
_COLUMN_ATTRS: Mapping[str, str] = {
    "operation_name": "gen_ai.operation.name",
    "provider_name": "gen_ai.provider.name",
    "conversation_id": "gen_ai.conversation.id",
    "request_model": "gen_ai.request.model",
    "response_model": "gen_ai.response.model",
    "input_tokens": "gen_ai.usage.input_tokens",
    "output_tokens": "gen_ai.usage.output_tokens",
    "cached_tokens": "gen_ai.usage.cache_read.input_tokens",
    "reasoning_tokens": "gen_ai.usage.reasoning.output_tokens",
    "turn_index": "copilot_chat.turn.index",
    "agent_name": "gen_ai.agent.name",
    "chat_session_id": K_CHAT_SESSION,
    "ttft_ms": K_TTFT,
}
_META_KEYS = ("schema", "principal", "principal_key_id", "team")
_OPERATIONS = ("chat", "invoke_agent", "execute_tool")


def _connect(path: Path) -> sqlite3.Connection:
    """A read-only connection (``mode=ro`` URI; the file is never written)."""
    uri = path.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({_quote(table)})")}


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                       (table,)).fetchone()
    return row is not None


def _meta(conn: sqlite3.Connection) -> dict[str, Any]:
    """The ``tokenbill_meta`` keys of a CP-VSCODE extract, or {} for a raw database."""
    if not _has_table(conn, "tokenbill_meta"):
        return {}
    marks = ",".join("?" * len(_META_KEYS))
    rows = conn.execute(f"SELECT key, value FROM tokenbill_meta WHERE key IN ({marks})",
                        _META_KEYS).fetchall()
    meta = {k: v for k, v in rows if isinstance(k, str)}
    return meta if meta.get("schema") == EXTRACT_SCHEMA else {}


def _attributes(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Allowlisted attribute rows per span id (the only ``span_attributes`` query)."""
    marks = ",".join("?" * len(VSCODE_ATTRIBUTE_ALLOWLIST))
    out: dict[str, dict[str, Any]] = {}
    sql = f"SELECT span_id, key, value FROM span_attributes WHERE key IN ({marks})"
    for span_id, key, value in conn.execute(sql, VSCODE_ATTRIBUTE_ALLOWLIST):
        if isinstance(span_id, (str, int)) and isinstance(key, str) and value is not None:
            if isinstance(value, bytes):
                continue
            out.setdefault(str(span_id), {})[key] = value
    return out


def _op_name(name: object) -> str:
    if isinstance(name, str):
        first = name.strip().split(" ", 1)[0]
        if first in _OPERATIONS:
            return first
    return "other"


def _error(status: object) -> bool:
    if isinstance(status, str):
        return status.strip().upper() in ("2", "ERROR", "STATUS_CODE_ERROR")
    return status == 2 and type(status) is int


def _ms(value: object) -> int | None:
    if type(value) is int and 0 < value < 2**53:
        return value
    if isinstance(value, float) and value.is_integer() and 0 < value < 2**53:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        n = int(value.strip())
        return n if 0 < n < 2**53 else None
    return None


class VsCodeAgentTracesAdapter:
    """VS Code ``agent-traces.db`` (and CP-VSCODE extracts) → Copilot requests through the
    allowlist."""

    name = ADAPTER_NAME
    capabilities = frozenset({"usage_sequence", "timing", "params", "credits"})

    def sniff(self, path: Path, head: bytes) -> bool:
        """SQLite magic and a ``span_attributes`` table in the head."""
        return head.startswith(SQLITE_MAGIC) and b"span_attributes" in head

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Read the allowlisted spans of the database at *path* (read-only)."""
        path = Path(path)
        scan = Scan(self.name, path, opts)
        if not path.is_file():
            raise SourceError(f"{path.name}: not a file")
        try:
            conn = _connect(path)
        except sqlite3.Error:
            raise SourceError(f"{path.name}: not a readable SQLite database") from None
        try:
            spans, meta = self._read(conn, scan)
        except sqlite3.Error as exc:
            raise SourceError(f"{path.name}: not a readable SQLite database "
                              f"({type(exc).__name__})") from None
        finally:
            conn.close()
        if not spans:
            scan.note(DQ_NO_SPANS)
        key_id = meta.get("principal_key_id")
        key_id = key_id if isinstance(key_id, str) and key_id.startswith("k_") \
            and len(key_id) <= 64 else None
        return run_mapping(spans, scan, dialect="vscode-db", allowed_caps=self.capabilities,
                           principal_key_id=key_id)

    def _read(self, conn: sqlite3.Connection, scan: Scan) -> tuple[list[SpanView],
                                                                  dict[str, Any]]:
        span_cols = _columns(conn, "spans")
        attr_cols = _columns(conn, "span_attributes")
        if any(c not in span_cols for c in _REQUIRED_SPAN_COLUMNS) or any(
                c not in attr_cols for c in _REQUIRED_ATTR_COLUMNS):
            scan.note(DQ_SCHEMA)
            return [], {}
        meta = _meta(conn)
        resource: dict[str, Any] = {}
        if meta:
            scan.count("collector_extract")
            resource = {"tokenbill.collector": COLLECTOR_MARKER}
            for key in ("principal", "principal_key_id", "team"):
                if meta.get(key) is not None:
                    resource[f"tokenbill.{key}"] = meta[key]
        attrs_by_span = _attributes(conn)
        cols = [c for c in SPAN_COLUMNS if c in span_cols]
        sql = (f"SELECT {', '.join(_quote(c) for c in cols)} FROM spans "
               "ORDER BY start_time_ms, span_id")
        spans = list(self._spans(conn.execute(sql), cols, attrs_by_span, resource, scan))
        return spans, meta

    @staticmethod
    def _spans(rows: Iterator[tuple[Any, ...]], cols: list[str],
               attrs_by_span: Mapping[str, Mapping[str, Any]], resource: Mapping[str, Any],
               scan: Scan) -> Iterator[SpanView]:
        for n, row in enumerate(rows, start=1):
            scan.count("records")
            rec = dict(zip(cols, row, strict=False))
            span_id = rec.get("span_id")
            trace_id = rec.get("trace_id")
            locator = f"row:{n}"
            if not isinstance(span_id, (str, int)) or not isinstance(trace_id, (str, int)):
                scan.quarantine(locator, "missing:span_id")
                continue
            attrs: dict[str, Any] = dict(attrs_by_span.get(str(span_id), {}))
            for column, key in _COLUMN_ATTRS.items():
                value = rec.get(column)
                if value is not None and key not in attrs and not isinstance(value, bytes):
                    attrs[key] = value
            start = _ms(rec.get("start_time_ms"))
            end = _ms(rec.get("end_time_ms"))
            parent = rec.get("parent_span_id")
            yield SpanView(
                name=_op_name(rec.get("name")), trace_id=str(trace_id), span_id=str(span_id),
                parent_span_id=str(parent) if isinstance(parent, (str, int)) and parent != ""
                else None,
                start_ms=start, end_ms=end if end is not None else start, attributes=attrs,
                resource=resource, events=(), locator=locator,
                error=_error(rec.get("status_code")), agent_product="copilot_vscode")
