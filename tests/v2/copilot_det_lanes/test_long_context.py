"""``long-context-band`` (addendum §10.3, §6.2 #4; Appendix C.G5 / G5b): the band premium over
default-tier rates on identical tokens, through the pricer."""

from __future__ import annotations

from tokenbill.core.findings import COPILOT_TITLE_PREFIX
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import InferenceKind
from tokenbill.detect.copilot_lanes import band_premium

from .helpers import (
    CLI,
    DAY,
    EVENTS_ONLY_CAPS,
    GPT55,
    PRICER,
    SONNET5,
    T0,
    VSCODE,
    attr,
    attrs,
    compaction_inf,
    ctx,
    lane,
    one,
    only,
    req,
    run,
    tri,
)

# Appendix C.G5: GPT-5.5 on 2026-09-10, uncached 20,000, read 280,000 (total 300,000 > 272,000),
# output 4,000 → band 660,000,000; default tier on the same tokens 360,000,000.
G5 = dict(u=20_000, r=280_000, o=4_000)
# Appendix C.G5b: read 250,000 (total 270,000) with context_tier long_context → range
# [345,000,000; 630,000,000] point 345,000,000; default 345,000,000.
G5B = dict(u=20_000, r=250_000, o=4_000)


def _serving(request):
    inf = request.serving_inference
    assert inf is not None
    return inf


def test_day_is_the_fixture_day() -> None:
    assert DAY == "2026-09-10" and T0 % 1000 == 0


def test_band_premium_c_g5_exact() -> None:
    r = req("L", 0, 0, **G5)
    assert PRICER.price_inference(_serving(r), ts_ms=T0).figure.nano == 660_000_000
    prem = band_premium(PRICER, _serving(r), T0)
    assert prem is not None
    assert (prem.low, prem.point, prem.high) == (300_000_000, 300_000_000, 300_000_000)
    assert prem.exact and not prem.tier_known


def test_band_premium_c_g5b_range() -> None:
    r = req("L", 0, 0, context_tier="long_context", **G5B)
    fig = PRICER.price_inference(_serving(r), ts_ms=T0).figure
    assert tri(fig) == (345_000_000, 345_000_000, 630_000_000)
    prem = band_premium(PRICER, _serving(r), T0)
    assert prem is not None
    assert (prem.low, prem.point, prem.high) == (0, 0, 285_000_000)
    assert not prem.exact and prem.tier_known


def test_band_premium_tier_default_disagrees_with_a() -> None:
    r = req("L", 0, 0, context_tier="default", **G5)
    prem = band_premium(PRICER, _serving(r), T0)
    assert prem is not None and (prem.low, prem.point, prem.high) == (0, 300_000_000,
                                                                      300_000_000)


def test_band_premium_zero_below_threshold_and_agreeing_tier() -> None:
    below = band_premium(PRICER, _serving(req("L", 0, 0, **G5B)), T0)
    assert below is not None and (below.low, below.point, below.high) == (0, 0, 0)
    agree = band_premium(PRICER, _serving(req("L", 0, 0, context_tier="long_context", **G5)),
                         T0)
    assert agree is not None and agree.exact and agree.point == 300_000_000


def test_band_premium_auto_routing_scales_both_sides() -> None:
    prem = band_premium(PRICER, _serving(req("L", 0, 0, routing="auto", **G5)), T0)
    assert prem is not None and prem.point == 270_000_000 and prem.exact


def test_band_premium_skips_unbillable_partial_and_unpriced() -> None:
    assert band_premium(PRICER, _serving(req("L", 0, 0, billable=None, **G5)), T0) is None
    assert band_premium(PRICER, _serving(req("L", 0, 0, billable=False, **G5)), T0) is None
    assert band_premium(PRICER, _serving(req("L", 0, 0, usage_source="partial_stream", **G5)),
                        T0) is None
    assert band_premium(PRICER, _serving(req("L", 0, 0, model="gpt-unknown", **G5)), T0) is None


def test_band_premium_folded_writes_and_non_band_model() -> None:
    # GPT-5.5 has no write price: writes fold into input (zero-width ESTIMATED lines) at the
    # band input rate vs the default input rate → +5,000 nano per written token
    prem = band_premium(PRICER, _serving(req("L", 0, 0, u=20_000, r=270_000, w=10_000,
                                             o=4_000)), T0)
    assert prem is not None and not prem.exact
    assert prem.point == prem.low == prem.high == 20_000 * 5_000 + 270_000 * 500 + \
        10_000 * 5_000 + 4_000 * 15_000
    # a Claude row has no band: unknown-TTL write ranges cancel against the default side
    claude = band_premium(PRICER, _serving(req("L", 0, 0, u=20_000, r=280_000, w=5_000,
                                               o=4_000, model=SONNET5,
                                               write_ttl_hint="1h")), T0)
    assert claude is not None and (claude.low, claude.point, claude.high) == (0, 0, 0)


def test_vscode_lane_g5_finding_exact_reach_as_evidence() -> None:
    vs = lane("vs-1", [req("vs-1", 0, 0, **G5), req("vs-1", 1, 60, **G5B)])
    f = one(run([vs], ctx(min_usd="0.10")), "long-context-band")
    assert dict(f.scope.dims) == {"team": "payments", "lane_kind": "main", "billing_class": "pool",
                                  "product": "copilot", "model": GPT55}
    assert f.cost_observed.nano == 300_000_000 and f.cost_observed.evidence is Evidence.EXACT
    assert f.cost_observed.basis is Basis.LIST_EQUIVALENT
    rec = f.recoverable                  # the standalone counterfactual, an upper bound
    assert rec is not None and rec.upper_bound and rec.evidence is Evidence.ESTIMATED
    assert tri(rec) == (None, 300_000_000, None) and rec.basis is Basis.LIST_EQUIVALENT
    assert f.headroom is None                # lane findings carry no pool conversion
    reach = attrs(f, "reach:copilot.context_default")       # contextTier does not reach VS Code
    assert (reach["reached_lanes"], reach["reached_nano"], reach["reached_share_pct"]) == \
        (0, 0, "0.0")
    assert f.title.startswith(COPILOT_TITLE_PREFIX)
    assert "list-equivalent AI-credit value" in f.summary and "Copilot CLI" in f.summary
    assert f.category == "lever" and f.lever_ids == ("copilot.context_default",)
    assert f.lever_class == "trajectory" and f.needs_eval and f.confidence == "high"
    assert f.fix is not None and f.fix.target == "github-copilot"
    assert f.fix.config_patch == (("copilot.repo.contextTier", '"default"'),)
    assert attrs(f, f"band:{GPT55}")["inferences"] == 1
    assert (f.n_events, f.n_lanes, f.n_users) == (1, 1, 1)
    assert f.first_seen_ms == T0


def test_cli_lane_g5b_range_and_reach() -> None:
    a = attr(product=CLI)
    cli = lane("cli-1", [req("cli-1", 0, 0, a=a, context_tier="long_context", **G5B)])
    f = one(run([cli], ctx(min_usd="0")), "long-context-band")
    assert tri(f.cost_observed) == (0, 0, 285_000_000)
    assert f.cost_observed.evidence is Evidence.ESTIMATED and f.confidence == "medium"
    rec = f.recoverable
    assert rec is not None and rec.upper_bound and rec.evidence is Evidence.ESTIMATED
    assert tri(rec) == (0, 0, 285_000_000) and rec.basis is Basis.LIST_EQUIVALENT
    assert attrs(f, f"band:{GPT55}")["ranged"] == 1
    reach = attrs(f, "reach:copilot.context_default")
    assert (reach["reached_lanes"], reach["reached_high_nano"], reach["reached_share_pct"]) == \
        (1, 285_000_000, "100.0")
    assert "reaches only the Copilot CLI" not in f.summary
    # the default min_usd drops a range whose point (hypothesis A) is 0
    assert only(run([cli], ctx()), "long-context-band") == []


def test_cli_lane_g5_recoverable_upper_bound() -> None:
    a = attr(product=CLI)
    cli = lane("cli-1", [req("cli-1", 0, 0, a=a, **G5), req("cli-1", 1, 60, a=a, **G5),
                         req("cli-1", 2, 120, a=a, **G5), req("cli-1", 3, 180, a=a, **G5)])
    f = one(run([cli]), "long-context-band")
    assert f.cost_observed.nano == 1_200_000_000 and f.cost_observed.evidence is Evidence.EXACT
    assert tri(f.recoverable) == (None, 1_200_000_000, None) and f.recoverable.upper_bound


def test_mixed_cohort_reach_only_cli_lanes_and_mixed_evidence() -> None:
    vs = lane("vs-1", [req("vs-1", 0, 0, **G5)])
    cli = lane("cli-1", [req("cli-1", 0, 0, a=attr(product=CLI), context_tier="long_context",
                             **G5B)])
    f = one(run([vs, cli], ctx(min_usd="0.10")), "long-context-band")
    assert tri(f.cost_observed) == (300_000_000, 300_000_000, 585_000_000)
    assert f.cost_observed.evidence is Evidence.ESTIMATED
    assert tri(f.recoverable) == (300_000_000, 300_000_000, 585_000_000)
    reach = attrs(f, "reach:copilot.context_default")
    assert (reach["reached_lanes"], reach["lanes"], reach["trusted_share"]) == (1, 2, "unknown")
    assert (reach["reached_nano"], reach["reached_high_nano"]) == (0, 285_000_000)
    assert reach["reached_share_pct"] == "48.7"          # 285 / 585


def test_events_only_cli_lane_is_skipped_for_the_band() -> None:
    # output-only requests (events-only CLI): no serving inference carries input, so the band is
    # not analysed even though a COMPACTION inference is above the threshold
    a = attr(product=CLI)
    big = compaction_inf("cmp-1", u=20_000, r=280_000, o=4_000, model=GPT55)
    cli = lane("cli-ev", [req("cli-ev", 0, 0, o=500, a=a), req("cli-ev", 1, 60, o=500, a=a,
                                                               extra=(big,))])
    c = ctx(min_usd="0.01", caps=EVENTS_ONLY_CAPS)
    assert only(run([cli], c), "long-context-band") == []
    assert big.kind is InferenceKind.COMPACTION


def test_band_counts_every_billable_inference_of_an_input_lane() -> None:
    comp = compaction_inf("cmp-2", u=20_000, r=280_000, o=4_000, model=GPT55)
    vs = lane("vs-2", [req("vs-2", 0, 0, u=1_000, o=10, extra=(comp,))])
    f = one(run([vs], ctx(min_usd="0.10")), "long-context-band")
    assert f.cost_observed.nano == 300_000_000 and f.n_events == 1


def test_non_band_models_and_small_requests_give_no_finding() -> None:
    vs = lane("vs-3", [req("vs-3", 0, 0, model=SONNET5, u=20_000, r=280_000, o=4_000),
                       req("vs-3", 1, 60, **G5B)])
    assert only(run([vs], ctx(min_usd="0")), "long-context-band") == []


def test_one_finding_per_model() -> None:
    vs = lane("vs-4", [req("vs-4", 0, 0, **G5),
                       req("vs-4", 1, 60, model="grok-4.6", u=50_000, r=160_000, o=5_000)])
    found = only(run([vs], ctx(min_usd="0.10")), "long-context-band")
    assert {dict(f.scope.dims)["model"] for f in found} == {GPT55, "grok-4.6"}
    grok = one(found, "long-context-band", model="grok-4.6")
    # C.G10 (Grok 4.7 rates; 4.6 is the row on this day): 420,000,000 at band rates vs
    # 50,000×2,000 + 160,000×500 + 5,000×6,000 = 210,000,000 at default rates
    assert grok.cost_observed.nano == 210_000_000


def test_unpriced_band_inference_is_ignored() -> None:
    vs = lane("vs-5", [req("vs-5", 0, 0, model="gpt-unknown", **G5)])
    assert only(run([vs], ctx(min_usd="0")), "long-context-band") == []


def test_vscode_product_constant() -> None:
    assert VSCODE == "copilot_vscode"
