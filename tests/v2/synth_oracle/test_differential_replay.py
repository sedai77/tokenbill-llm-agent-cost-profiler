"""Merge gate 1 (SPEC §9.8, PLAN §1.5): the fast ``UsageReplayer`` equals ``ReferenceReplay`` to
the nano — points and bounds of every request outcome and of baseline / cost / saving, serving
usage, inserted calls, per-lane totals and counts — on ≥ 500 seeded random lanes per policy family
(documented mode). On failure the message names the first differing request.

The gate test skips until ``tokenbill.sim.usage_replay`` is importable (REPLAY, same wave). The
harness self-tests below always run: the comparator accepts the oracle against itself and reports
a planted difference at the right request.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from tokenbill.core.records import InferenceKind, UsageBuckets
from tokenbill.core.types import ReplayResult
from tokenbill.synth.lanes_gen import FAMILIES, family_policies, random_lanes
from tokenbill.synth.oracle import ReferenceReplay

from .helpers import first_difference, replay

N_LANES = 500
SEED = 20_260_923


def _differential(fast: Any, family: str, seed: int, n: int) -> int:
    """Replay *n* lanes of *family* under each of its policies with both engines; returns the
    number of compared request outcomes."""
    lanes = random_lanes(seed, n, family=family)
    compared = 0
    for policy in family_policies(family):
        expected = replay(lanes, policy)
        actual = replay(lanes, policy, replayer=fast)
        diff = first_difference(expected, actual, lanes)
        assert diff is None, f"family {family!r}, seed {seed}: {diff}"
        compared += len(expected.outcomes or ())
    return compared


@pytest.mark.gate
@pytest.mark.slow
@pytest.mark.parametrize("family", FAMILIES)
def test_usage_replayer_equals_the_oracle(family: str) -> None:
    usage_replay = pytest.importorskip("tokenbill.sim.usage_replay")
    compared = _differential(usage_replay.UsageReplayer(), family, SEED, N_LANES)
    assert compared >= N_LANES


# ---------------------------------------------------------------------------------------------
# harness self-tests
# ---------------------------------------------------------------------------------------------


class _Perturbed:
    """Wraps the oracle and applies *edit* to its result (a stand-in for a buggy fast engine)."""

    def __init__(self, edit: Any) -> None:
        self.edit = edit

    def replay(self, lanes, policy, **kw):  # type: ignore[no-untyped-def]
        return self.edit(ReferenceReplay().replay(lanes, policy, **kw))


@pytest.mark.parametrize("family", ["ttl", "keepalive", "repairs", "allowance"])
def test_the_oracle_agrees_with_itself(family: str) -> None:
    assert _differential(ReferenceReplay(), family, 5, 40) > 0


def _bump_outcome(index: int, **changes: Any):
    def edit(res: ReplayResult) -> ReplayResult:
        outs = list(res.outcomes or ())
        outs[index] = replace(outs[index], **changes)
        return replace(res, outcomes=tuple(outs))
    return edit


def test_harness_names_the_first_differing_request() -> None:
    lanes = random_lanes(1, 5, family="ttl")
    policy = family_policies("ttl")[1]
    expected = replay(lanes, policy)
    first = expected.outcomes[0]  # type: ignore[index]
    bad = _Perturbed(_bump_outcome(0, cost_nano=(first.cost_nano or 0) + 1))
    msg = first_difference(expected, replay(lanes, policy, replayer=bad), lanes)
    assert msg is not None and first.request_id in msg and "cost (point, low, high)" in msg
    assert "request #0" in msg and "ttl=1h" in msg


@pytest.mark.parametrize("edit,fragment", [
    (_bump_outcome(1, usage=UsageBuckets(output=1)), "serving usage"),
    (_bump_outcome(1, low_nano=-1), "cost (point, low, high)"),
    (lambda r: replace(r, added_calls=r.added_calls + 1), "added_calls"),
    (lambda r: replace(r, keepalive_pings=7), "keepalive_pings"),
    (lambda r: replace(r, per_lane=r.per_lane[1:]), "per_lane differs"),
    (lambda r: replace(r, saving=replace(r.saving, nano=(r.saving.nano or 0) + 1,
                                         low_nano=None, high_nano=None)), "saving"),
    (lambda r: replace(r, outcomes=None), "keep outcomes"),
    (lambda r: replace(r, outcomes=(r.outcomes or ())[1:]), "missing outcome"),
])
def test_harness_reports_every_kind_of_difference(edit: Any, fragment: str) -> None:
    lanes = random_lanes(2, 4, family="ttl")
    policy = family_policies("ttl")[1]
    expected = replay(lanes, policy)
    msg = first_difference(expected, replay(lanes, policy, replayer=_Perturbed(edit)), lanes)
    assert msg is not None and fragment in msg, msg


def test_harness_compares_inserted_calls_by_kind() -> None:
    lanes = random_lanes(3, 20, family="keepalive")
    policy = family_policies("keepalive")[1]
    expected = replay(lanes, policy)
    index = next(i for i, o in enumerate(expected.outcomes or ()) if o.extra)
    ping = expected.outcomes[index].extra[0]  # type: ignore[index]
    doubled = _bump_outcome(index, extra=(*expected.outcomes[index].extra,  # type: ignore[index]
                                          replace(ping, kind=InferenceKind.KEEPALIVE)))
    msg = first_difference(expected, replay(lanes, policy, replayer=_Perturbed(doubled)), lanes)
    assert msg is not None and "inserted calls" in msg
    # the same billed tokens split over fewer Inference objects compare equal
    merged_usage = ping.usage + ping.usage
    two = [o for o in expected.outcomes or () if len(o.extra) == 2]
    if two:
        i2 = (expected.outcomes or ()).index(two[0])
        fused = _bump_outcome(i2, extra=(replace(two[0].extra[0],
                                                 usage=two[0].extra[0].usage
                                                 + two[0].extra[1].usage),))
        assert first_difference(expected, replay(lanes, policy, replayer=_Perturbed(fused)),
                                lanes) is None
    assert merged_usage.cache_read == 2 * ping.usage.cache_read
