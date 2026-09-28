"""Seeded synthetic lanes, Appendix A closed forms, rollout panels and A/B campaigns
(SPEC §9.8, Appendix A, §13; SYNTH-ORACLE).

* :func:`random_lanes` — ``n`` seeded random lanes of one *family* (the policy family they
  exercise), covering every inference shape the ledger can hold: placeholder output, unknown-TTL
  writes, refusal iterations (declined 0 / ambiguous 6-token outputs), retried attempts, compaction,
  advisor and other iterations, reconstructed (ESTIMATED) iterations, output-residual-only requests,
  server-tool counters, lane events and parameter changes. Allowance (subscription) lanes are a
  family of their own (one billing class per replay, SPEC §9.1 #5).
* :func:`family_policies` — the policies the differential test replays on each family.
* :func:`closed_form` — the Appendix A fixtures with their hand-computed expectations in nano.
* :func:`rollout_panel` / :func:`rollout_truth` — stepped-wedge (or org-wide) panels with a known
  effect for the verification estimators (§13.1–§13.3).
* :func:`ab_campaign` — a paired lab campaign with a planted token / turn / cost shape (§13.5).

Everything is deterministic per seed (randomness only via ``common.rng``) and integer or exact
(money never float). Timestamps start at 2026-09-23 00:00 UTC, where every model of
``core.testing.FakePricer`` has an enabled rate row.
"""

from __future__ import annotations

import datetime as _dt
import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import Any

from tokenbill.common import rng
from tokenbill.core.builders import (
    lane_from_table,
    make_attempt,
    make_inference,
    make_lane,
    make_request,
)
from tokenbill.core.errors import UsageError
from tokenbill.core.evidence import CC_FLEET_USD_PER_ACTIVE_DAY
from tokenbill.core.ids import stable_id
from tokenbill.core.labels import Figure
from tokenbill.core.policy import parse_policy
from tokenbill.core.protocols import Pricer
from tokenbill.core.records import (
    Attempt,
    Attribution,
    Inference,
    InferenceKind,
    Lane,
    LaneEvent,
    LaneEventKind,
    LaneKind,
    Outcome,
    Request,
    RequestParams,
    UsageBuckets,
    UsageSource,
    WorkloadClass,
)
from tokenbill.core.types import PanelRow, Policy

__all__ = [
    "CLOSED_FORMS",
    "EPOCH_MS",
    "FAMILIES",
    "ab_campaign",
    "closed_form",
    "family_policies",
    "random_lanes",
    "rollout_panel",
    "rollout_truth",
]

EPOCH_MS = 1_790_121_600_000          # 2026-09-23T00:00:00Z
_DAY_MS = 86_400_000
_MIN_MS = 60_000
_HOUR_MS = 3_600_000

FAMILIES = ("ttl", "keepalive", "compaction", "cold_resume", "remap", "effort", "rates", "batch",
            "repairs", "placeholder", "unknown_ttl", "allowance")

#: The last policy of several families is a joint policy (the §9.3 application order composes
#: levers; PLAN's Shapley credits come from joint replays), chosen so every lane stays priced.
_FAMILY_SPECS: dict[str, tuple[str, ...]] = {
    "ttl": ("ttl=1h", "ttl=5m", "ttl=1h@lane_kind:main", "ttl=5m@agent_product:agent_sdk",
            "ttl=1h@lane_kind:main;keepalive=240s@agent_product:agent_sdk;fast=off;geo=global;"
            "regional=global"),
    "keepalive": ("keepalive=240s", "keepalive=240s,max=1800s@agent_product:agent_sdk",
                  "keepalive=120s,max=600s", "keepalive=240s;effort=medium;repair=fallback_credit"),
    "compaction": ("compact-window=400000", "compact-window=300000,post=25000",
                   "compact-window=600000,post=20283",
                   "ttl=1h;compact-window=400000;cold-resume=compact;effort=high"),
    "cold_resume": ("cold-resume=compact", "cold-resume=clear",
                    "cold-resume=compact,min=100000",
                    "ttl=5m;cold-resume=clear,min=100000;repair=retry_backoff_cap"),
    "remap": ("model=claude-sonnet-5", "model=claude-haiku-4-5@lane_kind:subagent",
              "model=claude-opus-5-5@agent_product:agent_sdk", "model=claude-sonnet-4-6",
              "ttl=1h;model=claude-sonnet-5@lane_kind:main;effort=medium"),
    "effort": ("effort=medium", "effort=high,scale=0.25@lane_kind:main",
               "effort=low,scale=0.75@agent_product:agent_sdk",
               "effort=low@lane_kind:main;effort=medium;ttl=1h"),
    "rates": ("fast=off", "geo=global", "regional=global", "fast=off;geo=global;regional=global"),
    "batch": ("batch=eligible", "batch=eligible;fast=off"),
    "repairs": ("repair=restore_caching", "repair=stagger_fanout", "repair=retry_backoff_cap",
                "repair=fallback_credit", "repair=shared_ci_prefix",
                "repair=fallback_credit;repair=restore_caching;repair=retry_backoff_cap;"
                "repair=shared_ci_prefix;repair=stagger_fanout"),
    "placeholder": ("ttl=1h", "ttl=5m", "model=claude-sonnet-5", "effort=medium"),
    "unknown_ttl": ("ttl=1h", "ttl=5m", "keepalive=240s", "compact-window=400000"),
    "allowance": ("ttl=1h", "ttl=5m", "compact-window=400000", "cold-resume=compact"),
}


def family_policies(family: str) -> tuple[Policy, ...]:
    """The policies the differential test replays on *family* (the observed policy first)."""
    if family not in _FAMILY_SPECS:
        raise UsageError(f"unknown lane family {family!r}")
    return (Policy.observed(), *(parse_policy(spec) for spec in _FAMILY_SPECS[family]))


# =============================================================================================
# request specs and builders
# =============================================================================================

_MODELS_NEW = ("claude-opus-5-5", "claude-sonnet-5", "claude-opus-5", "claude-opus-4-8",
               "claude-fable-5-1")
_MODELS_LEGACY = ("claude-sonnet-4-6", "claude-haiku-4-5")
_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_WRITE_BUCKET = {"5m": "cache_write_5m", "1h": "cache_write_1h", "unknown": "cache_write_unknown"}


@dataclass
class _Spec:
    """One request to build: its serving usage, context and shape."""

    ts_ms: int
    model: str
    reads: int
    writes: int
    uncached: int
    output: int
    write_class: str = "5m"
    reasoning: int | None = None
    ctx: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    attribution: dict[str, Any] = field(default_factory=dict)
    shape: str = "plain"
    applied_edits: tuple[tuple[str, int], ...] = ()
    stop_reason: str | None = None
    web: int = 0


def _usage(spec: _Spec) -> UsageBuckets:
    buckets = {"uncached_input": spec.uncached, "cache_read": spec.reads, "output": spec.output,
               "output_reasoning": spec.reasoning, "web_search_requests": spec.web}
    if spec.writes:
        buckets[_WRITE_BUCKET[spec.write_class]] = spec.writes
    return UsageBuckets(**buckets)


def _h(rnd: random.Random) -> str:
    return "h_" + "".join(rnd.choice("0123456789abcdef") for _ in range(20))


def _build(lane_key: str, session_key: str, seq: int, spec: _Spec, base_attr: dict[str, Any],
           rnd: random.Random) -> Request:
    """A Request for *spec* in the shape it names (see the module docstring)."""
    attr = Attribution(**{**base_attr, **spec.attribution})
    params = RequestParams(model_requested=spec.model, **spec.params)
    usage = _usage(spec)
    rid = stable_id("rq", lane_key, seq)
    ctx_kw = dict(spec.ctx)
    common = {"request_id": rid, "session_key": session_key, "attribution": attr,
              "params": params, "stop_reason": spec.stop_reason,
              "applied_edits": spec.applied_edits}
    shape = spec.shape
    if shape == "placeholder":
        upper = spec.output + rnd.randint(200, 4_000)
        return make_request(lane_key, seq, spec.ts_ms, usage, spec.model,
                            usage_source=UsageSource.MESSAGE_START_ONLY, output_upper=upper,
                            **common, **ctx_kw)
    if shape in ("refusal", "refusal_ambiguous"):
        declined_out = 0 if shape == "refusal" else 6
        declined_model = "claude-fable-5-1"
        declined = make_inference(
            {"cache_read": spec.reads, "cache_write_5m": 0, "uncached_input": spec.uncached,
             "output": declined_out},
            model=declined_model, kind=InferenceKind.FALLBACK_DECLINED,
            inference_id=stable_id("inf", rid, "declined"),
            billable=False if shape == "refusal" else None,
            billing_rule_id="anthropic.refusal.pre_output" if shape == "refusal"
            else "anthropic.refusal.ambiguous", **ctx_kw)
        serving = make_inference(usage, model=spec.model, kind=InferenceKind.FALLBACK,
                                 inference_id=stable_id("inf", rid, 0), **ctx_kw)
        att = make_attempt((declined, serving), ts_ms=spec.ts_ms,
                           attempt_id=stable_id("at", rid, 0), model_served=spec.model,
                           stop_reason=spec.stop_reason)
        return make_request(lane_key, seq, spec.ts_ms, attempts=(att,), **common)
    if shape in ("retry", "retry_long"):
        attempts: list[Attempt] = []
        n_failed = rnd.randint(1, 2) if shape == "retry" else rnd.randint(3, 4)
        step = rnd.randint(2_000, 40_000) if shape == "retry" else rnd.randint(90_000, 200_000)
        for k in range(n_failed):
            infs: tuple[Inference, ...] = ()
            if rnd.random() < 0.5:
                infs = (make_inference(
                    {"uncached_input": spec.uncached, "cache_read": spec.reads,
                     "output": rnd.randint(0, 40)},
                    model=spec.model, inference_id=stable_id("inf", rid, "failed", k),
                    usage_source=UsageSource.PARTIAL_STREAM, billable=None,
                    billing_rule_id="anthropic.overloaded.mid_stream", **ctx_kw),)
            attempts.append(make_attempt(
                infs, ts_ms=spec.ts_ms + k * step, attempt_no=k,
                attempt_id=stable_id("at", rid, k), outcome=Outcome.HTTP_ERROR,
                http_status=529, error_type="overloaded"))
        final_ts = spec.ts_ms + n_failed * step
        serving = make_inference(usage, model=spec.model, inference_id=stable_id("inf", rid, 0),
                                 **ctx_kw)
        attempts.append(make_attempt((serving,), ts_ms=final_ts, attempt_no=n_failed,
                                     attempt_id=stable_id("at", rid, n_failed),
                                     model_served=spec.model, stop_reason=spec.stop_reason))
        return make_request(lane_key, seq, spec.ts_ms, attempts=tuple(attempts), **common)
    if shape == "residual_only":
        residual = make_inference({"output": spec.output}, model=spec.model,
                                  kind=InferenceKind.OUTPUT_RESIDUAL,
                                  inference_id=stable_id("inf", rid, "residual"), **ctx_kw)
        att = make_attempt((residual,), ts_ms=spec.ts_ms, attempt_id=stable_id("at", rid, 0))
        return make_request(lane_key, seq, spec.ts_ms, attempts=(att,), **common)
    extra: list[Inference] = []
    if shape == "compaction_iter":
        extra.append(make_inference(
            {"cache_read": spec.reads, "cache_write_5m": rnd.randint(1_000, 8_000),
             "output": rnd.randint(2_000, 20_000)},
            model=spec.model, kind=InferenceKind.COMPACTION,
            inference_id=stable_id("inf", rid, "compaction"), **ctx_kw))
    elif shape == "advisor":
        extra.append(make_inference(
            {"uncached_input": rnd.randint(2_000, 30_000), "output": rnd.randint(100, 2_000)},
            model="claude-opus-5-5", kind=InferenceKind.ADVISOR,
            inference_id=stable_id("inf", rid, "advisor"), **ctx_kw))
    elif shape == "other_iter":
        extra.append(make_inference(
            {"uncached_input": rnd.randint(500, 8_000), "output": rnd.randint(10, 800)},
            model=spec.model, kind=InferenceKind.OTHER,
            inference_id=stable_id("inf", rid, "other"), **ctx_kw))
    elif shape == "estimated_iter":
        extra.append(make_inference(
            {"cache_read": rnd.randint(1_000, 20_000), "output": rnd.randint(500, 5_000)},
            model=spec.model, kind=InferenceKind.COMPACTION,
            inference_id=stable_id("inf", rid, "estimated"),
            usage_source=UsageSource.ESTIMATED, **ctx_kw))
    return make_request(lane_key, seq, spec.ts_ms, usage, spec.model, extra_inferences=extra,
                        **common, **ctx_kw)


def _lane(lane_key: str, session_key: str, specs: Sequence[_Spec], base_attr: dict[str, Any],
          rnd: random.Random, *, kind: LaneKind, scope: str,
          events: Sequence[LaneEvent] = ()) -> Lane:
    requests = [_build(lane_key, session_key, seq, spec, base_attr, rnd)
                for seq, spec in enumerate(specs)]
    return make_lane(requests, kind=kind, events=events, scope=scope, lane_key=lane_key,
                     session_key=session_key)


# =============================================================================================
# the conversation walk: realistic cache behavior under a true TTL
# =============================================================================================


def _gap_ms(rnd: random.Random, mix: str) -> int:
    """One inter-request gap (ms) from a named mixture; the mixtures put mass on the ±10 s
    ambiguity zones around 5 minutes and 1 hour and on their exact edges."""
    x = rnd.random()
    if mix == "short":
        return rnd.randint(1_000, 120_000) if x < 0.9 else rnd.randint(120_000, 290_000)
    if mix == "keepalive":
        if x < 0.25:
            return rnd.randint(5_000, 240_000)
        if x < 0.35:
            return rnd.choice((240_000, 290_000, 300_000, 310_000, 1_200_000, 1_260_000))
        if x < 0.85:
            return rnd.randint(240_000, 70 * _MIN_MS)
        return rnd.randint(70 * _MIN_MS, 150 * _MIN_MS)
    if mix == "idle":
        if x < 0.55:
            return rnd.randint(2_000, 200_000)
        if x < 0.65:
            return rnd.randint(3_590_000, 3_610_000)
        return rnd.randint(_HOUR_MS, 8 * _HOUR_MS)
    # "mixed"
    if x < 0.38:
        return rnd.randint(1_000, 120_000)
    if x < 0.50:
        return rnd.randint(289_000, 311_000)
    if x < 0.55:
        return rnd.choice((290_000, 300_000, 310_000, 310_001, 289_999, 3_590_000, 3_600_000,
                           3_610_000, 3_610_001))
    if x < 0.78:
        return rnd.randint(311_000, _HOUR_MS)
    if x < 0.86:
        return rnd.randint(3_589_000, 3_611_000)
    return rnd.randint(_HOUR_MS, 5 * _HOUR_MS)


@dataclass
class _Walk:
    """Parameters of one generated conversation."""

    n: int
    t0_ms: int
    model: str = "claude-opus-5-5"
    ttl: str = "5m"                    # the true TTL deciding observed hits
    write_class: str = "5m"            # how the source reports writes ("unknown" = no split)
    start: tuple[int, int] = (6_000, 60_000)
    growth: tuple[int, int] = (300, 8_000)
    gaps: str = "mixed"
    static_prefix: int = 0
    ctx: dict[str, Any] = field(default_factory=dict)
    shapes: dict[str, float] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    output: tuple[int, int] = (50, 3_000)
    uncached: tuple[int, int] = (0, 60)
    big_uncached: float = 0.05
    models: tuple[str, ...] = ()       # when set: the model can switch between these
    switch_p: float = 0.0
    reasoning_p: float = 0.3
    compaction_p: float = 0.0
    clear_p: float = 0.0
    shrink_p: float = 0.02
    noise_p: float = 0.04


def _walk(rnd: random.Random, lane_key: str, w: _Walk) -> tuple[list[_Spec], list[LaneEvent]]:
    """Specs and events of one conversation: the context grows, the cache is hit while the prefix
    is alive under the true TTL (same model, no reset), misses otherwise, with a little noise
    (partial hits, unexplained misses, shrinks)."""
    specs: list[_Spec] = []
    events: list[LaneEvent] = []
    ttl_ms = 3_600_000 if w.ttl == "1h" else 300_000
    ts = w.t0_ms
    model = w.model
    total = rnd.randint(*w.start)
    prefix = 0
    shapes = list(w.shapes.items())
    if w.ctx.get("channel") == "bedrock":   # one Bedrock model is priced: no model-changing shapes
        shapes = [(k, p) for k, p in shapes if k not in ("refusal", "refusal_ambiguous", "advisor")]
    for i in range(w.n):
        gap = 0
        reset = False
        if i > 0:
            gap = _gap_ms(rnd, w.gaps)
            ts += gap
            total += rnd.randint(*w.growth)
            if w.models and rnd.random() < w.switch_p:
                model = rnd.choice(w.models)
            if rnd.random() < w.compaction_p and total > 60_000:
                post = rnd.randint(8_000, 40_000)
                events.append(LaneEvent(lane_key, ts - rnd.randint(1, max(1, gap - 1)),
                                        LaneEventKind.COMPACTION,
                                        (("post_tokens", post), ("pre_tokens", total),
                                         ("trigger", rnd.choice(("auto", "manual"))))))
                total = post + rnd.randint(500, 5_000)
                reset = True
            elif rnd.random() < w.clear_p:
                events.append(LaneEvent(lane_key, ts - rnd.randint(1, max(1, gap - 1)),
                                        LaneEventKind.CLEAR))
                total = rnd.randint(*w.start)
                reset = True
            elif rnd.random() < w.shrink_p:
                total = max(w.start[0], total // rnd.randint(2, 3))
        uncached = rnd.randint(*w.uncached)
        if rnd.random() < w.big_uncached:
            uncached += rnd.randint(1_000, 20_000)
        total = max(total, uncached + 1_200)
        prev_model = specs[-1].model if specs else model
        alive = i > 0 and gap <= ttl_ms and not reset and model == prev_model and prefix > 0
        if alive and rnd.random() > w.noise_p:
            reads = min(prefix, total - uncached)
            if rnd.random() < 0.08:
                reads = max(0, reads - rnd.randint(100, 3_000))   # partial hit
        else:
            reads = min(w.static_prefix, total - uncached)
        writes = total - uncached - reads
        out = rnd.randint(*w.output)
        reasoning = rnd.randint(0, out) if rnd.random() < w.reasoning_p else None
        shape = "plain"
        x = rnd.random()
        for name, p in shapes:
            if x < p:
                shape = name
                break
            x -= p
        spec = _Spec(ts_ms=ts, model=model, reads=reads, writes=writes, uncached=uncached,
                     output=out, write_class=w.write_class, reasoning=reasoning,
                     ctx=dict(w.ctx), params=dict(w.params), shape=shape)
        if shape == "web":
            spec.web = rnd.randint(1, 3)
        if shape in ("refusal", "refusal_ambiguous") and i > 0:
            spec.model = rnd.choice(("claude-opus-4-8", "claude-opus-5"))
            spec.reads = min(w.static_prefix, total - uncached)
            spec.writes = total - uncached - spec.reads
            model = spec.model
        elif shape in ("refusal", "refusal_ambiguous"):
            spec.shape = "plain"
        if shape == "residual_only" and i == 0:
            spec.shape = "plain"
        specs.append(spec)
        if spec.shape != "residual_only":
            prefix = spec.reads + spec.writes
    return specs, events


def _t0(rnd: random.Random) -> int:
    return EPOCH_MS + rnd.randint(0, 20 * _HOUR_MS)


def _names(family: str, seed: int, i: int) -> tuple[str, str]:
    return f"{family}-{seed}-{i:04d}", f"s_{family}-{seed}-{i:04d}"


_COMMON_SHAPES = {"placeholder": 0.02, "refusal": 0.015, "refusal_ambiguous": 0.01,
                  "retry": 0.02, "compaction_iter": 0.01, "advisor": 0.01,
                  "estimated_iter": 0.01, "residual_only": 0.01, "web": 0.02,
                  "other_iter": 0.01}


# =============================================================================================
# families
# =============================================================================================


def _ttl_lane(rnd: random.Random, key: str, skey: str, *, allowance: bool = False) -> Lane:
    kind = rnd.choice((LaneKind.MAIN, LaneKind.MAIN, LaneKind.SUBAGENT, LaneKind.API_RUN))
    product = "agent_sdk" if kind is LaneKind.API_RUN else rnd.choice(("claude_code",
                                                                      "claude_code",
                                                                      "agent_sdk"))
    ttl = rnd.choice(("5m", "5m", "1h"))
    x = rnd.random()
    ctx: dict[str, Any] = {}
    model = rnd.choice(("claude-opus-5-5", "claude-opus-5-5", "claude-sonnet-5", "claude-opus-5",
                        "claude-haiku-4-5", "claude-fable-5-1"))
    start = (6_000, 80_000)
    if model == "claude-haiku-4-5":
        start = (9_000, 60_000)
    if x < 0.08 and not allowance:
        model, ctx = "claude-opus-5", {"channel": "bedrock",
                                       "endpoint_scope": rnd.choice(("global", "regional"))}
    elif x < 0.12 and not allowance:
        return _openai_lane(rnd, key, skey)
    if allowance:
        ctx["billing_path"] = "subscription"
        product = "claude_code"
    walk = _Walk(n=rnd.randint(2, 12), t0_ms=_t0(rnd), model=model, ttl=ttl, write_class=ttl,
                 start=start, ctx=ctx, shapes=_COMMON_SHAPES,
                 static_prefix=rnd.choice((0, 0, 4_000)))
    specs, events = _walk(rnd, key, walk)
    attr = {"agent_product": product, "team": rnd.choice(("t1", "t2")),
            "principal": rnd.choice(("r_u1", "r_u2", None))}
    if allowance:
        attr["billing_path"] = "subscription"
    return _lane(key, skey, specs, attr, rnd, kind=kind, scope="ws:w1", events=events)


def _openai_lane(rnd: random.Random, key: str, skey: str) -> Lane:
    """An OpenAI lane (reads and uncached input only): TTL policies must skip it."""
    specs = []
    ts = _t0(rnd)
    total = rnd.randint(5_000, 40_000)
    for i in range(rnd.randint(2, 6)):
        if i:
            ts += rnd.randint(5_000, 900_000)
            total += rnd.randint(200, 3_000)
        reads = 0 if i == 0 else (total // 1024) * 1024 - 1024
        specs.append(_Spec(ts_ms=ts, model="gpt-5.6-sol", reads=max(0, reads), writes=0,
                           uncached=total - max(0, reads), output=rnd.randint(50, 900)))
    return _lane(key, skey, specs, {"agent_product": "api"}, rnd, kind=LaneKind.API_RUN,
                 scope="org:openai_api:acct")


def _keepalive_lane(rnd: random.Random, key: str, skey: str) -> Lane:
    x = rnd.random()
    params: dict[str, Any] = {}
    product = "agent_sdk"
    kind = LaneKind.API_RUN
    if x >= 0.97:   # an OpenAI agent lane: keepalive (Anthropic's mechanism) must skip it
        return _openai_lane(rnd, key, skey)
    if x < 0.12:
        product, kind = "claude_code", LaneKind.MAIN
    elif x < 0.17:
        params = {"output_format": "set"}
    elif x < 0.21:
        params = {"tool_choice": rnd.choice(("any", "tool:h_0123456789abcdef0123"))}
    elif x < 0.24:
        params = {"thinking": "enabled:4096"}
    elif x < 0.28:
        params = {"tool_choice": "auto", "thinking": "adaptive"}
    ttl = "1h" if rnd.random() < 0.15 else "5m"
    walk = _Walk(n=rnd.randint(2, 10), t0_ms=_t0(rnd), ttl=ttl, write_class=ttl,
                 model=rnd.choice(("claude-opus-5-5", "claude-sonnet-5", "claude-fable-5-1")),
                 gaps="keepalive", params=params, shapes=_COMMON_SHAPES, start=(20_000, 150_000))
    specs, events = _walk(rnd, key, walk)
    return _lane(key, skey, specs, {"agent_product": product, "team": "agents"}, rnd, kind=kind,
                 scope="ws:w1", events=events)


def _compaction_lane(rnd: random.Random, key: str, skey: str, *,
                     allowance: bool = False) -> Lane:
    model = rnd.choice(("claude-sonnet-5", "claude-opus-5-5", "claude-opus-5-5",
                        "claude-haiku-4-5"))
    ttl = rnd.choice(("5m", "5m", "1h"))
    kind = LaneKind.MAIN if rnd.random() < 0.85 else LaneKind.SUBAGENT
    ctx = {"billing_path": "subscription"} if allowance else {}
    walk = _Walk(n=rnd.randint(2, 14), t0_ms=_t0(rnd), model=model, ttl=ttl, write_class=ttl,
                 start=(120_000, 420_000), growth=(0, 90_000), gaps="short",
                 compaction_p=0.08, clear_p=0.03, shrink_p=0.03, ctx=ctx,
                 shapes={"compaction_iter": 0.03, "placeholder": 0.02, "retry": 0.02,
                         "estimated_iter": 0.02})
    if rnd.random() < 0.3:
        walk.gaps = "mixed"
    specs, events = _walk(rnd, key, walk)
    attr: dict[str, Any] = {"agent_product": "claude_code", "team": "search"}
    if allowance:
        attr["billing_path"] = "subscription"
    return _lane(key, skey, specs, attr, rnd, kind=kind, scope="ws:w1", events=events)


def _cold_resume_lane(rnd: random.Random, key: str, skey: str) -> Lane:
    ttl = rnd.choice(("5m", "1h", "1h"))
    kind = LaneKind.MAIN if rnd.random() < 0.85 else LaneKind.SUBAGENT
    walk = _Walk(n=rnd.randint(2, 10), t0_ms=_t0(rnd),
                 model=rnd.choice(("claude-opus-5-5", "claude-sonnet-5", "claude-opus-4-8")),
                 ttl=ttl, write_class=ttl, start=(60_000, 600_000), growth=(0, 30_000),
                 gaps="idle", compaction_p=0.04, clear_p=0.02,
                 shapes={"placeholder": 0.02, "retry": 0.02, "web": 0.02})
    specs, events = _walk(rnd, key, walk)
    return _lane(key, skey, specs, {"agent_product": "claude_code", "team": "mobile"}, rnd,
                 kind=kind, scope="ws:w1", events=events)


def _remap_lane(rnd: random.Random, key: str, skey: str) -> Lane:
    kind = rnd.choice((LaneKind.MAIN, LaneKind.SUBAGENT, LaneKind.API_RUN))
    product = "agent_sdk" if kind is LaneKind.API_RUN else "claude_code"
    models = _MODELS_NEW + _MODELS_LEGACY
    ttl = rnd.choice(("5m", "1h"))
    walk = _Walk(n=rnd.randint(1, 10), t0_ms=_t0(rnd), model=rnd.choice(models), ttl=ttl,
                 write_class=ttl, start=(2_000, 90_000), models=models, switch_p=0.15,
                 shapes=_COMMON_SHAPES, uncached=(0, 3_000))
    specs, events = _walk(rnd, key, walk)
    return _lane(key, skey, specs, {"agent_product": product}, rnd, kind=kind, scope="ws:w1",
                 events=events)


def _effort_lane(rnd: random.Random, key: str, skey: str) -> Lane:
    kind = rnd.choice((LaneKind.MAIN, LaneKind.MAIN, LaneKind.API_RUN, LaneKind.SUBAGENT))
    product = "agent_sdk" if kind is LaneKind.API_RUN else "claude_code"
    model = rnd.choice(("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5", "claude-fable-5-1"))
    walk = _Walk(n=rnd.randint(1, 10), t0_ms=_t0(rnd), model=model, gaps="short",
                 shapes={"placeholder": 0.03, "refusal": 0.02, "retry": 0.02},
                 output=(0, 20_000), reasoning_p=0.5)
    specs, events = _walk(rnd, key, walk)
    sticky = rnd.choice(_EFFORTS + (None,))
    betas: tuple[str, ...] = ()
    if product == "agent_sdk" and rnd.random() < 0.3:
        betas = ("mid-conversation-output-config-2026-07-01",)
    for spec in specs:
        effort = sticky if rnd.random() < 0.8 else rnd.choice(_EFFORTS + (None,))
        spec.params = {"effort": effort, "betas": betas,
                       "thinking": rnd.choice((None, "adaptive"))}
        if rnd.random() < 0.1:
            spec.params["thinking"] = "off"
    attr = {"agent_product": product,
            "client_version": rnd.choice(("2.1.250", "2.1.270", None))}
    return _lane(key, skey, specs, attr, rnd, kind=kind, scope="ws:w1", events=events)


def _rates_lane(rnd: random.Random, key: str, skey: str) -> Lane:
    x = rnd.random()
    ctx: dict[str, Any] = {}
    model = rnd.choice(("claude-opus-5-5", "claude-opus-5", "claude-opus-4-8"))
    if x < 0.3:
        model, ctx = "claude-opus-5", {"channel": "bedrock", "endpoint_scope": rnd.choice(
            ("regional", "global", "unknown", "multi_region"))}
    elif x < 0.6:
        ctx = {"inference_geo": rnd.choice(("us", "us", "global"))}
    walk = _Walk(n=rnd.randint(1, 9), t0_ms=_t0(rnd), model=model, ctx=ctx,
                 shapes=_COMMON_SHAPES)
    specs, events = _walk(rnd, key, walk)
    fast_lane = x >= 0.3 and rnd.random() < 0.6
    for spec in specs:
        if fast_lane and rnd.random() < 0.5:
            spec.ctx["speed"] = "fast"
            spec.params["speed"] = "fast"
    # a speed toggle breaks the cache on the next request: make the observed split say so
    for prev, spec in zip(specs, specs[1:], strict=False):
        if prev.ctx.get("speed") != spec.ctx.get("speed") and spec.shape == "plain":
            spec.writes += spec.reads
            spec.reads = 0
    return _lane(key, skey, specs, {"agent_product": rnd.choice(("claude_code", "agent_sdk"))},
                 rnd, kind=LaneKind.MAIN, scope="ws:w1", events=events)


def _batch_lane(rnd: random.Random, key: str, skey: str) -> Lane:
    n = 1 if rnd.random() < 0.8 else rnd.randint(2, 3)
    ctx: dict[str, Any] = {}
    model = rnd.choice(("claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5"))
    x = rnd.random()
    if x < 0.15:
        model, ctx = "claude-opus-5", {"channel": "bedrock", "endpoint_scope": "global"}
    elif x < 0.25:
        ctx = {"service_tier": "batch"}
    elif x < 0.35 and model == "claude-opus-5-5":
        ctx = {"speed": "fast"}
    walk = _Walk(n=n, t0_ms=_t0(rnd), model=model, ctx=ctx, static_prefix=rnd.choice(
        (0, 3_000, 20_000)), start=(5_000, 80_000), shapes={"retry": 0.03, "web": 0.05})
    specs, events = _walk(rnd, key, walk)
    if specs and rnd.random() < 0.6:   # a warm single request: part of its prefix is read
        moved = rnd.randint(0, specs[0].writes)
        specs[0].reads += moved
        specs[0].writes -= moved
    workload = rnd.choice(tuple(WorkloadClass))
    entry = rnd.choice((None, None, "claude-code-github-action", "managed-agents"))
    attr = {"agent_product": "agent_sdk", "workload_class": workload, "entrypoint": entry}
    return _lane(key, skey, specs, attr, rnd, kind=LaneKind.API_RUN, scope="ws:w1", events=events)


def _placeholder_lane(rnd: random.Random, key: str, skey: str) -> Lane:
    ttl = rnd.choice(("5m", "1h"))
    walk = _Walk(n=rnd.randint(1, 8), t0_ms=_t0(rnd),
                 model=rnd.choice(("claude-opus-5-5", "claude-sonnet-4-6", "claude-sonnet-5")),
                 ttl=ttl, write_class=ttl, output=(1, 40),
                 shapes={"placeholder": 0.6, "residual_only": 0.05})
    specs, events = _walk(rnd, key, walk)
    for spec in specs:
        spec.params = {"effort": rnd.choice(_EFFORTS)}
    return _lane(key, skey, specs, {"agent_product": "agent_sdk"}, rnd,
                 kind=rnd.choice((LaneKind.MAIN, LaneKind.API_RUN)), scope="ws:w1",
                 events=events)


def _unknown_ttl_lane(rnd: random.Random, key: str, skey: str) -> Lane:
    true_ttl = rnd.choice(("5m", "1h"))
    hint = rnd.choice((None, None, "5m", "1h"))
    ctx: dict[str, Any] = {"write_ttl_hint": hint} if hint else {}
    kind = rnd.choice((LaneKind.MAIN, LaneKind.API_RUN))
    walk = _Walk(n=rnd.randint(1, 10), t0_ms=_t0(rnd), ttl=true_ttl, write_class="unknown",
                 model=rnd.choice(("claude-opus-5-5", "claude-sonnet-5")), ctx=ctx,
                 start=(20_000, 450_000), growth=(0, 60_000), gaps=rnd.choice(("mixed", "short")),
                 shapes={"placeholder": 0.05, "retry": 0.03})
    specs, events = _walk(rnd, key, walk)
    product = "agent_sdk" if kind is LaneKind.API_RUN else "claude_code"
    return _lane(key, skey, specs, {"agent_product": product}, rnd, kind=kind, scope="ws:w1",
                 events=events)


def _allowance_lane(rnd: random.Random, key: str, skey: str) -> Lane:
    if rnd.random() < 0.5:
        return _compaction_lane(rnd, key, skey, allowance=True)
    return _ttl_lane(rnd, key, skey, allowance=True)


def _repairs_lanes(rnd: random.Random, family: str, seed: int, n: int) -> list[Lane]:
    """Lanes for the repair family: no-cache lanes (restore_caching), fan-out groups
    (stagger_fanout), CI runs (shared_ci_prefix), retried requests (retry_backoff_cap) and
    refusal fallbacks (fallback_credit), with near-misses of every predicate."""
    lanes: list[Lane] = []
    i = 0
    while len(lanes) < n:
        x = rnd.random()
        if x < 0.2:
            key, skey = _names(family, seed, i)
            i += 1
            lanes.append(_nocache_lane(rnd, key, skey))
        elif x < 0.45:
            group = _fanout_group(rnd, family, seed, i, n - len(lanes))
            i += len(group)
            lanes.extend(group)
        elif x < 0.65:
            group = _ci_group(rnd, family, seed, i, n - len(lanes))
            i += len(group)
            lanes.extend(group)
        else:
            key, skey = _names(family, seed, i)
            i += 1
            ttl = rnd.choice(("5m", "1h"))
            walk = _Walk(n=rnd.randint(2, 9), t0_ms=_t0(rnd), ttl=ttl, write_class=ttl,
                         shapes={"retry": 0.12, "retry_long": 0.15, "refusal": 0.12,
                                 "refusal_ambiguous": 0.06})
            specs, events = _walk(rnd, key, walk)
            lanes.append(_lane(key, skey, specs, {"agent_product": "agent_sdk"}, rnd,
                               kind=LaneKind.API_RUN, scope="ws:w1", events=events))
    return lanes[:n]


def _nocache_lane(rnd: random.Random, key: str, skey: str) -> Lane:
    """A lane behind a gateway that strips cache_control: no reads or writes at all."""
    n = rnd.randint(3, 10)
    ts = _t0(rnd)
    total = rnd.randint(1_000, 30_000)
    model = rnd.choice(("claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5"))
    specs = []
    for k in range(n):
        if k:
            ts += _gap_ms(rnd, "mixed")
            total += rnd.randint(0, 6_000)
        specs.append(_Spec(ts_ms=ts, model=model, reads=0, writes=0, uncached=total,
                           output=rnd.randint(20, 2_000)))
    return _lane(key, skey, specs, {"agent_product": "agent_sdk", "team": "platform"}, rnd,
                 kind=rnd.choice((LaneKind.MAIN, LaneKind.API_RUN)), scope="ws:w1")


def _cohorts(family: str, seed: int, i: int, size: int, kinds: tuple[LaneKind, LaneKind],
             teams: tuple[str, str]) -> list[tuple[LaneKind, dict[str, Any]]]:
    """(lane kind, extra attribution) per member of a fan-out / CI group: one cohort (the first
    kind, no team) for most groups; about a third straddle replay cohorts — members spread over
    two teams and two lane kinds — so the differential covers the cohort-confined repair groups
    (R-E24). A side RNG keeps the family's main random stream (every other lane) unchanged."""
    side = rng(seed, "synth.lanes_gen.cohort", family, i)
    if side.random() >= 0.35:
        return [(kinds[0], {})] * size
    return [(side.choice(kinds), {"team": side.choice(teams)}) for _ in range(size)]


def _fanout_group(rnd: random.Random, family: str, seed: int, i: int, room: int) -> list[Lane]:
    """2–4 lanes (usually subagents) of one (scope, model, cwd) whose cold first requests start
    within ~10 s of each other (edges at exactly 10 s included), plus the occasional outsider;
    some groups straddle replay cohorts (:func:`_cohorts`)."""
    size = min(room, rnd.randint(2, 4))
    cohorts = _cohorts(family, seed, i, size, (LaneKind.SUBAGENT, LaneKind.MAIN), ("t1", "t2"))
    t0 = _t0(rnd)
    model = rnd.choice(("claude-opus-5-5", "claude-sonnet-5"))
    cwd = _h(rnd)
    shared = rnd.randint(8_000, 30_000)
    lanes = []
    for k in range(size):
        key, skey = _names(family, seed, i + k)
        start = t0 + (0 if k == 0 else rnd.choice((rnd.randint(0, 10_000), 10_000, 10_001)))
        walk = _Walk(n=rnd.randint(1, 5), t0_ms=start, model=model, gaps="short",
                     start=(shared + 2_000, shared + 40_000))
        specs, events = _walk(rnd, key, walk)
        if rnd.random() < 0.15 and specs:           # a warm first request: not a fan-out member
            specs[0].reads = specs[0].writes // 2
            specs[0].writes -= specs[0].reads
        kind, extra = cohorts[k]
        lanes.append(_lane(key, skey, specs, {"agent_product": "claude_code", "cwd_key": cwd,
                                              **extra},
                           rnd, kind=kind, scope="ws:w1", events=events))
    return lanes


def _ci_group(rnd: random.Random, family: str, seed: int, i: int, room: int) -> list[Lane]:
    """2–5 CI runs of one (scope, model) whose starts are minutes apart (some beyond τ); some
    groups straddle replay cohorts (:func:`_cohorts`)."""
    size = min(room, rnd.randint(2, 5))
    cohorts = _cohorts(family, seed, i, size, (LaneKind.MAIN, LaneKind.API_RUN),
                       ("ci-a", "ci-b"))
    ts = _t0(rnd)
    model = rnd.choice(("claude-opus-5-5", "claude-sonnet-5"))
    ttl = rnd.choice(("5m", "1h"))
    lanes = []
    for k in range(size):
        key, skey = _names(family, seed, i + k)
        if k:
            ts += rnd.choice((rnd.randint(20_000, 290_000), rnd.randint(300_000, 900_000),
                              300_000))
        walk = _Walk(n=rnd.randint(1, 4), t0_ms=ts, model=model, ttl=ttl, write_class=ttl,
                     gaps="short", start=(15_000, 60_000))
        specs, events = _walk(rnd, key, walk)
        kind, extra = cohorts[k]
        lanes.append(_lane(key, skey, specs, {"agent_product": "claude_code",
                                              "workload_class": WorkloadClass.CI,
                                              "entrypoint": "claude-code-github-action",
                                              **extra},
                           rnd, kind=kind, scope="ws:ci", events=events))
    return lanes


_SINGLE: dict[str, Any] = {
    "ttl": _ttl_lane,
    "keepalive": _keepalive_lane,
    "compaction": _compaction_lane,
    "cold_resume": _cold_resume_lane,
    "remap": _remap_lane,
    "effort": _effort_lane,
    "rates": _rates_lane,
    "batch": _batch_lane,
    "placeholder": _placeholder_lane,
    "unknown_ttl": _unknown_ttl_lane,
    "allowance": _allowance_lane,
}


def random_lanes(seed: int, n: int, *, family: str) -> list[Lane]:
    """*n* seeded random lanes of *family* (one of :data:`FAMILIES`), deterministic per
    ``(seed, n, family)``. Lanes of the ``allowance`` family are on the subscription billing path;
    every other family is billed (so each family can be replayed in one call)."""
    if family not in FAMILIES:
        raise UsageError(f"unknown lane family {family!r}")
    if type(n) is not int or n < 0:
        raise UsageError("n must be a non-negative int")
    rnd = rng(seed, "synth.lanes_gen", family, n)
    if family == "repairs":
        return _repairs_lanes(rnd, family, seed, n)
    make = _SINGLE[family]
    return [make(rnd, *_names(family, seed, i)) for i in range(n)]


# =============================================================================================
# Appendix A closed forms
# =============================================================================================

_T0_S = EPOCH_MS // 1000
_M = 1_000_000_000   # nano per USD


def _shift(rows: Sequence[tuple[int, int, int, int, int, int]]) -> list[tuple[int, ...]]:
    return [(_T0_S + r[0], *r[1:]) for r in rows]


def _a1_rows() -> list[tuple[int, int, int, int, int, int]]:
    return [(0, 0, 100_000, 0, 0, 500), (420, 0, 102_000, 0, 0, 500),
            (840, 0, 104_000, 0, 0, 500), (1260, 0, 106_000, 0, 0, 500)]


def _closed_a1() -> tuple[list[Lane], dict[str, int]]:
    lane = lane_from_table(_shift(_a1_rows()), lane_key="A1-main",
                           attribution={"agent_product": "claude_code"})
    return [lane], {"baseline": 2_100_000_000, "cost:ttl=1h": 949_200_000,
                    "saving:ttl=1h": 1_150_800_000, "miss_events": 3}


def _a2_rows(write: int) -> list[tuple[int, int, int, int, int, int]]:
    rows = [(0, 0, 100_000, 0, 0, 500), (30, 100_000, 2_000, 0, 0, 500),
            (60, 102_000, 2_000, 0, 0, 500), (90, 104_000, 2_000, 0, 0, 500)]
    if write == 1:   # billed at 1h: move the write column
        rows = [(ts, r, 0, w, u, o) for ts, r, w, _w1, u, o in rows]
    return rows


def _closed_a2() -> tuple[list[Lane], dict[str, int]]:
    lane = lane_from_table(_shift(_a2_rows(5)), lane_key="A2-bursty",
                           attribution={"agent_product": "claude_code"})
    return [lane], {"baseline": 631_200_000, "cost:ttl=1h": 949_200_000,
                    "saving:ttl=1h": -318_000_000, "saving:ttl=5m": 0, "miss_events": 0}


def _closed_a2b() -> tuple[list[Lane], dict[str, int]]:
    lane = lane_from_table(_shift(_a2_rows(1)), lane_key="A2b-bursty-1h",
                           attribution={"agent_product": "claude_code"})
    return [lane], {"baseline": 949_200_000, "cost:ttl=5m": 631_200_000,
                    "saving:ttl=5m": 318_000_000}


def _closed_a3() -> tuple[list[Lane], dict[str, int]]:
    rows = [(0, 0, 0, 100_000, 0, 500), (420, 100_000, 0, 2_000, 0, 500),
            (840, 102_000, 0, 2_000, 0, 500), (1260, 104_000, 0, 2_000, 0, 500)]
    lane = lane_from_table(_shift(rows), lane_key="A3-main-1h",
                           attribution={"agent_product": "claude_code"})
    return [lane], {"baseline": 949_200_000, "cost:ttl=5m": 2_100_000_000,
                    "saving:ttl=5m": -1_150_800_000}


def _closed_a4() -> tuple[list[Lane], dict[str, int]]:
    sdk = lane_from_table(_shift(_a1_rows()), lane_key="A4-sdk", kind=LaneKind.API_RUN,
                          attribution={"agent_product": "agent_sdk"})
    return [sdk], {"baseline": 2_100_000_000, "cost:keepalive=240s,max=3600s": 692_400_000,
                   "pings:keepalive=240s,max=3600s": 3}


def _closed_a4_gap(gap_s: int, name: str) -> tuple[list[Lane], dict[str, int]]:
    rows = [(0, 0, 100_000, 0, 0, 500), (gap_s, 0, 102_000, 0, 0, 500)]
    sdk = lane_from_table(_shift(rows), lane_key=name, kind=LaneKind.API_RUN,
                          attribution={"agent_product": "agent_sdk"})
    base = 510_000_000 + 520_000_000   # 100k·$5 + 500·$20 ; 102k·$5 + 500·$20
    if gap_s == 1_200:   # 4 pings, warm: reads 100k @ $0.20 + writes 2k @ $5 + output
        cost = 510_000_000 + 4 * 20_000_000 + (20_000_000 + 10_000_000 + 10_000_000)
        pings = 4
    else:                # 2 h: 15 pings, cold
        cost = base + 15 * 20_000_000
        pings = 15
    return [sdk], {"baseline": base, "cost:keepalive=240s,max=3600s": cost,
                   "pings:keepalive=240s,max=3600s": pings}


def _closed_a4_cc() -> tuple[list[Lane], dict[str, int]]:
    cc = lane_from_table(_shift(_a1_rows()), lane_key="A4-claude-code",
                         attribution={"agent_product": "claude_code"})
    return [cc], {"baseline": 2_100_000_000, "cost:keepalive=240s,max=3600s": 2_100_000_000,
                  "pings:keepalive=240s,max=3600s": 0}


def _closed_a5() -> tuple[list[Lane], dict[str, int]]:
    rows = [(0, 0, 0, 500_000, 0, 1_000), (30, 500_000, 0, 0, 0, 1_000),
            (7_230, 0, 0, 500_000, 0, 1_000), (7_320, 500_000, 0, 0, 0, 1_000)]
    lane = lane_from_table(_shift(rows), lane_key="A5-cold",
                           attribution={"agent_product": "claude_code"})
    # compact at the 2 h gap: summarization call 500k uncached @ $4 + 20,283 output @ $20, the
    # request rewrites S_c + 0 new tokens @ $8 (1h); the next request reads S_c @ $0.20.
    s_c = 20_283
    event_req = 2_000_000_000 + s_c * 20_000 + s_c * 8_000 + 20_000_000
    next_req = s_c * 200 + 20_000_000
    cost = 4_020_000_000 + 120_000_000 + event_req + next_req
    return [lane], {"baseline": 8_280_000_000, "events": 1,
                    "cost_observed": 4_000_000_000, "premium": 3_900_000_000,
                    "cost:cold-resume=compact,min=200000": cost,
                    "saving:cold-resume=compact,min=200000": 8_280_000_000 - cost,
                    "cost:cold-resume=clear,min=200000": 8_280_000_000}


def _closed_a6() -> tuple[list[Lane], dict[str, int]]:
    rows = [(0, 0, 300_000, 0, 0, 1_000), (30, 300_000, 150_000, 0, 0, 1_000),
            (60, 450_000, 50_000, 0, 0, 1_000)]
    lane = lane_from_table(_shift(rows), lane_key="A6-sonnet", model="claude-sonnet-5",
                           attribution={"agent_product": "claude_code"})
    return [lane], {"baseline": 1_430_000_000,
                    "cost:compact-window=400000,post=20000": 1_999_000_000,
                    "saving:compact-window=400000,post=20000": -569_000_000,
                    "added_calls:compact-window=400000,post=20000": 1,
                    "cost:compact-window=600000,post=20000": 1_430_000_000}


def _closed_a10() -> tuple[list[Lane], dict[str, int]]:
    """Edit churn: a warm context edit clears X = 40,000 tokens at token 20,000; S = 62,000 are
    rewritten; five more requests follow."""
    t0 = EPOCH_MS
    reqs = [make_request("A10-sdk", 0, t0, {"cache_write_5m": 118_000, "output": 500}),
            make_request("A10-sdk", 1, t0 + 30_000,
                         {"cache_read": 118_000, "cache_write_5m": 2_000, "output": 500}),
            make_request("A10-sdk", 2, t0 + 60_000,
                         {"cache_read": 20_000, "cache_write_5m": 62_000, "output": 500},
                         applied_edits=(("clear_tool_uses_20250919", 40_000),))]
    prefix = 82_000
    for k in range(5):
        reqs.append(make_request("A10-sdk", 3 + k, t0 + (90 + 30 * k) * 1000,
                                 {"cache_read": prefix, "cache_write_5m": 2_000, "output": 500}))
        prefix += 2_000
    attr_reqs = [_with_attr(r, Attribution(agent_product="agent_sdk")) for r in reqs]
    lane = make_lane(attr_reqs, kind=LaneKind.API_RUN, lane_key="A10-sdk")
    return [lane], {"cost_observed": 310_000_000, "rewrite_premium": 297_600_000,
                    "benefit": 40_000_000, "net_loss": 257_600_000, "k_rem": 5,
                    "edit_request_seq": 2}


def _closed_a11() -> tuple[list[Lane], dict[str, int]]:
    """Truncation: four max_tokens attempts (R 48k, W5 2k, O 16,384) on Sonnet 5; three are
    followed within 120 s by a same-lane request with T ≥ 47,500."""
    t0 = EPOCH_MS
    rows: list[tuple[int, dict[str, int], str | None]] = [
        (0, {"cache_write_5m": 50_000, "output": 800}, "end_turn")]
    for start, follow in ((30, 90), (200, 260), (400, 460), (600, None)):
        rows.append((start, {"cache_read": 48_000, "cache_write_5m": 2_000, "output": 16_384},
                     "max_tokens"))
        if follow is not None:
            rows.append((follow, {"cache_read": 48_000, "cache_write_5m": 2_000,
                                  "output": 1_000}, "end_turn"))
    rows.append((900, {"cache_read": 48_000, "cache_write_5m": 2_000, "output": 700},
                 "end_turn"))
    reqs = [make_request("A11-sonnet", seq, t0 + ts * 1000, usage, "claude-sonnet-5",
                         stop_reason=stop, attribution={"agent_product": "agent_sdk"})
            for seq, (ts, usage, stop) in enumerate(rows)]
    lane = make_lane(reqs, kind=LaneKind.API_RUN, lane_key="A11-sonnet")
    return [lane], {"per_attempt": 178_440_000, "truncated_cost": 713_760_000,
                    "recoverable_upper": 535_320_000, "truncated": 4, "followed": 3}


def _with_attr(req: Request, attr: Attribution) -> Request:
    from dataclasses import replace

    return replace(req, attribution=attr)


CLOSED_FORMS: dict[str, Any] = {
    "A.1": _closed_a1,
    "A.2": _closed_a2,
    "A.2b": _closed_a2b,
    "A.3": _closed_a3,
    "A.4": _closed_a4,
    "A.4-20min": lambda: _closed_a4_gap(1_200, "A4-sdk-20min"),
    "A.4-2h": lambda: _closed_a4_gap(7_200, "A4-sdk-2h"),
    "A.4-claude-code": _closed_a4_cc,
    "A.5": _closed_a5,
    "A.6": _closed_a6,
    "A.10": _closed_a10,
    "A.11": _closed_a11,
}


def closed_form(name: str) -> tuple[list[Lane], dict[str, int]]:
    """The Appendix A fixture *name* (a key of :data:`CLOSED_FORMS`) and its hand-computed
    expectations (int nano unless the key says otherwise). Keys ``baseline``,
    ``cost:<spec>``, ``saving:<spec>``, ``pings:<spec>`` and ``added_calls:<spec>`` are replay
    results of the policy ``<spec>`` (documented mode, ``core.testing.FakePricer``); the other keys
    are the fixture-specific quantities of Appendix A (e.g. ``cost_observed``, ``premium``)."""
    try:
        build = CLOSED_FORMS[name]
    except KeyError:
        raise UsageError(f"unknown closed form {name!r}") from None
    return build()


# =============================================================================================
# rollout panels (stepped wedge / org-wide) with a known effect
# =============================================================================================

_PPM = 1_000_000
#: Synthetic default level of cost per active developer-day: the published Claude Code enterprise
#: average (CC_FLEET_USD_PER_ACTIVE_DAY, core.evidence) — a generator default, never a prediction.
_BASE_USD = str(CC_FLEET_USD_PER_ACTIVE_DAY.value)
_DOW_PPM = (40_000, 60_000, 50_000, 30_000, -20_000, -150_000, -180_000)   # Mon … Sun


_MAX_PARAM = Decimal(1_000_000)
_MIN_EXPONENT = -30


def _fraction(value: Decimal | str | int | float, what: str) -> Fraction:
    """A generator parameter as an exact Fraction: a finite decimal of magnitude ≤ 10⁶ with at
    most 30 decimal places (floats are read through their shortest repr); anything else raises
    :class:`UsageError`."""
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int, float)):
        raise UsageError(f"{what} must be a number")
    if isinstance(value, float):
        value = repr(value)
    try:
        number = Decimal(value.strip() if isinstance(value, str) else value)
    except (ArithmeticError, ValueError):
        raise UsageError(f"{what} must be a decimal number") from None
    exponent = number.as_tuple().exponent
    # adjusted() is the exponent of the leading digit: no context arithmetic (nothing can trap)
    if not number.is_finite() or not isinstance(exponent, int) or exponent < _MIN_EXPONENT \
            or (number and number.adjusted() > 6):
        raise UsageError(f"{what} must be a finite decimal of moderate size")
    fraction = Fraction(number)
    if abs(fraction) > _MAX_PARAM:
        raise UsageError(f"{what} must be a finite decimal of moderate size")
    return fraction


def _positive_base(base_usd: Decimal | str, noise_ppm: int) -> Fraction:
    base = _fraction(base_usd, "base_usd_per_dev_day")
    if base <= 0:
        raise UsageError("base_usd_per_dev_day must be positive")
    if type(noise_ppm) is not int or not 0 <= noise_ppm <= 400_000:
        raise UsageError("noise_ppm must be an int in [0, 400000]")
    return base


def _date(start: str, day: int) -> str:
    return (_dt.date.fromisoformat(start) + _dt.timedelta(days=day)).isoformat()


def _noise_ppm(rnd: random.Random, spread: int) -> int:
    """Approximately normal integer noise in ppm (Irwin–Hall of four uniforms)."""
    return sum(rnd.randint(-spread, spread) for _ in range(4)) // 2


@dataclass(frozen=True)
class _Cell:
    cluster: str
    day: int
    dev_days: int
    cost0: int          # counterfactual (untreated) cost, nano
    cost: int           # observed baseline-repriced cost, nano
    treated: bool
    arm: str | None
    wave: str | None
    prs: int


def _panel_cells(*, clusters: int | Sequence[str], weeks: int, true_effect: Fraction,
                 waves: int, holdback: Decimal | str | int | float, seed: int, org_wide: bool,
                 base_nano: int, noise: int) -> tuple[list[_Cell], str]:
    if type(weeks) is not int or weeks < 2:
        raise UsageError("weeks must be an int ≥ 2")
    names = [f"c{k:02d}" for k in range(1, clusters + 1)] if isinstance(clusters, int) \
        else [str(c) for c in clusters]
    if len(names) < 1 or len(set(names)) != len(names):
        raise UsageError("clusters must be a positive count or distinct names")
    rnd = rng(seed, "synth.rollout_panel", tuple(names), weeks, waves, org_wide)
    days = 7 * weeks
    sizes = {c: rnd.randint(4, 14) for c in names}
    level = {c: _noise_ppm(rnd, 60_000) for c in names}          # cluster effect ≈ ±6%
    trend = rnd.randint(-2_000, 2_000)                             # ppm per week
    if org_wide:
        change_day = days // 2
        start_of: dict[str, int | None] = {"org": change_day}
        arms: dict[str, tuple[str | None, str | None]] = {"org": (None, None)}
        sizes = {"org": sum(sizes.values())}
        level = {"org": 0}
        order = ["org"]
    else:
        if type(waves) is not int or waves < 1:
            raise UsageError("waves must be an int ≥ 1")
        step = weeks // (waves + 1)
        if step < 1:
            raise UsageError("stepped wedge needs weeks ≥ waves + 1")
        hb = _fraction(holdback, "holdback")
        n_hold = int(hb) if hb.denominator == 1 and hb >= 1 else (
            0 if hb <= 0 else max(1, _round(hb * len(names))))
        if n_hold >= len(names):
            raise UsageError("holdback leaves no treated cluster")
        order = list(names)
        rnd.shuffle(order)                           # seeded wave order, never by spend
        held = sorted(order[:n_hold])
        treated = order[n_hold:]
        start_of = {c: None for c in held}
        arms = {c: ("holdback", None) for c in held}
        per_wave = [treated[k::waves] for k in range(waves)]
        for k, members in enumerate(per_wave, start=1):
            for c in members:
                start_of[c] = 7 * step * k
                arms[c] = ("treatment", str(k))
    start = "2026-06-01"   # a Monday
    cells: list[_Cell] = []
    for c in sorted(start_of):
        for day in range(days):
            dow = day % 7
            active = sizes[c] * (85 if dow < 5 else 12)
            dev_days = max(1, (active + rnd.randint(-sizes[c] * 10, sizes[c] * 10)) // 100)
            factor = (Fraction(_PPM + level[c], _PPM) * Fraction(_PPM + _DOW_PPM[dow], _PPM)
                      * Fraction(_PPM + trend * (day // 7), _PPM)
                      * Fraction(_PPM + _noise_ppm(rnd, noise), _PPM))
            cost0 = _round(base_nano * dev_days * factor)
            begins = start_of[c]
            is_treated = begins is not None and day >= begins
            cost = _round(cost0 * (1 - true_effect)) if is_treated else cost0
            prs = max(0, (dev_days * 30 + rnd.randint(-20, 20)) // 100)
            arm, wave = arms[c]
            cells.append(_Cell(c, day, dev_days, cost0, cost, is_treated, arm, wave, prs))
    return cells, start


def _round(value: Fraction) -> int:
    floor = value.numerator // value.denominator
    rest = value - floor
    if rest > Fraction(1, 2) or (rest == Fraction(1, 2) and floor % 2 == 1):
        return floor + 1
    return floor


def rollout_panel(*, clusters: int | Sequence[str], weeks: int,
                  true_effect: Decimal | str | int | float, waves: int,
                  holdback: Decimal | str | int | float, seed: int, org_wide: bool = False,
                  base_usd_per_dev_day: Decimal | str = _BASE_USD, noise_ppm: int = 30_000,
                  price_change: Decimal | str | int | float = "0") -> list[PanelRow]:
    """A seeded cluster-day panel (SPEC §13.1 ``PanelRow``) with a known treatment effect.

    * ``true_effect`` is the relative reduction of cost per active developer-day on treated
      cluster-days (``0.25`` = −25%; negative values raise cost). The exact ATT of a panel is
      :func:`rollout_truth` (same arguments).
    * Stepped wedge (default): ``holdback`` is a fraction of the clusters (``int ≥ 1``: a count)
      that is never treated (arm ``"holdback"``); the others are split, in seeded random order
      (never by spend), into ``waves`` waves (arm ``"treatment"``, wave ``"1"``…); wave ``k``
      starts at week ``k · (weeks // (waves + 1))``, so the pre-period is one step long.
    * ``org_wide=True``: one cluster ``"org"`` (all developers) whose cost shifts by the effect
      from the middle day of the window on (``treated`` marks the post period; arm/wave None) —
      the series for the event-study ITS tests.
    * Costs: ``base_usd_per_dev_day`` (default: the published CC_FLEET_USD_PER_ACTIVE_DAY) ×
      active dev-days × cluster level (≈ ±6%) × day-of-week ×
      weekly trend × noise (``noise_ppm`` spread), exact integer nano (rounded once, half-even).
      ``cost_actual_nano`` equals the baseline-repriced cost except that ``price_change`` (e.g.
      ``"-0.2"`` for a 20% price cut) applies from the first treatment day on (rate variance, R8).
      ``outcome_prs`` is unaffected by treatment.
    Rows are ordered by (cluster, date); dates start on Monday 2026-06-01.
    """
    effect = _fraction(true_effect, "true_effect")
    base = _positive_base(base_usd_per_dev_day, noise_ppm)
    price = _fraction(price_change, "price_change")
    cells, start = _panel_cells(clusters=clusters, weeks=weeks, true_effect=effect, waves=waves,
                                holdback=holdback, seed=seed, org_wide=org_wide,
                                base_nano=_round(base * _M), noise=noise_ppm)
    first_treated = min((c.day for c in cells if c.treated), default=None)
    rows = []
    for cell in cells:
        actual = cell.cost
        if first_treated is not None and cell.day >= first_treated and price:
            actual = _round(cell.cost * (1 + price))
        rows.append(PanelRow(cluster_id=cell.cluster, date_utc=_date(start, cell.day),
                             cost_baseline_nano=cell.cost, cost_actual_nano=actual,
                             active_dev_days=cell.dev_days, arm=cell.arm, wave=cell.wave,
                             treated=cell.treated, outcome_prs=cell.prs))
    return rows


def rollout_truth(*, clusters: int | Sequence[str], weeks: int,
                  true_effect: Decimal | str | int | float, waves: int,
                  holdback: Decimal | str | int | float, seed: int, org_wide: bool = False,
                  base_usd_per_dev_day: Decimal | str = _BASE_USD, noise_ppm: int = 30_000) -> int:
    """The exact average treatment effect on the treated of :func:`rollout_panel` with the same
    arguments, in nano per active developer-day: ``Σ(cost − cost₀) / Σ dev-days`` over treated
    cluster-days (negative for a cost reduction), rounded half-even."""
    effect = _fraction(true_effect, "true_effect")
    base = _positive_base(base_usd_per_dev_day, noise_ppm)
    cells, _start = _panel_cells(clusters=clusters, weeks=weeks, true_effect=effect,
                                 waves=waves, holdback=holdback, seed=seed, org_wide=org_wide,
                                 base_nano=_round(base * _M), noise=noise_ppm)
    treated = [c for c in cells if c.treated]
    if not treated:  # pragma: no cover - the wave schedule always treats a cluster-day
        raise UsageError("panel has no treated cluster-day")
    return _round(Fraction(sum(c.cost - c.cost0 for c in treated),
                           sum(c.dev_days for c in treated)))


# =============================================================================================
# paired A/B lab campaign
# =============================================================================================


def _tokens_of(req: Request) -> int:
    return sum(inf.usage.total_input + inf.usage.output for inf in req.billable_inferences)


def _cost_of(reqs: Sequence[Request], pricer: Pricer) -> int:
    total = 0
    for req in reqs:
        for att in req.attempts:
            for inf in att.inferences:
                if inf.billable is not False:
                    fig: Figure = pricer.price_inference(inf, ts_ms=att.ts_start_ms).figure
                    total += fig.nano or 0
    return total


def ab_campaign(*, tasks: int, trials: int, cost_effect: Decimal | str | int | float,
                token_effect: Decimal | str | int | float,
                turn_effect: Decimal | str | int | float, seed: int,
                model: str = "claude-sonnet-5", pricer: Pricer | None = None
                ) -> tuple[list[Request], list[Request], list[dict[str, Any]]]:
    """A paired lab campaign for ``ab`` (SPEC §13.5): ``(baseline requests, candidate requests,
    outcomes)``.

    Every (task, trial) runs once per arm in a seeded random order; each run is one lane of
    requests tagged with ``attribution.extra`` ``task_id`` and ``run_attempt`` (the trial) and
    ``attribution.arm``. Relative to the baseline arm, the candidate uses ``1 + token_effect`` the
    tokens (Σ total input + output), ``1 + turn_effect`` the turns (requests) and ``1 +
    cost_effect`` the list cost (priced with *pricer*, default ``core.testing.FakePricer``), in
    every (task, trial) pair (up to integer rounding) and so campaign-wide; turns are rounded per
    run with the campaign total exact. The RTK-like shape is ``token_effect=-0.38,
    turn_effect=0.14, cost_effect=0.07`` (fewer tokens but a colder cache). Outcomes are dicts
    ``{task_id, arm, trial, success, order}`` with arms ``"baseline"``/``"candidate"`` and order
    1/2 (run position within the pair); success rates are equal in expectation.
    """
    if type(tasks) is not int or tasks < 1 or type(trials) is not int or trials < 1:
        raise UsageError("tasks and trials must be positive ints")
    if pricer is None:
        from tokenbill.core.testing import FakePricer

        pricer = FakePricer()
    te = _fraction(token_effect, "token_effect")
    ue = _fraction(turn_effect, "turn_effect")
    ce = _fraction(cost_effect, "cost_effect")
    if not (te > -1 and ue > -1 and ce > -1):
        raise UsageError("effects must be > -1")
    rnd = rng(seed, "synth.ab_campaign", tasks, trials)
    ts = EPOCH_MS
    runs: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    for t in range(tasks):
        task_id = f"task-{t:03d}"
        turns = rnd.randint(6, 16)
        start = rnd.randint(8_000, 30_000)
        growth = rnd.randint(1_500, 6_000)
        out_mean = rnd.randint(300, 1_200)
        p_success = Fraction(rnd.randint(60, 90), 100)
        for trial in range(trials):
            first = rnd.choice(("baseline", "candidate"))
            for position, arm in enumerate((first, "candidate" if first == "baseline"
                                            else "baseline"), start=1):
                runs.append({"task": task_id, "trial": trial, "arm": arm, "turns": turns,
                             "start": start, "growth": growth, "out": out_mean, "ts": ts})
                ts += 30 * _MIN_MS
                success = rnd.random() < p_success
                outcomes.append({"task_id": task_id, "arm": arm, "trial": trial,
                                 "success": success, "order": position})
    base_runs = {(r["task"], r["trial"]): r for r in runs if r["arm"] == "baseline"}
    cand_runs = [r for r in runs if r["arm"] == "candidate"]
    base_reqs = {key: _ab_run(rnd, run, model) for key, run in base_runs.items()}
    # candidate turns: round((1 + ue) · turns) per run, the campaign total fixed exactly by the
    # largest-remainder rule
    target_turns = _round(sum(len(reqs) for reqs in base_reqs.values()) * (1 + ue))
    raw = [Fraction(r["turns"]) * (1 + ue) for r in cand_runs]
    alloc = [max(1, _floor_f(x)) for x in raw]
    remainders = sorted(range(len(raw)), key=lambda k: (-(raw[k] - _floor_f(raw[k])), k))
    k = 0
    while sum(alloc) < target_turns and remainders:
        alloc[remainders[k % len(remainders)]] += 1
        k += 1
    candidate: list[Request] = []
    for run, n in zip(cand_runs, alloc, strict=True):
        run["turns"] = n
        paired = base_reqs[(run["task"], run["trial"])]
        # every (task, trial) pair carries the whole effect, so per-task paired differences
        # have the campaign's sign and size
        tokens = _round(sum(_tokens_of(req) for req in paired) * (1 + te))
        cost = _round(_cost_of(paired, pricer) * (1 + ce))
        candidate.extend(_ab_candidate(run, _ab_shape(rnd, run), model, pricer, tokens, cost))
    baseline = [req for key in base_runs for req in base_reqs[key]]
    return baseline, candidate, outcomes


def _floor_f(x: Fraction) -> int:
    return x.numerator // x.denominator


def _ab_attr(run: dict[str, Any]) -> Attribution:
    return Attribution(agent_product="agent_sdk", workload_class=WorkloadClass.EVAL,
                       arm=run["arm"], extra=(("run_attempt", str(run["trial"])),
                                              ("task_id", run["task"])))


def _ab_run(rnd: random.Random, run: dict[str, Any], model: str) -> list[Request]:
    """A warm-cache baseline run: the context grows each turn and the previous prefix is read."""
    lane_key = f"ab-{run['arm']}-{run['task']}-{run['trial']}"
    reqs = []
    total = run["start"]
    prefix = 0
    for turn in range(run["turns"]):
        if turn:
            total += run["growth"] + rnd.randint(-500, 500)
        reads = min(prefix, total)
        out = max(1, run["out"] + rnd.randint(-150, 150))
        usage = {"cache_read": reads, "cache_write_5m": total - reads, "output": out}
        reqs.append(make_request(lane_key, turn, run["ts"] + turn * 20_000, usage, model,
                                 session_key=f"s_{lane_key}", attribution=_ab_attr(run)))
        prefix = total
    return reqs


def _ab_shape(rnd: random.Random, run: dict[str, Any]) -> list[tuple[int, int]]:
    """Relative (context weight, output weight) per candidate turn."""
    weights = []
    total = run["start"]
    for turn in range(run["turns"]):
        if turn:
            total += run["growth"] + rnd.randint(-500, 500)
        weights.append((total, max(1, run["out"] + rnd.randint(-150, 150))))
    return weights


def _ab_candidate(run: dict[str, Any], shape: list[tuple[int, int]], model: str,
                  pricer: Pricer, target_tokens: int, target_cost: int) -> list[Request]:
    """One candidate run scaled to *target_tokens* whose cache read share ρ is solved so that
    its list cost hits *target_cost* (for fixed token counts the cost is linear in ρ)."""
    rates = pricer.resolve(make_inference({"output": 1}, model=model).pricing, ts_ms=run["ts"])
    if rates is None or rates.cache_read is None or rates.cache_write_5m is None:
        raise UsageError(f"no cache rates for {model}")
    ctx_w = sum(c for c, _o in shape)
    out_w = sum(o for _c, o in shape)
    out_total = _round(Fraction(target_tokens * out_w, ctx_w + out_w))
    in_total = target_tokens - out_total
    turns = [(_round(Fraction(in_total * c, ctx_w)), max(1, _round(Fraction(out_total * o, out_w))))
             for c, o in shape]
    # readable tokens per turn: min(previous context, this context)
    w, r = Fraction(rates.cache_write_5m), Fraction(rates.cache_read)
    out_rate = Fraction(rates.output)
    readable = sum(min(prev, t_in) for prev, (t_in, _o) in
                   zip([0] + [t for t, _o in turns], turns, strict=False))
    cost_cold = sum(t_in * w + t_out * out_rate for t_in, t_out in turns) * 1_000   # nano
    slope = readable * (w - r) * 1_000
    rho = (cost_cold - target_cost) / slope if slope else Fraction(0)
    if not Fraction(0) <= rho <= Fraction(1):
        raise UsageError("cost_effect is not reachable with these token and turn effects")
    lane_key = f"ab-{run['arm']}-{run['task']}-{run['trial']}"
    reqs = []
    prev = 0
    for turn, (t_in, t_out) in enumerate(turns):
        reads = _floor_f(rho * min(prev, t_in))
        usage = {"cache_read": reads, "cache_write_5m": t_in - reads, "output": t_out}
        reqs.append(make_request(lane_key, turn, run["ts"] + turn * 20_000, usage, model,
                                 session_key=f"s_{lane_key}", attribution=_ab_attr(run)))
        prev = t_in
    return reqs
