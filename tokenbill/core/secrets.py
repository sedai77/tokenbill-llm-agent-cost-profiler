"""Secret detection and redaction (SPEC §3.10, §8.6).

Every tier reports **counts by type only** (``dq.secrets_observed``); values never leave this
module.
Specific detectors win over ``high_entropy`` when spans overlap.
"""

from __future__ import annotations

import math
import re
from collections import Counter

__all__ = ["SECRET_TYPES", "find_secrets", "redact", "shannon_entropy"]

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "pem_private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
            r"(?:[\s\S]*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----)?"
        ),
    ),
    ("anthropic_key", re.compile(r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{10,}")),
    ("openai_key", re.compile(r"(?<![A-Za-z0-9_-])sk-(?!ant-)[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])")),
    (
        "github_token",
        re.compile(r"(?<![A-Za-z0-9_])(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    ),
    ("slack_token", re.compile(r"(?<![A-Za-z0-9])xox[abp]-[A-Za-z0-9-]{10,}")),
    (
        "jwt",
        re.compile(
            r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{5,}\.eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"
        ),
    ),
)
_ENTROPY_CANDIDATE = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_MIN_ENTROPY_BITS = 4.0

#: Every type ``find_secrets`` may report.
SECRET_TYPES = tuple(name for name, _ in _PATTERNS) + ("high_entropy",)


def shannon_entropy(s: str) -> float:
    """Shannon entropy in bits per character (statistics, not money)."""
    if not s:
        return 0.0
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def find_secrets(text: str) -> list[tuple[str, int, int]]:
    """``(type, start, end)`` spans of likely secrets in *text*, sorted by position.

    Types: ``anthropic_key``, ``openai_key``, ``aws_access_key``, ``github_token``, ``slack_token``,
    ``jwt``, ``pem_private_key`` and ``high_entropy`` (≥ 32 chars of a base64/hex alphabet with
    Shannon
    entropy ≥ 4.0 bits/char).
    """
    found: list[tuple[str, int, int]] = []
    taken: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < e and s < end for s, e in taken)

    for name, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            if not overlaps(m.start(), m.end()):
                found.append((name, m.start(), m.end()))
                taken.append((m.start(), m.end()))
    for m in _ENTROPY_CANDIDATE.finditer(text):
        if overlaps(m.start(), m.end()):
            continue
        if shannon_entropy(m.group()) >= _MIN_ENTROPY_BITS:
            found.append(("high_entropy", m.start(), m.end()))
            taken.append((m.start(), m.end()))
    found.sort(key=lambda t: (t[1], t[2], t[0]))
    return found


def redact(text: str) -> tuple[str, Counter[str]]:
    """Replace every secret span with ``[REDACTED:<type>]``; return the text and counts by type."""
    counts: Counter[str] = Counter()
    parts: list[str] = []
    pos = 0
    for name, start, end in find_secrets(text):
        parts.append(text[pos:start])
        parts.append(f"[REDACTED:{name}]")
        counts[name] += 1
        pos = end
    parts.append(text[pos:])
    return "".join(parts), counts
