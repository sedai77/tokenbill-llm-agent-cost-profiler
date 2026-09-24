"""``random_lanes`` / ``family_policies`` / ``closed_form`` (SYNTH-ORACLE acceptance): lanes are
deterministic per seed, valid, fully priced, of one billing class per family, and together cover
every inference shape the ledger can hold; the closed forms' expectations match the oracle."""

from __future__ import annotations

from collections import Counter

import pytest

from tokenbill.common import canonical_json
from tokenbill.core.errors import UsageError
from tokenbill.core.records import (
    InferenceKind,
    LaneEventKind,
    UsageSource,
    to_json,
)
from tokenbill.core.transitions import classify_transitions
from tokenbill.core.types import Policy
from tokenbill.synth.lanes_gen import (
    CLOSED_FORMS,
    FAMILIES,
    closed_form,
    family_policies,
    random_lanes,
)

from .helpers import PRICER, RULES, replay

N = 40


def _dump(lanes) -> str:
    return canonical_json([to_json(lane) for lane in lanes])


@pytest.mark.parametrize("family", FAMILIES)
def test_random_lanes_are_deterministic_per_seed(family: str) -> None:
    a, b = random_lanes(7, N, family=family), random_lanes(7, N, family=family)
    assert _dump(a) == _dump(b)
    assert _dump(a) != _dump(random_lanes(8, N, family=family))
    assert len(a) == N
    assert len({lane.lane_key for lane in a}) == N


@pytest.mark.parametrize("family", FAMILIES)
def test_random_lanes_are_priced_and_of_one_billing_class(family: str) -> None:
    lanes = random_lanes(3, N, family=family)
    classes = {lane.billing_class for lane in lanes if lane.requests}
    assert classes == ({"allowance"} if family == "allowance" else {"billed"})
    for lane in lanes:
        assert lane.requests
        for req in lane.requests:
            assert req.lane_key == lane.lane_key
            for att in req.attempts:
                for inf in att.inferences:
                    priced = PRICER.price_inference(inf, ts_ms=att.ts_start_ms)
                    assert priced.figure.nano is not None, (family, inf.pricing.model,
                                                            inf.pricing.channel)
        classify_transitions(lane, pricer=PRICER, rules=RULES)   # well-formed for F-SEM


def test_random_lanes_cover_every_inference_shape() -> None:
    kinds: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    billable: Counter[object] = Counter()
    events: Counter[str] = Counter()
    other = Counter()
    for family in FAMILIES:
        for lane in random_lanes(11, 80, family=family):
            events.update(ev.kind.value for ev in lane.events)
            for req in lane.requests:
                if len(req.attempts) > 1:
                    other["multi_attempt"] += 1
                if len(req.attempts) > 3:
                    other["more_than_3_attempts"] += 1
                if req.params.effort is not None:
                    other["effort"] += 1
                if req.attribution.billing_path == "subscription":
                    other["subscription"] += 1
                for att in req.attempts:
                    for inf in att.inferences:
                        kinds[inf.kind.value] += 1
                        sources[inf.usage_source.value] += 1
                        billable[inf.billable] += 1
                        u = inf.usage
                        if u.cache_write_unknown:
                            other["unknown_ttl_hint" if inf.pricing.write_ttl_hint
                                  else "unknown_ttl_no_hint"] += 1
                        if u.web_search_requests:
                            other["web_search"] += 1
                        if u.output_reasoning is not None:
                            other["reasoning"] += 1
                        if inf.output_upper is not None:
                            other["output_upper"] += 1
                        if inf.pricing.speed == "fast":
                            other["fast"] += 1
                        if inf.pricing.inference_geo == "us":
                            other["geo_us"] += 1
                        if inf.pricing.channel == "bedrock":
                            other["bedrock"] += 1
                        if inf.pricing.provider == "openai":
                            other["openai"] += 1
                        if u.cache_write_1h:
                            other["write_1h"] += 1
    assert {k.value for k in InferenceKind} - {InferenceKind.KEEPALIVE.value} <= set(kinds)
    assert {s.value for s in UsageSource} - {UsageSource.PROVIDER_ROLLUP.value} <= set(sources)
    assert {True, False, None} <= set(billable)
    assert {LaneEventKind.COMPACTION.value, LaneEventKind.CLEAR.value} <= set(events)
    for key in ("multi_attempt", "more_than_3_attempts", "effort", "subscription",
                "unknown_ttl_hint", "unknown_ttl_no_hint", "web_search", "reasoning",
                "output_upper", "fast", "geo_us", "bedrock", "openai", "write_1h"):
        assert other[key] > 0, key


def test_ambiguous_and_boundary_gaps_are_generated() -> None:
    ambiguous = Counter()
    for family in ("ttl", "unknown_ttl", "keepalive"):
        for lane in random_lanes(5, 120, family=family):
            for t in classify_transitions(lane, pricer=PRICER, rules=RULES):
                ambiguous[t.ambiguous] += 1
                if t.ttl_s is not None and t.gap_ms == t.ttl_s * 1000 + 10_000:
                    ambiguous["edge"] += 1
    assert ambiguous[True] > 0 and ambiguous[False] > 0


def test_repairs_family_has_fanout_groups_ci_runs_and_no_cache_lanes() -> None:
    lanes = random_lanes(2, 120, family="repairs")
    no_cache = [ln for ln in lanes if all(inf.usage.cache_read == 0 and inf.usage.cache_write == 0
                                          for r in ln.requests for inf in r.billable_inferences)]
    ci = [ln for ln in lanes if ln.requests[0].attribution.workload_class.value == "ci"]
    subagents = [ln for ln in lanes if ln.kind.value == "subagent"]
    assert no_cache and ci and len(subagents) >= 2
    for spec in ("repair=restore_caching", "repair=stagger_fanout", "repair=shared_ci_prefix",
                 "repair=retry_backoff_cap", "repair=fallback_credit"):
        assert replay(lanes, spec).saving.nano != 0, spec


def test_unknown_family_and_bad_n() -> None:
    with pytest.raises(UsageError):
        random_lanes(0, 5, family="nope")
    with pytest.raises(UsageError):
        random_lanes(0, -1, family="ttl")
    with pytest.raises(UsageError):
        family_policies("nope")
    assert random_lanes(0, 0, family="repairs") == []


@pytest.mark.parametrize("family", FAMILIES)
def test_family_policies_start_with_the_observed_policy(family: str) -> None:
    policies = family_policies(family)
    assert policies[0] == Policy.observed()
    assert len(policies) >= 3
    assert all(p.spec() for p in policies[1:])


@pytest.mark.parametrize("name", sorted(CLOSED_FORMS))
def test_closed_forms_are_valid_and_deterministic(name: str) -> None:
    lanes, expected = closed_form(name)
    again, expected_again = closed_form(name)
    assert _dump(lanes) == _dump(again) and expected == expected_again
    assert all(type(v) is int for v in expected.values())
    assert lanes and all(lane.requests for lane in lanes)


def test_unknown_closed_form() -> None:
    with pytest.raises(UsageError):
        closed_form("A.99")
