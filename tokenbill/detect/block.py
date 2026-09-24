"""Block-level cache breakers (SPEC §10.4; package BLOCK; registry id ``block.breakers``).

Runs on fingerprinted lanes only (``requires = {"blocks"}``; lanes without fingerprints on every
serving request are ignored) and emits eleven kinds, one finding per ``(team, lane_kind[,
billing_class])`` cohort and kind:

- ``volatile-system``: the system tier differs, ``h_norm`` equal, the changed blocks carry volatile
  classes; repair ``block:volatile``.
- ``system-edit``: the system tier differs, not purely volatile; no repair (cost only).
- ``serialization-churn``: ``h`` differs, ``h_sorted`` equal; repair ``block:sort_keys``.
- ``tool-churn`` (order / subset / definition): tools tier reordered / added-removed / changed;
  repair ``block:tool_order`` or ``block:tool_superset`` (definition changes: none).
- ``history-rewrite``: the messages diverge below the previous message count; no repair.
- ``param-churn``: a tier salt changes (effort and thinking only without the D28 exemption);
  repair ``block:pin_params``.
- ``missing-breakpoint``: a shared prefix ≥ the minimum, no markers (explicit or assumed), no
  automatic caching, billed reads = writes = 0; repair ``block:add_end``.
- ``lookback-overflow``: a live entry beyond every breakpoint's 20-position window, confirmed by
  billed reads; repair ``breakpoints=every_15``.
- ``breakpoint-placement``: a canonical placement is cheaper than the observed one on lanes with
  markers and no other breaker; repair: the cheapest placement.
- ``write-never-read``: a single-writer entry never read (one-shot traffic or a shadowed
  breakpoint), confirmed by billed writes; repair ``block:drop_unread``.
- ``fanout``: one prefix written by ≥ 2 requests before its first token, confirmed by billed writes;
  repair ``block:stagger`` (an upper bound).

A divergence is a breaker only when it falls inside the previous request's cached prefix (at or
before its last breakpoint) and no compaction, clear or context edit happened in between; moved
markers never diverge (hashes exclude ``cache_control``); model switches are usage-level
(``cache.switch-churn``). Billed-versus-predicted disagreement without a divergence is never a
breaker (§9.7 #5).

``recoverable`` = ``replay(observed) − replay(repair)`` from :class:`BlockReplayer` (ESTIMATED,
UNCALIBRATED), floored at 0 with the v0.1 wording when the model prices the repair above the
observed placement, None for kinds without a mechanical repair. ``cost_observed`` is billed
arithmetic on the affected requests (EXACT where every line is): their uncached + cache-write input
for divergence kinds, lookback overflow and missing breakpoints; all their input for placement;
their cache writes for write-never-read and fan-out. The ``min_usd`` threshold applies to the
recoverable point (to the observed cost for kinds without a repair). Findings never carry
content: only counts, tiers, block indexes, token sizes and pseudonymous ids.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping, Sequence

from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.catalog import levers_for_kind
from tokenbill.core.findings import (
    build_finding,
    cohort_key,
    make_scope,
    min_usd_nano,
    sum_figures,
    top_evidence,
)
from tokenbill.core.labels import Basis, Calibration, Figure, estimated
from tokenbill.core.policy import parse_policy
from tokenbill.core.protocols import CacheRulesProvider, Pricer
from tokenbill.core.records import Lane, LaneEventKind, UsageBuckets
from tokenbill.core.transitions import is_miss_event
from tokenbill.core.types import AnalysisContext, EvidenceItem, Finding, Fix
from tokenbill.sim.block_replay import (
    CANONICAL_PLACEMENTS,
    NO_RECOVERY_NOTE,
    REPAIR_ADD_END,
    REPAIR_DROP_UNREAD,
    REPAIR_PIN_PARAMS,
    REPAIR_SORT_KEYS,
    REPAIR_STAGGER,
    REPAIR_TOOL_ORDER,
    REPAIR_TOOL_SUPERSET,
    REPAIR_VOLATILE,
    BlockReplayer,
    _build,
    _Rec,
    _simulate,
    first_divergence,
    lane_skip_reason,
    salt_diff,
)

__all__ = ["KINDS", "BlockBreakers"]

KINDS = ("volatile-system", "system-edit", "serialization-churn", "tool-churn",
         "history-rewrite", "param-churn", "missing-breakpoint", "lookback-overflow",
         "breakpoint-placement", "write-never-read", "fanout")

_CACHING_DOC = "https://platform.claude.com/docs/en/build-with-claude/prompt-caching"
_RESETS = frozenset({LaneEventKind.COMPACTION, LaneEventKind.CLEAR, LaneEventKind.CONTEXT_EDIT})
_DIVERGENCE_KINDS = frozenset({"volatile-system", "system-edit", "serialization-churn",
                               "tool-churn", "history-rewrite", "param-churn"})
#: (tier, cause) → breaker kind; ``*`` matches any tier. Causes not listed (model, key,
#: compaction, context-edit, thinking-dropped) are expected rebuilds or usage-level switches.
_CAUSE_KIND: Mapping[tuple[str, str], str] = {
    ("system", "volatile"): "volatile-system",
    ("system", "system-edit"): "system-edit",
    ("*", "serialization"): "serialization-churn",
    ("tools", "tool-order"): "tool-churn",
    ("tools", "tool-subset"): "tool-churn",
    ("tools", "tool-definition"): "tool-churn",
    ("tools", "volatile"): "tool-churn",
    ("messages", "history-rewrite"): "history-rewrite",
    ("messages", "volatile"): "history-rewrite",
    ("*", "param"): "param-churn",
}
_TOOL_SUB = {"tool-order": "order", "tool-subset": "subset", "tool-definition": "definition",
             "volatile": "definition"}

_REFERENCES: Mapping[str, tuple[str, ...]] = {
    "volatile-system": ("anth-invalidation-hierarchy", "anth-cache-preserving-apis",
                        "cb-breaker-taxonomy"),
    "system-edit": ("anth-invalidation-hierarchy", "cb-breaker-taxonomy"),
    "serialization-churn": ("cb-key-order", "nondeterministic-tools"),
    "tool-churn": ("tool-subset-churn", "anth-invalidation-hierarchy"),
    "history-rewrite": ("history-rewrite-kstar", "cb-breaker-taxonomy"),
    "param-churn": ("anth-invalidation-hierarchy", "cache-aware-routing"),
    "missing-breakpoint": ("cb-breaker-taxonomy", "anth-cache-hidden-miss-causes"),
    "lookback-overflow": ("anth-lookback-20", "cb-lookback"),
    "breakpoint-placement": ("cb-shared-prefix-sim",),
    "write-never-read": ("write-without-read",),
    "fanout": ("anth-concurrency-fanout", "cb-concurrency"),
}
_LEVER_CLASS: Mapping[str, str] = {
    "missing-breakpoint": "cache_transform", "lookback-overflow": "cache_transform",
    "breakpoint-placement": "cache_transform", "write-never-read": "cache_transform",
    "fanout": "cache_transform",
}
_TITLES: Mapping[str, str] = {
    "volatile-system": "Volatile values in the system prompt break the prompt cache",
    "system-edit": "System prompt edits break the prompt cache",
    "serialization-churn": "Non-deterministic JSON key order breaks the prompt cache",
    "tool-churn": "Tool definition churn ({subs}) breaks the prompt cache",
    "history-rewrite": "Rewritten earlier messages break the cached history",
    "param-churn": "Parameter changes ({params}) break the prompt cache",
    "missing-breakpoint": "Cacheable prefix sent without any cache breakpoint",
    "lookback-overflow": "Breakpoints look back past the previous cache entry (20 positions)",
    "breakpoint-placement": "Breakpoint placement {best} would be cheaper than the observed one",
    "write-never-read": "Prompt-cache writes that are never read",
    "fanout": "Concurrent requests write the same prefix (cache fan-out)",
}


@dataclasses.dataclass
class _Event:
    kind: str
    rec: _Rec
    tier: str
    block_index: int
    cause: str
    tokens: int
    confirmed: bool
    evidence_kind: str = "block_divergence"


def _basis(pricer: Pricer, billing_class: str) -> Basis:
    if billing_class == "allowance":
        return Basis.LIST_EQUIVALENT
    return pricer.basis if pricer.basis in (Basis.LIST, Basis.CONTRACT) else Basis.LIST


def _price(pricer: Pricer, rec: _Rec, *, reads: bool, uncached: bool, writes: bool) -> Figure:
    u = rec.inf.usage
    usage = UsageBuckets(
        uncached_input=u.uncached_input if uncached else 0,
        cache_read=u.cache_read if reads else 0,
        cache_write_5m=u.cache_write_5m if writes else 0,
        cache_write_1h=u.cache_write_1h if writes else 0,
        cache_write_other=u.cache_write_other if writes else 0,
        cache_write_other_ttl_s=u.cache_write_other_ttl_s if writes and u.cache_write_other
        else None,
        cache_write_unknown=u.cache_write_unknown if writes else 0,
    )
    return pricer.price_usage(usage, rec.ctx, ts_ms=rec.t, billable=rec.inf.billable,
                              usage_source=rec.inf.usage_source).figure


def _billed_miss(prev: _Rec, rec: _Rec) -> bool:
    pu, cu = prev.inf.usage, rec.inf.usage
    expected = min(pu.cache_read + pu.cache_write, cu.total_input)
    return is_miss_event(max(0, expected - cu.cache_read), expected)


def _reset_between(lane: Lane, lo: int, hi: int) -> bool:
    return any(lo < ev.ts_ms <= hi and ev.kind in _RESETS for ev in lane.events)


class BlockBreakers:
    """The eleven block-level breakers of SPEC §10.4 (``Detector`` protocol)."""

    id = "block.breakers"
    version = "1"
    kinds = KINDS
    requires = frozenset({"blocks"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """Findings per cohort ``(team, lane_kind, billing_class)`` of the fingerprinted lanes."""
        usable = [lane for lane in lanes if lane_skip_reason(lane) is None]
        cohorts: dict[tuple[str | None, str, str], list[Lane]] = {}
        for lane in usable:
            cohorts.setdefault(cohort_key(lane), []).append(lane)
        out: list[Finding] = []
        for key in sorted(cohorts, key=lambda k: (k[0] or "", k[1], k[2])):
            out.extend(_Cohort(self, key, cohorts[key], ctx).findings())
        return out


class _Cohort:
    """One cohort's observed simulation, events and findings."""

    def __init__(self, det: BlockBreakers, key: tuple[str | None, str, str],
                 lanes: Sequence[Lane], ctx: AnalysisContext) -> None:
        self.det = det
        self.team, self.lane_kind, self.bclass = key
        self.ctx = ctx
        self.pricer = ctx.pricer
        self.rules: CacheRulesProvider = ctx.rules if ctx.rules is not None else RulesTable()
        self.lanes = sorted(lanes, key=lambda ln: ln.lane_key)
        self.basis = _basis(self.pricer, self.bclass)
        self.allowance = self.bclass == "allowance"
        self.chains = _build(self.lanes, pricer=self.pricer, rules=self.rules)
        self.sim = _simulate(self.chains)
        self.by_lane: dict[str, list[_Rec]] = {}
        for rec in self.chains.recs:
            self.by_lane.setdefault(rec.lane.lane_key, []).append(rec)
        self.events: dict[str, list[_Event]] = {k: [] for k in KINDS}
        self.replayer = BlockReplayer()
        self.min_usd = min_usd_nano(ctx)
        self.placement_best = ""
        self.placement_savings: list[tuple[str, int]] = []

    # ------------------------------------------------------------------ events
    def _divergences(self) -> None:
        for rec in self.chains.recs:
            prev = rec.prev
            if prev is None:
                continue
            if rec.root == prev.root and rec.d >= prev.n:
                continue                                   # a pure prefix extension
            div = first_divergence(prev.req, rec.req, rules=rec.rules)
            if div is None:
                continue
            tier, idx, cause = div
            kind = _CAUSE_KIND.get((tier, cause)) or _CAUSE_KIND.get(("*", cause))
            if kind is None:
                continue
            if not prev.bps or prev.bps[-1][1] < idx:
                continue                                   # nothing cached there to break
            if _reset_between(rec.lane, prev.req.ts_start_ms, rec.req.ts_start_ms):
                continue                                   # an expected rebuild
            sub = cause
            if kind == "tool-churn":
                sub = _TOOL_SUB.get(cause, "definition")
            elif kind == "param-churn":
                params = sorted({name for _tier, name in salt_diff(prev.req, rec.req,
                                                                  rules=rec.rules)})
                sub = "+".join(params) or "param"
            u = rec.inf.usage
            self.events[kind].append(_Event(
                kind, rec, tier, idx, sub, u.uncached_input + u.cache_write,
                _billed_miss(prev, rec)))

    def _missing_breakpoints(self) -> None:
        nodes = self.chains.nodes
        for lane in self.lanes:
            recs = self.by_lane.get(lane.lane_key, [])
            if not recs or any(r.bps or r.auto for r in recs):
                continue
            if any(r.inf.usage.cache_read or r.inf.usage.cache_write for r in recs):
                continue
            for rec in recs:
                if rec.prev is None or rec.d <= 0 or rec.units <= 0:
                    continue
                shared = nodes.cum[rec.base] * rec.total // rec.units
                if shared >= rec.min_cache and shared > 0:
                    self.events["missing-breakpoint"].append(_Event(
                        "missing-breakpoint", rec, "messages", rec.d - 1, "no-markers", shared,
                        True, "transition"))

    def _lookback(self) -> None:
        for rec in self.chains.recs:
            pred = self.sim.preds[rec.req.request_id]
            if pred.over_tokens <= 0:
                continue
            billed = rec.inf.usage.cache_read
            expected = pred.over_tokens
            if not is_miss_event(max(0, expected - billed), expected):
                continue                                   # billed reads do not confirm it
            last_bp = pred.bps[-1][1] if pred.bps else 0
            self.events["lookback-overflow"].append(_Event(
                "lookback-overflow", rec, "messages", last_bp, "lookback", expected, True))

    def _unread(self) -> None:
        for e in self.sim.unread:
            w = e.writer
            if e.writers > 1 or e.overflow or w.inf.usage.cache_write <= 0:
                continue
            if w.lane_n > 1 and w.lane_i == w.lane_n - 1:
                continue                                   # a lane's final write: unavoidable
            recs = self.by_lane[w.lane.lane_key]
            if w.lane_i + 1 < len(recs):
                succ = recs[w.lane_i + 1]
                if succ.d <= e.bp_index:
                    continue                               # the successor diverged: a breaker
                if succ.t > e.expires:
                    continue                               # idle past the TTL: usage level
            pred = self.sim.preds[w.req.request_id]
            seg = next((s for idx, s, ent in pred.written if ent is e), 0)
            self.events["write-never-read"].append(_Event(
                "write-never-read", w, "messages", e.bp_index, "unread", seg, True, "event"))

    def _fanout(self) -> None:
        for rec in self.chains.order:
            pred = self.sim.preds[rec.req.request_id]
            for e in pred.fanout:
                seg = next((s for idx, s, ent in pred.written if ent is e), 0)
                self.events["fanout"].append(_Event(
                    "fanout", rec, "messages", e.bp_index, "concurrent", seg,
                    rec.inf.usage.cache_write > 0, "event"))
        if not any(ev.confirmed for ev in self.events["fanout"]):
            self.events["fanout"] = []

    # ------------------------------------------------------------------ money
    def _replay(self, lanes: Sequence[Lane], spec: str) -> Figure:
        res = self.replayer.replay(lanes, parse_policy(spec), mode="documented",
                                   pricer=self.pricer, rules=self.rules, calibration=None)
        return res.saving

    def _floor(self, fig: Figure) -> Figure:
        if fig.nano is not None and fig.nano < 0:
            return estimated(0, self.basis, calibration=Calibration.UNCALIBRATED,
                             note=NO_RECOVERY_NOTE)
        return fig

    def _lanes_of(self, events: Iterable[_Event]) -> list[Lane]:
        keys = {ev.rec.lane.lane_key for ev in events}
        return [lane for lane in self.lanes if lane.lane_key in keys]

    def _recoverable(self, kind: str, events: list[_Event]) -> Figure | None:
        if kind == "volatile-system":
            return self._floor(self._replay(self.lanes, f"repair={REPAIR_VOLATILE}"))
        if kind == "serialization-churn":
            return self._floor(self._replay(self.lanes, f"repair={REPAIR_SORT_KEYS}"))
        if kind == "tool-churn":
            subs = {ev.cause for ev in events}
            if "subset" in subs:
                spec = f"repair={REPAIR_TOOL_SUPERSET}"
            elif "order" in subs:
                spec = f"repair={REPAIR_TOOL_ORDER}"
            else:
                return None
            return self._floor(self._replay(self.lanes, spec))
        if kind == "param-churn":
            return self._floor(self._replay(self.lanes, f"repair={REPAIR_PIN_PARAMS}"))
        if kind == "missing-breakpoint":
            return self._floor(self._replay(self._lanes_of(events), f"repair={REPAIR_ADD_END}"))
        if kind == "lookback-overflow":
            return self._floor(self._replay(self._lanes_of(events), "breakpoints=every_15"))
        if kind == "write-never-read":
            return self._floor(self._replay(self._lanes_of(events),
                                            f"repair={REPAIR_DROP_UNREAD}"))
        if kind == "fanout":
            fig = self._floor(self._replay(self.lanes, f"repair={REPAIR_STAGGER}"))
            return dataclasses.replace(fig, upper_bound=True) if fig.nano is not None else fig
        return None

    def _cost(self, kind: str, events: list[_Event]) -> Figure:
        recs: dict[str, _Rec] = {}
        for ev in events:
            recs.setdefault(ev.rec.req.request_id, ev.rec)
        writes_only = kind in ("write-never-read", "fanout")
        figs = [_price(self.pricer, rec, reads=False, uncached=not writes_only, writes=True)
                for _rid, rec in sorted(recs.items())]
        return sum_figures(figs, self.basis)

    # ------------------------------------------------------------------ placement
    def _placement(self) -> tuple[list[Lane], Figure | None]:
        busy = {ev.rec.lane.lane_key for kind, evs in self.events.items() for ev in evs
                if kind != "write-never-read"}
        candidates = [lane for lane in self.lanes
                      if lane.lane_key not in busy
                      and any(r.bps or r.auto for r in self.by_lane.get(lane.lane_key, []))]
        if not candidates:
            return [], None
        best: tuple[int, str, Figure] | None = None
        for placement in CANONICAL_PLACEMENTS:
            fig = self._replay(candidates, f"breakpoints={placement}")
            nano = fig.nano if fig.nano is not None else 0
            self.placement_savings.append((placement, nano))
            if nano > 0 and (best is None or nano > best[0]):
                best = (nano, placement, fig)
        if best is None:
            return candidates, None
        self.placement_best = best[1]
        return candidates, best[2]

    # ------------------------------------------------------------------ findings
    def findings(self) -> list[Finding]:
        self._divergences()
        self._missing_breakpoints()
        self._lookback()
        self._unread()
        self._fanout()
        out: list[Finding] = []
        for kind in KINDS:
            if kind == "breakpoint-placement":
                candidates, fig = self._placement()
                if fig is None or fig.nano is None or fig.nano < self.min_usd:
                    continue
                cost = sum_figures(
                    [_price(self.pricer, rec, reads=True, uncached=True, writes=True)
                     for lane in candidates for rec in self.by_lane[lane.lane_key]], self.basis)
                out.append(self._placement_finding(candidates, fig, cost))
                continue
            events = self.events[kind]
            if not events:
                continue
            recoverable = self._recoverable(kind, events)
            cost = self._cost(kind, events)
            gate = recoverable if recoverable is not None and recoverable.nano is not None \
                else cost
            if gate.nano is not None and gate.nano < self.min_usd:
                continue
            out.append(self._finding(kind, events, cost, recoverable))
        return out

    def _users(self, lanes: Iterable[Lane]) -> int:
        return len({r.attribution.principal for lane in lanes for r in lane.requests
                    if r.attribution.principal})

    def _title(self, kind: str, **fmt: str) -> str:
        title = _TITLES[kind].format(**fmt)
        if self.allowance:
            title = "Allowance headroom: " + title
        return title[:120]

    def _where(self) -> str:
        return f"{self.team or 'unattributed'} {self.lane_kind} lanes"

    def _summary(self, text: str, recoverable: Figure | None) -> str:
        parts = [text]
        if recoverable is not None and recoverable.note == NO_RECOVERY_NOTE:
            parts.append(NO_RECOVERY_NOTE + ".")
        if self.allowance:
            parts.append("Seat-allowance usage: list-equivalent, not invoice dollars.")
        return " ".join(parts)[:400]

    def _scope(self):  # type: ignore[no-untyped-def]
        return make_scope(team=self.team, lane_kind=self.lane_kind,
                          billing_class="allowance" if self.allowance else None)

    def _models_support(self, feature: str, lanes: Iterable[Lane]) -> bool:
        ctxs = {rec.ctx: rec.t for lane in lanes for rec in self.by_lane.get(lane.lane_key, [])}
        return bool(ctxs) and all(self.pricer.supports(c, feature, ts_ms=t)
                                  for c, t in ctxs.items())

    def _fix(self, kind: str, events: list[_Event]) -> Fix:
        gates: tuple[str, ...] = ()
        lanes = self._lanes_of(events)
        if kind == "volatile-system":
            if self._models_support("mid_conversation_system", lanes):
                text = ("Move the volatile value (timestamp, UUID, counter) out of the system "
                        "prompt: into a mid-conversation role:system message (Opus 5/5.5/4.8, "
                        "Fable, Mythos; not Sonnet 5) or the latest user message.")
                gates = ("supports:mid_conversation_system",)
            else:
                text = ("Move the volatile value (timestamp, UUID, counter) out of the system "
                        "prompt and inject it in the latest user message instead.")
        elif kind == "system-edit":
            text = ("Keep the system prompt byte-stable for the whole run; put per-turn context "
                    "in the latest user message instead of editing the system text.")
        elif kind == "serialization-churn":
            text = ("Serialize tool schemas and message JSON deterministically "
                    "(json.dumps(sort_keys=True)) and build schemas once.")
        elif kind == "tool-churn":
            subs = {ev.cause for ev in events}
            texts = []
            if "order" in subs:
                texts.append("send tool definitions in one fixed order (sort them by name once "
                             "at startup)")
            if "subset" in subs:
                texts.append("keep a constant tool set: defer rarely used tools with "
                             "defer_loading / tool search, or add tools with the tool_addition "
                             "beta where supported")
                gates = ("anthropic-beta:mid-conversation-tool-changes-2026-07-01",)
            if "definition" in subs:
                texts.append("keep tool definitions byte-stable across calls")
            joined = "; ".join(texts) or "keep tool definitions byte-stable across calls"
            text = joined[0].upper() + joined[1:] + "."
        elif kind == "history-rewrite":
            text = ("Append new messages instead of rewriting earlier ones; when history must be "
                    "cleared, batch the clears (clear_at_least) so each rebuild pays back.")
        elif kind == "param-churn":
            text = ("Pin tool_choice, thinking, effort, speed and output format for the whole "
                    "conversation; change effort per message with the per-message effort beta "
                    "(Fable 5.1, Mythos 5.1, Opus 5, Opus 5.5) where supported.")
            if self._models_support("per_message_effort", lanes):
                gates = ("anthropic-beta:mid-conversation-output-config-2026-07-01",)
        elif kind == "missing-breakpoint":
            text = ("add a cache_control breakpoint (for example on the last message); the "
                    "stable prefix already meets the minimum cacheable length")
        elif kind == "lookback-overflow":
            text = ("Add an intermediate cache breakpoint every ~15 blocks: a breakpoint looks "
                    "back at most 20 positions for an earlier cache entry.")
        elif kind == "write-never-read":
            text = ("Drop breakpoints whose entries are never read; do not cache one-shot "
                    "traffic.")
        else:  # fanout
            text = ("Send one request, await its first token, then send the other N-1: a cache "
                    "entry is readable only after the first response token.")
        return Fix(text=text, config_patch=None, target="code", doc_url=_CACHING_DOC,
                   gates=gates)

    def _evidence(self, events: list[_Event]) -> tuple[EvidenceItem, ...]:
        items = []
        for ev in events:
            attrs = sorted([("tier", ev.tier), ("block_index", ev.block_index),
                            ("cause", ev.cause), ("tokens", ev.tokens),
                            ("ts_ms", ev.rec.req.ts_start_ms)])
            items.append(EvidenceItem(kind=ev.evidence_kind, ref=ev.rec.req.request_id,
                                      attrs=tuple(attrs)))
        return top_evidence(items)

    def _text(self, kind: str, events: list[_Event]) -> tuple[str, dict[str, str]]:
        n = len(events)
        lanes = len({ev.rec.lane.lane_key for ev in events})
        where = self._where()
        fmt: dict[str, str] = {}
        if kind == "tool-churn":
            fmt["subs"] = ", ".join(sorted({ev.cause for ev in events}))
        if kind == "param-churn":
            fmt["params"] = ", ".join(sorted({p for ev in events for p in ev.cause.split("+")}))
        first = min(events, key=lambda ev: (ev.rec.req.ts_start_ms, ev.rec.req.request_id))
        body = {
            "missing-breakpoint": f"{n} transitions in {lanes} lanes of {where} share a cacheable "
                                  "prefix but no request carries a cache breakpoint and billing "
                                  "shows no cache activity.",
            "lookback-overflow": f"{n} requests in {lanes} lanes of {where} missed a live cache "
                                 "entry more than 20 positions behind their breakpoint "
                                 "(confirmed by billed reads).",
            "write-never-read": f"{n} cache writes in {lanes} lanes of {where} were never read "
                                "before they expired.",
            "fanout": f"{n} requests in {lanes} lanes of {where} re-wrote a prefix another "
                      "request was still writing (upper bound: staggering adds latency).",
        }.get(kind, f"{n} requests in {lanes} lanes of {where} diverged from the cached prefix "
                    f"(first at block {first.block_index} of the {first.tier} tier).")
        return body, fmt

    def _finding(self, kind: str, events: list[_Event], cost: Figure,
                 recoverable: Figure | None) -> Finding:
        body, fmt = self._text(kind, events)
        if recoverable is None:
            body += " No mechanical repair is priced (cost only)."
        lanes = self._lanes_of(events)
        confirmed = sum(1 for ev in events if ev.confirmed)
        return build_finding(
            detector_id=self.det.id, kind=kind, detector_version=self.det.version,
            category="breaker", lever_class=_LEVER_CLASS.get(kind, "hygiene"), audience="org",
            title=self._title(kind, **fmt), summary=self._summary(body, recoverable),
            scope=self._scope(), n_events=len(events), n_lanes=len(lanes),
            n_users=self._users(lanes),
            first_seen_ms=min(ev.rec.req.ts_start_ms for ev in events),
            cost_observed=cost, recoverable=recoverable,
            lever_ids=tuple(lv.lever_id for lv in levers_for_kind(kind)),
            evidence=self._evidence(events), fix=self._fix(kind, events),
            confidence="high" if confirmed else "medium",
            validated_against=f"billed misses at {confirmed}/{len(events)} events"
            if kind in _DIVERGENCE_KINDS else None,
            references=_REFERENCES[kind])

    def _placement_finding(self, candidates: list[Lane], saving: Figure, cost: Figure
                           ) -> Finding:
        kind = "breakpoint-placement"
        best = self.placement_best
        items = [EvidenceItem(kind="aggregate", ref=f"placement:{p}",
                              attrs=(("nano", nano), ("placement", p)))
                 for p, nano in self.placement_savings]
        body = (f"Replaying {len(candidates)} lanes of {self._where()} with breakpoints at "
                f"{best} is cheaper than the observed placement (end of the shared prefix + end "
                "of the prompt).")
        return build_finding(
            detector_id=self.det.id, kind=kind, detector_version=self.det.version,
            category="breaker", lever_class=_LEVER_CLASS[kind], audience="org",
            title=self._title(kind, best=best), summary=self._summary(body, saving),
            scope=self._scope(), n_events=len(candidates), n_lanes=len(candidates),
            n_users=self._users(candidates),
            first_seen_ms=min(lane.requests[0].ts_start_ms for lane in candidates),
            cost_observed=cost, recoverable=saving,
            lever_ids=tuple(lv.lever_id for lv in levers_for_kind(kind)),
            evidence=top_evidence(items),
            fix=Fix(text=f"Move the cache breakpoint to the end of the shared prefix "
                         f"(placement {best}).", config_patch=None, target="code",
                    doc_url=_CACHING_DOC),
            confidence="medium", references=_REFERENCES[kind])
