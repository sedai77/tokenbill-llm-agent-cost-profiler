"""GitHub billing adapters: the invoice side of GitHub Copilot (addendum §5.1–§5.3, CP-BILL).

Three registry adapters read the files an enterprise admin can produce — with the report-export API
(``copilot pull``) or by clicking the **UI downloads** of the no-token handoff path (the same CSV
formats) — into exact, content-free, revision-safe ledger records:

* :class:`AiUsageReportAdapter` (``github-ai-usage``) — the AI usage report CSV (per user × day ×
  model credits, gross / discount / net, tokens) and the legacy ``premium_request`` CSV. One
  ``CostLine(source_kind="github.ai_usage_report", channel="github_copilot")`` per natural key
  (``amount_nano`` = net, ``list_amount_nano`` = gross, ``quantity`` = credits, cost type
  ``ai_credit.user`` | ``ai_credit.direct`` (empty username) | ``ai_credit.legacy_pru`` |
  ``other``), one token ``UsageAggregate`` per (day, team, cost center, org, model, sku, routing,
  speed, pseudo) under the ``github.ai_usage_report.excl`` convention (report ``input`` →
  uncached, ``cache_write`` → unknown TTL), one ``github.ai_usage_report.coverage`` aggregate per
  (file, day) and — only with ``"copilot-report-quota" in opts.experimental`` — one
  ``ConfigSnapshot(kind="plan_quota")`` per (month, org, ``total_monthly_quota`` value).
* :class:`MeteredUsageAdapter` (``github-metered-usage``) — the detailed and summarized usage
  CSVs: seats (``seat``, SKU kept exactly for ``core.pool.detect_plans``), Actions minutes of the
  Copilot workloads (the dynamic Copilot paths and ``.github/workflows/*.lock.yml`` agentic
  workflows, ``workflow = h_(path)``; other workflows are counted, never stored), sandboxes, Code
  Quality licences and metered AI credits (``metered.ai_credit``, a cross-check only).
* :class:`BillingApiAdapter` (``github-billing-api``) — recorded REST pages (one JSON response,
  a JSON array, or CP-PULL's ``{"request": {path, query}, "response": {...}}`` envelopes as JSON
  lines): ``ai_credit/usage`` and ``premium_request/usage`` → ``rest.ai_credit``,
  ``usage/summary`` → ``rest.summary``, ``usage`` (by cost center) → ``rest.usage``; report-export
  envelopes yield no record and their ``download_urls`` are never read.

Rules shared by the three: exact decimals only (``core.money``; the half-even remainders of the net
amounts are summed in ``stats["rounding_remainder_e18"]`` as 1e-18 USD, ruling R-E44 keys them by
adapter name); natural ids (``core.ids.natural_id``) so an overlapping re-export produces the same
ids and the store keeps the latest fetch (``fetched_ms = opts.now_ms``); rows of one file with the
same natural key are summed (``dq.copilot_duplicate_key_summed``); logins go through
``opts.team_map`` / ``opts.cost_center_map`` (exact, then case-folded) and become ``p_`` pseudonyms
under ``opts.principal_key`` (the raw login, stripped — SPEC §5.1 ``central-ingest``), repositories
and agentic-workflow paths ``h_`` pseudonyms under ``opts.name_key``; org logins and cost-center
names are organizational and stay clear; ``finality`` is ``final`` only for days at least
``report_lag_days`` (facts, 3) before ``opts.now_ms`` in a closed month (April 2026 directional
rows: provisional forever); malformed rows are quarantined with content-free reasons. No adapter
emits a request or an inference, so ruling R-E43 (billing paths on inferences) has nothing to set.

Importing this module registers the report conventions ``github.ai_usage_report.excl`` and
``github.ai_usage_report.incl`` (``core.registry.CONVENTION_MODULES``). This is a money module: no
float anywhere (SPEC §2.4).
"""

from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import re
import zlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Context, Decimal
from pathlib import Path
from typing import Any

from tokenbill.core import catalog
from tokenbill.core import facts as _facts
from tokenbill.core.conventions import BadUsageError, Convention, register_convention
from tokenbill.core.errors import PrivacyError, SourceError, UsageError
from tokenbill.core.ids import key_id, natural_id, pseudonym
from tokenbill.core.jsonl import iter_lines, load_json_exact, open_text, parse_json_line
from tokenbill.core.models import normalize_copilot_model
from tokenbill.core.money import EXACT_CTX, usd_str_to_nano
from tokenbill.core.records import (
    MAX_TOKENS,
    ConfigSnapshot,
    CostLine,
    UsageAggregate,
    UsageBuckets,
)
from tokenbill.core.types import (
    DataQualityNote,
    IngestOptions,
    IngestResult,
    QuarantineItem,
    SourceInfo,
)

__all__ = [
    "AI_USAGE_SOURCE",
    "BILLING_API_SOURCE",
    "CONVENTION_EXCL",
    "CONVENTION_INCL",
    "COVERAGE_SOURCE",
    "METERED_SOURCE",
    "REPORT_QUOTA_FLAG",
    "AiUsageReportAdapter",
    "BillingApiAdapter",
    "MeteredUsageAdapter",
    "normalize_report_excl",
    "normalize_report_incl",
    "parse_report_date",
]

AI_USAGE_SOURCE = "github.ai_usage_report"
COVERAGE_SOURCE = "github.ai_usage_report.coverage"
METERED_SOURCE = "github.metered_usage"
BILLING_API_SOURCE = "github.billing_api"
CONVENTION_EXCL = "github.ai_usage_report.excl"
CONVENTION_INCL = "github.ai_usage_report.incl"
#: ``IngestOptions.experimental`` flag that turns ``total_monthly_quota`` into plan evidence.
REPORT_QUOTA_FLAG = "copilot-report-quota"
_CHANNEL = "github_copilot"
_DAY_MS = 86_400_000
_EPOCH = _dt.date(1970, 1, 1)
_REMAINDER_STAT = "rounding_remainder_e18"
#: Diagnostic: Σ half-even remainders of the gross amounts (the L1 side), 1e-18 USD.
_GROSS_REMAINDER_STAT = "rounding_remainder_e18.gross"

# data-quality codes (addendum §5.1–§5.3, CP-BILL brief)
DQ_ROW_IDENTITY = "dq.copilot_row_identity"
DQ_LEGACY_PRU = "dq.copilot_legacy_pru"
DQ_DIRECTIONAL = "dq.copilot_directional_report"
DQ_PREVIEW_COLUMNS = "dq.copilot_preview_columns"
DQ_UNATTRIBUTED = "dq.copilot_unattributed_rows"
DQ_UNMAPPED_SKU = "dq.unmapped_sku"
DQ_DUPLICATE_KEY = "dq.copilot_duplicate_key_summed"
DQ_QUOTA_IGNORED = "dq.copilot_report_quota_ignored"
DQ_INTEGER_QUANTITY = "dq.copilot_integer_quantity"
_NOTES: Mapping[str, tuple[str, str]] = {
    DQ_ROW_IDENTITY: ("warn", "rows whose gross - discount differs from net by more than 1 nano"),
    DQ_LEGACY_PRU: ("info", "legacy premium-request rows (no tokens, never token-priced)"),
    DQ_DIRECTIONAL: ("warn", "April 2026 rows: directional report, provisional forever"),
    DQ_PREVIEW_COLUMNS: ("info", "non-zero preview aic_* columns after 2026-06-01 ignored"),
    DQ_UNATTRIBUTED: ("info", "rows without a username: metered to the organization"),
    DQ_UNMAPPED_SKU: ("warn", "SKUs without a known cost type"),
    DQ_DUPLICATE_KEY: ("info", "rows sharing a natural key within one file were summed"),
    DQ_QUOTA_IGNORED: ("info", "total_monthly_quota values ignored (flag copilot-report-quota "
                               "off)"),
    DQ_INTEGER_QUANTITY: ("info", "integer REST quantities of AI-credit SKUs ignored"),
}

#: AI usage report columns a row needs (addendum §5.1; tokens, repository, cost center and quota
#: are optional).
AI_REQUIRED = ("date", "username", "product", "sku", "model", "quantity", "unit_type",
               "applied_cost_per_quantity", "gross_amount", "discount_amount", "net_amount",
               "organization")
#: Alias header names of the token columns (``0`` is not absent: a missing column is).
TOKEN_ALIASES: Mapping[str, str] = {
    "total_input_tokens": "input", "total_output_tokens": "output",
    "total_cache_read_tokens": "cache_read", "total_cache_creation_tokens": "cache_write",
}
TOKEN_COLUMNS = ("input", "output", "cache_read", "cache_write")
_PREVIEW_COLUMNS = ("aic_quantity", "aic_gross_amount", "aic_net_amount")
#: Detailed / summarized usage CSV columns a row needs (addendum §5.2).
METERED_REQUIRED = ("date", "product", "sku", "quantity", "unit_type", "gross_amount",
                    "discount_amount", "net_amount")
_METERED_SNIFF = frozenset(("date", "sku", "quantity", "unit_type", "gross_amount",
                            "discount_amount", "net_amount"))
#: GitHub product identifiers (lower-case, ``_`` for spaces) → channel; other products are skipped.
PRODUCT_CHANNELS: Mapping[str, str] = {
    "copilot": "github_copilot", "spark": "github_copilot", "code_quality": "github_copilot",
    "actions": "github_actions", "sandbox": "github_sandbox",
}
#: REST display names → product identifiers: only names from primary sources (OAS examples,
#: product-and-sku-names, facts); anything else is compared in its normalized form.
PRODUCT_ALIASES: Mapping[str, str] = {
    "copilot_ai_credits": "copilot",                                  # OAS user example
    "cloud_and_local_sandboxes_for_github_copilot": "sandbox",        # product-and-sku-names
}
#: REST SKU display names → SKU codes: the OAS examples (``Copilot AI Credits``, ``AI Credit``,
#: ``Copilot Premium Request``) and the display names of product-and-sku-names; other names are
#: normalized (lower case, ``_`` separators) and kept when a SKU fact or runner rate knows them
#: (``Actions Linux`` → ``actions_linux``), else kept with ``dq.unmapped_sku`` (VERIFY, §5.3).
SKU_ALIASES: Mapping[str, str] = {
    "copilot_ai_credits": "copilot_ai_credit", "ai_credit": "copilot_ai_credit",
    "copilot_premium_request": "copilot_premium_request",
    "copilot_cloud_agent": "coding_agent_ai_credit",
    "code_quality_ai_credits": "code_quality_ai_credit",
}
#: REST unit names → report unit names (OAS examples: ``credits``, ``ai-credits``).
UNIT_ALIASES: Mapping[str, str] = {"credits": "ai-credits"}

_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})\Z")
_SLASH_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})\Z")
_ISO_TS_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,]\d{1,9})?)?\s*"
    r"(Z|[+-]\d{2}(?::?\d{2})?)?\Z", re.IGNORECASE)
_NUMBER_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,6})?\Z")
_INT_RE = re.compile(r"[0-9]{1,16}\Z")
_SKU_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/ -]{0,63}\Z")
_ORG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_MODEL_RE = re.compile(r"[a-z0-9][a-z0-9._()+:/-]{0,63}\Z")
_UNIT_RE = re.compile(r"[a-z0-9][a-z0-9_. -]{0,31}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_KEY_RE = re.compile(r"[^a-z0-9]+")
_MAX_ADJUSTED = 30          # |amount| < 1e31 (USD, credits, minutes)
_MIN_EXPONENT = -60         # at most 60 fractional digits
_QTY_ADJUSTED = 20          # quantities: ≤ 21 integer and ≤ 20 fractional digits, exact sums
_QTY_EXPONENT = -20


class _Bad(Exception):
    """A malformed record; the message is its content-free quarantine reason."""


# ---------------------------------------------------------------------------------------------
# small parsers (public: parse_report_date)
# ---------------------------------------------------------------------------------------------


def parse_report_date(text: str) -> str | None:
    """A report date as ``YYYY-MM-DD`` (UTC): ``YYYY-MM-DD``, ``M/D/YY`` or ``M/D/YYYY`` (two-digit
    years are 20YY, as GitHub's parser reads them) or an ISO timestamp (``Z``, an offset, or naive
    = UTC, converted to its UTC date); None for anything else or a year before 1970."""
    if not isinstance(text, str):
        return None
    t = text.strip()
    try:
        m = _ISO_DATE_RE.match(t)
        if m is not None:
            day = _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        elif (m := _SLASH_DATE_RE.match(t)) is not None:
            year = int(m.group(3))
            day = _dt.date(2000 + year if len(m.group(3)) == 2 else year, int(m.group(1)),
                           int(m.group(2)))
        elif (m := _ISO_TS_RE.match(t)) is not None:
            moment = _dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                  int(m.group(4)), int(m.group(5)), int(m.group(6) or 0))
            zone = (m.group(7) or "Z").upper()
            if zone != "Z":
                digits = zone[1:].replace(":", "")
                offset = _dt.timedelta(hours=int(digits[:2]), minutes=int(digits[2:] or 0))
                moment = moment - offset if zone[0] == "+" else moment + offset
            day = moment.date()
        else:
            return None
    except (ValueError, OverflowError):
        return None
    return day.isoformat() if day >= _EPOCH else None


def _decimal(value: Any, name: str) -> Decimal:
    """A bounded finite decimal from a CSV cell or a JSON value (int, exact ``Decimal`` or a
    number string); anything else raises ``_Bad("bad_type:<name>")``."""
    if isinstance(value, bool):
        raise _Bad(f"bad_type:{name}")
    if isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, Decimal):
        d = value
    elif isinstance(value, str):
        text = value.strip()
        if len(text) > 100 or not _NUMBER_RE.match(text):
            raise _Bad(f"bad_type:{name}")
        d = Decimal(text)
    else:
        raise _Bad(f"bad_type:{name}")
    if not d.is_finite():
        raise _Bad(f"bad_type:{name}")
    if d and (d.adjusted() > _MAX_ADJUSTED or d.as_tuple().exponent < _MIN_EXPONENT):  # type: ignore[operator]
        raise _Bad(f"bad_type:{name}")
    return d


def _money(value: Any, name: str) -> tuple[int, int]:
    """``(nano, remainder in 1e-18 USD)``: exact USD → nano half-even once (``core.money``)."""
    d = _decimal(value, name)
    try:
        nano, rem = usd_str_to_nano(str(d))
    except (ValueError, ArithmeticError):
        raise _Bad(f"bad_type:{name}") from None
    return nano, _e18(rem)


def _e18(rem: Decimal) -> int:
    """A remainder in USD as an integer count of 1e-18 USD (half-even)."""
    if not rem:
        return 0
    sign, digits, exp = rem.as_tuple()
    x = Decimal((sign, digits, exp + 18))  # type: ignore[operator]
    adjusted = x.adjusted()
    if adjusted < -1:
        return 0
    return int(x.quantize(Decimal(1), context=Context(prec=adjusted + 3,
                                                      rounding=ROUND_HALF_EVEN)))


def _quantity(value: Any, name: str = "quantity") -> Decimal:
    d = _decimal(value, name)
    if d and (d.adjusted() > _QTY_ADJUSTED or d.as_tuple().exponent < _QTY_EXPONENT):  # type: ignore[operator]
        raise _Bad(f"bad_type:{name}")
    return d


def _dec_str(d: Decimal) -> str:
    """An exact plain decimal string without exponent or trailing fractional zeros."""
    if not d:
        return "0"
    sign, digits, exp = d.as_tuple()
    coeff = list(digits)
    while exp < 0 and len(coeff) > 1 and coeff[-1] == 0:  # type: ignore[operator]
        coeff.pop()
        exp += 1  # type: ignore[operator]
    return format(Decimal((sign, tuple(coeff), exp)), "f")


def _tokens(value: str, name: str) -> int | None:
    """A token count cell: None when empty (absent), else an int in [0, 2**53]."""
    text = value.strip()
    if not text:
        return None
    if _INT_RE.match(text):
        n = int(text)
    else:
        try:
            d = _decimal(text, name)
        except _Bad:
            raise _Bad("bad_usage") from None
        if d != d.to_integral_value() or d.adjusted() > 16:
            raise _Bad("bad_usage")
        n = int(d)
    if not 0 <= n <= MAX_TOKENS:
        raise _Bad("bad_usage")
    return n


def _clean(text: str, limit: int) -> str:
    return " ".join(_CONTROL_RE.sub(" ", text).split())[:limit]


def _name(text: str) -> str:
    """An organizational name (cost center) as stored: control characters and runs of spaces
    collapsed, at most 64 UTF-8 bytes (SPEC §8.1: no field carries more than 64 bytes of source
    text)."""
    return _clean(text, 64).encode("utf-8")[:64].decode("utf-8", "ignore").strip()


def _code(text: str, rx: re.Pattern[str], name: str) -> str:
    value = text.strip()
    if not rx.match(value):
        raise _Bad(f"bad_type:{name}")
    return value


def _key(text: str) -> str:
    return _KEY_RE.sub("_", text.strip().lower()).strip("_")


def _day_start(date: str) -> int:
    return (_dt.date.fromisoformat(date) - _EPOCH).days * _DAY_MS


def _month_start(month: str) -> int:
    return _day_start(f"{month}-01")


def _utc_today(now_ms: int) -> _dt.date:
    return _EPOCH + _dt.timedelta(days=max(0, now_ms) // _DAY_MS)


def _period_end(start: _dt.date, grain: str) -> _dt.date:
    if grain == "day":
        return start
    if grain == "year":
        return _dt.date(start.year, 12, 31)
    nxt = _dt.date(start.year + 1, 1, 1) if start.month == 12 else _dt.date(start.year,
                                                                          start.month + 1, 1)
    return nxt - _dt.timedelta(days=1)


# ---------------------------------------------------------------------------------------------
# per-read state
# ---------------------------------------------------------------------------------------------


@dataclass
class _LineAcc:
    """Accumulates the rows of one natural key into one cost line."""

    base: dict[str, Any]
    net: int = 0
    gross: int = 0
    qty: Decimal | None = None
    rows: int = 0


@dataclass
class _AggAcc:
    dims: tuple[tuple[str, str], ...]
    date: str
    usage: list[int] = field(default_factory=lambda: [0, 0, 0, 0])  # in, out, read, write
    net: int = 0
    gross: int = 0
    final: bool = True


class _Reader:
    """State of one ``read``: identity helpers, exact accumulation, quarantine, notes, stats."""

    def __init__(self, adapter: str, path: Path, opts: IngestOptions) -> None:
        if not isinstance(opts, IngestOptions):
            raise UsageError(f"{adapter}: read() needs IngestOptions")
        if not opts.name_key:
            raise UsageError(f"{adapter}: a name key is required (repositories and source ids "
                             "are pseudonymized)")
        self.adapter = adapter
        self.path = Path(path)
        if self.path.is_dir():
            raise UsageError(f"{adapter}: pass one report file at a time, not a directory")
        self.opts = opts
        self.source_id = pseudonym(opts.name_key, "s", f"{adapter}:{self.path.name}")
        self.today = _utc_today(opts.now_ms)
        self.lag = _facts.copilot_report_lag_days()
        dates = _facts.copilot_dates()
        self.directional = (dates.get("directional_report_start", "2026-04-01"),
                            dates.get("directional_report_end", "2026-05-01"))
        self.backfill = (dates.get("duplicate_rows_start", "2026-04-24"),
                         dates.get("duplicate_rows_end", "2026-05-01"))
        self.aic_zeroed = dates.get("aic_columns_zeroed", "2026-06-01")
        self.stats: dict[str, int] = {"rows": 0, _REMAINDER_STAT: 0}
        self.quarantined: list[QuarantineItem] = []
        self._notes: dict[str, list[Any]] = {}
        self.principal_used = False
        self._team = dict(opts.team_map)
        self._team_folded = {k.casefold(): v for k, v in sorted(self._team.items())}
        self._cc = dict(opts.cost_center_map)
        self._cc_folded = {k.casefold(): v for k, v in sorted(self._cc.items())}
        self.lines: dict[str, _LineAcc] = {}

    # ----- bookkeeping ----------------------------------------------------------------------

    def stat(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n

    def quarantine(self, locator: str, reason: str) -> None:
        if not self.opts.lenient:
            raise SourceError(f"{self.path.name}: {locator}: {reason}")
        self.quarantined.append(QuarantineItem(source_id=self.source_id, locator=locator,
                                               reason=reason))

    def note(self, code: str, n: int = 1, part: str | None = None) -> None:
        entry = self._notes.setdefault(code, [0, set()])
        entry[0] += n
        if part is not None:
            entry[1].add(part)

    def in_window(self, first: str, last: str | None = None) -> bool:
        """Whether the days *first* … *last* overlap ``[opts.since_ms, opts.until_ms)``."""
        o = self.opts
        if o.since_ms is not None and _day_start(last or first) + _DAY_MS <= o.since_ms:
            return False
        return o.until_ms is None or _day_start(first) < o.until_ms

    def is_directional(self, date: str) -> bool:
        return self.directional[0] <= date < self.directional[1]

    def final(self, last_day: str) -> bool:
        """``final`` iff *last_day* is at least ``report_lag_days`` before today in a closed
        month and not in the directional April window."""
        if self.is_directional(last_day):
            return False
        day = _dt.date.fromisoformat(last_day)
        return (day <= self.today - _dt.timedelta(days=self.lag)
                and (day.year, day.month) < (self.today.year, self.today.month))

    # ----- identity -------------------------------------------------------------------------

    def principal(self, login: str) -> str:
        key = self.opts.principal_key
        if not key:
            raise PrivacyError(f"{self.adapter}: rows with a username need a principal key "
                               "(the org or export key); none was given")
        self.principal_used = True
        return pseudonym(key, "p", login)

    def team(self, login: str) -> str | None:
        return self._team.get(login) or self._team_folded.get(login.casefold())

    def cost_center(self, login: str, column: str) -> str | None:
        if column:
            return column
        if not login:
            return None
        return self._cc.get(login) or self._cc_folded.get(login.casefold())

    def name_hash(self, value: str) -> str:
        return pseudonym(self.opts.name_key, "h", value)

    # ----- accumulation ---------------------------------------------------------------------

    def add_line(self, line_id: str, base: dict[str, Any], net: int, gross: int,
                 qty: Decimal | None) -> None:
        acc = self.lines.get(line_id)
        if acc is None:
            acc = self.lines[line_id] = _LineAcc(base=base)
        else:
            self.note(DQ_DUPLICATE_KEY)
            self.stat("duplicate_keys")
        acc.net += net
        acc.gross += gross
        if qty is not None:
            acc.qty = qty if acc.qty is None else EXACT_CTX.add(acc.qty, qty)
        acc.rows += 1

    def check_identity(self, gross: int, discount: int, net: int) -> None:
        if abs(gross - discount - net) > 1:
            self.note(DQ_ROW_IDENTITY)

    def cost_lines(self) -> list[CostLine]:
        out = []
        for line_id, acc in self.lines.items():
            out.append(CostLine(line_id=line_id, amount_nano=acc.net, list_amount_nano=acc.gross,
                                quantity=None if acc.qty is None else _dec_str(acc.qty),
                                fetched_ms=self.opts.now_ms, **acc.base))
        out.sort(key=lambda c: (c.date_utc, c.line_id))
        return out

    # ----- result ---------------------------------------------------------------------------

    def source_info(self) -> SourceInfo:
        digest = hashlib.sha256()
        total = 0
        try:
            with open(self.path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    digest.update(chunk)
                    total += len(chunk)
        except OSError as exc:
            raise SourceError(f"{self.path.name}: unreadable ({type(exc).__name__})") from None
        o = self.opts
        pkid = None
        if self.principal_used:
            pkid = o.principal_key_id or key_id(o.principal_key or b"")
        return SourceInfo(source_id=self.source_id, adapter=self.adapter,
                          name_hmac=self.name_hash(self.path.name), sha256=digest.hexdigest(),
                          bytes=total, name_key_id=o.name_key_id or key_id(o.name_key),
                          principal_key_id=pkid)

    def notes(self) -> list[DataQualityNote]:
        out = []
        for code in sorted(self._notes):
            count, parts = self._notes[code]
            severity, detail = _NOTES[code]
            if parts:
                detail = f"{detail}: {', '.join(sorted(parts))}"
            out.append(DataQualityNote(code=code, severity=severity, count=count,
                                       detail=detail[:256]))
        return out

    def result(self, *, cost_lines: list[CostLine], aggregates: Sequence[UsageAggregate] = (),
               config: Sequence[ConfigSnapshot] = ()) -> IngestResult:
        caps = set()
        if cost_lines:
            caps.add("cost")
        if aggregates:
            caps.add("aggregates")
        if cost_lines or aggregates:
            caps.add("copilot_billing")
        if config:
            caps.add("config")
        self.stats["cost_lines"] = len(cost_lines)
        self.stats["aggregates"] = len(aggregates)
        self.stats["config"] = len(config)
        self.stats["records"] = len(cost_lines) + len(aggregates) + len(config)
        self.stats["quarantined"] = len(self.quarantined)
        return IngestResult(source=self.source_info(), requests=[], sessions=[], events=[],
                            aggregates=list(aggregates), cost_lines=cost_lines, outcomes=[],
                            quarantined=list(self.quarantined), notes=self.notes(),
                            stats=dict(sorted(self.stats.items())),
                            capabilities=frozenset(caps), config=list(config))


# ---------------------------------------------------------------------------------------------
# CSV reading and sniffing
# ---------------------------------------------------------------------------------------------


def _head_names(head: bytes) -> set[str]:
    """Lower-cased header names of the first line of a CSV head (BOM and quotes stripped)."""
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    first = head.decode("utf-8", "ignore").split("\n", 1)[0].rstrip("\r")
    if not first or first.lstrip().startswith(("{", "[")):
        return set()
    try:
        cells = next(csv.reader([first]))
    except (csv.Error, StopIteration):
        return set()
    return {c.strip().lstrip("\ufeff").lower() for c in cells}


def _csv_rows(rd: _Reader) -> Iterator[tuple[str, dict[str, str] | list[str]]]:
    """``("header", names)`` first, then ``(locator, row)`` per data row. Rows with more cells
    than the header, or rows the csv module rejects, are quarantined (``bad_type:row`` /
    ``oversize_line``); short rows are padded with empty cells; blank rows are skipped."""
    raw = open_text(rd.path)
    try:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
        reader = csv.reader(text)
        header: list[str] | None = None
        last_error = -1
        while True:
            try:
                row = next(reader)
            except StopIteration:
                if header is None:
                    yield "header", []
                return
            except csv.Error as exc:
                if header is None:
                    yield "header", []
                    return
                if reader.line_num == last_error:
                    return
                last_error = reader.line_num
                rd.quarantine(f"line:{reader.line_num}",
                              "oversize_line" if "field limit" in str(exc) else "bad_type:row")
                continue
            except (OSError, EOFError, zlib.error) as exc:
                raise SourceError(f"{rd.path.name}: corrupt stream ({type(exc).__name__})"
                                  ) from None
            if header is None:
                names = [c.strip().lstrip("\ufeff").lower() for c in row]
                header = [TOKEN_ALIASES.get(n, n) if TOKEN_ALIASES.get(n, n) not in names else n
                          for n in names]
                yield "header", header
                continue
            if not any(cell.strip() for cell in row):
                continue
            locator = f"line:{reader.line_num}"
            if len(row) > len(header):
                rd.quarantine(locator, "bad_type:row")
                continue
            cells: dict[str, str] = {}
            for name, value in zip(header, row + [""] * (len(header) - len(row)), strict=True):
                cells.setdefault(name, value)
            yield locator, cells
    finally:
        raw.close()


def _missing_header(rd: _Reader, header: Sequence[str], required: Sequence[str]) -> bool:
    """Quarantine an unusable header (``missing:<first missing column>``); True when it is."""
    if not header:
        rd.quarantine("header", "missing:header")
        return True
    missing = [c for c in required if c not in header]
    if missing:
        rd.quarantine("header", f"missing:{missing[0]}")
        return True
    return False


# ---------------------------------------------------------------------------------------------
# github-ai-usage
# ---------------------------------------------------------------------------------------------


def _model_key(label: str, model: str, routing: str, speed: str, pseudo: str | None) -> str:
    """The model part of a report row's natural key: the normalized model (the raw label, cleaned,
    for pseudo labels), prefixed ``auto:`` for Auto rows and suffixed ``:fast`` for fast mode —
    so an Auto row and a direct row of one model on one day are two keys, not a duplicate."""
    base = model if model and pseudo is None else _clean(label, 128)
    if routing == "auto" and model:
        base = f"auto:{base}"
    if speed == "fast":
        base = f"{base}:fast"
    return base


class AiUsageReportAdapter:
    """``github-ai-usage``: the AI usage report CSV and the legacy premium-request CSV
    (addendum §5.1; see the module docstring)."""

    name = "github-ai-usage"
    capabilities = frozenset({"aggregates", "cost", "copilot_billing", "config"})

    def sniff(self, path: Path, head: bytes) -> bool:
        """A CSV header with ``unit_type``, ``applied_cost_per_quantity`` and ``model``."""
        try:
            return {"unit_type", "applied_cost_per_quantity", "model"} <= _head_names(head)
        except Exception:  # noqa: BLE001 - a sniffer never raises
            return False

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Parse one report file (plain or ``.gz``)."""
        rd = _Reader(self.name, Path(path), opts)
        quota_on = REPORT_QUOTA_FLAG in opts.experimental
        skus = _facts.copilot_skus()
        aggs: dict[tuple[Any, ...], _AggAcc] = {}
        coverage: dict[str, list[int]] = {}
        quota: dict[tuple[str, str, str], set[str]] = {}
        legacy_file = False
        with closing(_csv_rows(rd)) as rows:
            for locator, row in rows:
                if locator == "header":
                    if _missing_header(rd, list(row), AI_REQUIRED):
                        break
                    legacy_file = "exceeds_quota" in row
                    rd.stats["token_columns"] = int(any(c in row for c in TOKEN_COLUMNS))
                    continue
                assert isinstance(row, dict)
                rd.stat("rows")
                try:
                    self._row(rd, row, legacy_file, skus, aggs, coverage, quota, quota_on)
                except _Bad as exc:
                    rd.quarantine(locator, str(exc))
        aggregates = [self._agg(rd, acc) for acc in aggs.values()]
        aggregates += [self._coverage(rd, date, cell) for date, cell in coverage.items()]
        aggregates.sort(key=lambda a: (a.bucket_start_ms, a.source_kind, a.agg_id))
        config = [self._quota(rd, key, users) for key, users in sorted(quota.items())]
        return rd.result(cost_lines=rd.cost_lines(), aggregates=aggregates, config=config)

    # ----- one row --------------------------------------------------------------------------

    def _row(self, rd: _Reader, row: dict[str, str], legacy_file: bool,
             skus: Mapping[str, Any], aggs: dict[tuple[Any, ...], _AggAcc],
             coverage: dict[str, list[int]], quota: dict[tuple[str, str, str], set[str]],
             quota_on: bool) -> None:
        """One report row: every field is validated before anything is accumulated, so a
        quarantined row leaves no trace in the records, notes or stats."""
        date = parse_report_date(row["date"])
        if date is None:
            raise _Bad("bad_type:date" if row["date"].strip() else "missing:date")
        if not rd.in_window(date):
            rd.stat("rows_outside_window")
            return
        if not row["sku"].strip():
            raise _Bad("missing:sku")
        sku = _code(row["sku"], _SKU_RE, "sku")
        org = _code(row["organization"], _ORG_RE, "organization") if row[
            "organization"].strip() else ""
        qty = _quantity(row["quantity"]) if row["quantity"].strip() else None
        (gross, g_rem), (discount, _), (net, n_rem) = _amounts(row)
        tokens = [_tokens(row[c], c) if c in row else None for c in TOKEN_COLUMNS]
        quota_raw = row.get("total_monthly_quota", "").strip()
        quota_value = _quota_value(quota_raw)
        # GitHub's parser drops the April 24-30 back-fill duplicates (quantity 0, quota != 0)
        if rd.backfill[0] <= date < rd.backfill[1] and qty is not None and not qty and quota_value:
            rd.stat("backfill_duplicates_dropped")
            return
        unit = row["unit_type"].strip().lower()
        login = row["username"].strip()
        legacy = legacy_file or unit == "requests" or sku.lower().endswith("_premium_request")
        has_tokens = not legacy and any(t is not None for t in tokens)
        label = row["model"]
        cm = normalize_copilot_model(label)
        model = cm.model if _MODEL_RE.match(cm.model or "-") else ""
        cc_col = _name(row.get("cost_center_name", ""))
        team = rd.team(login) if login else None
        cost_center = rd.cost_center(login, cc_col) or None
        dims = {"channel": _CHANNEL, "organization": org or None, "team": team,
                "cost_center": cost_center, "model": model or None, "sku": sku,
                "routing": cm.routing, "speed": cm.speed, "pseudo": cm.pseudo}
        pairs = tuple(sorted((k, v) for k, v in dims.items() if v is not None))
        acc = aggs.get((date, pairs))
        usage = [(t or 0) for t in tokens]   # input, output, cache_read, cache_write
        if has_tokens and acc is not None and max(
                a + b for a, b in zip(acc.usage, usage, strict=True)) > MAX_TOKENS:
            raise _Bad("bad_usage")
        # ---- commit ----
        cost_type = catalog.copilot_cost_type(sku, username_present=bool(login))
        if legacy:
            cost_type = "ai_credit.legacy_pru"
            rd.note(DQ_LEGACY_PRU)
        elif cost_type not in ("ai_credit.user", "ai_credit.direct"):
            cost_type = "other"
            rd.note(DQ_UNMAPPED_SKU, part=_note_part(sku))
        if cm.model and not model:
            rd.stat("model_unreadable")
        if not login:
            rd.note(DQ_UNATTRIBUTED)
        if rd.is_directional(date):
            rd.note(DQ_DIRECTIONAL)
        if date >= rd.aic_zeroed and any(_nonzero(row.get(c, "")) for c in _PREVIEW_COLUMNS):
            rd.note(DQ_PREVIEW_COLUMNS)
        rd.check_identity(gross, discount, net)
        principal = rd.principal(login) if login else None
        repo_raw = row.get("repository", "").strip()
        repo = rd.name_hash(repo_raw) if repo_raw else None
        fact = skus.get(sku)
        workload = _PSEUDO_WORKLOADS.get(cm.pseudo or "") or (
            fact.workload if fact is not None else None)
        final = rd.final(date)
        line_id = natural_id("cl", AI_USAGE_SOURCE, date, principal,
                             _model_key(label, model, cm.routing, cm.speed, cm.pseudo), sku, org,
                             cc_col, repo)
        rd.add_line(line_id, {
            "source_kind": AI_USAGE_SOURCE, "date_utc": date, "channel": _CHANNEL,
            "workspace_id": org or None, "description": _clean(f"{sku} {label}", 128),
            "model": model or None, "cost_type": cost_type, "token_type": None, "sku": sku,
            "service_tier": None, "inference_geo": None, "endpoint_scope": None,
            "finality": "final" if final else "provisional", "principal": principal,
            "unit": unit if _UNIT_RE.match(unit) else None, "cost_center": cost_center,
            "team": team, "repo": repo, "workload": workload, "routing": cm.routing,
            "speed": cm.speed, "pseudo": cm.pseudo}, net, gross, qty)
        rd.stats[_REMAINDER_STAT] += n_rem
        rd.stat(_GROSS_REMAINDER_STAT, g_rem)
        cov = coverage.setdefault(date, [0, 0])
        cov[0] += net
        cov[1] += gross
        rd.stat(f"rows:{date}")
        if login and quota_raw and not legacy:
            if not quota_on:
                if quota_value:
                    rd.note(DQ_QUOTA_IGNORED)
            elif quota_value:
                entity = f"org:{org}" if org else "enterprise"
                quota.setdefault((date[:7], entity, _dec_str(quota_value)), set()).add(
                    principal or "")
            else:
                rd.stat("quota_unknown" if quota_value is None else "quota_zero")
        if not has_tokens:
            rd.stat("rows_without_tokens")
            return
        if acc is None:
            acc = aggs[(date, pairs)] = _AggAcc(dims=pairs, date=date)
        acc.usage = [a + b for a, b in zip(acc.usage, usage, strict=True)]
        acc.net += net
        acc.gross += gross
        acc.final = acc.final and final

    def _agg(self, rd: _Reader, acc: _AggAcc) -> UsageAggregate:
        start = _day_start(acc.date)
        inp, out, read, write = acc.usage
        return UsageAggregate(
            agg_id=natural_id("ag", AI_USAGE_SOURCE, acc.date,
                              *(f"{k}={v}" for k, v in acc.dims)),
            source_kind=AI_USAGE_SOURCE, bucket_start_ms=start, bucket_end_ms=start + _DAY_MS,
            dims=acc.dims,
            usage=UsageBuckets(uncached_input=inp, cache_read=read, cache_write_unknown=write,
                               output=out),
            reported_cost_nano=acc.net, reported_cost_basis="invoice", list_cost_nano=acc.gross,
            finality="final" if acc.final else "provisional", fetched_ms=rd.opts.now_ms)

    def _coverage(self, rd: _Reader, date: str, cell: list[int]) -> UsageAggregate:
        start = _day_start(date)
        return UsageAggregate(
            agg_id=natural_id("ag", COVERAGE_SOURCE, date, rd.source_id),
            source_kind=COVERAGE_SOURCE, bucket_start_ms=start, bucket_end_ms=start + _DAY_MS,
            dims=(("channel", _CHANNEL), ("source", rd.source_id)), usage=UsageBuckets(),
            reported_cost_nano=cell[0], reported_cost_basis="invoice", list_cost_nano=cell[1],
            finality="final" if rd.final(date) else "provisional", fetched_ms=rd.opts.now_ms)

    def _quota(self, rd: _Reader, key: tuple[str, str, str], users: set[str]) -> ConfigSnapshot:
        month, entity, value = key
        return ConfigSnapshot(
            snapshot_ms=_month_start(month), source_kind=AI_USAGE_SOURCE, kind="plan_quota",
            entity_id=entity, attrs=(("month", month), ("n_users", len(users)),
                                     ("quota", value)),
            fetched_ms=rd.opts.now_ms)


_PSEUDO_WORKLOADS: Mapping[str, str] = {"code_review": "copilot_code_review",
                                         "cloud_agent": "copilot_cloud_agent"}


def _amounts(row: Mapping[str, str]) -> list[tuple[int, int]]:
    """``[(nano, remainder e18)]`` of ``gross_amount``, ``discount_amount``, ``net_amount``."""
    out = []
    for name in ("gross_amount", "discount_amount", "net_amount"):
        if not row[name].strip():
            raise _Bad(f"missing:{name}")
        out.append(_money(row[name], name))
    return out


def _quota_value(text: str) -> Decimal | None:
    """``total_monthly_quota``: a non-negative number, or None (empty, ``Unknown``, malformed —
    GitHub's parser reads those as 0 for its back-fill rule)."""
    if not text:
        return None
    try:
        d = _decimal(text, "total_monthly_quota")
    except _Bad:
        return None
    if d < 0 or d != d.to_integral_value() or d.adjusted() > 9:
        return None
    return d


def _nonzero(text: str) -> bool:
    try:
        return bool(_decimal(text, "aic")) if text.strip() else False
    except _Bad:
        return False


def _note_part(code: str) -> str:
    part = _key(code)[:40]
    return part or "(blank)"


# ---------------------------------------------------------------------------------------------
# github-metered-usage
# ---------------------------------------------------------------------------------------------

_AI_COST_TYPES = frozenset({"ai_credit.user", "ai_credit.direct", "ai_credit.legacy_pru"})
_CHANNEL_COST_TYPES: Mapping[str, str] = {"github_actions": "actions",
                                          "github_sandbox": "sandbox"}


class MeteredUsageAdapter:
    """``github-metered-usage``: the detailed and the summarized usage report CSVs (addendum §5.2;
    see the module docstring)."""

    name = "github-metered-usage"
    capabilities = frozenset({"cost", "copilot_billing"})

    def sniff(self, path: Path, head: bytes) -> bool:
        """A usage-report CSV header (``date``, ``sku``, ``quantity``, ``unit_type`` and the three
        amounts) with ``workflow_path`` or without ``model``."""
        try:
            names = _head_names(head)
            return _METERED_SNIFF <= names and ("workflow_path" in names or "model" not in names)
        except Exception:  # noqa: BLE001 - a sniffer never raises
            return False

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Parse one detailed or summarized report file (plain or ``.gz``)."""
        rd = _Reader(self.name, Path(path), opts)
        dynamic = {w.pattern for w in _facts.copilot_workflow_paths() if w.match == "exact"}
        with closing(_csv_rows(rd)) as rows:
            for locator, row in rows:
                if locator == "header":
                    if _missing_header(rd, list(row), METERED_REQUIRED):
                        break
                    rd.stats["detailed"] = int("workflow_path" in row or "username" in row)
                    continue
                assert isinstance(row, dict)
                rd.stat("rows")
                try:
                    self._row(rd, row, dynamic)
                except _Bad as exc:
                    rd.quarantine(locator, str(exc))
        return rd.result(cost_lines=rd.cost_lines())

    def _row(self, rd: _Reader, row: dict[str, str], dynamic: set[str]) -> None:
        """One usage row (validated before anything is accumulated)."""
        date = parse_report_date(row["date"])
        if date is None:
            raise _Bad("bad_type:date" if row["date"].strip() else "missing:date")
        if not rd.in_window(date):
            rd.stat("rows_outside_window")
            return
        if not row["sku"].strip():
            raise _Bad("missing:sku")
        sku = _code(row["sku"], _SKU_RE, "sku")
        channel = PRODUCT_CHANNELS.get(PRODUCT_ALIASES.get(_key(row["product"]),
                                                           _key(row["product"])))
        if channel is None:
            rd.stat("rows_other_products")
            return
        path = row.get("workflow_path", "").strip()
        workload = catalog.copilot_workload(path) if path else None
        if channel == "github_actions" and workload is None:
            rd.stat("user_workflows_dropped" if path else "actions_unattributed_dropped")
            return
        qty = _quantity(row["quantity"]) if row["quantity"].strip() else None
        (gross, _), (discount, _), (net, n_rem) = _amounts(row)
        org = _code(row["organization"], _ORG_RE, "organization") if row.get(
            "organization", "").strip() else ""
        # ---- commit ----
        cost_type = catalog.copilot_cost_type(sku, username_present=True)
        expected = _CHANNEL_COST_TYPES.get(channel)
        if expected is not None:           # Actions minutes / sandbox meters, whatever the SKU
            if cost_type != expected:
                rd.note(DQ_UNMAPPED_SKU, part=_note_part(sku))
            cost_type = expected
        elif cost_type in _AI_COST_TYPES:
            cost_type = "metered.ai_credit"
            fact = _facts.copilot_skus().get(sku)
            workload = fact.workload if fact is not None else None
        elif cost_type not in ("seat", "code_quality.license"):
            cost_type = "other"
            rd.note(DQ_UNMAPPED_SKU, part=_note_part(sku))
        if channel != "github_actions" and workload is not None and cost_type != (
                "metered.ai_credit"):
            workload = None                # workflow paths classify Actions rows only
        rd.check_identity(gross, discount, net)
        login = row.get("username", "").strip()
        cc_col = _name(row.get("cost_center_name", ""))
        repo_raw = row.get("repository", "").strip()
        repo = rd.name_hash(repo_raw) if repo_raw else None
        principal = rd.principal(login) if login else None
        workflow = rd.name_hash(path) if workload == "agentic_workflow" else None
        unit = row["unit_type"].strip().lower()
        # dynamic Copilot paths are GitHub-defined names; agentic-workflow paths enter only as h_
        path_key = path if path in dynamic else (workflow or "")
        line_id = natural_id("cl", METERED_SOURCE, date, principal, sku, org, repo, cc_col,
                             path_key, workload, cost_type)
        rd.add_line(line_id, {
            "source_kind": METERED_SOURCE, "date_utc": date, "channel": channel,
            "workspace_id": org or None, "description": _clean(f"{sku} {unit}", 128),
            "model": None, "cost_type": cost_type, "token_type": None, "sku": sku,
            "service_tier": None, "inference_geo": None, "endpoint_scope": None,
            "finality": "final" if rd.final(date) else "provisional", "principal": principal,
            "unit": unit if _UNIT_RE.match(unit) else None,
            "cost_center": rd.cost_center(login, cc_col) or None,
            "team": rd.team(login) if login else None, "repo": repo, "workload": workload,
            "workflow": workflow, "routing": None, "speed": None, "pseudo": None},
            net, gross, qty)
        rd.stats[_REMAINDER_STAT] += n_rem
        rd.stat(f"cost_type:{cost_type}")


# ---------------------------------------------------------------------------------------------
# github-billing-api
# ---------------------------------------------------------------------------------------------

_ENDPOINTS = (("/ai_credit/usage", "ai_credit"), ("/premium_request/usage", "premium_request"),
              ("/usage/summary", "summary"), ("/settings/billing/usage", "usage"),
              ("/settings/billing/reports", "reports"))
_API_SNIFF_PATHS = tuple(p.encode() for p, _ in _ENDPOINTS)


def _endpoint(path: Any) -> str | None:
    """The billing endpoint a recorded request path names, or None."""
    if not isinstance(path, str):
        return None
    clean = path.split("?", 1)[0].rstrip("/")
    for suffix, kind in _ENDPOINTS[:4]:
        if clean.endswith(suffix):
            return kind
    return "reports" if "/settings/billing/reports" in clean else None


def _is_export(body: Mapping[str, Any]) -> bool:
    return "usage_report_exports" in body or (
        "report_type" in body and ("status" in body or "download_urls" in body))


def _canonical_sku(name: str) -> tuple[str, bool]:
    """``(SKU code, known)`` of a REST SKU display name or code: the alias table, else the
    normalized name (known when a SKU fact or a runner rate has it)."""
    k = _key(name)
    if k in SKU_ALIASES:
        return SKU_ALIASES[k], True
    return k, k in _facts.copilot_skus() or catalog.runner_rate(k) is not None


def _text(value: Any, name: str, *, required: bool = False) -> str:
    if value is None:
        if required:
            raise _Bad(f"missing:{name}")
        return ""
    if not isinstance(value, str):
        raise _Bad(f"bad_type:{name}")
    return value.strip()


class BillingApiAdapter:
    """``github-billing-api``: recorded billing REST pages (addendum §5.3; see the module
    docstring)."""

    name = "github-billing-api"
    capabilities = frozenset({"cost", "copilot_billing"})

    def sniff(self, path: Path, head: bytes) -> bool:
        """A JSON document whose head names ``usageItems`` or a report export, or a JSON-lines
        envelope whose request path is a billing usage or reports endpoint."""
        try:
            text = head.lstrip(b"\xef\xbb\xbf \t\r\n")
            if not text.startswith((b"{", b"[")):
                return False
            if b'"usageItems"' in text or b'"usage_report_exports"' in text:
                return True
            if b'"report_type"' in text and (b'"download_urls"' in text or b'"status"' in text):
                return True
            first = text.split(b"\n", 1)[0]
            return b'"response"' in first and any(p in first for p in _API_SNIFF_PATHS)
        except Exception:  # noqa: BLE001 - a sniffer never raises
            return False

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Parse one recorded file: a response, a JSON array of responses or envelopes, or JSON
        lines of them."""
        rd = _Reader(self.name, Path(path), opts)
        for locator, request, body in self._documents(rd):
            rd.stat("pages")
            try:
                self._page(rd, locator, request, body)
            except _Bad as exc:
                rd.quarantine(locator, str(exc))
        return rd.result(cost_lines=rd.cost_lines())

    # ----- documents ------------------------------------------------------------------------

    def _documents(self, rd: _Reader) -> Iterator[tuple[str, Mapping[str, Any] | None, Any]]:
        try:
            doc = load_json_exact(rd.path)
        except SourceError as exc:
            if "invalid JSON" not in str(exc):
                raise
            yield from self._lines(rd)
            return
        items = doc if isinstance(doc, list) else [doc]
        for i, item in enumerate(items):
            locator = f"item:{i}" if isinstance(doc, list) else "document"
            yield (locator, *self._unwrap(item))

    def _lines(self, rd: _Reader) -> Iterator[tuple[str, Mapping[str, Any] | None, Any]]:
        parsed = 0
        for line_no, _, raw in iter_lines(rd.path):
            locator = f"line:{line_no}"
            if not raw:
                rd.quarantine(locator, "oversize_line")
                continue
            obj = parse_json_line(raw, exact_numbers=True)
            if obj is None:
                rd.quarantine(locator, "bad_json")
                continue
            parsed += 1
            yield (locator, *self._unwrap(obj))
        if not parsed:
            rd.stat("unparsed_files")

    @staticmethod
    def _unwrap(item: Any) -> tuple[Mapping[str, Any] | None, Any]:
        if isinstance(item, dict) and "response" in item:
            request = item.get("request")
            return (request if isinstance(request, dict) else None), item["response"]
        return None, item

    # ----- one page -------------------------------------------------------------------------

    def _page(self, rd: _Reader, locator: str, request: Mapping[str, Any] | None,
              body: Any) -> None:
        if not isinstance(body, dict):
            raise _Bad("not_object")
        kind = _endpoint(request.get("path")) if request is not None else None
        if kind == "reports" or _is_export(body):
            rd.stat("export_envelopes")      # download_urls are never read or stored
            return
        items = body.get("usageItems")
        if items is None:
            raise _Bad("missing:usageItems")
        if not isinstance(items, list):
            raise _Bad("bad_type:usageItems")
        if kind is None:
            if "timePeriod" not in body:
                kind = "usage"
            elif any(isinstance(it, dict) and "model" in it for it in items):
                kind = "ai_credit"
            else:
                kind = "summary"
        query = request.get("query") if request is not None else None
        query = query if isinstance(query, dict) else {}
        scope = self._scope(rd, body, query, kind)
        for i, item in enumerate(items):
            try:
                self._item(rd, kind, scope, item)
            except _Bad as exc:
                rd.quarantine(f"{locator}:item:{i}", str(exc))

    def _scope(self, rd: _Reader, body: Mapping[str, Any], query: Mapping[str, Any],
               kind: str) -> dict[str, Any]:
        scope: dict[str, Any] = {"period": None, "grain": "day", "principal": None,
                                 "team": None, "org": "", "enterprise": "", "cc_name": "",
                                 "cc_id": ""}
        if kind != "usage":
            tp = body.get("timePeriod")
            if not isinstance(tp, dict):
                raise _Bad("missing:timePeriod")
            year, month, day = tp.get("year"), tp.get("month"), tp.get("day")
            try:
                if type(year) is not int:
                    raise _Bad("bad_type:timePeriod")
                start = _dt.date(year, month if type(month) is int else 1,
                                 day if type(day) is int and type(month) is int else 1)
            except (ValueError, OverflowError):
                raise _Bad("bad_type:timePeriod") from None
            if start < _EPOCH:
                raise _Bad("bad_type:timePeriod")
            grain = "year" if type(month) is not int else ("day" if type(day) is int else "month")
            scope.update(period=start, grain=grain)
        user = _text(body.get("user"), "user")
        if user:
            scope["principal"] = rd.principal(user)
            scope["team"] = rd.team(user)
        org = _text(body.get("organization"), "organization")
        scope["org"] = _code(org, _ORG_RE, "organization") if org else ""
        scope["enterprise"] = _clean(_text(body.get("enterprise"), "enterprise"), 128)
        cc = body.get("costCenter")
        if isinstance(cc, dict):
            scope["cc_name"] = _name(_text(cc.get("name"), "costCenter"))
            scope["cc_id"] = _clean(_text(cc.get("id"), "costCenter"), 128)
        qcc = query.get("cost_center_id")
        if not scope["cc_id"] and isinstance(qcc, str):
            scope["cc_id"] = _clean(qcc, 128)
        return scope

    def _item(self, rd: _Reader, kind: str, scope: Mapping[str, Any], item: Any) -> None:
        """One ``usageItems`` entry (validated before anything is accumulated)."""
        if not isinstance(item, dict):
            raise _Bad("not_object")
        sku_raw = _text(item.get("sku"), "sku", required=True)
        if not sku_raw or len(sku_raw) > 64:
            raise _Bad("bad_type:sku")
        sku, known = _canonical_sku(sku_raw)
        if not sku:
            raise _Bad("bad_type:sku")
        fact = _facts.copilot_skus().get(sku)
        product = _key(_text(item.get("product"), "product"))
        product = PRODUCT_ALIASES.get(product, product)
        if product not in PRODUCT_CHANNELS and fact is not None:
            product = fact.product
        channel = PRODUCT_CHANNELS.get(product)
        if channel is None:
            rd.stat("items_other_products")
            return
        unit_raw = _text(item.get("unitType"), "unitType").lower()
        unit = UNIT_ALIASES.get(unit_raw, unit_raw)
        amounts = []
        for name in ("grossAmount", "discountAmount", "netAmount"):
            if item.get(name) is None:
                raise _Bad(f"missing:{name}")
            amounts.append(_money(item[name], name))
        (gross, _), (discount, _), (net, n_rem) = amounts
        label = ""
        if kind in ("ai_credit", "premium_request") and item.get("model") is not None:
            label = _text(item.get("model"), "model")
        org, repo = scope["org"], None
        if kind == "usage":
            date = parse_report_date(_text(item.get("date"), "date", required=True))
            if date is None:
                raise _Bad("bad_type:date")
            last = period_key = date
            org_name = _text(item.get("organizationName"), "organizationName")
            org = _code(org_name, _ORG_RE, "organizationName") if org_name else org
            repo_name = _text(item.get("repositoryName"), "repositoryName")
            qty_value = item.get("quantity")
            cost_type = "rest.usage"
        else:
            date = scope["period"].isoformat()
            last = _period_end(scope["period"], scope["grain"]).isoformat()
            period_key = f"{scope['grain']}:{date}"
            repo_name = ""
            qty_value = item.get("netQuantity")
            cost_type = "rest.summary" if kind == "summary" else "rest.ai_credit"
        qty = _quantity(qty_value) if qty_value is not None else None
        if not rd.in_window(date, last):
            rd.stat("items_outside_window")
            return
        # ---- commit ----
        if not known:
            rd.note(DQ_UNMAPPED_SKU, part=_note_part(sku))
        if kind == "usage" and qty is not None and fact is not None and (
                fact.cost_type == "ai_credit.user"):
            rd.note(DQ_INTEGER_QUANTITY)       # the endpoint rounds AI credits to integers
            qty = None
        rd.check_identity(gross, discount, net)
        cm = normalize_copilot_model(label) if label else None
        model = cm.model if cm is not None and _MODEL_RE.match(cm.model or "-") else None
        if repo_name:
            repo = rd.name_hash(repo_name)
        model_key = _model_key(label, model or "", cm.routing, cm.speed,
                               cm.pseudo) if cm is not None else ""
        line_id = natural_id("cl", BILLING_API_SOURCE, cost_type, period_key,
                             scope["enterprise"], org, scope["principal"], scope["cc_id"],
                             product, sku, model_key, unit, repo)
        rd.add_line(line_id, {
            "source_kind": BILLING_API_SOURCE, "date_utc": date, "channel": channel,
            "workspace_id": org or None, "description": _clean(f"{sku} {label or unit}", 128),
            "model": model, "cost_type": cost_type, "token_type": None, "sku": sku,
            "service_tier": None, "inference_geo": None, "endpoint_scope": None,
            "finality": "final" if rd.final(last) else "provisional",
            "principal": scope["principal"], "unit": unit if _UNIT_RE.match(unit) else None,
            "cost_center": scope["cc_name"] or None, "team": scope["team"], "repo": repo,
            "workload": fact.workload if fact is not None else None, "workflow": None,
            "routing": cm.routing if cm is not None else None,
            "speed": cm.speed if cm is not None else None,
            "pseudo": cm.pseudo if cm is not None else None}, net, gross, qty)
        rd.stats[_REMAINDER_STAT] += n_rem
        rd.stat(f"items:{kind}")


# ---------------------------------------------------------------------------------------------
# report conventions (registered on import; addendum §5.11)
# ---------------------------------------------------------------------------------------------


def _report_counts(raw: Mapping[str, object]) -> tuple[int, int, int, int]:
    """``(input, cache_read, cache_write, output)`` of a report row (column names or the
    ``total_*_tokens`` aliases); absent = 0; malformed → ``BadUsageError``."""
    if not isinstance(raw, Mapping):
        raise BadUsageError("bad_usage: usage")
    out = []
    for column in ("input", "cache_read", "cache_write", "output"):
        alias = next((a for a, c in TOKEN_ALIASES.items() if c == column), column)
        value = raw.get(column, raw.get(alias))
        if value is None:
            out.append(0)
            continue
        if type(value) is not int or not 0 <= value <= MAX_TOKENS:
            raise BadUsageError(f"bad_usage: {column}")
        out.append(value)
    return out[0], out[1], out[2], out[3]


def normalize_report_excl(raw: Mapping[str, object]) -> tuple[UsageBuckets, list[str]]:
    """``github.ai_usage_report.excl``: the report's ``input`` excludes cache reads and writes —
    input → uncached, ``cache_read`` → read, ``cache_write`` → unknown-TTL writes, output."""
    inp, read, write, out = _report_counts(raw)
    return UsageBuckets(uncached_input=inp, cache_read=read, cache_write_unknown=write,
                        output=out), []


def normalize_report_incl(raw: Mapping[str, object]) -> tuple[UsageBuckets, list[str]]:
    """``github.ai_usage_report.incl``: the report's ``input`` includes cache reads and writes —
    uncached = input − read − write; read + write > input cannot be inclusive, so that row is
    mapped exclusively with ``dq.convention_mismatch`` (the reconciler decides per file, §12)."""
    inp, read, write, out = _report_counts(raw)
    if read + write > inp:
        return UsageBuckets(uncached_input=inp, cache_read=read, cache_write_unknown=write,
                            output=out), ["dq.convention_mismatch"]
    return UsageBuckets(uncached_input=inp - read - write, cache_read=read,
                        cache_write_unknown=write, output=out), []


register_convention(
    Convention(CONVENTION_EXCL, "github", False, True,
               "AI usage report: input excludes cache_read and cache_write (default); writes "
               "have no TTL split (addendum §5.11, inclusivity VERIFY)"),
    normalize_report_excl)
register_convention(
    Convention(CONVENTION_INCL, "github", True, True,
               "AI usage report: input includes cache_read and cache_write; uncached = input - "
               "read - write (decided per file by the reconciler, addendum §12)"),
    normalize_report_incl)

