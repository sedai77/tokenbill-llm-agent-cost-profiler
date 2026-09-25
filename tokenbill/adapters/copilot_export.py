"""``copilot-export``: the admin's handoff bundle ``*.tbx`` (``tokenbill/copilot-export@1``,
addendum §5.16, brief CP-HANDOFF Build 6).

The product owner ingests the bundle without GitHub rights, token or network. Sniffing needs no
decompression: a bundle is a zip (``PK\\x03\\x04``) whose first member is ``manifest.json``, so the
member name sits at offset 30 of the file; any other zip (an ``.xlsx``, a jar) is not claimed.
``read`` delegates to :func:`tokenbill.copilot.handoff.read_bundle` (zip-bomb and traversal limits,
exact member names, manifest schema and counts, ``core.records.from_json`` on every line) and
returns the records unchanged with ``SourceInfo.principal_key_id`` / ``name_key_id`` set to the
bundle's export key id: a store opened with ``adopt_key_ids=True`` adopts it (ruling R-E21) and
keeps every ``p_`` / ``h_`` value; a second bundle under another key id is refused by the store.
"""

from __future__ import annotations

from pathlib import Path

from tokenbill.core.errors import UsageError
from tokenbill.core.types import IngestOptions, IngestResult

__all__ = ["ADAPTER_NAME", "CopilotExportAdapter"]

ADAPTER_NAME = "copilot-export"
_ZIP_MAGIC = b"PK\x03\x04"
_MANIFEST_NAME = b"manifest.json"


class CopilotExportAdapter:
    """Reads a Copilot handoff bundle (capabilities: those present among ``aggregates``, ``cost``,
    ``copilot_billing``, ``licenses``, ``activity``, ``config``, ``outcomes``)."""

    name = ADAPTER_NAME
    capabilities = frozenset({"aggregates", "cost", "copilot_billing", "licenses", "activity",
                              "config", "outcomes"})

    def sniff(self, path: Path, head: bytes) -> bool:
        """A zip whose first local header names ``manifest.json`` (``head[30:43]``)."""
        return head[:4] == _ZIP_MAGIC and head[30:43] == _MANIFEST_NAME

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """The bundle's records (manifest data-quality codes re-emitted as ``export:<code>``
        notes); an unsafe or inconsistent bundle raises ``SourceError`` / ``ContractViolation``
        and nothing is returned."""
        if not isinstance(opts, IngestOptions):
            raise UsageError(f"{ADAPTER_NAME}: read() needs IngestOptions")
        from tokenbill.copilot.handoff import read_bundle

        _manifest, result = read_bundle(Path(path))
        return result
