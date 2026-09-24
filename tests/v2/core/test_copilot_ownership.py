"""CORE-AMENDMENTS C-31 / C-32: packaging of the Copilot data directories and the OWNERSHIP.toml
rows of SPEC-v0.2-COPILOT §21.3 (revision 3.1), checked with check_ownership.py's own matcher."""

from __future__ import annotations

from .test_guards import REPO, check_ownership

#: addendum §21.3 (revision 3.1) = the Owns sections of the Copilot briefs (C-32)
C32_ROWS = {
    "F-EXT": ["tokenbill/core/extensions.py", "tests/v2/ext/test_host.py",
              "tests/v2/ext/fake_ext/hooks.py"],
    "F-POOL": ["tokenbill/core/pool.py", "tests/v2/pool/test_pool.py"],
    "CP-RATES": ["tokenbill/copilot/data/__init__.py", "tokenbill/copilot/data/github_copilot.json",
                 "tokenbill/copilot/data/snapshots/models-and-pricing-2026-09-22.json",
                 "tokenbill/copilot/rates_verify.py", "tests/v2/copilot_rates/test_x.py",
                 "tests/v2/fixtures/copilot_rates/yml/commits.txt"],
    "CP-BILL": ["tokenbill/adapters/github_billing.py", "tests/v2/copilot_bill/test_x.py",
                "tests/v2/fixtures/copilot_bill/ai_usage_2026-09.csv"],
    "CP-ORGDATA": ["tokenbill/adapters/github_config.py", "tokenbill/adapters/github_metrics.py",
                   "tokenbill/adapters/github_seats.py", "tokenbill/adapters/github_agent_tasks.py",
                   "tokenbill/adapters/github_usage_records.py", "tokenbill/copilot/teammap.py",
                   "tests/v2/copilot_orgdata/test_x.py", "tests/v2/fixtures/copilot_orgdata/a"],
    "CP-HANDOFF": ["tokenbill/copilot/handoff.py", "tokenbill/copilot/admin_answers.py",
                   "tokenbill/copilot/handoff_data/__init__.py",
                   "tokenbill/copilot/handoff_data/admin_guide.md",
                   "tokenbill/copilot/handoff_data/admin_answers.template.json",
                   "tokenbill/adapters/copilot_export.py",
                   "tokenbill/adapters/github_activity_report.py",
                   "tests/v2/copilot_handoff/test_x.py",
                   "tests/v2/fixtures/copilot_handoff/README.md"],
    "CP-PULL": ["tokenbill/copilot/pull_common.py", "tokenbill/copilot/pull_billing.py",
                "tokenbill/copilot/pull_metrics.py", "tests/v2/copilot_pull/test_x.py"],
    "CP-LOCAL": ["tokenbill/adapters/copilot_cli.py", "tokenbill/adapters/copilot_collect.py",
                 "tokenbill/adapters/copilot_conventions.py", "tests/v2/copilot_local/test_x.py",
                 "tests/v2/fixtures/copilot_local/events.jsonl"],
    "CP-OTEL": ["tokenbill/adapters/copilot_otel.py", "tokenbill/adapters/copilot_vscode.py",
                "tokenbill/adapters/gh_aw.py", "tests/v2/copilot_otel/test_x.py",
                "tests/v2/fixtures/copilot_otel/vscode/DDL.sql"],
    "CP-VSCODE": ["tokenbill/adapters/copilot_vscode_collect.py",
                  "tests/v2/copilot_vscode/test_x.py", "tests/v2/fixtures/copilot_vscode/DDL.sql"],
    "CP-STORE": ["tokenbill/copilot/record_store.py", "tokenbill/copilot/enrich.py",
                 "tests/v2/copilot_store/test_x.py"],
    "CP-RECON": ["tokenbill/copilot/recon.py", "tokenbill/copilot/panel.py",
                 "tests/v2/copilot_recon/test_x.py", "tests/v2/fixtures/copilot_recon/x.csv"],
    "CP-DET-SEATS": ["tokenbill/detect/copilot_seats.py", "tests/v2/copilot_det_seats/test_x.py"],
    "CP-DET-USAGE": ["tokenbill/detect/copilot_org.py", "tests/v2/copilot_det_usage/test_x.py"],
    "CP-DET-LANES": ["tokenbill/detect/copilot_lanes.py", "tests/v2/copilot_det_lanes/test_x.py"],
    "CP-PLAN": ["tokenbill/copilot/plan.py", "tests/v2/copilot_plan/test_x.py"],
    "CP-POLICY": ["tokenbill/copilot/policy.py", "tokenbill/copilot/admin_actions.py",
                  "tokenbill/copilot/budgets.py", "tests/v2/copilot_policy/test_x.py"],
    "CP-OUT": ["tokenbill/copilot/summary.py", "tokenbill/copilot/render.py",
               "tokenbill/copilot/showback.py", "tokenbill/copilot/focus.py",
               "tests/v2/copilot_out/test_x.py"],
    "CP-WIRE": ["tokenbill/commands/copilot.py", "tokenbill/pipeline/copilot.py",
                "tests/v2/copilot_wire/test_x.py"],
    "CP-SYNTH": ["tokenbill/synth/copilot_world.py", "tokenbill/synth/copilot_truth.py",
                 "tests/v2/copilot_synth/test_x.py"],
    "CP-SYNTH-W": ["tokenbill/synth/copilot_writers.py", "tests/v2/copilot_writers/test_x.py"],
    "F-CORE": ["tokenbill/copilot/__init__.py", "tests/v2/core/test_copilot_records.py"],
    "F-SEM": ["tests/v2/sem/test_copilot_bases.py", "tokenbill/core/findings.py"],
    "F-KIT": ["tests/v2/kit/test_copilot_catalog.py", "tests/v2/gates/test_gateF_copilot_smoke.py"],
    "CLI-LEDGER": ["tokenbill/outputs/ccusage.py", "tests/v2/cli_ledger/test_ccusage.py"],
    "INTEGRATION": ["docs/COPILOT.md", "docs/COPILOT-ADMIN.md",
                    "tests/v2/e2e/test_copilot_admin_doc.py",
                    "tests/v2/e2e/test_copilot_flagship.py"],
}


def test_every_c32_row_has_exactly_its_owner() -> None:
    own = check_ownership.load_ownership(REPO / "OWNERSHIP.toml")
    for package, paths in C32_ROWS.items():
        assert package in own.packages, package
        for path in paths:
            assert own.owners_of(path) == [package], (path, own.owners_of(path))
    copilot_packages = {p for p in own.packages if p.startswith("CP-")}
    assert len(copilot_packages) == 19
    assert own.owners_of("tokenbill/core/spans.py") == []  # withdrawn (CA-42)
    assert own.owners_of("tests/v2/outputs/test_ccusage.py") == ["OUT"]  # OUT keeps its area


def test_pyproject_ships_copilot_data() -> None:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert '"tokenbill/copilot/data/**"' in text
    assert '"tokenbill/copilot/handoff_data/**"' in text
    init = (REPO / "tokenbill" / "copilot" / "__init__.py").read_text(encoding="utf-8")
    import ast

    module = ast.parse(init)
    assert len(module.body) == 1 and isinstance(module.body[0], ast.Expr)  # docstring only
