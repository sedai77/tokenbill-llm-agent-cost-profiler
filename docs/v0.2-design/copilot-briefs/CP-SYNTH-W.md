### CP-SYNTH-W — Schema-true writers for every Copilot source (wave 2)

**Goal.** Serialize any set of canonical Copilot records into the exact file shapes the Copilot adapters read
— AI usage report CSVs (with the documented quirks), detailed usage CSVs, REST pages, budgets / cost-center /
org-settings JSON, metrics NDJSON, seats pages, agent-task pages, a `~/.copilot` tree, OTel files, a VS Code
`agent-traces.db`, the VS Code OTel outfile and gh-aw `token-usage.jsonl` — plus the **UI downloads of the
no-token handoff path** (owner answer 1): the Copilot activity report CSV, the usage-dashboard NDJSON export
and the admin's `answers.json` — with canary text in every content field the adapters must never read. Read
SPEC §18, §8.8; addendum §5 (every field list and fixture shape), §8, §18; `CP-HANDOFF.md` (activity report,
answers schema), `CP-VSCODE.md` (VS Code paths and DDL).

**Owns.** `tokenbill/synth/copilot_writers.py` (`write_world`, one writer per source),
`tests/v2/copilot_writers/**`.

**Consumes.** `core.records`, `core.types`, `core.money`, `core.ids`, `core.builders` (`CANARY`,
`CANARY_LOGIN`, builders), `core.jsonl.write_jsonl`; stdlib `csv`, `json`, `sqlite3`, `gzip`. Writers take
records as input (build test inputs with the core builders); they never import CP-SYNTH.

**Provides.** `write_world(records, out_dir, *, conventions=("excl",), quirks=True) -> dict[str, Path]`
plus per-source writers (`write_ai_usage_csv`, `write_metered_csv`, `write_billing_pages`,
`write_config_pages`, `write_metrics_ndjson`, `write_seats_pages`, `write_agent_task_pages`,
`write_copilot_home`, `write_otel_file`, `write_vscode_traces_db`, `write_vscode_outfile`,
`write_gh_aw_token_usage`, `write_activity_report_csv`, `write_dashboard_ndjson`, `write_admin_answers`), a
`write_world(…, handoff="api"|"ui")` switch (`"ui"` writes only what an admin can click out of the GitHub UI:
AI usage and detailed CSVs, activity report, dashboard export, answers — no REST pages, no seats list, no org
settings), and a `MANIFEST.json` listing each file with `provenance: "synthetic"` and `schema_source` (the
primary document or repository file its shape follows). Synthetic files are gate inputs only and never count
as parser acceptance evidence (addendum §5 classes: `primary`, `third-party`, `real-redacted`). The `.tbx`
bundle is **not** written here: CP-HANDOFF's writer is the only implementation, and gate tests produce bundles
by running it on these files.

**Build.**
1. AI usage CSV in the documented field order with quirks when `quirks=True`: BOM, `M/D/YY` dates in one file,
   float-tail money strings (e.g. `0.4272621300000001`), `Unknown` quota, `Auto: ` labels, `(fast mode)`
   labels, `code review` pseudo rows with empty usernames, `CANARY_LOGIN` usernames mapped by a team map;
   one file per requested convention; two overlapping exports with one revised amount.
2. Detailed CSV with seat, Actions (three dynamic paths, one `linux_16_core`, one
   `.github/workflows/*.lock.yml`)
   and sandbox rows; summarized CSV; usage-summary and ai_credit JSON pages (both example name variants).
3. Budgets (both SKU field spellings, a $0 user budget), cost centers (with and without pool state), org
   settings with each `seat_management_setting`, metrics NDJSON (users-1-day with the documented field names,
   enterprise-1-day, user-teams-1-day with GitHub's < 5 rule), seats pages (with `assigning_team`), agent-task
   pages per repository.
4. `~/.copilot` tree: `session-state/<id>/events.jsonl` per the SDK schema with `CANARY` in every content field
   (and one concatenated line), plus `session-store.db` using the third-party DDL (for experimental tests).
5. OTel: an OTel-JS dump file (VS Code dialect), an OTLP/JSON collector file mixing a Claude Code resource and
   a Copilot resource, a CLI-envelope file; VS Code `agent-traces.db` from the VS Code DDL with canary rows
   under content keys; gh-aw `token-usage.jsonl` in the `token-usage/v0.28.7` shape.
6. Handoff-path writers: activity report CSV (`report_time, login, last_authenticated_at, last_activity_at,
   last_surface_used`, BOM, `CANARY_LOGIN` logins, surfaces such as `VS Code 1.126.0`, a JetBrains surface
   string, `Copilot Chat`, `Unspecified`); dashboard NDJSON in the API 28-day shape; `answers.json` per
   CP-HANDOFF's template schema (the `plan_unknown` variant leaves `plan` out; the `plan_conflict` variant
   states Enterprise); `write_vscode_traces_db` places canary rows under content keys and in `span_events`,
   and the 3 overlapping conversations also get `~/.copilot/session-state/<id>/events.jsonl` files.

**Acceptance tests.**
- Byte-identical files for identical inputs; files round-trip through a minimal local reader for each shape
  (header names, field names and types exactly as addendum §5).
- Canary present in the written content fields (so the adapters' canary tests are meaningful) and absent from
  every non-content field.
- Gate (`importorskip` on the real adapters): every written file read by the real CP-BILL / CP-ORGDATA /
  CP-LOCAL / CP-OTEL adapters reproduces the input records' token and money totals per team; canary and logins
  absent from every adapter output; the mixed OTLP file yields Claude spans via `otlp` and Copilot spans via
  `copilot-otel` with no duplicates; the activity report through CP-HANDOFF's adapter and the dashboard export
  through CP-ORGDATA's metrics adapter reproduce the input seat counts and 28-day totals; `handoff="ui"` output
  contains no file a UI user cannot download.

**Size.** ~2.2k LOC including tests.


---
**ORCHESTRATOR ADDITIONS (binding):** SPEC Appendix E.7 R-E43 — set `billing_path` to `copilot_pool` or `copilot_direct` on every Copilot inference you emit.
