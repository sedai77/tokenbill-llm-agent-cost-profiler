"""Admin handoff kit: the ``tokenbill/copilot-export@1`` bundle (addendum §22, brief CP-HANDOFF).

A GitHub admin (enterprise owner, org owner or billing manager) turns the files they downloaded —
API recordings of ``copilot pull`` or CSV / NDJSON files clicked out of the GitHub UI — into **one
content-free, pseudonymized file** that the product owner analyses without admin rights and without
network. Every input is read through ``core.registry`` with the admin's **export key** as both the
principal key and the name key (``IngestOptions(identity_mode="central-ingest", …)``), so logins
become ``p_`` and repository / workflow names ``h_`` values the analyst can never reverse.

Pipeline (:func:`export_from_files`): identity terms harvested from the raw inputs (memory only,
:func:`harvest_leak_terms`) → rows of excluded logins dropped before any adapter sees them
(erasure, private temporary copies) → every file sniffed and read → :func:`write_bundle`: records
deduplicated and windowed, **team labels below k merged into** ``"(other)"`` (distinct ``p_``
people over licenses ∪ activity ∪ cost lines), optionally **reduced to counts**
(``aggregate_only``), plan evidence from ``core.pool.detect_plans``, canonical JSONL members, the
**leak gate** (:func:`leak_scan`) over every member, and only then a deterministic zip written to a
private temporary file and renamed.

The reader (:func:`read_bundle`, used by the ``copilot-export`` adapter and
:func:`inspect_bundle`) enforces zip-bomb and path-traversal limits, the exact member names, the
manifest schema and counts, and decodes every line with ``core.records.from_json`` (so no raw login
can pass a ``p_`` field).

Nothing here names a person: logins, user ids, e-mail addresses, repository names and workflow paths
never leave the admin's machine; errors name a file, a member, a line number or a category — never a
value.
"""

from __future__ import annotations

import contextlib
import csv
import dataclasses
import datetime as _dt
import gzip
import hashlib
import io
import json
import os
import re
import secrets as _secrets
import shutil
import stat
import tempfile
import types
import zipfile
import zlib
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from importlib import resources
from pathlib import Path
from typing import Any

from tokenbill.core import records as _rec
from tokenbill.core.builders import CANARY, CANARY_EMAIL, CANARY_LOGIN
from tokenbill.core.errors import PrivacyError, SourceError, UsageError
from tokenbill.core.ids import key_id, natural_id, pseudonym, stable_id
from tokenbill.core.jsonl import open_private
from tokenbill.core.money import EXACT_CTX
from tokenbill.core.records import (
    ActivityDay,
    ConfigSnapshot,
    CostLine,
    LicenseSnapshot,
    OutcomeAggregate,
    UsageAggregate,
    from_json,
    record_key,
    to_json,
)
from tokenbill.core.secrets import find_secrets
from tokenbill.core.textsafe import sanitize
from tokenbill.core.types import (
    EXPERIMENTAL_FLAGS,
    DataQualityNote,
    IngestOptions,
    IngestResult,
    SourceInfo,
)

__all__ = [
    "AGGREGATE_ONLY_KINDS_OFF",
    "DEFAULT_EXPORT_KEY_FILE",
    "EXPORT_SCHEMA",
    "MANIFEST_KEYS",
    "MANIFEST_MEMBER",
    "OTHER_TEAM",
    "PRIVACY_MODES",
    "RECORD_KINDS",
    "RECORD_MEMBERS",
    "BundleManifest",
    "ExportReport",
    "LeakScanSummary",
    "LeakTerms",
    "PlanEvidenceSummary",
    "SourceSummary",
    "admin_guide_text",
    "export_from_files",
    "harvest_leak_terms",
    "inspect_bundle",
    "leak_scan",
    "pseudonym_of",
    "read_bundle",
    "read_team_map_csv",
    "render_inspect",
    "write_bundle",
]

# ---------------------------------------------------------------------------------------------
# format constants
# ---------------------------------------------------------------------------------------------

#: The bundle schema (manifest ``schema``).
EXPORT_SCHEMA = "tokenbill/copilot-export@1"
#: The first member of every bundle (``sniff`` finds its name at offset 30 of the file).
MANIFEST_MEMBER = "manifest.json"
#: Record kinds in member order.
RECORD_KINDS = ("aggregates", "config", "cost_lines", "activity", "licenses", "outcomes")
#: Record members in their fixed order (after ``manifest.json``).
RECORD_MEMBERS = tuple(f"records/{kind}.jsonl" for kind in RECORD_KINDS)
_MEMBER_ORDER = (MANIFEST_MEMBER, *RECORD_MEMBERS)
_KIND_OF_MEMBER = dict(zip(RECORD_MEMBERS, RECORD_KINDS, strict=True))
#: The exact keys of ``manifest.json``.
MANIFEST_KEYS = ("schema", "tool_version", "created_ms", "window", "entities", "orgs", "sources",
                 "counts", "dq", "principal_key_id", "name_key_id", "key_rotated", "rows_excluded",
                 "k", "teams_merged", "privacy_mode", "plan_evidence", "experimental", "leak_scan")
#: ``privacy_mode`` values: per-person ``p_`` records, or counts only (``--aggregate-only``).
PRIVACY_MODES = ("pseudonymous", "aggregate_only")
#: The literal team label of merged small teams in records (published tables use
#: ``core.kanon.other_label``).
OTHER_TEAM = "(other)"
#: The default export key file (created 0600 by ``core.keys.load_or_create``; CP-WIRE passes it).
DEFAULT_EXPORT_KEY_FILE = Path("~/.config/tokenbill/copilot-export.key")
#: Detector kinds that cannot run on an aggregate-only bundle (listed in the manifest dq codes).
AGGREGATE_ONLY_KINDS_OFF = ("plan-mix", "completions-only-seat", "duplicate-seat", "mcp-sprawl",
                            "context-heavy-cli")
#: Inputs that are never re-exported: a bundle (no re-export) and raw usage-record bodies.
_REFUSED_ADAPTERS = frozenset({"copilot-export", "github-usage-records"})

# reader limits (brief Build 5)
_MAX_MEMBERS = 16
_MAX_MEMBER_BYTES = 512 * 2**20
_MAX_RATIO = 200
_MAX_TOTAL_BYTES = 2 * 2**30
_MAX_MANIFEST_BYTES = 2**20

# data-quality codes of the export itself
DQ_SUPPRESSED = "dq.copilot_export_suppressed"
DQ_UNRECOGNIZED = "dq.copilot_export_unrecognized_input"
DQ_OUTSIDE_WINDOW = "dq.copilot_export_outside_window"
DQ_LANES_DROPPED = "dq.copilot_export_lane_records_dropped"
DQ_AGGREGATE_ONLY = "dq.copilot_export_aggregate_only"
DQ_KIND_OFF = "dq.copilot_export_kind_off."
DQ_TEAMS_MERGED = "dq.copilot_export_teams_merged"
_DQ_DETAILS = {
    DQ_SUPPRESSED: "aggregate-only rows below k dropped (count = people)",
    DQ_UNRECOGNIZED: "input files no adapter recognized (skipped)",
    DQ_OUTSIDE_WINDOW: "records outside the export window dropped",
    DQ_LANES_DROPPED: "per-request records are not part of a handoff bundle (dropped)",
    DQ_AGGREGATE_ONLY: "aggregate-only bundle: seat and activity kinds use counts",
    DQ_TEAMS_MERGED: "team labels below k merged into (other)",
}

_DAY_MS = 86_400_000
_EPOCH = _dt.date(1970, 1, 1)
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_KEY_ID_RE = re.compile(r"k_[0-9a-f]{12}\Z")
_P_RE = re.compile(r"p_[0-9a-f]{20}\Z")
_H_RE = re.compile(r"h_[0-9a-f]{20}\Z")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _day_ms(date_utc: str) -> int:
    return (_dt.date.fromisoformat(date_utc) - _EPOCH).days * _DAY_MS


def _date_of_ms(ms: int) -> str:
    return (_EPOCH + _dt.timedelta(milliseconds=ms)).isoformat()


def _canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _line(rec: object) -> str:
    return json.dumps(to_json(rec), sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------------------------
# manifest and report types
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceSummary:
    """One manifest ``sources`` row: an adapter, how many input files it read, how many records it
    produced and how many rows it quarantined (never a file name)."""

    adapter: str
    files: int
    records: int
    quarantined: int


@dataclass(frozen=True, slots=True)
class PlanEvidenceSummary:
    """One manifest ``plan_evidence`` row (``core.pool.detect_plans`` on the admin side)."""

    entity_id: str
    month: str
    plan: str
    source: str
    conflict: bool


@dataclass(frozen=True, slots=True)
class LeakScanSummary:
    """The manifest ``leak_scan``: how many harvested identity terms were checked; ``result`` is
    always ``"clean"`` (a bundle with a hit is never written)."""

    terms: int
    result: str = "clean"


@dataclass(frozen=True, slots=True)
class BundleManifest:
    """``manifest.json`` of a ``tokenbill/copilot-export@1`` bundle (keys: :data:`MANIFEST_KEYS`).

    ``window`` is ``(since, until)`` (inclusive UTC dates or None); ``counts`` and ``dq`` are sorted
    ``(name, count)`` pairs. Never holds a login, user id, e-mail, file name, path, token or URL.
    """

    schema: str
    tool_version: str
    created_ms: int
    window: tuple[str | None, str | None]
    entities: tuple[str, ...]
    orgs: tuple[str, ...]
    sources: tuple[SourceSummary, ...]
    counts: tuple[tuple[str, int], ...]
    dq: tuple[tuple[str, int], ...]
    principal_key_id: str
    name_key_id: str
    key_rotated: bool
    rows_excluded: int
    k: int
    teams_merged: int
    privacy_mode: str
    plan_evidence: tuple[PlanEvidenceSummary, ...]
    experimental: tuple[str, ...]
    leak_scan: LeakScanSummary

    def count(self, kind: str) -> int:
        """The manifest record count of *kind* (0 when absent)."""
        return dict(self.counts).get(kind, 0)

    def to_json(self) -> dict[str, Any]:
        """The manifest as the canonical JSON object written to ``manifest.json``."""
        return {
            "schema": self.schema,
            "tool_version": self.tool_version,
            "created_ms": self.created_ms,
            "window": {"since": self.window[0], "until": self.window[1]},
            "entities": list(self.entities),
            "orgs": list(self.orgs),
            "sources": [{"adapter": s.adapter, "files": s.files, "records": s.records,
                         "quarantined": s.quarantined} for s in self.sources],
            "counts": dict(self.counts),
            "dq": [{"code": c, "count": n} for c, n in self.dq],
            "principal_key_id": self.principal_key_id,
            "name_key_id": self.name_key_id,
            "key_rotated": self.key_rotated,
            "rows_excluded": self.rows_excluded,
            "k": self.k,
            "teams_merged": self.teams_merged,
            "privacy_mode": self.privacy_mode,
            "plan_evidence": [{"entity_id": p.entity_id, "month": p.month, "plan": p.plan,
                               "source": p.source, "conflict": p.conflict}
                              for p in self.plan_evidence],
            "experimental": list(self.experimental),
            "leak_scan": {"terms": self.leak_scan.terms, "result": self.leak_scan.result},
        }

    @classmethod
    def from_json(cls, d: object) -> BundleManifest:
        """Strictly decode a manifest object (exact key set, types, schema); ``SourceError``
        naming the offending key otherwise."""
        return _decode_manifest(d)


@dataclass(frozen=True, slots=True)
class ExportReport:
    """What :func:`export_from_files` wrote: the bundle path, its manifest and the aggregated,
    content-free data-quality notes."""

    out_path: Path
    manifest: BundleManifest
    dq: tuple[DataQualityNote, ...]


def _bad_manifest(key: str) -> SourceError:
    return SourceError(f"bundle manifest: {key} missing or invalid")


def _int_field(d: Mapping[str, Any], key: str, *, minimum: int = 0) -> int:
    v = d.get(key)
    if type(v) is not int or v < minimum:
        raise _bad_manifest(key)
    return v


def _str_field(d: Mapping[str, Any], key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str) or not v:
        raise _bad_manifest(key)
    return v


def _str_list(d: Mapping[str, Any], key: str) -> tuple[str, ...]:
    v = d.get(key)
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise _bad_manifest(key)
    return tuple(v)


def _rows(d: Mapping[str, Any], key: str, fields: Mapping[str, type]) -> list[dict[str, Any]]:
    v = d.get(key)
    if not isinstance(v, list):
        raise _bad_manifest(key)
    out = []
    for row in v:
        if not isinstance(row, dict) or set(row) != set(fields):
            raise _bad_manifest(key)
        for name, typ in fields.items():
            value = row[name]
            ok = type(value) is bool if typ is bool else (
                type(value) is int and value >= 0 if typ is int else isinstance(value, typ))
            if not ok:
                raise _bad_manifest(key)
        out.append(row)
    return out


def _decode_manifest(d: object) -> BundleManifest:
    if not isinstance(d, dict) or set(d) != set(MANIFEST_KEYS):
        raise SourceError("bundle manifest: keys differ from tokenbill/copilot-export@1")
    if d["schema"] != EXPORT_SCHEMA:
        raise SourceError("bundle manifest: schema is not tokenbill/copilot-export@1")
    window = d["window"]
    if not isinstance(window, dict) or set(window) != {"since", "until"} or not all(
            v is None or (isinstance(v, str) and _DATE_RE.match(v)) for v in window.values()):
        raise _bad_manifest("window")
    counts = d["counts"]
    if (not isinstance(counts, dict) or set(counts) != set(RECORD_KINDS)
            or not all(type(v) is int and v >= 0 for v in counts.values())):
        raise _bad_manifest("counts")
    for key in ("principal_key_id", "name_key_id"):
        if not isinstance(d[key], str) or not _KEY_ID_RE.match(d[key]):
            raise _bad_manifest(key)
    if type(d["key_rotated"]) is not bool:
        raise _bad_manifest("key_rotated")
    if d["privacy_mode"] not in PRIVACY_MODES:
        raise _bad_manifest("privacy_mode")
    leak = d["leak_scan"]
    if (not isinstance(leak, dict) or set(leak) != {"terms", "result"}
            or type(leak["terms"]) is not int or leak["terms"] < 0 or leak["result"] != "clean"):
        raise _bad_manifest("leak_scan")
    sources = _rows(d, "sources", {"adapter": str, "files": int, "records": int,
                                   "quarantined": int})
    dq = _rows(d, "dq", {"code": str, "count": int})
    plans = _rows(d, "plan_evidence", {"entity_id": str, "month": str, "plan": str,
                                       "source": str, "conflict": bool})
    return BundleManifest(
        schema=EXPORT_SCHEMA,
        tool_version=_str_field(d, "tool_version"),
        created_ms=_int_field(d, "created_ms"),
        window=(window["since"], window["until"]),
        entities=_str_list(d, "entities"),
        orgs=_str_list(d, "orgs"),
        sources=tuple(SourceSummary(**row) for row in sources),
        counts=tuple((kind, counts[kind]) for kind in RECORD_KINDS),
        dq=tuple((row["code"], row["count"]) for row in dq),
        principal_key_id=d["principal_key_id"],
        name_key_id=d["name_key_id"],
        key_rotated=d["key_rotated"],
        rows_excluded=_int_field(d, "rows_excluded"),
        k=_int_field(d, "k", minimum=1),
        teams_merged=_int_field(d, "teams_merged"),
        privacy_mode=d["privacy_mode"],
        plan_evidence=tuple(PlanEvidenceSummary(**row) for row in plans),
        experimental=_str_list(d, "experimental"),
        leak_scan=LeakScanSummary(terms=leak["terms"], result="clean"),
    )


# ---------------------------------------------------------------------------------------------
# identity terms (memory only) and the leak gate
# ---------------------------------------------------------------------------------------------

#: Category → wording in error messages (never the value).
_CATEGORY_TEXT = {
    "login": "login",
    "email": "e-mail address",
    "user_id": "numeric user id",
    "workflow_path": "workflow path",
    "repository": "repository name",
    "repository_short": "repository name",
    "merged_team": "merged team label",
}
#: Priority when one term falls in several categories (the stricter matching wins).
_CATEGORY_RANK = {c: i for i, c in enumerate(_CATEGORY_TEXT)}
#: Categories matched as substrings (terms of at least 6 characters) as well as whole values.
_CONTAINMENT = frozenset({"login", "email", "workflow_path", "repository"})
#: Organizational label fields: team / cost-center / org names and entity ids.
_LABEL_FIELDS = frozenset({"team", "cost_center", "org", "organization", "workspace_id",
                           "entity_id", "entities", "orgs", "target", "attrs#key"})
#: Categories never matched inside organizational label fields (a legitimate team or org named
#: like a repository must not stop the export); merged team labels only outside ``team``.
_EXEMPT_FIELDS = {
    "workflow_path": _LABEL_FIELDS,
    "repository": _LABEL_FIELDS,
    "repository_short": _LABEL_FIELDS,
    "merged_team": _LABEL_FIELDS - {"team"},
}
_MIN_TERM = 3
_MIN_CONTAINED = 6
#: Machine values carrying no identity: ids / pseudonyms / key ids, dates, months.
_MACHINE_RE = re.compile(r"(?:[a-z]{1,4}_[0-9a-f]{12,64}|\d{4}-\d{2}(?:-\d{2})?)\Z")
_DECIMAL_RE = re.compile(r"-?\d+(?:\.\d+)?\Z")
#: Harvested JSON keys whose ``login`` belongs to an organization, not a person.
_ORG_PARENTS = frozenset({"organization", "org", "enterprise", "owner", "business", "account"})


def _vocabulary() -> frozenset[str]:
    words: set[str] = set()
    for name in ("BILLING_PATHS", "BILLING_CLASSES", "COPILOT_CHANNELS", "GITHUB_COST_TYPES",
                 "COPILOT_WORKLOADS", "COPILOT_PSEUDO", "COPILOT_AGG_SOURCE_KINDS",
                 "COPILOT_AGG_DIMS", "OUTCOME_EXTRA_KEYS", "LICENSE_PLANS", "LICENSE_BUCKETS",
                 "LICENSE_SOURCE_KINDS", "EDITOR_FAMILIES", "ACTIVITY_KEYS", "ACTIVITY_FLAGS",
                 "CONFIG_KINDS", "CONFIG_SOURCE_KINDS", "PLAN_SOURCES"):
        words.update(getattr(_rec, name))
    for keys in _rec.CONFIG_KEYS.values():
        words.update(keys)
    words.update(RECORD_KINDS)
    words.update(PRIVACY_MODES)
    words.update(("USD", "provisional", "final", "direct", "auto", "unknown", "standard", "fast",
                  OTHER_TEAM, "*", "invoice", "list", "contract", "provider_estimate",
                  "list_equivalent", "none", "mixed", "metered", "volume", "azure", "block",
                  "continue", "enabled", "disabled", "assign_all", "assign_selected",
                  "unconfigured", "data_residency", "fedramp", "clean", EXPORT_SCHEMA,
                  "ai-credits", "minutes", "seat-months", "requests"))
    return frozenset(w.lower() for w in words)


_VOCABULARY = _vocabulary()


class LeakTerms(frozenset):  # type: ignore[type-arg]
    """A ``frozenset[str]`` of lower-cased identity values harvested from the raw inputs, each
    with a category (``login``, ``email``, ``user_id``, ``repository``, ``repository_short``,
    ``workflow_path``, ``merged_team``). Held in memory only; ``repr`` shows the count, never a
    term. A plain ``frozenset`` passed to :func:`leak_scan` is treated as logins."""

    _categories: Mapping[str, str]

    def __new__(cls, terms: Mapping[str, str] | Iterable[str] = ()) -> LeakTerms:
        cats: dict[str, str] = {}
        items = terms.items() if isinstance(terms, Mapping) else ((t, "login") for t in terms)
        for term, category in items:
            _add_term(cats, term, category)
        obj = super().__new__(cls, cats)
        obj._categories = types.MappingProxyType(cats)
        return obj

    def category(self, term: str) -> str:
        """The category of *term* (``login`` for unknown terms)."""
        return self._categories.get(term, "login")

    def merged(self, other: Mapping[str, str] | Iterable[str]) -> LeakTerms:
        """A new set with the terms of *other* added (stricter category wins)."""
        cats = dict(self._categories)
        extra = other._categories if isinstance(other, LeakTerms) else other
        items = extra.items() if isinstance(extra, Mapping) else ((t, "login") for t in extra)
        for term, category in items:
            _add_term(cats, term, category)
        return LeakTerms(cats)

    def __repr__(self) -> str:
        return f"LeakTerms(<{len(self)} terms>)"

    __str__ = __repr__


def _add_term(cats: dict[str, str], value: object, category: str) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, int):
        value = str(value)
        category = "user_id"
    if not isinstance(value, str):
        return
    term = value.strip().lower()
    if len(term) < _MIN_TERM:
        return
    if category == "login" and term.isdigit():
        category = "user_id"
    cur = cats.get(term)
    if cur is None or _CATEGORY_RANK.get(category, 99) < _CATEGORY_RANK.get(cur, 99):
        cats[term] = category


def _add_repository(cats: dict[str, str], value: object) -> None:
    if not isinstance(value, str):
        return
    text = value.strip()
    if "/" in text:
        _add_term(cats, text, "repository")
        short = text.rsplit("/", 1)[1]
        if len(short) >= 4:
            _add_term(cats, short, "repository_short")
    else:
        _add_term(cats, text, "repository_short")


_CSV_IDENTITY = {"username": "login", "login": "login", "user_login": "login", "email": "email",
                 "repository": "repository", "workflow_path": "workflow_path"}


def _read_input_bytes(path: Path) -> bytes | None:
    """The (decompressed) bytes of one raw input, or None for binary containers (zip)."""
    try:
        raw = path.read_bytes()
    except OSError:
        raise SourceError(f"{path.name}: unreadable") from None
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except (OSError, EOFError, zlib.error):
            return None
    if raw[:4] == b"PK\x03\x04":
        return None
    return raw


def _harvest_csv(text: str, cats: dict[str, str]) -> None:
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader, None)
        if not header:
            return
        cols = [(i, _CSV_IDENTITY[h.strip().lower()]) for i, h in enumerate(header)
                if h.strip().lower() in _CSV_IDENTITY]
        if not cols:
            return
        for row in reader:
            for i, category in cols:
                if i < len(row) and row[i].strip():
                    if category == "repository":
                        _add_repository(cats, row[i])
                    else:
                        _add_term(cats, row[i], category)
    except csv.Error:
        return


def _harvest_json(obj: object, cats: dict[str, str]) -> None:
    stack: list[tuple[object, str, object]] = [(obj, "", None)]
    while stack:
        value, key, parent = stack.pop()
        if isinstance(value, dict):
            scope = value.get("budget_scope")
            for k, v in value.items():
                if not isinstance(k, str):
                    continue
                _harvest_key(cats, k, v, key, scope)
                stack.append((v, k, value))
        elif isinstance(value, list):
            for item in value:
                stack.append((item, key, parent))


def _harvest_key(cats: dict[str, str], k: str, v: object, parent_key: str,
                 budget_scope: object) -> None:
    if parent_key == "assignee" and k in ("login", "name"):
        _add_term(cats, v, "login")
    elif parent_key in ("assignee", "user") and k == "id":
        _add_term(cats, v if isinstance(v, int) else str(v), "user_id")
    elif parent_key == "repository" and k in ("name", "full_name"):
        _add_repository(cats, v)
    elif k in ("user_login", "user") and isinstance(v, str):
        _add_term(cats, v, "login")
    elif k == "login" and parent_key not in _ORG_PARENTS:
        _add_term(cats, v, "login")
    elif k == "user_id":
        _add_term(cats, v if isinstance(v, int) else str(v), "user_id")
    elif k == "email":
        _add_term(cats, v, "email")
    elif k == "repositoryName":
        _add_repository(cats, v)
    elif k == "workflow_path":
        _add_term(cats, v, "workflow_path")
    elif k == "budget_entity_name" and budget_scope == "user":
        _add_term(cats, v, "login")
    elif k == "budget_entity_name" and budget_scope == "repository":
        _add_repository(cats, v)
    elif k in ("alert_recipients", "selected_usernames", "users") and isinstance(v, list):
        for item in v:
            _add_term(cats, item, "login")
    elif k == "resources" and isinstance(v, list):
        for item in v:
            if isinstance(item, dict):
                kind = str(item.get("type", "")).lower()
                if kind == "user":
                    _add_term(cats, item.get("name"), "login")
                elif kind in ("repo", "repository"):
                    _add_repository(cats, item.get("name"))


def _json_documents(text: str) -> Iterator[object]:
    """Every JSON value of *text*: one document, else one per line (NDJSON / JSONL)."""
    try:
        yield json.loads(text, parse_float=str, parse_constant=str)
        return
    except (ValueError, RecursionError):
        pass
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line, parse_float=str, parse_constant=str)
        except (ValueError, RecursionError):
            continue


def harvest_leak_terms(paths: Iterable[Path], *,
                       team_map_logins: Iterable[str] = ()) -> frozenset[str]:
    """The identity values of the raw inputs, lower-cased, in memory only (a :class:`LeakTerms`).

    Reads each file itself with stdlib ``csv`` / ``json`` (independently of the adapters): CSV
    columns ``username``, ``login``, ``user_login``, ``repository``, ``workflow_path``, ``email``;
    JSON ``assignee.login`` / ``assignee.id`` / ``assignee.name``, ``user_login``, ``user_id``,
    ``login`` (not under an organization / owner object), ``email``, ``user.id``,
    ``repository.name`` / ``full_name``, ``repositoryName``, ``workflow_path``, user-scope budget
    names, budget alert recipients and cost-center user / repository resources; plus
    *team_map_logins*. Directories are read recursively; zip containers are skipped. Values shorter
    than 3 characters are ignored."""
    cats: dict[str, str] = {}
    for path in _expand_inputs(list(paths), allow_empty=True):
        data = _read_input_bytes(path)
        if data is None:
            continue
        text = data.decode("utf-8-sig", errors="replace")
        head = text.lstrip()[:1]
        if head in ("{", "["):
            for doc in _json_documents(text):
                _harvest_json(doc, cats)
        else:
            _harvest_csv(text, cats)
    for login in team_map_logins:
        _add_term(cats, login, "login")
    return LeakTerms(cats)


@dataclass
class _Prepared:
    """Terms of one scan, split by matching mode."""

    exact: dict[str, str]
    contained: list[tuple[str, str]]
    ids: list[tuple[re.Pattern[str], str]]


def _prepare(terms: frozenset[str]) -> _Prepared:
    category = terms.category if isinstance(terms, LeakTerms) else (lambda _t: "login")
    exact: dict[str, str] = {}
    contained: list[tuple[str, str]] = []
    ids: list[tuple[re.Pattern[str], str]] = []
    for term in sorted(terms):
        if not isinstance(term, str) or len(term) < _MIN_TERM:
            continue
        cat = category(term)
        exact[term] = cat
        if len(term) >= _MIN_CONTAINED:
            if cat == "user_id":
                ids.append((re.compile(r"(?<![\d.])" + re.escape(term) + r"(?![\d.])"), cat))
            elif cat in _CONTAINMENT:
                contained.append((term, cat))
    return _Prepared(exact, contained, ids)


def _allowed(cat: str, fields: set[str]) -> bool:
    exempt = _EXEMPT_FIELDS.get(cat)
    return exempt is None or any(f not in exempt for f in fields)


def _hit_label(cat: str, fields: set[str]) -> str:
    text = _CATEGORY_TEXT.get(cat, "identity value")
    if "team" in fields and cat != "merged_team":
        return f"{text} in a team label"
    if fields & (_LABEL_FIELDS - {"team"}) and cat not in _EXEMPT_FIELDS:
        return f"{text} in an organization label"
    return text


def _value_hits(value: str, fields: set[str], prepared: _Prepared) -> set[str]:
    hits: set[str] = set()
    low = value.lower()
    if _MACHINE_RE.match(low) or low in _VOCABULARY or "quantity" in fields:
        return hits
    decimal = _DECIMAL_RE.match(low) is not None
    cat = prepared.exact.get(low.strip())
    if cat is not None and (not decimal or cat == "user_id") and _allowed(cat, fields):
        hits.add(_hit_label(cat, fields))
    if not decimal:
        for term, cat in prepared.contained:
            if term in low and _allowed(cat, fields):
                hits.add(_hit_label(cat, fields))
        for pattern, cat in prepared.ids:
            if pattern.search(low):
                hits.add(_hit_label(cat, fields))
    if _EMAIL_RE.search(value):
        hits.add("e-mail pattern")
    for kind, _, _ in find_secrets(value):
        hits.add(f"secret ({kind})")
    for canary in (CANARY, CANARY_LOGIN, CANARY_EMAIL):
        if canary.lower() in low:
            hits.add("canary")
    if "http://" in low or "https://" in low:
        hits.add("url")
    return hits


def _collect_values(name: str, data: bytes) -> tuple[dict[str, set[str]], set[str]]:
    """Distinct JSON string values of one member with the fields they occur in, and structural
    problems (as categories)."""
    problems: set[str] = set()
    values: dict[str, set[str]] = {}
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {data.decode("utf-8", errors="replace"): {"#text"}}, {"not utf-8"}
    docs: list[object] = []
    if name.endswith(".jsonl"):
        for line in text.split("\n"):
            if not line:
                continue
            try:
                docs.append(json.loads(line, parse_float=str, parse_constant=str))
            except (ValueError, RecursionError):
                problems.add("not json")
                values.setdefault(line, set()).add("#text")
    else:
        try:
            docs.append(json.loads(text, parse_float=str, parse_constant=str))
        except (ValueError, RecursionError):
            problems.add("not json")
            values.setdefault(text, set()).add("#text")
    for doc in docs:
        _walk_strings(doc, values)
    return values, problems


def _is_pairs(items: list[object]) -> bool:
    return bool(items) and all(
        isinstance(x, list) and len(x) == 2 and isinstance(x[0], str) for x in items)


def _walk_strings(doc: object, out: dict[str, set[str]]) -> None:
    stack: list[tuple[object, str]] = [(doc, "#root")]
    while stack:
        value, fld = stack.pop()
        if isinstance(value, str):
            out.setdefault(value, set()).add(fld)
        elif isinstance(value, dict):
            for k, v in value.items():
                stack.append((v, k if isinstance(k, str) else "#key"))
        elif isinstance(value, list):
            if fld in ("dims", "attrs", "counts", "extra") and _is_pairs(value):
                for key, v in value:  # type: ignore[misc]
                    out.setdefault(key, set()).add(f"{fld}#key")
                    stack.append((v, key))
            else:
                for item in value:
                    stack.append((item, fld))


def _text_hits(data: bytes) -> set[str]:
    low = data.lower()
    hits = set()
    for canary in (CANARY, CANARY_LOGIN, CANARY_EMAIL):
        if canary.lower().encode("utf-8") in low:
            hits.add("canary")
    if b"http://" in low or b"https://" in low:
        hits.add("url")
    if _EMAIL_RE.search(data.decode("utf-8", errors="replace")):
        hits.add("e-mail pattern")
    return hits


def leak_scan(members: Mapping[str, bytes], terms: frozenset[str]) -> list[tuple[str, str]]:
    """``(member, category)`` for every leak found, in member order, categories sorted — never
    the matched value.

    Per member: (a) every JSON string value equal (case-insensitive) to a term, or containing a
    login / e-mail / repository full name / workflow path of at least 6 characters (numeric user
    ids: whole values, or delimited digit runs of at least 6); repository names and workflow paths
    are not matched inside organizational label fields (team, cost center, org, entity ids) and
    merged team labels only inside ``team`` fields and non-label fields; ids, pseudonyms, dates,
    decimal quantities and the closed vocabularies of ``core.records`` are machine values and never
    match; (b) the e-mail pattern; (c) ``core.secrets.find_secrets``; (d) ``CANARY``,
    ``CANARY_LOGIN``, ``CANARY_EMAIL``; (e) ``http://`` / ``https://`` URLs — (b), (d) and (e) also
    over the raw member text."""
    prepared = _prepare(terms)
    out: list[tuple[str, str]] = []
    order = {name: i for i, name in enumerate(_MEMBER_ORDER)}
    for name in sorted(members, key=lambda n: (order.get(n, len(order)), n)):
        data = members[name]
        values, found = _collect_values(name, data)
        for value, fields in values.items():
            found |= _value_hits(value, fields, prepared)
        found |= _text_hits(data)
        out.extend((name, cat) for cat in sorted(found))
    return out


def _leak_message(hits: Sequence[tuple[str, str]]) -> str:
    by_member: dict[str, list[str]] = {}
    for member, cat in hits:
        by_member.setdefault(member, []).append(cat)
    first, cats = next(iter(by_member.items()))
    msg = f"export aborted: {len(cats)} leak(s) in {first}: {', '.join(cats)}"
    others = [m for m in by_member if m != first]
    if others:
        msg += f" (also in {', '.join(others)})"
    if any("team label" in c for _, c in hits):
        msg += "; a team label equals a login or e-mail: rename the team in --team-map-csv"
    return msg + "; nothing was written"


# ---------------------------------------------------------------------------------------------
# records: collection, window, dedupe
# ---------------------------------------------------------------------------------------------


@dataclass
class _Records:
    cost_lines: list[CostLine] = field(default_factory=list)
    aggregates: list[UsageAggregate] = field(default_factory=list)
    outcomes: list[OutcomeAggregate] = field(default_factory=list)
    licenses: list[LicenseSnapshot] = field(default_factory=list)
    activity: list[ActivityDay] = field(default_factory=list)
    config: list[ConfigSnapshot] = field(default_factory=list)


def _latest(items: Iterable[Any], key: Any) -> list[Any]:
    """One record per natural key: the latest ``fetched_ms`` wins, ties by canonical JSON."""
    best: dict[str, tuple[tuple[int, str], Any]] = {}
    for rec in items:
        k = key(rec)
        rank = (getattr(rec, "fetched_ms", 0), _line(rec))
        cur = best.get(k)
        if cur is None or rank > cur[0]:
            best[k] = (rank, rec)
    return [best[k][1] for k in sorted(best)]


def _outcome_key(o: OutcomeAggregate) -> str:
    return "\x1f".join((o.date_utc, o.team, o.source_kind))


def _has_names(result: IngestResult) -> bool:
    if any(_H_RE.match(v or "") for line in result.cost_lines for v in (line.repo, line.workflow)):
        return True
    return any(_H_RE.match(v) for agg in result.aggregates for _, v in agg.dims)


def _collect(results: Sequence[IngestResult], principal_key_id: str, name_key_id: str,
             window: tuple[str | None, str | None], dq: Counter[str]) -> _Records:
    recs = _Records()
    for result in results:
        if not isinstance(result, IngestResult):
            raise UsageError("write_bundle expects IngestResult values")
        people = bool(result.licenses or result.activity
                      or any(c.principal for c in result.cost_lines))
        if people and result.source.principal_key_id != principal_key_id:
            raise UsageError("an input was pseudonymized under another key than the export key")
        if _has_names(result) and result.source.name_key_id != name_key_id:
            raise UsageError("an input was name-hashed under another key than the export key")
        lanes = len(result.requests) + len(result.sessions) + len(result.events)
        if lanes:
            dq[DQ_LANES_DROPPED] += lanes
        recs.cost_lines.extend(result.cost_lines)
        recs.aggregates.extend(result.aggregates)
        recs.outcomes.extend(result.outcomes)
        recs.licenses.extend(result.licenses)
        recs.activity.extend(result.activity)
        recs.config.extend(result.config)
    since, until = window
    if since is not None or until is not None:
        lo = since or "0000-01-01"
        hi = until or "9999-12-31"

        def keep(date: str) -> bool:
            return lo <= date <= hi

        before = (len(recs.cost_lines) + len(recs.aggregates) + len(recs.outcomes)
                  + len(recs.licenses) + len(recs.activity))
        recs.cost_lines = [c for c in recs.cost_lines if keep(c.date_utc)]
        recs.aggregates = [a for a in recs.aggregates if keep(_date_of_ms(a.bucket_start_ms))]
        recs.outcomes = [o for o in recs.outcomes if keep(o.date_utc)]
        recs.licenses = [x for x in recs.licenses if keep(x.snapshot_date)]
        recs.activity = [x for x in recs.activity if keep(x.date_utc)]
        after = (len(recs.cost_lines) + len(recs.aggregates) + len(recs.outcomes)
                 + len(recs.licenses) + len(recs.activity))
        if before - after:
            dq[DQ_OUTSIDE_WINDOW] += before - after
    recs.cost_lines = _latest(recs.cost_lines, lambda c: c.line_id)
    recs.aggregates = _latest(recs.aggregates, lambda a: a.agg_id)
    recs.outcomes = _latest(recs.outcomes, _outcome_key)
    recs.licenses = _latest(recs.licenses, record_key)
    recs.activity = _latest(recs.activity, record_key)
    recs.config = _latest(recs.config, record_key)
    return recs


# ---------------------------------------------------------------------------------------------
# team k-merge (brief Build 2)
# ---------------------------------------------------------------------------------------------


def _team_of_attrs(attrs: tuple[tuple[str, Any], ...]) -> str | None:
    value = dict(attrs).get("team")
    return value if isinstance(value, str) else None


def _sum_opt(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return a + b


def _merge_aggregates(aggs: list[UsageAggregate], changed: set[str]) -> list[UsageAggregate]:
    """Aggregates whose team label changed are re-keyed on their new dims (their old id hashed
    the small team's label) and summed with every other one that now shares the same key."""
    keep = [a for a in aggs if a.agg_id not in changed]
    groups: dict[tuple, list[UsageAggregate]] = defaultdict(list)
    for a in aggs:
        if a.agg_id in changed:
            key = (a.source_kind, a.bucket_start_ms, a.bucket_end_ms, a.dims,
                   a.reported_cost_basis, a.finality, a.list_cost_nano is None,
                   a.reported_cost_nano is None)
            groups[key].append(a)
    for key, items in groups.items():
        usage = items[0].usage
        reported, listed = items[0].reported_cost_nano, items[0].list_cost_nano
        for a in items[1:]:
            usage = usage + a.usage
            reported = _sum_opt(reported, a.reported_cost_nano)
            listed = _sum_opt(listed, a.list_cost_nano)
        agg_id = natural_id("ag", key[0], "team-merged", key[1], key[2],
                            *(f"{k}={v}" for k, v in key[3]), key[4], key[5])
        keep.append(dataclasses.replace(
            items[0], agg_id=agg_id, usage=usage, reported_cost_nano=reported,
            list_cost_nano=listed, fetched_ms=max(a.fetched_ms for a in items)))
    return sorted(keep, key=lambda a: a.agg_id)


def _merge_outcomes(outs: list[OutcomeAggregate]) -> list[OutcomeAggregate]:
    groups: dict[str, list[OutcomeAggregate]] = defaultdict(list)
    for o in outs:
        groups[_outcome_key(o)].append(o)
    merged = []
    for items in groups.values():
        if len(items) == 1:
            merged.append(items[0])
            continue
        extra: Counter[str] = Counter()
        for o in items:
            extra.update(dict(o.extra))
        sums = {name: sum(getattr(o, name) for o in items)
                for name in ("n_users", "sessions", "commits", "pull_requests", "lines_added",
                             "lines_removed", "edits_accepted", "edits_rejected")}
        merged.append(dataclasses.replace(items[0], extra=tuple(sorted(extra.items())), **sums))
    return merged


def _k_merge_teams(recs: _Records, k: int) -> set[str]:
    """Relabel every team with fewer than *k* distinct people (licenses ∪ activity ∪ cost lines)
    as :data:`OTHER_TEAM` everywhere; returns the merged labels (memory only)."""
    people: dict[str, set[str]] = defaultdict(set)
    for lic in recs.licenses:
        if lic.team is not None:
            people[lic.team].add(lic.principal)
    for act in recs.activity:
        if act.team is not None:
            people[act.team].add(act.principal)
    for line in recs.cost_lines:
        if line.team is not None and line.principal is not None:
            people[line.team].add(line.principal)
    outcome_ok = {o.team for o in recs.outcomes if o.n_users >= k}
    labels: set[str] = set(people)
    labels.update(c.team for c in recs.cost_lines if c.team is not None)
    labels.update(v for a in recs.aggregates for kk, v in a.dims if kk == "team")
    labels.update(t for c in recs.config if (t := _team_of_attrs(c.attrs)) is not None)
    labels.update(o.team for o in recs.outcomes)
    small = {t for t in labels
             if t != OTHER_TEAM and len(people.get(t, ())) < k and t not in outcome_ok
             and not (t.startswith("(") and t.endswith(")"))}
    if not small:
        return small

    def relabel(team: str | None) -> str | None:
        return OTHER_TEAM if team in small else team

    recs.cost_lines = [dataclasses.replace(c, team=OTHER_TEAM) if c.team in small else c
                       for c in recs.cost_lines]
    recs.licenses = [dataclasses.replace(x, team=OTHER_TEAM) if x.team in small else x
                     for x in recs.licenses]
    recs.activity = [dataclasses.replace(x, team=OTHER_TEAM) if x.team in small else x
                     for x in recs.activity]
    changed: set[str] = set()
    aggs = []
    for a in recs.aggregates:
        if any(kk == "team" and v in small for kk, v in a.dims):
            changed.add(a.agg_id)
            a = dataclasses.replace(a, dims=tuple((kk, OTHER_TEAM if kk == "team" and v in small
                                                   else v) for kk, v in a.dims))
        aggs.append(a)
    recs.aggregates = _merge_aggregates(aggs, changed)
    recs.config = [dataclasses.replace(c, attrs=tuple(
        (kk, relabel(v) if kk == "team" else v) for kk, v in c.attrs))
        if _team_of_attrs(c.attrs) in small else c for c in recs.config]
    recs.outcomes = _merge_outcomes([dataclasses.replace(o, team=OTHER_TEAM)
                                     if o.team in small else o for o in recs.outcomes])
    return small


# ---------------------------------------------------------------------------------------------
# aggregate-only reduction (brief Build 3)
# ---------------------------------------------------------------------------------------------

_SEAT_DETAIL = ("team", "plan", "bucket", "surface", "assigned_via_team", "pending_cancellation",
                "created_over_30d", "zero_cost_30d")


def _seat_created_date(value: str | None) -> _dt.date | None:
    if not value or len(value) < 10 or not _DATE_RE.match(value[:10]):
        return None
    try:
        return _dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


@dataclass
class _Group:
    """People of one count row and the original keys merged into it."""

    people: set[str]
    origins: set[tuple]


def _ladder(rows: Mapping[tuple, set[str]], k: int, levels: Sequence[Any]
            ) -> tuple[dict[tuple, _Group], int]:
    """Publish groups of at least *k* people; merge the rest level by level (each level maps a key
    to a coarser key; a coarse key that is already published absorbs them). Returns the published
    groups by key and the number of people dropped (the residual below *k*). Deterministic: keys
    are visited in sorted order."""
    published: dict[tuple, _Group] = {}
    pending = {key: _Group(set(people), {key}) for key, people in rows.items()}
    for level in (None, *levels):
        if level is not None:
            merged: dict[tuple, _Group] = {}
            for key in sorted(pending, key=repr):
                grp = merged.setdefault(level(key), _Group(set(), set()))
                grp.people |= pending[key].people
                grp.origins |= pending[key].origins
            pending = merged
        rest: dict[tuple, _Group] = {}
        for key in sorted(pending, key=repr):
            grp = pending[key]
            if key in published:
                published[key].people |= grp.people
                published[key].origins |= grp.origins
            elif len(grp.people) >= k:
                published[key] = grp
            else:
                rest[key] = grp
        pending = rest
    return published, sum(len(g.people) for g in pending.values())


def _ai_credit_gross(recs: _Records) -> dict[str, list[tuple[str, int]]]:
    out: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for line in recs.cost_lines:
        if line.channel == "github_copilot" and (line.cost_type or "").startswith("ai_credit"):
            gross = line.list_amount_nano if line.list_amount_nano is not None else line.amount_nano
            out[line.principal or ""].append((line.date_utc, gross))
    return out


def _seat_rank(lic: LicenseSnapshot) -> tuple:
    """Which snapshot of one person on one date describes the seat best: a known plan, the seats
    API over the activity report, then the latest fetch and a deterministic org order."""
    return (lic.plan != "unknown", lic.source_kind == "github.copilot_seats", lic.fetched_ms,
            lic.org or "", lic.plan)


def _seat_counts(recs: _Records, k: int, dq: Counter[str]) -> list[ConfigSnapshot]:
    """``seat_counts`` rows per (entity, snapshot date): one seat per person and date (the seats
    API and the activity report describe the same seat; a multi-org seat is billed once), grouped
    by the pre-evaluated seat criteria and merged / suppressed below *k*."""
    gross = _ai_credit_gross(recs)
    best: dict[tuple[str, str], LicenseSnapshot] = {}
    for lic in recs.licenses:
        cur = best.get((lic.snapshot_date, lic.principal))
        if cur is None or _seat_rank(lic) > _seat_rank(cur):
            best[(lic.snapshot_date, lic.principal)] = lic
    by_snap: dict[tuple[str, str], list[LicenseSnapshot]] = defaultdict(list)
    for lic in best.values():
        entity = f"org:{lic.org}" if lic.org else "enterprise"
        by_snap[(entity, lic.snapshot_date)].append(lic)
    out: list[ConfigSnapshot] = []
    for (entity, date), lics in sorted(by_snap.items()):
        day = _dt.date.fromisoformat(date)
        lo = (day - _dt.timedelta(days=29)).isoformat()
        covered = any(lo <= d <= date for lines in gross.values() for d, _ in lines)
        detail: dict[tuple, set[str]] = defaultdict(set)
        team_people: dict[tuple, set[str]] = defaultdict(set)
        for lic in lics:
            created = _seat_created_date(lic.seat_created)
            spent = sum(g for d, g in gross.get(lic.principal, ()) if lo <= d <= date)
            key = (lic.team, lic.plan, lic.last_activity_bucket, lic.last_activity_surface,
                   lic.assigned_via_team, lic.pending_cancellation is not None,
                   created is not None and (day - created).days > 30,
                   covered and spent == 0)
            detail[key].add(lic.principal)
            team_people[(lic.team,)].add(lic.principal)
        levels = (
            lambda key: (OTHER_TEAM, *key[1:]),
            lambda key: (OTHER_TEAM, key[1], key[2], None, None, False, False, False),
            lambda key: (OTHER_TEAM, key[1], None, None, None, False, False, False),
        )
        rows, dropped = _ladder(detail, k, levels)
        summary, dropped_s = _ladder(team_people, k, (lambda key: (OTHER_TEAM,),))
        if dropped or dropped_s:
            dq[DQ_SUPPRESSED] += dropped + dropped_s
        snapshot_ms = _day_ms(date)
        fetched = max(x.fetched_ms for x in lics)
        for key, grp in rows.items():
            attrs = dict(zip(_SEAT_DETAIL, key, strict=True))
            attrs["n"] = len(grp.people)
            out.append(_count_row("seat_counts", entity, snapshot_ms, fetched, attrs))
        for key, grp in summary.items():
            out.append(_count_row("seat_counts", entity, snapshot_ms, fetched,
                                  {"team": key[0], "bucket": "*", "n_people": len(grp.people)}))
    return out


def _count_row(kind: str, entity: str, snapshot_ms: int, fetched_ms: int,
               attrs: Mapping[str, Any]) -> ConfigSnapshot:
    return ConfigSnapshot(snapshot_ms=snapshot_ms, source_kind="tokenbill.copilot_export",
                          kind=kind, entity_id=entity, attrs=tuple(sorted(attrs.items())),
                          fetched_ms=fetched_ms)


def _activity_counts(recs: _Records, k: int, dq: Counter[str]) -> list[ConfigSnapshot]:
    from tokenbill.core.catalog import editor_family  # facts-backed; imported at use

    groups: dict[tuple[str | None, str], list[ActivityDay]] = defaultdict(list)
    for act in recs.activity:
        groups[(act.team, act.date_utc[:7])].append(act)
    by_month: dict[str, dict[tuple, set[str]]] = defaultdict(dict)
    sums: dict[tuple[str | None, str], Counter[str]] = {}
    for (team, month), days in groups.items():
        by_month[month][(team,)] = {d.principal for d in days}
        c: Counter[str] = Counter()
        for d in days:
            counts = dict(d.counts)
            c["interactions"] += counts.get("interactions", 0)
            c["cli_requests"] += counts.get("cli_requests", 0)
            c["cli_prompt_tokens"] += counts.get("cli_prompt_tokens", 0)
            c["app_interactions"] += counts.get("app_prompts", 0)
            for key, value in counts.items():
                if key.startswith("ide:"):
                    c[f"ide:{editor_family(key)}"] += value
        sums[(team, month)] = c
    out: list[ConfigSnapshot] = []
    for month, rows in sorted(by_month.items()):
        published, dropped = _ladder(rows, k, (lambda key: (OTHER_TEAM,),))
        if dropped:
            dq[DQ_SUPPRESSED] += dropped
        fetched = max(d.fetched_ms for (t, m), days in groups.items() if m == month for d in days)
        for (team,), grp in published.items():
            total: Counter[str] = Counter()
            for (origin,) in grp.origins:
                total.update(sums[(origin, month)])
            attrs: dict[str, Any] = {"team": team, "month": month, "n_people": len(grp.people)}
            for name in ("interactions", "cli_requests", "cli_prompt_tokens", "app_interactions"):
                attrs[name] = total.get(name, 0)
            attrs.update({key: v for key, v in total.items() if key.startswith("ide:")})
            month_ms = _day_ms(f"{month}-01")
            out.append(_count_row("activity_counts", "enterprise", month_ms, fetched, attrs))
    return out


_COST_SUM_FIELDS = frozenset({"line_id", "principal", "amount_nano", "list_amount_nano",
                              "quantity", "fetched_ms"})


def _sum_cost_lines(lines: list[CostLine]) -> list[CostLine]:
    """Cost lines without principals: lines equal in every other field are summed (exact)."""
    names = [f.name for f in dataclasses.fields(CostLine) if f.name not in _COST_SUM_FIELDS]
    groups: dict[tuple, list[CostLine]] = defaultdict(list)
    for line in lines:
        key = (*(getattr(line, n) for n in names), line.list_amount_nano is None,
               line.quantity is None)
        groups[key].append(line)
    out = []
    for key, items in groups.items():
        amount = sum(x.amount_nano for x in items)
        listed = (None if items[0].list_amount_nano is None
                  else sum(x.list_amount_nano or 0 for x in items))
        quantity = None
        if items[0].quantity is not None:
            total = Decimal(0)
            for x in items:
                total = EXACT_CTX.add(total, Decimal(x.quantity or "0"))
            quantity = _decimal_text(total)
        line_id = natural_id("cl", "tokenbill.copilot_export", *(
            "" if v is None else str(v) for v in key))
        out.append(dataclasses.replace(
            items[0], line_id=line_id, principal=None, amount_nano=amount,
            list_amount_nano=listed, quantity=quantity,
            fetched_ms=max(x.fetched_ms for x in items)))
    return sorted(out, key=lambda c: c.line_id)


def _decimal_text(d: Decimal) -> str:
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


def _aggregate_only(recs: _Records, k: int, dq: Counter[str]) -> None:
    recs.config = recs.config + _seat_counts(recs, k, dq) + _activity_counts(recs, k, dq)
    recs.licenses = []
    recs.activity = []
    recs.cost_lines = _sum_cost_lines(recs.cost_lines)
    dq[DQ_AGGREGATE_ONLY] += 1
    for kind in AGGREGATE_ONLY_KINDS_OFF:
        dq[DQ_KIND_OFF + kind] += 1


# ---------------------------------------------------------------------------------------------
# manifest facts: entities, orgs, plan evidence
# ---------------------------------------------------------------------------------------------


def _orgs(recs: _Records) -> tuple[str, ...]:
    orgs: set[str] = set()
    orgs.update(c.workspace_id for c in recs.cost_lines
                if c.workspace_id and c.channel in _rec.COPILOT_CHANNELS)
    orgs.update(x.org for x in recs.licenses if x.org)
    orgs.update(v for a in recs.aggregates for kk, v in a.dims if kk == "organization" and v)
    orgs.update(c.entity_id[4:] for c in recs.config if c.entity_id.startswith("org:"))
    return tuple(sorted(orgs))


def _months(recs: _Records) -> list[str]:
    months: set[str] = set()
    months.update(c.date_utc[:7] for c in recs.cost_lines if c.channel == "github_copilot")
    months.update(x.snapshot_date[:7] for x in recs.licenses)
    months.update(_date_of_ms(c.snapshot_ms)[:7] for c in recs.config
                  if c.kind == "seat_counts")
    return sorted(months)


def _plan_evidence(recs: _Records) -> tuple[PlanEvidenceSummary, ...]:
    from tokenbill.core import pool  # the single plan-detection implementation (F-POOL)

    out = []
    for month in _months(recs):
        for pe in pool.detect_plans(recs.cost_lines, recs.licenses, recs.config, month=month):
            out.append(PlanEvidenceSummary(entity_id=pe.entity_id, month=pe.month, plan=pe.plan,
                                           source=pe.source, conflict=pe.conflict))
    return tuple(sorted(out, key=lambda p: (p.month, p.entity_id)))


def _entities(recs: _Records, orgs: Sequence[str],
              plans: Sequence[PlanEvidenceSummary]) -> tuple[str, ...]:
    ents: set[str] = {p.entity_id for p in plans}
    ents.update(f"org:{o}" for o in orgs)
    ents.update(f"cc:{c}" for c in (x.cost_center for x in recs.cost_lines) if c)
    ents.update(f"cc:{c}" for c in (x.cost_center for x in recs.licenses) if c)
    ents.update(c.entity_id for c in recs.config
                if c.entity_id == "enterprise" or c.entity_id.startswith(("org:", "cc:")))
    return tuple(sorted(ents))


# ---------------------------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------------------------

_SEED_KEYS = frozenset({"tool_version", "created_ms", "window", "principal_key_id",
                        "name_key_id", "key_rotated", "rows_excluded", "experimental", "dq"})


def _check_k(k: object) -> int:
    if type(k) is not int or k < 2:
        raise UsageError("k must be an integer >= 2")
    return k


def _check_date(value: object, what: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise UsageError(f"{what} must be a date YYYY-MM-DD")
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        raise UsageError(f"{what} must be a date YYYY-MM-DD") from None
    return value


def _check_seed(seed: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(seed, Mapping) or not set(seed) <= _SEED_KEYS:
        raise UsageError(f"manifest_seed keys must be within {sorted(_SEED_KEYS)}")
    out = dict(seed)
    for key in ("principal_key_id", "name_key_id"):
        if not isinstance(out.get(key), str) or not _KEY_ID_RE.match(out[key]):
            raise UsageError(f"manifest_seed.{key} must be a key id (k_<12 hex>)")
    if not isinstance(out.get("tool_version"), str) or not out["tool_version"]:
        raise UsageError("manifest_seed.tool_version must be a non-empty string")
    created = out.get("created_ms", 0)
    if type(created) is not int or created < 0:
        raise UsageError("manifest_seed.created_ms must be an int >= 0")
    window = out.get("window", (None, None))
    if isinstance(window, Mapping):
        window = (window.get("since"), window.get("until"))
    if not isinstance(window, (tuple, list)) or len(window) != 2:
        raise UsageError("manifest_seed.window must be (since, until)")
    since, until = _check_date(window[0], "since"), _check_date(window[1], "until")
    if since and until and since > until:
        raise UsageError("since must not be after until")
    out["window"] = (since, until)
    if type(out.get("key_rotated", False)) is not bool:
        raise UsageError("manifest_seed.key_rotated must be a bool")
    rows_excluded = out.get("rows_excluded", 0)
    if type(rows_excluded) is not int or rows_excluded < 0:
        raise UsageError("manifest_seed.rows_excluded must be an int >= 0")
    experimental = out.get("experimental", ())
    if not all(isinstance(x, str) for x in experimental):
        raise UsageError("manifest_seed.experimental must hold strings")
    out["experimental"] = tuple(sorted(set(experimental)))
    dq = out.get("dq", {})
    if not isinstance(dq, Mapping) or not all(
            isinstance(c, str) and type(n) is int and n >= 0 for c, n in dq.items()):
        raise UsageError("manifest_seed.dq must map codes to counts")
    return out


def _sources(results: Sequence[IngestResult]) -> tuple[SourceSummary, ...]:
    acc: dict[str, list[int]] = {}
    for r in results:
        row = acc.setdefault(r.source.adapter, [0, 0, 0])
        row[0] += 1
        row[1] += (len(r.cost_lines) + len(r.aggregates) + len(r.outcomes) + len(r.licenses)
                   + len(r.activity) + len(r.config))
        row[2] += len(r.quarantined)
    return tuple(SourceSummary(a, *v) for a, v in sorted(acc.items()))


def _sorted_members(recs: _Records) -> dict[str, list[Any]]:
    return {
        "aggregates": sorted(recs.aggregates, key=lambda a: a.agg_id),
        "config": sorted(recs.config, key=lambda c: (record_key(c), _line(c))),
        "cost_lines": sorted(recs.cost_lines, key=lambda c: c.line_id),
        "activity": sorted(recs.activity, key=record_key),
        "licenses": sorted(recs.licenses, key=record_key),
        "outcomes": sorted(recs.outcomes, key=lambda o: (_outcome_key(o), _line(o))),
    }


def _write_zip(out_path: Path, members: Mapping[str, bytes]) -> None:
    """Deterministic bytes: members in the fixed order, dated 1980-01-01, deflate level 9, Unix
    mode 0600, no comment, no extra fields; written to a private temporary file next to
    *out_path* and renamed (the file stays 0600)."""
    tmp = out_path.with_name(f".{out_path.name}.{_secrets.token_hex(8)}.tmp")
    try:
        with open_private(tmp, "xb") as fh:
            with zipfile.ZipFile(fh, "w") as zf:
                for name in _MEMBER_ORDER:
                    if name not in members:
                        continue
                    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.create_system = 3
                    info.external_attr = 0o100600 << 16
                    zf.writestr(info, members[name], compress_type=zipfile.ZIP_DEFLATED,
                                compresslevel=9)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, out_path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def write_bundle(results: Sequence[IngestResult], out_path: Path, *,
                 manifest_seed: Mapping[str, Any], leak_terms: frozenset[str], k: int,
                 aggregate_only: bool) -> BundleManifest:
    """Write a ``tokenbill/copilot-export@1`` bundle from adapter results; returns its manifest.

    *manifest_seed* carries what only the caller knows: ``tool_version``, ``created_ms``,
    ``window`` (``(since, until)`` inclusive dates or None — records outside are dropped),
    ``principal_key_id`` / ``name_key_id`` (the export key id; every input must use it),
    ``key_rotated``, ``rows_excluded``, ``experimental`` and extra ``dq`` counts. Records are
    deduplicated (latest fetch wins), team labels below *k* people become :data:`OTHER_TEAM`,
    ``aggregate_only`` reduces licenses / activity to count rows and strips every principal, the
    plan evidence comes from ``core.pool.detect_plans``; then every member is leak-scanned against
    *leak_terms* (plus the merged team labels) and, only when clean, the zip is written. A hit
    raises ``PrivacyError`` naming the member and categories, and nothing is written."""
    k = _check_k(k)
    seed = _check_seed(manifest_seed)
    if not isinstance(leak_terms, frozenset):
        raise UsageError("leak_terms must be a frozenset")
    out_path = Path(out_path)
    dq: Counter[str] = Counter(seed.get("dq", {}))
    for r in results:
        for note in getattr(r, "notes", ()):
            if isinstance(note, DataQualityNote) and note.count > 0:
                dq[note.code] += note.count
    recs = _collect(results, seed["principal_key_id"], seed["name_key_id"], seed["window"], dq)
    merged = _k_merge_teams(recs, k)
    if merged:
        dq[DQ_TEAMS_MERGED] += len(merged)
    if aggregate_only:
        _aggregate_only(recs, k, dq)
    plans = _plan_evidence(recs)
    orgs = _orgs(recs)
    ordered = _sorted_members(recs)
    members: dict[str, bytes] = {}
    for kind, items in ordered.items():
        members[f"records/{kind}.jsonl"] = "".join(_line(r) + "\n" for r in items).encode("ascii")
    terms = leak_terms if isinstance(leak_terms, LeakTerms) else LeakTerms(leak_terms)
    terms = terms.merged({t: "merged_team" for t in merged})
    manifest = BundleManifest(
        schema=EXPORT_SCHEMA,
        tool_version=seed["tool_version"],
        created_ms=seed.get("created_ms", 0),
        window=seed["window"],
        entities=_entities(recs, orgs, plans),
        orgs=orgs,
        sources=_sources(results),
        counts=tuple((kind, len(ordered[kind])) for kind in RECORD_KINDS),
        dq=tuple(sorted((c, n) for c, n in dq.items() if n > 0)),
        principal_key_id=seed["principal_key_id"],
        name_key_id=seed["name_key_id"],
        key_rotated=seed.get("key_rotated", False),
        rows_excluded=seed.get("rows_excluded", 0),
        k=k,
        teams_merged=len(merged),
        privacy_mode="aggregate_only" if aggregate_only else "pseudonymous",
        plan_evidence=plans,
        experimental=seed["experimental"],
        leak_scan=LeakScanSummary(terms=len(leak_terms)),
    )
    members = {MANIFEST_MEMBER: _canonical(manifest.to_json()).encode("ascii"), **members}
    hits = leak_scan(members, terms)
    if aggregate_only:
        hits.extend((name, "pseudonym in an aggregate-only bundle") for name, data in
                    members.items() if re.search(rb"\bp_[0-9a-f]{20}\b", data))
    if hits:
        raise PrivacyError(_leak_message(hits))
    _write_zip(out_path, members)
    return manifest


# ---------------------------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------------------------

_DECODERS: Mapping[str, type] = types.MappingProxyType({
    "aggregates": UsageAggregate, "config": ConfigSnapshot, "cost_lines": CostLine,
    "activity": ActivityDay, "licenses": LicenseSnapshot, "outcomes": OutcomeAggregate})


def _check_infos(infos: Sequence[zipfile.ZipInfo]) -> None:
    if not infos:
        raise SourceError("bundle: empty archive")
    if len(infos) > _MAX_MEMBERS:
        raise SourceError(f"bundle: more than {_MAX_MEMBERS} members")
    seen: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename
        parts = name.split("/")
        if (name.startswith("/") or "\\" in name or ".." in parts or ":" in name
                or any(not p for p in parts)):
            raise SourceError("bundle: unsafe member path")
        mode = info.external_attr >> 16
        if info.is_dir() or stat.S_ISDIR(mode):
            raise SourceError("bundle: directory member")
        if stat.S_ISLNK(mode):
            raise SourceError("bundle: symbolic-link member")
        if name not in _MEMBER_ORDER:
            raise SourceError("bundle: unexpected member name")
        if name in seen:
            raise SourceError("bundle: duplicate member")
        seen.add(name)
        if info.flag_bits & 0x1:
            raise SourceError("bundle: encrypted member")
        if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise SourceError("bundle: unsupported compression")
        if info.file_size > _MAX_MEMBER_BYTES:
            raise SourceError("bundle: member larger than 512 MiB")
        if info.file_size > _MAX_RATIO * info.compress_size:
            raise SourceError("bundle: member compression ratio above 200:1")
        total += info.file_size
        if total > _MAX_TOTAL_BYTES:
            raise SourceError("bundle: members larger than 2 GiB in total")
    if infos[0].filename != MANIFEST_MEMBER:
        raise SourceError("bundle: manifest.json must be the first member")
    if infos[0].file_size > _MAX_MANIFEST_BYTES:
        raise SourceError("bundle: manifest larger than 1 MiB")


def _read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    try:
        data = zf.read(info)
    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError,
            OSError, ValueError):
        raise SourceError(f"bundle: member {info.filename} unreadable") from None
    if len(data) != info.file_size:
        raise SourceError(f"bundle: member {info.filename} size mismatch")
    return data


def _reject_constant(_token: str) -> object:
    raise ValueError("non-finite number")


def _decode_member(name: str, data: bytes) -> list[Any]:
    kind = _KIND_OF_MEMBER[name]
    cls = _DECODERS[kind]
    if data and not data.endswith(b"\n"):
        raise SourceError(f"bundle: {name} does not end with a newline")
    out = []
    for i, raw in enumerate(data.split(b"\n")[:-1] if data else (), start=1):
        try:
            obj = json.loads(raw.decode("utf-8"), parse_float=Decimal,
                             parse_constant=_reject_constant)
        except (ValueError, RecursionError, UnicodeDecodeError):
            raise SourceError(f"bundle: {name} line {i}: not JSON") from None
        if not isinstance(obj, dict):
            raise SourceError(f"bundle: {name} line {i}: not an object")
        out.append(from_json(cls, obj))
    return out


def _check_names(recs: Mapping[str, list[Any]], manifest: BundleManifest) -> None:
    for agg in recs["aggregates"]:
        for key, value in agg.dims:
            if key in ("repo", "workflow") and not _H_RE.match(value):
                raise SourceError("bundle: aggregate name dims must be h_ pseudonyms")
    if manifest.privacy_mode == "aggregate_only":
        if recs["licenses"] or recs["activity"] or any(c.principal for c in recs["cost_lines"]):
            raise SourceError("bundle: per-person records in an aggregate-only bundle")


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
                size += len(chunk)
    except OSError:
        raise SourceError(f"{path.name}: unreadable") from None
    return digest.hexdigest(), size


def _read_raw(path: Path) -> tuple[BundleManifest, dict[str, list[Any]], dict[str, bytes]]:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, ValueError, EOFError):
        raise SourceError(f"{path.name}: not a readable zip archive") from None
    with zf:
        infos = zf.infolist()
        _check_infos(infos)
        raw = {info.filename: _read_member(zf, info) for info in infos}
    try:
        manifest_obj = json.loads(raw[MANIFEST_MEMBER].decode("utf-8"),
                                  parse_float=Decimal, parse_constant=_reject_constant)
    except (ValueError, RecursionError, UnicodeDecodeError):
        raise SourceError("bundle manifest: not JSON") from None
    manifest = _decode_manifest(manifest_obj)
    recs: dict[str, list[Any]] = {}
    for name, kind in _KIND_OF_MEMBER.items():
        recs[kind] = _decode_member(name, raw[name]) if name in raw else []
        if len(recs[kind]) != manifest.count(kind):
            raise SourceError(f"bundle: {kind} count differs from the manifest")
    _check_names(recs, manifest)
    return manifest, recs, raw


def _recheck(raw: Mapping[str, bytes]) -> None:
    """Defence in depth on read: the content checks of the leak gate that need no terms."""
    hits = leak_scan(raw, LeakTerms())
    if hits:
        member, cat = hits[0]
        raise SourceError(f"bundle: member {member} failed the content re-check ({cat})")


def read_bundle(path: Path) -> tuple[BundleManifest, IngestResult]:
    """Read and verify a bundle: the manifest and one ``IngestResult`` (adapter
    ``copilot-export``, key ids from the manifest) with every record kind.

    Limits: at most 16 members, exactly the manifest and the six record member names, no
    directories, absolute or ``..`` paths or symbolic links, no encryption, stored or deflate only,
    each member ≤ 512 MiB and ≤ 200× its compressed size, ≤ 2 GiB in total, a manifest ≤ 1 MiB
    with the export schema; lines decode with ``core.records.from_json`` (validators run, so a raw
    login cannot pass a ``p_`` field: ``ContractViolation``); a count differing from the manifest,
    a non-``h_`` name dim or a leak-gate content hit → ``SourceError``."""
    path = Path(path)
    sha, size = _file_digest(path)
    manifest, recs, raw = _read_raw(path)
    _recheck(raw)
    caps = set()
    if recs["aggregates"]:
        caps.add("aggregates")
    if recs["cost_lines"]:
        caps.add("cost")
        if any(c.channel in _rec.COPILOT_CHANNELS for c in recs["cost_lines"]):
            caps.add("copilot_billing")
    for kind, cap in (("licenses", "licenses"), ("activity", "activity"), ("config", "config"),
                      ("outcomes", "outcomes")):
        if recs[kind]:
            caps.add(cap)
    notes = [DataQualityNote(code=f"export:{code}", severity="info", count=count,
                             detail="data-quality count recorded by the admin's export")
             for code, count in manifest.dq]
    source = SourceInfo(source_id=stable_id("src", "copilot-export", sha), adapter="copilot-export",
                        name_hmac="", sha256=sha, bytes=size, name_key_id=manifest.name_key_id,
                        principal_key_id=manifest.principal_key_id)
    total = sum(len(v) for v in recs.values())
    result = IngestResult(
        source=source, requests=[], sessions=[], events=[], aggregates=recs["aggregates"],
        cost_lines=recs["cost_lines"], outcomes=recs["outcomes"], quarantined=[], notes=notes,
        stats={"records": total, "members": len(raw)}, capabilities=frozenset(caps),
        licenses=recs["licenses"], activity=recs["activity"], config=recs["config"])
    return manifest, result


# ---------------------------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------------------------


def _team_seats(recs: Mapping[str, list[Any]], manifest: BundleManifest) -> dict[str, int]:
    if manifest.privacy_mode == "pseudonymous":
        people: dict[str, set[str]] = defaultdict(set)
        for lic in recs["licenses"]:
            people[lic.team if lic.team is not None else "(unattributed)"].add(lic.principal)
        return {team: len(p) for team, p in people.items()}
    per_date: dict[str, Counter[str]] = defaultdict(Counter)
    for c in recs["config"]:
        if c.kind != "seat_counts":
            continue
        attrs = dict(c.attrs)
        if attrs.get("bucket") == "*" and isinstance(attrs.get("n_people"), int):
            team = attrs.get("team") if isinstance(attrs.get("team"), str) else "(unattributed)"
            per_date[_date_of_ms(c.snapshot_ms)][team] += attrs["n_people"]
    seats: dict[str, int] = {}
    for counts in per_date.values():
        for team, n in counts.items():
            seats[team] = max(seats.get(team, 0), n)
    return seats


def inspect_bundle(path: Path) -> dict[str, object]:
    """What a bundle holds — counts only, never a record or a pseudonym: the manifest, per-kind
    record counts, the months covered, team labels with their seat counts (only teams with at least
    ``k`` seats), plan evidence and data-quality codes. Runs every reader check first."""
    path = Path(path)
    manifest, recs, raw = _read_raw(path)
    _recheck(raw)
    months: set[str] = set()
    months.update(c.date_utc[:7] for c in recs["cost_lines"])
    months.update(x.snapshot_date[:7] for x in recs["licenses"])
    months.update(x.date_utc[:7] for x in recs["activity"])
    months.update(_date_of_ms(a.bucket_start_ms)[:7] for a in recs["aggregates"])
    seats = _team_seats(recs, manifest)
    teams = [{"team": t, "seats": n} for t, n in sorted(seats.items()) if n >= manifest.k]
    doc = manifest.to_json()
    return {
        "manifest": doc,
        "counts": {kind: len(recs[kind]) for kind in RECORD_KINDS},
        "months": sorted(months),
        "teams": teams,
        "teams_below_k": sum(1 for n in seats.values() if n < manifest.k),
        "plan_evidence": doc["plan_evidence"],
        "dq": doc["dq"],
    }


def render_inspect(d: Mapping[str, object], fmt: str = "text") -> str:
    """Render :func:`inspect_bundle` output as canonical JSON (``fmt="json"``) or terminal text
    (every string sanitized)."""
    if fmt == "json":
        return json.dumps(d, sort_keys=True, indent=2) + "\n"
    if fmt != "text":
        raise UsageError("render_inspect: fmt must be 'text' or 'json'")
    m: Any = d.get("manifest", {})

    def s(value: object) -> str:
        return sanitize(str(value), 200)

    window = m.get("window", {})
    months: Any = d.get("months", [])
    lines = [
        f"bundle {s(m.get('schema'))} (tool {s(m.get('tool_version'))})",
        f"window: {s(window.get('since') or 'open')} .. {s(window.get('until') or 'open')}",
        f"privacy: {s(m.get('privacy_mode'))}, k = {s(m.get('k'))}, "
        f"teams merged into (other): {s(m.get('teams_merged'))}, "
        f"rows excluded (erasure): {s(m.get('rows_excluded'))}",
        f"export key id: {s(m.get('principal_key_id'))} (name key id "
        f"{s(m.get('name_key_id'))}){' - rotated' if m.get('key_rotated') else ''}",
        f"leak scan: {s(m.get('leak_scan', {}).get('result'))} "
        f"({s(m.get('leak_scan', {}).get('terms'))} identity values checked)",
        "entities: " + (", ".join(s(e) for e in m.get("entities", [])) or "none"),
        "months: " + (", ".join(s(x) for x in months) or "none"),
        "records:",
    ]
    counts: Any = d.get("counts", {})
    lines.extend(f"  {kind}: {s(counts.get(kind, 0))}" for kind in RECORD_KINDS)
    lines.append("sources:")
    lines.extend(f"  {s(src['adapter'])}: {s(src['files'])} file(s), {s(src['records'])} "
                 f"record(s), {s(src['quarantined'])} quarantined" for src in m.get("sources", []))
    teams: Any = d.get("teams", [])
    lines.append(f"teams (seats >= k): {len(teams)}; teams below k: {s(d.get('teams_below_k', 0))}")
    lines.extend(f"  {s(t['team'])}: {s(t['seats'])} seat(s)" for t in teams)
    lines.append("plan evidence:")
    plans: Any = d.get("plan_evidence", [])
    lines.extend(f"  {s(p['entity_id'])} {s(p['month'])}: {s(p['plan'])} from {s(p['source'])}"
                 f"{' (conflict)' if p['conflict'] else ''}" for p in plans)
    if not plans:
        lines.append("  none (plan unknown: both scenarios will be shown)")
    lines.append("data quality:")
    dq: Any = d.get("dq", [])
    lines.extend(f"  {s(row['code'])}: {s(row['count'])}" for row in dq)
    if not dq:
        lines.append("  none")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------
# inputs, erasure pre-filter, team map, pseudonym, guide
# ---------------------------------------------------------------------------------------------


def _expand_inputs(paths: Sequence[Path], *, allow_empty: bool = False) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            files.extend(f for f in sorted(p.rglob("*"))
                         if f.is_file() and not any(part.startswith(".")
                                                    for part in f.relative_to(p).parts))
        elif p.is_file():
            files.append(p)
        else:
            raise UsageError(f"input {p.name}: not found")
    unique: list[Path] = []
    seen: set[Path] = set()
    for f in files:
        r = f.resolve()
        if r not in seen:
            seen.add(r)
            unique.append(f)
    if not unique and not allow_empty:
        raise UsageError("no input files")
    return unique


class _RawNumber(str):
    """A JSON number token kept verbatim while a raw file is rewritten (exact decimals)."""


def _dump_raw_json(obj: object) -> str:
    if isinstance(obj, _RawNumber):
        return str(obj)
    if isinstance(obj, dict):
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + _dump_raw_json(v)
                              for k, v in obj.items()) + "}"
    if isinstance(obj, list):
        return "[" + ",".join(_dump_raw_json(v) for v in obj) + "]"
    return json.dumps(obj, ensure_ascii=False)


def _load_raw_json(text: str) -> object:
    return json.loads(text, parse_float=_RawNumber, parse_int=_RawNumber,
                      parse_constant=_RawNumber)


def _excluded_obj(obj: object, excluded: frozenset[str]) -> bool:
    if not isinstance(obj, dict):
        return False
    login = obj.get("user_login")
    assignee = obj.get("assignee")
    if isinstance(assignee, dict):
        login = login if isinstance(login, str) else assignee.get("login")
    return isinstance(login, str) and login.strip().lower() in excluded


def _filter_json(obj: object, excluded: frozenset[str]) -> tuple[object, int]:
    """Drop every object of a person in *excluded* from the lists it is in; returns the object
    and the number of rows dropped."""
    if isinstance(obj, dict):
        out: dict[str, object] = {}
        n = 0
        for k, v in obj.items():
            out[k], m = _filter_json(v, excluded)
            n += m
        return out, n
    if isinstance(obj, list):
        kept: list[object] = []
        n = 0
        for item in obj:
            if _excluded_obj(item, excluded):
                n += 1
                continue
            item, m = _filter_json(item, excluded)
            n += m
            kept.append(item)
        return kept, n
    return obj, 0


def _filter_csv(text: str, excluded: frozenset[str]) -> tuple[str, int]:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return text, 0
    header = [h.strip().lower() for h in rows[0]]
    idx = [i for i, h in enumerate(header) if h in ("username", "login", "user_login")]
    if not idx:
        return text, 0
    kept = [rows[0]]
    dropped = 0
    for row in rows[1:]:
        if any(i < len(row) and row[i].strip().lower() in excluded for i in idx):
            dropped += 1
        else:
            kept.append(row)
    if not dropped:
        return text, 0
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(kept)
    return buf.getvalue(), dropped


def _filter_text(text: str, excluded: frozenset[str]) -> tuple[str, int]:
    head = text.lstrip()[:1]
    if head not in ("{", "["):
        try:
            return _filter_csv(text, excluded)
        except csv.Error:
            return text, 0
    try:
        doc = _load_raw_json(text)
    except (ValueError, RecursionError):
        doc = None
    if doc is not None:
        if _excluded_obj(doc, excluded):
            return "", 1
        new, n = _filter_json(doc, excluded)
        return (_dump_raw_json(new) + "\n", n) if n else (text, 0)
    lines_out: list[str] = []
    dropped = 0
    for line in text.splitlines():
        try:
            obj = _load_raw_json(line) if line.strip() else None
        except (ValueError, RecursionError):
            obj = None
        if obj is None:
            lines_out.append(line)
            continue
        if _excluded_obj(obj, excluded):
            dropped += 1
            continue
        new, n = _filter_json(obj, excluded)
        dropped += n
        lines_out.append(_dump_raw_json(new) if n else line)
    return ("\n".join(lines_out) + "\n", dropped) if dropped else (text, 0)


@contextlib.contextmanager
def _prefiltered(files: Sequence[Path], exclude_logins: frozenset[str]
                 ) -> Iterator[tuple[list[Path], int]]:
    """The inputs with every row of an excluded login removed: files holding such rows are
    replaced by filtered copies in a private (0700) temporary directory that is removed on exit,
    also after a failure. Yields ``(paths, rows_excluded)``."""
    excluded = frozenset(x.strip().lower() for x in exclude_logins if x and x.strip())
    if not excluded:
        yield list(files), 0
        return
    workdir = Path(tempfile.mkdtemp(prefix="tokenbill-export-"))
    try:
        os.chmod(workdir, 0o700)
        out: list[Path] = []
        total = 0
        for i, path in enumerate(files):
            data = _read_input_bytes(path)
            if data is None:
                out.append(path)
                continue
            gz = path.read_bytes()[:2] == b"\x1f\x8b"
            text = data.decode("utf-8-sig", errors="surrogateescape")
            new_text, n = _filter_text(text, excluded)
            if not n:
                out.append(path)
                continue
            total += n
            target = workdir / str(i) / path.name
            payload = new_text.encode("utf-8", errors="surrogateescape")
            with open_private(target, "wb") as fh:
                if gz:
                    with gzip.GzipFile(filename="", mode="wb", fileobj=fh, mtime=0) as g:
                        g.write(payload)
                else:
                    fh.write(payload)
            out.append(target)
        yield out, total
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def read_team_map_csv(path: Path) -> dict[str, str]:
    """``login → team`` from a CSV with the header ``login,team`` (further columns ignored; a row
    with an empty team is skipped). A blank, duplicate (case-insensitive) or e-mail-shaped login,
    or a team label with control characters or over 128 characters → ``UsageError`` naming the line
    number only."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise UsageError(f"team map {path.name}: not readable as UTF-8 text") from None
    reader = csv.reader(io.StringIO(text))
    out: dict[str, str] = {}
    seen: set[str] = set()
    try:
        header = next(reader, None)
        if header is None or [h.strip().lower() for h in header[:2]] != ["login", "team"]:
            raise UsageError(f"team map {path.name}: the header must be login,team")
        for row in reader:
            line = reader.line_num
            if not row or not any(c.strip() for c in row):
                continue
            login = row[0].strip()
            team = row[1].strip() if len(row) > 1 else ""
            if not login:
                raise UsageError(f"team map {path.name} line {line}: blank login")
            if "@" in login:
                raise UsageError(f"team map {path.name} line {line}: e-mail-shaped login "
                                 "(use GitHub logins)")
            if login.lower() in seen:
                raise UsageError(f"team map {path.name} line {line}: duplicate login")
            seen.add(login.lower())
            if not team:
                continue
            if _CONTROL_RE.search(team) or len(team) > 128:
                raise UsageError(f"team map {path.name} line {line}: invalid team label")
            out[login] = team
    except csv.Error:
        raise UsageError(f"team map {path.name} line {reader.line_num}: malformed CSV") from None
    return out


def pseudonym_of(login: str, *, key: bytes) -> str:
    """The ``p_`` value the adapters produce for *login* under the export *key*
    (``core.ids.pseudonym(key, "p", login)`` of the stripped login, SPEC §5.1 central-ingest), for
    ``copilot pseudonym`` (erasure requests). Never logs the login."""
    if not isinstance(login, str) or not login.strip():
        raise UsageError("pseudonym: a login is required")
    if not isinstance(key, (bytes, bytearray)) or len(key) < 16:
        raise UsageError("pseudonym: an export key of at least 16 bytes is required")
    return pseudonym(bytes(key), "p", login.strip())


def admin_guide_text() -> str:
    """The admin guide (packaged ``handoff_data/admin_guide.md``): the single source of
    ``tokenbill copilot admin-guide`` and of ``docs/COPILOT-ADMIN.md``."""
    return (resources.files("tokenbill.copilot.handoff_data")
            .joinpath("admin_guide.md").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------
# export orchestration
# ---------------------------------------------------------------------------------------------


def _ms_of(date: str | None, *, end: bool = False) -> int | None:
    if date is None:
        return None
    return _day_ms(date) + (_DAY_MS if end else 0)


def _answers_result(answers: Sequence[ConfigSnapshot]) -> IngestResult:
    for snap in answers:
        if not isinstance(snap, ConfigSnapshot):
            raise UsageError("answers must be ConfigSnapshot records (admin_answers.parse_answers)")
    source = SourceInfo(source_id=stable_id("src", "admin-answers"), adapter="admin-answers",
                        name_hmac="", sha256="", bytes=0, name_key_id=None, principal_key_id=None)
    return IngestResult(source=source, requests=[], sessions=[], events=[], aggregates=[],
                        cost_lines=[], outcomes=[], quarantined=[], notes=[], stats={},
                        capabilities=frozenset({"config"}), config=list(answers))


def _notes_of(manifest: BundleManifest, notes: Sequence[DataQualityNote]
              ) -> tuple[DataQualityNote, ...]:
    first: dict[str, DataQualityNote] = {}
    for note in notes:
        first.setdefault(note.code, note)
    out = []
    for code, count in manifest.dq:
        known = first.get(code)
        out.append(DataQualityNote(
            code=code, severity=known.severity if known else "info", count=count,
            detail=known.detail if known else _DQ_DETAILS.get(
                code, "not available in an aggregate-only bundle"
                if code.startswith(DQ_KIND_OFF) else code)))
    return tuple(out)


def export_from_files(paths: Sequence[Path], out_path: Path, *, key: bytes,
                      team_map: Mapping[str, str], cost_center_map: Mapping[str, str],
                      answers: Sequence[ConfigSnapshot] = (), k: int = 5,
                      aggregate_only: bool = False, since: str | None, until: str | None,
                      experimental: frozenset[str] = frozenset(),
                      exclude_logins: frozenset[str] = frozenset(), key_rotated: bool = False,
                      now_ms: int, tool_version: str) -> ExportReport:
    """Turn raw GitHub files into one ``tokenbill/copilot-export@1`` bundle at *out_path*.

    Every input (files, or directories read recursively) is sniffed with
    ``core.registry.sniff_adapter`` and read with ``IngestOptions(identity_mode="central-ingest")``
    under the export *key* (principal and name key); ``copilot-export`` and
    ``github-usage-records`` inputs are refused (``UsageError``); unrecognized files are skipped and
    counted. Rows of *exclude_logins* are dropped before any adapter pseudonymizes them (filtered
    copies in a private temporary directory, removed afterwards) and counted as ``rows_excluded``.
    *answers* (``admin_answers.parse_answers``) travel as configuration rows. The leak gate
    (:func:`leak_scan`) runs over every member before anything is written (``PrivacyError`` on a
    hit, CLI exit 3)."""
    if not isinstance(key, (bytes, bytearray)) or len(key) < 16:
        raise UsageError("export key: at least 16 bytes of key material are required")
    key = bytes(key)
    k = _check_k(k)
    since, until = _check_date(since, "since"), _check_date(until, "until")
    if since and until and since > until:
        raise UsageError("since must not be after until")
    unknown_flags = set(experimental) - EXPERIMENTAL_FLAGS
    if unknown_flags:
        raise UsageError("unknown experimental flag(s); allowed: "
                         + ", ".join(sorted(EXPERIMENTAL_FLAGS)))
    if type(now_ms) is not int or now_ms < 0:
        raise UsageError("now_ms must be an int >= 0")
    for name, mapping in (("team_map", team_map), ("cost_center_map", cost_center_map)):
        if not isinstance(mapping, Mapping) or not all(
                isinstance(a, str) and isinstance(b, str) for a, b in mapping.items()):
            raise UsageError(f"{name} must map logins to labels")
    out_path = Path(out_path)
    files = _expand_inputs(list(paths))
    out_resolved = out_path.resolve()
    if any(f.resolve() == out_resolved for f in files):
        raise UsageError("the output bundle must not be one of the inputs")
    terms = harvest_leak_terms(files, team_map_logins=[*team_map, *exclude_logins])
    kid = key_id(key)
    opts = IngestOptions(
        identity_mode="central-ingest", principal_key=key, principal_key_id=kid, name_key=key,
        name_key_id=kid, team_map=tuple(sorted(team_map.items())),
        cost_center_map=tuple(sorted(cost_center_map.items())), k_anonymity=k, now_ms=now_ms,
        since_ms=_ms_of(since), until_ms=_ms_of(until, end=True),
        experimental=frozenset(experimental))
    from tokenbill.core import registry  # resolved at runtime (tests register fake adapters)

    results: list[IngestResult] = []
    notes: list[DataQualityNote] = []
    unavailable: dict[str, DataQualityNote] = {}
    unrecognized = 0
    with _prefiltered(files, frozenset(exclude_logins)) as (inputs, rows_excluded):
        for path in inputs:
            sniff_notes: list[DataQualityNote] = []
            adapter = registry.sniff_adapter(path, notes=sniff_notes)
            for note in sniff_notes:
                unavailable.setdefault(note.detail, note)
            if adapter is None:
                unrecognized += 1
                continue
            if adapter.name in _REFUSED_ADAPTERS:
                raise UsageError(f"input {path.name}: {adapter.name} files are never exported "
                                 "(no re-export, no raw request bodies)")
            result = adapter.read(path, opts)
            results.append(result)
            notes.extend(result.notes)
    if not results:
        raise UsageError("no input file was recognized by a Copilot adapter")
    notes.extend(unavailable.values())
    if answers:
        results.append(_answers_result(answers))
    seed_dq: Counter[str] = Counter()
    for note in unavailable.values():
        seed_dq[note.code] += note.count
    if unrecognized:
        seed_dq[DQ_UNRECOGNIZED] += unrecognized
    seed = {"tool_version": tool_version, "created_ms": now_ms, "window": (since, until),
            "principal_key_id": kid, "name_key_id": kid, "key_rotated": bool(key_rotated),
            "rows_excluded": rows_excluded, "experimental": tuple(experimental),
            "dq": dict(seed_dq)}
    manifest = write_bundle(results, out_path, manifest_seed=seed, leak_terms=terms, k=k,
                            aggregate_only=aggregate_only)
    return ExportReport(out_path=out_path, manifest=manifest, dq=_notes_of(manifest, notes))

