"""Hypothesis properties: on random lane sets (teams, lane kinds, billing classes, products,
models incl. an unpriced one, gaps around both TTLs, hits, misses, partial reads, uncached input,
fast toggles, context edits and reset events), every cache detector conforms (declared kinds,
labeled figures, deterministic ids, allowance labeling, shard invariance), never raises, never
emits an unpriced or negative figure and never reports more events than transitions."""

from __future__ import annotations

from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.core.records import Lane, LaneKind
from tokenbill.core.registry import BUILTIN_DETECTORS, load
from tokenbill.core.testing import assert_detector_conforms

from .helpers import (
    ALL_DETECTORS,
    OPUS48,
    OPUS55,
    SONNET5,
    ctx,
    event,
    fleet_saving,
    fn_replayer,
    lane,
)

GAPS = (5, 30, 290, 295, 305, 420, 1_800, 3_590, 3_605, 7_200)
KINDS = (LaneKind.MAIN, LaneKind.SUBAGENT, LaneKind.WORKFLOW_AGENT, LaneKind.API_RUN)
MODELS = (OPUS55, OPUS55, OPUS55, SONNET5, OPUS48, "claude-sonnet-5-5")   # the last is unpriced


@st.composite
def lane_sets(draw: Any) -> list[Lane]:
    out = []
    for li in range(draw(st.integers(1, 6))):
        key = f"L{li}"
        model = draw(st.sampled_from(MODELS))
        hour = draw(st.booleans())
        rows = []
        per: dict[int, dict[str, Any]] = {}
        ts = draw(st.integers(0, 20))
        prefix = 0
        n = draw(st.integers(1, 7))
        for i in range(n):
            if i:
                ts += draw(st.sampled_from(GAPS))
            mode = draw(st.sampled_from(("hit", "hit", "miss", "partial", "uncached", "shrink")))
            append = draw(st.integers(0, 30_000) | st.just(120_000))
            u = draw(st.sampled_from((0, 0, 5, 12_000)))
            if i == 0 or mode == "miss":
                r, w = 0, prefix + append + 2_000
            elif mode == "hit":
                r, w = prefix, append
            elif mode == "partial":
                r, w = prefix // 3, prefix - prefix // 3 + append
            elif mode == "uncached":
                r, w, u = 0, 0, prefix + append
            else:
                r, w = prefix // 4, append
            rows.append((ts, r, 0 if hour else w, w if hour else 0, u, draw(st.integers(0, 900))))
            prefix = r + w
            extra: dict[str, Any] = {}
            if i and draw(st.integers(0, 9)) == 0:
                extra["model"] = draw(st.sampled_from(MODELS))
            if i and draw(st.integers(0, 9)) == 0:
                extra["speed"] = "fast"
            if i and draw(st.integers(0, 9)) == 0:
                extra["applied_edits"] = (("clear_tool_uses_20250919",
                                           draw(st.integers(1, 50_000))),)
            if extra:
                per[i] = extra
        events = []
        for _ in range(draw(st.integers(0, 3))):
            at = draw(st.integers(0, ts + 400))
            kind = draw(st.sampled_from(("compaction", "clear", "context_edit", "human_prompt")))
            attrs: dict[str, Any] = {}
            if kind == "compaction":
                attrs = {"trigger": "auto", "pre_tokens": draw(st.integers(1, 200_000)),
                         "post_tokens": 20_000, "duration_ms": 1_000, "dropped_tokens": None}
            elif kind == "context_edit":
                attrs = {"edit_type": "clear_tool_uses_20250919",
                         "cleared_input_tokens": draw(st.integers(0, 40_000))}
            events.append(event(key, at, kind, **attrs))
        out.append(lane(
            key, rows, kind=draw(st.sampled_from(KINDS)), model=model, per_request=per,
            events=events, team=draw(st.sampled_from(("a", "b", None))),
            principal=draw(st.sampled_from(("r_u1", "r_u2", "r_u3", None))),
            product=draw(st.sampled_from(("claude_code", "agent_sdk", "api", None))),
            billing_path=draw(st.sampled_from(("api_key", "subscription", "usage_credits"))),
            gateway=draw(st.sampled_from((None, None, "gw"))),
            cwd_key=draw(st.sampled_from(("h_" + "a" * 20, "h_" + "b" * 20)))))
    return out


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(lanes=lane_sets(), min_usd=st.sampled_from(("0", "0.001", "1.00")))
def test_every_detector_conforms_on_random_lanes(lanes: list[Lane], min_usd: str) -> None:
    c = ctx(replayer=fn_replayer(fleet_saving),
            thresholds={"min_usd": min_usd, "policy.ttl.a": "1h",
                        "cache.gateway-disabled.min_5m_requests": "2"})
    transitions = sum(max(0, len(ln.requests) - 1) for ln in lanes)
    for detector_id in ALL_DETECTORS:
        detector = load(BUILTIN_DETECTORS[detector_id])()
        findings = assert_detector_conforms(detector, lanes, c)
        for f in findings:
            assert f.cost_observed.nano is not None and f.cost_observed.nano >= 0
            if f.recoverable is not None and detector_id != "cache.ttl-advisor":
                assert f.recoverable.nano is not None
            if f.kind not in ("ttl-1h-recommended", "ttl-5m-recommended",
                              "keepalive-recommended", "ttl-heterogeneous", "no-cache",
                              "oversized-ttl", "tail-writes", "cold-fanout",
                              "compaction-cold"):
                assert f.n_events <= transitions
            assert f.n_lanes <= len(lanes)


@settings(max_examples=40, deadline=None)
@given(lanes=lane_sets())
def test_no_replayer_never_breaks(lanes: list[Lane]) -> None:
    c = ctx(thresholds={"min_usd": "0"})
    for detector_id in ALL_DETECTORS:
        detector = load(BUILTIN_DETECTORS[detector_id])()
        for f in detector.detect(lanes, c):
            if detector_id in ("cache.ttl-advisor",):
                raise AssertionError("no advice without a replayer")
            assert f.finding_id and f.references
