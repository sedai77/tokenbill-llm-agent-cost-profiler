"""``tokenbill scan``: one-shot local Claude Code review (self view), or ``scan --org`` (SPEC §15).

``scan`` reads the local Claude Code transcripts (``--claude-dir``, default
``~/.claude/projects``) into a temporary ledger (``--db PATH`` keeps it), prices them and runs the
findings path in the self view: the exact bill with label chips (allowance apart on the
subscription billing path), data quality (incl. the naive line-sum ratio), the model gate and the
top recoverable levers. ``me`` is an alias of ``scan --self``.

``scan --org`` is the aggregate-only org scan (D32): Admin / Analytics pages and cloud billing
exports through the ADMIN adapters (or ``--live --admin-key-env VAR``), reconciled per channel,
with the org-scan findings — no per-request data needed.

GitHub Copilot: ``scan --copilot`` is rewritten to ``copilot scan`` before argparse
(``core.extensions.rewrite_argv``); this parser adds no Copilot flag.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from tokenbill.core.errors import UsageError
from tokenbill.core.records import BILLING_PATHS
from tokenbill.pipeline import savings as sv

VERB = "scan"
HELP = "one-shot local review of your Claude Code usage (self view); --org: org-wide scan"
_ORG_FLAGS = (("usage_report", "--usage-report", "Anthropic Admin usage_report pages"),
              ("cost_report", "--cost-report", "Anthropic Admin cost_report pages"),
              ("cc_analytics", "--cc-analytics", "Claude Code Analytics pages"),
              ("enterprise_analytics", "--enterprise-analytics", "Enterprise Analytics pages"),
              ("aws_cur", "--aws-cur", "AWS CUR 2.0 export (CSV)"),
              ("gcp_billing", "--gcp-billing", "GCP billing export (CSV/JSONL)"))


def add_self_arguments(parser: argparse.ArgumentParser) -> None:
    """The flags of the self view (shared by ``scan`` and ``me``)."""
    parser.add_argument("--claude-dir", type=Path, metavar="DIR", default=None,
                        help="Claude Code projects directory (default ~/.claude/projects)")
    sv.add_window(parser)
    sv.add_db(parser, help_text="keep the ledger at PATH (default: a temporary ledger, deleted)")
    parser.add_argument("--rates", type=Path, metavar="FILE", action="append", default=None,
                        help="extra rate file (tokenbill/rates@1; repeatable)")
    parser.add_argument("--contract", type=Path, metavar="FILE", default=None,
                        help="contract overlay (prices on the contract basis)")
    parser.add_argument("-o", "--output", dest="output", type=Path, metavar="FILE",
                        default=None, help="also write the HTML report to FILE")
    parser.add_argument("--billing-path", choices=BILLING_PATHS, default=None,
                        help="how this usage is billed (subscription: allowance, list-equivalent;"
                             " default from the config threshold scan.billing_path, else "
                             "unknown)")
    parser.add_argument("--seed", type=sv.non_negative_int, default=0,
                        help="seed of the plan's Shapley sample (default 0)")
    parser.add_argument("--jobs", type=sv.positive_int, metavar="N", default=argparse.SUPPRESS,
                        help="process pool over shards (default 1)")
    sv.add_format(parser)


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register ``scan`` (self view and ``--org``)."""
    parser = subparsers.add_parser(VERB, help=HELP, description=HELP + ".")
    add_self_arguments(parser)
    parser.add_argument("--self", dest="self_view", action="store_true",
                        help="self view (the default of scan; kept for symmetry with me)")
    org = parser.add_argument_group("org scan (--org)")
    org.add_argument("--org", action="store_true",
                     help="aggregate-only org scan from provider files (no transcripts)")
    for dest, flag, what in _ORG_FLAGS:
        org.add_argument(flag, dest=dest, type=Path, nargs="+", action="extend", metavar="FILE",
                         default=None, help=f"{what} (repeatable)")
    org.add_argument("--live", action="store_true",
                     help="pull the Admin API pages (needs --admin-key-env; the only network use)")
    org.add_argument("--admin-key-env", metavar="VAR", default=None,
                     help="environment variable holding the Admin API key (never printed)")
    org.add_argument("--team-map", type=Path, metavar="FILE", default=None,
                     help="JSON object mapping actor references to teams (applied at ingest)")
    return parser


def _org_files(args: argparse.Namespace) -> dict[str, list[Path]]:
    return {dest: list(getattr(args, dest, None) or []) for dest, _flag, _what in _ORG_FLAGS}


def run(args: argparse.Namespace, *, self_alias: bool = False) -> int:
    """Execute ``scan`` (``self_alias`` is set by ``me``)."""
    verb = "me" if self_alias else VERB

    def body() -> int:
        org = bool(getattr(args, "org", False))
        files = _org_files(args)
        if org and (self_alias or getattr(args, "self_view", False)):
            raise UsageError("--org and --self are exclusive")
        if not org and (any(files.values()) or getattr(args, "live", False)):
            raise UsageError("provider files and --live need --org")
        env = sv.env_from_args(args, rates=tuple(args.rates or ()), contract=args.contract)
        db = getattr(args, "db", None)
        db_path = Path(db).expanduser() if db else None
        if org:
            result = sv.run_scan_org(env, files=files, live=args.live,
                                     admin_key_env=args.admin_key_env, team_map=args.team_map,
                                     since_ms=args.since, until_ms=args.until, db_path=db_path)
        else:
            result = sv.run_scan(env, claude_dir=args.claude_dir, since_ms=args.since,
                                 until_ms=args.until, db_path=db_path,
                                 billing_path=args.billing_path, seed=args.seed)
        sv.emit(result, args, html_out=args.output)
        return sv.dq_exit(result, args, env)

    return sv.run_command(body, verb=verb)
