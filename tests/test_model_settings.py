"""Provider configuration proof using synthetic secrets only; no API access."""

import traceback

import pytest

from statistics_agent.core.model_settings import load_model_settings
from statistics_agent.core.settings import ConfigurationError


@pytest.fixture(autouse=True)
def clean_model_environment(monkeypatch):
    for key in (
        "SERVICE_MODEL_API_KEY",
        "LLM_PROFILE",
        "LLM_AZURE_RESOURCE",
        "LLM_AZURE_DEPLOYMENT",
        "LLM_TIMEOUT_SECONDS",
        "LLM_MAX_RETRIES",
        "LLM_CONCURRENCY",
        "LLM_RUN_INPUT_BUDGET",
        "LLM_RUN_OUTPUT_BUDGET",
        "LLM_MAX_STEPS",
        "LLM_RUN_TIMEOUT_SECONDS",
        "LLM_TOOL_TIMEOUT_SECONDS",
        "LLM_RETRY_MAX_DELAY_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)


def test_groq_default_and_secret_exclusion(monkeypatch):
    monkeypatch.setenv("SERVICE_MODEL_API_KEY", "synthetic-secret")
    settings = load_model_settings()
    profile = settings.resolve()
    assert profile.model == "openai/gpt-oss-20b"
    assert profile.base_url == "https://api.groq.com/openai/v1"
    assert "synthetic-secret" not in repr(settings)
    assert "synthetic-secret" not in settings.model_dump_json()
    assert "api_key" not in settings.model_dump()
    assert "synthetic-secret" not in profile.model_dump_json()
    assert settings.api_key.get_secret_value() == "synthetic-secret"
    profile.require()
    with pytest.raises(ConfigurationError):
        profile.require(structured_output=True)


def test_azure_endpoint_and_reference_identity(monkeypatch):
    monkeypatch.setenv("SERVICE_MODEL_API_KEY", "synthetic-secret")
    monkeypatch.setenv("LLM_PROFILE", "azure_student")
    monkeypatch.setenv("LLM_AZURE_RESOURCE", "test-resource")
    monkeypatch.setenv("LLM_AZURE_DEPLOYMENT", "test-deployment")
    first = load_model_settings().resolve()
    assert first.base_url == "https://test-resource.openai.azure.com/openai/v1"
    assert first.model == "test-deployment"
    monkeypatch.setenv("SERVICE_MODEL_API_KEY", "rotated-synthetic-secret")
    assert load_model_settings().resolve().reference == first.reference
    monkeypatch.setenv("LLM_AZURE_DEPLOYMENT", "different-deployment")
    assert load_model_settings().resolve().reference != first.reference
    assert first.model == "test-deployment"


@pytest.mark.parametrize(
    "variables",
    [
        {},
        {"SERVICE_MODEL_API_KEY": ""},
        {"SERVICE_MODEL_API_KEY": "replace_me"},
        {"SERVICE_MODEL_API_KEY": "synthetic secret"},
        {"LLM_PROFILE": "arbitrary-provider"},
        {"LLM_PROFILE": "azure_student"},
        {"LLM_AZURE_RESOURCE": "https://untrusted.invalid/?secret=value"},
        {"LLM_TIMEOUT_SECONDS": "nan"},
        {"LLM_CONCURRENCY": "0"},
        {"LLM_MAX_RETRIES": "100"},
    ],
)
def test_invalid_settings_fail_without_input_in_traceback(monkeypatch, variables):
    if variables:
        monkeypatch.setenv("SERVICE_MODEL_API_KEY", "synthetic-secret")
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ConfigurationError) as error:
        load_model_settings()
    rendered = "".join(traceback.format_exception(error.value))
    assert "synthetic-secret" not in rendered
    assert "untrusted.invalid" not in rendered
    assert error.value.__context__ is None


def test_explicit_file_environment_precedence_and_missing_file(tmp_path, monkeypatch):
    source = tmp_path / ".env"
    source.write_text("SERVICE_MODEL_API_KEY=file-synthetic\nLLM_CONCURRENCY=2\n", encoding="utf-8")
    monkeypatch.setenv("SERVICE_MODEL_API_KEY", "environment-synthetic")
    settings = load_model_settings(env_file=source)
    assert settings.api_key.get_secret_value() == "environment-synthetic"
    assert settings.concurrency == 2
    with pytest.raises(ConfigurationError):
        load_model_settings(env_file=tmp_path / "missing")


def test_budget_changes_produce_new_snapshot_reference(monkeypatch):
    monkeypatch.setenv("SERVICE_MODEL_API_KEY", "synthetic-secret")
    first = load_model_settings().resolve()
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    second = load_model_settings().resolve()
    assert second.reference != first.reference
    assert first.max_retries == 2
    with pytest.raises(ValueError):
        first.max_retries = 0
