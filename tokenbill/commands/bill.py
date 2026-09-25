"""``tokenbill bill`` — exact priced totals (SPEC §15, §14.1, D26, D27; CLI-LEDGER).

The exact bill (billed-eligible lines only), with the estimated ranges, the seat-allowance and
Copilot-pool list-equivalent figures shown beside it — never added to it — plus ESR, the Claude Code
naive line-sum ratio and k-anonymous breakdowns (``--group-by team,model``, repeatable). Grouping by
a person is refused (exit 2); ``--self`` is the self view of a personal ledger.
"""

from __future__ import annotations

import argparse
import csv
import io
from pathlib import Path
from typing import Any

from tokenbill import cli
from tokenbill.core.errors import UsageError

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def add_parser(subparsers: Any) -> argparse.ArgumentParser:
    """Register ``bill``."""
    p = subparsers.add_parser("bill", help=cli.COMMAND_HELP["bill"])
    p.add_argument("--rates", type=Path, action="append", default=[], metavar="FILE",
                   help="extra rate file (repeatable; used with --reprice)")
    p.add_argument("--contract", type=Path, metavar="FILE", help="contract overlay")
    p.add_argument("--basis", choices=("list", "contract"), default=None,
                   help="price at list or contract rates (contract needs --contract or the "
                        "config's contract); applied with --reprice")
    p.add_argument("--group-by", action="append", default=[], metavar="DIMS",
                   help="breakdown by comma-separated dimensions (repeatable), e.g. team,model")
    p.add_argument("--since", metavar="DATE", help="window start (UTC date, inclusive)")
    p.add_argument("--until", metavar="DATE", help="window end (UTC date, exclusive)")
    p.add_argument("--self", dest="self_view", action="store_true",
                   help="self view of a personal ledger (no k-suppression)")
    p.add_argument("--reprice", action="store_true",
                   help="re-price the window with this run's rates / contract / basis first")
    p.add_argument("--format", choices=("text", "json", "csv"), default=argparse.SUPPRESS,
                   help="output format (default text)")
    return p


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def to_csv(result: Any) -> str:
    """The bill as CSV: one ``total`` row, then one row per published breakdown row. Money as exact
    decimal USD strings plus int nano; list-equivalent figures in their own columns."""
    from tokenbill.core.kanon import USERS_UNKNOWN, row_notes
    from tokenbill.core.money import nano_to_usd_str

    def money(fig: Any) -> tuple[str, str, str, str]:
        if fig is None:
            return ("", "", "", "")
        if fig.nano is None:
            return ("unpriced", "", fig.evidence.value, fig.basis.value)
        return (nano_to_usd_str(fig.nano), str(fig.nano), fig.evidence.value, fig.basis.value)

    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["breakdown", "group", "users", "requests", "exact_usd", "exact_nano",
                "exact_evidence", "exact_basis", "allowance_usd", "allowance_nano",
                "unpriced_inferences"])
    bill = result.bill
    t = bill.total
    w.writerow(["total", "", "", "", *money(t.exact), *money(t.allowance)[:2],
                t.unpriced_inferences])
    for key, agg in bill.breakdowns:
        for row in agg.rows:
            group = ";".join(f"{k}={'' if v is None else v}" for k, v in row.dims)
            users = ("users unknown" if USERS_UNKNOWN in row_notes(row, group_by=agg.group_by)
                     else row.n_users)
            w.writerow([_cell(key), _cell(group), users, row.n_requests,
                        *money(row.priced.exact), *money(row.priced.allowance)[:2],
                        row.priced.unpriced_inferences])
    return out.getvalue()


def run(args: argparse.Namespace) -> int:
    """Print the bill."""
    from tokenbill.pipeline.ledger import resolve_window, run_bill

    fmt = cli.output_format(args, ("text", "json", "csv"), "text")
    config = cli.load_config_for(args)
    contract = args.contract
    if args.basis == "contract" and contract is None and config.contract is None:
        raise UsageError("--basis contract needs --contract FILE (or a contract in the config)")
    if args.basis == "list":
        import dataclasses

        config = dataclasses.replace(config, contract=None)
        contract = None
    env = cli.env_for(args, config=config, rates=args.rates, contract=contract)
    since_ms, until_ms = resolve_window(args.since, args.until, now_ms=env.now_ms)
    result = run_bill(cli.require_db(args), env, since_ms=since_ms, until_ms=until_ms,
                      group_by=args.group_by, self_view=args.self_view, reprice=args.reprice)
    if fmt == "csv":
        print(to_csv(result), end="")
        return cli.strict_dq_code(args, cli.dq_warnings(result), config)
    return cli.emit_result(args, result, config=config, default=fmt)
