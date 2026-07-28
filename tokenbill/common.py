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
    masquerade as a prompt change. Non-finite floats raise ``ValueError``:
    ``NaN``/``Infinity`` are not valid JSON, and emitting them would create
    files ``trace.read_trace`` (correctly) rejects.
    """
    return _CANONICAL_ENCODER.encode(value)


# Prebuilt encoder: json.dumps with non-default options builds a fresh
# JSONEncoder per call, which is measurable overhead when rendering millions
# of small message dicts on the analysis hot path.
_CANONICAL_ENCODER = json.JSONEncoder(
    sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
)
