"""``github-copilot-activity-report`` adapter (brief Build 7, addendum §5.15)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from tokenbill.adapters.github_activity_report import (
    ActivityReportAdapter,
    bucket,
    parse_timestamp,
)
from tokenbill.core.builders import CANARY_LOGIN
from tokenbill.core.errors import SourceError, UsageError
from tokenbill.core.ids import key_id, pseudonym
from tokenbill.core.records import Attribution, to_json
from tokenbill.core.registry import SNIFF_HEAD_BYTES
from tokenbill.core.testing import assert_adapter_conforms, conformance_ingest_options
from tokenbill.core.types import IngestOptions

from .helpers import FIXTURES, KEY, NOW_MS

REPORT = FIXTURES / "activity_report.csv"
HEADER = "report_time,login,last_authenticated_at,last_activity_at,last_surface_used"


def opts(**kw: object) -> IngestOptions:
    base: dict[str, object] = dict(identity_mode="central-ingest", principal_key=KEY,
                                   principal_key_id=key_id(KEY), name_key=KEY,
                                   name_key_id=key_id(KEY), now_ms=NOW_MS)
    base.update(kw)
    return IngestOptions(**base)  # type: ignore[arg-type]


def read(path: Path, **kw: object):
    return ActivityReportAdapter().read(path, opts(**kw))


def write(tmp_path: Path, *rows: str, header: str = HEADER, name: str = "r.csv") -> Path:
    path = tmp_path / name
    path.write_text("\n".join((header, *rows)) + "\n", encoding="utf-8")
    return path


def test_fixture_snapshots() -> None:
    res = read(REPORT)
    by = {lic.principal: lic for lic in res.licenses}
    assert len(res.licenses) == 12
    assert all(lic.plan == "unknown" and lic.assigned_via_team is None for lic in res.licenses)
    assert all(lic.seat_created is None and lic.pending_cancellation is None
               for lic in res.licenses)
    assert all(lic.source_kind == "github.copilot_activity_report" for lic in res.licenses)
    assert all(lic.snapshot_date == "2026-09-20" and lic.org is None for lic in res.licenses)

    def lic(login: str):
        return by[pseudonym(KEY, "p", login)]

    canary = lic(CANARY_LOGIN)
    assert (canary.last_activity_bucket, canary.last_activity_surface) == ("0-7", "vscode")
    assert lic("octo-dev-02").last_activity_bucket == "8-30"
    assert lic("octo-dev-02").last_activity_surface == "jetbrains"   # third-party string
    assert lic("octo-dev-03").last_activity_bucket == "31-90"
    assert lic("octo-dev-03").last_activity_surface == "github_com"
    assert lic("octo-dev-04").last_activity_bucket == "none_90d"   # empty last_activity_at
    assert lic("octo-dev-04").last_activity_surface is None        # Unspecified
    assert lic("octo-dev-04").last_authenticated_bucket == "none_90d"
    assert lic("octo-dev-05").last_authenticated_bucket == "none_90d"
    assert lic("octo-dev-05").last_activity_surface is None
    assert lic("octo-dev-07").last_activity_bucket == "8-30"       # 8 days
    assert lic("octo-dev-08").last_activity_bucket == "0-7"        # 7 days
    assert lic("octo-dev-08").last_activity_surface == "other"     # unknown surface
    assert lic("octo-dev-09").last_activity_bucket == "8-30"       # M/D/YYYY h:mm AM
    assert lic("octo-dev-09").last_activity_surface == "neovim"
    assert lic("octo-dev-10").last_activity_bucket == "31-90"      # exactly 90 days
    assert lic("octo-dev-11").last_activity_bucket == "none_90d"   # 91 days
    assert lic("octo-dev-11").last_activity_surface == "cli"
    assert res.capabilities == frozenset({"licenses"})
    assert res.source.principal_key_id == key_id(KEY) and res.source.name_key_id == key_id(KEY)
    codes = {n.code: n for n in res.notes}
    assert codes["dq.copilot_activity_report_listed"].count == 12
    blob = json.dumps(to_json(res)) + repr(res)
    assert CANARY_LOGIN not in blob and "octo-dev" not in blob


def test_conforms() -> None:
    first = assert_adapter_conforms(ActivityReportAdapter(), REPORT,
                                    expect_capabilities={"licenses"},
                                    opts=conformance_ingest_options())
    assert len(first.licenses) == 12


def test_sniff() -> None:
    adapter = ActivityReportAdapter()
    head = REPORT.read_bytes()[:SNIFF_HEAD_BYTES]
    assert adapter.sniff(REPORT, head)
    assert adapter.sniff(REPORT, b"login,last_activity_at,last_surface_used,extra\n")
    assert not adapter.sniff(REPORT, b"date,username,product,sku,model\n")
    assert not adapter.sniff(REPORT, b"login,team\nalice,a\n")
    assert not adapter.sniff(REPORT, b"")
    assert not adapter.sniff(REPORT, b"\x00\xff\xfe binary")


def test_team_and_cost_center_maps_case_insensitive(tmp_path: Path) -> None:
    path = write(tmp_path, "2026-09-20T00:00:00Z,Octo-Dev-01,,2026-09-19T00:00:00Z,VS Code 1.9",
                 "2026-09-20T00:00:00Z,octo-dev-02,,,")
    res = read(path, team_map=(("octo-dev-01", "platform"),),
               cost_center_map=(("OCTO-DEV-02", "Data"),))
    by = {x.principal: x for x in res.licenses}
    assert by[pseudonym(KEY, "p", "Octo-Dev-01")].team == "platform"
    assert by[pseudonym(KEY, "p", "octo-dev-02")].cost_center == "Data"


def test_org_from_attribution(tmp_path: Path) -> None:
    path = write(tmp_path, "2026-09-20T00:00:00Z,a-user,,,")
    res = read(path, attribution=Attribution(workspace_id="octo-labs"))
    assert res.licenses[0].org == "octo-labs"


def test_quarantine_and_strict(tmp_path: Path) -> None:
    path = write(tmp_path,
                 "2026-09-20T00:00:00Z,,2026-09-19T00:00:00Z,,",       # no login
                 "yesterday,user-b,,,",                                  # bad report_time
                 "2026-09-20T00:00:00Z,user-c,,soon,",                   # bad last_activity_at
                 "2026-09-20T00:00:00Z,user-d,13/45/2026,,",             # bad auth time
                 "2026-09-20T00:00:00Z,user-e,,2026-09-18T00:00:00+02:00,VS Code 1.1",
                 "",                                                     # blank line skipped
                 )
    res = read(path)
    assert [q.reason for q in res.quarantined] == [
        "missing:login", "bad_type:report_time", "bad_type:last_activity_at",
        "bad_type:last_authenticated_at"]
    assert all(q.locator.startswith("line ") for q in res.quarantined)
    assert len(res.licenses) == 1 and res.stats["quarantined"] == 4
    assert any(n.code == "dq.quarantined" for n in res.notes)
    with pytest.raises(SourceError) as err:
        read(path, lenient=False)
    assert "line 2" in str(err.value) and "user" not in str(err.value)


def test_duplicate_login_keeps_most_recent(tmp_path: Path) -> None:
    path = write(tmp_path, "2026-09-20T00:00:00Z,dup-user,,2026-07-01T00:00:00Z,Xcode 1",
                 "2026-09-20T00:00:00Z,DUP-USER,,2026-09-19T00:00:00Z,VS Code 1",
                 "2026-09-20T00:00:00Z,dup-user,,2026-08-01T00:00:00Z,Eclipse 2")
    res = read(path)
    assert len(res.licenses) == 1
    assert res.licenses[0].last_activity_surface == "vscode"
    assert res.stats["duplicates"] == 2
    assert any(n.code == "dq.copilot_activity_report_duplicate_login" for n in res.notes)


def test_extra_columns_and_missing_optional(tmp_path: Path) -> None:
    path = write(tmp_path, "x,user-a,2026-09-19T00:00:00Z,VS Code 2,2026-09-20T00:00:00Z",
                 header="team_hint,login,last_activity_at,last_surface_used,report_time")
    res = read(path)
    (lic,) = res.licenses
    assert lic.last_authenticated_bucket == "unknown"      # column absent
    assert lic.last_activity_bucket == "0-7"
    assert res.stats["unknown_columns"] == 1
    assert any(n.code == "dq.unknown_fields" for n in res.notes)


def test_no_report_time_column_uses_clock(tmp_path: Path) -> None:
    path = write(tmp_path, "user-a,2026-09-20T00:00:00Z,VS Code 2",
                 header="login,last_activity_at,last_surface_used")
    res = read(path)
    assert res.licenses[0].snapshot_date == "2026-09-25"
    assert any(n.code == "dq.copilot_activity_report_no_report_time" for n in res.notes)
    res0 = read(path, now_ms=0)
    assert not res0.licenses and res0.quarantined[0].reason == "missing:report_time"


def test_window_filter(tmp_path: Path) -> None:
    path = write(tmp_path, "2026-08-20T00:00:00Z,user-a,,,", "2026-09-20T00:00:00Z,user-b,,,")
    since = (dt.date(2026, 9, 1) - dt.date(1970, 1, 1)).days * 86_400_000
    res = read(path, since_ms=since)
    assert len(res.licenses) == 1 and res.stats["outside_window"] == 1
    res = read(path, until_ms=since)
    assert len(res.licenses) == 1 and res.licenses[0].snapshot_date == "2026-08-20"


def test_errors(tmp_path: Path) -> None:
    adapter = ActivityReportAdapter()
    with pytest.raises(UsageError):
        adapter.read(REPORT, opts(principal_key=None))
    with pytest.raises(UsageError):
        adapter.read(REPORT, "not options")  # type: ignore[arg-type]
    with pytest.raises(SourceError):
        adapter.read(tmp_path / "missing.csv", opts())
    bad = tmp_path / "latin.csv"
    bad.write_bytes(HEADER.encode() + b"\n2026-09-20,\xe9t\xe9,,,\n")
    with pytest.raises(SourceError):
        adapter.read(bad, opts())
    empty = tmp_path / "empty.csv"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(SourceError):
        adapter.read(empty, opts())
    other = write(tmp_path, "a,b", header="login,team", name="other.csv")
    with pytest.raises(SourceError):
        adapter.read(other, opts())
    nul = tmp_path / "nul.csv"
    nul.write_text(HEADER + "\n" + "2026-09-20,a\x00b,,,\n", encoding="utf-8")
    res_or_err = None
    try:
        res_or_err = adapter.read(nul, opts())
    except SourceError:
        res_or_err = "error"
    assert res_or_err is not None


def test_empty_report_has_no_capabilities(tmp_path: Path) -> None:
    res = read(write(tmp_path))
    assert res.licenses == [] and res.capabilities == frozenset()
    assert res.source.principal_key_id is None


@pytest.mark.parametrize(("text", "expected"), [
    ("2026-09-20T08:00:00Z", dt.date(2026, 9, 20)),
    ("2026-09-20 23:30:00 UTC", dt.date(2026, 9, 20)),
    ("2026-09-20T23:30:00-02:00", dt.date(2026, 9, 21)),
    ("2026-09-20T00:30:00+0100", dt.date(2026, 9, 19)),
    ("2026-09-20T08:00:00.123456Z", dt.date(2026, 9, 20)),
    ("2026-09-20", dt.date(2026, 9, 20)),
    ("9/20/2026", dt.date(2026, 9, 20)),
    ("9/20/26 11:59 PM", dt.date(2026, 9, 20)),
    ("9/20/2026 12:00:00 AM", dt.date(2026, 9, 20)),
    ("", None),
    ("   ", None),
])
def test_parse_timestamp(text: str, expected: dt.date | None) -> None:
    assert parse_timestamp(text) == expected


@pytest.mark.parametrize("text", ["yesterday", "2026-13-01", "13/1/2026", "9/20/2026 13:00 PM",
                                  "2026-09-20T10:00:00+25:00", "0000-01-01",
                                  "9999-12-31T23:00:00-05:00"])
def test_parse_timestamp_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_timestamp(text)


def test_bucket_edges() -> None:
    day = dt.date(2026, 9, 20)
    assert bucket(day, None) == "none_90d"
    assert bucket(day, dt.date(2026, 9, 25)) == "0-7"     # after the report
    assert [bucket(day, day - dt.timedelta(days=n)) for n in (0, 7, 8, 30, 31, 90, 91)] == [
        "0-7", "0-7", "8-30", "8-30", "31-90", "31-90", "none_90d"]
