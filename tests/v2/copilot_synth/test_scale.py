"""Scale mode (brief Build 5; addendum §17): ``iter_records`` streams a 5,000-seat enterprise over
30 days in bounded memory (``perf``, nightly) — and a 1/10-size variant on every PR."""

from __future__ import annotations

import time
import tracemalloc
from collections import Counter

import pytest

from tokenbill.core.records import CostLine, LicenseSnapshot, UsageAggregate
from tokenbill.synth.copilot_world import iter_records


def _stream(users: int) -> tuple[Counter, float, int]:
    counts: Counter = Counter()
    tracemalloc.start()
    t0 = time.perf_counter()
    try:
        for rec in iter_records(seed=7, users=users):
            counts[type(rec).__name__] += 1
        elapsed = time.perf_counter() - t0
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    return counts, elapsed, peak


def _check(counts: Counter, users: int) -> None:
    assert counts[LicenseSnapshot.__name__] == users * 2        # August and September snapshots
    assert counts[CostLine.__name__] > users * 15
    assert counts[UsageAggregate.__name__] > 0


def test_scale_tenth_streams_in_bounded_memory() -> None:
    counts, elapsed, peak = _stream(500)
    _check(counts, 500)
    assert elapsed < 60
    assert peak < 64 * 2**20


@pytest.mark.perf
def test_scale_full_5000_seats_30_days() -> None:
    counts, elapsed, peak = _stream(5_000)
    _check(counts, 5_000)
    assert elapsed < 600
    assert peak < 256 * 2**20
