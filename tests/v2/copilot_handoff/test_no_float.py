"""SPEC §2.4 / C-30: the owned modules are money modules — no ``float(`` call, no float literal."""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
OWN_MODULES = (
    "tokenbill/copilot/handoff.py",
    "tokenbill/copilot/admin_answers.py",
    "tokenbill/adapters/copilot_export.py",
    "tokenbill/adapters/github_activity_report.py",
)


def test_no_float_in_owned_modules() -> None:
    for rel in OWN_MODULES:
        tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            assert not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "float"), rel
            assert not (isinstance(node, ast.Constant) and isinstance(node.value, float)), rel
