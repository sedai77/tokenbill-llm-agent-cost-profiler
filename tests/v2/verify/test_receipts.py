"""verify.receipts: canonical JSON, DSSE PAE, ssh-keygen signing and refusal rules (SPEC §13.6)."""

from __future__ import annotations

import ast
import base64
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tokenbill.core.builders import CANARY
from tokenbill.core.errors import GateFailed, UsageError
from tokenbill.core.labels import Basis, Calibration, Evidence, estimated
from tokenbill.core.money import nano_to_micro
from tokenbill.core.testing import MemoryStore
from tokenbill.verify import receipts as RC

from .helpers import sample_measurement

REPO = Path(__file__).resolve().parents[3]
PATCH = "9f" * 32


def _receipt(**kw):
    m = sample_measurement(**kw)
    return RC.build_receipt(m, lever_id=m.lever_id, patch_sha256=PATCH,
                            shapley_credit=estimated(987_654_321, Basis.LIST,
                                                     calibration=Calibration.CALIBRATED),
                            reconciliation_verdict="reconciled",
                            calibration=Calibration.CALIBRATED, tool_version="0.2.0",
                            created="2026-09-23T12:00:00Z")


class FakeRunner:
    """Records calls; writes a signature file on ``-Y sign``; returns *code*."""

    def __init__(self, code: int = 0, write_sig: bool = True, exc: Exception | None = None):
        self.code, self.write_sig, self.exc = code, write_sig, exc
        self.calls: list[tuple[list[str], bytes]] = []

    def __call__(self, args, **kw):
        assert isinstance(args, list) and "shell" not in kw
        self.calls.append((args, kw.get("input", b"")))
        if self.exc is not None:
            raise self.exc
        if args[1:3] == ["-Y", "sign"] and self.write_sig:
            Path(args[-1] + ".sig").write_text("-----BEGIN SSH SIGNATURE-----\nAAAA\n")
        return subprocess.CompletedProcess(args, self.code, b"", b"")


# ---------------------------------------------------------------------------------------------
# canonical bytes and PAE
# ---------------------------------------------------------------------------------------------


def test_canonical_bytes_match_jcs_for_the_integer_string_subset() -> None:
    obj = {"b": 1, "a": "x\u001f\"\\/é ", "c": {"z": [1, "y", -9_007_199_254_740_991]},
           "_type": "t"}
    expected = ('{"_type":"t","a":"x\\u001f\\"\\\\/é ","b":1,'
                '"c":{"z":[1,"y",-9007199254740991]}}').encode()
    assert RC.canonical_bytes(obj) == expected
    assert RC.canonical_bytes({"t": (1, 2)}) == b'{"t":[1,2]}'


@pytest.mark.parametrize("bad", [
    {"x": 1.5}, {"x": True}, {"x": None}, {"é": 1}, {1: 1}, {"x": 2**53},
    {"x": "\ud800"}, {"x": {"y": [1.0]}}, {"x": b"bytes"},
])
def test_canonical_bytes_refuse_anything_but_integers_and_strings(bad) -> None:
    with pytest.raises(UsageError):
        RC.canonical_bytes(bad)


def test_canonical_bytes_refuse_non_objects() -> None:
    with pytest.raises(UsageError):
        RC.canonical_bytes([1, 2])  # type: ignore[arg-type]


def test_canonical_bytes_identical_across_two_processes() -> None:
    """Acceptance: the receipt's canonical bytes are byte-identical in another process."""
    script = (
        "import sys, hashlib\n"
        "sys.path.insert(0, '.')\n"
        "from tests.v2.verify.test_receipts import _receipt\n"
        "from tokenbill.verify.receipts import canonical_bytes\n"
        "sys.stdout.write(canonical_bytes(_receipt()).hex())\n")
    here = RC.canonical_bytes(_receipt()).hex()
    for seed in ("1", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        out = subprocess.run([sys.executable, "-c", script], cwd=REPO, env=env,
                             capture_output=True, text=True, check=True)
        assert out.stdout == here


def test_pae_matches_the_dsse_specification_vector() -> None:
    assert RC.pae("http://example.com/HelloWorld", b"hello world") == \
        b"DSSEv1 29 http://example.com/HelloWorld 11 hello world"
    assert RC.pae("é", b"") == b"DSSEv1 2 \xc3\xa9 0 "
    with pytest.raises(UsageError):
        RC.pae(b"type", b"x")  # type: ignore[arg-type]
    with pytest.raises(UsageError):
        RC.pae("type", "x")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------------------------


def test_build_receipt_body() -> None:
    m = sample_measurement()
    r = _receipt()
    assert r["_type"] == RC.RECEIPT_TYPE
    assert r["subject"] == {"lever_id": m.lever_id, "patch_sha256": PATCH}
    p = r["predicate"]
    for key in ("label", "design", "metric", "scope_label", "window", "estimate_usd_micro",
                "ci_low_usd_micro", "ci_high_usd_micro", "projected_usd_micro",
                "realization_rate_milli", "shapley_credit_usd_micro", "guards", "adjustments",
                "rate_card_sha256", "assignment_log_sha256", "preregistration_sha256",
                "reconciliation_verdict", "calibration", "tool_version", "created"):
        assert key in p, key
    assert p["label"] == "verified" and p["basis"] == "list" and p["signable"] == "true"
    assert p["estimate_usd_micro"] == nano_to_micro(1_234_567_891) == 1_234_568
    assert p["ci_low_usd_micro"] == 1_000_000 and p["ci_high_usd_micro"] == 1_500_000
    assert p["projected_usd_micro"] == 1_519_468
    assert p["realization_rate_milli"] == 812          # 0.8125 → 812 (half-even)
    assert p["shapley_credit_usd_micro"] == 987_654
    assert p["window"] == {"since": "2026-06-01", "until": "2026-08-09"}
    assert p["guards"][0] == {"name": "srm", "passed": "true", "threshold": "p ≥ 0.001",
                              "value": "p=0.42"}
    assert p["metric"] == "cost per active developer-day"
    body = json.loads(RC.canonical_bytes(r))
    assert body == r


def test_build_receipt_omits_absent_optionals() -> None:
    m = sample_measurement(projected_calibration=None, rr=None)
    r = RC.build_receipt(m, lever_id="x", patch_sha256=PATCH, shapley_credit=None,
                         reconciliation_verdict="reconciled", calibration=Calibration.NA,
                         tool_version="0.2.0", created="2026-09-23")
    p = r["predicate"]
    for key in ("projected_usd_micro", "realization_rate_milli", "shapley_credit_usd_micro"):
        assert key not in p
    assert p["calibration"] == "n/a"


def test_build_receipt_validation() -> None:
    m = sample_measurement()
    base = {"lever_id": "x", "patch_sha256": PATCH, "shapley_credit": None,
            "reconciliation_verdict": "reconciled", "calibration": Calibration.NA,
            "tool_version": "0.2.0", "created": "2026-09-23"}
    for bad in ({"lever_id": ""}, {"patch_sha256": "abc"}, {"reconciliation_verdict": ""},
                {"created": "yesterday"}, {"tool_version": ""}):
        with pytest.raises(UsageError):
            RC.build_receipt(m, **{**base, **bad})
    with pytest.raises(UsageError):
        RC.build_receipt("m", **base)  # type: ignore[arg-type]
    unpriced = sample_measurement(evidence="estimated")
    unpriced = unpriced.__class__(**{**{f: getattr(unpriced, f) for f in unpriced.__slots__},
                                     "estimate": estimated(None, Basis.LIST, note="unpriced: x")})
    with pytest.raises(UsageError):
        RC.build_receipt(unpriced, **base)
    with pytest.raises(UsageError):
        RC.build_receipt(sample_measurement(rr="lots"), **base)
    with pytest.raises(UsageError):
        RC.build_receipt(sample_measurement(rr="Infinity"), **base)


# ---------------------------------------------------------------------------------------------
# refusal rules
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kw, why", [
    ({"evidence": "estimated"}, "label estimated"),
    ({"evidence": "exact"}, "label exact"),
    ({"basis": "list_equivalent"}, "allowance"),
    ({"projected_calibration": "uncalibrated"}, "projection not calibrated"),
    ({"signable": False}, "not signable"),
])
def test_sign_refuses_unsignable_receipts(kw, why, tmp_path: Path) -> None:
    runner = FakeRunner()
    key = tmp_path / "k"
    key.write_text("key")
    with pytest.raises(GateFailed) as exc:
        RC.sign(_receipt(**kw), key_path=key, runner=runner)
    assert why in str(exc.value)
    assert runner.calls == []


def test_refusal_rules_on_raw_receipts() -> None:
    r = _receipt()
    assert RC.refusal_reasons(r) == ()
    unrec = json.loads(json.dumps(r))
    unrec["predicate"]["reconciliation_verdict"] = "not_reconciled"
    assert any("reconciliation" in x for x in RC.refusal_reasons(unrec))
    no_basis = json.loads(json.dumps(r))
    del no_basis["predicate"]["basis"]
    assert "basis missing" in RC.refusal_reasons(no_basis)
    with pytest.raises(UsageError):
        RC.refusal_reasons({"_type": "other", "predicate": {}})


# ---------------------------------------------------------------------------------------------
# signing and verification
# ---------------------------------------------------------------------------------------------


@pytest.fixture()
def ssh_key(tmp_path: Path) -> tuple[Path, Path]:
    key = tmp_path / "id_ed25519"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "tokenbill-test", "-f",
                    str(key)], check=True, capture_output=True)
    pub = key.with_name(key.name + ".pub").read_text().strip()
    signers = tmp_path / "allowed_signers"
    signers.write_text(f'finops@example.test namespaces="tokenbill-receipt" {pub}\n')
    return key, signers


@pytest.mark.needs_ssh_keygen
def test_sign_then_verify_with_a_generated_ed25519_key(ssh_key) -> None:
    """Acceptance: sign then verify succeeds; flipping one payload byte fails verification."""
    key, signers = ssh_key
    env = RC.sign(_receipt(), key_path=key)
    assert env["payloadType"] == RC.PAYLOAD_TYPE
    assert base64.b64decode(env["payload"]) == RC.canonical_bytes(_receipt())
    sig = env["signatures"][0]
    assert sig["keyid"].startswith("SHA256:")
    assert base64.b64decode(sig["sig"]).startswith(b"-----BEGIN SSH SIGNATURE-----")
    assert RC.verify_envelope(env, allowed_signers=signers, identity="finops@example.test")
    assert RC.envelope_receipt(env) == _receipt()
    body = bytearray(base64.b64decode(env["payload"]))
    body[len(body) // 2] ^= 0x01
    flipped = dict(env, payload=base64.b64encode(bytes(body)).decode())
    assert not RC.verify_envelope(flipped, allowed_signers=signers,
                                  identity="finops@example.test")
    assert not RC.verify_envelope(env, allowed_signers=signers, identity="someone@else.test")
    assert not RC.verify_envelope(dict(env, payloadType="application/json"),
                                  allowed_signers=signers, identity="finops@example.test")
    bad_sig = dict(env, signatures=[{"keyid": "", "sig": base64.b64encode(b"junk").decode()}])
    assert not RC.verify_envelope(bad_sig, allowed_signers=signers,
                                  identity="finops@example.test")


def test_sign_and_verify_call_ssh_keygen_without_a_shell(tmp_path: Path) -> None:
    key = tmp_path / "k"
    key.write_text("key")
    key.with_name("k.pub").write_text("ssh-ed25519 " + base64.b64encode(b"blob").decode())
    runner = FakeRunner()
    env = RC.sign(_receipt(), key_path=key, runner=runner)
    args, stdin = runner.calls[0]
    assert args[:3] == ["ssh-keygen", "-Y", "sign"]
    assert args[3:7] == ["-f", str(key), "-n", "tokenbill-receipt"]
    assert stdin == b""
    assert env["signatures"][0]["keyid"].startswith("SHA256:")
    signers = tmp_path / "allowed"
    signers.write_text("x")
    assert RC.verify_envelope(env, allowed_signers=signers, identity="id", runner=runner)
    args, stdin = runner.calls[1]
    assert args[:3] == ["ssh-keygen", "-Y", "verify"]
    assert args[3:9] == ["-f", str(signers), "-I", "id", "-n", "tokenbill-receipt"]
    assert args[9] == "-s"
    assert stdin == RC.pae(RC.PAYLOAD_TYPE, RC.canonical_bytes(_receipt()))
    assert not RC.verify_envelope(env, allowed_signers=signers, identity="id",
                                  runner=FakeRunner(code=1))


def test_signing_failures(tmp_path: Path) -> None:
    key = tmp_path / "k"
    key.write_text("key")
    with pytest.raises(RC.SigningError):
        RC.sign(_receipt(), key_path=key, runner=FakeRunner(code=255))
    with pytest.raises(RC.SigningError):
        RC.sign(_receipt(), key_path=key, runner=FakeRunner(write_sig=False))
    with pytest.raises(RC.SigningError):
        RC.sign(_receipt(), key_path=key,
                runner=FakeRunner(exc=subprocess.TimeoutExpired("ssh-keygen", 1)))
    with pytest.raises(UsageError):
        RC.sign(_receipt(), key_path=tmp_path / "missing", runner=FakeRunner())
    env = RC.sign(_receipt(), key_path=key, runner=FakeRunner())
    assert env["signatures"][0]["keyid"] == ""        # no .pub next to the key
    with pytest.raises(UsageError):
        RC.verify_envelope(env, allowed_signers=tmp_path / "none", identity="x",
                           runner=FakeRunner())
    (tmp_path / "allowed").write_text("x")
    with pytest.raises(UsageError):
        RC.verify_envelope(env, allowed_signers=tmp_path / "allowed", identity="",
                           runner=FakeRunner())


def test_missing_ssh_keygen_gives_a_clear_error(tmp_path: Path, monkeypatch) -> None:
    """Acceptance: without ssh-keygen, signing and verifying say so."""
    key = tmp_path / "k"
    key.write_text("key")
    (tmp_path / "allowed").write_text("x")
    monkeypatch.setattr(RC.shutil, "which", lambda name: None)
    with pytest.raises(RC.SshKeygenUnavailable) as exc:
        RC.sign(_receipt(), key_path=key)
    assert "ssh-keygen" in str(exc.value) and "OpenSSH" in str(exc.value)
    env = RC.sign(_receipt(), key_path=key, runner=FakeRunner())
    with pytest.raises(RC.SshKeygenUnavailable):
        RC.verify_envelope(env, allowed_signers=tmp_path / "allowed", identity="x")
    with pytest.raises(RC.SshKeygenUnavailable):
        RC.sign(_receipt(), key_path=key, runner=FakeRunner(exc=FileNotFoundError("ssh-keygen")))


def test_malformed_envelopes_do_not_verify(tmp_path: Path) -> None:
    signers = tmp_path / "allowed"
    signers.write_text("x")
    runner = FakeRunner()
    good = RC.sign(_receipt(), key_path=_key(tmp_path), runner=runner)
    for env in ("not a dict", {}, dict(good, payload="%%%"), dict(good, signatures=[]),
                dict(good, signatures="x"), dict(good, signatures=["x"]),
                dict(good, signatures=[{"sig": 5}])):
        assert not RC.verify_envelope(env, allowed_signers=signers, identity="i",  # type: ignore
                                      runner=runner)
    with pytest.raises(UsageError):
        RC.envelope_receipt({"payload": "%%%"})
    with pytest.raises(UsageError):
        RC.envelope_receipt({"payload": base64.b64encode(b"\xff").decode()})
    with pytest.raises(UsageError):
        RC.envelope_receipt({"payload": base64.b64encode(b"[1]").decode()})
    with pytest.raises(UsageError):
        RC.envelope_receipt({"payload": base64.b64encode(b'{"_type":"x"}').decode()})


def _key(tmp_path: Path) -> Path:
    key = tmp_path / "k"
    key.write_text("key")
    return key


# ---------------------------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------------------------


def test_receipt_rows_carry_the_realization_rate(tmp_path: Path) -> None:
    store = MemoryStore()
    r = _receipt()
    env = RC.sign(r, key_path=_key(tmp_path), runner=FakeRunner())
    row = RC.store_receipt(store, r, envelope=env)
    assert store.receipts() == [row]
    assert store.receipts(lever_class="cache_transform") == [row]
    assert row.receipt_id == RC.receipt_id(r) and row.receipt_id.startswith("rc_")
    assert row.lever_id == "cc.prompt_cache_ttl.main" and row.label == "verified"
    assert row.realization_rate == "0.812"
    assert row.created_ms == 1_790_164_800_000
    assert json.loads(row.json) == r and json.loads(row.dsse) == env
    other = json.loads(json.dumps(r))
    other["subject"]["lever_id"] = "custom.lever"
    del other["predicate"]["realization_rate_milli"]
    other["predicate"]["created"] = "2026-09-23"
    row2 = RC.receipt_row(other)
    assert row2.lever_class == "unknown" and row2.realization_rate is None and row2.dsse is None
    assert row2.created_ms == 1_790_121_600_000
    assert RC.receipt_row(dict(other, predicate={**other["predicate"],
                                                  "created": "2026-09-23T14:00:00+02:00"})
                          ).created_ms == 1_790_164_800_000
    assert RC.receipt_row(dict(other, predicate={**other["predicate"],
                                                  "created": "2026-09-23T12:00:00"})
                          ).created_ms == 1_790_164_800_000
    for bad in ({"subject": {}}, {"predicate": {**other["predicate"], "created": 5}}):
        with pytest.raises(UsageError):
            RC.receipt_row({**other, **bad})


def test_receipts_never_carry_the_canary() -> None:
    m = sample_measurement()
    m = m.__class__(**{**{f: getattr(m, f) for f in m.__slots__},
                       "lever_id": "x"})
    r = RC.build_receipt(m, lever_id="cc.prompt_cache_ttl.main", patch_sha256=PATCH,
                         shapley_credit=None, reconciliation_verdict="reconciled",
                         calibration=Calibration.CALIBRATED, tool_version="0.2.0",
                         created="2026-09-23")
    assert CANARY.encode() not in RC.canonical_bytes(r)


def test_receipts_module_has_no_float() -> None:
    """SPEC §2.4: verify/receipts.py is a money module."""
    src = (REPO / "tokenbill" / "verify" / "receipts.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        assert not (isinstance(node, ast.Constant) and isinstance(node.value, float))
        assert not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "float")


def test_label_values_match_the_evidence_enum() -> None:
    assert RC.SIGNABLE_LABELS == {Evidence.MEASURED.value, Evidence.VERIFIED.value}
