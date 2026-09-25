#!/usr/bin/env python3
"""Zero-runtime-dependency gate (SPEC §8.10). Standard library only; exits 1 on any violation.

Checks, in order:

1. ``pyproject.toml``: ``[project].dependencies`` is present and empty, and ``dependencies`` is not
   listed in ``[project].dynamic`` (so no build hook can add one back).
2. Static: every ``import`` / ``from … import`` (top level *and* inside functions) in every ``.py``
   file of the ``tokenbill`` package names a standard-library module (``sys.stdlib_module_names``),
   ``tokenbill`` itself or ``__future__``. This also covers lazy imports a runtime check never
   reaches, and the package-data scripts (hook templates) that are shipped but never imported.
3. Runtime: every importable ``tokenbill`` module (a ``.py`` file in a regular package, except
   ``__main__``) is imported, and ``sys.modules`` is diffed against the snapshot taken before the
   first import: any new top-level module that is neither stdlib nor ``tokenbill`` fails, and so
   does a module that cannot be imported. Run it with the interpreter of a *clean* virtual
   environment so an undeclared third-party import fails loudly instead of resolving by accident.
4. ``--wheel PATH``: the wheel's ``METADATA`` carries no unconditional ``Requires-Dist`` (only
   ``extra == …`` ones, i.e. the ``dev`` extra).

By default the package is imported from the source tree next to this script. ``--installed``
imports whatever ``import tokenbill`` resolves to instead (the wheel installed in the running
interpreter) and fails if that is the source tree.

Usage::

    python scripts/check_zero_deps.py
    python scripts/check_zero_deps.py --installed --wheel dist/tokenbill-0.2.0-py3-none-any.whl
"""

from __future__ import annotations

import argparse
import ast
import email.parser
import importlib
import re
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGE = "tokenbill"
#: Top-level modules that joined the standard library after 3.10 (the floor), so an older
#: interpreter's ``sys.stdlib_module_names`` does not list them; tokenbill imports them only behind
#: a version or ImportError guard (e.g. ``compression.zstd`` for ``.jsonl.zst`` on 3.14+).
LATER_STDLIB = {"tomllib": "3.11", "compression": "3.14", "annotationlib": "3.14"}
#: Aliases the stdlib itself registers in ``sys.modules`` (``multiprocessing`` adds
#: ``__mp_main__`` for ``__main__``).
STDLIB_ALIASES = {"__main__", "__mp_main__"}
ALLOWED = (frozenset(sys.stdlib_module_names) | LATER_STDLIB.keys() | STDLIB_ALIASES
           | {PACKAGE, "__future__"})


def _top(name: str) -> str:
    return name.partition(".")[0]


# --------------------------------------------------------------------------------------------
# 1. pyproject
# --------------------------------------------------------------------------------------------


def _project_table(text: str) -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10: a narrow fallback for the two keys we need
        return _project_table_fallback(text)
    return tomllib.loads(text).get("project", {})


def _project_table_fallback(text: str) -> dict:
    section = re.search(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    if section is None:
        return {}
    body = section.group(1)
    out: dict = {}
    for key in ("dependencies", "dynamic"):
        m = re.search(rf"^{key}\s*=\s*\[(.*?)\]", body, re.M | re.S)
        if m is not None:
            out[key] = re.findall(r"""["']([^"']*)["']""", m.group(1))
    return out


def check_pyproject(path: Path) -> list[str]:
    if not path.is_file():
        return [f"{path}: not found"]
    project = _project_table(path.read_text(encoding="utf-8"))
    problems = []
    if "dependencies" not in project:
        problems.append(f"{path}: [project].dependencies is missing (must be present and empty)")
    elif project["dependencies"]:
        problems.append(f"{path}: [project].dependencies must be [], found "
                        f"{project['dependencies']!r}")
    if "dependencies" in project.get("dynamic", []):
        problems.append(f"{path}: 'dependencies' must not be dynamic")
    return problems


# --------------------------------------------------------------------------------------------
# 2. static import scan
# --------------------------------------------------------------------------------------------


def _imported_names(tree: ast.AST) -> list[tuple[int, str]]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            out.append((node.lineno, node.module))
        elif (isinstance(node, ast.Call) and node.args
              and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
              and ((isinstance(node.func, ast.Name) and node.func.id == "__import__")
                   or (isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"
                       and isinstance(node.func.value, ast.Name)
                       and node.func.value.id == "importlib"))):
            out.append((node.lineno, node.args[0].value))
    return out


def check_static(package_dir: Path) -> tuple[int, list[str]]:
    problems = []
    files = sorted(package_dir.rglob("*.py"))
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as exc:
            problems.append(f"{path}: cannot parse ({exc.__class__.__name__}: {exc})")
            continue
        for lineno, name in _imported_names(tree):
            if name and not name.startswith(".") and _top(name) not in ALLOWED:
                problems.append(f"{path}:{lineno}: imports non-stdlib module {name!r}")
    return len(files), problems


# --------------------------------------------------------------------------------------------
# 3. runtime import check
# --------------------------------------------------------------------------------------------


def importable_modules(package_dir: Path) -> list[str]:
    """Dotted names of the modules in regular packages (a chain of ``__init__.py`` up to the
    package root); ``__main__`` and package-data scripts are left to the static scan."""
    names = []
    for path in sorted(package_dir.rglob("*.py")):
        rel = path.relative_to(package_dir.parent)
        parents = rel.parents
        if not all((package_dir.parent / p / "__init__.py").is_file()
                   for p in list(parents)[:-1]):
            continue
        parts = list(rel.with_suffix("").parts)
        if parts[-1] == "__main__":
            continue
        if parts[-1] == "__init__":
            parts.pop()
        names.append(".".join(parts))
    return names


def check_runtime(package_dir: Path) -> tuple[int, list[str]]:
    before = set(sys.modules)
    problems = []
    modules = importable_modules(package_dir)
    for name in modules:
        try:
            importlib.import_module(name)
        except (Exception, SystemExit) as exc:  # noqa: BLE001 - every failure is a finding
            problems.append(f"import {name}: {exc.__class__.__name__}: {exc}")
    new = sorted({_top(m) for m in set(sys.modules) - before} - ALLOWED)
    for top in new:
        culprits = sorted(m for m in sys.modules if _top(m) == top)[:5]
        problems.append(f"importing {PACKAGE} pulled in non-stdlib module {top!r} "
                        f"({', '.join(culprits)})")
    return len(modules), problems


# --------------------------------------------------------------------------------------------
# 4. wheel metadata
# --------------------------------------------------------------------------------------------


def wheel_runtime_requirements(wheel: Path) -> list[str]:
    """Unconditional ``Requires-Dist`` entries of a wheel (those without an ``extra`` marker)."""
    with zipfile.ZipFile(wheel) as zf:
        meta = [n for n in zf.namelist()
                if n.endswith(".dist-info/METADATA") and n.count("/") == 1]
        if len(meta) != 1:
            raise ValueError(f"{wheel}: expected one .dist-info/METADATA, found {meta!r}")
        text = zf.read(meta[0]).decode("utf-8")
    msg = email.parser.HeaderParser().parsestr(text)
    return [r for r in msg.get_all("Requires-Dist", [])
            if not re.search(r"""\bextra\s*==""", r.partition(";")[2])]


def check_wheel(wheel: Path) -> list[str]:
    try:
        reqs = wheel_runtime_requirements(wheel)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        return [f"{wheel}: {exc}"]
    return [f"{wheel}: runtime Requires-Dist {r!r}" for r in reqs]


# --------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.partition("\n")[0])
    ap.add_argument("--pyproject", type=Path, default=REPO / "pyproject.toml")
    ap.add_argument("--installed", action="store_true",
                    help="import the installed tokenbill instead of the source tree")
    ap.add_argument("--wheel", type=Path, action="append", default=[],
                    help="also check this wheel's METADATA (repeatable)")
    args = ap.parse_args(argv)

    if not args.installed:
        sys.path.insert(0, str(REPO))
    try:
        pkg = importlib.import_module(PACKAGE)
    except ImportError as exc:
        print(f"FAIL: cannot import {PACKAGE}: {exc}", file=sys.stderr)
        return 1
    package_dir = Path(pkg.__file__).resolve().parent
    problems = []
    if args.installed and package_dir.is_relative_to(REPO.resolve()):
        problems.append(f"--installed imported the source tree {package_dir}; run from outside "
                        f"the repository with the wheel installed")

    problems += check_pyproject(args.pyproject)
    n_files, static = check_static(package_dir)
    n_modules, runtime = check_runtime(package_dir)
    problems += static + runtime
    for wheel in args.wheel:
        problems += check_wheel(wheel)

    print(f"{PACKAGE} {getattr(pkg, '__version__', '?')} from {package_dir}")
    print(f"python {sys.version.split()[0]} at {sys.executable}")
    print(f"static scan: {n_files} files; runtime: {n_modules} modules imported; "
          f"wheels: {len(args.wheel)}")
    if problems:
        print(f"FAIL: {len(problems)} zero-dependency violation(s):", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    print("OK: no runtime dependencies; every import is stdlib or tokenbill")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
