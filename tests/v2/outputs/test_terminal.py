"""Terminal renderer (SPEC §14.2): every slot, label chips, width, sanitizing, snapshots.

Snapshots live in ``snapshots/``; regenerate with ``TOKENBILL_UPDATE_SNAPSHOTS=1``."""

from __future__ import annotations

import dataclasses
import os
import re
from pathlib import Path

import pytest

from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.labels import (
    Basis,
    Calibration,
    Evidence,
    Figure,
    Finality,
    estimated,
    exact,
    unpriced,
)
from tokenbill.outputs import terminal as T

from . import extension
from .sample import empty_result, full_result, unpriced_bill, usd

SNAPSHOTS = Path(__file__).parent / "snapshots"
USD = 10**9


def snapshot(name: str, text: str) -> None:
    path = SNAPSHOTS / name
    if os.environ.get("TOKENBILL_UPDATE_SNAPSHOTS"):
        SNAPSHOTS.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
    assert text == path.read_text(encoding="utf-8")


def test_full_result_snapshot() -> None:
    snapshot("terminal_full.txt", T.render_terminal(full_result()))


def test_narrow_snapshot() -> None:
    snapshot("terminal_width80.txt", T.render_terminal(full_result(), width=80))


@pytest.mark.parametrize("width", [40, 64, 80, 100, 132])
def test_width_is_respected(width: int) -> None:
    text = T.render_terminal(full_result(), width=width)
    assert max(len(line) for line in text.splitlines()) <= width


SLOTS = {
    "bill": "BILL", "data_quality": "DATA QUALITY", "calibration": "CALIBRATION (MODEL GATE)",
    "reconciliation": "RECONCILIATION", "findings": "TOP RECOVERABLE",
    "action_plan": "ACTION PLAN", "policy_packs": "POLICY PACKS", "replays": "WHATIF",
    "measure_plan": "MEASURE PLAN", "measurements": "MEASUREMENTS", "ab": "AB (PAIRED",
    "check": "CHECK (FAILED)", "pricing": "PRICING (VERIFY", "receipts": "RECEIPTS",
    "notes": "NOTES",
}


@pytest.mark.parametrize("slot", sorted(SLOTS))
def test_each_slot_renders_its_own_section(slot: str) -> None:
    full = full_result()
    text = T.render_terminal(empty_result(**{slot: getattr(full, slot)}))
    assert SLOTS[slot] in text
    others = [h for s, h in SLOTS.items() if s != slot and h not in SLOTS[slot]]
    assert not [h for h in others if f"\n{h}" in text]
    assert text.rstrip().endswith("never billed")          # the legend line


def test_label_chips_follow_the_spec_examples() -> None:
    assert T.chip(exact(usd("41203.17"), Basis.LIST)) == "$41,203.17 exact·list"
    assert T.chip(exact(usd("9880.12"), Basis.LIST_EQUIVALENT), kind="allowance") == (
        "$9,880.12 allowance·list-equivalent (not billed)")
    proj = estimated(usd("6100"), Basis.LIST, low=usd("2900"), high=usd("8400"),
                     calibration=Calibration.CALIBRATED)
    assert T.chip(proj, per="/mo", range_label="p10–p90") == (
        "~$6,100/mo est. (p10–p90 $2,900–$8,400) calibrated")
    assert T.chip(unpriced("unknown model"), unpriced_count=3) == "unpriced (3 inferences)"
    assert T.chip(unpriced("unknown model")) == "unpriced"
    small = estimated(usd("12.5"), Basis.LIST_EQUIVALENT, note="x", upper_bound=True)
    assert T.chip(small) == "~$12.50 est.·list-equivalent (not billed) uncalibrated upper bound"
    measured = Figure(nano=usd("2"), evidence=Evidence.MEASURED, basis=Basis.LIST,
                      low_nano=usd("1"), high_nano=usd("3"), ci_level_pct=90,
                      finality=Finality.PROVISIONAL)
    assert T.chip(measured) == "$2.00 measured·list (90% CI $1.00–$3.00) provisional"
    assert T.chip(exact(1_500_000, Basis.INVOICE)) == "$0.0015 exact·invoice"
    assert T.chip(exact(-1_500_000, Basis.LIST)) == "-$0.0015 exact·list"
    with pytest.raises(ContractViolation):
        T.chip(5)  # type: ignore[arg-type]


def test_pct_and_usd_helpers() -> None:
    assert T.pct("0.998") == "99.8%"
    assert T.pct(None) == "n/a" and T.pct("x") == "n/a" and T.pct("NaN") == "n/a"
    assert T.usd(0) == "$0.00" and T.usd(usd("1234.5"), whole=True) == "$1,234"


def test_unpriced_bill_prints_unpriced_never_zero() -> None:
    text = T.render_terminal(empty_result(bill=unpriced_bill()))
    assert "exact      unpriced (3 inferences)" in text
    assert "unpriced (3 inferences) · 3,600 tokens" in text
    assert "$0.00 exact" not in text


def test_allowance_and_pool_are_apart_from_the_bill() -> None:
    text = T.render_terminal(full_result())
    assert "allowance  $9,880.12 allowance·list-equivalent (not billed)" in text
    assert "pool       $321.40 Copilot credits·list-equivalent (not billed)" in text
    assert "ALLOWANCE / POOL HEADROOM" in text


def test_billed_lines_refuse_estimates_and_allowance() -> None:
    r = full_result()
    for bad in (estimated(5, Basis.LIST, note="x"), exact(5, Basis.LIST_EQUIVALENT),
                exact(5, Basis.PROVIDER_ESTIMATE)):
        broken = dataclasses.replace(r.bill, total=dataclasses.replace(r.bill.total, exact=bad))
        with pytest.raises(ContractViolation):
            T.render_terminal(dataclasses.replace(r, bill=broken))
    le_est = dataclasses.replace(r.bill.total, estimated=estimated(5, Basis.LIST_EQUIVALENT,
                                                                   note="x"))
    with pytest.raises(ContractViolation):
        T.render_terminal(dataclasses.replace(r, bill=dataclasses.replace(r.bill, total=le_est)))
    billed_allowance = dataclasses.replace(r.bill.total, allowance=exact(5, Basis.LIST))
    with pytest.raises(ContractViolation):
        T.render_terminal(dataclasses.replace(
            r, bill=dataclasses.replace(r.bill, total=billed_allowance)))


def test_names_are_sanitized_and_pseudonyms_scrubbed() -> None:
    r = full_result(notes=("team \x1b[31mred\x1b[0m\u202e", "p_0123456789abcdef0123 was here"))
    text = T.render_terminal(r)
    assert "\x1b" not in text and "\u202e" not in text
    assert "p_0123456789abcdef0123" not in text and "(pseudonym) was here" in text
    assert "platform\x1b" not in text


def test_users_unknown_rows_say_so() -> None:
    assert "users unknown" in T.render_terminal(full_result())


def test_rejects_bad_arguments() -> None:
    with pytest.raises(UsageError):
        T.render_terminal(full_result(), width=39)
    with pytest.raises(ContractViolation):
        T.render_terminal("x")  # type: ignore[arg-type]


def test_findings_rank_by_shapley_and_split_headroom() -> None:
    text = T.render_terminal(full_result())
    top = text.index("TOP RECOVERABLE")
    assert text.index("1. TTL expiry", top) < text.index("2. Fast mode", top)
    assert "[needs-eval, trade-off]" in text
    assert "DATA-QUALITY FINDINGS" in text


def test_many_findings_are_truncated_with_a_pointer() -> None:
    r = full_result()
    many = tuple(dataclasses.replace(r.findings[0], finding_id=f"f_{i:03d}") for i in range(15))
    text = T.render_terminal(dataclasses.replace(r, findings=many))
    assert "(+5 more findings; see --format json)" in text


def test_reconciliation_badges_without_reconciliation() -> None:
    r = full_result(reconciliation=None)
    assert "reconciliation: not run" in T.render_terminal(r)


def test_extension_sections_are_appended(monkeypatch: pytest.MonkeyPatch) -> None:
    extension.install(monkeypatch)
    text = T.render_terminal(full_result(copilot=extension.summary()))
    assert "COPILOT BILL" in text and "seats 12" in text
    assert "COPILOT BILL" not in T.render_terminal(full_result())


def test_output_is_deterministic() -> None:
    assert T.render_terminal(full_result()) == T.render_terminal(full_result())
    assert re.search(r"\d{4}-\d{2}-\d{2} → \d{4}-\d{2}-\d{2}", T.render_terminal(empty_result()))
