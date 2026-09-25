# tests/v2/copilot_handoff — CP-HANDOFF acceptance tests

Package CP-HANDOFF (wave 2b, GitHub Copilot): the admin handoff kit. A GitHub admin turns raw GitHub
files into one content-free, pseudonymized bundle (`export.tbx`, schema
`tokenbill/copilot-export@1`) that the product owner analyses without admin rights or network.

| module | provides |
|---|---|
| `tokenbill/copilot/handoff.py` | `export_from_files`, `write_bundle`, `read_bundle`, `inspect_bundle`, `render_inspect`, `harvest_leak_terms`, `leak_scan`, `LeakTerms`, `read_team_map_csv`, `pseudonym_of`, `admin_guide_text`, `BundleManifest`, `ExportReport`, `EXPORT_SCHEMA` |
| `tokenbill/copilot/admin_answers.py` | `parse_answers`, `template_text` (schema `tokenbill/copilot-admin-answers@1`) |
| `tokenbill/copilot/handoff_data/` | `admin_guide.md` (≤ 350 lines; `docs/COPILOT-ADMIN.md` is INTEGRATION's copy), `admin_answers.template.json` |
| `tokenbill/adapters/copilot_export.py` | `CopilotExportAdapter` (`copilot-export`) |
| `tokenbill/adapters/github_activity_report.py` | `ActivityReportAdapter` (`github-copilot-activity-report`) |

Run: `uv run --python 3.12 --extra dev pytest -q tests/v2/copilot_handoff` (also `--python 3.10`).
Coverage: `uv run --python 3.12 --extra dev coverage run -m pytest tests/v2/copilot_handoff && uv run
--python 3.12 --extra dev coverage report --include='tokenbill/copilot/handoff.py,tokenbill/copilot/admin_answers.py,tokenbill/adapters/copilot_export.py,tokenbill/adapters/github_activity_report.py'`
(98% at hand-off). Longer fuzzing: `TB_HANDOFF_FUZZ_EXAMPLES=600 uv run … pytest
tests/v2/copilot_handoff/test_fuzz.py` (600 examples per property ran clean at hand-off; 60 by
default).

| test module | covers (brief section) |
|---|---|
| `test_bundle.py` | Build 1 round trip of every record kind (`CANARY_LOGIN` principals), byte-identical bundles across two processes, 0600 file, member order / dates / modes, manifest keys, dedupe, window, key-id checks, seed validation |
| `test_leak_gate.py` | Build 4: leaking fake adapters (login in a description, URL, `ghp_` token, `CANARY_LOGIN`, e-mail; e-mail in a budget attr) abort with member + category and no file; team label = login → rename hint; clean export records `leak_scan.terms`; matching-rule units; the harvester |
| `test_kmerge.py` | Build 2: teams of 7, 5 and 3 (the 3-team appears nowhere, `teams_merged == 1`), aggregates re-keyed and summed, outcomes, budget teams |
| `test_aggregate_only.py` | Build 3: no `p_` in any member, seat counts sum to input seats minus the residual, every `n` / `n_people` ≥ k, pre-evaluated seat criteria, activity counts, summed cost lines, plan detection from seat counts |
| `test_reader.py` | Build 5: `../x`, absolute, backslash, directory, symlink, encrypted, bzip2, duplicate, extra and 17th members, forged 2,000:1 and real 1,000:1 members, oversize manifest, schema, counts, JSON errors, non-`p_` principal (`ContractViolation`), non-`h_` name dims, per-person rows in aggregate-only, content re-check, size limits |
| `test_export_adapter.py` | Build 6: `assert_adapter_conforms`, sniff (bundle vs `.xlsx` / other zips), capabilities, `MemoryStore(adopt_key_ids=True)` keeps every `p_` and refuses a second key id (nothing ingested), `MemoryRecordStore` accepts the adopted key |
| `test_activity_report.py` | Build 7: the fixture (BOM, empty `last_activity_at`, unknown surface, `Unspecified`, `M/D/YYYY`, 7/8 and 90/91-day edges, `CANARY_LOGIN`), conformance, sniff, maps, quarantine / strict mode, duplicates, windows, timestamps |
| `test_answers.py` | Build 8: template → empty `run_flags`; filled file → exact attrs + one `org_settings` per stated seat policy; `unknown` omitted; unknown keys, free text, non-dates, floats, duplicates → `UsageError`; statements feed `core.pool.detect_plans` |
| `test_erasure.py` | Provides/erasure: `pseudonym_of(CANARY_LOGIN)` equals the bundle principal; `exclude_logins` drops every row (cost lines, licenses, activity), counts `rows_excluded`, passes the leak gate, `key_rotated` recorded; 0700 temp dir removed also on failure; exact-number rewriting of JSON / NDJSON / envelopes / gzip |
| `test_export.py` | orchestration: sources, answers, dq notes, window, refused `copilot-export` / `github-usage-records` inputs, unavailable adapters counted once, argument errors; `read_team_map_csv` |
| `test_inspect_and_guide.py` | `inspect_bundle` / `render_inspect` (counts only, no pseudonym); Build 9 guide: every call and UI report, section order, ≤ 350 lines, scopes only in the "never" sentence, VERIFY marks |
| `test_fuzz.py` | hypothesis: activity-report CSV parser (text and bytes), timestamps, `read_bundle` on mutated and random zips, `parse_answers`, `leak_scan`, the harvester, `read_team_map_csv` — only `TokenbillError` escapes |
| `test_no_float.py` | owned modules are money modules (C-30): no `float(`, no float literal |
| `test_gate_ui_vs_api.py` | **gate** (skipped until CP-SYNTH, CP-SYNTH-W, CP-BILL, CP-ORGDATA merge): the world written as API recordings and as UI downloads gives equal cost lines / aggregates, UI seats ⊆ API seats, no org settings on the UI path, both bundles clean |

`helpers.py` holds fake adapters (AI usage CSV, seats JSON, metrics NDJSON, budgets JSON — only the
documented identity field names are borrowed) registered by replacing
`core.registry.BUILTIN_ADAPTERS` for one test, a synthetic raw world (teams alpha 7, beta 5, gamma 3;
`CANARY_LOGIN` in alpha) and record builders for direct `write_bundle` calls.

## Fixtures and provenance

See `tests/v2/fixtures/copilot_handoff/README.md`: `activity_report.csv` (primary-derived from the
GitHub Docs metrics-data reference, one third-party-derived JetBrains string) and
`answers_filled.json` (synthetic). Everything else is generated per test; all data is synthetic.

## Decisions the brief left open (implemented readings)

- **Leak gate matching (Build 4).** Terms carry a category (`LeakTerms`, a `frozenset[str]` whose
  `repr` shows only the count). Logins, e-mails and full repository names / workflow paths match whole
  JSON string values (case-insensitive) and, from 6 characters on, as substrings; repository short
  names match whole values only; numeric user ids match whole string values or digit runs of ≥ 6 not
  inside a longer number or decimal (never JSON numbers); terms under 3 characters are ignored.
  Repository names and workflow paths are **not** matched inside organizational label fields (team,
  cost center, org, workspace, entity ids, attr keys naming entities), so a team or org named like a
  repository does not stop the export (addendum §22.5's rule); merged team labels are matched only in
  `team` and non-label fields. Machine values (ids, pseudonyms, key ids, dates, decimal `quantity`)
  and the closed vocabularies of `core.records` never match. Checks (b) e-mail pattern, (c)
  `find_secrets`, (d) canaries, (e) `http://` and `https://` run per value and, except secrets, over
  the raw member text. Messages name the first member, the categories and a rename hint when a team
  label equals a login — never a value. The harvester also reads budget `user` /
  `budget_entity_name` (user or repository scope), alert recipients and cost-center user / repo
  resources; `login` under `organization` / `owner` / `enterprise` objects is an org name and not
  harvested.
- **k-merge (Build 2).** No complementary suppression (the brief's acceptance requires
  `teams_merged == 1` for teams of 7, 5, 3; published tables apply `core.kanon` later). Labels in
  parentheses (`(enterprise)`, `(org:…)`, `(other)`) are never merged; a team with an outcome row of
  ≥ k users keeps its label. Aggregates whose team changed are re-keyed on their new dims (the old
  natural id hashed the small team's label) and summed with collisions; cost-line, license,
  activity and budget ids do not depend on the team and are kept. Outcome rows of merged teams are
  summed per (date, `(other)`, source kind).
- **Aggregate-only (Build 3).** One seat per (snapshot date, person) — the seats API row wins over
  the activity-report row (a multi-org seat is billed once); entity `org:<o>`, or `enterprise` for
  seats without an org. Small rows merge along a ladder: team → `(other)`, then surface and the seat
  flags generalized (`surface`/`assigned_via_team` → null, booleans → false), then the bucket
  (null); a published coarse row absorbs later merges; the residual below k is dropped and counted
  (people) in `dq.copilot_export_suppressed` (shared with activity-count residuals).
  `created_over_30d` is false when `seat_created` is unknown; `zero_cost_30d` is false unless AI
  credit rows cover the 30 days before the snapshot. Activity counts: entity `enterprise`,
  `snapshot_ms` = first day of the month, `app_interactions` = Σ `app_prompts`, `ide:<family>` via
  `core.catalog.editor_family`. Cost lines are summed per natural key without the principal (new
  ids, exact decimal `quantity`). The manifest keys are closed, so degraded detector kinds are listed
  as dq codes `dq.copilot_export_aggregate_only` and `dq.copilot_export_kind_off.<kind>`.
- **Members and manifest.** All six record members are always written (empty when no records);
  the reader accepts a missing member only when its manifest count is 0. Records are deduplicated per
  natural key (latest `fetched_ms`, then canonical JSON); outcomes per (date, team, source kind).
  Records outside `[since, until]` are dropped (`dq.copilot_export_outside_window`; configuration
  is kept); per-request records are dropped (`dq.copilot_export_lane_records_dropped`); unrecognized
  input files are skipped (`dq.copilot_export_unrecognized_input`), none recognized → `UsageError`.
  `sources[].files` counts input files per adapter (the answers count as one file of
  `admin-answers`).
- **Reader.** Structure / limit / count / name-dim / content re-check failures → `SourceError`;
  record validators → `ContractViolation`. Key-id adoption is the store's job (R-E21; the addendum's
  `opts.adopt_key_ids` check was withdrawn, CORE-AMENDMENTS CA-47).
- **Activity report.** `Unspecified` / empty surface → None (no surface); unrecognized strings →
  `other`; empty `last_authenticated_at` → `none_90d`, an absent column → `unknown`; duplicate
  logins (case-insensitive) keep the most recent activity; without a `report_time` column the run
  date is used (`dq.copilot_activity_report_no_report_time`); timestamps with offsets are converted
  to UTC dates. `SourceInfo.name_key_id` is set because `name_hmac` is an `h_` value.
- **Answers.** Per-entity keys are `enterprise` | `org:<login>` | `cc:<name>`; `capped_policy` keys
  must be `cc:<name>` and flatten to `capped_policy.<name>` (`core.pool` strips `cc:` either way);
  `seat_policy` keys must be `org:<login>`. Floats, duplicate keys and values outside the vocabularies
  are refused; `_help*` keys and a BOM are accepted.
- **Logins.** `pseudonym_of` and the activity report HMAC the stripped login with its case kept
  (SPEC §5.1); the erasure pre-filter matches case-insensitively. A shared normalization is proposed
  in `CONTRACT-CHANGE-CP-HANDOFF-1.md`.
- **R-E43** does not apply: this package emits no inference (no requests or lanes in a bundle).
- **Guide.** Paths use the `{e}` / `{org}` placeholders of addendum §19.4; the manual write calls
  of §19.4 are listed (for CP-WIRE's `AUTH_TABLE` gate) without recommending their scopes.

## Unverified facts (VERIFY; shipped as labelled text, nothing priced)

- UI click paths of the AI usage, detailed and activity reports and the dashboard export (§19.5
  #33); the 24-hour e-mail link validity is documented by GitHub Docs.
- Whether "Enterprise billing: read" (App) / a billing manager's classic PAT may `POST` the report
  export (#30); App access to the enterprise seats list (#21); billing-manager access to
  `GET /orgs/{org}/copilot/billing` (#32).
- Whether the dashboard NDJSON export has the API record shape (#34).
- Whether the activity report lists never-active seat holders; JetBrains / Visual Studio / Xcode /
  Neovim / Eclipse / Copilot CLI `last_surface_used` strings (#22, #31; `verified: false` in facts).
- The adapters' login normalization (CONTRACT-CHANGE-CP-HANDOFF-1).
