# CONTRACT-CHANGE-KIT-C-1: consequences of rulings R-E28 and R-E10 outside F-KIT's files

**Raised by:** F-KIT-C (wave 1.5b), for the orchestrator / contract owner.

## 1. R-E28 flips one VERIFY assertion (needs a one-line edit by VERIFY's owner)

SPEC E.4 R-E28 (accepting CONTRACT-CHANGE-VERIFY-1) makes `LedgerStore.cluster_days` accept cluster
kind `"gateway"`; F-KIT-C applied it to `core.testing.MemoryStore` (grouping by
`Attribution.extra["gateway"]`, same semantics as `mdm_group`). VERIFY pinned the interim behaviour
"until the store accepts the kind" in

    tests/v2/verify/test_panel.py::test_mdm_group_and_workspace_clusters (last two lines)
        with pytest.raises(UsageError):
            P.build_panel(store, cluster_kind="gateway", **kw)

which now fails (the panel is built — empty for that fixture, whose requests carry no gateway).
F-KIT-C does not own `tests/v2/verify/**`. Proposed edit (VERIFY / orchestrator): replace the two lines
with `assert P.build_panel(store, cluster_kind="gateway", **kw) == []` (or a fixture request with
`extra={"gateway": "gw"}` and its panel row). No production code changes; `verify.panel` already maps
`gateway` → `extra["gateway"]`.

## 2. R-E10 "note users_unknown" has no field to live in

`PublishedAggregate` and `AggRow` (frozen F-CORE types) carry no notes. F-KIT-C keeps the rows
(`n_users == 0`, no person-proxy key in the grouping) and exposes the note through
`core.kanon.row_notes(row, group_by=…) -> ("users_unknown",)` and the constant
`core.kanon.USERS_UNKNOWN`; renderers (OUT, CP-OUT) should print "users unknown" for such rows. If a
field is preferred, the additive change would be `AggRow.notes: tuple[str, ...] = appended(())`
(F-CORE), filled by `publish`.
