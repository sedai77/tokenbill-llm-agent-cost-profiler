"""Team k-merge before writing (brief Build 2)."""

from __future__ import annotations

import zipfile
from collections import Counter
from pathlib import Path

import pytest

from tokenbill.copilot.handoff import OTHER_TEAM, read_bundle, write_bundle
from tokenbill.core.builders import make_ai_usage_row, make_config
from tokenbill.core.ids import pseudonym
from tokenbill.core.records import OutcomeAggregate

from .helpers import (
    KEY,
    NOW_MS,
    _result,
    export,
    fake_budget_adapter,
    round_trip_results,
    seed,
    use_fake_registry,
    write_world,
)


def outcome(team: str, n: int, day: str = "2026-09-11") -> OutcomeAggregate:
    return OutcomeAggregate(date_utc=day, team=team, n_users=n, sessions=1, commits=1,
                            pull_requests=1, lines_added=10 * n, lines_removed=0,
                            edits_accepted=n, edits_rejected=0,
                            source_kind="github.copilot_metrics", extra=(("prs_merged", 1),))


def test_teams_of_7_5_3(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    budgets = ({"id": "b1", "team": "alpha", "amount": 30},
               {"id": "b2", "team": "gamma", "amount": 5})
    use_fake_registry(monkeypatch, budgets=fake_budget_adapter())
    world = write_world(tmp_path / "in", budgets=budgets)
    report = export(world, tmp_path / "out.tbx")
    assert report.manifest.teams_merged == 1
    assert ("dq.copilot_export_teams_merged", 1) in report.manifest.dq
    with zipfile.ZipFile(tmp_path / "out.tbx") as zf:
        blob = b"".join(zf.read(n) for n in zf.namelist())
    assert b"gamma" not in blob.lower()
    _, res = read_bundle(tmp_path / "out.tbx")
    gamma = {pseudonym(KEY, "p", login) for login, team in world["team_map"].items()
             if team == "gamma"}
    teams = Counter(lic.team for lic in res.licenses)
    assert teams == {"alpha": 14, "beta": 10, OTHER_TEAM: 6}   # seats API + activity report
    assert {lic.principal for lic in res.licenses if lic.team == OTHER_TEAM} == gamma
    assert {a.principal for a in res.activity if a.team == OTHER_TEAM} == gamma
    assert {c.principal for c in res.cost_lines if c.team == OTHER_TEAM} == gamma
    assert {c.team for c in res.cost_lines if c.principal is None} == {None}
    agg_teams = Counter(dict(a.dims).get("team") for a in res.aggregates)
    assert set(agg_teams) == {"alpha", "beta", OTHER_TEAM, None}
    budget_teams = {dict(c.attrs)["team"] for c in res.config if c.kind == "budget"}
    assert budget_teams == {"alpha", OTHER_TEAM}


def test_merged_aggregates_are_rekeyed_and_summed(tmp_path: Path) -> None:
    rows = [make_ai_usage_row(principal=pseudonym(KEY, "p", f"user-{i}"), team=team,
                              credits="10", input_tokens=100, output_tokens=5)
            for i, team in enumerate(("small-a", "small-b", "small-c"))]
    lines = [r[0] for r in rows]
    aggs = [r[1] for r in rows]
    src = round_trip_results()[0].source
    out = tmp_path / "m.tbx"
    manifest = write_bundle([_result(src, cost_lines=lines, aggregates=aggs)], out,
                            manifest_seed=seed(), leak_terms=frozenset(), k=5,
                            aggregate_only=False)
    assert manifest.teams_merged == 3
    _, res = read_bundle(out)
    (agg,) = res.aggregates
    assert dict(agg.dims)["team"] == OTHER_TEAM
    assert agg.usage.uncached_input == 300 and agg.usage.output == 15
    assert agg.reported_cost_nano == sum(a.reported_cost_nano for a in aggs)
    assert agg.list_cost_nano == sum(a.list_cost_nano for a in aggs)
    assert agg.agg_id not in {a.agg_id for a in aggs}
    assert sorted(c.line_id for c in res.cost_lines) == sorted(c.line_id for c in lines)


def test_outcome_rows_keep_the_adapters_merge(tmp_path: Path) -> None:
    src = round_trip_results()[1].source
    outcomes = [outcome("big", 6), outcome("tiny-1", 2), outcome("tiny-2", 1),
                outcome("(enterprise)", 40), outcome("(org:acme-org)", 3)]
    out = tmp_path / "o.tbx"
    manifest = write_bundle([_result(src, outcomes=outcomes)], out, manifest_seed=seed(),
                            leak_terms=frozenset(), k=5, aggregate_only=False)
    _, res = read_bundle(out)
    by_team = {o.team: o for o in res.outcomes}
    assert set(by_team) == {"big", OTHER_TEAM, "(enterprise)", "(org:acme-org)"}
    merged = by_team[OTHER_TEAM]
    assert merged.n_users == 3 and merged.lines_added == 30 and dict(merged.extra) == {
        "prs_merged": 2}
    assert manifest.teams_merged == 2


def test_budget_teams_without_people_are_merged(tmp_path: Path) -> None:
    conf = [make_config("budget", {"scope": "user", "team": "ghost-team"},
                        entity_id="budget:9", snapshot_ms=NOW_MS)]
    src = round_trip_results()[2].source
    out = tmp_path / "b.tbx"
    manifest = write_bundle([_result(src, config=conf)], out, manifest_seed=seed(),
                            leak_terms=frozenset(), k=5, aggregate_only=False)
    _, res = read_bundle(out)
    assert dict(res.config[0].attrs)["team"] == OTHER_TEAM and manifest.teams_merged == 1


def test_no_merge_when_every_team_is_large(tmp_path: Path) -> None:
    out = tmp_path / "r.tbx"
    manifest = write_bundle(round_trip_results(), out, manifest_seed=seed(),
                            leak_terms=frozenset(), k=5, aggregate_only=False)
    assert manifest.teams_merged == 0
    assert all(c.team in ("alpha", None) for c in read_bundle(out)[1].cost_lines)
