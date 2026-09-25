"""Area-local helpers for the CP-LOCAL tests (imported only by tests in this directory).

:class:`Events` writes synthetic ``events.jsonl`` files in the shape of ``github/copilot-sdk``
``session-events.ts`` @075f027 (envelope ``{type, data, id, parentId, timestamp, agentId?}``);
:func:`make_store` builds a ``session-store.db`` from ``store_ddl.sql`` (third-party column set,
experimental). Content fields always carry the canary.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from tokenbill.core.builders import CANARY
from tokenbill.core.records import to_json
from tokenbill.core.testing import conformance_ingest_options
from tokenbill.core.types import IngestOptions, IngestResult

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "copilot_local"
HOME = FIXTURES / "home"
SID_A = "5b2c6a1e-0000-4a1a-9c11-00000000000a"
SID_B = "5b2c6a1e-0000-4a1a-9c11-00000000000b"
T0 = 1_789_898_400_000          # 2026-09-20T10:00:00Z
CANARY_TEXT = f"secret prompt {CANARY} with a long body of text " * 3

#: G11 (addendum Appendix C): the ccusage issue #1174 payload.
G11_DETAILS = [
    {"tokenType": "input", "tokenCount": 6, "batchSize": 1_000_000,
     "costPerBatch": 500_000_000_000},
    {"tokenType": "cache_read", "tokenCount": 127_386, "batchSize": 1_000_000,
     "costPerBatch": 50_000_000_000},
    {"tokenType": "cache_write", "tokenCount": 2_220, "batchSize": 1_000_000,
     "costPerBatch": 625_000_000_000},
    {"tokenType": "output", "tokenCount": 6_210, "batchSize": 1_000_000,
     "costPerBatch": 2_500_000_000_000},
]
G11_TOTAL_NANO_AIU = 23_284_800_000


def iso(ms: int) -> str:
    """``YYYY-MM-DDTHH:MM:SS.mmmZ`` of epoch *ms*."""
    t = dt.datetime.fromtimestamp(ms // 1000, tz=dt.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S") + f".{ms % 1000:03d}Z"


class Events:
    """A synthetic ``events.jsonl`` under construction (1 s between events by default)."""

    def __init__(self, t0: int = T0, *, prefix: str = "ev") -> None:
        self.t = t0
        self.n = 0
        self.prefix = prefix
        self.objs: list[dict[str, Any]] = []
        self.raw_lines: dict[int, bytes] = {}

    def add(self, typ: str, data: Mapping[str, Any] | None = None, *, dt_ms: int = 1000,
            agent: str | None = None, **extra: Any) -> dict[str, Any]:
        """Append one event and return it."""
        self.t += dt_ms
        self.n += 1
        obj: dict[str, Any] = {"type": typ, "data": dict(data or {}),
                               "id": f"{self.prefix}-{self.n:04d}",
                               "parentId": self.objs[-1]["id"] if self.objs else None,
                               "timestamp": iso(self.t)}
        if agent is not None:
            obj["agentId"] = agent
        obj.update(extra)
        self.objs.append(obj)
        return obj

    def raw(self, line: bytes) -> None:
        """Append a raw line (e.g. a corrupt or concatenated one)."""
        self.raw_lines[len(self.objs)] = line

    def lines(self) -> list[bytes]:
        out: list[bytes] = []
        for i, obj in enumerate(self.objs):
            if i in self.raw_lines:
                out.append(self.raw_lines[i])
            out.append(json.dumps(obj).encode())
        if len(self.objs) in self.raw_lines:
            out.append(self.raw_lines[len(self.objs)])
        return out

    def write(self, path: Path, *, trailing_newline: bool = True) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = b"\n".join(self.lines())
        path.write_bytes(body + (b"\n" if trailing_newline else b""))
        return path

    # --- common events ---
    def start(self, sid: str, **data: Any) -> dict[str, Any]:
        base = {"sessionId": sid, "copilotVersion": "1.0.80", "producer": "copilot-agent",
                "startTime": iso(self.t), "version": 1,
                "context": {"cwd": f"/home/dev/{CANARY}/proj", "repository": f"acme/{CANARY}",
                            "branch": f"feature/{CANARY}", "gitRoot": f"/home/dev/{CANARY}",
                            "hostType": "github", "headCommit": "abc123"}}
        base.update(data)
        return self.add("session.start", base)

    def resume(self, **data: Any) -> dict[str, Any]:
        base = {"eventCount": self.n, "resumeTime": iso(self.t)}
        base.update(data)
        return self.add("session.resume", base)

    def user(self, **kw: Any) -> dict[str, Any]:
        return self.add("user.message", {"content": CANARY_TEXT,
                                         "transformedContent": CANARY_TEXT}, **kw)

    def message(self, api: str | None, out: int, model: str = "claude-sonnet-4.5", *,
                agent: str | None = None, dt_ms: int = 1000, **data: Any) -> dict[str, Any]:
        base: dict[str, Any] = {"messageId": f"m-{self.n}", "content": CANARY_TEXT,
                                "reasoningText": CANARY_TEXT, "model": model,
                                "outputTokens": out, "requestId": f"GH:{self.n:04d}",
                                "toolRequests": [{"name": "bash", "arguments": CANARY_TEXT}]}
        if api is not None:
            base["apiCallId"] = api
        base.update(data)
        return self.add("assistant.message", base, agent=agent, dt_ms=dt_ms)

    def tool(self, call: str, name: str = "bash", *, success: bool = True,
             agent: str | None = None) -> None:
        self.add("tool.execution_start", {"toolCallId": call, "toolName": name,
                                          "arguments": {"command": CANARY_TEXT}}, agent=agent)
        self.add("tool.execution_complete", {
            "toolCallId": call, "success": success,
            "result": {"content": CANARY_TEXT, "detailedContent": CANARY_TEXT}}, agent=agent)

    def compaction(self, *, details: list[dict[str, Any]] | None = None,
                   total: int | None = G11_TOTAL_NANO_AIU, model: str = "claude-opus-4.7",
                   success: bool = True, trigger: str = "threshold",
                   **data: Any) -> dict[str, Any]:
        usage: dict[str, Any] = {"tokenDetails": details if details is not None
                                 else G11_DETAILS}
        if total is not None:
            usage["totalNanoAiu"] = total
        base = {"success": success, "trigger": trigger, "preCompactionTokens": 150_000,
                "postCompactionTokens": 20_000, "tokensRemoved": 130_000, "systemTokens": 3_000,
                "toolDefinitionsTokens": 9_000, "summaryContent": CANARY_TEXT,
                "checkpointPath": f"/home/dev/{CANARY}/cp", "customInstructions": CANARY_TEXT,
                "requestId": "GH:COMPACT",
                "compactionTokensUsed": {"model": model, "duration": 4_200, "inputTokens": 129_612,
                                         "outputTokens": 6_210, "copilotUsage": usage}}
        base.update(data)
        return self.add("session.compaction_complete", base)

    def shutdown(self, metrics: Mapping[str, Mapping[str, int]], *, kind: str = "routine",
                 nano: Mapping[str, int] | None = None) -> dict[str, Any]:
        model_metrics = {}
        for model, usage in metrics.items():
            entry: dict[str, Any] = {"usage": dict(usage), "requests": {"count": 1, "cost": 1}}
            if nano and model in nano:
                entry["totalNanoAiu"] = nano[model]
            model_metrics[model] = entry
        return self.add("session.shutdown", {
            "shutdownType": kind, "modelMetrics": model_metrics, "sessionStartTime": T0,
            "totalApiDurationMs": 1234, "errorReason": CANARY_TEXT if kind == "error" else None,
            "codeChanges": {"filesModified": [f"/src/{CANARY}.py"], "linesAdded": 3,
                            "linesRemoved": 1}})


def usage(inp: int, read: int, write: int, out: int, reasoning: int | None = None
          ) -> dict[str, int]:
    """A ``ShutdownModelMetricUsage`` object."""
    u = {"inputTokens": inp, "cacheReadTokens": read, "cacheWriteTokens": write,
         "outputTokens": out}
    if reasoning is not None:
        u["reasoningTokens"] = reasoning
    return u


def session_file(home: Path, sid: str) -> Path:
    return home / "session-state" / sid / "events.jsonl"


STORE_COLUMNS = ("id", "session_id", "turn_index", "model", "copilot_usage_model",
                 "input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens",
                 "reasoning_tokens", "total_nano_aiu", "duration_ms", "initiator",
                 "request_multiplier", "created_at")


def make_store(path: Path, rows: Iterable[Mapping[str, Any]], *,
               sessions: Iterable[Mapping[str, Any]] = (), drop: Iterable[str] = (),
               without_table: bool = False) -> Path:
    """A ``session-store.db`` from ``store_ddl.sql`` minus the *drop* columns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    dropped = set(drop)
    ddl = (FIXTURES / "store_ddl.sql").read_text()
    conn = sqlite3.connect(path)
    try:
        conn.executescript(ddl)
        if without_table:
            conn.execute("DROP TABLE assistant_usage_events")
        elif dropped:
            keep = [c for c in STORE_COLUMNS if c not in dropped]
            conn.execute(f"CREATE TABLE t AS SELECT {', '.join(keep)} FROM "
                         "assistant_usage_events")
            conn.execute("DROP TABLE assistant_usage_events")
            conn.execute("ALTER TABLE t RENAME TO assistant_usage_events")
        for s in sessions:
            conn.execute("INSERT INTO sessions (id, cwd, repository, branch, summary, host_type, "
                         "producer) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (s["id"], s.get("cwd"), s.get("repository"), f"b/{CANARY}",
                          CANARY_TEXT, "cli", "copilot-agent"))
        if not without_table:
            for r in rows:
                cols = [c for c in r if c not in dropped]
                conn.execute(f"INSERT INTO assistant_usage_events ({', '.join(cols)}) VALUES "
                             f"({', '.join('?' for _ in cols)})", [r[c] for c in cols])
        conn.commit()
    finally:
        conn.close()
    return path


def row(rid: int, sid: str, created_ms: int, *, model: str = "claude-sonnet-4.5",
        inp: int = 10_000, read: int = 8_000, write: int = 1_000, out: int | None = 120,
        nano: int | None = 5_000_000, turn: int | None = None, initiator: str | None = None,
        reasoning: int | None = None) -> dict[str, Any]:
    """One ``assistant_usage_events`` row."""
    return {"id": rid, "session_id": sid, "turn_index": turn, "model": model,
            "copilot_usage_model": model, "input_tokens": inp, "cache_read_tokens": read,
            "cache_write_tokens": write, "output_tokens": out, "reasoning_tokens": reasoning,
            "total_nano_aiu": nano, "duration_ms": 900, "initiator": initiator,
            "request_multiplier": 1.0, "created_at": iso(created_ms)}


def opts(**kw: Any) -> IngestOptions:
    """Conformance options (install identity, fixed keys, 2026-09-23 clock) plus *kw*."""
    return conformance_ingest_options(**kw)


def blob(result: IngestResult) -> str:
    """``repr`` + JSON of a result (canary scans)."""
    return repr(result) + json.dumps(to_json(result), sort_keys=True)


def notes_by_code(result: IngestResult) -> dict[str, int]:
    return {n.code: n.count for n in result.notes}
