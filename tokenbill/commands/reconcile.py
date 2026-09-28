"""``tokenbill reconcile`` — the ledger gate, per channel (SPEC §12, §15, D19, D31; CLI-LEDGER).

Reads the provider's usage and invoice files with the ADMIN / cloud-billing adapters (or pulls the
usage and cost reports with ``--live --admin-key-env VAR``), stores them in the ledger, and
reconciles the window: RECON for the core channels plus every channel extension's reconciler,
merged into one report whose per-channel verdicts, residuals and decisions are printed. Exit 3
unless every channel with spend is ``reconciled`` (``--report-only`` always exits 0); with
``--suggest-contract OUT.json`` the suggested overlay is written and the re-run verdict decides.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from tokenbill import cli
from tokenbill.core.errors import UsageError

_FILE_FLAGS = (
    ("--usage-report", "usage_report", "Anthropic Admin usage report pages"),
    ("--cost-report", "cost_report", "Anthropic Admin cost report pages"),
    ("--cc-analytics", "cc_analytics", "Claude Code Analytics records"),
    ("--enterprise-analytics", "enterprise_analytics", "Enterprise Analytics usage/cost pages"),
    ("--openai-usage", "openai_usage", "OpenAI usage buckets"),
    ("--openai-costs", "openai_costs", "OpenAI costs"),
    ("--aws-cur", "aws_cur", "AWS CUR 2.0 CSV / CSV.gz"),
    ("--gcp-billing", "gcp_billing", "GCP billing export CSV / JSONL"),
)


def add_parser(subparsers: Any) -> argparse.ArgumentParser:
    """Register ``reconcile``."""
    p = subparsers.add_parser("reconcile", help=cli.COMMAND_HELP["reconcile"])
    for flag, dest, what in _FILE_FLAGS:
        p.add_argument(flag, dest=dest, type=Path, nargs="+", default=[], metavar="FILE",
                       help=what)
    p.add_argument("--live", action="store_true",
                   help="pull the usage and cost reports from the Admin API (network)")
    p.add_argument("--admin-key-env", metavar="VAR",
                   help="environment variable holding the Admin API key (required with --live; "
                        "the key is never logged or written)")
    p.add_argument("--record", type=Path, metavar="DIR",
                   help="with --live: keep the recorded pages here")
    p.add_argument("--since", metavar="DATE", help="window start (UTC date, inclusive)")
    p.add_argument("--until", metavar="DATE", help="window end (UTC date, exclusive)")
    p.add_argument("--tolerance-pct", default="0.5", metavar="PCT",
                   help="rate-card tolerance per model-day (default 0.5)")
    p.add_argument("--unexplained-pct", default="1.0", metavar="PCT",
                   help="unexplained residual tolerance per workspace-month (default 1.0)")
    p.add_argument("--closed-only", action="store_true",
                   help="exclude provisional days (within the 30-day revision window)")
    p.add_argument("--suggest-contract", type=Path, metavar="OUT.json",
                   help="write the suggested contract overlay and re-run with it")
    p.add_argument("--report-only", action="store_true",
                   help="always exit 0 (report without gating)")
    p.add_argument("--rates", type=Path, action="append", default=[], metavar="FILE",
                   help="extra rate file (repeatable)")
    p.add_argument("--contract", type=Path, metavar="FILE", help="contract overlay")
    return p


def run(args: argparse.Namespace) -> int:
    """Reconcile, print the report, gate."""
    from tokenbill.pipeline.ledger import LivePull, resolve_window, run_reconcile

    if args.live and not args.admin_key_env:
        raise UsageError("--live needs --admin-key-env VAR (the key is read from that variable)")
    if not args.live and (args.admin_key_env or args.record):
        raise UsageError("--admin-key-env and --record apply only with --live")
    if args.live and not os.environ.get(args.admin_key_env):
        raise UsageError("--admin-key-env: the variable is not set or empty")
    fmt = cli.output_format(args, ("text", "json"), "text")
    config = cli.load_config_for(args)
    env = cli.env_for(args, config=config, rates=args.rates, contract=args.contract)
    since_ms, until_ms = resolve_window(args.since, args.until, now_ms=env.now_ms)
    files = {dest: list(getattr(args, dest)) for _flag, dest, _what in _FILE_FLAGS
             if getattr(args, dest)}
    live = LivePull(key_env=args.admin_key_env, record_dir=args.record) if args.live else None
    result, written = run_reconcile(cli.require_db(args), env, files=files, since_ms=since_ms,
                                    until_ms=until_ms, tolerance_pct=args.tolerance_pct,
                                    unexplained_pct=args.unexplained_pct,
                                    closed_only=args.closed_only,
                                    suggest_contract=args.suggest_contract, live=live)
    code = cli.emit_result(args, result, config=config, default=fmt)
    report = result.reconciliation
    assert report is not None
    for path in written:
        cli.note(args, f"suggested contract written to {path}")
    if args.suggest_contract is not None and not written:
        cli.note(args, "no contract suggestion: no invoice lines to derive one from")
    passed = report.verdict == "reconciled"
    if args.suggest_contract is not None and report.rerun_verdict is not None:
        passed = report.rerun_verdict == "reconciled"
        if not passed:
            cli.note(args, "the re-run with the suggested contract is not reconciled: the "
                           "discount is not a simple multiplier")
    if args.report_only or passed:
        return code
    print(f"tokenbill: reconciliation gate failed (verdict {report.verdict}); "
          "--report-only reports without gating", file=sys.stderr)
    return cli.EXIT_GATE
