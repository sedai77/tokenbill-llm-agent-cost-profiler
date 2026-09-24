"""Anthropic Admin and Analytics page adapters (SPEC §5.11, D32; package ADMIN).

Recorded JSON pages of the provider-side truth become content-free ledger records:

* :class:`UsageReportAdapter` — ``GET /v1/organizations/usage_report/messages`` → one
  :class:`~tokenbill.core.records.UsageAggregate` per time bucket and group;
* :class:`CostReportAdapter` — ``GET /v1/organizations/cost_report`` → :class:`CostLine` rows
  (cents decimal strings parsed exactly; the rounding remainder is kept for the ``cents_rounding``
  residual);
* :class:`ClaudeCodeAnalyticsAdapter` — ``GET /v1/organizations/usage_report/claude_code``: per-user
  day records **aggregated at ingest** to ``(date, team)`` token aggregates and
  :class:`OutcomeAggregate` rows with k-anonymity (:func:`tokenbill.core.kanon.merge_small_groups`);
* :class:`EnterpriseAnalyticsAdapter` — ``/v1/organizations/analytics/{usage_report,
  cost_report, user_usage_report, user_cost_report}``: organization aggregates and cost lines with
  ``amount`` and ``list_amount``; the per-user endpoints are aggregated to teams exactly like
  Claude Code Analytics.

Input: one recorded page per file (the documented response object), a JSON array of pages, JSONL
of pages (one per line), a recorded-page wrapper ``{"endpoint"|"url"|"path",
"fetched_at"|"fetched_ms", "response"|"page"|"body"}``, ``.gz`` of any of these, or a
**directory** of such files (read together, so per-user pages of one day split across files are
aggregated before k-anonymity).

Privacy (SPEC §5.1, §8): workspace and API key ids become ``h_`` pseudonyms under the name key
(unless listed in ``opts.name_allowlist``); person-level dimensions (``account_id``,
``service_account_id``, actors, e-mails, Slack user ids) are dropped and their rows summed; actor
references are used only to look up ``opts.team_map`` and never leave the adapter, not even
pseudonymized. Money is never a float: JSON numbers are parsed as ``Decimal``, cents are shifted
to USD exactly, every amount is accumulated exactly as a scaled integer, and each cost line or
aggregate is rounded half-even once (:func:`tokenbill.core.money.usd_str_to_nano`, identical to
``cents_to_nano`` on the cents string).

The shared page machinery here (:class:`ReadContext`, :func:`load_documents`, amount and timestamp
parsing) is also used by :mod:`tokenbill.adapters.openai_admin` and
:mod:`tokenbill.adapters.cloud_billing`.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date as _date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tokenbill.core.errors import ContractViolation, SourceError, UsageError
from tokenbill.core.ids import pseudonym, stable_id
from tokenbill.core.jsonl import iter_lines, open_text
from tokenbill.core.kanon import merge_small_groups
from tokenbill.core.models import normalize_model
from tokenbill.core.money import decimal_to_nano, usd_str_to_nano
from tokenbill.core.records import (
    MAX_TOKENS,
    CostLine,
    OutcomeAggregate,
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
    "CHANNEL",
    "OTHER_TEAM",
    "UNMAPPED_TEAM",
    "ClaudeCodeAnalyticsAdapter",
    "CostReportAdapter",
    "EnterpriseAnalyticsAdapter",
    "UsageReportAdapter",
    "classify_page",
]

# ---------------------------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------------------------

#: Every Anthropic Admin/Analytics record is first-party API usage.
CHANNEL = "anthropic_api"
#: Team label of actors (and principals) that ``opts.team_map`` does not map (SPEC §5.11).
UNMAPPED_TEAM = "(unmapped)"
#: Label of the merged small-group row (``core.kanon.merge_small_groups``).
OTHER_TEAM = "(other)"

DAY_MS = 86_400_000
#: Anthropic's documented revision window: values for a date can change for up to 30 days.
REVISION_WINDOW_MS = 30 * DAY_MS
#: Documents larger than this are read as JSONL (line by line) instead of as one JSON value.
MAX_DOC_BYTES = 256 * 2**20
#: Money is accumulated exactly as an integer number of 10**-MONEY_SCALE USD.
MONEY_SCALE = 210
#: Accepted source amounts: at most this many fractional digits and |amount| < 10**30 USD.
MAX_FRACTION_DIGITS = 200
MAX_AMOUNT_EXP = 30
#: Longest provider label (model, tier, cost type, …) copied into a record.
MAX_LABEL = 64
MAX_DESCRIPTION = 256

_EPOCH_ORDINAL = _date(1970, 1, 1).toordinal()
_TS_RE = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})"
    r"(?:[Tt ](\d{2}):(\d{2})(?::(\d{2})(?:[.,](\d{1,12}))?)?)?"
    r"\s*(Z|z|UTC|utc|GMT|[+-]\d{2}(?::?\d{2})?)?\Z"
)
_CONTROL_RE = re.compile("[\x00-\x1f\x7f-\x9f‪-‮⁦-⁩]")
_KEY_RE = re.compile(r'"([A-Za-z0-9_./-]{1,64})"\s*:')

# page kinds (classify_page)
K_USAGE = "anthropic.usage_report"
K_COST = "anthropic.cost_report"
K_CC = "anthropic.cc_analytics"
K_ENT_USAGE = "anthropic.enterprise.usage_report"
K_ENT_COST = "anthropic.enterprise.cost_report"
K_ENT_USER_USAGE = "anthropic.enterprise.user_usage_report"
K_ENT_USER_COST = "anthropic.enterprise.user_cost_report"
K_OAI_USAGE = "openai.usage"
K_OAI_COSTS = "openai.costs"

#: Endpoint path fragments of recorded-page wrappers → page kind (most specific first).
ENDPOINT_HINTS: tuple[tuple[str, str], ...] = (
    ("/analytics/user_usage_report", K_ENT_USER_USAGE),
    ("/analytics/user_cost_report", K_ENT_USER_COST),
    ("/analytics/usage_report", K_ENT_USAGE),
    ("/analytics/cost_report", K_ENT_COST),
    ("/usage_report/claude_code", K_CC),
    ("/usage_report/messages", K_USAGE),
    ("/cost_report", K_COST),
    ("/organization/usage/", K_OAI_USAGE),
    ("/organization/costs", K_OAI_COSTS),
)
#: Result/record keys that only the Enterprise Analytics API returns.
_ENTERPRISE_MARKERS = frozenset({
    "rbac_group_id", "product", "requests", "list_amount", "slack_channel_id",
    "claude_tag_category", "claude_tag_user_id", "data_refreshed_at",
})
_TOKEN_KEYS = frozenset({"uncached_input_tokens", "cache_read_input_tokens", "output_tokens"})
_WRAPPER_BODY_KEYS = ("response", "page", "body")
_WRAPPER_HINT_KEYS = ("endpoint", "url", "path")
#: Provider inference_geo values that mean "unknown" (SPEC §3.2: "not_available" → None).
_NO_GEO = frozenset({"not_available"})


# ---------------------------------------------------------------------------------------------
# errors and small parsers
# ---------------------------------------------------------------------------------------------


class BadRecord(SourceError):
    """A malformed record. ``reason`` is a content-free quarantine code (SPEC §5.1). Adapters
    quarantine it; it subclasses :class:`SourceError` so that, should one ever escape, callers
    still see a content-free ``TokenbillError``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _reject_constant(token: str) -> Any:
    raise ValueError("non-finite number")


def loads(text: str) -> Any:
    """``json.loads`` with exact ``Decimal`` for every non-integer number (money is never a
    float), NaN/Infinity refused; any failure → :class:`BadRecord` ``bad_json``."""
    try:
        return json.loads(text, parse_float=Decimal, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise BadRecord("bad_json") from None


def tokens(value: Any, field_name: str) -> int:
    """A token (or request) count: an int in ``[0, 2**53]``; an integral ``Decimal`` (a JSON
    ``12.0``) is accepted; anything else → ``BadRecord("bad_usage")``."""
    if type(value) is int:
        n = value
    elif isinstance(value, Decimal) and value.is_finite() and value == value.to_integral_value():
        if value.adjusted() > 17:
            raise BadRecord("bad_usage")
        n = int(value)
    else:
        raise BadRecord("bad_usage")
    if n < 0 or n > MAX_TOKENS:
        raise BadRecord("bad_usage")
    return n


def opt_tokens(obj: Mapping[str, Any], key: str) -> int:
    """``tokens(obj[key])`` with a missing or null key counting as 0."""
    value = obj.get(key)
    return 0 if value is None else tokens(value, key)


def req_tokens(obj: Mapping[str, Any], key: str) -> int:
    """``tokens(obj[key])``; a missing key → ``BadRecord("missing:<key>")``."""
    if obj.get(key) is None:
        raise BadRecord(f"missing:{key}")
    return tokens(obj[key], key)


def _clean_text(value: str) -> bool:
    if _CONTROL_RE.search(value):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def label(value: Any, field_name: str, *, max_len: int = MAX_LABEL) -> str | None:
    """A short provider label (model id, tier, cost type, …): None for null/blank; otherwise a
    printable UTF-8 string of at most *max_len* characters or ``BadRecord("bad_type:<field>")``."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise BadRecord(f"bad_type:{field_name}")
    text = value.strip()
    if not text:
        return None
    if len(text) > max_len or not _clean_text(text):
        raise BadRecord(f"bad_type:{field_name}")
    return text


def description(value: Any) -> str:
    """A provider description (cost-report line label, SKU text): control characters and lone
    surrogates rejected, at most 256 characters; null → ``""``."""
    text = label(value, "description", max_len=MAX_DESCRIPTION)
    return text or ""


def model_id(value: Any, provider_hint: str | None = None) -> str | None:
    """The normalized model id (``core.models.normalize_model``); a non-priceable raw id (config
    alias, ``<synthetic>``) is kept verbatim so the row stays attributable."""
    raw = label(value, "model")
    if raw is None:
        return None
    norm = normalize_model(raw, provider_hint)
    return norm.model or raw


def _decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise BadRecord(f"bad_type:{field_name}")
    if isinstance(value, Decimal):
        d = value
    elif isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text or len(text) > 512:
            raise BadRecord(f"bad_type:{field_name}")
        try:
            d = Decimal(text)
        except InvalidOperation:
            raise BadRecord(f"bad_type:{field_name}") from None
    else:
        raise BadRecord(f"bad_type:{field_name}")
    if not d.is_finite():
        raise BadRecord(f"bad_type:{field_name}")
    if not d:
        return Decimal(0)
    if d.adjusted() >= MAX_AMOUNT_EXP or d.as_tuple().exponent < -MAX_FRACTION_DIGITS:  # type: ignore[operator]
        raise BadRecord(f"bad_type:{field_name}")
    return d


def money_scaled(value: Any, field_name: str, *, cents: bool) -> int:
    """An amount (decimal string, int or exact ``Decimal``; cents when *cents*) as an exact integer
    number of 10**-:data:`MONEY_SCALE` USD. Amounts with more than 200 fractional digits or a
    magnitude ≥ 10**30 → ``BadRecord("bad_type:<field>")``."""
    d = _decimal(value, field_name)
    if not d:
        return 0
    sign, digits, exp = d.as_tuple()
    coefficient = int("".join(map(str, digits)))
    shift = int(exp) + MONEY_SCALE - (2 if cents else 0)
    scaled = coefficient * 10**shift
    return -scaled if sign else scaled


def scaled_to_decimal(scaled: int) -> Decimal:
    """The exact USD ``Decimal`` of a scaled amount (no context rounding)."""
    sign = 1 if scaled < 0 else 0
    digits = tuple(int(c) for c in str(abs(scaled)))
    return Decimal((sign, digits, -MONEY_SCALE))


def scaled_to_nano(scaled: int) -> tuple[int, int]:
    """``(nano, remainder_e18)``: half-even to 1e-9 USD through ``core.money.usd_str_to_nano`` (the
    single rounding; cents amounts were shifted to USD exactly, so this equals
    ``core.money.cents_to_nano`` on the cents string) and the remainder in 1e-18 USD units
    (half-even; |remainder| ≤ 5e8)."""
    nano, remainder = usd_str_to_nano(str(scaled_to_decimal(scaled)))
    return nano, remainder_e18(remainder)


def remainder_e18(remainder_usd: Decimal) -> int:
    """A USD remainder (``|r| ≤ 5e-10``) in 1e-18 USD units, rounded half-even."""
    if not remainder_usd:
        return 0
    sign, digits, exp = remainder_usd.as_tuple()
    return decimal_to_nano(Decimal((sign, digits, int(exp) + 9)))


def currency(value: Any) -> None:
    """Only USD amounts are supported (every documented source reports USD)."""
    if not isinstance(value, str) or value.strip().upper() != "USD":
        raise BadRecord("bad_type:currency")


# ---------------------------------------------------------------------------------------------
# timestamps
# ---------------------------------------------------------------------------------------------


def ts_ms(value: Any, field_name: str) -> int:
    """RFC 3339 / ISO 8601 (``Z``, offsets, a bare date, BigQuery ``… UTC``) → int epoch ms (UTC,
    no float); malformed or out of ``[1970, 2**53 ms]`` → ``BadRecord("bad_type:<field>")``."""
    if value is None:
        raise BadRecord(f"missing:{field_name}")
    if not isinstance(value, str) or len(value) > 64:
        raise BadRecord(f"bad_type:{field_name}")
    m = _TS_RE.match(value.strip())
    if m is None:
        raise BadRecord(f"bad_type:{field_name}")
    year, month, day, hh, mi, ss, frac, tz = m.groups()
    try:
        ordinal = _date(int(year), int(month), int(day)).toordinal()
    except ValueError:
        raise BadRecord(f"bad_type:{field_name}") from None
    h, mnt, sec = int(hh or 0), int(mi or 0), int(ss or 0)
    if h > 23 or mnt > 59 or sec > 59:
        raise BadRecord(f"bad_type:{field_name}")
    frac_ms = int((frac or "0").ljust(3, "0")[:3])
    offset_min = 0
    if tz and tz[0] in "+-":
        digits = tz[1:].replace(":", "")
        oh, om = int(digits[:2]), int(digits[2:4] or 0)
        if oh > 23 or om > 59:
            raise BadRecord(f"bad_type:{field_name}")
        offset_min = (oh * 60 + om) * (1 if tz[0] == "+" else -1)
    ms = ((ordinal - _EPOCH_ORDINAL) * DAY_MS + h * 3_600_000 + mnt * 60_000 + sec * 1000
          + frac_ms - offset_min * 60_000)
    if ms < 0 or ms > MAX_TOKENS:
        raise BadRecord(f"bad_type:{field_name}")
    return ms


def unix_s_to_ms(value: Any, field_name: str) -> int:
    """Unix seconds (an int) → epoch ms."""
    if type(value) is not int:
        if value is None:
            raise BadRecord(f"missing:{field_name}")
        raise BadRecord(f"bad_type:{field_name}")
    ms = value * 1000
    if ms < 0 or ms > MAX_TOKENS:
        raise BadRecord(f"bad_type:{field_name}")
    return ms


def date_of(ms: int) -> str:
    """The UTC ``YYYY-MM-DD`` of an epoch-ms timestamp."""
    return _date.fromordinal(_EPOCH_ORDINAL + ms // DAY_MS).isoformat()


def day_start(ms: int) -> int:
    """Start of the UTC day containing *ms*."""
    return ms - ms % DAY_MS


# ---------------------------------------------------------------------------------------------
# documents (pages) on disk
# ---------------------------------------------------------------------------------------------


@dataclass
class Document:
    """One parsed JSON value from a source file (a page, a wrapper or a bare record)."""

    file_index: int
    locator: str
    value: Any                      # parsed JSON, or None when unparseable
    reason: str | None = None       # quarantine reason when value is None


def source_files(path: Path) -> list[Path]:
    """The files of a source: *path* itself, or every regular file below a directory (sorted,
    hidden files, ``MANIFEST.json`` and ``*.md``/``*.py`` skipped)."""
    path = Path(path)
    if path.is_dir():
        return sorted(
            p for p in path.rglob("*")
            if p.is_file() and not p.name.startswith(".") and p.name != "MANIFEST.json"
            and p.suffix.lower() not in (".md", ".py")
        )
    if not path.exists():
        raise SourceError(f"{path.name}: not found")
    return [path]


def read_source_bytes(path: Path, limit: int | None = None) -> bytes | None:
    """The (decompressed) bytes of *path*, or None when larger than *limit* (default
    :data:`MAX_DOC_BYTES`)."""
    if limit is None:
        limit = MAX_DOC_BYTES
    try:
        with open_text(path) as f:
            data = f.read(limit + 1)
    except SourceError:
        raise
    except Exception as exc:  # gzip/zlib/EOF errors of a corrupt stream
        raise SourceError(f"{path.name}: unreadable ({type(exc).__name__})") from None
    return None if len(data) > limit else data


def load_documents(path: Path, file_index: int = 0) -> Iterator[Document]:
    """Parse a file as one JSON value (an object, or an array of objects), else as JSONL. Lines that
    are not JSON become documents with ``value=None`` and a quarantine reason."""
    data = read_source_bytes(path)
    if data is None:
        for line_no, _off, raw in iter_lines(path):
            yield _line_document(file_index, line_no, raw)
        return
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    whole: Any = None
    try:
        whole = loads(data.decode("utf-8"))
    except (BadRecord, UnicodeDecodeError):
        whole = None
    if isinstance(whole, dict):
        yield Document(file_index, "doc", whole)
        return
    if isinstance(whole, list):
        for i, item in enumerate(whole):
            if isinstance(item, dict):
                yield Document(file_index, f"item:{i}", item)
            else:
                yield Document(file_index, f"item:{i}", None, "not_object")
        return
    if whole is not None:  # a bare scalar document
        yield Document(file_index, "doc", None, "not_object")
        return
    for line_no, raw in enumerate(data.split(b"\n"), start=1):
        raw = raw.rstrip(b"\r")
        if not raw.strip():
            continue
        yield _line_document(file_index, line_no, raw)


def _line_document(file_index: int, line_no: int, raw: bytes) -> Document:
    loc = f"line:{line_no}"
    if not raw:
        return Document(file_index, loc, None, "oversize_line")
    try:
        value = loads(raw.decode("utf-8"))
    except (BadRecord, UnicodeDecodeError):
        return Document(file_index, loc, None, "bad_json")
    if not isinstance(value, dict):
        return Document(file_index, loc, None, "not_object")
    return Document(file_index, loc, value)


@dataclass
class Page:
    """A page unwrapped from a recorded-page wrapper, with its classification."""

    body: dict[str, Any]
    kind: str | None
    fetched_ms: int


def unwrap(doc: dict[str, Any]) -> tuple[dict[str, Any], str | None, int]:
    """``(page, endpoint hint, fetched_ms)`` of a document: a recorded-page wrapper holds the page
    under ``response``/``page``/``body`` and may name the endpoint and the fetch time."""
    hint: str | None = None
    fetched = 0
    body = doc
    if "data" not in doc:
        for key in _WRAPPER_BODY_KEYS:
            inner = doc.get(key)
            if isinstance(inner, dict):
                body = inner
                break
    if body is not doc:
        for key in _WRAPPER_HINT_KEYS:
            value = doc.get(key)
            if isinstance(value, str):
                hint = value
                break
        fetched = _fetched_ms(doc)
    else:
        for key in _WRAPPER_HINT_KEYS:
            value = doc.get(key)
            if isinstance(value, str) and value.startswith(("/", "http")):
                hint = value
                break
    if not fetched:
        refreshed = body.get("data_refreshed_at")
        if isinstance(refreshed, str):
            try:
                fetched = ts_ms(refreshed, "data_refreshed_at")
            except BadRecord:
                fetched = 0
    return body, hint, fetched


def _fetched_ms(doc: Mapping[str, Any]) -> int:
    value = doc.get("fetched_ms")
    if type(value) is int and 0 <= value <= MAX_TOKENS:
        return value
    value = doc.get("fetched_at")
    if isinstance(value, str):
        try:
            return ts_ms(value, "fetched_at")
        except BadRecord:
            return 0
    return 0


def _hint_kind(hint: str | None) -> str | None:
    if not hint:
        return None
    for fragment, kind in ENDPOINT_HINTS:
        if fragment in hint:
            return kind
    return None


def _first_dict(items: Any) -> dict[str, Any] | None:
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                return item
    return None


def _first_result(items: list[Any]) -> dict[str, Any] | None:
    for bucket in items:
        if isinstance(bucket, dict):
            result = _first_dict(bucket.get("results"))
            if result is not None:
                return result
    return None


def classify_page(page: Mapping[str, Any], hint: str | None = None) -> str | None:
    """The page kind of a parsed page (or bare record): one of the ``K_*`` constants, or None
    when it cannot be told (e.g. an empty page without an endpoint hint)."""
    kind = _hint_kind(hint)
    if kind is not None:
        return kind
    data = page.get("data")
    items: list[Any] = data if isinstance(data, list) else [page]
    first = _first_dict(items)
    if first is None:
        return None
    top = set(page)
    obj = first.get("object")
    if page.get("object") == "page" or obj == "bucket" or isinstance(obj, str) and obj.startswith(
            "organization."):
        result = _first_result(items) if obj == "bucket" else first
        rtype = result.get("object") if result is not None else None
        if rtype == "organization.costs.result":
            return K_OAI_COSTS
        if isinstance(rtype, str) and rtype.startswith("organization.usage."):
            return K_OAI_USAGE
        return None
    if "actor" in first:
        if "core_metrics" in first or "model_breakdown" in first:
            return K_CC
        if "amount" in first:
            return K_ENT_USER_COST
        if _TOKEN_KEYS & set(first):
            return K_ENT_USER_USAGE
        return None
    if "core_metrics" in first or "model_breakdown" in first:
        return K_CC
    result = _first_result(items) if "results" in first else None
    if result is None:
        return None
    enterprise = bool(({"data_refreshed_at", "organization_id"} & top)
                      or (_ENTERPRISE_MARKERS & set(result)))
    if "amount" in result:
        return K_ENT_COST if enterprise else K_COST
    if _TOKEN_KEYS & set(result):
        return K_ENT_USAGE if enterprise else K_USAGE
    return None


def classify_head(head: bytes) -> str | None:
    """Classify a file from its first ≤ 64 KiB (decompressed): a full parse when the head holds a
    whole document or a whole first JSONL line, else key-name heuristics on the text."""
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    text = head.decode("utf-8", "ignore")
    candidates = [text]
    newline = text.find("\n")
    if newline > 0:
        candidates.append(text[:newline])
    for candidate in candidates:
        try:
            value = loads(candidate)
        except BadRecord:
            continue
        if isinstance(value, list):
            value = _first_dict(value)
        if isinstance(value, dict):
            body, hint, _ = unwrap(value)
            return classify_page(body, hint)
    return _classify_keys(text)


def _classify_keys(text: str) -> str | None:
    """Key-name heuristics for a truncated head (a page larger than the sniff window)."""
    hint = re.search(r'"(?:endpoint|url|path)"\s*:\s*"([^"]{1,256})"', text)
    kind = _hint_kind(hint.group(1)) if hint else None
    if kind is not None:
        return kind
    if '"organization.costs.result"' in text:
        return K_OAI_COSTS
    if re.search(r'"organization\.usage\.[a-z_]+\.result"', text):
        return K_OAI_USAGE
    keys = set(_KEY_RE.findall(text))
    if "actor" in keys:
        if "core_metrics" in keys or "model_breakdown" in keys:
            return K_CC
        if "amount" in keys:
            return K_ENT_USER_COST
        if _TOKEN_KEYS & keys:
            return K_ENT_USER_USAGE
        return None
    if "results" not in keys or not {"starting_at", "ending_at"} & keys:
        return None
    enterprise = bool((_ENTERPRISE_MARKERS | {"organization_id"}) & keys)
    if "amount" in keys:
        return K_ENT_COST if enterprise else K_COST
    if _TOKEN_KEYS & keys:
        return K_ENT_USAGE if enterprise else K_USAGE
    return None


# ---------------------------------------------------------------------------------------------
# accumulation and results
# ---------------------------------------------------------------------------------------------


@dataclass
class _AggCell:
    usage: UsageBuckets = field(default_factory=UsageBuckets)
    cost: int = 0          # scaled USD
    n_cost: int = 0
    listed: int = 0        # scaled USD
    n_list: int = 0
    basis: str | None = None
    fetched_ms: int = 0


@dataclass
class _CostCell:
    amount: int = 0        # scaled USD
    listed: int = 0
    n_list: int = 0
    fetched_ms: int = 0
    rows: int = 0


#: Fields of a cost line that identify it (``line_id``); rows sharing them are summed.
_COST_KEY_FIELDS = ("source_kind", "date_utc", "channel", "workspace_id", "description", "model",
                    "cost_type", "token_type", "sku", "service_tier", "inference_geo",
                    "endpoint_scope", "principal")


class ReadContext:
    """Per-read state shared by every ADMIN adapter: identity helpers, exact accumulation of
    aggregates / cost lines / outcomes, quarantine (lenient or strict), data-quality notes and
    stats. :meth:`result` builds the deterministic :class:`IngestResult`."""

    def __init__(self, adapter: str, path: Path, opts: IngestOptions) -> None:
        if not isinstance(opts, IngestOptions):
            raise UsageError("read() needs IngestOptions")
        if not opts.name_key:
            raise UsageError(f"{adapter}: a name key is required (ids are pseudonymized)")
        self.adapter = adapter
        self.path = Path(path)
        self.opts = opts
        self.files = source_files(self.path)
        self.source_id = pseudonym(opts.name_key, "s", f"{adapter}:{self.path.name}")
        self._name_hmac = pseudonym(opts.name_key, "h", self.path.name)
        self.stats: dict[str, int] = {"files": len(self.files), "records": 0,
                                      "rounding_remainder_e18": 0}
        self.quarantined: list[QuarantineItem] = []
        self._notes: dict[tuple[str, str, str], list[int]] = {}
        self._aggs: dict[tuple[Any, ...], _AggCell] = {}
        self._costs: dict[tuple[Any, ...], _CostCell] = {}
        self.outcomes: list[OutcomeAggregate] = []
        self.used_principal_key = False
        self.team_dims = False
        team_map = dict(opts.team_map)
        self._team_map = team_map
        self._team_map_folded = {k.casefold(): v for k, v in sorted(team_map.items())}
        self._allow = opts.name_allowlist
        #: adapter-private per-read state (e.g. team rollups); adapters stay stateless
        self.state: dict[str, Any] = {}

    # ----- file-level --------------------------------------------------------------------------

    def file_locator(self, file_index: int, locator: str) -> str:
        """``f<i>:<locator>`` for directory sources, the bare locator for one file."""
        return f"f{file_index}:{locator}" if len(self.files) > 1 else locator

    def source_info(self) -> SourceInfo:
        """Content-free source identity: HMACs of the name, a digest of the bytes."""
        digest = hashlib.sha256()
        total = 0
        for f in self.files:
            file_digest = hashlib.sha256()
            try:
                with open(f, "rb") as fh:
                    for chunk in iter(lambda fh=fh: fh.read(1 << 20), b""):
                        file_digest.update(chunk)
                        total += len(chunk)
            except OSError as exc:
                raise SourceError(f"{f.name}: unreadable ({type(exc).__name__})") from None
            if len(self.files) > 1:
                digest.update(f.relative_to(self.path).as_posix().encode("utf-8", "replace"))
                digest.update(b"\0")
                digest.update(file_digest.digest())
            else:
                digest = file_digest
        return SourceInfo(
            source_id=self.source_id, adapter=self.adapter, name_hmac=self._name_hmac,
            sha256=digest.hexdigest(), bytes=total, name_key_id=self.opts.name_key_id,
            principal_key_id=self.opts.principal_key_id if self.used_principal_key else None)

    # ----- quarantine, notes, stats ---------------------------------------------------------

    def quarantine(self, locator: str, reason: str) -> None:
        """Record a malformed record; with ``opts.lenient`` false the first one raises
        :class:`SourceError` naming only the file and the locator."""
        if not self.opts.lenient:
            raise SourceError(f"{self.path.name}: {locator}: {reason}")
        self.quarantined.append(QuarantineItem(source_id=self.source_id, locator=locator,
                                               reason=reason))

    def note(self, code: str, severity: str, detail: str, count: int = 1,
             tokens_: int | None = None) -> None:
        """Accumulate a data-quality note (same code, severity and detail add up)."""
        cell = self._notes.setdefault((code, severity, detail), [0, 0, 0])
        cell[0] += count
        if tokens_ is not None:
            cell[1] += tokens_
            cell[2] = 1

    def stat(self, key: str, n: int = 1) -> None:
        """Add *n* to ``stats[key]``."""
        self.stats[key] = self.stats.get(key, 0) + n

    # ----- identity ---------------------------------------------------------------------------

    def name(self, value: Any, field_name: str) -> str | None:
        """An id pseudonymized under the name key (``h_…``), or verbatim when allowlisted; None
        for null/blank. Ids are never user content, but they are hashed by default (SPEC §5.1)."""
        if value is None:
            return None
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            raise BadRecord(f"bad_type:{field_name}")
        text = value.strip()
        if not text:
            return None
        if len(text) > 1024:
            raise BadRecord(f"bad_type:{field_name}")
        if text in self._allow and len(text) <= MAX_LABEL and _clean_text(text):
            return text
        return pseudonym(self.opts.name_key, "h", text)

    def team_of(self, *refs: str | None) -> str:
        """``opts.team_map`` lookup of the first reference that maps (exact, then case-folded);
        :data:`UNMAPPED_TEAM` otherwise. The references are never stored."""
        for ref in refs:
            if isinstance(ref, str) and ref:
                team = self._team_map.get(ref)
                if team is None:
                    team = self._team_map_folded.get(ref.casefold())
                if team is not None:
                    return team
        return UNMAPPED_TEAM

    def principal(self, raw: str | None) -> str | None:
        """``p_`` pseudonym of a raw central identity under the principal key (identity modes
        ``central-ingest`` and ``install``); None when no principal key is available."""
        if not raw:
            return None
        opts = self.opts
        if opts.principal_key is None or opts.identity_mode not in ("central-ingest", "install"):
            return None
        self.used_principal_key = True
        return pseudonym(opts.principal_key, "p", raw)

    # ----- time -------------------------------------------------------------------------------

    def in_window(self, start_ms: int) -> bool:
        """``opts.since_ms <= start < opts.until_ms`` (unbounded when unset)."""
        o = self.opts
        if o.since_ms is not None and start_ms < o.since_ms:
            return False
        return o.until_ms is None or start_ms < o.until_ms

    def finality(self, end_ms: int) -> str:
        """``final`` once the 30-day revision window after the bucket end has passed relative to
        ``opts.now_ms`` (the injected clock), else ``provisional``."""
        return "final" if self.opts.now_ms >= end_ms + REVISION_WINDOW_MS else "provisional"

    # ----- accumulation -----------------------------------------------------------------------

    def add_aggregate(self, source_kind: str, start_ms: int, end_ms: int,
                      dims: Mapping[str, str | None], usage: UsageBuckets, *,
                      cost: int | None = None, basis: str | None = None,
                      listed: int | None = None, fetched_ms: int = 0) -> bool:
        """Add usage (and optionally a scaled reported/list cost) to the aggregate keyed by
        ``(source_kind, bucket, dims)``; rows sharing the key are summed. False when outside the
        ingest window."""
        if end_ms < start_ms:
            raise BadRecord("bad_type:ending_at")
        if not self.in_window(start_ms):
            self.stat("filtered_by_window")
            return False
        clean = tuple(sorted((k, v) for k, v in dims.items() if v is not None))
        if any(k == "team" for k, _ in clean):
            self.team_dims = True
        key = (source_kind, start_ms, end_ms, clean)
        cell = self._aggs.get(key)
        try:
            total = usage if cell is None else cell.usage + usage
        except Exception:  # noqa: BLE001 - ContractViolation (range) on absurd sums
            raise BadRecord("bad_usage") from None
        if cell is None:
            cell = self._aggs[key] = _AggCell()
        cell.usage = total
        if cost is not None:
            cell.cost += cost
            cell.n_cost += 1
            cell.basis = basis
        if listed is not None:
            cell.listed += listed
            cell.n_list += 1
        cell.fetched_ms = max(cell.fetched_ms, fetched_ms)
        return True

    def add_cost(self, *, source_kind: str, date_utc: str, channel: str, amount: int,
                 listed: int | None = None, workspace_id: str | None = None,
                 description: str = "", model: str | None = None, cost_type: str | None = None,
                 token_type: str | None = None, sku: str | None = None,
                 service_tier: str | None = None, inference_geo: str | None = None,
                 endpoint_scope: str | None = None, principal: str | None = None,
                 fetched_ms: int = 0) -> bool:
        """Add a scaled amount to the cost line keyed by every identifying field; rows sharing the
        key are summed exactly and rounded once. False when outside the ingest window."""
        start = ts_ms(date_utc, "date")
        if not self.in_window(start):
            self.stat("filtered_by_window")
            return False
        key = (source_kind, date_utc, channel, workspace_id, description, model, cost_type,
               token_type, sku, service_tier, inference_geo, endpoint_scope, principal)
        cell = self._costs.get(key)
        if cell is None:
            cell = self._costs[key] = _CostCell()
        cell.amount += amount
        if listed is not None:
            cell.listed += listed
            cell.n_list += 1
        cell.fetched_ms = max(cell.fetched_ms, fetched_ms)
        cell.rows += 1
        return True

    # ----- result -----------------------------------------------------------------------------

    def aggregates(self) -> list[UsageAggregate]:
        """The accumulated aggregates, one per key, sorted (deterministic)."""
        out: list[UsageAggregate] = []
        extra = 0
        for key in sorted(self._aggs):
            source_kind, start, end, dims = key
            cell = self._aggs[key]
            reported = listed = None
            if cell.n_cost:
                reported, rem = scaled_to_nano(cell.cost)
                extra += rem
            if cell.n_list:
                listed, _ = scaled_to_nano(cell.listed)
            out.append(UsageAggregate(
                agg_id=stable_id("ag", source_kind, start, end, *(f"{k}={v}" for k, v in dims)),
                source_kind=source_kind, bucket_start_ms=start, bucket_end_ms=end, dims=dims,
                usage=cell.usage, reported_cost_nano=reported,
                reported_cost_basis=cell.basis if reported is not None else None,
                list_cost_nano=listed, finality=self.finality(end), fetched_ms=cell.fetched_ms))
        if extra:
            self.stat("aggregate_rounding_remainder_e18", extra)
        return out

    def cost_lines(self) -> list[CostLine]:
        """The accumulated cost lines, one per key, rounded once; remainders summed into
        ``stats["rounding_remainder_e18"]``."""
        out: list[CostLine] = []
        remainder = 0
        for key in sorted(self._costs, key=lambda k: tuple("" if v is None else v for v in k)):
            fields = dict(zip(_COST_KEY_FIELDS, key, strict=True))
            cell = self._costs[key]
            nano, rem = scaled_to_nano(cell.amount)
            remainder += rem
            listed = scaled_to_nano(cell.listed)[0] if cell.n_list else None
            day = ts_ms(fields["date_utc"], "date")
            line_id = stable_id("cl", *("" if v is None else v for v in key))
            out.append(CostLine(line_id=line_id, amount_nano=nano, list_amount_nano=listed,
                                currency="USD", finality=self.finality(day + DAY_MS),
                                fetched_ms=cell.fetched_ms, **fields))
        self.stats["rounding_remainder_e18"] += remainder
        return out

    def notes(self) -> list[DataQualityNote]:
        """Accumulated notes plus ``dq.quarantined``, sorted by code."""
        if self.quarantined:
            self.note("dq.quarantined", "warn", "malformed records quarantined (content-free "
                      "reasons in the quarantine list)", len(self.quarantined))
        out = [DataQualityNote(code=code, severity=sev, count=c[0], detail=detail,
                               tokens=c[1] if c[2] else None)
               for (code, sev, detail), c in self._notes.items()]
        return sorted(out, key=lambda n: (n.code, n.severity, n.detail))

    def result(self, declared: frozenset[str]) -> IngestResult:
        """The final :class:`IngestResult`; capabilities are those actually present."""
        aggregates = self.aggregates()
        cost_lines = self.cost_lines()
        outcomes = sorted(self.outcomes, key=lambda o: (o.date_utc, o.source_kind, o.team))
        caps: set[str] = set()
        if aggregates:
            caps.add("aggregates")
        if cost_lines:
            caps.add("cost")
        if outcomes:
            caps.add("outcomes")
        if self.team_dims or outcomes:
            caps.add("attribution.team")
        self.stats.update(aggregates=len(aggregates), cost_lines=len(cost_lines),
                          outcomes=len(outcomes), quarantined=len(self.quarantined))
        notes = self.notes()
        return IngestResult(
            source=self.source_info(), requests=[], sessions=[], events=[],
            aggregates=aggregates, cost_lines=cost_lines, outcomes=outcomes,
            quarantined=list(self.quarantined), notes=notes,
            stats=dict(sorted(self.stats.items())), capabilities=frozenset(caps) & declared)


# ---------------------------------------------------------------------------------------------
# people → teams with k-anonymity (Claude Code Analytics, Enterprise user_* endpoints)
# ---------------------------------------------------------------------------------------------


class TeamRollup:
    """Per-(date, team) accumulation of person-level rows. Actor references are kept only as the
    distinct-user count; :meth:`flush` applies ``core.kanon.merge_small_groups`` per date and hands
    the surviving team cells to the context."""

    def __init__(self, ctx: ReadContext) -> None:
        self.ctx = ctx
        # date -> team -> [users set, outcome counts list | None, cells dict]
        self._days: dict[str, dict[str, list[Any]]] = {}

    def add(self, date_utc: str, team: str, actor: str, *, outcome: tuple[int, ...] | None = None,
            cells: Mapping[str, tuple[Any, ...]] | None = None) -> None:
        """Count *actor* (a raw reference, held only in memory) for ``(date, team)`` and add its
        outcome counts and aggregate cells (``cell key → (UsageBuckets, cost, n_cost, listed,
        n_list)`` with scaled money)."""
        team_cells = self._days.setdefault(date_utc, {}).setdefault(team, [set(), None, {}])
        merged = {key: value if (prev := team_cells[2].get(key)) is None
                  else _add_cell(prev, value) for key, value in (cells or {}).items()}
        team_cells[0].add(actor)            # only after every cell added cleanly
        team_cells[2].update(merged)
        if outcome is not None:
            prev_out = team_cells[1]
            team_cells[1] = outcome if prev_out is None else tuple(
                a + b for a, b in zip(prev_out, outcome, strict=True))

    def flush(self, *, emit: Callable[[str, str, int, tuple[int, ...] | None,
                                        Mapping[str, tuple[Any, ...]]], None]) -> None:
        """Apply k-anonymity per date and call ``emit(date, team, n_users, outcome, cells)`` for
        every published group; dropped groups are counted in ``dq.outcomes_suppressed``."""
        k = self.ctx.opts.k_anonymity
        for day in sorted(self._days):
            teams = self._days[day]
            rows = []
            for team in sorted(teams):
                users, outcome, cells = teams[team]
                rows.append((team, len(users), (outcome is not None, outcome or (), cells)))
            merged, dropped = merge_small_groups(_payload_rows(rows), k=k, other_label=OTHER_TEAM)
            if dropped:
                # The note carries the group count only: a token magnitude of the dropped groups
                # would disclose exactly what the suppression hides (as few as one person).
                small = [r for r in rows if r[1] < k or r[0] == OTHER_TEAM]
                self.ctx.note("dq.outcomes_suppressed", "info",
                              f"team groups below k={k} dropped at ingest (merged group still "
                              f"below k)", dropped)
                self.ctx.stat("groups_dropped", dropped)
                self.ctx.stat("users_dropped", sum(r[1] for r in small))
            for team, n_users, payload in merged:
                has_outcome, outcome, cells = _unpayload(payload)
                if team == OTHER_TEAM:
                    self.ctx.stat("groups_merged_other")
                emit(day, team, n_users, outcome if has_outcome else None, cells)
        self._days.clear()


def _add_cell(a: tuple[Any, ...], b: tuple[Any, ...]) -> tuple[Any, ...]:
    try:
        usage = a[0] + b[0]
    except ContractViolation:  # token sums beyond 2**53
        raise BadRecord("bad_usage") from None
    return (usage, *(x + y for x, y in zip(a[1:], b[1:], strict=True)))


def _payload_rows(rows: list[tuple[str, int, tuple[bool, tuple[int, ...], dict[str, Any]]]]
                  ) -> list[tuple[str, int, tuple[Any, ...]]]:
    """``merge_small_groups`` payloads: (has_outcome count, outcome ints, {cell: tuple}). The flag
    becomes an int so payloads add; outcome tuples are padded to one length."""
    width = max((len(r[2][1]) for r in rows), default=0)
    out = []
    for team, n, (has, outcome, cells) in rows:
        padded = tuple(outcome) + (0,) * (width - len(outcome))
        out.append((team, n, (1 if has else 0, padded, dict(cells))))
    return out


def _unpayload(payload: tuple[Any, ...]) -> tuple[bool, tuple[int, ...], dict[str, Any]]:
    has, outcome, cells = payload
    return bool(has), tuple(outcome), dict(cells)


def cell_key(dims: Mapping[str, str | None]) -> str:
    """A sortable string key for a dims mapping (JSON of the sorted non-null items)."""
    return json.dumps(sorted((k, v) for k, v in dims.items() if v is not None),
                      separators=(",", ":"))


def cell_dims(key: str) -> dict[str, str]:
    """Inverse of :func:`cell_key`."""
    return {k: v for k, v in json.loads(key)}


# ---------------------------------------------------------------------------------------------
# adapter base
# ---------------------------------------------------------------------------------------------


class PageAdapter:
    """Base of the page adapters: sniffing by page classification, reading every document of a
    file (or directory), unwrapping recorded-page wrappers and dispatching pages to
    :meth:`handle_page`. Subclasses set ``name``, ``capabilities`` and ``kinds``."""

    name: str = ""
    capabilities: frozenset[str] = frozenset()
    kinds: frozenset[str] = frozenset()

    def sniff(self, path: Path, head: bytes) -> bool:
        """True when the head of *path* classifies as one of this adapter's page kinds."""
        try:
            return classify_head(head) in self.kinds
        except Exception:  # noqa: BLE001 - a sniffer never raises on hostile input
            return False

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Parse every page of *path* (a file or a directory) into records (SPEC §5.11)."""
        ctx = ReadContext(self.name, Path(path), opts)
        self.begin(ctx)
        for i, f in enumerate(ctx.files):
            for doc in load_documents(f, i):
                loc = ctx.file_locator(doc.file_index, doc.locator)
                if doc.value is None:
                    ctx.quarantine(loc, doc.reason or "bad_json")
                    continue
                body, hint, fetched = unwrap(doc.value)
                kind = classify_page(body, hint)
                if kind is None:
                    if _empty_page(body):
                        ctx.stat("empty_pages")
                        continue
                    if len(ctx.files) > 1:
                        ctx.stat("documents_skipped")
                        continue
                    ctx.quarantine(loc, "bad_type:page")
                    continue
                if kind not in self.kinds:
                    if len(ctx.files) > 1:  # a directory may hold other endpoints' pages
                        ctx.stat("documents_skipped")
                        continue
                    ctx.quarantine(loc, "bad_type:page")
                    continue
                ctx.stat("pages")
                self.handle_page(ctx, kind, Page(body, kind, fetched), loc)
        self.finish(ctx)
        return ctx.result(self.capabilities)

    # hooks -----------------------------------------------------------------------------------

    def begin(self, ctx: ReadContext) -> None:
        """Called once before the first page."""

    def handle_page(self, ctx: ReadContext, kind: str, page: Page, loc: str) -> None:
        """Parse one classified page."""
        raise NotImplementedError

    def finish(self, ctx: ReadContext) -> None:
        """Called once after the last page (k-anonymity flushes here)."""


def _empty_page(body: Mapping[str, Any]) -> bool:
    data = body.get("data")
    if not isinstance(data, list):
        return False
    for item in data:
        if not isinstance(item, dict):
            return False
        results = item.get("results")
        if not (isinstance(results, list) and not results):
            return False
    return True


def page_items(page: Page) -> list[Any]:
    """The items of a page: ``data`` when it is a list, else the page itself as a bare item."""
    data = page.body.get("data")
    if isinstance(data, list):
        return data
    if "data" in page.body:
        return []
    return [page.body]


def iter_bucket_rows(ctx: ReadContext, page: Page, loc: str, *, unix_seconds: bool = False
                     ) -> Iterator[tuple[int, int, str, dict[str, Any]]]:
    """``(start_ms, end_ms, locator, result)`` for every result row of every well-formed time
    bucket of a page, in page order; malformed buckets and non-object results are quarantined
    in place."""
    for bi, bucket in enumerate(page_items(page)):
        bloc = f"{loc}/bucket:{bi}"
        if not isinstance(bucket, dict):
            ctx.quarantine(bloc, "not_object")
            continue
        try:
            if unix_seconds:
                start = unix_s_to_ms(bucket.get("start_time"), "start_time")
                end = unix_s_to_ms(bucket.get("end_time"), "end_time")
            else:
                start = ts_ms(bucket.get("starting_at"), "starting_at")
                end = ts_ms(bucket.get("ending_at"), "ending_at")
            if end < start:
                raise BadRecord("bad_type:ending_at")
            results = bucket.get("results")
            if not isinstance(results, list):
                raise BadRecord("missing:results")
        except BadRecord as exc:
            ctx.quarantine(bloc, exc.reason)
            continue
        for ri, result in enumerate(results):
            rloc = f"{bloc}/result:{ri}"
            if not isinstance(result, dict):
                ctx.quarantine(rloc, "not_object")
                continue
            yield start, end, rloc, result


def guarded(ctx: ReadContext, loc: str, fn: Callable[[], Any]) -> None:
    """Run one record's parser; a :class:`BadRecord` is quarantined at *loc*."""
    try:
        fn()
    except BadRecord as exc:
        ctx.quarantine(loc, exc.reason)
    else:
        ctx.stat("records")


# ---------------------------------------------------------------------------------------------
# Anthropic usage buckets (usage report and Enterprise usage endpoints)
# ---------------------------------------------------------------------------------------------


def anthropic_usage(result: Mapping[str, Any]) -> UsageBuckets:
    """Usage-report token fields → disjoint buckets (convention ``anthropic.usage_report``):
    uncached input, cache read, ``cache_creation`` 5m/1h (an unsplit
    ``cache_creation_input_tokens`` → unknown TTL), output, server-tool requests."""
    uncached = req_tokens(result, "uncached_input_tokens")
    output = req_tokens(result, "output_tokens")
    read = opt_tokens(result, "cache_read_input_tokens")
    w5 = w1 = unknown = 0
    creation = result.get("cache_creation")
    if isinstance(creation, dict):
        w5 = opt_tokens(creation, "ephemeral_5m_input_tokens")
        w1 = opt_tokens(creation, "ephemeral_1h_input_tokens")
    elif creation is not None:
        raise BadRecord("bad_type:cache_creation")
    if result.get("cache_creation_input_tokens") is not None:
        total = tokens(result["cache_creation_input_tokens"], "cache_creation_input_tokens")
        if total < w5 + w1:
            raise BadRecord("bad_usage")
        unknown = total - w5 - w1
    web_search = web_fetch = 0
    server = result.get("server_tool_use")
    if isinstance(server, dict):
        web_search = opt_tokens(server, "web_search_requests")
        web_fetch = opt_tokens(server, "web_fetch_requests")
    elif server is not None:
        raise BadRecord("bad_type:server_tool_use")
    return UsageBuckets(uncached_input=uncached, cache_read=read, cache_write_5m=w5,
                        cache_write_1h=w1, cache_write_unknown=unknown, output=output,
                        web_search_requests=web_search, web_fetch_requests=web_fetch)


def geo(value: Any) -> str | None:
    """``inference_geo`` label; ``not_available`` → None (SPEC §3.2)."""
    text = label(value, "inference_geo")
    return None if text is None or text in _NO_GEO else text


#: Person-level (or person-proxy) result keys: dropped, their rows summed (SPEC §8.5).
PERSON_KEYS = ("account_id", "service_account_id", "user_id", "claude_tag_user_id")


def drop_person_dims(ctx: ReadContext, result: Mapping[str, Any]) -> None:
    """Count person-level grouping values that are dropped from the output."""
    for key in PERSON_KEYS:
        if result.get(key) is not None:
            ctx.stat("person_dims_dropped")


class UsageReportAdapter(PageAdapter):
    """``anthropic-usage-report``: Messages usage report pages → one
    ``UsageAggregate(source_kind="anthropic.usage_report")`` per bucket and group; dims ``channel``
    (``anthropic_api``), ``workspace_id`` and ``api_key_id`` (``h_`` under the name key), ``model``,
    ``service_tier``, ``context_window``, ``inference_geo``, ``speed``."""

    name = "anthropic-usage-report"
    capabilities = frozenset({"aggregates"})
    kinds = frozenset({K_USAGE})
    source_kind = "anthropic.usage_report"

    def handle_page(self, ctx: ReadContext, kind: str, page: Page, loc: str) -> None:
        for start, end, rloc, result in iter_bucket_rows(ctx, page, loc):
            guarded(ctx, rloc, lambda r=result, s=start, e=end: self._row(ctx, page, s, e, r))

    def _row(self, ctx: ReadContext, page: Page, start: int, end: int,
             result: Mapping[str, Any]) -> None:
        usage = anthropic_usage(result)
        dims = {
            "channel": CHANNEL,
            "workspace_id": ctx.name(result.get("workspace_id"), "workspace_id"),
            "api_key_id": ctx.name(result.get("api_key_id"), "api_key_id"),
            "model": model_id(result.get("model")),
            "service_tier": label(result.get("service_tier"), "service_tier"),
            "context_window": label(result.get("context_window"), "context_window"),
            "inference_geo": geo(result.get("inference_geo")),
            "speed": label(result.get("speed"), "speed"),
        }
        drop_person_dims(ctx, result)
        ctx.add_aggregate(self.source_kind, start, end, dims, usage, fetched_ms=page.fetched_ms)


class CostReportAdapter(PageAdapter):
    """``anthropic-cost-report``: cost report pages → ``CostLine(source_kind=
    "anthropic.cost_report", channel="anthropic_api")``; ``amount`` (cents decimal string) exact,
    remainders in ``stats["rounding_remainder_e18"]``; ``workspace_id`` null = default workspace
    (None); rows differing only in ``context_window`` are summed."""

    name = "anthropic-cost-report"
    capabilities = frozenset({"cost"})
    kinds = frozenset({K_COST})
    source_kind = "anthropic.cost_report"

    def handle_page(self, ctx: ReadContext, kind: str, page: Page, loc: str) -> None:
        for start, _end, rloc, result in iter_bucket_rows(ctx, page, loc):
            guarded(ctx, rloc, lambda r=result, s=start: cost_row(
                ctx, self.source_kind, date_of(s), r, page.fetched_ms))


def cost_row(ctx: ReadContext, source_kind: str, day: str, result: Mapping[str, Any],
             fetched_ms: int, *, with_list: bool = False) -> None:
    """One Anthropic cost result (Admin cost report or Enterprise cost report) → cost line."""
    if result.get("amount") is None:
        raise BadRecord("missing:amount")
    amount = money_scaled(result["amount"], "amount", cents=True)
    listed = None
    if with_list and result.get("list_amount") is not None:
        listed = money_scaled(result["list_amount"], "list_amount", cents=True)
    currency(result.get("currency", "USD"))
    drop_person_dims(ctx, result)
    ctx.add_cost(
        source_kind=source_kind, date_utc=day, channel=CHANNEL, amount=amount, listed=listed,
        workspace_id=ctx.name(result.get("workspace_id"), "workspace_id"),
        description=description(result.get("description")),
        model=model_id(result.get("model")),
        cost_type=label(result.get("cost_type"), "cost_type"),
        token_type=label(result.get("token_type"), "token_type"),
        service_tier=label(result.get("service_tier"), "service_tier"),
        inference_geo=geo(result.get("inference_geo")), fetched_ms=fetched_ms)


# ---------------------------------------------------------------------------------------------
# Claude Code Analytics
# ---------------------------------------------------------------------------------------------

#: Actor fields that identify a person (used only for team mapping and distinct counts).
_ACTOR_REF_KEYS = ("email_address", "email", "api_key_name", "user_id", "id")
#: tool_actions keys summed into edits accepted / rejected.
_TOOL_ACTIONS = ("edit_tool", "multi_edit_tool", "write_tool", "notebook_edit_tool")


def actor_refs(record: Mapping[str, Any]) -> tuple[str, list[str]]:
    """``(identity, references)`` of a person-level record's actor: the stable ``user_id`` when
    present (else the first reference) for distinct-user counting, and every reference in lookup
    order for ``opts.team_map``. Held in memory only; ``missing:actor`` when there is none."""
    actor = record.get("actor")
    if not isinstance(actor, dict):
        raise BadRecord("missing:actor")
    refs = [v.strip() for k in _ACTOR_REF_KEYS
            if isinstance(v := actor.get(k), str) and v.strip()]
    if not refs:
        raise BadRecord("missing:actor")
    user_id = actor.get("user_id")
    identity = user_id.strip() if isinstance(user_id, str) and user_id.strip() else refs[0]
    return identity, refs


def _count(obj: Any, *path: str) -> int:
    for key in path:
        if not isinstance(obj, dict):
            if obj is None:
                return 0
            raise BadRecord(f"bad_type:{path[0]}")
        obj = obj.get(key)
    return 0 if obj is None else tokens(obj, path[-1])


def cc_outcome(record: Mapping[str, Any]) -> tuple[int, ...]:
    """``(sessions, commits, pull_requests, lines_added, lines_removed, edits_accepted,
    edits_rejected)`` of one Claude Code Analytics record."""
    core = record.get("core_metrics")
    if core is not None and not isinstance(core, dict):
        raise BadRecord("bad_type:core_metrics")
    tools = record.get("tool_actions")
    if tools is not None and not isinstance(tools, dict):
        raise BadRecord("bad_type:tool_actions")
    accepted = rejected = 0
    for name in _TOOL_ACTIONS:
        accepted += _count(tools, name, "accepted")
        rejected += _count(tools, name, "rejected")
    return (_count(core, "num_sessions"), _count(core, "commits_by_claude_code"),
            _count(core, "pull_requests_by_claude_code"), _count(core, "lines_of_code", "added"),
            _count(core, "lines_of_code", "removed"), accepted, rejected)


def cc_cells(record: Mapping[str, Any]) -> dict[str, tuple[Any, ...]]:
    """``model_breakdown`` → cells ``{model key: (UsageBuckets, estimated cost, n, 0, 0)}``;
    tokens are exclusive (convention ``anthropic.cc_analytics``, **VERIFY** §19.8 #7) and cache
    creation has no TTL split (→ ``cache_write_unknown``)."""
    breakdown = record.get("model_breakdown")
    if breakdown is None:
        return {}
    if not isinstance(breakdown, list):
        raise BadRecord("bad_type:model_breakdown")
    cells: dict[str, tuple[Any, ...]] = {}
    for entry in breakdown:
        if not isinstance(entry, dict):
            raise BadRecord("bad_type:model_breakdown")
        toks = entry.get("tokens") or {}
        if not isinstance(toks, dict):
            raise BadRecord("bad_type:tokens")
        usage = UsageBuckets(uncached_input=opt_tokens(toks, "input"),
                             cache_read=opt_tokens(toks, "cache_read"),
                             cache_write_unknown=opt_tokens(toks, "cache_creation"),
                             output=opt_tokens(toks, "output"))
        cost, n_cost = 0, 0
        est = entry.get("estimated_cost")
        if isinstance(est, dict) and est.get("amount") is not None:
            currency(est.get("currency", "USD"))
            cost, n_cost = money_scaled(est["amount"], "estimated_cost", cents=True), 1
        elif est is not None and not isinstance(est, dict):
            raise BadRecord("bad_type:estimated_cost")
        key = cell_key({"channel": CHANNEL, "model": model_id(entry.get("model"))})
        cell = (usage, cost, n_cost, 0, 0)
        cells[key] = _add_cell(cells[key], cell) if key in cells else cell
    return cells


class ClaudeCodeAnalyticsAdapter(PageAdapter):
    """``anthropic-cc-analytics``: Claude Code Analytics per-user day records → ``(date, team)``
    ``UsageAggregate(source_kind="anthropic.cc_analytics", dims=(channel, model, team))`` (estimated
    cost as ``provider_estimate``) and ``OutcomeAggregate`` rows. Teams come from ``opts.team_map``
    (actor e-mail / API key name → team; unmapped → ``(unmapped)``); groups with fewer than
    ``opts.k_anonymity`` users merge into ``(other)`` or are dropped (``dq.outcomes_suppressed``).
    Nothing is kept per person."""

    name = "anthropic-cc-analytics"
    capabilities = frozenset({"aggregates", "outcomes", "attribution.team"})
    kinds = frozenset({K_CC})
    source_kind = "anthropic.cc_analytics"

    def begin(self, ctx: ReadContext) -> None:
        ctx.state["rollup"] = TeamRollup(ctx)

    def handle_page(self, ctx: ReadContext, kind: str, page: Page, loc: str) -> None:
        for i, record in enumerate(page_items(page)):
            rloc = f"{loc}/record:{i}"
            if not isinstance(record, dict):
                ctx.quarantine(rloc, "not_object")
                continue
            guarded(ctx, rloc, lambda r=record: self._record(ctx, r))

    def _record(self, ctx: ReadContext, record: Mapping[str, Any]) -> None:
        start = day_start(ts_ms(record.get("date"), "date"))
        identity, refs = actor_refs(record)
        outcome = cc_outcome(record)
        cells = cc_cells(record)
        if not ctx.in_window(start):
            ctx.stat("filtered_by_window")
            return
        ctx.state["rollup"].add(date_of(start), ctx.team_of(*refs), identity, outcome=outcome,
                                cells=cells)

    def finish(self, ctx: ReadContext) -> None:
        no_ttl = [0, 0]

        def emit(day: str, team: str, n_users: int, outcome: tuple[int, ...] | None,
                 cells: Mapping[str, tuple[Any, ...]]) -> None:
            start = ts_ms(day, "date")
            for key in sorted(cells):
                usage, cost, n_cost, _l, _n = cells[key]
                dims = {**cell_dims(key), "team": team}
                ctx.add_aggregate(self.source_kind, start, start + DAY_MS, dims, usage,
                                  cost=cost if n_cost else None,
                                  basis="provider_estimate" if n_cost else None)
                if usage.cache_write_unknown:
                    no_ttl[0] += 1
                    no_ttl[1] += usage.cache_write_unknown
            if outcome is not None:
                sessions, commits, prs, added, removed, acc, rej = outcome[:7]
                ctx.outcomes.append(OutcomeAggregate(
                    date_utc=day, team=team, n_users=n_users, sessions=sessions, commits=commits,
                    pull_requests=prs, lines_added=added, lines_removed=removed,
                    edits_accepted=acc, edits_rejected=rej, source_kind=self.source_kind))

        ctx.state["rollup"].flush(emit=emit)
        if no_ttl[0]:
            ctx.note("dq.no_ttl_split", "info", "Claude Code Analytics cache_creation has no "
                     "5m/1h split: writes kept as cache_write_unknown", no_ttl[0], no_ttl[1])


# ---------------------------------------------------------------------------------------------
# Enterprise Analytics
# ---------------------------------------------------------------------------------------------


def enterprise_dims(result: Mapping[str, Any]) -> dict[str, str | None]:
    """Allowlisted Enterprise grouping dims; ``rbac_group_id``, Slack ids and Claude-tag fields are
    dropped (not in the SPEC dims; ``claude_tag_user_id`` is a person)."""
    return {
        "channel": CHANNEL,
        "model": model_id(result.get("model")),
        "product": label(result.get("product"), "product"),
        "speed": label(result.get("speed"), "speed"),
        "inference_geo": geo(result.get("inference_geo")),
        "context_window": label(result.get("context_window"), "context_window"),
    }


class EnterpriseAnalyticsAdapter(PageAdapter):
    """``anthropic-enterprise-analytics``: Claude Enterprise Analytics cost and usage endpoints.

    * ``usage_report`` → ``UsageAggregate(source_kind="anthropic.enterprise_usage")``;
    * ``cost_report`` → ``CostLine(source_kind="anthropic.enterprise_cost")`` with ``amount``
      (post-discount, pre-credit) and ``list_amount``, both cents decimal strings, exact;
    * ``user_usage_report`` / ``user_cost_report`` → aggregated at ingest to ``(date, team)`` with
      k-anonymity (like Claude Code Analytics): ``UsageAggregate`` rows of the team-level source
      kinds ``anthropic.enterprise_team_usage`` / ``anthropic.enterprise_team_cost`` (the latter
      with ``reported_cost_nano`` on basis ``invoice`` and ``list_cost_nano``), kept apart from the
      organization-level kinds so the two families are never added (CONTRACT-CHANGE-ADMIN-1).

    Dates within the 30-day revision window of ``opts.now_ms`` are ``provisional``. Seat-based plans
    report usage credits only (allowance usage has no cost line)."""

    name = "anthropic-enterprise-analytics"
    capabilities = frozenset({"aggregates", "cost", "attribution.team"})
    kinds = frozenset({K_ENT_USAGE, K_ENT_COST, K_ENT_USER_USAGE, K_ENT_USER_COST})
    usage_kind = "anthropic.enterprise_usage"
    cost_kind = "anthropic.enterprise_cost"
    team_usage_kind = "anthropic.enterprise_team_usage"
    team_cost_kind = "anthropic.enterprise_team_cost"

    def begin(self, ctx: ReadContext) -> None:
        ctx.state["usage"] = TeamRollup(ctx)
        ctx.state["cost"] = TeamRollup(ctx)

    def handle_page(self, ctx: ReadContext, kind: str, page: Page, loc: str) -> None:
        if kind == K_ENT_USAGE:
            for start, end, rloc, result in iter_bucket_rows(ctx, page, loc):
                guarded(ctx, rloc, lambda r=result, s=start, e=end:
                        self._usage(ctx, page, s, e, r))
        elif kind == K_ENT_COST:
            for start, _end, rloc, result in iter_bucket_rows(ctx, page, loc):
                guarded(ctx, rloc, lambda r=result, s=start: cost_row(
                    ctx, self.cost_kind, date_of(s), r, page.fetched_ms, with_list=True))
        else:
            rollup = ctx.state["usage"] if kind == K_ENT_USER_USAGE else ctx.state["cost"]
            for i, record in enumerate(page_items(page)):
                rloc = f"{loc}/record:{i}"
                if not isinstance(record, dict):
                    ctx.quarantine(rloc, "not_object")
                    continue
                guarded(ctx, rloc, lambda r=record: self._user(ctx, kind, rollup, r))

    def _usage(self, ctx: ReadContext, page: Page, start: int, end: int,
               result: Mapping[str, Any]) -> None:
        usage = anthropic_usage(result)
        drop_person_dims(ctx, result)
        ctx.add_aggregate(self.usage_kind, start, end, enterprise_dims(result), usage,
                          fetched_ms=page.fetched_ms)

    def _user(self, ctx: ReadContext, kind: str, rollup: TeamRollup,
              record: Mapping[str, Any]) -> None:
        identity, refs = actor_refs(record)
        start = day_start(ts_ms(record.get("starting_at"), "starting_at"))
        dims = enterprise_dims(record)
        drop_person_dims(ctx, record)
        if kind == K_ENT_USER_USAGE:
            cell = (anthropic_usage(record), 0, 0, 0, 0)
        else:
            if record.get("amount") is None:
                raise BadRecord("missing:amount")
            currency(record.get("currency", "USD"))
            amount = money_scaled(record["amount"], "amount", cents=True)
            listed, n_list = 0, 0
            if record.get("list_amount") is not None:
                listed = money_scaled(record["list_amount"], "list_amount", cents=True)
                n_list = 1
            dims["cost_type"] = label(record.get("cost_type"), "cost_type")
            dims["token_type"] = label(record.get("token_type"), "token_type")
            cell = (UsageBuckets(), amount, 1, listed, n_list)
        if not ctx.in_window(start):
            ctx.stat("filtered_by_window")
            return
        rollup.add(date_of(start), ctx.team_of(*refs), identity, cells={cell_key(dims): cell})

    def finish(self, ctx: ReadContext) -> None:
        for rollup, source_kind in ((ctx.state["usage"], self.team_usage_kind),
                                    (ctx.state["cost"], self.team_cost_kind)):
            def emit(day: str, team: str, n_users: int, outcome: tuple[int, ...] | None,
                     cells: Mapping[str, tuple[Any, ...]], _kind: str = source_kind) -> None:
                start = ts_ms(day, "date")
                for key in sorted(cells):
                    usage, cost, n_cost, listed, n_list = cells[key]
                    ctx.add_aggregate(_kind, start, start + DAY_MS,
                                      {**cell_dims(key), "team": team}, usage,
                                      cost=cost if n_cost else None,
                                      basis="invoice" if n_cost else None,
                                      listed=listed if n_list else None)

            rollup.flush(emit=emit)
