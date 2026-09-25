"""Token Bill command line (SPEC §15, §15.1, §16; package CLI-LEDGER).

``main`` dispatches through the static command table ``COMMANDS`` (verb → command module, SPEC
§15), merged with the channel extensions' verbs (``core.extensions.command_modules()``, e.g.
``copilot``). Only the module of the verb being run is imported (``add_parser(subparsers)`` then
``run(args) -> int``), so ``tokenbill --version`` and usage errors stay instant and a missing
module fails only its own verb. Extension aliases (``scan --copilot`` → ``copilot scan``, …) are
rewritten with ``core.extensions.rewrite_argv`` before argparse.

``demo`` and ``analyze`` keep their v0.1 handlers below (``_model_price``, ``_profile_and_render``,
``_cmd_demo``, ``_cmd_analyze``): their output on v0.1-safe inputs is byte-identical to 0.1.2
(``tests/v2/golden/``). ``analyze`` routes to the v2 engine only on v1-unsafe inputs (§15.1).

Global flags (accepted before or after the verb): ``--config PATH``, ``--db PATH``, ``--format``,
``--deterministic``, ``--quiet``, ``-v``, ``--log-json``, ``--plugins``, ``--strict-dq``,
``--jobs N``. Every command is offline unless ``--live``.

Exit codes: 0 success; 1 runtime failure (unreadable input, missing module, internal error); 2
usage error (argparse, ``UsageError``, ``PrivacyError`` refusals); 3 a gate failed
(``GateFailed``: reconcile, export gates, pricing verify, …); 4 completed with data-quality
warnings at or above the threshold under ``--strict-dq``.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import math
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tokenbill import __version__
from tokenbill.common import TokenbillError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from tokenbill.config import Config
    from tokenbill.core.types import RunResult
    from tokenbill.pipeline.common import Env
    from tokenbill.trace import Run

logger = logging.getLogger("tokenbill.cli")

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_GATE = 3
EXIT_DQ = 4

#: The static command table (SPEC §15): every verb of both CLI packages → its command module
#: (``add_parser(subparsers)``, ``run(args) -> int``), imported only when its verb runs.
#: ``COMMANDS`` (module attribute, computed on first access) adds the extension verbs.
BUILTIN_COMMANDS: dict[str, str] = {
    "demo": "tokenbill.commands.demo",
    "analyze": "tokenbill.commands.analyze",
    "scan": "tokenbill.commands.scan",
    "me": "tokenbill.commands.me",
    "init": "tokenbill.commands.init",
    "collect": "tokenbill.commands.collect",
    "ingest": "tokenbill.commands.ingest",
    "bill": "tokenbill.commands.bill",
    "reconcile": "tokenbill.commands.reconcile",
    "calibrate": "tokenbill.commands.calibrate",
    "findings": "tokenbill.commands.findings",
    "whatif": "tokenbill.commands.whatif",
    "policy": "tokenbill.commands.policy",
    "measure": "tokenbill.commands.measure",
    "ab": "tokenbill.commands.ab",
    "receipt": "tokenbill.commands.receipt",
    "export": "tokenbill.commands.export",
    "showback": "tokenbill.commands.showback",
    "report": "tokenbill.commands.report",
    "pricing": "tokenbill.commands.pricing",
    "check": "tokenbill.commands.check",
    "purge": "tokenbill.commands.purge",
}

#: One-line help per verb (``tokenbill --help`` lists every verb without importing any module).
COMMAND_HELP: dict[str, str] = {
    "demo": "profile the bundled synthetic demo scenarios (deterministic, no API keys)",
    "analyze": "profile recorded trace JSONL files (see tokenbill.instrument)",
    "scan": "one-shot local Claude Code review of your own usage (self view)",
    "me": "your own usage (alias of scan --self)",
    "init": "create the config, key files and store directory; print the privacy notice",
    "collect": "on-device / CI collectors: content-free trace@2 usage files",
    "ingest": "normalize sources (transcripts, traces, OTel, admin pages, CUR, …) into the ledger",
    "bill": "exact priced totals, with allowance and estimated figures beside them",
    "reconcile": "the ledger gate: reconcile the ledger against provider usage and invoices",
    "calibrate": "the model gate: predictive calibration of the replay engine",
    "findings": "detectors ranked by Shapley-credited recoverable dollars",
    "whatif": "counterfactual replays of policies",
    "policy": "per-cohort policy packs and post-rollout effectiveness checks",
    "measure": "randomized rollout plans and realized-savings measurement",
    "ab": "paired lab comparison of two trace sets",
    "receipt": "create, sign and verify savings receipts",
    "export": "export the ledger: FOCUS 1.4 CSV, trace@2, ccusage JSON",
    "showback": "per-team showback pages (HTML, CSV, JSON)",
    "report": "HTML fleet / team / self report",
    "pricing": "show, verify and diff the pricing registry",
    "check": "CI gate over a smoke-test trace or a ledger",
    "purge": "erasure and retention: delete one principal's or old data (audited)",
}

_COMMANDS_CACHE: dict[str, str] | None = None


def command_table(*, notes: list[Any] | None = None) -> dict[str, str]:
    """``BUILTIN_COMMANDS`` plus the extension verbs whose module exists
    (``core.extensions.command_modules``); built-in verbs are never shadowed."""
    from tokenbill.core import extensions

    table = dict(BUILTIN_COMMANDS)
    for verb, module in extensions.command_modules(notes=notes).items():
        table.setdefault(verb, module)
    return table


def __getattr__(name: str) -> Any:
    """``COMMANDS`` is computed on first access (it needs ``core.extensions``, which
    ``--version`` must not import)."""
    global _COMMANDS_CACHE
    if name == "COMMANDS":
        if _COMMANDS_CACHE is None:
            _COMMANDS_CACHE = command_table()
        return _COMMANDS_CACHE
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


#: Ceiling for --model-price rates. $1e9 per MTok is already absurd; anything
#: near float max would overflow tokens*rate to inf and poison every total.
_MAX_PRICE_PER_MTOK = 1e9


def _model_price(spec: str) -> tuple[str, float, float]:
    """Parse a ``--model-price MODEL=IN,OUT`` override (dollars per MTok)."""
    problem = f"expected MODEL=IN,OUT (dollars per MTok, e.g. mymodel=3,15), got {spec!r}"
    model, sep, prices = spec.partition("=")
    model = model.strip()
    if not sep or not model:
        raise argparse.ArgumentTypeError(problem)
    parts = prices.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(problem)
    try:
        input_price, output_price = (float(part) for part in parts)
    except ValueError:
        raise argparse.ArgumentTypeError(problem) from None
    if not (math.isfinite(input_price) and math.isfinite(output_price)):
        raise argparse.ArgumentTypeError(problem)  # nan/inf would render "$nan" reports
    if input_price < 0 or output_price < 0:
        raise argparse.ArgumentTypeError(problem)
    if input_price > _MAX_PRICE_PER_MTOK or output_price > _MAX_PRICE_PER_MTOK:
        # A finite-but-huge rate passes isfinite yet overflows tokens*rate to
        # inf during pricing, which the renderers cannot chart honestly.
        raise argparse.ArgumentTypeError(
            f"price out of range in {spec!r}: rates above {_MAX_PRICE_PER_MTOK:g} "
            "dollars per MTok are not supported"
        )
    return model, input_price, output_price


# =================================================================================================
# parser
# =================================================================================================

#: Global flags that take a value (skipped when locating the verb in argv).
_VALUE_FLAGS = frozenset({"--config", "--db", "--format", "--jobs"})


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}")
    return value


def _add_global_flags(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """The SPEC §15 global flags; on subcommand parsers their defaults are suppressed so a value
    given before the verb is never clobbered."""

    def d(value: object) -> object:
        return argparse.SUPPRESS if suppress else value

    g = parser.add_argument_group("global options")
    g.add_argument("--config", type=Path, metavar="PATH", default=d(None),
                   help="config file (JSON; default ./.tokenbill/config.json, then "
                        "~/.config/tokenbill/config.json)")
    g.add_argument("--db", type=Path, metavar="PATH", default=d(None),
                   help="the ledger (SQLite store) file")
    g.add_argument("--format", dest="format", metavar="{text,json}", default=d(None),
                   help="output format (commands may accept more)")
    g.add_argument("--deterministic", action="store_true", default=d(False),
                   help="drop wall-clock fields from outputs (byte-identical reruns)")
    g.add_argument("--quiet", action="store_true", default=d(False),
                   help="print only the result (no hints; errors only in logs)")
    g.add_argument("-v", "--verbose", action="count", default=d(0),
                   help="more logging (-v info, -vv debug)")
    g.add_argument("--log-json", action="store_true", default=d(False),
                   help="JSON log lines on stderr")
    g.add_argument("--plugins", action="store_true", default=d(False),
                   help="load third-party adapter/detector entry points")
    g.add_argument("--strict-dq", action="store_true", default=d(False),
                   help="exit 4 when data-quality warnings reach the configured threshold")
    g.add_argument("--jobs", type=_positive_int, metavar="N", default=d(None),
                   help="worker processes over shards (default 1; results identical for any N)")


class _Subparsers:
    """``subparsers`` handed to command modules: every ``add_parser`` gets the global flags (on a
    fresh parent parser; the command's own definitions win, ``conflict_handler="resolve"``)."""

    def __init__(self, action: Any) -> None:
        self._action = action

    def add_parser(self, name: str, **kwargs: Any) -> argparse.ArgumentParser:
        common = argparse.ArgumentParser(add_help=False)
        _add_global_flags(common, suppress=True)
        kwargs["parents"] = [*kwargs.get("parents", ()), common]
        kwargs.setdefault("conflict_handler", "resolve")
        if "help" in kwargs:
            kwargs.setdefault("description", kwargs["help"])
        return self._action.add_parser(name, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._action, name)


def add_command_flags(parser: argparse.ArgumentParser) -> None:
    """Add the global flags (suppressed defaults) to a nested sub-command parser (e.g.
    ``collect claude-code``, ``pricing show``), so they are accepted after the nested verb too."""
    _add_global_flags(parser, suppress=True)


def build_parser(verb: str | None = None, *, table: dict[str, str] | None = None
                 ) -> argparse.ArgumentParser:
    """The ``tokenbill`` parser. Only *verb*'s command module is imported (its ``add_parser``);
    every other verb of *table* (default: :data:`BUILTIN_COMMANDS`) is a lightweight placeholder
    that only lists it in ``--help``."""
    parser = argparse.ArgumentParser(
        prog="tokenbill",
        allow_abbrev=False,
        description=(
            "Profile LLM agent traces: token waterfalls from real billed usage, "
            "re-sent-prefix redundancy, prompt-cache simulation, and cache-breaker "
            "detection with dollar-valued fixes. v0.2 adds the fleet ledger: collection, "
            "exact bills, reconciliation, findings, policies, verification and exports."
        ),
        epilog="exit codes: 0 ok, 1 failure, 2 usage error, 3 gate failed, "
               "4 data-quality warnings (--strict-dq)",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _add_global_flags(parser, suppress=False)
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="command")
    proxy = _Subparsers(subparsers)
    for name, module_name in (table if table is not None else BUILTIN_COMMANDS).items():
        if name == verb:
            module = importlib.import_module(module_name)
            module.add_parser(proxy)
        else:
            subparsers.add_parser(name, help=COMMAND_HELP.get(name, f"{name} commands"),
                                  add_help=False)
    return parser


def _verb_index(argv: Sequence[str]) -> int | None:
    """Position of the verb: the first token that is not a global option or its value."""
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == "--":
            return i + 1 if i + 1 < len(argv) else None
        if token.startswith("-"):
            if token in _VALUE_FLAGS:
                i += 2
                continue
            i += 1
            continue
        return i
    return None


# =================================================================================================
# helpers for command modules
# =================================================================================================


def output_format(args: argparse.Namespace, allowed: Sequence[str], default: str) -> str:
    """The ``--format`` of this run (global or the command's own), validated against *allowed*
    (``UsageError`` otherwise)."""
    from tokenbill.core.errors import UsageError

    fmt = getattr(args, "format", None) or default
    if fmt not in allowed:
        raise UsageError(f"--format must be one of {', '.join(allowed)} for this command")
    return fmt


def load_config_for(args: argparse.Namespace, **overrides: object) -> Config:
    """The resolved Config: CLI overrides (``--jobs`` plus *overrides*) > ``TOKENBILL_*`` >
    ``--config`` file (or the default locations) > defaults."""
    from tokenbill.config import load_config

    ov: dict[str, object] = {"jobs": getattr(args, "jobs", None)}
    ov.update(overrides)
    return load_config(getattr(args, "config", None), os.environ, ov)


def env_for(args: argparse.Namespace, *, config: Config | None = None,
            rates: Sequence[Path] = (), contract: Path | None = None,
            model_prices: Sequence[tuple[str, str, str]] = (), key_file: Path | None = None,
            collection_key_file: Path | None = None, **overrides: object) -> Env:
    """``pipeline.common.build_env`` for this run (the real RATES rate card, keys from the flags
    or the Config)."""
    from tokenbill.pipeline.common import build_env

    cfg = config if config is not None else load_config_for(args, **overrides)
    return build_env(cfg, rates=tuple(rates), contract=contract, model_prices=model_prices,
                     key_file=key_file, collection_key_file=collection_key_file)


def require_db(args: argparse.Namespace) -> Path:
    """The ``--db`` path (``UsageError`` when missing)."""
    from tokenbill.core.errors import UsageError

    db = getattr(args, "db", None)
    if db is None:
        raise UsageError("--db PATH is required (the ledger file)")
    return Path(db)


def dq_warnings(result: RunResult) -> int:
    """Data-quality notes of severity warn or error in *result*."""
    return sum(1 for n in result.data_quality if n.severity in ("warn", "error"))


def strict_dq_code(args: argparse.Namespace, warnings: int, config: Config | None) -> int:
    """``EXIT_DQ`` under ``--strict-dq`` when *warnings* reach the Config's
    ``strict_dq_threshold`` (default 1), else ``EXIT_OK``."""
    if not getattr(args, "strict_dq", False):
        return EXIT_OK
    threshold = config.strict_dq_threshold if config is not None else 1
    if warnings >= max(threshold, 1):
        print(f"tokenbill: {warnings} data-quality warning(s) under --strict-dq "
              f"(threshold {max(threshold, 1)})", file=sys.stderr)
        return EXIT_DQ
    return EXIT_OK


def emit_result(args: argparse.Namespace, result: RunResult, *, config: Config | None,
                allowed: Sequence[str] = ("text", "json"), default: str = "text") -> int:
    """Render *result* on stdout (``--format text`` → ``outputs.terminal``, ``json`` →
    ``tokenbill/result@2`` with ``--deterministic`` honored) and return the strict-DQ exit code."""
    fmt = output_format(args, allowed, default)
    if fmt == "json":
        from tokenbill.outputs.result_json import dumps_result

        text = dumps_result(result, deterministic=bool(getattr(args, "deterministic", False)))
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    else:
        from tokenbill.outputs.terminal import render_terminal

        print(render_terminal(result))
    return strict_dq_code(args, dq_warnings(result), config)


def note(args: argparse.Namespace, text: str) -> None:
    """A human hint on stderr (suppressed by ``--quiet``)."""
    if not getattr(args, "quiet", False):
        print(text, file=sys.stderr)


# =================================================================================================
# demo / analyze (v0.1 handlers; byte-identical on v0.1-safe inputs)
# =================================================================================================


def _profile_and_render(
    runs: Sequence[Run], *, trace_name: str, synthetic: bool, output: Path | None
) -> int:
    """The shared demo/analyze pipeline: profile, detect, simulate, render."""
    if output is not None and not output.parent.exists():
        # Fail before the (potentially long) analysis, not after the full
        # summary has scrolled by with the report silently unwritten.
        print(
            f"tokenbill: error: output directory '{output.parent}' does not exist",
            file=sys.stderr,
        )
        return 1
    from tokenbill.analyzer import profile_run
    from tokenbill.breakers import detect, repaired_calls
    from tokenbill.report import render_report, render_text_summary
    from tokenbill.simulator import simulate

    profiles = []
    scenarios = {}
    breakers = {}
    for run in runs:
        profiles.append(profile_run(run))
        found = list(detect(run))
        breakers[run.run_id] = found
        fixed = list(repaired_calls(run, found)) if found else None
        scenarios[run.run_id] = list(simulate(run, fixed_calls=fixed))

    meta = {"trace": trace_name, "date": date.today().isoformat(), "synthetic": synthetic}
    print(render_text_summary(profiles, scenarios, breakers, meta=meta))
    if output is not None:
        # errors="backslashreplace": a trace is untrusted input, and one exotic
        # character must not discard a finished report at the final write.
        output.write_text(
            render_report(profiles, scenarios, breakers, meta),
            encoding="utf-8",
            errors="backslashreplace",
        )
        print(f"Report written to {output}")
    return 0


def _cmd_demo(args: argparse.Namespace) -> int:
    from tokenbill.demo_traces import all_scenarios
    from tokenbill.trace import Run

    scenarios = all_scenarios(args.seed)
    if args.scenario is not None:
        if args.scenario not in scenarios:
            names = ", ".join(sorted(scenarios))
            print(
                f"tokenbill demo: error: unknown scenario {args.scenario!r} "
                f"(choose from: {names})",
                file=sys.stderr,
            )
            return 2
        scenarios = {args.scenario: scenarios[args.scenario]}
    runs = [Run(run_id=calls[0].run_id, calls=tuple(calls)) for calls in scenarios.values()]
    if args.scenario is None:
        trace_name = f"bundled demo scenarios (seed {args.seed})"
    else:
        trace_name = f"bundled demo scenario {args.scenario!r} (seed {args.seed})"
    code = _profile_and_render(
        runs, trace_name=trace_name, synthetic=True, output=args.output
    )
    if code == 0:
        if args.output is None:
            print(
                "next: tokenbill demo -o report.html writes the full HTML report; "
                'then record a real agent — see "Your first real trace" in the README'
            )
        else:
            print(
                "next: record a real agent and analyze its trace — "
                'see "Your first real trace" in the README'
            )
    return code


def _apply_model_prices(model_prices: Sequence[tuple[str, float, float]]) -> None:
    """Step 1 of SPEC §15.1: ``--model-price`` into the v0.1 ``PRICING`` table, as 0.1.2 did."""
    if model_prices:
        from tokenbill.pricing import PRICING, ModelPricing

        for model, input_price, output_price in model_prices:
            PRICING[model] = ModelPricing(
                input_per_mtok=input_price, output_per_mtok=output_price
            )
            logger.info(
                "priced %s at $%.2f in / $%.2f out per MTok", model, input_price, output_price
            )


def _cmd_analyze(args: argparse.Namespace) -> int:
    """The v0.1 analyze pipeline (``--engine v1``): apply ``--model-price``, strict
    ``read_trace``, namespace colliding run ids (SPEC §15.1), render."""
    from tokenbill.trace import read_trace

    _apply_model_prices(args.model_price)
    per_file = [(path, read_trace(path)) for path in args.traces]
    runs = namespace_runs(per_file)
    if not runs:
        print("tokenbill analyze: error: no runs found in the given traces", file=sys.stderr)
        return 1
    trace_name = ", ".join(path.name for path in args.traces)
    return _profile_and_render(
        runs, trace_name=trace_name, synthetic=False, output=args.output
    )


def namespace_runs(per_file: Sequence[tuple[Path, Sequence[Run]]]) -> list[Run]:
    """Concatenate the runs of every file; a ``run_id`` that appears in more than one file is
    renamed ``<file name>:<run_id>`` on the ``Run`` and its ``Call`` s (SPEC §15.1, D2 (a)), so
    totals add instead of overwriting. Files sharing a name fall back to the path as given."""
    import dataclasses

    seen: dict[str, set[int]] = {}
    for i, (_path, runs) in enumerate(per_file):
        for run in runs:
            seen.setdefault(run.run_id, set()).add(i)
    names = [p.name for p, _r in per_file]
    out: list[Run] = []
    for path, runs in per_file:
        label = path.name if names.count(path.name) == 1 else str(path)
        for run in runs:
            if len(seen[run.run_id]) > 1:
                new_id = f"{label}:{run.run_id}"
                calls = tuple(dataclasses.replace(c, run_id=new_id) for c in run.calls)
                out.append(dataclasses.replace(run, run_id=new_id, calls=calls))
            else:
                out.append(run)
    return out


# =================================================================================================
# logging and error handling
# =================================================================================================


class _StderrHandler(logging.Handler):
    """Writes to the *current* ``sys.stderr`` (tests swap it)."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            sys.stderr.write(self.format(record) + "\n")
        except Exception:  # pragma: no cover - logging must never raise
            self.handleError(record)


class _JsonFormatter(logging.Formatter):
    def __init__(self, deterministic: bool) -> None:
        super().__init__()
        self.deterministic = deterministic

    def format(self, record: logging.LogRecord) -> str:
        doc: dict[str, object] = {"level": record.levelname.lower(), "logger": record.name,
                                  "message": record.getMessage()}
        if not self.deterministic:
            doc["ts_ms"] = int(record.created * 1000)
        return json.dumps(doc, sort_keys=True, ensure_ascii=True)


_HANDLER: _StderrHandler | None = None


def _configure_logging(args: argparse.Namespace) -> None:
    global _HANDLER
    root = logging.getLogger()
    verbose = int(getattr(args, "verbose", 0) or 0)
    if getattr(args, "quiet", False):
        level = logging.ERROR
    elif verbose >= 2:
        level = logging.DEBUG
    elif verbose == 1:
        level = logging.INFO
    else:
        level = logging.WARNING
    if _HANDLER is None:
        _HANDLER = _StderrHandler()
    if getattr(args, "log_json", False):
        _HANDLER.setFormatter(_JsonFormatter(bool(getattr(args, "deterministic", False))))
    else:
        _HANDLER.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    _HANDLER.setLevel(level)
    if _HANDLER not in root.handlers:
        root.addHandler(_HANDLER)
    if root.level == logging.NOTSET or root.level > level:
        root.setLevel(level)


def _tolerant_output_streams() -> None:
    """Make stdout/stderr total functions of their input.

    On a non-UTF-8 console (legacy Windows cp1252, PYTHONIOENCODING set) a
    CJK/emoji run_id — perfectly valid trace input — would otherwise crash
    the summary print with UnicodeEncodeError after all analysis succeeded.
    Unencodable characters degrade to backslash escapes instead.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (OSError, ValueError):  # exotic stream; keep going
                pass


def exit_code_for(exc: BaseException) -> int:
    """The SPEC §15 exit code of an expected failure: ``GateFailed`` 3; ``UsageError`` and
    ``PrivacyError`` 2; any other ``TokenbillError`` or ``OSError`` 1."""
    from tokenbill.core.errors import GateFailed, PrivacyError, UsageError

    if isinstance(exc, GateFailed):
        return EXIT_GATE
    if isinstance(exc, (UsageError, PrivacyError)):
        return EXIT_USAGE
    return EXIT_FAILURE


def _prepare_argv(argv: list[str]) -> tuple[list[str], str | None]:
    """Rewrite extension aliases at the verb (``core.extensions.rewrite_argv``) and return the new
    argv and its verb (None when there is none, e.g. ``--version``)."""
    at = _verb_index(argv)
    if at is None:
        return argv, None
    from tokenbill.core import extensions

    argv = [*argv[:at], *extensions.rewrite_argv(argv[at:])]
    return argv, argv[at] if at < len(argv) else None


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; returns the process exit code."""
    _tolerant_output_streams()
    started = time.monotonic()
    tokens = list(sys.argv[1:] if argv is None else argv)
    try:
        tokens, verb = _prepare_argv(tokens)
    except TokenbillError as exc:  # a malformed extension table: fail cleanly
        print(f"tokenbill: error: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    table = BUILTIN_COMMANDS
    if verb is not None and verb not in BUILTIN_COMMANDS or verb is None and _wants_help(tokens):
        table = __getattr__("COMMANDS")
    if verb is not None and verb in table:
        try:
            parser = build_parser(verb, table=table)
        except ImportError as exc:
            print(f"tokenbill: error: command {verb!r} is not available in this installation "
                  f"({type(exc).__name__}: {table[verb]})", file=sys.stderr)
            return EXIT_FAILURE
    else:
        parser = build_parser(None, table=table)
    try:
        args = parser.parse_args(tokens)
    except SystemExit as exc:  # argparse exits itself for --version/--help/usage errors
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 2
    if args.command != verb:  # e.g. an ambiguous option consumed the detected verb
        try:
            args = build_parser(args.command, table=table).parse_args(tokens)
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 2
        except ImportError as exc:
            print(f"tokenbill: error: command {args.command!r} is not available in this "
                  f"installation ({type(exc).__name__})", file=sys.stderr)
            return EXIT_FAILURE
    _configure_logging(args)
    try:
        if getattr(args, "plugins", False):
            from tokenbill.core import registry

            loaded = registry.load_plugins(True)
            logger.info("plugins loaded: %s", ", ".join(loaded) or "(none)")
        module = importlib.import_module(table[args.command])
        code = module.run(args)
        logger.debug("%s finished in %.3f s", args.command, time.monotonic() - started)
        return code
    except BrokenPipeError:  # e.g. `tokenbill export … | head`: stop quietly
        _silence_stdout()
        return EXIT_FAILURE
    except (TokenbillError, OSError) as exc:
        print(f"tokenbill: error: {exc}", file=sys.stderr)
        return exit_code_for(exc)
    except KeyboardInterrupt:
        print("tokenbill: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # a bug: one clean line, the traceback only with -v
        logger.debug("unexpected error", exc_info=True)
        print(f"tokenbill: internal error ({type(exc).__name__}); re-run with -vv for details",
              file=sys.stderr)
        return EXIT_FAILURE


def _silence_stdout() -> None:
    """After a broken pipe, point stdout at the null device so the interpreter's final flush does
    not raise again."""
    try:
        fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(fd, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):  # not a real file descriptor (tests)
        pass


def _wants_help(tokens: Sequence[str]) -> bool:
    return any(t in ("-h", "--help") for t in tokens)


if __name__ == "__main__":  # pragma: no cover - exercised via python -m tokenbill
    raise SystemExit(main())
