"""Protected requests use real encrypted PostgreSQL state and RSA-signed JWTs."""

import io
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated

import httpx
import jwt.api_jwt
import pytest
from fastapi import Depends, HTTPException
from pydantic import SecretStr
from sqlalchemy import Column, MetaData, String, Table, insert, select, update
from sqlalchemy.exc import OperationalError

from statistics_agent.api.access import require_admin, require_user
from statistics_agent.app import create_app
from statistics_agent.contracts.auth import AuthorizedPrincipal
from statistics_agent.contracts.common import AuthenticatedPrincipal
from statistics_agent.persistence.auth import Credentials
from statistics_agent.persistence.models import BrowserSession
from statistics_agent.persistence.ownership import owner_filter
from test_auth_storage import run, storage
from test_auth_storage import settings as settings
from test_oidc_provider import SyntheticProvider

pytestmark = pytest.mark.postgres


@asynccontextmanager
async def application(settings):
    async with storage(settings) as (database, store, ring):
        source = SyntheticProvider()
        output = io.StringIO()
        app = create_app(
            settings,
            auth_settings=source.settings,
            auth_secrets=ring,
            oidc_transport=httpx.MockTransport(source.handle),
            log_stream=output,
        )

        @app.get("/test/admin")
        async def admin(principal: Annotated[AuthorizedPrincipal, Depends(require_admin)]):
            return {"subject": principal.subject}

        @app.post("/test/write")
        async def write(principal: Annotated[AuthorizedPrincipal, Depends(require_user)]):
            return {"subject": principal.subject}

        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app),
                base_url=source.settings.public_origin,
            ) as client,
        ):
            app.state.auth_store = store
            yield app, client, source, store, database, output


async def new_session(store, source, *, changes=None, subject="synthetic-user", token=None):
    token = token or source.token(changes=changes)
    return await store.create_session(
        AuthenticatedPrincipal(issuer=source.settings.issuer, subject=subject),
        Credentials(SecretStr(token), SecretStr("private-refresh-token")),
    )


def headers(created):
    return {"Cookie": "agent-session-dev=" + created.cookie.get_secret_value()}


def test_me_uses_signed_roles_and_offline_cache_without_introspection_or_secret_leaks(settings):
    async def exercise():
        async with application(settings) as (_, client, source, store, database, output):
            created = await new_session(
                store, source, changes={"resource_access": {"agent-api": {"roles": ["admin"]}}}
            )
            first = await client.get("/auth/me", headers=headers(created))
            assert first.status_code == 200
            body = first.json()
            assert body["roles"] == ["admin", "user"] and body["email_verified"] is True
            assert set(body) == {"issuer", "subject", "roles", "email_verified", "csrf_token"}
            assert len(body["csrf_token"]) == 64
            assert first.headers["cache-control"] == "no-store"
            assert first.headers["referrer-policy"] == "no-referrer"
            assert (await client.get("/test/admin", headers=headers(created))).status_code == 200
            initial_calls = len(source.calls)
            source.offline = True
            second = await client.get("/auth/me", headers=headers(created))
            assert second.status_code == 200 and second.json()["csrf_token"] == body["csrf_token"]
            assert len(source.calls) == initial_calls == 2
            assert not any("introspect" in str(call.url) for call in source.calls)
            for value in (
                created.cookie.get_secret_value(),
                created.session.credentials.access_token.get_secret_value(),
                "private-refresh-token",
            ):
                assert value not in first.text + second.text + output.getvalue()
            async with database.session() as session:
                last_seen = await session.scalar(
                    select(BrowserSession.last_seen_at).where(
                        BrowserSession.id == created.session.session_id
                    )
                )
            assert last_seen > datetime.now(UTC) - timedelta(seconds=10)

    run(exercise())


@pytest.mark.parametrize(
    "fault,status",
    [
        ("missing-cookie", 401),
        ("malformed-cookie", 401),
        ("duplicate-cookie", 401),
        ("bearer-only", 401),
        ("missing-session", 401),
        ("revoked", 401),
        ("idle-expired", 401),
        ("absolute-expired", 401),
        ("tampered", 401),
        ("issuer", 401),
        ("audience", 401),
        ("subject", 401),
        ("email-type", 401),
        ("unverified", 403),
        ("no-role", 403),
        ("bad-ciphertext", 401),
        ("db-unavailable", 503),
    ],
)
def test_bad_session_token_or_storage_never_authorizes(settings, fault, status):
    async def exercise():
        async with application(settings) as (app, client, source, store, database, output):
            now = int(datetime.now(UTC).timestamp())
            changed = {
                "expired-token": {"iat": now - 301, "exp": now - 1},
                "issuer": {"iss": "https://wrong.example"},
                "audience": {"aud": "wrong"},
                "subject": {"sub": "different-user"},
                "email-type": {"email_verified": "true"},
                "unverified": {"email_verified": False},
                "no-role": {"resource_access": {}},
            }.get(fault)
            token = source.token(changes=changed)
            if fault == "tampered":
                other = SyntheticProvider()
                token = other.token()
            created = await new_session(store, source, token=token)
            request_headers = headers(created)
            if fault in {"missing-cookie", "bearer-only"}:
                request_headers = (
                    {"Authorization": "Bearer " + token} if fault == "bearer-only" else {}
                )
            elif fault == "malformed-cookie":
                request_headers = {"Cookie": "agent-session-dev=invalid"}
            elif fault == "duplicate-cookie":
                request_headers["Cookie"] += "; " + request_headers["Cookie"]
            elif fault == "missing-session":
                request_headers = {"Cookie": "agent-session-dev=" + "x" * 43}
            elif fault == "revoked":
                await store.revoke_session(created.session.session_id)
            elif fault in {"idle-expired", "absolute-expired"}:
                old = datetime.now(UTC) - timedelta(hours=9)
                values = {
                    "created_at": old,
                    "last_seen_at": old,
                    "idle_expires_at": old + timedelta(minutes=30),
                }
                if fault == "absolute-expired":
                    values["absolute_expires_at"] = old + timedelta(hours=8)
                async with database.session() as session:
                    await session.execute(
                        update(BrowserSession)
                        .where(BrowserSession.id == created.session.session_id)
                        .values(**values)
                    )
                    await session.commit()
            elif fault == "bad-ciphertext":
                async with database.session() as session:
                    await session.execute(
                        update(BrowserSession)
                        .where(BrowserSession.id == created.session.session_id)
                        .values(access_ciphertext="private-corrupt-ciphertext")
                    )
                    await session.commit()
            elif fault == "db-unavailable":

                class Unavailable:
                    async def read_session(self, cookie):
                        raise OperationalError("private-sql", {}, Exception("private-db-error"))

                app.state.auth_store = Unavailable()
            response = await client.get("/auth/me", headers=request_headers)
            assert response.status_code == status
            assert response.headers["cache-control"] == "no-store"
            assert all(
                value not in response.text + output.getvalue()
                for value in (
                    token,
                    "private-refresh-token",
                    "private-corrupt-ciphertext",
                    "private-db-error",
                    "private-sql",
                )
            )
            assert not any(call.method == "POST" for call in source.calls)  # No refresh attempt.

    run(exercise())


@pytest.mark.parametrize(
    "fault",
    [
        "missing-origin",
        "foreign-origin",
        "null-origin",
        "duplicate-origin",
        "missing-proof",
        "wrong-proof",
        "duplicate-proof",
        "other-session",
    ],
)
def test_unsafe_requests_require_exact_origin_and_session_bound_csrf(settings, fault):
    async def exercise():
        async with application(settings) as (_, client, source, store, _, _output):
            created = await new_session(store, source)
            status = await client.get("/auth/me", headers=headers(created))
            proof = status.json()["csrf_token"]
            request_headers = list(headers(created).items()) + [
                ("Origin", source.settings.public_origin),
                ("X-CSRF-Token", proof),
            ]
            if fault == "missing-origin":
                request_headers = [pair for pair in request_headers if pair[0] != "Origin"]
            elif fault in {"foreign-origin", "null-origin"}:
                request_headers[1] = (
                    "Origin",
                    "null" if fault == "null-origin" else "https://foreign.example",
                )
            elif fault == "duplicate-origin":
                request_headers.append(request_headers[1])
            elif fault == "missing-proof":
                request_headers.pop()
            elif fault == "wrong-proof":
                request_headers[2] = ("X-CSRF-Token", "x" * 64)
            elif fault == "duplicate-proof":
                request_headers.append(request_headers[2])
            else:
                other = await new_session(store, source)
                other_proof = (await client.get("/auth/me", headers=headers(other))).json()[
                    "csrf_token"
                ]
                request_headers[2] = ("X-CSRF-Token", other_proof)
            denied = await client.post("/test/write", headers=request_headers)
            assert denied.status_code == 403 and denied.headers["cache-control"] == "no-store"
            allowed = await client.post(
                "/test/write",
                headers={
                    **headers(created),
                    "Origin": source.settings.public_origin,
                    "X-CSRF-Token": proof,
                },
            )
            assert allowed.status_code == 200

    run(exercise())


def test_role_changes_take_effect_at_fresh_token_or_expiry_without_refresh(settings, monkeypatch):
    async def exercise():
        async with application(settings) as (_, client, source, store, _, _output):
            created = await new_session(
                store, source, changes={"resource_access": {"agent-api": {"roles": ["admin"]}}}
            )
            assert (await client.get("/test/admin", headers=headers(created))).status_code == 200
            source.changes = {"resource_access": {"agent-api": {"roles": ["user"]}}}
            source.offline = True  # Provider state changed; existing token remains authoritative.
            assert (await client.get("/test/admin", headers=headers(created))).status_code == 200
            replacement = Credentials(
                SecretStr(source.token(changes=source.changes)), SecretStr("replacement-refresh")
            )
            assert await store.replace_credentials(created.session.session_id, 0, replacement)
            replacement_claims = jwt.decode(
                replacement.access_token.get_secret_value(), options={"verify_signature": False}
            )
            assert (await client.get("/test/admin", headers=headers(created))).status_code == 403
            assert (await client.get("/auth/me", headers=headers(created))).status_code == 200

            class ExpiredClock(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime.fromtimestamp(replacement_claims["exp"] + 1, UTC)

            monkeypatch.setattr(jwt.api_jwt, "datetime", ExpiredClock)
            assert (await client.get("/auth/me", headers=headers(created))).status_code == 503
            assert len(source.calls) == 3 and source.calls[-1].method == "POST"
            assert await store.read_session(created.cookie) is None

    run(exercise())


def test_required_key_fetch_failures_and_local_revocation_race_fail_closed(settings):
    async def exercise():
        async with application(settings) as (app, client, source, store, _, _output):
            created = await new_session(store, source)
            source.offline = True
            assert (await client.get("/auth/me", headers=headers(created))).status_code == 503
            source.offline = False
            app.state.oidc._last_fetch = float("-inf")
            assert (await client.get("/auth/me", headers=headers(created))).status_code == 200
            source.offline = True
            app.state.oidc._trusted_until = 0
            app.state.oidc._last_fetch = float("-inf")
            assert (await client.get("/auth/me", headers=headers(created))).status_code == 503
            source.offline = False
            app.state.oidc._last_fetch = float("-inf")
            original_touch = store.touch_session

            async def revoke_before_touch(session_id):
                await store.revoke_session(session_id)
                return await original_touch(session_id)

            store.touch_session = revoke_before_touch
            assert (await client.get("/auth/me", headers=headers(created))).status_code == 401

    run(exercise())


def test_owner_filter_enforces_both_issuer_and_subject_even_for_admin(settings):
    async def exercise():
        async with application(settings) as (app, client, source, store, database, _output):
            table = Table(
                "owned_probe",
                MetaData(),
                Column("id", String, primary_key=True),
                Column("owner_issuer", String),
                Column("owner_subject", String),
            )
            async with database.engine.begin() as connection:
                await connection.run_sync(table.create)
                await connection.execute(
                    insert(table),
                    [
                        {
                            "id": "own",
                            "owner_issuer": source.settings.issuer,
                            "owner_subject": "synthetic-user",
                        },
                        {
                            "id": "other-user",
                            "owner_issuer": source.settings.issuer,
                            "owner_subject": "second-user",
                        },
                        {
                            "id": "other-issuer",
                            "owner_issuer": "https://other.example",
                            "owner_subject": "synthetic-user",
                        },
                    ],
                )

            @app.get("/test/resources/{resource_id}")
            async def resource(
                resource_id: str, principal: Annotated[AuthorizedPrincipal, Depends(require_user)]
            ):
                async with database.session() as session:
                    found = await session.scalar(
                        select(table.c.id).where(
                            table.c.id == resource_id,
                            owner_filter(table.c.owner_issuer, table.c.owner_subject, principal),
                        )
                    )
                if found is None:
                    raise HTTPException(404)
                return {"id": found}

            admin = await new_session(
                store, source, changes={"resource_access": {"agent-api": {"roles": ["admin"]}}}
            )
            second = await new_session(
                store, source, subject="second-user", changes={"sub": "second-user"}
            )
            assert (
                await client.get("/test/resources/own", headers=headers(admin))
            ).status_code == 200
            for identity, missing in [
                (admin, "other-user"),
                (admin, "other-issuer"),
                (admin, "absent"),
                (second, "own"),
            ]:
                response = await client.get(
                    f"/test/resources/{missing}?owner_subject=synthetic-user",
                    headers=headers(identity),
                )
                assert response.status_code == 404
            assert (
                await client.get("/test/resources/other-user", headers=headers(second))
            ).status_code == 200

    run(exercise())


def test_protected_request_key_rollover_and_unknown_kid_during_outage(settings):
    async def exercise():
        async with application(settings) as (app, client, source, store, _, _output):
            original = await new_session(store, source)
            assert (await client.get("/auth/me", headers=headers(original))).status_code == 200
            source.key = SyntheticProvider().key
            source.kid = "rotated"
            rotated = await new_session(store, source)
            source.offline = True
            app.state.oidc._last_fetch = float("-inf")
            assert (await client.get("/auth/me", headers=headers(rotated))).status_code == 503
            assert (await client.get("/auth/me", headers=headers(original))).status_code == 200
            source.offline = False
            app.state.oidc._last_fetch = float("-inf")
            assert (await client.get("/auth/me", headers=headers(rotated))).status_code == 200
            unknown = await new_session(
                store, source, token=source.token(headers={"kid": "unknown"})
            )
            app.state.oidc._last_fetch = float("-inf")
            assert (await client.get("/auth/me", headers=headers(unknown))).status_code == 401
            source.offline = True
            app.state.oidc._last_fetch = float("-inf")
            assert (await client.get("/auth/me", headers=headers(unknown))).status_code == 503

    run(exercise())
