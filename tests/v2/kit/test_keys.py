"""Key files (SPEC §3.23, §8.2, D44; F-KIT acceptance)."""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.core import keys
from tokenbill.core.errors import PrivacyError, TokenbillError, UsageError
from tokenbill.core.ids import key_id
from tokenbill.core.jsonl import ACL_WARNING

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX modes are no-ops on Windows")


class Runner:
    def __init__(self, code: int) -> None:
        self.code = code
        self.calls: list[list[str]] = []

    def __call__(self, argv) -> int:
        self.calls.append(list(argv))
        return self.code


@posix_only
def test_load_or_create_modes(tmp_path: Path) -> None:
    path = tmp_path / "cfg" / "tokenbill" / "key"
    key = keys.load_or_create(path)
    assert len(key) == keys.KEY_BYTES == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "cfg").stat().st_mode) == 0o700
    assert path.read_text() == key.hex() + "\n"
    assert keys.load_or_create(path) == key == keys.load(path)
    assert keys.key_id is key_id and key_id(key).startswith("k_")


def test_default_path_is_the_install_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    key = keys.load_or_create()
    assert (tmp_path / ".config" / "tokenbill" / "key").exists()
    assert keys.load_or_create() == key
    assert keys.DEFAULT_KEY_PATH == Path("~/.config/tokenbill/key")


@posix_only
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o620, 0o644])
def test_group_or_world_access_is_refused(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "org.key"
    path.write_text("ab" * 32)
    os.chmod(path, mode)
    with pytest.raises(PrivacyError, match="chmod 600"):
        keys.load(path)
    with pytest.raises(PrivacyError):
        keys.load_or_create(path)


def test_windows_path_warns_through_the_fake_runner(tmp_path: Path,
                                                    caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "win" / "key"
    notes: list = []
    failing = Runner(5)
    with caplog.at_level(logging.WARNING):
        key = keys.load_or_create(path, runner=failing, platform="nt", notes=notes)
    assert len(key) == 32 and [n.code for n in notes] == [ACL_WARNING]
    assert failing.calls and failing.calls[0][0] == "icacls"
    notes.clear()
    os.chmod(path, 0o644)  # POSIX bits are ignored on Windows
    assert keys.load(path, runner=failing, platform="nt", notes=notes) == key
    assert [n.code for n in notes] == [ACL_WARNING] and "key" in notes[0].detail
    assert key.hex() not in caplog.text
    notes.clear()
    assert keys.load(path, runner=Runner(0), platform="nt", notes=notes) == key
    assert notes == []
    assert keys.load(path, runner=failing, platform="nt") == key  # no notes list: logged only


def test_windows_without_a_user_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "key"
    path.write_text("cd" * 32)
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    notes: list = []
    runner = Runner(0)
    keys.load(path, runner=runner, platform="nt", notes=notes)
    assert runner.calls == [] and [n.code for n in notes] == [ACL_WARNING]


def test_default_runner_is_harmless(tmp_path: Path) -> None:
    assert keys._default_runner(["tokenbill-no-such-binary-xyz"]) == -1


@posix_only
def test_formats(tmp_path: Path) -> None:
    raw = bytes(range(200, 240))
    path = tmp_path / "raw.key"
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    assert keys.load(path) == raw
    upper = tmp_path / "upper.key"
    upper.write_text("  " + "AB" * 40 + "\r\n")
    os.chmod(upper, 0o600)
    assert keys.load(upper) == bytes.fromhex("ab" * 40)


@posix_only
@pytest.mark.parametrize("content", [b"", b"ab" * 16, b"abc" * 30 + b"a", b"short key"])
def test_short_or_malformed_keys_are_refused(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "bad.key"
    path.write_bytes(content)
    os.chmod(path, 0o600)
    with pytest.raises(UsageError):
        keys.load(path)


def test_missing_directory_and_huge_files(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="not found"):
        keys.load(tmp_path / "absent")
    with pytest.raises(UsageError, match="regular file"):
        keys.load(tmp_path)
    big = tmp_path / "big.key"
    big.write_bytes(b"a" * 5000)
    with pytest.raises(UsageError, match="too large"):
        keys.load(big, platform="nt", runner=Runner(0))


@posix_only
def test_unreadable_file(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    path = tmp_path / "locked.key"
    path.write_text("ab" * 32)
    os.chmod(path, 0o000)
    try:
        with pytest.raises(UsageError, match="not readable"):
            keys.load(path)
    finally:
        os.chmod(path, 0o600)


def test_creation_race_loads_the_winner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "race.key"
    winner = bytes(range(32))

    def racing_open(target, mode="w", **kw):
        target.write_text(winner.hex() + "\n")
        os.chmod(target, 0o600)
        raise FileExistsError(str(target))

    monkeypatch.setattr(keys, "open_private", racing_open)
    assert keys.load_or_create(path, platform="posix") == winner


@posix_only
@settings(max_examples=150, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(content=st.binary(max_size=300))
def test_fuzz_key_file_parser(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "fuzz.key"
    path.write_bytes(content)
    os.chmod(path, 0o600)
    try:
        key = keys.load(path)
    except TokenbillError:
        return
    assert isinstance(key, bytes) and len(key) >= keys.KEY_BYTES
