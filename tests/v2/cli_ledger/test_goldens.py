"""SPEC §16.2 golden byte-identity (owned by CLI-LEDGER): every ``demo`` / ``analyze`` case of
``tests/v2/golden/manifest.json`` — stdout and the HTML report — equals the v0.1.2 capture after
``capture_goldens.normalize`` (version string and report date), with exit code 0 and an empty
stderr (no routing note: the four demo traces are v1-safe)."""

from __future__ import annotations

import datetime as _dt
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from tokenbill import __version__

from .helpers import REPO, chdir, run_main

GOLDEN = REPO / "tests" / "v2" / "golden"
MANIFEST = json.loads((GOLDEN / "manifest.json").read_text(encoding="utf-8"))


def _capture() -> ModuleType:
    name = "capture_goldens_cli_ledger"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / "capture_goldens.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    work = tmp_path_factory.mktemp("goldens")
    inputs = _capture().write_inputs(work)
    for name, info in MANIFEST["inputs"].items():  # the regenerated inputs are the captured ones
        assert inputs[name]["sha256"] == info["sha256"], name
    return work


def _normalize(text: str) -> str:
    return _capture().normalize(text, version=__version__,
                                report_date=_dt.date.today().isoformat())


@pytest.mark.parametrize("case", MANIFEST["cases"], ids=[c["id"] for c in MANIFEST["cases"]])
def test_output_is_byte_identical_to_v012(case: dict, workdir: Path) -> None:
    report = workdir / "report.html"
    if report.exists():
        report.unlink()
    with chdir(workdir):
        code, out, err = run_main(case["argv"])
    assert code == case["exit_code"] == 0
    assert err == ""
    expected = (GOLDEN / case["stdout"]).read_bytes()
    assert hashlib.sha256(expected).hexdigest() == MANIFEST["sha256"][case["stdout"]]
    assert _normalize(out).encode("utf-8") == expected
    if case["html"] is not None:
        html = report.read_bytes().decode("utf-8")
        assert _normalize(html).encode("utf-8") == (GOLDEN / case["html"]).read_bytes()
    else:
        assert not report.exists()


def test_engine_v1_is_identical_to_auto_on_the_demo_traces(workdir: Path) -> None:
    with chdir(workdir):
        auto = run_main(["analyze", "timestamp.jsonl", "tool-churn.jsonl"])
        legacy = run_main(["analyze", "--engine", "v1", "timestamp.jsonl", "tool-churn.jsonl"])
    assert auto == legacy
    assert auto[0] == 0 and auto[2] == ""


def test_capture_script_check_passes_on_this_tree() -> None:
    """``capture_goldens.py --check`` (re-capture with the pinned report date) is clean."""
    manifest, files = _capture().capture()
    for name, text in files.items():
        assert text.encode("utf-8") == (GOLDEN / name).read_bytes(), name
    assert [c["id"] for c in manifest["cases"]] == [c["id"] for c in MANIFEST["cases"]]
