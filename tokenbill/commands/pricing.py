"""``tokenbill pricing show | verify | diff | emit-model-pricing`` (SPEC §6.5, §6.7; CLI-LEDGER).

``verify`` is offline by default (the packaged pricing-page snapshot plus the channel extensions'
verifiers) and exits 3 on any authoritative discrepancy; ``--live`` fetches the pricing page
(network); ``--feed`` cross-checks LiteLLM / OpenRouter files (warnings only).
``emit-model-pricing`` prints the Claude Code managed ``modelPricing`` block of a contract.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from tokenbill import cli


def add_parser(subparsers: Any) -> argparse.ArgumentParser:
    """Register ``pricing`` and its actions."""
    p = subparsers.add_parser("pricing", help=cli.COMMAND_HELP["pricing"])
    sub = p.add_subparsers(dest="action", required=True, metavar="action")
    show = sub.add_parser("show", help="the registry rows (optionally one model at a date)",
                          description="Show the built-in registry and --rates layers.")
    show.add_argument("model", nargs="?", metavar="MODEL", help="model id or alias")
    show.add_argument("--at", metavar="DATE", help="only rows effective on this date")
    show.add_argument("--rates", type=Path, action="append", default=[], metavar="FILE",
                      help="extra rate file (repeatable)")
    verify = sub.add_parser("verify", help="verify the registry (offline snapshot by default)",
                            description="Verify the registry against the pricing page snapshot.")
    verify.add_argument("--snapshot", type=Path, metavar="FILE",
                        help="pricing-page snapshot (default: the packaged one)")
    verify.add_argument("--live", action="store_true",
                        help="fetch the live pricing page instead (network)")
    verify.add_argument("--feed", type=Path, nargs="+", default=[], metavar="FILE",
                        help="LiteLLM / OpenRouter JSON to cross-check (warnings only)")
    diff = sub.add_parser("diff", help="differences between two rate files",
                          description="Rows added, removed or changed between A and B.")
    diff.add_argument("a", metavar="A", help="rate file (or 'builtin')")
    diff.add_argument("b", metavar="B", help="rate file (or 'builtin')")
    emit = sub.add_parser("emit-model-pricing",
                          help="Claude Code managed modelPricing block of a contract",
                          description="Print the managed-settings modelPricing block.")
    emit.add_argument("--contract", type=Path, required=True, metavar="FILE",
                      help="contract overlay (tokenbill/contract@1 or modelPricing JSON)")
    for child in (show, verify, diff, emit):
        cli.add_command_flags(child)
    return p


def run(args: argparse.Namespace) -> int:
    """Run the action; ``verify`` gates (exit 3) on authoritative discrepancies."""
    import time

    from tokenbill.pipeline.ledger import day_of, parse_date, run_emit_model_pricing, run_pricing

    if args.action == "emit-model-pricing":
        text = run_emit_model_pricing(args.contract)
        print(text if text.endswith("\n") else text + "\n", end="")
        return cli.EXIT_OK
    fmt = cli.output_format(args, ("text", "json"), "text")
    config = cli.load_config_for(args)
    today = day_of(time.time_ns() // 1_000_000)
    notes: list[Any] = []
    if args.action == "show":
        at = parse_date(args.at, "--at") if args.at else None
        result = run_pricing("show", today=today, k=config.k, model=args.model, at=at,
                             rates=args.rates)
    elif args.action == "verify":
        result = run_pricing("verify", today=today, k=config.k, snapshot=args.snapshot,
                             live=args.live, feeds=args.feed, notes=notes)
    else:
        result = run_pricing("diff", today=today, k=config.k, a=args.a, b=args.b)
    if notes:
        import dataclasses

        result = dataclasses.replace(result, data_quality=(*result.data_quality, *notes))
    code = cli.emit_result(args, result, config=config, default=fmt)
    report = result.pricing
    assert report is not None
    if args.action == "verify" and not report.ok:
        return cli.EXIT_GATE
    return code
