# CONTRACT-CHANGE-VERIFY-1: `cluster_days` cluster kind `gateway`

**Owner of the contract:** F-KIT (`core.testing.MemoryStore`), STORE (`store.db.SqliteStore`),
contract owner for `LedgerStore.cluster_days` (SPEC §3.6, §7.1).

## What

SPEC §13.2 names three cache-isolation units a rollout may randomize: workspace, MDM group, and
**IdP group via the Claude apps gateway**. `Attribution.extra` already allowlists the key
`gateway` (`core.records.EXTRA_KEYS`), but `LedgerStore.cluster_days(cluster_kind=…)` has no
documented list of kinds, and the reference `MemoryStore` accepts only `team`, `workspace` and
`mdm_group` (`UsageError` for anything else).

## Why it matters

`verify.panel.build_panel` reads active developer-days from `cluster_days` (they survive identity
retention, §13.1). With `cluster_kind="gateway"` the store raises `UsageError`, so a plan whose
clusters are gateway IdP groups (`verify.rollout.ORG_WIDE_RANDOMIZABLE`, the SPEC's path for
randomizing an org-wide server-managed setting) cannot be measured.

## Proposed change (additive)

- Document the `cluster_days` kinds in §3.6: `team` → `Attribution.team`, `workspace` →
  `Attribution.workspace_id`, `mdm_group` → `extra["mdm_group"]`, **`gateway` →
  `extra["gateway"]`**.
- `MemoryStore.cluster_days` and `SqliteStore.cluster_days` accept `gateway` (same semantics as
  `mdm_group`; the `cluster_day` table already keys by `cluster_kind`).

No field or signature changes.

## What VERIFY does meanwhile

`verify.panel.CLUSTER_FIELDS` already maps `gateway` → `extra["gateway"]` (table-driven), so the
panel works as soon as the store accepts the kind; until then `build_panel(cluster_kind="gateway")`
surfaces the store's `UsageError` (pinned by `test_panel.py::test_mdm_group_and_workspace_clusters`).
