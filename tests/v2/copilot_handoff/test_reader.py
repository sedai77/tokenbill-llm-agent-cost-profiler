"""Reader limits (brief Build 5): zip-bomb and traversal limits, exact member names, manifest
schema and counts, record validators."""

from __future__ import annotations

import json
import secrets
import stat
import struct
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from tokenbill.copilot import handoff
from tokenbill.copilot.handoff import MANIFEST_KEYS, read_bundle
from tokenbill.core.errors import ContractViolation, SourceError

from .helpers import write_round_trip

Members = list[tuple[zipfile.ZipInfo, bytes]]


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    out = tmp_path / "good.tbx"
    write_round_trip(out)
    return out


def members_of(path: Path) -> Members:
    with zipfile.ZipFile(path) as zf:
        return [(info, zf.read(info)) for info in zf.infolist()]


def rebuild(src: Path, dst: Path, mutate: Callable[[Members], Members]) -> Path:
    items = mutate(members_of(src))
    with zipfile.ZipFile(dst, "w") as zf:
        for info, data in items:
            zf.writestr(info, data, compress_type=info.compress_type)
    return dst


def info(name: str, **kw: object) -> zipfile.ZipInfo:
    zi = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    zi.compress_type = zipfile.ZIP_DEFLATED
    zi.external_attr = 0o100600 << 16
    for k, v in kw.items():
        setattr(zi, k, v)
    return zi


def edit(name: str, fn: Callable[[bytes], bytes]) -> Callable[[Members], Members]:
    def mutate(items: Members) -> Members:
        return [(i, fn(d) if i.filename == name else d) for i, d in items]
    return mutate


def edit_manifest(fn: Callable[[dict], None]) -> Callable[[Members], Members]:
    def change(data: bytes) -> bytes:
        doc = json.loads(data)
        fn(doc)
        return json.dumps(doc).encode()
    return edit("manifest.json", change)


def _central(data: bytes, name: str) -> int:
    needle = b"PK\x01\x02"
    pos = data.find(needle)
    while pos >= 0:
        n = struct.unpack("<H", data[pos + 28:pos + 30])[0]
        if data[pos + 46:pos + 46 + n] == name.encode():
            return pos
        pos = data.find(needle, pos + 4)
    raise AssertionError("member not found")


def forge_central_size(data: bytes, name: str, size: int) -> bytes:
    """Rewrite the uncompressed size of *name* in the central directory (a lying header)."""
    pos = _central(data, name)
    return data[:pos + 24] + struct.pack("<I", size) + data[pos + 28:]


def forge_central_flags(data: bytes, name: str, flags: int) -> bytes:
    """Rewrite the general-purpose flags of *name* in the central directory."""
    pos = _central(data, name)
    return data[:pos + 8] + struct.pack("<H", flags) + data[pos + 10:]


def test_good_bundle_reads(bundle: Path) -> None:
    manifest, result = read_bundle(bundle)
    assert manifest.count("licenses") == len(result.licenses) == 5


@pytest.mark.parametrize(("mutate", "message"), [
    (lambda items: [*items, (info("../x"), b"")], "unsafe member path"),
    (lambda items: [*items, (info("/abs.jsonl"), b"")], "unsafe member path"),
    (lambda items: [*items, (info("records\\x.jsonl"), b"")], "unsafe member path"),
    (lambda items: [*items, (info("records/"), b"")], "directory member"),
    (lambda items: [*items, (info("records/notes.jsonl"), b"")], "unexpected member name"),
    (lambda items: [*items, (info("records/outcomes.jsonl"), b"")], "duplicate member"),
    (lambda items: items[1:] + items[:1], "manifest.json must be the first member"),
    (lambda items: [(info(i.filename, external_attr=(stat.S_IFLNK | 0o777) << 16), d)
                    if i.filename == "records/config.jsonl" else (i, d) for i, d in items],
     "symbolic-link member"),
    (lambda items: [(info(i.filename, compress_type=zipfile.ZIP_BZIP2), d)
                    if i.filename == "records/config.jsonl" else (i, d) for i, d in items],
     "unsupported compression"),
    (lambda items: items + [(info(f"records/x{i}.jsonl"), b"") for i in range(10)],
     "more than 16 members"),
    (lambda items: [(i, b"\0" * (4 * 2**20)) if i.filename == "records/cost_lines.jsonl"
                    else (i, d) for i, d in items], "ratio above 200:1"),
    (edit("manifest.json", lambda d: json.dumps({**json.loads(d), "tool_version":
                                                 secrets.token_hex(600_000)}).encode()),
     "manifest larger than 1 MiB"),
    (edit_manifest(lambda m: m.update(schema="tokenbill/copilot-export@2")), "schema"),
    (edit_manifest(lambda m: m["counts"].update(licenses=4)), "licenses count differs"),
    (edit_manifest(lambda m: m.pop("leak_scan")), "keys differ"),
    (edit("manifest.json", lambda d: b"{not json"), "manifest: not JSON"),
    (edit("records/config.jsonl", lambda d: d + b"{broken\n"), "line 4: not JSON"),
    (edit("records/config.jsonl", lambda d: d + b"[1]\n"), "line 4: not an object"),
    (edit("records/config.jsonl", lambda d: d.rstrip(b"\n")), "does not end with a newline"),
    (lambda items: [(i, d) for i, d in items if i.filename != "records/activity.jsonl"],
     "activity count differs"),
])
def test_rejected(bundle: Path, tmp_path: Path, mutate: Callable[[Members], Members],
                  message: str) -> None:
    bad = rebuild(bundle, tmp_path / "bad.tbx", mutate)
    with pytest.raises(SourceError) as err:
        read_bundle(bad)
    assert message in str(err.value)


def test_forged_2000_to_1_member(bundle: Path, tmp_path: Path) -> None:
    data = bundle.read_bytes()
    with zipfile.ZipFile(bundle) as zf:
        member = zf.getinfo("records/cost_lines.jsonl")
    forged = forge_central_size(data, member.filename, member.compress_size * 2000)
    bad = tmp_path / "forged.tbx"
    bad.write_bytes(forged)
    with pytest.raises(SourceError, match="ratio above 200:1"):
        read_bundle(bad)


def test_encrypted_flag(bundle: Path, tmp_path: Path) -> None:
    bad = tmp_path / "encrypted.tbx"
    bad.write_bytes(forge_central_flags(bundle.read_bytes(), "records/config.jsonl", 0x1))
    with pytest.raises(SourceError, match="encrypted member"):
        read_bundle(bad)


def test_non_p_principal_is_a_contract_violation(bundle: Path, tmp_path: Path) -> None:
    def raw_login(data: bytes) -> bytes:
        first, _, rest = data.partition(b"\n")
        doc = json.loads(first)
        doc["principal"] = "octocat"
        return json.dumps(doc).encode() + b"\n" + rest
    bad = rebuild(bundle, tmp_path / "bad.tbx", edit("records/licenses.jsonl", raw_login))
    with pytest.raises(ContractViolation):
        read_bundle(bad)


def test_non_h_name_dim_is_rejected(bundle: Path, tmp_path: Path) -> None:
    def clear_repo(data: bytes) -> bytes:
        lines = data.decode().splitlines()
        doc = json.loads(lines[0])
        doc["dims"] = sorted([*doc["dims"], ["repo", "payments-api"]])
        lines[0] = json.dumps(doc)
        return ("\n".join(lines) + "\n").encode()
    bad = rebuild(bundle, tmp_path / "bad.tbx", edit("records/aggregates.jsonl", clear_repo))
    with pytest.raises(SourceError, match="h_ pseudonyms"):
        read_bundle(bad)


def test_per_person_rows_in_an_aggregate_only_bundle(bundle: Path, tmp_path: Path) -> None:
    bad = rebuild(bundle, tmp_path / "bad.tbx",
                  edit_manifest(lambda m: m.update(privacy_mode="aggregate_only")))
    with pytest.raises(SourceError, match="per-person"):
        read_bundle(bad)


def test_content_recheck_on_read(bundle: Path, tmp_path: Path) -> None:
    def plant(data: bytes) -> bytes:
        doc = json.loads(data.splitlines()[0])
        doc["attrs"] = sorted([*doc["attrs"], ["sku", "billing@example.com"]])
        return json.dumps(doc).encode() + b"\n" + b"\n".join(data.splitlines()[1:]) + b"\n"
    bad = rebuild(bundle, tmp_path / "bad.tbx", edit("records/config.jsonl", plant))
    with pytest.raises(SourceError, match="content re-check"):
        read_bundle(bad)


def test_corrupt_member_data(bundle: Path, tmp_path: Path) -> None:
    data = bytearray(bundle.read_bytes())
    with zipfile.ZipFile(bundle) as zf:
        member = zf.getinfo("records/cost_lines.jsonl")
    start = member.header_offset + 30 + len(member.filename)
    data[start + 5] ^= 0xFF
    data[start + 6] ^= 0xFF
    bad = tmp_path / "corrupt.tbx"
    bad.write_bytes(bytes(data))
    with pytest.raises(SourceError, match="unreadable|size mismatch|not JSON"):
        read_bundle(bad)


def test_not_a_zip_and_empty_zip(tmp_path: Path) -> None:
    junk = tmp_path / "junk.tbx"
    junk.write_bytes(b"PK\x03\x04 not really")
    with pytest.raises(SourceError, match="not a readable zip"):
        read_bundle(junk)
    empty = tmp_path / "empty.tbx"
    with zipfile.ZipFile(empty, "w"):
        pass
    with pytest.raises(SourceError, match="empty archive"):
        read_bundle(empty)


def test_size_limits(bundle: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(handoff, "_MAX_MEMBER_BYTES", 10)
    with pytest.raises(SourceError, match="larger than 512 MiB"):
        read_bundle(bundle)
    monkeypatch.setattr(handoff, "_MAX_MEMBER_BYTES", 512 * 2**20)
    monkeypatch.setattr(handoff, "_MAX_TOTAL_BYTES", 2000)
    with pytest.raises(SourceError, match="2 GiB in total"):
        read_bundle(bundle)


@pytest.mark.parametrize(("key", "value"), [
    ("window", {"since": "yesterday", "until": None}),
    ("counts", {"licenses": 5}),
    ("principal_key_id", "k_short"),
    ("key_rotated", 1),
    ("privacy_mode", "open"),
    ("leak_scan", {"terms": 3, "result": "dirty"}),
    ("sources", [{"adapter": "x", "files": -1, "records": 0, "quarantined": 0}]),
    ("sources", ["x"]),
    ("dq", {"code": "x"}),
    ("plan_evidence", [{"entity_id": "enterprise", "month": "2026-09", "plan": "business",
                        "source": "seats_api", "conflict": "no"}]),
    ("tool_version", ""),
    ("created_ms", -5),
    ("k", 0),
    ("entities", "enterprise"),
    ("orgs", [1]),
])
def test_manifest_field_validation(bundle: Path, tmp_path: Path, key: str, value: object) -> None:
    bad = rebuild(bundle, tmp_path / "bad.tbx", edit_manifest(lambda m: m.update({key: value})))
    with pytest.raises(SourceError, match=f"manifest: {key}"):
        read_bundle(bad)


def test_manifest_keys_constant() -> None:
    assert len(MANIFEST_KEYS) == len(set(MANIFEST_KEYS)) == 19


def test_missing_member_with_zero_count_is_accepted(bundle: Path, tmp_path: Path) -> None:
    def drop_outcomes(items: Members) -> Members:
        kept = [(i, d) for i, d in items if i.filename != "records/outcomes.jsonl"]
        out = []
        for i, d in kept:
            if i.filename == "manifest.json":
                doc = json.loads(d)
                doc["counts"]["outcomes"] = 0
                d = json.dumps(doc).encode()
            out.append((i, d))
        return out
    ok = rebuild(bundle, tmp_path / "ok.tbx", drop_outcomes)
    manifest, result = read_bundle(ok)
    assert manifest.count("outcomes") == 0 and result.outcomes == []
