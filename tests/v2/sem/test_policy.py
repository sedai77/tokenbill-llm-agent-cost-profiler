"""SPEC §9.5 / §3.19: the policy grammar, canonical form, combine and selectors."""

from __future__ import annotations

import dataclasses
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.common import TokenbillError
from tokenbill.core import policy as pol
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.records import BILLING_PATHS, Attribution, LaneKind, WorkloadClass
from tokenbill.core.types import Policy

from .helpers import lane, req, usage

# ---------------------------------------------------------------------------------------------
# strategies: canonical policies
# ---------------------------------------------------------------------------------------------

free_value = st.text("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:/ ",
                     min_size=1, max_size=12).map(str.strip).filter(bool)
term_values = {
    "lane_kind": st.sampled_from([k.value for k in LaneKind]),
    "workload": st.sampled_from([w.value for w in WorkloadClass]),
    "billing_path": st.sampled_from(BILLING_PATHS),
    "agent_type": free_value,
    "team": free_value,
    "agent_product": free_value,
    "model": free_value,
}


@st.composite
def selectors(draw: st.DrawFn) -> str:
    keys = draw(st.lists(st.sampled_from(sorted(term_values)), unique=True, max_size=3))
    if not keys:
        return "all"
    return ",".join(f"{k}:{draw(term_values[k])}" for k in sorted(keys))


scales = st.integers(0, 1000).map(lambda n: format((Decimal(n) / 1000).normalize(), "f"))
repairs = st.sampled_from([*pol.REPAIRS, "block:volatile_system", "block:tool-order"])
model_ids = st.text("abcdefghijklmnopqrstuvwxyz0123456789-.", min_size=1, max_size=20)


@st.composite
def policies(draw: st.DrawFn) -> Policy:
    ttl = draw(st.dictionaries(selectors(), st.sampled_from(["5m", "1h"]), max_size=3))
    remap = draw(st.dictionaries(selectors(), model_ids, max_size=3))
    effort = draw(st.dictionaries(selectors(), st.tuples(st.sampled_from(pol.EFFORT_LEVELS),
                                                         scales), max_size=3))
    positive = st.integers(1, 2**53)
    p = Policy(
        name="tmp",
        ttl=tuple(sorted(ttl.items())),
        keepalive=draw(st.none() | st.tuples(selectors(), positive, positive)),
        compaction_window=draw(st.none() | st.tuples(positive, st.none() | positive)),
        cold_resume=draw(st.none() | st.tuples(st.sampled_from(["compact", "clear"]), positive)),
        model_remap=tuple(sorted(remap.items())),
        effort=tuple(sorted((s, lv, sc) for s, (lv, sc) in effort.items())),
        fast_off=draw(st.booleans()),
        geo_global=draw(st.booleans()),
        regional_to_global=draw(st.booleans()),
        batch=draw(st.none() | st.just("eligible")),
        repairs=tuple(sorted(draw(st.sets(repairs, max_size=4)))),
        breakpoint_policy=draw(st.none() | st.sampled_from(pol.BREAKPOINT_POLICIES)),
    )
    return dataclasses.replace(p, name=pol.to_spec(p) or "observed")


@settings(max_examples=400, deadline=None)
@given(policies())
def test_round_trip_for_generated_policies(p: Policy) -> None:
    spec = pol.to_spec(p)
    assert pol.parse_policy(spec) == p
    assert p.spec() == spec                      # Policy.spec() delegates
    assert pol.to_spec(pol.parse_policy(spec)) == spec


@settings(max_examples=200, deadline=None)
@given(policies(), st.text(max_size=10))
def test_to_spec_is_canonical_for_any_name(p: Policy, name: str) -> None:
    renamed = dataclasses.replace(p, name=name)
    spec = pol.to_spec(renamed)
    assert spec == pol.to_spec(p)
    assert pol.to_spec(pol.parse_policy(spec)) == spec


@settings(max_examples=300, deadline=None)
@given(st.text(";,@=:.0123456789abcdefghijklmnopqrstuvwxyz_- ", max_size=60))
def test_fuzz_parse_only_usage_errors_escape(spec: str) -> None:
    try:
        p = pol.parse_policy(spec)
    except UsageError:
        return
    assert pol.parse_policy(pol.to_spec(p)) == p


@settings(max_examples=100, deadline=None)
@given(st.text(max_size=40))
def test_fuzz_arbitrary_unicode(spec: str) -> None:
    try:
        pol.parse_policy(spec)
    except TokenbillError:
        pass


# ---------------------------------------------------------------------------------------------
# grammar details
# ---------------------------------------------------------------------------------------------


def test_spec_example_is_canonical() -> None:
    spec = "ttl=1h@agent_product:claude_code,lane_kind:main;repair=fallback_credit"
    p = pol.parse_policy(spec)
    assert p.ttl == (("agent_product:claude_code,lane_kind:main", "1h"),)
    assert p.repairs == ("fallback_credit",)
    assert pol.to_spec(p) == spec and p.name == spec
    # terms and clauses in any order parse to the same policy
    same = pol.parse_policy(
        " repair=fallback_credit ; ttl=1h@lane_kind:main, agent_product:claude_code;")
    assert same == p


def test_observed_policy() -> None:
    for spec in ("", "  ", "observed", ";;"):
        p = pol.parse_policy(spec)
        assert p == Policy(name="observed") and p.is_observed()
    assert pol.to_spec(Policy.observed()) == "" and Policy.observed().spec() == ""


def test_defaults_and_units() -> None:
    p = pol.parse_policy("keepalive=4m@agent_product:agent_sdk")
    assert p.keepalive == ("agent_product:agent_sdk", 240, 3600)
    assert pol.parse_policy("keepalive=240,max=1h").keepalive == ("all", 240, 3600)
    assert pol.parse_policy("keepalive=1h,max=7200s").keepalive == ("all", 3600, 7200)
    assert pol.parse_policy("cold-resume=compact").cold_resume == ("compact", 200_000)
    assert pol.parse_policy("cold-resume=clear,min=150000").cold_resume == ("clear", 150_000)
    assert pol.parse_policy("compact-window=400000").compaction_window == (400_000, None)
    assert pol.parse_policy("compact-window=400000,post=20283").compaction_window == \
        (400_000, 20_283)
    assert pol.parse_policy("effort=high").effort == (("all", "high", "0.5"),)
    assert pol.parse_policy("effort=low,scale=.250").effort == (("all", "low", "0.25"),)
    assert pol.parse_policy("effort=low,scale=1.0").effort == (("all", "low", "1"),)
    assert pol.parse_policy("effort=low,scale=0").effort == (("all", "low", "0"),)
    assert pol.to_spec(pol.parse_policy("keepalive=4m;effort=high;cold-resume=clear")) == \
        "keepalive=240s,max=3600s;cold-resume=clear,min=200000;effort=high,scale=0.5"


def test_every_clause_and_the_canonical_order() -> None:
    spec = ("breakpoints=every_15;repair=stagger_fanout;repair=block:tool-order;batch=eligible;"
            "regional=global;geo=global;fast=off;effort=medium,scale=0.5@lane_kind:main;"
            "effort=max,scale=1;model=claude-sonnet-5@lane_kind:subagent;"
            "model=claude-haiku-4-5@agent_type:Explore;cold-resume=compact,min=200000;"
            "compact-window=300000;keepalive=240s,max=3600s@agent_product:api;ttl=5m;"
            "ttl=1h@workload:ci")
    p = pol.parse_policy(spec)
    assert pol.to_spec(p) == (
        "ttl=5m;ttl=1h@workload:ci;keepalive=240s,max=3600s@agent_product:api;"
        "compact-window=300000;cold-resume=compact,min=200000;"
        "model=claude-haiku-4-5@agent_type:Explore;model=claude-sonnet-5@lane_kind:subagent;"
        "effort=max,scale=1;effort=medium,scale=0.5@lane_kind:main;fast=off;geo=global;"
        "regional=global;batch=eligible;repair=block:tool-order;repair=stagger_fanout;"
        "breakpoints=every_15")
    assert p.fast_off and p.geo_global and p.regional_to_global and p.batch == "eligible"
    assert p.breakpoint_policy == "every_15"


CATALOG_GRIDS = [   # SPEC §11.1 and §10.2 policy fragments with their selectors
    "ttl=5m@agent_product:claude_code,lane_kind:main",
    "ttl=1h@agent_product:claude_code,lane_kind:main",
    "ttl=1h@agent_product:claude_code,lane_kind:subagent",
    "ttl=1h@agent_product:claude_code,lane_kind:workflow_agent",
    "ttl=5m@lane_kind:api_run", "ttl=1h@lane_kind:api_run",
    "keepalive=240s,max=3600s@agent_product:agent_sdk",
    "keepalive=240s,max=3600s@agent_product:api",
    *[f"compact-window={w}" for w in (200000, 300000, 400000, 500000, 700000)],
    "cold-resume=compact,min=200000", "cold-resume=clear,min=200000",
    "model=claude-sonnet-5@agent_product:claude_code,lane_kind:main",
    "effort=medium,scale=0.5@agent_product:claude_code,lane_kind:main",
    "effort=low,scale=0.25@agent_product:claude_code,lane_kind:main",
    "model=claude-sonnet-5@lane_kind:subagent", "model=claude-haiku-4-5@lane_kind:subagent",
    "model=claude-opus-5-5@model:claude-opus-5",
    "effort=high", "effort=medium", "fast=off", "geo=global", "regional=global",
    "batch=eligible", "repair=stagger_fanout", "repair=retry_backoff_cap",
    "repair=fallback_credit", "repair=restore_caching", "repair=shared_ci_prefix",
    "breakpoints=static_plus_end", "breakpoints=every_15", "ttl=1h@workload:ci",
]


@pytest.mark.parametrize("spec", CATALOG_GRIDS)
def test_catalog_grids_parse(spec: str) -> None:
    p = pol.parse_policy(spec)
    assert not p.is_observed()
    assert pol.parse_policy(pol.to_spec(p)) == p


@pytest.mark.parametrize("spec", [
    "nope=1", "ttl", "ttl=", "ttl=30m", "ttl=1h@", "ttl=1h@bogus:x", "ttl=1h@lane_kind:giant",
    "ttl=1h@workload:nightly", "ttl=1h@billing_path:barter", "ttl=1h@team:a,team:b",
    "ttl=1h@team", "ttl=1h@team:", "fast=off@all", "geo=local", "regional=eu", "batch=all",
    "fast=on", "repair=magic", "repair=block:", "repair=block:UPPER", "breakpoints=sometimes",
    "keepalive=0s", "keepalive=4x", "keepalive=240s,max=", "keepalive=240s,maxx=1",
    "keepalive=240s,max=1h,max=1h", "keepalive=240s,1h", "compact-window=-5",
    "compact-window=0", "compact-window=4e5", "compact-window=400000,post=0",
    "cold-resume=nap", "cold-resume=clear,min=x", "effort=extreme", "effort=low,scale=2",
    "effort=low,scale=abc", "effort=low,scale=-0.5", "model=@lane_kind:main",
    "model=a;b@x", "ttl=1h;ttl=5m", "ttl=1h@team:a;ttl=5m@team:a",
    "keepalive=240s;keepalive=300s", "compact-window=1;compact-window=2",
    "cold-resume=clear;cold-resume=compact", "breakpoints=end;breakpoints=observed",
    "effort=low;effort=high", "model=a;model=b", "ttl=1h@team:x\x07",
    "x" * 20_000, "compact-window=9007199254740993", "keepalive=9007199254740992h",
])
def test_invalid_specs_raise_usage_error(spec: str) -> None:
    with pytest.raises(UsageError):
        pol.parse_policy(spec)


def test_non_string_spec() -> None:
    with pytest.raises(UsageError):
        pol.parse_policy(None)  # type: ignore[arg-type]


def test_identical_repeats_are_deduplicated() -> None:
    p = pol.parse_policy("fast=off;fast=off;repair=fallback_credit;repair=fallback_credit;"
                         "ttl=1h@team:a;ttl=1h@team:a")
    assert pol.to_spec(p) == "ttl=1h@team:a;fast=off;repair=fallback_credit"


@pytest.mark.parametrize("bad", [
    Policy(name="x", ttl=(("all", "30m"),)),
    Policy(name="x", ttl=(("bogus:1", "1h"),)),
    Policy(name="x", ttl=((3, "1h"),)),  # type: ignore[arg-type]
    Policy(name="x", ttl=[("all", "1h")]),  # type: ignore[arg-type]
    Policy(name="x", ttl=(("all",),)),  # type: ignore[arg-type]
    Policy(name="x", keepalive=("all", 0, 3600)),
    Policy(name="x", keepalive=("all", 240, 2**53 + 1)),
    Policy(name="x", keepalive=("all", 240)),  # type: ignore[arg-type]
    Policy(name="x", keepalive=(5, 240, 3600)),  # type: ignore[arg-type]
    Policy(name="x", compaction_window=(0, None)),
    Policy(name="x", compaction_window=(10,)),  # type: ignore[arg-type]
    Policy(name="x", compaction_window=(10, -1)),
    Policy(name="x", cold_resume=("nap", 5)),
    Policy(name="x", cold_resume=("clear", True)),  # type: ignore[arg-type]
    Policy(name="x", model_remap=(("all", "a;b"),)),
    Policy(name="x", effort=(("all", "extreme", "0.5"),)),
    Policy(name="x", effort=(("all", "low", "2"),)),
    Policy(name="x", fast_off=1),  # type: ignore[arg-type]
    Policy(name="x", batch="all"),
    Policy(name="x", repairs=("magic",)),
    Policy(name="x", repairs=(1,)),  # type: ignore[arg-type]
    Policy(name="x", repairs=["fallback_credit"]),  # type: ignore[arg-type]
    Policy(name="x", breakpoint_policy="sometimes"),
])
def test_to_spec_rejects_inexpressible_policies(bad: Policy) -> None:
    with pytest.raises(ContractViolation):
        pol.to_spec(bad)


def test_to_spec_requires_a_policy() -> None:
    with pytest.raises(ContractViolation):
        pol.to_spec("ttl=1h")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# combine
# ---------------------------------------------------------------------------------------------


def test_combine_is_a_union_named_by_its_spec() -> None:
    a = pol.parse_policy("ttl=1h@lane_kind:main;repair=fallback_credit;fast=off")
    b = pol.parse_policy("ttl=5m@lane_kind:subagent;repair=stagger_fanout;geo=global;"
                         "compact-window=400000")
    c = pol.combine(a, b)
    assert c.ttl == (("lane_kind:main", "1h"), ("lane_kind:subagent", "5m"))
    assert c.repairs == ("fallback_credit", "stagger_fanout")
    assert c.fast_off and c.geo_global and c.compaction_window == (400_000, None)
    assert c.name == pol.to_spec(c)
    assert a.combine(b) == c == pol.combine(b, a)
    assert pol.combine(Policy.observed(), a) == a
    assert pol.combine(a, a) == a


def test_combine_accepts_non_canonical_inputs() -> None:
    raw = Policy(name="mine", ttl=(("lane_kind:main,team:x", "1h"),),
                 effort=(("all", "low", "0.50"),))
    other = Policy(name="theirs", ttl=(("team:x, lane_kind:main", "1h"),))
    c = pol.combine(raw, other)
    assert c.ttl == (("lane_kind:main,team:x", "1h"),) and c.effort == (("all", "low", "0.5"),)


@pytest.mark.parametrize(("a", "b"), [
    ("ttl=1h@lane_kind:main", "ttl=5m@lane_kind:main"),
    ("keepalive=240s", "keepalive=300s"),
    ("compact-window=400000", "compact-window=500000"),
    ("cold-resume=compact", "cold-resume=clear"),
    ("breakpoints=end", "breakpoints=every_15"),
    ("model=claude-sonnet-5@lane_kind:main", "model=claude-haiku-4-5@lane_kind:main"),
    ("effort=medium", "effort=low"),
])
def test_combine_conflicts_raise(a: str, b: str) -> None:
    with pytest.raises(ContractViolation):
        pol.combine(pol.parse_policy(a), pol.parse_policy(b))


# ---------------------------------------------------------------------------------------------
# selectors
# ---------------------------------------------------------------------------------------------


def test_selector_terms() -> None:
    assert pol.selector_terms("all") == () == pol.selector_terms("")
    assert pol.selector_terms("team:a, lane_kind:main") == (("lane_kind", "main"), ("team", "a"))
    assert pol.selector_terms("model:us.anthropic.claude-opus-5:1") == \
        (("model", "us.anthropic.claude-opus-5:1"),)
    with pytest.raises(UsageError):
        pol.selector_terms("colour:red")
    with pytest.raises(UsageError):
        pol.selector_terms(["team:a"])  # type: ignore[arg-type]


ATTR = Attribution(agent_type="Explore", team="payments", agent_product="claude_code",
                   billing_path="subscription", workload_class=WorkloadClass.CI)


def sample_lane(kind: LaneKind = LaneKind.SUBAGENT):
    first = req("L", 0, 0, usage(w5=10), "claude-haiku-4-5", attribution=ATTR)
    second = req("L", 1, 5, usage(w5=10), "claude-opus-5-5",
                 attribution=Attribution(team="search"))
    return lane([first, second], kind=kind)


@pytest.mark.parametrize(("selector", "matches"), [
    ("all", True),
    ("lane_kind:subagent", True), ("lane_kind:main", False),
    ("agent_type:Explore", True), ("agent_type:Plan", False),
    ("team:payments", True), ("team:search", False),
    ("agent_product:claude_code", True), ("agent_product:agent_sdk", False),
    ("billing_path:subscription", True), ("billing_path:api_key", False),
    ("workload:ci", True), ("workload:interactive", False),
    ("model:claude-haiku-4-5", True), ("model:claude-haiku-4-5-20251001", True),
    ("model:claude-opus-5-5", False),
    ("team:payments,lane_kind:subagent,model:claude-haiku-4-5", True),
    ("team:payments,lane_kind:main", False),
])
def test_lane_matches_each_selector_key(selector: str, matches: bool) -> None:
    assert pol.lane_matches(selector, sample_lane()) is matches


def test_request_matches_uses_the_request() -> None:
    lane_obj = sample_lane()
    second = lane_obj.requests[1]
    assert pol.request_matches("team:search", second, lane_obj)
    assert not pol.lane_matches("team:search", lane_obj)
    assert pol.request_matches("model:claude-opus-5-5,lane_kind:subagent", second, lane_obj)
    assert not pol.request_matches("agent_product:claude_code", second, lane_obj)


def test_billing_path_falls_back_to_the_pricing_context() -> None:
    r0 = req("L", 0, 0, usage(w5=1), attribution=Attribution(), billing_path="api_key")
    assert pol.lane_matches("billing_path:api_key", lane([r0]))
    residual = req("L", 0, 0, usage(o=5), kind="output_residual", attribution=Attribution())
    assert pol.lane_matches("billing_path:unknown", lane([residual]))


def test_empty_lane_matches_only_lane_terms() -> None:
    empty = lane([], kind=LaneKind.MAIN)
    assert pol.lane_matches("all", empty)
    assert pol.lane_matches("lane_kind:main", empty)
    assert not pol.lane_matches("team:payments", empty)
    with pytest.raises(UsageError):
        pol.lane_matches("colour:red", empty)
