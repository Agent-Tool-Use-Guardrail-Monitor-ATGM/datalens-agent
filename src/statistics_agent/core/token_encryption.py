"""Authenticated, context-bound secret envelopes with explicit read-key rotation."""

import json
from dataclasses import dataclass
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from statistics_agent.core.auth_secrets import AuthSecrets


class SecretEnvelopeError(ValueError):
    """A secret cannot be trusted; never attach the original input or exception."""


@dataclass(frozen=True)
class SecretContext:
    row_id: UUID
    issuer: str
    subject: str
    kind: str


class TokenEncryption:
    """The caller supplies trusted context; ciphertext cannot select its own owner."""

    def __init__(self, secrets: AuthSecrets):
        self._active = secrets.active_key_id
        self._keys = {
            key.key_id: Fernet(key.material.get_secret_value().encode()) for key in secrets.keys
        }
        if self._active not in self._keys:
            raise SecretEnvelopeError("Invalid encryption keyring")

    def seal(self, value: SecretStr, context: SecretContext) -> str:
        raw = value.get_secret_value()
        if not raw or len(raw.encode()) > 32768:
            raise SecretEnvelopeError("Invalid secret size")
        payload = json.dumps(
            [1, str(context.row_id), context.issuer, context.subject, context.kind, raw],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()
        encrypted = self._keys[self._active].encrypt(payload).decode()
        return f"{self._active}:{encrypted}"

    def open(self, envelope: str, context: SecretContext) -> SecretStr:
        try:
            if len(envelope) > 300000:
                raise ValueError
            key_id, ciphertext = envelope.split(":", 1)
            payload = json.loads(self._keys[key_id].decrypt(ciphertext.encode()))
            expected = [1, str(context.row_id), context.issuer, context.subject, context.kind]
            if (
                not isinstance(payload, list)
                or len(payload) != 6
                or payload[:5] != expected
                or not isinstance(payload[5], str)
                or not payload[5]
                or len(payload[5].encode()) > 32768
            ):
                raise ValueError
            return SecretStr(payload[5])
        except (InvalidToken, ValueError, KeyError, UnicodeError, TypeError, RecursionError):
            pass
        raise SecretEnvelopeError("Stored credentials cannot be decrypted")
