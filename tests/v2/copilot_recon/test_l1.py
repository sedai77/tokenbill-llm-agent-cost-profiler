"""L1 rate card on report tokens: Auto, compliance, long-context and utility explanations, pseudo
cells, K-dated rate boundaries, windows and provisional days (addendum §12 L1)."""

from __future__ import annotations

from decimal import Decimal

from tokenbill.core.records import UsageBuckets

from . import world as w


def _ingest(store, pairs, source: str = "s1") -> None:
    lines = [ln for ln, _ in pairs]
    aggs = [ag for _, ag in pairs]
    dates = sorted({ln.date_utc for ln in lines})
    aggs.extend(w.coverage(source, d, lines) for d in dates)
    w.ingest(store, lines=lines, aggs=aggs, source_id=source)


def _base(dates: list[str]) -> list:
    return [w.row(d, team="t1") for d in dates]


def test_auto_rows_at_list_show_minus_ten_percent_and_are_explained() -> None:
    store, rs = w.world()
    dates = w.days("2026-09", 5)
    pairs = _base(dates) + [w.row(d, "Auto: Claude Sonnet 5", team="t2", apply_auto=False)
                            for d in dates]
    _ingest(store, pairs)
    report = w.reconcile(store, rs)
    auto = w.rows_of(report, layer="L1", routing="auto")
    assert len(auto) == 5
    assert {r.rate_card_error_pct for r in auto} == {"-10.0000"}
    assert {(r.status, r.residual_code) for r in auto} == {("explained", "copilot_auto_discount")}
    assert w.residuals(report)["copilot_auto_discount"] > 0
    assert w.verdicts(report)["github_copilot"] == "reconciled"
    assert w.decisions(report)["convention:s1"] == "excl"
    assert w.decisions(report)["gross_is_list:enterprise:2026-09"] == "true"


def test_auto_rows_billed_with_the_discount_match() -> None:
    store, rs = w.world()
    _ingest(store, [w.row(d, "Auto: Claude Sonnet 5") for d in w.days("2026-09", 3)])
    report = w.reconcile(store, rs)
    assert {r.status for r in w.rows_of(report, layer="L1")} == {"match"}
    assert "copilot_auto_discount" not in w.residuals(report)


def test_pseudo_rows_never_enter_l1_pricing() -> None:
    store, rs = w.world()
    pairs = _base(w.days("2026-09", 3)) + [
        w.row("2026-09-02", "Code Review", credits="40", unattributed=True, team=None),
        w.row("2026-09-02", "Copilot coding agent", credits="25", team="t2")]
    _ingest(store, pairs)
    report = w.reconcile(store, rs)
    pseudo = w.rows_of(report, layer="L1", model="")
    assert {dict(r.key)["pseudo"] for r in pseudo} == {"code_review", "cloud_agent"}
    assert all(r.priced_provider_nano is None and r.residual_code == "copilot_model_undisclosed"
               for r in pseudo)
    assert w.residuals(report)["copilot_model_undisclosed"] == 65 * w.CREDIT
    priced = [r for r in w.rows_of(report, layer="L1") if dict(r.key)["model"]]
    assert {dict(r.key)["model"] for r in priced} == {"claude-sonnet-5"}
    assert w.verdicts(report)["github_copilot"] == "reconciled"


def test_only_pseudo_rows_leave_the_file_undecidable() -> None:
    store, rs = w.world()
    _ingest(store, [w.row("2026-09-02", "Code Review", credits="40", unattributed=True,
                          team=None)])
    report = w.reconcile(store, rs)
    assert w.decisions(report)["convention:s1"] == "undecidable"
    assert w.decisions(report)["gross_is_list:enterprise:2026-09"] == "unknown"


def test_k_dated_rate_boundary_is_not_a_failure() -> None:
    """GPT-5.6 Sol on 2026-08-21 billed at the 08-20 rate (the K-dated change is a docs commit
    date, not the billing date): ``copilot_rate_boundary``, excluded from the tolerance test."""
    store, rs = w.world()
    usage = dict(uncached=80_000, read=300_000, write=10_000, output=12_000)
    pairs = [w.row(d, "GPT-5.6 Sol", team="t1", **usage) for d in ("2026-08-10", "2026-08-11")]
    old = w.list_price(UsageBuckets(uncached_input=80_000, cache_read=300_000,
                                    cache_write_unknown=10_000, output=12_000), "GPT-5.6 Sol",
                       "2026-08-20")
    pairs.append(w.row("2026-08-21", "GPT-5.6 Sol", team="t1", credits=w.credits_of(old),
                       **usage))
    _ingest(store, pairs)
    report = w.reconcile(store, rs, month="2026-08")
    edge = w.rows_of(report, layer="L1", date="2026-08-21")
    assert [(r.status, r.residual_code) for r in edge] == [("explained", "copilot_rate_boundary")]
    assert edge[0].rate_card_error_pct != "0.0000"
    assert w.verdicts(report)["github_copilot"] == "reconciled"
    # the same mis-billed day away from a boundary fails
    store2, rs2 = w.world()
    old10 = w.list_price(UsageBuckets(uncached_input=80_000, cache_read=300_000,
                                      cache_write_unknown=10_000, output=12_000), "GPT-5.6 Sol",
                         "2026-08-20")
    _ingest(store2, [w.row("2026-08-10", "GPT-5.6 Sol", **usage),
                     w.row("2026-08-11", "GPT-5.6 Sol", **usage),
                     w.row("2026-08-12", "GPT-5.6 Sol", **usage),
                     w.row("2026-08-14", "GPT-5.6 Sol", credits=w.credits_of(old10), **usage)])
    report2 = w.reconcile(store2, rs2, month="2026-08")
    assert [r.status for r in w.rows_of(report2, layer="L1", date="2026-08-14")] == ["unexplained"]
    assert w.verdicts(report2)["github_copilot"] == "not_reconciled"
    assert report2.unexplained_nano > 0


def test_compliance_uplift_is_explained() -> None:
    store, rs = w.world()
    pairs = _base(w.days("2026-09", 3)) + [
        w.row(d, team="t2", org="org-eu", compliance="data_residency")
        for d in w.days("2026-09", 3)]
    _ingest(store, pairs)
    report = w.reconcile(store, rs)
    assert w.decisions(report)["convention:s1"] == "excl"
    t2 = [r for r in w.rows_of(report, layer="L1") if r.residual_code]
    assert {dict(r.key)["org"] for r in t2} == {"org-eu"}
    assert {r.residual_code for r in t2} == {"copilot_compliance_uplift"}
    assert w.verdicts(report)["github_copilot"] == "reconciled"


def test_compliance_run_flag_prices_the_uplift() -> None:
    from tokenbill.core.builders import make_config
    store, rs = w.world()
    w.put_records(rs, config=[make_config("run_flags", {"compliance": "fedramp"})])
    _ingest(store, [w.row(d, compliance="fedramp") for d in w.days("2026-09", 3)])
    report = w.reconcile(store, rs)
    assert {r.status for r in w.rows_of(report, layer="L1")} == {"match"}


def test_long_context_band_positive_gap_is_explained() -> None:
    store, rs = w.world()
    usage = UsageBuckets(uncached_input=400_000, cache_read=100_000, output=5_000)
    band = w.PRICER.price_usage(usage, w.list_ctx("GPT-5.5"), ts_ms=w.day_ms("2026-09-05"))
    pairs = [w.row(d, "GPT-5.5", uncached=40_000, read=100_000, write=0, output=5_000)
             for d in w.days("2026-09", 3)]
    pairs.append(w.row("2026-09-05", "GPT-5.5", uncached=400_000, read=100_000, write=0,
                       output=5_000, credits=w.credits_of(band.figure.nano or 0)))
    _ingest(store, pairs)
    report = w.reconcile(store, rs)
    row = w.rows_of(report, layer="L1", date="2026-09-05")[0]
    assert (row.status, row.residual_code) == ("explained", "copilot_long_context_band")
    assert Decimal(row.rate_card_error_pct) < 0
    assert w.verdicts(report)["github_copilot"] == "reconciled"


def test_utility_calls_with_zero_gross_are_unbilled() -> None:
    store, rs = w.world()
    pairs = _base(w.days("2026-09", 2)) + [
        w.row("2026-09-02", "GPT-4o mini", credits="0", team="t2"),
        w.row("2026-09-02", "GPT-5.4 nano", credits="0", team="t3")]
    _ingest(store, pairs)
    report = w.reconcile(store, rs)
    util = [r for r in w.rows_of(report, layer="L1") if r.residual_code]
    assert {r.residual_code for r in util} == {"copilot_utility_unbilled"}
    assert w.verdicts(report)["github_copilot"] == "reconciled"


def test_unpriced_model_is_unexplained() -> None:
    store, rs = w.world()
    _ingest(store, _base(w.days("2026-09", 3)) + [
        w.row("2026-09-02", "Mystery Model 9", credits="12", team="t2")])
    report = w.reconcile(store, rs)
    row = w.rows_of(report, layer="L1", model="mystery-model-9")[0]
    assert (row.status, row.priced_provider_nano, row.rate_card_error_pct) == \
        ("unexplained", None, None)
    assert report.unexplained_nano == 12 * w.CREDIT
    assert w.verdicts(report)["github_copilot"] == "not_reconciled"
    assert w.decisions(report)["gross_is_list:enterprise:2026-09"] == "true"


def test_scaled_gross_is_not_list() -> None:
    """A cache-free report 3% above list is evaluable (excl = incl) and fails; with cache tokens
    the same uniform error leaves the convention undecidable (totals only, never L1-gated)."""
    store, rs = w.world()
    _ingest(store, [w.row(d, read=0, write=0, scale=Decimal("1.03"))
                    for d in w.days("2026-09", 4)])
    report = w.reconcile(store, rs)
    assert w.decisions(report)["gross_is_list:enterprise:2026-09"] == "false"
    assert w.verdicts(report)["github_copilot"] == "not_reconciled"
    assert report.rate_card_error is not None and report.rate_card_error[2].startswith("2.91")
    store2, rs2 = w.world()
    _ingest(store2, [w.row(d, scale=Decimal("1.03")) for d in w.days("2026-09", 4)])
    report2 = w.reconcile(store2, rs2)
    assert w.decisions(report2)["convention:s1"] == "undecidable"
    assert w.decisions(report2)["gross_is_list:enterprise:2026-09"] == "unknown"
    assert w.verdicts(report2)["github_copilot"] == "reconciled"


def test_directional_and_preview_windows() -> None:
    store, rs = w.world()
    _ingest(store, [w.row("2026-04-10", "GPT-5.5", credits="7"),
                    w.row("2026-05-10", "GPT-5.5", credits="9")])
    report = w.reconcile(store, rs, month="2026-04", months=2)
    codes = {dict(r.key)["date"]: r.residual_code for r in w.rows_of(report, layer="L1")}
    assert codes == {"2026-04-10": "copilot_directional_report",
                     "2026-05-10": "copilot_preview_columns"}
    assert w.verdicts(report)["github_copilot"] == "reconciled"


def test_provisional_days_are_revision_window_and_closed_only_drops_them() -> None:
    store, rs = w.world()
    pairs = _base(["2026-09-01", "2026-09-02"]) + [
        w.row("2026-09-29", team="t1", scale=Decimal("1.2"), finality="provisional")]
    _ingest(store, pairs)
    report = w.reconcile(store, rs, today="2026-09-30")
    prov = w.rows_of(report, layer="L1", date="2026-09-29")
    assert [(r.status, r.residual_code) for r in prov] == [("provisional", "revision_window")]
    assert w.verdicts(report)["github_copilot"] == "reconciled"
    assert report.finality.value == "provisional"
    closed = w.reconcile(store, rs, today="2026-09-30", closed_only=True)
    assert not w.rows_of(closed, layer="L1", date="2026-09-29")


def test_rate_card_error_percentiles() -> None:
    store, rs = w.world()
    _ingest(store, [w.row(d) for d in w.days("2026-09", 4)])
    report = w.reconcile(store, rs)
    assert report.rate_card_error == ("0.0000", "0.0000", "0.0000")
    assert dict(report.effective_discount) == {"github_copilot:claude-sonnet-5:credits": "0.0000"}
