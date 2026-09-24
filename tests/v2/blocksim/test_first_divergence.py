"""``first_divergence(prev, cur)`` (SPEC §9.7 #5): every cause, by tier."""

from __future__ import annotations

import dataclasses

import pytest

from tests.v2.blocksim.helpers import blk, req, system, tool, usage
from tokenbill.core.builders import make_fingerprint
from tokenbill.sim.block_replay import first_divergence

SYS = system("fsys", 1000)


def pair(prev_blocks, cur_blocks, **kw):
    a = req("F", 0, 0, prev_blocks, usage(w5=1))
    b = req("F", 1, 30, cur_blocks, usage(w5=1), **kw)
    return a, b


def test_extension_and_prefix_are_not_divergences() -> None:
    a, b = pair([SYS, blk("a")], [SYS, blk("a"), blk("b")])
    assert first_divergence(a, b) is None
    assert first_divergence(b, a) is None          # a strict prefix: nothing diverges


def test_fingerprint_keys_must_match() -> None:
    a, b = pair([SYS, blk("a")], [SYS, blk("a")])
    b = dataclasses.replace(b, fingerprint=make_fingerprint(b.fingerprint.blocks, "k_other"))
    assert first_divergence(a, b) == ("tools", 0, "key")


@pytest.mark.parametrize("prev_tools,cur_tools,cause,index", [
    (["t1", "t2"], ["t2", "t1"], "tool-order", 0),
    (["t1"], ["t1", "t2"], "tool-subset", 1),
    (["t1", "t2"], ["t1"], "tool-subset", 1),
    (["t1", "t2"], ["t1", "t3"], "tool-definition", 1),
])
def test_tools_tier_causes(prev_tools, cur_tools, cause, index) -> None:
    a, b = pair([*(tool(t) for t in prev_tools), SYS], [*(tool(t) for t in cur_tools), SYS])
    assert first_divergence(a, b) == ("tools", index, cause)


def test_tools_serialization_and_volatile() -> None:
    a, b = pair([tool("ka", srt="k"), SYS], [tool("kb", srt="k"), SYS])
    assert first_divergence(a, b) == ("tools", 0, "serialization")
    a, b = pair([tool("va", norm="v", volatile=("uuid",)), SYS],
                [tool("vb", norm="v", volatile=("uuid",)), SYS])
    assert first_divergence(a, b) == ("tools", 0, "volatile")


def test_system_tier_causes() -> None:
    v1 = system("s-v1", norm="sn", volatile=("iso_datetime",))
    v2 = system("s-v2", norm="sn", volatile=("iso_datetime",))
    assert first_divergence(*pair([v1, blk("a")], [v2, blk("a")])) == ("system", 0, "volatile")
    e1, e2 = system("s-e1"), system("s-e2")
    assert first_divergence(*pair([e1, blk("a")], [e2, blk("a")])) == ("system", 0,
                                                                       "system-edit")
    k1, k2 = system("s-k1", srt="sk"), system("s-k2", srt="sk")
    assert first_divergence(*pair([k1], [k2])) == ("system", 0, "serialization")
    # a volatile class on only one of two changed blocks is not purely volatile
    m1 = [system("m-1", norm="mn1", volatile=("uuid",)), system("m-2", norm="mn2")]
    m2 = [system("m-1x", norm="mn1", volatile=("uuid",)), system("m-2x", norm="mn2")]
    assert first_divergence(*pair(m1, m2)) == ("system", 0, "system-edit")


def test_system_block_added_moves_the_tier_boundary() -> None:
    a, b = pair([tool("t1"), SYS, blk("a")], [tool("t1"), SYS, system("extra"), blk("a")])
    assert first_divergence(a, b) == ("system", 2, "system-edit")


@pytest.mark.parametrize("new,kw,cause", [
    (blk("x", kind="compaction"), {}, "compaction"),
    (blk("x"), {"serialization": True}, "serialization"),
    (blk("x"), {"volatile": True}, "volatile"),
    (blk("x"), {}, "history-rewrite"),
])
def test_messages_tier_causes(new, kw, cause) -> None:
    old = blk("old")
    if kw.get("serialization"):
        old, new = blk("so", srt="same"), blk("sn", srt="same")
    if kw.get("volatile"):
        old = blk("vo", norm="vn", volatile=("unix_ts",))
        new = blk("vx", norm="vn", volatile=("unix_ts",))
    a, b = pair([SYS, old, blk("tail")], [SYS, new, blk("tail"), blk("more")])
    assert first_divergence(a, b) == ("messages", 1, cause)


@pytest.mark.parametrize("field,value,cause", [
    ("applied_edits", (("clear_tool_uses", 400),), "context-edit"),
    ("thinking_dropped", 2, "thinking-dropped"),
])
def test_messages_rebuild_causes_from_the_attempt(field, value, cause) -> None:
    a, b = pair([SYS, blk("o1"), blk("o2")], [SYS, blk("n1"), blk("o2")])
    att = dataclasses.replace(b.attempts[0], **{field: value})
    b = dataclasses.replace(b, attempts=(att,))
    assert first_divergence(a, b) == ("messages", 1, cause)


def test_a_salt_change_wins_over_a_later_block_change() -> None:
    a = req("F", 0, 0, [SYS, blk("a"), blk("b")], usage(w5=1), params={"tool_choice": "auto"})
    b = req("F", 1, 30, [SYS, blk("a"), blk("c")], usage(w5=1), params={"tool_choice": "any"})
    assert first_divergence(a, b) == ("messages", 1, "param")


def test_a_block_change_before_the_salted_tier_wins() -> None:
    a = req("F", 0, 0, [system("p1"), blk("a")], usage(w5=1), params={"tool_choice": "auto"})
    b = req("F", 1, 30, [system("p2"), blk("a")], usage(w5=1), params={"tool_choice": "any"})
    assert first_divergence(a, b) == ("system", 0, "system-edit")
