"""Real Chromium, real Keycloak and migrated disposable agent PostgreSQL."""

import io
import os
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx
import pytest
import uvicorn
from playwright.sync_api import sync_playwright
from pydantic import SecretStr
from sqlalchemy import delete

from statistics_agent.app import create_app
from statistics_agent.core.auth import AuthSettings
from statistics_agent.core.token_encryption import TokenEncryption
from statistics_agent.persistence.auth import AuthStore
from statistics_agent.persistence.database import Database
from statistics_agent.persistence.migrate import execute
from statistics_agent.persistence.models import BrowserSession, LoginAttempt
from test_auth_storage import run
from test_auth_storage import settings as settings
from test_token_encryption import keyring

pytestmark = [pytest.mark.browser, pytest.mark.postgres]
ORIGIN = "http://localhost:18089"
ISSUER = "http://localhost:18081/realms/browser-test"


@pytest.fixture
def browser_app(settings, request):
    if os.environ.get("AUTH_BROWSER_TESTS") != "1":
        if os.environ.get("CI"):
            pytest.fail("CI must enable AUTH_BROWSER_TESTS; browser login cannot be skipped")
        pytest.skip("Enable AUTH_BROWSER_TESTS with the disposable Keycloak fixture")
    run(execute(settings, "upgrade"))
    auth = AuthSettings(
        app_env="test",
        issuer=getattr(request, "param", ISSUER),
        public_origin=ORIGIN,
        client_secret_file="unused-client",
        encryption_keys_file="unused-ring",
        read_timeout=15 if getattr(request, "param", ISSUER) != ISSUER else 5,
        request_timeout=30 if getattr(request, "param", ISSUER) != ISSUER else 10,
    )
    secrets = keyring().model_copy(
        update={"client_secret": SecretStr("synthetic-browser-client-secret")}
    )
    output = io.StringIO()
    app = create_app(settings, auth_settings=auth, auth_secrets=secrets, log_stream=output)
    app.state.test_keys = secrets
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=18089,
            log_config=None,
            access_log=False,
            loop="statistics_agent.core.event_loop:create_event_loop",
        )
    )
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    try:
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "Isolated agent test server failed to start"
        yield app, output
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        assert not thread.is_alive(), "Isolated agent test server did not stop"

        async def cleanup():
            database = Database(settings)
            try:
                async with database.session() as session:
                    for model in (BrowserSession, LoginAttempt):
                        await session.execute(delete(model).where(model.issuer == auth.issuer))
                    await session.commit()
            finally:
                await database.close()

        run(cleanup())


@pytest.mark.parametrize(
    "scenario",
    [
        "success",
        "state",
        "binding",
        "nonce",
        "pkce",
        "unverified",
        "roleless",
        "disabled",
    ],
)
def test_browser_login_success_or_safe_denial(browser_app, scenario):
    app, output = browser_app
    with httpx.Client(trust_env=False, timeout=15, follow_redirects=False) as client:
        started = client.get(ORIGIN + "/auth/login")
        assert started.status_code == 303
        original_binding = started.cookies["agent-login-dev"]
        assert "HttpOnly" in started.headers["set-cookie"]
        assert "SameSite=lax" in started.headers["set-cookie"]
        authorization = urlsplit(started.headers["location"])
        parameters = parse_qs(authorization.query)
        if scenario in {"state", "nonce", "pkce"}:
            parameters[{"state": "state", "nonce": "nonce", "pkce": "code_challenge"}[scenario]] = [
                "x" * 43
            ]
        destination = urlunsplit(authorization._replace(query=urlencode(parameters, doseq=True)))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=["--no-proxy-server"])
            context = browser.new_context()
            context.add_cookies(
                [
                    {
                        "name": "agent-login-dev",
                        "value": "x" * 43 if scenario == "binding" else original_binding,
                        "url": ORIGIN,
                        "httpOnly": True,
                        "sameSite": "Lax",
                    }
                ]
            )
            page = context.new_page()
            try:
                page.goto(destination)
                username = (
                    scenario if scenario in {"unverified", "roleless", "disabled"} else "analyst"
                )
                page.locator("#username").fill(username)
                page.locator("#password").fill("Synthetic-Test-Password!29")
                if scenario == "disabled":
                    with page.expect_response(
                        lambda response: "/login-actions/authenticate" in response.url,
                        timeout=15000,
                    ) as denied:
                        page.locator("#kc-login").click()
                    assert denied.value.status == 200
                    assert page.locator("#kc-login").is_visible()
                    assert not any(
                        cookie["name"] == app.state.auth_settings.session_cookie_name
                        for cookie in context.cookies()
                    )
                    return
                with page.expect_response(
                    lambda response: response.url.startswith(ORIGIN + "/auth/callback?"),
                    timeout=15000,
                ) as received:
                    page.locator("#kc-login").click()
                response = received.value
                assert response.headers["cache-control"] == "no-store"
                assert response.headers["referrer-policy"] == "no-referrer"
                cookie_name = app.state.auth_settings.session_cookie_name
                session_cookies = [
                    cookie for cookie in context.cookies(ORIGIN) if cookie["name"] == cookie_name
                ]
                if scenario == "success":
                    assert response.status == 303
                    page.wait_for_url(ORIGIN + "/")
                    assert response.headers["location"] == ORIGIN + "/"
                    assert len(session_cookies) == 1 and session_cookies[0]["httpOnly"]
                    assert cookie_name not in page.evaluate("document.cookie")
                    me = context.request.get(ORIGIN + "/auth/me")
                    assert me.status == 200
                    assert me.json()["roles"] == ["admin", "user"]
                    assert len(me.json()["csrf_token"]) == 64
                    replay = client.get(
                        response.url, headers={"Cookie": "agent-login-dev=" + original_binding}
                    )
                    assert replay.status_code == 401
                else:
                    assert response.status == (
                        403 if scenario in {"unverified", "roleless"} else 401
                    )
                    assert not session_cookies
                query = parse_qs(urlsplit(response.url).query)
                assert "Synthetic-Test-Password!29" not in output.getvalue()
                assert query["code"][0] not in output.getvalue()
            finally:
                context.close()
                browser.close()


@pytest.mark.parametrize(
    "browser_app", ["http://localhost:18081/realms/refresh-test"], indirect=True
)
def test_real_refresh_keeps_cookie_and_logout_invalidates_provider_session(browser_app, settings):
    app, _output = browser_app
    with ThreadPoolExecutor(max_workers=1) as database_thread, sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-proxy-server"])
        context = browser.new_context()
        try:
            page = context.new_page()
            page.goto(ORIGIN + "/auth/login")
            page.locator("#username").fill("analyst")
            page.locator("#password").fill("Synthetic-Test-Password!29")
            page.locator("#kc-login").click()
            page.wait_for_url(ORIGIN + "/")
            cookie_name = app.state.auth_settings.session_cookie_name
            original_cookie = next(
                item["value"] for item in context.cookies(ORIGIN) if item["name"] == cookie_name
            )
            first = context.request.get(ORIGIN + "/auth/me")
            assert first.status == 200
            proof = first.json()["csrf_token"]

            async def stored_session():
                database = Database(settings)
                try:
                    store = AuthStore(
                        database, app.state.auth_settings, TokenEncryption(app.state.test_keys)
                    )
                    return await store.read_session(SecretStr(original_cookie))
                finally:
                    await database.close()

            initial = database_thread.submit(run, stored_session()).result(timeout=10)
            assert initial.refresh_version == 0
            deadline = time.monotonic() + 45
            while True:
                response = context.request.get(ORIGIN + "/auth/me")
                assert response.status == 200
                assert response.json()["csrf_token"] == proof
                current = database_thread.submit(run, stored_session()).result(timeout=10)
                if current.refresh_version == 1:
                    break
                assert time.monotonic() < deadline, (
                    "Natural token expiry did not trigger one refresh"
                )
                time.sleep(0.25)
            assert current.credentials.refresh_token != initial.credentials.refresh_token
            assert (
                next(
                    item["value"] for item in context.cookies(ORIGIN) if item["name"] == cookie_name
                )
                == original_cookie
            )
            provider_statuses = []

            async def observe_provider(response):
                provider_statuses.append((response.request.url.path, response.status_code))

            app.state.oidc._http.event_hooks["response"].append(observe_provider)
            logged_out = context.request.post(
                ORIGIN + "/auth/logout",
                timeout=45000,
                headers={
                    "Origin": ORIGIN,
                    "X-CSRF-Token": proof,
                },
            )
            assert logged_out.status == 200
            assert logged_out.json() == {
                "local_logged_out": True,
                "provider_logout_succeeded": True,
            }, (
                f"Provider statuses: {provider_statuses}; "
                f"ID hint available: {current.credentials.id_token is not None}"
            )
            assert not any(item["name"] == cookie_name for item in context.cookies(ORIGIN))
            with httpx.Client(trust_env=False, timeout=15) as client:
                assert (
                    client.get(
                        ORIGIN + "/auth/me", headers={"Cookie": cookie_name + "=" + original_cookie}
                    ).status_code
                    == 401
                )
                rejected = client.post(
                    app.state.auth_settings.issuer + "/protocol/openid-connect/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": "agent-api",
                        "client_secret": "synthetic-browser-client-secret",
                        "refresh_token": current.credentials.refresh_token.get_secret_value(),
                    },
                )
                assert rejected.status_code in {400, 401}
        finally:
            context.close()
            browser.close()
