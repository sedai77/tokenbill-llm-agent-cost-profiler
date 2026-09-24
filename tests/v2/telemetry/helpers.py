"""Area-local helpers for the TELEM tests: fixture paths, ingest options, OTLP/JSON builders.

Only imported by tests in ``tests/v2/telemetry`` (SPEC D45).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from tokenbill.core.builders import CANARY, CANARY_EMAIL
from tokenbill.core.ids import pseudonym
from tokenbill.core.records import to_json
from tokenbill.core.testing import (
    CONFORMANCE_NAME_KEY,
    CONFORMANCE_PRINCIPAL_KEY,
    conformance_ingest_options,
)
from tokenbill.core.types import IngestOptions, IngestResult

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "telemetry"
OTLP_CC = FIXTURES / "otlp_claude_code.jsonl"
OTLP_GENAI = FIXTURES / "otlp_genai.jsonl"
OTLP_OI = FIXTURES / "otlp_openinference.jsonl"
OPENAI = FIXTURES / "openai_usage.jsonl"
BEDROCK = FIXTURES / "bedrock_invocations.jsonl"
ANTHROPIC = FIXTURES / "anthropic_responses.jsonl"
GOLDEN = FIXTURES / "conventions_golden.json"

T0 = 1_790_154_000_000  # 2026-09-23T09:00:00Z
ORG_KEY = CONFORMANCE_PRINCIPAL_KEY
NAME_KEY = CONFORMANCE_NAME_KEY
TEAM_MAP = ((CANARY_EMAIL, "payments"),
            ("arn:aws:sts::123456789012:assumed-role/DevRole", "platform"))

__all__ = [
    "ANTHROPIC", "BEDROCK", "CANARY", "CANARY_EMAIL", "FIXTURES", "GOLDEN", "NAME_KEY", "OPENAI",
    "ORG_KEY", "OTLP_CC", "OTLP_GENAI", "OTLP_OI", "T0", "TEAM_MAP", "attr", "blob", "central",
    "event", "logs", "metrics", "p_of", "point", "spans", "span", "write_lines",
]


def central(**kw: Any) -> IngestOptions:
    """Central-ingest options (org key as the principal key, collection key as the name key) with
    the fixture team map."""
    base: dict[str, Any] = {"identity_mode": "central-ingest", "team_map": TEAM_MAP}
    base.update(kw)
    return conformance_ingest_options(**base)


def p_of(raw: str) -> str:
    """The ``p_`` pseudonym of *raw* under the fixture org key."""
    return pseudonym(ORG_KEY, "p", raw)


def blob(result: IngestResult) -> str:
    """Everything an adapter produced, as one JSON string (for leak checks)."""
    return repr(result) + json.dumps(to_json(result), sort_keys=True)


def write_lines(path: Path, records: Iterable[Any]) -> Path:
    """Write JSONL (objects are JSON-encoded, strings written verbatim)."""
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write((rec if isinstance(rec, str) else json.dumps(rec)) + "\n")
    return path


# ---------------------------------------------------------------------------------------------
# OTLP/JSON builders (int64 as decimal strings, as the encoding requires)
# ---------------------------------------------------------------------------------------------

def attr(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    if isinstance(value, list):
        return {"key": key, "value": {"arrayValue": {"values": [
            attr("x", v)["value"] for v in value]}}}
    if isinstance(value, dict):
        return {"key": key, "value": {"kvlistValue": {"values": [
            attr(k, v) for k, v in value.items()]}}}
    return {"key": key, "value": {"stringValue": value}}


def _attrs(values: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [attr(k, v) for k, v in values.items()]


def event(name: str, t_ms: int, attrs: Mapping[str, Any] | None = None, *,
          body: bool = True, event_attr: bool = True) -> dict[str, Any]:
    """A Claude Code log record ``claude_code.<name>`` at *t_ms*."""
    rec: dict[str, Any] = {"timeUnixNano": str(t_ms * 1_000_000),
                           "attributes": ([attr("event.name", name)] if event_attr else [])
                           + _attrs(attrs or {})}
    if body:
        rec["body"] = {"stringValue": f"claude_code.{name}"}
    return rec


def logs(records: list[dict[str, Any]], resource: Mapping[str, Any] | None = None
         ) -> dict[str, Any]:
    return {"resourceLogs": [{"resource": {"attributes": _attrs(resource or {})},
                              "scopeLogs": [{"logRecords": records}]}]}


def span(name: str, span_id: str, start_ms: int, end_ms: int, attrs: Mapping[str, Any],
         *, trace: str = "ab" * 16, parent: str | None = None,
         error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"traceId": trace, "spanId": span_id, "name": name,
                           "startTimeUnixNano": str(start_ms * 1_000_000),
                           "endTimeUnixNano": str(end_ms * 1_000_000),
                           "attributes": _attrs(attrs)}
    if parent:
        out["parentSpanId"] = parent
    if error:
        out["status"] = {"code": 2}
    return out


def spans(items: list[dict[str, Any]], resource: Mapping[str, Any] | None = None
          ) -> dict[str, Any]:
    return {"resourceSpans": [{"resource": {"attributes": _attrs(resource or {})},
                               "scopeSpans": [{"spans": items}]}]}


def metrics(items: list[dict[str, Any]], resource: Mapping[str, Any] | None = None
            ) -> dict[str, Any]:
    return {"resourceMetrics": [{"resource": {"attributes": _attrs(resource or {})},
                                 "scopeMetrics": [{"metrics": items}]}]}


def point(start_ms: int, end_ms: int, value: Any, attrs: Mapping[str, Any]) -> dict[str, Any]:
    key = "asDouble" if isinstance(value, float) else "asInt"
    return {"attributes": _attrs(attrs), "startTimeUnixNano": str(start_ms * 1_000_000),
            "timeUnixNano": str(end_ms * 1_000_000),
            key: value if isinstance(value, float) else str(value)}
