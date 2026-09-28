"""Runaway-tail detector (SPEC §10.2 ``tail.runaway``, §8.5, R-E13; package DETECT-OTHER).

:class:`Runaway` finds sessions in the heavy tail of their cohort ``(team, lane_kind,
billing_class)`` — the cohort statistics keep the detector shard-invariant:

* ``runaway-session`` — a session whose exact spend in some rolling hour exceeds
  ``max($50, 5 × p99)``, where p99 is the 99th percentile (nearest rank) of the maximum
  rolling-hour spend of the cohort's **other** sessions (leave-one-out, ruling R-E41: a loop is
  never its own yardstick, so small teams are covered too). Cohorts of fewer than
  :data:`MIN_COHORT_SESSIONS` (20) sessions judge no session.
* ``idle-loop`` (needs ``human_prompts``) — a session with a stretch of ≥ 50 requests spanning
  ≥ 1 hour with no HUMAN_PROMPT event.

``cost_observed`` = the session's spend above the cohort's p95 session spend (nearest rank),
summed over the flagged sessions — EXACT. No recoverable: existing gateway / Console limits are
the fix (documented, not enforced). Tail findings name the **team only** (plus the lane kind when
it is not ``main``); the session pseudonym appears only with ``ctx.break_glass`` (one finding per
session; the CLI audits the reveal, R-E13). See ``detect.context`` for the shared conventions.
"""

from __future__ import annotations

import dataclasses
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from fractions import Fraction

from tokenbill.core.records import Lane, LaneEventKind, LaneKind
from tokenbill.core.types import AnalysisContext, EvidenceItem, Finding, Fix
from tokenbill.detect.context import (
    DETECTOR_VERSION,
    HOUR_MS,
    MISSING_KIND,
    Cohort,
    Emit,
    Money,
    Prices,
    Tally,
    capability_notes,
    cohorts,
    decimal_str,
    emit,
    evidence_item,
    int_threshold,
    kind_enabled,
    nearest_rank,
    positive_threshold,
    round_fraction,
    sort_findings,
    usd_threshold_nano,
)

__all__ = ["MIN_COHORT_SESSIONS", "Runaway", "SessionStats"]

#: The minimum number of sessions of a cohort for ``runaway-session`` (R-E41).
MIN_COHORT_SESSIONS = 20

_REFS = ("runaway-circuit-breaker", "cc-heavy-tail-concentration")
_FIX = ("Set spend limits where they are enforced (gateway budgets, Console workspace limits; "
        "Token Bill documents them, it does not enforce them) and cap calls per session in the "
        "agent loop.")


@dataclasses.dataclass
class SessionStats:
    """One session of a cohort: its requests' (start, point cost), spend, lanes and idle loops."""

    key: str
    items: list[tuple[int, int]] = dataclasses.field(default_factory=list)
    spend: Money = dataclasses.field(default_factory=Money)
    lanes: dict[str, Lane] = dataclasses.field(default_factory=dict)
    unpriced: int = 0
    idle: tuple[int, int] | None = None    # (requests, duration ms) of the longest idle stretch

    def peak_hour(self) -> int:
        """The maximum spend of the session's requests starting within any rolling hour."""
        items = sorted(self.items)
        best = window = 0
        j = 0
        for i in range(len(items)):
            while j < len(items) and items[j][0] < items[i][0] + HOUR_MS:
                window += items[j][1]
                j += 1
            best = max(best, window)
            window -= items[i][1]
        return best

    def first_ms(self) -> int:
        """The session's first request start."""
        return min(ts for ts, _ in self.items) if self.items else 0

    def hours(self) -> Fraction:
        """First to last request start, in hours."""
        if not self.items:
            return Fraction(0)
        starts = [ts for ts, _ in self.items]
        return Fraction(max(starts) - min(starts), HOUR_MS)


def _p99_without(ordered: Sequence[int], value: int) -> int:
    """The nearest-rank p99 of *ordered* (ascending, at least two values) with one occurrence of
    *value* left out — a session's cohort p99 over the **other** sessions (R-E41)."""
    rank = max(1, -(-99 * (len(ordered) - 1) // 100))
    return ordered[rank - 1] if rank - 1 < bisect_left(ordered, value) else ordered[rank]


def _idle_stretch(lane: Lane) -> tuple[int, int]:
    """The longest run of requests with no HUMAN_PROMPT event since the run's first request:
    ``(requests, first-to-last start in ms)``."""
    humans = [ev.ts_ms for ev in lane.events if ev.kind is LaneEventKind.HUMAN_PROMPT]
    best = (0, 0)
    start_ts = count = 0
    prev_ts: int | None = None
    for req in lane.requests:
        ts = req.ts_start_ms
        lo = prev_ts if prev_ts is not None else -1
        idx = bisect_right(humans, lo)
        new_turn = prev_ts is None or (idx < len(humans) and humans[idx] <= ts)
        if new_turn:
            start_ts, count = ts, 0
        count += 1
        if (count, ts - start_ts) > best:
            best = (count, ts - start_ts)
        prev_ts = ts
    return best


class Runaway:
    """``tail.runaway`` (SPEC §10.2). See the module docstring.

    Thresholds: ``tail.runaway.min_hourly_usd`` (50), ``tail.runaway.p99_multiple`` (5),
    ``tail.runaway.idle_min_requests`` (50), ``tail.runaway.idle_min_s`` (3600).
    """

    id = "tail.runaway"
    version = DETECTOR_VERSION
    kinds = ("runaway-session", "idle-loop", MISSING_KIND)
    requires = frozenset({"usage_sequence", "timing"})
    kind_requires: Mapping[str, frozenset[str]] = {"idle-loop": frozenset({"human_prompts"})}

    def detect(self, lanes: Sequence[Lane], ctx: AnalysisContext) -> list[Finding]:
        """Per cohort one finding per kind (team only), or one per session with break-glass."""
        if not lanes:
            return capability_notes(self, ctx, self.kind_requires)
        prices = Prices(ctx.pricer)
        min_hourly = usd_threshold_nano(ctx, f"{self.id}.min_hourly_usd", "50")
        multiple = Fraction(positive_threshold(ctx, f"{self.id}.p99_multiple", "5"))
        idle_n = int_threshold(ctx, f"{self.id}.idle_min_requests", 50)
        idle_ms = int_threshold(ctx, f"{self.id}.idle_min_s", 3600) * 1000
        idle_on = kind_enabled(ctx, self.kind_requires, "idle-loop")
        out: list[Finding] = []
        for cohort in cohorts(lanes, ctx):
            sessions = self._sessions(prices, cohort, idle_on)
            if not sessions:
                continue
            totals = [s.spend.point for s in sessions.values()]
            peaks = {k: s.peak_hour() for k, s in sessions.items()}
            p95 = nearest_rank(totals, 95)
            ordered = sorted(peaks.values())
            limits: dict[str, tuple[int, int]] = {}     # session → (p99 of the others, limit)
            if len(sessions) >= MIN_COHORT_SESSIONS:
                for k, peak in peaks.items():
                    p99 = _p99_without(ordered, peak)
                    limits[k] = (p99, max(min_hourly, round_fraction(multiple * p99)))
            # the cohort item shows the bar of its heaviest session (None below the minimum)
            bar = limits.get(max(peaks, key=lambda k: (peaks[k], k)), (None, None))
            stats = evidence_item("aggregate", "tail:cohort", sessions=len(sessions),
                                  p95_session_nano=p95, p99_hourly_nano=bar[0],
                                  threshold_nano=bar[1], min_sessions=MIN_COHORT_SESSIONS,
                                  magnitude=0)
            runaway = [s for k, s in sorted(sessions.items())
                       if k in limits and peaks[k] > limits[k][1]]
            idle = [s for _, s in sorted(sessions.items()) if s.idle is not None
                    and s.idle[0] >= max(1, idle_n) and s.idle[1] >= idle_ms] if idle_on else []
            for kind, flagged in (("runaway-session", runaway), ("idle-loop", idle)):
                if not flagged:
                    continue
                groups = [[s] for s in flagged] if ctx.break_glass else [flagged]
                for group in groups:
                    found = self._finding(ctx, cohort, kind, group, p95, peaks, limits, stats)
                    if found is not None:
                        out.append(found)
        return sort_findings(out)

    @staticmethod
    def _sessions(prices: Prices, cohort: Cohort, idle_on: bool) -> dict[str, SessionStats]:
        sessions: dict[str, SessionStats] = {}
        for lane in cohort.lanes:
            stats = sessions.setdefault(lane.session_key, SessionStats(lane.session_key))
            stats.lanes[lane.lane_key] = lane
            for req in lane.requests:
                money = prices.request(req)
                if money is None:
                    stats.unpriced += 1
                    stats.items.append((req.ts_start_ms, 0))
                    continue
                stats.items.append((req.ts_start_ms, money.point))
                stats.spend.add_money(money)
            if idle_on:
                stretch = _idle_stretch(lane)
                if stats.idle is None or stretch > stats.idle:
                    stats.idle = stretch
        return sessions

    def _finding(self, ctx: AnalysisContext, cohort: Cohort, kind: str,
                 group: Sequence[SessionStats], p95: int, peaks: Mapping[str, int],
                 limits: Mapping[str, tuple[int, int]], stats: EvidenceItem) -> Finding | None:
        tally = Tally()
        items: list[EvidenceItem] = [stats]
        ranked = sorted(group, key=lambda s: (-s.spend.point, s.key))
        for i, session in enumerate(ranked, 1):
            above = Money()
            above.add_money(session.spend)
            floor = Money()
            floor.add(p95)
            above.add_money(floor, -1)
            if above.point <= 0:
                continue
            for lane in session.lanes.values():
                tally.touch(lane, session.first_ms())
            tally.events += 1
            tally.unpriced += 1 if session.unpriced else 0
            tally.cost.add_money(above)
            ref = session.key if ctx.break_glass else f"tail:{kind}:{i}"
            attrs: dict[str, str | int | None] = {
                "requests": len(session.items), "hours": decimal_str(session.hours(), 2),
                "session_nano": session.spend.point, "rolling_1h_max_nano": peaks[session.key],
                "nano": above.point}
            if kind == "runaway-session":
                attrs["p99_hourly_nano"], attrs["threshold_nano"] = limits[session.key]
            if kind == "idle-loop" and session.idle is not None:
                attrs["idle_requests"] = session.idle[0]
                attrs["idle_hours"] = decimal_str(Fraction(session.idle[1], HOUR_MS), 2)
            items.append(evidence_item("aggregate", ref, **attrs))
        if not tally.events:
            return None
        basis = cohort.basis(ctx.pricer)
        scope_extra: dict[str, str | None] = {}
        if cohort.lane_kind == LaneKind.MAIN.value:
            scope_extra["lane_kind"] = None
        if ctx.break_glass:
            scope_extra["session"] = group[0].key
        if kind == "runaway-session":
            what = ("spent more in a rolling hour than max($50, 5 x the p99 hourly spend of the "
                    "cohort's other sessions)")
        else:
            what = "ran a long stretch of requests (50 or more over an hour) with no human prompt"
        n = tally.events
        who = (f"One session of team {_team(cohort)}" if ctx.break_glass else
               f"{n} session{'s' if n != 1 else ''} of team {_team(cohort)}")
        spec = Emit(
            kind=kind, category="failure", lever_class="none",
            title=(f"Runaway session in team {_team(cohort)}" if kind == "runaway-session" else
                   f"Idle agent loop in team {_team(cohort)}"),
            summary=(f"{who} {what}; the spend above the cohort's p95 session spend is shown. "
                     + ("Session named under break-glass." if ctx.break_glass else
                        "Sessions are named only with --break-glass.")),
            references=_REFS, fix=Fix(text=_FIX, config_patch=None, target="gateway",
                                      doc_url=None),
            triage=True, confidence="high", scope_extra=scope_extra)
        return emit(self, ctx, cohort, tally, spec, tally.cost.billed(basis), None, items)


def _team(cohort: Cohort) -> str:
    return cohort.label().rsplit(" ", 1)[0]
