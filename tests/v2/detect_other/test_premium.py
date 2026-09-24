"""``premium.modifiers`` (SPEC §6.9 case 4 vs case 1) and ``premium.sticky-escalation`` (self
vs org counts)."""

from __future__ import annotations

import pytest

from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import LaneKind
from tokenbill.detect.premium import PremiumModifiers, StickyEscalation, residency_required

from .helpers import DAY_MS, OPUS5, OPUS55, attribution, ctx, evidence, lane, one

# SPEC §6.9 case 1: uncached 1,000; read 100,000; 5m write 2,000; 1h write 3,000; output 500
CASE1 = (0, 100_000, 2_000, 3_000, 1_000, 500)


def _case_lane(key: str = "L-fast", **kw):
    return lane(key, [CASE1], per_request={0: kw})


def test_fast_premium_is_case_4_minus_case_1_exact() -> None:
    # case 4 ($0.136) − case 1 ($0.068) on identical tokens = $0.068 EXACT
    f = one(PremiumModifiers().detect([_case_lane(speed="fast")],
                                      ctx(thresholds={"min_usd": "0.01"})), "fast-premium")
    assert f.cost_observed.nano == 68_000_000 and f.cost_observed.evidence is Evidence.EXACT
    assert f.recoverable == f.cost_observed and f.recoverable.is_billed_eligible
    assert (f.category, f.lever_class, f.lever_ids) == ("premium", "rate",
                                                        ("cc.fast_mode_opt_in",))
    assert f.fix.config_patch == (("fastModePerSessionOptIn", "true"),)
    assert evidence(f, f"premium:{OPUS55}")["nano"] == 68_000_000
    assert PremiumModifiers().detect([_case_lane(speed="fast")], ctx()) == []   # < $1.00


def test_geo_and_regional_premiums() -> None:
    # case 1 with inference_geo "us": ×1.1 → $0.0748 − $0.068
    geo = one(PremiumModifiers().detect([_case_lane(inference_geo="us")],
                                        ctx(thresholds={"min_usd": "0.001"})), "geo-premium")
    assert geo.cost_observed.nano == 6_800_000 and geo.lever_ids == ("geo.global",)
    # Opus 5 on Bedrock, 1,000,000 uncached input: regional $5.50 vs global $5.00
    bed = lane("L-bed", [(0, 0, 0, 0, 1_000_000, 0)], model=OPUS5, billing_path="bedrock",
               per_request={0: {"channel": "bedrock", "endpoint_scope": "regional"}})
    reg = one(PremiumModifiers().detect([bed], ctx(thresholds={"min_usd": "0.01"})),
              "regional-premium")
    assert reg.cost_observed.nano == 500_000_000 and reg.recoverable.nano == 500_000_000
    assert reg.cost_observed.evidence is Evidence.EXACT and reg.lever_ids == ("endpoint.global",)
    assert reg.fix.target == "gateway"


def test_residency_policy_flags_instead_of_recovering() -> None:
    thresholds = {"min_usd": "0.001", "policy.residency_required.payments": "true"}
    f = one(PremiumModifiers().detect([_case_lane(inference_geo="us")], ctx(thresholds=thresholds)),
            "geo-premium")
    assert f.recoverable is None and f.lever_ids == ()
    assert "data-residency policy" in f.summary and f.cost_observed.nano == 6_800_000
    assert residency_required(ctx(thresholds={"policy.residency_required": "1"}), "x")
    assert not residency_required(ctx(thresholds={"policy.residency_required.x": "no"}), "x")
    assert not residency_required(ctx(), None)


def test_standard_traffic_and_unpriced_contexts_have_no_premium() -> None:
    low = ctx(thresholds={"min_usd": "0"})
    assert PremiumModifiers().detect([_case_lane()], low) == []
    # the priority tier has no modifier in the rate card: no premium
    assert PremiumModifiers().detect([_case_lane(service_tier="priority")], low) == []
    unknown = lane("L-x", [CASE1], model="claude-foo-9", per_request={0: {"speed": "fast"}})
    assert PremiumModifiers().detect([unknown], low) == []


def test_fast_premium_allowance_cohort() -> None:
    ln = lane("L-sub", [CASE1], billing_path="subscription", per_request={0: {"speed": "fast"}})
    f = one(PremiumModifiers().detect([ln], ctx(thresholds={"min_usd": "0.01"})), "fast-premium")
    assert f.cost_observed.basis is Basis.LIST_EQUIVALENT and f.recoverable.basis is \
        Basis.LIST_EQUIVALENT
    assert f.title.startswith("Allowance headroom:")


def test_fast_premium_with_a_range_line_is_estimated() -> None:
    ln = lane("L-range", [(0, 0, 0, 0, 1_000, 500)], per_request={0: {"speed": "fast",
                                                                      "wu": 10_000}})
    f = one(PremiumModifiers().detect([ln], ctx(thresholds={"min_usd": "0.001"})),
            "fast-premium")
    assert f.cost_observed.evidence is Evidence.ESTIMATED
    assert f.cost_observed.low_nano <= f.cost_observed.nano <= f.cost_observed.high_nano


# ---------------------------------------------------------------------------------------------
# premium.sticky-escalation
# ---------------------------------------------------------------------------------------------

def _dev_lanes(principal: str, days: int, *, fast: bool = False, effort: str | None = None,
               team: str = "infra"):
    out = []
    for d in range(days):
        kw = {"speed": "fast"} if fast else {}
        if effort:
            kw["session_effort"] = effort
        key = f"L-{principal}-{d}"
        out.append(lane(key, [(d * 86_400 + 3_600, 0, 10_000, 0, 5, 500)], team=team,
                        principal=principal, per_request={0: kw}))
    return out


def test_sticky_fast_self_view() -> None:
    lanes = _dev_lanes("r_fast", 6, fast=True) + _dev_lanes("r_five", 5, fast=True)
    mine = StickyEscalation().detect(lanes, ctx(self_principal="r_fast",
                                                thresholds={"min_usd": "0.01"}))
    f = one(mine, "sticky-escalation")
    # per day: 10,000 × (10,000 − 5,000) + 5 × (8,000 − 4,000) + 500 × (40,000 − 20,000)
    assert f.cost_observed.nano == 6 * (50_000_000 + 20_000 + 10_000_000)
    assert f.cost_observed.evidence is Evidence.EXACT and f.audience == "self"
    assert f.recoverable is None and f.n_users == 1
    assert evidence(f, "sticky:days")["fast_days"] == 6
    assert f.fix.config_patch == (("fastModePerSessionOptIn", "true"),)
    # five days is not sticky (more than 5 needed)
    assert StickyEscalation().detect(lanes, ctx(self_principal="r_five",
                                                thresholds={"min_usd": "0"})) == []


def test_sticky_effort_self_view_and_default_override() -> None:
    lanes = _dev_lanes("r_xhigh", 7, effort="xhigh")
    f = one(StickyEscalation().detect(lanes, ctx(self_principal="r_xhigh",
                                                 thresholds={"min_usd": "0.01"})),
            "sticky-escalation")
    # spend of the escalated requests: 7 × (10,000 × 5,000 + 5 × 4,000 + 500 × 20,000)
    assert f.cost_observed.nano == 7 * (50_000_000 + 20_000 + 10_000_000)
    assert "effort" in f.title
    raised = ctx(self_principal="r_xhigh", thresholds={"min_usd": "0", "defaults.effort": "xhigh"})
    assert StickyEscalation().detect(lanes, raised) == []
    with pytest.raises(UsageError):
        StickyEscalation().detect(lanes, ctx(thresholds={"defaults.effort": "extreme"}))


def test_sticky_org_count_only_when_at_least_k() -> None:
    three = [ln for p in ("r_a", "r_b", "r_c") for ln in _dev_lanes(p, 6, fast=True)]
    assert StickyEscalation().detect(three, ctx(thresholds={"min_usd": "0"})) == []
    five = three + [ln for p in ("r_d", "r_e") for ln in _dev_lanes(p, 6, effort="max")]
    f = one(StickyEscalation().detect(five, ctx(thresholds={"min_usd": "0.01"})),
            "sticky-escalation-count")
    assert f.audience == "org" and f.n_users == 5
    item = evidence(f, "sticky:count")
    assert (item["principals"], item["fast"], item["effort"]) == (5, 3, 2)
    assert all(dim != "principal" for dim, _ in f.scope.dims)
    assert "r_a" not in f.summary and "r_a" not in str(f.evidence)
    # the same five with k = 6: withheld
    assert StickyEscalation().detect(five, ctx(k_anonymity=6, thresholds={"min_usd": "0"})) == []


def test_sticky_main_lanes_only() -> None:
    sub = [lane(f"L-s{d}", [(d * 86_400, 0, 10_000, 0, 5, 500)], kind=LaneKind.SUBAGENT,
                principal="r_sub", per_request={0: {"speed": "fast"}}) for d in range(7)]
    assert StickyEscalation().detect(sub, ctx(self_principal="r_sub",
                                              thresholds={"min_usd": "0"})) == []
    anonymous = [lane(f"L-n{d}", [(d * 86_400, 0, 10_000, 0, 5, 500)],
                      attr=attribution(principal=None), per_request={0: {"speed": "fast"}})
                 for d in range(7)]
    assert StickyEscalation().detect(anonymous, ctx(thresholds={"min_usd": "0"})) == []
    assert DAY_MS == 86_400_000
