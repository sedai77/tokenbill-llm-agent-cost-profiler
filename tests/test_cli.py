"""Tests for tokenbill.cli: exit codes, --version, demo/analyze behavior.

Argument handling and error paths are tested standalone; the demo/analyze
integration tests exercise the full pipeline and skip cleanly until
Modules A-C are merged.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tokenbill import __version__
from tokenbill.cli import main

REPO_ROOT = Path(__file__).resolve().parent.parent


def _need_pipeline() -> None:
    for module in (
        "tokenbill.trace",
        "tokenbill.demo_traces",
        "tokenbill.pricing",
        "tokenbill.analyzer",
        "tokenbill.simulator",
        "tokenbill.breakers",
    ):
        pytest.importorskip(module, reason=f"{module} not merged yet")


# --- version and usage errors --------------------------------------------------


def test_version_via_python_dash_m() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "tokenbill", "--version"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"tokenbill {__version__}"


def test_version_in_process(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_is_a_usage_error() -> None:
    assert main([]) == 2


def test_unknown_command_is_a_usage_error() -> None:
    assert main(["frobnicate"]) == 2


@pytest.mark.parametrize(
    "bad",
    [
        "bogus",
        "model=1",
        "model=1,2,3",
        "=1,2",
        "model=a,b",
        # Regression: nan/inf parse as floats and pass the `< 0` check, then
        # every figure in the report renders as "$nan" — reject them upfront.
        "model=nan,5",
        "model=3,inf",
        "model=-inf,5",
    ],
)
def test_malformed_model_price_is_a_usage_error(bad: str) -> None:
    assert main(["analyze", "trace.jsonl", "--model-price", bad]) == 2


def test_missing_trace_file_exits_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("tokenbill.trace", reason="tokenbill.trace not merged yet")
    missing = tmp_path / "nope.jsonl"
    assert main(["analyze", str(missing)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("tokenbill: error:")
    assert len(err.strip().splitlines()) == 1  # one tidy line, no traceback


# --- demo ----------------------------------------------------------------------


def test_demo_unknown_scenario_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    _need_pipeline()
    assert main(["demo", "--scenario", "nope"]) == 2
    assert "unknown scenario" in capsys.readouterr().err


def test_demo_writes_report_and_prints_headline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _need_pipeline()
    report = tmp_path / "report.html"
    assert main(["demo", "-o", str(report)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("~")
    assert "of billed input tokens" in out
    assert f"Report written to {report}" in out
    html_doc = report.read_text(encoding="utf-8")
    assert "<svg" in html_doc
    assert "Synthetic demo data." in html_doc


def test_demo_single_scenario(capsys: pytest.CaptureFixture[str]) -> None:
    _need_pipeline()
    assert main(["demo", "--scenario", "well-behaved"]) == 0
    out = capsys.readouterr().out
    assert "well-behaved" in out


def test_demo_is_deterministic_per_seed(capsys: pytest.CaptureFixture[str]) -> None:
    _need_pipeline()
    assert main(["demo", "--seed", "3"]) == 0
    first = capsys.readouterr().out
    assert main(["demo", "--seed", "3"]) == 0
    assert capsys.readouterr().out == first


# --- analyze -------------------------------------------------------------------


def _write_demo_trace(tmp_path: Path, model: str | None = None) -> Path:
    from dataclasses import replace

    from tokenbill.demo_traces import scenario
    from tokenbill.trace import write_trace

    calls = scenario("well-behaved", 7)
    if model is not None:
        calls = [replace(call, model=model) for call in calls]
    path = tmp_path / "trace.jsonl"
    write_trace(path, calls)
    return path


def test_analyze_roundtrip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _need_pipeline()
    path = _write_demo_trace(tmp_path)
    report = tmp_path / "out.html"
    assert main(["analyze", str(path), "-o", str(report)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("~")
    html_doc = report.read_text(encoding="utf-8")
    assert "<svg" in html_doc
    assert "Synthetic demo data." not in html_doc  # analyze is not synthetic


def test_model_price_override_prices_unknown_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _need_pipeline()
    from tokenbill import pricing

    path = _write_demo_trace(tmp_path, model="acme-llm-1")
    try:
        assert main(["analyze", str(path)]) == 0
        assert "unavailable" in capsys.readouterr().out  # unknown model: no dollars

        assert main(["analyze", str(path), "--model-price", "acme-llm-1=3,15"]) == 0
        out = capsys.readouterr().out
        assert "unavailable" not in out
        assert "$" in out
    finally:
        pricing.PRICING.pop("acme-llm-1", None)
