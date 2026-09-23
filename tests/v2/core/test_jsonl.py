"""SPEC §3.9 jsonl: streaming lines, compression, oversize lines, JSON parsing, private files."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import stat
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tokenbill.core import jsonl
from tokenbill.core.errors import SourceError
from tokenbill.core.jsonl import (
    ACL_WARNING,
    MAX_LINE_BYTES,
    ZSTD_MESSAGE,
    acl_warning,
    head_sha,
    iter_lines,
    open_private,
    open_text,
    parse_json_line,
    write_jsonl,
)

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX modes are no-ops on Windows (D44)")


def test_constants() -> None:
    assert MAX_LINE_BYTES == 16 * 2**20
    assert (
        ZSTD_MESSAGE == "zstd needs Python 3.14; set the collector fileexporter compression to none"
    )


def test_head_sha(tmp_path: Path) -> None:
    p = tmp_path / "f.jsonl"
    p.write_bytes(b"a" * 5000)
    assert head_sha(p) == hashlib.sha256(b"a" * 4096).hexdigest()
    p.write_bytes(b"a" * 4096 + b"different tail")
    assert head_sha(p) == hashlib.sha256(b"a" * 4096).hexdigest()
    with pytest.raises(SourceError):
        head_sha(tmp_path / "missing")


def test_open_text_plain_gz_and_missing(tmp_path: Path) -> None:
    plain = tmp_path / "a.jsonl"
    plain.write_bytes(b'{"a":1}\n')
    with open_text(plain) as f:
        assert f.read() == b'{"a":1}\n'
    gz = tmp_path / "a.jsonl.GZ"
    gz.write_bytes(gzip.compress(b'{"a":2}\n'))
    with open_text(gz) as f:
        assert f.read() == b'{"a":2}\n'
    with pytest.raises(SourceError) as exc:
        open_text(tmp_path / "missing.jsonl")
    assert "missing.jsonl" in str(exc.value) and str(tmp_path) not in str(exc.value)


def test_zstd_needs_python_314(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jsonl, "_zstd_module", lambda: None)
    p = tmp_path / "otel.jsonl.zst"
    p.write_bytes(b"\x28\xb5\x2f\xfd")
    with pytest.raises(SourceError) as exc:
        open_text(p)
    assert ZSTD_MESSAGE in str(exc.value)


@pytest.mark.skipif(sys.version_info >= (3, 14), reason="compression.zstd exists on 3.14+")
def test_zstd_error_message_on_this_python(tmp_path: Path) -> None:
    p = tmp_path / "x.zst"
    p.write_bytes(b"\x28\xb5\x2f\xfd")
    with pytest.raises(SourceError, match="zstd needs Python 3.14"):
        list(iter_lines(p))


def test_iter_lines_offsets_blank_lines_and_crlf(tmp_path: Path) -> None:
    p = tmp_path / "f.jsonl"
    p.write_bytes(b'{"a":1}\n\n  \r\n{"b":2}\r\n{"c":3}')
    out = list(iter_lines(p))
    assert out == [(1, 0, b'{"a":1}'), (4, 13, b'{"b":2}'), (5, 22, b'{"c":3}')]
    tail = list(iter_lines(p, start_offset=13))
    assert tail == [(1, 13, b'{"b":2}'), (2, 22, b'{"c":3}')]
    assert list(iter_lines(p, start_offset=10**6)) == []
    with pytest.raises(ValueError):
        list(iter_lines(p, start_offset=-1))


def test_iter_lines_gzip(tmp_path: Path) -> None:
    p = tmp_path / "f.jsonl.gz"
    p.write_bytes(gzip.compress(b'{"a":1}\n{"b":2}\n'))
    assert [line for _, _, line in iter_lines(p)] == [b'{"a":1}', b'{"b":2}']
    with pytest.raises(ValueError):
        list(iter_lines(p, start_offset=3))
    corrupt = tmp_path / "bad.jsonl.gz"
    payload = b"".join(
        b'{"n":%d,"h":"%s"}\n' % (i, hashlib.sha256(bytes([i % 256])).hexdigest().encode())
        for i in range(2000)
    )
    blob = gzip.compress(payload)
    corrupt.write_bytes(blob[: len(blob) // 2])
    with pytest.raises(SourceError):
        list(iter_lines(corrupt))
    not_gzip = tmp_path / "plain.gz"
    not_gzip.write_bytes(b"not gzip at all")
    with pytest.raises(SourceError):
        list(iter_lines(not_gzip))


def test_oversize_line_is_yielded_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jsonl, "MAX_LINE_BYTES", 10)
    monkeypatch.setattr(jsonl, "_CHUNK", 4)
    p = tmp_path / "f.jsonl"
    p.write_bytes(b'{"a":1}\n' + b"x" * 50 + b"\n" + b'{"b":2}\n' + b"y" * 25)
    out = list(iter_lines(p))
    assert out == [(1, 0, b'{"a":1}'), (2, 8, b""), (3, 59, b'{"b":2}'), (4, 67, b"")]
    exact = tmp_path / "exact.jsonl"
    exact.write_bytes(b"z" * 10 + b"\n")
    assert list(iter_lines(exact)) == [(1, 0, b"z" * 10)]


def test_parse_json_line() -> None:
    assert parse_json_line(b'{"a": 1}') == {"a": 1}
    assert parse_json_line('\ufeff{"a": 1}'.encode()) == {"a": 1}
    assert parse_json_line(b'{"e": "\\ud83d\\ude00"}') == {"e": "\U0001f600"}
    for bad in (
        b"",
        b"[1, 2]",
        b'"s"',
        b"1",
        b"{",
        b'{"a": NaN}',
        b'{"a": Infinity}',
        b'{"a": -Infinity}',
        b"\xff\xfe",
        b'{"a": "\\ud800"}',
        b'{"\\udfff": 1}',
        b'{"a": ["\\uDBFF"]}',
        b"[" * 100000 + b"]" * 100000,
        b'{"a":' * 100000,
    ):
        assert parse_json_line(bad) is None
    assert parse_json_line(b'{"a": 1.5}') == {
        "a": 1.5
    }  # floats parse; validators reject them later


@given(st.binary(max_size=200))
@settings(max_examples=300, deadline=None)
def test_parse_json_line_fuzz_never_raises(raw: bytes) -> None:
    result = parse_json_line(raw)
    assert result is None or isinstance(result, dict)


@given(
    st.recursive(
        st.none() | st.booleans() | st.integers() | st.text(),
        lambda c: st.lists(c, max_size=3) | st.dictionaries(st.text(max_size=4), c, max_size=3),
        max_leaves=10,
    )
)
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
def test_parse_json_line_round_trips_objects(value: object) -> None:
    doc = {"v": value}
    raw = json.dumps(doc).encode()
    parsed = parse_json_line(raw)
    has_lone = (
        "\\ud8" in raw.decode().lower()
        or "\\udc" in raw.decode().lower()
        or any(f"\\ud{c}" in raw.decode().lower() for c in "9abcdef")
    )
    if not has_lone:
        assert parsed == doc


@given(st.lists(st.binary(max_size=40), max_size=10))
@settings(
    max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_iter_lines_fuzz(tmp_path: Path, chunks: list[bytes]) -> None:
    p = tmp_path / "fuzz.jsonl"
    p.write_bytes(b"\n".join(chunks))
    offsets = []
    for line_no, offset, line in iter_lines(p):
        assert line_no >= 1 and line.strip() and b"\n" not in line
        offsets.append(offset)
        assert p.read_bytes()[offset : offset + len(line)] == line
    assert offsets == sorted(offsets)


@posix_only
def test_open_private_modes(tmp_path: Path) -> None:
    target = tmp_path / "new" / "deeper" / "out.jsonl"
    with open_private(target) as f:
        f.write("x\n")
        assert acl_warning(f) is None
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "new").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "new" / "deeper").stat().st_mode) == 0o700
    target.chmod(0o644)
    with open_private(target, "a") as f:
        f.write("y\n")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text() == "x\ny\n"
    with open_private(target, "wb") as f:
        f.write(b"\x00")
    assert target.read_bytes() == b"\x00"
    with pytest.raises(FileExistsError):
        open_private(target, "x")
    with pytest.raises(ValueError):
        open_private(target, "r")
    old_umask = os.umask(0o077)
    try:
        with open_private(tmp_path / "umask" / "u.txt") as f:
            f.write("z")
    finally:
        os.umask(old_umask)
    assert stat.S_IMODE((tmp_path / "umask").stat().st_mode) == 0o700


def test_open_private_windows_acl_via_fake_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Sequence[str]] = []

    def ok_runner(argv: Sequence[str]) -> int:
        calls.append(list(argv))
        return 0

    monkeypatch.setenv("USERNAME", "tester")
    target = tmp_path / "win" / "collector.jsonl"
    with open_private(target, "w", runner=ok_runner, platform="nt") as f:
        f.write("{}\n")
        assert acl_warning(f) is None
    assert calls == [["icacls", str(target), "/inheritance:r", "/grant:r", "tester:F"]]

    with open_private(target, "w", runner=lambda argv: 5, platform="nt") as f:
        assert acl_warning(f) == ACL_WARNING == "dq.windows_acl_not_enforced"

    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    with open_private(target, "w", runner=ok_runner, platform="nt") as f:
        assert acl_warning(f) == ACL_WARNING


def test_default_runner_handles_missing_icacls(monkeypatch: pytest.MonkeyPatch) -> None:
    assert jsonl._default_runner(["tokenbill-definitely-not-a-command-xyz"]) == -1
    assert jsonl._default_runner([sys.executable, "-c", "raise SystemExit(3)"]) == 3


def test_write_jsonl_canonical_private_and_gzip(tmp_path: Path) -> None:
    records = [{"b": 1, "a": "é"}, {"z": [1, {"y": None}]}]
    p = tmp_path / "out" / "t.jsonl"
    write_jsonl(p, records)
    assert p.read_bytes() == '{"a":"é","b":1}\n{"z":[1,{"y":null}]}\n'.encode()
    if os.name != "nt":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
    g1, g2 = tmp_path / "a.jsonl.gz", tmp_path / "b.jsonl.gz"
    write_jsonl(g1, records)
    write_jsonl(g2, records)
    assert g1.read_bytes() == g2.read_bytes()  # deterministic: no name, mtime 0
    assert gzip.decompress(g1.read_bytes()) == p.read_bytes()
    assert [parse_json_line(line) for _, _, line in iter_lines(g1)] == records
    with pytest.raises(ValueError):
        write_jsonl(tmp_path / "nan.jsonl", [{"a": float("nan")}])
