"""Edge cases: a pricer without unit rates, foreign records mixed into the ledger, closed-only
filters on every layer, window clamping and unmatched summary SKUs."""

from __future__ import annotations

import dataclasses

from tokenbill.copilot import recon
from tokenbill.core.builders import make_request
from tokenbill.core.records import PricingContext
from tokenbill.core.types import UnitRates

from . import world as w

USD = 10**9


class ResolveOnly:
    """FakePricer without unit rates (a card whose scale exceeds 24 digits takes this path)."""

    def __init__(self) -> None:
        self._inner = w.PRICER
        self.rate_card_sha256 = "resolve-only"
        self.basis = self._inner.basis

    def unit_rates(self, ctx: PricingContext, *, ts_ms: int) -> UnitRates | None:
        return None

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def test_resolve_fallback_prices_like_unit_rates() -> None:
    store, rs = w.world()
    pairs = [w.row(d, m) for d in w.days("2026-09", 3)
             for m in ("Claude Sonnet 5", "GPT-5.5", "Claude Opus 4.8 (fast mode)")]
    lines = [ln for ln, _ in pairs]
    w.ingest(store, lines=lines, aggs=[a for _, a in pairs])
    unit = w.reconcile(store, rs)
    fallback = w.reconcile(store, rs, pricer=ResolveOnly())
    assert [r.priced_provider_nano for r in w.rows_of(unit, layer="L1")] == \
        [r.priced_provider_nano for r in w.rows_of(fallback, layer="L1")]
    assert {r.status for r in w.rows_of(fallback, layer="L1")} == {"match"}


def test_fallback_rate_buckets() -> None:
    rates = w.PRICER.resolve(w.list_ctx("GPT-5.5"), ts_ms=w.day_ms("2026-09-10"))
    assert rates is not None and rates.cache_write_5m is None
    assert recon._fallback_rate(rates, "cache_write_unknown") == rates.input
    assert recon._fallback_rate(rates, "cache_write_1h") == rates.input
    assert recon._fallback_rate(rates, "cache_write_other") == rates.input
    no_read = dataclasses.replace(rates, cache_read=None)
    assert recon._fallback_rate(no_read, "cache_read") == rates.input
    sol = w.PRICER.resolve(w.list_ctx("GPT-5.6 Sol"), ts_ms=w.day_ms("2026-09-10"))
    assert sol is not None
    assert recon._fallback_rate(sol, "cache_write_other") == sol.cache_write_5m


def test_foreign_records_and_closed_only_filters() -> None:
    store, rs = w.world()
    pairs = [w.row("2026-09-02"), w.row("2026-09-29", finality="provisional", team="t2"),
             w.row("2026-09-29", "Code Review", credits="4", finality="provisional", team="t3")]
    lines = [ln for ln, _ in pairs]
    foreign_cov = dataclasses.replace(w.coverage("s_x", "2026-09-02", lines),
                                      dims=(("channel", "anthropic_api"), ("source", "s_x")))
    empty_cov = dataclasses.replace(w.coverage("s_y", "2026-09-03", lines),
                                    reported_cost_nano=None)
    w.ingest(store, lines=lines, aggs=[a for _, a in pairs] + [foreign_cov, empty_cov])
    claude = make_request("cc-lane", 0, w.day_ms("2026-09-02") + 5, {"uncached_input": 10},
                          session_key="s_claude")
    w.ingest(store, requests=[claude, w.request("l", 0, "2026-09-29", {"uncached_input": 5},
                                                team="t2")],
             events=[w.cost_state("cc-lane", "2026-09-02", 7)], adapter="copilot-otel",
             source_id="otel")
    w.ingest(store, lines=[
        w.actions("2026-09-29", finality="provisional"),
        w.seats("business", "3", finality="provisional"),
        w.seats("business", "2", sku="copilot_mystery_seat"),
        w.summary("2026-09", "mystery_sku", 5 * USD),
        w.summary("2026-09", "actions_macos", 5 * USD, channel="github_actions")],
        adapter="github-metered-usage", source_id="m")
    report = w.reconcile(store, rs, today="2026-09-30", closed_only=True)
    assert all(dict(r.key).get("date") != "2026-09-29" for r in report.rows)
    assert not w.rows_of(report, check="seats") and not w.rows_of(report, check="runner_rate")
    assert not w.rows_of(report, layer="L0", check="conversation")
    assert all(dict(r.key)["team"] == "t1" for r in w.rows_of(report, layer="L2"))
    full = w.reconcile(store, rs, today="2026-10-20")
    unmatched = w.rows_of(full, check="summary", product="actions", sku="linux")
    assert [(r.status, r.invoice_nano) for r in unmatched] == [("unexplained", None)]
    assert w.verdicts(full)["github_actions"] == "not_reconciled"
    assert [dict(r.key)["plan"] for r in w.rows_of(full, check="seats")] == ["business"]


def test_plain_l0_match_and_window_clamp() -> None:
    store, rs = w.world()
    req = w.request("l", 0, "2026-09-10", {"uncached_input": 1_000, "output": 1_000})
    point = w.PRICER.price_inference(req.attempts[0].inferences[0], ts_ms=req.ts_start_ms)
    matched = w.request("l", 1, "2026-09-10", {"uncached_input": 1_000, "output": 1_000},
                        provider_nano=point.figure.nano)
    w.ingest(store, requests=[req, matched], adapter="copilot-otel", source_id="otel")
    report = recon.reconcile_copilot(store, [rs], w.PRICER, since_ms=w.day_ms("2026-09-01"),
                                     until_ms=2**53, today="2026-10-20")
    rows = w.rows_of(report, layer="L0", check="request")
    assert [(r.status, r.residual_code, "diagnostic" in dict(r.key)) for r in rows] == \
        [("match", None, False)]
    assert report.window == ("2026-09-01", "9999-12-31")
    assert report.tolerance_pct == "0.5" and report.unexplained_tolerance_pct == "1.0"
