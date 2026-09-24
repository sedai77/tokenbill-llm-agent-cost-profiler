"""Performance budgets (SPEC §17): usage-level replay O(n), 10⁶ requests × 1 policy ≤ 30 s;
calibrate (two streaming passes) over 10⁶ requests ≤ 120 s. The full-size tests carry the
``perf`` marker (nightly); the PR variants run at 1/10 size with 1/10 budgets. Budgets are checked
on process CPU time (best of three runs) so parallel builds on a shared machine do not flake."""

from __future__ import annotations

import random
import time
from collections.abc import Callable

import pytest

from tokenbill.sim.calibrate import calibrate_lanes

from .helpers import DAY_S, PRICER, RULES, SDK, replay, table


def perf_lanes(n_requests: int, *, per_lane: int = 100, seed: int = 0) -> list:
    """Synthetic SDK lanes (Opus 5.5) of *per_lane* requests: bursty hits and idle misses."""
    rnd = random.Random(seed)
    lanes = []
    for k in range(n_requests // per_lane):
        t = rnd.randint(0, 20) * DAY_S + rnd.randint(0, 40_000)
        total = 30_000
        rows = [(t, 0, total, 0, 0, 300)]
        for _ in range(1, per_lane):
            gap = rnd.choice((20, 45, 90, 240, 420, 900))
            t += gap
            new = total + rnd.randint(500, 3_000)
            if gap <= 300:
                rows.append((t, total, new - total, 0, 0, 300))
            else:
                rows.append((t, 0, new, 0, 0, 300))
            total = new if new < 400_000 else 30_000
        lanes.append(table(rows, lane_key=f"P{k:06d}", attribution=SDK))
    return lanes


def _cpu(fn: Callable[[], object], repeat: int = 3) -> float:
    best = float("inf")
    for _ in range(repeat):
        start = time.process_time()
        fn()
        best = min(best, time.process_time() - start)
    return best


def _replay_budget(lanes: list, budget_s: float, *, repeat: int) -> None:
    n = sum(len(lane.requests) for lane in lanes)
    elapsed = _cpu(lambda: replay(lanes, "ttl=1h", keep=False), repeat=repeat)
    assert elapsed <= budget_s, f"replay of {n} requests took {elapsed:.1f} s"


def _calibrate_budget(lanes: list, budget_s: float) -> None:
    n = sum(len(lane.requests) for lane in lanes)
    elapsed = _cpu(lambda: calibrate_lanes(lanes, pricer=PRICER, rules=RULES), repeat=1)
    assert elapsed <= budget_s, f"calibrate over {n} requests took {elapsed:.1f} s"


@pytest.fixture(scope="module")
def pr_lanes() -> list:
    lanes = perf_lanes(100_000)
    assert sum(len(lane.requests) for lane in lanes) == 100_000
    return lanes


def test_replay_budget_pr_size(pr_lanes: list) -> None:
    _replay_budget(pr_lanes, 3.0, repeat=2)


def test_replay_scales_linearly(pr_lanes: list) -> None:
    half = pr_lanes[: len(pr_lanes) // 2]
    t_half = _cpu(lambda: replay(half, "ttl=1h", keep=False), repeat=2)
    t_full = _cpu(lambda: replay(pr_lanes, "ttl=1h", keep=False), repeat=2)
    assert t_full <= 2.3 * t_half, (t_half, t_full)


def test_calibrate_budget_pr_size(pr_lanes: list) -> None:
    _calibrate_budget(pr_lanes, 12.0)


@pytest.mark.perf
def test_replay_budget_full_size() -> None:
    _replay_budget(perf_lanes(1_000_000), 30.0, repeat=1)


@pytest.mark.perf
def test_calibrate_budget_full_size() -> None:
    _calibrate_budget(perf_lanes(1_000_000, seed=1), 120.0)
