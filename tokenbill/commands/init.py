"""``tokenbill init`` — config, keys, store directory, privacy notice (SPEC §15, §8; CLI-LEDGER)."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from tokenbill import cli


def add_parser(subparsers: Any) -> argparse.ArgumentParser:
    """Register ``init``."""
    p = subparsers.add_parser("init", help=cli.COMMAND_HELP["init"])
    p.add_argument("--dir", type=Path, default=Path(".tokenbill"), metavar="DIR",
                   help="directory to create (default ./.tokenbill; created 0700)")
    p.add_argument("--identity-mode", choices=("central", "two-stage"), default="central",
                   help="how collectors ship identities (SPEC §5.4; default central)")
    p.add_argument("--k", type=cli._positive_int, default=5, metavar="K",
                   help="k-anonymity threshold for every published aggregate (default 5)")
    p.add_argument("--collector", action="store_true",
                   help="create only a collector config (no keys, no store)")
    p.add_argument("--force", action="store_true",
                   help="overwrite an existing config.json (keys are never replaced)")
    return p


def run(args: argparse.Namespace) -> int:
    """Create the directory, print what was created (paths only) and the privacy notice."""
    from tokenbill.pipeline.ledger import PRIVACY_NOTICE, run_init

    fmt = cli.output_format(args, ("text", "json"), "text")
    result = run_init(args.dir, identity_mode=args.identity_mode, k=args.k,
                      collector=args.collector, force=args.force)
    if fmt == "json":
        import json

        doc = {"schema": "tokenbill/init@1", "directory": str(result.directory),
               "config": str(result.config_path),
               "keys": [str(p) for p in result.key_paths],
               "store_dir": str(result.store_dir) if result.store_dir else None,
               "identity_mode": result.identity_mode, "collector": result.collector,
               "data_quality": [{"code": n.code, "severity": n.severity, "count": n.count,
                                 "evidence": "exact"} for n in result.notes]}
        print(json.dumps(doc, sort_keys=True, indent=2))
    else:
        kind = "collector config" if result.collector else "central host"
        print(f"initialized {kind} in {result.directory} (identity mode {result.identity_mode})")
        print(f"  config  {result.config_path}")
        for path in result.key_paths:
            print(f"  key     {path} (0600; back it up, never share it)")
        if result.store_dir is not None:
            print(f"  store   {result.store_dir} (0700)")
        for n in result.notes:
            print(f"  [{n.severity}] {n.code}: {n.detail}")
        print()
        print(PRIVACY_NOTICE.format(k=args.k))
        if result.store_dir is not None and not args.quiet:
            db = result.store_dir / "tokenbill.db"
            print()
            print(f"next: tokenbill --config {result.config_path} ingest --db {db} SOURCE…")
    return cli.strict_dq_code(args, len(result.notes), None)
