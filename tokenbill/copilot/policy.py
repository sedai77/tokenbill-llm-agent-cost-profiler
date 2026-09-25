"""GitHub Copilot policy pack: managed-settings patch, admin checklist, REST requests (CP-POLICY).

:func:`build_copilot_packs` turns the Copilot aggregate plan(s), the Copilot findings, the admin
checklist items (:func:`tokenbill.copilot.admin_actions.admin_actions`) and the budget design
(:func:`tokenbill.copilot.budgets.budget_design`) into :class:`~tokenbill.core.types.PolicyPack`
objects for target ``github-copilot`` (addendum §11.3, §11.4, §9.3, §19.4; DC13, DC19–DC21, DC29,
DC30; R17, ruling R-E22). CP-WIRE's extension hook ``tokenbill.pipeline.copilot:policy_packs``
calls it. **Nothing is ever applied or executed** (DC13): the pack is files an admin reviews.

**Files** (``PolicyPack.hooks`` as ``(relative path, text)``, sorted by path):

* ``copilot/managed-settings.patch.json`` / ``copilot/rollback.patch.json`` — an RFC 7386 merge
  patch against ``--current`` with only ``copilot.managed.*`` keys (``COPILOT_ALLOWLIST``; any
  other key → ``UsageError``): ``model`` (``"auto"``; ``{"overridable": "auto"}`` at enterprise
  level when team files carry the waves), ``telemetry`` (``enabled``, ``captureContent: false``,
  ``lockCaptureContent: true``, ``serviceName`` kept at ``github-copilot`` unless a name is given;
  the endpoint and protocol are the admin's collector) and the rollback restoring every previous
  value (``null`` for absent keys). Keys whose ``verified`` flag is False are never written into
  JSON; they appear only as commented README lines. Trade-off keys need ``include_tradeoffs``.
* ``copilot/team-mappings.patch.json`` and ``copilot/teams/<team>.json`` — per-team ``model`` for
  stepped-wedge waves (``cohort_by="team"``; teams of at least *k* people only; JetBrains-heavy
  teams are spread over the waves). The team-file mechanism is VERIFY (README).
* ``github/admin-checklist.md`` — every :class:`~tokenbill.core.types.AdminAction`: what, where,
  docs URL, projection with label (both scenario values while the plan is unknown) and reach,
  needs-eval / trade-off, deadline, rollback, auth note and verification pointer; then the budget
  design and "not assessed: plan unknown".
* ``github/requests.jsonl`` — REST request specs ``{"method", "path", "api_version":
  "2026-03-10", "body", "note", "lever_id", "auth"}``: seat removal with a placeholder list and the
  seats query (``last_activity_at`` before the cut-off and ``assigning_team`` null — never a
  username), team removal only for teams of at least *k* seats whose every seat is idle, budgets
  and cost-center pool changes from the budget design, cost-center creation and the coding-agent
  policy (body VERIFY). The README shows the equivalent ``gh api`` invocations as text.
* ``repo/.github/copilot/settings.patch.json`` (``copilot.repo.*`` keys from Copilot fixes: CLI
  only, trusted directories only) and ``repo/.github/allowed_models.txt`` (model globs and exactly
  one ``fallback:`` line; trade-off, so only with ``include_tradeoffs``).
* ``ci/copilot-limits.md`` (``--max-ai-credits N`` and the collector post-step) and
  ``ci/agentic-workflows.md`` (frontmatter ``max-ai-credits: N`` from the finding, trigger review,
  the ``token-usage.jsonl`` download recipe).
* ``vscode/settings.snippet.json`` and ``vscode/agent-traces-optin.md`` — the developer opt-in of
  the VS Code usage database (a user setting; no managed key can enforce it), the daily
  ``tokenbill copilot collect --source vscode`` schedule and the privacy statement.

**Reach (§9.3)** is stated per key in the README: managed ``model`` reaches the CLI, VS Code ≥
1.126, the Copilot app and the cloud agent but **not JetBrains** (Visual Studio, Xcode and Eclipse
unverified, so excluded); the estimated reach is ``1 −`` the share of interactions on those
editors (team counts, :func:`team_counts`). MCP server lists do not reach the cloud agent; managed
telemetry for JetBrains is documented inconsistently.

**Plan unknown (R17).** No plan-dependent figure is given as if the plan were known: projections
show both scenario values, budget specs come per scenario, the seat-plan change is "not assessed".

Content-free and deterministic: only catalog text, counts, dates, dollar figures and
organizational names (teams, cost centers) are written; finding text, logins, pseudonyms and the
unrelated parts of ``--current`` never are; the output is independent of input order.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from tokenbill.core import catalog
from tokenbill.core import facts as _facts
from tokenbill.core.errors import PrivacyError, UsageError
from tokenbill.core.labels import Figure
from tokenbill.core.records import ActivityDay, ConfigSnapshot, LicenseSnapshot
from tokenbill.core.textsafe import sanitize
from tokenbill.core.types import AdminAction, Finding, PolicyEntry, PolicyPack

from .admin_actions import (
    API_VERSION,
    EXCLUDED_EDITOR_FAMILIES,
    JETBRAINS_ACTION_ID,
    JETBRAINS_HEAVY_SHARE,
    NOT_ASSESSED_PLAN_UNKNOWN,
    figure_text,
    is_copilot_finding,
    lever_projection,
    scenario_projection_text,
    split_plans,
    team_editor_shares,
)

__all__ = [
    "DEFAULT_SERVICE_NAME",
    "MANAGED_PREFIX",
    "REPO_PREFIX",
    "TARGET",
    "apply_merge_patch",
    "build_copilot_packs",
    "load_current",
    "managed_settings_patch",
    "team_counts",
    "write_pack",
]

#: ``PolicyPack.target`` of every Copilot pack.
TARGET = "github-copilot"
#: Managed ``telemetry.serviceName`` unless ``--otel-service-name`` names another (addendum §11.3).
DEFAULT_SERVICE_NAME = "github-copilot"
#: Allowlist prefix of the managed-settings keys (JSON path after the prefix).
MANAGED_PREFIX = "copilot.managed."
#: Allowlist prefix of the repository ``.github/copilot/settings.json`` keys.
REPO_PREFIX = "copilot.repo."

_AUTO_LEVER = "copilot.default_model_auto"
_TELEMETRY_LEVER = "copilot.telemetry_on"
_MCP_LEVER = "copilot.mcp_trim"
_POLICY_LEVER = "copilot.model_policy"
_MODEL_KEY = "copilot.managed.model"
_ALLOWED_MODELS_KEY = "copilot.repo.allowed_models"
_MAX_CURRENT_BYTES = 1 << 20
_PSEUDONYM_RE = re.compile(r"(?<![0-9A-Za-z_])[prc]_[0-9a-f]{20}(?![0-9a-f])")
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_IDLE_BUCKETS = frozenset({"31-90", "none_90d"})
_WAVES = 3
_MIN_CLI_CAP = 30
_ABSENT = object()
_SCENARIO_LABEL = {None: "all plans known", "business": "if Business", "enterprise":
                   "if Enterprise"}


# ---------------------------------------------------------------------------------------------
# JSON: --current, RFC 7386 merge patches
# ---------------------------------------------------------------------------------------------


def _canon(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False)


def _pretty(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def _reject_constant(name: str) -> object:
    raise ValueError(f"non-standard JSON constant {name}")


def load_current(text: str | bytes | None) -> dict[str, object]:
    """The admin's current managed-settings JSON (``--current``) as a dict: at most 1 MiB,
    UTF-8 (a BOM is accepted), a JSON object without NaN / Infinity; ``None`` or blank → ``{}``.
    Anything else → ``UsageError`` (content-free message; the text is never echoed)."""
    if text is None:
        return {}
    if isinstance(text, bytes):
        if len(text) > _MAX_CURRENT_BYTES:
            raise UsageError("--current is larger than 1 MiB")
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            raise UsageError("--current is not UTF-8") from None
    if not isinstance(text, str):
        raise UsageError("--current must be JSON text")
    if len(text) > _MAX_CURRENT_BYTES:
        raise UsageError("--current is larger than 1 MiB")
    text = text.lstrip("﻿")
    if not text.strip():
        return {}
    try:
        obj = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        raise UsageError("--current is not valid JSON") from None
    if not isinstance(obj, dict):
        raise UsageError("--current must be a JSON object")
    return obj


def _json_copy(obj: object, what: str) -> object:
    try:
        return json.loads(_canon(obj))
    except (TypeError, ValueError, RecursionError):
        raise UsageError(f"{what} must be JSON data") from None


def _same(a: object, b: object) -> bool:
    return _canon(a) == _canon(b)


def _diff(a: Mapping[str, object], b: Mapping[str, object]) -> dict[str, object]:
    """The RFC 7386 merge patch turning document *a* into *b* (``null`` removes a key; explicit
    ``null`` values inside *b* cannot be expressed by a merge patch and are dropped)."""
    patch: dict[str, object] = {}
    for key in sorted(set(a) | set(b)):
        if key not in b:
            patch[key] = None
        elif key not in a:
            patch[key] = b[key]
        elif _same(a[key], b[key]):
            continue
        elif isinstance(a[key], dict) and isinstance(b[key], dict):
            patch[key] = _diff(a[key], b[key])  # type: ignore[arg-type]
        else:
            patch[key] = b[key]
    return patch


def apply_merge_patch(target: object, patch: object) -> object:
    """RFC 7386 JSON Merge Patch: *patch* applied to a deep copy of *target*."""
    if not isinstance(patch, dict):
        return _json_copy(patch, "a merge patch")
    result = _json_copy(target, "a merge patch target") if isinstance(target, dict) else {}
    assert isinstance(result, dict)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = apply_merge_patch(result.get(key), value)
    return result


def _managed_path(key: str) -> tuple[str, ...]:
    if not isinstance(key, str) or not key.startswith(MANAGED_PREFIX):
        raise UsageError(f"settings key {key!r} is not a managed-settings key")
    path = tuple(key[len(MANAGED_PREFIX):].split("."))
    if any(not part for part in path):
        raise UsageError(f"settings key {key!r} is malformed")
    return path


def _set_path(doc: dict[str, object], path: tuple[str, ...], value: object) -> None:
    node = doc
    for part in path[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[path[-1]] = _json_copy(value, "a settings value")


def managed_settings_patch(changes: Mapping[str, object] | Sequence[tuple[str, object]],
                           current: Mapping[str, object] | None) -> tuple[dict[str, object],
                                                                          dict[str, object]]:
    """``(merge patch, rollback patch)`` setting *changes* (allowlist key → JSON value) in the
    managed-settings document *current* (RFC 7386; only changed keys; the rollback restores each
    previous value, ``null`` for keys that were absent). Every key must be a ``copilot.managed.*``
    key of ``COPILOT_ALLOWLIST`` → else ``UsageError``."""
    items = list(changes.items()) if isinstance(changes, Mapping) else list(changes)
    base = _json_copy(dict(current) if current is not None else {}, "--current")
    if not isinstance(base, dict):  # pragma: no cover - dict in, dict out
        raise UsageError("--current must be a JSON object")
    new = _json_copy(base, "--current")
    assert isinstance(new, dict)
    for key, value in sorted(items, key=lambda kv: str(kv[0])):
        catalog.copilot_allowed(key)
        _set_path(new, _managed_path(key), value)
    return _diff(base, new), _diff(new, base)


# ---------------------------------------------------------------------------------------------
# teams
# ---------------------------------------------------------------------------------------------


def team_counts(activity: Iterable[ActivityDay] = (), config: Iterable[ConfigSnapshot] = (),
                licenses: Iterable[LicenseSnapshot] = ()) -> dict[str, dict[str, int]]:
    """Team → counts for :func:`build_copilot_packs` / ``admin_actions(teams=…)``: interactions by
    editor (``ide:<name>`` from metrics, ``cli`` = CLI requests, ``app`` = Copilot app requests),
    ``n_people`` (distinct people with activity or a seat), ``seats`` and ``idle_seats`` (latest
    seat snapshot per person; idle = last activity 31+ days ago). Aggregate-only bundles'
    ``activity_counts`` rows are used for teams without per-person activity. Unattributed rows are
    left out; no pseudonym leaves this function."""
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    people: dict[str, set[str]] = defaultdict(set)
    person_teams: set[str] = set()
    for day in activity:
        if not isinstance(day, ActivityDay):
            raise UsageError("activity must hold ActivityDay records")
        if not day.team:
            continue
        person_teams.add(day.team)
        people[day.team].add(day.principal)
        for key, value in day.counts:
            if key.startswith("ide:"):
                counts[day.team][key] += value
            elif key == "cli_requests":
                counts[day.team]["cli"] += value
            elif key == "app_requests":
                counts[day.team]["app"] += value
    agg_people: dict[str, int] = {}
    for snap in config:
        if not isinstance(snap, ConfigSnapshot):
            raise UsageError("config must hold ConfigSnapshot records")
        if snap.kind != "activity_counts":
            continue
        attrs = dict(snap.attrs)
        team = attrs.get("team")
        if not isinstance(team, str) or not team or team in person_teams:
            continue
        for key, value in attrs.items():
            if type(value) is not int or value < 0:
                continue
            if key.startswith("ide:"):
                counts[team][key] += value
            elif key == "cli_requests":
                counts[team]["cli"] += value
            elif key == "app_interactions":
                counts[team]["app"] += value
            elif key == "n_people":
                agg_people[team] = max(agg_people.get(team, 0), value)
    latest: dict[str, LicenseSnapshot] = {}
    for lic in licenses:
        if not isinstance(lic, LicenseSnapshot):
            raise UsageError("licenses must hold LicenseSnapshot records")
        cur = latest.get(lic.principal)
        if cur is None or (lic.snapshot_date, lic.fetched_ms) > (cur.snapshot_date,
                                                                 cur.fetched_ms):
            latest[lic.principal] = lic
    for principal, lic in latest.items():
        if not lic.team:
            continue
        people[lic.team].add(principal)
        counts[lic.team]["seats"] += 1
        counts[lic.team]["idle_seats"] += int(lic.last_activity_bucket in _IDLE_BUCKETS)
    for team in set(people) | set(agg_people):
        counts[team]["n_people"] = max(len(people.get(team, ())), agg_people.get(team, 0))
    return {team: dict(sorted(c.items())) for team, c in sorted(counts.items())}


def _team_table(teams: object) -> dict[str, dict[str, int]]:
    if teams is None:
        return {}
    if not isinstance(teams, Mapping):
        raise UsageError("teams must map a team to its counts")
    team_editor_shares(teams)  # validates names and counts
    return {t: {k: int(v) for k, v in c.items()} for t, c in sorted(teams.items())}  # type: ignore[union-attr]


def _excluded_share(counts: Mapping[str, int]) -> tuple[int, int]:
    """(interactions on editors managed ``model`` does not reach, all interactions)."""
    total = excluded = 0
    for key, value in counts.items():
        if key == "cli" or key == "app":
            total += value
        elif key.startswith("ide:") or key in catalog.EDITOR_FAMILIES:
            total += value
            if catalog.editor_family(key) in EXCLUDED_EDITOR_FAMILIES:
                excluded += value
    return excluded, total


def _pct(num: int, den: int) -> str:
    return f"{(num * 100 + den // 2) // den}%" if den else "n/a"


def _reach_text(num: int, den: int) -> str:
    """``1 − num/den`` as a two-place decimal, rounded down (a reach is never overstated)."""
    if den <= 0:
        return "unknown"
    hundredths = ((den - num) * 100) // den
    return f"{hundredths // 100}.{hundredths % 100:02d}"


def _slug(team: str, taken: set[str]) -> str:
    base = _SLUG_RE.sub("-", team).strip("-.").lower()[:48] or "team"
    slug = base
    if slug in taken or slug != team.lower():
        slug = f"{base}-{hashlib.sha256(team.encode('utf-8')).hexdigest()[:8]}"
    taken.add(slug)
    return slug


def _name(text: str) -> str:
    """An organizational name for output: control characters and markup-breaking characters
    removed, at most 80 characters."""
    return sanitize(text, 80).replace("`", "'").replace("|", "/")


# ---------------------------------------------------------------------------------------------
# the pack
# ---------------------------------------------------------------------------------------------


class _Inputs:
    def __init__(self, plans_by_scenario: object, findings: Sequence[Finding],
                 actions: Sequence[AdminAction], teams: object, budgets: object, k: int) -> None:
        self.known, self.scenarios = split_plans(plans_by_scenario)
        if isinstance(findings, (str, bytes)) or not isinstance(findings, Iterable):
            raise UsageError("findings must be a sequence")
        fs = list(findings)
        if any(not isinstance(f, Finding) for f in fs):
            raise UsageError("findings must hold Finding records")
        self.findings = sorted((f for f in fs if is_copilot_finding(f)),
                               key=lambda f: f.finding_id)
        if isinstance(actions, (str, bytes)) or not isinstance(actions, Iterable):
            raise UsageError("actions must be a sequence")
        self.actions = list(actions)
        if any(not isinstance(a, AdminAction) for a in self.actions):
            raise UsageError("actions must hold AdminAction records")
        self.action_ids = {a.admin_action for a in self.actions}
        self.teams = _team_table(teams)
        self.budgets = _budget_list(budgets)
        self.k = k
        self.levers = self._levers()
        self.unknown = bool(self.scenarios) or any(
            dict(f.scope.dims).get("plan_scenario") for f in self.findings) or any(
            s["scenario"] is not None for s in self.budgets)

    def _levers(self) -> set[str]:
        out: set[str] = set()
        for plan in ([self.known] if self.known else [p for _, p in self.scenarios]):
            out |= {lv.lever_id for lv in plan.levers}
        for f in self.findings:
            out |= set(f.lever_ids)
            out |= {lv.lever_id for lv in catalog.levers_for_kind(f.kind, family="copilot")}
        return out

    def projection(self, lever_id: str) -> tuple[Figure | None, str]:
        """The lever's projection (known plan) or the scenario text (plan unknown)."""
        if self.known is not None:
            fig = lever_projection(self.known, lever_id)
            return fig, figure_text(fig) if fig is not None else "not projected"
        if self.scenarios:
            text = scenario_projection_text(self.scenarios, lever_id)
            return None, text if text is not None else "not projected"
        return None, "not projected (no plan given)"

    def evidence_int(self, kinds: Iterable[str], name: str) -> int | None:
        best: int | None = None
        for f in self.findings:
            if f.kind not in kinds:
                continue
            for item in f.evidence:
                for key, value in item.attrs:
                    if key == name and type(value) is int and 0 < value <= 10**9:
                        best = value if best is None else max(best, value)
        return best


def _budget_list(budgets: object) -> list[dict[str, object]]:
    if budgets is None:
        return []
    if isinstance(budgets, (str, bytes, Mapping)) or not isinstance(budgets, Iterable):
        raise UsageError("budgets must be the list budget_design returns")
    out = []
    for spec in budgets:
        if (not isinstance(spec, Mapping) or spec.get("scenario") not in (None, "business",
                                                                          "enterprise")
                or not isinstance(spec.get("text"), str)):
            raise UsageError("budgets must be the list budget_design returns")
        req = spec.get("request")
        if req is not None and (not isinstance(req, Mapping) or not {
                "method", "path", "api_version", "body", "note", "lever_id", "auth"} <= set(req)):
            raise UsageError("a budget request spec lacks request fields")
        out.append(dict(spec))
    return out


class _Want:
    """One setting the pack proposes."""

    def __init__(self, key: str, value: object, lever_id: str) -> None:
        self.key = key
        self.value = _json_copy(value, "a settings value")
        self.lever_id = lever_id


def _fix_wants(findings: Sequence[Finding]) -> list[_Want]:
    """Settings the Copilot fixes propose: every ``config_patch`` key of a ``github-copilot`` fix
    must be in ``COPILOT_ALLOWLIST`` (``UsageError`` otherwise)."""
    wants: dict[str, tuple[str, object, str]] = {}
    for f in findings:
        fix = f.fix
        if fix is None or fix.target != TARGET or not fix.config_patch:
            continue
        for key, value_json in fix.config_patch:
            allowed = catalog.copilot_allowed(key)
            try:
                value = json.loads(value_json)
            except (TypeError, ValueError, RecursionError):
                raise UsageError(f"settings key {key!r}: value is not JSON") from None
            lever_id = allowed.lever_id or (f.lever_ids[0] if f.lever_ids else "")
            cand = (_canon(value), value, lever_id)
            if key not in wants or cand[0] < wants[key][0]:
                wants[key] = cand
    return [_Want(key, value, lever) for key, (_, value, lever) in sorted(wants.items())]


def _reach_note(key: str, teams: Mapping[str, Mapping[str, int]]) -> str:
    if key == _MODEL_KEY:
        excluded = total = 0
        for counts in teams.values():
            e, t = _excluded_share(counts)
            excluded += e
            total += t
        est = (f"estimated reach {_reach_text(excluded, total)} = 1 - the share of interactions "
               f"on JetBrains and unverified editors ({_pct(excluded, total)})" if total else
               "reach unknown without usage metrics (low 0, high 1)")
        return ("Reach: Copilot CLI, VS Code 1.126+, the Copilot app and the cloud agent; NOT "
                "JetBrains (managed model is not applied in JetBrains); Visual Studio, Xcode and "
                f"Eclipse unverified, so excluded; {est}.")
    if key.startswith("copilot.managed.telemetry."):
        return ("Reach: Copilot CLI and VS Code; JetBrains support is documented inconsistently "
                "(not counted). An enabler, not projected.")
    if key.startswith("copilot.managed.") and key.endswith("McpServers"):
        return "Reach: CLI, VS Code, the Copilot app and JetBrains; not the cloud agent."
    if key.startswith(REPO_PREFIX):
        return ("Reach: Copilot CLI only, and only in trusted directories (repository "
                ".github/copilot/settings.json).")
    return "Reach: see the key's documentation."


def _entry(want: _Want, inp: _Inputs, reach: str) -> PolicyEntry:
    allowed = catalog.copilot_allowed(want.key)
    lever_id = want.lever_id or allowed.lever_id or ""
    try:
        lv = catalog.lever(lever_id) if lever_id else None
    except UsageError:
        lv = None
    fig, text = inp.projection(lever_id) if lever_id else (None, "not projected")
    note = f"{reach} Projection: {text}."
    return PolicyEntry(key=want.key, value_json=_canon(want.value), projection=fig,
                       lever_id=lever_id, needs_eval=bool(lv.needs_eval) if lv else False,
                       verified_key=allowed.verified, min_version=allowed.min_version, note=note)


def _team_waves(inp: _Inputs) -> list[tuple[str, str, int, bool]]:
    """(team, file slug, wave, JetBrains-heavy) for teams of at least k people: sorted by a hash
    of the name within the JetBrains-heavy and other strata, dealt round-robin over the waves."""
    shares = team_editor_shares(inp.teams)
    eligible = sorted(t for t, c in inp.teams.items() if c.get("n_people", 0) >= inp.k)
    heavy = [t for t in eligible if t in shares and shares[t][0] >= JETBRAINS_HEAVY_SHARE]
    light = [t for t in eligible if t not in heavy]
    waves = min(_WAVES, len(eligible)) or 1
    out: list[tuple[str, str, int, bool]] = []
    taken: set[str] = set()
    i = 0
    for stratum in (heavy, light):
        for team in sorted(stratum, key=lambda t: (hashlib.sha256(t.encode("utf-8")).hexdigest(),
                                                   t)):
            out.append((team, _slug(team, taken), i % waves + 1, team in heavy))
            i += 1
    return sorted(out)


def _levers_arm(levers: Iterable[str]) -> str:
    ids = sorted(set(levers))
    return f"tokenbill.arm={'+'.join(ids) if ids else 'none'},tokenbill.wave=0"


# ---------------------------------------------------------------------------------------------
# requests
# ---------------------------------------------------------------------------------------------


def _req(method: str, path: str, body: Mapping[str, object], note: str, lever_id: str,
         action_id: str) -> dict[str, object]:
    auth = catalog.ADMIN_ACTIONS[action_id].auth_note or "see the REST reference"
    return {"method": method, "path": path, "api_version": API_VERSION, "body": dict(body),
            "note": note, "lever_id": lever_id, "auth": auth}


def _cutoff(today: str | None) -> str:
    if today is None:
        return "<today minus 30 days>"
    try:
        day = _dt.date.fromisoformat(today[:10])
    except (TypeError, ValueError):
        raise UsageError("today must be a YYYY-MM-DD date") from None
    return (day - _dt.timedelta(days=30)).isoformat()


def _requests(inp: _Inputs, today: str | None) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    ids = inp.action_ids
    if "rest:org_selected_users_delete" in ids:
        cutoff = _cutoff(today)
        out.append(_req(
            "DELETE", "/orgs/{org}/copilot/billing/selected_users",
            {"selected_usernames": [
                f"<from GET /orgs/{{org}}/copilot/billing/seats where last_activity_at < "
                f"{cutoff} and assigning_team is null>"]},
            "Remove idle, directly assigned seats per organization. Fill the list from the seats "
            "query yourself (Token Bill publishes counts, never logins); seats end at the close "
            "of the billing cycle unless they retain access through team membership.",
            "copilot.seat_reclaim", "rest:org_selected_users_delete"))
    if "rest:org_selected_teams_delete" in ids:
        for team, counts in inp.teams.items():
            seats, idle = counts.get("seats", 0), counts.get("idle_seats", 0)
            if seats >= inp.k and seats == idle:
                out.append(_req(
                    "DELETE", "/orgs/{org}/copilot/billing/selected_teams",
                    {"selected_teams": [f"<GitHub team slug of team '{_name(team)}'>"]},
                    f"Team {_name(team)}: every one of its {seats} seats is idle (counts). "
                    "Removing the team ends its members' seats at the end of the billing cycle "
                    "unless they hold a seat another way.",
                    "copilot.seat_reclaim_team", "rest:org_selected_teams_delete"))
    if "rest:cost_center_create" in ids:
        out.append(_req(
            "POST", "/enterprises/{enterprise}/settings/billing/cost-centers",
            {"name": "<new cost center name>", "ai_credit_pool_enabled": True},
            "Create a cost center for unattributed spend and assign its users, organizations or "
            "repositories; with its AI credit pool enabled choose, at the cap, whether members "
            "are blocked or continue as paid overage (billing UI).",
            "copilot.budget_plan", "rest:cost_center_create"))
    if "rest:coding_agent_policy" in ids:
        out.append(_req(
            "PUT", "/enterprises/{enterprise}/copilot/policies/coding_agent", {},
            "VERIFY: fill the body from the enterprise coding agent policy REST reference; Token "
            "Bill does not choose your policy (review runners, setup steps and session limits).",
            "copilot.agent_runner_standard", "rest:coding_agent_policy"))
    budget_reqs = [s["request"] for s in inp.budgets if s.get("request") is not None]
    for req in budget_reqs:
        out.append(dict(req))  # type: ignore[call-overload]
    if "rest:budget_create" in ids and not any(r["path"] == "/enterprises/{enterprise}/settings/"
                                               "billing/budgets" for r in out):
        out.append(_req(
            "POST", "/enterprises/{enterprise}/settings/billing/budgets",
            {"budget_amount": "<whole dollars: forecast overage p90 x 1.1, rounded up>",
             "prevent_further_usage": False,
             "budget_alerting": {"will_alert": True,
                                 "alert_recipients": ["<login of a billing manager to alert>"]},
             "budget_scope": "<enterprise | organization | cost_center>",
             "budget_type": "BundlePricing", "budget_product_sku": "ai_credits",
             "budget_entity_name": "<entity name>"},
            "Template (no budget design given): size the amount from the forecast overage p90; "
            "prevent_further_usage false alerts only (trade-off: true stops paid usage).",
            "copilot.budget_plan", "rest:budget_create"))
    return sorted(out, key=lambda r: (str(r["lever_id"]), str(r["method"]), str(r["path"]),
                                      _canon(r["body"]), str(r["note"])))


def _gh_api(req: Mapping[str, object]) -> str:
    path = str(req["path"]).replace("{org}", "ORG").replace("{enterprise}", "ENTERPRISE") \
        .replace("{cost_center_id}", "COST_CENTER_ID")
    lines = [f"gh api --method {req['method']} -H \"X-GitHub-Api-Version: {API_VERSION}\" "
             f"{path} --input - <<'EOF'", _canon(req["body"]), "EOF"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------
# text files
# ---------------------------------------------------------------------------------------------

_TITLES: Mapping[str, str] = {
    "admin:plan_confirm": "Confirm the Copilot plan (Business or Enterprise)",
    JETBRAINS_ACTION_ID: "JetBrains limits of the managed settings",
    "admin:model_policy": "Model policies (server-side, every editor)",
    "admin:model_policy_fast": "Disable the fast-mode model",
    "admin:org_seat_policy": "Organization seat policy: selected members",
    "admin:seat_plan_change": "Seat plan change (Enterprise to Business)",
    "admin:runner_type": "Runner type for code review and the cloud agent",
    "admin:team_membership_review": "Review Copilot-granting team membership",
    "admin:communicate_auto_tier": "Auto model selection guidance",
    "admin:review_effort_default": "Code review default effort: Lite",
    "admin:communicate_personal_review_settings": "Personal review settings caveat",
    "admin:review_triggers": "Code review triggers",
    "admin:repo_review_mcp_off": "Code review MCP tools setting (on by default)",
    "admin:review_instructions": "Repository custom instructions size",
    "admin:review_unlicensed_policy": "Code review of unlicensed members and bots",
    "admin:ci_limits_snippet": "Session limits in CI",
    "admin:aw_triggers": "Agentic workflow caps and triggers",
    "admin:org_cli_billing_policy": "Policy 'Allow use of Copilot CLI billed to the organization'",
    "admin:paid_usage_policy": "'AI credits paid usage' policy",
    "admin:cost_center_pool": "Cost-center AI credit pools: block or continue at the cap",
    "admin:budget_stop": "Budgets with 'Stop usage when budget limit is reached'",
    "admin:vscode_db_exporter_optin": "VS Code usage database opt-in (developers)",
    "rest:org_selected_users_delete": "Remove idle directly assigned seats (REST)",
    "rest:org_selected_teams_delete": "Remove wholly idle teams (REST)",
    "rest:budget_create": "Create budgets (REST)",
    "rest:cost_center_create": "Create a cost center (REST)",
    "rest:cost_center_patch": "Enable a cost center's AI credit pool (REST)",
    "rest:coding_agent_policy": "Coding agent policy (REST)",
}
_ROLLBACK: Mapping[str, str] = {
    "REST": "the inverse call (re-assign seats with POST …/selected_users or …/selected_teams; "
            "edit or delete the budget; PATCH the cost center back)",
    "personal settings (communicate)": "none needed (communication)",
    "workflow frontmatter": "revert the frontmatter change in the workflow's repository",
    "managed-settings.json": "apply copilot/rollback.patch.json",
}


def _lever_def(lever_id: str | None) -> catalog.LeverDef | None:
    if not lever_id:
        return None
    try:
        return catalog.lever(lever_id)
    except UsageError:
        return None


def _action_projection(action: AdminAction, inp: _Inputs) -> str:
    lv = _lever_def(action.lever_id)
    if action.projection is not None:
        text = figure_text(action.projection)
    elif lv is not None and "Projection per scenario" in action.what:
        text = inp.projection(lv.lever_id)[1] + " (per scenario, plan unknown)"
    elif lv is None or lv.lever_class == "behavioral":
        text = "not projected (behavioral, communication or enabler item)"
    elif inp.known is not None and lever_projection(inp.known, lv.lever_id) is not None:
        text = "not repeated (the lever's projection is on its first item)"
    else:
        text = "not projected (the plan has no value for this lever)"
    reach = f"; reach {action.reach}" if action.reach is not None else "; reach n/a"
    return text + reach


def _checklist(inp: _Inputs) -> str:
    out = ["# GitHub Copilot admin checklist (Token Bill)", "",
           "Nothing here has been applied: every item is a change for an admin to review and make. "
           "REST items are request specs in `github/requests.jsonl`; the managed-settings patch "
           "is `copilot/managed-settings.patch.json`.", ""]
    if inp.unknown:
        out += ["**Plan unknown** — every pool-dependent figure is given for both plans (if "
                "Business / if Enterprise); confirm the plan first.", ""]
    for n, action in enumerate(inp.actions, 1):
        title = _TITLES.get(action.action_id, action.action_id)
        tags = [t for t, on in (("needs evaluation", action.needs_eval),
                                ("trade-off", action.tradeoff)) if on]
        lines = [f"## {n}. {title}", "",
                 f"- What: {sanitize(action.what, 400)}",
                 f"- Where: {action.where}",
                 f"- Docs: {action.doc_url}",
                 f"- Label: {_action_projection(action, inp)}",
                 f"- Evaluation: {', '.join(tags) if tags else 'none needed'}",
                 f"- Deadline: {action.deadline or 'none'}",
                 f"- Rollback: {_ROLLBACK.get(action.where, 'restore the previous setting on the same page')}",  # noqa: E501
                 f"- Auth: {action.auth_note or 'a user who can change this setting'}"]
        if action.rest_file:
            spec = catalog.ADMIN_ACTIONS.get(action.admin_action)
            if spec is not None and spec.rest_method:
                lines.append(f"- REST: `{spec.rest_method} {spec.rest_path}` in "
                             f"`{action.rest_file}` (never executed by Token Bill)")
        if action.lever_id:
            lines.append(f"- Verify: `tokenbill measure plan --lever {action.lever_id}`")
        out += lines + [""]
    if inp.unknown:
        out += ["## Not assessed", "", NOT_ASSESSED_PLAN_UNKNOWN, ""]
    if inp.budgets:
        out += ["## Budget design", ""]
        for scenario in (None, "business", "enterprise"):
            texts = [str(s["text"]) for s in inp.budgets if s["scenario"] == scenario]
            if texts:
                out += [f"### {_SCENARIO_LABEL[scenario]}", ""] + [f"- {t}" for t in texts] + [""]
    return "\n".join(out).rstrip() + "\n"


def _allowed_models(inp: _Inputs) -> str | None:
    """``allowed_models.txt``: the remap targets and the Lightweight / Versatile models not
    retiring, one per line, and exactly one ``fallback:`` line (the cheapest-tier remap target)."""
    targets = sorted({fact.target for fact in _facts.copilot_remaps().values()})
    models = set(targets)
    for model in sorted(_facts.load().copilot.model_categories):
        if catalog.copilot_category(model) in ("Lightweight", "Versatile") and \
                model not in catalog.COPILOT_RETIREMENTS:
            models.add(model)
    if not models:  # pragma: no cover - facts always carry models
        return None
    fallback = next((t for t in targets if catalog.copilot_category(t) == "Versatile"),
                    sorted(models)[0])
    lines = ["# Token Bill: models the Copilot CLI may use in this repository (trade-off:",
             "# evaluate quality first; CLI only, trusted directories only). VERIFY the glob",
             "# syntax against the CLI configuration-directory reference before committing."]
    lines += sorted(models)
    lines.append(f"fallback: {fallback}")
    return "\n".join(lines) + "\n"


def _ci_limits(inp: _Inputs) -> str:
    cap = inp.evidence_int(("ci-uncapped", "direct-org-usage"), "suggested_max_ai_credits")
    n = str(max(_MIN_CLI_CAP, cap)) if cap is not None else "N"
    how = ("N from the ci-uncapped finding (p99 x 1.5 of priced sessions)" if cap is not None
           else "choose N near p99 x 1.5 of your CI sessions' credits (minimum 30)")
    return "\n".join([
        "# Copilot CLI limits in CI (Token Bill; nothing applied automatically)", "",
        "Add a session limit to every `copilot -p` step (a soft limit, minimum 30, Copilot CLI "
        "1.0.67+) and collect the session afterwards so the next scan can see it. "
        f"Here {how}.", "",
        "```yaml",
        "- name: Copilot",
        f"  run: copilot -p \"$PROMPT\" --max-ai-credits {n}",
        "- name: Collect Copilot usage for Token Bill",
        "  if: always()",
        "  run: tokenbill collect copilot-cli --ci --billing-path copilot_direct",
        "```", "",
        "Runs with `GITHUB_TOKEN` in an organization repository are metered to the organization "
        "(user budgets do not apply): review the policy 'Allow use of Copilot CLI billed to the "
        "organization'.", ""])


def _agentic(inp: _Inputs) -> str:
    cap = inp.evidence_int(("agentic-workflow-cost", "ci-uncapped"), "suggested_max_ai_credits")
    n = str(cap) if cap is not None else "N"
    how = ("N from the agentic-workflow-cost finding (p99 x 1.5 of priced runs)" if cap is not None
           else "choose N near p99 x 1.5 of your runs (the default cap is 1,000 AIC per run)")
    return "\n".join([
        "# Agentic workflows (gh-aw): caps, triggers, usage artifacts (Token Bill)", "",
        f"Set a per-run cap in each workflow's frontmatter; {how}:", "",
        "```yaml", "---", f"max-ai-credits: {n}", "---", "```", "",
        "Review `on:` schedules and triggers (every run is billed: Actions minutes plus AI "
        "credits metered to the organization) and the policy 'Allow use of Copilot CLI billed "
        "to the organization' (on by default when Copilot CLI is enabled).", "",
        "Download the runs' token usage for Token Bill (artifact names VERIFY; this takes every "
        "artifact of a run and keeps only `token-usage.jsonl`):", "",
        "```sh",
        "gh run download RUN_ID --dir ./gh-aw-artifacts",
        "find ./gh-aw-artifacts -name token-usage.jsonl -print",
        "tokenbill ingest --db tokenbill.db ./gh-aw-artifacts",
        "```", ""])


def _vscode_optin() -> tuple[str, str]:
    traces = _facts.copilot_vscode_traces()
    setting = traces.enable_setting
    snippet = _pretty({setting: True})
    doc = "\n".join([
        "# VS Code: share your Copilot token counts (developer opt-in)", "",
        f"This is a **user** setting (`{setting}`, default off). Your organization cannot enforce "
        "it through managed settings, so it is entirely your choice.", "",
        "1. Add the setting to your VS Code user settings (see `settings.snippet.json`):", "",
        "```json", snippet.rstrip(), "```", "",
        "2. Run the collector at least daily (VS Code keeps the local database for "
        f"{traces.retention_days} days / {traces.retention_sessions} sessions):", "",
        "```sh", "tokenbill copilot collect --source vscode --out ~/tokenbill-out", "```", "",
        "Schedule it with launchd (macOS), a systemd user timer (Linux) or Task Scheduler "
        "(Windows) running that command once a day.", "",
        "**What is recorded:** token counts per request (input, cached input, cache writes, "
        "output), the model, timestamps and Copilot's own credit estimate. **Never** prompts, "
        "responses, file names, repository names or tool arguments: the collector reads an "
        "allowlist of numeric keys only.", "",
        f"**Opt out:** set `{setting}` back to false and stop the scheduled task; your team's "
        "numbers keep coming from organization billing data.", ""])
    return snippet, doc


def _readme(inp: _Inputs, entries: Sequence[PolicyEntry], json_keys: set[str],
            excluded: Sequence[str], requests: Sequence[Mapping[str, object]],
            waves: Sequence[tuple[str, str, int, bool]], cohort: str, service_name: str,
            telemetry: bool, repo_keys: Sequence[str]) -> str:
    out = [f"# GitHub Copilot policy pack (target {TARGET}, cohort {cohort})", "",
           "Nothing in this pack is applied automatically. Review every file, then apply what "
           "you accept yourself.", ""]
    if inp.unknown:
        out += ["**Plan unknown:** every plan-dependent projection and budget is shown for both "
                "plans (if Business / if Enterprise); no plan is assumed. Confirm the plan first "
                "(checklist item 1).", ""]
    out += ["## Managed settings (`copilot/managed-settings.patch.json`)", "",
            "An RFC 7386 merge patch against your current managed settings. Destination: the "
            "`.github-private` repository file `copilot/managed-settings.json` (applied within "
            "about an hour), MDM or system files. Rollback: `copilot/rollback.patch.json` "
            "restores every previous value (null removes a key that was absent). Org-wide "
            "changes are measured with `tokenbill measure plan --design its` (MEASURED at best).",
            ""]
    for entry in entries:
        if not entry.key.startswith(MANAGED_PREFIX):
            continue
        state = ("written" if entry.key in json_keys else
                 "NOT written (unverified key)" if not entry.verified_key else
                 "NOT written (trade-off; needs --include-tradeoffs and an ab / measure gate)")
        out += [f"### `{entry.key}` = `{entry.value_json}` ({state})", "",
                f"- {entry.note}",
                f"- Needs evaluation: {'yes' if entry.needs_eval else 'no'}; lever "
                f"`{entry.lever_id or 'n/a'}`; minimum version {entry.min_version or 'n/a'}"]
        if entry.lever_id:
            out.append(f"- Verification: `tokenbill measure plan --lever {entry.lever_id}`; "
                       f"post-rollout check: `tokenbill policy check-effect --lever "
                       f"{entry.lever_id}`")
        out.append("")
        if not entry.verified_key:
            path = entry.key[len(MANAGED_PREFIX):]
            out += ["```jsonc", f"// \"{path}\": {entry.value_json}   VERIFY against the "
                    "managed-settings reference before applying", "```", ""]
    if telemetry:
        out += ["### Telemetry", "",
                f"`telemetry.serviceName` is `{_name(service_name)}` (Token Bill ingests that "
                "service name). Add `telemetry.endpoint` and `telemetry.protocol` for your "
                "OpenTelemetry collector before applying (Token Bill does not know them). "
                "Content capture stays off and locked (`captureContent: false`, "
                "`lockCaptureContent: true`). Per-team files may add `resourceAttributes` "
                "(`team.id`, `cost_center`).", ""]
    if _MCP_LEVER in inp.levers:
        out += ["### MCP server lists (guidance, not written)", "",
                "Restrict MCP servers teams do not need with `deniedMcpServers` / "
                "`allowedMcpServers` (server names are yours to choose; Token Bill never reads "
                "them). Reach: CLI, VS Code, the Copilot app and JetBrains; **not** the cloud "
                "agent.", ""]
    if excluded:
        out += ["### Excluded trade-off keys", "",
                "Written only with `--include-tradeoffs`, and each needs an `ab` or "
                "`measure` gate: " + ", ".join(f"`{k}`" for k in excluded) + ".", ""]
    shares = team_editor_shares(inp.teams)
    named = [(t, s) for t, s in shares.items() if (s[2] or 0) >= inp.k]
    if named:
        out += ["### Reach of managed `model` per team (teams of at least k people)", "",
                "| team | JetBrains share | managed-model reach | delivery |", "|---|---|---|---|"]
        for team, (share, _, _) in named:
            exc, tot = _excluded_share(inp.teams[team])
            heavy = share >= JETBRAINS_HEAVY_SHARE
            delivery = ("server-side model policy first (reach 1), Auto tier guidance" if heavy
                        else "managed \"model\": \"auto\"" if share == 0 else
                        "managed model + server-side model policy for JetBrains users")
            out.append(f"| {_name(team)} | {_pct(int(share * 1000), 1000)} | "
                       f"{_reach_text(exc, tot)} | {delivery} |")
        out.append("")
    if waves:
        out += ["## Team files for waves (`copilot/teams/<team>.json`)", "",
                "Stepped-wedge rollout: the enterprise level sets `model` to "
                "`{\"overridable\": \"auto\"}` and each wave's team files set `\"model\": "
                "\"auto\"` (JetBrains-heavy teams are spread over the waves because managed "
                "`model` does not reach them). VERIFY the team-file mechanism (file location and "
                "`copilot/team-mappings.patch.json` format) against the managed-settings "
                "reference.", "", "| wave | team | file |", "|---|---|---|"]
        for team, slug, wave, _heavy in sorted(waves, key=lambda w: (w[2], w[0])):
            out.append(f"| {wave} | {_name(team)} | `copilot/teams/{slug}.json` |")
        out.append("")
    if repo_keys:
        out += ["## Repository settings (`repo/.github/copilot/settings.patch.json`)", "",
                "Copilot CLI only, and only in trusted directories. Keys: "
                + ", ".join(f"`{k}`" for k in repo_keys) + ".", ""]
    if requests:
        out += ["## REST requests (`github/requests.jsonl`; never executed)", "",
                "Equivalent `gh api` invocations, for reading (replace the upper-case "
                "placeholders and every `<…>` value first):", ""]
        for req in requests:
            out += [f"- {req['method']} {req['path']} (lever `{req['lever_id']}`; auth: "
                    f"{req['auth']})", "", "```sh", _gh_api(req), "```", ""]
    if inp.unknown:
        out += ["## Not assessed", "", NOT_ASSESSED_PLAN_UNKNOWN, ""]
    out += ["## Files", "", "- `copilot/managed-settings.patch.json`, "
            "`copilot/rollback.patch.json`", "- `github/admin-checklist.md`, "
            "`github/requests.jsonl`", "- `ci/copilot-limits.md`, `ci/agentic-workflows.md`",
            "- `vscode/settings.snippet.json`, `vscode/agent-traces-optin.md` (developer opt-in; "
            "not enforceable)", ""]
    return "\n".join(out).rstrip() + "\n"


def _guard(files: Sequence[tuple[str, str]]) -> None:
    for path, text in files:
        if _PSEUDONYM_RE.search(text):
            raise PrivacyError(f"policy pack file {path} would contain a pseudonym")


def _check_cohort(cohort_by: object) -> bool:
    if cohort_by in (None, "none", "all"):
        return False
    if cohort_by == "team":
        return True
    raise UsageError("cohort_by must be None or 'team' for target github-copilot")


def build_copilot_packs(plans_by_scenario: object, findings: Sequence[Finding],
                        actions: Sequence[AdminAction], *, current: Mapping[str, object] | None,
                        cohort_by: str | None, include_tradeoffs: bool,
                        teams: Mapping[str, Mapping[str, int]] | None,
                        budgets: Sequence[Mapping[str, object]] | None,
                        otel_service_name: str | None = None, k: int = 5,
                        today: str | None = None) -> list[PolicyPack]:
    """The Copilot policy packs (target ``github-copilot``; module docstring).

    *plans_by_scenario*: CP-PLAN's ``(key, ActionPlan)`` pairs; *findings*: the run's findings
    (only Copilot ones are read); *actions*: :func:`~tokenbill.copilot.admin_actions.admin_actions`;
    *current*: the current managed settings (``load_current``); *cohort_by*: None or ``"team"``
    (team files and one extra pack per team of at least *k* people); *teams*: :func:`team_counts`;
    *budgets*: :func:`~tokenbill.copilot.budgets.budget_design`; *otel_service_name*: the managed
    ``telemetry.serviceName`` (default ``github-copilot``). Additive keywords: *k* (default 5) and
    *today* (``YYYY-MM-DD``, the seat-query cut-off). The first pack is cohort ``all``. Unknown
    settings keys → ``UsageError``; nothing is applied."""
    if type(k) is not int or k < 1:
        raise UsageError("k-anonymity threshold k must be an int >= 1")
    if not isinstance(include_tradeoffs, bool):
        raise UsageError("include_tradeoffs must be a bool")
    by_team = _check_cohort(cohort_by)
    if otel_service_name is not None and (not isinstance(otel_service_name, str)
                                          or not otel_service_name.strip()):
        raise UsageError("otel_service_name must be a non-empty string")
    service_name = otel_service_name.split("=", 1)[0].strip() if otel_service_name else \
        DEFAULT_SERVICE_NAME
    inp = _Inputs(plans_by_scenario, findings, actions, teams, budgets, k)
    cur = load_current(None) if current is None else current
    if not isinstance(cur, Mapping):
        raise UsageError("--current must be a JSON object")

    wants = {w.key: w for w in _fix_wants(inp.findings)}
    auto = _AUTO_LEVER in inp.levers or _MODEL_KEY in wants
    telemetry = _TELEMETRY_LEVER in inp.levers or otel_service_name is not None
    if auto and _MODEL_KEY not in wants:
        wants[_MODEL_KEY] = _Want(_MODEL_KEY, "auto", _AUTO_LEVER)
    if telemetry:
        for key, value in (("enabled", True), ("captureContent", False),
                           ("lockCaptureContent", True), ("serviceName", service_name)):
            full = f"copilot.managed.telemetry.{key}"
            wants.setdefault(full, _Want(full, value, _TELEMETRY_LEVER))
    waves = _team_waves(inp) if by_team and _MODEL_KEY in wants else []
    if waves and wants[_MODEL_KEY].value == "auto":
        wants[_MODEL_KEY] = _Want(_MODEL_KEY, {"overridable": "auto"}, _AUTO_LEVER)

    managed: dict[str, object] = {}
    repo: dict[str, object] = {}
    json_keys: set[str] = set()
    excluded: list[str] = []
    entries: list[PolicyEntry] = []
    for key in sorted(wants):
        want = wants[key]
        allowed = catalog.copilot_allowed(key)
        if not key.startswith((MANAGED_PREFIX, REPO_PREFIX)) or key == _ALLOWED_MODELS_KEY:
            continue            # ci / agentic-workflow keys: the ci/*.md snippets
        entries.append(_entry(want, inp, _reach_note(key, inp.teams)))
        if not allowed.verified:
            continue
        if allowed.tradeoff and not include_tradeoffs:
            excluded.append(key)
            continue
        json_keys.add(key)
        if key.startswith(MANAGED_PREFIX):
            managed[key] = want.value
        else:
            repo[key[len(REPO_PREFIX):]] = want.value
    patch, rollback = managed_settings_patch(managed, cur)

    files: list[tuple[str, str]] = [
        ("copilot/managed-settings.patch.json", _pretty(patch)),
        ("copilot/rollback.patch.json", _pretty(rollback)),
        ("github/admin-checklist.md", _checklist(inp)),
        ("ci/copilot-limits.md", _ci_limits(inp)),
        ("ci/agentic-workflows.md", _agentic(inp)),
    ]
    requests = _requests(inp, today)
    files.append(("github/requests.jsonl", "".join(_canon(r) + "\n" for r in requests)))
    snippet, optin = _vscode_optin()
    files += [("vscode/settings.snippet.json", snippet), ("vscode/agent-traces-optin.md", optin)]
    if repo:
        files.append(("repo/.github/copilot/settings.patch.json", _pretty(repo)))
    if include_tradeoffs and (_POLICY_LEVER in inp.levers or _ALLOWED_MODELS_KEY in wants):
        text = _allowed_models(inp)
        if text is not None:
            files.append(("repo/.github/allowed_models.txt", text))
    team_packs: list[PolicyPack] = []
    if waves and _MODEL_KEY in json_keys:
        mapping = {}
        model_entry = next(e for e in entries if e.key == _MODEL_KEY)
        for team, slug, wave, _heavy in waves:
            path = f"copilot/teams/{slug}.json"
            team_file = {"model": "auto"}
            if telemetry:
                team_file["telemetry"] = {"resourceAttributes": {  # type: ignore[assignment]
                    "team.id": _name(team), "tokenbill.arm": _AUTO_LEVER,
                    "tokenbill.wave": str(wave)}}
            files.append((path, _pretty(team_file)))
            mapping[_name(team)] = {"settings_file": path, "wave": wave}
            team_patch, team_rollback = _diff({}, team_file), _diff(team_file, {})
            team_readme = (f"# Team file for {_name(team)} (wave {wave})\n\n"
                           f"`{path}` sets the managed `model` to `auto` for this team. "
                           f"{model_entry.note}\n")
            team_packs.append(PolicyPack(
                target=TARGET, cohort=_name(team), merge_patch_json=_canon(team_patch),
                rollback_patch_json=_canon(team_rollback),
                entries=(PolicyEntry(key=_MODEL_KEY, value_json=_canon("auto"),
                                     projection=None, lever_id=_AUTO_LEVER,
                                     needs_eval=model_entry.needs_eval, verified_key=True,
                                     min_version=model_entry.min_version,
                                     note=model_entry.note),),
                otel_resource_attributes=f"tokenbill.arm={_AUTO_LEVER},tokenbill.wave={wave}",
                readme_md=team_readme, hooks=((path, _pretty(team_file)),)))
        files.append(("copilot/team-mappings.patch.json", _pretty({"teams": mapping})))
    readme = _readme(inp, entries, json_keys, excluded, requests, waves, "all", service_name,
                     telemetry, sorted(repo))
    files.sort()
    _guard(files + [("README.md", readme)])
    arm_levers = {e.lever_id for e in entries if e.key in json_keys and e.lever_id}
    main = PolicyPack(target=TARGET, cohort="all", merge_patch_json=_canon(patch),
                      rollback_patch_json=_canon(rollback), entries=tuple(entries),
                      otel_resource_attributes=_levers_arm(arm_levers), readme_md=readme,
                      hooks=tuple(files))
    return [main] + sorted(team_packs, key=lambda p: p.cohort)


def write_pack(pack: PolicyPack, out_dir: Path) -> list[Path]:
    """Write *pack*'s README (``README.md``, or ``teams/<cohort>/README.md`` for a team pack) and
    its hook files under *out_dir*; relative paths only (``..`` or absolute → ``UsageError``).
    Returns the written paths, sorted. Existing files are overwritten; nothing else is touched."""
    root = Path(out_dir)
    readme = "README.md" if pack.cohort == "all" else \
        f"copilot/teams/README-{_SLUG_RE.sub('-', pack.cohort).strip('-.').lower() or 'team'}.md"
    written = []
    for rel, text in [(readme, pack.readme_md), *pack.hooks]:
        parts = Path(rel).parts
        if Path(rel).is_absolute() or ".." in parts or not parts:
            raise UsageError("policy pack paths must be relative")
        dest = root.joinpath(*parts)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        written.append(dest)
    return sorted(written)
