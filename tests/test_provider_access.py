"""Real session/JWT authorization handoff to the provider verifier; model stays mocked."""

from typing import Annotated

import httpx
import pytest
from fastapi import Depends, Request

from provider_fixture import ProviderFixture
from statistics_agent.api.access import require_user
from statistics_agent.contracts.auth import AuthorizedPrincipal
from statistics_agent.core.model_settings import ModelSettings
from statistics_agent.verification.provider_check import check_provider
from test_access import application, headers, new_session
from test_auth_storage import run
from test_auth_storage import settings as settings

pytestmark = pytest.mark.postgres


def test_protected_provider_check_requires_real_session_and_csrf(settings):
    async def exercise():
        async with application(settings) as (app, client, source, store, _, output):
            fixture = ProviderFixture()

            @app.post("/test/provider-check")
            async def verify(
                request: Request, principal: Annotated[AuthorizedPrincipal, Depends(require_user)]
            ):
                async def access(context):
                    return await require_user(request) == context.principal

                return await check_provider(
                    ModelSettings(SERVICE_MODEL_API_KEY="synthetic-secret"),
                    principal,
                    access,
                    transport=httpx.MockTransport(fixture.handle),
                )

            assert (
                await client.post(
                    "/test/provider-check", headers={"Origin": source.settings.public_origin}
                )
            ).status_code == 401
            assert not fixture.requests
            created = await new_session(store, source)
            cookie = headers(created)
            me = await client.get("/auth/me", headers=cookie)
            assert me.status_code == 200
            unsafe = {**cookie, "Origin": source.settings.public_origin}
            assert (await client.post("/test/provider-check", headers=unsafe)).status_code == 403
            assert not fixture.requests
            response = await client.post(
                "/test/provider-check", headers={**unsafe, "X-CSRF-Token": me.json()["csrf_token"]}
            )
            assert response.status_code == 200 and response.json()["status"] == "passed"
            assert len(fixture.requests) == 2
            assert "synthetic-secret" not in response.text + output.getvalue()

    run(exercise())
