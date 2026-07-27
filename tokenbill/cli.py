"""Token Bill command line: ``demo``, ``analyze``, and ``--version``.

Heavy modules (trace parsing, the analyzer, the simulator, the report) are
imported lazily inside the command handlers so ``tokenbill --version`` and
usage errors stay instant.

Exit codes: 0 success; 1 runtime failure (bad trace file, no runs); 2 usage
errors (argparse and friends).
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from tokenbill import __version__
from tokenbill.common import TokenbillError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tokenbill.trace import Run

logger = logging.getLogger("tokenbill.cli")


def _model_price(spec: str) -> tuple[str, float, float]:
    """Parse a ``--model-price MODEL=IN,OUT`` override (dollars per MTok)."""
    problem = f"expected MODEL=IN,OUT (dollars per MTok, e.g. mymodel=3,15), got {spec!r}"
    model, sep, prices = spec.partition("=")
    model = model.strip()
    if not sep or not model:
        raise argparse.ArgumentTypeError(problem)
    parts = prices.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(problem)
    try:
        input_price, output_price = (float(part) for part in parts)
    except ValueError:
        raise argparse.ArgumentTypeError(problem) from None
    if not (math.isfinite(input_price) and math.isfinite(output_price)):
        raise argparse.ArgumentTypeError(problem)  # nan/inf would render "$nan" reports
    if input_price < 0 or output_price < 0:
        raise argparse.ArgumentTypeError(problem)
    return model, input_price, output_price


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tokenbill",
        description=(
            "Profile LLM agent traces: token waterfalls from real billed usage, "
            "re-sent-prefix redundancy, prompt-cache simulation, and cache-breaker "
            "detection with dollar-valued fixes."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="command")

    demo = subparsers.add_parser(
        "demo",
        help="profile the bundled synthetic demo scenarios (deterministic, no API keys)",
    )
    demo.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="report.html",
        help="also write the self-contained HTML report here",
    )
    demo.add_argument(
        "--seed", type=int, default=7, help="seed for the deterministic scenarios (default: 7)"
    )
    demo.add_argument(
        "--scenario",
        metavar="NAME",
        help="profile a single scenario by name (default: all four)",
    )
    demo.set_defaults(handler=_cmd_demo)

    analyze = subparsers.add_parser(
        "analyze", help="profile recorded trace JSONL files (see tokenbill.instrument)"
    )
    analyze.add_argument(
        "traces", nargs="+", type=Path, metavar="TRACE.jsonl", help="trace files to profile"
    )
    analyze.add_argument(
        "-o",
        "--output",
        type=Path,
        metavar="report.html",
        help="also write the self-contained HTML report here",
    )
    analyze.add_argument(
        "--model-price",
        action="append",
        type=_model_price,
        default=[],
        metavar="MODEL=IN,OUT",
        help=(
            "price an unknown or self-hosted model, dollars per MTok "
            "(repeatable; cache multipliers use the provider defaults)"
        ),
    )
    analyze.set_defaults(handler=_cmd_analyze)
    return parser


def _profile_and_render(
    runs: Sequence[Run], *, trace_name: str, synthetic: bool, output: Path | None
) -> int:
    """The shared demo/analyze pipeline: profile, detect, simulate, render."""
    from tokenbill.analyzer import profile_run
    from tokenbill.breakers import detect, repaired_calls
    from tokenbill.report import render_report, render_text_summary
    from tokenbill.simulator import simulate

    profiles = []
    scenarios = {}
    breakers = {}
    for run in runs:
        profiles.append(profile_run(run))
        found = list(detect(run))
        breakers[run.run_id] = found
        fixed = list(repaired_calls(run, found)) if found else None
        scenarios[run.run_id] = list(simulate(run, fixed_calls=fixed))

    meta = {"trace": trace_name, "date": date.today().isoformat(), "synthetic": synthetic}
    print(render_text_summary(profiles, scenarios, breakers, meta=meta))
    if output is not None:
        output.write_text(render_report(profiles, scenarios, breakers, meta), encoding="utf-8")
        print(f"Report written to {output}")
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    from tokenbill.demo_traces import all_scenarios
    from tokenbill.trace import Run

    scenarios = all_scenarios(args.seed)
    if args.scenario is not None:
        if args.scenario not in scenarios:
            names = ", ".join(sorted(scenarios))
            print(
                f"tokenbill demo: error: unknown scenario {args.scenario!r} "
                f"(choose from: {names})",
                file=sys.stderr,
            )
            return 2
        scenarios = {args.scenario: scenarios[args.scenario]}
    runs = [Run(run_id=calls[0].run_id, calls=tuple(calls)) for calls in scenarios.values()]
    if args.scenario is None:
        trace_name = f"bundled demo scenarios (seed {args.seed})"
    else:
        trace_name = f"bundled demo scenario {args.scenario!r} (seed {args.seed})"
    return _profile_and_render(
        runs, trace_name=trace_name, synthetic=True, output=args.output
    )


def _cmd_analyze(args: argparse.Namespace) -> int:
    from tokenbill.trace import read_trace

    if args.model_price:
        from tokenbill.pricing import PRICING, ModelPricing

        for model, input_price, output_price in args.model_price:
            PRICING[model] = ModelPricing(
                input_per_mtok=input_price, output_per_mtok=output_price
            )
            logger.info(
                "priced %s at $%.2f in / $%.2f out per MTok", model, input_price, output_price
            )

    runs: list[Run] = []
    for path in args.traces:
        runs.extend(read_trace(path))
    if not runs:
        print("tokenbill analyze: error: no runs found in the given traces", file=sys.stderr)
        return 1
    trace_name = ", ".join(path.name for path in args.traces)
    return _profile_and_render(
        runs, trace_name=trace_name, synthetic=False, output=args.output
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse exits itself for --version/--help/usage errors
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 2
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        return args.handler(args)
    except (TokenbillError, OSError) as exc:
        print(f"tokenbill: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover - exercised via python -m tokenbill
    raise SystemExit(main())
