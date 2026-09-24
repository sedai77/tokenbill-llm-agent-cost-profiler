"""SPEC §12 acceptance tests of ``recon.reconcile`` on hand-built aggregates and cost lines."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from tokenbill.core import catalog
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Finality
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import ContractOverlay
from tokenbill.recon.reconcile import reconcile, reconciled_channels, suggest_contracts

from .helpers import (
    DAY,
    DAY2,
    OPUS,
    PRICER,
    RECENT,
    SONNET,
    TODAY,
    USAGE,
    WS,
    WS2,
    agg,
    cost_lines,
    priced_buckets,
    record,
    residuals,
    rows_where,
    split_records,
    verdicts,
)


def _pair(factor: Decimal = Decimal(1), **kw: object) -> tuple[list, list]:
    aggs = [agg(date=DAY, **kw), agg(date=DAY2, model=SONNET, **kw)]  # type: ignore[arg-type]
    return aggs, [line for a in aggs for line in cost_lines(a, factor=factor)]


def _ledger(**kw: object) -> list:
    return [*split_records(USAGE, 3, date=DAY, **kw),  # type: ignore[arg-type]
            *split_records(USAGE, 2, date=DAY2, model=SONNET, **kw)]  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# exact pricing
# ---------------------------------------------------------------------------------------------


def test_exact_pricing_reconciles() -> None:
    aggs, lines = _pair()
    report = reconcile(iter(_ledger()), aggs, lines, PRICER, today=TODAY)
    assert report.verdict == "reconciled"
    assert verdicts(report) == {"anthropic_api": "reconciled"}
    assert report.rate_card_error == ("0", "0", "0")
    assert report.over_count_rows == 0 and report.unexplained_nano == 0
    assert report.token_coverage_pct == "100" and report.dollar_coverage_pct == "100"
    assert report.channels[0].mapping_verified is True
    assert report.channels[0].invoice_sources == ("anthropic.cost_report",)
    assert report.finality is Finality.FINAL
    assert report.window == (DAY, DAY2)
    assert report.tolerance_pct == "0.5" and report.unexplained_tolerance_pct == "1.0"
    token_rows = [r for r in report.rows if r.provider_tokens is not None]
    assert token_rows and all(r.status == "match" for r in token_rows)
    assert all(r.priced_provider_nano == r.invoice_nano == r.ledger_nano for r in token_rows)
    assert report.suggested_contract is None and report.rerun_verdict is None
    assert report.effective_discount and all(v == "0" for _, v in report.effective_discount)


def test_ledger_is_streamed_once_and_rows_are_keyed_not_per_request() -> None:
    aggs, lines = _pair()
    consumed = []

    def gen():  # type: ignore[no-untyped-def]
        for i, rec in enumerate(split_records(USAGE, 50, date=DAY)):
            consumed.append(i)
            yield rec
        yield from split_records(USAGE, 50, date=DAY2, model=SONNET)

    report = reconcile(gen(), aggs, lines, PRICER, today=TODAY)
    assert len(consumed) == 50
    assert len(report.rows) == len(reconcile([], aggs, lines, PRICER, today=TODAY).rows)
    assert verdicts(report) == {"anthropic_api": "reconciled"}


def test_no_invoice_rows_is_insufficient_data() -> None:
    aggs, _ = _pair()
    report = reconcile(_ledger(), aggs, [], PRICER, today=TODAY)
    assert report.verdict == "insufficient_data"
    assert verdicts(report) == {"anthropic_api": "insufficient_data"}
    assert report.channels[0].invoice_sources == ()
    empty = reconcile([], [], [], PRICER, today=TODAY)
    assert empty.verdict == "insufficient_data" and empty.channels == ()
    assert empty.window == (TODAY, TODAY) and empty.finality is Finality.NA


def test_admin_only_org_reconciles_on_the_provider_side() -> None:
    aggs, lines = _pair()
    report = reconcile([], aggs, lines, PRICER, today=TODAY)
    assert report.verdict == "reconciled"
    assert residuals(report) == {"unobserved_traffic": sum(c.amount_nano for c in lines)}
    assert report.dollar_coverage_pct == "0"


# ---------------------------------------------------------------------------------------------
# discounts and the contract suggestion
# ---------------------------------------------------------------------------------------------


def test_uniform_discount_suggests_a_multiplier_and_the_rerun_reconciles() -> None:
    aggs, lines = _pair(Decimal("0.85"))
    calls: list[ContractOverlay] = []

    def factory(overlay: ContractOverlay) -> FakePricer:
        calls.append(overlay)
        return FakePricer().with_contract(overlay)

    report = reconcile(_ledger(), aggs, lines, PRICER, today=TODAY, suggest_contract=True,
                       rerun_pricer_factory=factory)
    assert report.verdict == "not_reconciled"                     # list prices vs a discount
    assert all(Decimal(v) == Decimal("0.15") for _, v in report.effective_discount)
    assert {k.split(":")[1] for k, _ in report.effective_discount} == {OPUS, SONNET, "*"} - {"*"}
    overlay = report.suggested_contract
    assert overlay is not None and calls == [overlay]
    assert overlay.multiplier == Decimal("0.85") and overlay.overrides == ()
    assert overlay.derived is True and overlay.channels == ("anthropic_api",)
    assert overlay.effective_from == DAY and overlay.sha256
    assert report.rerun_verdict == "reconciled"
    assert FakePricer().with_contract(overlay).basis.value == "contract"
    assert residuals(report)["implied_discount"] < 0
    assert suggest_contracts(aggs, lines, PRICER, today=TODAY) == (overlay,)


def test_rerun_uses_with_contract_when_no_factory_is_given() -> None:
    aggs, lines = _pair(Decimal("0.85"))
    report = reconcile(_ledger(), aggs, lines, PRICER, today=TODAY, suggest_contract=True)
    assert report.rerun_verdict == "reconciled"


def test_non_uniform_discount_is_not_a_simple_multiplier() -> None:
    a1, a2 = agg(date=DAY), agg(date=DAY2)
    lines = cost_lines(a1, factor=Decimal("0.90")) + cost_lines(a2, factor=Decimal("0.70"))
    ledger = [record(date=DAY), record(date=DAY2)]
    report = reconcile(ledger, [a1, a2], lines, PRICER, today=TODAY, suggest_contract=True)
    assert report.suggested_contract is not None
    assert report.suggested_contract.multiplier == Decimal("0.8")
    assert report.rerun_verdict == "not_reconciled"


def test_per_model_multipliers_become_overrides() -> None:
    a1, a2 = agg(date=DAY, model=OPUS), agg(date=DAY, model=SONNET, ws=WS2)
    lines = cost_lines(a1, factor=Decimal("0.80")) + cost_lines(a2, factor=Decimal("0.90"))
    report = reconcile([record(), record(model=SONNET, ws=WS2)], [a1, a2], lines, PRICER,
                       today=TODAY, suggest_contract=True)
    overlay = report.suggested_contract
    assert overlay is not None and overlay.multiplier == Decimal("0.8")
    (model, prices), = overlay.overrides
    assert model == SONNET
    assert dict(prices)["input"] == Decimal("2.00") * Decimal("0.9")
    assert report.rerun_verdict == "reconciled"


def test_no_suggestion_when_the_invoice_is_at_list() -> None:
    aggs, lines = _pair()
    report = reconcile(_ledger(), aggs, lines, PRICER, today=TODAY, suggest_contract=True)
    assert report.suggested_contract is None and report.rerun_verdict is None


# ---------------------------------------------------------------------------------------------
# channels
# ---------------------------------------------------------------------------------------------


def test_bedrock_without_invoice_blocks_only_bedrock() -> None:
    aggs, lines = _pair()
    ledger = [*_ledger(), record(channel="bedrock", endpoint_scope="global", ws=None)]
    report = reconcile(ledger, aggs, lines, PRICER, today=TODAY)
    assert verdicts(report) == {"anthropic_api": "reconciled", "bedrock": "insufficient_data"}
    assert report.verdict == "not_reconciled"
    assert reconciled_channels(report) == frozenset({"anthropic_api"})
    bedrock = rows_where(report, channel="bedrock")
    assert bedrock and all(r.invoice_nano is None and r.residual_code is None for r in bedrock)


def test_delegated_channels_are_skipped() -> None:
    aggs, lines = _pair()
    copilot = record(channel="github_copilot", model=OPUS, billing_path="copilot_pool")
    report = reconcile([*_ledger(), copilot], aggs, lines, PRICER, today=TODAY)
    assert "github_copilot" not in verdicts(report)


def _cur(net: Decimal, *, usage_type: str, model: str | None, date: str = DAY,
         listed: int = 0, cost_type: str | None = None, amount: int | None = None):
    from tokenbill.core.builders import make_cost_line
    return make_cost_line(
        amount if amount is not None else int(Decimal(listed) * net),
        date_utc=date, channel="bedrock", model=model, source_kind="aws.cur2",
        workspace_id="h_" + "a" * 20, cost_type=cost_type, token_type=None, sku=usage_type,
        list_amount_nano=listed, line_id=f"cur-{date}-{usage_type}-{cost_type}")


_CUR_TYPES = {"uncached_input": "USE1-MP:USE1_InputTokenCount_Global-Units",
              "output": "USE1-MP:USE1_OutputTokenCount_Global-Units",
              "cache_read": "USE1-MP:USE1_CacheReadInputTokenCount_Global-Units",
              "cache_write_5m": "USE1-MP:USE1_CacheWriteInputTokenCount_Global-Units",
              "cache_write_1h": "USE1-MP:USE1_CacheWrite1hInputTokenCount_Global-Units"}


def _bedrock_world(net: Decimal = Decimal("0.9"), *, gap: Decimal = Decimal(1),
                   model_on_lines: str | None = None, mapped: bool = False):
    usage = {"uncached_input": 1_000_000, "output": 200_000, "cache_read": 4_000_000}
    ledger_rec = record(usage, channel="bedrock", endpoint_scope="global", ws="h_" + "a" * 20)
    listed = priced_buckets(agg(usage, channel="bedrock", model=OPUS, ws="h_" + "a" * 20,
                                endpoint_scope="global"))
    lines, aggs = [], []
    for bucket, nano in sorted(listed.items()):
        lines.append(_cur(net, usage_type=_CUR_TYPES[bucket], model=model_on_lines,
                          listed=int(Decimal(nano) * gap)))
        dims = {"sku": _CUR_TYPES[bucket]} if not mapped else {"endpoint_scope": "global"}
        aggs.append(agg({bucket if mapped else "uncached_input": usage[bucket]},
                        channel="bedrock", source_kind="aws.cur2", ws="h_" + "a" * 20,
                        model=OPUS if mapped else None, tier="standard" if mapped else None,
                        **dims))
    return ledger_rec, aggs, lines


def test_cur_channel_total_mode_with_every_sku_rule_disabled() -> None:
    assert not any(r.verified for r in catalog.SKU_RULES)
    rec, aggs, lines = _bedrock_world()
    report = reconcile([rec], aggs, lines, PRICER, today=TODAY, suggest_contract=True)
    (bedrock,) = report.channels
    assert bedrock.verdict == "reconciled" and bedrock.mapping_verified is False
    assert dict(report.effective_discount) == {"bedrock:*:*": "0.1"}
    overlay = report.suggested_contract
    assert overlay is not None and overlay.channels == ("bedrock",)
    assert overlay.multiplier == Decimal("0.9")
    (row,) = rows_where(report, channel="bedrock", bucket="total")
    assert row.priced_provider_nano is None and row.rate_card_error_pct is None
    assert row.ledger_tokens == row.provider_tokens
    assert report.rerun_verdict == "reconciled"   # a contract-basis ledger is not discounted twice


def test_cur_channel_total_mode_fails_on_an_unexplained_three_percent_gap() -> None:
    rec, aggs, lines = _bedrock_world(gap=Decimal("1.03"))
    report = reconcile([rec], aggs, lines, PRICER, today=TODAY)
    assert verdicts(report) == {"bedrock": "not_reconciled"}
    (row,) = rows_where(report, channel="bedrock", bucket="total")
    assert row.status == "unexplained" and report.unexplained_nano > 0


def test_cur_mapped_with_verified_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "SKU_RULES", tuple(dataclasses.replace(r, verified=True)
                                                    for r in catalog.SKU_RULES))
    rec, aggs, lines = _bedrock_world(model_on_lines=OPUS, mapped=True)
    report = reconcile([rec], aggs, lines, PRICER, today=TODAY, suggest_contract=True)
    (bedrock,) = report.channels
    assert bedrock.mapping_verified is True and bedrock.verdict == "not_reconciled"
    assert {k: v for k, v in report.effective_discount} == {
        f"bedrock:{OPUS}:cache_read": "0.1", f"bedrock:{OPUS}:output": "0.1",
        f"bedrock:{OPUS}:uncached_input": "0.1"}
    assert report.suggested_contract is not None
    assert report.suggested_contract.channels == ("bedrock",)
    assert report.rerun_verdict == "reconciled"


def test_cur_credit_lines_are_cloud_credits() -> None:
    rec, aggs, lines = _bedrock_world()
    credit = _cur(Decimal(1), usage_type="USE1-Credit", model=None, cost_type="Credit",
                  amount=-5_000_000)
    report = reconcile([rec], aggs, [*lines, credit], PRICER, today=TODAY)
    assert residuals(report)["cloud_credits"] == -5_000_000
    assert verdicts(report) == {"bedrock": "reconciled"}


def test_gcp_credits_are_split_from_the_gross_cost() -> None:
    from tokenbill.core.builders import make_cost_line
    usage = {"uncached_input": 1_000_000}
    a = agg(usage, channel="vertex", source_kind="gcp.billing_export", model=None,
            tier=None, sku="E0F1-CLAUDE")
    line = make_cost_line(950_000_000, channel="vertex", source_kind="gcp.billing_export",
                          model=None, cost_type="regular", token_type=None, sku="E0F1-CLAUDE",
                          date_utc=DAY, list_amount_nano=1_000_000_000, workspace_id=WS)
    report = reconcile([], [a], [line], PRICER, today=TODAY)
    assert residuals(report)["cloud_credits"] == -50_000_000
    assert report.channels[0].mapping_verified is False


def test_ccu_single_line_and_no_reporting_api() -> None:
    from tokenbill.core.builders import make_cost_line
    foundry = record(channel="foundry", ws=None)
    ccu = make_cost_line(1_234_000_000, channel="foundry", source_kind="azure.marketplace",
                         model=None, cost_type=None, token_type=None, date_utc=DAY)
    cpa = record(channel="claude_platform_aws", ws=None)
    report = reconcile([foundry, cpa], [], [ccu], PRICER, today=TODAY)
    codes = residuals(report)
    assert "ccu_single_line" in codes and "no_reporting_api" in codes
    assert verdicts(report) == {"claude_platform_aws": "insufficient_data",
                                "foundry": "reconciled"}


# ---------------------------------------------------------------------------------------------
# residual codes
# ---------------------------------------------------------------------------------------------


def test_priority_tier_usage_is_excluded_not_an_error() -> None:
    aggs, lines = _pair()
    prio = agg({"uncached_input": 1_000_000, "output": 100_000}, tier="priority")
    report = reconcile([*_ledger(), record({"uncached_input": 1_000_000, "output": 100_000},
                                           tier="priority", n=9)],
                       [*aggs, prio], lines, PRICER, today=TODAY)
    assert verdicts(report) == {"anthropic_api": "reconciled"}
    assert residuals(report)["priority_excluded_from_cost_report"] == 5_000_000_000 + 2_500_000_000
    rows = rows_where(report, service_tier="priority")
    assert rows and all(r.residual_code == "priority_excluded_from_cost_report" for r in rows)
    assert report.dollar_coverage_pct == "100"


def test_code_execution_and_session_lines_are_cost_report_only() -> None:
    from tokenbill.core.builders import make_cost_line
    aggs, lines = _pair()
    extra = [make_cost_line(3_000_000_000, model=None, cost_type="code_execution",
                            date_utc=DAY, workspace_id=WS, line_id="ce"),
             make_cost_line(70_000_000, model=None, cost_type="session_usage", date_utc=DAY,
                            line_id="su")]
    report = reconcile(_ledger(), aggs, [*lines, *extra], PRICER, today=TODAY)
    assert residuals(report)["code_execution_cost_report_only"] == 3_070_000_000
    assert verdicts(report) == {"anthropic_api": "reconciled"}


def test_revision_window_rows_are_provisional_and_excluded_with_closed_only() -> None:
    aggs, lines = _pair()
    recent = agg(date=RECENT)
    lines2 = [*lines, *cost_lines(recent, factor=Decimal("0.5"))]
    report = reconcile(_ledger(), [*aggs, recent], lines2, PRICER, today=TODAY)
    assert report.finality is Finality.PROVISIONAL
    assert residuals(report)["revision_window"] > 0
    assert {r.status for r in rows_where(report, date=RECENT)} == {"provisional"}
    assert verdicts(report) == {"anthropic_api": "reconciled"}
    closed = reconcile(_ledger(), [*aggs, recent], lines2, PRICER, today=TODAY,
                       closed_only=True)
    assert not rows_where(closed, date=RECENT) and closed.finality is Finality.FINAL
    assert "revision_window" not in residuals(closed)


def test_provider_tokens_absent_from_the_ledger_are_unobserved_traffic() -> None:
    aggs, lines = _pair()
    other = agg(ws=WS2)
    report = reconcile(_ledger(), [*aggs, other], [*lines, *cost_lines(other)], PRICER,
                       today=TODAY)
    assert residuals(report)["unobserved_traffic"] == sum(c.amount_nano
                                                          for c in cost_lines(other))
    assert verdicts(report) == {"anthropic_api": "reconciled"}
    assert {r.residual_code for r in rows_where(report, workspace=WS2)} == {"unobserved_traffic"}


def test_partial_coverage_prices_the_missing_tokens_as_unobserved() -> None:
    a = agg()
    half = {k: v // 2 for k, v in USAGE.items()}
    report = reconcile([record(half)], [a], cost_lines(a), PRICER, today=TODAY)
    codes = residuals(report)
    assert codes["unobserved_traffic"] > 0 and report.unexplained_nano == 0
    assert report.token_coverage_pct == "50"


def test_subscription_ledger_dollars_are_seat_allowance_unmetered() -> None:
    aggs, lines = _pair()
    sub = record({"uncached_input": 1_000_000}, billing_path="subscription", n=7)
    report = reconcile([*_ledger(), sub], aggs, lines, PRICER, today=TODAY)
    assert residuals(report)["seat_allowance_unmetered"] == 5_000_000_000
    assert verdicts(report) == {"anthropic_api": "reconciled"}
    assert report.dollar_coverage_pct == "100"


def test_parse_remainders_are_cents_rounding() -> None:
    a = agg()
    # every one of the five invoice lines rounded one nano up (within per-line rounding noise)
    lines = [dataclasses.replace(c, amount_nano=c.amount_nano + 1) for c in cost_lines(a)]
    report = reconcile([record()], [a], lines, PRICER, today=TODAY,
                       rounding_remainders={"anthropic-cost-report": Decimal("5E-9"),
                                            "github-ai-usage": Decimal("1")})
    assert residuals(report)["cents_rounding"] == 5
    assert report.unexplained_nano == 0 and "implied_discount" not in residuals(report)
    assert verdicts(report) == {"anthropic_api": "reconciled"}
    bigger = [dataclasses.replace(c, amount_nano=c.amount_nano + 7) for c in cost_lines(a)]
    shifted = reconcile([record()], [a], bigger, PRICER, today=TODAY)
    assert residuals(shifted)["implied_discount"] == 35


def test_unknown_cost_type_is_unmapped_cost_type() -> None:
    from tokenbill.core.builders import make_cost_line
    aggs, lines = _pair()
    odd = make_cost_line(2_000_000_000, model=None, cost_type="fine_tuning", date_utc=DAY,
                         line_id="ft")
    report = reconcile(_ledger(), aggs, [*lines, odd], PRICER, today=TODAY)
    assert residuals(report)["unmapped_cost_type"] == 2_000_000_000
    assert verdicts(report) == {"anthropic_api": "reconciled"}
    assert report.channels[0].mapping_verified is True
    (row,) = rows_where(report, bucket="unmapped")
    assert ("cost_type", "fine_tuning") in row.key


def test_estimated_ledger_parts_explain_a_gap_within_their_range() -> None:
    a = agg({"uncached_input": 1_000_000, "cache_write_1h": 100_000})
    unknown_ttl = record({"uncached_input": 1_000_000, "cache_write_unknown": 100_000})
    report = reconcile([unknown_ttl], [a], cost_lines(a), PRICER, today=TODAY)
    # the ledger point prices the unknown-TTL writes at 5m ($6.25) against a 1h invoice ($10)
    assert residuals(report)["estimated_components"] == 375_000_000
    assert report.unexplained_nano == 0
    assert verdicts(report) == {"anthropic_api": "reconciled"}


def test_default_workspace_null_id_join() -> None:
    a = agg(ws=None)
    report = reconcile([record(ws="wrkspc_default")], [a], cost_lines(a), PRICER, today=TODAY)
    assert "default_workspace_null_id" in residuals(report)
    assert verdicts(report) == {"anthropic_api": "reconciled"}
    assert all(("join", "default_workspace") in r.key for r in rows_where(report, model=OPUS))


def test_ledger_without_workspaces_is_joined_per_model_day() -> None:
    aggs, lines = _pair()
    report = reconcile(_ledger(ws=None), aggs, lines, PRICER, today=TODAY)
    assert verdicts(report) == {"anthropic_api": "reconciled"}
    assert {dict(r.key)["workspace"] for r in report.rows} == {"*"}


# ---------------------------------------------------------------------------------------------
# over-count, rate-card failures, gates
# ---------------------------------------------------------------------------------------------


def test_ledger_three_percent_above_provider_is_an_over_count() -> None:
    aggs, lines = _pair()
    over = {k: v * 103 // 100 for k, v in USAGE.items()}
    ledger = [record(over), *split_records(USAGE, 2, date=DAY2, model=SONNET)]
    report = reconcile(ledger, aggs, lines, PRICER, today=TODAY)
    assert report.over_count_rows == 1
    assert verdicts(report) == {"anthropic_api": "not_reconciled"}
    assert {r.status for r in rows_where(report, date=DAY, model=OPUS)} == {"over"}
    within = {k: v + v // 200 for k, v in USAGE.items()}     # +0.5%: inside max(1%, 1,000)
    ok = reconcile([record(within), *split_records(USAGE, 2, date=DAY2, model=SONNET)],
                   aggs, lines, PRICER, today=TODAY)
    assert ok.over_count_rows == 0


def test_rate_card_error_outside_tolerance_fails_the_channel() -> None:
    a = agg()
    lines = cost_lines(a, factor={"output": Decimal("1.02")})
    report = reconcile([record()], [a], lines, PRICER, today=TODAY)
    assert verdicts(report) == {"anthropic_api": "not_reconciled"}
    assert report.rate_card_error is not None and Decimal(report.rate_card_error[2]) > 0
    assert any(r.status == "unexplained" for r in report.rows)
    loose = reconcile([record()], [a], lines, PRICER, today=TODAY, tolerance_pct="5")
    assert verdicts(loose) == {"anthropic_api": "reconciled"}
    assert {r.status for r in loose.rows if r.rate_card_error_pct not in (None, "0")} == {
        "explained"}


def test_unpriced_models_are_never_within_tolerance() -> None:
    a = agg(model="claude-unknown-9")
    lines = cost_lines(agg())
    lines = [dataclasses.replace(c, model="claude-unknown-9") for c in lines]
    report = reconcile([], [a], lines, PRICER, today=TODAY)
    assert verdicts(report) == {"anthropic_api": "not_reconciled"}
    assert all(r.priced_provider_nano is None for r in report.rows)


def test_invoice_without_provider_usage_fails_the_rate_card() -> None:
    a = agg()
    extra = cost_lines(agg(model=SONNET))
    report = reconcile([record()], [a], [*cost_lines(a), *extra], PRICER, today=TODAY)
    assert verdicts(report) == {"anthropic_api": "not_reconciled"}
    assert {r.priced_provider_nano for r in rows_where(report, model=SONNET)} == {0}


def test_unexplained_gap_above_one_percent_fails() -> None:
    a = agg()
    rec = record({k: v * 97 // 100 for k, v in USAGE.items()})
    report = reconcile([rec], [a], cost_lines(a), PRICER, today=TODAY)
    assert verdicts(report) == {"anthropic_api": "reconciled"}   # a coverage gap, not an error
    cheap = record(USAGE, tier="batch")                          # same tokens, half the dollars
    worse = reconcile([cheap], [a], cost_lines(a), PRICER, today=TODAY)
    assert verdicts(worse) == {"anthropic_api": "not_reconciled"}
    assert worse.unexplained_nano > 0


def test_enterprise_cost_is_channel_total_and_prefers_the_cost_report() -> None:
    a = agg(source_kind="anthropic.enterprise_usage")
    ent = cost_lines(a, factor=Decimal("0.8"), source_kind="anthropic.enterprise_cost",
                     with_list=True)
    report = reconcile([record()], [a], ent, PRICER, today=TODAY)
    assert report.channels[0].mapping_verified is False
    assert report.channels[0].invoice_sources == ("anthropic.enterprise_cost",)
    assert dict(report.effective_discount) == {"anthropic_api:*:*": "0.2"}
    assert verdicts(report) == {"anthropic_api": "reconciled"}
    both = reconcile([record()], [a, agg()], [*ent, *cost_lines(agg())], PRICER, today=TODAY)
    assert both.channels[0].invoice_sources == ("anthropic.cost_report",)


def test_cc_analytics_team_coverage_rows_are_informational() -> None:
    aggs, lines = _pair()
    cc = agg({"uncached_input": 1000, "output": 10}, source_kind="anthropic.cc_analytics",
             ws=None, tier=None, team="payments")
    rec = record({"uncached_input": 900, "output": 10}, agent_product="claude_code",
                 team="payments", n=5)
    report = reconcile([*_ledger(), rec], [*aggs, cc], lines, PRICER, today=TODAY)
    (row,) = rows_where(report, info="anthropic.cc_analytics")
    assert (row.ledger_tokens, row.provider_tokens, row.status) == (910, 1010, "under")
    assert row.residual_code is None


@pytest.mark.parametrize("kw", [{"today": "2026-13-01"}, {"today": 20260923},
                                {"today": TODAY, "tolerance_pct": "x"},
                                {"today": TODAY, "unexplained_pct": Decimal("-1")},
                                {"today": TODAY, "tolerance_pct": True}])
def test_bad_arguments_are_usage_errors(kw: dict) -> None:
    with pytest.raises(UsageError):
        reconcile([], [], [], PRICER, **kw)


def test_bad_inputs_are_usage_errors() -> None:
    with pytest.raises(UsageError):
        reconcile([], ["not an aggregate"], [], PRICER, today=TODAY)  # type: ignore[list-item]
    with pytest.raises(UsageError):
        reconcile([], [], ["not a line"], PRICER, today=TODAY)  # type: ignore[list-item]
    with pytest.raises(UsageError):
        reconcile(["not a record"], [], [], PRICER, today=TODAY)  # type: ignore[list-item]
    with pytest.raises(UsageError):
        reconcile([], [], [], object(), today=TODAY)  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        reconcile([], [], [], PRICER, today=TODAY, rounding_remainders={"aws-cur": 1.5})


def test_output_is_deterministic() -> None:
    aggs, lines = _pair(Decimal("0.85"))
    one = reconcile(_ledger(), aggs, lines, PRICER, today=TODAY, suggest_contract=True)
    two = reconcile(list(reversed(_ledger())), list(reversed(aggs)), list(reversed(lines)),
                    PRICER, today=TODAY, suggest_contract=True)
    assert one == two
