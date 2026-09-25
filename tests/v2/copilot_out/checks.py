"""Local mirrors of the output rules (SPEC §14.1 JSON rule, §8.7 HTML rules) for CP-OUT unit tests.

A mirror, not an import of OUT (packages never import each other's modules); the gate tests run
OUT's own ``rule_violations`` on the same documents.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from html.parser import HTMLParser

from tokenbill.core.money import nano_to_usd_str

BILLED_KEYS = frozenset({"exact", "billed", "spend", "invoice", "joint_saving",
                         "headline_monthly"})
MONEY_KEYS = frozenset({"usd", "nano", "evidence", "basis", "finality", "range", "ci_level_pct",
                        "calibration", "upper_bound", "provenance", "note"})
EVIDENCE = frozenset({"exact", "estimated", "measured", "verified"})
_MONEY_KEY_RE = re.compile(r"(?:usd|nano|.+_usd|.+_nano)\Z")


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def json_violations(doc: object, path: str = "", key: str | None = None) -> list[str]:
    """SPEC §14.1: no floats; objects with integer leaves carry ``evidence``; money only as MONEY
    objects; list-equivalent never under a billed key."""
    errors: list[str] = []
    if isinstance(doc, float):
        return [f"{path}: float"]
    if isinstance(doc, (list, tuple)):
        for i, item in enumerate(doc):
            errors += json_violations(item, f"{path}/{i}", None)
        return errors
    if not isinstance(doc, Mapping):
        return errors
    if "usd" in doc or "nano" in doc:
        if set(doc) != MONEY_KEYS:
            errors.append(f"{path}: malformed MONEY")
        elif doc["nano"] is not None and doc["usd"] != nano_to_usd_str(doc["nano"]):
            errors.append(f"{path}: MONEY usd mismatch")
        if key in BILLED_KEYS and doc.get("basis") == "list_equivalent":
            errors.append(f"{path}: list_equivalent under billed key {key}")
        return errors
    has_int = False
    for k, v in doc.items():
        if _MONEY_KEY_RE.match(k) and v is not None:
            errors.append(f"{path}/{k}: money not in MONEY form")
        if _is_int(v) or (isinstance(v, list) and any(_is_int(x) for x in v)):
            has_int = True
        errors += json_violations(v, f"{path}/{k}", k)
    if has_int and doc.get("evidence") not in EVIDENCE:
        errors.append(f"{path}: integer object without evidence")
    return errors


_VOID = frozenset({"meta", "br", "hr", "img", "input", "link", "rect", "line", "path"})


class _Tree(HTMLParser):
    """Checks balanced tags and that every ``<svg>`` is followed by a sibling ``<table>`` whose
    first child is a ``<caption>``."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.after_svg = False
        self.expect_caption = False
        self.svgs = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:  # noqa: ANN001
        if self.expect_caption:
            if tag != "caption":
                self.errors.append("table after svg has no caption first")
            self.expect_caption = False
        if self.after_svg:
            if tag != "table":
                self.errors.append(f"svg followed by <{tag}>, not <table>")
            else:
                self.expect_caption = True
            self.after_svg = False
        if tag == "svg":
            self.svgs += 1
        if tag not in _VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list) -> None:  # noqa: ANN001
        pass

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unbalanced </{tag}>")
            return
        self.stack.pop()
        if tag == "svg":
            self.after_svg = True

    def handle_data(self, data: str) -> None:
        if self.after_svg and data.strip():
            self.errors.append("text between svg and its table")


def html_violations(text: str) -> list[str]:
    """No scripts, no URL schemes, balanced tags, SVG table twins with captions."""
    errors = []
    if re.search(r"(?i)<\s*script|javascript:", text):
        errors.append("script")
    if re.search(r"(?i)\b(?:https?|ftp)://", text):
        errors.append("external URL")
    tree = _Tree()
    tree.feed(text)
    tree.close()
    errors += tree.errors
    if tree.stack and tree.stack != ["html"]:
        errors.append(f"unclosed {tree.stack}")
    return errors
