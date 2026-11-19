"""Real Keycloak public PKCE, browser CORS and Bearer API acceptance."""

import base64
import hashlib
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from secrets import token_urlsafe
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from playwright.sync_api import sync_playwright

from access_harness import ISSUER, ORIGIN, PASSWORD, ApiProcess, IdentityAdmin
from statistics_agent.persistence.migrate import execute
from support.database import run

pytestmark = [pytest.mark.browser, pytest.mark.postgres]
CLIENT = "agent-web"


@pytest.fixture
def frontend_origin():
    class FrontendPage(BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"<title>OAuth fixture</title>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass  # Never log the authorization code from the callback URL.

    with ThreadingHTTPServer(("127.0.0.1", 0), FrontendPage) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


def test_browser_pkce_refresh_logout_and_bearer_cors(
    auth_storage_settings, tmp_path, frontend_origin
):
    if os.environ.get("AUTH_BROWSER_TESTS") != "1":
        if os.environ.get("CI"):
            pytest.fail("CI must enable the real Bearer browser test")
        pytest.skip("Enable AUTH_BROWSER_TESTS with disposable Keycloak")
    run(execute(auth_storage_settings, "upgrade"))
    endpoint = ISSUER + "/protocol/openid-connect"
    admin = IdentityAdmin()
    api = ApiProcess(auth_storage_settings, tmp_path, frontend_origin=frontend_origin)
    callback = frontend_origin + "/auth/callback"
    client_id = user_id = None
    try:
        fixture = Path(__file__).with_name("fixtures") / "integration-realm.json"
        configuration = json.loads(fixture.read_text(encoding="utf-8"))["clients"][0]
        configuration.pop("secret")
        configuration.update(
            clientId=CLIENT,
            publicClient=True,
            redirectUris=[callback],
            webOrigins=[frontend_origin],
        )
        configuration["attributes"]["post.logout.redirect.uris"] = frontend_origin + "/"
        created = admin.request("POST", "/clients", json=configuration)
        client_id = created.headers["location"].rsplit("/", 1)[1]
        roles = [admin.request("GET", f"/clients/{admin.client_id}/roles/user").json()]
        admin.request(
            "POST", f"/clients/{client_id}/scope-mappings/clients/{admin.client_id}", json=roles
        )
        username, user_id = admin.user()
        api.start()

        verifier, state = token_urlsafe(32), token_urlsafe(32)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        login_url = (
            endpoint
            + "/auth?"
            + urlencode(
                {
                    "client_id": CLIENT,
                    "response_type": "code",
                    "scope": "openid email",
                    "redirect_uri": callback,
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                }
            )
        )
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=["--no-proxy-server"])
            page = browser.new_page()
            page.goto(login_url)
            page.locator("#username").fill(username)
            page.locator("#password").fill(PASSWORD)
            page.locator("#kc-login").click()
            page.wait_for_url(re.compile(re.escape(callback) + r"\?.*"))
            query = parse_qs(urlsplit(page.url).query)
            assert query["state"] == [state] and query["iss"] == [ISSUER]

            def token(form):
                return page.evaluate(
                    """async ([url, form]) => {
                    const response = await fetch(url, {
                        method: 'POST', body: new URLSearchParams(form)
                    });
                    return [response.status, await response.json()];
                }""",
                    [endpoint + "/token", {"client_id": CLIENT} | form],
                )

            def me(access_token):
                return page.evaluate(
                    """async ([url, token]) => {
                    const response = await fetch(url, {
                        headers: {Authorization: `Bearer ${token}`}
                    });
                    return [response.status, await response.json()];
                }""",
                    [ORIGIN + "/auth/me", access_token],
                )

            status, tokens = token(
                {
                    "grant_type": "authorization_code",
                    "code": query["code"][0],
                    "code_verifier": verifier,
                    "redirect_uri": callback,
                }
            )
            assert status == 200
            status, profile = me(tokens["access_token"])
            assert status == 200 and profile["subject"] == user_id
            assert profile["profile_status"] == "synchronizing" and profile["roles"] == ["user"]
            status, refreshed = token(
                {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
            )
            assert status == 200 and me(refreshed["access_token"])[0] == 200

            page.goto(
                endpoint
                + "/logout?"
                + urlencode(
                    {
                        "id_token_hint": refreshed["id_token"],
                        "post_logout_redirect_uri": frontend_origin + "/",
                    }
                )
            )
            page.wait_for_url(frontend_origin + "/")
            status, _ = token(
                {"grant_type": "refresh_token", "refresh_token": refreshed["refresh_token"]}
            )
            assert status in {400, 401}
            browser.close()
    finally:
        api.stop()
        try:
            if user_id is not None:
                admin.request("DELETE", "/users/" + user_id, expected=(204, 404))
            if client_id is not None:
                admin.request("DELETE", "/clients/" + client_id, expected=(204, 404))
        finally:
            admin.close()
