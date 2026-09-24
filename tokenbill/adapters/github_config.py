"""GitHub Copilot configuration adapter and the shared org-data reader (addendum §5.4; CP-ORGDATA).

:class:`CopilotConfigAdapter` (registry name ``github-copilot-config``) turns GitHub's budgets
(``GET /enterprises/{e}/settings/billing/budgets`` and ``…/budgets/{id}``, both SKU field spellings,
the three budget types, integer or ``1000.0`` amounts), budget user-states
(``…/budgets/{id}/user-states``, summarized at ingest), cost centers (``…/cost-centers`` and
``…/cost-centers/{id}``) and the organization Copilot billing object (``GET
/orgs/{org}/copilot/billing``: ``plan_type``, ``seat_management_setting``, ``seat_breakdown``) into
content-free :class:`~tokenbill.core.records.ConfigSnapshot` records.

Privacy (addendum R14, §8.1): a user-scope budget keeps only the **team and cost center** of its
user (from ``opts.team_map`` / ``opts.cost_center_map``) — never the login or a ``p_``; alert
recipients become a count; user-states become counts and, with at least ``opts.k_anonymity``
users, the p50 / p90 consumption; cost centers keep resource counts, never resource names;
repository budget targets are ``h_`` pseudonyms under the name key.

The module also holds the machinery the other CP-ORGDATA adapters share (``github_metrics``,
``github_seats``, ``github_agent_tasks``, ``github_usage_records``) and ``copilot.teammap``: the
document reader (:meth:`OrgRead.items`: one JSON document, a JSON array, NDJSON / JSONL, ``.gz``, a
directory of such files, CP-PULL envelopes ``{"request": {path, query}, "response": …}`` whose
response may be the body or ``{status, headers, body}``), the login normalization
(:func:`login_key`), identity helpers (team / cost-center maps, ``p_`` under the principal key,
``h_`` under the name key), strict number, date and timestamp parsers and the
:class:`~tokenbill.core.types.IngestResult` builder. Money is never a float: JSON numbers are exact
``Decimal`` values (``core.jsonl``) and amounts go through ``core.money`` once.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from tokenbill.core.catalog import copilot_cost_type
from tokenbill.core.errors import SourceError, UsageError
from tokenbill.core.ids import pseudonym, stable_id
from tokenbill.core.jsonl import iter_lines, load_json_exact, parse_json_line
from tokenbill.core.money import usd_str_to_nano
from tokenbill.core.records import (
    MAX_TOKENS,
    ActivityDay,
    ConfigSnapshot,
    ContentTier,
    LicenseSnapshot,
    OutcomeAggregate,
    UsageAggregate,
    record_key,
    to_json,
)
from tokenbill.core.types import (
    DataQualityNote,
    IngestOptions,
    IngestResult,
    QuarantineItem,
    SourceInfo,
)

__all__ = [
    "BUDGET_SCOPES",
    "BUDGET_TYPES",
    "DAY_MS",
    "OTHER_TEAM",
    "PRODUCT",
    "SEAT_MANAGEMENT_SETTINGS",
    "UNMAPPED_TEAM",
    "BadRecord",
    "CopilotConfigAdapter",
    "Item",
    "OrgRead",
    "count_of",
    "day_of_ms",
    "day_start_ms",
    "decimal_of",
    "head_keys",
    "head_request_path",
    "iter_documents",
    "keep_latest",
    "login_key",
    "natural_agg_id",
    "parse_day",
    "parse_ts_ms",
    "safe_label",
    "safe_token",
]

#: ``product`` of every Copilot seat / activity record.
PRODUCT = "github_copilot"
#: Team label of people the team map does not know (outcome rows, aggregate dims).
UNMAPPED_TEAM = "(unmapped)"
#: Team label of merged small groups (``core.kanon.merge_small_groups``).
OTHER_TEAM = "(other)"
DAY_MS = 86_400_000
#: Documented ``budget_scope`` values (ghec.json, 2026-09-23).
BUDGET_SCOPES = ("enterprise", "organization", "repository", "cost_center", "multi_user_customer",
                 "multi_user_cost_center", "user")
#: Documented ``budget_type`` values (ghec.json schema + example; addendum §19.3 #12).
BUDGET_TYPES = ("BundlePricing", "ProductPricing", "SkuPricing")
#: Documented ``seat_management_setting`` values of ``GET /orgs/{org}/copilot/billing``.
SEAT_MANAGEMENT_SETTINGS = ("assign_all", "assign_selected", "disabled", "unconfigured")
_POLICY_VALUES = ("enabled", "disabled", "unconfigured")
_ORG_PLANS = ("business", "enterprise")
_CC_STATES = ("active", "deleted")
_SEAT_BREAKDOWN = (("total", "seats_total"), ("added_this_cycle", "seats_added_this_cycle"),
                   ("pending_cancellation", "seats_pending_cancellation"),
                   ("pending_invitation", "seats_pending_invitation"),
                   ("active_this_cycle", "seats_active_this_cycle"),
                   ("inactive_this_cycle", "seats_inactive_this_cycle"))
#: Cost-center resource types → count attr (case-insensitive; the docs show User / Team / Repo).
_RESOURCE_COUNTS = {"user": "n_users", "team": "n_teams", "org": "n_orgs",
                    "organization": "n_orgs", "repo": "n_repos", "repository": "n_repos"}
#: SKU cost types whose budget amount is a license count, not dollars (ghec.json: "For
#: license-based products, this represents the number of licenses").
_LICENSE_COST_TYPES = frozenset({"seat", "code_quality.license"})

MAX_LABEL = 128
_EPOCH = _dt.date(1970, 1, 1)
_MAX_TS_MS = (_dt.date(9999, 12, 31) - _EPOCH).days * DAY_MS + DAY_MS
_CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_DAY_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})\Z")
_TS_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})"
    r"(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,12}))?)?)?"
    r"\s*(Z|z|UTC|[+-]\d{2}(?::?\d{2})?)?\Z")
_HEAD_KEY_RE = re.compile(rb'"([A-Za-z0-9_@./-]{1,64})"\s*:')
_HEAD_PATH_RE = re.compile(
    rb'"request"\s*:\s*\{(?:[^{}]|\{[^{}]{0,1024}\}){0,4096}?"(?:path|url)"\s*:\s*'
    rb'"([^"\\]{1,512})"')
_DATA_SUFFIXES = (".json", ".jsonl", ".ndjson")


# ---------------------------------------------------------------------------------------------
# small strict parsers (every failure is a content-free BadRecord)
# ---------------------------------------------------------------------------------------------


class BadRecord(SourceError):
    """A malformed record; ``reason`` is the content-free quarantine code (SPEC §5.1). It subclasses
    :class:`SourceError`, so should one escape, callers still see a ``TokenbillError``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def login_key(login: object) -> str | None:
    """The normalized form of a GitHub login every CP-ORGDATA adapter pseudonymizes and looks up:
    stripped and lower-cased (GitHub logins are case-insensitive ASCII); None for a non-string,
    blank or over-long value. The ``p_`` of a login is
    ``core.ids.pseudonym(principal_key, "p", login_key(login))``."""
    if not isinstance(login, str):
        return None
    text = login.strip()
    if not text or len(text) > 256 or _CONTROL_RE.search(text):
        return None
    return text.lower()


def safe_label(value: object, *, max_len: int = MAX_LABEL) -> str | None:
    """An organizational label (org login, cost-center or team name): stripped, control and
    bidi-format characters replaced by ``_``; None when not a string, blank or longer than
    *max_len* (a longer value is not a label)."""
    if not isinstance(value, str):
        return None
    text = _CONTROL_RE.sub("_", value).strip()
    if not text or len(text) > max_len:
        return None
    return text


def safe_token(value: object) -> str | None:
    """A provider id or enum token (``[A-Za-z0-9][A-Za-z0-9._-]{0,127}``), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    if isinstance(value, str) and _TOKEN_RE.match(value.strip()):
        return value.strip()
    return None


def decimal_of(value: object, field_name: str) -> Decimal:
    """An exact finite ``Decimal`` from a JSON int or ``Decimal`` (never a float or bool)."""
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise BadRecord(f"bad_type:{field_name}")
    d = Decimal(value)
    if not d.is_finite() or (d and d.adjusted() > 30):
        raise BadRecord(f"bad_type:{field_name}")
    return d


def count_of(value: object, field_name: str) -> int:
    """A count: an int (or an integral ``Decimal`` such as ``12.0``) in ``[0, 2**53]``."""
    d = decimal_of(value, field_name)
    if d != d.to_integral_value() or d < 0 or d > MAX_TOKENS:
        raise BadRecord(f"bad_type:{field_name}")
    return int(d)


def _nano_of_usd(value: object, field_name: str) -> int:
    d = decimal_of(value, field_name)
    if d < 0:
        raise BadRecord(f"bad_type:{field_name}")
    try:
        return usd_str_to_nano(str(d))[0]
    except ValueError:
        raise BadRecord(f"bad_type:{field_name}") from None


def _credits_attr(value: object, field_name: str) -> int | str | None:
    """An AI-credit amount as an attr value: an int when integral, else the exact decimal string."""
    if value is None:
        return None
    d = decimal_of(value, field_name)
    if d == d.to_integral_value():
        return int(d)
    return format(d.normalize(), "f")


def parse_day(value: object, field_name: str) -> str:
    """A ``YYYY-MM-DD`` calendar date (validated)."""
    if not isinstance(value, str) or not _DAY_RE.match(value.strip()):
        raise BadRecord(f"bad_type:{field_name}")
    try:
        return _dt.date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        raise BadRecord(f"bad_type:{field_name}") from None


def day_start_ms(day: str) -> int:
    """Epoch milliseconds of 00:00 UTC of a ``YYYY-MM-DD`` date."""
    return (_dt.date.fromisoformat(day) - _EPOCH).days * DAY_MS


def day_of_ms(ms: int) -> str:
    """The UTC ``YYYY-MM-DD`` date of epoch milliseconds."""
    return (_EPOCH + _dt.timedelta(days=ms // DAY_MS)).isoformat()


def parse_ts_ms(value: object, field_name: str) -> int:
    """Epoch milliseconds (UTC) of an ISO-8601 timestamp or date (``Z`` or ``±hh:mm`` offsets,
    fractions truncated to milliseconds); inside 1970 … 9999, else ``BadRecord``."""
    if not isinstance(value, str):
        raise BadRecord(f"bad_type:{field_name}")
    m = _TS_RE.match(value.strip())
    if m is None:
        raise BadRecord(f"bad_type:{field_name}")
    y, mo, d, hh, mi, ss, frac, tz = m.groups()
    try:
        days = (_dt.date(int(y), int(mo), int(d)) - _EPOCH).days
        h, mnt, sec = int(hh or 0), int(mi or 0), int(ss or 0)
        _dt.time(h, mnt, sec)
    except ValueError:
        raise BadRecord(f"bad_type:{field_name}") from None
    ms = days * DAY_MS + ((h * 60 + mnt) * 60 + sec) * 1000 + int((frac or "0")[:3].ljust(3, "0"))
    if tz and tz not in ("Z", "z", "UTC"):
        sign = -1 if tz[0] == "-" else 1
        digits = tz[1:].replace(":", "")
        off_h, off_m = int(digits[:2]), int(digits[2:4] or 0)
        if off_h > 23 or off_m > 59:
            raise BadRecord(f"bad_type:{field_name}")
        ms -= sign * (off_h * 60 + off_m) * 60_000
    if not 0 <= ms < _MAX_TS_MS:
        raise BadRecord(f"bad_type:{field_name}")
    return ms


def _bool_or_none(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _enum(value: object, allowed: tuple[str, ...], ctx: OrgRead) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value in allowed:
        return value
    ctx.stat("unknown_enum_values")
    return None


# ---------------------------------------------------------------------------------------------
# sniffing helpers
# ---------------------------------------------------------------------------------------------


def head_keys(head: bytes) -> frozenset[str]:
    """The JSON object keys that occur in *head* (a byte prefix; no parse needed)."""
    return frozenset(m.decode("ascii") for m in _HEAD_KEY_RE.findall(head[:64 * 1024]))


def head_request_path(head: bytes) -> str | None:
    """The request path of a recorded CP-PULL envelope in *head* (``request.path`` or
    ``request.url``, host and query removed), or None."""
    m = _HEAD_PATH_RE.search(head[:64 * 1024])
    if m is None:
        return None
    return _split_path(m.group(1).decode("utf-8", "replace"))[0]


def _split_path(raw: str) -> tuple[str, dict[str, str]]:
    parts = urlsplit(raw.strip())
    path = parts.path if (parts.scheme or parts.netloc) else raw.strip().split("?", 1)[0]
    query = dict(parse_qsl(parts.query)) if parts.query else {}
    return "/" + path.strip("/"), query


# ---------------------------------------------------------------------------------------------
# the per-read context: documents, quarantine, notes, identity, result
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    """One JSON value of a source: a page, a record or an envelope's body. ``path`` / ``query``
    are the recorded request's (never stored: they may name a login or a repository)."""

    locator: str
    body: Any
    path: str | None = None
    query: Mapping[str, str] = field(default_factory=dict)
    fetched_ms: int | None = None


def _source_files(path: Path) -> list[Path]:
    path = Path(path)
    if path.is_dir():
        return sorted(p for p in path.rglob("*") if p.is_file() and not p.name.startswith(".")
                      and p.name.lower() != "manifest.json" and _is_data_file(p.name))
    if not path.exists():
        raise SourceError(f"{path.name}: not found")
    return [path]


def _is_data_file(name: str) -> bool:
    low = name.lower()
    for comp in (".gz", ".zst"):
        if low.endswith(comp):
            low = low[: -len(comp)]
    return low.endswith(_DATA_SUFFIXES)


def _fetched_of(obj: Mapping[str, Any]) -> int | None:
    value = obj.get("fetched_ms")
    if type(value) is int and 0 <= value < _MAX_TS_MS:
        return value
    try:
        return parse_ts_ms(obj.get("fetched_at"), "fetched_at") if "fetched_at" in obj else None
    except BadRecord:
        return None


class OrgRead:
    """Per-read state of a CP-ORGDATA adapter (adapters themselves stay stateless)."""

    def __init__(self, adapter: str, path: Path, opts: IngestOptions) -> None:
        if not isinstance(opts, IngestOptions):
            raise UsageError("read() needs IngestOptions")
        if str(opts.content_tier) != ContentTier.NONE.value:
            raise UsageError(f"{adapter}: GitHub Copilot sources are read in content tier "
                             "none only")
        if not opts.name_key:
            raise UsageError(f"{adapter}: a name key is required (ids are pseudonymized)")
        self.adapter = adapter
        self.path = Path(path)
        self.opts = opts
        self.files = _source_files(self.path)
        self.source_id = pseudonym(opts.name_key, "s", f"{adapter}:{self.path.name}")
        self.stats: dict[str, int] = {"files": len(self.files), "records": 0}
        self.quarantined: list[QuarantineItem] = []
        self._notes: dict[tuple[str, str, str], int] = {}
        self.used_principal_key = False
        self._team = dict(opts.team_map)
        self._team_folded = {k.casefold(): v for k, v in sorted(self._team.items())}
        self._cc = dict(opts.cost_center_map)
        self._cc_folded = {k.casefold(): v for k, v in sorted(self._cc.items())}

    # ----- documents --------------------------------------------------------------------------

    def items(self) -> Iterator[Item]:
        """Every JSON value of the source, envelopes unwrapped (bad lines are quarantined)."""
        multi = len(self.files) > 1
        for i, f in enumerate(self.files):
            prefix = f"f{i}:" if multi else ""
            yield from self._file_items(f, prefix)

    def _file_items(self, f: Path, prefix: str) -> Iterator[Item]:
        lines = iter_lines(f)
        try:
            first = next(lines, None)
        except SourceError:  # corrupt compressed stream
            self.quarantine(f"{prefix}file", "unreadable")
            return
        if first is None:
            return
        if first[2] and parse_json_line(first[2], exact_numbers=True) is None:
            # not one object per line: one JSON document (an object or an array)
            try:
                doc = load_json_exact(f)
            except SourceError:
                if not self._has_object_line(f):
                    self.quarantine(f"{prefix}file", "bad_json")
                    return
                # NDJSON whose first line is broken: read it line by line after all
                yield from self._line_items(iter_lines(f), None, prefix)
                return
            if isinstance(doc, list):
                for j, value in enumerate(doc):
                    yield from self._unwrap(value, f"{prefix}item:{j}")
            else:
                yield from self._unwrap(doc, f"{prefix}doc")
            return
        yield from self._line_items(lines, first, prefix)

    def _has_object_line(self, f: Path) -> bool:
        try:
            return any(raw and parse_json_line(raw, exact_numbers=True) is not None
                       for _, _, raw in iter_lines(f))
        except SourceError:
            return False

    def _line_items(self, lines: Iterator[tuple[int, int, bytes]],
                    first: tuple[int, int, bytes] | None, prefix: str) -> Iterator[Item]:
        pending = first
        last = 0
        while True:
            if pending is None:
                try:
                    pending = next(lines, None)
                except SourceError:  # corrupt compressed stream mid-file
                    self.quarantine(f"{prefix}line:{last + 1}", "unreadable")
                    return
                if pending is None:
                    return
            line_no, _, raw = pending
            pending, last = None, line_no
            loc = f"{prefix}line:{line_no}"
            if not raw:
                self.quarantine(loc, "oversize_line")
                continue
            obj = parse_json_line(raw, exact_numbers=True)
            if obj is None:
                self.quarantine(loc, "not_object" if raw.lstrip()[:1] == b"[" else "bad_json")
                continue
            yield from self._unwrap(obj, loc)

    def _unwrap(self, value: Any, locator: str) -> Iterator[Item]:
        if not isinstance(value, dict):
            self.quarantine(locator, "not_object")
            return
        req = value.get("request")
        if isinstance(req, dict) and "response" in value:  # CP-PULL envelope
            raw_path = req.get("path") or req.get("url")
            path, query = _split_path(raw_path) if isinstance(raw_path, str) else (None, {})
            q = req.get("query")
            if isinstance(q, dict):
                query = {**query, **{k: str(v) for k, v in q.items() if isinstance(k, str)}}
            body = value["response"]
            fetched = _first_not_none(_fetched_of(value), _fetched_of(req))
            if isinstance(body, dict) and "body" in body and (
                    "status" in body or "headers" in body):
                fetched = _first_not_none(fetched, _fetched_of(body))
                body = body["body"]
            if isinstance(body, str):
                try:
                    body = _loads_exact(body)
                except BadRecord:
                    self.quarantine(locator, "bad_json")
                    return
            yield Item(locator, body, path, query, fetched)
            return
        hint = next((value[k] for k in ("endpoint", "url", "path")
                     if isinstance(value.get(k), str)), None)
        body_key = next((k for k in ("response", "page") if isinstance(value.get(k), (dict, list))),
                        None)
        if hint is not None and body_key is not None:  # {endpoint|url|path, response|page}
            path, query = _split_path(hint)
            yield Item(locator, value[body_key], path, query, _fetched_of(value))
            return
        yield Item(locator, value)

    # ----- quarantine, notes, stats -----------------------------------------------------------

    def quarantine(self, locator: str, reason: str) -> None:
        """Quarantine a malformed record (content-free reason); strict mode raises."""
        if not self.opts.lenient:
            raise SourceError(f"{self.path.name}: {locator}: {reason}")
        self.quarantined.append(QuarantineItem(source_id=self.source_id, locator=locator,
                                               reason=reason))

    def note(self, code: str, severity: str, detail: str, count: int = 1) -> None:
        """Accumulate a data-quality note (same code, severity and detail add up)."""
        key = (code, severity, detail)
        self._notes[key] = self._notes.get(key, 0) + count

    def stat(self, key: str, n: int = 1) -> None:
        """Add *n* to ``stats[key]``."""
        self.stats[key] = self.stats.get(key, 0) + n

    # ----- identity and time ------------------------------------------------------------------

    def principal(self, login: object) -> str:
        """The ``p_`` pseudonym of a GitHub login under the principal key (the org key, or the
        admin's export key in a handoff); the login itself is never kept."""
        key = login_key(login)
        if key is None:
            raise BadRecord("missing:login")
        opts = self.opts
        if opts.principal_key is None or not opts.principal_key_id:
            raise UsageError(f"{self.adapter}: a principal key is required (logins are "
                             "pseudonymized at ingest)")
        if opts.identity_mode not in ("central-ingest", "install"):
            raise UsageError(f"{self.adapter}: GitHub org data needs identity mode "
                             "central-ingest (or install for a self-view)")
        self.used_principal_key = True
        return pseudonym(opts.principal_key, "p", key)

    def _lookup(self, exact: Mapping[str, str], folded: Mapping[str, str],
                refs: Iterable[object]) -> str | None:
        for ref in refs:
            if isinstance(ref, bool):
                continue
            if isinstance(ref, int):
                ref = str(ref)
            if not isinstance(ref, str) or not ref.strip():
                continue
            hit = exact.get(ref.strip())
            if hit is None:
                hit = folded.get(ref.strip().casefold())
            if hit is not None:
                return safe_label(hit)
        return None

    def team(self, *refs: object) -> str | None:
        """``opts.team_map`` lookup (login, then user id; exact, then case-folded) or None."""
        return self._lookup(self._team, self._team_folded, refs)

    def cost_center(self, *refs: object) -> str | None:
        """``opts.cost_center_map`` lookup like :meth:`team`, or None."""
        return self._lookup(self._cc, self._cc_folded, refs)

    def name(self, value: str) -> str:
        """``h_`` pseudonym of an identifier under the name key (repositories, workflow paths)."""
        return pseudonym(self.opts.name_key, "h", value)

    def in_window(self, ms: int) -> bool:
        """``opts.since_ms <= ms < opts.until_ms`` (unbounded when unset)."""
        o = self.opts
        return (o.since_ms is None or ms >= o.since_ms) and (o.until_ms is None
                                                             or ms < o.until_ms)

    def fetched(self, item: Item) -> int:
        """The fetch time of an item: the envelope's, else ``opts.now_ms``."""
        return item.fetched_ms if item.fetched_ms is not None else self.opts.now_ms

    # ----- result -----------------------------------------------------------------------------

    def source_info(self) -> SourceInfo:
        """Content-free identity of the source: HMACs of its name, a digest of its bytes."""
        digest = hashlib.sha256()
        total = 0
        for f in self.files:
            part = hashlib.sha256()
            try:
                with open(f, "rb") as fh:
                    for chunk in iter(lambda fh=fh: fh.read(1 << 20), b""):
                        part.update(chunk)
                        total += len(chunk)
            except OSError as exc:
                raise SourceError(f"{f.name}: unreadable ({type(exc).__name__})") from None
            if len(self.files) == 1:
                digest = part
            else:
                digest.update(f.relative_to(self.path).as_posix().encode("utf-8", "replace"))
                digest.update(b"\0" + part.digest())
        return SourceInfo(
            source_id=self.source_id, adapter=self.adapter,
            name_hmac=pseudonym(self.opts.name_key, "h", self.path.name),
            sha256=digest.hexdigest(), bytes=total, name_key_id=self.opts.name_key_id,
            principal_key_id=self.opts.principal_key_id if self.used_principal_key else None)

    def result(self, declared: frozenset[str], *, licenses: Iterable[LicenseSnapshot] = (),
               activity: Iterable[ActivityDay] = (), config: Iterable[ConfigSnapshot] = (),
               aggregates: Iterable[UsageAggregate] = (),
               outcomes: Iterable[OutcomeAggregate] = ()) -> IngestResult:
        """The deterministic :class:`IngestResult`; capabilities are those actually present."""
        lic = sorted(licenses, key=record_key)
        act = sorted(activity, key=record_key)
        cfg = sorted(config, key=record_key)
        aggs = sorted(aggregates, key=lambda a: a.agg_id)
        outs = sorted(outcomes, key=lambda o: (o.date_utc, o.source_kind, o.team))
        present = {"licenses": lic, "activity": act, "config": cfg, "aggregates": aggs,
                   "outcomes": outs}
        caps = frozenset(k for k, v in present.items() if v) & declared
        self.stats.update(licenses=len(lic), activity=len(act), config=len(cfg),
                          aggregates=len(aggs), outcomes=len(outs),
                          quarantined=len(self.quarantined))
        notes = [DataQualityNote(code=c, severity=s, count=n, detail=d)
                 for (c, s, d), n in sorted(self._notes.items())]
        return IngestResult(
            source=self.source_info(), requests=[], sessions=[], events=[], aggregates=aggs,
            cost_lines=[], outcomes=outs, quarantined=list(self.quarantined), notes=notes,
            stats=dict(sorted(self.stats.items())), capabilities=caps, licenses=lic,
            activity=act, config=cfg)


def iter_documents(path: Path) -> Iterator[Item]:
    """Every JSON value of a GitHub source file or directory (the :meth:`OrgRead.items` reader
    without an adapter: malformed lines are skipped). Used by ``copilot.teammap``."""
    yield from OrgRead("github-documents", Path(path),
                       IngestOptions(name_key=b"\0", name_key_id="k_documents")).items()


def _first_not_none(*values: int | None) -> int | None:
    return next((v for v in values if v is not None), None)


def _reject_constant(token: str) -> Any:
    raise ValueError("non-finite number")


def _loads_exact(text: str) -> Any:
    """``json.loads`` with exact ``Decimal`` fractions; failures → ``BadRecord("bad_json")``."""
    try:
        return json.loads(text, parse_float=Decimal, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise BadRecord("bad_json") from None


def keep_latest(table: dict[str, Any], key: str, rec: Any) -> None:
    """Keep one record per natural key: the latest ``fetched_ms``, then the canonical smallest."""
    cur = table.get(key)
    if cur is None or (rec.fetched_ms, _canon(cur)) > (cur.fetched_ms, _canon(rec)):
        table[key] = rec


def _canon(rec: Any) -> str:
    return repr(sorted(to_json(rec).items()))


# ---------------------------------------------------------------------------------------------
# github-copilot-config
# ---------------------------------------------------------------------------------------------

_STATES_PATH_RE = re.compile(r"/settings/billing/budgets/([^/]+)/user-states\Z")
_CC_PATH_RE = re.compile(r"/settings/billing/cost-centers(?:/[^/]+)?\Z")
_ORG_BILLING_RE = re.compile(r"/orgs/([^/]+)/copilot/billing\Z")
_CONFIG_PATH_RE = re.compile(
    r"(?:/settings/billing/budgets(?:/[^/]+(?:/user-states)?)?|/settings/billing/cost-centers"
    r"(?:/[^/]+)?|/orgs/[^/]+/copilot/billing)\Z")


def _config_kind(item: Item) -> str | None:
    """``budgets`` | ``budget`` | ``user_states`` | ``cost_centers`` | ``cost_center`` |
    ``org_settings`` | None (not a configuration document)."""
    body, path = item.body, item.path or ""
    if _STATES_PATH_RE.search(path) or (isinstance(body, dict) and "user_states" in body):
        return "user_states"
    if not isinstance(body, dict):
        return None
    if isinstance(body.get("budgets"), list):
        return "budgets"
    if "budget_scope" in body or ("budget_type" in body and "budget_amount" in body):
        return "budget"
    if isinstance(body.get("costCenters"), list):
        return "cost_centers"
    if isinstance(body.get("resources"), list) and "name" in body and (
            "ai_credit_pool_enabled" in body or "state" in body or _CC_PATH_RE.search(path)):
        return "cost_center"
    if "seat_breakdown" in body or "seat_management_setting" in body or _ORG_BILLING_RE.search(
            path):
        return "org_settings"
    return None


@dataclass
class _States:
    consumed: list[Decimal] = field(default_factory=list)
    at_or_over: int = 0
    total_count: int = 0
    fetched_ms: int = 0


class CopilotConfigAdapter:
    """Budgets, budget user-states, cost centers and org Copilot settings → ``ConfigSnapshot``."""

    name = "github-copilot-config"
    capabilities = frozenset({"config"})

    def sniff(self, path: Path, head: bytes) -> bool:
        """Budgets / user-states / cost-center / org-billing pages by their top-level keys, or a
        recorded envelope of one of those requests."""
        req = head_request_path(head)
        if req is not None and _CONFIG_PATH_RE.search(req):
            return True
        keys = head_keys(head)
        if "budgets" in keys and ("budget_scope" in keys or "budget_type" in keys):
            return True
        if "user_states" in keys and ("consumed_amount" in keys or "has_next_page" in keys):
            return True
        if "costCenters" in keys or ("ai_credit_pool_enabled" in keys and "resources" in keys):
            return True
        return "seat_breakdown" in keys and "seat_management_setting" in keys

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Parse every configuration document of *path* (file or directory)."""
        ctx = OrgRead(self.name, path, opts)
        out: dict[str, ConfigSnapshot] = {}
        states: dict[str, _States] = {}
        for item in ctx.items():
            kind = _config_kind(item)
            if kind is None:
                ctx.stat("skipped_documents")
                continue
            try:
                if kind == "user_states":
                    _user_states(ctx, item, states)
                    continue
                for snap in _config_snapshots(ctx, item, kind):
                    ctx.stat("records")
                    keep_latest(out, record_key(snap), snap)
            except BadRecord as exc:
                ctx.quarantine(item.locator, exc.reason)
        for budget_id, acc in sorted(states.items()):
            snap = _budget_users(ctx, budget_id, acc)
            keep_latest(out, record_key(snap), snap)
        return ctx.result(self.capabilities, config=out.values())


def _config_snapshots(ctx: OrgRead, item: Item, kind: str) -> Iterator[ConfigSnapshot]:
    body: dict[str, Any] = item.body
    fetched = ctx.fetched(item)
    if kind == "budgets":
        page_user = body.get("user")
        for i, b in enumerate(body["budgets"]):
            try:
                yield _budget(ctx, b, fetched, page_user)
            except BadRecord as exc:
                ctx.quarantine(f"{item.locator}/budgets[{i}]", exc.reason)
    elif kind == "budget":
        yield _budget(ctx, body, fetched, None)
    elif kind == "cost_centers":
        for i, c in enumerate(body["costCenters"]):
            try:
                yield _cost_center(c, fetched)
            except BadRecord as exc:
                ctx.quarantine(f"{item.locator}/costCenters[{i}]", exc.reason)
    elif kind == "cost_center":
        yield _cost_center(body, fetched)
    else:
        yield _org_settings(ctx, item, body, fetched)


def _snapshot(kind: str, source_kind: str, entity_id: str, attrs: Mapping[str, Any],
              fetched: int) -> ConfigSnapshot:
    clean = tuple(sorted((k, v) for k, v in attrs.items() if v is not None))
    try:
        return ConfigSnapshot(snapshot_ms=fetched, source_kind=source_kind, kind=kind,
                              entity_id=entity_id, attrs=clean, fetched_ms=fetched)
    except Exception:  # a ContractViolation of a hostile value: never echo it
        raise BadRecord("bad_config") from None


def _budget(ctx: OrgRead, b: object, fetched: int, page_user: object) -> ConfigSnapshot:
    if not isinstance(b, dict):
        raise BadRecord("not_object")
    budget_id = safe_token(b.get("id"))
    if budget_id is None:
        raise BadRecord("missing:id" if b.get("id") is None else "bad_type:id")
    scope = _enum(b.get("budget_scope"), BUDGET_SCOPES, ctx)
    raw_skus = b.get("budget_product_skus")
    skus = raw_skus if isinstance(raw_skus, list) else [b.get("budget_product_sku")]
    tokens = sorted({t for t in (safe_token(s) for s in skus) if t is not None})
    alerting = b.get("budget_alerting") if isinstance(b.get("budget_alerting"), dict) else {}
    recipients = alerting.get("alert_recipients")
    attrs: dict[str, Any] = {
        "scope": scope, "type": _enum(b.get("budget_type"), BUDGET_TYPES, ctx),
        "sku": ",".join(tokens) or None,
        "prevent_further_usage": _bool_or_none(b.get("prevent_further_usage")),
        "will_alert": _bool_or_none(alerting.get("will_alert")),
        "n_recipients": len(recipients) if isinstance(recipients, list) else None,
    }
    if b.get("budget_amount") is not None:
        if any(copilot_cost_type(t, username_present=True) in _LICENSE_COST_TYPES for t in tokens):
            ctx.stat("license_budget_amounts_not_stored")  # a seat count, not dollars
        else:
            attrs["amount_nano"] = _nano_of_usd(b["budget_amount"], "budget_amount")
    if b.get("expires_at") is not None:
        try:
            attrs["expires_at"] = parse_day(b["expires_at"], "expires_at")
        except BadRecord:
            ctx.stat("bad_expires_at")
    entity = safe_label(b.get("budget_entity_name"))
    if scope == "organization" and entity:
        attrs["target"] = f"org:{entity}"
    elif scope in ("cost_center", "multi_user_cost_center") and entity:
        attrs["target"] = f"cc:{entity}"
    elif scope == "repository" and entity:  # repository names are h_ under the name key
        attrs["target"] = "repo:" + ctx.name(b["budget_entity_name"].strip())
    elif scope in ("enterprise", "multi_user_customer"):
        attrs["target"] = "enterprise"
    if scope == "user":  # R14: the user's team / cost center only, never the login or a p_
        user = b.get("user") if b.get("user") is not None else page_user
    else:  # a multi-user budget page filtered by one user describes that user
        user = page_user if scope in ("multi_user_customer", "multi_user_cost_center") else None
    if scope == "user" or user is not None:
        attrs["team"] = ctx.team(user)
        attrs["cost_center"] = ctx.cost_center(user)
        if b.get("consumed_amount") is not None:
            attrs["consumed_nano"] = _nano_of_usd(b["consumed_amount"], "consumed_amount")
        ctx.stat("user_budgets")
    return _snapshot("budget", "github.budgets", f"budget:{budget_id}", attrs, fetched)


def _user_states(ctx: OrgRead, item: Item, states: dict[str, _States]) -> None:
    m = _STATES_PATH_RE.search(item.path or "")
    body = item.body
    budget_id = safe_token(m.group(1)) if m else None
    if budget_id is None and isinstance(body, dict):
        budget_id = safe_token(body.get("budget_id"))
    if budget_id is None:
        ctx.note("dq.copilot_budget_users_unattributed", "warn",
                 "budget user-states page without its request path: the budget id is unknown")
        raise BadRecord("missing:budget_id")
    rows = body.get("user_states") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        raise BadRecord("bad_type:user_states")
    acc = states.setdefault(budget_id, _States())
    acc.fetched_ms = max(acc.fetched_ms, ctx.fetched(item))
    if isinstance(body, dict) and type(body.get("total_count")) is int:
        acc.total_count = max(acc.total_count, body["total_count"])
    for i, row in enumerate(rows):
        try:
            if not isinstance(row, dict):
                raise BadRecord("not_object")
            consumed = decimal_of(row.get("consumed_amount"), "consumed_amount")
            target = row.get("target_amount")
            over = target is not None and consumed >= decimal_of(target, "target_amount")
        except BadRecord as exc:
            ctx.quarantine(f"{item.locator}/user_states[{i}]", exc.reason)
            continue
        ctx.stat("records")
        acc.consumed.append(consumed)
        acc.at_or_over += int(over)


def _nearest_rank(sorted_values: list[Decimal], pct: int) -> Decimal:
    n = len(sorted_values)
    return sorted_values[max(1, (pct * n + 99) // 100) - 1]


def _budget_users(ctx: OrgRead, budget_id: str, acc: _States) -> ConfigSnapshot:
    n = len(acc.consumed)
    attrs: dict[str, Any] = {"n_users": n, "n_at_or_over_target": acc.at_or_over}
    if n >= ctx.opts.k_anonymity:  # quantiles only with at least k users (addendum §8.1)
        values = sorted(acc.consumed)
        for pct in (50, 90):
            try:
                attrs[f"consumed_p{pct}_nano"] = usd_str_to_nano(str(_nearest_rank(values, pct)))[0]
            except ValueError:
                pass
    elif n:
        ctx.stat("budget_users_quantiles_withheld")
    if acc.total_count > n:
        ctx.stat("user_states_not_read", acc.total_count - n)
    return _snapshot("budget_users", "github.budgets", f"budget:{budget_id}", attrs,
                     acc.fetched_ms)


def _cost_center(c: object, fetched: int) -> ConfigSnapshot:
    if not isinstance(c, dict):
        raise BadRecord("not_object")
    name = safe_label(c.get("name"))
    if name is None:
        raise BadRecord("missing:name")
    counts: dict[str, int] = {}
    resources = c.get("resources") if isinstance(c.get("resources"), list) else []
    for r in resources:  # names are never read: only the resource types are counted
        attr = _RESOURCE_COUNTS.get(str(r.get("type", "")).lower()) if isinstance(r, dict) else None
        if attr is not None:
            counts[attr] = counts.get(attr, 0) + 1
    state = c.get("ai_credit_pool_state") if isinstance(c.get("ai_credit_pool_state"), dict) else {}
    attrs: dict[str, Any] = {
        "cost_center_id": safe_token(c.get("id")),
        "state": c.get("state") if c.get("state") in _CC_STATES else None,
        "pool_enabled": _bool_or_none(c.get("ai_credit_pool_enabled")),
        "pool_target_credits": _credits_attr(state.get("target_amount"), "target_amount"),
        "pool_current_credits": _credits_attr(state.get("current_amount"), "current_amount"),
        "azure": isinstance(c.get("azure_subscription"), str) and bool(
            c["azure_subscription"].strip()),
        **{k: counts.get(k, 0) for k in ("n_users", "n_teams", "n_orgs", "n_repos")},
    }
    return _snapshot("cost_center", "github.cost_centers", f"cc:{name}", attrs, fetched)


def _org_settings(ctx: OrgRead, item: Item, body: dict[str, Any], fetched: int) -> ConfigSnapshot:
    m = _ORG_BILLING_RE.search(item.path or "")
    org = safe_label(m.group(1)) if m else safe_label(ctx.opts.attribution.workspace_id)
    if org is None:
        ctx.note("dq.copilot_org_unknown", "warn", "org Copilot billing without its request path "
                 "(or --attr workspace_id=<org>): the organization is unknown")
        raise BadRecord("missing:org")
    attrs: dict[str, Any] = {
        "plan_type": _enum(body.get("plan_type"), _ORG_PLANS, ctx),
        "seat_management_setting": _enum(body.get("seat_management_setting"),
                                         SEAT_MANAGEMENT_SETTINGS, ctx),
        **{k: _enum(body.get(k), _POLICY_VALUES, ctx) for k in ("ide_chat", "platform_chat",
                                                                "cli")},
    }
    breakdown = body.get("seat_breakdown")
    if isinstance(breakdown, dict):
        for src, attr in _SEAT_BREAKDOWN:
            if breakdown.get(src) is not None:
                attrs[attr] = count_of(breakdown[src], f"seat_breakdown.{src}")
    return _snapshot("org_settings", "github.org_copilot_settings", f"org:{org}", attrs, fetched)


# Kept here so the ids of every CP-ORGDATA record derive from one helper.
def natural_agg_id(source_kind: str, *parts: str) -> str:
    """The natural id of an aggregate row (overlapping pulls of one row share it)."""
    return stable_id("ag", source_kind, *parts)
