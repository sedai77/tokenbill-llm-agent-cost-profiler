"""Area-local builders of synthetic Copilot billing worlds for the CP-RECON tests.

Every record is synthetic and built from ``core.builders`` in the shapes the CP-BILL adapters
emit (addendum §4, §5.1–§5.3): AI usage report rows (a ``CostLine`` plus its token
``UsageAggregate``), per (file, day) coverage aggregates, seat / Actions / sandbox lines and REST
usage-summary lines. Report gross amounts are computed from the tokens with ``FakePricer``'s point
rates and rounded to 6-decimal credits, as GitHub's ``quantity`` column is.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable, Sequence
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from tokenbill.copilot.recon import reconcile_copilot
from tokenbill.core import testing as kit
from tokenbill.core.builders import (
    make_actions_line,
    make_ai_usage_row,
    make_attempt,
    make_inference,
    make_request,
    make_seat_line,
)
from tokenbill.core.ids import key_id, natural_id, pseudonym, stable_id
from tokenbill.core.models import normalize_copilot_model
from tokenbill.core.records import (
    CostLine,
    LaneEvent,
    LaneEventKind,
    PricingContext,
    Request,
    UsageAggregate,
    UsageBuckets,
)
from tokenbill.core.types import IngestResult, ReconciliationReport, SourceInfo

ORG = bytes(range(11, 43))
DAY_MS = 86_400_000
CREDIT = 10_000_000          # nano-USD per AI credit
PRICER = kit.FakePricer()


def p(name: str) -> str:
    """A ``p_`` principal under the store's org key."""
    return pseudonym(ORG, "p", name)


def day_ms(date: str) -> int:
    return (_dt.date.fromisoformat(date) - _dt.date(1970, 1, 1)).days * DAY_MS


def days(month: str, n: int | None = None) -> list[str]:
    first = _dt.date.fromisoformat(f"{month}-01")
    out = []
    d = first
    while d.month == first.month and (n is None or len(out) < n):
        out.append(d.isoformat())
        d += _dt.timedelta(days=1)
    return out


def list_ctx(model_label: str, *, apply_auto: bool = True, compliance: str | None = None,
             **kw: Any) -> PricingContext:
    """The Copilot pricing context of a report model label."""
    cm = normalize_copilot_model(model_label)
    return PricingContext(provider="github", channel="github_copilot", model=cm.model,
                          model_raw=model_label, speed=cm.speed, billing_path="copilot_pool",
                          routing=cm.routing if apply_auto else "direct", compliance=compliance,
                          **kw)


def list_price(usage: UsageBuckets, model_label: str, date: str, *, apply_auto: bool = True,
               compliance: str | None = None) -> int:
    """FakePricer point price (nano) of *usage* for a report model label on *date* (base rates,
    unknown-TTL writes at the published rate)."""
    ctx = list_ctx(model_label, apply_auto=apply_auto, compliance=compliance)
    rates = PRICER.unit_rates(ctx, ts_ms=day_ms(date))
    assert rates is not None, (model_label, date)
    return sum(rates.bucket_nano(b, getattr(usage, b)) for b in (
        "uncached_input", "cache_read", "cache_write_unknown", "output") if getattr(usage, b))


def credits_of(nano: int) -> str:
    """Nano-USD → a 6-decimal credit string (half-even), GitHub's ``quantity`` precision."""
    q = (Decimal(nano) / Decimal(CREDIT)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)
    return format(q, "f")


def row(date: str, model: str = "Claude Sonnet 5", *, uncached: int = 50_000,
        read: int = 200_000, write: int = 5_000, output: int = 10_000,
        convention: str = "excl", discount: str | None = None, discount_all: bool = False,
        scale: Decimal | None = None, credits: str | None = None, apply_auto: bool = True,
        compliance: str | None = None, principal: str | None = None, unattributed: bool = False,
        team: str | None = "t1", cost_center: str | None = None, org: str | None = "org-a",
        sku: str = "copilot_ai_credit", finality: str = "final", fetched_ms: int = 0
        ) -> tuple[CostLine, UsageAggregate]:
    """One AI usage report row with gross = the tokens' list price (× *scale*); the report's
    ``input`` column is ``uncached`` under ``excl`` and ``uncached + read + write`` under ``incl``;
    *discount* credits (or the whole gross with *discount_all*)."""
    usage = UsageBuckets(uncached_input=uncached, cache_read=read, cache_write_unknown=write,
                         output=output)
    if credits is None:
        price = list_price(usage, model, date, apply_auto=apply_auto, compliance=compliance) \
            if normalize_copilot_model(model).model else 0
        if scale is not None:
            price = int(Decimal(price) * scale)
        credits = credits_of(price)
    input_col = uncached if convention == "excl" else uncached + read + write
    return make_ai_usage_row(
        date_utc=date, model=model, credits=credits,
        discount_credits=credits if discount_all else (discount or "0"),
        principal=principal if principal is not None else p(team or "anon"),
        unattributed=unattributed, organization=org, cost_center=cost_center, team=team, sku=sku,
        input_tokens=input_col, output_tokens=output, cache_read_tokens=read,
        cache_write_tokens=write, finality=finality, fetched_ms=fetched_ms)


def coverage(source: str, date: str, lines: Iterable[CostLine], *, fetched_ms: int = 0
             ) -> UsageAggregate:
    """The ``github.ai_usage_report.coverage`` aggregate of one file-day (Σ net, Σ gross)."""
    ls = [c for c in lines if c.date_utc == date]
    start = day_ms(date)
    return UsageAggregate(
        agg_id=natural_id("ag", "github.ai_usage_report.coverage", source, date),
        source_kind="github.ai_usage_report.coverage", bucket_start_ms=start,
        bucket_end_ms=start + DAY_MS, dims=(("channel", "github_copilot"), ("source", source)),
        usage=UsageBuckets(), reported_cost_nano=sum(c.amount_nano for c in ls),
        reported_cost_basis="invoice", list_cost_nano=sum(c.list_amount_nano or 0 for c in ls),
        finality="final", fetched_ms=fetched_ms)


def summary(month: str, sku: str, net_nano: int, *, gross_nano: int | None = None,
            channel: str = "github_copilot", org: str | None = None) -> CostLine:
    """A REST ``usage/summary`` line (cost type ``rest.summary``) dated on the 1st of *month*."""
    return CostLine(
        line_id=natural_id("cl", "github.billing_api", f"{month}-01", sku, org, "summary"),
        source_kind="github.billing_api", date_utc=f"{month}-01", channel=channel,
        workspace_id=org, description=f"{sku} summary", model=None, cost_type="rest.summary",
        token_type=None, sku=sku, service_tier=None, inference_geo=None, endpoint_scope=None,
        amount_nano=net_nano, list_amount_nano=gross_nano if gross_nano is not None else net_nano,
        finality="final")


def sandbox(date: str, net_nano: int, *, sku: str = "sandbox_linux", org: str = "org-a"
            ) -> CostLine:
    """A detailed-usage sandbox line (channel ``github_sandbox``)."""
    return CostLine(
        line_id=natural_id("cl", "github.metered_usage", date, sku, org, "sandbox"),
        source_kind="github.metered_usage", date_utc=date, channel="github_sandbox",
        workspace_id=org, description=f"{sku} usage", model=None, cost_type="sandbox",
        token_type=None, sku=sku, service_tier=None, inference_geo=None, endpoint_scope=None,
        amount_nano=net_nano, list_amount_nano=net_nano, finality="final", quantity="1",
        unit="hours")


def seats(plan: str, n: str, month: str = "2026-09", **kw: Any) -> CostLine:
    return make_seat_line(plan, n, date_utc=f"{month}-01", **kw)


def actions(date: str, minutes: str = "1000", **kw: Any) -> CostLine:
    return make_actions_line(minutes, date_utc=date, **kw)


def request(lane: str, seq: int, date: str, usage: dict[str, int],
            model: str = "claude-sonnet-5", *, provider_nano: int | None = None,
            team: str | None = "t1", session: str = "s_conv", billable: bool | None = True,
            billing_rule_id: str | None = None, **ctx_kw: Any) -> Request:
    """A Copilot client request (one serving inference on ``github_copilot``, billing path
    ``copilot_pool``) with an optional provider-reported cost (nano-AIU → nano, R12)."""
    ts = day_ms(date) + 3_600_000 + 1000 * seq
    ctx_kw.setdefault("billing_path", "copilot_pool")
    inf = make_inference(usage, model=model, inference_id=stable_id("inf", lane, seq),
                         provider="github", channel="github_copilot", billable=billable,
                         billing_rule_id=billing_rule_id, provider_reported_cost_nano=provider_nano,
                         provider_reported_cost_basis=None if provider_nano is None
                         else "provider_estimate", **ctx_kw)
    att = make_attempt([inf], ts_ms=ts, attempt_id=stable_id("at", lane, seq))
    return make_request(lane, seq, ts, attempts=[att], session_key=session,
                        request_id=stable_id("rq", lane, seq),
                        attribution={"team": team, "billing_path": ctx_kw["billing_path"]})


def cost_state(lane: str, date: str, total_nano: int,
               reporter: str = "copilot.otel.invoke_agent") -> LaneEvent:
    return LaneEvent(lane_key=lane, ts_ms=day_ms(date) + 7_200_000, kind=LaneEventKind.COST_STATE,
                     attrs=(("reported_total_nano", total_nano), ("reporter", reporter)))


def ingest(store: kit.MemoryStore, *, lines: Sequence[CostLine] = (),
           aggs: Sequence[UsageAggregate] = (), requests: Sequence[Any] = (),
           events: Sequence[LaneEvent] = (), adapter: str = "github-ai-usage",
           source_id: str = "src", stats: dict[str, int] | None = None) -> None:
    src = SourceInfo(source_id=source_id, adapter=adapter, name_hmac="h_" + "1" * 20,
                     sha256=source_id, bytes=1, name_key_id=None, principal_key_id=key_id(ORG))
    store.ingest(IngestResult(source=src, requests=list(requests), sessions=[],
                              events=list(events), aggregates=list(aggs), cost_lines=list(lines),
                              outcomes=[], quarantined=[], notes=[], stats=dict(stats or {}),
                              capabilities=frozenset({"cost", "aggregates", "copilot_billing"})))


def put_records(rs: kit.MemoryRecordStore, *, licenses: Sequence[Any] = (),
                activity: Sequence[Any] = (), config: Sequence[Any] = ()) -> None:
    src = SourceInfo(source_id="rec", adapter="github-copilot-seats", name_hmac="h_" + "2" * 20,
                     sha256="rec", bytes=1, name_key_id=None, principal_key_id=key_id(ORG))
    result = IngestResult(source=src, requests=[], sessions=[], events=[], aggregates=[],
                          cost_lines=[], outcomes=[], quarantined=[], notes=[], stats={},
                          capabilities=frozenset({"licenses", "activity", "config"}))
    result.licenses = list(licenses)
    result.activity = list(activity)
    result.config = list(config)
    rs.put(result, principal_key_id=key_id(ORG))


def world() -> tuple[kit.MemoryStore, kit.MemoryRecordStore]:
    store = kit.MemoryStore(org_key=ORG, pricer=PRICER)
    return store, kit.MemoryRecordStore(store)


def reconcile(store: kit.MemoryStore, rs: kit.MemoryRecordStore | None = None, *,
              month: str = "2026-09", months: int = 1, today: str = "2026-10-20",
              **kw: Any) -> ReconciliationReport:
    """Reconcile *months* calendar months from *month* with FakePricer."""
    first = _dt.date.fromisoformat(f"{month}-01")
    y, m = first.year, first.month + months
    y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    until = day_ms(_dt.date(y, m, 1).isoformat())
    kw.setdefault("tolerance_pct", "0.5")
    kw.setdefault("unexplained_pct", "1.0")
    kw.setdefault("closed_only", False)
    return reconcile_copilot(store, [rs] if rs is not None else [], kw.pop("pricer", PRICER),
                             since_ms=day_ms(first.isoformat()), until_ms=until, today=today,
                             **kw)


def rows_of(report: ReconciliationReport, **match: str) -> list[Any]:
    """Report rows whose key contains every ``name=value`` of *match*."""
    out = []
    for r in report.rows:
        key = dict(r.key)
        if all(key.get(k) == v for k, v in match.items()):
            out.append(r)
    return out


def verdicts(report: ReconciliationReport) -> dict[str, str]:
    return {v.channel: v.verdict for v in report.channels}


def residuals(report: ReconciliationReport) -> dict[str, int]:
    return dict(report.residuals)


def decisions(report: ReconciliationReport) -> dict[str, str]:
    return dict(report.decisions)


# ---------------------------------------------------------------------------------------------
# Appendix C.P1: enterprise E, metered, 1,000 Business + 200 Enterprise seats (pool 2,680,000
# credits), pooled use ≈ 3,100,000 credits → overage ≈ 420,000 credits; direct-org code review
# 15,000 credits with discount 0 while the pool still had credits (direct_draws_pool "no")
# ---------------------------------------------------------------------------------------------

P1_TEAMS = ("t1", "t2", "t3", "t4")
P1_POOL = 2_680_000 * CREDIT
P1_ROW = {"uncached": 50_000_000, "read": 200_000_000, "write": 5_000_000, "output": 10_583_333}


def p1_lines(*, scale: Decimal | None = None, month: str = "2026-09"
             ) -> tuple[list[CostLine], list[UsageAggregate]]:
    """The C.P1 report rows in pool-drawdown order (row by row, day by day)."""
    lines: list[CostLine] = []
    aggs: list[UsageAggregate] = []
    left = P1_POOL
    for date in days(month):
        for team in P1_TEAMS:
            probe, _ = row(date, team=team, scale=scale, **P1_ROW)
            gross = probe.list_amount_nano or 0
            take = min(gross, left)
            left -= take
            line, agg = row(date, team=team, scale=scale, discount=credits_of(take), **P1_ROW)
            lines.append(line)
            aggs.append(agg)
        review, agg = row(date, "Code Review", credits="500", unattributed=True, team=None,
                          uncached=1000, read=0, write=0, output=100)
        lines.append(review)
        aggs.append(agg)
    return lines, aggs


def p1_world(*, scale: Decimal | None = None, summary_delta: int = 0, month: str = "2026-09",
             extra_lines: Sequence[CostLine] = ()
             ) -> tuple[kit.MemoryStore, kit.MemoryRecordStore]:
    """The C.P1 month with seat lines, Actions and sandbox lines, coverage aggregates and the
    matching REST usage summary (``summary_delta`` nano added to the AI-credit summary)."""
    store, rs = world()
    lines, aggs = p1_lines(scale=scale, month=month)
    cov = [coverage("s_p1", d, lines, fetched_ms=1) for d in days(month)]
    seat = [seats("business", "1000", month), seats("enterprise", "200", month)]
    act = [actions(d) for d in days(month)]
    box = [sandbox(f"{month}-15", 50 * 10**9)]
    ai_net = sum(c.amount_nano for c in lines)
    ai_gross = sum(c.list_amount_nano or 0 for c in lines)
    summ = [summary(month, "copilot_ai_credit", ai_net + summary_delta, gross_nano=ai_gross),
            summary(month, "copilot_for_business", 19_000 * 10**9),
            summary(month, "copilot_enterprise", 7_800 * 10**9),
            summary(month, "actions_linux", 500 * 10**9, channel="github_actions"),
            summary(month, "sandbox_linux", 50 * 10**9, channel="github_sandbox")]
    ingest(store, lines=lines, aggs=[*aggs, *cov], source_id="s_p1")
    ingest(store, lines=[*seat, *act, *box, *extra_lines], adapter="github-metered-usage",
           source_id="metered")
    ingest(store, lines=summ, adapter="github-billing-api", source_id="rest")
    return store, rs
