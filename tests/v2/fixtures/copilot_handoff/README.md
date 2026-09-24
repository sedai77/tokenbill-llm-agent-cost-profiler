# Fixtures of CP-HANDOFF (`tests/v2/fixtures/copilot_handoff/`)

All data is synthetic. No real export, login, e-mail address or repository name appears here; the
planted login `tb-canary-login-7f3a91` is `core.builders.CANARY_LOGIN`.

| file | provenance class | shape follows |
|---|---|---|
| `activity_report.csv` | **primary**-derived (header and value forms); one surface string **third-party**-derived | GitHub Docs, *Metrics data properties for GitHub Copilot* → "Copilot activity report" (see below) |
| `answers_filled.json` | synthetic, schema of this package | `tokenbill/copilot-admin-answers@1` (`tokenbill/copilot/handoff_data/admin_answers.template.json`) |

Every other input of the tests (AI usage / seats / metrics / budget shapes read by the fake
adapters of `tests/v2/copilot_handoff/helpers.py`, and every `.tbx` bundle) is generated in the
tests' temporary directories; the fake shapes only borrow the documented identity field names
(`username`, `assignee.login` / `assignee.id`, `user_login` / `user_id`) so the leak-gate harvester
and the erasure pre-filter are exercised on them. They are not parser acceptance evidence for the
sibling adapters (CP-BILL, CP-ORGDATA own those).

## Provenance note: the Copilot activity report (orchestrator item O-4 / ruling G-2)

Source: the research copy of the GitHub Docs page *Metrics data properties for GitHub Copilot*
(`copilot/raw/_en_enterprise-cloud_latest_copilot_reference_metrics-data.md` in the design
scratchpad; docs path `/en/enterprise-cloud@latest/copilot/reference/metrics-data`, re-opened by the
data-apis fact-check on 2026-09-23). Its section "Copilot activity report" documents:

- the report "shows user activity data for an organization or enterprise" and "refreshes
  automatically every 30 minutes";
- the fields `report_time` (UTC timestamp when the report was generated), `login` (GitHub username),
  `last_authenticated_at`, `last_activity_at` (UTC timestamps) and `last_surface_used` (the Copilot
  feature used most recently: for an IDE the editor name and version, e.g. `VS Code 1.89.1`; for
  GitHub.com a feature name, e.g. `Copilot Chat`; `Unspecified` when IDE details are unavailable or
  no recent activity exists);
- a rolling 90-day retention (consistent with `last_activity_at`, which is `nil` after 90 days
  without activity);
- limitations: GitHub may lack consistent telemetry from third-party IDEs outside VS Code (such as
  JetBrains and Xcode); Copilot Spaces and Spark are not fully recorded.

The same page says the per-user CSV is downloaded from the organization's "Access management"
page; the addendum's enterprise / org "Licensing → Get activity report" click path is **VERIFY**
(addendum §19.5 #33).

`activity_report.csv` uses exactly the documented columns in the documented order, a UTF-8 BOM
(line ends are LF in the repository; the tests also read a CRLF copy), ISO timestamps with `Z`, one
row with quoted `M/D/YYYY h:mm AM/PM` timestamps (the format of spreadsheet re-saves; accepted per
the brief), an empty `last_activity_at`, an empty row tail, `Unspecified`, `VS Code 1.89.1` (the
documented example), `Copilot Chat` (the documented GitHub.com example), an unknown surface
(`Frobnicator IDE 3.1` → `other`), bucket boundaries at 7 / 8 and 90 / 91 days, and `CANARY_LOGIN`.

**Unverified (third-party-derived, `verified: false` in `core.facts`):** the JetBrains surface
string `JetBrains IntelliJ IDEA 2026.2`, and the `Visual Studio`, `Xcode`, `Neovim`, `Eclipse` and
`Copilot CLI` prefixes. Whether the report lists every seat holder including never-active seats is
also unverified (addendum §19.5 #31); the adapter labels its seats "seat holders as listed".
