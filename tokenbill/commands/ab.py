"""``tokenbill ab``: paired lab comparison of two agent configurations (SPEC §13.5).

Two usage sets (trace@2 files — or any file an adapter reads — or ledgers) whose requests carry
the ``task_id`` attribution, and an outcomes table (``task_id, arm, trial, success[, order]`` as a
JSON array, JSON lines or CSV). Per-task paired cost differences, cost per success (failures stay
in the numerator), a task-clustered seeded bootstrap and the verdict ``cheaper`` /
``no-difference`` / ``costlier``. VERIFIED only at the lab scope with randomized arm order and
at least five trials per task-arm.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from tokenbill.pipeline import savings as sv
from tokenbill.pipeline import verification as vf

VERB = "ab"
HELP = "paired lab A/B of two agent configurations on the same tasks"


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register ``ab`` and its flags."""
    parser = subparsers.add_parser(VERB, help=HELP, description=HELP + ".")
    parser.add_argument("--baseline", required=True, type=Path, metavar="FILE",
                        help="baseline usage (trace@2 file or ledger)")
    parser.add_argument("--candidate", required=True, type=Path, metavar="FILE",
                        help="candidate usage (trace@2 file or ledger)")
    parser.add_argument("--outcomes", required=True, type=Path, metavar="FILE",
                        help="outcomes: task_id, arm, trial, success[, order]")
    parser.add_argument("--boot", type=sv.positive_int, default=10_000,
                        help="bootstrap resamples (default 10000)")
    parser.add_argument("--seed", type=sv.non_negative_int, default=0,
                        help="bootstrap seed (default 0)")
    sv.add_format(parser)
    return parser


def run(args: argparse.Namespace) -> int:
    """Execute ``ab``."""
    def body() -> int:
        env = sv.env_from_args(args)
        result = vf.run_ab(env, baseline=args.baseline, candidate=args.candidate,
                           outcomes=args.outcomes, boot=args.boot, seed=args.seed)
        sv.emit(result, args)
        return sv.dq_exit(result, args, env)

    return sv.run_command(body, verb=VERB)
