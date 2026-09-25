"""Property and fuzz tests (hypothesis): random seats, activity, configuration (incl. malformed
budget / seat-count / org-settings / run-flag values), report rows, pool months and plan evidence
→ the detector never raises, conforms, is permutation invariant, names no person, keeps scenario
dims on scenario kinds only, and its findings publish through ``core.kanon`` without a privacy
error."""

from __future__ import annotations

import dataclasses
import json
import re

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.core import builders as b
from tokenbill.core import kanon
from tokenbill.core import testing as kit
from tokenbill.core.labels import Basis, Evidence, estimated
from tokenbill.core.records import LICENSE_BUCKETS, LICENSE_PLANS, LICENSE_SOURCE_KINDS, to_json
from tokenbill.detect.copilot_seats import SCENARIO_KINDS, CopilotSeatsBudgets

from .helpers import C, ctx, detect, rows

_P_RE = re.compile(r"p_[0-9a-f]{20}")
ORGS = st.sampled_from([None, "org-a", "org-b"])
TEAMS = st.sampled_from([None, "t1", "t2", "(other)"])
DATES = st.sampled_from(["2026-08-01", "2026-09-10", "2026-09-30"])
VALUES = st.one_of(st.none(), st.booleans(), st.integers(-10**12, 10**12),
                   st.sampled_from(["", "x", "0", "12.5", "1e3", "NaN", "true", "false", "-5",
                                    "enterprise", "org:org-a", "cc:data", "9" * 40]))


@st.composite
def licenses(draw: st.DrawFn) -> list:
    out = []
    for _ in range(draw(st.integers(0, 12))):
        out.append(b.make_license(
            b.make_principal(draw(st.integers(0, 7))), snapshot_date=draw(DATES),
            plan=draw(st.sampled_from(LICENSE_PLANS)), team=draw(TEAMS), org=draw(ORGS),
            cost_center=draw(st.sampled_from([None, "data"])),
            seat_created=draw(st.sampled_from([None, "2026-01-01", "2026-09-25", "garbage",
                                               "2026-02-30"])),
            pending_cancellation=draw(st.sampled_from([None, "2026-10-01"])),
            last_activity_bucket=draw(st.sampled_from(LICENSE_BUCKETS)),
            assigned_via_team=draw(st.sampled_from([None, True, False])),
            source_kind=draw(st.sampled_from(LICENSE_SOURCE_KINDS))))
    return out


@st.composite
def config(draw: st.DrawFn) -> list:
    out = []
    for i in range(draw(st.integers(0, 6))):
        attrs = {"scope": draw(st.sampled_from(["enterprise", "organization", "cost_center",
                                                "user", "multi_user_customer",
                                                "multi_user_cost_center", "repository", "odd"])),
                 "amount_nano": draw(VALUES), "prevent_further_usage": draw(VALUES),
                 "target": draw(VALUES), "team": draw(VALUES), "sku": draw(VALUES)}
        out.append(b.make_config("budget", attrs, entity_id=f"budget:{i}",
                                 source_kind="github.budgets"))
    for i in range(draw(st.integers(0, 2))):
        out.append(b.make_config("budget_users", {"n_users": draw(VALUES)},
                                 entity_id=f"budget:{i}", source_kind="github.budgets"))
    for _ in range(draw(st.integers(0, 4))):
        attrs = {k: draw(VALUES) for k in ("team", "plan", "bucket", "assigned_via_team",
                                           "pending_cancellation", "created_over_30d",
                                           "zero_cost_30d", "n")}
        attrs["bucket"] = draw(st.sampled_from([*LICENSE_BUCKETS, "*", None]))
        out.append(b.make_config("seat_counts", attrs, entity_id=draw(st.sampled_from(
            ["org:org-a", "cc:data", "enterprise"])), snapshot_ms=draw(st.sampled_from(
                [0, 1_790_000_000_000]))))
    for org in draw(st.lists(st.sampled_from(["org-a", "org-b"]), max_size=2)):
        out.append(b.make_config("org_settings", {
            "seat_management_setting": draw(st.sampled_from(
                ["assign_all", "assign_selected", "disabled", "unconfigured", "", None])),
            "plan_type": draw(st.sampled_from(["business", "enterprise", None]))},
            entity_id=f"org:{org}", source_kind=draw(st.sampled_from(
                ["github.org_copilot_settings", "tokenbill.admin_answers"]))))
    if draw(st.booleans()):
        out.append(b.make_config("cost_center", {"pool_enabled": draw(VALUES),
                                                 "pool_target_credits": draw(VALUES),
                                                 "n_users": draw(VALUES)},
                                 entity_id="cc:data", source_kind="github.cost_centers"))
    keys = ["plan.enterprise", "billing_mode.enterprise", "renewal_date.enterprise",
            "budget_stop.enterprise", "paid_usage_policy", "promo_eligible", "capped_policy.data"]
    flags = {k: draw(VALUES) for k in draw(st.lists(st.sampled_from(keys), max_size=4))}
    if flags:
        out.append(b.make_config("run_flags", flags))
    return out


@st.composite
def pools(draw: st.DrawFn) -> list:
    out = []
    for month in draw(st.lists(st.sampled_from(["2026-07", "2026-08", "2026-09"]), max_size=3,
                               unique=True)):
        unknown = draw(st.booleans())
        common = {"entity_id": draw(st.sampled_from(["enterprise", "cc:data"])),
                  "month": month, "consumed_report_nano": draw(st.integers(0, 400_000)) * C,
                  "billing_mode": draw(st.sampled_from(["metered", "volume", "unknown"])),
                  "finality": draw(st.sampled_from(["closed", "open"]))}
        if common["finality"] == "open" and draw(st.booleans()):
            point = draw(st.integers(0, 10**13))
            common["overage_forecast"] = estimated(point, Basis.LIST, low=0,
                                                   high=point + 10**12)
        if month != "2026-09" and draw(st.booleans()):
            common["promo"] = "promo:2026-06-01/2026-09-01"
        if unknown:
            out += [b.make_pool_month(seats={"unknown": "40"}, plan_scenario=s, **common)
                    for s in ("business", "enterprise")]
        else:
            out.append(b.make_pool_month(seats={"business": "30", "enterprise": "10"},
                                         **common))
    return out


@st.composite
def worlds(draw: st.DrawFn):
    lics = draw(licenses())
    act = [b.make_activity(b.make_principal(draw(st.integers(0, 7))), date_utc=draw(DATES),
                           counts={"code_generation": draw(st.integers(0, 3))},
                           flags=draw(st.sampled_from([(), ("used_chat",)])),
                           reported_cost_nano=draw(st.sampled_from([None, 0, C])))
           for _ in range(draw(st.integers(0, 5)))]
    cost = []
    for _ in range(draw(st.integers(0, 3))):
        cost += rows(draw(st.integers(0, 3000)), date=draw(DATES),
                     users=[b.make_principal(draw(st.integers(0, 7)))],
                     cost_center=draw(st.sampled_from([None, "data"])))[0]
    plans = [b.make_plan_evidence(entity_id="enterprise", month=m,
                                  plan=draw(st.sampled_from(["business", "enterprise", "mixed",
                                                             "unknown"])),
                                  seats={draw(st.sampled_from(LICENSE_PLANS)): 40})
             for m in draw(st.lists(st.sampled_from(["2026-08", "2026-09"]), max_size=2,
                                    unique=True))]
    return ctx(pools=draw(pools()), plans=plans, licenses=lics, config=draw(config()),
               activity=act, cost_lines=cost,
               today=draw(st.sampled_from([None, "2026-10-05", "2026-12-15"])),
               min_usd=draw(st.sampled_from([None, "0", "1000"])),
               reconciled=draw(st.sampled_from([(), ("github_copilot",)])))


_SETTINGS = settings(max_examples=60, deadline=None,
                     suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])


@_SETTINGS
@given(worlds())
def test_random_worlds_conform_and_never_name_people(context) -> None:
    found = kit.assert_detector_conforms(CopilotSeatsBudgets(), [], context)
    blob = json.dumps([to_json(f) for f in found], sort_keys=True)
    assert not _P_RE.search(blob)
    for f in found:
        scen = dict(f.scope.dims).get("plan_scenario")
        if f.kind not in SCENARIO_KINDS:
            assert scen is None
        elif scen is not None:
            for fig in (f.cost_observed, f.recoverable):
                if fig is not None and fig.nano is not None:
                    assert fig.evidence is Evidence.ESTIMATED
    kanon.rescope_findings(found, k=5)
    kanon.rescope_findings(found, k=5, count_users=lambda _f, _s: 7)


@_SETTINGS
@given(worlds(), st.randoms(use_true_random=False))
def test_random_worlds_permutation_invariant(context, rnd) -> None:
    fields = {}
    for name in ("pools", "plans", "licenses", "config", "activity", "cost_lines"):
        items = list(getattr(context, name))
        rnd.shuffle(items)
        fields[name] = tuple(items)
    shuffled = dataclasses.replace(context, **fields)
    assert [to_json(f) for f in detect(shuffled)] == [to_json(f) for f in detect(context)]
