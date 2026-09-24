"""k-anonymity: the only implementation (SPEC §3.23, §8.4, §8.5; ruling R-E1).

* :func:`publish` turns a store :class:`~tokenbill.core.types.RawAggregate` into the only aggregate
  type renderers accept. Rows with fewer than *k* users merge into an ``"(other: <k users)"`` row
  inside their parent group (the group-by prefix without the last dimension); when that row is still
  below *k* it absorbs the smallest remaining row of the parent (complementary suppression), so no
  suppressed cell is recoverable by subtracting published rows from a published total. A parent
  group with no row of *k* or more users is resolved one level up; what cannot reach *k* even at the
  top is withheld (counted in ``suppressed_rows``/``suppressed_users``). Published totals equal raw
  totals whenever the whole table reaches *k*.
* A merged row's ``n_users`` is the **largest constituent count** — the only exact lower bound on
  the distinct users of a union when rows may share people (a user appears in several model or day
  rows). This mirrors :func:`rescope_findings` without ``count_users`` and can only suppress more.
* :func:`rescope_findings` re-scopes org findings below *k* to their parent scope, with exact
  distinct counts from the store (``count_users``) when given. Data-quality findings (kinds ``dq.*``
  and category ``data-quality``, e.g. the missing-capabilities finding) carry no personal data and
  are exempt (R-E1); so are provider-side ``aggregate`` findings whose scope names no person or API
  key (SPEC §8.5).
* :func:`require_self_or_aggregate` refuses grouping by person without the self view;
  :func:`merge_small_groups` is the ingest-time team aggregation of SPEC §5.11.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any, TypeVar

from tokenbill.core.errors import ContractViolation, PrivacyError, UsageError
from tokenbill.core.labels import Figure, add
from tokenbill.core.money import ratio
from tokenbill.core.records import UsageBuckets
from tokenbill.core.types import (
    _PUBLISH_TOKEN,
    AggRow,
    Finding,
    PricedTotal,
    PublishedAggregate,
    RawAggregate,
    Scope,
)

__all__ = [
    "PERSON_DIMS",
    "RESCOPE_LEVELS",
    "merge_small_groups",
    "other_label",
    "publish",
    "require_self_or_aggregate",
    "rescope_findings",
]

T = TypeVar("T")

#: Dimensions that name a person (or a person's session); never grouped or listed for the org.
PERSON_DIMS = frozenset({"principal", "session", "session_key"})
#: Scope dimensions kept at each re-scoping level, finest first: team × lane kind → team →
#: cost center → org. ``billing_class`` is kept everywhere so allowance (list-equivalent) and billed
#: figures are never added (R10).
RESCOPE_LEVELS: tuple[frozenset[str], ...] = (
    frozenset({"team", "cost_center", "lane_kind", "billing_class"}),
    frozenset({"team", "cost_center", "billing_class"}),
    frozenset({"cost_center", "billing_class"}),
    frozenset({"billing_class"}),
)
_KEY_DIMS = frozenset({"api_key_id", "api_key"})
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def other_label(k: int) -> str:
    """The label of merged small groups: ``"(other: <5 users)"`` for ``k = 5``."""
    return f"(other: <{k} users)"


def _check_k(k: object) -> int:
    if type(k) is not int or k < 1:
        raise UsageError("k-anonymity threshold k must be an int >= 1")
    return k


# ---------------------------------------------------------------------------------------------
# value arithmetic for merged rows
# ---------------------------------------------------------------------------------------------


def _add_usage(a: UsageBuckets, b: UsageBuckets) -> UsageBuckets:
    """``a + b``; two different other-write TTLs keep the shorter one instead of raising (a
    published row mixing providers keeps the token total; the TTL is informational)."""
    ta, tb = a.cache_write_other_ttl_s, b.cache_write_other_ttl_s
    if ta is not None and tb is not None and ta != tb:
        ttl = min(ta, tb)
        a = dataclasses.replace(a, cache_write_other_ttl_s=ttl)
        b = dataclasses.replace(b, cache_write_other_ttl_s=ttl)
    return a + b


def _add_opt(a: Figure | None, b: Figure | None) -> Figure | None:
    if a is None:
        return b
    if b is None:
        return a
    return add(a, b)


def _tokens(usage: UsageBuckets) -> int:
    return usage.total_input + usage.output


def _coverage(priced_tokens: int, all_tokens: int) -> str:
    if all_tokens <= 0:
        return "1"
    value = ratio(max(priced_tokens, 0), all_tokens)
    assert value is not None
    text = format(value.normalize(), "f")
    return text


def _add_priced(a: PricedTotal, b: PricedTotal, usage_a: UsageBuckets,
                usage_b: UsageBuckets) -> PricedTotal:
    all_tokens = _tokens(usage_a) + _tokens(usage_b)
    unpriced = a.unpriced_tokens + b.unpriced_tokens
    return PricedTotal(
        exact=add(a.exact, b.exact),
        estimated=_add_opt(a.estimated, b.estimated),
        allowance=_add_opt(a.allowance, b.allowance),
        priced_inferences=a.priced_inferences + b.priced_inferences,
        unpriced_inferences=a.unpriced_inferences + b.unpriced_inferences,
        unpriced_tokens=unpriced,
        coverage=_coverage(all_tokens - unpriced, all_tokens),
    )


@dataclasses.dataclass
class _Cell:
    dims: tuple[tuple[str, str | None], ...]
    n_users: int
    n_requests: int
    usage: UsageBuckets
    priced: PricedTotal
    members: tuple[int, ...]     # indexes of the raw rows inside this cell

    def row(self) -> AggRow:
        return AggRow(dims=self.dims, n_users=self.n_users, n_requests=self.n_requests,
                      usage=self.usage, priced=self.priced)


def _merge_dims(cells: Sequence[_Cell], label: str) -> tuple[tuple[str, str | None], ...]:
    first = cells[0].dims
    out = []
    for i, (name, value) in enumerate(first):
        same = all(len(c.dims) > i and c.dims[i] == (name, value) for c in cells)
        out.append((name, value if same else label))
    return tuple(out)


def _merge_cells(cells: Sequence[_Cell], label: str) -> _Cell:
    usage = cells[0].usage
    priced = cells[0].priced
    for c in cells[1:]:
        priced = _add_priced(priced, c.priced, usage, c.usage)
        usage = _add_usage(usage, c.usage)
    return _Cell(
        dims=_merge_dims(cells, label),
        n_users=max(c.n_users for c in cells),  # lower bound on distinct users of the union
        n_requests=sum(c.n_requests for c in cells),
        usage=usage,
        priced=priced,
        members=tuple(sorted(m for c in cells for m in c.members)),
    )


def _dims_key(dims: tuple[tuple[str, str | None], ...], label: str) -> tuple:
    return tuple(
        (2 if v == label else 1 if v is None else 0, v or "", name) for name, v in dims
    )


def _value_key(c: _Cell) -> tuple:
    exact = c.priced.exact.nano if c.priced.exact.nano is not None else -1
    return (c.n_users, exact, c.n_requests, _tokens(c.usage))


def _prefix(n: int) -> Callable[[tuple], tuple]:
    return lambda dims: tuple(dims[:n])


def publish(raw: RawAggregate, *, k: int = 5,
            parent_of: Callable[[tuple], tuple] | None = None) -> PublishedAggregate:
    """Publish *raw* with k-anonymity and complementary suppression (SPEC §8.4).

    *parent_of* maps a row's ``dims`` to its parent key (default: the dims without the last
    dimension). Rows below *k* merge into ``other_label(k)`` rows; see the module docstring for the
    full rule. Output rows are sorted by dims (other rows last within their parent).
    """
    if not isinstance(raw, RawAggregate):
        raise ContractViolation("publish expects a RawAggregate")
    k = _check_k(k)
    label = other_label(k)
    cells = [
        _Cell(dims=tuple(r.dims), n_users=r.n_users, n_requests=r.n_requests, usage=r.usage,
              priced=r.priced, members=(i,))
        for i, r in enumerate(raw.rows)
    ]
    depth = max((len(c.dims) for c in cells), default=len(raw.group_by))
    levels: list[Callable[[tuple], tuple]]
    if parent_of is not None:
        levels = [parent_of, _prefix(0)]
    else:
        levels = [_prefix(n) for n in range(depth - 1, -1, -1)]
    for parent in levels:
        if all(c.n_users >= k for c in cells):
            break
        groups: dict[tuple, list[_Cell]] = {}
        for c in cells:
            groups.setdefault(tuple(parent(c.dims)), []).append(c)
        next_cells: list[_Cell] = []
        for key in sorted(groups, key=lambda g: repr(g)):
            group = sorted(groups[key], key=lambda c: _dims_key(c.dims, label))
            small = [c for c in group if c.n_users < k]
            big = [c for c in group if c.n_users >= k]
            if not small:
                next_cells.extend(group)
                continue
            merged = _merge_cells(small, label)
            if merged.n_users < k and big:
                complement = min(big, key=lambda c: (_value_key(c), _dims_key(c.dims, label)))
                big = [c for c in big if c is not complement]
                merged = _merge_cells([merged, complement], label)
            next_cells.extend(big)
            next_cells.append(merged)
        cells = next_cells
    published = [c for c in cells if c.n_users >= k]
    published.sort(key=lambda c: _dims_key(c.dims, label))
    shown = {c.members[0] for c in published if len(c.members) == 1}
    suppressed = [i for i in range(len(raw.rows)) if i not in shown]
    return PublishedAggregate(
        group_by=tuple(raw.group_by),
        rows=tuple(c.row() for c in published),
        window=raw.window,
        k=k,
        suppressed_rows=len(suppressed),
        suppressed_users=sum(raw.rows[i].n_users for i in suppressed),
        token=_PUBLISH_TOKEN,
    )


# ---------------------------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------------------------


def _exempt(f: Finding) -> bool:
    """R-E1: data-quality findings carry no personal data. Provider-side aggregate findings whose
    scope names no person and no API key describe workspaces/models, not people (§8.5)."""
    if f.category == "data-quality" or f.kind.startswith("dq."):
        return True
    names = {name for name, _ in f.scope.dims}
    return f.category == "aggregate" and not (names & (PERSON_DIMS | _KEY_DIMS))


def _needs_rescope(f: Finding, k: int) -> bool:
    names = {name for name, _ in f.scope.dims}
    return f.n_users < k or bool(names & PERSON_DIMS)


def _finding_id(detector_id: str, kind: str, scope: Scope) -> str:
    from tokenbill.core.registry import _finding_id as registry_finding_id

    return registry_finding_id(detector_id, kind, scope)


def _parent_scope(scope: Scope, keep: frozenset[str]) -> Scope:
    return Scope(dims=tuple(sorted((n, v) for n, v in scope.dims if n in keep)))


def _aggregate_parent(scope: Scope) -> Scope:
    return Scope(dims=tuple(sorted((n, v) for n, v in scope.dims
                                   if n not in PERSON_DIMS and n not in _KEY_DIMS)))


def _sum_opt(figs: Iterable[Figure | None]) -> Figure | None:
    out: Figure | None = None
    for f in figs:
        out = _add_opt(out, f)
    return out


def _merge_findings(children: Sequence[Finding], scope: Scope, n_users: int, k: int) -> Finding:
    first = children[0]
    cost = children[0].cost_observed
    for c in children[1:]:
        cost = add(cost, c.cost_observed)
    evidence: list = []
    seen: set = set()
    for c in children:
        for item in c.evidence:
            if item not in seen:
                seen.add(item)
                evidence.append(item)
    validated = {c.validated_against for c in children}
    fix = next((c.fix for c in children if c.fix is not None), None)
    summary = first.summary
    if len(children) > 1 or scope != first.scope:
        prefix = f"[re-scoped for k-anonymity (k={k}); {len(children)} finding(s) merged] "
        summary = (prefix + summary)[:400]
    return dataclasses.replace(
        first,
        finding_id=_finding_id(first.detector_id, first.kind, scope),
        scope=scope,
        n_events=sum(c.n_events for c in children),
        n_lanes=sum(c.n_lanes for c in children),
        n_users=n_users,
        first_seen_ms=min(c.first_seen_ms for c in children),
        summary=summary,
        cost_observed=cost,
        recoverable=_sum_opt(c.recoverable for c in children),
        recoverable_shapley=_sum_opt(c.recoverable_shapley for c in children),
        projected_monthly=_sum_opt(c.projected_monthly for c in children),
        lever_ids=tuple(sorted({lv for c in children for lv in c.lever_ids})),
        evidence=tuple(evidence[:20]),
        fix=fix,
        confidence=min((c.confidence for c in children),
                       key=lambda v: _CONFIDENCE_RANK.get(v, 1)),
        validated_against=validated.pop() if len(validated) == 1 else None,
        needs_eval=any(c.needs_eval for c in children),
        references=tuple(sorted({r for c in children for r in c.references})),
    )


def _sort_key(f: Finding) -> tuple[int, str, str]:
    p50 = f.recoverable.nano if f.recoverable is not None and f.recoverable.nano is not None else 0
    return (-p50, f.detector_id, f.finding_id)


def rescope_findings(findings: Sequence[Finding], *, k: int = 5,
                     count_users: Callable[[Scope], int] | None = None) -> list[Finding]:
    """Publish *findings* for the org audience with k-anonymity (SPEC §8.4).

    An org finding whose scope has fewer than *k* users — or names a person or session — is
    re-scoped to its parent (team × lane kind → team → cost center → org, :data:`RESCOPE_LEVELS`),
    merging the findings of the same detector and kind that land on the same parent (and an already
    published finding with exactly that scope). The merged ``n_users`` is
    ``count_users(parent_scope)`` — an exact distinct count from the store — or, without it, the
    largest child count (a lower bound, which can only suppress more). What stays below *k* at the
    org level is withheld. Exempt and passed through unchanged: data-quality findings (R-E1),
    provider-side aggregate findings without person or API-key dimensions, and ``self``-audience
    findings (published only to the self view by the caller). Output order: (−recoverable point or
    0, detector_id, finding_id).
    """
    k = _check_k(k)
    out: list[Finding] = []
    pending: list[Finding] = []
    key_scoped: dict[tuple, list[Finding]] = {}
    for f in findings:
        if not isinstance(f, Finding):
            raise ContractViolation("rescope_findings expects Findings")
        if f.audience != "org" or _exempt(f):
            out.append(f)
        elif f.category == "aggregate" and _needs_rescope(f, k):
            # Provider-side finding on an API key below k: re-scope to the key's workspace level,
            # which names no person (SPEC §8.5).
            parent = _aggregate_parent(f.scope)
            key_scoped.setdefault((f.detector_id, f.kind, parent.dims), []).append(f)
        elif _needs_rescope(f, k):
            pending.append(f)
        else:
            out.append(f)
    for (detector_id, kind, dims), children in sorted(key_scoped.items()):
        parent = Scope(dims=dims)
        same_scope = [f for f in out if f.detector_id == detector_id and f.kind == kind
                      and f.scope == parent and f.audience == "org"]
        out = [f for f in out if f not in same_scope]
        group = sorted(children + same_scope, key=lambda f: f.finding_id)
        out.append(_merge_findings(group, parent, max(c.n_users for c in group), k))
    for keep in RESCOPE_LEVELS:
        if not pending:
            break
        groups: dict[tuple, list[Finding]] = {}
        for f in pending:
            parent = _parent_scope(f.scope, keep)
            groups.setdefault((f.detector_id, f.kind, parent.dims), []).append(f)
        pending = []
        for key in sorted(groups):
            detector_id, kind, dims = key
            parent = Scope(dims=dims)
            children = sorted(groups[key], key=lambda f: f.finding_id)
            same_scope = [f for f in out if f.detector_id == detector_id and f.kind == kind
                          and f.scope == parent and f.audience == "org" and not _exempt(f)]
            if same_scope:
                out = [f for f in out if f not in same_scope]
                children = sorted(children + same_scope, key=lambda f: f.finding_id)
            if count_users is not None:
                n = int(count_users(parent))
            else:
                n = max(c.n_users for c in children)
            merged = _merge_findings(children, parent, n, k)
            if n >= k:
                out.append(merged)
            else:
                pending.append(merged)
    out.sort(key=_sort_key)
    return out


def require_self_or_aggregate(group_by: Sequence[str], self_principal: str | None) -> None:
    """Refuse grouping by a person (``principal``, ``session``, ``session_key``) unless the caller
    is the self view (``self_principal`` given): no command lists or ranks people (SPEC §8.5)."""
    if isinstance(group_by, str):
        group_by = [part for part in group_by.split(",") if part]
    person = sorted({g.strip() for g in group_by} & PERSON_DIMS)
    if person and not self_principal:
        raise PrivacyError(
            f"grouping by {', '.join(person)} is only allowed in the self view (--self)"
        )


def _combine_payload(a: Any, b: Any) -> Any:
    if a is None:
        return b
    if b is None:
        return a
    if isinstance(a, bool) or isinstance(b, bool):
        raise UsageError("merge_small_groups: boolean payloads cannot be combined")
    if isinstance(a, (int, Decimal)) and isinstance(b, (int, Decimal)):
        return a + b
    if isinstance(a, (tuple, list)) and isinstance(b, (tuple, list)):
        if len(a) != len(b):
            raise UsageError("merge_small_groups: payload sequences differ in length")
        return type(a)(_combine_payload(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        return {key: _combine_payload(a.get(key), b.get(key)) for key in sorted({*a, *b})}
    if isinstance(a, UsageBuckets) and isinstance(b, UsageBuckets):
        return _add_usage(a, b)
    try:
        return a + b
    except TypeError:
        raise UsageError("merge_small_groups: payloads must support addition") from None


def merge_small_groups(rows: Sequence[tuple[str, int, T]], *, k: int,
                       other_label: str = "(other)") -> tuple[list[tuple[str, int, T]], int]:
    """Ingest-time aggregation of people into groups (SPEC §5.11): groups with fewer than *k* users
    merge into one *other_label* row when together they reach *k*; otherwise they are dropped.

    Precondition: the groups partition the users (each person maps to one team through
    ``IngestOptions.team_map``), so the merged count is the exact sum. Payloads are combined with
    ``+`` (numbers, ``UsageBuckets``, anything supporting ``+``), element-wise for tuples/lists and
    key-wise for mappings. Returns ``(rows, dropped)``: rows sorted by label with the other row
    last, and the number of input groups dropped entirely (for ``dq.outcomes_suppressed``).
    """
    k = _check_k(k)
    big: dict[str, tuple[str, int, Any]] = {}
    small: list[tuple[str, int, Any]] = []
    for row in rows:
        label, n, payload = row
        if type(n) is not int or n < 0:
            raise UsageError("merge_small_groups: user counts must be non-negative ints")
        if n >= k and label != other_label:
            if label in big:
                prev = big[label]
                big[label] = (label, prev[1] + n, _combine_payload(prev[2], payload))
            else:
                big[label] = (label, n, payload)
        else:
            small.append((label, n, payload))
    out: list[tuple[str, int, Any]] = [big[label] for label in sorted(big)]
    dropped = 0
    if small:
        total = sum(n for _, n, _ in small)
        if total >= k:
            payload = None
            for _, _, p in small:
                payload = _combine_payload(payload, p)
            out.append((other_label, total, payload))
        else:
            dropped = len(small)
    return out, dropped
