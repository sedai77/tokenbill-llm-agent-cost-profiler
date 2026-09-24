"""Hypothesis properties and fuzzing: on random lane sets (teams, lane kinds, billing classes,
products, workloads, models incl. an unpriced one, gaps, contexts up to 900k, efforts, fast
mode, US geo, truncations, multi-attempt retries, tool errors, appended items and events), every
DETECT-OTHER detector conforms (declared kinds, labeled figures, deterministic ids, allowance
labeling, shard invariance), never raises, keeps ranges ordered and never reports more lanes than
it was given; random threshold strings only ever raise ``UsageError``."""

from __future__ import annotations

from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.core.builders import make_lane
from tokenbill.core.errors import UsageError
from tokenbill.core.records import AppendedItem, Lane, LaneKind, WorkloadClass
from tokenbill.core.registry import BUILTIN_DETECTORS, load
from tokenbill.core.testing import assert_detector_conforms

from .helpers import (
    ALL_DETECTORS,
    OPUS5,
    OPUS48,
    OPUS55,
    SONNET5,
    attempt,
    attribution,
    ctx,
    event,
    fleet_saving,
    lane,
    multi_request,
    request,
    table_replayer,
    usage,
)

GAPS = (5, 30, 70, 240, 305, 600, 1_800, 3_605, 86_400)
KINDS = (LaneKind.MAIN, LaneKind.SUBAGENT, LaneKind.WORKFLOW_AGENT, LaneKind.API_RUN)
MODELS = (OPUS55, OPUS55, OPUS5, SONNET5, OPUS48, "claude-sonnet-5-5")   # the last is unpriced
EFFORTS = (None, "low", "medium", "high", "xhigh", "max", "turbo")
WORKLOADS = (WorkloadClass.INTERACTIVE, WorkloadClass.CI, WorkloadClass.SERVICE,
             WorkloadClass.SCHEDULED)


@st.composite
def lane_sets(draw: Any) -> list[Lane]:
    out: list[Lane] = []
    for li in range(draw(st.integers(1, 6))):
        key = f"L{li}"
        model = draw(st.sampled_from(MODELS))
        rows = []
        per: dict[int, dict[str, Any]] = {}
        ts = draw(st.integers(0, 2 * 86_400))
        prefix = 0
        n = draw(st.integers(1, 8))
        for i in range(n):
            if i:
                ts += draw(st.sampled_from(GAPS))
            append = draw(st.integers(0, 40_000) | st.just(250_000))
            cold = i == 0 or draw(st.integers(0, 3)) == 0
            r, w = (0, prefix + append + 1_000) if cold else (prefix, append)
            u = draw(st.sampled_from((0, 5, 10_000)))
            rows.append((ts, r, w, 0, u, draw(st.integers(0, 17_000))))
            prefix = r + w
            extra: dict[str, Any] = {}
            if draw(st.integers(0, 4)) == 0:
                extra["effort"] = draw(st.sampled_from(EFFORTS))
                extra["session_effort"] = draw(st.sampled_from(EFFORTS))
            if draw(st.integers(0, 6)) == 0:
                extra["speed"] = "fast"
            if draw(st.integers(0, 8)) == 0:
                extra["inference_geo"] = "us"
            if draw(st.integers(0, 5)) == 0:
                extra["stop_reason"] = "max_tokens"
            if i and draw(st.integers(0, 3)) == 0:
                name = draw(st.sampled_from(("Read", "Bash", "h_" + "e" * 20)))
                extra["appended"] = [AppendedItem(
                    kind="tool_result", name=name, n_bytes=draw(st.integers(0, 60_000)),
                    is_error=draw(st.booleans()))]
            if extra:
                per[i] = extra
        events = []
        for _ in range(draw(st.integers(0, 3))):
            at = draw(st.integers(0, ts + 400))
            kind = draw(st.sampled_from(("clear", "human_prompt", "api_error",
                                         "context_injection")))
            attrs: dict[str, Any] = {}
            if kind == "api_error":
                attrs = {"status": 400, "error_type": draw(st.sampled_from(
                    ("prompt_too_long", "overloaded")))}
            elif kind == "context_injection":
                attrs = {"att_type": "claude_md", "n_bytes": draw(st.integers(0, 30_000))}
            events.append(event(key, at, kind, **attrs))
        out.append(lane(
            key, rows, kind=draw(st.sampled_from(KINDS)), model=model, per_request=per,
            events=events, team=draw(st.sampled_from(("a", "b", None))),
            principal=draw(st.sampled_from(("r_u1", "r_u2", "r_u3", None))),
            product=draw(st.sampled_from(("claude_code", "agent_sdk", "api", None))),
            billing_path=draw(st.sampled_from(("api_key", "subscription", "usage_credits"))),
            workload=draw(st.sampled_from(WORKLOADS)),
            agent_type=draw(st.sampled_from((None, "Explore"))),
            session_key=draw(st.sampled_from((None, "s_shared")))))
    if draw(st.booleans()):
        attr = attribution(team="a", principal="r_u1", product="agent_sdk")
        first = request("R0", 0, 0, w5=60_000, attr=attr)
        atts = [attempt(10 + i, n=i, outcome=draw(st.sampled_from(("http_error", "timeout"))),
                        error_type=draw(st.sampled_from(("prompt_too_long", "overloaded", None))),
                        retry_layer=draw(st.sampled_from(("sdk", "agent", None))),
                        should_retry=draw(st.sampled_from((None, False))))
                for i in range(draw(st.integers(1, 5)))]
        atts.append(attempt(draw(st.sampled_from((20, 400))), n=len(atts),
                            usage=usage(w5=61_000, o=300),
                            sdk_retry_count=draw(st.sampled_from((None, 0, 4)))))
        out.append(make_lane([first, multi_request("R0", 1, atts, attr=attr)], lane_key="R0",
                             session_key="s_R0", kind=LaneKind.API_RUN))
    return out


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(lanes=lane_sets(), min_usd=st.sampled_from(("0", "0.001", "1.00")),
       floor=st.sampled_from(({}, {("ws:w1", OPUS55): 5_000})))
def test_every_detector_conforms_on_random_lanes(lanes: list[Lane], min_usd: str,
                                                 floor: dict) -> None:
    c = ctx(replayer=table_replayer(fleet_saving), static_prefix_floor=floor,
            thresholds={"min_usd": min_usd, "model.routing.min_requests": "2",
                        "tail.runaway.min_hourly_usd": "0.01"})
    for detector_id in ALL_DETECTORS:
        detector = load(BUILTIN_DETECTORS[detector_id])()
        findings = assert_detector_conforms(detector, lanes, c)
        for f in findings:
            assert f.n_lanes <= len(lanes) and f.references
            for fig in (f.cost_observed, f.recoverable):
                if fig is not None and fig.low_nano is not None:
                    assert fig.low_nano <= fig.high_nano


@settings(max_examples=30, deadline=None)
@given(lanes=lane_sets())
def test_no_replayer_never_breaks(lanes: list[Lane]) -> None:
    c = ctx(thresholds={"min_usd": "0"})
    for detector_id in ALL_DETECTORS:
        for f in load(BUILTIN_DETECTORS[detector_id])().detect(lanes, c):
            assert f.kind not in ("compaction-window", "delegation-routing", "default-model",
                                  "default-effort")
            assert f.finding_id and f.references


KEYS = ("min_usd", "context.size-tax.threshold_tokens", "context.compaction-window.min_window",
        "context.compaction-window.max_extra_compactions", "model.routing.default_model_share",
        "failure.path.retry_share", "automation.cadence_cv", "tail.runaway.min_hourly_usd",
        "tail.runaway.p99_multiple", "premium.sticky-escalation.min_days", "defaults.effort",
        "context.compaction-window.post_tokens", "context.static-prefix.tool_defs_threshold")


@settings(max_examples=150, deadline=None)
@given(key=st.sampled_from(KEYS), value=st.text(max_size=12) | st.sampled_from(
    ("1e999", "NaN", "-0", "0.5", "7", "Infinity", "high", "1/4", " 2 ", "1E30", "9e99999",
     "1e-999999")))
def test_threshold_fuzz_raises_only_usage_errors(key: str, value: str) -> None:
    lanes = [lane("L0", [(0, 0, 250_000, 0, 5, 800), (30, 250_000, 3_000, 0, 5, 800)]),
             lane("L1", [(0, 0, 20_000, 0, 5, 800)], kind=LaneKind.SUBAGENT)]
    c = ctx(replayer=table_replayer(fleet_saving), thresholds={key: value})
    for detector_id in ALL_DETECTORS:
        try:
            load(BUILTIN_DETECTORS[detector_id])().detect(lanes, c)
        except UsageError:
            pass
