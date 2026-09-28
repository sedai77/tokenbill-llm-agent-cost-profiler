"""``tokenbill whatif``: counterfactual replays of policies over a ledger (SPEC §9, §15).

Each ``--policy SPEC`` (SPEC §9.5 grammar, e.g. ``ttl=1h@lane_kind:main``) is replayed per billing
class in the documented and/or calibrated mode; savings carry their label and calibration.
``--shapley`` splits the policies' joint saving on a seeded stratified sample.
"""

from __future__ import annotations

import argparse

from tokenbill.pipeline import savings as sv

VERB = "whatif"
HELP = "counterfactual replays: what a policy would have cost on your recorded usage"


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register ``whatif`` and its flags."""
    parser = subparsers.add_parser(VERB, help=HELP, description=HELP + ".")
    sv.add_db(parser, required=True)
    sv.add_window(parser)
    parser.add_argument("--policy", metavar="SPEC", action="append", required=True,
                        help="policy to replay, e.g. ttl=1h@lane_kind:main (repeatable)")
    parser.add_argument("--mode", choices=("documented", "calibrated", "both"),
                        default="documented",
                        help="replay mode (calibrated needs a passing model gate; default "
                             "documented)")
    parser.add_argument("--shapley", action="store_true",
                        help="split the policies' joint saving by Shapley value (sample)")
    parser.add_argument("--sample-lanes", type=sv.positive_int, metavar="N", default=None,
                        help="Shapley sample size in lanes (default from the config, 20000)")
    parser.add_argument("--seed", type=sv.non_negative_int, default=0,
                        help="seed of the Shapley sample (default 0)")
    sv.add_run_flags(parser)
    sv.add_format(parser)
    return parser


def run(args: argparse.Namespace) -> int:
    """Execute ``whatif``."""
    def body() -> int:
        env = sv.env_from_args(args)
        store, path = sv.open_ledger(args, env)
        try:
            result = sv.run_whatif(store, env, policies=args.policy, db_path=path,
                                   since_ms=args.since, until_ms=args.until, mode=args.mode,
                                   shapley=args.shapley, sample_lanes=args.sample_lanes,
                                   seed=args.seed, shard_max_requests=args.shard_max_requests)
        finally:
            sv.close_ledger(store)
        sv.emit(result, args)
        return sv.dq_exit(result, args, env)

    return sv.run_command(body, verb=VERB)
