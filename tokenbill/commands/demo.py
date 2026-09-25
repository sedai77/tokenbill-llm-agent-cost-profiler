"""``tokenbill demo`` (SPEC §15, §16.2, §18; CLI-LEDGER).

Without ``--fleet`` this is the unchanged v0.1 demo (``cli._cmd_demo``): its stdout and HTML are
byte-identical to 0.1.2 (``tests/v2/golden/``). ``demo --fleet`` runs the synthetic fleet end to
end through ``pipeline.savings.run_demo_fleet`` (CLI-SAVINGS), imported only on that path.
"""

from __future__ import annotations

import argparse
import inspect
import sys
from pathlib import Path
from typing import Any

from tokenbill import cli
from tokenbill.core.errors import UsageError

SAVINGS_MODULE = "tokenbill.pipeline.savings"


def add_parser(subparsers: Any) -> argparse.ArgumentParser:
    """Register ``demo``."""
    p = subparsers.add_parser("demo", help=cli.COMMAND_HELP["demo"])
    p.add_argument("-o", "--output", type=Path, metavar="report.html",
                   help="also write the self-contained HTML report here")
    p.add_argument("--seed", type=int, default=7,
                   help="seed for the deterministic scenarios (default: 7)")
    p.add_argument("--scenario", metavar="NAME",
                   help="profile a single scenario by name (default: all four)")
    p.add_argument("--fleet", action="store_true",
                   help="run the synthetic fleet end to end (keyless, networkless; SPEC §18)")
    p.add_argument("--out-dir", type=Path, metavar="DIR",
                   help="with --fleet: keep the generated source files here")
    return p


def run(args: argparse.Namespace) -> int:
    """Dispatch to the v0.1 demo or to the fleet demo."""
    if args.fleet:
        if args.scenario is not None:
            raise UsageError("--scenario applies to the trace demo, not --fleet")
        return _run_fleet(args)
    if args.out_dir is not None:
        raise UsageError("--out-dir needs --fleet")
    cli.output_format(args, ("text",), "text")
    return cli._cmd_demo(args)


def _run_fleet(args: argparse.Namespace) -> int:
    import importlib

    try:
        savings = importlib.import_module(SAVINGS_MODULE)
    except ImportError:
        print("tokenbill: error: demo --fleet needs the savings pipeline "
              f"({SAVINGS_MODULE}), which is not installed", file=sys.stderr)
        return cli.EXIT_FAILURE
    fmt = cli.output_format(args, ("text", "json"), "text")
    if args.output is not None and not args.output.parent.exists():
        raise UsageError(f"output directory '{args.output.parent}' does not exist")
    fn = savings.run_demo_fleet
    offered: dict[str, object] = {"seed": args.seed, "out_dir": args.out_dir,
                                  "deterministic": bool(args.deterministic), "jobs": args.jobs}
    try:
        accepted = inspect.signature(fn).parameters
        kwargs = {k: v for k, v in offered.items() if k in accepted and v is not None}
    except (TypeError, ValueError):  # pragma: no cover - builtins without a signature
        kwargs = {"seed": args.seed}
    result = fn(**kwargs)
    if args.output is not None:
        from tokenbill.outputs.html import render_html

        args.output.write_text(render_html(result), encoding="utf-8", newline="\n")
    code = cli.emit_result(args, result, config=None, default=fmt)
    if args.output is not None and fmt == "text":
        print(f"Report written to {args.output}")
    return code
