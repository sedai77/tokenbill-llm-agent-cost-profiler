"""Rate registry files: load and validate ``tokenbill/rates@1`` layers (SPEC §6.1).

A layer is one :class:`~tokenbill.core.types.RateLayer`: the built-in registry
(``rates/data/*.json`` plus the rate files channel extensions ship, :func:`load_builtin`), a user
file (``--rates FILE``, :func:`load_file`) or the ``--model-price`` overrides
(:func:`model_price_layer`). Every number is a decimal string parsed exactly; a JSON float anywhere
is a load error.

Load validation (each failure raises :class:`~tokenbill.core.errors.PricingError` naming the file
and the row or modifier): every row has at least one source and a ``verified_on`` date; no two rows
naming the same model (or alias) on the same channel have overlapping ``[effective_from,
effective_to)`` intervals within a layer; every ``published_absolute`` cache price equals ``input ×
multiplier`` exactly; modifiers use only the predicate keys of :data:`PREDICATE_KEYS` and the bucket
names of :data:`APPLIES_TO`; a ``promotion`` id exists in ``core.catalog`` (``PROMOTIONS`` or
``COPILOT_PROMOTIONS``); a ``supports`` entry naming cache-rule behavior (such as the retired
``effort_keeps_cache``) is refused — cache rules live in ``core.cache_rules`` (D28).

Rows with ``enabled: false`` (the **VERIFY** rows of SPEC §19) load but never price. Unknown keys in
a row or modifier are ignored (row files carry documentation keys such as ``notes``), so rate files
built by other packages (``tokenbill/copilot/data/github_copilot.json``) load unchanged.

Channel ``"*"`` (used by :func:`model_price_layer`) matches every channel.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import importlib.resources
import json
import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from tokenbill.core import catalog
from tokenbill.core import extensions as _extensions
from tokenbill.core import facts as _facts
from tokenbill.core.errors import PricingError, SourceError, UsageError
from tokenbill.core.money import EXACT_CTX
from tokenbill.core.records import to_json
from tokenbill.core.types import DataQualityNote, Modifier, RateLayer, RateRow, SourceCitation

__all__ = [
    "APPLIES_TO",
    "BUILTIN_FILES",
    "MULTIPLIER_BUCKETS",
    "PER_REQUEST_KEYS",
    "PREDICATE_KEYS",
    "SCHEMA",
    "WILDCARD_CHANNEL",
    "layer_kind",
    "load_builtin",
    "load_file",
    "make_layer",
    "model_price_layer",
    "parse_layer",
    "parse_model_price",
]

logger = logging.getLogger(__name__)

SCHEMA = "tokenbill/rates@1"
#: The built-in registry files (package data of ``tokenbill.rates``, directory ``data/``).
BUILTIN_FILES = ("anthropic.json", "bedrock.json", "vertex.json", "openai.json")
#: SPEC §6.1 predicate keys plus the Copilot ``routing`` / ``compliance_in`` (addendum §6.2 #1).
PREDICATE_KEYS = frozenset({"service_tier", "speed", "inference_geo", "endpoint_scope",
                            "channel_in", "model_in", "generation_gte", "routing",
                            "compliance_in"})
#: Cache buckets priced as ``input × multiplier``.
MULTIPLIER_BUCKETS = ("cache_read", "cache_write_5m", "cache_write_1h", "cache_write_other")
#: Buckets a long-context band may price.
BAND_BUCKETS = frozenset({"input", "output", *MULTIPLIER_BUCKETS})
#: Bucket names a ``multiply`` modifier may target (``*`` = every token bucket).
APPLIES_TO = frozenset({"*", "input", "uncached_input", *BAND_BUCKETS})
#: Per-request (server tool) prices a row may carry.
PER_REQUEST_KEYS = frozenset({"web_search"})
WILDCARD_CHANNEL = "*"
MODEL_PRICE_LAYER = "model-price"
_MAX_FILE_BYTES = 16 * 2**20
_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,18})?\Z")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_GENERATION_RE = re.compile(r"[0-9]{1,4}(?:\.[0-9]{1,4}){0,3}\Z")
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")
_ID_RE = re.compile(r"[^\x00-\x1f\x7f]{1,200}\Z")
_CACHE_RULE_WORDS = ("cache", "keeps", "invalidat", "break")
_MULTI_VALUE_KEYS = frozenset({"channel_in", "model_in", "compliance_in"})
_MODEL_PRICE_RE = re.compile(r"\A\s*(?P<model>[^=\s][^=]*?)\s*=\s*(?P<inp>[^,\s]+)\s*,\s*"
                             r"(?P<out>[^,\s]+)\s*\Z")


def _fail(origin: str, where: str, why: str) -> PricingError:
    return PricingError(f"{origin}: {where}: {why}")


def _reject_float(token: str) -> Any:
    raise PricingError("JSON floats and non-finite numbers are not allowed (use decimal strings)")


def _date(value: object, origin: str, where: str, field: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise _fail(origin, where, f"{field} must be a YYYY-MM-DD date")
    try:
        _dt.date.fromisoformat(value)
    except ValueError:
        raise _fail(origin, where, f"{field} is not a calendar date") from None
    return value


def _decimal(value: object, origin: str, where: str, field: str) -> Decimal:
    if isinstance(value, Decimal):
        if not value.is_finite() or value < 0:
            raise _fail(origin, where, f"{field} must be a finite, non-negative decimal")
        return value
    if not isinstance(value, str) or not _DECIMAL_RE.match(value):
        raise _fail(origin, where, f"{field} must be a non-negative decimal string")
    return Decimal(value)


def _str(value: object, origin: str, where: str, field: str, *,
         pattern: re.Pattern[str] = _ID_RE) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise _fail(origin, where, f"{field} must be a non-empty string")
    return value


def _int(value: object, origin: str, where: str, field: str, *, positive: bool) -> int:
    if type(value) is not int or value < (1 if positive else 0) or value > 2**53:
        raise _fail(origin, where, f"{field} must be a {'positive' if positive else 'non-negative'}"
                                   " integer")
    return value


def _mapping(value: object, origin: str, where: str, field: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _fail(origin, where, f"{field} must be an object")
    return value


def _list(value: object, origin: str, where: str, field: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _fail(origin, where, f"{field} must be a list")
    return value


def _pairs(obj: Mapping[str, Any], allowed: Iterable[str], origin: str, where: str,
           field: str) -> tuple[tuple[str, Decimal], ...]:
    allowed = frozenset(allowed)
    out = []
    for key, value in obj.items():
        if key not in allowed:
            raise _fail(origin, where, f"{field}: unknown bucket {key!r}")
        out.append((key, _decimal(value, origin, where, f"{field}.{key}")))
    return tuple(sorted(out))


def _citations(items: object, origin: str, where: str) -> tuple[SourceCitation, ...]:
    out = []
    for item in _list(items, origin, where, "sources"):
        s = _mapping(item, origin, where, "sources[]")
        finding = s.get("finding")
        if finding is not None and not isinstance(finding, str):
            raise _fail(origin, where, "sources[].finding must be a string or null")
        out.append(SourceCitation(url=_str(s.get("url"), origin, where, "sources[].url"),
                                  retrieved=_date(s.get("retrieved"), origin, where,
                                                  "sources[].retrieved"),
                                  finding=finding))
    return tuple(out)


# ---------------------------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------------------------


def _row(r: object, provider: str, origin: str, index: int) -> RateRow:
    if not isinstance(r, Mapping):
        raise _fail(origin, f"rows[{index}]", "a row must be an object")
    row_id = r.get("row_id")
    where = f"row {row_id}" if isinstance(row_id, str) and _ID_RE.match(row_id) else \
        f"rows[{index}]"
    row_id = _str(row_id, origin, where, "row_id")
    prices = _mapping(r.get("usd_per_mtok"), origin, where, "usd_per_mtok")
    mult = _mapping(r.get("multipliers"), origin, where, "multipliers")
    for key in mult:
        if key not in MULTIPLIER_BUCKETS:
            raise _fail(origin, where, f"multipliers: unknown bucket {key!r}")

    def multiplier(bucket: str) -> Decimal | None:
        value = mult.get(bucket)
        return None if value is None else _decimal(value, origin, where, f"multipliers.{bucket}")

    lc = r.get("long_context")
    threshold: int | None = None
    band: tuple[tuple[str, Decimal], ...] = ()
    if lc is not None:
        lc = _mapping(lc, origin, where, "long_context")
        threshold = _int(lc.get("threshold"), origin, where, "long_context.threshold",
                         positive=True)
        band = _pairs(_mapping(lc.get("usd_per_mtok"), origin, where, "long_context.usd_per_mtok"),
                      BAND_BUCKETS, origin, where, "long_context.usd_per_mtok")
    ttl = r.get("cache_write_other_ttl_s")
    if ttl is not None:
        ttl = _int(ttl, origin, where, "cache_write_other_ttl_s", positive=True)
    min_cache = r.get("min_cacheable_tokens")
    if min_cache is not None:
        min_cache = _int(min_cache, origin, where, "min_cacheable_tokens", positive=False)
    enabled = r.get("enabled")
    if type(enabled) is not bool:
        raise _fail(origin, where, "enabled must be true or false")
    effective_to = r.get("effective_to")
    notes = r.get("notes", "")
    if not isinstance(notes, str):
        raise _fail(origin, where, "notes must be a string")
    promotion = r.get("promotion")
    if promotion is not None:
        promotion = _str(promotion, origin, where, "promotion")
    return RateRow(
        row_id=row_id,
        provider=_str(r.get("provider", provider), origin, where, "provider"),
        channel=_str(r.get("channel"), origin, where, "channel"),
        model=_str(r.get("model"), origin, where, "model"),
        aliases=tuple(_str(a, origin, where, "aliases[]")
                      for a in _list(r.get("aliases"), origin, where, "aliases")),
        generation=_str(r.get("generation"), origin, where, "generation"),
        effective_from=_date(r.get("effective_from"), origin, where, "effective_from"),
        effective_to=None if effective_to is None else _date(effective_to, origin, where,
                                                             "effective_to"),
        input_usd_per_mtok=_decimal(prices.get("input"), origin, where, "usd_per_mtok.input"),
        output_usd_per_mtok=_decimal(prices.get("output"), origin, where, "usd_per_mtok.output"),
        cache_read_mult=multiplier("cache_read"),
        cache_write_5m_mult=multiplier("cache_write_5m"),
        cache_write_1h_mult=multiplier("cache_write_1h"),
        cache_write_other_mult=multiplier("cache_write_other"),
        cache_write_other_ttl_s=ttl,
        published_absolute=_pairs(_mapping(r.get("published_absolute"), origin, where,
                                           "published_absolute"),
                                  MULTIPLIER_BUCKETS, origin, where, "published_absolute"),
        min_cacheable_tokens=min_cache,
        tokenizer_family=_str(r.get("tokenizer_family"), origin, where, "tokenizer_family"),
        per_request_usd=_pairs(_mapping(r.get("per_request_usd"), origin, where,
                                        "per_request_usd"),
                               PER_REQUEST_KEYS, origin, where, "per_request_usd"),
        long_context_threshold=threshold,
        long_context_usd_per_mtok=band,
        supports=tuple(_str(s, origin, where, "supports[]", pattern=_TOKEN_RE)
                       for s in _list(r.get("supports"), origin, where, "supports")),
        enabled=enabled,
        verified_on=_date(r.get("verified_on"), origin, where, "verified_on"),
        sources=_citations(r.get("sources"), origin, where),
        promotion=promotion,
        notes=notes,
    )


def _modifier(m: object, origin: str, index: int) -> Modifier:
    if not isinstance(m, Mapping):
        raise _fail(origin, f"modifiers[{index}]", "a modifier must be an object")
    mid = m.get("modifier_id")
    where = f"modifier {mid}" if isinstance(mid, str) and _ID_RE.match(mid) else \
        f"modifiers[{index}]"
    mid = _str(mid, origin, where, "modifier_id")
    kind = m.get("kind")
    factor = m.get("factor")
    when = _mapping(m.get("when"), origin, where, "when")
    for key, value in when.items():
        if not isinstance(value, str):
            raise _fail(origin, where, f"when.{key} must be a string")
    applies = m.get("applies_to", ["*"])
    return Modifier(
        modifier_id=mid,
        kind=_str(kind, origin, where, "kind"),
        factor=None if factor is None else _decimal(factor, origin, where, "factor"),
        base_usd_per_mtok=_pairs(_mapping(m.get("base_usd_per_mtok"), origin, where,
                                          "base_usd_per_mtok"),
                                 ("input", "output"), origin, where, "base_usd_per_mtok"),
        applies_to=tuple(_str(a, origin, where, "applies_to[]", pattern=re.compile(r"\S+\Z"))
                         for a in _list(applies, origin, where, "applies_to")),
        when=tuple(sorted((str(k), v) for k, v in when.items())),
        stacking=_str(m.get("stacking"), origin, where, "stacking"),
        sources=_citations(m.get("sources"), origin, where),
    )


# ---------------------------------------------------------------------------------------------
# validation (on parsed rows and modifiers, so facts-provided rows are checked the same way)
# ---------------------------------------------------------------------------------------------

_PROMOTION_IDS: frozenset[str] | None = None


def _promotion_ids() -> frozenset[str]:
    global _PROMOTION_IDS
    if _PROMOTION_IDS is None:
        _PROMOTION_IDS = frozenset(p.promotion_id for p in (*catalog.PROMOTIONS,
                                                            *catalog.COPILOT_PROMOTIONS))
    return _PROMOTION_IDS


def _check_row(row: RateRow, origin: str) -> None:
    where = f"row {row.row_id}"
    if not row.sources:
        raise _fail(origin, where, "a row needs at least one source")
    _date(row.verified_on, origin, where, "verified_on")
    _date(row.effective_from, origin, where, "effective_from")
    if row.effective_to is not None:
        _date(row.effective_to, origin, where, "effective_to")
        if row.effective_to <= row.effective_from:
            raise _fail(origin, where, "effective_to must be after effective_from")
    if not _GENERATION_RE.match(row.generation):
        raise _fail(origin, where, "generation must be numeric (e.g. '5.5')")
    mults = {"cache_read": row.cache_read_mult, "cache_write_5m": row.cache_write_5m_mult,
             "cache_write_1h": row.cache_write_1h_mult,
             "cache_write_other": row.cache_write_other_mult}
    for bucket, published in row.published_absolute:
        mult = mults.get(bucket)
        if mult is None:
            raise _fail(origin, where, f"published_absolute.{bucket} has no multiplier")
        if EXACT_CTX.multiply(row.input_usd_per_mtok, mult) != published:
            raise _fail(origin, where, f"published_absolute.{bucket} != input x multiplier")
    if row.cache_write_other_mult is not None and row.cache_write_other_ttl_s is None:
        raise _fail(origin, where, "cache_write_other needs cache_write_other_ttl_s")
    for feature in row.supports:
        if any(word in feature for word in _CACHE_RULE_WORDS):
            raise _fail(origin, where, f"supports entry {feature!r} names cache-rule behavior "
                                       "(it belongs to core.cache_rules, D28)")
    if row.promotion is not None and row.promotion not in _promotion_ids():
        raise _fail(origin, where, f"unknown promotion id {row.promotion!r}")
    if (row.long_context_threshold is None) != (not row.long_context_usd_per_mtok):
        raise _fail(origin, where, "long_context needs a threshold and band rates")


def _check_modifier(m: Modifier, origin: str) -> None:
    where = f"modifier {m.modifier_id}"
    if m.kind == "multiply":
        if m.factor is None or m.base_usd_per_mtok:
            raise _fail(origin, where, "a multiply modifier has a factor and no base rates")
    elif m.kind == "replace_base":
        if m.factor is not None or not m.base_usd_per_mtok:
            raise _fail(origin, where, "a replace_base modifier has base rates and no factor")
    else:
        raise _fail(origin, where, "kind must be multiply or replace_base")
    if not m.applies_to:
        raise _fail(origin, where, "applies_to must name at least one bucket")
    for bucket in m.applies_to:
        if bucket not in APPLIES_TO:
            raise _fail(origin, where, f"unknown bucket {bucket!r}")
    if m.stacking not in ("documented", "assumed"):
        raise _fail(origin, where, "stacking must be documented or assumed")
    for key, value in m.when:
        if key not in PREDICATE_KEYS:
            raise _fail(origin, where, f"unknown predicate key {key!r}")
        parts = value.split(",") if key in _MULTI_VALUE_KEYS else [value]
        if not value or any(not p for p in parts):
            raise _fail(origin, where, f"when.{key} must be a non-empty value")
        if key == "generation_gte" and not _GENERATION_RE.match(value):
            raise _fail(origin, where, "when.generation_gte must be numeric")


def _check_layer(rows: Sequence[RateRow], modifiers: Sequence[Modifier], origin: str) -> None:
    seen: set[str] = set()
    for row in rows:
        _check_row(row, origin)
        if row.row_id in seen:
            raise _fail(origin, f"row {row.row_id}", "duplicate row_id")
        seen.add(row.row_id)
    mids: set[str] = set()
    for m in modifiers:
        _check_modifier(m, origin)
        if m.modifier_id in mids:
            raise _fail(origin, f"modifier {m.modifier_id}", "duplicate modifier_id")
        mids.add(m.modifier_id)
    by_name: dict[tuple[str, str], list[RateRow]] = {}
    for row in rows:
        for name in dict.fromkeys((row.model, *row.aliases)):
            by_name.setdefault((row.channel, name), []).append(row)
    for (channel, name), group in sorted(by_name.items()):
        group.sort(key=lambda r: r.effective_from)
        for prev, nxt in zip(group, group[1:], strict=False):
            if prev.effective_to is None or prev.effective_to > nxt.effective_from:
                raise _fail(origin, f"row {nxt.row_id}",
                            f"overlaps row {prev.row_id} for {channel}/{name}")


def _canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def make_layer(name: str, rows: Sequence[RateRow], modifiers: Sequence[Modifier], *,
               as_of: str, origin: str | None = None) -> RateLayer:
    """A validated :class:`RateLayer` from parsed rows and modifiers, with its ``sha256`` (of the
    canonical JSON of name, schema, as_of, rows and modifiers)."""
    origin = origin or name
    rows, modifiers = tuple(rows), tuple(modifiers)
    _check_layer(rows, modifiers, origin)
    payload = {"name": name, "schema": SCHEMA, "as_of": as_of,
               "rows": [to_json(r) for r in rows], "modifiers": [to_json(m) for m in modifiers]}
    sha = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
    return RateLayer(name=name, schema=SCHEMA, as_of=as_of, rows=rows, modifiers=modifiers,
                     sha256=sha)


def _parse_doc(text: str | bytes, origin: str) -> tuple[str, str, list[RateRow], list[Modifier]]:
    try:
        doc = json.loads(text, parse_float=_reject_float, parse_constant=_reject_float)
    except PricingError as exc:
        raise _fail(origin, "document", str(exc)) from None
    except (ValueError, RecursionError):
        raise _fail(origin, "document", "not valid JSON") from None
    if not isinstance(doc, dict):
        raise _fail(origin, "document", "must be a JSON object")
    if doc.get("schema") != SCHEMA:
        raise _fail(origin, "document", f"schema must be {SCHEMA}")
    provider = _str(doc.get("provider"), origin, "document", "provider")
    as_of = _date(doc.get("as_of"), origin, "document", "as_of")
    rows = [_row(r, provider, origin, i)
            for i, r in enumerate(_list(doc.get("rows"), origin, "document", "rows"))]
    mods = [_modifier(m, origin, i)
            for i, m in enumerate(_list(doc.get("modifiers"), origin, "document", "modifiers"))]
    return provider, as_of, rows, mods


def parse_layer(text: str | bytes, name: str, *, origin: str | None = None) -> RateLayer:
    """Parse and validate one ``tokenbill/rates@1`` document as a layer called *name*."""
    origin = origin or name
    _, as_of, rows, mods = _parse_doc(text, origin)
    return make_layer(name, rows, mods, as_of=as_of, origin=origin)


def load_file(path: Path, name: str) -> RateLayer:
    """A user rate file (``--rates FILE``) as layer ``user:<name>`` (*name* is used as given when it
    already starts with ``user:``). Unreadable files raise ``SourceError``; invalid content
    ``PricingError``."""
    path = Path(path)
    try:
        with path.open("rb") as fh:
            data = fh.read(_MAX_FILE_BYTES + 1)
    except OSError:
        raise SourceError(f"cannot read rate file {path.name}") from None
    if len(data) > _MAX_FILE_BYTES:
        raise PricingError(f"{path.name}: rate file larger than 16 MiB")
    layer_name = name if name.startswith("user:") else f"user:{name}"
    return parse_layer(data, layer_name, origin=path.name)


def _read_builtin(filename: str) -> bytes:
    return importlib.resources.files("tokenbill.rates").joinpath("data").joinpath(
        filename).read_bytes()


def load_builtin(provider: str | None = None, *,
                 notes: list[DataQualityNote] | None = None) -> RateLayer:
    """The built-in registry as one layer ``builtin@<as_of>``.

    Rows and modifiers of ``rates/data/{anthropic,bedrock,vertex,openai}.json`` plus the rate files
    channel extensions ship (``core.extensions.extension_rate_files``; GitHub Copilot's
    ``github_copilot.json``). An extension rate file that is not installed is skipped with a
    ``dq.extension_unavailable`` note appended to *notes* (ruling R-E18); its channel's rows then
    come from the verified subset in ``core.facts`` (``copilot_rates()`` / ``copilot_modifiers()``),
    so Copilot usage stays priced. *provider* (``"anthropic"``, ``"openai"``, ``"github"``, …) keeps
    only that provider's rows and the modifiers of the files that carried them.
    """
    rows: list[RateRow] = []
    mods: list[Modifier] = []
    as_ofs: list[str] = []
    for filename in BUILTIN_FILES:
        file_provider, as_of, file_rows, file_mods = _parse_doc(_read_builtin(filename),
                                                                f"rates/data/{filename}")
        if provider is not None and file_provider != provider:
            continue
        rows.extend(file_rows)
        mods.extend(file_mods)
        as_ofs.append(as_of)
    covered: set[str] = set()
    for handle in _extensions.extension_rate_files(notes):
        origin = f"extension rate file {handle.name}"
        file_provider, as_of, file_rows, file_mods = _parse_doc(handle.read_bytes(), origin)
        covered.update(r.channel for r in file_rows)
        if provider is not None and file_provider != provider:
            continue
        rows.extend(file_rows)
        mods.extend(file_mods)
        as_ofs.append(as_of)
    facts_rows = [r for r in _facts.copilot_rates() if r.channel not in covered]
    if facts_rows and (provider is None or provider == "github"):
        logger.info("Copilot rate file unavailable: using the verified subset of core.facts")
        rows.extend(facts_rows)
        facts_channels = {r.channel for r in facts_rows}
        mods.extend(m for m in _facts.copilot_modifiers()
                    if set(dict(m.when).get("channel_in", "").split(",")) & facts_channels)
        as_ofs.append(max(r.verified_on for r in facts_rows))
    if not rows:
        raise UsageError(f"no built-in rate rows for provider {provider!r}")
    as_of = max(as_ofs)
    return make_layer(f"builtin@{as_of}", rows, mods, as_of=as_of, origin="builtin registry")


def layer_kind(layer: RateLayer) -> str:
    """``"builtin"`` | ``"model-price"`` | ``"user"`` (every other layer name)."""
    if layer.name.startswith("builtin"):
        return "builtin"
    if layer.name == MODEL_PRICE_LAYER:
        return "model-price"
    return "user"


def parse_model_price(text: str) -> tuple[str, str, str]:
    """Parse one ``--model-price MODEL=IN,OUT`` value into ``(model, in, out)`` decimal strings
    (USD per million tokens, ≤ 999,999.999999999). Malformed values raise ``UsageError``."""
    if not isinstance(text, str):
        raise UsageError("--model-price expects MODEL=IN,OUT")
    m = _MODEL_PRICE_RE.match(text)
    if m is None:
        raise UsageError("--model-price expects MODEL=IN,OUT")
    model, inp, out = m.group("model"), m.group("inp"), m.group("out")
    for value in (inp, out):
        if not re.fullmatch(r"(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,9})?", value):
            raise UsageError("--model-price rates must be decimal USD per million tokens "
                             "(0 to 999999.999999999)")
    if any(ord(c) < 32 or ord(c) == 127 for c in model) or len(model) > 128:
        raise UsageError("--model-price model id is not printable")
    return model, inp, out


def model_price_layer(specs: Sequence[tuple[str, str, str]]) -> RateLayer:
    """The ``--model-price`` overrides as layer ``model-price``: one row per model on every channel
    (``"*"``), effective from 1970-01-01, at *in* / *out* USD per MTok with the v0.1 cache defaults
    (reads 0.1×, 5m writes 1.25×, 1h writes 2×). A model given twice keeps its last price (v0.1
    behaviour). Invalid specs raise ``UsageError``."""
    latest: dict[str, tuple[str, str]] = {}
    for spec in specs:
        if not (isinstance(spec, tuple) and len(spec) == 3):
            raise UsageError("model_price_layer expects (model, in, out) tuples")
        model, inp, out = parse_model_price(f"{spec[0]}={spec[1]},{spec[2]}")
        latest[model] = (inp, out)
    rows = []
    source = (SourceCitation(url="cli:--model-price", retrieved="1970-01-01", finding=None),)
    for model, (inp, out) in sorted(latest.items()):
        rows.append(RateRow(
            row_id=f"model-price/*/{model}/1970-01-01", provider="user",
            channel=WILDCARD_CHANNEL, model=model, aliases=(), generation="0",
            effective_from="1970-01-01", effective_to=None, input_usd_per_mtok=Decimal(inp),
            output_usd_per_mtok=Decimal(out), cache_read_mult=Decimal("0.1"),
            cache_write_5m_mult=Decimal("1.25"), cache_write_1h_mult=Decimal("2"),
            cache_write_other_mult=None, cache_write_other_ttl_s=None, published_absolute=(),
            min_cacheable_tokens=None, tokenizer_family="unknown", per_request_usd=(),
            long_context_threshold=None, long_context_usd_per_mtok=(), supports=(),
            enabled=True, verified_on="1970-01-01", sources=source,
            notes="--model-price override (exempt from staleness)"))
    return make_layer(MODEL_PRICE_LAYER, rows, (), as_of="1970-01-01")
