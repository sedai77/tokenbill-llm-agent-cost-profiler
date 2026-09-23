"""Terminal/output text hardening (SPEC §3.10, §8.7).

``sanitize`` removes ANSI escape sequences, C0 and C1 control characters (keeping ``\\n`` and
``\\t``),
DEL, and Unicode bidirectional overrides/isolates (a "Trojan source" defense for terminal output),
then
truncates with ``…``.
"""

from __future__ import annotations

import re

__all__ = ["sanitize"]

_ANSI_RE = re.compile(
    r"""
    \x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?     # OSC ... BEL / ST (or unterminated)
    | \x1b[PX^_][^\x1b]*(?:\x1b\\)?         # DCS / SOS / PM / APC ... ST
    | \x1b\[[0-?]*[ -/]*[@-~]?              # CSI
    | \x9b[0-?]*[ -/]*[@-~]?                # 8-bit CSI
    | \x1b[ -/]*[0-~]?                      # other ESC sequences (two-char, charset selection)
    """,
    re.VERBOSE,
)
_CONTROL_RE = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")
_ELLIPSIS = "…"


def sanitize(text: str, limit: int | None = None) -> str:
    """Strip C0/C1 controls and ANSI escapes (keep ``\\n``, ``\\t``); truncate to *limit* chars
    with ``…``."""
    if not isinstance(text, str):
        text = str(text)
    cleaned = _CONTROL_RE.sub("", _ANSI_RE.sub("", text))
    if limit is not None:
        if limit <= 0:
            return ""
        if len(cleaned) > limit:
            cleaned = cleaned[: limit - 1] + _ELLIPSIS
    return cleaned
