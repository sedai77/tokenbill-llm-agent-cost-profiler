"""Cross-source merge rules (SPEC §7.3) — idempotent and order-independent.

A **contribution** is one source's view of a logical request (after write-time privacy). Merged
requests are a pure function of the *set* of contributions (the executable specification is
``core.testing.MemoryStore``, RULINGS K-11), which is what makes ingest idempotent and
order-independent:

* identity — contributions join through their request id, provider message ids and provider
  request ids; a provider request id seen with two message ids anywhere is a **collision** and never
  a join key (``dq.request_id_collision``);
* the usage set (attempts, inferences, timing, and with it lane, session and sequence number) comes
  from the contribution with the highest ``(fidelity, source priority, serving output)``; exact ties
  go to the canonically smallest contribution ("keep the existing set" would depend on order);
* attribution is merged per field and per ``extra`` key: the value of the highest-priority
  contribution that has one, equal priority → the lexicographically smallest value;
* ``sources_mask`` is the OR of the adapters' bits; diagnostics and request parameters fill if null;
* the surviving request id is the smallest id among contributions carrying a provider message id
  (else the smallest id).

Provider records keep one version per id: ``final`` over provisional, then the latest fetch,
then the canonically largest; GitHub Copilot records: the latest fetch, then the canonically
largest (addendum §7.1). Lane shells keep the best-ranked shell (a known kind, a known cache
scope, then the canonically smallest).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from tokenbill.core.records import (
    COPILOT_AGG_SOURCE_KINDS,
    COPILOT_CHANNELS,
    Attempt,
    Attribution,
    CostLine,
    Fidelity,
    Lane,
    LaneKind,
    OutcomeAggregate,
    Request,
    RequestParams,
    UsageAggregate,
    WorkloadClass,
    to_json,
)

__all__ = [
    "SOURCES_MASK_BITS",
    "Contribution",
    "MergedGroup",
    "canonical",
    "choose_version",
    "latest_fetch_wins",
    "merge_contributions",
    "merge_attribution",
    "shell_rank",
    "sources_mask",
]

#: ``sources_mask`` bit per adapter (SPEC §7.3 table; Copilot bits per CORE-AMENDMENTS C-30 and
#: R-E37). Other adapters contribute no bit.
SOURCES_MASK_BITS: Mapping[str, int] = {
    "claude-code": 1, "trace@1": 2, "trace@2": 4, "otlp": 8, "openai": 16, "bedrock": 32,
    "anthropic-responses": 64, "claude-code-headless": 128,
    "copilot-cli": 256, "copilot-otel": 512, "copilot-vscode-traces": 1024,
    "gh-aw-token-usage": 2048, "copilot-export": 4096,
}

#: Default source priority of a request without a ``SourceRef`` (SPEC §3.2 "other 10").
DEFAULT_PRIORITY = 10


def canonical(obj: Any) -> str:
    """Canonical JSON text (sorted keys, no whitespace, UTF-8 kept)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sources_mask(adapters: Iterable[str]) -> int:
    """Bitwise OR of the adapters' ``sources_mask`` bits."""
    mask = 0
    for adapter in adapters:
        mask |= SOURCES_MASK_BITS.get(adapter, 0)
    return mask


@dataclasses.dataclass(eq=False)
class Contribution:
    """One source's (cleaned) view of a request, with the metadata the merge rules read."""

    request: Request
    adapter: str
    fidelity: Fidelity
    priority: int
    source_id: str
    _canon: str | None = None

    @classmethod
    def of(cls, req: Request, *, default_adapter: str, source_id: str) -> Contribution:
        """The contribution of *req* read from a source of adapter *default_adapter*."""
        ref = req.source
        if ref is not None:
            return cls(req, ref.adapter, ref.fidelity, ref.priority, source_id)
        return cls(req, default_adapter, Fidelity.FULL, DEFAULT_PRIORITY, source_id)

    @property
    def canon(self) -> str:
        """Canonical JSON of the request (computed once)."""
        if self._canon is None:
            self._canon = canonical(to_json(self.request))
        return self._canon

    @property
    def key(self) -> str:
        """Identity of the contribution: adapter and the SHA-256 of its canonical JSON."""
        return f"{self.adapter}:{hashlib.sha256(self.canon.encode('utf-8')).hexdigest()}"

    @property
    def serving_output(self) -> int:
        """``output`` of the serving inference (0 without one): the split-entry rule."""
        si = self.request.serving_inference
        return si.usage.output if si is not None else 0

    def rank(self) -> tuple[int, int, int, str]:
        """Sort key of the usage-set rule: the smallest key wins."""
        return (-int(self.fidelity), -self.priority, -self.serving_output, self.canon)

    def message_ids(self) -> set[str]:
        """Provider message ids of every attempt."""
        return {a.provider_message_id for a in self.request.attempts if a.provider_message_id}

    def request_hints(self) -> set[str]:
        """Provider request ids of every attempt (join hints)."""
        return {a.provider_request_id for a in self.request.attempts if a.provider_request_id}

    def hint_pairs(self) -> set[tuple[str, str]]:
        """``(provider_request_id, provider_message_id)`` seen on one attempt."""
        return {(a.provider_request_id, a.provider_message_id) for a in self.request.attempts
                if a.provider_request_id and a.provider_message_id}

    def join_keys(self, collided: Iterable[str] | frozenset[str] = frozenset()) -> list[tuple]:
        """Join keys: request id, message ids and non-colliding request hints."""
        bad = collided if isinstance(collided, (set, frozenset)) else frozenset(collided)
        keys: list[tuple] = [("id", self.request.request_id)]
        keys += [("msg", m) for m in sorted(self.message_ids())]
        keys += [("rq", q) for q in sorted(self.request_hints()) if q not in bad]
        return keys


@dataclasses.dataclass
class MergedGroup:
    """The merged request of a group of contributions and its merge metadata."""

    request: Request
    winner: Contribution
    members: tuple[Contribution, ...]
    sources_mask: int
    attr_prio: dict[str, int]
    usage_mismatch: bool

    @property
    def fidelity(self) -> Fidelity:
        return self.winner.fidelity

    @property
    def priority(self) -> int:
        return self.winner.priority

    @property
    def adapter(self) -> str:
        return self.winner.adapter


def _is_null(value: object) -> bool:
    return value is None or value == () or value is WorkloadClass.UNKNOWN


def _value_key(value: Any) -> str:
    if dataclasses.is_dataclass(value):
        return canonical(to_json(value))
    return str(value)


def _pick(candidates: list[tuple[int, Any]]) -> tuple[int, Any]:
    """Highest priority wins; equal priority → lexicographically smallest value."""
    best = max(p for p, _ in candidates)
    values = [v for p, v in candidates if p == best]
    return best, min(values, key=_value_key)


_ATTR_FIELDS = tuple(f.name for f in dataclasses.fields(Attribution) if f.name != "extra")
_PARAM_FIELDS = tuple(f.name for f in dataclasses.fields(RequestParams)
                      if f.name != "model_requested")


def merge_attribution(group: Sequence[Contribution]) -> tuple[Attribution, dict[str, int]]:
    """Per-field (and per ``extra`` key) attribution merge; returns the attribution and the
    priority of every chosen value (``extra.<key>`` for extra keys)."""
    fields: dict[str, Any] = {}
    prio: dict[str, int] = {}
    for name in _ATTR_FIELDS:
        cands = [(c.priority, getattr(c.request.attribution, name)) for c in group
                 if not _is_null(getattr(c.request.attribution, name))]
        if cands:
            prio[name], fields[name] = _pick(cands)
    extra: dict[str, list[tuple[int, str]]] = {}
    for c in group:
        for key, value in c.request.attribution.extra:
            extra.setdefault(key, []).append((c.priority, value))
    chosen = []
    for key, cands_x in sorted(extra.items()):
        p, v = _pick(cands_x)
        prio[f"extra.{key}"] = p
        chosen.append((key, v))
    fields["extra"] = tuple(chosen)
    return Attribution(**fields), prio


def _merge_params(winner: Contribution, group: Sequence[Contribution]) -> RequestParams:
    base = winner.request.params
    fills: dict[str, Any] = {}
    for name in _PARAM_FIELDS:
        if not _is_null(getattr(base, name)):
            continue
        cands = [(c.priority, getattr(c.request.params, name)) for c in group
                 if not _is_null(getattr(c.request.params, name))]
        if cands:
            fills[name] = _pick(cands)[1]
    return dataclasses.replace(base, **fills) if fills else base


def _fill_diagnostics(winner: Contribution, group: Sequence[Contribution]) -> tuple[Attempt, ...]:
    attempts = winner.request.attempts
    final = attempts[-1]
    if final.diagnostics is not None:
        return attempts
    cands = [(c.priority, c.request.attempts[-1].diagnostics) for c in group
             if c.request.attempts[-1].diagnostics is not None]
    if not cands:
        return attempts
    return (*attempts[:-1], dataclasses.replace(final, diagnostics=_pick(cands)[1]))


def _first_by_priority(group: Sequence[Contribution],
                       getter: Callable[[Request], Any]) -> Any:
    for c in sorted(group, key=lambda c: (-c.priority, c.canon)):
        value = getter(c.request)
        if not _is_null(value):
            return value
    return None


def _merge_group(group: Sequence[Contribution]) -> MergedGroup:
    if len(group) == 1:
        only = group[0]
        _, prio = merge_attribution(group)
        return MergedGroup(request=only.request, winner=only, members=(only,),
                           sources_mask=sources_mask([only.adapter]), attr_prio=prio,
                           usage_mismatch=False)
    ordered = sorted(group, key=lambda c: c.rank())
    winner = ordered[0]
    with_msg = [c.request.request_id for c in group if c.message_ids()]
    rid = min(with_msg) if with_msg else min(c.request.request_id for c in group)
    usages = {canonical(to_json(c.request.serving_inference.usage)) for c in group
              if c.request.serving_inference is not None}
    w = winner.request
    attribution, prio = merge_attribution(group)
    merged = Request(
        request_id=rid, session_key=w.session_key, lane_key=w.lane_key, seq=w.seq,
        attribution=attribution, params=_merge_params(winner, group),
        attempts=_fill_diagnostics(winner, group),
        fingerprint=w.fingerprint if w.fingerprint is not None else _first_by_priority(
            group, lambda r: r.fingerprint),
        appended=w.appended or (_first_by_priority(group, lambda r: r.appended) or ()),
        source=w.source)
    members = tuple(sorted(group, key=lambda c: c.key))
    return MergedGroup(request=merged, winner=winner, members=members,
                       sources_mask=sources_mask(c.adapter for c in group), attr_prio=prio,
                       usage_mismatch=len(usages) > 1)


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def merge_contributions(contribs: Sequence[Contribution],
                        collided: Iterable[str] = ()) -> list[MergedGroup]:
    """Merge *contribs* (identical contributions count once) into merged requests. *collided*
    names provider request ids that are never join keys (the store-wide collision set; hints seen
    with two message ids among *contribs* are added). Output order: by merged request id."""
    unique: dict[str, Contribution] = {}
    for c in contribs:
        unique.setdefault(c.key, c)
    items = sorted(unique.values(), key=lambda c: (c.request.request_id, c.adapter, c.canon))
    bad = set(collided)
    seen: dict[str, set[str]] = {}
    for c in items:
        for rq, msg in c.hint_pairs():
            seen.setdefault(rq, set()).add(msg)
    bad |= {rq for rq, msgs in seen.items() if len(msgs) > 1}
    uf = _UnionFind(len(items))
    first_by: dict[tuple, int] = {}
    for i, c in enumerate(items):
        for key in c.join_keys(bad):
            if key in first_by:
                uf.union(first_by[key], i)
            else:
                first_by[key] = i
    groups: dict[int, list[Contribution]] = {}
    for i, c in enumerate(items):
        groups.setdefault(uf.find(i), []).append(c)
    merged = [_merge_group(g) for g in groups.values()]
    merged.sort(key=lambda m: m.request.request_id)
    return merged


def shell_rank(shell: Lane) -> tuple[bool, bool, str]:
    """Preferred lane shell: a known kind, a known cache scope, then the canonically smallest."""
    return (shell.kind is LaneKind.UNKNOWN, shell.cache_scope_key == "unknown",
            canonical(to_json(shell)))


def latest_fetch_wins(rec: object) -> bool:
    """GitHub Copilot provider records keep the version with the latest ``fetched_ms`` (addendum
    §7.1); every other record keeps the SPEC rule (final, then latest)."""
    if isinstance(rec, CostLine):
        return rec.channel in COPILOT_CHANNELS
    if isinstance(rec, UsageAggregate):
        return rec.source_kind in COPILOT_AGG_SOURCE_KINDS or dict(rec.dims).get(
            "channel") in COPILOT_CHANNELS
    return isinstance(rec, OutcomeAggregate) and rec.source_kind == "github.copilot_metrics"


def _version_key(rec: Any, copilot: bool) -> tuple:
    canon = canonical(to_json(rec))
    fetched = getattr(rec, "fetched_ms", 0)
    if copilot:
        return (fetched, canon)
    return (getattr(rec, "finality", "") == "final", fetched, canon)


def choose_version(existing: Any, incoming: Any) -> Any:
    """The version of one provider record to keep (a commutative, associative choice)."""
    copilot = latest_fetch_wins(existing) or latest_fetch_wins(incoming)
    if _version_key(incoming, copilot) > _version_key(existing, copilot):
        return incoming
    return existing
