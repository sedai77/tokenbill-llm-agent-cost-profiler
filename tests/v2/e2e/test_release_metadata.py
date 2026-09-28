"""Release metadata contract for the v0.2 integration."""

from __future__ import annotations

from pathlib import Path

from tokenbill import __version__

REPO = Path(__file__).resolve().parents[3]


def test_current_package_version_has_a_changelog_section() -> None:
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"## [{__version__}] - " in changelog
    assert (f"[Unreleased]: https://github.com/sedai77/"
            f"tokenbill-llm-agent-cost-profiler/compare/v{__version__}...HEAD") in changelog


def test_readme_links_to_the_enterprise_pilot_guide() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    guide = REPO / "docs" / "ENTERPRISE.md"
    assert "[enterprise pilot guide](docs/ENTERPRISE.md)" in readme
    assert guide.is_file() and "## Pilot Exit Criteria" in guide.read_text(encoding="utf-8")
