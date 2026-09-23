#!/usr/bin/env python3
"""Capture the v0.1.2 golden outputs of ``tokenbill demo`` and ``tokenbill analyze`` (SPEC §16.2).

Runs the *untouched* v0.1.2 CLI in-process and stores every stdout and HTML report under
``tests/v2/golden/`` with the version string and the report date replaced by placeholders:

- ``demo`` (default), ``demo -o report.html``, each ``--scenario`` (stdout and HTML),
  ``--seed 7`` and ``--seed 11`` (stdout and HTML);
- ``analyze`` on each of the four demo scenarios (seed 7) written with the frozen
  ``trace.write_trace``, and on all four together (stdout and HTML).

``manifest.json`` lists every case: its argv (relative to a scratch working directory), exit code,
the golden files, and the sha256 of every regenerated trace input, so the comparison test
(CLI-LEDGER) can re-run the same argv against the current CLI and diff after :func:`normalize`.

Usage::

    python scripts/capture_goldens.py [--out tests/v2/golden] [--check]

``--check`` recaptures into memory and exits 1 if any stored golden differs (used to prove the
capture is deterministic). The script imports only frozen v0.1 modules and ``tokenbill.cli``.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import hashlib
import io
import json
import os
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:  # run as a script from any cwd
    sys.path.insert(0, str(REPO_ROOT))

VERSION_PLACEHOLDER = "{{TOKENBILL_VERSION}}"
DATE_PLACEHOLDER = "{{REPORT_DATE}}"
#: The report date pinned during capture. A sentinel that cannot occur in demo/analyze content, so
#: replacing it never touches anything but the rendered report date.
SENTINEL_DATE = _dt.date(1999, 12, 31)
GOLDEN_SCHEMA = "tokenbill/goldens@1"
SCENARIO_SEED = 7


def normalize(text: str, *, version: str, report_date: str) -> str:
    """Replace the version string and the report date with the golden placeholders.

    The comparison test applies exactly this function to the current CLI's output (with the current
    ``__version__`` and ``date.today().isoformat()``) before comparing bytes.
    """
    return text.replace(version, VERSION_PLACEHOLDER).replace(report_date, DATE_PLACEHOLDER)


class _PinnedDate(_dt.date):
    """``date`` subclass whose ``today()`` is the capture sentinel."""

    @classmethod
    def today(cls) -> _PinnedDate:  # type: ignore[override]
        return cls(SENTINEL_DATE.year, SENTINEL_DATE.month, SENTINEL_DATE.day)


@contextlib.contextmanager
def _pinned_report_date() -> Iterator[None]:
    import tokenbill.cli as cli

    original = cli.date
    cli.date = _PinnedDate  # type: ignore[misc]
    try:
        yield
    finally:
        cli.date = original  # type: ignore[misc]


@contextlib.contextmanager
def _chdir(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def cases(scenarios: list[str]) -> list[dict[str, object]]:
    """Every golden case: id, argv, and the HTML report file name the argv writes (or None)."""
    out: list[dict[str, object]] = [
        {"id": "demo", "argv": ["demo"], "html": None},
        {"id": "demo_o", "argv": ["demo", "-o", "report.html"], "html": "report.html"},
    ]
    for name in scenarios:
        out.append(
            {"id": f"demo_scenario_{name}", "argv": ["demo", "--scenario", name], "html": None}
        )
        out.append(
            {
                "id": f"demo_scenario_{name}_o",
                "argv": ["demo", "--scenario", name, "-o", "report.html"],
                "html": "report.html",
            }
        )
    for seed in (7, 11):
        out.append({"id": f"demo_seed{seed}", "argv": ["demo", "--seed", str(seed)], "html": None})
        out.append(
            {
                "id": f"demo_seed{seed}_o",
                "argv": ["demo", "--seed", str(seed), "-o", "report.html"],
                "html": "report.html",
            }
        )
    for name in scenarios:
        out.append({"id": f"analyze_{name}", "argv": ["analyze", f"{name}.jsonl"], "html": None})
        out.append(
            {
                "id": f"analyze_{name}_o",
                "argv": ["analyze", f"{name}.jsonl", "-o", "report.html"],
                "html": "report.html",
            }
        )
    every = [f"{name}.jsonl" for name in scenarios]
    out.append({"id": "analyze_all", "argv": ["analyze", *every], "html": None})
    out.append(
        {
            "id": "analyze_all_o",
            "argv": ["analyze", *every, "-o", "report.html"],
            "html": "report.html",
        }
    )
    return out


def write_inputs(work: Path) -> dict[str, dict[str, object]]:
    """Write the four demo scenarios (seed 7) with the frozen ``trace.write_trace``."""
    from tokenbill.demo_traces import SCENARIOS, scenario
    from tokenbill.trace import write_trace

    inputs: dict[str, dict[str, object]] = {}
    for name in SCENARIOS:
        path = work / f"{name}.jsonl"
        write_trace(path, scenario(name, SCENARIO_SEED))
        inputs[path.name] = {
            "scenario": name,
            "seed": SCENARIO_SEED,
            "writer": "tokenbill.trace.write_trace",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    return inputs


def run_case(main: Callable[[list[str]], int], argv: list[str]) -> tuple[int, str, str]:
    """Run the CLI in-process; return (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


def capture() -> tuple[dict[str, object], dict[str, str]]:
    """Capture every case; return (manifest, {golden file name: normalized text})."""
    from tokenbill import __version__
    from tokenbill.cli import main
    from tokenbill.demo_traces import SCENARIOS

    sentinel = SENTINEL_DATE.isoformat()
    files: dict[str, str] = {}
    manifest_cases: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="tokenbill-goldens-") as tmp:
        work = Path(tmp)
        inputs = write_inputs(work)
        with _chdir(work), _pinned_report_date():
            for case in cases(list(SCENARIOS)):
                argv = list(case["argv"])  # type: ignore[call-overload]
                html_name = case["html"]
                report = work / "report.html"
                if report.exists():
                    report.unlink()
                code, stdout, stderr = run_case(main, argv)
                if code != 0:
                    raise SystemExit(f"case {case['id']} exited {code}: {stderr.strip()}")
                entry: dict[str, object] = {
                    "id": case["id"],
                    "argv": argv,
                    "exit_code": code,
                    "stdout": f"{case['id']}.stdout.txt",
                    "stderr_empty": stderr == "",
                    "html": None,
                }
                files[f"{case['id']}.stdout.txt"] = normalize(
                    stdout, version=__version__, report_date=sentinel
                )
                if html_name is not None:
                    html = report.read_text(encoding="utf-8")
                    files[f"{case['id']}.report.html"] = normalize(
                        html, version=__version__, report_date=sentinel
                    )
                    entry["html"] = f"{case['id']}.report.html"
                manifest_cases.append(entry)
    for name, text in files.items():
        if not text:
            raise SystemExit(f"golden {name} is empty")
    manifest: dict[str, object] = {
        "schema": GOLDEN_SCHEMA,
        "captured_from_version": __version__,
        "placeholders": {"version": VERSION_PLACEHOLDER, "report_date": DATE_PLACEHOLDER},
        "normalization": (
            "text.replace(__version__, version placeholder)"
            ".replace(date.today().isoformat(), report_date placeholder)"
        ),
        "cwd": "a scratch directory holding the inputs; -o writes report.html there",
        "inputs": inputs,
        "cases": manifest_cases,
        "sha256": {
            name: hashlib.sha256(text.encode("utf-8")).hexdigest()
            for name, text in sorted(files.items())
        },
    }
    return manifest, files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "tests" / "v2" / "golden")
    parser.add_argument(
        "--check",
        action="store_true",
        help="compare a fresh capture with the stored goldens; exit 1 on drift",
    )
    args = parser.parse_args(argv)
    manifest, files = capture()
    manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.check:
        drift = [
            name
            for name, text in files.items()
            if not (args.out / name).exists()
            or (args.out / name).read_text(encoding="utf-8") != text
        ]
        stored = args.out / "manifest.json"
        if not stored.exists() or stored.read_text(encoding="utf-8") != manifest_text:
            drift.append("manifest.json")
        for name in drift:
            print(f"drift: {name}", file=sys.stderr)
        return 1 if drift else 0
    args.out.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (args.out / name).write_text(text, encoding="utf-8", newline="")
    (args.out / "manifest.json").write_text(manifest_text, encoding="utf-8", newline="")
    print(f"captured {len(files)} golden files into {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
