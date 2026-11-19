"""Synthetic encryption keys without database or application fixtures."""

from cryptography.fernet import Fernet
from pydantic import SecretStr

from statistics_agent.core.auth_secrets import AuthSecrets, EncryptionKey


def keyring(active="first", keys=None):
    keys = keys or {"first": Fernet.generate_key().decode()}
    return AuthSecrets(
        client_secret=SecretStr("synthetic-client-secret"),
        active_key_id=active,
        keys=tuple(
            EncryptionKey(key_id=name, material=SecretStr(value)) for name, value in keys.items()
        ),
    )
