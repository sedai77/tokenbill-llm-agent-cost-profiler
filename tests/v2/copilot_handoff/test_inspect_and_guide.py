"""``inspect_bundle`` / ``render_inspect`` (counts only) and the packaged admin guide
(brief Build 9)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tokenbill.copilot.admin_answers import template_text
from tokenbill.copilot.handoff import admin_guide_text, inspect_bundle, render_inspect
from tokenbill.core.builders import CANARY_LOGIN
from tokenbill.core.errors import SourceError, UsageError

from .helpers import export, use_fake_registry, write_world

P_RE = re.compile(r"p_[0-9a-f]{20}|h_[0-9a-f]{20}")


@pytest.fixture
def bundles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    use_fake_registry(monkeypatch)
    world = write_world(tmp_path / "in")
    return {"pseudonymous": export(world, tmp_path / "p.tbx").out_path,
            "aggregate_only": export(world, tmp_path / "a.tbx", aggregate_only=True).out_path}


@pytest.mark.parametrize("mode", ["pseudonymous", "aggregate_only"])
def test_inspect_counts_only(bundles: dict[str, Path], mode: str) -> None:
    info = inspect_bundle(bundles[mode])
    assert info["manifest"]["privacy_mode"] == mode  # type: ignore[index]
    assert info["months"] == ["2026-09"]
    assert info["teams"] == [{"team": "alpha", "seats": 7}, {"team": "beta", "seats": 5}]
    counts = info["counts"]
    assert counts["licenses"] == (30 if mode == "pseudonymous" else 0)  # type: ignore[index]
    for fmt in ("text", "json"):
        out = render_inspect(info, fmt)
        assert not P_RE.search(out)
        assert CANARY_LOGIN not in out and "dev-alpha" not in out
    text = render_inspect(info)
    assert "leak scan: clean" in text and "alpha: 7 seat(s)" in text
    assert json.loads(render_inspect(info, "json"))["counts"] == counts
    assert info["teams_below_k"] == (1 if mode == "pseudonymous" else 0)


def test_render_inspect_errors_and_empty_sections(bundles: dict[str, Path]) -> None:
    with pytest.raises(UsageError):
        render_inspect({}, "yaml")
    text = render_inspect({"manifest": {"window": {}}, "counts": {}})
    assert "none (plan unknown: both scenarios will be shown)" in text
    assert "data quality:\n  none" in text


def test_inspect_runs_the_reader_checks(tmp_path: Path) -> None:
    bad = tmp_path / "bad.tbx"
    bad.write_bytes(b"PK\x03\x04 broken")
    with pytest.raises(SourceError):
        inspect_bundle(bad)


# ---------------------------------------------------------------------------------------------
# admin guide
# ---------------------------------------------------------------------------------------------

GUIDE = admin_guide_text()

CALLS = (
    "POST /enterprises/{e}/settings/billing/reports",
    "GET /enterprises/{e}/settings/billing/reports/{report_id}",
    "`ai_credit`",
    "`detailed`",
    "31 days",
    "GET /enterprises/{e}/settings/billing/usage/summary",
    "GET /enterprises/{e}/copilot/billing/seats",
    "GET /orgs/{org}/copilot/billing/seats",
    "GET /orgs/{org}/copilot/billing",
    "users-1-day",
    "user-teams-1-day",
    "enterprise-1-day",
    "GET /enterprises/{e}/settings/billing/budgets",
    "/user-states",
    "GET /enterprises/{e}/settings/billing/cost-centers",
    "GET /agents/repos/{o}/{r}/tasks",
    "tokenbill copilot pull",
    "--out export.tbx",
)
UI_REPORTS = (
    "AI usage report CSV",
    "Usage → AI\n   usage → Get usage report",
    "expires after 24 hours",
    "Detailed usage report CSV",
    "Copilot activity report CSV",
    "Get activity report",
    "Copilot usage dashboard NDJSON export",
    "rolling 28-day window",
    "excludes Copilot CLI",
    "tokenbill copilot export --in",
    "--answers answers.json",
    "--user-teams",
    "--team-map-csv",
)
SECTIONS = (
    "## 1. What the analyst receives, and what never leaves your machine",
    "## 2. Path A: a read-only token",
    "## 3. The calls path A makes",
    "## 4. Path B: no token",
    "## 5. Team labels",
    "## 6. Check the file before you hand it over",
    "## 7. Keep the export key",
    "## 8. Erasure requests",
    "## 9. Raw files only as a last resort",
    "## 10. Open points to confirm (VERIFY)",
)


def test_guide_lists_every_call_and_ui_report() -> None:
    for needle in (*CALLS, *UI_REPORTS):
        assert needle in GUIDE, needle


def test_guide_sections_in_order_and_size() -> None:
    positions = [GUIDE.index(s) for s in SECTIONS]
    assert positions == sorted(positions)
    assert len(GUIDE.splitlines()) <= 350


def test_guide_token_path_and_scopes() -> None:
    for needle in ("`read:enterprise`", "`read:org`", "revoke it right after the run",
                   "--github-token-file", "chmod 600", '"Enterprise billing: read"',
                   '"Enterprise Copilot metrics: read"', '"GitHub\nCopilot Business: read"',
                   "Token Bill never mints\ntokens", "copilot-export.key",
                   "tokenbill copilot pseudonym --login -", "--exclude-logins FILE",
                   "tokenbill purge --principal", "--rotate-key", "tokenbill copilot inspect",
                   "data protection officer", "works council"):
        assert needle in GUIDE, needle
    risky = [line for line in GUIDE.splitlines()
             if "manage_billing:copilot" in line or re.search(r"\badmin:", line)]
    assert len(risky) == 1 and risky[0].startswith("**Never** grant")


def test_guide_marks_every_verify_item() -> None:
    verify = GUIDE[GUIDE.index("## 10."):]
    for needle in ("POST /enterprises/{e}/settings/billing/reports",
                   "GET /enterprises/{e}/copilot/billing/seats", "billing manager",
                   "click paths", "NDJSON export has the same record shape",
                   "lists every seat holder", "JetBrains"):
        assert needle in verify, needle
    assert verify.count("**VERIFY:**") == 6
    assert GUIDE.count("**VERIFY**") >= 4       # the click paths and the dashboard shape


def test_answers_template_is_packaged() -> None:
    doc = json.loads(template_text())
    assert doc["schema"] == "tokenbill/copilot-admin-answers@1"
