"""``build_env`` (injected FakePricer factory, keys, defaults), the lazy RateCard loader and
``open_store`` (SPEC §15 ``pipeline/common.py``)."""

from __future__ import annotations

import os
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

from tokenbill.config import Config
from tokenbill.core.cache_rules import RulesTable
from tokenbill.core.errors import ContractViolation, PricingError, PrivacyError, UsageError
from tokenbill.core.ids import key_id
from tokenbill.core.testing import FakePricer
from tokenbill.pipeline import common
from tokenbill.pipeline.common import Env, build_env, load_rate_card, open_store

from .support import NAME_KEY, ORG_KEY, NoAdoptStore, PathStore, make_env, write_key


class _Factory:
    """Records the arguments of each call and returns a FakePricer."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kw: Any) -> FakePricer:
        self.calls.append(kw)
        return FakePricer()


# ---------------------------------------------------------------------------------------------
# build_env
# ---------------------------------------------------------------------------------------------


def test_build_env_with_injected_fake_pricer() -> None:
    factory = _Factory()
    env = build_env(Config(k=7, jobs=3), pricer_factory=factory, now_ms=123)
    assert isinstance(env.pricer, FakePricer) and isinstance(env.rules, RulesTable)
    assert (env.k, env.jobs, env.now_ms) == (7, 3, 123)
    assert env.org_key is None and env.name_key is None and env.name_key_id is None
    assert factory.calls == [{"rates": (), "contract": None, "model_prices": ()}]


def test_build_env_default_clock() -> None:
    before = time.time_ns() // 1_000_000
    env = build_env(Config(), pricer_factory=_Factory())
    assert before <= env.now_ms <= time.time_ns() // 1_000_000


def test_rate_arguments_explicit_win_over_config(tmp_path: Path) -> None:
    factory = _Factory()
    cfg = Config(rates=(str(tmp_path / "cfg.json"),), contract=str(tmp_path / "cfg-contract.json"))
    build_env(cfg, pricer_factory=factory, now_ms=0)
    build_env(cfg, rates=[tmp_path / "flag.json"], contract=tmp_path / "flag-contract.json",
              model_prices=[("my-model", " 3.00", "15 ")], pricer_factory=factory, now_ms=0)
    assert factory.calls[0] == {"rates": (tmp_path / "cfg.json",),
                                "contract": tmp_path / "cfg-contract.json", "model_prices": ()}
    assert factory.calls[1] == {"rates": (tmp_path / "flag.json",),
                                "contract": tmp_path / "flag-contract.json",
                                "model_prices": (("my-model", "3.00", "15"),)}


@pytest.mark.parametrize("spec", [("m", "1"), ("m", "1", 2), ["", "1", "2"], "m=1,2", None])
def test_model_price_shape(spec: Any) -> None:
    with pytest.raises(UsageError, match="model-price"):
        build_env(Config(), model_prices=[spec], pricer_factory=_Factory(), now_ms=0)


def test_keys_org_only_names_with_the_org_key(tmp_path: Path) -> None:
    org = write_key(tmp_path / "org.key", ORG_KEY)
    env = build_env(Config(key_file=str(org)), pricer_factory=_Factory(), now_ms=0)
    assert env.org_key == ORG_KEY and env.name_key == ORG_KEY
    assert env.name_key_id == key_id(ORG_KEY)


def test_keys_collection_key_is_the_name_key(tmp_path: Path) -> None:
    org = write_key(tmp_path / "org.key", ORG_KEY)
    coll = write_key(tmp_path / "coll.key", NAME_KEY)
    env = build_env(Config(), key_file=org, collection_key_file=coll, pricer_factory=_Factory(),
                    now_ms=0)
    assert env.org_key == ORG_KEY and env.name_key == NAME_KEY
    assert env.name_key_id == key_id(NAME_KEY)
    # the collection key alone: names only, no org key
    env2 = build_env(Config(collection_key_file=str(coll)), pricer_factory=_Factory(), now_ms=0)
    assert env2.org_key is None and env2.name_key == NAME_KEY


def test_explicit_key_file_wins_over_config(tmp_path: Path) -> None:
    a = write_key(tmp_path / "a.key", ORG_KEY)
    b = write_key(tmp_path / "b.key", NAME_KEY)
    env = build_env(Config(key_file=str(a)), key_file=b, pricer_factory=_Factory(), now_ms=0)
    assert env.org_key == NAME_KEY


def test_repr_never_shows_key_material(tmp_path: Path) -> None:
    env = make_env()
    text = repr(env)
    for key in (ORG_KEY, NAME_KEY):
        assert key.hex() not in text and repr(key) not in text
    assert "name_key_id" in text


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_group_readable_key_is_refused(tmp_path: Path) -> None:
    key = write_key(tmp_path / "loose.key", ORG_KEY)
    os.chmod(key, 0o644)
    with pytest.raises(PrivacyError):
        build_env(Config(key_file=str(key)), pricer_factory=_Factory(), now_ms=0)


def test_missing_or_bad_key_file(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        build_env(Config(key_file=str(tmp_path / "nope.key")), pricer_factory=_Factory(), now_ms=0)
    with pytest.raises(UsageError, match="collection key file"):
        build_env(Config(), collection_key_file=" ", pricer_factory=_Factory(),  # type: ignore
                  now_ms=0)


def test_bad_inputs() -> None:
    with pytest.raises(UsageError, match="Config"):
        build_env({"k": 5}, pricer_factory=_Factory())  # type: ignore[arg-type]
    for bad in (-1, True, "5"):
        with pytest.raises(UsageError, match="now_ms"):
            build_env(Config(), pricer_factory=_Factory(), now_ms=bad)  # type: ignore[arg-type]
    with pytest.raises(ContractViolation, match="Pricer"):
        build_env(Config(), pricer_factory=lambda **kw: object(), now_ms=0)


def test_env_is_frozen() -> None:
    env = make_env()
    with pytest.raises(AttributeError):
        env.k = 9  # type: ignore[misc]
    assert isinstance(env, Env)


# ---------------------------------------------------------------------------------------------
# the default factory: RATES loaded lazily
# ---------------------------------------------------------------------------------------------


def _install_fake_rates(monkeypatch: pytest.MonkeyPatch, *, contract: bool = True
                        ) -> dict[str, list[Any]]:
    log: dict[str, list[Any]] = {"cards": [], "files": [], "contracts": []}
    schema = types.ModuleType("tokenbill.rates.schema")
    schema.load_builtin = lambda provider=None: "builtin"  # type: ignore[attr-defined]

    def load_file(path: Path, name: str) -> str:
        log["files"].append((path, name))
        return name

    schema.load_file = load_file  # type: ignore[attr-defined]
    schema.model_price_layer = lambda specs: ("model-price", specs)  # type: ignore[attr-defined]
    engine = types.ModuleType("tokenbill.rates.engine")

    class RateCard(FakePricer):
        def __init__(self, layers: list[Any], contract: Any = None) -> None:
            super().__init__()
            log["cards"].append((list(layers), contract))

    engine.RateCard = RateCard  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tokenbill.rates.schema", schema)
    monkeypatch.setitem(sys.modules, "tokenbill.rates.engine", engine)
    if contract:
        module = types.ModuleType("tokenbill.rates.contract")

        def load_contract(path: Path) -> str:
            log["contracts"].append(path)
            return f"overlay:{Path(path).name}"

        module.load_contract = load_contract  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "tokenbill.rates.contract", module)
    else:
        monkeypatch.setitem(sys.modules, "tokenbill.rates.contract", None)
    return log


def test_default_factory_layers_lowest_precedence_first(monkeypatch: pytest.MonkeyPatch,
                                                        tmp_path: Path) -> None:
    log = _install_fake_rates(monkeypatch)
    env = build_env(Config(), rates=[tmp_path / "a.json", tmp_path / "b.json"],
                    contract=tmp_path / "deal.json", model_prices=[("m", "1", "2")], now_ms=0)
    assert type(env.pricer).__name__ == "RateCard"
    layers, overlay = log["cards"][0]
    assert layers == ["builtin", "user:a.json", "user:b.json", ("model-price", (("m", "1", "2"),))]
    assert overlay == "overlay:deal.json" and log["contracts"] == [tmp_path / "deal.json"]
    assert load_rate_card() is not None and log["cards"][-1] == (["builtin"], None)


def test_default_factory_without_rates_raises_pricing_error(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "tokenbill.rates.schema", None)
    with pytest.raises(PricingError, match="not installed"):
        build_env(Config(), now_ms=0)


def test_default_factory_without_contract_loader(monkeypatch: pytest.MonkeyPatch,
                                                 tmp_path: Path) -> None:
    _install_fake_rates(monkeypatch, contract=False)
    assert load_rate_card(rates=[tmp_path / "x.json"]) is not None  # no contract: fine
    with pytest.raises(PricingError, match="contract"):
        load_rate_card(contract=tmp_path / "c.json")


# ---------------------------------------------------------------------------------------------
# open_store
# ---------------------------------------------------------------------------------------------


def test_open_store_passes_keys_pricer_and_adoption(monkeypatch: pytest.MonkeyPatch,
                                                    tmp_path: Path) -> None:
    monkeypatch.setattr(common, "STORE_CLASS", "v2.wiring.support:PathStore")
    PathStore.opened.clear()
    env = make_env()
    store = open_store(tmp_path / "ledger.db", env)
    assert isinstance(store, PathStore) and store.path == tmp_path / "ledger.db"
    opened = PathStore.opened[-1]
    assert opened["create"] is True and opened["org_key"] == ORG_KEY
    assert opened["name_key_id"] == key_id(NAME_KEY) and opened["pricer"] is env.pricer
    assert opened["adopt_key_ids"] is False and opened["read_only"] is False
    assert common._store_path(store) == tmp_path / "ledger.db"
    open_store(tmp_path / "ledger2.db", env, adopt_key_ids=True)
    assert PathStore.opened[-1]["adopt_key_ids"] is True


def test_adoption_keyword_only_when_asked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(common, "STORE_CLASS", "v2.wiring.support:NoAdoptStore")
    store = open_store(tmp_path / "old.db", make_env())  # a store without adopt_key_ids works
    assert isinstance(store, NoAdoptStore)
    with pytest.raises(UsageError, match="adoption"):
        open_store(tmp_path / "old2.db", make_env(), adopt_key_ids=True)


def test_open_store_create_false_and_missing_class(monkeypatch: pytest.MonkeyPatch,
                                                   tmp_path: Path) -> None:
    monkeypatch.setattr(common, "STORE_CLASS", "v2.wiring.support:PathStore")
    with pytest.raises(UsageError, match="not found"):
        open_store(tmp_path / "missing.db", make_env(), create=False)
    monkeypatch.setattr(common, "STORE_CLASS", "v2.wiring.no_such_store:Store")
    with pytest.raises(UsageError, match="not installed"):
        open_store(tmp_path / "x.db", make_env())


def test_store_that_is_not_weak_referenceable(monkeypatch: pytest.MonkeyPatch,
                                              tmp_path: Path) -> None:
    monkeypatch.setattr(common, "STORE_CLASS", "v2.wiring.support:SlotStore")
    store = open_store(tmp_path / "slots.db", make_env())
    assert type(store).__name__ == "SlotStore" and common._store_path(store) is None


def test_store_path_fallbacks() -> None:
    class Plain:
        __slots__ = ("db_path",)

        def __init__(self) -> None:
            self.db_path = "/tmp/x.db"

    assert common._store_path(Plain()) == Path("/tmp/x.db")   # not weak-referenceable
    assert common._store_path(object()) is None
