"""``tokenbill receipt create | sign | verify``: signed savings receipts (SPEC §13.6).

``create`` turns a ``measure run -o measurement.json`` into a canonical receipt (integers and
strings only); ``sign`` wraps it in a DSSE envelope signed with ``ssh-keygen -Y sign`` and refuses
(exit 3) any receipt that is not signable (estimated or exact label, unreconciled, allowance
basis, uncalibrated projection); ``verify`` checks an envelope against an ``allowed_signers`` file
and identity (exit 3 when it does not verify).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tokenbill.core.errors import UsageError
from tokenbill.pipeline import savings as sv
from tokenbill.pipeline import verification as vf

VERB = "receipt"
HELP = "savings receipts: create from a measurement, sign (DSSE, ssh-keygen), verify"


def add_parser(subparsers: argparse._SubParsersAction) -> argparse.ArgumentParser:
    """Register ``receipt create``, ``receipt sign`` and ``receipt verify``."""
    parser = subparsers.add_parser(VERB, help=HELP, description=HELP + ".")
    actions = parser.add_subparsers(dest="receipt_action", metavar="ACTION", required=True)
    create = actions.add_parser("create", help="receipt from a measurement",
                                description="Build a receipt from measure run -o output.")
    create.add_argument("--measurement", required=True, type=Path, metavar="FILE",
                        help="measurement.json of measure run")
    create.add_argument("-o", "--output", dest="output", type=Path, metavar="FILE",
                        default=Path("receipt.json"), help="receipt file (default receipt.json)")
    create.add_argument("--patch", type=Path, metavar="FILE", default=None,
                        help="the settings patch that was rolled out (its SHA-256 is the "
                             "receipt subject)")
    sv.add_db(create, help_text="also store the receipt (and its realization rate) in the "
                                "ledger")
    sv.add_format(create)
    sign = actions.add_parser("sign", help="sign a receipt (DSSE envelope)",
                              description="Sign a receipt with an OpenSSH key (ssh-keygen -Y).")
    sign.add_argument("receipt", type=Path, metavar="RECEIPT", help="receipt.json")
    sign.add_argument("--key", required=True, type=Path, metavar="PATH",
                      help="OpenSSH private key")
    sign.add_argument("-o", "--output", dest="output", type=Path, metavar="FILE", default=None,
                      help="envelope file (default <receipt>.dsse.json)")
    sv.add_db(sign, help_text="also store the signed receipt in the ledger")
    sv.add_format(sign)
    verify = actions.add_parser("verify", help="verify a signed receipt",
                                description="Verify a DSSE envelope with ssh-keygen -Y verify.")
    verify.add_argument("envelope", type=Path, metavar="ENVELOPE", help="receipt.dsse.json")
    verify.add_argument("--allowed-signers", required=True, type=Path, metavar="FILE",
                        help="OpenSSH allowed_signers file")
    verify.add_argument("--identity", required=True, metavar="ID",
                        help="signer identity in the allowed_signers file")
    sv.add_format(verify)
    return parser


def _canonical(doc: object) -> str:
    from tokenbill.verify.receipts import canonical_bytes

    return canonical_bytes(doc).decode("ascii") + "\n"  # type: ignore[arg-type]


def run(args: argparse.Namespace) -> int:
    """Execute a ``receipt`` action (exit 3 on a refusal or a failed verification)."""
    def body() -> int:
        action = getattr(args, "receipt_action", None)
        if action not in ("create", "sign", "verify"):
            raise UsageError("receipt needs an action: create, sign or verify")
        env = sv.env_from_args(args)
        store = None
        if action in ("create", "sign") and getattr(args, "db", None):
            store, _path = sv.open_ledger(args, env)
        try:
            result, doc = vf.run_receipt(
                action, env, measurement=getattr(args, "measurement", None),
                receipt=getattr(args, "receipt", None), envelope=getattr(args, "envelope", None),
                key=getattr(args, "key", None),
                allowed_signers=getattr(args, "allowed_signers", None),
                identity=getattr(args, "identity", None), patch=getattr(args, "patch", None),
                store=store)
        finally:
            sv.close_ledger(store)
        if action == "create":
            sv.write_text(args.output, _canonical(doc))
        elif action == "sign":
            out = args.output or Path(str(args.receipt).removesuffix(".json") + ".dsse.json")
            sv.write_text(out, json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n")
        sv.emit(result, args)
        return sv.EXIT_OK

    return sv.run_command(body, verb=VERB)
