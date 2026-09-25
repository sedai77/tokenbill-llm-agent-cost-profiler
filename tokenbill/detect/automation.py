"""Automation detector (SPEC §10.2 ``automation``, D33; package DETECT-OTHER).

:class:`Automation` looks at non-interactive traffic, per cohort:

* ``ci-cross-run`` — CI runs (workload ``ci`` or entrypoint ``claude-code-github-action``) whose
  first call writes at least 80% of its prompt and reads under 10%, with another such run in the
  same cache scope and model starting within the TTL ``τ``: every later run of such a chain
  rewrote a prefix the previous run had just written. ``cost_observed`` = those first-call
  rewrites (EXACT); ``recoverable`` = ``repair=shared_ci_prefix`` (upper bound).
* ``scheduled-cadence`` — lanes with at least 4 requests at near-constant intervals (coefficient
  of variation of the gaps < 0.25) longer than ``τ``, and no human prompt: every run starts
  cold. ``cost_observed`` = the TTL-expiry rewrites billed (EXACT); ``recoverable`` = the better
  of ``ttl=1h`` and (SDK/API lanes) a keepalive replay.
* ``batch-eligible`` — single-request lanes of CI / eval / scheduled / service workloads not
  already on the batch tier, not fast, not Managed Agents (SPEC §9.3.5 predicate).
  ``cost_observed`` = their spend (EXACT); ``recoverable`` = the ``batch=eligible`` replay
  (a range from the batch cache-hit band).
* ``ci-run-cost`` (info) — per (repo, workflow) the dollars per CI run (session), p50 and p90
  (EXACT), beside the published $15–25 per Anthropic Code Review benchmark with its source, and
  the cohort's CI dollars against its interactive dollars. ``cost_observed`` = the CI spend.

Replay-based recoverables need ``ctx.replayer`` (without one the finding keeps its
``cost_observed`` and no recoverable). See ``detect.context`` for the shared conventions.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from fractions import Fraction

from tokenbill.core.evidence import CODE_REVIEW_USD_PER_REVIEW
from tokenbill.core.findings import miss_waste
from tokenbill.core.labels import Basis, Figure
from tokenbill.core.policy import to_spec
from tokenbill.core.records import Lane, LaneEventKind, WorkloadClass
from tokenbill.core.types import AnalysisContext, Finding, Fix, Policy, ReplayResult
from tokenbill.detect.context import (
    API_CACHE_DOC,
    DEFAULT_TTL_S,
    DETECTOR_VERSION,
    Cohort,
    Emit,
    Money,
    Prices,
    Tally,
    applicable_levers,
    cohorts,
    combine,
    decimal_str,
    emit,
    evidence_item,
    int_threshold,
    is_claude_code,
    lane_model,
    nearest_rank,
    opaque_ref,
    replay,
    saving_figure,
    serving,
    serving_steps,
    share_threshold,
    sort_findings,
    transitions,
    write_bucket,
)

__all__ = ["BATCH_WORKLOADS", "CI_ENTRYPOINTS", "Automation", "is_ci_lane"]

#: Entrypoints of CI traffic besides workload ``ci`` (a table).
CI_ENTRYPOINTS = frozenset({"claude-code-github-action"})
#: Workload classes the batch predicate accepts (SPEC §9.3.5).
BATCH_WORKLOADS = frozenset({WorkloadClass.CI, WorkloadClass.EVAL, WorkloadClass.SCHEDULED,
                             WorkloadClass.SERVICE})
_MANAGED_AGENTS_MARKER = "managed"
_REFS_CROSS = ("ci-cross-run-cache", "ci-headless-ingest")
_REFS_SCHED = ("sched-cadence-ttl",)
_REFS_BATCH = ("anth-batch-stacking",)
_REFS_RUN_COST = ("ci-headless-ingest", "ci-review-unit-costs")
_BATCH_DOC = "https://platform.claude.com/docs/en/build-with-claude/batch-processing"
_CV_LIMIT = "0.25"


def is_ci_lane(lane: Lane) -> bool:
    """CI traffic: workload ``ci`` or a CI entrypoint (:data:`CI_ENTRYPOINTS`) on the lane's
    first request."""
    if not lane.requests:
        return False
    attr = lane.requests[0].attribution
    return attr.workload_class is WorkloadClass.CI or attr.entrypoint in CI_ENTRYPOINTS


def _extra(lane: Lane, key: str) -> str | None:
    for k, v in lane.requests[0].attribution.extra:
        if k == key:
            return v
    return None


class Automation:
    """``automation`` (SPEC §10.2, D33). See the module docstring for the four kinds.

    Thresholds: ``automation.first_write_share`` (0.8), ``automation.first_read_share`` (0.1),
    ``automation.min_scheduled_requests`` (4), ``automation.cadence_cv`` (0.25).
    """

    id = "automation"
    version = DETECTOR_VERSION
    kinds = ("ci-cross-run", "scheduled-cadence", "batch-eligible", "ci-run-cost")
    requires = frozenset({"usage_sequence", "timing"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """One finding per kind and cohort at or above ``min_usd``."""
        prices = Prices(ctx.pricer)
        out: list[Finding] = []
        for cohort in cohorts(lanes, ctx):
            for found in (self._cross_run(ctx, prices, cohort),
                          self._scheduled(ctx, prices, cohort),
                          self._batch(ctx, prices, cohort),
                          self._run_cost(ctx, prices, cohort)):
                if found is not None:
                    out.append(found)
        return sort_findings(out)

    # ---------- ci-cross-run ----------

    def _cross_run(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort) -> Finding | None:
        write_share = share_threshold(ctx, f"{self.id}.first_write_share", "0.8")
        read_share = share_threshold(ctx, f"{self.id}.first_read_share", "0.1")
        runs: dict[tuple[str, str], list[tuple[int, Lane]]] = {}
        for lane in cohort.lanes:
            if not is_ci_lane(lane):
                continue
            steps = serving_steps(lane)
            if not steps:
                continue
            u = serving(steps[0]).usage
            total = u.total_input
            if total <= 0 or u.cache_write < write_share * total or \
                    u.cache_read >= read_share * total:
                continue
            runs.setdefault((lane.cache_scope_key, lane_model(lane)), []).append(
                (steps[0].ts_start_ms, lane))
        tally = Tally()
        chained: dict[str, Lane] = {}
        versions: set[str] = set()
        for key in sorted(runs):
            members = sorted(runs[key], key=lambda x: (x[0], x[1].lane_key))
            for i in range(1, len(members)):
                prev_ts, prev = members[i - 1]
                ts, lane = members[i]
                first_prev = serving(serving_steps(prev)[0]).usage
                tau_ms = (3600 if write_bucket(first_prev) == "cache_write_1h"
                          else DEFAULT_TTL_S) * 1000
                if ts - prev_ts > tau_ms:
                    continue
                req = serving_steps(lane)[0]
                inf = serving(req)
                chained.setdefault(prev.lane_key, prev)
                chained.setdefault(lane.lane_key, lane)
                if req.attribution.client_version:
                    versions.add(req.attribution.client_version)
                tally.hit(lane, ts)
                rewrite = prices.written(inf.pricing, ts, inf.usage, inf.usage.cache_write)
                if rewrite is None:
                    tally.unpriced += 1
                    continue
                tally.cost.add_money(rewrite)
                tally.items.append(evidence_item("aggregate", lane.lane_key,
                                                 start_gap_ms=ts - prev_ts,
                                                 tokens=inf.usage.cache_write,
                                                 nano=rewrite.point))
        if tally.events == tally.unpriced:
            return None
        basis = cohort.basis(ctx.pricer)
        chain = [chained[k] for k in sorted(chained)]
        recoverable = saving_figure(replay(ctx, [ln for ln in chain if prices.priceable(ln)],
                                           "repair=shared_ci_prefix"), basis, upper_bound=True)
        text = ("Make CI runs share one cached prefix: --exclude-dynamic-system-prompt-sections, "
                "--bare with an explicit --append-system-prompt-file, a pinned CLI version, one CI "
                "workspace, a 1h TTL for 5-60 min gaps between runs.")
        item = evidence_item("aggregate", "ci-cross-run:runs", chained_runs=len(chained),
                             avoidable_rewrites=tally.events, client_versions=len(versions),
                             magnitude=0)
        spec = Emit(
            kind="ci-cross-run", category="breaker", lever_class="cache_transform",
            title=f"CI runs rewrite the same prompt in {cohort.label()} lanes",
            summary=(f"{tally.events} CI runs started within the cache TTL of a previous run in "
                     f"the same scope and still wrote their whole first prompt "
                     f"({len(versions)} client versions seen)."),
            references=_REFS_CROSS,
            fix=Fix(text=text, config_patch=None, target="ci", doc_url=API_CACHE_DOC),
            confidence="medium", lever_ids=applicable_levers("ci-cross-run", chain))
        return emit(self, ctx, cohort, tally, spec, tally.cost.billed(basis), recoverable, [item])

    # ---------- scheduled-cadence ----------

    @staticmethod
    def _cadence(gaps: Sequence[int]) -> Fraction | None:
        """The coefficient of variation of *gaps* squared (exact), or None when empty."""
        n = len(gaps)
        if not n:
            return None
        total = sum(gaps)
        if total <= 0:
            return None
        mean = Fraction(total, n)
        var = sum((g - mean) ** 2 for g in gaps) / n
        return var / (mean * mean)

    def _scheduled(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort) -> Finding | None:
        min_requests = int_threshold(ctx, f"{self.id}.min_scheduled_requests", 4)
        cv = Fraction(share_threshold(ctx, f"{self.id}.cadence_cv", _CV_LIMIT))
        tally = Tally()
        lanes: list[Lane] = []
        intervals: list[int] = []
        for lane in cohort.lanes:
            steps = serving_steps(lane)
            if len(steps) < max(2, min_requests):
                continue
            if any(ev.kind is LaneEventKind.HUMAN_PROMPT for ev in lane.events):
                continue
            trans = transitions(lane, ctx)
            gaps = [t.gap_ms for t in trans]
            cv2 = self._cadence(gaps)
            tau_ms = max(((t.ttl_s or DEFAULT_TTL_S) for t in trans), default=DEFAULT_TTL_S) * 1000
            if cv2 is None or cv2 >= cv * cv or Fraction(sum(gaps), len(gaps)) <= tau_ms:
                continue
            reqs = {r.request_id: r for r in steps}
            lane_cost = Money()
            misses = 0
            for t in trans:
                if not t.is_miss_event or t.cause != "ttl-expiry":
                    continue
                req = reqs[t.request_id]
                inf = serving(req)
                mw, mu = miss_waste(t, req)
                rewrite = combine((1, prices.written(inf.pricing, req.ts_start_ms, inf.usage, mw)),
                                  (1, prices.line(inf.pricing, req.ts_start_ms, "uncached_input",
                                                  mu)))
                tally.hit(lane, req.ts_start_ms)
                misses += 1
                if rewrite is None:
                    tally.unpriced += 1
                    continue
                lane_cost.add_money(rewrite)
            if not misses:
                continue
            tally.cost.add_money(lane_cost)
            lanes.append(lane)
            interval = sum(gaps) // len(gaps)
            intervals.append(interval)
            tally.items.append(evidence_item("aggregate", lane.lane_key, requests=len(steps),
                                             interval_s=interval // 1000,
                                             cv_pct=decimal_str(Fraction(100) * _sqrt(cv2), 1),
                                             nano=lane_cost.point))
        if not lanes or tally.events == tally.unpriced:
            return None
        basis = cohort.basis(ctx.pricer)
        recoverable, what = self._cadence_saving(ctx, prices, cohort, lanes, basis)
        text = ("A cadence longer than the cache TTL rewrites the prompt on every run: use a 1h "
                "TTL for 5-60 min cadences, an SDK keepalive for agents, or event triggers "
                "instead of polling.")
        spec = Emit(
            kind="scheduled-cadence", category="breaker", lever_class="cache_transform",
            title=f"Scheduled runs start cold in {cohort.label()} lanes",
            summary=(f"{len(lanes)} lanes ran at near-constant intervals (median "
                     f"{nearest_rank(intervals, 50) // 1000:,} s) longer than the cache TTL with "
                     f"no human prompt; {tally.events} runs rewrote their prefix."
                     + (f" Replayed: {what}." if what else "")),
            references=_REFS_SCHED,
            fix=Fix(text=text, config_patch=None, target="sdk", doc_url=API_CACHE_DOC),
            confidence="medium", lever_ids=applicable_levers("scheduled-cadence", lanes))
        return emit(self, ctx, cohort, tally, spec, tally.cost.billed(basis), recoverable)

    @staticmethod
    def _cadence_saving(ctx: AnalysisContext, prices: Prices, cohort: Cohort,
                        lanes: Sequence[Lane], basis: Basis) -> tuple[Figure | None, str]:
        targets = [lane for lane in lanes if prices.priceable(lane)]
        selector = f"lane_kind:{cohort.lane_kind}"
        candidates = [("ttl=1h", to_spec(Policy(name="", ttl=((selector, "1h"),))), targets)]
        pingable = [lane for lane in targets if not is_claude_code(lane)]
        if pingable:
            candidates.append(("keepalive",
                               to_spec(Policy(name="", keepalive=(selector, 240, 3600))),
                               pingable))
        best: tuple[ReplayResult, str] | None = None
        for label, spec, members in candidates:
            result = replay(ctx, members, spec)
            if result is None or result.saving.nano is None or result.saving.basis is not basis:
                continue
            if best is None or result.saving.nano > (best[0].saving.nano or 0):
                best = (result, label)
        if best is None or (best[0].saving.nano or 0) <= 0:
            return None, ""
        return saving_figure(best[0], basis), best[1]

    # ---------- batch-eligible ----------

    @staticmethod
    def _batch_eligible(lane: Lane) -> bool:
        steps = serving_steps(lane)
        if len(lane.requests) != 1 or len(steps) != 1:
            return False
        req = steps[0]
        inf = serving(req)
        attr = req.attribution
        if attr.workload_class not in BATCH_WORKLOADS:
            return False
        if inf.pricing.service_tier == "batch" or inf.pricing.speed == "fast":
            return False
        return not (attr.entrypoint and _MANAGED_AGENTS_MARKER in attr.entrypoint.lower())

    def _batch(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort) -> Finding | None:
        eligible = [lane for lane in cohort.lanes if self._batch_eligible(lane)]
        if not eligible:
            return None
        tally = Tally()
        priced: list[Lane] = []
        for lane in eligible:
            req = lane.requests[0]
            tally.hit(lane, req.ts_start_ms)
            money = prices.request(req)
            if money is None:
                tally.unpriced += 1
                continue
            tally.cost.add_money(money)
            priced.append(lane)
        if tally.events == tally.unpriced:
            return None
        basis = cohort.basis(ctx.pricer)
        recoverable = saving_figure(replay(ctx, priced, "batch=eligible"), basis)
        item = evidence_item("aggregate", "batch:eligible", lanes=len(priced),
                             nano=tally.cost.point)
        text = ("Send one-shot CI, eval, scheduled and service calls through the Message Batches "
                "API (50% off every token category, cache hits best effort) or a flex tier.")
        spec = Emit(
            kind="batch-eligible", category="lever", lever_class="rate",
            title=f"Batch-eligible single calls in {cohort.label()} lanes",
            summary=(f"{len(priced)} single-request lanes of non-interactive workloads ran at the "
                     f"standard tier; the batch tier halves their price (range from the batch "
                     f"cache-hit band)."),
            references=_REFS_BATCH,
            fix=Fix(text=text, config_patch=None, target="sdk", doc_url=_BATCH_DOC),
            confidence="high", lever_ids=applicable_levers("batch-eligible", priced))
        return emit(self, ctx, cohort, tally, spec, tally.cost.billed(basis), recoverable, [item])

    # ---------- ci-run-cost ----------

    def _run_cost(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort) -> Finding | None:
        sessions: dict[str, tuple[str, Money, list[Lane]]] = {}
        interactive = Money()
        tally = Tally()
        for lane in cohort.lanes:
            money = prices.lanes([lane])
            if not is_ci_lane(lane):
                if money is not None and lane.requests[0].attribution.workload_class is \
                        WorkloadClass.INTERACTIVE:
                    interactive.add_money(money)
                continue
            tally.hit(lane, lane.requests[0].ts_start_ms)
            if money is None:
                tally.unpriced += 1
                continue
            attr = lane.requests[0].attribution
            key = opaque_ref("ci", attr.repo, _extra(lane, "workflow"))
            entry = sessions.setdefault(lane.session_key, (key, Money(), []))
            entry[1].add_money(money)
            entry[2].append(lane)
            tally.cost.add_money(money)
        if not sessions:
            return None
        per_key: dict[str, list[int]] = {}
        for key, money, _lanes in sessions.values():
            per_key.setdefault(key, []).append(money.point)
        bench = CODE_REVIEW_USD_PER_REVIEW
        low, high = bench.value if isinstance(bench.value, tuple) else (None, None)
        ranked = sorted(per_key.items(), key=lambda kv: (-sum(kv[1]), kv[0]))
        items = [evidence_item("aggregate", key, runs=len(costs),
                               p50_nano=nearest_rank(costs, 50), p90_nano=nearest_rank(costs, 90),
                               nano=sum(costs))
                 for key, costs in ranked[:18]]
        all_runs = [money.point for _, money, _ in sessions.values()]
        items.append(evidence_item(
            "aggregate", "ci-run-cost:benchmark",
            benchmark_usd_low=str(low) if low is not None else None,
            benchmark_usd_high=str(high) if high is not None else None,
            source=bench.source_url, finding=bench.finding_id, checked_on=bench.checked_on,
            magnitude=0))
        items.append(evidence_item("aggregate", "ci-run-cost:ci-vs-interactive",
                                   ci_nano=tally.cost.point, interactive_nano=interactive.point,
                                   runs=len(all_runs), magnitude=0))
        basis = cohort.basis(ctx.pricer)
        p50, p90 = nearest_rank(all_runs, 50), nearest_rank(all_runs, 90)
        text = ("Review once after the pull request is created instead of on every push; add a "
                "concurrency group with cancel-in-progress and paths filters to CI workflows.")
        spec = Emit(
            kind="ci-run-cost", category="attribution", lever_class="none",
            title=f"CI run cost in {cohort.label()} lanes",
            summary=(f"{len(all_runs)} CI runs over {len(per_key)} repo/workflow pairs: "
                     f"${_usd(p50)} per run at p50 and ${_usd(p90)} at p90 (exact), beside the "
                     f"published ${low}-{high} per Anthropic Code Review benchmark."),
            references=_REFS_RUN_COST,
            fix=Fix(text=text, config_patch=None, target="ci", doc_url=None),
            triage=True, confidence="high")
        return emit(self, ctx, cohort, tally, spec, tally.cost.billed(basis), None, items)


def _usd(nano: int) -> str:
    """Dollars with two decimals, half-even (display only)."""
    return decimal_str(Decimal(nano) / Decimal(10**9), 2)


def _sqrt(value: Fraction) -> Fraction:
    """An exact-enough square root of a non-negative Fraction (Newton, integer arithmetic at 10⁻⁹
    resolution) — for the displayed coefficient of variation only."""
    scaled = value * 10**18
    n = int(scaled)
    if n <= 0:
        return Fraction(0)
    x = n
    y = (x + n // x) // 2
    while y < x:
        x = y
        y = (x + n // x) // 2
    return Fraction(x, 10**9)
