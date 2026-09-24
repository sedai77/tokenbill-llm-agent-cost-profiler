"""Policy grammar and selectors (SPEC §9.5, §3.19, D29; F-SEM).

A policy spec is a ``;``-joined list of clauses ``name=value[,opt=val…][@selector]``; the selector
(default ``all``) is a ``,``-joined AND of ``key:value`` terms. Clauses, in canonical order::

    ttl=5m|1h[@sel]                       (repeatable)   → Policy.ttl
    keepalive=240s[,max=3600s][@sel]                     → Policy.keepalive
    compact-window=400000[,post=20283]                   → Policy.compaction_window
    cold-resume=compact|clear[,min=200000]               → Policy.cold_resume
    model=<model id>[@sel]                (repeatable)   → Policy.model_remap
    effort=<level>[,scale=0.5][@sel]      (repeatable)   → Policy.effort
    fast=off  geo=global  regional=global  batch=eligible
    repair=<id>                           (repeatable)   → Policy.repairs
    breakpoints=observed|end|static_plus_end|every_15

Selector keys: ``lane_kind`` (a LaneKind value), ``agent_type``, ``team``, ``agent_product``,
``billing_path`` (a ``core.records.BILLING_PATHS`` value), ``workload`` (a WorkloadClass value),
``model`` (a model id). Unknown clauses, options, selector keys or values raise
:class:`~tokenbill.core.errors.UsageError`.

Canonical form (:func:`to_spec`, which ``Policy.spec()`` delegates to): clauses in the order above;
repeated clauses sorted by selector then value; durations in seconds with an ``s`` suffix; decimal
scales normalized (``0.50`` → ``0.5``); every option printed except an absent ``post``; ``@all``
omitted; selector terms sorted by key. Defaults when an option is omitted: ``max=3600s``
(KEEPALIVE_MAX_IDLE_S), ``min=200000`` (§9.3.4), ``scale=0.5``, ``post`` absent → org median.

The ``name`` of a parsed policy is its canonical spec (``"observed"`` for the empty policy), so
``parse_policy(to_spec(p)) == p`` holds for every *canonical* policy (named that way, with
normalized selectors and scales); :func:`combine` keeps that naming. ``to_spec`` of any valid
policy is canonical: ``to_spec(parse_policy(to_spec(p))) == to_spec(p)``.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal, InvalidOperation

from tokenbill.core import records
from tokenbill.core.errors import ContractViolation, UsageError
from tokenbill.core.evidence import KEEPALIVE_MAX_IDLE_S
from tokenbill.core.models import normalize_model
from tokenbill.core.records import Lane, LaneKind, Request, WorkloadClass
from tokenbill.core.types import Policy

__all__ = [
    "BREAKPOINT_POLICIES",
    "CLAUSE_ORDER",
    "COLD_RESUME_MIN_DEFAULT",
    "EFFORT_LEVELS",
    "EFFORT_SCALE_DEFAULT",
    "KEEPALIVE_MAX_DEFAULT_S",
    "REPAIRS",
    "SELECTOR_KEYS",
    "combine",
    "lane_matches",
    "parse_policy",
    "request_matches",
    "selector_terms",
    "to_spec",
]

CLAUSE_ORDER = ("ttl", "keepalive", "compact-window", "cold-resume", "model", "effort", "fast",
                "geo", "regional", "batch", "repair", "breakpoints")
SELECTOR_KEYS = ("lane_kind", "agent_type", "team", "agent_product", "billing_path", "workload",
                 "model")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
REPAIRS = ("restore_caching", "stagger_fanout", "retry_backoff_cap", "fallback_credit",
           "shared_ci_prefix")
BREAKPOINT_POLICIES = ("observed", "end", "static_plus_end", "every_15")
TTL_VALUES = ("5m", "1h")
COLD_RESUME_ACTIONS = ("compact", "clear")
COLD_RESUME_MIN_DEFAULT = 200_000
EFFORT_SCALE_DEFAULT = "0.5"
KEEPALIVE_MAX_DEFAULT_S: int = KEEPALIVE_MAX_IDLE_S.value  # type: ignore[assignment]

_OBSERVED_NAME = "observed"
_BLOCK_REPAIR_RE = re.compile(r"block:[a-z0-9][a-z0-9_-]{0,63}\Z")
_DURATION_RE = re.compile(r"(\d{1,9})([smh]?)\Z")
_INT_RE = re.compile(r"\d{1,12}\Z")
_SCALE_RE = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)\Z")
_FORBIDDEN = frozenset(";,@=")
_MAX_SPEC_LEN = 16_384


def _bad(message: str) -> UsageError:
    return UsageError(f"policy spec: {message}")


def _clip(text: str) -> str:
    return repr(text[:40])


# ---------------------------------------------------------------------------------------------
# selectors
# ---------------------------------------------------------------------------------------------


def _check_value(text: str, what: str) -> str:
    """A free value (team, agent type, model id…): non-empty, no separators, no control chars, no
    surrounding whitespace."""
    if not text or text != text.strip():
        raise _bad(f"empty or padded {what}")
    if any(ch in _FORBIDDEN or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise _bad(f"invalid character in {what}")
    return text


def _check_term(key: str, value: str) -> None:
    if key not in SELECTOR_KEYS:
        raise _bad(f"unknown selector key {_clip(key)}")
    _check_value(value, f"{key} value")
    if key == "lane_kind" and value not in {k.value for k in LaneKind}:
        raise _bad(f"unknown lane_kind {_clip(value)}")
    if key == "workload" and value not in {w.value for w in WorkloadClass}:
        raise _bad(f"unknown workload {_clip(value)}")
    if key == "billing_path" and value not in records.BILLING_PATHS:
        raise _bad(f"unknown billing_path {_clip(value)}")


def _parse_selector(selector: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(selector, str):
        raise _bad("selector must be a string")
    return _parse_selector_text(selector)


@functools.lru_cache(maxsize=4096)
def _parse_selector_text(selector: str) -> tuple[tuple[str, str], ...]:
    text = selector.strip()
    if text in ("", "all"):
        return ()
    terms: dict[str, str] = {}
    for raw in text.split(","):
        term = raw.strip()
        key, sep, value = term.partition(":")
        if not sep:
            raise _bad(f"selector term without ':' {_clip(term)}")
        key, value = key.strip(), value.strip()
        _check_term(key, value)
        if key in terms:
            raise _bad(f"repeated selector key {_clip(key)}")
        terms[key] = value
    return tuple(sorted(terms.items()))


def selector_terms(selector: str) -> tuple[tuple[str, str], ...]:
    """The ``(key, value)`` terms of *selector*, sorted by key; ``"all"`` (or empty) → ``()``.
    Unknown keys, invalid values and repeated keys raise :class:`UsageError`."""
    return _parse_selector(selector)


def _canonical_selector(selector: str) -> str:
    terms = _parse_selector(selector)
    return ",".join(f"{k}:{v}" for k, v in terms) if terms else "all"


@functools.lru_cache(maxsize=4096)
def _normalized_model(model: str) -> str:
    return normalize_model(model).model or model


def _billing_path(request: Request) -> str:
    if request.attribution.billing_path is not None:
        return request.attribution.billing_path
    inf = request.serving_inference
    return inf.pricing.billing_path if inf is not None else "unknown"


def _term_matches(key: str, value: str, request: Request | None, lane: Lane) -> bool:
    if key == "lane_kind":
        return lane.kind.value == value
    if request is None:
        return False
    attribution = request.attribution
    if key == "agent_type":
        return attribution.agent_type == value
    if key == "team":
        return attribution.team == value
    if key == "agent_product":
        return attribution.agent_product == value
    if key == "billing_path":
        return _billing_path(request) == value
    if key == "workload":
        return attribution.workload_class.value == value
    # key == "model": the serving model, compared on normalized ids
    return request.model == value or request.model == _normalized_model(value)


def lane_matches(selector: str, lane: Lane) -> bool:
    """True when every term of *selector* holds for *lane*: ``lane_kind`` on the lane, the other
    keys on the lane's first request (a lane without requests matches only lane-level terms)."""
    first = lane.requests[0] if lane.requests else None
    return all(_term_matches(k, v, first, lane) for k, v in _parse_selector(selector))


def request_matches(selector: str, request: Request, lane: Lane) -> bool:
    """True when every term of *selector* holds for *request* (``lane_kind`` from *lane*)."""
    return all(_term_matches(k, v, request, lane) for k, v in _parse_selector(selector))


# ---------------------------------------------------------------------------------------------
# values
# ---------------------------------------------------------------------------------------------


def _duration_s(text: str, what: str) -> int:
    m = _DURATION_RE.match(text)
    if m is None:
        raise _bad(f"invalid {what} {_clip(text)}")
    n = int(m.group(1))
    seconds = n * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]
    if seconds <= 0:
        raise _bad(f"{what} must be positive")
    return seconds


def _positive_int(text: str, what: str) -> int:
    if not _INT_RE.match(text):
        raise _bad(f"invalid {what} {_clip(text)}")
    n = int(text)
    if n <= 0:
        raise _bad(f"{what} must be positive")
    return n


def _canonical_scale(text: str) -> str:
    if not isinstance(text, str) or not _SCALE_RE.match(text):
        raise _bad(f"invalid scale {_clip(str(text))}")
    try:
        value = Decimal(text)
    except InvalidOperation:  # pragma: no cover - the regex admits only plain decimals
        raise _bad("invalid scale") from None
    if not Decimal(0) <= value <= Decimal(1):
        raise _bad("scale must be in [0, 1]")
    return format(value.normalize(), "f")


def _check_repair(repair: str) -> str:
    if repair in REPAIRS or _BLOCK_REPAIR_RE.match(repair):
        return repair
    raise _bad(f"unknown repair {_clip(repair)}")


def _check_model_id(model: str) -> str:
    return _check_value(model, "model id")


# ---------------------------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------------------------


class _Builder:
    """Accumulates parsed clauses; repeated clauses dedupe, conflicting ones raise."""

    def __init__(self, error: Callable[[str], Exception]) -> None:
        self.error = error
        self.ttl: dict[str, str] = {}
        self.model: dict[str, str] = {}
        self.effort: dict[str, tuple[str, str]] = {}
        self.repairs: set[str] = set()
        self.scalars: dict[str, object] = {}

    def keyed(self, table: dict, selector: str, value: object, what: str) -> None:
        old = table.get(selector)
        if old is not None and old != value:
            raise self.error(f"conflicting {what} clauses for selector {_clip(selector)}")
        table[selector] = value

    def scalar(self, name: str, value: object) -> None:
        old = self.scalars.get(name)
        if old is not None and old != value:
            raise self.error(f"conflicting {name} clauses")
        self.scalars[name] = value

    def build(self) -> Policy:
        s = self.scalars
        policy = Policy(
            name=_OBSERVED_NAME,
            ttl=tuple(sorted(self.ttl.items())),
            keepalive=s.get("keepalive"),  # type: ignore[arg-type]
            compaction_window=s.get("compaction_window"),  # type: ignore[arg-type]
            cold_resume=s.get("cold_resume"),  # type: ignore[arg-type]
            model_remap=tuple(sorted(self.model.items())),
            effort=tuple(sorted((sel, lvl, sc) for sel, (lvl, sc) in self.effort.items())),
            fast_off=bool(s.get("fast_off", False)),
            geo_global=bool(s.get("geo_global", False)),
            regional_to_global=bool(s.get("regional_to_global", False)),
            batch=s.get("batch"),  # type: ignore[arg-type]
            repairs=tuple(sorted(self.repairs)),
            breakpoint_policy=s.get("breakpoint_policy"),  # type: ignore[arg-type]
        )
        spec = to_spec(policy)
        return _named(policy, spec)


def _named(policy: Policy, spec: str) -> Policy:
    return replace(policy, name=spec or _OBSERVED_NAME)


_OPTIONS = {
    "ttl": (), "keepalive": ("max",), "compact-window": ("post",), "cold-resume": ("min",),
    "model": (), "effort": ("scale",), "fast": (), "geo": (), "regional": (), "batch": (),
    "repair": (), "breakpoints": (),
}
_TAKES_SELECTOR = frozenset({"ttl", "keepalive", "model", "effort"})
_FLAGS = {"fast": ("off", "fast_off"), "geo": ("global", "geo_global"),
          "regional": ("global", "regional_to_global"), "batch": ("eligible", "batch")}


def _parse_clause(text: str, builder: _Builder) -> None:
    head, at, selector_text = text.partition("@")
    if at and not selector_text.strip():
        raise _bad("empty selector after '@'")
    name, eq, rest = head.partition("=")
    name = name.strip()
    if not eq:
        raise _bad(f"clause without '=' {_clip(text)}")
    if name not in _OPTIONS:
        raise _bad(f"unknown clause {_clip(name)}")
    if at and name not in _TAKES_SELECTOR:
        raise _bad(f"clause {name} takes no selector")
    selector = _canonical_selector(selector_text) if at else "all"
    parts = [p.strip() for p in rest.split(",")]
    value, opt_parts = parts[0], parts[1:]
    if not value:
        raise _bad(f"clause {name} needs a value")
    options: dict[str, str] = {}
    for part in opt_parts:
        key, oeq, oval = part.partition("=")
        key, oval = key.strip(), oval.strip()
        if not oeq or not oval:
            raise _bad(f"malformed option in clause {name}")
        if key not in _OPTIONS[name]:
            raise _bad(f"unknown option {_clip(key)} for clause {name}")
        if key in options:
            raise _bad(f"repeated option {key} in clause {name}")
        options[key] = oval

    if name == "ttl":
        if value not in TTL_VALUES:
            raise _bad(f"ttl must be 5m or 1h, not {_clip(value)}")
        builder.keyed(builder.ttl, selector, value, "ttl")
    elif name == "keepalive":
        interval = _duration_s(value, "keepalive interval")
        max_idle = _duration_s(options["max"], "keepalive max") if "max" in options \
            else KEEPALIVE_MAX_DEFAULT_S
        builder.scalar("keepalive", (selector, interval, max_idle))
    elif name == "compact-window":
        window = _positive_int(value, "compaction window")
        post = _positive_int(options["post"], "summary tokens") if "post" in options else None
        builder.scalar("compaction_window", (window, post))
    elif name == "cold-resume":
        if value not in COLD_RESUME_ACTIONS:
            raise _bad(f"cold-resume must be compact or clear, not {_clip(value)}")
        min_ctx = _positive_int(options["min"], "cold-resume min") if "min" in options \
            else COLD_RESUME_MIN_DEFAULT
        builder.scalar("cold_resume", (value, min_ctx))
    elif name == "model":
        builder.keyed(builder.model, selector, _check_model_id(value), "model")
    elif name == "effort":
        if value not in EFFORT_LEVELS:
            raise _bad(f"unknown effort level {_clip(value)}")
        scale = _canonical_scale(options.get("scale", EFFORT_SCALE_DEFAULT))
        builder.keyed(builder.effort, selector, (value, scale), "effort")
    elif name in _FLAGS:
        expected, field_name = _FLAGS[name]
        if value != expected:
            raise _bad(f"{name} accepts only {expected}")
        builder.scalar(field_name, expected if field_name == "batch" else True)
    elif name == "repair":
        builder.repairs.add(_check_repair(value))
    else:  # breakpoints
        if value not in BREAKPOINT_POLICIES:
            raise _bad(f"unknown breakpoints policy {_clip(value)}")
        builder.scalar("breakpoint_policy", value)


def parse_policy(spec: str) -> Policy:
    """Parse a policy spec (§9.5) into a :class:`Policy` named by its canonical spec.

    The empty spec (or ``"observed"``) is the observed policy. Unknown clauses, options, selector
    keys or values, conflicting repeats and malformed text raise :class:`UsageError` with a short
    content-free message.
    """
    if not isinstance(spec, str):
        raise _bad("spec must be a string")
    if len(spec) > _MAX_SPEC_LEN:
        raise _bad("spec too long")
    text = spec.strip()
    builder = _Builder(_bad)
    if text == _OBSERVED_NAME:
        return builder.build()
    for clause in text.split(";"):
        clause = clause.strip()
        if clause:
            _parse_clause(clause, builder)
    return builder.build()


# ---------------------------------------------------------------------------------------------
# to_spec
# ---------------------------------------------------------------------------------------------


def _invalid(message: str) -> ContractViolation:
    return ContractViolation(f"Policy: {message}")


def _selector_of(selector: object) -> tuple[str, str]:
    """(canonical selector, spec suffix) of a stored selector; invalid → ContractViolation."""
    if not isinstance(selector, str):
        raise _invalid("selectors must be strings")
    try:
        canonical = _canonical_selector(selector)
    except UsageError:
        raise _invalid("invalid selector") from None
    return canonical, ("" if canonical == "all" else "@" + canonical)


def _sorted_entries(entries: object, width: int, what: str) -> list[tuple]:
    if not isinstance(entries, tuple):
        raise _invalid(f"{what} must be a tuple")
    out = []
    for entry in entries:
        if not isinstance(entry, tuple) or len(entry) != width or \
                not all(isinstance(x, str) for x in entry):
            raise _invalid(f"{what} entries must be {width}-tuples of str")
        out.append(entry)
    return out


def _require_int(value: object, what: str) -> int:
    if type(value) is not int or value <= 0:
        raise _invalid(f"{what} must be a positive int")
    return value


def to_spec(policy: Policy) -> str:
    """The canonical grammar string of *policy* (§9.5); ``""`` for the observed policy.

    ``Policy.spec()`` delegates here. The ``name`` field is not part of the spec. A policy that
    the grammar cannot express raises :class:`ContractViolation`.
    """
    if not isinstance(policy, Policy):
        raise _invalid("not a Policy")
    # (clause rank, canonical selector, value, text): repeated clauses sort by selector then value
    clauses: list[tuple[int, str, str, str]] = []

    def add(name: str, selector: str, value: str, text: str) -> None:
        clauses.append((CLAUSE_ORDER.index(name), selector, value, text))

    for selector, ttl in _sorted_entries(policy.ttl, 2, "ttl"):
        if ttl not in TTL_VALUES:
            raise _invalid("ttl must be 5m or 1h")
        canonical, suffix = _selector_of(selector)
        add("ttl", canonical, ttl, f"ttl={ttl}{suffix}")
    if policy.keepalive is not None:
        ka = policy.keepalive
        if not isinstance(ka, tuple) or len(ka) != 3 or not isinstance(ka[0], str):
            raise _invalid("keepalive must be (selector, interval_s, max_idle_s)")
        interval, max_idle = _require_int(ka[1], "keepalive interval"), \
            _require_int(ka[2], "keepalive max")
        canonical, suffix = _selector_of(ka[0])
        add("keepalive", canonical, "", f"keepalive={interval}s,max={max_idle}s{suffix}")
    if policy.compaction_window is not None:
        cw = policy.compaction_window
        if not isinstance(cw, tuple) or len(cw) != 2:
            raise _invalid("compaction_window must be (window, summary | None)")
        window = _require_int(cw[0], "compaction window")
        text = f"compact-window={window}"
        if cw[1] is not None:
            text += f",post={_require_int(cw[1], 'summary tokens')}"
        add("compact-window", "", "", text)
    if policy.cold_resume is not None:
        cr = policy.cold_resume
        if not isinstance(cr, tuple) or len(cr) != 2 or cr[0] not in COLD_RESUME_ACTIONS:
            raise _invalid("cold_resume must be ('compact'|'clear', min_context_tokens)")
        add("cold-resume", "", "",
            f"cold-resume={cr[0]},min={_require_int(cr[1], 'cold-resume min')}")
    for selector, model in _sorted_entries(policy.model_remap, 2, "model_remap"):
        try:
            _check_model_id(model)
        except UsageError:
            raise _invalid("invalid model id") from None
        canonical, suffix = _selector_of(selector)
        add("model", canonical, model, f"model={model}{suffix}")
    for selector, level, scale in _sorted_entries(policy.effort, 3, "effort"):
        if level not in EFFORT_LEVELS:
            raise _invalid("unknown effort level")
        try:
            canonical = _canonical_scale(scale)
        except UsageError:
            raise _invalid("invalid effort scale") from None
        selector_key, suffix = _selector_of(selector)
        add("effort", selector_key, level, f"effort={level},scale={canonical}{suffix}")
    for flag, name, text in ((policy.fast_off, "fast", "fast=off"),
                             (policy.geo_global, "geo", "geo=global"),
                             (policy.regional_to_global, "regional", "regional=global")):
        if type(flag) is not bool:
            raise _invalid(f"{name} flag must be a bool")
        if flag:
            add(name, "", "", text)
    if policy.batch is not None:
        if policy.batch != "eligible":
            raise _invalid("batch must be 'eligible' or None")
        add("batch", "", "", "batch=eligible")
    if not isinstance(policy.repairs, tuple):
        raise _invalid("repairs must be a tuple")
    for repair in policy.repairs:
        if not isinstance(repair, str):
            raise _invalid("repairs must be strings")
        try:
            _check_repair(repair)
        except UsageError:
            raise _invalid("unknown repair") from None
        add("repair", "", repair, f"repair={repair}")
    if policy.breakpoint_policy is not None:
        if policy.breakpoint_policy not in BREAKPOINT_POLICIES:
            raise _invalid("unknown breakpoint policy")
        add("breakpoints", "", "", f"breakpoints={policy.breakpoint_policy}")
    texts = [text for *_key, text in sorted(set(clauses))]
    return ";".join(texts)


# ---------------------------------------------------------------------------------------------
# combine
# ---------------------------------------------------------------------------------------------


def _conflict(message: str) -> ContractViolation:
    return ContractViolation(f"combine: {message}")


def combine(a: Policy, b: Policy) -> Policy:
    """Union of two policies (``Policy.combine`` delegates here), named by its canonical spec.

    Keyed entries (ttl, model, effort) union by canonical selector; the same selector with a
    different value, or two different non-default scalars (keepalive, compaction window, cold
    resume, batch, breakpoint policy), raise :class:`ContractViolation`. Flags OR, repairs union.
    """
    pa, pb = parse_policy(to_spec(a)), parse_policy(to_spec(b))
    builder = _Builder(_conflict)
    for p in (pa, pb):
        for selector, ttl in p.ttl:
            builder.keyed(builder.ttl, selector, ttl, "ttl")
        for selector, model in p.model_remap:
            builder.keyed(builder.model, selector, model, "model")
        for selector, level, scale in p.effort:
            builder.keyed(builder.effort, selector, (level, scale), "effort")
        builder.repairs.update(p.repairs)
        for name in ("keepalive", "compaction_window", "cold_resume", "batch",
                     "breakpoint_policy"):
            value = getattr(p, name)
            if value is not None:
                builder.scalar(name, value)
        for name in ("fast_off", "geo_global", "regional_to_global"):
            if getattr(p, name):
                builder.scalars[name] = True
    return builder.build()
