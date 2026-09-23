"""SPEC §3.10 textsafe.sanitize and secrets.find_secrets / redact."""

from __future__ import annotations

import hashlib
import re
from collections import Counter

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.core.secrets import SECRET_TYPES, find_secrets, redact, shannon_entropy
from tokenbill.core.textsafe import sanitize

_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


# ---------- sanitize ----------


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("plain text", "plain text"),
        ("keep\nnew\tlines", "keep\nnew\tlines"),
        ("\x1b[31mred\x1b[0m", "red"),
        ("\x1b[1;32;40mbold\x1b[m", "bold"),
        ("\x1b]0;window title\x07after", "after"),
        ("\x1b]8;;https://x.test\x1b\\link\x1b]8;;\x1b\\", "link"),
        ("\x9b31mc1-csi", "c1-csi"),
        ("a\x00b\x07c\x08d\x1fe", "abcde"),
        ("bell\r\nline", "bell\nline"),
        ("del\x7fchar", "delchar"),
        ("c1\x85\x9fend", "c1end"),
        ("\x1bPdcs payload\x1b\\ok", "ok"),
        ("\x1b(Bcharset", "charset"),
        ("bidi\u202eevil\u202c", "bidievil"),
        ("isolate\u2066x\u2069", "isolatex"),
        ("unicode ok: héllo 日本", "unicode ok: héllo 日本"),
    ],
)
def test_sanitize_strips_controls_and_ansi(raw: str, clean: str) -> None:
    assert sanitize(raw) == clean


def test_sanitize_truncates() -> None:
    assert sanitize("abcdef", 4) == "abc…"
    assert sanitize("abc", 3) == "abc"
    assert sanitize("abc", 1) == "…"
    assert sanitize("abc", 0) == ""
    assert sanitize("\x1b[31mabcdef\x1b[0m", 4) == "abc…"
    assert sanitize(42) == "42"  # type: ignore[arg-type]


@given(st.text(), st.none() | st.integers(0, 50))
@settings(max_examples=300, deadline=None)
def test_sanitize_properties(text: str, limit: int | None) -> None:
    out = sanitize(text, limit)
    assert not _CONTROL.search(out)
    assert "\x1b" not in out
    if limit is not None:
        assert len(out) <= limit
    assert sanitize(out) == out  # idempotent


# ---------- secrets ----------

SAMPLES = {
    "anthropic_key": "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "openai_key": "sk-proj-AbCdEfGhIjKlMnOpQrStUv1234",
    "aws_access_key": "AKIAIOSFODNN7EXAMPLE",
    "github_token": "ghp_0123456789abcdefghijABCDEFGHIJ012345",
    "slack_token": "xoxb-1234567890-abcdefghij",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
    "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    "pem_private_key": "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIBOgIBAAJBAKj34GkxFhD90vcNLYLInFEX6Ppy1tPf\n-----END RSA PRIVATE KEY-----",
    "high_entropy": "Zq8Lr2Vx9Ty4Wm7Ns1Pk5Hd3Fg6Jc0Bv8Qa2Xe",
}


@pytest.mark.parametrize("kind", sorted(SAMPLES))
def test_every_secret_type_is_detected(kind: str) -> None:
    secret = SAMPLES[kind]
    text = f"prefix {secret} suffix"
    found = find_secrets(text)
    assert [(t, text[s:e]) for t, s, e in found] == [(kind, secret)]
    red, counts = redact(text)
    assert red == f"prefix [REDACTED:{kind}] suffix"
    assert counts == Counter({kind: 1})
    assert kind in SECRET_TYPES


def test_github_pat_and_pem_header_only() -> None:
    pat = "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz"
    assert [t for t, _, _ in find_secrets(pat)] == ["github_token"]
    header = "-----BEGIN PRIVATE KEY-----"
    assert find_secrets(f"x {header} y") == [("pem_private_key", 2, 2 + len(header))]


def test_no_false_positives_on_ordinary_content() -> None:
    ordinary = [
        "The quick brown fox jumps over the lazy dog.",
        "request id 550e8400-e29b-41d4-a716-446655440000",
        "sha256 " + hashlib.sha256(b"tokenbill").hexdigest(),
        "sk-short",
        "AKIA-not-a-key",
        "h_0123456789abcdef0123",
        "/usr/local/lib/python3.12/site-packages/tokenbill/core/records.py",
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    ]
    for text in ordinary:
        assert find_secrets(text) == [], text


def test_multiple_and_overlapping_secrets() -> None:
    text = f"{SAMPLES['openai_key']} and {SAMPLES['aws_access_key']} and {SAMPLES['anthropic_key']}"
    kinds = [t for t, _, _ in find_secrets(text)]
    assert kinds == ["openai_key", "aws_access_key", "anthropic_key"]
    red, counts = redact(text)
    assert counts == Counter({"openai_key": 1, "aws_access_key": 1, "anthropic_key": 1})
    assert "sk-" not in red and "AKIA" not in red
    assert redact("nothing here") == ("nothing here", Counter())


def test_shannon_entropy() -> None:
    assert shannon_entropy("") == 0.0
    assert shannon_entropy("aaaa") == 0.0
    assert shannon_entropy("ab") == 1.0
    assert shannon_entropy("0123456789abcdef") == 4.0


@given(st.text(max_size=300))
@settings(max_examples=200, deadline=None)
def test_redact_properties(text: str) -> None:
    found = find_secrets(text)
    for (_, _s1, e1), (_, s2, _e2) in zip(found, found[1:], strict=False):
        assert e1 <= s2  # sorted, non-overlapping
    red, counts = redact(text)
    assert sum(counts.values()) == len(found)
    for _, s, e in found:
        assert text[s:e] not in red or not text[s:e].strip()
