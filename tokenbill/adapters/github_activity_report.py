"""``github-copilot-activity-report``: the UI-only Copilot activity report CSV (addendum §5.15).

The report an admin downloads from the enterprise or organization Copilot licensing / access page
("Get activity report"; exact click path **VERIFY**, addendum §19.5 #33). Documented fields
(metrics-data reference): ``report_time, login, last_authenticated_at, last_activity_at,
last_surface_used``; refreshed every 30 minutes, 90-day retention; ``last_surface_used`` is an
editor name and version (``VS Code 1.89.1``), a GitHub.com feature (``Copilot Chat``) or
``Unspecified``. More columns are tolerated, a UTF-8 BOM is stripped, timestamps are ISO 8601 or
``M/D/YYYY`` (with an optional time).

Each login becomes one ``LicenseSnapshot`` (source kind ``github.copilot_activity_report``) with
``plan="unknown"`` and ``assigned_via_team=None`` (the report knows neither), ``seat_created`` and
``pending_cancellation`` None, recency buckets against ``report_time`` (``0-7``, ``8-30``,
``31-90``, ``none_90d``; an empty timestamp → ``none_90d``), ``last_activity_surface`` from
``core.catalog.editor_family`` (``Unspecified`` / empty → None, unknown strings → ``other``;
JetBrains strings **VERIFY**, §19.5 #22 / #31) and ``org`` from ``opts.attribution.workspace_id``
(an org report) else None. The login is mapped to team / cost center through the ingest maps,
pseudonymized with ``opts.principal_key`` and dropped. Whether the report lists every seat holder
(also never-active ones) is unverified (§19.5 #31): every result carries the note
``dq.copilot_activity_report_listed`` ("seat holders as listed").
"""

from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from tokenbill.core.errors import SourceError, UsageError
from tokenbill.core.ids import pseudonym, stable_id
from tokenbill.core.records import LicenseSnapshot
from tokenbill.core.types import (
    DataQualityNote,
    IngestOptions,
    IngestResult,
    QuarantineItem,
    SourceInfo,
)

__all__ = ["ADAPTER_NAME", "SOURCE_KIND", "ActivityReportAdapter", "bucket", "parse_timestamp"]

ADAPTER_NAME = "github-copilot-activity-report"
SOURCE_KIND = "github.copilot_activity_report"
#: Columns the report must carry (sniff and read).
REQUIRED_COLUMNS = ("login", "last_activity_at", "last_surface_used")
#: The documented columns.
DOCUMENTED_COLUMNS = ("report_time", "login", "last_authenticated_at", "last_activity_at",
                      "last_surface_used")
DQ_LISTED = "dq.copilot_activity_report_listed"
DQ_DUPLICATE = "dq.copilot_activity_report_duplicate_login"
DQ_NO_REPORT_TIME = "dq.copilot_activity_report_no_report_time"
_UNSPECIFIED = frozenset({"", "unspecified", "none", "n/a"})
_MAX_BYTES = 256 * 2**20
_DAY_MS = 86_400_000
_EPOCH = _dt.date(1970, 1, 1)

_ISO_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,]\d{1,9})?)?)?"
    r"\s*(Z|UTC|GMT|[+-]\d{2}:?\d{2})?\Z", re.IGNORECASE)
_US_RE = re.compile(
    r"(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})(?:[ T,]+(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(AM|PM)?)?"
    r"\s*(Z|UTC|GMT)?\Z", re.IGNORECASE)


def _offset(text: str | None) -> _dt.timedelta:
    if not text or text.upper() in ("Z", "UTC", "GMT"):
        return _dt.timedelta(0)
    sign = -1 if text[0] == "-" else 1
    digits = text[1:].replace(":", "")
    hours, minutes = int(digits[:2]), int(digits[2:])
    if hours > 23 or minutes > 59:
        raise ValueError("bad offset")
    return sign * _dt.timedelta(hours=hours, minutes=minutes)


def parse_timestamp(text: str) -> _dt.date | None:
    """The UTC date of an activity-report timestamp (ISO 8601 with ``Z`` / ``UTC`` / an offset, or
    ``M/D/YYYY`` / ``M/D/YY`` with an optional ``H:MM[:SS] [AM|PM]`` time); None for an empty cell;
    ``ValueError`` for anything else."""
    value = text.strip()
    if not value:
        return None
    try:
        return _parse(value)
    except OverflowError:
        raise ValueError("timestamp out of range") from None


def _parse(value: str) -> _dt.date:
    m = _ISO_RE.match(value)
    if m:
        year, month, day = int(m[1]), int(m[2]), int(m[3])
        hour, minute, second = int(m[4] or 0), int(m[5] or 0), int(m[6] or 0)
        local = _dt.datetime(year, month, day, hour, minute, second)
        return (local - _offset(m[7])).date()
    m = _US_RE.match(value)
    if m:
        month, day, year = int(m[1]), int(m[2]), int(m[3])
        if year < 100:
            year += 2000
        hour, minute, second = int(m[4] or 0), int(m[5] or 0), int(m[6] or 0)
        if m[7]:
            if not 1 <= hour <= 12:
                raise ValueError("bad 12-hour time")
            hour = hour % 12 + (12 if m[7].upper() == "PM" else 0)
        return _dt.datetime(year, month, day, hour, minute, second).date()
    raise ValueError("not a timestamp")


def bucket(report_day: _dt.date, day: _dt.date | None) -> str:
    """The recency bucket of *day* seen from *report_day*: ``0-7``, ``8-30``, ``31-90`` days, else
    (or no timestamp) ``none_90d``; a timestamp after the report counts as ``0-7``."""
    if day is None:
        return "none_90d"
    days = (report_day - day).days
    if days <= 7:
        return "0-7"
    if days <= 30:
        return "8-30"
    if days <= 90:
        return "31-90"
    return "none_90d"


def _header(head: bytes) -> list[str]:
    text = head.decode("utf-8", errors="replace").lstrip("\ufeff")
    first = text.splitlines()[0] if text else ""
    try:
        row = next(csv.reader([first]), [])
    except csv.Error:
        return []
    return [c.strip().lower() for c in row]


@dataclass
class _Row:
    line: int
    login: str
    report_day: _dt.date
    activity: _dt.date | None
    authenticated: _dt.date | None
    auth_known: bool
    surface: str | None


class ActivityReportAdapter:
    """Reads the Copilot activity report CSV into ``LicenseSnapshot`` records (capability
    ``licenses``); logins are pseudonymized at read time and never kept."""

    name = ADAPTER_NAME
    capabilities = frozenset({"licenses"})

    def sniff(self, path: Path, head: bytes) -> bool:
        """A CSV whose header has ``login``, ``last_activity_at`` and ``last_surface_used``."""
        cols = set(_header(head))
        return all(c in cols for c in REQUIRED_COLUMNS)

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Parse the report; malformed rows are quarantined (``missing:login``,
        ``bad_type:<column>``) or, with ``opts.lenient`` false, raise ``SourceError`` naming the
        file and line only."""
        if not isinstance(opts, IngestOptions):
            raise UsageError(f"{ADAPTER_NAME}: read() needs IngestOptions")
        if not opts.principal_key:
            raise UsageError(f"{ADAPTER_NAME}: a principal key is required (logins are "
                             "pseudonymized at read time)")
        path = Path(path)
        try:
            raw = path.read_bytes()
        except OSError:
            raise SourceError(f"{path.name}: unreadable") from None
        if len(raw) > _MAX_BYTES:
            raise SourceError(f"{path.name}: larger than 256 MiB")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise SourceError(f"{path.name}: not UTF-8 text") from None
        state = _ReadState(self, path, opts)
        state.parse(text)
        return state.result(raw)


class _ReadState:
    def __init__(self, adapter: ActivityReportAdapter, path: Path, opts: IngestOptions) -> None:
        self.path = path
        self.opts = opts
        self.quarantined: list[QuarantineItem] = []
        self.rows: dict[str, _Row] = {}
        self.stats = {"rows": 0, "records": 0, "quarantined": 0, "duplicates": 0,
                      "outside_window": 0, "unknown_columns": 0}
        name_key = opts.name_key
        self.source_id = (pseudonym(name_key, "s", f"{adapter.name}:{path.name}") if name_key
                          else stable_id("s", adapter.name, path.name))
        self.name_hmac = pseudonym(name_key, "h", path.name) if name_key else ""
        self.no_report_time = False
        self.team_map = dict(opts.team_map)
        self.team_folded = {k.lower(): v for k, v in sorted(self.team_map.items())}
        self.cc_map = dict(opts.cost_center_map)
        self.cc_folded = {k.lower(): v for k, v in sorted(self.cc_map.items())}

    def quarantine(self, line: int, reason: str) -> None:
        if not self.opts.lenient:
            raise SourceError(f"{self.path.name}: line {line}: {reason}")
        self.quarantined.append(QuarantineItem(source_id=self.source_id, locator=f"line {line}",
                                               reason=reason))
        self.stats["quarantined"] += 1

    def parse(self, text: str) -> None:
        reader = csv.reader(io.StringIO(text))
        try:
            header = next(reader, None)
            if header is None:
                raise SourceError(f"{self.path.name}: empty file")
            cols = [c.strip().lower() for c in header]
            missing = [c for c in REQUIRED_COLUMNS if c not in cols]
            if missing:
                raise SourceError(f"{self.path.name}: not an activity report (missing columns)")
            index = {c: cols.index(c) for c in DOCUMENTED_COLUMNS if c in cols}
            self.stats["unknown_columns"] = sum(1 for c in cols if c not in DOCUMENTED_COLUMNS)
            self.no_report_time = "report_time" not in index
            for row in reader:
                line = reader.line_num
                if not row or not any(cell.strip() for cell in row):
                    continue
                self.stats["rows"] += 1
                self._row(line, row, index)
        except csv.Error:
            raise SourceError(f"{self.path.name}: line {reader.line_num}: malformed CSV") from None

    def _cell(self, row: list[str], index: Mapping[str, int], column: str) -> str:
        i = index.get(column)
        return row[i].strip() if i is not None and i < len(row) else ""

    def _row(self, line: int, row: list[str], index: Mapping[str, int]) -> None:
        login = self._cell(row, index, "login")
        if not login:
            self.quarantine(line, "missing:login")
            return
        if self.no_report_time:
            if self.opts.now_ms <= 0:
                self.quarantine(line, "missing:report_time")
                return
            report_day = _EPOCH + _dt.timedelta(days=self.opts.now_ms // _DAY_MS)
        else:
            try:
                report = parse_timestamp(self._cell(row, index, "report_time"))
            except ValueError:
                report = None
            if report is None:
                self.quarantine(line, "bad_type:report_time")
                return
            report_day = report
        try:
            activity = parse_timestamp(self._cell(row, index, "last_activity_at"))
        except ValueError:
            self.quarantine(line, "bad_type:last_activity_at")
            return
        auth_known = "last_authenticated_at" in index
        try:
            authenticated = parse_timestamp(self._cell(row, index, "last_authenticated_at"))
        except ValueError:
            self.quarantine(line, "bad_type:last_authenticated_at")
            return
        day_ms = (report_day - _EPOCH).days * _DAY_MS
        if ((self.opts.since_ms is not None and day_ms + _DAY_MS <= self.opts.since_ms)
                or (self.opts.until_ms is not None and day_ms >= self.opts.until_ms)):
            self.stats["outside_window"] += 1
            return
        surface_raw = self._cell(row, index, "last_surface_used")
        surface = None if surface_raw.lower() in _UNSPECIFIED else _family(surface_raw)
        new = _Row(line, login, report_day, activity, authenticated, auth_known, surface)
        key = f"{login.lower()}\x1f{report_day.isoformat()}"
        cur = self.rows.get(key)
        if cur is not None:
            self.stats["duplicates"] += 1
            if _recency(new) <= _recency(cur):
                return
        self.rows[key] = new

    def _team(self, login: str) -> tuple[str | None, str | None]:
        team = self.team_map.get(login) or self.team_folded.get(login.lower())
        cc = self.cc_map.get(login) or self.cc_folded.get(login.lower())
        return team, cc

    def result(self, raw: bytes) -> IngestResult:
        key = self.opts.principal_key or b""
        org = self.opts.attribution.workspace_id or None
        licenses: list[LicenseSnapshot] = []
        for row in sorted(self.rows.values(), key=lambda r: r.line):
            team, cc = self._team(row.login)
            licenses.append(LicenseSnapshot(
                snapshot_date=row.report_day.isoformat(), product="github_copilot",
                plan="unknown", principal=pseudonym(key, "p", row.login.strip().lower()), team=team,
                cost_center=cc, org=org, seat_created=None, pending_cancellation=None,
                last_activity_bucket=bucket(row.report_day, row.activity),
                last_activity_surface=row.surface,
                last_authenticated_bucket=(bucket(row.report_day, row.authenticated)
                                           if row.auth_known else "unknown"),
                assigned_via_team=None, fetched_ms=self.opts.now_ms, source_kind=SOURCE_KIND))
        licenses.sort(key=lambda x: (x.snapshot_date, x.principal))
        self.stats["records"] = len(licenses)
        notes = []
        if licenses:
            notes.append(DataQualityNote(
                code=DQ_LISTED, severity="info", count=len(licenses),
                detail="activity report: seat holders as listed (whether never-active seats are "
                       "listed is unverified); plan and seat assignment unknown"))
        if self.stats["duplicates"]:
            notes.append(DataQualityNote(code=DQ_DUPLICATE, severity="warn",
                                         count=self.stats["duplicates"],
                                         detail="a login listed twice for one report date; "
                                                "the most recent activity kept"))
        if self.no_report_time and licenses:
            notes.append(DataQualityNote(code=DQ_NO_REPORT_TIME, severity="warn",
                                         count=len(licenses),
                                         detail="no report_time column: buckets use the run date"))
        if self.quarantined:
            notes.append(DataQualityNote(code="dq.quarantined", severity="warn",
                                         count=len(self.quarantined),
                                         detail="activity report rows quarantined"))
        if self.stats["unknown_columns"]:
            notes.append(DataQualityNote(code="dq.unknown_fields", severity="info",
                                         count=self.stats["unknown_columns"],
                                         detail="undocumented activity report columns ignored"))
        source = SourceInfo(
            source_id=self.source_id, adapter=ADAPTER_NAME, name_hmac=self.name_hmac,
            sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw),
            name_key_id=self.opts.name_key_id if self.name_hmac else None,
            principal_key_id=self.opts.principal_key_id if licenses else None)
        return IngestResult(
            source=source, requests=[], sessions=[], events=[], aggregates=[], cost_lines=[],
            outcomes=[], quarantined=self.quarantined, notes=notes,
            stats=dict(sorted(self.stats.items())),
            capabilities=frozenset({"licenses"}) if licenses else frozenset(),
            licenses=licenses)


def _recency(row: _Row) -> tuple:
    return (row.activity or _dt.date.min, row.authenticated or _dt.date.min,
            row.surface is not None, -row.line)


def _family(raw: str) -> str:
    from tokenbill.core.catalog import editor_family  # facts-backed (the one shared mapping)

    return editor_family(raw)
