"""Version-one event envelope; payload schemas belong to their owning plans."""

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, JsonValue, field_serializer, field_validator

from statistics_agent.contracts.common import Contract, PositiveSequence

SCHEMA_VERSION = "1.0"
MAX_EVENT_DATA_BYTES = 16_384
MAX_EVENT_DATA_DEPTH = 16


class EventType(StrEnum):
    SESSION_STARTED = "session.started"
    TOOL_CALL_REQUESTED = "tool_call.requested"
    TOOL_CALL_COMPLETED = "tool_call.completed"
    TOOL_CALL_FAILED = "tool_call.failed"
    MODEL_CALL_COMPLETED = "model_call.completed"
    MODEL_CALL_FAILED = "model_call.failed"
    SESSION_ENDED = "session.ended"


def _check_data(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_EVENT_DATA_DEPTH:
            raise ValueError("Event data exceeds its nesting limit")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("Event data object keys must be strings")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
        elif item is not None and type(item) not in {str, int, float, bool}:
            raise ValueError("Event data must contain JSON values only")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        size = len(encoded.encode("utf-8"))
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("Event data must contain finite, UTF-8 serializable JSON") from None
    if size > MAX_EVENT_DATA_BYTES:
        raise ValueError("Event data exceeds its byte limit; use an artifact reference")
    return value


class EventEnvelope(Contract):
    """Stable event identity with bounded evidence, not a tool execution request.

    Attribute replacement is forbidden; nested data remains normal JSON.
    Validate it again on serialization so later mutations cannot bypass bounds.
    Producers must redact secrets before constructing this evidence envelope.
    """

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    event_id: UUID
    event_type: EventType
    occurred_at: AwareDatetime
    run_id: UUID
    session_id: UUID
    sequence: PositiveSequence
    request_id: UUID | None = None
    data: dict[str, JsonValue]

    @field_validator("occurred_at", mode="before")
    @classmethod
    def require_explicit_timestamp(cls, value: object) -> object:
        if not isinstance(value, (str, datetime)):
            raise ValueError("Timestamp must be an aware datetime or ISO datetime string")
        return value

    @field_validator("occurred_at")
    @classmethod
    def normalize_utc(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("data")
    @classmethod
    def validate_data(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _check_data(value)

    @field_serializer("data")
    def serialize_data(self, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _check_data(value)
