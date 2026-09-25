"""Plan-fit diagnostic (owner answer 2, addendum §12 item 5, R17): Appendix C.P13."""

from __future__ import annotations

from tokenbill.core import pool
from tokenbill.core.builders import make_config, make_license

from . import world as w

MONTH = "2026-10"
TODAY = "2026-11-20"


def _p13(discount_per_day: str | None, *, capped: bool = False, days: int = 10):
    """100 activity-report seats (plan unknown), 25,000 credits a day (250,000 over 10 days)."""
    store, rs = w.world()
    config = []
    if capped:
        config.append(make_config("cost_center", {"pool_enabled": True,
                                                  "pool_target_credits": "1000"},
                                  entity_id="cc:Z", source_kind="github.cost_centers"))
    w.put_records(rs, licenses=[
        make_license(w.p(f"u{i}"), snapshot_date=f"{MONTH}-15", plan="unknown",
                     source_kind="github.copilot_activity_report", assigned_via_team=None)
        for i in range(100)], config=config)
    pairs = [w.row(d, uncached=0, read=0, write=0, output=25_000_000,
                   discount=discount_per_day, discount_all=discount_per_day is None)
             for d in w.days(MONTH, days)]
    lines = [ln for ln, _ in pairs]
    w.ingest(store, lines=lines, aggs=[a for _, a in pairs])
    return store, rs


def _scenarios(store, rs):
    lines = store.cost_lines(since_ms=0, until_ms=2**53)
    cells, _ = pool.build_cells(store.aggregates(since_ms=0, until_ms=2**53), lines)
    pms = pool.pool_months(cells, lines, rs.licenses(), rs.config(), today=TODAY)
    return {pm.plan_scenario: pm.pool_credits for pm in pms if pm.entity_id == "enterprise"}


def test_c_p13_pooled_discount_above_the_business_pool_fits_enterprise() -> None:
    store, rs = _p13(None)
    report = w.reconcile(store, rs, month=MONTH, today=TODAY)
    assert w.decisions(report)["plan_fit:enterprise:2026-10"] == "enterprise"
    assert w.residuals(report)["copilot_plan_inferred"] == 0
    row = w.rows_of(report, check="plan_fit")[0]
    assert (row.status, row.residual_code, dict(row.key)["plan_fit"]) == \
        ("explained", "copilot_plan_inferred", "enterprise")
    # both scenario pool months untouched: the decision never picks a scenario or a label
    assert _scenarios(store, rs) == {"business": "190000", "enterprise": "390000"}
    # the discount cannot be classified while the plan is unknown (it exceeds one scenario pool)
    assert "copilot_discount_unclassified" in w.residuals(report)


def test_c_p13_overage_below_the_enterprise_pool_fits_business() -> None:
    store, rs = _p13("19000")          # 190,000 credits of discount, net 60,000 credits ($600)
    net = sum(c.amount_nano for c in store.cost_lines(since_ms=0, until_ms=2**53))
    assert net == 600 * 10**9
    report = w.reconcile(store, rs, month=MONTH, today=TODAY)
    assert w.decisions(report)["plan_fit:enterprise:2026-10"] == "business"
    assert _scenarios(store, rs) == {"business": "190000", "enterprise": "390000"}
    assert w.residuals(report)["copilot_pool_included"] == -190_000 * w.CREDIT


def test_pooled_use_above_the_business_pool_without_overage_fits_enterprise() -> None:
    """Use 250,000 credits over a 190,000 Business pool but no overage billed: only Enterprise
    fits, even when the discount itself is not all visible (Auto tenths aside)."""
    store, rs = _p13("24000")          # 240,000 credits discount, net 10,000 credits
    report = w.reconcile(store, rs, month=MONTH, today=TODAY)
    assert w.decisions(report)["plan_fit:enterprise:2026-10"] == "enterprise"


def test_capped_cost_center_or_open_month_stays_unknown() -> None:
    store, rs = _p13("19000", capped=True)
    report = w.reconcile(store, rs, month=MONTH, today=TODAY)
    assert w.decisions(report)["plan_fit:enterprise:2026-10"] == "unknown"
    assert "copilot_plan_inferred" not in w.residuals(report)
    store, rs = _p13(None)
    report = w.reconcile(store, rs, month=MONTH, today="2026-10-20")
    assert w.decisions(report)["plan_fit:enterprise:2026-10"] == "unknown"


def test_usage_below_both_pools_with_overage_fits_neither() -> None:
    store, rs = _p13("0", days=2)      # 50,000 credits, no discount at all
    lines = store.cost_lines(since_ms=0, until_ms=2**53)
    assert sum(c.list_amount_nano or 0 for c in lines) == 50_000 * w.CREDIT
    report = w.reconcile(store, rs, month=MONTH, today=TODAY)
    assert w.decisions(report)["plan_fit:enterprise:2026-10"] == "unknown"


def test_known_plan_has_no_plan_fit_key() -> None:
    store, rs = _p13(None)
    w.ingest(store, lines=[w.seats("enterprise", "100", MONTH)], adapter="github-metered-usage",
             source_id="m")
    report = w.reconcile(store, rs, month=MONTH, today=TODAY)
    assert not any(k.startswith("plan_fit:") for k in w.decisions(report))
