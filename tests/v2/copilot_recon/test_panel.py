"""Copilot verification panels at constant prices (addendum §13, SPEC §13.1, R8)."""

from __future__ import annotations

from collections import defaultdict

import pytest

from tokenbill.copilot.panel import build_copilot_panel
from tokenbill.core import extensions
from tokenbill.core.builders import make_activity, make_license
from tokenbill.core.errors import UsageError
from tokenbill.core.protocols import Pricer
from tokenbill.core.records import Inference, PricingContext, UsageBuckets, UsageSource
from tokenbill.core.types import PanelRow, PricedInference, ResolvedRates, UnitRates

from . import world as w


class PinnedPricer:
    """A pre-registered baseline card: FakePricer's rates as of one date, for every date (R8)."""

    def __init__(self, date: str) -> None:
        self._inner = w.PRICER
        self._ts = w.day_ms(date) + 43_200_000
        self.rate_card_sha256 = "pinned:" + date
        self.basis = self._inner.basis

    def resolve(self, ctx: PricingContext, *, ts_ms: int) -> ResolvedRates | None:
        return self._inner.resolve(ctx, ts_ms=self._ts)

    def unit_rates(self, ctx: PricingContext, *, ts_ms: int) -> UnitRates | None:
        return self._inner.unit_rates(ctx, ts_ms=self._ts)

    def price_inference(self, inf: Inference, *, ts_ms: int) -> PricedInference:
        return self._inner.price_inference(inf, ts_ms=self._ts)

    def price_usage(self, usage: UsageBuckets, ctx: PricingContext, *, ts_ms: int,
                    billable: bool | None = True, usage_source: UsageSource = UsageSource.FINAL,
                    output_upper: int | None = None) -> PricedInference:
        return self._inner.price_usage(usage, ctx, ts_ms=self._ts, billable=billable,
                                       usage_source=usage_source, output_upper=output_upper)

    def min_cacheable_tokens(self, ctx: PricingContext, *, ts_ms: int) -> int | None:
        return None

    def supports(self, ctx: PricingContext, feature: str, *, ts_ms: int) -> bool:
        return False

    def tokenizer_family(self, ctx: PricingContext, *, ts_ms: int) -> str | None:
        return None


USAGE = {"uncached": 400_000, "read": 1_600_000, "write": 40_000, "output": 80_000}
BUCKETS = UsageBuckets(uncached_input=400_000, cache_read=1_600_000, cache_write_unknown=40_000,
                       output=80_000)


def _world(model: str, dates: list[str], credits_date: str):
    store, rs = w.world()
    credits = w.credits_of(w.list_price(BUCKETS, model, credits_date))
    pairs = [w.row(d, model, team=team, credits=credits, **USAGE)
             for d in dates for team in ("alpha", "beta")]
    lines = [ln for ln, _ in pairs]
    w.ingest(store, lines=lines, aggs=[a for _, a in pairs] + [
        w.coverage("s1", d, lines) for d in dates])
    return store, rs


def test_pinned_pricer_is_a_pricer() -> None:
    assert isinstance(PinnedPricer("2026-09-23"), Pricer)


def test_r8_opus_5_5_row_baseline_constant_actual_moves() -> None:
    """Two teams × 10 days across the Opus 5.5 row of 2026-09-22: the baseline (a card registered
    on 2026-09-23) is constant, the actual card cannot price the days before its row (0)."""
    dates = w.days("2026-09")[16:26]                          # 09-17 … 09-26
    store, rs = _world("Claude Opus 5.5", dates, "2026-09-23")
    rows = build_copilot_panel(store, [rs], since="2026-09-17", until="2026-09-27",
                               baseline_pricer=PinnedPricer("2026-09-23"),
                               actual_pricer=w.PRICER)
    assert len(rows) == 20 and all(isinstance(r, PanelRow) for r in rows)
    by_team: dict[str, list[PanelRow]] = defaultdict(list)
    for r in rows:
        by_team[r.cluster_id].append(r)
    assert set(by_team) == {"alpha", "beta"}
    for team_rows in by_team.values():
        assert len({r.cost_baseline_nano for r in team_rows}) == 1        # constant (R8)
        actual = [r.cost_actual_nano for r in team_rows]
        assert actual[:5] == [0] * 5 and len(set(actual[5:])) == 1       # moves at 09-22
        assert actual[5] == team_rows[0].cost_baseline_nano > 0
        assert [r.date_utc for r in team_rows] == dates


def test_r8_gpt_5_6_sol_price_change_is_rate_variance() -> None:
    """GPT-5.6 Sol's promo row ends 2026-09-04 ($2 → $4 input): same tokens, baseline constant,
    actual doubles after the change."""
    dates = w.days("2026-08")[28:] + w.days("2026-09", 7)       # 08-29 … 09-07
    store, rs = _world("GPT-5.6 Sol", dates, "2026-09-10")
    rows = build_copilot_panel(store, [rs], since="2026-08-29", until="2026-09-08",
                               baseline_pricer=PinnedPricer("2026-09-10"),
                               actual_pricer=w.PRICER)
    alpha = [r for r in rows if r.cluster_id == "alpha"]
    assert len({r.cost_baseline_nano for r in alpha}) == 1
    before = {r.cost_actual_nano for r in alpha if r.date_utc < "2026-09-04"}
    after = {r.cost_actual_nano for r in alpha if r.date_utc >= "2026-09-04"}
    assert len(before) == len(after) == 1
    assert after.pop() == alpha[0].cost_baseline_nano == 2 * before.pop()


def test_clusters_dev_days_arms_and_pseudo_cells() -> None:
    store, rs = w.world()
    pairs = [w.row("2026-09-10", team="alpha", cost_center="cc1"),
             w.row("2026-09-10", team="beta", cost_center="cc2", org="org-b"),
             w.row("2026-09-11", "Code Review", credits="30", team="alpha", cost_center="cc1"),
             w.row("2026-09-11", team=None, cost_center=None)]
    lines = [ln for ln, _ in pairs]
    w.ingest(store, lines=lines, aggs=[a for _, a in pairs])
    w.put_records(rs, activity=[
        make_activity(w.p("a1"), date_utc="2026-09-10", team="alpha", cost_center="cc1"),
        make_activity(w.p("a2"), date_utc="2026-09-10", team="alpha", cost_center="cc1"),
        make_activity(w.p("a1"), date_utc="2026-09-12", team="alpha", cost_center="cc1")],
        licenses=[make_license(w.p("a1"), snapshot_date="2026-09-10", org="org-a"),
                  make_license(w.p("a2"), snapshot_date="2026-09-10", org="org-b")])
    kw = dict(since="2026-09-01", until="2026-10-01", baseline_pricer=w.PRICER,
              actual_pricer=w.PRICER)
    rows = build_copilot_panel(store, [rs], arms={"alpha": "auto@2026-09-11",
                                                  "beta": "control"}, **kw)
    got = [(r.cluster_id, r.date_utc, r.active_dev_days, r.arm, r.treated) for r in rows]
    assert got == [("alpha", "2026-09-10", 2, "auto", False),
                   ("alpha", "2026-09-11", 0, "auto", True),
                   ("alpha", "2026-09-12", 1, "auto", True),
                   ("beta", "2026-09-10", 0, "control", False)]
    review = rows[1]
    assert review.cost_baseline_nano == review.cost_actual_nano == 30 * w.CREDIT
    assert all(r.outcome_prs is None and r.wave is None for r in rows)
    by_cc = build_copilot_panel(store, [rs], cluster_kind="cost_center",
                                arms={"cc1": "treat"}, **kw)
    assert {(r.cluster_id, r.treated) for r in by_cc} == {("cc1", True), ("cc2", False)}
    by_org = build_copilot_panel(store, [rs], cluster_kind="org", **kw)
    assert {(r.cluster_id, r.date_utc, r.active_dev_days) for r in by_org} == {
        ("org-a", "2026-09-10", 1), ("org-a", "2026-09-11", 0), ("org-a", "2026-09-12", 1),
        ("org-b", "2026-09-10", 1)}


def test_incl_file_is_priced_under_its_decided_convention() -> None:
    store, rs = w.world()
    pairs = [w.row(d, convention="incl", **USAGE) for d in w.days("2026-09", 4)]
    lines = [ln for ln, _ in pairs]
    w.ingest(store, lines=lines, aggs=[a for _, a in pairs] + [
        w.coverage("s_incl", d, lines) for d in w.days("2026-09", 4)])
    rows = build_copilot_panel(store, [rs], since="2026-09-01", until="2026-09-05",
                               baseline_pricer=w.PRICER, actual_pricer=w.PRICER)
    assert [r.cost_actual_nano for r in rows] == [ln.list_amount_nano for ln in lines]


def test_extension_host_reaches_the_panel_builder() -> None:
    store, rs = _world("Claude Sonnet 5", ["2026-09-10"], "2026-09-10")
    rows = extensions.panel("copilot", store, [rs], since="2026-09-10", until="2026-09-11",
                            baseline_pricer=w.PRICER, actual_pricer=w.PRICER)
    assert [r.cluster_id for r in rows] == ["alpha", "beta"]


@pytest.mark.parametrize("kw", [
    {"cluster_kind": "workspace"}, {"since": "2026-09-31"}, {"until": "2026-09-01"},
    {"since": "2026/09/01"}, {"arms": {"alpha": "@2026-09-01"}}, {"arms": {"alpha": ""}},
    {"arms": {"alpha": "x@2026-13-01"}}, {"since": 20260901}])
def test_bad_arguments(kw: dict) -> None:
    store, rs = w.world()
    args = dict(since="2026-09-01", until="2026-10-01", baseline_pricer=w.PRICER,
                actual_pricer=w.PRICER)
    args.update(kw)
    with pytest.raises(UsageError):
        build_copilot_panel(store, [rs], **args)
