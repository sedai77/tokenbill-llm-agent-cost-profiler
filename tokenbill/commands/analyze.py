"""``tokenbill analyze TRACE…`` with engine routing (SPEC §15.1, §16, D2, D38; CLI-LEDGER).

``--engine auto`` (default), in this order: (1) ``--model-price`` into the v0.1 ``PRICING`` table
exactly as 0.1.2; (2) the frozen strict ``trace.read_trace()`` on every file (errors such as an
unpaired surrogate propagate as in 0.1.2: one line, exit 1); (3) run ids that appear in more than
one file are renamed ``<file name>:<run_id>``; (4) the runs are inspected for a **v1-unsafe**
condition — ``cache_control`` markers that move between consecutive calls (v1 renders the markers
and reports a false history rewrite), any marker with ``ttl: "1h"`` (v1 prices 1h writes at
1.25×), or more than one model change within a run (interleaved lanes). Without one the frozen v1
pipeline renders byte-identically; with one, :func:`tokenbill.pipeline.ledger.run_analyze_v2`
runs on the same files and exactly one stderr note names the reason. An unknown model is not a
trigger (v1 prints "unavailable"). ``--engine v1`` forces the legacy engine (still namespacing run
ids); ``--engine v2`` forces the new one (``--model-price`` becomes a rate layer). ``--format json``
has no v1 renderer, so it selects the v2 engine.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tokenbill import cli
from tokenbill.core.errors import UsageError

if TYPE_CHECKING:
    from tokenbill.trace import Call, Run

ENGINES = ("auto", "v1", "v2")
REASON_MOVING = "cache_control markers move between consecutive calls"
REASON_1H = 'a cache_control marker has ttl "1h"'
REASON_MODELS = "a run changes model more than once (interleaved models)"
_MAX_DEPTH = 64


def add_parser(subparsers: Any) -> argparse.ArgumentParser:
    """Register ``analyze``."""
    p = subparsers.add_parser("analyze", help=cli.COMMAND_HELP["analyze"])
    p.add_argument("traces", nargs="+", type=Path, metavar="TRACE.jsonl",
                   help="trace files to profile")
    p.add_argument("-o", "--output", type=Path, metavar="report.html",
                   help="also write the self-contained HTML report here")
    p.add_argument("--model-price", action="append", type=cli._model_price, default=[],
                   metavar="MODEL=IN,OUT",
                   help=("price an unknown or self-hosted model, dollars per MTok "
                         "(repeatable; cache multipliers use the provider defaults)"))
    p.add_argument("--engine", choices=ENGINES, default="auto",
                   help="auto (default: v1 unless the traces are v1-unsafe), v1 or v2")
    p.add_argument("--rates", type=Path, action="append", default=[], metavar="FILE",
                   help="extra rate file (v2 engine; repeatable)")
    p.add_argument("--contract", type=Path, metavar="FILE",
                   help="contract overlay (v2 engine)")
    return p


def _has_marker(value: object, depth: int = 0) -> bool:
    if depth > _MAX_DEPTH:
        return False
    if isinstance(value, Mapping):
        return "cache_control" in value or any(
            _has_marker(v, depth + 1) for v in value.values() if isinstance(v, (Mapping, list,
                                                                                tuple)))
    if isinstance(value, (list, tuple)):
        return any(_has_marker(v, depth + 1) for v in value)
    return False


def _has_1h(value: object, depth: int = 0) -> bool:
    if depth > _MAX_DEPTH:
        return False
    if isinstance(value, Mapping):
        cc = value.get("cache_control")
        if isinstance(cc, Mapping) and cc.get("ttl") == "1h":
            return True
        return any(_has_1h(v, depth + 1) for v in value.values()
                   if isinstance(v, (Mapping, list, tuple)))
    if isinstance(value, (list, tuple)):
        return any(_has_1h(v, depth + 1) for v in value)
    return False


def _strip(value: Any, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        return value
    if isinstance(value, Mapping):
        return {k: _strip(v, depth + 1) for k, v in value.items() if k != "cache_control"}
    if isinstance(value, list):
        return [_strip(v, depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(_strip(v, depth + 1) for v in value)
    return value


def _stripped(call: Call) -> Call:
    import dataclasses

    return dataclasses.replace(call, tools=_strip(call.tools), messages=_strip(call.messages))


def _markers_move(prev: Call, cur: Call) -> bool:
    """True when the markers alone change where v1 sees the two calls diverge (the rendered
    prefix differs only because a marker moved)."""
    from tokenbill.trace import diverging_segment

    if not (_has_marker(prev.tools) or _has_marker(prev.messages) or _has_marker(cur.tools)
            or _has_marker(cur.messages)):
        return False
    return diverging_segment(prev, cur) != diverging_segment(_stripped(prev), _stripped(cur))


def unsafe_reason(runs: Sequence[Run]) -> str | None:
    """The first v1-unsafe condition found in *runs* (SPEC §15.1), or None."""
    one_hour = interleaved = False
    for run in runs:
        changes = 0
        for i, call in enumerate(run.calls):
            if not one_hour and (_has_1h(call.tools) or _has_1h(call.messages)):
                one_hour = True
            if i:
                prev = run.calls[i - 1]
                if prev.model != call.model:
                    changes += 1
                if _markers_move(prev, call):
                    return REASON_MOVING
        if changes > 1:
            interleaved = True
    if one_hour:
        return REASON_1H
    if interleaved:
        return REASON_MODELS
    return None


def _price_text(value: float) -> str:
    return format(Decimal(repr(float(value))), "f")


def _run_v2(args: argparse.Namespace, fmt: str) -> int:
    from tokenbill.pipeline.ledger import run_analyze_v2

    if args.output is not None and not args.output.parent.exists():
        print(f"tokenbill: error: output directory '{args.output.parent}' does not exist",
              file=sys.stderr)
        return cli.EXIT_FAILURE
    prices = [(m, _price_text(i), _price_text(o)) for m, i, o in args.model_price]
    config = cli.load_config_for(args)
    env = cli.env_for(args, config=config, rates=args.rates, contract=args.contract,
                      model_prices=prices)
    result = run_analyze_v2(args.traces, env)
    if args.output is not None:
        from tokenbill.outputs.html import render_html

        args.output.write_text(render_html(result), encoding="utf-8", newline="\n")
    code = cli.emit_result(args, result, config=config, default=fmt)
    if args.output is not None and fmt == "text":
        print(f"Report written to {args.output}")
    return code


def run(args: argparse.Namespace) -> int:
    """Route per SPEC §15.1 and run the chosen engine."""
    fmt = cli.output_format(args, ("text", "json"), "text")
    if args.engine == "v1":
        if fmt == "json":
            raise UsageError("--format json needs the v2 engine (drop --engine v1)")
        return cli._cmd_analyze(args)
    if args.engine == "v2":
        return _run_v2(args, fmt)
    from tokenbill.trace import read_trace

    cli._apply_model_prices(args.model_price)
    per_file = [(path, read_trace(path)) for path in args.traces]
    runs = cli.namespace_runs(per_file)
    if not runs:
        print("tokenbill analyze: error: no runs found in the given traces", file=sys.stderr)
        return cli.EXIT_FAILURE
    reason = "--format json has no v1 renderer" if fmt == "json" else unsafe_reason(runs)
    if reason is None:
        trace_name = ", ".join(path.name for path in args.traces)
        return cli._profile_and_render(runs, trace_name=trace_name, synthetic=False,
                                       output=args.output)
    print(f"note: using the v2 engine because {reason} (--engine v1 forces the legacy engine)",
          file=sys.stderr)
    return _run_v2(args, fmt)
