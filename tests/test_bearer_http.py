"""Public Bearer API against signed tokens and a disposable PostgreSQL projection."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text

from statistics_agent.app import create_app
from statistics_agent.contracts.users import UserProfileClaims
from statistics_agent.core.event_loop import create_event_loop
from statistics_agent.core.resource_auth import ResourceAuthSettings
from statistics_agent.persistence.identity_projection import IdentityProjection
from statistics_agent.persistence.migrate import execute
from statistics_agent.persistence.users import UserProfileStore
from support.auth import SyntheticProvider
from support.database import run

ORIGIN = "https://frontend.example"


def credentials(source, **changes):
    return {
        "Authorization": "Bearer "
        + source.token(changes={"azp": "agent-web", "sid": "s"} | changes)
    }


@pytest.fixture
def api(auth_storage_settings, monkeypatch):
    monkeypatch.setenv("AUTH_HTTP_MODE", "bearer")
    monkeypatch.setenv("OIDC_CLIENT_SECRET_FILE", "absent-client-secret")
    monkeypatch.setenv("AUTH_TOKEN_ENCRYPTION_KEYS_FILE", "absent-keyring")
    run(execute(auth_storage_settings, "upgrade"))
    source = SyntheticProvider()
    settings = ResourceAuthSettings(
        app_env="test", issuer=source.settings.issuer, cors_origins=(ORIGIN,)
    )
    app = create_app(
        auth_storage_settings,
        resource_auth_settings=settings,
        oidc_transport=httpx.MockTransport(source.handle),
    )
    with TestClient(app, backend_options={"loop_factory": create_event_loop}) as client:
        yield app, client, source


def event(issuer, subject, kind, data):
    return json.dumps(
        {
            "schema_version": 1,
            "event_id": str(uuid4()),
            "event_type": kind,
            "issuer": issuer,
            "subject": subject,
            "occurred_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "data": data,
        }
    ).encode()


@pytest.mark.postgres
def test_profile_comes_from_projection_and_revocation_is_enforced(api):
    app, client, source = api
    issuer = source.settings.issuer
    subject = "bearer-http-" + uuid4().hex
    headers = credentials(source, sub=subject, name=["malformed display claim"])
    portal = client.portal
    store = UserProfileStore(app.state.database)
    projection = IdentityProjection(app.state.database, issuer)
    portal.call(
        store.upsert, UserProfileClaims(issuer=issuer, subject=subject, display_name="Unprojected")
    )
    try:
        pending = client.get("/auth/me", headers=headers)
        assert pending.status_code == 200
        assert pending.json() == {
            "issuer": issuer,
            "subject": subject,
            "roles": ["user"],
            "email_verified": True,
            "profile_status": "synchronizing",
            "profile": None,
        }

        portal.call(
            projection.apply,
            event(
                issuer,
                subject,
                "user.profile.updated",
                {
                    "profile_revision": 1,
                    "display_name": "Projected",
                    "username": None,
                    "email": None,
                },
            ),
        )
        result = client.get("/auth/me", headers=headers | {"Origin": ORIGIN})
        assert result.status_code == 200 and result.json()["profile_status"] == "ready"
        assert result.json()["profile"]["display_name"] == "Projected"
        assert result.headers["access-control-allow-origin"] == ORIGIN
        assert result.headers["cache-control"] == "no-store"
        assert "set-cookie" not in result.headers
        other = client.get("/auth/me", headers=credentials(source, sub=uuid4().hex))
        assert other.status_code == 200 and other.json()["profile"] is None

        portal.call(
            projection.apply,
            event(
                issuer,
                subject,
                "user.session.revoked",
                {
                    "security_revision": 1,
                    "session_id": "s",
                    "reason": "logout",
                },
            ),
        )
        revoked = client.get("/auth/me", headers=headers)
        assert revoked.status_code == 401 and revoked.headers["www-authenticate"] == "Bearer"
        assert client.get("/v1/datasets", headers=headers).status_code == 401
        assert (
            client.get("/auth/me", headers=credentials(source, sub=subject, sid="new")).status_code
            == 200
        )
        assert all(call.method == "GET" for call in source.calls)
    finally:

        async def cleanup():
            async with app.state.database.session() as session, session.begin():
                for table in (
                    "agent_identity_revoked_sessions",
                    "agent_user_profiles",
                    "agent_identity_state",
                ):
                    await session.execute(
                        text(f"DELETE FROM {table} WHERE issuer=:issuer AND subject=:subject"),
                        {"issuer": issuer, "subject": subject},
                    )

        portal.call(cleanup)


@pytest.mark.postgres
def test_bearer_only_http_contract_preflight_and_key_failure(api):
    app, client, source = api
    assert app.state.auth_store is None
    for route in ("/auth/login", "/auth/callback", "/auth/logout"):
        assert client.get(route).status_code == 404
    assert client.post("/auth/logout").status_code == 404
    assert client.get("/openapi.json").json()["paths"]["/auth/me"]["get"]["security"] == [
        {"BearerToken": []}
    ]
    for headers in (
        {"Cookie": "agent-session-dev=old", "X-CSRF-Token": "old"},
        {"Authorization": "Bearer one, Bearer two"},
        [("Authorization", "Bearer a.b.c"), ("Authorization", "Bearer d.e.f")],
        {"X-User-Role": "admin"},
    ):
        result = client.get("/auth/me", headers=headers)
        assert result.status_code == 401 and result.headers["www-authenticate"] == "Bearer"
    preflight = {
        "Origin": ORIGIN,
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "Authorization, X-Request-ID",
    }
    response = client.options("/auth/me", headers=preflight)
    assert response.status_code == 200 and response.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-credentials" not in response.headers
    for overrides in (
        {"Origin": ORIGIN + ".evil.example"},
        {"Access-Control-Request-Headers": "X-CSRF-Token"},
        {"Access-Control-Request-Method": "POST"},
    ):
        assert client.options("/auth/me", headers=preflight | overrides).status_code == 400
    assert client.get("/auth/me", headers={"Origin": ORIGIN}).status_code == 401
    source.offline = True
    failure = client.get("/auth/me", headers=credentials(source) | {"Origin": ORIGIN})
    assert failure.status_code == 503 and failure.json()["code"] == "identity_unavailable"
    assert failure.headers["access-control-allow-origin"] == ORIGIN


@pytest.mark.postgres
def test_bearer_roles_expiry_and_client_audience(api):
    _, client, source = api
    now = int(datetime.now(UTC).timestamp())
    for changes, expected in (
        ({"iat": now - 60, "exp": now - 1}, 401),
        ({"aud": "agent-web"}, 401),
        ({"azp": "agent-api"}, 401),
        ({"sid": None}, 401),
        ({"resource_access": {}}, 403),
        ({"email_verified": False}, 403),
    ):
        assert (
            client.get("/auth/me", headers=credentials(source, **changes)).status_code == expected
        )
        assert (
            client.get("/v1/datasets", headers=credentials(source, **changes)).status_code
            == expected
        )
    response = client.get(
        "/auth/me", headers=credentials(source, resource_access={"agent-api": {"roles": ["admin"]}})
    )
    assert response.status_code == 200 and response.json()["roles"] == ["admin", "user"]


@pytest.mark.parametrize(
    "origin",
    [
        "*",
        "https://*.example",
        "https://example.test/path",
        "http://example.test",
        "https://example.test/",
    ],
)
def test_origins_must_be_exact_and_secure(origin):
    with pytest.raises(ValidationError):
        ResourceAuthSettings(app_env="test", cors_origins=(origin,))
