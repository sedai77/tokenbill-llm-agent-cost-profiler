"""Post-rollout effectiveness checks (SPEC §11.5; ``tokenbill policy check-effect``).

A managed setting can be delivered and still not take effect: a gateway strips the
``anthropic-beta`` header, the Claude apps gateway cannot use the 1h TTL, clients are below the
key's minimum version, users override a default per session. :func:`check_effect` looks at the
cohort's lanes in the ``days`` after rollout and returns a ``setting-not-effective`` finding when
the observed behavior does not match the rolled-out value:

====================================  =========================================================
lever                                 check
====================================  =========================================================
``cc.prompt_cache_ttl.main`` /        share of cache writes at the target TTL (default 1h)
``cc.prompt_cache_ttl.subagent`` /    among the lever's lanes' write tokens must exceed 90%
``sdk.ttl``
``cc.autocompact_window``             median COMPACTION ``pre_tokens`` ≤ window × 1.05
``cc.default_model``                  share of main-lane requests on the target model > 80%
``cc.default_effort`` /               share of main-lane requests at or below the level > 80%
``cc.max_effort``                     (requests without a known effort are left out)
====================================  =========================================================

The rolled-out value is ``target`` (keyword) or a ``=<value>`` suffix of ``lever_id`` (e.g.
``cc.autocompact_window=400000``); TTL levers default to ``1h`` and ``cc.default_model`` to the
catalog's grid model; the window and effort levers need it (``UsageError`` otherwise; see
``tests/v2/plan/CONTRACT-CHANGE-PLAN.md``). ``cohort`` is ``all``, a team or an MDM group. No
evidence in the window (no writes, no compactions, no requests) gives None. The finding is
content-free (shares and counts only) and carries no dollar figure (``cost_observed`` is unpriced:
the check compares token shares, not money).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from fractions import Fraction

from tokenbill.core.catalog import lever
from tokenbill.core.errors import UsageError
from tokenbill.core.findings import build_finding, make_scope
from tokenbill.core.labels import Basis, unpriced
from tokenbill.core.models import normalize_model
from tokenbill.core.policy import EFFORT_LEVELS, lane_matches, parse_policy
from tokenbill.core.records import Lane, LaneEventKind, Request
from tokenbill.core.types import EvidenceItem, Finding, Fix

__all__ = [
    "CHECKS",
    "DETECTOR_ID",
    "DETECTOR_VERSION",
    "KIND",
    "MODEL_SHARE_MIN_PCT",
    "TTL_SHARE_MIN_PCT",
    "WINDOW_SLACK_PCT",
    "check_effect",
]

DETECTOR_ID = "plan.effectiveness"
DETECTOR_VERSION = "1"
KIND = "setting-not-effective"
#: The TTL share that must be exceeded (percent).
TTL_SHARE_MIN_PCT = 90
#: The model / effort share that must be exceeded (percent).
MODEL_SHARE_MIN_PCT = 80
#: Median compaction point may exceed the window by this much (percent).
WINDOW_SLACK_PCT = 5
_DAY_MS = 86_400_000
_TTL_LEVERS = ("cc.prompt_cache_ttl.main", "cc.prompt_cache_ttl.subagent", "sdk.ttl")
#: Levers with an effectiveness check (SPEC §11.5).
CHECKS = _TTL_LEVERS + ("cc.autocompact_window", "cc.default_model", "cc.default_effort",
                        "cc.max_effort")
_CC_MAIN = "agent_product:claude_code,lane_kind:main"
_TTL_SELECTORS = {
    "cc.prompt_cache_ttl.main": (_CC_MAIN,),
    "cc.prompt_cache_ttl.subagent": ("agent_product:claude_code,lane_kind:subagent",
                                     "agent_product:claude_code,lane_kind:workflow_agent"),
    "sdk.ttl": ("lane_kind:api_run",),
}
_TTL_CAUSES = ("gateway strips the anthropic-beta header? Claude apps gateway cannot use 1h? "
               "client below minimum version?")
_SETTINGS_DOC = "https://code.claude.com/docs/en/settings-reference"
_EFFORT_RANK = {level: i for i, level in enumerate(EFFORT_LEVELS)}
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}\Z")


def _split(lever_id: str, target: str | None) -> tuple[str, str | None]:
    if not isinstance(lever_id, str) or not lever_id:
        raise UsageError("lever_id must be a non-empty string")
    base, eq, suffix = lever_id.partition("=")
    if eq:
        if target is not None and target != suffix:
            raise UsageError("check_effect: lever_id suffix and target disagree")
        target = suffix
    if target is not None and (not isinstance(target, str) or not target.strip()):
        raise UsageError("check_effect: target must be a non-empty string")
    return base.strip(), target.strip() if target is not None else None


def _in_cohort(lane: Lane, cohort: str) -> bool:
    if cohort == "all":
        return True
    if not lane.requests:
        return False
    attr = lane.requests[0].attribution
    return attr.team == cohort or dict(attr.extra).get("mdm_group") == cohort


def _requests(lane: Lane, lo: int, hi: int) -> list[Request]:
    return [r for r in lane.requests if lo <= r.ts_start_ms < hi]


def _pct(num: int, den: int) -> str:
    """A percentage with one decimal, half-even, from exact integers."""
    tenths = Fraction(num * 1000, den)
    whole = tenths.numerator // tenths.denominator
    rest = tenths - whole
    if rest > Fraction(1, 2) or (rest == Fraction(1, 2) and whole % 2 == 1):
        whole += 1
    return f"{whole // 10}.{whole % 10}"


def _principals(lanes: Sequence[Lane]) -> int:
    return len({ln.requests[0].attribution.principal for ln in lanes
                if ln.requests and ln.requests[0].attribution.principal})


def _finding(lever_id: str, cohort: str, lane_kind: str | None, lanes: Sequence[Lane],
             since_ms: int, n_events: int, title: str, summary: str,
             attrs: tuple[tuple[str, str | int], ...], references: tuple[str, ...],
             fix_text: str) -> Finding:
    ldef = lever(lever_id)
    return build_finding(
        detector_id=DETECTOR_ID, kind=KIND, detector_version=DETECTOR_VERSION, category="lever",
        lever_class=ldef.lever_class, audience="org", title=title, summary=summary,
        scope=make_scope(cohort=cohort, lane_kind=lane_kind, lever=lever_id),
        n_events=n_events, n_lanes=len(lanes), n_users=_principals(lanes),
        first_seen_ms=since_ms,
        cost_observed=unpriced("the effectiveness check compares token shares, not money",
                               Basis.LIST),
        recoverable=None, lever_ids=(lever_id,),
        evidence=(EvidenceItem(kind="aggregate", ref=f"effectiveness:{lever_id}", attrs=attrs),),
        fix=Fix(text=fix_text, config_patch=None, target="claude-code-managed-settings",
                doc_url=_SETTINGS_DOC),
        confidence="medium", references=references)


def _ttl_check(lever_id: str, target: str, lanes: Sequence[Lane], cohort: str, lo: int,
               hi: int) -> Finding | None:
    if target not in ("5m", "1h"):
        raise UsageError("TTL levers are checked against 5m or 1h")
    selectors = _TTL_SELECTORS[lever_id]
    mine = [ln for ln in lanes if any(lane_matches(sel, ln) for sel in selectors)]
    at_target = total = n = 0
    used = []
    for ln in mine:
        reqs = _requests(ln, lo, hi)
        if reqs:
            used.append(ln)
        for r in reqs:
            n += 1
            for inf in r.billable_inferences:
                u = inf.usage
                writes = (u.cache_write_5m + u.cache_write_1h + u.cache_write_other
                          + u.cache_write_unknown)
                total += writes
                at_target += u.cache_write_1h if target == "1h" else u.cache_write_5m
    if total == 0:
        return None
    if at_target * 100 > TTL_SHARE_MIN_PCT * total:
        return None
    share = _pct(at_target, total)
    kind = "main" if lever_id == "cc.prompt_cache_ttl.main" else (
        "subagent" if lever_id == "cc.prompt_cache_ttl.subagent" else "api_run")
    return _finding(
        lever_id, cohort, kind, used, lo, n,
        title=f"TTL {target} is not in effect: {share}% of cache writes at {target}",
        summary=(f"After rollout, {share}% of {total} cache-write tokens on {len(used)} lanes "
                 f"were written at {target} (needs more than {TTL_SHARE_MIN_PCT}%). "
                 f"{_TTL_CAUSES}"),
        attrs=(("share_pct", share), ("threshold_pct", str(TTL_SHARE_MIN_PCT)),
               ("target", target), ("write_tokens", total)),
        references=("cc-ttl-managed-settings", "cc-gateway-cache-strip"),
        fix_text=(f"Check that the cohort's clients read the setting ({_TTL_CAUSES}) and that "
                  "the TTL key is delivered to this cohort."))


def _median_twice(values: Sequence[int]) -> int:
    """Twice the median (exact for even counts)."""
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return 2 * ordered[mid] if n % 2 else ordered[mid - 1] + ordered[mid]


def _window_check(target: str, lanes: Sequence[Lane], cohort: str, lo: int,
                  hi: int) -> Finding | None:
    if not target.isdigit() or not 0 < int(target) <= 10**7:
        raise UsageError("cc.autocompact_window is checked against a window in tokens")
    window = int(target)
    mine = [ln for ln in lanes if lane_matches(_CC_MAIN, ln)]
    pre: list[int] = []
    used = []
    for ln in mine:
        got = [ev for ev in ln.events if ev.kind is LaneEventKind.COMPACTION
               and lo <= ev.ts_ms < hi]
        vals = [v for ev in got for k, v in ev.attrs if k == "pre_tokens" and type(v) is int]
        if vals:
            used.append(ln)
            pre += vals
    if not pre:
        return None
    twice = _median_twice(pre)
    # median ≤ window × 1.05  ⇔  2·median·100 ≤ 2·window·105
    if twice * 100 <= 2 * window * (100 + WINDOW_SLACK_PCT):
        return None
    median = f"{twice // 2}" if twice % 2 == 0 else f"{twice // 2}.5"
    return _finding(
        "cc.autocompact_window", cohort, "main", used, lo, len(pre),
        title=f"Compaction window {window} is not in effect",
        summary=(f"After rollout the median compaction started at {median} tokens over "
                 f"{len(pre)} compactions (window {window}, allowed up to "
                 f"{100 + WINDOW_SLACK_PCT}%). Is env.CLAUDE_CODE_AUTO_COMPACT_WINDOW or "
                 "--autocompact overriding it, or are clients below the minimum version?"),
        attrs=(("median_pre_tokens", median), ("window", window),
               ("compactions", len(pre))),
        references=("cc-autocompact-window",),
        fix_text=("Deliver autoCompactWindow together with env.CLAUDE_CODE_AUTO_COMPACT_WINDOW; "
                  "the env var takes precedence over the setting and the --autocompact flag."))


def _share_check(lever_id: str, lanes: Sequence[Lane], cohort: str, lo: int, hi: int,
                 matches: Callable[[Request], bool | None], title: str, what: str,
                 attrs: tuple[tuple[str, str | int], ...]) -> Finding | None:
    mine = [ln for ln in lanes if lane_matches(_CC_MAIN, ln)]
    hit = known = 0
    used = []
    for ln in mine:
        reqs = _requests(ln, lo, hi)
        counted = False
        for r in reqs:
            got = matches(r)
            if got is None:
                continue
            counted = True
            known += 1
            hit += 1 if got else 0
        if counted:
            used.append(ln)
    if known == 0:
        return None
    if hit * 100 > MODEL_SHARE_MIN_PCT * known:
        return None
    share = _pct(hit, known)
    return _finding(
        lever_id, cohort, "main", used, lo, known,
        title=f"{title}: {share}% of main-lane requests",
        summary=(f"After rollout {share}% of {known} main-lane requests {what} (needs more "
                 f"than {MODEL_SHARE_MIN_PCT}%). Users may override the default per session "
                 "(--model, /model, --effort), or clients may not read the setting."),
        attrs=(("share_pct", share), ("threshold_pct", str(MODEL_SHARE_MIN_PCT)),
               ("requests", known)) + attrs,
        references=("cc-model-effort-mix",),
        fix_text=("Check the cohort's client versions and whether per-session overrides are "
                  "common; enforce with availableModels / maxEffortLevel only after an eval."))


def _default_model(ldef_grid: Sequence[str]) -> str | None:
    for spec in ldef_grid:
        policy = parse_policy(spec)
        if policy.model_remap:
            return policy.model_remap[0][1]
    return None  # pragma: no cover - the catalog grid always remaps


def _norm(model: str) -> str:
    return normalize_model(model).model or model


def check_effect(lanes_after: Sequence[Lane], *, lever_id: str, cohort: str, since_ms: int,
                 days: int = 7, target: str | None = None) -> Finding | None:
    """A ``setting-not-effective`` finding when the rolled-out value of *lever_id* is not
    observed in *cohort*'s lanes within ``[since_ms, since_ms + days)``; None when it is, or when
    there is no evidence (see the module docstring). Levers without a check, malformed values and
    windows raise :class:`UsageError`."""
    base, value = _split(lever_id, target)
    if not isinstance(cohort, str) or not cohort:
        raise UsageError("cohort must be a non-empty string ('all', a team or an MDM group)")
    if type(since_ms) is not int or since_ms < 0:
        raise UsageError("since_ms must be a non-negative int (ms since the epoch)")
    if type(days) is not int or not 1 <= days <= 366:
        raise UsageError("days must be an int in [1, 366]")
    lanes = [ln for ln in lanes_after if isinstance(ln, Lane) and _in_cohort(ln, cohort)]
    lo, hi = since_ms, since_ms + days * _DAY_MS
    if base in _TTL_LEVERS:
        return _ttl_check(base, value or "1h", lanes, cohort, lo, hi)
    if base == "cc.autocompact_window":
        if value is None:
            raise UsageError("cc.autocompact_window needs the rolled-out window: pass target= "
                             "or lever_id='cc.autocompact_window=<tokens>'")
        return _window_check(value, lanes, cohort, lo, hi)
    if base == "cc.default_model":
        model = value or _default_model(lever(base).grid)
        if model is None or not _MODEL_RE.match(model):
            raise UsageError("cc.default_model is checked against a model id")
        want = _norm(model)
        return _share_check(
            base, lanes, cohort, lo, hi,
            lambda r: r.serving_inference is not None and _norm(r.model) == want,
            title="The default model is not in effect", what=f"ran on {model}",
            attrs=(("target", model),))
    if base in ("cc.default_effort", "cc.max_effort"):
        if value not in _EFFORT_RANK:
            raise UsageError(f"{base} needs the rolled-out effort level (one of "
                             f"{', '.join(EFFORT_LEVELS)}): pass target= or "
                             f"lever_id='{base}=<level>'")
        limit = _EFFORT_RANK[value]

        def at_or_below(r: Request) -> bool | None:
            level = r.params.effort or r.params.session_effort
            rank = _EFFORT_RANK.get(level) if level is not None else None
            return None if rank is None else rank <= limit

        return _share_check(base, lanes, cohort, lo, hi, at_or_below,
                            title=f"Effort {value} is not in effect",
                            what=f"ran at effort {value} or below", attrs=(("target", value),))
    raise UsageError(f"no effectiveness check for lever {base!r}")

