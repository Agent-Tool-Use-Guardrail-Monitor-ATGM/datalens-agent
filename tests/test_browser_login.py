"""Real Chromium, real Keycloak and migrated disposable agent PostgreSQL."""

import io
import os
import time
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
def browser_app(settings):
    if os.environ.get("AUTH_BROWSER_TESTS") != "1":
        if os.environ.get("CI"):
            pytest.fail("CI must enable AUTH_BROWSER_TESTS; browser login cannot be skipped")
        pytest.skip("Enable AUTH_BROWSER_TESTS with the disposable Keycloak fixture")
    run(execute(settings, "upgrade"))
    auth = AuthSettings(
        app_env="test",
        issuer=ISSUER,
        public_origin=ORIGIN,
        client_secret_file="unused-client",
        encryption_keys_file="unused-ring",
    )
    secrets = keyring().model_copy(
        update={"client_secret": SecretStr("synthetic-browser-client-secret")}
    )
    output = io.StringIO()
    app = create_app(settings, auth_settings=auth, auth_secrets=secrets, log_stream=output)
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
                        await session.execute(delete(model).where(model.issuer == ISSUER))
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
