# Token Bill for GitHub Copilot: guide for the GitHub admin

You are asked for a monthly export of your organization's GitHub Copilot usage and billing data so
that a colleague (the "analyst", who has no GitHub admin rights) can analyse Copilot costs **at team
level**. You run one offline command on your own machine; it produces one file, `export.tbx`, which
you hand over. Token Bill never changes anything in GitHub, and the analyst never sees a name.

Items marked **VERIFY** are documented facts we could not yet confirm end to end. Each has a
fallback, and the list in section 10 asks you to tell the analyst what you observed.

## 1. What the analyst receives, and what never leaves your machine

The `.tbx` file is a zip archive with one manifest and six record files. It contains:

- **pseudonymous IDs** (`p_…` for people, `h_…` for repositories and workflows), computed with a key
  that stays with you (section 7). The analyst cannot turn them back into names;
- **team labels** of teams with at least 5 Copilot users; smaller teams are merged into `(other)`
  before anything is written, and people without a team stay unattributed;
- **organization and cost-center names**, plan and billing statements from your answers file;
- **numbers**: AI credits, dollar amounts, token counts, seat counts, activity counts, dates.

It never contains: GitHub logins, e-mail addresses, numeric user IDs, display names, repository
names, workflow paths, prompts or code, access tokens, download links, file names or your key file.

Before the file is written, a **leak gate** re-reads every record and compares it with the logins,
user IDs, e-mail addresses, repository names and workflow paths found in your raw files (kept in
memory only). It also looks for e-mail patterns, secrets and tokens, test canaries and `https://`
links. Any hit stops the export (exit code 3) and nothing is written; the message names the record
file and the kind of value, never the value itself.

With `--aggregate-only` (for works councils that do not allow per-person pseudonyms to leave your
machine) the file holds no per-person record at all: seats and activity become counts per team
(each at least 5 people, smaller counts merged or dropped). The analyst then loses the per-seat
findings (idle seats become counts, plan mix, completions-only seats, duplicate seats, MCP and
context-heavy CLI findings are unavailable).

What you may get back later: a checklist, a managed-settings patch and REST request files. Nothing
in them runs by itself; each item names the settings page or API call and the role it needs.

## 2. Path A: a read-only token (fewer steps each month, more data)

Choose path A if you can create a token; otherwise use path B (section 4). Both produce the same
file format. Path B loses the seat plan and assignment details, the usage-summary cross-check and
daily activity.

**One-off run (recommended): a classic personal access token** of an **enterprise owner or billing
manager** with only the `read:enterprise` scope, plus `read:org` when you also pull organization
seats and organization Copilot settings (`GET /orgs/{org}/copilot/billing`). Give it the shortest
expiry GitHub offers (at most 7 days) and **revoke it right after the run**. The billing endpoints
document roles rather than scopes, and fine-grained tokens are not supported by the billing usage
endpoints.

Store the token in a private file and pass it with `--github-token-file`:

```
umask 077 && printf '%s\n' 'PASTE-TOKEN-HERE' > ~/.config/tokenbill/gh-token
chmod 600 ~/.config/tokenbill/gh-token
```

A token file readable by others is refused. Token Bill never writes the token anywhere.

**Recurring runs: a GitHub App** installed on the enterprise with the read permissions
"Enterprise billing: read" and "Enterprise Copilot metrics: read" and, per organization, "GitHub
Copilot Business: read". Its installation tokens expire after one hour. Token Bill never mints
tokens: your own tooling (for example a scheduled job using `gh`) refreshes the token file, and
Token Bill re-reads the file before each step and after a 401 response.

**Never** grant `manage_billing:copilot`, `admin:org`, `admin:enterprise`, any other `admin:*` scope or any write permission to the token you use with Token Bill.

If a call returns 401, the token expired: refresh the file and re-run with `--resume`. A 403 names
the endpoint and the permission it needs; switch that source to path B. A 409 means another usage
report is being generated; the pull waits and retries.

## 3. The calls path A makes (all read-only, except the export request)

The single command:

```
tokenbill copilot pull --live --github-enterprise SLUG [--github-org ORG ...] \
  --since 2026-09-01 --until 2026-09-30 \
  --sources ai_usage,metered,summary,metrics,seats,config \
  --github-token-file ~/.config/tokenbill/gh-token --out export-2026-09.tbx
```

`tokenbill copilot pull ... --out export.tbx` records the raw responses in a private temporary
directory, builds the export from them and deletes the directory (unless you pass `--keep-raw DIR`).
It calls:

| data | call | why |
|---|---|---|
| AI usage report | `POST /enterprises/{e}/settings/billing/reports` with `report_type` `ai_credit`, then `GET /enterprises/{e}/settings/billing/reports/{report_id}` and the download, in windows of at most 31 days | per user, day and model: credits, gross / discount / net amounts, tokens |
| Detailed usage report | the same export calls with `report_type` `detailed` | seat SKU lines (plan and billing mode), Actions minutes of Copilot workloads |
| AI credit usage | `GET /enterprises/{e}/settings/billing/ai_credit/usage` | monthly cross-check |
| Usage summary | `GET /enterprises/{e}/settings/billing/usage/summary` and `GET /enterprises/{e}/settings/billing/usage` (per cost center) | invoice cross-check |
| Seats | `GET /enterprises/{e}/copilot/billing/seats`, or per organization `GET /orgs/{org}/copilot/billing/seats` | plan per seat, assignment through a team, last activity and editor |
| Organization Copilot settings | `GET /orgs/{org}/copilot/billing` | plan type and seat management setting |
| Usage metrics | `GET /enterprises/{e}/copilot/metrics/reports/users-1-day?day=`, `.../user-teams-1-day?day=`, `.../enterprise-1-day?day=` (organization variants under `/orgs/{org}/copilot/metrics/reports/`) | daily activity, VS Code vs JetBrains split, team labels |
| Budgets | `GET /enterprises/{e}/settings/billing/budgets` and `GET /enterprises/{e}/settings/billing/budgets/{budget_id}/user-states` | budget settings; user states are summarized to counts |
| Cost centers | `GET /enterprises/{e}/settings/billing/cost-centers` | pools and caps |

The export request only creates a download; the download link is used at once and never stored.

**Not requested:** Copilot agent tasks (`GET /agents/repos/{o}/{r}/tasks` needs a per-user token and
a repository list and is not needed for billing), the usage-records API (raw request bodies), audit
logs.

Calls you may later be asked to make **by hand** (they change settings and need write rights, which
you use through your own session in the GitHub UI or your own approved tooling, never through the
read-only token above): `DELETE /orgs/{org}/copilot/billing/selected_users`,
`DELETE /orgs/{org}/copilot/billing/selected_teams`,
`PUT /enterprises/{e}/copilot/policies/coding_agent`, and budget or cost-center `POST` / `PATCH`
requests under `/enterprises/{e}/settings/billing/`.

## 4. Path B: no token (UI downloads and a short questionnaire)

Download these files into one folder (only the first is required):

1. **AI usage report CSV** (with token columns): enterprise **Billing and licensing → Usage → AI
   usage → Get usage report**. At most 31 days per report; the report is e-mailed to your primary
   e-mail address and the download link expires after 24 hours. One report at a time. (Click path
   **VERIFY**.)
2. **Detailed usage report CSV**: **Billing and licensing → Usage → Get usage report**, type
   *detailed* (at most 31 days). Its seat SKU lines decide the plan and billing mode, and its Actions
   rows show the minutes of Copilot code review, cloud agent and agentic workflows. (Click path
   **VERIFY**.)
3. **Copilot activity report CSV**: enterprise or organization settings → **Copilot → Licensing /
   access → Get activity report**. It lists seat holders with their last activity and last used
   surface, but no plan and no assignment. (Click path **VERIFY**.)
4. Optional: **Copilot usage dashboard NDJSON export**: **Insights → Copilot usage → export**. It
   covers a rolling 28-day window and excludes Copilot CLI; it gives the VS Code vs JetBrains split.
   (Record shape **VERIFY**.)

Then fill the questionnaire. Write the template with

```
tokenbill copilot admin-guide --answers-template answers.json
```

and answer what you know: which plan the Licensing page shows (Business, Enterprise, both, or I
don't know), seat counts, billing mode (metered, volume, Azure), renewal date, what happens when a
capped cost center reaches its cap (block or continue), compliance program, promotion eligibility,
paid-usage and CLI billing policies, whether budgets stop usage, and each organization's seat policy
(assign to all or to selected members). Leave `unknown` wherever you are not sure: unknown answers
are ignored, never guessed. The file accepts no free text.

The one command:

```
tokenbill copilot export --in downloads/ --answers answers.json \
  [--user-teams user-teams.ndjson | --team-map-csv teams.csv] --out export-2026-09.tbx
```

It runs offline and creates the export key on first use (section 7). Compared with path A the
analyst gets no per-seat plan (the detailed report's seat SKU lines still decide the plan; without
them the plan is unknown and both the Business and the Enterprise scenario are shown), no seat
assignment (idle seats are reported as "assignment unknown"), no usage-summary cross-check, budgets
and cost centers only as answered, and 28-day instead of daily activity.

## 5. Team labels

Token Bill reports teams, never people. Give it team labels in one of two ways:

- **path A:** the metrics `user-teams-1-day` report (GitHub already omits teams with fewer than 5
  seated users), passed with `--user-teams`;
- **either path:** a CSV with the header `login,team` that you prepare from your identity provider
  or HR export, passed with `--team-map-csv teams.csv`.

Teams with fewer than 5 Copilot users are merged into `(other)` before anything is written. A team
label must not be a login or an e-mail address: the leak gate stops the export and asks you to
rename the team in the CSV. The CSV itself never leaves your machine.

## 6. Check the file before you hand it over

```
tokenbill copilot inspect export-2026-09.tbx
```

prints only counts: record counts, months, teams with their seat counts (at least 5), teams merged,
plan evidence, key IDs, the privacy mode and the leak-scan result. It never prints a record or a
pseudonym. Then send the `.tbx` file through your organization's approved file-transfer channel and
delete the raw downloads (path B). Path A keeps no raw files unless you used `--keep-raw`.

## 7. Keep the export key

The export key is created on first use at `~/.config/tokenbill/copilot-export.key` (readable only by
you). Keep it, back it up according to your organization's secret policy, and **reuse it every
month**: the same key gives the same pseudonymous IDs, so months can be compared. **Never send the
key.** Losing it only breaks month-to-month comparison. `--rotate-key` creates a new key on purpose;
the manifest records the rotation, and months before and after it no longer join per person.

## 8. Erasure requests

To remove one person's data, print their pseudonymous ID. Type the login exactly as GitHub shows
it (upper and lower case matter); it is read from standard input and never logged:

```
tokenbill copilot pseudonym --login -
```

Send the printed `p_…` value to the analyst, who runs `tokenbill purge --principal p_…`. Add the
login to a file (one login per line) and pass it with `--exclude-logins FILE` to every later export:
that person's rows are dropped before anything is pseudonymized, and the manifest counts the dropped
rows.

## 9. Raw files only as a last resort

Handing over raw GitHub files instead of the `.tbx` export is allowed only with the written approval
of your data protection officer or works council. The analyst then runs `tokenbill copilot export`
on receipt and deletes the originals the same day. There is no other shortcut.

## 10. Open points to confirm (VERIFY)

Please tell the analyst what you observed for each item; the guide is corrected from your answers.

1. **VERIFY:** whether a GitHub App with "Enterprise billing: read" (and a billing manager's classic
   token) may call the report export `POST /enterprises/{e}/settings/billing/reports`. On a 403 use
   the UI downloads of path B for the AI usage and detailed reports.
2. **VERIFY:** whether a GitHub App may read the enterprise seats list
   `GET /enterprises/{e}/copilot/billing/seats`. Fallback: an enterprise owner's classic token, or
   the organization seats lists.
3. **VERIFY:** whether an enterprise billing manager (not an organization owner) may read
   `GET /orgs/{org}/copilot/billing` and the organization seats list. Fallback: ask an organization
   owner for these two calls, or use the enterprise seats list.
4. **VERIFY:** the exact UI click paths and button labels of the AI usage report, the detailed usage
   report, the Copilot activity report and the dashboard export, and that the e-mailed download link
   stays valid for 24 hours.
5. **VERIFY:** whether the dashboard NDJSON export has the same record shape as the API usage
   metrics reports. Until confirmed it is read only with the experimental flag, and records of any
   other shape are set aside.
6. **VERIFY:** whether the Copilot activity report lists every seat holder, including seats that
   were never used, and which `last_surface_used` strings JetBrains IDEs report. Until confirmed,
   seat counts from it are labelled "seat holders as listed".
