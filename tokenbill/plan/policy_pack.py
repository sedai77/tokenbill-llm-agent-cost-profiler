"""Per-cohort policy packs: managed-settings patches, hook, snippets, README (SPEC §11.3, §11.4).

:func:`build_policy_packs` turns an :class:`~tokenbill.core.types.ActionPlan` and its findings into
deployable, **never auto-applied** packs; :func:`render_pack` writes one pack to disk.

Target ``claude-code`` (one pack per cohort):

- ``managed-settings.patch.json`` — an RFC 7386 JSON merge patch against ``current`` (only changed
  keys; ``env.*`` keys nested under ``"env"``, ``hooks.SessionStart`` under ``"hooks"``), and
  ``rollback.patch.json``, the merge patch that restores ``current`` (``null`` for keys that were
  absent). Applying the patch and then the rollback gives back ``current`` exactly.
- Keys come only from ``core.catalog.ALLOWLIST`` (an unknown key, also in a finding's
  ``Fix.config_patch``, raises :class:`UsageError`); values are checked against the key's domain.
  Keys whose ``verified`` flag (``core/facts.json``) is False are never written into JSON: they
  appear in the README as commented guidance ("VERIFY against settings-reference before applying").
- Trade-off levers (``LeverDef.tradeoff``: default model / effort, max effort, subagent model,
  compaction window, same-tier upgrade) are left out unless ``include_tradeoffs``; the README names
  their primary key and requires an ``ab`` or ``measure`` gate. ``autoCompactWindow`` is always
  paired with ``env.CLAUDE_CODE_AUTO_COMPACT_WINDOW``; ``availableModels`` +
  ``enforceAvailableModels`` accompany ``model`` only with ``include_tradeoffs``.
- ``env.OTEL_RESOURCE_ATTRIBUTES`` gets ``tokenbill.arm=<levers>,tokenbill.wave=1`` (existing
  attributes kept) so ``measure`` can attribute the rollout.
- ``hooks/tokenbill_session_start.py`` (copied from ``plan/templates/``) when the cold-resume hook
  levers are in the plan; ``litellm-config.patch.yaml`` when caching behind a gateway is to be
  restored; ``modelPricing`` from ``contract`` through the injected emitter when that key is
  verified.

Cohorts: ``cohort_by`` ``team`` or ``mdm-group`` (scope dims ``team`` / ``mdm_group`` of the
findings); a ``ttl-heterogeneous`` finding forces per-cohort packs (``team`` by default). A cohort
pack carries its findings' patches (their values win) and the plan levers those findings link; the
``all`` pack carries the org-wide levers whose keys no cohort pack sets. Targets ``litellm`` and
``sdk`` give one ``all`` pack (gateway and SDK configuration are not delivered per MDM group).

Every entry carries its projection Figure (with its label), ``needs_eval``, the minimum Claude Code
version, and a rollout note: server-managed settings apply org-wide; per-group delivery uses
MDM/endpoint files or the Claude apps gateway per IdP group; an org-wide change is measurable only
with ``tokenbill measure plan --design its`` (MEASURED at best). Packs are content-free: they hold
lever ids, allowlisted keys, validated values, figures, finding ids and cohort names only.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from tokenbill.core.catalog import AllowedKey, LeverDef, allowed, lever
from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis, Calibration, Figure
from tokenbill.core.money import fmt_usd
from tokenbill.core.policy import EFFORT_LEVELS, parse_policy
from tokenbill.core.textsafe import sanitize
from tokenbill.core.types import ActionPlan, ContractOverlay, Finding, PolicyEntry, PolicyPack
from tokenbill.plan.litellm import render_injection_points, validate_litellm_fragment
from tokenbill.plan.realization import crosses_zero

__all__ = [
    "COHORT_BY",
    "HOOK_COMMAND",
    "HOOK_PATH",
    "LITELLM_FILE",
    "PATCH_FILE",
    "README_FILE",
    "ROLLBACK_FILE",
    "TARGETS",
    "apply_merge_patch",
    "build_policy_packs",
    "canonical_json",
    "make_merge_patch",
    "render_pack",
    "snippet",
]

#: Targets PLAN builds (``github-copilot`` packs come from the Copilot extension).
TARGETS = ("claude-code", "litellm", "sdk")
#: ``--cohort-by`` values and the finding scope dim each one reads.
COHORT_BY = ("team", "mdm-group")
_COHORT_DIMS = {"team": "team", "mdm-group": "mdm_group"}
PATCH_FILE = "managed-settings.patch.json"
ROLLBACK_FILE = "rollback.patch.json"
README_FILE = "README.md"
LITELLM_FILE = "litellm-config.patch.yaml"
#: Where the pack puts the hook, and the command managed settings run (MDM copies the file to
#: every developer's ``~/.claude/hooks/``).
HOOK_PATH = "hooks/tokenbill_session_start.py"
HOOK_COMMAND = "python3 ~/.claude/hooks/tokenbill_session_start.py"
_HOOK_TEMPLATE = "tokenbill_session_start.py"
_TEMPLATES = Path(__file__).resolve().parent / "templates"
_SNIPPETS = _TEMPLATES / "snippets"
_WAVE = "1"
_HOOK_LEVERS = ("cc.cold_resume_hook", "cc.compact_on_resume")
_GATEWAY_LEVER = "gateway.restore_caching"
_GATEWAY_KINDS = frozenset({"no-cache", "beta-header-dropped"})
#: The fragment's model name when no finding names the affected models.
_MODEL_PLACEHOLDER = "REPLACE-WITH-YOUR-MODEL-NAME"
_TTL_LEVERS = ("cc.prompt_cache_ttl.main", "cc.prompt_cache_ttl.subagent", "sdk.ttl")
_CHECKED_LEVERS = frozenset(_TTL_LEVERS + ("cc.autocompact_window", "cc.default_model",
                                           "cc.default_effort", "cc.max_effort"))
_REPORTING = "reporting.model_pricing"
_VERIFICATION = "verification"
_SETTINGS_TARGET = "claude-code"
_FIX_TARGETS = frozenset({None, "claude-code-managed-settings"})
_OTEL_KEY = "env.OTEL_RESOURCE_ATTRIBUTES"
_ROLLOUT_NOTE = ("rollout: server-managed settings apply org-wide; deliver per group via "
                 "MDM/endpoint files or the Claude apps gateway per IdP group; an org-wide change "
                 "is measurable only with `tokenbill measure plan --design its` (MEASURED at best)")
_VERIFY_TEXT = "VERIFY against settings-reference before applying"
_TRADEOFF_TEXT = ("trade-off: excluded unless --include-tradeoffs; requires an `ab` or `measure` "
                  "gate before rollout")
_MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}\Z")
_SAFE_STR_RE = re.compile(r"[^\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]{0,512}\Z")
_ATTR_RE = re.compile(r"[A-Za-z0-9_.\-]{1,128}\Z")
_ATTR_VALUE_RE = re.compile(r"[A-Za-z0-9_.+:/@%\-]{0,256}\Z")
_COHORT_RE = re.compile(r"[^\x00-\x1f\x7f-\x9f/\\\u202a-\u202e\u2066-\u2069]{1,128}\Z")
_DIR_RE = re.compile(r"[^A-Za-z0-9._-]")
_TTLS = ("5m", "1h")
_EFFORT_LEVEL = ("low", "medium", "high", "xhigh")
_MISSING = object()


# ---------------------------------------------------------------------------------------------
# JSON: canonical text (no floats) and RFC 7386 merge patches
# ---------------------------------------------------------------------------------------------


def _json_value(obj: object) -> object:
    """A deep copy of *obj* restricted to JSON types (dict, list, str, int, bool, Decimal);
    floats become Decimal (never emitted as floats); nulls and other types raise."""
    if isinstance(obj, bool) or isinstance(obj, (str, int, Decimal)):
        if isinstance(obj, Decimal) and not obj.is_finite():
            raise UsageError("settings values must be finite numbers")
        return obj
    if isinstance(obj, float):
        value = Decimal(repr(obj))
        if not value.is_finite():
            raise UsageError("settings values must be finite numbers")
        return value
    if isinstance(obj, Mapping):
        out: dict[str, object] = {}
        for key, value in obj.items():
            if not isinstance(key, str):
                raise UsageError("settings object keys must be strings")
            out[key] = _json_value(value)
        return out
    if isinstance(obj, (list, tuple)):
        return [_json_value(v) for v in obj]
    if obj is None:
        raise UsageError("settings contain null values, which a JSON merge patch cannot restore")
    raise UsageError("settings values must be JSON values")


def _enc(obj: object, indent: int | None, level: int) -> str:
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, Decimal):
        return format(obj.normalize(), "f") if obj != obj.to_integral_value() else \
            str(int(obj))
    if isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        items = [(json.dumps(k, ensure_ascii=False), _enc(obj[k], indent, level + 1))
                 for k in sorted(obj)]
        if indent is None:
            return "{" + ",".join(f"{k}:{v}" for k, v in items) + "}"
        pad, inner = " " * (indent * level), " " * (indent * (level + 1))
        return "{\n" + ",\n".join(f"{inner}{k}: {v}" for k, v in items) + f"\n{pad}}}"
    if isinstance(obj, list):
        if not obj:
            return "[]"
        vals = [_enc(v, indent, level + 1) for v in obj]
        if indent is None:
            return "[" + ",".join(vals) + "]"
        pad, inner = " " * (indent * level), " " * (indent * (level + 1))
        return "[\n" + ",\n".join(f"{inner}{v}" for v in vals) + f"\n{pad}]"
    raise UsageError("not a JSON value")  # pragma: no cover - inputs pass _json_value


def canonical_json(obj: object, *, indent: int | None = None) -> str:
    """JSON text with sorted keys, no floats (Decimal numbers verbatim), UTF-8; compact unless
    *indent* is given. ``None`` values (merge-patch deletions) are written as ``null``."""
    return _enc(obj, indent, 0)


def apply_merge_patch(target: object, patch: object) -> object:
    """RFC 7386: apply merge *patch* to *target* (returns a new value; inputs are not mutated)."""
    if not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = apply_merge_patch(result.get(key), value)
    return result


def make_merge_patch(source: object, target: object) -> object:
    """The RFC 7386 merge patch turning *source* into *target* (only changed members; ``None`` for
    members absent from *target*). *target* must contain no ``None`` values."""
    if not isinstance(source, dict) or not isinstance(target, dict):
        return copy.deepcopy(target)
    patch: dict[str, object] = {}
    for key in source:
        if key not in target:
            patch[key] = None
    for key, value in target.items():
        if key not in source:
            patch[key] = copy.deepcopy(value)
        elif source[key] != value:
            patch[key] = make_merge_patch(source[key], value)
    return patch


def _path(key: str) -> tuple[str, ...]:
    head, dot, tail = key.partition(".")
    return (head, tail) if dot else (key,)


def _get(doc: Mapping[str, object], key: str) -> object:
    node: object = doc
    for part in _path(key):
        if not isinstance(node, Mapping) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _set(doc: dict[str, object], key: str, value: object) -> None:
    parts = _path(key)
    node = doc
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            if child is not None:
                raise UsageError(f"current settings: {part!r} is not an object")
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


# ---------------------------------------------------------------------------------------------
# key domains
# ---------------------------------------------------------------------------------------------


def _is_model(value: object) -> bool:
    return isinstance(value, str) and bool(_MODEL_ID_RE.match(value))


def _int_in(value: object, lo: int, hi: int) -> bool:
    return type(value) is int and lo <= value <= hi


def _digits_in(value: object, lo: int, hi: int) -> bool:
    return isinstance(value, str) and value.isdigit() and len(value) <= 16 and \
        lo <= int(value) <= hi


def _safe_str(value: object) -> bool:
    return isinstance(value, str) and bool(_SAFE_STR_RE.match(value))


def _check_hooks(value: object) -> bool:
    if not isinstance(value, list) or len(value) > 64:
        return False
    for item in value:
        if not isinstance(item, dict) or not _safe_str(item.get("matcher", "")):
            return False
        hooks = item.get("hooks")
        if not isinstance(hooks, list) or not hooks:
            return False
        for hook in hooks:
            if not isinstance(hook, dict) or hook.get("type") != "command" or \
                    not _safe_str(hook.get("command")) or not hook.get("command"):
                return False
    return True


def _check_model_pricing(value: object) -> bool:
    if not isinstance(value, dict) or not value or set(value) - {"multiplier", "overrides"}:
        return False
    mult = value.get("multiplier", _MISSING)
    if mult is not _MISSING and not (isinstance(mult, (int, Decimal))
                                     and not isinstance(mult, bool) and 0 < mult <= 10):
        return False
    overrides = value.get("overrides", {})
    if not isinstance(overrides, dict):
        return False
    for model, rates in overrides.items():
        if not _is_model(model) or not isinstance(rates, dict):
            return False
        for name, rate in rates.items():
            if name not in ("input", "output", "cacheRead", "cacheWrite"):
                return False
            if isinstance(rate, bool) or not isinstance(rate, (int, Decimal)) or rate < 0:
                return False
    return True


_DOMAIN_CHECKS: Mapping[str, Callable[[object], bool]] = {
    "autoCompactWindow": lambda v: _int_in(v, 100_000, 1_000_000),
    "env.CLAUDE_CODE_AUTO_COMPACT_WINDOW": lambda v: _digits_in(v, 100_000, 1_000_000),
    "env.CLAUDE_CODE_DISABLE_1M_CONTEXT": lambda v: v == "1",
    "promptCacheTtl": lambda v: v in _TTLS,
    "subagentPromptCacheTtl": lambda v: v in _TTLS,
    "env.CLAUDE_CODE_PROMPT_CACHE_TTL": lambda v: v in _TTLS,
    "env.CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL": lambda v: v in _TTLS,
    "env.ENABLE_PROMPT_CACHING_1H": lambda v: v == "1",
    "env.FORCE_PROMPT_CACHING_5M": lambda v: v == "1",
    "model": _is_model,
    "effortLevel": lambda v: v in _EFFORT_LEVEL,
    "env.CLAUDE_CODE_SUBAGENT_MODEL": _is_model,
    "env.ANTHROPIC_DEFAULT_OPUS_MODEL": _is_model,
    "env.ANTHROPIC_DEFAULT_SONNET_MODEL": _is_model,
    "env.ANTHROPIC_DEFAULT_HAIKU_MODEL": _is_model,
    "availableModels": lambda v: isinstance(v, list) and 0 < len(v) <= 64 and all(
        _is_model(m) for m in v),
    "enforceAvailableModels": lambda v: isinstance(v, bool),
    "maxEffortLevel": lambda v: v in EFFORT_LEVELS,
    "fastModePerSessionOptIn": lambda v: isinstance(v, bool),
    "env.CLAUDE_CODE_DISABLE_FAST_MODE": lambda v: v == "1",
    "modelPricing": _check_model_pricing,
    "bashOutputMaxChars": lambda v: _int_in(v, 1, 10**9),
    "env.MAX_MCP_OUTPUT_TOKENS": lambda v: _digits_in(v, 1, 10**9),
    "env.ENABLE_TOOL_SEARCH": lambda v: isinstance(v, str) and bool(
        re.match(r"(?:true|false|auto|auto:\d{1,6})\Z", v)),
    "skillListingBudgetFraction": lambda v: isinstance(v, (int, Decimal)) and not isinstance(
        v, bool) and 0 < v <= 1,
    "claudeMdExcludes": lambda v: isinstance(v, list) and len(v) <= 256 and all(
        _safe_str(p) and p for p in v),
    "cleanupPeriodDays": lambda v: _int_in(v, 1, 100_000),
    "env.CLAUDE_CODE_ENABLE_TELEMETRY": lambda v: v == "1",
    "env.OTEL_RESOURCE_ATTRIBUTES": lambda v: isinstance(v, str) and _attrs_ok(v),
    "env.OTEL_METRICS_INCLUDE_ENTRYPOINT": lambda v: v == "true",
    "hooks.SessionStart": _check_hooks,
}


def _attrs_ok(text: str) -> bool:
    if not text:
        return True
    for part in text.split(","):
        name, eq, value = part.partition("=")
        if not eq or not _ATTR_RE.match(name.strip()) or not _ATTR_VALUE_RE.match(value.strip()):
            return False
    return True


def _allowed_key(key: object) -> AllowedKey:
    if not isinstance(key, str):
        raise UsageError("settings keys must be strings")
    entry = allowed(key)   # UsageError for a key outside the allowlist
    if entry.target != _SETTINGS_TARGET:
        raise UsageError(f"settings key {key!r} is not a Claude Code managed-settings key")
    return entry


def _check_value(key: str, value: object) -> None:
    check = _DOMAIN_CHECKS.get(key)
    ok = check(value) if check is not None else _generic_ok(value)
    if not ok:
        raise UsageError(f"settings key {key!r}: value outside its domain")


def _generic_ok(value: object, depth: int = 0) -> bool:
    if depth > 8:
        return False
    if isinstance(value, str):
        return _safe_str(value)
    if isinstance(value, (bool, int, Decimal)):
        return True
    if isinstance(value, list):
        return len(value) <= 256 and all(_generic_ok(v, depth + 1) for v in value)
    if isinstance(value, dict):
        return len(value) <= 256 and all(_safe_str(k) and _generic_ok(v, depth + 1)
                                         for k, v in value.items())
    return False


# ---------------------------------------------------------------------------------------------
# proposals
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Proposal:
    key: str
    value: object
    lever_id: str
    projection: Figure | None
    needs_eval: bool
    tradeoff: bool
    primary: bool
    note: str


def _tier_key(model: str) -> str | None:
    for tier in ("opus", "sonnet", "haiku"):
        if f"-{tier}-" in f"-{model}-" or model.startswith(f"claude-{tier}"):
            return f"env.ANTHROPIC_DEFAULT_{tier.upper()}_MODEL"
    return None


def _hook_value() -> list[dict[str, object]]:
    return [{"matcher": "resume", "hooks": [{"type": "command", "command": HOOK_COMMAND}]}]


def _lever_settings(ldef: LeverDef, params: str) -> list[tuple[str, object, bool]]:
    """``(key, value, primary)`` settings delivering *ldef* at *params* (a canonical policy spec);
    empty for levers delivered by snippets or policy decisions."""
    lid = ldef.lever_id
    if lid in _HOOK_LEVERS:
        return [("hooks.SessionStart", _hook_value(), True)]
    if lid == "cc.tool_search":
        return [("env.ENABLE_TOOL_SEARCH", "true", True)]
    if not params:
        return []
    policy = parse_policy(params)
    if lid == "cc.prompt_cache_ttl.main" and policy.ttl:
        return [("promptCacheTtl", policy.ttl[0][1], True)]
    if lid == "cc.prompt_cache_ttl.subagent" and policy.ttl:
        return [("subagentPromptCacheTtl", policy.ttl[0][1], True)]
    if lid == "cc.autocompact_window" and policy.compaction_window is not None:
        window = policy.compaction_window[0]
        return [("autoCompactWindow", window, True),
                ("env.CLAUDE_CODE_AUTO_COMPACT_WINDOW", str(window), True)]
    if lid == "cc.default_model" and policy.model_remap:
        model = policy.model_remap[0][1]
        return [("model", model, True), ("availableModels", [model], False),
                ("enforceAvailableModels", True, False)]
    if lid == "cc.default_effort" and policy.effort:
        return [("effortLevel", policy.effort[0][1], True)]
    if lid == "cc.max_effort" and policy.effort:
        return [("maxEffortLevel", policy.effort[0][1], True)]
    if lid == "cc.subagent_model" and policy.model_remap:
        return [("env.CLAUDE_CODE_SUBAGENT_MODEL", policy.model_remap[0][1], True)]
    if lid == "model.same_tier_upgrade":
        out: list[tuple[str, object, bool]] = []
        for _sel, target in policy.model_remap:
            key = _tier_key(target)
            if key is not None and all(k != key for k, _, _ in out):
                out.append((key, target, True))
        return out
    if lid == "cc.fast_mode_opt_in" and policy.fast_off:
        return [("fastModePerSessionOptIn", True, True)]
    return []


def _lever_proposals(ldef: LeverDef, params: str, projection: Figure | None,
                     note: str) -> list[_Proposal]:
    out = []
    for key, value, primary in _lever_settings(ldef, params):
        entry = _allowed_key(key)
        value = _json_value(value)
        _check_value(key, value)
        out.append(_Proposal(key=key, value=value, lever_id=ldef.lever_id, projection=projection,
                             needs_eval=ldef.needs_eval, tradeoff=ldef.tradeoff or entry.tradeoff,
                             primary=primary, note=note))
    return out


def _no_constant(_name: str) -> object:
    raise ValueError("NaN and Infinity are not JSON values")


def _parse_patch_value(text: object) -> object:
    if not isinstance(text, str):
        raise UsageError("a fix config_patch value must be a JSON string")
    try:
        value = json.loads(text, parse_float=Decimal, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        raise UsageError("a fix config_patch value is not valid JSON") from None
    return _json_value(value)


def _finding_levers(f: Finding) -> list[LeverDef]:
    out = []
    for lid in f.lever_ids:
        try:
            ldef = lever(lid)
        except UsageError:
            continue
        if ldef.replay != "aggregate":
            out.append(ldef)
    return out


def _finding_proposals(f: Finding) -> list[_Proposal]:
    """Proposals from a finding's ``Fix.config_patch`` (Claude Code managed settings only)."""
    fix = f.fix
    if fix is None or not fix.config_patch or fix.target not in _FIX_TARGETS:
        return []
    levers = _finding_levers(f)
    out = []
    for key, text in fix.config_patch:
        entry = _allowed_key(key)
        value = _parse_patch_value(text)
        _check_value(key, value)
        owner = next((lv for lv in levers if key in lv.patch_keys), None)
        if owner is None and levers:
            owner = levers[0]
        lever_id = owner.lever_id if owner is not None else f"fix:{f.detector_id}"
        tradeoff = entry.tradeoff or any(lv.tradeoff for lv in levers)
        needs_eval = f.needs_eval or any(lv.needs_eval for lv in levers)
        note = f"from finding {f.finding_id} ({f.kind})"
        if f.kind == "ttl-heterogeneous":
            note += ("; heterogeneous: only some principals gain, deliver to the MDM / IdP groups "
                     "that gain and measure first")
        out.append(_Proposal(key=key, value=value, lever_id=lever_id,
                             projection=f.projected_monthly, needs_eval=needs_eval,
                             tradeoff=tradeoff, primary=True, note=note))
    return out


# ---------------------------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------------------------


def _fmt_fig(fig: Figure | None) -> str:
    if fig is None:
        return "not projected for this cohort (see the findings)"
    if fig.nano is None:
        reason = sanitize(fig.note.split(";")[0].removeprefix("unpriced:").strip(), 160)
        reason = reason.replace(";", ",")
        return f"unpriced ({reason})" if reason else "unpriced"
    text = fmt_usd(fig.nano) + "/month"
    if fig.low_nano is not None and fig.high_nano is not None:
        text += f" (range {fmt_usd(fig.low_nano)} to {fmt_usd(fig.high_nano)})"
    labels = [fig.evidence.value, fig.basis.value]
    if fig.calibration is not Calibration.NA:
        labels.append(fig.calibration.value)
    if fig.upper_bound:
        labels.append("upper bound")
    text += ", " + ", ".join(labels)
    if crosses_zero(fig):
        text += " (the range crosses zero: the lever may cost more than it saves)"
    if fig.basis is Basis.LIST_EQUIVALENT:
        text += " (list-equivalent allowance headroom, not invoice dollars)"
    return text


def _cohort_ok(cohort: str) -> str:
    if not isinstance(cohort, str) or not _COHORT_RE.match(cohort):
        raise UsageError("cohort ids must be 1-128 printable characters without slashes")
    return cohort


def _dir_name(cohort: str) -> str:
    name = _DIR_RE.sub("_", cohort)
    if name in ("", ".", ".."):
        name = "_" + name
    return name[:100]


def _version(text: str) -> tuple[int, ...]:
    if not isinstance(text, str) or not re.match(r"\d{1,6}(?:\.\d{1,6}){0,3}\Z", text):
        raise UsageError("versions must be dotted integers such as 2.1.242")
    return tuple(int(p) for p in text.split("."))


def snippet(lever_id: str) -> str | None:
    """The SDK / gateway / policy snippet of *lever_id* from ``plan/templates/snippets`` (R-E12),
    or None when the lever has none."""
    if not isinstance(lever_id, str) or not re.match(r"[a-z0-9_.]{1,64}\Z", lever_id):
        return None
    path = _SNIPPETS / f"{lever_id}.md"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _hook_text() -> str:
    return (_TEMPLATES / _HOOK_TEMPLATE).read_text(encoding="utf-8")


def _merge_attrs(existing: object, arm: str) -> str:
    pairs: list[str] = []
    if isinstance(existing, str) and existing:
        for part in existing.split(","):
            name = part.partition("=")[0].strip()
            if name and name not in ("tokenbill.arm", "tokenbill.wave"):
                pairs.append(part.strip())
    pairs += [f"tokenbill.arm={arm}", f"tokenbill.wave={_WAVE}"]
    return ",".join(pairs)


def _final_value(key: str, value: object, current: Mapping[str, object]) -> object:
    """The value to write for *key*, merged with *current* where a merge patch would otherwise
    drop existing members (arrays are replaced whole by RFC 7386)."""
    existing = _get(current, key)
    if key == "hooks.SessionStart" and isinstance(existing, list) and isinstance(value, list):
        merged = list(existing)
        for item in value:
            if item not in merged:
                merged.append(item)
        return merged
    if key == "availableModels" and isinstance(existing, list) and isinstance(value, list):
        merged = list(existing)
        for item in value:
            if item not in merged:
                merged.append(item)
        return merged
    return value


# ---------------------------------------------------------------------------------------------
# pack assembly
# ---------------------------------------------------------------------------------------------


@dataclass
class _PackInput:
    cohort: str
    proposals: list[_Proposal]
    finding_ids: list[str]
    snippets: list[tuple[str, str]]
    litellm: str | None
    conflicts: list[str]


def _dedupe(proposals: Sequence[_Proposal]) -> tuple[list[_Proposal], list[str]]:
    """One proposal per key, the first one winning; a different value later is a conflict (kept
    out, listed in the README)."""
    kept: dict[str, _Proposal] = {}
    conflicts: list[str] = []
    for p in proposals:
        old = kept.get(p.key)
        if old is None:
            kept[p.key] = p
        elif old.value != p.value:
            conflicts.append(f"`{p.key}`: {p.lever_id} proposes {canonical_json(p.value)}; "
                             f"kept {canonical_json(old.value)} from {old.lever_id}")
    return list(kept.values()), conflicts


def _entry_note(p: _Proposal, entry: AllowedKey, cohort: str,
                min_client: tuple[int, ...] | None, min_client_text: str | None) -> str:
    parts = [_fmt_fig(p.projection) if p.lever_id not in (_VERIFICATION, _REPORTING)
             else "no projection (verification / reporting setting)"]
    if p.needs_eval:
        parts.append("needs eval")
    if p.tradeoff:
        parts.append("trade-off (included with --include-tradeoffs)")
    if not entry.verified:
        parts.append(_VERIFY_TEXT)
    if entry.min_version is not None:
        parts.append(f"requires Claude Code >= {entry.min_version}")
        if min_client is not None and _version(entry.min_version) > min_client:
            parts.append(f"clients below {entry.min_version} ignore this key (fleet minimum "
                         f"{min_client_text})")
    if p.note:
        parts.append(sanitize(p.note, 300))
    if cohort == "all":
        parts.append(_ROLLOUT_NOTE)
    else:
        parts.append("rollout: deliver to this cohort only (MDM/endpoint managed-settings file or "
                     "the Claude apps gateway IdP group)")
    return "; ".join(parts)


def _assemble(target: str, inp: _PackInput, current: Mapping[str, object],
              include_tradeoffs: bool, min_client: tuple[int, ...] | None,
              min_client_text: str | None) -> PolicyPack | None:
    usable = [p for p in inp.proposals if include_tradeoffs or not p.tradeoff]
    active, conflicts = _dedupe(usable)
    conflicts = inp.conflicts + conflicts
    active_keys = {p.key for p in active}
    excluded, _ = _dedupe([p for p in inp.proposals if p.tradeoff and not include_tradeoffs
                           and p.primary and p.key not in active_keys])
    arms = sorted({p.lever_id for p in active
                   if _allowed_key(p.key).verified and p.lever_id != _REPORTING})
    if arms:
        otel_value = _merge_attrs(_get(current, _OTEL_KEY), "+".join(arms))
        if not _DOMAIN_CHECKS[_OTEL_KEY](otel_value):
            raise UsageError("current env.OTEL_RESOURCE_ATTRIBUTES is not a key=value list")
        active.append(_Proposal(key=_OTEL_KEY, value=otel_value, lever_id=_VERIFICATION,
                                projection=None, needs_eval=False, tradeoff=False, primary=True,
                                note="verification tags for tokenbill measure"))
    otel = f"tokenbill.arm={'+'.join(arms) if arms else 'none'},tokenbill.wave={_WAVE}"
    entries: list[PolicyEntry] = []
    desired: dict[str, object] = copy.deepcopy(dict(current))
    for p in active:
        entry = _allowed_key(p.key)
        value = _final_value(p.key, p.value, current)
        _check_value(p.key, value)
        entries.append(PolicyEntry(
            key=p.key, value_json=canonical_json(value), projection=p.projection,
            lever_id=p.lever_id, needs_eval=p.needs_eval, verified_key=entry.verified,
            min_version=entry.min_version,
            note=_entry_note(p, entry, inp.cohort, min_client, min_client_text)))
        if entry.verified:
            _set(desired, p.key, value)
    hooks: list[tuple[str, str]] = []
    if any(p.key == "hooks.SessionStart" for p in active):
        hooks.append((HOOK_PATH, _hook_text()))
    if inp.litellm is not None:
        hooks.append((LITELLM_FILE, inp.litellm))
    if target == "sdk":
        hooks += [(f"snippets/{lid}.md", text) for lid, text in inp.snippets]
    if not entries and not hooks and not inp.snippets and not excluded:
        return None
    patch = make_merge_patch(dict(current), desired)
    rollback = make_merge_patch(desired, dict(current))
    readme = _readme(target, inp, entries, excluded, conflicts, current, rollback, otel)
    return PolicyPack(target=target, cohort=inp.cohort, merge_patch_json=canonical_json(patch),
                      rollback_patch_json=canonical_json(rollback), entries=tuple(entries),
                      otel_resource_attributes=otel, readme_md=readme, hooks=tuple(hooks))


def _rollback_line(key: str, current: Mapping[str, object]) -> str:
    old = _get(current, key)
    if old is _MISSING:
        return f"rollback: remove `{key}` (it was not set)"
    return f"rollback: restore `{key}` to `{sanitize(canonical_json(old), 200)}`"


def _readme(target: str, inp: _PackInput, entries: Sequence[PolicyEntry],
            excluded: Sequence[_Proposal], conflicts: Sequence[str],
            current: Mapping[str, object], rollback: object, otel: str) -> str:
    cohort = sanitize(inp.cohort, 128)
    lines = [f"# Token Bill policy pack: {target}, cohort `{cohort}`", "",
             "Nothing in this pack is applied automatically. Review every entry first.", ""]
    if target == "claude-code":
        lines += [f"- `{PATCH_FILE}`: RFC 7386 JSON merge patch against your current managed "
                  "settings (only changed keys).",
                  f"- `{ROLLBACK_FILE}`: the merge patch that restores the current settings "
                  "(`null` removes a key that was not set).", ""]
    lines += ["## Rollout", "",
              "- Server-managed settings apply org-wide. Deliver per group with MDM / endpoint "
              "managed-settings files or the Claude apps gateway per IdP group.",
              "- An org-wide change cannot be randomized: measure it with `tokenbill measure plan "
              "--design its` (MEASURED at best). Per-group waves can use `--design stepped_wedge` "
              "or `cluster_rct`.",
              f"- Verification tags (`env.OTEL_RESOURCE_ATTRIBUTES`): `{sanitize(otel, 300)}`.",
              ""]
    in_json = [e for e in entries if e.verified_key]
    comments = [e for e in entries if not e.verified_key]
    if in_json:
        lines += ["## Settings in the patch", ""]
        for e in in_json:
            lines += _entry_lines(e, inp.cohort, current)
    if comments:
        lines += ["## Commented guidance (not in the patch)", "",
                  f"These keys are not verified: {_VERIFY_TEXT}.", "", "```"]
        for e in comments:
            lines.append(f"// {_VERIFY_TEXT}")
            lines.append(f"// \"{e.key}\": {sanitize(e.value_json, 400)}")
        lines += ["```", ""]
        for e in comments:
            lines += _entry_lines(e, inp.cohort, current)
    if excluded:
        lines += ["## Trade-off levers (not in this pack)", "",
                  f"{_TRADEOFF_TEXT[0].upper()}{_TRADEOFF_TEXT[1:]}.", "", "```"]
        by_lever: dict[str, list[_Proposal]] = {}
        for p in excluded:
            by_lever.setdefault(p.lever_id, []).append(p)
        for lever_id, props in by_lever.items():
            lines.append(f"// trade-off lever {lever_id}: run `tokenbill ab` or `tokenbill "
                         "measure plan` first, then re-run with --include-tradeoffs")
            for p in props:
                lines.append(f"// \"{p.key}\": {sanitize(canonical_json(p.value), 400)}")
        lines += ["```", ""]
        for lever_id, props in by_lever.items():
            lines.append(f"- `{lever_id}` ({', '.join(f'`{p.key}`' for p in props)}): projected "
                         f"{_fmt_fig(props[0].projection)}")
        lines.append("")
    if any(e.key == "hooks.SessionStart" for e in entries):
        lines += ["## SessionStart hook", "",
                  f"`{HOOK_PATH}` warns before an expensive cold resume (threshold $1.00, env "
                  "`TOKENBILL_HOOK_THRESHOLD_USD`; at most once per session and three times per "
                  "person per week; never blocks, always exits 0, stores no content). Copy it to "
                  f"`~/.claude/hooks/` on every machine; managed settings run `{HOOK_COMMAND}`.",
                  ""]
    if inp.litellm is not None:
        lines += ["## Gateway (LiteLLM)", "",
                  f"`{LITELLM_FILE}` adds `cache_control_injection_points` to the affected "
                  "models; merge it into the proxy's config.yaml. The TTL key is VERIFY.", ""]
    if inp.snippets:
        lines += ["## Other levers (SDK, gateway and policy decisions)", ""]
        for _lid, text in inp.snippets:
            lines += [text.rstrip("\n"), ""]
    if conflicts:
        lines += ["## Conflicts", ""] + [f"- {sanitize(c, 400)}" for c in conflicts] + [""]
    if inp.finding_ids:
        lines += ["## Findings behind this pack", ""]
        lines += [f"- `{sanitize(fid, 80)}`" for fid in sorted(set(inp.finding_ids))]
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _entry_lines(e: PolicyEntry, cohort: str, current: Mapping[str, object]) -> list[str]:
    setting = e.lever_id in (_VERIFICATION, _REPORTING)
    projected = "n/a (verification / reporting setting)" if setting else _fmt_fig(e.projection)
    lines = [f"### `{e.key}` = `{sanitize(e.value_json, 400)}`", "",
             f"- lever: `{e.lever_id}`; needs eval: {'yes' if e.needs_eval else 'no'}; minimum "
             f"Claude Code version: {e.min_version or 'none stated'}",
             f"- projected: {projected}",
             f"- {_rollback_line(e.key, current)}"]
    if e.lever_id not in (_VERIFICATION, _REPORTING) and not e.lever_id.startswith("fix:"):
        lines.append(f"- verify: `tokenbill measure plan --lever {e.lever_id}` (org-wide: add "
                     "`--design its`)")
        if e.lever_id in _CHECKED_LEVERS:
            lines.append(f"- post-rollout check: `tokenbill policy check-effect --lever "
                         f"{e.lever_id} --cohort {sanitize(cohort, 128)} --since <rollout date>`")
    rest = e.note.split("; ", 1)[1] if "; " in e.note else ""
    if rest:
        lines.append(f"- note: {sanitize(rest, 800)}")
    lines.append("")
    return lines


# ---------------------------------------------------------------------------------------------
# the entry point
# ---------------------------------------------------------------------------------------------


def _normalize_current(current: Mapping[str, object] | None) -> dict[str, object]:
    if current is None:
        return {}
    if not isinstance(current, Mapping):
        raise UsageError("current settings must be a JSON object")
    value = _json_value(current)
    assert isinstance(value, dict)
    for section in ("env", "hooks"):
        if section in value and not isinstance(value[section], dict):
            raise UsageError(f"current settings: {section!r} must be an object")
    return value


def _plan_levers(plan: ActionPlan) -> list[tuple[LeverDef, str, Figure | None]]:
    """``(definition, params, projection)`` per lever of the plan, first class wins (billed, then
    allowance, then pool: the plan's order)."""
    out: list[tuple[LeverDef, str, Figure | None]] = []
    seen: set[str] = set()
    for r in plan.levers:
        if r.lever_id in seen:
            continue
        try:
            ldef = lever(r.lever_id)
        except UsageError:
            continue
        if ldef.replay == "aggregate":
            continue
        seen.add(r.lever_id)
        out.append((ldef, r.params, r.projected_monthly))
    return out


def _gateway_models(findings: Sequence[Finding]) -> list[str]:
    models = set()
    for f in findings:
        if f.kind in _GATEWAY_KINDS or _GATEWAY_LEVER in f.lever_ids:
            model = dict(f.scope.dims).get("model")
            if isinstance(model, str) and _MODEL_ID_RE.match(model):
                models.add(model)
    return sorted(models)


def _litellm_text(plan: ActionPlan, findings: Sequence[Finding]) -> str | None:
    relevant = any(r.lever_id == _GATEWAY_LEVER for r in plan.levers) or any(
        f.kind in _GATEWAY_KINDS or _GATEWAY_LEVER in f.lever_ids for f in findings)
    if not relevant:
        return None
    models = _gateway_models(findings) or [_MODEL_PLACEHOLDER]
    ttl = None
    for r in plan.levers:
        if r.lever_id in _TTL_LEVERS and r.params:
            policy = parse_policy(r.params)
            if policy.ttl:
                ttl = policy.ttl[0][1]
                break
    text = render_injection_points(models, ttl=ttl)
    validate_litellm_fragment(text)
    return text


def _snippets(plan: ActionPlan, findings: Sequence[Finding]) -> list[tuple[str, str]]:
    ids = [r.lever_id for r in plan.levers]
    ids += [lid for f in findings for lid in f.lever_ids]
    out = []
    for lid in dict.fromkeys(ids):
        text = snippet(lid)
        if text is None:
            continue
        if "{ttl}" in text:
            ttl = next((parse_policy(r.params).ttl[0][1] for r in plan.levers
                        if r.lever_id == lid and r.params and parse_policy(r.params).ttl),
                       "1h")
            text = text.replace("{ttl}", ttl)
        out.append((lid, text))
    return out


def build_policy_packs(plan: ActionPlan, findings: Sequence[Finding], *, target: str,
                       current: Mapping[str, object] | None, cohort_by: str | None,
                       include_tradeoffs: bool, contract: ContractOverlay | None,
                       model_pricing_emitter: Callable[[ContractOverlay], dict] | None = None,
                       min_client_version: str | None = None
                       ) -> list[PolicyPack]:
    """Per-cohort policy packs for *target* (see the module docstring); ``[]`` when there is
    nothing to deliver. The CLI passes ``rates.contract.to_model_pricing`` as
    *model_pricing_emitter*. Nothing is ever applied. Invalid arguments, keys outside the
    allowlist and values outside a key's domain raise :class:`UsageError`."""
    if not isinstance(plan, ActionPlan):
        raise UsageError("build_policy_packs: plan must be an ActionPlan")
    if target == "github-copilot":
        raise UsageError("target github-copilot is built by the Copilot extension "
                         "(core.extensions.policy_targets)")
    if target not in TARGETS:
        raise UsageError(f"unknown policy target {target!r} (one of {', '.join(TARGETS)})")
    if cohort_by is not None and cohort_by not in COHORT_BY:
        raise UsageError(f"unknown --cohort-by {cohort_by!r} (one of {', '.join(COHORT_BY)})")
    if contract is not None and not isinstance(contract, ContractOverlay):
        raise UsageError("contract must be a ContractOverlay")
    findings = list(findings)
    if any(not isinstance(f, Finding) for f in findings):
        raise UsageError("findings must be Finding values")
    cur = _normalize_current(current)
    min_client = _version(min_client_version) if min_client_version is not None else None
    include = bool(include_tradeoffs)
    litellm = _litellm_text(plan, findings)
    snippets = _snippets(plan, findings)
    fids = [f.finding_id for f in findings if f.lever_ids]
    if target in ("litellm", "sdk"):
        inp = _PackInput(cohort="all", proposals=[], finding_ids=fids,
                         snippets=snippets if target == "sdk" else [],
                         litellm=litellm if target == "litellm" else None, conflicts=[])
        if target == "litellm" and litellm is None:
            return []
        if target == "sdk" and not snippets:
            return []
        pack = _assemble(target, inp, {}, include, min_client, min_client_version)
        return [pack] if pack is not None else []
    return _claude_code_packs(plan, findings, cur, cohort_by, include, contract,
                              model_pricing_emitter, min_client, min_client_version, litellm,
                              snippets)


def _claude_code_packs(plan: ActionPlan, findings: Sequence[Finding], cur: dict[str, object],
                       cohort_by: str | None, include: bool, contract: ContractOverlay | None,
                       emitter: Callable[[ContractOverlay], dict] | None,
                       min_client: tuple[int, ...] | None, min_client_text: str | None,
                       litellm: str | None, snippets: list[tuple[str, str]]) -> list[PolicyPack]:
    hetero = any(f.kind == "ttl-heterogeneous" for f in findings)
    mode = cohort_by or ("team" if hetero else None)
    dim = _COHORT_DIMS[mode] if mode is not None else None
    levers = _plan_levers(plan)
    lever_props: dict[str, list[_Proposal]] = {}
    for ldef, params, proj in levers:
        lever_props[ldef.lever_id] = _lever_proposals(ldef, params, proj, "")
    cohorts: dict[str, _PackInput] = {}
    all_inp = _PackInput(cohort="all", proposals=[], finding_ids=[], snippets=snippets,
                         litellm=litellm, conflicts=[])
    all_finding_props: list[_Proposal] = []
    for f in findings:
        props = _finding_proposals(f)
        cohort = dict(f.scope.dims).get(dim) if dim is not None else None
        if cohort is None:
            all_finding_props += props
            if f.lever_ids:
                all_inp.finding_ids.append(f.finding_id)
            continue
        inp = cohorts.setdefault(_cohort_ok(cohort), _PackInput(
            cohort=cohort, proposals=[], finding_ids=[], snippets=[], litellm=None,
            conflicts=[]))
        inp.finding_ids.append(f.finding_id)
        inp.proposals += props
        for ldef in _finding_levers(f):
            inp.proposals += [p for p in lever_props.get(ldef.lever_id, ())
                              if all(q.key != p.key for q in props)]
    claimed = {p.key for inp in cohorts.values() for p in inp.proposals}
    for _ldef, _params, _proj in levers:
        all_inp.proposals += [p for p in lever_props[_ldef.lever_id] if p.key not in claimed]
    all_inp.proposals += [p for p in all_finding_props if p.key not in claimed]
    if contract is not None:
        _model_pricing(all_inp, contract, emitter)
    packs = []
    pack = _assemble("claude-code", all_inp, cur, include, min_client, min_client_text)
    if pack is not None:
        packs.append(pack)
    for cohort in sorted(cohorts):
        pack = _assemble("claude-code", cohorts[cohort], cur, include, min_client,
                         min_client_text)
        if pack is not None:
            packs.append(pack)
    return packs


def _numbers(value: object) -> object:
    """Decimal strings in a ``modelPricing`` object become JSON numbers (the key's domain)."""
    if isinstance(value, dict):
        return {k: _numbers(v) for k, v in value.items()}
    if isinstance(value, str) and re.match(r"\d{1,12}(?:\.\d{1,12})?\Z", value):
        return Decimal(value)
    return value


def _model_pricing(inp: _PackInput, contract: ContractOverlay,
                   emitter: Callable[[ContractOverlay], dict] | None) -> None:
    entry = _allowed_key("modelPricing")
    if emitter is None:
        inp.conflicts.append("`modelPricing` not emitted: no model-pricing emitter was passed "
                             "(the CLI passes rates.contract.to_model_pricing)")
        return
    value = _numbers(_json_value(emitter(contract)))
    if not isinstance(value, dict):
        raise UsageError("the model-pricing emitter must return a JSON object")
    _check_value("modelPricing", value)
    inp.proposals.append(_Proposal(
        key="modelPricing", value=value, lever_id=_REPORTING, projection=None, needs_eval=False,
        tradeoff=False, primary=True,
        note=("reporting accuracy: /usage, the status line and OTel cost use the contract rates; "
              "managed settings only" + ("" if entry.verified else f"; {_VERIFY_TEXT}"))))


def render_pack(pack: PolicyPack, out_dir: Path) -> list[Path]:
    """Write *pack* under ``out_dir/<cohort>/`` (the cohort id with unsafe characters replaced):
    ``managed-settings.patch.json`` and ``rollback.patch.json`` (claude-code target, indented
    canonical JSON), ``README.md`` and every ``hooks`` file (the hook is made executable).
    Returns the written paths, sorted. Unsafe relative paths raise :class:`UsageError`."""
    if not isinstance(pack, PolicyPack):
        raise UsageError("render_pack: pack must be a PolicyPack")
    base = Path(out_dir) / _dir_name(_cohort_ok(pack.cohort))
    files: list[tuple[str, str]] = []
    if pack.target == "claude-code":
        for name, text in ((PATCH_FILE, pack.merge_patch_json),
                           (ROLLBACK_FILE, pack.rollback_patch_json)):
            try:
                doc = json.loads(text, parse_float=Decimal)
            except ValueError:
                raise UsageError(f"render_pack: {name} is not JSON") from None
            files.append((name, canonical_json(doc, indent=2) + "\n"))
    files.append((README_FILE, pack.readme_md))
    for rel, text in pack.hooks:
        parts = Path(rel).parts
        if (not rel or rel.startswith(("/", "\\")) or "\\" in rel or ".." in parts
                or Path(rel).is_absolute() or ":" in rel):
            raise UsageError("render_pack: unsafe file path in pack")
        files.append((rel, text))
    written = []
    for rel, text in files:
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if rel == HOOK_PATH:
            path.chmod(0o755)
        written.append(path)
    return sorted(written)

