"""Error hierarchy of Token Bill v0.2 (SPEC §3.1).

Every expected failure raises a :class:`~tokenbill.common.TokenbillError` subclass with a
content-free message (SPEC §2.4). The CLI maps them to exit codes.
"""

from __future__ import annotations

from tokenbill.common import TokenbillError

__all__ = [
    "ContractViolation",
    "GateFailed",
    "PricingError",
    "PrivacyError",
    "SourceError",
    "TokenbillError",
    "UsageError",
]


class UsageError(TokenbillError):
    """Invalid invocation or input shape; CLI exit 2."""


class GateFailed(TokenbillError):
    """A gate failed (reconcile, calibrate, check, verify, export); CLI exit 3."""


class PrivacyError(TokenbillError):
    """A privacy rule would be violated (e.g. group by principal, export of content tier full)."""


class PricingError(TokenbillError):
    """Rate registry load or validation failure."""


class ContractViolation(TokenbillError):
    """A result object fails a construction-time invariant."""


class SourceError(TokenbillError):
    """An unreadable source; the message names the file and a locator only."""
