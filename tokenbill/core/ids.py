"""Stable ids, HMAC pseudonyms and key ids (SPEC §3.8, D4, D5)."""

from __future__ import annotations

import hashlib
import hmac
import re

__all__ = ["hmac_hex", "is_opaque_ref", "key_id", "pseudonym", "request_id_for", "stable_id"]

_OPAQUE_REF_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")


def stable_id(prefix: str, *parts: str | int) -> str:
    """``prefix + "_" + sha256("\\x1f".join(map(str, parts)))[:24]`` — deterministic across
    processes."""
    digest = hashlib.sha256(
        "\x1f".join(map(str, parts)).encode("utf-8", "surrogatepass")
    ).hexdigest()
    return f"{prefix}_{digest[:24]}"


def hmac_hex(key: bytes, data: bytes, n: int = 20) -> str:
    """HMAC-SHA256 of *data* under *key*, hex, truncated to *n* characters."""
    return hmac.new(key, data, hashlib.sha256).hexdigest()[:n]


def pseudonym(key: bytes, prefix: str, value: str) -> str:
    """``prefix + "_" + hmac_hex(key, value.encode())`` (e.g. ``p_<20hex>``, ``h_<20hex>``)."""
    return f"{prefix}_{hmac_hex(key, value.encode('utf-8', 'surrogatepass'))}"


def key_id(key: bytes) -> str:
    """``"k_" + sha256(b"tokenbill-key-id\\0" + key)[:12]``: names a key without revealing it."""
    return "k_" + hashlib.sha256(b"tokenbill-key-id\0" + key).hexdigest()[:12]


def request_id_for(
    provider: str, provider_message_id: str | None, source_id: str, locator: str
) -> str:
    """The logical request id: ``stable_id("rq", provider, message_id)`` when a provider message id
    exists
    (idempotent across sources and machines), else ``stable_id("rq", source_id, locator)``."""
    if provider_message_id:
        return stable_id("rq", provider, provider_message_id)
    return stable_id("rq", source_id, locator)


def is_opaque_ref(value: str) -> bool:
    """``[A-Za-z0-9._-]{1,64}`` and no ``@`` (central identity mode: never an email)."""
    return isinstance(value, str) and _OPAQUE_REF_RE.match(value) is not None
