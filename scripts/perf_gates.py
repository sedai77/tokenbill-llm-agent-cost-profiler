#!/usr/bin/env python3
"""Full-size performance gates (SPEC §8.10, INTEGRATION item 2): run the ``perf``-marked tests and
print a summary table. Standard library only (pytest is the test runner it drives).

The ``perf`` tests carry their own budgets (e.g. a one-million-request replay within 30 s) and fail
when they exceed them; they are excluded from the default run (``addopts = -m 'not perf'``) and run
nightly (``.github/workflows/perf.yml``). This script runs ``python -m pytest -m perf`` with a JUnit
XML report, then prints one row per gate — outcome, wall time and, for a failure, the first line
of its message — and exits with pytest's status (``5``, nothing collected, is a failure unless
``--allow-empty``).

Usage::

    python scripts/perf_gates.py                      # run every perf gate
    python scripts/perf_gates.py --list               # collect only
    python scripts/perf_gates.py --markdown "$GITHUB_STEP_SUMMARY" -- -k sim
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NO_TESTS_COLLECTED = 5


@dataclass(frozen=True)
class Gate:
    test: str
    outcome: str  # passed | failed | error | skipped
    seconds: float
    message: str


def _test_id(case: ET.Element) -> str:
    classname, name = case.get("classname", ""), case.get("name", "")
    parts = classname.split(".")
    # pytest's classname is "tests.v2.sim.test_perf" (+ ".TestClass"); rebuild a node id.
    for i in range(len(parts), 0, -1):
        candidate = REPO / Path(*parts[:i]).with_suffix(".py")
        if candidate.is_file():
            path = "/".join(parts[:i]) + ".py"
            return "::".join([path, *parts[i:], name])
    return f"{classname}::{name}" if classname else name


def parse_junit(path: Path) -> list[Gate]:
    gates = []
    for case in ET.parse(path).getroot().iter("testcase"):
        outcome, message = "passed", ""
        for tag in ("failure", "error", "skipped"):
            node = case.find(tag)
            if node is not None:
                outcome = "failed" if tag == "failure" else tag
                text = node.get("message") or (node.text or "")
                message = text.strip().splitlines()[0] if text.strip() else ""
                break
        gates.append(Gate(_test_id(case), outcome, float(case.get("time") or 0.0), message))
    return gates


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def text_table(gates: list[Gate]) -> str:
    rows = [("gate", "result", "seconds", "note")]
    rows += [(g.test, g.outcome.upper(), f"{g.seconds:.2f}", _clip(g.message, 80)) for g in gates]
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    line = "  ".join("-" * w for w in widths)
    out = []
    for i, r in enumerate(rows):
        out.append("  ".join((c.rjust(w) if j == 2 else c.ljust(w))
                             for j, (c, w) in enumerate(zip(r, widths, strict=True))).rstrip())
        if i == 0:
            out.append(line)
    return "\n".join(out)


def markdown_table(gates: list[Gate], status: int, wall: float) -> str:
    lines = [f"### Perf gates: {'PASS' if status == 0 else 'FAIL'} "
             f"({len(gates)} gates, {wall:.1f} s wall)", "",
             "| gate | result | seconds | note |", "|---|---|---:|---|"]
    for g in gates:
        note = _clip(g.message, 120).replace("|", "\\|")
        lines.append(f"| `{g.test}` | {g.outcome.upper()} "
                     f"| {g.seconds:.2f} | {note} |")
    return "\n".join(lines) + "\n"


def summary(gates: list[Gate]) -> str:
    counts: dict[str, int] = {}
    for g in gates:
        counts[g.outcome] = counts.get(g.outcome, 0) + 1
    parts = [f"{counts[k]} {k}" for k in ("passed", "failed", "error", "skipped") if counts.get(k)]
    return ", ".join(parts) or "no gates"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.partition("\n")[0])
    ap.add_argument("--list", action="store_true", help="collect the perf gates, do not run")
    ap.add_argument("--junit", type=Path, help="keep the JUnit XML report here")
    ap.add_argument("--markdown", type=Path,
                    help="append a Markdown summary here (e.g. $GITHUB_STEP_SUMMARY)")
    ap.add_argument("--allow-empty", action="store_true",
                    help="exit 0 when no perf test is collected")
    ap.add_argument("pytest_args", nargs="*", help="extra pytest arguments (after --)")
    args = ap.parse_args(argv)

    base = [sys.executable, "-m", "pytest", "-m", "perf", "-p", "no:cacheprovider"]
    if args.list:
        return subprocess.call([*base, "--collect-only", "-q", *args.pytest_args], cwd=REPO)

    with tempfile.TemporaryDirectory(prefix="tokenbill-perf-") as tmp:
        junit = args.junit or Path(tmp) / "perf.xml"
        cmd = [*base, "-q", "-rfE", f"--junitxml={junit}", "-o", "junit_family=xunit2",
               *args.pytest_args]
        print("$ " + " ".join(cmd), flush=True)
        start = time.monotonic()
        status = subprocess.call(cmd, cwd=REPO, env={**os.environ, "PYTHONHASHSEED": "0"})
        wall = time.monotonic() - start
        gates = parse_junit(junit) if junit.is_file() else []

    print()
    print(text_table(gates) if gates else "(no perf gates ran)")
    print(f"\nperf gates: {summary(gates)} in {wall:.1f} s (pytest exit {status})")
    if status == NO_TESTS_COLLECTED and args.allow_empty:
        status = 0
    if args.markdown is not None:
        with args.markdown.open("a", encoding="utf-8") as fh:
            fh.write(markdown_table(gates, status, wall))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
