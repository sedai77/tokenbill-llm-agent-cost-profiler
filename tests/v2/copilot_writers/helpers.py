"""Minimal local readers of every written shape (CP-SYNTH-W tests; independent of the adapters)
and shared constants."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

from tokenbill.core.builders import CANARY
from tokenbill.synth.copilot_writers import CONTENT_KEYS

ALL_CONVENTIONS = ("excl", "incl", "undecidable")


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]], bool]:
    """``(header, rows, has_bom)`` of a written CSV."""
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    reader = csv.reader(io.StringIO(raw.decode("utf-8-sig")))
    header = next(reader)
    rows = [dict(zip(header, row, strict=True)) for row in reader if row]
    return header, rows, bom


def read_jsonl(path: Path) -> list[Any]:
    """Every JSON value of a JSON-lines file; a torn line is recovered from its last
    ``{"type":`` (addendum §5.9)."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line, parse_float=Decimal))
        except json.JSONDecodeError:
            out.append(json.loads(line[line.rindex('{"type":'):], parse_float=Decimal))
    return out


def torn_lines(path: Path) -> int:
    """Lines that are not one JSON value."""
    n = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            json.loads(line)
        except json.JSONDecodeError:
            n += 1
    return n


def query(path: Path, sql: str, *args: Any) -> list[tuple]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def walk(value: Any, key: str = "#root", content: bool = False
         ) -> Iterator[tuple[str, str, bool]]:
    """``(key, string value, under a content key)`` for every string of a JSON value."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield from walk(v, k, content or k in CONTENT_KEYS)
    elif isinstance(value, list):
        for v in value:
            yield from walk(v, key, content)
    elif isinstance(value, str):
        yield key, value, content


def canary_split(docs: list[Any]) -> tuple[int, list[str]]:
    """``(content strings with the canary, keys of non-content strings with the canary)``."""
    hits, leaks = 0, []
    for doc in docs:
        for key, text, content in walk(doc):
            if CANARY in text:
                if content:
                    hits += 1
                else:
                    leaks.append(key)
    return hits, leaks


def otlp_attrs(attrs: list[dict[str, Any]]) -> dict[str, Any]:
    out = {}
    for a in attrs:
        v = a["value"]
        out[a["key"]] = int(v["intValue"]) if "intValue" in v else next(iter(v.values()))
    return out
