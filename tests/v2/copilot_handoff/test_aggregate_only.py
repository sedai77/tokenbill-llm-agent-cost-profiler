"""Aggregate-only mode (brief Build 3): no ``p_`` anywhere, counts at k."""

from __future__ import annotations

import re
import zipfile
from collections import Counter
from pathlib import Path

import pytest

from tokenbill.copilot.handoff import (
    AGGREGATE_ONLY_KINDS_OFF,
    OTHER_TEAM,
    read_bundle,
    write_bundle,
)
from tokenbill.core import pool
from tokenbill.core.builders import make_activity, make_ai_usage_row, make_license
from tokenbill.core.ids import pseudonym

from .helpers import KEY, _result, export, round_trip_results, seed, use_fake_registry, write_world

P_RE = re.compile(rb"p_[0-9a-f]{20}")


def attrs_of(res, kind: str) -> list[dict]:
    return [dict(c.attrs) | {"_entity": c.entity_id} for c in res.config if c.kind == kind]


def test_world_aggregate_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    use_fake_registry(monkeypatch)
    world = write_world(tmp_path / "in")
    report = export(world, tmp_path / "agg.tbx", aggregate_only=True)
    manifest = report.manifest
    assert manifest.privacy_mode == "aggregate_only"
    with zipfile.ZipFile(tmp_path / "agg.tbx") as zf:
        for name in zf.namelist():
            assert not P_RE.search(zf.read(name)), name
    _, res = read_bundle(tmp_path / "agg.tbx")
    assert res.licenses == [] and res.activity == []
    assert all(c.principal is None for c in res.cost_lines)
    codes = dict(manifest.dq)
    assert codes["dq.copilot_export_aggregate_only"] == 1
    assert all(codes[f"dq.copilot_export_kind_off.{k}"] == 1 for k in AGGREGATE_ONLY_KINDS_OFF)
    for row in attrs_of(res, "seat_counts") + attrs_of(res, "activity_counts"):
        assert row.get("n", 5) >= 5 and row.get("n_people", 5) >= 5
    # the gamma team (3 people) was merged at the team step, then suppressed as a residual
    assert codes["dq.copilot_export_suppressed"] > 0
    # money is preserved when principals are summed away
    assert sum(c.amount_nano for c in res.cost_lines) == sum(
        c.amount_nano for c in read_bundle(export(world, tmp_path / "p.tbx").out_path)[1]
        .cost_lines)
    # the analyst's plan detection reads the seat counts
    (pe,) = pool.detect_plans(res.cost_lines, [], res.config, month="2026-09")
    assert pe.plan == "business" and pe.source == "seats_api"


def seat_world() -> list:
    """Seats in two teams (8 and 6 people) across buckets, plus 3 unattributed seats."""
    lics, acts, lines = [], [], []
    buckets = ("0-7", "8-30", "31-90", "none_90d")
    people = [(f"a{i}", "alpha") for i in range(8)] + [(f"b{i}", "beta") for i in range(6)]
    people += [(f"u{i}", None) for i in range(3)]
    for i, (login, team) in enumerate(people):
        p = pseudonym(KEY, "p", login)
        lics.append(make_license(p, snapshot_date="2026-09-20", team=team, org="acme-org",
                                 plan="enterprise" if i % 5 == 0 else "business",
                                 last_activity_bucket=buckets[i % 4],
                                 seat_created="2026-01-01" if i % 2 else "2026-09-15",
                                 pending_cancellation="2026-10-01" if i == 3 else None,
                                 assigned_via_team=(i % 3 == 0)))
        acts.append(make_activity(p, date_utc="2026-09-12", team=team,
                                  counts={"interactions": 2, "cli_requests": 1,
                                          "cli_prompt_tokens": 40, "app_prompts": 3,
                                          "ide:vscode": 1, "ide:intellij": 1}))
        if i % 2:
            lines.append(make_ai_usage_row(principal=p, team=team, credits="4",
                                           date_utc="2026-09-10")[0])
    src = round_trip_results()[0].source
    return [_result(src, licenses=lics, activity=acts, cost_lines=lines)]


def test_seat_counts_sum_to_the_input_seats_minus_the_residual(tmp_path: Path) -> None:
    out = tmp_path / "s.tbx"
    manifest = write_bundle(seat_world(), out, manifest_seed=seed(), leak_terms=frozenset(),
                            k=5, aggregate_only=True)
    _, res = read_bundle(out)
    detail = [a for a in attrs_of(res, "seat_counts") if a["bucket"] != "*"]
    summary = [a for a in attrs_of(res, "seat_counts") if a["bucket"] == "*"]
    assert all(a["n"] >= 5 for a in detail) and all(a["n_people"] >= 5 for a in summary)
    per_key = Counter()
    for a in detail:
        per_key[(a["_entity"], a["team"], a["bucket"])] += a["n"]
    dropped = dict(manifest.dq).get("dq.copilot_export_suppressed", 0)
    summary_total = sum(a["n_people"] for a in summary)
    activity_residual = 3           # the 3 unattributed people are below k in activity too
    assert (17 - sum(per_key.values())) + (17 - summary_total) + activity_residual == dropped
    assert {a["team"] for a in summary} == {"alpha", "beta"}   # 3 unattributed → dropped
    keys = {"team", "plan", "bucket", "surface", "assigned_via_team", "pending_cancellation",
            "created_over_30d", "zero_cost_30d", "n", "_entity"}
    assert all(set(a) == keys for a in detail)
    assert all(isinstance(a[k], bool) for a in detail
               for k in ("pending_cancellation", "created_over_30d", "zero_cost_30d"))


def test_seat_criteria_are_pre_evaluated(tmp_path: Path) -> None:
    lics = []
    # pairs of identical seats: (created over 30 days, zero cost, pending cancellation)
    for n, (old, _zero, pending) in enumerate([(True, True, False), (True, False, False),
                                              (False, True, False), (False, False, False),
                                              (True, True, True)]):
        for j in range(2):
            p = pseudonym(KEY, "p", f"s{n}-{j}")
            lics.append(make_license(p, snapshot_date="2026-09-20", team="alpha",
                                     last_activity_bucket="none_90d",
                                     seat_created="2026-01-01" if old else "2026-09-18",
                                     pending_cancellation="2026-10-01" if pending else None))
    lines = [make_ai_usage_row(principal=lic.principal, credits="1", date_utc="2026-09-01")[0]
             for i, lic in enumerate(lics) if i // 2 in (1, 3)]
    lines.append(make_ai_usage_row(principal=pseudonym(KEY, "p", "old"), credits="1",
                                   date_utc="2026-08-01")[0])    # outside the 30-day window
    src = round_trip_results()[0].source
    out = tmp_path / "c.tbx"
    write_bundle([_result(src, licenses=lics, cost_lines=lines)], out, manifest_seed=seed(),
                 leak_terms=frozenset(), k=2, aggregate_only=True)
    detail = [a for a in attrs_of(read_bundle(out)[1], "seat_counts") if a["bucket"] != "*"]
    assert sum(a["n"] for a in detail) == 10 and all(a["team"] == "alpha" for a in detail)
    zero = sum(a["n"] for a in detail if a["zero_cost_30d"])
    old_seats = sum(a["n"] for a in detail if a["created_over_30d"])
    pending = sum(a["n"] for a in detail if a["pending_cancellation"])
    assert (zero, old_seats, pending) == (6, 6, 2)


def test_zero_cost_is_false_without_any_usage_rows(tmp_path: Path) -> None:
    lics = [make_license(pseudonym(KEY, "p", f"n{i}"), snapshot_date="2026-09-20",
                         team="alpha") for i in range(5)]
    src = round_trip_results()[0].source
    out = tmp_path / "z.tbx"
    write_bundle([_result(src, licenses=lics)], out, manifest_seed=seed(),
                 leak_terms=frozenset(), k=5, aggregate_only=True)
    detail = [a for a in attrs_of(read_bundle(out)[1], "seat_counts") if a["bucket"] != "*"]
    assert detail and not any(a["zero_cost_30d"] for a in detail)


def test_activity_counts_per_team_and_month(tmp_path: Path) -> None:
    out = tmp_path / "a.tbx"
    manifest = write_bundle(seat_world(), out, manifest_seed=seed(), leak_terms=frozenset(),
                            k=5, aggregate_only=True)
    _, res = read_bundle(out)
    rows = {a["team"]: a for a in attrs_of(res, "activity_counts")}
    assert set(rows) == {"alpha", "beta"}
    alpha = rows["alpha"]
    assert alpha["month"] == "2026-09" and alpha["n_people"] == 8
    assert (alpha["interactions"], alpha["cli_requests"], alpha["cli_prompt_tokens"],
            alpha["app_interactions"]) == (16, 8, 320, 24)
    assert alpha["ide:vscode"] == 8 and alpha["ide:jetbrains"] == 8
    assert manifest.count("activity") == 0


def test_small_groups_merge_into_other(tmp_path: Path) -> None:
    acts = [make_activity(pseudonym(KEY, "p", f"x{i}"), team=f"team-{i % 3}",
                          date_utc="2026-09-02") for i in range(7)]
    src = round_trip_results()[0].source
    out = tmp_path / "o.tbx"
    write_bundle([_result(src, activity=acts)], out, manifest_seed=seed(),
                 leak_terms=frozenset(), k=5, aggregate_only=True)
    (row,) = attrs_of(read_bundle(out)[1], "activity_counts")
    assert row["team"] == OTHER_TEAM and row["n_people"] == 7 and row["interactions"] == 7


def test_cost_lines_are_summed_without_principals(tmp_path: Path) -> None:
    lines = [make_ai_usage_row(principal=pseudonym(KEY, "p", f"z{i}"), credits=f"{i}.25",
                               discount_credits="0.25", date_utc="2026-09-03")[0]
             for i in range(1, 4)]
    src = round_trip_results()[0].source
    out = tmp_path / "c.tbx"
    write_bundle([_result(src, cost_lines=lines)], out, manifest_seed=seed(),
                 leak_terms=frozenset(), k=5, aggregate_only=True)
    (line,) = read_bundle(out)[1].cost_lines
    assert line.principal is None and line.quantity == "6.75"
    assert line.amount_nano == sum(x.amount_nano for x in lines)
    assert line.list_amount_nano == sum(x.list_amount_nano for x in lines)
    assert line.line_id not in {x.line_id for x in lines}
