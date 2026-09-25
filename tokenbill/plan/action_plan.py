"""The ranked, overlap-aware action plan (SPEC §11.2, D16, D26, D30, D34; package PLAN).

:func:`build_action_plan` turns findings into lever results whose credits never double count:

1. **Candidates** — levers linked by findings (``Finding.lever_ids``, looked up with
   ``core.catalog.lever``, which searches the SPEC and the Copilot tables) plus the
   always-evaluated defaults (:data:`DEFAULT_LEVERS`: the TTL levers, the compaction window,
   fast mode and geo) whose selector matches some sampled lane. Copilot aggregate levers
   (``replay == "aggregate"``) are planned on the cell table by the Copilot plan and skipped here
   (CORE-AMENDMENTS A-7).
2. **Billing classes** — ``billed``, ``allowance`` (seat allowance, D26) and ``pool`` (GitHub
   Copilot pooled credits) are planned separately; allowance and pool results carry basis
   LIST_EQUIVALENT and go only to ``allowance_headroom_monthly`` / ``pool_headroom_monthly``.
3. **Grid choice** on a seeded stratified sample (``core.shards.stratified_sample``): per lever the
   grid value with the largest *documented* saving, subject to the compaction guards
   (``w ≥ context.compaction-window.min_compaction_window``, default 300,000; projected extra
   compactions per session ≤ ``context.compaction-window.max_extra_compactions``, default 3);
   ``compact-window`` grid values get ``post=`` from ``context.compaction-window.post_tokens``
   when the pipeline set it (R-E24, R-E40). A lever whose best value saves nothing is dropped.
4. **Interaction groups** — levers whose touched lane sets on the sample overlap form a connected
   component; disjoint groups add.
5. **Shapley on the sample** — per group, exact (``core.shapley.shapley_exact``) for at most
   :data:`MAX_EXACT_PLAYERS` levers, else 200 seeded permutations (``shapley_mc``) with standard
   errors. ``v(S)`` is the replayed saving of the coalition in the gated mode (calibrated when the
   model gate passed, else documented and UNCALIBRATED).
6. **Full-scope scaling** — the selected joint set is replayed once on the full scope, shard by
   shard (``map_shards`` + ``core.shards.merge_replay`` semantics), and every sample credit is
   rescaled with ``core.shapley.scale_credits`` so Σφ equals that joint saving to the nano (groups
   keep their proportions). When the scope fits in the sample, the sample *is* the full scope and
   nothing is scaled.
7–9. Monthly normalization, realization-rate priors (:mod:`tokenbill.plan.realization`) and the
   headline Σ φ·RR(p50) over billed-basis levers with the comonotone p10/p90 range, CALIBRATED only
   when every included replay was. Standalone values are shown per lever and never summed.

**Delivery scope.** A lever acts only on the lanes it is delivered to: its keyed clauses' selectors
(``ttl``, ``model``, ``effort``, ``keepalive``), else the catalog selector for clauses the grammar
cannot scope (``compact-window``, ``cold-resume``, ``fast``, ``geo``, ``regional``, ``batch``,
``repair``). Every replay therefore runs per partition of lanes by the set of levers touching them,
each partition under the combination of exactly those levers' policies, and the savings add (per
request, R-E24). Sample, joint and shard replays use the same rule, so sharded and unsharded plans
are identical.

**Levers without a joint replay.** Trade-off levers (without ``include_tradeoffs``) and the
ceiling-only ``cc.compact_on_resume`` are evaluated and shown with a sample-scaled standalone
value but no Shapley credit; levers that cannot be replayed at usage level (``replay`` ``none`` or
``block``) are shown with a projection from their findings' recoverable figures; behavioral levers
are shown, never projected. None of them enters ``joint_saving`` or a headline.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from functools import reduce

from tokenbill.core.catalog import LEVERS, LeverDef, lever
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Calibration, Evidence, Figure, add
from tokenbill.core.policy import combine, lane_matches, parse_policy, to_spec
from tokenbill.core.protocols import Replayer
from tokenbill.core.records import BILLING_CLASSES, Lane
from tokenbill.core.shapley import scale_credits, shapley_exact, shapley_mc
from tokenbill.core.shards import stratified_sample
from tokenbill.core.types import (
    ActionPlan,
    AnalysisContext,
    Finding,
    LaneIndexRow,
    LeverResult,
    Policy,
    ReplayResult,
    ShardKey,
)
from tokenbill.plan.realization import (
    DAYS_PER_MONTH,
    parse_observed_rr,
    project,
    round_half_even,
)

__all__ = [
    "CEILING_ONLY",
    "CLASS_ORDER",
    "DEFAULT_LEVERS",
    "MAX_EXACT_PLAYERS",
    "MC_PERMUTATIONS",
    "ShardJob",
    "build_action_plan",
    "lever_selectors",
    "touches",
]

#: Levers evaluated even when no finding links them (SPEC §11.2 step 1).
DEFAULT_LEVERS = ("cc.prompt_cache_ttl.main", "cc.prompt_cache_ttl.subagent", "sdk.ttl",
                  "cc.autocompact_window", "cc.fast_mode_opt_in", "geo.global")
#: Trajectory levers delivered by behavior (the SessionStart hook): evaluated for their ceiling,
#: never projected nor added to a joint set (SPEC §11.1 "ceiling only").
CEILING_ONLY = frozenset({"cc.compact_on_resume"})
#: Groups up to this size get exact Shapley values; larger groups Monte Carlo (SPEC §11.2 step 5).
MAX_EXACT_PLAYERS = 6
#: Seeded permutations of the Monte Carlo Shapley values (D16).
MC_PERMUTATIONS = 200
#: Billing classes in plan order (``core.records.BILLING_CLASSES``).
CLASS_ORDER: tuple[str, ...] = tuple(BILLING_CLASSES)

_COMPACTION = "cc.autocompact_window"
_T_MIN_WINDOW = "context.compaction-window.min_compaction_window"
_T_MAX_EXTRA = "context.compaction-window.max_extra_compactions"
_T_POST = "context.compaction-window.post_tokens"
_MIN_WINDOW_DEFAULT = 300_000
_MAX_EXTRA_DEFAULT = "3"
_REPLAYABLE = frozenset({"usage"})
_UNREPLAYED = frozenset({"none", "block"})
_UNPRICED_REPLAY = ("unpriced: a replay this lever depends on changes a request without a priced "
                    "rate row (R2)")


class _Unpriced(Exception):
    """A coalition value is unpriced (R2, R-E26): the group's credits cannot be computed."""


# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------


def _text(value: object) -> str:
    return str(value)


def _class_basis(cls: str, ctx: AnalysisContext) -> Basis:
    if cls in ("allowance", "pool"):
        return Basis.LIST_EQUIVALENT
    basis = getattr(ctx.pricer, "basis", Basis.LIST)
    return basis if basis in (Basis.LIST, Basis.CONTRACT) else Basis.LIST


def _no_value(reason: str, basis: Basis) -> Figure:
    """A figure that deliberately carries no number (rendered "unpriced" with its reason)."""
    note = reason if reason.startswith("unpriced:") else f"unpriced: {reason}"
    return Figure(nano=None, evidence=Evidence.ESTIMATED, basis=basis, note=note)


def _estimate(nano: int, basis: Basis, calibration: Calibration, upper: bool,
              note: str) -> Figure:
    return Figure(nano=nano, evidence=Evidence.ESTIMATED, basis=basis, calibration=calibration,
                  upper_bound=upper, note=note)


def _combine_calibration(values: Iterable[Calibration]) -> Calibration:
    seen = set(values)
    if Calibration.UNCALIBRATED in seen:
        return Calibration.UNCALIBRATED
    if Calibration.CALIBRATED in seen:
        return Calibration.CALIBRATED
    return Calibration.NA


def _threshold(ctx: AnalysisContext, key: str) -> Decimal | None:
    raw = ctx.thresholds.get(key) if ctx.thresholds else None
    if raw is None:
        return None
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise UsageError(f"threshold {key}: not a decimal") from None
    if not value.is_finite() or value < 0 or value > 2**53:
        raise UsageError(f"threshold {key}: must be a number in [0, 2**53]")
    return value


def _finding_class(f: Finding) -> str:
    dims = dict(f.scope.dims)
    cls = dims.get("billing_class")
    if cls in BILLING_CLASSES:
        return cls
    if dims.get("product") == "copilot":
        return "pool"
    if f.cost_observed.basis is Basis.LIST_EQUIVALENT:
        return "allowance"
    return "billed"


def _group_sort_key(ids: Collection[str]) -> str:
    return min(ids)


# ---------------------------------------------------------------------------------------------
# delivery scope
# ---------------------------------------------------------------------------------------------


def lever_selectors(ldef: LeverDef, policy: Policy) -> tuple[str, ...]:
    """The selectors that decide which lanes *policy* (a grid value of *ldef*) is delivered to:
    the keyed clauses' selectors (``ttl``, ``model``, ``effort``, ``keepalive``) when there are
    any, else the catalog selector."""
    sels = [sel for sel, _ in policy.ttl]
    sels += [sel for sel, _ in policy.model_remap]
    sels += [sel for sel, _level, _scale in policy.effort]
    if policy.keepalive is not None:
        sels.append(policy.keepalive[0])
    if not sels:
        sels = [ldef.selector]
    return tuple(dict.fromkeys(sels))


def touches(selectors: Sequence[str], lane: Lane) -> bool:
    """True when any of *selectors* matches *lane* (``core.policy.lane_matches``, the rule the
    replayer applies)."""
    return any(lane_matches(sel, lane) for sel in selectors)


@dataclass(frozen=True)
class _Member:
    """A lever at its chosen grid value: the unit of every combined replay."""

    lever_id: str
    policy: Policy
    selectors: tuple[str, ...]

    @property
    def key(self) -> tuple[str, str]:
        return (self.lever_id, to_spec(self.policy))


@dataclass
class _Outcome:
    saving: Figure
    added_calls: int
    touched: int


def _normalized_note(figs: Sequence[Figure], total: Figure) -> str:
    """The note of a sum of replay figures: its distinct parts sorted (unpriced reasons first),
    so the sum does not depend on the order or number of parts (shards, partitions)."""
    parts = sorted({p for f in figs for p in f.note.split("; ") if p})
    unpriced = [p for p in parts if p.startswith("unpriced:")]
    rest = [p for p in parts if not p.startswith("unpriced:")]
    if total.nano is None:
        return "; ".join(unpriced + rest) or "unpriced: replay"
    return "; ".join(rest + unpriced)


def _sum_figures(figs: Sequence[Figure], basis: Basis) -> Figure:
    """Σ of replay saving figures (``core.labels.add``) with a normalized note; empty → EXACT 0."""
    if not figs:
        return Figure(nano=0, evidence=Evidence.EXACT, basis=basis)
    total = reduce(add, figs)
    return dataclasses.replace(total, note=_normalized_note(figs, total))


def _replay_members(replayer: Replayer, lanes: Sequence[Lane], members: Sequence[_Member], *,
                    mode: str, ctx: AnalysisContext, basis: Basis,
                    touched: Sequence[frozenset[str]] | None = None) -> _Outcome:
    """Replay *lanes* with each lane under the combination of the *members* touching it; lanes no
    member touches are not replayed (they save exactly 0, R-E24). Savings add. *touched* (one
    lane-key set per member) short-cuts the selector evaluation."""
    if touched is None:
        touched = [frozenset(ln.lane_key for ln in lanes if touches(m.selectors, ln))
                   for m in members]
    parts: dict[tuple[int, ...], list[Lane]] = {}
    for lane in lanes:
        idx = tuple(i for i, keys in enumerate(touched) if lane.lane_key in keys)
        if idx:
            parts.setdefault(idx, []).append(lane)
    savings: list[Figure] = []
    added = 0
    n_touched = 0
    floor = dict(ctx.static_prefix_floor) if ctx.static_prefix_floor else None
    for idx in sorted(parts):
        policy = reduce(combine, (members[i].policy for i in idx))
        part = sorted(parts[idx], key=lambda ln: ln.lane_key)
        result: ReplayResult = replayer.replay(
            part, policy, mode=mode, pricer=ctx.pricer, rules=ctx.rules,
            calibration=ctx.calibration, static_prefix_floor=floor)
        savings.append(result.saving)
        added += result.added_calls
        n_touched += len(part)
    return _Outcome(saving=_sum_figures(savings, basis), added_calls=added, touched=n_touched)


@dataclass
class ShardJob:
    """One shard's part of the full-scope joint replay (SPEC §11.2 step 6), as a picklable
    callable for ``map_shards``: loads the shard's lanes with *load_lanes* and replays, per
    billing class, that class's lanes under its joint members (delivery-scoped, see the module
    docstring). Returns ``{billing class: (saving Figure, lanes replayed)}``."""

    load_lanes: Callable[[Collection[str] | None, ShardKey | None], Sequence[Lane]]
    members: Mapping[str, tuple[_Member, ...]]
    mode: str
    ctx: AnalysisContext
    bases: Mapping[str, Basis]

    def __call__(self, shard: ShardKey) -> dict[str, tuple[Figure, int]]:
        """Replay one shard (see the class docstring)."""
        lanes = list(self.load_lanes(None, shard))
        replayer = self.ctx.replayer
        assert replayer is not None
        out: dict[str, tuple[Figure, int]] = {}
        for cls, members in self.members.items():
            mine = [ln for ln in lanes if ln.billing_class == cls]
            if not mine or not members:
                continue
            outcome = _replay_members(replayer, mine, members, mode=self.mode, ctx=self.ctx,
                                      basis=self.bases[cls])
            out[cls] = (outcome.saving, outcome.touched)
        return out


def _sequential(fn: Callable[[ShardKey], object], shards: Sequence[ShardKey]) -> list:
    return [fn(shard) for shard in shards]


# ---------------------------------------------------------------------------------------------
# the planner
# ---------------------------------------------------------------------------------------------


@dataclass
class _Chosen:
    ldef: LeverDef
    member: _Member
    documented: _Outcome
    finding_ids: tuple[str, ...]


@dataclass
class _ClassPlan:
    cls: str
    basis: Basis
    results: list[LeverResult] = field(default_factory=list)
    joint: Figure | None = None
    headline: Figure | None = None
    groups: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    se: dict[str, int] = field(default_factory=dict)
    mc: bool = False


class _Planner:
    def __init__(self, findings: Sequence[Finding], index: Sequence[LaneIndexRow],
                 load_lanes: Callable[[Collection[str] | None, ShardKey | None], Sequence[Lane]],
                 shards: Sequence[ShardKey], ctx: AnalysisContext, *, window_days: int,
                 include_tradeoffs: bool, seed: int, sample_lanes: int,
                 map_shards: Callable[[Callable[[ShardKey], object], Sequence[ShardKey]], list]
                 ) -> None:
        self.findings = list(findings)
        self.index = {row.lane_key: row for row in index}
        self.load_lanes = load_lanes
        self.shards = list(shards)
        self.ctx = ctx
        self.window_days = window_days
        self.include_tradeoffs = include_tradeoffs
        self.seed = seed
        self.sample_lanes = sample_lanes
        self.map_shards = map_shards
        self.replayer = ctx.replayer
        cal = ctx.calibration
        self.mode = "calibrated" if cal is not None and cal.status == "pass" else "documented"
        self.memo: dict[tuple, _Outcome] = {}
        self.touch_sets: dict[tuple, frozenset[str]] = {}

    # ----- inputs -----

    def _linked(self) -> tuple[list[LeverDef], dict[tuple[str, str], list[str]]]:
        """Candidate lever definitions (catalog order, then Copilot order) and finding ids per
        (lever, class)."""
        by_id: dict[str, LeverDef] = {}
        links: dict[tuple[str, str], list[str]] = {}
        for lever_id in DEFAULT_LEVERS:
            by_id[lever_id] = lever(lever_id)
        for f in self.findings:
            cls = _finding_class(f)
            for lever_id in f.lever_ids:
                try:
                    ldef = lever(lever_id)
                except UsageError:
                    continue   # not a catalog lever: nothing to plan
                if ldef.replay == "aggregate":
                    continue   # Copilot aggregate levers: planned on the cell table (A-7)
                by_id[lever_id] = ldef
                ids = links.setdefault((lever_id, cls), [])
                if f.finding_id not in ids:
                    ids.append(f.finding_id)
        order = {lv.lever_id: i for i, lv in enumerate(LEVERS)}
        ordered = sorted(by_id.values(),
                         key=lambda lv: (order.get(lv.lever_id, len(order)), lv.lever_id))
        return ordered, links

    def _grid_policies(self, ldef: LeverDef) -> list[Policy]:
        post = _threshold(self.ctx, _T_POST)
        out = []
        for spec in ldef.grid:
            policy = parse_policy(spec)
            cw = policy.compaction_window
            if cw is not None and cw[1] is None and post is not None and int(post) > 0:
                policy = parse_policy(to_spec(dataclasses.replace(
                    policy, compaction_window=(cw[0], int(post)))))
            out.append(policy)
        return out

    # ----- replays -----

    def _touched(self, lanes: Sequence[Lane], member: _Member) -> frozenset[str]:
        key = (id(lanes), member.key)
        got = self.touch_sets.get(key)
        if got is None:
            got = frozenset(ln.lane_key for ln in lanes if touches(member.selectors, ln))
            self.touch_sets[key] = got
        return got

    def _value(self, cls: str, lanes: Sequence[Lane], members: Sequence[_Member],
               mode: str) -> _Outcome:
        key = (cls, mode, frozenset(m.key for m in members))
        got = self.memo.get(key)
        if got is None:
            assert self.replayer is not None
            ordered = sorted(members, key=lambda m: m.lever_id)
            got = _replay_members(self.replayer, lanes, ordered, mode=mode, ctx=self.ctx,
                                  basis=self._basis(cls),
                                  touched=[self._touched(lanes, m) for m in ordered])
            self.memo[key] = got
        return got

    def _basis(self, cls: str) -> Basis:
        return _class_basis(cls, self.ctx)

    def _guard_ok(self, ldef: LeverDef, policy: Policy, outcome: _Outcome,
                  lanes: Sequence[Lane], selectors: Sequence[str]) -> bool:
        cw = policy.compaction_window
        if ldef.lever_id != _COMPACTION or cw is None:
            return True
        min_window = _threshold(self.ctx, _T_MIN_WINDOW)
        min_w = int(min_window) if min_window is not None else _MIN_WINDOW_DEFAULT
        if cw[0] < min_w:
            return False
        max_extra = _threshold(self.ctx, _T_MAX_EXTRA)
        limit = Fraction(max_extra if max_extra is not None else Decimal(_MAX_EXTRA_DEFAULT))
        sessions = {ln.session_key for ln in lanes if touches(selectors, ln)}
        if not sessions:
            return False
        return Fraction(outcome.added_calls, len(sessions)) <= limit

    def _choose(self, cls: str, ldef: LeverDef, lanes: Sequence[Lane],
                finding_ids: tuple[str, ...]) -> _Chosen | None:
        """The grid value with the largest positive documented saving on the sample, or None."""
        best: tuple[int, int, _Member, _Outcome] | None = None
        for rank, policy in enumerate(self._grid_policies(ldef)):
            selectors = lever_selectors(ldef, policy)
            if not any(touches(selectors, ln) for ln in lanes):
                continue
            member = _Member(ldef.lever_id, policy, selectors)
            outcome = self._value(cls, lanes, (member,), "documented")
            nano = outcome.saving.nano
            if nano is None or nano <= 0:
                continue
            if not self._guard_ok(ldef, policy, outcome, lanes, selectors):
                continue
            if best is None or (nano, -rank) > (best[0], -best[1]):
                best = (nano, rank, member, outcome)
        if best is None:
            return None
        return _Chosen(ldef, best[2], best[3], finding_ids)

    # ----- groups and Shapley -----

    @staticmethod
    def _groups(chosen: Sequence[_Chosen], lanes: Sequence[Lane]) -> list[list[_Chosen]]:
        touched = {c.ldef.lever_id: {ln.lane_key for ln in lanes
                                     if touches(c.member.selectors, ln)} for c in chosen}
        parent = {c.ldef.lever_id: c.ldef.lever_id for c in chosen}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        ids = sorted(parent)
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if touched[a] & touched[b]:
                    ra, rb = find(a), find(b)
                    if ra != rb:
                        parent[max(ra, rb)] = min(ra, rb)
        comps: dict[str, list[_Chosen]] = {}
        for c in chosen:
            comps.setdefault(find(c.ldef.lever_id), []).append(c)
        return [sorted(g, key=lambda c: c.ldef.lever_id)
                for _, g in sorted(comps.items(), key=lambda kv: _group_sort_key(
                    [c.ldef.lever_id for c in kv[1]]))]

    def _shapley(self, cls: str, lanes: Sequence[Lane], group: Sequence[_Chosen]
                 ) -> tuple[dict[str, int], dict[str, int], bool, list[_Outcome]]:
        members = {c.ldef.lever_id: c.member for c in group}
        seen: list[_Outcome] = []

        def value(coalition: frozenset[str]) -> int:
            if not coalition:
                return 0
            outcome = self._value(cls, lanes, tuple(members[i] for i in sorted(coalition)),
                                  self.mode)
            seen.append(outcome)
            if outcome.saving.nano is None:
                raise _Unpriced
            return outcome.saving.nano

        players = sorted(members)
        if len(players) <= MAX_EXACT_PLAYERS:
            return shapley_exact(players, value), {}, False, seen
        credits, errors = shapley_mc(players, value, permutations=MC_PERMUTATIONS, seed=self.seed)
        return credits, errors, True, seen

    # ----- one billing class -----

    def plan_class(self, cls: str, lanes: Sequence[Lane], candidates: Sequence[LeverDef],
                   links: Mapping[tuple[str, str], list[str]], sample_is_full: bool,
                   spend_factor: Fraction, n_sample: int, n_full: int) -> _ClassPlan:
        basis = self._basis(cls)
        out = _ClassPlan(cls=cls, basis=basis)
        joint: list[_Chosen] = []
        evaluated: list[_Chosen] = []
        unreplayed: list[tuple[LeverDef, tuple[str, ...]]] = []
        for ldef in candidates:
            fids = tuple(links.get((ldef.lever_id, cls), ()))
            if ldef.lever_class == "behavioral" or ldef.replay in _UNREPLAYED:
                if fids:
                    unreplayed.append((ldef, fids))
                continue
            if ldef.replay not in _REPLAYABLE or self.replayer is None or not lanes:
                continue
            chosen = self._choose(cls, ldef, lanes, fids)
            if chosen is None:
                continue
            if ldef.lever_id in CEILING_ONLY or (ldef.tradeoff and not self.include_tradeoffs):
                evaluated.append(chosen)
            else:
                joint.append(chosen)
        groups = self._groups(joint, lanes)
        credits: dict[str, int] = {}
        unpriced = False
        calibrations: list[Calibration] = []
        uppers: dict[str, bool] = {}
        group_of: dict[str, str] = {}
        methods: dict[str, str] = {}
        for gi, group in enumerate(groups, start=1):
            gid = f"{cls}:g{gi}"
            ids = tuple(c.ldef.lever_id for c in group)
            out.groups.append((gid, ids))
            try:
                got, errors, mc, seen = self._shapley(cls, lanes, group)
            except _Unpriced:
                unpriced = True
                got, errors, mc, seen = {}, {}, len(group) > MAX_EXACT_PLAYERS, []
            out.mc = out.mc or mc
            credits.update(got)
            out.se.update(errors)
            calibrations += [o.saving.calibration for o in seen]
            for c in group:
                group_of[c.ldef.lever_id] = gid
                methods[c.ldef.lever_id] = (f"Monte Carlo, {MC_PERMUTATIONS} permutations"
                                            if mc else "exact")
                uppers[c.ldef.lever_id] = c.ldef.upper_bound or any(
                    o.saving.upper_bound for o in seen)
        # full-scope joint replay
        joint_fig: Figure | None = None
        if joint:
            joint_fig = self._joint(cls, lanes, joint, sample_is_full)
            calibrations.append(joint_fig.calibration)
            if joint_fig.nano is None:
                unpriced = True
        cal = _combine_calibration(calibrations)
        # sample → full scope: credits via scale_credits; standalone values by the same factor
        # (by the class's spend ratio when there is no priced joint to scale to)
        factor = spend_factor
        scaled: dict[str, int] = {}
        if joint and not unpriced:
            assert joint_fig is not None and joint_fig.nano is not None
            total = sum(credits.values())
            scaled = scale_credits(credits, joint_fig.nano)
            if total:
                factor = Fraction(joint_fig.nano, total)
        scaled_note = ("" if sample_is_full
                       else f"; sample-scaled ({n_sample}/{n_full} lanes)")
        for c in joint:
            lid = c.ldef.lever_id
            standalone = self._standalone(cls, lanes, c, basis, factor, sample_is_full,
                                          scaled_note, uppers[lid])
            if unpriced:
                shap = _no_value(_UNPRICED_REPLAY, basis)
            else:
                shap = _estimate(scaled[lid], basis, cal, uppers[lid],
                                 f"Shapley credit ({methods[lid]}) in group {group_of[lid]}, "
                                 f"scaled to the full-scope joint replay{scaled_note}")
            proj = project(shap, c.ldef.lever_class, window_days=self.window_days,
                           upper_bound=uppers[lid], calibration=cal)
            assert proj is not None
            out.results.append(LeverResult(
                lever_id=lid, lever_class=c.ldef.lever_class, params=to_spec(c.member.policy),
                basis=basis, standalone=standalone, shapley=shap, projected_monthly=proj,
                needs_eval=c.ldef.needs_eval, upper_bound=uppers[lid], group=group_of[lid],
                finding_ids=c.finding_ids))
            if lid in out.se:
                out.se[lid] = round_half_even(out.se[lid] * abs(factor))
        for c in evaluated:
            out.results.append(self._evaluated(cls, lanes, c, basis, factor, sample_is_full,
                                               scaled_note))
        for ldef, fids in unreplayed:
            res = self._unreplayed(cls, ldef, fids, basis)
            if res is not None:
                out.results.append(res)
        out.joint = joint_fig
        out.headline = self._headline(out, basis)
        return out

    def _joint(self, cls: str, lanes: Sequence[Lane], joint: Sequence[_Chosen],
               sample_is_full: bool) -> Figure:
        members = tuple(sorted((c.member for c in joint), key=lambda m: m.lever_id))
        if sample_is_full:
            return self._value(cls, lanes, members, self.mode).saving
        job = ShardJob(load_lanes=self.load_lanes, members={cls: members}, mode=self.mode,
                       ctx=self.ctx, bases={cls: self._basis(cls)})
        parts = self.map_shards(job, self.shards)
        figs = []
        for part in parts:
            got = part.get(cls) if isinstance(part, Mapping) else None
            if got is not None and got[1] > 0:
                figs.append(got[0])
        return _sum_figures(figs, self._basis(cls))

    def _standalone(self, cls: str, lanes: Sequence[Lane], c: _Chosen, basis: Basis,
                    factor: Fraction, sample_is_full: bool, scaled_note: str,
                    upper: bool) -> Figure:
        outcome = self._value(cls, lanes, (c.member,), self.mode)
        if outcome.saving.nano is None:
            return _no_value(_UNPRICED_REPLAY, basis)
        nano = outcome.saving.nano if sample_is_full else round_half_even(
            outcome.saving.nano * factor)
        return _estimate(nano, basis, outcome.saving.calibration, upper,
                         f"standalone saving of this lever alone{scaled_note}; never summed "
                         "across levers")

    def _evaluated(self, cls: str, lanes: Sequence[Lane], c: _Chosen, basis: Basis,
                   factor: Fraction, sample_is_full: bool, scaled_note: str) -> LeverResult:
        lid = c.ldef.lever_id
        outcome = self._value(cls, lanes, (c.member,), self.mode)
        upper = c.ldef.upper_bound or outcome.saving.upper_bound
        standalone = self._standalone(cls, lanes, c, basis, factor, sample_is_full, scaled_note,
                                      upper)
        if lid in CEILING_ONLY:
            why = "ceiling only: delivered by the SessionStart hook (behavior), not projected"
            shap = _no_value(f"not in the joint set ({why})", basis)
            proj = _no_value(why, basis)
            group = "ceiling-only"
        else:
            why = ("trade-off lever outside the joint set and the headline "
                   "(needs --include-tradeoffs and an ab/measure gate)")
            shap = _no_value(why, basis)
            cal = standalone.calibration
            got = project(standalone, c.ldef.lever_class, window_days=self.window_days,
                          upper_bound=upper, calibration=cal)
            assert got is not None
            proj = dataclasses.replace(got, note=f"standalone projection; {why}; {got.note}")
            group = "trade-off"
        return LeverResult(lever_id=lid, lever_class=c.ldef.lever_class,
                           params=to_spec(c.member.policy), basis=basis, standalone=standalone,
                           shapley=shap, projected_monthly=proj, needs_eval=c.ldef.needs_eval,
                           upper_bound=upper, group=group, finding_ids=c.finding_ids)

    def _unreplayed(self, cls: str, ldef: LeverDef, fids: tuple[str, ...],
                    basis: Basis) -> LeverResult | None:
        if ldef.lever_class == "behavioral":
            why = "behavioral lever: not replayed, never projected (SPEC §11.2)"
            return LeverResult(
                lever_id=ldef.lever_id, lever_class=ldef.lever_class, params="", basis=basis,
                standalone=_no_value(why, basis), shapley=_no_value(why, basis),
                projected_monthly=_no_value(why, basis), needs_eval=ldef.needs_eval,
                upper_bound=ldef.upper_bound, group="behavioral", finding_ids=fids)
        wanted = set(fids)
        figs = [fig for f in self.findings if f.finding_id in wanted
                for fig in (f.recoverable_shapley or f.recoverable,)
                if fig is not None and fig.basis is basis]
        if not figs:
            return None
        total = reduce(add, figs)
        why = (f"not replayed at usage level (replay {ldef.replay}): projection from "
               f"{len(figs)} finding(s), outside the Shapley plan and the headline")
        upper = ldef.upper_bound or total.upper_bound
        cal = total.calibration if total.calibration is not Calibration.NA else \
            Calibration.UNCALIBRATED
        if total.nano is None:
            standalone = _no_value(total.note, basis)
        else:
            standalone = Figure(nano=total.nano, evidence=Evidence.ESTIMATED, basis=basis,
                                low_nano=total.low_nano, high_nano=total.high_nano,
                                calibration=cal, upper_bound=upper,
                                note=f"standalone from findings; never summed across levers; "
                                     f"{why}")
        got = project(standalone, ldef.lever_class, window_days=self.window_days,
                      upper_bound=upper, calibration=cal)
        assert got is not None   # behavioral levers are handled above
        return LeverResult(
            lever_id=ldef.lever_id, lever_class=ldef.lever_class, params="", basis=basis,
            standalone=standalone, shapley=_no_value(why, basis),
            projected_monthly=dataclasses.replace(got, note=f"{why}; {got.note}"),
            needs_eval=ldef.needs_eval, upper_bound=upper, group="projection-only",
            finding_ids=fids)

    def _headline(self, plan: _ClassPlan, basis: Basis) -> Figure:
        included = [r for r in plan.results if ":" in r.group]   # joint levers only
        if not included:
            return Figure(nano=0, evidence=Evidence.ESTIMATED, basis=basis,
                          note="no lever with a replayed saving in the joint set")
        projections = [r.projected_monthly for r in included]
        cal = _combine_calibration(p.calibration for p in projections)
        upper = any(p.upper_bound for p in projections)
        note = (f"sum of Shapley credit x realization-rate p50 over {len(included)} lever(s); "
                f"range p10-p90 (comonotone); per {DAYS_PER_MONTH}-day month; standalone values "
                "are never summed")
        if any(p.nano is None for p in projections):
            return Figure(nano=None, evidence=Evidence.ESTIMATED, basis=basis, calibration=cal,
                          upper_bound=upper, note=f"{_UNPRICED_REPLAY}; {note}")
        point = sum(p.nano for p in projections)  # type: ignore[misc]
        low = sum(p.low_nano if p.low_nano is not None else p.nano  # type: ignore[misc]
                  for p in projections)
        high = sum(p.high_nano if p.high_nano is not None else p.nano  # type: ignore[misc]
                   for p in projections)
        return Figure(nano=point, evidence=Evidence.ESTIMATED, basis=basis, low_nano=low,
                      high_nano=high, calibration=cal, upper_bound=upper, note=note)

    # ----- the whole plan -----

    def run(self, observed_rr: Mapping[str, tuple[str, int]] | None) -> ActionPlan:
        observed = parse_observed_rr(observed_rr)
        n_full = len(self.index)
        sample = stratified_sample(list(self.index.values()), n=self.sample_lanes,
                                   seed=self.seed)
        sample_is_full = len(sample) >= n_full
        lanes_by_class: dict[str, list[Lane]] = {cls: [] for cls in CLASS_ORDER}
        if sample and self.replayer is not None:
            loaded = self.load_lanes(frozenset(sample), None)
            for ln in sorted(loaded, key=lambda ln: ln.lane_key):
                if ln.lane_key in sample:
                    lanes_by_class.setdefault(ln.billing_class, []).append(ln)
        candidates, links = self._linked()
        classes_present = {_text(row.billing_class) for row in self.index.values()}
        classes_present |= {cls for (_lid, cls) in links}
        plans: dict[str, _ClassPlan] = {}
        for cls in CLASS_ORDER:
            if cls not in classes_present:
                continue
            spend_factor = self._spend_factor(cls, sample, sample_is_full)
            plans[cls] = self.plan_class(cls, lanes_by_class.get(cls, []), candidates, links,
                                         sample_is_full, spend_factor, len(sample), n_full)
        billed_basis = _class_basis("billed", self.ctx)
        billed = plans.get("billed")
        joint = billed.joint if billed is not None and billed.joint is not None else Figure(
            nano=0, evidence=Evidence.ESTIMATED, basis=billed_basis,
            note="no billed lever in the joint set")
        headline = billed.headline if billed is not None and billed.headline is not None else \
            Figure(nano=0, evidence=Evidence.ESTIMATED, basis=billed_basis,
                   note="no lever with a replayed saving in the joint set")
        levers: list[LeverResult] = []
        groups: list[tuple[str, tuple[str, ...]]] = []
        se: dict[str, int] = {}
        mc = False
        for cls in CLASS_ORDER:
            cp = plans.get(cls)
            if cp is None:
                continue
            levers += sorted(cp.results, key=_lever_rank)
            groups += cp.groups
            for lid, value in cp.se.items():
                se[f"{cls}:{lid}" if cls != "billed" else lid] = value
            mc = mc or cp.mc
        allowance = plans["allowance"].headline if "allowance" in plans else None
        pool = plans["pool"].headline if "pool" in plans else None
        return ActionPlan(
            joint_saving=joint, headline_monthly=headline, allowance_headroom_monthly=allowance,
            levers=tuple(levers), groups=tuple(groups),
            method="shapley-mc" if mc else "shapley-exact",
            shapley_se=tuple(sorted(se.items())), sample=self._sample_text(len(sample), n_full),
            observed_rr=observed, pool_headroom_monthly=pool)

    def _spend_factor(self, cls: str, sample: frozenset[str], sample_is_full: bool) -> Fraction:
        if sample_is_full:
            return Fraction(1)
        full = sum(r.point_nano for r in self.index.values() if _text(r.billing_class) == cls)
        part = sum(r.point_nano for k, r in self.index.items()
                   if k in sample and _text(r.billing_class) == cls)
        return Fraction(full, part) if part > 0 and full >= 0 else Fraction(1)

    def _sample_text(self, n: int, total: int) -> str:
        if self.replayer is None:
            return f"no replayer: no lever replayed ({total} lanes in scope)"
        if n >= total:
            return (f"shapley on {total}/{total} lanes (seed {self.seed}), the sample is the "
                    "full scope (no scaling)")
        return f"shapley on {n}/{total} lanes (seed {self.seed}), scaled to full-scope joint replay"


_GROUP_RANK = {"trade-off": 1, "ceiling-only": 2, "projection-only": 3, "behavioral": 4}


def _lever_rank(r: LeverResult) -> tuple[int, int, int, str]:
    rank = _GROUP_RANK.get(r.group, 0)
    point = r.projected_monthly.nano
    return (rank, 0 if point is not None else 1, -(point or 0), r.lever_id)


def build_action_plan(findings: Sequence[Finding], index: Sequence[LaneIndexRow],
                      load_lanes: Callable[[Collection[str] | None, ShardKey | None],
                                           Sequence[Lane]],
                      shards: Sequence[ShardKey], ctx: AnalysisContext, *, window_days: int,
                      include_tradeoffs: bool = False, seed: int = 0, sample_lanes: int = 20_000,
                      observed_rr: Mapping[str, tuple[str, int]] | None = None,
                      map_shards: Callable[[Callable[[ShardKey], object], Sequence[ShardKey]],
                                           list] | None = None
                      ) -> ActionPlan:
    """The ranked action plan of SPEC §11.2 (see the module docstring for every step).

    ``load_lanes(lane_keys, None)`` returns the sample; ``load_lanes(None, shard)`` one shard's
    lanes. *map_shards* runs a function over shards (the pipeline passes a process-pool mapper for
    ``--jobs``; the function is a picklable :class:`ShardJob`); default sequential. The result is
    deterministic for a given seed and identical for any shard partition of the same lanes.
    Invalid arguments raise :class:`UsageError`.
    """
    if type(window_days) is not int or window_days < 1:
        raise UsageError("window_days must be a positive int")
    if type(sample_lanes) is not int or sample_lanes < 1:
        raise UsageError("sample_lanes must be a positive int")
    if type(seed) is not int:
        raise UsageError("seed must be an int")
    if not isinstance(ctx, AnalysisContext):
        raise UsageError("ctx must be an AnalysisContext")
    if not callable(load_lanes):
        raise UsageError("load_lanes must be callable")
    rows = list(index)
    if any(not isinstance(r, LaneIndexRow) for r in rows):
        raise UsageError("index must hold LaneIndexRow values")
    findings = list(findings)
    if any(not isinstance(f, Finding) for f in findings):
        raise UsageError("build_action_plan: findings must be Finding values")
    planner = _Planner(findings, rows, load_lanes, shards, ctx, window_days=window_days,
                       include_tradeoffs=bool(include_tradeoffs), seed=seed,
                       sample_lanes=sample_lanes,
                       map_shards=map_shards if map_shards is not None else _sequential)
    return planner.run(observed_rr)
