"""Code-level hygiene of the PLAN modules: no floats in money code, stdlib-only imports, no
network modules, docstrings on the public API, and the content canary absent from plans built
from lanes that carry it in every free-text attribution field."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from tokenbill.core.builders import CANARY, assert_no_canary
from tokenbill.core.records import to_json
from tokenbill.core.shards import plan_shards
from tokenbill.plan import action_plan, effectiveness, litellm, policy_pack, realization
from tokenbill.plan.policy_pack import build_policy_packs, render_pack

from .helpers import Loader, a7_findings, a7_replayer, ctx, index_of, lane

ROOT = Path(__file__).resolve().parents[3] / "tokenbill" / "plan"
MODULES = sorted(ROOT.glob("*.py")) + sorted((ROOT / "templates").glob("*.py"))
OWNED = [p for p in MODULES if p.name != "__init__.py"]
FORBIDDEN_IMPORTS = {"socket", "urllib", "http", "requests", "ssl", "ftplib", "smtplib"}


@pytest.mark.parametrize("path", OWNED, ids=lambda p: p.name)
def test_no_float_calls_or_literals(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "float", f"{path.name}:{node.lineno} calls float()"
        if isinstance(node, ast.Constant):
            assert not isinstance(node.value, float), f"{path.name}:{node.lineno} float literal"


@pytest.mark.parametrize("path", OWNED, ids=lambda p: p.name)
def test_stdlib_only_and_no_network(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    stdlib = set(sys.stdlib_module_names)
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names = [node.module.split(".")[0]]
        for name in names:
            assert name not in FORBIDDEN_IMPORTS, f"{path.name} imports {name}"
            assert name in stdlib or name == "tokenbill", f"{path.name} imports {name}"
            if path.parent.name == "templates":
                assert name in stdlib, "the hook template is standard library only"


def test_the_hook_template_has_no_tokenbill_imports() -> None:
    text = (ROOT / "templates" / "tokenbill_session_start.py").read_text(encoding="utf-8")
    assert "import tokenbill" not in text and "from tokenbill" not in text


@pytest.mark.parametrize("module", [action_plan, realization, policy_pack, litellm,
                                    effectiveness], ids=lambda m: m.__name__)
def test_public_functions_have_docstrings(module) -> None:
    assert module.__doc__
    for name in module.__all__:
        obj = getattr(module, name)
        if callable(obj) and not isinstance(obj, type(ast)):
            assert obj.__doc__, f"{module.__name__}.{name} has no docstring"


def test_the_canary_never_reaches_a_plan_or_its_packs(tmp_path) -> None:
    tainted = dict(project=f"proj {CANARY}", agent_type=f"agent {CANARY}",
                   entrypoint=f"cli {CANARY}", cost_center=f"cc {CANARY}",
                   extra=(("task_id", CANARY), ("mdm_group", f"g {CANARY}")))
    lanes = [lane(f"l{i}", **tainted) for i in range(3)]
    index = index_of(lanes)
    plan = action_plan.build_action_plan(a7_findings(), index, Loader(lanes),
                                         plan_shards(index), ctx(a7_replayer()),
                                         window_days=30)
    assert plan.levers
    assert_no_canary(repr(plan), str(to_json(plan)))
    for pack in build_policy_packs(plan, a7_findings(), target="claude-code", current=None,
                                   cohort_by="mdm-group", include_tradeoffs=True,
                                   contract=None):
        assert_no_canary(repr(pack))
        for path in render_pack(pack, tmp_path):
            assert_no_canary(path.read_bytes())
