"""Determinism (brief acceptance): the same seed gives byte-identical records (``to_json``) in two
processes; a different seed gives different records."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

from tokenbill.synth.copilot_world import generate

from .worlds import world

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = (
    "import hashlib, sys\n"
    "from tokenbill.synth.copilot_world import generate\n"
    "w = generate(seed=int(sys.argv[1]), conventions=('excl', 'incl'))\n"
    "h = hashlib.sha256('\\n'.join(w.records.json_lines()).encode()).hexdigest()\n"
    "print(h, repr(w.truth.pool_months[-1].consumed_report_nano))\n"
)


def _digest(seed: int) -> str:
    env = dict(os.environ, PYTHONPATH=str(_REPO), PYTHONHASHSEED=str(seed + 11))
    out = subprocess.run([sys.executable, "-c", _SCRIPT, str(seed)], capture_output=True,
                         text=True, check=True, env=env, cwd=str(_REPO))
    return out.stdout.strip()


def test_same_seed_identical_across_processes() -> None:
    a, b = _digest(7), _digest(7)
    assert a == b
    local = world()
    h = hashlib.sha256("\n".join(local.records.json_lines()).encode()).hexdigest()
    assert a.split()[0] == h


def test_different_seed_differs() -> None:
    a = generate(seed=7, conventions=("excl",))
    b = generate(seed=8, conventions=("excl",))
    assert a.records.json_lines() != b.records.json_lines()
    assert a.truth.pool_months != b.truth.pool_months
    assert [p.regime for p in a.truth.pool_months] == [p.regime for p in b.truth.pool_months]
