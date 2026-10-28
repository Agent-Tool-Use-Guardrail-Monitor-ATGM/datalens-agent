"""Real process restarts, captured activation and changed Keycloak permissions."""

import os
import time

import httpx
import pytest
from cryptography.fernet import Fernet
from playwright.sync_api import Error as BrowserError
from playwright.sync_api import sync_playwright
from sqlalchemy import delete

from access_harness import (
    ISSUER,
    ORIGIN,
    PASSWORD,
    ApiProcess,
    IdentityAdmin,
    identity_service,
    login,
    session_cookie,
)
from statistics_agent.persistence.database import Database
from statistics_agent.persistence.migrate import execute
from statistics_agent.persistence.models import BrowserSession, LoginAttempt
from test_auth_storage import run
from test_auth_storage import settings as settings

pytestmark = [pytest.mark.browser, pytest.mark.postgres]


@pytest.fixture
def process_api(settings, tmp_path, request):
    if os.environ.get("AUTH_BROWSER_TESTS") != "1":
        if os.environ.get("CI"):
            pytest.fail("CI must enable real access integration")
        pytest.skip("Enable AUTH_BROWSER_TESTS with the disposable identity fixture")
    run(execute(settings, "upgrade"))
    api = ApiProcess(settings, tmp_path, issuer=getattr(request, "param", ISSUER))
    try:
        api.start()
        yield api
    finally:
        api.stop()

        async def cleanup():
            database = Database(settings)
            try:
                async with database.session() as session:
                    for model in (BrowserSession, LoginAttempt):
                        await session.execute(delete(model).where(model.issuer == api.issuer))
                    await session.commit()
            finally:
                await database.close()

        run(cleanup())


@pytest.fixture
def mutable_user(process_api):
    admin = IdentityAdmin()
    username, user_id = admin.user()
    try:
        yield admin, username, user_id
    finally:
        try:
            admin.request("DELETE", "/users/" + user_id, expected=(204, 404))
        finally:
            admin.close()


@pytest.mark.parametrize("change", ["role", "email", "disabled", "identity-logout"])
def test_real_permission_changes_apply_after_token_expiry(process_api, mutable_user, change):
    admin, username, user_id = mutable_user
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-proxy-server"])
        context = browser.new_context()
        try:
            login(context.new_page(), username)
            cookie = session_cookie(context)
            initial = context.request.get(ORIGIN + "/auth/me")
            assert initial.status == 200 and initial.json()["roles"] == ["admin", "user"]
            proof = initial.json()["csrf_token"]
            if change == "role":
                role = admin.request("GET", f"/clients/{admin.client_id}/roles/admin").json()
                admin.request(
                    "DELETE",
                    f"/users/{user_id}/role-mappings/clients/{admin.client_id}",
                    json=[role],
                )
            elif change == "email":
                admin.request("PUT", "/users/" + user_id, json={"emailVerified": False})
            elif change == "disabled":
                admin.request("PUT", "/users/" + user_id, json={"enabled": False})
            else:
                admin.request("POST", f"/users/{user_id}/logout")
            # The existing signed JWT remains accepted until its own expiry.
            before_expiry = context.request.get(ORIGIN + "/auth/me")
            assert before_expiry.status == 200
            assert before_expiry.json()["roles"] == ["admin", "user"]
            deadline = time.monotonic() + 45
            while True:
                response = context.request.get(ORIGIN + "/auth/me", timeout=120000)
                if change == "role":
                    assert response.status == 200
                    if response.json()["roles"] == ["user"]:
                        assert response.json()["csrf_token"] == proof
                        assert session_cookie(context) == cookie
                        break
                elif response.status != 200:
                    assert response.status == (403 if change == "email" else 401)
                    # The local session must stay revoked even after provider re-enabling.
                    if change == "disabled":
                        admin.request("PUT", "/users/" + user_id, json={"enabled": True})
                    assert context.request.get(ORIGIN + "/auth/me").status == 401
                    break
                assert time.monotonic() < deadline, "Provider change outlived access-token expiry"
                time.sleep(0.25)
        finally:
            context.close()
            browser.close()


@pytest.mark.parametrize(
    "process_api", ["http://localhost:18081/realms/browser-test"], indirect=True
)
def test_process_restart_key_rotation_loss_and_local_revocation(process_api):
    api = process_api
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-proxy-server"])
        context = browser.new_context()
        try:
            login(context.new_page())
            cookie = session_cookie(context)
            initial = context.request.get(ORIGIN + "/auth/me")
            assert initial.status == 200
            proof = initial.json()["csrf_token"]
            api.restart()
            assert context.request.get(ORIGIN + "/auth/me").json()["csrf_token"] == proof
            second = Fernet.generate_key().decode()
            api.write_keys("second", {**api.keys, "second": second})
            api.restart()
            assert context.request.get(ORIGIN + "/auth/me").status == 200
            api.write_keys("second", {"second": second})
            api.restart()
            assert context.request.get(ORIGIN + "/auth/me").status == 401
            # Restoring an old read key restores decryptability, not a revoked session.
            api.write_keys("second", {**api.keys, "second": second})
            api.restart()
            assert context.request.get(ORIGIN + "/auth/me").status == 200
            logged_out = context.request.post(
                ORIGIN + "/auth/logout",
                timeout=45000,
                headers={"Origin": ORIGIN, "X-CSRF-Token": proof},
            )
            assert logged_out.status == 200
            api.restart()
            with httpx.Client(trust_env=False, timeout=15) as client:
                assert (
                    client.get(
                        ORIGIN + "/auth/me", headers={"Cookie": "agent-session-dev=" + cookie}
                    ).status_code
                    == 401
                )
            api.key_path.write_text("invalid-keyring", encoding="utf-8")
            api.restart(valid=False)
            api.key_path.unlink()
            api.restart(valid=False)
        finally:
            context.close()
            browser.close()


def test_captured_activation_then_agent_login(process_api):
    admin = IdentityAdmin()
    username, user_id = admin.user(activated=False)
    try:
        record = admin.request("GET", "/users/" + user_id).json()
        assert record["emailVerified"] is False
        assert admin.request("GET", f"/users/{user_id}/credentials").json() == []
        link = admin.invitation(user_id)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=["--no-proxy-server"])
            context = browser.new_context()
            try:
                page = context.new_page()
                try:
                    page.goto(link)
                    confirm = page.get_by_role("link", name="Click here to proceed")
                    if confirm.count():
                        confirm.click()
                    page.locator("#password-new").fill(PASSWORD)
                    page.locator("#password-confirm").fill(PASSWORD)
                    page.locator('button[type="submit"], input[type="submit"]').click()
                    page.wait_for_load_state()
                except BrowserError:
                    raise AssertionError("Synthetic email activation failed") from None
                activated = admin.request("GET", "/users/" + user_id).json()
                assert activated["emailVerified"] is True and activated["requiredActions"] == []
                login(page, username)
                status = context.request.get(ORIGIN + "/auth/me")
                assert status.status == 200 and status.json()["email_verified"] is True
                assert status.json()["subject"] == user_id
            finally:
                context.close()
                browser.close()
    finally:
        try:
            admin.request("DELETE", "/users/" + user_id, expected=(204, 404))
        finally:
            admin.close()


@pytest.mark.parametrize(
    "process_api", ["http://localhost:18081/realms/browser-test"], indirect=True
)
def test_real_identity_outage_allows_warm_keys_denies_cold_keys_and_keeps_local_logout(process_api):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-proxy-server"])
        context = browser.new_context()
        try:
            login(context.new_page())
            cookie = session_cookie(context)
            first = context.request.get(ORIGIN + "/auth/me")
            assert first.status == 200
            proof = first.json()["csrf_token"]
            try:
                identity_service("stop")
                assert context.request.get(ORIGIN + "/auth/me").status == 200
                process_api.restart()
                assert context.request.get(ORIGIN + "/health/live").status == 200
                assert context.request.get(ORIGIN + "/health/ready").status == 200
                assert context.request.get(ORIGIN + "/auth/me", timeout=120000).status == 503
                logged_out = context.request.post(
                    ORIGIN + "/auth/logout",
                    timeout=45000,
                    headers={"Origin": ORIGIN, "X-CSRF-Token": proof},
                )
                assert logged_out.status == 200
                assert logged_out.json() == {
                    "local_logged_out": True,
                    "provider_logout_succeeded": False,
                }
                with httpx.Client(trust_env=False, timeout=15) as client:
                    assert (
                        client.get(
                            ORIGIN + "/auth/me", headers={"Cookie": "agent-session-dev=" + cookie}
                        ).status_code
                        == 401
                    )
            finally:
                identity_service("start")
        finally:
            context.close()
            browser.close()


def test_real_expired_token_does_not_gain_access_during_identity_outage(process_api, mutable_user):
    _admin, username, _user_id = mutable_user
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-proxy-server"])
        context = browser.new_context()
        try:
            login(context.new_page(), username)
            assert context.request.get(ORIGIN + "/auth/me").status == 200
            try:
                identity_service("stop")
                deadline = time.monotonic() + 45
                while True:
                    response = context.request.get(ORIGIN + "/auth/me", timeout=120000)
                    if response.status != 200:
                        assert response.status == 503
                        assert context.request.get(ORIGIN + "/auth/me").status == 401
                        break
                    assert time.monotonic() < deadline, (
                        "Expired access survived a real provider outage"
                    )
                    time.sleep(0.25)
            finally:
                identity_service("start")
        finally:
            context.close()
            browser.close()
