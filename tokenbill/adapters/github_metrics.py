"""GitHub Copilot usage-metrics adapter (addendum §5.5; package CP-ORGDATA).

:class:`CopilotMetricsAdapter` (registry name ``github-copilot-metrics``) reads the NDJSON of the
Copilot usage-metrics reports (the documented JSON-array form of the example schema and CP-PULL
envelopes are accepted too), parsed with exact numbers:

* ``users-1-day`` → one :class:`~tokenbill.core.records.ActivityDay` per user and day under the
  documented field names (``user_initiated_interaction_count`` → ``interactions``,
  ``loc_suggested_to_add_sum`` → ``loc_suggested_add``, …, ``totals_by_cli`` → ``cli_*``,
  ``totals_by_copilot_app`` → ``app_*``, ``feature:<f>`` = interactions + generations,
  ``model:<id>`` from ``totals_by_model_feature`` (interactions + generations), ``ide:<ide>`` from
  ``totals_by_ide`` (interaction counts; the raw IDE name normalized to ``[a-z0-9._-]``, the
  editor family is applied by consumers through ``core.catalog.editor_family``)),
  ``ai_credits_used`` → ``reported_cost_nano`` (a provider estimate, R12: 12.5 credits →
  125,000,000 nano-USD); the login and user id are used for the team / cost-center maps and the
  ``p_`` pseudonym, then dropped. Undocumented top-level keys are ignored and counted in
  ``dq.unknown_fields`` (a record with ``loc_added`` instead of ``loc_added_sum`` has no
  ``loc_added`` count).
* the same user-days per (day, team) → team :class:`OutcomeAggregate` rows, merged below
  ``opts.k_anonymity`` people with ``core.kanon.merge_small_groups`` (``dq.outcomes_suppressed``).
* ``enterprise-1-day`` / ``organization-1-day`` → ``(enterprise)`` / ``(org:<org>)`` rows with the
  pull-request totals in ``extra``; enterprise and organization rows are never added together.
* ``repos-1-day`` → one pull-request total per day (``source_kind``
  ``github.copilot_metrics.repos``); repository ids and names are never read into a record.
* 28-day files — API ``*-28-day`` reports and the **usage-dashboard NDJSON export** (UI, 28-day,
  excludes Copilot CLI; the no-token handoff path) — are accepted only in the API 28-day shape
  (``report_start_day`` / ``report_end_day`` with ``day_totals[]`` or per-user records), flagged
  ``dq.copilot_dashboard_export`` + ``dq.copilot_28day_window`` and kept apart under
  ``source_kind`` ``github.copilot_metrics.28day`` (dated ``report_end_day``), so they are never
  joined with daily team rows; any other record carrying a 28-day window is quarantined as
  ``dashboard-shape-unverified`` (**VERIFY** against a real export, addendum §19.5 #34).
* ``user-teams-1-day`` records are not stored (``copilot.teammap.build_maps`` reads them); legacy
  Copilot metrics JSON is counted and skipped (``dq.copilot_legacy_metrics``).

Masked customization names (``totals_by_mcp`` …) and language breakdowns are never read.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tokenbill.adapters.github_config import (
    PRODUCT,
    UNMAPPED_TEAM,
    BadRecord,
    Item,
    OrgRead,
    count_of,
    day_start_ms,
    decimal_of,
    head_keys,
    login_key,
    parse_day,
    safe_label,
)
from tokenbill.core.kanon import merge_small_groups
from tokenbill.core.models import normalize_copilot_model
from tokenbill.core.money import credits_str_to_nano
from tokenbill.core.records import MAX_TOKENS, ActivityDay, OutcomeAggregate, record_key
from tokenbill.core.types import IngestOptions, IngestResult

__all__ = [
    "FEATURES",
    "SOURCE_KIND",
    "SOURCE_KIND_28DAY",
    "SOURCE_KIND_REPOS",
    "CopilotMetricsAdapter",
    "ide_key",
    "model_key",
]

SOURCE_KIND = "github.copilot_metrics"
#: 28-day files (API ``*-28-day`` reports, the usage-dashboard export): apart from daily rows.
SOURCE_KIND_28DAY = "github.copilot_metrics.28day"
#: Pull-request totals summed from ``repos-1-day`` (apart from the aggregated-report rows).
SOURCE_KIND_REPOS = "github.copilot_metrics.repos"
#: Documented ``feature`` values (usage-metrics field reference); others count as ``others``.
FEATURES = ("code_completion", "chat_inline", "chat_panel_ask_mode", "chat_panel_edit_mode",
            "chat_panel_agent_mode", "chat_panel_plan_mode", "chat_panel_custom_mode",
            "chat_panel_unknown_mode", "agent_edit", "copilot_cli", "copilot_app", "others")
_TOP_COUNTS = (
    ("user_initiated_interaction_count", "interactions"),
    ("code_generation_activity_count", "code_generation"),
    ("code_acceptance_activity_count", "code_acceptance"),
    ("loc_suggested_to_add_sum", "loc_suggested_add"),
    ("loc_suggested_to_delete_sum", "loc_suggested_delete"),
    ("loc_added_sum", "loc_added"), ("loc_deleted_sum", "loc_deleted"),
    ("distinct_mcp_use_count", "mcp_distinct"), ("distinct_skill_use_count", "skill_distinct"),
    ("distinct_custom_agent_use_count", "custom_agent_distinct"),
    ("distinct_plugin_use_count", "plugin_distinct"),
    ("distinct_slash_cmd_use_count", "slash_cmd_distinct"),
)
_SURFACES = (("totals_by_cli", "cli"), ("totals_by_copilot_app", "app"))
_SURFACE_COUNTS = (("session_count", "sessions"), ("request_count", "requests"),
                   ("prompt_count", "prompts"))
_SURFACE_TOKENS = (("prompt_tokens_sum", "prompt_tokens"), ("output_tokens_sum", "output_tokens"))
_FLAGS = (("used_chat", "used_chat"), ("used_agent", "used_agent"), ("used_cli", "used_cli"),
          ("used_copilot_app", "used_copilot_app"),
          ("used_copilot_cloud_agent", "used_cloud_agent"),
          ("used_copilot_coding_agent", "used_cloud_agent"),
          ("used_copilot_code_review_active", "used_code_review_active"),
          ("used_copilot_code_review_passive", "used_code_review_passive"))
_PR_EXTRA = (("total_merged", "prs_merged"), ("total_created_by_copilot", "prs_created_by_copilot"),
             ("total_merged_created_by_copilot", "prs_merged_created_by_copilot"),
             ("total_reviewed_by_copilot", "prs_reviewed_by_copilot"),
             ("total_copilot_suggestions", "copilot_suggestions"),
             ("total_copilot_applied_suggestions", "copilot_applied_suggestions"))
_PARTITION = frozenset({"day", "enterprise_id", "organization_id", "etl_id", "day_partition",
                        "entity_id_partition"})
_BREAKDOWNS = frozenset({
    "user_initiated_interaction_count", "code_generation_activity_count",
    "code_acceptance_activity_count", "loc_suggested_to_add_sum", "loc_suggested_to_delete_sum",
    "loc_added_sum", "loc_deleted_sum", "distinct_skill_use_count",
    "distinct_custom_agent_use_count", "distinct_mcp_use_count", "distinct_slash_cmd_use_count",
    "distinct_plugin_use_count", "totals_by_skill", "totals_by_custom_agent", "totals_by_mcp",
    "totals_by_slash_cmd", "totals_by_plugin", "totals_by_cli", "totals_by_copilot_app",
    "totals_by_3rd_party_agent", "totals_by_ide", "totals_by_feature",
    "totals_by_language_feature", "totals_by_language_model", "totals_by_model_feature"})
#: Documented per-user report fields (1-day and 28-day, usage-metrics field reference).
USER_FIELDS = _PARTITION | _BREAKDOWNS | frozenset({
    "user_id", "user_login", "ai_credits_used", "used_agent", "used_chat", "used_cli",
    "used_copilot_app", "used_copilot_coding_agent", "used_copilot_cloud_agent",
    "used_copilot_code_review_active", "used_copilot_code_review_passive", "ai_adoption_phase",
    "report_start_day", "report_end_day", "created_at"})
#: Documented aggregated (enterprise / organization) report fields.
AGGREGATE_FIELDS = _PARTITION | _BREAKDOWNS | frozenset({
    "totals_by_ai_adoption_phase", "pull_requests", "daily_active_users", "weekly_active_users",
    "monthly_active_users", "monthly_active_chat_users", "monthly_active_agent_users",
    *(f"{p}_{a}_copilot_{f}_users" for p in ("daily", "weekly", "monthly")
      for a in ("active", "passive") for f in ("cloud_agent", "code_review")),
    "daily_active_cli_users", "daily_active_copilot_app_users"})
_LEGACY_MARKERS = ("total_active_users", "copilot_ide_code_completions", "copilot_ide_chat",
                   "total_suggestions_count")
_SAFE_KEY_RE = re.compile(r"[^a-z0-9._-]+")
_ORG_PATH_RE = re.compile(r"/orgs/([^/]+)/")


def model_key(raw: object) -> str:
    """The ``model:<id>`` suffix of a ``totals_by_model_feature`` model: ``auto`` / ``unknown`` /
    ``others`` kept, other ids through ``core.models.normalize_copilot_model``; ``unknown`` when
    the label names no model."""
    if not isinstance(raw, str) or not raw.strip():
        return "unknown"
    low = raw.strip().lower()
    if low in ("auto", "unknown", "others"):
        return low
    cm = normalize_copilot_model(raw)
    return _safe_key(cm.model) if not cm.pseudo else "unknown"


def ide_key(raw: object) -> str:
    """The ``ide:<ide>`` suffix: the raw IDE name lower-cased, other characters than
    ``[a-z0-9._-]`` collapsed to ``-`` (``vscode``, ``intellij``, ``jetbrains-rider``, …)."""
    return _safe_key(raw.strip().lower()) if isinstance(raw, str) else "unknown"


def _safe_key(text: str) -> str:
    return _SAFE_KEY_RE.sub("-", text).strip("-")[:64].strip("-") or "unknown"


def _add(counts: dict[str, int], key: str, value: int) -> None:
    total = counts.get(key, 0) + value
    if total > MAX_TOKENS:
        raise BadRecord(f"bad_type:{key}")
    counts[key] = total


@dataclass
class _Teams:
    """(source kind, day) → team → [principals, lines added, lines removed, edits accepted]."""

    days: dict[tuple[str, str], dict[str, list[Any]]] = field(default_factory=dict)

    def add(self, source_kind: str, day: str, team: str, principal: str,
            payload: tuple[int, int, int]) -> None:
        cell = self.days.setdefault((source_kind, day), {}).setdefault(team, [set(), 0, 0, 0])
        cell[0].add(principal)
        for i, v in enumerate(payload, start=1):
            cell[i] += v


class CopilotMetricsAdapter:
    """Usage-metrics NDJSON → ``ActivityDay`` per user-day and k-merged team / entity outcomes."""

    name = "github-copilot-metrics"
    capabilities = frozenset({"activity", "outcomes"})

    def sniff(self, path: Path, head: bytes) -> bool:
        """Per-user, aggregated, 28-day (any record with a report window, so an unverified
        dashboard shape is quarantined rather than left unread), user-teams, repository and
        legacy metrics records."""
        keys = head_keys(head)
        if "user_login" in keys and keys & {"ai_credits_used", "user_initiated_interaction_count",
                                            "loc_added_sum", "totals_by_ide", "used_chat",
                                            "code_generation_activity_count"}:
            return True
        if keys & {"day_totals", "daily_active_users", "report_start_day", "report_end_day"}:
            return True  # a 28-day window of any shape is claimed (and checked by read)
        if {"user_login", "team_id", "slug"} <= keys or {"repo_id", "pull_requests"} <= keys:
            return True
        return ("total_active_users" in keys and ("total_engaged_users" in keys
                                                  or "copilot_ide_code_completions" in keys)) or (
            "total_suggestions_count" in keys and "breakdown" in keys)

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Parse every metrics record of *path* (file or directory)."""
        ctx = OrgRead(self.name, path, opts)
        run = _Run(ctx)
        for item in ctx.items():
            for loc, rec in _records(ctx, item):
                try:
                    run.record(item, loc, rec)
                except BadRecord as exc:
                    ctx.quarantine(loc, exc.reason)
        return run.finish(self.capabilities)


def _records(ctx: OrgRead, item: Item) -> Iterator[tuple[str, dict[str, Any]]]:
    if isinstance(item.body, list):
        for i, rec in enumerate(item.body):
            if isinstance(rec, dict):
                yield f"{item.locator}[{i}]", rec
            else:
                ctx.quarantine(f"{item.locator}[{i}]", "not_object")
    elif isinstance(item.body, dict):
        yield item.locator, item.body
    else:
        ctx.quarantine(item.locator, "not_object")


class _Run:
    """The state of one metrics read."""

    def __init__(self, ctx: OrgRead) -> None:
        self.ctx = ctx
        self.activity: dict[tuple[str, str, str], ActivityDay] = {}
        self.teams = _Teams()
        self.outcomes: dict[tuple[str, str, str], OutcomeAggregate] = {}
        self.repos: dict[tuple[str, str], list[int]] = {}
        self.window = 0

    # ----- dispatch -------------------------------------------------------------------------

    def record(self, item: Item, loc: str, rec: dict[str, Any]) -> None:
        ctx = self.ctx
        if "download_links" in rec:
            ctx.stat("link_documents")  # a report-links response: the NDJSON is a separate file
            return
        if any(k in rec for k in _LEGACY_MARKERS) or ("date" in rec and "total_engaged_users"
                                                       in rec):
            ctx.stat("legacy_records")
            ctx.note("dq.copilot_legacy_metrics", "info",
                     "legacy Copilot metrics API records counted and skipped")
            return
        if "day_totals" in rec or "report_start_day" in rec or "report_end_day" in rec:
            self._window(item, loc, rec)
            return
        if "team_id" in rec or "slug" in rec:
            ctx.stat("user_teams_records")  # read by copilot team-map, never stored
            return
        if "repo_id" in rec or "repo_name" in rec:
            self._repo(item, rec)
            return
        if "user_login" in rec or "user_id" in rec:
            day = parse_day(rec.get("day"), "day") if rec.get("day") is not None else None
            if day is None:
                raise BadRecord("missing:day")
            self._user(rec, day, SOURCE_KIND, ctx.fetched(item))
            return
        if rec.get("day") is not None and ("enterprise_id" in rec or "organization_id" in rec):
            self._aggregate(item, rec, parse_day(rec["day"], "day"), SOURCE_KIND)
            return
        raise BadRecord("missing:day" if "day" not in rec else "unknown_shape")

    # ----- 28-day files -----------------------------------------------------------------------

    def _window(self, item: Item, loc: str, rec: dict[str, Any]) -> None:
        try:
            parse_day(rec.get("report_start_day"), "report_start_day")
            end = parse_day(rec.get("report_end_day"), "report_end_day")
        except BadRecord:
            raise BadRecord("dashboard-shape-unverified") from None
        totals = rec.get("day_totals")
        if isinstance(totals, list):
            for i, entry in enumerate(totals):
                try:
                    if not isinstance(entry, dict) or entry.get("day") is None:
                        raise BadRecord("missing:day")
                    merged = {k: v for k, v in rec.items() if k in _PARTITION and k != "day"}
                    merged.update(entry)
                    self._aggregate(item, merged, parse_day(entry["day"], "day"),
                                    SOURCE_KIND_28DAY)
                except BadRecord as exc:
                    self.ctx.quarantine(f"{loc}/day_totals[{i}]", exc.reason)
        elif "day_totals" not in rec and login_key(rec.get("user_login")) is not None:
            self._user(rec, end, SOURCE_KIND_28DAY, self.ctx.fetched(item))
        else:
            raise BadRecord("dashboard-shape-unverified")
        self.window += 1

    # ----- per-user records -------------------------------------------------------------------

    def _user(self, rec: dict[str, Any], day: str, source_kind: str, fetched: int) -> None:
        ctx = self.ctx
        if not ctx.in_window(day_start_ms(day)):
            ctx.stat("outside_window")
            return
        login, user_id = rec.get("user_login"), rec.get("user_id")
        if login_key(login) is None:
            raise BadRecord("missing:user_login")
        counts = _user_counts(rec)
        flags = sorted({flag for src, flag in _FLAGS if rec.get(src) is True})
        cost = None
        if rec.get("ai_credits_used") is not None:
            credits = decimal_of(rec["ai_credits_used"], "ai_credits_used")
            if credits < 0:
                raise BadRecord("bad_type:ai_credits_used")
            try:
                cost = credits_str_to_nano(str(credits))[0]
            except ValueError:
                raise BadRecord("bad_type:ai_credits_used") from None
        unknown = sum(1 for k in rec if k not in USER_FIELDS)
        principal = ctx.principal(login)
        key = (source_kind, day, principal)
        if key in self.activity:
            ctx.stat("duplicate_records")
            return
        team = ctx.team(login, user_id)
        try:
            self.activity[key] = ActivityDay(
                date_utc=day, product=PRODUCT, principal=principal, team=team,
                cost_center=ctx.cost_center(login, user_id), reported_cost_nano=cost,
                counts=tuple(sorted(counts.items())), flags=tuple(flags),
                fetched_ms=fetched, source_kind=source_kind)
        except Exception:  # a ContractViolation of a hostile value: never echo it
            raise BadRecord("bad_record") from None
        if unknown:
            ctx.note("dq.unknown_fields", "info", "undocumented usage-metrics fields ignored",
                     unknown)
        ctx.stat("records")
        self.teams.add(source_kind, day, team or UNMAPPED_TEAM, principal,
                       (counts.get("loc_added", 0), counts.get("loc_deleted", 0),
                        counts.get("code_acceptance", 0)))

    # ----- aggregated records -----------------------------------------------------------------

    def _org_label(self, item: Item, org_id: object) -> str:
        m = _ORG_PATH_RE.search(item.path or "")
        org = safe_label(m.group(1)) if m else safe_label(self.ctx.opts.attribution.workspace_id)
        if org is None:
            org = safe_label(str(org_id)) if isinstance(org_id, (str, int)) else None
        return f"(org:{org or 'unknown'})"

    def _aggregate(self, item: Item, rec: Mapping[str, Any], day: str, source_kind: str) -> None:
        ctx = self.ctx
        if not ctx.in_window(day_start_ms(day)):
            ctx.stat("outside_window")
            return
        org_id = rec.get("organization_id")
        label = self._org_label(item, org_id) if org_id not in (None, "") else "(enterprise)"
        pr = rec.get("pull_requests") if isinstance(rec.get("pull_requests"), dict) else {}
        extra = tuple(sorted((key, count_of(pr[src], f"pull_requests.{src}"))
                             for src, key in _PR_EXTRA if pr.get(src) is not None))
        try:
            outcome = OutcomeAggregate(
                date_utc=day, team=label, n_users=_opt_count(rec, "daily_active_users"),
                sessions=0, commits=0, pull_requests=dict(extra).get("prs_merged", 0),
                lines_added=_opt_count(rec, "loc_added_sum"),
                lines_removed=_opt_count(rec, "loc_deleted_sum"),
                edits_accepted=_opt_count(rec, "code_acceptance_activity_count"),
                edits_rejected=0, source_kind=source_kind, extra=extra)
        except BadRecord:
            raise
        except Exception:  # a ContractViolation of a hostile value: never echo it
            raise BadRecord("bad_record") from None
        unknown = sum(1 for k in rec if k not in AGGREGATE_FIELDS)
        if unknown:
            ctx.note("dq.unknown_fields", "info", "undocumented usage-metrics fields ignored",
                     unknown)
        key = (day, label, source_kind)
        if key in self.outcomes:
            ctx.stat("duplicate_records")
            return
        ctx.stat("records")
        self.outcomes[key] = outcome

    def _repo(self, item: Item, rec: Mapping[str, Any]) -> None:
        ctx = self.ctx
        day = parse_day(rec.get("day"), "day")
        if not ctx.in_window(day_start_ms(day)):
            ctx.stat("outside_window")
            return
        m = _ORG_PATH_RE.search(item.path or "")
        org = safe_label(m.group(1)) if m else safe_label(ctx.opts.attribution.workspace_id)
        label = f"(org:{org})" if org else "(enterprise)"
        pr = rec.get("pull_requests") if isinstance(rec.get("pull_requests"), dict) else {}
        values = [count_of(pr[src], f"pull_requests.{src}") if pr.get(src) is not None else 0
                  for src, _ in _PR_EXTRA]
        cell = self.repos.setdefault((day, label), [0] * len(_PR_EXTRA))
        for i, v in enumerate(values):
            if cell[i] + v > MAX_TOKENS:
                raise BadRecord("bad_type:pull_requests")
        for i, v in enumerate(values):
            cell[i] += v
        ctx.stat("records")
        ctx.stat("repo_rows")

    # ----- result -----------------------------------------------------------------------------

    def finish(self, declared: frozenset[str]) -> IngestResult:
        ctx = self.ctx
        outcomes = list(self.outcomes.values())
        for (day, label), values in sorted(self.repos.items()):
            extra = tuple(sorted((key, v) for (_, key), v in zip(_PR_EXTRA, values, strict=True)))
            outcomes.append(OutcomeAggregate(
                date_utc=day, team=label, n_users=0, sessions=0, commits=0,
                pull_requests=values[0], lines_added=0, lines_removed=0, edits_accepted=0,
                edits_rejected=0, source_kind=SOURCE_KIND_REPOS, extra=extra))
        dropped_groups = dropped_users = 0
        for (source_kind, day), teams in sorted(self.teams.days.items()):
            rows = [(team, len(cell[0]), tuple(cell[1:])) for team, cell in sorted(teams.items())]
            merged, dropped = merge_small_groups(rows, k=ctx.opts.k_anonymity)
            kept = {label for label, _, _ in merged}
            dropped_users += sum(n for label, n, _ in rows if label not in kept and dropped)
            dropped_groups += dropped
            for label, n, (added, removed, accepted) in merged:
                if max(added, removed, accepted) > MAX_TOKENS:
                    ctx.stat("team_rows_out_of_range")
                    continue
                outcomes.append(OutcomeAggregate(
                    date_utc=day, team=label, n_users=n, sessions=0, commits=0, pull_requests=0,
                    lines_added=added, lines_removed=removed, edits_accepted=accepted,
                    edits_rejected=0, source_kind=source_kind))
        if dropped_groups:
            ctx.note("dq.outcomes_suppressed", "info", "team outcome groups below k dropped",
                     dropped_groups)
            ctx.stat("users_dropped", dropped_users)
        if self.window:
            ctx.note("dq.copilot_28day_window", "info", "28-day usage-metrics records: kept "
                     "apart from daily rows (source kind github.copilot_metrics.28day)",
                     self.window)
            ctx.note("dq.copilot_dashboard_export", "info", "28-day NDJSON in the API shape "
                     "(usage-dashboard export or API 28-day report); a dashboard export excludes "
                     "Copilot CLI usage; export shape VERIFY", self.window)
        return ctx.result(declared, activity=self._activity(), outcomes=outcomes)

    def _activity(self) -> list[ActivityDay]:
        """One activity row per natural key (date, product, principal): a daily row wins over a
        28-day row dated on the same day (the record store would otherwise replace one by the
        other)."""
        out: dict[str, ActivityDay] = {}
        superseded = 0
        # sorted by source kind: the daily rows ("github.copilot_metrics") come first
        for _, day in sorted(self.activity.items()):
            key = record_key(day)
            if key in out:
                superseded += 1
            else:
                out[key] = day
        if superseded:
            self.ctx.stat("activity_28day_superseded", superseded)
            self.ctx.note("dq.copilot_28day_window", "info", "28-day user rows dated like a "
                          "daily row of the same person were not kept (the daily row wins)",
                          superseded)
        return list(out.values())


def _opt_count(rec: Mapping[str, Any], key: str) -> int:
    return count_of(rec[key], key) if rec.get(key) is not None else 0


def _entry_counts(entry: object, fields: tuple[str, ...]) -> int:
    if not isinstance(entry, dict):
        raise BadRecord("not_object")
    return sum(count_of(entry[f], f) for f in fields if entry.get(f) is not None)


def _user_counts(rec: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for src, key in _TOP_COUNTS:
        if rec.get(src) is not None:
            counts[key] = count_of(rec[src], src)
    for src, prefix in _SURFACES:
        block = rec.get(src)
        if not isinstance(block, dict):
            continue
        for f, key in _SURFACE_COUNTS:
            if block.get(f) is not None:
                counts[f"{prefix}_{key}"] = count_of(block[f], f"{src}.{f}")
        usage = block.get("token_usage") if isinstance(block.get("token_usage"), dict) else {}
        for f, key in _SURFACE_TOKENS:
            if usage.get(f) is not None:
                counts[f"{prefix}_{key}"] = count_of(usage[f], f"{src}.token_usage.{f}")
    agents = rec.get("totals_by_3rd_party_agent")
    if isinstance(agents, list):
        counts["third_party_agent_jobs"] = 0
        for entry in agents:
            _add(counts, "third_party_agent_jobs",
                 _entry_counts(entry, ("user_initiated_interaction_count",)))
    both = ("user_initiated_interaction_count", "code_generation_activity_count")
    for entry in _entries(rec, "totals_by_feature"):
        feature = entry.get("feature")
        _add(counts, f"feature:{feature if feature in FEATURES else 'others'}",
             _entry_counts(entry, both))
    for entry in _entries(rec, "totals_by_model_feature"):
        _add(counts, f"model:{model_key(entry.get('model'))}", _entry_counts(entry, both))
    for entry in _entries(rec, "totals_by_ide"):
        _add(counts, f"ide:{ide_key(entry.get('ide'))}",
             _entry_counts(entry, ("user_initiated_interaction_count",)))
    return counts


def _entries(rec: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = rec.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(e, dict) for e in value):
        raise BadRecord(f"bad_type:{key}")
    return value
