"""VS Code Copilot collector: content-free extracts of ``agent-traces.db`` and of the OTel
outfile (addendum §5.13, §5.18, §8.1; CP-VSCODE brief — the brief wins where §5.18 differs).

``tokenbill copilot collect --source vscode`` (wired by CP-WIRE) calls
:func:`collect_vscode_extracts` on the developer machine. It never maps spans to requests (that
is CP-OTEL's single implementation): it selects, filters and copies allowlisted rows into files
that the ``copilot-vscode-traces`` / ``copilot-otel`` adapters read at central ingest.

**agent-traces.db.** Opened read-only (URI ``mode=ro&immutable=0``, ``PRAGMA query_only``; one
short read transaction, so VS Code's writer is never blocked). Only the ``spans`` columns of
``core.facts.copilot_vscode_traces().span_columns`` are selected (never ``status_message`` or
``tool_*``), ``span_attributes`` rows only with ``key IN (<allowlist>)`` as a parameterized
list, and ``span_events`` is never queried. New spans are those strictly after the per-database
high-water mark ``(start_time_ms, span_id)``; because VS Code inserts a span when it *ends*,
spans that start up to :data:`LATE_WINDOW_MS` before the mark and were not processed yet
(``recent`` ids in the state) are picked up once as late arrivals. Each run with new spans
writes one private SQLite file ``vscode-<collector id>-<start>-<end>.db`` with the
``otelSqliteStore.ts`` DDL of ``spans`` (the allowlisted column subset) and ``span_attributes``
plus ``tokenbill_meta`` (:data:`VSCODE_META_TABLE_DDL`).

**Retention.** VS Code keeps 7 days / 100 sessions: an oldest span newer than the mark →
``dq.copilot_vscode_gap``; unread spans older than 5 days → ``dq.copilot_vscode_retention_risk``
(run the collector at least daily). A database that was recreated below the mark restarts from
its first span (see :func:`_recreated`).

**OTel outfile** (``github.copilot.chat.otel.outfile``; path given by the caller, settings are
never read): byte cursor + head-hash rotation check; complete lines only (a partial last line
waits for the next run); span lines keep the VS Code OTel-JS dialect with allowlisted attributes
only, content and identity attributes removed, and the resource marked ``tokenbill.collector =
"copilot-vscode-collect@1"`` with the collector's principal, key id and team; log and metric
lines are dropped. Output ``vscode-otel-<collector id>-<n>.jsonl`` (canonical JSON, 0600).

**Privacy.** Every file is built under a hidden temporary name in ``out_dir``, scanned (canary
bytes, ``core.secrets.find_secrets``) and only then renamed into place; any hit aborts the file
(``dq.copilot_vscode_extract_aborted``) and leaves the state unchanged. Source files are never
written.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import platform as _platform_mod
import re
import sqlite3
import sys
import types
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import tokenbill
from tokenbill.core import facts as _facts
from tokenbill.core import jsonl
from tokenbill.core.builders import CANARY, CANARY_EMAIL, CANARY_LOGIN
from tokenbill.core.errors import SourceError, UsageError
from tokenbill.core.ids import hmac_hex, pseudonym
from tokenbill.core.secrets import find_secrets
from tokenbill.core.types import DataQualityNote

__all__ = [
    "COLLECTOR",
    "DQ_ABORTED",
    "DQ_BUSY",
    "DQ_GAP",
    "DQ_NO_SPANS",
    "DQ_RETENTION_RISK",
    "DQ_SCHEMA",
    "EXTRACT_SCHEMA",
    "LATE_WINDOW_MS",
    "OPERATION_NAMES",
    "RETENTION_RISK_MS",
    "STATE_SCHEMA",
    "VSCODE_META_TABLE_DDL",
    "CollectResult",
    "CollectorIdentity",
    "DatabaseCursor",
    "OutfileCursor",
    "VsCodeCollectorState",
    "VsCodeSources",
    "collect_vscode_extracts",
    "extract_ddl",
    "optin_snippet",
    "vscode_paths",
]

logger = logging.getLogger("tokenbill.adapters.copilot_vscode_collect")

#: ``tokenbill_meta.schema`` of a database extract (CP-OTEL recognizes extracts by it).
EXTRACT_SCHEMA = "tokenbill/vscode-extract@1"
#: ``tokenbill.collector`` resource attribute of an OTel extract.
COLLECTOR = "copilot-vscode-collect@1"
STATE_SCHEMA = "tokenbill/vscode-collector@1"
#: Span names kept in clear; every other name becomes :data:`OTHER_NAME`.
OPERATION_NAMES = ("chat", "invoke_agent", "execute_tool")
OTHER_NAME = "other"
#: Spans starting this long before the mark that were not processed yet are read once (late inserts:
#: VS Code writes a span when it ends).
LATE_WINDOW_MS = 6 * 3_600_000
#: Unread spans older than this → ``dq.copilot_vscode_retention_risk`` (VS Code keeps 7 days).
RETENTION_RISK_MS = 5 * 86_400_000
#: Span ids kept in the state for late-arrival detection (newest first when capped).
MAX_RECENT_IDS = 50_000
#: A text value longer than this (UTF-8 bytes) is never copied (defense in depth, SPEC §8.1).
MAX_TEXT_BYTES = 256
#: Seconds a read waits for a transient lock (VS Code itself uses busy_timeout 3000 ms).
BUSY_TIMEOUT_S = 2
HEAD_SHA_BYTES = jsonl.HEAD_SHA_BYTES

#: The metadata table every database extract carries (brief: exactly these keys, see :func:`_meta`).
VSCODE_META_TABLE_DDL = "CREATE TABLE tokenbill_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"

DQ_SCHEMA = "dq.copilot_vscode_schema"
DQ_GAP = "dq.copilot_vscode_gap"
DQ_RETENTION_RISK = "dq.copilot_vscode_retention_risk"
DQ_ABORTED = "dq.copilot_vscode_extract_aborted"
DQ_NO_SPANS = "dq.copilot_vscode_no_spans"
DQ_BUSY = "dq.copilot_vscode_busy"

#: Column DDL of ``spans`` in ``otelSqliteStore.ts`` (SCHEMA_VERSION 1); the extract keeps the
#: allowlisted subset in the same order with the same types and constraints.
_COLUMN_DDL: Mapping[str, str] = types.MappingProxyType({
    "span_id": "TEXT PRIMARY KEY", "trace_id": "TEXT NOT NULL", "parent_span_id": "TEXT",
    "name": "TEXT NOT NULL", "start_time_ms": "INTEGER NOT NULL", "end_time_ms": "INTEGER NOT NULL",
    "status_code": "INTEGER NOT NULL DEFAULT 0", "status_message": "TEXT",
    "operation_name": "TEXT", "provider_name": "TEXT", "agent_name": "TEXT",
    "conversation_id": "TEXT", "request_model": "TEXT", "response_model": "TEXT",
    "input_tokens": "INTEGER", "output_tokens": "INTEGER", "cached_tokens": "INTEGER",
    "reasoning_tokens": "INTEGER", "tool_name": "TEXT", "tool_call_id": "TEXT", "tool_type": "TEXT",
    "chat_session_id": "TEXT", "turn_index": "INTEGER", "ttft_ms": "REAL",
})
_SPAN_ATTRIBUTES_DDL = (
    "CREATE TABLE span_attributes (\n"
    "\tspan_id TEXT NOT NULL REFERENCES spans(span_id) ON DELETE CASCADE,\n"
    "\tkey TEXT NOT NULL, value TEXT,\n"
    "\tPRIMARY KEY (span_id, key)\n)")
_INDEX_DDL = (("idx_spans_trace", "trace_id"), ("idx_spans_conversation", "conversation_id"),
              ("idx_spans_chat_session", "chat_session_id"),
              ("idx_spans_operation", "operation_name"), ("idx_spans_start_time", "start_time_ms"))
_REQUIRED_ATTRIBUTE_COLUMNS = frozenset({"span_id", "key", "value"})
#: Values that are random identifiers: find_secrets' ``high_entropy`` detector is expected to fire
#: on them, so only its specific detectors apply (see :func:`_scan_file`).
_ID_COLUMNS = frozenset({"span_id", "trace_id", "parent_span_id", "conversation_id",
                         "chat_session_id"})
_ID_ATTRIBUTES = frozenset({"gen_ai.response.id", "gen_ai.conversation.id"})
_RESPONSE_ID = "gen_ai.response.id"
_CONVERSATION_ID = "gen_ai.conversation.id"
_OPERATION = "gen_ai.operation.name"

#: Resource attributes an OTel extract keeps (plus the ``tokenbill.*`` collector attributes).
RESOURCE_KEYS = frozenset({"service.name", "service.version", "telemetry.sdk.language",
                           "telemetry.sdk.name", "telemetry.sdk.version"})
_EVENT_PREFIX = "github.copilot.session."
_IDENTITY_PREFIXES = ("enduser.", "user.", "process.user.")
_CONTENT_PREFIXES = ("github.copilot.tool.parameters.", "gen_ai.tool.call.")
_OTEL_ID_RE = re.compile(r"[0-9A-Fa-f]{1,64}\Z")
_PRINCIPAL_RE = re.compile(r"(?:r_[A-Za-z0-9._-]{1,64}|c_[0-9a-f]{20})\Z")
_KEY_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
#: Substrings every specific (non-entropy) detector of ``core.secrets`` needs; text without them
#: cannot hold such a secret, so only matching segments are passed to ``find_secrets``.
_SECRET_TRIGGER = re.compile(r"sk-|AKIA|ghp_|github_pat_|xox[abp]-|eyJ|PRIVATE KEY")
_NON_PRINTABLE = re.compile(r"[^\x20-\x7e]")
_CANARIES = tuple(s.encode("utf-8") for s in (CANARY, CANARY_LOGIN, CANARY_EMAIL))


# ---------------------------------------------------------------------------------------------
# public value types
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VsCodeSources:
    """The VS Code inputs of one run: ``agent-traces.db`` files and OTel JSON-lines outfiles."""

    traces_dbs: tuple[Path, ...] = ()
    otel_outfiles: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        for name in ("traces_dbs", "otel_outfiles"):
            value = getattr(self, name)
            if isinstance(value, (str, os.PathLike)):
                raise UsageError(f"VsCodeSources.{name} must be a sequence of paths")
            object.__setattr__(self, name, tuple(Path(p) for p in value))


@dataclass(frozen=True, slots=True)
class CollectorIdentity:
    """Who the extracts belong to (built by CP-WIRE from the CC collector's identity modes:
    ``install`` → no principal, ``two-stage`` → ``c_<20hex>``, MDM / central → ``r_<ref>``).

    *name_key* HMACs paths (``source_db``, state keys, the collector id); it is never printed."""

    principal: str | None
    principal_key_id: str | None
    team: str | None
    name_key: bytes = field(repr=False)
    name_key_id: str

    def __post_init__(self) -> None:
        p = self.principal
        if p is not None and (not isinstance(p, str) or not _PRINCIPAL_RE.match(p)):
            raise UsageError("collector principal must be r_<ref> or c_<20 hex> (or absent)")
        kid = self.principal_key_id
        if kid is not None and (not isinstance(kid, str) or not _KEY_ID_RE.match(kid)):
            raise UsageError("collector principal_key_id must be a key id")
        if isinstance(p, str) and p.startswith("c_") and kid is None:
            raise UsageError("a c_ principal needs its principal_key_id (two-stage mode)")
        team = self.team
        if team is not None and (not isinstance(team, str) or not team.strip()
                                 or len(team) > 128 or _CONTROL_RE.search(team)):
            raise UsageError("collector team must be a short printable label")
        if not isinstance(self.name_key, bytes) or not self.name_key:
            raise UsageError("collector name_key must be non-empty bytes")
        if not isinstance(self.name_key_id, str) or not _KEY_ID_RE.match(self.name_key_id):
            raise UsageError("collector name_key_id must be a key id")


@dataclass(frozen=True, slots=True)
class CollectResult:
    """What one run produced: extract files (in ``out_dir``), the conversation ids VS Code covers
    for the in-editor CLI agent (CP-WIRE passes them to CP-LOCAL as ``skip_session_ids``),
    data-quality notes and content-free counters."""

    files: tuple[Path, ...]
    covered_session_ids: frozenset[str]
    notes: tuple[DataQualityNote, ...]
    stats: Mapping[str, int]


# ---------------------------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------------------------


def _is_int(v: object) -> bool:
    return type(v) is int


@dataclass
class DatabaseCursor:
    """Per-database progress: the ``(start_time_ms, span_id)`` high-water mark, the database head
    (its oldest span: start and SHA-256 of ``start\\x1fspan_id``) and the recently processed spans
    ``[start_time_ms, span_id]`` inside :data:`LATE_WINDOW_MS` of the mark."""

    mark_start_ms: int | None = None
    mark_span_id: str | None = None
    head_start_ms: int | None = None
    head_sha: str | None = None
    recent: list[tuple[int, str]] = field(default_factory=list)

    @property
    def mark(self) -> tuple[int, str] | None:
        """The high-water mark, or None before the first extraction."""
        if self.mark_start_ms is None or self.mark_span_id is None:
            return None
        return (self.mark_start_ms, self.mark_span_id)

    def to_json(self) -> dict[str, Any]:
        """JSON form (the state file)."""
        return {"mark_start_ms": self.mark_start_ms, "mark_span_id": self.mark_span_id,
                "head_start_ms": self.head_start_ms, "head_sha": self.head_sha,
                "recent": [[s, i] for s, i in self.recent]}

    @classmethod
    def from_json(cls, d: object) -> DatabaseCursor | None:
        """Parse a cursor; None when any field has the wrong type."""
        if not isinstance(d, Mapping):
            return None
        ms, mid = d.get("mark_start_ms"), d.get("mark_span_id")
        hs, hsha = d.get("head_start_ms"), d.get("head_sha")
        recent = d.get("recent", [])
        if (ms is None) != (mid is None) or (hs is None) != (hsha is None):
            return None
        if ms is not None and (not _is_int(ms) or not isinstance(mid, str)):
            return None
        if hs is not None and (not _is_int(hs) or not isinstance(hsha, str)):
            return None
        if not isinstance(recent, list):
            return None
        pairs: list[tuple[int, str]] = []
        for item in recent:
            if (not isinstance(item, list) or len(item) != 2 or not _is_int(item[0])
                    or not isinstance(item[1], str)):
                return None
            pairs.append((item[0], item[1]))
        return cls(mark_start_ms=ms, mark_span_id=mid, head_start_ms=hs, head_sha=hsha,
                   recent=sorted(set(pairs))[-MAX_RECENT_IDS:])


@dataclass
class OutfileCursor:
    """Per-outfile progress: the byte offset of the first unread line and the SHA-256 of the first
    ``head_len`` bytes when the offset was saved (rotation / truncation detection)."""

    offset: int = 0
    head_sha: str = ""
    head_len: int = 0

    def to_json(self) -> dict[str, Any]:
        """JSON form (the state file)."""
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, d: object) -> OutfileCursor | None:
        """Parse a cursor; None when any field has the wrong type."""
        if not isinstance(d, Mapping):
            return None
        off, sha, n = d.get("offset"), d.get("head_sha"), d.get("head_len")
        if (not _is_int(off) or not _is_int(n) or not isinstance(sha, str) or off < 0 or n < 0
                or n > HEAD_SHA_BYTES):
            return None
        return cls(offset=off, head_sha=sha, head_len=n)


@dataclass
class VsCodeCollectorState:
    """Everything the collector remembers between runs; a private JSON file holding no content
    (``h_`` path ids, span ids, offsets, hashes, times)."""

    databases: dict[str, DatabaseCursor] = field(default_factory=dict)
    outfiles: dict[str, OutfileCursor] = field(default_factory=dict)
    last_run_ms: int | None = None
    otel_seq: int = 0

    @staticmethod
    def load(path: Path) -> VsCodeCollectorState:
        """Read the state file; a missing file is an empty state. An unreadable or corrupt file is
        logged and replaced by an empty state (the next run re-extracts what VS Code still holds;
        central ingest deduplicates by request)."""
        path = Path(path)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return VsCodeCollectorState()
        except OSError as exc:
            logger.warning("collector state unreadable (%s); starting fresh", type(exc).__name__)
            return VsCodeCollectorState()
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            logger.warning("collector state is not valid JSON; starting fresh")
            return VsCodeCollectorState()
        if not isinstance(doc, dict) or doc.get("schema") != STATE_SCHEMA:
            logger.warning("collector state has an unknown schema; starting fresh")
            return VsCodeCollectorState()
        state = VsCodeCollectorState()
        last, seq = doc.get("last_run_ms"), doc.get("otel_seq", 0)
        state.last_run_ms = last if _is_int(last) else None
        state.otel_seq = seq if _is_int(seq) and seq >= 0 else 0
        dbs, outs = doc.get("databases"), doc.get("outfiles")
        for key, value in (dbs.items() if isinstance(dbs, dict) else ()):
            db = DatabaseCursor.from_json(value)
            if isinstance(key, str) and db is not None:
                state.databases[key] = db
        for key, value in (outs.items() if isinstance(outs, dict) else ()):
            out = OutfileCursor.from_json(value)
            if isinstance(key, str) and out is not None:
                state.outfiles[key] = out
        return state

    def save(self, path: Path) -> None:
        """Write the state atomically to an owner-only file (``core.jsonl.open_private``; the
        directory is created ``0700``)."""
        path = Path(path)
        doc = {"schema": STATE_SCHEMA, "last_run_ms": self.last_run_ms, "otel_seq": self.otel_seq,
               "databases": {k: v.to_json() for k, v in sorted(self.databases.items())},
               "outfiles": {k: v.to_json() for k, v in sorted(self.outfiles.items())}}
        text = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        tmp = path.with_name(path.name + ".tmp")
        with jsonl.open_private(tmp, "w") as f:
            f.write(text)
            warning = jsonl.acl_warning(f)
        if warning:
            logger.warning("collector state: %s", warning)
        os.replace(tmp, path)


# ---------------------------------------------------------------------------------------------
# paths and opt-in text
# ---------------------------------------------------------------------------------------------


def _platform_key(platform: str | None) -> str:
    plat = (platform if platform is not None else sys.platform).lower()
    if plat.startswith(("win", "cygwin", "msys")) or plat == "nt":
        return "win32"
    if plat in ("darwin", "macos", "mac"):
        return "darwin"
    return "linux"


def _node_tmpdir(plat: str, env: Mapping[str, str]) -> str:
    """Node's ``os.tmpdir()`` (VS Code's fallback directory) for *plat* under *env*."""
    if plat == "win32":
        tmp = env.get("TEMP") or env.get("TMP") or (
            (env.get("SystemRoot") or env.get("windir") or "C:\\Windows") + "\\temp")
        if len(tmp) > 1 and tmp.endswith("\\") and not tmp.endswith(":\\"):
            tmp = tmp[:-1]
        return tmp
    tmp = env.get("TMPDIR") or env.get("TMP") or env.get("TEMP") or "/tmp"
    if len(tmp) > 1 and tmp.endswith("/"):
        tmp = tmp[:-1]
    return tmp


def _expand(template: str, plat: str, env: Mapping[str, str], home: str) -> str:
    if plat == "win32":
        appdata = env.get("APPDATA") or (home.rstrip("\\") + "\\AppData\\Roaming")
        return template.replace("%APPDATA%", appdata.rstrip("\\"))
    rest = template[2:] if template.startswith("~/") else template
    if plat == "linux" and rest.startswith(".config/"):
        xdg = env.get("XDG_CONFIG_HOME") or ""
        if xdg.startswith("/"):  # the XDG spec ignores relative values
            return xdg.rstrip("/") + "/" + rest[len(".config/"):]
    if template.startswith("~/"):
        return home.rstrip("/") + "/" + rest
    return template


def vscode_paths(*, platform: str | None = None, env: Mapping[str, str] | None = None,
                 home: Path | None = None) -> list[Path]:
    """Candidate ``agent-traces.db`` paths, most likely first (addendum §5.13; **VERIFY**, facts
    ``vscode_traces.paths``; ``--vscode-traces PATH`` always overrides).

    ``<user data>/User/globalStorage/github.copilot-chat/agent-traces.db`` for ``Code`` then
    ``Code - Insiders`` — macOS ``~/Library/Application Support/<product>``, Linux
    ``$XDG_CONFIG_HOME`` (absolute) or ``~/.config/<product>``, Windows
    ``%APPDATA%\\<product>`` — on Linux then the VS Code Server user data
    (``~/.vscode-server[-insiders]/data``: Remote-SSH, WSL, containers; **VERIFY**), and last VS
    Code's fallback ``<os.tmpdir()>/copilot-agent-traces.db``.
    *platform* is a ``sys.platform`` value (default: this machine), *env* the environment (default
    ``os.environ``) and *home* the home directory (default ``Path.home()``). Paths are not checked
    for existence."""
    fact = _facts.copilot_vscode_traces()
    plat = _platform_key(platform)
    environ: Mapping[str, str] = os.environ if env is None else env
    home_s = str(home) if home is not None else str(Path.home())
    found = [_expand(t, plat, environ, home_s) for t in fact.paths.get(plat, ())]
    if plat == "linux":
        tail = f"data/User/globalStorage/{fact.global_storage_dir}/{fact.db_file}"
        found += [f"{home_s.rstrip('/')}/{d}/{tail}"
                  for d in (".vscode-server", ".vscode-server-insiders")]
    sep = "\\" if plat == "win32" else "/"
    found.append(_node_tmpdir(plat, environ) + sep + fact.tmp_fallback_file)
    out: list[Path] = []
    seen: set[str] = set()
    for s in found:
        if s not in seen:
            seen.add(s)
            out.append(Path(s))
    return out


def optin_snippet() -> str:
    """The developer opt-in text (CP-POLICY's pack, ``copilot me``): the user-level setting, the
    retention note and the privacy statement."""
    fact = _facts.copilot_vscode_traces()
    return (
        "Turn on the VS Code Copilot Chat span database (a user setting: add it to your *user*\n"
        "settings.json; organization-managed settings cannot turn it on for you):\n"
        "\n"
        "  {\n"
        f'    "{fact.enable_setting}": true\n'
        "  }\n"
        "\n"
        f"VS Code then writes {fact.db_file} in the Copilot Chat extension's global storage\n"
        f"and keeps {fact.retention_days} days / {fact.retention_sessions} sessions of spans.\n"
        "Run `tokenbill copilot collect --source vscode` at least once a day, or older\n"
        "requests are pruned before they are collected.\n"
        "\n"
        "What is collected: per request token counts (input, cache read, cache write, output),\n"
        "model, timing and GitHub's credit estimate. Never collected: prompts, responses, tool\n"
        "arguments or results, file names, repository names or your user name. Others only\n"
        "see team-level results; `tokenbill copilot me` shows your own. To opt out, set the\n"
        f"setting to false and delete {fact.db_file}.\n"
    )


def extract_ddl(columns: Sequence[str] | None = None) -> tuple[str, ...]:
    """The DDL statements of a database extract: ``spans`` (the allowlisted *columns*, default facts
    ``span_columns``, with the ``otelSqliteStore.ts`` types), ``span_attributes``, the store's
    ``spans`` indexes on kept columns, and ``tokenbill_meta``."""
    cols = tuple(columns) if columns is not None else _facts.copilot_vscode_traces().span_columns
    body = ",\n".join(f"\t{c} {_COLUMN_DDL.get(c, '')}".rstrip() for c in cols)
    stmts = [f"CREATE TABLE spans (\n{body}\n)", _SPAN_ATTRIBUTES_DDL]
    stmts += [f"CREATE INDEX {name} ON spans({col})" for name, col in _INDEX_DDL if col in cols]
    stmts.append(VSCODE_META_TABLE_DDL)
    return tuple(stmts)


# ---------------------------------------------------------------------------------------------
# run context
# ---------------------------------------------------------------------------------------------


class _Abort(Exception):
    """An extract file failed the privacy scan (content-free reason)."""


class _Run:
    """Mutable per-run context: notes, counters, output directory and identity."""

    def __init__(self, identity: CollectorIdentity, out_dir: Path, now_ms: int,
                 cli_session_ids: frozenset[str]) -> None:
        self.identity = identity
        self.out_dir = out_dir
        self.now_ms = now_ms
        self.cli_ids = cli_session_ids
        self.fact = _facts.copilot_vscode_traces()
        self.allowlist = frozenset(self.fact.attribute_allowlist)
        self.content_keys = frozenset(self.fact.content_keys)
        self.identity_keys = frozenset(self.fact.identity_keys)
        self.notes: list[DataQualityNote] = []
        self.stats: Counter[str] = Counter()
        self.files: list[Path] = []
        self.covered: set[str] = set()
        self.collector_id = hmac_hex(
            identity.name_key,
            f"vscode-collector\x1f{identity.principal or ''}\x1f{_platform_mod.node()}"
            .encode("utf-8", "surrogatepass"), 12)

    def note(self, code: str, severity: str, detail: str, count: int = 1) -> None:
        self.notes.append(DataQualityNote(code=code, severity=severity, count=count,
                                          detail=detail[:256]))

    def h(self, path: Path) -> str:
        return pseudonym(self.identity.name_key, "h", str(path))


def _abs(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


def collect_vscode_extracts(sources: VsCodeSources, state: VsCodeCollectorState, *,
                            out_dir: Path, identity: CollectorIdentity, now_ms: int,
                            cli_session_ids: frozenset[str] = frozenset()) -> CollectResult:
    """Extract every new allowlisted span of *sources* into content-free files in *out_dir*,
    updating *state* in place (the caller saves it afterwards).

    *cli_session_ids* are the directory names under ``~/.copilot/session-state/`` (CP-WIRE lists
    them; no Copilot file is opened here): a VS Code conversation in that set is reported in
    ``covered_session_ids`` so CP-LOCAL emits nothing for it. Never raises for bad input files
    (they become notes); ``UsageError`` for bad arguments."""
    if not isinstance(sources, VsCodeSources):
        raise UsageError("sources must be VsCodeSources")
    if not isinstance(state, VsCodeCollectorState):
        raise UsageError("state must be VsCodeCollectorState")
    if not isinstance(identity, CollectorIdentity):
        raise UsageError("identity must be CollectorIdentity")
    if not _is_int(now_ms) or now_ms < 0:
        raise UsageError("now_ms must be an int >= 0")
    cli_ids = frozenset(s for s in cli_session_ids if isinstance(s, str) and s)
    run = _Run(identity, _abs(Path(out_dir)), now_ms, cli_ids)
    seen: set[Path] = set()
    for db in sources.traces_dbs:
        path = _abs(db)
        if path not in seen:
            seen.add(path)
            _collect_db(run, path, state)
    seen.clear()
    for outfile in sources.otel_outfiles:
        path = _abs(outfile)
        if path not in seen:
            seen.add(path)
            _collect_outfile(run, path, state)
    state.last_run_ms = now_ms
    notes = tuple(sorted(run.notes, key=lambda n: (n.code, n.detail)))
    stats = types.MappingProxyType({k: v for k, v in sorted(run.stats.items()) if v})
    return CollectResult(files=tuple(run.files), covered_session_ids=frozenset(run.covered),
                         notes=notes, stats=stats)


# ---------------------------------------------------------------------------------------------
# agent-traces.db
# ---------------------------------------------------------------------------------------------


def _ro_uri(path: Path) -> str:
    return path.as_uri() + "?mode=ro&immutable=0"


def _open_readonly(path: Path) -> sqlite3.Connection:
    """Open *path* read-only (never creates, never writes the database file; WAL-safe)."""
    conn = sqlite3.connect(_ro_uri(path), uri=True, timeout=BUSY_TIMEOUT_S, isolation_level=None)
    try:
        conn.execute("PRAGMA query_only = ON")
    except BaseException:
        conn.close()
        raise
    return conn


def _head_key_sha(start: int, span_id: str) -> str:
    return hashlib.sha256(f"{start}\x1f{span_id}".encode("utf-8", "surrogatepass")).hexdigest()


@dataclass
class _Snapshot:
    """What one read transaction saw (only allowlisted columns and attribute rows)."""

    total: int = 0
    oldest: tuple[int, str] | None = None
    mark_present: bool = False
    head_present: bool = False
    restarted: bool = False
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    attrs: dict[str, list[tuple[str, Any]]] = field(default_factory=dict)
    lo: int | None = None
    covered: set[str] = field(default_factory=set)


def _placeholders(n: int) -> str:
    return ", ".join("?" * n)


def _read_db(run: _Run, path: Path, cur: DatabaseCursor | None) -> _Snapshot:
    """Run every query of one database in one read transaction."""
    fact = run.fact
    conn = _open_readonly(path)
    try:
        conn.execute("BEGIN")
        span_cols = {r[1] for r in conn.execute("PRAGMA table_info(spans)")}
        attr_cols = {r[1] for r in conn.execute("PRAGMA table_info(span_attributes)")}
        missing = (set(fact.span_columns) - span_cols) | (_REQUIRED_ATTRIBUTE_COLUMNS - attr_cols)
        if missing:
            raise _SchemaMismatch(len(missing))
        snap = _Snapshot()
        snap.total = conn.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
        head = conn.execute("SELECT start_time_ms, span_id FROM spans "
                            "ORDER BY start_time_ms, span_id LIMIT 1").fetchone()
        if head is not None and _is_int(head[0]) and isinstance(head[1], str):
            snap.oldest = (head[0], head[1])
        mark = cur.mark if cur is not None else None
        if mark is not None:
            row = conn.execute("SELECT start_time_ms FROM spans WHERE span_id = ?",
                               (mark[1],)).fetchone()
            snap.mark_present = row is not None and row[0] == mark[0]
        if cur is not None and cur.head_start_ms is not None:
            snap.head_present = any(
                isinstance(r[0], str) and _head_key_sha(cur.head_start_ms, r[0]) == cur.head_sha
                for r in conn.execute("SELECT span_id FROM spans WHERE start_time_ms = ?",
                                      (cur.head_start_ms,)))
        snap.restarted = mark is not None and _recreated(cur, snap)  # type: ignore[arg-type]
        if mark is not None and not snap.restarted:
            snap.lo = mark[0] - LATE_WINDOW_MS
        cols = ", ".join(fact.span_columns)
        if snap.lo is None:
            traces_sql, params = "SELECT DISTINCT trace_id FROM spans", ()
        else:
            traces_sql = "SELECT DISTINCT trace_id FROM spans WHERE start_time_ms >= ?"
            params = (snap.lo,)
        snap.rows = conn.execute(
            f"SELECT {cols} FROM spans WHERE trace_id IN ({traces_sql}) "
            "ORDER BY start_time_ms, span_id", params).fetchall()
        allow = tuple(fact.attribute_allowlist)
        attr_sql = (f"SELECT span_id, key, value FROM span_attributes "
                    f"WHERE key IN ({_placeholders(len(allow))}) AND span_id IN "
                    f"(SELECT span_id FROM spans WHERE trace_id IN ({traces_sql})) "
                    "ORDER BY span_id, key")
        for sid, key, value in conn.execute(attr_sql, allow + params):
            snap.attrs.setdefault(sid, []).append((key, value))
        if run.cli_ids:
            for conv, chat in conn.execute(
                    "SELECT DISTINCT conversation_id, chat_session_id FROM spans "
                    "WHERE operation_name = ?", ("chat",)):
                snap.covered.update(v for v in (conv, chat) if isinstance(v, str))
            snap.covered &= run.cli_ids
        conn.execute("COMMIT")
        return snap
    finally:
        conn.close()


class _SchemaMismatch(Exception):
    """Required ``spans`` / ``span_attributes`` columns are missing."""


def _recreated(cur: DatabaseCursor, snap: _Snapshot) -> bool:
    """Whether the database was recreated (or replaced) below the mark: the mark span is gone, spans
    at or before the mark exist, and the head moved *backwards* (pruning only ever removes the
    oldest spans, so an older head or a vanished head at the same start is a different database).
    A recreated database that holds only spans newer than the mark needs no restart: reading after
    the mark already reads all of it."""
    mark = cur.mark
    if mark is None or snap.mark_present or snap.oldest is None or snap.oldest > mark:
        return False
    if cur.head_start_ms is None:
        return True
    oldest_start = snap.oldest[0]
    return oldest_start < cur.head_start_ms or (
        oldest_start == cur.head_start_ms and not snap.head_present)


def _text_ok(value: Any) -> bool:
    return not isinstance(value, str) or len(value.encode("utf-8", "surrogatepass")) \
        <= MAX_TEXT_BYTES


def _collect_db(run: _Run, path: Path, state: VsCodeCollectorState) -> None:
    fact = run.fact
    key = run.h(path)
    if not path.is_file():
        run.stats["databases_missing"] += 1
        run.note(DQ_NO_SPANS, "info", "agent-traces.db not found (VS Code writes it only with "
                 f"{fact.enable_setting} = true)")
        return
    cur = state.databases.get(key)
    try:
        snap = _read_db(run, path, cur)
    except _SchemaMismatch as exc:
        run.stats["databases_skipped"] += 1
        run.note(DQ_SCHEMA, "warn", f"agent-traces.db lacks {exc.args[0]} required column(s); "
                 "skipped")
        return
    except sqlite3.OperationalError as exc:
        text = str(exc).lower()
        if "locked" in text or "busy" in text:
            run.stats["databases_busy"] += 1
            run.note(DQ_BUSY, "warn", "agent-traces.db was busy; it is read again next run")
            return
        run.stats["databases_skipped"] += 1
        run.note(DQ_SCHEMA, "warn", "agent-traces.db is not a readable span database; skipped")
        return
    except sqlite3.Error:
        run.stats["databases_skipped"] += 1
        run.note(DQ_SCHEMA, "warn", "agent-traces.db is not a readable span database; skipped")
        return
    run.stats["databases_read"] += 1
    if snap.total == 0:
        run.note(DQ_NO_SPANS, "info", "agent-traces.db holds no spans")
    if snap.restarted:
        run.stats["databases_restarted"] += 1
        logger.info("agent-traces.db was recreated below the mark; restarting from its first span")
        cur = None
    mark = cur.mark if cur is not None else None
    if mark is not None and snap.oldest is not None and snap.oldest > mark:
        hours = -(-(snap.oldest[0] - mark[0]) // 3_600_000)  # ceiling: at least 1
        run.stats["gap_hours"] += hours
        run.note(DQ_GAP, "warn", f"spans were pruned by VS Code before collection: up to {hours} "
                 "hours may be missing; run the collector at least daily")
    recent_ids = {sid for _, sid in cur.recent} if cur is not None else set()
    names = run.fact.span_columns
    i_sid, i_tid = names.index("span_id"), names.index("trace_id")
    i_start = names.index("start_time_ms")
    new: list[tuple[Any, ...]] = []
    for row in snap.rows:
        sid, start = row[i_sid], row[i_start]
        if not isinstance(sid, str) or not _is_int(start) or not isinstance(row[i_tid], str):
            run.stats["bad_rows"] += 1
            continue
        if snap.lo is not None and start < snap.lo:
            continue
        if mark is None or (start, sid) > mark:
            new.append(row)
        elif sid not in recent_ids:
            run.stats["late_spans"] += 1
            new.append(row)
    kept, dropped = _dedupe_rows(run, snap, new)
    processed = kept + dropped
    if processed:
        oldest_new = min(r[i_start] for r in processed)
        if oldest_new < run.now_ms - RETENTION_RISK_MS:
            days = (run.now_ms - oldest_new) // 86_400_000
            run.note(DQ_RETENTION_RISK, "warn", f"unread spans are {days} days old; VS Code prunes "
                     f"after {fact.retention_days} days: run the collector at least daily")
    if kept:
        try:
            _write_db_extract(run, path, kept, snap.attrs, len(dropped))
        except _Abort as exc:
            run.stats["files_aborted"] += 1
            run.note(DQ_ABORTED, "error", f"database extract aborted: {exc.args[0]}; nothing "
                     "written, the spans are retried next run")
            return
    run.stats["spans_dropped_synthesized"] += len(dropped)
    run.covered |= snap.covered
    # the cursor advances only after the file (if any) was written
    new_cur = cur if cur is not None else DatabaseCursor()
    if processed:
        top = max((r[i_start], r[i_sid]) for r in processed)
        if mark is None or top > mark:
            new_cur.mark_start_ms, new_cur.mark_span_id = top
        mark_start = new_cur.mark_start_ms
        pairs = set(new_cur.recent) | {(r[i_start], r[i_sid]) for r in processed}
        floor = (mark_start or 0) - LATE_WINDOW_MS
        new_cur.recent = sorted(p for p in pairs if p[0] >= floor)[-MAX_RECENT_IDS:]
    if snap.oldest is not None:
        new_cur.head_start_ms = snap.oldest[0]
        new_cur.head_sha = _head_key_sha(*snap.oldest)
    state.databases[key] = new_cur


def _dedupe_rows(run: _Run, snap: _Snapshot, new: list[tuple[Any, ...]]
                 ) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    """Split *new* into kept rows and synthesized chat spans dropped because a native span (one with
    ``gen_ai.response.id``) of the same trace and conversation exists anywhere in the database."""
    names = run.fact.span_columns
    idx = {n: names.index(n) for n in ("span_id", "trace_id", "operation_name", "conversation_id")}

    def facts_of(row: tuple[Any, ...]) -> tuple[bool, bool, tuple[Any, Any]]:
        attrs = dict(snap.attrs.get(row[idx["span_id"]], ()))
        chat = row[idx["operation_name"]] == "chat" or attrs.get(_OPERATION) == "chat"
        native = bool(attrs.get(_RESPONSE_ID))
        conv = row[idx["conversation_id"]] or attrs.get(_CONVERSATION_ID)
        return chat, native, (row[idx["trace_id"]], conv)

    natives: set[tuple[Any, Any]] = set()
    for row in snap.rows:
        chat, native, group = facts_of(row)
        if chat and native:
            natives.add(group)
    kept: list[tuple[Any, ...]] = []
    dropped: list[tuple[Any, ...]] = []
    for row in new:
        chat, native, group = facts_of(row)
        (dropped if chat and not native and group in natives else kept).append(row)
    return kept, dropped


def _write_db_extract(run: _Run, source: Path, rows: list[tuple[Any, ...]],
                      attrs: Mapping[str, list[tuple[str, Any]]], dropped: int) -> None:
    fact = run.fact
    names = fact.span_columns
    i_sid, i_name = names.index("span_id"), names.index("name")
    i_start = names.index("start_time_ms")
    required = {i for i, n in enumerate(names) if "NOT NULL" in _COLUMN_DDL.get(n, "")
                or n == "span_id"}
    out_rows: list[tuple[Any, ...]] = []
    out_attrs: list[tuple[str, str, Any]] = []
    plain_values: set[str] = set()
    for row in rows:
        values = list(row)
        values[i_name] = row[i_name] if row[i_name] in OPERATION_NAMES else OTHER_NAME
        if not all(_text_ok(values[i]) for i in required):
            run.stats["oversize_values_dropped"] += 1
            continue
        for i, v in enumerate(values):
            if not _text_ok(v):
                run.stats["oversize_values_dropped"] += 1
                values[i] = None
            elif isinstance(v, str) and names[i] not in _ID_COLUMNS:
                plain_values.add(v)
        out_rows.append(tuple(values))
        for k, v in attrs.get(row[i_sid], ()):
            if not _text_ok(v):
                run.stats["oversize_values_dropped"] += 1
                continue
            if isinstance(v, str) and k not in _ID_ATTRIBUTES:
                plain_values.add(v)
            out_attrs.append((row[i_sid], k, v))
    if not out_rows:
        return
    start = min(r[i_start] for r in out_rows)
    end = max(r[i_start] for r in out_rows)
    meta = _meta(run, source, start, end, len(out_rows), dropped)
    plain_values.update(meta.values())
    name = f"vscode-{run.collector_id}-{start}-{end}.db"
    tmp = run.out_dir / f".{name}.tmp"
    try:
        _build_sqlite(run, tmp, names, out_rows, out_attrs, meta)
        _scan_file(tmp, plain_values)
        final = _publish(tmp, run.out_dir, name)
    except (OSError, sqlite3.Error) as exc:
        _unlink(tmp)
        raise UsageError(f"cannot write extracts to the output directory ({type(exc).__name__})"
                         ) from None
    except BaseException:
        _unlink(tmp)
        raise
    run.files.append(final)
    run.stats["files_written"] += 1
    run.stats["spans_extracted"] += len(out_rows)
    run.stats["attributes_extracted"] += len(out_attrs)


def _meta(run: _Run, source: Path, start: int, end: int, n: int, dropped: int) -> dict[str, str]:
    ident = run.identity
    meta = {"schema": EXTRACT_SCHEMA, "collector_version": tokenbill.__version__,
            "name_key_id": ident.name_key_id, "source_db": run.h(source),
            "window_start_ms": str(start), "window_end_ms": str(end), "spans": str(n),
            "dropped_synthesized": str(dropped)}
    for k, v in (("principal", ident.principal), ("principal_key_id", ident.principal_key_id),
                 ("team", ident.team)):
        if v is not None:
            meta[k] = v
    return dict(sorted(meta.items()))


def _build_sqlite(run: _Run, tmp: Path, names: Sequence[str], rows: list[tuple[Any, ...]],
                  attrs: list[tuple[str, str, Any]], meta: Mapping[str, str]) -> None:
    _unlink(tmp)
    with jsonl.open_private(tmp, "wb") as f:  # 0600 before SQLite writes a byte
        warning = jsonl.acl_warning(f)
    if warning:
        run.note(warning, "warn", "extract written without an owner-only ACL")
    conn = sqlite3.connect(str(tmp), isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("BEGIN")
        for stmt in extract_ddl(names):
            conn.execute(stmt)
        conn.executemany(f"INSERT INTO spans ({', '.join(names)}) VALUES "
                         f"({_placeholders(len(names))})", rows)
        conn.executemany("INSERT INTO span_attributes (span_id, key, value) VALUES (?, ?, ?)",
                         attrs)
        conn.executemany("INSERT INTO tokenbill_meta (key, value) VALUES (?, ?)",
                         list(meta.items()))
        conn.execute("COMMIT")
    finally:
        conn.close()


# ---------------------------------------------------------------------------------------------
# privacy scan and publishing
# ---------------------------------------------------------------------------------------------


def _scan_file(path: Path, plain_values: Iterable[str]) -> None:
    """Abort (``_Abort``) when *path* holds a canary or a secret.

    * the canary strings (``CANARY``, ``CANARY_LOGIN``, ``CANARY_EMAIL``) anywhere in the raw bytes;
    * any specific ``core.secrets.find_secrets`` detector (keys, tokens, JWTs, PEM keys) over the
      raw bytes (SQLite pages / JSON text): each printable segment holding a detector prefix is
      passed to ``find_secrets``; ``high_entropy`` hits there are ignored because span, trace,
      response and conversation ids are random by design;
    * every ``find_secrets`` detector, ``high_entropy`` included, over *plain_values* — the
      non-identifier text values written (models, names, labels, meta values)."""
    try:
        blob = path.read_bytes()
    except OSError as exc:
        raise _Abort(f"extract unreadable ({type(exc).__name__})") from None
    if any(needle in blob for needle in _CANARIES):
        raise _Abort("content canary found")
    text = blob.decode("latin-1")
    done = -1
    for m in _SECRET_TRIGGER.finditer(text):
        if m.start() < done:
            continue
        left = m.start()
        while left > 0 and not _NON_PRINTABLE.match(text, left - 1):
            left -= 1
        nxt = _NON_PRINTABLE.search(text, m.end())
        right = nxt.start() if nxt is not None else len(text)
        done = right
        if any(kind != "high_entropy" for kind, _, _ in find_secrets(text[left:right])):
            raise _Abort("secret pattern found")
    joined = "\n".join(sorted(set(plain_values)))
    if find_secrets(joined):
        raise _Abort("secret pattern found")


def _publish(tmp: Path, out_dir: Path, name: str) -> Path:
    """Rename *tmp* into *out_dir* as *name* (``-2``, ``-3``, … before the suffix when a file of
    that name exists already: an unshipped extract is never overwritten)."""
    stem, dot, suffix = name.rpartition(".")
    final = out_dir / name
    n = 1
    while final.exists():
        n += 1
        final = out_dir / f"{stem}-{n}{dot}{suffix}"
    os.replace(tmp, final)
    return final


def _unlink(path: Path) -> None:
    """Best-effort removal of a temporary file (missing or unremovable is not an error here)."""
    try:
        path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------------------------
# OTel outfile
# ---------------------------------------------------------------------------------------------


def _head_sha(path: Path, n: int) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read(n)).hexdigest()


def _line_complete(path: Path, pos: int) -> int | None:
    """Offset just after the newline ending the line whose content ends at *pos*; None when the
    line is not terminated yet (the writer is mid-line)."""
    with jsonl.open_text(path) as f:
        f.seek(pos)
        tail = f.read(4096)
    stripped = tail.lstrip(b"\r")
    if stripped.startswith(b"\n"):
        return pos + (len(tail) - len(stripped)) + 1
    return None


def _complete_lines(path: Path, start: int) -> Iterator[tuple[int, bytes]]:
    """``(next_offset, raw_line)`` for every complete line at or after *start*; the offset is where
    the next unread line begins. A partial last line is not yielded."""
    compressed = path.suffix.lower() in (".gz", ".zst")
    lines = jsonl.iter_lines(path, start_offset=0 if compressed else start)
    prev: tuple[int, bytes] | None = None
    for _, offset, raw in lines:
        if offset < start:
            continue
        if prev is not None:
            yield offset, prev[1]
        prev = (offset, raw)
    if prev is not None:
        offset, raw = prev
        if raw:
            end = _line_complete(path, offset + len(raw))
            if end is not None:
                yield end, raw


def _collect_outfile(run: _Run, path: Path, state: VsCodeCollectorState) -> None:
    key = run.h(path)
    if not path.is_file():
        run.stats["outfiles_missing"] += 1
        run.note(DQ_NO_SPANS, "info", "OTel outfile not found (empty while a managed OTLP "
                 "endpoint is set)")
        return
    try:
        size = path.stat().st_size
        cur = state.outfiles.get(key)
        start = 0
        if cur is not None:
            if size < cur.offset or _head_sha(path, cur.head_len) != cur.head_sha:
                run.stats["outfile_rotations"] += 1
                cur = None
            else:
                start = cur.offset
        records: list[dict[str, Any]] = []
        offset = start
        for next_offset, raw in _complete_lines(path, start):
            offset = next_offset
            run.stats["otel_lines"] += 1
            _take_line(run, raw, records)
        head_len = min(HEAD_SHA_BYTES, size)
        head = _head_sha(path, head_len)
    except (OSError, SourceError, ValueError):
        run.stats["outfiles_unreadable"] += 1
        run.note(DQ_SCHEMA, "warn", "OTel outfile unreadable; it is read again next run")
        return
    kept, dropped = _dedupe_records(records)
    covered = {r["attributes"].get(_CONVERSATION_ID) for r in kept
               if r["attributes"].get(_OPERATION) == "chat"}
    if kept:
        try:
            _write_otel_extract(run, kept, state)
        except _Abort as exc:
            run.stats["files_aborted"] += 1
            run.note(DQ_ABORTED, "error", f"OTel extract aborted: {exc.args[0]}; nothing "
                     "written, the lines are retried next run")
            return
    run.stats["otel_dropped_synthesized"] += dropped
    run.covered |= {c for c in covered if isinstance(c, str)} & run.cli_ids
    state.outfiles[key] = OutfileCursor(offset=offset, head_sha=head, head_len=head_len)


def _take_line(run: _Run, raw: bytes, records: list[dict[str, Any]]) -> None:
    if not raw:
        run.stats["otel_oversize_lines"] += 1
        return
    obj = jsonl.parse_json_line(raw, exact_numbers=True)
    if obj is None:
        run.stats["otel_invalid_lines"] += 1
        return
    if "traceId" not in obj or "spanId" not in obj:
        run.stats["otel_non_span_lines"] += 1  # log records and metrics are never copied
        return
    rec = _sanitize_span(run, obj)
    if rec is None:
        run.stats["otel_invalid_spans"] += 1
        return
    records.append(rec)


def _hr_time(v: Any) -> list[int] | None:
    if (isinstance(v, list) and len(v) == 2 and all(_is_int(x) and x >= 0 for x in v)):
        return [v[0], v[1]]
    return None


def _scalar(v: Any) -> bool:
    if isinstance(v, str):
        return _text_ok(v)
    return isinstance(v, (bool, int, Decimal))


def _filter_attributes(run: _Run, attrs: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(attrs, dict):
        return out
    for k, v in attrs.items():
        if k in run.allowlist and _scalar(v):
            out[k] = v
        elif k in run.content_keys or k.startswith(_CONTENT_PREFIXES):
            run.stats["content_attributes_removed"] += 1
        elif k in run.identity_keys or k.startswith(_IDENTITY_PREFIXES):
            run.stats["identity_attributes_removed"] += 1
        else:
            run.stats["attributes_removed"] += 1
    return out


def _span_context(obj: Any) -> dict[str, Any] | None:
    if not isinstance(obj, dict):
        return None
    tid, sid = obj.get("traceId"), obj.get("spanId")
    if not (isinstance(tid, str) and _OTEL_ID_RE.match(tid) and isinstance(sid, str)
            and _OTEL_ID_RE.match(sid)):
        return None
    ctx: dict[str, Any] = {"traceId": tid, "spanId": sid}
    if _is_int(obj.get("traceFlags")):
        ctx["traceFlags"] = obj["traceFlags"]
    return ctx


def _sanitize_span(run: _Run, obj: dict[str, Any]) -> dict[str, Any] | None:
    """A span line in the VS Code OTel-JS dialect (``readableSpanToJson``) reduced to allowlisted,
    content-free fields; None when its ids or times are malformed."""
    rec = _span_context(obj)
    start, end = _hr_time(obj.get("startTime")), _hr_time(obj.get("endTime"))
    if rec is None or start is None or end is None:
        return None
    parent = obj.get("parentSpanContext")
    if parent is not None:
        pctx = _span_context(parent)
        if pctx is not None:
            rec["parentSpanContext"] = pctx
    name = obj.get("name")
    rec["name"] = name if name in OPERATION_NAMES else OTHER_NAME
    rec["startTime"], rec["endTime"] = start, end
    duration = _hr_time(obj.get("duration"))
    if duration is not None:
        rec["duration"] = duration
    if _is_int(obj.get("kind")):
        rec["kind"] = obj["kind"]
    if isinstance(obj.get("ended"), bool):
        rec["ended"] = obj["ended"]
    rec["attributes"] = _filter_attributes(run, obj.get("attributes"))
    status = obj.get("status")
    rec["status"] = {"code": status["code"]} if isinstance(status, dict) and _is_int(
        status.get("code")) else {"code": 0}
    events = []
    for ev in obj.get("events") or ():
        if not isinstance(ev, dict):
            continue
        ev_name, ev_time = ev.get("name"), _hr_time(ev.get("time"))
        if isinstance(ev_name, str) and ev_name.startswith(_EVENT_PREFIX) and _text_ok(ev_name) \
                and ev_time is not None:
            events.append({"name": ev_name, "time": ev_time,
                           "attributes": _filter_attributes(run, ev.get("attributes"))})
        else:
            run.stats["events_removed"] += 1
    rec["events"] = events
    res_in = obj.get("resource")
    res_attrs = res_in.get("attributes") if isinstance(res_in, dict) else None
    resource: dict[str, Any] = {}
    if isinstance(res_attrs, dict):
        for k, v in res_attrs.items():
            if k in RESOURCE_KEYS and isinstance(v, str) and _text_ok(v):
                resource[k] = v
            else:
                run.stats["resource_attributes_removed"] += 1
    ident = run.identity
    resource["tokenbill.collector"] = COLLECTOR
    for k, v in (("tokenbill.principal", ident.principal),
                 ("tokenbill.principal_key_id", ident.principal_key_id),
                 ("tokenbill.team", ident.team)):
        if v is not None:
            resource[k] = v
    rec["resource"] = {"attributes": resource}
    scope = obj.get("instrumentationScope")
    if isinstance(scope, dict):
        kept_scope = {k: scope[k] for k in ("name", "version")
                      if isinstance(scope.get(k), str) and _text_ok(scope[k])}
        if kept_scope:
            rec["instrumentationScope"] = kept_scope
    return rec


def _dedupe_records(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Drop synthesized chat spans (no ``gen_ai.response.id``) of a trace and conversation that has
    a native chat span among *records*."""
    def group(r: dict[str, Any]) -> tuple[Any, Any]:
        return (r["traceId"], r["attributes"].get(_CONVERSATION_ID))

    def chat(r: dict[str, Any]) -> bool:
        return r["attributes"].get(_OPERATION) == "chat" or r["name"] == "chat"

    natives = {group(r) for r in records if chat(r) and r["attributes"].get(_RESPONSE_ID)}
    kept = [r for r in records if not (chat(r) and not r["attributes"].get(_RESPONSE_ID)
                                       and group(r) in natives)]
    return kept, len(records) - len(kept)


def _canonical(value: Any) -> str:
    """Canonical JSON (sorted keys, no spaces, UTF-8 text); ``Decimal`` numbers are exact."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, Decimal)):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(_canonical(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _canonical(value[k])
                              for k in sorted(value)) + "}"
    raise TypeError(f"unsupported value type {type(value).__name__}")


def _plain_strings(rec: Any, key: str = "") -> Iterator[str]:
    """Every string of *rec* except random identifiers (span / trace ids, response and
    conversation ids)."""
    if isinstance(rec, dict):
        for k, v in rec.items():
            if k in ("traceId", "spanId") or k in _ID_ATTRIBUTES:
                continue
            yield from _plain_strings(v, k)
    elif isinstance(rec, list):
        for v in rec:
            yield from _plain_strings(v, key)
    elif isinstance(rec, str):
        yield rec


def _write_otel_extract(run: _Run, records: list[dict[str, Any]],
                        state: VsCodeCollectorState) -> None:
    seq = state.otel_seq + 1
    name = f"vscode-otel-{run.collector_id}-{seq}.jsonl"
    tmp = run.out_dir / f".{name}.tmp"
    try:
        with jsonl.open_private(tmp, "w") as f:
            for rec in records:
                f.write(_canonical(rec) + "\n")
            warning = jsonl.acl_warning(f)
        if warning:
            run.note(warning, "warn", "extract written without an owner-only ACL")
        _scan_file(tmp, (s for rec in records for s in _plain_strings(rec)))
        final = _publish(tmp, run.out_dir, name)
    except OSError as exc:
        _unlink(tmp)
        raise UsageError(f"cannot write extracts to the output directory ({type(exc).__name__})"
                         ) from None
    except BaseException:
        _unlink(tmp)
        raise
    state.otel_seq = seq
    run.files.append(final)
    run.stats["files_written"] += 1
    run.stats["otel_spans_extracted"] += len(records)

