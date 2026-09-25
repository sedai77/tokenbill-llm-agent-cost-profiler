"""``tokenbill report``: the HTML fleet / team / self report (SPEC §14.3, §15).

Runs reconciliation (RECON and the extensions' reconcilers over the same window), the model gate,
the findings path and the action plan, and writes a self-contained HTML page (no scripts, strict
CSP). ``--team T`` renders one team's page (refused below k people); ``--self`` your own data.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from tokenbill.pipeline import savings as sv

VERB = "report"
HELP = "HTML report of the fleet, one team, or yourself"


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register ``report`` and its flags."""
    parser = subparsers.add_parser(VERB, help=HELP, description=HELP + ".")
    sv.add_db(parser, required=True)
    sv.add_window(parser)
    parser.add_argument("-o", "--output", dest="output", type=Path, metavar="FILE",
                        default=Path("report.html"),
                        help="HTML file to write (default report.html)")
    who = parser.add_mutually_exclusive_group()
    who.add_argument("--team", metavar="T", default=None,
                     help="one team's page (only for teams of at least k people)")
    who.add_argument("--self", dest="self_view", action="store_true",
                     help="your own data only")
    parser.add_argument("--principal-ref", metavar="REF", default=None,
                        help="with --self on a shared ledger: your principal reference (org key "
                             "holder only)")
    parser.add_argument("--reconciliation", type=Path, metavar="FILE", default=None,
                        help="a reconcile --format json output (the report recomputes the "
                             "window's reconciliation; decisions are never read from files)")
    parser.add_argument("--calibration", type=Path, metavar="FILE", default=None,
                        help="a calibrate --format json output to use instead of re-running the "
                             "model gate")
    parser.add_argument("--seed", type=sv.non_negative_int, default=0,
                        help="seed of the plan's Shapley sample (default 0)")
    sv.add_run_flags(parser)
    sv.add_format(parser)
    return parser


def run(args: argparse.Namespace) -> int:
    """Execute ``report`` (writes the HTML file, prints the summary)."""
    def body() -> int:
        env = sv.env_from_args(args)
        store, path = sv.open_ledger(args, env)
        try:
            result = sv.run_report(store, env, db_path=path, since_ms=args.since,
                                   until_ms=args.until, team=args.team,
                                   self_view=args.self_view, principal_ref=args.principal_ref,
                                   calibration=args.calibration,
                                   reconciliation=args.reconciliation, seed=args.seed,
                                   shard_max_requests=args.shard_max_requests)
        finally:
            sv.close_ledger(store)
        sv.emit(result, args, html_out=args.output)
        return sv.dq_exit(result, args, env)

    return sv.run_command(body, verb=VERB)
