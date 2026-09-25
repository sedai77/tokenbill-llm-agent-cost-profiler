"""``tokenbill check``: the CI gate (SPEC §15.2).

Reads the trace@1 / trace@2 files of a recorded smoke test (fingerprinted on import) or a ledger
(``--db``) and checks the cache-read share after the warm-up turns, new cache-breaker kinds versus
a baseline (a previous ``check --format json``), the median cost per run (≥ ``--min-runs`` runs)
and serialization churn. Outputs text, result@2 JSON or SARIF 2.1.0; ``--summary-md`` writes the
Markdown summary (Δ$ per 1,000 runs), ``--write-baseline`` the next baseline. Exit 3 when a check
selected by ``--fail-on`` fails.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tokenbill.core.errors import GateFailed
from tokenbill.pipeline import savings as sv

VERB = "check"
HELP = "CI gate: cache-read share, new cache breakers, cost regression, serialization churn"


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register ``check`` and its flags."""
    parser = subparsers.add_parser(VERB, help=HELP, description=HELP + ".")
    parser.add_argument("traces", nargs="*", type=Path, metavar="TRACE",
                        help="trace@1 / trace@2 files of the smoke test (or use --db)")
    sv.add_db(parser, help_text="judge a ledger instead of trace files")
    sv.add_window(parser)
    parser.add_argument("--min-cache-read-share", type=sv.decimal_arg, default="0.80",
                        metavar="X", help="minimum cache-read share after --after-turn "
                                          "(default 0.80)")
    parser.add_argument("--after-turn", type=sv.non_negative_int, default=2, metavar="N",
                        help="warm-up turns excluded from the share (default 2)")
    parser.add_argument("--baseline", type=Path, metavar="FILE", default=None,
                        help="a previous check --format json output")
    parser.add_argument("--max-cost-regression-pct", type=sv.decimal_arg, default="15",
                        metavar="PCT", help="allowed median cost growth over the baseline "
                                            "(default 15)")
    parser.add_argument("--min-runs", type=sv.positive_int, default=3, metavar="N",
                        help="runs needed on both sides to compare costs (default 3)")
    parser.add_argument("--fail-on", choices=("breaker", "regression", "share", "any"),
                        default="any", help="which checks fail the gate (default any)")
    parser.add_argument("--summary-md", type=Path, metavar="FILE", default=None,
                        help="write the Markdown summary (e.g. $GITHUB_STEP_SUMMARY)")
    parser.add_argument("--write-baseline", type=Path, metavar="FILE", default=None,
                        help="write this run as the next baseline (JSON)")
    sv.add_format(parser, ("text", "json", "sarif"))
    return parser


def run(args: argparse.Namespace) -> int:
    """Execute ``check``: 0 when the gate passes, 3 when it fails."""
    from tokenbill import __version__, gate

    def body() -> int:
        env = sv.env_from_args(args)
        options = gate.CheckOptions(min_cache_read_share=args.min_cache_read_share,
                                    after_turn=args.after_turn,
                                    max_cost_regression_pct=args.max_cost_regression_pct,
                                    min_runs=args.min_runs, fail_on=args.fail_on)
        store, _path = sv.open_ledger(args, env, required=False)
        try:
            result = sv.run_check_result(args.traces, env=env, store=store, since_ms=args.since,
                                         until_ms=args.until, options=options,
                                         baseline=args.baseline)
        finally:
            sv.close_ledger(store)
        check = result.check
        assert check is not None
        if args.summary_md is not None:
            sv.write_text(args.summary_md, check.summary_md)
        if args.write_baseline is not None:
            sv.write_text(args.write_baseline, sv.render(result, "json", deterministic=True))
        if sv.fmt_of(args) == "sarif":
            from tokenbill.outputs.sarif import to_sarif

            doc = to_sarif(check, tool_version=__version__)
            print(json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=True))
        else:
            sv.emit(result, args)
        if not check.passed:
            failed = sorted({v.rule_id for v in check.violations if v.level == "error"})
            raise GateFailed("check failed: " + ", ".join(failed))
        return sv.EXIT_OK

    return sv.run_command(body, verb=VERB)
