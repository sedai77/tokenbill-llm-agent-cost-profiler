"""Tests for tokenbill.report: HTML self-containment, honesty labels, purity.

The unit tests run against small duck-typed fakes (report.py touches only the
attribute names pinned in docs/SPEC.md, so the fakes double as a contract
check). The integration tests at the bottom exercise the real demo pipeline
and skip cleanly until Modules A-C are merged.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pytest

from tokenbill.report import render_report, render_text_summary

# --- duck-typed fakes matching the SPEC interfaces -----------------------------


@dataclass(frozen=True)
class FakeUsage:
    input_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    output_tokens: int

    @property
    def total_input(self) -> int:
        return (
            self.input_tokens
            + self.cache_read_input_tokens
            + self.cache_creation_input_tokens
        )


@dataclass(frozen=True)
class FakeCall:
    index: int
    model: str
    usage: FakeUsage


@dataclass(frozen=True)
class FakeCallProfile:
    call: FakeCall
    dollars: dict[str, float] | None


@dataclass(frozen=True)
class FakeTotals:
    redundancy_fraction: float


@dataclass(frozen=True)
class FakeRun:
    run_id: str


@dataclass(frozen=True)
class FakeRunProfile:
    run: FakeRun
    calls: list[FakeCallProfile]
    totals: FakeTotals


@dataclass(frozen=True)
class FakeScenarioResult:
    name: str
    dollars: float | None
    note: str = ""
    tokens: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class FakeBreaker:
    kind: str
    first_call_index: int
    evidence: str
    fix: str
    est_recovered_usd: float | None


def _profile(run_id: str = "run-1", *, priced: bool = True) -> FakeRunProfile:
    calls = []
    for index in range(3):
        usage = FakeUsage(
            input_tokens=4000 + 900 * index,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            output_tokens=350,
        )
        dollars = (
            {"uncached": 0.012 + 0.003 * index, "write": 0.0, "read": 0.0, "output": 0.005}
            if priced
            else None
        )
        call = FakeCall(index, "claude-sonnet-5", usage)
        calls.append(FakeCallProfile(call=call, dollars=dollars))
    return FakeRunProfile(run=FakeRun(run_id), calls=calls, totals=FakeTotals(0.62))


def _scenarios(run_id: str = "run-1", *, priced: bool = True) -> dict:
    def usd(value: float) -> float | None:
        return value if priced else None

    return {
        run_id: [
            FakeScenarioResult("as-billed", usd(5.12), note="ground truth from billed usage"),
            FakeScenarioResult("no-cache", usd(5.12)),
            FakeScenarioResult("optimal-cache", usd(0.81)),
            FakeScenarioResult("fixed-cache", usd(0.81)),
        ]
    }


def _breakers(run_id: str = "run-1", count: int = 1) -> dict:
    found = [
        FakeBreaker(
            kind="volatile-system",
            first_call_index=1,
            evidence="[session 2026-07-26 14:03:01]\nsecond line <script>alert(1)</script>",
            fix="move the timestamp out of the system prompt",
            est_recovered_usd=4.31,
        ),
        FakeBreaker(
            kind="tool-churn",
            first_call_index=4,
            evidence="tools order changed: [a, b] -> [b, a]",
            fix="keep tool definitions in a stable order",
            est_recovered_usd=0.40,
        ),
    ]
    return {run_id: found[:count]}


META = {"trace": "unit fixture", "date": "2026-07-26", "synthetic": True}


# --- render_report -------------------------------------------------------------


def test_report_headline_and_dollars() -> None:
    html_doc = render_report([_profile()], _scenarios(), _breakers(), META)
    assert "~62%" in html_doc
    # Token wording, deliberately: redundancy is a share of billed input
    # TOKENS, and a spend claim would be wrong whenever rate classes mix.
    assert "of billed input tokens went to re-sending bytes" in html_doc
    assert "of input spend" not in html_doc
    assert "the fix below recovers an estimated $4.31 of $5.12." in html_doc


def test_report_headline_pluralizes_fixes() -> None:
    html_doc = render_report([_profile()], _scenarios(), _breakers(count=2), META)
    assert "the two fixes below recover an estimated" in html_doc


def test_report_headline_without_breakers() -> None:
    html_doc = render_report([_profile()], _scenarios(), {}, META)
    assert "no cache breakers detected." in html_doc
    assert "None detected." in html_doc


def test_report_is_self_contained() -> None:
    html_doc = render_report([_profile()], _scenarios(), _breakers(), META)
    for banned in ("<script", "<link", "<img", "<iframe", "src=", "url(", "@import", "fetch("):
        assert banned not in html_doc, f"external-resource marker {banned!r} found"


def test_report_supports_dark_and_light() -> None:
    html_doc = render_report([_profile()], _scenarios(), _breakers(), META)
    assert "prefers-color-scheme: dark" in html_doc
    assert "color-scheme: light dark" in html_doc


def test_report_has_waterfall_and_scenario_svgs() -> None:
    html_doc = render_report([_profile()], _scenarios(), _breakers(), META)
    assert html_doc.count("<svg") == 2
    for css_class in ("wf-read", "wf-write", "wf-uncached", "wf-output", "sc-bar", "sc-fixed"):
        assert css_class in html_doc
    assert "$5.12" in html_doc and "$0.81" in html_doc


def test_report_escapes_evidence() -> None:
    html_doc = render_report([_profile()], _scenarios(), _breakers(), META)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_doc
    assert "<script>" not in html_doc


def test_report_truncates_long_evidence() -> None:
    breakers = {
        "run-1": [
            FakeBreaker(
                kind="history-rewrite",
                first_call_index=2,
                evidence="x" * 5000,
                fix="append to history instead of rewriting it",
                est_recovered_usd=None,
            )
        ]
    }
    html_doc = render_report([_profile()], _scenarios(), breakers, META)
    assert "x" * 5000 not in html_doc
    assert "truncated, 5000 chars total" in html_doc
    assert "recovery estimate unavailable" in html_doc


def test_report_marks_approx_numbers() -> None:
    html_doc = render_report([_profile()], _scenarios(), _breakers(), META)
    assert 'id="fn-approx"' in html_doc
    assert html_doc.count('href="#fn-approx"') >= 2  # headline + per-run redundancy


def test_report_synthetic_banner_toggles_via_meta() -> None:
    with_banner = render_report([_profile()], _scenarios(), _breakers(), META)
    without = render_report([_profile()], _scenarios(), _breakers(), dict(META, synthetic=False))
    assert "Synthetic demo data." in with_banner
    assert "Synthetic demo data." not in without


def test_report_is_pure_and_uses_meta_date() -> None:
    args = ([_profile()], _scenarios(), _breakers(), META)
    first, second = render_report(*args), render_report(*args)
    assert first == second
    assert "2026-07-26" in first


def test_report_handles_unknown_pricing() -> None:
    html_doc = render_report(
        [_profile(priced=False)],
        _scenarios(priced=False),
        _breakers(),
        META,
    )
    assert "billed dollars unavailable" in html_doc
    assert "n/a" in html_doc
    assert "dollar impact unknown" in html_doc


def test_report_handles_empty_trace() -> None:
    html_doc = render_report([], {}, {}, META)
    assert "No runs in this trace." in html_doc


def test_waterfall_legend_fits_single_call_viewbox() -> None:
    # Regression: the waterfall's viewBox width was 64 + n*slot + 14, but the
    # legend row extends to ~453px regardless of call count — on short runs
    # SVG clipped the legend labels entirely.
    profile = FakeRunProfile(
        run=FakeRun("run-1"),
        calls=[
            FakeCallProfile(
                call=FakeCall(0, "claude-sonnet-5", FakeUsage(4000, 0, 0, 350)),
                dollars={"uncached": 0.012, "write": 0.0, "read": 0.0, "output": 0.005},
            )
        ],
        totals=FakeTotals(0.1),
    )
    html_doc = render_report([profile], _scenarios(), {}, META)
    match = re.search(
        r'<svg viewBox="0 0 (\d+) \d+"[^>]*Per-call billed token waterfall', html_doc
    )
    assert match is not None
    # Required legend width, from the same advance arithmetic the SVG uses.
    labels = ("cache read", "cache write", "uncached input", "output")
    legend_w = 64 + sum(14 + int(len(label) * 6.4) + 18 for label in labels)
    assert int(match.group(1)) >= legend_w


# --- render_text_summary -------------------------------------------------------


def test_text_summary_leads_with_headline() -> None:
    text = render_text_summary([_profile()], _scenarios(), _breakers(), meta=META)
    first_line = text.splitlines()[0]
    assert first_line.startswith("~62%")
    assert "recovers an estimated $4.31 of $5.12." in first_line


def test_text_summary_alignment_and_bars() -> None:
    text = render_text_summary([_profile()], _scenarios(), _breakers(), meta=META)
    scenario_lines = [
        line
        for line in text.splitlines()
        if line.lstrip().startswith(("as-billed", "no-cache", "optimal-cache", "fixed-cache"))
        and "note" not in line
    ]
    assert len(scenario_lines) == 4
    dollar_columns = {line.index("$") for line in scenario_lines}
    assert len(dollar_columns) == 1, f"misaligned dollars: {scenario_lines}"
    assert any("#" in line for line in scenario_lines)


def test_text_summary_flattens_evidence_and_shows_fix() -> None:
    text = render_text_summary([_profile()], _scenarios(), _breakers(), meta=META)
    assert "fix: move the timestamp out of the system prompt" in text
    evidence_lines = [line for line in text.splitlines() if "evidence:" in line]
    assert len(evidence_lines) == 1  # newlines flattened into one line
    assert "\\n" in evidence_lines[0]


def test_text_summary_has_no_trailing_whitespace() -> None:
    # Header rows ("scenarios", "breakers") used to carry label padding; the
    # README quotes real output byte-for-byte, so lines must diff cleanly.
    text = render_text_summary([_profile()], _scenarios(), _breakers(), meta=META)
    assert all(line == line.rstrip() for line in text.splitlines())


def test_text_summary_labels_approx_and_synthetic() -> None:
    text = render_text_summary([_profile()], _scenarios(), _breakers(), meta=META)
    assert "(approx)" in text
    assert "[synthetic demo data" in text
    assert "come from real billed usage" in text


# --- integration with the real demo pipeline (skips until Modules A-C land) ----


def _demo_pipeline() -> tuple[list, dict, dict]:
    demo_traces = pytest.importorskip("tokenbill.demo_traces", reason="Module A not merged yet")
    trace = pytest.importorskip("tokenbill.trace", reason="Module A not merged yet")
    analyzer = pytest.importorskip("tokenbill.analyzer", reason="Module B not merged yet")
    simulator = pytest.importorskip("tokenbill.simulator", reason="Module C not merged yet")
    breakers_mod = pytest.importorskip("tokenbill.breakers", reason="Module C not merged yet")

    profiles, scenarios, breakers = [], {}, {}
    for calls in demo_traces.all_scenarios(7).values():
        run = trace.Run(run_id=calls[0].run_id, calls=tuple(calls))
        profiles.append(analyzer.profile_run(run))
        found = list(breakers_mod.detect(run))
        breakers[run.run_id] = found
        fixed = list(breakers_mod.repaired_calls(run, found)) if found else None
        scenarios[run.run_id] = list(simulator.simulate(run, fixed_calls=fixed))
    return profiles, scenarios, breakers


def test_report_renders_all_demo_scenarios() -> None:
    profiles, scenarios, breakers = _demo_pipeline()
    meta = {"trace": "demo", "date": "2026-07-26", "synthetic": True}
    html_doc = render_report(profiles, scenarios, breakers, meta)
    assert len(html_doc) > 5000
    for profile in profiles:
        assert profile.run.run_id in html_doc
    for banned in ("<script", "<link", "<img", "<iframe", "src=", "url(", "@import"):
        assert banned not in html_doc
    text = render_text_summary(profiles, scenarios, breakers, meta=meta)
    assert text.splitlines()[0].startswith("~")
