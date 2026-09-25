### CP-WIRE — `tokenbill copilot …` command group, admin handoff and plan commands, Copilot pipeline (wave 2)

**Goal.** Wire the Copilot extension into commands for the three people involved: the **GitHub admin** (who
has the rights: `copilot admin-guide`, `copilot pull … --out export.tbx`, `copilot export`, `copilot
inspect`), the **product owner / analyst** (no admin rights: `copilot scan --from export.tbx`, `copilot plan`,
`copilot rates`, `copilot team-map`) and the **developer** (`copilot collect --source vscode|cli|auto`,
`copilot me`), plus the extension hooks generic commands call (`summarize`, `policy_packs`). The plan
(Business vs Enterprise) is never assumed: this package owns its command surface — `copilot plan`, the
`--plan` admin statement, the plan status in every scan and the two-scenario orchestration when the plan is
unknown — on top of the single detection function `core.pool.detect_plans` (F-POOL). Every stage is resolved
lazily so the package is unit-tested with fakes and proven by gate tests. Read SPEC §3.7, §5.4, §15 (all),
§16, §21; addendum DC9, DC13, DC16, DC18, DC23, §2.2, §3.4, §3.8, §5.9, §5.12, §8, §15, §21.4;
`CP-HANDOFF.md`, `CP-VSCODE.md`, `F-POOL.md` (plan detection); rulings R-E21, R-E22.

**Owns.** `tokenbill/commands/copilot.py` (`add_parser(subparsers)`, `run(args) -> int`),
`tokenbill/pipeline/copilot.py`, `tests/v2/copilot_wire/**`.

**Consumes.** `core.registry`, `core.extensions` (`open_record_stores`, `persist`, `run_reconcilers`,
`enrich(…, recon_decisions=…)`, `render_sections`, `count_users_fn`, `policy_packs`), `core.pool`
(`detect_plans`, `build_cells`), `core.types`, `core.protocols`, `core.kanon`, `core.keys`, `core.testing`
(`FakePricer`, `MemoryStore(adopt_key_ids=…)`, `MemoryRecordStore`, `FakeReplayer`), `core.builders`. Lazily
at runtime only (keyword parameters with default resolvers, never at import time or in unit tests): WIRING
`pipeline.common` (`build_env`, `open_store(…, adopt_key_ids=…)`, `ingest_paths`), RECON `merge_reports`,
PLAN `build_action_plan`, TRACE `write_trace_v2`, CP-PLAN `plan_copilot_scenarios`, CP-POLICY
`admin_actions` / `build_copilot_packs` / `budget_design`, CP-OUT `assemble_summary`, CP-LOCAL
`collect_incremental_copilot` / `session_ids`, CP-VSCODE `collect_vscode_extracts` / `vscode_paths` /
`optin_snippet`, CP-PULL `pull` / `private_workdir` / `HANDOFF_KINDS` / `AUTH_TABLE`, CP-HANDOFF
`export_from_files` / `inspect_bundle` / `render_inspect` / `read_team_map_csv` / `admin_guide_text` /
`admin_answers.parse_answers` / `admin_answers.template_text` / `pseudonym_of`, CP-ORGDATA `teammap.build_maps`,
OUT renderers.

**Provides.**
- `run_scan_copilot(*, sources: CopilotSources, flags: CopilotFlags, env_factory=None, store_factory=None,
  stages=None) -> RunResult`; `run_copilot_export(…) -> ExportReport`; `run_collect_copilot(*, source:
  str, home, vscode: VsCodeSources, out_dir, state, opts, now_ms, writer=None) -> list[Path]`;
  `plan_status(store, record_stores, *, window, flags) -> tuple[PlanEvidence, ...]`.
- Hooks `summarize(store, record_stores, ctx, findings, plan, pricer, *, today, k) -> CopilotSummary` and
  `policy_packs(store, record_stores, ctx, findings, result, *, out_dir, current, cohort_by, include_tradeoffs)
  -> list[PolicyPack]` (the `ExtensionSpec` entries `summary_builder` and `policy_targets["github-copilot"]`;
  `policy_packs` adapts the host call to CP-POLICY's `build_copilot_packs` — addendum §11.3's
  `policy_packs(...)` names this hook).
- The `copilot` command group (all commands and flags of addendum §15 plus the ones below, **except**
  §15's `--adopt-key-id`, `--allow-mixed-keys` and `--hash-workspaces`, which R-E21 and CP-HANDOFF replace:
  adoption is automatic for the first bundle, a second export key id is refused, and org / cost-center names
  are organizational data kept in clear).

**Build.**
1. **Admin commands** (need GitHub rights; everything else needs none).
   - `copilot admin-guide [-o FILE] [--answers-template]` prints CP-HANDOFF's guide or writes the
     questionnaire template.
   - `copilot pull --out DIR` records raw sources (admin's own analysis; unchanged behavior). `copilot pull
     --out FILE.tbx [--keep-raw DIR] [--user-teams-from-pull | --team-map-csv FILE] [--answers FILE] [--k 5]
     [--key-file PATH] [--aggregate-only]` = `private_workdir` → `pull(HANDOFF_KINDS, …)` → team map (from
     the pulled `user-teams-1-day` via CP-ORGDATA `build_maps`, or the CSV) → `run_copilot_export` → work
     directory removed (or moved to `--keep-raw`). Token flags as addendum §15; no token command is executed.
   - `copilot export --in PATH… [--user-teams FILE… | --team-map-csv FILE] [--cost-center-map FILE]
     [--answers FILE] [--k 5] [--key-file PATH] [--rotate-key] [--exclude-logins FILE] [--aggregate-only]
     [--since/--until] --out FILE.tbx`: offline, no token, no network (socket guard in tests). Export key:
     `--key-file` or `~/.config/tokenbill/copilot-export.key` through `core.keys.load_or_create` (0600),
     printed only as its key id; `--rotate-key` renames the old key file with a date suffix, creates a new one
     and passes `key_rotated=True`; `--exclude-logins FILE` (one login per line, read into memory, never
     echoed) → CP-HANDOFF `exclude_logins`. The same key, rotate and exclude flags apply to `copilot pull --out
     FILE.tbx`. Leak-gate abort → exit 3 with CP-HANDOFF's message; `--content` other than `none` → exit 2.
   - `copilot pseudonym [--key-file PATH] --login LOGIN|-` (admin only; erasure requests): reads the login
     from stdin when `-`, prints only the `p_` from CP-HANDOFF `pseudonym_of` (the analyst runs `tokenbill purge
     --principal p_…`); a missing key file → exit 2 (never creates one).
   - `copilot inspect FILE.tbx [--format text|json]`: counts, teams (≥ k) with seat counts, plan evidence, dq
     codes, key ids; never a record.
2. **Analyst commands.** `copilot scan --from FILE.tbx` (and `--from DIR` for a raw pull) opens the store
   with `adopt_key_ids=True` (R-E21): the first bundle's key ids are adopted and its `p_` / `h_` values kept;
   when an org key is configured (`--db` store created with one, or `--key-file`), VS Code / CLI collector
   files (`r_` / `c_` principals) given in the same run are pseudonymized with it and join the bundle's data by
   team only; a second bundle under another key id is refused with exit 2 and the hint "use the same export
   key every month" (or `--db` pointing at a fresh store). New scan flags:
   `--activity-report FILE…`, `--answers FILE`, `--plan ENTITY=business|enterprise|mixed` (repeatable; an
   admin statement stored as run flag `plan.<entity>`; **no default**), `--vscode-traces FILE…`,
   `--vscode-otel FILE…`,
   `--experimental copilot-report-quota|copilot-jetbrains-otel|copilot-store|copilot-cli-otel-file`.
   `copilot plan [same inputs as scan]` prints, per entity × month, the detected plan, its source, the
   evidence lines, conflicts, and when unknown: "unknown — both scenarios are shown" plus the how-to-find-out
   hint (seats API `plan_type`, detailed report seat SKU `copilot_for_business` / `copilot_enterprise`, the
   licensing page, or ask the admin to fill `answers.json`); exit 0 in every case.
3. **`copilot scan` order** (addendum §15 amended): run flags (CLI) and admin answers →
   `ConfigSnapshot(kind="run_flags")` (entities `"run"` and `"admin_answers"`; CLI flags win, conflicts →
   `dq.copilot_statement_conflict`); ingest every input (sniffed; `--adapter` override) into STORE and the
   record stores; `run_reconcilers` (+ RECON for other channels) → `merge_reports` with `rounding_remainders`
   from `LedgerStats.source_stats` when the store provides it; `reconciled_channels` and `recon_decisions` from
   the merged report's `decisions`; `enrich(…, recon_decisions=…)` (pools per plan scenario, plan evidence,
   outcomes); `run_detectors(aggregates_only=False)` per shard then `(True)` once; `rescope_findings(
   count_users=count_users_fn(...))`; PLAN for Copilot lanes when present; `summarize` (CP-PLAN per scenario,
   admin actions, `assemble_summary` with `plan_status`); policy packs with `--policy-out`; `RunResult` with
   `copilot` set. When any entity's plan is unknown, the terminal header states it before any number.
4. **Developer commands.** `copilot collect --source auto|vscode|cli` (alias `collect copilot-cli` → `--source
   cli`): `auto` runs VS Code first when a database or outfile exists — `vscode_paths()` unless
   `--vscode-traces PATH`; `--vscode-otel-outfile PATH` — writing CP-VSCODE extracts into `--out`, then the
   CLI collector with `skip_session_ids = covered_session_ids` (VS Code spans are richer than CLI events-only
   data); identity modes and `--principal-ref`, `--collection-key-file`, `--team`, `--attr` as the CC
   collector; `--ci` requires `--billing-path`; content `none` only. `copilot me`: install key, identity mode
   `install`; reads VS Code `agent-traces.db` **by default** (first existing `vscode_paths()` entry), then
   `~/.copilot`; own lanes only; note "your organization's pool status is not visible from this machine";
   without VS Code data prints CP-VSCODE's opt-in snippet; when the only Copilot surface found is JetBrains
   (seat editor family from `--seats`, or none found) prints "JetBrains keeps no local per-request Copilot
   usage; ask your admin for the org export".
5. Exit codes per SPEC §15 (`--live` without a token flag → 2; token expiry during a pull → 3 with the resume
   hint; leak-gate abort → 3; reconciliation failure is reported, exit 3 only with `--require-reconciled`).
   Everything offline unless `--live` or `pull`.

**Acceptance tests.**
- Unit (fakes for every stage): `copilot scan` calls the stages in order; aggregate detectors run once;
  `reconciled_channels` and `recon_decisions` reach `enrich`; CLI flags and answers become two `run_flags`
  snapshots, the CLI one wins on conflict with the dq code; `--plan` without `=` or with another value →
  exit 2; no `--plan` → no `plan.*` flag; an unknown-plan fixture puts "both scenarios" before the first
  number in the terminal output.
- `copilot export` with fake CP-HANDOFF: key created 0600 on first use and reused (same key id twice); the
  team map built from `--user-teams` via a fake `build_maps` or from the CSV; a socket guard proves no network;
  a leak-gate `PrivacyError` → exit 3 and no output file.
- `copilot pull --out x.tbx` with a fake opener and fake exporter: the work directory is 0700, exists during
  the export and is gone afterwards (also after an injected exception); `--keep-raw D` moves it; the raw
  directory path never appears in the bundle manifest.
- `copilot scan --from x.tbx` on `MemoryStore(adopt_key_ids=True)`: `p_` values kept; a second bundle under a
  different key id → exit 2 with the hint.
- `copilot plan`: known plan (seat lines), conflict (seat lines vs statement), unknown (activity report only)
  → the three printed forms; exit 0.
- `copilot pseudonym` with a fake `pseudonym_of`: the login read from stdin never appears in stdout / stderr /
  logs; missing key file → exit 2 and no file created. `copilot export --rotate-key` twice → two key ids, the
  old file kept with a date suffix; `--exclude-logins` passes the set unchanged and prints only its size.
- `copilot scan --from x.tbx` on an org-keyed `MemoryStore(adopt_key_ids=True)` together with a CP-VSCODE
  extract carrying a `c_` principal: both kept, no per-person join across the two key ids.
- `copilot collect --source auto` with fake collectors: VS Code first, its `covered_session_ids` passed as
  CLI `skip_session_ids`; `--source vscode` with no database → exit 2 and the opt-in snippet; `copilot me`
  opens no file outside the VS Code path, `~/.copilot` and the output; `collect --ci` without
  `--billing-path` → exit 2; `--content full` → exit 2.
- `core.extensions.rewrite_argv(["scan", "--copilot", "--since", "2026-09-01"])` reaches `copilot scan`;
  `collect copilot-cli` reaches `copilot collect --source cli`; `scan --org` untouched.
- Argparse introspection: every subcommand and flag above and of addendum §15 registered; importing the module
  imports no sibling wave-2 package (`sys.modules`).
- Gate (`@pytest.mark.gate`, `importorskip` on every real module): (a) CP-SYNTH-W files → `copilot scan` end to
  end with real adapters, `RateCard`, `SqliteStore`, record store, reconciler, detectors, plan, policy and
  renderers: exit 0, verdicts "(synthetic; schema unverified)", every §18 plant present, canary and logins
  absent, byte-identical JSON across two runs with `--deterministic`; (b) the same world through `copilot
  export` (UI files) and `copilot pull --out x.tbx` (fake opener) → two bundles, both scanned with
  `--from` on a keyless store → the same findings as (a) modulo seat-assignment kinds the UI path cannot
  know; (c) the `plan_unknown` world → both scenario columns, never one number; (d) every `AUTH_TABLE` call
  appears in `admin_guide_text()`.

**Size.** ~2.85k LOC including tests.
