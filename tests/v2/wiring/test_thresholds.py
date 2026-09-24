"""Rulings R-E40 / R-E24: the org median COMPACTION summary size, computed once per run and passed
to detectors (``ctx.thresholds``) and to compact-window replays (``post=``)."""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tokenbill.core.builders import make_lane, make_request
from tokenbill.core.errors import UsageError
from tokenbill.core.records import LaneEvent, LaneEventKind
from tokenbill.core.shards import plan_shards, shard_of_lanes
from tokenbill.core.testing import MemoryStore
from tokenbill.core.types import IngestOptions, Policy
from tokenbill.pipeline.common import (
    COMPACTION_POST_TOKENS_KEY,
    analysis_thresholds,
    compaction_post_tokens,
    ingest_paths,
    median_tokens,
    org_compaction_median,
    with_compaction_post,
)

from .support import ORG_KEY, T0, make_env, req, write_fake

pytestmark = pytest.mark.usefixtures("fake_adapters")

WINDOW = {"since_ms": T0, "until_ms": T0 + 86_400_000}


def _compaction(lane: str, post: int | None, ts: int, team: str = "payments") -> dict:
    rec = {"lane": lane, "event": "compaction", "ts": ts, "team": team}
    if post is not None:
        rec["post"] = post
    return rec


def _store(tmp_path: Path) -> MemoryStore:
    records = []
    posts = {"A": [18_000, 20_000], "B": [30_000], "C": [21_000, None], "D": [25_000]}
    for n, (lane, values) in enumerate(sorted(posts.items())):
        team = "payments" if lane in "AB" else "search"
        records.append(req(lane, 0, team=team, ts=T0 + 3_600_000 + n))
        for i, post in enumerate(values):
            records.append(_compaction(lane, post, T0 + 3_700_000 + 10 * n + i, team))
    records.append(_compaction("A", 99_999, T0 - 1000))     # outside the window
    store = MemoryStore(org_key=ORG_KEY)
    ingest_paths(store, [write_fake(tmp_path / "c.jsonl", records)], make_env(), IngestOptions())
    return store


def test_org_median_over_the_whole_window(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert sorted(compaction_post_tokens(store.iter_lanes(**WINDOW))) == [
        18_000, 20_000, 21_000, 25_000, 30_000]
    assert org_compaction_median(store, **WINDOW) == 21_000
    # computed from the whole store once: a per-shard median would differ (R-E40)
    lanes = list(store.iter_lanes(**WINDOW))
    per_shard = [median_tokens(compaction_post_tokens(shard_of_lanes(lanes, key)))
                 for key in plan_shards(store.lane_index(**WINDOW))]
    assert per_shard == [20_000, 23_000]


def test_only_compaction_events_with_post_tokens_count() -> None:
    events = [LaneEvent("L", T0, LaneEventKind.CLEAR),
              LaneEvent("L", T0 + 1, LaneEventKind.COMPACTION, (("post_tokens", 7),)),
              LaneEvent("L", T0 + 2, LaneEventKind.COMPACTION, (("trigger", "manual"),))]
    lane = make_lane([make_request("L", 0, T0)], events=events)
    assert compaction_post_tokens([lane]) == [7]


def test_median_rules() -> None:
    assert median_tokens([]) is None
    assert median_tokens([5]) == 5
    assert median_tokens([1, 2]) == 2           # 1.5 → half-even 2
    assert median_tokens([2, 3]) == 2           # 2.5 → half-even 2
    assert median_tokens([3, 1, 2, 10]) == 2    # 2.5 → 2
    assert median_tokens([4, 1, 2, 10]) == 3


@given(st.lists(st.integers(min_value=0, max_value=2**40), max_size=40), st.integers())
def test_median_is_order_independent(values: list[int], seed: int) -> None:
    shuffled = list(values)
    random.Random(seed).shuffle(shuffled)
    got = median_tokens(shuffled)
    assert got == median_tokens(values)
    if values:
        ordered = sorted(values)
        assert ordered[(len(values) - 1) // 2] <= got <= ordered[len(values) // 2]


def test_analysis_thresholds(tmp_path: Path) -> None:
    store = _store(tmp_path)
    env = make_env(thresholds={"defaults.effort": "high"})
    assert analysis_thresholds(env, store, **WINDOW) == {
        COMPACTION_POST_TOKENS_KEY: "21000", "defaults.effort": "high"}
    # a configured value wins; no COMPACTION event → no key
    pinned = make_env(thresholds={COMPACTION_POST_TOKENS_KEY: "12345"})
    assert analysis_thresholds(pinned, store, **WINDOW) == {COMPACTION_POST_TOKENS_KEY: "12345"}
    assert analysis_thresholds(make_env(), MemoryStore(), **WINDOW) == {}


def test_with_compaction_post() -> None:
    policy = Policy(name="cw", compaction_window=(400_000, None))
    assert with_compaction_post(policy, 21_000).compaction_window == (400_000, 21_000)
    explicit = Policy(name="cw", compaction_window=(400_000, 9_000))
    assert with_compaction_post(explicit, 21_000) is explicit
    plain = Policy(name="ttl", ttl=(("all", "1h"),))
    assert with_compaction_post(plain, 21_000) is plain
    assert with_compaction_post(policy, None) is policy
    with pytest.raises(UsageError):
        with_compaction_post(policy, -1)
