### CP-POLICY — Copilot policy pack, admin checklist, REST request files, budget design (wave 2)

**Goal.** Turn the Copilot plan and findings into the exact things a GitHub admin changes, with nothing
executed: a managed-settings patch for the few client-enforced keys, an admin checklist for server-side
settings and communications (with docs URL, projection, reach, deadline and the auth each REST call needs),
REST request files, repository settings patches, CI and agentic-workflow snippets, and a billing-mode-aware
budget design per cost center — honest about what the adopting organization's editors reach (managed `model`
does not reach JetBrains; VS Code's local usage database is a developer opt-in, never enforceable) and about an
unknown plan (no plan-dependent advice is given as if the plan were known). Read SPEC §3.5 (`PolicyPack`,
`PolicyEntry`), §11.3–§11.4; addendum DC13,
DC19, DC20, DC21, §3.2 (`AdminAction`), §3.7 (`ADMIN_ACTIONS`, `ALLOWLIST` — here `COPILOT_ALLOWLIST`), §9.3,
§10.1–§10.2 (fix
columns), §11.1, §11.3, §11.4, §19.1 #9–#10, #13–#15, #19, #24, #26–#27, §19.4, §19.5 #25, #27; rulings
R-E22. Naming: CP-POLICY's builder is `build_copilot_packs`; addendum §11.3's `policy_packs(...)` is CP-WIRE's
`ExtensionSpec` hook (`tokenbill.pipeline.copilot:policy_packs`), which calls this builder.

**Owns.** `tokenbill/copilot/policy.py` (`build_copilot_packs`), `tokenbill/copilot/admin_actions.py`
(`admin_actions`), `tokenbill/copilot/budgets.py` (`budget_design`), `tests/v2/copilot_policy/**`.

**Consumes.** `core.catalog` (`COPILOT_ALLOWLIST`, `copilot_allowed`, `ADMIN_ACTIONS`, `COPILOT_LEVERS`,
`lever`, `fix_for`, `editor_family`), `core.types` (`PolicyPack`, `PolicyEntry`, `AdminAction`, `ActionPlan`,
`PoolMonth` with `plan_scenario`, `PlanEvidence`, `Finding`), `core.labels`,
`core.kanon` (quantiles only for cohorts ≥ k), `core.pool` (`Cell`), `core.facts` (dates), `core.textsafe`,
`core.testing`, `core.builders`.

**Provides.**
- `admin_actions(plans_by_scenario, findings, pools, *, plans: Sequence[PlanEvidence], today) ->
  tuple[AdminAction, ...]` (one per lever or finding kind
  with an `ADMIN_ACTIONS` entry; `where`, `what` ≤ 400 chars, docs URL, projection with label, reach,
  deadline, auth note, REST file path).
- `build_copilot_packs(plans_by_scenario, findings, actions, *, current, cohort_by, include_tradeoffs, teams, budgets,
  otel_service_name=None) -> list[PolicyPack]` (target `"github-copilot"`; files of addendum §11.3 in
  `PolicyPack.hooks`).
- `budget_design(pools, cells, config, *, k) -> list[dict]` (REST request specs, whole-dollar amounts; one set
  per plan scenario when the plan is unknown).

**Build.**
1. Managed-settings merge patch (RFC 7386 against `--current`) with only `copilot.managed.*` keys, rollback
   patch, per-team files for waves; README notes reach (no JetBrains for `model`; not cloud agent for MCP
   lists) and keeps `telemetry.serviceName` at `github-copilot` unless a name is given.
2. Admin checklist items from `ADMIN_ACTIONS`, including: **plan confirmation** (`admin:plan_confirm`, first
   item whenever a `PlanEvidence` is `unknown` or has `conflict`: where to read the plan — licensing page, seats
   API `plan_type`, detailed-report seat SKU — and that the answer goes into `answers.json`); **JetBrains
   limits** (whenever any team's JetBrains share > 0: managed `model` is not applied in JetBrains, managed
   telemetry for JetBrains is documented inconsistently, so JetBrains-heavy teams get the server-side model
   policy — reach 1 — and Auto-tier communication instead); **VS Code usage database opt-in**
   (`admin:vscode_db_exporter_optin`: the user-level setting `github.copilot.chat.otel.dbSpanExporter.enabled`
   cannot be enforced by managed settings; the pack ships `vscode/agent-traces-optin.md` for developers who
   volunteer, with the daily `tokenbill copilot collect --source vscode` recipe and the privacy statement);
   code-review Lite default before 2026-09-28 plus the
   personal-settings caveat, the review MCP setting (on by default), custom-instruction size, unlicensed review
   policy, runner type, paid-usage policy, org seat policy (assign_all → selected members), team membership
   review, cost-center pools **with the block-or-continue choice**, budgets with stop, the org policy "Allow
   use of Copilot CLI billed to the organization", agentic-workflow caps and triggers, Auto tier guidance.
3. `github/requests.jsonl`: method, path, `api_version: "2026-03-10"`, schema-only body fields, note,
   `lever_id`, `auth`; seat removal bodies carry placeholders and the seats query (filter on
   `last_activity_at` and `assigning_team == null`), never usernames; `selected_teams` only for teams whose
   every seat is idle.
4. Repo `settings.patch.json` (CLI only, trusted directories), `allowed_models.txt` (exactly one
   `fallback:`), `ci/copilot-limits.md`, `ci/agentic-workflows.md` (frontmatter `max-ai-credits: N` from the
   finding, artifact download recipe).
5. **Plan unknown:** the `copilot.seat_downgrade` lever and every "Business instead of Enterprise" item are
   omitted (listed once as "not assessed: plan unknown"); projections of plan-dependent actions carry both
   scenario values ("if Business: …; if Enterprise: …"), never one; budget sizes are given per scenario.
6. Budget design per addendum §11.3: metered entities only for cost-center pools and budgets; cohort p99
   user-level budgets from ≥ k users; metered budget = forecast p90 overage × 1.1 rounded up to whole dollars,
   `prevent_further_usage: false` with a trade-off note (true where the API requires it); enterprise budget ≥
   Σ + direct forecast; sizing check printed; volume / azure / unknown → enterprise / org budgets only with a
   VERIFY note.

**Acceptance tests.**
- Merge patch against a `--current` with `model: "gpt-5.4"` sets `"auto"`; rollback restores it; an unknown
  key raises `UsageError`; an unverified key appears only as a README comment.
- `requests.jsonl` lines parse; no username, no token, no `p_`; each has an `auth` note matching §19.4.
- Checklist contains the review MCP item, the personal-settings caveat, the cap-choice item and the CLI
  billing policy item when their findings exist; every action has a docs URL and a label.
- Budget design: a 3-cost-center metered fixture (one at 180% of its allowance in overage) enables its pool
  with the choice stated and sizes budgets as specified; the same fixture with `billing_mode=volume` emits
  no cost-center advice; a cohort of 3 users gets no quantile.
- Plan unknown (C.P13): the checklist starts with the plan-confirmation item; no downgrade item; the seat
  reclaim action shows two projections; `budget_design` returns two labelled sets; plan known → neither.
- A team with 30% `ide:intellij` interactions → the JetBrains-limits item and the model-policy recommendation;
  the managed-settings README states the reach; the VS Code opt-in file is present and contains no
  enforceable managed key.
- Determinism across runs and permutations; canary absent from every file.

**Size.** ~2.3k LOC including tests.
