"""Dual-engine agreement checks (SPEC §10.4 acceptance), shared by the local test (the area's §5.5
bridge, :mod:`tests.v2.blocksim.fp`) and the merge-gate test (TRACE's ``TraceV1Adapter``).

Each check takes the lane built from one v0.1 demo scenario and the v0.1 calls it came from."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any

from tokenbill.core.cache_rules import PER_MESSAGE_EFFORT_BETA, RulesTable
from tokenbill.core.records import Lane, UsageBuckets
from tokenbill.core.testing import FakePricer
from tokenbill.core.transitions import classify_transitions
from tokenbill.core.types import AnalysisContext
from tokenbill.detect.block import BlockBreakers
from tokenbill.sim.block_replay import BlockReplayer, first_divergence
from tokenbill.simulator import simulate
from tokenbill.trace import Run

CAPS = frozenset({"blocks", "usage_sequence", "timing", "params"})


def context() -> AnalysisContext:
    """The dual-engine gate compares breaker kinds, not dollar thresholds: ``min_usd = 0``."""
    return AnalysisContext(pricer=FakePricer(), rules=RulesTable(), replayer=None,
                           calibration=None, window=(0, 2**53), capabilities=CAPS,
                           thresholds={"min_usd": "0"})


def _request_of(lane: Lane, calls: Sequence[Any]) -> dict[int, Any]:
    """v0.1 call index → the lane's request for it. One request per call in call order (§5.5),
    matched by position rather than by millisecond timestamp, so the check does not depend on
    how an adapter rounds v0.1's float seconds."""
    assert len(lane.requests) == len(calls), (len(lane.requests), len(calls))
    return {c.index: r for c, r in zip(sorted(calls, key=lambda c: (c.ts, c.index)),
                                       lane.requests, strict=True)}


def check_timestamp(lane: Lane, calls: Sequence[Any]) -> None:
    """Exactly ``volatile-system``, first biting at call 1."""
    findings = BlockBreakers().detect([lane], context())
    assert [f.kind for f in findings] == ["volatile-system"], [f.kind for f in findings]
    f = findings[0]
    call1 = _request_of(lane, calls)[calls[1].index]
    assert f.first_seen_ms == call1.ts_start_ms
    assert call1.request_id in {e.ref for e in f.evidence}
    assert f.n_events == len(calls) - 1


def check_tool_churn(lane: Lane, calls: Sequence[Any]) -> None:
    """Exactly ``tool-churn`` (order) at the rotation calls; no volatile-system, no
    missing-breakpoint (the count is 1)."""
    findings = BlockBreakers().detect([lane], context())
    assert [f.kind for f in findings] == ["tool-churn"], [f.kind for f in findings]
    f = findings[0]
    rotations = [c for c in calls[1:] if c.index % 4 == 0]
    by_call = _request_of(lane, calls)
    assert {e.ref for e in f.evidence} == {by_call[c.index].request_id for c in rotations}
    assert all(dict(e.attrs)["cause"] == "order" for e in f.evidence)
    assert "order" in f.title


def check_no_cache(lane: Lane, calls: Sequence[Any]) -> None:
    """Exactly ``missing-breakpoint``."""
    findings = BlockBreakers().detect([lane], context())
    assert [f.kind for f in findings] == ["missing-breakpoint"], [f.kind for f in findings]
    assert findings[0].recoverable is not None and findings[0].recoverable.nano > 0


def check_well_behaved(lane: Lane, calls: Sequence[Any]) -> None:
    """No breaker; ``predict(placement="end")`` reads equal v1's optimal-cache reads within
    rounding (one token per call)."""
    findings = BlockBreakers().detect([lane], context())
    assert findings == [], [f.kind for f in findings]
    outs = BlockReplayer().predict([lane], pricer=FakePricer(), placement="end")
    predicted = sum(o.usage.cache_read for o in outs)
    v1 = simulate(Run(run_id=calls[0].run_id, calls=tuple(calls)))
    optimal = next(s for s in v1 if s.name == "optimal-cache").tokens["cache_read"]
    assert abs(predicted - optimal) <= len(calls), (predicted, optimal)
    billed = sum(c.usage.cache_read_input_tokens for c in calls)
    assert predicted == billed


EFFORT_ROWS = (
    # (agent_product, serving model, betas, exempt)
    ("agent_sdk", "claude-sonnet-5", (), False),
    ("agent_sdk", "claude-opus-5-5", (), False),
    ("claude_code", "claude-opus-5-5", (), True),
    ("agent_sdk", "claude-opus-5", (PER_MESSAGE_EFFORT_BETA,), True),
)


def with_effort(lane: Lane, product: str, model: str, betas: tuple[str, ...]) -> Lane:
    """The lane with effort alternating high/low, *product*/*model*/*betas*, and billed full
    rewrites (so every transition is a billed miss the usage level must explain)."""
    reqs = []
    for i, r in enumerate(lane.requests):
        att = r.attempts[0]
        inf = att.inferences[-1]
        u = inf.usage
        usage = UsageBuckets(cache_write_5m=u.total_input, output=u.output)
        inf = dataclasses.replace(inf, usage=usage,
                                  pricing=dataclasses.replace(inf.pricing, model=model))
        att = dataclasses.replace(att, inferences=(*att.inferences[:-1], inf))
        params = dataclasses.replace(r.params, effort="high" if i % 2 == 0 else "low",
                                     betas=betas)
        attribution = dataclasses.replace(r.attribution, agent_product=product,
                                          client_version="2.1.270")
        reqs.append(dataclasses.replace(r, attempts=(att, *r.attempts[1:]), params=params,
                                        attribution=attribution))
    return dataclasses.replace(lane, requests=tuple(reqs))


def check_effort_agreement(lane: Lane) -> None:
    """The block engine salts effort into the messages tier exactly when ``core.transitions``
    calls the billed miss an ``effort-change`` (the shared D28 predicate)."""
    pricer, rules = FakePricer(), RulesTable()
    for product, model, betas, exempt in EFFORT_ROWS:
        variant = with_effort(lane, product, model, betas)
        by_id = {r.request_id: r for r in variant.requests}
        order = [r.request_id for r in variant.requests]
        transitions = [t for t in classify_transitions(variant, pricer=pricer, rules=rules)
                       if t.is_miss_event]          # causes are assigned to billed misses only
        assert len(transitions) >= 5, "the demo lane has billed misses"
        for t in transitions:
            cur = by_id[t.request_id]
            prev = by_id[order[order.index(t.request_id) - 1]]
            div = first_divergence(prev, cur)
            block_says = div is not None and div[2] == "param" and div[0] == "messages"
            usage_says = t.cause == "param-change" and t.sub_cause == "effort-change"
            assert block_says == usage_says, (product, model, t.index, div, t.cause)
            assert block_says is (not exempt)
        breakers = {f.kind for f in BlockBreakers().detect([variant], context())}
        assert ("param-churn" in breakers) is (not exempt)
