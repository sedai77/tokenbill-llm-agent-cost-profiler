"""``core.testing.assert_adapter_conforms`` on the events-only fixture, one session file and a
store-backed home (SPEC §3.18, §21 #4)."""

from __future__ import annotations

from pathlib import Path

from tokenbill.adapters.copilot_cli import CAPABILITIES, CopilotCliAdapter
from tokenbill.core import registry
from tokenbill.core.protocols import Adapter
from tokenbill.core.testing import assert_adapter_conforms

from .helpers import HOME, SID_A, SID_B, T0, Events, make_store, opts, row, session_file, usage

EVENTS_ONLY = {"timing", "events", "lanes_exact", "params", "credits", "appended"}


def test_registered_and_protocol() -> None:
    adapter = registry.get_adapter("copilot-cli")
    assert isinstance(adapter, CopilotCliAdapter) and isinstance(adapter, Adapter)
    assert adapter.name == "copilot-cli" and adapter.capabilities == CAPABILITIES
    assert registry.sniff_adapter(session_file(HOME, SID_A)).name == "copilot-cli"


def test_conforms_events_only_home() -> None:
    result = assert_adapter_conforms(CopilotCliAdapter(), HOME, expect_capabilities=EVENTS_ONLY,
                                     opts=opts())
    assert len(result.sessions) == 2


def test_conforms_single_session_file() -> None:
    assert_adapter_conforms(CopilotCliAdapter(), session_file(HOME, SID_B),
                            expect_capabilities=EVENTS_ONLY,
                            opts=opts())


def test_conforms_with_the_store(tmp_path: Path) -> None:
    ev = Events()
    ev.start("conf-1", selectedModel="claude-sonnet-4.5")
    ev.user()
    ev.message("c1", 120, turnId="0")
    ev.compaction()
    ev.shutdown({"claude-sonnet-4.5": usage(10_000, 8_000, 1_000, 120)})
    ev.write(session_file(tmp_path, "conf-1"))
    make_store(tmp_path / "session-store.db", [row(1, "conf-1", T0 + 3_200, turn=0),
                                               row(2, "conf-1", T0 + 90_000)],
               sessions=[{"id": "conf-1", "cwd": "/w", "repository": "acme/w"}])
    options = opts(experimental=frozenset({"copilot-store"}))
    result = assert_adapter_conforms(CopilotCliAdapter(), tmp_path,
                                     expect_capabilities=EVENTS_ONLY | {"usage_sequence"},
                                     opts=options)
    assert result.aggregates == []
