"""Hypothesis fuzz of the config parser (SPEC §21 #5): only ``UsageError`` may escape, and a
successful load always yields a valid, hashable Config."""

from __future__ import annotations

import dataclasses
import json
import tempfile
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tokenbill.config import ENV_VARS, Config, load_config
from tokenbill.core.errors import UsageError

_FIELDS = [f.name for f in dataclasses.fields(Config)]
_KEYS = st.one_of(st.sampled_from(_FIELDS + ["schema", "retention.identity_days", "thresholds.a"]),
                  st.text(max_size=12))
_SCALARS = st.one_of(st.none(), st.booleans(), st.integers(min_value=-(2**70), max_value=2**70),
                     st.decimals(allow_nan=False, allow_infinity=False, places=4),
                     st.text(max_size=20))
_VALUES = st.recursive(_SCALARS, lambda inner: st.one_of(
    st.lists(inner, max_size=4), st.dictionaries(st.text(max_size=8), inner, max_size=4)),
    max_leaves=10)

_DIR = Path(tempfile.mkdtemp(prefix="tb-wiring-fuzz-"))
_HOME = _DIR / "home"
_HOME.mkdir()
_EMPTY = _DIR / "empty.json"   # an explicit empty file: no default config file is ever read
_EMPTY.write_text("{}", encoding="utf-8")


def _check(cfg: Config) -> None:
    assert isinstance(cfg, Config)
    hash(cfg)
    assert cfg.k >= 1 and cfg.jobs >= 1 and cfg.strict_dq_threshold >= 0
    assert set(cfg.retention) == {"identity_days", "request_days", "event_days", "rollup_days"}
    assert all(isinstance(v, str) for v in cfg.thresholds.values())


def _encode(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)  # JSON test input only; the loader reads it back as a Decimal
    if isinstance(value, list):
        return [_encode(v) for v in value]
    if isinstance(value, dict):
        return {k: _encode(v) for k, v in value.items()}
    return value


@settings(max_examples=300, deadline=None)
@given(st.dictionaries(_KEYS, _VALUES, max_size=6))
def test_file_objects_only_raise_usage_errors(obj: dict) -> None:
    path = _DIR / "config.json"
    path.write_text(json.dumps(_encode(obj)), encoding="utf-8")
    try:
        cfg = load_config(path, {"HOME": str(_HOME)}, {})
    except UsageError:
        return
    _check(cfg)


@settings(max_examples=200, deadline=None)
@given(st.binary(max_size=200))
def test_file_bytes_only_raise_usage_errors(raw: bytes) -> None:
    path = _DIR / "raw.json"
    path.write_bytes(raw)
    try:
        cfg = load_config(path, {"HOME": str(_HOME)}, {})
    except UsageError:
        return
    _check(cfg)


@settings(max_examples=300, deadline=None)
@given(st.dictionaries(st.sampled_from(sorted(ENV_VARS)), st.text(max_size=24), max_size=5))
def test_environment_only_raises_usage_errors(env: dict[str, str]) -> None:
    try:
        cfg = load_config(_EMPTY, {"HOME": str(_HOME), **env}, {})
    except UsageError:
        return
    _check(cfg)


@settings(max_examples=300, deadline=None)
@given(st.dictionaries(_KEYS, _VALUES, max_size=5))
def test_overrides_only_raise_usage_errors(overrides: dict) -> None:
    try:
        cfg = load_config(_EMPTY, {"HOME": str(_HOME)}, overrides)
    except UsageError:
        return
    _check(cfg)


def test_unknown_home_user_in_a_path_is_a_usage_error() -> None:
    """``~0`` / ``~no-such-user`` cannot be expanded: a usage error, never a RuntimeError."""
    with pytest.raises(UsageError, match="home directory"):
        load_config(_EMPTY, {"HOME": str(_HOME),
                             "TOKENBILL_COLLECTION_KEY_FILE": "~tokenbill-no-such-user-0/k"}, {})
