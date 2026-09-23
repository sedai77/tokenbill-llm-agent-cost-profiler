"""Secret detection and redaction (SPEC §3.10, §8.6).

Every tier reports **counts by type only** (``dq.secrets_observed``); values never leave this
module. Specific detectors win over ``high_entropy`` when spans overlap.
"""

from __future__ import annotations

import math
import re
from collections import Counter

__all__ = ["SECRET_TYPES", "find_secrets", "redact", "shannon_entropy"]

#: PEM private keys: a BEGIN header through the first END footer after it (the header alone when no
#: footer follows). Matched in one linear pass (:func:`_pem_spans`) — a lazy ``BEGIN.*?END`` regex
#: rescans to the end of the text for every footer-less header, quadratic on hostile content.
_PEM_BEGIN = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")
_PEM_END = re.compile(r"-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----")
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
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
SECRET_TYPES = ("pem_private_key", *(name for name, _ in _PATTERNS), "high_entropy")


def shannon_entropy(s: str) -> float:
    """Shannon entropy in bits per character (statistics, not money)."""
    if not s:
        return 0.0
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def _mixed_classes(s: str) -> bool:
    """Upper case, lower case and digits all present."""
    return (any(c.isupper() for c in s) and any(c.islower() for c in s)
            and any(c.isdigit() for c in s))


def _is_high_entropy(candidate: str) -> bool:
    """SPEC §3.10: ≥ 32 characters of the base64/hex alphabet (the caller's regex) with Shannon
    entropy ≥ 4.0 bits/char. One narrow exception keeps file paths out: a candidate containing
    ``/`` must also mix upper case, lower case and digits (``/usr/local/lib/python3/site-packages``
    shares the alphabet; a random token with a ``/`` practically always mixes all three)."""
    if shannon_entropy(candidate) < _MIN_ENTROPY_BITS:
        return False
    return "/" not in candidate or _mixed_classes(candidate)


def _pem_spans(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of every PEM private key: header through the first footer after it, or the
    header alone; linear time (the footer search only ever moves forward)."""
    spans: list[tuple[int, int]] = []
    footer: re.Match[str] | None = None
    no_more_footers = False
    covered = 0
    for header in _PEM_BEGIN.finditer(text):
        if header.start() < covered:  # inside the previous key
            continue
        if not no_more_footers and (footer is None or footer.start() < header.end()):
            footer = _PEM_END.search(text, header.end())
            no_more_footers = footer is None
        end = footer.end() if footer is not None and not no_more_footers else header.end()
        spans.append((header.start(), end))
        covered = end
    return spans


def find_secrets(text: str) -> list[tuple[str, int, int]]:
    """``(type, start, end)`` spans of likely secrets in *text*, sorted by position.

    Types: ``anthropic_key``, ``openai_key``, ``aws_access_key``, ``github_token``, ``slack_token``,
    ``jwt``, ``pem_private_key`` and ``high_entropy`` (≥ 32 chars of a base64/hex alphabet with
    Shannon entropy ≥ 4.0 bits/char; a candidate containing ``/`` must also mix upper case, lower
    case and digits, so file paths do not match — see :func:`_is_high_entropy`).
    """
    found: list[tuple[str, int, int]] = []
    taken: list[tuple[int, int]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < e and s < end for s, e in taken)

    for start, end in _pem_spans(text):
        found.append(("pem_private_key", start, end))
        taken.append((start, end))
    for name, pattern in _PATTERNS:
        for m in pattern.finditer(text):
            if not overlaps(m.start(), m.end()):
                found.append((name, m.start(), m.end()))
                taken.append((m.start(), m.end()))
    for m in _ENTROPY_CANDIDATE.finditer(text):
        if overlaps(m.start(), m.end()):
            continue
        if _is_high_entropy(m.group()):
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
