"""Shared helpers of the CC tests (this area only).

``bf`` is ``tests/v2/fixtures/claude_code/build_fixtures.py`` loaded by path (the fixture builder
doubles as the transcript/stream builder library).
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
from pathlib import Path
from typing import Any

from tokenbill.adapters.cc_headless import ClaudeCodeHeadlessAdapter
from tokenbill.adapters.claude_code import ClaudeCodeAdapter
from tokenbill.core.ids import key_id
from tokenbill.core.records import Attribution, LaneEventKind, Request, to_json
from tokenbill.core.testing import conformance_ingest_options
from tokenbill.core.types import IngestOptions, IngestResult

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "claude_code"
_spec = importlib.util.spec_from_file_location("cc_build_fixtures",
                                               FIXTURES / "build_fixtures.py")
assert _spec is not None and _spec.loader is not None
bf: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bf)

NAME_KEY = bytes(range(32))
PRINCIPAL_KEY = bytes(range(32, 64))
NOW_MS = bf.T0_MS + 86_400_000   # 2026-09-23T09:00Z
CC = ClaudeCodeAdapter()
HEADLESS = ClaudeCodeHeadlessAdapter()


def opts(**kw: Any) -> IngestOptions:
    """Conformance-style options (install mode, fixed keys, clock 2026-09-23) with overrides;
    ``billing_path=`` / ``attr=`` set ``opts.attribution``."""
    attr = kw.pop("attr", {})
    if "billing_path" in kw:
        attr = dict(attr, billing_path=kw.pop("billing_path"))
    if attr:
        kw["attribution"] = Attribution(**attr)
    kw.setdefault("now_ms", NOW_MS)
    return conformance_ingest_options(**kw)


def write(tmp: Path, tx: Any, name: str | None = None) -> Path:
    """Write a transcript builder under *tmp* (a main-lane path by default)."""
    return tx.write(tmp / "projects" / "-home-dev-x" / (name or f"{tx.session_id}.jsonl"))


def read(tmp: Path, tx: Any, name: str | None = None, **kw: Any) -> IngestResult:
    """Write and import one transcript."""
    return CC.read(write(tmp, tx, name), opts(**kw))


def read_headless(tmp: Path, text: str, name: str = "stream.jsonl", **kw: Any) -> IngestResult:
    path = tmp / name
    path.write_text(text, encoding="utf-8")
    return HEADLESS.read(path, opts(**kw))


def by_message(result: IngestResult) -> dict[str, Request]:
    """Requests keyed by provider message id."""
    return {r.attempts[0].provider_message_id: r for r in result.requests
            if r.attempts[0].provider_message_id}


def note(result: IngestResult, code: str) -> Any:
    """The data-quality note *code* (None when absent)."""
    return next((n for n in result.notes if n.code == code), None)


def events(result: IngestResult, kind: LaneEventKind) -> list[Any]:
    return [e for e in result.events if e.kind is kind]


def attrs(event: Any) -> dict[str, Any]:
    return dict(event.attrs)


def canonical(result: IngestResult) -> str:
    return json.dumps(to_json(result), sort_keys=True)


def set_mtime(path: Path, ms: int) -> None:
    os.utime(path, ns=(ms * 1_000_000, ms * 1_000_000))


def store_dump(store: Any) -> str:
    """Canonical JSON of every merged lane of a store (requests, events, shells)."""
    lanes = list(store.iter_lanes(since_ms=0, until_ms=2**53))
    return json.dumps([to_json(lane) for lane in lanes], sort_keys=True)


def replace_opts(o: IngestOptions, **kw: Any) -> IngestOptions:
    return dataclasses.replace(o, **kw)


__all__ = ["CC", "FIXTURES", "HEADLESS", "NAME_KEY", "NOW_MS", "PRINCIPAL_KEY", "attrs", "bf",
           "by_message", "canonical", "events", "key_id", "note", "opts", "read", "read_headless",
           "replace_opts", "set_mtime", "store_dump", "write"]
