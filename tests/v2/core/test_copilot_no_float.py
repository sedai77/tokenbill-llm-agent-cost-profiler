"""SPEC §2.4 / §8.9 as amended by CORE-AMENDMENTS C-30: no ``float(`` call and no float literal in
the Copilot money modules (AST scan; modules of packages not merged yet are skipped)."""

from __future__ import annotations

from pathlib import Path

from .test_no_float_money import REPO, float_uses

#: C-30: the §2.4 no-float list gains these modules.
COPILOT_MONEY_PATHS = (
    "tokenbill/core/pool.py",
    "tokenbill/core/extensions.py",
    *(f"tokenbill/adapters/{m}.py" for m in (
        "github_billing", "github_config", "github_metrics", "github_agent_tasks", "copilot_cli",
        "copilot_otel", "copilot_vscode", "gh_aw", "copilot_export", "github_activity_report",
        "copilot_vscode_collect")),
    *(f"tokenbill/copilot/{m}.py" for m in (
        "rates_verify", "recon", "panel", "enrich", "plan", "budgets", "policy", "admin_actions",
        "summary", "render", "showback", "focus", "record_store", "handoff", "admin_answers")),
    *(f"tokenbill/detect/{m}.py" for m in ("copilot_seats", "copilot_org", "copilot_lanes")),
    "tokenbill/synth/copilot_truth.py",
    "tokenbill/synth/copilot_world.py",
)
#: F-CORE-C's own Copilot money code is scanned too (it exists now).
F_CORE_C_PATHS = ("tokenbill/core/money.py", "tokenbill/core/labels.py",
                  "tokenbill/core/builders.py", "tokenbill/core/facts.py",
                  "tokenbill/core/records.py", "tokenbill/core/types.py")


def _existing(paths: tuple[str, ...]) -> list[Path]:
    return [REPO / p for p in paths if (REPO / p).exists()]


def test_c30_module_list() -> None:
    assert len(COPILOT_MONEY_PATHS) == 2 + 11 + 15 + 3 + 2
    assert len(set(COPILOT_MONEY_PATHS)) == len(COPILOT_MONEY_PATHS)
    assert "tokenbill/core/spans.py" not in COPILOT_MONEY_PATHS  # withdrawn (CA-42)


def test_no_float_in_copilot_money_modules() -> None:
    files = _existing(COPILOT_MONEY_PATHS) + _existing(F_CORE_C_PATHS)
    assert any(f.name == "money.py" for f in files)
    offenders = {str(f.relative_to(REPO)): float_uses(f.read_text(encoding="utf-8"))
                 for f in files}
    assert {k: v for k, v in offenders.items() if v} == {}


def test_absent_modules_are_skipped(tmp_path: Path) -> None:
    assert _existing(("tokenbill/copilot/does_not_exist.py",)) == []
    assert float_uses("x = Decimal('0.01')\n") == [] and float_uses("y = 0.01\n") == [1]
