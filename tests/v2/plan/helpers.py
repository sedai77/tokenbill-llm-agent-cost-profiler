"""Area-local builders for the PLAN tests (imported only by tests in ``tests/v2/plan``).

Every lane is synthetic, built in code with ``core.builders`` (no fixture files, no real
transcripts), dated from 2026-09-23 (Opus 5.5 is priced from 2026-09-22). Replays come from
``core.testing.FakeReplayer`` (tables or functions of the policy) unless a gate test uses the real
``UsageReplayer``.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Sequence
from typing import Any

from tokenbill.core.builders import FlatRates, make_lane, make_request
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.findings import build_finding, make_scope
from tokenbill.core.labels import Basis, Figure, estimated, exact
from tokenbill.core.records import (
    Attribution,
    Lane,
    LaneEvent,
    LaneKind,
    RequestParams,
    UsageBuckets,
    WorkloadClass,
)
from tokenbill.core.shards import shard_of_lanes
from tokenbill.core.testing import FakeReplayer
from tokenbill.core.types import (
    AnalysisContext,
    CalibrationReport,
    Finding,
    Fix,
    LaneIndexRow,
    Policy,
    ShardKey,
)

T0 = 1_790_121_600_000  # 2026-09-23T00:00:00Z
DAY_MS = 86_400_000
USD = 10**9
PRICER = FlatRates()
RULES = RulesTable()
MAIN_SEL = "agent_product:claude_code,lane_kind:main"

#: SPEC §3.16 / Appendix A.7 value function over players A, B, C (nano).
A7 = {"": 0, "A": 10, "B": 20, "C": 5, "AB": 26, "AC": 15, "BC": 24, "ABC": 30}


def attribution(team: str | None = "t1", *, product: str | None = "claude_code",
                billing_path: str = "api_key", principal: str | None = None,
                workload: WorkloadClass = WorkloadClass.INTERACTIVE,
                extra: tuple[tuple[str, str], ...] = (), **kw: Any) -> Attribution:
    """An attribution with the common fields set."""
    return Attribution(team=team, agent_product=product, billing_path=billing_path,
                       principal=principal, workload_class=workload, extra=extra, **kw)


def lane(key: str, *, team: str | None = "t1", kind: LaneKind = LaneKind.MAIN,
         product: str | None = "claude_code", billing_path: str = "api_key",
         model: str = "claude-opus-5-5", n: int = 2, ts: int = T0, gap_s: int = 30,
         usage: UsageBuckets | None = None, principal: str | None = None,
         session: str | None = None, effort: str | None = None,
         events: Sequence[LaneEvent] = (), extra: tuple[tuple[str, str], ...] = (),
         workload: WorkloadClass = WorkloadClass.INTERACTIVE, **attr_kw: Any) -> Lane:
    """A lane of *n* requests *gap_s* seconds apart."""
    attr = attribution(team, product=product, billing_path=billing_path, principal=principal,
                       workload=workload, extra=extra, **attr_kw)
    use = usage if usage is not None else UsageBuckets(uncached_input=1_000, output=100)
    reqs = [make_request(key, i, ts + i * gap_s * 1000, use, model, attribution=attr,
                         session_key=session or f"s_{key}", billing_path=billing_path,
                         params=RequestParams(model_requested=model, effort=effort))
            for i in range(n)]
    return make_lane(reqs, kind=kind, events=events, lane_key=key,
                     session_key=session or f"s_{key}")


def index_of(lanes: Iterable[Lane], *, point: Callable[[Lane], int] | None = None
             ) -> list[LaneIndexRow]:
    """The lane index of *lanes* (``point_nano`` from *point*, default the FlatRates bill)."""
    rows = []
    for ln in lanes:
        if point is not None:
            nano = point(ln)
        else:
            nano = 0
            for r in ln.requests:
                for att in r.attempts:
                    for inf in att.inferences:
                        fig = PRICER.price_inference(inf, ts_ms=att.ts_start_ms).figure
                        nano += fig.nano or 0
        rows.append(LaneIndexRow(lane_key=ln.lane_key, team=ln.team, lane_kind=ln.kind.value,
                                 billing_class=ln.billing_class, requests=len(ln.requests),
                                 point_nano=nano))
    return rows


class Loader:
    """An in-memory ``load_lanes`` over *lanes* that records its calls."""

    def __init__(self, lanes: Sequence[Lane]) -> None:
        self.lanes = list(lanes)
        self.calls: list[tuple[str, object]] = []

    def __call__(self, keys: Collection[str] | None, shard: ShardKey | None) -> list[Lane]:
        if shard is None:
            self.calls.append(("sample", len(keys or ())))
            wanted = set(keys or ())
            return [ln for ln in self.lanes if ln.lane_key in wanted]
        self.calls.append(("shard", shard))
        return shard_of_lanes(self.lanes, shard)


def ctx(replayer: Any = None, *, pricer: Any = PRICER, calibration: CalibrationReport | None = None,
        thresholds: dict[str, str] | None = None) -> AnalysisContext:
    """An AnalysisContext over the window of the fixtures."""
    return AnalysisContext(pricer=pricer, rules=RULES, replayer=replayer,
                           calibration=calibration, window=(T0, T0 + 30 * DAY_MS),
                           capabilities=frozenset({"usage_sequence", "timing"}),
                           thresholds=thresholds or {}, now_ms=T0 + 30 * DAY_MS)


def finding(kind: str, levers: Sequence[str], *, team: str | None = "t1",
            detector: str = "premium.modifiers", basis: Basis = Basis.LIST,
            billing_class: str | None = None, lane_kind: str | None = None,
            recoverable: Figure | None = None, projected: Figure | None = None,
            fix: Fix | None = None, model: str | None = None, extra_dims: dict | None = None,
            lever_class: str = "rate", needs_eval: bool = False) -> Finding:
    """A validated finding linking *levers*."""
    dims = {"team": team, "lane_kind": lane_kind, "billing_class": billing_class, "model": model}
    dims.update(extra_dims or {})
    return build_finding(
        detector_id=detector, kind=kind, detector_version="1", category="lever",
        lever_class=lever_class, audience="org", title=f"{kind} finding",
        summary="synthetic finding", scope=make_scope(**dims), n_events=1, n_lanes=1,
        n_users=5, first_seen_ms=T0, cost_observed=exact(1_000_000, basis),
        recoverable=recoverable, projected_monthly=projected, lever_ids=tuple(levers), fix=fix,
        references=("test-fixture",), needs_eval=needs_eval)


def est(nano: int, basis: Basis = Basis.LIST, **kw: Any) -> Figure:
    """An ESTIMATED figure."""
    return estimated(nano, basis, note="test", **kw)


def flags(policy: Policy) -> str:
    """Letters of the A.7 players present in *policy*: A = fast=off, B = regional=global, C =
    geo=global (so A < B < C follows the lever-id order cc.fast_mode_opt_in < endpoint.global <
    geo.global)."""
    out = ""
    if policy.fast_off:
        out += "A"
    if policy.regional_to_global:
        out += "B"
    if policy.geo_global:
        out += "C"
    return out


def a7_replayer(weight: Callable[[Lane], int] = lambda _ln: 1) -> FakeReplayer:
    """A FakeReplayer whose per-lane saving is the A.7 value of the policy's players × weight."""
    return FakeReplayer.from_function(lambda ln, pol: A7[flags(pol)] * weight(ln))


def a7_findings(team: str | None = "t1") -> list[Finding]:
    """Findings linking the three A.7 levers (endpoint.global is not a default candidate)."""
    return [finding("regional-premium", ["endpoint.global"], team=team),
            finding("fast-premium", ["cc.fast_mode_opt_in"], team=team),
            finding("geo-premium", ["geo.global"], team=team)]


def plan_nanos(obj: object) -> list[int]:
    """Every int leaf of ``to_json(obj)``."""
    from tokenbill.core.records import to_json

    out: list[int] = []

    def walk(x: object) -> None:
        if isinstance(x, bool):
            return
        if isinstance(x, int):
            out.append(x)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(to_json(obj))
    return out


def table_lane(key: str, rows: Sequence[tuple[int, int, int, int, int, int]], *,
               team: str | None = "t1", kind: LaneKind = LaneKind.MAIN,
               product: str | None = "claude_code", billing_path: str = "api_key",
               model: str = "claude-opus-5-5", ts: int = T0) -> Lane:
    """A lane from rows of ``(ts_s, R, W5, W1, U, O)`` (SPEC Appendix A notation)."""
    attr = attribution(team, product=product, billing_path=billing_path)
    reqs = [make_request(key, i, ts + t * 1000,
                         UsageBuckets(cache_read=r, cache_write_5m=w5, cache_write_1h=w1,
                                      uncached_input=u, output=o),
                         model, attribution=attr, session_key=f"s_{key}",
                         billing_path=billing_path)
            for i, (t, r, w5, w1, u, o) in enumerate(rows)]
    return make_lane(reqs, kind=kind, lane_key=key, session_key=f"s_{key}")


#: Appendix A.1: Opus 5.5, 4 requests 420 s apart, full 5m rewrites of 100k…106k, O = 500.
A1_ROWS = [(0, 0, 100_000, 0, 0, 500), (420, 0, 102_000, 0, 0, 500),
           (840, 0, 104_000, 0, 0, 500), (1260, 0, 106_000, 0, 0, 500)]
#: Appendix A.2 (bursty, billed 5m) and A.2b (the same billed 1h).
A2_ROWS = [(0, 0, 100_000, 0, 0, 500), (30, 100_000, 2_000, 0, 0, 500),
           (60, 102_000, 2_000, 0, 0, 500), (90, 104_000, 2_000, 0, 0, 500)]
A2B_ROWS = [(t, r, 0, w5, u, o) for t, r, w5, _w1, u, o in A2_ROWS]
#: Appendix A.6: Sonnet 5, 30 s gaps, contexts 300k / 450k / 500k, outputs 1k.
A6_ROWS = [(0, 0, 300_000, 0, 0, 1_000), (30, 300_000, 150_000, 0, 0, 1_000),
           (60, 450_000, 50_000, 0, 0, 1_000)]
