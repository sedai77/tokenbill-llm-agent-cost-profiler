"""Identity modes and name pseudonymization (SPEC §5.1, §5.4, D5, D39)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tokenbill.adapters.claude_code import NameHasher, mcp_server_of, resolve_identity, token
from tokenbill.core.errors import UsageError
from tokenbill.core.ids import key_id, pseudonym
from tokenbill.core.testing import MemoryStore

from .helpers import CC, HEADLESS, NAME_KEY, PRINCIPAL_KEY, bf, by_message, opts, read

OPUS = "claude-opus-5-5"


def _tx() -> object:
    t = bf.Tx("22222222-0000-4000-8000-000000000002")
    t.human("go")
    t.call("msg_i1", OPUS, inp=5, outputs=(9,), stop="end_turn",
           line_extra={"attributionMcpServer": "github", "attributionSkill": "pdf",
                       "attributionPlugin": "acme-plugin"})
    return t


def test_central_mode_emits_r_refs(tmp_path: Path) -> None:
    r = read(tmp_path, _tx(), identity_mode="central", principal_ref="dev-1042",
             principal_key=None, principal_key_id=None)
    assert {q.attribution.principal for q in r.requests} == {"r_dev-1042"}
    assert r.source.principal_key_id is None
    store = MemoryStore(org_key=bytes(range(64, 96)), name_key_id=r.source.name_key_id)
    store.ingest(r)
    [req] = list(store.iter_requests())
    assert req.attribution.principal == pseudonym(bytes(range(64, 96)), "p", "dev-1042")


def test_two_stage_mode_emits_c_hmacs(tmp_path: Path) -> None:
    r = read(tmp_path, _tx(), identity_mode="two-stage", principal_ref="dev-1042")
    assert {q.attribution.principal for q in r.requests} == {
        pseudonym(PRINCIPAL_KEY, "c", "dev-1042")}
    assert r.source.principal_key_id == key_id(PRINCIPAL_KEY)


def test_install_mode_emits_p_hmacs(tmp_path: Path) -> None:
    r = read(tmp_path, _tx(), identity_mode="install", principal_ref="me")
    assert {q.attribution.principal for q in r.requests} == {pseudonym(PRINCIPAL_KEY, "p", "me")}
    assert r.source.principal_key_id == key_id(PRINCIPAL_KEY)


def test_no_ref_means_no_principal(tmp_path: Path) -> None:
    r = read(tmp_path, _tx())
    assert {q.attribution.principal for q in r.requests} == {None}
    assert r.source.principal_key_id is None


@pytest.mark.parametrize("ref", ["jane@example.com", "has space", "", "x" * 65, "ü"])
@pytest.mark.parametrize("mode", ["central", "two-stage", "install"])
def test_non_opaque_refs_are_rejected(tmp_path: Path, mode: str, ref: str) -> None:
    with pytest.raises(UsageError):
        read(tmp_path, _tx(), identity_mode=mode, principal_ref=ref)


def test_unknown_mode_and_missing_key_are_usage_errors(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        read(tmp_path, _tx(), identity_mode="telepathy", principal_ref="a")
    with pytest.raises(UsageError):
        read(tmp_path, _tx(), identity_mode="two-stage", principal_ref="a", principal_key=None)
    assert resolve_identity(opts(identity_mode="central", principal_ref="a")).principal == "r_a"


def test_names_are_hmacd_unless_allowlisted(tmp_path: Path) -> None:
    [req] = read(tmp_path, _tx()).requests
    a = req.attribution
    assert a.mcp_server == pseudonym(NAME_KEY, "h", "github")
    assert a.skill == pseudonym(NAME_KEY, "h", "pdf")
    assert a.plugin == pseudonym(NAME_KEY, "h", "acme-plugin")
    [req] = read(tmp_path, _tx(), name_allowlist=frozenset({"github", "pdf"})).requests
    assert (req.attribution.mcp_server, req.attribution.skill) == ("github", "pdf")
    assert req.attribution.plugin.startswith("h_")


def test_without_a_name_key_hashed_fields_are_dropped(tmp_path: Path) -> None:
    r = read(tmp_path, _tx(), name_key=b"", name_key_id="")
    [req] = r.requests
    assert req.attribution.mcp_server is None and req.attribution.cwd_key is None
    assert r.source.name_key_id is None and r.source.name_hmac == ""


def test_same_mcp_name_same_h_in_both_adapters(tmp_path: Path) -> None:
    [cc_req] = read(tmp_path, _tx()).requests
    stream = tmp_path / "stream.jsonl"
    stream.write_text(bf.headless_stream().jsonl(), encoding="utf-8")
    hr = HEADLESS.read(stream, opts())
    servers = {q.attribution.mcp_server for q in hr.requests} - {None}
    assert servers == {cc_req.attribution.mcp_server} == {pseudonym(NAME_KEY, "h", "github")}
    assert hr.source.name_key_id == key_id(NAME_KEY)


def test_cwd_is_only_a_pseudonym(tmp_path: Path) -> None:
    t = _tx()
    [req] = read(tmp_path, t).requests
    assert req.attribution.cwd_key == pseudonym(NAME_KEY, "h", t.cwd)
    reqs = by_message(CC.read(bf.alpha_main().write(tmp_path / "a.jsonl"), opts()))
    assert {q.attribution.cwd_key for q in reqs.values()} == {
        pseudonym(NAME_KEY, "h", bf.Tx("x").cwd)}


def test_name_hasher_and_helpers() -> None:
    h = NameHasher(opts())
    assert h.tool("Bash") == "Bash" and h.tool("mcp__x__y").startswith("h_")
    assert h.name(None) is None and h.name("") is None
    assert h.name("x" * 100).startswith("h_")          # long names are never clear
    assert h.name("x" * 100) == h.name("x" * 100)       # cached
    allow = NameHasher(opts(name_allowlist=frozenset({"ok-name", "bad name"})))
    assert allow.name("ok-name") == "ok-name"
    assert allow.name("bad name").startswith("h_")     # not token-shaped: hashed anyway
    assert mcp_server_of("mcp__github__list") == "github"
    assert mcp_server_of("mcp__") is None and mcp_server_of("Bash") is None
    assert token("a b") is None and token("x" * 65) is None and token("claude-opus-5-5[1m]")
    assert token("claude", 3) is None and token(5) is None
