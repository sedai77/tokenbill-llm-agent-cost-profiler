"""Area-local helpers: generated worlds (cached per variant set) and analysis contexts built the
way CP-STORE's enricher fills them (``core.pool`` cells, pool months and plan evidence)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import functools
from collections.abc import Iterable, Sequence

from tokenbill.core import pool as cpool
from tokenbill.core import testing as kit
from tokenbill.core.types import AnalysisContext, Finding
from tokenbill.synth.copilot_world import CopilotWorld, generate

DAY_MS = 86_400_000
CREDIT = 10_000_000
ALL_CAPS = frozenset({"ext:copilot", "aggregates", "copilot_billing", "activity", "licenses",
                      "config", "cost", "events", "usage_sequence"})


def ms(date: str) -> int:
    """Epoch milliseconds of a UTC date."""
    return (dt.date.fromisoformat(date) - dt.date(1970, 1, 1)).days * DAY_MS


@functools.lru_cache(maxsize=None)
def world(*variants: str) -> CopilotWorld:
    """The seed-7 world of *variants* (generated once per test session)."""
    return generate(seed=7, variants=variants)


def pools(w: CopilotWorld) -> tuple[list, list]:
    """(pool months, plan evidence) of the world's canonical records via ``core.pool``."""
    r = w.records
    cells, _ = cpool.build_cells(r.aggregates, r.cost_lines,
                                 capped=cpool.capped_cost_centers(r.config))
    pms = cpool.pool_months(cells, r.cost_lines, r.licenses, r.config, today=w.today)
    plans = [pe for m in sorted({pm.month for pm in pms})
             for pe in cpool.detect_plans(r.cost_lines, r.licenses, r.config, month=m)]
    return pms, plans


def ctx(w: CopilotWorld, *, reconciled: Iterable[str] = ("github_copilot",),
        min_usd: str = "1.00") -> AnalysisContext:
    """The analysis context of *w* (every capability, pools and plans from ``core.pool``).

    ``min_usd`` defaults to the product default ($1.00); the lane detector's small-dollar lever
    findings (e.g. ``compaction-cost``, one summary call each) are validated at ``"0.10"``, as
    CP-DET-LANES' own suite does."""
    r = w.records
    pms, plans = pools(w)
    return AnalysisContext(
        pricer=kit.FakePricer(), rules=None, replayer=None, calibration=None,
        window=(ms("2026-07-01"), ms(w.today)), capabilities=ALL_CAPS, now_ms=ms(w.today),
        aggregates=r.aggregates, cost_lines=r.cost_lines, licenses=r.licenses,
        activity=r.activity, config=r.config, outcomes=r.outcomes, pools=tuple(pms),
        plans=tuple(plans), reconciled_channels=frozenset(reconciled),
        thresholds={"min_usd": min_usd})


def dims(f: Finding) -> dict[str, str]:
    return dict(f.scope.dims)


def of(findings: Sequence[Finding], kind: str, **match: str | None) -> list[Finding]:
    """Findings of *kind* whose scope dims match every ``name=value`` (None = absent)."""
    return [f for f in findings if f.kind == kind
            and all(dims(f).get(k) == v for k, v in match.items())]


def evidence(f: Finding, ref: str) -> dict:
    return dict(next(e for e in f.evidence if e.ref == ref).attrs)


def seat_evidence_only(f: Finding) -> Finding:
    """*f* without its nano-valued evidence attrs (CP-PLAN's ``seat_counts`` rejects any int attr
    above 10**9 — seam note in README.md); counts and strings are kept."""
    items = tuple(dataclasses.replace(e, attrs=tuple(
        (k, v) for k, v in e.attrs if not (type(v) is int and v > 10**9))) for e in f.evidence)
    return dataclasses.replace(f, evidence=items)
