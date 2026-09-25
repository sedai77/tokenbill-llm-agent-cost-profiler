"""``vscode_paths`` per platform and the opt-in snippet (CP-VSCODE brief, Provides and Build 6)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tokenbill.adapters import copilot_vscode_collect as cvc
from tokenbill.core import facts
from tokenbill.core.builders import CANARY

FACT = facts.copilot_vscode_traces()
GS = "User/globalStorage/github.copilot-chat/agent-traces.db"

MAC_HOME = "/Users/dev"
LINUX_HOME = "/home/dev"
WIN_HOME = "C:\\Users\\dev"
WIN_GS = GS.replace("/", "\\")

CASES = [
    ("darwin", {"TMPDIR": "/var/folders/xy/T/"}, MAC_HOME, [
        f"{MAC_HOME}/Library/Application Support/Code/{GS}",
        f"{MAC_HOME}/Library/Application Support/Code - Insiders/{GS}",
        "/var/folders/xy/T/copilot-agent-traces.db"]),
    ("linux", {}, LINUX_HOME, [
        f"{LINUX_HOME}/.config/Code/{GS}",
        f"{LINUX_HOME}/.config/Code - Insiders/{GS}",
        f"{LINUX_HOME}/.vscode-server/data/{GS}",
        f"{LINUX_HOME}/.vscode-server-insiders/data/{GS}",
        "/tmp/copilot-agent-traces.db"]),
    ("linux", {"XDG_CONFIG_HOME": "/srv/xdg/", "TMP": "/scratch"}, LINUX_HOME, [
        f"/srv/xdg/Code/{GS}",
        f"/srv/xdg/Code - Insiders/{GS}",
        f"{LINUX_HOME}/.vscode-server/data/{GS}",
        f"{LINUX_HOME}/.vscode-server-insiders/data/{GS}",
        "/scratch/copilot-agent-traces.db"]),
    ("linux", {"TMPDIR": "/"}, LINUX_HOME, [
        f"{LINUX_HOME}/.config/Code/{GS}",
        f"{LINUX_HOME}/.config/Code - Insiders/{GS}",
        f"{LINUX_HOME}/.vscode-server/data/{GS}",
        f"{LINUX_HOME}/.vscode-server-insiders/data/{GS}",
        "/copilot-agent-traces.db"]),
    ("linux", {"XDG_CONFIG_HOME": "relative/ignored", "TEMP": "/t/"}, LINUX_HOME, [
        f"{LINUX_HOME}/.config/Code/{GS}",
        f"{LINUX_HOME}/.config/Code - Insiders/{GS}",
        f"{LINUX_HOME}/.vscode-server/data/{GS}",
        f"{LINUX_HOME}/.vscode-server-insiders/data/{GS}",
        "/t/copilot-agent-traces.db"]),
    ("win32", {"APPDATA": "C:\\Users\\dev\\AppData\\Roaming",
               "TEMP": "C:\\Users\\dev\\AppData\\Local\\Temp\\"}, WIN_HOME, [
        f"C:\\Users\\dev\\AppData\\Roaming\\Code\\{WIN_GS}",
        f"C:\\Users\\dev\\AppData\\Roaming\\Code - Insiders\\{WIN_GS}",
        "C:\\Users\\dev\\AppData\\Local\\Temp\\copilot-agent-traces.db"]),
    ("win32", {"SystemRoot": "D:\\Windows"}, WIN_HOME, [
        f"C:\\Users\\dev\\AppData\\Roaming\\Code\\{WIN_GS}",
        f"C:\\Users\\dev\\AppData\\Roaming\\Code - Insiders\\{WIN_GS}",
        "D:\\Windows\\temp\\copilot-agent-traces.db"]),
    ("win32", {"TMP": "C:\\"}, WIN_HOME, [
        f"C:\\Users\\dev\\AppData\\Roaming\\Code\\{WIN_GS}",
        f"C:\\Users\\dev\\AppData\\Roaming\\Code - Insiders\\{WIN_GS}",
        "C:\\copilot-agent-traces.db"]),
]


@pytest.mark.parametrize(("platform", "env", "home", "expected"), CASES)
def test_vscode_paths_table(platform: str, env: dict, home: str, expected: list[str]) -> None:
    got = cvc.vscode_paths(platform=platform, env=env, home=Path(home))
    assert got == [Path(p) for p in expected]


def test_platform_aliases_and_defaults() -> None:
    env = {"TMPDIR": "/tmp"}
    home = Path("/home/dev")
    assert cvc.vscode_paths(platform="cygwin", env={}, home=Path(WIN_HOME))[0] == Path(
        f"C:\\Users\\dev\\AppData\\Roaming\\Code\\{WIN_GS}")
    assert cvc.vscode_paths(platform="freebsd13", env=env, home=home) == cvc.vscode_paths(
        platform="linux", env=env, home=home)
    assert cvc.vscode_paths(platform="macos", env=env, home=home)[0] == Path(
        f"/home/dev/Library/Application Support/Code/{GS}")
    default = cvc.vscode_paths()
    assert default and default[-1].name == "copilot-agent-traces.db"


def test_templates_come_from_facts() -> None:
    assert FACT.db_file == "agent-traces.db" and FACT.tmp_fallback_file == "copilot-agent-traces.db"
    assert FACT.global_storage_dir == "github.copilot-chat"
    for plat in ("darwin", "linux", "win32"):
        assert len(FACT.paths[plat]) == 2
        assert all(FACT.global_storage_dir in t for t in FACT.paths[plat])


def test_optin_snippet() -> None:
    text = cvc.optin_snippet()
    block = text[text.index("{"):text.index("}") + 1]
    assert json.loads(block) == {"github.copilot.chat.otel.dbSpanExporter.enabled": True}
    assert "user" in text and "managed" in text
    assert "7 days / 100 sessions" in text and "at least once a day" in text
    assert "Never collected: prompts, responses" in text and "team-level" in text
    assert "copilot me" in text and "false" in text
    assert CANARY not in text and not re.search(r"\d{13}", text)
    assert text == cvc.optin_snippet()
