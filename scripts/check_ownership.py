#!/usr/bin/env python3
"""Fail a package branch that touches files outside its ownership or any FROZEN file (SPEC §21 #2).

Usage::

    python scripts/check_ownership.py --package F-CORE --base v0.2
    python scripts/check_ownership.py --frozen-only --base origin/main

Lists the files changed versus the merge base of ``--base`` and ``HEAD`` (committed, staged,
unstaged and untracked; renames count as a delete plus an add, so moving a file checks both paths)
and checks each against ``OWNERSHIP.toml``. Exit 0 when clean, 1 with one line per violation, 2 on
usage errors. FROZEN files always violate, whatever the package. Stdlib only (Python >= 3.10: no
``tomllib`` needed — a minimal parser for the subset OWNERSHIP.toml uses is included).
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FROZEN = "FROZEN"


# ---------------------------------------------------------------------------------------------
# a minimal TOML subset: [tables], key = "string" | ["array", "of", "strings"], # comments
# ---------------------------------------------------------------------------------------------

_TABLE_RE = re.compile(r"^\[([A-Za-z0-9_.-]+)\]$")
_KEY_RE = re.compile(r"^([A-Za-z0-9_-]+)\s*=\s*(.*)$")
_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _strip_comment(line: str) -> str:
    out, in_str, escaped = [], False, False
    for ch in line:
        if in_str:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == "#":
            break
        if ch == '"':
            in_str = True
        out.append(ch)
    return "".join(out).strip()


def parse_toml_subset(text: str) -> dict[str, object]:
    """Parse the TOML subset OWNERSHIP.toml uses into nested dicts."""
    root: dict[str, object] = {}
    table: dict[str, object] = root
    pending_key: str | None = None
    pending: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line:
            continue
        if pending_key is not None:
            pending.extend(_STRING_RE.findall(line))
            if line.endswith("]"):
                table[pending_key] = [_unescape(s) for s in pending]
                pending_key, pending = None, []
            continue
        m = _TABLE_RE.match(line)
        if m:
            table = root
            for part in m.group(1).split("."):
                table = table.setdefault(part, {})  # type: ignore[assignment]
                if not isinstance(table, dict):
                    raise ValueError(f"line {lineno}: table {m.group(1)} clashes with a key")
            continue
        m = _KEY_RE.match(line)
        if not m:
            raise ValueError(f"line {lineno}: unsupported TOML")
        key, value = m.group(1), m.group(2).strip()
        if value.startswith("["):
            items = _STRING_RE.findall(value)
            if value.endswith("]"):
                table[key] = [_unescape(s) for s in items]
            else:
                pending_key, pending = key, items
        elif value.startswith('"') and value.endswith('"'):
            table[key] = _unescape(value[1:-1])
        else:
            raise ValueError(f"line {lineno}: only strings and string arrays are supported")
    if pending_key is not None:
        raise ValueError("unterminated array")
    return root


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\")


def _load_toml(text: str) -> dict[str, object]:
    try:
        import tomllib  # Python >= 3.11
    except ImportError:  # pragma: no cover - exercised on 3.10
        return parse_toml_subset(text)
    return tomllib.loads(text)


# ---------------------------------------------------------------------------------------------
# ownership model
# ---------------------------------------------------------------------------------------------


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """``*`` = any characters within one path segment; ``**`` = any number of whole segments."""
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            parts.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            parts.append(".*")
            i += 2
        elif pattern[i] == "*":
            parts.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(parts) + r"\Z")


@dataclass
class Owner:
    name: str
    include: list[re.Pattern[str]] = field(default_factory=list)
    exclude: list[re.Pattern[str]] = field(default_factory=list)

    def owns(self, path: str) -> bool:
        return any(p.match(path) for p in self.include) and not any(
            p.match(path) for p in self.exclude
        )


@dataclass
class Ownership:
    frozen: Owner
    packages: dict[str, Owner]

    def owners_of(self, path: str) -> list[str]:
        """Every owner (packages and ``FROZEN``) whose patterns match *path*."""
        found = [FROZEN] if self.frozen.owns(path) else []
        found.extend(name for name, owner in self.packages.items() if owner.owns(path))
        return found


def load_ownership(path: Path) -> Ownership:
    """Parse OWNERSHIP.toml."""
    doc = _load_toml(Path(path).read_text(encoding="utf-8"))
    frozen_tbl = doc.get("frozen", {})
    packages_tbl = doc.get("packages", {})
    if not isinstance(frozen_tbl, dict) or not isinstance(packages_tbl, dict):
        raise ValueError("OWNERSHIP.toml needs [frozen] and [packages.*] tables")

    def owner(name: str, tbl: dict[str, object]) -> Owner:
        inc = tbl.get("paths", [])
        exc = tbl.get("exclude", [])
        if not isinstance(inc, list) or not isinstance(exc, list):
            raise ValueError(f"{name}: paths/exclude must be arrays")
        return Owner(
            name, [glob_to_regex(str(p)) for p in inc], [glob_to_regex(str(p)) for p in exc]
        )

    return Ownership(
        frozen=owner(FROZEN, frozen_tbl),
        packages={
            name: owner(name, tbl) for name, tbl in packages_tbl.items() if isinstance(tbl, dict)
        },
    )


def check(package: str | None, files: Iterable[str], ownership: Ownership) -> list[str]:
    """Violations of *package* for the changed *files* (empty list = clean). With ``package=None``
    only FROZEN files are checked (integration and other non-package branches)."""
    if package is not None and package not in ownership.packages:
        raise KeyError(package)
    violations = []
    for path in sorted(set(files)):
        if ownership.frozen.owns(path):
            violations.append(f"FROZEN file changed: {path}")
        elif package is not None and not ownership.packages[package].owns(path):
            owners = [o for o in ownership.owners_of(path) if o != package]
            who = ", ".join(owners) if owners else "no package"
            violations.append(f"outside {package} ownership: {path} (owned by {who})")
    return violations


def _git(args: Sequence[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def changed_files(base: str, cwd: Path = REPO_ROOT) -> list[str]:
    """Paths changed since the merge base of *base* and HEAD, plus untracked files."""
    merge_base = _git(["merge-base", base, "HEAD"], cwd).strip()
    diff = _git(["diff", "--name-only", "--no-renames", merge_base], cwd)
    untracked = _git(["ls-files", "--others", "--exclude-standard"], cwd)
    return sorted({line for line in (diff + untracked).splitlines() if line})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--package", help="work package id, e.g. F-CORE")
    who.add_argument(
        "--frozen-only",
        action="store_true",
        help="check only that no FROZEN file changed (non-package branches)",
    )
    parser.add_argument("--base", required=True, help="git ref the branch started from, e.g. v0.2")
    parser.add_argument(
        "--ownership", type=Path, default=None, help="OWNERSHIP.toml (default: the repository's)"
    )
    parser.add_argument(
        "--repo", type=Path, default=REPO_ROOT, help="repository root (default: this one)"
    )
    args = parser.parse_args(argv)
    ownership = load_ownership(args.ownership or args.repo / "OWNERSHIP.toml")
    if args.package is not None and args.package not in ownership.packages:
        print(f"check_ownership: unknown package {args.package!r}", file=sys.stderr)
        return 2
    try:
        files = changed_files(args.base, args.repo)
    except subprocess.CalledProcessError as exc:
        print(f"check_ownership: git failed: {exc.stderr.strip()}", file=sys.stderr)
        return 2
    violations = check(args.package, files, ownership)
    for line in violations:
        print(line)
    scope = args.package or "FROZEN files"
    if violations:
        print(f"{len(violations)} ownership violation(s) for {scope}", file=sys.stderr)
        return 1
    print(f"ownership clean ({scope}): {len(files)} changed file(s) checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
