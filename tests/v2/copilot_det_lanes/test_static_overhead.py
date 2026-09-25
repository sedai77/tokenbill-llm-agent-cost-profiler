"""``static-overhead`` (addendum §10.3): static tokens from COMPACTION events (else the
static-prefix floor) carried at each request's read / write / uncached mix; the tool-definition
carry × ``TOOL_SEARCH_REDUCTION_BAND`` is the ESTIMATED upper-bound recoverable (lever
``copilot.mcp_trim``)."""

from __future__ import annotations

from fractions import Fraction

from tokenbill.core.evidence import TOOL_SEARCH_REDUCTION_BAND
from tokenbill.core.labels import Basis, Evidence
from tokenbill.detect.copilot_lanes import static_carry

from .helpers import (
    GH_AW,
    GPT55,
    OPUS48,
    PRICER,
    T0,
    attr,
    attrs,
    compaction_event,
    ctx,
    lane,
    one,
    only,
    req,
    run,
    tri,
)

# Claude Opus 4.8 on Copilot: input 5,000 nano/token, read 500, 5m write 6,250, output 25,000.
SYSTEM, TOOLS = 5_000, 30_000


def vscode_static_lane(key: str = "vs-s", **kw: object) -> object:
    """A VS Code lane: one cold request (40,000 unknown-TTL writes) then nine warm requests
    (40,000 reads each); a COMPACTION event reports 5,000 system + 30,000 tool-definition
    tokens."""
    requests = [req(key, 0, 0, w=40_000, u=2_000, o=500, model=OPUS48, **kw)]
    requests += [req(key, i, 60 * i, r=40_000, u=2_000, o=500, model=OPUS48, **kw)
                 for i in range(1, 10)]
    return lane(key, requests, events=[compaction_event(key, 500, "threshold", system=SYSTEM,
                                                        tools=TOOLS)])


def _hand() -> tuple[int, int]:
    """(static carry, tool carry) of :func:`vscode_static_lane` by hand: the cold request writes
    the prefix at 6,250 nano/token, the nine warm requests read it at 500."""
    static = (SYSTEM + TOOLS) * 6_250 + 9 * (SYSTEM + TOOLS) * 500     # 376,250,000
    tools = TOOLS * 6_250 + 9 * TOOLS * 500                             # 322,500,000
    return static, tools


def test_thirty_k_tool_definitions_range_contains_the_hand_value() -> None:
    f = one(run([vscode_static_lane()], ctx(min_usd="0.01")), "static-overhead")
    static, tools = _hand()
    assert (static, tools) == (376_250_000, 322_500_000)
    assert f.cost_observed.nano == static and f.cost_observed.evidence is Evidence.ESTIMATED
    assert f.cost_observed.basis is Basis.LIST_EQUIVALENT
    rec = f.recoverable
    assert rec is not None and rec.upper_bound and rec.evidence is Evidence.ESTIMATED
    assert tri(rec) == (161_250_000, 225_750_000, 274_125_000)
    low, high = (Fraction(v) for v in TOOL_SEARCH_REDUCTION_BAND.value)
    hand = tools * Fraction(7, 10)
    assert rec.low_nano <= hand <= rec.high_nano
    assert rec.low_nano == tools * low and rec.high_nano == tools * high
    assert f.lever_ids == ("copilot.mcp_trim",) and f.lever_class == "cache_transform"
    assert f.category == "lever" and not f.needs_eval and f.confidence == "medium"
    assert attrs(f, "static:prefix")["static_tokens_max"] == SYSTEM + TOOLS
    assert attrs(f, "static:tool_definitions")["carry_nano"] == tools
    assert attrs(f, "reach:copilot.mcp_trim")["reached_lanes"] == 1
    assert f.fix is not None and f.fix.target == "github-copilot"
    assert (f.n_events, f.first_seen_ms) == (10, T0)


def test_static_carry_prefix_order_read_write_uncached() -> None:
    r = req("L", 0, 0, r=20_000, w=20_000, u=2_000, o=10, model=OPUS48)
    inf = r.serving_inference
    priced = PRICER.price_inference(inf, ts_ms=T0)
    # 20,000 read at 500 + 15,000 written at 6,250
    assert static_carry(priced, inf, 35_000) == 20_000 * 500 + 15_000 * 6_250
    # beyond reads and writes the prefix is uncached input (5,000/token)
    assert static_carry(priced, inf, 41_000) == 20_000 * 500 + 20_000 * 6_250 + 1_000 * 5_000
    # never more than the billed input
    assert static_carry(priced, inf, 10**6) == static_carry(priced, inf, 42_000)
    assert static_carry(priced, inf, 0) == 0


def test_static_carry_uncached_only_model() -> None:
    r = req("L", 0, 0, u=50_000, o=10, model=GPT55)
    inf = r.serving_inference
    assert static_carry(PRICER.price_inference(inf, ts_ms=T0), inf, 35_000) == 35_000 * 5_000


def test_latest_static_report_applies_after_it_and_first_before() -> None:
    key = "vs-2"
    requests = [req(key, 0, 0, r=40_000, u=10, o=10, model=OPUS48),
                req(key, 1, 1_000, r=40_000, u=10, o=10, model=OPUS48)]
    ln = lane(key, requests, events=[compaction_event(key, 500, "threshold", system=1_000,
                                                      tools=10_000),
                                     compaction_event(key, 800, "manual", tools=20_000)])
    f = one(run([ln], ctx(min_usd="0")), "static-overhead")
    # request 0 (before both) uses the first report: 11,000; request 1 the latest: 20,000
    assert f.cost_observed.nano == (11_000 + 20_000) * 500
    assert attrs(f, "static:tool_definitions")["carry_nano"] == (10_000 + 20_000) * 500


def test_floor_only_lane_has_no_recoverable() -> None:
    key = "vs-f"
    requests = [req(key, i, 60 * i, r=40_000, u=2_000, o=500, model=OPUS48) for i in range(4)]
    ln = lane(key, requests, scope="ws:vs")
    f = one(run([ln], ctx(min_usd="0.01", floor={("ws:vs", OPUS48): 20_000})),
            "static-overhead")
    assert f.cost_observed.nano == 4 * 20_000 * 500 and f.recoverable is None
    assert f.confidence == "low" and "tool-definition share is unknown" in f.summary
    assert attrs(f, "static:prefix")["lanes_from_floor"] == 1


def test_lane_without_static_input_is_skipped() -> None:
    key = "vs-n"
    ln = lane(key, [req(key, 0, 0, r=40_000, u=2_000, o=500, model=OPUS48)], scope="ws:vs")
    assert only(run([ln], ctx(min_usd="0")), "static-overhead") == []
    # a floor for another scope or model does not apply
    c = ctx(min_usd="0", floor={("ws:other", OPUS48): 20_000, ("ws:vs", GPT55): 9})
    assert only(run([ln], c), "static-overhead") == []


def test_unreached_lane_counts_in_recoverable_but_not_in_reach() -> None:
    f = one(run([vscode_static_lane("aw-1", a=attr(product=GH_AW, workload="ci",
                                                   billing_path="copilot_direct"))],
                ctx(min_usd="0.01")), "static-overhead")
    assert f.recoverable is not None
    reach = attrs(f, "reach:copilot.mcp_trim")
    assert (reach["reached_lanes"], reach["reached_carry_nano"]) == (0, 0)


def test_unpriced_requests_are_left_out_and_disclosed() -> None:
    key = "vs-u"
    requests = [req(key, 0, 0, r=40_000, u=10, o=10, model=OPUS48),
                req(key, 1, 60, r=40_000, u=10, o=10, model="claude-unknown-9")]
    ln = lane(key, requests, events=[compaction_event(key, 30, "threshold", system=SYSTEM,
                                                      tools=TOOLS)])
    f = one(run([ln], ctx(min_usd="0")), "static-overhead")
    assert f.cost_observed.nano == (SYSTEM + TOOLS) * 500 and f.n_events == 1
    assert "1 requests had no priced rate" in f.summary


def test_compaction_events_without_static_attrs_do_not_qualify() -> None:
    key = "vs-e"
    ln = lane(key, [req(key, 0, 0, r=40_000, u=10, o=10, model=OPUS48)],
              events=[compaction_event(key, 30, "threshold", system=0)])
    assert only(run([ln], ctx(min_usd="0")), "static-overhead") == []
