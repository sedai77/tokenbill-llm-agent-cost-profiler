"""CORE-AMENDMENTS S-4 (transitions): the unknown-TTL rule on rows with
``ttl_semantics_known=False`` (GitHub Copilot) and the ``param-change`` sub-cause
``context-tier-change``; Claude lanes classify exactly as before."""

from __future__ import annotations

import dataclasses
import time
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core import transitions as tr
from tokenbill.core.builders import FlatRates, make_attempt
from tokenbill.core.cache_rules import CacheRules, RulesTable
from tokenbill.core.records import Attribution, Request, RequestParams, UsageBuckets
from tokenbill.core.transitions import (
    AMBIGUITY_MS,
    PARAM_CHANGE_SUB_CAUSES,
    classify_transitions,
)

from .helpers import lane, req, usage

PRICER = FlatRates()
RULES = RulesTable()
TAU = 300                     # the 5m write-TTL hint of the Copilot requests below (seconds)
COPILOT = {"provider": "github", "channel": "github_copilot", "billing_path": "copilot_pool"}


def with_duration(r: Request, duration_s: float | None) -> Request:
    """*r* with its single attempt's ``duration_ms`` set."""
    ms = None if duration_s is None else int(round(duration_s * 1000))
    return dataclasses.replace(r, attempts=(dataclasses.replace(r.attempts[0], duration_ms=ms),))


def cp_req(seq: int, ts_s: float, u: UsageBuckets, *, duration_s: float | None = 0,
           hint: str | None = "5m", tier: str | None = None, model: str = "claude-opus-5-5",
           **kw: Any) -> Request:
    """A Copilot request: writes with an unknown TTL class, τ from the write-TTL hint."""
    ctx = {**COPILOT, "write_ttl_hint": hint, "context_tier": tier}
    ctx.update(kw)
    return with_duration(req("L", seq, ts_s, u, model, **ctx), duration_s)


def rewrite(total: int = 100_000) -> tuple[UsageBuckets, UsageBuckets]:
    return usage(cache_write_unknown=total), usage(cache_write_unknown=total)


def copilot_pair(gap_s: float, *, prev_duration_s: float | None = 20, hint: str | None = "5m",
                 **kw: Any):
    first, second = rewrite()
    return lane([cp_req(0, 0, first, duration_s=prev_duration_s, hint=hint, **kw),
                 cp_req(1, gap_s, second, duration_s=3, hint=hint, **kw)])


def one(lane_obj: Any, rules: Any = RULES):
    (t,) = classify_transitions(lane_obj, pricer=PRICER, rules=rules)
    return t


# ---------------------------------------------------------------------------------------------
# the unknown-TTL rule
# ---------------------------------------------------------------------------------------------


def test_expiry_needs_tau_plus_previous_duration_plus_10s() -> None:
    t = one(copilot_pair(TAU + 20 + 11))
    assert t.is_miss_event and t.ttl_s == TAU
    assert t.cause == "ttl-expiry" and t.ambiguous is False
    edge = one(copilot_pair(TAU + 20 + 10))           # within the window: never ttl-expiry
    assert edge.is_miss_event and edge.cause == "unexplained" and edge.ambiguous is True


@pytest.mark.parametrize(("gap_s", "ambiguous"), [
    (TAU - 30, True), (TAU - 31, False), (TAU, True), (TAU + 30, True), (TAU + 31, False),
    (TAU - 29.999, True),
])
def test_ambiguity_window_is_widened_by_the_previous_duration(gap_s: float,
                                                              ambiguous: bool) -> None:
    t = one(copilot_pair(gap_s))
    assert t.ambiguous is ambiguous
    assert (t.cause == "ttl-expiry") is (gap_s > TAU + 30)


def test_the_duration_is_that_of_the_previous_request() -> None:
    first, second = rewrite()
    ln = lane([cp_req(0, 0, first, duration_s=0), cp_req(1, TAU + 15, second, duration_s=500)])
    t = one(ln)
    assert t.cause == "ttl-expiry" and t.ambiguous is False     # d = 0: τ + 10 s suffices
    unknown = lane([cp_req(0, 0, first, duration_s=None),
                    cp_req(1, TAU + 11, second, duration_s=None)])
    assert one(unknown).cause == "ttl-expiry"                   # unknown duration adds nothing


def test_retries_count_into_the_previous_duration() -> None:
    first, second = rewrite()
    base = cp_req(0, 0, first)
    inf = base.attempts[0].inferences[0]
    failed = make_attempt((), ts_ms=0, attempt_no=0, outcome="http_error", http_status=529,
                          duration_ms=5_000)
    ok = make_attempt((inf,), ts_ms=10_000, attempt_no=1, duration_ms=20_000)
    retried = dataclasses.replace(base, attempts=(failed, ok))
    assert tr._duration_ms(retried) == 30_000
    assert tr._duration_ms(dataclasses.replace(
        base, attempts=(failed, dataclasses.replace(ok, duration_ms=None)))) == 10_000
    t_in = one(lane([retried, cp_req(1, TAU + 40, second)]))
    assert t_in.cause != "ttl-expiry" and t_in.ambiguous is True   # 340 ≤ 300 + 30 + 10
    t_out = one(lane([retried, cp_req(1, TAU + 41, second)]))
    assert t_out.cause == "ttl-expiry" and t_out.ambiguous is False


def test_unknown_tau_is_never_ttl_expiry() -> None:
    for gap_s in (TAU + 100, 7_200, 86_400 * 2):
        t = one(copilot_pair(gap_s, hint=None))
        assert t.is_miss_event and t.ttl_s is None
        assert t.cause != "ttl-expiry" and t.ambiguous is False and t.predicted_hit is None


def test_tau_from_a_known_write_class_on_copilot() -> None:
    # a 5m-bucket write gives τ = 300 s without a hint; the unknown-semantics rule still applies
    ln = lane([cp_req(0, 0, usage(w5=100_000), duration_s=20, hint=None),
               cp_req(1, TAU + 25, usage(w5=100_000), hint=None)])
    t = one(ln)
    assert t.ttl_s == TAU and t.cause != "ttl-expiry" and t.ambiguous is True


def test_claude_lanes_are_unchanged() -> None:
    first, second = usage(w5=100_000), usage(w5=100_000)
    for d in (0, 20, 500, None):
        claude = lane([with_duration(req("L", 0, 0, first), d),
                       with_duration(req("L", 1, TAU + 11, second), 3)])
        t = one(claude)
        assert t.cause == "ttl-expiry" and t.ambiguous is False and t.ttl_s == TAU
        near = lane([with_duration(req("L", 0, 0, first), d),
                     with_duration(req("L", 1, TAU + 10, second), 3)])
        t = one(near)
        assert t.cause == "ttl-expiry" and t.ambiguous is True


def test_rules_none_uses_the_builtin_table() -> None:
    ln = copilot_pair(TAU + 25)
    assert one(ln, rules=None).cause != "ttl-expiry"
    assert one(ln, rules=None) == one(ln)


class Rows:
    """A rules provider that counts calls and can mark any row's semantics unknown."""

    def __init__(self, unknown: frozenset[str] = frozenset(), strip: bool = False) -> None:
        self.unknown = unknown
        self.strip = strip
        self.calls: list[tuple[str, str, str]] = []

    def rules_for(self, provider: str, channel: str, model: str) -> Any:
        self.calls.append((provider, channel, model))
        row = RULES.rules_for(provider, channel, model)
        if self.strip:          # an older provider whose rows lack the field
            return type("Old", (), {"ttl_options_s": row.ttl_options_s})()
        if channel in self.unknown:
            return dataclasses.replace(row, ttl_semantics_known=False)
        return row


def test_the_rule_is_row_driven() -> None:
    first, second = usage(w5=100_000), usage(w5=100_000)
    claude = lane([with_duration(req("L", 0, 0, first), 20),
                   with_duration(req("L", 1, TAU + 25, second), 0)])
    assert one(claude).cause == "ttl-expiry"
    assert one(claude, rules=Rows(frozenset({"anthropic_api"}))).cause != "ttl-expiry"
    # rows without the attribute are known (providers built before S-4 keep working)
    old = Rows(strip=True)
    assert one(copilot_pair(TAU + 25), rules=old).cause == "ttl-expiry"
    # rows are looked up once per (provider, channel, model)
    rows = Rows()
    reqs = [cp_req(i, i * 400, usage(cache_write_unknown=100_000)) for i in range(20)]
    assert all(t.cause == "ttl-expiry" for t in classify_transitions(
        lane(reqs), pricer=PRICER, rules=rows))
    assert rows.calls == [("github", "github_copilot", "claude-opus-5-5")]


def test_copilot_rules_row_reaches_transitions() -> None:
    row = RULES.rules_for("github", "github_copilot", "gpt-5.6-sol")
    assert isinstance(row, CacheRules) and row.ttl_semantics_known is False
    t = one(copilot_pair(TAU + 25, model="gpt-5.6-sol"))
    assert t.ambiguous is True and t.cause != "ttl-expiry"


# ---------------------------------------------------------------------------------------------
# context-tier-change
# ---------------------------------------------------------------------------------------------


def tier_pair(tier0: str | None, tier1: str | None, *, gap_s: float = 30, kw0: Any = None,
              kw1: Any = None):
    first, second = rewrite()
    return lane([cp_req(0, 0, first, tier=tier0, **(kw0 or {})),
                 cp_req(1, gap_s, second, tier=tier1, **(kw1 or {}))])


def test_context_tier_change_is_a_param_change() -> None:
    assert PARAM_CHANGE_SUB_CAUSES.index("context-tier-change") == \
        PARAM_CHANGE_SUB_CAUSES.index("effort-change") + 1
    t = one(tier_pair("default", "long_context"))
    assert t.is_miss_event and (t.cause, t.sub_cause) == ("param-change", "context-tier-change")
    assert t.predicted_hit is False                      # an invalidating change
    back = one(tier_pair("long_context", "default"))
    assert (back.cause, back.sub_cause) == ("param-change", "context-tier-change")


def test_an_unobserved_tier_is_never_a_change() -> None:
    for tiers in (("default", None), (None, "long_context"), (None, None),
                  ("default", "default")):
        t = one(tier_pair(*tiers))
        assert t.cause != "param-change" and t.predicted_hit is True


def test_context_tier_change_order() -> None:
    sdk = RequestParams(model_requested="claude-opus-5-5", effort="high")
    sdk2 = RequestParams(model_requested="claude-opus-5-5", effort="low")
    attr = Attribution(agent_product="copilot_cli", billing_path="copilot_pool",
                       client_version="1.0.64")
    attr2 = dataclasses.replace(attr, client_version="1.0.70")
    # fast-toggle and effort-change come first
    fast = one(tier_pair("default", "long_context", kw1={"speed": "fast"}))
    assert fast.sub_cause == "fast-toggle"
    effort = one(tier_pair("default", "long_context", kw0={"params": sdk},
                           kw1={"params": sdk2}))
    assert effort.sub_cause == "effort-change"
    # the tier change beats client-upgrade and directory-change
    upgrade = one(tier_pair("default", "long_context", kw0={"attribution": attr},
                            kw1={"attribution": attr2}))
    assert upgrade.sub_cause == "context-tier-change"
    assert one(tier_pair("default", "default", kw0={"attribution": attr},
                         kw1={"attribution": attr2})).sub_cause == "client-upgrade"
    # a model switch and a known expiry still beat it
    switch = one(tier_pair("default", "long_context", kw1={"model": "claude-sonnet-5"}))
    assert switch.cause == "model-switch"
    expired = one(tier_pair("default", "long_context", gap_s=TAU + 11))
    assert expired.cause == "ttl-expiry"


def test_claude_lane_with_tiers_set_the_same_way() -> None:
    # the sub-cause is channel-independent: any serving context that states a tier
    first, second = usage(w5=100_000), usage(w5=100_000)
    ln = lane([req("L", 0, 0, first, context_tier="default"),
               req("L", 1, 30, second, context_tier="long_context")])
    assert one(ln).sub_cause == "context-tier-change"


# ---------------------------------------------------------------------------------------------
# properties
# ---------------------------------------------------------------------------------------------

_STEP = st.tuples(st.integers(0, 900), st.integers(0, 200), st.integers(0, 120_000),
                  st.integers(0, 120_000), st.sampled_from(["5m", "1h", None]),
                  st.sampled_from(["default", "long_context", None]))


def build(steps: list[tuple[int, int, int, int, str | None, str | None]], copilot: bool):
    reqs, ts = [], 0
    for seq, (gap, dur, reads, writes, hint, tier) in enumerate(steps):
        ts += gap
        u = usage(r=reads, cache_write_unknown=writes)
        if copilot:
            reqs.append(cp_req(seq, ts, u, duration_s=dur, hint=hint, tier=tier))
        else:
            reqs.append(with_duration(req("L", seq, ts, u, write_ttl_hint=hint), dur))
    return lane(reqs)


@settings(max_examples=150, deadline=None)
@given(st.lists(_STEP, min_size=2, max_size=8))
def test_copilot_rule_property(steps: list[Any]) -> None:
    ln = build(steps, copilot=True)
    ts = classify_transitions(ln, pricer=PRICER, rules=RULES)
    reqs = ln.requests
    for t in ts:
        prev = reqs[t.index - 1]
        slack = AMBIGUITY_MS + tr._duration_ms(prev)
        if t.ttl_s is None:
            assert t.cause != "ttl-expiry" and not t.ambiguous
            continue
        tau_ms = t.ttl_s * 1000
        assert t.ambiguous is (abs(t.gap_ms - tau_ms) <= slack)
        if t.cause == "ttl-expiry":
            assert t.gap_ms > tau_ms + slack
        if t.is_miss_event and t.gap_ms > tau_ms + slack:
            assert t.cause in ("compaction", "model-switch", "ttl-expiry")


@settings(max_examples=150, deadline=None)
@given(st.lists(_STEP, min_size=2, max_size=8))
def test_claude_lanes_ignore_durations(steps: list[Any]) -> None:
    """On rows with known semantics the durations play no part (the pre-S-4 rule)."""
    ln = build(steps, copilot=False)
    no_dur = lane([with_duration(r, None) for r in ln.requests])
    got = classify_transitions(ln, pricer=PRICER, rules=RULES)
    assert got == classify_transitions(no_dur, pricer=PRICER, rules=RULES)
    for t in got:
        if t.ttl_s is None:
            assert t.cause != "ttl-expiry" and not t.ambiguous
            continue
        assert t.ambiguous is (abs(t.gap_ms - t.ttl_s * 1000) <= AMBIGUITY_MS)
        if t.cause == "ttl-expiry":
            assert t.gap_ms > t.ttl_s * 1000


def test_linear_time_on_a_long_copilot_lane() -> None:
    n = 20_000
    reqs = [req("L", i, i * 30, usage(r=5_000 * i, cache_write_unknown=5_000),
                **{**COPILOT, "write_ttl_hint": "5m"}) for i in range(n)]
    ln = lane(reqs)
    start = time.perf_counter()
    ts = classify_transitions(ln, pricer=PRICER, rules=RULES)
    assert len(ts) == n - 1
    assert time.perf_counter() - start < 10
