"""The default world: team table, record shapes, internal consistency (brief CP-SYNTH acceptance:
gross − discount = net, aggregates = row sums, coverage = file-day sums, conventions), privacy
(no login, no canary) and R-E43 billing paths."""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter, defaultdict

import pytest

from tokenbill.core.builders import CANARY, CANARY_LOGIN
from tokenbill.core.errors import UsageError
from tokenbill.core.records import (
    COPILOT_BILLING_PATHS,
    CostLine,
    LaneEventKind,
    UsageBuckets,
    WorkloadClass,
    to_json,
)
from tokenbill.synth import copilot_world as cw

from .worlds import world

REPORT = "github.ai_usage_report"


def _report_lines(w: cw.CopilotWorld) -> list[CostLine]:
    return [c for c in w.records.cost_lines if c.source_kind == REPORT]


def test_team_table_146_users() -> None:
    w = world()
    sizes = dict(w.truth.team_sizes)
    assert sum(sizes.values()) == 146 == len(w.people) == len(w.team_map)
    assert sizes == {"platform": 20, "infra": 10, "ops": 8, "payments": 20, "mobile": 15,
                     "data": 15, "agents": 4, "vscode": 15, "jetbrains": 15, "core": 20,
                     "tiny": 4}
    assert w.team_map[CANARY_LOGIN] == "core"
    assert w.cost_center_map == {u.login: "cc-data" for u in w.people if u.team == "data"}
    assert w.today == "2026-09-23" and (w.start, w.end) == ("2026-07-03", "2026-09-22")
    assert w.truth.control_team == "core" and w.truth.tiny_team == "tiny"
    assert set(w.reports) == {"excl", "incl", "undecidable"}


def test_team_sizes_scale_and_validate() -> None:
    assert cw.team_sizes() == {t.name: t.size for t in cw.TEAMS}
    for n in (11, 500, 5_000):
        sizes = cw.team_sizes(n)
        assert sum(sizes.values()) == n and min(sizes.values()) >= 1
    with pytest.raises(UsageError):
        cw.team_sizes(3)


def test_every_report_row_identity_and_quantity() -> None:
    for c in _report_lines(world()):
        assert c.list_amount_nano is not None
        assert 0 <= c.amount_nano <= c.list_amount_nano
        assert c.list_amount_nano % 100 == 0          # every closed form is rounding-free
        assert c.quantity is not None
        assert credits_nano(c.quantity) == c.list_amount_nano
        assert c.cost_type in ("ai_credit.user", "ai_credit.direct")
        assert (c.principal is None) == (c.cost_type == "ai_credit.direct")


def credits_nano(quantity: str) -> int:
    """Nano of a credits decimal string (1 credit = 10**7 nano), exact."""
    whole, _, frac = quantity.partition(".")
    return int(whole) * 10**7 + int((frac + "0000000")[:7])


def test_aggregates_equal_row_sums_under_each_convention() -> None:
    w = world()
    lines = _report_lines(w)
    for conv, rep in w.reports.items():
        assert rep.cost_lines == tuple(lines)
        sums: dict[tuple, list] = defaultdict(lambda: [UsageBuckets(), 0, 0])
        for c in lines:
            dims = {"channel": c.channel, "organization": c.workspace_id, "team": c.team,
                    "cost_center": c.cost_center, "model": c.model, "sku": c.sku,
                    "routing": c.routing, "speed": c.speed, "pseudo": c.pseudo}
            key = (c.date_utc, tuple(sorted((k, v) for k, v in dims.items() if v is not None)))
            sums[key][1] += c.amount_nano
            sums[key][2] += c.list_amount_nano or 0
        tokens = [a for a in rep.aggregates if a.source_kind == REPORT]
        assert len(tokens) == len(sums)
        for a in tokens:
            key = (_day(a.bucket_start_ms), a.dims)
            assert (a.reported_cost_nano, a.list_cost_nano) == (sums[key][1], sums[key][2])
            if conv == "undecidable":
                assert a.usage.cache_read == a.usage.cache_write == 0


def _day(ms: int) -> str:
    return (dt.date(1970, 1, 1) + dt.timedelta(milliseconds=ms)).isoformat()


def test_coverage_aggregates_equal_file_day_sums() -> None:
    w = world()
    per_day: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for c in _report_lines(w):
        per_day[c.date_utc][0] += c.amount_nano
        per_day[c.date_utc][1] += c.list_amount_nano or 0
    for rep in w.reports.values():
        cov = [a for a in rep.aggregates if a.source_kind == "github.ai_usage_report.coverage"]
        assert {_day(a.bucket_start_ms) for a in cov} == set(per_day)
        for a in cov:
            assert dict(a.dims) == {"channel": "github_copilot", "source": rep.source_id}
            assert [a.reported_cost_nano, a.list_cost_nano] == per_day[_day(a.bucket_start_ms)]
    assert len({r.source_id for r in w.reports.values()}) == 3


def test_excl_and_incl_differ_only_in_the_input_column() -> None:
    w = world()
    excl = {a.agg_id: a for a in w.reports["excl"].aggregates if a.source_kind == REPORT}
    incl = {a.agg_id: a for a in w.reports["incl"].aggregates if a.source_kind == REPORT}
    assert set(excl) == set(incl)
    cached = 0
    for agg_id, a in excl.items():
        b = incl[agg_id]
        assert (a.reported_cost_nano, a.list_cost_nano, a.dims) == (
            b.reported_cost_nano, b.list_cost_nano, b.dims)
        assert b.usage.uncached_input == a.usage.total_input
        assert (b.usage.cache_read, b.usage.cache_write_unknown, b.usage.output) == (
            a.usage.cache_read, a.usage.cache_write_unknown, a.usage.output)
        cached += a.usage.cache_read + a.usage.cache_write
    assert cached > 0
    und = [a for a in w.reports["undecidable"].aggregates if a.source_kind == REPORT]
    assert sum(a.usage.cache_read + a.usage.cache_write for a in und) == 0
    assert w.truth.report_convention == {"excl": "excl", "incl": "incl",
                                         "undecidable": "undecidable"}


def test_records_carry_no_login_or_canary() -> None:
    w = world()
    blob = "\n".join(w.records.json_lines())
    assert CANARY not in blob and CANARY_LOGIN not in blob
    for login in w.team_map:
        assert f'"{login}"' not in blob
    for rep in w.reports.values():
        text = json.dumps([to_json(a) for a in rep.aggregates])
        assert CANARY_LOGIN not in text
    assert dict(w.records.logins)[w.people[0].principal] == w.people[0].login


def test_every_copilot_inference_has_a_copilot_billing_path() -> None:
    r = world().records
    assert r.requests and len(r.lanes) == len(r.sessions)
    for req in r.requests:
        assert req.attribution.billing_path in COPILOT_BILLING_PATHS
        for inf in req.billable_inferences:
            assert inf.pricing.billing_path in COPILOT_BILLING_PATHS
            assert inf.pricing.channel == "github_copilot" and inf.pricing.provider == "github"
    direct = {s.attribution.agent_product for s in r.sessions
              if s.attribution.billing_path == "copilot_direct"}
    assert direct == {"copilot_gh_aw"}
    assert any(e.kind is LaneEventKind.COST_STATE for e in r.events)


def test_seat_actions_and_sandbox_lines() -> None:
    w = world()
    lines = [c for c in w.records.cost_lines if c.source_kind == "github.metered_usage"]
    seats = Counter()
    for c in lines:
        if c.cost_type == "seat":
            seats[(c.date_utc[:7], c.sku, c.workspace_id, c.cost_center)] += int(c.quantity or 0)
    assert seats[("2026-09", "copilot_enterprise", "org-a", None)] == 20
    assert seats[("2026-09", "copilot_for_business", "org-b", None)] == 8
    assert seats[("2026-09", "copilot_for_business", "org-a", "cc-data")] == 15
    assert sum(v for k, v in seats.items() if k[0] == "2026-07") == 146
    kinds = Counter((c.channel, c.cost_type, c.sku, c.workload) for c in lines
                    if c.cost_type != "seat")
    assert kinds[("github_actions", "actions", "linux_16_core", "copilot_code_review")] > 40
    assert kinds[("github_actions", "actions", "actions_linux", "agentic_workflow")] == \
        len(w.truth.agentic_run_prices) == 20
    assert kinds[("github_sandbox", "sandbox", "sandbox_linux", None)] == 3
    aw = [c for c in lines if c.workload == "agentic_workflow"]
    assert all(c.workflow and c.workflow.startswith("h_") and c.repo for c in aw)


def test_licenses_activity_and_config() -> None:
    w = world()
    r = w.records
    assert len(r.licenses) == 146 * 3
    idle = Counter(x.team for x in r.licenses if x.last_activity_bucket == "none_90d"
                   and x.snapshot_date == "2026-09-22")
    assert idle == {"platform": 6, "infra": 5, "ops": 5}
    assert all(x.assigned_via_team == (x.team == "infra") for x in r.licenses)
    ide = Counter()
    for a in r.activity:
        for k, v in a.counts:
            if k.startswith("ide:"):
                ide[(a.team, k)] += v
    assert ide[("jetbrains", "ide:intellij")] == 9 * ide[("jetbrains", "ide:vscode")]
    assert 3 * ide[("core", "ide:vscode")] == 7 * ide[("core", "ide:intellij")]
    assert not any(k.startswith("ide:") for a in r.activity if a.team == "agents"
                   for k, _ in a.counts)
    kinds = Counter(c.kind for c in r.config)
    assert kinds == {"run_flags": 1, "org_settings": 2, "cost_center": 1, "budget": 6}
    zero = [c for c in r.config if c.kind == "budget" and dict(c.attrs)["amount_nano"] == 0]
    assert [dict(c.attrs)["team"] for c in zero] == ["platform"] * 5
    assert r.outcomes and any(o.team == "(enterprise)" and o.extra for o in r.outcomes)


def test_lanes_plants() -> None:
    r = world().records
    by_source = Counter(s.source_kind for s in r.sessions)
    assert by_source == {"copilot.vscode_traces": 20, "copilot.otel": 16,
                         "copilot.cli_events": 13, "gh_aw.token_usage": 20}
    keys = Counter(s.session_key for s in r.sessions)
    assert sum(1 for n in keys.values() if n == 2) == 3 + 6    # vscode+CLI, CLI+OTel
    ci = [s for s in r.sessions if s.attribution.workload_class is WorkloadClass.CI
          and s.attribution.agent_product == "copilot_cli"]
    assert len(ci) == 2
    for s in ci:
        meta = [dict(e.attrs) for e in s.lanes[0].events if e.kind is LaneEventKind.SESSION_META]
        assert meta and meta[0]["credit_limit_nano"] is None
    triggers = Counter(dict(e.attrs)["copilot_trigger"] for e in r.events
                       if e.kind is LaneEventKind.COMPACTION)
    assert triggers["context_limit_retry"] >= 2 and triggers["memory_pressure"] >= 1
    assert all(dict(e.attrs)["tool_definitions_tokens"] == 30_000 for e in r.events
               if e.kind is LaneEventKind.COMPACTION)


def test_generate_rejects_bad_arguments() -> None:
    with pytest.raises(UsageError):
        cw.generate(variants=("nope",))
    with pytest.raises(UsageError):
        cw.generate(variants=("plan_unknown", "plan_conflict"))
    with pytest.raises(UsageError):
        cw.generate(conventions=())
    with pytest.raises(UsageError):
        cw.generate(conventions=("excl", "other"))
    with pytest.raises(UsageError):
        cw.generate(start="2026-09-22", end="2026-09-01")
    with pytest.raises(UsageError):
        cw.generate(start="not-a-date")
    with pytest.raises(UsageError):
        cw.generate(seed="7")  # type: ignore[arg-type]
    with pytest.raises(UsageError, match="no rate row"):
        cw.generate(start="2026-06-29", end="2026-07-02", conventions=("excl",))
    with pytest.raises(UsageError, match="K-dated"):
        cw.generate(start="2026-07-01", end="2026-07-02", conventions=("excl",))
