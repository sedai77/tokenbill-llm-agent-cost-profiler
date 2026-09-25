"""Rate verification: offline snapshot, opt-in live check, feed cross-check (SPEC §6.7).

* :func:`verify_snapshot` compares a layer with a recorded parse of the Claude pricing page
  (default: the packaged ``rates/data/snapshots/pricing-2026-09-23.json``): every listed model needs
  an enabled ``anthropic_api`` row effective on the snapshot date with the listed input, output and
  cache prices; a registry row the page does not list, a batch or fast-mode price the modifiers do
  not reproduce, and a different US-geo multiplier or web-search price are discrepancies too. All
  are authoritative (``pricing verify`` exits 3 on them).
* :func:`verify_live` (``--live`` only) fetches the ``.md`` pricing page over HTTPS with the stdlib
  (TLS verified, 30 s timeout; the opener is injectable for tests), parses its tables with
  :func:`parse_pricing_page` and compares like the snapshot.
* :func:`crosscheck_feed` compares with a LiteLLM model map or an OpenRouter models list supplied as
  a file: **non-authoritative** — every difference is a warning, and it never raises.
* :func:`verify_extensions` runs the channel extensions' rate verifiers
  (``core.extensions.rate_verifiers()``, e.g. the Copilot YAML check); :func:`verify_report`
  combines everything with the stale rows into a ``PricingReport``.
"""

from __future__ import annotations

import datetime as _dt
import html
import importlib.resources
import json
import re
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tokenbill.core import catalog, registry
from tokenbill.core import extensions as _extensions
from tokenbill.core.errors import ContractViolation, PricingError, SourceError, UsageError
from tokenbill.core.models import normalize_model
from tokenbill.core.money import EXACT_CTX
from tokenbill.core.records import PricingContext
from tokenbill.core.types import DataQualityNote, Discrepancy, PricingReport, RateLayer, RateRow
from tokenbill.rates.engine import STALE_AFTER_DAYS, RateCard
from tokenbill.rates.schema import layer_kind

__all__ = [
    "DEFAULT_SNAPSHOT",
    "PRICING_MD_URL",
    "SNAPSHOT_SCHEMA",
    "compare_snapshot",
    "crosscheck_feed",
    "diff_layers",
    "load_snapshot",
    "model_id_of",
    "parse_pricing_page",
    "promotions_ending",
    "show_report",
    "stale_rows",
    "verify_extensions",
    "verify_live",
    "verify_report",
    "verify_snapshot",
]

PRICING_MD_URL = "https://platform.claude.com/docs/en/about-claude/pricing.md"
SNAPSHOT_SCHEMA = "tokenbill/pricing-snapshot@1"
DEFAULT_SNAPSHOT = "pricing-2026-09-23.json"
Opener = Callable[[str, int], bytes]
_TIMEOUT_S = 30
_MAX_PAGE_BYTES = 8 * 2**20
_DAY_MS = 86_400_000
_PRICE_RE = re.compile(r"\$\s*([0-9]{1,9}(?:\.[0-9]{1,9})?)\s*/\s*MTok")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_TAG_RE = re.compile(r"<[^>]*>")
_SUP_RE = re.compile(r"<sup>.*?</sup>", re.S)
_PAREN_RE = re.compile(r"\([^)]*\)")
_CLAUDE_RE = re.compile(r"claude(?:\s+[a-z]+)+\s+[0-9]+(?:\.[0-9]+)?\Z")
_GEO_RE = re.compile(r"inference_geo[^\n]{0,80}?([0-9]+(?:\.[0-9]+)?)x (?:pricing )?multiplier")
_WEB_RE = re.compile(r"\$([0-9]+(?:\.[0-9]+)?) per 1,000 searches")
_FIELDS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")


def _dec_str(d: Decimal) -> str:
    text = format(d, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _noon_ms(date: str) -> int:
    return (_dt.date.fromisoformat(date) - _dt.date(1970, 1, 1)).days * _DAY_MS + 43_200_000


def _today() -> str:
    return _dt.datetime.now(_dt.timezone.utc).date().isoformat()


# ---------------------------------------------------------------------------------------------
# the pricing page
# ---------------------------------------------------------------------------------------------


def model_id_of(display: str) -> list[str]:
    """Model ids named by a pricing-table cell: ``"Claude Opus 5.5 ([limited …](…))"`` →
    ``["claude-opus-5-5"]``; ``"Claude Opus 5 / Claude Opus 4.8"`` → two ids; non-Claude → []."""
    text = _LINK_RE.sub(r"\1", _SUP_RE.sub("", display))
    text = _PAREN_RE.sub("", html.unescape(_TAG_RE.sub("", text))).replace("*", "")
    out = []
    for part in text.split("/"):
        name = " ".join(part.lower().split())
        if _CLAUDE_RE.match(name):
            out.append(name.replace(" ", "-").replace(".", "-"))
    return out


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _price(cell: str) -> str | None:
    m = _PRICE_RE.search(_SUP_RE.sub("", cell))
    return _dec_str(Decimal(m.group(1))) if m else None


def parse_pricing_page(text: str) -> dict[str, Any]:
    """Parse the model, batch and fast-mode tables (and the US-geo multiplier and web-search
    price) of the Claude pricing page (Markdown) into a snapshot-shaped dict; ``PricingError`` when
    the page has no model table."""
    if not isinstance(text, str):
        raise PricingError("pricing page: expected text")
    models: list[dict[str, str]] = []
    batch: list[dict[str, str]] = []
    fast: list[dict[str, str]] = []
    heading = ""
    header: list[str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            heading, header = line.lower(), None
            continue
        if not line.startswith("|"):
            header = None
            continue
        cells = _cells(line)
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if all(set(c) <= set(":- ") for c in cells):
            continue
        names = model_id_of(cells[0]) if cells else []
        prices = [_price(c) for c in cells[1:]]
        if not names or any(p is None for p in prices):
            continue
        if "base input tokens" in header and len(prices) == 5:
            for name in names:
                models.append({"model": name, **dict(zip(_FIELDS, prices, strict=True))})  # type: ignore[arg-type]
        elif "batch input" in header and len(prices) == 2:
            for name in names:
                batch.append({"model": name, "input": prices[0], "output": prices[1]})  # type: ignore[dict-item]
        elif "fast" in heading and header[1:] == ["input", "output"] and len(prices) == 2:
            for name in names:
                fast.append({"model": name, "input": prices[0], "output": prices[1]})  # type: ignore[dict-item]
    if not models:
        raise PricingError("pricing page: no model price table found")
    modifiers: dict[str, str] = {}
    geo = _GEO_RE.search(text)
    if geo:
        modifiers["inference_geo_us"] = _dec_str(Decimal(geo.group(1)))
    web = _WEB_RE.search(text)
    if web:
        modifiers["web_search_per_request"] = _dec_str(
            EXACT_CTX.divide(Decimal(web.group(1)), Decimal(1000)))
    return {"schema": SNAPSHOT_SCHEMA, "channel": "anthropic_api", "models": models,
            "batch": batch, "fast": fast, "modifiers": modifiers}


def load_snapshot(path: Path | None = None) -> dict[str, Any]:
    """A pricing snapshot (default: the packaged ``snapshots/pricing-2026-09-23.json``)."""
    try:
        if path is None:
            data = importlib.resources.files("tokenbill.rates").joinpath("data").joinpath(
                "snapshots").joinpath(DEFAULT_SNAPSHOT).read_bytes()
        else:
            data = Path(path).read_bytes()
    except OSError:
        raise SourceError("cannot read the pricing snapshot") from None
    try:
        snap = json.loads(data)
    except (ValueError, RecursionError):
        raise PricingError("pricing snapshot: not valid JSON") from None
    if not isinstance(snap, dict) or snap.get("schema") != SNAPSHOT_SCHEMA \
            or not isinstance(snap.get("models"), list) or not isinstance(snap.get("as_of"), str):
        raise PricingError(f"pricing snapshot: schema must be {SNAPSHOT_SCHEMA}")
    return snap


# ---------------------------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------------------------


def _row_on(layer: RateLayer, channel: str, model: str, date: str) -> RateRow | None:
    for row in layer.rows:
        if row.channel == channel and row.enabled and (row.model == model or model in row.aliases) \
                and row.effective_from <= date and (row.effective_to is None
                                                     or date < row.effective_to):
            return row
    return None


def _num(value: object) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        d = Decimal(value)
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _row_prices(row: RateRow) -> dict[str, Decimal | None]:
    def times(mult: Decimal | None) -> Decimal | None:
        return None if mult is None else EXACT_CTX.multiply(row.input_usd_per_mtok, mult)
    return {"input": row.input_usd_per_mtok, "output": row.output_usd_per_mtok,
            "cache_read": times(row.cache_read_mult),
            "cache_write_5m": times(row.cache_write_5m_mult),
            "cache_write_1h": times(row.cache_write_1h_mult)}


def compare_snapshot(layer: RateLayer, snap: Mapping[str, Any], *,
                     source: str) -> list[Discrepancy]:
    """Discrepancies (authoritative) between *layer* and a parsed pricing snapshot."""
    date = snap["as_of"]
    channel = snap.get("channel", "anthropic_api")
    out: list[Discrepancy] = []

    def add(row_id: str, field: str, ours: object, theirs: object) -> None:
        out.append(Discrepancy(row_id=row_id, field=field,
                               ours=_dec_str(ours) if isinstance(ours, Decimal) else str(ours),
                               theirs=theirs if isinstance(theirs, str) else str(theirs),
                               source=source, authoritative=True))

    listed: set[str] = set()
    web_listed = _num(snap.get("modifiers", {}).get("web_search_per_request"))
    for entry in snap["models"]:
        model = entry.get("model")
        if not isinstance(model, str):
            continue
        listed.add(model)
        row = _row_on(layer, channel, model, date)
        if row is None:
            add(f"(missing) {channel}/{model}", "row", "absent", "listed")
            continue
        ours = _row_prices(row)
        for field in _FIELDS:
            theirs = _num(entry.get(field))
            if theirs is not None and ours[field] != theirs:
                add(row.row_id, field, ours[field] if ours[field] is not None else "absent",
                    _dec_str(theirs))
        web = dict(row.per_request_usd).get("web_search")
        if web_listed is not None and web != web_listed:
            add(row.row_id, "per_request.web_search", web if web is not None else "absent",
                _dec_str(web_listed))
    for row in layer.rows:
        if row.channel == channel and row.enabled and row.effective_from <= date and (
                row.effective_to is None or date < row.effective_to) and not (
                {row.model, *row.aliases} & listed):
            add(row.row_id, "row", "listed", "absent")
    card = RateCard([layer])
    ts = _noon_ms(date)
    for table, extra in (("batch", {"service_tier": "batch"}), ("fast", {"speed": "fast"})):
        for entry in snap.get(table, []):
            model = entry.get("model")
            if not isinstance(model, str):
                continue
            ctx = PricingContext(provider="anthropic", channel=channel, model=model,
                                 model_raw=model, **extra)
            rates = card.resolve(ctx, ts_ms=ts)
            for field in ("input", "output"):
                theirs = _num(entry.get(field))
                if theirs is None:
                    continue
                ours = getattr(rates, field) if rates is not None else None
                if ours != theirs:
                    add(rates.row_id if rates is not None else f"(missing) {channel}/{model}",
                        f"{table}.{field}", ours if ours is not None else "absent",
                        _dec_str(theirs))
    geo = _num(snap.get("modifiers", {}).get("inference_geo_us"))
    if geo is not None:
        factors = [m.factor for m in layer.modifiers if ("inference_geo", "us") in m.when
                   and ("channel_in" not in dict(m.when)
                        or channel in dict(m.when)["channel_in"].split(","))]
        if factors != [geo]:
            add("modifier:inference_geo=us", "factor",
                ",".join(_dec_str(f) for f in factors if f is not None) or "absent",
                _dec_str(geo))
    return sorted(out, key=lambda d: (d.row_id, d.field))


def verify_snapshot(layer: RateLayer, snapshot_path: Path | None = None) -> list[Discrepancy]:
    """Offline verification against the packaged (or given) pricing snapshot."""
    if not isinstance(layer, RateLayer):
        raise UsageError("verify_snapshot expects a RateLayer")
    snap = load_snapshot(snapshot_path)
    return compare_snapshot(layer, snap, source=str(snap.get("source", "snapshot")))


def _https_get(url: str, timeout: int) -> bytes:  # pragma: no cover - network (tests inject)
    request = urllib.request.Request(url, headers={"User-Agent": "tokenbill-pricing-verify"})
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            return response.read(_MAX_PAGE_BYTES + 1)
    except (urllib.error.URLError, OSError, ValueError):
        raise SourceError("cannot fetch the pricing page") from None


def verify_live(layer: RateLayer, url: str = PRICING_MD_URL, *, opener: Opener | None = None,
                today: str | None = None) -> list[Discrepancy]:
    """Fetch the ``.md`` pricing page (HTTPS only, TLS verified, 30 s timeout) and compare its
    tables with the rows effective *today* (default: the current UTC date)."""
    if not isinstance(url, str) or not url.startswith("https://"):
        raise UsageError("verify_live needs an https:// URL")
    data = (opener or _https_get)(url, _TIMEOUT_S)
    if not isinstance(data, bytes) or len(data) > _MAX_PAGE_BYTES:
        raise SourceError("the pricing page is missing or too large")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise SourceError("the pricing page is not UTF-8") from None
    snap = parse_pricing_page(text)
    snap["as_of"] = today or _today()
    return compare_snapshot(layer, snap, source=url)


# ---------------------------------------------------------------------------------------------
# non-authoritative feeds
# ---------------------------------------------------------------------------------------------

_LITELLM_CHANNELS = {"anthropic": "anthropic_api", "openai": "openai_api", "bedrock": "bedrock",
                     "bedrock_converse": "bedrock", "vertex_ai-anthropic_models": "vertex"}
_LITELLM_FIELDS = {"input_cost_per_token": "input", "output_cost_per_token": "output",
                   "cache_read_input_token_cost": "cache_read",
                   "cache_creation_input_token_cost": "cache_write_5m"}
_OPENROUTER_FIELDS = {"prompt": "input", "completion": "output", "input_cache_read": "cache_read",
                      "input_cache_write": "cache_write_5m"}
_OPENROUTER_CHANNELS = {"anthropic": "anthropic_api", "openai": "openai_api"}


def _feed_number(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, Decimal)):
        d = Decimal(value)
    elif isinstance(value, float):     # a feed parsed by json.loads: its shortest repr
        d = Decimal(repr(value))
    elif isinstance(value, str):
        try:
            d = Decimal(value.strip())
        except InvalidOperation:
            return None
    else:
        return None
    return d if d.is_finite() and d >= 0 else None


def _feed_entries(feed: object) -> Iterable[tuple[str, str, str, dict[str, Decimal]]]:
    """(source, channel, raw model, {field: USD per MTok}) of a LiteLLM map or OpenRouter list."""
    if isinstance(feed, Mapping) and isinstance(feed.get("data"), list):
        for item in feed["data"]:
            if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                continue
            vendor, _, name = item["id"].partition("/")
            pricing = item.get("pricing")
            if vendor not in _OPENROUTER_CHANNELS or not isinstance(pricing, Mapping):
                continue
            prices = {f: n.scaleb(6) for k, f in _OPENROUTER_FIELDS.items()
                      if (n := _feed_number(pricing.get(k))) is not None}
            yield "openrouter", _OPENROUTER_CHANNELS[vendor], name, prices
    elif isinstance(feed, Mapping):
        for key, item in feed.items():
            if not isinstance(key, str) or not isinstance(item, Mapping):
                continue
            channel = _LITELLM_CHANNELS.get(str(item.get("litellm_provider")))
            if channel is None:
                continue
            prices = {f: n.scaleb(6) for k, f in _LITELLM_FIELDS.items()
                      if (n := _feed_number(item.get(k))) is not None}
            yield "litellm", channel, key.rpartition("/")[2], prices


def crosscheck_feed(layer: RateLayer, feed_json: object, *,
                    today: str | None = None) -> list[Discrepancy]:
    """Warnings (``authoritative=False``) where a LiteLLM model map or an OpenRouter models list
    (parsed JSON, or its text) disagrees with the rows effective *today*. Never raises: an
    unreadable feed yields one ``format`` warning."""
    try:
        feed = json.loads(feed_json, parse_float=Decimal) if isinstance(feed_json, (str, bytes)) \
            else feed_json
        date = today or _today()
        ts = _noon_ms(date)
        card = RateCard([layer])
        out: list[Discrepancy] = []
        for source, channel, raw, prices in _feed_entries(feed):
            name = raw.replace(".", "-") if raw.startswith("claude") else raw
            model = normalize_model(name).model
            if not model:
                continue
            ctx = PricingContext(provider="openai" if channel == "openai_api" else "anthropic",
                                 channel=channel, model=model, model_raw=raw,
                                 endpoint_scope="global")
            rates = card.resolve(ctx, ts_ms=ts)
            if rates is None:
                continue
            for field, theirs in sorted(prices.items()):
                ours = getattr(rates, field)
                if ours is not None and ours != theirs:
                    out.append(Discrepancy(row_id=rates.row_id, field=field, ours=_dec_str(ours),
                                           theirs=_dec_str(theirs), source=f"{source}:{raw}",
                                           authoritative=False))
        return sorted(out, key=lambda d: (d.row_id, d.field, d.source))
    except Exception:  # noqa: BLE001 - a non-authoritative feed never fails a run
        return [Discrepancy(row_id="(feed)", field="format", ours="", theirs="unreadable",
                            source="feed", authoritative=False)]


# ---------------------------------------------------------------------------------------------
# extensions, staleness, reports
# ---------------------------------------------------------------------------------------------


def verify_extensions(layer: RateLayer, *, snapshot: Path | None = None, live: bool = False,
                      opener: Opener | None = None,
                      notes: list[DataQualityNote] | None = None) -> list[Discrepancy]:
    """Run every available extension rate verifier (``core.extensions.rate_verifiers()``), each
    called as ``verify(layer, snapshot=…, live=…, opener=…)``; an unavailable verifier is skipped
    with a ``dq.extension_unavailable`` note."""
    out: list[Discrepancy] = []
    for dotted in _extensions.rate_verifiers(notes=notes):
        result = registry.load(dotted)(layer, snapshot=snapshot, live=live, opener=opener)
        if not isinstance(result, (list, tuple)) or not all(isinstance(d, Discrepancy)
                                                            for d in result):
            raise ContractViolation("a rate verifier must return a list of Discrepancy")
        out.extend(result)
    return out


def stale_rows(layer: RateLayer, *, today: str,
               max_age_days: int = STALE_AFTER_DAYS) -> tuple[str, ...]:
    """Ids of the enabled rows of *layer* verified more than *max_age_days* before *today*
    (a ``--model-price`` layer has none)."""
    if layer_kind(layer) == "model-price":
        return ()
    cutoff = (_dt.date.fromisoformat(today) - _dt.timedelta(days=max_age_days)).isoformat()
    return tuple(sorted(r.row_id for r in layer.rows if r.enabled and r.verified_on < cutoff))


def verify_report(layer: RateLayer, *, today: str, snapshot_path: Path | None = None,
                  live: bool = False, url: str = PRICING_MD_URL, opener: Opener | None = None,
                  notes: list[DataQualityNote] | None = None) -> PricingReport:
    """``pricing verify``: the snapshot (or, with *live*, the live page) plus the extension
    verifiers; ``ok`` is False when any authoritative discrepancy exists."""
    found = verify_live(layer, url, opener=opener, today=today) if live else \
        verify_snapshot(layer, snapshot_path)
    found = [*found, *verify_extensions(layer, live=live, opener=opener, notes=notes)]
    return PricingReport(kind="verify", rows=layer.rows, modifiers=layer.modifiers,
                         discrepancies=tuple(found), stale_rows=stale_rows(layer, today=today),
                         ok=not any(d.authoritative for d in found))


def show_report(layer: RateLayer, *, today: str) -> PricingReport:
    """``pricing show``: the layer's rows, modifiers and stale rows."""
    return PricingReport(kind="show", rows=layer.rows, modifiers=layer.modifiers,
                         discrepancies=(), stale_rows=stale_rows(layer, today=today), ok=True)


def diff_layers(old: RateLayer, new: RateLayer) -> PricingReport:
    """``pricing diff``: rows added, removed or with different prices (by row id); informational
    (``authoritative=False``); ``ok`` when there is no difference."""
    before = {r.row_id: r for r in old.rows}
    after = {r.row_id: r for r in new.rows}
    out: list[Discrepancy] = []
    for row_id in sorted(before.keys() | after.keys()):
        a, b = before.get(row_id), after.get(row_id)
        if a is None or b is None:
            out.append(Discrepancy(row_id=row_id, field="row", ours="present" if a else "absent",
                                   theirs="present" if b else "absent", source=new.name,
                                   authoritative=False))
            continue
        pa, pb = _row_prices(a), _row_prices(b)
        extra = (("enabled", a.enabled, b.enabled), ("effective_to", a.effective_to,
                                                      b.effective_to))
        for field in _FIELDS:
            if pa[field] != pb[field]:
                out.append(Discrepancy(row_id=row_id, field=field, ours=str(pa[field]),
                                       theirs=str(pb[field]), source=new.name,
                                       authoritative=False))
        for field, x, y in extra:
            if x != y:
                out.append(Discrepancy(row_id=row_id, field=field, ours=str(x), theirs=str(y),
                                       source=new.name, authoritative=False))
    return PricingReport(kind="diff", rows=new.rows, modifiers=new.modifiers,
                         discrepancies=tuple(out), stale_rows=(), ok=not out)


def promotions_ending(today: str, *, days: int = 14) -> list[catalog.Promotion]:
    """Promotions whose ``not_before_end`` falls within *days* days of *today* (the weekly live
    check opens an issue for each)."""
    start = _dt.date.fromisoformat(today)
    end = (start + _dt.timedelta(days=days)).isoformat()
    return sorted((p for p in (*catalog.PROMOTIONS, *catalog.COPILOT_PROMOTIONS)
                   if today <= p.not_before_end <= end), key=lambda p: p.promotion_id)
