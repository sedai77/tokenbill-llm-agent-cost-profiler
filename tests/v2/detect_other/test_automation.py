"""``automation``: every sub-kind on hand-computed fixtures (ci-cross-run, scheduled-cadence,
batch-eligible, ci-run-cost)."""

from __future__ import annotations

from tokenbill.core.evidence import CODE_REVIEW_USD_PER_REVIEW
from tokenbill.core.labels import Evidence
from tokenbill.core.records import LaneKind, WorkloadClass
from tokenbill.detect.automation import Automation, is_ci_lane
from tokenbill.detect.context import opaque_ref

from .helpers import (
    OPUS55,
    SONNET5,
    by_kind,
    ctx,
    event,
    evidence,
    lane,
    one,
    table_replayer,
)

REPO = "h_" + "a" * 20
WORKFLOW = "h_" + "b" * 20
CI = {"workload": WorkloadClass.CI, "entrypoint": "claude-code-github-action", "repo": REPO,
      "extra": (("workflow", WORKFLOW),), "product": "claude_code"}
LOW = {"min_usd": "0.10"}


def _ci_run(i: int, start: float, *, reads: int = 0, version: str = "2.1.270"):
    rows = [(start, reads, 40_000, 0, 5, 1_000)]
    return lane(f"L-ci{i}", rows, model=SONNET5, session_key=f"s_ci{i}", team="ci-bots",
                client_version=version, **CI)


def _ci_lanes():
    return [_ci_run(0, 0), _ci_run(1, 60, version="2.1.271"), _ci_run(2, 120),
            _ci_run(3, 3_600), _ci_run(4, 3_660, reads=20_000)]


def test_ci_cross_run_rewrites_within_the_ttl() -> None:
    rep = table_replayer({"repair=shared_ci_prefix": 50_000_000})
    f = one(Automation().detect(_ci_lanes(), ctx(replayer=rep, thresholds=LOW)), "ci-cross-run")
    # runs 1 and 2 start within 300 s of the previous run and rewrite 40,000 tokens at $2.50/M;
    # run 3 starts an hour later, run 4 reads half its prompt
    assert f.cost_observed.nano == 2 * 100_000_000 and f.cost_observed.evidence is Evidence.EXACT
    assert f.n_events == 2
    assert f.recoverable.nano == 3 * 50_000_000 and f.recoverable.upper_bound
    runs = evidence(f, "ci-cross-run:runs")
    assert (runs["chained_runs"], runs["avoidable_rewrites"], runs["client_versions"]) == (3, 2, 2)
    assert f.lever_ids == ("ci.shared_prefix",) and f.fix.target == "ci"
    assert is_ci_lane(_ci_lanes()[0])


def test_ci_run_cost_per_repo_and_workflow() -> None:
    interactive = lane("L-dev", [(0, 0, 10_000, 0, 0, 500)], model=SONNET5, team="ci-bots")
    found = Automation().detect(_ci_lanes()[:3] + [interactive], ctx(thresholds=LOW))
    f = one(found, "ci-run-cost")
    # each run: 40,000 × 2,500 + 5 × 2,000 + 1,000 × 10,000 = 110,010,000
    assert f.cost_observed.nano == 3 * 110_010_000 and f.recoverable is None
    per = evidence(f, opaque_ref("ci", REPO, WORKFLOW))
    assert (per["runs"], per["p50_nano"], per["p90_nano"]) == (3, 110_010_000, 110_010_000)
    bench = evidence(f, "ci-run-cost:benchmark")
    low, high = CODE_REVIEW_USD_PER_REVIEW.value  # type: ignore[misc]
    assert (bench["benchmark_usd_low"], bench["benchmark_usd_high"]) == (str(low), str(high))
    assert bench["source"] == CODE_REVIEW_USD_PER_REVIEW.source_url
    both = evidence(f, "ci-run-cost:ci-vs-interactive")
    assert (both["ci_nano"], both["interactive_nano"]) == (330_030_000, 30_000_000)
    assert "$0.11 per run at p50" in f.summary


def test_scheduled_cadence_cold_runs() -> None:
    rows = [(t, 0, 50_000, 0, 0, 100) for t in (0, 600, 1_205, 1_800, 2_400)]
    sdk = lane("L-cron", rows, kind=LaneKind.API_RUN, product="agent_sdk",
               workload=WorkloadClass.SCHEDULED)
    rep = table_replayer({"ttl=1h@lane_kind:api_run": 600_000_000,
                          "keepalive=240s,max=3600s@lane_kind:api_run": 700_000_000})
    f = one(Automation().detect([sdk], ctx(replayer=rep, thresholds=LOW)), "scheduled-cadence")
    # four TTL-expiry rewrites of 50,000 tokens at $5/M
    assert f.cost_observed.nano == 1_000_000_000 and f.n_events == 4
    assert f.recoverable.nano == 700_000_000 and "keepalive" in f.summary
    assert set(f.lever_ids) == {"sdk.ttl", "sdk.keepalive"}
    cc = lane("L-cron-cc", rows, kind=LaneKind.API_RUN, workload=WorkloadClass.SCHEDULED)
    f = one(Automation().detect([cc], ctx(replayer=rep, thresholds=LOW)), "scheduled-cadence")
    assert f.recoverable.nano == 600_000_000           # Claude Code is never pinged
    assert one(Automation().detect([sdk], ctx()), "scheduled-cadence").recoverable is None


def test_scheduled_cadence_needs_regular_long_intervals_without_humans() -> None:
    low = ctx(thresholds={"min_usd": "0"})
    irregular = [(t, 0, 50_000, 0, 0, 100) for t in (0, 400, 2_000, 2_500, 5_000)]
    assert by_kind(Automation().detect([lane("L-i", irregular)], low), "scheduled-cadence") == []
    short = [(t, 0, 50_000, 0, 0, 100) for t in (0, 100, 200, 300, 400)]
    assert by_kind(Automation().detect([lane("L-s", short)], low), "scheduled-cadence") == []
    rows = [(t, 0, 50_000, 0, 0, 100) for t in (0, 600, 1_200, 1_800)]
    human = lane("L-h", rows, events=[event("L-h", 0, "human_prompt")])
    assert by_kind(Automation().detect([human], low), "scheduled-cadence") == []
    three = lane("L-3", rows[:3])
    assert by_kind(Automation().detect([three], low), "scheduled-cadence") == []


def _single(i: int, **kw):
    per = {k: kw.pop(k) for k in ("speed", "service_tier") if k in kw}
    return lane(f"L-b{i}", [(i * 10, 0, 0, 0, 10_000, 1_000)], model=kw.pop("model", SONNET5),
                kind=LaneKind.API_RUN, product="api", team="ci-bots",
                workload=kw.pop("workload", WorkloadClass.SERVICE),
                per_request={0: per}, **kw)


def test_batch_eligible_single_calls() -> None:
    rep = table_replayer({"batch=eligible": 15_000_000})
    lanes = [_single(0), _single(1), _single(2, workload=WorkloadClass.EVAL),
             _single(3, workload=WorkloadClass.INTERACTIVE), _single(4, service_tier="batch"),
             _single(5, model=OPUS55, speed="fast"), _single(6, entrypoint="managed-agents")]
    f = one(Automation().detect(lanes, ctx(replayer=rep, thresholds={"min_usd": "0.01"})),
            "batch-eligible")
    # three eligible lanes × (10,000 × 2,000 + 1,000 × 10,000)
    assert f.cost_observed.nano == 90_000_000 and f.n_lanes == 3
    assert f.recoverable.nano == 45_000_000 and f.lever_ids == ("batch.eligible",)
    two = lane("L-two", [(0, 0, 0, 0, 10_000, 100), (30, 0, 0, 0, 10_000, 100)],
               workload=WorkloadClass.SERVICE)
    assert by_kind(Automation().detect([two], ctx(replayer=rep, thresholds={"min_usd": "0"})),
                   "batch-eligible") == []
