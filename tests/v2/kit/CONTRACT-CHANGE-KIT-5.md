# CONTRACT-CHANGE-KIT-5 — additive surface of the F-KIT modules (for the record)

Nothing in SPEC §3.18/§3.20/§3.23 was renamed or removed. F-KIT added keyword-only parameters with
defaults and extra public names that wave-2 packages may use:

- `core.keys.load(path, *, runner=None, platform=None, notes=None)` and
  `load_or_create(path=None, *, runner=None, platform=None, notes=None)` — injectable `icacls` runner
  and platform for the Windows ACL path (D44); `notes` receives `dq.windows_acl_not_enforced`.
  Constants `DEFAULT_KEY_PATH`, `KEY_BYTES`.
- `core.kanon`: `PERSON_DIMS`, `RESCOPE_LEVELS`, `other_label(k)`.
- `core.catalog`: `LEVER_CLASSES`.
- `core.testing`: `FakeReplayer(table=None, *, fn=None)` and `FakeReplayer.policy_key(policy)`;
  `MemoryStore(*, org_key=None, name_key_id=None, pricer=None, now_ms=0)` with the extensions
  `dq_counts()` and `audit_log()`; `fake_price_total(pricer, items)` (the fake of
  `rates.engine.price_total`); `UNPRICED_DQ`; `SOURCES_MASK_BITS`; `conformance_ingest_options(**kw)`;
  `lane_from_table_allowance()`; `SmokeTtlDetector`; the conformance helpers return small summaries
  (`assert_pricer_conforms`, `assert_store_conforms`, `assert_replayer_conforms`) or the checked
  result (`assert_adapter_conforms`, `assert_detector_conforms`), and take optional keyword tuning
  (`samples`, `seed`, `permutations`, `rules`).
- `assert_store_conforms(factory)` calls `factory(org_key=…, name_key_id=…, pricer=…)`.

**Proposal.** Mention these in SPEC §3.18/§3.23 at the next contract update. No change requested.
