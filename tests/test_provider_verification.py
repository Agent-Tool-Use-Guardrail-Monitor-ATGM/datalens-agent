"""Opt-in verifier proof without real provider keys or live inference in CI."""

import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from provider_fixture import ProviderFixture
from statistics_agent.contracts.auth import AuthorizedPrincipal
from statistics_agent.core.model_settings import ModelSettings
from statistics_agent.verification.provider_check import check_provider, main


def principal():
    return AuthorizedPrincipal(
        issuer="https://fixture.example.invalid",
        subject="private-user",
        auth_session_ref=uuid4(),
        roles=["user"],
        email_verified=True,
    )


@pytest.mark.parametrize("profile", ["groq_free", "azure_student"])
def test_same_synthetic_task_checks_both_profile_contracts_without_live_requests(profile):
    settings = {"SERVICE_MODEL_API_KEY": "synthetic-secret", "LLM_PROFILE": profile}
    if profile == "azure_student":
        settings.update(LLM_AZURE_RESOURCE="fixture", LLM_AZURE_DEPLOYMENT="fixture-model")
    fixture = ProviderFixture()
    owner = principal()

    async def access(context):
        return context.principal == owner

    report = asyncio.run(
        check_provider(
            ModelSettings(**settings, _env_file=None),
            owner,
            access,
            transport=httpx.MockTransport(fixture.handle),
        )
    )
    assert report["status"] == "passed"
    assert report["computed_values"] == [4.0]
    assert report["reported_models"] == ["fixture-model-v1"]
    assert report["sequence_contiguous"] and report["attempts"] == 2
    assert report["reported_model_snapshot_version"] is None
    assert all(
        s not in json.dumps(fixture.requests)
        for s in ("synthetic-secret", owner.subject, str(owner.auth_session_ref))
    )
    assert "synthetic-secret" not in json.dumps(report)


@pytest.mark.parametrize("denied_access,denied_dataset", [(True, False), (False, True)])
def test_denials_never_compute_a_fixture_result(denied_access, denied_dataset):
    fixture = ProviderFixture(denied_dataset=denied_dataset)

    async def access(_):
        return not denied_access

    report = asyncio.run(
        check_provider(
            ModelSettings(SERVICE_MODEL_API_KEY="synthetic-secret"),
            principal(),
            access,
            transport=httpx.MockTransport(fixture.handle),
        )
    )
    assert report["status"] == "failed" and report["computed_values"] == []
    assert len(fixture.requests) == (0 if denied_access else 1)


def test_provider_failure_is_reported_without_body_or_retries():
    async def access(_):
        return True

    report = asyncio.run(
        check_provider(
            ModelSettings(SERVICE_MODEL_API_KEY="synthetic-secret"),
            principal(),
            access,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(401, text="private provider message")
            ),
        )
    )
    assert report["status"] == "failed" and report["attempts"] == 1
    assert report["http_statuses"] == [401]
    assert "private provider message" not in json.dumps(report)


def test_cli_requires_explicit_live_flag_before_loading_secrets(monkeypatch):
    monkeypatch.setattr("sys.argv", ["provider-check"])
    monkeypatch.setattr(
        "statistics_agent.verification.provider_check.load_model_settings",
        lambda **_: pytest.fail("Loaded credentials without opt-in"),
    )
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 2
