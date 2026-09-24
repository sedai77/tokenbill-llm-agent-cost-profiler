"""core.kanon for GitHub Copilot (CORE-AMENDMENTS K-4; rulings R-E10, R-E16, R-E31, R-E37; F-KIT-C
acceptance): scope counting, the Copilot parent chain, the entity exemption, users-unknown rows,
summaries that are never cut mid-sentence and merged pool totals. Every merged ``test_kanon.py``
case stays unchanged and green."""

from __future__ import annotations

import dataclasses
import functools
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core import kanon
from tokenbill.core import testing as kit
from tokenbill.core.builders import (
    make_ai_usage_row,
    make_config,
    make_copilot_ctx,
    make_license,
    make_request,
)
from tokenbill.core.errors import ContractViolation, PrivacyError, UsageError
from tokenbill.core.ids import key_id, pseudonym
from tokenbill.core.labels import Basis, estimated, exact
from tokenbill.core.records import UsageBuckets
from tokenbill.core.registry import _finding_id
from tokenbill.core.types import (
    AggRow,
    Finding,
    IngestResult,
    PricedTotal,
    RawAggregate,
    Scope,
    SourceInfo,
)

K = 5
ORG = bytes(range(40, 72))
W = {"since_ms": 0, "until_ms": 2**53}
SEATS, ORGSCAN, LANES = "copilot.seats-budgets", "copilot.org-scan", "copilot.lanes"


def p(name: str) -> str:
    return pseudonym(ORG, "p", name)


def finding(detector_id: str, kind: str, dims: dict[str, str], n_users: int, *,
            category: str = "aggregate", cost: int = 100, headroom: int | None = None,
            summary: str = "s", audience: str = "org") -> Finding:
    scope = Scope(dims=tuple(sorted(dims.items())))
    return Finding(
        finding_id=_finding_id(detector_id, kind, scope), detector_id=detector_id, kind=kind,
        detector_version="1", category=category, lever_class="none", audience=audience,
        title=f"{kind} finding", summary=summary, scope=scope, n_events=1, n_lanes=0,
        n_users=n_users, first_seen_ms=0, cost_observed=exact(cost, Basis.LIST_EQUIVALENT),
        recoverable=None, references=("r",),
        headroom=None if headroom is None else estimated(headroom, Basis.LIST_EQUIVALENT,
                                                         note="h"))


def copilot(**dims: str) -> dict[str, str]:
    return {"product": "copilot", "entity": "enterprise", **dims}


def _source(source_id: str) -> SourceInfo:
    return SourceInfo(source_id=source_id, adapter="github-ai-usage", name_hmac="h_" + "0" * 20,
                      sha256=source_id, bytes=1, name_key_id=None, principal_key_id=key_id(ORG))


def ledger_with_cost_lines(rows: list[tuple[str, str, str | None]]) -> kit.MemoryStore:
    """A MemoryStore holding one AI-usage cost line per (person, team, cost center) row."""
    store = kit.MemoryStore(org_key=ORG)
    lines = []
    for i, (who, team, cc) in enumerate(rows):
        line, _ = make_ai_usage_row(principal=p(who), team=team, cost_center=cc,
                                    credits=str(10 + i), repo=None,
                                    model="Claude Opus 5.5" if i % 2 else "GPT-5.5")
        lines.append(line)
    store.ingest(IngestResult(source=_source("lines"), requests=[], sessions=[], events=[],
                              aggregates=[], cost_lines=lines, outcomes=[], quarantined=[],
                              notes=[], stats={}, capabilities=frozenset()))
    return store


TWO_TEAMS = [("u1", "a", "cc1"), ("u2", "a", "cc1"), ("u3", "a", "cc1"),
             ("u1", "b", "cc1"), ("u4", "b", "cc1"), ("u5", "b", "cc1")]


# ---------- scope_counter ----------


def test_person_in_two_teams_counted_once_at_the_cost_center_parent() -> None:
    store = ledger_with_cost_lines(TWO_TEAMS)
    count = kanon.scope_counter(store, (), **W)
    fa = finding(ORGSCAN, "premium-model-share", copilot(team="a", cost_center="cc1"), 3)
    fb = finding(ORGSCAN, "premium-model-share", copilot(team="b", cost_center="cc1"), 3)
    assert count(fa, fa.scope) == 3
    (merged,) = kanon.rescope_findings([fa, fb], k=K, count_users=count)
    assert dict(merged.scope.dims) == {"product": "copilot", "entity": "enterprise",
                                       "cost_center": "cc1"}
    assert merged.n_users == 5  # distinct people, not 3 + 3
    assert kanon.rescope_findings([fa, fb], k=6, count_users=count) == []  # never inflated to 6
    # without a counter the merge takes the largest child count (a lower bound)
    assert kanon.rescope_findings([fa, fb], k=K) == []


def test_a_team_scope_counts_cost_line_principals_not_requests() -> None:
    store = ledger_with_cost_lines(TWO_TEAMS)
    reqs = [make_request(f"L{i}", 0, 1_790_000_000_000 + i, {"output": 1}, "claude-opus-5-5",
                         provider="github", channel="github_copilot",
                         billing_path="copilot_pool",
                         attribution={"principal": p(f"x{i}"), "team": "a",
                                      "billing_path": "copilot_pool"})
            for i in range(7)]
    store.ingest(IngestResult(source=_source("reqs"), requests=reqs, sessions=[], events=[],
                              aggregates=[], cost_lines=[], outcomes=[], quarantined=[],
                              notes=[], stats={}, capabilities=frozenset()))
    count = kanon.scope_counter(store, (), **W)
    team_a = Scope(dims=(("product", "copilot"), ("team", "a")))
    by_lines = finding(ORGSCAN, "fast-mode", copilot(team="a"), 1)
    by_requests = finding(LANES, "compaction-cost", copilot(team="a"), 1)
    assert count(by_lines, team_a) == 3
    assert count(by_requests, team_a) == 7
    forced = kanon.scope_counter(store, (), source_of=lambda f: "cost_lines", **W)
    assert forced(by_requests, team_a) == 3


def test_a_seat_scope_counts_licenses_then_seat_count_rows() -> None:
    ledger = kit.MemoryStore(org_key=ORG)
    rs = kit.MemoryRecordStore(ledger)
    result = kit._rs_result([make_license(p(f"s{i}"), snapshot_date="2026-09-20", team="t",
                                          last_activity_bucket="none_90d") for i in range(6)])
    counts = rs.put(result, principal_key_id=key_id(ORG))
    assert counts["licenses"] == 6
    count = kanon.scope_counter(ledger, [rs], **W)
    idle = finding(SEATS, "idle-seat", copilot(team="t", bucket="none_90d"), 6,
                   category="lever")
    assert count(idle, idle.scope) == 6
    assert count(idle, Scope(dims=(("bucket", "0-7"), ("product", "copilot")))) == 0
    # aggregate-only bundle: no licenses, a seat_counts summary row for team t
    summary_only = kit.MemoryRecordStore(ledger)
    summary_only.put(kit._rs_result(config=[make_config(
        "seat_counts", {"team": "t", "bucket": "none_90d", "n": 7, "n_people": 7},
        entity_id="org:org-a", snapshot_ms=1_790_000_000_000)]), principal_key_id=None)
    count2 = kanon.scope_counter(ledger, [summary_only], **W)
    assert count2(idle, idle.scope) == 7
    # several record stores: the largest per-store count (a lower bound)
    assert kanon.scope_counter(ledger, [rs, summary_only], **W)(idle, idle.scope) == 7
    assert kanon.scope_counter(ledger, [], **W)(idle, idle.scope) == 0


def test_scope_to_where_mapping() -> None:
    where = kanon._where_for
    sc = Scope
    assert where(sc(dims=(("entity", "cc:platform"), ("product", "copilot"))), "cost_lines") == {
        "cost_center": "platform", "channel": "github_copilot"}
    assert where(sc(dims=(("entity", "org:acme"),)), "cost_lines") == {"workspace_id": "acme"}
    assert where(sc(dims=(("entity", "org:acme"),)), "licenses") == {"org": "acme"}
    assert where(sc(dims=(("org", "acme"),)), "requests") == {"workspace_id": "acme"}
    assert where(sc(dims=(("entity", "enterprise"), ("plan_scenario", "business"))),
                 "licenses") == {}
    assert where(sc(dims=(("product", "copilot"), ("plan", "business"))), "licenses") == {
        "product": "github_copilot", "plan": "business"}
    assert where(sc(dims=(("plan", "business"),)), "cost_lines") is None  # not filterable there
    assert where(sc(dims=(("product", "cursor"),)), "requests") is None
    assert where(sc(dims=(("entity", "budget:1"),)), "cost_lines") is None
    assert where(sc(dims=(("entity", "cc:x"), ("cost_center", "y"))), "cost_lines") is None
    assert where(sc(dims=(("lane_kind", "main"), ("team", "t"))), "requests") == {
        "lane_kind": "main", "team": "t"}


def test_scope_counter_edges() -> None:
    store = ledger_with_cost_lines(TWO_TEAMS)
    f = finding(SEATS, "pool-regime", copilot(team="a"), 1)  # entity source, not exempt
    assert kanon.scope_counter(store, (), **W)(f, f.scope) == 3  # counted over cost lines
    assert kanon.scope_counter(None, (), **W)(f, f.scope) == 0
    req = finding(LANES, "ci-uncapped", copilot(team="a"), 1)
    assert kanon.scope_counter(None, (), **W)(req, req.scope) == 0
    odd = kanon.scope_counter(store, (), source_of=lambda _f: "tea-leaves", **W)
    assert odd(f, f.scope) == 0
    plan = Scope(dims=(("plan", "business"), ("product", "copilot")))
    assert kanon.scope_counter(store, (), **W)(finding(ORGSCAN, "fast-mode", {}, 1), plan) == 0


# ---------- R-E16 exemption and the Copilot parent chain ----------


def test_entity_scope_with_entity_count_source_is_published_with_three_users() -> None:
    regime = finding(SEATS, "pool-regime", copilot(plan_scenario="business"), 3)
    status = finding(SEATS, "plan-status", copilot(model="claude-opus-5-5", sku="x"), 2)
    skipped = finding(SEATS, "dq.skipped-kinds", copilot(), 0, category="data-quality")
    out = kanon.rescope_findings([regime, status, skipped], k=K, count_users=lambda s: 0)
    assert set(out) == {regime, status, skipped}


def _team_small(_f: Finding, scope: Scope) -> int:
    """3 people in any team-level scope, 9 above it."""
    return 3 if any(name == "team" for name, _ in scope.dims) else 9


def test_team_scoped_aggregate_findings_are_rescoped_whatever_their_category() -> None:
    small = finding(ORGSCAN, "premium-model-share", copilot(team="a"), 3, category="aggregate")
    entity_team = finding(ORGSCAN, "agentic-workflow-cost", copilot(team="a"), 3)
    assert not kanon._exempt(small) and not kanon._exempt(entity_team)
    out = kanon.rescope_findings([small, entity_team], k=K, count_users=_team_small)
    scopes = {f.kind: dict(f.scope.dims) for f in out}
    assert scopes["premium-model-share"] == {"product": "copilot", "entity": "enterprise"}
    assert scopes["agentic-workflow-cost"] == {"product": "copilot", "entity": "enterprise"}
    # the same finding without a product dim keeps the R-E9 aggregate exemption
    plain = finding("aggregate.org-scan", "cache-read-share", {"team": "a"}, 3)
    assert kanon._exempt(plain)
    assert kanon.rescope_findings([plain], k=K) == [plain]


def test_plan_scenario_survives_every_rescoping_level() -> None:
    dims = copilot(team="t", bucket="none_90d", plan="business", model="m",
                   cost_center="cc1", plan_scenario="enterprise", surface="vscode")
    levels = kanon.COPILOT_RESCOPE_LEVELS
    for level in range(len(levels)):
        f = finding(SEATS, "idle-seat", dims, 1, category="lever")

        def count(_f: Finding, scope: Scope, at: int = level) -> int:
            names = {n for n, _ in scope.dims}
            return 9 if names == set(dims) & levels[at] else 0

        (out,) = kanon.rescope_findings([f], k=K, count_users=count)
        got = dict(out.scope.dims)
        assert got["plan_scenario"] == "enterprise" and got["product"] == "copilot"
        assert got["entity"] == "enterprise" and "surface" not in got
        assert set(got) == set(dims) & levels[level]
    assert kanon.rescope_findings([finding(SEATS, "idle-seat", dims, 1)], k=K,
                                  count_users=lambda f, s: 0) == []


def test_scenario_findings_never_merge_across_scenarios() -> None:
    fb = finding(SEATS, "idle-seat", copilot(team="t", plan_scenario="business"), 2, cost=190)
    fe = finding(SEATS, "idle-seat", copilot(team="t", plan_scenario="enterprise"), 2, cost=390)
    out = kanon.rescope_findings([fb, fe], k=K, count_users=lambda f, s: 6)
    assert sorted(dict(f.scope.dims)["plan_scenario"] for f in out) == ["business", "enterprise"]
    assert sorted(f.cost_observed.nano for f in out) == [190, 390]


def test_merged_findings_sum_headroom() -> None:
    a = finding(ORGSCAN, "auto-adoption", copilot(team="a"), 2, headroom=400)
    b = finding(ORGSCAN, "auto-adoption", copilot(team="b"), 2, headroom=600)
    c = finding(ORGSCAN, "auto-adoption", copilot(team="c"), 2)
    (merged,) = kanon.rescope_findings([a, b, c], k=K, count_users=_team_small)
    assert merged.headroom is not None and merged.headroom.nano == 1000
    assert merged.headroom.basis is Basis.LIST_EQUIVALENT
    assert merged.cost_observed.nano == 300


def test_budget_findings_never_name_a_person() -> None:
    bad = finding(SEATS, "budget-zero-user-budget", copilot(team=p("u1")), 1)
    with pytest.raises(PrivacyError):
        kanon.rescope_findings([bad], k=K)
    with pytest.raises(PrivacyError):
        kanon.rescope_findings([finding(SEATS, "budget-ulb-gap",
                                        copilot(principal="r_someone"), 9)], k=K)
    ok = finding(SEATS, "budget-zero-user-budget", copilot(team="t"), 5)
    assert kanon.rescope_findings([ok], k=K) == [ok]


def test_person_dims_in_copilot_scopes_are_rescoped() -> None:
    f = finding(LANES, "subagent-share", copilot(team="t", session_key="s1"), 9)
    (out,) = kanon.rescope_findings([f], k=K, count_users=lambda f, s: 9)
    assert dict(out.scope.dims) == copilot(team="t")


def test_copilot_and_default_chains_do_not_mix() -> None:
    claude = finding("cache.miss-by-cause", "model-switch",
                     {"team": "t", "lane_kind": "main", "billing_class": "billed"}, 2,
                     category="lever")
    pool = finding("cache.miss-by-cause", "model-switch",
                   {"team": "t", "lane_kind": "main", "billing_class": "pool",
                    "product": "copilot"}, 2, category="lever")
    big_pool = finding("cache.miss-by-cause", "model-switch",
                       {"team": "t", "lane_kind": "subagent", "billing_class": "pool",
                        "product": "copilot"}, 9, category="lever")
    out = kanon.rescope_findings([claude, pool, big_pool], k=K, count_users=lambda s: 6)
    by_class = {dict(f.scope.dims).get("billing_class"): f for f in out}
    assert set(by_class) == {"billed", "pool"}
    assert "product" not in dict(by_class["billed"].scope.dims)
    # the small Copilot child absorbed its Copilot sibling (complementary suppression), never the
    # Claude finding
    assert by_class["pool"].cost_observed.nano == 200
    assert by_class["billed"].cost_observed.nano == 100


class _CallableCounter:
    def __call__(self, scope: Scope) -> int:
        return 7


def _two(finding: Finding, scope: Scope, extra: int = 0) -> int:
    return 7 + extra


@pytest.mark.parametrize("counter,arity", [
    (lambda scope: 7, 1), (_two, 2), (_CallableCounter(), 1),
    (functools.partial(_two, extra=1), 2), (len, 1), (lambda *args: 7, 2),
    (lambda scope, *rest: 7, 2), (lambda scope, extra=0: 7, 1),
])
def test_count_users_arity(counter, arity: int) -> None:
    assert kanon._counter_arity(counter) == arity


def test_one_argument_counters_still_work_for_copilot_findings() -> None:
    f = finding(ORGSCAN, "fast-mode", copilot(team="a"), 2)
    seen: list[Scope] = []

    def one(scope: Scope) -> int:
        seen.append(scope)
        return 5

    (out,) = kanon.rescope_findings([f], k=K, count_users=one)
    assert out.n_users == 5 and seen == [out.scope]


def test_rescope_rejects_non_findings() -> None:
    with pytest.raises(ContractViolation):
        kanon.rescope_findings(["x"])  # type: ignore[list-item]


@settings(max_examples=150, deadline=None)
@given(st.lists(st.tuples(st.sampled_from(["a", "b", "c", "d"]), st.sampled_from(["x", "y"]),
                          st.integers(0, 9), st.sampled_from([None, "business", "enterprise"])),
                min_size=1, max_size=10, unique_by=lambda t: (t[0], t[1], t[3])),
       st.integers(0, 12))
def test_copilot_rescope_property(cells, users_per_scope: int) -> None:
    findings = [finding(ORGSCAN, "fast-mode",
                        {**copilot(team=t, model=m),
                         **({"plan_scenario": sc} if sc else {})}, n)
                for t, m, n, sc in cells]
    out = kanon.rescope_findings(findings, k=K, count_users=lambda f, s: users_per_scope)
    ids = [f.finding_id for f in out]
    assert len(ids) == len(set(ids))
    for f in out:
        assert f.n_users >= K
        assert dict(f.scope.dims)["product"] == "copilot"
    scenarios = {dict(f.scope.dims).get("plan_scenario") for f in out}
    assert scenarios <= {None, "business", "enterprise"}
    total = sum(f.cost_observed.nano for f in out)
    assert total <= 100 * len(findings)


# ---------- R-E10: users-unknown rows ----------


def priced(nano: int, *, pool: int | None = None) -> PricedTotal:
    return PricedTotal(exact=exact(nano, Basis.LIST), estimated=None, allowance=None,
                       priced_inferences=1, unpriced_inferences=0, unpriced_tokens=0,
                       coverage="1",
                       pool=None if pool is None else exact(pool, Basis.LIST_EQUIVALENT))


def row(dims: dict, n_users: int, value: int, *, pool: int | None = None) -> AggRow:
    return AggRow(dims=tuple(dims.items()), n_users=n_users, n_requests=value % 7 + 1,
                  usage=UsageBuckets(uncached_input=value), priced=priced(value, pool=pool))


def raw(group_by: tuple[str, ...], rows: list[AggRow]) -> RawAggregate:
    return RawAggregate(group_by=group_by, rows=tuple(rows), window=(0, 10))


def test_users_unknown_rows_are_published_by_model() -> None:
    table = raw(("model",), [row({"model": "m1"}, 0, 100), row({"model": "m2"}, 0, 200)])
    pub = kanon.publish(table, k=K)
    assert [dict(r.dims)["model"] for r in pub.rows] == ["m1", "m2"]
    assert all(kanon.row_notes(r, group_by=pub.group_by) == (kanon.USERS_UNKNOWN,)
               for r in pub.rows)
    assert (pub.suppressed_rows, pub.suppressed_users) == (0, 0)


def test_users_unknown_rows_with_a_person_proxy_key_stay_suppressed() -> None:
    for key in ("api_key_id", "cwd_key", "session", "principal", "session_key"):
        table = raw(("model", key), [row({"model": "m1", key: "v1"}, 0, 100)])
        pub = kanon.publish(table, k=K)
        assert pub.rows == () and pub.suppressed_rows == 1, key
    assert kanon.row_notes(row({"model": "m"}, 0, 1), group_by=("api_key_id",)) == ()
    assert kanon.row_notes(row({"model": "m"}, 6, 1), group_by=("model",)) == ()


def test_users_unknown_rows_beside_counted_rows() -> None:
    table = raw(("team",), [row({"team": "a"}, 10, 1), row({"team": "b"}, 2, 2),
                            row({"team": "c"}, 1, 3), row({"team": "d"}, 0, 4)])
    pub = kanon.publish(table, k=K)
    got = {dict(r.dims)["team"]: r.n_users for r in pub.rows}
    assert got == {kanon.other_label(K): 10, "d": 0}  # b and c absorbed a (complementary)
    assert sum(r.usage.uncached_input for r in pub.rows) == 10


def test_self_audience_never_suppresses() -> None:
    table = raw(("principal", "model"), [row({"principal": p("me"), "model": "m"}, 1, 5)])
    pub = kanon.publish(table, k=K, audience="self")
    assert pub.rows == table.rows and pub.suppressed_rows == 0
    assert kanon.publish(table, k=K).rows == ()
    with pytest.raises(UsageError):
        kanon.publish(table, audience="everyone")


# ---------- R-E37: pool carried through merges ----------


def test_merged_rows_keep_the_pool() -> None:
    table = raw(("team",), [row({"team": "a"}, 3, 10, pool=7), row({"team": "b"}, 3, 20, pool=5),
                            row({"team": "c"}, 9, 30)])
    (merged,) = kanon.publish(table, k=K).rows
    assert merged.priced.pool is not None and merged.priced.pool.nano == 12
    assert merged.priced.exact.nano == 60


# ---------- R-E31: summaries are never cut mid-sentence ----------

LABEL = "Copilot credits are list-equivalent AI-credit value, not invoice dollars."


def test_short_summaries_are_unchanged() -> None:
    prefix = "[re-scoped for k-anonymity (k=5); 2 finding(s) merged] "
    assert kanon._fit_summary(prefix, "Plain summary.") == prefix + "Plain summary."


def _long_summary(label_first: bool) -> str:
    parts = [LABEL]
    i = 0
    while len(" ".join(parts)) < 389:
        nxt = f"Detail {i} is here." if len(" ".join(parts)) < 370 else "Ok."
        parts.insert(len(parts) if label_first else len(parts) - 1, nxt)
        i += 1
    summary = " ".join(parts)
    assert 389 <= len(summary) <= 400
    return summary


def test_long_summaries_keep_the_labelling_statement() -> None:
    summary = _long_summary(label_first=False)
    prefix = "[re-scoped for k-anonymity (k=5); 12 finding(s) merged] "
    out = kanon._fit_summary(prefix, summary)
    assert len(out) <= 400 and LABEL in out
    body = out[out.index("] ") + 2:]
    kept = [s for s in kanon._sentences(summary) if s in body]
    assert body.startswith(kept[0])
    assert all(s.endswith(".") for s in kept)
    assert out.endswith(LABEL + " …") or out.endswith(LABEL)
    out_short = kanon._fit_summary(prefix, summary[:340])
    assert len(out_short) <= 400
    assert out_short.endswith(summary[:340])  # fits beside a shortened prefix: nothing dropped


def test_merge_keeps_the_label_through_rescoping() -> None:
    summary = _long_summary(label_first=True)
    children = [finding(LANES, "compaction-cost", copilot(team=t), 2, summary=summary)
                for t in ("a", "b", "c")]
    (merged,) = kanon.rescope_findings(children, k=K, count_users=_team_small)
    assert LABEL in merged.summary and len(merged.summary) <= 400
    assert merged.summary.startswith("[re-scoped")


def test_labels_alone_too_long_are_cut_at_a_word_boundary() -> None:
    label = ("This estimated figure is list-equivalent and not invoice dollars " * 8).strip()
    out = kanon._fit_summary("[re-scoped for k-anonymity (k=5); 3 finding(s) merged] ",
                             label + ".")
    assert len(out) <= 400 and out.endswith("…")
    assert out[:-1].rstrip().split(" ")[-1] in label.split(" ")
    assert kanon._cut_words("short", 10) == "short"


@settings(max_examples=200, deadline=None)
@given(st.lists(st.sampled_from([
    "The cohort rewrote its prefix after idle gaps.", LABEL,
    "Estimated range reflects unknown TTLs.", "Evidence lists the top transitions.",
    "No mechanical fix exists for this sub-cause.", "Teams share one model.",
    "A reasonably long sentence that explains how the saving would be realised in practice.",
]), min_size=1, max_size=12), st.integers(1, 99))
def test_fit_summary_property(sentences: list[str], merged: int) -> None:
    summary = " ".join(sentences)[:400]
    prefix = f"[re-scoped for k-anonymity (k=5); {merged} finding(s) merged] "
    out = kanon._fit_summary(prefix, summary)
    assert len(out) <= 400 and out.startswith("[re-scoped")
    if len(prefix) + len(summary) <= 400:
        assert out == prefix + summary
    labels = [s for s in kanon._sentences(summary) if s == LABEL]
    if labels and len(" ".join(labels)) + 20 <= 388:
        assert LABEL in out


def test_random_mixed_tables_publish_consistently() -> None:
    for seed in range(200):
        rnd = random.Random(seed)
        rows = [row({"team": f"t{i}"}, rnd.choice([0, 0, 1, 3, 6, 9]), rnd.randint(1, 10**6))
                for i in range(rnd.randint(1, 7))]
        table = raw(("team",), rows)
        pub = kanon.publish(table, k=K)
        assert all(r.n_users >= K or r.n_users == 0 for r in pub.rows), seed
        zero = {r.dims for r in table.rows if r.n_users == 0}
        assert zero <= {r.dims for r in pub.rows}, seed
        if any(r.n_users >= K for r in table.rows):
            assert sum(r.usage.uncached_input for r in pub.rows) == sum(
                r.usage.uncached_input for r in table.rows), seed


def test_copilot_scoped_self_findings_pass_through() -> None:
    me = finding(LANES, "compaction-cost", copilot(principal=p("me")), 1, audience="self")
    assert kanon.rescope_findings([me], k=K) == [me]


def test_make_copilot_ctx_prices_into_the_pool_through_aggregate() -> None:
    """Merged AggRows from a MemoryStore carry PricedTotal.pool (R-E37 end to end)."""
    store = kit.MemoryStore(org_key=ORG, pricer=kit.FakePricer())
    reqs = [make_request(f"L{t}{i}", 0, 1_790_000_000_000 + i, {"uncached_input": 1000,
                                                               "output": 10},
                         "claude-sonnet-5", provider="github", channel="github_copilot",
                         billing_path="copilot_pool",
                         attribution={"principal": p(f"{t}{i}"), "team": t,
                                      "billing_path": "copilot_pool"})
            for t, n in (("a", 2), ("b", 2), ("c", 6)) for i in range(n)]
    assert make_copilot_ctx().billing_path == "copilot_pool"
    store.ingest(IngestResult(source=_source("agg"), requests=reqs, sessions=[], events=[],
                              aggregates=[], cost_lines=[], outcomes=[], quarantined=[],
                              notes=[], stats={}, capabilities=frozenset()))
    agg = store.aggregate(group_by=["team"], **W)
    assert all(r.priced.pool is not None and r.priced.allowance is None for r in agg.rows)
    total = sum(r.priced.pool.nano for r in agg.rows)  # type: ignore[union-attr]
    pub = kanon.publish(agg, k=K)
    assert sum(r.priced.pool.nano for r in pub.rows) == total  # type: ignore[union-attr]
    assert dataclasses.replace(pub.rows[0].priced).pool is not None
