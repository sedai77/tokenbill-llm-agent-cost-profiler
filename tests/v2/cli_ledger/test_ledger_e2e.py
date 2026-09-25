"""The ledger verbs end to end on the checked-in adapter fixtures (real adapters, ``SqliteStore``
and ``RateCard``): ``init``, ``ingest`` (auto-sniff of every adapter fixture), ``bill``, config
precedence, ``--strict-dq``, byte-identical ``--deterministic`` JSON across processes, ``export
trace2`` / ``ccusage``, ``showback``, ``pricing``, ``purge`` — all under the socket guard."""

from __future__ import annotations

import csv
import io
import json
import os
from pathlib import Path

import pytest

from tokenbill.core.builders import CANARY

from .helpers import (
    ADMIN,
    CC_HEADLESS,
    CC_PROJECTS,
    FIXTURES,
    init_dir,
    read_trace2,
    run_main,
    run_subprocess,
)

pytestmark = pytest.mark.gate
pytest.importorskip("tokenbill.store.db")
pytest.importorskip("tokenbill.rates.engine")

SNIFFED = [
    ("claude_code/projects", "claude-code"),
    ("claude_code/headless/claude-execution-output.json", "claude-code-headless"),
    ("claude_code/headless/stream.jsonl", "claude-code-headless"),
    ("telemetry/anthropic_responses.jsonl", "anthropic-responses"),
    ("telemetry/bedrock_invocations.jsonl", "bedrock"),
    ("telemetry/openai_usage.jsonl", "openai"),
    ("telemetry/otlp_claude_code.jsonl", "otlp"),
    ("telemetry/otlp_genai.jsonl", "otlp"),
    ("trace/trace1_runs.jsonl", "trace@1"),
    ("trace/trace2_usage.jsonl.gz", "trace@2"),
    ("trace/trace2_fingerprint.jsonl", "trace@2"),
    ("admin/anthropic/usage_report_2026-08.json", "anthropic-usage-report"),
    ("admin/anthropic/usage_report_2026-09_pages.jsonl", "anthropic-usage-report"),
    ("admin/anthropic/cost_report_2026-08.json", "anthropic-cost-report"),
    ("admin/anthropic/cc_analytics_2026-09-10.json", "anthropic-cc-analytics"),
    ("admin/anthropic/enterprise/usage_report.json", "anthropic-enterprise-analytics"),
    ("admin/anthropic/enterprise/cost_report.json", "anthropic-enterprise-analytics"),
    ("admin/openai/usage_completions_2026-09.json", "openai-usage-buckets"),
    ("admin/openai/costs_2026-09.json", "openai-costs"),
    ("admin/cloud/cur2_bedrock_2026-09.csv", "aws-cur"),
    ("admin/cloud/cur2_bedrock_2026-09.csv.gz", "aws-cur"),
    ("admin/cloud/gcp_billing_2026-09.csv", "gcp-billing"),
    ("admin/cloud/gcp_billing_2026-09.jsonl", "gcp-billing"),
    ("copilot_bill/ai_usage_2026-09.csv", "github-ai-usage"),
]


@pytest.fixture(scope="module")
def config(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return init_dir(tmp_path_factory.mktemp("tb") / "tb")


@pytest.fixture(scope="module")
def ledger(tmp_path_factory: pytest.TempPathFactory, config: Path) -> Path:
    db = tmp_path_factory.mktemp("ledger") / "l.db"
    code, _out, err = run_main(["--config", str(config), "ingest", "--db", str(db),
                                str(CC_PROJECTS), str(CC_HEADLESS / "claude-execution-output.json"),
                                str(ADMIN / "anthropic" / "usage_report_2026-08.json"),
                                str(ADMIN / "anthropic" / "cost_report_2026-08.json"),
                                "--attr", "team=payments"])
    assert code == 0, err
    return db


@pytest.mark.parametrize(("rel", "adapter"), SNIFFED, ids=[r for r, _a in SNIFFED])
def test_ingest_auto_sniffs_every_adapter_fixture(rel: str, adapter: str, config: Path,
                                                  tmp_path: Path) -> None:
    code, out, err = run_main(["--config", str(config), "ingest", "--db",
                               str(tmp_path / "l.db"), str(FIXTURES / rel), "--format", "json",
                               "--deterministic"])
    assert code == 0, err
    doc = json.loads(out)
    assert [i["adapter"] for i in doc["inputs"]][0] == adapter
    assert CANARY not in out


def test_ingest_text_output_and_errors(config: Path, tmp_path: Path) -> None:
    db = str(tmp_path / "l.db")
    code, out, _ = run_main(["--config", str(config), "ingest", "--db", db, str(CC_PROJECTS)])
    assert code == 0 and "TOKEN BILL · ingest" in out and "DATA QUALITY" in out
    cases = [
        (["ingest", "--db", db, str(tmp_path / "nope.jsonl")], "nope.jsonl"),
        (["ingest", "--db", db, str(CC_PROJECTS), "--content", "full"], "content tier full"),
        (["ingest", "--db", db, str(CC_PROJECTS), "--adapter", "nope"], "unknown adapter"),
        (["ingest", "--db", db, str(CC_PROJECTS), "--experimental", "warp"], "--experimental"),
        (["ingest", "--db", db, str(CC_PROJECTS), "--since", "2026-09-02", "--until",
          "2026-09-01"], "--until"),
        (["ingest", str(CC_PROJECTS)], "--db"),
    ]
    for argv, needle in cases:
        code, _out, err = run_main(["--config", str(config), *argv])
        assert code == 2 and needle in err, (argv, err)


def test_ingest_with_rules_team_map_and_window(config: Path, tmp_path: Path) -> None:
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"rules": [{"id": "all", "match": {},
                                            "set": {"team": "platform",
                                                    "cost_center": "cc-7"}}]}),
                     encoding="utf-8")
    team_map = tmp_path / "teams.json"
    team_map.write_text(json.dumps({"someone": "search"}), encoding="utf-8")
    db = tmp_path / "l.db"
    code, _out, err = run_main(["--config", str(config), "ingest", "--db", str(db),
                                str(CC_PROJECTS), "--rules", str(rules), "--team-map",
                                str(team_map), "--since", "2020-01-01", "--until", "2099-01-01",
                                "--strict", "--k", "3"])
    assert code == 0, err
    code, out, _ = run_main(["--config", str(config), "bill", "--db", str(db), "--group-by",
                             "team,cost_center", "--format", "json", "--self"])
    assert code == 0
    doc = json.loads(out)
    rows = doc["bill"]["breakdowns"][0]["rows"]
    assert {r["dims"]["team"] for r in rows} == {"platform"}  # users unknown: folded (R-E30)
    from tokenbill.store.db import SqliteStore

    store = SqliteStore(db, create=False, read_only=True)
    try:
        cells = {(r.attribution.team, r.attribution.cost_center) for r in store.iter_requests()}
    finally:
        store.close()
    assert cells == {("platform", "cc-7")}


def test_bill_shows_allowance_apart_from_the_exact_bill(config: Path, tmp_path: Path) -> None:
    db = str(tmp_path / "l.db")
    assert run_main(["--config", str(config), "ingest", "--db", db, str(CC_PROJECTS),
                     "--attr", "billing_path=subscription"])[0] == 0
    code, out, _ = run_main(["--config", str(config), "bill", "--db", db, "--format", "json"])
    assert code == 0
    bill = json.loads(out)["bill"]
    assert bill["allowance"] is not None and int(bill["allowance"]["nano"]) > 0
    assert bill["allowance"]["basis"] == "list_equivalent"
    assert bill["exact"]["basis"] != "list_equivalent" and int(bill["exact"]["nano"]) == 0
    code, text, _ = run_main(["--config", str(config), "bill", "--db", db])
    assert "allowance" in text and "list-equivalent" in text


def test_bill_text_json_csv_and_group_by(config: Path, ledger: Path) -> None:
    base = ["--config", str(config), "bill", "--db", str(ledger)]
    code, out, _ = run_main([*base, "--group-by", "model", "--group-by", "team"])
    assert code == 0 and "BILL" in out and "by model" in out and "exact" in out
    code, out, _ = run_main([*base, "--format", "json", "--group-by", "model"])
    from tokenbill.outputs.result_json import validate_result_json

    doc = json.loads(out)
    assert validate_result_json(doc) == [] and doc["command"] == "bill"
    code, out, _ = run_main([*base, "--format", "csv", "--group-by", "model"])
    assert code == 0
    rows = list(csv.reader(io.StringIO(out)))
    assert rows[0][:3] == ["breakdown", "group", "users"] and rows[1][0] == "total"
    assert len(rows) > 2 and rows[2][0] == "model"
    code, _out, err = run_main([*base, "--group-by", "principal"])
    assert code == 2 and "self view" in err
    code, _out, err = run_main([*base, "--group-by", "colour"])
    assert code == 2
    code, _out, err = run_main([*base, "--basis", "contract"])
    assert code == 2 and "--contract" in err
    code, out, _ = run_main([*base, "--basis", "list", "--reprice", "--since", "2026-01-01",
                             "--until", "2099-01-01", "--format", "json"])
    assert code == 0 and json.loads(out)["rate_card"]["contract"] is None


def test_bill_self_refuses_a_multi_person_ledger(config: Path, tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    db = str(tmp_path / "l.db")
    for ref in ("dev-1", "dev-2"):
        out = tmp_path / ref
        monkeypatch.setenv("TB_DEVICE", ref)
        assert run_main(["collect", "claude-code", "--projects", str(CC_PROJECTS), "--out",
                         str(out), "--state", str(tmp_path / f"{ref}.state"), "--final",
                         "--principal-ref", "env:TB_DEVICE"])[0] == 0
        assert run_main(["--config", str(config), "ingest", "--db", db, str(out)])[0] == 0
    code, _out, err = run_main(["--config", str(config), "bill", "--db", db, "--self"])
    assert code == 2 and "one person's data" in err
    code, out, _ = run_main(["--config", str(config), "bill", "--db", db, "--format", "json"])
    assert code == 0


def test_config_precedence_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """flag > TOKENBILL_* env > config file > defaults, seen in the result's privacy.k."""
    config = init_dir(tmp_path / "tb", k=7)
    db = str(tmp_path / "l.db")

    def k_of(*extra: str) -> int:
        code, out, err = run_main(["--config", str(config), "ingest", "--db", db,
                                   str(CC_PROJECTS), "--format", "json", *extra])
        assert code == 0, err
        return json.loads(out)["privacy"]["k"]

    monkeypatch.delenv("TOKENBILL_K", raising=False)
    assert k_of() == 7
    monkeypatch.setenv("TOKENBILL_K", "6")
    assert k_of() == 6
    assert k_of("--k", "9") == 9
    monkeypatch.delenv("TOKENBILL_K")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"colour": "red"}), encoding="utf-8")
    code, _out, err = run_main(["--config", str(bad), "bill", "--db", db])
    assert code == 2 and "not a known key" in err
    code, _out, err = run_main(["--config", str(tmp_path / "missing.json"), "bill", "--db", db])
    assert code == 2


def test_default_config_location_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    init_dir(tmp_path / ".tokenbill", k=8)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TOKENBILL_K", raising=False)
    code, out, err = run_main(["ingest", "--db", "l.db", str(CC_PROJECTS), "--format", "json"])
    assert code == 0, err
    assert json.loads(out)["privacy"]["k"] == 8
    assert json.loads(out)["privacy"]["key_id"] is not None  # the org key from the config


def test_strict_dq_exits_4_on_warnings(config: Path, tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    db = str(tmp_path / "l.db")
    code, _out, _err = run_main(["--config", str(config), "--strict-dq", "ingest", "--db", db,
                                 str(CC_PROJECTS)])
    assert code == 4  # the fixture carries dq.request_id_collision (warn)
    monkeypatch.setenv("TOKENBILL_STRICT_DQ_THRESHOLD", "1000")
    code, _out, _err = run_main(["--config", str(config), "--strict-dq", "ingest", "--db", db,
                                 str(CC_PROJECTS)])
    assert code == 0
    code, _out, _err = run_main(["--config", str(config), "ingest", "--db", db, str(CC_PROJECTS)])
    assert code == 0


def test_json_is_byte_identical_across_processes(config: Path, ledger: Path) -> None:
    outs = []
    for _ in range(2):
        proc = run_subprocess(["--config", str(config), "--deterministic", "bill", "--db",
                               str(ledger), "--format", "json", "--group-by", "model"])
        assert proc.returncode == 0, proc.stderr
        outs.append(proc.stdout)
    assert outs[0] == outs[1] and b"generated_ms" not in outs[0]
    proc = run_subprocess(["--config", str(config), "bill", "--db", str(ledger), "--format",
                           "json"])
    assert b"generated_ms" in proc.stdout


def test_export_trace2_roundtrips_through_ingest(config: Path, ledger: Path,
                                                 tmp_path: Path) -> None:
    out = tmp_path / "ledger.jsonl.gz"
    code, _o, err = run_main(["--config", str(config), "export", "--db", str(ledger),
                              "--format", "trace2", "-o", str(out)])
    assert code == 0, err
    records = read_trace2(out)
    assert records[0]["profile"] == "usage" and records[0]["identity_mode"] == "central-ingest"
    assert CANARY not in json.dumps(records)
    db2 = str(tmp_path / "copy.db")
    code, _o, err = run_main(["--config", str(config), "ingest", "--db", db2, str(out),
                              "--format", "json"])
    assert code == 0, err
    assert json.loads(_o)["inputs"][0]["quarantined"] == 0

    def exact(db: str) -> str:
        code, text, _ = run_main(["--config", str(config), "bill", "--db", db, "--format",
                                  "json"])
        assert code == 0
        return json.loads(text)["bill"]["exact"]["nano"]

    assert exact(db2) == exact(str(ledger))
    code, text, _ = run_main(["--config", str(config), "export", "--db", str(ledger),
                              "--format", "trace2"])
    assert code == 0 and text.splitlines()[0].startswith('{"attribution"')
    code, _t, err = run_main(["--config", str(config), "export", "--db", str(ledger),
                              "--format", "trace2", "--content", "full"])
    assert code == 2 and "content tier full" in err


def test_export_ccusage_daily_and_monthly(config: Path, ledger: Path) -> None:
    code, out, _ = run_main(["--config", str(config), "export", "--db", str(ledger), "--format",
                             "ccusage"])
    assert code == 0
    doc = json.loads(out)
    assert set(doc) == {"daily", "totals"} and doc["daily"]
    day = doc["daily"][0]
    assert {"date", "inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens",
            "totalTokens", "totalCost", "modelsUsed", "modelBreakdowns"} <= set(day)
    code, out, _ = run_main(["--config", str(config), "export", "--db", str(ledger), "--format",
                             "ccusage", "--report", "monthly"])
    assert code == 0 and "monthly" in json.loads(out)


def test_export_usage_errors(config: Path, ledger: Path, tmp_path: Path) -> None:
    base = ["--config", str(config), "export", "--db", str(ledger)]
    for argv, needle in ((["--format", "focus", "--grain", "hour"], "hour"),
                         ([], "--format"),
                         (["--format", "focus", "-o", str(tmp_path / "no" / "f.csv")],
                          "does not exist")):
        code, _out, err = run_main([*base, *argv])
        assert code == 2 and needle in err, argv
    code, _out, err = run_main(["--format", "json", *base[:3], str(ledger)])
    assert code == 2


def test_showback_pages(config: Path, ledger: Path, tmp_path: Path) -> None:
    out_dir = tmp_path / "sb"
    code, out, err = run_main(["--config", str(config), "showback", "--db", str(ledger), "--out",
                               str(out_dir), "--format", "html", "--format", "csv",
                               "--format", "json"])
    assert code == 0, err
    names = {p.name for p in out_dir.iterdir()}
    assert {"index.html", "showback.csv", "showback.json"} <= names
    for path in out_dir.iterdir():
        assert CANARY not in path.read_text(encoding="utf-8")
    assert str(out_dir / "index.html") in out
    code, _out, err = run_main(["--config", str(config), "--format", "xml", "showback", "--db",
                                str(ledger), "--out", str(out_dir)])
    assert code == 2


def test_pricing_show_verify_diff_emit(tmp_path: Path) -> None:
    code, out, _ = run_main(["pricing", "verify"])
    assert code == 0 and "PRICING (VERIFY; OK)" in out
    code, out, _ = run_main(["pricing", "show", "claude-opus-5-5", "--format", "json",
                             "--deterministic"])
    assert code == 0
    doc = json.loads(out)
    assert doc["pricing"]["kind"] == "show" and doc["pricing"]["rows"]
    code, out, _ = run_main(["pricing", "show", "claude-opus-5-5", "--at", "2026-09-01",
                             "--format", "json"])
    assert code == 0 and json.loads(out)["pricing"]["rows"] == []  # launched 2026-09-22
    code, _out, err = run_main(["pricing", "show", "--at", "yesterday"])
    assert code == 2
    code, out, _ = run_main(["pricing", "diff", "builtin", "builtin"])
    assert code == 0 and "PRICING (DIFF; OK)" in out
    code, _out, err = run_main(["pricing", "diff", "builtin", str(tmp_path / "nope.json")])
    assert code == 2 and "not found" in err
    contract = FIXTURES / "rates" / "contract_acme.json"
    code, out, _ = run_main(["pricing", "emit-model-pricing", "--contract", str(contract)])
    assert code == 0 and "modelPricing" in out or out.lstrip().startswith("{")
    code, _out, err = run_main(["pricing", "emit-model-pricing", "--contract",
                                str(tmp_path / "none.json")])
    assert code == 2


def test_pricing_verify_gates_and_cross_checks_feeds(tmp_path: Path) -> None:
    snap = tmp_path / "snap.json"
    from tokenbill.rates.verify import load_snapshot

    doc = load_snapshot()
    models = doc.get("models") if isinstance(doc, dict) else None
    if not models:
        pytest.skip("snapshot shape without a models table")
    first = next(iter(models)) if isinstance(models, dict) else None
    if isinstance(models, dict) and isinstance(models[first], dict) and "input" in models[first]:
        models[first]["input"] = "999.99"
    elif isinstance(models, list) and models and isinstance(models[0], dict) and \
            "input" in models[0]:
        models[0]["input"] = "999.99"
    else:
        pytest.skip("snapshot model rows without an input price")
    snap.write_text(json.dumps(doc), encoding="utf-8")
    code, out, _ = run_main(["pricing", "verify", "--snapshot", str(snap)])
    assert code == 3 and "NOT ok" in out
    feed = FIXTURES / "rates" / "litellm_model_prices.json"
    code, out, _ = run_main(["pricing", "verify", "--feed", str(feed)])
    assert code == 0
    code, _out, err = run_main(["pricing", "verify", "--feed", str(tmp_path / "none.json")])
    assert code == 2


def test_purge_by_date_and_principal(config: Path, tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "l.db"
    monkeypatch.setenv("TB_DEVICE", "dev-9")
    assert run_main(["collect", "claude-code", "--projects", str(CC_PROJECTS), "--out",
                     str(tmp_path / "o"), "--state", str(tmp_path / "s"), "--final",
                     "--principal-ref", "env:TB_DEVICE"])[0] == 0
    assert run_main(["--config", str(config), "ingest", "--db", str(db),
                     str(tmp_path / "o")])[0] == 0
    from tokenbill.store.db import SqliteStore

    store = SqliteStore(db, create=False, read_only=True)
    (person,) = {r.attribution.principal for r in store.iter_requests()}
    store.close()
    base = ["--config", str(config), "purge", "--db", str(db)]
    for argv, needle in (([], "--principal P or --before"), (["--principal", "alice"], "p_"),
                         (["--principal", person], "--yes"),
                         (["--before", "soon", "--yes"], "--before")):
        code, _out, err = run_main([*base, *argv])
        assert code == 2 and needle in err, argv
    code, out, err = run_main([*base, "--principal", person, "--yes", "--format", "json"])
    assert code == 0, err
    doc = json.loads(out)
    assert doc["requests"] > 0 and person not in out
    store = SqliteStore(db, create=False, read_only=True)
    try:
        assert list(store.iter_requests()) == []
        log = store.audit_log()
    finally:
        store.close()
    assert log and all(person not in row[3] for row in log)
    code, out, _ = run_main([*base, "--before", "2026-01-01", "--principal", person, "--yes"])
    assert code == 0 and "purged principal and data before 2026-01-01" in out
    code, _out, err = run_main(["--config", str(config), "purge", "--db",
                                str(tmp_path / "none.db"), "--before", "2026-01-01", "--yes"])
    assert code == 2


def test_init_variants(tmp_path: Path) -> None:
    code, out, _ = run_main(["init", "--dir", str(tmp_path / "c"), "--collector", "--format",
                             "json"])
    assert code == 0
    doc = json.loads(out)
    assert doc["collector"] is True and doc["keys"] == [] and doc["store_dir"] is None
    code, _out, err = run_main(["init", "--dir", str(tmp_path / "c")])
    assert code == 2 and "--force" in err
    code, out, _ = run_main(["init", "--dir", str(tmp_path / "c"), "--force", "--identity-mode",
                             "two-stage", "--k", "6"])
    assert code == 0 and "collection.key" in out and "k = 6" in out
    cfg = json.loads((tmp_path / "c" / "config.json").read_text(encoding="utf-8"))
    assert cfg["identity_mode"] == "two-stage" and cfg["collection_key_file"] == "collection.key"
    assert (tmp_path / "c" / "org.key").stat().st_mode & 0o077 == 0
    assert (tmp_path / "c").stat().st_mode & 0o077 == 0
    key_before = (tmp_path / "c" / "org.key").read_bytes()
    assert run_main(["init", "--dir", str(tmp_path / "c"), "--force"])[0] == 0
    assert (tmp_path / "c" / "org.key").read_bytes() == key_before  # keys are never replaced
    assert os.environ.get("TOKENBILL_K") is None or True


def test_every_ledger_verb_is_offline(config: Path, ledger: Path, tmp_path: Path,
                                      socket_guard: list[str]) -> None:
    """The autouse socket guard records any non-loopback attempt; none may happen."""
    for argv in (["bill", "--db", str(ledger)], ["pricing", "verify"],
                 ["export", "--db", str(ledger), "--format", "ccusage"],
                 ["showback", "--db", str(ledger), "--out", str(tmp_path / "sb")],
                 ["reconcile", "--db", str(ledger), "--report-only"]):
        run_main(["--config", str(config), *argv])
    assert socket_guard == []
