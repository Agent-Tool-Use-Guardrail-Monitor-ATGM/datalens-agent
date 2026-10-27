"""HTTP and lifecycle behavior at the application boundary; no live services."""

import asyncio
import io
import json
import os
import socket
import subprocess
import sys
from datetime import datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.responses import StreamingResponse

from statistics_agent.app import create_app
from statistics_agent.core.logging import request_id_context
from statistics_agent.core.settings import ConfigurationError, Settings

SECRET = "private-value-must-never-appear"


@pytest.fixture
def settings(monkeypatch):
    for name, field in Settings.model_fields.items():
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(str(field.validation_alias), raising=False)
    return Settings(database_url=f"postgresql+psycopg://agent:{SECRET}@localhost/agent_db")


@pytest.fixture
def application(settings):
    output = io.StringIO()
    return create_app(settings, log_stream=output), output


def records(output):
    return [json.loads(line) for line in output.getvalue().splitlines()]


def test_import_does_not_load_settings_or_contact_infrastructure():
    script = """
import socket
def deny(*args, **kwargs):
    raise AssertionError('Network is forbidden')
socket.socket.connect = deny
socket.create_connection = deny
import statistics_agent.app
print('imported')
"""
    environment = dict(os.environ)
    environment.pop("DATABASE_URL", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    assert result.stdout.strip() == "imported"


def test_factory_and_lifecycle_do_not_connect_and_use_one_settings_object(settings, monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Network is forbidden")

    output = io.StringIO()
    app = None

    async def exercise():
        nonlocal app
        # Construct the event loop before denying sockets: on Windows the loop
        # itself needs a local wakeup socket, unrelated to application resources.
        with monkeypatch.context() as isolated:
            isolated.setattr(socket, "create_connection", deny)
            isolated.setattr(socket.socket, "connect", deny)
            app = create_app(settings, log_stream=output)
            assert app.state.settings is settings
            assert not app.state.started
            async with app.router.lifespan_context(app):
                assert app.state.started

    asyncio.run(exercise())
    assert not app.state.started
    events = records(output)
    assert [event["event"] for event in events] == ["service_started", "service_stopped"]
    assert SECRET not in output.getvalue()
    for event in events:
        assert event["service"] == "agent-api"
        assert event["version"] == "0.1.0"
        assert event["environment"] == "development"
        assert event["request_id"] is None
        assert datetime.fromisoformat(event["timestamp"]).utcoffset().total_seconds() == 0


def test_factory_loads_validated_configuration_once(settings, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", settings.database_url.get_secret_value())
    app = create_app(log_stream=io.StringIO())
    monkeypatch.setenv("APP_ENV", "invalid")
    with TestClient(app):
        assert app.state.settings.app_env == "development"


def test_factory_fails_safely_for_missing_configuration(settings):
    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        create_app(log_stream=io.StringIO())


def test_shutdown_runs_when_application_user_raises(application):
    app, output = application
    with pytest.raises(RuntimeError, match="test interruption"), TestClient(app):
        raise RuntimeError("test interruption")
    assert not app.state.started
    assert records(output)[-1]["event"] == "service_stopped"


@pytest.mark.parametrize("header", [None, "invalid", "x" * 5000, str(uuid4()).upper()])
def test_request_id_is_safe_and_shared_by_response_error_and_log(application, header):
    app, output = application
    headers = {} if header is None else {"X-Request-ID": header}
    with TestClient(app) as client:
        response = client.get(f"/unknown/{SECRET}?token={SECRET}", headers=headers)
    identifier = response.headers["X-Request-ID"]
    UUID(identifier)
    if header and len(header) == 36:
        assert identifier == header.lower()
    else:
        assert identifier != header
    assert response.status_code == 404
    assert response.json()["request_id"] == identifier
    event = next(item for item in records(output) if item["event"] == "http_completed")
    assert event["request_id"] == identifier
    assert event["route"] == "unmatched"
    assert event["status"] == 404
    assert event["duration_ms"] >= 0
    assert SECRET not in response.text + output.getvalue()


def test_duplicate_request_id_headers_are_replaced(application):
    app, _ = application
    supplied = str(uuid4())
    with TestClient(app) as client:
        response = client.get("/missing", headers=[("X-Request-ID", supplied)] * 2)
    assert response.headers["X-Request-ID"] != supplied


def test_safe_500_and_route_template_logs_omit_all_submitted_secrets(application):
    app, output = application

    @app.post("/test/{item}")
    async def fail(item: str):
        raise RuntimeError(f"password={SECRET}; item={item}")

    with TestClient(app) as client:
        response = client.post(
            f"/test/{SECRET}?code={SECRET}",
            json={"password": SECRET},
            headers={"Authorization": f"Bearer {SECRET}", "Cookie": f"session={SECRET}"},
        )
    assert response.status_code == 500
    assert response.json() == {
        "code": "internal_error",
        "message": "Internal server error",
        "request_id": response.headers["X-Request-ID"],
        "details": [],
    }
    event = next(item for item in records(output) if item["event"] == "http_failed")
    assert event["route"] == "/test/{item}"
    assert event["severity"] == "ERROR"
    assert event["request_id"] == response.headers["X-Request-ID"]
    assert SECRET not in response.text + output.getvalue()
    assert "RuntimeError" not in output.getvalue()


def test_validation_does_not_echo_dynamic_field_keys_input_or_exception_context(application):
    app, output = application

    @app.post("/test")
    async def validate(values: dict[str, int]):
        return values

    with TestClient(app) as client:
        response = client.post("/test", json={SECRET: SECRET})
    assert response.status_code == 422
    assert response.json()["details"] == [{"location": ["body"], "code": "invalid_value"}]
    assert SECRET not in response.text + output.getvalue()


def test_validation_details_are_bounded_and_ignore_raw_messages(application):
    app, output = application

    @app.get("/test")
    async def validate():
        raise RequestValidationError(
            [{"loc": ("query", SECRET), "type": SECRET, "msg": SECRET, "input": SECRET}] * 30
        )

    with TestClient(app) as client:
        response = client.get("/test")
    assert len(response.json()["details"]) == 20
    assert SECRET not in response.text + output.getvalue()


@pytest.mark.parametrize("status", [400, 401, 403, 429, 503])
def test_http_exception_detail_and_arbitrary_headers_are_not_exposed(application, status):
    app, output = application

    @app.get("/test")
    async def denied():
        raise HTTPException(status, detail={"password": SECRET}, headers={"X-Secret": SECRET})

    with TestClient(app) as client:
        response = client.get("/test")
    assert response.status_code == status
    assert "X-Secret" not in response.headers
    assert SECRET not in response.text + output.getvalue()


def test_method_not_allowed_keeps_safe_allow_header(application):
    app, _ = application

    @app.get("/test")
    async def example():
        return {}

    with TestClient(app) as client:
        response = client.post("/test")
    assert response.status_code == 405
    assert response.headers["Allow"] == "GET"
    assert response.json()["code"] == "method_not_allowed"


@pytest.mark.parametrize("environment,expected", [("development", 200), ("production", 404)])
def test_docs_are_development_only_and_no_business_routes_exist(settings, environment, expected):
    app = create_app(settings.model_copy(update={"app_env": environment}), log_stream=io.StringIO())
    with TestClient(app) as client:
        assert client.get("/openapi.json").status_code == expected
        assert client.get("/docs").status_code == expected
        for path in ("/runs", "/auth/login"):
            assert client.get(path).status_code == 404


def test_concurrent_requests_keep_independent_correlation(application):
    app, output = application

    @app.get("/test")
    async def example(request: Request):
        await asyncio.sleep(0)
        assert str(request.state.request_id) == request_id_context.get()
        return {}

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            ids = [str(uuid4()) for _ in range(8)]
            responses = await asyncio.gather(
                *[client.get("/test", headers={"X-Request-ID": identifier}) for identifier in ids]
            )
            assert [response.headers["X-Request-ID"] for response in responses] == ids
            assert request_id_context.get() is None
            assert {event["request_id"] for event in records(output)} == set(ids)

    asyncio.run(exercise())


def test_stream_failure_aborts_without_a_second_response_or_raw_exception(application):
    app, output = application

    @app.get("/test")
    async def streaming():
        async def chunks():
            yield b"partial result"
            raise RuntimeError(SECRET)

        return StreamingResponse(chunks())

    with TestClient(app) as client, pytest.raises(RuntimeError) as caught:
        client.get("/test")
    assert str(caught.value) == "Response failed after headers were sent"
    assert caught.value.__context__ is None
    assert SECRET not in output.getvalue()
    event = next(item for item in records(output) if item["event"] == "http_failed")
    assert event["status"] == 200  # This status had already been sent.


def test_separate_app_log_levels_and_streams_do_not_interfere(settings):
    quiet_output = io.StringIO()
    normal_output = io.StringIO()
    quiet = create_app(settings.model_copy(update={"log_level": "ERROR"}), log_stream=quiet_output)
    normal = create_app(settings, log_stream=normal_output)
    with TestClient(quiet) as quiet_client, TestClient(normal) as normal_client:
        quiet_client.get("/missing")
        normal_client.get("/missing")
    assert quiet_output.getvalue() == ""
    assert len(records(normal_output)) == 3
