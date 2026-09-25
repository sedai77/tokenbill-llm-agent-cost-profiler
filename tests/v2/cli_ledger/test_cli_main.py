"""``cli.main``: the lazy command table, global flags, exit codes, logging, plugins, extension
aliases and ``demo --fleet`` dispatch (SPEC §15; Copilot addendum §15, §21.4)."""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from tokenbill import __version__, cli
from tokenbill.core import registry
from tokenbill.core.errors import (
    ContractViolation,
    GateFailed,
    PricingError,
    PrivacyError,
    SourceError,
    UsageError,
)

from .helpers import REPO, run_main, run_subprocess

SPEC_VERBS = {"demo", "analyze", "scan", "me", "init", "collect", "ingest", "bill", "reconcile",
              "calibrate", "findings", "whatif", "policy", "measure", "ab", "receipt", "export",
              "showback", "report", "pricing", "check", "purge"}
LEDGER_VERBS = {"demo", "analyze", "init", "collect", "ingest", "bill", "reconcile", "export",
                "showback", "pricing", "purge"}


# --- the command table --------------------------------------------------------------------------


def test_command_table_lists_every_spec_verb_by_dotted_path() -> None:
    assert set(cli.BUILTIN_COMMANDS) == SPEC_VERBS
    for verb, module in cli.BUILTIN_COMMANDS.items():
        assert module == f"tokenbill.commands.{verb}"
        assert verb in cli.COMMAND_HELP
    for verb in LEDGER_VERBS:
        mod = importlib.import_module(cli.BUILTIN_COMMANDS[verb])
        assert callable(mod.add_parser) and callable(mod.run)


def test_commands_merges_extension_verbs(monkeypatch: pytest.MonkeyPatch) -> None:
    from tokenbill.core import extensions

    monkeypatch.setattr(cli, "_COMMANDS_CACHE", None)
    monkeypatch.setattr(extensions, "command_modules",
                        lambda notes=None: {"copilot": "tbtest.copilot", "demo": "evil.demo"})
    table = cli.COMMANDS
    assert table["copilot"] == "tbtest.copilot"
    assert table["demo"] == "tokenbill.commands.demo"  # built-ins are never shadowed
    from tokenbill.cli import COMMANDS  # from-import works through the module __getattr__

    assert COMMANDS is table
    with pytest.raises(AttributeError):
        cli.NOT_A_THING  # noqa: B018


def test_version_is_instant_and_imports_no_command() -> None:
    code = ("import sys, tokenbill.cli as c; rc = c.main(['--version']); "
            "print(sorted(m for m in sys.modules if m.startswith(('tokenbill.commands.', "
            "'tokenbill.core.extensions', 'tokenbill.pipeline'))))")
    proc = __import__("subprocess").run([sys.executable, "-c", code], capture_output=True,
                                        cwd=REPO, timeout=120, check=False)
    assert proc.stdout.decode().strip().splitlines() == [f"tokenbill {__version__}", "[]"]


def test_version_subprocess_and_in_process() -> None:
    assert run_main(["--version"])[1].strip() == f"tokenbill {__version__}"
    proc = run_subprocess(["--version"])
    assert proc.returncode == 0 and proc.stdout.decode().strip() == f"tokenbill {__version__}"


def test_help_lists_every_verb_without_importing_them() -> None:
    for name in list(sys.modules):
        if name.startswith("tokenbill.commands.") and name.rsplit(".", 1)[1] in ("bill", "purge"):
            sys.modules.pop(name)
    code, out, _ = run_main(["--help"])
    assert code == 0
    for verb in SPEC_VERBS:
        assert f"    {verb} " in out or f"    {verb}\n" in out, verb
    assert "exit codes" in out
    assert "tokenbill.commands.purge" not in sys.modules


@pytest.mark.parametrize("verb", sorted(LEDGER_VERBS))
def test_every_ledger_verb_has_help(verb: str) -> None:
    code, out, err = run_main([verb, "--help"])
    assert code == 0 and err == ""
    assert f"usage: tokenbill {verb}" in out
    assert "--strict-dq" in out  # the global flags are accepted after the verb


@pytest.mark.parametrize("argv", [["collect", "claude-code", "--help"],
                                  ["collect", "claude-code-headless", "--help"],
                                  ["pricing", "show", "--help"], ["pricing", "verify", "--help"],
                                  ["pricing", "diff", "--help"],
                                  ["pricing", "emit-model-pricing", "--help"]])
def test_nested_verbs_have_help(argv: list[str]) -> None:
    code, out, _ = run_main(argv)
    assert code == 0 and "usage: tokenbill" in out and "--quiet" in out


def test_usage_errors_exit_2() -> None:
    assert run_main([])[0] == 2
    assert run_main(["frobnicate"])[0] == 2
    assert run_main(["bill", "--bogus"])[0] == 2
    assert run_main(["--jobs", "0", "bill"])[0] == 2
    code, _out, err = run_main(["bill"])
    assert code == 2 and "--db PATH is required" in err


def test_a_missing_command_module_fails_only_its_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    table = dict(cli.BUILTIN_COMMANDS, findings="tokenbill.commands.not_installed_xyz")
    monkeypatch.setattr(cli, "BUILTIN_COMMANDS", table)
    code, _out, err = run_main(["findings", "--db", "x"])
    assert code == 1
    assert "not available in this installation" in err
    assert run_main(["--version"])[0] == 0
    assert run_main(["demo", "--scenario", "well-behaved"])[0] == 0


def test_global_flags_before_and_after_the_verb(tmp_path: Path) -> None:
    code, _out, err = run_main(["--db", str(tmp_path / "none.db"), "bill"])
    assert code == 2 and "not found" in err  # --db before the verb reaches the command
    code, _out, err = run_main(["bill", "--db", str(tmp_path / "none.db")])
    assert code == 2 and "not found" in err


def test_format_is_validated_per_command() -> None:
    code, _out, err = run_main(["--format", "xml", "pricing", "show"])
    assert code == 2 and "--format must be one of" in err
    code, _out, err = run_main(["--format", "json", "demo"])
    assert code == 2


# --- exit-code mapping ---------------------------------------------------------------------------


@pytest.mark.parametrize(("exc", "code"), [
    (UsageError("bad flag"), 2), (PrivacyError("person"), 2), (GateFailed("gate"), 3),
    (PricingError("rates"), 1), (SourceError("file"), 1), (ContractViolation("bug"), 1),
    (OSError("disk"), 1),
])
def test_errors_map_to_spec_exit_codes(monkeypatch: pytest.MonkeyPatch, exc: Exception,
                                       code: int) -> None:
    from tokenbill.commands import purge

    def boom(args: argparse.Namespace) -> int:
        raise exc

    monkeypatch.setattr(purge, "run", boom)
    got, out, err = run_main(["purge", "--db", "x", "--before", "2026-01-01", "--yes"])
    assert got == code == cli.exit_code_for(exc)
    assert err.startswith("tokenbill: error:") and len(err.strip().splitlines()) == 1
    assert out == ""


def test_unexpected_errors_are_one_line_and_debuggable(monkeypatch: pytest.MonkeyPatch) -> None:
    from tokenbill.commands import purge

    def boom(args: argparse.Namespace) -> int:
        raise ZeroDivisionError("x")

    monkeypatch.setattr(purge, "run", boom)
    code, _out, err = run_main(["purge", "--db", "x"])
    assert code == 1 and "internal error (ZeroDivisionError)" in err
    assert "Traceback" not in err
    code, _out, err = run_main(["-vv", "purge", "--db", "x"])
    assert code == 1 and "Traceback" in err  # -vv logs the traceback


def test_keyboard_interrupt_exits_130(monkeypatch: pytest.MonkeyPatch) -> None:
    from tokenbill.commands import purge

    def interrupted(args: argparse.Namespace) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(purge, "run", interrupted)
    assert run_main(["purge"])[0] == 130


def test_broken_pipe_is_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    from tokenbill.commands import purge

    def pipe(args: argparse.Namespace) -> int:
        raise BrokenPipeError

    monkeypatch.setattr(purge, "run", pipe)
    code, _out, err = run_main(["purge"])
    assert code == 1 and err == ""


def test_broken_pipe_subprocess() -> None:
    proc = __import__("subprocess").run(
        f"{sys.executable} -m tokenbill pricing show --format json | head -c 10", shell=True,
        capture_output=True, cwd=REPO, timeout=120, check=False)
    assert b"Traceback" not in proc.stderr and b"Broken pipe" not in proc.stderr


# --- logging -------------------------------------------------------------------------------------


def test_log_json_writes_json_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    from tokenbill.commands import purge

    def logs(args: argparse.Namespace) -> int:
        import logging

        logging.getLogger("tokenbill.test").warning("hello %s", "world")
        return 0

    monkeypatch.setattr(purge, "run", logs)
    code, _out, err = run_main(["--log-json", "--deterministic", "purge"])
    assert code == 0
    doc = json.loads(err.strip().splitlines()[-1])
    assert doc == {"level": "warning", "logger": "tokenbill.test", "message": "hello world"}
    code, _out, err = run_main(["--log-json", "purge"])
    assert "ts_ms" in json.loads(err.strip().splitlines()[-1])
    code, _out, err = run_main(["--quiet", "purge"])
    assert err == ""  # --quiet: warnings are not logged
    code, _out, err = run_main(["purge"])
    assert err.strip() == "WARNING tokenbill.test: hello world"


# --- plugins -------------------------------------------------------------------------------------


class _EntryPoint:
    def __init__(self, name: str, obj: Any) -> None:
        self.name = name
        self._obj = obj

    def load(self) -> Any:
        return self._obj


class _AcmeAdapter:
    """A third-party adapter shipped as an entry point."""

    name = "acme-usage"
    capabilities = frozenset({"usage_sequence"})

    def sniff(self, path: Path, head: bytes) -> bool:
        return False

    def read(self, path: Path, opts: Any) -> Any:
        from tokenbill.core.types import IngestResult, SourceInfo

        src = SourceInfo(source_id="s_acme", adapter=self.name, name_hmac="", sha256="0" * 64,
                         bytes=0, name_key_id=None, principal_key_id=None)
        return IngestResult(source=src, requests=[], sessions=[], events=[], aggregates=[],
                            cost_lines=[], outcomes=[], quarantined=[], notes=[], stats={},
                            capabilities=frozenset())


def test_plugins_are_loaded_only_with_the_flag(monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path) -> None:
    monkeypatch.setattr(registry, "_PLUGIN_ADAPTERS", {})
    monkeypatch.setattr(registry, "_entry_points",
                        lambda group: [_EntryPoint("acme-usage", _AcmeAdapter)]
                        if group == "tokenbill.adapters" else [])
    src = tmp_path / "usage.json"
    src.write_text("{}", encoding="utf-8")
    db = str(tmp_path / "l.db")
    code, _out, err = run_main(["ingest", "--db", db, "--adapter", "acme-usage", str(src)])
    assert code == 2 and "unknown adapter" in err
    code, out, err = run_main(["--plugins", "ingest", "--db", db, "--adapter", "acme-usage",
                               str(src), "--format", "json"])
    assert code == 0, err
    assert json.loads(out)["inputs"][0]["adapter"] == "acme-usage"


# --- extension aliases (rewrite_argv) -------------------------------------------------------------


def _fake_copilot(monkeypatch: pytest.MonkeyPatch) -> list[argparse.Namespace]:
    seen: list[argparse.Namespace] = []
    mod = types.ModuleType("tbtest_copilot_cmd")

    def add_parser(subparsers: Any) -> argparse.ArgumentParser:
        p = subparsers.add_parser("copilot", help="Copilot")
        sub = p.add_subparsers(dest="action", required=True)
        scan = sub.add_parser("scan")
        scan.add_argument("--github-org")
        coll = sub.add_parser("collect")
        coll.add_argument("--source", required=True)
        coll.add_argument("--out")
        return p

    def run(args: argparse.Namespace) -> int:
        seen.append(args)
        return 0

    mod.add_parser = add_parser  # type: ignore[attr-defined]
    mod.run = run  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tbtest_copilot_cmd", mod)
    spec = registry.EXTENSIONS["copilot"]
    monkeypatch.setitem(registry.EXTENSIONS, "copilot",
                        dataclasses.replace(spec, command_module="tbtest_copilot_cmd"))
    monkeypatch.setattr(cli, "_COMMANDS_CACHE", None)
    return seen


def test_aliases_are_rewritten_before_argparse(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_copilot(monkeypatch)
    assert run_main(["scan", "--copilot", "--github-org", "acme"])[0] == 0
    assert (seen[-1].command, seen[-1].action, seen[-1].github_org) == ("copilot", "scan", "acme")
    assert run_main(["--quiet", "collect", "copilot-cli", "--out", "D"])[0] == 0
    assert (seen[-1].action, seen[-1].source, seen[-1].out) == ("collect", "cli", "D")
    assert run_main(["collect", "copilot-vscode", "--out", "D"])[0] == 0
    assert seen[-1].source == "vscode"
    code, out, _ = run_main(["--help"])
    assert code == 0 and "copilot" in out
    assert run_main(["copilot", "scan"])[0] == 0


def test_an_uninstalled_extension_verb_is_a_usage_error() -> None:
    code, _out, err = run_main(["scan", "--copilot"])
    assert code in (1, 2)  # copilot's command module is not installed on this tree
    assert "Traceback" not in err


# --- demo --fleet ----------------------------------------------------------------------------------


def _result() -> Any:
    from tokenbill.core.records import ContentTier
    from tokenbill.core.types import PrivacyInfo, RunResult

    return RunResult(command="demo", window=(0, 86_400_000), inputs=(),
                     privacy=PrivacyInfo(content_tier=ContentTier.NONE, key_id=None,
                                         identity_mode="install", k=5, suppressed_groups=0),
                     rate_card=None, synthetic=True)


def test_demo_fleet_without_the_savings_pipeline_fails_cleanly(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "tokenbill.pipeline.savings", None)
    code, _out, err = run_main(["demo", "--fleet"])
    assert code == 1 and "pipeline.savings" in err


def test_demo_fleet_dispatches_lazily(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict] = []
    fake = types.ModuleType("tokenbill.pipeline.savings")

    def run_demo_fleet(*, seed: int, out_dir: Path | None = None) -> Any:
        calls.append({"seed": seed, "out_dir": out_dir})
        return _result()

    fake.run_demo_fleet = run_demo_fleet  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tokenbill.pipeline.savings", fake)
    report = tmp_path / "fleet.html"
    code, out, _ = run_main(["demo", "--fleet", "--seed", "11", "-o", str(report),
                             "--out-dir", str(tmp_path / "src")])
    assert code == 0 and calls == [{"seed": 11, "out_dir": tmp_path / "src"}]
    assert "SYNTHETIC" in out and f"Report written to {report}" in out
    assert report.read_text(encoding="utf-8").startswith("<!DOCTYPE html>")
    code, out, _ = run_main(["demo", "--fleet", "--format", "json", "--deterministic"])
    assert code == 0 and json.loads(out)["synthetic"] is True


def test_demo_flag_conflicts() -> None:
    assert run_main(["demo", "--fleet", "--scenario", "timestamp"])[0] == 2
    assert run_main(["demo", "--out-dir", "x"])[0] == 2
    assert run_main(["demo", "--fleet", "-o", "missing_dir/x.html"])[0] in (1, 2)


# --- strict data quality -------------------------------------------------------------------------


def test_strict_dq_code() -> None:
    from tokenbill.config import Config

    args = argparse.Namespace(strict_dq=True)
    assert cli.strict_dq_code(args, 0, None) == 0
    assert cli.strict_dq_code(args, 1, None) == 4
    assert cli.strict_dq_code(args, 2, Config(strict_dq_threshold=3)) == 0
    assert cli.strict_dq_code(argparse.Namespace(), 9, None) == 0
