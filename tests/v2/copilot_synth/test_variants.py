"""Variants (brief Provides): the no-token handoff view ``plan_unknown`` (no seat SKU line, no
seats-API license, no org settings; every license plan unknown with unknown assignment),
``plan_quota``, ``plan_conflict``, ``volume`` and ``slack`` — and the GitHub-side money that every
view but ``slack`` shares."""

from __future__ import annotations

from collections import Counter

from tokenbill.core import pool as cpool

from .worlds import world


def _report_money(w) -> list[tuple[str, int, int]]:
    return [(c.line_id, c.amount_nano, c.list_amount_nano or 0) for c in w.records.cost_lines
            if c.source_kind == "github.ai_usage_report"]


def test_plan_unknown_is_the_handoff_view() -> None:
    w = world("plan_unknown")
    r = w.records
    assert not [c for c in r.cost_lines if c.cost_type == "seat"]
    assert r.licenses and all(x.source_kind == "github.copilot_activity_report"
                              and x.plan == "unknown" and x.assigned_via_team is None
                              and x.org is None for x in r.licenses)
    assert not [c for c in r.config if c.kind == "org_settings"]
    assert Counter(c.kind for c in r.config) == {"run_flags": 1}
    assert r.config[0].source_kind == "tokenbill.admin_answers"
    assert "plan." not in "".join(k for k, _ in r.config[0].attrs)
    assert _report_money(w) == _report_money(world())
    assert w.variants == ("plan_unknown",)


def test_plan_quota_rows_and_implied_plan_unknown() -> None:
    w = world("plan_quota")
    assert w.variants == ("plan_quota", "plan_unknown")
    quota = [c for c in w.records.config if c.kind == "plan_quota"]
    assert quota and all(c.source_kind == "github.ai_usage_report" for c in quota)
    sep: Counter = Counter()
    for c in quota:
        attrs = dict(c.attrs)
        if attrs["month"] == "2026-09":
            sep[(c.entity_id, attrs["quota"])] += attrs["n_users"]
    assert sep == {("org:org-a", "1900"): 113, ("org:org-b", "1900"): 3,
                   ("org:org-a", "3900"): 14}
    jul = {dict(c.attrs)["quota"] for c in quota if dict(c.attrs)["month"] == "2026-07"}
    assert jul == {"3000", "7000"}
    [pe] = cpool.detect_plans(w.records.cost_lines, w.records.licenses, w.records.config,
                              month="2026-09")
    assert (pe.plan, pe.source) == ("unknown", "report_quota")


def test_plan_conflict_seat_lines_win() -> None:
    w = world("plan_conflict")
    api = {x.plan for x in w.records.licenses if x.org == "org-b"}
    assert api == {"enterprise"}
    lines = {c.sku for c in w.records.cost_lines if c.cost_type == "seat"
             and c.workspace_id == "org-b"}
    assert lines == {"copilot_for_business"}
    assert ("enterprise", "2026-09", "mixed", "seat_lines", True) in w.truth.plans
    stated = [c for c in w.records.config if c.source_kind == "tokenbill.admin_answers"]
    assert dict(stated[0].attrs) == {"plan.org:org-b": "enterprise"}
    assert _report_money(w) == _report_money(world())


def test_volume_and_slack() -> None:
    vol = world("volume")
    flags = cpool.run_flags(vol.records.config)
    assert flags["billing_mode.enterprise"] == "volume"
    assert flags["billing_mode.cc:cc-data"] == "volume"
    assert _report_money(vol) == _report_money(world())
    slack = world("slack")
    sep = [c for c in slack.records.cost_lines if c.date_utc == "2026-09-10"
           and c.team == "core"]
    base = [c for c in world().records.cost_lines if c.date_utc == "2026-09-10"
            and c.team == "core"]
    assert sum(c.list_amount_nano or 0 for c in sep) < sum(c.list_amount_nano or 0 for c in base)
    data = [c.list_amount_nano for c in slack.records.cost_lines if c.team == "data"]
    assert data == [c.list_amount_nano for c in world().records.cost_lines if c.team == "data"]
