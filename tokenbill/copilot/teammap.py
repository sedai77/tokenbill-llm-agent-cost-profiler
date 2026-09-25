"""Team and cost-center maps for GitHub Copilot org data (addendum §15 ``copilot team-map``;
package CP-ORGDATA).

:func:`build_maps` turns GitHub's **user-teams** report (``user-teams-1-day`` NDJSON of the usage
metrics: ``user_login``, ``user_id``, ``day``, ``team_id``, ``slug``) and the enterprise **cost
centers** (``GET /enterprises/{e}/settings/billing/cost-centers``) into the two maps every
CP-ORGDATA / CP-BILL / CP-HANDOFF adapter applies at ingest (``IngestOptions.team_map`` /
``cost_center_map``: login → label), after which the login is dropped:

* **teams** — a team's size is its largest number of distinct members on one day of the input
  (the report is daily and lists seated Copilot users); teams with fewer than *k* members are
  omitted (GitHub already omits teams below 5 seated users per day); a login in several teams
  maps to the team with the most members, ties broken by the smaller slug (deterministic);
* **cost centers** — ``User`` resources map their login directly; ``Team`` resources map the
  members of the user-teams team with that slug (case-insensitive) unless the login is a direct
  resource of a cost center; deleted cost centers are skipped; no *k* applies (cost centers are
  billing entities whose names the billing reports already carry per row).

Keys are :func:`~tokenbill.adapters.github_config.login_key` normalized (lower-cased) logins; no
key or label that looks like an e-mail address (contains ``@``) is ever emitted. The maps hold
logins and therefore stay on the admin's machine (they are inputs of ``copilot export``, never part
of a bundle).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from tokenbill.adapters.github_config import (
    BadRecord,
    iter_documents,
    login_key,
    parse_day,
    safe_label,
)
from tokenbill.core.errors import UsageError

__all__ = ["build_maps", "user_team_records"]

MapSource = Mapping[str, Any] | Path | str


def _sources(value: MapSource | Iterable[MapSource] | None) -> list[MapSource]:
    if value is None:
        return []
    if isinstance(value, (Mapping, Path, str)):
        return [value]
    return list(value)


def _objects(source: MapSource) -> Iterator[Mapping[str, Any]]:
    """The JSON objects of one input: a mapping (a CP-PULL envelope's response), or every record /
    page of a file."""
    if isinstance(source, Mapping):
        body = source.get("response") if isinstance(source.get("request"), Mapping) else source
        values = body if isinstance(body, list) else [body]
        yield from (v for v in values if isinstance(v, Mapping))
        return
    if not isinstance(source, (Path, str)):
        raise UsageError("build_maps: inputs are records, pages or file paths")
    for item in iter_documents(Path(source)):
        values = item.body if isinstance(item.body, list) else [item.body]
        yield from (v for v in values if isinstance(v, Mapping))


def user_team_records(user_teams: MapSource | Iterable[MapSource] | None
                      ) -> list[tuple[str, str, str]]:
    """``(login_key, slug, day)`` of every usable user-teams record (logins without ``@``)."""
    out: list[tuple[str, str, str]] = []
    for source in _sources(user_teams):
        for rec in _objects(source):
            key, slug = login_key(rec.get("user_login")), safe_label(rec.get("slug"))
            if key is None or slug is None or "@" in key or "@" in slug:
                continue
            try:
                day = parse_day(rec.get("day"), "day")
            except BadRecord:  # an undated record still names a membership
                day = ""
            out.append((key, slug, day))
    return out


def _cost_center_objects(cost_centers: MapSource | Iterable[MapSource] | None
                         ) -> Iterator[Mapping[str, Any]]:
    for source in _sources(cost_centers):
        for obj in _objects(source):
            if isinstance(obj.get("costCenters"), list):
                yield from (c for c in obj["costCenters"] if isinstance(c, Mapping))
            elif isinstance(obj.get("resources"), list):
                yield obj


def build_maps(user_teams: MapSource | Iterable[MapSource] | None,
               cost_centers: MapSource | Iterable[MapSource] | None, *, k: int = 5
               ) -> tuple[dict[str, str], dict[str, str]]:
    """``(team_map, cost_center_map)``, both login → label, sorted by login.

    *user_teams*: user-teams records, or paths of NDJSON / JSON / envelope files or directories;
    *cost_centers*: cost-center pages (``{"costCenters": [...]}``), single cost centers, or paths.
    Teams below *k* members are omitted; a login in several teams gets the one with the most
    members (ties: the smaller slug)."""
    if type(k) is not int or k < 1:
        raise UsageError("build_maps: k must be an int >= 1")
    members: dict[tuple[str, str], set[str]] = {}
    teams_of: dict[str, set[str]] = {}
    for key, slug, day in user_team_records(user_teams):
        members.setdefault((slug, day), set()).add(key)
        teams_of.setdefault(key, set()).add(slug)
    size: dict[str, int] = {}
    for (slug, _), people in members.items():
        size[slug] = max(size.get(slug, 0), len(people))
    team_map: dict[str, str] = {}
    for key, slugs in teams_of.items():
        eligible = [s for s in slugs if size[s] >= k]
        if eligible:
            team_map[key] = min(eligible, key=lambda s: (-size[s], s))
    direct: dict[str, set[str]] = {}
    via_team: dict[str, set[str]] = {}
    by_slug: dict[str, set[str]] = {}
    for key, slugs in teams_of.items():
        for slug in slugs:
            by_slug.setdefault(slug.casefold(), set()).add(key)
    for cc in _cost_center_objects(cost_centers):
        name = safe_label(cc.get("name"))
        if name is None or "@" in name or cc.get("state") == "deleted":
            continue
        resources = cc.get("resources") if isinstance(cc.get("resources"), list) else []
        for res in resources:
            if not isinstance(res, Mapping) or not isinstance(res.get("type"), str):
                continue
            kind = res["type"].lower()
            if kind == "user":
                key = login_key(res.get("name"))
                if key is not None and "@" not in key:
                    direct.setdefault(key, set()).add(name)
            elif kind == "team" and isinstance(res.get("name"), str):
                for key in by_slug.get(res["name"].strip().casefold(), ()):
                    via_team.setdefault(key, set()).add(name)
    cc_map = {key: min(names) for key, names in via_team.items()}
    cc_map.update({key: min(names) for key, names in direct.items()})
    return dict(sorted(team_map.items())), dict(sorted(cc_map.items()))
