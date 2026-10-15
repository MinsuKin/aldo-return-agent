"""ALDO bot store: owner size memory, ALDO write-backs, conversations, runs and events.

One DynamoDB table (``PERSONAL_AGENT_TABLE_NAME``, default ``aldo-personal-agent-demo``), keys per
PIVOT_ADDENDUM 5.5 and contracts.md F.9. Everything lives in the owner's partition::

    pk = OWNER#{owner_id}
      sk = PROFILE                       typical size, size system, display name
      sk = MEM#{memory_id}               one remembered purchase (synthetic retailer label)
      sk = UPD#{update_id}               applied ALDO write-back (provisional flag, status)
      sk = PCONV#{conv_id}               conversation: messages (JSON string), run lease
      sk = PRUN#{run_id}#META            run meta: status, seq counter, metrics
      sk = PRUN#{run_id}#EVT#{seq:06d}   run event (payload is a JSON string)

Decimal-safe by construction: only strings, ints and booleans are written. Event payloads and
conversation messages are stored as JSON *strings*, so floats never reach DynamoDB.

Run and event helpers mirror ``returns_domain.trace``/``runs`` semantics (same event shape, plus
``actor``) but are stored in this table so the ALDO agent role never touches personal memory.
Another owner's conversation or run is simply NOT_FOUND: the partition key is the owner.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import json
import math
import re
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from boto3.dynamodb.conditions import Key
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, field_validator

from returns_domain import clock
from returns_domain import config as domain_config
from returns_domain.errors import DomainError, ErrorCode

from . import a2a, settings
from .sanitize import normalize_text

SK_PROFILE = "PROFILE"
SIZE_PATTERN = r"^\d{1,2}\.\d$"
MEMORY_OUTCOMES = ("kept", "returned_too_big", "returned_too_small")
UPDATE_OUTCOMES = ("exchange_created", "exchange_completed_kept")
RUN_ID_PREFIX = "PRUN-"
CONV_ID_PREFIX = "PCONV-"
MEMORY_LABEL = "Synthetic demo memory"
MEMORY_NOTE = "synthetic demo memory"

#: Event types of an ALDO bot run (left panel). ``actor`` is personal_agent or system.
EVENT_TYPES = (
    "run_started",
    "tool_call",
    "tool_result",
    "decision",
    "size_assist_requested",
    "size_recommended",
    "handoff_to_returns",
    "assistant_message",
    "run_completed",
    "warning",
)
CONVERSATION_MODES = ("bot", "returns")
MODE_BOT = "bot"
MODE_RETURNS = "returns"
ACTORS = ("personal_agent", "system")

# A key is sensitive when its LAST segment is token/secret/authorization/password/email, or it is a
# customer/owner id, an API key or one of our signing keys. "input_tokens" (a count) is NOT sensitive.
_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(?:token|secret|authorization|password|email)$"
    r"|^(?:customer|owner)[_-]?id$"
    r"|api[_-]?key|(?:writeback|signing|admin)[_-]?key",
    re.IGNORECASE,
)
_ID_ANCHORED = f"^{settings.ID_REGEX}$"
_STRING_CAPS = {"assistant_message": 1500}
_DEFAULT_STRING_CAP = 500
_THROTTLE_CODES = frozenset(
    {
        "ProvisionedThroughputExceededException",
        "ThrottlingException",
        "RequestLimitExceeded",
        "InternalServerError",
        "ServiceUnavailable",
    }
)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_NAME_TOKEN = re.compile(r"#\w+")
_VALUE_TOKEN = re.compile(r":\w+")


class ConditionFailed(Exception):
    """A DynamoDB conditional write was rejected (internal control flow, never leaves the module)."""


# ------------------------------------------------------------------------------ ids and keys


def new_conv_id() -> str:
    return CONV_ID_PREFIX + uuid.uuid4().hex[:10].upper()


def new_run_id() -> str:
    return RUN_ID_PREFIX + uuid.uuid4().hex[:10].upper()


def is_personal_run_id(run_id: object) -> bool:
    """True for run ids minted by this package; the API uses it to route event reads."""
    return isinstance(run_id, str) and run_id.startswith(RUN_ID_PREFIX)


def owner_pk(owner_id: str) -> str:
    return f"OWNER#{owner_id}"


def _sk_mem(memory_id: str) -> str:
    return f"MEM#{memory_id}"


def _sk_upd(update_id: str) -> str:
    return f"UPD#{update_id}"


def _sk_conv(conv_id: str) -> str:
    return f"PCONV#{conv_id}"


def _sk_run(run_id: str) -> str:
    return f"PRUN#{run_id}#META"


def _sk_evt(run_id: str, seq: int) -> str:
    return f"PRUN#{run_id}#EVT#{seq:06d}"


# --------------------------------------------------------------------------- JSON utilities


def _plain(value: Any) -> Any:
    """DynamoDB (boto3) values -> plain Python: Decimal -> int (or float), sets -> lists."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(item) for item in value]
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)


def jsonable(value: Any) -> Any:
    """Deep copy of ``value`` made of JSON-native types only (bytes -> base64, NaN/Inf -> None)."""

    def clean(item: Any) -> Any:
        if isinstance(item, float) and not math.isfinite(item):
            return None
        if isinstance(item, Mapping):
            return {str(key): clean(sub) for key, sub in item.items()}
        if isinstance(item, (list, tuple, set, frozenset)):
            return [clean(sub) for sub in item]
        return item

    return json.loads(json.dumps(clean(value), default=_json_default, allow_nan=False))


def redact_payload(value: Any, *, string_cap: int = _DEFAULT_STRING_CAP) -> Any:
    """Drop sensitive keys (tokens, secrets, customer/owner ids, emails) and cap long strings."""
    if isinstance(value, Mapping):
        return {
            str(key): redact_payload(sub, string_cap=string_cap)
            for key, sub in value.items()
            if not _SENSITIVE_KEY.search(str(key))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_payload(sub, string_cap=string_cap) for sub in value]
    if isinstance(value, str) and len(value) > string_cap:
        return value[: string_cap - 1] + "…"
    return value


# ------------------------------------------------------------------------- message helpers


def strip_reasoning(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """JSON-safe copy of a Strands message list without ``reasoningContent`` blocks."""
    safe = jsonable(copy.deepcopy(messages))
    for message in safe:
        content = message.get("content")
        if isinstance(content, list):
            message["content"] = [
                block
                for block in content
                if not (isinstance(block, dict) and "reasoningContent" in block)
            ]
    return safe


def _blocks(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _tool_use_ids(message: Mapping[str, Any]) -> set[str]:
    return {
        str(block["toolUse"].get("toolUseId"))
        for block in _blocks(message)
        if isinstance(block.get("toolUse"), dict)
    }


def _tool_result_ids(message: Mapping[str, Any]) -> set[str]:
    return {
        str(block["toolResult"].get("toolUseId"))
        for block in _blocks(message)
        if isinstance(block.get("toolResult"), dict)
    }


def clean_boundary(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Longest prefix that ends on a final assistant reply with every toolUse answered.

    Valid shape per turn: user(text) -> [assistant(toolUse) -> user(toolResult)]* -> assistant(text).
    Anything after the last complete turn (for example an unanswered toolUse) is dropped.
    """
    last_ok = 0
    state = "user"
    pending: set[str] = set()
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            break
        role = message.get("role")
        if state == "user":
            if role != "user" or _tool_result_ids(message):
                break
            state = "assistant"
        elif state == "assistant":
            if role != "assistant":
                break
            pending = _tool_use_ids(message)
            if pending:
                state = "result"
            else:
                state = "user"
                last_ok = index + 1
        else:  # state == "result"
            if role != "user" or _tool_result_ids(message) != pending:
                break
            state = "assistant"
    return messages[:last_ok]


def _size(messages: list[dict[str, Any]]) -> int:
    return len(json.dumps(messages, ensure_ascii=False).encode("utf-8"))


def _truncate_tool_results(messages: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    out = copy.deepcopy(messages)
    for message in out:
        for block in _blocks(message):
            result = block.get("toolResult")
            if not isinstance(result, dict):
                continue
            for part in result.get("content") or []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    if len(part["text"]) > limit:
                        part["text"] = part["text"][:limit] + " …[truncated]"
    return out


def _split_turns(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    turns: list[list[dict[str, Any]]] = []
    for message in messages:
        starts_turn = message.get("role") == "user" and not _tool_result_ids(message)
        if starts_turn or not turns:
            turns.append([])
        turns[-1].append(message)
    return turns


def _text_only_tail(messages: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Last resort: the final user prompt and the final assistant reply as plain text."""
    turns = _split_turns(messages)
    if not turns:
        return []
    turn = turns[-1]
    prompt = " ".join(b["text"] for b in _blocks(turn[0]) if isinstance(b.get("text"), str))
    reply = " ".join(b["text"] for b in _blocks(turn[-1]) if isinstance(b.get("text"), str))
    return [
        {"role": "user", "content": [{"text": prompt[:limit]}]},
        {"role": "assistant", "content": [{"text": reply[:limit]}]},
    ]


def shrink_messages(messages: list[dict[str, Any]], max_bytes: int) -> list[dict[str, Any]]:
    """Fit a clean message list under ``max_bytes`` keeping toolUse/toolResult pairs intact.

    Steps: truncate oversized tool results (8 KB), drop the oldest whole turns, truncate tool
    results harder (1 KB), and finally keep only the last prompt and reply as plain text.
    """
    shrunk = _truncate_tool_results(messages, 8_000)
    while _size(shrunk) > max_bytes:
        turns = _split_turns(shrunk)
        if len(turns) <= 1:
            break
        shrunk = [message for turn in turns[1:] for message in turn]
    if _size(shrunk) > max_bytes:
        shrunk = _truncate_tool_results(shrunk, 1_000)
    if _size(shrunk) > max_bytes:
        shrunk = _text_only_tail(shrunk, max(1, max_bytes // 4))
    return shrunk


# --------------------------------------------------------------------- pydantic data models


class MemoryEntry(BaseModel):
    """One remembered purchase at another (synthetic) retailer."""

    memory_id: str
    retailer: str
    item: str
    size: str
    outcome: Literal["kept", "returned_too_big", "returned_too_small"]
    source: str = "synthetic"
    color: str | None = None  # optional: the colour of the item (feeds the return workflow's context)


class AppliedUpdate(BaseModel):
    """An ALDO size write-back that was applied (contracts.md F.9, plus status fields)."""

    update_id: str
    style_id: str
    product_name: str
    color: str
    original_size: str
    new_size: str
    reason: str
    outcome: str
    provisional: bool
    confidence: Literal["provisional", "confirmed"]
    applied_at: str
    source: str = "ALDO_CA"
    order_id: str | None = None
    status: Literal["active", "superseded"] = "active"
    superseded_by: str | None = None
    #: "size_ok" on a PREFERENCE (colour) exchange: the owner kept the size, only the colour changed.
    fit_feedback: Literal["size_ok"] | None = None


class OwnerMemory(BaseModel):
    """What ``GET /api/personal-agent/memory`` returns (contracts.md C.8 ``OwnerMemoryView``)."""

    owner_id: str
    display_name: str
    size_system: str
    typical_size: str
    source: str = "synthetic"
    label: str = MEMORY_LABEL
    memory: list[MemoryEntry]
    updates: list[AppliedUpdate]


class MemoryUpdate(BaseModel):
    """Body of ``POST /api/personal-agent/memory-updates`` (PIVOT_ADDENDUM 5.4). Unknown keys are rejected."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    update_id: str = Field(pattern=_ID_ANCHORED)
    owner_ref: str = Field(pattern=_ID_ANCHORED)
    order_id: str = Field(pattern=_ID_ANCHORED)
    style_id: str = Field(pattern=_ID_ANCHORED)
    product_name: str = Field(min_length=1, max_length=120)
    color: str = Field(min_length=1, max_length=40)
    original_size: str = Field(pattern=SIZE_PATTERN)
    new_size: str = Field(pattern=SIZE_PATTERN)
    reason: str = Field(pattern=r"^[A-Z][A-Z_]{2,31}$")
    outcome: Literal["exchange_created", "exchange_completed_kept"]
    provisional: StrictBool
    source: str = Field(min_length=1, max_length=40)
    ts: str = Field(min_length=10, max_length=40)
    fit_feedback: Literal["size_ok"] | None = None  # set by ALDO on a PREFERENCE (colour) exchange

    @field_validator("product_name", "color", "source", mode="after")
    @classmethod
    def _clean_text(cls, value: str) -> str:
        cleaned = normalize_text(value)
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned


def validate_update(raw: object) -> MemoryUpdate:
    """Parse a write-back body; raises VALIDATION_ERROR naming the bad fields only (never values)."""
    if not isinstance(raw, Mapping):
        raise DomainError(ErrorCode.VALIDATION_ERROR, "memory update must be a JSON object")
    try:
        update = MemoryUpdate.model_validate(dict(raw))
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in err["loc"]) or "body" for err in exc.errors()})
        raise DomainError(
            ErrorCode.VALIDATION_ERROR, f"invalid memory update: {', '.join(fields)}"
        ) from None
    expected_provisional = update.outcome == "exchange_created"
    if update.provisional != expected_provisional:
        raise DomainError(
            ErrorCode.VALIDATION_ERROR,
            "invalid memory update: provisional must be true for exchange_created and false "
            "for exchange_completed_kept",
        )
    clock.parse_iso(update.ts)  # VALIDATION_ERROR when not an ISO-8601 timestamp with an offset
    return update


# ------------------------------------------------------------------------------ fixtures


def default_fixture_path() -> Path:
    """FIXTURES_DIR/personal_agent/memory.json (deployed bundle), else the repo fixture."""
    candidates = [
        domain_config.get_settings().fixtures_dir / "personal_agent" / "memory.json",
        _REPO_ROOT / "fixtures" / "personal_agent" / "memory.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise DomainError(ErrorCode.INTERNAL_ERROR, "personal agent memory fixture not found")


def load_fixture(path: Path | str | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else default_fixture_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DomainError(ErrorCode.INTERNAL_ERROR, "personal agent memory fixture is unreadable") from None
    problem = _fixture_problem(data)
    if problem:
        raise DomainError(ErrorCode.INTERNAL_ERROR, f"personal agent memory fixture is invalid: {problem}")
    return data


def _fixture_problem(data: object) -> str | None:
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        return "schema_version must be 1"
    owners = data.get("owners")
    if not isinstance(owners, list) or not owners:
        return "owners must be a non-empty list"
    size_ok = re.compile(SIZE_PATTERN)
    seen_owners: set[str] = set()
    for owner in owners:
        if not isinstance(owner, dict) or not settings.valid_id(owner.get("owner_id")):
            return "owner_id is missing or malformed"
        if owner["owner_id"] in seen_owners:
            return "duplicate owner_id"
        seen_owners.add(owner["owner_id"])
        for key in ("display_name", "size_system", "source"):
            if not isinstance(owner.get(key), str) or not owner[key].strip():
                return f"owner {key} is missing"
        if owner["source"] != "synthetic":
            return "owner source must be synthetic"
        if not isinstance(owner.get("typical_size"), str) or not size_ok.match(owner["typical_size"]):
            return "typical_size must be a size string with one decimal"
        entries = owner.get("memory")
        if not isinstance(entries, list):
            return "memory must be a list"
        seen_ids: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or not settings.valid_id(entry.get("memory_id")):
                return "memory_id is missing or malformed"
            if entry["memory_id"] in seen_ids:
                return "duplicate memory_id"
            seen_ids.add(entry["memory_id"])
            for key in ("retailer", "item", "source"):
                if not isinstance(entry.get(key), str) or not entry[key].strip():
                    return f"memory {key} is missing"
            if entry["source"] != "synthetic":
                return "memory source must be synthetic"
            if not isinstance(entry.get("size"), str) or not size_ok.match(entry["size"]):
                return "memory size must be a size string with one decimal"
            if entry.get("outcome") not in MEMORY_OUTCOMES:
                return "memory outcome is not one of kept, returned_too_big, returned_too_small"
            if "color" in entry and (not isinstance(entry["color"], str) or not entry["color"].strip()):
                return "memory color must be a non-empty string when present"
    return None


# ---------------------------------------------------------------------------- repository


class MemoryRepo:
    """DynamoDB repository for the personal-agent table.

    ``resource`` is an optional boto3 DynamoDB service resource (tests, local runs); by default one
    is created lazily for ``region`` so importing this module never opens a client.
    """

    def __init__(
        self,
        table_name: str | None = None,
        *,
        resource: Any = None,
        region: str | None = None,
    ) -> None:
        self.table_name = table_name or settings.table_name()
        self._resource = resource
        self._region = region or settings.region()
        self._table: Any = None

    @property
    def table(self) -> Any:
        if self._table is None:
            resource = self._resource or boto3.resource(
                "dynamodb",
                region_name=self._region,
                config=Config(
                    retries={"mode": "standard", "max_attempts": 3},
                    connect_timeout=3,
                    read_timeout=5,
                ),
            )
            self._table = resource.Table(self.table_name)
        return self._table

    # ------------------------------------------------------------------ low-level helpers

    @contextlib.contextmanager
    def _ddb(self) -> Iterator[None]:
        try:
            yield
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                raise ConditionFailed from exc
            if code in _THROTTLE_CODES:
                raise DomainError(
                    ErrorCode.UPSTREAM_TIMEOUT, "the personal agent store is busy, try again", True
                ) from exc
            raise DomainError(ErrorCode.INTERNAL_ERROR, "personal agent store error") from exc
        except BotoCoreError as exc:
            raise DomainError(
                ErrorCode.UPSTREAM_TIMEOUT, "the personal agent store is unreachable", True
            ) from exc

    def _get(self, pk: str, sk: str, *, consistent: bool = True) -> dict[str, Any] | None:
        with self._ddb():
            response = self.table.get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=consistent)
        item = response.get("Item")
        return _plain(item) if item else None

    def _put(self, item: dict[str, Any], *, if_absent: bool = False) -> None:
        kwargs: dict[str, Any] = {"Item": item}
        if if_absent:
            kwargs["ConditionExpression"] = "attribute_not_exists(#pk)"
            kwargs["ExpressionAttributeNames"] = {"#pk": "pk"}
        with self._ddb():
            self.table.put_item(**kwargs)

    def _update(
        self,
        pk: str,
        sk: str,
        expression: str,
        *,
        condition: str | None = None,
        names: Mapping[str, str] | None = None,
        values: Mapping[str, Any] | None = None,
        return_values: str = "NONE",
    ) -> dict[str, Any]:
        text = f"{expression} {condition or ''}"
        used_names = {t: n for t, n in (names or {}).items() if t in set(_NAME_TOKEN.findall(text))}
        used_values = {t: v for t, v in (values or {}).items() if t in set(_VALUE_TOKEN.findall(text))}
        kwargs: dict[str, Any] = {
            "Key": {"pk": pk, "sk": sk},
            "UpdateExpression": expression,
            "ReturnValues": return_values,
        }
        if condition:
            kwargs["ConditionExpression"] = condition
        if used_names:
            kwargs["ExpressionAttributeNames"] = used_names
        if used_values:
            kwargs["ExpressionAttributeValues"] = used_values
        with self._ddb():
            response = self.table.update_item(**kwargs)
        return _plain(response.get("Attributes") or {})

    def _query(
        self,
        pk: str,
        *,
        begins_with: str | None = None,
        between: tuple[str, str] | None = None,
        limit: int | None = None,
        consistent: bool = True,
    ) -> list[dict[str, Any]]:
        condition = Key("pk").eq(pk)
        if begins_with is not None:
            condition = condition & Key("sk").begins_with(begins_with)
        elif between is not None:
            condition = condition & Key("sk").between(*between)
        items: list[dict[str, Any]] = []
        start_key: dict[str, Any] | None = None
        while True:
            kwargs: dict[str, Any] = {"KeyConditionExpression": condition, "ConsistentRead": consistent}
            if start_key:
                kwargs["ExclusiveStartKey"] = start_key
            if limit is not None:
                kwargs["Limit"] = max(1, limit - len(items))
            with self._ddb():
                response = self.table.query(**kwargs)
            items.extend(_plain(item) for item in response.get("Items", []))
            start_key = response.get("LastEvaluatedKey")
            if not start_key or (limit is not None and len(items) >= limit):
                break
        return items[:limit] if limit is not None else items

    # ---------------------------------------------------------------------- table lifecycle

    def create_table(self) -> None:
        """Create the table (moto tests, local runs). CDK owns the real table."""
        resource = self._resource or boto3.resource("dynamodb", region_name=self._region)
        resource.create_table(
            TableName=self.table_name,
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

    # ------------------------------------------------------------------ seed / reset / read

    def seed(self, fixture_path: Path | str | None = None) -> dict[str, int]:
        """Write the fixture owners and memories (overwrite, never deletes anything else)."""
        data = load_fixture(fixture_path)
        owners = entries = 0
        with self._ddb(), self.table.batch_writer(overwrite_by_pkeys=["pk", "sk"]) as batch:
            for owner in data["owners"]:
                pk = owner_pk(owner["owner_id"])
                batch.put_item(
                    Item={
                        "pk": pk,
                        "sk": SK_PROFILE,
                        "owner_id": owner["owner_id"],
                        "display_name": owner["display_name"],
                        "size_system": owner["size_system"],
                        "typical_size": owner["typical_size"],
                        "source": owner["source"],
                    }
                )
                owners += 1
                for entry in owner["memory"]:
                    item = {
                        "pk": pk,
                        "sk": _sk_mem(entry["memory_id"]),
                        "memory_id": entry["memory_id"],
                        "retailer": entry["retailer"],
                        "item": entry["item"],
                        "size": entry["size"],
                        "outcome": entry["outcome"],
                        "source": entry["source"],
                    }
                    if entry.get("color"):
                        item["color"] = entry["color"].strip().upper()
                    batch.put_item(Item=item)
                    entries += 1
        return {"owners": owners, "entries": entries}

    def reset(self, fixture_path: Path | str | None = None) -> dict[str, int]:
        """Delete EVERYTHING in this table (it is the dedicated ALDO bot demo table), then seed."""
        load_fixture(fixture_path)  # fail before deleting anything if the fixture is unusable
        keys: list[dict[str, str]] = []
        start_key: dict[str, Any] | None = None
        while True:
            kwargs: dict[str, Any] = {"ProjectionExpression": "pk, sk", "ConsistentRead": True}
            if start_key:
                kwargs["ExclusiveStartKey"] = start_key
            with self._ddb():
                response = self.table.scan(**kwargs)
            keys.extend({"pk": item["pk"], "sk": item["sk"]} for item in response.get("Items", []))
            start_key = response.get("LastEvaluatedKey")
            if not start_key:
                break
        with self._ddb(), self.table.batch_writer() as batch:
            for key in keys:
                batch.delete_item(Key=key)
        return {"deleted": len(keys), **self.seed(fixture_path)}

    def get_owner(self, owner_id: str) -> dict[str, Any] | None:
        settings.require_id(owner_id, "owner_id")
        item = self._get(owner_pk(owner_id), SK_PROFILE)
        if item is None:
            return None
        return {
            "owner_id": item["owner_id"],
            "display_name": item["display_name"],
            "size_system": item["size_system"],
            "typical_size": item["typical_size"],
            "source": item.get("source", "synthetic"),
        }

    def get_owner_memory(self, owner_id: str, *, include_superseded: bool = False) -> OwnerMemory:
        """The owner's view: profile, remembered purchases and applied ALDO write-backs.

        ``updates`` holds active updates (provisional ones with ``confidence: provisional``);
        superseded ones only with ``include_superseded=True``.
        """
        owner = self.get_owner(owner_id)
        if owner is None:
            raise DomainError(ErrorCode.NOT_FOUND, "no personal agent profile for this owner")
        pk = owner_pk(owner_id)
        memory = [MemoryEntry(**_public_memory(item)) for item in self._query(pk, begins_with="MEM#")]
        memory.sort(key=lambda entry: _natural_key(entry.memory_id))
        applied = [self._applied(item) for item in self._query(pk, begins_with="UPD#")]
        if not include_superseded:
            applied = [update for update in applied if update[0].status == "active"]
        applied.sort(key=lambda pair: (pair[1], pair[0].update_id))
        return OwnerMemory(
            owner_id=owner["owner_id"],
            display_name=owner["display_name"],
            size_system=owner["size_system"],
            typical_size=owner["typical_size"],
            source=owner["source"],
            memory=memory,
            updates=[pair[0] for pair in applied],
        )

    @staticmethod
    def _applied(item: Mapping[str, Any]) -> tuple[AppliedUpdate, str]:
        provisional = bool(item.get("provisional"))
        update = AppliedUpdate(
            update_id=item["update_id"],
            style_id=item.get("style_id", ""),
            product_name=item.get("product_name", ""),
            color=item.get("color", ""),
            original_size=item.get("original_size", ""),
            new_size=item.get("new_size", ""),
            reason=item.get("reason", ""),
            outcome=item.get("outcome", ""),
            provisional=provisional,
            confidence="provisional" if provisional else "confirmed",
            applied_at=item.get("applied_at", ""),
            source=item.get("source", "ALDO_CA"),
            order_id=item.get("order_id"),
            status=item.get("status", "active"),
            superseded_by=item.get("superseded_by"),
            fit_feedback=item.get("fit_feedback"),
        )
        return update, str(item.get("ts", ""))

    def list_memory(self, owner_id: str) -> dict[str, Any]:
        """``GET /api/personal-agent/memory`` body: ``OwnerMemory`` as a JSON-ready dict."""
        return self.get_owner_memory(owner_id).model_dump(mode="json")

    def recall_size_memory(self, owner_id: str, retailer: str | None = None) -> dict[str, Any]:
        """What the ``recall_size_memory`` tool returns (private to the owner, never sent to ALDO)."""
        view = self.get_owner_memory(owner_id)
        needle = normalize_text(retailer).lower() if isinstance(retailer, str) else ""
        entries = [
            entry.model_dump(mode="json", exclude={"source"})
            for entry in view.memory
            if not needle or needle in entry.retailer.lower()
        ]
        updates = [
            update.model_dump(
                mode="json",
                include={
                    "update_id",
                    "product_name",
                    "color",
                    "original_size",
                    "new_size",
                    "reason",
                    "outcome",
                    "confidence",
                    "fit_feedback",
                },
                exclude_none=True,
            )
            for update in view.updates
        ]
        return {
            "typical_size": view.typical_size,
            "size_system": view.size_system,
            "entries": entries,
            "aldo_updates": updates,
            "note": MEMORY_NOTE,
        }

    # ------------------------------------------------------------------------ write-backs

    def apply_update(self, raw_update: object, *, emit_a2a: bool | None = None) -> dict[str, Any]:
        """Apply one ALDO write-back, idempotent on ``update_id``.

        A conditional put on ``UPD#{update_id}`` makes replays no-ops (``duplicate: true``). Updates
        of the same order are then resolved: a non-provisional update supersedes provisional ones;
        otherwise the latest provisional wins. Returns ``{"update_id", "applied", "duplicate",
        "provisional", "update"}``.

        ``emit_a2a`` (default: env ``PERSONAL_AGENT_EMIT_A2A``, off) additionally writes
        ``memory_updated`` to the owner's A2A stream. It is off because ALDO's outbox emits that event
        exactly once itself (contracts.md F.5); a second emitter would duplicate it.
        """
        update = validate_update(raw_update)
        owner_id = update.owner_ref
        if self.get_owner(owner_id) is None:
            raise DomainError(ErrorCode.NOT_FOUND, "unknown owner for this memory update")
        pk = owner_pk(owner_id)
        item = {
            "pk": pk,
            "sk": _sk_upd(update.update_id),
            "update_id": update.update_id,
            "owner_id": owner_id,
            "order_id": update.order_id,
            "style_id": update.style_id,
            "product_name": update.product_name,
            "color": update.color,
            "original_size": update.original_size,
            "new_size": update.new_size,
            "reason": update.reason,
            "outcome": update.outcome,
            "provisional": update.provisional,
            "source": update.source,
            "ts": clock.iso(clock.parse_iso(update.ts)),
            "applied_at": clock.iso(clock.now_utc()),
            "status": "active",
        }
        if update.fit_feedback:
            item["fit_feedback"] = update.fit_feedback
        try:
            self._put(item, if_absent=True)
        except ConditionFailed:
            existing = self._get(pk, _sk_upd(update.update_id)) or item
            return self._update_result(existing, applied=False)
        self._resolve_order_updates(owner_id, update.order_id)
        stored = self._get(pk, _sk_upd(update.update_id)) or item
        result = self._update_result(stored, applied=True)
        if settings.emit_a2a_enabled() if emit_a2a is None else emit_a2a:
            public = result["update"]
            a2a.emit_a2a(
                owner_id,
                "memory_updated",
                {
                    key: public[key]
                    for key in (
                        "update_id",
                        "style_id",
                        "product_name",
                        "color",
                        "original_size",
                        "new_size",
                        "reason",
                        "outcome",
                        "provisional",
                        "fit_feedback",
                    )
                    if public.get(key) is not None
                },
                actor="personal_agent",
            )
        return result

    def _update_result(self, item: Mapping[str, Any], *, applied: bool) -> dict[str, Any]:
        update, _ts = self._applied(item)
        return {
            "update_id": update.update_id,
            "applied": applied,
            "duplicate": not applied,
            "provisional": update.provisional,
            "update": update.model_dump(mode="json"),
        }

    def _resolve_order_updates(self, owner_id: str, order_id: str) -> None:
        """Mark all but the winning update of one order as superseded (idempotent, order-free)."""
        pk = owner_pk(owner_id)
        items = [
            item
            for item in self._query(pk, begins_with="UPD#")
            if item.get("order_id") == order_id
        ]
        if len(items) < 2:
            return

        def order(item: dict[str, Any]) -> tuple[str, str, str]:
            return (item.get("ts", ""), item.get("applied_at", ""), item.get("update_id", ""))

        finals = [item for item in items if not item.get("provisional")]
        winner = max(finals or items, key=order)
        names = {"#status": "status", "#by": "superseded_by"}
        for item in items:
            if item["update_id"] == winner["update_id"]:
                if item.get("status") != "active":
                    self._update(
                        pk,
                        item["sk"],
                        "SET #status = :active REMOVE #by",
                        names=names,
                        values={":active": "active"},
                    )
            elif item.get("status") != "superseded" or item.get("superseded_by") != winner["update_id"]:
                self._update(
                    pk,
                    item["sk"],
                    "SET #status = :superseded, #by = :winner",
                    names=names,
                    values={":superseded": "superseded", ":winner": winner["update_id"]},
                )

    # ----------------------------------------------------------------------- conversations

    def create_conversation(self, owner_id: str, conv_id: str | None = None) -> str:
        settings.require_id(owner_id, "owner_id")
        conv_id = conv_id or new_conv_id()
        settings.require_id(conv_id, "conv_id")
        try:
            self._put(
                {
                    "pk": owner_pk(owner_id),
                    "sk": _sk_conv(conv_id),
                    "conv_id": conv_id,
                    "owner_id": owner_id,
                    "messages": "[]",
                    "created_at": clock.iso(clock.now_utc()),
                },
                if_absent=True,
            )
        except ConditionFailed:
            pass  # idempotent: the conversation already exists
        return conv_id

    def get_conversation(self, owner_id: str, conv_id: str) -> dict[str, Any]:
        """Conversation meta (no messages). Another owner's conversation is simply NOT_FOUND.

        ``mode`` is ``bot`` (the ALDO bot purchase workflow answers) or ``returns`` (the owner's
        messages go straight to the return workflow); in ``returns`` mode ``returns_order_id`` is
        the order being returned, ``returns_conv_id`` the ALDO-side conversation once it exists,
        ``handoff_text`` the owner message that started the hand-off until it was forwarded
        (``handoff_forwarded``) and ``memory_context`` the trusted context for the return run.
        """
        settings.require_id(conv_id, "conv_id")
        item = self._get(owner_pk(owner_id), _sk_conv(conv_id))
        if item is None:
            raise DomainError(ErrorCode.NOT_FOUND, "conversation not found")
        mode = item.get("mode") if item.get("mode") in CONVERSATION_MODES else MODE_BOT
        return {
            "conv_id": item["conv_id"],
            "created_at": item.get("created_at"),
            "active_run_id": item.get("active_run_id"),
            "active_run_expires_at": item.get("active_run_expires_at"),
            "mode": mode,
            "returns_order_id": item.get("returns_order_id") if mode == MODE_RETURNS else None,
            "returns_conv_id": item.get("returns_conv_id") if mode == MODE_RETURNS else None,
            "handoff_text": item.get("handoff_text") if mode == MODE_RETURNS else None,
            "handoff_forwarded": bool(item.get("handoff_forwarded")) if mode == MODE_RETURNS else False,
            "memory_context": _json_object(item.get("memory_context_json")) if mode == MODE_RETURNS else None,
        }

    # ------------------------------------------------------------------ conversation mode

    def set_returns_mode(
        self,
        owner_id: str,
        conv_id: str,
        order_id: str | None,
        *,
        handoff_text: str,
        memory_context: Mapping[str, Any] | None = None,
    ) -> bool:
        """bot -> returns, for ``order_id`` when the bot knew it; stores the owner's triggering message.

        ``order_id`` may be None: the return workflow then looks up the owner's recent delivered
        orders itself. True when the mode (or the order) changed. False when the conversation is
        ALREADY in returns mode for this order (or, with no order given, in returns mode at all),
        so a repeated hand-off is a harmless no-op that keeps the first hand-off's text and
        context. A hand-off for another order starts over (the ALDO-side conversation of the
        earlier order is forgotten). NOT_FOUND for a missing or foreign conversation.
        """
        if order_id is not None:
            settings.require_id(order_id, "order_id")
        context_json = json.dumps(dict(memory_context), ensure_ascii=False, separators=(",", ":")) if memory_context else ""
        sets = "#mode = :returns, #text = :text, #fwd = :no, #mc = :mc, #at = :now"
        values: dict[str, Any] = {
            ":returns": MODE_RETURNS,
            ":text": handoff_text,
            ":no": False,
            ":mc": context_json,
            ":now": clock.iso(clock.now_utc()),
        }
        changed = "attribute_not_exists(#mode) OR #mode <> :returns"
        if order_id is None:
            expression = f"SET {sets} REMOVE #rconv, #order"
        else:
            expression = f"SET {sets}, #order = :order REMOVE #rconv"
            values[":order"] = order_id
            changed += " OR attribute_not_exists(#order) OR #order <> :order"
        try:
            self._update(
                owner_pk(owner_id),
                _sk_conv(conv_id),
                expression,
                condition=f"attribute_exists(#pk) AND ({changed})",
                names={
                    "#pk": "pk",
                    "#mode": "mode",
                    "#order": "returns_order_id",
                    "#text": "handoff_text",
                    "#fwd": "handoff_forwarded",
                    "#mc": "memory_context_json",
                    "#at": "handoff_at",
                    "#rconv": "returns_conv_id",
                },
                values=values,
            )
        except ConditionFailed:
            self.get_conversation(owner_id, conv_id)  # NOT_FOUND when the conversation is missing
            return False
        return True

    def attach_returns_conversation(self, owner_id: str, conv_id: str, returns_conv_id: str) -> str | None:
        """Remember the ALDO-side conversation of the hand-off, once.

        Returns the id that is stored: ``returns_conv_id`` for the first caller, the earlier
        winner's id for a concurrent caller, None when the conversation is no longer in returns
        mode.
        """
        settings.require_id(returns_conv_id, "returns_conv_id")
        try:
            self._update(
                owner_pk(owner_id),
                _sk_conv(conv_id),
                "SET #rconv = :conv",
                condition="attribute_exists(#pk) AND #mode = :returns AND attribute_not_exists(#rconv)",
                names={"#pk": "pk", "#mode": "mode", "#rconv": "returns_conv_id"},
                values={":returns": MODE_RETURNS, ":conv": returns_conv_id},
            )
        except ConditionFailed:
            current = self.get_conversation(owner_id, conv_id)
            return current["returns_conv_id"] if current["mode"] == MODE_RETURNS else None
        return returns_conv_id

    def forget_returns_conversation(self, owner_id: str, conv_id: str, returns_conv_id: str) -> None:
        """Drop a stored ALDO-side conversation id that no longer exists (for example after a reset)."""
        with contextlib.suppress(ConditionFailed):
            self._update(
                owner_pk(owner_id),
                _sk_conv(conv_id),
                "REMOVE #rconv",
                condition="#rconv = :conv",
                names={"#rconv": "returns_conv_id"},
                values={":conv": returns_conv_id},
            )

    def mark_handoff_forwarded(self, owner_id: str, conv_id: str) -> None:
        """The stored triggering text has been sent to the return workflow; never send it again."""
        with contextlib.suppress(ConditionFailed):
            self._update(
                owner_pk(owner_id),
                _sk_conv(conv_id),
                "SET #fwd = :yes",
                condition="attribute_exists(#pk) AND #mode = :returns",
                names={"#pk": "pk", "#fwd": "handoff_forwarded", "#mode": "mode"},
                values={":yes": True, ":returns": MODE_RETURNS},
            )

    def set_bot_mode(
        self, owner_id: str, conv_id: str, *, expected_order_id: str | None = None
    ) -> dict[str, Any] | None:
        """returns -> bot (handback). Returns ``{"returns_order_id", "returns_conv_id"}`` as they were.

        None when the conversation was not in returns mode (or, with ``expected_order_id``, was
        returning another order), so a late or repeated handback changes nothing.
        """
        condition = "attribute_exists(#pk) AND #mode = :returns"
        values: dict[str, Any] = {":bot": MODE_BOT, ":returns": MODE_RETURNS}
        if expected_order_id is not None:
            condition += " AND #order = :expected"
            values[":expected"] = expected_order_id
        try:
            old = self._update(
                owner_pk(owner_id),
                _sk_conv(conv_id),
                "SET #mode = :bot REMOVE #order, #rconv, #text, #fwd, #mc, #at",
                condition=condition,
                names={
                    "#pk": "pk",
                    "#mode": "mode",
                    "#order": "returns_order_id",
                    "#rconv": "returns_conv_id",
                    "#text": "handoff_text",
                    "#fwd": "handoff_forwarded",
                    "#mc": "memory_context_json",
                    "#at": "handoff_at",
                },
                values=values,
                return_values="ALL_OLD",
            )
        except ConditionFailed:
            return None
        return {"returns_order_id": old.get("returns_order_id"), "returns_conv_id": old.get("returns_conv_id")}

    def load_messages(self, owner_id: str, conv_id: str) -> list[dict[str, Any]]:
        item = self._get(owner_pk(owner_id), _sk_conv(conv_id))
        if item is None:
            raise DomainError(ErrorCode.NOT_FOUND, "conversation not found")
        try:
            stored = json.loads(item.get("messages") or "[]")
        except ValueError:
            return []
        return clean_boundary(stored) if isinstance(stored, list) else []

    def save_messages(
        self, owner_id: str, conv_id: str, messages: list[dict[str, Any]]
    ) -> int:
        """Persist a Strands message list at a clean turn boundary. Returns the stored byte size.

        JSON-safe (reasoning stripped, bytes -> base64), oversized tool results truncated, oldest
        turns dropped to stay under the cap, toolUse/toolResult pairs never split. A list without
        a complete turn is not written (returns 0) so the stored history stays valid.
        """
        clean = clean_boundary(strip_reasoning(messages))
        if not clean:
            return 0
        clean = shrink_messages(clean, settings.conversation_max_bytes())
        payload = json.dumps(clean, ensure_ascii=False)
        try:
            self._update(
                owner_pk(owner_id),
                _sk_conv(conv_id),
                "SET #messages = :messages, #updated = :now",
                condition="attribute_exists(#pk)",
                names={"#messages": "messages", "#updated": "updated_at", "#pk": "pk"},
                values={":messages": payload, ":now": clock.iso(clock.now_utc())},
            )
        except ConditionFailed:
            raise DomainError(ErrorCode.NOT_FOUND, "conversation not found") from None
        return len(payload.encode("utf-8"))

    def try_acquire_lease(
        self, owner_id: str, conv_id: str, run_id: str, *, ttl_seconds: int | None = None
    ) -> bool:
        """One active run per conversation: True if ``run_id`` now holds the lease."""
        ttl = settings.lease_seconds() if ttl_seconds is None else ttl_seconds
        now = int(clock.now_utc().timestamp())
        try:
            self._update(
                owner_pk(owner_id),
                _sk_conv(conv_id),
                "SET #rid = :run, #exp = :exp",
                condition=(
                    "attribute_exists(#pk) AND (attribute_not_exists(#rid) "
                    "OR #exp <= :now OR #rid = :run)"
                ),
                names={"#rid": "active_run_id", "#exp": "active_run_expires_at", "#pk": "pk"},
                values={":run": run_id, ":exp": now + ttl, ":now": now},
            )
        except ConditionFailed:
            self.get_conversation(owner_id, conv_id)  # NOT_FOUND if the conversation is missing
            return False
        return True

    def release_lease(self, owner_id: str, conv_id: str, run_id: str) -> None:
        with contextlib.suppress(ConditionFailed):
            self._update(
                owner_pk(owner_id),
                _sk_conv(conv_id),
                "REMOVE #rid, #exp",
                condition="#rid = :run",
                names={"#rid": "active_run_id", "#exp": "active_run_expires_at"},
                values={":run": run_id},
            )

    def lease_active(self, owner_id: str, conv_id: str) -> bool:
        meta = self.get_conversation(owner_id, conv_id)
        expires = meta.get("active_run_expires_at")
        return bool(
            meta.get("active_run_id")
            and isinstance(expires, int)
            and expires > int(clock.now_utc().timestamp())
        )

    # -------------------------------------------------------------------------- runs/events

    def create_run(self, owner_id: str, conv_id: str, run_id: str) -> bool:
        """Create a queued run. False if it already exists (idempotent)."""
        settings.require_id(run_id, "run_id")
        try:
            self._put(
                {
                    "pk": owner_pk(owner_id),
                    "sk": _sk_run(run_id),
                    "run_id": run_id,
                    "conv_id": conv_id,
                    "owner_id": owner_id,
                    "status": "queued",
                    "seq": 0,
                    "created_at": clock.iso(clock.now_utc()),
                },
                if_absent=True,
            )
        except ConditionFailed:
            return False
        return True

    def get_run(self, owner_id: str, run_id: str) -> dict[str, Any] | None:
        item = self._get(owner_pk(owner_id), _sk_run(run_id))
        if item is None:
            return None
        metrics_json = item.get("metrics_json")
        return {
            "run_id": item["run_id"],
            "conv_id": item.get("conv_id"),
            "status": item["status"],
            "seq": int(item.get("seq", 0)),
            "created_at": item.get("created_at"),
            "metrics": json.loads(metrics_json) if metrics_json else None,
        }

    def claim_run(self, owner_id: str, run_id: str) -> bool:
        """queued -> running. False if the run is missing or already claimed (duplicate delivery)."""
        try:
            self._update(
                owner_pk(owner_id),
                _sk_run(run_id),
                "SET #status = :running, #started = :now",
                condition="#status = :queued",
                names={"#status": "status", "#started": "started_at"},
                values={":running": "running", ":queued": "queued", ":now": clock.iso(clock.now_utc())},
            )
        except ConditionFailed:
            return False
        return True

    def mark_started(self, owner_id: str, run_id: str) -> bool:
        """The runner's once-only guard: sets ``agent_started_at`` and moves queued -> running.

        True for the first caller; False for any later one (duplicate delivery) or for a run that
        is missing or already finished. Works whether or not a worker already ran ``claim_run``.
        """
        try:
            self._update(
                owner_pk(owner_id),
                _sk_run(run_id),
                "SET #status = :running, #agent = :now",
                condition="attribute_not_exists(#agent) AND #status IN (:queued, :running)",
                names={"#status": "status", "#agent": "agent_started_at"},
                values={
                    ":running": "running",
                    ":queued": "queued",
                    ":now": clock.iso(clock.now_utc()),
                },
            )
        except ConditionFailed:
            return False
        return True

    def finish_run(
        self, owner_id: str, run_id: str, status: Literal["completed", "error"], metrics: Mapping[str, Any]
    ) -> bool:
        """queued|running -> completed|error. The winner emits ``run_completed`` (last event)."""
        if status not in ("completed", "error"):
            raise DomainError(ErrorCode.VALIDATION_ERROR, "status must be completed or error")
        payload = {"status": status, **redact_payload(jsonable(dict(metrics)))}
        try:
            self._update(
                owner_pk(owner_id),
                _sk_run(run_id),
                "SET #status = :final, #finished = :now, #metrics = :metrics",
                condition="#status IN (:queued, :running)",
                names={"#status": "status", "#finished": "finished_at", "#metrics": "metrics_json"},
                values={
                    ":final": status,
                    ":queued": "queued",
                    ":running": "running",
                    ":now": clock.iso(clock.now_utc()),
                    ":metrics": json.dumps(payload, ensure_ascii=False),
                },
            )
        except ConditionFailed:
            return False
        self.emit(owner_id, run_id, "run_completed", payload, actor="system")
        return True

    def emit(
        self,
        owner_id: str,
        run_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        actor: str = "personal_agent",
    ) -> int:
        """Append one event: atomic ``seq`` increment on the run, then the event item."""
        if event_type not in EVENT_TYPES:
            raise DomainError(ErrorCode.VALIDATION_ERROR, f"unknown event type {event_type}")
        if actor not in ACTORS:
            raise DomainError(ErrorCode.VALIDATION_ERROR, f"unknown actor {actor}")
        cap = _STRING_CAPS.get(event_type, _DEFAULT_STRING_CAP)
        body = redact_payload(jsonable(dict(payload or {})), string_cap=cap)
        try:
            attributes = self._update(
                owner_pk(owner_id),
                _sk_run(run_id),
                "ADD #seq :one",
                condition="attribute_exists(#pk)",
                names={"#seq": "seq", "#pk": "pk"},
                values={":one": 1},
                return_values="UPDATED_NEW",
            )
        except ConditionFailed:
            raise DomainError(ErrorCode.NOT_FOUND, "run not found") from None
        seq = int(attributes["seq"])
        self._put(
            {
                "pk": owner_pk(owner_id),
                "sk": _sk_evt(run_id, seq),
                "run_id": run_id,
                "seq": seq,
                "type": event_type,
                "actor": actor,
                "ts": clock.iso(clock.now_utc()),
                "payload_json": json.dumps(body, ensure_ascii=False),
            }
        )
        return seq

    def list_events(
        self, owner_id: str, run_id: str, after: int = 0, limit: int = 100
    ) -> dict[str, Any]:
        """Events with ``seq > after`` as ``{run_id, status, events, next_after}``.

        Only the contiguous prefix is returned (a gap means a writer is mid-append), so a polling
        client using ``next_after`` as its cursor can never skip an event.
        """
        run = self.get_run(owner_id, run_id)
        if run is None:
            raise DomainError(ErrorCode.NOT_FOUND, "run not found")
        after = max(0, int(after))
        items = self._query(
            owner_pk(owner_id),
            between=(_sk_evt(run_id, after + 1), _sk_evt(run_id, 999999)),
            limit=max(1, min(int(limit), 500)),
        )
        events: list[dict[str, Any]] = []
        expected = after + 1
        for item in items:
            if int(item["seq"]) != expected:
                break
            events.append(
                {
                    "seq": expected,
                    "type": item["type"],
                    "ts": item["ts"],
                    "actor": item.get("actor", "personal_agent"),
                    "payload": json.loads(item.get("payload_json") or "{}"),
                }
            )
            expected += 1
        return {
            "run_id": run_id,
            "status": run["status"],
            "events": events,
            "next_after": events[-1]["seq"] if events else after,
        }


# --------------------------------------------------------------------------- public views


def _natural_key(identifier: str) -> tuple[str, int]:
    match = re.search(r"(\d+)$", identifier)
    return (identifier[: match.start()] if match else identifier, int(match.group(1)) if match else 0)


def _json_object(raw: object) -> dict[str, Any] | None:
    """The object stored as a JSON string, or None when it is absent, empty or not an object."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _public_memory(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "memory_id": item["memory_id"],
        "retailer": item["retailer"],
        "item": item["item"],
        "size": item["size"],
        "outcome": item["outcome"],
        "source": item.get("source", "synthetic"),
        "color": item.get("color"),
    }


# ------------------------------------------------------------------------ default instance

_default_repo: MemoryRepo | None = None


def get_repo() -> MemoryRepo:
    """Process-wide repository for ``PERSONAL_AGENT_TABLE_NAME`` (created lazily)."""
    global _default_repo
    if _default_repo is None:
        _default_repo = MemoryRepo()
    return _default_repo


def set_repo(repo: MemoryRepo | None) -> None:
    """Swap (or clear with None) the process-wide repository; used by tests and local runs."""
    global _default_repo
    _default_repo = repo


# Module-level API (contracts.md F.9 names; the aliases keep earlier callers working).


def seed_memory(path: Path | str | None = None) -> int:
    """Write the fixture memory; returns the number of owners seeded."""
    return get_repo().seed(path)["owners"]


def reset_memory(path: Path | str | None = None) -> dict[str, int]:
    """Wipe the personal-agent table and restore the fixture: ``{"deleted", "owners", "entries"}``."""
    return get_repo().reset(path)


def reset() -> dict[str, int]:
    """Alias of ``reset_memory`` (the API's demo reset calls ``memory.reset()``)."""
    return reset_memory()


def seed(path: Path | str | None = None) -> dict[str, int]:
    return get_repo().seed(path)


def get_owner_memory(owner_id: str) -> OwnerMemory:
    return get_repo().get_owner_memory(owner_id)


def list_memory(owner_id: str) -> dict[str, Any]:
    return get_repo().list_memory(owner_id)


def recall_size_memory(owner_id: str, retailer: str | None = None) -> dict[str, Any]:
    return get_repo().recall_size_memory(owner_id, retailer)


def apply_update(update: object) -> dict[str, Any]:
    """Apply an ALDO write-back (the HMAC route and the ``local`` ``PersonalAgentClient`` handler)."""
    return get_repo().apply_update(update)


#: Name used by contracts.md F.5/F.9 for the same function.
apply_memory_update = apply_update


def create_conversation(owner_id: str) -> str:
    return get_repo().create_conversation(owner_id)


def create_run(owner_id: str, conv_id: str, run_id: str) -> None:
    get_repo().create_run(owner_id, conv_id, run_id)


def claim_run(owner_id: str, run_id: str) -> bool:
    return get_repo().claim_run(owner_id, run_id)


def mark_started(owner_id: str, run_id: str) -> bool:
    return get_repo().mark_started(owner_id, run_id)


def finish_run(owner_id: str, run_id: str, status: Literal["completed", "error"], metrics: Mapping[str, Any]) -> bool:
    return get_repo().finish_run(owner_id, run_id, status, metrics)


def try_acquire_lease(owner_id: str, conv_id: str, run_id: str, ttl_seconds: int | None = None) -> bool:
    return get_repo().try_acquire_lease(owner_id, conv_id, run_id, ttl_seconds=ttl_seconds)


def release_lease(owner_id: str, conv_id: str, run_id: str) -> None:
    get_repo().release_lease(owner_id, conv_id, run_id)


def emit_run_event(
    owner_id: str, run_id: str, event_type: str, payload: Mapping[str, Any], *, actor: str = "personal_agent"
) -> int:
    return get_repo().emit(owner_id, run_id, event_type, payload, actor=actor)


def list_run_events(owner_id: str, run_id: str, after: int = 0, limit: int = 100) -> dict[str, Any]:
    """Event page of an ALDO bot run (``GET /api/runs/{id}/events`` for ``PRUN-`` ids)."""
    return get_repo().list_events(owner_id, run_id, after, limit)
