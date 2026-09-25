"""Area-local fixture builders for the CP-DET-LANES tests (imported only by tests in
``tests/v2/copilot_det_lanes``).

Every fixture is synthetic, built in code with ``core.builders`` (never real traces) and dated
2026-09-10 (every model used has a Copilot row that day). Copilot rates are FakePricer's
(``core/facts.json`` ``copilot.rates``), in nano-USD per token: GPT-5.5 input 5,000, read 500,
output 30,000 — band (total input > 272,000) input 10,000, read 1,000, output 45,000, no write
price (writes fold into input); Claude Sonnet 5 input 2,000, read 200, 5m write 2,500, output
10,000; Claude Opus 4.8 input 5,000, read 500, 5m write 6,250, output 25,000.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from tokenbill.core.builders import make_inference, make_lane, make_request
from tokenbill.core.records import (
    Attribution,
    Inference,
    InferenceKind,
    Lane,
    LaneEvent,
    LaneKind,
    Request,
    RequestParams,
    UsageBuckets,
)
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import AnalysisContext, Finding
from tokenbill.detect.copilot_lanes import CopilotLanes

DAY = "2026-09-10"
T0 = int(dt.datetime(2026, 9, 10, 9, tzinfo=dt.timezone.utc).timestamp()) * 1000
CREDIT = 10_000_000                 # nano-USD per AI credit
USD = 1_000_000_000
GPT55 = "gpt-5.5"
SONNET5 = "claude-sonnet-5"
OPUS48 = "claude-opus-4-8"
VSCODE, CLI, GH_AW = "copilot_vscode", "copilot_cli", "copilot_gh_aw"
PRICER = FakePricer()
DET = CopilotLanes()
#: A VS Code / store-like lane source (per-request usage) behind the copilot extension.
CAPS = frozenset({"credits", "ext:copilot", "usage_sequence", "timing", "params"})
#: The default events-only CLI source: no ``usage_sequence``.
EVENTS_ONLY_CAPS = frozenset({"credits", "ext:copilot", "timing", "events", "lanes_exact",
                              "params", "appended"})


def attr(*, team: str | None = "payments", principal: str | None = "r_dev1",
         product: str = VSCODE, workload: str = "interactive",
         billing_path: str = "copilot_pool", agent_type: str | None = None) -> Attribution:
    """An Attribution of a Copilot lane."""
    return Attribution(principal=principal, team=team, agent_product=product,
                       workload_class=workload, billing_path=billing_path,
                       agent_type=agent_type)


def req(lane_key: str, seq: int, ts_s: float, *, u: int = 0, r: int = 0, w: int = 0,
        o: int = 0, model: str = GPT55, a: Attribution | None = None,
        context_tier: str | None = None, routing: str = "direct",
        write_ttl_hint: str | None = None, extra: Sequence[Inference] = (),
        session_key: str | None = None, **kw: Any) -> Request:
    """One Copilot request at ``T0 + ts_s``: uncached *u*, reads *r*, unknown-TTL writes *w*,
    output *o* on channel ``github_copilot`` (``provider`` / ``channel`` overridable)."""
    a = a if a is not None else attr()
    usage = UsageBuckets(uncached_input=u, cache_read=r, cache_write_unknown=w, output=o)
    kw.setdefault("provider", "github")
    kw.setdefault("channel", "github_copilot")
    return make_request(lane_key, seq, T0 + int(round(ts_s * 1000)), usage, model,
                        attribution=a, params=RequestParams(model_requested=model),
                        billing_path=a.billing_path or "copilot_pool",
                        context_tier=context_tier, routing=routing,
                        write_ttl_hint=write_ttl_hint, extra_inferences=tuple(extra),
                        session_key=session_key or f"s_{lane_key}", **kw)


def compaction_inf(iid: str, *, u: int, o: int, r: int = 0, model: str = SONNET5,
                   billing_path: str = "copilot_pool") -> Inference:
    """An exact COMPACTION inference (``compactionTokensUsed``) on Copilot."""
    return make_inference(UsageBuckets(uncached_input=u, cache_read=r, output=o), model=model,
                          kind=InferenceKind.COMPACTION, inference_id=iid, provider="github",
                          channel="github_copilot", billing_path=billing_path)


def event(lane_key: str, ts_s: float, kind: str, **attrs: Any) -> LaneEvent:
    """A lane event at ``T0 + ts_s``."""
    return LaneEvent(lane_key=lane_key, ts_ms=T0 + int(round(ts_s * 1000)), kind=kind,
                     attrs=tuple(sorted(attrs.items())))


def compaction_event(lane_key: str, ts_s: float, trigger: str | None, *,
                     system: int | None = None, tools: int | None = None) -> LaneEvent:
    """A COMPACTION event with the CLI's ``copilot_trigger`` and static-token attrs."""
    attrs: dict[str, Any] = {"trigger": "manual" if trigger == "manual" else "auto",
                             "pre_tokens": 150_000, "post_tokens": 20_000, "duration_ms": 900,
                             "copilot_trigger": trigger}
    if system is not None:
        attrs["system_tokens"] = system
    if tools is not None:
        attrs["tool_definitions_tokens"] = tools
    return event(lane_key, ts_s, "compaction", **attrs)


def lane(key: str, requests: Sequence[Request], *, kind: LaneKind | str = LaneKind.MAIN,
         events: Iterable[LaneEvent] = (), scope: str = "unknown",
         session_key: str | None = None) -> Lane:
    """A lane over *requests*."""
    return make_lane(list(requests), kind=kind, events=list(events), scope=scope, lane_key=key,
                     session_key=session_key or (requests[0].session_key if requests
                                                 else f"s_{key}"))


def ctx(*, min_usd: str = "1.00", caps: frozenset[str] = CAPS,
        floor: Mapping[tuple[str, str], int] | None = None, self_principal: str | None = None,
        **kw: Any) -> AnalysisContext:
    """An AnalysisContext over FakePricer (no rules / replayer: copilot.lanes needs neither)."""
    thresholds = {"min_usd": min_usd, **kw.pop("thresholds", {})}
    return AnalysisContext(pricer=kw.pop("pricer", PRICER), rules=kw.pop("rules", None),
                           replayer=kw.pop("replayer", None), calibration=None,
                           window=(0, T0 + 30 * 86_400_000), capabilities=caps,
                           thresholds=thresholds, now_ms=T0 + 86_400_000,
                           static_prefix_floor=dict(floor or {}), self_principal=self_principal,
                           **kw)


def run(lanes: Sequence[Lane], c: AnalysisContext | None = None) -> list[Finding]:
    return DET.detect(list(lanes), c if c is not None else ctx())


def only(findings: Sequence[Finding], kind: str, **dims: str) -> list[Finding]:
    """Findings of *kind* whose scope contains every *dims* pair."""
    return [f for f in findings if f.kind == kind
            and all(dict(f.scope.dims).get(k) == v for k, v in dims.items())]


def one(findings: Sequence[Finding], kind: str, **dims: str) -> Finding:
    got = only(findings, kind, **dims)
    assert len(got) == 1, [(f.kind, f.scope.dims) for f in findings]
    return got[0]


def attrs(f: Finding, ref: str) -> dict[str, Any]:
    for item in f.evidence:
        if item.ref == ref:
            return dict(item.attrs)
    raise AssertionError(f"no evidence {ref!r} in {[i.ref for i in f.evidence]}")


def tri(fig: Any) -> tuple[int | None, int | None, int | None]:
    """(low, point, high) of a Figure."""
    return (fig.low_nano, fig.nano, fig.high_nano)
