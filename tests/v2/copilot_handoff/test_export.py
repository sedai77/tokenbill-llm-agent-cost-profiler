"""``export_from_files`` orchestration and ``read_team_map_csv``."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tokenbill.copilot.admin_answers import parse_answers
from tokenbill.copilot.handoff import (
    DEFAULT_EXPORT_KEY_FILE,
    ExportReport,
    read_bundle,
    read_team_map_csv,
)
from tokenbill.core import registry
from tokenbill.core.errors import UsageError

from .helpers import FIXTURES, NOW_MS, export, use_fake_registry, write_world


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    use_fake_registry(monkeypatch)
    return write_world(tmp_path / "in")


def test_report_and_sources(world: dict, tmp_path: Path) -> None:
    (world["dir"] / "notes.txt").write_text("not a report\n", encoding="utf-8")
    answers = parse_answers(FIXTURES / "answers_filled.json", snapshot_ms=NOW_MS)
    report = export(world, tmp_path / "out.tbx", answers=answers,
                    experimental=frozenset({"copilot-report-quota"}))
    assert isinstance(report, ExportReport) and report.out_path == tmp_path / "out.tbx"
    manifest = report.manifest
    assert manifest.experimental == ("copilot-report-quota",)
    adapters = {s.adapter: s for s in manifest.sources}
    assert adapters["admin-answers"].files == 1 and adapters["admin-answers"].records == 3
    assert adapters["github-copilot-activity-report"].records == 15
    dq = {n.code: n for n in report.dq}
    assert dq["dq.copilot_export_unrecognized_input"].count == 1
    assert dq["dq.copilot_activity_report_listed"].severity == "info"
    assert all(n.detail for n in report.dq)
    _, res = read_bundle(report.out_path)
    stated = [c for c in res.config if c.source_kind == "tokenbill.admin_answers"]
    assert [c.kind for c in stated] == ["org_settings", "org_settings", "run_flags"]
    assert DEFAULT_EXPORT_KEY_FILE.name == "copilot-export.key"


def test_window_is_applied(world: dict, tmp_path: Path) -> None:
    report = export(world, tmp_path / "w.tbx", since="2026-09-11", until="2026-09-30")
    _, res = read_bundle(report.out_path)
    assert {c.date_utc for c in res.cost_lines} == {"2026-09-11"}
    assert report.manifest.window == ("2026-09-11", "2026-09-30")
    assert dict(report.manifest.dq)["dq.copilot_export_outside_window"] > 0


def test_refused_inputs(world: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = export(world, tmp_path / "first.tbx")
    shutil.copy(first.out_path, world["dir"] / "again.tbx")
    with pytest.raises(UsageError, match="copilot-export files are never exported"):
        export(world, tmp_path / "second.tbx")
    (world["dir"] / "again.tbx").unlink()

    class Records:
        name = "github-usage-records"
        capabilities = frozenset()

        def sniff(self, path: Path, head: bytes) -> bool:
            return path.name == "records.json"

        def read(self, path: Path, opts: object) -> object:
            raise AssertionError("never read")

    (world["dir"] / "records.json").write_text("{}", encoding="utf-8")
    use_fake_registry(monkeypatch, Records)
    with pytest.raises(UsageError, match="raw request bodies"):
        export(world, tmp_path / "third.tbx")


def test_unavailable_adapters_are_counted_once(world: dict, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "BUILTIN_ADAPTERS", {"not-built-yet": "tokenbill.nope:Missing",
                                                       **registry.BUILTIN_ADAPTERS})
    report = export(world, tmp_path / "u.tbx")
    assert dict(report.manifest.dq)["dq.adapter_unavailable"] == 1


@pytest.mark.parametrize(("kw", "match"), [
    ({"key": b"short"}, "export key"),
    ({"k": 1}, "k must"),
    ({"since": "2026-9-1"}, "since"),
    ({"until": "2026-02-30"}, "until"),
    ({"since": "2026-09-10", "until": "2026-09-01"}, "after"),
    ({"experimental": frozenset({"copilot-everything"})}, "experimental"),
    ({"now_ms": -1}, "now_ms"),
    ({"team_map": {"login": 5}}, "team_map"),
    ({"cost_center_map": ["x"]}, "cost_center_map"),
    ({"answers": ["not a snapshot"]}, "ConfigSnapshot"),
])
def test_argument_errors(world: dict, tmp_path: Path, kw: dict, match: str) -> None:
    with pytest.raises(UsageError, match=match):
        export(world, tmp_path / "x.tbx", **kw)


def test_input_errors(world: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tokenbill.copilot.handoff import export_from_files

    kw = dict(key=b"k" * 32, team_map={}, cost_center_map={}, since=None, until=None,
              now_ms=NOW_MS, tool_version="t")
    with pytest.raises(UsageError, match="not found"):
        export_from_files([tmp_path / "missing"], tmp_path / "o.tbx", **kw)
    with pytest.raises(UsageError, match="no input files"):
        empty = tmp_path / "empty"
        empty.mkdir()
        export_from_files([empty], tmp_path / "o.tbx", **kw)
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "a.txt").write_text("hello", encoding="utf-8")
    with pytest.raises(UsageError, match="no input file was recognized"):
        export_from_files([junk], tmp_path / "o.tbx", **kw)
    with pytest.raises(UsageError, match="must not be one of the inputs"):
        export_from_files([world["dir"]], world["dir"] / "usage.csv", **kw)


# ---------------------------------------------------------------------------------------------
# team map CSV
# ---------------------------------------------------------------------------------------------


def csv_file(tmp_path: Path, text: str, name: str = "teams.csv") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_team_map_csv(tmp_path: Path) -> None:
    path = csv_file(tmp_path, "\ufeffLogin,Team,cost_center\nalice,Platform,CC1\n\n"
                              "bob,,x\ncarol,Data Science\n")
    assert read_team_map_csv(path) == {"alice": "Platform", "carol": "Data Science"}


@pytest.mark.parametrize(("text", "match"), [
    ("user,team\na,b\n", "header must be login,team"),
    ("", "header must be login,team"),
    ("login,team\n ,x\n", "line 2: blank login"),
    ("login,team\nalice,a\nALICE,b\n", "line 3: duplicate login"),
    ("login,team\nalice@corp.example,a\n", "line 2: e-mail-shaped login"),
    ("login,team\nalice,bad\x07team\n", "line 2: invalid team label"),
    ("login,team\nalice," + "t" * 129 + "\n", "line 2: invalid team label"),
])
def test_team_map_csv_errors(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(UsageError, match=match) as err:
        read_team_map_csv(csv_file(tmp_path, text))
    assert "alice" not in str(err.value).lower()


def test_team_map_csv_unreadable(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="not readable"):
        read_team_map_csv(tmp_path / "missing.csv")
    bad = tmp_path / "latin1.csv"
    bad.write_bytes(b"login,team\n\xe9,x\n")
    with pytest.raises(UsageError, match="not readable"):
        read_team_map_csv(bad)
    huge = csv_file(tmp_path, "login,team\nalice," + "x" * 200_000 + "\n", name="huge.csv")
    with pytest.raises(UsageError, match="malformed CSV"):
        read_team_map_csv(huge)
