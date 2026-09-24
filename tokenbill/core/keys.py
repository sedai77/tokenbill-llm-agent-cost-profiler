"""Key files: the install key, org keys and collection keys (SPEC §3.23, §8.2, D5, D39, D44).

A key file holds 32 random bytes (``secrets.token_bytes``) written as 64 lowercase hex characters
and a newline. :func:`load` also accepts a file of raw key bytes (≥ 32). Files are created ``0600``
inside a ``0700`` directory (``core.jsonl.open_private``). On POSIX, :func:`load` refuses a key file
that any group or other user may access; on Windows (where POSIX modes are no-ops) it re-applies the
best-effort owner-only ACL and records ``dq.windows_acl_not_enforced`` when that fails. Key roles
(install / org / collection) are a matter of which file is passed, never of the format. Hashes are
only ever compared within one :func:`key_id`.
"""

from __future__ import annotations

import binascii
import logging
import os
import secrets
import stat
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from tokenbill.core.errors import PrivacyError, UsageError
from tokenbill.core.ids import key_id
from tokenbill.core.jsonl import ACL_WARNING, acl_warning, open_private
from tokenbill.core.types import DataQualityNote

__all__ = ["DEFAULT_KEY_PATH", "KEY_BYTES", "key_id", "load", "load_or_create"]

logger = logging.getLogger("tokenbill.core.keys")

#: The install key (local self-view; name key and principal key of ``install`` identity mode).
DEFAULT_KEY_PATH = Path("~/.config/tokenbill/key")
#: Bytes of key material created by :func:`load_or_create` and required by :func:`load`.
KEY_BYTES = 32
_MAX_KEY_FILE_BYTES = 4096

Runner = Callable[[Sequence[str]], int]


def _platform(platform: str | None) -> str:
    return platform if platform is not None else os.name


def _acl_note(path: Path) -> DataQualityNote:
    return DataQualityNote(
        code=ACL_WARNING,
        severity="warn",
        count=1,
        detail=f"key file {path.name}: owner-only ACL not enforced; keep it in an MDM-restricted "
        "directory",
    )


def _default_runner(argv: Sequence[str]) -> int:
    import subprocess

    try:
        proc = subprocess.run(list(argv), capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return -1
    return proc.returncode


def _windows_owner_acl(path: Path, runner: Runner | None) -> bool:
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if not user:
        return False
    run = runner if runner is not None else _default_runner
    return run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"]) == 0


_HEX_DIGITS = frozenset(b"0123456789abcdefABCDEF")


def _decode(raw: bytes, path: Path) -> bytes:
    """Hex text (the format :func:`load_or_create` writes) or raw key bytes; ≥ 32 bytes either way.
    Content that is entirely hex digits is always read as hex, so a short hex key is refused rather
    than silently used as ASCII."""
    text = raw.strip()
    key = raw
    if text and all(c in _HEX_DIGITS for c in text):
        key = binascii.unhexlify(text) if len(text) % 2 == 0 else b""
    if len(key) < KEY_BYTES:
        raise UsageError(f"key file {path.name}: expected {KEY_BYTES} bytes of key material "
                         f"({2 * KEY_BYTES} hex characters)")
    return key


def load(
    path: Path,
    *,
    runner: Runner | None = None,
    platform: str | None = None,
    notes: list[DataQualityNote] | None = None,
) -> bytes:
    """Read an org or collection key (or the install key) from *path*.

    POSIX: a file that grants any permission to group or others is refused with ``PrivacyError``
    (``chmod 600`` it). Windows (``platform == "nt"``): the owner-only ACL is re-applied with
    ``icacls`` through *runner*; when that fails a ``dq.windows_acl_not_enforced`` note is appended
    to *notes* (and logged). A missing or unreadable file, or one without enough key material,
    raises ``UsageError``. The key bytes are never logged.
    """
    path = Path(path).expanduser()
    try:
        st = os.stat(path)
    except OSError:
        raise UsageError(f"key file {path.name}: not found or unreadable") from None
    _check_stat(st, path, platform)  # before opening: never block on a FIFO
    flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NONBLOCK", 0))
    try:
        fd = os.open(path, flags)
    except OSError:
        raise UsageError(f"key file {path.name}: not readable") from None
    try:
        # The checks apply to the file actually read (no stat-then-open race).
        _check_stat(os.fstat(fd), path, platform)
        chunks: list[bytes] = []
        size = 0
        while size <= _MAX_KEY_FILE_BYTES:
            chunk = os.read(fd, _MAX_KEY_FILE_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    except OSError:
        raise UsageError(f"key file {path.name}: not readable") from None
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if len(raw) > _MAX_KEY_FILE_BYTES:
        raise UsageError(f"key file {path.name}: too large to be a key")
    if _platform(platform) == "nt" and not _windows_owner_acl(path, runner):
        logger.warning("%s: owner-only ACL not enforced (%s)", path.name, ACL_WARNING)
        if notes is not None:
            notes.append(_acl_note(path))
    return _decode(raw, path)


def _check_stat(st: os.stat_result, path: Path, platform: str | None) -> None:
    """A key file is a regular file of at most 4 KiB; on POSIX no group/other permission bit."""
    if not stat.S_ISREG(st.st_mode):
        raise UsageError(f"key file {path.name}: not a regular file")
    if st.st_size > _MAX_KEY_FILE_BYTES:
        raise UsageError(f"key file {path.name}: too large to be a key")
    if _platform(platform) != "nt" and st.st_mode & 0o077:
        raise PrivacyError(
            f"key file {path.name}: accessible by group or others "
            f"(mode {stat.S_IMODE(st.st_mode):04o}); run chmod 600"
        )


def _load_settled(target: Path, *, runner: Runner | None, platform: str | None,
                  notes: list[DataQualityNote] | None, attempts: int = 20,
                  pause_s: float = 0.05) -> bytes:
    """:func:`load` for a key another process may still be writing in place (the no-hard-link
    fallback): a short read is retried for up to ``attempts × pause_s`` seconds."""
    for _ in range(attempts - 1):
        try:
            return load(target, runner=runner, platform=platform, notes=notes)
        except UsageError:
            time.sleep(pause_s)
    return load(target, runner=runner, platform=platform, notes=notes)


def load_or_create(
    path: Path | None = None,
    *,
    runner: Runner | None = None,
    platform: str | None = None,
    notes: list[DataQualityNote] | None = None,
) -> bytes:
    """The key at *path* (default ``~/.config/tokenbill/key``, the install key), created on first
    use.

    Creation writes 32 bytes from ``secrets.token_bytes`` as hex into a private temporary file
    (``0600``, missing directories ``0700``), flushes it, and publishes it under *path* with a
    hard link, which fails when *path* exists: a concurrent creator's key wins and is loaded, and
    no reader ever sees a partially written key. Where hard links are unavailable the key is
    written with ``O_EXCL`` directly. On Windows the owner-only ACL is attempted and a
    ``dq.windows_acl_not_enforced`` note is appended to *notes* when it fails. An existing file is
    read with :func:`load` (same permission checks).
    """
    target = Path(path if path is not None else DEFAULT_KEY_PATH).expanduser()
    if target.exists():
        return load(target, runner=runner, platform=platform, notes=notes)
    key = secrets.token_bytes(KEY_BYTES)
    text = key.hex() + "\n"
    tmp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    try:
        handle = open_private(tmp, "x", runner=runner, platform=platform)
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            warning = acl_warning(handle)
        try:
            os.link(tmp, target)
        except FileExistsError:
            return load(target, runner=runner, platform=platform, notes=notes)
        except (AttributeError, NotImplementedError, OSError):
            # No hard links on this file system: write the key exclusively in place.
            try:
                handle = open_private(target, "x", runner=runner, platform=platform)
            except FileExistsError:
                return _load_settled(target, runner=runner, platform=platform, notes=notes)
            with handle:
                handle.write(text)
                warning = acl_warning(handle)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    if warning is not None and notes is not None:
        notes.append(_acl_note(target))
    return key
