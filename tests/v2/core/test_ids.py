"""SPEC §3.8 ids: stable ids, HMAC pseudonyms, key ids, request ids, opaque refs."""

from __future__ import annotations

import hashlib
import hmac
import re

from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core.ids import hmac_hex, is_opaque_ref, key_id, pseudonym, request_id_for, stable_id


def test_stable_id() -> None:
    digest = hashlib.sha256(b"a\x1f1\x1fb").hexdigest()[:24]
    assert stable_id("rq", "a", 1, "b") == f"rq_{digest}"
    assert stable_id("x") == "x_" + hashlib.sha256(b"").hexdigest()[:24]
    assert stable_id("rq", "a", "b") != stable_id("rq", "ab")
    assert stable_id("rq", "\ud800") == stable_id("rq", "\ud800")  # lone surrogates never crash


def test_hmac_and_pseudonym() -> None:
    key = b"k" * 32
    expected = hmac.new(key, b"alice", hashlib.sha256).hexdigest()
    assert hmac_hex(key, b"alice") == expected[:20]
    assert hmac_hex(key, b"alice", n=32) == expected[:32]
    p = pseudonym(key, "p", "alice")
    assert re.fullmatch(r"p_[0-9a-f]{20}", p) and p == "p_" + expected[:20]
    assert pseudonym(key, "h", "mcp-server") != pseudonym(b"other", "h", "mcp-server")


def test_key_id() -> None:
    assert key_id(b"secret") == "k_" + hashlib.sha256(b"tokenbill-key-id\0secret").hexdigest()[:12]
    assert re.fullmatch(r"k_[0-9a-f]{12}", key_id(b""))


def test_request_id_for() -> None:
    by_message = request_id_for("anthropic", "msg_01", "s_a", "line:1")
    assert by_message == stable_id("rq", "anthropic", "msg_01")
    assert by_message == request_id_for("anthropic", "msg_01", "s_other", "line:99")  # idempotent
    assert request_id_for("anthropic", None, "s_a", "line:7") == stable_id("rq", "s_a", "line:7")
    assert request_id_for("anthropic", "", "s_a", "line:7") == stable_id("rq", "s_a", "line:7")


def test_is_opaque_ref() -> None:
    for ok in ("emp-42", "a", "A.b_c-9", "x" * 64):
        assert is_opaque_ref(ok)
    for bad in ("", "x" * 65, "alice@example.com", "a b", "é", "a/b", None, 5):
        assert not is_opaque_ref(bad)  # type: ignore[arg-type]


@given(st.text(max_size=80))
@settings(max_examples=200, deadline=None)
def test_opaque_refs_never_contain_at(value: str) -> None:
    if is_opaque_ref(value):
        assert "@" not in value and 1 <= len(value) <= 64
