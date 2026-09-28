"""``tokenbill findings``: detectors ranked by Shapley-credited recoverable dollars (SPEC §15).

Runs the sharded findings path of :func:`tokenbill.pipeline.savings.run_findings` over a ledger:
the model gate, reconciliation, every detector per shard (``--jobs`` process pool; results are
identical for any N), k-anonymous re-scoping (the self view never suppresses), the action plan
with Shapley credits, and ``put_findings``. ``--group-by principal`` needs ``--self`` (exit 2);
``--break-glass REASON`` names runaway sessions (team and session pseudonym only) and writes an
audit row.
"""

from __future__ import annotations

import argparse

from tokenbill.pipeline import savings as sv

VERB = "findings"
HELP = "detectors over a ledger, ranked by Shapley-credited recoverable dollars"


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register ``findings`` and its flags."""
    parser = subparsers.add_parser(
        VERB, help=HELP, description=HELP + ". Findings are k-anonymous (re-scoped below k "
        "people); --self shows only your own data.")
    sv.add_db(parser, required=True)
    sv.add_window(parser)
    parser.add_argument("--detector", metavar="ID", nargs="+", action="extend", default=None,
                        help="run only these detector ids (repeatable; default: all)")
    parser.add_argument("--min-usd", type=sv.decimal_arg, metavar="X", default=None,
                        help="dollar floor of a finding's recoverable amount (default from the "
                             "config, 1.00)")
    parser.add_argument("--group-by", choices=sv.FINDINGS_GROUP_BY, default=None,
                        help="order findings by one scope dimension (principal only with --self)")
    parser.add_argument("--self", dest="self_view", action="store_true",
                        help="self view: only your own lanes, nothing suppressed")
    parser.add_argument("--principal-ref", metavar="REF", default=None,
                        help="with --self on a shared ledger: your opaque principal reference "
                             "(needs the org key; key holder only)")
    parser.add_argument("--break-glass", metavar="REASON", default=None,
                        help="name runaway sessions (tail.runaway only; team and session "
                             "pseudonym, never a person) and write an audit row")
    parser.add_argument("--no-shapley", dest="shapley", action="store_false",
                        help="skip the action plan (no Shapley credits)")
    parser.add_argument("--include-tradeoffs", action="store_true",
                        help="include trade-off levers (model/effort changes) in the plan")
    parser.add_argument("--seed", type=sv.non_negative_int, default=0,
                        help="seed of the stratified Shapley sample (default 0)")
    parser.add_argument("--sample-lanes", type=sv.positive_int, metavar="N", default=None,
                        help="Shapley sample size in lanes (default from the config, 20000)")
    sv.add_run_flags(parser)
    sv.add_format(parser)
    return parser


def run(args: argparse.Namespace) -> int:
    """Execute ``findings``; returns the exit code (0, 1, 2, or 4 under ``--strict-dq``)."""
    def body() -> int:
        env = sv.env_from_args(args, overrides={"min_usd": args.min_usd})
        store, path = sv.open_ledger(args, env)
        try:
            result = sv.run_findings(
                store, env, db_path=path, since_ms=args.since, until_ms=args.until,
                detectors=args.detector, min_usd=args.min_usd, group_by=args.group_by,
                self_view=args.self_view, principal_ref=args.principal_ref,
                break_glass=args.break_glass, shapley=args.shapley,
                include_tradeoffs=args.include_tradeoffs, seed=args.seed,
                sample_lanes=args.sample_lanes, shard_max_requests=args.shard_max_requests)
        finally:
            sv.close_ledger(store)
        sv.emit(result, args)
        return sv.dq_exit(result, args, env)

    return sv.run_command(body, verb=VERB)
