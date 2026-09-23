"""SPEC §16.2: the v0.1.2 goldens exist, are non-empty and match their manifest.

The byte-for-byte comparison against the current CLI is owned by CLI-LEDGER; this test only guards
the captured artifacts and the capture script's normalization contract.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GOLDEN = REPO / "tests" / "v2" / "golden"


def _capture_module():  # noqa: ANN202 - returns a module
    spec = importlib.util.spec_from_file_location(
        "capture_goldens", REPO / "scripts" / "capture_goldens.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["capture_goldens"] = module
    spec.loader.exec_module(module)
    return module


def test_goldens_exist_and_match_manifest() -> None:
    manifest = json.loads((GOLDEN / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == "tokenbill/goldens@1"
    assert manifest["captured_from_version"] == "0.1.2"
    cases = {c["id"]: c for c in manifest["cases"]}
    required = {"demo", "demo_o", "demo_seed7", "demo_seed11", "analyze_all", "analyze_all_o"}
    required |= {
        f"demo_scenario_{n}" for n in ("well-behaved", "timestamp", "tool-churn", "no-cache")
    }
    required |= {f"analyze_{n}" for n in ("well-behaved", "timestamp", "tool-churn", "no-cache")}
    assert required <= set(cases)
    for case in manifest["cases"]:
        assert case["exit_code"] == 0 and case["stderr_empty"]
        for key in ("stdout", "html"):
            name = case[key]
            if name is None:
                continue
            data = (GOLDEN / name).read_bytes()
            assert data.strip(), name
            assert hashlib.sha256(data).hexdigest() == manifest["sha256"][name]
    assert len(manifest["inputs"]) == 4
    for info in manifest["inputs"].values():
        assert info["seed"] == 7 and info["writer"] == "tokenbill.trace.write_trace"
        assert len(info["sha256"]) == 64


def test_goldens_carry_placeholders_not_versions_or_dates() -> None:
    placeholders = {"{{TOKENBILL_VERSION}}", "{{REPORT_DATE}}"}
    html = (GOLDEN / "demo_o.report.html").read_text(encoding="utf-8")
    assert all(p in html for p in placeholders)
    for path in GOLDEN.glob("*.txt"):
        text = path.read_text(encoding="utf-8")
        assert "0.1.2" not in text and "1999-12-31" not in text
    for path in GOLDEN.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "tokenbill 0.1.2" not in text and "1999-12-31" not in text
        assert "{{REPORT_DATE}}" in text


def test_normalize_contract() -> None:
    capture = _capture_module()
    out = capture.normalize(
        "tokenbill 0.2.0 on 2026-10-01", version="0.2.0", report_date="2026-10-01"
    )
    assert out == "tokenbill {{TOKENBILL_VERSION}} on {{REPORT_DATE}}"
    ids = [c["id"] for c in capture.cases(["a"])]
    assert ids[:2] == ["demo", "demo_o"] and "analyze_a_o" in ids and ids[-1] == "analyze_all_o"


def test_capture_check_is_deterministic(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    capture = _capture_module()
    assert capture.main(["--check"]) == 0
    out = tmp_path / "golden"
    assert capture.main(["--out", str(out)]) == 0
    assert capture.main(["--check", "--out", str(out)]) == 0
    (out / "demo.stdout.txt").write_text("tampered", encoding="utf-8")
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    manifest["cases"] = manifest["cases"][:1]
    (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    capsys.readouterr()
    assert capture.main(["--check", "--out", str(out)]) == 1
    err = capsys.readouterr().err
    assert "drift: demo.stdout.txt" in err and "drift: manifest.json" in err


def test_pinning_falls_back_when_the_cli_has_no_date(monkeypatch: pytest.MonkeyPatch) -> None:
    import datetime

    import tokenbill.cli as cli

    capture = _capture_module()
    with capture._pinned_report_date() as pinned:
        assert pinned == "1999-12-31" and cli.date.today().isoformat() == "1999-12-31"
    assert cli.date is datetime.date
    monkeypatch.delattr(cli, "date")
    with capture._pinned_report_date() as today:
        assert today == datetime.date.today().isoformat()


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_goldens_survive_an_autocrlf_checkout(tmp_path: Path) -> None:
    """Windows runners check out with core.autocrlf=true; the goldens' .gitattributes keeps them
    byte-identical (their sha256 is pinned in manifest.json and CLI-LEDGER compares bytes)."""
    golden = (GOLDEN / "demo.stdout.txt").read_bytes()
    assert b"\r" not in golden
    repo = tmp_path / "repo"
    (repo / "g").mkdir(parents=True)
    shutil.copy(GOLDEN / ".gitattributes", repo / "g" / ".gitattributes")
    (repo / "g" / "demo.stdout.txt").write_bytes(golden)

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "core.autocrlf=true", "-c", "user.email=t@example.com",
             "-c", "user.name=t", *args],
            cwd=repo, check=True, capture_output=True,
        )

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "goldens")
    (repo / "g" / "demo.stdout.txt").unlink()
    git("checkout", "--", "g/demo.stdout.txt")
    assert (repo / "g" / "demo.stdout.txt").read_bytes() == golden
