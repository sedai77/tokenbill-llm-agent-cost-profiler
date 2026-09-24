"""Cloud billing exports: AWS CUR 2.0 and the GCP billing export (SPEC §5.13, D31; package ADMIN).

The invoice side of the Bedrock and Vertex channels. Log-derived cloud costs "do not reflect
discounts, commitments or provisioned throughput"; these exports do.

* :class:`AwsCurAdapter` (``aws-cur``) — CUR 2.0 **CSV or CSV.gz** (a file, or a directory of export
  parts). Parquet is not readable with the standard library: it raises :class:`SourceError` asking
  for the CSV export. Only Bedrock rows (``line_item_product_code`` ∈ :data:`CUR_PRODUCT_CODES`, or
  ``anthropic`` in ``line_item_usage_type``) are read; tax rows are skipped. Per row:
  ``CostLine(source_kind="aws.cur2", channel="bedrock", date_utc=line_item_usage_start_date[:10],
  workspace_id=h_(line_item_usage_account_id), sku=line_item_usage_type,
  description=line_item_line_item_description (provider text; else the usage type), amount=
  line_item_net_unblended_cost or line_item_unblended_cost, list_amount=line_item_unblended_cost,
  principal=p_(line_item_iam_principal), cost_type=None for usage line items else the line item
  type (Credit, Discount, Refund, …))`` summed per (date, account, usage type, principal, line
  type); usage rows also give a per-day token ``UsageAggregate`` from ``line_item_usage_amount``
  × the ``pricing_unit`` (``1K tokens`` / ``1M tokens``; see :func:`token_unit`) with dims
  ``channel``, ``workspace_id``, the principal's ``team`` (``opts.team_map`` on the raw ARN or its
  role, else the ``iamPrincipal/team`` tag, else ``(unmapped)``) and the mapped
  model/scope/tier.
* :class:`GcpBillingExportAdapter` (``gcp-billing``) — rows of the BigQuery billing export as JSONL
  (nested) or CSV (``sku.id`` or ``sku_id`` style headers; ``credits`` and ``labels`` as JSON
  text). Claude SKUs only (``sku.description`` contains "Claude", or a verified SKU rule matches);
  tax rows skipped. ``CostLine(source_kind="gcp.billing_export", channel="vertex",
  workspace_id=h_(project.id), amount = cost + Σ credits[].amount, list_amount = cost,
  endpoint_scope from location.region: "global" → global, else regional)`` and per-day token
  aggregates from ``usage.amount``; allowlisted labels (:data:`GCP_LABEL_DIMS`) become dims, others
  are dropped (``dq.unknown_fields``).

Usage types and SKUs map to (model, bucket, scope, tier) **only** through
:func:`tokenbill.core.catalog.map_sku` (verified rules). An unmapped row keeps its ``sku``, leaves
``model``/``token_type`` empty and its tokens go to an aggregate with dim ``sku`` in bucket
``uncached_input`` (channel-total coverage only), counted in ``dq.unmapped_sku``; nothing is
guessed. This module is a money module: no float anywhere (SPEC §2.4, §8.9).
"""

from __future__ import annotations

import csv
import io
import re
import zlib
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from tokenbill.adapters.anthropic_admin import (
    DAY_MS,
    MONEY_SCALE,
    BadRecord,
    ReadContext,
    date_of,
    day_start,
    description,
    label,
    load_documents,
    loads,
    model_id,
    money_scaled,
    source_files,
    ts_ms,
)
from tokenbill.core import catalog
from tokenbill.core.errors import SourceError
from tokenbill.core.jsonl import open_text
from tokenbill.core.records import MAX_TOKENS, UsageBuckets
from tokenbill.core.types import IngestOptions, IngestResult

__all__ = [
    "CUR_PRODUCT_CODES",
    "GCP_LABEL_DIMS",
    "PARQUET_MESSAGE",
    "AwsCurAdapter",
    "GcpBillingExportAdapter",
    "token_unit",
]

#: CUR ``line_item_product_code`` values of Bedrock model inference (SPEC §5.13).
CUR_PRODUCT_CODES = frozenset({"AmazonBedrock", "AmazonBedrockFoundationModels"})
PARQUET_MESSAGE = ("Parquet CUR is not readable with the standard library; export CUR 2.0 as CSV "
                   "or CSV.gz (docs/FLEET.md documents the CSV export and an Athena query)")
#: Legacy CUR (``lineItem/…``) column names → CUR 2.0 names.
CUR_ALIASES: Mapping[str, str] = {
    "lineItem/UsageStartDate": "line_item_usage_start_date",
    "lineItem/UsageEndDate": "line_item_usage_end_date",
    "lineItem/ProductCode": "line_item_product_code",
    "lineItem/UsageType": "line_item_usage_type",
    "lineItem/UsageAmount": "line_item_usage_amount",
    "lineItem/UnblendedCost": "line_item_unblended_cost",
    "lineItem/NetUnblendedCost": "line_item_net_unblended_cost",
    "lineItem/UsageAccountId": "line_item_usage_account_id",
    "lineItem/LineItemType": "line_item_line_item_type",
    "lineItem/CurrencyCode": "line_item_currency_code",
    "lineItem/IamPrincipal": "line_item_iam_principal",
    "pricing/unit": "pricing_unit",
}
#: CUR line item types whose usage amount is billed token usage.
CUR_TOKEN_LINE_TYPES = frozenset({"", "Usage", "DiscountedUsage", "SavingsPlanCoveredUsage"})
#: Line item types that are not model cost (skipped, counted in stats).
CUR_SKIPPED_LINE_TYPES = frozenset({"Tax"})
#: CUR 2.0 ``tags`` keys that name the principal's team (IAM principal cost-allocation tags).
CUR_TEAM_TAGS = ("iamPrincipal/team", "iamPrincipal/Team")
#: GCP label keys → aggregate dims (all other labels are dropped).
GCP_LABEL_DIMS: Mapping[str, str] = {
    "team": "team", "cost_center": "cost_center", "cost-center": "cost_center",
    "department": "department", "environment": "environment",
}
GCP_SKIPPED_COST_TYPES = frozenset({"tax"})
GCP_TOKEN_COST_TYPES = frozenset({"", "regular"})
#: UsageBuckets fields a SKU rule may name.
_RULE_BUCKETS = frozenset({"uncached_input", "cache_read", "cache_write_5m", "cache_write_1h",
                           "cache_write_unknown", "output"})
_UNIT_RE = re.compile(
    r"(?:(?P<n>\d{1,9}(?:,\d{3})*)\s*)?(?P<mult>k|m|thousand|million)?\s*"
    r"(?:input\s+|output\s+)?tokens?", re.IGNORECASE)
_UNIT_MULT: Mapping[str, int] = {"k": 1000, "thousand": 1000, "m": 1_000_000,
                                 "million": 1_000_000}
_LABEL_VALUE_RE = re.compile(r"[A-Za-z0-9_.:/ -]{1,63}")


# ---------------------------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------------------------


def token_unit(text: Any) -> int | None:
    """Tokens per usage unit named by a pricing/usage unit string: ``tokens`` → 1, ``1K tokens``
    / ``Thousand Tokens`` → 1,000, ``1M tokens`` / ``Million tokens`` → 1,000,000, ``1000
    tokens`` → 1,000; anything else (``Units``, ``count``) → None (the conversion is unknown, so
    no tokens are derived)."""
    if not isinstance(text, str) or len(text) > 64:
        return None
    m = _UNIT_RE.fullmatch(text.strip())
    if m is None:
        return None
    n = int(m.group("n").replace(",", "")) if m.group("n") else 1
    mult = _UNIT_MULT[m.group("mult").lower()] if m.group("mult") else 1
    unit = n * mult
    return unit if unit > 0 else None


def usage_tokens(amount: Any, unit: int) -> tuple[int, bool]:
    """``(tokens, rounded)``: usage amount × tokens-per-unit, exact; a non-integral product is
    rounded half-even (``rounded`` True). Negative or out-of-range → ``BadRecord("bad_usage")``."""
    scaled = money_scaled(amount, "usage_amount", cents=False)  # exact 10**-210 fixed point
    if scaled < 0:
        raise BadRecord("bad_usage")
    total = scaled * unit
    one = 10**MONEY_SCALE
    q, r = divmod(total, one)
    if 2 * r > one or (2 * r == one and q % 2 == 1):
        q += 1
    if q > MAX_TOKENS:
        raise BadRecord("bad_usage")
    return q, r != 0


def _usage_of(bucket: str, n: int) -> UsageBuckets:
    return UsageBuckets(**{bucket: n})


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _sniff_text(head: bytes) -> str:
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    return head.decode("utf-8", "ignore")


# ---------------------------------------------------------------------------------------------
# CSV reading
# ---------------------------------------------------------------------------------------------


def _is_parquet(path: Path) -> bool:
    if path.suffix.lower() == ".parquet":
        return True
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"PAR1"
    except OSError:
        return False


def iter_csv(ctx: ReadContext, path: Path, file_index: int, aliases: Mapping[str, str]
             ) -> Iterator[tuple[str, dict[str, str]]]:
    """``(locator, row)`` for every data row of a CSV / CSV.gz file (header names normalized
    through *aliases*); rows the csv module rejects are quarantined."""
    raw = open_text(path)
    try:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
        reader = csv.reader(text)
        header: list[str] | None = None
        last_error_line = -1
        while True:
            try:
                row = next(reader)
            except StopIteration:
                return
            except csv.Error:
                loc = ctx.file_locator(file_index, f"line:{reader.line_num}")
                if reader.line_num == last_error_line:
                    return
                last_error_line = reader.line_num
                ctx.quarantine(loc, "bad_csv")
                continue
            except (OSError, EOFError, ValueError, zlib.error) as exc:
                raise SourceError(f"{path.name}: corrupt stream ({type(exc).__name__})") from None
            if header is None:
                header = [aliases.get(h.strip(), h.strip()) for h in row]
                continue
            if not any(cell.strip() for cell in row):
                continue
            loc = ctx.file_locator(file_index, f"line:{reader.line_num}")
            if len(row) != len(header):
                ctx.quarantine(loc, "bad_csv")
                continue
            yield loc, dict(zip(header, row, strict=True))
    finally:
        raw.close()


# ---------------------------------------------------------------------------------------------
# AWS CUR 2.0
# ---------------------------------------------------------------------------------------------


def _role_arn(arn: str) -> str | None:
    """``arn:aws:sts::<acct>:assumed-role/<role>/<session>`` → ``arn:aws:iam::<acct>:role/<role>``
    (the team map may be keyed by roles; session names often carry a person)."""
    m = re.fullmatch(r"arn:(aws[a-z-]*):sts::(\d{12}):assumed-role/([^/]+)/.+", arn)
    if m is None:
        return None
    return f"arn:{m.group(1)}:iam::{m.group(2)}:role/{m.group(3)}"


def _tag_team(tags: str) -> str | None:
    if not tags.strip():
        return None
    try:
        value = loads(tags)
    except BadRecord:
        return None
    if not isinstance(value, dict):
        return None
    for key in CUR_TEAM_TAGS:
        team = value.get(key)
        if isinstance(team, str) and _LABEL_VALUE_RE.fullmatch(team.strip()):
            return team.strip()
    return None


class AwsCurAdapter:
    """``aws-cur``: AWS CUR 2.0 CSV / CSV.gz → Bedrock cost lines and token aggregates."""

    name = "aws-cur"
    capabilities = frozenset({"aggregates", "cost", "attribution.team"})
    source_kind = "aws.cur2"
    channel = "bedrock"

    def sniff(self, path: Path, head: bytes) -> bool:
        """A CSV header with CUR 2.0 (or legacy CUR) line-item columns, or a Parquet file whose name
        or head suggests CUR (read then explains the CSV export)."""
        try:
            if head[:4] == b"PAR1" or Path(path).suffix.lower() == ".parquet":
                return "cur" in Path(path).name.lower() or b"line_item_" in head
            first = _sniff_text(head).split("\n", 1)[0]
            has_type = "line_item_usage_type" in first or "lineItem/UsageType" in first
            has_code = "line_item_product_code" in first or "lineItem/ProductCode" in first
            return has_type and has_code
        except Exception:  # noqa: BLE001 - a sniffer never raises
            return False

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Parse the export (a file or a directory of parts) per SPEC §5.13."""
        ctx = ReadContext(self.name, Path(path), opts)
        for f in source_files(Path(path)):
            if _is_parquet(f):
                raise SourceError(f"{f.name}: {PARQUET_MESSAGE}")
        unmapped = [0, 0]
        for i, f in enumerate(ctx.files):
            for loc, row in iter_csv(ctx, f, i, CUR_ALIASES):
                try:
                    self._row(ctx, row, unmapped)
                except BadRecord as exc:
                    ctx.quarantine(loc, exc.reason)
        if unmapped[0]:
            ctx.note("dq.unmapped_sku", "info", "Bedrock usage types without a verified SKU rule: "
                     "tokens kept as uncached_input with dim sku (channel totals only)",
                     unmapped[0], unmapped[1])
        return ctx.result(self.capabilities)

    def _row(self, ctx: ReadContext, row: Mapping[str, str], unmapped: list[int]) -> None:
        code = _text(row.get("line_item_product_code"))
        usage_type = _text(row.get("line_item_usage_type"))
        if code not in CUR_PRODUCT_CODES and "anthropic" not in usage_type.lower():
            ctx.stat("rows_skipped_not_bedrock")
            return
        line_type = _text(row.get("line_item_line_item_type"))
        if line_type in CUR_SKIPPED_LINE_TYPES:
            ctx.stat("rows_skipped_tax")
            return
        ctx.stat("records")
        if not usage_type:
            raise BadRecord("missing:line_item_usage_type")
        start = ts_ms(_text(row.get("line_item_usage_start_date")) or None,
                      "line_item_usage_start_date")
        day = date_of(start)
        account = ctx.name(_text(row.get("line_item_usage_account_id")) or None,
                           "line_item_usage_account_id")
        if account is None:
            raise BadRecord("missing:line_item_usage_account_id")
        cur = _text(row.get("line_item_currency_code"))
        if cur and cur.upper() != "USD":
            raise BadRecord("bad_type:currency")
        net = _text(row.get("line_item_net_unblended_cost"))
        unblended = _text(row.get("line_item_unblended_cost"))
        if not net and not unblended:
            raise BadRecord("missing:line_item_unblended_cost")
        amount = money_scaled(net or unblended, "line_item_net_unblended_cost", cents=False)
        listed = money_scaled(unblended, "line_item_unblended_cost", cents=False) if unblended \
            else None
        sku = label(usage_type, "line_item_usage_type", max_len=256)
        desc = description(row.get("line_item_line_item_description"))  # provider text
        rule = catalog.map_sku(self.source_kind, usage_type)
        if rule is not None and rule.bucket not in _RULE_BUCKETS:
            rule = None
        raw_principal = _text(row.get("line_item_iam_principal"))
        principal = ctx.principal(raw_principal) if raw_principal else None
        team = None
        if raw_principal:
            team = ctx.team_of(raw_principal, _role_arn(raw_principal) or "")
            if team == "(unmapped)":
                team = _tag_team(_text(row.get("tags"))) or team
        tokens = None
        amount_text = _text(row.get("line_item_usage_amount"))
        if line_type in CUR_TOKEN_LINE_TYPES and amount_text:
            unit = token_unit(row.get("pricing_unit"))
            if unit is None and rule is not None:
                unit = rule.unit_tokens
            if unit is None:
                ctx.stat("rows_unit_unknown")
                ctx.note("dq.unmapped_sku", "info", "Bedrock rows whose pricing_unit is not a "
                         "token unit and no verified rule: cost kept, tokens not derived")
            else:
                if rule is not None and token_unit(row.get("pricing_unit")) not in (None,
                                                                                    rule.unit_tokens):
                    ctx.stat("unit_rule_conflicts")
                tokens, rounded = usage_tokens(amount_text, unit)
                if rounded:
                    ctx.stat("token_amounts_rounded")
        model = model_id(rule.model, "bedrock") if rule and rule.model else None
        if tokens is not None:  # validated side effects last: a bad row adds nothing
            dims: dict[str, str | None] = {"channel": self.channel, "workspace_id": account,
                                           "team": team}
            if rule is None:
                dims["sku"] = sku
                bucket = "uncached_input"
            else:
                dims.update(model=model, endpoint_scope=rule.endpoint_scope,
                            service_tier=rule.service_tier)
                bucket = rule.bucket
            start_day = day_start(start)
            if ctx.add_aggregate(self.source_kind, start_day, start_day + DAY_MS, dims,
                                 _usage_of(bucket, tokens)) and rule is None:
                unmapped[0] += 1
                unmapped[1] += tokens
        ctx.add_cost(source_kind=self.source_kind, date_utc=day, channel=self.channel,
                     amount=amount, listed=listed, workspace_id=account,
                     description=desc or sku or "", model=model,
                     cost_type=None if line_type in CUR_TOKEN_LINE_TYPES else line_type,
                     token_type=rule.bucket if rule else None, sku=sku,
                     service_tier=rule.service_tier if rule else None,
                     endpoint_scope=rule.endpoint_scope if rule else None, principal=principal)


# ---------------------------------------------------------------------------------------------
# GCP billing export
# ---------------------------------------------------------------------------------------------


def gcp_get(row: Mapping[str, Any], dotted: str) -> Any:
    """A billing-export field from a nested JSONL row or a flattened CSV row (``sku.id`` or
    ``sku_id`` headers)."""
    if dotted in row:
        return row[dotted]
    flat = dotted.replace(".", "_")
    if flat in row:
        return row[flat]
    obj: Any = row
    for part in dotted.split("."):
        if not isinstance(obj, Mapping):
            return None
        obj = obj.get(part)
    return obj


def _json_field(value: Any) -> Any:
    """CSV cells holding JSON (``credits``, ``labels``) are parsed; other values pass through."""
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text[0] in "[{":
            return loads(text)
    return value


def _credits(row: Mapping[str, Any]) -> list[Any]:
    value = _json_field(gcp_get(row, "credits"))
    if value is None:
        flat = gcp_get(row, "credits.amount")
        return [] if flat in (None, "") else [flat]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        raise BadRecord("bad_type:credits")
    amounts = []
    for credit in value:
        if not isinstance(credit, dict) or credit.get("amount") is None:
            raise BadRecord("bad_type:credits")
        amounts.append(credit["amount"])
    return amounts


def _labels(row: Mapping[str, Any]) -> dict[str, str]:
    value = _json_field(gcp_get(row, "labels"))
    pairs: list[tuple[Any, Any]] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                raise BadRecord("bad_type:labels")
            pairs.append((item.get("key"), item.get("value")))
    elif isinstance(value, dict):
        pairs.extend(value.items())
    elif value is not None:
        raise BadRecord("bad_type:labels")
    for key, cell in row.items():  # flattened "labels.team" columns
        if isinstance(key, str) and key.startswith("labels.") and key != "labels.key":
            pairs.append((key[len("labels."):], cell))
    return {k: v for k, v in pairs if isinstance(k, str) and isinstance(v, str)}


class GcpBillingExportAdapter:
    """``gcp-billing``: GCP billing export rows (JSONL or CSV) → Vertex cost lines and token
    aggregates."""

    name = "gcp-billing"
    capabilities = frozenset({"aggregates", "cost", "attribution.team"})
    source_kind = "gcp.billing_export"
    channel = "vertex"

    def sniff(self, path: Path, head: bytes) -> bool:
        """A CSV header or JSONL row with the billing-export ``sku`` and ``usage_start_time``
        fields."""
        try:
            text = _sniff_text(head)
            first = text.split("\n", 1)[0]
            if first.lstrip().startswith("{"):
                return '"sku"' in first and '"usage_start_time"' in first and (
                    '"cost"' in first)
            names = set(n.strip().strip('"') for n in first.split(","))
            return bool({"sku.id", "sku_id"} & names) and "usage_start_time" in names and (
                "cost" in names)
        except Exception:  # noqa: BLE001 - a sniffer never raises
            return False

    def read(self, path: Path, opts: IngestOptions) -> IngestResult:
        """Parse the export (a file or a directory) per SPEC §5.13."""
        ctx = ReadContext(self.name, Path(path), opts)
        state: dict[str, Any] = {"unmapped": [0, 0], "labels_dropped": 0, "scope_unknown": 0}
        for i, f in enumerate(ctx.files):
            for loc, row in self._rows(ctx, f, i):
                if row is None:
                    continue
                try:
                    self._row(ctx, row, state)
                except BadRecord as exc:
                    ctx.quarantine(loc, exc.reason)
        unmapped = state["unmapped"]
        if unmapped[0]:
            ctx.note("dq.unmapped_sku", "info", "Vertex SKUs without a verified SKU rule: tokens "
                     "kept as uncached_input with dim sku (channel totals only)",
                     unmapped[0], unmapped[1])
        if state["labels_dropped"]:
            ctx.note("dq.unknown_fields", "info", "billing-export labels outside the allowlist "
                     "dropped", state["labels_dropped"])
        if state["scope_unknown"]:
            ctx.note("dq.scope_unknown", "info", "billing-export rows without a location: "
                     "endpoint scope unknown", state["scope_unknown"])
        return ctx.result(self.capabilities)

    def _rows(self, ctx: ReadContext, path: Path, file_index: int
              ) -> Iterator[tuple[str, Mapping[str, Any] | None]]:
        text = _sniff_text(_head(path))
        if text.lstrip().startswith(("{", "[")):
            for doc in load_documents(path, file_index):
                loc = ctx.file_locator(file_index, doc.locator)
                if doc.value is None:
                    ctx.quarantine(loc, doc.reason or "bad_json")
                    yield loc, None
                    continue
                yield loc, doc.value
            return
        yield from iter_csv(ctx, path, file_index, {})

    def _row(self, ctx: ReadContext, row: Mapping[str, Any], state: dict[str, Any]) -> None:
        sku_id = label(gcp_get(row, "sku.id"), "sku.id", max_len=256)
        sku_desc = description(gcp_get(row, "sku.description"))
        rule = catalog.map_sku(self.source_kind, sku_id) if sku_id else None
        if rule is not None and rule.bucket not in _RULE_BUCKETS:
            rule = None
        if "claude" not in sku_desc.lower() and rule is None:
            ctx.stat("rows_skipped_not_claude")
            return
        cost_type = label(gcp_get(row, "cost_type"), "cost_type") or ""
        if cost_type.lower() in GCP_SKIPPED_COST_TYPES:
            ctx.stat("rows_skipped_tax")
            return
        ctx.stat("records")
        start = ts_ms(_str_or_none(gcp_get(row, "usage_start_time")), "usage_start_time")
        day = date_of(start)
        project = ctx.name(_str_or_none(gcp_get(row, "project.id")), "project.id")
        cur = gcp_get(row, "currency")
        if cur not in (None, "") and (not isinstance(cur, str) or cur.strip().upper() != "USD"):
            raise BadRecord("bad_type:currency")
        cost_value = gcp_get(row, "cost")
        if cost_value in (None, ""):
            raise BadRecord("missing:cost")
        cost = money_scaled(cost_value, "cost", cents=False)
        credits = sum(money_scaled(c, "credits.amount", cents=False) for c in _credits(row))
        region = _text(gcp_get(row, "location.region")) or _text(gcp_get(row, "location.location"))
        scope: str | None
        if not region:
            scope = rule.endpoint_scope if rule else None
        else:
            scope = "global" if region.lower() == "global" else "regional"
        labels = _labels(row)
        dims: dict[str, str | None] = {"channel": self.channel, "workspace_id": project,
                                       "endpoint_scope": scope}
        dropped = 0
        for key in sorted(labels):
            dim = GCP_LABEL_DIMS.get(key)
            value = labels[key].strip()
            if dim is None or not _LABEL_VALUE_RE.fullmatch(value):
                dropped += 1
                continue
            dims[dim] = value
        model = model_id(rule.model, "vertex") if rule and rule.model else None
        tokens = self._tokens(ctx, row, rule) if cost_type.lower() in GCP_TOKEN_COST_TYPES \
            else None
        # validated: record the row (a malformed row above adds nothing)
        state["labels_dropped"] += dropped
        if scope is None:
            state["scope_unknown"] += 1
        if tokens is not None:
            if rule is None:
                dims["sku"] = sku_id
                bucket = "uncached_input"
            else:
                dims.update(model=model, service_tier=rule.service_tier)
                bucket = rule.bucket
            start_day = day_start(start)
            if ctx.add_aggregate(self.source_kind, start_day, start_day + DAY_MS, dims,
                                 _usage_of(bucket, tokens)) and rule is None:
                state["unmapped"][0] += 1
                state["unmapped"][1] += tokens
        ctx.add_cost(source_kind=self.source_kind, date_utc=day, channel=self.channel,
                     amount=cost + credits, listed=cost, workspace_id=project,
                     description=sku_desc, model=model, cost_type=cost_type or None,
                     token_type=rule.bucket if rule else None, sku=sku_id,
                     service_tier=rule.service_tier if rule else None, endpoint_scope=scope)

    @staticmethod
    def _tokens(ctx: ReadContext, row: Mapping[str, Any], rule: catalog.SkuRule | None
                ) -> int | None:
        """Tokens of a usage row: ``usage.amount`` × the tokens per ``usage.unit`` (or the
        amount in pricing units × the pricing unit, or a verified rule's unit); None when the row
        has no amount or no known token unit."""
        amount = gcp_get(row, "usage.amount")
        if amount in (None, ""):
            return None
        unit = token_unit(gcp_get(row, "usage.unit"))
        if unit is None:
            pricing_unit = token_unit(gcp_get(row, "usage.pricing_unit"))
            in_pricing = gcp_get(row, "usage.amount_in_pricing_units")
            if pricing_unit is not None and in_pricing not in (None, ""):
                amount, unit = in_pricing, pricing_unit
        if unit is None and rule is not None:
            unit = rule.unit_tokens
        if unit is None:
            ctx.stat("rows_unit_unknown")
            return None
        tokens, rounded = usage_tokens(amount, unit)
        if rounded:
            ctx.stat("token_amounts_rounded")
        return tokens


def _str_or_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _head(path: Path, n: int = 4096) -> bytes:
    try:
        with open_text(path) as f:
            return f.read(n)
    except SourceError:
        raise
    except Exception as exc:  # noqa: BLE001 - corrupt compressed stream
        raise SourceError(f"{path.name}: unreadable ({type(exc).__name__})") from None

