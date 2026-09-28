"""``tokenbill policy``: per-cohort policy packs, and ``policy check-effect`` (SPEC §11.3, §11.5).

``policy`` runs the findings path and builds the packs of one target from the action plan:
``claude-code`` (managed-settings merge patch, rollback patch, README, hooks), ``litellm``,
``sdk``, or an extension target such as ``github-copilot`` (dispatched through
``core.extensions.policy_targets``). ``-o DIR`` writes the pack files; nothing is ever applied.
``policy check-effect`` reports whether a rolled-out setting is observed in a cohort's lanes.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from tokenbill.core.errors import UsageError
from tokenbill.pipeline import savings as sv

VERB = "policy"
HELP = "policy packs (settings patches) for the recommended levers; check-effect after rollout"
_BUILTIN_TARGETS = ("claude-code", "litellm", "sdk")


def _targets() -> list[str]:
    from tokenbill.core import extensions

    return sorted({*_BUILTIN_TARGETS, *extensions.policy_targets()})


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register ``policy`` and ``policy check-effect``."""
    parser = subparsers.add_parser(VERB, help=HELP, description=HELP + ".")
    sv.add_db(parser, required=True)
    sv.add_window(parser)
    parser.add_argument("--target", default="claude-code", metavar="TARGET",
                        help="pack target: claude-code (default), litellm, sdk, or an extension "
                             "target such as github-copilot")
    parser.add_argument("--current", type=Path, metavar="FILE", default=None,
                        help="the current settings (JSON object) the merge patch applies to")
    parser.add_argument("--cohort-by", choices=("team", "mdm-group"), default=None,
                        help="one pack per team or MDM group (default: one org-wide pack)")
    parser.add_argument("--include-tradeoffs", action="store_true",
                        help="include trade-off levers (they need an ab or measure run first)")
    parser.add_argument("--contract", type=Path, metavar="FILE", default=None,
                        help="contract overlay: emit modelPricing from it")
    parser.add_argument("-o", "--out-dir", dest="out_dir", type=Path, metavar="DIR",
                        default=None, help="write the pack files under DIR")
    parser.add_argument("--seed", type=sv.non_negative_int, default=0,
                        help="seed of the plan's Shapley sample (default 0)")
    sv.add_run_flags(parser)
    sv.add_format(parser)
    actions = parser.add_subparsers(dest="policy_action", metavar="ACTION")
    check = actions.add_parser(
        "check-effect", help="post-rollout effectiveness of one setting",
        description="Report whether a rolled-out setting is observed in a cohort's lanes "
                    "(a setting-not-effective finding when it is not).")
    sv.add_db(check, required=True)
    check.add_argument("--lever", required=True, metavar="ID",
                       help="lever id, optionally with its value (e.g. "
                            "cc.autocompact_window=400000)")
    check.add_argument("--cohort", default="all", metavar="C",
                       help="'all', a team or an MDM group (default all)")
    check.add_argument("--since", type=sv.date_arg, required=True, metavar="DATE",
                       help="rollout date, YYYY-MM-DD (UTC)")
    check.add_argument("--days", type=sv.positive_int, default=7,
                       help="days observed after the rollout (default 7)")
    check.add_argument("--value", dest="target_value", default=None, metavar="VALUE",
                       help="the rolled-out value when --lever does not carry it")
    sv.add_format(check)
    return parser


def _current(path: Path | None) -> dict | None:
    if path is None:
        return None
    doc = sv.load_json_arg(path, "--current")
    if not isinstance(doc, dict):
        raise UsageError("--current must hold a JSON object (the current settings)")
    return doc


def run(args: argparse.Namespace) -> int:
    """Execute ``policy`` or ``policy check-effect``."""
    def body() -> int:
        env = sv.env_from_args(args)
        store, path = sv.open_ledger(args, env)
        try:
            if getattr(args, "policy_action", None) == "check-effect":
                result = sv.run_check_effect(store, env, lever_id=args.lever,
                                             cohort=args.cohort, since_ms=args.since,
                                             days=args.days, target=args.target_value)
            else:
                if args.target not in _targets():
                    raise UsageError(f"unknown --target {args.target!r} (one of "
                                     f"{', '.join(_targets())})")
                result = sv.run_policy(store, env, target=args.target, db_path=path,
                                       since_ms=args.since, until_ms=args.until,
                                       current=_current(args.current), cohort_by=args.cohort_by,
                                       include_tradeoffs=args.include_tradeoffs,
                                       contract=args.contract, out_dir=args.out_dir,
                                       seed=args.seed,
                                       shard_max_requests=args.shard_max_requests)
        finally:
            sv.close_ledger(store)
        sv.emit(result, args)
        return sv.dq_exit(result, args, env)

    return sv.run_command(body, verb=VERB)
