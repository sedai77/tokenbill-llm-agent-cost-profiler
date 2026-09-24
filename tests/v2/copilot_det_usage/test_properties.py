"""Hypothesis: random Copilot billing worlds — the detector conforms, never raises, keeps the pool
rule (invoice + headroom = saving), never leaks a person and is permutation invariant; threshold
fuzzing raises only ``UsageError``."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from decimal import Decimal
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.core import builders as b
from tokenbill.core.errors import UsageError
from tokenbill.core.records import UsageBuckets, to_json
from tokenbill.core.testing import assert_detector_conforms
from tokenbill.detect.copilot_org import CopilotOrgScan

from .worlds import CREDIT, REPO, World, run

MODELS = ("Claude Opus 5.5", "Claude Opus 4.8", "Claude Opus 4.8 (fast mode)", "Claude Sonnet 5",
          "GPT-5.4", "GPT-5.5", "GPT-5.6 Sol", "Auto: Claude Haiku 4.5", "Code Review",
          "Copilot Cloud Agent", "unknown", "Gemini 3.8 Flash", "Grok 4.5", "Some Future Model")
TEAMS = ("alpha", "beta", "gamma", None)
DATES = ("2026-08-20", "2026-09-05", "2026-09-10", "2026-09-22", "2026-09-23")
_P_RE = re.compile(r"p_[0-9a-f]{20}")
SETTINGS = settings(max_examples=40, deadline=None, derandomize=True,
                    suppress_health_check=[HealthCheck.too_slow])

tokens = st.integers(min_value=0, max_value=3_000_000)
row = st.fixed_dictionaries({
    "model": st.sampled_from(MODELS), "team": st.sampled_from(TEAMS),
    "date_utc": st.sampled_from(DATES), "user": st.integers(0, 7),
    "unattributed": st.booleans(), "credits": st.integers(0, 50_000),
    "discount": st.integers(0, 100), "input_tokens": tokens, "output_tokens": tokens,
    "cache_read_tokens": tokens, "cache_write_tokens": st.integers(0, 50_000),
    "sku": st.sampled_from(("copilot_ai_credit", "coding_agent_ai_credit",
                            "copilot_premium_request")),
    "finality": st.sampled_from(("final", "provisional")), "cc": st.sampled_from((None, "cc-1")),
})
action = st.tuples(st.sampled_from(("actions_linux", "linux_16_core", "windows_8_core")),
                   st.sampled_from(("copilot_code_review", "copilot_cloud_agent",
                                    "agentic_workflow", None)),
                   st.integers(1, 5000), st.booleans())
activity = st.tuples(st.sampled_from(TEAMS), st.integers(0, 7), st.integers(0, 20),
                     st.integers(0, 20), st.integers(0, 12), st.integers(0, 40),
                     st.integers(0, 5_000_000))
pools = st.sampled_from(("none", "overage", "slack", "scenario", "open", "org"))


def build(rows: list[dict[str, Any]], actions: list, acts: list, pool_kind: str,
          extras: dict[str, Any]) -> World:
    w = World()
    for r in rows:
        credits = str(r["credits"])
        discount = str(r["credits"] * r["discount"] // 100)
        w.row(model=r["model"], team=r["team"], date_utc=r["date_utc"], credits=credits,
              discount_credits=discount, unattributed=r["unattributed"],
              principal=None if r["unattributed"] else b.make_principal(r["user"]),
              input_tokens=r["input_tokens"], output_tokens=r["output_tokens"],
              cache_read_tokens=r["cache_read_tokens"],
              cache_write_tokens=r["cache_write_tokens"], sku=r["sku"],
              finality=r["finality"], cost_center=r["cc"], repo=REPO if r["user"] < 2 else None)
    for sku, workload, minutes, in_repo in actions:
        w.actions(str(minutes), sku=sku, workload=workload, repo=REPO if in_repo else None)
    for team, user, vs, jb, mcp, cli, prompt in acts:
        counts = {"ide:vscode": vs, "ide:intellij": jb, "mcp_distinct": mcp,
                  "cli_requests": cli, "cli_prompt_tokens": prompt}
        w.activity.append(b.make_activity(b.make_principal(f"act-{user}"), team=team,
                                          counts={k: v for k, v in counts.items() if v}))
    consumed = {"overage": 3_100_000, "slack": 1_000_000}.get(pool_kind, 250_000)
    for month in ("2026-08", "2026-09"):
        if pool_kind in ("overage", "slack"):
            w.pools.append(b.make_pool_month(month=month, consumed_report_nano=consumed * CREDIT,
                                             seats={"business": "1000", "enterprise": "200"}))
        elif pool_kind == "scenario":
            w.pools.extend(b.make_pool_month(month=month, seats={"unknown": "100"},
                                             plan_scenario=s, consumed_report_nano=consumed
                                             * CREDIT) for s in ("business", "enterprise"))
        elif pool_kind == "org":
            w.pools.append(b.make_pool_month(month=month, entity_id="org:org-a",
                                             consumed_report_nano=consumed * CREDIT))
        elif pool_kind == "open":
            w.pools.append(b.make_pool_month(month=month, finality="open", regime="unknown",
                                             consumed_report_nano=consumed * CREDIT))
    if extras["compliance"]:
        w.config.append(b.make_config("run_flags", {"compliance": extras["compliance"]}))
    if extras["runs"]:
        for i in range(extras["runs"]):
            w.agg("gh_aw.run", "2026-09-12", {"channel": "github_copilot", "repo": REPO,
                                              "source": f"r{i}", "model": "claude-sonnet-5"},
                  usage=UsageBuckets(uncached_input=1000 * (i + 1), output=100))
    for state in extras["tasks"]:
        w.agg("github.agent_tasks", "2026-09-12", {"state": state}, reported=5 * CREDIT)
    return w


extras = st.fixed_dictionaries({
    "compliance": st.sampled_from((None, "none", "data_residency", "fedramp")),
    "runs": st.integers(0, 3),
    "tasks": st.lists(st.sampled_from(("failed", "completed", "timed_out")), max_size=3),
    "today": st.sampled_from(("2026-09-24", "2026-10-10", "2026-10-30", "")),
    "decisions": st.sampled_from(((), (("convention:a", "incl"),),
                                  (("convention:a", "excl"), ("convention:b", "undecidable")))),
})


@SETTINGS
@given(rows=st.lists(row, max_size=12), actions=st.lists(action, max_size=3),
       acts=st.lists(activity, max_size=8), pool_kind=pools, extras=extras)
def test_random_worlds_conform_and_keep_the_pool_rule(rows: list, actions: list, acts: list,
                                                      pool_kind: str,
                                                      extras: dict[str, Any]) -> None:
    w = build(rows, actions, acts, pool_kind, extras)
    ctx = w.ctx(today=extras["today"], min_usd="0.01", recon_decisions=extras["decisions"],
                reconciled={"github_copilot"})
    findings = assert_detector_conforms(CopilotOrgScan(), [], ctx)
    blob = json.dumps([to_json(f) for f in findings], sort_keys=True)
    assert not _P_RE.search(blob) and REPO not in blob
    pairs: dict[tuple, set[str]] = defaultdict(set)
    for f in findings:
        dims = dict(f.scope.dims)
        if "plan_scenario" in dims:
            key = (f.kind, tuple(sorted((k, v) for k, v in dims.items() if k != "plan_scenario")))
            pairs[key].add(dims["plan_scenario"])
        if f.kind == "fast-mode" and f.recoverable.nano is not None \
                and f.cost_observed.nano is not None:
            assert f.recoverable.nano + f.headroom.nano == f.cost_observed.nano
        if f.recoverable is not None and f.headroom is not None and f.recoverable.nano \
                is not None and f.recoverable.low_nano is not None:
            assert f.recoverable.low_nano <= f.recoverable.nano <= f.recoverable.high_nano
        if f.kind == "auto-adoption":
            reach = dict(next(i.attrs for i in f.evidence if i.ref == "reach"))
            assert reach["reach"] == "unknown" or 0 <= Decimal(reach["reach"]) <= 1
    assert all(v == {"business", "enterprise"} for v in pairs.values())
    if pool_kind != "scenario":
        assert not pairs
    w.lines.reverse()
    w.aggs.reverse()
    w.activity.reverse()
    again = run(w.ctx(today=extras["today"], min_usd="0.01", recon_decisions=extras["decisions"],
                      reconciled={"github_copilot"}))
    assert [to_json(f) for f in again] == [to_json(f) for f in findings]


@SETTINGS
@given(key=st.sampled_from(("min_usd", "copilot.org-scan.jetbrains_policy_share",
                            "copilot.org-scan.mcp_heavy_distinct",
                            "copilot.org-scan.mcp_heavy_share",
                            "copilot.org-scan.cli_heavy_tokens",
                            "copilot.org-scan.review_window_days")),
       value=st.one_of(st.text(max_size=12), st.sampled_from(("NaN", "1e999999", "-1", "0",
                                                              "1e-30", "Infinity"))))
def test_threshold_fuzz_raises_only_usage_error(key: str, value: str) -> None:
    w = World().rows(6, team="t", date_utc="2026-09-23", model="Claude Opus 5.5",
                     credits="100", output_tokens=1000)
    w.ide("t", 6, vscode=1, intellij=1, mcp_distinct=9, cli_requests=1, cli_prompt_tokens=10**6)
    w.row(unattributed=True, date_utc="2026-09-20", model="Code Review", credits="10")
    try:
        run(w.ctx(thresholds={key: value}))
    except UsageError:
        pass
