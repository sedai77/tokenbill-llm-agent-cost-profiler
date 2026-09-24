"""Area-local fixture builders for the DETECT-CACHE tests (imported only by tests in
``tests/v2/detect_cache``).

Every fixture is synthetic, built in code with ``core.builders`` (never real transcripts) and dated
2026-09-23 (Opus 5.5 is priced from 2026-09-22). Rates are FakePricer's (``core/facts.json``); in
nano-USD per token: Opus 5.5 input 4,000, output 20,000, read 200, 5m write 5,000, 1h write 8,000
(fast mode: input 8,000, read 400, 5m write 10,000); Sonnet 5 input 2,000, read 200, 5m write
2,500, 1h write 4,000; Opus 4.8 5m write 6,250.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from tokenbill.core.builders import make_inference, make_lane, make_request
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.labels import Figure
from tokenbill.core.records import (
    Attribution,
    CacheDiagnostic,
    InferenceKind,
    Lane,
    LaneEvent,
    LaneKind,
    Request,
    RequestParams,
    UsageBuckets,
)
from tokenbill.core.testing import FakePricer, FakeReplayer
from tokenbill.core.types import AnalysisContext, Finding, Policy

T0 = 1_790_121_600_000  # 2026-09-23T00:00:00Z
DAY_MS = 86_400_000
OPUS55 = "claude-opus-5-5"
OPUS5 = "claude-opus-5"
OPUS48 = "claude-opus-4-8"
SONNET5 = "claude-sonnet-5"
FABLE5 = "claude-fable-5"
PRICER = FakePricer()
RULES = RulesTable()
CAPS = frozenset({"usage_sequence", "timing", "ttl_split", "events", "attempts", "params",
                  "human_prompts", "lanes_exact", "diagnostics", "appended", "iterations"})
CWD = "h_" + "c" * 20
USD = 10**9

Row = tuple[float, int, int, int, int, int]   # (ts_s, R, W5, W1, U, O)


def attribution(*, team: str | None = "payments", principal: str | None = "r_dev1",
                product: str | None = "claude_code", billing_path: str = "api_key",
                gateway: str | None = None, cwd_key: str | None = CWD,
                client_version: str | None = "2.1.270") -> Attribution:
    """An Attribution with the fields the cache detectors read."""
    extra = (("gateway", gateway),) if gateway else ()
    return Attribution(principal=principal, team=team, agent_product=product,
                       billing_path=billing_path, cwd_key=cwd_key, extra=extra,
                       client_version=client_version)


def request(lane_key: str, seq: int, ts_s: float, *, r: int = 0, w5: int = 0, w1: int = 0,
            u: int = 0, o: int = 500, model: str = OPUS55, attr: Attribution | None = None,
            params: RequestParams | None = None, **kw: Any) -> Request:
    """One request at ``T0 + ts_s`` whose serving inference carries the given buckets."""
    attr = attr if attr is not None else attribution()
    usage = UsageBuckets(cache_read=r, cache_write_5m=w5, cache_write_1h=w1, uncached_input=u,
                         output=o)
    return make_request(lane_key, seq, T0 + int(round(ts_s * 1000)), usage, model,
                        attribution=attr, params=params or RequestParams(model_requested=model),
                        billing_path=attr.billing_path or "unknown", **kw)


def lane(key: str, rows: Iterable[Row], *, kind: LaneKind | str = LaneKind.MAIN,
         events: Sequence[LaneEvent] = (), scope: str = "ws:w1", model: str = OPUS55,
         attr: Attribution | None = None,
         per_request: Mapping[int, Mapping[str, Any]] | None = None, **attr_kw: Any) -> Lane:
    """A lane from ``(ts_s, R, W5, W1, U, O)`` rows; *per_request* overrides request kwargs by
    index (``model``, ``speed``, ``diagnostics``, ``applied_edits``, ``params`` …)."""
    attr = attr if attr is not None else attribution(**attr_kw)
    reqs = []
    for seq, (ts_s, r, w5, w1, u, o) in enumerate(rows):
        extra = dict((per_request or {}).get(seq, {}))
        m = extra.pop("model", model)
        req_attr = extra.pop("attr", attr)
        reqs.append(request(key, seq, ts_s, r=r, w5=w5, w1=w1, u=u, o=o, model=m, attr=req_attr,
                            session_key=f"s_{key}", **extra))
    return make_lane(reqs, kind=kind, events=list(events), scope=scope, lane_key=key,
                     session_key=f"s_{key}")


def event(lane_key: str, ts_s: float, kind: str, **attrs: Any) -> LaneEvent:
    """A lane event at ``T0 + ts_s``."""
    return LaneEvent(lane_key=lane_key, ts_ms=T0 + int(round(ts_s * 1000)), kind=kind,
                     attrs=tuple(sorted(attrs.items())))


def diag(reason: str) -> CacheDiagnostic:
    """An Anthropic cache diagnostic with a canonical reason."""
    return CacheDiagnostic(reason=reason, provider_reason=reason,
                           missed_input_tokens_estimate=None,
                           source="anthropic.cache_diagnostics")


def fallback_request(lane_key: str, seq: int, ts_s: float, *, declined_model: str,
                     w5: int, model: str, attr: Attribution | None = None) -> Request:
    """A refusal-fallback request: a declined attempt (0 output, not billable) then the
    fallback model's serving inference writing *w5*."""
    attr = attr if attr is not None else attribution()
    declined = make_inference({"cache_write_5m": w5, "output": 0}, model=declined_model,
                              kind=InferenceKind.FALLBACK_DECLINED, billable=False,
                              billing_rule_id="anthropic.refusal.pre_output",
                              inference_id=f"inf-declined-{lane_key}-{seq}",
                              billing_path=attr.billing_path or "unknown")
    return make_request(lane_key, seq, T0 + int(round(ts_s * 1000)),
                        {"cache_write_5m": w5, "output": 500}, model,
                        kind=InferenceKind.FALLBACK, extra_inferences=(declined,),
                        attribution=attr, params=RequestParams(model_requested=declined_model),
                        billing_path=attr.billing_path or "unknown", session_key=f"s_{lane_key}")


def ctx(*, replayer: Any = None, thresholds: Mapping[str, str] | None = None,
        caps: frozenset[str] = CAPS, k: int = 5, self_principal: str | None = None,
        pricer: Any = PRICER) -> AnalysisContext:
    """An AnalysisContext over the FakePricer and the core rules table."""
    return AnalysisContext(pricer=pricer, rules=RULES, replayer=replayer, calibration=None,
                           window=(T0, T0 + 30 * DAY_MS), capabilities=caps,
                           thresholds=dict(thresholds or {}), k_anonymity=k,
                           self_principal=self_principal, now_ms=T0 + 30 * DAY_MS)


def table_replayer(values: Mapping[tuple[str, str], int]) -> FakeReplayer:
    """A FakeReplayer keyed by (lane key, canonical policy spec)."""
    return FakeReplayer(dict(values))


def fn_replayer(fn: Callable[[Lane, Policy], int]) -> FakeReplayer:
    """A FakeReplayer whose per-lane saving is a function."""
    return FakeReplayer.from_function(fn)


def only(findings: Sequence[Finding], kind: str) -> Finding:
    """The single finding of *kind* (AssertionError otherwise)."""
    got = [f for f in findings if f.kind == kind]
    assert len(got) == 1, [f.kind for f in findings]
    return got[0]


def nano(fig: Figure | None) -> int | None:
    """The point of a figure (None for None)."""
    return None if fig is None else fig.nano


# ---------------------------------------------------------------------------------------------
# SPEC Appendix A lanes
# ---------------------------------------------------------------------------------------------


def lane_a1(key: str = "A1", **kw: Any) -> Lane:
    """A.1: Opus 5.5 main lane, 4 requests 420 s apart, full 5m rewrites of 100k..106k, U = 0,
    O = 500 each. Observed $2.10; ``ttl=1h`` $0.9492 (saving $1.1508)."""
    rows = [(0, 0, 100_000, 0, 0, 500), (420, 0, 102_000, 0, 0, 500),
            (840, 0, 104_000, 0, 0, 500), (1260, 0, 106_000, 0, 0, 500)]
    return lane(key, rows, **kw)


def lane_a2(key: str = "A2", *, hour: bool = False, **kw: Any) -> Lane:
    """A.2 (5m) / A.2b (``hour=True``, billed 1h): bursty hits 30 s apart. A.2 $0.6312; A.2b
    $0.9492 (``ttl=5m`` saves $0.318)."""
    def w(n: int) -> tuple[int, int]:
        return (0, n) if hour else (n, 0)
    rows = [(0, 0, *w(100_000), 0, 500), (30, 100_000, *w(2_000), 0, 500),
            (60, 102_000, *w(2_000), 0, 500), (90, 104_000, *w(2_000), 0, 500)]
    return lane(key, rows, **kw)


def lane_a5(key: str = "A5", **kw: Any) -> Lane:
    """A.5: Opus 5.5 main lane billed 1h, context 500,000, gaps [30 s, 2 h, 90 s]: exactly one
    cold resume ($4.00 billed, $3.90 premium)."""
    rows = [(0, 0, 0, 500_000, 0, 500), (30, 500_000, 0, 0, 0, 500),
            (7230, 0, 0, 500_000, 0, 500), (7320, 500_000, 0, 0, 0, 500)]
    return lane(key, rows, **kw)


def lane_a10(key: str = "A10", **kw: Any) -> Lane:
    """A.10: Opus 5.5 SDK lane (api_run), 5m. Request 1 has T = 120,000; request 2 (30 s later,
    warm) clears X = 40,000 and bills R = 20,000, W = S = 62,000; five more requests follow
    (K_rem = 5). $0.31 billed, $0.2576 net loss, K* = 37.2."""
    rows = [(0, 0, 100_000, 0, 0, 500), (30, 100_000, 20_000, 0, 0, 500),
            (60, 20_000, 62_000, 0, 0, 500)]
    prefix = 82_000
    for k in range(5):
        rows.append((90 + 30 * k, prefix, 2_000, 0, 0, 500))
        prefix += 2_000
    kw.setdefault("kind", LaneKind.API_RUN)
    kw.setdefault("product", "agent_sdk")
    kw.setdefault("team", "agents")
    return lane(key, rows, per_request={2: {"applied_edits": (("clear_tool_uses_20250919",
                                                                40_000),)}}, **kw)


def healthy_lanes(team: str = "core", n: int = 6) -> list[Lane]:
    """Healthy-control lanes: Sonnet 5 Claude Code main lanes, every gap within the 5m TTL, each
    request reading the whole previous prefix and writing only the appended tokens."""
    out = []
    for d in range(n):
        rows: list[Row] = []
        prefix = 0
        for i in range(12):
            if i == 0:
                rows.append((0, 0, 20_000, 0, 3, 600))
                prefix = 20_000
            else:
                rows.append((i * 45 + d, prefix, 2_500, 0, 3, 600))
                prefix += 2_500
        out.append(lane(f"H-{team}-{d}", rows, model=SONNET5, team=team,
                        principal=f"r_{team}{d}"))
    return out
