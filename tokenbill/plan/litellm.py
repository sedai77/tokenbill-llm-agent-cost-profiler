"""LiteLLM gateway fragment: restore prompt caching behind a proxy (SPEC §11.3; package PLAN).

A LiteLLM proxy that forwards Anthropic traffic without ``cache_control`` breakpoints bills every
prompt as uncached input (``gateway.restore_caching``, finding kinds ``no-cache`` /
``beta-header-dropped``). :func:`render_injection_points` writes the ``litellm-config.patch.yaml``
fragment of a policy pack: one ``model_list`` entry per affected model whose ``litellm_params``
carry ``cache_control_injection_points`` for the system message and the last message. A TTL is
emitted **only as a comment**: its LiteLLM key is unverified (SPEC §11.3, "TTL key VERIFY").

:func:`validate_litellm_fragment` checks such a fragment with a small stdlib parser for the YAML
subset the renderer emits (block mappings and sequences, plain / single- / double-quoted scalars,
integers, booleans, null, comments). Anything else — flow collections, anchors, tags, multi-line
scalars, tabs, duplicate keys, a wrong structure — raises :class:`UsageError`; nothing else escapes.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence

from tokenbill.core.errors import UsageError

__all__ = [
    "INJECTION_POINTS",
    "MAX_FRAGMENT_BYTES",
    "parse_yaml_subset",
    "render_injection_points",
    "validate_litellm_fragment",
]

#: The two injection points of SPEC §11.3 (system message, last message).
INJECTION_POINTS: tuple[tuple[tuple[str, str | int], ...], ...] = (
    (("location", "message"), ("role", "system")),
    (("location", "message"), ("index", -1)),
)
#: Fragments larger than this are refused by the validator.
MAX_FRAGMENT_BYTES = 1_000_000
_MAX_DEPTH = 32
_MAX_MODELS = 1_000
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+<>* -]{0,199}\Z")
_TTLS = ("5m", "1h")
_ROLES = ("system", "user", "assistant")
_PLAIN_RE = re.compile(r"[A-Za-z0-9_./+<>@-][A-Za-z0-9_./+<>@ :-]*\Z")
_INT_RE = re.compile(r"-?(?:0|[1-9][0-9]{0,17})\Z")
_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}\Z")
_HEADER = ("# tokenbill: LiteLLM prompt-caching fragment (litellm-config.patch.yaml).\n"
           "# Merge the litellm_params below into the matching model_list entries of your proxy\n"
           "# config.yaml; nothing is applied automatically. Review before rollout.\n")


def _bad(message: str) -> UsageError:
    return UsageError(f"litellm fragment: {message}")


def _quote(text: str) -> str:
    """A YAML double-quoted scalar (JSON string syntax is valid YAML)."""
    return json.dumps(text, ensure_ascii=True)


def render_injection_points(models: Sequence[str], *, ttl: str | None = None) -> str:
    """The ``litellm-config.patch.yaml`` text for *models* (de-duplicated, sorted).

    Every model gets ``cache_control_injection_points`` ``[{location: message, role: system},
    {location: message, index: -1}]``. *ttl* (``"5m"`` | ``"1h"``) is written as a commented
    line to verify against the LiteLLM documentation before enabling. Empty or unsafe model names
    and unknown TTLs raise :class:`UsageError`. The result always passes
    :func:`validate_litellm_fragment`.
    """
    if isinstance(models, str) or not isinstance(models, Sequence):
        raise _bad("models must be a sequence of model names")
    names = sorted(set(models))
    if not names:
        raise _bad("no model names")
    if len(names) > _MAX_MODELS:
        raise _bad("too many models")
    for name in names:
        if not isinstance(name, str) or not _MODEL_RE.match(name):
            raise _bad("model names must be printable model ids (no control characters)")
    if ttl is not None and ttl not in _TTLS:
        raise _bad("ttl must be 5m or 1h")
    lines = [_HEADER.rstrip("\n")]
    if ttl is not None:
        lines.append(f"# Desired cache TTL: {ttl}. The LiteLLM key for a TTL on injected "
                     "breakpoints is VERIFY:")
        lines.append("# check the LiteLLM prompt-caching documentation before adding it.")
    lines.append("model_list:")
    for name in names:
        lines.append(f"  - model_name: {_quote(name)}")
        lines.append("    litellm_params:")
        lines.append("      cache_control_injection_points:")
        for point in INJECTION_POINTS:
            for i, (key, value) in enumerate(point):
                prefix = "        - " if i == 0 else "          "
                lines.append(f"{prefix}{key}: {value}")
        if ttl is not None:
            lines.append(f"      # ttl: {_quote(ttl)}  # VERIFY (LiteLLM key unverified)")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------------------------
# the YAML-subset parser
# ---------------------------------------------------------------------------------------------


def _strip_comment(text: str, lineno: int) -> str:
    """*text* without a trailing ``# comment`` (a ``#`` at the start or after whitespace,
    outside quotes)."""
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
            elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
                return text[:i].rstrip()
        elif quote == '"':
            if ch == "\\":
                i += 1
            elif ch == '"':
                quote = None
        elif ch == "'":
            if i + 1 < len(text) and text[i + 1] == "'":
                i += 1
            else:
                quote = None
        i += 1
    if quote is not None:
        raise _bad(f"line {lineno}: unterminated quoted scalar")
    return text.rstrip()


def _scalar(text: str, lineno: int) -> object:
    text = text.strip()
    if not text:
        return None
    if text[0] == '"':
        try:
            value = json.loads(text)
        except ValueError:
            raise _bad(f"line {lineno}: invalid double-quoted scalar") from None
        if not isinstance(value, str):
            raise _bad(f"line {lineno}: invalid double-quoted scalar")
        return value
    if text[0] == "'":
        if len(text) < 2 or text[-1] != "'":
            raise _bad(f"line {lineno}: invalid single-quoted scalar")
        inner = text[1:-1]
        if re.search(r"(?<!')'(?!')", inner.replace("''", "")):
            raise _bad(f"line {lineno}: invalid single-quoted scalar")
        return inner.replace("''", "'")
    if text in ("null", "~"):
        return None
    if text in ("true", "false"):
        return text == "true"
    if _INT_RE.match(text):
        return int(text)
    if not _PLAIN_RE.match(text) or ": " in text or text.endswith(":"):
        raise _bad(f"line {lineno}: unsupported scalar")
    return text


def _key(text: str, lineno: int) -> str:
    text = text.strip()
    if text[:1] in ('"', "'"):
        value = _scalar(text, lineno)
        if not isinstance(value, str) or not value:
            raise _bad(f"line {lineno}: invalid key")
        return value
    if not _KEY_RE.match(text):
        raise _bad(f"line {lineno}: invalid key")
    return text


def _split_key(text: str, lineno: int) -> tuple[str, str] | None:
    """``(key, rest)`` of a ``key: value`` / ``key:`` line, or None when it is not one."""
    if text[:1] in ('"', "'"):
        quote = text[0]
        end = 1
        while end < len(text):
            if text[end] == "\\" and quote == '"':
                end += 2
                continue
            if text[end] == quote:
                if quote == "'" and end + 1 < len(text) and text[end + 1] == "'":
                    end += 2
                    continue
                break
            end += 1
        if end >= len(text):
            raise _bad(f"line {lineno}: unterminated quoted key")
        rest = text[end + 1:]
        if rest == ":" or rest.startswith(": "):
            return text[:end + 1], rest[1:]
        return None
    m = re.match(r"([^\s:'\"#][^:]*?):(?:\s(.*)|)\Z", text)
    if m is None:
        return None
    return m.group(1), m.group(2) or ""


class _Parser:
    def __init__(self, text: str) -> None:
        self.lines: list[tuple[int, str, int]] = []   # (indent, content, line number)
        for lineno, raw in enumerate(text.split("\n"), start=1):
            if "\t" in raw:
                raise _bad(f"line {lineno}: tabs are not allowed")
            if any(ord(ch) < 32 and ch != "\r" for ch in raw):
                raise _bad(f"line {lineno}: control character")
            line = raw.rstrip("\r")
            stripped = line.lstrip(" ")
            if stripped.startswith("---") or stripped.startswith("..."):
                if stripped in ("---", "..."):
                    raise _bad(f"line {lineno}: multiple documents are not supported")
            content = _strip_comment(stripped, lineno)
            if not content:
                continue
            if content[0] in "[{&*!|>%@`?":
                raise _bad(f"line {lineno}: unsupported YAML construct")
            self.lines.append((len(line) - len(stripped), content, lineno))
        self.pos = 0

    def parse(self) -> object:
        if not self.lines:
            raise _bad("empty document")
        value = self.block(self.lines[0][0], 0)
        if self.pos != len(self.lines):
            raise _bad(f"line {self.lines[self.pos][2]}: unexpected indentation")
        return value

    def block(self, indent: int, depth: int) -> object:
        if depth > _MAX_DEPTH:
            raise _bad("nesting too deep")
        ind, content, _ = self.lines[self.pos]
        if ind != indent:
            raise _bad(f"line {self.lines[self.pos][2]}: unexpected indentation")
        if content == "-" or content.startswith("- "):
            return self.sequence(indent, depth)
        return self.mapping(indent, depth)

    def _child(self, indent: int, depth: int, lineno: int, *, allow_same_seq: bool) -> object:
        if self.pos >= len(self.lines):
            return None
        ind, content, _ = self.lines[self.pos]
        if ind > indent:
            return self.block(ind, depth + 1)
        if allow_same_seq and ind == indent and (content == "-" or content.startswith("- ")):
            return self.sequence(indent, depth + 1)
        return None

    def sequence(self, indent: int, depth: int) -> list[object]:
        items: list[object] = []
        while self.pos < len(self.lines):
            ind, content, lineno = self.lines[self.pos]
            if ind != indent or not (content == "-" or content.startswith("- ")):
                break
            rest = content[1:].lstrip(" ")
            if not rest:
                self.pos += 1
                items.append(self._child(indent, depth, lineno, allow_same_seq=False))
                continue
            offset = indent + (len(content) - len(rest))
            if rest.startswith("- ") or rest == "-" or _split_key(rest, lineno) is not None:
                self.lines[self.pos] = (offset, rest, lineno)
                items.append(self.block(offset, depth + 1))
            else:
                self.pos += 1
                items.append(_scalar(rest, lineno))
        return items

    def mapping(self, indent: int, depth: int) -> dict[str, object]:
        out: dict[str, object] = {}
        while self.pos < len(self.lines):
            ind, content, lineno = self.lines[self.pos]
            if ind != indent:
                if ind > indent:
                    raise _bad(f"line {lineno}: unexpected indentation")
                break
            if content == "-" or content.startswith("- "):
                break
            split = _split_key(content, lineno)
            if split is None:
                raise _bad(f"line {lineno}: expected 'key: value'")
            key = _key(split[0], lineno)
            if key in out:
                raise _bad(f"line {lineno}: duplicate key")
            self.pos += 1
            rest = split[1].strip()
            if rest:
                out[key] = _scalar(rest, lineno)
            else:
                out[key] = self._child(indent, depth, lineno, allow_same_seq=True)
        return out


def parse_yaml_subset(text: str) -> object:
    """Parse the YAML subset of the module docstring into dicts, lists and scalars
    (:class:`UsageError` for anything outside it)."""
    if not isinstance(text, str):
        raise _bad("text must be a string")
    if len(text.encode("utf-8", "surrogatepass")) > MAX_FRAGMENT_BYTES:
        raise _bad("fragment too large")
    return _Parser(text).parse()


def _check_point(point: object, where: str) -> None:
    if not isinstance(point, dict) or point.get("location") != "message":
        raise _bad(f"{where}: each injection point needs location: message")
    extra = set(point) - {"location", "role", "index"}
    if extra:
        raise _bad(f"{where}: unknown injection-point key")
    has_role, has_index = "role" in point, "index" in point
    if has_role == has_index:
        raise _bad(f"{where}: an injection point has exactly one of role / index")
    if has_role and point["role"] not in _ROLES:
        raise _bad(f"{where}: role must be system, user or assistant")
    if has_index and type(point["index"]) is not int:
        raise _bad(f"{where}: index must be an integer")


def validate_litellm_fragment(text: str) -> None:
    """Check a LiteLLM config fragment: ``model_list`` is a non-empty list of entries with a
    ``model_name`` string and ``litellm_params.cache_control_injection_points``, a non-empty list
    of ``{location: message, role: …}`` / ``{location: message, index: <int>}`` points. Raises
    :class:`UsageError` (never anything else) when the text is not in the parser's YAML subset
    or the structure is wrong."""
    doc = parse_yaml_subset(text)
    if not isinstance(doc, dict) or set(doc) != {"model_list"}:
        raise _bad("the fragment must be a mapping with only model_list")
    entries = doc["model_list"]
    if not isinstance(entries, list) or not entries:
        raise _bad("model_list must be a non-empty list")
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        where = f"model_list[{i}]"
        if not isinstance(entry, dict) or set(entry) != {"model_name", "litellm_params"}:
            raise _bad(f"{where}: needs model_name and litellm_params only")
        name = entry["model_name"]
        if not isinstance(name, str) or not name or name in seen:
            raise _bad(f"{where}: model_name must be a unique non-empty string")
        seen.add(name)
        params = entry["litellm_params"]
        if not isinstance(params, dict) or set(params) != {"cache_control_injection_points"}:
            raise _bad(f"{where}: litellm_params needs cache_control_injection_points only")
        points = params["cache_control_injection_points"]
        if not isinstance(points, list) or not points:
            raise _bad(f"{where}: cache_control_injection_points must be a non-empty list")
        for j, point in enumerate(points):
            _check_point(point, f"{where}.cache_control_injection_points[{j}]")
