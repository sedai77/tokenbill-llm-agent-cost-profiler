"""Loader of ``core/facts.json``, the single transcription of verified facts (SPEC §3.24, D37).

``load()`` parses the package data once (cached) into typed, immutable accessors; no other module
parses the JSON. Numbers that are money or multipliers are decimal strings in the file and
``Decimal`` here — a JSON float anywhere in the file is a load error. Every entry carries
``source``, ``finding``, ``verified_on`` and ``verification`` (``"primary"`` | ``"research"``).
"""

from __future__ import annotations

import copy
import functools
import importlib.resources
import json
import types
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tokenbill.core.errors import ContractViolation
from tokenbill.core.money import EXACT_CTX, usd
from tokenbill.core.types import Modifier, RateRow, SourceCitation

__all__ = [
    "FACTS_SCHEMA",
    "META_KEYS",
    "AnnouncedFact",
    "EvidenceFact",
    "Facts",
    "FocusColumn",
    "FocusSpec",
    "HeadlessFieldGroup",
    "PromotionFact",
    "SettingsKeyFact",
    "SkuRuleFact",
    "load",
    "parse",
]

FACTS_SCHEMA = "tokenbill/facts@1"
META_KEYS = ("source", "finding", "verified_on", "verification")
VERIFICATIONS = frozenset({"primary", "research"})
_RATE_BUCKETS = ("cache_read", "cache_write_5m", "cache_write_1h", "cache_write_other")


@dataclass(frozen=True, slots=True)
class SettingsKeyFact:
    key: str
    target: str
    domain: str
    min_version: str | None
    verified: bool
    lever_id: str | None
    tradeoff: bool
    managed_only: bool
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class EvidenceFact:
    name: str
    type: str  # "int" | "decimal" | "decimal_pair" | "formula" |
    # "str"
    value: Decimal | int | str | tuple[Decimal, Decimal]
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class PromotionFact:
    promotion_id: str
    model: str
    channel: str
    start: str
    not_before_end: str
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class AnnouncedFact:
    model: str
    announced_on: str
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class FocusColumn:
    name: str
    feature_level: str  # "Mandatory" | "Conditional" | "Recommended"
    allows_nulls: bool
    data_type: str
    column_type: str
    introduced: str


@dataclass(frozen=True, slots=True)
class FocusSpec:
    version: str
    ratified: str
    dataset: str
    columns: tuple[FocusColumn, ...]
    removed: tuple[tuple[str, tuple[str, ...]], ...]  # removed column → replacements
    custom_column_prefix: str
    service_category_ai: str
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""

    def column(self, name: str) -> FocusColumn:
        """The column named *name* (KeyError when FOCUS 1.4 has no such column)."""
        for col in self.columns:
            if col.name == name:
                return col
        raise KeyError(name)

    @property
    def mandatory(self) -> tuple[str, ...]:
        """Names of the Mandatory columns, in specification order."""
        return tuple(c.name for c in self.columns if c.feature_level == "Mandatory")


@dataclass(frozen=True, slots=True)
class HeadlessFieldGroup:
    group: str
    fields: Mapping[str, str]
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


@dataclass(frozen=True, slots=True)
class SkuRuleFact:
    source_kind: str
    pattern: str  # regular expression over the provider usage type / SKU id
    model: str | None  # None: the model comes from the product, not the usage type
    bucket: str
    endpoint_scope: str | None
    service_tier: str | None
    unit_tokens: int
    verified: bool
    source: str
    finding: str
    verified_on: str
    verification: str
    notes: str = ""


class Facts:
    """Typed, read-only view of ``facts.json``."""

    def __init__(self, raw: Mapping[str, Any]) -> None:
        self._raw = raw
        self.schema: str = raw["schema"]
        self.as_of: str = raw["as_of"]
        self.rate_rows: tuple[RateRow, ...] = tuple(_rate_row(r) for r in raw["rates"])
        self.modifiers: tuple[Modifier, ...] = tuple(_modifier(m) for m in raw["modifiers"])
        self.settings_keys: Mapping[str, SettingsKeyFact] = types.MappingProxyType(
            {k["key"]: SettingsKeyFact(**k) for k in raw["settings_keys"]}
        )
        self.evidence: Mapping[str, EvidenceFact] = types.MappingProxyType(
            {e["name"]: _evidence(e) for e in raw["evidence"]}
        )
        life = raw["lifecycle"]
        self.successors: Mapping[str, str] = types.MappingProxyType(
            {s["model"]: s["successor"] for s in life["successors"]}
        )
        self.retirements: Mapping[str, str] = types.MappingProxyType(
            {r["model"]: r["retirement_floor"] for r in life["retirements"]}
        )
        self.retired: Mapping[str, str] = types.MappingProxyType(
            {r["model"]: r["retired_on"] for r in life["retired"]}
        )
        self.promotions: tuple[PromotionFact, ...] = tuple(
            PromotionFact(**p) for p in life["promotions"]
        )
        self.announced: tuple[AnnouncedFact, ...] = tuple(
            AnnouncedFact(**a) for a in life["announced"]
        )
        self.focus: FocusSpec = _focus(raw["focus_columns"])
        self.headless_fields: Mapping[str, HeadlessFieldGroup] = types.MappingProxyType(
            {
                g["group"]: HeadlessFieldGroup(
                    **{**g, "fields": types.MappingProxyType(dict(g["fields"]))}
                )
                for g in raw["headless_fields"]
            }
        )
        self.sku_rules: tuple[SkuRuleFact, ...] = tuple(SkuRuleFact(**s) for s in raw["sku_rules"])

    def rate_row(self, row_id: str) -> RateRow:
        """The rate row with *row_id* (KeyError when absent)."""
        for row in self.rate_rows:
            if row.row_id == row_id:
                return row
        raise KeyError(row_id)

    def rows_for(self, model: str, channel: str = "anthropic_api") -> tuple[RateRow, ...]:
        """Every row (enabled or not) for ``(channel, model)``, ordered by ``effective_from``."""
        rows = [r for r in self.rate_rows if r.model == model and r.channel == channel]
        return tuple(sorted(rows, key=lambda r: r.effective_from))

    def modifier(self, modifier_id: str) -> Modifier:
        """The modifier with *modifier_id* (KeyError when absent)."""
        for m in self.modifiers:
            if m.modifier_id == modifier_id:
                return m
        raise KeyError(modifier_id)

    def rate_rows_json(self) -> list[dict[str, Any]]:
        """Deep copies of the raw ``tokenbill/rates@1``-shaped rows (for registry parity tests)."""
        return copy.deepcopy(list(self._raw["rates"]))

    def modifiers_json(self) -> list[dict[str, Any]]:
        """Deep copies of the raw modifier objects."""
        return copy.deepcopy(list(self._raw["modifiers"]))

    def entries(self) -> Iterator[tuple[str, Mapping[str, Any]]]:
        """``(section, raw entry)`` for every fact entry (each carries the META_KEYS)."""
        raw = self._raw
        for section in (
            "rates",
            "modifiers",
            "settings_keys",
            "evidence",
            "headless_fields",
            "sku_rules",
        ):
            for entry in raw[section]:
                yield section, entry
        for part, items in raw["lifecycle"].items():
            for entry in items:
                yield f"lifecycle.{part}", entry
        yield "focus_columns", raw["focus_columns"]


def _dec(value: Any, what: str) -> Decimal:
    if not isinstance(value, str):
        raise ContractViolation(f"facts: {what} must be a decimal string")
    return usd(value)


def _dec_pairs(obj: Mapping[str, Any], what: str) -> tuple[tuple[str, Decimal], ...]:
    return tuple(sorted((k, _dec(v, what)) for k, v in obj.items()))


def _citations(items: Any) -> tuple[SourceCitation, ...]:
    return tuple(
        SourceCitation(url=s["url"], retrieved=s["retrieved"], finding=s.get("finding"))
        for s in items
    )


def _rate_row(r: Mapping[str, Any]) -> RateRow:
    mult = r.get("multipliers", {})
    lc = r.get("long_context")
    row = RateRow(
        row_id=r["row_id"],
        provider=r["provider"],
        channel=r["channel"],
        model=r["model"],
        aliases=tuple(r["aliases"]),
        generation=r["generation"],
        effective_from=r["effective_from"],
        effective_to=r["effective_to"],
        input_usd_per_mtok=_dec(r["usd_per_mtok"]["input"], "input"),
        output_usd_per_mtok=_dec(r["usd_per_mtok"]["output"], "output"),
        cache_read_mult=_dec(mult["cache_read"], "multiplier") if "cache_read" in mult else None,
        cache_write_5m_mult=_dec(mult["cache_write_5m"], "multiplier")
        if "cache_write_5m" in mult
        else None,
        cache_write_1h_mult=_dec(mult["cache_write_1h"], "multiplier")
        if "cache_write_1h" in mult
        else None,
        cache_write_other_mult=(
            _dec(mult["cache_write_other"], "multiplier") if "cache_write_other" in mult else None
        ),
        cache_write_other_ttl_s=r.get("cache_write_other_ttl_s"),
        published_absolute=_dec_pairs(r.get("published_absolute", {}), "published_absolute"),
        min_cacheable_tokens=r["min_cacheable_tokens"],
        tokenizer_family=r["tokenizer_family"],
        per_request_usd=_dec_pairs(r.get("per_request_usd", {}), "per_request_usd"),
        long_context_threshold=lc["threshold"] if lc else None,
        long_context_usd_per_mtok=_dec_pairs(lc["usd_per_mtok"], "long_context") if lc else (),
        supports=tuple(r["supports"]),
        enabled=r["enabled"],
        verified_on=r["verified_on"],
        sources=_citations(r["sources"]),
        promotion=r.get("promotion"),
        notes=r.get("notes", ""),
    )
    # Published absolute cache prices must equal input × multiplier exactly (the RATES load rule).
    for bucket, published in row.published_absolute:
        m = {
            "cache_read": row.cache_read_mult,
            "cache_write_5m": row.cache_write_5m_mult,
            "cache_write_1h": row.cache_write_1h_mult,
            "cache_write_other": row.cache_write_other_mult,
        }.get(bucket)
        if m is None or EXACT_CTX.multiply(row.input_usd_per_mtok, m) != published:
            raise ContractViolation(f"facts: {row.row_id} published {bucket} != input x multiplier")
    return row


def _modifier(m: Mapping[str, Any]) -> Modifier:
    return Modifier(
        modifier_id=m["modifier_id"],
        kind=m["kind"],
        factor=_dec(m["factor"], "factor") if m.get("factor") is not None else None,
        base_usd_per_mtok=_dec_pairs(m.get("base_usd_per_mtok", {}), "base_usd_per_mtok"),
        applies_to=tuple(m["applies_to"]),
        when=tuple(sorted((k, str(v)) for k, v in m["when"].items())),
        stacking=m["stacking"],
        sources=_citations(m["sources"]),
    )


def _evidence(e: Mapping[str, Any]) -> EvidenceFact:
    typ, raw = e["type"], e["value"]
    value: Decimal | int | str | tuple[Decimal, Decimal]
    if typ == "int":
        if type(raw) is not int:
            raise ContractViolation(f"facts: evidence {e['name']} must be an int")
        value = raw
    elif typ == "decimal":
        value = _dec(raw, e["name"])
    elif typ == "decimal_pair":
        if not isinstance(raw, list) or len(raw) != 2:
            raise ContractViolation(f"facts: evidence {e['name']} must be a pair")
        value = (_dec(raw[0], e["name"]), _dec(raw[1], e["name"]))
    elif typ in ("formula", "str"):
        if not isinstance(raw, str):
            raise ContractViolation(f"facts: evidence {e['name']} must be a string")
        value = raw
    else:
        raise ContractViolation(f"facts: evidence {e['name']} has unknown type")
    return EvidenceFact(
        name=e["name"],
        type=typ,
        value=value,
        source=e["source"],
        finding=e["finding"],
        verified_on=e["verified_on"],
        verification=e["verification"],
        notes=e.get("notes", ""),
    )


def _focus(f: Mapping[str, Any]) -> FocusSpec:
    return FocusSpec(
        version=f["version"],
        ratified=f["ratified"],
        dataset=f["dataset"],
        columns=tuple(FocusColumn(**c) for c in f["columns"]),
        removed=tuple((r["name"], tuple(r["replaced_by"])) for r in f["removed_in_1_4"]),
        custom_column_prefix=f["custom_column_prefix"],
        service_category_ai=f["service_category_ai"],
        source=f["source"],
        finding=f["finding"],
        verified_on=f["verified_on"],
        verification=f["verification"],
        notes=f.get("notes", ""),
    )


def _reject_float(token: str) -> Any:
    raise ContractViolation("facts: JSON floats are not allowed (use decimal strings)")


def parse(text: str) -> Facts:
    """Parse and validate a facts document (the text of ``facts.json``)."""
    try:
        raw = json.loads(text, parse_float=_reject_float, parse_constant=_reject_float)
    except ValueError as exc:
        raise ContractViolation(f"facts: invalid JSON ({type(exc).__name__})") from None
    if not isinstance(raw, dict) or raw.get("schema") != FACTS_SCHEMA:
        raise ContractViolation(f"facts: schema must be {FACTS_SCHEMA}")
    try:
        facts = Facts(raw)
    except (KeyError, TypeError) as exc:
        raise ContractViolation(
            f"facts: malformed document ({type(exc).__name__}: {exc})"
        ) from None
    for section, entry in facts.entries():
        for key in META_KEYS:
            if not isinstance(entry.get(key), str) or not entry[key]:
                raise ContractViolation(f"facts: an entry of {section} lacks {key}")
        if entry["verification"] not in VERIFICATIONS:
            raise ContractViolation(f"facts: an entry of {section} has an unknown verification")
    return facts


@functools.lru_cache(maxsize=1)
def load() -> Facts:
    """The packaged ``tokenbill/core/facts.json`` (parsed once per process)."""
    text = (
        importlib.resources.files("tokenbill.core")
        .joinpath("facts.json")
        .read_text(encoding="utf-8")
    )
    return parse(text)
