"""GitHub Copilot rates: pricing-YAML mini-parser, revision replay, loader and verifier (CP-RATES).

GitHub publishes Copilot's per-token prices in
``github/docs:data/tables/copilot/models-and-pricing.yml`` (a flat YAML list of maps, one map per
model and pricing tier). Token Bill's Copilot rate file
``tokenbill/copilot/data/github_copilot.json`` (schema ``tokenbill/rates@1``, provider ``github``,
channel ``github_copilot``; addendum §6.1) is the replay of the dated revisions of that file. This
module holds everything that reads it, stdlib only:

* :func:`parse_yaml_list` — the mini-parser: a flat list of ``key: value`` maps; plain, single- and
  double-quoted scalars (with escapes), comments and blank lines; anything else (nesting, flow
  collections, block scalars, anchors, tags) is a :class:`~tokenbill.core.errors.PricingError`.
* :func:`quotes_from_maps` — normalization: ``[^…]`` footnotes stripped from model names, names
  mapped to canonical ids with ``core.models.normalize_copilot_model`` (``(fast mode)`` → speed
  ``fast``), ``$`` prices to exact ``Decimal``, ``Not applicable`` → ``None``, ``≤ 272K`` /
  ``> 272K`` thresholds to token counts, ``Default`` / ``Long context`` tiers merged into one
  :class:`Quote` per (model, speed).
* :func:`load_revisions` / :func:`replay` — the dated revisions (``<YYYY-MM-DD>_<sha10>.yml`` named
  by ``commits.txt``: ISO timestamp + full SHA per line) replayed in commit order; a day's state is
  its last commit's; one :class:`Interval` per (model, speed) and price run, ``effective_from``
  clamped to the start of usage-based billing (date source ``B``), else the first day listed (``K``,
  a docs merge date: **VERIFY**), ``effective_to`` exclusive (the first day it is no longer listed
  at those prices).
* :func:`load_layer` — the rate file as a :class:`~tokenbill.core.types.RateLayer`, with the SPEC
  §6.1 load validation (sources, exact decimals, ``published_absolute`` = input × multiplier,
  non-overlapping intervals per (channel, model), promotion ids in
  ``core.catalog.COPILOT_PROMOTIONS``, predicate keys incl. ``routing`` / ``compliance_in``).
* :func:`verify` — ``core.registry.EXTENSIONS["copilot"].rate_verifier`` (``pricing verify``):
  compares the ``github_copilot`` rows in force at the snapshot date with the packaged snapshot
  (a recorded parse of revision ``2026-09-22_d1153b57c9``), a dated revision file, or — with
  ``live=True`` — the raw YAML fetched through an injectable opener.

Discrepancy policy: prices (input, cached input, cache writes, output, the long-context threshold
and band prices), a listed model without a row in force, a row in force for a model the table no
longer lists, and fast-mode entries versus their ``replace_base`` modifier are **authoritative**;
the category, a promotion footnote versus the row's ``promotion``, a missing display-name alias
and a listed model whose row is disabled are warnings (``authoritative=False``). Claude rows'
1-hour write price is not published (DC6: an assumed 2 × input), so it is never compared.
"""

from __future__ import annotations

import datetime as _dt
import email.utils
import hashlib
import importlib.resources
import json
import re
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Any

from tokenbill.core.errors import PricingError, SourceError, UsageError
from tokenbill.core.models import normalize_copilot_model
from tokenbill.core.money import EXACT_CTX, usd
from tokenbill.core.types import Discrepancy, Modifier, RateLayer, RateRow, SourceCitation

__all__ = [
    "BILLING_START",
    "CHANNEL",
    "DOCS_URL",
    "PROVIDER",
    "RATE_FILE",
    "SNAPSHOT_RESOURCE",
    "SNAPSHOT_SCHEMA",
    "YAML_URL",
    "Interval",
    "Quote",
    "Revision",
    "Snapshot",
    "TierPrices",
    "compare",
    "fetch_live",
    "layer_from_json",
    "load_layer",
    "load_revisions",
    "parse_yaml_list",
    "quotes_from_maps",
    "read_snapshot",
    "replay",
    "verify",
]

PROVIDER = "github"
CHANNEL = "github_copilot"
RATES_SCHEMA = "tokenbill/rates@1"
DATA_PACKAGE = "tokenbill.copilot.data"
RATE_FILE = "github_copilot.json"
SNAPSHOT_RESOURCE = "snapshots/models-and-pricing-2026-09-22.json"
SNAPSHOT_SCHEMA = "tokenbill/copilot-pricing-snapshot@1"
DOCS_URL = "https://docs.github.com/en/copilot/reference/copilot-billing/models-and-pricing"
YAML_URL = ("https://raw.githubusercontent.com/github/docs/main/data/tables/copilot/"
            "models-and-pricing.yml")
#: First day of usage-based Copilot billing; earlier revisions only clamp ``effective_from``.
BILLING_START = "2026-06-01"
#: Model-id prefix of the rows whose 1-hour write price is an assumption (DC6; facts
#: ``copilot.write_1h_rule``): never compared with the table.
CLAUDE_PREFIX = "claude-"
LIVE_TIMEOUT_S = 30
USER_AGENT = "tokenbill-pricing-verify"
MAX_YAML_CHARS = 1_048_576

#: Predicate keys a modifier may use: SPEC §6.1 plus ``routing`` / ``compliance_in`` (§6.2 #1).
PREDICATES = frozenset({"service_tier", "speed", "inference_geo", "endpoint_scope", "channel_in",
                        "model_in", "generation_gte", "routing", "compliance_in"})
#: Rate-row features (SPEC §3.5); cache-rule behaviour is never a row property (D28).
ROW_FEATURES = frozenset({"fast_mode", "inference_geo", "1m_context", "batch", "keepalive",
                          "mid_conversation_system", "per_message_effort"})
MULT_BUCKETS = ("cache_read", "cache_write_5m", "cache_write_1h", "cache_write_other")
BAND_BUCKETS = ("input", "output", *MULT_BUCKETS)
MODIFIER_BUCKETS = frozenset({"*", "input", "uncached_input", "output", *MULT_BUCKETS,
                              "web_search"})
ROW_KEYS = frozenset({
    "row_id", "channel", "model", "aliases", "generation", "effective_from", "effective_to",
    "usd_per_mtok", "multipliers", "published_absolute", "min_cacheable_tokens",
    "tokenizer_family", "per_request_usd", "long_context", "promotion", "supports", "enabled",
    "verified_on", "notes", "sources"})
OPTIONAL_ROW_KEYS = frozenset({"cache_write_other_ttl_s"})
MODIFIER_KEYS = frozenset({"modifier_id", "kind", "factor", "base_usd_per_mtok", "applies_to",
                           "when", "stacking", "sources", "notes"})

_NOT_APPLICABLE = frozenset({"not applicable", "n/a", "na", "none", "-", "\u2014", ""})
_KEY_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)[ \t]*:(?:[ \t]+(.*))?\Z")
_FOOTNOTE_RE = re.compile(r"\[\^([^\]]*)\]")
_PRICE_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")
_THRESHOLD_RE = re.compile(
    r"(\u2264|<=|>)\s*([0-9]+(?:\.[0-9]+)?)\s*([KkMm]?)(?:\s*tokens)?\Z")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_COMMIT_RE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_SHA_RE = re.compile(r"[0-9a-f]{40}\Z")
_REVISION_NAME_RE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})_[0-9a-f]{6,40}\Z")
_GENERATION_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*\Z")
_CATEGORY_RE = re.compile(r"(?:^|; )category ([^;]+)")
_PLAIN_FORBIDDEN_START = frozenset("[{|>&*!%@`")
#: YAML white space and line breaks (never the wider Unicode sets of ``str.strip`` /
#: ``str.splitlines``: those characters may sit inside quoted values).
_WS = " \t"
_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")
_ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n", "v": "\v",
            "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"', "/": "/", "\\": "\\",
            "N": "\x85", "_": "\xa0", "L": "\u2028", "P": "\u2029"}
_HEX_ESCAPES = {"x": 2, "u": 4, "U": 8}


# =============================================================================================
# the mini-parser (stage 1: a flat list of maps of strings)
# =============================================================================================


def _fail(lineno: int, what: str) -> PricingError:
    return PricingError(f"pricing YAML: line {lineno}: {what}")


def _double_quoted(text: str, lineno: int) -> tuple[str, str]:
    """The value of the double-quoted scalar at the start of *text* and the rest after it."""
    out: list[str] = []
    i = 1
    while i < len(text):
        ch = text[i]
        if ch == '"':
            return "".join(out), text[i + 1:]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        if i + 1 >= len(text):
            break
        esc = text[i + 1]
        if esc in _ESCAPES:
            out.append(_ESCAPES[esc])
            i += 2
            continue
        width = _HEX_ESCAPES.get(esc)
        digits = text[i + 2:i + 2 + width] if width else ""
        if not width or len(digits) != width or any(c not in "0123456789abcdefABCDEF"
                                                    for c in digits):
            raise _fail(lineno, "unsupported escape in a double-quoted value")
        code = int(digits, 16)
        if code > 0x10FFFF:
            raise _fail(lineno, "escape outside the Unicode range")
        out.append(chr(code))
        i += 2 + width
    raise _fail(lineno, "unterminated double-quoted value")


def _single_quoted(text: str, lineno: int) -> tuple[str, str]:
    """The value of the single-quoted scalar at the start of *text* and the rest after it."""
    out: list[str] = []
    i = 1
    while i < len(text):
        ch = text[i]
        if ch == "'":
            if text[i + 1:i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), text[i + 1:]
        out.append(ch)
        i += 1
    raise _fail(lineno, "unterminated single-quoted value")


def _scalar(raw: str, lineno: int) -> str:
    """One scalar value: quoted (with escapes) or plain (an inline ``#`` comment removed)."""
    text = raw.strip(_WS)
    if text[:1] in ("'", '"'):
        value, rest = (_single_quoted if text[0] == "'" else _double_quoted)(text, lineno)
        rest = rest.strip(_WS)
        if rest and not rest.startswith("#"):
            raise _fail(lineno, "text after a quoted value")
        return value
    cut = re.search(r"(?:^|[ \t])#", text)
    if cut is not None:
        text = text[:cut.start()].rstrip(_WS)
    if text[:1] in _PLAIN_FORBIDDEN_START or text.startswith(("- ", "? ")) or text in ("-", "?"):
        raise _fail(lineno, "unsupported YAML syntax (only flat scalar maps are read)")
    return text


def parse_yaml_list(text: str) -> list[dict[str, str]]:
    """Parse the pricing YAML (a flat list of ``key: value`` maps) into maps of strings.

    Blank lines, comment lines and document markers are skipped; every list item starts with
    ``- key: value`` (or a lone ``-``) and continues with ``key: value`` lines at the item's key
    column. Values are plain scalars (an inline `` #`` comment removed) or single- / double-quoted
    scalars (YAML escapes decoded); an empty value is ``""``. Anything else — nested blocks, flow
    collections, block scalars, anchors, aliases, tags, duplicate keys, tabs in indentation — raises
    :class:`~tokenbill.core.errors.PricingError` naming the line (never the content)."""
    if not isinstance(text, str):
        raise PricingError("pricing YAML: expected text")
    if len(text) > MAX_YAML_CHARS:
        raise PricingError("pricing YAML: document too large")
    items: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    column: int | None = None
    for lineno, raw in enumerate(_LINE_BREAK_RE.split(text), 1):
        line = raw.rstrip(_WS)
        body = line.lstrip(" ")
        if not body or body.startswith("#") or (line in ("---", "...")):
            continue
        indent = len(line) - len(body)
        if body.startswith("\t") or "\t" in line[:indent]:
            raise _fail(lineno, "tab in indentation")
        if indent == 0 and (body == "-" or body.startswith(("- ", "-\t"))):
            current = {}
            items.append(current)
            rest = body[1:].lstrip(" \t")
            if not rest:
                column = None
                continue
            column = len(line) - len(rest)
            key_value = rest
        elif indent > 0 and current is not None:
            if column is None:
                column = indent
            elif indent != column:
                raise _fail(lineno, "unexpected indentation (nested blocks are not supported)")
            key_value = body
        else:
            raise _fail(lineno, "expected a list of maps")
        m = _KEY_RE.match(key_value)
        if m is None:
            raise _fail(lineno, "expected 'key: value'")
        key, value = m.group(1), m.group(2)
        if key in current:
            raise _fail(lineno, "duplicate key in an item")
        current[key] = "" if value is None or not value.strip(_WS) else _scalar(value, lineno)
    return items


# =============================================================================================
# stage 2: quotes per (model, speed)
# =============================================================================================


@dataclass(frozen=True, slots=True)
class TierPrices:
    """USD per million tokens of one pricing tier (``None``: not applicable / not listed)."""

    input: Decimal
    cached_input: Decimal | None
    cache_write: Decimal | None
    output: Decimal


@dataclass(frozen=True, slots=True)
class Quote:
    """One model (and speed) as one revision of the table lists it."""

    model: str                       # canonical id (core.models.normalize_copilot_model)
    speed: str                       # "standard" | "fast"
    display: str                     # the table's model name, footnotes removed
    provider: str
    category: str | None
    release_status: str | None
    footnotes: tuple[str, ...]       # footnote ids of the model name, sorted
    default: TierPrices
    threshold: int | None            # long-context threshold in input tokens (with a band)
    long_context: TierPrices | None  # the "Long context" tier (band rates for the whole request)

    def prices(self) -> tuple[Any, ...]:
        """The price identity (tiers and threshold): what starts a new interval."""
        return (self.default, self.threshold, self.long_context)


def _price(value: str | None, what: str) -> Decimal | None:
    text = (value or "").strip()
    if text.lower() in _NOT_APPLICABLE:
        return None
    if text.startswith("$"):
        text = text[1:].strip()
    text = text.replace(",", "")
    if not _PRICE_RE.match(text) or len(text) > 32:
        raise PricingError(f"pricing YAML: {what} is not a price")
    return Decimal(text)


def _threshold(value: str | None) -> tuple[str | None, int | None]:
    text = (value or "").strip()
    if text.lower() in _NOT_APPLICABLE:
        return None, None
    m = _THRESHOLD_RE.match(text)
    if m is None or len(m.group(2)) > 12:
        raise PricingError("pricing YAML: unsupported threshold")
    scale = {"": 1, "k": 1000, "m": 1_000_000}[m.group(3).lower()]
    tokens = Decimal(m.group(2)) * scale
    if tokens != tokens.to_integral_value() or tokens <= 0:
        raise PricingError("pricing YAML: threshold is not a whole number of tokens")
    return ("gt" if m.group(1) == ">" else "le"), int(tokens)


def _tier(value: str | None) -> str:
    text = " ".join((value or "").replace("_", " ").replace("-", " ").split()).lower()
    if text in ("", "default"):
        return "default"
    if text == "long context":
        return "long_context"
    raise PricingError("pricing YAML: unknown pricing tier")


def _text(item: Mapping[str, str], key: str) -> str | None:
    value = item.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise PricingError(f"pricing YAML: {key} must be text")
    return value.strip() or None


def quotes_from_maps(items: Iterable[Mapping[str, str]]) -> dict[tuple[str, str], Quote]:
    """One :class:`Quote` per (canonical model, speed), in order of first appearance, from the
    maps of :func:`parse_yaml_list` (or a snapshot's recorded entries). Every map needs ``model``,
    ``input`` and ``output``; a model may list one ``Default`` and at most one ``Long context``
    tier (the latter with a ``> N`` threshold equal to the default's ``≤ N``). Inconsistent or
    unreadable entries raise :class:`~tokenbill.core.errors.PricingError`."""
    tiers: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise PricingError("pricing YAML: every entry must be a map")
        name = _text(item, "model")
        if name is None:
            raise PricingError("pricing YAML: an entry has no model")
        footnotes = tuple(_FOOTNOTE_RE.findall(name))
        display = " ".join(_FOOTNOTE_RE.sub("", name).split())
        cm = normalize_copilot_model(display)
        if not cm.model or cm.pseudo is not None or cm.routing != "direct":
            raise PricingError("pricing YAML: an entry names no priceable model")
        tier = _tier(_text(item, "tier"))
        op, threshold = _threshold(_text(item, "threshold"))
        if (tier == "default" and op == "gt") or (tier == "long_context" and op != "gt"):
            raise PricingError(f"pricing YAML: {cm.model}: threshold does not match its tier")
        input_price = _price(_text(item, "input"), f"{cm.model} input")
        output_price = _price(_text(item, "output"), f"{cm.model} output")
        if input_price is None or output_price is None:
            raise PricingError(f"pricing YAML: {cm.model}: input and output prices are required")
        prices = TierPrices(input=input_price,
                            cached_input=_price(_text(item, "cached_input"),
                                                f"{cm.model} cached input"),
                            cache_write=_price(_text(item, "cache_write"),
                                               f"{cm.model} cache write"),
                            output=output_price)
        slot = tiers.setdefault((cm.model, cm.speed), {"footnotes": set()})
        if tier in slot:
            raise PricingError(f"pricing YAML: {cm.model}: tier listed twice")
        slot[tier] = (prices, threshold, item, display)
        slot["footnotes"].update(footnotes)
    quotes: dict[tuple[str, str], Quote] = {}
    for (model, speed), slot in tiers.items():
        if "default" not in slot:
            raise PricingError(f"pricing YAML: {model}: long-context tier without a default tier")
        prices, limit, item, display = slot["default"]
        band = slot.get("long_context")
        if band is not None and limit is not None and limit != band[1]:
            raise PricingError(f"pricing YAML: {model}: tier thresholds disagree")
        quotes[(model, speed)] = Quote(
            model=model, speed=speed, display=display, provider=_text(item, "provider") or "",
            category=_text(item, "category"), release_status=_text(item, "release_status"),
            footnotes=tuple(sorted(slot["footnotes"])), default=prices,
            threshold=band[1] if band is not None else None,
            long_context=band[0] if band is not None else None)
    return quotes


# =============================================================================================
# dated revisions and their replay
# =============================================================================================


@dataclass(frozen=True, slots=True)
class Revision:
    """One dated revision of the pricing YAML."""

    committed: str                   # ISO timestamp of the docs commit (UTC)
    sha: str                         # full commit SHA
    quotes: Mapping[tuple[str, str], Quote]

    @property
    def date(self) -> str:
        """The UTC date of the commit."""
        return self.committed[:10]

    @property
    def name(self) -> str:
        """``<YYYY-MM-DD>_<sha10>`` (the fixture file stem and the ``yml`` source reference)."""
        return f"{self.date}_{self.sha[:10]}"


@dataclass(frozen=True, slots=True)
class Interval:
    """A replayed price run of one (model, speed): the rows of the rate file."""

    model: str
    speed: str
    quote: Quote                     # as listed on the interval's first day
    latest: Quote                    # as listed on its last day (same prices; category may move)
    first_seen: str                  # first day listed at these prices (unclamped)
    effective_from: str              # clamped to BILLING_START
    effective_to: str | None         # exclusive; None = still listed by the last revision
    date_source: str                 # "B" (listed before billing started) | "K" (docs commit)
    to_source: str | None            # "K" when a later revision ended it, else None
    since: str                       # revision whose default-tier prices the interval carries
    band_since: str | None           # revision that listed its long-context band (None: no band)


def load_revisions(directory: Path) -> tuple[Revision, ...]:
    """The dated revisions in *directory* (``commits.txt`` + ``<YYYY-MM-DD>_<sha10>.yml``), in
    commit order. Every listed commit must have its file and every ``.yml`` file must be listed."""
    directory = Path(directory)
    try:
        lines = (directory / "commits.txt").read_text(encoding="utf-8").splitlines()
        present = {p.name for p in directory.glob("*.yml")}
    except OSError as exc:
        raise SourceError(f"pricing revisions: cannot read commits.txt ({type(exc).__name__})") \
            from None
    commits: list[tuple[str, str]] = []
    for lineno, line in enumerate(lines, 1):
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 2 or not _COMMIT_RE.match(parts[0]) or not _SHA_RE.match(parts[1]):
            raise PricingError(f"pricing revisions: commits.txt line {lineno} is malformed")
        commits.append((parts[0], parts[1]))
    names = [f"{ts[:10]}_{sha[:10]}.yml" for ts, sha in commits]
    if len(set(names)) != len(names) or set(names) != present:
        raise PricingError("pricing revisions: commits.txt and the .yml files disagree")
    revisions = []
    for (ts, sha), name in sorted(zip(commits, names, strict=True)):
        try:
            text = (directory / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SourceError(f"pricing revisions: {name} unreadable ({type(exc).__name__})") \
                from None
        revisions.append(Revision(committed=ts, sha=sha,
                                  quotes=quotes_from_maps(parse_yaml_list(text))))
    return tuple(revisions)


@dataclass(frozen=True, slots=True)
class _Run:
    quote: Quote
    since: str
    band_since: str


def replay(revisions: Sequence[Revision], *, billing_start: str = BILLING_START
           ) -> tuple[Interval, ...]:
    """Replay *revisions* (any order; sorted by commit) into price intervals.

    A day's state is the state of its last commit (a price listed and withdrawn on the same day
    never bills). A (model, speed) interval starts on the first day its prices are listed and ends
    (exclusive) on the first day they are not; intervals ending on or before *billing_start* are
    dropped and earlier starts are clamped to it (date source ``B``). Intervals are ordered by
    model order of first appearance, then by date."""
    ordered = sorted(revisions, key=lambda r: (r.committed, r.sha))
    days: list[tuple[str, dict[tuple[str, str], _Run]]] = []
    runs: dict[tuple[str, str], _Run] = {}
    keys: dict[tuple[str, str], None] = {}
    for rev in ordered:
        nxt: dict[tuple[str, str], _Run] = {}
        for key, quote in rev.quotes.items():
            keys.setdefault(key)
            prev = runs.get(key)
            same_default = prev is not None and prev.quote.default == quote.default
            same_band = prev is not None and (prev.quote.threshold, prev.quote.long_context) == (
                quote.threshold, quote.long_context)
            nxt[key] = _Run(quote=quote,
                            since=prev.since if same_default and prev else rev.name,
                            band_since=prev.band_since if same_band and prev else rev.name)
        runs = nxt
        if days and days[-1][0] == rev.date:
            days[-1] = (rev.date, runs)
        else:
            days.append((rev.date, runs))
    out: list[Interval] = []
    for key in keys:
        start: tuple[str, _Run] | None = None
        latest: Quote | None = None
        for date, state in days:
            run = state.get(key)
            if start is not None and (run is None or run.quote.prices() != start[1].quote.prices()):
                out.extend(_interval(key, start, latest, date, billing_start))
                start = None
            if run is not None and start is None:
                start = (date, run)
            if run is not None:
                latest = run.quote
        if start is not None:
            out.extend(_interval(key, start, latest, None, billing_start))
    return tuple(out)


def _interval(key: tuple[str, str], start: tuple[str, _Run], latest: Quote | None,
              end: str | None, billing_start: str) -> list[Interval]:
    first, run = start
    if end is not None and end <= billing_start:
        return []
    return [Interval(model=key[0], speed=key[1], quote=run.quote, latest=latest or run.quote,
                     first_seen=first,
                     effective_from=max(first, billing_start),
                     effective_to=end, date_source="B" if first < billing_start else "K",
                     to_source="K" if end is not None else None, since=run.since,
                     band_since=run.band_since if run.quote.long_context is not None else None)]


# =============================================================================================
# the rate file (tokenbill/rates@1) → RateLayer, with load validation
# =============================================================================================


def _reject_float(token: str) -> Any:
    raise PricingError("rate file: JSON floats are not allowed (use decimal strings)")


def _row_fail(row_id: str, what: str) -> PricingError:
    return PricingError(f"rate file: row {row_id}: {what}")


def _dec(value: Any, where: str) -> Decimal:
    if not isinstance(value, str):
        raise PricingError(f"rate file: {where} must be a decimal string")
    try:
        d = usd(value)
    except (TypeError, ValueError, ArithmeticError):
        raise PricingError(f"rate file: {where} is not a decimal") from None
    if d < 0:
        raise PricingError(f"rate file: {where} is negative")
    return d


def _pairs(obj: Any, allowed: Iterable[str], where: str) -> tuple[tuple[str, Decimal], ...]:
    if not isinstance(obj, Mapping):
        raise PricingError(f"rate file: {where} must be an object")
    unknown = set(obj) - set(allowed)
    if unknown:
        raise PricingError(f"rate file: {where} has an unknown bucket")
    return tuple(sorted((k, _dec(v, where)) for k, v in obj.items()))


def _date(value: Any, where: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise PricingError(f"rate file: {where} must be a YYYY-MM-DD date")
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        raise PricingError(f"rate file: {where} is not a calendar date") from None
    return value


def _citations(items: Any, where: str) -> tuple[SourceCitation, ...]:
    if not isinstance(items, list) or not items:
        raise PricingError(f"rate file: {where} needs at least one source")
    out = []
    for s in items:
        if not isinstance(s, Mapping) or not isinstance(s.get("url"), str) or not s["url"]:
            raise PricingError(f"rate file: {where} has a source without a url")
        finding = s.get("finding")
        if finding is not None and not isinstance(finding, str):
            raise PricingError(f"rate file: {where} has a malformed source finding")
        out.append(SourceCitation(url=s["url"], retrieved=str(_date(s.get("retrieved"), where)),
                                  finding=finding))
    return tuple(out)


def _strings(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise PricingError(f"rate file: {where} must be a list of strings")
    return tuple(value)


def _times(a: Decimal, b: Decimal, where: str) -> Decimal:
    try:
        return EXACT_CTX.multiply(a, b)
    except (ArithmeticError, DecimalException):
        raise PricingError(f"rate file: {where}: inexact derivation") from None


def _row(r: Any, provider: str) -> RateRow:
    if not isinstance(r, Mapping):
        raise PricingError("rate file: every row must be an object")
    row_id = r.get("row_id") if isinstance(r.get("row_id"), str) else "?"
    missing = ROW_KEYS - set(r)
    if missing or set(r) - ROW_KEYS - OPTIONAL_ROW_KEYS:
        raise _row_fail(row_id, "missing or unknown keys")
    model, channel = r["model"], r["channel"]
    if not isinstance(model, str) or not model or not isinstance(channel, str) or not channel:
        raise _row_fail(row_id, "model and channel are required")
    start = _date(r["effective_from"], f"row {row_id} effective_from")
    end = _date(r["effective_to"], f"row {row_id} effective_to", optional=True)
    if row_id != f"{provider}/{channel}/{model}/{start}":
        raise _row_fail(row_id, "row_id is not <provider>/<channel>/<model>/<effective_from>")
    if end is not None and end <= str(start):
        raise _row_fail(row_id, "effective_to must be after effective_from")
    prices = _pairs(r["usd_per_mtok"], ("input", "output"), f"row {row_id} usd_per_mtok")
    base = dict(prices)
    if set(base) != {"input", "output"}:
        raise _row_fail(row_id, "usd_per_mtok needs input and output")
    mult = dict(_pairs(r["multipliers"], MULT_BUCKETS, f"row {row_id} multipliers"))
    published = _pairs(r["published_absolute"], MULT_BUCKETS, f"row {row_id} published_absolute")
    for bucket, value in published:
        if bucket not in mult or _times(base["input"], mult[bucket], row_id) != value:
            raise _row_fail(row_id, f"published {bucket} != input x multiplier")
    lc = r["long_context"]
    threshold: int | None = None
    band: tuple[tuple[str, Decimal], ...] = ()
    if lc is not None:
        if not isinstance(lc, Mapping) or set(lc) != {"threshold", "usd_per_mtok"}:
            raise _row_fail(row_id, "long_context must be {threshold, usd_per_mtok}")
        threshold = lc["threshold"]
        if type(threshold) is not int or threshold <= 0:
            raise _row_fail(row_id, "long_context.threshold must be a positive int")
        band = _pairs(lc["usd_per_mtok"], BAND_BUCKETS, f"row {row_id} long_context")
        if {k for k, _ in band} != {"input", "output", *mult}:
            raise _row_fail(row_id, "long_context must price exactly the row's buckets")
    supports = _strings(r["supports"], f"row {row_id} supports") if r["supports"] else ()
    if set(supports) - ROW_FEATURES:
        raise _row_fail(row_id, "supports names an unknown or cache-rule feature")
    generation = r["generation"]
    if not isinstance(generation, str) or not _GENERATION_RE.match(generation):
        raise _row_fail(row_id, "generation must be a dotted number")
    mct = r["min_cacheable_tokens"]
    if mct is not None and (type(mct) is not int or mct < 0):
        raise _row_fail(row_id, "min_cacheable_tokens must be a non-negative int or null")
    ttl = r.get("cache_write_other_ttl_s")
    if ttl is not None and (type(ttl) is not int or ttl <= 0):
        raise _row_fail(row_id, "cache_write_other_ttl_s must be a positive int")
    if type(r["enabled"]) is not bool or not isinstance(r["tokenizer_family"], str) \
            or not r["tokenizer_family"] or not isinstance(r["notes"], str):
        raise _row_fail(row_id, "enabled, tokenizer_family and notes are malformed")
    promotion = r["promotion"]
    if promotion is not None and not isinstance(promotion, str):
        raise _row_fail(row_id, "promotion must be an id or null")
    aliases = _strings(r["aliases"], f"row {row_id} aliases") if r["aliases"] else ()
    return RateRow(
        row_id=row_id, provider=provider, channel=channel, model=model, aliases=aliases,
        generation=generation, effective_from=str(start), effective_to=end,
        input_usd_per_mtok=base["input"], output_usd_per_mtok=base["output"],
        cache_read_mult=mult.get("cache_read"), cache_write_5m_mult=mult.get("cache_write_5m"),
        cache_write_1h_mult=mult.get("cache_write_1h"),
        cache_write_other_mult=mult.get("cache_write_other"), cache_write_other_ttl_s=ttl,
        published_absolute=published, min_cacheable_tokens=mct,
        tokenizer_family=r["tokenizer_family"],
        per_request_usd=_pairs(r["per_request_usd"], ("web_search",),
                               f"row {row_id} per_request_usd"),
        long_context_threshold=threshold, long_context_usd_per_mtok=band, supports=supports,
        enabled=r["enabled"], verified_on=str(_date(r["verified_on"], f"row {row_id} verified_on")),
        sources=_citations(r["sources"], f"row {row_id}"), promotion=promotion, notes=r["notes"])


def _modifier(m: Any) -> Modifier:
    if not isinstance(m, Mapping) or not isinstance(m.get("modifier_id"), str):
        raise PricingError("rate file: every modifier needs a modifier_id")
    mid = m["modifier_id"]
    if not {"modifier_id", "kind", "applies_to", "when", "stacking", "sources"} <= set(m) \
            or set(m) - MODIFIER_KEYS:
        raise PricingError(f"rate file: modifier {mid}: missing or unknown keys")
    kind = m["kind"]
    factor = None
    base: tuple[tuple[str, Decimal], ...] = ()
    if kind == "multiply":
        factor = _dec(m.get("factor"), f"modifier {mid} factor")
        if m.get("base_usd_per_mtok"):
            raise PricingError(f"rate file: modifier {mid}: multiply takes no base prices")
    elif kind == "replace_base":
        if m.get("factor") is not None:
            raise PricingError(f"rate file: modifier {mid}: replace_base takes no factor")
        base = _pairs(m.get("base_usd_per_mtok"), ("input", "output"), f"modifier {mid}")
        if {k for k, _ in base} != {"input", "output"}:
            raise PricingError(f"rate file: modifier {mid}: replace_base needs input and output")
    else:
        raise PricingError(f"rate file: modifier {mid}: unknown kind")
    when = m["when"]
    if not isinstance(when, Mapping) or set(when) - PREDICATES \
            or not all(isinstance(v, str) and v for v in when.values()):
        raise PricingError(f"rate file: modifier {mid}: unknown predicate key")
    applies = _strings(m["applies_to"], f"modifier {mid} applies_to")
    if not applies or set(applies) - MODIFIER_BUCKETS:
        raise PricingError(f"rate file: modifier {mid}: unknown bucket")
    if m["stacking"] not in ("documented", "assumed"):
        raise PricingError(f"rate file: modifier {mid}: stacking must be documented|assumed")
    if "notes" in m and not isinstance(m["notes"], str):
        raise PricingError(f"rate file: modifier {mid}: notes must be text")
    return Modifier(modifier_id=mid, kind=kind, factor=factor, base_usd_per_mtok=base,
                    applies_to=applies, when=tuple(sorted(when.items())),
                    stacking=m["stacking"], sources=_citations(m["sources"], f"modifier {mid}"))


def _check_promotions(rows: Sequence[RateRow]) -> None:
    from tokenbill.core.catalog import COPILOT_PROMOTIONS, PROMOTIONS  # facts-backed tables

    known = {p.promotion_id: p for p in (*PROMOTIONS, *COPILOT_PROMOTIONS)}
    for row in rows:
        if row.promotion is None:
            continue
        promo = known.get(row.promotion)
        if promo is None or promo.model != row.model or promo.channel != row.channel:
            raise _row_fail(row.row_id, "promotion id unknown or for another model / channel")


def _check_overlaps(rows: Sequence[RateRow]) -> None:
    by_key: dict[tuple[str, str], list[RateRow]] = {}
    for row in rows:
        by_key.setdefault((row.channel, row.model), []).append(row)
    for group in by_key.values():
        group.sort(key=lambda r: r.effective_from)
        for prev, cur in zip(group, group[1:], strict=False):
            if prev.effective_to is None or prev.effective_to > cur.effective_from:
                raise _row_fail(cur.row_id, "overlaps an earlier interval of the same model")


def layer_from_json(doc: Any, *, name: str, channels: Iterable[str] = (CHANNEL,)) -> RateLayer:
    """Validate a ``tokenbill/rates@1`` document (SPEC §6.1 load rules) and return its layer.

    Rows and modifiers are checked as documented in the module docstring; the provider comes from
    the file (``github``) and every row's channel must be one of *channels*. ``sha256`` is the
    SHA-256 of the document's canonical JSON (sorted keys, compact separators)."""
    if not isinstance(doc, Mapping) or doc.get("schema") != RATES_SCHEMA:
        raise PricingError(f"rate file: schema must be {RATES_SCHEMA}")
    provider, as_of = doc.get("provider"), doc.get("as_of")
    if not isinstance(provider, str) or not provider or not isinstance(doc.get("rows"), list) \
            or not isinstance(doc.get("modifiers"), list):
        raise PricingError("rate file: provider, rows and modifiers are required")
    rows = tuple(_row(r, provider) for r in doc["rows"])
    allowed = set(channels)
    for row in rows:
        if row.channel not in allowed:
            raise _row_fail(row.row_id, "channel not served by this file")
    if len({r.row_id for r in rows}) != len(rows):
        raise PricingError("rate file: duplicate row_id")
    _check_overlaps(rows)
    _check_promotions(rows)
    modifiers = tuple(_modifier(m) for m in doc["modifiers"])
    if len({m.modifier_id for m in modifiers}) != len(modifiers):
        raise PricingError("rate file: duplicate modifier_id")
    models = {r.model for r in rows}
    for m in modifiers:
        when = dict(m.when)
        if not set(when.get("channel_in", "").split(",")) <= allowed:
            raise PricingError(f"rate file: modifier {m.modifier_id}: channel_in must be limited "
                               "to the file's channels")
        if "model_in" in when and not set(when["model_in"].split(",")) <= models:
            raise PricingError(f"rate file: modifier {m.modifier_id}: model_in names no row")
    canonical = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return RateLayer(name=name, schema=RATES_SCHEMA, as_of=str(_date(as_of, "as_of")), rows=rows,
                     modifiers=modifiers,
                     sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest())


def _loads(text: str, what: str) -> Any:
    try:
        return json.loads(text, parse_float=_reject_float, parse_constant=_reject_float)
    except (ValueError, RecursionError):
        raise PricingError(f"{what}: invalid JSON") from None


def load_layer(path: Path | None = None) -> RateLayer:
    """The Copilot rate file as a validated :class:`~tokenbill.core.types.RateLayer`: the
    packaged ``github_copilot.json`` (layer ``builtin@<as_of>``) or the file at *path* (layer
    ``user:<file name>``)."""
    if path is None:
        text = importlib.resources.files(DATA_PACKAGE).joinpath(RATE_FILE).read_text(
            encoding="utf-8")
        doc = _loads(text, "rate file")
        return layer_from_json(doc, name=f"builtin@{doc.get('as_of')}")
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SourceError(f"rate file {Path(path).name}: unreadable ({type(exc).__name__})") \
            from None
    return layer_from_json(_loads(text, "rate file"), name=f"user:{Path(path).name}")


# =============================================================================================
# verification
# =============================================================================================


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A recorded parse of the pricing YAML: the maps of :func:`parse_yaml_list` and their date."""

    date: str                        # the date the table is compared at (rows in force then)
    label: str                       # Discrepancy.source
    entries: tuple[Mapping[str, str], ...]


def read_snapshot(path: Path | None = None) -> Snapshot:
    """The packaged snapshot (``snapshots/models-and-pricing-2026-09-22.json``), a snapshot JSON
    file (schema ``tokenbill/copilot-pricing-snapshot@1``) or a dated revision file
    ``<YYYY-MM-DD>_<sha>.yml`` (compared at its file-name date)."""
    if path is None:
        text = importlib.resources.files(DATA_PACKAGE).joinpath(SNAPSHOT_RESOURCE).read_text(
            encoding="utf-8")
        return _snapshot_doc(_loads(text, "pricing snapshot"))
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SourceError(f"pricing snapshot {p.name}: unreadable ({type(exc).__name__})") \
            from None
    if p.suffix.lower() in (".yml", ".yaml"):
        m = _REVISION_NAME_RE.match(p.stem)
        if m is None:
            raise UsageError("a YAML snapshot must be named <YYYY-MM-DD>_<sha>.yml")
        date = str(_date(m.group(1), "snapshot date"))
        return Snapshot(date=date, label=f"github/docs models-and-pricing.yml {p.stem}",
                        entries=tuple(parse_yaml_list(text)))
    return _snapshot_doc(_loads(text, "pricing snapshot"))


def _snapshot_doc(doc: Any) -> Snapshot:
    if not isinstance(doc, Mapping) or doc.get("schema") != SNAPSHOT_SCHEMA:
        raise PricingError(f"pricing snapshot: schema must be {SNAPSHOT_SCHEMA}")
    entries = doc.get("entries")
    revision = doc.get("revision")
    if not isinstance(entries, list) or not isinstance(revision, str) or not all(
            isinstance(e, Mapping) and all(isinstance(k, str) and isinstance(v, str)
                                           for k, v in e.items()) for e in entries):
        raise PricingError("pricing snapshot: revision and entries (maps of text) are required")
    date = str(_date(doc.get("date"), "pricing snapshot date"))
    return Snapshot(date=date, label=f"github/docs models-and-pricing.yml {revision} (snapshot)",
                    entries=tuple(entries))


def _response_date(response: Any) -> str:
    headers = getattr(response, "headers", None)
    value = headers.get("Date") if headers is not None and hasattr(headers, "get") else None
    if isinstance(value, str):
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            when = None
        if when is not None:
            if when.tzinfo is None:
                when = when.replace(tzinfo=_dt.timezone.utc)
            return when.astimezone(_dt.timezone.utc).date().isoformat()
    return _dt.datetime.now(_dt.timezone.utc).date().isoformat()


def fetch_live(*, opener: Any = None, url: str = YAML_URL) -> tuple[str, str]:
    """Fetch the raw pricing YAML (opt-in network; TLS via ``urllib``; 30 s timeout) and return
    ``(text, date)``, *date* being the UTC date of the response's ``Date`` header (else today).

    *opener* is ``urllib.request.urlopen``-like — a callable ``opener(request, timeout=…)`` or an
    object with ``open(request, timeout=…)`` (an ``OpenerDirector``) — returning a response with
    ``read()``; tests inject a fake (the socket guard proves no network is touched). Transport
    failures and non-200 responses raise :class:`~tokenbill.core.errors.SourceError`."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "text/plain"})
    op = urllib.request.urlopen if opener is None else opener
    try:
        response = (op.open(request, timeout=LIVE_TIMEOUT_S) if hasattr(op, "open")
                    else op(request, timeout=LIVE_TIMEOUT_S))
        try:
            status = getattr(response, "status", None)
            if status is None:
                status = getattr(response, "code", None)
            if status is not None and status != 200:
                raise SourceError(f"pricing verify --live: HTTP status {int(status)}")
            body = response.read(MAX_YAML_CHARS * 4 + 1)
            date = _response_date(response)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    except (OSError, ValueError, TypeError) as exc:
        raise SourceError(f"pricing verify --live: fetch failed ({type(exc).__name__})") from None
    if not isinstance(body, bytes) or len(body) > MAX_YAML_CHARS * 4:
        raise SourceError("pricing verify --live: response is not a pricing YAML document")
    try:
        return body.decode("utf-8"), date
    except UnicodeDecodeError:
        raise SourceError("pricing verify --live: response is not UTF-8") from None


def _fmt(value: Decimal | int | None) -> str:
    if value is None:
        return "not applicable"
    if isinstance(value, int):
        return str(value)
    return format(value.normalize(), "f")


def _category(notes: str) -> str | None:
    m = _CATEGORY_RE.search(notes)
    return m.group(1).strip() if m else None


def _in_force(row: RateRow, date: str) -> bool:
    return row.effective_from <= date and (row.effective_to is None or date < row.effective_to)


def _rate_of(base: Decimal, mult: Decimal | None, row_id: str) -> Decimal | None:
    return None if mult is None else _times(base, mult, row_id)


def _compare_prices(out: list[Discrepancy], row_id: str, field_prefix: str,
                    ours: Mapping[str, Decimal | None], theirs: TierPrices, *, claude: bool,
                    source: str) -> None:
    pairs: list[tuple[str, Decimal | None, Decimal | None]] = [
        ("input", ours.get("input"), theirs.input),
        ("cache_read", ours.get("cache_read"), theirs.cached_input),
        ("cache_write_5m", ours.get("cache_write_5m"), theirs.cache_write),
        ("output", ours.get("output"), theirs.output),
    ]
    if not claude:  # one published write price in both TTL classes (DC6)
        pairs.append(("cache_write_1h", ours.get("cache_write_1h"), theirs.cache_write))
    elif theirs.cache_write is None and ours.get("cache_write_1h") is not None:
        pairs.append(("cache_write_1h", ours.get("cache_write_1h"), None))
    for field, a, b in pairs:
        if a != b:
            out.append(Discrepancy(row_id=row_id, field=field_prefix + field, ours=_fmt(a),
                                   theirs=_fmt(b), source=source, authoritative=True))


def _compare_row(out: list[Discrepancy], row: RateRow, quote: Quote, source: str) -> None:
    rid = row.row_id
    claude = row.model.startswith(CLAUDE_PREFIX)
    base = row.input_usd_per_mtok
    ours: dict[str, Decimal | None] = {
        "input": base, "output": row.output_usd_per_mtok,
        "cache_read": _rate_of(base, row.cache_read_mult, rid),
        "cache_write_5m": _rate_of(base, row.cache_write_5m_mult, rid),
        "cache_write_1h": _rate_of(base, row.cache_write_1h_mult, rid)}
    _compare_prices(out, rid, "", ours, quote.default, claude=claude, source=source)
    if row.long_context_threshold != quote.threshold:
        out.append(Discrepancy(rid, "long_context.threshold", _fmt(row.long_context_threshold),
                               _fmt(quote.threshold), source, True))
    if quote.long_context is not None and row.long_context_threshold is not None:
        band = dict(row.long_context_usd_per_mtok)
        _compare_prices(out, rid, "long_context.", {k: band.get(k) for k in BAND_BUCKETS},
                        quote.long_context, claude=claude, source=source)
    category = _category(row.notes)
    if quote.category is not None and category != quote.category:
        out.append(Discrepancy(rid, "category", category or "not recorded", quote.category,
                               source, False))
    promo_note = any("promo" in f.lower() for f in quote.footnotes)
    if promo_note != (row.promotion is not None):
        out.append(Discrepancy(rid, "promotion", row.promotion or "none",
                               "promotion footnote" if promo_note else "no promotion footnote",
                               source, False))
    if quote.display not in row.aliases:
        out.append(Discrepancy(rid, "aliases", ", ".join(row.aliases) or "none", quote.display,
                               source, False))
    if not row.enabled:
        out.append(Discrepancy(rid, "enabled", "false", "listed", source, False))


def _fast_modifier(modifiers: Iterable[Modifier], model: str) -> Modifier | None:
    for m in modifiers:
        when = dict(m.when)
        if m.kind == "replace_base" and when.get("speed") == "fast" \
                and model in when.get("model_in", "").split(",") \
                and CHANNEL in when.get("channel_in", "").split(","):
            return m
    return None


def _compare_fast(out: list[Discrepancy], row: RateRow, quote: Quote, mod: Modifier | None,
                  source: str) -> None:
    if mod is None:
        out.append(Discrepancy(row.row_id, "fast_mode", "no replace_base modifier",
                               "fast mode listed", source, True))
        return
    fast_in = dict(mod.base_usd_per_mtok)["input"]
    ours = {"input": fast_in, "output": dict(mod.base_usd_per_mtok)["output"],
            "cache_read": _rate_of(fast_in, row.cache_read_mult, row.row_id),
            "cache_write_5m": _rate_of(fast_in, row.cache_write_5m_mult, row.row_id),
            "cache_write_1h": _rate_of(fast_in, row.cache_write_1h_mult, row.row_id)}
    _compare_prices(out, mod.modifier_id, "", ours, quote.default,
                    claude=row.model.startswith(CLAUDE_PREFIX), source=source)
    if "fast_mode" not in row.supports:
        out.append(Discrepancy(row.row_id, "supports", ", ".join(row.supports) or "none",
                               "fast_mode", source, False))


def compare(layer: RateLayer, quotes: Mapping[tuple[str, str], Quote], *, as_of: str,
            source: str) -> list[Discrepancy]:
    """Discrepancies between the ``github_copilot`` rows of *layer* in force on *as_of* and the
    table *quotes*, sorted by ``(row_id, field)`` (policy in the module docstring)."""
    rows = [r for r in layer.rows if r.channel == CHANNEL and _in_force(r, as_of)]
    by_model: dict[str, RateRow] = {}
    for row in sorted(rows, key=lambda r: r.row_id):
        by_model.setdefault(row.model, row)
    out: list[Discrepancy] = []
    listed: set[str] = set()
    fast_listed: set[str] = set()
    for (model, speed), quote in quotes.items():
        row = by_model.get(model) or next(
            (r for r in by_model.values() if quote.display in r.aliases), None)
        target = row.model if row is not None else model
        (fast_listed if speed == "fast" else listed).add(target)
        if row is None:
            out.append(Discrepancy(f"{PROVIDER}/{CHANNEL}/{model}", "row" if speed == "standard"
                                   else "fast_mode", "missing", "listed", source, True))
        elif speed == "standard":
            _compare_row(out, row, quote, source)
        else:
            _compare_fast(out, row, quote, _fast_modifier(layer.modifiers, row.model), source)
    for model, row in by_model.items():
        if model not in listed:
            out.append(Discrepancy(row.row_id, "row", "in force", "not listed", source, True))
        mod = _fast_modifier(layer.modifiers, model)
        if mod is not None and model not in fast_listed:
            out.append(Discrepancy(mod.modifier_id, "fast_mode", "priced", "not listed", source,
                                   True))
    return sorted(out, key=lambda d: (d.row_id, d.field))


def verify(layer: RateLayer, *, snapshot: Path | None = None, live: bool = False,
           opener: Any = None) -> list[Discrepancy]:
    """Verify the Copilot rows of *layer* against GitHub's pricing table (addendum §6.2 #6).

    Offline (default) the table is the packaged snapshot or *snapshot* (a snapshot JSON or a dated
    ``<YYYY-MM-DD>_<sha>.yml`` revision) and rows are taken in force on its date; with
    ``live=True`` the raw YAML is fetched through *opener* (see :func:`fetch_live`) and rows are
    taken in force on the response date. Returns every discrepancy (authoritative ones fail
    ``pricing verify``), sorted by ``(row_id, field)``."""
    if live:
        if snapshot is not None:
            raise UsageError("pricing verify: --live and a snapshot are exclusive")
        text, date = fetch_live(opener=opener)
        return compare(layer, quotes_from_maps(parse_yaml_list(text)), as_of=date,
                       source=f"live {YAML_URL} ({date})")
    snap = read_snapshot(snapshot)
    return compare(layer, quotes_from_maps(snap.entries), as_of=snap.date, source=snap.label)
