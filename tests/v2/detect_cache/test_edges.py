"""Edge cases of the shared machinery and the detectors: range-priced contexts (unknown
endpoint scope, unknown TTL), unpriceable models and pricers that fail, replayers that refuse or
mislabel, other-TTL (OpenAI) writes, lanes without writes, principals missing, mixed cohorts."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from tokenbill.core.builders import make_lane, make_request
from tokenbill.core.errors import PricingError, UsageError
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import Attribution, LaneKind, RequestParams, UsageBuckets
from tokenbill.core.testing import FakePricer
from tokenbill.detect import cache_miss as cm
from tokenbill.detect.cache_miss import MissByCause, Money, Prices, RebuildEvents
from tokenbill.detect.cache_structure import ColdFanout, GatewayDisabled, UnreadWrite
from tokenbill.detect.cache_ttl import ColdResume, TtlAdvisor, ttl_spec

from .helpers import (
    OPUS5,
    OPUS55,
    PRICER,
    T0,
    attribution,
    ctx,
    event,
    fn_replayer,
    lane,
    lane_a1,
    lane_a5,
    lane_a10,
    only,
    table_replayer,
)

ALL = (MissByCause, RebuildEvents, ColdResume, TtlAdvisor, GatewayDisabled, UnreadWrite,
       ColdFanout)


class FailingPricer(FakePricer):
    """A pricer whose every call fails: nothing is priceable (and nothing is zero)."""

    def unit_rates(self, ctx: Any, *, ts_ms: int) -> Any:
        raise PricingError("unit rates unavailable")

    def price_usage(self, *args: Any, **kwargs: Any) -> Any:
        raise PricingError("pricing unavailable")

    def resolve(self, ctx: Any, *, ts_ms: int) -> Any:
        raise PricingError("no rates")


class RefusingReplayer:
    """A replayer that refuses every input with a TokenbillError."""

    def replay(self, *args: Any, **kwargs: Any) -> Any:
        raise UsageError("refused")


class MislabelingReplayer:
    """Wraps a FakeReplayer and relabels its figures with another basis."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def replay(self, *args: Any, **kwargs: Any) -> Any:
        res = self.inner.replay(*args, **kwargs)
        return dataclasses.replace(
            res, saving=dataclasses.replace(res.saving, basis=Basis.CONTRACT),
            baseline=dataclasses.replace(res.baseline, basis=Basis.CONTRACT))


# ---------------------------------------------------------------------------------------------
# pricing machinery
# ---------------------------------------------------------------------------------------------


def test_money_arithmetic() -> None:
    a = Money()
    a.add(100)
    b = Money()
    b.add_range(10, 5, 20)
    a.add_money(b, -1)
    assert (a.point, a.low, a.high, a.ranged) == (90, 80, 95, True)
    a.add(-200)
    a.floor_at_zero()
    assert (a.point, a.low, a.high) == (0, 0, 0)
    fig = b.billed(Basis.LIST)
    assert fig.evidence is Evidence.ESTIMATED and (fig.low_nano, fig.high_nano) == (5, 20)
    assert cm.combine((1, a), (1, None)) is None


def test_unknown_endpoint_scope_is_a_range() -> None:
    """Bedrock with an unknown endpoint scope prices [global, regional]: the billed rewrite is
    ESTIMATED with that range, never EXACT (R9)."""
    attr = attribution(billing_path="bedrock")
    rows = []
    for i, (ts, w5) in enumerate(((0, 100_000), (420, 102_000))):
        rows.append(make_request("BR", i, T0 + ts * 1000, {"cache_write_5m": w5, "output": 5},
                                 OPUS5, attribution=attr, billing_path="bedrock",
                                 channel="bedrock", endpoint_scope="unknown"))
    lane_ = make_lane(rows, lane_key="BR")
    f = only(MissByCause().detect([lane_], ctx(thresholds={"min_usd": "0.10"})), "ttl-expiry")
    assert f.cost_observed.evidence is Evidence.ESTIMATED
    assert f.cost_observed.nano == 100_000 * 6_250            # global point
    assert f.cost_observed.high_nano == 100_000 * 6_875       # regional (×1.1) high end
    prices = Prices(PRICER)
    serving = rows[0].serving_inference
    assert serving is not None
    line = prices.line(serving.pricing, T0, "cache_write_5m", 1_000)
    assert line is not None and line.ranged


def test_write_rate_bucket_and_other_ttl() -> None:
    prices = Prices(PRICER)
    assert prices.write_rate_bucket(UsageBuckets()) == "cache_write_5m"
    assert prices.write_rate_bucket(UsageBuckets(cache_write_1h=5, cache_write_5m=9)) == \
        "cache_write_1h"
    assert cm.write_ttl_s(UsageBuckets(cache_write_other=10, cache_write_other_ttl_s=1800),
                          None) == 1800
    assert cm.write_ttl_s(UsageBuckets(cache_write_unknown=10), "1h") == 3600
    assert cm.write_ttl_s(UsageBuckets(), None) is None
    assert cm.percentile([3, 1, 2], 50) == 2 and cm.percentile([5], 90) == 5


def test_openai_other_ttl_lane() -> None:
    """gpt-5.6-sol writes a single 30-minute class: a 40-minute gap is a TTL expiry priced at
    the other-TTL write rate."""
    attr = Attribution(team="payments", principal="r_o", agent_product="api",
                       billing_path="openai")
    reqs = [make_request("OA", i, T0 + ts * 1000,
                         UsageBuckets(cache_write_other=w, cache_write_other_ttl_s=1800,
                                      output=5), "gpt-5.6-sol", attribution=attr,
                         billing_path="openai")
            for i, (ts, w) in enumerate(((0, 100_000), (2_400, 102_000)))]
    lane_ = make_lane(reqs, lane_key="OA", kind=LaneKind.API_RUN)
    f = only(MissByCause().detect([lane_], ctx(thresholds={"min_usd": "0.10"})), "ttl-expiry")
    assert f.cost_observed.nano == 100_000 * 5_000          # $5/MTok other-TTL write


def test_unpriceable_pricer_yields_nothing() -> None:
    lanes = [lane_a1(), lane_a5(), lane_a10()]
    c = ctx(pricer=FailingPricer(), thresholds={"min_usd": "0"},
            replayer=fn_replayer(lambda ln, p: 1))
    for cls in ALL:
        assert cls().detect(lanes, c) == []


def test_unpriced_model_edge_paths() -> None:
    """An announced but unpriced model: events are counted as unpriced, no dollars invented."""
    unpriced = "claude-sonnet-5-5"
    t = {"min_usd": "0"}
    assert RebuildEvents().detect([lane_a10(model=unpriced)], ctx(thresholds=t)) == []
    assert ColdResume().detect([lane_a5(model=unpriced)], ctx(thresholds=t)) == []
    no_cache = lane("NC", [(i * 40, 0, 0, 0, 20_000, 400) for i in range(6)], model=unpriced)
    assert GatewayDisabled().detect([no_cache], ctx(thresholds=t)) == []
    fan = [lane(f"FO{i}", [(i, 0, 30_000, 0, 0, 300)], kind=LaneKind.SUBAGENT, model=unpriced)
           for i in range(3)]
    assert ColdFanout().detect(fan, ctx(thresholds=t)) == []
    one_shot = lane("OS", [(0, 0, 30_000, 0, 0, 300)], model=unpriced)
    burst_1h = lane("B1", [(0, 0, 0, 30_000, 0, 300), (30, 30_000, 0, 1_000, 0, 300)],
                    model=unpriced)
    unread = lane("UR", [(0, 0, 50_000, 0, 0, 200), (30, 10_000, 42_000, 0, 0, 200)],
                  model=unpriced)
    assert UnreadWrite().detect([one_shot, burst_1h, unread], ctx(thresholds=t)) == []
    compaction = lane("CP", [(0, 0, 150_000, 0, 0, 5), (30, 150_000, 1_000, 0, 0, 5)],
                      model=unpriced,
                      events=[event("CP", 1_000, "compaction", trigger="auto",
                                    pre_tokens=151_000, post_tokens=20_000, duration_ms=1,
                                    dropped_tokens=None)])
    assert RebuildEvents().detect([compaction], ctx(thresholds=t)) == []
    gw = lane("GW", [(0, 0, 40_000, 0, 0, 5)] + [(i * 30, 0, 41_000 + i, 0, 0, 5)
                                                  for i in range(1, 30)],
              model=unpriced, gateway="gw", per_request={i: {"speed": "fast"}
                                                         for i in range(1, 30, 2)})
    assert GatewayDisabled().detect([gw], ctx(thresholds=t)) == []


# ---------------------------------------------------------------------------------------------
# replayer edge cases
# ---------------------------------------------------------------------------------------------


def test_refusing_replayer_gives_no_recoverable() -> None:
    no_cache = lane("NC", [(i * 40, 0, 0, 0, 20_000, 400) for i in range(6)])
    f = only(GatewayDisabled().detect([no_cache], ctx(replayer=RefusingReplayer(),
                                                      thresholds={"min_usd": "0.10"})),
             "no-cache")
    assert f.recoverable is None
    assert TtlAdvisor().detect([lane_a1()], ctx(replayer=RefusingReplayer())) == []


def test_mislabeled_replay_is_not_used() -> None:
    inner = table_replayer({("NC", "repair=restore_caching"): 400_000_000,
                            ("A1", ttl_spec("main", "1h")): 1_150_800_000})
    no_cache = lane("NC", [(i * 40, 0, 0, 0, 20_000, 400) for i in range(6)])
    f = only(GatewayDisabled().detect([no_cache], ctx(replayer=MislabelingReplayer(inner),
                                                      thresholds={"min_usd": "0.10"})),
             "no-cache")
    assert f.recoverable is None
    assert TtlAdvisor().detect([lane_a1()], ctx(replayer=MislabelingReplayer(inner))) == []


def test_unpriced_cohort_gets_no_ttl_advice() -> None:
    lane_ = lane_a1("UP", model="claude-sonnet-5-5")
    assert TtlAdvisor().detect([lane_], ctx(replayer=fn_replayer(lambda ln, p: 10**12))) == []


def test_all_candidates_unusable() -> None:
    """Every candidate replay fails or is mislabeled: no recommendation."""
    class HalfReplayer:
        def __init__(self) -> None:
            self.inner = table_replayer({})

        def replay(self, lanes: Any, policy: Any, **kw: Any) -> Any:
            if policy.is_observed():
                return self.inner.replay(lanes, policy, **kw)
            raise UsageError("refused")

    assert TtlAdvisor().detect([lane_a1()], ctx(replayer=HalfReplayer())) == []


# ---------------------------------------------------------------------------------------------
# detector edge cases
# ---------------------------------------------------------------------------------------------


def test_ambiguous_ttl_transitions_are_counted() -> None:
    lane_ = lane("AM", [(0, 0, 100_000, 0, 0, 500), (305, 0, 102_000, 0, 0, 500)])
    f = only(MissByCause().detect([lane_], ctx(thresholds={"min_usd": "0.1"})), "ttl-expiry")
    agg = dict(next(e for e in f.evidence if e.kind == "aggregate").attrs)
    assert agg["ambiguous_events"] == 1


def test_compaction_cold_edge_paths() -> None:
    t = {"min_usd": "0"}
    before = lane("EB", [(100, 0, 150_000, 0, 0, 5)],
                  events=[event("EB", 50, "compaction", trigger="auto", pre_tokens=150_000,
                                post_tokens=1, duration_ms=1, dropped_tokens=None)])
    no_ttl = lane("NT", [(0, 0, 0, 0, 150_000, 5)],
                  events=[event("NT", 5_000, "compaction", trigger="auto", pre_tokens=150_000,
                                post_tokens=1, duration_ms=1, dropped_tokens=None)])
    assert RebuildEvents().detect([before, no_ttl], ctx(thresholds=t)) == []
    no_pre = lane("NP", [(0, 0, 150_000, 0, 0, 5)],
                  events=[event("NP", 1_000, "compaction", trigger="auto", pre_tokens=0,
                                post_tokens=1, duration_ms=1, dropped_tokens=None)])
    f = only(RebuildEvents().detect([no_pre], ctx(thresholds=t)), "compaction-cold")
    assert f.cost_observed.nano == 150_000 * 5_000          # falls back to the context size


def test_edit_churn_without_unit_rates_uses_line_prices() -> None:
    """Bedrock with an unknown endpoint scope: K* from priced lines (points)."""
    attr = attribution(product="agent_sdk", billing_path="bedrock", team="agents")
    rows = [(0, 0, 100_000), (30, 100_000, 20_000), (60, 20_000, 62_000), (90, 82_000, 2_000)]
    reqs = []
    for i, (ts, r, w) in enumerate(rows):
        reqs.append(make_request(
            "EK", i, T0 + ts * 1000, {"cache_read": r, "cache_write_5m": w, "output": 5}, OPUS5,
            attribution=attr, billing_path="bedrock", channel="bedrock",
            endpoint_scope="unknown", params=RequestParams(model_requested=OPUS5),
            applied_edits=((("clear", 40_000),) if i == 2 else ())))
    lane_ = make_lane(reqs, lane_key="EK", kind=LaneKind.API_RUN)

    class NoUnits(FakePricer):
        def unit_rates(self, ctx: Any, *, ts_ms: int) -> Any:
            return None

    f = only(RebuildEvents().detect([lane_], ctx(pricer=NoUnits(),
                                                 thresholds={"min_usd": "0.01"})), "edit-churn")
    kstar = dict(next(e for e in f.evidence if e.kind == "event").attrs)["kstar"]
    assert kstar == "17.8"      # 62,000·(6,250 − 500)/(40,000·500) = 17.825
    assert f.cost_observed.evidence is Evidence.ESTIMATED


def test_heterogeneity_ignores_lanes_without_principal() -> None:
    lanes = [lane_a1(f"P{i}", principal=f"r_dev{i}" if i < 5 else None) for i in range(7)]
    spec = ttl_spec("main", "1h")
    table = {(f"P{i}", spec): (2_000_000_000 if i in (0, 5, 6) else -100_000_000)
             for i in range(7)}
    f = only(TtlAdvisor().detect(lanes, ctx(replayer=table_replayer(table))), "ttl-heterogeneous")
    het = dict(next(e for e in f.evidence if e.ref == "ttl:heterogeneity").attrs)
    assert het == {"principals": 5, "principals_cheaper": 1}


def test_mixed_claude_code_and_sdk_main_cohort() -> None:
    lanes = [lane_a1("CC1"), lane_a1("SD1", product="agent_sdk")]
    spec = ttl_spec("main", "1h")
    f = only(TtlAdvisor().detect(lanes, ctx(replayer=table_replayer(
        {("CC1", spec): 1_150_800_000, ("SD1", spec): 1_150_800_000}))), "ttl-1h-recommended")
    assert f.fix is not None and f.fix.config_patch == (("promptCacheTtl", '"1h"'),)
    assert "For the SDK lanes" in f.fix.text


def test_single_request_cohort_rule_defaults_to_5m() -> None:
    lanes = [lane(f"S{i}", [(i * 10, 0, 50_000, 0, 0, 100)], kind=LaneKind.API_RUN,
                  product="agent_sdk") for i in range(2)]
    spec = ttl_spec("api_run", "1h")
    f = only(TtlAdvisor().detect(lanes, ctx(replayer=table_replayer(
        {("S0", spec): 2_000_000_000}))), "ttl-1h-recommended")
    rule = dict(next(e for e in f.evidence if e.ref == "ttl:rule-1-in-20").attrs)
    assert rule["verdict"] == "5m" and rule["share_5_60m_pct"] == "0.0"


def test_break_even_absent_without_rates() -> None:
    class NoResolve(FakePricer):
        def resolve(self, ctx: Any, *, ts_ms: int) -> Any:
            return None

    replayer = table_replayer({("A1", ttl_spec("main", "1h")): 1_150_800_000})
    f = only(TtlAdvisor().detect([lane_a1()], ctx(replayer=replayer, pricer=NoResolve())),
             "ttl-1h-recommended")
    assert not [e for e in f.evidence if e.ref == "ttl:keepalive-break-even"]


def test_unread_write_edge_paths() -> None:
    t = {"min_usd": "0"}
    # the next request is unrelated to a write-free request
    no_write = lane("NW", [(0, 0, 0, 0, 5_000, 5), (30, 0, 0, 0, 5_100, 5)])
    assert UnreadWrite().detect([no_write], ctx(thresholds=t)) == []
    # ttl unknown (no writes before): default 5m
    one_shot_no_write = lane("NO", [(0, 0, 0, 0, 5_000, 5)])
    assert UnreadWrite().detect([one_shot_no_write], ctx(thresholds=t)) == []


def test_fanout_unknown_ttl_bucket_prices_a_range() -> None:
    attr = attribution()
    lanes = []
    for i, off in enumerate((0, 2, 4)):
        req = make_request(f"FU{i}", 0, T0 + off * 1000,
                           {"cache_write_unknown": 30_000, "output": 5}, OPUS55,
                           attribution=attr, billing_path="api_key", write_ttl_hint="5m")
        lanes.append(make_lane([req], lane_key=f"FU{i}", kind=LaneKind.SUBAGENT))
    f = only(ColdFanout().detect(lanes, ctx(thresholds={"min_usd": "0.01"})), "cold-fanout")
    assert f.cost_observed.low_nano == 2 * 30_000 * 4_800
    assert f.cost_observed.high_nano == 2 * 30_000 * (8_000 - 200)


@pytest.mark.parametrize("cls", ALL)
def test_empty_input(cls: Any) -> None:
    assert cls().detect([], ctx(replayer=table_replayer({}))) == []
