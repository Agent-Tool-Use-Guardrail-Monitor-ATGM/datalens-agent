"""Isolated API processes and synthetic Keycloak administration; no developer inputs."""

import json
import os
import re
import subprocess
import sys
import time
from html import unescape
from pathlib import Path
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet
from playwright.sync_api import Error as BrowserError

from statistics_agent.core.auth import AuthSettings
from statistics_agent.core.settings import Settings

ORIGIN = "http://localhost:18089"
IDENTITY = "http://localhost:18081"
REALM = "integration-test"
ISSUER = IDENTITY + "/realms/" + REALM
PASSWORD = "Synthetic-Test-Password!29"


class ApiProcess:
    def __init__(self, settings, directory, *, issuer=ISSUER):
        self.directory = directory
        self.issuer = issuer
        self.keys = {"first": Fernet.generate_key().decode()}
        self.key_path = directory / "keys.json"
        self.write_keys("first", self.keys)
        self.client_path = directory / "client.txt"
        self.client_path.write_text("synthetic-browser-client-secret", encoding="utf-8")
        self.environment = dict(os.environ)
        for model in (Settings, AuthSettings):
            for name, field in model.model_fields.items():
                self.environment.pop(name, None)
                self.environment.pop(str(field.validation_alias), None)
        self.environment.update(
            APP_ENV="test",
            DATABASE_URL=settings.database_url.get_secret_value(),
            OIDC_ISSUER=issuer,
            AUTH_PUBLIC_ORIGIN=ORIGIN,
            OIDC_CLIENT_SECRET_FILE=str(self.client_path),
            AUTH_TOKEN_ENCRYPTION_KEYS_FILE=str(self.key_path),
            AUTH_HTTP_READ_SECONDS="15",
            AUTH_HTTP_DEADLINE_SECONDS="30",
        )
        self.process = None

    def write_keys(self, active, keys):
        self.key_path.write_text(
            json.dumps({"active_key_id": active, "keys": keys}), encoding="utf-8"
        )

    def start(self, *, valid=True):
        assert self.process is None
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "statistics_agent.app:create_app",
                "--factory",
                "--loop",
                "statistics_agent.core.event_loop:create_event_loop",
                "--host",
                "127.0.0.1",
                "--port",
                "18089",
                "--no-access-log",
                "--log-level",
                "critical",
            ],
            cwd=Path(__file__).resolve().parents[1],
            env=self.environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if not valid:
            assert self.process.wait(timeout=20) != 0, "Invalid test secrets did not stop startup"
            return
        deadline = time.monotonic() + 20
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            while time.monotonic() < deadline:
                assert self.process.poll() is None, "Synthetic API process exited during startup"
                try:
                    if client.get(ORIGIN + "/health/live").status_code == 200:
                        assert client.get(ORIGIN + "/health/ready").status_code == 200
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
        raise AssertionError("Synthetic API process did not become ready")

    def stop(self):
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=15)
            self.process = None

    def restart(self, *, valid=True):
        self.stop()
        self.start(valid=valid)


class IdentityAdmin:
    """The disposable master's synthetic administrator, never production credentials."""

    def __init__(self):
        self.http = httpx.Client(base_url=IDENTITY, trust_env=False, timeout=30)
        self.authenticate()
        self.client_id = self.request("GET", "/clients", params={"clientId": "agent-api"}).json()[
            0
        ]["id"]

    def authenticate(self):
        response = self.http.post(
            "/realms/master/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": "integration-admin",
                "password": "Synthetic-Admin-Password!41",
            },
        )
        assert response.status_code == 200, "Synthetic identity admin is unavailable"
        self.http.headers["Authorization"] = "Bearer " + response.json()["access_token"]

    def request(self, method, path, *, expected=(200, 201, 204), **kwargs):
        assert path.startswith("/") and ".." not in path
        response = self.http.request(method, "/admin/realms/" + REALM + path, **kwargs)
        if response.status_code == 401:
            self.authenticate()
            response = self.http.request(method, "/admin/realms/" + REALM + path, **kwargs)
        assert response.status_code in expected, "Synthetic identity administration failed"
        return response

    def user(self, *, activated=True):
        name = "integration-" + uuid4().hex
        record = {
            "username": name,
            "firstName": "Synthetic",
            "lastName": "Analyst",
            "email": name + "@example.invalid",
            "emailVerified": activated,
            "enabled": True,
            "requiredActions": [] if activated else ["VERIFY_EMAIL", "UPDATE_PASSWORD"],
        }
        if activated:
            record["credentials"] = [{"type": "password", "value": PASSWORD, "temporary": False}]
        self.request("POST", "/users", json=record)
        user_id = self.request("GET", "/users", params={"username": name, "exact": "true"}).json()[
            0
        ]["id"]
        roles = [
            self.request("GET", f"/clients/{self.client_id}/roles/{role}").json()
            for role in ("admin", "user")
        ]
        self.request("POST", f"/users/{user_id}/role-mappings/clients/{self.client_id}", json=roles)
        return name, user_id

    def invitation(self, user_id):
        with httpx.Client(base_url="http://localhost:18025", trust_env=False, timeout=10) as mail:
            previous = {item["ID"] for item in mail.get("/api/v1/messages").json()["messages"]}
            self.request(
                "PUT",
                f"/users/{user_id}/execute-actions-email",
                json=["VERIFY_EMAIL", "UPDATE_PASSWORD"],
            )
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                for item in mail.get("/api/v1/messages").json()["messages"]:
                    if item["ID"] not in previous:
                        html = mail.get("/api/v1/message/" + item["ID"]).json()["HTML"]
                        for value in re.findall(r'href="([^"]+)"', html):
                            link = unescape(value)
                            if link.startswith(IDENTITY + "/realms/" + REALM + "/login-actions/"):
                                return link
                time.sleep(0.1)
        raise AssertionError("Synthetic activation email was not captured")

    def close(self):
        self.http.close()


def login(page, username="analyst"):
    try:
        page.goto(ORIGIN + "/auth/login")
        if page.url == ORIGIN + "/":
            return
        page.locator("#username").fill(username)
        page.locator("#password").fill(PASSWORD)
        page.locator("#kc-login").click()
        page.wait_for_url(ORIGIN + "/")
    except BrowserError:
        raise AssertionError("Synthetic browser login failed") from None


def session_cookie(context):
    return next(
        item["value"] for item in context.cookies(ORIGIN) if item["name"] == "agent-session-dev"
    )


def identity_service(action):
    """Control only this repository's fixed disposable Keycloak, never developer services."""
    root = Path(__file__).resolve().parents[1]
    compose = root / "compose.auth.test.yaml"
    command = ["docker", "compose", "-p", "datalens-agent-auth-tests", "-f", str(compose)]
    if action == "stop":
        command += ["stop", "identity-test-keycloak"]
    elif action == "start":
        command += ["up", "-d", "--wait", "--wait-timeout", "240"]
    else:
        raise ValueError("Unknown disposable identity action")
    completed = subprocess.run(command, cwd=root, capture_output=True, timeout=260, check=False)
    assert completed.returncode == 0, "Disposable identity lifecycle operation failed"
