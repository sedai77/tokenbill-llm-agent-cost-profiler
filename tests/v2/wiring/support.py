"""Helpers of the WIRING tests (imported only inside ``tests/v2/wiring``, SPEC D45).

* Fake adapters over a tiny JSON-lines test format (``{"format": "tb-wiring-fake", …}`` header, one
  request per line), registered per test by the ``fake_adapters`` fixture (``conftest.py``).
* :class:`PathStore` — a ``MemoryStore`` that can be opened by path (the file holds the pickled
  ``IngestResult`` list), so shard workers can open it read-only like ``SqliteStore``.
* Top-level shard functions (picklable, for the process pool).

Everything is synthetic; no real transcript, key or network is involved.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar

from tokenbill.config import Config
from tokenbill.core.builders import make_config, make_license, make_request
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.errors import UsageError
from tokenbill.core.ids import key_id, stable_id
from tokenbill.core.records import (
    Attribution,
    Fidelity,
    Lane,
    LaneEvent,
    LaneEventKind,
    LaneKind,
    Session,
    SourceRef,
    UsageBuckets,
)
from tokenbill.core.shards import shard_where
from tokenbill.core.testing import FakePricer, MemoryRecordStore, MemoryStore
from tokenbill.core.types import (
    DataQualityNote,
    IngestOptions,
    IngestResult,
    QuarantineItem,
    ShardKey,
    SourceInfo,
)
from tokenbill.pipeline.common import Env, shard_store

ORG_KEY = bytes(range(10, 42))
NAME_KEY = bytes(range(50, 82))
EXPORT_KEY = bytes(range(90, 122))
DAY_MS = 86_400_000
T0 = 1_790_121_600_000          # 2026-09-23T00:00:00Z (Opus 5.5 is priced from 2026-09-22)
FAKE_FORMAT = "tb-wiring-fake"
ALT_FORMAT = "tb-wiring-alt"
H_REPO = "h_0123456789abcdef0123"


def make_env(*, org_key: bytes | None = ORG_KEY, name_key: bytes | None = NAME_KEY,
             now_ms: int = T0 + DAY_MS, pricer: Any = None, **config_kw: Any) -> Env:
    """An Env over ``FakePricer`` with in-memory keys (``build_env`` reads keys from files)."""
    config = Config(**config_kw)
    return Env(config=config, pricer=pricer if pricer is not None else FakePricer(),
               rules=RulesTable(), k=config.k, jobs=config.jobs, org_key=org_key,
               name_key=name_key, name_key_id=key_id(name_key) if name_key else None,
               now_ms=now_ms)


def write_key(path: Path, key: bytes) -> Path:
    """A key file as ``core.keys`` writes it (64 hex characters, mode 0600)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key.hex() + "\n", encoding="ascii")
    os.chmod(path, 0o600)
    return path


def write_fake(path: Path, records: Sequence[Mapping[str, Any]] = (), *,
               fmt: str = FAKE_FORMAT, **header: Any) -> Path:
    """A test source file: a header line then one JSON record per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"format": fmt, **header})] + [json.dumps(r) for r in records]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def req(lane: str, seq: int, *, ts: int | None = None, team: str | None = "payments",
        principal: str | None = None, u: int = 1000, r: int = 0, w: int = 0, o: int = 100,
        model: str = "claude-opus-5-5", **extra: Any) -> dict[str, Any]:
    """One request record of the test format."""
    return {"lane": lane, "seq": seq, "ts": T0 + 3_600_000 + seq * 1000 if ts is None else ts,
            "team": team, "principal": principal, "u": u, "r": r, "w": w, "o": o,
            "model": model, **extra}


def _files(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(p for p in path.rglob("*") if p.is_file() and not any(
            part.startswith(".") for part in p.relative_to(path).parts))
    return [path]


def read_fake(path: Path, opts: IngestOptions, adapter: str, *,
              records_key: str | None = None) -> IngestResult:
    """Build the IngestResult of a test file (or of every test file in a directory)."""
    raw = b"".join(p.read_bytes() for p in _files(path))
    header: dict[str, Any] = {}
    records: list[dict[str, Any]] = []
    for member in _files(path):
        lines = [json.loads(line) for line in member.read_text("utf-8").splitlines()
                 if line.strip()]
        if not lines or lines[0].get("format") not in (FAKE_FORMAT, ALT_FORMAT):
            continue
        header = {**header, **lines[0]}
        records.extend(lines[1:])
    if records_key is not None:
        records = list(header.get(records_key, []))
    source = SourceInfo(
        source_id=stable_id("s", adapter, path.name), adapter=adapter,
        name_hmac=stable_id("h", path.name), sha256=hashlib.sha256(raw).hexdigest(),
        bytes=len(raw), name_key_id=header.get("name_key_id", opts.name_key_id or None),
        principal_key_id=header.get("principal_key_id"))
    source_adapter = header.get("source_adapter", adapter)
    requests = []
    events = []
    kinds: dict[tuple[str, str], str] = {}
    for i, rec in enumerate(records):
        session = rec.get("session", "s_" + rec["lane"])
        kinds[(session, rec["lane"])] = rec.get("kind", "main")
        if rec.get("event") == "compaction":
            attrs: tuple[tuple[str, Any], ...] = (("pre_tokens", rec.get("pre", 150_000)),
                                                  ("trigger", "auto"), ("duration_ms", 1000))
            if "post" in rec:
                attrs += (("post_tokens", rec["post"]),)
            events.append(LaneEvent(lane_key=rec["lane"], ts_ms=rec["ts"],
                                    kind=LaneEventKind.COMPACTION, attrs=attrs))
            continue
        bp = rec.get("billing_path")
        ctx: dict[str, Any] = {}
        if bp:
            ctx["billing_path"] = bp
        if bp in ("copilot_pool", "copilot_direct"):
            ctx.update(provider="github", channel="github_copilot")
        requests.append(make_request(
            rec["lane"], rec["seq"], rec["ts"],
            {"uncached_input": rec.get("u", 0), "cache_read": rec.get("r", 0),
             "cache_write_5m": rec.get("w", 0), "output": rec.get("o", 0)},
            rec.get("model", "claude-opus-5-5"), session_key=session,
            attribution=Attribution(principal=rec.get("principal"), team=rec.get("team"),
                                    billing_path=bp, repo=rec.get("repo")),
            usage_source=rec.get("usage_source", "final"),
            output_upper=rec.get("output_upper"), billable=rec.get("billable", True),
            message_id=rec.get("msg"),
            source=SourceRef(adapter=source_adapter, source_id=source.source_id,
                             locator=f"line:{i + 2}", fidelity=Fidelity.FULL, priority=40),
            **ctx))
    sessions = []
    for session in sorted({s for s, _ in kinds}):
        lanes = tuple(Lane(lane_key=lane, session_key=session, kind=LaneKind(kind),
                           parent_lane_key=None, cache_scope_key="ws:test", requests=())
                      for (s, lane), kind in sorted(kinds.items()) if s == session)
        sessions.append(Session(session_key=session, source_kind="test", attribution=Attribution(),
                                lanes=lanes, started_ms=0, ended_ms=0))
    notes = [DataQualityNote(**n) for n in header.get("notes", [])]
    quarantined = [QuarantineItem(source_id=source.source_id, locator=f"line:{900 + i}",
                                  reason="bad_json") for i in range(header.get("quarantine", 0))]
    naive = {m: UsageBuckets(**v) for m, v in header.get("naive", {}).items()}
    licenses = [make_license(p, snapshot_date="2026-09-20") for p in header.get("licenses", [])]
    config = [make_config("run_flags", {"promo_eligible": True}) for _ in
              range(header.get("config", 0))]
    return IngestResult(source=source, requests=requests, sessions=sessions, events=events,
                        aggregates=[], cost_lines=[], outcomes=[], quarantined=quarantined,
                        notes=notes, stats=dict(header.get("stats", {})),
                        capabilities=frozenset({"usage_sequence", "timing"}),
                        naive_usage=naive, licenses=licenses, config=config)


class FakeUsageAdapter:
    """``wiring-fake``: sniffs the test format; records every read (path, options)."""

    name = "wiring-fake"
    capabilities = frozenset({"usage_sequence", "timing"})
    reads: ClassVar[list[tuple[str, str, IngestOptions]]] = []
    magic = b'{"format": "' + FAKE_FORMAT.encode() + b'"'

    def sniff(self, path: Path, head: bytes) -> bool:
        return head.startswith(self.magic)

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        FakeUsageAdapter.reads.append((self.name, str(path), opts))
        return read_fake(path, opts, self.name)


class AltAdapter(FakeUsageAdapter):
    """``wiring-fake-alt``: a second format (mixed directories)."""

    name = "wiring-fake-alt"
    magic = b'{"format": "' + ALT_FORMAT.encode() + b'"'


class DeferredAdapter(FakeUsageAdapter):
    """``wiring-fake-b``: never sniffs; reads the header's ``b_records`` (a deferral target)."""

    name = "wiring-fake-b"

    def sniff(self, path: Path, head: bytes) -> bool:
        return False

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        FakeUsageAdapter.reads.append((self.name, str(path), opts))
        result = read_fake(path, opts, self.name, records_key="b_records")
        result.stats.clear()
        result.stats["defer:wiring-fake"] = 1   # a deferral back must not loop
        return result


class ExportAdapter(FakeUsageAdapter):
    """Stands in for ``copilot-export`` (the only adapter whose key ids a store adopts)."""

    name = "copilot-export"

    def sniff(self, path: Path, head: bytes) -> bool:
        return False


class NotAnAdapterResult(FakeUsageAdapter):
    """``wiring-broken``: returns something that is not an IngestResult."""

    name = "wiring-broken"

    def sniff(self, path: Path, head: bytes) -> bool:
        return False

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        return {"not": "a result"}  # type: ignore[return-value]


# ---------------------------------------------------------------------------------------------
# a store that can be opened by path
# ---------------------------------------------------------------------------------------------


class PathStore(MemoryStore):
    """``MemoryStore`` persisted as a pickled list of ingested results at *path*; opened with the
    ``SqliteStore`` keywords (``create``, ``org_key``, ``name_key_id``, ``pricer``, ``read_only``,
    ``adopt_key_ids``)."""

    opened: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, path: Path, *, create: bool = True, read_only: bool = False,
                 org_key: bytes | None = ORG_KEY, name_key_id: str | None = None,
                 pricer: Any = None, adopt_key_ids: bool = False) -> None:
        super().__init__(org_key=org_key, name_key_id=name_key_id,
                         pricer=pricer if pricer is not None else FakePricer(),
                         adopt_key_ids=adopt_key_ids)
        PathStore.opened.append({"path": str(path), "create": create, "read_only": read_only,
                                 "org_key": org_key, "name_key_id": name_key_id,
                                 "pricer": pricer, "adopt_key_ids": adopt_key_ids})
        self.path = Path(path)
        self.read_only = read_only
        self.closed = False
        self._results: list[IngestResult] = []
        if self.path.exists():
            for result in pickle.loads(self.path.read_bytes()):
                self._results.append(result)
                super().ingest(result)
        elif not create:
            raise UsageError("store not found")

    def ingest(self, result: IngestResult, *, pricer: Any = None) -> dict[str, int]:
        if self.read_only:
            raise UsageError("read-only store")
        counts = super().ingest(result, pricer=pricer)
        self._results.append(result)
        self.path.write_bytes(pickle.dumps(self._results))
        return counts

    def close(self) -> None:
        self.closed = True


class NoAdoptStore(PathStore):
    """A store class that does not know ``adopt_key_ids`` (a pre-R-E21 store)."""

    def __init__(self, path: Path, *, create: bool = True, org_key: bytes | None = ORG_KEY,
                 name_key_id: str | None = None, pricer: Any = None) -> None:
        super().__init__(path, create=create, org_key=org_key, name_key_id=name_key_id,
                         pricer=pricer)


class SlotStore:
    """A store object that is not weak-referenceable (``__slots__`` without ``__weakref__``)."""

    __slots__ = ("args",)

    def __init__(self, path: Path, **kw: Any) -> None:
        self.args = (path, kw)


class PathRecordStore(MemoryRecordStore):
    """An extension record store opened as ``cls(db_path, create=…)`` (``open_record_stores``);
    accepts principals under ``key_id(ORG_KEY)``."""

    opened: ClassVar[list[str]] = []
    last: ClassVar[PathRecordStore | None] = None

    def __init__(self, db_path: Path, *, create: bool = True) -> None:
        super().__init__(None, name="wiringtest", org_key_id=key_id(ORG_KEY))
        PathRecordStore.opened.append(str(db_path))
        PathRecordStore.last = self


# ---------------------------------------------------------------------------------------------
# shard functions (top level: picklable for the process pool)
# ---------------------------------------------------------------------------------------------


def shard_label(key: ShardKey) -> str:
    """A pure function of the shard key."""
    return f"{key.team}/{key.lane_kind}"


def shard_pid(key: ShardKey) -> int:
    """The process that ran the shard."""
    return os.getpid()


def shard_lanes(key: ShardKey) -> tuple[Any, ...]:
    """The lanes of the shard as read from the worker's read-only store."""
    store = shard_store()
    lanes = tuple((lane.lane_key, len(lane.requests))
                  for lane in store.iter_lanes(where=shard_where(key)))
    return (key.team, key.lane_kind, lanes, type(store).__name__,
            getattr(store, "read_only", None))


def shard_fail(key: ShardKey) -> str:
    """Raises for the shard of team ``boom``."""
    if key.team == "boom":
        raise UsageError("boom shard")
    return str(key.team)
