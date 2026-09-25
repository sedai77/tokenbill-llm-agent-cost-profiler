"""A minimal local ``tokenbill`` front end for the CLI-SAVINGS command modules.

CLI-LEDGER owns ``tokenbill/cli.py`` (the static ``COMMANDS`` table and the global flags); these
tests never edit it. :func:`main` mimics its contract (SPEC §15): global flags before the verb,
``core.extensions.rewrite_argv`` before argparse, one lazily imported module per verb exposing
``add_parser(subparsers)`` and ``run(args) -> int``, argparse usage errors exit 2.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import io
from collections.abc import Sequence
from dataclasses import dataclass

from tokenbill.core import extensions

#: The dotted paths CLI-LEDGER's ``COMMANDS`` table lists for CLI-SAVINGS's verbs.
COMMANDS: dict[str, str] = {
    "scan": "tokenbill.commands.scan",
    "me": "tokenbill.commands.me",
    "calibrate": "tokenbill.commands.calibrate",
    "findings": "tokenbill.commands.findings",
    "whatif": "tokenbill.commands.whatif",
    "policy": "tokenbill.commands.policy",
    "measure": "tokenbill.commands.measure",
    "ab": "tokenbill.commands.ab",
    "receipt": "tokenbill.commands.receipt",
    "check": "tokenbill.commands.check",
    "report": "tokenbill.commands.report",
}


def build_parser(verbs: Sequence[str] | None = None) -> argparse.ArgumentParser:
    """The parser: SPEC §15 global flags, then every verb's ``add_parser``."""
    parser = argparse.ArgumentParser(prog="tokenbill")
    parser.add_argument("--config", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--format", choices=("text", "json"), default=None)
    parser.add_argument("--deterministic", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("-v", dest="verbose", action="count", default=0)
    parser.add_argument("--log-json", action="store_true")
    parser.add_argument("--plugins", action="store_true")
    parser.add_argument("--strict-dq", action="store_true")
    parser.add_argument("--jobs", type=int, default=None)
    sub = parser.add_subparsers(dest="command", metavar="command", required=True)
    for verb in verbs or COMMANDS:
        importlib.import_module(COMMANDS[verb]).add_parser(sub)
    return parser


@dataclass
class Outcome:
    """Exit code and captured streams of one :func:`main` call."""

    code: int
    out: str
    err: str


def main(argv: Sequence[str]) -> Outcome:
    """Run ``tokenbill ARGV`` in-process (stdout/stderr captured)."""
    out, err = io.StringIO(), io.StringIO()
    args_list = extensions.rewrite_argv(list(argv))
    code: int
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        parser = build_parser()
        try:
            args = parser.parse_args(args_list)
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 2
            return Outcome(code, out.getvalue(), err.getvalue())
        module = importlib.import_module(COMMANDS[args.command])
        code = module.run(args)
    return Outcome(code, out.getvalue(), err.getvalue())
