"""L0 parity with provider estimates (Appendix C.G11) and L2 token coverage (addendum §12 L0,
L2)."""

from __future__ import annotations

import dataclasses

from tokenbill.core.money import nano_aiu_to_nano

from . import world as w

G11_USAGE = {"uncached_input": 6, "cache_read": 127_386, "cache_write_unknown": 2_220,
             "output": 6_210}
G11_NANO = nano_aiu_to_nano(23_284_800_000)[0]
DATE = "2026-09-10"


def _price(req, **ctx) -> int:
    inf = req.attempts[0].inferences[0]
    if ctx:
        inf = dataclasses.replace(inf, pricing=dataclasses.replace(inf.pricing, **ctx))
    nano = w.PRICER.price_inference(inf, ts_ms=req.ts_start_ms).figure.nano
    assert nano is not None
    return nano


def _l0(provider_nano: int, model: str = "claude-opus-4-7", usage=None, **ctx):
    store, rs = w.world()
    req = w.request("lane-a", 0, DATE, usage or G11_USAGE, model, provider_nano=provider_nano,
                    **ctx)
    w.ingest(store, requests=[req], adapter="copilot-otel", source_id="otel")
    report = w.reconcile(store, rs)
    return report, w.rows_of(report, layer="L0", check="request")


def test_g11_parity_zero_mismatch_and_published_write_rate() -> None:
    assert G11_NANO == 232_848_000
    report, rows = _l0(G11_NANO)
    assert len(rows) == 1
    r = rows[0]
    assert (r.status, r.residual_code, dict(r.key)["diagnostic"]) == \
        ("match", "copilot_write_1h_price", "published rate observed")
    assert (r.ledger_nano, r.priced_provider_nano, r.rate_card_error_pct) == \
        (232_848_000, 232_848_000, "0.0000")
    assert w.residuals(report)["copilot_write_1h_price"] == 0
    assert "copilot_rate_mismatch" not in w.residuals(report)
    # L0 never decides a verdict: no report data → insufficient
    assert w.verdicts(report)["github_copilot"] == "insufficient_data"


def test_one_percent_injected_mismatch() -> None:
    report, rows = _l0(G11_NANO * 101 // 100)
    assert [(r.status, r.residual_code) for r in rows] == [("unexplained", "copilot_rate_mismatch")]
    assert w.residuals(report)["copilot_rate_mismatch"] == G11_NANO // 100
    assert report.unexplained_nano == 0     # provider estimates never enter the invoice gates


def test_write_billed_at_two_times_input() -> None:
    req = w.request("x", 0, DATE, G11_USAGE, "claude-opus-4-7")
    hour = _price(req, write_ttl_hint="1h")
    _, rows = _l0(hour)
    assert [(r.status, r.residual_code, dict(r.key)["diagnostic"]) for r in rows] == \
        [("explained", "copilot_write_1h_price", "2 x input rate observed")]


def test_auto_discount_in_or_out_of_nano_aiu() -> None:
    usage = {"uncached_input": 10_000, "cache_read": 50_000, "output": 2_000}
    req = w.request("x", 0, DATE, usage, "claude-sonnet-5", routing="auto")
    auto, direct = _price(req), _price(req, routing="direct")
    assert auto != direct
    _, rows = _l0(auto, "claude-sonnet-5", usage, routing="auto")
    assert [dict(r.key)["diagnostic"] for r in rows] == ["nano-AIU includes the Auto discount"]
    _, rows = _l0(direct, "claude-sonnet-5", usage, routing="auto")
    assert [(r.status, dict(r.key)["diagnostic"]) for r in rows] == \
        [("explained", "nano-AIU excludes the Auto discount")]


def test_band_hypotheses_near_the_threshold() -> None:
    usage = {"uncached_input": 20_000, "cache_read": 250_000, "output": 4_000}   # C.G5b
    _, rows = _l0(630_000_000, "gpt-5.5", usage, context_tier="long_context")
    assert [(r.status, r.residual_code, dict(r.key)["diagnostic"]) for r in rows] == \
        [("explained", "copilot_band_hypothesis", "hypothesis B observed")]
    report, rows = _l0(345_000_000, "gpt-5.5", usage, context_tier="long_context")
    assert [dict(r.key)["diagnostic"] for r in rows] == ["hypothesis A observed"]
    assert w.residuals(report)["copilot_band_hypothesis"] == 0


def test_unpriced_request_is_a_mismatch() -> None:
    _, rows = _l0(1_000, "claude-opus-5-5", {"uncached_input": 10, "output": 10})  # 09-10 < 09-22
    assert [(r.status, dict(r.key).get("diagnostic")) for r in rows] == [("unexplained",
                                                                           "unpriced")]


def test_conversation_totals_against_cost_state() -> None:
    store, rs = w.world()
    reqs = [w.request("lane-c", i, DATE, G11_USAGE, "claude-opus-4-7", provider_nano=G11_NANO)
            for i in range(2)]
    other = [w.request("lane-d", 0, DATE, G11_USAGE, "claude-opus-4-7", session="s_other")]
    events = [w.cost_state("lane-c", DATE, 2 * G11_NANO), w.cost_state("lane-d", DATE, 5),
              w.cost_state("lane-c", DATE, 9, reporter="gh_aw.run_total")]
    w.ingest(store, requests=[*reqs, *other], events=events, adapter="copilot-otel",
             source_id="otel")
    report = w.reconcile(store, rs)
    conv = w.rows_of(report, layer="L0", check="conversation")
    assert sorted((r.status, r.ledger_nano, r.priced_provider_nano) for r in conv) == [
        ("match", 2 * G11_NANO, 2 * G11_NANO), ("unexplained", G11_NANO, 5)]
    assert w.residuals(report)["copilot_rate_mismatch"] == 5 - G11_NANO


# ---------------------------------------------------------------------------------------------
# L2
# ---------------------------------------------------------------------------------------------

TOKENS = {"uncached": 50_000, "read": 200_000, "write": 5_000, "output": 10_000}
LEDGER_USAGE = {"uncached_input": 50_000, "cache_read": 200_000, "cache_write_unknown": 5_000,
                "output": 10_000}


def _report(store, teams=("t1",)) -> None:
    pairs = [w.row(DATE, team=t, **TOKENS) for t in teams]
    lines = [ln for ln, _ in pairs]
    w.ingest(store, lines=lines, aggs=[a for _, a in pairs] + [w.coverage("s1", DATE, lines)])


def test_same_session_from_store_rows_and_otel_spans_is_an_over_count() -> None:
    store, rs = w.world()
    _report(store)
    store_row = w.request("store-lane", 0, DATE, LEDGER_USAGE)
    span = w.request("otel-lane", 0, DATE, LEDGER_USAGE)
    w.ingest(store, requests=[store_row], adapter="copilot-cli", source_id="store")
    w.ingest(store, requests=[span], adapter="copilot-otel", source_id="otel")
    report = w.reconcile(store, rs)
    over = w.rows_of(report, layer="L2", check="token_coverage")
    assert [(r.status, r.ledger_tokens, r.provider_tokens) for r in over] == \
        [("over", 530_000, 265_000)]
    assert report.over_count_rows == 1
    assert w.verdicts(report)["github_copilot"] == "not_reconciled"
    assert report.verdict == "not_reconciled"
    assert report.token_coverage_pct == "200.0000"


def test_matching_ledger_and_unobserved_teams() -> None:
    store, rs = w.world()
    _report(store, teams=("t1", "t2"))
    w.ingest(store, requests=[w.request("l1", 0, DATE, LEDGER_USAGE, team="t1"),
                              w.request("l2", 0, DATE, {"uncached_input": 5, "output": 5},
                                        model="gpt-4o-mini", team="t1", billable=False,
                                        billing_rule_id="github.copilot.utility_unbilled")],
             adapter="copilot-otel", source_id="otel")
    report = w.reconcile(store, rs)
    cov = {dict(r.key)["team"]: r for r in w.rows_of(report, layer="L2", check="token_coverage")}
    assert (cov["t1"].status, cov["t1"].coverage_pct) == ("match", "100.0000")
    assert (cov["t2"].status, cov["t2"].residual_code) == ("explained", "unobserved_traffic")
    assert w.residuals(report)["unobserved_traffic"] == cov["t2"].invoice_nano
    util = w.rows_of(report, layer="L2", check="utility")
    assert [(r.residual_code, r.ledger_tokens) for r in util] == [("copilot_utility_unbilled", 10)]
    assert report.token_coverage_pct == "50.0000"
    assert report.dollar_coverage_pct == "50.0000"
    assert w.verdicts(report)["github_copilot"] == "reconciled"
    assert report.verdict == "reconciled"


def test_ledger_without_report_data() -> None:
    """SPEC §12.4: no channel with invoice data → ``insufficient_data``; once another channel has
    invoice data, ledger spend on a channel without it fails the overall verdict."""
    store, rs = w.world()
    w.ingest(store, requests=[w.request("l1", 0, DATE, LEDGER_USAGE)], adapter="copilot-otel",
             source_id="otel")
    report = w.reconcile(store, rs)
    assert w.verdicts(report)["github_copilot"] == "insufficient_data"
    assert report.verdict == "insufficient_data"
    assert report.finality.value == "n/a"
    w.ingest(store, lines=[w.actions(DATE), w.summary("2026-09", "actions_linux", 6 * 10**9,
                                                      channel="github_actions")],
             adapter="github-metered-usage", source_id="metered")
    report = w.reconcile(store, rs)
    assert w.verdicts(report) == {"github_copilot": "insufficient_data",
                                  "github_actions": "reconciled",
                                  "github_sandbox": "insufficient_data"}
    assert report.verdict == "not_reconciled"


def test_ledger_days_the_lagging_report_does_not_cover_are_no_over_count() -> None:
    store, rs = w.world()
    _report(store)
    w.ingest(store, requests=[w.request("l1", 0, DATE, LEDGER_USAGE),
                              w.request("l1", 1, "2026-09-28", LEDGER_USAGE)],
             adapter="copilot-otel", source_id="otel")
    report = w.reconcile(store, rs, today="2026-09-30")
    assert [dict(r.key)["date"] for r in w.rows_of(report, layer="L2")] == [DATE]
    assert report.over_count_rows == 0
    assert w.verdicts(report)["github_copilot"] == "reconciled"
