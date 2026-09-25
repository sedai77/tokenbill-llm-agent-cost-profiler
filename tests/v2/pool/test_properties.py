"""Property and fuzz tests (F-POOL brief; SPEC §21 #5): conversion identities and bounds,
permutation invariance, scenario pairs, and hostile configuration values (only ``TokenbillError``
subclasses may escape)."""

from __future__ import annotations

import ast
import dataclasses
from decimal import Decimal
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.common import TokenbillError
from tokenbill.core import builders as b
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Calibration, Figure, estimated
from tokenbill.core.pool import (
    build_cells,
    capped_cost_centers,
    capped_policies,
    detect_plans,
    forecast,
    invoice_delta,
    pool_credits,
    pool_months,
    realize_credit_saving,
    realize_seat_change,
    run_flags,
    seat_months,
)
from tokenbill.core.records import CONFIG_KEYS

from .worlds import C, ms

PROPS = settings(max_examples=60, deadline=None,
                 suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
FEES = {"business": Decimal("19"), "enterprise": Decimal("39")}
POOL_FIELDS = {"seats", "pool_credits", "pool_nano", "pool_draw_nano", "discount_other_nano",
               "discount_unclassified_nano", "overage_observed_nano", "overage_forecast", "regime",
               "notes", "plan_scenario", "promo"}
nano = st.integers(min_value=0, max_value=10**16)


# ---------- conversions ----------


@PROPS
@given(nano, nano, nano, nano)
def test_invoice_delta_is_monotone_and_bounded_by_the_saving(before: int, s1: int, s2: int,
                                                              pool: int) -> None:
    small, large = sorted((s1, s2))
    d_small = invoice_delta(before + large, before + large - small, pool)
    d_large = invoice_delta(before + large, before, pool)
    assert 0 <= d_small <= small and 0 <= d_large <= large and d_small <= d_large


pool_month = st.builds(
    lambda biz, ent, unknown, scenario, used, closed: b.make_pool_month(
        seats={k: str(v) for k, v in (("business", biz), ("enterprise", ent),
                                      ("unknown", unknown)) if v or k == "business"},
        plan_scenario=scenario if unknown else None, consumed_report_nano=used * C,
        finality="closed" if closed else "open"),
    st.integers(0, 2_000), st.integers(0, 500), st.integers(0, 300),
    st.sampled_from(["business", "enterprise"]), st.integers(0, 10_000_000), st.booleans())


@st.composite
def savings(draw: st.DrawFn) -> Figure:
    point = draw(st.integers(-10**13, 10**13))
    if draw(st.booleans()):
        low = point - draw(st.integers(0, 10**13))
        high = point + draw(st.integers(0, 10**13))
        return estimated(point, Basis.LIST_EQUIVALENT, low=low, high=high, note="replay")
    return estimated(point, Basis.LIST_EQUIVALENT, calibration=Calibration.CALIBRATED)


@PROPS
@given(pool_month, savings(), st.integers(0, 10**13))
def test_invoice_plus_headroom_equals_the_saving(pm, saving: Figure, over: int) -> None:
    if pm.finality == "open":
        pm = dataclasses.replace(pm, overage_forecast=estimated(over, Basis.LIST, low=0,
                                                                high=over, note="o"))
    invoice, headroom = realize_credit_saving(saving, pm)
    assert invoice.nano is not None and headroom.nano is not None
    assert invoice.nano + headroom.nano == saving.nano
    if saving.low_nano is not None:
        assert invoice.low_nano + headroom.low_nano == saving.low_nano  # type: ignore[operator]
        assert invoice.high_nano + headroom.high_nano == saving.high_nano  # type: ignore[operator]
    if saving.nano >= 0:  # type: ignore[operator]
        assert 0 <= invoice.nano <= saving.nano  # type: ignore[operator]
    assert (invoice.basis, headroom.basis) == (Basis.LIST, Basis.LIST_EQUIVALENT)
    if pm.plan_scenario is not None:
        assert f"scenario {pm.plan_scenario}" in invoice.note


@PROPS
@given(pool_month, st.integers(1, 200), st.booleans(), st.sampled_from(["2026-05", "2026-10"]))
def test_seat_removals_and_downgrades_never_lose_money_outside_the_promo(
        pm, n: int, downgrade: bool, month: str) -> None:
    pm = dataclasses.replace(pm, month=month, promo=None, billing_mode="metered")
    plan = pm.plan_scenario and "unknown" or "business"
    delta = {"enterprise": -n, "business": n} if downgrade else {plan: -n}
    fig = realize_seat_change(pm, delta, month_fee=FEES)
    assert fig is not None and fig.nano is not None and fig.nano >= 0
    assert fig.low_nano is None or fig.low_nano >= 0


@PROPS
@given(st.dictionaries(st.sampled_from(["business", "enterprise", "unknown"]),
                       st.integers(0, 10**6), min_size=1))
def test_pool_credits_with_plan_unknown_raises(seats: dict[str, int]) -> None:
    if "unknown" in seats:
        try:
            pool_credits(seats, "2026-10", promo_eligible=True)
        except UsageError:
            return
        raise AssertionError("pool_credits accepted plan unknown")
    total, _ = pool_credits(seats, "2026-10", promo_eligible=True)
    assert total == sum(n * (1900 if p == "business" else 3900) for p, n in seats.items())


@PROPS
@given(st.lists(st.tuples(st.integers(1, 30), st.integers(0, 10**12)), max_size=40),
       st.integers(0, 45), st.integers(0, 5))
def test_forecast_bounds(days: list[tuple[int, int]], today_offset: int, lag: int) -> None:
    import datetime as dt

    today = (dt.date(2026, 9, 1) + dt.timedelta(days=today_offset)).isoformat()
    series = [(f"2026-09-{d:02d}", v) for d, v in days]
    got = forecast(series, month="2026-09", today=today, lag_days=lag)
    if got is None:
        assert today_offset < lag
        return
    observed, point, low, high = got
    assert observed <= low <= point <= high
    if today_offset - lag >= 29:
        assert observed == point == low == high == sum(v for _, v in series)


# ---------- permutation invariance and scenario pairs ----------

LABELS = ["Claude Sonnet 5", "Auto: Claude Haiku 4.5", "Copilot Code Review", "GPT-5.5"]


@st.composite
def worlds(draw: st.DrawFn) -> tuple[list, list, list, list]:
    principals = [b.make_principal(i) for i in range(6)]
    lines: list = []
    aggs: list = []
    for _ in range(draw(st.integers(0, 12))):
        credits = draw(st.integers(0, 50_000))
        who = draw(st.sampled_from(principals + [None]))
        line, agg = b.make_ai_usage_row(
            date_utc=f"2026-10-{draw(st.integers(1, 28)):02d}", model=draw(st.sampled_from(LABELS)),
            credits=str(credits), discount_credits=str(draw(st.integers(0, credits))),
            principal=who, unattributed=who is None,
            organization=draw(st.sampled_from(["a", "b"])),
            cost_center=draw(st.sampled_from([None, "A"])),
            input_tokens=draw(st.integers(0, 10**6)), cache_read_tokens=draw(st.integers(0, 10**6)),
            finality=draw(st.sampled_from(["final", "provisional"])))
        lines.append(line)
        aggs.append(agg)
    for _ in range(draw(st.integers(0, 3))):
        lines.append(b.make_seat_line(draw(st.sampled_from(["business", "enterprise"])),
                                      str(draw(st.integers(1, 30))),
                                      organization=draw(st.sampled_from(["a", "b"])),
                                      date_utc="2026-10-01"))
    lics = [b.make_license(draw(st.sampled_from(principals)),
                           snapshot_date=f"2026-10-{draw(st.integers(1, 28)):02d}",
                           plan=draw(st.sampled_from(["business", "enterprise", "unknown"])),
                           org=draw(st.sampled_from(["a", "b", None])))
            for _ in range(draw(st.integers(0, 8)))]
    conf = []
    if draw(st.booleans()):
        conf.append(b.make_config("cost_center", {"pool_enabled": True,
                                                  "pool_target_credits": "5000"},
                                  entity_id="cc:A", source_kind="github.cost_centers"))
    if draw(st.booleans()):
        conf.append(b.make_config("plan_quota", {"month": "2026-10", "quota": "3900",
                                                 "n_users": draw(st.integers(1, 9))},
                                  entity_id="org:a", source_kind="github.ai_usage_report"))
    if draw(st.booleans()):
        conf.append(b.make_config("run_flags", {"plan.enterprise": draw(st.sampled_from(
            ["business", "enterprise", "mixed"]))}, entity_id="admin_answers",
            source_kind="tokenbill.admin_answers"))
    return lines, aggs, lics, conf


def _run(lines: list, aggs: list, lics: list, conf: list, mode: str):
    cells, clamped = build_cells(aggs, lines, capped=capped_cost_centers(conf), entity_mode=mode,
                                 convention="incl")
    plans = detect_plans(lines, lics, conf, month="2026-10", entity_mode=mode)
    months = pool_months(cells, lines, lics, conf, today="2026-10-20", gross_is_list=True,
                         entity_mode=mode)
    return cells, clamped, plans, months


@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(worlds(), st.randoms(use_true_random=False), st.sampled_from(["enterprise", "org"]))
def test_outputs_are_identical_under_input_permutation(world, rnd, mode: str) -> None:
    lines, aggs, lics, conf = world
    expected = _run(lines, aggs, lics, conf, mode)
    for items in (lines, aggs, lics, conf):
        rnd.shuffle(items)
    assert _run(lines, aggs, lics, conf, mode) == expected
    cells, _, plans, months = expected
    # cells keep every nano of the report rows they cover
    copilot = [x for x in lines if x.cost_type != "seat"]
    assert sum(c.net_nano for c in cells) == sum(x.amount_nano for x in copilot)
    assert sum(c.gross_nano for c in cells) == sum(x.list_amount_nano for x in copilot)
    # a known plan gives one pool month per entity × month; an unknown one a scenario pair
    by_key: dict[tuple[str, str], list] = {}
    for pm in months:
        by_key.setdefault((pm.entity_id, pm.month), []).append(pm)
    for group in by_key.values():
        scenarios = [pm.plan_scenario for pm in group]
        assert scenarios in ([None], ["business", "enterprise"])
        if len(group) == 2:
            first, second = (dataclasses.asdict(pm) for pm in group)
            assert {k for k in first if first[k] != second[k]} <= POOL_FIELDS
            assert group[0].pool_nano <= group[1].pool_nano
    assert all(p.month == "2026-10" for p in plans)


# ---------- fuzz: hostile configuration values ----------

values = st.one_of(st.none(), st.booleans(), st.integers(-10**20, 10**20),
                   st.text(max_size=12),
                   st.sampled_from(["1E+999999999", "NaN", "-0", "3900", "1900.5", "1e-30",
                                    "business", "enterprise", "mixed", "unknown", "block",
                                    "continue", "2026-10", "*", "  7 ", "Infinity"]))
SUFFIXES = st.sampled_from(["enterprise", "org:a", "cc:A", "enterprise.business",
                            "enterprise.enterprise", "org:a.unknown", "x", "cc:A.pro", "A"])


@st.composite
def configs(draw: st.DrawFn) -> list:
    out = []
    for _ in range(draw(st.integers(0, 8))):
        kind = draw(st.sampled_from(["run_flags", "cost_center", "org_settings", "plan_quota",
                                     "seat_counts"]))
        keys = draw(st.lists(st.sampled_from(CONFIG_KEYS[kind]), max_size=5, unique=True))
        attrs = {}
        for key in keys:
            if key.endswith("."):
                key += draw(SUFFIXES)
            attrs[key] = draw(values)
        entity = draw(st.sampled_from({"run_flags": ["run", "admin_answers"],
                                       "cost_center": ["cc:A", "cc:B"]}.get(
                                           kind, ["org:a", "org:b", "cc:A", "enterprise"])))
        try:
            out.append(b.make_config(kind, attrs, entity_id=entity,
                                     snapshot_ms=ms(f"2026-10-{draw(st.integers(1, 28)):02d}")))
        except TokenbillError:
            continue            # the record validator refused it (e.g. a p_-looking value)
    return out


@settings(max_examples=150, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(configs(), st.sampled_from(["enterprise", "org"]))
def test_fuzz_configuration_values(conf: list, mode: str) -> None:
    lines = [b.make_seat_line("business", "3", date_utc="2026-10-01", cost_center="A")]
    lics = [b.make_license(b.make_principal(i), snapshot_date="2026-10-05", plan="unknown")
            for i in range(3)]
    for call in (lambda: run_flags(conf), lambda: capped_cost_centers(conf),
                 lambda: capped_policies(conf),
                 lambda: seat_months(lines, lics, conf, "2026-10", entity_mode=mode),
                 lambda: detect_plans(lines, lics, conf, month="2026-10", entity_mode=mode),
                 lambda: pool_months(build_cells([], lines, capped=capped_cost_centers(conf))[0],
                                     lines, lics, conf, today="2026-10-20", entity_mode=mode)):
        try:
            call()
        except TokenbillError:
            pass


months = st.one_of(st.text(max_size=8), st.from_regex(r"\d{4}-\d{2}", fullmatch=True))
dates = st.one_of(st.text(max_size=11), st.from_regex(r"\d{4}-\d{2}-\d{2}", fullmatch=True))
anything = st.one_of(values, st.decimals(allow_nan=True), st.floats(), st.tuples(st.integers()))


@settings(max_examples=200, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(months, dates, st.dictionaries(st.text(max_size=10), anything, max_size=3),
       st.lists(st.tuples(dates, st.one_of(st.none(), st.text(max_size=8)), anything), max_size=3),
       st.one_of(st.none(), st.booleans(), st.dictionaries(st.text(max_size=20), anything)),
       st.integers(-3, 400))
def test_fuzz_arguments(month, today, mapping, estimates, gil, lag) -> None:
    pm = b.make_pool_month(consumed_report_nano=10**12)
    line, agg = b.make_ai_usage_row(date_utc="2026-10-02")
    cells = build_cells([agg], [line])[0]
    for call in (lambda: pool_credits(mapping, month, promo_eligible=True),
                 lambda: forecast([(today, 5)], month=month, today=today, lag_days=lag),
                 lambda: realize_seat_change(pm, {"business": -1}, month_fee=mapping),
                 lambda: realize_seat_change(pm, mapping, month_fee={}),
                 lambda: pool_months(cells, [], [], [], today=today, recent_estimates=estimates,
                                     gross_is_list=gil),
                 lambda: seat_months([], [], [], month),
                 lambda: detect_plans([], [], [], month=month)):
        try:
            call()
        except TokenbillError:
            pass


amounts = st.one_of(st.integers(-10**80, 10**80), st.sampled_from([10**70 + 1, -(10**22) - 1,
                                                                    10**22 - 1, 0, 1]))
quantities = st.one_of(st.none(), st.from_regex(r"-?[0-9]{1,40}(\.[0-9]{1,40})?", fullmatch=True))


@settings(max_examples=150, deadline=None,
          suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(st.lists(st.tuples(amounts, amounts, quantities, st.booleans()), min_size=1, max_size=4),
       st.integers(-10**40, 10**40), st.integers(-10**40, 10**40))
def test_fuzz_cost_line_amounts_and_counts(items, seats: int, delta: int) -> None:
    """Hostile but record-valid amounts, quantities and counts: only TokenbillError escapes."""
    base, _ = b.make_ai_usage_row(date_utc="2026-10-02")
    lines = []
    for i, (net, gross, qty, seat) in enumerate(items):
        try:
            if seat:
                line = b.make_seat_line("business", "1", date_utc="2026-10-01")
                lines.append(dataclasses.replace(line, quantity=qty, amount_nano=net))
            else:
                lines.append(dataclasses.replace(base, line_id=f"cl_{i}", quantity=qty,
                                                 amount_nano=net, list_amount_nano=gross))
        except TokenbillError:
            continue
    plan = b.make_plan_evidence(month="2026-10", plan="unknown", seats={"unknown": abs(seats)})
    pm = b.make_pool_month(consumed_report_nano=10**12)
    for call in (lambda: build_cells([], lines),
                 lambda: pool_months(build_cells([], lines)[0], lines, [], [], today="2026-10-20"),
                 lambda: pool_months([], lines, [], [], today="2026-11-20", plans=[plan]),
                 lambda: seat_months(lines, [], [], "2026-10"),
                 lambda: detect_plans(lines, [], [], month="2026-10"),
                 lambda: realize_seat_change(pm, {"business": delta, "enterprise": -delta},
                                             month_fee={"business": str(seats)})):
        try:
            call()
        except TokenbillError:
            pass


# ---------- no float in the money module (SPEC §2.4, C-30) ----------


def test_pool_module_has_no_float() -> None:
    import tokenbill.core.pool as pool

    tree = ast.parse(Path(pool.__file__).read_text(encoding="utf-8"))
    floats = [n.lineno for n in ast.walk(tree)
              if (isinstance(n, ast.Constant) and isinstance(n.value, float))
              or (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "float")
              or (isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div))]
    assert floats == []
