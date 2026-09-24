"""``BlockBreakers`` (SPEC §10.4): one hand-computed fixture per breaker kind, the floor with the
v0.1 wording, the trigger rules, conformance and privacy.

Opus 5.5 unit prices (nano per token): uncached U = 4,000, read R = 200, 5m write W5 = 5,000.
Block sizes are exact, billed totals equal the block sizes, gaps are 30 s unless stated, and
``min_usd`` is lowered because the fixtures are small (a few cents)."""

from __future__ import annotations

import dataclasses
import json

import pytest

from tests.v2.blocksim.fp import payload_request
from tests.v2.blocksim.helpers import (
    CAPS,
    W5,
    R,
    U,
    blk,
    context,
    conversation,
    kinds,
    lane,
    req,
    size,
    system,
    tool,
    usage,
)
from tokenbill.core.builders import CANARY, assert_no_canary
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import LaneEvent, LaneKind, UsageBuckets, to_json
from tokenbill.core.registry import all_detectors, run_detectors
from tokenbill.core.testing import assert_detector_conforms
from tokenbill.detect.block import KINDS, BlockBreakers
from tokenbill.sim.block_replay import NO_RECOVERY_NOTE

LOW = {"min_usd": "0.001"}


def detect(lanes, **thresholds):
    return BlockBreakers().detect(list(lanes), context(**(thresholds or LOW)))


def only(findings, kind):
    got = [f for f in findings if f.kind == kind]
    assert len(got) == 1, kinds(findings)
    return got[0]


def growing(key, first, rest, *, billed=None, gap=30, **kw):
    """Requests whose blocks are ``first[i]`` (explicit lists); billed full 5m writes."""
    out = []
    for i, blocks in enumerate([first, *rest]):
        bu = billed[i] if billed else usage(w5=size(blocks))
        out.append(req(key, i, gap * i, blocks, bu, **kw))
    return out


MSG = [blk(f"m{i}") for i in range(4)]


# ---------------------------------------------------------------------------- the eleven kinds


def volatile_lane(*, volatile=True, key="V", **kw):
    reqs = []
    for i in range(3):
        sysb = system(f"{key}-sys{i}", 2000, norm="vsys" if volatile else f"{key}-n{i}",
                      volatile=("iso_datetime",) if volatile else ())
        blocks = [sysb, *MSG[: i + 1]]
        reqs.append(req(key, i, 30 * i, blocks, usage(w5=size(blocks)), **kw))
    return lane(reqs)


def test_volatile_system() -> None:
    f = only(detect([volatile_lane()]), "volatile-system")
    # observed model 60,000,000 − repaired 26,400,000 (h_norm makes the system tier stable)
    assert f.recoverable.nano == 33_600_000
    assert f.recoverable.evidence is Evidence.ESTIMATED
    assert f.cost_observed.nano == (4000 + 5000) * W5 and f.cost_observed.evidence is \
        Evidence.EXACT
    assert (f.n_events, f.n_lanes, f.category) == (2, 1, "breaker")
    assert f.evidence[0].attrs[0] == ("block_index", 0)
    assert "mid-conversation role:system" in f.fix.text


def test_system_edit_has_no_mechanical_repair() -> None:
    f = only(detect([volatile_lane(volatile=False)]), "system-edit")
    assert f.recoverable is None
    assert f.cost_observed.nano == 45_000_000
    assert "No mechanical repair" in f.summary


def test_serialization_churn() -> None:
    a = [tool("sA1", srt="k1"), tool("sA2", srt="k2")]
    b = [tool("sB1", srt="k1"), tool("sB2", srt="k2")]
    sysb = system("ssys", 1000)
    blocks = [[*a, sysb, *MSG[:1]], [*b, sysb, *MSG[:2]], [*a, sysb, *MSG[:3]]]
    f = only(detect([lane(growing("S", blocks[0], blocks[1:]))]), "serialization-churn")
    # observed: 3000·W5 + 4000·W5 + (3000·R + 2000·W5) = 45,600,000; sorted: 26,400,000
    assert f.recoverable.nano == 19_200_000
    assert f.cost_observed.nano == (4000 + 5000) * W5
    assert f.n_events == 2


def test_tool_churn_order() -> None:
    t1, t2, sysb = tool("o1"), tool("o2"), system("osys", 1000)
    blocks = [[t1, t2, sysb, *MSG[:1]], [t2, t1, sysb, *MSG[:2]], [t2, t1, sysb, *MSG[:3]]]
    f = only(detect([lane(growing("O", blocks[0], blocks[1:]))]), "tool-churn")
    # observed 40,800,000 − first-seen order 26,400,000
    assert f.recoverable.nano == 14_400_000
    assert f.cost_observed.nano == 4000 * W5 and f.n_events == 1
    assert "order" in f.title and "fixed order" in f.fix.text


def test_tool_churn_subset() -> None:
    t1, t2, sysb = tool("p1"), tool("p2"), system("psys", 1000)
    blocks = [[t1, sysb, *MSG[:1]], [t1, t2, sysb, *MSG[:2]], [t1, t2, sysb, *MSG[:3]]]
    f = only(detect([lane(growing("Q", blocks[0], blocks[1:]))]), "tool-churn")
    # observed 2500·W5 + 4000·W5 + (4000·R + 1000·W5) = 38,300,000;
    # constant superset: 3000·W5 + (3000·R + 1000·W5) + (4000·R + 1000·W5) = 26,400,000
    assert f.recoverable.nano == 11_900_000
    assert "subset" in f.title and f.fix.gates


def test_tool_churn_definition_only_has_no_repair() -> None:
    sysb = system("dsys", 1000)
    blocks = [[tool("d1"), tool("d2"), sysb, *MSG[:1]],
              [tool("d1"), tool("d2x"), sysb, *MSG[:2]]]
    f = only(detect([lane(growing("D", blocks[0], blocks[1:]))]), "tool-churn")
    assert f.recoverable is None and f.cost_observed.nano == 4000 * W5


def test_history_rewrite() -> None:
    sysb = system("hsys", 1000)
    blocks = [[sysb, blk("h0"), blk("h1")], [sysb, blk("h0x"), blk("h1"), blk("h2")]]
    f = only(detect([lane(growing("H", blocks[0], blocks[1:]))]), "history-rewrite")
    assert f.recoverable is None and f.cost_observed.nano == 3000 * W5 + 1000 * W5
    assert f.evidence[0].attrs == (("block_index", 1), ("cause", "history-rewrite"),
                                   ("tier", "messages"), ("tokens", 4000),
                                   ("ts_ms", f.first_seen_ms))


def param_lane(product="agent_sdk", version=None):
    sysb = system("rsys", 2000)
    reqs = []
    for i, effort in enumerate(("high", "low", "low")):
        blocks = [sysb, *MSG[: i + 1]]
        reqs.append(req(f"PC-{product}", i, 30 * i, blocks, usage(w5=size(blocks)),
                        params={"effort": effort},
                        attribution={"agent_product": product, "client_version": version}))
    return lane(reqs)


def test_param_churn() -> None:
    f = only(detect([param_lane()]), "param-churn")
    # observed 40,800,000 − pinned 26,400,000
    assert f.recoverable.nano == 14_400_000
    assert f.cost_observed.nano == 4000 * W5
    assert "effort" in f.title


def test_effort_change_under_claude_code_opus_5_5_is_not_a_breaker() -> None:
    assert detect([param_lane("claude_code", "2.1.270")]) == []


def marker_less(gap=30):
    return lane(conversation("MB", [system("msys", 2000)], [[m] for m in MSG[:3]],
                             billed="none", bps=None, gap_s=gap))


def test_missing_breakpoint() -> None:
    f = only(detect([marker_less()]), "missing-breakpoint")
    # observed: all uncached 48,000,000; one end breakpoint: 26,400,000
    assert f.recoverable.nano == 21_600_000
    assert f.cost_observed.nano == (4000 + 5000) * U
    assert f.fix.text.startswith("add a cache_control breakpoint")
    assert f.lever_ids == ("blocks.breakpoints",)


def test_negative_recoverable_is_floored_with_the_v01_wording() -> None:
    # 10-minute gaps: every added write expires unread, so the repair costs more than no cache
    f = only(detect([marker_less(gap=600)], min_usd="0"), "missing-breakpoint")
    assert f.recoverable.nano == 0
    assert f.recoverable.note == NO_RECOVERY_NOTE
    assert NO_RECOVERY_NOTE in f.summary


def test_an_assumed_end_breakpoint_suppresses_missing_breakpoint() -> None:
    reqs = conversation("AS", [system("asys", 2000)], [[m] for m in MSG[:3]], billed="none",
                        bps="end", assumed=True)
    assert "missing-breakpoint" not in kinds(detect([lane(reqs)], min_usd="0"))


def overflow_lane():
    turns = [[blk(f"x{t}-{j}", 40) for j in range(25)] for t in range(3)]
    return lane(conversation("LO", [system("losys", 1000)], turns, billed="cold"))


def test_lookback_overflow() -> None:
    f = only(detect([overflow_lane()]), "lookback-overflow")
    # observed: 2000·W5 + 3000·W5 + 4000·W5 = 45,000,000; every_15: 10,000,000 + 5,400,000
    # + 5,600,000 = 21,000,000
    assert f.recoverable.nano == 24_000_000
    assert f.cost_observed.nano == (3000 + 4000) * W5
    assert "every ~15" in f.fix.text and f.lever_ids == ("blocks.breakpoints",)
    assert kinds(detect([overflow_lane()])) == ["lookback-overflow"]


def placement_lanes():
    doc = blk("doc", 3000)
    lanes = []
    for k in range(3):
        q, fu = blk(f"q{k}"), blk(f"f{k}")
        lanes.append(lane([
            req(f"PL{k}", 0, 60 * k, [doc, q], usage(w5=4000)),
            req(f"PL{k}", 1, 60 * k + 30, [doc, q, fu], usage(r=4000, w5=1000)),
        ]))
    return lanes


def test_breakpoint_placement() -> None:
    findings = detect(placement_lanes())
    assert kinds(findings) == ["breakpoint-placement"]
    f = findings[0]
    # observed 77,400,000 − static_plus_end 48,600,000 (lanes 1-2 read the shared document)
    assert f.recoverable.nano == 28_800_000
    assert f.cost_observed.nano == 3 * 4000 * W5 + 3 * (4000 * R + 1000 * W5)
    assert "static_plus_end" in f.title and f.lever_ids == ("blocks.breakpoints",)
    assert dict((e.ref, dict(e.attrs)["nano"]) for e in f.evidence) == {
        "placement:end": 0, "placement:static_plus_end": 28_800_000,
        "placement:every_15": 28_800_000}


def test_write_never_read() -> None:
    lanes = [lane([req(f"WN{i}", 0, 60 * i, [system(f"w{i}", 2000)], usage(w5=2000))])
             for i in range(2)]
    f = only(detect(lanes), "write-never-read")
    assert f.recoverable.nano == 2 * 2000 * (W5 - U)
    assert f.cost_observed.nano == 2 * 2000 * W5


def test_the_tail_write_of_a_conversation_is_not_write_never_read() -> None:
    reqs = conversation("TW", [system("twsys", 2000)], [[m] for m in MSG[:3]])
    assert detect([lane(reqs)], min_usd="0") == []


def test_fanout() -> None:
    lanes = [lane([req(f"FO{i}", 0, 0, [system("fsys", 3000)], usage(w5=3000), ttft_ms=900)])
             for i in range(3)]
    findings = detect(lanes)
    assert kinds(findings) == ["fanout"]
    f = findings[0]
    assert f.recoverable.nano == 2 * 3000 * (W5 - R) and f.recoverable.upper_bound
    assert f.cost_observed.nano == 2 * 3000 * W5 and f.n_events == 2
    assert f.lever_ids == ("fanout.stagger",)


def test_fanout_needs_billed_writes() -> None:
    lanes = [lane([req(f"FU{i}", 0, 0, [system("fus", 3000)], usage(u=3000))]) for i in range(3)]
    assert "fanout" not in kinds(detect(lanes, min_usd="0"))


def test_every_kind_has_a_fixture_here() -> None:
    covered = {"volatile-system", "system-edit", "serialization-churn", "tool-churn",
               "history-rewrite", "param-churn", "missing-breakpoint", "lookback-overflow",
               "breakpoint-placement", "write-never-read", "fanout"}
    assert covered == set(KINDS) and len(KINDS) == 11


# ---------------------------------------------------------------------------- trigger rules


def test_min_usd_default_suppresses_small_findings() -> None:
    assert detect([volatile_lane()], min_usd="1.00") == []


def test_no_breaker_outside_the_cached_prefix() -> None:
    sysb = system("csys", 2000)
    first = [sysb, blk("c0"), blk("c1")]
    second = [sysb, blk("c0x"), blk("c1"), blk("c2")]
    reqs = growing("OC", first, [second], bps=[(0, "5m")])       # only the system is marked
    assert "history-rewrite" not in kinds(detect([lane(reqs)], min_usd="0"))


def test_model_switch_is_not_a_block_breaker() -> None:
    sysb = system("mss", 2000)
    r0 = req("MS", 0, 0, [sysb, MSG[0]], usage(w5=3000))
    r1 = req("MS", 1, 30, [sysb, *MSG[:2]], usage(w5=4000), model="claude-opus-5")
    assert detect([lane([r0, r1])], min_usd="0") == []


@pytest.mark.parametrize("variant", ["event", "edits", "compaction_block", "thinking"])
def test_expected_rebuilds_are_not_history_rewrites(variant) -> None:
    sysb = system("ers", 1000)
    first = [sysb, blk("e0"), blk("e1")]
    second = [sysb, blk("e0x", kind="compaction" if variant == "compaction_block" else "text"),
              blk("e1"), blk("e2")]
    r0 = req("ER", 0, 0, first, usage(w5=3000))
    r1 = req("ER", 1, 30, second, usage(w5=4000))
    events = ()
    if variant == "event":
        events = (LaneEvent(lane_key="ER", ts_ms=r0.ts_start_ms + 10_000, kind="compaction",
                            attrs=(("pre_tokens", 3000), ("post_tokens", 1000))),)
    if variant in ("edits", "thinking"):
        att = r1.attempts[0]
        att = dataclasses.replace(att, applied_edits=(("clear_tool_uses", 500),)) \
            if variant == "edits" else dataclasses.replace(att, thinking_dropped=1)
        r1 = dataclasses.replace(r1, attempts=(att,))
    assert "history-rewrite" not in kinds(detect([lane([r0, r1], events=events)], min_usd="0"))


def test_billed_misses_raise_confidence() -> None:
    reqs = conversation("BC", [system("bcs", 2000)], [[m] for m in MSG[:2]], billed="working")
    sysb = [system("bcs-v0", 2000, norm="bcv", volatile=("uuid",)),
            system("bcs-v1", 2000, norm="bcv", volatile=("uuid",))]
    r0 = req("BV", 0, 0, [sysb[0], MSG[0]], usage(w5=3000))
    r1 = req("BV", 1, 30, [sysb[1], *MSG[:2]], usage(w5=4000))
    f = only(detect([lane([r0, r1])]), "volatile-system")
    assert f.confidence == "high" and f.validated_against == "billed misses at 1/1 events"
    assert detect([lane(reqs)], min_usd="0") == []


def test_fix_gates_follow_model_support() -> None:
    reqs = []
    for i in range(3):
        sysb = system(f"g-sys{i}", 2000, norm="gsys", volatile=("iso_date",))
        blocks = [sysb, *MSG[: i + 1]]
        reqs.append(req("G", i, 30 * i, blocks, usage(w5=size(blocks)), model="claude-sonnet-5"))
    f = only(detect([lane(reqs)]), "volatile-system")
    assert "mid-conversation" not in f.fix.text and f.fix.gates == ()


def test_allowance_cohort_findings_are_list_equivalent() -> None:
    f = only(detect([volatile_lane(key="AV", billing_path="subscription")]), "volatile-system")
    assert f.title.startswith("Allowance headroom:")
    assert "list-equivalent, not invoice dollars" in f.summary
    assert f.cost_observed.basis is Basis.LIST_EQUIVALENT
    assert f.recoverable.basis is Basis.LIST_EQUIVALENT
    assert ("billing_class", "allowance") in f.scope.dims


def test_lanes_without_fingerprints_are_ignored() -> None:
    from tokenbill.core.builders import lane_from_table
    plain = lane_from_table([(0, 0, 3000, 0, 0, 100), (30, 0, 4000, 0, 0, 100)])
    assert detect([plain], min_usd="0") == []


# ---------------------------------------------------------------------------- conformance


def mixed_lanes():
    def team(ln, name, kind=LaneKind.API_RUN):
        reqs = tuple(dataclasses.replace(r, attribution=dataclasses.replace(
            r.attribution, team=name, principal=f"r_user-{name}")) for r in ln.requests)
        return dataclasses.replace(ln, requests=reqs, kind=kind)

    return [
        team(volatile_lane(key="C1"), "payments"),
        team(volatile_lane(key="C2"), "payments", LaneKind.SUBAGENT),
        team(param_lane(), "search"),
        team(overflow_lane(), "search", LaneKind.MAIN),
        team(marker_less(), "infra"),
        *[team(ln, "docs") for ln in placement_lanes()],
        volatile_lane(key="C3", billing_path="subscription"),
        team(volatile_lane(key="C4", billing_path="subscription"), "payments"),
    ]


def test_conforms_to_the_detector_contract_including_shard_invariance() -> None:
    lanes = mixed_lanes()
    findings = assert_detector_conforms(BlockBreakers(), lanes, context(**LOW))
    assert {"volatile-system", "param-churn", "lookback-overflow", "missing-breakpoint",
            "breakpoint-placement"} <= set(kinds(findings))
    ids = [f.finding_id for f in findings]
    assert len(ids) == len(set(ids))


def test_detector_surface_and_registry() -> None:
    det = BlockBreakers()
    assert det.id == "block.breakers" and det.requires == frozenset({"blocks"})
    assert det.kinds == KINDS
    assert any(isinstance(d, BlockBreakers) for d in all_detectors())
    got = run_detectors([volatile_lane()], context(**LOW), only=["block.breakers"])
    assert kinds(got) == ["volatile-system"]
    ctx = dataclasses.replace(context(**LOW), capabilities=CAPS - {"blocks"})
    missing = run_detectors([volatile_lane()], ctx, only=["block.breakers"])
    assert [f.kind for f in missing] == ["missing-capabilities"]


def test_findings_are_deterministic() -> None:
    a = detect(mixed_lanes())
    b = detect(list(reversed(mixed_lanes())))
    assert json.dumps([to_json(f) for f in a], sort_keys=True) == \
        json.dumps([to_json(f) for f in b], sort_keys=True)


def test_no_content_reaches_any_finding() -> None:
    system_text = f"You are the agent {CANARY}. Session 2026-09-23 10:00:{{sec:02d}}."
    reqs = []
    for i in range(3):
        msgs = [{"role": "user", "content": f"question {CANARY} " + "x " * 800}]
        msgs += [{"role": "user", "content": f"more {CANARY} {j} " + "y " * 400}
                 for j in range(i)]
        text = system_text.replace("{sec:02d}", f"{i:02d}")
        reqs.append(payload_request("CAN", i, 1_790_121_600_000 + 30_000 * i, tools=[],
                                    system=text * 20, messages=msgs,
                                    billed=UsageBuckets(uncached_input=6000 + 700 * i, output=10),
                                    breakpoint_count=1))
    findings = detect([lane(reqs)], min_usd="0")
    assert "volatile-system" in kinds(findings)
    blob = json.dumps([to_json(f) for f in findings]) + repr(findings)
    assert_no_canary(blob)
