"""SPEC §2.4 / §8.9: no ``float(`` call and no float literal in the money modules (AST scan).

Paths owned by packages that have not landed yet are skipped; once they exist they are scanned.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

#: SPEC §2.4: modules that must contain no ``float(`` call and no float literal.
MONEY_PATHS = (
    "tokenbill/core/money.py",
    "tokenbill/core/labels.py",
    "tokenbill/rates",
    "tokenbill/recon",
    "tokenbill/store",
    "tokenbill/adapters/cloud_billing.py",
    "tokenbill/verify/receipts.py",
    "tokenbill/outputs/result_json.py",
    "tokenbill/outputs/focus.py",
)


def float_uses(source: str) -> list[int]:
    """Line numbers of ``float(...)`` calls and float literals in *source*."""
    lines = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "float"
        ):
            lines.append(node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, float):
            lines.append(node.lineno)
    return sorted(lines)


def _money_files() -> list[Path]:
    files: list[Path] = []
    for rel in MONEY_PATHS:
        path = REPO / rel
        if path.is_dir():
            files.extend(sorted(path.rglob("*.py")))
        elif path.exists():  # paths that do not exist yet (other packages) are skipped
            files.append(path)
    return files


def test_float_detector_works() -> None:
    assert float_uses("x = 1.5\ny = float('2')\nz = 3\n") == [1, 2]
    assert float_uses("from decimal import Decimal\nx = Decimal('1.5')\n") == []
    assert float_uses("x = 1e3\n") == [1]


def test_no_float_in_money_modules() -> None:
    files = _money_files()
    assert any(f.name == "money.py" for f in files)
    offenders = {str(f.relative_to(REPO)): float_uses(f.read_text(encoding="utf-8")) for f in files}
    assert {k: v for k, v in offenders.items() if v} == {}
