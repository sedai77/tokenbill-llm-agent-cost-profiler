"""GitHub Copilot builders (CORE-AMENDMENTS C-29) and ``FlatRates`` on the Copilot billing paths."""

from __future__ import annotations

from dataclasses import replace

import pytest

from tokenbill.core import builders as b
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import (
    BILLING_PATHS,
    ActivityDay,
    ConfigSnapshot,
    CostLine,
    LicenseSnapshot,
    UsageAggregate,
    from_json,
    to_json,
)


def test_canary_login_and_principals() -> None:
    assert b.CANARY_LOGIN == "tb-canary-login-7f3a91"
    assert b.CANARY not in b.CANARY_LOGIN
    p = b.make_principal("alice")
    assert p.startswith("p_") and len(p) == 22 and p == b.make_principal("alice")
    assert p != b.make_principal("bob") and b.CANARY_LOGIN not in p
    with pytest.raises(AssertionError):
        b.assert_no_canary(f"user {b.CANARY}")


def test_make_copilot_ctx() -> None:
    ctx = b.make_copilot_ctx()
    assert (ctx.provider, ctx.channel, ctx.billing_path, ctx.model) == (
        "github", "github_copilot", "copilot_pool", "claude-opus-5-5")
    direct = b.make_copilot_ctx("gpt-5.6-sol", billing_path="copilot_direct", routing="auto",
                                context_tier="default")
    assert (direct.billing_path, direct.routing, direct.context_tier, direct.provider) == (
        "copilot_direct", "auto", "default", "github")


def test_record_builders_round_trip() -> None:
    lic = b.make_license()
    act = b.make_activity(counts={"interactions": 4, "ide:vscode": 4}, flags=["used_chat"])
    cfg = b.make_config("seat_counts", {"team": "core", "n": 7, "n_people": 7},
                        entity_id="enterprise")
    for rec, cls in ((lic, LicenseSnapshot), (act, ActivityDay), (cfg, ConfigSnapshot)):
        assert from_json(cls, to_json(rec)) == rec
    assert cfg.source_kind == "tokenbill.copilot_export"
    assert b.make_config("budget_users", {"n_users": 12}, entity_id="budget:1").source_kind == (
        "github.budgets")
    assert b.make_config("cost_center", {}, entity_id="cc:x").source_kind == "github.cost_centers"
    assert b.make_activity().counts == (("interactions", 1),)


def test_make_ai_usage_row_github_parser_example() -> None:
    """GitHub's test row: 42.726213 credits, gross = discount → net 0, Auto: Claude Haiku 4.5."""
    line, agg = b.make_ai_usage_row(date_utc="2026-06-01", model="Auto: Claude Haiku 4.5",
                                    credits="42.726213", discount_credits="42.726213",
                                    organization="example-org")
    assert (line.list_amount_nano, line.amount_nano) == (427_262_130, 0)
    assert (line.model, line.routing, line.speed, line.pseudo) == ("claude-haiku-4-5", "auto",
                                                                   "standard", None)
    assert (line.cost_type, line.channel, line.quantity, line.unit) == (
        "ai_credit.user", "github_copilot", "42.726213", "ai-credits")
    assert line.principal == b.make_principal(0) and line.workspace_id == "example-org"
    assert from_json(CostLine, to_json(line)) == line
    assert dict(agg.dims)["routing"] == "auto" and agg.reported_cost_nano == 0
    assert (agg.bucket_end_ms - agg.bucket_start_ms, agg.list_cost_nano) == (86_400_000,
                                                                              427_262_130)
    assert from_json(UsageAggregate, to_json(agg)) == agg
    again, _ = b.make_ai_usage_row(date_utc="2026-06-01", model="Auto: Claude Haiku 4.5",
                                   credits="1", organization="example-org")
    assert again.line_id == line.line_id  # natural key: revised exports keep the id


def test_make_ai_usage_row_pseudo_direct_and_tokens() -> None:
    review, agg = b.make_ai_usage_row(model="Copilot code review", unattributed=True,
                                      input_tokens=10, output_tokens=5, cache_read_tokens=100,
                                      cache_write_tokens=7)
    assert (review.principal, review.cost_type, review.pseudo, review.workload, review.model) == (
        None, "ai_credit.direct", "code_review", "copilot_code_review", None)
    assert (agg.usage.uncached_input, agg.usage.cache_read, agg.usage.cache_write_unknown,
            agg.usage.output) == (10, 100, 7, 5)
    assert "model" not in dict(agg.dims) and dict(agg.dims)["pseudo"] == "code_review"
    agent, _ = b.make_ai_usage_row(sku="coding_agent_ai_credit")
    assert agent.workload == "copilot_cloud_agent"
    quality, _ = b.make_ai_usage_row(sku="code_quality_ai_credit", team="core",
                                     repo="h_" + "1" * 20)
    assert (quality.workload, quality.team, quality.repo) == ("code_quality", "core",
                                                              "h_" + "1" * 20)
    legacy, _ = b.make_ai_usage_row(sku="copilot_premium_request")
    assert legacy.cost_type == "ai_credit.legacy_pru"
    fast, _ = b.make_ai_usage_row(model="Claude Opus 4.8 (fast mode) (preview)")
    assert (fast.model, fast.speed) == ("claude-opus-4-8", "fast")


def test_seat_and_actions_lines() -> None:
    seat = b.make_seat_line("enterprise", "50")
    assert (seat.sku, seat.cost_type, seat.amount_nano, seat.quantity, seat.channel) == (
        "copilot_enterprise", "seat", 1_950 * 10**9, "50", "github_copilot")
    biz = b.make_seat_line("business", "1000", discount_nano=10)
    assert (biz.sku, biz.list_amount_nano, biz.amount_nano) == ("copilot_for_business",
                                                                19_000 * 10**9, 19_000 * 10**9 - 10)
    assert b.make_seat_line("business", sku="copilot_standalone").sku == "copilot_standalone"
    act = b.make_actions_line("100", sku="linux_16_core", usd_per_minute="0.042",
                              workload="copilot_cloud_agent", workflow="h_" + "c" * 20)
    assert (act.channel, act.cost_type, act.amount_nano, act.unit) == ("github_actions",
                                                                       "actions", 4_200_000_000,
                                                                       "minutes")
    assert from_json(CostLine, to_json(act)) == act
    free = b.make_actions_line("10", discount_nano=60_000_000)
    assert free.amount_nano == 0 and free.list_amount_nano == 60_000_000


def test_pool_month_and_plan_evidence_builders() -> None:
    pm = b.make_pool_month(seats={"business": "1000", "enterprise": "200"},
                           consumed_report_nano=3_100_000 * 10**7)
    assert pm.pool_credits == "2680000" and pm.regime == "overage"  # Appendix C.P1
    assert pm.overage_observed_nano == 420_000 * 10**7
    fractional = b.make_pool_month(seats={"business": "99.5"})
    assert fractional.pool_credits == "189050"
    custom = b.make_pool_month(regime="unknown", billing_mode="volume")
    assert (custom.regime, custom.billing_mode) == ("unknown", "volume")
    ev = b.make_plan_evidence(plan="enterprise", source="seat_lines", seats={"enterprise": 50},
                              conflict=True, evidence=["seat_lines: copilot_enterprise 50"])
    assert ev.evidence == ("seat_lines: copilot_enterprise 50",)


@pytest.mark.parametrize("path", BILLING_PATHS)
def test_flat_rates_bases_per_path(path: str) -> None:
    ctx = b.make_ctx("claude-opus-5-5", billing_path=path)
    priced = b.FlatRates().price_usage(b.make_usage(uncached_input=1000, output=100), ctx, ts_ms=0)
    expected = (Basis.LIST_EQUIVALENT if path in ("subscription", "copilot_pool",
                                                  "copilot_direct") else Basis.LIST)
    assert priced.figure.basis is expected and priced.figure.evidence is Evidence.EXACT
    assert priced.exact_nano == 1_000_000 + 500_000


def test_flat_rates_copilot_ctx_unit_rates_agree() -> None:
    ctx = b.make_copilot_ctx(write_ttl_hint="1h")
    pricer = b.FlatRates()
    usage = b.make_usage(uncached_input=12_000, cache_read=180_000, cache_write_5m=6_000,
                         output=3_000)
    priced = pricer.price_usage(usage, ctx, ts_ms=0)
    unit = pricer.unit_rates(ctx, ts_ms=0)
    assert unit is not None
    total = sum(unit.bucket_nano(line.bucket, line.quantity) for line in priced.lines)
    assert total == priced.exact_nano and priced.figure.basis is Basis.LIST_EQUIVALENT
    lane_req = b.make_request("L", 0, 0, usage, provider="github", channel="github_copilot",
                              billing_path="copilot_pool")
    assert lane_req.attribution.billing_path == "copilot_pool"
    assert replace(ctx, routing="auto").routing == "auto"
