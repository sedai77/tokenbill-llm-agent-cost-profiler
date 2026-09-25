"""``tokenbill me``: your own Claude Code usage — a pure alias of ``tokenbill scan --self``.

It has the flags of ``scan``'s self view and no logic of its own (SPEC §15). GitHub Copilot users:
``me --copilot`` is rewritten to ``copilot me`` before argparse (``core.extensions.rewrite_argv``).
"""

from __future__ import annotations

import argparse

from tokenbill.commands import scan

VERB = "me"
HELP = "your own usage: alias of scan --self"


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register ``me`` with the flags of ``scan``'s self view."""
    parser = subparsers.add_parser(VERB, help=HELP, description=HELP + ".")
    scan.add_self_arguments(parser)
    parser.set_defaults(self_view=True, org=False)
    return parser


def run(args: argparse.Namespace) -> int:
    """Execute ``me`` (``scan --self``)."""
    return scan.run(args, self_alias=True)
