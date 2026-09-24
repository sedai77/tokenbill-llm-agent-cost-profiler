"""verify.panel: cluster-day panels at constant prices on MemoryStore (SPEC §13.1, R8, D26)."""

from __future__ import annotations

import pytest

from tokenbill.core.builders import make_request
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import Attribution, Lane, LaneKind, OutcomeAggregate, Session
from tokenbill.core.testing import FakePricer, MemoryStore
from tokenbill.core.types import IngestResult, PanelRow
from tokenbill.verify import panel as P

from .helpers import CUT20, ORG_KEY, ingest, request, source

DAYS = ("2026-09-01", "2026-09-02", "2026-09-03")
LEVER = "cc.prompt_cache_ttl.main"


def _store(**extra_requests) -> MemoryStore:
    store = MemoryStore(org_key=ORG_KEY)
    reqs = []
    for i, d in enumerate(DAYS):
        for dev in (1, 2):
            reqs.append(request("alpha", d, dev, arm="control", wave="0"))
        for dev in (3, 4, 5):
            tagged = d >= "2026-09-03"
            reqs.append(request("beta", d, dev, seq=i, arm=LEVER if tagged else None,
                                wave="1" if tagged else None))
    reqs.extend(extra_requests.get("more", ()))
    ingest(store, reqs, outcomes=extra_requests.get("outcomes", ()))
    return store


def _priced(pricer, reqs) -> int:
    return sum(pricer.price_inference(inf, ts_ms=att.ts_start_ms).exact_nano
               for r in reqs for att in r.attempts for inf in att.inferences)


def test_build_panel_devdays_costs_and_treatment_from_tags() -> None:
    store = _store()
    base, actual = FakePricer(), FakePricer(contract=CUT20)
    rows = P.build_panel(store, cluster_kind="team", since=DAYS[0], until="2026-09-04",
                         baseline_pricer=base, actual_pricer=actual)
    assert [(r.cluster_id, r.date_utc) for r in rows] == \
        [(c, d) for c in ("alpha", "beta") for d in DAYS]
    one = _priced(base, [request("x", DAYS[0], 1)])
    for r in rows:
        n = 2 if r.cluster_id == "alpha" else 3
        assert r.active_dev_days == n
        assert r.cost_baseline_nano == n * one
        assert r.cost_actual_nano == pytest.approx(0.8 * r.cost_baseline_nano, abs=10)
        assert r.outcome_prs is None
    alpha = [r for r in rows if r.cluster_id == "alpha"]
    beta = [r for r in rows if r.cluster_id == "beta"]
    assert all(not r.treated and r.arm == "control" for r in alpha)
    assert [r.treated for r in beta] == [False, False, True]
    assert beta[-1].arm == LEVER and beta[-1].wave == "1" and beta[0].arm is None


def test_arms_mapping_overrides_tags_itt() -> None:
    store = _store()
    kw = {"cluster_kind": "team", "since": DAYS[0], "until": "2026-09-04",
          "baseline_pricer": FakePricer(), "actual_pricer": FakePricer()}
    rows = P.build_panel(store, arms={"beta": f"{LEVER}@2026-09-02", "alpha": "holdback"}, **kw)
    beta = [r.treated for r in rows if r.cluster_id == "beta"]
    assert beta == [False, True, True]
    assert all(r.arm == LEVER for r in rows if r.cluster_id == "beta")
    assert all(r.arm == "holdback" and not r.treated for r in rows if r.cluster_id == "alpha")
    rows = P.build_panel(store, arms={"beta": LEVER}, **kw)
    assert [r.treated for r in rows if r.cluster_id == "beta"] == [False, False, True]
    # a treatment arm with no tag and no date: never observed adopting
    rows = P.build_panel(store, arms={"alpha": LEVER}, **kw)
    assert not any(r.treated for r in rows if r.cluster_id == "alpha")
    # another experiment's tag does not start this arm's treatment
    rows = P.build_panel(store, arms={"beta": "other.lever"}, **kw)
    assert not any(r.treated for r in rows if r.cluster_id == "beta")
    with pytest.raises(UsageError):
        P.build_panel(store, arms={"beta": "@2026-09-02"}, **kw)
    with pytest.raises(UsageError):
        P.build_panel(store, arms={"beta": "lever@notadate"}, **kw)


def test_allowance_is_measured_separately() -> None:
    sub = [request("alpha", DAYS[0], 9, billing_path="subscription", seq=5)]
    store = _store(more=sub)
    kw = {"cluster_kind": "team", "since": DAYS[0], "until": "2026-09-04",
          "baseline_pricer": FakePricer(), "actual_pricer": FakePricer()}
    billed = P.build_panel(store, **kw)
    allowance = P.build_panel(store, billing_class="allowance", **kw)
    first = next(r for r in billed if r.cluster_id == "alpha" and r.date_utc == DAYS[0])
    assert first.active_dev_days == 3                      # the seat user is a developer-day
    one = _priced(FakePricer(), [request("x", DAYS[0], 1)])
    assert first.cost_baseline_nano == 2 * one             # allowance never in billed cost
    a_first = next(r for r in allowance if r.cluster_id == "alpha" and r.date_utc == DAYS[0])
    assert a_first.cost_baseline_nano > 0
    assert sum(r.cost_baseline_nano for r in allowance) == a_first.cost_baseline_nano
    with pytest.raises(UsageError):
        P.build_panel(store, billing_class="seat", **kw)


def test_outcomes_fill_outcome_prs_for_team_clusters() -> None:
    outs = [OutcomeAggregate(date_utc=DAYS[1], team="beta", n_users=5, sessions=9, commits=4,
                             pull_requests=7, lines_added=1, lines_removed=1, edits_accepted=1,
                             edits_rejected=0),
            OutcomeAggregate(date_utc=DAYS[1], team="beta", n_users=5, sessions=9, commits=4,
                             pull_requests=3, lines_added=1, lines_removed=1, edits_accepted=1,
                             edits_rejected=0, source_kind="other")]
    store = _store(outcomes=outs)
    rows = P.build_panel(store, cluster_kind="team", since=DAYS[0], until="2026-09-04",
                         baseline_pricer=FakePricer(), actual_pricer=FakePricer())
    got = {(r.cluster_id, r.date_utc): r.outcome_prs for r in rows}
    assert got[("beta", DAYS[1])] == 7
    assert got[("beta", DAYS[0])] is None and got[("alpha", DAYS[1])] is None


def test_mdm_group_and_workspace_clusters() -> None:
    store = MemoryStore(org_key=ORG_KEY)
    ingest(store, [request("g1", DAYS[0], 1, kind="mdm_group"),
                   request("g2", DAYS[0], 2, kind="mdm_group"),
                   request("ws-a", DAYS[1], 3, kind="workspace")])
    kw = {"since": DAYS[0], "until": "2026-09-04", "baseline_pricer": FakePricer(),
          "actual_pricer": FakePricer()}
    mdm = P.build_panel(store, cluster_kind="mdm_group", **kw)
    assert [r.cluster_id for r in mdm] == ["g1", "g2"]
    ws = P.build_panel(store, cluster_kind="workspace", **kw)
    assert [(r.cluster_id, r.date_utc, r.active_dev_days) for r in ws] == [("ws-a", DAYS[1], 1)]
    # the store decides which cluster kinds it can count developer-days for
    with pytest.raises(UsageError):
        P.build_panel(store, cluster_kind="gateway", **kw)


def test_validation() -> None:
    store = _store()
    kw = {"baseline_pricer": FakePricer(), "actual_pricer": FakePricer()}
    for bad in ({"cluster_kind": "person", "since": DAYS[0], "until": DAYS[1]},
                {"cluster_kind": "team", "since": "x", "until": DAYS[1]},
                {"cluster_kind": "team", "since": DAYS[1], "until": DAYS[1]}):
        with pytest.raises(UsageError):
            P.build_panel(store, **bad, **kw)  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        P.cluster_of(request("a", DAYS[0], 1), "person")
    with pytest.raises(UsageError):
        P.parse_arm("")
    assert P.parse_arm("x") == ("x", None)


def test_rate_variance_org_series_and_window() -> None:
    store = _store()
    rows = P.build_panel(store, cluster_kind="team", since=DAYS[0], until="2026-09-04",
                         baseline_pricer=FakePricer(), actual_pricer=FakePricer(contract=CUT20))
    rv = P.rate_variance(rows, basis=Basis.CONTRACT)
    assert rv.evidence is Evidence.EXACT and rv.basis is Basis.CONTRACT
    post = [r for r in rows if r.date_utc >= "2026-09-03"]
    assert rv.nano == sum(r.cost_actual_nano - r.cost_baseline_nano for r in post) < 0
    everything = P.rate_variance(rows, post_from="2026-09-01")
    assert everything.nano == sum(r.cost_actual_nano - r.cost_baseline_nano for r in rows)
    untreated = [PanelRow(r.cluster_id, r.date_utc, r.cost_baseline_nano, r.cost_actual_nano,
                          r.active_dev_days, r.arm, r.wave, False) for r in rows]
    assert P.rate_variance(untreated).nano == everything.nano
    series = P.org_series(rows)
    assert [d for d, _, _ in series] == list(DAYS)
    assert all(n == 5 for _, _, n in series)
    assert P.panel_window(rows) == (DAYS[0], DAYS[2])
    with pytest.raises(UsageError):
        P.panel_window([])


def test_panel_channels_and_cache_scopes() -> None:
    store = MemoryStore(org_key=ORG_KEY)
    reqs = [request("alpha", DAYS[0], 1), request("beta", DAYS[0], 3)]
    reqs.append(make_request("L-bed", 0, reqs[0].ts_start_ms + 5, {"uncached_input": 100},
                             "claude-opus-5", channel="bedrock",
                             attribution={"team": "beta", "principal": "r_dev3"},
                             message_id="msg_bed"))
    shells = [Session(session_key="s_test", source_kind="test", attribution=Attribution(),
                      lanes=(Lane(lane_key=reqs[0].lane_key, session_key="s_test",
                                  kind=LaneKind.MAIN, parent_lane_key=None,
                                  cache_scope_key="ws:shared", requests=()),
                             Lane(lane_key=reqs[1].lane_key, session_key="s_test",
                                  kind=LaneKind.MAIN, parent_lane_key=None,
                                  cache_scope_key="ws:shared", requests=())),
                      started_ms=0, ended_ms=0)]
    store.ingest(IngestResult(source=source("shells"), requests=reqs, sessions=shells, events=[],
                              aggregates=[], cost_lines=[], outcomes=[], quarantined=[], notes=[],
                              stats={}, capabilities=frozenset()))
    chans = P.panel_channels(store, cluster_kind="team", since=DAYS[0], until=DAYS[1])
    assert chans == ("anthropic_api", "bedrock")
    assert P.panel_channels(store, cluster_kind="team", since=DAYS[0], until=DAYS[1],
                            billing_class="allowance") == ()
    scopes = P.cache_scope_clusters(store, cluster_kind="team", since=DAYS[0], until=DAYS[1])
    assert scopes == {"ws:shared": ("alpha", "beta")}   # the bedrock lane's scope is unknown
    with pytest.raises(UsageError):
        P.panel_channels(store, cluster_kind="person", since=DAYS[0], until=DAYS[1])
    with pytest.raises(UsageError):
        P.cache_scope_clusters(store, cluster_kind="person", since=DAYS[0], until=DAYS[1])
