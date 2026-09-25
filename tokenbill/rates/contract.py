"""Contract overlays (SPEC §6.5): Token Bill contract files and Claude Code ``modelPricing``.

A :class:`~tokenbill.core.types.ContractOverlay` holds a ``multiplier`` (applied to every bucket
after list resolution) and per-model ``overrides`` (bucket → USD per MTok, final prices); the rate
card applies it last, only on the overlay's channels (all when empty), never on the
``subscription`` path or on GitHub Copilot, and prices with basis CONTRACT where it applies.

Formats:

* ``tokenbill/contract@1`` (:func:`load_contract`, :func:`contract_json`): ``{"schema", "name",
  "multiplier": "0.85" | null, "overrides": {model: {bucket: "rate"}}, "effective_from",
  "effective_to", "derived", "assumed_fields", "channels"}`` with decimal strings.
* Claude Code's managed ``modelPricing`` (:func:`from_model_pricing`, :func:`to_model_pricing`):
  ``{"multiplier": number, "overrides": {model: {"input", "output", "cacheRead", "cacheWrite"}}}``
  in USD per MTok as JSON numbers (shape verified in ``core.facts`` settings keys). ``cacheWrite``
  covers both TTLs, so on import the 5m rate is ``cacheWrite`` and the 1h rate is derived as
  ``cacheWrite × 2 / 1.25`` and listed in ``assumed_fields`` (``"cache_write_1h"`` when every
  model's 1h rate is derived, else ``"cache_write_1h@<model>"`` per derived model) — unless the
  block states ``cacheWrite1h``. Numbers may be ints, decimal strings, ``Decimal`` or JSON floats
  (read through their shortest ``repr``, so ``0.85`` is exactly ``Decimal("0.85")``);
  :func:`to_model_pricing`
  returns ``Decimal`` / ``int`` numbers so ``to_model_pricing(from_model_pricing(x)) == x`` for a
  block parsed with ``parse_float=Decimal``, and :func:`dumps_model_pricing` writes them as exact
  JSON numbers.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

from tokenbill.core.errors import PricingError, SourceError
from tokenbill.core.money import EXACT_CTX
from tokenbill.core.records import to_json
from tokenbill.core.types import ContractOverlay

__all__ = [
    "BUCKETS",
    "SCHEMA",
    "contract_json",
    "dumps_model_pricing",
    "from_model_pricing",
    "load_contract",
    "make_overlay",
    "to_model_pricing",
]

SCHEMA = "tokenbill/contract@1"
#: Buckets a contract override may price (``web_search`` is per request).
BUCKETS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h",
           "cache_write_other", "web_search")
_MP_KEYS = {"input": "input", "output": "output", "cacheRead": "cache_read",
            "cacheWrite": "cache_write_5m", "cacheWrite1h": "cache_write_1h"}
_ONE_HOUR_FROM_WRITE = EXACT_CTX.divide(Decimal(2), Decimal("1.25"))   # 1.6
_DECIMAL_RE = re.compile(r"(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,18})?\Z")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_NAME_RE = re.compile(r"[^\x00-\x1f\x7f]{1,128}\Z")
_MAX_BYTES = 4 * 2**20
_MAX_MULTIPLIER = Decimal(10)


def _num(value: object, what: str) -> Decimal:
    """A non-negative exact decimal from a JSON number, decimal string or Decimal."""
    if isinstance(value, bool):
        raise PricingError(f"contract: {what} must be a number")
    if isinstance(value, int):
        d = Decimal(value)
    elif isinstance(value, Decimal):
        d = value
    elif isinstance(value, str):
        if not _DECIMAL_RE.match(value.strip()):
            raise PricingError(f"contract: {what} must be a non-negative decimal")
        d = Decimal(value.strip())
    elif isinstance(value, float):   # a JSON number read by json.loads: its shortest repr
        text = repr(value)
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?", text):
            raise PricingError(f"contract: {what} must be a finite, non-negative number")
        d = Decimal(text)
    else:
        raise PricingError(f"contract: {what} must be a number")
    if not d.is_finite() or d < 0 or d.adjusted() > 11 or d.as_tuple().exponent < -18:  # type: ignore[operator]
        raise PricingError(f"contract: {what} is out of range")
    return d


def _canonical(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def make_overlay(*, name: str, multiplier: Decimal | None,
                 overrides: Mapping[str, Mapping[str, Decimal]], effective_from: str,
                 effective_to: str | None = None, derived: bool = False,
                 assumed_fields: Sequence[str] = (),
                 channels: Sequence[str] = ()) -> ContractOverlay:
    """A validated :class:`ContractOverlay` with its ``sha256`` (sorted overrides and channels)."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise PricingError("contract: name must be a printable string")
    if multiplier is not None and multiplier > _MAX_MULTIPLIER:
        raise PricingError("contract: multiplier above 10")
    for date in (effective_from, effective_to):
        if date is not None and (not isinstance(date, str) or not _DATE_RE.match(date)):
            raise PricingError("contract: effective dates must be YYYY-MM-DD")
    if effective_to is not None and effective_to <= effective_from:
        raise PricingError("contract: effective_to must be after effective_from")
    rows = []
    for model, buckets in sorted(overrides.items()):
        if not isinstance(model, str) or not _NAME_RE.match(model):
            raise PricingError("contract: override model ids must be printable strings")
        for bucket in buckets:
            if bucket not in BUCKETS:
                raise PricingError(f"contract: unknown override bucket {bucket!r}")
        rows.append((model, tuple(sorted(buckets.items()))))
    if any(f.partition("@")[0] not in BUCKETS for f in assumed_fields):
        raise PricingError("contract: assumed_fields must name buckets")
    if any(not isinstance(c, str) or not c for c in channels):
        raise PricingError("contract: channels must be non-empty strings")
    overlay = ContractOverlay(name=name, multiplier=multiplier, overrides=tuple(rows),
                              effective_from=effective_from, effective_to=effective_to,
                              derived=bool(derived), assumed_fields=tuple(sorted(assumed_fields)),
                              channels=tuple(sorted(channels)))
    sha = hashlib.sha256(_canonical(to_json(overlay)).encode("utf-8")).hexdigest()
    return ContractOverlay(name=overlay.name, multiplier=overlay.multiplier,
                           overrides=overlay.overrides, effective_from=overlay.effective_from,
                           effective_to=overlay.effective_to, derived=overlay.derived,
                           assumed_fields=overlay.assumed_fields, channels=overlay.channels,
                           sha256=sha)


def _reject_float(token: str) -> Any:
    raise PricingError("contract: numbers must be decimal strings")


def load_contract(path: Path) -> ContractOverlay:
    """Load a ``tokenbill/contract@1`` file; a JSON object with a top-level ``modelPricing`` key (a
    Claude Code managed-settings file) is imported with :func:`from_model_pricing` under the file's
    stem. ``SourceError`` when unreadable, ``PricingError`` when invalid."""
    path = Path(path)
    try:
        with path.open("rb") as fh:
            data = fh.read(_MAX_BYTES + 1)
    except OSError:
        raise SourceError(f"cannot read contract file {path.name}") from None
    if len(data) > _MAX_BYTES:
        raise PricingError("contract: file larger than 4 MiB")
    try:
        doc = json.loads(data, parse_float=Decimal)
    except (ValueError, RecursionError):
        raise PricingError(f"contract: {path.name} is not valid JSON") from None
    if isinstance(doc, dict) and "modelPricing" in doc and "schema" not in doc:
        block = doc["modelPricing"]
        if not isinstance(block, dict):
            raise PricingError("contract: modelPricing must be an object")
        return from_model_pricing(block, name=path.stem)
    try:
        doc = json.loads(data, parse_float=_reject_float, parse_constant=_reject_float)
    except (ValueError, RecursionError):
        raise PricingError(f"contract: {path.name} is not valid JSON") from None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise PricingError(f"contract: schema must be {SCHEMA}")
    raw_overrides = doc.get("overrides") or {}
    if not isinstance(raw_overrides, dict) or any(not isinstance(v, dict)
                                                  for v in raw_overrides.values()):
        raise PricingError("contract: overrides must map models to bucket objects")
    overrides = {model: {b: _num(v, f"overrides.{model}.{b}") for b, v in buckets.items()}
                 for model, buckets in raw_overrides.items()}
    multiplier = doc.get("multiplier")
    for key in ("assumed_fields", "channels"):
        if not isinstance(doc.get(key, []), list):
            raise PricingError(f"contract: {key} must be a list")
    if type(doc.get("derived", False)) is not bool:
        raise PricingError("contract: derived must be true or false")
    return make_overlay(name=doc.get("name"),  # type: ignore[arg-type]
                        multiplier=None if multiplier is None else _num(multiplier, "multiplier"),
                        overrides=overrides, effective_from=doc.get("effective_from"),  # type: ignore[arg-type]
                        effective_to=doc.get("effective_to"), derived=doc.get("derived", False),
                        assumed_fields=doc.get("assumed_fields", []),
                        channels=doc.get("channels", []))


def contract_json(overlay: ContractOverlay) -> dict[str, object]:
    """The ``tokenbill/contract@1`` document of *overlay* (decimal strings), which
    :func:`load_contract` reads back to an equal overlay."""
    return {
        "schema": SCHEMA,
        "name": overlay.name,
        "multiplier": None if overlay.multiplier is None else str(overlay.multiplier),
        "overrides": {model: {b: str(v) for b, v in buckets} for model, buckets in
                      overlay.overrides},
        "effective_from": overlay.effective_from,
        "effective_to": overlay.effective_to,
        "derived": overlay.derived,
        "assumed_fields": list(overlay.assumed_fields),
        "channels": list(overlay.channels),
    }


def from_model_pricing(obj: Mapping[str, object], *, name: str,
                       effective_from: str = "1970-01-01",
                       channels: Sequence[str] = ()) -> ContractOverlay:
    """Import Claude Code's managed ``modelPricing`` block as a contract overlay (§6.5).

    ``cacheWrite`` becomes the 5m write rate and, unless ``cacheWrite1h`` is given, the 1h rate
    ``cacheWrite × 2 / 1.25`` listed in ``assumed_fields``. Unknown keys raise ``PricingError``."""
    if not isinstance(obj, Mapping):
        raise PricingError("modelPricing must be an object")
    unknown = set(obj) - {"multiplier", "overrides"}
    if unknown:
        raise PricingError("modelPricing: unknown keys")
    multiplier = obj.get("multiplier")
    overrides_in = obj.get("overrides")
    if overrides_in is None:
        overrides_in = {}
    if not isinstance(overrides_in, Mapping):
        raise PricingError("modelPricing.overrides must be an object")
    overrides: dict[str, dict[str, Decimal]] = {}
    derived: list[str] = []
    for model, rates in overrides_in.items():
        if not isinstance(rates, Mapping) or set(rates) - set(_MP_KEYS):
            raise PricingError("modelPricing.overrides entries use input, output, cacheRead, "
                               "cacheWrite (and optionally cacheWrite1h)")
        buckets = {_MP_KEYS[k]: _num(v, f"modelPricing.overrides.{k}") for k, v in rates.items()}
        if "cache_write_5m" in buckets and "cache_write_1h" not in buckets:
            buckets["cache_write_1h"] = EXACT_CTX.multiply(buckets["cache_write_5m"],
                                                           _ONE_HOUR_FROM_WRITE)
            derived.append(str(model))
        overrides[str(model)] = buckets
    stated = [m for m, b in overrides.items() if "cache_write_1h" in b and m not in derived]
    assumed = ["cache_write_1h"] if derived and not stated else [
        f"cache_write_1h@{m}" for m in derived]
    return make_overlay(name=name, multiplier=None if multiplier is None else _num(multiplier,
                                                                                    "multiplier"),
                        overrides=overrides, effective_from=effective_from,
                        assumed_fields=assumed, channels=channels)


def _number(d: Decimal) -> Decimal | int:
    return int(d) if d == d.to_integral_value() else d


def to_model_pricing(overlay: ContractOverlay) -> dict[str, object]:
    """The managed ``modelPricing`` block of *overlay* (numbers as ``int`` or ``Decimal``): the
    multiplier (when set) and per-model ``input``, ``output``, ``cacheRead``, ``cacheWrite`` (the 5m
    rate); ``cacheWrite1h`` only when the 1h rate is stated rather than derived. Buckets
    ``modelPricing`` cannot express (``cache_write_other``, ``web_search``) are left out."""
    if not isinstance(overlay, ContractOverlay):
        raise PricingError("to_model_pricing expects a ContractOverlay")
    out: dict[str, object] = {}
    if overlay.multiplier is not None:
        out["multiplier"] = _number(overlay.multiplier)
    reverse = {v: k for k, v in _MP_KEYS.items()}
    overrides: dict[str, dict[str, Decimal | int]] = {}
    for model, buckets in overlay.overrides:
        entry: dict[str, Decimal | int] = {}
        assumed_1h = ("cache_write_1h" in overlay.assumed_fields
                      or f"cache_write_1h@{model}" in overlay.assumed_fields)
        for bucket, value in buckets:
            key = reverse.get(bucket)
            if key is None or (key == "cacheWrite1h" and assumed_1h):
                continue
            entry[key] = _number(value)
        overrides[model] = entry
    if overrides or "multiplier" not in out:
        out["overrides"] = overrides
    return out


def _json_value(value: object) -> str:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)}: {_json_value(v)}" for k, v in value.items()) \
            + "}"
    return json.dumps(value)


def dumps_model_pricing(overlay: ContractOverlay) -> str:
    """:func:`to_model_pricing` as JSON text with exact numbers (no float round trip)."""
    return _json_value(to_model_pricing(overlay))
