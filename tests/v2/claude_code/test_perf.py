"""Import performance (SPEC §17: ≥ 25,000 assistant lines/s; 200,000 synthetic lines ≤ 30 s;
peak RSS ≤ 150 MB, streaming per file).

The measurement runs in a fresh interpreter so the peak RSS is the importer's, not pytest's. The
full-size budget is marker ``perf`` (nightly); the PR variant reads 20,000 lines against the
budgets scaled by 1/10 (≤ 3 s) and the same RSS bound.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from .helpers import bf

_MEASURE = r"""
import json, sys, time
from pathlib import Path
from tokenbill.adapters.claude_code import ClaudeCodeAdapter
from tokenbill.core.ids import key_id
from tokenbill.core.types import IngestOptions
key = bytes(range(32))
opts = IngestOptions(name_key=key, name_key_id=key_id(key), now_ms=1_790_121_600_000)
t = time.perf_counter()
r = ClaudeCodeAdapter().read(Path(sys.argv[1]), opts)
dt = time.perf_counter() - t
rss = None
try:
    import resource
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss = rss if sys.platform == "darwin" else rss * 1024
except ImportError:
    pass
print(json.dumps({"seconds": dt, "assistant_lines": r.stats["assistant_lines"],
                  "requests": len(r.requests), "rss": rss}))
"""


def _measure(tmp: Path, n_lines: int) -> dict:
    path = tmp / "perf.jsonl"
    counts = bf.synthetic_transcript(path, n_lines)
    out = subprocess.run([sys.executable, "-c", _MEASURE, str(path)], capture_output=True,
                         text=True, timeout=600, check=True)
    result = json.loads(out.stdout)
    assert result["assistant_lines"] == counts["assistant_lines"]
    assert result["requests"] == counts["messages"]
    return result


def test_pr_variant_20k_lines(tmp_path: Path) -> None:
    r = _measure(tmp_path, 20_000)
    assert r["seconds"] <= 3.0, r                 # 30 s / 10
    if r["rss"] is not None:
        assert r["rss"] <= 150 * 10**6, r


@pytest.mark.perf
def test_full_size_200k_lines(tmp_path: Path) -> None:
    r = _measure(tmp_path, 200_000)
    assert r["seconds"] <= 30.0, r
    assert r["assistant_lines"] / r["seconds"] >= 25_000, r
    if r["rss"] is not None:
        assert r["rss"] <= 150 * 10**6, r
