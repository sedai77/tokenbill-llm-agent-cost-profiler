"""copilot.teammap.build_maps: team and cost-center maps (addendum §15 ``copilot team-map``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tokenbill.copilot.teammap import build_maps, user_team_records
from tokenbill.core.builders import CANARY_LOGIN
from tokenbill.core.errors import UsageError

from .helpers import FIXTURES

USER_TEAMS = FIXTURES / "metrics" / "user-teams-1-day.ndjson"
COST_CENTERS = FIXTURES / "config" / "cost_centers.json"


def _row(login: str, slug: str, day: str = "2026-09-20") -> dict[str, object]:
    return {"user_id": 1, "user_login": login, "day": day, "team_id": 1, "slug": slug}


def test_fixture_maps() -> None:
    team_map, cc_map = build_maps(USER_TEAMS, COST_CENTERS, k=5)
    assert "mobile" not in set(team_map.values())                 # the 3-person team
    assert team_map[CANARY_LOGIN] == "platform"
    assert team_map["dev-10"] == "payments"                       # payments (5) beats mobile (3)
    assert team_map["dev-12"] == "platform"                       # platform (6) beats mobile (3)
    assert "dev-11" not in team_map                               # only in the 3-person team
    assert sorted(set(team_map.values())) == ["payments", "platform"]
    assert list(team_map) == sorted(team_map)
    assert cc_map[CANARY_LOGIN] == "cc-platform"                  # direct User resource
    assert cc_map["dev-02"] == "cc-platform"                      # via the Team resource
    assert cc_map["dev-07"] == "cc-data" and "dev-04" in cc_map
    assert "cc-retired" not in set(cc_map.values())               # deleted cost center


def test_three_person_team_absent_and_k() -> None:
    rows = [_row(f"a{i}", "big") for i in range(5)] + [_row(f"b{i}", "small") for i in range(3)]
    team_map, _ = build_maps(rows, None, k=5)
    assert set(team_map.values()) == {"big"} and len(team_map) == 5
    team_map3, _ = build_maps(rows, None, k=3)
    assert set(team_map3.values()) == {"big", "small"}


def test_team_size_is_the_largest_day() -> None:
    rows = [_row(f"a{i}", "t", "2026-09-20") for i in range(3)]
    rows += [_row(f"a{i}", "t", "2026-09-21") for i in range(3, 6)]
    assert build_maps(rows, None, k=5)[0] == {}                  # never 5 members on one day
    rows += [_row(f"a{i}", "t", "2026-09-22") for i in range(5)]
    assert len(build_maps(rows, None, k=5)[0]) == 6


def test_multi_team_login_is_deterministic() -> None:
    rows = [_row(f"x{i}", "zeta") for i in range(6)] + [_row(f"y{i}", "alpha") for i in range(6)]
    rows += [_row("both", "zeta"), _row("both", "alpha"), _row("most", "medium"),
             _row("most", "large")] + [_row(f"z{i}", "large") for i in range(9)]
    rows += [_row(f"m{i}", "medium") for i in range(6)]
    team_map, _ = build_maps(rows, None, k=5)
    assert team_map["both"] == "alpha"                            # tie (7 vs 7): smaller slug
    assert team_map["most"] == "large"                            # the team with most members
    assert build_maps(list(reversed(rows)), None, k=5)[0] == team_map


def test_no_emails_anywhere() -> None:
    rows = [_row(f"u{i}", "t") for i in range(5)] + [_row("mona@example.com", "t"),
                                                     _row("v", "odd@team")]
    ccs = {"costCenters": [
        {"name": "cc", "resources": [{"type": "User", "name": "x@y.org"},
                                     {"type": "User", "name": "u1"}]},
        {"name": "someone@corp.com", "resources": [{"type": "User", "name": "u2"}]}]}
    team_map, cc_map = build_maps(rows, ccs, k=5)
    text = json.dumps([team_map, cc_map])
    assert "@" not in text and cc_map == {"u1": "cc"}


def test_inputs_as_paths_records_and_envelopes(tmp_path: Path) -> None:
    lines = USER_TEAMS.read_text(encoding="utf-8").splitlines()
    env = tmp_path / "ut.json"
    env.write_text(json.dumps({"request": {"path": "/x"},
                               "response": [json.loads(x) for x in lines]}), encoding="utf-8")
    records = [json.loads(x) for x in lines]
    ref = build_maps(USER_TEAMS, COST_CENTERS)
    assert build_maps([env], [COST_CENTERS]) == ref
    assert build_maps(records, json.loads(COST_CENTERS.read_text(encoding="utf-8"))) == ref
    assert build_maps(str(USER_TEAMS), str(COST_CENTERS)) == ref
    single = json.loads(COST_CENTERS.read_text(encoding="utf-8"))["response"]["costCenters"][0]
    assert build_maps(records, single)[1][CANARY_LOGIN] == "cc-platform"
    folder = tmp_path / "dir"
    folder.mkdir()
    (folder / "a.ndjson").write_text("\n".join(lines[:20]) + "\n", encoding="utf-8")
    (folder / "b.ndjson").write_text("\n".join(lines[20:]) + "\n", encoding="utf-8")
    assert build_maps(folder, None)[0] == ref[0]


def test_bad_inputs() -> None:
    with pytest.raises(UsageError):
        build_maps([], None, k=0)
    with pytest.raises(UsageError):
        build_maps([42], None)  # type: ignore[list-item]
    rows = [{"user_login": "", "slug": "t"}, {"user_login": "a"}, {"user_login": "b", "slug": "t",
                                                                   "day": "someday"}]
    assert user_team_records(rows) == [("b", "t", "")]
    ccs = {"costCenters": [{"name": "c", "resources": "nope"},
                           {"name": "d", "resources": [1, {"type": 3}, {"type": "Team"}]}]}
    assert build_maps([], ccs) == ({}, {})
