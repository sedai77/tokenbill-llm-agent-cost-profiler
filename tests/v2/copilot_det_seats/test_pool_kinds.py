"""Pool kinds (addendum §10.1 ``pool-regime``, ``overage-forecast``, ``promo-cliff``; labels R16 /
R-E20): Appendix C.P1 (closed month), C.P9 (open month forecast), C.P6 (promo cliff) and C.P7
(unknown cap policy) through the real ``core.pool``."""

from __future__ import annotations

import datetime as dt

import pytest

from tokenbill.core import builders as b
from tokenbill.core.labels import Basis, Evidence, Finality

from .helpers import (
    METERED,
    USD,
    C,
    by_scenario,
    cost_center,
    ctx,
    detect,
    dims,
    enrich,
    evidence,
    flags,
    of_kind,
    p_plan,
    p_pool,
    people,
    rows,
)


def _p1(*, reconciled: bool) -> list:
    """C.P1: 1,000 Business + 200 Enterprise seat lines, pooled use 3,100,000 (overage 420,000 =
    $4,200), direct-org review 15,000 credits ($150), October 2026 closed."""
    seats = [b.make_seat_line("business", "1000", date_utc="2026-10-01"),
             b.make_seat_line("enterprise", "200", date_utc="2026-10-01")]
    pooled, pooled_aggs = rows(2_680_000, date="2026-10-05", users=people(10),
                               discount=2_680_000)
    over, over_aggs = rows(420_000, date="2026-10-20", users=people(10))
    review, review_aggs = rows(15_000, date="2026-10-05", unattributed=True,
                               model="Copilot Code Review")
    cost = seats + pooled + over + review
    pms, plans = enrich(cost, pooled_aggs + over_aggs + review_aggs, today="2026-11-10")
    return detect(ctx(pools=pms, plans=plans, cost_lines=cost, today="2026-11-10",
                      reconciled=("github_copilot",) if reconciled else ()))


def test_pool_regime_closed_reconciled_month_is_invoice() -> None:
    [f] = of_kind(_p1(reconciled=True), "pool-regime")
    fig = f.cost_observed
    assert (fig.nano, fig.evidence, fig.basis, fig.finality) == (
        4_200 * USD, Evidence.EXACT, Basis.INVOICE, Finality.FINAL)
    assert dims(f) == {"entity": "enterprise", "product": "copilot"}
    assert (f.category, f.confidence, f.recoverable, f.headroom) == ("aggregate", "high", None,
                                                                    None)
    assert evidence(f, "direct") == {"nano": 150 * USD, "draws_pool": "no",
                                     "label": "exact invoice"}
    assert evidence(f, "consumed")["credits"] == "3100000"
    assert evidence(f, "regime")["regime"] == "overage"
    assert "credit savings cut the invoice" in f.summary
    assert f.fix is not None and f.fix.target == "github-copilot"


def test_pool_regime_same_month_unreconciled_is_exact_list() -> None:
    [f] = of_kind(_p1(reconciled=False), "pool-regime")
    fig = f.cost_observed
    assert (fig.nano, fig.evidence, fig.basis, fig.finality) == (
        4_200 * USD, Evidence.EXACT, Basis.LIST, Finality.FINAL)
    assert "unreconciled" in fig.note
    assert evidence(f, "direct")["label"] == "exact list"


def _p9_ctx(**kw: object):
    """C.P9: 2026-09 open (today 2026-09-23, lag 3): observed days 1–20 = 2,000,000 credits (all
    within the pool: net 0), seat lines 1,000 Business + 200 Enterprise."""
    series = {f"2026-09-{d:02d}": 130_000 for d in (1, 2, 3, 4)}
    last10 = [110_000, 110_000, 120_000, 120_000, 130_000, 130_000, 140_000, 140_000, 150_000,
              150_000]
    for d, v in zip((7, 8, 9, 10, 11, 14, 15, 16, 17, 18), last10, strict=True):
        series[f"2026-09-{d:02d}"] = v
    series.update({"2026-09-05": 30_000, "2026-09-06": 30_000, "2026-09-12": 20_000,
                   "2026-09-13": 30_000, "2026-09-19": 30_000, "2026-09-20": 40_000})
    cost, aggs = [], []
    users = people(10, "p9")
    for date, credits in sorted(series.items()):
        ls, ag = rows(credits, date=date, users=users, discount=credits)
        cost += ls
        aggs += ag
    cost += [b.make_seat_line("business", "1000"), b.make_seat_line("enterprise", "200")]
    pms, plans = enrich(cost, aggs, today="2026-09-23")
    return ctx(pools=pms, plans=plans, cost_lines=cost, today="2026-09-23", **kw)


def test_pool_regime_open_month_provisional_plus_forecast() -> None:
    found = detect(_p9_ctx(reconciled=("github_copilot",)))
    [f] = of_kind(found, "pool-regime")
    fig = f.cost_observed
    assert (fig.nano, fig.evidence, fig.basis, fig.finality) == (
        0, Evidence.EXACT, Basis.LIST, Finality.PROVISIONAL)
    assert "provisional" in fig.note
    fc = evidence(f, "forecast")
    assert (fc["nano"], fc["low_nano"], fc["high_nano"], fc["label"]) == (
        3_100_000 * C, 2_920_000 * C, 3_280_000 * C, "estimated list_equivalent")
    of = evidence(f, "overage_forecast")
    assert (of["nano"], of["low_nano"], of["high_nano"], of["label"]) == (
        4_200 * USD, 2_400 * USD, 6_000 * USD, "estimated list")
    assert "Forecast overage $4,200.00 ($2,400.00 to $6,000.00), estimated." in f.summary
    assert evidence(f, "regime")["finality"] == "open"
    [ov] = of_kind(found, "overage-forecast")
    cost = ov.cost_observed
    assert (cost.nano, cost.low_nano, cost.high_nano, cost.evidence, cost.basis) == (
        4_200 * USD, 2_400 * USD, 6_000 * USD, Evidence.ESTIMATED, Basis.LIST)
    assert ov.lever_ids == ("copilot.budget_plan",)
    assert (ov.category, ov.lever_class) == ("aggregate", "behavioral")
    assert "at unchanged use" in ov.summary


def test_overage_forecast_gated_on_its_p90() -> None:
    assert of_kind(detect(_p9_ctx(min_usd="6000")), "overage-forecast") != []
    assert of_kind(detect(_p9_ctx(min_usd="6000.01")), "overage-forecast") == []


def _p7(policy: str | None):
    """C.P7 in an open month: capped cost center A (50 Business seats, cap 95,000) drew 130,000
    credits by 2026-10-05; the rest of the enterprise 2,000,000."""
    conf = [cost_center("A", pool_enabled=True, cap="95000", date="2026-10-02"), METERED]
    if policy is not None:
        conf.append(flags({"capped_policy.A": policy}))
    seats = [b.make_seat_line("business", "950", date_utc="2026-10-01"),
             b.make_seat_line("enterprise", "200", date_utc="2026-10-01"),
             b.make_seat_line("business", "50", date_utc="2026-10-01", cost_center="A")]
    ent, ent_aggs = rows(2_000_000, date="2026-10-05", users=people(20), discount=2_000_000)
    cc, cc_aggs = rows(130_000, date="2026-10-05", users=people(5, "a"), discount=95_000,
                       cost_center="A")
    cost = seats + ent + cc
    pms, plans = enrich(cost, ent_aggs + cc_aggs, [], conf, today="2026-11-02")
    return detect(ctx(pools=pms, plans=plans, config=conf, cost_lines=cost, today="2026-11-02"))


def test_p7_unknown_cap_policy_widens_the_overage_forecast() -> None:
    unknown = {dims(f)["entity"]: f for f in of_kind(_p7(None), "overage-forecast")}
    cont = {dims(f)["entity"]: f for f in of_kind(_p7("continue"), "overage-forecast")}
    u, c = unknown["cc:A"].cost_observed, cont["cc:A"].cost_observed
    assert (u.low_nano, u.nano, u.high_nano) == (0, 350 * USD, 350 * USD)
    assert (c.low_nano, c.nano, c.high_nano) == (350 * USD, 350 * USD, 350 * USD)
    assert "cap policy unknown" in u.note
    assert evidence(unknown["cc:A"], "regime")["capped_policy"] == "unknown"
    assert "cap policy unknown" in unknown["cc:A"].summary
    assert of_kind(_p7("block"), "overage-forecast") == [] or all(
        dims(f)["entity"] != "cc:A" for f in of_kind(_p7("block"), "overage-forecast"))


def _promo_world(today: str):
    """C.P6: 100 Business seats; July and August use 250,000/month against the promo pool
    300,000 (slack); September 250,000 against the standard pool 190,000."""
    cost, aggs = [], []
    for month in ("07", "08", "09"):
        cost.append(b.make_seat_line("business", "100", date_utc=f"2026-{month}-01"))
        ls, ag = rows(250_000, date=f"2026-{month}-15", users=people(40, "promo"),
                      discount=250_000 if month != "09" else 190_000)
        cost += ls
        aggs += ag
    pms, plans = enrich(cost, aggs, today=today)
    return ctx(pools=pms, plans=plans, cost_lines=cost, today=today)


def test_p6_promo_cliff() -> None:
    [f] = of_kind(detect(_promo_world("2026-10-10")), "promo-cliff")
    fig = f.cost_observed
    assert (fig.nano, fig.evidence, fig.basis) == (600 * USD, Evidence.ESTIMATED, Basis.LIST)
    assert "at unchanged use" in fig.note and "expires 2026-11-30" in fig.note
    assert evidence(f, "promo:2026-08")["pool_nano"] == 300_000 * C
    assert evidence(f, "standard:2026-09")["pool_nano"] == 190_000 * C
    assert evidence(f, "expires") == {"date": "2026-11-30"}
    assert "$600.00/month at unchanged use" in f.title
    assert f.fix is not None and "promotional credits ended" in f.fix.text


@pytest.mark.parametrize(("today", "emitted"), [("2026-11-30", True), ("2026-12-01", False)])
def test_p6_promo_cliff_expires(today: str, emitted: bool) -> None:
    assert bool(of_kind(detect(_promo_world(today)), "promo-cliff")) is emitted


def test_promo_cliff_per_scenario_with_unknown_plan() -> None:
    promo = b.make_pool_month(month="2026-08", seats={"unknown": "100"},
                              plan_scenario="business", consumed_report_nano=250_000 * C,
                              pool_credits="300000", pool_nano=300_000 * C,
                              promo="promo:2026-06-01/2026-09-01", regime="slack")
    promo_e = b.make_pool_month(month="2026-08", seats={"unknown": "100"},
                                plan_scenario="enterprise", consumed_report_nano=250_000 * C,
                                pool_credits="700000", pool_nano=700_000 * C,
                                promo="promo:2026-06-01/2026-09-01", regime="slack")
    sept = [b.make_pool_month(month="2026-09", seats={"unknown": "100"}, plan_scenario=s,
                              consumed_report_nano=250_000 * C) for s in ("business",
                                                                         "enterprise")]
    found = of_kind(detect(ctx(pools=[promo, promo_e, *sept])), "promo-cliff")
    cliffs = by_scenario(found)
    assert set(cliffs) == {"business"}          # Enterprise: 390,000 still covers 250,000
    assert cliffs["business"].cost_observed.nano == 600 * USD
    assert "scenario business" in cliffs["business"].cost_observed.note
    assert cliffs["business"].title.startswith("If Business: ")


def test_pool_regime_lists_earlier_months_and_regime_texts() -> None:
    pms = [p_pool(2_000_000, month="2026-07"), p_pool(2_650_000, month="2026-08",
                                                       regime="straddling"),
           p_pool(0, month="2026-09", finality="open", regime="unknown")]
    [f] = of_kind(detect(ctx(pools=pms, plans=[p_plan()])), "pool-regime")
    assert evidence(f, "regime")["regime"] == "unknown"
    assert "Regime unknown" in f.summary
    assert evidence(f, "month:2026-08")["regime"] == "straddling"
    assert evidence(f, "month:2026-07")["regime"] == "slack"
    [g] = of_kind(detect(ctx(pools=[pms[1]])), "pool-regime")
    assert "Straddling the pool" in g.summary


def test_pool_regime_scenario_with_report_user_seats_is_an_upper_bound() -> None:
    pm = b.make_pool_month(month="2026-10", seats={"unknown": "60"}, plan_scenario="business",
                           seats_source="report_users", consumed_report_nano=150_000 * C)
    [f] = of_kind(detect(ctx(pools=[pm])), "pool-regime")
    assert f.cost_observed.upper_bound and f.cost_observed.nano == 360 * USD
    assert "seats lower bound" in f.cost_observed.note


def test_pool_months_of_several_entities() -> None:
    pms = [p_pool(10_000, seats_map={"business": "10"}, entity_id="org:a"),
           p_pool(30_000, seats_map={"business": "10"}, entity_id="org:b")]
    found = of_kind(detect(ctx(pools=pms)), "pool-regime")
    assert [dims(f)["entity"] for f in found] == ["org:a", "org:b"]
    assert [f.cost_observed.nano for f in found] == [0, 110 * USD]


def test_forecast_dates_follow_the_calendar() -> None:
    # guard for the P9 fixture: 2026-09-01 is a Tuesday (binding series)
    assert dt.date(2026, 9, 1).weekday() == 1
