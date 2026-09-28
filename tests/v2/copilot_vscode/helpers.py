"""Shared helpers of the CP-VSCODE tests: the fixture builder, identities and extract readers."""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any

from tokenbill.adapters import copilot_vscode_collect as cvc
from tokenbill.core.ids import hmac_hex, key_id

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "copilot_vscode"


def _load_maker() -> ModuleType:
    name = "_cp_vscode_make_traces"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, FIXTURES / "make_traces.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mk = _load_maker()

NAME_KEY = bytes(range(32))
COLLECTION_KEY = bytes(range(32, 64))
C_PRINCIPAL = "c_" + hmac_hex(COLLECTION_KEY, b"employee-4711")
NOW_MS = mk.BASE_MS + 86_400_000  # one day after the fixture spans


def identity(principal: str | None = C_PRINCIPAL, team: str | None = "platform",
             name_key: bytes = NAME_KEY) -> cvc.CollectorIdentity:
    """A two-stage identity by default (``c_`` principal under the collection key)."""
    pkid = key_id(COLLECTION_KEY) if principal is not None else None
    return cvc.CollectorIdentity(principal=principal, principal_key_id=pkid, team=team,
                                 name_key=name_key, name_key_id=key_id(name_key))


def collect(tmp: Path, *, dbs: Iterable[Path] = (), outfiles: Iterable[Path] = (),
            state: cvc.VsCodeCollectorState | None = None, now_ms: int = NOW_MS,
            ident: cvc.CollectorIdentity | None = None, out: str = "out",
            cli: frozenset[str] = frozenset()) -> tuple[cvc.CollectResult,
                                                          cvc.VsCodeCollectorState]:
    """One collector run into ``tmp / out``."""
    st = state if state is not None else cvc.VsCodeCollectorState()
    res = cvc.collect_vscode_extracts(
        cvc.VsCodeSources(traces_dbs=tuple(dbs), otel_outfiles=tuple(outfiles)), st,
        out_dir=tmp / out, identity=ident or identity(), now_ms=now_ms, cli_session_ids=cli)
    return res, st


def read_extract(path: Path) -> dict[str, Any]:
    """Tables of a database extract: ``spans`` rows as dicts, ``span_attributes`` triples, the
    meta mapping, table names and ``spans`` column names."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        tables = sorted(r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(spans)")]
        spans = [dict(r) for r in conn.execute("SELECT * FROM spans ORDER BY start_time_ms, "
                                                "span_id")]
        attrs = [tuple(r) for r in conn.execute("SELECT span_id, key, value FROM span_attributes "
                                                 "ORDER BY span_id, key")]
        meta = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM tokenbill_meta")}
    finally:
        conn.close()
    return {"tables": tables, "columns": cols, "spans": spans, "attrs": attrs, "meta": meta}


def db_files(res: cvc.CollectResult) -> list[Path]:
    """The database extracts of a run."""
    return [f for f in res.files if f.suffix == ".db"]


def otel_files(res: cvc.CollectResult) -> list[Path]:
    """The OTel extracts of a run."""
    return [f for f in res.files if f.suffix == ".jsonl"]


def codes(res: cvc.CollectResult) -> list[str]:
    """Data-quality codes of a run (sorted)."""
    return sorted(n.code for n in res.notes)


@contextmanager
def traced(monkeypatch: Any) -> Iterator[list[str]]:
    """Record every SQL statement run on read-only source connections (SQLite trace hook)."""
    statements: list[str] = []
    real = cvc._open_readonly

    def opener(path: Path) -> sqlite3.Connection:
        conn = real(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(cvc, "_open_readonly", opener)
    yield statements


def file_state(path: Path) -> tuple[bytes, int]:
    """Bytes and mtime (ns) of a file."""
    return path.read_bytes(), os.stat(path).st_mtime_ns
