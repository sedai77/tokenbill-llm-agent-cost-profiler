"""SARIF 2.1.0 for ``tokenbill check`` (SPEC §14.5, §15.2; package OUT).

:func:`to_sarif` maps a :class:`~tokenbill.core.types.CheckResult` to one SARIF run whose driver
declares the four rules of SPEC §14.5 (:data:`RULES`) and which has **one result per violation**.
A violation location ``"<file name>#run=<run>#call=<index>"`` becomes an artifact URI (the file
name) plus a logical location (``run=<run>#call=<index>``); every message is content-free and
sanitized. Results carry a stable ``partialFingerprints`` entry so code-scanning tools track them
across runs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

from tokenbill.core.errors import ContractViolation
from tokenbill.core.textsafe import sanitize
from tokenbill.core.types import CheckResult

__all__ = ["LEVELS", "RULES", "SARIF_SCHEMA", "SARIF_VERSION", "to_sarif"]

SARIF_VERSION = "2.1.0"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"
#: SARIF result levels a ``CheckViolation.level`` may map to.
LEVELS = ("error", "warning", "note", "none")
#: The rules of ``tokenbill check`` (SPEC §14.5): id → (name, short description, full description).
RULES: Mapping[str, tuple[str, str, str]] = {
    "TB-CACHE-SHARE": (
        "CacheReadShare",
        "Cache-read share below the threshold",
        "The share of input tokens read from the prompt cache after the warm-up turns is below "
        "--min-cache-read-share; the agent re-sends context the provider already cached."),
    "TB-NEW-BREAKER": (
        "NewCacheBreaker",
        "New cache-breaker kind versus the baseline",
        "A block-level cache breaker (e.g. a volatile system prompt or reordered tools) appears "
        "that the baseline run did not have."),
    "TB-COST-REGRESSION": (
        "CostRegression",
        "Median cost per run regressed",
        "The median cost per run exceeds the baseline median by more than "
        "--max-cost-regression-pct (constant prices, at least --min-runs runs)."),
    "TB-SERIALIZATION-CHURN": (
        "SerializationChurn",
        "Serialization churn breaks the cache",
        "Byte-level serialization of an unchanged prefix differs between calls (e.g. key order "
        "or whitespace), so the provider cannot reuse the cache."),
}
_INFO_URI = "https://github.com/sedai77/tokenbill-llm-agent-cost-profiler"


def _split(location: str) -> tuple[str, str | None]:
    text = sanitize(location, 512)
    name, sep, rest = text.partition("#")
    return (name or "unknown", rest if sep else None)


def to_sarif(check: CheckResult, *, tool_version: str) -> dict[str, object]:
    """A SARIF 2.1.0 log of *check*: ``version``, ``$schema`` and one run with the tool driver
    (name, version, the four rules), one result per violation (``ruleId``, ``ruleIndex``,
    ``level``, ``message.text``, ``locations``, ``partialFingerprints``) and an invocation whose
    ``executionSuccessful`` is True (the check itself ran; pass/fail is in the results and in
    ``properties.passed``). An unknown rule id or level raises ``ContractViolation``."""
    if not isinstance(check, CheckResult):
        raise ContractViolation("to_sarif expects a CheckResult")
    ids = list(RULES)
    rules = [{"id": rid, "name": name, "shortDescription": {"text": short},
              "fullDescription": {"text": full}, "helpUri": _INFO_URI,
              "defaultConfiguration": {"level": "error"}}
             for rid, (name, short, full) in RULES.items()]
    results = []
    for v in check.violations:
        if v.rule_id not in RULES:
            raise ContractViolation(f"unknown check rule {v.rule_id!r}")
        if v.level not in LEVELS:
            raise ContractViolation(f"unknown SARIF level {v.level!r}")
        artifact, logical = _split(v.location)
        location: dict[str, object] = {
            "physicalLocation": {"artifactLocation": {"uri": artifact}}}
        if logical is not None:
            location["logicalLocations"] = [{"fullyQualifiedName": logical, "kind": "function"}]
        digest = hashlib.sha256(f"{v.rule_id}\x1f{sanitize(v.location, 512)}".encode()).hexdigest()
        results.append({
            "ruleId": v.rule_id, "ruleIndex": ids.index(v.rule_id), "level": v.level,
            "message": {"text": sanitize(v.message, 1000) or v.rule_id},
            "locations": [location],
            "partialFingerprints": {"tokenbillViolation/v1": digest[:32]},
        })
    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [{
            "tool": {"driver": {"name": "tokenbill", "version": sanitize(tool_version, 64),
                                "informationUri": _INFO_URI, "rules": rules}},
            "invocations": [{"executionSuccessful": True}],
            "results": results,
            "properties": {"passed": check.passed, "runs": check.runs,
                           "cacheReadShare": check.cache_read_share,
                           "breakerKinds": list(check.breaker_kinds)},
        }],
    }
