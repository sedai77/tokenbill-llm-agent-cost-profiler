"""Staleness and verification (SPEC §6.7): snapshot, live page (injected opener), feed cross-check,
extension verifiers, reports."""

from __future__ import annotations

import dataclasses
import json
import sys
import types
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tokenbill.core import extensions
from tokenbill.core.errors import ContractViolation, PricingError, SourceError, UsageError
from tokenbill.core.records import UsageBuckets
from tokenbill.core.types import Discrepancy
from tokenbill.rates import verify
from tokenbill.rates.engine import RateCard
from tokenbill.rates.schema import make_layer, model_price_layer
from tokenbill.rates.verify import (
    crosscheck_feed,
    diff_layers,
    load_snapshot,
    model_id_of,
    parse_pricing_page,
    promotions_ending,
    show_report,
    stale_rows,
    verify_extensions,
    verify_live,
    verify_report,
    verify_snapshot,
)

from .helpers import FIXTURES, builtin, ctx, layer, row, ts

PAGE = (FIXTURES / "pricing_page.md").read_text(encoding="utf-8")


def _with_row(target_id: str, **changes: Any):  # type: ignore[no-untyped-def]
    rows = [dataclasses.replace(r, **changes) if r.row_id == target_id else r
            for r in builtin().rows]
    return make_layer("builtin@test", rows, builtin().modifiers, as_of="2026-09-24")


def test_snapshot_has_zero_discrepancies() -> None:
    assert verify_snapshot(builtin()) == []
    snap = load_snapshot()
    assert snap["as_of"] == "2026-09-23" and len(snap["models"]) == 18
    assert {m["model"] for m in snap["fast"]} == {"claude-opus-5-5", "claude-opus-5",
                                                  "claude-opus-4-8"}


def test_one_injected_discrepancy_is_reported_with_its_row_id() -> None:
    target = "anthropic/anthropic_api/claude-sonnet-4-6/2026-02-17"
    drifted = _with_row(target, output_usd_per_mtok=Decimal("16.00"))
    found = verify_snapshot(drifted)
    assert len(found) == 2    # the output price and the batch output price it implies
    assert {(d.row_id, d.field) for d in found} == {(target, "output"), (target, "batch.output")}
    out = next(d for d in found if d.field == "output")
    assert (out.ours, out.theirs, out.authoritative) == ("16", "15", True)
    assert out.source.endswith("pricing.md")


def test_snapshot_flags_missing_and_unlisted_rows(tmp_path: Path) -> None:
    snap = load_snapshot()
    snap["models"].append({"model": "claude-sonnet-5-5", "input": "3", "output": "15"})
    snap["models"] = [m for m in snap["models"] if m["model"] != "claude-opus-4"]
    snap["modifiers"] = {"inference_geo_us": "1.2", "web_search_per_request": "0.02"}
    path = tmp_path / "snap.json"
    path.write_text(json.dumps(snap), encoding="utf-8")
    found = {(d.row_id, d.field) for d in verify_snapshot(builtin(), path)}
    assert ("(missing) anthropic_api/claude-sonnet-5-5", "row") in found   # announced, now priced
    assert ("anthropic/anthropic_api/claude-opus-4/2025-05-14", "row") in found
    assert ("modifier:inference_geo=us", "factor") in found
    assert ("anthropic/anthropic_api/claude-opus-5-5/2026-09-22", "per_request.web_search") in found


def test_snapshot_flags_batch_and_fast_drift() -> None:
    mods = [dataclasses.replace(m, factor=Decimal("0.6")) if m.modifier_id == "anthropic.batch"
            else m for m in builtin().modifiers if m.modifier_id != "anthropic.fast.opus-5-5"]
    lay = make_layer("builtin@test", builtin().rows, mods, as_of="2026-09-24")
    fields = {d.field for d in verify_snapshot(lay)}
    assert {"batch.input", "batch.output", "fast.input", "fast.output"} <= fields


def test_load_snapshot_errors(tmp_path: Path) -> None:
    with pytest.raises(SourceError):
        load_snapshot(tmp_path / "none.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    with pytest.raises(PricingError):
        load_snapshot(bad)
    bad.write_text(json.dumps({"schema": "x"}), encoding="utf-8")
    with pytest.raises(PricingError):
        load_snapshot(bad)
    with pytest.raises(UsageError):
        verify_snapshot("builtin")  # type: ignore[arg-type]


def test_parse_pricing_page_fixture() -> None:
    snap = parse_pricing_page(PAGE)
    assert len(snap["models"]) == 18 and len(snap["batch"]) == 18 and len(snap["fast"]) == 3
    opus = next(m for m in snap["models"] if m["model"] == "claude-opus-5-5")
    assert opus == {"model": "claude-opus-5-5", "input": "4", "cache_write_5m": "5",
                    "cache_write_1h": "8", "cache_read": "0.2", "output": "20"}
    assert snap["modifiers"] == {"inference_geo_us": "1.1", "web_search_per_request": "0.01"}
    with pytest.raises(PricingError):
        parse_pricing_page("# nothing here\n| a | b |\n| - | - |\n| x | y |\n")
    with pytest.raises(PricingError):
        parse_pricing_page(b"bytes")  # type: ignore[arg-type]


def test_model_id_of() -> None:
    assert model_id_of("Claude Opus 5.5") == ["claude-opus-5-5"]
    assert model_id_of("Claude Mythos 5.1 ([limited availability](https://x))") == \
        ["claude-mythos-5-1"]
    assert model_id_of("Claude Opus 5 / Claude Opus 4.8") == ["claude-opus-5", "claude-opus-4-8"]
    assert model_id_of("**Claude Haiku 3.5**<sup>2</sup>") == ["claude-haiku-3-5"]
    assert model_id_of("Batch input") == [] and model_id_of("Claude Mythos Preview") == []


def test_verify_live_with_an_injected_opener() -> None:
    calls: list[tuple[str, int]] = []

    def opener(url: str, timeout: int) -> bytes:
        calls.append((url, timeout))
        return PAGE.encode("utf-8")

    assert verify_live(builtin(), opener=opener, today="2026-09-24") == []
    assert calls == [(verify.PRICING_MD_URL, 30)]
    drifted = PAGE.replace("| Claude Opus 5.5 | $4 / MTok |", "| Claude Opus 5.5 | $4.5 / MTok |")
    found = verify_live(builtin(), opener=lambda u, t: drifted.encode(), today="2026-09-24")
    assert [(d.row_id, d.field, d.theirs) for d in found] == [
        ("anthropic/anthropic_api/claude-opus-5-5/2026-09-22", "input", "4.5")]


def test_verify_live_refuses_insecure_or_broken_pages() -> None:
    with pytest.raises(UsageError):
        verify_live(builtin(), "http://platform.claude.com/pricing.md", opener=lambda u, t: b"")
    with pytest.raises(SourceError):
        verify_live(builtin(), opener=lambda u, t: b"\xff\xfe", today="2026-09-24")
    with pytest.raises(SourceError):
        verify_live(builtin(), opener=lambda u, t: "text", today="2026-09-24")  # type: ignore
    with pytest.raises(PricingError):
        verify_live(builtin(), opener=lambda u, t: b"no tables", today="2026-09-24")


def test_verify_live_default_date_is_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify, "_today", lambda: "2026-09-24")
    assert verify_live(builtin(), opener=lambda u, t: PAGE.encode()) == []


def test_crosscheck_feed_litellm_and_openrouter() -> None:
    litellm = (FIXTURES / "litellm_model_prices.json").read_text(encoding="utf-8")
    found = crosscheck_feed(builtin(), litellm, today="2026-09-24")
    assert found and all(not d.authoritative for d in found)
    fields = {(d.row_id, d.field, d.theirs) for d in found}
    assert ("anthropic/anthropic_api/claude-sonnet-5/2026-06-30", "input", "3") in fields
    assert ("openai/openai_api/gpt-5.6-sol/2026-08-21", "input", "2") in fields
    assert all("claude-opus-5-5" not in d.row_id for d in found)   # agrees with the registry
    # parsed JSON with floats (json.loads) is read through repr, exactly
    assert crosscheck_feed(builtin(), json.loads(litellm), today="2026-09-24") == found
    openrouter = json.loads((FIXTURES / "openrouter_models.json").read_text(encoding="utf-8"))
    found = crosscheck_feed(builtin(), openrouter, today="2026-09-24")
    assert [(d.row_id, d.field, d.source) for d in found] == [
        ("openai/openai_api/gpt-5.6-sol/2026-08-21", "input", "openrouter:gpt-5.6-sol"),
        ("openai/openai_api/gpt-5.6-sol/2026-08-21", "output", "openrouter:gpt-5.6-sol")]


@pytest.mark.parametrize("feed", ["{", b"\xff", 3, None, [1, 2], {"data": "x"},
                                  {"m": {"litellm_provider": "anthropic",
                                         "input_cost_per_token": "abc"}}])
def test_crosscheck_feed_never_fails(feed: Any) -> None:
    found = crosscheck_feed(builtin(), feed, today="2026-09-24")
    assert all(isinstance(d, Discrepancy) and not d.authoritative for d in found)


def test_crosscheck_feed_unreadable_is_one_warning() -> None:
    assert crosscheck_feed(builtin(), "{", today="2026-09-24") == [
        Discrepancy(row_id="(feed)", field="format", ours="", theirs="unreadable", source="feed",
                    authoritative=False)]
    assert crosscheck_feed(builtin(), "{}", today="not-a-date")[0].field == "format"


def test_verify_extensions_runs_the_extension_verifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    notes: list[Any] = []
    if not extensions.rate_verifiers():
        assert verify_extensions(builtin(), notes=notes) == []
        assert [n.code for n in notes] == ["dq.extension_unavailable"]
    module = types.ModuleType("tb_test_rate_verifier")
    seen: dict[str, Any] = {}

    def good(layer: Any, *, snapshot: Any = None, live: bool = False, opener: Any = None) -> list:
        seen.update(layer=layer, live=live)
        return [Discrepancy(row_id="github/x", field="input", ours="1", theirs="2",
                            source="yml", authoritative=True)]

    module.good = good  # type: ignore[attr-defined]
    module.bad = lambda layer, **kw: "oops"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tb_test_rate_verifier", module)
    monkeypatch.setattr(extensions, "rate_verifiers",
                        lambda notes=None: ["tb_test_rate_verifier:good"])
    found = verify_extensions(builtin(), live=True)
    assert [d.row_id for d in found] == ["github/x"] and seen["live"] is True
    report = verify_report(builtin(), today="2026-09-24")
    assert not report.ok and report.kind == "verify" and len(report.discrepancies) == 1
    monkeypatch.setattr(extensions, "rate_verifiers",
                        lambda notes=None: ["tb_test_rate_verifier:bad"])
    with pytest.raises(ContractViolation):
        verify_extensions(builtin())


def test_verify_report_offline_and_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(extensions, "rate_verifiers", lambda notes=None: [])
    report = verify_report(builtin(), today="2026-09-24")
    assert report.ok and report.discrepancies == () and report.stale_rows == ()
    assert report.rows == builtin().rows
    live = verify_report(builtin(), today="2026-09-24", live=True,
                         opener=lambda u, t: PAGE.replace("$20 / MTok |", "$21 / MTok |").encode())
    assert not live.ok and all(d.authoritative for d in live.discrepancies)


def test_stale_rows_of_a_layer() -> None:
    old = layer([row(verified_on="2026-07-01")])
    assert stale_rows(old, today="2026-08-30") == ("test/anthropic_api/claude-test-1/2026-01-01",)
    assert stale_rows(old, today="2026-08-15") == ()
    assert stale_rows(model_price_layer([("m", "1", "1")]), today="2030-01-01") == ()
    assert show_report(old, today="2026-08-30").stale_rows == stale_rows(old, today="2026-08-30")
    # the built-in registry becomes stale 46 days after its verification
    assert stale_rows(builtin(), today="2026-11-08")
    assert not stale_rows(builtin(), today="2026-11-06")


def test_stale_rate_note_for_a_row_verified_60_days_before_the_run() -> None:
    old = layer([row(verified_on="2026-07-24")])
    card = RateCard([old])
    card.price_usage(UsageBuckets(output=10), ctx("claude-test-1"), ts_ms=ts("2026-09-22"))
    notes = card.data_quality(today="2026-09-22")
    assert [(n.code, n.count) for n in notes] == [("dq.stale_rate", 1)]
    assert "claude-test-1" in notes[0].detail


def test_diff_layers() -> None:
    a = layer([row()], name="user:a")
    b = layer([row(usd_per_mtok={"input": "2.00", "output": "11.00"}, enabled=False),
               row(row_id="new", model="claude-new")], name="user:b")
    report = diff_layers(a, b)
    assert report.kind == "diff" and not report.ok
    assert {(d.row_id, d.field) for d in report.discrepancies} == {
        ("test/anthropic_api/claude-test-1/2026-01-01", "output"),
        ("test/anthropic_api/claude-test-1/2026-01-01", "enabled"), ("new", "row")}
    assert diff_layers(a, a).ok


def test_promotions_ending() -> None:
    assert [p.promotion_id for p in promotions_ending("2026-11-10")] == \
        ["openai.gpt-5.6-sol.2026-08"]
    assert promotions_ending("2026-09-24") == []
    assert [p.promotion_id for p in promotions_ending("2026-12-20", days=14)] == [
        "github.gemini-3.6-flash.2026", "github.gemini-3.7-flash.2026",
        "github.gemini-3.8-flash.2026"]
