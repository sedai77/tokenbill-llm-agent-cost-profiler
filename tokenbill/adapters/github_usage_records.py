"""Refusal of GitHub Copilot usage records (addendum §5.8, DC8; package CP-ORGDATA).

``GET /enterprises/{enterprise}/copilot/usage-records`` (EMU enterprises) returns raw request and
response **bodies** of Copilot sessions — prompts, code and answers. Token Bill never ingests
content: :class:`UsageRecordsRefusal` (registry name ``github-usage-records``) claims these pages
and streams (``type``, ``github_request_id``, ``endpoint``, ``body``, ``@timestamp``; JSON arrays,
NDJSON, CP-PULL envelopes) only so that they are not mistaken for another source, and returns an
empty result with ``dq.raw_bodies_ignored`` (a count of records, nothing else). The file is never
parsed as JSON: records are counted by their ``"github_request_id"`` keys in the raw bytes, so no
body is ever decoded into memory as a value.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from tokenbill.adapters.github_config import head_keys, head_request_path
from tokenbill.core.errors import SourceError, UsageError
from tokenbill.core.ids import pseudonym, stable_id
from tokenbill.core.jsonl import open_text
from tokenbill.core.types import DataQualityNote, IngestOptions, IngestResult, SourceInfo

__all__ = ["UsageRecordsRefusal"]

#: An unescaped ``"github_request_id":`` key (an escaped one inside a body string does not match).
_RECORD_KEY_RE = re.compile(rb'(?<!\\)"github_request_id"\s{0,64}:')
_CHUNK = 1 << 20
_TAIL = 128


class UsageRecordsRefusal:
    """Claims Copilot usage-record pages and ingests nothing (raw bodies are content)."""

    name = "github-usage-records"
    capabilities: frozenset[str] = frozenset()

    def sniff(self, path: Path, head: bytes) -> bool:
        """A recorded ``/copilot/usage-records`` request or records with ``github_request_id``
        next to ``body`` / ``endpoint``."""
        req = head_request_path(head)
        if req is not None and req.endswith("/copilot/usage-records"):
            return True
        keys = head_keys(head)
        return "github_request_id" in keys and bool(keys & {"body", "endpoint", "@timestamp"})

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Count the records of *path* and return nothing but the count."""
        if not isinstance(opts, IngestOptions):
            raise UsageError("read() needs IngestOptions")
        path = Path(path)
        files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
        if not path.exists():
            raise SourceError(f"{path.name}: not found")
        digest, total, records = hashlib.sha256(), 0, 0
        for f in files:
            tail = b""
            try:
                with open_text(f) as fh:
                    for chunk in iter(lambda fh=fh: fh.read(_CHUNK), b""):
                        total += len(chunk)
                        digest.update(chunk)
                        window = tail + chunk
                        # keys inside the carried-over tail were counted with the previous chunk
                        records += (len(_RECORD_KEY_RE.findall(window))
                                    - len(_RECORD_KEY_RE.findall(tail)))
                        tail = window[-_TAIL:]
            except SourceError:
                raise
            except Exception as exc:  # corrupt compressed stream (OSError, EOFError, zlib.error)
                raise SourceError(f"{f.name}: unreadable ({type(exc).__name__})") from None
        source_id = (pseudonym(opts.name_key, "s", f"{self.name}:{path.name}") if opts.name_key
                     else stable_id("s", self.name, digest.hexdigest()))
        note = DataQualityNote(code="dq.raw_bodies_ignored", severity="warn", count=records,
                               detail="Copilot usage records carry raw request/response bodies: "
                                      "refused, nothing ingested")
        return IngestResult(
            source=SourceInfo(source_id=source_id, adapter=self.name,
                              name_hmac=(pseudonym(opts.name_key, "h", path.name)
                                         if opts.name_key else source_id),
                              sha256=digest.hexdigest(), bytes=total,
                              name_key_id=(opts.name_key_id or None) if opts.name_key else None,
                              principal_key_id=None),
            requests=[], sessions=[], events=[], aggregates=[], cost_lines=[], outcomes=[],
            quarantined=[], notes=[note], stats={"bytes": total, "files": len(files),
                                                 "records_ignored": records},
            capabilities=frozenset())
