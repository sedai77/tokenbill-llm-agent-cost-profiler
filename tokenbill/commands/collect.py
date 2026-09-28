"""``tokenbill collect claude-code | claude-code-headless`` (SPEC §5.3, §5.4, §5.12; CLI-LEDGER).

On-device and CI collectors: content-free, incremental, one trace@2 ``usage`` file per run, never a
socket. Identity per SPEC §5.4: ``central`` ships ``r_<opaque ref>`` (``--principal-ref``),
``two-stage`` a collection-key HMAC (``c_``). ``--content`` is fixed to ``none`` (anything else is
refused, exit 2). Team and attributes come from ``--team`` / ``--attr`` and
``OTEL_RESOURCE_ATTRIBUTES``, never from content. The headless collector reads the CI context
(``GITHUB_*``) from the environment. Extension aliases (``collect copilot-cli`` /
``copilot-vscode``) are rewritten to ``copilot collect`` before this parser runs.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

from tokenbill import cli
from tokenbill.core.errors import UsageError

DEFAULT_PROJECTS = Path("~/.claude/projects")
DEFAULT_STATE = Path("~/.config/tokenbill/cc-collector-state.json")


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--out", type=Path, required=True, metavar="DIR",
                   help="directory for the trace@2 file (created 0700)")
    p.add_argument("--principal-ref", default="none", metavar="env:VAR|mdm-file:PATH|none",
                   help="opaque employee/device id (never an email); default none")
    p.add_argument("--identity-mode", choices=("central", "two-stage"), default=None,
                   help="central (r_ refs; default) or two-stage (c_ HMAC with the collection "
                        "key); default from the config")
    p.add_argument("--collection-key-file", type=Path, metavar="PATH",
                   help="the fleet collection (name) key distributed by MDM")
    p.add_argument("--team", metavar="TEAM", help="team of this device / runner")
    p.add_argument("--attr", action="append", default=[], metavar="K=V",
                   help="attribution default (repeatable; also OTEL_RESOURCE_ATTRIBUTES)")
    p.add_argument("--content", default="none", metavar="none",
                   help="content tier: none only (collectors never ship content)")
    cli.add_command_flags(p)


def add_parser(subparsers: Any) -> argparse.ArgumentParser:
    """Register ``collect`` with its two sources."""
    p = subparsers.add_parser("collect", help=cli.COMMAND_HELP["collect"])
    sub = p.add_subparsers(dest="source", required=True, metavar="source")
    cc = sub.add_parser("claude-code", help="Claude Code transcripts → trace@2 usage file",
                        description="Incremental, content-free Claude Code collection.")
    cc.add_argument("--projects", type=Path, default=DEFAULT_PROJECTS, metavar="DIR",
                    help="Claude Code projects directory (default ~/.claude/projects)")
    cc.add_argument("--state", type=Path, default=DEFAULT_STATE, metavar="FILE",
                    help="collector state (private; default ~/.config/tokenbill/"
                         "cc-collector-state.json)")
    cc.add_argument("--since", metavar="DATE", help="ignore records before this date")
    cc.add_argument("--final", action="store_true",
                    help="emit in-flight groups too (a last collection at shutdown)")
    _common(cc)
    hl = sub.add_parser("claude-code-headless",
                        help="CI / headless / Agent SDK streams → trace@2 usage file",
                        description="claude-code-action execution files, stream-json / json "
                                    "outputs and Agent SDK logs.")
    hl.add_argument("--in", dest="inputs", type=Path, nargs="+", required=True, metavar="FILE",
                    help="execution_file / stream-json / json files")
    _common(hl)
    return p


def _options(args: argparse.Namespace, config: Any, *, since_ms: int | None,
             now_ms: int) -> tuple[Any, bytes | None, list[Any]]:
    from tokenbill.core import keys
    from tokenbill.pipeline.ledger import (
        attribution_from,
        ci_attribution,
        collector_options,
        parse_attr_pairs,
        parse_resource_attributes,
        resolve_principal_ref,
    )

    key_path = args.collection_key_file or (Path(config.collection_key_file)
                                            if config.collection_key_file else None)
    collection_key = keys.load(key_path) if key_path is not None else None
    values = parse_resource_attributes(os.environ.get("OTEL_RESOURCE_ATTRIBUTES"))
    values.update(parse_attr_pairs(args.attr))
    if args.team is not None:
        values.update(parse_attr_pairs([f"team={args.team}"]))
    attribution = attribution_from(values)
    notes: list[Any] = []
    if args.source == "claude-code-headless":
        attribution, notes = ci_attribution(os.environ, collection_key, attribution)
    opts = collector_options(identity_mode=args.identity_mode or config.identity_mode,
                             principal_ref=resolve_principal_ref(args.principal_ref, os.environ),
                             collection_key=collection_key, attribution=attribution,
                             now_ms=now_ms, since_ms=since_ms, content=args.content)
    return opts, collection_key, notes


def run(args: argparse.Namespace) -> int:
    """Collect, write the file, print a content-free summary (path, counts, notes)."""
    from tokenbill.pipeline.ledger import (
        date_start_ms,
        parse_date,
        run_collect,
        run_collect_headless,
    )

    fmt = cli.output_format(args, ("text", "json"), "text")
    config = cli.load_config_for(args)
    now_ms = time.time_ns() // 1_000_000
    since_ms = None
    if getattr(args, "since", None) is not None:
        since_ms = date_start_ms(parse_date(args.since, "--since"))
    opts, _key, extra = _options(args, config, since_ms=since_ms, now_ms=now_ms)
    if args.source == "claude-code":
        result = run_collect(args.projects, args.out, opts, state_path=args.state,
                             now_ms=now_ms, final=args.final)
    elif args.source == "claude-code-headless":
        result = run_collect_headless(args.inputs, args.out, opts, now_ms=now_ms)
    else:  # pragma: no cover - argparse restricts the choices
        raise UsageError(f"unknown collect source {args.source!r}")
    notes = [*extra, *result.notes]
    if fmt == "json":
        import json

        doc = {"schema": "tokenbill/collect@1", "source": args.source,
               "file": str(result.path) if result.path else None,
               "requests": result.requests, "sessions": result.sessions,
               "identity_mode": opts.identity_mode, "evidence": "exact",
               "data_quality": [{"code": n.code, "severity": n.severity, "count": n.count,
                                 "detail": n.detail, "evidence": "exact"} for n in notes]}
        print(json.dumps(doc, sort_keys=True, indent=2))
    else:
        if result.path is None:
            print(f"collect {args.source}: nothing new since the last run")
        else:
            print(f"collect {args.source}: {result.requests:,} requests in "
                  f"{result.sessions:,} sessions → {result.path}")
        for n in notes:
            print(f"  [{n.severity}] {n.code} ×{n.count}: {n.detail}", file=sys.stderr)
    warnings = sum(1 for n in notes if n.severity in ("warn", "error"))
    return cli.strict_dq_code(args, warnings, config)
