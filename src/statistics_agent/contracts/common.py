"""Shared immutable references and validation rules, without infrastructure."""

from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

type ShortReference = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$"),
]


class Contract(BaseModel):
    """Reject unknown top-level fields and protect validated attribute values."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        allow_inf_nan=False,
        revalidate_instances="always",
    )


class AuthenticatedPrincipal(Contract):
    """Identity reference from verified claims; this model does not authenticate."""

    issuer: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2048)]
    subject: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=255)]

    @field_validator("issuer")
    @classmethod
    def validate_issuer(cls, value: str) -> str:
        try:
            parsed = urlsplit(value)
            valid_port = parsed.port is None or 1 <= parsed.port <= 65535
        except ValueError:
            raise ValueError("Issuer must be an HTTP(S) identity URL") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not valid_port
            or any(character.isspace() or not character.isprintable() for character in value)
        ):
            raise ValueError("Issuer must be an HTTP(S) identity URL without credentials or query")
        # Preserve the verified issuer exactly; normalization could change identity.
        return value

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Subject must not be blank")
        return value


class AuthSessionReference(Contract):
    """Internal reference, never the browser cookie or OAuth token."""

    auth_session_ref: UUID


class ConfigurationReference(Contract):
    """Non-secret identity/version for a later immutable configuration snapshot."""

    config_id: UUID
    version: ShortReference


class ArtifactReference(Contract):
    """Opaque application artifact identity, not an arbitrary path or URL."""

    artifact_id: UUID
    sha256: Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]
    media_type: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128)]


type PositiveSequence = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
