"""Area-local builders for ``copilot.org-scan`` tests (synthetic records, ``core.builders``)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from tokenbill.core import builders as b
from tokenbill.core.records import (
    ActivityDay,
    ConfigSnapshot,
    CostLine,
    OutcomeAggregate,
    UsageAggregate,
    UsageBuckets,
)
from tokenbill.core.testing import FakePricer, _ts
from tokenbill.core.types import AnalysisContext, Finding, PoolMonth
from tokenbill.detect.copilot_org import CopilotOrgScan

CREDIT = 10_000_000                    # nano-USD per AI credit
USD = 1_000_000_000
ALL_CAPS = frozenset({"aggregates", "copilot_billing", "cost", "activity", "licenses", "config",
                      "outcomes", "ext:copilot"})
TODAY = "2026-09-24"
DET = CopilotOrgScan()
REPO = "h_" + "a" * 20
WORKFLOW = "h_" + "b" * 20


class World:
    """Records of one context, appended by the helpers below."""

    def __init__(self) -> None:
        self.lines: list[CostLine] = []
        self.aggs: list[UsageAggregate] = []
        self.activity: list[ActivityDay] = []
        self.config: list[ConfigSnapshot] = []
        self.outcomes: list[OutcomeAggregate] = []
        self.pools: list[PoolMonth] = []
        self.licenses: list[Any] = []

    def row(self, **kw: Any) -> World:
        line, agg = b.make_ai_usage_row(**kw)
        self.lines.append(line)
        self.aggs.append(agg)
        return self

    def rows(self, n: int, *, team: str, prefix: str | None = None, **kw: Any) -> World:
        """*n* report rows of distinct users of *team* (same model, tokens and credits)."""
        for i in range(n):
            self.row(principal=b.make_principal(f"{prefix or team}-{i}"), team=team, **kw)
        return self

    def actions(self, minutes: str, **kw: Any) -> World:
        self.lines.append(b.make_actions_line(minutes, **kw))
        return self

    def ide(self, team: str, users: int, **counts: int) -> World:
        """*users* activity days of *team* with ``ide:<name>`` interaction *counts* each."""
        for i in range(users):
            c = {"interactions": sum(counts.values()) or 1,
                 **{(k if ":" in k or k in ("mcp_distinct", "cli_requests", "cli_prompt_tokens",
                                             "app_requests") else f"ide:{k}"): v
                    for k, v in counts.items()}}
            self.activity.append(b.make_activity(b.make_principal(f"{team}-{i}"), team=team,
                                                 date_utc="2026-09-10", counts=c))
        return self

    def agg(self, source_kind: str, date: str, dims: dict[str, str], *,
            usage: UsageBuckets | None = None, reported: int | None = None) -> World:
        pairs = tuple(sorted(dims.items()))
        self.aggs.append(UsageAggregate(
            agg_id=f"{source_kind}:{len(self.aggs)}", source_kind=source_kind,
            bucket_start_ms=_ts(date), bucket_end_ms=_ts(date) + 86_400_000, dims=pairs,
            usage=usage or UsageBuckets(), reported_cost_nano=reported,
            reported_cost_basis="provider_estimate" if reported is not None else None))
        return self

    def ctx(self, *, caps: frozenset[str] = ALL_CAPS, today: str = TODAY,
            reconciled: Iterable[str] = (), min_usd: str = "1.00", **kw: Any) -> AnalysisContext:
        thresholds = {"min_usd": min_usd, **kw.pop("thresholds", {})}
        return AnalysisContext(
            pricer=kw.pop("pricer", FakePricer()), rules=None, replayer=None, calibration=None,
            window=(0, _ts("2026-10-01")), capabilities=caps, thresholds=thresholds,
            now_ms=_ts(today) if today else 0, aggregates=tuple(self.aggs),
            cost_lines=tuple(self.lines), activity=tuple(self.activity),
            config=tuple(self.config), outcomes=tuple(self.outcomes), pools=tuple(self.pools),
            licenses=tuple(self.licenses), reconciled_channels=frozenset(reconciled), **kw)


def pool_month(consumed_credits: int, *, seats: dict[str, str] | None = None, **kw: Any
               ) -> PoolMonth:
    """A closed metered pool month with *consumed_credits* pooled consumption (C.P1 seats by
    default: pool 2,680,000 credits)."""
    return b.make_pool_month(seats=seats or {"business": "1000", "enterprise": "200"},
                             consumed_report_nano=consumed_credits * CREDIT, **kw)


def p13_pools(**kw: Any) -> list[PoolMonth]:
    """C.P13: 100 seats, plan unknown, pooled use 250,000 credits → two scenario pool months."""
    return [b.make_pool_month(seats={"unknown": "100"}, plan_scenario=s,
                              consumed_report_nano=250_000 * CREDIT, **kw)
            for s in ("business", "enterprise")]


def run(ctx: AnalysisContext) -> list[Finding]:
    return DET.detect([], ctx)


def only(findings: Sequence[Finding], kind: str, **dims: str) -> list[Finding]:
    """Findings of *kind* whose scope contains every *dims* pair."""
    return [f for f in findings if f.kind == kind
            and all(dict(f.scope.dims).get(k) == v for k, v in dims.items())]


def one(findings: Sequence[Finding], kind: str, **dims: str) -> Finding:
    got = only(findings, kind, **dims)
    assert len(got) == 1, [(f.kind, f.scope.dims) for f in findings]
    return got[0]


def attrs(f: Finding, ref: str) -> dict[str, Any]:
    for item in f.evidence:
        if item.ref == ref:
            return dict(item.attrs)
    raise AssertionError(f"no evidence {ref!r} in {[i.ref for i in f.evidence]}")


def tri(fig: Any) -> tuple[int | None, int | None, int | None]:
    return (fig.low_nano, fig.nano, fig.high_nano)
