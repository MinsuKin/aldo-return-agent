"""DynamoDB repository: the ONLY module in the package that imports the DynamoDB client.

Everything else (services, tests, scripts) goes through the helpers below. They give the
rest of the code four guarantees:

* Key formats live here (design section 3). No other module builds a ``pk`` or ``sk``.
* Numbers: ``to_ddb`` turns floats into ``Decimal`` (NaN and Infinity are refused),
  ``from_ddb`` turns every number read back into ``int`` when integral, else ``float``,
  and ``json_safe`` is the single choke point for tool results, trace payloads and API bodies
  (it never returns a ``Decimal``). Sizes are STRINGS with one decimal (``size_str``). G-03.
* Reserved words: write every attribute name in an expression as ``#name`` (the helpers build
  ``ExpressionAttributeNames`` for you) and every value as ``:name`` supplied in ``values``.
  ``set``/``add``/``remove`` take plain attribute names. DynamoDB conditions cannot do
  arithmetic, so pass precomputed values (for example ``:max = purchased_qty - qty``). G-27.
* Failure shapes: a failed condition raises ``ConditionFailed``; a cancelled transaction
  raises ``TransactionCanceled`` (per-operation reasons and labels); throttling and network
  trouble raise a retryable ``UPSTREAM_TIMEOUT`` ``DomainError``. G-04.

``update_item`` and ``TxUpdate`` add ``attribute_exists(#pk)`` unless ``must_exist=False``:
an UpdateItem on a missing item would otherwise create a stray partial item.
"""

from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import json
import math
import random
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import PurePath
from typing import Any, Iterable, Iterator, Mapping, Sequence

import boto3
from boto3.dynamodb.types import Binary, TypeDeserializer, TypeSerializer
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from . import clock, config
from .errors import DomainError, ErrorCode

Key = tuple[str, str]

# --------------------------------------------------------------------------- key builders


def _part(value: object, label: str = "id") -> str:
    if not isinstance(value, str) or not value or len(value) > 256:
        raise DomainError(ErrorCode.VALIDATION_ERROR, f"{label} must be a non-empty string")
    return value


def customer_key(cid: str) -> Key:
    return (f"CUST#{_part(cid, 'customer_id')}", "PROFILE")


def order_key(oid: str) -> Key:
    return (f"ORDER#{_part(oid, 'order_id')}", "META")


def line_key(oid: str, lid: str) -> Key:
    return (f"ORDER#{_part(oid, 'order_id')}", f"LINE#{_part(lid, 'line_id')}")


def product_key(sku: str) -> Key:
    return (f"PRODUCT#{_part(sku, 'sku')}", "META")


def location_key(loc: str) -> Key:
    return (f"LOC#{_part(loc, 'location_id')}", "META")


def inventory_key(sku: str, loc: str) -> Key:
    return (f"INV#{_part(sku, 'sku')}", f"LOC#{_part(loc, 'location_id')}")


def pchk_key(id: str) -> Key:  # noqa: A002 - the contract names the parameter ``id``
    return (f"PCHK#{_part(id, 'policy_check_id')}", "META")


def cand_key(id: str) -> Key:  # noqa: A002
    return (f"CAND#{_part(id, 'candidate_set_id')}", "META")


def quote_key(qid: str) -> Key:
    return (f"QUOTE#{_part(qid, 'quote_id')}", "META")


def approval_key(qid: str) -> Key:
    return (f"QUOTE#{_part(qid, 'quote_id')}", "APPROVAL")


def txn_key(tid: str) -> Key:
    return (f"TXN#{_part(tid, 'transaction_id')}", "META")


def idem_key(approval_id: str) -> Key:
    return (f"IDEM#{_part(approval_id, 'approval_id')}", "META")


def case_key(case_id: str) -> Key:
    return (f"CASE#{_part(case_id, 'case_id')}", "META")


def conv_key(conv_id: str) -> Key:
    return (f"CONV#{_part(conv_id, 'conv_id')}", "META")


def run_key(run_id: str) -> Key:
    return (f"RUN#{_part(run_id, 'run_id')}", "META")


def event_key(run_id: str, seq: int) -> Key:
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise DomainError(ErrorCode.VALIDATION_ERROR, "seq must be a non-negative integer")
    return (f"RUN#{_part(run_id, 'run_id')}", f"EVT#{seq:06d}")


def audit_key(day: str, ts: str, uid: str) -> Key:
    return (f"AUDIT#{_part(day, 'day')}", f"{_part(ts, 'ts')}#{_part(uid, 'uid')}")


# Fit Gateway pivot (PIVOT_ADDENDUM 5.2, 5.4, C8; contracts.md F.5, F.7)
def sizereq_key(size_request_id: str) -> Key:
    return (f"SIZEREQ#{_part(size_request_id, 'size_request_id')}", "META")


def outbox_key(update_id: str) -> Key:
    """Write-back outbox item of one memory update (ALDO -> the owner's personal agent)."""
    return (f"A2AOUT#{_part(update_id, 'update_id')}", "META")


def gsi1_order(cid: str, ordered_at: str, oid: str) -> tuple[str, str]:
    return (f"CUST#{_part(cid, 'customer_id')}", f"ORDER#{_part(ordered_at, 'ordered_at')}#{_part(oid, 'order_id')}")


def gsi1_product(style: str, color: str, size: str | int | float | Decimal) -> tuple[str, str]:
    return (
        f"STYLE#{_part(style, 'style')}#{_part(color, 'color')}",
        f"SIZE#{float(size_str(size)):05.1f}",
    )


def gsi1_case(created_at: str) -> tuple[str, str]:
    return ("CASES", _part(created_at, "created_at"))


def gsi1_outbox_pending(created_at: str) -> tuple[str, str]:
    """GSI1 keys of a PENDING outbox item; delivered or failed items drop them (sparse index)."""
    return ("A2AOUT#pending", _part(created_at, "created_at"))


def gsi1_pchk(run_id: str, created_at: str, pchk_id: str) -> tuple[str, str]:
    """Lets ``cases.escalate`` find the latest policy check of a run without a scan."""
    return (f"PCHKRUN#{_part(run_id, 'run_id')}", f"{_part(created_at, 'created_at')}#{_part(pchk_id)}")


#: Reference data written by ``fixtures.seed`` (never deleted by a reset).
FIXTURE_PREFIXES: tuple[str, ...] = ("CUST#", "ORDER#", "PRODUCT#", "LOC#", "INV#")

#: Everything a demo run creates; ``fixtures.reset`` deletes every item whose pk starts with one
#: of these. ``ORDER#DEMO-A2A-`` are the orders the agent-to-agent purchase creates at runtime
#: (fixture orders are ``ORDER#DEMO-1001`` ...) and their lines share the pk; ``A2AOUT#`` is the
#: write-back outbox and ``RUN#`` also covers the per-owner A2A streams. G-27, contracts.md F.7.
RUNTIME_PREFIXES: tuple[str, ...] = (
    "CONV#",
    "RUN#",
    "PCHK#",
    "CAND#",
    "QUOTE#",
    "TXN#",
    "IDEM#",
    "CASE#",
    "AUDIT#",
    "SIZEREQ#",
    "A2AOUT#",
    "ORDER#DEMO-A2A-",
)

#: Id prefix of orders created by the gateway (channel ``agent``).
AGENT_ORDER_PREFIX = "DEMO-A2A-"


def order_items(order: Mapping[str, Any], lines: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One ``ORDER#/META`` item and one ``ORDER#/LINE#`` item per line, with every key set.

    ``order`` carries ``order_id``, ``customer_id``, ``ordered_at`` and the order fields
    (``banner``, ``currency``, ``channel``, ``delivered_at`` ...); each line carries
    ``line_id`` and the line fields. Used by ``fixtures`` and by the gateway's agent orders,
    so both write identical shapes (``list_orders`` reads them through GSI1).
    """
    oid = _part(order.get("order_id"), "order_id")
    cid = _part(order.get("customer_id"), "customer_id")
    ordered_at = _part(order.get("ordered_at"), "ordered_at")
    pk, sk = order_key(oid)
    g_pk, g_sk = gsi1_order(cid, ordered_at, oid)
    meta = {k: v for k, v in order.items() if k != "lines"}
    meta.update({"pk": pk, "sk": sk, "gsi1pk": g_pk, "gsi1sk": g_sk})
    items = [meta]
    for line in lines:
        l_pk, l_sk = line_key(oid, line.get("line_id"))  # type: ignore[arg-type]
        item = dict(line)
        item.update({"pk": l_pk, "sk": l_sk, "order_id": oid})
        items.append(item)
    return items


# ------------------------------------------------------------------------------ ids / sizes


def new_id(prefix: str, n: int = 6) -> str:
    """``prefix`` + n random upper-case hex characters, e.g. ``new_id("DEMO-RMA-")``."""
    return f"{prefix}{uuid.uuid4().hex[:n].upper()}"


def derived_id(prefix: str, *parts: str, n: int = 10) -> str:
    """Deterministic id: ``prefix`` + the first n hex characters of SHA-1 over the parts."""
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:n].upper()}"


_SIZE_TEXT = re.compile(r"^\d{1,2}(\.\d{1,3})?$")
MAX_SIZE = Decimal(30)


def size_str(x: str | int | float | Decimal) -> str:
    """Normalize a shoe size to a string with one decimal: ``"7"``, ``7``, ``7.0``, ``Decimal("7.50")``.

    Only whole and half sizes between 0.5 and 30 are valid; anything else raises
    VALIDATION_ERROR, so a float like 7.25 or a phrase like "seven" never reaches a key.
    """
    bad = DomainError(
        ErrorCode.VALIDATION_ERROR, "size must be a whole or half size such as 7 or 7.5"
    )
    if isinstance(x, bool):
        raise bad
    try:
        if isinstance(x, str):
            text = x.strip()
            if not _SIZE_TEXT.match(text):
                raise bad
            value = Decimal(text)
        elif isinstance(x, int):
            value = Decimal(x)
        elif isinstance(x, float):
            if not math.isfinite(x):
                raise bad
            value = Decimal(str(x))
        elif isinstance(x, Decimal):
            value = x
        else:
            raise bad
    except InvalidOperation:
        raise bad from None
    if not value.is_finite() or value < Decimal("0.5") or value > MAX_SIZE:
        raise bad
    doubled = value * 2
    if doubled != doubled.to_integral_value():
        raise bad
    return str(value.quantize(Decimal("0.1")))


def now_iso() -> str:
    """Current wall-clock time as ``2026-10-16T16:00:00+00:00`` (goes through ``clock.now_utc``)."""
    return clock.iso(clock.now_utc())


# --------------------------------------------------------- Decimal / float layer (G-03)


def to_ddb(obj: Any) -> Any:
    """Prepare a value for DynamoDB: floats become ``Decimal(str(x))``; NaN and Infinity are refused."""
    if isinstance(obj, Enum):
        return to_ddb(obj.value)
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        if not math.isfinite(obj):
            raise DomainError(ErrorCode.VALIDATION_ERROR, "NaN and Infinity cannot be stored")
        return Decimal(str(obj))
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            raise DomainError(ErrorCode.VALIDATION_ERROR, "NaN and Infinity cannot be stored")
        return obj
    if isinstance(obj, datetime):
        return clock.iso(obj) if obj.utcoffset() is not None else obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, (bytes, bytearray, memoryview, Binary)):
        raw = obj.value if isinstance(obj, Binary) else bytes(obj)
        return base64.b64encode(raw).decode("ascii")
    if isinstance(obj, PurePath):
        return str(obj)
    if hasattr(obj, "model_dump"):
        return to_ddb(obj.model_dump(mode="python"))
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return to_ddb(dataclasses.asdict(obj))
    if isinstance(obj, Mapping):
        return {str(k): to_ddb(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return [to_ddb(v) for v in _ordered(obj)]
    if isinstance(obj, (list, tuple)):
        return [to_ddb(v) for v in obj]
    raise DomainError(
        ErrorCode.VALIDATION_ERROR, f"cannot store a value of type {type(obj).__name__}"
    )


def from_ddb(obj: Any) -> Any:
    """Read-side counterpart: ``Decimal`` becomes ``int`` when integral, else ``float``."""
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            return float(obj)
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, Binary):
        return base64.b64encode(obj.value).decode("ascii")
    if isinstance(obj, Mapping):
        return {k: from_ddb(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [from_ddb(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return [from_ddb(v) for v in _ordered(obj)]
    return obj


def json_safe(obj: Any) -> Any:
    """JSON-native form for tool results, trace payloads and API bodies. Never returns a ``Decimal``.

    ``Decimal`` -> int or float, pydantic models and dataclasses -> dict, ``datetime`` -> UTC ISO
    string, ``bytes`` -> base64 string, enums -> value, sets -> sorted lists. Non-finite floats
    become ``None`` (JSON has no NaN).
    """
    if isinstance(obj, Enum):
        return json_safe(obj.value)
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, int):
        return int(obj)
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, Decimal):
        return from_ddb(obj)
    if isinstance(obj, datetime):
        return clock.iso(obj) if obj.utcoffset() is not None else obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, (bytes, bytearray, memoryview, Binary)):
        raw = obj.value if isinstance(obj, Binary) else bytes(obj)
        return base64.b64encode(raw).decode("ascii")
    if isinstance(obj, PurePath):
        return str(obj)
    if hasattr(obj, "model_dump"):
        return json_safe(obj.model_dump(mode="python"))
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return json_safe(dataclasses.asdict(obj))
    if isinstance(obj, Mapping):
        return {str(k): json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (set, frozenset)):
        return [json_safe(v) for v in _ordered(obj)]
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    raise DomainError(
        ErrorCode.INTERNAL_ERROR, f"a value of type {type(obj).__name__} is not JSON-serializable"
    )


def canonical_json(obj: Any) -> str:
    """Stable JSON text (sorted keys, no spaces) used for hashes."""
    return json.dumps(json_safe(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _ordered(values: Iterable[Any]) -> list[Any]:
    items = list(values)
    try:
        return sorted(items)
    except TypeError:
        return items


# ------------------------------------------------------------------------------ exceptions


class ConditionFailed(Exception):
    """A single-item ConditionExpression evaluated to false."""


TRANSIENT_REASONS: frozenset[str] = frozenset(
    {
        "TransactionConflict",
        "ThrottlingError",
        "ProvisionedThroughputExceeded",
        "TransactionInProgressException",
    }
)


class TransactionCanceled(Exception):
    """A TransactWriteItems call was cancelled; ``reasons[i]`` belongs to ``labels[i]``."""

    def __init__(self, reasons: Sequence[str | None], labels: Sequence[str]) -> None:
        self.reasons: list[str | None] = list(reasons)
        self.labels: list[str] = list(labels)
        super().__init__(
            "transaction cancelled: "
            + ", ".join(f"{label}={reason}" for label, reason in zip(self.labels, self.reasons))
        )

    def failed(self) -> list[str]:
        """Labels whose condition failed."""
        return [
            label
            for label, reason in zip(self.labels, self.reasons)
            if reason == "ConditionalCheckFailed"
        ]

    def transient(self) -> bool:
        """True when any operation was cancelled for a reason that a retry can resolve."""
        return any(reason in TRANSIENT_REASONS for reason in self.reasons)


# ------------------------------------------------------------------------- client lifecycle

_SERIALIZER = TypeSerializer()
_DESERIALIZER = TypeDeserializer()
_CLIENT_LOCK = threading.Lock()
_client: Any = None


def get_client() -> Any:
    """The cached low-level DynamoDB client (created on first use, after the environment is set)."""
    global _client
    if _client is None:
        with _CLIENT_LOCK:
            if _client is None:
                settings = config.get_settings()
                _client = boto3.client(
                    "dynamodb",
                    region_name=settings.region,
                    config=Config(
                        retries={"mode": "standard", "max_attempts": 3},
                        connect_timeout=3,
                        read_timeout=5,
                    ),
                )
    return _client


def reset_clients() -> None:
    """Forget the cached client (call after ``mock_aws`` starts or the environment changes)."""
    global _client
    with _CLIENT_LOCK:
        _client = None


def table_name() -> str:
    return config.get_settings().table_name


TABLE_SCHEMA: dict[str, Any] = {
    "AttributeDefinitions": [
        {"AttributeName": "pk", "AttributeType": "S"},
        {"AttributeName": "sk", "AttributeType": "S"},
        {"AttributeName": "gsi1pk", "AttributeType": "S"},
        {"AttributeName": "gsi1sk", "AttributeType": "S"},
    ],
    "KeySchema": [
        {"AttributeName": "pk", "KeyType": "HASH"},
        {"AttributeName": "sk", "KeyType": "RANGE"},
    ],
    "GlobalSecondaryIndexes": [
        {
            "IndexName": "GSI1",
            "KeySchema": [
                {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        }
    ],
    "BillingMode": "PAY_PER_REQUEST",
}


def create_table(name: str | None = None) -> None:
    """Create the single table (``TABLE_SCHEMA``). An existing table is left alone."""
    client = get_client()
    target = name or table_name()
    try:
        client.create_table(TableName=target, **copy.deepcopy(TABLE_SCHEMA))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceInUseException":
            return
        raise
    client.get_waiter("table_exists").wait(TableName=target)


# ------------------------------------------------------------------ expression plumbing

_NAME_REF = re.compile(r"#([A-Za-z_][A-Za-z0-9_]*)")
_VALUE_REF = re.compile(r":([A-Za-z_][A-Za-z0-9_]*)")
_ATTR_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_NETWORK_ERRORS = (
    EndpointConnectionError,
    ConnectTimeoutError,
    ReadTimeoutError,
    ConnectionClosedError,
)
_UNAVAILABLE_CODES = frozenset(
    {
        "ProvisionedThroughputExceededException",
        "ThrottlingException",
        "RequestLimitExceeded",
        "InternalServerError",
        "ServiceUnavailable",
    }
)
_CONFLICT_CODES = frozenset(
    {
        "TransactionConflictException",
        "TransactionInProgressException",
        "ThrottlingException",
        "ProvisionedThroughputExceededException",
        "RequestLimitExceeded",
    }
)


def _upstream_error() -> DomainError:
    return DomainError(
        ErrorCode.UPSTREAM_TIMEOUT, "The data store is busy. Please try again.", retryable=True
    )


@contextmanager
def _translate() -> Iterator[None]:
    """Map AWS failures to the domain shapes (condition failure, retryable upstream error)."""
    try:
        yield
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ConditionalCheckFailedException":
            raise ConditionFailed() from exc
        if code in _UNAVAILABLE_CODES:
            raise _upstream_error() from exc
        raise
    except _NETWORK_ERRORS as exc:  # never echo the cause
        raise _upstream_error() from exc


def _attr_name(name: str) -> str:
    if not isinstance(name, str) or not _ATTR_NAME.match(name):
        raise DomainError(ErrorCode.INTERNAL_ERROR, f"invalid attribute name {name!r}")
    return name


def _serialize(value: Any) -> dict[str, Any]:
    return _SERIALIZER.serialize(to_ddb(value))


def _serialize_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {k: _serialize(v) for k, v in item.items()}


def _deserialize_item(raw: Mapping[str, Any]) -> dict[str, Any]:
    return from_ddb({k: _DESERIALIZER.deserialize(v) for k, v in raw.items()})


def _key_dict(key: Key) -> dict[str, Any]:
    pk, sk = key
    return {"pk": {"S": pk}, "sk": {"S": sk}}


def _build_expressions(
    *,
    condition: str | None = None,
    values: Mapping[str, Any] | None = None,
    set_values: Mapping[str, Any] | None = None,
    add_values: Mapping[str, Any] | None = None,
    remove: Sequence[str] | None = None,
    must_exist: bool = False,
) -> dict[str, Any]:
    """Compile condition and update arguments into UpdateItem/ConditionExpression kwargs."""
    extra_values: dict[str, Any] = {}
    actions: list[str] = []
    names_needed: dict[str, None] = {}

    if set_values:
        parts = []
        for index, (name, value) in enumerate(set_values.items()):
            attr = _attr_name(name)
            ref = f"setv{index}"
            parts.append(f"#{attr} = :{ref}")
            extra_values[ref] = value
            names_needed[attr] = None
        actions.append("SET " + ", ".join(parts))
    if add_values:
        parts = []
        for index, (name, value) in enumerate(add_values.items()):
            attr = _attr_name(name)
            ref = f"addv{index}"
            parts.append(f"#{attr} :{ref}")
            extra_values[ref] = value
            names_needed[attr] = None
        actions.append("ADD " + ", ".join(parts))
    if remove:
        parts = []
        for name in remove:
            attr = _attr_name(name)
            parts.append(f"#{attr}")
            names_needed[attr] = None
        actions.append("REMOVE " + ", ".join(parts))

    cond = condition
    if must_exist:
        cond = "attribute_exists(#pk)" if not cond else f"attribute_exists(#pk) AND ({cond})"

    expressions = [e for e in (cond, *actions) if e]
    referenced_names = {m for e in expressions for m in _NAME_REF.findall(e)} | names_needed.keys()
    referenced_values = {m for e in expressions for m in _VALUE_REF.findall(e)}

    supplied: dict[str, Any] = {}
    for key, value in (values or {}).items():
        supplied[key[1:] if key.startswith(":") else key] = value
    clash = supplied.keys() & extra_values.keys()
    if clash:  # the helper reserves :setvN and :addvN for the set/add values
        raise DomainError(ErrorCode.INTERNAL_ERROR, f"expression value name(s) reserved: {sorted(clash)}")
    supplied.update(extra_values)

    missing = referenced_values - supplied.keys()
    if missing:
        raise DomainError(
            ErrorCode.INTERNAL_ERROR, f"expression value(s) not supplied: {sorted(missing)}"
        )

    out: dict[str, Any] = {}
    if actions:
        out["UpdateExpression"] = " ".join(actions)
    if cond:
        out["ConditionExpression"] = cond
    if referenced_names:
        out["ExpressionAttributeNames"] = {f"#{n}": n for n in sorted(referenced_names)}
    if referenced_values:
        out["ExpressionAttributeValues"] = {
            f":{n}": _serialize(supplied[n]) for n in sorted(referenced_values)
        }
    return out


# ------------------------------------------------------------------------------- helpers


def get_item(key: Key, *, consistent: bool = True) -> dict[str, Any] | None:
    with _translate():
        response = get_client().get_item(
            TableName=table_name(), Key=_key_dict(key), ConsistentRead=consistent
        )
    raw = response.get("Item")
    return _deserialize_item(raw) if raw else None


def put_item(
    item: Mapping[str, Any], *, condition: str | None = None, values: Mapping[str, Any] | None = None
) -> None:
    """Put a full item (it carries ``pk`` and ``sk``). Raises ``ConditionFailed``."""
    if "pk" not in item or "sk" not in item:
        raise DomainError(ErrorCode.INTERNAL_ERROR, "item is missing pk or sk")
    kwargs: dict[str, Any] = {"TableName": table_name(), "Item": _serialize_item(item)}
    kwargs.update(_build_expressions(condition=condition, values=values))
    with _translate():
        get_client().put_item(**kwargs)


def update_item(
    key: Key,
    *,
    set: Mapping[str, Any] | None = None,  # noqa: A002
    add: Mapping[str, Any] | None = None,
    remove: Sequence[str] | None = None,
    condition: str | None = None,
    values: Mapping[str, Any] | None = None,
    return_new: bool = True,
    must_exist: bool = True,
) -> dict[str, Any] | None:
    """UpdateItem. Raises ``ConditionFailed`` (also when the item is missing and ``must_exist``)."""
    if not (set or add or remove):
        raise DomainError(ErrorCode.INTERNAL_ERROR, "update_item needs set, add or remove")
    kwargs: dict[str, Any] = {
        "TableName": table_name(),
        "Key": _key_dict(key),
        "ReturnValues": "ALL_NEW" if return_new else "NONE",
    }
    kwargs.update(
        _build_expressions(
            condition=condition,
            values=values,
            set_values=set,
            add_values=add,
            remove=remove,
            must_exist=must_exist,
        )
    )
    with _translate():
        response = get_client().update_item(**kwargs)
    raw = response.get("Attributes")
    return _deserialize_item(raw) if raw else None


def delete_item(key: Key) -> None:
    with _translate():
        get_client().delete_item(TableName=table_name(), Key=_key_dict(key))


def query(
    pk: str,
    *,
    sk_begins_with: str | None = None,
    sk_gt: str | None = None,
    sk_between: tuple[str, str] | None = None,
    index: str | None = None,
    consistent: bool = True,
    forward: bool = True,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Query one partition (or one GSI1 partition when ``index="GSI1"``); paginates internally.

    ``limit`` caps the total number of items returned. At most one of ``sk_begins_with``,
    ``sk_gt`` and ``sk_between`` (inclusive bounds) may be used. GSI reads are eventually
    consistent, so ``consistent`` is ignored when ``index`` is set.
    """
    if sum(arg is not None for arg in (sk_begins_with, sk_gt, sk_between)) > 1:
        raise DomainError(ErrorCode.INTERNAL_ERROR, "use only one sort-key condition")
    pk_attr, sk_attr = ("gsi1pk", "gsi1sk") if index else ("pk", "sk")
    names = {"#pk": pk_attr}
    values: dict[str, Any] = {":pk": {"S": pk}}
    expression = "#pk = :pk"
    if sk_begins_with is not None:
        names["#sk"] = sk_attr
        values[":sk0"] = {"S": sk_begins_with}
        expression += " AND begins_with(#sk, :sk0)"
    elif sk_gt is not None:
        names["#sk"] = sk_attr
        values[":sk0"] = {"S": sk_gt}
        expression += " AND #sk > :sk0"
    elif sk_between is not None:
        names["#sk"] = sk_attr
        values[":sk0"] = {"S": sk_between[0]}
        values[":sk1"] = {"S": sk_between[1]}
        expression += " AND #sk BETWEEN :sk0 AND :sk1"

    kwargs: dict[str, Any] = {
        "TableName": table_name(),
        "KeyConditionExpression": expression,
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
        "ScanIndexForward": forward,
    }
    if index:
        kwargs["IndexName"] = index
    elif consistent:
        kwargs["ConsistentRead"] = True

    items: list[dict[str, Any]] = []
    start: dict[str, Any] | None = None
    client = get_client()
    while True:
        if start:
            kwargs["ExclusiveStartKey"] = start
        if limit is not None:
            kwargs["Limit"] = max(1, limit - len(items))
        with _translate():
            response = client.query(**kwargs)
        items.extend(_deserialize_item(raw) for raw in response.get("Items", []))
        start = response.get("LastEvaluatedKey")
        if not start or (limit is not None and len(items) >= limit):
            break
    return items[:limit] if limit is not None else items


def batch_get(keys: Sequence[Key]) -> list[dict[str, Any]]:
    """Consistent BatchGetItem for any number of keys (100 per request, unprocessed keys retried)."""
    unique = list(dict.fromkeys(keys))
    found: list[dict[str, Any]] = []
    client = get_client()
    table = table_name()
    for start in range(0, len(unique), 100):
        pending: list[dict[str, Any]] = [_key_dict(k) for k in unique[start : start + 100]]
        for attempt in range(8):
            with _translate():
                response = client.batch_get_item(
                    RequestItems={table: {"Keys": pending, "ConsistentRead": True}}
                )
            found.extend(_deserialize_item(raw) for raw in response.get("Responses", {}).get(table, []))
            pending = response.get("UnprocessedKeys", {}).get(table, {}).get("Keys", [])
            if not pending:
                break
            _backoff(attempt)
        else:
            raise _upstream_error()
    return found


def batch_put(items: Sequence[Mapping[str, Any]]) -> int:
    """BatchWriteItem puts in groups of 25 (overwrites). Items must have unique keys."""
    seen: set[Key] = set()
    for item in items:
        key = (item["pk"], item["sk"])
        if key in seen:
            raise DomainError(ErrorCode.INTERNAL_ERROR, "duplicate key in batch_put")
        seen.add(key)
    requests = [{"PutRequest": {"Item": _serialize_item(item)}} for item in items]
    _batch_write(requests)
    return len(requests)


def batch_delete(keys: Sequence[Key]) -> int:
    """BatchWriteItem deletes in groups of 25; UnprocessedItems are retried with backoff."""
    requests = [{"DeleteRequest": {"Key": _key_dict(k)}} for k in dict.fromkeys(keys)]
    _batch_write(requests)
    return len(requests)


def _batch_write(requests: list[dict[str, Any]]) -> None:
    client = get_client()
    table = table_name()
    for start in range(0, len(requests), 25):
        pending = requests[start : start + 25]
        for attempt in range(8):
            with _translate():
                response = client.batch_write_item(RequestItems={table: pending})
            pending = response.get("UnprocessedItems", {}).get(table, [])
            if not pending:
                break
            _backoff(attempt)
        else:
            raise _upstream_error()


def scan_keys(prefixes: Sequence[str]) -> Iterator[Key]:
    """Yield ``(pk, sk)`` of every item whose pk starts with one of ``prefixes`` (consistent Scan).

    Only for ``fixtures.reset``; the table is small and a reset is an operator action.
    """
    if not prefixes:
        return
    names = {"#pk": "pk", "#sk": "sk"}
    values = {f":p{i}": {"S": prefix} for i, prefix in enumerate(prefixes)}
    kwargs: dict[str, Any] = {
        "TableName": table_name(),
        "ProjectionExpression": "#pk, #sk",
        "FilterExpression": " OR ".join(f"begins_with(#pk, :p{i})" for i in range(len(prefixes))),
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
        "ConsistentRead": True,
    }
    client = get_client()
    start: dict[str, Any] | None = None
    while True:
        if start:
            kwargs["ExclusiveStartKey"] = start
        with _translate():
            response = client.scan(**kwargs)
        for raw in response.get("Items", []):
            yield (raw["pk"]["S"], raw["sk"]["S"])
        start = response.get("LastEvaluatedKey")
        if not start:
            return


def _backoff(attempt: int) -> None:
    time.sleep(min(1.0, 0.05 * (2**attempt)) * (0.5 + random.random() / 2))


# --------------------------------------------------------------------------- transactions


@dataclass
class TxPut:
    item: dict[str, Any]
    condition: str | None = None
    values: dict[str, Any] | None = None
    label: str = ""


@dataclass
class TxUpdate:
    key: Key
    set: dict[str, Any] | None = None  # noqa: A003
    add: dict[str, Any] | None = None
    condition: str | None = None
    values: dict[str, Any] | None = None
    label: str = ""
    remove: list[str] | None = None
    must_exist: bool = True


@dataclass
class TxCheck:
    key: Key
    condition: str
    values: dict[str, Any] | None = None
    label: str = ""


def transact_write(
    ops: Sequence[TxPut | TxUpdate | TxCheck], *, client_request_token: str | None = None
) -> None:
    """All-or-nothing TransactWriteItems. Raises ``TransactionCanceled`` with per-label reasons."""
    if not ops:
        raise DomainError(ErrorCode.INTERNAL_ERROR, "transact_write needs at least one operation")
    table = table_name()
    labels = [op.label or f"op{index}" for index, op in enumerate(ops)]
    items: list[dict[str, Any]] = []
    for op in ops:
        if isinstance(op, TxPut):
            if "pk" not in op.item or "sk" not in op.item:
                raise DomainError(ErrorCode.INTERNAL_ERROR, "item is missing pk or sk")
            put = {"TableName": table, "Item": _serialize_item(op.item)}
            put.update(_build_expressions(condition=op.condition, values=op.values))
            items.append({"Put": put})
        elif isinstance(op, TxUpdate):
            update = {"TableName": table, "Key": _key_dict(op.key)}
            update.update(
                _build_expressions(
                    condition=op.condition,
                    values=op.values,
                    set_values=op.set,
                    add_values=op.add,
                    remove=op.remove,
                    must_exist=op.must_exist,
                )
            )
            items.append({"Update": update})
        elif isinstance(op, TxCheck):
            check = {"TableName": table, "Key": _key_dict(op.key)}
            check.update(_build_expressions(condition=op.condition, values=op.values))
            items.append({"ConditionCheck": check})
        else:
            raise DomainError(ErrorCode.INTERNAL_ERROR, "unknown transaction operation")

    kwargs: dict[str, Any] = {"TransactItems": items}
    if client_request_token:
        kwargs["ClientRequestToken"] = client_request_token
    try:
        get_client().transact_write_items(**kwargs)
    except ClientError as exc:
        error = exc.response.get("Error", {})
        code = error.get("Code")
        if code == "TransactionCanceledException":
            raw_reasons = exc.response.get("CancellationReasons") or []
            reasons: list[str | None] = []
            for index in range(len(ops)):
                reason = raw_reasons[index].get("Code") if index < len(raw_reasons) else None
                reasons.append(None if reason in (None, "None") else reason)
            raise TransactionCanceled(reasons, labels) from exc
        if code in _CONFLICT_CODES:
            # Top-level conflict or throttling: no per-operation detail, all transient.
            reason = (
                "TransactionConflict"
                if code in ("TransactionConflictException", "TransactionInProgressException")
                else "ThrottlingError"
            )
            raise TransactionCanceled([reason] * len(ops), labels) from exc
        if code in _UNAVAILABLE_CODES:
            raise _upstream_error() from exc
        raise
    except _NETWORK_ERRORS as exc:
        raise _upstream_error() from exc
