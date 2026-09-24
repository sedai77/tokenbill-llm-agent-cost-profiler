#!/usr/bin/env python3
"""Token Bill SessionStart hook for Claude Code: warn before an expensive cold resume.

Installed by a Token Bill policy pack (``tokenbill policy``; lever ``cc.cold_resume_hook``) and
registered in managed settings as ``hooks.SessionStart`` with matcher ``resume``. Claude Code passes
the hook input as JSON on stdin; on a resume it carries ``prompt_cache_likely_expired`` and
``estimated_cache_write_usd``. When the cache has likely expired and rebuilding it would cost at
least the threshold (default $1.00; environment variable ``TOKENBILL_HOOK_THRESHOLD_USD``), the hook
prints one JSON object with a ``systemMessage``, e.g.::

    {"systemMessage": "Resuming this session re-writes its prompt cache: about $3.20 to rebuild
     the cache; /compact to continue this task or /clear for a new one"}

Rules: standard library only; never blocks the session (no ``decision`` / ``continue`` output);
always exits 0; never logs or stores any content (the state file holds only a hash of the session
id and timestamps); malformed or unexpected input prints nothing. Frequency caps: at most one
message per session and three per person (this account) in any 7 days, kept in
``~/.cache/tokenbill/hook_counts.json`` (mode 0600). When the state file cannot be read or written
the hook stays silent rather than risk repeating itself.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path

DEFAULT_THRESHOLD_USD = Decimal("1.00")
THRESHOLD_ENV = "TOKENBILL_HOOK_THRESHOLD_USD"
PER_SESSION_CAP = 1
PER_WEEK_CAP = 3
WEEK_MS = 7 * 86_400_000
MAX_INPUT_BYTES = 1_000_000
MAX_STATE_BYTES = 1_000_000
STATE_SCHEMA = "tokenbill/hook-counts@1"


def _threshold() -> Decimal:
    raw = os.environ.get(THRESHOLD_ENV)
    if raw is None:
        return DEFAULT_THRESHOLD_USD
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, ValueError):
        return DEFAULT_THRESHOLD_USD
    if not value.is_finite() or value < 0 or value > Decimal(10) ** 9:
        return DEFAULT_THRESHOLD_USD
    return value


def _usd(value: object) -> Decimal | None:
    """The estimate as a Decimal (the JSON number is parsed as Decimal, never float)."""
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return None
    amount = Decimal(value)
    if not amount.is_finite() or amount < 0 or amount > Decimal(10) ** 9:
        return None
    return amount


def _read_input() -> dict | None:
    try:
        data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(data) > MAX_INPUT_BYTES:
        return None
    try:
        doc = json.loads(data.decode("utf-8"), parse_float=Decimal,
                         parse_constant=lambda _name: None)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) else None


def _state_path() -> Path:
    return Path.home() / ".cache" / "tokenbill" / "hook_counts.json"


def _load_state(path: Path) -> dict:
    try:
        with open(path, "rb") as handle:
            data = handle.read(MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return {}
    if len(data) > MAX_STATE_BYTES:
        return {}
    try:
        doc = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _ints(values: object) -> list[int]:
    if not isinstance(values, list):
        return []
    return [v for v in values if type(v) is int]


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _allowed(session_id: str, now_ms: int) -> bool:
    """Apply the frequency caps and record this message; False when capped or unrecordable."""
    path = _state_path()
    state = _load_state(path)
    session_key = hashlib.sha256(session_id.encode("utf-8", "surrogatepass")).hexdigest()[:32]
    shown = [t for t in _ints(state.get("shown")) if now_ms - WEEK_MS < t <= now_ms]
    sessions_raw = state.get("sessions")
    sessions = {}
    if isinstance(sessions_raw, dict):
        for key, stamps in sessions_raw.items():
            kept = [t for t in _ints(stamps) if now_ms - WEEK_MS < t <= now_ms]
            if isinstance(key, str) and kept:
                sessions[key] = kept
    if len(sessions.get(session_key, [])) >= PER_SESSION_CAP or len(shown) >= PER_WEEK_CAP:
        return False
    shown.append(now_ms)
    sessions.setdefault(session_key, []).append(now_ms)
    _save_state(path, {"schema": STATE_SCHEMA, "shown": shown, "sessions": sessions})
    return True


def _message(amount: Decimal) -> str:
    cents = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return (f"Resuming this session re-writes its prompt cache: about ${cents} to rebuild the "
            "cache; /compact to continue this task or /clear for a new one")


def main() -> int:
    """Run the hook (see the module docstring); always returns 0."""
    try:
        doc = _read_input()
        if doc is None or doc.get("prompt_cache_likely_expired") is not True:
            return 0
        amount = _usd(doc.get("estimated_cache_write_usd"))
        if amount is None or amount < _threshold():
            return 0
        session_id = doc.get("session_id")
        if not isinstance(session_id, str) or not session_id or len(session_id) > 512:
            return 0
        if not _allowed(session_id, time.time_ns() // 1_000_000):
            return 0
        sys.stdout.write(json.dumps({"systemMessage": _message(amount)}) + "\n")
        sys.stdout.flush()
    except Exception:  # never block, never fail the session
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
