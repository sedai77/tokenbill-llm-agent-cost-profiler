"""Entity-level kinds: forced-migration, compliance-uplift, code review, direct-org usage,
agentic workflows, larger runners, cloud agent, failed agent sessions, unattributed spend
(addendum §10.2, R12, R13, R16, DC21)."""

from __future__ import annotations

import dataclasses

import pytest

from tokenbill.core import builders as b
from tokenbill.core.labels import Basis, Evidence
from tokenbill.core.records import OutcomeAggregate, UsageBuckets

from .worlds import CREDIT, REPO, USD, WORKFLOW, World, attrs, one, only, pool_month, run, tri

GPT54 = dict(date_utc="2026-09-10", model="GPT-5.4", credits="400", input_tokens=1_000_000,
             cache_read_tokens=500_000, output_tokens=100_000)


def _migration(today: str) -> list:
    w = World().rows(3, team="data", **GPT54)
    return only(run(w.ctx(today=today)), "forced-migration")


def test_forced_migration_gpt54_sol_vs_terra_hand_arithmetic() -> None:
    (f,) = _migration("2026-09-24")
    # per row at 2026-09-24 rates: GPT-5.6 Sol 1M×$4 + 0.5M×$0.40 + 0.1M×$20 = $6.20;
    # GPT-5.6 Terra 1M×$2 + 0.5M×$0.20 + 0.1M×$12 = $3.30 → Δ $2.90 × 3 rows
    assert f.cost_observed.nano == 8_700_000_000
    assert (f.cost_observed.evidence, f.cost_observed.basis) == (Evidence.EXACT,
                                                                 Basis.LIST_EQUIVALENT)
    a = attrs(f, "model:gpt-5.4")
    assert (a["retire_on"], a["successor"], a["alternative"]) == ("2026-10-19", "gpt-5.6-sol",
                                                                  "gpt-5.6-terra")
    # vs GPT-5.4 today (1M×$2.50 + 0.5M×$0.25 + 0.1M×$15 = $4.125): +$2.075 per row
    assert a["delta_vs_current_nano"] == 6_225_000_000
    assert f.recoverable is None and f.category == "premium"
    assert dict(f.scope.dims) == {"product": "copilot", "entity": "enterprise",
                                  "model": "gpt-5.4"}
    assert f.projected_monthly.nano == 30 * 8_700_000_000       # one observed day
    assert f.projected_monthly.evidence is Evidence.ESTIMATED


def test_forced_migration_expires_after_the_retirement_date() -> None:
    assert len(_migration("2026-10-19")) == 1
    assert _migration("2026-10-20") == []


def test_forced_migration_without_cheaper_option_and_already_retired() -> None:
    # Grok 4.5 → Grok 4.6 (no remap target): Δ vs the current model at today's rates (0 here)
    w = World().rows(2, team="x", date_utc="2026-09-10", model="Grok 4.5", credits="10",
                     input_tokens=100_000, output_tokens=10_000)
    w.rows(2, team="x", prefix="old", date_utc="2026-08-20", model="Claude Opus 4.6",
           credits="10", input_tokens=1_000, output_tokens=100)
    assert only(run(w.ctx(min_usd="0.0")), "forced-migration") == []


def test_compliance_uplift_is_observed_over_eleven() -> None:
    w = World().row(team="t", principal=b.make_principal(1), date_utc="2026-09-10",
                    model="Claude Sonnet 5", credits="1100")
    w.config.append(b.make_config("run_flags", {"compliance": "data_residency"}))
    f = one(run(w.ctx()), "compliance-uplift")
    assert f.cost_observed.nano == 100 * CREDIT == 1 * USD
    assert f.cost_observed.evidence is Evidence.ESTIMATED and f.recoverable is None
    assert "Not a recommendation to disable" in f.summary
    w.config[0] = b.make_config("run_flags", {"compliance": "none"})
    assert only(run(w.ctx()), "compliance-uplift") == []


def _review_world(date: str = "2026-09-12") -> World:
    w = World().row(unattributed=True, date_utc=date, model="Code Review", credits="300")
    w.actions("1000", sku="linux_16_core", usd_per_minute="0.042", date_utc=date,
              workload="copilot_code_review")
    return w


def test_review_cost_credits_and_actions_dollars_r16() -> None:
    f = one(run(_review_world().ctx()), "review-cost")
    assert f.cost_observed.nano == 300 * CREDIT
    assert f.cost_observed.basis is Basis.LIST_EQUIVALENT
    a = attrs(f, "review:actions")
    assert (a["actions_net_nano"], a["actions_basis"], a["actions_minutes"]) == (
        42 * USD, "list", "1000")
    assert "(list, not yet invoice)" in f.summary and "never added" in f.summary
    # final rows of a closed month on a reconciled channel → invoice
    ctx = _review_world().ctx(today="2026-10-10", reconciled={"github_actions"})
    f = one(run(ctx), "review-cost")
    assert attrs(f, "review:actions")["actions_basis"] == "invoice" and "(invoice)" in f.summary
    only_actions = World().actions("100", sku="linux_16_core", usd_per_minute="0.042",
                                   workload="copilot_code_review")
    f = one(run(only_actions.ctx()), "review-cost")
    assert (f.cost_observed.nano, f.cost_observed.basis) == (4_200_000_000, Basis.LIST)


@pytest.mark.parametrize(("today", "text"), [("2026-09-24", "becomes Balanced on"),
                                             ("2026-10-27", "has been Balanced since")])
def test_review_default_balanced_before_expiry(today: str, text: str) -> None:
    w = _review_world("2026-09-12" if today < "2026-10" else "2026-10-05")
    f = one(run(w.ctx(today=today)), "review-default-balanced")
    assert f.recoverable is None and f.projected_monthly is None and f.headroom is None
    assert text in f.title and "$0.05-$1 Lite" in f.summary and "$0.25-$5 Balanced" in f.summary
    assert "personal defaults" in f.summary and "never projected" in f.summary
    assert attrs(f, "review:last-days")["checked"] == "2026-09-23"
    assert f.lever_ids == ("copilot.review_effort_lite",)


def test_review_default_balanced_not_after_expiry() -> None:
    w = _review_world("2026-10-20")
    assert only(run(w.ctx(today="2026-10-28")), "review-default-balanced") == []
    assert only(run(w.ctx(today="2026-10-28")), "review-cost")


def test_review_drivers_lists_the_drivers() -> None:
    f = one(run(_review_world().ctx()), "review-drivers")
    assert "MCP tools" in f.summary and "custom instructions" in f.summary
    assert "new pushes and drafts" in f.summary
    assert set(f.lever_ids) == {"copilot.review_triggers", "copilot.review_mcp_off",
                                "copilot.review_instructions_trim"}


def _direct_world() -> World:
    w = World().row(unattributed=True, date_utc="2026-09-12", model="Code Review", credits="300")
    w.row(unattributed=True, date_utc="2026-09-12", model="Claude Sonnet 5", credits="200",
          discount_credits="50", repo=REPO)
    return w


def test_direct_org_usage_net_and_pool_draw() -> None:
    f = one(run(_direct_world().ctx()), "direct-org-usage")
    assert f.cost_observed.nano == 450 * CREDIT               # Σ net = $4.50
    assert (f.cost_observed.basis, f.cost_observed.evidence) == (Basis.LIST, Evidence.EXACT)
    assert "unreconciled" in f.cost_observed.note
    a = attrs(f, "direct:pool")
    assert (a["gross_nano"], a["pool_draw_nano"], a["direct_draws_pool"]) == (
        500 * CREDIT, 50 * CREDIT, "yes")
    assert attrs(f, "direct:code_review")["net_nano"] == 300 * CREDIT
    assert f.n_users == 0 and f.category == "aggregate"
    f = one(run(_direct_world().ctx(today="2026-10-10", reconciled={"github_copilot"})),
            "direct-org-usage")
    assert (f.cost_observed.basis, f.cost_observed.evidence) == (Basis.INVOICE, Evidence.EXACT)


def test_direct_org_usage_mixed_labels_take_the_weakest() -> None:
    w = _direct_world()
    w.row(unattributed=True, date_utc="2026-09-12", model="Claude Sonnet 5", credits="100",
          finality="provisional", organization="org-b")
    f = one(run(w.ctx(today="2026-10-10", reconciled={"github_copilot"})), "direct-org-usage")
    assert f.cost_observed.basis is Basis.LIST and f.cost_observed.nano == 550 * CREDIT
    assert "non-invoice" in f.cost_observed.note


def _aw_world(*, model: bool = True) -> World:
    w = _direct_world()
    w.row(unattributed=True, date_utc="2026-09-12", model="Claude Sonnet 5", credits="70",
          repo="h_" + "c" * 20)                             # another repository: not joined
    w.actions("500", sku="actions_linux", workload="agentic_workflow", repo=REPO,
              workflow=WORKFLOW)
    for out in (1_000, 2_000, 3_000):
        dims = {"channel": "github_copilot", "repo": REPO, "workflow": WORKFLOW,
                "source": f"run-{out}"}
        if model:
            dims["model"] = "claude-sonnet-5"
        w.agg("gh_aw.run", "2026-09-12", dims, usage=UsageBuckets(uncached_input=100_000,
                                                                  output=out))
    return w


def test_agentic_workflow_cost_two_figures_and_exposure() -> None:
    f = one(run(_aw_world().ctx()), "agentic-workflow-cost")
    assert f.cost_observed.nano == 3 * USD and f.cost_observed.basis is Basis.LIST
    assert attrs(f, "aw:credits")["ai_credits_nano"] == 200 * CREDIT       # same repo only
    runs = attrs(f, "aw:runs")
    assert runs["runs"] == 3 and runs["exposure_nano"] == 3 * 1_000 * CREDIT  # $30 upper bound
    # per run: 100k × $2 + out × $10 per 1M → $0.21, $0.22, $0.23
    assert (runs["run_p50_nano"], runs["run_p90_nano"]) == (220_000_000, 230_000_000)
    assert runs["suggested_max_ai_credits"] == 35                        # ceil(0.23 × 1.5 / 0.01)
    assert "exposure" in f.summary and f.lever_ids == ("copilot.agentic_workflow_caps",)


def test_agentic_workflow_runs_without_model_or_actions() -> None:
    f = one(run(_aw_world(model=False).ctx()), "agentic-workflow-cost")
    runs = attrs(f, "aw:runs")
    assert "run_p50_nano" not in runs and runs["run_tokens_p50"] == 102_000
    w = World().agg("gh_aw.run", "2026-09-12", {"channel": "github_copilot", "repo": REPO},
                    usage=UsageBuckets(uncached_input=10))
    f = one(run(w.ctx()), "agentic-workflow-cost")
    assert f.cost_observed.nano is None and attrs(f, "aw:runs")["runs"] == 1


def test_larger_runner_range() -> None:
    w = World().actions("1000", sku="linux_16_core", usd_per_minute="0.042",
                        workload="copilot_cloud_agent")
    w.actions("1000", sku="actions_linux", workload="copilot_code_review")
    f = one(run(w.ctx()), "larger-runner")
    assert f.cost_observed.nano == 42 * USD and f.cost_observed.basis is Basis.LIST
    assert tri(f.recoverable) == (36 * USD, 36 * USD, 42 * USD)   # [net − 1000 × $0.006; net]
    assert f.recoverable.basis is Basis.LIST and f.recoverable.evidence is Evidence.ESTIMATED
    assert f.headroom is None and attrs(f, "sku:linux_16_core")["minutes"] == "1000"
    w.lines[0] = dataclasses.replace(w.lines[0], quantity=None)
    assert one(run(w.ctx()), "larger-runner").recoverable.nano is None


def _outcome(prs: int) -> OutcomeAggregate:
    return OutcomeAggregate(date_utc="2026-09-10", team="(enterprise)", n_users=40, sessions=0,
                            commits=0, pull_requests=10, lines_added=0, lines_removed=0,
                            edits_accepted=0, edits_rejected=0,
                            source_kind="github.copilot_metrics",
                            extra=(("prs_merged_created_by_copilot", prs),))


def test_cloud_agent_cost_reads_outcomes() -> None:
    w = World().rows(2, team="agents", date_utc="2026-09-10", model="Claude Sonnet 5",
                     sku="coding_agent_ai_credit", credits="500")
    w.actions("100", workload="copilot_cloud_agent")
    w.outcomes.extend([_outcome(3), _outcome(1)])
    found = run(w.ctx())
    f = one(found, "cloud-agent-cost")
    assert f.cost_observed.nano == 1000 * CREDIT
    assert attrs(f, "agent:prs")["credits_per_merged_pr_nano"] == 250 * CREDIT
    assert attrs(f, "agent:actions")["actions_net_nano"] == 600_000_000
    assert "per merged Copilot-authored pull request" in f.summary
    assert "PR ratio" not in one(found, "dq.skipped-kinds").summary   # only activity kinds
    w.outcomes.clear()
    found = run(w.ctx())
    assert "credits_per_merged_pr_nano" not in attrs(one(found, "cloud-agent-cost"),
                                                     "agent:prs")
    dq = one(found, "dq.skipped-kinds")
    assert "cloud-agent-cost (PR ratio)" in dq.summary


def test_agent_failed_sessions_provider_estimate_only_in_evidence() -> None:
    w = World()
    for state, amount in (("failed", 30), ("timed_out", 20), ("cancelled", 10),
                          ("completed", 90), ("failed", 0)):
        w.agg("github.agent_tasks", "2026-09-10", {"state": state, "model": "claude-sonnet-5"},
              reported=amount * CREDIT)
    f = one(run(w.ctx()), "agent-failed-sessions")
    assert f.cost_observed.nano is None and "provider estimate only" in f.cost_observed.note
    assert f.cost_observed.basis is Basis.LIST_EQUIVALENT and f.category == "failure"
    a = attrs(f, "agent-tasks")
    assert a["provider_estimate_nano"] == 60 * CREDIT and a["sessions_total"] == 5
    assert (a["sessions_failed"], a["sessions_timed_out"], a["sessions_cancelled"]) == (1, 1, 1)
    figs = [f.cost_observed, f.recoverable, f.headroom, f.projected_monthly]
    assert all(g is None or g.basis is not Basis.PROVIDER_ESTIMATE for g in figs)


def test_unattributed_spend_share() -> None:
    w = World().row(team="t", principal=b.make_principal(1), cost_center="cc-1",
                    date_utc="2026-09-10", model="Claude Sonnet 5", credits="300")
    w.row(team="t", principal=b.make_principal(2), date_utc="2026-09-10",
          model="Claude Sonnet 5", credits="100")                       # no cost center
    w.row(unattributed=True, cost_center="cc-1", date_utc="2026-09-10",
          model="Claude Sonnet 5", credits="100")                       # no username
    f = one(run(w.ctx()), "unattributed-spend")
    assert f.cost_observed.nano == 200 * CREDIT and f.category == "attribution"
    a = attrs(f, "unattributed")
    assert (a["share"], a["no_username_nano"], a["no_cost_center_nano"]) == (
        "0.4", 100 * CREDIT, 100 * CREDIT)
    assert f.title.startswith("40%")


def test_no_reference_date_skips_dated_kinds() -> None:
    w = World().rows(2, team="data", **GPT54)
    w.pools.append(pool_month(3_100_000))
    ctx = dataclasses.replace(w.ctx(today=""), window=(0, 0))
    found = run(ctx)
    dq = one(found, "dq.skipped-kinds")
    assert "forced-migration: no reference date" in dq.summary
    assert "review-default-balanced" in dq.summary
    assert dq.category == "data-quality" and dq.cost_observed.nano is None
    assert "no mechanical fix" in dq.summary.lower() and dq.fix is None
