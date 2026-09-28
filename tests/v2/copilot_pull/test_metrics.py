"""Metrics report days: not-ready days, D-3 re-pulls, download links → envelope lines, NDJSON /
array / gzip downloads, organization variants and report selection (addendum §5.12)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from tokenbill.copilot.pull_common import Manifest, PullError, pull
from tokenbill.copilot.pull_metrics import (
    METRICS_REPORTS,
    check_reports,
    download_links,
    iter_records,
    metrics_days,
)
from tokenbill.core.builders import CANARY_LOGIN
from tokenbill.core.errors import UsageError

from .helpers import API, BLOB, ENT, NOW_MS, ORG, SIG, FakeGitHub, World, err, ok, source

REPORT = rf"{API}/enterprises/{ENT}/copilot/metrics/reports/users-1-day"


def run(tmp_path: Path, gh: FakeGitHub, *, since: str = "2026-09-01", until: str = "2026-09-01",
        resume: bool = False, **kw: Any) -> Manifest:
    return pull(["metrics"], enterprise=kw.pop("enterprise", ENT), orgs=kw.pop("orgs", []),
                since=since, until=until, token=source(tmp_path), out_dir=tmp_path / "out",
                opener=gh, sleep=lambda s: None, now_ms=NOW_MS, resume=resume, **kw)


def records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_day_becomes_envelope_lines_of_the_report_call(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    manifest = run(tmp_path, gh)
    unit = manifest.unit("metrics/users-1-day/enterprise/2026-09-01")
    assert unit is not None and unit.status == "complete" and unit.day == "2026-09-01"
    lines = records(tmp_path / "out" / "metrics/users-1-day/enterprise/2026-09-01.jsonl")
    assert len(lines) == 1
    assert lines[0]["request"] == {"path": f"/enterprises/{ENT}/copilot/metrics/reports/"
                                           "users-1-day", "query": {"day": "2026-09-01"}}
    assert lines[0]["response"]["user_login"] == CANARY_LOGIN
    assert lines[0]["response"]["ai_credits_used"] == 12.5  # exact decimal kept verbatim
    text = (tmp_path / "out" / "metrics/users-1-day/enterprise/2026-09-01.jsonl").read_text()
    assert '"ai_credits_used":12.5' in text and "download_links" not in text
    assert "blob.example.net" not in text and "sig=" not in text
    assert {u.id.split("/")[1] for u in manifest.units} == set(METRICS_REPORTS)


@pytest.mark.parametrize("status", [204, 404])
def test_not_ready_days(tmp_path: Path, status: int) -> None:
    gh = FakeGitHub()
    World(gh)
    gh.route("GET", REPORT, err(status) if status == 404 else ok(b"", status=204))
    manifest = run(tmp_path, gh)
    unit = manifest.unit("metrics/users-1-day/enterprise/2026-09-01")
    assert unit is not None and unit.status == "not_ready" and unit.files == ()
    assert unit.reason == f"http_{status}"
    assert manifest.complete  # a day GitHub has not processed is not a failure
    gh.calls.clear()
    run(tmp_path, gh, resume=True)
    assert [c.path for c in gh.api_calls()] == [f"/enterprises/{ENT}/copilot/metrics/reports/"
                                                "users-1-day"]


def test_days_newer_than_d_minus_3_are_provisional_and_pulled_again(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    manifest = run(tmp_path, gh, since="2026-09-21", until="2026-09-30",
                   metrics_reports=["users-1-day"])
    status = {u.day: u.status for u in manifest.units}
    # today is 2026-09-25: D-3 = 09-22; later days are provisional; days after today not asked
    assert status == {"2026-09-21": "complete", "2026-09-22": "complete",
                      "2026-09-23": "provisional", "2026-09-24": "provisional",
                      "2026-09-25": "provisional"}
    assert manifest.complete
    gh.calls.clear()
    run(tmp_path, gh, since="2026-09-21", until="2026-09-30", resume=True,
        metrics_reports=["users-1-day"])
    assert sorted(c.query["day"] for c in gh.api_calls()) == ["2026-09-23", "2026-09-24",
                                                              "2026-09-25"]


def test_a_failed_repull_keeps_the_earlier_files(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    run(tmp_path, gh, since="2026-09-24", until="2026-09-24", metrics_reports=["users-1-day"])
    gh.route("GET", REPORT, err(500))
    manifest = run(tmp_path, gh, since="2026-09-24", until="2026-09-24", resume=True,
                   metrics_reports=["users-1-day"])
    unit = manifest.unit("metrics/users-1-day/enterprise/2026-09-24")
    assert unit is not None and unit.status == "incomplete"
    assert unit.files == ("metrics/users-1-day/enterprise/2026-09-24.jsonl",)
    assert (tmp_path / "out" / unit.files[0]).is_file()


def test_array_gzip_bad_lines_and_empty_links(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    rec = {"day": "2026-09-01", "enterprise_id": "1", "user_login": "u1", "loc_added_sum": 1}
    gh.route("GET", REPORT, ok({"download_links": [f"{BLOB}/a.json?{SIG}",
                                                   f"{BLOB}/b.ndjson.gz?{SIG}"]}))
    gh.route("GET", r"blob\.example\.net/a\.json", ok(b'\xef\xbb\xbf[\n' + json.dumps(rec).encode()
                                                      + b', 3\n]'))
    gh.route("GET", r"blob\.example\.net/b\.ndjson\.gz", ok(gzip.compress(
        json.dumps(rec).encode() + b"\n\nnot json\n[1]\n", mtime=0)))
    manifest = run(tmp_path, gh, metrics_reports=["users-1-day"])
    unit = manifest.unit("metrics/users-1-day/enterprise/2026-09-01")
    assert unit is not None and unit.status == "complete" and unit.skipped == 3
    assert len(records(tmp_path / "out" / unit.files[0])) == 2
    gh.route("GET", REPORT, ok({"download_links": []}))
    empty = run(tmp_path / "e", gh, metrics_reports=["users-1-day"])
    assert empty.units[0].status == "complete" and empty.units[0].files == ()


def test_bad_report_answers(tmp_path: Path) -> None:
    gh = FakeGitHub()
    gh.route("GET", REPORT, ok({"report_day": "2026-09-01"}))
    manifest = run(tmp_path, gh, metrics_reports=["users-1-day"])
    assert manifest.units[0].reason == "metrics_shape"
    gh.route("GET", REPORT, ok({"download_links": ["http://insecure/x"]}))
    assert run(tmp_path / "b", gh, metrics_reports=["users-1-day"]).units[0].reason == "bad_link"


def test_organization_variants_without_an_enterprise(tmp_path: Path) -> None:
    gh = FakeGitHub()
    gh.route("GET", rf"{API}/orgs/{ORG}/copilot/metrics/reports/([a-z0-9-]+)", lambda c: ok(
        {"download_links": [f"{BLOB}/o.ndjson?{SIG}"]}))
    gh.route("GET", r"blob\.example\.net/o\.ndjson", ok(b'{"day": "2026-09-01", '
                                                        b'"organization_id": "9"}\n'))
    manifest = run(tmp_path, gh, enterprise=None, orgs=[ORG])
    assert {u.id for u in manifest.units} == {
        f"metrics/users-1-day/org-{ORG}/2026-09-01",
        f"metrics/user-teams-1-day/org-{ORG}/2026-09-01",
        f"metrics/organization-1-day/org-{ORG}/2026-09-01"}
    assert sorted(c.path.rsplit("/", 1)[1] for c in gh.api_calls()) == [
        "organization-1-day", "user-teams-1-day", "users-1-day"]
    line = records(tmp_path / "out" / f"metrics/organization-1-day/org-{ORG}/2026-09-01.jsonl")
    assert line[0]["request"]["path"] == f"/orgs/{ORG}/copilot/metrics/reports/organization-1-day"


def test_report_selection() -> None:
    assert check_reports(None) == METRICS_REPORTS
    assert check_reports("repos-1-day,users-1-day") == ("users-1-day", "repos-1-day")
    for bad in ([], ["x"], 5, [1]):
        with pytest.raises(UsageError):
            check_reports(bad)  # type: ignore[arg-type]


def test_metrics_days() -> None:
    assert metrics_days("2026-09-24", "2026-09-30", "2026-09-25") == ["2026-09-24", "2026-09-25"]
    assert metrics_days("2026-09-26", "2026-09-30", "2026-09-25") == []


def test_download_links_shapes() -> None:
    assert download_links({"download_links": None}) == ()
    assert download_links({"download_links": ["https://a/b"]}) == ("https://a/b",)
    for bad in ([], {"x": 1}, {"download_links": "https://a"}, {"download_links": [2]}):
        with pytest.raises(PullError):
            download_links(bad)


def test_iter_records_edge_cases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tokenbill.copilot import pull_metrics

    path = tmp_path / "d"
    path.write_bytes(b"[1, 2")
    assert list(iter_records(path)) == [None]
    path.write_bytes(b'[{"a": 1}]\n')
    assert list(iter_records(path)) == [{"a": 1}]
    path.write_bytes(b"{}")
    assert list(iter_records(path)) == [{}]
    path.write_bytes(b'\x1f\x8b\x08\x00broken')
    with pytest.raises(PullError, match="corrupt"):
        list(iter_records(path))
    good = gzip.compress(b'{"a": 1}\n' * 50, mtime=0)
    path.write_bytes(good[:12] + b"\xff" * 20 + good[32:])  # valid header, broken deflate
    with pytest.raises(PullError, match="corrupt"):
        list(iter_records(path))
    monkeypatch.setattr(pull_metrics, "MAX_LINE_BYTES", 8)
    path.write_bytes(b'{"a": 123456789}\n{"b": 1}\n')
    assert list(iter_records(path)) == [None, {"b": 1}]
    monkeypatch.undo()
    monkeypatch.setattr(pull_metrics, "_MAX_ARRAY_BYTES", 4)
    path.write_bytes(b"[1, 2, 3]")
    with pytest.raises(PullError, match="size limit"):
        list(iter_records(path))
