# tests/v2/copilot_policy — CP-POLICY acceptance tests

Wave-2 package CP-POLICY: the GitHub Copilot policy pack (target `github-copilot`) —
`tokenbill/copilot/policy.py` (`build_copilot_packs`, `load_current`, `managed_settings_patch`,
`apply_merge_patch`, `team_counts`, `write_pack`), `tokenbill/copilot/admin_actions.py`
(`admin_actions`) and `tokenbill/copilot/budgets.py` (`budget_design`); addendum §11.3, §11.4,
§9.3, §19.1, §19.4, §19.5; DC13, DC19–DC21; R17, rulings R-E22.

Run (from the worktree root): `PYTHONPATH=$PWD .venv/bin/python -m pytest -q -p no:cacheprovider
tests/v2/copilot_policy` (65 tests, incl. 3 gate tests). Coverage: `coverage run
--source=tokenbill.copilot.policy,tokenbill.copilot.admin_actions,tokenbill.copilot.budgets -m
pytest tests/v2/copilot_policy` → 97% at hand-off.

| file | covers |
|---|---|
| `helpers.py` | area-local builders: plans (`LeverResult`s with ESTIMATED figures), valid Copilot findings through `core.findings.build_finding` (hostile fixes swapped in afterwards), CP-DET-SEATS-shaped `idle-seat` evidence, known / C.P13 scenario pool months, the 3-cost-center metered budget world (report rows per user, seat lines, cost-center snapshots) |
| `test_managed_settings.py` | acceptance 1: `--current` `model: "gpt-5.4"` → patch `"auto"`, rollback restores it; absent keys roll back to null; nested telemetry and a non-object previous value; unknown / non-managed / Claude Code keys and non-JSON fix values → `UsageError`; an unverified key only as a README comment (allowlist monkeypatched); repo keys and trade-off keys; `serviceName` default and `--otel-service-name`; team files with `{"overridable": "auto"}`, waves, per-team packs; `load_current` limits; RFC 7386 cases |
| `test_admin_actions.py` | acceptance 3, 5, 6: review MCP item, personal-settings caveat, cap-choice item, CLI billing policy item when their findings exist; docs URL and a Label line for every item; C.P13 plan unknown (confirmation first, no downgrade, "not assessed" once, seat reclaim with both scenario values) vs plan known (neither); conflicting evidence; JetBrains limits from team counts and from `editor-mix` findings; volume billing (no cost-center items, renewal deadlines); Lite deadline and the 60-day cut-off; kind-only items; order independence; validation and helpers |
| `test_budgets.py` | acceptance 4, 5: cost center A at 180% of its allowance in overage → pool advice with the block-or-continue choice; budgets A $26, B $12, C $10 (overage $42 pro rata × 1.1), user-level p99 $72 / $15 / $12, enterprise $48, sizing check; the same world from `core.pool.pool_months`; volume / azure / unknown → no cost-center advice; a cohort of 3 users → no quantile; plan unknown → business and enterprise sets, known → none; open-month forecast p90 and direct-org extrapolation; capped cost-center and org entities; allowance fallbacks; latest month; validation and order independence |
| `test_pack.py` | acceptance 2, 6, 7: `requests.jsonl` lines parse with the fixed key set, `api_version` 2026-03-10, no username / token / `p_`; seat bodies are placeholders with the seats query; `selected_teams` only for wholly idle teams of ≥ k seats; auth notes per §19.4; budget requests per scenario and the template; 30% `ide:intellij` → JetBrains item, model policy, README reach 0.70; VS Code opt-in files with no managed key; allowed models (one `fallback:`), CI files; determinism across runs and permutations; canary absent from every file (planted in finding title / summary / fix / evidence and unrelated `--current` keys); pseudonym guard; `team_counts`; `write_pack` |
| `test_properties.py` | hypothesis: `load_current` fuzz (only `TokenbillError`), patch-then-rollback restores any document, team-count reader fuzz, hostile evidence attrs through `admin_actions` and `build_copilot_packs`, budget amounts whole dollars and enterprise ≥ Σ cost-center budgets for random worlds |
| `test_gate.py` | `@pytest.mark.gate`: CP-PLAN's real scenario plans (C.P13b) → seat reclaim $0 if Business / $390 if Enterprise; CP-DET-SEATS's real findings → checklist and pack (no pseudonym); CP-DET-USAGE's real `editor-mix` / `auto-adoption` findings with CP-PLAN → JetBrains item, overridable Auto, README reach |
| `CONTRACT-CHANGE-CP-POLICY.md` | one projection per `AdminAction` vs two scenarios, the JetBrains-limits id, additive keywords, the budget spec shape, the missing CP-VSCODE snippet, the CP-PLAN × CP-DET-SEATS seam observed at this base |

## Fixtures and provenance

No fixture files. Every record is synthetic and built in the tests with `core.builders`
(`make_pool_month`, `make_plan_evidence`, `make_ai_usage_row`, `make_seat_line`, `make_license`,
`make_activity`, `make_config`, `make_principal`); cells and pool months come from the real
`core.pool.build_cells` / `pool_months`; findings from `core.findings.build_finding` (and, in the
gate tests, from the real CP-DET-SEATS / CP-DET-USAGE detectors). Numbers are hand-computed in
the tests (the C.P13b $0 / $390 pair is addendum Appendix C). No real exports, logins,
repositories or transcripts; the canary values are `core.builders.CANARY*`.

## Unverified facts (VERIFY; shipped as commented guidance or labelled assumptions)

No network was used; these were not re-verified against primary sources in this build:

- **Team files for waves** — the managed-settings team-file mechanism, the file location
  `copilot/teams/<team>.json` and the format of `copilot/team-mappings.patch.json` (Token Bill's
  own `{"teams": {name: {"settings_file", "wave"}}}`); the README says VERIFY. The enterprise-level
  value `{"overridable": "auto"}` is taken from addendum §11.3 / §11.4.
- **Telemetry** — `endpoint` / `protocol` values are the admin's (not written); `resourceAttributes`
  as an object (`team.id`, `tokenbill.arm`, `tokenbill.wave`) in team files follows addendum §11.3;
  JetBrains support for managed telemetry is contradictory in GitHub's docs (§19.3 #34).
- **Budgets API** — scope names `enterprise`, `organization`, `cost_center`,
  `multi_user_cost_center`; `prevent_further_usage: true` required for user-level scopes;
  `budget_alerting.alert_recipients` as a list of logins (a placeholder is written); whole-dollar
  `budget_amount` ≥ 1 (a $0 forecast gets the $1 minimum, noted); `api_version` 2026-03-10.
- **Cost centers** — REST sets only `ai_credit_pool_enabled`; the block-or-continue choice is made
  in the UI (§19.1 #10); whether volume-licensed seats' included credits can be assigned to cost
  centers (§19.5 #27) → no cost-center advice for volume / azure / unknown billing.
- **Coding agent policy** — the `PUT …/copilot/policies/coding_agent` body schema (an empty body
  with a VERIFY note is written).
- **Agentic workflows** — gh-aw artifact names (§19.5 #26): the recipe downloads every artifact of
  a run and keeps `token-usage.jsonl`.
- **`allowed_models.txt`** — the glob syntax and the `fallback:` line format (CLI config-dir
  reference); model list from `core.facts` categories and remaps, retiring models excluded.
- **Command names** — `tokenbill collect copilot-cli --ci --billing-path copilot_direct` and
  `tokenbill copilot collect --source vscode` are CP-WIRE's (not on disk at this wave).
- **VS Code opt-in** — setting `github.copilot.chat.otel.dbSpanExporter.enabled` and the 7-day /
  100-session retention come from `core.facts` (research; the per-OS paths are `verified: false`).
