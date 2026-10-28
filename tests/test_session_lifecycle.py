"""Real PostgreSQL refresh claims with independent replicas and controlled OAuth."""

import asyncio
import io
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import httpx
import pytest
from alembic import command
from pydantic import SecretStr
from sqlalchemy import event, text, update
from sqlalchemy.exc import OperationalError

from statistics_agent.api.access import csrf_token
from statistics_agent.app import create_app
from statistics_agent.contracts.auth import SessionOwner
from statistics_agent.contracts.common import AuthenticatedPrincipal
from statistics_agent.core.oidc import AuthenticationError, OidcProvider
from statistics_agent.core.session_lifecycle import SessionLifecycle
from statistics_agent.core.token_encryption import TokenEncryption
from statistics_agent.persistence.auth import AuthStore, Credentials
from statistics_agent.persistence.database import Database
from statistics_agent.persistence.models import BrowserSession
from statistics_agent.persistence.schema import migration_config
from test_access import application, headers, new_session
from test_auth_storage import run, storage
from test_auth_storage import settings as settings
from test_oidc_provider import SyntheticProvider
from test_token_encryption import keyring

pytestmark = pytest.mark.postgres


class RefreshSource(SyntheticProvider):
    def __init__(self, mode="success"):
        super().__init__()
        self.mode = mode
        self.exchanges = 0
        self.consumed = set()
        self.entered = asyncio.Event()
        self.release = None

    async def handle_async(self, request):
        if not request.url.path.endswith("/token"):
            return self.handle(request)
        self.calls.append(request)
        form = parse_qs(request.content.decode())
        assert form["grant_type"] == ["refresh_token"]
        assert "cookie" not in request.headers
        credential = form["refresh_token"][0]
        self.exchanges += 1
        if credential in self.consumed:
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.consumed.add(credential)
        self.entered.set()
        if self.release is not None:
            await self.release.wait()
        await asyncio.sleep(0.02)
        if self.mode == "outage":
            raise httpx.ReadError("private-uncertain-response", request=request)
        if self.mode == "reject":
            return httpx.Response(400, json={"error": "invalid_grant"})
        changes = {"resource_access": {"agent-api": {"roles": ["user"]}}}
        if self.mode == "wrong-owner":
            changes["sub"] = "another-user"
        if self.mode == "no-role":
            changes["resource_access"] = {}
        return httpx.Response(
            200,
            json={
                "token_type": "Bearer",
                "access_token": self.token(changes=changes),
                "refresh_token": "rotated-refresh-" + str(self.exchanges),
            },
        )


def expired(source, **changes):
    now = int(datetime.now(UTC).timestamp())
    return source.token(changes={"iat": now - 301, "exp": now - 1, **changes})


@asynccontextmanager
async def replicas(settings, source):
    async with storage(settings) as (database, store, ring):
        async with database.session() as session:
            schema = await session.scalar(text("SELECT current_schema()"))
        second_database = Database(settings)

        def set_schema(connection, record, proxy):
            cursor = connection.cursor()
            cursor.execute(f'SET search_path TO "{schema}"')
            connection.commit()
            cursor.close()

        event.listen(second_database.engine.sync_engine, "checkout", set_schema)
        other_store = AuthStore(second_database, source.settings, TokenEncryption(ring))
        providers = [
            OidcProvider(source.settings, ring, transport=httpx.MockTransport(source.handle_async))
            for _ in range(2)
        ]
        try:
            yield (
                database,
                store,
                other_store,
                [
                    SessionLifecycle(item, provider, source.settings)
                    for item, provider in zip((store, other_store), providers, strict=True)
                ],
            )
        finally:
            for provider in providers:
                await provider.close()
            await second_database.close()


def test_two_replicas_exchange_once_and_keep_cookie_with_fresh_roles(settings):
    async def exercise():
        source = RefreshSource()
        async with replicas(settings, source) as (_, store, other, lifecycle):
            created = await new_session(store, source, token=expired(source))
            results = await asyncio.gather(
                *(service.authorize(created.session) for service in lifecycle)
            )
            assert source.exchanges == 1
            assert all(identity.subject == "synthetic-user" for identity in results)
            saved = await other.read_session(created.cookie)
            assert saved.refresh_version == 1
            assert saved.credentials.refresh_token.get_secret_value() == "rotated-refresh-1"
            assert all(identity.roles == {"user"} for identity in results)
            await lifecycle[0].authorize(saved)
            assert source.exchanges == 1

    run(exercise())


@pytest.mark.parametrize(
    "mode,status", [("reject", 401), ("outage", 503), ("wrong-owner", 401), ("no-role", 403)]
)
def test_rejected_unknown_or_invalid_replacement_revokes_without_retry(settings, mode, status):
    async def exercise():
        source = RefreshSource(mode)
        async with replicas(settings, source) as (_, store, _, lifecycle):
            created = await new_session(store, source, token=expired(source))
            with pytest.raises(AuthenticationError) as caught:
                await lifecycle[0].authorize(created.session)
            assert {"login_required": 401, "identity_unavailable": 503, "access_denied": 403}[
                caught.value.code
            ] == status
            assert await store.read_session(created.cookie) is None
            with pytest.raises(AuthenticationError):
                await lifecycle[1].authorize(created.session)
            assert source.exchanges == 1

    run(exercise())


def test_crashed_claim_expires_to_revocation_without_provider_reuse(settings):
    async def exercise():
        source = RefreshSource()
        async with replicas(settings, source) as (database, store, _, lifecycle):
            created = await new_session(store, source, token=expired(source))
            claim = await store.claim_refresh(created.session.session_id, 0, 1)
            assert claim.claim_id is not None
            async with database.session() as session:
                await session.execute(
                    update(BrowserSession)
                    .where(BrowserSession.id == created.session.session_id)
                    .values(refresh_started_at=datetime.now(UTC) - timedelta(seconds=100))
                )
                await session.commit()
            with pytest.raises(AuthenticationError):
                await lifecycle[1].authorize(created.session)
            assert source.exchanges == 0
            assert await store.read_session(created.cookie) is None

    run(exercise())


@pytest.mark.parametrize("committed", [False, True], ids=["commit-failed", "commit-ack-lost"])
def test_persistence_failure_after_rotation_revokes(settings, committed):
    async def exercise():
        source = RefreshSource()
        async with replicas(settings, source) as (_, store, _, lifecycle):
            created = await new_session(store, source, token=expired(source))
            finish = store.finish_refresh

            async def broken(*args):
                if committed:
                    assert await finish(*args)
                raise OperationalError("private-commit", {}, Exception("private-error"))

            store.finish_refresh = broken
            with pytest.raises(AuthenticationError):
                await lifecycle[0].authorize(created.session)
            assert await store.read_session(created.cookie) is None
            assert source.exchanges == 1

    run(exercise())


@pytest.mark.parametrize("interrupt", ["logout", "cancel", "timeout"])
def test_logout_cancellation_or_timeout_cannot_resurrect_a_refresh(settings, interrupt):
    async def exercise():
        source = RefreshSource()
        source.release = asyncio.Event()
        async with replicas(settings, source) as (_, store, _, lifecycle):
            created = await new_session(store, source, token=expired(source))
            if interrupt == "timeout":
                lifecycle[0].budget = 0.1
            task = asyncio.create_task(lifecycle[0].authorize(created.session))
            await asyncio.wait_for(source.entered.wait(), 3)
            if interrupt == "logout":
                await store.revoke_session(created.session.session_id)
                source.release.set()
            elif interrupt == "cancel":
                task.cancel()
            with pytest.raises((AuthenticationError, asyncio.CancelledError)):
                await task
            assert await store.read_session(created.cookie) is None
            assert source.exchanges == 1

    run(exercise())


def test_logout_is_local_even_with_expired_token_and_provider_outage(settings):
    async def exercise():
        async with application(settings) as (_, client, source, store, _, _output):
            created = await store.create_session(
                AuthenticatedPrincipal(issuer=source.settings.issuer, subject="synthetic-user"),
                Credentials(
                    SecretStr(expired(source)),
                    SecretStr("private-refresh"),
                    SecretStr(source.token(False)),
                ),
            )
            owner = SessionOwner(
                issuer=source.settings.issuer,
                subject="synthetic-user",
                auth_session_ref=created.session.session_id,
            )
            source.offline = True
            response = await client.post(
                "/auth/logout",
                headers={
                    **headers(created),
                    "Origin": source.settings.public_origin,
                    "X-CSRF-Token": csrf_token(created.cookie, owner),
                },
            )
            assert response.status_code == 200
            assert response.json() == {"local_logged_out": True, "provider_logout_succeeded": False}
            assert "Max-Age=0" in response.headers["set-cookie"]
            assert await store.read_session(created.cookie) is None
            assert (await client.get("/auth/me", headers=headers(created))).status_code == 401
            assert len(source.calls) == 1 and source.calls[0].method == "GET"

    run(exercise())


@pytest.mark.parametrize("fault", ["origin", "csrf", "duplicate"])
def test_logout_rejects_csrf_before_revocation(settings, fault):
    async def exercise():
        async with application(settings) as (_, client, source, store, _, _output):
            created = await new_session(store, source)
            body = (await client.get("/auth/me", headers=headers(created))).json()
            supplied = list(headers(created).items()) + [
                ("Origin", source.settings.public_origin),
                ("X-CSRF-Token", body["csrf_token"]),
            ]
            if fault == "origin":
                supplied[1] = ("Origin", "https://foreign.example")
            elif fault == "csrf":
                supplied.pop()
            else:
                supplied.append(supplied[-1])
            response = await client.post("/auth/logout", headers=supplied)
            assert response.status_code == 403 and "set-cookie" not in response.headers
            assert await store.read_session(created.cookie)

    run(exercise())


@pytest.mark.parametrize(
    "change",
    [
        {"aud": "other-client"},
        {"iss": "https://other.example"},
        {"sub": "another-user"},
        {"email_verified": False},
        {"resource_access": {}},
    ],
)
def test_expired_invalid_identity_or_permissions_never_trigger_refresh(settings, change):
    async def exercise():
        source = RefreshSource()
        async with replicas(settings, source) as (_, store, _, lifecycle):
            created = await new_session(store, source, token=expired(source, **change))
            with pytest.raises(AuthenticationError):
                await lifecycle[0].authorize(created.session)
            assert source.exchanges == 0

    run(exercise())


def test_expired_user_token_cannot_refresh_to_repair_admin_permission(settings):
    async def exercise():
        source = RefreshSource()
        async with replicas(settings, source) as (_, store, _, lifecycle):
            created = await new_session(store, source, token=expired(source))
            with pytest.raises(AuthenticationError) as caught:
                await lifecycle[0].authorize(created.session, required_role="admin")
            assert caught.value.code == "access_denied" and source.exchanges == 0

    run(exercise())


def test_two_api_instances_share_one_refresh_and_csrf_value(settings):
    async def exercise():
        source = RefreshSource()
        async with replicas(settings, source) as (_, store, other, _lifecycles):
            created = await new_session(store, source, token=expired(source))
            async with AsyncExitStack() as stack:
                clients = []
                for local_store in (store, other):
                    app = create_app(
                        settings,
                        auth_settings=source.settings,
                        auth_secrets=keyring(),
                        oidc_transport=httpx.MockTransport(source.handle_async),
                        log_stream=io.StringIO(),
                    )
                    await stack.enter_async_context(app.router.lifespan_context(app))
                    app.state.auth_store = local_store
                    clients.append(
                        await stack.enter_async_context(
                            httpx.AsyncClient(
                                transport=httpx.ASGITransport(app),
                                base_url=source.settings.public_origin,
                            )
                        )
                    )
                responses = await asyncio.gather(
                    *(client.get("/auth/me", headers=headers(created)) for client in clients)
                )
                assert [response.status_code for response in responses] == [200, 200]
                assert responses[0].json()["csrf_token"] == responses[1].json()["csrf_token"]
                assert source.exchanges == 1
                assert not any("set-cookie" in response.headers for response in responses)

    run(exercise())


def test_refresh_claim_downgrade_revokes_pending_sessions(settings):
    async def exercise():
        source = RefreshSource()
        async with replicas(settings, source) as (database, store, _, _lifecycles):
            created = await new_session(store, source, token=expired(source))
            assert (await store.claim_refresh(created.session.session_id, 0, 30)).claim_id
            async with database.session() as session:
                schema = await session.scalar(text("SELECT current_schema()"))
            config = migration_config()
            config.attributes["version_table_schema"] = schema
            async with database.engine.begin() as connection:

                def downgrade(connection):
                    config.attributes["connection"] = connection
                    command.downgrade(config, "0002_auth_storage")

                await connection.run_sync(downgrade)
                assert (
                    await connection.scalar(
                        text("SELECT revoked_at IS NOT NULL FROM auth_sessions WHERE id=:id"),
                        {"id": created.session.session_id},
                    )
                    is True
                )

                def upgrade(connection):
                    config.attributes["connection"] = connection
                    command.upgrade(config, "head")

                await connection.run_sync(upgrade)
            assert await store.read_session(created.cookie) is None

    run(exercise())


@pytest.mark.parametrize("error_type", [OperationalError, ValueError])
def test_claim_failure_revokes_without_exchanging_a_refresh_token(settings, error_type):
    async def exercise():
        source = RefreshSource()
        async with replicas(settings, source) as (_, store, _, lifecycle):
            created = await new_session(store, source, token=expired(source))
            claim = store.claim_refresh

            async def uncertain(*args):
                await claim(*args)
                if error_type is ValueError:
                    raise ValueError("private-corrupt-record")
                raise OperationalError("private-claim-commit", {}, Exception("private-error"))

            store.claim_refresh = uncertain
            with pytest.raises(AuthenticationError):
                await lifecycle[0].authorize(created.session)
            assert source.exchanges == 0
            assert await store.read_session(created.cookie) is None

    run(exercise())


def test_refreshed_role_removal_denies_admin_without_revoking_user_access(settings):
    async def exercise():
        source = RefreshSource()
        async with replicas(settings, source) as (_, store, _, lifecycle):
            created = await new_session(
                store,
                source,
                token=expired(source, resource_access={"agent-api": {"roles": ["admin", "user"]}}),
            )
            with pytest.raises(AuthenticationError) as caught:
                await lifecycle[0].authorize(created.session, required_role="admin")
            assert caught.value.code == "access_denied"
            saved = await store.read_session(created.cookie)
            assert saved is not None and saved.refresh_version == 1
            assert (await lifecycle[1].authorize(saved)).roles == {"user"}
            assert source.exchanges == 1

    run(exercise())
