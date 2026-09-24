"""Gate (merge gate 1): the cache detectors with REPLAY's real ``UsageReplayer`` on the SPEC
Appendix A lanes — A.1 → ``ttl-1h-recommended`` $1.1508, A.2b → ``ttl-5m-recommended`` $0.318
(``min_usd`` 0.10), A.2 → no TTL finding, A.4 → keepalive on an SDK lane, and the
``restore_caching`` / ``stagger_fanout`` / ``fallback_credit`` repairs replayed."""

from __future__ import annotations

from typing import Any

import pytest

from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import LaneKind
from tokenbill.detect.cache_miss import SwitchChurn
from tokenbill.detect.cache_structure import ColdFanout, GatewayDisabled
from tokenbill.detect.cache_ttl import TtlAdvisor

from .helpers import ctx, healthy_lanes, lane, lane_a1, lane_a2, only

pytestmark = pytest.mark.gate
usage_replay = pytest.importorskip("tokenbill.sim.usage_replay")


def _ctx(**kw: Any):
    return ctx(replayer=usage_replay.UsageReplayer(), **kw)


def test_a1_lane_recommends_1h_with_the_hand_computed_saving() -> None:
    f = only(TtlAdvisor().detect([lane_a1()], _ctx()), "ttl-1h-recommended")
    assert f.recoverable is not None and f.recoverable.nano == 1_150_800_000     # $1.1508
    assert f.recoverable.evidence is Evidence.ESTIMATED
    assert f.cost_observed.nano == 2_100_000_000                                  # $2.10


def test_a2b_lane_recommends_5m() -> None:
    lane_ = lane_a2("A2b", hour=True)
    assert TtlAdvisor().detect([lane_], _ctx()) == []                  # $0.318 < $1.00 default
    f = only(TtlAdvisor().detect([lane_], _ctx(thresholds={"min_usd": "0.10"})),
             "ttl-5m-recommended")
    assert f.recoverable is not None and f.recoverable.nano == 318_000_000       # $0.318
    assert f.cost_observed.nano == 949_200_000


def test_a2_lane_already_optimal() -> None:
    assert TtlAdvisor().detect([lane_a2()], _ctx(thresholds={"min_usd": "0.10"})) == []


def test_a4_sdk_lane_keepalive() -> None:
    """A.4: keepalive costs $0.6924 against $2.10 observed (and $0.9492 under 1h)."""
    lane_ = lane_a1("A4", kind=LaneKind.API_RUN, product="agent_sdk", team="agents")
    f = only(TtlAdvisor().detect([lane_], _ctx()), "keepalive-recommended")
    assert f.recoverable is not None and f.recoverable.nano == 2_100_000_000 - 692_400_000
    replays = dict(next(e for e in f.evidence if e.ref == "ttl:replays").attrs)
    assert replays["saving_ttl_1h_nano"] == 1_150_800_000


def test_allowance_lane_saving_is_list_equivalent() -> None:
    f = only(TtlAdvisor().detect([lane_a1("AL", billing_path="subscription")], _ctx()),
             "ttl-1h-recommended")
    assert f.recoverable is not None and f.recoverable.basis is Basis.LIST_EQUIVALENT
    assert f.title.startswith("Allowance headroom:")


def test_healthy_lanes_get_no_ttl_advice() -> None:
    assert TtlAdvisor().detect(healthy_lanes(), _ctx(thresholds={"min_usd": "0.01"})) == []


def test_repairs_replayed() -> None:
    no_cache = lane("NC", [(i * 40, 0, 0, 0, 20_000 + 2_000 * i, 400) for i in range(6)],
                    team="platform")
    f = only(GatewayDisabled().detect([no_cache], _ctx(thresholds={"min_usd": "0.10"})),
             "no-cache")
    assert f.recoverable is not None and 0 < f.recoverable.nano < f.cost_observed.nano
    fan = [lane(f"FO{i}", [(off, 0, 30_000, 0, 0, 300), (off + 30, 30_000, 1_000, 0, 0, 300)],
                kind=LaneKind.SUBAGENT) for i, off in enumerate((0, 2, 5))]
    g = only(ColdFanout().detect(fan, _ctx(thresholds={"min_usd": "0.01"})), "cold-fanout")
    assert g.recoverable is not None and g.recoverable.upper_bound
    assert g.recoverable.nano is not None and g.recoverable.nano > 0


def test_fallback_credit_repair() -> None:
    from tokenbill.core.builders import make_lane

    from .helpers import FABLE5, OPUS48, fallback_request, request

    r0 = request("F", 0, 0, w5=100_000, model=FABLE5, session_key="s_F")
    r1 = fallback_request("F", 1, 30, declined_model=FABLE5, w5=102_000, model=OPUS48)
    lane_ = make_lane([r0, r1], lane_key="F", session_key="s_F")
    f = only(SwitchChurn().detect([lane_], _ctx(thresholds={"min_usd": "0.10"})),
             "refusal-fallback-no-credit")
    assert f.cost_observed.nano == 625_000_000
    assert f.recoverable is not None and f.recoverable.nano is not None
    assert f.recoverable.nano > 0
