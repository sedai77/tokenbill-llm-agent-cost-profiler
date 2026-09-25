"""SPEC §10.3 acceptance tests of ``recon.orgscan.OrgScan`` on hand-built aggregates."""

from __future__ import annotations

import dataclasses
from decimal import Decimal
from typing import Any

from tokenbill.core import registry
from tokenbill.core.builders import make_lane, make_request, make_usage
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.evidence import CACHE_READ_SHARE_MEDIAN, CACHE_READ_SHARE_TOP_DECILE
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.money import token_nano
from tokenbill.core.testing import FakePricer, assert_detector_conforms
from tokenbill.core.types import AnalysisContext
from tokenbill.recon.orgscan import KINDS, OrgScan

from .helpers import DAY, DAY_MS, OPUS, SONNET, TODAY, WS, WS2, agg, cost_lines, ms, priced_buckets

WINDOW = (ms("2026-08-01"), ms("2026-09-01"))


def _ctx(aggs: list, lines: list | None = None, pricer: Any = None, **kw: Any) -> AnalysisContext:
    return AnalysisContext(pricer=pricer or FakePricer(), rules=RulesTable(), replayer=None,
                           calibration=None, window=WINDOW,
                           capabilities=frozenset({"aggregates"}), aggregates=tuple(aggs),
                           cost_lines=tuple(lines or ()), **kw)


def _by_kind(findings: list) -> dict[str, list]:
    out: dict[str, list] = {}
    for f in findings:
        out.setdefault(f.kind, []).append(f)
    return out


def _share(read_share: str, total: int = 10_000_000) -> dict[str, int]:
    reads = int(Decimal(total) * Decimal(read_share))
    return {"cache_read": reads, "uncached_input": total - reads, "output": 500_000}


def test_detector_surface_and_registry_entry() -> None:
    det = OrgScan()
    assert det.id == "aggregate.org-scan" and det.requires == frozenset({"aggregates"})
    assert det.aggregate is True and det.kinds == KINDS
    assert registry.load(registry.BUILTIN_DETECTORS["aggregate.org-scan"]) is OrgScan


def test_cache_read_share_below_benchmark_has_sources() -> None:
    findings = OrgScan().detect([], _ctx([agg(_share("0.60"))]))
    (f,) = _by_kind(findings)["cache-read-share"]
    attrs = dict(f.evidence[0].attrs)
    assert attrs["read_share"] == "0.6"
    assert attrs["benchmark_median"] == "0.84" and attrs["benchmark_top_decile"] == "0.94"
    assert attrs["source"] == CACHE_READ_SHARE_MEDIAN.source_url
    assert attrs["source_top_decile"] == CACHE_READ_SHARE_TOP_DECILE.source_url
    assert CACHE_READ_SHARE_MEDIAN.source_url in f.summary
    assert CACHE_READ_SHARE_MEDIAN.finding_id in f.references
    assert dict(f.scope.dims) == {"channel": "anthropic_api", "model": OPUS, "workspace_id": WS}
    assert f.cost_observed.evidence is Evidence.EXACT
    assert f.cost_observed.nano == sum(priced_buckets(agg(_share("0.60"))).values())
    # (0.84·T − R)·(w5 − r) at Opus 5 rates: 2.4M tokens × ($6.25 − $0.50)/MTok
    assert f.recoverable is not None and f.recoverable.upper_bound
    assert f.recoverable.nano == token_nano(2_400_000, Decimal("5.75"))
    assert f.recoverable.evidence is Evidence.ESTIMATED
    assert f.n_users == 0 and f.audience == "org" and f.category == "aggregate"


def test_cache_read_share_above_threshold_has_no_finding() -> None:
    findings = OrgScan().detect([], _ctx([agg(_share("0.90"))]))
    assert "cache-read-share" not in _by_kind(findings)


def test_threshold_override() -> None:
    ctx = _ctx([agg(_share("0.90"))], thresholds={"aggregate.org-scan.read_share_below": "0.95"})
    assert "cache-read-share" in _by_kind(OrgScan().detect([], ctx))


def test_fast_geo_priority_premiums_exact_to_the_nano() -> None:
    usage = {"uncached_input": 1_000_000, "output": 100_000}
    fast = agg(usage, speed="fast")
    geo = agg({k: 10 * v for k, v in usage.items()}, model=SONNET, ws=WS2, inference_geo="us")
    findings = _by_kind(OrgScan().detect([], _ctx([fast, geo])))
    (f,) = findings["fast-premium"]
    # Opus 5 fast base $10/$50 vs $5/$25: 1M × $5 + 100k × $25
    assert f.cost_observed.nano == f.recoverable.nano == 5_000_000_000 + 2_500_000_000
    assert f.cost_observed.evidence is Evidence.EXACT and f.lever_class == "rate"
    assert f.lever_ids == ("cc.fast_mode_opt_in",)
    (g,) = findings["geo-premium"]
    # Sonnet 5 US geo ×1.1: 10% of 10M × $2 + 1M × $10
    assert g.cost_observed.nano == 2_000_000_000 + 1_000_000_000
    assert g.lever_ids == ("geo.global",)


class _PriorityPricer(FakePricer):
    """FakePricer with a Priority Tier price of 2× standard (the fake has no priority modifier)."""

    def price_usage(self, usage: Any, ctx: Any, **kw: Any) -> Any:
        if ctx.service_tier == "priority":
            doubled = make_usage(**{k: 2 * getattr(usage, k) for k in
                                    ("uncached_input", "output")})
            return super().price_usage(doubled, dataclasses.replace(ctx, service_tier="standard"),
                                       **kw)
        return super().price_usage(usage, ctx, **kw)


def test_priority_share_premium_exact() -> None:
    usage = {"uncached_input": 1_000_000, "output": 100_000}
    findings = _by_kind(OrgScan().detect([], _ctx([agg(usage, tier="priority")],
                                                  pricer=_PriorityPricer())))
    (f,) = findings["priority-share"]
    assert f.cost_observed.nano == 5_000_000_000 + 2_500_000_000
    assert "of spend" in f.summary


def test_write_read_thrash_and_ttl_mix() -> None:
    thrash = agg({"uncached_input": 100_000, "cache_write_5m": 5_000_000,
                  "cache_read": 1_000_000})
    one_hour = agg({"uncached_input": 100_000, "cache_write_1h": 3_000_000,
                    "cache_read": 10_000_000}, ws=WS2)
    findings = _by_kind(OrgScan().detect([], _ctx([thrash, one_hour])))
    (w,) = findings["write-read-thrash"]
    assert w.recoverable is not None and w.recoverable.nano == token_nano(5_000_000,
                                                                          Decimal("5.75"))
    assert w.cost_observed.nano == token_nano(5_000_000, Decimal("6.25"))
    (t,) = findings["ttl-mix"]
    assert dict(t.scope.dims)["workspace_id"] == WS2 and t.recoverable is None


def test_batch_share() -> None:
    usage = {"uncached_input": 2_000_000, "output": 200_000}
    findings = _by_kind(OrgScan().detect([], _ctx([agg(usage, tier="batch"), agg(usage)])))
    (b,) = findings["batch-share"]
    assert b.cost_observed.nano == (2_000_000 * 5 + 200_000 * 25) * 1000 // 2
    assert b.summary.startswith("33.3%") and b.lever_ids == ("batch.eligible",)


def test_effective_discount_from_reconciliation_inputs() -> None:
    a = agg()
    lines = cost_lines(a, factor=Decimal("0.85"))
    findings = _by_kind(OrgScan().detect([], _ctx([a], lines)))
    (d,) = findings["effective-discount"]
    assert "0.15" in d.title
    assert d.cost_observed.basis is Basis.INVOICE
    assert d.cost_observed.nano == sum(c.amount_nano for c in lines if c.model == OPUS)
    assert dict(d.evidence[0].attrs)["discount"] == "0.15"
    none = _by_kind(OrgScan().detect([], _ctx([a], cost_lines(a))))
    assert "effective-discount" not in none


def test_min_usd_gate_and_window() -> None:
    tiny = agg({"uncached_input": 1000, "cache_read": 10}, speed="fast")
    assert OrgScan().detect([], _ctx([tiny])) == []
    outside = agg(_share("0.5"), date="2026-09-15")
    assert OrgScan().detect([], _ctx([outside])) == []


def test_delegated_channels_and_unpriced_models_are_skipped() -> None:
    copilot = agg(_share("0.5"), channel="github_copilot", source_kind="github.ai_usage_report")
    unknown = agg(_share("0.5"), model="claude-unknown-9")
    assert OrgScan().detect([], _ctx([copilot, unknown])) == []


def test_api_key_scope_needs_break_glass() -> None:
    a = agg(_share("0.5"), api_key_id="h_" + "b" * 20)
    plain = OrgScan().detect([], _ctx([a], thresholds={"aggregate.org-scan.scope": "api_key"}))
    assert all("api_key_id" not in dict(f.scope.dims) for f in plain)
    keyed = OrgScan().detect([], _ctx([a], thresholds={"aggregate.org-scan.scope": "api_key"},
                                      break_glass="incident-42"))
    assert keyed and all(dict(f.scope.dims).get("api_key_id") == "h_" + "b" * 20
                         for f in keyed if f.kind != "effective-discount")


def test_conforms_and_is_lane_independent() -> None:
    aggs = [agg(_share("0.6")), agg({"uncached_input": 1_000_000, "output": 100_000},
                                    speed="fast", ws=WS2),
            agg({"uncached_input": 1_000_000}, tier="batch", model=SONNET)]
    lines = cost_lines(aggs[0], factor=Decimal("0.9"))
    lane = make_lane([make_request("L1", 0, ms(DAY) + 1000, {"uncached_input": 5000},
                                   OPUS)])
    ctx = _ctx(aggs, lines)
    findings = assert_detector_conforms(OrgScan(), [lane], ctx)
    assert {f.kind for f in findings} >= {"cache-read-share", "fast-premium", "batch-share",
                                          "effective-discount"}


def test_run_detectors_runs_it_once_per_run() -> None:
    ctx = _ctx([agg(_share("0.6"))])
    once = registry.run_detectors([], ctx, only=["aggregate.org-scan"], aggregates_only=True)
    per_shard = registry.run_detectors([], ctx, only=["aggregate.org-scan"],
                                       aggregates_only=False)
    assert once and per_shard == []
    missing = registry.run_detectors([], dataclasses.replace(ctx, capabilities=frozenset()),
                                     only=["aggregate.org-scan"])
    assert [f.kind for f in missing] == ["missing-capabilities"]


def test_today_constant_is_after_the_window() -> None:
    assert ms(TODAY) > WINDOW[1] - DAY_MS
