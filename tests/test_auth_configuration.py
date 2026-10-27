"""Authentication policy, trust-boundary and secret-disclosure regressions."""

import json
import socket
import traceback
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from statistics_agent.contracts.auth import AuthFailure, AuthFailureCode, AuthorizedPrincipal
from statistics_agent.core.auth import AuthSettings, load_auth_settings
from statistics_agent.core.auth_secrets import load_auth_secrets
from statistics_agent.core.settings import ConfigurationError

SECRET = "synthetic-client-secret-never-log-this"


@pytest.fixture(autouse=True)
def auth_environment(monkeypatch, tmp_path):
    for name, field in AuthSettings.model_fields.items():
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(str(field.validation_alias), raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OIDC_CLIENT_SECRET_FILE", str(tmp_path / "client.txt"))
    monkeypatch.setenv("AUTH_TOKEN_ENCRYPTION_KEYS_FILE", str(tmp_path / "keys.json"))


def test_configuration_is_explicit_and_does_not_read_secrets_or_connect(monkeypatch):
    Path(".env").write_text(
        "OIDC_ISSUER=https://ignored.invalid/realms/ignored\n", encoding="utf-8"
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("Infrastructure access during configuration")

    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    settings = load_auth_settings()
    assert settings.issuer == "http://localhost:8081/realms/datalens"
    assert settings.callback_url == "http://localhost:8000/auth/callback"
    assert settings.cookie_secure is False
    assert settings.session_cookie_name == "agent-session-dev"
    assert settings.allowed_algorithms == ("RS256",)
    assert "client_secret_file" not in settings.model_dump()
    assert "client.txt" not in repr(settings)


def test_explicit_dotenv_and_process_precedence(monkeypatch):
    Path("auth.env").write_text("OIDC_CLIENT_ID=file-client\n", encoding="utf-8")
    monkeypatch.setenv("OIDC_CLIENT_ID", "environment-client")
    assert load_auth_settings(env_file="auth.env").client_id == "environment-client"
    monkeypatch.delenv("OIDC_CLIENT_ID")
    assert load_auth_settings(env_file="auth.env").client_id == "file-client"
    with pytest.raises(ConfigurationError):
        load_auth_settings(env_file="not-present.env")


@pytest.mark.parametrize(
    "name,value",
    [
        ("OIDC_ISSUER", "http://remote.invalid/realms/datalens"),
        ("OIDC_ISSUER", "https://user:" + SECRET + "@identity.invalid/realms/datalens"),
        ("OIDC_ISSUER", "https://identity.invalid/realms/datalens?key=" + SECRET),
        ("OIDC_ISSUER", "http://LOCALHOST:8081/realms/datalens"),
        ("OIDC_ISSUER", "http://localhost:0/realms/datalens"),
        ("OIDC_ISSUER", "http://localhost:99999/realms/datalens"),
        ("OIDC_ISSUER", "https://bad_host.invalid/realms/datalens"),
        ("OIDC_ISSUER", "https://identity.invalid/realms/../admin"),
        ("OIDC_ISSUER", "https://identity.invalid/realms/%2e%2e"),
        ("OIDC_ISSUER", "https://identity.invalid/other"),
        ("OIDC_ISSUER", "https://identity.invalid/realms/datalens/"),
        ("AUTH_PUBLIC_ORIGIN", "https://agent.invalid/path"),
        ("AUTH_PUBLIC_ORIGIN", "https://agent.invalid/"),
        ("AUTH_PUBLIC_ORIGIN", "https://agent.invalid:443"),
        ("AUTH_CALLBACK_URL", "http://other.invalid/auth/callback"),
        ("AUTH_CALLBACK_URL", "http://localhost:8000/other"),
        ("AUTH_COOKIE_SECURE", "true"),
        ("AUTH_COOKIE_SAMESITE", "none"),
        ("OIDC_ALLOWED_ALGORITHMS", '["HS256"]'),
        ("OIDC_ALLOWED_ALGORITHMS", "[]"),
        ("AUTH_HTTP_READ_SECONDS", "nan"),
        ("AUTH_HTTP_DEADLINE_SECONDS", "1"),
        ("AUTH_SESSION_IDLE_SECONDS", "1801"),
        ("AUTH_SESSION_MAX_SECONDS", "60"),
        ("AUTH_LOGIN_ATTEMPT_SECONDS", "301"),
        ("OIDC_CLIENT_SECRET_FILE", ""),
        ("APP_ENV", "production"),
    ],
)
def test_invalid_policy_is_rejected_without_disclosing_inputs(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError) as caught:
        load_auth_settings()
    assert str(caught.value) == "Invalid authentication configuration"
    assert caught.value.__context__ is None
    assert SECRET not in "".join(traceback.format_exception(caught.value))


def test_production_https_and_cookie_policy(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("OIDC_ISSUER", "https://identity.example/realms/datalens")
    monkeypatch.setenv("AUTH_PUBLIC_ORIGIN", "https://agent.example")
    settings = load_auth_settings()
    assert settings.cookie_secure is True
    assert settings.session_cookie_name == "__Host-agent-session"
    assert settings.callback_url == "https://agent.example/auth/callback"
    monkeypatch.setenv("AUTH_COOKIE_SECURE", "false")
    with pytest.raises(ConfigurationError):
        load_auth_settings()


def metadata(settings):
    root = settings.issuer + "/protocol/openid-connect/"
    return {
        "issuer": settings.issuer,
        "authorization_endpoint": root + "auth",
        "token_endpoint": root + "token",
        "introspection_endpoint": root + "token/introspect",
        "jwks_uri": root + "certs",
        "end_session_endpoint": root + "logout",
        "id_token_signing_alg_values_supported": ["RS256"],
    }


@pytest.mark.parametrize(
    "field",
    [
        "issuer",
        "authorization_endpoint",
        "token_endpoint",
        "introspection_endpoint",
        "jwks_uri",
        "end_session_endpoint",
    ],
)
def test_discovery_never_redirects_credentials_or_keys_to_untrusted_endpoints(field):
    settings = load_auth_settings()
    document = metadata(settings)
    assert settings.validate_metadata(document).issuer == settings.issuer
    document[field] = "https://attacker.invalid/" + SECRET
    with pytest.raises(ConfigurationError, match="Invalid identity metadata") as caught:
        settings.validate_metadata(document)
    assert SECRET not in str(caught.value)


def test_discovery_requires_supported_algorithm():
    settings = load_auth_settings()
    document = metadata(settings)
    document["id_token_signing_alg_values_supported"] = ["none"]
    with pytest.raises(ConfigurationError):
        settings.validate_metadata(document)


@pytest.fixture
def secret_files():
    settings = load_auth_settings()
    key = Fernet.generate_key().decode()
    settings.client_secret_file.write_text(SECRET + "\n", encoding="utf-8")
    settings.encryption_keys_file.write_text(
        json.dumps({"active_key_id": "key-1", "keys": {"key-1": key}}), encoding="utf-8"
    )
    return settings, key


def test_explicit_secret_loading_is_bounded_and_excluded_from_output(secret_files):
    settings, key = secret_files
    loaded = load_auth_secrets(settings)
    assert loaded.client_secret.get_secret_value() == SECRET
    assert loaded.active_key_id == "key-1"
    assert loaded.keys[0].material.get_secret_value() == key
    assert loaded.model_dump() == {"active_key_id": "key-1"}
    for text in (repr(loaded), loaded.model_dump_json(), repr(loaded.keys[0])):
        assert SECRET not in text and key not in text


@pytest.mark.parametrize(
    "replacement",
    [
        "{}",
        '{"active_key_id":"missing","keys":{}}',
        '{"active_key_id":"a","active_key_id":"b","keys":{}}',
        '{"active_key_id":"a","keys":{"a":"invalid-key"}}',
        '["not-an-object"]',
        "x" * 65537,
    ],
    ids=["empty", "missing", "duplicate", "invalid", "array", "oversized"],
)
def test_malformed_keyring_never_discloses_secret_material(secret_files, replacement):
    settings, key = secret_files
    settings.encryption_keys_file.write_text(replacement, encoding="utf-8")
    with pytest.raises(ConfigurationError) as caught:
        load_auth_secrets(settings)
    trace = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == "Invalid authentication secrets"
    assert caught.value.__context__ is None
    assert SECRET not in trace and key not in trace


def test_missing_secret_or_invalid_client_secret_fails_safely(secret_files):
    settings, key = secret_files
    settings.client_secret_file.unlink()
    with pytest.raises(ConfigurationError):
        load_auth_secrets(settings)
    settings.client_secret_file.write_text("REPLACE_WITH_CLIENT_SECRET", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_auth_secrets(settings)


def test_keyring_active_key_must_exist_and_key_material_cannot_repeat(secret_files):
    settings, key = secret_files
    for ring in (
        {"active_key_id": "absent", "keys": {"a": key}},
        {"active_key_id": "a", "keys": {"a": key, "b": key}},
    ):
        settings.encryption_keys_file.write_text(json.dumps(ring), encoding="utf-8")
        with pytest.raises(ConfigurationError):
            load_auth_secrets(settings)


def test_principal_and_failures_reuse_safe_immutable_contracts():
    values = {
        "issuer": "http://localhost:8081/realms/datalens",
        "subject": "user-1",
        "auth_session_ref": uuid4(),
        "roles": ["admin", "user"],
        "email_verified": True,
    }
    principal = AuthorizedPrincipal(**values)
    assert principal.roles == frozenset({"admin", "user"})
    with pytest.raises(ValidationError):
        principal.subject = "changed"
    for invalid in (False, 1, "true"):
        with pytest.raises(ValidationError):
            AuthorizedPrincipal(**{**values, "email_verified": invalid})
    for roles in ([], ["superuser"]):
        with pytest.raises(ValidationError):
            AuthorizedPrincipal(**{**values, "roles": roles})
    for code, status in (
        (AuthFailureCode.LOGIN_REQUIRED, 401),
        (AuthFailureCode.ACCESS_DENIED, 403),
        (AuthFailureCode.IDENTITY_UNAVAILABLE, 503),
    ):
        failure = AuthFailure(code=code)
        assert failure.status_code == status
        assert failure.message
        with pytest.raises(ValidationError):
            AuthFailure(code=code, details=SECRET)
