"""Closed-form truths: the Appendix C worked examples through ``copilot_truth``, the pricing guard
from ``core.facts``, per-plant arithmetic, and the single comparison with ``core.pool`` (pool
months and plan evidence of every variant, brief acceptance)."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from decimal import Decimal
from fractions import Fraction

import pytest

from tokenbill.core import pool as cpool
from tokenbill.core import testing as kit
from tokenbill.core.builders import make_copilot_ctx
from tokenbill.core.records import UsageBuckets
from tokenbill.synth import copilot_truth as T

from .worlds import CREDIT, ms, pools, world

VARIANTS = ((), ("slack",), ("volume",), ("plan_unknown",), ("plan_quota",), ("plan_conflict",))


@pytest.mark.parametrize("variants", VARIANTS, ids=lambda v: "-".join(v) or "default")
def test_pool_months_equal_core_pool(variants: tuple[str, ...]) -> None:
    w = world(*variants)
    pms, plans = pools(w)
    got = {(p.entity_id, p.month, p.plan_scenario): p for p in pms}
    want = {(p.entity_id, p.month, p.plan_scenario): p for p in w.truth.pool_months}
    assert set(got) == set(want)
    for key, t in want.items():
        p = got[key]
        fc = p.forecast
        of = p.overage_forecast
        assert (p.seats, p.pool_credits, p.pool_nano, p.promo is not None,
                p.consumed_report_nano, p.overage_observed_nano, p.direct_net_nano, p.finality,
                p.regime) == (t.seats, t.pool_credits, t.pool_nano, t.promo,
                              t.consumed_report_nano, t.overage_observed_nano,
                              t.direct_net_nano, t.finality, t.regime), key
        assert ((fc.nano, fc.low_nano, fc.high_nano) if fc else None) == t.forecast, key
        assert ((of.nano, of.low_nano, of.high_nano) if of else None) == \
            t.overage_forecast, key
    assert {(pe.entity_id, pe.month, pe.plan, pe.source, pe.conflict) for pe in plans} == \
        set(w.truth.plans)


def test_plan_unknown_truth_has_two_scenarios_per_entity_month() -> None:
    t = world("plan_unknown").truth
    per: dict[tuple[str, str], set] = defaultdict(set)
    for p in t.pool_months:
        per[(p.entity_id, p.month)].add(p.plan_scenario)
    assert per and all(v == {"business", "enterprise"} for v in per.values())
    assert {plan for _, _, plan, _, _ in t.plans} == {"unknown"}
    assert t.plan_view == "unknown"


def test_default_regimes_promo_slack_then_standard_overage() -> None:
    t = world().truth
    ent = {p.month: p for p in t.pool_months if p.entity_id == "enterprise"}
    assert [ent[m].regime for m in ("2026-07", "2026-08", "2026-09")] == [
        "slack", "slack", "overage"]
    assert ent["2026-07"].promo and not ent["2026-09"].promo
    assert ent["2026-09"].pool_credits == "288900"            # 111 x 1,900 + 20 x 3,900
    assert ent["2026-07"].pool_credits == "473000"            # 111 x 3,000 + 20 x 7,000
    assert ent["2026-09"].overage_observed_nano > 0
    slack = {p.month: p for p in world("slack").truth.pool_months if p.entity_id == "enterprise"}
    assert slack["2026-09"].regime == "slack" and slack["2026-09"].overage_observed_nano == 0
    cc = [p for p in t.pool_months if p.entity_id == "cc:cc-data"]
    assert all(p.pool_credits == "28500" and p.regime == "overage" for p in cc)
    assert t.cap_overage_range == {p.month: (0, p.overage_observed_nano) for p in cc}
    aug, sep = ent["2026-08"], ent["2026-09"]
    assert t.promo_cliff_nano["enterprise"] == max(0, aug.consumed_report_nano - sep.pool_nano)


# ---------------------------------------------------------------------------------------------
# Appendix C worked examples through the closed forms
# ---------------------------------------------------------------------------------------------


def _pm(pool_credits: int, consumed_credits: int, *, month: str = "2026-09",
        scenario: str | None = None,
        forecast: tuple[int, int, int] | None = None) -> T.TruthPoolMonth:
    pool = pool_credits * CREDIT
    consumed = consumed_credits * CREDIT
    return T.TruthPoolMonth(
        entity_id="enterprise", month=month, plan_scenario=scenario, seats=(),
        pool_credits=str(pool_credits), pool_nano=pool, promo=False,
        consumed_report_nano=consumed, overage_observed_nano=max(0, consumed - pool),
        direct_net_nano=0, finality="closed", regime=T.regime(consumed, consumed, pool),
        forecast=forecast, overage_forecast=None)


USD = 10**9


def test_appendix_c_seat_changes() -> None:
    p1 = 2_680_000
    assert T.seat_change_saving(_pm(p1, 3_100_000), {"business": -50})[0] == 0      # P2
    assert T.seat_change_saving(_pm(p1, 2_000_000), {"business": -50})[0] == 950 * USD  # P3
    assert T.seat_change_saving(_pm(p1, 2_650_000), {"business": -50})[0] == 300 * USD  # P4
    assert T.seat_change_saving(_pm(p1, 2_000_000), {"business": -10},
                                billing_mode="volume") is None                        # P10
    assert T.seat_change_saving(_pm(p1, 2_000_000), {"business": -10})[0] == 190 * USD  # P11
    assert T.seat_change_saving(_pm(p1, 2_000_000), {"business": -12})[0] == 228 * USD  # P12
    b = _pm(190_000, 250_000, scenario="business")                                   # P13b
    e = _pm(390_000, 250_000, scenario="enterprise")
    assert T.seat_change_saving(b, {"unknown": -10})[0] == 0
    assert T.seat_change_saving(e, {"unknown": -10})[0] == 390 * USD
    ranged = _pm(p1, 2_600_000, forecast=(2_700_000 * CREDIT, 2_600_000 * CREDIT,
                                          2_800_000 * CREDIT))
    point, low, high = T.seat_change_saving(ranged, {"business": -50})
    assert (point, low, high) == (0, 0, 800 * USD)


def test_appendix_c_p9_forecast() -> None:
    daily: dict[str, int] = {}
    business = [110, 120, 125, 128, 130, 132, 135, 140, 150, 160]
    for day, v in zip((7, 8, 9, 10, 11, 14, 15, 16, 17, 18), business, strict=True):
        daily[f"2026-09-{day:02d}"] = v * 1_000
    for day, v in zip((12, 13, 19, 20), (20, 30, 35, 40), strict=True):
        daily[f"2026-09-{day:02d}"] = v * 1_000
    for day in (1, 2, 3, 4):
        daily[f"2026-09-{day:02d}"] = 130_000
    for day in (5, 6):
        daily[f"2026-09-{day:02d}"] = 12_500
    daily["2026-09-22"] = 999_999                  # after the cutoff: not observed
    assert T.forecast(daily, "2026-09", "2026-09-23", 3) == (
        2_000_000, 3_100_000, 2_920_000, 3_280_000)
    assert T.forecast(daily, "2026-10", "2026-09-23", 3) is None
    only_weekdays = {"2026-09-01": 10}
    assert T.forecast(only_weekdays, "2026-09", "2026-09-01", 0) == (10, 300, 300, 300)


def test_nearest_rank_regime_allowance() -> None:
    assert T.nearest_rank([5, 1, 3], 1, 2) == 3
    assert T.nearest_rank(list(range(1, 11)), 9) == 9
    assert T.nearest_rank([7], 1) == 7
    assert T.regime(1, 2, 2) == "slack" and T.regime(3, 4, 2) == "overage"
    assert T.regime(1, 3, 2) == "straddling"
    assert T.allowance("business", "2026-08") == (Decimal(3000), True)
    assert T.allowance("enterprise", "2026-09") == (Decimal(3900), False)
    assert T.allowance("business", "2026-08", promo_eligible=False) == (Decimal(1900), False)
    assert T.seat_fee_nano("enterprise") == 39 * USD


def test_appendix_c_golden_prices() -> None:
    g1 = UsageBuckets(uncached_input=12_000, cache_read=180_000, cache_write_unknown=6_000,
                      output=3_000)
    assert T.price(g1, "claude-opus-5-5", "2026-09-23") == 174_000_000
    assert T.price(g1, "claude-opus-5-5", "2026-09-23", write="1h") == 192_000_000
    assert T.price(g1, "claude-opus-5-5", "2026-09-23", routing="auto") == 156_600_000     # G2
    g8 = UsageBuckets(uncached_input=10_000, cache_read=90_000, output=2_000)
    assert T.price(g8, "claude-opus-4-8", "2026-09-23", speed="fast") == 290_000_000
    assert T.price(g8, "claude-opus-4-8", "2026-09-23") == 145_000_000
    g5 = UsageBuckets(uncached_input=20_000, cache_read=280_000, output=4_000)
    assert T.price(g5, "gpt-5.5", "2026-09-10", band=True) == 660_000_000
    g15 = UsageBuckets(cache_write_5m=1_000)
    assert T.price(g15, "gpt-5.3-codex", "2026-09-23") == 1_750_000                     # G15
    assert T.price(g1, "claude-opus-5-5", "2026-09-21") is None                          # G16
    assert T.rates("no-such-model", "2026-09-23") is None
    assert T.band_threshold("gpt-5.5", "2026-09-10") == 272_000


def test_closed_form_prices_equal_fakepricer_on_every_used_unit() -> None:
    pricer = kit.FakePricer()
    units = {"claude": UsageBuckets(uncached_input=20_000, cache_read=200_000,
                                    cache_write_unknown=10_000, output=5_000),
             "gpt": UsageBuckets(uncached_input=20_000, cache_read=200_000, output=5_000)}
    seen = set()
    for c in world().records.cost_lines:
        if c.source_kind != "github.ai_usage_report" or not c.model:
            continue
        key = (c.model, c.date_utc, c.routing, c.speed)
        if key in seen:
            continue
        seen.add(key)
        unit = units["gpt" if c.model.startswith("gpt") else "claude"]
        ctx = make_copilot_ctx(c.model, routing=c.routing, speed=c.speed)
        fig = pricer.price_usage(unit, ctx, ts_ms=ms(c.date_utc)).figure
        assert T.price(unit, c.model, c.date_utc, routing=c.routing, speed=c.speed) == fig.nano
    assert len(seen) > 300


def test_report_rows_are_priced_at_base_rates() -> None:
    w = world()
    lines = _report(w)
    assert len(lines) == len(w.rows)
    lane_users = {u.principal for u in w.people if u.team in ("vscode", "agents")}
    for c, f in zip(lines, w.rows, strict=True):
        assert (c.list_amount_nano, c.amount_nano, c.principal, c.date_utc) == (
            f.gross, f.net, f.principal, f.date)
        if not c.model:
            assert f.pseudo == "code_review"
            continue
        base = T.price(f.usage, c.model, c.date_utc, routing=c.routing, speed=c.speed)
        if c.principal in lane_users and c.model == "gpt-5.5":
            assert c.list_amount_nano >= base            # band requests of the VS Code lanes
        else:
            assert c.list_amount_nano == base, (c.team, c.model, c.date_utc)


def _report(w):
    return [c for c in w.records.cost_lines if c.source_kind == "github.ai_usage_report"]


def _units(w, line) -> int:
    return next(f for c, f in zip(_report(w), w.rows, strict=True)
                if c.line_id == line.line_id).usage.output // 5_000


def test_pricing_guard_from_facts() -> None:
    w = world()
    changes = T.k_dated_changes()
    assert "2026-06-29" in changes["claude-opus-4-8"]          # the fast-mode listing (K)
    used: set[tuple[str, str]] = set()
    for c in _report(w):
        if c.model:
            used.add((c.model, c.date_utc))
    for req in w.records.requests:
        for inf in req.billable_inferences:
            used.add((inf.pricing.model, (dt.date(1970, 1, 1) + dt.timedelta(
                milliseconds=req.ts_start_ms)).isoformat()))
    assert {m for m, _ in used} == {"claude-sonnet-5", "claude-opus-4-8", "claude-opus-5-5",
                                    "gpt-5.4", "gpt-5.5"}
    for model, date in used:
        assert T.rate_row(model, date) is not None, (model, date)
        day = dt.date.fromisoformat(date)
        assert all(abs((day - dt.date.fromisoformat(k)).days) > 2
                   for k in changes.get(model, ())), (model, date)
    assert {d for m, d in used if m == "claude-opus-5-5"} == {"2026-09-22"}
    assert "2026-09-22" not in {d for m, d in used if m == "claude-opus-4-8"}


# ---------------------------------------------------------------------------------------------
# plant truths
# ---------------------------------------------------------------------------------------------


def test_plant_truths_default() -> None:
    w = world()
    t = w.truth
    assert t.idle_seats == {"platform": {"removable": 6}, "infra": {"team": 5},
                            "ops": {"auto": 5}}
    assert t.plan_mix_seats == {"platform": 4} and t.zero_user_budgets == {"platform": 5}
    assert t.idle_seat_saving_nano == 0 == t.seat_auto_assign_nano     # September overage
    assert t.seat_reclaim_nano == t.idle_seat_saving_nano
    assert t.idle_seat_saving_by_scenario[("infra", None)] is None
    fast_rows = [c for c in _report(w) if c.speed == "fast"]
    units = sum(_units(w, c) for c in fast_rows)
    assert t.fast_premium_nano == units * 387_500_000 == t.fast_mode_premium_nano
    assert t.auto_reach == {"platform": Fraction(7, 10), "infra": Fraction(7, 10),
                            "ops": Fraction(7, 10), "payments": Fraction(7, 10),
                            "mobile": Fraction(7, 10), "data": Fraction(7, 10),
                            "agents": Fraction(1), "vscode": Fraction(1),
                            "jetbrains": Fraction(1, 10), "core": Fraction(7, 10),
                            "tiny": Fraction(7, 10)} == t.reach_by_team
    assert "core" not in t.auto_saving_by_team                          # Auto only
    pay = [c for c in _report(w) if c.team == "payments"]
    assert t.auto_saving_by_team["payments"] * 100 == sum(c.list_amount_nano for c in pay) * 7
    opus = [c for c in pay if c.model in ("claude-opus-4-8", "claude-opus-5-5")]
    assert t.premium_share_by_team["payments"] == Fraction(
        sum(c.list_amount_nano for c in opus), sum(c.list_amount_nano for c in pay))
    per_unit = {"claude-opus-4-8": 232_500_000, "claude-opus-5-5": 115_000_000}
    assert t.premium_remap_saving_by_team["payments"] == sum(
        _units(w, c) * per_unit[c.model] for c in opus)
    assert t.editor_share["jetbrains"]["jetbrains"] == Fraction(9, 10)
    direct = [c for c in _report(w) if c.cost_type == "ai_credit.direct"]
    assert all(c.amount_nano == c.list_amount_nano for c in direct)
    assert t.direct_org_net_nano == sum(c.amount_nano for c in direct) == t.direct_net_nano
    net, low, high = t.larger_runner
    days = sum(1 for c in w.records.cost_lines if c.sku == "linux_16_core")
    assert net == high == days * 30 * 42_000_000 and low == net - days * 30 * 6_000_000
    runs = sorted(t.agentic_run_prices)
    assert t.agentic_run_p50_nano == runs[9] and t.agentic_run_p90_nano == runs[17]
    assert set(t.forced_migration_delta) == {("cc:cc-data", "gpt-5.4"),
                                             ("cc:cc-data", "gpt-5.5"),
                                             ("enterprise", "gpt-5.5")}
    assert all(v > 0 for v in t.forced_migration_delta.values())


@pytest.mark.parametrize(("variants", "platform", "ops"), [
    (("slack",), 6 * 39 * USD, 5 * 19 * USD),
    (("volume",), None, None),
])
def test_seat_truths_by_regime(variants: tuple[str, ...], platform: int | None,
                               ops: int | None) -> None:
    t = world(*variants).truth
    assert (t.idle_seat_saving_nano, t.seat_auto_assign_nano) == (platform, ops)


def test_seat_truths_plan_unknown_scenarios() -> None:
    t = world("plan_unknown").truth
    assert t.idle_seats == {"platform": {"unknown": 6}, "infra": {"unknown": 5},
                            "ops": {"unknown": 5}}
    assert t.idle_seat_saving_nano is None
    for team, n in (("platform", 6), ("infra", 5), ("ops", 5)):
        assert t.idle_seat_saving_by_scenario[(team, "business")] == 0
        assert t.idle_seat_saving_by_scenario[(team, "enterprise")] == n * 39 * USD
    quota = world("plan_quota").truth
    sep = {p.plan_scenario: p for p in quota.pool_months if p.month == "2026-09"}
    assert dict(sep["business"].seats) == {"business": "116", "enterprise": "14",
                                           "unknown": "16"}
    assert sep["enterprise"].pool_nano - sep["business"].pool_nano == 16 * 2_000 * CREDIT


def test_lane_truths() -> None:
    w = world()
    lanes = w.truth.lanes
    requests = w.records.requests
    assert lanes.requests_unique == len({r.request_id for r in requests})
    assert lanes.requests_duplicated == 3 * 6 + 6 * 6
    store = kit.MemoryStore(org_key=w.principal_key)
    from tokenbill.core.ids import key_id
    from tokenbill.core.types import IngestResult, SourceInfo
    for n, sess in enumerate(w.records.sessions):
        src = SourceInfo(source_id=f"s{n}", adapter=sess.lanes[0].requests[0].source.adapter,
                         name_hmac="h_" + "3" * 20, sha256=f"s{n}", bytes=1,
                         name_key_id=key_id(w.name_key), principal_key_id=key_id(w.principal_key))
        store.ingest(IngestResult(source=src, requests=[], sessions=[sess], events=[],
                                  aggregates=[], cost_lines=[], outcomes=[], quarantined=[],
                                  notes=[], stats={}, capabilities=frozenset({"usage_sequence"})))
    assert len(list(store.iter_requests())) == lanes.requests_unique      # counted once
    assert lanes.model_switches == 1 and lanes.ci_uncapped_sessions == 2
    assert lanes.static_tool_definition_tokens == 30_000
    assert lanes.band_requests == 8
    assert lanes.band_premium_exact_nano == sum(
        p for p, lo, hi in lanes.band_premium_ranges if lo == hi)
    assert any(lo == 0 < hi for _, lo, hi in lanes.band_premium_ranges)       # A/B ranges
    triggers = dict(lanes.compactions_by_trigger)
    assert lanes.compaction_spend_nano == sum(triggers.values())
    assert lanes.compaction_forced_nano == triggers["context_limit_retry"] + \
        triggers["memory_pressure"]
    assert lanes.static_overhead_nano > 0
