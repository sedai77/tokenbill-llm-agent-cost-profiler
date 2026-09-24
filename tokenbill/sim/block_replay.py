"""Block-level replay (SPEC §9.7, §10.4, D28; package BLOCK).

A documented-rules model of the Anthropic prompt cache over fingerprinted traffic
(``Request.fingerprint``: recorder trace@2 fingerprint profile, trace@1 through the adapter).

**Chain.** Every block of a request is a node of a prefix chain keyed by ``(parent, tier, block
hash, tier salts)``. The root carries the fingerprint key id, the cache scope, the serving model and
the channel's run-collapsing rule; each tier's first block carries the salt of that tier (and of
empty tiers before it): the request parameters ``CacheRules.tier_params`` lists for the tier
(tools: model and tool definitions, which are the root and the blocks themselves; system: speed,
web search, citations; messages: tool_choice, disable_parallel_tool_use, image presence,
thinking, effort, output format). **Effort and thinking are left out of the messages salt when
``core.cache_rules.effort_change_keeps_cache`` is True for the request** (D28) — the predicate the
usage level uses, so both engines agree. The invalidation hierarchy is thus plain inequality of
chain nodes. Block hashes exclude ``cache_control`` (a moved marker never changes a hash). A
parameter a request does not report (``None``) takes the lane's nearest reported value, so — as in
``core.transitions`` — only two reported, different values count as a change; and two requests'
salts are compared under the later request's exemption (``core.transitions`` evaluates the
predicate for request ``i``), so a flip of the exemption itself (a client upgrade, a beta added)
with an unchanged effort breaks nothing.

**Entries** are keyed by chain node (so by ``(cache scope, model, chain hash at block k)``) with
``visible_ms`` = the writer's start + ttft, else + duration, else + 1,000 ms (same-timestamp
siblings never read each other), ``expires_ms`` = start + TTL, refreshed on read to
``max(expires, reader start + TTL)``. A breakpoint looks back at most 20 collapsed positions
(runs of ``tool_use`` / ``tool_result`` blocks count as one position on channels that collapse
them; the breakpoint's own position is the first) for the deepest live, visible entry. Tokens up
to the deepest hit are reads; from the hit to the last breakpoint are writes at each segment's
breakpoint TTL; after it uncached. A breakpoint whose prefix is below the model's minimum
cacheable tokens writes nothing. At most ``CacheRules.max_breakpoints`` (4) breakpoints; automatic
caching takes one slot at the end of the prompt; assumed end-of-messages breakpoints (trace@1
counts, §5.5) count as observed.

**Token sizes.** Block ``est_tokens`` are rescaled per request so the request's tokens sum to its
billed ``total_input`` (the v0.1 honesty invariant); a cache hit reads the size its entry recorded
when written (clamped to the request's total; the whole total when the hit covers every block),
and the blocks after the hit share the rest in proportion to ``est_tokens``.

**Placements** (``Policy.breakpoint_policy``): ``observed`` (the recorded markers), ``end`` (end of
the prompt every call — v0.1's optimum), ``static_plus_end`` (end of the static prefix + end) and
``every_15`` (static + the largest positive multiples of 15 collapsed positions below the end +
end, within the breakpoint limit; lookback-safe for turns that add up to about 45 positions). The
*static prefix* of a request ends at its deepest block whose prefix a request of another lane of
the cohort also sends, else at the end of its lane's common prefix (hindsight over the replayed
lanes).

**Repairs** (``Policy.repairs`` prefixed ``block:``; SPEC §10.4 "repair priced"):
``block:volatile`` (system blocks with volatile classes use their normalized hash ``h_norm``),
``block:sort_keys`` (every block uses its key-sorted hash ``h_sorted``), ``block:tool_order``
(tool definitions in first-seen order per lane), ``block:tool_superset`` (every request sends the
lane's constant superset of tool definitions in first-seen order; the added definitions add
tokens at the request's own scale), ``block:pin_params`` (tier salts pinned to the lane's first
values), ``block:add_end`` (one end breakpoint on requests without breakpoints or automatic
caching), ``block:drop_unread`` (hindsight: drop the observed breakpoints the
``write-never-read`` breaker flags — entries never read, billed as written, that no other lever
explains: fan-out, lookback overflow, a divergence of the next request, an idle gap past the TTL
and the tail write of a multi-request lane are kept) and
``block:stagger`` (send one, await its first token, then the rest: the visibility delay is
waived).

**Minimal change** (§9.1): ``replay(policy)`` prices every request at its billed cost minus
``model(observed) − model(policy)``, so ``replay(Policy.observed())`` equals the billed ledger to
the nano and model error cancels; requests the policy does not change keep their billed usage
exactly.
Cross-lane cache sharing is modeled inside a ``(team, lane_kind)`` cohort only, so sharded and
unsharded replays are identical (D30). Lanes without fingerprints on every request that has a
serving inference are skipped with a reason (callers fall back to usage-level replay). Everything
is integer arithmetic on tokens and int nano-USD (no floats); costs use the pricer's exact unit
rates (``price_usage`` points when a context has none).
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from operator import attrgetter

from tokenbill.core.cache_rules import CacheRules, RulesTable, effort_change_keeps_cache
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Calibration, Figure, add, estimated, sub, unpriced, zero
from tokenbill.core.money import RATIO_CTX
from tokenbill.core.protocols import CacheRulesProvider, Pricer
from tokenbill.core.records import (
    BlockRef,
    Inference,
    Lane,
    PricingContext,
    Request,
    UsageBuckets,
)
from tokenbill.core.types import CalibrationReport, Policy, ReplayRequestOutcome, ReplayResult

__all__ = [
    "BLOCK_REPAIRS",
    "BREAKPOINT_TTL_S",
    "CANONICAL_PLACEMENTS",
    "INTERMEDIATE_SPACING",
    "NO_RECOVERY_NOTE",
    "PLACEMENTS",
    "REPAIR_ADD_END",
    "REPAIR_DROP_UNREAD",
    "REPAIR_PIN_PARAMS",
    "REPAIR_SORT_KEYS",
    "REPAIR_STAGGER",
    "REPAIR_TOOL_ORDER",
    "REPAIR_TOOL_SUPERSET",
    "REPAIR_VOLATILE",
    "VISIBLE_DEFAULT_MS",
    "BlockReplayer",
    "chain_hashes",
    "first_divergence",
    "lane_skip_reason",
    "read_agreement",
    "salt_diff",
]

#: Breakpoint placements of ``Policy.breakpoint_policy`` (§9.5 grammar).
PLACEMENTS = ("observed", "end", "static_plus_end", "every_15")
#: The canonical (non-observed) placements, in tie-break order.
CANONICAL_PLACEMENTS = ("end", "static_plus_end", "every_15")
REPAIR_VOLATILE = "block:volatile"
REPAIR_SORT_KEYS = "block:sort_keys"
REPAIR_TOOL_ORDER = "block:tool_order"
REPAIR_TOOL_SUPERSET = "block:tool_superset"
REPAIR_PIN_PARAMS = "block:pin_params"
REPAIR_ADD_END = "block:add_end"
REPAIR_DROP_UNREAD = "block:drop_unread"
REPAIR_STAGGER = "block:stagger"
#: Every ``block:`` repair the engine prices.
BLOCK_REPAIRS = (REPAIR_VOLATILE, REPAIR_SORT_KEYS, REPAIR_TOOL_ORDER, REPAIR_TOOL_SUPERSET,
                 REPAIR_PIN_PARAMS, REPAIR_ADD_END, REPAIR_DROP_UNREAD, REPAIR_STAGGER)
_CHAIN_REPAIRS = frozenset({REPAIR_VOLATILE, REPAIR_SORT_KEYS, REPAIR_TOOL_ORDER,
                            REPAIR_TOOL_SUPERSET, REPAIR_PIN_PARAMS})
#: Spacing (collapsed positions) of the ``every_15`` intermediate breakpoints.
INTERMEDIATE_SPACING = 15
#: Visibility delay when the writer's ttft and duration are unknown (§9.7 #2).
VISIBLE_DEFAULT_MS = 1_000
#: Breakpoint TTL strings → seconds (``Breakpoint.ttl`` values).
BREAKPOINT_TTL_S: Mapping[str, int] = {"5m": 300, "30m": 1800, "1h": 3600}
#: The v0.1 wording for a repair the model prices above the observed placement (floored at 0).
NO_RECOVERY_NOTE = "no recovery modeled — billed caching already beats the simulated fix"

_TIERS = ("tools", "system", "messages")
_TOOL_RUN_KINDS = frozenset({"tool_use", "tool_result"})
_NOT_SALT = frozenset({"model", "tool_defs"})     # the chain root and the tool blocks themselves
_EFFORT_PARAMS = ("thinking", "effort")
_DEFAULT_TTL_S = 300
_DAY_MS = 86_400_000
_OTHER_TTL_S = 1800
_H = attrgetter("h")


# =============================================================================================
# salts and pairwise helpers (public)
# =============================================================================================


def _param_value(req: Request, name: str) -> object:
    """A salted parameter of *req*: the served speed, else the ``RequestParams`` field of that
    name (None when the request does not report it or the field does not exist)."""
    if name == "speed":
        inf = req.serving_inference
        if inf is not None:
            return inf.pricing.speed
        return req.params.speed
    return getattr(req.params, name, None)


def _ctx_of(req: Request) -> PricingContext | None:
    inf = req.serving_inference
    return inf.pricing if inf is not None else None


def _effort_exempt(req: Request) -> bool:
    ctx = _ctx_of(req)
    return effort_change_keeps_cache(
        agent_product=req.attribution.agent_product,
        model=(ctx.model if ctx is not None and ctx.model else req.model),
        channel=ctx.channel if ctx is not None else "unknown",
        client_version=req.attribution.client_version,
        betas=req.params.betas,
    )


def _salt_names(rules: CacheRules) -> tuple[tuple[str, ...], ...]:
    table = dict(rules.tier_params)
    return tuple(tuple(n for n in table.get(t, ()) if n not in _NOT_SALT) for t in _TIERS)


def _all_salt_names(rules: CacheRules) -> tuple[str, ...]:
    names: list[str] = []
    for tier_names in _salt_names(rules):
        names.extend(n for n in tier_names if n not in names)
    for n in _EFFORT_PARAMS:
        if n not in names:
            names.append(n)
    return tuple(names)


def _salts(req: Request, rules: CacheRules, values: Mapping[str, object],
           exempt: bool | None = None) -> tuple[tuple[tuple[str, object], ...], ...]:
    """The three tier salts of *req* (tools, system, messages) from parameter *values*.

    *exempt* is the D28 effort exemption to apply (default: *req*'s own). Pairwise comparisons
    pass the exemption of the later request for both sides, exactly as ``core.transitions``
    evaluates ``effort_change_keeps_cache`` for request ``i`` only — so an exemption that flips
    between two requests (a client upgrade, a beta added) with an unchanged effort is no change."""
    names = _salt_names(rules)
    if exempt is None:
        exempt = _effort_exempt(req)
    all_tiers = (not exempt) and req.model in rules.effort_invalidates_all_tiers_models
    out = []
    for ti, tier_names in enumerate(names):
        pairs = [(n, values.get(n)) for n in tier_names
                 if not (exempt and n in _EFFORT_PARAMS)]
        if all_tiers and ti < 2:
            pairs.extend((n, values.get(n)) for n in _EFFORT_PARAMS if n not in tier_names)
        out.append(tuple(pairs))
    return tuple(out)


def _rules_for(rules: CacheRulesProvider, req: Request) -> CacheRules:
    ctx = _ctx_of(req)
    if ctx is None:
        return rules.rules_for("anthropic", "anthropic_api", req.model)
    return rules.rules_for(ctx.provider, ctx.channel, ctx.model or req.model)


def _pair_values(prev: Request, cur: Request, rules: CacheRules
                 ) -> tuple[dict[str, object], dict[str, object]]:
    """Salted parameter values of two requests where a value only one side reports is copied to
    the other (None means not observed, never a change — as in ``core.transitions``)."""
    pv: dict[str, object] = {}
    cv: dict[str, object] = {}
    for n in _all_salt_names(rules):
        a, b = _param_value(prev, n), _param_value(cur, n)
        pv[n] = a if a is not None else b
        cv[n] = b if b is not None else a
    return pv, cv


def salt_diff(prev: Request, cur: Request, *, rules: CacheRules | None = None
              ) -> tuple[tuple[str, str], ...]:
    """``(tier, parameter)`` pairs whose tier salt differs between two requests (after the effort
    exemption of *cur*, applied to both sides as in ``core.transitions``, and the pairwise None
    rule), in tier order."""
    rules = rules if rules is not None else _rules_for(RulesTable(), cur)
    pv, cv = _pair_values(prev, cur, rules)
    exempt = _effort_exempt(cur)
    ps, cs = _salts(prev, rules, pv, exempt), _salts(cur, rules, cv, exempt)
    out: list[tuple[str, str]] = []
    for ti, tier in enumerate(_TIERS):
        a, b = dict(ps[ti]), dict(cs[ti])
        for name in sorted(set(a) | set(b)):
            if a.get(name) != b.get(name) or (name in a) != (name in b):
                out.append((tier, name))
    return tuple(out)


def _tier_starts(te: tuple[int, int, int]) -> tuple[int, int, int]:
    return (0, te[0], te[1])


def _salt_part(k: int, starts: tuple[int, int, int],
               salts: tuple[tuple[tuple[str, object], ...], ...]) -> tuple | None:
    if k != 0 and k != starts[1] and k != starts[2]:
        return None
    return tuple(salts[t] for t in range(3) if starts[t] == k)


def chain_hashes(request: Request, *, rules: CacheRules | None = None) -> tuple[str, ...]:
    """The tier-salted chain hash after each block of *request* (hex, 32 chars): what the block
    engine keys cache entries by (within one cache scope). Parameters the request does not report
    salt as absent. Empty for a request without a fingerprint."""
    fp = request.fingerprint
    if fp is None:
        return ()
    rules = rules if rules is not None else _rules_for(RulesTable(), request)
    values = {n: _param_value(request, n) for n in _all_salt_names(rules)}
    salts = _salts(request, rules, values)
    starts = _tier_starts(fp.tier_end)
    prev = hashlib.sha256(repr(("root", fp.key_id, request.model,
                                rules.collapse_tool_runs)).encode()).hexdigest()
    out: list[str] = []
    for k, block in enumerate(fp.blocks):
        part = _salt_part(k, starts, salts)
        text = repr((prev, block.tier, block.h, part))
        prev = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
        out.append(prev)
    return tuple(out)


def _tier_of(k: int, te: tuple[int, int, int]) -> str:
    if k < te[0]:
        return "tools"
    if k < te[1]:
        return "system"
    return "messages"


def _all_equal(a: Sequence[str | None], b: Sequence[str | None]) -> bool:
    return len(a) == len(b) and None not in a and None not in b and list(a) == list(b)


def _purely_volatile(pb: Sequence[BlockRef], cb: Sequence[BlockRef]) -> bool:
    if len(pb) != len(cb):
        return False
    if not _all_equal([b.h_norm for b in pb], [b.h_norm for b in cb]):
        return False
    changed = [(x, y) for x, y in zip(pb, cb, strict=True) if x.h != y.h]
    return bool(changed) and all(x.volatile_classes or y.volatile_classes for x, y in changed)


def _classify_tools(pb: Sequence[BlockRef], cb: Sequence[BlockRef]) -> str:
    if _all_equal([b.h_sorted for b in pb], [b.h_sorted for b in cb]):
        return "serialization"
    if _purely_volatile(pb, cb):
        return "volatile"
    use_sorted = all(b.h_sorted is not None for b in (*pb, *cb))
    kp = [b.h_sorted if use_sorted else b.h for b in pb]
    kc = [b.h_sorted if use_sorted else b.h for b in cb]
    if Counter(kp) == Counter(kc):
        return "tool-order"
    sp, sc = set(kp), set(kc)
    if sp <= sc or sc <= sp:
        return "tool-subset"
    return "tool-definition"


def _classify_system(pb: Sequence[BlockRef], cb: Sequence[BlockRef]) -> str:
    if _all_equal([b.h_sorted for b in pb], [b.h_sorted for b in cb]):
        return "serialization"
    if _purely_volatile(pb, cb):
        return "volatile"
    return "system-edit"


def _classify_message(old: BlockRef, new: BlockRef, cur: Request) -> str:
    if old.kind == "compaction" or new.kind == "compaction":
        return "compaction"
    if any(att.applied_edits for att in cur.attempts):
        return "context-edit"
    if any(att.thinking_dropped > 0 for att in cur.attempts):
        return "thinking-dropped"
    if old.h_sorted is not None and old.h_sorted == new.h_sorted:
        return "serialization"
    if old.h_norm is not None and old.h_norm == new.h_norm and (
            old.volatile_classes or new.volatile_classes):
        return "volatile"
    return "history-rewrite"


def first_divergence(prev: Request, cur: Request, *, rules: CacheRules | None = None
                     ) -> tuple[str, int, str] | None:
    """The first point where *cur*'s cached prefix stops matching *prev*'s (SPEC §9.7 #5).

    Returns ``(tier, block index in cur, cause)`` or None when *cur* extends (or is a prefix of)
    *prev*. Causes: ``model`` (serving model differs), ``key`` (fingerprint keys differ: hashes are
    not comparable), ``param`` (a tier salt differs: an invalidating parameter changed; effort and
    thinking only when ``effort_change_keeps_cache`` is False), ``serialization`` (``h`` differs,
    ``h_sorted`` equal), ``volatile`` (``h_norm`` equal and the changed blocks carry volatile
    classes), ``tool-order`` / ``tool-subset`` / ``tool-definition`` (tools tier: same multiset in
    another order / tools added or removed / a changed definition), ``system-edit``, and in the
    messages tier ``compaction`` (a compaction block), ``context-edit`` (applied context edits on
    *cur*), ``thinking-dropped`` (server-dropped thinking blocks) or ``history-rewrite``. Moved
    ``cache_control`` markers never diverge (hashes exclude them). The effort exemption is *cur*'s,
    applied to both requests (the ``core.transitions`` rule). None when either request has no
    fingerprint."""
    pf, cf = prev.fingerprint, cur.fingerprint
    if pf is None or cf is None:
        return None
    if prev.model != cur.model:
        return ("tools", 0, "model")
    if pf.key_id != cf.key_id:
        return ("tools", 0, "key")
    rules = rules if rules is not None else _rules_for(RulesTable(), cur)
    pv, cv = _pair_values(prev, cur, rules)
    exempt = _effort_exempt(cur)
    ps, cs = _salts(prev, rules, pv, exempt), _salts(cur, rules, cv, exempt)
    pblocks, cblocks = pf.blocks, cf.blocks
    n = len(cblocks)
    starts_c, starts_p = _tier_starts(cf.tier_end), _tier_starts(pf.tier_end)
    salt_at: int | None = None
    for t in range(3):
        if ps[t] != cs[t] and starts_c[t] < n:
            salt_at = starts_c[t]
            break
    m = min(len(pblocks), n)
    k = 0
    while k < m:
        a, b = pblocks[k], cblocks[k]
        if a.h != b.h or a.tier != b.tier:
            break
        k += 1
    # tier boundaries that moved inside the common prefix are divergences of their own
    for t in (1, 2):
        if starts_p[t] != starts_c[t]:
            k = min(k, starts_p[t], starts_c[t])
    if salt_at is not None and salt_at <= k:
        return (_tier_of(salt_at, cf.tier_end), salt_at, "param")
    if k >= m:
        return None
    # a block added to or removed from a tier shifts the next tier: classify the earlier tier
    tier = min(_tier_of(k, pf.tier_end), _tier_of(k, cf.tier_end), key=_TIERS.index)
    if tier == "tools":
        cause = _classify_tools(pblocks[:pf.tier_end[0]], cblocks[:cf.tier_end[0]])
    elif tier == "system":
        cause = _classify_system(pblocks[pf.tier_end[0]:pf.tier_end[1]],
                                 cblocks[cf.tier_end[0]:cf.tier_end[1]])
    else:
        cause = _classify_message(pblocks[k], cblocks[k], cur)
    return (tier, k, cause)


# =============================================================================================
# lanes, costs
# =============================================================================================


def _serving_requests(lane: Lane) -> list[Request]:
    return [r for r in lane.requests if r.serving_inference is not None]


def lane_skip_reason(lane: Lane) -> str | None:
    """Why *lane* cannot be block-replayed (None when it can): every request with a serving
    inference needs a fingerprint."""
    serving = _serving_requests(lane)
    if not serving:
        return "no requests with a serving inference"
    missing = sum(1 for r in serving if r.fingerprint is None)
    if missing == len(serving):
        return "no fingerprints (block replay needs the fingerprint tier)"
    if missing:
        return f"fingerprints missing on {missing} of {len(serving)} requests"
    return None


def _request_figure(pricer: Pricer, req: Request, basis: Basis) -> Figure:
    fig = zero(basis)
    for att in req.attempts:
        for inf in att.inferences:
            if inf.billable is not False:
                fig = add(fig, pricer.price_inference(inf, ts_ms=att.ts_start_ms).figure)
    return fig


class _Costs:
    """Point nano of predicted input usage, with unit rates memoized per (context, day)."""

    __slots__ = ("_memo", "pricer", "unpriced")

    def __init__(self, pricer: Pricer) -> None:
        self.pricer = pricer
        self._memo: dict[tuple[PricingContext, int], object] = {}
        self.unpriced = 0

    def input_nano(self, ctx: PricingContext, ts_ms: int, r: int, w5: int, w1: int, wo: int,
                   u: int) -> int | None:
        key = (ctx, ts_ms // _DAY_MS)
        unit = self._memo.get(key, _MISSING)
        if unit is _MISSING:
            unit = self.pricer.unit_rates(ctx, ts_ms=ts_ms)
            self._memo[key] = unit
        if unit is not None:
            bucket = unit.bucket_nano  # type: ignore[attr-defined]
            total = 0
            if r:
                total += bucket("cache_read", r)
            if w5:
                total += bucket("cache_write_5m", w5)
            if w1:
                total += bucket("cache_write_1h", w1)
            if wo:
                total += bucket("cache_write_other", wo)
            if u:
                total += bucket("uncached_input", u)
            return total
        usage = UsageBuckets(uncached_input=u, cache_read=r, cache_write_5m=w5, cache_write_1h=w1,
                             cache_write_other=wo,
                             cache_write_other_ttl_s=_OTHER_TTL_S if wo else None)
        priced = self.pricer.price_usage(usage, ctx, ts_ms=ts_ms)
        return priced.figure.nano


_MISSING = object()


# =============================================================================================
# chains
# =============================================================================================


class _Nodes:
    """Interned chain nodes: one node per distinct (parent, tier, hash, salts) — i.e. per distinct
    tier-salted prefix — with its block index, collapsed position, cumulative size units and
    kind; ``lane`` is the lane that created the node and ``xshared`` marks nodes that requests of
    more than one lane visit."""

    __slots__ = ("cum", "index", "intern", "kind", "lane", "parent", "pos", "xshared")

    def __init__(self) -> None:
        self.intern: dict[tuple, int] = {}
        self.parent: list[int] = []
        self.index: list[int] = []
        self.pos: list[int] = []
        self.cum: list[int] = []
        self.kind: list[str | None] = []
        self.lane: list[int] = []
        self.xshared = bytearray()

    def root(self, key: tuple) -> int:
        nid = self.intern.get(key)
        if nid is None:
            nid = len(self.parent)
            self.parent.append(-1)
            self.index.append(-1)
            self.pos.append(-1)
            self.cum.append(0)
            self.kind.append(None)
            self.lane.append(-1)
            self.xshared.append(0)
            self.intern[key] = nid
        return nid

    def back(self, node: int, steps: int) -> int:
        parent = self.parent
        for _ in range(steps):
            node = parent[node]
        return node


def _units(block: BlockRef) -> int:
    est = block.est_tokens
    if est is not None:
        return est
    return (block.n_bytes * 10 + 35) // 36      # ceil(n_bytes / 3.6)


class _Rec:
    """One request with a serving inference, as the engine sees it."""

    __slots__ = ("auto", "base", "blocks", "bp_nodes", "bps", "ctx", "d", "end_ttl", "exempt",
                 "inf", "keys", "lane", "lane_i", "lane_n", "last", "min_cache", "n", "next",
                 "order", "prev", "req", "root", "rules", "salts", "t", "tbase", "te", "total",
                 "units", "values", "vis")

    def __init__(self) -> None:
        self.prev: _Rec | None = None
        self.next: _Rec | None = None
        self.keys: list[str] | None = None


class _Chains:
    """The chain view of one cohort's lanes under a set of chain repairs."""

    __slots__ = ("lane_static", "lane_ttl", "lanes", "nodes", "order", "recs", "static_memo")

    def __init__(self) -> None:
        self.nodes = _Nodes()
        self.recs: list[_Rec] = []           # lane order
        self.order: list[_Rec] = []          # processing (time) order
        self.lanes: list[Lane] = []
        self.lane_ttl: dict[str, int] = {}
        self.lane_static: dict[str, int] = {}     # lane → node ending the lane's common prefix
        self.static_memo: dict[int, int] = {}     # node → deepest cross-lane shared ancestor | -1


def _lane_values(reqs: Sequence[Request], names: Sequence[str], *, pin: bool
                 ) -> list[dict[str, object]]:
    raw = [{n: _param_value(r, n) for n in names} for r in reqs]
    for n in names:
        first = next((v[n] for v in raw if v[n] is not None), None)
        last = first
        for v in raw:
            if v[n] is None:
                v[n] = last
            else:
                last = v[n]
    if pin and raw:
        return [dict(raw[0]) for _ in raw]
    return raw


def _visible_ms(req: Request, rules: CacheRules) -> tuple[int, int, int]:
    """(t, visible_ms, ttl base ms) of the serving attempt."""
    att = req.final_attempt
    t = att.ts_start_ms
    if rules.visible_from == "first_token" and att.ttft_ms is not None:
        vis = t + att.ttft_ms
    elif att.duration_ms is not None:
        vis = t + att.duration_ms
    else:
        vis = t + VISIBLE_DEFAULT_MS
    vis = max(vis, t + 1)
    tbase = t if rules.ttl_measured_from == "request_start" else t + (att.duration_ms or 0)
    return t, vis, tbase


def _normalize_bps(req: Request, n: int, rules: CacheRules, index_map: Mapping[int, int] | None,
                   ) -> tuple[list[tuple[int, int]], bool]:
    """Observed breakpoints as sorted ``(block index, ttl_s)`` (≤ max_breakpoints, automatic
    caching taking one slot at the end) and whether automatic caching is on."""
    by_index: dict[int, int] = {}
    for bp in req.params.breakpoints:
        idx = bp.block_index
        if index_map is not None:
            idx = index_map.get(idx, idx)
        if 0 <= idx < n:
            ttl = BREAKPOINT_TTL_S.get(bp.ttl, _DEFAULT_TTL_S)
            by_index[idx] = max(by_index.get(idx, 0), ttl)
    auto = req.params.automatic_caching is True
    out = sorted(by_index.items())[: rules.max_breakpoints]
    if auto and n and (not out or out[-1][0] != n - 1):
        if len(out) >= rules.max_breakpoints:
            out = out[: rules.max_breakpoints - 1]
        out.append((n - 1, _DEFAULT_TTL_S))
    return out, auto


def _key_fn(repairs: frozenset[str]):  # type: ignore[no-untyped-def]
    vol = REPAIR_VOLATILE in repairs
    srt = REPAIR_SORT_KEYS in repairs
    if not vol and not srt:
        return None

    def key(b: BlockRef) -> str:
        if vol and b.tier == "system" and b.volatile_classes and b.h_norm is not None:
            return b.h_norm
        if srt and b.h_sorted is not None:
            return b.h_sorted
        return b.h

    return key


def _tool_repair(reqs: Sequence[Request], superset: bool
                 ) -> list[tuple[tuple[BlockRef, ...], tuple[int, int, int], dict[int, int], int]]:
    """Per request: repaired blocks, tier_end, observed-index map and added size units."""
    first_seen: dict[str, int] = {}
    all_tools: list[BlockRef] = []
    for r in reqs:
        fp = r.fingerprint
        assert fp is not None
        for b in fp.blocks[: fp.tier_end[0]]:
            key = b.h_sorted or b.h
            if key not in first_seen:
                first_seen[key] = len(first_seen)
                all_tools.append(b)
    out = []
    for r in reqs:
        fp = r.fingerprint
        assert fp is not None
        te0 = fp.tier_end[0]
        tools = fp.blocks[:te0]
        if superset:
            present = {b.h_sorted or b.h for b in tools}
            new_tools = tuple(all_tools)
            added = sum(_units(b) for b in all_tools if (b.h_sorted or b.h) not in present)
        else:
            new_tools = tuple(sorted(tools, key=lambda b: first_seen[b.h_sorted or b.h]))
            added = 0
        shift = len(new_tools) - te0
        blocks = new_tools + fp.blocks[te0:]
        te = (fp.tier_end[0] + shift, fp.tier_end[1] + shift, fp.tier_end[2] + shift)
        imap: dict[int, int] = {}
        if shift:
            for bp in r.params.breakpoints:
                i = bp.block_index
                if i >= te0:
                    imap[i] = i + shift
                elif i == te0 - 1:
                    imap[i] = te[0] - 1
        out.append((blocks, te, imap, added))
    return out


def _build(lanes: Sequence[Lane], *, pricer: Pricer, rules: CacheRulesProvider,
           repairs: frozenset[str] = frozenset()) -> _Chains:
    """Pass A: chain nodes of every serving request of the (fingerprinted) cohort lanes."""
    ch = _Chains()
    nodes = ch.nodes
    intern = nodes.intern
    n_parent, n_index, n_pos, n_cum, n_kind, n_lane, n_xshared = (
        nodes.parent, nodes.index, nodes.pos, nodes.cum, nodes.kind, nodes.lane, nodes.xshared)
    keyfn = _key_fn(repairs)
    pin = REPAIR_PIN_PARAMS in repairs
    min_memo: dict[tuple[PricingContext, int], int] = {}
    ch.lanes = sorted(lanes, key=lambda ln: ln.lane_key)
    for lane_no, lane in enumerate(ch.lanes):
        reqs = _serving_requests(lane)
        if not reqs:
            continue
        rules_list = [_rules_for(rules, r) for r in reqs]
        names: list[str] = []
        for rl in dict.fromkeys(rules_list):
            names.extend(n for n in _all_salt_names(rl) if n not in names)
        values = _lane_values(reqs, names, pin=pin)
        tool_fix = None
        if REPAIR_TOOL_SUPERSET in repairs:
            tool_fix = _tool_repair(reqs, superset=True)
        elif REPAIR_TOOL_ORDER in repairs:
            tool_fix = _tool_repair(reqs, superset=False)
        scope = lane.cache_scope_key
        if scope == "unknown":
            scope = f"unknown:{lane.lane_key}"     # an unknown scope is never assumed shared
        prev: _Rec | None = None
        ttl_counts: Counter[int] = Counter()
        lane_recs: list[_Rec] = []
        for i, req in enumerate(reqs):
            rl = rules_list[i]
            fp = req.fingerprint
            assert fp is not None
            inf = req.serving_inference
            assert inf is not None
            rec = _Rec()
            rec.req, rec.inf, rec.ctx, rec.rules = req, inf, inf.pricing, rl
            rec.lane, rec.lane_i, rec.lane_n = lane, i, len(reqs)
            rec.t, rec.vis, rec.tbase = _visible_ms(req, rl)
            total = inf.usage.total_input
            if tool_fix is not None:
                blocks, te, imap, added = tool_fix[i]
            else:
                blocks, te, imap, added = fp.blocks, fp.tier_end, None, 0
            n = len(blocks)
            rec.blocks, rec.te, rec.n = blocks, te, n
            rec.values = values[i]
            rec.exempt = _effort_exempt(req)
            rec.salts = _salts(req, rl, values[i], rec.exempt)
            model = req.model
            rec.root = nodes.root(("root", fp.key_id, scope, model, rl.collapse_tool_runs))
            # --- common prefix with the lane predecessor (chain reuse) ---
            d = 0
            if prev is not None and prev.root == rec.root and n and prev.n:
                pb = prev.blocks
                m = min(len(pb), n)
                if pb[m - 1] is blocks[m - 1] and pb[:m] == blocks[:m]:
                    d = m                      # delta-shared blocks: a C-speed identity compare
                else:
                    # equal but distinct blocks: compare hash-key lists (C speed), keeping only
                    # the newest request's keys; tiers are enforced by the tier_end clamp below
                    kf = keyfn if keyfn is not None else _H
                    pk = prev.keys if prev.keys is not None else [kf(b) for b in pb]
                    ck = [kf(b) for b in blocks]
                    if pk[:m] == ck[:m]:
                        d = m
                    else:
                        k = 0
                        while k < m and pk[k] == ck[k]:
                            k += 1
                        d = k
                    rec.keys = ck
                pte = prev.te
                for t in (1, 2):
                    if pte[t - 1] != te[t - 1]:
                        d = min(d, pte[t - 1], te[t - 1])
                # tier salts compared under *this* request's effort exemption (core.transitions)
                if prev.exempt == rec.exempt:
                    ps, cs = prev.salts, rec.salts
                else:
                    ps = _salts(prev.req, rl, prev.values, rec.exempt)
                    cs = _salts(req, rl, rec.values, rec.exempt)
                if ps[0] != cs[0]:
                    d = 0
                elif ps[1] != cs[1]:
                    d = min(d, te[0])
                elif ps[2] != cs[2]:
                    d = min(d, te[1])
            if prev is not None:
                prev.keys = None               # only the newest request's key list is kept
            rec.d = d
            if d > 0:
                assert prev is not None
                base = nodes.back(prev.last, prev.n - d)
            else:
                base = rec.root
            rec.base = base
            # --- new nodes ---
            starts = _tier_starts(te)
            salts = rec.salts
            collapse = rl.collapse_tool_runs
            parent = base
            new_ids: list[int] = []
            for k in range(d, n):
                b = blocks[k]
                part = _salt_part(k, starts, salts)
                h = keyfn(b) if keyfn is not None else b.h
                key = (parent, b.tier, h, part)
                nid = intern.get(key)
                if nid is None:
                    nid = len(n_parent)
                    kind = b.kind
                    ppos = n_pos[parent]
                    if ppos < 0:
                        pos = 0
                    elif collapse and kind in _TOOL_RUN_KINDS and n_kind[parent] == kind:
                        pos = ppos
                    else:
                        pos = ppos + 1
                    n_parent.append(parent)
                    n_index.append(k)
                    n_pos.append(pos)
                    n_cum.append(n_cum[parent] + _units(b))
                    n_kind.append(kind)
                    n_lane.append(lane_no)
                    n_xshared.append(0)
                    intern[key] = nid
                elif n_lane[nid] != lane_no:
                    n_xshared[nid] = 1
                new_ids.append(nid)
                parent = nid
            rec.last = parent
            rec.units = n_cum[parent]
            if added and rec.units > added:
                orig = rec.units - added
                total = total + (added * total + orig // 2) // orig
            rec.total = total
            # --- observed breakpoints → nodes ---
            bps, auto = _normalize_bps(req, n, rl, imap)
            rec.auto = auto
            bp_nodes: dict[int, int] = {}
            resolved: list[tuple[int, int, int]] = []
            for idx, ttl in bps:
                if idx >= d:
                    node = new_ids[idx - d]
                elif prev is not None and idx in prev.bp_nodes:
                    node = prev.bp_nodes[idx]
                else:
                    node = nodes.back(base, d - 1 - idx)
                bp_nodes[idx] = node
                resolved.append((node, idx, ttl))
                ttl_counts[ttl] += 1
            rec.bps = resolved
            rec.bp_nodes = bp_nodes
            rec.end_ttl = resolved[-1][2] if resolved else None
            mkey = (inf.pricing, rec.t // _DAY_MS)
            mc = min_memo.get(mkey)
            if mc is None:
                got = pricer.min_cacheable_tokens(inf.pricing, ts_ms=rec.t)
                mc = got if got is not None else 0
                min_memo[mkey] = mc
            rec.min_cache = mc
            rec.prev = prev
            if prev is not None:
                prev.next = rec
            lane_recs.append(rec)
            prev = rec
        ch.lane_ttl[lane.lane_key] = (max(ttl_counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
                                      if ttl_counts else _DEFAULT_TTL_S)
        if len(lane_recs) >= 2:
            lcp = min(r.d for r in lane_recs[1:])
            if lcp > 0:
                first = lane_recs[0]
                ch.lane_static[lane.lane_key] = nodes.back(first.last, first.n - lcp)
        ch.recs.extend(lane_recs)
    ch.order = sorted(ch.recs, key=lambda r: (r.t, r.lane.lane_key, r.req.seq,
                                              r.req.request_id))
    for i, rec in enumerate(ch.order):
        rec.order = i
    return ch


def _static_node(ch: _Chains, rec: _Rec) -> int | None:
    """The end of *rec*'s static prefix: its deepest block whose prefix a request of another lane
    of the cohort also sends, else the end of its lane's common prefix (the prefix every request
    of the lane re-sends), else None. Memoized per node: O(new blocks) over a replay."""
    nodes = ch.nodes
    memo = ch.static_memo
    index, xshared, parent = nodes.index, nodes.xshared, nodes.parent
    path: list[int] = []
    node = rec.last
    found = -1
    while node >= 0 and index[node] >= 0:
        got = memo.get(node)
        if got is not None:
            found = got
            break
        if xshared[node]:
            found = node
            break
        path.append(node)
        node = parent[node]
    for p in path:
        memo[p] = found
    if found >= 0:
        memo[found] = found
        return found
    return ch.lane_static.get(rec.lane.lane_key)


# =============================================================================================
# simulation
# =============================================================================================


class _Entry:
    __slots__ = ("bp_index", "bp_ord", "expires", "overflow", "reads", "tokens", "ttl_ms",
                 "visible", "writer", "writers")

    def __init__(self, writer: _Rec, bp_index: int, bp_ord: int, visible: int, expires: int,
                 ttl_ms: int, tokens: int) -> None:
        self.writer = writer
        self.bp_index = bp_index
        self.bp_ord = bp_ord                 # position among the writer's breakpoints
        self.visible = visible
        self.expires = expires
        self.ttl_ms = ttl_ms
        self.tokens = tokens
        self.writers = 1
        self.reads = 0
        self.overflow = False


class _Pred:
    """Predicted input usage of one request and the events behind it."""

    __slots__ = ("bps", "fanout", "hit_index", "over_tokens", "r", "ttl_disorder", "u", "w1",
                 "w5", "wo", "written")

    def __init__(self) -> None:
        self.r = self.w5 = self.w1 = self.wo = self.u = 0
        self.hit_index = -1
        self.bps: list[tuple[int, int, int]] = []
        self.written: list[tuple[int, int, _Entry]] = []   # (bp index, segment tokens, entry)
        self.fanout: list[_Entry] = []
        self.over_tokens = 0
        self.ttl_disorder = False


class _Sim:
    __slots__ = ("preds", "unread")

    def __init__(self) -> None:
        self.preds: dict[str, _Pred] = {}
        self.unread: list[_Entry] = []


def _placement_bps(ch: _Chains, rec: _Rec, placement: str, *, add_end: bool,
                   drop: frozenset[tuple[str, int]]) -> list[tuple[int, int, int]]:
    """Breakpoints ``(node, block index, ttl_s)`` of *rec* under *placement* (sorted, deduped,
    ≤ max_breakpoints)."""
    if rec.n == 0:
        return []
    if placement == "observed":
        bps = list(rec.bps)
        if drop:
            rid = rec.req.request_id
            bps = [bp for i, bp in enumerate(bps) if (rid, i) not in drop]
        if add_end and not bps and not rec.auto:
            bps = [(rec.last, rec.n - 1, rec.end_ttl or _DEFAULT_TTL_S)]
        return bps
    ttl = rec.end_ttl or ch.lane_ttl.get(rec.lane.lane_key, _DEFAULT_TTL_S)
    nodes = ch.nodes
    end = (rec.last, rec.n - 1, ttl)
    if placement == "end":
        return [end]
    chosen: dict[int, tuple[int, int, int]] = {end[1]: end}
    static = _static_node(ch, rec)
    static_idx = -1
    if static is not None and nodes.index[static] < rec.n - 1:
        static_idx = nodes.index[static]
        chosen[static_idx] = (static, static_idx, ttl)
    if placement == "every_15":
        limit = rec.rules.max_breakpoints
        slots = limit - len(chosen)
        pos, index, parent = nodes.pos, nodes.index, nodes.parent
        end_pos = pos[rec.last]
        node = parent[rec.last]
        taken: set[int] = set()
        while slots > 0 and node >= 0 and index[node] > static_idx:
            p = pos[node]
            if p < INTERMEDIATE_SPACING:
                break
            if p < end_pos and p % INTERMEDIATE_SPACING == 0 and p not in taken:
                taken.add(p)
                chosen[index[node]] = (node, index[node], ttl)
                slots -= 1
            node = parent[node]
    out = [chosen[k] for k in sorted(chosen)]
    return out[: rec.rules.max_breakpoints]


def _simulate(ch: _Chains, placement: str = "observed", *, add_end: bool = False,
              drop: frozenset[tuple[str, int]] = frozenset(), stagger: bool = False) -> _Sim:
    """Pass B: replay the cohort in time order under the documented rules."""
    nodes = ch.nodes
    n_parent, n_index, n_pos, n_cum = nodes.parent, nodes.index, nodes.pos, nodes.cum
    entries: dict[int, _Entry] = {}
    sim = _Sim()
    used: dict[int, list[tuple[int, int, int]]] = {}
    for rec in ch.order:
        pred = _Pred()
        sim.preds[rec.req.request_id] = pred
        bps = _placement_bps(ch, rec, placement, add_end=add_end, drop=drop)
        pred.bps = bps
        used[rec.order] = bps
        total = rec.total
        if not bps or total <= 0:
            pred.u = max(total, 0)
            continue
        t = rec.t
        lookback = rec.rules.lookback_positions
        hit_node = -1
        hit_idx = -1
        hit_entry: _Entry | None = None
        for bnode, _bidx, _ttl in bps:
            pmin = n_pos[bnode] - (lookback - 1) if lookback is not None else None
            node = bnode
            while node >= 0 and n_index[node] >= 0:
                if pmin is not None and n_pos[node] < pmin:
                    break
                e = entries.get(node)
                if e is not None and t <= e.expires and (stagger or e.visible <= t):
                    e.reads += 1
                    refreshed = rec.tbase + e.ttl_ms
                    if refreshed > e.expires:
                        e.expires = refreshed
                    if n_index[node] > hit_idx:
                        hit_idx, hit_node, hit_entry = n_index[node], node, e
                    break
                node = n_parent[node]
        # --- sizes ---
        units = rec.units
        if hit_entry is not None:
            if hit_idx >= rec.n - 1:
                reads = total
            else:
                reads = min(hit_entry.tokens, total)
            hit_cum = n_cum[hit_node]
            rem_units = units - hit_cum
            rem_tok = total - reads
        else:
            reads = 0
            hit_cum = 0
            rem_units = units
            rem_tok = total

        def tokens_at(node: int, _hc: int = hit_cum, _ru: int = rem_units, _rt: int = rem_tok,
                      _r: int = reads) -> int:
            if _ru <= 0:
                return _r
            num = (n_cum[node] - _hc) * _rt
            return _r + (2 * num + _ru) // (2 * _ru)

        pred.hit_index = hit_idx
        pred.r = reads
        # --- lookback overflow: a live entry of the lane predecessor out of every window ---
        prev = rec.prev
        if prev is not None and rec.d > 0:
            deepest_bp = bps[-1][1]
            for pnode, pidx, _pttl in used.get(prev.order, ()):
                if pidx >= rec.d or pidx <= hit_idx or pidx > deepest_bp:
                    continue
                e = entries.get(pnode)
                if e is not None and t <= e.expires and (stagger or e.visible <= t):
                    e.overflow = True
                    pred.over_tokens = max(pred.over_tokens, min(e.tokens, total))
        # --- writes ---
        boundary = reads
        last_ttl = None
        min_cache = rec.min_cache
        for ordinal, (bnode, bidx, ttl) in enumerate(bps):
            if bidx <= hit_idx:
                continue
            tok = tokens_at(bnode)
            if tok < min_cache or tok <= 0:
                continue
            seg = tok - boundary
            if seg > 0:
                if ttl == 3600:
                    pred.w1 += seg
                elif ttl == _OTHER_TTL_S:
                    pred.wo += seg
                else:
                    pred.w5 += seg
                boundary = tok
            if last_ttl is not None and ttl > last_ttl:
                pred.ttl_disorder = True
            last_ttl = ttl
            ttl_ms = ttl * 1000
            e = entries.get(bnode)
            if e is not None and t <= e.expires and not (stagger or e.visible <= t):
                e.writers += 1                       # concurrent sibling: both pay the write
                e.visible = min(e.visible, rec.vis)
                e.expires = max(e.expires, rec.tbase + ttl_ms)
                pred.fanout.append(e)
                pred.written.append((bidx, max(seg, 0), e))
                continue
            if e is not None and e.reads == 0:
                sim.unread.append(e)
            ne = _Entry(rec, bidx, ordinal, rec.vis, rec.tbase + ttl_ms, ttl_ms, tok)
            entries[bnode] = ne
            pred.written.append((bidx, max(seg, 0), ne))
        pred.u = total - reads - pred.w5 - pred.w1 - pred.wo
    sim.unread.extend(e for e in entries.values() if e.reads == 0)
    return sim


def _wasted_unread(sim: _Sim) -> list[_Entry]:
    """Entries written and never read that a dropped breakpoint would have saved — the
    ``write-never-read`` breaker's events and exactly what ``block:drop_unread`` drops, so its
    recoverable never counts what another lever recovers. Excluded: concurrent re-writes (fan-out,
    ``block:stagger``), entries out of the lookback window (lookback overflow, ``every_15``),
    writes billing does not confirm, the final write of a multi-request lane (unavoidable), entries
    the lane's next request diverged before (a divergence breaker's repair makes them read) and
    entries that expired before the next request (idle gaps: the usage-level TTL levers)."""
    out: list[_Entry] = []
    for e in sim.unread:
        w = e.writer
        if e.writers > 1 or e.overflow or w.inf.usage.cache_write <= 0:
            continue
        if w.lane_n > 1 and w.lane_i == w.lane_n - 1:
            continue
        succ = w.next
        if succ is not None and (succ.d <= e.bp_index or succ.t > e.expires):
            continue
        out.append(e)
    return out


def _drop_set(sim: _Sim) -> frozenset[tuple[str, int]]:
    """Observed breakpoints to drop under ``block:drop_unread``, as ``(request id, ordinal among
    the request's observed breakpoints)`` (stable under the index shifts of tool repairs): the
    entries of :func:`_wasted_unread`."""
    return frozenset((e.writer.req.request_id, e.bp_ord) for e in _wasted_unread(sim))


# =============================================================================================
# policy parts
# =============================================================================================


def _block_parts(policy: Policy) -> tuple[str, frozenset[str], tuple[str, ...]]:
    """(placement, block repairs, ignored non-block clause names)."""
    placement = policy.breakpoint_policy or "observed"
    if placement not in PLACEMENTS:
        raise UsageError(f"unknown breakpoint policy {placement!r}")
    repairs = []
    ignored: list[str] = []
    for rep in policy.repairs:
        if rep.startswith("block:"):
            if rep not in BLOCK_REPAIRS:
                raise UsageError(f"unknown block repair {rep!r}")
            repairs.append(rep)
        elif "repair" not in ignored:
            ignored.append("repair")
    for name, active in (("ttl", bool(policy.ttl)), ("keepalive", policy.keepalive is not None),
                         ("compact-window", policy.compaction_window is not None),
                         ("cold-resume", policy.cold_resume is not None),
                         ("model", bool(policy.model_remap)), ("effort", bool(policy.effort)),
                         ("fast", policy.fast_off), ("geo", policy.geo_global),
                         ("regional", policy.regional_to_global),
                         ("batch", policy.batch is not None)):
        if active:
            ignored.append(name)
    return placement, frozenset(repairs), tuple(ignored)


def _cohorts(lanes: Iterable[Lane]) -> list[tuple[tuple[str, str], list[Lane]]]:
    groups: dict[tuple[str, str], list[Lane]] = {}
    for lane in lanes:
        groups.setdefault((lane.team or "", lane.kind.value), []).append(lane)
    return sorted(groups.items(), key=lambda kv: kv[0])


def _pred_cost(costs: _Costs, rec: _Rec, p: _Pred) -> int | None:
    return costs.input_nano(rec.ctx, rec.t, p.r, p.w5, p.w1, p.wo, p.u)


def read_agreement(outcomes: Iterable[ReplayRequestOutcome], lanes: Iterable[Lane]
                   ) -> Decimal | None:
    """``Σ predicted reads / Σ billed reads`` over the requests of *outcomes* (SPEC §9.7 #4), or
    None when the billed reads are 0. An agreement metric only: disagreement without a hash
    divergence is never a breaker."""
    billed_by_id = {}
    for lane in lanes:
        for req in lane.requests:
            inf = req.serving_inference
            if inf is not None:
                billed_by_id[req.request_id] = inf.usage.cache_read
    pred = billed = 0
    for o in outcomes:
        if o.request_id in billed_by_id:
            pred += o.usage.cache_read
            billed += billed_by_id[o.request_id]
    if billed <= 0:
        return None
    return RATIO_CTX.divide(Decimal(pred), Decimal(billed))


# =============================================================================================
# the replayer
# =============================================================================================


class BlockReplayer:
    """``Replayer`` for ``Policy.breakpoint_policy`` and ``block:`` repairs (SPEC §9.7).

    Other policy clauses are not modeled at block level (they are usage-level transforms) and are
    listed in ``ReplayResult.assumptions``; lanes without fingerprints are skipped with a reason.
    The mode is always ``documented`` (the model gate calibrates the usage level only), and every
    counterfactual is ESTIMATED and UNCALIBRATED.
    """

    def replay(self, lanes: Sequence[Lane], policy: Policy, *, mode: str, pricer: Pricer,
               rules: CacheRulesProvider | None, calibration: CalibrationReport | None,
               static_prefix_floor: Mapping[tuple[str, str], int] | None = None,
               keep_outcomes: bool = False) -> ReplayResult:
        """Replay *lanes* under *policy* (minimal change: billed − (model(observed) −
        model(policy)) per request). Mixed billing classes raise ``UsageError``."""
        classes = {lane.billing_class for lane in lanes if lane.requests}
        if len(classes) > 1:
            raise UsageError("replay: lanes of one billing class only (billed | allowance)")
        if mode not in ("documented", "calibrated"):
            raise UsageError(f"unknown replay mode {mode!r}")
        rules = rules if rules is not None else RulesTable()
        placement, repairs, ignored = _block_parts(policy)
        basis = Basis.LIST_EQUIVALENT if classes == {"allowance"} else (
            pricer.basis if pricer.basis in (Basis.LIST, Basis.CONTRACT) else Basis.LIST)
        active = placement != "observed" or bool(repairs)
        assumptions: list[str] = []
        if mode == "calibrated":
            assumptions.append("block replay has no calibrated mode; documented rules used")
        if ignored:
            assumptions.append("block replay ignores usage-level clauses: " + ", ".join(ignored))
        ordered = sorted(lanes, key=lambda ln: ln.lane_key)
        skipped: list[tuple[str, str]] = []
        deltas: dict[str, int] = {}
        pol_usage: dict[str, UsageBuckets] = {}
        obs_usage: dict[str, UsageBuckets] = {}
        if active:
            replayable = []
            for lane in ordered:
                reason = lane_skip_reason(lane)
                if reason is None:
                    replayable.append(lane)
                elif lane.requests:
                    skipped.append((lane.lane_key, reason))
            costs = _Costs(pricer)
            stats = _Stats()
            for _key, members in _cohorts(replayable):
                self._cohort_deltas(members, placement, repairs, pricer, rules, costs, deltas,
                                    obs_usage, pol_usage, stats)
            assumptions.extend(_assumption_lines(placement, repairs, stats, costs))
        baseline = zero(basis)
        per_lane: list[tuple[str, int]] = []
        outcomes: list[ReplayRequestOutcome] = []
        total_delta = 0
        n_requests = 0
        for lane in ordered:
            lane_point = 0
            lane_priced = True
            for req in lane.requests:
                n_requests += 1
                fig = _request_figure(pricer, req, basis)
                baseline = add(baseline, fig)
                delta = deltas.get(req.request_id, 0)
                total_delta += delta
                if fig.nano is None:
                    lane_priced = False
                else:
                    lane_point += fig.nano - delta
                if keep_outcomes:
                    outcomes.append(_outcome(req, fig, delta, obs_usage, pol_usage))
            per_lane.append((lane.lane_key, lane_point if lane_priced else 0))
        if not active:
            cost = baseline
            saving = zero(basis) if baseline.nano is not None else unpriced(
                "baseline unpriced", basis)
        else:
            saving = estimated(total_delta, basis, calibration=Calibration.UNCALIBRATED,
                               note="block-level replay (documented cache rules)")
            cost = sub(baseline, saving)
        return ReplayResult(
            policy=policy, mode="documented", baseline=baseline, cost=cost, saving=saving,
            per_lane=tuple(per_lane), outcomes=tuple(outcomes) if keep_outcomes else None,
            assumptions=tuple(assumptions), calibration=Calibration.UNCALIBRATED
            if active else Calibration.NA, added_calls=0, keepalive_pings=0,
            lanes_skipped=tuple(skipped), n_lanes=len(lanes), n_requests=n_requests)

    @staticmethod
    def _cohort_deltas(members: Sequence[Lane], placement: str, repairs: frozenset[str],
                       pricer: Pricer, rules: CacheRulesProvider, costs: _Costs,
                       deltas: dict[str, int], obs_usage: dict[str, UsageBuckets],
                       pol_usage: dict[str, UsageBuckets], stats: _Stats) -> None:
        obs_ch = _build(members, pricer=pricer, rules=rules)
        obs = _simulate(obs_ch)
        chain_repairs = repairs & _CHAIN_REPAIRS
        pol_ch = _build(members, pricer=pricer, rules=rules, repairs=chain_repairs) \
            if chain_repairs else obs_ch
        drop = _drop_set(obs) if REPAIR_DROP_UNREAD in repairs else frozenset()
        pol = _simulate(pol_ch, placement, add_end=REPAIR_ADD_END in repairs, drop=drop,
                        stagger=REPAIR_STAGGER in repairs)
        pol_recs = {r.req.request_id: r for r in pol_ch.recs}
        for rec in obs_ch.recs:
            rid = rec.req.request_id
            po, pp = obs.preds[rid], pol.preds[rid]
            stats.billed_reads += rec.inf.usage.cache_read
            stats.pred_reads += po.r
            stats.ttl_disorder += pp.ttl_disorder
            if (po.r, po.w5, po.w1, po.wo, po.u) == (pp.r, pp.w5, pp.w1, pp.wo, pp.u):
                continue
            prec = pol_recs[rid]
            a = _pred_cost(costs, rec, po)
            b = _pred_cost(costs, prec, pp)
            if a is None or b is None:
                costs.unpriced += 1
                continue
            deltas[rid] = a - b
            obs_usage[rid] = _usage_of(rec.inf.usage, po)
            pol_usage[rid] = _usage_of(rec.inf.usage, pp)

    def predict(self, lanes: Sequence[Lane], *, pricer: Pricer,
                rules: CacheRulesProvider | None = None, placement: str = "observed"
                ) -> list[ReplayRequestOutcome]:
        """The model's own predicted usage and cost per request under *placement* (no minimal
        change): input buckets predicted, output and server tools as billed, cost = every billable
        inference with the serving one repriced on the prediction. Requests of lanes that cannot be
        block-replayed are omitted; order: lanes by key, requests in lane order."""
        if placement not in PLACEMENTS:
            raise UsageError(f"unknown breakpoint placement {placement!r}")
        rules = rules if rules is not None else RulesTable()
        replayable = [lane for lane in lanes if lane_skip_reason(lane) is None]
        by_id: dict[str, tuple[_Rec, _Pred]] = {}
        for _key, members in _cohorts(replayable):
            ch = _build(members, pricer=pricer, rules=rules)
            sim = _simulate(ch, placement)
            for rec in ch.recs:
                by_id[rec.req.request_id] = (rec, sim.preds[rec.req.request_id])
        out: list[ReplayRequestOutcome] = []
        for lane in sorted(replayable, key=lambda ln: ln.lane_key):
            basis = Basis.LIST_EQUIVALENT if lane.billing_class == "allowance" else (
                pricer.basis if pricer.basis in (Basis.LIST, Basis.CONTRACT) else Basis.LIST)
            for req in lane.requests:
                got = by_id.get(req.request_id)
                if got is None:
                    fig = _request_figure(pricer, req, basis)
                    si = req.serving_inference
                    out.append(ReplayRequestOutcome(
                        request_id=req.request_id, usage=si.usage if si else UsageBuckets(),
                        extra=(), cost_nano=fig.nano, low_nano=fig.low_nano,
                        high_nano=fig.high_nano, changed=False))
                    continue
                rec, pred = got
                usage = _usage_of(rec.inf.usage, pred)
                fig = _predicted_figure(pricer, req, rec.inf, usage, basis)
                billed = rec.inf.usage
                changed = (usage.cache_read, usage.cache_write, usage.uncached_input) != (
                    billed.cache_read, billed.cache_write, billed.uncached_input) or (
                    usage.cache_write_5m, usage.cache_write_1h) != (
                    billed.cache_write_5m, billed.cache_write_1h)
                out.append(ReplayRequestOutcome(
                    request_id=req.request_id, usage=usage, extra=(), cost_nano=fig.nano,
                    low_nano=fig.low_nano, high_nano=fig.high_nano, changed=changed))
        return out


class _Stats:
    __slots__ = ("billed_reads", "pred_reads", "ttl_disorder")

    def __init__(self) -> None:
        self.billed_reads = 0
        self.pred_reads = 0
        self.ttl_disorder = 0


def _assumption_lines(placement: str, repairs: frozenset[str], stats: _Stats,
                      costs: _Costs) -> list[str]:
    lines = [
        "block replay: documented cache rules (tier-salted prefix chain, <= 4 breakpoints, "
        "20-position lookback with collapsed tool runs, first-token visibility, TTL from request "
        "start with refresh on read)",
        f"breakpoint placement: {placement}; block repairs: "
        + (", ".join(sorted(repairs)) if repairs else "none"),
        "minimal change: each request costs billed - (model(observed) - model(policy))",
        "block est_tokens rescaled to billed total_input; a cache hit reads its entry's size",
        "cache sharing modeled within each (team, lane_kind) cohort",
    ]
    if stats.billed_reads > 0:
        ratio = RATIO_CTX.divide(Decimal(stats.pred_reads), Decimal(stats.billed_reads))
        lines.append(f"observed-model read agreement {ratio.quantize(Decimal('0.001'))} "
                     "(predicted / billed reads)")
    if stats.ttl_disorder:
        lines.append(f"{stats.ttl_disorder} requests with breakpoint TTLs not longest-first")
    if costs.unpriced:
        lines.append(f"{costs.unpriced} requests unpriceable at block level: unchanged")
    return lines


def _usage_of(billed: UsageBuckets, p: _Pred) -> UsageBuckets:
    return UsageBuckets(
        uncached_input=p.u, cache_read=p.r, cache_write_5m=p.w5, cache_write_1h=p.w1,
        cache_write_other=p.wo, cache_write_other_ttl_s=_OTHER_TTL_S if p.wo else None,
        output=billed.output, output_reasoning=billed.output_reasoning,
        web_search_requests=billed.web_search_requests,
        web_fetch_requests=billed.web_fetch_requests)


def _outcome(req: Request, fig: Figure, delta: int, obs_usage: Mapping[str, UsageBuckets],
             pol_usage: Mapping[str, UsageBuckets]) -> ReplayRequestOutcome:
    si = req.serving_inference
    billed = si.usage if si is not None else UsageBuckets()
    rid = req.request_id
    usage = billed
    changed = False
    if rid in pol_usage:
        o, p = obs_usage[rid], pol_usage[rid]
        shifted = {
            "uncached_input": billed.uncached_input + p.uncached_input - o.uncached_input,
            "cache_read": billed.cache_read + p.cache_read - o.cache_read,
            "cache_write_5m": billed.cache_write_5m + p.cache_write_5m - o.cache_write_5m,
            "cache_write_1h": billed.cache_write_1h + p.cache_write_1h - o.cache_write_1h,
            "cache_write_other": billed.cache_write_other + p.cache_write_other
            - o.cache_write_other,
        }
        if billed.cache_write_other and (p.cache_write_other or o.cache_write_other) and \
                billed.cache_write_other_ttl_s not in (None, _OTHER_TTL_S):
            shifted = {}
        if shifted and all(v >= 0 for v in shifted.values()):
            usage = UsageBuckets(
                **shifted, cache_write_unknown=billed.cache_write_unknown,
                cache_write_other_ttl_s=(billed.cache_write_other_ttl_s or _OTHER_TTL_S)
                if shifted["cache_write_other"] else None,
                output=billed.output, output_reasoning=billed.output_reasoning,
                web_search_requests=billed.web_search_requests,
                web_fetch_requests=billed.web_fetch_requests)
        else:
            usage = p
        changed = True
    point = None if fig.nano is None else fig.nano - delta
    low = None if fig.low_nano is None else fig.low_nano - delta
    high = None if fig.high_nano is None else fig.high_nano - delta
    return ReplayRequestOutcome(request_id=rid, usage=usage, extra=(), cost_nano=point,
                                low_nano=low, high_nano=high, changed=changed or delta != 0)


def _predicted_figure(pricer: Pricer, req: Request, serving: Inference, usage: UsageBuckets,
                      basis: Basis) -> Figure:
    fig = zero(basis)
    for att in req.attempts:
        for inf in att.inferences:
            if inf.billable is False:
                continue
            if inf is serving:
                priced = pricer.price_usage(usage, inf.pricing, ts_ms=att.ts_start_ms,
                                            billable=inf.billable, usage_source=inf.usage_source,
                                            output_upper=inf.output_upper)
            else:
                priced = pricer.price_inference(inf, ts_ms=att.ts_start_ms)
            fig = add(fig, priced.figure)
    return fig
