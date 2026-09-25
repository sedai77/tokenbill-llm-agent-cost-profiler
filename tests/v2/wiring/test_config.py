"""Config precedence, defaults and validation (SPEC §15 ``config.py``)."""

from __future__ import annotations

import dataclasses
import json
import os
import pickle
from decimal import Decimal
from pathlib import Path

import pytest

from tokenbill.config import (
    CONFIG_SCHEMA,
    DEFAULT_RETENTION,
    ENV_VARS,
    Config,
    FrozenMap,
    config_json,
    default_config_paths,
    load_config,
)
from tokenbill.core.errors import UsageError


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated working directory (``./.tokenbill``) and home (``~/.config/tokenbill``)."""
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    user = tmp_path / "home"
    user.mkdir()
    return user


def _env(home: Path, **kv: str) -> dict[str, str]:
    return {"HOME": str(home), **kv}


def _write(path: Path, obj: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
    return path


def _home_file(home: Path) -> Path:
    return home / ".config" / "tokenbill" / "config.json"


def _cwd_file() -> Path:
    return Path.cwd() / ".tokenbill" / "config.json"


# ---------------------------------------------------------------------------------------------
# defaults
# ---------------------------------------------------------------------------------------------


def test_defaults_per_spec(home: Path) -> None:
    cfg = load_config(None, _env(home), {})
    assert cfg == Config()
    assert (cfg.k, cfg.min_usd, cfg.jobs, cfg.shard_max_requests, cfg.sample_lanes) == (
        5, "1.00", 1, 250_000, 20_000)
    assert cfg.identity_mode == "central"
    assert dict(cfg.retention) == {"identity_days": 90, "request_days": 395, "event_days": 90,
                                   "rollup_days": 395}
    assert dict(cfg.thresholds) == {}
    assert cfg.name_allowlist == frozenset() and cfg.residency_required == ()
    assert (cfg.key_file, cfg.collection_key_file, cfg.rates, cfg.contract) == (None, None, (),
                                                                                  None)
    assert cfg.strict_dq_threshold == 1


def test_default_config_paths_order(tmp_path: Path) -> None:
    first, second = default_config_paths(cwd=tmp_path / "w", home=tmp_path / "h")
    assert first == tmp_path / "w" / ".tokenbill" / "config.json"
    assert second == tmp_path / "h" / ".config" / "tokenbill" / "config.json"
    assert default_config_paths()[0] == Path.cwd() / ".tokenbill" / "config.json"


# ---------------------------------------------------------------------------------------------
# precedence: overrides > env > file > defaults (every layer)
# ---------------------------------------------------------------------------------------------


def test_every_layer_in_order(home: Path) -> None:
    _write(_cwd_file(), {"k": 7, "jobs": 2})
    env = _env(home, TOKENBILL_K="8")
    assert load_config(None, env, {"k": 9}).k == 9          # override wins
    assert load_config(None, env, {}).k == 8                # then the environment
    assert load_config(None, _env(home), {}).k == 7         # then the file
    assert load_config(None, _env(home), {}).jobs == 2
    _cwd_file().unlink()
    assert load_config(None, _env(home), {}).k == 5         # then the default


def test_cwd_file_beats_home_file_and_files_do_not_merge(home: Path) -> None:
    _write(_home_file(home), {"k": 11, "jobs": 4})
    assert load_config(None, _env(home), {}).k == 11        # home file when no cwd file
    _write(_cwd_file(), {"k": 12})
    cfg = load_config(None, _env(home), {})
    assert (cfg.k, cfg.jobs) == (12, 1)                     # the home file is not read at all


def test_explicit_path_beats_default_files(home: Path, tmp_path: Path) -> None:
    _write(_cwd_file(), {"k": 12})
    explicit = _write(tmp_path / "custom.json", {"k": 13})
    assert load_config(explicit, _env(home), {}).k == 13


def test_explicit_missing_path_is_a_usage_error(home: Path, tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="not found"):
        load_config(tmp_path / "missing.json", _env(home), {})


def test_home_from_environ_else_path_home(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(_home_file(home), {"k": 21})
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    assert load_config(None, {}, {}).k == 21                # no HOME in environ → Path.home()


def test_mapping_fields_merge_per_key(home: Path) -> None:
    _write(_cwd_file(), {"retention": {"identity_days": 30},
                         "thresholds": {"a": "1", "b": "2"}})
    env = _env(home, TOKENBILL_RETENTION_EVENT_DAYS="10",
               TOKENBILL_THRESHOLDS=json.dumps({"b": "3"}))
    cfg = load_config(None, env, {"retention.rollup_days": 100, "thresholds.c": "4",
                                  "thresholds": {"d": 5}})
    assert dict(cfg.retention) == {"identity_days": 30, "event_days": 10, "rollup_days": 100,
                                   "request_days": 395}
    assert dict(cfg.thresholds) == {"a": "1", "b": "3", "c": "4", "d": "5"}


def test_sequence_fields_replace_not_merge(home: Path) -> None:
    _write(_cwd_file(), {"name_allowlist": ["github", "Bash"], "residency_required": ["us"]})
    cfg = load_config(None, _env(home, TOKENBILL_NAME_ALLOWLIST="linear"), {})
    assert cfg.name_allowlist == frozenset({"linear"})
    assert cfg.residency_required == ("us",)
    cfg = load_config(None, _env(home), {"residency_required": ["eu", "us", "eu"]})
    assert cfg.residency_required == ("eu", "us")


def test_none_overrides_mean_flag_not_given(home: Path) -> None:
    _write(_cwd_file(), {"k": 7, "key_file": "org.key"})
    cfg = load_config(None, _env(home), {"k": None, "key_file": None, "jobs": None})
    assert cfg.k == 7 and cfg.key_file is not None and cfg.jobs == 1


def test_every_env_variable(home: Path, tmp_path: Path) -> None:
    rates = os.pathsep.join([str(tmp_path / "a.json"), str(tmp_path / "b.json")])
    env = _env(home, TOKENBILL_K="6", TOKENBILL_MIN_USD="2.50", TOKENBILL_JOBS="3",
               TOKENBILL_SHARD_MAX_REQUESTS="1000", TOKENBILL_SAMPLE_LANES="50",
               TOKENBILL_IDENTITY_MODE="two-stage", TOKENBILL_THRESHOLDS='{"x.y": "0.5"}',
               TOKENBILL_NAME_ALLOWLIST="github, linear", TOKENBILL_RESIDENCY_REQUIRED="us",
               TOKENBILL_KEY_FILE="keys/org.key", TOKENBILL_COLLECTION_KEY_FILE="~/c.key",
               TOKENBILL_RATES=rates, TOKENBILL_CONTRACT="contract.json",
               TOKENBILL_STRICT_DQ_THRESHOLD="0", TOKENBILL_RETENTION_IDENTITY_DAYS="7",
               TOKENBILL_RETENTION_REQUEST_DAYS="8", TOKENBILL_RETENTION_EVENT_DAYS="9",
               TOKENBILL_RETENTION_ROLLUP_DAYS="10", TOKENBILL_LOCAL_CORPUS="1")
    cfg = load_config(None, env, {})
    assert (cfg.k, cfg.min_usd, cfg.jobs, cfg.shard_max_requests, cfg.sample_lanes) == (
        6, "2.50", 3, 1000, 50)
    assert cfg.identity_mode == "two-stage"
    assert dict(cfg.thresholds) == {"x.y": "0.5"}
    assert cfg.name_allowlist == frozenset({"github", "linear"})
    assert cfg.residency_required == ("us",)
    assert cfg.key_file == "keys/org.key"                   # relative to the working directory
    assert cfg.collection_key_file == str(Path("~/c.key").expanduser())
    assert cfg.rates == (str(tmp_path / "a.json"), str(tmp_path / "b.json"))
    assert cfg.contract == "contract.json"
    assert cfg.strict_dq_threshold == 0
    assert dict(cfg.retention) == {"identity_days": 7, "request_days": 8, "event_days": 9,
                                   "rollup_days": 10}
    # every documented variable maps to a field
    assert {v.split(".")[0] for v in ENV_VARS.values()} == {f.name for f in
                                                           dataclasses.fields(Config)}


# ---------------------------------------------------------------------------------------------
# unknown keys and invalid values
# ---------------------------------------------------------------------------------------------


def test_unknown_file_key_is_rejected(home: Path) -> None:
    _write(_cwd_file(), {"k": 5, "shard_max": 3})
    with pytest.raises(UsageError, match="shard_max"):
        load_config(None, _env(home), {})


def test_unknown_override_key_is_rejected(home: Path) -> None:
    with pytest.raises(UsageError, match="nope"):
        load_config(None, _env(home), {"nope": 1})
    with pytest.raises(UsageError, match="is not a known key"):
        load_config(None, _env(home), {"other.sub": 1})
    with pytest.raises(UsageError, match="is not a known key"):
        load_config(None, _env(home), {3: 1})  # type: ignore[dict-item]


def test_unknown_retention_key_is_rejected(home: Path) -> None:
    _write(_cwd_file(), {"retention": {"identity_days": 1, "forever": 2}})
    with pytest.raises(UsageError, match="retention.forever"):
        load_config(None, _env(home), {})
    _cwd_file().unlink()
    with pytest.raises(UsageError, match="retention.bogus"):
        load_config(None, _env(home), {"retention.bogus": 3})


def test_unknown_env_variables_are_ignored(home: Path) -> None:
    assert load_config(None, _env(home, TOKENBILL_JBOS="4", TOKENBILL_="1"), {}) == Config()


@pytest.mark.parametrize("obj, key", [
    ({"k": 0}, "k"), ({"k": True}, "k"), ({"k": "x"}, "k"), ({"k": 1.5}, "k"),
    ({"jobs": -1}, "jobs"), ({"shard_max_requests": 0}, "shard_max_requests"),
    ({"sample_lanes": 2**60}, "sample_lanes"), ({"strict_dq_threshold": -1}, "strict_dq"),
    ({"min_usd": "-1"}, "min_usd"), ({"min_usd": "abc"}, "min_usd"), ({"min_usd": True}, "min_usd"),
    ({"min_usd": "1e-20"}, "min_usd"), ({"min_usd": "Infinity"}, "min_usd"),
    ({"identity_mode": "install"}, "identity_mode"), ({"identity_mode": 3}, "identity_mode"),
    ({"retention": [1]}, "retention"), ({"retention": {"identity_days": 0}}, "retention"),
    ({"thresholds": ["a"]}, "thresholds"), ({"thresholds": {"a": True}}, "thresholds.a"),
    ({"thresholds": {"a": None}}, "thresholds.a"), ({"thresholds": {"": "1"}}, "thresholds"),
    ({"name_allowlist": 3}, "name_allowlist"), ({"name_allowlist": ["ok", 4]}, "name_allowlist"),
    ({"residency_required": {"us": 1}}, "residency_required"),
    ({"key_file": ""}, "key_file"), ({"key_file": 5}, "key_file"),
    ({"contract": "a\nb"}, "contract"), ({"rates": 7}, "rates"), ({"rates": [""]}, "rates"),
    ({"k": None}, "k"), ({"schema": "other@9"}, "schema"),
])
def test_invalid_file_values(home: Path, obj: dict, key: str) -> None:
    _write(_cwd_file(), obj)
    with pytest.raises(UsageError, match=key):
        load_config(None, _env(home), {})


@pytest.mark.parametrize("env, var", [
    ({"TOKENBILL_K": "abc"}, "TOKENBILL_K"), ({"TOKENBILL_K": "-3"}, "TOKENBILL_K"),
    ({"TOKENBILL_JOBS": "1.5"}, "TOKENBILL_JOBS"),
    ({"TOKENBILL_THRESHOLDS": "[1]"}, "TOKENBILL_THRESHOLDS"),
    ({"TOKENBILL_THRESHOLDS": "{bad"}, "TOKENBILL_THRESHOLDS"),
    ({"TOKENBILL_THRESHOLDS": '{"a": NaN}'}, "TOKENBILL_THRESHOLDS"),
    ({"TOKENBILL_RETENTION_IDENTITY_DAYS": "0"}, "TOKENBILL_RETENTION_IDENTITY_DAYS"),
    ({"TOKENBILL_IDENTITY_MODE": "email"}, "identity_mode"),
    ({"TOKENBILL_KEY_FILE": " "}, "key_file"),
])
def test_invalid_env_values(home: Path, env: dict[str, str], var: str) -> None:
    with pytest.raises(UsageError, match=var):
        load_config(None, _env(home, **env), {})


def test_non_string_env_value_is_rejected(home: Path) -> None:
    with pytest.raises(UsageError, match="TOKENBILL_K"):
        load_config(None, {"HOME": str(home), "TOKENBILL_K": 5}, {})  # type: ignore[dict-item]


@pytest.mark.parametrize("overrides", [{"k": "zero"}, {"jobs": 0}, {"retention.event_days": -1},
                                       {"thresholds.a": 1.5}, {"rates": 3}])
def test_invalid_override_values(home: Path, overrides: dict) -> None:
    with pytest.raises(UsageError):
        load_config(None, _env(home), overrides)


def test_mapping_arguments_are_checked(home: Path) -> None:
    with pytest.raises(UsageError, match="overrides"):
        load_config(None, _env(home), [("k", 1)])  # type: ignore[arg-type]
    with pytest.raises(UsageError, match="environment"):
        load_config(None, [("HOME", "x")], {})  # type: ignore[arg-type]


@pytest.mark.parametrize("text, why", [("[]", "JSON object"), ("{", "invalid JSON"),
                                       ('{"k": NaN}', "invalid JSON"), ("\xff", "not UTF-8")])
def test_malformed_files(home: Path, text: str, why: str) -> None:
    path = _cwd_file()
    path.parent.mkdir(parents=True)
    path.write_bytes(text.encode("latin-1"))
    with pytest.raises(UsageError, match=why):
        load_config(None, _env(home), {})


def test_oversized_file(home: Path) -> None:
    _write(_cwd_file(), json.dumps({"thresholds": {"a": "x" * 100}}) + " " * (1 << 20))
    with pytest.raises(UsageError, match="larger than"):
        load_config(None, _env(home), {})


def test_errors_never_echo_the_value(home: Path) -> None:
    _write(_cwd_file(), {"min_usd": "SECRET-VALUE-42"})
    with pytest.raises(UsageError) as info:
        load_config(None, _env(home), {})
    assert "SECRET-VALUE-42" not in str(info.value)
    with pytest.raises(UsageError) as info:
        load_config(None, _env(home, TOKENBILL_JOBS="SECRET-VALUE-43"), {})
    assert "SECRET-VALUE-43" not in str(info.value)


# ---------------------------------------------------------------------------------------------
# values: types, numbers, paths, schema
# ---------------------------------------------------------------------------------------------


def test_json_numbers_and_decimals(home: Path) -> None:
    _write(_cwd_file(), '{"min_usd": 2.5, "thresholds": {"a": 0.25, "b": 3}, "k": 6}')
    cfg = load_config(None, _env(home), {})
    assert cfg.min_usd == "2.5" and dict(cfg.thresholds) == {"a": "0.25", "b": "3"}
    assert load_config(None, _env(home), {"min_usd": Decimal("0.10")}).min_usd == "0.10"
    assert load_config(None, _env(home), {"min_usd": 3}).min_usd == "3"
    assert load_config(None, _env(home), {"k": "9"}).k == 9  # digit strings from flags are fine


def test_file_paths_resolve_against_the_file(home: Path, tmp_path: Path) -> None:
    conf = _write(tmp_path / "etc" / "tb.json", {
        "key_file": "keys/org.key", "collection_key_file": "/abs/c.key",
        "contract": "~/contract.json", "rates": ["r1.json", "/abs/r2.json"]})
    cfg = load_config(conf, _env(home), {})
    base = conf.resolve().parent
    assert cfg.key_file == str(base / "keys" / "org.key")
    assert cfg.collection_key_file == "/abs/c.key"
    assert cfg.contract == str(Path("~/contract.json").expanduser())
    assert cfg.rates == (str(base / "r1.json"), "/abs/r2.json")
    # a single rates path and null paths are accepted too
    conf2 = _write(tmp_path / "etc" / "tb2.json", {"rates": "one.json", "key_file": None})
    cfg2 = load_config(conf2, _env(home), {})
    assert cfg2.rates == (str(base / "one.json"),) and cfg2.key_file is None


def test_cli_paths_and_rates(home: Path, tmp_path: Path) -> None:
    cfg = load_config(None, _env(home), {"rates": [tmp_path / "x.json", "y.json"],
                                         "key_file": tmp_path / "k", "contract": "c.json"})
    assert cfg.rates == (str(tmp_path / "x.json"), "y.json")
    assert cfg.key_file == str(tmp_path / "k") and cfg.contract == "c.json"
    assert load_config(None, _env(home), {"rates": "single.json"}).rates == ("single.json",)


def test_schema_key_is_accepted(home: Path) -> None:
    _write(_cwd_file(), {"schema": CONFIG_SCHEMA, "k": 8})
    assert load_config(None, _env(home), {}).k == 8


def test_config_json_round_trip(home: Path, tmp_path: Path) -> None:
    cfg = load_config(None, _env(home), {
        "k": 7, "min_usd": "0.50", "jobs": 4, "identity_mode": "two-stage",
        "retention.identity_days": 30, "thresholds": {"policy.ttl.payments": "1h"},
        "name_allowlist": ["github", "Bash"], "residency_required": ["us"],
        "key_file": str(tmp_path / "org.key"), "rates": [str(tmp_path / "r.json")],
        "contract": str(tmp_path / "c.json"), "strict_dq_threshold": 3})
    data = config_json(cfg)
    assert list(data)[0] == "schema" and data["name_allowlist"] == ["Bash", "github"]
    path = _write(tmp_path / "round.json", data)
    assert load_config(path, _env(home), {}) == cfg
    assert json.loads(json.dumps(data)) == data  # plain JSON types only


# ---------------------------------------------------------------------------------------------
# the Config object
# ---------------------------------------------------------------------------------------------


def test_config_is_frozen_hashable_and_picklable() -> None:
    cfg = Config(k=6, thresholds={"a": "1"}, name_allowlist=frozenset({"x"}))
    assert hash(cfg) == hash(Config(k=6, thresholds={"a": "1"}, name_allowlist=frozenset({"x"})))
    assert pickle.loads(pickle.dumps(cfg)) == cfg
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.k = 7  # type: ignore[misc]
    assert isinstance(cfg.thresholds, FrozenMap) and isinstance(cfg.retention, FrozenMap)


def test_direct_construction_is_validated_and_normalized() -> None:
    cfg = Config(retention={"identity_days": 30}, thresholds={"x": 1},  # type: ignore[arg-type]
                 name_allowlist=["b", "a"], residency_required=["us"],  # type: ignore[arg-type]
                 rates="one.json", key_file=Path("/k"))  # type: ignore[arg-type]
    assert dict(cfg.retention) == {**DEFAULT_RETENTION, "identity_days": 30}
    assert dict(cfg.thresholds) == {"x": "1"}
    assert cfg.name_allowlist == frozenset({"a", "b"}) and cfg.residency_required == ("us",)
    assert cfg.rates == ("one.json",) and cfg.key_file == "/k"
    with pytest.raises(UsageError, match="k"):
        Config(k=0)
    with pytest.raises(UsageError, match="retention"):
        Config(retention={"never": 1})
    assert Config(residency_required=frozenset({"us", "eu"})).residency_required == ("eu", "us")


def test_frozen_map() -> None:
    m = FrozenMap({"b": 2, "a": 1})
    assert list(m) == ["a", "b"] and len(m) == 2 and m["a"] == 1
    assert m == {"a": 1, "b": 2} and m != {"a": 1} and (m == 3) is False
    assert hash(m) == hash(FrozenMap([("a", 1), ("b", 2)]))
    assert repr(m) == "FrozenMap({'a': 1, 'b': 2})"
    assert pickle.loads(pickle.dumps(m)) == m
    with pytest.raises(AttributeError):
        m.x = 1  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        m["c"] = 3  # type: ignore[index]
