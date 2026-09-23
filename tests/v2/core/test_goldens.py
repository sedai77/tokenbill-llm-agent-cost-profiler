"""SPEC §16.2: the v0.1.2 goldens exist, are non-empty and match their manifest.

The byte-for-byte comparison against the current CLI is owned by CLI-LEDGER; this test only guards
the captured artifacts and the capture script's normalization contract.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

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
