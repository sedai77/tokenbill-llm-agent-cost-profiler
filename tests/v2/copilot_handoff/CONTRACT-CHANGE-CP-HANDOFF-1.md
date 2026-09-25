# CONTRACT-CHANGE-CP-HANDOFF-1 — one shared GitHub login normalization for `p_` pseudonyms

**Status:** proposal to the contract owner (F-KIT agent); CP-HANDOFF implements the current
contract and does not work around it.

## What

The CP-HANDOFF brief defines `pseudonym_of(login, *, key)` as "the `p_` value the adapters produce
for `login` under the export key (`core.ids.pseudonym` with the adapters' login normalization)".
No core contract defines that normalization. SPEC §5.1 says central-ingest adapters compute
`pseudonym(opts.principal_key, "p", raw)`; the Copilot briefs (CP-BILL, CP-ORGDATA, CP-HANDOFF) do
not say whether the raw login is case-folded or stripped first.

GitHub logins are case-insensitive, and the same person may be reported as `Octo-Cat` by one source
and `octo-cat` by another (or typed in lower case by an admin answering an erasure request). If one
adapter HMACs the raw value and another the lower-cased value, one person gets two `p_` values:
seats × activity × credits joins break, k counts double-count, and `copilot pseudonym` may print a
`p_` that matches none of the person's rows (an erasure request would silently miss data).

## What CP-HANDOFF does now

- `github-copilot-activity-report` and `pseudonym_of` use `core.ids.pseudonym(key, "p",
  login.strip())` — the raw login with surrounding whitespace removed, case preserved (SPEC §5.1
  literally).
- Team / cost-center map lookups are exact first, then case-insensitive (like ADMIN's adapters).
- The erasure pre-filter (`exclude_logins`) matches logins case-insensitively, so an excluded
  person is dropped whatever case a source uses.
- The guide tells the admin to type the login exactly as GitHub shows it.

## Proposed signature (core, additive)

```python
# tokenbill/core/ids.py
def login_pseudonym(key: bytes, login: str) -> str:
    """The p_ pseudonym of a GitHub login: pseudonym(key, "p", login.strip().casefold())."""
```

Every GitHub adapter (CP-BILL `username`, CP-ORGDATA `user_login` / `assignee.login`, CP-HANDOFF
`login`) and `copilot pseudonym` would call it, and `assert_adapter_conforms` could check one
login planted in two cases yields one `p_`. Changing the normalization later changes every existing
`p_` value, so it should be decided before the first real export (Copilot release gate §21.6 (5)).
