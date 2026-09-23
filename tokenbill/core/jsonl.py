"""JSONL reading and private-file writing (SPEC §3.9, D44).

Readers stream line by line with a hard per-line cap (a line over :data:`MAX_LINE_BYTES` is yielded
as
``b""`` so callers quarantine it as ``oversize_line``), transparently decompress ``.gz`` (and
``.zst``
only when the standard library has ``compression.zstd``, Python ≥ 3.14), and never raise anything
but
``SourceError`` for unreadable input. Writers create files owner-only.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib
import json
import logging
import os
import re
import subprocess
import zlib
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import IO, Any

from tokenbill.core.errors import SourceError

__all__ = [
    "ACL_WARNING",
    "MAX_LINE_BYTES",
    "ZSTD_MESSAGE",
    "acl_warning",
    "head_sha",
    "iter_lines",
    "open_private",
    "open_text",
    "parse_json_line",
    "write_jsonl",
]

logger = logging.getLogger("tokenbill.core.jsonl")

MAX_LINE_BYTES = 16 * 2**20
HEAD_SHA_BYTES = 4096
ZSTD_MESSAGE = "zstd needs Python 3.14; set the collector fileexporter compression to none"
#: Data-quality code recorded when an owner-only ACL could not be applied on Windows (D44).
ACL_WARNING = "dq.windows_acl_not_enforced"

_CHUNK = 1 << 16
_SURROGATE_ESCAPE_RE = re.compile(rb"\\[uU][dD][89a-fA-F]")
_DECOMPRESSION_ERRORS: tuple[type[BaseException], ...] = (OSError, EOFError, zlib.error)

Runner = Callable[[Sequence[str]], int]


def _zstd_module() -> Any | None:
    try:
        return importlib.import_module("compression.zstd")
    except ImportError:
        return None


def _kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".gz":
        return "gz"
    if suffix == ".zst":
        return "zst"
    return "plain"


def head_sha(path: Path) -> str:
    """SHA-256 (hex) of the first 4 KiB of the file as stored on disk (rotation detection)."""
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read(HEAD_SHA_BYTES)).hexdigest()
    except OSError as exc:
        raise SourceError(f"{Path(path).name}: unreadable ({type(exc).__name__})") from None


def open_text(path: Path) -> IO[bytes]:
    """Open a source for byte-wise line reading: plain, ``.gz`` (gzip) or ``.zst``.

    ``.zst`` needs ``compression.zstd`` (Python ≥ 3.14); otherwise ``SourceError`` with
    :data:`ZSTD_MESSAGE`.
    """
    path = Path(path)
    kind = _kind(path)
    try:
        if kind == "gz":
            return gzip.open(path, "rb")
        if kind == "zst":
            zstd = _zstd_module()
            if zstd is None:
                raise SourceError(f"{path.name}: {ZSTD_MESSAGE}")
            return zstd.open(path, "rb")  # pragma: no cover - Python >= 3.14 only
        return open(path, "rb")
    except OSError as exc:
        raise SourceError(f"{path.name}: unreadable ({type(exc).__name__})") from None


def iter_lines(path: Path, *, start_offset: int = 0) -> Iterator[tuple[int, int, bytes]]:
    """Yield ``(line_no, byte_offset_of_line_start, raw_line)`` for every non-blank line.

    ``line_no`` counts from 1 at *start_offset*; offsets are in the (decompressed) stream. The line
    terminator is stripped; blank lines are skipped, so ``b""`` means only one thing: the line
    exceeded
    :data:`MAX_LINE_BYTES` (callers quarantine it as ``oversize_line``). Compressed files require
    ``start_offset == 0``.
    """
    path = Path(path)
    if start_offset < 0:
        raise ValueError("start_offset must be >= 0")
    if start_offset and _kind(path) != "plain":
        raise ValueError("compressed files require start_offset 0")
    f = open_text(path)
    try:
        if start_offset:
            f.seek(start_offset)
        offset = start_offset
        line_no = 0
        while True:
            try:
                chunk = f.readline(MAX_LINE_BYTES + 1)
            except _DECOMPRESSION_ERRORS as exc:
                raise SourceError(
                    f"{path.name}: corrupt stream at byte {offset} ({type(exc).__name__})"
                ) from None
            if not chunk:
                return
            line_no += 1
            start = offset
            offset += len(chunk)
            if len(chunk) > MAX_LINE_BYTES and not chunk.endswith(b"\n"):
                # oversize: skip the rest of the line without holding it in memory
                while True:
                    try:
                        rest = f.readline(_CHUNK)
                    except _DECOMPRESSION_ERRORS as exc:
                        raise SourceError(
                            f"{path.name}: corrupt stream at byte {offset} ({type(exc).__name__})"
                        ) from None
                    offset += len(rest)
                    if not rest or rest.endswith(b"\n"):
                        break
                yield line_no, start, b""
                continue
            line = chunk.rstrip(b"\r\n")
            if not line.strip():
                continue
            yield line_no, start, line
    finally:
        f.close()


def _has_lone_surrogate(value: Any) -> bool:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return True
        return False
    if isinstance(value, dict):
        return any(_has_lone_surrogate(k) or _has_lone_surrogate(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_lone_surrogate(v) for v in value)
    return False


def _reject_constant(token: str) -> Any:
    raise ValueError(f"non-finite number {token}")


def parse_json_line(raw: bytes) -> dict | None:
    """Parse one JSONL line: a ``dict``, or None for invalid UTF-8/JSON, non-objects, NaN/Infinity,
    excessive nesting and strings holding unpaired surrogates."""
    if not raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if text.startswith("﻿"):
        text = text[1:]
    try:
        obj = json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict):
        return None
    if _SURROGATE_ESCAPE_RE.search(raw) and _has_lone_surrogate(obj):
        return None
    return obj


def _default_runner(argv: Sequence[str]) -> int:
    try:
        proc = subprocess.run(list(argv), capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return -1
    return proc.returncode


def _make_private_dirs(directory: Path, *, posix: bool) -> None:
    missing: list[Path] = []
    d = directory
    while not d.exists():
        missing.append(d)
        if d.parent == d:
            break
        d = d.parent
    for d in reversed(missing):
        try:
            d.mkdir(mode=0o700)
        except FileExistsError:
            continue
        if posix:
            os.chmod(d, 0o700)


def _windows_user() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or ""


def _apply_windows_acl(path: Path, runner: Runner) -> bool:
    user = _windows_user()
    if not user:
        return False
    code = runner(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"])
    return code == 0


def acl_warning(handle: IO[Any]) -> str | None:
    """The data-quality code recorded on a handle from :func:`open_private` (None when enforced)."""
    return getattr(handle, "tokenbill_acl_warning", None)


def open_private(
    path: Path, mode: str = "w", *, runner: Runner | None = None, platform: str | None = None
) -> IO[Any]:
    """Open *path* for writing, owner-only.

    POSIX: the file is created (or re-moded) ``0600`` and missing parent directories ``0700``.
    Windows
    (``platform == "nt"``): POSIX modes are no-ops, so an owner-only ACL is applied with ``icacls``
    through
    *runner* (injectable; returns the exit code). When that fails the handle's
    ``tokenbill_acl_warning`` attribute is :data:`ACL_WARNING` (``dq.windows_acl_not_enforced``);
    read it
    with :func:`acl_warning`.
    """
    path = Path(path)
    plat = platform if platform is not None else os.name
    posix = plat != "nt"
    base = mode.replace("b", "").replace("t", "")
    flags = {
        "w": os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        "a": os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        "x": os.O_WRONLY | os.O_CREAT | os.O_EXCL,
    }.get(base)
    if flags is None:
        raise ValueError("open_private supports modes w, a, x (text or binary)")
    flags |= getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
    _make_private_dirs(path.parent, posix=posix)
    fd = os.open(path, flags, 0o600)
    try:
        if posix and hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        if "b" in mode:
            handle: IO[Any] = os.fdopen(fd, mode)
        else:
            handle = os.fdopen(fd, mode, encoding="utf-8", newline="")
    except BaseException:
        os.close(fd)
        raise
    warning = None
    if not posix:
        if not _apply_windows_acl(path, runner or _default_runner):
            warning = ACL_WARNING
            logger.warning("%s: owner-only ACL not enforced (%s)", path.name, ACL_WARNING)
    try:
        handle.tokenbill_acl_warning = warning  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - every io class accepts attributes
        pass
    return handle


def _canonical(record: Mapping[str, Any]) -> str:
    return json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def write_jsonl(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    """Write canonical JSONL (sorted keys, no spaces, one record per line) to an owner-only file;
    ``.gz`` paths are gzip-compressed deterministically (no name, mtime 0)."""
    path = Path(path)
    if _kind(path) == "gz":
        with (
            open_private(path, "wb") as raw,
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz,
        ):
            for record in records:
                gz.write((_canonical(record) + "\n").encode("utf-8"))
        return
    with open_private(path, "w") as f:
        for record in records:
            f.write(_canonical(record) + "\n")
