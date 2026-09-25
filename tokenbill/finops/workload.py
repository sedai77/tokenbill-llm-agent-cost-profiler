"""Workload classifier (SPEC §14.6; package OUT).

``classify(lane) -> (WorkloadClass, confidence)`` collects every rule that fires on the lane and
returns the most confident one; ties go to the more automated class (:data:`AUTOMATION_RANK`).
Confidences are decimal strings.

=====================================================================  ===========  ====
signal                                                                 class        conf
=====================================================================  ===========  ====
resource tag ``workload=…`` (``Attribution.workload_class`` is set)    as tagged    0.99
most serving inferences on ``service_tier == "batch"``                 BATCH        0.99
entrypoint ``claude-code-github-action``                               CI           0.95
the allocation rule set (``rules=``) sets a workload class             as set       0.9
≥ 4 requests, CV of gaps < 0.25, no human prompt, mean gap > 5 min     SCHEDULED    0.8
≥ 10 requests, no human prompt, main/API/unknown lane, not ``cli``     SERVICE      0.7
entrypoint ``sdk-py`` / ``sdk-ts`` / ``sdk-cli``                       SERVICE      0.6
a HUMAN_PROMPT event, or entrypoint ``cli`` (interactive Claude Code)  INTERACTIVE  0.5
=====================================================================  ===========  ====

The last row and the cadence guards (no human prompt, mean gap above the 5-minute cache TTL, as
in DETECT-OTHER's ``scheduled-cadence``) are this package's documented additions to the SPEC list,
so an interactive agent loop with regular tool-call gaps is not called scheduled and an interactive
session is not left ``unknown``. Nothing fires → ``(UNKNOWN, "0")``. The cadence test is exact
integer arithmetic (CV < 0.25 ⇔ 16·variance < mean²).
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal

from tokenbill.core.errors import ContractViolation
from tokenbill.core.records import Lane, LaneEventKind, LaneKind, WorkloadClass

__all__ = [
    "AUTOMATION_RANK",
    "CI_ENTRYPOINTS",
    "INTERACTIVE_ENTRYPOINTS",
    "SDK_ENTRYPOINTS",
    "classify",
    "signals",
]

#: Tie-break order: the more automated class wins.
AUTOMATION_RANK: dict[WorkloadClass, int] = {
    WorkloadClass.BATCH: 6, WorkloadClass.SCHEDULED: 5, WorkloadClass.CI: 4,
    WorkloadClass.SERVICE: 3, WorkloadClass.EVAL: 2, WorkloadClass.INTERACTIVE: 1,
    WorkloadClass.UNKNOWN: 0,
}
CI_ENTRYPOINTS = frozenset({"claude-code-github-action"})
SDK_ENTRYPOINTS = frozenset({"sdk-py", "sdk-ts", "sdk-cli"})
INTERACTIVE_ENTRYPOINTS = frozenset({"cli"})
_AUTOMATED_KINDS = frozenset({LaneKind.MAIN, LaneKind.API_RUN, LaneKind.UNKNOWN})
_MIN_AUTOMATED_REQUESTS = 10
_MIN_PERIODIC_REQUESTS = 4
_MIN_PERIOD_MS = 300_000


def _majority(values: list[str]) -> str | None:
    if not values:
        return None
    counts = Counter(values)
    best = max(counts.values())
    return min(v for v, n in counts.items() if n == best)


def _periodic(starts: list[int]) -> bool:
    if len(starts) < _MIN_PERIODIC_REQUESTS:
        return False
    gaps = [b - a for a, b in zip(starts, starts[1:], strict=False)]
    n = len(gaps)
    total = sum(gaps)
    if total <= 0 or total < _MIN_PERIOD_MS * n:
        return False
    # CV² = Σ(n·g − total)² / (n · total²) < 1/16  ⇔  16 · Σ(n·g − total)² < n · total²
    spread = sum((n * g - total) ** 2 for g in gaps)
    return 16 * spread < total * total * n


def signals(lane: Lane, *, rules: object | None = None) -> list[tuple[WorkloadClass, Decimal, str]]:
    """Every rule that fires on *lane*: ``(class, confidence, reason)`` (see the module table)."""
    if not isinstance(lane, Lane):
        raise ContractViolation("classify expects a Lane")
    out: list[tuple[WorkloadClass, Decimal, str]] = []
    reqs = lane.requests
    tags = [r.attribution.workload_class.value for r in reqs
            if r.attribution.workload_class is not WorkloadClass.UNKNOWN]
    tag = _majority(tags)
    if tag is not None:
        out.append((WorkloadClass(tag), Decimal("0.99"), "resource tag"))
    served = [r.serving_inference for r in reqs]
    tiers = [s.pricing.service_tier for s in served if s is not None]
    if tiers and 2 * sum(t == "batch" for t in tiers) > len(tiers):
        out.append((WorkloadClass.BATCH, Decimal("0.99"), "batch service tier"))
    entry = _majority([r.attribution.entrypoint for r in reqs if r.attribution.entrypoint])
    if entry in CI_ENTRYPOINTS:
        out.append((WorkloadClass.CI, Decimal("0.95"), "CI entrypoint"))
    elif entry in SDK_ENTRYPOINTS:
        out.append((WorkloadClass.SERVICE, Decimal("0.6"), "SDK entrypoint"))
    if rules is not None and reqs:
        from tokenbill.finops.allocation import apply_rules

        first = reqs[0]
        attr = apply_rules(first, rules)  # type: ignore[arg-type]
        if attr.workload_class is not first.attribution.workload_class and (
                attr.workload_class is not WorkloadClass.UNKNOWN):
            out.append((attr.workload_class, Decimal("0.9"), "allocation rule"))
    prompts = sum(1 for e in lane.events if e.kind is LaneEventKind.HUMAN_PROMPT)
    if not prompts and _periodic([r.ts_start_ms for r in reqs]):
        out.append((WorkloadClass.SCHEDULED, Decimal("0.8"), "periodic cadence"))
    if (not prompts and lane.kind in _AUTOMATED_KINDS and len(reqs) >= _MIN_AUTOMATED_REQUESTS
            and entry not in INTERACTIVE_ENTRYPOINTS):
        out.append((WorkloadClass.SERVICE, Decimal("0.7"), "automated session"))
    if prompts or entry in INTERACTIVE_ENTRYPOINTS:
        out.append((WorkloadClass.INTERACTIVE, Decimal("0.5"), "human prompts"))
    return out


def classify(lane: Lane, *, rules: object | None = None) -> tuple[WorkloadClass, str]:
    """``(WorkloadClass, confidence decimal string)`` of *lane*: the most confident signal, ties
    to the more automated class; ``(UNKNOWN, "0")`` when nothing fires. *rules* (an optional
    ``finops.allocation.RuleSet``) adds the rule-set signal (0.9)."""
    found = signals(lane, rules=rules)
    if not found:
        return WorkloadClass.UNKNOWN, "0"
    cls, conf, _why = max(found, key=lambda s: (s[1], AUTOMATION_RANK[s[0]]))
    return cls, str(conf)
