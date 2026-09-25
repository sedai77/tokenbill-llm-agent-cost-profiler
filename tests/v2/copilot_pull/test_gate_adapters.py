"""Gate (merge gate 1): what CP-PULL records is what the real CP-BILL / CP-ORGDATA adapters read.

The fake GitHub serves the sibling packages' checked-in fixture bodies (``tests/v2/fixtures/
copilot_orgdata``, ``copilot_bill``; read as data files, SPEC §21 #4); after a pull every recorded
file is sniffed by the expected adapter through ``core.registry``, read without quarantine, and —
for seats, budgets, cost centers, org settings and metrics — gives exactly the records the adapter
reads from the fixture itself. The manifest is claimed by no adapter. A handoff pull composes with
CP-HANDOFF's export (no login in the bundle; the work directory is gone afterwards)."""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import Any

import pytest

from tokenbill.copilot.pull_common import (
    HANDOFF_KINDS,
    MANIFEST_NAME,
    Manifest,
    TokenSource,
    private_workdir,
    pull,
    require_complete,
)
from tokenbill.core.builders import CANARY_LOGIN

from .helpers import API, BLOB, NOW_MS, SIG, USER_TOKEN, Call, FakeGitHub, err, ok, source
from .helpers import token_file as make_token_file

pytestmark = pytest.mark.gate

FIX = Path(__file__).resolve().parents[1] / "fixtures"
ORGDATA = FIX / "copilot_orgdata"
BILL = FIX / "copilot_bill"
ENT = "acme"  # the fixtures' enterprise
ORGS = ["org-assign-all", "org-assign-selected", "org-disabled", "org-unconfigured"]
REPO = "acme-eng/api"
KEY = bytes(range(32))
EXPECTED = {"ai_usage": "github-ai-usage", "metered": "github-metered-usage",
            "summary": "github-billing-api", "seats": "github-copilot-seats",
            "config": "github-copilot-config", "metrics": "github-copilot-metrics",
            "agent_tasks": "github-agent-tasks"}


def _modules() -> Any:
    for name in ("tokenbill.adapters.github_billing", "tokenbill.adapters.github_config",
                 "tokenbill.adapters.github_metrics", "tokenbill.adapters.github_seats",
                 "tokenbill.adapters.github_agent_tasks"):
        pytest.importorskip(name)
    from tokenbill.core import registry

    return registry


def _opts() -> Any:
    from tokenbill.core.ids import key_id
    from tokenbill.core.types import IngestOptions

    kid = key_id(KEY)
    return IngestOptions(identity_mode="central-ingest", principal_key=KEY, principal_key_id=kid,
                         name_key=KEY, name_key_id=kid, now_ms=NOW_MS, lenient=True)


def _body(name: str) -> Any:
    return json.loads((ORGDATA / name).read_text())["response"]


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class FixtureWorld:
    """Routes that answer with the sibling fixtures' bodies."""

    def __init__(self, gh: FakeGitHub) -> None:
        base = rf"{API}/enterprises/{ENT}"
        gh.route("GET", rf"{base}/settings/billing/budgets", ok(_body("config/budgets.json")))
        for n in ("3", "7"):
            doc = json.loads((ORGDATA / f"config/budget_user_states_{n}.json").read_text())
            gh.route("GET", API + doc["request"]["path"], ok(doc["response"]))
        gh.route("GET", rf"{base}/settings/billing/cost-centers",
                 ok(_body("config/cost_centers.json")))
        for doc in _lines(ORGDATA / "config/org_billing.jsonl"):
            gh.route("GET", API + doc["request"]["path"], ok(doc["response"]))
        gh.route("GET", rf"{base}/copilot/billing/seats", ok(_body("seats/enterprise_seats.json")))
        gh.route("GET", rf"{API}/orgs/[^/]+/copilot/billing/seats", ok({"seats": [],
                                                                          "total_seats": 0}))
        pages = {d["request"]["path"]: d["response"]
                 for d in _lines(BILL / "rest/pull_pages_2026-09.jsonl")}
        for path in (f"/enterprises/{ENT}/settings/billing/ai_credit/usage",
                     f"/enterprises/{ENT}/settings/billing/usage/summary",
                     f"/enterprises/{ENT}/settings/billing/usage"):
            gh.route("GET", API + path, ok(pages[path]))
        self.exports: dict[str, str] = {}
        gh.route("POST", rf"{base}/settings/billing/reports", self._post)
        gh.route("GET", rf"blob\.example\.net/exports/(\w+)\.csv", lambda c: ok(
            (BILL / ("ai_usage_2026-09.csv" if "ai" in c.path else "detailed_2026-09.csv"))
            .read_bytes()))
        files = {"users-1-day": "users-1-day_12x3.ndjson",
                 "user-teams-1-day": "user-teams-1-day.ndjson",
                 "enterprise-1-day": "enterprise-1-day.ndjson"}
        gh.route("GET", rf"{base}/copilot/metrics/reports/([a-z0-9-]+)", lambda c: ok(
            {"download_links": [f"{BLOB}/m/{c.path.rsplit('/', 1)[1]}/{c.query['day']}?{SIG}"]}))

        def metrics(call: Call) -> Any:
            _, _, report, day = call.path.split("/")
            text = (ORGDATA / "metrics" / files[report]).read_text()
            keep = [line for line in text.splitlines() if json.loads(line).get("day") == day]
            return ok(("\n".join(keep) + "\n").encode() if keep else b"")

        gh.route("GET", r"blob\.example\.net/m/([a-z0-9-]+)/([0-9-]+)", metrics)
        gh.route("GET", rf"{API}/agents/repos/{REPO}/tasks",
                 ok(_body("agent_tasks/task_list.json")))
        for doc in _lines(ORGDATA / "agent_tasks/repo_tasks.jsonl"):
            gh.route("GET", API + doc["request"]["path"].replace("web-TB-CANARY-7f3a91", "api"),
                     ok(doc["response"]))

    def _post(self, call: Call) -> Any:
        kind = json.loads(call.body or b"{}")["report_type"]
        rid = "ai" if kind == "ai_credit" else "det"
        return ok({"id": rid, "status": "completed",
                   "download_urls": [f"{BLOB}/exports/{rid}.csv?{SIG}"]}, status=202)


def _pull(tmp_path: Path, out: Path, kinds: Any = (*HANDOFF_KINDS, "agent_tasks")) -> Manifest:
    gh = FakeGitHub()
    FixtureWorld(gh)
    user = TokenSource.from_file(make_token_file(tmp_path / "user", USER_TOKEN))
    manifest = pull(kinds, enterprise=ENT, orgs=ORGS, since="2026-09-20", until="2026-09-22",
                    token=source(tmp_path), user_token=user if "agent_tasks" in kinds else None,
                    agent_repos=[REPO] if "agent_tasks" in kinds else (), out_dir=out,
                    opener=gh, sleep=lambda s: None, now_ms=NOW_MS)
    assert "/agents/tasks" not in gh.paths()
    return manifest


def _canon(records: Any) -> list[str]:
    from tokenbill.core.records import to_json

    out = []
    for rec in records:
        doc = to_json(rec)
        doc.pop("source_id", None)
        out.append(json.dumps(doc, sort_keys=True, default=str))
    return sorted(out)


def _records(result: Any) -> dict[str, list[str]]:
    return {k: _canon(getattr(result, k)) for k in ("licenses", "activity", "config",
                                                    "aggregates", "outcomes", "cost_lines")}


def test_every_recorded_file_is_read_by_its_adapter(tmp_path: Path) -> None:
    registry = _modules()
    out = tmp_path / "out"
    manifest = _pull(tmp_path, out)
    assert manifest.complete, [u.to_json() for u in manifest.units if u.status != "complete"]
    opts = _opts()
    assert registry.sniff_adapter(out / MANIFEST_NAME) is None
    seen: set[str] = set()
    for unit in manifest.units:
        for rel in unit.files:
            adapter = registry.sniff_adapter(out / rel)
            assert adapter is not None and adapter.name == EXPECTED[unit.kind], rel
            result = adapter.read(out / rel, opts)
            assert result.quarantined == [], (rel, result.quarantined)
            seen.add(unit.kind)
    assert seen == set(EXPECTED)


@pytest.mark.parametrize("fixture, recorded", [
    ("seats/enterprise_seats.json", "seats/enterprise.jsonl"),
    ("config/cost_centers.json", "config/cost_centers.jsonl"),
    ("config/org_billing.jsonl", "config/org_billing"),
    ("config/budgets.json+config/budget_user_states_3.json+config/budget_user_states_7.json",
     "config/budgets.jsonl"),
    ("metrics/users-1-day_12x3.ndjson", "metrics/users-1-day/enterprise"),
    ("metrics/enterprise-1-day.ndjson", "metrics/enterprise-1-day/enterprise"),
])
def test_recorded_files_give_the_fixture_records(tmp_path: Path, fixture: str,
                                                 recorded: str) -> None:
    registry = _modules()
    out = tmp_path / "out"
    _pull(tmp_path, out, kinds=("seats", "config", "metrics"))
    parts = fixture.split("+")
    if len(parts) > 1:
        src = tmp_path / "fixture"
        src.mkdir()
        for p in parts:
            shutil.copy(ORGDATA / p, src / Path(p).name)
    else:
        src = ORGDATA / fixture
    adapter = registry.sniff_adapter(out / recorded if (out / recorded).is_file()
                                     else next((out / recorded).rglob("*.jsonl")))
    assert adapter is not None
    expected = _records(adapter.read(src, _opts()))
    got = _records(adapter.read(out / recorded, _opts()))
    assert got == expected
    assert any(expected.values())


def test_billing_files_are_read_with_their_rows(tmp_path: Path) -> None:
    registry = _modules()
    out = tmp_path / "out"
    manifest = _pull(tmp_path, out, kinds=("ai_usage", "metered", "summary"))
    opts = _opts()
    ai = manifest.unit("ai_usage/2026-09-20_2026-09-22")
    assert ai is not None and ai.files == ("ai_usage/2026-09-20_2026-09-22.csv",)
    assert (out / ai.files[0]).read_bytes() == (BILL / "ai_usage_2026-09.csv").read_bytes()
    result = registry.get_adapter("github-ai-usage").read(out / ai.files[0], opts)
    assert len(result.cost_lines) == 40
    summary = [f for u in manifest.units if u.kind == "summary" for f in u.files]
    lines = [registry.get_adapter("github-billing-api").read(out / f, opts) for f in summary]
    assert all(r.quarantined == [] for r in lines) and sum(len(r.cost_lines) for r in lines) > 0


def test_handoff_pull_composes_with_the_export(tmp_path: Path) -> None:
    _modules()
    handoff = pytest.importorskip("tokenbill.copilot.handoff")
    bundle = tmp_path / "export.tbx"
    with private_workdir(parent=tmp_path / "tmp") as work:
        manifest = _pull(tmp_path, work, kinds=HANDOFF_KINDS)
        require_complete(manifest, keep_raw=False)
        report = handoff.export_from_files(
            manifest.files(work), bundle, key=KEY, team_map={}, cost_center_map={},
            since="2026-09-20", until="2026-09-22", now_ms=NOW_MS, tool_version="gate")
    assert not work.exists()
    assert report.manifest.leak_scan.result == "clean"
    with zipfile.ZipFile(bundle) as zf:
        blob = b"".join(zf.read(n) for n in zf.namelist()).lower()
    assert CANARY_LOGIN.encode() not in blob and b"sig=" not in blob


def test_forbidden_export_leaves_other_sources_readable(tmp_path: Path) -> None:
    registry = _modules()
    gh = FakeGitHub()
    FixtureWorld(gh)
    gh.route("POST", rf"{API}/enterprises/{ENT}/settings/billing/reports", err(403))
    out = tmp_path / "out"
    manifest = pull(("ai_usage", "seats"), enterprise=ENT, orgs=[], since="2026-09-20",
                    until="2026-09-22", token=source(tmp_path), out_dir=out, opener=gh,
                    sleep=lambda s: None, now_ms=NOW_MS)
    assert [u.status for u in manifest.units] == ["forbidden", "complete"]
    seats = registry.sniff_adapter(out / "seats/enterprise.jsonl")
    assert seats is not None and seats.read(out / "seats/enterprise.jsonl", _opts()).licenses
