"""Regressions for the adversarial review of SYNTH-FLEET: source files and canonical records agree
where the SPEC fixes the mapping (workspace ids, Claude Code session keys, headless timestamps,
subagent appended items), CANARY sits in every transcript content field, SPEC §18's single
unknown-model call, argument and scale-world errors stay ``UsageError``, and scale mode grows its
population."""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path

import pytest

from tokenbill.core.builders import CANARY
from tokenbill.core.errors import UsageError
from tokenbill.core.ids import stable_id
from tokenbill.core.records import LaneKind
from tokenbill.synth import fleet as F
from tokenbill.synth import writers as W


def test_workspace_ids_are_clear_and_allowlisted(world: F.FleetWorld) -> None:
    assert F.FLEET_WORKSPACES == frozenset(world.hints.workspaces.values())
    opts = F.fleet_ingest_options()
    assert opts.name_allowlist == F.FLEET_WORKSPACES
    for agg in world.aggregates:
        if agg.source_kind == "anthropic.usage_report":
            assert dict(agg.dims)["workspace_id"] in F.FLEET_WORKSPACES
    for line in world.cost_lines:
        if line.source_kind == "anthropic.cost_report":
            assert line.workspace_id in F.FLEET_WORKSPACES
    for req in world.requests:
        ws = req.attribution.workspace_id
        assert ws is None or ws in F.FLEET_WORKSPACES


def test_world_ingest_options_carry_today_and_team_map(world: F.FleetWorld) -> None:
    opts = world.ingest_options(k_anonymity=7)
    today_ms = (_dt.date.fromisoformat(world.today) - _dt.date(1970, 1, 1)).days * 86_400_000
    assert opts.now_ms == today_ms and opts.k_anonymity == 7
    assert opts.team_map == world.truth.team_map
    assert opts.identity_mode == "central-ingest"
    assert world.ingest_options(now_ms=1).now_ms == 1


def test_claude_code_session_keys_follow_the_importer_rule(world: F.FleetWorld) -> None:
    """SPEC §5.3 #11: ``stable_id("ses", "claude-code", sessionId)`` for every Claude Code session,
    headless CI runs included; lanes ``stable_id("ln", sessionId, agent or "main")``."""
    hints = world.hints
    by_key = {s.session_key: s for s in world.sessions}
    ci = [sk for sk in hints.ci_runs]
    assert ci
    for sk in ci:
        native = hints.session_native[sk]
        assert sk == stable_id("ses", "claude-code", native)
        assert by_key[sk].source_kind == "claude-code-headless"
        (lane,) = by_key[sk].lanes
        assert lane.lane_key == stable_id("ln", native, "main")


def test_every_subagent_request_carries_its_appended_item(world: F.FleetWorld) -> None:
    subagents = [ln for ln in world.lanes() if ln.kind in (LaneKind.SUBAGENT,
                                                            LaneKind.WORKFLOW_AGENT)]
    assert subagents
    for lane in subagents:
        kinds = [tuple(a.kind for a in q.appended) for q in lane.requests]
        assert kinds[0] == ("user_text",) and all(k == ("tool_result",) for k in kinds[1:])


def _transcript_lines(world: F.FleetWorld) -> list[dict]:
    out = []
    for key, path in world.source_files.items():
        if key.startswith("claude-code/") and key.endswith(".jsonl"):
            out.extend(json.loads(line) for line in path.read_text("utf-8").splitlines())
    return out


def test_transcript_sizes_come_from_the_canonical_appended_items(world: F.FleetWorld) -> None:
    """The writer serialises the canonical sizes (no invented defaults): the user entries written
    before a request's first assistant line are exactly its appended items (kind and UTF-8
    size), on main and subagent lanes alike."""
    by_msg = {q.final_attempt.provider_message_id: q for q in world.requests
              if q.session_key in set(world.hints.transcript_sessions)}
    checked = 0
    for key, path in world.source_files.items():
        if not (key.startswith("claude-code/") and key.endswith(".jsonl")):
            continue
        pending: list[tuple[str, int]] = []
        seen: set[str] = set()
        for raw in path.read_text("utf-8").splitlines():
            line = json.loads(raw)
            if line["type"] == "user" and not line.get("isCompactSummary"):
                content = line["message"]["content"]
                if isinstance(content, str):
                    pending.append(("user_text", len(content.encode("utf-8"))))
                else:
                    pending.extend(("tool_result", len(b["content"].encode("utf-8")))
                                   for b in content)
            elif line["type"] == "assistant" and line["message"]["id"] not in seen:
                seen.add(line["message"]["id"])
                req = by_msg[line["message"]["id"]]
                assert pending == [(a.kind, a.n_bytes) for a in req.appended], key
                pending = []
                checked += 1
    assert checked == sum(1 for q in by_msg.values() if q.serving_inference is not None)


def test_canary_in_every_transcript_content_field(world: F.FleetWorld) -> None:
    lines = _transcript_lines(world)
    boundaries = [ln for ln in lines if ln.get("subtype") == "compact_boundary"]
    assert boundaries and all(CANARY in ln["content"] for ln in boundaries)
    results = [ln["toolUseResult"] for ln in lines if "toolUseResult" in ln]
    assert results and all(CANARY in r["stdout"] and CANARY in r["stderr"] for r in results)


def test_headless_messages_are_all_timed(world: F.FleetWorld) -> None:
    """SPEC §5.12 #2: a stream is timed only when every user/assistant/result message carries a
    timestamp; the result is stamped at the end of the run."""
    paths = [p for k, p in world.source_files.items() if k.startswith("claude-code-headless/")]
    assert paths
    for path in paths:
        msgs = json.loads(path.read_text("utf-8"))
        timed = [m for m in msgs if m["type"] in ("user", "assistant", "result")]
        assert all(isinstance(m.get("timestamp"), str) for m in timed)
        assert msgs[-1]["type"] == "result"
        assert msgs[-1]["timestamp"] >= max(m["timestamp"] for m in timed[:-1])


def test_exactly_one_call_on_the_unknown_model(world: F.FleetWorld) -> None:
    unpriced = [q for q in world.requests if q.model == "claude-sonnet-5-5"]
    assert len(unpriced) == 1
    assert world.truth.recon.unpriced_models == ("claude-sonnet-5-5",)
    assert unpriced[0].session_key in world.hints.transcript_sessions


def test_same_tier_plants_follow_the_catalog(world: F.FleetWorld) -> None:
    from tokenbill.core.catalog import successor

    plants = world.truth.plants_for(kind="same-tier-upgrade")
    assert plants
    for plant in plants:
        model = dict(plant.scope)["model"]
        assert plant.detail("successor") == successor(model)
        assert plant.policy == f"model={successor(model)}@model:{model}"


def test_out_dir_errors_are_usage_errors(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(UsageError):
        F.generate(seed=1, days=7, out_dir=blocker)
    with pytest.raises(UsageError):
        F.generate(seed=1, days=7, out_dir=5)  # type: ignore[arg-type]


def test_writers_refuse_scale_worlds(tmp_path: Path) -> None:
    world = F.generate(seed=2, scale_requests=50)
    for writer in (W.write_cc_transcripts, W.write_headless_streams, W.write_otlp,
                   W.write_trace_v2_fingerprint, W.write_admin_pages, W.write_cur_csv,
                   W.write_all):
        with pytest.raises(UsageError):
            writer(world, tmp_path)


def test_scale_mode_grows_the_population() -> None:
    world = F.generate(seed=5, scale_requests=60_000)       # more than two epochs
    principals = {q.attribution.principal for q in world.requests}
    assert len(principals) > 61
    one_epoch = F.generate(seed=5, scale_requests=5_000)
    assert len({q.attribution.principal for q in one_epoch.requests}) <= 61


def test_team_sizes_are_arithmetic_and_bounded() -> None:
    for devs in (61, 62, 71, 72, 1_000, F.MAX_DEVS):
        sizes = F.team_sizes(devs)
        assert sum(sizes.values()) == devs and sizes["tiny"] == 3
        grown = [sizes[t.name] - t.devs for t in F.TEAMS if t.name != "tiny"]
        assert max(grown) - min(grown) <= 1 and grown == sorted(grown, reverse=True)
    with pytest.raises(UsageError):
        F.team_sizes(F.MAX_DEVS + 1)
    with pytest.raises(UsageError):
        F.generate(devs=10**12)


def test_quota_state_and_workflow_agents_are_written(world: F.FleetWorld,
                                                     tmp_path: Path) -> None:
    """``write_cc_transcripts`` on any session: QUOTA_STATE → ``quotaLimits`` on the next
    assistant entry (SPEC §5.3 #8), workflow agents under ``<session>/workflows/<run>/``."""
    hints = world.hints
    overage = sorted({q.session_key for q in world.requests
                      if q.attribution.billing_path == "usage_credits"})
    workflow = sorted({ln.session_key for ln in world.lanes("data")
                       if ln.kind is LaneKind.WORKFLOW_AGENT})[:2]
    assert overage and workflow and hints.overage_days
    files = W.write_cc_transcripts(world, tmp_path, [overage[0], *workflow])
    quota = []
    for key, path in files.items():
        if key.endswith(".jsonl"):
            for raw in path.read_text("utf-8").splitlines():
                line = json.loads(raw)
                if "quotaLimits" in line:
                    assert line["type"] == "assistant"
                    quota.append(line["quotaLimits"])
    assert quota == [{"status": "allowed", "rateLimitType": "seven_day",
                      "isUsingOverage": True, "overageStatus": "allowed",
                      "resetsAt": quota[0]["resetsAt"]}]
    assert isinstance(quota[0]["resetsAt"], int)
    wf = [k for k in files if "/workflows/" in k]
    assert wf and all("/subagents/" not in k for k in wf)
    metas = [json.loads(files[k].read_text()) for k in wf if k.endswith(".meta.json")]
    assert metas and all(set(m) == {"agentType", "spawnDepth", "workflowPhase"} for m in metas)
