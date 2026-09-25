"""GitHub Copilot cloud-agent tasks adapter (addendum §5.7; package CP-ORGDATA).

:class:`AgentTasksAdapter` (registry name ``github-agent-tasks``) reads the task pages CP-PULL
records per repository with a user token (``GET /agents/repos/{owner}/{repo}/tasks[/{task_id}]``)
into one :class:`~tokenbill.core.records.UsageAggregate` (``source_kind="github.agent_tasks"``,
channel ``github_copilot``) per **session**: dims ``model`` (``core.models`` normalization),
``state``, ``artifact`` (the task's artifact types, e.g. ``pull``, ``branch+pull``, ``none``),
``repo`` (``h_`` of ``owner/repo`` from the request path, else of the repository id) and
``team`` (the session user's id through ``opts.team_map``, else ``(unmapped)``);
``reported_cost_nano`` = ``usage.amount`` (nano AI credits) ÷ 100 on basis ``provider_estimate``
(R12: never a billed number). Sessions billed in ``premium_requests`` (before 2026-06-01) are
skipped with ``dq.copilot_legacy_pru``; task ``name``, session ``prompt``, ``name``, branch names
and error messages are never read. A list page without sessions yields nothing
(``stats["tasks_without_sessions"]``).

``GET /agents/tasks`` lists the authenticated user's **own** tasks: files recorded from that
request are refused unless ``opts.identity_mode == "install"`` (the developer's self-view),
with ``dq.copilot_agent_tasks_user_scope``. The number of repositories read is carried as the
coverage note ``dq.copilot_agent_tasks_coverage`` ("tasks from N listed repositories").
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tokenbill.adapters.github_config import (
    UNMAPPED_TEAM,
    BadRecord,
    Item,
    OrgRead,
    decimal_of,
    head_keys,
    head_request_path,
    natural_agg_id,
    parse_ts_ms,
    safe_token,
)
from tokenbill.adapters.github_metrics import model_key
from tokenbill.core.money import nano_aiu_to_nano
from tokenbill.core.records import UsageAggregate, UsageBuckets
from tokenbill.core.types import IngestOptions, IngestResult

__all__ = ["SOURCE_KIND", "STATES", "AgentTasksAdapter"]

SOURCE_KIND = "github.agent_tasks"
#: Documented task / session states (ghec.json, 2026-09-23).
STATES = ("queued", "in_progress", "completed", "failed", "idle", "waiting_for_user",
          "timed_out", "cancelled")
_TERMINAL = frozenset({"completed", "failed", "timed_out", "cancelled"})
_ARTIFACT_TYPES = ("branch", "pull")
_REPO_PATH_RE = re.compile(r"/agents/repos/([^/]+)/([^/]+)/tasks(?:/[^/]+)?\Z")
_USER_PATH_RE = re.compile(r"/agents/tasks(?:/[^/]+)?\Z")


class AgentTasksAdapter:
    """Cloud-agent task pages → one provider-estimate ``UsageAggregate`` per session."""

    name = "github-agent-tasks"
    capabilities = frozenset({"aggregates"})

    def sniff(self, path: Path, head: bytes) -> bool:
        """A recorded ``/agents/…/tasks`` request, or a task page / task with sessions."""
        req = head_request_path(head)
        if req is not None and (_REPO_PATH_RE.search(req) or _USER_PATH_RE.search(req)):
            return True
        keys = head_keys(head)
        if "sessions" in keys and "task_id" in keys:
            return True
        return "tasks" in keys and bool(keys & {"session_count", "creator_type", "artifacts"})

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Parse every task page of *path* (file or directory)."""
        ctx = OrgRead(self.name, path, opts)
        aggs: dict[str, UsageAggregate] = {}
        repos: set[str] = set()
        for item in ctx.items():
            tasks = _tasks_of(item.body)
            if tasks is None:
                ctx.stat("skipped_documents")
                continue
            if _USER_PATH_RE.search(item.path or "") and opts.identity_mode != "install":
                ctx.note("dq.copilot_agent_tasks_user_scope", "warn",
                         "GET /agents/tasks lists the authenticated user's own tasks: self-view "
                         "only, refused in a fleet scan", len(tasks))
                continue
            m = _REPO_PATH_RE.search(item.path or "")
            repo = ctx.name(f"{m.group(1)}/{m.group(2)}") if m else None
            for i, task in enumerate(tasks):
                try:
                    for agg, repo_key in _task_sessions(ctx, item, task, repo):
                        cur = aggs.get(agg.agg_id)
                        if cur is None or agg.fetched_ms > cur.fetched_ms:
                            aggs[agg.agg_id] = agg
                        if repo_key is not None:
                            repos.add(repo_key)
                except BadRecord as exc:
                    ctx.quarantine(f"{item.locator}/tasks[{i}]", exc.reason)
        if repos:
            ctx.note("dq.copilot_agent_tasks_coverage", "info",
                     f"tasks from {len(repos)} listed repositories", len(repos))
        return ctx.result(self.capabilities, aggregates=aggs.values())


def _tasks_of(body: Any) -> list[Any] | None:
    if isinstance(body, dict):
        if isinstance(body.get("tasks"), list):
            return body["tasks"]
        if isinstance(body.get("sessions"), list) or ("id" in body and "state" in body
                                                      and "created_at" in body):
            return [body]
        return None
    if isinstance(body, list) and body and all(isinstance(t, dict) and "state" in t
                                               for t in body):
        return body
    return None


def _id_of(obj: object) -> object:
    return obj.get("id") if isinstance(obj, dict) else None


def _task_sessions(ctx: OrgRead, item: Item, task: object,
                   repo: str | None) -> list[tuple[UsageAggregate, str | None]]:
    if not isinstance(task, dict):
        raise BadRecord("not_object")
    sessions = task.get("sessions")
    if not isinstance(sessions, list):
        ctx.stat("tasks_without_sessions")
        return []
    arts = task.get("artifacts") if isinstance(task.get("artifacts"), list) else []
    types = {a.get("type") for a in arts if isinstance(a, dict)}
    artifact = "+".join(t for t in _ARTIFACT_TYPES if t in types) or "none"
    out = []
    for j, session in enumerate(sessions):
        try:
            got = _session(ctx, item, task, session, artifact, repo)
        except BadRecord as exc:
            ctx.quarantine(f"{item.locator}/sessions[{j}]", exc.reason)
            continue
        if got is not None:
            out.append(got)
    return out


def _session(ctx: OrgRead, item: Item, task: Mapping[str, Any], s: object, artifact: str,
             repo: str | None) -> tuple[UsageAggregate, str | None] | None:
    if not isinstance(s, dict):
        raise BadRecord("not_object")
    session_id = safe_token(s.get("id"))
    if session_id is None:
        raise BadRecord("missing:id")
    if s.get("created_at") is None:
        raise BadRecord("missing:created_at")
    start = parse_ts_ms(s["created_at"], "created_at")
    end_raw = s.get("completed_at") or s.get("updated_at")
    end = max(start, parse_ts_ms(end_raw, "completed_at")) if end_raw else start
    if not ctx.in_window(start):
        ctx.stat("outside_window")
        return None
    usage = s.get("usage")
    cost: int | None = None
    if isinstance(usage, dict):
        if usage.get("type") == "premium_requests":
            ctx.stat("premium_request_sessions")
            ctx.note("dq.copilot_legacy_pru", "info", "cloud-agent sessions billed in legacy "
                     "premium requests skipped")
            return None
        if usage.get("type") == "ai_credits" and usage.get("amount") is not None:
            amount = decimal_of(usage["amount"], "usage.amount")
            if amount < 0 or amount != amount.to_integral_value():
                raise BadRecord("bad_type:usage.amount")
            cost = nano_aiu_to_nano(int(amount))[0]
    state = s.get("state") if s.get("state") in STATES else "unknown"
    if repo is None:
        repo_id = safe_token(_id_of(s.get("repository")) or _id_of(task.get("repository")))
        repo = ctx.name(f"id:{repo_id}") if repo_id is not None else None
    user = _id_of(s.get("user")) or _id_of(task.get("creator"))
    dims = {"channel": "github_copilot", "model": model_key(s.get("model")), "state": state,
            "artifact": artifact, "team": ctx.team(user) or UNMAPPED_TEAM}
    if repo is not None:
        dims["repo"] = repo
    ctx.stat("records")
    agg = UsageAggregate(
        agg_id=natural_agg_id(SOURCE_KIND, session_id), source_kind=SOURCE_KIND,
        bucket_start_ms=start, bucket_end_ms=end, dims=tuple(sorted(dims.items())),
        usage=UsageBuckets(), reported_cost_nano=cost,
        reported_cost_basis="provider_estimate" if cost is not None else None,
        finality="final" if state in _TERMINAL else "provisional", fetched_ms=ctx.fetched(item))
    return agg, repo
