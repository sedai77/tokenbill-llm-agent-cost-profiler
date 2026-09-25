"""L3 invoice identities per entity × month: Appendix C.P1, DC22 discount classes, C.P7 capped cost
centers, seats, the usage summary per product, Actions and sandbox, revisions (addendum §12 L3)."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

from tokenbill.core import pool
from tokenbill.core.builders import make_config, make_license

from . import world as w

MONTH = "2026-09"
USD = 10**9


def _ai_rows(store, pairs, source: str = "s1", fetched_ms: int = 0) -> None:
    lines = [ln for ln, _ in pairs]
    dates = sorted({ln.date_utc for ln in lines})
    w.ingest(store, lines=lines, aggs=[a for _, a in pairs] + [
        w.coverage(source, d, lines, fetched_ms=fetched_ms) for d in dates], source_id=source)


def test_c_p1_month_reconciles_all_three_channels() -> None:
    store, rs = w.p1_world()
    report = w.reconcile(store, rs)
    assert w.verdicts(report) == {"github_copilot": "reconciled", "github_actions": "reconciled",
                                  "github_sandbox": "reconciled"}
    assert report.verdict == "reconciled"
    assert report.unexplained_nano == 0
    assert report.finality.value == "final"
    d = w.decisions(report)
    assert d["gross_is_list:enterprise:2026-09"] == "true"
    assert d["convention:s_p1"] == "excl"
    assert not any(k.startswith("plan_fit:") for k in d)          # the plan is known
    # identities hold
    ident = w.rows_of(report, check="row_identity")
    assert [(r.status, dict(r.key)["failing_rows"]) for r in ident] == [("match", "0")]
    # discounts classified pool-included (the whole C.P1 pool of 2,680,000 credits)
    res = w.residuals(report)
    assert res["copilot_pool_included"] == -w.P1_POOL
    assert "copilot_discount_unclassified" not in res
    assert res["copilot_model_undisclosed"] == 15_000 * w.CREDIT     # direct code review
    # usage summary per product and the seat, Actions and sandbox checks
    summ = {dict(r.key)["product"]: r.status for r in w.rows_of(report, check="summary")
            if "diagnostic" not in dict(r.key) and "sku" not in dict(r.key)}
    assert summ == {"ai_credits": "match", "seats": "match", "sandbox": "match"}
    assert {r.status for r in w.rows_of(report, check="summary", product="actions")} == {"match"}
    assert {r.status for r in w.rows_of(report, check="runner_rate")} == {"match"}
    assert {r.status for r in w.rows_of(report, check="seats")} == {"match"}
    assert {r.status for r in w.rows_of(report, check="revision")} == {"match"}
    # C.P1 invoice arithmetic: overage ≈ 420,000 credits, direct 15,000 credits (net)
    lines = store.cost_lines(since_ms=0, until_ms=2**53)
    pooled_net = sum(c.amount_nano for c in lines if c.cost_type == "ai_credit.user")
    direct_net = sum(c.amount_nano for c in lines if c.cost_type == "ai_credit.direct")
    assert abs(pooled_net - 420_000 * w.CREDIT) < 100 * w.CREDIT
    assert direct_net == 150 * USD


def test_discounts_stay_unclassified_while_gross_is_not_list() -> None:
    store, rs = w.p1_world(scale=Decimal("1.04"))
    report = w.reconcile(store, rs)
    assert w.decisions(report)["gross_is_list:enterprise:2026-09"] == "unknown"   # undecidable
    res = w.residuals(report)
    assert "copilot_pool_included" not in res and res["copilot_discount_unclassified"] < 0
    # with a cache-free month the same 4% gap is evaluable: gross_is_list false
    store2, rs2 = w.world()
    _ai_rows(store2, [w.row(d, read=0, write=0, scale=Decimal("1.04"), discount_all=True)
                      for d in w.days(MONTH, 5)])
    report2 = w.reconcile(store2, rs2)
    assert w.decisions(report2)["gross_is_list:enterprise:2026-09"] == "false"
    assert "copilot_discount_unclassified" in w.residuals(report2)
    assert w.verdicts(report2)["github_copilot"] == "not_reconciled"


def test_direct_rows_that_draw_on_the_pool() -> None:
    store, rs = w.world()
    pairs = [w.row(d, read=0, write=0, discount_all=True) for d in w.days(MONTH, 3)]
    pairs += [w.row(d, read=0, write=0, discount_all=True, unattributed=True, team=None)
              for d in w.days(MONTH, 3)]
    _ai_rows(store, pairs)
    w.ingest(store, lines=[w.seats("business", "10")], adapter="github-metered-usage",
             source_id="m")
    report = w.reconcile(store, rs)
    res = w.residuals(report)
    assert res["copilot_direct_pool_draw"] < 0 and res["copilot_pool_included"] < 0
    assert res["copilot_direct_pool_draw"] == res["copilot_pool_included"]


def test_auto_tenth_is_an_other_discount() -> None:
    store, rs = w.world()
    pairs = []
    for d in w.days(MONTH, 3):
        probe, _ = w.row(d, "Auto: Claude Sonnet 5", read=0, write=0, apply_auto=False)
        tenth = w.credits_of((probe.list_amount_nano or 0) // 10)
        pairs.append(w.row(d, "Auto: Claude Sonnet 5", read=0, write=0, apply_auto=False,
                           discount=tenth))
    _ai_rows(store, pairs)
    report = w.reconcile(store, rs)
    rows = w.rows_of(report, check="discount_class")
    assert {r.residual_code for r in rows} == {"copilot_auto_discount"}
    # the rate card shows +10% (at list), the discount −10%: they net to (about) zero
    assert abs(w.residuals(report)["copilot_auto_discount"]) <= 3 * w.CREDIT // 10**6


def _p7(per_row_discount: str) -> tuple:
    store, rs = w.world()
    w.put_records(rs, config=[
        make_config("cost_center", {"pool_enabled": True, "pool_target_credits": "95000"},
                    entity_id="cc:A", source_kind="github.cost_centers"),
        make_config("run_flags", {"capped_policy.A": "continue"})])
    pairs = []
    for d in w.days(MONTH, 10):
        pairs.append(w.row(d, uncached=0, read=0, write=0, output=13_000_000, team="ta",
                           cost_center="A", discount=per_row_discount))
        pairs.append(w.row(d, uncached=0, read=0, write=0, output=200_000_000, team="tb",
                           discount_all=True))
    _ai_rows(store, pairs)
    w.ingest(store, lines=[w.seats("business", "1000"), w.seats("enterprise", "200")],
             adapter="github-metered-usage", source_id="m")
    return store, rs


def test_c_p7_capped_cost_center_within_its_cap() -> None:
    store, rs = _p7("9500")
    report = w.reconcile(store, rs)
    cap = w.rows_of(report, check="cost_center_cap")
    assert [(r.status, dict(r.key)["entity"], dict(r.key)["policy"], r.priced_provider_nano,
             r.invoice_nano) for r in cap] == [
        ("match", "cc:A", "continue", 95_000 * w.CREDIT, 95_000 * w.CREDIT)]
    by_entity = {dict(r.key)["entity"]: (r.residual_code, r.invoice_nano)
                 for r in w.rows_of(report, check="discount_class")}
    assert by_entity == {"cc:A": ("copilot_pool_included", -95_000 * w.CREDIT),
                         "enterprise": ("copilot_pool_included", -2_000_000 * w.CREDIT)}
    assert w.decisions(report)["gross_is_list:cc:A:2026-09"] == "true"
    net_a = sum(c.amount_nano for c in store.cost_lines(since_ms=0, until_ms=2**53)
                if c.cost_center == "A")
    assert net_a == 350 * USD                                    # C.P7: A's overage $350
    assert w.verdicts(report)["github_copilot"] == "reconciled"


def test_c_p7_capped_cost_center_above_its_cap_fails() -> None:
    store, rs = _p7("10000")
    report = w.reconcile(store, rs)
    cap = w.rows_of(report, check="cost_center_cap")
    assert [r.status for r in cap] == ["over"]
    assert report.unexplained_nano == 5_000 * w.CREDIT
    assert w.verdicts(report)["github_copilot"] == "not_reconciled"


def test_seat_proration_and_contract() -> None:
    store, rs = w.world()
    w.put_records(rs, licenses=[make_license(w.p(f"u{i}"), snapshot_date="2026-09-15",
                                             plan="business", org="org-a") for i in range(100)])
    _ai_rows(store, [w.row(d, read=0, write=0) for d in w.days(MONTH, 2)])
    w.ingest(store, lines=[w.seats("business", "95.5")], adapter="github-metered-usage",
             source_id="m")
    report = w.reconcile(store, rs)
    seat = w.rows_of(report, check="seats")
    assert [(r.status, r.residual_code, dict(r.key)["count_source"], r.priced_provider_nano,
             r.invoice_nano) for r in seat] == [
        ("explained", "copilot_seat_proration", "seats_api", 1_814_500_000_000,
         1_900 * USD)]
    assert w.residuals(report)["copilot_seat_proration"] == -85_500_000_000
    assert w.verdicts(report)["github_copilot"] == "reconciled"
    w.put_records(rs, config=[make_config("run_flags", {"billing_mode.enterprise": "volume"})])
    report = w.reconcile(store, rs)
    assert [r.residual_code for r in w.rows_of(report, check="seats")] == \
        ["copilot_seat_contract"]


def test_seat_gap_beyond_the_seat_value_is_unexplained() -> None:
    store, rs = w.world()
    w.put_records(rs, licenses=[make_license(w.p(f"u{i}"), snapshot_date="2026-09-15",
                                             plan="business") for i in range(10)])
    _ai_rows(store, [w.row(d, read=0, write=0) for d in w.days(MONTH, 2)])
    w.ingest(store, lines=[w.seats("business", "25")], adapter="github-metered-usage",
             source_id="m")
    report = w.reconcile(store, rs)
    assert [r.status for r in w.rows_of(report, check="seats")] == ["unexplained"]
    assert w.verdicts(report)["github_copilot"] == "not_reconciled"


def test_revised_export_that_lacks_a_row_leaves_stale_rows() -> None:
    store, rs = w.world()
    a1, g1 = w.row("2026-09-10", team="t1", read=0, write=0)
    a2, g2 = w.row("2026-09-10", team="t2", read=0, write=0)
    w.ingest(store, lines=[a1, a2], aggs=[g1, g2, w.coverage("s_a", "2026-09-10", [a1, a2],
                                                             fetched_ms=1)], source_id="s_a")
    b1, h1 = w.row("2026-09-10", team="t1", read=0, write=0, uncached=60_000, fetched_ms=2)
    w.ingest(store, lines=[b1], aggs=[h1, w.coverage("s_b", "2026-09-10", [b1], fetched_ms=2)],
             source_id="s_b")
    report = w.reconcile(store, rs)
    rev = w.rows_of(report, check="revision")
    assert [(r.status, r.residual_code) for r in rev] == [
        ("explained", "copilot_revision_stale_rows")]
    assert w.residuals(report)["copilot_revision_stale_rows"] == -a2.amount_nano
    assert w.decisions(report)["convention:s_b"] == "undecidable"
    # with the matching usage summary the stale rows explain the summary gap instead
    w.ingest(store, lines=[w.summary(MONTH, "copilot_ai_credit", b1.amount_nano)],
             adapter="github-billing-api", source_id="rest")
    report = w.reconcile(store, rs)
    summ = [r for r in w.rows_of(report, check="summary") if "sku" not in dict(r.key)]
    assert [(r.status, r.residual_code) for r in summ] == [
        ("explained", "copilot_revision_stale_rows")]
    assert w.residuals(report)["copilot_revision_stale_rows"] == -a2.amount_nano
    assert w.verdicts(report)["github_copilot"] == "reconciled"


def test_coverage_above_current_rows_is_unexplained() -> None:
    store, rs = w.world()
    a1, g1 = w.row("2026-09-10", read=0, write=0)
    cov = dataclasses.replace(w.coverage("s_a", "2026-09-10", [a1]),
                              reported_cost_nano=a1.amount_nano + 5 * USD,
                              list_cost_nano=a1.list_amount_nano)
    w.ingest(store, lines=[a1], aggs=[g1, cov], source_id="s_a")
    report = w.reconcile(store, rs)
    assert [r.status for r in w.rows_of(report, check="revision")] == ["under"]
    assert report.unexplained_nano == 5 * USD


def test_summary_mismatch_fails_beyond_the_unexplained_tolerance() -> None:
    store, rs = w.p1_world(summary_delta=500 * USD)
    report = w.reconcile(store, rs)
    summ = [r for r in w.rows_of(report, check="summary", product="ai_credits")
            if "sku" not in dict(r.key)]
    assert [r.status for r in summ] == ["unexplained"]
    assert report.unexplained_nano == 500 * USD
    # $500 on ≈ $58,000 of gross is below 1%: reconciled; at 0.5% it is not
    assert w.verdicts(report)["github_copilot"] == "reconciled"
    strict = w.reconcile(store, rs, unexplained_pct="0.5")
    assert w.verdicts(strict)["github_copilot"] == "not_reconciled"
    sku = w.rows_of(report, check="summary", diagnostic="per_sku")
    assert [(dict(r.key)["sku"], r.status) for r in sku] == [("copilot_ai_credit", "explained")]


def test_org_summary_and_unattributed_rows() -> None:
    store, rs = w.world()
    pairs = [w.row(d, read=0, write=0) for d in w.days(MONTH, 3)]
    stray = w.row("2026-09-02", read=0, write=0, org=None, team="t9")
    _ai_rows(store, [*pairs, stray])
    org_net = sum(ln.amount_nano for ln, _ in pairs)
    w.ingest(store, lines=[w.summary(MONTH, "copilot_ai_credit", org_net + stray[0].amount_nano,
                                     org="org-a")], adapter="github-billing-api", source_id="rest")
    report = w.reconcile(store, rs)
    summ = [r for r in w.rows_of(report, check="summary") if "sku" not in dict(r.key)]
    assert [(dict(r.key)["scope"], r.status, r.residual_code) for r in summ] == [
        ("org:org-a", "explained", "copilot_unattributed_org")]
    assert w.residuals(report)["copilot_unattributed_org"] == stray[0].amount_nano


def test_row_identity_failures() -> None:
    store, rs = w.world()
    good, agg = w.row("2026-09-03", read=0, write=0)
    bad = dataclasses.replace(good, line_id="cl_bad", quantity="999", team="t2")
    worse = dataclasses.replace(good, line_id="cl_worse", team="t3",
                                amount_nano=(good.list_amount_nano or 0) + 1)
    w.ingest(store, lines=[good, bad, worse], aggs=[agg])
    report = w.reconcile(store, rs)
    ident = w.rows_of(report, check="row_identity")
    assert [(r.status, dict(r.key)["failing_rows"]) for r in ident] == [("unexplained", "2")]
    assert w.verdicts(report)["github_copilot"] == "not_reconciled"


def test_actions_channel_rules() -> None:
    store, rs = w.world()
    w.ingest(store, lines=[w.actions("2026-09-03"), w.actions("2026-09-04", sku="linux_16_core",
                                                                 usd_per_minute="0.042")],
             adapter="github-metered-usage", source_id="m")
    report = w.reconcile(store, rs)
    assert w.verdicts(report)["github_actions"] == "insufficient_data"   # no summary
    w.ingest(store, lines=[w.summary(MONTH, "actions_linux", 3 * USD, channel="github_actions"),
                           w.summary(MONTH, "actions_linux_16_core", 100 * USD,
                                     channel="github_actions")],
             adapter="github-billing-api", source_id="rest")
    report = w.reconcile(store, rs)
    over = w.rows_of(report, check="summary", product="actions", sku="linux")
    assert [(r.status, r.priced_provider_nano, r.invoice_nano) for r in over] == [
        ("over", 6 * USD, 3 * USD)]
    assert w.verdicts(report)["github_actions"] == "not_reconciled"
    assert report.verdict == "not_reconciled"


def test_actions_rate_card_and_unknown_runner() -> None:
    store, rs = w.world()
    mispriced = dataclasses.replace(w.actions("2026-09-03"), list_amount_nano=7 * USD,
                                    amount_nano=7 * USD)
    unknown = w.actions("2026-09-04", sku="quantum_runner_9", usd_per_minute="0.5")
    w.ingest(store, lines=[mispriced, unknown,
                           w.summary(MONTH, "actions_linux", 50 * USD, channel="github_actions"),
                           w.summary(MONTH, "quantum_runner_9", 900 * USD,
                                     channel="github_actions")],
             adapter="github-metered-usage", source_id="m")
    report = w.reconcile(store, rs)
    rate = {dict(r.key)["sku"]: r.status for r in w.rows_of(report, check="runner_rate")}
    assert rate == {"linux": "unexplained", "quantum_runner_9": "unexplained"}
    assert w.verdicts(report)["github_actions"] == "not_reconciled"


def test_sandbox_mismatch_and_missing_summary_sku() -> None:
    store, rs = w.world()
    w.ingest(store, lines=[w.sandbox("2026-09-05", 40 * USD),
                           w.summary(MONTH, "sandbox_linux", 55 * USD, channel="github_sandbox")],
             adapter="github-metered-usage", source_id="m")
    report = w.reconcile(store, rs)
    assert [r.status for r in w.rows_of(report, check="summary", product="sandbox")] == \
        ["unexplained"]
    assert w.verdicts(report)["github_sandbox"] == "not_reconciled"
    assert w.verdicts(report)["github_copilot"] == "insufficient_data"


def test_pool_months_are_what_core_pool_says() -> None:
    """The reconciler classifies discounts only through ``core.pool.pool_months``."""
    store, rs = w.p1_world()
    lines = store.cost_lines(since_ms=0, until_ms=2**53)
    aggs = store.aggregates(since_ms=0, until_ms=2**53)
    cells, _ = pool.build_cells(aggs, lines)
    pms = pool.pool_months(cells, lines, [], [], today="2026-10-20", gross_is_list=True)
    assert [(pm.entity_id, pm.pool_draw_nano, pm.plan_scenario) for pm in pms] == [
        ("enterprise", w.P1_POOL, None)]


def test_closed_only_skips_open_months_in_l3() -> None:
    store, rs = w.p1_world()
    report = w.reconcile(store, rs, today="2026-10-01", closed_only=True)
    assert not w.rows_of(report, check="summary")
    assert not w.rows_of(report, check="discount_class")


def test_open_month_summary_gap_is_revision_window() -> None:
    store, rs = w.p1_world(summary_delta=5_000 * USD)
    report = w.reconcile(store, rs, today="2026-10-01")
    summ = [r for r in w.rows_of(report, check="summary", product="ai_credits")
            if "sku" not in dict(r.key)]
    assert [(r.status, r.residual_code) for r in summ] == [("explained", "revision_window")]
    assert w.residuals(report)["revision_window"] == 5_000 * USD
    assert report.finality.value == "provisional"
