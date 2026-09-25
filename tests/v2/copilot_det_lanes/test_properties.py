"""Property tests: on random Copilot worlds (VS Code, CLI, gh-aw and Claude Code lanes; band and
non-band models; known and unknown context tiers; compactions with and without events) the
detector conforms (incl. shard invariance), is independent of lane order, ignores Claude Code
lanes, and every band premium satisfies ``0 ≤ low ≤ point ≤ high``."""

from __future__ import annotations

import json
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.core.builders import assert_no_canary
from tokenbill.core.findings import product_family
from tokenbill.core.labels import Evidence
from tokenbill.core.records import LaneKind, to_json
from tokenbill.core.testing import assert_detector_conforms
from tokenbill.detect.copilot_lanes import band_premium

from .helpers import (
    CLI,
    CREDIT,
    DET,
    GH_AW,
    PRICER,
    T0,
    VSCODE,
    attr,
    compaction_event,
    compaction_inf,
    ctx,
    event,
    lane,
    req,
    run,
)

SETTINGS = settings(max_examples=40, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow])
MODELS = ("gpt-5.5", "gpt-5.4", "grok-4.6", "claude-sonnet-5", "claude-opus-4-8",
          "gpt-5-mini", "no-such-model")
TIERS = (None, "default", "long_context")
TRIGGERS = ("threshold", "manual", "context_limit_retry", "memory_pressure", "model_switch", None)

usage = st.fixed_dictionaries({
    "u": st.integers(0, 400_000), "r": st.integers(0, 400_000), "w": st.integers(0, 50_000),
    "o": st.integers(0, 20_000),
})
request = st.fixed_dictionaries({
    "usage": usage, "model": st.sampled_from(MODELS), "tier": st.sampled_from(TIERS),
    "compaction": st.one_of(st.none(), st.tuples(st.integers(0, 200_000),
                                                 st.integers(0, 5_000))),
    "trigger": st.sampled_from(TRIGGERS), "gap": st.integers(1, 900),
    "tools": st.one_of(st.none(), st.integers(0, 40_000)),
})
lane_spec = st.fixed_dictionaries({
    "team": st.sampled_from(("alpha", "beta", None)),
    "product": st.sampled_from((VSCODE, CLI, GH_AW, "claude_code")),
    "kind": st.sampled_from((LaneKind.MAIN, LaneKind.SUBAGENT)),
    "ci": st.booleans(), "limit": st.one_of(st.none(), st.integers(1, 2_000)),
    "agent_type": st.sampled_from((None, "Explore", "general-purpose")),
    "principal": st.sampled_from(("r_dev1", "r_dev2", None)),
    "requests": st.lists(request, min_size=1, max_size=5),
})


def _build(specs: list[dict[str, Any]]) -> list:
    lanes = []
    for n, spec in enumerate(specs):
        key = f"L{n}"
        claude = spec["product"] == "claude_code"
        a = attr(team=spec["team"], principal=spec["principal"], product=spec["product"],
                 workload="ci" if spec["ci"] else "interactive",
                 billing_path="api_key" if claude else "copilot_pool",
                 agent_type=spec["agent_type"])
        requests, events, ts = [], [], 0
        for seq, r in enumerate(spec["requests"]):
            ts += r["gap"]
            extra = ()
            if r["compaction"] is not None:
                cu, co = r["compaction"]
                extra = (compaction_inf(f"{key}-c{seq}", u=cu, o=co,
                                        billing_path="api_key" if claude else "copilot_pool"),)
                events.append(compaction_event(key, ts, r["trigger"], system=2_000,
                                               tools=r["tools"]))
            kw: dict[str, Any] = {}
            if claude:
                kw = {"provider": "anthropic", "channel": "anthropic_api"}
            requests.append(req(key, seq, ts, a=a, model=r["model"], context_tier=r["tier"],
                                extra=extra, **r["usage"], **kw))
        if spec["limit"] is not None:
            events.append(event(key, 0, "session_meta", credit_limit_nano=spec["limit"] * CREDIT))
        lanes.append(lane(key, requests, kind=spec["kind"], events=events))
    return lanes


@SETTINGS
@given(st.lists(lane_spec, min_size=1, max_size=6), st.sampled_from(("0", "0.05", "1.00")))
def test_conforms_order_independent_and_family_filtered(specs: list[dict[str, Any]],
                                                        min_usd: str) -> None:
    lanes = _build(specs)
    c = ctx(min_usd=min_usd)
    found = assert_detector_conforms(DET, lanes, c)
    assert [to_json(f) for f in run(list(reversed(lanes)), c)] == [to_json(f) for f in found]
    copilot = [ln for ln in lanes if product_family(ln) == "copilot"]
    assert run(copilot, c) == found
    for f in found:
        assert dict(f.scope.dims)["product"] == "copilot"
        for fig in (f.cost_observed, f.recoverable):
            if fig is not None and fig.low_nano is not None:
                assert fig.low_nano <= fig.nano <= fig.high_nano
        if f.kind == "long-context-band" and f.cost_observed.evidence is Evidence.EXACT:
            assert f.cost_observed.nano > 0
    assert_no_canary(json.dumps([to_json(f) for f in found]))


@SETTINGS
@given(usage, st.sampled_from(MODELS), st.sampled_from(TIERS), st.booleans())
def test_band_premium_bounds(u: dict[str, int], model: str, tier: str | None,
                             auto: bool) -> None:
    r = req("P", 0, 0, model=model, context_tier=tier, routing="auto" if auto else "direct",
            **u)
    inf = r.serving_inference
    assert inf is not None
    prem = band_premium(PRICER, inf, T0)
    if prem is None:
        return
    assert 0 <= prem.low <= prem.point <= prem.high
    priced = PRICER.price_inference(inf, ts_ms=T0).figure
    assert priced.nano is not None and prem.point <= priced.nano
    if prem.exact:
        assert prem.low == prem.point == prem.high
