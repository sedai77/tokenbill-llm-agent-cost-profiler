"""Area-local fixture builders for the DETECT-OTHER tests (imported only by tests in
``tests/v2/detect_other``).

Every fixture is synthetic, built in code with ``core.builders`` (never real transcripts) and dated
from 2026-09-23 (Opus 5.5 is priced from 2026-09-22). Rates are FakePricer's (``core/facts.json``);
in nano-USD per token: Opus 5.5 input 4,000, output 20,000, read 200, 5m write 5,000, 1h write
8,000 (fast mode: input 8,000, read 400, 5m write 10,000, 1h write 16,000, output 40,000); Opus 5
and Opus 4.8 input 5,000, read 500, 5m write 6,250, 1h write 10,000, output 25,000; Sonnet 5 input
2,000, read 200, 5m write 2,500, 1h write 4,000, output 10,000; Haiku 4.5 input 1,000.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from tokenbill.core.builders import make_attempt, make_inference, make_lane, make_request
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.records import (
    AppendedItem,
    Attempt,
    Attribution,
    Lane,
    LaneEvent,
    LaneKind,
    Request,
    RequestParams,
    UsageBuckets,
    WorkloadClass,
)
from tokenbill.core.testing import FakePricer, FakeReplayer
from tokenbill.core.types import AnalysisContext, Finding, Policy, ReplayResult

T0 = 1_790_121_600_000  # 2026-09-23T00:00:00Z
DAY_MS = 86_400_000
USD = 10**9
OPUS55 = "claude-opus-5-5"
OPUS5 = "claude-opus-5"
OPUS48 = "claude-opus-4-8"
SONNET5 = "claude-sonnet-5"
SONNET46 = "claude-sonnet-4-6"
FABLE5 = "claude-fable-5"
HAIKU45 = "claude-haiku-4-5"
PRICER = FakePricer()
RULES = RulesTable()
CAPS = frozenset({"usage_sequence", "timing", "ttl_split", "events", "attempts", "params",
                  "human_prompts", "lanes_exact", "appended", "iterations", "blocks",
                  "workload"})
CWD = "h_" + "c" * 20
#: The ten DETECT-OTHER registry ids (SPEC §3.7).
ALL_DETECTORS = ("context.size-tax", "context.compaction-window", "context.static-prefix",
                 "attrib.carry", "premium.modifiers", "premium.sticky-escalation", "model.routing",
                 "failure.path", "automation", "tail.runaway")


def attribution(*, team: str | None = "payments", principal: str | None = "r_dev1",
                product: str | None = "claude_code", billing_path: str = "api_key",
                workload: WorkloadClass | str = WorkloadClass.INTERACTIVE,
                entrypoint: str | None = "cli", agent_type: str | None = None,
                repo: str | None = None, extra: tuple[tuple[str, str], ...] = (),
                client_version: str | None = "2.1.270") -> Attribution:
    """An Attribution with the fields the detectors read."""
    return Attribution(principal=principal, team=team, agent_product=product,
                       billing_path=billing_path, cwd_key=CWD, workload_class=workload,
                       entrypoint=entrypoint, agent_type=agent_type, repo=repo, extra=extra,
                       client_version=client_version)


def usage(*, r: int = 0, w5: int = 0, w1: int = 0, u: int = 0, o: int = 500,
          reasoning: int | None = None, wu: int = 0) -> UsageBuckets:
    """Usage buckets (``wu``: unknown-TTL writes)."""
    return UsageBuckets(cache_read=r, cache_write_5m=w5, cache_write_1h=w1, uncached_input=u,
                        output=o, output_reasoning=reasoning, cache_write_unknown=wu)


def request(lane_key: str, seq: int, ts_s: float, *, model: str = OPUS55,
            attr: Attribution | None = None, params: RequestParams | None = None,
            effort: str | None = None, session_effort: str | None = None,
            max_tokens: int | None = None, stop_reason: str | None = None,
            appended: Sequence[AppendedItem] = (), session_key: str | None = None,
            **buckets: Any) -> Request:
    """One request at ``T0 + ts_s`` whose serving inference carries the given buckets
    (``r``, ``w5``, ``w1``, ``u``, ``o``, ``reasoning``, ``wu``) plus pricing kwargs (``speed``,
    ``inference_geo``, ``endpoint_scope``, ``service_tier``, ``channel``)."""
    attr = attr if attr is not None else attribution()
    ctx_kw = {k: buckets.pop(k) for k in ("speed", "inference_geo", "endpoint_scope",
                                          "service_tier", "channel", "write_ttl_hint")
              if k in buckets}
    fingerprint = buckets.pop("fingerprint", None)
    if params is None:
        params = RequestParams(model_requested=model, effort=effort,
                               session_effort=session_effort, max_tokens=max_tokens)
    return make_request(lane_key, seq, T0 + int(round(ts_s * 1000)), usage(**buckets), model,
                        attribution=attr, params=params,
                        billing_path=attr.billing_path or "unknown",
                        session_key=session_key or f"s_{lane_key}", stop_reason=stop_reason,
                        appended=tuple(appended), fingerprint=fingerprint, **ctx_kw)


Row = tuple[float, int, int, int, int, int]   # (ts_s, R, W5, W1, U, O)


def lane(key: str, rows: Iterable[Row], *, kind: LaneKind | str = LaneKind.MAIN,
         events: Sequence[LaneEvent] = (), scope: str = "ws:w1", model: str = OPUS55,
         attr: Attribution | None = None, session_key: str | None = None,
         per_request: Mapping[int, Mapping[str, Any]] | None = None, **attr_kw: Any) -> Lane:
    """A lane from ``(ts_s, R, W5, W1, U, O)`` rows; *per_request* overrides request kwargs by
    index."""
    attr = attr if attr is not None else attribution(**attr_kw)
    skey = session_key or f"s_{key}"
    reqs = []
    for seq, (ts_s, r, w5, w1, u, o) in enumerate(rows):
        extra = dict((per_request or {}).get(seq, {}))
        m = extra.pop("model", model)
        req_attr = extra.pop("attr", attr)
        buckets = {"r": r, "w5": w5, "w1": w1, "u": u, "o": o}
        buckets.update({k: extra.pop(k) for k in list(extra) if k in buckets})
        reqs.append(request(key, seq, ts_s, model=m, attr=req_attr, session_key=skey,
                            **buckets, **extra))
    return make_lane(reqs, kind=kind, events=list(events), scope=scope, lane_key=key,
                     session_key=skey)


def event(lane_key: str, ts_s: float, kind: str, **attrs: Any) -> LaneEvent:
    """A lane event at ``T0 + ts_s``."""
    return LaneEvent(lane_key=lane_key, ts_ms=T0 + int(round(ts_s * 1000)), kind=kind,
                     attrs=tuple(sorted(attrs.items())))


def attempt(ts_s: float, *, model: str = OPUS55, outcome: str = "ok",
            error_type: str | None = None, http_status: int | None = None,
            stop_reason: str | None = None, n: int = 0, billable: bool | None = True,
            usage_source: str = "final", **kw: Any) -> Attempt:
    """An attempt at ``T0 + ts_s``: with ``usage=`` (UsageBuckets) a serving inference is added,
    else the attempt carries no inference (a pre-token failure)."""
    u = kw.pop("usage", None)
    infs = ()
    if u is not None:
        infs = (make_inference(u, model=model, billable=billable, usage_source=usage_source,
                               inference_id=f"inf-{ts_s}-{n}", billing_path="api_key"),)
    return make_attempt(infs, ts_ms=T0 + int(round(ts_s * 1000)), attempt_no=n,
                        attempt_id=f"at-{ts_s}-{n}", outcome=outcome, error_type=error_type,
                        http_status=http_status, stop_reason=stop_reason, **kw)


def multi_request(lane_key: str, seq: int, attempts: Sequence[Attempt], *,
                  model: str = OPUS55, attr: Attribution | None = None,
                  session_key: str | None = None) -> Request:
    """A request made of explicit attempts."""
    attr = attr if attr is not None else attribution()
    return make_request(lane_key, seq, attempts[0].ts_start_ms, None, model, attribution=attr,
                        attempts=attempts, session_key=session_key or f"s_{lane_key}")


def ctx(*, replayer: Any = None, thresholds: Mapping[str, str] | None = None,
        capabilities: frozenset[str] = CAPS, **kw: Any) -> AnalysisContext:
    """An AnalysisContext over FakePricer and the SPEC cache rules."""
    return AnalysisContext(pricer=PRICER, rules=RULES, replayer=replayer, calibration=None,
                           window=(T0, T0 + 30 * DAY_MS), capabilities=capabilities,
                           thresholds=dict(thresholds or {}), now_ms=T0, **kw)


def table_replayer(savings: Mapping[str, int] | Callable[[Lane, Policy], int]) -> FakeReplayer:
    """A FakeReplayer saving ``savings[policy spec]`` nano **per lane** (or a function)."""
    if callable(savings):
        return FakeReplayer.from_function(savings)
    table = dict(savings)
    return FakeReplayer.from_function(lambda ln, p: table.get(FakeReplayer.policy_key(p), 0))


class CountingReplayer(FakeReplayer):
    """A FakeReplayer that also reports ``added_calls`` from a per-lane table keyed by policy spec
    (the extra compactions of a compaction-window replay)."""

    def __init__(self, savings: Mapping[str, int], added: Mapping[str, int]) -> None:
        table = dict(savings)
        super().__init__(fn=lambda ln, p: table.get(FakeReplayer.policy_key(p), 0))
        self.added = dict(added)

    def replay(self, lanes: Sequence[Lane], policy: Policy, **kw: Any) -> ReplayResult:
        result = super().replay(lanes, policy, **kw)
        per_lane = self.added.get(FakeReplayer.policy_key(policy), 0)
        return dataclasses.replace(result, added_calls=per_lane * len(lanes))


def by_kind(findings: Iterable[Finding], kind: str) -> list[Finding]:
    """The findings of one kind."""
    return [f for f in findings if f.kind == kind]


def one(findings: Iterable[Finding], kind: str) -> Finding:
    """Exactly one finding of *kind*."""
    found = by_kind(findings, kind)
    assert len(found) == 1, [(f.kind, f.scope.dims) for f in findings]
    return found[0]


def evidence(finding: Finding, ref: str) -> dict[str, str | int]:
    """The attrs of the evidence item *ref*."""
    for item in finding.evidence:
        if item.ref == ref:
            return dict(item.attrs)
    raise AssertionError(f"no evidence {ref!r}: {[e.ref for e in finding.evidence]}")


# ---------------------------------------------------------------------------------------------
# a mixed fleet planting every DETECT-OTHER kind (with min_usd 0.01)
# ---------------------------------------------------------------------------------------------

REPO = "h_" + "a" * 20
WORKFLOW = "h_" + "b" * 20
#: The static-prefix floor of the mixed fleet (agents' workspace, Opus 5.5).
FLEET_FLOOR = {("ws:agents", OPUS55): 10_000}
FLEET_THRESHOLDS = {"min_usd": "0.01"}


def fleet_saving(lane: Lane, policy: Policy) -> int:
    """A deterministic per-lane saving for any policy: 20,000,000 nano per request."""
    return 20_000_000 * len(lane.requests)


def _tool_fingerprint() -> Any:
    from tokenbill.core.builders import make_block, make_fingerprint

    tools = [make_block(f"t{i}", tier="tools", kind="tool_def", role=None, est_tokens=700)
             for i in range(20)]
    rest = [make_block("sys", tier="system", kind="system_text", role=None, est_tokens=3_000),
            make_block("m0", est_tokens=3_000)]
    return make_fingerprint([*tools, *rest])


def healthy_lanes(team: str = "core") -> list[Lane]:
    """The healthy control: Sonnet 5 Claude Code main lanes at effort medium, every gap within the
    5m TTL, contexts below 150k, a Sonnet subagent."""
    out = []
    for d in range(3):
        rows = [(i * 40, 20_000 + 3_000 * (i - 1) if i else 0, 3_000 if i else 20_000, 0, 5,
                 400) for i in range(8)]
        evs = [event(f"{team}-{d}", 0, "human_prompt")]
        out.append(lane(f"{team}-{d}", rows, model=SONNET5, team=team, principal=f"r_{team}{d}",
                        events=evs, per_request={i: {"effort": "medium",
                                                     "session_effort": "medium"}
                                                 for i in range(8)}))
    out.append(lane(f"{team}-sub", [(20, 0, 15_000, 0, 5, 300), (40, 15_000, 2_000, 0, 5, 300)],
                    kind=LaneKind.SUBAGENT, model=SONNET5, team=team, principal=f"r_{team}0"))
    return out


def mixed_fleet() -> list[Lane]:
    """Lanes of eight teams planting every DETECT-OTHER finding kind (with ``min_usd`` 0.01),
    plus the healthy ``core`` control."""
    from tokenbill.core.builders import make_lane as _make_lane

    lanes: list[Lane] = []
    # search (seat allowance): big contexts, 1M-context main lanes (size tax, compaction window)
    for d in range(2):
        rows = [(0, 0, 150_000, 0, 0, 1_000), (30, 150_000, 100_000, 0, 0, 1_000),
                (60, 250_000, 200_000, 0, 0, 1_000)]
        lanes.append(lane(f"search-{d}", rows, model=SONNET5, team="search",
                          principal=f"r_search{d}", billing_path="subscription"))
    # infra: five developers with sticky fast mode, a US-geo lane, tool output carried along
    for p in range(5):
        for day in range(6):
            lanes.append(lane(f"infra-{p}-{day}", [(day * 86_400 + 3_600, 0, 10_000, 0, 5, 500)],
                              team="infra", principal=f"r_infra{p}",
                              per_request={0: {"speed": "fast", "session_effort": "high"}}))
    carry_rows = [(0, 0, 20_000, 0, 0, 500), (30, 20_000, 3_000, 0, 0, 500),
                  (60, 23_000, 3_000, 0, 0, 500), (90, 26_000, 3_000, 0, 0, 500)]
    tool = [AppendedItem(kind="tool_result", name="Read", n_bytes=50_000)]
    injections = [event("infra-carry", -5, "context_injection", att_type="claude_md",
                        n_bytes=25_000)]
    lanes.append(lane("infra-carry", carry_rows, team="infra", principal="r_infra0",
                      events=injections, per_request={1: {"appended": tool},
                                                      2: {"appended": tool}}))
    lanes.append(lane("infra-geo", [(0, 0, 0, 0, 1_000_000, 500)], team="infra",
                      principal="r_infra1", per_request={0: {"inference_geo": "us"}}))
    # data: top-tier subagents (one Explore), an Opus 5 main lane, high-effort main lanes
    for i, agent in enumerate((None, "Explore")):
        lanes.append(lane(f"data-sub-{i}", [(0, 0, 20_000, 0, 5, 800),
                                            (20, 20_000, 3_000, 0, 5, 800)],
                          kind=LaneKind.SUBAGENT, team="data", principal="r_data0",
                          agent_type=agent))
    lanes.append(lane("data-o5", [(0, 100_000, 2_000, 3_000, 1_000, 500)], model=OPUS5,
                      team="data", principal="r_data1"))
    for i in range(2):
        lanes.append(lane(f"data-main-{i}", [(0, 0, 30_000, 0, 5, 1_000),
                                             (30, 30_000, 4_000, 0, 5, 1_000)],
                          team="data", principal=f"r_data{i}",
                          per_request={0: {"effort": "high", "reasoning": 400},
                                       1: {"effort": "high", "reasoning": 600}}))
    # legacy: a model migration (Opus 4.8 → Opus 5) with +30% output per call
    for day in range(28):
        model, out = (OPUS48, 1_000) if day < 14 else (OPUS5, 1_300)
        rows = [(day * 86_400 + 3_600 + 30 * i, 0, 10_000, 0, 5, out) for i in range(10)]
        lanes.append(lane(f"legacy-{day:02d}", rows, model=model, team="legacy",
                          principal=f"r_legacy{day % 3}"))
    # ops: a runaway loop among 120 small sessions, a regional Bedrock lane
    for i in range(120):
        lanes.append(lane(f"ops-s{i:03d}", [(i * 7_200, 0, 0, 0, 10_000, 1_000)], model=SONNET5,
                          team="ops", principal=f"r_ops{i % 7}"))
    loop_rows = [(1_000_000 + 240 * i, 0, 0, 0, 1_000_000, 0) for i in range(15)]
    lanes.append(lane("ops-loop", loop_rows, team="ops", principal="r_ops9",
                      session_key="s_ops_loop"))
    idle_rows = [(2_000_000 + 70 * i, 0, 0, 0, 10_000, 1_000) for i in range(60)]
    lanes.append(lane("ops-idle", idle_rows, model=SONNET5, team="ops", principal="r_ops8",
                      events=[event("ops-idle", 2_000_000, "human_prompt")],
                      session_key="s_ops_idle"))
    lanes.append(lane("ops-bed", [(0, 0, 0, 0, 1_000_000, 0)], model=OPUS5, team="ops",
                      principal="r_ops1", billing_path="bedrock",
                      per_request={0: {"channel": "bedrock", "endpoint_scope": "regional"}}))
    # ci-bots: CI runs within the TTL, truncated steps, single-shot service calls, a cron agent
    ci = {"workload": WorkloadClass.CI, "entrypoint": "claude-code-github-action",
          "repo": REPO, "extra": (("workflow", WORKFLOW),)}
    for i, start in enumerate((0, 60, 120)):
        lanes.append(lane(f"ci-{i}", [(start, 0, 40_000, 0, 5, 1_000)], model=SONNET5,
                          team="ci-bots", principal="r_bot0", session_key=f"s_ci{i}", **ci))
    trunc = {"stop_reason": "max_tokens", "max_tokens": 16_384}
    retry = {"r": 50_000, "w5": 0, "o": 500}
    rows = [(1_000, 48_000, 2_000, 0, 0, 16_384), (1_060, 0, 0, 0, 0, 0),
            (1_200, 48_000, 2_000, 0, 0, 16_384), (1_260, 0, 0, 0, 0, 0),
            (1_400, 48_000, 2_000, 0, 0, 16_384), (1_460, 0, 0, 0, 0, 0)]
    lanes.append(lane("ci-trunc", rows, model=SONNET5, team="ci-bots", principal="r_bot1",
                      per_request={0: trunc, 1: retry, 2: trunc, 3: retry, 4: trunc, 5: retry},
                      **ci))
    for i in range(3):
        lanes.append(lane(f"ci-single-{i}", [(i * 10, 0, 0, 0, 10_000, 1_000)], model=SONNET5,
                          kind=LaneKind.API_RUN, product="api", team="ci-bots",
                          principal="r_bot2", workload=WorkloadClass.SERVICE))
    cron = [(t, 0, 50_000, 0, 0, 100) for t in (0, 600, 1_205, 1_800, 2_400)]
    lanes.append(lane("ci-cron", cron, kind=LaneKind.API_RUN, product="agent_sdk",
                      team="ci-bots", principal="r_bot3", workload=WorkloadClass.SCHEDULED))
    # agents: tool definitions on every call, the static prefix, retries and tool errors
    fp = _tool_fingerprint()
    rows = [(i * 30, 30_000 if i else 0, 10_000 if i else 40_000, 0, 0, 500) for i in range(5)]
    lanes.append(lane("ag-tools", rows, kind=LaneKind.API_RUN, product="agent_sdk",
                      team="agents", principal="r_ag0", scope="ws:agents",
                      per_request={i: {"fingerprint": fp} for i in range(5)}))
    first = request("ag-retry", 0, 0, w5=100_000, o=500,
                    attr=attribution(team="agents", principal="r_ag1", product="agent_sdk"))
    failed = attempt(100, outcome="http_error", http_status=529, error_type="overloaded",
                     retry_layer="sdk")
    storm = [attempt(200 + i, n=i, outcome="timeout", retry_layer="sdk",
                     usage=usage(u=10_000, o=200), billable=None, usage_source="partial_stream")
             for i in range(3)]
    ok = attempt(500, usage=usage(w5=102_000, o=500), n=4, retry_layer="agent")
    attr = attribution(team="agents", principal="r_ag1", product="agent_sdk")
    lanes.append(_make_lane([first, multi_request("ag-retry", 1, [failed, *storm, ok],
                                                  attr=attr)],
                            kind=LaneKind.API_RUN, lane_key="ag-retry", session_key="s_ag-retry",
                            scope="ws:agents"))
    bad = [AppendedItem(kind="tool_result", name="Bash", n_bytes=300, is_error=True)]
    lanes.append(lane("ag-loop", [(i * 10, 10_000 * i, 10_000, 0, 0, 200) for i in range(5)],
                      kind=LaneKind.API_RUN, product="agent_sdk", team="agents",
                      principal="r_ag2", scope="ws:agents",
                      per_request={i: {"appended": bad} for i in range(1, 5)}))
    never = [attempt(3_000, outcome="http_error", http_status=400, error_type="prompt_too_long"),
             attempt(3_005, n=1, outcome="http_error", http_status=400,
                     error_type="prompt_too_long",
                     usage=usage(u=300_000, o=0), billable=None, usage_source="partial_stream")]
    lanes.append(_make_lane([multi_request("ag-400", 0, never, attr=attr)],
                            kind=LaneKind.API_RUN, lane_key="ag-400", session_key="s_ag-400",
                            scope="ws:agents"))
    lanes.extend(healthy_lanes())
    return lanes
