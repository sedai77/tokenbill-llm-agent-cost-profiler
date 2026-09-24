"""Addendum Appendix C.G1–G10, G14–G16 (hand-computed in the comments; int nano; 1 µ$ = 1,000
nano) with the reference FakePricer over ``facts.copilot`` and with the same pricer over the
shipped rate file (``FilePricer``) — the unit stand-in for RATES' RateCard, which the gate test in
``test_gate_rates.py`` runs on the real engine."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from tokenbill.core.labels import Basis, Evidence, Figure
from tokenbill.core.records import UsageBuckets
from tokenbill.core.testing import (
    DQ_COPILOT_BAND_HYPOTHESIS,
    DQ_COPILOT_WRITE_FOLDED,
    UNPRICED_DQ,
    FakePricer,
    assert_pricer_conforms,
)
from tokenbill.core.types import PricedInference

from .support import FilePricer, ctx, layer, ratecard, ts

G1 = UsageBuckets(uncached_input=12_000, cache_read=180_000, cache_write_unknown=6000,
                  output=3000)
G5_BIG = UsageBuckets(uncached_input=20_000, cache_read=280_000, output=4000)   # 300,000 > 272K
G5_SMALL = UsageBuckets(uncached_input=20_000, cache_read=250_000, output=4000)  # 270,000
G6 = UsageBuckets(uncached_input=2000, cache_read=6000, cache_write_unknown=2000, output=1000)
G8 = UsageBuckets(uncached_input=10_000, cache_read=90_000, output=2000)
G9 = UsageBuckets(uncached_input=100_000, cache_read=400_000, output=10_000)
C_CASES = ["C.G1", "C.G2", "C.G3", "C.G4", "C.G5", "C.G5b", "C.G6", "C.G7", "C.G8", "C.G8b",
           "C.G9", "C.G10", "C.G14", "C.G15", "C.G16"]


@pytest.fixture(params=["facts", "file", pytest.param("ratecard", marks=pytest.mark.gate)])
def pricer(request: pytest.FixtureRequest) -> Any:
    """The reference FakePricer (facts), the same over the shipped file, and — gate — RATES'
    RateCard loading the file through ``core.extensions.extension_rate_files()``."""
    if request.param == "facts":
        return FakePricer()
    return FilePricer(layer()) if request.param == "file" else ratecard()


@pytest.fixture(params=["file", pytest.param("ratecard", marks=pytest.mark.gate)])
def file_pricer(request: pytest.FixtureRequest) -> Any:
    return FilePricer(layer()) if request.param == "file" else ratecard()


def _reason(priced: PricedInference) -> str:
    reason = priced.unpriced_reason or ""
    return f"{reason} {UNPRICED_DQ.get(reason, '')}"


def price(p: Any, usage: UsageBuckets, model: str, date: str,
          **kw: Any) -> PricedInference:
    priced = p.price_usage(usage, ctx(model, **kw), ts_ms=ts(date))
    assert priced.figure.basis is Basis.LIST_EQUIVALENT  # both Copilot paths (addendum §6.2 #3)
    return priced


def rng(f: Figure) -> tuple[int | None, int | None, int | None]:
    return (f.nano, f.low_nano, f.high_nano)


def test_assert_pricer_conforms_runs_the_copilot_goldens(pricer: Any) -> None:
    summary = assert_pricer_conforms(pricer, samples=40)
    assert set(C_CASES) <= set(summary["golden_cases"])


def test_g1_opus_5_5_unknown_ttl_write_range(pricer: Any) -> None:
    # 12,000 × $4 = 48,000,000; 180,000 × $0.20 = 36,000,000; writes 6,000 × [$5; $8] =
    # [30,000,000; 48,000,000]; 3,000 × $20 = 60,000,000 → point 174,000,000 (5m), high 192M
    p = price(pricer, G1, "claude-opus-5-5", "2026-09-23")
    assert rng(p.figure) == (174_000_000, 174_000_000, 192_000_000)
    assert p.figure.evidence is Evidence.ESTIMATED and p.exact_nano == 144_000_000
    assert price(pricer, G1, "claude-opus-5-5", "2026-09-23",
                 write_ttl_hint="1h").figure.nano == 192_000_000
    assert price(pricer, G1, "claude-opus-5-5", "2026-09-23",
                 billing_path="copilot_direct").figure == p.figure  # C.G13: both paths alike


def test_g2_to_g4_auto_and_compliance_modifiers(pricer: Any) -> None:
    # ×0.9: 174M → 156,600,000, high 192M × 0.9 = 172,800,000
    g2 = price(pricer, G1, "claude-opus-5-5", "2026-09-23", routing="auto").figure
    assert rng(g2) == (156_600_000, 156_600_000, 172_800_000)
    # ×1.1: 191,400,000, high 211,200,000
    g3 = price(pricer, G1, "claude-opus-5-5", "2026-09-23", compliance="data_residency").figure
    assert (g3.nano, g3.high_nano) == (191_400_000, 211_200_000)
    # ×0.99 (stacking assumed): 47,520,000 + 35,640,000 + [29,700,000; 47,520,000] + 59,400,000
    g4 = price(pricer, G1, "claude-opus-5-5", "2026-09-23", routing="auto",
               compliance="fedramp")
    assert (g4.figure.nano, g4.figure.high_nano) == (172_260_000, 190_080_000)
    assert {ln.bucket: ln.amount_nano for ln in g4.lines} == {
        "uncached_input": 47_520_000, "cache_read": 35_640_000, "cache_write_unknown": 29_700_000,
        "output": 59_400_000}
    assert all(set(ln.modifier_ids) == {"github.auto", "github.compliance"} for ln in g4.lines)


def test_g5_long_context_band_exact_with_unknown_tier(pricer: Any) -> None:
    # 300,000 > 272,000 → band $10/$1/$45: 200,000,000 + 280,000,000 + 180,000,000
    big = price(pricer, G5_BIG, "gpt-5.5", "2026-09-10").figure
    assert big.nano == 660_000_000 and big.evidence is Evidence.EXACT
    # 270,000 ≤ 272,000 → $5/$0.50/$30: 100,000,000 + 125,000,000 + 120,000,000
    small = price(pricer, G5_SMALL, "gpt-5.5", "2026-09-10").figure
    assert small.nano == 345_000_000 and small.evidence is Evidence.EXACT


def test_g5b_band_hypotheses_disagree_range(pricer: Any) -> None:
    # hypothesis B (session tier long_context) → band: 200,000,000 + 250,000,000 + 180,000,000
    g5b = price(pricer, G5_SMALL, "gpt-5.5", "2026-09-10", context_tier="long_context").figure
    assert rng(g5b) == (345_000_000, 345_000_000, 630_000_000)
    assert g5b.evidence is Evidence.ESTIMATED
    if isinstance(pricer, FakePricer):  # the fake names the dq code in the figure note
        assert DQ_COPILOT_BAND_HYPOTHESIS in (g5b.note or "")
    agree = price(pricer, G5_SMALL, "gpt-5.5", "2026-09-10", context_tier="default").figure
    assert agree.nano == 345_000_000 and agree.evidence is Evidence.EXACT


def test_g6_g7_gpt_5_6_sol_rows_by_date(pricer: Any) -> None:
    # 2026-09-10 ($4/$0.40/$5/$20): 8,000,000 + 2,400,000 + 10,000,000 + 20,000,000; zero-width
    g6 = price(pricer, G6, "gpt-5.6-sol", "2026-09-10").figure
    assert rng(g6) == (40_400_000, 40_400_000, 40_400_000) and g6.evidence is Evidence.ESTIMATED
    # 2026-08-25, promo ($2/$0.20/$2.50/$10): 4,000,000 + 1,200,000 + 5,000,000 + 10,000,000
    assert price(pricer, G6, "gpt-5.6-sol", "2026-08-25").figure.nano == 20_200_000
    # 2026-08-20 ($2.50/$0.25/$3.125/$15): 5,000,000 + 1,500,000 + 6,250,000 + 15,000,000
    assert price(pricer, G6, "gpt-5.6-sol", "2026-08-20").figure.nano == 27_750_000


def test_g8_fast_mode_premium(pricer: Any) -> None:
    # fast $10/$1/$50: 100,000,000 + 90,000,000 + 100,000,000; standard half of it
    fast = price(pricer, G8, "claude-opus-4-8", "2026-09-23", speed="fast").figure
    std = price(pricer, G8, "claude-opus-4-8", "2026-09-23").figure
    assert (fast.nano, std.nano) == (290_000_000, 145_000_000)
    assert fast.evidence is std.evidence is Evidence.EXACT
    assert fast.nano - std.nano == 145_000_000  # premium, EXACT
    g8b = dataclasses.replace(G8, cache_write_unknown=5000)
    # fast writes [62,500,000; 100,000,000], standard [31,250,000; 50,000,000]
    fb = rng(price(pricer, g8b, "claude-opus-4-8", "2026-09-23", speed="fast").figure)
    sb = rng(price(pricer, g8b, "claude-opus-4-8", "2026-09-23").figure)
    assert tuple((a or 0) - (b or 0) for a, b in zip(fb, sb, strict=True)) == (
        176_250_000, 176_250_000, 195_000_000)


def test_g9_promotional_row_expires(pricer: Any) -> None:
    # $0.75/$0.075/$3.75: 75,000,000 + 30,000,000 + 37,500,000
    assert price(pricer, G9, "gemini-3.8-flash", "2026-09-23").figure.nano == 142_500_000
    expired = price(pricer, G9, "gemini-3.8-flash", "2027-01-01")
    assert expired.figure.nano is None and "promotion" in _reason(expired)


def test_g10_g14_bands_and_reads(pricer: Any) -> None:
    # Grok 4.7, 210,000 > 200,000 → $4/$1/$12: 200,000,000 + 160,000,000 + 60,000,000
    g10 = price(pricer, UsageBuckets(uncached_input=50_000, cache_read=160_000, output=5000),
                "grok-4.7", "2026-09-23").figure
    assert g10.nano == 420_000_000 and g10.evidence is Evidence.EXACT
    # Kimi K2.7 Code read 1,000,000 × $0.19
    g14 = price(pricer, UsageBuckets(cache_read=1_000_000), "kimi-k2.7-code", "2026-09-23").figure
    assert g14.nano == 190_000_000 and g14.evidence is Evidence.EXACT


def test_g15_writes_folded_to_input(pricer: Any) -> None:
    # GPT-5.3-Codex has no write price: 1,000 × $1.75 = 1,750,000, zero-width ESTIMATED
    g15 = price(pricer, UsageBuckets(cache_write_unknown=1000), "gpt-5.3-codex",
                "2026-09-23").figure
    assert rng(g15) == (1_750_000, 1_750_000, 1_750_000) and g15.evidence is Evidence.ESTIMATED
    # the GPT-5.6 window without a write price folds too (write bucket disabled 07-30 → 08-03)
    luna = price(pricer, UsageBuckets(cache_write_5m=1000), "gpt-5.6-luna", "2026-08-01").figure
    assert luna.nano == 200_000 and luna.evidence is Evidence.ESTIMATED
    if isinstance(pricer, FakePricer):
        assert DQ_COPILOT_WRITE_FOLDED in (g15.note or "") and DQ_COPILOT_WRITE_FOLDED in (
            luna.note or "")


def test_g16_before_the_changelog_date(pricer: Any) -> None:
    g16 = price(pricer, G1, "claude-opus-5-5", "2026-09-21")
    assert g16.figure.nano is None and "effective" in _reason(g16)


def test_closed_rows_of_the_file_price_inside_their_interval(file_pricer: Any) -> None:
    p = file_pricer
    one_m = UsageBuckets(uncached_input=1_000_000)
    assert price(p, one_m, "raptor-mini", "2026-08-01").figure.nano == 250_000_000
    assert price(p, one_m, "gpt-4.1", "2026-06-02").figure.nano == 2_000_000_000
    gone = price(p, one_m, "raptor-mini", "2026-09-04")
    assert gone.figure.nano is None and gone.unpriced_reason
    assert price(p, one_m, "gemini-3.6-flash", "2026-08-12").figure.nano == 1_500_000_000
    assert price(p, one_m, "gemini-3.6-flash", "2026-08-13").figure.nano == 750_000_000
    band = UsageBuckets(uncached_input=250_000)  # > 200K on Gemini 3.1 Pro's band
    assert price(p, band, "gemini-3.1-pro", "2026-07-01").figure.nano == 1_000_000_000
    assert price(p, band, "gemini-3.1-pro", "2026-06-02").figure.nano == 500_000_000  # no band
    # the facts-only pricer does not carry these closed rows (R-E34)
    assert price(FakePricer(), one_m, "raptor-mini", "2026-08-01").figure.nano is None
