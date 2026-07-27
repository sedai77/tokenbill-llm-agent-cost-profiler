"""Shared primitives: exceptions, seeded randomness, canonical serialization."""

from __future__ import annotations

import hashlib
import json
import random
from typing import Any


class TokenbillError(Exception):
    """Base class for all Token Bill errors."""


class TraceError(TokenbillError):
    """A trace file is missing, malformed, or fails schema validation."""


def rng(seed: int, *scope: object) -> random.Random:
    """A deterministic RNG namespaced by *scope*.

    Built on a stable digest — never ``hash()``, which is salted per process
    for strings and would silently break reproducibility.
    """
    digest = hashlib.sha256(repr((seed, *scope)).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def canonical_json(value: Any) -> str:
    """Deterministic JSON rendering: sorted keys, no whitespace variance.

    Used wherever two calls' payloads are compared byte-for-byte (prefix
    reconstruction, fingerprints) — a dict-ordering difference must never
    masquerade as a prompt change.
    """
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
