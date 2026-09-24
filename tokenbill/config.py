"""Configuration with precedence (SPEC §15, WIRING).

:func:`load_config` merges four layers, highest first:

1. **overrides** — the CLI flags (a mapping of :class:`Config` field names; ``None`` values mean
   "flag not given" and are skipped);
2. **environment** — ``TOKENBILL_<FIELD>`` variables (see :data:`ENV_VARS`);
3. **file** — a JSON object: the explicit ``--config PATH``, else the first existing of
   ``./.tokenbill/config.json`` and ``~/.config/tokenbill/config.json``;
4. **defaults** — the :class:`Config` field defaults.

Scalar and sequence fields take the value of the highest layer that sets them. The two mapping
fields merge **per key**: ``retention`` (``identity_days``, ``request_days``, ``event_days``,
``rollup_days``) and ``thresholds`` (detector overrides, SPEC §10.1) — a higher layer replaces only
the keys it sets. Unknown keys in the file or in the overrides, unknown ``retention`` keys and
values of the wrong type raise :class:`~tokenbill.core.errors.UsageError` (CLI exit 2) with a
message that names the key and the layer, never the value. Unknown ``TOKENBILL_*`` variables are
ignored (other tools and tests use the prefix, e.g. ``TOKENBILL_LOCAL_CORPUS``).

Environment encodings: integers as decimal digits; ``name_allowlist`` and ``residency_required`` as
comma-separated lists; ``rates`` as an ``os.pathsep``-separated list of paths; ``thresholds`` as a
JSON object of strings; ``retention`` per key as ``TOKENBILL_RETENTION_<KEY>`` (e.g.
``TOKENBILL_RETENTION_IDENTITY_DAYS``). Relative paths in a config **file** (``key_file``,
``collection_key_file``, ``rates``, ``contract``) are resolved against the file's directory and
``~`` is expanded; paths from the environment and the CLI stay relative to the working directory
(``~`` expanded). A file may carry ``"schema": "tokenbill/config@1"`` (:func:`config_json` writes
it).

The result is an immutable, hashable and picklable :class:`Config` (its mappings are
:class:`FrozenMap`), so it can travel to shard worker processes.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TypeVar

from tokenbill.core.errors import SourceError, UsageError
from tokenbill.core.jsonl import load_json_exact

__all__ = [
    "CONFIG_SCHEMA",
    "DEFAULT_RETENTION",
    "ENV_PREFIX",
    "ENV_VARS",
    "IDENTITY_MODES",
    "RETENTION_KEYS",
    "Config",
    "FrozenMap",
    "config_json",
    "default_config_paths",
    "load_config",
]

V = TypeVar("V")

#: ``schema`` value a config file may carry (and :func:`config_json` writes).
CONFIG_SCHEMA = "tokenbill/config@1"
#: Prefix of the environment layer.
ENV_PREFIX = "TOKENBILL_"
#: Collector identity modes (SPEC §5.4; ``init --identity-mode``).
IDENTITY_MODES = ("central", "two-stage")
#: Retention keys (SPEC §7.5) and their defaults in days.
DEFAULT_RETENTION: Mapping[str, int] = {
    "identity_days": 90, "request_days": 395, "event_days": 90, "rollup_days": 395,
}
RETENTION_KEYS = tuple(DEFAULT_RETENTION)

_MAX_FILE_BYTES = 1 << 20
_MAX_TEXT = 4096
_MAX_INT = 2**53
_DIGITS_RE = re.compile(r"[0-9]{1,16}\Z")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class FrozenMap(Mapping[str, V]):
    """An immutable, hashable, picklable ``str``-keyed mapping with sorted iteration (deterministic
    ``repr`` and JSON)."""

    __slots__ = ("_items",)

    def __init__(self, items: Mapping[str, V] | Iterable[tuple[str, V]] = ()) -> None:
        pairs = items.items() if isinstance(items, Mapping) else items
        object.__setattr__(self, "_items", dict(sorted(dict(pairs).items())))

    def __getitem__(self, key: str) -> V:
        return self._items[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __hash__(self) -> int:
        return hash(tuple(self._items.items()))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self._items) == dict(other.items())
        return NotImplemented

    def __repr__(self) -> str:
        return f"FrozenMap({self._items!r})"

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("FrozenMap is immutable")

    def __reduce__(self) -> tuple[type, tuple[tuple[tuple[str, V], ...]]]:
        return (FrozenMap, (tuple(self._items.items()),))


@dataclass(frozen=True)
class Config:
    """Resolved configuration (SPEC §15), usually built by :func:`load_config`; every field is
    validated on construction.

    ``retention`` holds the four :data:`RETENTION_KEYS` in days; ``thresholds`` holds detector
    overrides as strings (SPEC §10.1, e.g. ``"defaults.effort"``, ``"policy.ttl.<team>"``);
    ``rates`` are extra rate files (``--rates``); ``key_file`` is the org key,
    ``collection_key_file`` the fleet collection (name) key; ``strict_dq_threshold`` is the number
    of data-quality warnings at which ``--strict-dq`` exits 4.
    """

    k: int = 5
    min_usd: str = "1.00"
    jobs: int = 1
    shard_max_requests: int = 250_000
    sample_lanes: int = 20_000
    identity_mode: str = "central"
    retention: Mapping[str, int] = field(default_factory=lambda: FrozenMap(DEFAULT_RETENTION))
    thresholds: Mapping[str, str] = field(default_factory=FrozenMap)
    name_allowlist: frozenset[str] = frozenset()
    residency_required: tuple[str, ...] = ()
    key_file: str | None = None
    collection_key_file: str | None = None
    rates: tuple[str, ...] = ()
    contract: str | None = None
    strict_dq_threshold: int = 1

    def __post_init__(self) -> None:
        """Validate every field (``UsageError``) and normalize container types: mappings become
        :class:`FrozenMap` (``retention`` is completed with :data:`DEFAULT_RETENTION`),
        ``name_allowlist`` a ``frozenset``, sequences tuples. Paths are checked, not resolved."""
        for name in _FIELDS:
            value = getattr(self, name)
            if name in _PATH_FIELDS:
                checked: object = None if value is None else _text(value, name, "value")
            elif name == "rates":
                checked = _str_list([value] if isinstance(value, (str, Path)) else value,
                                    name, "value")
            elif name == "retention":
                checked = FrozenMap({**DEFAULT_RETENTION, **_retention_map(value, "value")})
            elif name == "thresholds":
                checked = FrozenMap(_threshold_map(value, name, "value"))
            else:
                checked = _field_value(name, value, "value", None)
            if checked is not value:
                object.__setattr__(self, name, checked)


_FIELDS = tuple(f.name for f in fields(Config))
_INT_MIN = {"k": 1, "jobs": 1, "shard_max_requests": 1, "sample_lanes": 1,
            "strict_dq_threshold": 0}
_PATH_FIELDS = ("key_file", "collection_key_file", "contract")

#: ``TOKENBILL_*`` variable → config field (``retention`` per key: ``TOKENBILL_RETENTION_<KEY>``).
ENV_VARS: Mapping[str, str] = {
    **{ENV_PREFIX + name.upper(): name for name in _FIELDS if name != "retention"},
    **{f"{ENV_PREFIX}RETENTION_{key.upper()}": f"retention.{key}" for key in RETENTION_KEYS},
}


# ---------------------------------------------------------------------------------------------
# value checks (every failure is a UsageError naming the key and the layer, never the value)
# ---------------------------------------------------------------------------------------------


def _bad(key: str, layer: str, why: str) -> UsageError:
    return UsageError(f"config {layer}: {key} {why}")


def _text(value: object, key: str, layer: str, *, allow_empty: bool = False) -> str:
    if isinstance(value, Path):
        value = str(value)
    if not isinstance(value, str):
        raise _bad(key, layer, "must be a string")
    text = value.strip()
    if (not text and not allow_empty) or len(text) > _MAX_TEXT or _CONTROL_RE.search(text):
        raise _bad(key, layer, f"must be a non-empty string of at most {_MAX_TEXT} characters "
                               "without control characters")
    return text


def _int(value: object, key: str, layer: str, minimum: int) -> int:
    if isinstance(value, str) and _DIGITS_RE.match(value.strip()):
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int):
        raise _bad(key, layer, "must be an integer")
    if not minimum <= value <= _MAX_INT:
        raise _bad(key, layer, f"must be an integer >= {minimum}")
    return value


def _decimal_text(value: object, key: str, layer: str) -> str:
    """A finite, non-negative decimal amount as a plain string (``"1.00"``)."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise _bad(key, layer, "must be a decimal string")
    text = value.strip() if isinstance(value, str) else str(value)
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise _bad(key, layer, "must be a decimal string") from None
    if (not amount.is_finite() or amount < 0 or amount.adjusted() > 15
            or len(text) > 64 or amount.as_tuple().exponent < -12):  # type: ignore[operator]
        raise _bad(key, layer, "must be a non-negative decimal of at most 12 decimal places")
    if isinstance(value, str):
        return text
    return format(amount, "f")


def _str_list(value: object, key: str, layer: str, *, sep: str = ",") -> tuple[str, ...]:
    if isinstance(value, (str, Path)):
        parts: Iterable[object] = [p for p in str(value).split(sep) if p.strip()]
    elif isinstance(value, (set, frozenset)):
        parts = sorted(value, key=str)
    elif isinstance(value, (list, tuple)):
        parts = value
    else:
        raise _bad(key, layer, "must be a list of strings")
    out = [_text(p, key, layer) for p in parts]
    return tuple(dict.fromkeys(out))


def _threshold_map(value: object, key: str, layer: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise _bad(key, layer, "must be an object of strings")
    out: dict[str, str] = {}
    for name, raw in value.items():
        tkey = _text(name, f"{key} key", layer)
        if isinstance(raw, bool) or not isinstance(raw, (str, int, Decimal)):
            raise _bad(f"{key}.{tkey}", layer, "must be a string or a number")
        text = raw if isinstance(raw, str) else format(raw, "f") if isinstance(
            raw, Decimal) else str(raw)
        out[tkey] = _text(text, f"{key}.{tkey}", layer)
    return out


def _retention_map(value: object, layer: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise _bad("retention", layer, "must be an object of day counts")
    out: dict[str, int] = {}
    for name, raw in value.items():
        if name not in DEFAULT_RETENTION:
            raise _bad(f"retention.{str(name)[:64]}", layer, "is not a known key "
                       f"({', '.join(RETENTION_KEYS)})")
        out[name] = _int(raw, f"retention.{name}", layer, 1)
    return out


def _resolve_path(text: str, base: Path | None) -> str:
    path = Path(text).expanduser()
    if base is not None and not path.is_absolute():
        path = base / path
    return str(path)


def _field_value(name: str, value: object, layer: str, base: Path | None,
                 key: str | None = None) -> object:
    """The validated value of top-level field *name* (``retention``/``thresholds`` return dicts);
    errors name *key* (default: the field name; the environment layer passes its variable)."""
    label = key or name
    if name in _INT_MIN:
        return _int(value, label, layer, _INT_MIN[name])
    if name == "min_usd":
        return _decimal_text(value, label, layer)
    if name == "identity_mode":
        mode = _text(value, label, layer)
        if mode not in IDENTITY_MODES:
            raise _bad(label, layer, f"(identity_mode) must be one of {', '.join(IDENTITY_MODES)}")
        return mode
    if name == "retention":
        return _retention_map(value, layer)
    if name == "thresholds":
        return _threshold_map(value, label, layer)
    if name == "name_allowlist":
        return frozenset(_str_list(value, label, layer))
    if name == "residency_required":
        return _str_list(value, label, layer)
    if name == "rates":
        sep = os.pathsep if layer == "environment" else ","
        if isinstance(value, (str, Path)) and layer != "environment":
            value = [value]
        return tuple(_resolve_path(p, base) for p in _str_list(value, label, layer, sep=sep))
    if name in _PATH_FIELDS:
        if value is None:
            return None
        return _resolve_path(_text(value, label, layer), base)
    raise _bad(label[:64], layer, "is not a known key")  # pragma: no cover - callers check names


# ---------------------------------------------------------------------------------------------
# layers
# ---------------------------------------------------------------------------------------------


class _Layer:
    """One precedence layer: validated top-level values plus per-key mapping entries."""

    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.retention: dict[str, int] = {}
        self.thresholds: dict[str, str] = {}

    def set(self, name: str, value: object, layer: str, base: Path | None = None,
            key: str | None = None) -> None:
        checked = _field_value(name, value, layer, base, key)
        if name == "retention":
            self.retention.update(checked)  # type: ignore[arg-type]
        elif name == "thresholds":
            self.thresholds.update(checked)  # type: ignore[arg-type]
        else:
            self.values[name] = checked


def default_config_paths(*, cwd: Path | None = None, home: Path | None = None) -> tuple[Path, ...]:
    """The config files tried when no ``--config`` is given, in order:
    ``<cwd>/.tokenbill/config.json`` then ``<home>/.config/tokenbill/config.json``."""
    here = Path.cwd() if cwd is None else Path(cwd)
    user = Path.home() if home is None else Path(home)
    return (here / ".tokenbill" / "config.json", user / ".config" / "tokenbill" / "config.json")


def _home(environ: Mapping[str, str]) -> Path | None:
    home = environ.get("HOME")
    if home:
        return Path(home)
    try:
        return Path.home()
    except RuntimeError:  # pragma: no cover - no home directory at all
        return None


def _file_layer(path: Path | None, environ: Mapping[str, str]) -> _Layer:
    layer = _Layer()
    if path is None:
        home = _home(environ)
        candidates = default_config_paths(home=home) if home is not None else (
            default_config_paths(home=Path("."))[0],)
        found = next((p for p in candidates if p.is_file()), None)
        if found is None:
            return layer
        path = found
    else:
        path = Path(path).expanduser()
        if not path.is_file():
            raise UsageError(f"config file {path.name}: not found")
    try:
        data = load_json_exact(path, max_bytes=_MAX_FILE_BYTES)
    except SourceError as exc:
        raise UsageError(f"config file {exc}") from None
    if not isinstance(data, dict):
        raise UsageError(f"config file {path.name}: must be a JSON object")
    base = path.resolve().parent
    where = f"file {path.name}"
    for name, value in data.items():
        if name == "schema":
            if value != CONFIG_SCHEMA:
                raise _bad("schema", where, f"must be {CONFIG_SCHEMA!r}")
            continue
        if name not in _FIELDS:
            raise _bad(str(name)[:64], where, "is not a known key")
        if value is None and name in _PATH_FIELDS:
            layer.values[name] = None
            continue
        layer.set(name, value, where, base)
    return layer


def _env_layer(environ: Mapping[str, str]) -> _Layer:
    layer = _Layer()
    for var in sorted(environ):
        name = ENV_VARS.get(var)
        if name is None:
            continue
        raw = environ[var]
        if not isinstance(raw, str):
            raise _bad(var, "environment", "must be a string")
        if name.startswith("retention."):
            sub = name.split(".", 1)[1]
            layer.retention[sub] = _int(raw, var, "environment", 1)
        elif name == "thresholds":
            try:
                parsed = json.loads(raw, parse_float=Decimal, parse_constant=_reject_constant)
            except (ValueError, RecursionError):
                raise _bad(var, "environment", "must be a JSON object of strings") from None
            layer.thresholds.update(_threshold_map(parsed, var, "environment"))
        else:
            layer.set(name, raw, "environment", key=f"{var} ({name})")
    return layer


def _reject_constant(name: str) -> object:
    raise ValueError(f"{name} is not allowed")


def _override_layer(overrides: Mapping[str, object]) -> _Layer:
    layer = _Layer()
    if not isinstance(overrides, Mapping):
        raise UsageError("config overrides must be a mapping")
    for name in sorted(overrides, key=str):
        value = overrides[name]
        if not isinstance(name, str):
            raise _bad(str(name)[:64], "override", "is not a known key")
        if value is None:
            continue
        head, _, sub = name.partition(".")
        if sub and head == "retention":
            layer.retention.update(_retention_map({sub: value}, "override"))
        elif sub and head == "thresholds":
            layer.thresholds.update(_threshold_map({sub: value}, "thresholds", "override"))
        elif name in _FIELDS:
            layer.set(name, value, "override")
        else:
            raise _bad(name[:64], "override", "is not a known key")
    return layer


def load_config(path: Path | None, environ: Mapping[str, str],
                overrides: Mapping[str, object]) -> Config:
    """Resolve the configuration: *overrides* (CLI flags) > ``TOKENBILL_*`` in *environ* > the JSON
    file (*path*, else ``./.tokenbill/config.json``, else ``~/.config/tokenbill/config.json``, the
    home directory taken from ``environ["HOME"]`` when set) > defaults. An explicit *path* that does
    not exist, an unreadable or malformed file, unknown keys and invalid values raise
    ``UsageError``."""
    if not isinstance(environ, Mapping):
        raise UsageError("config environment must be a mapping")
    layers = [_file_layer(path, environ), _env_layer(environ), _override_layer(overrides)]
    values: dict[str, Any] = {}
    retention = dict(DEFAULT_RETENTION)
    thresholds: dict[str, str] = {}
    for layer in layers:  # lowest precedence first
        values.update(layer.values)
        retention.update(layer.retention)
        thresholds.update(layer.thresholds)
    return Config(retention=FrozenMap(retention), thresholds=FrozenMap(thresholds), **values)


def config_json(config: Config) -> dict[str, object]:
    """The JSON object of *config* (``schema`` first, keys in field order; sets and tuples as sorted
    / ordered lists): what ``init`` writes and :func:`load_config` reads back to an equal Config."""
    out: dict[str, object] = {"schema": CONFIG_SCHEMA}
    for name in _FIELDS:
        value = getattr(config, name)
        if isinstance(value, Mapping):
            value = dict(value)
        elif isinstance(value, frozenset):
            value = sorted(value)
        elif isinstance(value, tuple):
            value = list(value)
        out[name] = value
    return out
