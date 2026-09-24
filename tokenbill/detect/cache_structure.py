"""Cache-structure detectors (SPEC §10.2 ``cache.gateway-disabled``, ``cache.unread-write``,
``cache.cold-fanout``; package DETECT-CACHE).

* :class:`GatewayDisabled` — caching disabled or crippled on the way to the provider: (a) lanes
  that never read or write the cache although their prompts are cacheable (a gateway strips
  ``cache_control``), (b) a team configured for 1h that still writes 5m only (the ``anthropic-beta``
  header is dropped), (c) Claude Code behind a non-first-party base URL with frequent
  tools-changed/parameter misses (tool search is off behind such gateways).
* :class:`UnreadWrite` — cache writes that are never read: writes the next warm request did not
  read (breakpoint placed after the shared portion), 1h writes on lanes with no 5–60 min gap
  (``oversized-ttl``, EXACT rate arithmetic) and one-shot lanes that write (``tail-writes``, info).
* :class:`ColdFanout` — parallel lane starts in one cache scope that each paid a cold write
  because an entry is readable only after the first response token.

See ``detect.cache_miss`` for the shared conventions (cohorts, allowance labeling, ``min_usd``).
"""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction

from tokenbill.core.findings import threshold
from tokenbill.core.ids import stable_id
from tokenbill.core.labels import Figure
from tokenbill.core.records import Lane, LaneKind, Request
from tokenbill.core.types import AnalysisContext, Finding, Fix, Transition
from tokenbill.detect.cache_miss import (
    API_CACHE_DOC,
    DEFAULT_TTL_S,
    DETECTOR_VERSION,
    Cohort,
    Emit,
    Money,
    Prices,
    Tally,
    applicable_levers,
    cc_gates,
    cohorts,
    emit,
    evidence_item,
    gateway_of,
    is_claude_code,
    miss_money,
    patch,
    replay,
    saving_figure,
    serving_steps,
    settings_doc,
    sort_findings,
    transitions,
    usage_of,
)

__all__ = ["ColdFanout", "GatewayDisabled", "UnreadWrite"]

_GATEWAY_REFS = ("cc-gateway-marker-stripping", "gateway-strip", "cc-gateway-cache-strip")
_MIN_CACHE_PROMPT = 4096
_NO_CACHE_MIN_REQUESTS = 5
_BETA_MIN_REQUESTS = "20"
_TOOL_SEARCH_PER_100 = "3"
_ONE_HOUR_MS = 3_600_000


def _median(values: Sequence[int]) -> Fraction:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return Fraction(ordered[mid])
    return Fraction(ordered[mid - 1] + ordered[mid], 2)


# =============================================================================================
# cache.gateway-disabled
# =============================================================================================


class GatewayDisabled:
    """``cache.gateway-disabled`` (SPEC §10.2).

    * ``no-cache``: a lane with ≥ 5 requests, median ``T ≥ max(min_cacheable, 4,096)`` and
      Σ reads = Σ writes = 0. ``cost_observed`` = billed uncached input ``ΣU·u`` (EXACT);
      ``recoverable`` = replay ``repair=restore_caching``.
    * ``beta-header-dropped``: the team is configured for 1h (``thresholds["policy.ttl.<team>"]
      == "1h"``) but ≥ 20 of its main/API requests write 5m only. ``cost_observed`` = the TTL-expiry
      miss rewrites a working 1h TTL would have avoided (gap ≤ 1 h, EXACT); ``recoverable`` = their
      triage premium (upper bound).
    * ``tool-search-disabled``: Claude Code lanes behind a non-first-party base URL
      (``attribution.extra["gateway"]``) with ≥ 3 tools-changed or parameter-change misses per 100
      requests. Figures as for ``beta-header-dropped``; fix ``ENABLE_TOOL_SEARCH=true``.
    """

    id = "cache.gateway-disabled"
    version = DETECTOR_VERSION
    kinds = ("no-cache", "beta-header-dropped", "tool-search-disabled")
    requires = frozenset({"usage_sequence"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """Per cohort: at most one finding of each kind at or above ``min_usd``."""
        prices = Prices(ctx.pricer)
        out: list[Finding] = []
        for cohort in cohorts(lanes, ctx):
            for fn in (self._no_cache, self._beta_dropped, self._tool_search):
                found = fn(ctx, prices, cohort)
                if found is not None:
                    out.append(found)
        return sort_findings(out)

    # ---------- (a) no-cache ----------

    def _no_cache_lane(self, ctx: AnalysisContext, lane: Lane) -> bool:
        steps = serving_steps(lane)
        if len(steps) < _NO_CACHE_MIN_REQUESTS:
            return False
        for req in lane.requests:
            for inf in req.billable_inferences:
                if inf.usage.cache_read or inf.usage.cache_write:
                    return False
        first = steps[0]
        inf = first.serving_inference
        assert inf is not None
        floor = ctx.pricer.min_cacheable_tokens(inf.pricing, ts_ms=first.ts_start_ms) or 0
        totals = [usage_of(r).total_input for r in steps]
        return _median(totals) >= max(floor, _MIN_CACHE_PROMPT)

    def _no_cache(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort) -> Finding | None:
        tally = Tally()
        for lane in cohort.lanes:
            if not self._no_cache_lane(ctx, lane):
                continue
            lane_cost = 0
            unpriced = False
            for req in serving_steps(lane):
                inf = req.serving_inference
                assert inf is not None
                nano = prices.nano(inf.pricing, req.ts_start_ms, "uncached_input",
                                   inf.usage.uncached_input)
                if nano is None:
                    unpriced = True
                    break
                lane_cost += nano
            tally.hit(lane, lane.requests[0].ts_start_ms)
            if unpriced:
                tally.unpriced += 1
                continue
            tally.cost.add(lane_cost)
            tally.items.append(evidence_item("lane", lane.lane_key,
                                             requests=len(serving_steps(lane)), nano=lane_cost))
        if tally.events == tally.unpriced:
            return None
        basis = cohort.basis(ctx.pricer)
        lanes = tally.lane_list()
        cost = tally.cost.billed(basis)
        recoverable = saving_figure(replay(ctx, lanes, "repair=restore_caching"), basis)
        gateway = next((g for g in (gateway_of(lane) for lane in lanes) if g), None)
        where = " (behind a gateway)" if gateway else ""
        spec = Emit(
            kind="no-cache", category="breaker", lever_class="cache_transform",
            title=f"Prompt caching disabled on {cohort.label()} lanes{where}",
            summary=(f"{tally.events} {cohort.label()} lanes with cacheable prompts never read or "
                     f"wrote the cache: every input token was billed uncached (exact). The saving "
                     f"replays restored caching."),
            references=_GATEWAY_REFS,
            lever_ids=applicable_levers("no-cache", lanes),
            fix=Fix(text=("Forward cache_control and anthropic-beta unchanged through the "
                          "gateway and do not flatten system blocks; with LiteLLM set "
                          "cache_control_injection_points (system message and the last "
                          "message)."),
                    config_patch=None, target="gateway", doc_url=API_CACHE_DOC),
            confidence="high")
        return emit(self, ctx, cohort, tally, spec, cost, recoverable)

    # ---------- (b) beta-header-dropped ----------

    def _beta_dropped(self, ctx: AnalysisContext, prices: Prices,
                      cohort: Cohort) -> Finding | None:
        if cohort.team is None or cohort.lane_kind not in (LaneKind.MAIN.value,
                                                           LaneKind.API_RUN.value):
            return None
        configured = ctx.thresholds.get(f"policy.ttl.{cohort.team}") if ctx.thresholds else None
        if configured not in ("1h", "3600", "3600s"):
            return None
        five_only = 0
        for lane in cohort.lanes:
            for req in serving_steps(lane):
                u = usage_of(req)
                if u.cache_write_5m > 0 and u.cache_write_1h == 0:
                    five_only += 1
        minimum = int(threshold(ctx, f"{self.id}.min_5m_requests", _BETA_MIN_REQUESTS))
        if five_only < minimum:
            return None
        tally = Tally()
        for lane in cohort.lanes:
            reqs = {r.request_id: r for r in lane.requests}
            for t in transitions(lane, ctx):
                if not (t.is_miss_event and t.cause == "ttl-expiry"
                        and t.gap_ms <= _ONE_HOUR_MS):
                    continue
                self._add_miss(prices, tally, lane, reqs[t.request_id], t)
        if tally.events == tally.unpriced:
            return None
        basis = cohort.basis(ctx.pricer)
        lanes = tally.lane_list()
        spec = Emit(
            kind="beta-header-dropped", category="breaker", lever_class="cache_transform",
            title=f"1h cache TTL not in effect for {cohort.label()} lanes",
            summary=(f"The team is configured for a 1h cache TTL but {five_only} requests wrote "
                     f"5m entries only; {tally.events} idle gaps under an hour rewrote the prefix "
                     f"(exact). The gateway likely drops the anthropic-beta header."),
            references=_GATEWAY_REFS, lever_ids=applicable_levers("beta-header-dropped", lanes),
            fix=Fix(text=("Forward the anthropic-beta header unchanged through the gateway (the "
                          "1h TTL needs it); the Claude apps gateway cannot use 1h."),
                    config_patch=None, target="gateway", doc_url=API_CACHE_DOC),
            triage=True)
        return emit(self, ctx, cohort, tally, spec, tally.cost.billed(basis),
                    tally.rec.estimate(basis, "triage premium versus a warm read",
                                       upper_bound=True),
                    (evidence_item("aggregate", "beta:5m-only", requests=five_only,
                                   nano=tally.cost.point),))

    # ---------- (c) tool-search-disabled ----------

    def _tool_search(self, ctx: AnalysisContext, prices: Prices,
                     cohort: Cohort) -> Finding | None:
        behind = [lane for lane in cohort.lanes if is_claude_code(lane) and gateway_of(lane)]
        if not behind:
            return None
        tally = Tally()
        n_requests = 0
        for lane in behind:
            n_requests += len(serving_steps(lane))
            reqs = {r.request_id: r for r in lane.requests}
            for t in transitions(lane, ctx):
                if t.is_miss_event and t.cause in ("tools-changed", "param-change"):
                    self._add_miss(prices, tally, lane, reqs[t.request_id], t)
        per_100 = threshold(ctx, f"{self.id}.tool_search_misses_per_100", _TOOL_SEARCH_PER_100)
        if not tally.events or 100 * tally.events < per_100 * n_requests:
            return None
        if tally.events == tally.unpriced:
            return None
        basis = cohort.basis(ctx.pricer)
        lanes = tally.lane_list()
        key = "env.ENABLE_TOOL_SEARCH"
        spec = Emit(
            kind="tool-search-disabled", category="breaker", lever_class="cache_transform",
            title=f"Tool search likely off behind the gateway for {cohort.label()} lanes",
            summary=(f"{tally.events} tools-changed or parameter misses per {n_requests} requests "
                     f"of Claude Code behind a non-first-party base URL, where tool search is off "
                     f"by default; the rewrites billed are exact."),
            references=_GATEWAY_REFS, lever_ids=applicable_levers("tool-search-disabled", lanes),
            fix=Fix(text="Set ENABLE_TOOL_SEARCH=true in the managed env block for fleets behind "
                         "a gateway so tool definitions are deferred instead of changing the "
                         "cached prefix.",
                    config_patch=patch((key, '"true"')), target="claude-code-managed-settings",
                    doc_url=settings_doc(key), gates=cc_gates(key)),
            triage=True)
        return emit(self, ctx, cohort, tally, spec, tally.cost.billed(basis),
                    tally.rec.estimate(basis, "triage premium versus a warm read",
                                       upper_bound=True))

    @staticmethod
    def _add_miss(prices: Prices, tally: Tally, lane: Lane, req: Request,
                  t: Transition) -> None:
        tally.hit(lane, req.ts_start_ms)
        priced = miss_money(prices, t, req)
        if priced is None:
            tally.unpriced += 1
            return
        rewrite, premium = priced
        tally.cost.add_money(rewrite)
        tally.rec.add_money(premium)
        tally.items.append(evidence_item(
            "transition", req.request_id, cause=t.cause, gap_ms=t.gap_ms, tokens=t.missed,
            nano=rewrite.point))


# =============================================================================================
# cache.unread-write
# =============================================================================================

_UNREAD_REFS = ("write-without-read", "oai-cache-write-waste")
#: Causes of the next transition that already explain an unread prefix (another detector prices
#: them): an unread write is flagged only on a hit or an unexplained miss.
_PLACEMENT_CAUSES = frozenset({"hit", "unexplained"})


class UnreadWrite:
    """``cache.unread-write`` (SPEC §10.2).

    * ``write-never-read``: request ``i`` (not the last) with ``W_i > 0`` whose next request
      starts within ``τ`` yet reads ``R_j < 0.95·(R_i + W_i)`` (and no other cause explains it);
      the unread written tokens are ``min(W_i, R_i + W_i − R_j)``. ``cost_observed`` = their write
      premium over sending them uncached ``W·(wτ − u)`` (ESTIMATED). No replayed recoverable: the
      block-placement lever is linked when the requests carry fingerprints, else guidance only.
    * ``oversized-ttl``: lanes with 1h writes but no gap in (300 s, 3,600 s] — 5m would have served
      them the same. ``cost_observed`` = ``W1·(w1 − w5)`` (EXACT rate arithmetic on identical
      tokens).
    * ``tail-writes`` (info): single-request lanes that write — one-shot traffic paying the write
      premium ``W·(wτ − u)`` (ESTIMATED) for an entry nobody reads.
    """

    id = "cache.unread-write"
    version = DETECTOR_VERSION
    kinds = ("write-never-read", "oversized-ttl", "tail-writes")
    requires = frozenset({"usage_sequence", "timing"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """Per cohort: at most one finding of each kind at or above ``min_usd``."""
        prices = Prices(ctx.pricer)
        out: list[Finding] = []
        for cohort in cohorts(lanes, ctx):
            unread, oversized, tail = Tally(), Tally(), Tally()
            with_fp = False
            for lane in cohort.lanes:
                steps = serving_steps(lane)
                if len(steps) == 1:
                    self._tail(prices, tail, lane, steps[0])
                    continue
                with_fp = self._unread(ctx, prices, unread, lane, steps) or with_fp
                self._oversized(ctx, prices, oversized, lane, steps)
            for kind, tally in (("write-never-read", unread), ("oversized-ttl", oversized),
                                ("tail-writes", tail)):
                if tally.events == tally.unpriced:
                    continue
                found = self._finding(ctx, cohort, kind, tally, with_fp)
                if found is not None:
                    out.append(found)
        return sort_findings(out)

    @staticmethod
    def _premium(prices: Prices, req: Request, tokens: int) -> Money | None:
        """``tokens`` of *req*'s writes at the billed write rates minus their uncached price."""
        inf = req.serving_inference
        assert inf is not None
        written = prices.written(inf.pricing, req.ts_start_ms, inf.usage, tokens)
        uncached = prices.nano(inf.pricing, req.ts_start_ms, "uncached_input", tokens)
        if written is None or uncached is None:
            return None
        written.add(-uncached)
        return written

    def _unread(self, ctx: AnalysisContext, prices: Prices, tally: Tally, lane: Lane,
                steps: Sequence[Request]) -> bool:
        trans = {t.request_id: t for t in transitions(lane, ctx)}
        with_fp = False
        for i in range(len(steps) - 1):
            cur, nxt = steps[i], steps[i + 1]
            u = usage_of(cur)
            if u.cache_write <= 0:
                continue
            t = trans.get(nxt.request_id)
            if t is None or t.cause not in _PLACEMENT_CAUSES:
                continue
            tau = t.ttl_s if t.ttl_s is not None else DEFAULT_TTL_S
            if t.gap_ms > tau * 1000:
                continue
            prefix = u.cache_read + u.cache_write
            reads = usage_of(nxt).cache_read
            if 100 * reads >= 95 * prefix:
                continue
            tokens = min(u.cache_write, prefix - reads)
            tally.hit(lane, cur.ts_start_ms)
            with_fp = with_fp or cur.fingerprint is not None
            premium = self._premium(prices, cur, tokens)
            if premium is None:
                tally.unpriced += 1
                continue
            tally.cost.add_money(premium)
            tally.items.append(evidence_item("transition", cur.request_id, tokens=tokens,
                                             next_reads=reads, nano=premium.point))
        return with_fp

    def _oversized(self, ctx: AnalysisContext, prices: Prices, tally: Tally, lane: Lane,
                   steps: Sequence[Request]) -> None:
        if not any(usage_of(r).cache_write_1h for r in steps):
            return
        for prev, cur in zip(steps, steps[1:], strict=False):
            if 300_000 < cur.ts_start_ms - prev.ts_start_ms <= _ONE_HOUR_MS:
                return
        lane_cost = 0
        tokens = 0
        for req in steps:
            inf = req.serving_inference
            assert inf is not None
            w1 = inf.usage.cache_write_1h
            if not w1:
                continue
            hour = prices.nano(inf.pricing, req.ts_start_ms, "cache_write_1h", w1)
            five = prices.nano(inf.pricing, req.ts_start_ms, "cache_write_5m", w1)
            if hour is None or five is None:
                tally.hit(lane, req.ts_start_ms)
                tally.unpriced += 1
                return
            lane_cost += hour - five
            tokens += w1
        tally.hit(lane, steps[0].ts_start_ms)
        tally.cost.add(lane_cost)
        tally.items.append(evidence_item("lane", lane.lane_key, tokens=tokens, nano=lane_cost))

    def _tail(self, prices: Prices, tally: Tally, lane: Lane, req: Request) -> None:
        u = usage_of(req)
        if u.cache_write <= 0:
            return
        tally.hit(lane, req.ts_start_ms)
        premium = self._premium(prices, req, u.cache_write)
        if premium is None:
            tally.unpriced += 1
            return
        tally.cost.add_money(premium)
        tally.items.append(evidence_item("lane", lane.lane_key, tokens=u.cache_write,
                                         nano=premium.point))

    def _finding(self, ctx: AnalysisContext, cohort: Cohort, kind: str, tally: Tally,
                 with_fp: bool) -> Finding | None:
        basis = cohort.basis(ctx.pricer)
        lanes = tally.lane_list()
        label = cohort.label()
        levers: tuple[str, ...]
        if kind == "write-never-read":
            cost: Figure = tally.cost.estimate(
                basis, "write premium of unread tokens over sending them uncached")
            levers = applicable_levers(kind, lanes) if with_fp else ()
            spec = Emit(
                kind=kind, category="breaker",
                lever_class="cache_transform" if with_fp else "hygiene",
                title=f"Cache writes the next request did not read in {label} lanes",
                summary=(f"{tally.events} warm requests in {label} lanes read less than 95% of "
                         f"the prefix cached just before, so part of each write was never read; "
                         f"the write premium is estimated."),
                references=_UNREAD_REFS, lever_ids=levers,
                fix=Fix(text=("Place the cache breakpoint at the end of the portion that is "
                               "shared by the next request (before per-turn or volatile "
                               "content)."),
                        config_patch=None, target="code", doc_url=API_CACHE_DOC),
                triage=True)
        elif kind == "oversized-ttl":
            cost = tally.cost.billed(basis)
            levers = applicable_levers(kind, lanes)
            claude_main = cohort.lane_kind == LaneKind.MAIN.value and all(
                is_claude_code(lane) for lane in lanes)
            config = patch(("promptCacheTtl", '"5m"')) if claude_main else None
            spec = Emit(
                kind=kind, category="lever", lever_class="cache_transform",
                title=f"1h cache writes that 5m would have served in {label} lanes",
                summary=(f"{tally.events} {label} lanes wrote 1h cache entries but never idled "
                         f"between 5 and 60 minutes, so 5m entries would have served every read; "
                         f"the extra write price is exact rate arithmetic."),
                references=_UNREAD_REFS, lever_ids=levers,
                fix=Fix(text="Use the 5m TTL for bursty lanes (no 5-60 min pauses).",
                        config_patch=config,
                        target="claude-code-managed-settings" if claude_main else "sdk",
                        doc_url=settings_doc("promptCacheTtl") if claude_main else
                        API_CACHE_DOC,
                        gates=cc_gates("promptCacheTtl") if claude_main else ()),
                confidence="high", triage=True)
        else:
            cost = tally.cost.estimate(basis, "write premium over sending the prompt uncached")
            spec = Emit(
                kind=kind, category="breaker", lever_class="hygiene",
                title=f"One-shot {label} lanes writing the cache",
                summary=(f"{tally.events} single-request {label} lanes wrote cache entries that "
                         f"no later request of the lane read (info; the premium over uncached "
                         f"input is estimated)."),
                references=_UNREAD_REFS,
                fix=Fix(text="Do not set cache breakpoints on one-shot traffic unless another "
                             "request reuses the same prefix within the TTL.",
                        config_patch=None, target="code", doc_url=API_CACHE_DOC),
                triage=True)
        return emit(self, ctx, cohort, tally, spec, cost, None)


# =============================================================================================
# cache.cold-fanout
# =============================================================================================

_FANOUT_REFS = ("anth-concurrency-fanout", "cc-agent-spinup-fanout")
_FANOUT_WINDOW_MS = 10_000
_FANOUT_MIN_WRITE = 1024


class ColdFanout:
    """``cache.cold-fanout`` (SPEC §10.2): at least two lane-first requests in the same
    (cache scope, model, cwd key) — a finer key inside the cohort — starting within 10 s, each
    writing ``W ≥ 1,024`` and reading ``R < 0.5·T``: an entry is readable only after the first
    response token, so parallel starts all pay the write.

    ``cost_observed`` = ``(N − 1)·P·(w − r)`` per group with ``P`` in [min, median] of the
    first-call writes (ESTIMATED range, point at the min, upper bound); ``recoverable`` = replay
    ``repair=stagger_fanout`` on the groups' lanes (upper bound).
    """

    id = "cache.cold-fanout"
    version = DETECTOR_VERSION
    kinds = ("cold-fanout",)
    requires = frozenset({"usage_sequence", "timing"})

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """One ``cold-fanout`` finding per cohort at or above ``min_usd``."""
        prices = Prices(ctx.pricer)
        out: list[Finding] = []
        for cohort in cohorts(lanes, ctx):
            found = self._cohort(ctx, prices, cohort)
            if found is not None:
                out.append(found)
        return sort_findings(out)

    def _cohort(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort) -> Finding | None:
        keyed: dict[tuple[str, str, str], list[tuple[int, str, Lane, Request]]] = {}
        for lane in cohort.lanes:
            steps = serving_steps(lane)
            if not steps:
                continue
            first = steps[0]
            u = usage_of(first)
            if u.cache_write < _FANOUT_MIN_WRITE or 2 * u.cache_read >= u.total_input:
                continue
            key = (lane.cache_scope_key, first.model, first.attribution.cwd_key or "")
            keyed.setdefault(key, []).append((first.ts_start_ms, first.request_id, lane, first))
        tally = Tally()
        group_lanes: list[Lane] = []
        for key in sorted(keyed):
            members = sorted(keyed[key], key=lambda m: (m[0], m[1]))
            i = 0
            while i < len(members):
                j = i + 1
                while j < len(members) and members[j][0] - members[i][0] <= _FANOUT_WINDOW_MS:
                    j += 1
                if j - i >= 2:
                    self._group(prices, tally, members[i:j])
                    group_lanes.extend(m[2] for m in members[i:j])
                i = j
        if tally.events == tally.unpriced:
            return None
        basis = cohort.basis(ctx.pricer)
        cost = tally.cost.estimate(
            basis, "(N−1) parallel cold writes of the shared prefix, P in [min, median] of the "
                   "first-call writes", upper_bound=True)
        lanes = sorted(group_lanes, key=lambda lane: lane.lane_key)
        recoverable = saving_figure(replay(ctx, lanes, "repair=stagger_fanout"), basis,
                                    upper_bound=True)
        spec = Emit(
            kind="cold-fanout", category="breaker", lever_class="cache_transform",
            title=f"Parallel cold starts sharing a prefix in {cohort.label()} lanes",
            summary=(f"{tally.events} groups of {cohort.label()} lanes started within 10 s of "
                     f"each other on the same prefix and each paid a cold write (estimated "
                     f"range, upper bound)."),
            references=_FANOUT_REFS, lever_ids=applicable_levers("cold-fanout", lanes),
            fix=Fix(text="Send one request, await its first token, then start the other N−1 "
                         "(the cache entry is readable only after the first response token).",
                    config_patch=None, target="sdk", doc_url=API_CACHE_DOC))
        return emit(self, ctx, cohort, tally, spec, cost, recoverable)

    @staticmethod
    def _group(prices: Prices, tally: Tally,
               members: Sequence[tuple[int, str, Lane, Request]]) -> None:
        writes = [usage_of(m[3]).cache_write for m in members]
        n = len(members)
        low_p = min(writes)
        ordered = sorted(writes)
        high_p = ordered[(n - 1) // 2]
        first = members[0][3]
        inf = first.serving_inference
        assert inf is not None
        ts = first.ts_start_ms
        bucket = prices.write_rate_bucket(inf.usage)
        if bucket == "cache_write_unknown":
            bucket = "cache_write_5m"
        costs = []
        for p in (low_p, high_p):
            w = prices.nano(inf.pricing, ts, bucket, (n - 1) * p)
            r = prices.nano(inf.pricing, ts, "cache_read", (n - 1) * p)
            if w is None or r is None:
                tally.hit(members[0][2], ts)
                tally.unpriced += 1
                return
            costs.append(w - r)
        tally.hit(members[0][2], ts)
        for m in members[1:]:
            tally.touch(m[2])
        low, high = costs
        tally.cost.add_range(low, low, max(low, high))
        tally.items.append(evidence_item(
            "aggregate", stable_id("fan", *(m[1] for m in members)), lanes=n, p_min=low_p,
            p_median=high_p, nano=low))
