"""Minimal stand-ins for F-SEM modules (area-local test helper).

They let wave-1 tests exercise the plumbing of
:func:`tokenbill.core.testing.smoke_pipeline_on_fakes` and the optional F-SEM hooks of the
conformance suites before F-SEM is merged. They implement only what those helpers call, following
the SPEC signatures (§3.11, §3.14, §3.15, §3.16, §3.19, §3.22); the real modules are exercised by
the gate-F tests (``test_gate_f.py``, ``test_smoke_fakes.py``).
"""

from __future__ import annotations

import itertools
import sys
import types
from fractions import Fraction
from typing import Any

import pytest

from tokenbill.core.ids import stable_id
from tokenbill.core.records import UsageBuckets
from tokenbill.core.types import Finding, Scope, Transition


def _transitions() -> types.ModuleType:
    mod = types.ModuleType("tokenbill.core.transitions")

    def classify_transitions(lane, *, pricer, rules, static_prefix_floor=None):
        reqs = [r for r in lane.requests if r.serving_inference is not None]
        out = []
        for i in range(1, len(reqs)):
            prev, cur = reqs[i - 1], reqs[i]
            pu, cu = prev.serving_inference.usage, cur.serving_inference.usage
            p, t = pu.cache_read + pu.cache_write, cu.total_input
            e = min(p, t)
            m = max(0, e - cu.cache_read)
            gap = cur.ts_start_ms - prev.ts_start_ms
            miss = 20 * m > e and m >= 2000
            cause = ("ttl-expiry" if gap > 300_000 else "unexplained") if miss else "hit"
            out.append(Transition(
                request_id=cur.request_id, lane_key=lane.lane_key, index=i, gap_ms=gap,
                total=t, reads=cu.cache_read, prev_prefix=p, expected_reuse=e, missed=m,
                is_miss_event=miss, cause=cause, sub_cause=None, ttl_s=300, ambiguous=False,
                predicted_hit=None, diag_reason=None))
        return out

    mod.classify_transitions = classify_transitions  # type: ignore[attr-defined]
    return mod


def _findings() -> types.ModuleType:
    mod = types.ModuleType("tokenbill.core.findings")

    def cohort_key(lane):
        return (lane.team, lane.kind.value, lane.billing_class)

    def miss_waste(t, request):
        usage = request.serving_inference.usage
        mw = min(t.missed, usage.cache_write)
        return mw, min(t.missed - mw, usage.uncached_input)

    def rate_nano(pricer, ctx, ts_ms, bucket, tokens):
        return pricer.unit_rates(ctx, ts_ms=ts_ms).bucket_nano(bucket, tokens)

    def make_scope(**dims):
        return Scope(dims=tuple(sorted((k, v) for k, v in dims.items() if v is not None)))

    def finding_id(detector_id, kind, scope):
        return stable_id("fd", detector_id, kind, *(f"{k}={v}" for k, v in scope.dims))

    def build_finding(**fields):
        return Finding(**fields)

    for fn in (cohort_key, miss_waste, rate_nano, make_scope, finding_id, build_finding):
        setattr(mod, fn.__name__, fn)
    return mod


def _policy() -> types.ModuleType:
    mod = types.ModuleType("tokenbill.core.policy")

    def lane_matches(selector, lane):
        if selector == "all":
            return True
        first = lane.requests[0].attribution if lane.requests else None
        values = {"lane_kind": lane.kind.value, "team": lane.team,
                  "agent_product": first.agent_product if first else None}
        return all(values.get(k) == v for k, _, v in (t.partition(":")
                                                      for t in selector.split(",")))

    def to_spec(policy):
        return policy.name

    mod.lane_matches = lane_matches  # type: ignore[attr-defined]
    mod.to_spec = to_spec  # type: ignore[attr-defined]
    return mod


def _shapley() -> types.ModuleType:
    mod = types.ModuleType("tokenbill.core.shapley")

    def shapley_exact(players, value):
        players = list(players)
        total = {p: Fraction(0) for p in players}
        orders = list(itertools.permutations(players))
        for order in orders:
            seen: frozenset[str] = frozenset()
            for p in order:
                total[p] += value(seen | {p}) - value(seen)
                seen = seen | {p}
        exact = {p: v / len(orders) for p, v in total.items()}
        out = {p: int(v) for p, v in exact.items()}
        out[players[-1]] += value(frozenset(players)) - sum(out.values())
        return out

    mod.shapley_exact = shapley_exact  # type: ignore[attr-defined]
    return mod


def _cache_rules() -> types.ModuleType:
    mod = types.ModuleType("tokenbill.core.cache_rules")

    class RulesTable:
        def rules_for(self, provider, channel, model):  # pragma: no cover - never called
            raise NotImplementedError

    mod.RulesTable = RulesTable  # type: ignore[attr-defined]
    return mod


def conventions(normalize_to: Any = None, *, enabled: bool = True) -> types.ModuleType:
    """A ``core.conventions`` stand-in: ``normalize`` maps ``input_tokens``/``output_tokens``."""
    mod = types.ModuleType("tokenbill.core.conventions")

    class Conv:
        def __init__(self) -> None:
            self.enabled = enabled

    def get_convention(convention_id):
        if convention_id == "unknown":
            raise KeyError(convention_id)
        return Conv()

    def normalize(convention_id, raw):
        if normalize_to is not None:
            return normalize_to, []
        return UsageBuckets(uncached_input=raw.get("input_tokens", 0),
                            output=raw.get("output_tokens", 0)), []

    mod.get_convention = get_convention  # type: ignore[attr-defined]
    mod.normalize = normalize  # type: ignore[attr-defined]
    return mod


def install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the stand-ins into ``sys.modules`` for one test."""
    for name, factory in (("tokenbill.core.transitions", _transitions),
                          ("tokenbill.core.findings", _findings),
                          ("tokenbill.core.policy", _policy),
                          ("tokenbill.core.shapley", _shapley),
                          ("tokenbill.core.cache_rules", _cache_rules)):
        monkeypatch.setitem(sys.modules, name, factory())
