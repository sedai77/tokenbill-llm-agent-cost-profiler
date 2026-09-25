"""A small Copilot world of canonical records built from ``core.builders`` (CP-SYNTH-W tests).

The records are shaped as the Copilot adapters produce them (token aggregates summed per
(day, dims), one cost line per report row, seats per (principal, org), …) so a writer → adapter
round trip can be compared with them. Teams: ``platform`` (6, org-a, Enterprise / Business mix,
3 idle seats, $0 user budgets), ``infra`` (5, org-a, team-assigned), ``ops`` (5, org-b,
``assign_all``, JetBrains) and ``tiny`` (3, org-b, below k). Local sources: two Copilot CLI
sessions (subagent lane, compaction, model switch, truncation, checkpoint, error, session meta),
four VS Code conversations, one gh-aw run and one Claude Code request.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from typing import Any

from tokenbill.core.builders import (
    make_activity,
    make_actions_line,
    make_ai_usage_row,
    make_attempt,
    make_config,
    make_copilot_ctx,
    make_inference,
    make_lane,
    make_license,
    make_principal,
    make_request,
    make_seat_line,
)
from tokenbill.core.ids import copilot_lane_key, copilot_session_key, natural_id, stable_id
from tokenbill.core.records import (
    Attribution,
    CostLine,
    InferenceKind,
    LaneEvent,
    LaneEventKind,
    LaneKind,
    OutcomeAggregate,
    Session,
    UsageAggregate,
    UsageBuckets,
    WorkloadClass,
)

DAY_MS = 86_400_000
TODAY = "2026-09-23"
DAYS = tuple(f"2026-09-{d:02d}" for d in range(15, 23))
SNAPSHOT = "2026-09-22"
TEAMS = {"platform": ("org-a", "cc-platform", 6), "infra": ("org-a", "cc-infra", 5),
         "ops": ("org-b", "cc-ops", 5), "tiny": ("org-b", None, 3)}
TEAM_MODELS = {"platform": "Claude Opus 4.8", "infra": "Auto: Claude Haiku 4.5",
               "ops": "Claude Opus 4.8 (fast mode)", "tiny": "GPT-5.5"}


def day_ms(day: str) -> int:
    return (dt.date.fromisoformat(day) - dt.date(1970, 1, 1)).days * DAY_MS


def h(seed: str) -> str:
    """A fixture ``h_`` pseudonym."""
    return "h_" + hashlib.sha256(f"fixture-name:{seed}".encode()).hexdigest()[:20]


@dataclass
class World:
    """A CopilotWorld-like container: ``records`` by kind, ``today``, optional ``logins``."""

    records: dict[str, list[Any]]
    today: str = TODAY
    variants: tuple[str, ...] = ()
    logins: dict[str, str] = field(default_factory=dict)
    members: dict[str, list[str]] = field(default_factory=dict)


def principals() -> dict[str, list[str]]:
    return {team: [make_principal(f"{team}-{i}") for i in range(n)]
            for team, (_, _, n) in TEAMS.items()}


def _ai_rows(members: dict[str, list[str]]) -> tuple[list[CostLine], list[UsageAggregate]]:
    lines: list[CostLine] = []
    aggs: dict[str, UsageAggregate] = {}

    def add(line: CostLine, agg: UsageAggregate, tokens: bool = True) -> None:
        lines.append(line)
        if not tokens:
            return
        cur = aggs.get(agg.agg_id)
        if cur is None:
            aggs[agg.agg_id] = agg
            return
        aggs[agg.agg_id] = UsageAggregate(
            agg_id=cur.agg_id, source_kind=cur.source_kind, bucket_start_ms=cur.bucket_start_ms,
            bucket_end_ms=cur.bucket_end_ms, dims=cur.dims, usage=cur.usage + agg.usage,
            reported_cost_nano=(cur.reported_cost_nano or 0) + (agg.reported_cost_nano or 0),
            reported_cost_basis=cur.reported_cost_basis,
            list_cost_nano=(cur.list_cost_nano or 0) + (agg.list_cost_nano or 0),
            finality=cur.finality, fetched_ms=cur.fetched_ms)

    for d_i, day in enumerate(DAYS):
        for team, people in members.items():
            org, cc, _ = TEAMS[team]
            for i, p in enumerate(people):
                if (i + d_i) % 4 == 3:
                    continue      # not everyone works every day
                credits = f"{10 + 3 * i + d_i}.{(7 * i + 13 * d_i) % 100:02d}"
                if i == 0 and d_i == 1:
                    credits = "42.726213"
                discount = credits if team in ("platform", "infra") else "5"
                line, agg = make_ai_usage_row(
                    date_utc=day, model=TEAM_MODELS[team], credits=credits,
                    discount_credits=discount, principal=p, organization=org, cost_center=cc,
                    team=team, repo=h(f"repo-{team}") if i % 2 == 0 else None,
                    input_tokens=1_000 * (i + 1) + d_i, output_tokens=300 + 11 * i + d_i,
                    cache_read_tokens=20_000 + 1_000 * i if team != "tiny" else 0,
                    cache_write_tokens=1_500 + 10 * d_i if team == "platform" else 0)
                add(line, agg)
            if team == "platform" and d_i % 3 == 0:
                # a second model for one platform user: Opus 5.5 is priced only from 09-22
                line, agg = make_ai_usage_row(
                    date_utc=day, model="Claude Sonnet 5", credits="3.5",
                    discount_credits="3.5", principal=people[1], organization=org,
                    cost_center=cc, team=team, input_tokens=700, output_tokens=90,
                    cache_read_tokens=4_000, cache_write_tokens=0)
                add(line, agg)
        line, agg = make_ai_usage_row(date_utc=day, model="Code Review", credits=f"{2 + d_i}.25",
                                      discount_credits="0", unattributed=True,
                                      organization="org-a", input_tokens=5_000 + d_i,
                                      output_tokens=400, cache_read_tokens=0,
                                      cache_write_tokens=0)
        add(line, agg)
    legacy, _ = make_ai_usage_row(date_utc=DAYS[0], model="Claude Opus 4.8", credits="2",
                                  discount_credits="2", principal=members["platform"][0],
                                  organization="org-a", cost_center="cc-platform",
                                  team="platform", sku="copilot_premium_request")
    lines.append(legacy)
    return lines, list(aggs.values())


def _metered(members: dict[str, list[str]]) -> list[CostLine]:
    out: list[CostLine] = []
    for team, people in members.items():
        org, cc, _ = TEAMS[team]
        for i, p in enumerate(people):
            plan = "enterprise" if team == "platform" and i < 4 else "business"
            out.append(make_seat_line(plan, "1", date_utc="2026-09-01", organization=org,
                                      cost_center=cc, team=team, principal=p))
    out.append(make_actions_line("30", workload="copilot_code_review", date_utc=DAYS[2],
                                 repo=h("repo-platform")))
    out.append(make_actions_line("12", workload="copilot_code_review", date_utc=DAYS[3],
                                 repo=h("repo-infra"), discount_nano=72_000_000))
    out.append(make_actions_line("45", sku="linux_16_core", usd_per_minute="0.064",
                                 workload="copilot_cloud_agent", date_utc=DAYS[4],
                                 repo=h("repo-platform")))
    out.append(make_actions_line("18", workload="agentic_workflow", date_utc=DAYS[5],
                                 repo=h("repo-ci"), workflow=h("triage.lock.yml")))
    out.append(make_actions_line("6", workload="code_quality", date_utc=DAYS[5],
                                 repo=h("repo-infra")))
    gross = 2_500_000_000
    out.append(CostLine(
        line_id=natural_id("cl", "github.metered_usage", DAYS[6], "sandbox_linux", "org-a"),
        source_kind="github.metered_usage", date_utc=DAYS[6], channel="github_sandbox",
        workspace_id="org-a", description="sandbox_linux hours", model=None, cost_type="sandbox",
        token_type=None, sku="sandbox_linux", service_tier=None, inference_geo=None,
        endpoint_scope=None, amount_nano=gross, list_amount_nano=gross, finality="final",
        quantity="2.5", unit="hours"))
    return out


def _licenses(members: dict[str, list[str]]) -> list[Any]:
    out = []
    fetched = day_ms(SNAPSHOT) + 6 * 3_600_000
    for team, people in members.items():
        org, cc, _ = TEAMS[team]
        for i, p in enumerate(people):
            plan = "enterprise" if team == "platform" and i < 4 else "business"
            idle = (team == "platform" and i >= 3) or (team == "ops" and i >= 2)
            surface = {"platform": "vscode", "infra": "vscode", "ops": "jetbrains",
                       "tiny": "github_com"}[team]
            if team == "tiny" and i == 2:
                surface = None
            out.append(make_license(
                p, snapshot_date=SNAPSHOT, plan=plan, team=team, cost_center=cc, org=org,
                seat_created="2026-01-15" if i % 2 == 0 else None,
                pending_cancellation="2026-10-01" if (team, i) == ("ops", 4) else None,
                last_activity_bucket="none_90d" if idle else ("31-90" if i == 1 else "0-7"),
                last_activity_surface=None if idle else surface,
                last_authenticated_bucket="8-30" if i == 2 else "0-7",
                assigned_via_team=team == "infra", fetched_ms=fetched))
    return out


def _activity(members: dict[str, list[str]]) -> list[Any]:
    out = []
    for d_i, day in enumerate(DAYS[-3:]):
        for team, people in members.items():
            for i, p in enumerate(people):
                counts = {"interactions": 5 + i + d_i, "code_generation": 3 + i,
                          "code_acceptance": 2 + d_i, "loc_suggested_add": 40 + i,
                          "loc_suggested_delete": 4, "loc_added": 30 + i + d_i,
                          "loc_deleted": 3 + d_i,
                          f"ide:{'intellij' if team == 'ops' else 'vscode'}": 5 + i + d_i,
                          "feature:chat_panel_agent_mode": 6 + i,
                          "model:claude-opus-4-8": 4 + d_i, "model:auto": 1}
                flags = ["used_chat", "used_agent"]
                if team == "platform":
                    counts.update({"cli_sessions": 1, "cli_requests": 4 + i,
                                   "cli_prompts": 2, "cli_prompt_tokens": 12_000 + i,
                                   "cli_output_tokens": 800 + d_i,
                                   "feature:copilot_cli": 3})
                    flags.append("used_cli")
                if (team, i) == ("infra", 0):
                    counts.update({"app_requests": 2, "app_sessions": 1})
                    flags.append("used_copilot_app")
                if (team, i) == ("ops", 1):
                    counts["third_party_agent_jobs"] = 2
                    flags.append("used_cloud_agent")
                out.append(make_activity(p, date_utc=day, team=team, cost_center=TEAMS[team][1],
                                         reported_cost_nano=125_000_000 + 10_000_000 * i,
                                         counts=counts, flags=flags))
    return out


def _outcomes() -> list[OutcomeAggregate]:
    out = []
    for d_i, day in enumerate(DAYS[-3:]):
        out.append(OutcomeAggregate(
            date_utc=day, team="(enterprise)", n_users=19, sessions=0, commits=0,
            pull_requests=7 + d_i, lines_added=600 + d_i, lines_removed=70, edits_accepted=40,
            edits_rejected=0, source_kind="github.copilot_metrics",
            extra=(("prs_created_by_copilot", 2), ("prs_merged", 7 + d_i),
                   ("prs_reviewed_by_copilot", 5))))
    out.append(OutcomeAggregate(
        date_utc=DAYS[-1], team="(org:org-a)", n_users=11, sessions=0, commits=0,
        pull_requests=4, lines_added=300, lines_removed=20, edits_accepted=21, edits_rejected=0,
        source_kind="github.copilot_metrics", extra=(("prs_merged", 4),)))
    out.append(OutcomeAggregate(
        date_utc=DAYS[-1], team="(enterprise)", n_users=0, sessions=0, commits=0,
        pull_requests=9, lines_added=0, lines_removed=0, edits_accepted=0, edits_rejected=0,
        source_kind="github.copilot_metrics.repos",
        extra=(("copilot_applied_suggestions", 0), ("copilot_suggestions", 0),
               ("prs_created_by_copilot", 3), ("prs_merged", 9),
               ("prs_merged_created_by_copilot", 1), ("prs_reviewed_by_copilot", 0))))
    return out


def _config(members: dict[str, list[str]]) -> list[Any]:
    ms = day_ms(SNAPSHOT) + 7 * 3_600_000
    out = [
        make_config("budget", {"scope": "enterprise", "type": "ProductPricing",
                               "sku": "copilot_ai_credit", "amount_nano": 1_000_000_000_000,
                               "prevent_further_usage": False, "will_alert": True,
                               "n_recipients": 2, "target": "enterprise"},
                    entity_id="budget:b-ent", snapshot_ms=ms, fetched_ms=ms),
        make_config("budget", {"scope": "organization", "type": "SkuPricing",
                               "sku": "coding_agent_ai_credit,copilot_ai_credit",
                               "amount_nano": 250_500_000_000, "prevent_further_usage": True,
                               "will_alert": False, "n_recipients": 0, "target": "org:org-a",
                               "expires_at": "2026-12-31"},
                    entity_id="budget:b-org", snapshot_ms=ms, fetched_ms=ms),
        make_config("budget", {"scope": "cost_center", "type": "SkuPricing",
                               "sku": "copilot_ai_credit", "amount_nano": 500_000_000_000,
                               "prevent_further_usage": False, "will_alert": True,
                               "n_recipients": 1, "target": "cc:cc-platform"},
                    entity_id="budget:b-cc", snapshot_ms=ms, fetched_ms=ms),
        make_config("budget", {"scope": "organization", "type": "SkuPricing",
                               "sku": "copilot_enterprise", "prevent_further_usage": False,
                               "target": "org:org-b"},
                    entity_id="budget:b-seats", snapshot_ms=ms, fetched_ms=ms),
        make_config("budget_users", {"n_users": 6, "n_at_or_over_target": 2,
                                     "consumed_p50_nano": 12_500_000_000,
                                     "consumed_p90_nano": 30_000_000_000},
                    entity_id="budget:b-ent", snapshot_ms=ms, fetched_ms=ms),
        make_config("cost_center", {"cost_center_id": "cc-0001", "state": "active",
                                    "pool_enabled": True, "pool_target_credits": 50_000,
                                    "pool_current_credits": "12345.5", "n_users": 6,
                                    "n_teams": 1, "n_orgs": 1, "n_repos": 0, "azure": False},
                    entity_id="cc:cc-platform", snapshot_ms=ms, fetched_ms=ms),
        make_config("cost_center", {"cost_center_id": "cc-0002", "state": "active",
                                    "pool_enabled": False, "n_users": 5, "n_teams": 0,
                                    "n_orgs": 0, "n_repos": 1, "azure": True},
                    entity_id="cc:cc-infra", snapshot_ms=ms, fetched_ms=ms),
        make_config("org_settings", {"plan_type": "business",
                                     "seat_management_setting": "assign_selected",
                                     "ide_chat": "enabled", "platform_chat": "enabled",
                                     "cli": "enabled", "seats_total": 11,
                                     "seats_added_this_cycle": 1,
                                     "seats_pending_cancellation": 0,
                                     "seats_pending_invitation": 0,
                                     "seats_active_this_cycle": 8,
                                     "seats_inactive_this_cycle": 3},
                    entity_id="org:org-a", snapshot_ms=ms, fetched_ms=ms),
        make_config("org_settings", {"plan_type": "business",
                                     "seat_management_setting": "assign_all",
                                     "ide_chat": "enabled", "platform_chat": "disabled",
                                     "cli": "disabled", "seats_total": 8},
                    entity_id="org:org-b", snapshot_ms=ms, fetched_ms=ms),
        make_config("run_flags", {"billing_mode.enterprise": "metered",
                                  "promo_eligible": True, "compliance": "none",
                                  "capped_policy.cc-platform": "block",
                                  "renewal_date.enterprise": "2027-01-01",
                                  "pool_seats.org:org-a.business": 7,
                                  "budget_stop.enterprise": False},
                    entity_id="run", snapshot_ms=ms, fetched_ms=ms),
    ]
    for i in range(2):   # $0 user budgets in platform
        out.append(make_config("budget", {"scope": "user", "type": "ProductPricing",
                                          "sku": "copilot_ai_credit", "amount_nano": 0,
                                          "prevent_further_usage": True, "will_alert": False,
                                          "team": "platform", "cost_center": "cc-platform",
                                          "consumed_nano": 0},
                               entity_id=f"budget:b-user-{i}", snapshot_ms=ms, fetched_ms=ms))
    return out


def _agent_tasks() -> list[UsageAggregate]:
    out = []
    base = day_ms(DAYS[3]) + 10 * 3_600_000
    specs = [("claude-sonnet-5", "completed", "branch+pull", h("repo-platform"), "platform",
              1_234_567_890, 900_000),
             ("auto", "completed", "pull", h("repo-infra"), "infra", 50_000_000, 300_000),
             ("unknown", "in_progress", "none", None, "(unmapped)", None, 0)]
    for i, (model, state, artifact, repo, team, cost, dur) in enumerate(specs):
        dims = {"channel": "github_copilot", "model": model, "state": state,
                "artifact": artifact, "team": team}
        if repo:
            dims["repo"] = repo
        out.append(UsageAggregate(
            agg_id=stable_id("ag", "github.agent_tasks", f"session-{i}"),
            source_kind="github.agent_tasks", bucket_start_ms=base + i * 3_600_000,
            bucket_end_ms=base + i * 3_600_000 + dur, dims=tuple(sorted(dims.items())),
            usage=UsageBuckets(), reported_cost_nano=cost,
            reported_cost_basis="provider_estimate" if cost is not None else None,
            finality="final" if state == "completed" else "provisional"))
    return out


def _request(session_key: str, lane_key: str, seq: int, ts: int, usage: UsageBuckets,
             model: str, attribution: Attribution, cost: int | None, *,
             routing: str = "direct", context_tier: str | None = None,
             compaction: tuple[UsageBuckets, int] | None = None, billing_path: str) -> Any:
    ctx = make_copilot_ctx(model, routing=routing, context_tier=context_tier,
                           billing_path=billing_path)
    infs = []
    rid = stable_id("rq", lane_key, seq)
    if compaction is not None:
        c_usage, c_cost = compaction
        infs.append(make_inference(c_usage, model=model, kind=InferenceKind.COMPACTION,
                                   inference_id=stable_id("inf", rid, "c"), ctx=ctx,
                                   provider_reported_cost_nano=c_cost,
                                   provider_reported_cost_basis="provider_estimate"))
    infs.append(make_inference(usage, model=model, inference_id=stable_id("inf", rid, 0), ctx=ctx,
                               provider_reported_cost_nano=cost,
                               provider_reported_cost_basis="provider_estimate"
                               if cost is not None else None))
    attempt = make_attempt(infs, ts_ms=ts, attempt_id=stable_id("at", rid, 0),
                           message_id=f"msg_{stable_id('m', rid)[3:]}", model_served=model,
                           duration_ms=2_000 + 100 * seq, ttft_ms=400 + seq)
    return make_request(lane_key, seq, ts, model=model, request_id=rid, session_key=session_key,
                        attribution=attribution, attempts=[attempt])


def _sessions(members: dict[str, list[str]]) -> list[Session]:
    out = []
    base = day_ms(DAYS[-2]) + 9 * 3_600_000
    # Copilot CLI: two platform users
    for n, p in enumerate(members["platform"][:2]):
        skey = copilot_session_key(f"cli-session-{n}")
        main_key = copilot_lane_key(skey, "main", None)
        sub_key = copilot_lane_key(skey, "subagent", "explore")
        attr = Attribution(principal=p, team="platform", agent_product="copilot_cli",
                           billing_path="copilot_pool", repo=h("repo-platform"),
                           workload_class=WorkloadClass.INTERACTIVE)
        t0 = base + n * 3_600_000
        main = []
        for s in range(4):
            usage = UsageBuckets(uncached_input=1_200 + s, cache_read=30_000 + 1_000 * s,
                                 cache_write_unknown=2_000 if s == 0 else 0,
                                 output=400 + 10 * s, output_reasoning=50 if s == 1 else None)
            main.append(_request(skey, main_key, s, t0 + s * 60_000, usage, "claude-sonnet-5",
                                 attr, 23_284_800 + s * 1_000_000,
                                 routing="auto" if s == 2 else "direct",
                                 context_tier="long_context" if n == 0 else None,
                                 compaction=(UsageBuckets(uncached_input=90_000,
                                                          cache_read=10_000, output=2_000),
                                             180_000_000) if s == 3 else None,
                                 billing_path="copilot_pool"))
        sub = [_request(skey, sub_key, 0, t0 + 90_000,
                        UsageBuckets(uncached_input=500, cache_read=8_000, output=120),
                        "gpt-5.5", attr, 4_000_000, billing_path="copilot_pool")]
        events = [
            LaneEvent(main_key, t0 - 1_000, LaneEventKind.SESSION_META,
                      (("context_tier", "long_context" if n == 0 else "default"),
                       ("credit_limit_nano", 5_000_000_000), ("routing_mode", "manual"))),
            LaneEvent(main_key, t0 + 170_000, LaneEventKind.COMPACTION,
                      (("copilot_trigger", "threshold"), ("post_tokens", 20_000),
                       ("pre_tokens", 150_000), ("system_tokens", 4_000),
                       ("tool_definitions_tokens", 30_000), ("trigger", "auto"))),
            LaneEvent(main_key, t0 + 100_000, LaneEventKind.MODEL_SWITCH_USER,
                      (("from_model", "claude-sonnet-5"), ("to_model", "gpt-5.5"))),
            LaneEvent(main_key, t0 + 110_000, LaneEventKind.CONTEXT_EDIT,
                      (("cleared_input_tokens", 12_000), ("edit_type", "copilot_truncation"))),
            LaneEvent(main_key, t0 + 120_000, LaneEventKind.COST_STATE,
                      (("reported_total_nano", 50_000_000),
                       ("reporter", "copilot.cli.checkpoint"))),
            LaneEvent(main_key, t0 + 130_000, LaneEventKind.API_ERROR,
                      (("error_type", "rate_limit"), ("status", 429))),
        ]
        lanes = (make_lane(main, LaneKind.MAIN, events, scope="unknown", lane_key=main_key,
                           session_key=skey),
                 make_lane(sub, LaneKind.SUBAGENT, scope="unknown", lane_key=sub_key,
                           session_key=skey, parent_lane_key=main_key))
        out.append(Session(skey, "copilot.cli", attr, lanes, t0, t0 + 300_000))
    # VS Code: infra 0-1, ops 0-1
    for n, p in enumerate(members["infra"][:2] + members["ops"][:2]):
        team = "infra" if n < 2 else "ops"
        skey = copilot_session_key(f"vscode-conversation-{n}")
        lane_key = copilot_lane_key(skey, "main", None)
        attr = Attribution(principal=p, team=team, agent_product="copilot_vscode",
                           billing_path="copilot_pool")
        t0 = base + 7_200_000 + n * 600_000
        reqs = []
        for s in range(3):
            usage = UsageBuckets(uncached_input=800 + 7 * s + n,
                                 cache_read=12_000 * s,
                                 cache_write_unknown=5_000 if s == 0 else 250 * s,
                                 output=250 + s * 25 + n)
            model = "claude-opus-4-8" if s < 2 else "gpt-5.4"
            reqs.append(_request(skey, lane_key, s, t0 + s * 45_000, usage, model, attr,
                                 1_000_000 * (s + 1) + n,
                                 context_tier="default" if n == 0 else None,
                                 billing_path="copilot_pool"))
        lane = make_lane(reqs, LaneKind.MAIN, scope="unknown", lane_key=lane_key,
                         session_key=skey)
        out.append(Session(skey, "copilot.vscode", attr, (lane,), t0, t0 + 200_000))
    # gh-aw run (org-billed CI)
    skey = copilot_session_key("gh-aw-run-1")
    lane_key = copilot_lane_key(skey, "main", None)
    attr = Attribution(team="ci", agent_product="copilot_gh_aw", billing_path="copilot_direct",
                       repo=h("repo-ci"), workload_class=WorkloadClass.CI)
    t0 = base + 20_000_000
    reqs = [_request(skey, lane_key, s, t0 + s * 5_000,
                     UsageBuckets(uncached_input=272 + s, cache_read=19_200, output=35 + s),
                     "claude-sonnet-5", attr, 1_501_800 + s * 100, billing_path="copilot_direct")
            for s in range(3)]
    out.append(Session(skey, "gh_aw.run", attr, (make_lane(reqs, LaneKind.MAIN, scope="unknown",
                                                           lane_key=lane_key,
                                                           session_key=skey),), t0, t0 + 20_000))
    return out


def claude_request() -> Any:
    """A Claude Code request (the Claude resource of the mixed OTLP file)."""
    attr = Attribution(agent_product="claude_code", billing_path="api_key")
    return make_request("cc-lane", 0, day_ms(DAYS[-1]) + 3_600_000,
                        UsageBuckets(uncached_input=12, cache_read=40_000,
                                     cache_write_unknown=3_000, output=800),
                        "claude-opus-5-5", request_id="rq-claude-code", session_key="cc-session",
                        attribution=attr)


def build_world(**kw: Any) -> World:
    """The fixture world (deterministic)."""
    members = principals()
    lines, aggs = _ai_rows(members)
    records = {
        "cost_lines": lines + _metered(members),
        "aggregates": aggs + _agent_tasks(),
        "licenses": _licenses(members),
        "activity": _activity(members),
        "config": _config(members),
        "outcomes": _outcomes(),
        "sessions": _sessions(members),
        "requests": [claude_request()],
    }
    return World(records=records, members=members, **kw)


def team_of(world: World) -> dict[str, str]:
    """principal → team."""
    return {p: t for t, ps in world.members.items() for p in ps}
