"""``merge_reports`` (Copilot amendment A-5, addendum §21.4): disjoint channels, the SPEC §12.4
verdict, coverage and finality over the union, and the union of ``decisions``."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest

from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.extensions import recon_decisions_of
from tokenbill.core.labels import Finality
from tokenbill.core.types import ChannelVerdict, ReconciliationReport, ReconRow
from tokenbill.recon.reconcile import merge_reports, reconcile, reconciled_channels

from .helpers import PRICER, TODAY, agg, cost_lines, record


def _recon() -> ReconciliationReport:
    a = agg()
    return reconcile([record()], [a], cost_lines(a), PRICER, today=TODAY)


def _copilot(verdict: str = "reconciled", *, decisions: tuple = (("convention:s_1", "excl"),),
             finality: Finality = Finality.FINAL, **kw: object) -> ReconciliationReport:
    row = ReconRow(key=(("channel", "github_copilot"), ("month", "2026-08")), ledger_tokens=100,
                   provider_tokens=100, ledger_nano=5_000_000, priced_provider_nano=5_000_000,
                   invoice_nano=5_000_000, rate_card_error_pct="0", coverage_pct="100",
                   status="match", residual_code=None)
    base = dict(
        window=("2026-08-01", "2026-08-31"), tolerance_pct="0.5", unexplained_tolerance_pct="1.0",
        rows=(row,), token_coverage_pct="100", dollar_coverage_pct="100",
        rate_card_error=("0.1", "0.3", "0.4"), over_count_rows=0, effective_discount=(),
        residuals=(("copilot_rounding", 3), ("unobserved_traffic", 1)), unexplained_nano=2,
        channels=(ChannelVerdict("github_copilot", verdict, ("github.ai_usage_report",), False),),
        verdict=verdict, finality=finality, suggested_contract=None, rerun_verdict=None,
        decisions=decisions)
    base.update(kw)
    return ReconciliationReport(**base)  # type: ignore[arg-type]


def test_single_report_is_returned_unchanged() -> None:
    report = _recon()
    assert merge_reports([report]) is report


def test_merge_of_disjoint_channels() -> None:
    mine, theirs = _recon(), _copilot()
    merged = merge_reports([mine, theirs])
    assert [c.channel for c in merged.channels] == ["anthropic_api", "github_copilot"]
    assert merged.verdict == "reconciled"
    assert reconciled_channels(merged) == frozenset({"anthropic_api", "github_copilot"})
    assert merged.decisions == (("convention:s_1", "excl"),)
    assert merged.decisions == recon_decisions_of([mine, theirs])
    assert dict(merged.residuals)["unobserved_traffic"] == 1 + dict(mine.residuals).get(
        "unobserved_traffic", 0)
    assert list(dict(merged.residuals))[-1] == "copilot_rounding"   # unknown codes last
    assert merged.unexplained_nano == mine.unexplained_nano + 2
    assert merged.rate_card_error == ("0.1", "0.3", "0.4")
    assert len(merged.rows) == len(mine.rows) + 1
    assert [r.key for r in merged.rows] == sorted(r.key for r in merged.rows)
    assert merged.window == (min(mine.window[0], "2026-08-01"), max(mine.window[1], "2026-08-31"))
    assert merged.token_coverage_pct == "100" and merged.dollar_coverage_pct == "100"
    assert merged.finality is Finality.FINAL


def test_verdict_recomputed_over_the_union() -> None:
    merged = merge_reports([_recon(), _copilot("not_reconciled")])
    assert merged.verdict == "not_reconciled"
    provisional = merge_reports([_recon(), _copilot(finality=Finality.PROVISIONAL)])
    assert provisional.finality is Finality.PROVISIONAL
    empty = _copilot("insufficient_data", rows=(), channels=(
        ChannelVerdict("github_copilot", "insufficient_data", (), False),))
    assert merge_reports([empty, dataclasses.replace(empty, channels=(
        ChannelVerdict("github_actions", "insufficient_data", (), False),))]).verdict == (
        "insufficient_data")


def test_extension_rows_without_a_channel_dim_count_as_spend() -> None:
    row = ReconRow(key=(("month", "2026-08"), ("entity", "org:acme"), ("layer", "L2")),
                   ledger_tokens=10, provider_tokens=10, ledger_nano=7, priced_provider_nano=7,
                   invoice_nano=9, rate_card_error_pct=None, coverage_pct=None,
                   status="unexplained", residual_code=None)
    failing = _copilot("not_reconciled", rows=(row,))
    merged = merge_reports([_recon(), failing])
    assert merged.verdict == "not_reconciled"
    idle = dataclasses.replace(row, ledger_tokens=0, ledger_nano=0)
    assert merge_reports([_recon(), _copilot("not_reconciled", rows=(idle,))]).verdict == (
        "reconciled")


def test_conflicting_decisions_raise() -> None:
    with pytest.raises(ContractViolation):
        merge_reports([_copilot(), _copilot(decisions=(("convention:s_1", "incl"),),
                                            channels=(ChannelVerdict("github_actions",
                                                                     "reconciled", (), True),))])


def test_overlapping_channels_and_tolerances_raise() -> None:
    with pytest.raises(ContractViolation):
        merge_reports([_copilot(), _copilot()])
    with pytest.raises(ContractViolation):
        merge_reports([_recon(), _copilot(tolerance_pct="2")])
    with pytest.raises(UsageError):
        merge_reports([])
    with pytest.raises(ContractViolation):
        merge_reports([_recon(), "not a report"])  # type: ignore[list-item]


def test_rerun_and_suggestion_are_carried() -> None:
    a = agg()
    discounted = reconcile([record()], [a], cost_lines(a, factor=Decimal("0.85")), PRICER,
                           today=TODAY, suggest_contract=True)
    merged = merge_reports([discounted, _copilot()])
    assert merged.suggested_contract == discounted.suggested_contract
    assert merged.rerun_verdict == "reconciled"
    failing = merge_reports([discounted, _copilot("not_reconciled")])
    assert failing.rerun_verdict == "not_reconciled"
    assert merge_reports([_recon(), _copilot()]).rerun_verdict is None
    with pytest.raises(ContractViolation):
        other = dataclasses.replace(discounted.suggested_contract, name="other")
        merge_reports([discounted, _copilot(suggested_contract=other)])
