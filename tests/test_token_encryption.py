"""Real authenticated encryption; synthetic credentials and keys only."""

from dataclasses import replace
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from statistics_agent.core.auth_secrets import AuthSecrets, EncryptionKey
from statistics_agent.core.token_encryption import (
    SecretContext,
    SecretEnvelopeError,
    TokenEncryption,
)


def keyring(active="first", keys=None):
    keys = keys or {"first": Fernet.generate_key().decode()}
    return AuthSecrets(
        client_secret=SecretStr("synthetic-client-secret"),
        active_key_id=active,
        keys=tuple(
            EncryptionKey(key_id=name, material=SecretStr(value)) for name, value in keys.items()
        ),
    )


def test_roundtrip_restart_and_randomized_ciphertext():
    ring = keyring()
    cipher = TokenEncryption(ring)
    context = SecretContext(uuid4(), "https://issuer.example/realms/test", "owner", "access")
    secret = SecretStr("private-synthetic-token")
    first, second = cipher.seal(secret, context), cipher.seal(secret, context)
    assert first != second
    assert secret.get_secret_value() not in first
    assert TokenEncryption(ring).open(first, context) == secret


@pytest.mark.parametrize("field", ["row_id", "issuer", "subject", "kind"])
def test_cross_session_owner_issuer_or_token_kind_substitution_is_denied(field):
    cipher = TokenEncryption(keyring())
    context = SecretContext(uuid4(), "https://issuer.example", "owner", "access")
    ciphertext = cipher.seal(SecretStr("synthetic-token"), context)
    altered = replace(context, **{field: uuid4() if field == "row_id" else "different"})
    with pytest.raises(SecretEnvelopeError, match="cannot be decrypted") as caught:
        cipher.open(ciphertext, altered)
    assert caught.value.__context__ is None


@pytest.mark.parametrize("damage", ["tampered", "missing_key", "wrong_key", "malformed"])
def test_corrupt_unknown_or_wrong_keys_fail_with_safe_diagnostics(damage):
    cipher = TokenEncryption(keyring())
    context = SecretContext(uuid4(), "https://issuer.example", "owner", "refresh")
    ciphertext = cipher.seal(SecretStr("private-synthetic-token"), context)
    if damage == "tampered":
        position = len(ciphertext) // 2
        ciphertext = (
            ciphertext[:position]
            + ("A" if ciphertext[position] != "A" else "B")
            + ciphertext[position + 1 :]
        )
    elif damage == "missing_key":
        ciphertext = ciphertext.replace("first:", "unknown:", 1)
    elif damage == "wrong_key":
        cipher = TokenEncryption(keyring())
    else:
        ciphertext = "malformed-private-input"
    with pytest.raises(SecretEnvelopeError) as caught:
        cipher.open(ciphertext, context)
    assert str(caught.value) == "Stored credentials cannot be decrypted"
    assert caught.value.__context__ is None


def test_rotation_reads_previous_key_and_writes_active_key_only():
    first, second = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    context = SecretContext(uuid4(), "https://issuer.example", "owner", "access")
    secret = SecretStr("synthetic-token")
    old = TokenEncryption(keyring(keys={"first": first})).seal(secret, context)
    rotated = TokenEncryption(keyring("second", {"first": first, "second": second}))
    assert rotated.open(old, context) == secret
    assert rotated.seal(secret, context).startswith("second:")
    with pytest.raises(SecretEnvelopeError):
        TokenEncryption(keyring("second", {"second": second})).open(old, context)


@pytest.mark.parametrize("value", ["", "x" * 32769], ids=["empty", "oversized"])
def test_secret_size_is_bounded(value):
    with pytest.raises(SecretEnvelopeError, match="Invalid secret size"):
        TokenEncryption(keyring()).seal(
            SecretStr(value), SecretContext(uuid4(), "https://issuer.example", "owner", "access")
        )
