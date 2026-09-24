"""GitHub Copilot record contracts (CORE-AMENDMENTS C-1 … C-10): billing paths and class ``pool``,
the extra key, ``PricingContext`` fields, raw-usage enums, ``CostLine`` / ``OutcomeAggregate``
additions, lane-event attrs, the vocabularies and the seat / activity / configuration records."""

from __future__ import annotations

import dataclasses
import datetime
import json
from dataclasses import replace

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.core import records as r
from tokenbill.core.builders import (
    make_activity,
    make_config,
    make_cost_line,
    make_ctx,
    make_license,
    make_principal,
)
from tokenbill.core.errors import ContractViolation

P1 = make_principal(1)
P2 = make_principal(2)
H = "h_" + "a" * 20

# ---------- C-1 / C-2 / C-3 ----------


def test_billing_class_table() -> None:
    table = {
        "subscription": "allowance",
        "copilot_pool": "pool",
        "copilot_direct": "pool",
        "api_key": "billed",
        "usage_credits": "billed",
        "bedrock": "billed",
        "unknown": "billed",
        None: "billed",
        "not-a-path": "billed",
    }
    for path, cls in table.items():
        assert r.billing_class(path) == cls, path
    assert r.BILLING_PATHS[-2:] == ("copilot_pool", "copilot_direct")
    assert r.BILLING_PATHS[:10] == (
        "api_key", "subscription", "usage_credits", "bedrock", "vertex", "foundry",
        "claude_platform_aws", "openai", "azure_openai", "unknown",
    )
    assert r.COPILOT_BILLING_PATHS == ("copilot_pool", "copilot_direct")
    assert r.BILLING_CLASSES == ("billed", "allowance", "pool")
    assert {r.billing_class(p) for p in r.BILLING_PATHS} == set(r.BILLING_CLASSES)
    assert r.COPILOT_CHANNELS == ("github_copilot", "github_actions", "github_sandbox")


def test_lane_billing_class_pool() -> None:
    from tokenbill.core.builders import make_lane, make_request, make_usage

    lane = make_lane([make_request("L", 0, 0, make_usage(output=1), provider="github",
                                   channel="github_copilot", billing_path="copilot_pool")])
    assert lane.billing_class == "pool"
    direct = make_lane([make_request("D", 0, 0, make_usage(output=1),
                                     billing_path="copilot_direct")])
    assert direct.billing_class == "pool"


def test_copilot_compliance_extra_key() -> None:
    assert r.EXTRA_KEYS[-1] == "copilot_compliance"
    attr = r.Attribution(extra=(("copilot_compliance", "data_residency"), ("gateway", "g")))
    assert dict(attr.extra).get("copilot_compliance") == "data_residency"
    with pytest.raises(ContractViolation):
        r.Attribution(extra=(("copilot_compliance", "x" * 129),))


def test_pricing_context_copilot_fields() -> None:
    ctx = make_ctx("claude-opus-5-5", provider="github", channel="github_copilot",
                   billing_path="copilot_pool")
    assert (ctx.routing, ctx.compliance, ctx.context_tier) == ("direct", None, None)
    ok = replace(ctx, routing="auto", compliance="fedramp", context_tier="long_context")
    assert r.from_json(r.PricingContext, r.to_json(ok)) == ok
    for field, value in (("routing", "sometimes"), ("routing", None), ("compliance", "none"),
                         ("compliance", "gdpr"), ("context_tier", "1m")):
        with pytest.raises(ContractViolation):
            replace(ctx, **{field: value})
    # names of the pre-Copilot fields unchanged, new ones appended
    names = [f.name for f in dataclasses.fields(r.PricingContext)]
    assert names[-3:] == ["routing", "compliance", "context_tier"]
    assert names.index("billing_path") == len(names) - 4


# ---------- C-4 ----------


def test_raw_usage_enums_and_record_fields() -> None:
    assert r.RAW_USAGE_ENUMS["tokenType"] == {"input", "cache_read", "cache_write", "output"}
    assert r.RAW_USAGE_ENUMS["contextTier"] == {"default", "long_context"}
    assert r.RAW_USAGE_ENUMS["initiator"] == frozenset()
    assert r.RAW_USAGE_ENUMS["interactionType"] == frozenset()
    with pytest.raises(TypeError):
        r.RAW_USAGE_ENUMS["x"] = frozenset()  # type: ignore[index]
    assert r.RAW_USAGE_NUMERIC == {"totalNanoAiu", "batchSize", "costPerBatch", "tokenCount"}
    line = make_cost_line(1)
    assert r.record_fields(r.CostLine) == frozenset(r.to_json(_copilot_line()))
    assert {"quantity", "pseudo", "workflow"} <= r.record_fields(r.CostLine)
    assert frozenset(r.to_json(line)) < r.record_fields(r.CostLine)  # defaults left out
    from tokenbill.core.types import PublishedAggregate

    assert "token" not in r.record_fields(PublishedAggregate)
    with pytest.raises(TypeError):
        r.record_fields(line)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        r.record_fields(int)


# ---------- appended fields: pre-Copilot documents stay byte-identical ----------

WAVE1_COST_LINE_KEYS = {
    "line_id", "source_kind", "date_utc", "channel", "workspace_id", "description", "model",
    "cost_type", "token_type", "sku", "service_tier", "inference_geo", "endpoint_scope",
    "amount_nano", "list_amount_nano", "currency", "finality", "principal", "fetched_ms"}
WAVE1_PRICING_KEYS = {"provider", "channel", "model", "model_raw", "service_tier", "speed",
                      "inference_geo", "endpoint_scope", "write_ttl_hint", "billing_path"}


def test_appended_fields_omitted_at_default() -> None:
    line = make_cost_line(1)
    assert set(r.to_json(line)) == WAVE1_COST_LINE_KEYS
    ctx = make_ctx("claude-opus-5-5")
    doc = r.to_json(ctx)
    assert set(doc) == WAVE1_PRICING_KEYS
    assert json.dumps(doc, sort_keys=True) == json.dumps(
        {k: v for k, v in r.to_json(replace(ctx, routing="auto")).items() if k != "routing"},
        sort_keys=True)
    assert set(r.to_json(replace(ctx, routing="auto"))) == WAVE1_PRICING_KEYS | {"routing"}
    assert "extra" not in r.to_json(_outcome()) and "extra" in r.to_json(
        _outcome([("prs_merged", 1)]))
    assert r.from_json(r.PricingContext, doc) == ctx  # missing appended keys → defaults
    full = r.to_json(replace(ctx, routing="direct", compliance=None))
    assert "routing" not in full and "compliance" not in full
    fields = {f.name: f for f in dataclasses.fields(r.PricingContext)}
    assert fields["routing"].metadata[r.OMIT_DEFAULT] and not fields["model"].metadata


# ---------- C-5 ----------


def _copilot_line(**kw: object) -> r.CostLine:
    base = dict(channel="github_copilot", model="claude-opus-5-5", cost_type="ai_credit.user",
                source_kind="github.ai_usage_report", quantity="42.726213", unit="ai-credits",
                cost_center="cc-1", team="t", repo=H, workload="copilot_code_review",
                workflow="h_" + "b" * 20, routing="auto", speed="fast", pseudo="code_review",
                principal=P1)
    base.update(kw)
    return make_cost_line(427262130, **base)  # type: ignore[arg-type]


def test_cost_line_copilot_fields_round_trip() -> None:
    line = _copilot_line()
    assert r.from_json(r.CostLine, r.to_json(line)) == line
    names = [f.name for f in dataclasses.fields(r.CostLine)]
    assert names[-10:] == ["quantity", "unit", "cost_center", "team", "repo", "workload",
                           "workflow", "routing", "speed", "pseudo"]
    assert names[-11] == "fetched_ms"
    plain = make_cost_line(5)
    assert (plain.quantity, plain.repo, plain.pseudo) == (None, None, None)


@pytest.mark.parametrize(
    "field,value",
    [
        ("repo", "acme/widgets"),
        ("repo", "h_short"),
        ("workflow", ".github/workflows/x.lock.yml"),
        ("pseudo", "reviewer"),
        ("workload", "ci"),
        ("routing", "manual"),
        ("speed", "turbo"),
        ("quantity", "1e3"),
        ("quantity", " 1"),
        ("quantity", "NaN"),
        ("quantity", 5),
        ("principal", "r_ref"),
        ("principal", "c_" + "a" * 20),
        ("team", 5),
    ],
)
def test_cost_line_rejects(field: str, value: object) -> None:
    with pytest.raises(ContractViolation):
        _copilot_line(**{field: value})


def test_cost_vocabularies() -> None:
    assert r.GITHUB_COST_TYPES == (
        "ai_credit.user", "ai_credit.direct", "ai_credit.legacy_pru", "seat", "actions", "sandbox",
        "code_quality.license", "metered.ai_credit", "rest.ai_credit", "rest.summary",
        "rest.usage", "other",
    )
    assert r.COPILOT_WORKLOADS == ("copilot_code_review", "copilot_cloud_agent",
                                   "agentic_workflow", "code_quality")
    assert r.COPILOT_PSEUDO == ("code_review", "cloud_agent", "auto_unattributed", "unknown")
    # cost_type stays free for other providers
    assert make_cost_line(1, cost_type="tokens").cost_type == "tokens"


# ---------- C-6 / C-7 ----------


def test_aggregate_vocabularies() -> None:
    assert r.COPILOT_AGG_SOURCE_KINDS == ("github.ai_usage_report",
                                          "github.ai_usage_report.coverage",
                                          "github.agent_tasks", "copilot.cli_rollup", "gh_aw.run")
    assert r.COPILOT_AGG_DIMS[:3] == ("channel", "team", "cost_center")
    assert "source" in r.COPILOT_AGG_DIMS and len(set(r.COPILOT_AGG_DIMS)) == 15


def _outcome(extra: object = ()) -> r.OutcomeAggregate:
    return r.OutcomeAggregate("2026-09-01", "(enterprise)", 12, 0, 0, 40, 10, 2, 3, 0,
                              "github.copilot_metrics", extra)  # type: ignore[arg-type]


def test_outcome_extra() -> None:
    o = _outcome([("prs_merged", 40), ("copilot_suggestions", 9)])
    assert o.extra == (("copilot_suggestions", 9), ("prs_merged", 40))  # sorted, tuple
    assert r.from_json(r.OutcomeAggregate, r.to_json(o)) == o
    legacy = r.OutcomeAggregate("2026-09-01", "t", 5, 1, 1, 1, 1, 1, 1, 1)
    assert legacy.extra == () and legacy.source_kind == "anthropic.cc_analytics"
    for bad in ([("prs_open", 1)], [("prs_merged", -1)], [("prs_merged", True)],
                [("prs_merged", 1), ("prs_merged", 2)], "nope", [("prs_merged", 2**60)]):
        with pytest.raises(ContractViolation):
            _outcome(bad)
    assert r.OUTCOME_EXTRA_KEYS[0] == "prs_merged" and len(r.OUTCOME_EXTRA_KEYS) == 6


# ---------- C-8 ----------


def test_copilot_event_attrs() -> None:
    ev = r.LaneEvent("L", 1, "compaction", [
        ("trigger", "auto"), ("copilot_trigger", "context_limit_retry"), ("system_tokens", 900),
        ("tool_definitions_tokens", None), ("pre_tokens", 100)])
    assert dict(ev.attrs)["copilot_trigger"] == "context_limit_retry"
    meta = r.LaneEvent("L", 1, "session_meta", [("credit_limit_nano", 300_000_000),
                                                 ("routing_mode", "auto"),
                                                 ("context_tier", "long_context")])
    assert r.from_json(r.LaneEvent, r.to_json(meta)) == meta
    with pytest.raises(ContractViolation):  # the Claude trigger domain is unchanged
        r.LaneEvent("L", 1, "compaction", [("trigger", "threshold")])
    with pytest.raises(ContractViolation):
        r.LaneEvent("L", 1, "compaction", [("system_tokens", "900")])
    with pytest.raises(ContractViolation):
        r.LaneEvent("L", 1, "session_meta", [("credit_limit_nano", True)])
    assert len(r.LaneEventKind) == 13
    domains = r.COPILOT_EVENT_VALUE_DOMAINS
    assert domains[(r.LaneEventKind.COMPACTION, "copilot_trigger")] == {
        "threshold", "manual", "context_limit_retry", "memory_pressure", "model_switch"}
    assert domains[(r.LaneEventKind.SESSION_META, "context_tier")] == {"default", "long_context"}
    for kind, key in domains:
        assert key in r.EVENT_ATTRS[kind]


# ---------- C-9 ----------


def test_vocabularies_c9() -> None:
    assert r.LICENSE_PLANS == ("business", "enterprise", "unknown")
    assert r.LICENSE_BUCKETS == ("0-7", "8-30", "31-90", "none_90d")
    assert r.EDITOR_FAMILIES == ("vscode", "jetbrains", "visual_studio", "xcode", "eclipse",
                                 "neovim", "cli", "github_com", "copilot_app", "mobile", "other")
    assert len(r.ACTIVITY_KEYS) == 23 and r.ACTIVITY_KEYS[0] == "interactions"
    assert r.ACTIVITY_KEYS[-1] == "third_party_agent_jobs"
    assert r.ACTIVITY_KEY_PREFIXES == ("feature:", "model:", "ide:")
    assert r.ACTIVITY_FLAGS == ("used_chat", "used_agent", "used_cli", "used_copilot_app",
                                "used_cloud_agent", "used_code_review_active",
                                "used_code_review_passive")
    assert r.CONFIG_KINDS == ("budget", "budget_users", "cost_center", "org_settings",
                              "run_flags", "seat_counts", "activity_counts", "plan_quota")
    assert r.COUNT_CONFIG_KINDS == ("seat_counts", "activity_counts", "plan_quota")
    assert r.CONFIG_SOURCE_KINDS[-2:] == ("tokenbill.admin_answers", "tokenbill.copilot_export")
    assert set(r.CONFIG_KEYS) == set(r.CONFIG_KINDS)
    assert "plan_type" in r.CONFIG_KEYS["org_settings"]
    assert "seat_management_setting" in r.CONFIG_KEYS["org_settings"]
    assert {"plan.", "pool_seats.", "billing_mode.", "renewal_date.", "capped_policy.",
            "budget_stop."} <= set(r.CONFIG_KEYS["run_flags"])
    assert r.CONFIG_KEYS["plan_quota"] == ("month", "quota", "n_users")
    assert r.PLAN_SOURCES == ("seat_lines", "seats_api", "org_settings", "report_quota",
                              "admin_statement", "none")


# ---------- C-10: LicenseSnapshot ----------


def test_license_snapshot_seats_and_activity_report() -> None:
    seat = make_license(P1, plan="enterprise", team="core", assigned_via_team=True,
                        last_activity_surface="jetbrains")
    assert r.from_json(r.LicenseSnapshot, r.to_json(seat)) == seat
    report = make_license(P2, plan="unknown", assigned_via_team=None, org=None,
                          source_kind="github.copilot_activity_report",
                          last_activity_surface=None, last_authenticated_bucket="unknown",
                          last_activity_bucket="none_90d")
    assert report.assigned_via_team is None and report.plan == "unknown"
    assert r.from_json(r.LicenseSnapshot, r.to_json(report)) == report
    names = [f.name for f in dataclasses.fields(r.LicenseSnapshot)]
    assert names[-2:] == ["fetched_ms", "source_kind"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("principal", "tb-canary-login-7f3a91"),
        ("principal", "r_device"),
        ("principal", "c_" + "0" * 20),
        ("plan", "mixed"),
        ("last_activity_bucket", "unknown"),
        ("last_authenticated_bucket", "1-2"),
        ("last_activity_surface", "vs code"),
        ("assigned_via_team", "yes"),
        ("source_kind", "github.copilot_metrics"),
        ("snapshot_date", "2026/09/01"),
        ("fetched_ms", -1),
        ("team", 3),
    ],
)
def test_license_snapshot_rejects(field: str, value: object) -> None:
    with pytest.raises(ContractViolation):
        replace(make_license(P1), **{field: value})


# ---------- ActivityDay ----------


def test_activity_day() -> None:
    counts = {"interactions": 5, "ide:intellij": 3, "model:claude-sonnet-4.6": 2,
              "feature:agent_edit": 1}
    day = make_activity(P1, counts=counts, flags=["used_chat", "used_agent"],
                        reported_cost_nano=125_000_000)
    assert day.counts[0] == ("feature:agent_edit", 1) and day.flags == ("used_agent", "used_chat")
    assert r.from_json(r.ActivityDay, r.to_json(day)) == day
    unsorted = replace(day, flags=("used_chat", "used_agent"))
    assert unsorted.flags == ("used_agent", "used_chat")
    for bad in ({"lines": 1}, {"ide:": 1}, {"ide:IntelliJ": 1}, {"ide:" + "x" * 65: 1},
                {"interactions": -1}, {"interactions": True}, {"other:x": 1}):
        with pytest.raises(ContractViolation):
            make_activity(P1, counts=bad)
    for flags in (["used_everything"], ["used_chat", "used_chat"]):
        with pytest.raises(ContractViolation):
            make_activity(P1, flags=flags)
    with pytest.raises(ContractViolation):
        make_activity("p_bad")
    with pytest.raises(ContractViolation):
        replace(day, reported_cost_nano="12")


# ---------- ConfigSnapshot ----------


def test_config_snapshot_kinds_and_keys() -> None:
    flags = make_config("run_flags", {"plan.enterprise": "enterprise", "promo_eligible": True,
                                      "pool_seats.enterprise.business": 100,
                                      "compliance": "none", "budget_stop.org:a": False},
                        entity_id="run")
    assert dict(flags.attrs)["plan.enterprise"] == "enterprise"
    assert r.from_json(r.ConfigSnapshot, r.to_json(flags)) == flags
    answers = make_config("run_flags", {"plan.org:acme": "business"}, entity_id="admin_answers",
                          source_kind="tokenbill.admin_answers")
    assert answers.source_kind == "tokenbill.admin_answers"
    budget = make_config("budget", {"scope": "user", "team": "core", "amount_nano": 10**12,
                                    "target": "cc:eng", "expires_at": None},
                         entity_id="budget:42")
    assert r.from_json(r.ConfigSnapshot, r.to_json(budget)) == budget
    org = make_config("org_settings", {"plan_type": "business",
                                       "seat_management_setting": "assign_selected"},
                      entity_id="org:acme")
    assert org.source_kind == "github.org_copilot_settings"
    quota = make_config("plan_quota", {"month": "2026-09", "quota": 3900, "n_users": 60},
                        entity_id="org:acme")
    assert quota.source_kind == "github.ai_usage_report"


@pytest.mark.parametrize(
    "kind,attrs,entity",
    [
        ("run_flags", {"plan": "business"}, "run"),              # "plan." is a prefix only
        ("run_flags", {"plan.": "business"}, "run"),             # empty suffix
        ("run_flags", {"stop_usage.org:a": True}, "run"),        # C-9 spells budget_stop.
        ("budget", {"principal": "x"}, "budget:1"),
        ("budget", {"team": make_principal(9)}, "budget:1"),     # no p_ value anywhere (R14)
        ("seat_counts", {"note": make_principal(3)}, "enterprise"),
        ("seat_counts", {"n": 2**64}, "enterprise"),
        ("seat_counts", {"n": 1.5}, "enterprise"),
        ("org_settings", {"plan_type": "business"}, "organization"),
        ("org_settings", {"plan_type": "business"}, "org:"),
        ("cost_center", {"n_users": 3}, "cc:a\nb"),
        ("mystery", {}, "run"),
    ],
)
def test_config_snapshot_rejects(kind: str, attrs: dict, entity: str) -> None:
    with pytest.raises(ContractViolation):
        make_config(kind, attrs, entity_id=entity, source_kind="tokenbill.cli")


def test_config_snapshot_rejects_source_kind_and_duplicates() -> None:
    with pytest.raises(ContractViolation):
        make_config("run_flags", {}, source_kind="github.seats")
    with pytest.raises(ContractViolation):
        r.ConfigSnapshot(0, "tokenbill.cli", "run_flags", "run",
                         (("compliance", "none"), ("compliance", "fedramp")))
    with pytest.raises(ContractViolation):
        r.ConfigSnapshot(-1, "tokenbill.cli", "run_flags", "run", ())


# ---------- record_key ----------


def test_record_keys() -> None:
    lic = make_license(P1, snapshot_date="2026-09-01", org=None)
    assert r.record_key(lic) == "\x1f".join(("2026-09-01", "github_copilot", P1, ""))
    assert r.record_key(replace(lic, org="acme")).endswith("\x1facme")
    day = make_activity(P2, date_utc="2026-09-02")
    assert r.record_key(day) == "\x1f".join(("2026-09-02", "github_copilot", P2))
    day_ms = (datetime.date(2026, 9, 1) - datetime.date(1970, 1, 1)).days * 86_400_000
    a = make_config("seat_counts", {"team": "core", "plan": "business", "bucket": "none_90d",
                                    "n": 7, "n_people": 7}, entity_id="enterprise",
                    snapshot_ms=day_ms + 5)
    b = make_config("seat_counts", {"team": "core", "plan": "business", "bucket": "0-7",
                                    "n": 7, "n_people": 7}, entity_id="enterprise",
                    snapshot_ms=day_ms + 9)
    c = replace(a, attrs=tuple((k, 99 if k == "n" else v) for k, v in a.attrs),
                snapshot_ms=day_ms)
    assert r.record_key(a) != r.record_key(b)       # count rows differ by their non-count attrs
    assert r.record_key(a) == r.record_key(c)       # the counts and the time of day are not key
    assert "bucket=none_90d" in r.record_key(a) and "n=7" not in r.record_key(a)
    assert r.record_key(a).split("\x1f")[:3] == ["seat_counts", "enterprise", "2026-09-01"]
    flag_a = make_config("run_flags", {"promo_eligible": True}, snapshot_ms=day_ms)
    flag_b = make_config("run_flags", {"promo_eligible": False}, snapshot_ms=day_ms + 1)
    assert r.record_key(flag_a) == r.record_key(flag_b)  # one run_flags row per entity and day
    act = make_config("activity_counts",
                      {"team": "core", "month": "2026-09", "ide:intellij": 4, "n_people": 6},
                      entity_id="enterprise")
    assert "ide:intellij=4" in r.record_key(act)
    bools = make_config("seat_counts", {"assigned_via_team": True, "zero_cost_30d": False,
                                        "team": None, "n": 1}, entity_id="enterprise")
    assert r.record_key(bools).endswith("assigned_via_team=true\x1fteam=\x1fzero_cost_30d=false")
    with pytest.raises(TypeError):
        r.record_key(make_cost_line(1))  # type: ignore[arg-type]


def test_record_key_is_injective_and_total() -> None:
    # a separator inside a free string cannot forge another row's key
    one = make_config("seat_counts", {"team": "a\x1fplan=business", "n": 1},
                      entity_id="enterprise")
    two = make_config("seat_counts", {"team": "a", "plan": "business", "n": 1},
                      entity_id="enterprise")
    assert r.record_key(one) != r.record_key(two)
    assert r.record_key(one).count("\x1f") == 3
    slash = make_config("seat_counts", {"team": "a\\x1fplan=business", "n": 1},
                        entity_id="enterprise")
    assert r.record_key(slash) != r.record_key(one)
    assert r.record_key(make_license(P1, org="acme")).endswith("\x1facme")  # plain strings as-is
    lic = make_license(P1, org="a\x1fb")
    assert r.record_key(lic).split("\x1f")[-1] == "a\\x1fb"
    # the key's UTC date exists for every valid snapshot (9999-12-31 is the last one)
    last = make_config("run_flags", {}, snapshot_ms=253_402_300_799_999)
    assert r.record_key(last).endswith("\x1f9999-12-31")
    for ms in (253_402_300_800_000, 2**53):
        with pytest.raises(ContractViolation):
            make_config("run_flags", {}, snapshot_ms=ms)


# ---------- round-trip properties ----------

_ident = st.text("abcdefghijklmnopqrstuvwxyz0123456789_-.", min_size=1, max_size=12)
_principals = st.text("0123456789abcdef", min_size=20, max_size=20).map(lambda h: "p_" + h)
_dates = st.dates().map(lambda d: d.isoformat())
_opt = st.none() | _ident

licenses = st.builds(
    r.LicenseSnapshot,
    snapshot_date=_dates, product=st.just("github_copilot"),
    plan=st.sampled_from(r.LICENSE_PLANS), principal=_principals, team=_opt, cost_center=_opt,
    org=_opt, seat_created=_opt, pending_cancellation=_opt,
    last_activity_bucket=st.sampled_from(r.LICENSE_BUCKETS),
    last_activity_surface=st.none() | st.sampled_from(r.EDITOR_FAMILIES),
    last_authenticated_bucket=st.sampled_from((*r.LICENSE_BUCKETS, "unknown")),
    assigned_via_team=st.none() | st.booleans(), fetched_ms=st.integers(0, 2**40),
    source_kind=st.sampled_from(r.LICENSE_SOURCE_KINDS),
)
_count_keys = st.one_of(
    st.sampled_from(r.ACTIVITY_KEYS),
    st.tuples(st.sampled_from(r.ACTIVITY_KEY_PREFIXES),
              st.text("abcdefghijklmnopqrstuvwxyz0123456789._-", min_size=1, max_size=20)
              ).map("".join),
)
activity_days = st.builds(
    r.ActivityDay,
    date_utc=_dates, product=st.just("github_copilot"), principal=_principals, team=_opt,
    cost_center=_opt, reported_cost_nano=st.none() | st.integers(0, 10**12),
    counts=st.dictionaries(_count_keys, st.integers(0, r.MAX_TOKENS), max_size=6).map(
        lambda d: tuple(sorted(d.items()))),
    flags=st.lists(st.sampled_from(r.ACTIVITY_FLAGS), unique=True).map(lambda f: tuple(sorted(f))),
    fetched_ms=st.integers(0, 2**40),
)
_values = (st.none() | st.booleans() | st.integers(-(2**63) + 1, 2**63 - 1) | _ident
           | st.text("ab\\\x1f=", max_size=4))


@st.composite
def configs(draw: st.DrawFn) -> r.ConfigSnapshot:
    kind = draw(st.sampled_from(r.CONFIG_KINDS))
    keys = []
    for entry in r.CONFIG_KEYS[kind]:
        if entry.endswith((".", ":")):
            keys.append(entry + draw(_ident))
        else:
            keys.append(entry)
    chosen = draw(st.lists(st.sampled_from(keys), unique=True, max_size=5))
    entity = draw(st.sampled_from(["enterprise", "run", "admin_answers"]) | st.tuples(
        st.sampled_from(["org:", "cc:", "budget:"]), _ident).map("".join))
    return r.ConfigSnapshot(
        snapshot_ms=draw(st.integers(0, 253_402_300_799_999)),
        source_kind=draw(st.sampled_from(r.CONFIG_SOURCE_KINDS)),
        kind=kind,
        entity_id=entity,
        attrs=tuple(sorted((k, draw(_values)) for k in chosen)),
        fetched_ms=draw(st.integers(0, 2**40)),
    )


@given(st.one_of(licenses, activity_days, configs()))
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_new_records_round_trip(rec: object) -> None:
    doc = r.to_json(rec)
    json.dumps(doc)  # JSON-serializable, no float
    assert r.from_json(type(rec), json.loads(json.dumps(doc))) == rec
    assert r.record_fields(type(rec)) == frozenset(doc)
    key = r.record_key(rec)  # type: ignore[arg-type]
    assert isinstance(key, str) and key == r.record_key(r.from_json(type(rec), doc))


@given(st.one_of(licenses, activity_days, configs()), st.data())
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_new_records_mutation_fuzz(rec: object, data: st.DataObject) -> None:
    """Mutated documents decode or raise ContractViolation — never anything else."""
    doc = r.to_json(rec)
    key = data.draw(st.sampled_from(sorted(doc)))
    junk = data.draw(st.none() | st.booleans() | st.integers() | st.text(max_size=5)
                     | st.lists(st.integers(), max_size=2) | st.just({"x": 1}))
    mutated = {**doc, key: junk}
    if data.draw(st.booleans()):
        mutated["unknown_key"] = 1
    try:
        decoded = r.from_json(type(rec), mutated)
    except ContractViolation:
        return
    assert isinstance(r.record_key(decoded), str)  # every valid record has a natural key
