"""Explicit synthetic auth injection for existing infrastructure-independent tests."""

from statistics_agent.app import create_app as application_factory
from statistics_agent.core.auth import AuthSettings
from statistics_agent.core.settings import load_settings
from support.crypto import keyring


def create_legacy_app(settings=None, **kwargs):
    settings = settings if settings is not None else load_settings()
    return application_factory(settings.model_copy(update={"http_auth_mode": "legacy"}), **kwargs)


def create_app(settings=None, **kwargs):
    settings = settings if settings is not None else load_settings()
    secure = settings.app_env in {"staging", "production"}
    auth = AuthSettings(
        app_env=settings.app_env,
        issuer="https://identity.example/realms/test"
        if secure
        else "http://localhost:8081/realms/test",
        public_origin="https://agent.example" if secure else "http://localhost:8000",
        client_secret_file="unused-client",
        encryption_keys_file="unused-ring",
    )
    return create_legacy_app(settings, auth_settings=auth, auth_secrets=keyring(), **kwargs)
