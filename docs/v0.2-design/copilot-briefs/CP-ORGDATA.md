### CP-ORGDATA — Copilot org data: config, metrics, seats, agent tasks, usage-records refusal, team maps (wave 2)

**Goal.** Turn GitHub's Copilot configuration and activity data into content-free, pseudonymized records
Token Bill can join: budgets, cost centers and org Copilot settings (incl. `seat_management_setting`) as
`ConfigSnapshot`s; per-user-day activity (`ActivityDay`) and team outcomes; the seat inventory
(`LicenseSnapshot`, incl. team assignment); cloud-agent session credits; plus team / cost-center maps built
from GitHub's user-teams report; and refuse the content-bearing usage-records API. This org-data path needs no
laptop collection and is **primary and release-blocking** (owner answers 1 and 3): it is how an organization
whose developers use VS Code and IntelliJ is covered, JetBrains usage included (`totals_by_ide`, seat
`last_activity_editor`, report rows), and it is what CP-HANDOFF bundles. It also supplies plan evidence (seats
`plan_type`, org billing `plan_type`) for `core.pool.detect_plans`. Read SPEC §3.2, §5.1, §8
(esp. §8.4); addendum DC7, DC8, DC20, §3.1 (CA-6, CA-8), §4, §5.4–§5.8, §8, §15 (`copilot team-map`), §19.3
#7–#15, §19.5 #21, #22; `CORE-AMENDMENTS.md` items C-7, C-9, C-10.

**Owns.** `tokenbill/adapters/{github_config,github_metrics,github_seats,github_agent_tasks,
github_usage_records}.py`, `tokenbill/copilot/teammap.py`, `tests/v2/copilot_orgdata/**`,
`tests/v2/fixtures/copilot_orgdata/**`.

**Consumes.** `core.records` (`ConfigSnapshot`, `ActivityDay`, `LicenseSnapshot`, `OutcomeAggregate` with
`extra`, `UsageAggregate`), `core.types`, `core.jsonl` (`parse_json_line(exact_numbers=True)`,
`load_json_exact`), `core.money`, `core.ids`, `core.kanon.merge_small_groups`, `core.models`, `core.catalog`
(`ACTIVITY_KEYS`, `ACTIVITY_FLAGS`, `CONFIG_KEYS` — vocabularies defined in `core.records` —, `editor_family`),
`core.builders`, `core.testing`.

**Provides.** Registry adapters (names and dotted paths fixed by F-CORE-C, `CORE-AMENDMENTS.md` C-24)
`github-copilot-config` (`github_config:CopilotConfigAdapter`), `github-copilot-metrics`
(`github_metrics:CopilotMetricsAdapter`), `github-copilot-seats` (`github_seats:CopilotSeatsAdapter`),
`github-agent-tasks` (`github_agent_tasks:AgentTasksAdapter`), `github-usage-records`
(`github_usage_records:UsageRecordsRefusal`); `teammap.build_maps(user_teams, cost_centers, *, k) ->
tuple[dict[str, str], dict[str, str]]` (teams below k omitted; a login in several teams → the team with the
most seated members, ties by slug).

**Build.**
1. Config per §5.4: both budget SKU field variants, three budget types, `1000.0` amounts; user and
   multi-user scopes store **team / cost center of the user, never the user or a `p_`**; `user-states`
   aggregated (quantiles only with ≥ k users); cost centers without resource names; `GET
   /orgs/{org}/copilot/billing` → `org_settings` with `seat_management_setting` and `seat_breakdown`.
2. Metrics per §5.5 with the documented field names (`loc_suggested_to_add_sum`, `loc_suggested_to_delete_sum`,
   `loc_added_sum`, `loc_deleted_sum`, …), `reported_cost_nano` in nano-USD, team outcome rows with
   `merge_small_groups`, enterprise / org PR extras, repos → `(enterprise)` only, 28-day files flagged, legacy
   JSON skipped. `totals_by_ide` → `ide:<ide>` counts with the raw IDE name normalized
   (`[a-z0-9._-]`, e.g. `ide:vscode`, `ide:intellij`); the editor family is applied by consumers through
   `core.catalog.editor_family` (**VERIFY** the JetBrains IDE strings: `intellij`, `pycharm`, `goland`, …
   all map to `jetbrains`). **Usage-dashboard NDJSON export** (UI, 28-day, excludes CLI; no-token handoff
   path): accepted only when every record matches the API 28-day shape (`report_start_day`,
   `report_end_day`, `day_totals[]` or per-user records), flagged `dq.copilot_dashboard_export` +
   `dq.copilot_28day_window`, never joined with daily team rows; any other shape quarantined with reason
   `dashboard-shape-unverified` (**VERIFY** against a real export).
3. Seats per §5.6 with `assigned_via_team = assigning_team is not None` (always a bool from the API; `None`
   is reserved for the activity report, CP-HANDOFF), `plan` from `plan_type` (`business` / `enterprise` /
   `unknown`), buckets vs the fetch date, `last_activity_editor` → `editor_family` (e.g.
   `vscode/1.77.3/copilot/1.86.82` → `vscode`; JetBrains strings **VERIFY**), assignee identity dropped at parse
   time.
4. Agent tasks per §5.7 (per-repo pages only in fleet mode; `/agents/tasks` files refused unless
   `opts.identity_mode == "install"`); usage records per §5.8.

**Acceptance tests.**
- The docs example users-1-day record (`ai_credits_used: 12.5`) → `reported_cost_nano == 125_000_000`,
  counts under the documented names (a record using `loc_added` instead of `loc_added_sum` leaves
  `loc_added` absent and counts `dq.unknown_fields`); `CANARY_LOGIN` absent.
- 12 users × 3 days: team outcomes with n ≥ 5, the team of 3 merged or dropped; enterprise and org files never
  added.
- Budgets: a user-scope $0 budget becomes `team=<team>` in attrs, no `p_` anywhere; user-states with 3 users →
  no quantiles, 7 users → p50/p90.
- Org settings: all four `seat_management_setting` values parse; `plan_type` stored as an attr; seats: boundary
  buckets, team-assigned seat
  flagged, a principal seated via two orgs → two snapshots.
- IDE counts: a users-1-day record with `totals_by_ide` for VS Code and IntelliJ → `ide:vscode` and
  `ide:intellij`; a seat with a JetBrains `last_activity_editor` string → family `jetbrains`.
- Dashboard export: a record in the API 28-day shape → accepted with both dq codes; another shape → quarantined.
- `build_maps`: 3-person team absent; multi-team login deterministic; cost-center users mapped; no emails.
- Agent tasks: completed / failed / timed-out sessions → provider-estimate aggregates; `/agents/tasks` file in
  central mode → refused with the dq code.
- `assert_adapter_conforms` on all five adapters; hypothesis fuzz of the NDJSON and JSON parsers.

**Size.** ~2.85k LOC including tests.


---
**ORCHESTRATOR ADDITIONS (binding):** SPEC Appendix E.7 R-E43 — set `billing_path` to `copilot_pool` or `copilot_direct` on every Copilot inference you emit.
