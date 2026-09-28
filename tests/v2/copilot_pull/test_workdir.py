"""``private_workdir`` for the admin handoff: 0700, removed on every exit path (normal, exception,
``KeyboardInterrupt``), moved to ``--keep-raw``, refused before any request when ``--keep-raw``
already holds files, reused with ``--resume`` (addendum §5.12 handoff output; brief Build 7)."""

from __future__ import annotations

import logging
import shutil
import stat
from collections.abc import Sequence
from pathlib import Path

import pytest

from tokenbill.copilot import pull_common
from tokenbill.copilot.pull_common import (
    HANDOFF_KINDS,
    MANIFEST_NAME,
    Manifest,
    TokenExpired,
    private_workdir,
    pull,
    require_complete,
)
from tokenbill.core.errors import GateFailed, SourceError, UsageError
from tokenbill.core.jsonl import ACL_WARNING

from .helpers import API, ENT, NOW_MS, ORG, Call, FakeGitHub, World, source


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def handoff_pull(tmp_path: Path, gh: FakeGitHub, work: Path, *, resume: bool = False) -> Manifest:
    return pull(HANDOFF_KINDS, enterprise=ENT, orgs=[ORG], since="2026-09-01",
                until="2026-09-01", token=source(tmp_path), out_dir=work, opener=gh,
                sleep=lambda s: None, now_ms=NOW_MS, resume=resume)


def test_mode_0700_and_removed_after_normal_exit(tmp_path: Path) -> None:
    with private_workdir(parent=tmp_path) as work:
        assert work.parent == tmp_path and work.is_dir() and mode(work) == 0o700
        (work / "sub").mkdir()
        (work / "sub" / "f.jsonl").write_text("x")
        (work / "sub" / "f.jsonl").chmod(0o400)
        (work / "sub").chmod(0o500)
    assert not work.exists()


def test_removed_after_an_exception_and_after_keyboard_interrupt(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError), private_workdir(parent=tmp_path) as work:
        (work / "f").write_text("x")
        raise RuntimeError("boom")
    assert not work.exists()
    with pytest.raises(KeyboardInterrupt), private_workdir(parent=tmp_path) as work2:
        raise KeyboardInterrupt
    assert not work2.exists()
    assert list(tmp_path.iterdir()) == []


def test_a_real_pull_in_the_workdir_is_deleted_with_it(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)
    with private_workdir(parent=tmp_path / "tmp") as work:
        manifest = handoff_pull(tmp_path, gh, work)
        require_complete(manifest, keep_raw=False)
        assert manifest.files(work)
    assert not work.exists() and list((tmp_path / "tmp").iterdir()) == []


def test_token_expiry_without_keep_raw_deletes_the_raw_data(tmp_path: Path) -> None:
    gh = FakeGitHub()
    World(gh)

    def expire(call: Call) -> None:
        if call.host == API and len(gh.api_calls()) == 5:
            gh.valid = set()

    gh.hook = expire
    with pytest.raises(TokenExpired), private_workdir(parent=tmp_path / "tmp") as work:
        handoff_pull(tmp_path, gh, work)
    assert not work.exists()


def test_keep_raw_moves_the_directory_and_resume_continues_it(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    keep = tmp_path / "kept" / "raw"
    gh = FakeGitHub()
    World(gh)

    def expire(call: Call) -> None:
        if call.host == API and len(gh.api_calls()) == 5:
            gh.valid = set()

    gh.hook = expire
    with pytest.raises(TokenExpired), private_workdir(parent=tmp_path / "tmp",
                                                      keep_raw=keep) as work:
        handoff_pull(tmp_path, gh, work)
    assert not work.exists() and keep.is_dir() and mode(keep) == 0o700
    assert (keep / MANIFEST_NAME).is_file() and "logins" in caplog.text
    first = Manifest.load(keep / MANIFEST_NAME)
    assert first.incomplete
    # resume in the kept directory: it is used in place and left there
    gh2 = FakeGitHub()
    World(gh2)
    with private_workdir(parent=tmp_path / "tmp", keep_raw=keep, resume=True) as work2:
        assert work2 == keep
        final = handoff_pull(tmp_path, gh2, work2, resume=True)
    assert keep.is_dir() and final.complete
    done_first = {u.id for u in first.units if u.status == "complete"}
    assert done_first and not ({c.path for c in gh2.api_calls()}
                               & {f"/enterprises/{ENT}/settings/billing/budgets"})


def test_keep_raw_after_success_and_into_an_empty_directory(tmp_path: Path) -> None:
    keep = tmp_path / "raw"
    keep.mkdir()
    with private_workdir(parent=tmp_path / "tmp", keep_raw=keep) as work:
        (work / "a.jsonl").write_text("x")
    assert (keep / "a.jsonl").read_text() == "x" and not work.exists()


def test_existing_non_empty_keep_raw_is_refused_before_the_first_request(
        tmp_path: Path) -> None:
    keep = tmp_path / "raw"
    keep.mkdir()
    (keep / "old.csv").write_text("x")
    gh = FakeGitHub()
    World(gh)
    with pytest.raises(UsageError, match="already holds files"):
        with private_workdir(parent=tmp_path, keep_raw=keep) as work:
            handoff_pull(tmp_path, gh, work)
    assert gh.calls == []
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("tokenbill-pull-")] == []
    # without a manifest, --resume does not make it acceptable either
    with pytest.raises(UsageError), private_workdir(parent=tmp_path, keep_raw=keep,
                                                    resume=True):
        pass
    (tmp_path / "file").write_text("x")
    with pytest.raises(UsageError, match="new or empty directory"):
        with private_workdir(parent=tmp_path, keep_raw=tmp_path / "file"):
            pass
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "missing-target")
    with pytest.raises(UsageError), private_workdir(parent=tmp_path, keep_raw=link):
        pass


def test_windows_acl_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERNAME", "admin")
    calls: list[Sequence[str]] = []

    def good(argv: Sequence[str]) -> int:
        calls.append(argv)
        return 0

    notes: list[str] = []
    with private_workdir(parent=tmp_path, platform="nt", runner=good, notes=notes) as work:
        pass
    assert notes == [] and calls and calls[0][0] == "icacls" and str(work) in calls[0]
    assert "admin:(OI)(CI)F" in calls[0]
    with private_workdir(parent=tmp_path, platform="nt", runner=lambda a: 5, notes=notes):
        pass
    assert notes == [ACL_WARNING]
    monkeypatch.delenv("USERNAME")
    monkeypatch.delenv("USER", raising=False)
    notes2: list[str] = []
    with private_workdir(parent=tmp_path, platform="nt", runner=good, notes=notes2):
        pass
    assert notes2 == [ACL_WARNING]


def test_default_runner_never_raises() -> None:
    assert pull_common._default_runner(["/nonexistent/tokenbill-icacls"]) == -1


def test_a_failed_move_deletes_the_raw_data(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_move(src: str, dst: str) -> None:
        raise OSError("cross-device")

    monkeypatch.setattr(shutil, "move", broken_move)
    with pytest.raises(SourceError, match="deleted"):
        with private_workdir(parent=tmp_path / "tmp", keep_raw=tmp_path / "keep") as work:
            (work / "f").write_text("x")
    assert not work.exists()
    with pytest.raises(RuntimeError, match="inner"):
        with private_workdir(parent=tmp_path / "tmp", keep_raw=tmp_path / "keep2") as work2:
            raise RuntimeError("inner")
    assert not work2.exists()


def test_an_undeletable_workdir_is_reported(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pull_common, "_rmtree", lambda p: None)
    with pytest.raises(SourceError, match="could not be deleted"):
        with private_workdir(parent=tmp_path) as work:
            pass
    shutil.rmtree(work)

    def failing(p: Path) -> None:
        raise OSError("busy")

    monkeypatch.setattr(pull_common, "_rmtree", failing)
    with pytest.raises(ValueError), private_workdir(parent=tmp_path) as work2:
        raise ValueError("original error wins")
    shutil.rmtree(work2)


def test_require_complete_messages() -> None:
    manifest = Manifest(enterprise=ENT, orgs=(), since="2026-09-01", until="2026-09-01",
                        kinds=("seats",))
    require_complete(manifest, keep_raw=False)
    with pytest.raises(GateFailed):
        require_complete(Manifest.from_json({**manifest.to_json(), "units": [
            {"id": "seats/enterprise", "kind": "seats", "status": "incomplete", "files": [],
             "reason": "token_expired"}]}), keep_raw=False)
