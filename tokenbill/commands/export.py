"""``tokenbill export`` — FOCUS 1.4 CSV, trace@2, ccusage JSON (SPEC §14.4, §14.5, §4, D19).

``--format focus`` refuses (exit 3) a window whose channels are not all reconciled — the
reconciliation of the window is re-run from the ledger's stored provider data — unless
``--channel`` restricts the export to reconciled channels or ``--allow-unreconciled`` fills
``BilledCost`` from list / contract cost with ``x_Reconciled=false``. ``--role enrichment`` zeroes
Billed/Effective cost for organizations that also load the provider's own FOCUS feed;
``--chargeback`` refuses below 95% allocation coverage. Channel extensions own their channels'
rows (Copilot rows come from the extension, never from the ledger). ``--content full`` is refused.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from tokenbill import cli
from tokenbill.core.errors import UsageError


def add_parser(subparsers: Any) -> argparse.ArgumentParser:
    """Register ``export``."""
    p = subparsers.add_parser("export", help=cli.COMMAND_HELP["export"])
    p.add_argument("--format", choices=("focus", "trace2", "ccusage"), default=argparse.SUPPRESS,
                   help="focus (FOCUS 1.4 CSV), trace2 (tokenbill/trace@2) or ccusage (JSON)")
    p.add_argument("-o", "--output", type=Path, metavar="FILE",
                   help="output file (default stdout; trace2 files ending .gz are gzipped)")
    p.add_argument("--grain", choices=("day", "hour"), default="day",
                   help="charge period grain (day; hour is not available in v0.2)")
    p.add_argument("--content", choices=("none", "fingerprint", "full"), default="none",
                   help="trace2 content tier (none or fingerprint; full is refused)")
    gate = p.add_mutually_exclusive_group()
    gate.add_argument("--require-reconciled", dest="allow_unreconciled", action="store_false",
                      help="refuse unreconciled channels (default)")
    gate.add_argument("--allow-unreconciled", dest="allow_unreconciled", action="store_true",
                      help="export unreconciled channels with x_Reconciled=false")
    p.set_defaults(allow_unreconciled=False)
    p.add_argument("--channel", nargs="+", default=[], metavar="C",
                   help="restrict the export to these channels")
    p.add_argument("--role", choices=("primary", "enrichment"), default="primary",
                   help="enrichment zeroes Billed/Effective cost (double-count guard)")
    p.add_argument("--chargeback", action="store_true",
                   help="refuse below 95%% allocation coverage")
    p.add_argument("--rules", type=Path, metavar="RULES.json",
                   help="allocation rules whose splits apportion FOCUS rows")
    p.add_argument("--report", choices=("daily", "monthly"), default="daily",
                   help="ccusage report (default daily)")
    p.add_argument("--since", metavar="DATE", help="window start (UTC date, inclusive)")
    p.add_argument("--until", metavar="DATE", help="window end (UTC date, exclusive)")
    return p


def run(args: argparse.Namespace) -> int:
    """Export; nothing is written when a gate refuses."""
    from tokenbill.pipeline.ledger import EXPORT_FORMATS, resolve_window, run_export

    fmt = getattr(args, "format", None)
    if fmt is None:
        raise UsageError("export needs --format focus|trace2|ccusage")
    if fmt not in EXPORT_FORMATS:
        raise UsageError(f"export --format must be one of {', '.join(EXPORT_FORMATS)}")
    if args.output is not None and not args.output.parent.exists():
        raise UsageError(f"output directory '{args.output.parent}' does not exist")
    config = cli.load_config_for(args)
    env = cli.env_for(args, config=config)
    since_ms, until_ms = resolve_window(args.since, args.until, now_ms=env.now_ms)
    rules = None
    if args.rules is not None:
        from tokenbill.finops.allocation import load_rules

        rules = load_rules(args.rules)
    notes: list[Any] = []
    n = run_export(cli.require_db(args), env, fmt=fmt, since_ms=since_ms, until_ms=until_ms,
                   out=None if args.output is not None else sys.stdout, out_path=args.output,
                   grain=args.grain, content=args.content,
                   allow_unreconciled=args.allow_unreconciled, channels=args.channel,
                   role=args.role, chargeback=args.chargeback, rules=rules, report=args.report,
                   notes=notes)
    if args.output is not None:
        cli.note(args, f"export {fmt}: {n:,} rows written to {args.output}")
    for item in notes:
        cli.note(args, f"[{item.severity}] {item.code}: {item.detail}")
    warnings = sum(1 for item in notes if item.severity in ("warn", "error"))
    return cli.strict_dq_code(args, warnings, config)
