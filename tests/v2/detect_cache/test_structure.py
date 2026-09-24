"""``cache.gateway-disabled`` (no-cache, beta-header-dropped, tool-search-disabled),
``cache.unread-write`` (write-never-read, oversized-ttl, tail-writes) and ``cache.cold-fanout``,
hand-computed to the nano."""

from __future__ import annotations

from tokenbill.core.builders import make_block, make_fingerprint
from tokenbill.core.labels import Evidence
from tokenbill.core.records import LaneKind
from tokenbill.detect.cache_structure import ColdFanout, GatewayDisabled, UnreadWrite

from .helpers import (
    CAPS,
    CWD,
    SONNET5,
    ctx,
    diag,
    lane,
    lane_a2,
    only,
    table_replayer,
)

MIN_1C = {"min_usd": "0.01"}


# ---------------------------------------------------------------------------------------------
# gateway-disabled
# ---------------------------------------------------------------------------------------------


def _no_cache_lane(key: str = "NC", n: int = 6, start: int = 20_000, **kw):
    rows = [(i * 40, 0, 0, 0, start + 2_000 * i, 400) for i in range(n)]
    return lane(key, rows, team="platform", gateway="litellm-proxy", **kw)


def test_no_cache_lane() -> None:
    """Six requests, every input token uncached: ΣU = 6·20,000 + 2,000·(0+…+5) = 150,000 at
    4,000 nano = $0.60 (EXACT); the restore_caching replay gives the recoverable."""
    replayer = table_replayer({("NC", "repair=restore_caching"): 420_000_000})
    f = only(GatewayDisabled().detect([_no_cache_lane()],
                                      ctx(replayer=replayer, thresholds={"min_usd": "0.10"})),
             "no-cache")
    assert f.cost_observed.nano == 150_000 * 4_000
    assert f.cost_observed.evidence is Evidence.EXACT
    assert f.recoverable is not None and f.recoverable.nano == 420_000_000
    assert f.lever_ids == ("gateway.restore_caching",)
    assert f.fix is not None and f.fix.target == "gateway"
    assert "cache_control_injection_points" in f.fix.text
    assert "(behind a gateway)" in f.title
    assert f.scope.dims == (("lane_kind", "main"), ("team", "platform"))


def test_no_cache_trigger_boundaries() -> None:
    """≥ 5 requests; median T ≥ max(min cacheable, 4,096); no read or write anywhere."""
    t = {"min_usd": "0"}
    assert GatewayDisabled().detect([_no_cache_lane(n=4)], ctx(thresholds=t)) == []
    small = lane("SM", [(i * 40, 0, 0, 0, 4_095, 100) for i in range(6)], team="platform")
    assert GatewayDisabled().detect([small], ctx(thresholds=t)) == []
    ok = lane("OK", [(i * 40, 0, 0, 0, 4_096, 100) for i in range(6)], team="platform")
    only(GatewayDisabled().detect([ok], ctx(thresholds=t)), "no-cache")
    rows = [(i * 40, 0, 0, 0, 20_000, 100) for i in range(6)]
    rows[3] = (120, 0, 1_000, 0, 19_000, 100)
    cached = lane("CA", rows, team="platform")
    assert not [f for f in GatewayDisabled().detect([cached], ctx(thresholds=t))
                if f.kind == "no-cache"]


def _ttl_expiry_lane(key: str, n: int = 21, **kw):
    """n requests 420 s apart, each a full 5m rewrite of 40,000 tokens (a miss per gap)."""
    return lane(key, [(i * 420, 0, 40_000, 0, 0, 200) for i in range(n)], **kw)


def test_beta_header_dropped() -> None:
    """Team configured for 1h, 21 requests writing 5m only: 20 TTL-expiry misses of 40,000
    tokens at 5,000 nano = $4.00 billed (EXACT); premium 20·40,000·4,800 (upper bound)."""
    t = {"policy.ttl.payments": "1h"}
    f = only(GatewayDisabled().detect([_ttl_expiry_lane("B1")], ctx(thresholds=t)),
             "beta-header-dropped")
    assert f.cost_observed.nano == 20 * 40_000 * 5_000
    assert f.recoverable is not None and f.recoverable.nano == 20 * 40_000 * 4_800
    assert f.recoverable.upper_bound
    assert f.lever_ids == ("gateway.restore_caching",)
    assert f.fix is not None and "anthropic-beta" in f.fix.text
    agg = dict(next(e for e in f.evidence if e.ref == "beta:5m-only").attrs)
    assert agg["requests"] == 21


def test_beta_header_needs_config_and_20_requests() -> None:
    assert not [f for f in GatewayDisabled().detect([_ttl_expiry_lane("B2")], ctx())
                if f.kind == "beta-header-dropped"]
    t = {"policy.ttl.payments": "1h"}
    assert not [f for f in GatewayDisabled().detect([_ttl_expiry_lane("B3", n=19)],
                                                    ctx(thresholds=t))
                if f.kind == "beta-header-dropped"]
    other_team = {"policy.ttl.search": "1h"}
    assert not [f for f in GatewayDisabled().detect([_ttl_expiry_lane("B4")],
                                                    ctx(thresholds=other_team))
                if f.kind == "beta-header-dropped"]


def test_tool_search_disabled_behind_gateway() -> None:
    """Claude Code behind a gateway: 3 tools-changed misses in 30 requests (10 per 100 ≥ 3)."""
    rows = []
    prefix = 0
    over = {}
    for i in range(30):
        if i in (5, 15, 25):
            rows.append((i * 30, 0, prefix + 1_000, 0, 0, 200))
            over[i] = {"diagnostics": diag("tools_changed")}
        elif i == 0:
            rows.append((0, 0, 30_000, 0, 0, 200))
        else:
            rows.append((i * 30, prefix, 1_000, 0, 0, 200))
        prefix = rows[-1][1] + rows[-1][2]
    lane_ = lane("TS", rows, gateway="corp-gateway", per_request=over)
    f = only(GatewayDisabled().detect([lane_], ctx(thresholds=MIN_1C)), "tool-search-disabled")
    expected = sum((30_000 + 1_000 * (i - 1)) * 5_000 for i in (5, 15, 25))
    assert f.n_events == 3 and f.cost_observed.nano == expected
    assert f.fix is not None and f.fix.config_patch == (("env.ENABLE_TOOL_SEARCH", '"true"'),)
    assert f.lever_ids == ("cc.tool_search",)
    # without the gateway (first-party base URL) nothing is flagged
    plain = lane("TP", rows, per_request=over)
    assert not [x for x in GatewayDisabled().detect([plain], ctx(thresholds=MIN_1C))
                if x.kind == "tool-search-disabled"]
    # below 3 per 100 requests nothing is flagged
    strict = ctx(thresholds={**MIN_1C, "cache.gateway-disabled.tool_search_misses_per_100": "11"})
    assert not [x for x in GatewayDisabled().detect([lane_], strict)
                if x.kind == "tool-search-disabled"]


# ---------------------------------------------------------------------------------------------
# unread-write
# ---------------------------------------------------------------------------------------------


def test_write_never_read() -> None:
    """Request 0 writes 50,000; request 1 (30 s later) reads only 10,000 of them: 40,000 unread
    tokens at w5 − u = 1,000 nano = $0.04 (ESTIMATED)."""
    lane_ = lane("WN", [(0, 0, 50_000, 0, 0, 200), (30, 10_000, 42_000, 0, 0, 200),
                        (60, 52_000, 1_000, 0, 0, 200)])
    f = only(UnreadWrite().detect([lane_], ctx(thresholds=MIN_1C)), "write-never-read")
    assert f.cost_observed.nano == 40_000 * 1_000
    assert f.cost_observed.evidence is Evidence.ESTIMATED
    assert f.recoverable is None
    assert f.lever_ids == ()          # no fingerprints: guidance only
    assert f.fix is not None and "breakpoint" in f.fix.text


def test_write_never_read_links_block_lever_with_fingerprints() -> None:
    fp = make_fingerprint([make_block("h" * 32, tier="system", kind="system_text")])
    lane_ = lane("WF", [(0, 0, 50_000, 0, 0, 200), (30, 10_000, 42_000, 0, 0, 200)],
                 per_request={0: {"fingerprint": fp}})
    f = only(UnreadWrite().detect([lane_], ctx(thresholds=MIN_1C)), "write-never-read")
    assert f.lever_ids == ("blocks.breakpoints",)
    assert f.lever_class == "cache_transform"


def test_unread_after_idle_or_reset_is_not_placement() -> None:
    t = {"min_usd": "0"}
    idle = lane("WI", [(0, 0, 50_000, 0, 0, 200), (400, 0, 52_000, 0, 0, 200)])
    assert not [f for f in UnreadWrite().detect([idle], ctx(thresholds=t))
                if f.kind == "write-never-read"]
    switch = lane("WS", [(0, 0, 50_000, 0, 0, 200), (30, 0, 52_000, 0, 0, 200)],
                  per_request={1: {"model": SONNET5}})
    assert not [f for f in UnreadWrite().detect([switch], ctx(thresholds=t))
                if f.kind == "write-never-read"]
    full = lane("WR", [(0, 0, 50_000, 0, 0, 200), (30, 47_500, 3_000, 0, 0, 200)])
    assert not [f for f in UnreadWrite().detect([full], ctx(thresholds=t))
                if f.kind == "write-never-read"]      # 95% read


def test_oversized_ttl_is_exact_arithmetic() -> None:
    """A.2b: 106,000 1h-written tokens on a lane with no 5–60 min gap: 106,000·(8,000 − 5,000)
    = $0.318, EXACT."""
    f = only(UnreadWrite().detect([lane_a2("OV", hour=True)], ctx(thresholds={"min_usd": "0.10"})),
             "oversized-ttl")
    assert f.cost_observed.nano == 318_000_000
    assert f.cost_observed.evidence is Evidence.EXACT
    assert f.recoverable is None
    assert f.fix is not None and f.fix.config_patch == (("promptCacheTtl", '"5m"'),)
    assert set(f.lever_ids) == {"cc.prompt_cache_ttl.main"}


def test_1h_lane_with_a_mid_gap_is_not_oversized() -> None:
    lane_ = lane("OK1", [(0, 0, 0, 100_000, 0, 500), (30, 100_000, 0, 2_000, 0, 500),
                         (1_000, 102_000, 0, 2_000, 0, 500)])
    assert not [f for f in UnreadWrite().detect([lane_], ctx(thresholds={"min_usd": "0"}))
                if f.kind == "oversized-ttl"]


def test_tail_writes_one_shot_lanes() -> None:
    """Single-request lanes writing 30,000 tokens: premium over uncached 30,000·1,000 each."""
    lanes = [lane(f"T{i}", [(i * 100, 0, 30_000, 0, 10, 200)], kind=LaneKind.API_RUN,
                  product="api") for i in range(3)]
    f = only(UnreadWrite().detect(lanes, ctx(thresholds=MIN_1C)), "tail-writes")
    assert f.n_events == 3 and f.cost_observed.nano == 3 * 30_000_000
    assert f.cost_observed.evidence is Evidence.ESTIMATED
    assert f.recoverable is None


# ---------------------------------------------------------------------------------------------
# cold fan-out
# ---------------------------------------------------------------------------------------------


def _fanout_lanes(offsets=(0, 2, 5, 15), writes=(20_000, 22_000, 30_000, 25_000), cwd=CWD):
    return [lane(f"FO{i}", [(off, 0, w, 0, 0, 300), (off + 30, w, 1_000, 0, 0, 300)],
                 kind=LaneKind.SUBAGENT, cwd_key=cwd)
            for i, (off, w) in enumerate(zip(offsets, writes, strict=True))]


def test_cold_fanout_upper_bound_range() -> None:
    """Three subagents start within 10 s on one prefix (the fourth 15 s later does not join):
    N = 3, P ∈ [min 20,000, median 22,000]: 2·P·(w5 − r) = 2·P·4,800."""
    lanes = _fanout_lanes()
    replayer = table_replayer({("FO1", "repair=stagger_fanout"): 96_000_000,
                               ("FO2", "repair=stagger_fanout"): 96_000_000})
    f = only(ColdFanout().detect(lanes, ctx(replayer=replayer, thresholds=MIN_1C)),
             "cold-fanout")
    assert (f.cost_observed.low_nano, f.cost_observed.nano, f.cost_observed.high_nano) == (
        192_000_000, 192_000_000, 211_200_000)
    assert f.cost_observed.upper_bound and f.cost_observed.evidence is Evidence.ESTIMATED
    assert f.recoverable is not None and f.recoverable.nano == 192_000_000
    assert f.recoverable.upper_bound
    assert f.n_events == 1 and f.n_lanes == 3
    assert f.lever_ids == ("fanout.stagger",)
    assert f.fix is not None and "first token" in f.fix.text


def test_fanout_needs_same_scope_model_and_cwd() -> None:
    t = {"min_usd": "0"}
    other_cwd = _fanout_lanes(offsets=(0, 2), writes=(20_000, 22_000))
    other_cwd[1] = lane("FOX", [(2, 0, 22_000, 0, 0, 300)], kind=LaneKind.SUBAGENT,
                        cwd_key="h_" + "d" * 20)
    assert ColdFanout().detect(other_cwd, ctx(thresholds=t)) == []
    warm = [lane(f"W{i}", [(i, 15_000, 2_000, 0, 0, 300)], kind=LaneKind.SUBAGENT)
            for i in range(3)]                     # R ≥ 0.5·T: already warm
    assert ColdFanout().detect(warm, ctx(thresholds=t)) == []
    tiny = [lane(f"Y{i}", [(i, 0, 1_000, 0, 0, 300)], kind=LaneKind.SUBAGENT) for i in range(3)]
    assert ColdFanout().detect(tiny, ctx(thresholds=t)) == []      # W < 1,024


def test_fanout_without_replayer_gates_on_cost() -> None:
    f = only(ColdFanout().detect(_fanout_lanes(), ctx(thresholds=MIN_1C)), "cold-fanout")
    assert f.recoverable is None


def test_capability_declarations() -> None:
    assert GatewayDisabled.requires == frozenset({"usage_sequence"})
    assert UnreadWrite.requires <= CAPS and ColdFanout.requires <= CAPS
