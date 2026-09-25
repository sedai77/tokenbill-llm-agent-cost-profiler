"""WIRING's Definition-of-done guards (SPEC §21 #5, §2.4, §8.8): no float in the owned modules,
the content canary absent from every output, sibling packages imported lazily only, and runs
byte-identical across processes."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from tokenbill.core.builders import CANARY, CANARY_EMAIL
from tokenbill.core.records import to_json
from tokenbill.core.testing import MemoryStore
from tokenbill.core.types import IngestOptions
from tokenbill.pipeline.common import bill_summary, ingest_paths

from .support import ORG_KEY, T0, make_env, req, write_fake

ROOT = Path(__file__).resolve().parents[3]
OWNED = [ROOT / "tokenbill" / "config.py", ROOT / "tokenbill" / "pipeline" / "common.py"]


@pytest.mark.parametrize("path", OWNED, ids=lambda p: p.name)
def test_no_float_in_owned_modules(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        assert not (isinstance(node, ast.Constant) and isinstance(node.value, float)), node.lineno
        assert not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "float"), node.lineno


@pytest.mark.usefixtures("fake_adapters")
def test_canary_absent_from_every_output(tmp_path: Path) -> None:
    records = [req("L1", i, principal="r_dev1", prompt=CANARY, email=CANARY_EMAIL)
               for i in range(3)]
    source = write_fake(tmp_path / "canary.jsonl", records, extra_text=CANARY)
    store = MemoryStore(org_key=ORG_KEY)
    sources, notes = ingest_paths(store, [source], make_env(), IngestOptions())
    summary = bill_summary(store, make_env(), since_ms=T0, until_ms=T0 + 86_400_000,
                           group_by=["team", "model"])
    blob = repr((sources, notes, summary)) + str(to_json(summary.total))
    assert CANARY not in blob and CANARY_EMAIL not in blob


def test_sibling_packages_are_imported_lazily() -> None:
    code = ("import sys, tokenbill.pipeline.common, tokenbill.config; "
            "print(sorted(m for m in sys.modules if m.startswith(('tokenbill.rates', "
            "'tokenbill.store', 'tokenbill.adapters', 'tokenbill.copilot', 'tokenbill.detect'))))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         cwd=ROOT)
    assert out.stdout.strip() == "[]"


def test_bill_summary_is_deterministic_across_processes(tmp_path: Path) -> None:
    script = tmp_path / "run.py"
    script.write_text(f"""
import sys
sys.path[:0] = [{str(ROOT)!r}, {str(ROOT / "tests")!r}]
from pathlib import Path
from tokenbill.core import registry
from tokenbill.core.records import to_json
from tokenbill.core.testing import MemoryStore
from tokenbill.core.types import IngestOptions
from tokenbill.pipeline.common import bill_summary, ingest_paths
from v2.wiring.support import FakeUsageAdapter, ORG_KEY, T0, make_env, req, write_fake
registry.BUILTIN_ADAPTERS = {{"wiring-fake": FakeUsageAdapter, **registry.BUILTIN_ADAPTERS}}
records = [req("L%d" % i, s, team=t, principal="r_u%d" % i) for i, t in
           enumerate(["a", "a", "b", "c", "c", "c"]) for s in range(3)]
f = write_fake(Path(sys.argv[1]) / "f.jsonl", records)
store = MemoryStore(org_key=ORG_KEY)
ingest_paths(store, [f], make_env(), IngestOptions())
s = bill_summary(store, make_env(k=2), since_ms=T0, until_ms=T0 + 86_400_000,
                 group_by=["team", "team,model"])
print(repr(to_json(s.total)), [(k, [(r.dims, r.n_users, r.priced.exact.nano) for r in p.rows])
                               for k, p in s.breakdowns], s.footnotes)
""")
    runs = [subprocess.run([sys.executable, str(script), str(tmp_path / f"d{i}")],
                           capture_output=True, text=True, check=True).stdout for i in range(2)]
    assert runs[0] == runs[1] and runs[0]
