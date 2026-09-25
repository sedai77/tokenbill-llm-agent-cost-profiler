"""Schema-true writers for every GitHub Copilot source (addendum §18, §5; package CP-SYNTH-W).

Each writer serializes canonical records (``core.records``, as the Copilot adapters produce them)
into the exact file shape one Copilot adapter reads, so gate tests can prove that the real adapter
reproduces the records' token and money totals per team:

* :func:`write_ai_usage_csv` — the AI usage report CSV in the documented field order (addendum
  §5.1), one file per convention (``excl``: ``input`` = uncached; ``incl``: ``input`` includes
  cache reads and writes; ``undecidable``: no cache tokens) and, with ``quirks``, GitHub's
  quirks: a BOM, ``M/D/YY`` dates in one file, float-tail money strings
  (``0.4272621300000001``), ``Unknown`` quota, ``Auto:`` and ``(fast mode)`` labels, and two
  overlapping exports with one revised amount; legacy premium-request rows in their own file;
* :func:`write_metered_csv` — the detailed usage CSV (seats, Actions minutes of the Copilot
  dynamic workflows, larger runners and ``.github/workflows/*.lock.yml`` agentic workflows,
  sandboxes) and the summarized CSV (a cross-check);
* :func:`write_billing_pages` — recorded ``usage/summary`` and ``ai_credit/usage`` REST pages
  (CP-PULL envelopes, both documented SKU / unit name variants), derived from the report lines;
* :func:`write_config_pages` — budgets (both SKU field spellings, integer and ``1000.0``
  amounts, user budgets with a user login), budget user-states, cost centers (with and without
  pool state) and the org Copilot billing objects;
* :func:`write_metrics_ndjson` — usage-metrics ``users-1-day``, ``enterprise-1-day``,
  ``organization-1-day``, ``repos-1-day`` and ``user-teams-1-day`` (teams below 5 seated users
  omitted, as GitHub does);
* :func:`write_seats_pages`, :func:`write_agent_task_pages` — seat lists per organization
  (``assigning_team``, editor strings) and cloud-agent task pages per repository;
* :func:`write_copilot_home` — a ``~/.copilot`` tree per CLI user: ``session-state/<id>/
  events.jsonl`` per the SDK schema (``github/copilot-sdk`` ``session-events.ts`` @075f027) with
  the canary in every content field and one concatenated line, plus a ``session-store.db`` from
  the third-party DDL (experimental source);
* :func:`write_otel_file` — a VS Code OTel-JS dump, an OTLP/JSON collector file mixing a Claude
  Code resource with a Copilot resource, or a Copilot CLI envelope file;
  :func:`write_vscode_traces_db` — ``agent-traces.db`` per VS Code developer from the
  ``otelSqliteStore.ts`` DDL, canary rows under content keys and in ``span_events``;
  :func:`write_vscode_outfile` — the VS Code OTel outfile; :func:`write_gh_aw_token_usage` —
  gh-aw ``token-usage.jsonl`` in the ``token-usage/v0.28.7`` shape;
* the UI downloads of the no-token handoff path: :func:`write_activity_report_csv`,
  :func:`write_dashboard_ndjson` (API 28-day shape, Copilot CLI excluded) and
  :func:`write_admin_answers` (CP-HANDOFF's ``tokenbill/copilot-admin-answers@1`` template).

:func:`write_world` writes every source (or, with ``handoff="api"`` / ``"ui"``, only what an admin
records with a token / clicks out of the GitHub UI) plus ``admin/team_map.csv`` (``login,team,
cost_center,user_id``) and ``MANIFEST.json`` (every file with ``provenance: "synthetic"``, its
``schema_source``, role and digest). Synthetic files are gate inputs only and never parser
acceptance evidence (addendum §5 classes ``primary``, ``third-party``, ``real-redacted``).

Records carry pseudonyms (``p_`` principals, ``h_`` repositories and workflows); the writers need
names, so an :class:`Identity` maps every principal to a login (the world's own logins when it
provides them, else ``dev-<hex>`` names with :data:`~tokenbill.core.builders.CANARY_LOGIN` for one
mapped user), a numeric user id, and every ``h_`` to a repository or workflow name. Output bytes
are a pure function of the input (no clock, total sort orders); money never passes through
``float`` (JSON decimals are spliced in as exact literals).
"""

from __future__ import annotations

import csv
import datetime as _dt
import hashlib
import io
import json
import re
import sqlite3
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from tokenbill.core import facts as _facts
from tokenbill.core.builders import CANARY, CANARY_EMAIL, CANARY_LOGIN
from tokenbill.core.errors import UsageError
from tokenbill.core.ids import pseudonym
from tokenbill.core.jsonl import write_jsonl
from tokenbill.core.models import normalize_copilot_model
from tokenbill.core.money import nano_to_credits_str, nano_to_usd_str
from tokenbill.core.records import (
    ActivityDay,
    ConfigSnapshot,
    CostLine,
    Inference,
    InferenceKind,
    Lane,
    LaneEvent,
    LaneEventKind,
    LicenseSnapshot,
    OutcomeAggregate,
    Request,
    Session,
    UsageAggregate,
    UsageBuckets,
)
from tokenbill.core.types import IngestResult

__all__ = [
    "CONTENT_KEYS",
    "HANDOFF_MODES",
    "MANIFEST_SCHEMA",
    "ROLES",
    "Identity",
    "build_identity",
    "model_label",
    "raw_session_id",
    "read_team_map",
    "repo_name",
    "revision_info",
    "workflow_file",
    "write_activity_report_csv",
    "write_admin_answers",
    "write_agent_task_pages",
    "write_ai_usage_csv",
    "write_billing_pages",
    "write_config_pages",
    "write_copilot_home",
    "write_dashboard_ndjson",
    "write_gh_aw_token_usage",
    "write_metered_csv",
    "write_metrics_ndjson",
    "write_otel_file",
    "write_seats_pages",
    "write_vscode_outfile",
    "write_vscode_traces_db",
    "write_world",
]

#: Schema of ``MANIFEST.json``.
MANIFEST_SCHEMA = "tokenbill/synth-copilot-files@1"
#: ``write_world(handoff=…)`` values besides None (every source).
HANDOFF_MODES = ("api", "ui")
#: File roles in the manifest: ``primary`` files carry the world exactly once (read them all);
#: ``variant`` files are the same report under another convention; ``revision_pair`` files are
#: two overlapping exports (one revised amount); ``cross_check`` files restate primary data in
#: another grain (summarized CSV, billing REST pages); ``view`` files carry the same requests as a
#: primary file in another dialect (OTel files next to CLI events / VS Code databases);
#: ``overlap`` files are the CLI sessions of VS Code conversations (dedupe inputs);
#: ``admin_input`` files are written by the admin (team map, answers).
ROLES = ("primary", "variant", "revision_pair", "cross_check", "view", "overlap", "admin_input")
#: Keys (JSON object keys, SQLite attribute keys, CSV columns) whose values are content the
#: adapters must never read; the canary is planted only under them.
CONTENT_KEYS = frozenset({
    "content", "transformedContent", "reasoningText", "summaryContent", "arguments", "command",
    "result", "cwd", "branch", "message", "filesModified", "detailedContent", "name", "prompt",
    "email", "summary", "gen_ai.input.messages", "gen_ai.output.messages",
    "gen_ai.system_instructions", "gen_ai.tool.definitions", "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result", "github.copilot.tool.parameters.command",
    "copilot_chat.hook_input", "copilot_chat.hook_output", "copilot_chat.user_request",
    "span_events.attributes",
})

_DAY_MS = 86_400_000
_EPOCH = _dt.date(1970, 1, 1)
_ENTERPRISE = "synth-enterprise"
_ENTERPRISE_ID = 4242
_COPILOT_VERSION = "1.0.64"
_DEC_RE = re.compile(r'"@@dec:(-?[0-9]+(?:\.[0-9]+)?)@@"')
_LOGIN_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,38}\Z")

# ---------------------------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------------------------


def _day_ms(day: str) -> int:
    return (_dt.date.fromisoformat(day) - _EPOCH).days * _DAY_MS


def _day_of(ms: int) -> str:
    return (_EPOCH + _dt.timedelta(days=ms // _DAY_MS)).isoformat()


def _shift(day: str, days: int) -> str:
    return (_dt.date.fromisoformat(day) + _dt.timedelta(days=days)).isoformat()


def _iso(ms: int, *, millis: bool = False) -> str:
    moment = _dt.datetime(1970, 1, 1) + _dt.timedelta(milliseconds=ms)
    if millis:
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms % 1000:03d}Z"
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _hr(ms: int) -> list[int]:
    """OTel-JS ``HrTime`` ``[seconds, nanoseconds]`` of epoch milliseconds."""
    return [ms // 1000, (ms % 1000) * 1_000_000]


def _hex(seed: str, n: int) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:n]


def _uuid(seed: str) -> str:
    h = _hex(seed, 32)
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-8{h[17:20]}-{h[20:32]}"


def _dec(text: str) -> str:
    """A JSON number placeholder spliced in as an exact decimal literal by :func:`_dumps`."""
    return f"@@dec:{text}@@"


def _dumps(obj: Any) -> str:
    """Compact JSON (insertion key order) with exact decimal literals for :func:`_dec` values."""
    return _DEC_RE.sub(r"\1", json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def _put(path: Path, text: str, *, bom: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + data)
    return path


def _put_lines(path: Path, docs: Iterable[Any]) -> Path:
    return _put(path, "".join(_dumps(d) + "\n" for d in docs))


def _csv_text(header: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    return buf.getvalue()


def _rel(out_dir: Path, path: Path) -> str:
    return path.relative_to(out_dir).as_posix()


def _allocate(total: int, weights: Sequence[int]) -> list[int]:
    """*total* split in proportion to *weights* (largest remainder; ties by position)."""
    n = len(weights)
    if n == 0:
        return []
    w = list(weights) if sum(weights) > 0 else [1] * n
    s = sum(w)
    base = [total * x // s for x in w]
    order = sorted(range(n), key=lambda i: (-(total * w[i] % s), i))
    for i in order[: total - sum(base)]:
        base[i] += 1
    return base


def _float_tail(text: str) -> str:
    """A float-rendering tail GitHub's exports show (``0.42726213`` → ``0.4272621300000001``):
    +1e-16 USD, which rounds away at nano precision."""
    if "." not in text or text.startswith("-"):
        return text
    whole, frac = text.split(".", 1)
    return f"{whole}.{frac.ljust(15, '0')}1"


def _mdyy(day: str) -> str:
    d = _dt.date.fromisoformat(day)
    return f"{d.month}/{d.day}/{d.year % 100:02d}"


# ---------------------------------------------------------------------------------------------
# model labels
# ---------------------------------------------------------------------------------------------

_PSEUDO_LABELS = {"code_review": "Code Review", "cloud_agent": "Coding Agent",
                  "auto_unattributed": "Auto", "unknown": "Unknown"}


def _aliases() -> dict[str, str]:
    out: dict[str, str] = {}
    for row in _facts.copilot_rates():
        for alias in row.aliases:
            if normalize_copilot_model(alias).model == row.model:
                out.setdefault(row.model, alias)
    return out


_ALIASES = _aliases()


def _display(model: str) -> str:
    """The display name of a normalized Copilot model id (facts alias; else the id itself, which
    normalizes to itself)."""
    return _ALIASES.get(model, model)


def model_label(model: str | None, routing: str | None = None, speed: str | None = None,
                pseudo: str | None = None) -> str:
    """The report label of a normalized model: ``Auto: Claude Haiku 4.5``, ``Claude Opus 4.8
    (fast mode)``, ``Code Review`` …; :func:`core.models.normalize_copilot_model` of the label
    gives back *model*, *routing*, *speed* and *pseudo*."""
    if pseudo:
        label = _PSEUDO_LABELS.get(pseudo, "Unknown")
        if routing == "auto" and pseudo != "auto_unattributed":
            label = f"Auto: {label}"
        return label
    if not model:
        return "Unknown"
    label = _display(model)
    if speed == "fast":
        label = f"{label} (fast mode)"
    if routing == "auto":
        label = f"Auto: {label}"
    return label


# ---------------------------------------------------------------------------------------------
# input records
# ---------------------------------------------------------------------------------------------

_KINDS = ("cost_lines", "aggregates", "licenses", "activity", "config", "outcomes", "requests",
          "sessions", "events", "lanes")


@dataclass
class _Recs:
    """Every canonical record of the input, by kind (deterministically sorted)."""

    cost_lines: list[CostLine] = field(default_factory=list)
    aggregates: list[UsageAggregate] = field(default_factory=list)
    licenses: list[LicenseSnapshot] = field(default_factory=list)
    activity: list[ActivityDay] = field(default_factory=list)
    config: list[ConfigSnapshot] = field(default_factory=list)
    outcomes: list[OutcomeAggregate] = field(default_factory=list)
    requests: list[Request] = field(default_factory=list)
    sessions: list[Session] = field(default_factory=list)
    events: list[LaneEvent] = field(default_factory=list)
    lanes: dict[str, Lane] = field(default_factory=dict)
    world: Any = None

    def finish(self) -> _Recs:
        seen: set[str] = set()
        reqs = []
        for r in self.requests:
            if r.request_id not in seen:
                seen.add(r.request_id)
                reqs.append(r)
        self.requests = sorted(reqs, key=lambda r: (r.session_key, r.lane_key, r.ts_start_ms,
                                                    r.seq, r.request_id))
        ev_seen: set[str] = set()
        evs = []
        for e in self.events:
            k = repr((e.lane_key, e.ts_ms, e.kind.value, e.attrs))
            if k not in ev_seen:
                ev_seen.add(k)
                evs.append(e)
        self.events = sorted(evs, key=lambda e: (e.lane_key, e.ts_ms, e.kind.value, repr(e.attrs)))
        self.cost_lines.sort(key=lambda c: (c.date_utc, c.source_kind, c.line_id))
        self.aggregates.sort(key=lambda a: (a.bucket_start_ms, a.source_kind, a.agg_id))
        self.licenses.sort(key=lambda x: (x.snapshot_date, x.principal, x.org or "",
                                          x.source_kind))
        self.activity.sort(key=lambda x: (x.date_utc, x.principal, x.source_kind))
        self.config.sort(key=lambda x: (x.kind, x.entity_id, x.snapshot_ms, repr(x.attrs)))
        self.outcomes.sort(key=lambda x: (x.date_utc, x.source_kind, x.team))
        self.sessions.sort(key=lambda s: s.session_key)
        return self


def _absorb(recs: _Recs, obj: Any, depth: int = 0) -> None:
    if obj is None:
        return
    if depth > 6:
        raise UsageError("copilot writers: records nested too deeply")
    simple: tuple[tuple[type, list[Any]], ...] = (
        (CostLine, recs.cost_lines), (UsageAggregate, recs.aggregates),
        (LicenseSnapshot, recs.licenses), (ActivityDay, recs.activity),
        (ConfigSnapshot, recs.config), (OutcomeAggregate, recs.outcomes),
        (Request, recs.requests), (LaneEvent, recs.events))
    for cls, bucket in simple:
        if isinstance(obj, cls):
            bucket.append(obj)
            return
    if isinstance(obj, Session):
        recs.sessions.append(obj)
        for lane in obj.lanes:
            _absorb(recs, lane, depth + 1)
        return
    if isinstance(obj, Lane):
        recs.lanes.setdefault(obj.lane_key, obj)
        recs.requests.extend(obj.requests)
        recs.events.extend(obj.events)
        return
    if isinstance(obj, IngestResult):
        for name in ("requests", "sessions", "events", "aggregates", "cost_lines", "outcomes",
                     "licenses", "activity", "config"):
            for item in getattr(obj, name):
                _absorb(recs, item, depth + 1)
        return
    if isinstance(obj, (str, bytes)):
        raise UsageError("copilot writers: records must be canonical records, not text")
    if isinstance(obj, Mapping):
        if "records" in obj and not any(k in obj for k in _KINDS):
            if recs.world is None:
                recs.world = obj
            _absorb(recs, obj["records"], depth + 1)
            return
        for key in _KINDS:
            values = obj.get(key)
            if values is not None:
                _absorb(recs, list(values.values()) if isinstance(values, Mapping) else values,
                        depth + 1)
        return
    if not isinstance(obj, (list, tuple, set, frozenset)) and hasattr(obj, "records"):
        if recs.world is None:
            recs.world = obj
        _absorb(recs, obj.records, depth + 1)
        return
    if not isinstance(obj, (list, tuple, set, frozenset)) and any(
            hasattr(obj, k) for k in _KINDS):
        if recs.world is None:
            recs.world = obj
        for key in _KINDS:
            values = getattr(obj, key, None)
            if values is not None and not callable(values):
                _absorb(recs, list(values.values()) if isinstance(values, Mapping) else values,
                        depth + 1)
        return
    if isinstance(obj, Iterable):
        for item in obj:
            _absorb(recs, item, depth + 1)
        return
    raise UsageError("copilot writers: unsupported records container")


def _collect(records: Any) -> _Recs:
    """Normalize the input: a ``CopilotWorld`` (its ``records``), a mapping or object with
    record-kind attributes (``cost_lines``, ``licenses``, …), an ``IngestResult``, or any iterable
    of canonical records (sessions and lanes are opened)."""
    if isinstance(records, _Recs):
        return records
    recs = _Recs()
    _absorb(recs, records)
    return recs.finish()


def _world_attr(recs: _Recs, *names: str) -> Any:
    for obj in (recs.world, getattr(recs.world, "records", None)):
        if obj is None:
            continue
        for name in names:
            value = obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)
            if value is not None and not callable(value):
                return value
    return None


# ---------------------------------------------------------------------------------------------
# identity: principals → logins, user ids; h_ → repository and workflow names
# ---------------------------------------------------------------------------------------------


def _derived_login(principal: str) -> str:
    tail = principal[2:10] if principal.startswith(("p_", "c_")) else _hex(principal, 8)
    return f"dev-{tail}"


@dataclass(frozen=True)
class Identity:
    """The names the files need for pseudonymous records (built by :func:`build_identity`)."""

    logins: Mapping[str, str]
    teams: Mapping[str, str]
    cost_centers: Mapping[str, str]
    user_ids: Mapping[str, int]
    enterprise: str = _ENTERPRISE
    enterprise_id: int = _ENTERPRISE_ID

    def login(self, principal: str | None) -> str:
        """The login of *principal* (``""`` for None; a derived ``dev-<hex>`` when unknown)."""
        if not principal:
            return ""
        return self.logins.get(principal) or _derived_login(principal)

    def user_id(self, principal: str | None) -> int:
        """The numeric GitHub user id of *principal* (derived when unknown)."""
        if not principal:
            return 0
        known = self.user_ids.get(principal)
        return known if known is not None else 50_000_000 + int(_hex(principal, 7), 16) % 10**7

    def members(self, team: str | None) -> list[str]:
        """Principals of *team*, sorted by login."""
        return sorted((p for p, t in self.teams.items() if t == team), key=self.login)

    def team_map(self) -> dict[str, str]:
        """login → team (what the admin's ``--team-map-csv`` holds)."""
        return {self.login(p): t for p, t in sorted(self.teams.items(), key=lambda x: x[0])}

    def cost_center_map(self) -> dict[str, str]:
        """login → cost center."""
        return {self.login(p): c for p, c in sorted(self.cost_centers.items())}

    def id_team_map(self) -> dict[str, str]:
        """str(user id) → team (cloud-agent sessions name users by id only)."""
        return {str(self.user_id(p)): t for p, t in sorted(self.teams.items())}


def repo_name(repo: str | None, org: str | None = None) -> str:
    """A repository ``owner/name`` for an ``h_`` repository pseudonym (deterministic)."""
    if not repo:
        return ""
    tail = repo[2:10] if repo.startswith("h_") else _hex(repo, 8)
    return f"{org or 'synth-org'}/service-{tail}"


def workflow_file(workflow: str | None) -> str:
    """An agentic-workflow path ``.github/workflows/<name>.lock.yml`` for an ``h_`` pseudonym."""
    tail = workflow[2:10] if workflow and workflow.startswith("h_") else _hex(workflow or "", 8)
    return f".github/workflows/triage-{tail}.lock.yml"


def _principal_attrs(recs: _Recs) -> tuple[dict[str, str], dict[str, str], set[str]]:
    teams: dict[str, str] = {}
    ccs: dict[str, str] = {}
    principals: set[str] = set()

    def take(principal: str | None, team: str | None, cc: str | None) -> None:
        if not principal:
            return
        principals.add(principal)
        if team and principal not in teams:
            teams[principal] = team
        if cc and principal not in ccs:
            ccs[principal] = cc

    for lic in sorted(recs.licenses, key=lambda x: (x.source_kind != "github.copilot_seats",
                                                     x.snapshot_date, x.principal)):
        take(lic.principal, lic.team, lic.cost_center)
    for act in recs.activity:
        take(act.principal, act.team, act.cost_center)
    for line in recs.cost_lines:
        take(line.principal, line.team, line.cost_center)
    for req in recs.requests:
        a = req.attribution
        take(a.principal, a.team, a.cost_center)
    return teams, ccs, principals


def _world_logins(recs: _Recs, principals: set[str]) -> dict[str, str]:
    """principal → login from the world, when it provides them: a mapping attribute (``logins``,
    ``login_of``, ``principal_logins``), or a login → team map plus the key the world's principals
    were derived with (``principal_key`` / ``org_key`` / ``key``)."""
    for name in ("logins", "login_of", "principal_logins", "logins_by_principal"):
        value = _world_attr(recs, name)
        if isinstance(value, Mapping) and value:
            items = {str(k): str(v) for k, v in value.items()}
            if any(k in principals for k in items):
                return items
            flipped = {v: k for k, v in items.items()}
            if any(k in principals for k in flipped):
                return flipped
    logins: list[str] = []
    for name in ("team_map", "login_teams", "teams_by_login"):
        value = _world_attr(recs, name)
        if isinstance(value, Mapping):
            logins.extend(str(k) for k in value)
    if not logins:
        return {}
    for name in ("principal_key", "org_key", "export_key", "key"):
        key = _world_attr(recs, name)
        if isinstance(key, (bytes, bytearray)) and key:
            out = {}
            for login in logins:
                for form in (login.strip().lower(), login.strip()):
                    p = pseudonym(bytes(key), "p", form)
                    if p in principals:
                        out.setdefault(p, login)
            if out:
                return out
    return {}


def build_identity(records: Any, *, logins: Mapping[str, str] | None = None,
                   enterprise: str = _ENTERPRISE) -> Identity:
    """Logins, user ids, teams and cost centers of every principal in *records*.

    *logins* (principal → login) wins; else the world's own logins (see :func:`write_world`);
    else ``dev-<hex>`` names, and :data:`CANARY_LOGIN` for the first principal (sorted) that has a
    team, so the canary login is mapped by the team map. Logins are lower-case (the org-data
    adapters lower-case logins before pseudonymizing; the billing and activity-report adapters do
    not, so lower-case logins keep one ``p_`` per person across every source)."""
    recs = _collect(records)
    teams, ccs, principals = _principal_attrs(recs)
    given = dict(logins) if logins is not None else _world_logins(recs, principals)
    out: dict[str, str] = {}
    used: set[str] = set()
    for p in sorted(principals):
        name = given.get(p)
        if name is None:
            continue
        out[p] = name
        used.add(name.lower())
    if not given:
        mapped = sorted(p for p in principals if p in teams)
        if mapped:
            out[mapped[0]] = CANARY_LOGIN
            used.add(CANARY_LOGIN)
    for p in sorted(principals):
        if p in out:
            continue
        name = _derived_login(p)
        n = 1
        while name in used:
            n += 1
            name = f"{_derived_login(p)}-{n}"
        out[p] = name
        used.add(name)
    ids: dict[str, int] = {}
    taken: set[int] = set()
    for p in sorted(principals):
        uid = 50_000_000 + int(_hex(p, 7), 16) % 10**7
        while uid in taken:
            uid += 1
        taken.add(uid)
        ids[p] = uid
    return Identity(logins=out, teams=teams, cost_centers=ccs, user_ids=ids,
                    enterprise=enterprise)


def read_team_map(path: Path) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """``(team_map, cost_center_map, id_team_map)`` from ``admin/team_map.csv`` (header
    ``login,team,cost_center,user_id``): login → team, login → cost center, user id → team."""
    team: dict[str, str] = {}
    cc: dict[str, str] = {}
    ids: dict[str, str] = {}
    reader = csv.DictReader(io.StringIO(Path(path).read_text(encoding="utf-8-sig")))
    for row in reader:
        login = (row.get("login") or "").strip()
        if not login:
            continue
        if row.get("team"):
            team[login] = row["team"]
            if row.get("user_id"):
                ids[row["user_id"]] = row["team"]
        if row.get("cost_center"):
            cc[login] = row["cost_center"]
    return team, cc, ids


def _write_team_map(ident: Identity, out_dir: Path) -> dict[str, Path]:
    rows = []
    for p in sorted(ident.logins, key=ident.login):
        rows.append([ident.login(p), ident.teams.get(p, ""), ident.cost_centers.get(p, ""),
                     str(ident.user_id(p))])
    path = _put(out_dir / "admin" / "team_map.csv",
                _csv_text(("login", "team", "cost_center", "user_id"), rows))
    return {_rel(out_dir, path): path}


def _ident(recs: _Recs, identity: Identity | None) -> Identity:
    return identity if identity is not None else build_identity(recs)


# ---------------------------------------------------------------------------------------------
# AI usage report CSV (addendum §5.1)
# ---------------------------------------------------------------------------------------------

AI_USAGE_SOURCE = "github.ai_usage_report"
METERED_SOURCE = "github.metered_usage"
#: The documented AI usage report field order (billing-reports reference, 2026-09-23).
AI_USAGE_HEADER = ("date", "product", "sku", "quantity", "unit_type", "applied_cost_per_quantity",
                   "gross_amount", "discount_amount", "net_amount", "username", "organization",
                   "repository", "cost_center_name", "model", "input", "output", "cache_read",
                   "cache_write")
#: The legacy ``premium_request`` report: no token columns, ``exceeds_quota``.
LEGACY_HEADER = (*AI_USAGE_HEADER[:14], "exceeds_quota")
#: Conventions a report file can be rendered under.
CONVENTIONS = ("excl", "incl", "undecidable")


@dataclass(frozen=True)
class _AiRow:
    date: str
    product: str
    sku: str
    quantity: str
    unit: str
    applied: str
    gross: int
    net: int
    login: str
    org: str
    repo: str
    cc: str
    label: str
    tokens: tuple[int, int, int, int] | None   # uncached, cache read, cache write, output
    quota: str
    legacy: bool

    def key(self) -> tuple:
        return (self.date, self.login, self.label, self.sku, self.org, self.cc, self.repo,
                self.gross, self.net, self.tokens or ())


def _line_dims(line: CostLine) -> tuple[tuple[str, str], ...]:
    """The token-aggregate dims the ``github-ai-usage`` adapter gives this line's row."""
    dims = {"channel": line.channel, "organization": line.workspace_id, "team": line.team,
            "cost_center": line.cost_center, "model": line.model, "sku": line.sku,
            "routing": line.routing, "speed": line.speed, "pseudo": line.pseudo}
    return tuple(sorted((k, v) for k, v in dims.items() if v is not None))


def _sku_product(sku: str | None, default: str = "copilot") -> str:
    fact = _facts.copilot_skus().get(sku or "")
    return fact.product if fact is not None else default


def _quota_fn(recs: _Recs, quirks: bool) -> Callable[[str | None, str, str], str] | None:
    """``total_monthly_quota`` per (principal, org, month): the plan's quota (facts) when the
    records carry plan-quota evidence, ``Unknown`` otherwise (with quirks); None = no column."""
    if not any(c.kind == "plan_quota" for c in recs.config):
        if not quirks:
            return None
        return lambda p, org, month: "Unknown" if p else ""
    plans: dict[tuple[str, str], str] = {}
    for lic in recs.licenses:
        if lic.plan != "unknown":
            plans.setdefault((lic.principal, lic.org or ""), lic.plan)
            plans.setdefault((lic.principal, "*"), lic.plan)
    for line in recs.cost_lines:
        fact = _facts.copilot_skus().get(line.sku or "")
        if line.cost_type == "seat" and line.principal and fact is not None and fact.plan:
            plans.setdefault((line.principal, line.workspace_id or ""), fact.plan)
            plans.setdefault((line.principal, "*"), fact.plan)
    quotas = _facts.copilot_plan_quota_map()

    def quota(principal: str | None, org: str, month: str) -> str:
        if not principal:
            return ""
        plan = plans.get((principal, org)) or plans.get((principal, "*"))
        if plan is None:
            return "Unknown"
        promo = [q for q, f in sorted(quotas.items()) if f.plan == plan and month in f.months]
        base = [q for q, f in sorted(quotas.items()) if f.plan == plan and not f.months]
        pick = promo or base
        return str(pick[0]) if pick else "Unknown"

    return quota


def _ai_rows(recs: _Recs, ident: Identity, quota: Callable[[str | None, str, str], str] | None
             ) -> list[_AiRow]:
    lines = [c for c in recs.cost_lines if c.source_kind == AI_USAGE_SOURCE]
    usage: dict[tuple[str, tuple], list[int]] = {}
    for agg in recs.aggregates:
        if agg.source_kind != AI_USAGE_SOURCE:
            continue
        u = agg.usage
        cell = usage.setdefault((_day_of(agg.bucket_start_ms), agg.dims), [0, 0, 0, 0])
        for i, v in enumerate((u.uncached_input, u.cache_read, u.cache_write, u.output)):
            cell[i] += v
    by_key: dict[tuple[str, tuple], list[int]] = defaultdict(list)
    for i, line in enumerate(lines):
        if line.cost_type != "ai_credit.legacy_pru":
            by_key[(line.date_utc, _line_dims(line))].append(i)
    tokens: list[tuple[int, int, int, int] | None] = [None] * len(lines)
    rows: list[_AiRow] = []
    for key in sorted(usage):
        cell = usage[key]
        idx = by_key.get(key)
        if idx:
            weights = [max(lines[i].list_amount_nano if lines[i].list_amount_nano is not None
                           else lines[i].amount_nano, 0) for i in idx]
            parts = [_allocate(v, weights) for v in cell]
            for j, i in enumerate(idx):
                tokens[i] = (parts[0][j], parts[1][j], parts[2][j], parts[3][j])
            continue
        rows.append(_orphan_row(key, cell, ident, quota))   # tokens without a money line
    for i, line in enumerate(lines):
        legacy = line.cost_type == "ai_credit.legacy_pru"
        gross = line.list_amount_nano if line.list_amount_nano is not None else line.amount_nano
        org = line.workspace_id or ""
        rows.append(_AiRow(
            date=line.date_utc, product=_sku_product(line.sku),
            sku=line.sku or "copilot_ai_credit",
            quantity=line.quantity if line.quantity is not None else nano_to_credits_str(gross),
            unit=line.unit or ("requests" if legacy else "ai-credits"),
            applied="0.04" if legacy else "0.01", gross=gross, net=line.amount_nano,
            login=ident.login(line.principal), org=org, repo=repo_name(line.repo, org),
            cc=line.cost_center or "",
            label=model_label(line.model, line.routing, line.speed, line.pseudo),
            tokens=tokens[i], quota=quota(line.principal, org, line.date_utc[:7]) if quota else "",
            legacy=legacy))
    rows.sort(key=_AiRow.key)
    return rows


def _orphan_row(key: tuple[str, tuple], cell: list[int], ident: Identity,
                quota: Callable[[str | None, str, str], str] | None) -> _AiRow:
    date, dims = key
    d = dict(dims)
    members = ident.members(d.get("team"))
    principal = members[0] if members else None
    org = d.get("organization", "")
    return _AiRow(date=date, product=_sku_product(d.get("sku")),
                  sku=d.get("sku", "copilot_ai_credit"), quantity="0", unit="ai-credits",
                  applied="0.01", gross=0, net=0, login=ident.login(principal), org=org, repo="",
                  cc=d.get("cost_center", ""),
                  label=model_label(d.get("model"), d.get("routing"), d.get("speed"),
                                    d.get("pseudo")),
                  tokens=(cell[0], cell[1], cell[2], cell[3]),
                  quota=quota(principal, org, date[:7]) if quota else "", legacy=False)


def _token_cells(tokens: tuple[int, int, int, int] | None, convention: str) -> list[str]:
    if tokens is None:
        return ["", "", "", ""]
    u, r, w, o = tokens
    if convention == "incl":
        return [str(u + r + w), str(o), str(r), str(w)]
    if convention == "undecidable":
        return [str(u), str(o), "0", "0"]
    return [str(u), str(o), str(r), str(w)]


def _render_ai(rows: Sequence[_AiRow], *, convention: str, quirks: bool, mdyy: bool,
               quota: bool, legacy: bool = False) -> str:
    header = list(LEGACY_HEADER if legacy else AI_USAGE_HEADER)
    if quota and not legacy:
        header.append("total_monthly_quota")
    out = []
    for i, row in enumerate(rows):
        gross, net = nano_to_usd_str(row.gross), nano_to_usd_str(row.net)
        if quirks and i % 4 == 1:
            gross, net = _float_tail(gross), _float_tail(net)
        cells = [_mdyy(row.date) if mdyy else row.date, row.product, row.sku, row.quantity,
                 row.unit, row.applied, gross, nano_to_usd_str(row.gross - row.net), net,
                 row.login, row.org, row.repo, row.cc, row.label]
        if legacy:
            cells.append("False")
        else:
            cells.extend(_token_cells(row.tokens, convention))
            if quota:
                cells.append(row.quota)
        out.append(cells)
    return _csv_text(header, out)


def _revision_pair(rows: Sequence[_AiRow]) -> tuple[list[_AiRow], list[_AiRow], dict] | None:
    """Two exports overlapping by 3 days; the earlier one shows a stale amount for one row of the
    overlap (+1 credit), the later one the revised (true) amount."""
    dates = sorted({r.date for r in rows})
    if len(dates) < 4:
        return None
    mid = len(dates) // 2
    overlap = set(dates[mid - 1: mid + 2])
    first = [r for r in rows if r.date <= dates[mid + 1]]
    second = [r for r in rows if r.date >= dates[mid - 1]]
    for i, r in enumerate(first):
        if r.date in overlap and r.gross > 0 and not r.legacy:
            stale = _AiRow(**{**r.__dict__, "gross": r.gross + 10_000_000,
                              "net": r.net + 10_000_000,
                              "quantity": str(Decimal(r.quantity) + 1)})
            first[i] = stale
            info = {"date": r.date, "stale_net_nano": stale.net, "revised_net_nano": r.net,
                    "overlap": sorted(overlap)}
            return first, second, info
    return None


def write_ai_usage_csv(records: Any, out_dir: Path, *, conventions: Sequence[str] = ("excl",),
                       quirks: bool = True, identity: Identity | None = None,
                       revision_pair: bool | None = None, mdyy_file: str | None = None,
                       report_sets: Mapping[str, Any] | None = None) -> dict[str, Path]:
    """The AI usage report as ``billing/ai_usage_report_<convention>.csv`` per convention
    (``excl`` | ``incl`` | ``undecidable``), ``billing/ai_usage_legacy_pru.csv`` for legacy
    premium-request lines and, with *revision_pair* (default: *quirks*), the overlapping exports
    ``billing/ai_usage_overlap_{a,b}.csv``. Rows are the ``github.ai_usage_report`` cost lines;
    tokens are the report's token aggregates split over the lines of their dims in proportion to
    gross. *report_sets* (convention → records) renders each convention from its own record set.
    ``M/D/YY`` dates go to *mdyy_file* (default: the first overlap file, else the last
    convention file) when *quirks*."""
    recs = _collect(records)
    ident = _ident(recs, identity)
    for conv in conventions:
        if conv not in CONVENTIONS:
            raise UsageError(f"write_ai_usage_csv: unknown convention {conv!r}")
    out_dir = Path(out_dir)
    base = out_dir / "billing"
    pair_on = quirks if revision_pair is None else revision_pair
    names = [f"ai_usage_report_{c}.csv" for c in conventions]
    rendered: dict[str, tuple[list[_AiRow], str, bool]] = {}
    for conv, name in zip(conventions, names, strict=True):
        src = _collect(report_sets[conv]) if report_sets and conv in report_sets else recs
        quota = _quota_fn(src, quirks)
        rows = _ai_rows(src, ident, quota)
        rendered[name] = ([r for r in rows if not r.legacy], conv, quota is not None)
    main_rows = rendered[names[0]][0] if names else []
    pair = _revision_pair(main_rows) if pair_on and names else None
    if quirks and mdyy_file is None:
        mdyy_file = "ai_usage_overlap_a.csv" if pair else (names[-1] if names else None)
    written: dict[str, Path] = {}
    for name, (rows, conv, has_quota) in rendered.items():
        path = _put(base / name, _render_ai(rows, convention=conv, quirks=quirks,
                                            mdyy=name == mdyy_file, quota=has_quota), bom=quirks)
        written[_rel(out_dir, path)] = path
    legacy_rows = [r for r in _ai_rows(recs, ident, None) if r.legacy]
    if legacy_rows:
        path = _put(base / "ai_usage_legacy_pru.csv",
                    _render_ai(legacy_rows, convention="excl", quirks=False, mdyy=False,
                               quota=False, legacy=True), bom=quirks)
        written[_rel(out_dir, path)] = path
    if pair is not None:
        first, second, _info = pair
        has_quota = rendered[names[0]][2]
        for tag, rows in (("a", first), ("b", second)):
            name = f"ai_usage_overlap_{tag}.csv"
            path = _put(base / name, _render_ai(rows, convention=conventions[0], quirks=quirks,
                                                mdyy=name == mdyy_file, quota=has_quota),
                        bom=quirks)
            written[_rel(out_dir, path)] = path
    return written


def revision_info(records: Any, *, identity: Identity | None = None) -> dict[str, Any] | None:
    """Which row the overlapping exports revise (date, stale and revised net nano, overlap days),
    or None when the report spans fewer than 4 days."""
    recs = _collect(records)
    rows = [r for r in _ai_rows(recs, _ident(recs, identity), None) if not r.legacy]
    pair = _revision_pair(rows)
    return pair[2] if pair is not None else None


# ---------------------------------------------------------------------------------------------
# detailed and summarized usage CSVs (addendum §5.2)
# ---------------------------------------------------------------------------------------------

#: Detailed usage report columns (billing-reports reference).
DETAILED_HEADER = ("date", "product", "sku", "quantity", "unit_type", "applied_cost_per_quantity",
                   "gross_amount", "discount_amount", "net_amount", "username", "organization",
                   "repository", "workflow_path", "cost_center_name")
#: Summarized usage report columns (no username, no workflow path).
SUMMARIZED_HEADER = ("date", "product", "sku", "quantity", "unit_type",
                     "applied_cost_per_quantity", "gross_amount", "discount_amount", "net_amount",
                     "organization", "repository", "cost_center_name")
#: Copilot dynamic workflow paths per workload (facts ``copilot.workflow_paths``).
DYNAMIC_PATHS: Mapping[str, tuple[str, ...]] = {
    "copilot_cloud_agent": ("dynamic/copilot-swe-agent/copilot",),
    "copilot_code_review": ("dynamic/agents/copilot-pull-request-reviewer",
                            "dynamic/copilot-pull-request-reviewer/copilot-pull-request-reviewer"),
    "code_quality": ("dynamic/github-code-quality/codeql",),
}
#: The user workflow row added with quirks (the adapter counts and drops it).
USER_WORKFLOW_PATH = ".github/workflows/ci.yml"
_DEFAULT_UNITS = {"seat": "seat-months", "actions": "minutes", "sandbox": "hours",
                  "code_quality.license": "licenses"}


def _metered_product(line: CostLine) -> str:
    if line.channel == "github_actions":
        return "actions"
    if line.channel == "github_sandbox":
        return "sandbox"
    product = _sku_product(line.sku)
    return product if product in ("copilot", "spark", "code_quality") else "copilot"


def _applied(gross: int, quantity: str) -> str:
    try:
        qty = Decimal(quantity)
    except ArithmeticError:
        return "0"
    if not qty:
        return "0"
    value = Decimal(nano_to_usd_str(gross)) / qty
    text = format(value.quantize(Decimal("1e-12")).normalize(), "f")
    return "0" if text in ("-0", "0E-12") else text


def _metered_rows(recs: _Recs, ident: Identity, quirks: bool) -> list[list[str]]:
    rows: list[tuple[str, ...]] = []
    review = 0
    for line in recs.cost_lines:
        if line.source_kind != METERED_SOURCE:
            continue
        gross = line.list_amount_nano if line.list_amount_nano is not None else line.amount_nano
        path = ""
        if line.channel == "github_actions":
            if line.workload == "agentic_workflow":
                path = workflow_file(line.workflow)
            elif line.workload in DYNAMIC_PATHS:
                choices = DYNAMIC_PATHS[line.workload]
                path = choices[review % len(choices)]
                if line.workload == "copilot_code_review":
                    review += 1
        qty = line.quantity if line.quantity is not None else "1"
        org = line.workspace_id or ""
        rows.append((line.date_utc, _metered_product(line), line.sku or "", qty,
                     line.unit or _DEFAULT_UNITS.get(line.cost_type or "", "ai-credits"),
                     _applied(gross, qty), nano_to_usd_str(gross),
                     nano_to_usd_str(gross - line.amount_nano), nano_to_usd_str(line.amount_nano),
                     ident.login(line.principal), org, repo_name(line.repo, org), path,
                     line.cost_center or ""))
    actions = [r for r in rows if r[1] == "actions"]
    if quirks and actions:
        first = min(actions)   # a user workflow: counted and dropped by the adapter
        rows.append((first[0], "actions", "actions_linux", "3", "minutes", "0.006", "0.018", "0",
                     "0.018", "", first[10], first[11] or f"{first[10] or 'synth-org'}/tools",
                     USER_WORKFLOW_PATH, first[13]))
    rows.sort()
    return [list(r) for r in rows]


def write_metered_csv(records: Any, out_dir: Path, *, quirks: bool = True,
                      identity: Identity | None = None, summarized: bool = True
                      ) -> dict[str, Path]:
    """``billing/detailed_usage_report.csv`` from the ``github.metered_usage`` cost lines (seat,
    Actions, sandbox, code-quality and metered AI-credit rows; Actions rows get their workload's
    dynamic Copilot path or an agentic-workflow ``.lock.yml`` path; with *quirks* one user
    workflow row the adapter drops) and, with *summarized*, ``billing/summarized_usage_report.csv``
    (detailed rows and AI usage report lines summed by date, SKU, organization, repository and
    cost center — a cross-check, never read together with the detailed file)."""
    recs = _collect(records)
    ident = _ident(recs, identity)
    out_dir = Path(out_dir)
    rows = _metered_rows(recs, ident, quirks)
    written: dict[str, Path] = {}
    if rows:
        path = _put(out_dir / "billing" / "detailed_usage_report.csv",
                    _csv_text(DETAILED_HEADER, rows), bom=quirks)
        written[_rel(out_dir, path)] = path
    if summarized:
        cells: dict[tuple[str, ...], list[Decimal]] = {}
        units: dict[tuple[str, ...], str] = {}
        source = [r for r in rows if r[12] != USER_WORKFLOW_PATH]
        for row in _ai_rows(recs, ident, None):
            source.append([row.date, row.product, row.sku, row.quantity, row.unit, row.applied,
                           nano_to_usd_str(row.gross), nano_to_usd_str(row.gross - row.net),
                           nano_to_usd_str(row.net), row.login, row.org, row.repo, "", row.cc])
        for r in source:
            key = (r[0], r[1], r[2], r[10], r[11], r[13])
            cell = cells.setdefault(key, [Decimal(0), Decimal(0), Decimal(0)])
            cell[0] += Decimal(r[3])
            cell[1] += Decimal(r[6])
            cell[2] += Decimal(r[8])
            units.setdefault(key, r[4])
        out = []
        for key in sorted(cells):
            qty, gross, net = cells[key]
            g_nano, n_nano = int(gross * 10**9), int(net * 10**9)
            out.append([key[0], key[1], key[2], _qty_text(qty), units[key],
                        _applied(g_nano, _qty_text(qty)), nano_to_usd_str(g_nano),
                        nano_to_usd_str(g_nano - n_nano), nano_to_usd_str(n_nano), key[3],
                        key[4], key[5]])
        if out:
            path = _put(out_dir / "billing" / "summarized_usage_report.csv",
                        _csv_text(SUMMARIZED_HEADER, out), bom=quirks)
            written[_rel(out_dir, path)] = path
    return written


# ---------------------------------------------------------------------------------------------
# billing REST pages (addendum §5.3), derived from the report lines
# ---------------------------------------------------------------------------------------------

#: The two documented ``ai_credit/usage`` SKU / unit name variants (OAS examples).
AI_CREDIT_NAME_VARIANTS = (("Copilot AI Credits", "credits"), ("AI Credit", "ai-credits"))


def _qty_text(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


def write_billing_pages(records: Any, out_dir: Path, *, identity: Identity | None = None
                        ) -> dict[str, Path]:
    """``billing/billing_api.jsonl``: CP-PULL envelopes of the enterprise ``ai_credit/usage``
    pages (per month, items per model; the month's index picks one of
    :data:`AI_CREDIT_NAME_VARIANTS`) and ``usage/summary`` pages (per month, items per product ×
    SKU × unit), derived from the AI usage report and detailed usage lines — a cross-check of
    those lines (``rest.*`` cost lines), never a second copy of them."""
    recs = _collect(records)
    ident = _ident(recs, identity)
    e = ident.enterprise
    ai: dict[str, dict[str, list[int]]] = defaultdict(dict)
    summary: dict[str, dict[tuple[str, str, str], list[Any]]] = defaultdict(dict)
    for line in recs.cost_lines:
        if line.source_kind not in (AI_USAGE_SOURCE, METERED_SOURCE):
            continue
        month = line.date_utc[:7]
        gross = line.list_amount_nano if line.list_amount_nano is not None else line.amount_nano
        if line.source_kind == AI_USAGE_SOURCE and line.cost_type != "ai_credit.legacy_pru":
            label = model_label(line.model, line.routing, line.speed, line.pseudo)
            cell = ai[month].setdefault(label, [0, 0])
            cell[0] += gross
            cell[1] += line.amount_nano
            product, unit = "Copilot", "ai-credits"
            qty = Decimal(line.quantity if line.quantity is not None
                          else nano_to_credits_str(gross))
        else:
            product = _metered_product(line)
            unit = line.unit or _DEFAULT_UNITS.get(line.cost_type or "", "units")
            qty = Decimal(line.quantity) if line.quantity is not None else Decimal(1)
        cell2 = summary[month].setdefault((product, line.sku or "", unit), [Decimal(0), 0, 0])
        cell2[0] += qty
        cell2[1] += gross
        cell2[2] += line.amount_nano
    docs = []
    for i, month in enumerate(sorted(set(ai) | set(summary))):
        year, mon = int(month[:4]), int(month[5:7])
        query = {"year": str(year), "month": str(mon)}
        if month in ai:
            sku_name, unit_name = AI_CREDIT_NAME_VARIANTS[i % 2]
            items = []
            for label, (gross, net) in sorted(ai[month].items()):
                items.append({
                    "product": "Copilot", "sku": sku_name, "model": label, "unitType": unit_name,
                    "pricePerUnit": _dec("0.01"),
                    "grossQuantity": _dec(nano_to_credits_str(gross)),
                    "grossAmount": _dec(nano_to_usd_str(gross)),
                    "discountQuantity": _dec(nano_to_credits_str(gross - net)),
                    "discountAmount": _dec(nano_to_usd_str(gross - net)),
                    "netQuantity": _dec(nano_to_credits_str(net)),
                    "netAmount": _dec(nano_to_usd_str(net))})
            docs.append({"request": {"method": "GET",
                                     "path": f"/enterprises/{e}/settings/billing/ai_credit/usage",
                                     "query": query},
                         "response": {"timePeriod": {"year": year, "month": mon},
                                      "enterprise": e, "usageItems": items}})
        items = []
        for (product, sku, unit), (qty, gross, net) in sorted(summary[month].items()):
            items.append({
                "product": product, "sku": sku, "unitType": unit,
                "pricePerUnit": _dec(_applied(gross, _qty_text(qty))),
                "grossQuantity": _dec(_qty_text(qty)), "grossAmount": _dec(nano_to_usd_str(gross)),
                "discountQuantity": _dec("0"),
                "discountAmount": _dec(nano_to_usd_str(gross - net)),
                "netQuantity": _dec(_qty_text(qty)), "netAmount": _dec(nano_to_usd_str(net))})
        docs.append({"request": {"method": "GET",
                                 "path": f"/enterprises/{e}/settings/billing/usage/summary",
                                 "query": query},
                     "response": {"timePeriod": {"year": year, "month": mon},
                                  "enterprise": e, "usageItems": items}})
    if not docs:
        return {}
    out_dir = Path(out_dir)
    path = _put_lines(out_dir / "billing" / "billing_api.jsonl", docs)
    return {_rel(out_dir, path): path}


# ---------------------------------------------------------------------------------------------
# configuration pages (addendum §5.4)
# ---------------------------------------------------------------------------------------------

_LICENSE_SKUS = frozenset({"copilot_for_business", "copilot_enterprise", "copilot_standalone",
                           "code_quality_licenses"})


def _usd_json(nano: int, *, style: int = 0) -> Any:
    """A USD amount as a JSON number: an int for whole dollars (``style`` even) or ``1000.0``
    (``style`` odd), else the exact decimal."""
    text = nano_to_usd_str(nano)
    if "." in text:
        return _dec(text)
    return int(text) if style % 2 == 0 else _dec(f"{text}.0")


def _envelope(path: str, body: Any, fetched_ms: int | None = None,
              query: Mapping[str, str] | None = None) -> dict[str, Any]:
    request: dict[str, Any] = {"method": "GET", "path": path}
    if query:
        request["query"] = dict(query)
    doc: dict[str, Any] = {"request": request, "response": body}
    if fetched_ms is not None:
        doc["fetched_ms"] = fetched_ms
    return doc


class _Picker:
    """Deterministic login choices per team and cost center (user budgets, recipients)."""

    def __init__(self, ident: Identity) -> None:
        self.ident = ident
        self.used: dict[tuple[str | None, str | None], int] = defaultdict(int)
        self.all = sorted((ident.login(p) for p in ident.logins), key=str)

    def member(self, team: str | None, cc: str | None) -> tuple[str, int]:
        ident = self.ident
        pool = [p for p in ident.members(team) if ident.cost_centers.get(p) == cc] or (
            ident.members(team) if team else [])
        i = self.used[(team, cc)]
        self.used[(team, cc)] += 1
        if pool:
            p = pool[i % len(pool)]
            return ident.login(p), ident.user_id(p)
        return f"ext-user-{i + 1}", 90_000_000 + i

    def logins(self, n: int) -> list[str]:
        if not self.all:
            return [f"billing-admin-{i + 1}" for i in range(n)]
        return [self.all[i % len(self.all)] for i in range(n)]


def _budget_obj(snap: ConfigSnapshot, index: int, pick: _Picker, ident: Identity
                ) -> dict[str, Any]:
    a = dict(snap.attrs)
    scope = a.get("scope") or "custom_scope"
    obj: dict[str, Any] = {"id": snap.entity_id[len("budget:"):],
                           "budget_type": a.get("type") or "CustomPricing"}
    skus = [s for s in (a.get("sku") or "").split(",") if s]
    if len(skus) > 1 or (skus and index % 2 == 1):
        obj["budget_product_skus"] = skus
    else:
        obj["budget_product_sku"] = skus[0] if skus else None
    obj["budget_scope"] = scope
    target = a.get("target")
    user: str | None = None
    if scope == "user":
        user, _ = pick.member(a.get("team"), a.get("cost_center"))
        entity = user
    elif isinstance(target, str) and target.startswith(("org:", "cc:")):
        entity = target.split(":", 1)[1]
    elif isinstance(target, str) and target.startswith("repo:"):
        entity = repo_name(target[5:])
    elif scope in ("enterprise", "multi_user_customer"):
        entity = ident.enterprise
    else:
        entity = ""
    obj["budget_entity_name"] = entity
    if a.get("amount_nano") is not None:
        obj["budget_amount"] = _usd_json(a["amount_nano"], style=index)
    elif any(s in _LICENSE_SKUS for s in skus):
        obj["budget_amount"] = 10                      # a license count: never stored as dollars
    obj["prevent_further_usage"] = a.get("prevent_further_usage")
    alerting: dict[str, Any] = {}
    if a.get("will_alert") is not None:
        alerting["will_alert"] = a["will_alert"]
    if a.get("n_recipients") is not None:
        alerting["alert_recipients"] = pick.logins(a["n_recipients"])
    obj["budget_alerting"] = alerting
    if a.get("expires_at") is not None:
        obj["expires_at"] = a["expires_at"]
    if user is not None:
        obj["user"] = user
        if a.get("consumed_nano") is not None:
            obj["consumed_amount"] = _dec(nano_to_usd_str(a["consumed_nano"]))
    return obj


def _user_states(snap: ConfigSnapshot, pick: _Picker) -> dict[str, Any]:
    """A user-states page whose nearest-rank p50 / p90 and at-or-over count reproduce the
    ``budget_users`` snapshot."""
    a = dict(snap.attrs)
    n = int(a.get("n_users") or 0)
    over = int(a.get("n_at_or_over_target") or 0)
    p50 = a.get("consumed_p50_nano")
    p50 = 0 if p50 is None else int(p50)
    p90 = a.get("consumed_p90_nano")
    p90 = p50 if p90 is None else int(p90)
    r50 = max(1, (50 * n + 99) // 100)
    values = [p50 if i + 1 <= r50 else p90 for i in range(n)]
    rows = []
    for i, (login, consumed) in enumerate(zip(pick.logins(n), values, strict=True)):
        at_or_over = i >= n - over
        target = consumed if at_or_over else consumed + 1_000_000_000
        rows.append({"login": login, "consumed_amount": _dec(nano_to_usd_str(consumed)),
                     "target_amount": _dec(nano_to_usd_str(target))})
    return {"total_count": n, "user_states": rows}


def _cost_center_obj(snap: ConfigSnapshot, ident: Identity) -> dict[str, Any]:
    a = dict(snap.attrs)
    name = snap.entity_id[len("cc:"):]
    people = [ident.login(p) for p, c in sorted(ident.cost_centers.items()) if c == name]
    people.sort()
    resources: list[dict[str, Any]] = []
    for i in range(int(a.get("n_users") or 0)):
        login = people[i] if i < len(people) else f"cc-user-{_hex(name, 6)}-{i + 1}"
        resources.append({"type": "User", "name": login})
    for i in range(int(a.get("n_teams") or 0)):
        resources.append({"type": "Team", "name": f"cc-team-{_hex(name, 6)}-{i + 1}"})
    for i in range(int(a.get("n_orgs") or 0)):
        resources.append({"type": "Org", "name": f"cc-org-{_hex(name, 6)}-{i + 1}"})
    for i in range(int(a.get("n_repos") or 0)):
        resources.append({"type": "Repo", "name": f"synth-org/cc-repo-{_hex(name, 6)}-{i + 1}"})
    obj: dict[str, Any] = {"id": a.get("cost_center_id") or _uuid(snap.entity_id), "name": name}
    if a.get("state") is not None:
        obj["state"] = a["state"]
    obj["resources"] = resources
    if a.get("pool_enabled") is not None:
        obj["ai_credit_pool_enabled"] = a["pool_enabled"]
    if a.get("pool_target_credits") is not None or a.get("pool_current_credits") is not None:
        state: dict[str, Any] = {}
        for attr, key in (("pool_target_credits", "target_amount"),
                          ("pool_current_credits", "current_amount")):
            if a.get(attr) is not None:
                state[key] = _dec(str(a[attr]))
        obj["ai_credit_pool_state"] = state
    if a.get("azure"):
        obj["azure_subscription"] = f"sub-{_hex(name, 12)}"
    return obj


_SEAT_BREAKDOWN_KEYS = (("seats_total", "total"), ("seats_added_this_cycle", "added_this_cycle"),
                        ("seats_pending_cancellation", "pending_cancellation"),
                        ("seats_pending_invitation", "pending_invitation"),
                        ("seats_active_this_cycle", "active_this_cycle"),
                        ("seats_inactive_this_cycle", "inactive_this_cycle"))


def _org_billing(snap: ConfigSnapshot) -> dict[str, Any]:
    a = dict(snap.attrs)
    body: dict[str, Any] = {}
    breakdown = {key: a[attr] for attr, key in _SEAT_BREAKDOWN_KEYS if a.get(attr) is not None}
    body["seat_breakdown"] = breakdown
    for key in ("seat_management_setting", "ide_chat", "platform_chat", "cli"):
        if a.get(key) is not None:
            body[key] = a[key]
    body["public_code_suggestions"] = "block"
    if a.get("plan_type") is not None:
        body["plan_type"] = a["plan_type"]
    return body


def write_config_pages(records: Any, out_dir: Path, *, identity: Identity | None = None
                       ) -> dict[str, Path]:
    """CP-PULL envelopes (``fetched_ms`` = the snapshot time) of the budgets pages and budget
    user-states (``config/budgets.jsonl``), cost centers (``config/cost_centers.jsonl``) and org
    Copilot billing objects (``config/org_copilot_billing.jsonl``) for the ``github.budgets``,
    ``github.cost_centers`` and ``github.org_copilot_settings`` snapshots. Budgets alternate the
    ``budget_product_sku`` / ``budget_product_skus`` spellings and integer / ``1000.0`` amounts;
    a user budget names a login of its team and cost center (the adapter keeps only those)."""
    recs = _collect(records)
    ident = _ident(recs, identity)
    e = ident.enterprise
    out_dir = Path(out_dir)
    pick = _Picker(ident)
    written: dict[str, Path] = {}
    budget_docs: list[dict[str, Any]] = []
    by_ms: dict[int, list[ConfigSnapshot]] = defaultdict(list)
    for snap in recs.config:
        if snap.kind == "budget" and snap.source_kind == "github.budgets":
            by_ms[snap.snapshot_ms].append(snap)
    index = 0
    for ms in sorted(by_ms):
        budgets = []
        for snap in by_ms[ms]:
            budgets.append(_budget_obj(snap, index, pick, ident))
            index += 1
        budget_docs.append(_envelope(f"/enterprises/{e}/settings/billing/budgets",
                                     {"budgets": budgets, "has_next_page": False}, ms))
    for snap in recs.config:
        if snap.kind == "budget_users" and snap.source_kind == "github.budgets":
            bid = snap.entity_id[len("budget:"):]
            budget_docs.append(_envelope(
                f"/enterprises/{e}/settings/billing/budgets/{bid}/user-states",
                _user_states(snap, pick), snap.snapshot_ms))
    if budget_docs:
        path = _put_lines(out_dir / "config" / "budgets.jsonl", budget_docs)
        written[_rel(out_dir, path)] = path
    cc_by_ms: dict[int, list[ConfigSnapshot]] = defaultdict(list)
    for snap in recs.config:
        if snap.kind == "cost_center" and snap.source_kind == "github.cost_centers":
            cc_by_ms[snap.snapshot_ms].append(snap)
    if cc_by_ms:
        docs = [_envelope(f"/enterprises/{e}/settings/billing/cost-centers",
                          {"costCenters": [_cost_center_obj(s, ident) for s in cc_by_ms[ms]]}, ms)
                for ms in sorted(cc_by_ms)]
        path = _put_lines(out_dir / "config" / "cost_centers.jsonl", docs)
        written[_rel(out_dir, path)] = path
    org_docs = [_envelope(f"/orgs/{s.entity_id[len('org:'):]}/copilot/billing", _org_billing(s),
                          s.snapshot_ms)
                for s in recs.config
                if s.kind == "org_settings" and s.source_kind == "github.org_copilot_settings"
                and s.entity_id.startswith("org:")]
    if org_docs:
        path = _put_lines(out_dir / "config" / "org_copilot_billing.jsonl", org_docs)
        written[_rel(out_dir, path)] = path
    return written


# ---------------------------------------------------------------------------------------------
# usage metrics NDJSON (addendum §5.5) and the usage-dashboard export (§22.4)
# ---------------------------------------------------------------------------------------------

_TOP_COUNTS = (("interactions", "user_initiated_interaction_count"),
               ("code_generation", "code_generation_activity_count"),
               ("code_acceptance", "code_acceptance_activity_count"),
               ("loc_suggested_add", "loc_suggested_to_add_sum"),
               ("loc_suggested_delete", "loc_suggested_to_delete_sum"),
               ("loc_added", "loc_added_sum"), ("loc_deleted", "loc_deleted_sum"),
               ("mcp_distinct", "distinct_mcp_use_count"),
               ("skill_distinct", "distinct_skill_use_count"),
               ("custom_agent_distinct", "distinct_custom_agent_use_count"),
               ("plugin_distinct", "distinct_plugin_use_count"),
               ("slash_cmd_distinct", "distinct_slash_cmd_use_count"))
_FLAG_FIELDS = (("used_agent", "used_agent"), ("used_chat", "used_chat"), ("used_cli", "used_cli"),
                ("used_copilot_app", "used_copilot_app"),
                ("used_cloud_agent", "used_copilot_cloud_agent"),
                ("used_code_review_active", "used_copilot_code_review_active"),
                ("used_code_review_passive", "used_copilot_code_review_passive"))
_PR_FIELDS = (("prs_merged", "total_merged"),
              ("prs_created_by_copilot", "total_created_by_copilot"),
              ("prs_merged_created_by_copilot", "total_merged_created_by_copilot"),
              ("prs_reviewed_by_copilot", "total_reviewed_by_copilot"),
              ("copilot_suggestions", "total_copilot_suggestions"),
              ("copilot_applied_suggestions", "total_copilot_applied_suggestions"))
_CLI_KEYS = ("cli_sessions", "cli_requests", "cli_prompts", "cli_prompt_tokens",
             "cli_output_tokens")
METRICS_SOURCE = "github.copilot_metrics"
METRICS_SOURCE_28DAY = "github.copilot_metrics.28day"
METRICS_SOURCE_REPOS = "github.copilot_metrics.repos"


def _surface_block(counts: Mapping[str, int], prefix: str) -> dict[str, Any] | None:
    keys = [k for k in counts if k.startswith(f"{prefix}_")]
    if not keys:
        return None
    block: dict[str, Any] = {}
    for key, src in (("sessions", "session_count"), ("requests", "request_count"),
                     ("prompts", "prompt_count")):
        if f"{prefix}_{key}" in counts:
            block[src] = counts[f"{prefix}_{key}"]
    usage = {}
    for key, src in (("prompt_tokens", "prompt_tokens_sum"),
                     ("output_tokens", "output_tokens_sum")):
        if f"{prefix}_{key}" in counts:
            usage[src] = counts[f"{prefix}_{key}"]
    if usage:
        block["token_usage"] = usage
    return block


def _model_feature_label(model: str) -> str:
    return model if model in ("auto", "unknown", "others") else _display(model)


def _user_metrics(counts: Mapping[str, int], flags: Iterable[str], cost: int | None, *,
                  cli: bool = True, coding_agent_name: bool = False) -> dict[str, Any]:
    """The documented per-user report fields of one user's counts (usage-metrics field
    reference): top-level counts, ``used_*`` flags, ``ai_credits_used`` and the
    ``totals_by_*`` breakdowns; without *cli* the Copilot CLI fields are left out."""
    flags = set(flags)
    rec: dict[str, Any] = {}
    for key, src in _TOP_COUNTS:
        if key in counts:
            rec[src] = counts[key]
    for flag, src in _FLAG_FIELDS:
        if flag == "used_cli" and not cli:
            continue
        if flag == "used_cloud_agent" and coding_agent_name:
            src = "used_copilot_coding_agent"
        rec[src] = flag in flags
    if cost is not None:
        rec["ai_credits_used"] = _dec(nano_to_credits_str(cost))
    ide = [{"ide": k[4:], "user_initiated_interaction_count": v}
           for k, v in sorted(counts.items()) if k.startswith("ide:")]
    if ide:
        rec["totals_by_ide"] = ide
    features = [{"feature": k[8:], "user_initiated_interaction_count": v,
                 "code_generation_activity_count": 0}
                for k, v in sorted(counts.items()) if k.startswith("feature:")
                and (cli or k != "feature:copilot_cli")]
    if features:
        rec["totals_by_feature"] = features
    models = [{"model": _model_feature_label(k[6:]), "feature": "chat_panel_agent_mode",
               "user_initiated_interaction_count": v, "code_generation_activity_count": 0}
              for k, v in sorted(counts.items()) if k.startswith("model:")]
    if models:
        rec["totals_by_model_feature"] = models
    if cli:
        block = _surface_block(counts, "cli")
        if block is not None:
            rec["totals_by_cli"] = block
    app = _surface_block(counts, "app")
    if app is not None:
        rec["totals_by_copilot_app"] = app
    if "third_party_agent_jobs" in counts:
        rec["totals_by_3rd_party_agent"] = [
            {"agent_name": "third-party-agent",
             "user_initiated_interaction_count": counts["third_party_agent_jobs"]}]
    return rec


def _outcome_record(o: OutcomeAggregate) -> dict[str, Any]:
    extra = dict(o.extra)
    pr = {src: extra[key] for key, src in _PR_FIELDS if key in extra}
    if "total_merged" not in pr and o.pull_requests:
        pr["total_merged"] = o.pull_requests
    return {"daily_active_users": o.n_users, "loc_added_sum": o.lines_added,
            "loc_deleted_sum": o.lines_removed,
            "code_acceptance_activity_count": o.edits_accepted, "pull_requests": pr}


def _org_of_label(label: str) -> str | None:
    return label[5:-1] if label.startswith("(org:") and label.endswith(")") else None


def write_metrics_ndjson(records: Any, out_dir: Path, *, identity: Identity | None = None
                         ) -> dict[str, Path]:
    """The usage-metrics reports as NDJSON under ``metrics/``: ``users-1-day.ndjson`` (one record
    per daily ``ActivityDay``, documented field names), ``enterprise-1-day.ndjson`` (the
    ``(enterprise)`` outcome rows), ``organization-1-day.jsonl`` (``(org:<login>)`` rows as
    CP-PULL envelopes, so the org is named by the request path), ``repos-1-day.ndjson`` /
    ``org-repos-1-day.jsonl`` (pull-request totals) and ``user-teams-1-day.ndjson`` (seated users
    of teams with at least 5 of them, on the last day — GitHub omits smaller teams)."""
    recs = _collect(records)
    ident = _ident(recs, identity)
    out_dir = Path(out_dir)
    eid = str(ident.enterprise_id)
    written: dict[str, Path] = {}

    def emit(rel: str, docs: list[Any]) -> None:
        if docs:
            path = _put_lines(out_dir / "metrics" / rel, docs)
            written[_rel(out_dir, path)] = path

    users = []
    for i, act in enumerate(recs.activity):
        if act.source_kind != METRICS_SOURCE:
            continue
        rec: dict[str, Any] = {"day": act.date_utc, "enterprise_id": eid,
                               "user_id": ident.user_id(act.principal),
                               "user_login": ident.login(act.principal)}
        rec.update(_user_metrics(dict(act.counts), act.flags, act.reported_cost_nano,
                                 coding_agent_name=i % 5 == 4))
        users.append(rec)
    emit("users-1-day.ndjson", users)
    ent, orgs, repos, org_repos = [], [], [], []
    for o in recs.outcomes:
        if o.source_kind == METRICS_SOURCE and o.team == "(enterprise)":
            ent.append({"day": o.date_utc, "enterprise_id": eid, **_outcome_record(o)})
        elif o.source_kind == METRICS_SOURCE and _org_of_label(o.team):
            org = _org_of_label(o.team)
            orgs.append(_envelope(f"/orgs/{org}/copilot/metrics/reports/organization-1-day",
                                  {"day": o.date_utc, "organization_id": str(
                                      int(_hex(str(org), 6), 16)), **_outcome_record(o)},
                                  query={"day": o.date_utc}))
        elif o.source_kind == METRICS_SOURCE_REPOS:
            body = {"day": o.date_utc, "repo_id": int(_hex(f"{o.team}{o.date_utc}", 7), 16),
                    "pull_requests": _outcome_record(o)["pull_requests"]}
            org = _org_of_label(o.team)
            if org:
                org_repos.append(_envelope(f"/orgs/{org}/copilot/metrics/reports/repos-1-day",
                                           body, query={"day": o.date_utc}))
            else:
                repos.append(body)
    emit("enterprise-1-day.ndjson", ent)
    emit("organization-1-day.jsonl", orgs)
    emit("repos-1-day.ndjson", repos)
    emit("org-repos-1-day.jsonl", org_repos)
    days = [a.date_utc for a in recs.activity] + [x.snapshot_date for x in recs.licenses]
    if days and ident.teams:
        day = max(days)
        teams = sorted({t for t in ident.teams.values()})
        rows = []
        for team in teams:
            members = ident.members(team)
            if len(members) < 5:
                continue
            for p in members:
                rows.append({"day": day, "enterprise_id": eid, "user_id": ident.user_id(p),
                             "user_login": ident.login(p),
                             "team_id": int(_hex(f"team:{team}", 6), 16), "slug": team})
        emit("user-teams-1-day.ndjson", rows)
    return written


def write_dashboard_ndjson(records: Any, out_dir: Path, *, identity: Identity | None = None,
                           end_day: str | None = None) -> dict[str, Path]:
    """``ui/copilot_usage_dashboard.ndjson``: the usage-dashboard export (Insights → Copilot usage
    → export) in the API 28-day shape — one record per user with ``report_start_day`` /
    ``report_end_day`` and the user's counts summed over the 28 days ending *end_day* (default:
    the last activity day), **without** the Copilot CLI fields (the dashboard excludes CLI), plus
    one enterprise record with ``day_totals`` from the ``(enterprise)`` outcome rows. Existing
    28-day activity rows (``github.copilot_metrics.28day``) are written as they are."""
    recs = _collect(records)
    ident = _ident(recs, identity)
    daily = [a for a in recs.activity if a.source_kind == METRICS_SOURCE]
    windowed = [a for a in recs.activity if a.source_kind == METRICS_SOURCE_28DAY]
    eid = str(ident.enterprise_id)
    docs: list[dict[str, Any]] = []
    if windowed:
        for act in windowed:
            start = _shift(act.date_utc, -27)
            docs.append({"report_start_day": start, "report_end_day": act.date_utc,
                         "enterprise_id": eid, "user_id": ident.user_id(act.principal),
                         "user_login": ident.login(act.principal),
                         **_user_metrics(dict(act.counts), act.flags, act.reported_cost_nano,
                                         cli=False)})
        end = max(a.date_utc for a in windowed)
    elif daily:
        end = end_day or max(a.date_utc for a in daily)
        start = _shift(end, -27)
        sums: dict[str, dict[str, int]] = defaultdict(dict)
        flags: dict[str, set[str]] = defaultdict(set)
        cost: dict[str, int | None] = {}
        for act in daily:
            if not start <= act.date_utc <= end:
                continue
            cell = sums[act.principal]
            for k, v in act.counts:
                if k in _CLI_KEYS or k == "feature:copilot_cli":
                    continue
                cell[k] = cell.get(k, 0) + v
            flags[act.principal].update(f for f in act.flags if f != "used_cli")
            if act.reported_cost_nano is not None:
                cost[act.principal] = (cost.get(act.principal) or 0) + act.reported_cost_nano
            else:
                cost.setdefault(act.principal, None)
        for p in sorted(sums, key=ident.login):
            docs.append({"report_start_day": start, "report_end_day": end, "enterprise_id": eid,
                         "user_id": ident.user_id(p), "user_login": ident.login(p),
                         **_user_metrics(sums[p], flags[p], cost.get(p), cli=False)})
    else:
        return {}
    start = _shift(end, -27)
    totals = [{"day": o.date_utc, **_outcome_record(o)} for o in recs.outcomes
              if o.team == "(enterprise)" and o.source_kind in (METRICS_SOURCE,
                                                                METRICS_SOURCE_28DAY)
              and start <= o.date_utc <= end]
    if totals:
        docs.append({"report_start_day": start, "report_end_day": end, "enterprise_id": eid,
                     "day_totals": totals})
    out_dir = Path(out_dir)
    path = _put_lines(out_dir / "ui" / "copilot_usage_dashboard.ndjson", docs)
    return {_rel(out_dir, path): path}


# ---------------------------------------------------------------------------------------------
# seats (addendum §5.6), agent tasks (§5.7) and the activity report (§5.15)
# ---------------------------------------------------------------------------------------------

#: Seat ``last_activity_editor`` strings per editor family (``vscode/…`` from ghec.json; the
#: others synthetic, JetBrains strings unverified — they only need ``editor_family`` to agree).
SEAT_EDITORS: Mapping[str, str] = {
    "vscode": "vscode/1.126.0/copilot-chat/0.35.3",
    "jetbrains": "JetBrains-IU/252.23892.409/copilot-intellij/1.5.52-241",
    "visual_studio": "VisualStudio/17.14.4/copilot-vs/1.0.0",
    "xcode": "Xcode/16.4/copilot-xcode/0.40.0",
    "eclipse": "Eclipse/4.36.0/copilot-eclipse/0.9.0",
    "neovim": "Neovim/0.11.2/copilot.vim/1.50.0",
    "cli": "copilot-cli/1.0.64",
    "github_com": "github.com",
    "copilot_app": "GitHub Copilot App/1.0.0",
    "mobile": "GitHub Mobile/1.200.0",
    "other": "zed/0.190.0",
}
#: Activity-report ``last_surface_used`` strings per family (metrics-data reference examples
#: ``VS Code 1.89.1``, ``Copilot Chat``, ``Unspecified``; the JetBrains string is synthetic).
REPORT_SURFACES: Mapping[str, str] = {
    "vscode": "VS Code 1.126.0",
    "jetbrains": "JetBrains IntelliJ IDEA 2025.2.3",
    "visual_studio": "Visual Studio 17.14",
    "xcode": "Xcode 16.4",
    "eclipse": "Eclipse 4.36",
    "neovim": "Neovim 0.11.2",
    "cli": "Copilot CLI 1.0.64",
    "github_com": "Copilot Chat",
    "copilot_app": "GitHub Copilot App",
    "mobile": "GitHub Mobile",
    "other": "Zed 0.190.0",
}
#: Days before the snapshot a recency bucket is rendered at (``none_90d`` → no timestamp).
_BUCKET_DAYS = {"0-7": 1, "8-30": 15, "31-90": 45}


def _bucket_ts(day: str, bucket: str, hour: int) -> str | None:
    back = _BUCKET_DAYS.get(bucket)
    if back is None:
        return None
    return _iso(_day_ms(_shift(day, -back)) + hour * 3_600_000)


def _seat_obj(lic: LicenseSnapshot, ident: Identity, org: str | None) -> dict[str, Any]:
    login = ident.login(lic.principal)
    uid = ident.user_id(lic.principal)
    seat: dict[str, Any] = {
        "created_at": f"{lic.seat_created}T08:00:00Z" if lic.seat_created else None,
        "updated_at": _iso(_day_ms(_shift(lic.snapshot_date, -1)) + 9 * 3_600_000),
        "pending_cancellation_date": lic.pending_cancellation,
        "last_activity_at": _bucket_ts(lic.snapshot_date, lic.last_activity_bucket, 14),
        "last_activity_editor": (SEAT_EDITORS.get(lic.last_activity_surface, "zed/0.190.0")
                                 if lic.last_activity_surface else None),
        "last_authenticated_at": _bucket_ts(lic.snapshot_date, lic.last_authenticated_bucket, 13),
        "plan_type": lic.plan,
        "assignee": {"login": login, "id": uid, "node_id": f"U_{_hex(login, 12)}",
                     "type": "User", "site_admin": False,
                     "name": f"Synthetic Developer {CANARY}", "email": CANARY_EMAIL,
                     "avatar_url": f"https://avatars.githubusercontent.com/u/{uid}?v=4",
                     "url": f"https://api.github.com/users/{login}",
                     "html_url": f"https://github.com/{login}"},
    }
    if lic.assigned_via_team:
        team = lic.team or "copilot-users"
        seat["assigning_team"] = {"id": int(_hex(f"team:{team}", 6), 16), "name": team,
                                  "slug": team, "privacy": "closed"}
    if org:
        seat["organization"] = {"login": org, "id": int(_hex(org, 6), 16)}
    return seat


def write_seats_pages(records: Any, out_dir: Path, *, identity: Identity | None = None,
                      per_page: int = 100) -> dict[str, Path]:
    """``seats/copilot_seats.jsonl``: ``GET /orgs/{org}/copilot/billing/seats`` pages (CP-PULL
    envelopes with ``fetched_ms`` on the snapshot date, ``per_page`` seats each) for every
    seats-API ``LicenseSnapshot``; org-less seats go to the enterprise endpoint. Recency buckets
    become timestamps inside their bucket; ``assigning_team`` marks team-assigned seats; the
    assignee's name and e-mail carry the canary (dropped at parse time)."""
    recs = _collect(records)
    ident = _ident(recs, identity)
    groups: dict[tuple[str, str], list[LicenseSnapshot]] = defaultdict(list)
    for lic in recs.licenses:
        if lic.source_kind == "github.copilot_seats":
            groups[(lic.org or "", lic.snapshot_date)].append(lic)
    docs = []
    for (org, day) in sorted(groups):
        seats = sorted(groups[(org, day)], key=lambda x: ident.login(x.principal))
        stamps = [x.fetched_ms for x in seats if _day_of(x.fetched_ms) == day]
        fetched = max(stamps) if stamps else _day_ms(day) + 6 * 3_600_000
        path = (f"/orgs/{org}/copilot/billing/seats" if org
                else f"/enterprises/{ident.enterprise}/copilot/billing/seats")
        for page, start in enumerate(range(0, len(seats), per_page), start=1):
            chunk = seats[start:start + per_page]
            docs.append(_envelope(path, {"total_seats": len(seats),
                                         "seats": [_seat_obj(x, ident, org or None)
                                                   for x in chunk]},
                                  fetched, query={"page": str(page), "per_page": str(per_page)}))
    if not docs:
        return {}
    out_dir = Path(out_dir)
    path = _put_lines(out_dir / "seats" / "copilot_seats.jsonl", docs)
    return {_rel(out_dir, path): path}


def write_agent_task_pages(records: Any, out_dir: Path, *, identity: Identity | None = None
                           ) -> dict[str, Path]:
    """``agents/agent_tasks.jsonl``: one ``GET /agents/repos/{owner}/{repo}/tasks`` page per
    repository (CP-PULL envelopes) with one task and session per ``github.agent_tasks``
    aggregate (model label, state, artifacts, creator id of a member of the aggregate's team,
    ``usage.amount`` = nano-USD × 100 nano-credits); task name and session prompt carry the
    canary (never read). Aggregates without a repository go to a plain page without ids."""
    recs = _collect(records)
    ident = _ident(recs, identity)
    by_team: dict[str, list[str]] = defaultdict(list)
    for p, t in ident.teams.items():
        by_team[t].append(p)
    pages: dict[str, list[dict[str, Any]]] = defaultdict(list)
    counter: dict[str, int] = defaultdict(int)
    for agg in recs.aggregates:
        if agg.source_kind != "github.agent_tasks":
            continue
        d = dict(agg.dims)
        team = d.get("team")
        members = ident.members(team) if team else []
        i = counter[team or ""]
        counter[team or ""] += 1
        uid = ident.user_id(members[i % len(members)]) if members else 1
        repo = repo_name(d.get("repo")) if d.get("repo") else ""
        repo_id = int(_hex(repo, 7), 16) if repo else None
        state = d.get("state", "completed")
        artifact = d.get("artifact", "none")
        arts = [{"type": t, "id": f"{t}-{_hex(agg.agg_id + t, 6)}"} for t in ("branch", "pull")
                if t in artifact.split("+")]
        model = d.get("model", "unknown")
        session: dict[str, Any] = {
            "id": agg.agg_id, "name": f"Copilot session {CANARY}",
            "prompt": f"Fix the failing build {CANARY}", "state": state,
            "model": _model_feature_label(model),
            "created_at": _iso(agg.bucket_start_ms, millis=True),
            "completed_at": (_iso(agg.bucket_end_ms, millis=True)
                             if agg.bucket_end_ms > agg.bucket_start_ms else None),
            "user": {"id": uid}}
        if repo_id is not None:
            session["repository"] = {"id": repo_id}
        if agg.reported_cost_nano is not None:
            session["usage"] = {"type": "ai_credits", "amount": agg.reported_cost_nano * 100}
        task: dict[str, Any] = {"id": f"task-{agg.agg_id[3:]}",
                                "name": f"Copilot task {CANARY}", "state": state,
                                "creator_type": "user", "creator": {"id": uid},
                                "created_at": session["created_at"], "artifacts": arts,
                                "session_count": 1, "sessions": [session]}
        if repo_id is not None:
            task["repository"] = {"id": repo_id}
        pages[repo].append(task)
    docs = []
    for repo in sorted(pages):
        body = {"tasks": pages[repo], "total_count": len(pages[repo])}
        if repo:
            owner, name = repo.split("/", 1)
            docs.append(_envelope(f"/agents/repos/{owner}/{name}/tasks", body))
        else:
            docs.append(body)
    if not docs:
        return {}
    out_dir = Path(out_dir)
    path = _put_lines(out_dir / "agents" / "agent_tasks.jsonl", docs)
    return {_rel(out_dir, path): path}


#: The documented activity report columns.
ACTIVITY_REPORT_HEADER = ("report_time", "login", "last_authenticated_at", "last_activity_at",
                          "last_surface_used")
_BUCKET_ORDER = ("0-7", "8-30", "31-90", "none_90d")


def _report_licenses(recs: _Recs) -> list[LicenseSnapshot]:
    """The activity report's rows: its own snapshots when the records have them, else one row per
    seat holder of the latest seats snapshot (the most recent activity across orgs)."""
    own = [x for x in recs.licenses if x.source_kind == "github.copilot_activity_report"]
    if own:
        return own
    seats = [x for x in recs.licenses if x.source_kind == "github.copilot_seats"]
    if not seats:
        return []
    day = max(x.snapshot_date for x in seats)
    best: dict[str, LicenseSnapshot] = {}
    for x in seats:
        if x.snapshot_date != day:
            continue
        cur = best.get(x.principal)
        if cur is None or _BUCKET_ORDER.index(x.last_activity_bucket) < _BUCKET_ORDER.index(
                cur.last_activity_bucket):
            best[x.principal] = x
    return list(best.values())


def write_activity_report_csv(records: Any, out_dir: Path, *, identity: Identity | None = None,
                              quirks: bool = True) -> dict[str, Path]:
    """``ui/copilot_activity_report.csv`` (Licensing → Get activity report): the documented columns
    ``report_time, login, last_authenticated_at, last_activity_at, last_surface_used`` (BOM with
    *quirks*); surfaces such as ``VS Code 1.126.0``, a JetBrains string, ``Copilot Chat`` and
    ``Unspecified`` (no surface). Rows come from activity-report snapshots, else from the latest
    seats snapshot (one row per seat holder)."""
    recs = _collect(records)
    ident = _ident(recs, identity)
    rows = []
    for lic in sorted(_report_licenses(recs), key=lambda x: (x.snapshot_date,
                                                              ident.login(x.principal))):
        day = lic.snapshot_date
        surface = (REPORT_SURFACES.get(lic.last_activity_surface, "Zed 0.190.0")
                   if lic.last_activity_surface else "Unspecified")
        rows.append([_iso(_day_ms(day) + 12 * 3_600_000 + 17 * 60_000), ident.login(lic.principal),
                     _bucket_ts(day, lic.last_authenticated_bucket, 8) or "",
                     _bucket_ts(day, lic.last_activity_bucket, 10) or "", surface])
    if not rows:
        return {}
    out_dir = Path(out_dir)
    path = _put(out_dir / "ui" / "copilot_activity_report.csv",
                _csv_text(ACTIVITY_REPORT_HEADER, rows), bom=quirks)
    return {_rel(out_dir, path): path}


# ---------------------------------------------------------------------------------------------
# admin answers (addendum §5.17; CP-HANDOFF template schema)
# ---------------------------------------------------------------------------------------------

ANSWERS_SCHEMA = "tokenbill/copilot-admin-answers@1"
_ANSWER_MAPS = ("plan", "pool_seats", "billing_mode", "renewal_date", "capped_policy",
                "budget_stop")
_ANSWER_SCALARS = ("compliance", "promo_eligible", "paid_usage_policy", "org_cli_billing_policy")
#: ``write_admin_answers(variant=…)`` values.
ANSWER_VARIANTS = ("plan_unknown", "plan_conflict")


def _conflict_orgs(recs: _Recs) -> list[str]:
    """Orgs whose seats API says ``enterprise`` while their seat lines say Business."""
    api = {x.org for x in recs.licenses
           if x.source_kind == "github.copilot_seats" and x.plan == "enterprise" and x.org}
    lines = {c.workspace_id for c in recs.cost_lines
             if c.cost_type == "seat" and c.sku in ("copilot_for_business", "copilot_standalone")}
    return sorted(o for o in api & lines if o)


def write_admin_answers(records: Any, out_dir: Path, *, variant: str | None = None,
                        identity: Identity | None = None) -> dict[str, Path]:
    """``admin/answers.json`` in CP-HANDOFF's template schema: statements from the records'
    ``run_flags`` (``plan.<entity>``, ``pool_seats.<entity>.<plan>``, ``billing_mode.<entity>``,
    ``renewal_date.<entity>``, ``capped_policy.<cc>``, ``compliance``, ``promo_eligible``,
    ``paid_usage_policy``, ``org_cli_billing_policy``, ``budget_stop.<entity>``), the orgs' seat
    policies (``org_settings.seat_management_setting``) and, without a stated plan, the plan the
    seat lines show per org; unknown answers stay ``"unknown"``. ``variant="plan_unknown"`` leaves
    ``plan`` out; ``"plan_conflict"`` states ``enterprise`` for the conflicted orgs (seats API
    Enterprise, seat lines Business), else for the enterprise."""
    recs = _collect(records)
    if variant is not None and variant not in ANSWER_VARIANTS:
        raise UsageError(f"write_admin_answers: unknown variant {variant!r}")
    maps: dict[str, dict[str, Any]] = {k: {} for k in _ANSWER_MAPS}
    scalars: dict[str, Any] = {k: "unknown" for k in _ANSWER_SCALARS}
    seat_policy: dict[str, str] = {}
    for snap in recs.config:
        if snap.kind == "run_flags":
            for key, value in snap.attrs:
                if value is None:
                    continue
                if key in _ANSWER_SCALARS:
                    scalars[key] = value
                elif key.startswith("pool_seats."):
                    entity, plan = key[len("pool_seats."):].rsplit(".", 1)
                    maps["pool_seats"].setdefault(entity, {})[plan] = value
                elif key.startswith("capped_policy."):
                    maps["capped_policy"][f"cc:{key[len('capped_policy.'):]}"] = value
                else:
                    head, _, entity = key.partition(".")
                    if head in maps and entity:
                        maps[head][entity] = value
        elif snap.kind == "org_settings":
            policy = dict(snap.attrs).get("seat_management_setting")
            if policy in ("assign_all", "assign_selected") and snap.entity_id.startswith("org:"):
                seat_policy.setdefault(snap.entity_id, policy)
    if not maps["plan"]:
        per_org: dict[str, set[str]] = defaultdict(set)
        for c in recs.cost_lines:
            fact = _facts.copilot_skus().get(c.sku or "")
            if c.cost_type == "seat" and fact is not None and fact.plan and c.workspace_id:
                per_org[f"org:{c.workspace_id}"].add(fact.plan)
        for entity, plans in sorted(per_org.items()):
            maps["plan"][entity] = plans.pop() if len(plans) == 1 else "mixed"
    if variant == "plan_conflict":
        orgs = _conflict_orgs(recs)
        maps["plan"] = ({f"org:{o}": "enterprise" for o in orgs} if orgs
                        else {"enterprise": "enterprise"})
    doc: dict[str, Any] = {"schema": ANSWERS_SCHEMA}
    for key in ("plan", "pool_seats", "billing_mode", "renewal_date", "capped_policy"):
        if key == "plan" and variant == "plan_unknown":
            continue
        doc[key] = dict(sorted(maps[key].items()))
    doc["compliance"] = scalars["compliance"]
    doc["promo_eligible"] = scalars["promo_eligible"]
    doc["paid_usage_policy"] = scalars["paid_usage_policy"]
    doc["org_cli_billing_policy"] = scalars["org_cli_billing_policy"]
    doc["budget_stop"] = dict(sorted(maps["budget_stop"].items()))
    doc["seat_policy"] = dict(sorted(seat_policy.items()))
    out_dir = Path(out_dir)
    path = _put(out_dir / "admin" / "answers.json",
                json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    return {_rel(out_dir, path): path}


# ---------------------------------------------------------------------------------------------
# local sources: calls flattened from canonical requests
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Call:
    """One Copilot model call, flattened from a canonical ``Request``."""

    req: Request
    lane_kind: str
    agent_id: str | None
    ts: int
    end: int
    ttft: int
    model: str
    model_raw: str
    routing: str
    context_tier: str | None
    ttl_hint: str | None
    usage: UsageBuckets
    nano_usd: int | None
    response_id: str
    request_id: str
    status: int
    compactions: tuple[Inference, ...]
    product: str

    @property
    def session_key(self) -> str:
        return self.req.session_key

    @property
    def principal(self) -> str | None:
        return self.req.attribution.principal

    @property
    def nano_aiu(self) -> int | None:
        return None if self.nano_usd is None else self.nano_usd * 100


def _sum_cost(infs: Sequence[Inference]) -> int | None:
    costs = [i.provider_reported_cost_nano for i in infs
             if i.provider_reported_cost_nano is not None]
    return sum(costs) if costs else None


def _calls(recs: _Recs) -> list[_Call]:
    out = []
    for req in recs.requests:
        lane = recs.lanes.get(req.lane_key)
        kind = lane.kind.value if lane is not None else (
            "subagent" if req.attribution.query_source == "subagent" else "main")
        att = req.final_attempt
        infs = [i for i in att.inferences if i.billable is not False]
        msg = [i for i in infs if i.kind is not InferenceKind.COMPACTION]
        comp = tuple(i for i in infs if i.kind is InferenceKind.COMPACTION)
        if kind == "compaction" and msg and not comp:
            comp, msg = tuple(msg), []
        serving = req.serving_inference or (msg[-1] if msg else (comp[-1] if comp else None))
        pricing = serving.pricing if serving is not None else None
        model = (pricing.model if pricing is not None and pricing.model
                 else req.params.model_requested)
        duration = att.duration_ms if att.duration_ms is not None else 1_500 + req.seq % 7 * 250
        product = req.attribution.agent_product or "copilot_cli"
        out.append(_Call(
            req=req, lane_kind=kind,
            agent_id=_hex(f"agent:{req.lane_key}", 8) if kind == "subagent" else None,
            ts=req.ts_start_ms, end=req.ts_start_ms + duration,
            ttft=att.ttft_ms if att.ttft_ms is not None else min(duration, 600),
            model=model, model_raw=(pricing.model_raw if pricing is not None else model) or model,
            routing=pricing.routing if pricing is not None else "direct",
            context_tier=pricing.context_tier if pricing is not None else None,
            ttl_hint=pricing.write_ttl_hint if pricing is not None else None,
            usage=sum((i.usage for i in msg), UsageBuckets()), nano_usd=_sum_cost(msg),
            response_id=att.provider_message_id or f"msg_{_hex('resp:' + req.request_id, 24)}",
            request_id=att.provider_request_id or _uuid(f"req:{req.request_id}"),
            status=att.http_status or (200 if att.outcome.value == "ok" else 500),
            compactions=comp, product=product))
    out.sort(key=lambda c: (c.session_key, c.ts, c.req.seq, c.req.request_id))
    return out


def raw_session_id(session_key: str) -> str:
    """The raw (UUID-shaped) Copilot session / conversation id the files use for a canonical
    session key."""
    return _uuid(f"ses:{session_key}")


def _owner_dir(principal: str | None) -> str:
    return f"u-{_hex(principal or 'unattributed', 8)}"


def _group(calls: Iterable[_Call], key: Callable[[_Call], Any]) -> dict[Any, list[_Call]]:
    out: dict[Any, list[_Call]] = defaultdict(list)
    for c in calls:
        out[key(c)].append(c)
    return dict(out)


def _session_events(recs: _Recs, session_key: str) -> list[LaneEvent]:
    lanes = {r.lane_key for r in recs.requests if r.session_key == session_key}
    lanes |= {k for k, lane in recs.lanes.items() if lane.session_key == session_key}
    return sorted((e for e in recs.events if e.lane_key in lanes),
                  key=lambda e: (e.ts_ms, e.kind.value, repr(e.attrs)))


# ---------------------------------------------------------------------------------------------
# ~/.copilot: session-state/<id>/events.jsonl (SDK schema) and session-store.db (third-party)
# ---------------------------------------------------------------------------------------------

#: DDL of the CLI session store as third-party readers (tokscale, codeburn) describe it
#: (``assistant_usage_events`` required and optional columns of addendum §5.9).
SESSION_STORE_DDL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, cwd TEXT, repository TEXT, branch TEXT, summary TEXT,
    created_at TEXT NOT NULL, updated_at TEXT
);
CREATE TABLE assistant_usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, turn_index INTEGER,
    model TEXT NOT NULL, copilot_usage_model TEXT, input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER, cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER,
    total_nano_aiu INTEGER, duration_ms INTEGER, initiator TEXT, request_multiplier REAL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_usage_session ON assistant_usage_events(session_id);
"""


class _EventLog:
    """Builds one ``events.jsonl``: envelopes ``{type, data, id, timestamp, parentId[, agentId]}``
    (``type`` first, so a torn line is recoverable from its last ``{"type":``)."""

    def __init__(self, raw_id: str) -> None:
        self.raw_id = raw_id
        self.items: list[tuple[int, int, dict[str, Any]]] = []

    def add(self, ts: int, etype: str, data: dict[str, Any], agent_id: str | None = None) -> None:
        env: dict[str, Any] = {"type": etype, "data": data}
        if agent_id:
            env["agentId"] = agent_id
        self.items.append((ts, len(self.items), env))

    def lines(self, *, concatenate: bool = False) -> list[str]:
        out = []
        parent = None
        for n, (ts, _, env) in enumerate(sorted(self.items, key=lambda x: (x[0], x[1]))):
            eid = _uuid(f"{self.raw_id}:event:{n}")
            env = {**env, "id": eid, "timestamp": _iso(ts, millis=True), "parentId": parent}
            parent = eid
            out.append(_dumps(env))
        if concatenate:
            for i, line in enumerate(out):
                if line.startswith('{"type":"user.message"') and i > 1:
                    out[i] = line[: len(line) // 2] + line   # a torn write, then the retry
                    break
        return out


def _compaction_data(ev: LaneEvent | None, inf: Inference | None, model_raw: str
                     ) -> dict[str, Any]:
    a = dict(ev.attrs) if ev is not None else {}
    pre = a.get("pre_tokens") if isinstance(a.get("pre_tokens"), int) else 150_000
    post = a.get("post_tokens") if isinstance(a.get("post_tokens"), int) else 20_000
    trigger = a.get("copilot_trigger") or ("manual" if a.get("trigger") == "manual"
                                           else "threshold")
    data: dict[str, Any] = {
        "success": True, "trigger": trigger, "preCompactionTokens": pre,
        "postCompactionTokens": post,
        "tokensRemoved": a["dropped_tokens"] if isinstance(a.get("dropped_tokens"), int)
        else max(pre - post, 0),
        "messagesRemoved": 12, "summaryContent": f"Summary of the earlier work {CANARY}"}
    for attr, key in (("system_tokens", "systemTokens"),
                      ("tool_definitions_tokens", "toolDefinitionsTokens")):
        if isinstance(a.get(attr), int):
            data[key] = a[attr]
    if inf is not None:
        u = inf.usage
        used: dict[str, Any] = {"model": model_raw, "inputTokens": u.uncached_input,
                                "outputTokens": u.output, "cacheReadTokens": u.cache_read,
                                "cacheWriteTokens": u.cache_write,
                                "duration": a.get("duration_ms", 4_000)}
        if inf.provider_reported_cost_nano is not None:
            total = inf.provider_reported_cost_nano * 100
            parts = [(t, n) for t, n in (("input", u.uncached_input),
                                         ("cache_read", u.cache_read),
                                         ("cache_write", u.cache_write), ("output", u.output))
                     if n > 0]
            shares = _allocate(total, [n for _, n in parts])
            used["copilotUsage"] = {
                "model": model_raw, "totalNanoAiu": total,
                "tokenDetails": [{"tokenType": t, "tokenCount": n, "batchSize": n,
                                  "costPerBatch": s, "model": model_raw}
                                 for (t, n), s in zip(parts, shares, strict=True)]}
        data["compactionTokensUsed"] = used
    return data


def _cli_log(recs: _Recs, raw_id: str, calls: Sequence[_Call]) -> _EventLog:
    log = _EventLog(raw_id)
    first = calls[0]
    events = _session_events(recs, first.session_key)
    meta = {k: v for e in events if e.kind is LaneEventKind.SESSION_META for k, v in e.attrs}
    repo = first.req.attribution.repo
    start: dict[str, Any] = {
        "sessionId": raw_id, "version": 1, "producer": "copilot-agent",
        "copilotVersion": _COPILOT_VERSION, "startTime": _iso(first.ts - 2_000, millis=True),
        "selectedModel": ("auto" if first.routing == "auto" or meta.get("routing_mode") == "auto"
                          else first.model_raw),
        "context": {"cwd": f"/home/dev/src/project {CANARY}", "gitRoot": f"/home/dev/src/{CANARY}",
                    "branch": f"feature/{CANARY}", "hostType": "github",
                    **({"repository": repo_name(repo)} if repo else {})}}
    tier = meta.get("context_tier") or first.context_tier
    if tier:
        start["contextTier"] = tier
    if isinstance(meta.get("credit_limit_nano"), int):
        start["sessionLimits"] = {"maxAiCredits": _dec(nano_to_credits_str(
            meta["credit_limit_nano"]))}
    log.add(max(first.ts - 2_000, 0), "session.start", start)
    comp_events = [e for e in events if e.kind is LaneEventKind.COMPACTION]
    used_events: set[int] = set()
    for n, c in enumerate(calls):
        iid = _uuid(f"interaction:{c.req.request_id}")
        if c.lane_kind == "main" and c.req.seq % 3 == 0:
            log.add(c.ts, "user.message", {
                "content": f"Please fix the failing test in module {n} {CANARY}",
                "transformedContent": f"<context>{CANARY}</context>", "interactionId": iid})
        tool = f"toolu_{_hex(c.req.request_id, 20)}"
        with_tool = c.req.seq % 2 == 0
        if c.usage.total_input or c.usage.output or c.lane_kind != "compaction":
            msg: dict[str, Any] = {
                "messageId": _uuid(f"message:{c.req.request_id}"),
                "content": f"I updated the handler {CANARY}", "model": c.model_raw,
                "outputTokens": c.usage.output, "requestId": c.request_id,
                "serviceRequestId": _uuid(f"service:{c.req.request_id}"),
                "apiCallId": f"call_{_hex(c.req.request_id, 16)}", "interactionId": iid,
                "turnId": str(c.req.seq)}
            if c.usage.output_reasoning:
                msg["reasoningText"] = f"Considering the options {CANARY}"
            if with_tool:
                msg["toolRequests"] = [{"toolCallId": tool, "name": "bash",
                                        "arguments": {"command": f"pytest -q {CANARY}"},
                                        "type": "function"}]
            log.add(c.end, "assistant.message", msg, c.agent_id)
            if with_tool:
                log.add(c.end + 10, "tool.execution_start", {
                    "toolCallId": tool, "toolName": "bash",
                    "arguments": {"command": f"pytest -q {CANARY}"}}, c.agent_id)
                log.add(c.end + 900, "tool.execution_complete", {
                    "toolCallId": tool, "success": True, "model": c.model_raw,
                    "result": {"content": f"3 passed {CANARY}",
                               "detailedContent": f"collected 3 items {CANARY}"}}, c.agent_id)
        for inf in c.compactions:
            near = [i for i, e in enumerate(comp_events)
                    if i not in used_events and abs(e.ts_ms - c.ts) <= 600_000]
            ev = comp_events[near[0]] if near else None
            if near:
                used_events.add(near[0])
            log.add(c.ts + 1, "session.compaction_complete",
                    _compaction_data(ev, inf, c.model_raw), c.agent_id)
    for i, ev in enumerate(comp_events):
        if i not in used_events:
            log.add(ev.ts_ms, "session.compaction_complete", _compaction_data(ev, None, ""))
    ttl = 3_600 if any(c.ttl_hint == "1h" for c in calls) else 300
    for ev in events:
        a = dict(ev.attrs)
        if ev.kind is LaneEventKind.MODEL_SWITCH_USER:
            log.add(ev.ts_ms, "session.model_change", {
                "previousModel": a.get("from_model"), "newModel": a.get("to_model"),
                "source": "user"})
        elif ev.kind is LaneEventKind.MODEL_FALLBACK:
            log.add(ev.ts_ms, "session.model_change", {
                "previousModel": a.get("from_model"), "newModel": a.get("to_model"),
                "cause": str(a.get("trigger") or "unknown")})
        elif ev.kind is LaneEventKind.CONTEXT_EDIT:
            cleared = int(a.get("cleared_input_tokens") or 0)
            pre = 100_000 + cleared
            log.add(ev.ts_ms, "session.truncation", {
                "tokenLimit": 128_000, "preTruncationTokensInMessages": pre,
                "postTruncationTokensInMessages": pre - cleared,
                "tokensRemovedDuringTruncation": cleared, "preTruncationMessagesLength": 40,
                "postTruncationMessagesLength": 30, "messagesRemovedDuringTruncation": 10,
                "performedBy": "BasicTruncator"})
        elif ev.kind is LaneEventKind.COST_STATE and a.get("reporter") == "copilot.cli.checkpoint":
            log.add(ev.ts_ms, "session.usage_checkpoint", {
                "totalNanoAiu": int(a.get("reported_total_nano") or 0) * 100,
                "totalPremiumRequests": 0,
                "modelCacheState": [{"modelId": first.model_raw, "cacheTtlSeconds": ttl,
                                     "cacheExpiresAt": _iso(ev.ts_ms + ttl * 1000, millis=True)}]})
        elif ev.kind is LaneEventKind.API_ERROR:
            log.add(ev.ts_ms, "session.error", {
                "errorType": str(a.get("error_type") or "server_error"),
                "message": f"The request failed {CANARY}",
                **({"statusCode": a["status"]} if isinstance(a.get("status"), int) else {})})
    metrics: dict[str, dict[str, Any]] = {}
    for c in calls:
        if not (c.usage.total_input or c.usage.output):
            continue
        m = metrics.setdefault(c.model_raw, {"requests": {"count": 0, "cost": 0},
                                             "usage": {"inputTokens": 0, "outputTokens": 0,
                                                       "cacheReadTokens": 0,
                                                       "cacheWriteTokens": 0},
                                             "totalNanoAiu": 0})
        m["requests"]["count"] += 1
        m["usage"]["inputTokens"] += c.usage.total_input
        m["usage"]["outputTokens"] += c.usage.output
        m["usage"]["cacheReadTokens"] += c.usage.cache_read
        m["usage"]["cacheWriteTokens"] += c.usage.cache_write
        m["totalNanoAiu"] += c.nano_aiu or 0
    total_aiu = sum((c.nano_aiu or 0) + sum((i.provider_reported_cost_nano or 0) * 100
                                            for i in c.compactions) for c in calls)
    last = calls[-1]
    shutdown: dict[str, Any] = {
        "shutdownType": "routine", "sessionStartTime": max(first.ts - 2_000, 0),
        "totalApiDurationMs": sum(c.end - c.ts for c in calls), "totalNanoAiu": total_aiu,
        "totalPremiumRequests": 0,
        "codeChanges": {"filesModified": [f"src/handler_{CANARY}.py"], "linesAdded": 10,
                        "linesRemoved": 2},
        "modelMetrics": dict(sorted(metrics.items())), "currentModel": last.model_raw}
    log.add(max(c.end for c in calls) + 5_000, "session.shutdown", shutdown)
    return log


def _cli_calls(calls: Iterable[_Call]) -> list[_Call]:
    return [c for c in calls if c.product == "copilot_cli"]


def _vscode_calls(calls: Iterable[_Call]) -> list[_Call]:
    return [c for c in calls if c.product == "copilot_vscode"]


def _overlap_keys(calls: Sequence[_Call], n: int | Iterable[str]) -> list[str]:
    """Session keys of the VS Code conversations that also run as ``~/.copilot`` sessions of the
    in-VS Code CLI agent: *n* given keys, or the first *n* VS Code sessions."""
    keys = sorted({c.session_key for c in _vscode_calls(calls)})
    if isinstance(n, int):
        return keys[:max(n, 0)]
    wanted = set(n)
    return [k for k in keys if k in wanted]


def write_copilot_home(records: Any, out_dir: Path, *, quirks: bool = True,
                       overlap_sessions: int | Iterable[str] = 3, store: bool = True
                       ) -> dict[str, Path]:
    """One ``~/.copilot`` tree per CLI user under ``local/copilot_home/u-<hex>/``:
    ``session-state/<session id>/events.jsonl`` per Copilot CLI session (``agent_product ==
    "copilot_cli"``) — ``session.start`` (cwd / branch / repository context, context tier,
    credit limit), ``user.message``, ``assistant.message`` (output tokens, request ids),
    ``tool.execution_*``, ``session.compaction_complete`` (with ``compactionTokensUsed`` and
    ``copilotUsage.tokenDetails`` whose Σ tokenCount × costPerBatch / batchSize equals
    ``totalNanoAiu``), model changes, truncations, usage checkpoints, errors and
    ``session.shutdown`` (``modelMetrics`` with cache-inclusive input) — the canary in every
    content field, and with *quirks* one torn-then-retried line; plus the overlapping VS Code
    conversations (*overlap_sessions*) as CLI sessions, and with *store* a ``session-store.db``
    (third-party DDL) per tree."""
    recs = _collect(records)
    calls = _calls(recs)
    out_dir = Path(out_dir)
    written: dict[str, Path] = {}
    cli = _cli_calls(calls)
    overlap = set(_overlap_keys(calls, overlap_sessions))
    sessions = _group(cli, lambda c: c.session_key)
    for key, group in _group([c for c in calls if c.session_key in overlap
                              and c.product == "copilot_vscode"],
                             lambda c: c.session_key).items():
        sessions.setdefault(key, group)
    by_owner = _group([g[0] for g in sessions.values()], lambda c: _owner_dir(c.principal))
    concat_done = not quirks
    for owner in sorted(by_owner):
        home = out_dir / "local" / "copilot_home" / owner
        owned = sorted(c.session_key for c in by_owner[owner])
        for key in owned:
            group = sessions[key]
            raw = raw_session_id(key)
            lines = _cli_log(recs, raw, group).lines(concatenate=not concat_done)
            concat_done = True
            path = _put(home / "session-state" / raw / "events.jsonl", "\n".join(lines) + "\n")
            written[_rel(out_dir, path)] = path
        if store:
            path = _write_store(home / "session-store.db",
                                [(raw_session_id(k), sessions[k]) for k in owned])
            written[_rel(out_dir, path)] = path
    return written


def _write_store(path: Path, sessions: Sequence[tuple[str, Sequence[_Call]]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path), isolation_level=None)
    try:
        con.executescript(SESSION_STORE_DDL)
        con.execute("BEGIN")
        for raw, calls in sessions:
            first = calls[0]
            repo = first.req.attribution.repo
            con.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)", (
                raw, f"/home/dev/src/project {CANARY}", repo_name(repo) if repo else None,
                f"feature/{CANARY}", f"Fixed the flaky test {CANARY}",
                _iso(first.ts, millis=True), _iso(calls[-1].end, millis=True)))
            for c in calls:
                if not (c.usage.total_input or c.usage.output):
                    continue
                con.execute(
                    "INSERT INTO assistant_usage_events (session_id, turn_index, model, "
                    "copilot_usage_model, input_tokens, output_tokens, cache_read_tokens, "
                    "cache_write_tokens, reasoning_tokens, total_nano_aiu, duration_ms, "
                    "initiator, request_multiplier, created_at) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (raw, c.req.seq, c.model_raw, c.model, c.usage.total_input, c.usage.output,
                     c.usage.cache_read, c.usage.cache_write, c.usage.output_reasoning,
                     c.nano_aiu, c.end - c.ts, "user" if c.req.seq == 0 else "agent", 1.0,
                     _iso(c.ts, millis=True)))
        con.execute("COMMIT")
    finally:
        con.close()
    return path


# ---------------------------------------------------------------------------------------------
# OTel: spans of Copilot calls (VS Code OTel-JS dumps, OTLP/JSON, CLI envelopes)
# ---------------------------------------------------------------------------------------------


@dataclass
class _Span:
    trace_id: str
    span_id: str
    parent: str | None
    name: str
    start: int
    end: int
    attrs: dict[str, Any]
    events: list[tuple[str, int, dict[str, Any]]] = field(default_factory=list)


def _content_attrs(seed: str) -> dict[str, Any]:
    return {
        "gen_ai.input.messages": json.dumps(
            [{"role": "user", "parts": [{"type": "text", "content": f"Fix the bug {CANARY}"}]}]),
        "gen_ai.output.messages": json.dumps(
            [{"role": "assistant", "parts": [{"type": "text", "content": f"Done {CANARY}"}],
              "finish_reason": "stop"}]),
        "gen_ai.system_instructions": json.dumps([{"type": "text",
                                                   "content": f"You are Copilot {CANARY}"}]),
        "gen_ai.tool.definitions": json.dumps([{"type": "function", "name": "runInTerminal",
                                                "description": f"Run a command {CANARY}"}]),
        "copilot_chat.user_request": f"please fix {seed[:6]} {CANARY}",
    }


def _api_type(model: str) -> str:
    if model.startswith("claude"):
        return "messages"
    return "responses" if model.startswith("gpt-5") else "chat_completions"


def _chat_attrs(c: _Call, raw: str, *, cli: bool, synthesized: bool = False) -> dict[str, Any]:
    u = c.usage
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": "chat", "gen_ai.provider.name": "github",
        "gen_ai.request.model": "auto" if c.routing == "auto" else c.model_raw,
        "gen_ai.response.model": c.model_raw, "gen_ai.conversation.id": raw,
        "gen_ai.usage.input_tokens": u.total_input, "gen_ai.usage.output_tokens": u.output,
        "gen_ai.usage.cache_read.input_tokens": u.cache_read}
    if not synthesized:
        attrs["gen_ai.response.id"] = c.response_id
        attrs["gen_ai.usage.cache_creation.input_tokens"] = u.cache_write
    if u.output_reasoning is not None:
        attrs["gen_ai.usage.reasoning.output_tokens"] = u.output_reasoning
        attrs["gen_ai.usage.reasoning_tokens"] = u.output_reasoning
    if c.nano_aiu is not None:
        attrs["github.copilot.nano_aiu" if cli else "copilot_chat.copilot_usage_nano_aiu"] = (
            c.nano_aiu)
    if cli:
        attrs["github.copilot.cost"] = 1
        attrs["github.copilot.interaction_id"] = _uuid(f"interaction:{c.req.request_id}")
    else:
        if c.context_tier is not None:
            attrs["copilot_chat.request.max_prompt_tokens"] = (
                1_000_000 if c.context_tier == "long_context" else 200_000)
        attrs.update({"copilot_chat.turn.index": c.req.seq,
                      "copilot_chat.api_type": _api_type(c.model),
                      "copilot_chat.endpoint_type": "capi", "copilot_chat.location": "panel",
                      "copilot_chat.chat_session_id": raw,
                      "copilot_chat.time_to_first_token": c.ttft})
    if synthesized:
        attrs["gen_ai.agent.name"] = "Copilot CLI"
    attrs.update(_content_attrs(c.req.request_id))
    return attrs


def _session_spans(raw: str, calls: Sequence[_Call], *, cli: bool,
                   synthesized: bool = False) -> list[_Span]:
    """An ``invoke_agent`` root (nano-AIU total), per subagent a nested ``invoke_agent``, one
    ``chat`` span per call and an ``execute_tool`` span for every other call."""
    trace = _hex(f"trace:{raw}", 32)
    root_id = _hex(f"span:{raw}:root", 16)
    start = min(c.ts for c in calls) - 50
    end = max(c.end for c in calls) + 1_000
    known = [c.nano_aiu for c in calls if c.nano_aiu is not None]
    root_attrs: dict[str, Any] = {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": "GitHub Copilot CLI" if cli else "GitHub Copilot Chat",
        "gen_ai.conversation.id": raw,
        "gen_ai.usage.input_tokens": sum(c.usage.total_input for c in calls),
        "gen_ai.usage.output_tokens": sum(c.usage.output for c in calls),
        "copilot_chat.user_request" if not cli else "gen_ai.input.messages": (
            f"please fix the build {CANARY}")}
    if known:
        root_attrs["github.copilot.nano_aiu" if cli else "copilot_chat.copilot_usage_nano_aiu"] = (
            sum(known))
    if not cli:
        root_attrs["copilot_chat.chat_session_id"] = raw
    spans = [_Span(trace, root_id, None, "invoke_agent GitHub Copilot", max(start, 0), end,
                   root_attrs)]
    sub_roots: dict[str, str] = {}
    for c in calls:
        parent = root_id
        if c.agent_id:
            if c.agent_id not in sub_roots:
                sid = _hex(f"span:{raw}:agent:{c.agent_id}", 16)
                sub_roots[c.agent_id] = sid
                spans.append(_Span(trace, sid, root_id, f"invoke_agent subagent-{c.agent_id}",
                                   c.ts, max(x.end for x in calls if x.agent_id == c.agent_id),
                                   {"gen_ai.operation.name": "invoke_agent",
                                    "gen_ai.agent.name": f"subagent-{c.agent_id}",
                                    "gen_ai.agent.id": c.agent_id,
                                    "gen_ai.conversation.id": raw}))
            parent = sub_roots[c.agent_id]
        sid = _hex(f"span:{raw}:chat:{c.req.request_id}", 16)
        chat = _Span(trace, sid, parent, f"chat {c.model_raw}", c.ts, c.end,
                     _chat_attrs(c, raw, cli=cli))
        chat.events.append(("gen_ai.choice", c.end, {"content": f"Done {CANARY}"}))
        spans.append(chat)
        if synthesized:
            spans.append(_Span(trace, _hex(f"span:{raw}:synth:{c.req.request_id}", 16), root_id,
                               f"chat {c.model_raw}", c.ts, c.end,
                               _chat_attrs(c, raw, cli=False, synthesized=True)))
        if c.req.seq % 2 == 0:
            tid = _hex(f"span:{raw}:tool:{c.req.request_id}", 16)
            spans.append(_Span(trace, tid, parent, "execute_tool runInTerminal", c.end + 5,
                               c.end + 800, {
                                   "gen_ai.operation.name": "execute_tool",
                                   "gen_ai.tool.name": "runInTerminal",
                                   "gen_ai.tool.type": "function",
                                   "gen_ai.tool.call.id": f"call_{_hex(tid, 12)}",
                                   "gen_ai.tool.call.arguments": json.dumps(
                                       {"command": f"npm test {CANARY}"}),
                                   "gen_ai.tool.call.result": f"All tests passed {CANARY}",
                                   "github.copilot.tool.parameters.command":
                                       f"npm test {CANARY}"}))
    return spans


def _otel_js(span: _Span, resource: Mapping[str, Any], scope: str) -> dict[str, Any]:
    """``readableSpanToJson`` of VS Code's ``fileExporters.ts``."""
    doc: dict[str, Any] = {"traceId": span.trace_id, "spanId": span.span_id, "traceFlags": 1,
                           "isRemote": False}
    if span.parent:
        doc["parentSpanContext"] = {"traceId": span.trace_id, "spanId": span.parent,
                                    "traceFlags": 1, "isRemote": False}
    doc.update({
        "name": span.name, "kind": 2 if span.attrs.get("gen_ai.operation.name") == "chat" else 0,
        "startTime": _hr(span.start), "endTime": _hr(span.end),
        "duration": _hr(span.end - span.start), "ended": True, "attributes": span.attrs,
        "status": {"code": 0},
        "events": [{"name": n, "attributes": a, "time": _hr(t), "droppedAttributesCount": 0}
                   for n, t, a in span.events],
        "links": [], "resource": {"attributes": dict(resource)},
        "instrumentationScope": {"name": scope, "version": "0.35.3"},
        "droppedAttributesCount": 0, "droppedEventsCount": 0, "droppedLinksCount": 0})
    return doc


def _otlp_value(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    return {"stringValue": str(value)}


def _otlp_attrs(attrs: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [{"key": k, "value": _otlp_value(v)} for k, v in attrs.items()]


def _otlp_span(span: _Span) -> dict[str, Any]:
    doc: dict[str, Any] = {"traceId": span.trace_id, "spanId": span.span_id}
    if span.parent:
        doc["parentSpanId"] = span.parent
    doc.update({"name": span.name, "kind": 3 if span.name.startswith("chat") else 1,
                "startTimeUnixNano": str(span.start * 1_000_000),
                "endTimeUnixNano": str(span.end * 1_000_000),
                "attributes": _otlp_attrs(span.attrs),
                "events": [{"timeUnixNano": str(t * 1_000_000), "name": n,
                            "attributes": _otlp_attrs(a)} for n, t, a in span.events],
                "status": {"code": 1}})
    return doc


def _claude_spans(recs: _Recs) -> list[_Span]:
    """``claude_code.llm_request`` spans of the input's Claude Code requests, else two fixed
    ones (a Claude Code resource sharing the collector file with Copilot)."""
    rows = []
    for req in recs.requests:
        if req.attribution.agent_product != "claude_code":
            continue
        si = req.serving_inference
        if si is None:
            continue
        rows.append((req.session_key, req.ts_start_ms, si.pricing.model_raw or si.pricing.model,
                     si.usage, req.request_id))
    if not rows:
        base = _day_ms("2026-09-10") + 9 * 3_600_000
        rows = [("claude-session-a", base + i * 60_000, "claude-opus-5-5",
                 UsageBuckets(uncached_input=12 + i, cache_read=20_000 * i,
                              cache_write_unknown=3_000, output=800 + i),
                 f"claude-request-{i}") for i in range(2)]
    spans = []
    for sess, ts, model, u, rid in rows:
        trace = _hex(f"cc-trace:{sess}", 32)
        spans.append(_Span(trace, _hex(f"cc-span:{rid}", 16), None, "claude_code.llm_request",
                           ts, ts + 4_000, {
                               "model": model, "request_id": f"req_{_hex(rid, 24)}",
                               "session.id": _uuid(f"cc:{sess}"),
                               "query_source": "repl_main_thread",
                               "input_tokens": u.uncached_input, "output_tokens": u.output,
                               "cache_read_tokens": u.cache_read,
                               "cache_creation_tokens": u.cache_write, "duration_ms": 4_000,
                               "ttft_ms": 900}))
    return spans


#: ``write_otel_file`` dialects.
OTEL_DIALECTS = ("vscode", "otlp", "cli")


def write_otel_file(records: Any, out_dir: Path, *, dialect: str = "otlp",
                    identity: Identity | None = None, name: str | None = None
                    ) -> dict[str, Path]:
    """One OTel file of the input's Copilot calls:

    * ``"vscode"`` — ``local/otel/vscode_otel_dump.jsonl``, VS Code OTel-JS JSON lines
      (``readableSpanToJson``; resource ``service.name = copilot-chat``) of the VS Code calls;
    * ``"otlp"`` — ``local/otel/otlp_mixed.jsonl``, one OTLP/JSON ``ExportTraceServiceRequest``
      line per CLI user mixing a Claude Code resource (``claude_code.llm_request`` spans) and a
      Copilot CLI resource (``service.name = github-copilot``, ``user.name`` = login for the team
      map; int64 values as strings);
    * ``"cli"`` — ``local/otel/copilot_cli_otel.jsonl``, Copilot CLI file-exporter envelopes
      (``type: "span"``, ``spanContext``, ``hrTime`` pairs; experimental, docs-derived).

    Chat spans carry cache-inclusive input, cache read / creation, nano-AIU and the canary in the
    content attributes; ``invoke_agent`` roots carry the conversation's nano-AIU total."""
    if dialect not in OTEL_DIALECTS:
        raise UsageError(f"write_otel_file: unknown dialect {dialect!r}")
    recs = _collect(records)
    ident = _ident(recs, identity)
    calls = _calls(recs)
    out_dir = Path(out_dir)
    docs: list[dict[str, Any]] = []
    if dialect == "vscode":
        resource = {"service.name": "copilot-chat", "service.version": "0.35.3"}
        for key, group in sorted(_group(_vscode_calls(calls), lambda c: c.session_key).items()):
            for span in _session_spans(raw_session_id(key), group, cli=False):
                docs.append(_otel_js(span, resource, "copilot-chat"))
        rel = name or "vscode_otel_dump.jsonl"
    elif dialect == "cli":
        for key, group in sorted(_group(_cli_calls(calls), lambda c: c.session_key).items()):
            for span in _session_spans(raw_session_id(key), group, cli=True):
                doc: dict[str, Any] = {"type": "span", "name": span.name,
                                       "spanContext": {"traceId": span.trace_id,
                                                       "spanId": span.span_id, "traceFlags": 1}}
                if span.parent:
                    doc["parentSpanId"] = span.parent
                doc.update({"kind": 2 if span.name.startswith("chat") else 0,
                            "startTime": {"_hrTime": _hr(span.start)},
                            "endTime": {"_hrTime": _hr(span.end)},
                            "attributes": span.attrs,
                            "events": [{"name": n, "attributes": a, "hrTime": _hr(t)}
                                       for n, t, a in span.events],
                            "resource": {"attributes": {"service.name": "github-copilot",
                                                        "service.version": _COPILOT_VERSION}},
                            "instrumentationScope": {"name": "github.copilot"}})
                docs.append(doc)
        rel = name or "copilot_cli_otel.jsonl"
    else:
        claude = _claude_spans(recs)
        by_owner = _group(_cli_calls(calls), lambda c: c.principal or "")
        for n, owner in enumerate(sorted(by_owner)):
            copilot_spans = []
            for key, group in sorted(_group(by_owner[owner], lambda c: c.session_key).items()):
                copilot_spans.extend(_session_spans(raw_session_id(key), group, cli=True))
            resource_spans = []
            if n == 0:
                resource_spans.append({
                    "resource": {"attributes": _otlp_attrs({
                        "service.name": "claude-code", "service.version": "2.1.270"})},
                    "scopeSpans": [{"scope": {"name": "com.anthropic.claude_code"},
                                    "spans": [_otlp_span(s) for s in claude]}]})
            if copilot_spans:
                attrs: dict[str, Any] = {"service.name": "github-copilot",
                                         "service.version": _COPILOT_VERSION}
                if owner:
                    attrs["user.name"] = ident.login(owner)
                resource_spans.append({
                    "resource": {"attributes": _otlp_attrs(attrs)},
                    "scopeSpans": [{"scope": {"name": "github.copilot"},
                                    "spans": [_otlp_span(s) for s in copilot_spans]}]})
            docs.append({"resourceSpans": resource_spans})
        rel = name or "otlp_mixed.jsonl"
    if not docs:
        return {}
    path = out_dir / "local" / "otel" / rel
    write_jsonl(path, docs)
    return {_rel(out_dir, path): path}


# ---------------------------------------------------------------------------------------------
# VS Code agent-traces.db (otelSqliteStore.ts DDL) and the OTel outfile
# ---------------------------------------------------------------------------------------------

#: The ``agent-traces.db`` DDL of VS Code's ``otelSqliteStore.ts`` (``SCHEMA_VERSION = 1``),
#: verbatim apart from whitespace.
VSCODE_TRACES_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
INSERT OR REPLACE INTO schema_version (version) VALUES (1);

CREATE TABLE IF NOT EXISTS spans (
    span_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, parent_span_id TEXT,
    name TEXT NOT NULL, start_time_ms INTEGER NOT NULL, end_time_ms INTEGER NOT NULL,
    status_code INTEGER NOT NULL DEFAULT 0, status_message TEXT,
    operation_name TEXT, provider_name TEXT, agent_name TEXT, conversation_id TEXT,
    request_model TEXT, response_model TEXT,
    input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER, reasoning_tokens INTEGER,
    tool_name TEXT, tool_call_id TEXT, tool_type TEXT,
    chat_session_id TEXT, turn_index INTEGER, ttft_ms REAL
);

CREATE TABLE IF NOT EXISTS span_attributes (
    span_id TEXT NOT NULL REFERENCES spans(span_id) ON DELETE CASCADE,
    key TEXT NOT NULL, value TEXT,
    PRIMARY KEY (span_id, key)
);

CREATE TABLE IF NOT EXISTS span_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    span_id TEXT NOT NULL REFERENCES spans(span_id) ON DELETE CASCADE,
    name TEXT NOT NULL, timestamp_ms INTEGER NOT NULL, attributes TEXT
);

CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_conversation ON spans(conversation_id);
CREATE INDEX IF NOT EXISTS idx_spans_chat_session ON spans(chat_session_id);
CREATE INDEX IF NOT EXISTS idx_spans_operation ON spans(operation_name);
CREATE INDEX IF NOT EXISTS idx_spans_start_time ON spans(start_time_ms);
CREATE INDEX IF NOT EXISTS idx_span_events_span ON span_events(span_id);

CREATE VIEW IF NOT EXISTS sessions AS
SELECT
    COALESCE(conversation_id, chat_session_id) AS session_id,
    agent_name,
    response_model AS model,
    MIN(start_time_ms) AS started_at,
    MAX(end_time_ms) AS ended_at,
    MAX(end_time_ms) - MIN(start_time_ms) AS duration_ms,
    COUNT(*) AS span_count,
    SUM(CASE WHEN operation_name = 'chat' THEN 1 ELSE 0 END) AS llm_calls,
    SUM(CASE WHEN operation_name = 'execute_tool' THEN 1 ELSE 0 END) AS tool_calls,
    SUM(CASE WHEN operation_name = 'chat' THEN input_tokens ELSE 0 END) AS total_input_tokens,
    SUM(CASE WHEN operation_name = 'chat' THEN output_tokens ELSE 0 END) AS total_output_tokens,
    SUM(CASE WHEN operation_name = 'chat' THEN cached_tokens ELSE 0 END) AS total_cached_tokens
FROM spans
WHERE COALESCE(conversation_id, chat_session_id) IS NOT NULL
GROUP BY COALESCE(conversation_id, chat_session_id);
"""


def _attr_text(value: Any) -> str:
    """``String(value)`` as ``insertSpan`` stores attribute values (arrays as JSON)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"))
    return str(value)


def _span_row(span: _Span) -> tuple[Any, ...]:
    a = span.attrs

    def col(key: str) -> Any:
        value = a.get(key)
        return (1 if value else 0) if isinstance(value, bool) else value

    return (span.span_id, span.trace_id, span.parent, span.name, span.start, span.end, 0, None,
            col("gen_ai.operation.name"), col("gen_ai.provider.name"), col("gen_ai.agent.name"),
            col("gen_ai.conversation.id"), col("gen_ai.request.model"),
            col("gen_ai.response.model"), col("gen_ai.usage.input_tokens"),
            col("gen_ai.usage.output_tokens"), col("gen_ai.usage.cache_read.input_tokens"),
            col("gen_ai.usage.reasoning_tokens"), col("gen_ai.tool.name"),
            col("gen_ai.tool.call.id"), col("gen_ai.tool.type"),
            col("copilot_chat.chat_session_id"), col("copilot_chat.turn.index"),
            col("copilot_chat.time_to_first_token"))


def _write_traces_db(path: Path, spans: Sequence[_Span]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    con = sqlite3.connect(str(path), isolation_level=None)
    try:
        con.executescript(VSCODE_TRACES_DDL)
        con.execute("BEGIN")
        for span in sorted(spans, key=lambda s: (s.start, s.span_id)):
            con.execute("INSERT OR REPLACE INTO spans (span_id, trace_id, parent_span_id, name, "
                        "start_time_ms, end_time_ms, status_code, status_message, operation_name, "
                        "provider_name, agent_name, conversation_id, request_model, "
                        "response_model, input_tokens, output_tokens, cached_tokens, "
                        "reasoning_tokens, tool_name, tool_call_id, tool_type, chat_session_id, "
                        "turn_index, ttft_ms) VALUES "
                        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        _span_row(span))
            for key, value in span.attrs.items():
                con.execute("INSERT OR REPLACE INTO span_attributes (span_id, key, value) "
                            "VALUES (?, ?, ?)", (span.span_id, key, _attr_text(value)))
            for name, ts, attrs in span.events:
                con.execute("INSERT INTO span_events (span_id, name, timestamp_ms, attributes) "
                            "VALUES (?, ?, ?, ?)", (span.span_id, name, ts,
                                                    json.dumps(attrs, separators=(",", ":"))))
        con.execute("COMMIT")
    finally:
        con.close()
    return path


def write_vscode_traces_db(records: Any, out_dir: Path, *,
                           overlap_sessions: int | Iterable[str] = 3) -> dict[str, Path]:
    """``local/vscode/u-<hex>/agent-traces.db`` per VS Code developer (``agent_product ==
    "copilot_vscode"``), built from the ``otelSqliteStore.ts`` DDL: typed ``spans`` rows and
    every attribute in ``span_attributes`` (``String(value)``) — allowlisted usage keys next to
    content keys carrying the canary — and ``span_events`` rows with the canary. Per conversation
    an ``invoke_agent`` root, ``chat`` spans (cache-inclusive input, cache read / creation,
    ``copilot_chat.copilot_usage_nano_aiu``) and ``execute_tool`` spans. The *overlap_sessions*
    conversations also get synthesized ``chat`` spans (no response id, no cache creation), the
    duplicates VS Code writes for the in-editor CLI agent."""
    recs = _collect(records)
    calls = _calls(recs)
    overlap = set(_overlap_keys(calls, overlap_sessions))
    out_dir = Path(out_dir)
    written: dict[str, Path] = {}
    by_owner = _group(_vscode_calls(calls), lambda c: _owner_dir(c.principal))
    for owner in sorted(by_owner):
        spans: list[_Span] = []
        for key, group in sorted(_group(by_owner[owner], lambda c: c.session_key).items()):
            spans.extend(_session_spans(raw_session_id(key), group, cli=False,
                                        synthesized=key in overlap))
        path = _write_traces_db(out_dir / "local" / "vscode" / owner / "agent-traces.db", spans)
        written[_rel(out_dir, path)] = path
    return written


def write_vscode_outfile(records: Any, out_dir: Path, *, developers: int = 3) -> dict[str, Path]:
    """``local/vscode/u-<hex>/copilot-otel.jsonl``: the VS Code OTel outfile
    (``github.copilot.chat.otel.outfile``, OTel-JS JSON lines) of the first *developers* VS Code
    developers — the same calls as their ``agent-traces.db`` (a view)."""
    recs = _collect(records)
    calls = _vscode_calls(_calls(recs))
    out_dir = Path(out_dir)
    written: dict[str, Path] = {}
    resource = {"service.name": "copilot-chat", "service.version": "0.35.3"}
    by_owner = _group(calls, lambda c: _owner_dir(c.principal))
    for owner in sorted(by_owner)[:max(developers, 0)]:
        docs = []
        for key, group in sorted(_group(by_owner[owner], lambda c: c.session_key).items()):
            docs.extend(_otel_js(s, resource, "copilot-chat")
                        for s in _session_spans(raw_session_id(key), group, cli=False))
        path = out_dir / "local" / "vscode" / owner / "copilot-otel.jsonl"
        _put_lines(path, docs)
        written[_rel(out_dir, path)] = path
    return written


# ---------------------------------------------------------------------------------------------
# gh-aw token-usage.jsonl (addendum §5.14)
# ---------------------------------------------------------------------------------------------

GH_AW_SCHEMA = "token-usage/v0.28.7"


def _gh_aw_path(model: str) -> str:
    if model.startswith("claude"):
        return "/v1/messages"
    return "/responses" if model.startswith("gpt-5") else "/chat/completions"


def write_gh_aw_token_usage(records: Any, out_dir: Path, *, quirks: bool = True
                            ) -> dict[str, Path]:
    """``local/gh_aw/run-<id>/token-usage.jsonl`` per gh-aw run (one session of
    ``agent_product == "copilot_gh_aw"``): one ``token_usage`` line per call in the
    ``token-usage/v0.28.7`` field order (GitHub's fixture), exclusive input with
    ``input_tokens_include_cache: false``, ``ai_credits_this_response`` = the call's provider
    estimate in credits and the running ``ai_credits_total``; with *quirks* the first file also
    has a non-Copilot provider line (counted and skipped by the adapter)."""
    recs = _collect(records)
    calls = [c for c in _calls(recs) if c.product == "copilot_gh_aw"]
    out_dir = Path(out_dir)
    written: dict[str, Path] = {}
    for n, (key, group) in enumerate(sorted(_group(calls, lambda c: c.session_key).items())):
        total = 0
        known = False
        lines = []
        for i, c in enumerate(group):
            u = c.usage
            line: dict[str, Any] = {
                "_schema": GH_AW_SCHEMA, "timestamp": _iso(c.end, millis=True),
                "event": "token_usage", "request_id": c.request_id, "provider": "copilot",
                "model": c.model_raw, "path": _gh_aw_path(c.model), "status": c.status,
                "streaming": True, "input_tokens": u.uncached_input, "output_tokens": u.output,
                "cache_read_tokens": u.cache_read, "cache_write_tokens": u.cache_write}
            if u.output_reasoning:
                line["reasoning_tokens"] = u.output_reasoning
            line.update({"duration_ms": c.end - c.ts, "response_bytes": 4_096 + 4 * u.output,
                         "x_initiator": "user" if i == 0 else "agent"})
            if c.nano_usd is not None:
                known = True
                total += c.nano_usd
                line["ai_credits_this_response"] = _dec(nano_to_credits_str(c.nano_usd))
            if known:
                line["ai_credits_total"] = _dec(nano_to_credits_str(total))
            line.update({"ai_credits_pricing_source": "models.dev",
                         "ai_credits_pricing_tier": "default",
                         "ai_credits_accounting_policy": "concrete_model",
                         "ai_credits_fallback_pricing_used": False,
                         "input_tokens_include_cache": False})
            lines.append(line)
        if quirks and n == 0:
            last = group[-1]
            lines.append({"_schema": GH_AW_SCHEMA, "timestamp": _iso(last.end + 2_000, millis=True),
                          "event": "token_usage", "request_id": _uuid(f"foreign:{key}"),
                          "provider": "openai", "model": "gpt-4o-mini-2024-07-18",
                          "path": "/chat/completions", "status": 200, "streaming": True,
                          "input_tokens": 19_288, "output_tokens": 35, "cache_read_tokens": 0,
                          "cache_write_tokens": 0, "duration_ms": 2_242, "response_bytes": 8_642,
                          "x_initiator": "agent"})
        run = f"run-{int(_hex(key, 8), 16)}"
        path = _put_lines(out_dir / "local" / "gh_aw" / run / "token-usage.jsonl", lines)
        written[_rel(out_dir, path)] = path
    return written


# ---------------------------------------------------------------------------------------------
# the world: every source, the team map and MANIFEST.json
# ---------------------------------------------------------------------------------------------

_UI = "ui_download"
_API = "api_recording"
_ADMIN = "admin_authored"
_LOCAL = "local"
#: Per file-name pattern: (adapter, schema source, schema provenance, path class).
_SCHEMAS: tuple[tuple[str, str, str, str, str], ...] = (
    (r"billing/ai_usage_(report|overlap)_.*\.csv", "github-ai-usage",
     "docs.github.com billing-reports reference (AI usage report, 2026-09-23); quirks from "
     "github/copilot-billing-preview parser.ts and reportAdapters.test.ts", "primary", _UI),
    (r"billing/ai_usage_legacy_pru\.csv", "github-ai-usage",
     "github/copilot-billing-preview legacy premium_request CSV (exceeds_quota)", "primary", _UI),
    (r"billing/(detailed|summarized)_usage_report\.csv", "github-metered-usage",
     "docs.github.com billing-reports reference (detailed / summarized usage report)", "primary",
     _UI),
    (r"billing/billing_api\.jsonl", "github-billing-api",
     "github/rest-api-description ghec.json (ai_credit/usage, usage/summary); CP-PULL envelopes",
     "primary", _API),
    (r"config/.*\.jsonl", "github-copilot-config",
     "ghec.json budgets, budget user-states, cost-centers, GET /orgs/{org}/copilot/billing",
     "primary", _API),
    (r"metrics/.*", "github-copilot-metrics",
     "docs.github.com usage-metrics field reference and example schema; team-level metrics "
     "(teams below 5 seated users omitted)", "primary", _API),
    (r"seats/.*", "github-copilot-seats", "ghec.json GET /orgs/{org}/copilot/billing/seats",
     "primary", _API),
    (r"agents/.*", "github-agent-tasks",
     "ghec.json GET /agents/repos/{owner}/{repo}/tasks; cloud-agent API doc", "primary", _API),
    (r"ui/copilot_activity_report\.csv", "github-copilot-activity-report",
     "docs.github.com copilot/reference/metrics-data (activity report columns)", "primary", _UI),
    (r"ui/copilot_usage_dashboard\.ndjson", "github-copilot-metrics",
     "usage-metrics API 28-day shape (report_start_day / report_end_day); the dashboard export's "
     "identity with it is VERIFY", "docs-derived", _UI),
    (r"admin/answers\.json", "copilot-admin-answers",
     "tokenbill/copilot/handoff_data/admin_answers.template.json", "primary", _ADMIN),
    (r"admin/team_map\.csv", "",
     "tokenbill.copilot.handoff.read_team_map_csv (login,team) plus cost_center, user_id",
     "primary", _ADMIN),
    (r"local/copilot_home/.*/events\.jsonl", "copilot-cli",
     "github/copilot-sdk nodejs/src/generated/session-events.ts @075f027", "primary", _LOCAL),
    (r"local/copilot_home/.*/session-store\.db", "copilot-cli",
     "third-party readers (tokscale, codeburn) of assistant_usage_events; experimental "
     "copilot-store", "third-party", _LOCAL),
    (r"local/vscode/.*/agent-traces\.db", "copilot-vscode-traces",
     "microsoft/vscode extensions/copilot src/platform/otel/node/sqlite/otelSqliteStore.ts "
     "(SCHEMA_VERSION 1)", "primary", _LOCAL),
    (r"local/vscode/.*/copilot-otel\.jsonl", "copilot-otel",
     "microsoft/vscode extensions/copilot src/platform/otel/node/fileExporters.ts "
     "(readableSpanToJson)", "primary", _LOCAL),
    (r"local/otel/vscode_otel_dump\.jsonl", "copilot-otel",
     "microsoft/vscode fileExporters.ts (readableSpanToJson)", "primary", _LOCAL),
    (r"local/otel/otlp_mixed\.jsonl", "otlp+copilot-otel",
     "OTLP/JSON ExportTraceServiceRequest (opentelemetry-proto); Copilot CLI OTel attributes "
     "(CLI command reference); claude_code.llm_request spans", "docs-derived", _LOCAL),
    (r"local/otel/copilot_cli_otel\.jsonl", "copilot-otel",
     "Copilot CLI file exporter (COPILOT_OTEL_FILE_EXPORTER_PATH); envelope shape docs-derived, "
     "experimental copilot-cli-otel-file", "third-party", _LOCAL),
    (r"local/gh_aw/.*/token-usage\.jsonl", "gh-aw-token-usage",
     "github/gh-aw pkg/cli/token_usage_types.go and "
     "actions/setup/js/fixtures/awf-v0.28.7-aic-token-usage.jsonl", "primary", _LOCAL),
)


def _schema_of(rel: str) -> tuple[str, str, str, str]:
    for pattern, adapter, source, prov, klass in _SCHEMAS:
        if re.fullmatch(pattern, rel):
            return adapter, source, prov, klass
    return "", "", "synthetic", _LOCAL


def _rows_of(path: Path) -> int:
    if path.suffix == ".db":
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            table = ("spans" if "agent-traces" in path.name else "assistant_usage_events")
            return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            con.close()
    text = path.read_bytes().decode("utf-8-sig")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    if path.suffix == ".csv":
        return max(len(lines) - 1, 0)
    if path.suffix == ".json":
        return 1
    return len(lines)


def _expected_counts(recs: _Recs) -> dict[str, int]:
    ai = [c for c in recs.cost_lines if c.source_kind == AI_USAGE_SOURCE]
    return {
        "ai_usage_lines": len(ai),
        "ai_usage_days": len({c.date_utc for c in ai}),
        "token_aggregates": sum(1 for a in recs.aggregates if a.source_kind == AI_USAGE_SOURCE),
        "metered_lines": sum(1 for c in recs.cost_lines if c.source_kind == METERED_SOURCE),
        "seat_licenses": sum(1 for x in recs.licenses if x.source_kind == "github.copilot_seats"),
        "seat_holders": len({x.principal for x in _report_licenses(recs)}),
        "activity_days": sum(1 for a in recs.activity if a.source_kind == METRICS_SOURCE),
        "config": sum(1 for c in recs.config if c.kind in ("budget", "budget_users",
                                                            "cost_center", "org_settings")
                      and c.source_kind.startswith("github.")),
        "agent_task_sessions": sum(1 for a in recs.aggregates
                                   if a.source_kind == "github.agent_tasks"),
        "requests": len(recs.requests),
    }


def write_world(records: Any, out_dir: Path, *, conventions: Sequence[str] = ("excl",),
                quirks: bool = True, handoff: str | None = None,
                logins: Mapping[str, str] | None = None, identity: Identity | None = None,
                answers_variant: str | None = None,
                overlap_sessions: int | Iterable[str] = 3) -> dict[str, Path]:
    """Write every Copilot source of *records* under *out_dir* and return ``{relative path:
    Path}`` of every file (``MANIFEST.json`` included).

    *records*: a ``CopilotWorld`` (its ``records``; its ``logins`` / ``team_map`` + key,
    ``report_records`` per convention and ``variants`` are used when present), a mapping or
    object with record-kind attributes, or an iterable of canonical records.

    ``handoff=None`` writes everything: the AI usage report per convention (the first is the
    ``primary`` file, the rest ``variant``), the overlapping exports (``revision_pair``), the
    detailed report, the summarized report and billing REST pages (``cross_check``), config,
    metrics, seats and agent-task pages, the activity report and dashboard export (``view``s of
    seats / metrics), the answers and team map (``admin_input``) and the local sources (CLI trees,
    VS Code databases and outfiles, OTel files, gh-aw runs). ``handoff="api"`` writes what an
    admin records with a token (``copilot pull``: the export CSVs of the first convention, config,
    metrics and seats pages; no agent tasks, no billing REST pages) and ``"ui"`` only what the
    GitHub UI downloads (AI usage and detailed CSVs, activity report, dashboard export) plus the
    admin's answers and team map; file names are equal on both paths. ``MANIFEST.json`` lists each
    file with ``provenance: "synthetic"``, its ``schema_source``, role, path class, adapter,
    row count and digest."""
    if handoff not in (None, *HANDOFF_MODES):
        raise UsageError(f"write_world: handoff must be one of {HANDOFF_MODES} or None")
    recs = _collect(records)
    ident = identity or build_identity(recs, logins=logins)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    convs = tuple(conventions)[:1] if handoff else tuple(conventions)
    if not convs:
        raise UsageError("write_world: at least one convention is required")
    report_sets = _world_attr(recs, "report_records", "report_sets", "convention_records")
    variants = _world_attr(recs, "variants")
    if answers_variant is None and variants:
        answers_variant = next((v for v in ANSWER_VARIANTS if v in tuple(variants)), None)
    roles: dict[str, str] = {}
    files: dict[str, Path] = {}

    def take(written: Mapping[str, Path], role: str) -> None:
        for rel, path in written.items():
            files[rel] = path
            roles[rel] = role

    ai = write_ai_usage_csv(recs, out_dir, conventions=convs, quirks=quirks, identity=ident,
                            revision_pair=quirks and handoff is None,
                            report_sets=report_sets if isinstance(report_sets, Mapping) else None)
    for rel, path in ai.items():
        role = "primary"
        if "overlap" in rel:
            role = "revision_pair"
        elif rel.startswith("billing/ai_usage_report_") and rel != (
                f"billing/ai_usage_report_{convs[0]}.csv"):
            role = "variant"
        take({rel: path}, role)
    metered = write_metered_csv(recs, out_dir, quirks=quirks, identity=ident,
                                summarized=handoff is None)
    for rel, path in metered.items():
        take({rel: path}, "cross_check" if "summarized" in rel else "primary")
    if handoff in (None, "api"):
        take(write_config_pages(recs, out_dir, identity=ident), "primary")
        take(write_metrics_ndjson(recs, out_dir, identity=ident), "primary")
        take(write_seats_pages(recs, out_dir, identity=ident), "primary")
    if handoff is None:
        take(write_billing_pages(recs, out_dir, identity=ident), "cross_check")
        take(write_agent_task_pages(recs, out_dir, identity=ident), "primary")
    if handoff in (None, "ui"):
        role = "primary" if handoff == "ui" else "view"
        take(write_activity_report_csv(recs, out_dir, identity=ident, quirks=quirks), role)
        take(write_dashboard_ndjson(recs, out_dir, identity=ident), role)
        take(write_admin_answers(recs, out_dir, variant=answers_variant, identity=ident),
             "admin_input")
    take(_write_team_map(ident, out_dir), "admin_input")
    if handoff is None:
        calls = _calls(recs)
        overlap = {raw_session_id(k) for k in _overlap_keys(calls, overlap_sessions)}
        home = write_copilot_home(recs, out_dir, quirks=quirks, overlap_sessions=overlap_sessions)
        for rel, path in home.items():
            if rel.endswith("session-store.db"):
                take({rel: path}, "view")
            else:
                take({rel: path}, "overlap" if path.parent.name in overlap else "primary")
        take(write_vscode_traces_db(recs, out_dir, overlap_sessions=overlap_sessions), "primary")
        take(write_vscode_outfile(recs, out_dir), "view")
        take(write_otel_file(recs, out_dir, dialect="otlp", identity=ident), "view")
        take(write_otel_file(recs, out_dir, dialect="cli", identity=ident), "view")
        take(write_gh_aw_token_usage(recs, out_dir, quirks=quirks), "primary")
    entries = []
    for rel in sorted(files):
        path = files[rel]
        adapter, source, prov, klass = _schema_of(rel)
        data = path.read_bytes()
        entry: dict[str, Any] = {
            "path": rel, "adapter": adapter, "role": roles[rel], "path_class": klass,
            "provenance": "synthetic", "schema_provenance": prov, "schema_source": source,
            "acceptance_evidence": False, "rows": _rows_of(path), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}
        m = re.fullmatch(r"billing/ai_usage_report_(\w+)\.csv", rel)
        if m:
            entry["convention"] = m.group(1)
        if rel.startswith("local/copilot_home/"):
            entry["copilot_home"] = "/".join(rel.split("/")[:3])
        entries.append(entry)
    manifest = {
        "schema": MANIFEST_SCHEMA, "provenance": "synthetic", "acceptance_evidence": False,
        "handoff": handoff, "conventions": list(convs), "quirks": quirks,
        "enterprise": ident.enterprise, "answers_variant": answers_variant,
        "revision": revision_info(recs, identity=ident) if quirks and handoff is None else None,
        "expected_counts": _expected_counts(recs), "files": entries}
    path = _put(out_dir / "MANIFEST.json",
                json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    files["MANIFEST.json"] = path
    return dict(sorted(files.items()))
