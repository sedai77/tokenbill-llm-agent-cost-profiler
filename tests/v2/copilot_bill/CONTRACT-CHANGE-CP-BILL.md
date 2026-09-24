# CONTRACT-CHANGE-CP-BILL — notes for the contract owner (no core file was edited)

CP-BILL implements against the frozen core at f2162e4. The items below are divergences or gaps
found while building; each names what CP-BILL does today and a proposal.

1. **`core.builders.make_ai_usage_row` natural id collides Auto and direct rows.** The builder's
   `line_id` uses `cm.model or model` as the model part, so `"Auto: Claude Haiku 4.5"` and
   `"Claude Haiku 4.5"` of one user on one day get the same id although they are two report rows
   (the documented grouping is `date, model, username`, with the label as `model`). CP-BILL's
   adapter uses the model key `auto:<model>` / `<model>:fast` (plain model otherwise, the cleaned
   label for pseudo labels), so both rows survive with their routing. Ids of direct, standard rows
   are identical to the builder's. *Proposal:* the builder adopts the same model key. Likewise
   `make_seat_line` / `make_actions_line` use id schemes that differ from the adapter's metered
   scheme (which also keys `username`, `repository` and the workflow path, the documented grouping of
   the detailed report); consumers must not compare builder ids with adapter ids.

2. **No shared login normalization.** CP-HANDOFF's `pseudonym_of(login, *, key)` must equal "the
   adapters' login normalization", and CP-ORGDATA (seats, metrics), CP-HANDOFF (activity report)
   and CP-BILL must produce the same `p_` for one login. CP-BILL uses
   `core.ids.pseudonym(key, "p", login.strip())` — the raw login, case kept (SPEC §5.1
   `central-ingest`; GitHub writes canonical-case logins in every report). *Proposal:* a core helper
   `core.ids.github_principal(key, login) -> str` with exactly that rule, used by every GitHub
   adapter and `pseudonym_of` (gate-test the equality at gate 1).

3. **Plan-quota carrier (informational).** Addendum §5.1 rule 8 describes a
   `UsageAggregate(github.ai_usage_report.quota)` under flag `copilot-ai-usage-quota` and
   `dq.copilot_quota_ignored`; the brief / CORE-AMENDMENTS (binding) replace it with
   `ConfigSnapshot(kind="plan_quota")` under `copilot-report-quota` and
   `dq.copilot_report_quota_ignored`. CP-BILL implements the brief. `snapshot_ms` is the first
   instant of the month so re-reading a file never adds a second natural key.

4. **Rounding remainders.** `stats["rounding_remainder_e18"]` holds the net-amount remainders (what
   L3 compares with the usage summary); the gross remainders (L1's side) are in
   `stats["rounding_remainder_e18.gross"]`, which `core.extensions.run_reconcilers` does not
   forward. *Proposal (optional):* forward it too if CP-RECON wants an L1 rounding residual.

5. **No-float list.** `tests/v2/core/test_no_float_money.py` `MONEY_PATHS` does not yet list the
   C-30 Copilot modules (e.g. `tokenbill/adapters/github_billing.py`). CP-BILL has its own AST test
   (`test_conformance.py::test_no_float_in_the_money_module`). *Proposal:* add the C-30 paths
   (absent modules are skipped).

6. **Capability `config`.** `github-ai-usage` declares `config` in its maximum capabilities (the
   addendum lists `{aggregates, cost, copilot_billing}`) because plan-quota snapshots are
   configuration records; the result carries it only when such a snapshot is emitted.
