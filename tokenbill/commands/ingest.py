"""``tokenbill ingest SOURCE…`` — normalize sources into the ledger (SPEC §15, §5; CLI-LEDGER)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from tokenbill import cli
from tokenbill.core.errors import UsageError


def add_parser(subparsers: Any) -> argparse.ArgumentParser:
    """Register ``ingest``."""
    p = subparsers.add_parser("ingest", help=cli.COMMAND_HELP["ingest"])
    p.add_argument("sources", nargs="+", type=Path, metavar="SOURCE",
                   help="files or directories (Claude Code projects, trace@1/@2, OTLP/JSON, "
                        "admin pages, CUR, GCP billing export, Copilot exports, …)")
    p.add_argument("--adapter", default="auto", metavar="NAME|auto",
                   help="adapter name, or auto to sniff each file (default)")
    p.add_argument("--content", choices=("none", "fingerprint", "full"), default="none",
                   help="content tier to read (none; fingerprint for block hashes; full is "
                        "refused)")
    p.add_argument("--key-file", type=Path, metavar="PATH",
                   help="org key (pseudonymizes people; default from the config)")
    p.add_argument("--collection-key-file", type=Path, metavar="PATH",
                   help="fleet collection (name) key (default from the config)")
    p.add_argument("--attr", action="append", default=[], metavar="K=V",
                   help="attribution default for sources without one (repeatable)")
    p.add_argument("--rules", type=Path, metavar="RULES.json",
                   help="allocation rules (tokenbill/allocation@1), applied at ingest")
    p.add_argument("--team-map", type=Path, metavar="FILE",
                   help="JSON object raw actor reference → team (applied, then discarded)")
    p.add_argument("--k", type=cli._positive_int, metavar="K", help="k-anonymity threshold")
    p.add_argument("--since", metavar="DATE", help="ignore records before this date")
    p.add_argument("--until", metavar="DATE", help="ignore records from this date on")
    p.add_argument("--strict", action="store_true",
                   help="fail on the first malformed record instead of quarantining it")
    p.add_argument("--renormalize", action="store_true",
                   help="trace@2: rebuild inferences from raw usage with current conventions")
    p.add_argument("--experimental", action="append", default=[], metavar="FEATURE",
                   help="opt-in experimental source (repeatable)")
    p.add_argument("--rates", type=Path, action="append", default=[], metavar="FILE",
                   help="extra rate file (repeatable)")
    p.add_argument("--contract", type=Path, metavar="FILE", help="contract overlay")
    return p


def run(args: argparse.Namespace) -> int:
    """Ingest and print the sources read and the data-quality notes."""
    from tokenbill.core.types import EXPERIMENTAL_FLAGS
    from tokenbill.pipeline.common import load_team_map
    from tokenbill.pipeline.ledger import (
        attribution_from,
        date_start_ms,
        parse_attr_pairs,
        parse_date,
        run_ingest,
    )

    unknown = sorted(set(args.experimental) - EXPERIMENTAL_FLAGS)
    if unknown:
        raise UsageError(f"unknown --experimental feature(s): {', '.join(unknown)} "
                         f"(known: {', '.join(sorted(EXPERIMENTAL_FLAGS))})")
    cli.output_format(args, ("text", "json"), "text")
    config = cli.load_config_for(args, k=args.k)
    env = cli.env_for(args, config=config, rates=args.rates, contract=args.contract,
                      key_file=args.key_file, collection_key_file=args.collection_key_file)
    rules = None
    if args.rules is not None:
        from tokenbill.finops.allocation import load_rules

        rules = load_rules(args.rules)
    since_ms = date_start_ms(parse_date(args.since, "--since")) if args.since else None
    until_ms = date_start_ms(parse_date(args.until, "--until")) if args.until else None
    if since_ms is not None and until_ms is not None and until_ms <= since_ms:
        raise UsageError("--until must be after --since (the end date is exclusive)")
    result = run_ingest(cli.require_db(args), args.sources, env, adapter=args.adapter,
                        content=args.content, rules=rules,
                        team_map=load_team_map(args.team_map) if args.team_map else (),
                        attribution=attribution_from(parse_attr_pairs(args.attr)),
                        since_ms=since_ms, until_ms=until_ms, strict=args.strict,
                        renormalize=args.renormalize,
                        experimental=frozenset(args.experimental))
    return cli.emit_result(args, result, config=config)
