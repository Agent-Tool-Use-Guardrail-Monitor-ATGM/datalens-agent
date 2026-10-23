"""HTTP wire contracts and bounded request correlation."""

from typing import Annotated
from uuid import UUID, uuid4

from pydantic import Field, StringConstraints

from statistics_agent.contracts.common import Contract

type ErrorCode = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$"),
]
type FieldLocation = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]
type LocationPart = FieldLocation | Annotated[int, Field(strict=True, ge=0)]


class ValidationDetail(Contract):
    """Trusted schema location and code; never rejected values or raw exceptions."""

    location: Annotated[tuple[LocationPart, ...], Field(min_length=1, max_length=8)]
    code: ErrorCode


class ErrorEnvelope(Contract):
    """Callers must supply server-authored safe messages and schema field names."""

    code: ErrorCode
    message: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=256)]
    request_id: UUID
    details: Annotated[tuple[ValidationDetail, ...], Field(max_length=20)] = ()


def resolve_request_id(header: str | None) -> UUID:
    """Accept a canonical bounded UUID header or generate a fresh request UUID."""
    if isinstance(header, str) and len(header) == 36:
        try:
            candidate = UUID(header)
        except ValueError:
            pass
        else:
            if str(candidate) == header.lower():
                return candidate
    return uuid4()
