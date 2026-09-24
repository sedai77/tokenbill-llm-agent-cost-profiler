"""Command module of the test-only ``fake`` extension (``add_parser`` / ``run``)."""

from __future__ import annotations

from typing import Any


def add_parser(subparsers: Any) -> Any:
    """Register the ``fake`` verb."""
    return subparsers.add_parser("fake")


def run(args: Any) -> int:
    """Nothing to do."""
    return 0
