"""Allocation rules (SPEC §14.6; package OUT).

A rules file is JSON, git-versioned and **ordered**::

    {"rules": [{"id": "sdk-services",
                "match": {"workspace_id": {"in": ["h_…"]}, "extra.mdm_group": {"eq": "x"},
                          "entrypoint": {"prefix": "sdk-"}},
                "set": {"team": "platform", "cost_center": "cc-42", "project": "p",
                        "workload_class": "service"},
                "split": {"method": "proportional", "by": "dev_days",
                          "targets": ["payments", "search"]}}]}

* ``match`` — every condition must hold (an empty ``match`` matches everything). Fields: the
  ``Attribution`` fields (:data:`MATCH_FIELDS`), ``extra.<key>`` for the allowlisted extra keys, and
  the request's ``model`` / ``channel`` / ``provider``. Operators ``eq``, ``in``, ``prefix`` and
  ``regex`` (id fields only, :data:`REGEX_FIELDS`; a safe subset: at most 200 characters, no
  backreferences, no nested unbounded quantifiers; Python ``re.search`` semantics). Values are the
  ledger's values (hashed ids stay ``h_…``).
* ``set`` — target fields ``team``, ``cost_center``, ``project``, ``workload_class`` (a
  ``WorkloadClass`` value). **First match wins per target field**: rules are visited in order and a
  field is taken from the first matching rule that sets it. A field no rule sets keeps the
  request's own value; a request left without a team is ``"(unallocated)"``.
* ``split`` — the first matching rule's split owns the team: :func:`apply_rules` marks the team as
  ``"(split:<rule id>)"`` and :func:`split_rows` apportions those ledger rows over the targets in
  proportion to their active developer-days (largest remainder, so the parts sum to the original to
  the nano and token); the split rows carry ``AllocatedMethodId`` / ``AllocatedMethodDetails``
  (:class:`AllocatedCostRow`, read by ``outputs.focus``).
* :func:`coverage` — allocated dollars / all dollars over billed-basis rows, the chargeback KPI
  (≥ 95% is the gate of ``export focus --chargeback``).

Parsing errors raise ``UsageError`` with content-free messages (rule index and field only).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from tokenbill.core.errors import UsageError
from tokenbill.core.labels import Basis
from tokenbill.core.money import ratio
from tokenbill.core.records import EXTRA_KEYS, Attribution, Request, WorkloadClass
from tokenbill.core.types import LedgerCostRow

__all__ = [
    "MATCH_FIELDS",
    "OPERATORS",
    "REGEX_FIELDS",
    "SCHEMA",
    "TARGETS",
    "UNALLOCATED",
    "AllocatedCostRow",
    "Condition",
    "Rule",
    "RuleSet",
    "Split",
    "apply_rules",
    "coverage",
    "is_allocated",
    "load_rules",
    "parse_rules",
    "split_marker",
    "split_rows",
]

SCHEMA = "tokenbill/allocation@1"
UNALLOCATED = "(unallocated)"
OPERATORS = ("eq", "in", "prefix", "regex")
TARGETS = ("team", "cost_center", "project", "workload_class")
#: Attribution fields a rule may match (``principal`` is deliberately absent: rules never target a
#: person).
MATCH_FIELDS = ("team", "cost_center", "project", "repo", "workspace_id", "api_key_id",
                "agent_product", "agent_type", "query_source", "skill", "mcp_server", "plugin",
                "workload_class", "entrypoint", "client_version", "billing_path", "cwd_key", "arm",
                "wave", "model", "channel", "provider")
#: Fields that accept the ``regex`` operator (identifiers, never free text).
REGEX_FIELDS = frozenset({"repo", "workspace_id", "api_key_id", "agent_product", "agent_type",
                          "entrypoint", "client_version", "cwd_key", "model", "channel",
                          "provider"} | {f"extra.{k}" for k in EXTRA_KEYS})
SPLIT_METHODS = ("proportional",)
SPLIT_BY = ("dev_days",)
_MAX_BYTES = 1 << 20
_MAX_REGEX = 200
_MAX_VALUE = 256
_MAX_RULES = 10_000
_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_SPLIT_RE = re.compile(r"\(split:([A-Za-z0-9._-]{1,64})\)\Z")
_WORKLOADS = frozenset(w.value for w in WorkloadClass)
_RULE_KEYS = frozenset({"id", "match", "set", "split", "description"})
_TOP_KEYS = frozenset({"rules", "schema", "description"})


@dataclass(frozen=True, slots=True)
class Condition:
    """One ``match`` condition: *field* *op* *values* (``in`` has several values)."""

    field: str
    op: str
    values: tuple[str, ...]
    pattern: re.Pattern[str] | None = None

    def test(self, value: str | None) -> bool:
        """True when *value* satisfies the condition (a missing value never does)."""
        if value is None:
            return False
        if self.op == "eq":
            return value == self.values[0]
        if self.op == "in":
            return value in self.values
        if self.op == "prefix":
            return value.startswith(self.values[0])
        assert self.pattern is not None
        return self.pattern.search(value) is not None


@dataclass(frozen=True, slots=True)
class Split:
    """A proportional split of the matched cost over *targets* (teams) by *by*."""

    method: str
    by: str
    targets: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Rule:
    rule_id: str
    conditions: tuple[Condition, ...]
    sets: tuple[tuple[str, str], ...]
    split: Split | None = None


@dataclass(frozen=True, slots=True)
class RuleSet:
    """An ordered rule set; ``sha256`` is the digest of its canonical JSON (provenance)."""

    rules: tuple[Rule, ...]
    sha256: str

    def rule(self, rule_id: str) -> Rule | None:
        """The rule named *rule_id*, or None."""
        for r in self.rules:
            if r.rule_id == rule_id:
                return r
        return None


@dataclass(frozen=True, slots=True)
class AllocatedCostRow(LedgerCostRow):
    """A ``LedgerCostRow`` produced by a rule split: FOCUS ``AllocatedMethodId`` and
    ``AllocatedMethodDetails`` (JSON) travel with it."""

    method_id: str = ""
    method_details: str = ""


# ---------------------------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------------------------


def _fail(where: str, why: str) -> UsageError:
    return UsageError(f"allocation rules: {where}: {why}")


def _string(value: object, where: str) -> str:
    if not isinstance(value, str) or not value or len(value) > _MAX_VALUE:
        raise _fail(where, f"must be a non-empty string of at most {_MAX_VALUE} characters")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise _fail(where, "control characters are not allowed")
    return value


def _check_regex(pattern: str, where: str) -> re.Pattern[str]:
    """Compile *pattern* after the safe-subset checks: ≤ 200 chars, no backreferences, no
    nested unbounded quantifiers (catastrophic backtracking)."""
    if len(pattern) > _MAX_REGEX:
        raise _fail(where, f"regex longer than {_MAX_REGEX} characters")
    if re.search(r"\\[1-9]|\\g<|\(\?P=|\(\?\(", pattern):
        raise _fail(where, "regex backreferences are not allowed")
    stack: list[bool] = []          # per open group: does it contain a quantifier?
    i, n = 0, len(pattern)
    last_group_quantified = False
    while i < n:
        ch = pattern[i]
        if ch == "\\":
            i += 2
            last_group_quantified = False
            continue
        if ch == "[":
            j = i + 1
            if j < n and pattern[j] == "^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 2 if pattern[j] == "\\" else 1
            i = j + 1
            last_group_quantified = False
            continue
        if ch == "(":
            stack.append(False)
            last_group_quantified = False
            i += 1
            if i < n and pattern[i] == "?":   # group extension syntax is not a quantifier
                i += 1
                if pattern.startswith("P<", i):
                    close = pattern.find(">", i)
                    i = n if close < 0 else close + 1
                else:
                    if i < n and pattern[i] == "<":
                        i += 1
                    while i < n and pattern[i] not in ":=!)":
                        i += 1
                    if i < n and pattern[i] in ":=!":
                        i += 1
            continue
        elif ch == ")":
            last_group_quantified = stack.pop() if stack else False
            if last_group_quantified and stack:
                stack[-1] = True     # an enclosing group contains the inner quantifier too
            i += 1
            if i < n and pattern[i] in "*+{" and last_group_quantified:
                raise _fail(where, "nested quantifiers are not allowed in a regex")
            continue
        elif ch in "*+?{":
            if stack:
                stack[-1] = True
            last_group_quantified = False
        i += 1
    try:
        return re.compile(pattern)
    except (re.error, RecursionError, OverflowError):
        raise _fail(where, "not a valid regular expression") from None


def _field(name: object, where: str) -> str:
    if not isinstance(name, str):
        raise _fail(where, "match fields must be strings")
    if name.startswith("extra."):
        if name[6:] not in EXTRA_KEYS:
            raise _fail(where, "unknown extra key")
        return name
    if name not in MATCH_FIELDS:
        raise _fail(where, "unknown match field")
    return name


def _condition(field: str, spec: object, where: str) -> Condition:
    if not isinstance(spec, Mapping) or len(spec) != 1:
        raise _fail(where, "a condition is one {operator: value} object")
    op, value = next(iter(spec.items()))
    if op not in OPERATORS:
        raise _fail(where, "unknown operator")
    if op == "in":
        if not isinstance(value, list) or not value or len(value) > 10_000:
            raise _fail(where, "'in' takes a non-empty list")
        values = tuple(_string(v, where) for v in value)
        return Condition(field, op, values)
    text = _string(value, where)
    if op == "regex":
        if field not in REGEX_FIELDS:
            raise _fail(where, "regex is allowed on id fields only")
        return Condition(field, op, (text,), _check_regex(text, where))
    return Condition(field, op, (text,))


def _rule(doc: object, index: int, seen: set[str]) -> Rule:
    where = f"rule {index + 1}"
    if not isinstance(doc, Mapping):
        raise _fail(where, "must be an object")
    unknown = set(doc) - _RULE_KEYS
    if unknown:
        raise _fail(where, "unknown keys")
    rid = doc.get("id", f"r{index + 1}")
    if not isinstance(rid, str) or not _ID_RE.match(rid):
        raise _fail(where, "id must match [A-Za-z0-9._-]{1,64}")
    if rid in seen:
        raise _fail(where, "duplicate rule id")
    seen.add(rid)
    match = doc.get("match", {})
    if not isinstance(match, Mapping):
        raise _fail(where, "match must be an object")
    conditions = tuple(_condition(_field(f, where), spec, f"{where} match")
                       for f, spec in sorted(match.items(), key=lambda kv: str(kv[0])))
    sets_doc = doc.get("set", {})
    if not isinstance(sets_doc, Mapping):
        raise _fail(where, "set must be an object")
    sets = []
    for target in sorted(sets_doc, key=str):
        if target not in TARGETS:
            raise _fail(where, "set targets are team, cost_center, project, workload_class")
        value = _string(sets_doc[target], f"{where} set")
        if target == "workload_class" and value not in _WORKLOADS:
            raise _fail(where, "workload_class must be a WorkloadClass value")
        if target == "team" and (value == UNALLOCATED or _SPLIT_RE.match(value)):
            raise _fail(where, "reserved team name")
        sets.append((target, value))
    split = None
    if "split" in doc:
        split = _split(doc["split"], where)
        if any(t == "team" for t, _v in sets):
            raise _fail(where, "a split rule cannot also set team")
    if not sets and split is None:
        raise _fail(where, "a rule needs set or split")
    return Rule(rule_id=rid, conditions=conditions, sets=tuple(sets), split=split)


def _split(doc: object, where: str) -> Split:
    if not isinstance(doc, Mapping) or set(doc) - {"method", "by", "targets"}:
        raise _fail(where, "split is {method, by, targets}")
    method = doc.get("method", "proportional")
    by = doc.get("by", "dev_days")
    if method not in SPLIT_METHODS:
        raise _fail(where, "split method must be proportional")
    if by not in SPLIT_BY:
        raise _fail(where, "split by must be dev_days")
    targets = doc.get("targets")
    if not isinstance(targets, list) or not targets or len(targets) > 1000:
        raise _fail(where, "split targets must be a non-empty list of teams")
    teams = tuple(_string(t, f"{where} split") for t in targets)
    if len(set(teams)) != len(teams):
        raise _fail(where, "split targets must be distinct")
    if any(t == UNALLOCATED or _SPLIT_RE.match(t) for t in teams):
        raise _fail(where, "reserved team name")
    return Split(method=method, by=by, targets=teams)


def parse_rules(doc: object) -> RuleSet:
    """Validate a decoded rules document and build the :class:`RuleSet` (``UsageError`` on any
    malformed part)."""
    if not isinstance(doc, Mapping):
        raise _fail("document", "must be a JSON object with a rules list")
    if set(doc) - _TOP_KEYS:
        raise _fail("document", "unknown top-level keys")
    if "schema" in doc and doc["schema"] != SCHEMA:
        raise _fail("document", f"schema must be {SCHEMA}")
    rules = doc.get("rules")
    if not isinstance(rules, list):
        raise _fail("document", "rules must be a list")
    if len(rules) > _MAX_RULES:
        raise _fail("document", "too many rules")
    seen: set[str] = set()
    parsed = tuple(_rule(r, i, seen) for i, r in enumerate(rules))
    try:
        canonical = json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, RecursionError):
        raise _fail("document", "not JSON-serializable") from None
    return RuleSet(rules=parsed, sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest())


def _no_constants(name: str) -> object:
    raise ValueError(name)


def load_rules(path: Path) -> RuleSet:
    """Read and validate the rules file at *path* (JSON, ≤ 1 MiB). ``UsageError`` when it is
    missing, too large, not UTF-8 JSON or malformed."""
    try:
        with open(path, "rb") as fh:
            data = fh.read(_MAX_BYTES + 1)
    except OSError:
        raise UsageError("allocation rules: file cannot be read") from None
    if len(data) > _MAX_BYTES:
        raise _fail("document", "file larger than 1 MiB")
    try:
        doc = json.loads(data.decode("utf-8"), parse_constant=_no_constants)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _fail("document", "not valid UTF-8 JSON") from None
    return parse_rules(doc)


# ---------------------------------------------------------------------------------------------
# applying
# ---------------------------------------------------------------------------------------------


def _value(req: Request, field: str) -> str | None:
    attr = req.attribution
    if field.startswith("extra."):
        return dict(attr.extra).get(field[6:])
    if field == "model":
        return req.model or None
    if field in ("channel", "provider"):
        inf = req.serving_inference
        return None if inf is None else getattr(inf.pricing, field)
    value = getattr(attr, field)
    return None if value is None else str(value)


def matches(rule: Rule, req: Request) -> bool:
    """True when every condition of *rule* holds for *req*."""
    return all(c.test(_value(req, c.field)) for c in rule.conditions)


def split_marker(rule_id: str) -> str:
    """The team value of requests owned by *rule_id*'s split (``"(split:<rule id>)"``)."""
    return f"(split:{rule_id})"


def apply_rules(req: Request, rules: RuleSet) -> Attribution:
    """The attribution of *req* after *rules* (first match wins per target field; a split rule
    owns the team as :func:`split_marker`; no team at all → ``"(unallocated)"``)."""
    if not isinstance(rules, RuleSet):
        raise UsageError("apply_rules expects a RuleSet (finops.allocation.load_rules)")
    chosen: dict[str, str] = {}
    for rule in rules.rules:
        if not matches(rule, req):
            continue
        for target, value in rule.sets:
            chosen.setdefault(target, value)
        if rule.split is not None:
            chosen.setdefault("team", split_marker(rule.rule_id))
        if len(chosen) == len(TARGETS):
            break
    attr = req.attribution
    changes: dict[str, object] = {}
    for target in ("team", "cost_center", "project"):
        if target in chosen:
            changes[target] = chosen[target]
    if "workload_class" in chosen:
        changes["workload_class"] = WorkloadClass(chosen["workload_class"])
    team = changes.get("team", attr.team)
    if not team:
        changes["team"] = UNALLOCATED
    return dataclasses.replace(attr, **changes) if changes else attr  # type: ignore[arg-type]


def _apportion(total: int, weights: Sequence[int]) -> list[int]:
    """Split *total* over *weights* (largest remainder, ties by position): the parts sum to
    *total* exactly."""
    den = sum(weights)
    parts = [total * w // den for w in weights]
    rest = total - sum(parts)
    order = sorted(range(len(weights)), key=lambda i: (-((total * weights[i]) % den), i))
    for i in order[:rest]:
        parts[i] += 1
    return parts


def split_rows(rows: Iterable[LedgerCostRow], rules: RuleSet, *,
               weights: Mapping[str, int]) -> list[LedgerCostRow]:
    """Apportion every row whose team is a split marker over its rule's targets in proportion to
    *weights* (active developer-days per team; all zero → equal parts). Amounts and quantities are
    split by largest remainder, so the parts of a row sum to it exactly; the split rows are
    :class:`AllocatedCostRow` s carrying the method. Other rows pass through unchanged (so does a
    marker whose rule is not in *rules*)."""
    out: list[LedgerCostRow] = []
    for row in rows:
        m = _SPLIT_RE.match(row.team or "")
        rule = rules.rule(m.group(1)) if m else None
        if rule is None or rule.split is None:
            out.append(row)
            continue
        targets = rule.split.targets
        raw = [weights.get(t, 0) for t in targets]
        if any(type(w) is not int for w in raw):
            raise UsageError("split weights must be int developer-days")
        w = [max(v, 0) for v in raw]
        basis = "dev_days" if sum(w) else "equal (no developer-days)"
        if not sum(w):
            w = [1] * len(targets)
        details = json.dumps({"rule": rule.rule_id, "method": rule.split.method,
                              "by": rule.split.by, "weights_basis": basis,
                              "weights": dict(zip(targets, w, strict=True)),
                              "rule_set_sha256": rules.sha256},
                             sort_keys=True, separators=(",", ":"))
        method_id = f"tokenbill.split.{rule.split.method}.{rule.split.by}"
        amounts = {name: _apportion(getattr(row, name), w)
                   for name in ("quantity", "priced_nano", "estimated_low_nano",
                                "estimated_high_nano")}
        fields = {f.name: getattr(row, f.name) for f in dataclasses.fields(LedgerCostRow)}
        for i, team in enumerate(targets):
            fields.update({name: parts[i] for name, parts in amounts.items()}, team=team)
            out.append(AllocatedCostRow(**fields, method_id=method_id, method_details=details))
    return out


def is_allocated(team: str | None) -> bool:
    """True for a team that counts as allocated (not empty and not ``"(unallocated)"``)."""
    return bool(team) and team != UNALLOCATED


def coverage(rows: Iterable[LedgerCostRow]) -> str:
    """Allocation coverage: allocated dollars ÷ all dollars of the billed-basis rows (exact
    ``priced_nano``; list-equivalent rows are not billed and do not count), as a decimal string;
    ``"1"`` when there is nothing to allocate. ≥ 0.95 is the chargeback gate."""
    total = allocated = 0
    for row in rows:
        if row.basis is Basis.LIST_EQUIVALENT:
            continue
        total += row.priced_nano
        if is_allocated(row.team):
            allocated += row.priced_nano
    value = ratio(allocated, total)
    if value is None:
        return "1"
    text = format(value.normalize(), "f")
    return text
