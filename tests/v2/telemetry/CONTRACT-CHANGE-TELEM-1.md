# CONTRACT-CHANGE-TELEM-1 — tool-result token sizes have no home in `AppendedItem`

Status: proposed (TELEM, wave 2). Implemented against the current contract; no core file edited.

## What

SPEC §5.9 says the beta `claude_code.tool` span's `result_tokens` is "recorded in the item" (the
`AppendedItem` built from the matching `claude_code.tool_result` event). `core.records.AppendedItem`
has no field that can carry a token count: it has `kind`, `name`, `n_bytes`, `is_error`, `images`.

A second, related observation: the current Claude Code monitoring reference
(<https://code.claude.com/docs/en/monitoring-usage>, read 2026-09-23) no longer lists
`tool_result_size_bytes` among the `claude_code.tool_result` attributes (SPEC §19.4 still does). A
file without it gives the item no size at all.

## Why it matters

The appended size is what the context detectors use to explain growth between two requests of a lane.
Without a size, an OTel-only fleet reports tool results with `n_bytes = 0`, which reads as "empty"
rather than "unknown".

## Proposed (additive, with a default — nothing renamed or removed)

```python
@dataclass(frozen=True, slots=True)
class AppendedItem:
    kind: str
    name: str | None
    n_bytes: int
    is_error: bool = False
    images: int = 0
    est_tokens: int | None = None   # NEW: provider/client token estimate of the item; never billed
```

and a sentence in §5.9: "`n_bytes` is `tool_result_size_bytes` when present, else 0 with
`est_tokens` from the beta `claude_code.tool` span's `result_tokens` when present".

## What TELEM does until then

* `n_bytes = tool_result_size_bytes` when the attribute is present, else `0`.
* `result_tokens` of the joined `claude_code.tool` span is summed into
  `IngestResult.stats["tool_result_tokens"]` (content-free, per source file) so nothing is lost.
* Nothing else in the TELEM adapters depends on the change.
