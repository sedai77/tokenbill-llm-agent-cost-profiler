"""Block fingerprinting (SPEC §5.8; package TRACE).

A request payload (tools, system, messages) becomes a content-free
:class:`~tokenbill.core.records.ContentFingerprint`: one :class:`~tokenbill.core.records.BlockRef`
per rendered block in wire order — one per tool definition, the system prompt as one
``system_text`` block (a string) or one block per system content block, and one block per message
content block (a string content is one ``text`` block).

Per block, every ``cache_control`` key is removed recursively **before** hashing (recorded as a
:class:`~tokenbill.core.records.Breakpoint` at the block's index with its TTL, default ``"5m"``), so
moving a marker never changes a hash. Hashes are HMAC-SHA256 (hex, 32 characters) under the caller's
key:

* ``h`` — the tier tag plus the wire bytes ``json.dumps(block, ensure_ascii=False,
  separators=(",", ":"))`` with keys **in the order sent** (a key-order change is a real
  serialization change for the provider's cache);
* ``h_sorted`` — the tier tag plus ``common.canonical_json(block)`` (key-sorted: equal for pure
  serialization churn);
* ``h_norm`` — the tier tag plus the wire bytes after every string value went through
  :func:`normalize_volatile` (volatile spans replaced by ``<class>`` placeholders).

The tier tag is ``"<tier>\\x1f<role>\\x1f"``: identical content in another tier or another message
role never shares a hash. Volatile classes (the frozen ``breakers.VOLATILE_PATTERNS``, named by
:data:`VOLATILE_CLASSES`) are computed on the block's string values before hashing. ``n_bytes`` is
the UTF-8 length of the wire; ``est_tokens = ceil(n_bytes / bytes_per_token)`` with 2.5 (tool
results), 2.7 (tool definitions) and 3.6 (everything else) bytes per token for the ``claude-4.7+``
tokenizer and × 1.3 for ``claude-legacy`` (an estimate, never billed); images are
``ceil(w/28)·ceil(h/28)`` tokens capped at 1,568 (4,784 for ``claude-4.7+`` high-res) when the
dimensions can be read from a base64 PNG / GIF / JPEG / WebP header, else ``None``. Tool
definitions sent with ``defer_loading: true`` are ``deferred``. ``lookback_pos`` collapses runs
of consecutive ``tool_use`` (and of ``tool_result``) blocks into one position
(:func:`lookback_positions`).
The content map (block hash → wire text) is returned only in the ``full`` tier.

:func:`infer_lanes` groups requests of a source without a lane identity into lanes (SPEC §5.8,
ruling R-E27): a request joins the lane whose last request shares the longest block-hash prefix with
it (a radix index over ``h``), requiring the same model and tools-tier hash; otherwise it opens a
new lane.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from bisect import bisect_left
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from tokenbill.breakers import VOLATILE_PATTERNS
from tokenbill.common import canonical_json
from tokenbill.core.errors import UsageError
from tokenbill.core.ids import key_id, stable_id
from tokenbill.core.records import (
    BlockRef,
    Breakpoint,
    ContentFingerprint,
    ContentTier,
    Request,
)

__all__ = [
    "BLOCK_TYPES",
    "IMAGE_TOKEN_CAP_HIGHRES",
    "IMAGE_TOKEN_CAP_STANDARD",
    "VOLATILE_CLASSES",
    "FingerprintCache",
    "est_tokens_for",
    "fingerprint_request",
    "image_dimensions",
    "image_tokens",
    "infer_lanes",
    "lookback_positions",
    "normalize_volatile",
    "plain",
    "replace_lookback",
    "request_breakpoints",
    "tools_tier_hash",
    "volatile_spans",
]

#: Class names of the frozen ``breakers.VOLATILE_PATTERNS``, in pattern order (earlier wins).
VOLATILE_CLASSES = ("iso_datetime", "iso_date", "clock_time", "unix_ts", "uuid", "counter")
if len(VOLATILE_CLASSES) != len(VOLATILE_PATTERNS):  # pragma: no cover - frozen module
    raise ImportError("breakers.VOLATILE_PATTERNS changed; update VOLATILE_CLASSES")

#: Message content block types kept as their own :data:`~tokenbill.core.records.BLOCK_KINDS`; any
#: other type is ``other``.
BLOCK_TYPES = frozenset({"text", "tool_use", "tool_result", "image", "document", "thinking",
                         "redacted_thinking", "compaction"})
#: Image token caps (SPEC §19.3): standard models and the 4.7+ high-res path.
IMAGE_TOKEN_CAP_STANDARD = 1_568
IMAGE_TOKEN_CAP_HIGHRES = 4_784

_HASH_CHARS = 32
_RUN_KINDS = frozenset({"tool_use", "tool_result"})
_BREAKPOINT_TTLS = frozenset({"5m", "1h", "30m"})
#: Bytes per token × 1000 (integer arithmetic keeps estimates identical on every platform).
_CPT_MILLI = {"tool_result": 2_500, "tool_def": 2_700}
_CPT_DEFAULT_MILLI = 3_600
_LEGACY_FACTOR = (13, 10)
_LEGACY_FAMILY = "claude-legacy"
_HIGHRES_FAMILY = "claude-4.7+"
#: At most this many base64 characters of an image are decoded to read its header.
_IMAGE_HEAD_B64 = 256 * 1024
_CC_MARK = '"cache_control"'


# ---------------------------------------------------------------------------------------------
# volatile spans
# ---------------------------------------------------------------------------------------------


def volatile_spans(text: str) -> list[tuple[str, int, int]]:
    """Non-overlapping ``(class, start, end)`` spans of volatile values in *text*, sorted by start.

    Classes come from the frozen ``breakers.VOLATILE_PATTERNS`` (:data:`VOLATILE_CLASSES`); an
    earlier pattern wins an overlap, so an ISO datetime is one ``iso_datetime`` span rather than a
    date plus a clock time.
    """
    if not isinstance(text, str) or not text:
        return []
    starts: list[int] = []
    spans: list[tuple[int, int, str]] = []
    for cls, pattern in zip(VOLATILE_CLASSES, VOLATILE_PATTERNS, strict=True):
        for m in pattern.finditer(text):
            a, b = m.start(), m.end()
            if a == b:
                continue
            i = bisect_left(starts, a)
            if i > 0 and spans[i - 1][1] > a:
                continue
            if i < len(spans) and spans[i][0] < b:
                continue
            starts.insert(i, a)
            spans.insert(i, (a, b, cls))
    return [(cls, a, b) for a, b, cls in spans]


def normalize_volatile(text: str) -> str:
    """*text* with every :func:`volatile_spans` span replaced by ``<class>``."""
    spans = volatile_spans(text)
    if not spans:
        return text
    out: list[str] = []
    pos = 0
    for cls, start, end in spans:
        out.append(text[pos:start])
        out.append(f"<{cls}>")
        pos = end
    out.append(text[pos:])
    return "".join(out)


# ---------------------------------------------------------------------------------------------
# plain JSON conversion (SDK objects never go through repr)
# ---------------------------------------------------------------------------------------------


def plain(obj: Any, _depth: int = 0) -> Any:
    """A JSON-shaped deep copy of a request payload value.

    Mappings and sequences are copied; SDK objects are converted with ``model_dump(mode="json")``
    (or ``model_dump()`` / ``to_dict()``) when present — never ``repr``, so no object address or
    free-form rendering enters a hash; anything else becomes ``{"type": <class name>}``. Bytes
    become ``{"type": "bytes", "n_bytes": len}``.
    """
    if _depth > 200:
        return None
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, Mapping):
        return {str(k): plain(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [plain(v, _depth + 1) for v in obj]
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return {"type": "bytes", "n_bytes": len(obj)}
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        try:
            try:
                data = dump(mode="json")
            except TypeError:
                data = dump()
            return plain(data, _depth + 1)
        except Exception:  # a broken SDK object: fall through to the type name
            pass
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        try:
            return plain(to_dict(), _depth + 1)
        except Exception:
            pass
    return {"type": type(obj).__name__}


# ---------------------------------------------------------------------------------------------
# block walk
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Raw:
    tier: str
    kind: str
    role: str | None
    obj: Any            # the block as sent (a mapping, possibly with cache_control keys)


def _walk(tools: Sequence[Any] | None, system: Any, messages: Sequence[Any] | None) -> list[_Raw]:
    out: list[_Raw] = []
    for tool in tools or ():
        out.append(_Raw("tools", "tool_def", None, tool))
    if isinstance(system, str):
        if system:
            out.append(_Raw("system", "system_text", None, {"type": "text", "text": system}))
    elif isinstance(system, (list, tuple)):
        for block in system:
            if isinstance(block, str):
                block = {"type": "text", "text": block}
            out.append(_Raw("system", "system_text", None, block))
    for msg in messages or ():
        if not isinstance(msg, Mapping):
            out.append(_Raw("messages", "other", None, msg))
            continue
        role = msg.get("role")
        role = role if isinstance(role, str) and len(role) <= 32 else None
        content = msg.get("content")
        if isinstance(content, str):
            out.append(_Raw("messages", "text", role, {"type": "text", "text": content}))
        elif isinstance(content, (list, tuple)):
            for block in content:
                if isinstance(block, str):
                    out.append(_Raw("messages", "text", role, {"type": "text", "text": block}))
                    continue
                btype = block.get("type") if isinstance(block, Mapping) else None
                kind = btype if isinstance(btype, str) and btype in BLOCK_TYPES else "other"
                out.append(_Raw("messages", kind, role, block))
    return out


def lookback_positions(blocks: Sequence[BlockRef] | Sequence[str]) -> list[int]:
    """Lookback position per block (SPEC §5.8, §19.3): a run of consecutive ``tool_use`` blocks is
    one position, likewise a run of ``tool_result`` blocks; every other block is its own position.

    Accepts :class:`BlockRef` objects or bare kind strings. Positions count from 0 in wire order.
    """
    positions: list[int] = []
    prev: str | None = None
    for b in blocks:
        kind = b if isinstance(b, str) else b.kind
        if positions and kind in _RUN_KINDS and kind == prev:
            positions.append(positions[-1])
        else:
            positions.append(positions[-1] + 1 if positions else 0)
        prev = kind
    return positions


def _first_ttl(obj: Any, _depth: int = 0) -> str | None:
    """The TTL of the first ``cache_control`` mapping anywhere in *obj* (None when absent)."""
    if _depth > 64:
        return None
    if isinstance(obj, Mapping):
        cc = obj.get("cache_control")
        if isinstance(cc, Mapping):
            return _ttl(cc)
        for v in obj.values():
            if isinstance(v, (Mapping, list, tuple)):
                found = _first_ttl(v, _depth + 1)
                if found is not None:
                    return found
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            found = _first_ttl(v, _depth + 1)
            if found is not None:
                return found
    return None


def _ttl(cc: Mapping[str, Any]) -> str:
    ttl = cc.get("ttl")
    return ttl if isinstance(ttl, str) and ttl in _BREAKPOINT_TTLS else "5m"


def _strip(obj: Any, markers: list[str], _depth: int = 0) -> Any:
    """A copy of *obj* without any ``cache_control`` key; the TTL of each removed marker mapping
    is appended to *markers*."""
    if _depth > 200:
        return None
    if isinstance(obj, Mapping):
        cc = obj.get("cache_control")
        if isinstance(cc, Mapping):
            markers.append(_ttl(cc))
        return {k: _strip(v, markers, _depth + 1) for k, v in obj.items() if k != "cache_control"}
    if isinstance(obj, (list, tuple)):
        return [_strip(v, markers, _depth + 1) for v in obj]
    return obj


def request_breakpoints(*, tools: Sequence[Any] | None, system: Any,
                        messages: Sequence[Any] | None) -> tuple[tuple[Breakpoint, ...], int]:
    """Content-free ``(breakpoints, block count)`` of a payload: the block index and TTL of every
    ``cache_control`` marker, without hashing anything (usable in content tier ``none``)."""
    raws = _walk(tools, system, messages)
    bps = []
    for i, raw in enumerate(raws):
        ttl = _first_ttl(raw.obj)
        if ttl is not None:
            bps.append(Breakpoint(block_index=i, ttl=ttl))
    return tuple(bps), len(raws)


# ---------------------------------------------------------------------------------------------
# token estimates and images
# ---------------------------------------------------------------------------------------------


def est_tokens_for(kind: str, n_bytes: int, tokenizer_family: str = _HIGHRES_FAMILY) -> int:
    """``ceil(n_bytes / bytes_per_token)`` for a non-image block (integer arithmetic)."""
    cpt = _CPT_MILLI.get(kind, _CPT_DEFAULT_MILLI)
    if tokenizer_family == _LEGACY_FAMILY:
        cpt = cpt * _LEGACY_FACTOR[0] // _LEGACY_FACTOR[1]
    return -(-(n_bytes * 1000) // cpt)


def image_tokens(width: int, height: int, tokenizer_family: str = _HIGHRES_FAMILY) -> int:
    """``ceil(w/28)·ceil(h/28)`` capped at 1,568 tokens (4,784 on the ``claude-4.7+`` high-res
    path)."""
    tokens = (-(-width // 28)) * (-(-height // 28))
    cap = IMAGE_TOKEN_CAP_HIGHRES if tokenizer_family == _HIGHRES_FAMILY \
        else IMAGE_TOKEN_CAP_STANDARD
    return min(tokens, cap)


def _u16be(b: bytes, i: int) -> int:
    return (b[i] << 8) | b[i + 1]


def _u16le(b: bytes, i: int) -> int:
    return b[i] | (b[i + 1] << 8)


def _u24le(b: bytes, i: int) -> int:
    return b[i] | (b[i + 1] << 8) | (b[i + 2] << 16)


def _u32be(b: bytes, i: int) -> int:
    return (b[i] << 24) | (b[i + 1] << 16) | (b[i + 2] << 8) | b[i + 3]


_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """``(width, height)`` from the header of a PNG, GIF, WebP or JPEG image, else None."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            return _u32be(data, 16), _u32be(data, 20)
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return _u16le(data, 6), _u16le(data, 8)
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8 ":
                return _u16le(data, 26) & 0x3FFF, _u16le(data, 28) & 0x3FFF
            if chunk == b"VP8L" and data[20] == 0x2F:
                bits = data[21] | (data[22] << 8) | (data[23] << 16) | (data[24] << 24)
                return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            if chunk == b"VP8X":
                return _u24le(data, 24) + 1, _u24le(data, 27) + 1
            return None
        if data[:2] == b"\xff\xd8":
            i = 2
            n = len(data)
            while i + 9 < n:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker == 0xFF:
                    i += 1
                    continue
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                if marker in _JPEG_SOF:
                    return _u16be(data, i + 7), _u16be(data, i + 5)
                i += 2 + _u16be(data, i + 2)
    except IndexError:
        return None
    return None


def _image_px(block: Mapping[str, Any]) -> tuple[int, int] | None:
    source = block.get("source")
    if not isinstance(source, Mapping) or source.get("type") != "base64":
        return None
    data = source.get("data")
    if not isinstance(data, str) or not data:
        return None
    head = data[:_IMAGE_HEAD_B64]
    head = head[: len(head) - len(head) % 4]
    try:
        raw = base64.b64decode(head, validate=False)
    except (binascii.Error, ValueError):
        return None
    dims = image_dimensions(raw)
    if dims is None or dims[0] <= 0 or dims[1] <= 0:
        return None
    return dims


# ---------------------------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------------------------


def _hmac(key: bytes, data: bytes) -> str:
    return hmac.new(key, data, hashlib.sha256).hexdigest()[:_HASH_CHARS]


def _wire(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _sorted_wire(obj: Any) -> str:
    try:
        return canonical_json(obj)
    except ValueError:  # NaN/Infinity: hashed as json.dumps renders them
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _strings(obj: Any, out: list[str], _depth: int = 0) -> None:
    if _depth > 200:
        return
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, Mapping):
        for v in obj.values():
            _strings(v, out, _depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _strings(v, out, _depth + 1)


def _normalized(obj: Any, _depth: int = 0) -> Any:
    if _depth > 200:
        return None
    if isinstance(obj, str):
        return normalize_volatile(obj)
    if isinstance(obj, Mapping):
        return {k: _normalized(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalized(v, _depth + 1) for v in obj]
    return obj


class FingerprintCache:
    """A bounded memo of fingerprinted blocks keyed by (key id, tokenizer family, tier tag, wire,
    lookback position): re-sent blocks — most of a growing agent context — are hashed once.

    Holds wire text in memory only (never written); bounded by *max_bytes* of wire text, least
    recently used first out.
    """

    def __init__(self, max_bytes: int = 64 * 2**20) -> None:
        self.max_bytes = max_bytes
        self._bytes = 0
        self._items: OrderedDict[tuple, tuple[BlockRef, str]] = OrderedDict()
        self.hits = 0
        self.misses = 0

    def get(self, k: tuple) -> tuple[BlockRef, str] | None:
        """The cached ``(BlockRef, wire)`` for *k*, or None."""
        hit = self._items.get(k)
        if hit is not None:
            self._items.move_to_end(k)
            self.hits += 1
        else:
            self.misses += 1
        return hit

    def put(self, k: tuple, value: tuple[BlockRef, str]) -> None:
        """Remember *value*; evicts the least recently used entries beyond ``max_bytes``."""
        size = len(k[3]) + 64
        if size > self.max_bytes:
            return
        if k in self._items:
            return
        self._items[k] = value
        self._bytes += size
        while self._bytes > self.max_bytes and self._items:
            old, _ = self._items.popitem(last=False)
            self._bytes -= len(old[3]) + 64

    def __len__(self) -> int:
        return len(self._items)


def _block_ref(raw: _Raw, stripped: Any, wire: str, tag: bytes, key: bytes, family: str,
               pos: int) -> BlockRef:
    wire_b = wire.encode("utf-8", "surrogatepass")
    strings: list[str] = []
    _strings(stripped, strings)
    classes = sorted({cls for s in strings for cls, _a, _b in volatile_spans(s)})
    h = _hmac(key, tag + wire_b)
    h_norm = h if not classes else _hmac(
        key, tag + _wire(_normalized(stripped)).encode("utf-8", "surrogatepass"))
    n_bytes = len(wire_b)
    px: tuple[int, int] | None = None
    if raw.kind == "image" and isinstance(stripped, Mapping):
        px = _image_px(stripped)
        est = image_tokens(px[0], px[1], family) if px is not None else None
    else:
        est = est_tokens_for(raw.kind, n_bytes, family)
    deferred = (raw.tier == "tools" and isinstance(stripped, Mapping)
                and stripped.get("defer_loading") is True)
    return BlockRef(
        h=h,
        h_sorted=_hmac(key, tag + _sorted_wire(stripped).encode("utf-8", "surrogatepass")),
        h_norm=h_norm,
        tier=raw.tier,
        kind=raw.kind,
        role=raw.role,
        n_bytes=n_bytes,
        est_tokens=est,
        image_px=px,
        volatile_classes=tuple(classes),
        lookback_pos=pos,
        deferred=deferred,
    )


def fingerprint_request(*, tools: Sequence[Mapping], system: str | Sequence[Mapping] | None,
                        messages: Sequence[Mapping], key: bytes, tier: ContentTier,
                        tokenizer_family: str = "claude-4.7+",
                        cache: FingerprintCache | None = None,
                        ) -> tuple[ContentFingerprint, tuple[Breakpoint, ...], dict[str, str]]:
    """``(fingerprint, breakpoints, content map)`` of one request payload (SPEC §5.8).

    *key* is the HMAC key of the block hashes (its :func:`~tokenbill.core.ids.key_id` becomes
    ``ContentFingerprint.key_id``); *tier* must be ``fingerprint`` or ``full`` (hashes are
    content-derived, D22) — the content map (``h`` → wire text) is filled only for ``full``.
    Breakpoints are the removed ``cache_control`` markers (block index, TTL, not assumed).
    *cache* (optional) memoizes blocks across requests of one producer.
    """
    try:
        tier = ContentTier(tier)
    except ValueError:
        raise UsageError("unknown content tier") from None
    if tier is ContentTier.NONE:
        raise UsageError("block fingerprints need content tier fingerprint or full (D22)")
    if not isinstance(key, (bytes, bytearray)) or not key:
        raise UsageError("fingerprints need a non-empty key")
    key = bytes(key)
    kid = key_id(key)
    raws = _walk(tools, system, messages)
    positions = lookback_positions([r.kind for r in raws])
    blocks: list[BlockRef] = []
    bps: list[Breakpoint] = []
    content: dict[str, str] = {}
    for idx, (raw, pos) in enumerate(zip(raws, positions, strict=True)):
        obj = raw.obj
        try:
            wire = _wire(obj)
        except (TypeError, ValueError, RecursionError):
            obj = plain(obj)
            wire = _wire(obj)
        stripped: Any = obj
        if _CC_MARK in wire:
            markers: list[str] = []
            stripped = _strip(obj, markers)
            if markers:
                bps.append(Breakpoint(block_index=idx, ttl=markers[0]))
            wire = _wire(stripped)
        tag = f"{raw.tier}\x1f{raw.role or ''}\x1f".encode()
        ck = (kid, tokenizer_family, tag, wire, pos, raw.kind)
        hit = cache.get(ck) if cache is not None else None
        if hit is None:
            ref = _block_ref(raw, stripped, wire, tag, key, tokenizer_family, pos)
            if cache is not None:
                cache.put(ck, (ref, wire))
        else:
            ref = hit[0]
        blocks.append(ref)
        if tier is ContentTier.FULL:
            content[ref.h] = wire
    n_tools = sum(1 for r in raws if r.tier == "tools")
    n_system = sum(1 for r in raws if r.tier == "system")
    fp = ContentFingerprint(key_id=kid, blocks=tuple(blocks),
                            tier_end=(n_tools, n_tools + n_system, len(blocks)))
    return fp, tuple(bps), content


# ---------------------------------------------------------------------------------------------
# lane inference
# ---------------------------------------------------------------------------------------------


def tools_tier_hash(fp: ContentFingerprint | None) -> str | None:
    """SHA-256 (hex, 24) of the ``h`` sequence of the tools tier, or None without a fingerprint."""
    if fp is None:
        return None
    hs = "\x1f".join(b.h for b in fp.blocks[: fp.tier_end[0]])
    return hashlib.sha256(hs.encode()).hexdigest()[:24]


class _Node:
    __slots__ = ("children", "lanes")

    def __init__(self) -> None:
        self.children: dict[str, _Node] = {}
        self.lanes: set[str] = set()


class _Trie:
    """Radix index over block-hash paths of each lane's last request."""

    def __init__(self) -> None:
        self.root = _Node()

    def insert(self, path: Sequence[str], lane: str) -> None:
        node = self.root
        node.lanes.add(lane)
        for h in path:
            nxt = node.children.get(h)
            if nxt is None:
                nxt = node.children[h] = _Node()
            nxt.lanes.add(lane)
            node = nxt

    def remove(self, path: Sequence[str], lane: str) -> None:
        node = self.root
        node.lanes.discard(lane)
        for h in path:
            nxt = node.children.get(h)
            if nxt is None:
                return
            nxt.lanes.discard(lane)
            if not nxt.lanes:
                del node.children[h]
                return
            node = nxt

    def deepest(self, path: Sequence[str]) -> tuple[int, set[str]]:
        node = self.root
        depth = 0
        for h in path:
            nxt = node.children.get(h)
            if nxt is None or not nxt.lanes:
                break
            node = nxt
            depth += 1
        return depth, node.lanes


def _order_key(r: Request) -> tuple[str, int, int, str]:
    return (r.session_key, r.attempts[0].ts_start_ms, r.seq, r.request_id)


def infer_lanes(requests: Iterable[Request]) -> dict[str, str]:
    """``request_id → lane_key`` for requests of sources without a lane identity (SPEC §5.8).

    Per run (``session_key``), in ``(ts, seq, request_id)`` order: a fingerprinted request joins
    the lane whose last request shares the longest block-hash prefix beyond the tools tier with it
    (radix index over ``h``), among lanes with the same model and tools-tier hash; ties go to the
    most recently extended lane. No shared block (or no candidate) opens a new lane. Requests
    without a fingerprint join the latest unfingerprinted lane of the same model. Lane keys are
    ``stable_id("ln", session_key, "inferred", <first request id>)``.
    """
    out: dict[str, str] = {}
    by_session: dict[str, list[Request]] = {}
    for r in requests:
        by_session.setdefault(r.session_key, []).append(r)
    for session_key in sorted(by_session):
        tries: dict[tuple[str, str | None], _Trie] = {}
        tails: dict[str, list[str]] = {}
        recency: dict[str, int] = {}
        plain_lanes: dict[str, str] = {}
        tick = 0
        for r in sorted(by_session[session_key], key=_order_key):
            tick += 1
            fp = r.fingerprint
            if fp is None:
                lane = plain_lanes.get(r.model)
                if lane is None:
                    lane = plain_lanes[r.model] = stable_id("ln", session_key, "inferred",
                                                            r.request_id)
                out[r.request_id] = lane
                continue
            group = (r.model, tools_tier_hash(fp))
            trie = tries.setdefault(group, _Trie())
            path = [b.h for b in fp.blocks[fp.tier_end[0]:]]
            depth, lanes = trie.deepest(path)
            if depth > 0 and lanes:
                lane = max(lanes, key=lambda k: (recency[k], k))
                trie.remove(tails[lane], lane)
            else:
                lane = stable_id("ln", session_key, "inferred", r.request_id)
            trie.insert(path, lane)
            tails[lane] = path
            recency[lane] = tick
            out[r.request_id] = lane
    return out


def replace_lookback(blocks: Sequence[BlockRef]) -> tuple[BlockRef, ...]:
    """*blocks* with ``lookback_pos`` recomputed from their order (:func:`lookback_positions`);
    blocks already at the right position are returned as the same objects."""
    positions = lookback_positions(blocks)
    return tuple(b if b.lookback_pos == p else replace(b, lookback_pos=p)
                 for b, p in zip(blocks, positions, strict=True))

