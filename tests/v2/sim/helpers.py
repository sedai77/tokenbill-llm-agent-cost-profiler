"""Area-local fixture helpers for the REPLAY tests (imported only by tests in ``tests/v2/sim``).

Every fixture is synthetic, built with ``core.builders`` (never real transcripts). Timestamps are
seconds after ``T0_S`` = 2026-09-23 12:00 UTC (FakePricer prices Opus 5.5 from 2026-09-22 and the
promotional gpt-5.6-sol row through 2026-11-21).
"""

from __future__ import annotations

import datetime as _dt
import random
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

from tokenbill.core.builders import (
    lane_from_table,
    make_attempt,
    make_ctx,
    make_inference,
    make_lane,
    make_request,
)
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.labels import Basis, Figure, add, zero
from tokenbill.core.policy import parse_policy
from tokenbill.core.protocols import Pricer
from tokenbill.core.records import (
    Attribution,
    CacheDiagnostic,
    InferenceKind,
    Lane,
    LaneEvent,
    LaneEventKind,
    LaneKind,
    Request,
    RequestParams,
    UsageBuckets,
    UsageSource,
    WorkloadClass,
)
from tokenbill.core.testing import FakePricer
from tokenbill.core.types import CalibrationReport, Policy, ReplayResult
from tokenbill.sim.usage_replay import UsageReplayer

T0_S = int(_dt.datetime(2026, 9, 23, 12, tzinfo=_dt.timezone.utc).timestamp())
T0_MS = T0_S * 1000
DAY_S = 86_400
PRICER = FakePricer()
RULES = RulesTable()
REPLAYER = UsageReplayer()
SDK = {"agent_product": "agent_sdk"}
CC = {"agent_product": "claude_code"}


def usd(text: str) -> int:
    """A USD decimal string → int nano (exact)."""
    value = Decimal(text) * 10**9
    assert value == value.to_integral_value()
    return int(value)


def table(rows: Iterable[Sequence[int]], **kw: Any) -> Lane:
    """``lane_from_table`` with timestamps in seconds after :data:`T0_S`; ``attribution`` dicts
    are accepted as in ``make_request``."""
    return lane_from_table([(T0_S + r[0], *r[1:]) for r in rows], **kw)


def at(seconds: float) -> int:
    """ms timestamp *seconds* after T0."""
    return T0_MS + int(round(seconds * 1000))


def policy(spec: str | Policy) -> Policy:
    return spec if isinstance(spec, Policy) else parse_policy(spec)


def replay(lanes: Lane | Sequence[Lane], spec: str | Policy, *, mode: str = "documented",
           calibration: CalibrationReport | None = None, floor: dict | None = None,
           keep: bool = True, pricer: Pricer = PRICER) -> ReplayResult:
    if isinstance(lanes, Lane):
        lanes = [lanes]
    return REPLAYER.replay(list(lanes), policy(spec), mode=mode, pricer=pricer, rules=RULES,
                           calibration=calibration, static_prefix_floor=floor,
                           keep_outcomes=keep)


def same(a: Figure, b: Figure) -> bool:
    """Equal point and bounds (labels may differ: a policy cost is ESTIMATED)."""
    return (a.nano, a.low_nano, a.high_nano) == (b.nano, b.low_nano, b.high_nano)


def outcomes(result: ReplayResult) -> dict[str, Any]:
    assert result.outcomes is not None
    return {o.request_id: o for o in result.outcomes}


def priced_request(req: Request, pricer: Pricer = PRICER, basis: Basis = Basis.LIST) -> Figure:
    """Σ ``PricedInference.figure`` over the billable inferences of *req* (the ledger)."""
    fig = zero(basis)
    for att in req.attempts:
        for inf in att.inferences:
            if inf.billable is not False:
                fig = add(fig, pricer.price_inference(inf, ts_ms=att.ts_start_ms).figure)
    return fig


def price(usage: UsageBuckets, model: str = "claude-opus-5-5", ts_s: float = 0, **ctx: Any) -> int:
    """Point nano of *usage* on *model* at T0 + *ts_s* (FakePricer, Decimal path)."""
    fig = PRICER.price_usage(usage, make_ctx(model, **ctx), ts_ms=at(ts_s)).figure
    assert fig.nano is not None
    return fig.nano


# ---------------------------------------------------------------------------------------------
# Appendix A lanes
# ---------------------------------------------------------------------------------------------


def a1_lane(**kw: Any) -> Lane:
    """A.1: Opus 5.5 main lane, gaps 420 s, every request a full 5m write."""
    kw.setdefault("attribution", SDK)
    return table([(0, 0, 100_000, 0, 0, 500), (420, 0, 102_000, 0, 0, 500),
                  (840, 0, 104_000, 0, 0, 500), (1260, 0, 106_000, 0, 0, 500)], **kw)


def a2_lane(**kw: Any) -> Lane:
    """A.2: the bursty lane (gaps 30 s, billed as 5m hits)."""
    kw.setdefault("attribution", SDK)
    return table([(0, 0, 100_000, 0, 0, 500), (30, 100_000, 2_000, 0, 0, 500),
                  (60, 102_000, 2_000, 0, 0, 500), (90, 104_000, 2_000, 0, 0, 500)], **kw)


def a2b_lane(**kw: Any) -> Lane:
    """A.2b: the bursty lane billed with 1h writes."""
    kw.setdefault("attribution", SDK)
    return table([(0, 0, 0, 100_000, 0, 500), (30, 100_000, 0, 2_000, 0, 500),
                  (60, 102_000, 0, 2_000, 0, 500), (90, 104_000, 0, 2_000, 0, 500)], **kw)


def a3_lane(**kw: Any) -> Lane:
    """A.3: the A.1 lane billed at 1h (hits every 420 s)."""
    kw.setdefault("attribution", SDK)
    return table([(0, 0, 0, 100_000, 0, 500), (420, 100_000, 0, 2_000, 0, 500),
                  (840, 102_000, 0, 2_000, 0, 500), (1260, 104_000, 0, 2_000, 0, 500)], **kw)


def a6_lane(**kw: Any) -> Lane:
    """A.6: Sonnet 5 main lane, 5m, gaps 30 s, T = 300k, 450k, 500k."""
    kw.setdefault("attribution", CC)
    return table([(0, 0, 300_000, 0, 0, 1_000), (30, 300_000, 150_000, 0, 0, 1_000),
                  (60, 450_000, 50_000, 0, 0, 1_000)], model="claude-sonnet-5", **kw)


# ---------------------------------------------------------------------------------------------
# random lanes (every inference shape the ledger can hold)
# ---------------------------------------------------------------------------------------------

_MODELS = ("claude-opus-5-5", "claude-opus-5-5", "claude-opus-5-5", "claude-sonnet-5",
           "claude-sonnet-4-6", "claude-opus-5", "claude-haiku-4-5", "claude-fable-5-1")
_KINDS = (LaneKind.MAIN, LaneKind.MAIN, LaneKind.SUBAGENT, LaneKind.API_RUN,
          LaneKind.WORKFLOW_AGENT)
_PRODUCTS = ("claude_code", "agent_sdk", "api")
_GAPS = (5, 30, 45, 120, 250, 290, 300, 305, 310, 420, 900, 1200, 3590, 3600, 3610, 5000, 9000)
_EFFORTS = (None, "low", "medium", "high", "xhigh", "max")


def _random_usage(rnd: random.Random, prev_prefix: int, *, hit: bool, ttl: str,
                  growth: int) -> UsageBuckets:
    total = prev_prefix + growth
    u = rnd.choice((0, 0, 0, rnd.randint(1, 300)))
    prefix = max(0, total - u)
    reads = min(prev_prefix, prefix) if hit else rnd.choice((0, 0, min(prev_prefix, prefix) // 3))
    writes = prefix - reads
    w5 = w1 = wu = 0
    if ttl == "5m":
        w5 = writes
    elif ttl == "1h":
        w1 = writes
    elif ttl == "mixed":
        w1 = writes // 2
        w5 = writes - w1
    else:
        wu = writes
    out = rnd.randint(0, 3_000)
    reasoning = rnd.choice((None, None, out // 2))
    return UsageBuckets(uncached_input=u, cache_read=reads, cache_write_5m=w5, cache_write_1h=w1,
                        cache_write_unknown=wu, output=out, output_reasoning=reasoning,
                        web_search_requests=rnd.choice((0, 0, 0, 1)))


def random_lane(rnd: random.Random, key: str, *, billing_path: str | None = None,
                team: str | None = None, n: int | None = None,
                allow_unpriced: bool = True) -> Lane:
    """A random lane: 5m / 1h / mixed / unknown-TTL writes, placeholder output, uncertain billing,
    partial streams, reconstructed usage, refusal fallbacks, compaction and advisor iterations,
    retries, output-residual-only requests, reset events, speed / geo / scope / batch contexts,
    efforts, several models (incl. an unpriced one on some lanes)."""
    kind = rnd.choice(_KINDS)
    product = rnd.choice(_PRODUCTS)
    model = rnd.choice(_MODELS)
    ttl = rnd.choice(("5m", "5m", "1h", "mixed", "unknown"))
    hint = rnd.choice((None, "5m", "1h")) if ttl == "unknown" else None
    channel = "anthropic_api"
    scope = "unknown"
    if rnd.random() < 0.1:
        model, channel, scope = "claude-opus-5", "bedrock", rnd.choice(("unknown", "regional",
                                                                         "global"))
    if rnd.random() < 0.03 and allow_unpriced:
        model = "claude-unknown-model-x"
    speed = "fast" if model in ("claude-opus-5-5", "claude-opus-5") and channel != "bedrock" \
        and rnd.random() < 0.15 else "standard"
    geo = "us" if rnd.random() < 0.1 and channel != "bedrock" else None
    workload = rnd.choice(list(WorkloadClass))
    n = n if n is not None else rnd.randint(1, 9)
    attr = {"agent_product": product, "team": team, "workload_class": workload,
            "principal": "r_u1"}
    if billing_path is not None:
        attr["billing_path"] = billing_path
    ts = T0_MS + rnd.randint(0, 3 * DAY_S) * 1000
    requests: list[Request] = []
    events: list[LaneEvent] = []
    prefix = rnd.choice((0, 5_000, 20_000))
    for seq in range(n):
        if seq:
            ts += rnd.choice(_GAPS) * 1000
            if rnd.random() < 0.08:
                ev_kind = rnd.choice((LaneEventKind.COMPACTION, LaneEventKind.CLEAR))
                attrs = (("post_tokens", rnd.choice((12_000, 15_000, 18_000))),) \
                    if ev_kind is LaneEventKind.COMPACTION and rnd.random() < 0.7 else ()
                events.append(LaneEvent(lane_key=key, ts_ms=ts - 1, kind=ev_kind, attrs=attrs))
        hit = seq > 0 and rnd.random() < 0.7
        usage = _random_usage(rnd, prefix, hit=hit, ttl=ttl, growth=rnd.randint(0, 6_000) + 600)
        prefix = usage.cache_read + usage.cache_write
        source = UsageSource.FINAL
        upper = None
        billable: bool | None = True
        roll = rnd.random()
        if roll < 0.08:
            source, upper = UsageSource.MESSAGE_START_ONLY, usage.output + rnd.randint(0, 900)
        elif roll < 0.12:
            billable = None
        elif roll < 0.15:
            source = UsageSource.PARTIAL_STREAM
        elif roll < 0.18:
            source = UsageSource.ESTIMATED
        ctx_kw: dict[str, Any] = {"channel": channel, "endpoint_scope": scope, "speed": speed,
                                  "inference_geo": geo, "write_ttl_hint": hint}
        if billing_path is not None:
            ctx_kw["billing_path"] = billing_path
        if rnd.random() < 0.05:
            ctx_kw["service_tier"] = "batch"
        req_model = model if rnd.random() > 0.1 or channel == "bedrock" \
            else rnd.choice(_MODELS[:4])
        fallback_model = "claude-opus-5" if channel == "bedrock" else "claude-opus-4-8"
        params = RequestParams(model_requested=req_model, effort=rnd.choice(_EFFORTS),
                               stream=rnd.choice((None, True, False)),
                               output_format=rnd.choice((None, None, None, "set")))
        extra = []
        if rnd.random() < 0.06:
            extra.append(make_inference({"cache_read": 1_000, "output": 300}, model=req_model,
                                        kind=InferenceKind.COMPACTION,
                                        inference_id=f"{key}-c{seq}", **ctx_kw))
        if rnd.random() < 0.05:
            extra.append(make_inference({"uncached_input": 2_000, "output": 200},
                                        model=fallback_model, kind=InferenceKind.ADVISOR,
                                        inference_id=f"{key}-a{seq}", **ctx_kw))
        diag = None
        if rnd.random() < 0.1:
            diag = CacheDiagnostic(reason=rnd.choice(("messages_changed", "system_changed",
                                                      "unavailable")),
                                   provider_reason="x", missed_input_tokens_estimate=None,
                                   source="anthropic.cache_diagnostics")
        if rnd.random() < 0.05:
            # output-residual-only request: no serving inference
            res = make_inference({"output": 700}, model=req_model,
                                 kind=InferenceKind.OUTPUT_RESIDUAL,
                                 inference_id=f"{key}-r{seq}", **ctx_kw)
            requests.append(make_request(key, seq, ts, attribution=attr, params=params,
                                         attempts=[make_attempt([res], ts_ms=ts)]))
            continue
        if rnd.random() < 0.05:
            declined = make_inference({"uncached_input": 100, "output": 0}, model=req_model,
                                      kind=InferenceKind.FALLBACK_DECLINED, billable=False,
                                      inference_id=f"{key}-d{seq}", **ctx_kw)
            serving = make_inference(usage, model=fallback_model, kind=InferenceKind.FALLBACK,
                                     inference_id=f"{key}-f{seq}", **ctx_kw)
            requests.append(make_request(key, seq, ts, attribution=attr, params=params,
                                         attempts=[make_attempt([declined, serving], ts_ms=ts)]))
            continue
        if rnd.random() < 0.06:
            failed = make_attempt([make_inference({"uncached_input": 500, "output": 5},
                                                  model=req_model, billable=None,
                                                  inference_id=f"{key}-x{seq}", **ctx_kw)],
                                  ts_ms=ts, attempt_no=0, outcome="http_error", http_status=529)
            serving = make_inference(usage, model=req_model, inference_id=f"{key}-s{seq}",
                                     usage_source=source, output_upper=upper, billable=billable,
                                     **ctx_kw)
            requests.append(make_request(key, seq, ts, attribution=attr, params=params,
                                         attempts=[failed, make_attempt(
                                             [serving], ts_ms=ts + rnd.choice((2, 400)) * 1000,
                                             attempt_no=1)]))
            continue
        requests.append(make_request(key, seq, ts, usage, req_model, attribution=attr,
                                     params=params, usage_source=source, output_upper=upper,
                                     billable=billable, extra_inferences=extra, diagnostics=diag,
                                     request_id=f"{key}-q{seq}", **ctx_kw))
    return make_lane(requests, kind=kind, events=events, scope=rnd.choice(("ws:a", "ws:b")),
                     lane_key=key, session_key="s_rand")


def random_lanes(seed: int, n: int, **kw: Any) -> list[Lane]:
    rnd = random.Random(seed)
    return [random_lane(rnd, f"L{seed}-{i:03d}", **kw) for i in range(n)]


def attribution(**kw: Any) -> Attribution:
    return Attribution(**kw)
