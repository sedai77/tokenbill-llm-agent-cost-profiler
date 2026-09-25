"""Copilot FOCUS rows (addendum §14.3): discounts, reconciliation gate, roles, k-merge, plans."""

from __future__ import annotations

import datetime as _dt
import logging
from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal

import pytest

from tokenbill.copilot import focus as F
from tokenbill.core import builders as b
from tokenbill.core import testing as kit
from tokenbill.core.errors import UsageError
from tokenbill.core.ids import key_id
from tokenbill.core.records import COPILOT_CHANNELS
from tokenbill.core.types import FocusRow, IngestResult, SourceInfo

from .worlds import World, p1_world, p13_world

ORG = b"cp-out-tests-org-key-0123456789ab"
SINCE = (_dt.date(2026, 9, 1) - _dt.date(1970, 1, 1)).days * 86_400_000
UNTIL = SINCE + 30 * 86_400_000
ALL = frozenset(COPILOT_CHANNELS)


def stores(w: World) -> tuple[kit.MemoryStore, kit.MemoryRecordStore]:
    store = kit.MemoryStore(org_key=ORG, pricer=kit.FakePricer())
    src = SourceInfo(source_id="src", adapter="github-ai-usage", name_hmac="h_" + "1" * 20,
                     sha256="src", bytes=1, name_key_id=None, principal_key_id=key_id(ORG))
    store.ingest(IngestResult(source=src, requests=[], sessions=[], events=[],
                              aggregates=list(w.aggs), cost_lines=list(w.lines), outcomes=[],
                              quarantined=[], notes=[], stats={},
                              capabilities=frozenset({"cost", "aggregates", "copilot_billing"})))
    rs = kit.MemoryRecordStore(store)
    rec = IngestResult(source=replace(src, source_id="rec", adapter="github-copilot-seats"),
                       requests=[], sessions=[], events=[], aggregates=[], cost_lines=[],
                       outcomes=[], quarantined=[], notes=[], stats={},
                       capabilities=frozenset({"licenses", "config"}))
    rec.licenses = list(w.licenses)
    rec.config = list(w.config)
    rs.put(rec, principal_key_id=key_id(ORG))
    return store, rs


def rows_of(w: World, **kw) -> F.FocusRowList:
    store, rs = stores(w)
    kw.setdefault("reconciled_channels", ALL)
    kw.setdefault("allow_unreconciled", False)
    kw.setdefault("role", "primary")
    kw.setdefault("k", 5)
    return F.focus_rows(store, [rs], since_ms=SINCE, until_ms=UNTIL, **kw)


def col(row: FocusRow, name: str) -> str:
    return dict(row.columns)[name]


def usd(text: str) -> Decimal:
    return Decimal(text or "0")


def by_channel(rows: Sequence[FocusRow], channel: str) -> list[FocusRow]:
    return [r for r in rows if r.channel == channel]


def test_list_minus_discounts_equals_billed_per_ai_credit_row() -> None:
    rows = rows_of(p1_world())
    ai = [r for r in rows if col(r, "PricingUnit") == "AI Credits"]
    assert ai
    for r in ai:
        discounts = sum(usd(col(r, c)) for c in ("x_DiscountPool", "x_DiscountOther",
                                                  "x_DiscountUnclassified"))
        assert usd(col(r, "ListCost")) - discounts == usd(col(r, "BilledCost"))
        assert col(r, "x_Reconciled") == "true" and r.reconciled
        assert col(r, "ChargeCategory") == "Usage" and col(r, "x_Channel") == "github_copilot"
    total_net = sum(c.amount_nano for c in p1_world().lines if c.source_kind == F.AI_SOURCE)
    assert sum(usd(col(r, "BilledCost")) for r in ai) == Decimal(total_net) / Decimal(10**9)
    assert all(usd(col(r, "x_DiscountPool")) == 0 for r in ai)


def test_classified_pool_discounts_with_gross_is_list() -> None:
    rows = rows_of(p1_world(), gross_is_list=True)
    pool = sum(usd(col(r, "x_DiscountPool")) for r in rows)
    assert pool == Decimal(26_800)
    assert all(usd(col(r, "x_DiscountUnclassified")) == 0 for r in rows)


def test_unreconciled_channel_without_allow_emits_no_rows_and_names_it(caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="tokenbill.copilot.focus"):
        rows = rows_of(p1_world(), reconciled_channels=frozenset({"github_actions"}))
    assert not by_channel(rows, "github_copilot")
    assert by_channel(rows, "github_actions")
    assert rows.skipped_channels == ("github_copilot",)
    assert any("github_copilot" in n for n in rows.notes)
    assert "github_copilot" in caplog.text


def test_allow_unreconciled_fills_billed_with_reconciled_false() -> None:
    rows = rows_of(p1_world(), reconciled_channels=frozenset(), allow_unreconciled=True)
    assert rows and all(not r.reconciled and col(r, "x_Reconciled") == "false" for r in rows)
    assert all(col(r, "x_PriceBasis") == "list" for r in rows)
    assert any(usd(col(r, "BilledCost")) > 0 for r in rows)


def test_enrichment_zeroes_billed_and_effective() -> None:
    rows = rows_of(p1_world(), role="enrichment")
    assert all(col(r, "BilledCost") == "0" and col(r, "EffectiveCost") == "0" for r in rows)
    assert any(usd(col(r, "ListCost")) > 0 for r in rows)


def test_small_teams_merge_and_carry_suppressed_users() -> None:
    w = World()
    w.lines.append(b.make_seat_line("business", "20"))
    for i in range(14):
        w.usage("100", i=i, day=3, team="big" if i < 6 else "mid" if i < 11 else "small")
    rows = rows_of(w)
    ai = [r for r in rows if col(r, "PricingUnit") == "AI Credits"]
    tags = [col(r, "Tags") for r in ai]
    assert any('"team":"big"' in t for t in tags)
    assert not any('"small"' in t or '"mid"' in t for t in tags)   # complementary suppression
    merged = [r for r in ai if col(r, "x_SuppressedUsers") != "0"]
    assert len(merged) == 1 and col(merged[0], "x_SuppressedUsers") == "8"
    assert '"team":"(other: <5 users)"' in col(merged[0], "Tags")
    assert sum(usd(col(r, "BilledCost")) for r in ai) == Decimal(14)


def test_identity_below_k_becomes_one_org_level_row() -> None:
    w = World()
    for i in range(3):
        w.usage("100", i=i, day=4, team=f"t{i}")
    ai = [r for r in rows_of(w) if col(r, "PricingUnit") == "AI Credits"]
    assert len(ai) == 1 and '"team"' not in col(ai[0], "Tags")
    assert col(ai[0], "x_SuppressedUsers") == "3"


def test_unknown_plan_has_no_seat_rows_and_notes_it() -> None:
    rows = rows_of(p13_world())
    assert not [r for r in rows if col(r, "PricingUnit") == "Seats"]
    assert any(F.UNKNOWN_SEATS_NOTE in n for n in rows.notes)
    assert all(F.UNKNOWN_SEATS_NOTE in col(r, "x_Notes") for r in rows)


def test_seat_rows_use_the_verify_fallback() -> None:
    seats = [r for r in rows_of(p1_world()) if col(r, "PricingUnit") == "Seats"]
    assert {col(r, "SkuId") for r in seats} == {"copilot_for_business", "copilot_enterprise"}
    for r in seats:
        assert col(r, "ChargeCategory") == "Usage" and "VERIFY" in col(r, "x_Notes")
    assert sum(usd(col(r, "BilledCost")) for r in seats) == Decimal(26_800)


def test_verified_seat_charge_switch(monkeypatch) -> None:
    monkeypatch.setattr(F, "SEAT_CHARGE_VERIFIED", True)
    seats = [r for r in rows_of(p1_world()) if col(r, "PricingUnit") == "Seats"]
    assert all(col(r, "ChargeCategory") == "Purchase" and col(r, "ChargeFrequency") == "Recurring"
               for r in seats)


def test_auto_discount_is_other_and_columns_are_well_formed() -> None:
    w = World()
    w.lines.append(b.make_seat_line("business", "10"))
    for i in range(5):
        w.usage("1000", i=i, discount="100", model="Auto: Claude Haiku 4.5", team="t")
    rows = rows_of(w)
    ai = [r for r in rows if col(r, "PricingUnit") == "AI Credits"]
    assert sum(usd(col(r, "x_DiscountOther")) for r in ai) == Decimal(5)
    for r in rows:
        assert all(v == v.strip() for _, v in r.columns)
        assert col(r, "BillingPeriodStart") == "2026-09-01T00:00:00Z"
        assert col(r, "BillingPeriodEnd") == "2026-10-01T00:00:00Z"


def test_cross_check_sources_never_become_rows() -> None:
    w = p1_world()
    metered = replace(w.lines[2], line_id="cl_metered", source_kind="github.metered_usage",
                      cost_type="metered.ai_credit")
    w.lines.append(metered)
    base = rows_of(p1_world())
    assert len(rows_of(w)) == len(base)


def test_bad_arguments() -> None:
    with pytest.raises(UsageError):
        rows_of(p1_world(), role="viewer")
    with pytest.raises(UsageError):
        rows_of(p1_world(), k=0)


def test_december_rows_bill_to_january_end() -> None:
    w = World()
    line, _ = b.make_ai_usage_row(date_utc="2026-12-30", credits="10",
                                  principal=b.make_principal(1))
    w.lines.append(line)
    store, rs = stores(w)
    since = (_dt.date(2026, 12, 1) - _dt.date(1970, 1, 1)).days * 86_400_000
    rows = F.focus_rows(store, [rs], since_ms=since, until_ms=since + 31 * 86_400_000,
                        reconciled_channels=ALL, k=1, allow_unreconciled=False, role="primary")
    assert col(rows[0], "BillingPeriodEnd") == "2027-01-01T00:00:00Z"
