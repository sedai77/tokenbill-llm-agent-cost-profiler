#!/usr/bin/env python3
"""CycloneDX 1.5 JSON SBOM for the tokenbill wheel (SPEC §8.10). Standard library only.

The runtime SBOM describes one component, ``pkg:pypi/tokenbill@<version>`` (version read from
``tokenbill/__init__.py``, license MIT, SHA-256/SHA-512 of the wheel when ``--wheel`` is given),
with **zero runtime components**: ``components`` is empty, the dependency graph declares
``dependsOn: []`` and a ``complete`` composition says the list is exhaustive. Before writing
anything the script *asserts* that claim and exits 1 if any evidence disagrees:

* ``pyproject.toml`` ``[project].dependencies`` is ``[]`` (and not dynamic);
* the wheel's ``METADATA`` (``--wheel``) names ``tokenbill`` at the same version, carries no
  unconditional ``Requires-Dist`` and, when present, ``License-Expression: MIT``;
* the ``tokenbill`` entry of ``uv.lock`` (when present) has no runtime ``dependencies``.

The development toolchain (the ``dev`` extra and its locked closure from ``uv.lock``) is listed
separately with ``--dev-out``: a second CycloneDX document whose components all have
``scope: "excluded"`` (CycloneDX: used for test or other non-runtime purposes), each with its
locked version, purl and the sdist's URL and SHA-256. Reading ``uv.lock`` needs Python ≥ 3.11
(``tomllib``).

Output is deterministic: keys sorted, a serial number derived from the purl and the wheel hash
(UUIDv5), and a ``metadata.timestamp`` only when ``SOURCE_DATE_EPOCH`` is set.

Usage::

    python scripts/sbom.py --wheel dist/tokenbill-0.2.0-py3-none-any.whl \\
        -o dist/tokenbill-0.2.0.cdx.json --dev-out dist/tokenbill-0.2.0.dev.cdx.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import email.parser
import hashlib
import json
import os
import re
import sys
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_zero_deps import check_pyproject, wheel_runtime_requirements  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
NAME = "tokenbill"
LICENSE_ID = "MIT"
SPEC_VERSION = "1.5"
TOOL_VERSION = "1.0.0"
REPO_URL = "https://github.com/sedai77/tokenbill-llm-agent-cost-profiler"
_VERSION_RE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']\s*$""", re.M)


class SbomError(Exception):
    """The evidence contradicts the zero-runtime-component claim, or an input is unusable."""


def package_version(repo: Path = REPO) -> str:
    text = (repo / NAME / "__init__.py").read_text(encoding="utf-8")
    m = _VERSION_RE.search(text)
    if m is None:
        raise SbomError(f"no __version__ in {repo / NAME / '__init__.py'}")
    return m.group(1)


def purl(name: str, version: str) -> str:
    return f"pkg:pypi/{name.lower().replace('_', '-')}@{version}"


def file_hashes(path: Path) -> list[dict[str, str]]:
    sha256, sha512 = hashlib.sha256(), hashlib.sha512()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            sha256.update(chunk)
            sha512.update(chunk)
    return [{"alg": "SHA-256", "content": sha256.hexdigest()},
            {"alg": "SHA-512", "content": sha512.hexdigest()}]


def _timestamp() -> str | None:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if not epoch:
        return None
    try:
        when = _dt.datetime.fromtimestamp(int(epoch), tz=_dt.timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise SbomError(f"SOURCE_DATE_EPOCH={epoch!r} is not a Unix timestamp") from None
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _tools() -> dict:
    return {"components": [{"type": "application", "group": NAME, "name": "sbom.py",
                            "version": TOOL_VERSION,
                            "description": "stdlib CycloneDX generator (scripts/sbom.py)"}]}


# --------------------------------------------------------------------------------------------
# evidence for "zero runtime components"
# --------------------------------------------------------------------------------------------


def check_wheel(wheel: Path, version: str) -> list[str]:
    problems = []
    if not wheel.is_file():
        return [f"{wheel}: not found"]
    m = re.fullmatch(r"([A-Za-z0-9_.]+)-([^-]+)-.+\.whl", wheel.name)
    if m is None or m.group(1).lower() != NAME or m.group(2) != version:
        problems.append(f"{wheel.name}: not a {NAME} {version} wheel")
    try:
        with zipfile.ZipFile(wheel) as zf:
            meta = [n for n in zf.namelist()
                    if n.endswith(".dist-info/METADATA") and n.count("/") == 1]
            if len(meta) != 1:
                return [*problems, f"{wheel.name}: expected one METADATA, found {meta!r}"]
            msg = email.parser.HeaderParser().parsestr(zf.read(meta[0]).decode("utf-8"))
        runtime = wheel_runtime_requirements(wheel)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return [*problems, f"{wheel}: {exc}"]
    if (msg.get("Name") or "").lower() != NAME or msg.get("Version") != version:
        problems.append(f"{wheel.name}: METADATA says {msg.get('Name')} {msg.get('Version')}, "
                        f"expected {NAME} {version}")
    lic = msg.get("License-Expression")
    if lic is not None and lic != LICENSE_ID:
        problems.append(f"{wheel.name}: License-Expression {lic!r}, expected {LICENSE_ID}")
    problems += [f"{wheel.name}: runtime Requires-Dist {r!r}" for r in runtime]
    return problems


def _load_lock(path: Path) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:
        raise SbomError("reading uv.lock needs Python >= 3.11 (tomllib)") from None
    return tomllib.loads(path.read_text(encoding="utf-8"))


def check_lock(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        lock = _load_lock(path)
    except SbomError:
        return []  # optional evidence; pyproject and the wheel remain authoritative
    roots = [p for p in lock.get("package", []) if p.get("name") == NAME]
    if len(roots) != 1:
        return [f"{path}: expected one {NAME} package entry, found {len(roots)}"]
    deps = roots[0].get("dependencies", [])
    return [f"{path}: {NAME} has locked runtime dependencies {[d.get('name') for d in deps]}"] \
        if deps else []


# --------------------------------------------------------------------------------------------
# documents
# --------------------------------------------------------------------------------------------


def runtime_sbom(version: str, wheel: Path | None) -> dict:
    ref = purl(NAME, version)
    component: dict = {
        "type": "library",
        "bom-ref": ref,
        "name": NAME,
        "version": version,
        "description": "Token Bill: token economics and prompt-cache profiling for LLM agents",
        "licenses": [{"license": {"id": LICENSE_ID}}],
        "purl": ref,
        "externalReferences": [
            {"type": "vcs", "url": REPO_URL},
            {"type": "website", "url": REPO_URL},
            {"type": "issue-tracker", "url": f"{REPO_URL}/issues"},
            {"type": "distribution", "url": f"https://pypi.org/project/{NAME}/{version}/"},
        ],
        "properties": [{"name": "tokenbill:runtime-dependencies", "value": "0"}],
    }
    seed = f"{ref}#source"
    if wheel is not None:
        hashes = file_hashes(wheel)
        component["hashes"] = hashes
        component["properties"].append({"name": "tokenbill:wheel", "value": wheel.name})
        seed = f"{ref}#{hashes[0]['content']}"
    metadata: dict = {"lifecycles": [{"phase": "build"}], "tools": _tools(),
                      "component": component}
    ts = _timestamp()
    if ts is not None:
        metadata["timestamp"] = ts
    return {
        "bomFormat": "CycloneDX",
        "specVersion": SPEC_VERSION,
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, seed)}",
        "version": 1,
        "metadata": metadata,
        "components": [],
        "dependencies": [{"ref": ref, "dependsOn": []}],
        "compositions": [{"aggregate": "complete", "assemblies": [ref], "dependencies": [ref]}],
    }


def dev_sbom(version: str, lock_path: Path, runtime_serial: str) -> dict:
    """The locked ``dev`` toolchain (never shipped): every non-root package of ``uv.lock``."""
    lock = _load_lock(lock_path)
    root_ref = purl(NAME, version)
    packages = sorted((p for p in lock.get("package", []) if p.get("name") != NAME),
                      key=lambda p: (p["name"], p.get("version", "")))
    refs = {p["name"]: purl(p["name"], p.get("version", "")) for p in packages}
    components, graph = [], []
    for p in packages:
        ref = refs[p["name"]]
        comp: dict = {"type": "library", "bom-ref": ref, "name": p["name"],
                      "version": p.get("version", ""), "purl": ref, "scope": "excluded"}
        sdist = p.get("sdist") or {}
        alg, _, digest = str(sdist.get("hash", "")).partition(":")
        if sdist.get("url") and alg == "sha256" and digest:
            comp["externalReferences"] = [{"type": "distribution", "url": sdist["url"],
                                           "hashes": [{"alg": "SHA-256", "content": digest}]}]
        components.append(comp)
        deps = sorted({refs[d["name"]] for d in p.get("dependencies", []) if d["name"] in refs})
        graph.append({"ref": ref, "dependsOn": deps})
    roots = [p for p in lock.get("package", []) if p.get("name") == NAME]
    dev_direct = []
    if roots:
        dev_direct = sorted({refs[d["name"]] for d in
                             roots[0].get("optional-dependencies", {}).get("dev", [])
                             if d["name"] in refs})
    root = {"type": "library", "bom-ref": f"{root_ref}#dev", "name": NAME, "version": version,
            "purl": root_ref, "description": "tokenbill development/test toolchain (extra 'dev')",
            "properties": [{"name": "tokenbill:runtime-sbom", "value": runtime_serial}]}
    metadata: dict = {"lifecycles": [{"phase": "pre-build"}], "tools": _tools(),
                      "component": root}
    ts = _timestamp()
    if ts is not None:
        metadata["timestamp"] = ts
    digest = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    return {
        "bomFormat": "CycloneDX",
        "specVersion": SPEC_VERSION,
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'{root_ref}#dev#{digest}')}",
        "version": 1,
        "metadata": metadata,
        "components": components,
        "dependencies": [{"ref": f"{root_ref}#dev", "dependsOn": dev_direct}, *graph],
    }


def dumps(doc: dict) -> str:
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _write(text: str, out: Path | None) -> None:
    if out is None:
        sys.stdout.write(text)
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.partition("\n")[0])
    ap.add_argument("--wheel", type=Path, help="built wheel to describe and hash")
    ap.add_argument("-o", "--output", type=Path, help="runtime SBOM path (default: stdout)")
    ap.add_argument("--dev-out", type=Path, help="also write the dev-toolchain SBOM here")
    ap.add_argument("--pyproject", type=Path, default=REPO / "pyproject.toml")
    ap.add_argument("--lock", type=Path, default=REPO / "uv.lock")
    args = ap.parse_args(argv)
    try:
        version = package_version()
        problems = check_pyproject(args.pyproject) + check_lock(args.lock)
        if args.wheel is not None:
            problems += check_wheel(args.wheel, version)
        if problems:
            raise SbomError("zero-runtime-component check failed:\n  - " + "\n  - ".join(problems))
        doc = runtime_sbom(version, args.wheel)
        assert doc["components"] == [] and doc["dependencies"][0]["dependsOn"] == []
        dev = dev_sbom(version, args.lock, doc["serialNumber"]) if args.dev_out else None
    except (SbomError, OSError) as exc:
        print(f"sbom: {exc}", file=sys.stderr)
        return 1
    _write(dumps(doc), args.output)
    if dev is not None:
        _write(dumps(dev), args.dev_out)
    where = args.output or "stdout"
    print(f"sbom: {purl(NAME, version)} -> {where} (0 runtime components"
          + (f"; {len(dev['components'])} dev components -> {args.dev_out})" if dev else ")"),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
