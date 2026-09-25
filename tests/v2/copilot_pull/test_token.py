"""``TokenSource``: environment variable or private token file, re-read on every ``get()``, never
shown (addendum §5.12, §22.3; brief CP-PULL Build 3)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from tokenbill.copilot.pull_common import TokenSource
from tokenbill.core.errors import UsageError
from tokenbill.core.jsonl import ACL_WARNING

from .helpers import TOKEN_A, TOKEN_B, token_file


def test_file_is_reread_on_every_get(tmp_path: Path) -> None:
    path = token_file(tmp_path, TOKEN_A)
    src = TokenSource.from_file(path)
    assert src.get() == TOKEN_A
    path.write_text(f"\n  {TOKEN_B}  \nsecond line\n")
    assert src.get() == TOKEN_B
    path.write_bytes(b"\xef\xbb\xbf" + TOKEN_A.encode() + b"\r\n")
    assert src.get() == TOKEN_A


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o620, 0o602])
def test_a_token_file_readable_or_writable_by_others_is_refused(tmp_path: Path,
                                                                mode: int) -> None:
    src = TokenSource.from_file(token_file(tmp_path, TOKEN_A, mode=mode))
    with pytest.raises(UsageError, match="chmod 600") as info:
        src.get()
    assert TOKEN_A not in str(info.value)


def test_owner_only_modes_are_accepted(tmp_path: Path) -> None:
    assert TokenSource.from_file(token_file(tmp_path, TOKEN_A, mode=0o400)).get() == TOKEN_A


@pytest.mark.parametrize("content", [b"", b"\n\n", b"two words\n", b"\xff\xfe\x00",
                                     b"tab\tinside", "ümlaut".encode()])
def test_malformed_token_files(tmp_path: Path, content: bytes) -> None:
    path = token_file(tmp_path)
    path.write_bytes(content)
    with pytest.raises(UsageError, match="does not hold a token") as info:
        TokenSource.from_file(path).get()
    assert "two words" not in str(info.value)


def test_missing_directory_fifo_and_oversize(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="not found"):
        TokenSource.from_file(tmp_path / "nope").get()
    d = tmp_path / "dir"
    d.mkdir(mode=0o700)
    with pytest.raises(UsageError, match="regular file"):
        TokenSource.from_file(d).get()
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo, 0o600)
        with pytest.raises(UsageError, match="regular file"):
            TokenSource.from_file(fifo).get()
    big = token_file(tmp_path, "x" * 70_000)
    with pytest.raises(UsageError, match="too large"):
        TokenSource.from_file(big).get()
    locked = token_file(tmp_path / "l", TOKEN_A, mode=0o000)
    if os.geteuid() != 0:
        with pytest.raises(UsageError, match="unreadable"):
            TokenSource.from_file(locked).get()


def test_windows_skips_the_mode_check_with_an_acl_warning(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    src = TokenSource.from_file(token_file(tmp_path, TOKEN_A, mode=0o644), platform="nt")
    assert src.get() == TOKEN_A and src.get() == TOKEN_A
    assert src.warnings == (ACL_WARNING,)
    assert caplog.text.count(ACL_WARNING) == 1 and TOKEN_A not in caplog.text


def test_environment_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    src = TokenSource.from_env("TB_TEST_TOKEN_VAR")
    monkeypatch.setenv("TB_TEST_TOKEN_VAR", f" {TOKEN_A}\n")
    assert src.get() == TOKEN_A
    monkeypatch.setenv("TB_TEST_TOKEN_VAR", TOKEN_B)
    assert src.get() == TOKEN_B
    monkeypatch.setenv("TB_TEST_TOKEN_VAR", "   ")
    with pytest.raises(UsageError, match="not set"):
        src.get()
    monkeypatch.setenv("TB_TEST_TOKEN_VAR", "has space")
    with pytest.raises(UsageError, match="does not hold a token") as info:
        src.get()
    assert "has space" not in str(info.value)
    assert src.label == "environment variable TB_TEST_TOKEN_VAR" and src.warnings == ()


def test_constructor_validation(tmp_path: Path) -> None:
    for kwargs in ({}, {"env": "A", "file": tmp_path / "f"}):
        with pytest.raises(UsageError):
            TokenSource(**kwargs)  # type: ignore[arg-type]
    for bad in ("", "1ABC", "A-B", "A B", 5):
        with pytest.raises(UsageError, match="environment variable"):
            TokenSource.from_env(bad)  # type: ignore[arg-type]
    with pytest.raises(UsageError, match="names a file"):
        TokenSource(file=3.5)  # type: ignore[arg-type]


def test_repr_and_label_never_show_the_value(tmp_path: Path) -> None:
    src = TokenSource.from_file(token_file(tmp_path, TOKEN_A))
    src.get()
    assert TOKEN_A not in repr(src) and repr(src) == "TokenSource(token file gh-token)"
    assert src.label == "token file gh-token"
    assert not hasattr(src, "__dict__")  # slots: nowhere to cache a value
