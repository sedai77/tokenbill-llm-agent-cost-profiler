### CP-HANDOFF — Admin handoff kit: pseudonymized export bundle, bundle reader, UI activity report, admin answers, admin guide (wave 2)

**Goal.** The product owner has no GitHub admin rights; a colleague with enterprise-owner, org-owner or
billing-manager rights does the setup. Give that admin one offline command that turns what they downloaded
(API recordings from `copilot pull`, or CSV/NDJSON files clicked out of the GitHub UI) into **one
content-free, pseudonymized file** (`export.tbx`) the product owner can analyze without admin rights and
without network. Nothing in the file names a person: logins, user ids, emails, repository names and workflow
paths never leave the admin's machine; teams below k = 5 are merged into `(other)`; a leak gate aborts the
export on any hit. Also ship the admin guide (what to click or which read-only token to create, which reports
and endpoints) and the admin questionnaire (plan, billing mode, cap policies, compliance) that fills the gaps
no report shows. Owner answers 1 and 2 (plan detection evidence travels in the bundle). Read SPEC §5.1, §7.2,
§7.6, §8 (all), §15, §21; addendum DC7, DC8, §4, §5.1–§5.8, §8, §19.4; `CORE-AMENDMENTS.md` items C-9, C-10,
C-13, C-24, K-5 and rulings R-E21, R-E22.

**Owns.**
- `tokenbill/copilot/handoff.py` (bundle writer, reader, inspector, leak gate, team k-merge, aggregate-only
  reduction, export orchestration over `core.registry` adapters)
- `tokenbill/copilot/admin_answers.py` (questionnaire JSON → `ConfigSnapshot(kind="run_flags")`)
- `tokenbill/copilot/handoff_data/__init__.py` (docstring only),
  `tokenbill/copilot/handoff_data/admin_guide.md`,
  `tokenbill/copilot/handoff_data/admin_answers.template.json`
- `tokenbill/adapters/copilot_export.py` (`CopilotExportAdapter`, registry name `copilot-export`)
- `tokenbill/adapters/github_activity_report.py` (`ActivityReportAdapter`, registry name
  `github-copilot-activity-report`)
- `tests/v2/copilot_handoff/**`, `tests/v2/fixtures/copilot_handoff/**`

**Consumes (core only).** `core.records` (`CostLine`, `UsageAggregate`, `OutcomeAggregate`, `LicenseSnapshot`
with `assigned_via_team: bool | None`, `ActivityDay`, `ConfigSnapshot` with kinds `run_flags`, `seat_counts`,
`activity_counts`, `record_key`, `to_json` / `from_json`, `CONFIG_KEYS`, `EDITOR_FAMILIES`), `core.types`
(`IngestOptions`, `IngestResult`, `SourceInfo`, `DataQualityNote`, `PlanEvidence`), `core.registry`
(`sniff_adapter(path, notes=…)`, `get_adapter` — resolved at runtime; unit tests register fake adapters with
`monkeypatch`), `core.keys` (`load_or_create`, `load`), `core.ids` (`pseudonym`, `key_id`, `stable_id`),
`core.secrets.find_secrets`, `core.pool.detect_plans` (plan evidence for the
manifest), `core.facts` (`copilot_editor_families()` for `last_surface_used`), `core.jsonl.open_private`,
`core.builders` (`CANARY`, `CANARY_LOGIN`, `CANARY_EMAIL`, record builders), `core.testing`
(`assert_adapter_conforms`, `MemoryStore(adopt_key_ids=True)`, `MemoryRecordStore`); stdlib `zipfile`,
`csv`, `json`, `tempfile`, `importlib.resources`. Never imports another wave-2 package (team maps arrive as
plain mappings from CP-WIRE).

**Provides.**
- `EXPORT_SCHEMA = "tokenbill/copilot-export@1"`; `BundleManifest` (frozen dataclass mirroring the manifest
  JSON below) and `ExportReport(out_path, manifest, dq: tuple[DataQualityNote, ...])`.
- `export_from_files(paths: Sequence[Path], out_path: Path, *, key: bytes, team_map: Mapping[str, str],
  cost_center_map: Mapping[str, str], answers: Sequence[ConfigSnapshot] = (), k: int = 5,
  aggregate_only: bool = False, since: str | None, until: str | None, experimental: frozenset[str] =
  frozenset(), exclude_logins: frozenset[str] = frozenset(), key_rotated: bool = False, now_ms: int,
  tool_version: str) -> ExportReport` — sniffs and reads every input through `core.registry` with
  `IngestOptions(identity_mode="central-ingest", principal_key=key, principal_key_id=key_id(key),
  name_key=key, name_key_id=key_id(key), team_map=…, cost_center_map=…, k_anonymity=k, now_ms=…)`, then
  `write_bundle`. `copilot-export` and `github-usage-records` inputs are refused (`UsageError`: no
  re-export, no raw bodies). **Erasure / opt-out:** rows whose raw login (CSV `username` / `login`, JSON
  `assignee.login` / `user_login`) is in `exclude_logins` are dropped by a pre-filter on the raw rows before any
  adapter pseudonymizes them (filtered copies go to a private 0700 temporary directory that is removed
  afterwards, also on failure; the logins never appear in a log line), counted as `rows_excluded` in the
  manifest.
- `pseudonym_of(login: str, *, key: bytes) -> str` — the `p_` value the adapters produce for `login` under
  the export key (`core.ids.pseudonym` with the adapters' login normalization); used by `copilot pseudonym` so
  the admin can answer an erasure request (the analyst then runs `tokenbill purge --principal p_…`).
- `write_bundle(results: Sequence[IngestResult], out_path, *, manifest_seed, leak_terms: frozenset[str], k,
  aggregate_only) -> BundleManifest` and `read_bundle(path) -> tuple[BundleManifest, IngestResult]`.
- `inspect_bundle(path) -> dict[str, object]` (manifest plus per-kind counts, team labels with their seat
  counts ≥ k, plan evidence, dq codes; **counts only, never a record**) and
  `render_inspect(d, fmt="text"|"json")`.
- `harvest_leak_terms(paths, *, team_map_logins: Iterable[str] = ()) -> frozenset[str]` (memory only) and
  `leak_scan(members: Mapping[str, bytes], terms) -> list[tuple[str, str]]` (member, category) — categories
  only, never the matched term.
- `read_team_map_csv(path) -> dict[str, str]` (header `login,team`; blank / duplicate / email-shaped logins →
  `UsageError` naming the line number only).
- `admin_answers.parse_answers(path, *, snapshot_ms) -> list[ConfigSnapshot]` (first the `run_flags`
  snapshot, source kind `tokenbill.admin_answers`, entity `"admin_answers"`; then one `org_settings` snapshot
  per stated seat policy, Build 8), `admin_answers.template_text() -> str`.
- `admin_guide_text() -> str` (the packaged `admin_guide.md`, read with `importlib.resources`).
- Adapters `CopilotExportAdapter` (`copilot-export`) and `ActivityReportAdapter`
  (`github-copilot-activity-report`) registered by F-CORE-C (C-24).

**Build.**
1. **Bundle format** `tokenbill/copilot-export@1`: a stdlib `zipfile` whose **first member is
   `manifest.json`** (so `sniff` sees `PK\x03\x04` and the name `manifest.json` at offset 30 of the head),
   then `records/{aggregates,config,cost_lines,activity,licenses,outcomes}.jsonl` in that fixed order, each
   line `json.dumps(core.records.to_json(rec), sort_keys=True, separators=(",", ":"))`, records sorted by
   `record_key` / id. Deterministic bytes: every member dated 1980-01-01 00:00:00, `ZIP_DEFLATED` level 9,
   `external_attr = 0o100600 << 16`, no comment, no extra fields; the file is written to a private temp file
   next to `out_path` and renamed (0600). Manifest keys (exactly): `schema`, `tool_version`, `created_ms`,
   `window {since, until}`, `entities` (enterprise / `org:<login>` / `cc:<name>`), `orgs`, `sources
   [{adapter, files, records, quarantined}]`, `counts {<kind>: n}`, `dq [{code, count}]`,
   `principal_key_id`, `name_key_id`, `key_rotated` (bool), `rows_excluded` (count), `k`, `teams_merged`
   (number of team labels folded into `(other)`),
   `privacy_mode` (`"pseudonymous"` | `"aggregate_only"`), `plan_evidence [{entity_id, month, plan, source,
   conflict}]` (from `core.pool.detect_plans`), `experimental [...]`, `leak_scan {terms: n, result: "clean"}`.
   Never: logins, user ids, emails, file names, paths, tokens, URLs.
2. **Team k-merge before writing.** Distinct `p_` principals per team label over licenses ∪ activity ∪
   cost lines of the window; every team with fewer than k people becomes `"(other)"` (`core.kanon.other_label`
   labels such as `"(other: <5 users)"` are for published tables; records carry the literal team label
   `"(other)"`) in `CostLine.team`, aggregate `team` dims,
   `LicenseSnapshot.team`, `ActivityDay.team`, budget attrs `team`; `OutcomeAggregate` rows keep the metrics
   adapter's k-merge. Deterministic; `teams_merged` counted. User-scope budgets already carry team / cost
   center only (CP-ORGDATA).
3. **Aggregate-only mode** (`--aggregate-only`): no `p_` anywhere. Cost lines keep amounts with
   `principal=None`; licenses become `ConfigSnapshot(kind="seat_counts", source_kind="tokenbill.copilot_export",
   entity_id="org:<o>")` rows with attrs `team, plan, bucket, surface, assigned_via_team (true|false|null),
   pending_cancellation (bool), created_over_30d (bool), zero_cost_30d (bool), n` plus one summary row per
   (org, team) with `bucket="*"` and `n_people` (distinct principals); activity becomes
   `ConfigSnapshot(kind="activity_counts")` per (team, month) with `n_people`, `interactions`, `cli_requests`,
   `cli_prompt_tokens`, `app_interactions` and `ide:<family>` interaction sums. Any row whose `n` or
   `n_people` is below k is merged into its org's `(other)` row, and a residual below k is dropped (counted in
   `dq.copilot_export_suppressed`). The manifest says which detector kinds degrade (CP-DET-SEATS uses seat
   counts; `plan-mix`, `completions-only-seat`, `duplicate-seat`, `mcp-sprawl`, `context-heavy-cli` are off).
4. **Leak gate (binding).** `harvest_leak_terms` reads the raw inputs itself (stdlib `csv` / `json`, only the
   identity columns: CSV `username`, `login`, `user_login`, `repository`, `workflow_path`, `email`; JSON
   `assignee.login`, `assignee.id`, `user_login`, `user_id`, `login`, `email`, `user.id`, `repository.name`,
   `repositoryName`) plus the team-map CSV logins, keeping the terms in memory only. Before the rename,
   `leak_scan` checks every member: (a) any JSON string value equal (case-insensitive) to a term, or containing
   a term of ≥ 6 characters; numeric ids only against string values; (b) the email pattern
   `[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}`; (c) `core.secrets.find_secrets`; (d) `CANARY`,
   `CANARY_LOGIN`, `CANARY_EMAIL`; (e) `https://` URLs. Any hit deletes the temp file and raises
   `PrivacyError("export aborted: <n> leak(s) in <member>: <categories>")` (CLI exit 3); a team or org label
   equal to a login is reported as such ("rename the team in --team-map-csv").
5. **Reader** (`read_bundle`, used by the adapter): zip-bomb and traversal limits — ≤ 16 members; names must
   be exactly the six record members and `manifest.json`; no directories, no absolute paths, no `..`, no
   symlink mode bits; each member ≤ 512 MiB uncompressed and ≤ 200× its compressed size; total ≤ 2 GiB;
   manifest ≤ 1 MiB and `schema == EXPORT_SCHEMA`; lines parsed with `core.records.from_json` (validators run,
   so a raw login cannot pass a `p_` field); a count mismatch with the manifest → `SourceError`.
6. **`copilot-export` adapter.** `sniff`: head starts with `PK\x03\x04` and `head[30:43] == b"manifest.json"`.
   `read` returns an `IngestResult` with `cost_lines`, `aggregates`, `outcomes`, `licenses`, `activity`,
   `config`; `SourceInfo(adapter="copilot-export", principal_key_id=manifest.principal_key_id,
   name_key_id=manifest.name_key_id, …)`; capabilities = those present among `{aggregates, cost,
   copilot_billing, licenses, activity, config, outcomes}`; manifest dq codes re-emitted as notes (prefixed
   `export:`). The product owner's store accepts the `p_` / `h_` values through key-id adoption (R-E21).
7. **`github-copilot-activity-report` adapter** (UI: Licensing → "Get activity report"; header `report_time,
   login, last_authenticated_at, last_activity_at, last_surface_used`, more columns tolerated, BOM, ISO or
   `M/D/YYYY` timestamps): one `LicenseSnapshot(source_kind="github.copilot_activity_report", plan="unknown",
   assigned_via_team=None, seat_created=None, pending_cancellation=None, org=opts.attribution.workspace_id or
   None)` per login; buckets against `report_time` (`0-7`, `8-30`, `31-90`, `none_90d`; empty
   `last_activity_at` → `none_90d`); `last_surface_used` → editor family via `core.catalog.editor_family`
   (facts-backed, the same function CP-ORGDATA uses; **VERIFY** the surface strings; unknown → `other`); login → team map → `p_` → dropped. Sniff: header has
   `login`, `last_activity_at` and `last_surface_used`. Capabilities `{licenses}`.
8. **Admin answers.** `admin_answers.template.json` (schema `tokenbill/copilot-admin-answers@1`): `plan`
   (per entity `business|enterprise|mixed|unknown`, as shown on the licensing page), `pool_seats`,
   `billing_mode` (`metered|volume|azure|unknown`), `renewal_date`, `capped_policy` (per cost center
   `block|continue|unknown`), `compliance` (`none|data_residency|fedramp|unknown`), `promo_eligible`,
   `paid_usage_policy` and `org_cli_billing_policy` (`enabled|disabled|unknown`), `budget_stop` (per budget
   scope), `seat_policy` (per org `assign_all|assign_selected|unknown`, as shown in the org's Copilot access
   settings). `unknown` and null are **omitted** (never a default). Flattened into `run_flags` attrs
   (`plan.<entity>`, `pool_seats.<entity>.<plan>`, `billing_mode.<entity>`, …) validated against
   `core.records.CONFIG_KEYS["run_flags"]`; `seat_policy` becomes one `ConfigSnapshot(kind="org_settings",
   source_kind="tokenbill.admin_answers", entity_id="org:<o>")` per org with only `seat_management_setting`
   (a pulled org-settings snapshot of the same org wins; the statement is named as such in findings). So
   `parse_answers` returns `list[ConfigSnapshot]` (the `run_flags` snapshot first). Free text anywhere →
   `UsageError`.
9. **Admin guide** (`admin_guide.md`, ≤ 350 lines, the single source of `copilot admin-guide` and of
   INTEGRATION's `docs/COPILOT-ADMIN.md`). Sections, in order: (1) what the product owner receives and what
   never leaves the machine; (2) **token path**: a classic PAT of an enterprise owner or billing manager with
   `read:enterprise` (plus `read:org` for org seats and `GET /orgs/{org}/copilot/billing`), shortest expiry,
   revoked after the run, stored in a 0600 file for `--github-token-file`; for recurring runs a GitHub App
   with "Enterprise billing: read", "Enterprise Copilot metrics: read" and org "GitHub Copilot Business:
   read", whose installation token the admin's own tooling refreshes hourly into the token file (Token Bill
   never mints tokens); never `manage_billing:copilot` or `admin:*`; (3) the exact calls: report export
   `POST/GET /enterprises/{e}/settings/billing/reports` for `ai_credit` and `detailed` in 31-day windows,
   `…/settings/billing/usage/summary`, enterprise and org seats, `GET /orgs/{org}/copilot/billing`, metrics
   `users-1-day`, `user-teams-1-day`, `enterprise-1-day`, budgets (+ `user-states`), cost centers; agent tasks
   excluded; the single command `tokenbill copilot pull … --out export.tbx`; (4) **no-token path**: UI
   downloads — AI usage report CSV (Usage → AI usage → Get usage report, ≤ 31 days, e-mailed link valid 24 h),
   detailed usage report CSV (seat SKUs, Actions), Copilot activity report CSV (Licensing → Get activity
   report), Copilot usage dashboard NDJSON export (28-day, excludes CLI) — then fill `answers.json` and run
   `tokenbill copilot export --in <files> --answers answers.json --user-teams … --out export.tbx`; (5) team
   labels (user-teams report or a `login,team` CSV); (6) `tokenbill copilot inspect export.tbx` before handing
   over; (7) keep the export key (`~/.config/tokenbill/copilot-export.key`, 0600) and reuse it every month so
   pseudonyms stay joinable; never send the key; `--rotate-key` only on purpose (breaks month-to-month joins,
   recorded in the manifest); (8) erasure requests: `tokenbill copilot pseudonym --login -` (login read from
   stdin) prints the `p_` to pass to the product owner for `tokenbill purge --principal`, and the login goes
   into `--exclude-logins FILE` for later exports; (9) raw files only as a last resort with DPO / works-council
   approval — the owner then exports at once and deletes the originals; (10) VERIFY list: whether billing-read
   permits the export POST, App access to enterprise seats, billing-manager access to org Copilot billing,
   exact UI click paths, dashboard NDJSON shape. Every VERIFY item is marked in the text.

**Acceptance tests** (fake adapters via `monkeypatch` on `core.registry.BUILTIN_ADAPTERS`; builders;
`MemoryStore(adopt_key_ids=True)`).
- Round trip: records of every kind (incl. `CANARY_LOGIN`-mapped principals) → `write_bundle` → `read_bundle`
  gives records equal by `to_json`; byte-identical bundles for identical inputs across two processes; the
  file mode is 0600 (POSIX).
- Leak gate: a fake adapter that leaks a harvested login into `CostLine.description`, an email into a budget
  attr, a `https://` URL, a GitHub token-shaped string, or `CANARY_LOGIN` → `PrivacyError`, no output file, the
  message names the member and category but not the term; a team label equal to a login → abort with the
  rename hint; a clean export passes and records `leak_scan.terms`.
- k-merge: teams of 7, 5 and 3 people → the 3-person team appears nowhere, `(other)` holds its rows,
  `teams_merged == 1`; aggregate-only mode: no `p_` value in any member (regex over bytes), seat counts per
  (org, team, bucket) sum to the input seats minus the dropped residual, every `n` and `n_people` ≥ k.
- Reader limits: a member named `../x`, a 2,000:1 member, an extra member, a manifest count mismatch, a
  non-`p_` principal in `licenses.jsonl` → each rejected with `SourceError` / `ContractViolation`.
- `copilot-export` adapter: `assert_adapter_conforms`; sniff true for a bundle and false for any other zip
  (e.g. an `.xlsx`); ingest into `MemoryStore(adopt_key_ids=True)` keeps every `p_` (adopted key id, R-E21)
  while a second bundle under another key id is refused with `UsageError` (nothing ingested).
- Activity report: fixture (primary-derived from the documented columns, `CANARY_LOGIN`, BOM, empty
  `last_activity_at`, an unknown surface) → snapshots with `plan="unknown"`, `assigned_via_team=None`, correct
  buckets; `assert_adapter_conforms`; hypothesis fuzz of the CSV parser.
- Answers: the template parses to an empty `run_flags` snapshot; a filled file gives exactly the flattened
  attrs plus one `org_settings` snapshot per stated `seat_policy`; `unknown` values omitted; unknown keys, free
  text, a non-date renewal → `UsageError`.
- Erasure: `pseudonym_of(CANARY_LOGIN, key=K)` equals the `principal` of that login's rows in a bundle made with
  K; with `exclude_logins={CANARY_LOGIN}` none of its rows (cost lines, licenses, activity) is in the bundle,
  `rows_excluded` counts them, and the leak gate still passes; `key_rotated=True` appears in the manifest.
- Guide: every call of Build 9 (3) and every UI report of Build 9 (4) appears in `admin_guide_text()`; the text
  recommends no `manage_billing:copilot` / `admin:*` scope outside the "never" sentence;
  `admin_answers.template.json` parses (equality with CP-PULL's `AUTH_TABLE` is CP-WIRE's gate test).
- Gate (`@pytest.mark.gate`, `importorskip` CP-BILL / CP-ORGDATA / CP-SYNTH-W): the CP-SYNTH-W world written
  once as API recordings and once as UI files → two bundles whose records are equal after dropping source ids
  (the UI path lacks only what the UI lacks: seat assignment kind, org settings); both pass the leak gate.

**Size.** ~2.5k LOC including tests (plus ≈ 350 lines of packaged guide text and a 40-line JSON template).
Addendum §22 is the design background; where its signatures (`export_bundle`, `pseudonym(login, *,
key_file)`), manifest keys, leak-gate matching rule or `--hash-workspaces` option differ from this brief, this
brief wins (CORE-AMENDMENTS "Precedence").


---
**ORCHESTRATOR ADDITIONS (binding):** SPEC Appendix E.7 R-E43 — set `billing_path` to `copilot_pool` or `copilot_direct` on every Copilot inference you emit.
