"""Pool-converted team kinds: premium-model-share, fast-mode, auto-adoption, cache-health
(addendum §9.2–§9.3, §10.2, Appendix C.G8 / C.P5 / C.P13)."""

from __future__ import annotations

from fractions import Fraction

import pytest

from tokenbill.core import builders as b
from tokenbill.core.labels import Basis, Evidence
from tokenbill.detect.copilot_org import Reach, team_reach

from .worlds import CREDIT, USD, World, attrs, one, only, p13_pools, pool_month, run, tri

# C.P5: a saving of 100,000 credits ($1,000): Opus 5.5 output at $20/M vs Sonnet 5 at $10/M
P5_ROW = dict(date_utc="2026-09-23", model="Claude Opus 5.5", credits="200000",
              output_tokens=100_000_000)
SAVING = 1_000 * USD


def _premium_world(pools: list) -> World:
    w = World().row(team="payments", principal=b.make_principal("pay-0"), **P5_ROW)
    w.pools.extend(pools)
    return w


@pytest.mark.parametrize(("consumed", "invoice", "headroom"), [
    (3_100_000, 1_000 * USD, 0),         # overage 420,000 ≥ S → all invoice
    (2_740_000, 600 * USD, 400 * USD),   # overage 60,000 → $600 invoice, $400 headroom
    (2_000_000, 0, 1_000 * USD),         # slack → all headroom
])
def test_c_p5_premium_model_share_per_regime(consumed: int, invoice: int, headroom: int) -> None:
    f = one(run(_premium_world([pool_month(consumed)]).ctx()), "premium-model-share")
    assert f.cost_observed.nano == 200_000 * CREDIT
    assert (f.cost_observed.basis, f.cost_observed.evidence) == (Basis.LIST_EQUIVALENT,
                                                                 Evidence.EXACT)
    assert f.recoverable.nano == invoice and f.headroom.nano == headroom
    assert f.recoverable.nano + f.headroom.nano == SAVING
    assert (f.recoverable.basis, f.recoverable.evidence) == (Basis.LIST, Evidence.ESTIMATED)
    assert f.headroom.basis is Basis.LIST_EQUIVALENT
    assert f.needs_eval and f.lever_ids == ("copilot.model_policy",)
    assert f.category == "aggregate" and f.lever_class == "trajectory"
    assert dict(f.scope.dims) == {"product": "copilot", "entity": "enterprise",
                                  "team": "payments"}
    assert f.fix is not None and f.fix.target == "github-copilot"
    assert attrs(f, "model:claude-opus-5-5")["target"] == "claude-sonnet-5"


def test_plan_unknown_two_scenario_findings_c_p13() -> None:
    found = only(run(_premium_world(p13_pools()).ctx()), "premium-model-share")
    by = {dict(f.scope.dims)["plan_scenario"]: f for f in found}
    assert set(by) == {"business", "enterprise"}
    assert (by["business"].recoverable.nano, by["business"].headroom.nano) == (600 * USD,
                                                                                400 * USD)
    assert (by["enterprise"].recoverable.nano, by["enterprise"].headroom.nano) == (0,
                                                                                    1_000 * USD)
    for scenario, f in by.items():
        assert f.recoverable.evidence is Evidence.ESTIMATED
        assert f.headroom.evidence is Evidence.ESTIMATED
        assert f.title.startswith("If Business:" if scenario == "business" else "If Enterprise:")
        assert f"scenario {scenario}" in f.recoverable.note
        assert "unknown seats" in f.summary
    assert by["business"].finding_id != by["enterprise"].finding_id


def test_premium_known_month_used_in_both_scenarios_across_months() -> None:
    w = _premium_world(p13_pools())
    # August (plan known, overage): Opus 4.8 output 100M → Sonnet 5 saves $15/M = $1,500
    w.row(team="payments", principal=b.make_principal("pay-0"), date_utc="2026-08-25",
          model="Claude Opus 4.8", credits="250000", output_tokens=100_000_000)
    w.pools.append(pool_month(3_100_000, month="2026-08"))
    by = {dict(f.scope.dims)["plan_scenario"]: f
          for f in only(run(w.ctx()), "premium-model-share")}
    assert by["business"].recoverable.nano == 2_100 * USD      # $1,500 August + $600
    assert by["enterprise"].recoverable.nano == 1_500 * USD    # $1,500 August + $0
    assert by["enterprise"].headroom.nano == 1_000 * USD


def test_premium_without_pool_month_or_tokens_is_unpriced() -> None:
    f = one(run(_premium_world([]).ctx()), "premium-model-share")
    assert f.recoverable.nano is None and "no pool month" in f.recoverable.note
    w = World().row(team="t", principal=b.make_principal(1), date_utc="2026-09-23",
                    model="Claude Opus 5.5", credits="500")          # no token columns
    w.pools.append(pool_month(3_100_000))
    f = one(run(w.ctx()), "premium-model-share")
    assert f.recoverable.nano is None and "token columns" in f.recoverable.note
    assert f.cost_observed.nano == 500 * CREDIT                      # gate on cost_observed


def test_premium_tokenizer_band_and_model_scope() -> None:
    # GPT-5.5 → GPT-5.6 Terra has another tokenizer: target tokens × [1.00, 1.35]
    w = World().row(team="data", principal=b.make_principal(2), date_utc="2026-09-10",
                    model="GPT-5.5", credits="3500", input_tokens=1_000_000,
                    output_tokens=1_000_000)
    w.pools.append(pool_month(3_100_000))
    f = one(run(w.ctx()), "premium-model-share")
    # 1M × ($5 − $2) + 1M × ($30 − $12) = $21; low: $35 − $14 × 1.35 = $16.10
    assert tri(f.recoverable) == (16_100_000_000, 21 * USD, 21 * USD)
    assert f.recoverable.nano + f.headroom.nano == 21 * USD


def test_direct_credits_are_invoice_unless_they_draw_the_pool() -> None:
    w = World().row(unattributed=True, **P5_ROW)                     # ai_credit.direct, no team
    w.pools.append(pool_month(2_000_000))                            # slack
    f = one(run(w.ctx()), "premium-model-share")
    assert f.recoverable.nano == SAVING and f.headroom.nano == 0
    assert f.recoverable.upper_bound and "draw the pool is unknown" in f.recoverable.note
    w.pools[0] = pool_month(2_000_000, direct_draws_pool="yes")
    f = one(run(w.ctx()), "premium-model-share")
    assert (f.recoverable.nano, f.headroom.nano) == (0, SAVING)
    w.pools[0] = pool_month(2_000_000, direct_draws_pool="no")
    f = one(run(w.ctx()), "premium-model-share")
    assert (f.recoverable.nano, f.headroom.nano) == (SAVING, 0) and not f.recoverable.upper_bound


# ---- fast-mode (C.G8) ------------------------------------------------------------------------

G8 = dict(date_utc="2026-09-10", model="Claude Opus 4.8 (fast mode)", credits="29",
          input_tokens=10_000, cache_read_tokens=90_000, output_tokens=2_000)


def test_fast_mode_c_g8_exact_premium() -> None:
    w = World().row(team="mobile", principal=b.make_principal(3), **G8)
    w.pools.append(pool_month(3_100_000))
    f = one(run(w.ctx(min_usd="0.01")), "fast-mode")
    assert f.cost_observed.nano == 145_000_000 and f.cost_observed.evidence is Evidence.EXACT
    assert f.cost_observed.basis is Basis.LIST_EQUIVALENT
    assert f.recoverable.nano == 145_000_000 and f.headroom.nano == 0     # overage regime
    assert f.lever_ids == ("copilot.fast_mode_off",) and f.lever_class == "rate"
    assert not f.needs_eval and f.confidence == "high"
    # the remap of the same cells excludes the fast premium (priced at standard speed)
    prem = one(run(w.ctx(min_usd="0.01")), "premium-model-share")
    assert attrs(prem, "model:claude-opus-4-8")["saving_nano"] == 145_000_000 - 58_000_000


def test_fast_mode_c_g8b_unknown_ttl_writes_estimated() -> None:
    w = World().row(team="mobile", principal=b.make_principal(3),
                    **{**G8, "cache_write_tokens": 5_000})
    w.pools.append(pool_month(3_100_000))
    f = one(run(w.ctx(min_usd="0.01")), "fast-mode")
    assert f.cost_observed.evidence is Evidence.ESTIMATED
    assert tri(f.cost_observed) == (176_250_000, 176_250_000, 195_000_000)
    assert tri(f.recoverable) == (176_250_000, 176_250_000, 195_000_000)


def test_fast_mode_undecided_convention_is_estimated() -> None:
    w = World().row(team="mobile", principal=b.make_principal(3), **G8)
    w.pools.append(pool_month(3_100_000))
    ctx = w.ctx(min_usd="0.01", recon_decisions=(("convention:a", "excl"),
                                                  ("convention:b", "undecidable")))
    f = one(run(ctx), "fast-mode")
    assert f.cost_observed.evidence is Evidence.ESTIMATED
    assert f.cost_observed.nano == 145_000_000 and "undecided" in f.cost_observed.note


# ---- auto-adoption and reach (§9.3) ----------------------------------------------------------

AUTO_ROW = dict(date_utc="2026-09-23", model="Claude Opus 5.5", credits="1000000")


def _auto_world(consumed: int = 3_100_000) -> World:
    w = World().row(team="payments", principal=b.make_principal("pay-0"), **AUTO_ROW)
    w.pools.append(pool_month(consumed))
    return w


@pytest.mark.parametrize(("consumed", "invoice", "headroom"), [
    (3_100_000, 1_000 * USD, 0), (2_740_000, 600 * USD, 400 * USD),
    (2_000_000, 0, 1_000 * USD)])
def test_c_p5_auto_adoption_full_reach(consumed: int, invoice: int, headroom: int) -> None:
    w = _auto_world(consumed).ide("payments", 5, vscode=10)
    f = one(run(w.ctx()), "auto-adoption")
    assert (f.recoverable.nano, f.headroom.nano) == (invoice, headroom)
    assert attrs(f, "reach")["reach"] == "1"


def test_auto_reach_from_metrics_scales_exactly() -> None:
    w = _auto_world().ide("payments", 5, vscode=8, intellij=2)       # 20% ide:intellij
    f = one(run(w.ctx()), "auto-adoption")
    assert f.recoverable.nano == 800 * USD and f.recoverable.low_nano is None
    a = attrs(f, "reach")
    assert (a["reach"], a["jetbrains_share"], a["reach_source"]) == ("0.8", "0.2", "metrics")
    assert f.lever_ids[0] == "copilot.default_model_auto" and f.needs_eval
    assert f.fix is not None and f.fix.config_patch == (("copilot.managed.model", '"auto"'),)
    assert f.category == "aggregate"


def test_auto_no_metrics_point_zero_range_to_full() -> None:
    f = one(run(_auto_world().ctx()), "auto-adoption")
    assert tri(f.recoverable) == (0, 0, 1_000 * USD)
    assert "reach unknown" in f.summary and attrs(f, "reach")["reach"] == "unknown"
    assert f.confidence == "low"


def test_jetbrains_heavy_team_gets_the_model_policy_fix() -> None:
    w = _auto_world().ide("payments", 5, vscode=4, intellij=6)       # 60% JetBrains
    f = one(run(w.ctx()), "auto-adoption")
    assert f.recoverable.nano == 400 * USD                          # reach 0.4
    assert "server-side model policy" in f.fix.text and f.fix.config_patch is None
    assert f.fix.target == "github-copilot"
    assert f.lever_ids[0] == "copilot.model_policy" and "copilot.auto_tier" in f.lever_ids
    mix = one(run(w.ctx()), "editor-mix", team="payments")
    assert "server-side model policy" in mix.fix.text


def test_cloud_agent_cells_reach_one_and_auto_routed_or_review_excluded() -> None:
    w = _auto_world().ide("payments", 5, intellij=10)                 # reach 0 for editors
    w.row(team="payments", principal=b.make_principal("pay-1"), date_utc="2026-09-23",
          model="Claude Sonnet 5", sku="coding_agent_ai_credit", credits="100000")
    w.row(team="payments", principal=b.make_principal("pay-2"), date_utc="2026-09-23",
          model="Auto: Claude Sonnet 5", credits="500000")
    w.row(team="payments", principal=b.make_principal("pay-3"), date_utc="2026-09-23",
          model="Code Review", credits="500000")
    f = one(run(w.ctx()), "auto-adoption")
    assert f.recoverable.nano == 100 * USD                          # 10% × 100,000 × reach 1
    assert f.cost_observed.nano == 1_100_000 * CREDIT
    assert attrs(f, "credits:eligible")["cloud_agent_nano"] == 100_000 * CREDIT


def test_reach_from_activity_counts_equals_reach_from_days() -> None:
    w = _auto_world()
    w.config.append(b.make_config("activity_counts", {
        "team": "payments", "month": "2026-09", "n_people": 6, "ide:vscode": 80,
        "ide:jetbrains": 20}, entity_id="org:org-a"))
    f = one(run(w.ctx()), "auto-adoption")
    assert f.recoverable.nano == 800 * USD
    assert attrs(f, "reach")["reach_source"] == "activity_counts"
    days = World().ide("payments", 5, vscode=16, intellij=4)
    assert team_reach(days.activity, (), "payments").point == Fraction(4, 5)
    assert team_reach((), w.config, "payments").point == Fraction(4, 5)


def test_team_reach_edge_cases() -> None:
    w = World().ide("cli", 2, cli_requests=5)
    assert team_reach(w.activity, (), "cli").source == "cli_app"
    assert team_reach(w.activity, (), "cli").point == 1
    unknown = team_reach((), (), "nobody")
    assert (unknown.point, unknown.low, unknown.high, unknown.known) == (0, 0, 1, False)
    mixed = team_reach(World().ide("t", 1, vscode=2, visualstudio=1, xcode=1).activity, (), "t")
    assert mixed.point == Fraction(1, 2) and mixed.unreached_share == Fraction(1, 2)
    assert isinstance(mixed, Reach) and mixed.jetbrains_share == 0


# ---- cache-health ----------------------------------------------------------------------------


def test_cache_health_team_below_the_org_median() -> None:
    w = World()
    for team, read in (("good", 900_000), ("ok", 800_000), ("poor", 200_000)):
        w.row(team=team, principal=b.make_principal(team), date_utc="2026-09-10",
              model="Claude Sonnet 5", credits="100", input_tokens=1_000_000 - read,
              cache_read_tokens=read)
    w.pools.append(pool_month(2_000_000))                            # slack → headroom
    found = run(w.ctx(min_usd="0.01"))
    f = one(found, "cache-health")
    assert dict(f.scope.dims)["team"] == "poor" and dict(f.scope.dims)["model"] == "claude-sonnet-5"
    # median share 0.8: (0.8 × 1M − 0.2M) × ($2.50 − $0.20)/M = $1.38 upper bound
    assert f.headroom.nano == 1_380_000_000 and f.recoverable.nano == 0
    assert f.headroom.upper_bound and f.recoverable.upper_bound
    assert f.cost_observed.nano == 100 * CREDIT
    assert attrs(f, "model:claude-sonnet-5")["org_median"] == "0.8"
    assert not only(found, "cache-health", team="good")


def test_rows_without_team_are_scoped_by_cost_center() -> None:
    w = World().rows(2, team=None, prefix="cc", cost_center="cc-eng", **P5_ROW)
    w.row(principal=b.make_principal("loose"), **P5_ROW)            # no team, no cost center
    w.pools.append(pool_month(3_100_000))
    found = only(run(w.ctx()), "premium-model-share")
    by_cc = {dict(f.scope.dims).get("cost_center"): f for f in found}
    assert set(by_cc) == {"cc-eng", None}
    assert "team" not in dict(by_cc["cc-eng"].scope.dims)
    assert by_cc["cc-eng"].n_users == 2 and by_cc[None].n_users == 1
    assert "cost center cc-eng" in by_cc["cc-eng"].title
    assert "unattributed usage" in by_cc[None].summary
