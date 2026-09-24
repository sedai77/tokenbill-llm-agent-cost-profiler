"""SPEC §2.4 / §8.9 and the F-SEM brief: no ``float(`` call and no float literal in the F-SEM core
modules (transition thresholds, Shapley credits, standard errors and shard quotas are all exact
integer / Fraction / Decimal arithmetic). AST scan, same rule as the F-CORE money-module lint."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
F_SEM_MODULES = ("conventions", "cache_rules", "transitions", "shapley", "policy", "shards",
                 "findings")


def float_uses(source: str) -> list[int]:
    """Line numbers of ``float(...)`` calls and float literals in *source*."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and \
                node.func.id == "float":
            lines.append(node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, float):
            lines.append(node.lineno)
    return sorted(lines)


def test_detector_works() -> None:
    assert float_uses("a = 0.5\nb = float('1')\nc = 1\n") == [1, 2]


@pytest.mark.parametrize("module", F_SEM_MODULES)
def test_no_float_in_f_sem_modules(module: str) -> None:
    path = REPO / "tokenbill" / "core" / f"{module}.py"
    assert float_uses(path.read_text(encoding="utf-8")) == []
