"""Block fingerprinting (SPEC §5.8): the TRACE acceptance items and the helpers around them."""

from __future__ import annotations

import base64
import copy
import struct
import zlib
from typing import Any

import pytest

from tests.v2.trace.helpers import FP_KEY, FP_KID, SYSTEM, conversation, tools
from tokenbill.adapters import fingerprint as F
from tokenbill.core.builders import CANARY, make_block
from tokenbill.core.errors import UsageError
from tokenbill.core.ids import stable_id
from tokenbill.core.records import BlockRef, Breakpoint, ContentTier
from tokenbill.demo_traces import scenario

FP = ContentTier.FINGERPRINT


def _fp(messages: list[dict[str, Any]], **kw: Any) -> Any:
    return F.fingerprint_request(tools=kw.pop("tools", tools()), system=kw.pop("system", SYSTEM),
                                 messages=messages, key=kw.pop("key", FP_KEY),
                                 tier=kw.pop("tier", FP), **kw)


def _marked(msgs: list[dict[str, Any]], index: int, ttl: str | None = None) -> list[dict]:
    out = copy.deepcopy(msgs)
    content = out[index]["content"]
    if isinstance(content, str):
        out[index]["content"] = [{"type": "text", "text": content}]
        content = out[index]["content"]
    cc: dict[str, Any] = {"type": "ephemeral"}
    if ttl:
        cc["ttl"] = ttl
    content[-1]["cache_control"] = cc
    return out


# ---------------------------------------------------------------------------------------------
# acceptance
# ---------------------------------------------------------------------------------------------


def test_moving_cache_control_leaves_every_hash_unchanged() -> None:
    msgs = conversation(3)
    a, bps_a, _ = _fp(_marked(msgs, 3))
    b, bps_b, _ = _fp(_marked(msgs, 4))
    assert [(x.h, x.h_sorted, x.h_norm) for x in a.blocks] == \
        [(x.h, x.h_sorted, x.h_norm) for x in b.blocks]
    assert a == b                                   # every BlockRef field, too
    assert bps_a != bps_b and len(bps_a) == len(bps_b) == 1
    plain, none_bps, _ = _fp(msgs)
    assert plain == a and none_bps == ()


def test_key_order_changes_h_but_not_h_sorted() -> None:
    t1 = tools()
    t2 = [dict(reversed(list(t.items()))) for t in t1]
    a, _, _ = _fp(conversation(1), tools=t1)
    b, _, _ = _fp(conversation(1), tools=t2)
    for x, y in zip(a.blocks[:2], b.blocks[:2], strict=True):
        assert x.tier == "tools" and x.kind == "tool_def"
        assert x.h != y.h and x.h_sorted == y.h_sorted
    assert a.blocks[2:] == b.blocks[2:]


def test_demo_timestamp_system_blocks_differ_in_h_match_in_h_norm() -> None:
    calls = scenario("timestamp")
    fps = [_fp(list(c.messages), tools=list(c.tools), system=c.system)[0] for c in calls[:4]]
    systems = [fp.blocks[fp.tier_end[0]] for fp in fps]
    assert all(s.kind == "system_text" and s.tier == "system" for s in systems)
    assert len({s.h for s in systems}) == len(systems)
    assert len({s.h_norm for s in systems}) == 1
    assert all(s.volatile_classes == ("iso_datetime",) for s in systems)
    # the byte-stable well-behaved system prompt has no volatile class and h_norm == h
    wb = scenario("well-behaved")[0]
    fp = _fp(list(wb.messages), tools=list(wb.tools), system=wb.system)[0]
    sys_block = fp.blocks[fp.tier_end[0]]
    assert sys_block.volatile_classes == () and sys_block.h_norm == sys_block.h


def test_25_consecutive_tool_results_share_one_lookback_position() -> None:
    results = [{"type": "tool_result", "tool_use_id": f"t{i}", "content": f"r{i}"}
               for i in range(25)]
    uses = [{"type": "tool_use", "id": f"t{i}", "name": "read_file", "input": {}}
            for i in range(25)]
    msgs = [{"role": "user", "content": "go"}, {"role": "assistant", "content": uses},
            {"role": "user", "content": results}, {"role": "assistant", "content": "done"}]
    fp, _, _ = _fp(msgs, tools=[], system=None)
    pos = [b.lookback_pos for b in fp.blocks]
    assert pos[0] == 0
    assert set(pos[1:26]) == {1}
    assert set(pos[26:51]) == {2}
    assert pos[51] == 3
    assert F.lookback_positions(fp.blocks) == pos
    assert F.lookback_positions(["text", "tool_result", "tool_result", "text"]) == [0, 1, 1, 2]


def _png(w: int, h: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr
            + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr)))


def _image_block(data: bytes, media: str = "image/png") -> dict[str, Any]:
    return {"type": "image", "source": {"type": "base64", "media_type": media,
                                        "data": base64.b64encode(data).decode()}}


@pytest.mark.parametrize(("w", "h", "family", "expected"), [
    (280, 140, "claude-4.7+", 50),            # ceil(280/28) · ceil(140/28) = 10 · 5
    (281, 141, "claude-4.7+", 66),            # 11 · 6
    (1568, 1568, "claude-legacy", 1568),      # 56 · 56 = 3,136 capped at 1,568 (standard)
    (2576, 2576, "claude-4.7+", 4784),        # 92 · 92 = 8,464 capped at 4,784 (4.7+ high-res)
    (1000, 1000, "claude-4.7+", 1296),        # 36 · 36 under the high-res cap
])
def test_image_estimate_with_caps(w: int, h: int, family: str, expected: int) -> None:
    msgs = [{"role": "user", "content": [_image_block(_png(w, h)), {"type": "text", "text": "?"}]}]
    fp, _, _ = _fp(msgs, tools=[], system=None, tokenizer_family=family)
    img = fp.blocks[0]
    assert img.kind == "image" and img.image_px == (w, h) and img.est_tokens == expected
    assert F.image_tokens(w, h, family) == expected


def test_image_without_readable_dimensions_has_no_estimate() -> None:
    url = {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}}
    junk = _image_block(b"not an image at all")
    fp, _, _ = _fp([{"role": "user", "content": [url, junk]}], tools=[], system=None)
    assert [(b.image_px, b.est_tokens) for b in fp.blocks] == [(None, None), (None, None)]


def test_image_dimensions_of_gif_webp_jpeg() -> None:
    gif = b"GIF89a" + struct.pack("<HH", 30, 40) + b"\x00" * 8
    webp_vp8x = b"RIFF" + b"\x00" * 4 + b"WEBPVP8X" + b"\x00" * 8 + (99).to_bytes(3, "little") \
        + (49).to_bytes(3, "little")
    webp_vp8 = b"RIFF" + b"\x00" * 4 + b"WEBPVP8 " + b"\x00" * 10 + struct.pack("<HH", 64, 32)
    bits = (120 - 1) | ((60 - 1) << 14)
    webp_vp8l = b"RIFF" + b"\x00" * 4 + b"WEBPVP8L" + b"\x00" * 4 + b"\x2f" \
        + bits.to_bytes(4, "little")
    jpeg = (b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4) + b"JF"
            + b"\xff\xc0" + struct.pack(">HBHH", 11, 8, 480, 640) + b"\x00" * 6)
    assert F.image_dimensions(gif) == (30, 40)
    assert F.image_dimensions(webp_vp8x) == (100, 50)
    assert F.image_dimensions(webp_vp8) == (64, 32)
    assert F.image_dimensions(webp_vp8l) == (120, 60)
    assert F.image_dimensions(jpeg) == (640, 480)
    assert F.image_dimensions(b"\xff\xd8\xff") is None
    assert F.image_dimensions(b"RIFF\x00\x00\x00\x00WEBPXXXX") is None
    assert F.image_dimensions(b"") is None


def test_defer_loading_marks_the_tool_deferred() -> None:
    t = tools()
    t[1]["defer_loading"] = True
    fp, _, _ = _fp(conversation(0), tools=t)
    assert [b.deferred for b in fp.blocks[:2]] == [False, True]
    assert not any(b.deferred for b in fp.blocks[2:])


def test_markers_and_ttls_extracted() -> None:
    t = tools()
    t[1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
    system = [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]
    msgs = _marked(conversation(2), 4, "1h")
    msgs[1]["content"][1]["cache_control"] = {"type": "ephemeral", "ttl": "weird"}
    fp, bps, _ = _fp(msgs, tools=t, system=system)
    assert fp.tier_end == (2, 3, 10)
    assert bps == (Breakpoint(1, "1h"), Breakpoint(2, "5m"), Breakpoint(5, "5m"),
                   Breakpoint(9, "1h"))
    assert not any(bp.assumed for bp in bps)
    # the content-free walk finds the same markers without hashing
    walk, n = F.request_breakpoints(tools=t, system=system, messages=msgs)
    assert walk == bps and n == len(fp.blocks)


def test_content_map_only_in_full() -> None:
    msgs = conversation(2)
    fp_a, _, content_fp = _fp(msgs, tier=ContentTier.FINGERPRINT)
    fp_b, _, content_full = _fp(msgs, tier=ContentTier.FULL)
    assert content_fp == {}
    assert fp_a == fp_b
    assert set(content_full) == {b.h for b in fp_b.blocks}
    assert any(CANARY in text for text in content_full.values())
    assert CANARY not in repr(fp_a)


def test_tier_none_is_refused() -> None:
    with pytest.raises(UsageError):
        _fp(conversation(1), tier=ContentTier.NONE)
    with pytest.raises(UsageError):
        _fp(conversation(1), tier="bogus")
    with pytest.raises(UsageError):
        _fp(conversation(1), key=b"")


# ---------------------------------------------------------------------------------------------
# details
# ---------------------------------------------------------------------------------------------


def test_block_layout_kinds_roles_and_tier_end() -> None:
    msgs = [{"role": "user", "content": "plain string"},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "hm"},
                                              {"type": "server_tool_use", "id": "x"},
                                              "bare string element"]},
            "not a message"]
    fp, _, _ = _fp(msgs, system=[{"type": "text", "text": "a"}, "b"])
    kinds = [(b.tier, b.kind, b.role) for b in fp.blocks]
    assert kinds == [("tools", "tool_def", None), ("tools", "tool_def", None),
                     ("system", "system_text", None), ("system", "system_text", None),
                     ("messages", "text", "user"), ("messages", "thinking", "assistant"),
                     ("messages", "other", "assistant"), ("messages", "text", "assistant"),
                     ("messages", "other", None)]
    assert fp.tier_end == (2, 4, 9)
    assert fp.key_id == FP_KID


def test_same_content_in_another_role_or_tier_hashes_differently() -> None:
    msgs = [{"role": "user", "content": "OK"}, {"role": "assistant", "content": "OK"}]
    fp, _, _ = _fp(msgs, tools=[], system="OK")
    assert len({b.h for b in fp.blocks}) == 3


def test_empty_system_string_is_no_block_and_est_tokens_by_kind() -> None:
    fp, _, _ = _fp([{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a",
                                                  "content": "x" * 100}]}],
                   system="", tools=tools()[:1])
    assert fp.tier_end == (1, 1, 2)
    tool_def, result = fp.blocks
    assert tool_def.est_tokens == F.est_tokens_for("tool_def", tool_def.n_bytes)
    assert result.est_tokens == -(-result.n_bytes * 10 // 25)
    assert F.est_tokens_for("text", 36) == 10
    assert F.est_tokens_for("text", 36, "claude-legacy") == 8     # 36 / 4.68
    assert F.est_tokens_for("tool_result", 25) == 10


def test_volatile_spans_classes_and_normalize() -> None:
    text = ("started 2026-07-26T14:03:05Z on 2026-07-27 at 09:15 ts 1784037780 "
            "id 123e4567-e89b-12d3-a456-426614174000 turn #7")
    spans = F.volatile_spans(text)
    assert [c for c, _a, _b in spans] == ["iso_datetime", "iso_date", "clock_time", "unix_ts",
                                          "uuid", "counter"]
    assert F.normalize_volatile(text) == ("started <iso_datetime> on <iso_date> at <clock_time> "
                                          "ts <unix_ts> id <uuid> <counter>")
    assert F.volatile_spans("") == [] and F.normalize_volatile("stable") == "stable"
    assert F.volatile_spans(None) == []  # type: ignore[arg-type]


def test_plain_converts_sdk_objects_without_repr() -> None:
    class Dumps:
        def model_dump(self, mode: str = "python") -> dict[str, Any]:
            return {"type": "text", "text": "dumped", "mode": mode}

    class OldDumps:
        def model_dump(self) -> dict[str, Any]:
            return {"type": "text", "text": "old"}

    class ToDict:
        def to_dict(self) -> dict[str, Any]:
            return {"type": "text", "text": "dict"}

    class Broken:
        def model_dump(self, mode: str = "python") -> Any:
            raise RuntimeError("no")

        def __repr__(self) -> str:
            return f"<Broken {CANARY}>"

    got = F.plain({"a": (Dumps(), OldDumps(), ToDict(), Broken(), b"\x00\x01", 1.5, None)})
    assert got == {"a": [{"type": "text", "text": "dumped", "mode": "json"},
                         {"type": "text", "text": "old"}, {"type": "text", "text": "dict"},
                         {"type": "Broken"}, {"type": "bytes", "n_bytes": 2}, 1.5, None]}
    # SDK objects inside a payload are fingerprinted through plain(), never repr()
    fp, _, content = _fp([{"role": "user", "content": [Dumps(), Broken()]}], tools=[],
                         system=None, tier=ContentTier.FULL)
    assert len(fp.blocks) == 2 and CANARY not in "".join(content.values())


def test_cache_reuses_blocks_and_is_bounded() -> None:
    cache = F.FingerprintCache()
    a, _, _ = _fp(conversation(3), cache=cache)
    misses = cache.misses
    b, _, _ = _fp(conversation(4), cache=cache)
    assert b.blocks[: len(a.blocks)] == a.blocks
    assert cache.misses - misses == 3 and cache.hits >= len(a.blocks)
    assert all(x is y for x, y in zip(a.blocks, b.blocks[: len(a.blocks)], strict=False))
    tiny = F.FingerprintCache(max_bytes=600)
    _fp(conversation(4), cache=tiny)
    assert tiny._bytes <= 600
    assert len(tiny) < 11
    huge = F.FingerprintCache(max_bytes=10)
    _fp(conversation(1), cache=huge)
    assert len(huge) == 0


def test_non_json_values_and_nan_hash_without_error() -> None:
    fp, _, _ = _fp([{"role": "user", "content": [{"type": "text", "text": "x",
                                                   "score": float("nan")}]}],
                   tools=[], system=None)
    assert len(fp.blocks) == 1 and fp.blocks[0].h_sorted


def test_replace_lookback_keeps_objects_already_in_place() -> None:
    blocks = [make_block("a", kind="tool_result"), make_block("b", kind="tool_result"),
              make_block("c", kind="text")]
    fixed = F.replace_lookback(blocks)
    assert [b.lookback_pos for b in fixed] == [0, 0, 1]
    assert fixed[0] is blocks[0] and fixed[1] is blocks[1]


# ---------------------------------------------------------------------------------------------
# infer_lanes (sources without a lane identity)
# ---------------------------------------------------------------------------------------------


def _req(rid: str, seq: int, messages: list[dict[str, Any]], *, model: str = "claude-opus-5-5",
         t: list[dict[str, Any]] | None = None, session: str = "s1",
         fingerprint: bool = True) -> Any:
    from tokenbill.core.builders import make_request
    fp = _fp(messages, tools=t if t is not None else tools())[0] if fingerprint else None
    return make_request("unknown", seq, 1000 * seq, {"uncached_input": 10}, model,
                        request_id=rid, session_key=session, fingerprint=fp)


def test_infer_lanes_follows_the_longest_prefix() -> None:
    main = conversation(3)
    other = [{"role": "user", "content": "An unrelated batch job"}]
    reqs = [_req("r0", 0, main[:1]), _req("r1", 1, other), _req("r2", 2, main[:3]),
            _req("r3", 3, other + [{"role": "assistant", "content": "ok"}]),
            _req("r4", 4, main[:5])]
    lanes = F.infer_lanes(reqs)
    assert lanes["r0"] == lanes["r2"] == lanes["r4"]
    assert lanes["r1"] == lanes["r3"] != lanes["r0"]
    assert lanes["r0"] == stable_id("ln", "s1", "inferred", "r0")


def test_infer_lanes_requires_same_model_and_tools_tier() -> None:
    main = conversation(2)
    reqs = [_req("a", 0, main[:1]), _req("b", 1, main[:3], model="claude-haiku-4-5"),
            _req("c", 2, main[:3], t=tools()[:1]), _req("d", 3, main[:5])]
    lanes = F.infer_lanes(reqs)
    assert len({lanes["a"], lanes["b"], lanes["c"]}) == 3
    assert lanes["d"] == lanes["a"]


def test_infer_lanes_without_fingerprints_groups_by_model_per_session() -> None:
    reqs = [_req("a", 0, [], fingerprint=False), _req("b", 1, [], fingerprint=False),
            _req("c", 2, [], fingerprint=False, model="claude-haiku-4-5"),
            _req("d", 3, [], fingerprint=False, session="s2")]
    lanes = F.infer_lanes(reqs)
    assert lanes["a"] == lanes["b"] != lanes["c"]
    assert lanes["d"] not in (lanes["a"], lanes["c"])


def test_infer_lanes_siblings_and_no_shared_block_open_new_lanes() -> None:
    base = conversation(1)
    a = base + [{"role": "assistant", "content": "variant A"}]
    b = base + [{"role": "assistant", "content": "variant B"}]
    reqs = [_req("x", 0, [{"role": "user", "content": "first"}]),
            _req("y", 1, [{"role": "user", "content": "second"}]),
            _req("s1", 2, a), _req("s2", 3, b)]
    lanes = F.infer_lanes(reqs)
    assert lanes["x"] != lanes["y"]
    assert lanes["s1"] == lanes["s2"]          # s2 extends s1's shared prefix
    assert F.tools_tier_hash(None) is None


def test_trie_remove_prunes_paths() -> None:
    trie = F._Trie()
    trie.insert(["a", "b"], "L1")
    trie.insert(["a", "c"], "L2")
    trie.remove(["a", "b"], "L1")
    assert trie.deepest(["a", "b"]) == (1, {"L2"})
    trie.remove(["x"], "L3")
    trie.remove(["a", "c"], "L2")
    assert trie.deepest(["a"]) == (0, set())


def test_blockref_shape_is_contract() -> None:
    fp, _, _ = _fp(conversation(1))
    assert all(isinstance(b, BlockRef) and len(b.h) == 32 for b in fp.blocks)
