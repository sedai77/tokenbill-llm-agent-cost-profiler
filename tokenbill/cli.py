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


#: Ceiling for --model-price rates. $1e9 per MTok is already absurd; anything
#: near float max would overflow tokens*rate to inf and poison every total.
_MAX_PRICE_PER_MTOK = 1e9


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
    if input_price > _MAX_PRICE_PER_MTOK or output_price > _MAX_PRICE_PER_MTOK:
        # A finite-but-huge rate passes isfinite yet overflows tokens*rate to
        # inf during pricing, which the renderers cannot chart honestly.
        raise argparse.ArgumentTypeError(
            f"price out of range in {spec!r}: rates above {_MAX_PRICE_PER_MTOK:g} "
            "dollars per MTok are not supported"
        )
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
    if output is not None and not output.parent.exists():
        # Fail before the (potentially long) analysis, not after the full
        # summary has scrolled by with the report silently unwritten.
        print(
            f"tokenbill: error: output directory '{output.parent}' does not exist",
            file=sys.stderr,
        )
        return 1
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
        # errors="backslashreplace": a trace is untrusted input, and one exotic
        # character must not discard a finished report at the final write.
        output.write_text(
            render_report(profiles, scenarios, breakers, meta),
            encoding="utf-8",
            errors="backslashreplace",
        )
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
    code = _profile_and_render(
        runs, trace_name=trace_name, synthetic=True, output=args.output
    )
    if code == 0:
        if args.output is None:
            print(
                "next: tokenbill demo -o report.html writes the full HTML report; "
                'then record a real agent — see "Your first real trace" in the README'
            )
        else:
            print(
                "next: record a real agent and analyze its trace — "
                'see "Your first real trace" in the README'
            )
    return code


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


def _tolerant_output_streams() -> None:
    """Make stdout/stderr total functions of their input.

    On a non-UTF-8 console (legacy Windows cp1252, PYTHONIOENCODING set) a
    CJK/emoji run_id — perfectly valid trace input — would otherwise crash
    the summary print with UnicodeEncodeError after all analysis succeeded.
    Unencodable characters degrade to backslash escapes instead.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (OSError, ValueError):  # exotic stream; keep going
                pass


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    _tolerant_output_streams()
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
