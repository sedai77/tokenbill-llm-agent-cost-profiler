"""``tokenbill purge`` — erasure and retention (SPEC §7.5, §8; CLI-LEDGER).

Deletes one principal's rows (a ``p_`` pseudonym — e.g. printed by ``tokenbill copilot pseudonym``;
rows under an adopted key id included) and/or everything before a date, in the ledger and in every
extension record store. Irreversible: ``--yes`` is required. Each store writes an audit row without
the identity.
"""

from __future__ import annotations

import argparse
from typing import Any

from tokenbill import cli
from tokenbill.core.errors import UsageError


def add_parser(subparsers: Any) -> argparse.ArgumentParser:
    """Register ``purge``."""
    p = subparsers.add_parser("purge", help=cli.COMMAND_HELP["purge"])
    p.add_argument("--principal", metavar="P", help="p_ pseudonym to erase")
    p.add_argument("--before", metavar="DATE", help="erase everything before this UTC date")
    p.add_argument("--yes", action="store_true", help="confirm the irreversible deletion")
    return p


def run(args: argparse.Namespace) -> int:
    """Purge and print the counts (never the identity)."""
    from tokenbill.pipeline.ledger import date_start_ms, parse_date, run_purge

    if args.principal is None and args.before is None:
        raise UsageError("purge needs --principal P or --before DATE")
    before_ms = date_start_ms(parse_date(args.before, "--before")) if args.before else None
    db = cli.require_db(args)
    if not args.yes:
        raise UsageError("purge is irreversible: re-run with --yes to confirm")
    fmt = cli.output_format(args, ("text", "json"), "text")
    config = cli.load_config_for(args)
    env = cli.env_for(args, config=config)
    counts = run_purge(db, env, principal=args.principal, before_ms=before_ms)
    if fmt == "json":
        import json

        print(json.dumps({"schema": "tokenbill/purge@1", "requests": counts["requests"],
                          "records": counts["records"], "evidence": "exact"}, sort_keys=True))
    else:
        what = "principal" if args.principal else f"data before {args.before}"
        if args.principal and args.before:
            what = f"principal and data before {args.before}"
        print(f"purged {what}: {counts['requests']:,} requests, {counts['records']:,} records "
              "(audited)")
    return cli.EXIT_OK
