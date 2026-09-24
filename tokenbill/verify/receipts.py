"""Signed savings receipts (SPEC §13.6, D15): canonical JSON, DSSE, OpenSSH signatures.

* **Canonical JSON**: sorted keys, ``separators=(",", ":")``, ASCII keys, **integers and strings
  only** (money ``*_usd_micro``, ratios ``*_milli``/``*_ppm``; integers within ±(2⁵³ − 1); booleans
  are written as the strings ``"true"``/``"false"``; absent optional values are omitted) — for this
  subset the bytes equal RFC 8785 JCS.
* **Body**: ``_type "urn:tokenbill:receipt:v1"``, ``subject {lever_id, patch_sha256}``,
  ``predicate {label, design, metric, scope_label, window, estimate_usd_micro, ci_low_usd_micro,
  ci_high_usd_micro, projected_usd_micro, realization_rate_milli, shapley_credit_usd_micro,
  guards[], adjustments[], rate_card_sha256, assignment_log_sha256, preregistration_sha256,
  reconciliation_verdict, calibration, tool_version, created}``. Two additive predicate fields
  carry the refusal inputs a receipt must be judged on by itself: ``basis`` (the estimate's
  basis; allowance receipts are refused) and ``signable`` (the measurement's own verdict).
* **DSSE**: ``payloadType "application/vnd.tokenbill.receipt+json"``,
  ``PAE = "DSSEv1" SP len(type) SP type SP len(body) SP body``, envelope
  ``{payload: base64, payloadType, signatures: [{keyid, sig}]}`` where ``sig`` is the base64 of the
  armored SSH signature file.
* **Signing**: ``ssh-keygen -Y sign -f KEY -n tokenbill-receipt FILE`` over the PAE bytes;
  **verifying**: ``ssh-keygen -Y verify -f allowed_signers -I IDENTITY -n tokenbill-receipt -s SIG``
  with the PAE on stdin (OpenSSH ≥ 8.1). Subprocesses run through an injectable ``runner``
  (``subprocess.run`` by default) with an argument list, never a shell.
* :func:`sign` refuses (:class:`~tokenbill.core.errors.GateFailed`, CLI exit 3) any receipt that is
  not signable: label not measured/verified, reconciliation not ``reconciled``, allowance
  (``list_equivalent``) basis, a projection that is not calibrated, or a measurement that declared
  itself unsignable.

This is a money module: no float anywhere (SPEC §2.4, AST-linted).
"""

from __future__ import annotations

import base64
import binascii
import datetime as _dt
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from tokenbill.common import TokenbillError
from tokenbill.core.catalog import lever as catalog_lever
from tokenbill.core.errors import GateFailed, UsageError
from tokenbill.core.ids import stable_id
from tokenbill.core.labels import Calibration, Figure
from tokenbill.core.money import nano_to_micro
from tokenbill.core.protocols import LedgerStore
from tokenbill.core.types import MeasurementResult, ReceiptRow

__all__ = [
    "NAMESPACE",
    "PAYLOAD_TYPE",
    "RECEIPT_TYPE",
    "SigningError",
    "SshKeygenUnavailable",
    "build_receipt",
    "canonical_bytes",
    "envelope_receipt",
    "pae",
    "receipt_id",
    "receipt_row",
    "refusal_reasons",
    "sign",
    "store_receipt",
    "verify_envelope",
]

RECEIPT_TYPE = "urn:tokenbill:receipt:v1"
PAYLOAD_TYPE = "application/vnd.tokenbill.receipt+json"
NAMESPACE = "tokenbill-receipt"
SIGNABLE_LABELS = frozenset({"measured", "verified"})
_MAX_INT = 2**53 - 1
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TIMEOUT_S = 120

Runner = Callable[..., Any]


class SshKeygenUnavailable(TokenbillError):
    """OpenSSH ``ssh-keygen`` (≥ 8.1) is not installed or not on PATH."""


class SigningError(TokenbillError):
    """``ssh-keygen`` ran but did not produce a signature."""


# ---------------------------------------------------------------------------------------------
# canonical bytes and PAE
# ---------------------------------------------------------------------------------------------


def _check(value: object, where: str) -> None:
    t = type(value)
    if t is str:
        return
    if t is int:
        if not -_MAX_INT <= value <= _MAX_INT:  # type: ignore[operator]
            raise UsageError(f"receipt integer out of the I-JSON range at {where}")
        return
    if t is dict:
        for k, v in value.items():  # type: ignore[attr-defined]
            if type(k) is not str or not k.isascii():
                raise UsageError(f"receipt keys must be ASCII strings (at {where})")
            _check(v, f"{where}.{k}")
        return
    if t is list or t is tuple:
        for i, v in enumerate(value):  # type: ignore[arg-type]
            _check(v, f"{where}[{i}]")
        return
    raise UsageError(f"receipts hold only objects, arrays, strings and integers (at {where})")


def canonical_bytes(receipt: Mapping[str, object]) -> bytes:
    """The canonical encoding (sorted keys, compact, UTF-8; integers and strings only)."""
    if not isinstance(receipt, Mapping):
        raise UsageError("a receipt is a JSON object")
    data = dict(receipt)
    _check(data, "$")
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        raise UsageError("receipt strings must be valid Unicode") from None


def pae(payload_type: str, body: bytes) -> bytes:
    """DSSE pre-authentication encoding:
    ``"DSSEv1" SP len(type) SP type SP len(body) SP body`` (lengths in bytes, ASCII decimal)."""
    if not isinstance(payload_type, str) or not isinstance(body, (bytes, bytearray)):
        raise UsageError("pae takes a str payload type and bytes")
    t = payload_type.encode("utf-8")
    return b"DSSEv1 %d %s %d %s" % (len(t), t, len(body), bytes(body))


# ---------------------------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------------------------


def _micro(fig: Figure | None, attr: str = "nano") -> int | None:
    if fig is None:
        return None
    v = getattr(fig, attr)
    return nano_to_micro(v) if v is not None else None


def _milli(value: str) -> int:
    try:
        d = Decimal(value)
    except (InvalidOperation, TypeError):
        raise UsageError("realization rate must be a decimal string") from None
    if not d.is_finite():
        raise UsageError("realization rate must be finite")
    return int((d * 1000).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def build_receipt(m: MeasurementResult, *, lever_id: str, patch_sha256: str,
                  shapley_credit: Figure | None, reconciliation_verdict: str,
                  calibration: Calibration, tool_version: str, created: str) -> dict:
    """The receipt body for measurement *m* (see the module docstring)."""
    if not isinstance(m, MeasurementResult):
        raise UsageError("build_receipt expects a MeasurementResult")
    if not isinstance(lever_id, str) or not lever_id:
        raise UsageError("lever_id must be a non-empty string")
    if not isinstance(patch_sha256, str) or not _SHA256_RE.match(patch_sha256):
        raise UsageError("patch_sha256 must be 64 lowercase hex characters")
    if m.estimate.nano is None:
        raise UsageError("an unpriced estimate cannot be receipted")
    for name, value in (("reconciliation_verdict", reconciliation_verdict),
                        ("tool_version", tool_version), ("created", created)):
        if not isinstance(value, str) or not value:
            raise UsageError(f"{name} must be a non-empty string")
    _created_ms(created)
    cal = Calibration(calibration)
    predicate: dict[str, object] = {
        "label": m.estimate.evidence.value,
        "basis": m.estimate.basis.value,
        "signable": "true" if m.signable else "false",
        "design": m.design,
        "metric": m.unit,
        "scope_label": m.scope_label,
        "window": {k: v for k, v in m.window},
        "estimate_usd_micro": nano_to_micro(m.estimate.nano),
        "guards": [{"name": g.name, "passed": "true" if g.passed else "false",
                    "threshold": g.threshold, "value": g.value} for g in m.guards],
        "adjustments": list(m.adjustments),
        "rate_card_sha256": m.rate_card_sha256,
        "reconciliation_verdict": reconciliation_verdict,
        "calibration": cal.value,
        "tool_version": tool_version,
        "created": created,
    }
    optional: dict[str, object | None] = {
        "ci_low_usd_micro": _micro(m.estimate, "low_nano"),
        "ci_high_usd_micro": _micro(m.estimate, "high_nano"),
        "projected_usd_micro": _micro(m.projected),
        "realization_rate_milli": (_milli(m.realization_rate[0])
                                   if m.realization_rate is not None else None),
        "shapley_credit_usd_micro": _micro(shapley_credit),
        "assignment_log_sha256": m.assignment_log_sha256,
        "preregistration_sha256": m.preregistration_sha256,
    }
    predicate.update({k: v for k, v in optional.items() if v is not None})
    receipt = {"_type": RECEIPT_TYPE,
               "subject": {"lever_id": lever_id, "patch_sha256": patch_sha256},
               "predicate": predicate}
    canonical_bytes(receipt)  # validates the integer/string-only shape
    return receipt


def _predicate(receipt: Mapping[str, object]) -> Mapping[str, object]:
    pred = receipt.get("predicate")
    if receipt.get("_type") != RECEIPT_TYPE or not isinstance(pred, Mapping):
        raise UsageError("not a tokenbill receipt")
    return pred


def refusal_reasons(receipt: Mapping[str, object]) -> tuple[str, ...]:
    """Why *receipt* may not be signed (empty when it may)."""
    pred = _predicate(receipt)
    reasons = []
    label = pred.get("label")
    if label not in SIGNABLE_LABELS:
        reasons.append(f"label {label} (only measured or verified receipts are signed)")
    if pred.get("reconciliation_verdict") != "reconciled":
        reasons.append("reconciliation not passed")
    basis = pred.get("basis")
    if basis is None:
        reasons.append("basis missing")
    elif basis == "list_equivalent":
        reasons.append("allowance (list-equivalent) basis is never invoice savings")
    if "projected_usd_micro" in pred and pred.get("calibration") != Calibration.CALIBRATED.value:
        reasons.append("projection not calibrated")
    if pred.get("signable") != "true":
        reasons.append("the measurement is not signable")
    return tuple(reasons)


# ---------------------------------------------------------------------------------------------
# ssh-keygen
# ---------------------------------------------------------------------------------------------


def _ssh_keygen(runner: Runner) -> str:
    if runner is subprocess.run:
        exe = shutil.which("ssh-keygen")
        if exe is None:
            raise SshKeygenUnavailable("ssh-keygen (OpenSSH >= 8.1) is not on PATH; install "
                                       "OpenSSH to sign or verify receipts")
        return exe
    return "ssh-keygen"


def _run(runner: Runner, args: list[str], data: bytes) -> Any:
    try:
        return runner(args, input=data, capture_output=True, check=False, timeout=_TIMEOUT_S)
    except FileNotFoundError:
        raise SshKeygenUnavailable("ssh-keygen (OpenSSH >= 8.1) is not on PATH; install "
                                   "OpenSSH to sign or verify receipts") from None
    except subprocess.TimeoutExpired:
        raise SigningError("ssh-keygen timed out") from None


def _key_fingerprint(key_path: Path) -> str:
    pub = Path(str(key_path) + ".pub")
    try:
        parts = pub.read_text(encoding="utf-8").split()
        blob = base64.b64decode(parts[1], validate=True)
    except (OSError, IndexError, ValueError, binascii.Error):
        return ""
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return "SHA256:" + digest


def sign(receipt: Mapping[str, object], *, key_path: Path,
         runner: Runner = subprocess.run) -> dict:
    """Sign *receipt* with the OpenSSH private key at *key_path*; returns the DSSE envelope.

    Refuses unsignable receipts with ``GateFailed`` (exit 3); a missing ``ssh-keygen`` raises
    :class:`SshKeygenUnavailable`."""
    reasons = refusal_reasons(receipt)
    if reasons:
        raise GateFailed("receipt not signable: " + "; ".join(reasons))
    body = canonical_bytes(receipt)
    key = Path(key_path)
    if not key.is_file():
        raise UsageError("the signing key file does not exist")
    exe = _ssh_keygen(runner)
    with tempfile.TemporaryDirectory(prefix="tokenbill-sign-") as tmp:
        msg = Path(tmp) / "receipt.pae"
        msg.write_bytes(pae(PAYLOAD_TYPE, body))
        proc = _run(runner, [exe, "-Y", "sign", "-f", str(key), "-n", NAMESPACE, str(msg)], b"")
        if proc.returncode != 0:
            raise SigningError(f"ssh-keygen -Y sign failed (exit {proc.returncode})")
        sig_path = Path(str(msg) + ".sig")
        if not sig_path.is_file():
            raise SigningError("ssh-keygen -Y sign produced no signature file")
        sig = sig_path.read_bytes()
    return {"payload": base64.b64encode(body).decode("ascii"), "payloadType": PAYLOAD_TYPE,
            "signatures": [{"keyid": _key_fingerprint(key),
                            "sig": base64.b64encode(sig).decode("ascii")}]}


def _b64(value: object) -> bytes | None:
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


def verify_envelope(envelope: Mapping[str, object], *, allowed_signers: Path, identity: str,
                    runner: Runner = subprocess.run) -> bool:
    """True iff one of the envelope's signatures verifies for *identity* under the
    ``allowed_signers`` file and namespace ``tokenbill-receipt`` over the PAE of its payload."""
    if not isinstance(envelope, Mapping) or envelope.get("payloadType") != PAYLOAD_TYPE:
        return False
    body = _b64(envelope.get("payload"))
    sigs = envelope.get("signatures")
    if body is None or not isinstance(sigs, list) or not sigs:
        return False
    if not isinstance(identity, str) or not identity:
        raise UsageError("identity must be a non-empty string")
    signers = Path(allowed_signers)
    if not signers.is_file():
        raise UsageError("the allowed_signers file does not exist")
    exe = _ssh_keygen(runner)
    data = pae(PAYLOAD_TYPE, body)
    with tempfile.TemporaryDirectory(prefix="tokenbill-verify-") as tmp:
        for i, entry in enumerate(sigs):
            sig = _b64(entry.get("sig")) if isinstance(entry, Mapping) else None
            if not sig:
                continue
            sig_path = Path(tmp) / f"receipt.{i}.sig"
            sig_path.write_bytes(sig)
            proc = _run(runner, [exe, "-Y", "verify", "-f", str(signers), "-I", identity, "-n",
                                 NAMESPACE, "-s", str(sig_path)], data)
            if proc.returncode == 0:
                return True
    return False


def envelope_receipt(envelope: Mapping[str, object]) -> dict:
    """The receipt carried by a DSSE envelope (not a signature check)."""
    body = _b64(envelope.get("payload")) if isinstance(envelope, Mapping) else None
    if body is None:
        raise UsageError("the envelope payload is not base64")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise UsageError("the envelope payload is not JSON") from None
    if not isinstance(data, dict):
        raise UsageError("the envelope payload is not a receipt")
    _predicate(data)
    return data


# ---------------------------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------------------------


def _created_ms(created: str) -> int:
    text = created.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        if len(text) == 10:
            moment = _dt.datetime.combine(_dt.date.fromisoformat(text), _dt.time(),
                                          tzinfo=_dt.timezone.utc)
        else:
            moment = _dt.datetime.fromisoformat(text)
    except ValueError:
        raise UsageError("created must be an ISO 8601 date or date-time") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    delta = moment - _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
    return (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000


def receipt_id(receipt: Mapping[str, object]) -> str:
    """``rc_…``: a stable id over the canonical bytes."""
    return stable_id("rc", hashlib.sha256(canonical_bytes(receipt)).hexdigest())


def receipt_row(receipt: Mapping[str, object], *,
                envelope: Mapping[str, object] | None = None) -> ReceiptRow:
    """The store row of *receipt* (the realization rate travels with it, SPEC §13.6)."""
    pred = _predicate(receipt)
    subject = receipt.get("subject")
    lever_id = subject.get("lever_id") if isinstance(subject, Mapping) else None
    if not isinstance(lever_id, str) or not lever_id:
        raise UsageError("receipt subject has no lever_id")
    try:
        lever_class = catalog_lever(lever_id).lever_class
    except UsageError:
        lever_class = "unknown"
    milli = pred.get("realization_rate_milli")
    rr = str(Decimal(milli) / 1000) if type(milli) is int else None
    created = pred.get("created")
    if not isinstance(created, str):
        raise UsageError("receipt predicate has no created time")
    dsse = canonical_bytes(envelope).decode("utf-8") if envelope is not None else None
    return ReceiptRow(receipt_id=receipt_id(receipt), lever_id=lever_id,
                      lever_class=lever_class, label=str(pred.get("label")),
                      realization_rate=rr, created_ms=_created_ms(created),
                      json=canonical_bytes(receipt).decode("utf-8"), dsse=dsse)


def store_receipt(store: LedgerStore, receipt: Mapping[str, object], *,
                  envelope: Mapping[str, object] | None = None) -> ReceiptRow:
    """Persist *receipt* (and its envelope) with ``LedgerStore.put_receipt``."""
    row = receipt_row(receipt, envelope=envelope)
    store.put_receipt(row)
    return row
