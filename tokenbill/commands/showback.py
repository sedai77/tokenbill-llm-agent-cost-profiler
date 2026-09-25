"""``tokenbill showback`` — per-team pages from published aggregates (SPEC §14.5; CLI-LEDGER)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from tokenbill import cli


def add_parser(subparsers: Any) -> argparse.ArgumentParser:
    """Register ``showback``."""
    p = subparsers.add_parser("showback", help=cli.COMMAND_HELP["showback"])
    p.add_argument("--out", type=Path, required=True, metavar="DIR",
                   help="directory for the pages")
    p.add_argument("--format", choices=("html", "csv", "json"), action="append",
                   default=argparse.SUPPRESS,
                   help="page format (repeatable; default html)")
    p.add_argument("--since", metavar="DATE", help="window start (UTC date, inclusive)")
    p.add_argument("--until", metavar="DATE", help="window end (UTC date, exclusive)")
    return p


def run(args: argparse.Namespace) -> int:
    """Write the pages and list them."""
    from tokenbill.core.errors import UsageError
    from tokenbill.pipeline.ledger import resolve_window, run_showback

    formats = getattr(args, "format", None) or ["html"]
    if isinstance(formats, str):  # the global --format given before the verb
        formats = [formats]
    bad = sorted(set(formats) - {"html", "csv", "json"})
    if bad:
        raise UsageError("showback --format must be html, csv or json")
    config = cli.load_config_for(args)
    env = cli.env_for(args, config=config)
    since_ms, until_ms = resolve_window(args.since, args.until, now_ms=env.now_ms)
    notes: list[Any] = []
    written = run_showback(cli.require_db(args), env, args.out,
                           formats=tuple(dict.fromkeys(formats)), since_ms=since_ms,
                           until_ms=until_ms, notes=notes)
    for path in written:
        print(path)
    for item in notes:
        cli.note(args, f"[{item.severity}] {item.code}: {item.detail}")
    return cli.strict_dq_code(args, sum(1 for n in notes if n.severity != "info"), config)
