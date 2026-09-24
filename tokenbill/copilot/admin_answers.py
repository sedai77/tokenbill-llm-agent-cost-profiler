"""The admin questionnaire ``admin_answers.json`` (brief CP-HANDOFF Build 8, addendum §5.17).

What no GitHub report shows — the plan the Licensing page states, seat counts, billing mode, renewal
date, cap policies of cost centers, compliance, promotion eligibility, paid-usage and CLI billing
policies, budget stop flags and org seat policies — is answered by the admin in a closed-vocabulary
JSON file (schema ``tokenbill/copilot-admin-answers@1``; template :func:`template_text`). Every
answer is a **statement** (R17): it ranks below any data source (``core.pool.detect_plans``), and
findings name "admin statement" as their source. ``unknown`` and ``null`` answers are omitted —
never a default. Free text is refused (``UsageError``).

:func:`parse_answers` returns the ``ConfigSnapshot(kind="run_flags", source_kind=
"tokenbill.admin_answers", entity_id="admin_answers")`` first (attrs validated against
``core.records.CONFIG_KEYS["run_flags"]``), then one ``ConfigSnapshot(kind="org_settings")`` per
stated org seat policy with only ``seat_management_setting`` (a pulled org-settings snapshot of the
same org wins over it).
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

from tokenbill.core.errors import UsageError
from tokenbill.core.records import ConfigSnapshot

__all__ = ["ANSWERS_SCHEMA", "FIELDS", "SOURCE_KIND", "parse_answers", "template_text"]

#: Schema of the answers file.
ANSWERS_SCHEMA = "tokenbill/copilot-admin-answers@1"
#: ``ConfigSnapshot.source_kind`` of every snapshot made from the answers.
SOURCE_KIND = "tokenbill.admin_answers"
#: Answer fields, in template order.
FIELDS = ("plan", "pool_seats", "billing_mode", "renewal_date", "capped_policy", "compliance",
          "promo_eligible", "paid_usage_policy", "org_cli_billing_policy", "budget_stop",
          "seat_policy")

_UNKNOWN = "unknown"
_PLANS = ("business", "enterprise", "mixed")
_SEAT_PLANS = ("business", "enterprise")
_BILLING_MODES = ("metered", "volume", "azure")
_CAP_POLICIES = ("block", "continue")
_COMPLIANCE = ("none", "data_residency", "fedramp")
_POLICIES = ("enabled", "disabled")
_SEAT_POLICIES = ("assign_all", "assign_selected")
_MAX_BYTES = 2**20
_MAX_SEATS = 10**9
_ORG_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
_FIELD_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,40}\Z")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_BAD_NAME_RE = re.compile(r"[\x00-\x1f\x7f-\x9f@]|://")


def template_text() -> str:
    """The packaged questionnaire template (every field ``unknown``, one ``_help_<field>`` note
    per field); :func:`parse_answers` of it yields an empty ``run_flags`` snapshot."""
    return (resources.files("tokenbill.copilot.handoff_data")
            .joinpath("admin_answers.template.json").read_text(encoding="utf-8"))


def _fail(field: str, why: str) -> UsageError:
    return UsageError(f"admin answers: {field}: {why}")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise UsageError("admin answers: a key appears twice in one object")
        out[key] = value
    return out


def _no_float(_token: str) -> Any:
    raise UsageError("admin answers: numbers must be whole numbers")


def _is_unknown(value: object) -> bool:
    return value is None or value == _UNKNOWN


def _entity(field: str, key: object, *, kinds: tuple[str, ...] = ("enterprise", "org", "cc")
            ) -> str:
    """Validate an entity key: ``enterprise``, ``org:<login>`` or ``cc:<name>``."""
    if not isinstance(key, str):
        raise _fail(field, "keys must be entity ids")
    if key == "enterprise" and "enterprise" in kinds:
        return key
    if key.startswith("org:") and "org" in kinds and _ORG_RE.match(key[4:]):
        return key
    if key.startswith("cc:") and "cc" in kinds:
        name = key[3:]
        if name.strip() == name and 0 < len(name) <= 128 and not _BAD_NAME_RE.search(name):
            return key
    allowed = " | ".join({"enterprise": "enterprise", "org": "org:<login>",
                          "cc": "cc:<cost center name>"}[k] for k in kinds)
    raise _fail(field, f"keys must be {allowed}")


def _choice(field: str, value: object, choices: tuple[str, ...]) -> str | None:
    if _is_unknown(value):
        return None
    if isinstance(value, str) and value in choices:
        return value
    raise _fail(field, "value must be one of " + ", ".join((*choices, _UNKNOWN)))


def _flag(field: str, value: object) -> bool | None:
    if _is_unknown(value):
        return None
    if type(value) is bool:
        return value
    raise _fail(field, "value must be true, false or unknown")


def _date(field: str, value: object) -> str | None:
    if _is_unknown(value):
        return None
    if isinstance(value, str) and _DATE_RE.match(value):
        try:
            _dt.date.fromisoformat(value)
        except ValueError:
            pass
        else:
            return value
    raise _fail(field, "value must be a date YYYY-MM-DD or unknown")


def _count(field: str, value: object) -> int | None:
    if _is_unknown(value):
        return None
    if type(value) is int and 0 <= value <= _MAX_SEATS:
        return value
    raise _fail(field, "value must be a whole number of seats or unknown")


def _mapping(field: str, value: object) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _fail(field, "must be an object keyed by entity")
    return value


def _flatten(doc: Mapping[str, Any]) -> tuple[dict[str, str | int | bool], dict[str, str]]:
    attrs: dict[str, str | int | bool] = {}
    seats: dict[str, str] = {}
    for key, value in _mapping("plan", doc.get("plan")).items():
        choice = _choice("plan", value, _PLANS)
        if choice is not None:
            attrs[f"plan.{_entity('plan', key)}"] = choice
    for key, value in _mapping("pool_seats", doc.get("pool_seats")).items():
        entity = _entity("pool_seats", key)
        for plan, n in _mapping("pool_seats", value).items():
            if plan not in _SEAT_PLANS:
                raise _fail("pool_seats", "plans must be business or enterprise")
            count = _count("pool_seats", n)
            if count is not None:
                attrs[f"pool_seats.{entity}.{plan}"] = count
    for key, value in _mapping("billing_mode", doc.get("billing_mode")).items():
        choice = _choice("billing_mode", value, _BILLING_MODES)
        if choice is not None:
            attrs[f"billing_mode.{_entity('billing_mode', key)}"] = choice
    for key, value in _mapping("renewal_date", doc.get("renewal_date")).items():
        date = _date("renewal_date", value)
        if date is not None:
            attrs[f"renewal_date.{_entity('renewal_date', key)}"] = date
    for key, value in _mapping("capped_policy", doc.get("capped_policy")).items():
        choice = _choice("capped_policy", value, _CAP_POLICIES)
        if choice is not None:
            name = _entity("capped_policy", key, kinds=("cc",))[3:]
            attrs[f"capped_policy.{name}"] = choice
    compliance = _choice("compliance", doc.get("compliance"), _COMPLIANCE)
    if compliance is not None:
        attrs["compliance"] = compliance
    promo = _flag("promo_eligible", doc.get("promo_eligible"))
    if promo is not None:
        attrs["promo_eligible"] = promo
    for name in ("paid_usage_policy", "org_cli_billing_policy"):
        choice = _choice(name, doc.get(name), _POLICIES)
        if choice is not None:
            attrs[name] = choice
    for key, value in _mapping("budget_stop", doc.get("budget_stop")).items():
        flag = _flag("budget_stop", value)
        if flag is not None:
            attrs[f"budget_stop.{_entity('budget_stop', key)}"] = flag
    for key, value in _mapping("seat_policy", doc.get("seat_policy")).items():
        choice = _choice("seat_policy", value, _SEAT_POLICIES)
        if choice is not None:
            seats[_entity("seat_policy", key, kinds=("org",))] = choice
    return attrs, seats


def _load(path: Path) -> dict[str, Any]:
    path = Path(path)
    try:
        with open(path, "rb") as fh:
            raw = fh.read(_MAX_BYTES + 1)
    except OSError:
        raise UsageError(f"admin answers {path.name}: not readable") from None
    if len(raw) > _MAX_BYTES:
        raise UsageError(f"admin answers {path.name}: larger than 1 MiB")
    try:
        doc = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_no_duplicates,
                         parse_float=_no_float, parse_constant=_no_float)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise UsageError(f"admin answers {path.name}: not valid JSON") from None
    if not isinstance(doc, dict):
        raise UsageError(f"admin answers {path.name}: must be a JSON object")
    return doc


def parse_answers(path: Path, *, snapshot_ms: int) -> list[ConfigSnapshot]:
    """Parse an answers file into ``[run_flags snapshot, org_settings snapshots…]``.

    The ``run_flags`` attrs are ``plan.<entity>``, ``pool_seats.<entity>.<plan>`` (int),
    ``billing_mode.<entity>``, ``renewal_date.<entity>`` (``YYYY-MM-DD``),
    ``capped_policy.<cost center>``, ``compliance``, ``promo_eligible`` (bool),
    ``paid_usage_policy``, ``org_cli_billing_policy`` and ``budget_stop.<entity>`` (bool) — only
    for stated answers. A wrong schema, an unknown key (``_help*`` keys are ignored), a value
    outside its vocabulary (free text), a non-date renewal or a fractional number → ``UsageError``
    naming the field, never the value."""
    if type(snapshot_ms) is not int or snapshot_ms < 0:
        raise UsageError("admin answers: snapshot_ms must be an int >= 0")
    doc = _load(path)
    if doc.get("schema") != ANSWERS_SCHEMA:
        raise UsageError(f"admin answers: schema must be {ANSWERS_SCHEMA}")
    for key in doc:
        if key == "schema" or key in FIELDS or key.startswith("_help"):
            continue
        shown = repr(key) if _FIELD_TOKEN_RE.match(key) else "a key"
        raise UsageError(f"admin answers: unknown field {shown}; allowed: " + ", ".join(FIELDS))
    attrs, seats = _flatten(doc)
    try:
        out = [ConfigSnapshot(snapshot_ms=snapshot_ms, source_kind=SOURCE_KIND, kind="run_flags",
                              entity_id="admin_answers", attrs=tuple(sorted(attrs.items())),
                              fetched_ms=snapshot_ms)]
        out.extend(ConfigSnapshot(snapshot_ms=snapshot_ms, source_kind=SOURCE_KIND,
                                  kind="org_settings", entity_id=org,
                                  attrs=(("seat_management_setting", policy),),
                                  fetched_ms=snapshot_ms)
                   for org, policy in sorted(seats.items()))
    except Exception as exc:  # a record invariant (e.g. snapshot_ms past 9999-12-31)
        raise UsageError(f"admin answers: {type(exc).__name__} building the snapshots") from None
    return out
