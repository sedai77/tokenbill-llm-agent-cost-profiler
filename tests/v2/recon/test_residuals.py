"""``recon.residuals.classify`` on hand-built rows (SPEC §12.3 order and the bridge identity)."""

from __future__ import annotations

from tokenbill.core.types import ReconRow
from tokenbill.recon.residuals import RESIDUAL_CODES, classify, classify_rows, residual_order


def row(bucket: str = "output", *, date: str = "2026-08-10", ws: str = "w", model: str = "m",
        lt: int | None = 100, pt: int | None = 100, led: int | None = 1000,
        priced: int | None = 1000, inv: int | None = 1000, **extra: str) -> ReconRow:
    key = [("channel", extra.pop("channel", "anthropic_api")), ("date", date), ("workspace", ws),
           ("model", model), ("bucket", bucket), *extra.items()]
    return ReconRow(key=tuple(key), ledger_tokens=lt, provider_tokens=pt, ledger_nano=led,
                    priced_provider_nano=priced, invoice_nano=inv, rate_card_error_pct=None,
                    coverage_pct=None, status="match", residual_code=None)


def run(rows: list[ReconRow], **kw: object) -> tuple[dict[str, int], int]:
    args = {"ledger_estimated_nano": {}, "allowance_nano": {}, "provisional_dates": frozenset(),
            "remainders_nano": 0}
    args.update(kw)
    codes, unexplained = classify(rows, **args)  # type: ignore[arg-type]
    assert [c for c, _ in codes] == sorted((c for c, _ in codes), key=residual_order)
    return dict(codes), unexplained


def test_codes_are_the_spec_order() -> None:
    assert RESIDUAL_CODES[0] == "priority_excluded_from_cost_report"
    assert RESIDUAL_CODES[-1] == "cloud_credits" and len(RESIDUAL_CODES) == 13


def test_match_has_no_residual() -> None:
    assert run([row()]) == ({}, 0)


def test_special_rows() -> None:
    rows = [row(inv=None, led=0, service_tier="priority", priced=700),
            row("code_execution", lt=None, pt=None, led=None, priced=None, inv=50),
            row("unmapped", lt=None, pt=None, led=None, priced=None, inv=30),
            row("credits", lt=None, pt=None, led=None, priced=None, inv=-20),
            row("unmapped", date="2026-09-20", lt=None, pt=None, led=None, priced=None, inv=9)]
    codes, unexplained = run(rows, provisional_dates=frozenset({"2026-09-20"}))
    assert codes == {"priority_excluded_from_cost_report": 700,
                     "code_execution_cost_report_only": 50, "unmapped_cost_type": 30,
                     "cloud_credits": -20, "revision_window": 9}
    assert unexplained == 0


def test_implied_discount_and_unobserved_split() -> None:
    # provider 1000 tokens priced 10_000 nano×10^4; ledger saw 800 tokens; invoice 15% below
    rows = [row(lt=800, pt=1000, led=80_000_000, priced=100_000_000, inv=85_000_000)]
    codes, unexplained = run(rows)
    assert codes == {"unobserved_traffic": 20_000_000, "implied_discount": -15_000_000}
    assert unexplained == 0


def test_estimated_components_and_unexplained() -> None:
    rows = [row(led=90_000_000, priced=100_000_000, inv=100_000_000)]
    key = rows[0].key
    codes, unexplained = run(rows, ledger_estimated_nano={key: 4_000_000})
    assert codes == {"estimated_components": 4_000_000} and unexplained == 6_000_000


def test_allowance_is_a_magnitude_beside_the_gap() -> None:
    rows = [row()]
    codes, unexplained = run(rows, allowance_nano={rows[0].key: 555})
    assert codes == {"seat_allowance_unmetered": 555} and unexplained == 0
    detail = classify_rows(rows, ledger_estimated_nano={}, allowance_nano={rows[0].key: 555},
                           provisional_dates=frozenset(), remainders_nano=0)
    assert detail.row_codes[rows[0].key] == "seat_allowance_unmetered"
    assert detail.row_status[rows[0].key] == "explained"


def test_cents_rounding_claims_up_to_the_remainder() -> None:
    rows = [row(led=1000, priced=1000, inv=1009)]
    assert run(rows, remainders_nano=5) == ({"cents_rounding": 5}, 4)
    assert run(rows, remainders_nano=-50) == ({"cents_rounding": 9}, 0)


def test_channel_specific_codes() -> None:
    ccu = [row(channel="foundry", led=100, priced=None, pt=None, inv=150)]
    assert run(ccu)[0] == {"ccu_single_line": 50}
    cpa = [row(channel="claude_platform_aws", pt=None, priced=None, inv=None, led=40)]
    assert run(cpa) == ({"no_reporting_api": -40}, 0)
    joined = [row(led=90, priced=None, pt=None, inv=100, join="default_workspace")]
    assert run(joined) == ({"default_workspace_null_id": 10}, 0)


def test_info_rows_are_skipped_and_groups_span_buckets() -> None:
    info = row(info="anthropic.cc_analytics", inv=5)
    a = row("output", lt=100, pt=0, led=50, priced=0, inv=None)
    b = row("cache_write_5m", lt=0, pt=100, led=None, priced=50, inv=50)
    codes, unexplained = run([info, a, b])
    assert unexplained == 0 and codes == {}     # one model-day: the buckets offset


def test_bridge_identity_per_group() -> None:
    rows = [row(lt=500, pt=1000, led=40, priced=100, inv=70),
            row("cache_read", lt=500, pt=500, led=10, priced=10, inv=12),
            row(date="2026-08-11", lt=0, pt=10, led=0, priced=5, inv=6)]
    codes, unexplained = run(rows)
    gap = sum((r.invoice_nano or 0) - (r.ledger_nano or 0) for r in rows)
    assert sum(codes.values()) + unexplained == gap
