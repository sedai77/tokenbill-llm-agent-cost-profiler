"""The pool rule (addendum R11, DC3, CA-40): pools, regimes, cap policies, conversions and the
forecast — Appendix C.P2–P5, P7–P13c exactly (nano integers and labels)."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from tokenbill.core.builders import make_pool_month
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.labels import Basis, Calibration, Evidence, estimated, exact, unpriced
from tokenbill.core.pool import (
    forecast,
    invoice_delta,
    overage_total,
    pool_credits,
    realize_credit_saving,
    realize_seat_change,
    regime,
)

from .worlds import USD, C, p9_series

FEES = {"business": Decimal("19"), "enterprise": Decimal("39")}
P1_SEATS = {"business": "1000", "enterprise": "200"}


def _pm(consumed_credits: int, **kw: object):
    kw.setdefault("seats", P1_SEATS)
    return make_pool_month(consumed_report_nano=consumed_credits * C, **kw)  # type: ignore[arg-type]


# ---------- pools and regimes ----------


def test_pool_credits_plans_and_promo() -> None:
    assert pool_credits({"business": Decimal(1000), "enterprise": Decimal(200)}, "2026-10",
                        promo_eligible=True) == (Decimal(2_680_000), None)       # C.P1
    total, promo = pool_credits({"business": Decimal(100)}, "2026-07", promo_eligible=True)
    assert (total, promo) == (Decimal(300_000), "promo:2026-06-01/2026-09-01")    # C.P6
    assert pool_credits({"enterprise": 40}, "2026-07", promo_eligible=True)[0] == 280_000
    assert pool_credits({"business": Decimal(100)}, "2026-07", promo_eligible=False) == (
        Decimal(190_000), None)
    assert pool_credits({"business": "99.5"}, "2026-09", promo_eligible=True)[0] == Decimal(
        "189050")
    assert pool_credits({}, "2026-09", promo_eligible=True) == (Decimal(0), None)
    assert pool_credits({"business": 0}, "2026-07", promo_eligible=True) == (Decimal(0), None)


@pytest.mark.parametrize("seats", [{"unknown": Decimal(1)}, {"unknown": 0}, {"pro": 1},
                                   {"business": -1}, {"business": "many"}, {"business": 1.5}])
def test_pool_credits_rejects_unknown_plans_and_bad_counts(seats: dict) -> None:
    with pytest.raises(UsageError):
        pool_credits(seats, "2026-09", promo_eligible=True)


def test_regime() -> None:
    assert regime(10, 10, 10) == "slack"
    assert regime(11, 12, 10) == "overage"
    assert regime(9, 11, 10) == "straddling"
    assert regime(1, 2, None) == "unknown"
    with pytest.raises(UsageError):
        regime(2, 1, 10)
    with pytest.raises(UsageError):
        regime(1, 2, "10")  # type: ignore[arg-type]


# ---------- C.P7 capped cost center, C.P8 ex post ----------


@pytest.mark.parametrize("policy,expected", [("continue", (350, 350)), ("block", (0, 0)),
                                             ("unknown", (0, 350))])
def test_p7_capped_cost_center_policies(policy: str, expected: tuple[int, int]) -> None:
    use = {"enterprise": 2_000_000 * C, "cc:A": 130_000 * C}
    policies = {} if policy == "unknown" else {"A": policy}
    got = overage_total(use, {"enterprise": 2_680_000 * C}, {"A": 95_000 * C}, policies=policies)
    assert got == (expected[0] * USD, expected[1] * USD)
    # the same with partitioned pools (the parent's own seats plus the cap entry) and entity keys
    got = overage_total(use, {"enterprise": 2_585_000 * C, "cc:A": 95_000 * C},
                        {"cc:A": 95_000 * C},
                        policies={f"cc:{k}": v for k, v in policies.items()})
    assert got == (expected[0] * USD, expected[1] * USD)


def test_p7_shared_slack_absorbs_capped_draws() -> None:
    # the enterprise remainder counts the capped draw min(use, cap) against the shared pool
    use = {"enterprise": 2_600_000 * C, "cc:A": 50_000 * C}
    assert overage_total(use, {"enterprise": 2_680_000 * C}, {"A": 95_000 * C},
                         policies={"A": "continue"}) == (0, 0)
    use["enterprise"] = 2_700_000 * C
    assert overage_total(use, {"enterprise": 2_680_000 * C}, {"A": 95_000 * C},
                         policies={"A": "block"}) == (70_000 * C, 70_000 * C)


def test_overage_total_independent_pools_and_errors() -> None:
    use = {"org:a": 300 * C, "org:b": 100 * C}
    assert overage_total(use, {"org:a": 200 * C, "org:b": 200 * C}, {}, policies={}) == (
        100 * C, 100 * C)
    with pytest.raises(UsageError):
        overage_total(use, {"org:a": 1, "org:b": 1}, {"A": 1}, policies={})
    with pytest.raises(UsageError):
        overage_total({"enterprise": "1"}, {}, {}, policies={})  # type: ignore[dict-item]


@pytest.mark.parametrize("after,expected_usd", [(2_700_000, 1_000), (2_500_000, 0),
                                                (2_600_000, 200)])
def test_p8_invoice_delta(after: int, expected_usd: int) -> None:
    before = after + 100_000
    assert invoice_delta(before * C, after * C, 2_680_000 * C) == expected_usd * USD
    with pytest.raises(UsageError):
        invoice_delta(1, 2, None)  # type: ignore[arg-type]


# ---------- C.P2–P4, P10–P12, P13b: seat changes ----------


@pytest.mark.parametrize("use,remove,expected_usd", [
    (3_100_000, 50, 0),      # P2: overage 420,000 → 515,000 (+$950) cancels the fees
    (2_000_000, 50, 950),    # P3: slack 680,000
    (2_650_000, 50, 300),    # P4: slack 30,000; new overage 65,000 ($650)
    (2_000_000, 10, 190),    # P11: seat-policy projection in P3's regime
    (2_000_000, 12, 228),    # P12: 12 removable of 20 idle seats
])
def test_p2_p4_p11_p12_seat_removals(use: int, remove: int, expected_usd: int) -> None:
    fig = realize_seat_change(_pm(use), {"business": -remove}, month_fee=FEES)
    assert fig is not None
    assert (fig.nano, fig.evidence, fig.basis) == (expected_usd * USD, Evidence.ESTIMATED,
                                                   Basis.LIST)
    assert fig.low_nano is None and "list price" in fig.note


@pytest.mark.parametrize("mode", ["volume", "azure"])
def test_p10_volume_and_azure_save_only_at_renewal(mode: str) -> None:
    assert realize_seat_change(_pm(2_000_000, billing_mode=mode), {"business": -10},
                               month_fee=FEES) is None


def test_p13b_idle_seats_per_scenario() -> None:
    biz = _pm(250_000, seats={"unknown": "100"}, plan_scenario="business")
    ent = _pm(250_000, seats={"unknown": "100"}, plan_scenario="enterprise")
    b_fig = realize_seat_change(biz, {"unknown": -10}, month_fee=FEES)
    e_fig = realize_seat_change(ent, {"unknown": -10}, month_fee=FEES)
    assert b_fig is not None and e_fig is not None
    assert (b_fig.nano, e_fig.nano) == (0, 390 * USD)
    assert "scenario business" in b_fig.note and "scenario enterprise" in e_fig.note
    with pytest.raises(UsageError):
        realize_seat_change(_pm(10), {"unknown": -1}, month_fee=FEES)


def test_seat_change_details() -> None:
    pm = _pm(2_000_000)
    # facts supply missing fees; a zero delta is no change; downgrades save the fee difference
    assert realize_seat_change(pm, {"business": -10}, month_fee={}).nano == 190 * USD  # type: ignore[union-attr]
    assert realize_seat_change(pm, {"business": 0}, month_fee=FEES).nano == 0  # type: ignore[union-attr]
    down = realize_seat_change(pm, {"enterprise": -10, "business": 10}, month_fee=FEES)
    assert down is not None and down.nano == 200 * USD
    unknown_mode = realize_seat_change(replace(pm, billing_mode="unknown"), {"business": -1},
                                       month_fee=FEES)
    assert unknown_mode is not None and "billing mode unknown" in unknown_mode.note
    with pytest.raises(UsageError):
        realize_seat_change(pm, {"business": -1.0}, month_fee=FEES)  # type: ignore[dict-item]
    with pytest.raises(UsageError):
        realize_seat_change(pm, {"pro": -1}, month_fee=FEES)
    with pytest.raises(UsageError):
        realize_seat_change(pm, {"business": -1}, month_fee={"business": object()})  # type: ignore[dict-item]
    with pytest.raises(ContractViolation):
        realize_seat_change("pm", {}, month_fee=FEES)  # type: ignore[arg-type]


def test_seat_change_in_promo_month_can_cost_more_than_it_saves() -> None:
    """During the promo a Business seat carries 3,000 credits ($30) for a $19 fee: removing it in
    overage raises the overage by more than the fee (an honest negative saving)."""
    pm = make_pool_month(seats={"business": "100"}, month="2026-07", promo="promo:x",
                         consumed_report_nano=400_000 * C)
    fig = realize_seat_change(pm, {"business": -1}, month_fee=FEES)
    assert fig is not None and fig.nano == (19 - 30) * USD


def test_seat_change_open_month_and_unknown_cap_policy_ranges() -> None:
    fc = estimated(2_700_000 * C, Basis.LIST_EQUIVALENT, low=2_600_000 * C, high=2_800_000 * C,
                   note="forecast")
    pm = replace(_pm(1_000_000), finality="open", forecast=fc, regime="straddling")
    fig = realize_seat_change(pm, {"business": -50}, month_fee=FEES)
    assert fig is not None
    # slack at p10 (2,600,000 vs 2,585,000 → +15,000) … deep overage at p90
    assert (fig.low_nano, fig.nano, fig.high_nano) == (0, 0, 800 * USD)
    cc = make_pool_month(entity_id="cc:A", seats={"business": "50"}, capped_policy="unknown",
                         consumed_report_nano=130_000 * C)
    fig = realize_seat_change(cc, {"business": -5}, month_fee=FEES)
    assert fig is not None
    assert (fig.low_nano, fig.nano, fig.high_nano) == (0, 0, 95 * USD)
    assert "cap policy unknown" in fig.note
    blocked = realize_seat_change(replace(cc, capped_policy="block"), {"business": -5},
                                  month_fee=FEES)
    assert blocked is not None and blocked.nano == 95 * USD and blocked.low_nano is None


# ---------- C.P5, P13c: credit savings ----------


@pytest.mark.parametrize("use,invoice_usd,headroom_usd", [
    (3_100_000, 1_000, 0),    # overage 420,000
    (2_740_000, 600, 400),    # overage 60,000
    (2_000_000, 0, 1_000),    # slack
])
def test_p5_credit_saving(use: int, invoice_usd: int, headroom_usd: int) -> None:
    saving = exact(1_000 * USD, Basis.LIST_EQUIVALENT)
    pm = _pm(use)
    invoice, headroom = realize_credit_saving(saving, pm)
    assert (invoice.nano, headroom.nano) == (invoice_usd * USD, headroom_usd * USD)
    assert (invoice.evidence, invoice.basis) == (Evidence.ESTIMATED, Basis.LIST)
    assert (headroom.evidence, headroom.basis) == (Evidence.ESTIMATED, Basis.LIST_EQUIVALENT)
    assert f"regime {pm.regime}" in invoice.note and f"regime {pm.regime}" in headroom.note


def test_p13c_credit_saving_per_scenario() -> None:
    saving = exact(500 * USD, Basis.LIST_EQUIVALENT)
    biz = _pm(250_000, seats={"unknown": "100"}, plan_scenario="business")
    ent = _pm(250_000, seats={"unknown": "100"}, plan_scenario="enterprise")
    b_inv, b_head = realize_credit_saving(saving, biz)
    e_inv, e_head = realize_credit_saving(saving, ent)
    assert (b_inv.nano, b_head.nano, e_inv.nano, e_head.nano) == (500 * USD, 0, 0, 500 * USD)
    assert "scenario business" in b_inv.note and "scenario enterprise" in e_head.note


def test_credit_saving_ranges_increases_and_unknowns() -> None:
    pm = _pm(2_740_000)                                           # overage 60,000 credits
    saving = estimated(1_000 * USD, Basis.LIST_EQUIVALENT, low=400 * USD, high=1_200 * USD,
                       calibration=Calibration.CALIBRATED, note="replay", provenance=("r1",))
    inv, head = realize_credit_saving(saving, pm)
    assert (inv.low_nano, inv.nano, inv.high_nano) == (400 * USD, 600 * USD, 600 * USD)
    assert (head.low_nano, head.nano, head.high_nano) == (0, 400 * USD, 600 * USD)
    assert inv.calibration is Calibration.CALIBRATED and inv.provenance == ("r1",)
    # a cost increase is billed beyond the slack (slack 680,000 credits = $6,800)
    inc, inc_head = realize_credit_saving(exact(-7_000 * USD, Basis.LIST), _pm(2_000_000))
    assert (inc.nano, inc_head.nano) == (-200 * USD, -6_800 * USD)
    inc, _ = realize_credit_saving(exact(-100 * USD, Basis.LIST_EQUIVALENT), _pm(3_100_000))
    assert inc.nano == -100 * USD
    # unknown regime or an unpriced saving → both unpriced
    for s, pm_ in ((saving, replace(pm, regime="unknown")),
                   (unpriced("no rate", Basis.LIST_EQUIVALENT), pm)):
        a, h = realize_credit_saving(s, pm_)
        assert a.nano is None and h.nano is None
        assert (a.basis, h.basis) == (Basis.LIST, Basis.LIST_EQUIVALENT)
    with pytest.raises(ContractViolation):
        realize_credit_saving(exact(1, Basis.INVOICE), pm)
    with pytest.raises(ContractViolation):
        realize_credit_saving(saving, "pm")  # type: ignore[arg-type]


def test_credit_saving_open_month_uses_the_overage_forecast() -> None:
    fc = estimated(0, Basis.LIST, low=0, high=0, note="x")
    over = estimated(300 * USD, Basis.LIST, low=0, high=900 * USD, upper_bound=True, note="o")
    pm = replace(_pm(2_000_000), finality="open", forecast=fc, overage_forecast=over,
                 regime="straddling")
    inv, head = realize_credit_saving(exact(1_000 * USD, Basis.LIST_EQUIVALENT), pm)
    assert (inv.nano, head.nano, inv.upper_bound) == (300 * USD, 700 * USD, True)
    assert "overage forecast 30000 credits" in inv.note
    lb = replace(_pm(3_100_000), seats_source="report_users")
    assert realize_credit_saving(exact(1, Basis.LIST_EQUIVALENT), lb)[0].upper_bound is True


# ---------- C.P9 forecast ----------


def test_p9_forecast() -> None:
    series = [(d, v * C) for d, v in p9_series().items()]
    assert sum(v for _, v in series) == 2_000_000 * C
    got = forecast(series, month="2026-09", today="2026-09-23", lag_days=3)
    assert got == (2_000_000 * C, 3_100_000 * C, 2_920_000 * C, 3_280_000 * C)
    assert regime(got[2], got[3], 2_680_000 * C) == "overage"    # type: ignore[index]
    # series order and days outside the month do not matter
    noise = [("2026-08-31", 10**15), ("2026-10-01", 10**15)]
    assert forecast(list(reversed(series)) + noise, month="2026-09", today="2026-09-23") == got


def test_forecast_edges() -> None:
    assert forecast([], month="2026-09", today="2026-09-02", lag_days=3) is None   # nothing yet
    assert forecast([("2026-09-01", 5)], month="2026-09", today="2026-10-10") == (5, 5, 5, 5)
    # days without a row are observed zeros
    got = forecast([("2026-09-05", 7), ("2026-09-06", 9)], month="2026-09", today="2026-09-06",
                   lag_days=0)
    assert got == (16, 16 + 6 * 7, 16 + 6 * 7, 16 + 6 * 9)
    # only weekend days observed (2026-08-01 is a Saturday): business days use weekend statistics
    got = forecast([("2026-08-01", 7), ("2026-08-02", 9)], month="2026-08", today="2026-08-02",
                   lag_days=0)
    assert got == (16, 16 + 29 * 7, 16 + 29 * 7, 16 + 29 * 9)
    # one business day observed, no weekend: weekend days use the business statistics
    got = forecast([("2026-09-01", 4)], month="2026-09", today="2026-09-01", lag_days=0)
    assert got == (4, 4 * 30, 4 * 30, 4 * 30)
    for bad in ({"month": "2026-9"}, {"today": "yesterday"}, {"lag_days": -1},
                {"lag_days": True}):
        kw = {"month": "2026-09", "today": "2026-09-10", "lag_days": 3} | bad
        with pytest.raises(UsageError):
            forecast([], **kw)  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        forecast([("2026-09-01", "5")], month="2026-09", today="2026-09-10")  # type: ignore[list-item]
    with pytest.raises(UsageError):
        forecast([("2026-02-30", 5)], month="2026-02", today="2026-03-10")
