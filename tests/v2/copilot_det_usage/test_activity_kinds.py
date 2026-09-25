"""Activity kinds (mcp-sprawl, context-heavy-cli, editor-mix) and per-kind gating
(addendum §10.0, §10.2, §9.3)."""

from __future__ import annotations

import pytest

from tokenbill.core import builders as b
from tokenbill.core.errors import UsageError
from tokenbill.detect.copilot_org import KIND_REQUIRES, KINDS

from .worlds import ALL_CAPS, World, attrs, one, only, run


def _mcp_world() -> World:
    w = World().ide("platform", 4, vscode=5, mcp_distinct=7)
    w.ide("core", 6, vscode=5, mcp_distinct=1)
    # one platform user seen on two days: the larger daily distinct count counts
    w.activity.append(b.make_activity(b.make_principal("platform-0"), team="platform",
                                      date_utc="2026-09-11", counts={"mcp_distinct": 2}))
    return w


def test_mcp_sprawl_per_team_median_and_share() -> None:
    found = run(_mcp_world().ctx())
    f = one(found, "mcp-sprawl")
    assert dict(f.scope.dims) == {"product": "copilot", "entity": "enterprise",
                                  "team": "platform"}
    a = attrs(f, "mcp")
    assert (a["users"], a["median_distinct"], a["heavy_share"]) == (4, 7, "1")
    assert f.cost_observed.nano is None and "count-only" in f.cost_observed.note
    assert f.n_users == 4 and f.lever_ids == ("copilot.mcp_trim",)
    assert not only(found, "mcp-sprawl", team="core")


def test_mcp_sprawl_thresholds_override() -> None:
    ctx = _mcp_world().ctx(thresholds={"copilot.org-scan.mcp_heavy_distinct": "8",
                                       "copilot.org-scan.mcp_heavy_share": "0.9"})
    assert only(run(ctx), "mcp-sprawl") == []
    with pytest.raises(UsageError):
        run(_mcp_world().ctx(thresholds={"copilot.org-scan.mcp_heavy_share": "x"}))


def test_context_heavy_cli_p50_p90() -> None:
    w = World()
    for i, tokens in enumerate((40_000, 60_000, 90_000, 120_000, 300_000)):
        w.activity.append(b.make_activity(b.make_principal(f"cli-{i}"), team="agents",
                                          counts={"cli_requests": 10,
                                                  "cli_prompt_tokens": tokens * 10}))
    w.ide("light", 5, cli_requests=10, cli_prompt_tokens=1000)
    found = run(w.ctx())
    f = one(found, "context-heavy-cli")
    a = attrs(f, "cli")
    assert (a["prompt_tokens_per_request_p50"], a["prompt_tokens_per_request_p90"]) == (
        90_000, 300_000)
    assert "/compact" in f.summary and f.lever_ids == ("copilot.context_default",)
    assert not only(found, "context-heavy-cli", team="light")


def test_editor_mix_from_metrics_counts_and_seat_surfaces() -> None:
    w = World().ide("vs", 5, vscode=9, intellij=1).ide("jb", 5, vscode=1, intellij=9)
    w.config.append(b.make_config("activity_counts", {
        "team": "bundle", "month": "2026-09", "n_people": 7, "ide:vscode": 30,
        "ide:jetbrains": 70}, entity_id="org:org-a"))
    for i, surface in enumerate(("jetbrains", "jetbrains", "vscode", "vscode", "vscode")):
        w.licenses.append(b.make_license(b.make_principal(f"seat-{i}"), team="seats",
                                         last_activity_surface=surface))
    found = run(w.ctx())
    jb = one(found, "editor-mix", team="jb")
    assert attrs(jb, "editors")["jetbrains_share"] == "0.9"
    assert attrs(jb, "editors")["reach"] == "0.1" and "JetBrains carries 90%" in jb.fix.text
    vs = one(found, "editor-mix", team="vs")
    assert attrs(vs, "editors")["reach"] == "0.9" and vs.fix is not None
    assert "JetBrains carries" not in vs.fix.text                 # the catalog's fix text
    bundle = one(found, "editor-mix", team="bundle")
    assert attrs(bundle, "editors")["share_jetbrains"] == "0.7" and bundle.n_users == 7
    seats = one(found, "editor-mix", team="seats")
    assert attrs(seats, "editors")["reach"] == "unknown"
    assert attrs(seats, "editors")["basis"] == "seat last-activity surfaces"
    assert "reach unknown" in seats.summary and seats.n_users == 5
    assert seats.category == "aggregate" and seats.cost_observed.nano is None


def test_aggregate_only_bundle_skips_per_user_activity_kinds() -> None:
    w = World()
    w.config.append(b.make_config("activity_counts", {
        "team": "t", "month": "2026-09", "n_people": 9, "ide:vscode": 10}, entity_id="org:o"))
    found = run(w.ctx())
    dq = one(found, "dq.skipped-kinds")
    assert "mcp-sprawl: no per-user activity" in dq.summary
    assert "context-heavy-cli: no per-user activity" in dq.summary
    assert one(found, "editor-mix", team="t")                       # counts suffice


def test_capability_gates_name_every_skipped_kind_once() -> None:
    w = _mcp_world().row(team="platform", principal=b.make_principal(1), date_utc="2026-09-10",
                         model="Claude Opus 5.5", credits="100")
    found = run(w.ctx(caps=frozenset({"aggregates"})))
    dq = one(found, "dq.skipped-kinds")
    for kind in ("premium-model-share", "mcp-sprawl", "editor-mix", "larger-runner"):
        assert f"{kind}: needs" in dq.summary or kind in {i.ref[8:] for i in dq.evidence}
    assert {i.ref for i in dq.evidence} >= {"skipped:auto-adoption", "skipped:mcp-sprawl"}
    assert not [f for f in found if f.kind not in ("dq.skipped-kinds", "agentic-workflow-cost",
                                                   "agent-failed-sessions")]
    assert set(KIND_REQUIRES) == set(KINDS) - {"dq.skipped-kinds"}
    full = run(w.ctx(caps=ALL_CAPS))
    assert not any("mcp-sprawl" in f.summary for f in only(full, "dq.skipped-kinds"))
