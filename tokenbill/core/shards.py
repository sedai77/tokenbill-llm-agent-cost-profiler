"""Fleet-scale streaming by shard (SPEC §3.21, D30; F-SEM).

Analysis runs per shard: one shard per team, or per (team, lane kind) when a team exceeds
:data:`SHARD_MAX_REQUESTS` requests in the window. Detector cross-lane logic is confined to
``(team, lane_kind, billing_class)`` cohorts, which nest inside shards, so sharded and unsharded
runs agree. Replays of disjoint lane sets add (:func:`merge_replay`); findings concatenate
(:func:`merge_findings`); Shapley runs on a seeded stratified sample (:func:`stratified_sample`).

Unattributed lanes (team ``None``; an empty team string is treated the same) form the shard
``ShardKey(team=None, …)``; :func:`shard_where` encodes it as ``{"team": ""}``, which stores must
read as "team IS NULL" (``tests/v2/sem/CONTRACT-CHANGE-F-SEM-1.md``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from fractions import Fraction

from tokenbill.common import rng
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.labels import Calibration, add
from tokenbill.core.records import Lane
from tokenbill.core.types import Finding, LaneIndexRow, ReplayResult, ShardKey

__all__ = [
    "SHARD_MAX_REQUESTS",
    "UNATTRIBUTED_TEAM",
    "finding_sort_key",
    "merge_findings",
    "merge_replay",
    "plan_shards",
    "shard_of_lanes",
    "shard_where",
    "stratified_sample",
]

SHARD_MAX_REQUESTS = 250_000
#: ``where`` value that selects lanes without a team (see the module docstring).
UNATTRIBUTED_TEAM = ""


def _team(team: str | None) -> str | None:
    return team or None


def _text(value: object) -> str:
    """The plain-str value of a lane kind / billing class (``TBEnum`` members format as their
    value), so enum- and str-valued index rows plan, group and seed identically."""
    return str(value)


def _team_order(team: str | None) -> tuple[int, str]:
    return (0, "") if team is None else (1, team)


def plan_shards(index: Iterable[LaneIndexRow], *,
                max_requests: int = SHARD_MAX_REQUESTS) -> list[ShardKey]:
    """One shard per team; a team with **more than** *max_requests* requests is split into one
    shard per lane kind (never further). Order: unattributed first, then teams by name; lane
    kinds by name. Deterministic for any order of *index*; ``ShardKey.lane_kind`` is always a
    plain str (the LaneKind value)."""
    if type(max_requests) is not int or max_requests < 1:
        raise UsageError("plan_shards: max_requests must be a positive int")
    totals: dict[str | None, int] = {}
    kinds: dict[str | None, set[str]] = {}
    for row in index:
        team = _team(row.team)
        totals[team] = totals.get(team, 0) + row.requests
        kinds.setdefault(team, set()).add(_text(row.lane_kind))
    shards: list[ShardKey] = []
    for team in sorted(totals, key=_team_order):
        if totals[team] > max_requests:
            shards.extend(ShardKey(team=team, lane_kind=kind) for kind in sorted(kinds[team]))
        else:
            shards.append(ShardKey(team=team, lane_kind=None))
    return shards


def shard_where(key: ShardKey) -> dict[str, str]:
    """The ``LedgerStore.iter_lanes(where=…)`` filter of *key*: ``team`` (``""`` = no team) and,
    for a split team, ``lane_kind``."""
    where = {"team": key.team if key.team else UNATTRIBUTED_TEAM}
    if key.lane_kind is not None:
        where["lane_kind"] = str(key.lane_kind)
    return where


def shard_of_lanes(lanes: Sequence[Lane], key: ShardKey) -> list[Lane]:
    """The lanes of *key* among *lanes* (in their given order): the in-memory equivalent of
    ``iter_lanes(where=shard_where(key))`` for tests and fakes."""
    team = _team(key.team)
    return [lane for lane in lanes
            if _team(lane.team) == team
            and (key.lane_kind is None or lane.kind.value == key.lane_kind)]


def _combine_calibration(values: Iterable[Calibration]) -> Calibration:
    seen = set(values)
    if Calibration.UNCALIBRATED in seen:
        return Calibration.UNCALIBRATED
    if Calibration.CALIBRATED in seen:
        return Calibration.CALIBRATED
    return Calibration.NA


def merge_replay(parts: Sequence[ReplayResult]) -> ReplayResult:
    """Merge replays of **disjoint** lane sets of one policy and mode.

    Figures add (``core.labels.add``: basis must match, ranges add, weaker evidence), counts add,
    ``per_lane`` / ``lanes_skipped`` / ``outcomes`` concatenate in part order (``outcomes`` is None
    when any part dropped them), assumptions are de-duplicated in first-seen order, calibration is
    UNCALIBRATED if any part is. A lane key appearing in two parts, a different policy or mode, or
    no parts raise :class:`ContractViolation`.
    """
    parts = list(parts)
    if not parts:
        raise ContractViolation("merge_replay: no parts")
    first = parts[0]
    for part in parts[1:]:
        if part.policy != first.policy:
            raise ContractViolation("merge_replay: parts replay different policies")
        if part.mode != first.mode:
            raise ContractViolation("merge_replay: parts use different modes")
    seen: set[str] = set()
    for part in parts:
        keys = [k for k, _ in part.per_lane] + [k for k, _ in part.lanes_skipped]
        for key in dict.fromkeys(keys):
            if key in seen:
                raise ContractViolation("merge_replay: lane sets are not disjoint")
            seen.add(key)
    baseline, cost, saving = first.baseline, first.cost, first.saving
    for part in parts[1:]:
        baseline = add(baseline, part.baseline)
        cost = add(cost, part.cost)
        saving = add(saving, part.saving)
    outcomes: tuple | None = ()
    for part in parts:
        if part.outcomes is None or outcomes is None:
            outcomes = None
        else:
            outcomes = outcomes + part.outcomes
    assumptions = tuple(dict.fromkeys(a for part in parts for a in part.assumptions))
    return ReplayResult(
        policy=first.policy,
        mode=first.mode,
        baseline=baseline,
        cost=cost,
        saving=saving,
        per_lane=tuple(item for part in parts for item in part.per_lane),
        outcomes=outcomes,
        assumptions=assumptions,
        calibration=_combine_calibration(part.calibration for part in parts),
        added_calls=sum(part.added_calls for part in parts),
        keepalive_pings=sum(part.keepalive_pings for part in parts),
        lanes_skipped=tuple(item for part in parts for item in part.lanes_skipped),
        n_lanes=sum(part.n_lanes for part in parts),
        n_requests=sum(part.n_requests for part in parts),
    )


def finding_sort_key(f: Finding) -> tuple[int, str, str]:
    """``(−recoverable point or 0, detector_id, finding_id)`` — the ``run_detectors`` order."""
    p50 = f.recoverable.nano if f.recoverable is not None and f.recoverable.nano is not None else 0
    return (-p50, f.detector_id, f.finding_id)


def merge_findings(parts: Iterable[Sequence[Finding]]) -> list[Finding]:
    """Concatenate per-shard findings and sort them in the ``run_detectors`` order (so the result
    does not depend on shard order or size). A ``finding_id`` seen twice raises
    :class:`ContractViolation` (finding ids are shard-independent, so a duplicate means a detector
    crossed a cohort boundary)."""
    merged: list[Finding] = []
    seen: set[str] = set()
    for part in parts:
        for finding in part:
            if finding.finding_id in seen:
                raise ContractViolation("merge_findings: duplicate finding_id across shards")
            seen.add(finding.finding_id)
            merged.append(finding)
    merged.sort(key=finding_sort_key)
    return merged


def _deciles(rows: list[LaneIndexRow]) -> dict[str, int]:
    """Spend decile (0–9) of each lane within its group, by (point_nano, lane_key) rank."""
    ranked = sorted(rows, key=lambda r: (r.point_nano, r.lane_key))
    size = len(ranked)
    return {row.lane_key: rank * 10 // size for rank, row in enumerate(ranked)}


def stratified_sample(index: Sequence[LaneIndexRow], *, n: int = 20_000,
                      seed: int = 0) -> frozenset[str]:
    """A seeded sample of *n* lane keys stratified by (lane_kind, billing_class, spend decile).

    Spend deciles are ranks of ``point_nano`` within each (lane_kind, billing_class) group;
    strata get proportional quotas (largest-remainder rule, ties by stratum key) and are sampled
    with ``common.rng(seed, …)`` over their lane keys in sorted order. The whole index is returned
    when it has at most *n* lanes. Deterministic per seed and independent of the index order.
    """
    if type(n) is not int or n < 0:
        raise UsageError("stratified_sample: n must be a non-negative int")
    rows = {row.lane_key: row for row in sorted(index, key=lambda r: r.lane_key)}
    if len(rows) <= n:
        return frozenset(rows)
    groups: dict[tuple[str, str], list[LaneIndexRow]] = {}
    for row in rows.values():
        groups.setdefault((_text(row.lane_kind), _text(row.billing_class)), []).append(row)
    strata: dict[tuple[str, str, int], list[str]] = {}
    for (kind, bclass), members in groups.items():
        for key, decile in _deciles(members).items():
            strata.setdefault((kind, bclass, decile), []).append(key)
    total = len(rows)
    order = sorted(strata)
    quotas = {s: Fraction(n * len(strata[s]), total) for s in order}
    alloc = {s: int(quotas[s]) for s in order}
    deficit = n - sum(alloc.values())
    for s in sorted(order, key=lambda s: (-(quotas[s] - alloc[s]), s))[:deficit]:
        alloc[s] += 1
    chosen: list[str] = []
    for s in order:
        members = sorted(strata[s])
        take = min(alloc[s], len(members))
        chosen.extend(rng(seed, "core.shards.stratified_sample", *s).sample(members, take))
    return frozenset(chosen)
