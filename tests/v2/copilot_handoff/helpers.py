"""Area-local helpers for the CP-HANDOFF tests (imported only by ``tests/v2/copilot_handoff``).

Fake adapters stand in for the sibling Copilot adapters (CP-BILL, CP-ORGDATA), which are built in
parallel: they read tiny synthetic raw files whose identity columns follow the documented GitHub
field names (``username``, ``assignee.login`` / ``assignee.id``, ``user_login`` / ``user_id``) and
pseudonymize exactly as SPEC §5.1 central-ingest requires. ``use_fake_registry`` replaces
``core.registry.BUILTIN_ADAPTERS`` with them plus the two real adapters of this package. Every
login, e-mail and repository name here is synthetic; ``CANARY_LOGIN`` is a member of team alpha.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from tokenbill.adapters.copilot_export import CopilotExportAdapter
from tokenbill.adapters.github_activity_report import ActivityReportAdapter
from tokenbill.core import registry
from tokenbill.core.builders import CANARY_LOGIN, make_ai_usage_row
from tokenbill.core.ids import key_id, pseudonym, stable_id
from tokenbill.core.records import (
    ActivityDay,
    ConfigSnapshot,
    CostLine,
    LicenseSnapshot,
    OutcomeAggregate,
)
from tokenbill.core.types import IngestOptions, IngestResult, SourceInfo

KEY = bytes(range(32))
KEY2 = bytes(range(100, 132))
KID = key_id(KEY)
NOW_MS = 1_790_294_400_000          # 2026-09-25T00:00:00Z
SNAPSHOT = "2026-09-20"
ORG = "acme-org"
TEAMS = {"alpha": 7, "beta": 5, "gamma": 3}
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "copilot_handoff"


def logins(teams: Mapping[str, int] = TEAMS) -> dict[str, str]:
    """login → team; the first alpha member is ``CANARY_LOGIN``."""
    out: dict[str, str] = {}
    for team, n in teams.items():
        for i in range(n):
            login = CANARY_LOGIN if (team == "alpha" and i == 0) else f"dev-{team}-{i:02d}x"
            out[login] = team
    return out


def principal(login: str, key: bytes = KEY) -> str:
    return pseudonym(key, "p", login)


def _source(adapter: str, path: Path, opts: IngestOptions, *, people: bool,
            names: bool = False) -> SourceInfo:
    return SourceInfo(source_id=stable_id("s", adapter, path.name), adapter=adapter,
                      name_hmac="", sha256=stable_id("sha", path.read_bytes().hex()), bytes=0,
                      name_key_id=opts.name_key_id if names else None,
                      principal_key_id=opts.principal_key_id if people else None)


def _result(source: SourceInfo, **records: Any) -> IngestResult:
    base: dict[str, Any] = dict(requests=[], sessions=[], events=[], aggregates=[],
                                cost_lines=[], outcomes=[], quarantined=[], notes=[], stats={},
                                capabilities=frozenset())
    base.update(records)
    return IngestResult(source=source, **base)


def _team(opts: IngestOptions, login: str) -> str | None:
    folded = {k.lower(): v for k, v in opts.team_map}
    return folded.get(login.lower())


# ---------------------------------------------------------------------------------------------
# fake adapters
# ---------------------------------------------------------------------------------------------


def fake_usage_adapter(leak: Callable[[CostLine, dict[str, str]], CostLine] | None = None,
                       name: str = "fake-ai-usage") -> type:
    """A CSV adapter (header ``fake_usage,date,username,organization,model,credits,repository``)
    producing AI usage cost lines + aggregates; *leak* may rewrite each line with raw row values
    (to prove the leak gate)."""

    class FakeUsage:
        capabilities = frozenset({"aggregates", "cost", "copilot_billing"})

        def sniff(self, path: Path, head: bytes) -> bool:
            return head.lstrip(b"\xef\xbb\xbf").startswith(b"fake_usage,")

        def read(self, path: Path, opts: IngestOptions) -> IngestResult:
            rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
            lines, aggs = [], []
            for row in rows:
                who = row["username"].strip()
                repo = row.get("repository") or None
                line, agg = make_ai_usage_row(
                    date_utc=row["date"], model=row["model"], credits=row["credits"],
                    principal=pseudonym(opts.principal_key, "p", who) if who else None,
                    unattributed=not who, organization=row["organization"],
                    team=_team(opts, who) if who else None,
                    repo=pseudonym(opts.name_key, "h", repo) if repo else None,
                    fetched_ms=opts.now_ms)
                if leak is not None:
                    line = leak(line, row)
                lines.append(line)
                aggs.append(agg)
            src = _source(self.name, path, opts, people=True, names=True)
            return _result(src, cost_lines=lines, aggregates=aggs,
                           capabilities=self.capabilities)

    FakeUsage.name = name  # type: ignore[attr-defined]
    return FakeUsage


class FakeSeats:
    """JSON ``{"fake_seats": true, "snapshot_date": …, "seats": [{assignee{login,id},
    organization{login}, plan_type, bucket, assigning_team, created_at,
    pending_cancellation_date}]}`` → licenses (seats API shape)."""

    name = "fake-seats"
    capabilities = frozenset({"licenses"})

    def sniff(self, path: Path, head: bytes) -> bool:
        return b'"fake_seats"' in head[:64]

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        doc = json.loads(path.read_text(encoding="utf-8"))
        lics = []
        for seat in doc["seats"]:
            login = seat["assignee"]["login"]
            lics.append(LicenseSnapshot(
                snapshot_date=doc["snapshot_date"], product="github_copilot",
                plan=seat.get("plan_type", "business"),
                principal=pseudonym(opts.principal_key, "p", login), team=_team(opts, login),
                cost_center=None, org=seat["organization"]["login"],
                seat_created=seat.get("created_at"),
                pending_cancellation=seat.get("pending_cancellation_date"),
                last_activity_bucket=seat.get("bucket", "0-7"), last_activity_surface="vscode",
                last_authenticated_bucket="0-7",
                assigned_via_team=seat.get("assigning_team") is not None,
                fetched_ms=opts.now_ms))
        return _result(_source(self.name, path, opts, people=True), licenses=lics,
                       capabilities=self.capabilities)


class FakeMetrics:
    """NDJSON lines ``{"fake_metrics": 1, "day", "user_login", "user_id", "interactions",
    "ide": {name: n}}`` → activity days, plus one outcome row per (day, team)."""

    name = "fake-metrics"
    capabilities = frozenset({"activity", "outcomes"})

    def sniff(self, path: Path, head: bytes) -> bool:
        return head.startswith(b'{"fake_metrics"')

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        days = []
        per_team: dict[tuple[str, str], int] = {}
        for raw in path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(raw)
            login = rec["user_login"]
            team = _team(opts, login)
            counts = {"interactions": rec["interactions"], "cli_requests": rec.get("cli", 0)}
            counts.update({f"ide:{k}": v for k, v in rec.get("ide", {}).items()})
            days.append(ActivityDay(
                date_utc=rec["day"], product="github_copilot",
                principal=pseudonym(opts.principal_key, "p", login), team=team,
                cost_center=None, reported_cost_nano=None, counts=tuple(sorted(counts.items())),
                flags=("used_chat",), fetched_ms=opts.now_ms))
            if team is not None:
                per_team[(rec["day"], team)] = per_team.get((rec["day"], team), 0) + 1
        outcomes = [OutcomeAggregate(date_utc=d, team=t, n_users=n, sessions=0, commits=0,
                                     pull_requests=0, lines_added=n, lines_removed=0,
                                     edits_accepted=0, edits_rejected=0,
                                     source_kind="github.copilot_metrics")
                    for (d, t), n in sorted(per_team.items()) if n >= opts.k_anonymity]
        return _result(_source(self.name, path, opts, people=True), activity=days,
                       outcomes=outcomes, capabilities=self.capabilities)


def fake_budget_adapter(extra_attr: tuple[str, str] | None = None) -> type:
    """JSON ``{"fake_budgets": [{"id", "team", "amount"}]}`` → budget configuration rows;
    *extra_attr* plants one more attr (e.g. an e-mail in ``sku``) for the leak gate."""

    class FakeBudgets:
        name = "fake-budgets"
        capabilities = frozenset({"config"})

        def sniff(self, path: Path, head: bytes) -> bool:
            return head.startswith(b'{"fake_budgets"')

        def read(self, path: Path, opts: IngestOptions) -> IngestResult:
            doc = json.loads(path.read_text(encoding="utf-8"))
            rows = []
            for b in doc["fake_budgets"]:
                attrs: dict[str, Any] = {"scope": "user", "team": b.get("team"),
                                         "amount_nano": b["amount"] * 1_000_000_000,
                                         "sku": "ai_credits"}
                if extra_attr is not None:
                    attrs[extra_attr[0]] = extra_attr[1]
                rows.append(ConfigSnapshot(snapshot_ms=NOW_MS, source_kind="github.budgets",
                                           kind="budget", entity_id=f"budget:{b['id']}",
                                           attrs=tuple(sorted(attrs.items())),
                                           fetched_ms=opts.now_ms))
            return _result(_source(self.name, path, opts, people=False), config=rows,
                           capabilities=self.capabilities)

    return FakeBudgets


def use_fake_registry(monkeypatch: Any, *extra: type, usage: type | None = None,
                      budgets: type | None = None) -> dict[str, Any]:
    """Replace the registry's built-in adapters with the fakes (plus this package's two real
    adapters) for one test."""
    usage_cls = usage or fake_usage_adapter()
    table: dict[str, Any] = {
        usage_cls.name: usage_cls,  # type: ignore[attr-defined]
        FakeSeats.name: FakeSeats,
        FakeMetrics.name: FakeMetrics,
        "fake-budgets": budgets or fake_budget_adapter(),
        "github-copilot-activity-report": ActivityReportAdapter,
        "copilot-export": CopilotExportAdapter,
    }
    for cls in extra:
        table[cls.name] = cls  # type: ignore[attr-defined]
    monkeypatch.setattr(registry, "BUILTIN_ADAPTERS", table)
    return table


# ---------------------------------------------------------------------------------------------
# a synthetic raw world
# ---------------------------------------------------------------------------------------------


def write_world(root: Path, teams: Mapping[str, int] = TEAMS, *, email_in_usage: bool = False,
                budgets: Sequence[Mapping[str, Any]] = ({"id": "b1", "team": "alpha",
                                                        "amount": 30},)) -> dict[str, Any]:
    """Raw files an admin would hand to ``copilot export``: fake AI usage CSV (2 days per user +
    one unattributed code-review row), fake seats JSON, fake metrics NDJSON, a real-format activity
    report CSV and budgets. Returns ``{"dir", "team_map", "logins"}``."""
    root.mkdir(parents=True, exist_ok=True)
    who = logins(teams)
    usage = ["fake_usage,date,username,organization,model,credits,repository"]
    for i, login in enumerate(sorted(who)):
        repo = f"{ORG}/service-{i % 3}"
        for day in ("2026-09-10", "2026-09-11"):
            usage.append(f"1,{day},{login},{ORG},Claude Opus 5.5,{10 + i},{repo}")
    usage.append(f"1,2026-09-10,,{ORG},Code Review model,5,")
    if email_in_usage:
        usage.append(f"1,2026-09-12,someone@example.com,{ORG},GPT-5.5,1,")
    (root / "usage.csv").write_text("\n".join(usage) + "\n", encoding="utf-8")
    seats = {"fake_seats": True, "snapshot_date": SNAPSHOT, "seats": [
        {"assignee": {"login": login, "id": 1_000_000 + i}, "organization": {"login": ORG},
         "plan_type": "business", "bucket": ("0-7", "8-30", "31-90", "none_90d")[i % 4],
         "assigning_team": None, "created_at": "2026-01-15T10:00:00Z"}
        for i, login in enumerate(sorted(who))]}
    (root / "seats.json").write_text(json.dumps(seats), encoding="utf-8")
    metrics = [json.dumps({"fake_metrics": 1, "day": day, "user_login": login,
                           "user_id": 1_000_000 + i, "interactions": 3 + i,
                           "ide": {"vscode": 2, "intellij": 1}})
               for i, login in enumerate(sorted(who)) for day in ("2026-09-10", "2026-09-11")]
    (root / "metrics.ndjson").write_text("\n".join(metrics) + "\n", encoding="utf-8")
    report = ["report_time,login,last_authenticated_at,last_activity_at,last_surface_used"]
    for i, login in enumerate(sorted(who)):
        report.append(f"2026-09-20T08:00:00Z,{login},2026-09-19T08:00:00Z,"
                      f"2026-09-{10 + i % 9:02d}T08:00:00Z,VS Code 1.10{i}.0")
    (root / "activity_report.csv").write_text("﻿" + "\n".join(report) + "\n",
                                              encoding="utf-8")
    (root / "budgets.json").write_text(json.dumps({"fake_budgets": list(budgets)}),
                                       encoding="utf-8")
    return {"dir": root, "team_map": dict(who), "logins": sorted(who)}


def team_csv(path: Path, team_map: Mapping[str, str]) -> Path:
    lines = ["login,team"] + [f"{login},{team}" for login, team in sorted(team_map.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def export(world: Mapping[str, Any], out: Path, **kw: Any) -> Any:
    """``export_from_files`` over a world with the test key and clock."""
    from tokenbill.copilot.handoff import export_from_files

    args: dict[str, Any] = dict(key=KEY, team_map=world["team_map"], cost_center_map={},
                                since=None, until=None, now_ms=NOW_MS, tool_version="test")
    args.update(kw)
    return export_from_files([world["dir"]], out, **args)


# ---------------------------------------------------------------------------------------------
# direct write_bundle inputs (records built with the core builders)
# ---------------------------------------------------------------------------------------------


def round_trip_results(key: bytes = KEY) -> list[IngestResult]:
    """One ``IngestResult`` per record kind family, every kind present, principals of five alpha
    members (``CANARY_LOGIN`` among them) under *key*; no team below k, so nothing is relabelled."""
    from tokenbill.core.builders import make_activity, make_config, make_license

    kid = key_id(key)
    members = [CANARY_LOGIN, *(f"alpha-{i}" for i in range(1, 5))]
    ps = [pseudonym(key, "p", m) for m in members]
    repo = pseudonym(key, "h", f"{ORG}/payments-api")
    lines, aggs = [], []
    for i, p in enumerate(ps):
        line, agg = make_ai_usage_row(date_utc=f"2026-09-1{i}", credits=f"{12 + i}.5",
                                      discount_credits="2", principal=p, team="alpha",
                                      repo=repo if i == 0 else None, organization=ORG,
                                      input_tokens=100 * i, output_tokens=7)
        lines.append(line)
        aggs.append(agg)
    direct, direct_agg = make_ai_usage_row(model="Code Review model", unattributed=True,
                                           organization=ORG, credits="3")
    lines.append(direct)
    aggs.append(direct_agg)
    src = SourceInfo(source_id="s1", adapter="fake-ai-usage", name_hmac="", sha256="0" * 64,
                     bytes=1, name_key_id=kid, principal_key_id=kid)
    usage = _result(src, cost_lines=lines, aggregates=aggs)
    lics = [make_license(p, snapshot_date="2026-09-20", team="alpha", org=ORG,
                         seat_created="2026-01-02T00:00:00Z", plan="enterprise")
            for p in ps]
    acts = [make_activity(p, date_utc="2026-09-11", team="alpha",
                          counts={"interactions": 4, "ide:vscode": 3, "cli_requests": 1},
                          flags=("used_chat", "used_cli"), reported_cost_nano=125_000_000)
            for p in ps]
    outcome = OutcomeAggregate(date_utc="2026-09-11", team="alpha", n_users=5, sessions=0,
                               commits=0, pull_requests=2, lines_added=40, lines_removed=3,
                               edits_accepted=6, edits_rejected=0,
                               source_kind="github.copilot_metrics",
                               extra=(("prs_merged", 2),))
    src2 = SourceInfo(source_id="s2", adapter="fake-org", name_hmac="", sha256="1" * 64,
                      bytes=1, name_key_id=None, principal_key_id=kid)
    org = _result(src2, licenses=lics, activity=acts, outcomes=[outcome])
    conf = [make_config("budget", {"scope": "user", "team": "alpha", "amount_nano": 5 * 10**9,
                                   "prevent_further_usage": True}, entity_id="budget:b-1",
                        snapshot_ms=NOW_MS),
            make_config("org_settings", {"plan_type": "enterprise",
                                         "seat_management_setting": "assign_selected"},
                        entity_id=f"org:{ORG}", snapshot_ms=NOW_MS),
            make_config("run_flags", {"plan.enterprise": "enterprise"},
                        entity_id="admin_answers", source_kind="tokenbill.admin_answers",
                        snapshot_ms=NOW_MS)]
    src3 = SourceInfo(source_id="s3", adapter="fake-config", name_hmac="", sha256="2" * 64,
                      bytes=1, name_key_id=None, principal_key_id=None)
    return [usage, org, _result(src3, config=conf)]


def seed(key: bytes = KEY, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"tool_version": "0.2.0-test", "created_ms": NOW_MS,
                            "window": (None, None), "principal_key_id": key_id(key),
                            "name_key_id": key_id(key)}
    base.update(kw)
    return base


def write_round_trip(out: Path, **kw: Any) -> Any:
    from tokenbill.copilot.handoff import LeakTerms, write_bundle

    args: dict[str, Any] = dict(manifest_seed=seed(), k=5, aggregate_only=False,
                                leak_terms=LeakTerms({CANARY_LOGIN: "login",
                                                      "alpha-1": "login"}))
    args.update(kw)
    return write_bundle(round_trip_results(), out, **args)
