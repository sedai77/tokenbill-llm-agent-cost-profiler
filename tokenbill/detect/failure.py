"""Failure-path detector (SPEC §10.2 ``failure.path``, D35; package DETECT-OTHER).

:class:`FailurePath` finds money spent on failed or wasted calls, per cohort:

* ``cold-retry`` — a failed attempt (or an API_ERROR event) followed by the success starting more
  than the cache TTL ``τ`` after the first attempt (after the previous request, for the event
  path) and writing at least half of the expected prefix ``E``: the cache expired during the
  retries. ``cost_observed`` = the failed attempts' billed cost + the cold rewrite billed (EXACT;
  billing-rule ranges for uncertain attempts); ``recoverable`` = the ``repair=retry_backoff_cap``
  replay on the lanes with retried requests (``ctx.replayer``), plus the premium of the rewrite
  over a warm read for the event path (ESTIMATED, upper bound).
* ``retry-storm`` (needs ``attempts``) — more than 3 attempts for one request (SDK attempts seen
  by the recorder's HTTP hooks count, as does ``x-stainless-retry-count``) or attempts at ≥ 2
  retry layers (sdk, agent, gateway). ``cost_observed`` = the billed cost of the retries.
* ``never-succeeding-400`` — the same non-retryable error (``prompt_too_long``,
  ``thinking_binding``, ``spend_cap``, ``invalid_request``) at least twice without a success, a
  retry after ``x-should-retry: false``, or a retry after a spend-cap 429 (attempts or API_ERROR
  events). ``cost_observed`` = the billed cost of the affected attempts (usually zero).
* ``tool-error-loop`` (needs ``appended``) — ≥ 3 consecutive requests carrying ``is_error`` tool
  results. ``cost_observed`` = the billed cost of those requests.
* ``max-tokens-truncation`` — at least 3 attempts per cohort stopped at ``max_tokens`` (a
  completed, billed response: billing rules ``anthropic.max_tokens`` /
  ``openai.max_output_tokens``). ``cost_observed`` = the billed cost of the truncated attempts
  (EXACT); ``recoverable`` = the cost of those followed within 120 s by a same-lane attempt with
  ``T ≥ 0.95·T_trunc`` (a retry or continuation) — ESTIMATED upper bound, lever class hygiene.

Retry storms, never-succeeding 400s and tool-error loops are triage kinds (no recoverable). See
``detect.context`` for the shared conventions.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from collections.abc import Mapping, Sequence

from tokenbill.core.evidence import (
    MAX_TOKENS_AGENTIC_RECOMMENDED,
    MAX_TOKENS_AGENTIC_RECOMMENDED_XHIGH,
)
from tokenbill.core.findings import sum_figures
from tokenbill.core.labels import Figure
from tokenbill.core.records import (
    Attempt,
    Inference,
    InferenceKind,
    Lane,
    LaneEvent,
    LaneEventKind,
    Outcome,
    Request,
)
from tokenbill.core.types import AnalysisContext, Finding, Fix
from tokenbill.detect.context import (
    API_CACHE_DOC,
    DEFAULT_TTL_S,
    DETECTOR_VERSION,
    MISSING_KIND,
    Cohort,
    Emit,
    Money,
    Prices,
    Tally,
    applicable_levers,
    capability_notes,
    cohorts,
    combine,
    emit,
    event_attr,
    evidence_item,
    int_threshold,
    replay,
    saving_figure,
    share_threshold,
    sort_findings,
    transitions,
)

__all__ = ["NEVER_RETRY_ERRORS", "TRUNCATION_RULES", "FailurePath"]

#: Error types a retry never fixes (SPEC §10.2 never-succeeding-400).
NEVER_RETRY_ERRORS = frozenset({"prompt_too_long", "thinking_binding", "spend_cap",
                                "invalid_request"})
#: Stop reasons of a truncated response → the documented billing rule (SPEC §6.6).
TRUNCATION_RULES: Mapping[str, str] = {"max_tokens": "anthropic.max_tokens",
                                       "max_output_tokens": "openai.max_output_tokens"}
_SERVING = frozenset({InferenceKind.MESSAGE, InferenceKind.FALLBACK})
_REFS_RETRY = ("fp-ttl-from-request-start", "fp-nested-retry-amplification",
               "fp-sdk-retry-after-uncapped")
_REFS_NEVER = ("fp-never-succeeding-400",)
_REFS_LOOP = ("fp-agent-loop-prevalence",)
_REFS_TRUNC = ("max-tokens-truncation", "anth-output-hygiene")
_RETRY_TEXT = ("Give retries one owner (SDK, agent or gateway, not all three) and cap the backoff "
               "below the cache TTL minus the generation time (the SDK honors any retry-after); "
               "use a 1h TTL for watchdog and CI runs.")


def attempt_serving(att: Attempt) -> Inference | None:
    """The last MESSAGE or FALLBACK inference of an attempt."""
    for inf in reversed(att.inferences):
        if inf.kind in _SERVING:
            return inf
    return None


class FailurePath:
    """``failure.path`` (SPEC §10.2, D35). See the module docstring for the five kinds.

    Thresholds: ``failure.path.storm_attempts`` (3: a storm has more), ``failure.path.
    min_truncations`` (3), ``failure.path.retry_window_s`` (120), ``failure.path.retry_share``
    (0.95), ``failure.path.min_loop`` (3), ``failure.path.cold_write_share`` (0.5).
    """

    id = "failure.path"
    version = DETECTOR_VERSION
    kinds = ("cold-retry", "retry-storm", "never-succeeding-400", "tool-error-loop",
             "max-tokens-truncation", MISSING_KIND)
    requires = frozenset({"usage_sequence", "timing"})
    kind_requires: Mapping[str, frozenset[str]] = {"retry-storm": frozenset({"attempts"}),
                                                   "tool-error-loop": frozenset({"appended"})}

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """One finding per kind and cohort at or above ``min_usd``."""
        if not lanes:
            return capability_notes(self, ctx, self.kind_requires)
        prices = Prices(ctx.pricer)
        out: list[Finding] = []
        for cohort in cohorts(lanes, ctx):
            for found in (self._cold_retry(ctx, prices, cohort),
                          self._storm(ctx, prices, cohort),
                          self._never(ctx, prices, cohort),
                          self._loop(ctx, prices, cohort),
                          self._truncation(ctx, prices, cohort)):
                if found is not None:
                    out.append(found)
        return sort_findings(out)

    # ---------- cold-retry ----------

    def _cold_retry(self, ctx: AnalysisContext, prices: Prices,
                    cohort: Cohort) -> Finding | None:
        share = share_threshold(ctx, f"{self.id}.cold_write_share", "0.5")
        tally = Tally()
        triage = Money()
        replay_lanes: list[Lane] = []
        for lane in cohort.lanes:
            trans = {t.request_id: t for t in transitions(lane, ctx)}
            errors = [ev.ts_ms for ev in lane.events if ev.kind is LaneEventKind.API_ERROR]
            prev_ts: int | None = None
            retried_lane = False
            for req in lane.requests:
                inf = req.serving_inference
                if inf is None:
                    continue
                t = trans.get(req.request_id)
                start, last = prev_ts, req.ts_start_ms
                prev_ts = req.ts_start_ms
                if t is None or t.expected_reuse <= 0:
                    continue
                tau_ms = (t.ttl_s or DEFAULT_TTL_S) * 1000
                writes = inf.usage.cache_write
                if writes < share * t.expected_reuse:
                    continue
                first, final = req.attempts[0], req.final_attempt
                attempt_path = (len(req.attempts) >= 2 and first.outcome is not Outcome.OK
                                and final.outcome is Outcome.OK
                                and final.ts_start_ms - first.ts_start_ms > tau_ms)
                lo = start if start is not None else last
                idx = bisect_right(errors, lo)
                event_path = (not attempt_path and idx < len(errors) and errors[idx] <= last
                              and t.gap_ms > tau_ms)
                if not (attempt_path or event_path):
                    continue
                ts = final.ts_start_ms
                tokens = min(writes, t.expected_reuse)
                failed = [prices.attempt(a) for a in req.attempts[:-1]]
                rewrite = prices.written(inf.pricing, ts, inf.usage, tokens)
                premium = combine((1, rewrite),
                                  (-1, prices.line(inf.pricing, ts, "cache_read", tokens)))
                tally.hit(lane, req.ts_start_ms)
                cost = combine((1, rewrite), *((1, m) for m in failed))
                if cost is None or premium is None:
                    tally.unpriced += 1
                    continue
                tally.cost.add_money(cost)
                if attempt_path and ctx.replayer is not None:
                    retried_lane = True
                else:
                    triage.add_money(premium)
                tally.items.append(evidence_item(
                    "attempt_chain" if attempt_path else "event", req.request_id,
                    attempts=len(req.attempts), gap_ms=final.ts_start_ms - (start or last),
                    tokens=tokens, nano=cost.point))
            if retried_lane:
                replay_lanes.append(lane)
        if tally.events == tally.unpriced:
            return None
        basis = cohort.basis(ctx.pricer)
        parts: list[Figure] = []
        if replay_lanes:
            fig = saving_figure(replay(ctx, replay_lanes, "repair=retry_backoff_cap"), basis)
            if fig is not None:
                parts.append(fig)
        if triage.point or triage.ranged:
            parts.append(triage.estimate(basis, "premium of the cold rewrite over a warm read",
                                         upper_bound=True))
        recoverable = sum_figures(parts, basis) if parts else None
        spec = Emit(
            kind="cold-retry", category="failure", lever_class="cache_transform",
            title=f"Retries outlived the cache TTL in {cohort.label()} lanes",
            summary=(f"{tally.events} requests succeeded only after the prompt cache expired "
                     f"during retries and rewrote their prefix; the rewrite billed is exact."),
            references=_REFS_RETRY,
            fix=Fix(text=_RETRY_TEXT, config_patch=None, target="sdk", doc_url=API_CACHE_DOC),
            confidence="medium", lever_ids=applicable_levers("cold-retry", tally.lane_list()))
        return emit(self, ctx, cohort, tally, spec, tally.cost.billed(basis), recoverable)

    # ---------- retry-storm ----------

    def _storm(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort) -> Finding | None:
        limit = int_threshold(ctx, f"{self.id}.storm_attempts", 3)
        tally = Tally()
        layers_seen: Counter[str] = Counter()
        for lane in cohort.lanes:
            for req in lane.requests:
                counts = [a.sdk_retry_count for a in req.attempts if a.sdk_retry_count is not None]
                sdk = max(counts) if counts else 0
                n = max(len(req.attempts), sdk + 1 if counts else 0)
                layers = {a.retry_layer for a in req.attempts if a.retry_layer}
                if sdk > 0:
                    layers.add("sdk")
                if n <= limit and len(layers) < 2:
                    continue
                tally.hit(lane, req.ts_start_ms)
                cost = combine(*((1, prices.attempt(a)) for a in req.attempts[:-1]))
                if cost is None:
                    tally.unpriced += 1
                    continue
                tally.cost.add_money(cost)
                layers_seen.update(layers)
                tally.items.append(evidence_item("attempt_chain", req.request_id, attempts=n,
                                                 layers=",".join(sorted(layers)) or "none",
                                                 nano=cost.point))
        if tally.events == tally.unpriced:
            return None
        basis = cohort.basis(ctx.pricer)
        spec = Emit(
            kind="retry-storm", category="failure", lever_class="hygiene",
            title=f"Retry storms in {cohort.label()} lanes",
            summary=(f"{tally.events} requests took more than {limit} attempts or were retried at "
                     f"two or more layers (SDK, agent, gateway); the retries' billed cost is "
                     f"shown."),
            references=_REFS_RETRY,
            fix=Fix(text=_RETRY_TEXT, config_patch=None, target="sdk", doc_url=API_CACHE_DOC),
            triage=True, confidence="high",
            lever_ids=applicable_levers("retry-storm", tally.lane_list()))
        item = evidence_item("aggregate", "retry-storm:layers",
                             **{f"layer_{k}": v for k, v in sorted(layers_seen.items())},
                             requests=tally.events)
        return emit(self, ctx, cohort, tally, spec, tally.cost.billed(basis), None, [item])

    # ---------- never-succeeding-400 ----------

    @staticmethod
    def _never_request(req: Request) -> str | None:
        """Why a request's attempts never could succeed (None when they could)."""
        attempts = req.attempts
        if not any(a.outcome is Outcome.OK for a in attempts):
            same = Counter(a.error_type for a in attempts if a.error_type in NEVER_RETRY_ERRORS)
            if same and max(same.values()) >= 2:
                return "same-error"
        for att in attempts[:-1]:
            if att.should_retry is False:
                return "should-retry-false"
            if att.error_type == "spend_cap" or (att.http_status == 429
                                                 and att.error_type == "spend_cap"):
                return "spend-cap"
        return None

    @staticmethod
    def _never_events(lane: Lane) -> list[tuple[LaneEvent, int]]:
        """Runs of ≥ 2 API_ERROR events with the same never-retry error type and no successful
        request between them: ``(first event, run length)``."""
        ok_starts = sorted(r.ts_start_ms for r in lane.requests
                           if r.final_attempt.outcome is Outcome.OK
                           and r.serving_inference is not None)
        runs: list[tuple[LaneEvent, int]] = []
        current: list[LaneEvent] = []

        def flush() -> None:
            if len(current) >= 2:
                runs.append((current[0], len(current)))

        for ev in lane.events:
            if ev.kind is not LaneEventKind.API_ERROR:
                continue
            etype = event_attr(ev, "error_type")
            if etype not in NEVER_RETRY_ERRORS:
                flush()
                current = []
                continue
            if current:
                same = event_attr(current[-1], "error_type") == etype
                idx = bisect_right(ok_starts, current[-1].ts_ms)
                success_between = idx < len(ok_starts) and ok_starts[idx] <= ev.ts_ms
                if not same or success_between:
                    flush()
                    current = []
            current.append(ev)
        flush()
        return runs

    def _never(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort) -> Finding | None:
        tally = Tally()
        reasons: Counter[str] = Counter()
        for lane in cohort.lanes:
            for req in lane.requests:
                why = self._never_request(req)
                if why is None:
                    continue
                tally.hit(lane, req.ts_start_ms)
                cost = combine(*((1, prices.attempt(a)) for a in req.attempts))
                if cost is None:
                    tally.unpriced += 1
                    continue
                reasons[why] += 1
                tally.cost.add_money(cost)
                tally.items.append(evidence_item("attempt_chain", req.request_id, reason=why,
                                                 attempts=len(req.attempts), nano=cost.point))
            for ev, n in self._never_events(lane):
                tally.hit(lane, ev.ts_ms)
                reasons["events"] += 1
                tally.items.append(evidence_item("event", lane.lane_key, reason="same-error",
                                                 errors=n, ts_ms=ev.ts_ms))
        if tally.events == tally.unpriced:
            return None
        basis = cohort.basis(ctx.pricer)
        text = ("Stop retrying errors a retry cannot fix: shrink or compact prompts that are too "
                "long, strip thinking blocks that no longer bind, honor x-should-retry: false and "
                "stop at a spend cap.")
        spec = Emit(
            kind="never-succeeding-400", category="failure", lever_class="hygiene",
            title=f"Retries that can never succeed in {cohort.label()} lanes",
            summary=(f"{tally.events} requests or error runs repeated a non-retryable error "
                     f"(prompt too long, thinking binding, spend cap, invalid request) or retried "
                     f"against x-should-retry: false."),
            references=_REFS_NEVER, fix=Fix(text=text, config_patch=None, target="sdk",
                                            doc_url=None),
            triage=True, confidence="high")
        item = evidence_item("aggregate", "never-succeeding:reasons",
                             **{k.replace("-", "_"): v for k, v in sorted(reasons.items())})
        return emit(self, ctx, cohort, tally, spec, tally.cost.billed(basis), None, [item])

    # ---------- tool-error-loop ----------

    def _loop(self, ctx: AnalysisContext, prices: Prices, cohort: Cohort) -> Finding | None:
        min_loop = int_threshold(ctx, f"{self.id}.min_loop", 3)
        tally = Tally()
        for lane in cohort.lanes:
            run: list[Request] = []
            steps = [r for r in lane.requests if r.serving_inference is not None]
            for req in [*steps, None]:
                if req is not None and any(it.kind == "tool_result" and it.is_error
                                           for it in req.appended):
                    run.append(req)
                    continue
                if len(run) >= max(1, min_loop):
                    tally.hit(lane, run[0].ts_start_ms)
                    cost = combine(*((1, prices.request(r)) for r in run))
                    if cost is None:
                        tally.unpriced += 1
                    else:
                        tally.cost.add_money(cost)
                        tally.items.append(evidence_item("attempt_chain", run[0].request_id,
                                                         requests=len(run), nano=cost.point))
                run = []
        if tally.events == tally.unpriced:
            return None
        basis = cohort.basis(ctx.pricer)
        text = ("Break tool-error loops: PreToolUse guards that stop a tool after repeated "
                "failures, and a cap on consecutive failing calls in the agent loop.")
        spec = Emit(
            kind="tool-error-loop", category="failure", lever_class="hygiene",
            title=f"Tool-error loops in {cohort.label()} lanes",
            summary=(f"{tally.events} runs of {min_loop} or more consecutive calls carried failing "
                     f"tool results; their billed cost is shown."),
            references=_REFS_LOOP, fix=Fix(text=text, config_patch=None, target="code",
                                           doc_url=None),
            triage=True, confidence="medium")
        return emit(self, ctx, cohort, tally, spec, tally.cost.billed(basis), None)

    # ---------- max-tokens-truncation ----------

    def _truncation(self, ctx: AnalysisContext, prices: Prices,
                    cohort: Cohort) -> Finding | None:
        min_count = int_threshold(ctx, f"{self.id}.min_truncations", 3)
        window_ms = int_threshold(ctx, f"{self.id}.retry_window_s", 120) * 1000
        share = share_threshold(ctx, f"{self.id}.retry_share", "0.95")
        tally = Tally()
        recover = Money()
        retried = 0
        rules: Counter[str] = Counter()
        caps: Counter[int] = Counter()
        for lane in cohort.lanes:
            flat = [(req, att, attempt_serving(att)) for req in lane.requests
                    for att in req.attempts]
            flat = [x for x in flat if x[2] is not None]
            for j, (req, att, inf) in enumerate(flat):
                assert inf is not None
                if att.stop_reason not in TRUNCATION_RULES:
                    continue
                tally.hit(lane, att.ts_start_ms)
                cost = prices.attempt(att)
                if cost is None:
                    tally.unpriced += 1
                    continue
                rule = TRUNCATION_RULES[att.stop_reason] if inf.pricing.provider != "openai" \
                    else "openai.max_output_tokens"
                rules[rule] += 1
                if req.params.max_tokens is not None:
                    caps[req.params.max_tokens] += 1
                tally.cost.add_money(cost)
                t_trunc = inf.usage.total_input
                is_retried = False
                if j + 1 < len(flat):
                    _req2, att2, inf2 = flat[j + 1]
                    assert inf2 is not None
                    is_retried = (att2.ts_start_ms - att.ts_start_ms <= window_ms
                                  and inf2.usage.total_input >= share * t_trunc)
                if is_retried:
                    retried += 1
                    recover.add_money(cost)
                tally.items.append(evidence_item("attempt_chain", req.request_id,
                                                 retried="yes" if is_retried else "no",
                                                 output=inf.usage.output, nano=cost.point))
        truncated = tally.events - tally.unpriced
        if tally.events < max(1, min_count) or truncated <= 0:
            return None
        basis = cohort.basis(ctx.pricer)
        recoverable = recover.estimate(
            basis, "truncated attempts followed within the retry window by a retry or "
                   "continuation (upper bound)", upper_bound=True) if retried else None
        agentic = MAX_TOKENS_AGENTIC_RECOMMENDED.value
        xhigh = MAX_TOKENS_AGENTIC_RECOMMENDED_XHIGH.value
        assert isinstance(agentic, int) and isinstance(xhigh, int)
        common = min(caps.items(), key=lambda kv: (-kv[1], kv[0]))[0] if caps else None
        items = [evidence_item("aggregate", "truncation:rule",
                               **{r.replace(".", "_"): n for r, n in sorted(rules.items())},
                               truncated=truncated, retried=retried, max_tokens=common,
                               magnitude=0)]
        text = (f"Raise max_tokens to {agentic:,} for agentic work ({xhigh:,} at xhigh/max "
                f"effort), use stop sequences as early exits and ask for the output shape you "
                f"need.")
        rule_names = " / ".join(sorted(rules)) or "anthropic.max_tokens"
        spec = Emit(
            kind="max-tokens-truncation", category="failure", lever_class="hygiene",
            title=f"Responses truncated at max_tokens in {cohort.label()} lanes",
            summary=(f"{truncated} attempts stopped at max_tokens, a completed and billed "
                     f"response ({rule_names}); {retried} were retried or continued within "
                     f"{window_ms // 1000} s (upper bound of what raising the cap saves)."),
            references=_REFS_TRUNC,
            fix=Fix(text=text, config_patch=None, target="code", doc_url=None),
            confidence="high")
        return emit(self, ctx, cohort, tally, spec, tally.cost.billed(basis), recoverable, items)
