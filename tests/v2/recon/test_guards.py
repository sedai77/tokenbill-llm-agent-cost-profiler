"""RECON's code-level guards: no float in ``tokenbill/recon`` (SPEC §2.4, §8.9 AST lint), no network
module outside ``pull``, docstrings on the public API, and the canary never reaches a report."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import json
from pathlib import Path

from tokenbill.core.builders import CANARY, make_cost_line
from tokenbill.core.records import to_json
from tokenbill.recon import costmap, orgscan, pull, reconcile, residuals

from .helpers import PRICER, TODAY, agg, cost_lines, record

RECON = Path(reconcile.__file__).resolve().parent


def test_no_float_in_recon() -> None:
    offenders = {}
    for path in sorted(RECON.glob("*.py")):
        lines = []
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and (
                    node.func.id == "float"):
                lines.append(node.lineno)
            elif isinstance(node, ast.Constant) and isinstance(node.value, float):
                lines.append(node.lineno)
        if lines:
            offenders[path.name] = lines
    assert offenders == {}


def test_only_pull_imports_network_modules() -> None:
    for path in sorted(RECON.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        network = {n for n in names if n.split(".")[0] in ("urllib", "http", "socket", "ssl")}
        assert not network or path.name == "pull.py", path.name


def test_public_api_is_documented() -> None:
    for module in (costmap, orgscan, pull, reconcile, residuals):
        assert module.__doc__
        for name in module.__all__:
            obj = getattr(module, name)
            if inspect.isfunction(obj) or inspect.isclass(obj):
                assert obj.__doc__, f"{module.__name__}.{name}"


def test_canary_never_reaches_a_report() -> None:
    a = agg()
    lines = [dataclasses.replace(c, description=f"{CANARY} {c.description}")
             for c in cost_lines(a)]
    odd = make_cost_line(1_000, model=None, cost_type="fine_tuning", line_id="x",
                         description=CANARY)
    report = reconcile.reconcile([record()], [a], [*lines, odd], PRICER, today=TODAY)
    assert CANARY not in json.dumps(to_json(report))
