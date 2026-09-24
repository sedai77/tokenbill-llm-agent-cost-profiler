"""Failure-mode billing rules (SPEC §6.6, D10; ``rates/data/billing_rules.json``).

Each rule maps ``(provider, failure_mode)`` to what is billed: ``billed_input``,
``billed_partial_output`` and ``cache_written`` (``"yes"`` | ``"no"`` | ``"unknown"``) with a
``confidence`` (``"documented"`` | ``"assumed"`` | ``"unknown"``) and its source. Documented rules
price EXACT (billed → full price, not billed → EXACT 0); assumed and unknown rules price as a range
``[0, full]`` (ESTIMATED), which reports show as "assumed" dollars.
:attr:`BillingRule.billable` is the ``Inference.billable`` value an adapter sets for usage under a
rule.
"""

from __future__ import annotations

import functools
import importlib.resources
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from tokenbill.core.errors import PricingError, UsageError

__all__ = [
    "CONFIDENCES",
    "SCHEMA",
    "BillingRule",
    "parse_rules",
    "refusal_rule",
    "rule",
    "rule_for",
    "rules",
]

SCHEMA = "tokenbill/billing-rules@1"
CONFIDENCES = ("documented", "assumed", "unknown")
_BILLED = ("yes", "no", "unknown")
_NAME_RE = re.compile(r"[a-z0-9_]+(?:\.[a-z0-9_]+)*\Z")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


@dataclass(frozen=True, slots=True)
class BillingRule:
    """One failure-mode billing rule."""

    rule_id: str                 # "<provider>.<failure_mode>"
    provider: str
    failure_mode: str
    billed_input: str            # "yes" | "no" | "unknown"
    billed_partial_output: str   # "yes" | "no" | "unknown"
    cache_written: str           # "yes" | "no" | "unknown"
    confidence: str              # "documented" | "assumed" | "unknown"
    source: str
    notes: str = ""
    finding: str | None = None
    verified_on: str | None = None
    params: tuple[tuple[str, int], ...] = ()
    known: bool = True           # False for the default rule of an undocumented failure mode

    @property
    def billable(self) -> bool | None:
        """``Inference.billable`` under this rule: True when documented as billed, False when
        documented as not billed, None (range ``[0, full]``) when assumed or unknown."""
        if self.confidence != "documented":
            return None
        billed = (self.billed_input, self.billed_partial_output)
        if "yes" in billed:
            return True
        if billed == ("no", "no"):
            return False
        return None

    def param(self, name: str) -> int:
        """An integer parameter of the rule (``UsageError`` when absent)."""
        for key, value in self.params:
            if key == name:
                return value
        raise UsageError(f"billing rule {self.rule_id} has no parameter {name!r}")


def _field(r: Mapping[str, Any], key: str, where: str, allowed: tuple[str, ...] | None = None,
           *, optional: bool = False) -> Any:
    value = r.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or (allowed is not None and value not in allowed):
        raise PricingError(f"billing rules: {where}: invalid {key}")
    return value


def parse_rules(text: str | bytes) -> tuple[BillingRule, ...]:
    """Parse and validate a ``tokenbill/billing-rules@1`` document (``PricingError`` on any
    defect: unknown values, a rule id that is not ``<provider>.<failure_mode>``, duplicates)."""
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        raise PricingError("billing rules: not valid JSON") from None
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA \
            or not isinstance(doc.get("rules"), list):
        raise PricingError(f"billing rules: schema must be {SCHEMA} with a rules list")
    out: dict[str, BillingRule] = {}
    for i, r in enumerate(doc["rules"]):
        if not isinstance(r, dict):
            raise PricingError(f"billing rules: rules[{i}] must be an object")
        where = f"rules[{i}]"
        rule_id = _field(r, "rule_id", where)
        provider = _field(r, "provider", where)
        mode = _field(r, "failure_mode", where)
        if rule_id != f"{provider}.{mode}" or not _NAME_RE.match(rule_id):
            raise PricingError(f"billing rules: {where}: rule_id must be <provider>.<mode>")
        params = r.get("params") or {}
        if not isinstance(params, dict) or any(
                not isinstance(k, str) or type(v) is not int or v < 0 for k, v in params.items()):
            raise PricingError(f"billing rules: {where}: params must map names to integers")
        verified = _field(r, "verified_on", where, optional=True)
        if verified is not None and not _DATE_RE.match(verified):
            raise PricingError(f"billing rules: {where}: verified_on must be a date")
        notes = r.get("notes", "")
        if not isinstance(notes, str):
            raise PricingError(f"billing rules: {where}: notes must be a string")
        if rule_id in out:
            raise PricingError(f"billing rules: duplicate rule {rule_id}")
        out[rule_id] = BillingRule(
            rule_id=rule_id, provider=provider, failure_mode=mode,
            billed_input=_field(r, "billed_input", where, _BILLED),
            billed_partial_output=_field(r, "billed_partial_output", where, _BILLED),
            cache_written=_field(r, "cache_written", where, _BILLED),
            confidence=_field(r, "confidence", where, CONFIDENCES),
            source=_field(r, "source", where), notes=notes,
            finding=_field(r, "finding", where, optional=True), verified_on=verified,
            params=tuple(sorted(params.items())))
    return tuple(out.values())


@functools.lru_cache(maxsize=1)
def rules() -> tuple[BillingRule, ...]:
    """Every packaged rule (``rates/data/billing_rules.json``), in file order."""
    data = importlib.resources.files("tokenbill.rates").joinpath("data").joinpath(
        "billing_rules.json").read_bytes()
    return parse_rules(data)


@functools.lru_cache(maxsize=1)
def _by_id() -> dict[str, BillingRule]:
    return {r.rule_id: r for r in rules()}


def rule(rule_id: str) -> BillingRule:
    """The packaged rule with id *rule_id* (``UsageError`` when there is none)."""
    found = _by_id().get(rule_id) if isinstance(rule_id, str) else None
    if found is None:
        raise UsageError("unknown billing rule id")
    return found


def rule_for(provider: str, failure_mode: str) -> BillingRule:
    """The rule for *failure_mode* on *provider*; an undocumented combination gets a default rule
    (confidence ``unknown``, billed ``unknown``, ``known=False``) that prices as ``[0, full]``."""
    if not isinstance(provider, str) or not isinstance(failure_mode, str):
        raise UsageError("rule_for expects provider and failure_mode strings")
    found = _by_id().get(f"{provider}.{failure_mode}")
    if found is not None:
        return found
    return BillingRule(rule_id=f"{provider}.{failure_mode}", provider=provider,
                       failure_mode=failure_mode, billed_input="unknown",
                       billed_partial_output="unknown", cache_written="unknown",
                       confidence="unknown", source="no documented rule",
                       notes="undocumented failure mode: priced as a range [0, full]",
                       known=False)


def refusal_rule(output_tokens: int, *, threshold: int | None = None) -> BillingRule:
    """The Anthropic refusal rule for a declined attempt with *output_tokens* output tokens (D10):
    0 → ``anthropic.refusal.pre_output`` (not billed); 1 … *threshold* (default: the
    ``max_output_tokens`` parameter of ``anthropic.refusal.ambiguous``, 16) → ``ambiguous`` (range);
    more → ``mid_stream`` (billed)."""
    if type(output_tokens) is not int or output_tokens < 0:
        raise UsageError("output_tokens must be a non-negative int")
    if output_tokens == 0:
        return rule("anthropic.refusal.pre_output")
    ambiguous = rule("anthropic.refusal.ambiguous")
    limit = ambiguous.param("max_output_tokens") if threshold is None else threshold
    if type(limit) is not int or limit < 0:
        raise UsageError("threshold must be a non-negative int")
    return ambiguous if output_tokens <= limit else rule("anthropic.refusal.mid_stream")
