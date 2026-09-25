# CONTRACT-CHANGE-CP-ORGDATA — gaps found while building the Copilot org-data adapters

Implemented against the current (frozen) core; nothing in `tokenbill/core/*` was edited. Items 1–3
are proposals for the contract owner; items 4–6 are cross-package agreements the gate tests
(`test_gate.py`) check at merge gate 1.

## 1. `ActivityDay` natural key vs. 28-day files (proposal)

`core.records.record_key(ActivityDay)` is `(date_utc, product, principal)` — it has no source kind or
window. Addendum §5.5 / the CP-ORGDATA brief accept 28-day files (API `*-28-day` reports and the
usage-dashboard NDJSON export, the no-token handoff path) "never joined with daily team rows". The
adapter keeps their per-user rows as `ActivityDay(source_kind="github.copilot_metrics.28day",
date_utc=report_end_day)` so that consumers get the 28-day IDE split and activity (CP-PLAN reach,
CP-DET-USAGE `editor-mix`, CP-SYNTH-W's "28-day totals" gate). Inside one read a daily row of the
same person and day wins (`stats["activity_28day_superseded"]`), but a daily file and a 28-day file
ingested **separately** into one record store share natural keys on `report_end_day`, and the later
fetch replaces the other.

*Proposed (between waves):* add the source kind to the `ActivityDay` natural key when it is not
`github.copilot_metrics` (or an appended `window_days: int = 1` field that is part of the key).
*Until then:* do not mix daily users files and 28-day / dashboard files of the same window in one
store (the UI path has only the dashboard export; the token path only daily files).

## 2. New free-string source kinds (documentation)

`OutcomeAggregate.source_kind` gains `github.copilot_metrics.28day` (28-day team rows and
`day_totals` entity rows) and `github.copilot_metrics.repos` (pull-request totals summed from
`repos-1-day`), beside C-7's `github.copilot_metrics`; `ActivityDay.source_kind` gains
`github.copilot_metrics.28day`. Stores key outcome rows by `(date, team, source_kind)`, so the
three never replace each other. Consumers reading pull-request ratios should prefer the aggregated
report's `(enterprise)` row (`github.copilot_metrics`) and fall back to the repos total.

## 3. Budget amounts of license-based SKUs (proposal)

ghec.json: "`budget_amount` … For license-based products, this represents the number of licenses."
`CONFIG_KEYS["budget"]` has no key for a license count, so such budgets are stored without
`amount_nano` (`stats["license_budget_amounts_not_stored"]`). *Proposed:* `amount_licenses` (int).

## 4. One login normalization for every GitHub adapter (cross-package agreement)

Seats × activity × AI credits join per person only if every adapter pseudonymizes a login the
same way (addendum §4 "Identity"). CP-ORGDATA uses
`p_ = core.ids.pseudonym(principal_key, "p", login.strip().lower())`
(`tokenbill.adapters.github_config.login_key`; GitHub logins are case-insensitive ASCII, EMU
suffixes are part of the login). CP-BILL (`username`) and CP-HANDOFF (`pseudonym_of`, the
activity report's `login`, the `exclude_logins` pre-filter) must use the same rule — gate tests
`test_gate_handoff_pseudonym_matches_the_adapters` and `test_gate_ai_usage_report_logins_join_seats`.
Team / cost-center map keys are looked up exactly, then case-folded.

## 5. Agent-task sessions carry user ids, team maps carry logins (open)

`/agents/repos/{o}/{r}/tasks` sessions name their user only as `user.id`; `build_maps` returns
login-keyed maps (the brief's contract), so cloud-agent aggregates get team `(unmapped)` unless the
caller adds `str(user_id)` keys (the adapter looks them up). Adding ids to the maps would make them
leak-gate terms in CP-HANDOFF; left to CP-WIRE / the contract owner.

## 6. Data-quality codes to add to SPEC §5.1 (R-E38)

Named in the briefs and emitted: `dq.copilot_28day_window`, `dq.copilot_dashboard_export`,
`dq.copilot_legacy_metrics`, `dq.copilot_legacy_pru`, `dq.copilot_agent_tasks_user_scope`
(besides SPEC's `dq.unknown_fields`, `dq.outcomes_suppressed`, `dq.raw_bodies_ignored`). New in
this package: `dq.copilot_org_unknown` (org billing without its org), `dq.copilot_budget_users_unattributed`
(user-states without their budget id), `dq.copilot_snapshot_date_assumed` (seats without a fetch
time or clock), `dq.copilot_agent_tasks_coverage` ("tasks from N listed repositories").
