### CP-PULL — Live pulls of GitHub Copilot billing, metrics, seats, config and agent tasks (wave 2)

**Goal.** Record every Copilot source from GitHub's APIs into files the adapters read, safely and resumably:
read-only GETs plus the report-export POST, tokens from an env var or a private token file (re-read on
401 because GitHub App installation tokens expire after one hour), unit-by-unit progress with `--resume`,
per-repository agent-task enumeration with a user token, and no secrets or signed URLs on disk. The pulled
directory is **raw**: it holds GitHub logins (AI usage report `username`, seats `assignee.login`, metrics
`user_login`) and is therefore never the artifact handed to the product owner (owner answer 1). For the admin
handoff (`copilot pull … --out export.tbx`) the pull records into a private temporary work directory, CP-WIRE
runs CP-HANDOFF's export on it, and the raw directory is deleted unless `--keep-raw DIR` is given. Read SPEC
§12.5, §8.6, §21; addendum DC13, DC16, §5.12, §19.4 (auth table), §19.5 #21; `CP-HANDOFF.md` (the bundle it
feeds).

**Owns.** `tokenbill/copilot/{pull_common,pull_billing,pull_metrics}.py`, `tests/v2/copilot_pull/**`.

**Consumes.** `core.jsonl` (`open_private`, `write_jsonl`), `core.secrets` (redaction checks),
`core.errors`, stdlib `urllib`, `ssl`, `json`, `time`, `hashlib`. No adapter imports (files only).

**Provides.**
- `TokenSource` (env var name or private file path; `get()` re-reads a file; never logs the value).
- `pull(kinds, *, enterprise, orgs, since, until, token, user_token=None, agent_repos=(), out_dir, resume=False,
  opener=None, sleep=time.sleep, now_ms) -> Manifest` and the `Manifest` JSON (`units[{id, kind, window or
  day, status, files, repos?}]`, no auth data).
- `AUTH_TABLE` (the §19.4 rows, printed by `copilot pull --help`; CP-WIRE's gate test checks that every row's
  call appears in CP-HANDOFF's admin guide).
- `HANDOFF_KINDS = ("ai_usage", "metered", "summary", "metrics", "seats", "config")` — what `--out *.tbx`
  pulls (metrics = `users-1-day`, `user-teams-1-day`, `enterprise-1-day`; config = budgets + user-states,
  cost centers, `GET /orgs/{org}/copilot/billing`); agent tasks are never part of a handoff.
- `private_workdir(*, parent: Path | None = None, keep_raw: Path | None = None) -> ContextManager[Path]` — a
  0700 temporary directory (Windows: owner-only ACL through `core.jsonl` conventions, else the ACL warning dq);
  on exit it is removed recursively (also after an exception or `KeyboardInterrupt`), or moved to `keep_raw`
  (created 0700; an existing non-empty `keep_raw` → `UsageError` before any request).

**Build.**
1. Units: one report-export window (≤ 31 days; summarized ≤ 366), one metrics report day, one REST page set
   (seats per org / enterprise, org billing, budgets + user-states, cost centers, usage summary per month and
   per cost center incl. `none`, ai_credit usage per month), one agent-task repository. The manifest is
   rewritten atomically after every unit; `resume=True` skips completed units.
2. HTTP: TLS on, 30 s timeout, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2026-03-10`,
   `Link` pagination (`per_page=100`), secondary-rate-limit backoff honoring `retry-after` /
   `x-ratelimit-reset` (≤ 60 s per wait, bounded retries), 409 on export POST → wait and retry.
3. Auth: before each unit `token.get()`; on 401 re-read once; a second 401 stops with a typed
   `TokenExpired` error (CLI exit 3 with the resume hint). A token file must be private (0600 on POSIX; ACL
   warning path on Windows via `open_private` conventions) or `UsageError`.
4. Export: POST `{report_type, start_date, end_date, send_email: false}`, poll `GET …/reports/{id}` every
   30 s up to 30 min per unit (then leave the unit incomplete), download each `download_urls` entry
   immediately with the same opener, store the CSV under the unit id; never store the URL.
5. Metrics: per day `GET …/copilot/metrics/reports/<name>?day=`; 204/404 → "not ready"; days newer than D-3
   re-pulled on the next run; download `download_links` immediately.
6. Agent tasks: only with `user_token` and `agent_repos`; per repo page `/agents/repos/{o}/{r}/tasks`; record
   the repository count; never call `/agents/tasks` in fleet mode.
7. Handoff mode: CP-WIRE calls `pull(HANDOFF_KINDS, …, out_dir=<private_workdir>)`; a unit left incomplete
   (token expiry, 30-min export cap) keeps the work directory for `--resume` only when `--keep-raw` was given,
   else the pull stops with exit 3 and the message "re-run with --keep-raw DIR to resume" (raw data is never
   left behind silently).
8. Recording: envelopes `{"request": {path, query}, "response": {...}}`; strip `Authorization`, cookies,
   signed URLs and every response header except `Link`; `core.secrets.find_secrets` over each file before
   the rename → any hit aborts the unit.

**Acceptance tests** (fake opener; the socket guard proves no network).
- Export POST → poll (processing → completed) → download; a 409 then success; a unit exceeding 30 min stays
  incomplete and is resumed.
- Token file rotated mid-pull: a 401 triggers one re-read and the pull continues; a second 401 raises
  `TokenExpired`; `--resume` then skips completed units and finishes; the token value never appears in any
  file, manifest or log capture.
- Pagination across 3 pages; secondary-rate-limit headers honored with the injected `sleep`.
- Agent tasks without a user token → `UsageError`; with 2 repos → two units and `repos: 2` in the manifest;
  `/agents/tasks` never requested.
- A token file with mode 0644 → `UsageError` (POSIX).
- Recorded files contain no `Authorization`, no signed URL, no header except `Link`.
- `private_workdir`: mode 0700; removed after normal exit, after an exception and after `KeyboardInterrupt`;
  with `keep_raw` moved there; an existing non-empty `keep_raw` refused before the first request (fake opener
  records zero calls).

**Size.** ~2.0k LOC including tests.


---
**ORCHESTRATOR ADDITIONS (binding):** SPEC Appendix E.7 R-E43 — set `billing_path` to `copilot_pool` or `copilot_direct` on every Copilot inference you emit.
