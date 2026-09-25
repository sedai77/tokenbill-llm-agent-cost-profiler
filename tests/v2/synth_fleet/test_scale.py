"""Scale mode streams exactly ``scale_requests`` canonical requests lazily and deterministically
(SPEC §17 perf gates): 10⁶ requests ≤ 60 s with bounded memory (marker ``perf``; the PR-size
variant runs 10⁵)."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tokenbill.core.lanes import group_lanes
from tokenbill.synth.fleet import generate

REPO = Path(__file__).resolve().parents[3]

SCRIPT = textwrap.dedent("""
    import resource, sys, time
    from tokenbill.synth.fleet import generate
    n = int(sys.argv[1])
    t = time.perf_counter()
    world = generate(scale_requests=n)
    count = sum(1 for _ in world.requests)
    elapsed = time.perf_counter() - t
    try:  # Linux: own high-water mark (ru_maxrss of an exec'd child carries the parent's peak)
        with open("/proc/self/status") as f:
            rss_mb = next(int(x.split()[1]) for x in f if x.startswith("VmHWM:")) / 2**10
    except (OSError, StopIteration):
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_mb = rss / 2**20 if sys.platform == "darwin" else rss / 2**10
    print(count, round(elapsed, 3), round(rss_mb, 1))
""")


def _run(n: int) -> tuple[int, float, float]:
    out = subprocess.run([sys.executable, "-c", SCRIPT, str(n)], cwd=REPO, capture_output=True,
                         text=True, timeout=600, check=True).stdout.split()
    return int(out[0]), float(out[1]), float(out[2])


def test_scale_mode_small_world_is_exact_and_deterministic(tmp_path: Path) -> None:
    world = generate(seed=3, scale_requests=30_000, out_dir=tmp_path)   # > one epoch
    assert len(world.requests) == 30_000
    first = [q.request_id for q in world.requests]
    assert len(first) == len(set(first)) == 30_000
    assert [q.request_id for q in world.requests] == first               # re-iterable
    again = generate(seed=3, scale_requests=30_000)
    assert [q.request_id for q in again.requests] == first
    lanes = group_lanes(world.requests, world.events, world.sessions)
    assert sum(len(ln.requests) for ln in lanes) == 30_000
    assert all(ln.kind.value != "unknown" for ln in lanes)
    teams = {ln.team for ln in lanes}
    assert {"platform", "payments", "tiny"} <= teams
    assert world.truth.plants == () and world.source_files == {}
    assert world.aggregates == () and "scale mode" in world.truth.note
    assert len(world.events) >= 1 and len(world.sessions) >= 1
    assert "stream" in repr(world.requests)


@pytest.mark.skipif(sys.platform == "win32", reason="resource module is POSIX-only")
def test_scale_mode_pr_size_budget() -> None:
    count, elapsed, rss_mb = _run(100_000)
    assert count == 100_000
    assert elapsed <= 15.0          # 1/10 of the 60 s budget, with slack for shared runners
    assert rss_mb <= 300


@pytest.mark.perf
@pytest.mark.skipif(sys.platform == "win32", reason="resource module is POSIX-only")
def test_scale_mode_one_million_requests_within_budget() -> None:
    count, elapsed, rss_mb = _run(1_000_000)
    assert count == 1_000_000
    assert elapsed <= 60.0
    assert rss_mb <= 300            # bounded: one developer's records at a time
