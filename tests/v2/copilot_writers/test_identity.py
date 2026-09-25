"""Identity, labels and input normalization of the CP-SYNTH-W writers."""

from __future__ import annotations

from pathlib import Path

import pytest

from tokenbill.core import facts
from tokenbill.core.builders import CANARY_LOGIN, make_ai_usage_row, make_principal
from tokenbill.core.errors import UsageError
from tokenbill.core.ids import pseudonym
from tokenbill.core.models import normalize_copilot_model
from tokenbill.core.records import COPILOT_PSEUDO
from tokenbill.core.types import IngestResult, SourceInfo
from tokenbill.synth import copilot_writers as w

from .world import World, build_world, team_of


def test_canary_login_is_a_mapped_user(ident: w.Identity) -> None:
    team_map = ident.team_map()
    assert team_map[CANARY_LOGIN] in {"platform", "infra", "ops", "tiny"}
    logins = [ident.login(p) for p in ident.logins]
    assert len(logins) == len(set(logins))
    assert all(x == x.lower() for x in logins)
    ids = list(ident.user_ids.values())
    assert len(ids) == len(set(ids)) and all(10**7 <= i < 10**8 for i in ids)


def test_identity_maps_and_members(world: World, ident: w.Identity) -> None:
    teams = team_of(world)
    assert dict(ident.teams) == {p: t for p, t in teams.items()}
    assert set(ident.members("tiny")) == set(world.members["tiny"])
    assert ident.cost_center_map()[ident.login(world.members["infra"][0])] == "cc-infra"
    assert ident.id_team_map()[str(ident.user_id(world.members["ops"][0]))] == "ops"
    assert ident.login(None) == "" and ident.user_id(None) == 0
    stranger = make_principal("stranger")
    assert ident.login(stranger).startswith("dev-") and ident.user_id(stranger) > 0


def test_given_logins_win(world: World) -> None:
    p = world.members["ops"][0]
    ident = w.build_identity(world, logins={p: "octo-ops"})
    assert ident.login(p) == "octo-ops"
    assert CANARY_LOGIN not in ident.team_map()


def test_world_logins_mapping_and_keyed_team_map() -> None:
    base = build_world()
    p = base.members["infra"][1]
    world = build_world(logins={p: "mona-infra"})
    assert w.build_identity(world).login(p) == "mona-infra"

    class Keyed:
        def __init__(self, records: object, team_map: dict[str, str], key: bytes) -> None:
            self.records, self.team_map, self.principal_key = records, team_map, key

    key = bytes(range(16, 48))
    principal = pseudonym(key, "p", "hubot")
    line, agg = make_ai_usage_row(principal=principal, team="core")
    keyed = Keyed([line, agg], {"Hubot": "core"}, key)
    assert w.build_identity(keyed).login(principal) == "Hubot"


def test_team_map_file_round_trip(full: tuple[Path, dict[str, Path]], ident: w.Identity) -> None:
    root, files = full
    team, cc, ids = w.read_team_map(files["admin/team_map.csv"])
    assert team == ident.team_map()
    assert cc == ident.cost_center_map()
    assert ids == ident.id_team_map()
    header = files["admin/team_map.csv"].read_text().splitlines()[0]
    assert header == "login,team,cost_center,user_id"


def test_model_labels_round_trip() -> None:
    models = sorted({r.model for r in facts.copilot_rates()})
    for model in models:
        for routing in ("direct", "auto"):
            for speed in ("standard", "fast"):
                cm = normalize_copilot_model(w.model_label(model, routing, speed))
                assert (cm.model, cm.routing, cm.speed, cm.pseudo) == (model, routing, speed,
                                                                         None)
    for pseudo in COPILOT_PSEUDO:
        cm = normalize_copilot_model(w.model_label("", "direct", "standard", pseudo))
        assert cm.pseudo == pseudo
    assert normalize_copilot_model(w.model_label("", "auto", None, "code_review")).routing == (
        "auto")
    assert w.model_label(None) == "Unknown"
    assert w.model_label("Claude Opus 4.8".lower().replace(" ", "-").replace(".", "-")) == (
        "Claude Opus 4.8")


def test_names_are_deterministic() -> None:
    assert w.repo_name("h_0123456789abcdef0123", "org-a") == "org-a/service-01234567"
    assert w.repo_name(None) == ""
    assert w.workflow_file("h_0123456789abcdef0123").endswith(".lock.yml")
    assert w.raw_session_id("ses_x") == w.raw_session_id("ses_x")
    assert len(w.raw_session_id("ses_x")) == 36


def test_input_containers(world: World, tmp_path: Path) -> None:
    flat = [r for kind in world.records.values() for r in kind]
    from_world = w.write_world(world, tmp_path / "a", handoff="api")
    from_dict = w.write_world(world.records, tmp_path / "b", handoff="api")
    from_list = w.write_world(flat, tmp_path / "c", handoff="api")
    wrapped = w.write_world({"records": world.records}, tmp_path / "d", handoff="api")
    for other in (from_dict, from_list, wrapped):
        assert sorted(other) == sorted(from_world)
        for rel in from_world:
            assert other[rel].read_bytes() == from_world[rel].read_bytes(), rel
    result = IngestResult(source=SourceInfo(source_id="s_x", adapter="x", name_hmac="h_x",
                                            sha256="0", bytes=0, name_key_id=None,
                                            principal_key_id=None),
                          requests=[], sessions=[], events=[],
                          aggregates=list(world.records["aggregates"]),
                          cost_lines=list(world.records["cost_lines"]), outcomes=[],
                          quarantined=[], notes=[], stats={}, capabilities=frozenset())
    got = w.write_ai_usage_csv(result, tmp_path / "e", identity=w.build_identity(world),
                               revision_pair=False)
    assert got["billing/ai_usage_report_excl.csv"].read_bytes() == (
        from_world["billing/ai_usage_report_excl.csv"].read_bytes())


def test_bad_inputs(tmp_path: Path) -> None:
    with pytest.raises(UsageError):
        w.write_world("not records", tmp_path)
    with pytest.raises(UsageError):
        w.write_world([object()], tmp_path)
    with pytest.raises(UsageError):
        w.write_world([], tmp_path, handoff="email")
    with pytest.raises(UsageError):
        w.write_world([], tmp_path, conventions=())
    with pytest.raises(UsageError):
        w.write_ai_usage_csv([], tmp_path, conventions=("both",))
    with pytest.raises(UsageError):
        w.write_otel_file([], tmp_path, dialect="zipkin")
    with pytest.raises(UsageError):
        w.write_admin_answers([], tmp_path, variant="plan_maybe")
    nested: list = []
    for _ in range(8):
        nested = [nested]
    with pytest.raises(UsageError):
        w.write_world(nested, tmp_path)


def test_empty_world_writes_admin_files_only(tmp_path: Path) -> None:
    files = w.write_world([], tmp_path)
    assert sorted(files) == ["MANIFEST.json", "admin/answers.json", "admin/team_map.csv",
                             "billing/ai_usage_report_excl.csv"]
