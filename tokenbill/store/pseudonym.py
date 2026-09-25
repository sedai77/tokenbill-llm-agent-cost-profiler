"""Write-time pseudonymization of principals and name hashes (SPEC §7.6, §8.3, D5, D39, R-E21).

Collector files carry ``r_<opaque ref>`` (central mode) or ``c_<20 hex>`` (two-stage mode)
principals; the store converts them to ``p_`` with the **org key** before anything is written.
``p_`` values (and ``h_`` names) are only kept when the source says they were made under a key id
the store accepts: its own org / name key id, or the one key id it adopted from a Copilot export
bundle (R-E21). The raw value is never written anywhere.
"""

from __future__ import annotations

from collections.abc import Iterable

from tokenbill.core.errors import PrivacyError, UsageError
from tokenbill.core.ids import key_id as _key_id
from tokenbill.core.ids import pseudonym

__all__ = ["Pseudonymizer", "is_collector_ref", "is_name_hash"]

_OWN = object()   # sentinel: "made under this Pseudonymizer's own org key"


def is_collector_ref(value: object) -> bool:
    """True for an in-transit collector principal (``r_…`` or ``c_…``)."""
    return isinstance(value, str) and value.startswith(("r_", "c_"))


def is_name_hash(value: object) -> bool:
    """True for an ``h_…`` name hash (MCP / skill / plugin / repo / workspace / cwd names)."""
    return isinstance(value, str) and value.startswith("h_")


class Pseudonymizer:
    """``Pseudonymizer(org_key).principal(value)`` (SPEC §7.6).

    * ``r_<ref>`` → ``pseudonym(org_key, "p", ref)``;
    * ``c_<hex>`` → ``pseudonym(org_key, "p", "c:" + hex)``;
    * ``p_…`` passes through only when *key_id* (the source's ``principal_key_id``) is accepted —
      the org key's own id (the default: the caller vouches the value was made with this key) or
      one of *accepted_key_ids* (the adopted Copilot export key id, R-E21); otherwise None.

    Without an org key, ``r_`` / ``c_`` values raise :class:`PrivacyError` (they must never be
    stored in transit form).
    """

    __slots__ = ("_accepted", "_key", "key_id")

    def __init__(self, org_key: bytes | None, *, accepted_key_ids: Iterable[str] = ()) -> None:
        if org_key is not None and (not isinstance(org_key, (bytes, bytearray)) or not org_key):
            raise UsageError("the org key must be non-empty bytes")
        self._key = bytes(org_key) if org_key is not None else None
        self.key_id: str | None = _key_id(self._key) if self._key is not None else None
        accepted = {k for k in accepted_key_ids if k}
        if self.key_id is not None:
            accepted.add(self.key_id)
        self._accepted = frozenset(accepted)

    @property
    def accepted_key_ids(self) -> frozenset[str]:
        """Key ids whose ``p_`` values are kept (own org key id and adopted ids)."""
        return self._accepted

    def accepts(self, key_id: str | None) -> bool:
        """Whether ``p_`` values made under *key_id* are kept."""
        return key_id is not None and key_id in self._accepted

    def principal(self, value: str | None, *, key_id: object = _OWN) -> str | None:
        """The stored form of *value* (see the class docstring); None stays None."""
        if value is None:
            return None
        if not isinstance(value, str):
            raise UsageError("a principal must be a string")
        if value.startswith("r_"):
            return pseudonym(self._require_key(), "p", value[2:])
        if value.startswith("c_"):
            return pseudonym(self._require_key(), "p", "c:" + value[2:])
        if value.startswith("p_"):
            if key_id is _OWN:
                return value if self._key is not None else None
            return value if self.accepts(key_id if isinstance(key_id, str) else None) else None
        raise UsageError("a principal must be p_, c_ or r_")

    def _require_key(self) -> bytes:
        if self._key is None:
            raise PrivacyError("collector principals (r_/c_) need the org key to be stored")
        return self._key
