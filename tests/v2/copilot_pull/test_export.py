"""Report exports: POST → poll → download, 409 back-off, the 30-minute cap and ``--resume`` of a
pending export, failures, multi-part and compressed downloads, windows (addendum §5.12)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest

from tokenbill.copilot.pull_billing import (
    REPORT_WINDOW_DAYS,
    ExportState,
    export_windows,
    months,
    parse_export,
)
from tokenbill.copilot.pull_common import (
    EXPORT_CAP_S,
    EXPORT_POLL_S,
    MANIFEST_NAME,
    Manifest,
    PullError,
    pull,
)

from .helpers import (
    API,
    BLOB,
    ENT,
    NOW_MS,
    SIG,
    FakeGitHub,
    World,
    ai_usage_csv,
    all_recorded_bytes,
    err,
    ok,
    source,
)

REPORTS = rf"{API}/enterprises/{ENT}/settings/billing/reports"
UNIT = "ai_usage/2026-09-01_2026-09-02"


def run(tmp_path: Path, gh: FakeGitHub, sleeps: list[int], *, kinds: Any = ("ai_usage",),
        since: str = "2026-09-01", until: str = "2026-09-02", resume: bool = False) -> Manifest:
    return pull(kinds, enterprise=ENT, orgs=[], since=since, until=until,
                token=source(tmp_path), out_dir=tmp_path / "out", opener=gh,
                sleep=sleeps.append, now_ms=NOW_MS, resume=resume)


def posts(gh: FakeGitHub) -> list[dict[str, Any]]:
    return [json.loads(c.body or b"") for c in gh.api_calls() if c.method == "POST"]


def test_post_poll_download(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh, polls_until_done=3)  # processing, processing, completed
    sleeps: list[int] = []
    manifest = run(tmp_path, gh, sleeps)
    unit = manifest.unit(UNIT)
    assert unit is not None and unit.status == "complete" and unit.window == ("2026-09-01",
                                                                             "2026-09-02")
    assert unit.files == (f"{UNIT}.csv",) and unit.export_id is None
    assert sleeps == [EXPORT_POLL_S] * 3
    assert (tmp_path / "out" / f"{UNIT}.csv").read_bytes() == ai_usage_csv("2026-09-01")
    methods = [(c.method, c.path.rsplit("/", 1)[-1]) for c in gh.calls]
    assert methods == [("POST", "reports")] + [("GET", "rpt-ai_credit-2026-09-01")] * 3 + [
        ("GET", "rpt-ai_credit-2026-09-01.csv")]
    assert posts(gh) == [{"report_type": "ai_credit", "start_date": "2026-09-01",
                          "end_date": "2026-09-02", "send_email": False}]
    assert b"sig=" not in all_recorded_bytes(tmp_path / "out")


def test_409_waits_then_succeeds(tmp_path: Path) -> None:
    gh = FakeGitHub()
    world = World(gh)
    gh.route("POST", REPORTS, [err(409, {"message": "export in progress"}, retry_after="10"),
                               err(409), world._post_export])
    sleeps: list[int] = []
    manifest = run(tmp_path, gh, sleeps)
    assert manifest.complete
    assert sleeps == [10, EXPORT_POLL_S, EXPORT_POLL_S]
    assert len(posts(gh)) == 3


def test_409_until_the_cap_blocks_later_exports(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    gh.route("POST", REPORTS, err(409))
    sleeps: list[int] = []
    manifest = run(tmp_path, gh, sleeps, kinds=("ai_usage", "metered"))
    ai = manifest.unit(UNIT)
    metered = manifest.unit("metered/2026-09-01_2026-09-02")
    assert ai is not None and ai.status == "incomplete" and ai.reason == "export_busy"
    assert metered is not None and metered.reason == "export_pending"
    assert sum(sleeps) == EXPORT_CAP_S
    assert len(posts(gh)) == EXPORT_CAP_S // EXPORT_POLL_S + 1  # nothing for metered


def test_export_over_30_minutes_stays_incomplete_and_is_resumed(tmp_path: Path) -> None:
    gh = FakeGitHub()
    world = World(gh, polls_until_done=10_000)
    sleeps: list[int] = []
    manifest = run(tmp_path, gh, sleeps, kinds=("ai_usage", "metered"))
    ai = manifest.unit(UNIT)
    assert ai is not None and ai.status == "incomplete" and ai.reason == "export_timeout"
    assert ai.export_id == "rpt-ai_credit-2026-09-01" and ai.files == ()
    assert sum(sleeps) == EXPORT_CAP_S and len(sleeps) == EXPORT_CAP_S // EXPORT_POLL_S
    metered = manifest.unit("metered/2026-09-01_2026-09-02")
    assert metered is not None and metered.reason == "export_pending"
    assert [p["report_type"] for p in posts(gh)] == ["ai_credit"]  # one export at a time
    assert not manifest.complete
    # the export finishes meanwhile; --resume polls the same export instead of exporting again
    world.polls_until_done = 0
    gh.calls.clear()
    sleeps2: list[int] = []
    final = run(tmp_path, gh, sleeps2, kinds=("ai_usage", "metered"), resume=True)
    assert final.complete
    assert [p["report_type"] for p in posts(gh)] == ["detailed"]
    first = gh.api_calls()[0]
    assert first.method == "GET" and first.path.endswith("/reports/rpt-ai_credit-2026-09-01")
    assert final.unit(UNIT).export_id is None  # type: ignore[union-attr]
    assert (tmp_path / "out" / f"{UNIT}.csv").is_file()


def test_resume_of_a_vanished_export_exports_again(tmp_path: Path) -> None:
    gh = FakeGitHub()
    world = World(gh, polls_until_done=10_000)
    run(tmp_path, gh, [])
    world.exports.clear()  # GitHub forgot it: 404
    world.polls_until_done = 0
    gh.calls.clear()
    final = run(tmp_path, gh, [], resume=True)
    assert final.complete and len(posts(gh)) == 1


def test_failed_export(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    gh.route("GET", rf"{REPORTS}/[^/]+", ok({"id": "rpt-1", "status": "failed"}))
    manifest = run(tmp_path, gh, [])
    unit = manifest.unit(UNIT)
    assert unit is not None and unit.reason == "export_failed" and unit.export_id is None
    gh2 = FakeGitHub()
    gh2.route("POST", REPORTS, ok({"id": "rpt-2", "status": "error"}, status=202))
    assert run(tmp_path / "b", gh2, []).unit(UNIT).reason == "export_failed"  # type: ignore


def test_completed_without_links_and_bad_shapes(tmp_path: Path) -> None:
    gh = FakeGitHub()
    gh.route("POST", REPORTS, ok({"id": "rpt-1", "status": "completed", "download_urls": []},
                                 status=201))
    assert run(tmp_path, gh, []).unit(UNIT).reason == "export_without_download"  # type: ignore
    gh2 = FakeGitHub()
    gh2.route("POST", REPORTS, ok({"status": "processing"}, status=202))
    assert run(tmp_path / "b", gh2, []).unit(UNIT).reason == "export_shape"  # type: ignore


def test_several_download_parts_and_gzip(tmp_path: Path) -> None:
    gh = FakeGitHub()
    gh.route("POST", REPORTS, ok({"id": "rpt-9", "status": "completed", "download_urls": [
        f"{BLOB}/p1.csv?{SIG}", f"{BLOB}/p2.csv.gz?{SIG}"]}, status=202))
    gh.route("GET", r"blob\.example\.net/p1\.csv", ok(b"date,sku\n2026-09-01,x\n"))
    gh.route("GET", r"blob\.example\.net/p2\.csv\.gz",
             ok(gzip.compress(b"date,sku\n2026-09-02,y\n", mtime=0)))
    manifest = run(tmp_path, gh, [])
    unit = manifest.unit(UNIT)
    assert unit is not None and unit.files == (f"{UNIT}_part1.csv", f"{UNIT}_part2.csv.gz")
    out = tmp_path / "out"
    assert gzip.decompress((out / f"{UNIT}_part2.csv.gz").read_bytes()).endswith(b"y\n")


def test_a_corrupt_gzip_download_is_rejected(tmp_path: Path) -> None:
    gh = FakeGitHub()
    gh.route("POST", REPORTS, ok({"id": "rpt-9", "status": "completed",
                                  "download_urls": [f"{BLOB}/p.csv?{SIG}"]}, status=202))
    gh.route("GET", r"blob\.example\.net/p\.csv", ok(b"\x1f\x8b\x08garbage"))
    unit = run(tmp_path, gh, []).unit(UNIT)
    assert unit is not None and unit.reason == "rejected_unreadable" and unit.files == ()


def test_a_signed_url_inside_a_csv_rejects_the_unit(tmp_path: Path) -> None:
    gh = FakeGitHub()
    gh.route("POST", REPORTS, ok({"id": "rpt-9", "status": "completed",
                                  "download_urls": [f"{BLOB}/p.csv?{SIG}"]}, status=202))
    gh.route("GET", r"blob\.example\.net/p\.csv",
             ok(b"date,note\n2026-09-01,https://s.example/x?X-Amz-Signature=1\n"))
    assert run(tmp_path, gh, []).unit(UNIT).reason == "rejected_signed_url"  # type: ignore


def test_windows_split_at_31_days_and_clamp_to_today(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh, polls_until_done=0)
    manifest = run(tmp_path, gh, [], since="2026-07-01", until="2026-09-30")
    assert [u.window for u in manifest.units] == [("2026-07-01", "2026-07-31"),
                                                  ("2026-08-01", "2026-08-31"),
                                                  ("2026-09-01", "2026-09-25")]
    assert [(p["start_date"], p["end_date"]) for p in posts(gh)] == [
        ("2026-07-01", "2026-07-31"), ("2026-08-01", "2026-08-31"), ("2026-09-01", "2026-09-25")]
    assert Manifest.load(tmp_path / "out" / MANIFEST_NAME).complete


def test_export_windows_and_months() -> None:
    assert export_windows("2026-01-01", "2026-01-31", "ai_credit") == [("2026-01-01",
                                                                        "2026-01-31")]
    assert export_windows("2026-01-01", "2026-02-01", "detailed") == [
        ("2026-01-01", "2026-01-31"), ("2026-02-01", "2026-02-01")]
    assert export_windows("2025-01-01", "2025-12-31", "summarized") == [("2025-01-01",
                                                                         "2025-12-31")]
    assert REPORT_WINDOW_DAYS["summarized"] == 366
    assert export_windows("2026-01-02", "2026-01-01", "ai_credit") == []
    assert months("2026-11-15", "2027-02-01") == [(2026, 11), (2026, 12), (2027, 1), (2027, 2)]


@pytest.mark.parametrize("doc, state", [
    ({"id": "a1", "status": "processing"}, ExportState("a1", "processing", ())),
    ({"id": 12, "status": "Completed", "download_urls": ["https://x/y"]},
     ExportState("12", "completed", ("https://x/y",))),
    ({"id": "a1", "download_urls": ["https://x/y"]},
     ExportState("a1", "completed", ("https://x/y",))),
    ({"id": "a1", "status": "expired"}, ExportState("a1", "failed", ())),
    ({"usage_report_exports": [{"id": "z", "status": "completed", "download_urls": []}]},
     ExportState("z", "completed", ())),
    ({"id": "a1", "status": 3}, ExportState("a1", "processing", ())),
])
def test_parse_export(doc: Any, state: ExportState) -> None:
    assert parse_export(doc) == state


@pytest.mark.parametrize("doc", [
    None, [], {"id": ""}, {"id": "../x"}, {"id": True}, {"usage_report_exports": []},
    {"id": "a", "download_urls": "https://x"}, {"id": "a", "download_urls": [1]},
    {"id": "a", "download_urls": ["http://x/y"]},
])
def test_parse_export_rejects(doc: Any) -> None:
    with pytest.raises(PullError):
        parse_export(doc)
