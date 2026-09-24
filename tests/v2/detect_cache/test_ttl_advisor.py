"""``cache.ttl-advisor`` with ``FakeReplayer``: recommendations follow the replayed values and
differ from the observed TTL, keepalive only for SDK/API lanes, ``ttl-heterogeneous`` with
counts only, the gateway note, allowance labeling, thresholds and evidence (Appendix A.1,
A.2, A.2b, A.4, A.12)."""

from __future__ import annotations

from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import LaneKind
from tokenbill.core.types import Policy
from tokenbill.detect.cache_ttl import TtlAdvisor, keepalive_spec, ttl_spec

from .helpers import (
    ctx,
    fn_replayer,
    lane,
    lane_a1,
    lane_a2,
    only,
    table_replayer,
)

H1_MAIN = ttl_spec("main", "1h")
M5_MAIN = ttl_spec("main", "5m")
KA_API = keepalive_spec("api_run")


def test_policy_specs_are_canonical() -> None:
    assert H1_MAIN == "ttl=1h@lane_kind:main"
    assert M5_MAIN == "ttl=5m@lane_kind:main"
    assert KA_API == "keepalive=240s,max=3600s@lane_kind:api_run"


def test_appendix_a1_recommends_1h() -> None:
    replayer = table_replayer({("A1", H1_MAIN): 1_150_800_000, ("A1", M5_MAIN): 0})
    findings = TtlAdvisor().detect([lane_a1()], ctx(replayer=replayer))
    f = only(findings, "ttl-1h-recommended")
    assert f.recoverable is not None and f.recoverable.nano == 1_150_800_000   # $1.1508
    assert f.recoverable.evidence is Evidence.ESTIMATED
    assert f.cost_observed.nano == 2_100_000_000                                # $2.10
    assert f.cost_observed.evidence is Evidence.EXACT
    assert f.lever_ids == ("cc.prompt_cache_ttl.main",)
    assert f.fix is not None and f.fix.config_patch == (("promptCacheTtl", '"1h"'),)
    assert f.fix.target == "claude-code-managed-settings"
    assert f.fix.gates == ("claude-code>=2.1.242",)
    assert f.scope.dims == (("lane_kind", "main"), ("team", "payments"))
    ev = {e.ref: dict(e.attrs) for e in f.evidence}
    hist = ev["ttl:gap-histogram"]
    assert hist["gaps_5_10m"] == 3 and hist["transitions"] == 3
    rule = ev["ttl:rule-1-in-20"]
    assert rule["verdict"] == "1h" and rule["agrees"] == "yes"
    assert ev["ttl:keepalive-break-even"]["seconds"] == 5_760     # Opus 5.5: 96 min (A.12)
    assert ev["ttl:keepalive-break-even"]["minutes"] == "96.0"
    assert ev["ttl:replays"]["saving_ttl_1h_nano"] == 1_150_800_000
    assert f.confidence == "high"


def test_appendix_a2b_recommends_5m_with_min_usd() -> None:
    lane_ = lane_a2("A2b", hour=True)
    replayer = table_replayer({("A2b", M5_MAIN): 318_000_000, ("A2b", H1_MAIN): 0})
    assert TtlAdvisor().detect([lane_], ctx(replayer=replayer)) == []     # $0.318 < $1.00
    f = only(TtlAdvisor().detect([lane_], ctx(replayer=replayer,
                                              thresholds={"min_usd": "0.10"})),
             "ttl-5m-recommended")
    assert f.recoverable is not None and f.recoverable.nano == 318_000_000
    assert f.cost_observed.nano == 949_200_000                            # $0.9492
    assert f.fix is not None and f.fix.config_patch == (("promptCacheTtl", '"5m"'),)


def test_appendix_a2_status_quo_is_optimal() -> None:
    """Already 5m: ttl=5m is the observed TTL and is never recommended; 1h costs more."""
    replayer = table_replayer({("A2", H1_MAIN): -318_000_000, ("A2", M5_MAIN): 50_000_000_000})
    assert TtlAdvisor().detect([lane_a2()], ctx(replayer=replayer,
                                                thresholds={"min_usd": "0.10"})) == []


def test_recommendation_differs_from_observed_ttl() -> None:
    """A 1h lane is never told to use 1h, however large the (re-rating) 1h replay saving."""
    replayer = table_replayer({("A2b", H1_MAIN): 9_000_000_000, ("A2b", M5_MAIN): 2_000_000_000})
    f = only(TtlAdvisor().detect([lane_a2("A2b", hour=True)], ctx(replayer=replayer)),
             "ttl-5m-recommended")
    assert f.recoverable is not None and f.recoverable.nano == 2_000_000_000


def test_two_percent_of_spend_threshold() -> None:
    """Saving must reach max(min_usd, 2% of cohort spend): $2.10 × 2% = $0.042."""
    small = table_replayer({("A1", H1_MAIN): 41_000_000})
    assert TtlAdvisor().detect([lane_a1()], ctx(replayer=small,
                                                thresholds={"min_usd": "0.01"})) == []
    enough = table_replayer({("A1", H1_MAIN): 42_000_000})
    assert TtlAdvisor().detect([lane_a1()], ctx(replayer=enough,
                                                thresholds={"min_usd": "0.01"}))
    stricter = ctx(replayer=enough, thresholds={"min_usd": "0.01",
                                                "cache.ttl-advisor.spend_share": "0.05"})
    assert TtlAdvisor().detect([lane_a1()], stricter) == []


def test_no_replayer_no_advice() -> None:
    assert TtlAdvisor().detect([lane_a1()], ctx()) == []


def _sdk_lane(key: str, **kw):
    """An SDK agent lane with 7-minute idle gaps (Appendix A.4 shape)."""
    return lane_a1(key, kind=LaneKind.API_RUN, product="agent_sdk", team="agents", **kw)


def test_keepalive_recommended_for_sdk_lane_with_7_minute_gaps() -> None:
    """A.4: keepalive $0.6924 beats 1h $0.9492 on the A.1 lane (saving $1.4076 vs $1.1508)."""
    h1 = ttl_spec("api_run", "1h")
    replayer = table_replayer({("S1", h1): 1_150_800_000, ("S1", KA_API): 1_407_600_000})
    f = only(TtlAdvisor().detect([_sdk_lane("S1")], ctx(replayer=replayer)),
             "keepalive-recommended")
    assert f.recoverable is not None and f.recoverable.nano == 1_407_600_000
    assert f.lever_ids == ("sdk.keepalive",)
    assert f.fix is not None and f.fix.target == "sdk" and f.fix.config_patch is None
    assert "240 s" in f.fix.text and "max_tokens 0" in f.fix.text
    ev = {e.ref: dict(e.attrs) for e in f.evidence}
    assert ev["ttl:replays"]["saving_keepalive_nano"] == 1_407_600_000
    assert ev["ttl:rule-1-in-20"]["agrees"] == "n/a"


def test_1h_beats_keepalive_when_replay_says_so() -> None:
    h1 = ttl_spec("api_run", "1h")
    replayer = table_replayer({("S2", h1): 1_500_000_000, ("S2", KA_API): 1_400_000_000})
    f = only(TtlAdvisor().detect([_sdk_lane("S2")], ctx(replayer=replayer)),
             "ttl-1h-recommended")
    assert f.lever_ids == ("sdk.ttl",)
    assert f.fix is not None and f.fix.config_patch is None and "cache_control" in f.fix.text


def test_never_keepalive_for_claude_code() -> None:
    """Claude Code lanes are never pinged: even an enormous keepalive saving is not evaluated."""
    seen: list[str] = []

    def fn(lane_, policy: Policy) -> int:
        seen.append(policy.spec())
        return 50_000_000_000 if policy.keepalive else 0

    for kind in (LaneKind.MAIN, LaneKind.API_RUN):
        seen.clear()
        lane_ = lane_a1(f"CC-{kind.value}", kind=kind, product="claude_code")
        assert TtlAdvisor().detect([lane_], ctx(replayer=fn_replayer(fn))) == []
        assert not any(spec.startswith("keepalive") for spec in seen)


def test_keepalive_only_for_api_run_cohorts() -> None:
    """R-E11: an SDK lane of a Claude Code lane kind (main) gets TTL advice, never keepalive."""
    seen: list[str] = []

    def fn(lane_, policy: Policy) -> int:
        seen.append(policy.spec())
        return 50_000_000_000 if policy.keepalive else 0

    lane_ = lane_a1("SM", product="agent_sdk")
    assert TtlAdvisor().detect([lane_], ctx(replayer=fn_replayer(fn))) == []
    # observed 5m: only 1h is a candidate (the FakeReplayer never calls fn for "observed")
    assert seen == [H1_MAIN]


def test_gateway_note() -> None:
    replayer = table_replayer({("G1", H1_MAIN): 1_150_800_000})
    f = only(TtlAdvisor().detect([lane_a1("G1", gateway="litellm-proxy")],
                                 ctx(replayer=replayer)), "ttl-1h-recommended")
    assert f.fix is not None
    assert "forward anthropic-beta; the Claude apps gateway cannot use 1h" in f.fix.text
    plain = only(TtlAdvisor().detect([lane_a1("G1")], ctx(replayer=replayer)),
                 "ttl-1h-recommended")
    assert plain.fix is not None and "anthropic-beta" not in plain.fix.text


def test_allowance_cohort_saving_is_list_equivalent() -> None:
    replayer = table_replayer({("AL", H1_MAIN): 1_150_800_000})
    f = only(TtlAdvisor().detect([lane_a1("AL", billing_path="subscription")],
                                 ctx(replayer=replayer)), "ttl-1h-recommended")
    assert f.recoverable is not None and f.recoverable.basis is Basis.LIST_EQUIVALENT
    assert f.cost_observed.basis is Basis.LIST_EQUIVALENT
    assert not f.cost_observed.is_billed_eligible
    assert f.title.startswith("Allowance headroom:")
    assert "list-equivalent, not invoice dollars" in f.summary
    assert ("billing_class", "allowance") in f.scope.dims


def _team(n: int, winners: int, *, per_winner: int, per_loser: int):
    lanes = [lane_a1(f"P{i}", principal=f"r_dev{i}") for i in range(n)]
    table = {(f"P{i}", H1_MAIN): per_winner if i < winners else per_loser for i in range(n)}
    return lanes, table_replayer(table)


def test_ttl_heterogeneous_counts_only() -> None:
    """2 of 5 principals cheaper under 1h (< 60% of ≥ k): per-cohort delivery."""
    lanes, replayer = _team(5, 2, per_winner=3_000_000_000, per_loser=-100_000_000)
    f = only(TtlAdvisor().detect(lanes, ctx(replayer=replayer)), "ttl-heterogeneous")
    assert f.recoverable is not None and f.recoverable.nano == 2 * 3_000_000_000 - 3 * 100_000_000
    ev = {e.ref: dict(e.attrs) for e in f.evidence}
    assert ev["ttl:heterogeneity"] == {"principals": 5, "principals_cheaper": 2}
    assert f.fix is not None and "per cohort" in f.fix.text
    assert f.fix.config_patch == (("promptCacheTtl", '"1h"'),)
    assert "r_dev" not in repr(f)          # counts only: no principal is named
    assert f.n_users == 5


def test_homogeneous_when_60_percent_benefit() -> None:
    lanes, replayer = _team(5, 3, per_winner=3_000_000_000, per_loser=-100_000_000)
    only(TtlAdvisor().detect(lanes, ctx(replayer=replayer)), "ttl-1h-recommended")


def test_heterogeneity_needs_k_principals() -> None:
    lanes, replayer = _team(4, 1, per_winner=9_000_000_000, per_loser=-100_000_000)
    only(TtlAdvisor().detect(lanes, ctx(replayer=replayer)), "ttl-1h-recommended")
    only(TtlAdvisor().detect(lanes, ctx(replayer=replayer, k=4)), "ttl-heterogeneous")


def test_cohorts_by_team_lane_kind_and_billing_path() -> None:
    """One recommendation per (team, lane kind, billing path); a billed cohort with two paths
    names the path in the scope."""
    lanes = [lane_a1("K1"), lane_a1("K2", billing_path="usage_credits"),
             lane_a1("K3", kind=LaneKind.SUBAGENT), lane_a1("K4", team="search"),
             lane_a1("K5", kind=LaneKind.COMPACTION)]
    replayer = fn_replayer(lambda lane_, policy: 1_150_800_000 if policy.ttl and any(
        ttl == "1h" for _, ttl in policy.ttl) else 0)
    findings = TtlAdvisor().detect(lanes, ctx(replayer=replayer))
    scopes = sorted(f.scope.dims for f in findings)
    assert scopes == sorted([
        (("billing_path", "api_key"), ("lane_kind", "main"), ("team", "payments")),
        (("billing_path", "usage_credits"), ("lane_kind", "main"), ("team", "payments")),
        (("lane_kind", "subagent"), ("team", "payments")),
        (("lane_kind", "main"), ("team", "search")),
    ])
    sub = next(f for f in findings if ("lane_kind", "subagent") in f.scope.dims)
    assert sub.lever_ids == ("cc.prompt_cache_ttl.subagent",)
    assert sub.fix is not None and sub.fix.config_patch == (("subagentPromptCacheTtl", '"1h"'),)


def test_fable_break_even() -> None:
    replayer = table_replayer({("FB", H1_MAIN): 5_000_000_000})
    f = only(TtlAdvisor().detect([lane_a1("FB", model="claude-fable-5-1")],
                                 ctx(replayer=replayer)), "ttl-1h-recommended")
    ev = {e.ref: dict(e.attrs) for e in f.evidence}
    assert ev["ttl:keepalive-break-even"]["seconds"] == 11_760        # 196 min (A.12)


def test_rule_disagreement_lowers_confidence() -> None:
    """A bursty lane where the replay still favors 1h: the 1-in-20 rule says 5m."""
    replayer = table_replayer({("A2", H1_MAIN): 2_000_000_000})
    f = only(TtlAdvisor().detect([lane_a2()], ctx(replayer=replayer)), "ttl-1h-recommended")
    ev = {e.ref: dict(e.attrs) for e in f.evidence}
    assert ev["ttl:rule-1-in-20"]["verdict"] == "5m" and ev["ttl:rule-1-in-20"]["agrees"] == "no"
    assert f.confidence == "medium"


def test_mixed_and_unknown_observed_ttl_consider_both() -> None:
    mixed = lane("MX", [(0, 0, 100_000, 0, 0, 500), (420, 0, 0, 102_000, 0, 500)])
    replayer = table_replayer({("MX", M5_MAIN): 1_500_000_000, ("MX", H1_MAIN): 1_200_000_000})
    only(TtlAdvisor().detect([mixed], ctx(replayer=replayer)), "ttl-5m-recommended")
    none = lane("NO", [(0, 0, 0, 0, 20_000, 500), (420, 0, 0, 0, 22_000, 500)])
    replayer2 = table_replayer({("NO", H1_MAIN): 1_200_000_000})
    only(TtlAdvisor().detect([none], ctx(replayer=replayer2)), "ttl-1h-recommended")
