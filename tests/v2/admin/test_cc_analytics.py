"""Claude Code Analytics adapter: per-user day records → (date, team) aggregates and outcomes with
k-anonymity at ingest (SPEC §5.11, §8.4; brief ADMIN acceptance 2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tokenbill.adapters.anthropic_admin import OTHER_TEAM, UNMAPPED_TEAM
from tokenbill.core.builders import CANARY, CANARY_EMAIL

from .helpers import (
    DAY_MS,
    TEAM_MAP,
    assert_person_free,
    dims,
    fixture,
    manifest_entry,
    read,
    tokens,
    write_json,
    write_jsonl,
)

DAY1 = "anthropic/cc_analytics_2026-09-10.json"
DAY2 = "anthropic/cc_analytics_2026-09-11_pages.jsonl"


def _record(email: str | None, day: str = "2026-09-10", *, commits: int = 1,
            model: str = "claude-opus-5", tokens_in: int = 100, api_key: str | None = None,
            cost: object = 12) -> dict:
    actor = ({"type": "api_actor", "api_key_name": api_key} if api_key
             else {"type": "user_actor", "email_address": email})
    return {"date": f"{day}T00:00:00Z", "actor": actor, "organization_id": "org-x",
            "customer_type": "api", "terminal_type": "vscode",
            "core_metrics": {"num_sessions": 2, "lines_of_code": {"added": 10, "removed": 3},
                             "commits_by_claude_code": commits,
                             "pull_requests_by_claude_code": 1},
            "tool_actions": {"edit_tool": {"accepted": 4, "rejected": 1},
                             "write_tool": {"accepted": 2, "rejected": 0}},
            "model_breakdown": [{"model": model,
                                 "tokens": {"input": tokens_in, "output": 20, "cache_read": 300,
                                            "cache_creation": 40},
                                 "estimated_cost": {"currency": "USD", "amount": cost}}]}


def _page(records: list[dict]) -> dict:
    return {"data": records, "has_more": False, "next_page": None}


def _outcomes(result) -> list[tuple[str, str, int, int]]:
    return [(o.date_utc, o.team, o.n_users, o.commits) for o in result.outcomes]


def test_fixture_day1_teams_and_other_group() -> None:
    result = read("anthropic-cc-analytics", fixture(DAY1))
    expect = manifest_entry(DAY1)["expect"]
    got = sorted(_outcomes(result))
    want = sorted((o["date"], o["team"], o["n_users"], o["commits"]) for o in expect["outcomes"])
    assert got == want
    teams = {o.team for o in result.outcomes}
    assert teams == {"platform", "payments", OTHER_TEAM}   # mobile 3 + tiny 2 + (unmapped) 1
    assert len(result.aggregates) == expect["aggregates"]
    assert tokens(result) == expect["usage_tokens"]
    assert sum(a.reported_cost_nano or 0 for a in result.aggregates) == expect[
        "reported_cost_nano"]
    for agg in result.aggregates:
        assert agg.source_kind == "anthropic.cc_analytics"
        assert set(dims(agg)) == {"channel", "model", "team"}
        assert dims(agg)["channel"] == "anthropic_api"
        assert agg.reported_cost_basis == "provider_estimate"
        assert agg.bucket_end_ms - agg.bucket_start_ms == DAY_MS
        assert agg.usage.cache_write_5m == agg.usage.cache_write_1h == 0  # no TTL split
    assert result.capabilities == frozenset({"aggregates", "outcomes", "attribution.team"})
    (note,) = [n for n in result.notes if n.code == "dq.no_ttl_split"]
    assert note.tokens and note.tokens > 0
    assert not [n for n in result.notes if n.code == "dq.outcomes_suppressed"]


def test_outcome_counts_sum_tool_actions() -> None:
    page = json.loads(fixture(DAY1).read_text())
    platform = [r for r in page["data"]
                if TEAM_MAP.get(r["actor"].get("email_address", "")) == "platform"]
    result = read("anthropic-cc-analytics", fixture(DAY1))
    (out,) = [o for o in result.outcomes if o.team == "platform"]
    assert out.sessions == sum(r["core_metrics"]["num_sessions"] for r in platform)
    assert out.lines_added == sum(r["core_metrics"]["lines_of_code"]["added"] for r in platform)
    assert out.lines_removed == sum(r["core_metrics"]["lines_of_code"]["removed"]
                                    for r in platform)
    assert out.pull_requests == sum(r["core_metrics"]["pull_requests_by_claude_code"]
                                    for r in platform)
    assert out.edits_accepted == sum(v["accepted"] for r in platform
                                     for v in r["tool_actions"].values())
    assert out.edits_rejected == sum(v["rejected"] for r in platform
                                     for v in r["tool_actions"].values())
    assert out.source_kind == "anthropic.cc_analytics"


def test_fixture_day2_small_teams_dropped() -> None:
    result = read("anthropic-cc-analytics", fixture(DAY2))
    expect = manifest_entry(DAY2)["expect"]
    assert _outcomes(result) == [(o["date"], o["team"], o["n_users"], o["commits"])
                                 for o in expect["outcomes"]]
    assert {o.team for o in result.outcomes} == {"platform"}  # mobile 3 + tiny 1 < 5: dropped
    (note,) = [n for n in result.notes if n.code == "dq.outcomes_suppressed"]
    assert note.count == expect["dropped_groups"] == 2
    assert note.tokens and note.tokens > 0
    assert result.stats["users_dropped"] == 4
    assert tokens(result) == expect["usage_tokens"]
    assert {dims(a)["team"] for a in result.aggregates} == {"platform"}


def test_no_output_row_carries_a_person() -> None:
    raw = [CANARY_EMAIL, "dev01@example.com", "ci-bot-key", "dc9f6c26", "vscode"]
    for rel in (DAY1, DAY2):
        result = read("anthropic-cc-analytics", fixture(rel))
        assert_person_free(result, *raw)
        assert all(q.locator.startswith(("doc", "line")) for q in result.quarantined)
        assert result.source.principal_key_id is None  # not even pseudonymized


def test_team_with_three_users_merged_or_dropped(tmp_path: Path) -> None:
    team_map = {f"a{i}@x.io": "alpha" for i in range(6)}
    team_map |= {f"b{i}@x.io": "beta" for i in range(3)}
    team_map |= {f"c{i}@x.io": "gamma" for i in range(2)}
    recs = [_record(e) for e in team_map]
    path = write_json(tmp_path / "cc.json", _page(recs))
    result = read("anthropic-cc-analytics", path, team_map=tuple(sorted(team_map.items())))
    assert sorted((o.team, o.n_users) for o in result.outcomes) == [("(other)", 5),
                                                                     ("alpha", 6)]
    # without gamma the 3-user team is alone below k and is dropped
    recs = [_record(e) for e in team_map if not e.startswith("c")]
    path = write_json(tmp_path / "cc2.json", _page(recs))
    result = read("anthropic-cc-analytics", path, team_map=tuple(sorted(team_map.items())))
    assert [(o.team, o.n_users) for o in result.outcomes] == [("alpha", 6)]
    assert [n.count for n in result.notes if n.code == "dq.outcomes_suppressed"] == [1]


def test_k_threshold_from_options(tmp_path: Path) -> None:
    recs = [_record("a@x.io"), _record("b@x.io"), _record("c@x.io")]
    path = write_json(tmp_path / "cc.json", _page(recs))
    tm = (("a@x.io", "t1"), ("b@x.io", "t2"), ("c@x.io", "t2"))
    assert sorted(_outcomes(read("anthropic-cc-analytics", path, team_map=tm,
                                 k_anonymity=1))) == [("2026-09-10", "t1", 1, 1),
                                                      ("2026-09-10", "t2", 2, 2)]
    assert _outcomes(read("anthropic-cc-analytics", path, team_map=tm,
                          k_anonymity=3)) == [("2026-09-10", OTHER_TEAM, 3, 3)]
    result = read("anthropic-cc-analytics", path, team_map=tm, k_anonymity=4)
    assert result.outcomes == [] and result.aggregates == []


def test_unmapped_actors_and_case_insensitive_lookup(tmp_path: Path) -> None:
    recs = [_record(f"U{i}@X.IO") for i in range(5)] + [_record(None, api_key="k1")]
    path = write_json(tmp_path / "cc.json", _page(recs))
    tm = tuple((f"u{i}@x.io", "lower") for i in range(5))
    result = read("anthropic-cc-analytics", path, team_map=tm)
    assert [(o.team, o.n_users) for o in result.outcomes] == [("lower", 5)]
    result = read("anthropic-cc-analytics", path, team_map=())
    assert [(o.team, o.n_users) for o in result.outcomes] == [(UNMAPPED_TEAM, 6)]


def test_same_user_twice_counts_once(tmp_path: Path) -> None:
    recs = [_record(f"u{i}@x.io") for i in range(4)] + [_record("u0@x.io", commits=5)]
    path = write_json(tmp_path / "cc.json", _page(recs))
    result = read("anthropic-cc-analytics", path, team_map=(), k_anonymity=4)
    (out,) = result.outcomes
    assert (out.n_users, out.commits) == (4, 9)


def test_estimated_cost_exact_and_models_split(tmp_path: Path) -> None:
    recs = [_record(f"u{i}@x.io", cost="12.345678901") for i in range(5)]
    recs += [_record(f"u{i}@x.io", model="claude-sonnet-5", cost=7) for i in range(5)]
    path = write_json(tmp_path / "cc.json", _page(recs))
    result = read("anthropic-cc-analytics", path, team_map=())
    by_model = {dims(a)["model"]: a for a in result.aggregates}
    assert by_model["claude-opus-5"].reported_cost_nano == 617_283_945   # 5 × $0.12345678901
    assert by_model["claude-sonnet-5"].reported_cost_nano == 350_000_000
    assert result.stats["aggregate_rounding_remainder_e18"] == 50_000_000  # 0.05 nano, exact
    usage = by_model["claude-opus-5"].usage
    assert (usage.uncached_input, usage.cache_read, usage.cache_write_unknown,
            usage.output) == (500, 1500, 200, 100)


def test_pages_split_across_files_in_a_directory(tmp_path: Path) -> None:
    recs = [_record(f"u{i}@x.io") for i in range(6)]
    d = tmp_path / "pull"
    write_jsonl(d / "page1.jsonl", [_page(recs[:3])])
    write_json(d / "page2.json", _page(recs[3:]))
    (d / "notes.md").write_text("ignored")
    alone = read("anthropic-cc-analytics", d / "page1.jsonl", team_map=())
    assert alone.outcomes == []                       # 3 users alone: dropped
    together = read("anthropic-cc-analytics", d, team_map=())
    assert [(o.team, o.n_users) for o in together.outcomes] == [(UNMAPPED_TEAM, 6)]
    assert together.stats["files"] == 2


def test_window_filter_and_bad_records(tmp_path: Path) -> None:
    recs = [_record(f"u{i}@x.io") for i in range(5)]
    recs += [_record(f"u{i}@x.io", day="2026-09-12") for i in range(5)]
    recs += [{"date": "2026-09-10T00:00:00Z", "actor": {"type": "user_actor"}},
             {"actor": {"type": "user_actor", "email_address": "z@x.io"}},
             {**_record("y@x.io"), "core_metrics": [1]},
             {**_record("y@x.io"), "model_breakdown": {"x": 1}},
             {**_record("y@x.io"), "tool_actions": "x"},
             {**_record("y@x.io"), "model_breakdown": [{"model": "m", "tokens": [1]}]},
             {**_record("y@x.io"), "model_breakdown": [3]},
             {**_record("y@x.io"), "model_breakdown": [{"model": "m",
                                                        "estimated_cost": "bad"}]},
             {**_record("y@x.io"), "core_metrics": {"lines_of_code": 5}},
             "junk"]
    path = write_json(tmp_path / "cc.json", _page(recs))
    since = 20_708 * DAY_MS  # 2026-09-12
    result = read("anthropic-cc-analytics", path, team_map=(), since_ms=since)
    assert [o.date_utc for o in result.outcomes] == ["2026-09-12"]
    assert result.stats["filtered_by_window"] == 5
    assert [q.reason for q in result.quarantined] == [
        "missing:actor", "missing:date", "bad_type:core_metrics", "bad_type:model_breakdown",
        "bad_type:tool_actions", "bad_type:tokens", "bad_type:model_breakdown",
        "bad_type:estimated_cost", "bad_type:lines_of_code", "not_object"]


def test_bare_records_and_other_currency(tmp_path: Path) -> None:
    rec = _record("a@x.io")
    rec["model_breakdown"][0]["estimated_cost"]["currency"] = "EUR"
    path = write_jsonl(tmp_path / "records.jsonl",
                       [_record(f"u{i}@x.io") for i in range(5)] + [rec])
    result = read("anthropic-cc-analytics", path, team_map=())
    assert [(o.team, o.n_users) for o in result.outcomes] == [(UNMAPPED_TEAM, 5)]
    assert [q.reason for q in result.quarantined] == ["bad_type:currency"]
    assert CANARY not in repr(result)


@pytest.mark.parametrize("k", [1, 2, 5, 7])
def test_published_groups_never_below_k(tmp_path: Path, k: int) -> None:
    result = read("anthropic-cc-analytics", fixture(DAY1), k_anonymity=k)
    assert all(o.n_users >= k for o in result.outcomes)
    total = sum(o.n_users for o in result.outcomes) + result.stats.get("users_dropped", 0)
    assert total == 17
