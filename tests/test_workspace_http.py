"""Owned HTTP routes against signed JWTs and disposable PostgreSQL."""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from statistics_agent.app import create_app
from statistics_agent.contracts.datasets import MAX_UPLOAD_BYTES
from statistics_agent.core.event_loop import create_event_loop
from statistics_agent.core.model_settings import load_model_settings
from statistics_agent.core.resource_auth import ResourceAuthSettings
from statistics_agent.datasets.execution import DatasetParser
from statistics_agent.persistence.datasets import DatasetStore
from statistics_agent.persistence.jobs import JobStore
from statistics_agent.persistence.migrate import execute
from statistics_agent.worker import AgentWorker
from support.auth import SyntheticProvider
from support.database import run
from support.datasets import CONTENT
from support.worker import answer, call

ORIGIN = "https://frontend.example"
pytestmark = pytest.mark.postgres


@pytest.fixture
def workspace(auth_storage_settings, monkeypatch):
    monkeypatch.setenv("AUTH_HTTP_MODE", "bearer")
    monkeypatch.setenv("SERVICE_MODEL_API_KEY", "synthetic-api-only-key")
    monkeypatch.setenv("LLM_PROFILE", "groq_free")
    run(execute(auth_storage_settings, "upgrade"))
    source = SyntheticProvider()
    app = create_app(
        auth_storage_settings,
        resource_auth_settings=ResourceAuthSettings(
            app_env="test", issuer=source.settings.issuer, cors_origins=(ORIGIN,)
        ),
        oidc_transport=httpx.MockTransport(source.handle),
    )

    def token():
        return {
            "Authorization": "Bearer "
            + source.token(
                changes={
                    "sub": uuid4().hex,
                    "azp": "agent-web",
                    "sid": uuid4().hex,
                }
            )
        }

    with TestClient(app, backend_options={"loop_factory": create_event_loop}) as client:
        yield app, client, token(), token()


def test_csv_limits_cors_preview_and_ownership(workspace):
    _, client, owner, foreign = workspace
    path = "/v1/datasets?filename=sales.csv"
    headers = owner | {"Content-Type": "text/csv"}
    spec = client.get("/openapi.json").json()
    upload_contract = spec["paths"]["/v1/datasets"]["post"]
    assert upload_contract["security"] == [{"BearerToken": []}]
    assert set(upload_contract["requestBody"]["content"]) == {"text/csv"}
    response = client.options(
        "/v1/datasets",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert client.post(path, headers=owner, content=CONTENT).status_code == 415
    assert client.post(path, headers=headers, content=b'price\n"').status_code == 422
    assert (
        client.post(path, headers=headers, content=b"x" * (MAX_UPLOAD_BYTES + 1)).status_code == 413
    )
    upload = client.post(path, headers=headers, content=CONTENT)
    assert upload.status_code == 201, upload.text
    dataset = upload.json()
    url = "/v1/datasets/" + dataset["dataset_id"]
    try:
        assert upload.headers["cache-control"] == "no-store"
        assert client.get(url + "/preview", headers=owner).json()["rows"] == [["2"], ["4"], ["6"]]
        page = client.get("/v1/datasets?limit=1", headers=owner).json()
        assert page == {"items": [dataset], "next_cursor": dataset["dataset_id"]}
        assert not client.get("/v1/datasets", headers=foreign).json()["items"]
        for action in (client.get, client.delete):
            assert action(url, headers=foreign).status_code == 404
        assert client.get(url).status_code == 401
    finally:
        assert client.delete(url, headers=owner).status_code == 204
    assert client.get(url + "/preview", headers=owner).status_code == 404


def test_admission_retry_history_clarification_and_results(workspace):
    app, client, owner, foreign = workspace
    upload = client.post(
        "/v1/datasets?filename=sales.csv",
        headers=owner | {"Content-Type": "text/csv"},
        content=CONTENT,
    )
    assert upload.status_code == 201, upload.text
    dataset_id = upload.json()["dataset_id"]
    created = client.post("/v1/conversations", headers=owner, json={"title": "Sales"})
    assert created.status_code == 201, created.text
    url = "/v1/conversations/" + created.json()["conversation_id"]
    runs = url + "/runs"

    async def execute_one(provider):
        async with DatasetParser() as parser:
            worker = AgentWorker(
                jobs=JobStore(app.state.database),
                access=None,
                datasets=app.state.datasets,
                artifacts=DatasetStore(app.state.database),
                parser=parser,
                model_settings=load_model_settings(),
                transport=httpx.MockTransport(provider),
            )
            assert await worker.run_once()

    try:
        assert client.get(url, headers=foreign).status_code == 404
        assert (
            client.post(
                "/v1/conversations", headers=owner, json={"title": "x" * 131072}
            ).status_code
            == 413
        )
        assert (
            client.put(
                url + "/attachments", headers=owner, json={"dataset_ids": [dataset_id]}
            ).status_code
            == 200
        )
        payload = {"idempotency_key": str(uuid4()), "question": "Analyze."}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(
                pool.map(lambda _: client.post(runs, headers=owner, json=payload), range(2))
            )
        assert sorted(item.status_code for item in results) == [200, 202], [r.text for r in results]
        run_id = results[0].json()["run_id"]
        assert results[1].json()["run_id"] == run_id
        conflict = client.post(runs, headers=owner, json=payload | {"question": "Changed"})
        assert (conflict.status_code, conflict.json()["code"]) == (409, "idempotency_conflict")
        followup = {
            "idempotency_key": str(uuid4()),
            "question": "Price mean.",
            "selection": {"dataset_ids": [dataset_id]},
        }
        busy = client.post(runs, headers=owner, json=followup)
        assert (busy.status_code, busy.json()["code"]) == (409, "conversation_busy")
        assert client.delete(url, headers=owner).status_code == 409
        run_url = "/v1/runs/" + run_id
        assert client.get(run_url, headers=foreign).status_code == 404
        assert client.get(run_url, headers=owner).json()["lifecycle"] == "queued"
        client.portal.call(
            execute_one, lambda _: call("request_clarification", {"question": "Which column?"})
        )
        result = client.get(run_url, headers=owner).json()
        assert result["outcome"] == "needs_input", result
        assert result["clarification"]["question"] == "Which column?"
        page = client.get(url + "/messages?limit=1", headers=owner).json()
        assert page["items"][0]["content"] == "Analyze."
        page = client.get(
            url + "/messages?after_sequence=" + page["next_cursor"], headers=owner
        ).json()
        assert [item["role"] for item in page["items"]] == ["assistant"]

        queued = client.post(runs, headers=owner, json=followup)
        assert queued.status_code == 202, queued.text
        client.portal.call(execute_one, lambda request: answer(json.loads(request.content)))
        run_url = "/v1/runs/" + queued.json()["run_id"]
        result = client.get(run_url, headers=owner).json()
        assert result["outcome"] == "completed", result
        result_url = run_url + "/results/" + result["results"][0]["tool_call_id"]
        assert client.get(result_url, headers=foreign).status_code == 404
        artifact = client.get(result_url, headers=owner).json()
        assert artifact["payload"]["result"]["columns"][0]["metrics"]["mean"] == 4

        def interrupted(request):
            body = json.loads(request.content)
            if any(message["role"] == "tool" for message in body["messages"]):
                return httpx.Response(401)
            return answer(body)

        retry = followup | {"idempotency_key": str(uuid4())}
        queued = client.post(runs, headers=owner, json=retry)
        assert queued.status_code == 202, queued.text
        client.portal.call(execute_one, interrupted)
        partial_url = "/v1/runs/" + queued.json()["run_id"]
        partial = client.get(partial_url, headers=owner).json()
        assert partial["outcome"] == "incomplete", partial
        partial_result = partial_url + "/results/" + partial["results"][0]["tool_call_id"]
        assert client.get(partial_result, headers=owner).status_code == 200

        assert client.delete("/v1/datasets/" + dataset_id, headers=owner).status_code == 204
        assert client.get(url + "/attachments", headers=owner).json() == []
        assert client.get(result_url, headers=owner).status_code == 404
        assert client.get(run_url, headers=owner).json()["outcome"] == "completed"
        app.state.admission_configuration = None
        assert client.post(runs, headers=owner, json=followup).status_code == 200
        unavailable = client.post(
            runs, headers=owner, json=payload | {"idempotency_key": str(uuid4())}
        )
        assert (unavailable.status_code, unavailable.json()["code"]) == (503, "run_unavailable")
    finally:
        client.delete(url, headers=owner)
        client.delete("/v1/datasets/" + dataset_id, headers=owner)
