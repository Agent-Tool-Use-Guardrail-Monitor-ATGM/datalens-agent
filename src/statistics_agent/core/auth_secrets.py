"""Bounded explicit secret loading and keyring validation; no token storage yet."""

import base64
import json
import re

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, ValidationError

from statistics_agent.contracts.common import Contract, ShortReference
from statistics_agent.core.auth import AuthSettings
from statistics_agent.core.settings import ConfigurationError


class EncryptionKey(Contract):
    key_id: ShortReference
    material: SecretStr = Field(exclude=True, repr=False)


class AuthSecrets(Contract):
    client_secret: SecretStr = Field(exclude=True, repr=False)
    active_key_id: ShortReference
    keys: tuple[EncryptionKey, ...] = Field(exclude=True, repr=False)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate keyring field")
        result[key] = value
    return result


def load_auth_secrets(settings: AuthSettings) -> AuthSecrets:
    """Explicit call only; never include paths, contents or raw errors in diagnostics."""
    try:
        with settings.client_secret_file.open("rb") as source:
            raw_client = source.read(4099)
        if len(raw_client) > 4098:
            raise ValueError("Client secret is too large")
        # Allow a terminal newline, but never silently strip whitespace from a credential.
        client = raw_client.decode("utf-8").removesuffix("\n").removesuffix("\r")
        if not re.fullmatch(r"[A-Za-z0-9_.~+\-]{16,4096}", client) or client.upper() in {
            "REPLACE_ME",
            "CHANGE_ME",
            "CHANGEME",
            "REPLACE_WITH_CLIENT_SECRET",
        }:
            raise ValueError("Invalid client secret")
        with settings.encryption_keys_file.open("rb") as source:
            raw_keys = source.read(65537)
        if len(raw_keys) > 65536:
            raise ValueError("Keyring is too large")
        ring = json.loads(raw_keys.decode("utf-8"), object_pairs_hook=unique_object)
        if not isinstance(ring, dict) or set(ring) != {"active_key_id", "keys"}:
            raise ValueError("Invalid keyring structure")
        keys = ring["keys"]
        if not isinstance(keys, dict) or not 1 <= len(keys) <= 5:
            raise ValueError("Invalid key count")
        if not isinstance(ring["active_key_id"], str) or ring["active_key_id"] not in keys:
            raise ValueError("Missing active key")
        validated = []
        seen = set()
        for key_id, value in keys.items():
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}=", value):
                raise ValueError("Invalid encryption key")
            decoded = base64.urlsafe_b64decode(value)
            if base64.urlsafe_b64encode(decoded).decode() != value or value in seen:
                raise ValueError("Noncanonical or duplicate key")
            Fernet(value.encode())
            seen.add(value)
            validated.append(EncryptionKey(key_id=key_id, material=SecretStr(value)))
        return AuthSecrets(
            client_secret=SecretStr(client),
            active_key_id=ring["active_key_id"],
            keys=tuple(validated),
        )
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError, ValidationError):
        pass
    raise ConfigurationError("Invalid authentication secrets")
