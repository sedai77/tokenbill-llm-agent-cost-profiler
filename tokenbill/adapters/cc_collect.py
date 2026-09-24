"""Incremental Claude Code collection (SPEC §5.3 "Incremental collection", §5.4).

The on-device collector (``tokenbill collect claude-code``, wired by CLI-LEDGER) calls
:func:`collect_incremental` on every run. For each transcript it keeps a :class:`FileCursor`:

* a file is re-read from ``offset`` when ``size ≥ offset`` and the head hash is unchanged, else from
  the start (rotation / truncation: fresh parser state);
* the offset never passes an unclosed assistant group (§5.3: closed = a non-null ``stop_reason``
  or followed by a non-assistant entry) nor an unterminated last line. It stops at the start of
  the earliest *trailing* group — one not yet followed by a conversation entry, even when its last
  line carries a stop reason — so those calls are re-read, and **not emitted**, until the next
  entry closes them: a one-shot import finalizes them at that point too, which keeps both paths
  record-for-record identical. A quiescent file (untouched for :data:`QUIESCENT_MS`), or any file
  with ``final=True``, is emitted to its end;
* the set of recent uuids (duplicate-line detection) is bounded to the last 2,000.

Besides the cursors, :class:`CollectorState` keeps each file's content-free parser context (the
triggers, pending appended sizes, recent tool names, quota state, …) at the cursor offset, so a
resumed read derives every record exactly as a one-shot import of the whole file would. Re-emitting
a message id with a larger output is still safe: the store keeps the max-output row (§7.3).

The state file is private (``core.jsonl.open_private``) and holds no content: HMAC path ids,
offsets, hashes, uuids, tool ids, allowlisted/``h_`` names and numbers.
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

from tokenbill.adapters.claude_code import (
    ContextError,
    build_result,
    check_options,
    iter_claude_files,
    parse_file,
    retention_note,
    source_id_for,
    source_info,
)
from tokenbill.core import jsonl
from tokenbill.core.errors import SourceError
from tokenbill.core.types import DataQualityNote, IngestOptions, IngestResult

__all__ = [
    "CLOCK_SKEW_MS",
    "HEAD_SHA_BYTES",
    "QUIESCENT_MS",
    "RECENT_UUIDS",
    "STATE_SCHEMA",
    "CollectorState",
    "FileCursor",
    "collect_incremental",
    "retention_check",
]

logger = logging.getLogger("tokenbill.adapters.cc_collect")

STATE_SCHEMA = "tokenbill/cc-collector@1"
#: A transcript untouched for this long is final: in-flight groups are emitted as they are.
QUIESCENT_MS = 10 * 60_000
#: An mtime this far after ``now_ms`` means the clock was injected (tests, replays): final.
CLOCK_SKEW_MS = 60_000
#: Recent uuids kept per file for duplicate-line detection (SPEC §5.3).
RECENT_UUIDS = 2000
HEAD_SHA_BYTES = jsonl.HEAD_SHA_BYTES


@dataclass
class FileCursor:
    """Where the collector stopped in one transcript (SPEC §5.3)."""

    path_hmac: str
    offset: int
    head_sha: str
    size: int
    mtime_ns: int
    last_trigger_ts_ms: int | None
    recent_uuids: list[str]   # bounded, last 2,000

    def to_json(self) -> dict[str, Any]:
        """JSON form (the state file)."""
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, d: object) -> FileCursor | None:
        """Parse a cursor; None when any field has the wrong type."""
        if not isinstance(d, Mapping):
            return None
        uuids = d.get("recent_uuids")
        if uuids is not None and not isinstance(uuids, list):
            return None
        try:
            cur = cls(path_hmac=d["path_hmac"], offset=d["offset"], head_sha=d["head_sha"],
                      size=d["size"], mtime_ns=d["mtime_ns"],
                      last_trigger_ts_ms=d.get("last_trigger_ts_ms"),
                      recent_uuids=list(uuids or []))
        except (KeyError, TypeError):
            return None
        ints_ok = all(type(v) is int and v >= 0 for v in (cur.offset, cur.size, cur.mtime_ns))
        trig = cur.last_trigger_ts_ms
        if (not isinstance(cur.path_hmac, str) or not isinstance(cur.head_sha, str) or not ints_ok
                or (trig is not None and (type(trig) is not int or trig < 0))
                or not all(isinstance(u, str) for u in cur.recent_uuids)):
            return None
        cur.recent_uuids = cur.recent_uuids[-RECENT_UUIDS:]
        return cur


@dataclass
class CollectorState:
    """Every file's cursor plus its content-free parser context; a private JSON file."""

    cursors: dict[str, FileCursor] = field(default_factory=dict)
    contexts: dict[str, dict[str, Any]] = field(default_factory=dict)

    @staticmethod
    def load(path: Path) -> CollectorState:
        """Read the state file; a missing file is an empty state. An unreadable or corrupt file is
        logged and replaced by an empty state (the next run re-reads everything; the store's merge
        makes that idempotent)."""
        path = Path(path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return CollectorState()
        except OSError as exc:
            logger.warning("collector state unreadable (%s); starting fresh", type(exc).__name__)
            return CollectorState()
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            logger.warning("collector state is not valid JSON; starting fresh")
            return CollectorState()
        if not isinstance(doc, dict) or doc.get("schema") != STATE_SCHEMA:
            logger.warning("collector state has an unknown schema; starting fresh")
            return CollectorState()
        state = CollectorState()
        cursors = doc.get("cursors")
        contexts = doc.get("contexts")
        if isinstance(cursors, dict):
            for key, value in cursors.items():
                cur = FileCursor.from_json(value)
                if isinstance(key, str) and cur is not None and cur.path_hmac == key:
                    state.cursors[key] = cur
        if isinstance(contexts, dict):
            for key, value in contexts.items():
                if isinstance(key, str) and isinstance(value, dict) and key in state.cursors:
                    state.contexts[key] = value
        return state

    def save(self, path: Path) -> None:
        """Write the state atomically to an owner-only file (``open_private``; the directory is
        created ``0700``)."""
        path = Path(path)
        doc = {"schema": STATE_SCHEMA,
               "cursors": {k: v.to_json() for k, v in sorted(self.cursors.items())},
               "contexts": {k: v for k, v in sorted(self.contexts.items())}}
        text = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        tmp = path.with_name(path.name + ".tmp")
        with jsonl.open_private(tmp, "w") as f:
            f.write(text)
            warning = jsonl.acl_warning(f)
        if warning:
            logger.warning("collector state: %s", warning)
        os.replace(tmp, path)


def _head_sha(path: Path, n: int) -> str:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read(n)).hexdigest()
    except OSError as exc:
        raise SourceError(f"{path.name}: unreadable ({type(exc).__name__})") from None


def retention_check(root: Path, now_ms: int) -> DataQualityNote | None:
    """``dq.retention_warning`` over every transcript under *root* (the collector prints it)."""
    return retention_note(iter_claude_files(root), now_ms)


def collect_incremental(root: Path, state: CollectorState, opts: IngestOptions, *,
                        now_ms: int, final: bool = False) -> Iterator[IngestResult]:
    """One :class:`IngestResult` per transcript under *root* with new, closed records since the
    last run, updating *state* in place (the caller saves it after consuming the iterator).

    Unchanged files (same size and mtime) are skipped, as are files whose new bytes are still
    in flight (a trailing message group, or an unterminated last line); a file with withheld bytes
    is processed again once it is quiescent — untouched for :data:`QUIESCENT_MS` before *now_ms*,
    or with an mtime more than :data:`CLOCK_SKEW_MS` after *now_ms* (an injected clock behind the
    file system: the file is treated as final) — or when *final* is true (e.g. a last collection
    at shutdown). The first result also carries ``dq.retention_warning`` when transcripts approach
    ``cleanupPeriodDays``.
    """
    check_options(opts)
    opts = dataclasses.replace(opts, now_ms=now_ms) if opts.now_ms != now_ms else opts
    files = list(iter_claude_files(root))
    retention = retention_note(files, now_ms)
    first = True
    for path in files:
        result = _collect_file(path, state, opts, now_ms, final)
        if result is None:
            continue
        if first and retention is not None:
            result.notes.append(retention)
            result.notes.sort(key=lambda n: n.code)
        first = False
        yield result


def _collect_file(path: Path, state: CollectorState, opts: IngestOptions,
                  now_ms: int, final: bool) -> IngestResult | None:
    key = source_id_for(opts, path)
    try:
        st = os.stat(path)
    except OSError:
        return None
    size, mtime_ns = st.st_size, st.st_mtime_ns
    age_ms = now_ms - mtime_ns // 1_000_000
    quiescent = final or age_ms >= QUIESCENT_MS or age_ms < -CLOCK_SKEW_MS
    cur = state.cursors.get(key)
    context = state.contexts.get(key)
    if cur is not None:
        unchanged = cur.size == size and cur.mtime_ns == mtime_ns
        if unchanged and not (cur.offset < size and quiescent):
            return None
        rotated = size < cur.offset or _head_sha(path, min(HEAD_SHA_BYTES, cur.size)) \
            != cur.head_sha
        if rotated:
            logger.info("transcript rotated or truncated; re-reading from the start")
            cur, context = None, None
    start = cur.offset if cur is not None else 0
    recent = cur.recent_uuids if cur is not None else []
    trigger = cur.last_trigger_ts_ms if cur is not None else None
    common = {"source_id": key, "start_offset": start, "context": context,
              "recent_uuids": recent, "last_trigger": trigger, "size_limit": size}
    try:
        outcome = parse_file(opts, path, stop_at=None, finalize_open=quiescent,
                             process_unterminated=quiescent, **common)
    except ContextError:
        # a damaged saved context: forget the cursor and re-read the file from the start (the
        # store's merge makes the re-emission idempotent)
        logger.warning("collector context unusable; re-reading the transcript from the start")
        cur, context, start, recent, trigger = None, None, 0, [], None
        common = {"source_id": key, "start_offset": 0, "context": None, "recent_uuids": [],
                  "last_trigger": None, "size_limit": size}
        outcome = parse_file(opts, path, stop_at=None, finalize_open=quiescent,
                             process_unterminated=quiescent, **common)
    resume = None
    if not quiescent:
        marks = [m for m in (outcome.earliest_open, outcome.unterminated) if m is not None]
        resume = min(marks) if marks else None
    if resume is None:
        new_offset = size
    elif resume <= start:
        outcome = None
        new_offset = start
    else:
        outcome = parse_file(opts, path, stop_at=resume, finalize_open=True,
                             process_unterminated=True, **common)
        new_offset = resume
    state.cursors[key] = FileCursor(
        path_hmac=key, offset=new_offset,
        head_sha=_head_sha(path, min(HEAD_SHA_BYTES, size)), size=size, mtime_ns=mtime_ns,
        last_trigger_ts_ms=outcome.last_trigger if outcome is not None else trigger,
        recent_uuids=(outcome.recent_uuids if outcome is not None else list(recent))[
            -RECENT_UUIDS:])
    if outcome is None:
        # nothing consumed: the context stays the one at the (unchanged) offset — none at all
        # after a rotation or a discarded context, never the previous file's
        if context is not None:
            state.contexts[key] = context
        else:
            state.contexts.pop(key, None)
        return None
    state.contexts[key] = outcome.context
    info = source_info(outcome.run, key, path, outcome.sha256, outcome.n_bytes)
    return build_result(outcome.run, info)
