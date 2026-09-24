"""L1 convention decision (the power rule, addendum §12 L1, §12.1) and the per-file decision keys."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tokenbill.copilot import recon
from tokenbill.core.errors import UsageError

from . import world as w

MODELS = ("Claude Sonnet 5", "GPT-5.5", "Claude Opus 4.8")


def _file(convention: str, dates: list[str], *, read: int = 200_000, write: int = 5_000,
          source: str | None = None, fetched_ms: int = 1, **kw):
    lines, aggs = [], []
    for date in dates:
        for i, model in enumerate(MODELS):
            line, agg = w.row(date, model, convention=convention, read=read, write=write,
                              team=f"t{i}", uncached=60_000 + 1_000 * i, **kw)
            lines.append(line)
            aggs.append(agg)
    if source is not None:
        aggs.extend(w.coverage(source, d, lines, fetched_ms=fetched_ms) for d in dates)
    return lines, aggs


@pytest.mark.parametrize("convention", ["excl", "incl"])
def test_power_rule_names_the_convention_blind(convention: str) -> None:
    store, rs = w.world()
    lines, aggs = _file(convention, w.days("2026-09", 6), source="s_file")
    w.ingest(store, lines=lines, aggs=aggs)
    report = w.reconcile(store, rs)
    assert w.decisions(report)["convention:s_file"] == convention
    assert w.verdicts(report)["github_copilot"] == "reconciled"
    assert report.unexplained_nano == 0
    l1 = w.rows_of(report, layer="L1", check="rate_card")
    assert l1 and all(dict(r.key)["convention"] == convention for r in l1)
    assert {r.status for r in l1} <= {"match", "within_tolerance"}
    assert recon.verdict_label(report, "github_copilot") == \
        "reconciled (synthetic; schema unverified)"
    assert w.decisions(report)["gross_is_list:enterprise:2026-09"] == "true"


def test_the_same_totals_under_the_wrong_convention_fail() -> None:
    """Pricing an ``incl`` file as ``excl`` would bill reads and writes twice: the decision is what
    keeps L1 green, and the decided convention is the file's own (both totals are identical)."""
    excl_lines, _ = _file("excl", w.days("2026-09", 3))
    incl_lines, _ = _file("incl", w.days("2026-09", 3))
    assert sum(c.list_amount_nano or 0 for c in excl_lines) == \
        sum(c.list_amount_nano or 0 for c in incl_lines)


def test_report_without_cache_tokens_is_undecidable_and_totals_only() -> None:
    store, rs = w.world()
    lines, aggs = _file("excl", w.days("2026-09", 4), read=0, write=0, source="s_nocache")
    w.ingest(store, lines=lines, aggs=aggs)
    report = w.reconcile(store, rs)
    assert w.decisions(report)["convention:s_nocache"] == "undecidable"
    # cache-free: excl and incl price alike, so L1 is still evaluated
    assert w.verdicts(report)["github_copilot"] == "reconciled"
    assert recon.verdict_label(report, "github_copilot") == \
        "reconciled (totals only; synthetic; schema unverified)"
    notes = {n.code: n for n in recon.report_notes(report)}
    assert notes[recon.DQ_CONVENTION_UNDECIDABLE].count == 1
    assert recon.DQ_RECON_SCHEMA_UNVERIFIED in notes
    assert all(dict(r.key)["convention"] == "undecidable"
               for r in w.rows_of(report, layer="L1", check="rate_card"))


def test_small_cache_share_is_undecidable_and_never_gates() -> None:
    """Power passes (read ≥ 5% of input) but the conventions differ by < 2 × tolerance: the file is
    undecidable, its rows are computed under excl (R-E46) and excluded from the verdict even when
    they would fail (never reconciled beyond totals only)."""
    store, rs = w.world()
    lines, aggs = [], []
    for date in w.days("2026-09", 3):
        line, agg = w.row(date, uncached=1_000_000, read=60_000, write=0, output=10_000_000,
                          convention="incl", scale=Decimal("1.05"))
        lines.append(line)
        aggs.append(agg)
    aggs.extend(w.coverage("s_weak", d, lines) for d in w.days("2026-09", 3))
    w.ingest(store, lines=lines, aggs=aggs)
    report = w.reconcile(store, rs)
    assert w.decisions(report)["convention:s_weak"] == "undecidable"
    assert w.decisions(report)["gross_is_list:enterprise:2026-09"] == "unknown"
    assert w.verdicts(report)["github_copilot"] == "reconciled"
    assert "totals only" in recon.verdict_label(report, "github_copilot")
    assert {r.status for r in w.rows_of(report, layer="L1")} == {"unexplained"}


def test_three_files_get_three_decisions() -> None:
    store, rs = w.world()
    a_lines, a_aggs = _file("excl", ["2026-09-01", "2026-09-02"], source="s_excl")
    b_lines, b_aggs = _file("incl", ["2026-09-10", "2026-09-11"], source="s_incl")
    c_lines, c_aggs = _file("excl", ["2026-09-20", "2026-09-21"], read=0, write=0,
                            source="s_none")
    for i, (ls, ag) in enumerate(((a_lines, a_aggs), (b_lines, b_aggs), (c_lines, c_aggs))):
        w.ingest(store, lines=ls, aggs=ag, source_id=f"src{i}")
    report = w.reconcile(store, rs)
    d = w.decisions(report)
    assert (d["convention:s_excl"], d["convention:s_incl"], d["convention:s_none"]) == \
        ("excl", "incl", "undecidable")
    assert w.verdicts(report)["github_copilot"] == "reconciled"
    assert list(report.decisions) == sorted(report.decisions)


def test_days_without_coverage_fall_back_to_the_report_source() -> None:
    store, rs = w.world()
    lines, aggs = _file("excl", w.days("2026-09", 2))
    w.ingest(store, lines=lines, aggs=aggs)
    report = w.reconcile(store, rs)
    assert w.decisions(report) == {
        f"convention:{recon.FALLBACK_SOURCE_ID}": "excl",
        "gross_is_list:enterprise:2026-09": "true",
        "plan_fit:enterprise:2026-09": "unknown",
    }


def test_report_sources_latest_fetch_wins() -> None:
    lines, _ = _file("excl", ["2026-09-03"])
    old = w.coverage("s_old", "2026-09-03", lines, fetched_ms=1)
    new = w.coverage("s_new", "2026-09-03", lines, fetched_ms=2)
    tie = w.coverage("s_tie", "2026-09-03", lines, fetched_ms=2)
    assert recon.report_sources([new, old]) == {"2026-09-03": "s_new"}
    assert recon.report_sources([old, tie, new]) == {"2026-09-03": "s_tie"}
    other = w.coverage("s_x", "2026-09-04", lines)
    foreign = type(other)(**{**{f: getattr(other, f) for f in other.__slots__},
                             "dims": (("channel", "anthropic_api"), ("source", "s_y"))})
    assert recon.report_sources([foreign, "not an aggregate"]) == {}  # type: ignore[list-item]


def test_k_dated_boundaries_read_from_facts() -> None:
    bounds = recon.k_dated_boundaries()
    assert {"2026-08-03", "2026-08-20", "2026-08-21"} <= bounds["gpt-5.6-sol"]
    assert "2026-09-04" not in bounds["gpt-5.6-sol"]         # D-dated
    assert "claude-opus-5-5" not in bounds                   # C-dated


@pytest.mark.parametrize("bad", [{"tolerance_pct": "x"}, {"tolerance_pct": "-1"},
                                 {"unexplained_pct": 0.5}, {"closed_only": "yes"},
                                 {"today": "2026-13-01"}, {"today": "20261001"},
                                 {"tolerance_pct": True}, {"tolerance_pct": "NaN"}])
def test_bad_arguments_raise_usage_error(bad: dict) -> None:
    store, rs = w.world()
    with pytest.raises(UsageError):
        w.reconcile(store, rs, **bad)


def test_bad_windows_raise_usage_error() -> None:
    store, rs = w.world()
    with pytest.raises(UsageError):
        recon.reconcile_copilot(store, [rs], w.PRICER, since_ms=10, until_ms=5, today="2026-10-01")
    with pytest.raises(UsageError):
        recon.reconcile_copilot(store, [rs], w.PRICER, since_ms=-1, until_ms=5, today="2026-10-01")
    with pytest.raises(UsageError):
        recon.reconcile_copilot(store, [rs], w.PRICER, since_ms=0, until_ms=5, today="2026-10-01",
                                rounding_remainders={"github-ai-usage": "1"})  # type: ignore[dict-item]
