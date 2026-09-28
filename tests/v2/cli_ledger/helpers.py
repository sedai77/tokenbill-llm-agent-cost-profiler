"""Shared helpers of the CLI-LEDGER tests (in-process ``main`` runs, subprocess runs, fixtures)."""

from __future__ import annotations

import contextlib
import datetime as _dt
import gzip
import io
import json
import os
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from tokenbill import cli

REPO = Path(__file__).resolve().parents[3]
FIXTURES = REPO / "tests" / "v2" / "fixtures"
CC_PROJECTS = FIXTURES / "claude_code" / "projects"
CC_HEADLESS = FIXTURES / "claude_code" / "headless"
ADMIN = FIXTURES / "admin"
DAY_MS = 86_400_000


def run_main(argv: Sequence[str]) -> tuple[int, str, str]:
    """Run ``cli.main`` in-process; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def run_subprocess(argv: Sequence[str], *, cwd: Path | None = None,
                   env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    """``python -m tokenbill ARGV`` importing this checkout's package."""
    environ = dict(os.environ)
    environ["PYTHONPATH"] = str(REPO) + os.pathsep + environ.get("PYTHONPATH", "")
    environ.update(env or {})
    return subprocess.run([sys.executable, "-m", "tokenbill", *argv], capture_output=True,
                          cwd=cwd or REPO, env=environ, timeout=600, check=False)


def ms_of(date: str) -> int:
    """Epoch ms of a UTC date."""
    return (_dt.date.fromisoformat(date) - _dt.date(1970, 1, 1)).days * DAY_MS


def freeze_now(monkeypatch: pytest.MonkeyPatch, ms: int) -> None:
    """Pin the wall clock the CLI reads (``time.time_ns``) to *ms*."""
    import time

    monkeypatch.setattr(time, "time_ns", lambda: ms * 1_000_000)


def init_dir(path: Path, **kw: Any) -> Path:
    """``tokenbill init`` into *path*; returns the config file."""
    from tokenbill.pipeline.ledger import run_init

    return run_init(path, **kw).config_path


def read_trace2(path: Path) -> list[dict[str, Any]]:
    """Every record of a (gzipped) trace@2 file."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:  # type: ignore[operator]
        return [json.loads(line) for line in fh if line.strip()]


def principals(records: Sequence[dict[str, Any]]) -> set[str]:
    """Every non-null ``principal`` anywhere in *records*."""
    found: set[str] = set()

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key == "principal" and isinstance(value, str):
                    found.add(value)
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(list(records))
    return found


@contextlib.contextmanager
def chdir(path: Path) -> Iterator[None]:
    """Temporarily change the working directory."""
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)
