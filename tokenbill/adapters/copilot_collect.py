"""Incremental GitHub Copilot CLI collection (addendum §5.9 "Collector", CP-LOCAL).

``tokenbill copilot collect --source cli`` (wired by CP-WIRE, which writes trace@2 through an
injected writer) calls :func:`collect_incremental_copilot` on every run. Per
``session-state/<id>/events.jsonl`` it keeps a :class:`CopilotFileCursor` and the parser's
content-free context (:class:`~tokenbill.adapters.copilot_cli.SessionState`):

* a file is re-read from ``offset`` when it grew and its head hash is unchanged, else from the start
  (rotation / truncation: fresh parser state);
* the offset never passes an unterminated last line nor a chunked ``assistant.message`` response
  still missing its last chunk, unless the file is quiescent (untouched for :data:`QUIESCENT_MS`, or
  an mtime more than :data:`CLOCK_SKEW_MS` after the injected clock); in-flight sessions are simply
  read again from their cursor on the next run;
* sessions whose raw id is in ``skip_session_ids`` (conversations CP-VSCODE's extract already covers
  with per-request VS Code spans) yield their lane events only — no request, inference, rollup
  aggregate or COST_STATE — counted as ``dq.copilot_session_covered_by_vscode``;
* with ``"copilot-store" in opts.experimental`` the store is read **after** the events (its rows
  commit before their leg's ``session.shutdown``), rows above a per-database high-water mark on
  ``assistant_usage_events.id``; rows and messages of a leg are joined once the leg is closed (a
  shutdown or a later resume was read) or the file is quiescent, so a row is never emitted twice
  and an output is never counted both as a row and as a residual. A locked store is retried once;
  then the whole run is skipped (nothing committed, ``dq.copilot_store_unavailable``).

Copilot files are only read (their mtimes never change). The state file is private
(``core.jsonl.open_private``, written atomically) and holds no content: HMAC path ids, offsets,
hashes, ``h_`` names, model tokens, lane keys and numbers. Content tier is fixed to ``none``.
Unclean shutdowns are reported when the next ``session.resume`` arrives (an idle session is not a
crashed one); a one-shot ``copilot-cli`` read also reports a last leg without a shutdown.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tokenbill.adapters.copilot_cli import (
    STORE_FILE,
    STORE_FLAG,
    SessionParser,
    SessionState,
    StoreRow,
    _Run,
    _sha256_file,
    check_options,
    emit_session,
    emit_store_only,
    iter_events_files,
    read_store,
    session_ids,
    source_id_for,
)
from tokenbill.core import jsonl
from tokenbill.core.errors import SourceError
from tokenbill.core.types import IngestOptions, IngestResult

__all__ = [
    "CLOCK_SKEW_MS",
    "QUIESCENT_MS",
    "STATE_SCHEMA",
    "CopilotCollectorState",
    "CopilotFileCursor",
    "collect_incremental_copilot",
    "session_ids",
]

logger = logging.getLogger("tokenbill.adapters.copilot_collect")

STATE_SCHEMA = "tokenbill/copilot-collector@1"
#: An ``events.jsonl`` untouched for this long is final: open responses and an unterminated last
#: line are processed as they are, and held messages / store rows are joined.
QUIESCENT_MS = 10 * 60_000
#: An mtime this far after ``now_ms`` means the clock was injected (tests, replays): final.
CLOCK_SKEW_MS = 60_000
#: Store row ids above the high-water mark remembered as emitted (bounded).
DONE_WINDOW = 65_536
HEAD_SHA_BYTES = jsonl.HEAD_SHA_BYTES


@dataclass
class CopilotFileCursor:
    """Where the collector stopped in one ``events.jsonl``."""

    path_hmac: str
    offset: int
    head_sha: str
    size: int
    mtime_ns: int

    def to_json(self) -> dict[str, Any]:
        """JSON form (the state file)."""
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, d: object) -> CopilotFileCursor | None:
        """Parse a cursor; None when any field is missing or has the wrong type."""
        if not isinstance(d, Mapping):
            return None
        try:
            cur = cls(path_hmac=d["path_hmac"], offset=d["offset"], head_sha=d["head_sha"],
                      size=d["size"], mtime_ns=d["mtime_ns"])
        except (KeyError, TypeError):
            return None
        ints = (cur.offset, cur.size, cur.mtime_ns)
        if (not isinstance(cur.path_hmac, str) or not isinstance(cur.head_sha, str)
                or not all(type(v) is int and v >= 0 for v in ints)):
            return None
        return cur


@dataclass
class CopilotCollectorState:
    """Every file's cursor and parser context, plus the store high-water mark; a private JSON
    file."""

    cursors: dict[str, CopilotFileCursor] = field(default_factory=dict)
    contexts: dict[str, dict[str, Any]] = field(default_factory=dict)
    store: dict[str, Any] = field(default_factory=dict)   # {"source", "hwm", "done"}

    @staticmethod
    def load(path: Path) -> CopilotCollectorState:
        """Read the state file; a missing file is an empty state. An unreadable or corrupt file is
        logged and replaced by an empty state (the next run re-reads everything; the store's merge
        makes that idempotent)."""
        path = Path(path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return CopilotCollectorState()
        except OSError as exc:
            logger.warning("collector state unreadable (%s); starting fresh", type(exc).__name__)
            return CopilotCollectorState()
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            logger.warning("collector state is not valid JSON; starting fresh")
            return CopilotCollectorState()
        if not isinstance(doc, dict) or doc.get("schema") != STATE_SCHEMA:
            logger.warning("collector state has an unknown schema; starting fresh")
            return CopilotCollectorState()
        state = CopilotCollectorState()
        cursors, contexts, store = doc.get("cursors"), doc.get("contexts"), doc.get("store")
        if isinstance(cursors, dict):
            for key, value in cursors.items():
                cur = CopilotFileCursor.from_json(value)
                if isinstance(key, str) and cur is not None and cur.path_hmac == key:
                    state.cursors[key] = cur
        if isinstance(contexts, dict):
            for key, value in contexts.items():
                if isinstance(key, str) and isinstance(value, dict) and key in state.cursors:
                    state.contexts[key] = value
        if isinstance(store, dict):
            hwm, done, source = store.get("hwm"), store.get("done"), store.get("source")
            if (type(hwm) is int and hwm >= 0 and isinstance(source, str)
                    and isinstance(done, list) and all(type(i) is int for i in done)):
                state.store = {"source": source, "hwm": hwm, "done": done}
        return state

    def save(self, path: Path) -> None:
        """Write the state atomically to an owner-only file (``open_private``; the directory is
        created ``0700``)."""
        path = Path(path)
        doc = {"schema": STATE_SCHEMA,
               "cursors": {k: v.to_json() for k, v in sorted(self.cursors.items())},
               "contexts": {k: v for k, v in sorted(self.contexts.items())},
               "store": self.store}
        text = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        tmp = path.with_name(path.name + ".tmp")
        with jsonl.open_private(tmp, "w") as f:
            f.write(text)
            warning = jsonl.acl_warning(f)
        if warning:
            logger.warning("collector state: %s", warning)
        os.replace(tmp, path)


@dataclass
class _Work:
    """One ``events.jsonl`` of this run: its parser, cursor candidate and byte range."""

    key: str
    path: Path
    run: _Run
    parser: SessionParser
    quiescent: bool
    start: int
    offset: int
    size: int
    mtime_ns: int
    head: str
    parsed: bool


def _head_sha(path: Path, n: int) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read(n)).hexdigest()
    except OSError as exc:
        raise SourceError(f"{path.name}: unreadable ({type(exc).__name__})") from None


def _restore(ctx: Mapping[str, Any] | None) -> SessionState | None:
    if ctx is None:
        return None
    try:
        return SessionState.from_json(ctx)
    except ValueError:
        logger.warning("collector context unusable; re-reading the session from the start")
        return None


def _prepare(path: Path, state: CopilotCollectorState, opts: IngestOptions, now_ms: int, *,
             store_mode: bool) -> _Work | None:
    """Parse the new bytes of *path* (two passes when a response is still open); None when the
    file is unchanged and holds nothing to settle."""
    key = source_id_for(opts, path)
    try:
        st = os.stat(path)
    except OSError:
        return None
    size, mtime_ns = st.st_size, st.st_mtime_ns
    age_ms = now_ms - mtime_ns // 1_000_000
    quiescent = age_ms >= QUIESCENT_MS or age_ms < -CLOCK_SKEW_MS
    cur = state.cursors.get(key)
    restored = _restore(state.contexts.get(key)) if cur is not None else None
    if cur is not None and restored is None and state.contexts.get(key) is not None:
        cur = None
    if cur is not None:
        unchanged = cur.size == size and cur.mtime_ns == mtime_ns
        if not unchanged and (size < cur.offset or _head_sha(path, min(HEAD_SHA_BYTES, cur.size))
                              != cur.head_sha):
            logger.info("events.jsonl rotated or truncated; re-reading from the start")
            cur, restored = None, None
    start = cur.offset if cur is not None else 0
    nothing_new = cur is not None and cur.size == size and cur.mtime_ns == mtime_ns and not (
        cur.offset < size and quiescent)
    if nothing_new and not store_mode:
        return None
    head = _head_sha(path, min(HEAD_SHA_BYTES, size))

    def fresh() -> tuple[_Run, SessionParser]:
        run = _Run(opts, key)
        snapshot = SessionState.from_json(restored.to_json()) if restored is not None else None
        parser = SessionParser(run, path, state=snapshot)
        parser.restore_shells()
        return run, parser

    run, parser = fresh()
    if nothing_new:
        return _Work(key, path, run, parser, quiescent, start, start, size, mtime_ns, head, False)
    parser.parse(start_offset=start, final=quiescent, size=size)
    marks = [m for m in (parser.open_offset, parser.unterminated) if m is not None]
    resume = min(marks) if marks and not quiescent else None
    if resume is None:
        return _Work(key, path, run, parser, quiescent, start, size, size, mtime_ns, head, True)
    run, parser = fresh()
    if resume > start:
        parser.parse(start_offset=start, stop_at=resume, final=True, size=size)
    return _Work(key, path, run, parser, quiescent, start, max(resume, start), size, mtime_ns,
                 head, resume > start)


def collect_incremental_copilot(home: Path, state: CopilotCollectorState, opts: IngestOptions,
                                *, now_ms: int, skip_session_ids: frozenset[str] = frozenset()
                                ) -> Iterator[IngestResult]:
    """One :class:`IngestResult` per ``events.jsonl`` under ``<home>/session-state`` with new
    records since the last run (plus one for store rows of sessions without an events file),
    updating *state* in place (the caller saves it after consuming the iterator).

    *skip_session_ids* holds raw session ids (``session-state`` directory names) that CP-VSCODE's
    extract already covers: those sessions contribute lane events only
    (``dq.copilot_session_covered_by_vscode``). Cursors of files no longer on disk are dropped once
    the iterator is exhausted; use one state per Copilot home."""
    check_options(opts)
    opts = dataclasses.replace(opts, now_ms=now_ms) if opts.now_ms != now_ms else opts
    home = Path(home)
    skip = frozenset(skip_session_ids)
    files = iter_events_files(home)
    store_path = home / STORE_FILE
    store_mode = STORE_FLAG in opts.experimental and store_path.is_file()
    works: list[_Work] = []
    for path in files:
        try:
            work = _prepare(path, state, opts, now_ms, store_mode=store_mode)
        except SourceError:
            if not opts.lenient:
                raise
            logger.warning("events.jsonl unreadable; skipped this run")
            continue
        if work is not None:
            works.append(work)

    store_run: _Run | None = None
    rows_by_sid: dict[str, list[StoreRow]] = {}
    store_meta: dict[str, tuple[str | None, str | None]] = {}
    store_source = ""
    hwm = 0
    top = 0
    done: set[int] = set()
    if store_mode:
        store_source = source_id_for(opts, store_path)
        store_run = _Run(opts, store_source)
        prior = state.store if state.store.get("source") == store_source else {}
        hwm = int(prior.get("hwm", 0))
        done = set(prior.get("done", []))
        read = read_store(store_path, store_run, after_id=hwm)
        if read.status == "unavailable":
            # nothing is committed: every cursor stays, the next run retries the store
            yield store_run.result(store_run.source_info(store_path, "", 0))
            return
        if read.status != "ok":
            store_mode = False
        top = max(hwm, read.max_id if read.max_id is not None else hwm)
        done.update(read.bad_ids)
        for row in read.rows:
            if row.row_id not in done:
                rows_by_sid.setdefault(row.session_id, []).append(row)
        store_meta = read.sessions

    def commit_store(held: list[int] | None) -> None:
        """Record emitted row ids (and, at the end, the new high-water mark) in *state*."""
        if not store_source:
            return
        new_hwm = hwm if held is None else max(hwm, min(held) - 1 if held else top)
        state.store = {"source": store_source, "hwm": new_hwm,
                       "done": sorted(i for i in done if i > new_hwm)[-DONE_WINDOW:]}

    for work in works:
        st = work.parser.state
        sids = [x for x in dict.fromkeys((st.raw_sid, st.dir_id)) if x]
        rows = [row for sid in sids for row in rows_by_sid.pop(sid, [])]
        if not work.parsed and not rows and not st.pending and not work.quiescent:
            continue
        if store_mode and st.repo is None and st.cwd is None:
            meta = next((store_meta[s] for s in sids if s in store_meta), None)
            if meta is not None:
                st.repo, st.cwd = meta
        used = emit_session(work.run, work.parser, rows=rows, store_mode=store_mode,
                            covered=any(s in skip for s in sids), settle_all=work.quiescent)
        done.update(r.row_id for r in used)
        for row in rows:   # unsettled rows of an open leg: kept for the next run
            if row.row_id not in done:
                rows_by_sid.setdefault(row.session_id, []).append(row)
        state.cursors[work.key] = CopilotFileCursor(
            path_hmac=work.key, offset=work.offset, head_sha=work.head, size=work.size,
            mtime_ns=work.mtime_ns)
        state.contexts[work.key] = st.to_json()
        commit_store(None)
        if not work.parsed and not work.run.requests and not work.run.events:
            continue
        digest, n = _sha256_file(work.path, work.start, work.offset)
        yield work.run.result(work.run.source_info(work.path, digest, n))

    if store_run is not None:
        held: list[int] = []
        cutoff = now_ms - QUIESCENT_MS
        live = session_ids(home)
        for sid, rows in sorted(rows_by_sid.items()) if store_mode else ():
            if sid in skip:
                store_run.dq["dq.copilot_session_covered_by_vscode"] += 1
                done.update(r.row_id for r in rows)
                continue
            settled = [r for r in rows if sid not in live and r.created_ms < cutoff]
            if settled:
                emit_store_only(store_run, sid, settled, store_meta.get(sid))
                done.update(r.row_id for r in settled)
            held.extend(r.row_id for r in rows if r.row_id not in done)
        if store_mode:
            commit_store(held)
        if store_run.requests or store_run.dq or store_run.quarantined:
            digest, n = _sha256_file(store_path)
            yield store_run.result(store_run.source_info(store_path, digest, n))

    # forget the cursors of sessions deleted from disk (one state per Copilot home)
    live_keys = {source_id_for(opts, p) for p in files}
    for key in [k for k in state.cursors if k not in live_keys]:
        del state.cursors[key]
    for key in [k for k in state.contexts if k not in live_keys]:
        del state.contexts[key]
