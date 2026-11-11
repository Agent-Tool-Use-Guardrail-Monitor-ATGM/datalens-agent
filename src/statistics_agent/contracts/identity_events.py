"""Strict decoder for the identity service's v1 wire contract."""

import json
from datetime import datetime
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from statistics_agent.contracts.common import AuthenticatedPrincipal

_SCHEMA = Path(__file__).with_name("identity-events.v1.schema.json")
_VALIDATOR = Draft202012Validator(
    json.loads(_SCHEMA.read_text(encoding="utf-8")), format_checker=FormatChecker()
)


class IdentityEventError(ValueError):
    """Safe error suitable for rejecting an identity delivery."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def _invalid_constant(_value):
    raise ValueError("Non-finite number")


def decode_identity_event(body: bytes, expected_issuer: str) -> dict:
    if not isinstance(body, bytes) or not 0 < len(body) <= 16_384:
        raise IdentityEventError("Invalid identity event size or encoding")
    try:
        event = json.loads(
            body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
        _VALIDATOR.validate(event)
        if type(event["schema_version"]) is not int or any(
            type(event["data"][field]) is not int
            for field in ("profile_revision", "security_revision", "tokens_issued_at_or_before")
            if field in event["data"]
        ):
            raise ValueError("Identity revisions and cutoffs require integer JSON values")
        datetime.fromisoformat(event["occurred_at"])
        AuthenticatedPrincipal(issuer=event["issuer"], subject=event["subject"])
        if event["issuer"] != expected_issuer:
            raise ValueError("Untrusted issuer")
    except (ValueError, ValidationError, RecursionError):
        raise IdentityEventError("Invalid identity event contract") from None
    return event
