"""``tokenbill calibrate``: the model gate (SPEC §9.6, §15).

Streams the ledger's shards twice (``sim.calibrate`` pass 1, ρ fitted out of fold, pass 2) and
reports NMBE / CV(RMSE) against the FEMP thresholds. Only a passing gate labels projections
CALIBRATED. ``--require-pass`` exits 3 when the status is not ``pass``.
"""

from __future__ import annotations

import argparse

from tokenbill.core.errors import GateFailed
from tokenbill.pipeline import savings as sv

VERB = "calibrate"
HELP = "the model gate: predictive calibration of the replay model against billed usage"


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register ``calibrate`` and its flags."""
    parser = subparsers.add_parser(VERB, help=HELP, description=HELP + ".")
    sv.add_db(parser, required=True)
    sv.add_window(parser)
    parser.add_argument("--granularity", choices=("day", "month"), default="day",
                        help="comparison period (FEMP thresholds: day 10%%/30%%, month 5%%/15%%; "
                             "default day)")
    parser.add_argument("--require-pass", action="store_true",
                        help="exit 3 unless the gate passes")
    sv.add_run_flags(parser)
    sv.add_format(parser)
    return parser


def run(args: argparse.Namespace) -> int:
    """Execute ``calibrate``; exit 3 with ``--require-pass`` when the gate does not pass."""
    def body() -> int:
        env = sv.env_from_args(args)
        store, path = sv.open_ledger(args, env)
        try:
            result = sv.run_calibrate(store, env, db_path=path, since_ms=args.since,
                                      until_ms=args.until, granularity=args.granularity,
                                      shard_max_requests=args.shard_max_requests)
        finally:
            sv.close_ledger(store)
        sv.emit(result, args)
        report = result.calibration
        if args.require_pass and (report is None or report.status != "pass"):
            status = report.status if report is not None else "missing"
            raise GateFailed(f"model gate status {status} (--require-pass)")
        return sv.dq_exit(result, args, env)

    return sv.run_command(body, verb=VERB)
