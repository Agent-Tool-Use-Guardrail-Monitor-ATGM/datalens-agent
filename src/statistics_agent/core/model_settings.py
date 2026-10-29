"""Explicit server-owned model configuration; no provider requests at import/load."""

import hashlib
import re
from pathlib import Path
from typing import Literal, Self
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict, SettingsError

from statistics_agent.contracts.common import ConfigurationReference, Contract
from statistics_agent.core.settings import ConfigurationError


class ModelProfile(Contract):
    """Non-secret snapshot. Limits are application caps, not provider quotas."""

    name: Literal["groq_free", "azure_student"]
    provider: Literal["groq", "azure"]
    model: str
    base_url: str
    api_style: Literal["chat_completions_v1"] = "chat_completions_v1"
    configuration_version: Literal["model-profile-v3"] = "model-profile-v3"
    temperature: Literal[0.6] | None = None
    reasoning_effort: Literal["low"] | None = None
    tool_calling: bool = True
    json_mode: bool = True
    json_schema: bool = False
    parallel_tools: bool = False
    input_token_budget: int = 4096
    max_output_tokens: int = 1024
    timeout_seconds: float
    max_retries: int
    concurrency: int
    run_input_budget: int
    run_output_budget: int
    max_steps: int
    run_timeout_seconds: float
    tool_timeout_seconds: float
    retry_max_delay_seconds: float
    pricing_version: Literal["unpriced"] = "unpriced"

    @property
    def reference(self) -> ConfigurationReference:
        digest = hashlib.sha256(self.model_dump_json().encode()).hexdigest()
        return ConfigurationReference(
            config_id=uuid5(NAMESPACE_URL, f"datalens:model-profile:{digest}"),
            version=self.configuration_version,
        )

    def require(self, *, tools: bool = True, structured_output: bool = False) -> None:
        if (tools and not self.tool_calling) or (structured_output and not self.json_schema):
            raise ConfigurationError("Model profile does not support required capabilities")


class ModelSettings(BaseSettings):
    """Read only in the server composition layer, never from API request bodies."""

    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=True,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )
    profile: Literal["groq_free", "azure_student"] = Field(
        default="groq_free",
        validation_alias="LLM_PROFILE",
    )
    api_key: SecretStr = Field(
        validation_alias="SERVICE_MODEL_API_KEY",
        exclude=True,
        repr=False,
    )
    azure_resource: str | None = Field(default=None, validation_alias="LLM_AZURE_RESOURCE")
    azure_deployment: str | None = Field(default=None, validation_alias="LLM_AZURE_DEPLOYMENT")
    timeout_seconds: float = Field(
        default=30,
        ge=1,
        le=120,
        allow_inf_nan=False,
        validation_alias="LLM_TIMEOUT_SECONDS",
    )
    max_retries: int = Field(default=2, ge=0, le=3, validation_alias="LLM_MAX_RETRIES")
    concurrency: int = Field(default=1, ge=1, le=4, validation_alias="LLM_CONCURRENCY")

    run_input_budget: int = Field(
        default=32768, ge=1, le=262144, validation_alias="LLM_RUN_INPUT_BUDGET"
    )
    run_output_budget: int = Field(
        default=8192, ge=1, le=65536, validation_alias="LLM_RUN_OUTPUT_BUDGET"
    )
    max_steps: int = Field(default=8, ge=1, le=32, validation_alias="LLM_MAX_STEPS")
    run_timeout_seconds: float = Field(
        default=120,
        ge=0.01,
        le=600,
        allow_inf_nan=False,
        validation_alias="LLM_RUN_TIMEOUT_SECONDS",
    )
    tool_timeout_seconds: float = Field(
        default=10, ge=0.01, le=60, allow_inf_nan=False, validation_alias="LLM_TOOL_TIMEOUT_SECONDS"
    )
    retry_max_delay_seconds: float = Field(
        default=10, ge=0, le=60, allow_inf_nan=False, validation_alias="LLM_RETRY_MAX_DELAY_SECONDS"
    )

    @field_validator("api_key")
    @classmethod
    def check_key(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not 1 <= len(raw) <= 4096 or any(c.isspace() or not c.isprintable() for c in raw):
            raise ValueError("A nonempty single-line API key is required")
        if raw.lower() in {"replace_me", "your-key", "your_key", "your-api-key"}:
            raise ValueError("Replace the API key placeholder")
        return value

    @field_validator("azure_resource", "azure_deployment")
    @classmethod
    def check_azure_name(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
            raise ValueError("Expected an Azure resource/deployment name, not a URL")
        return value

    @model_validator(mode="after")
    def check_profile(self) -> Self:
        if self.profile == "azure_student":
            if not self.azure_resource or not self.azure_deployment:
                raise ValueError("Azure profile requires resource and deployment names")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}", self.azure_resource):
                raise ValueError("Invalid Azure resource hostname")
        elif self.azure_resource is not None or self.azure_deployment is not None:
            raise ValueError("Azure settings cannot be used with the Groq profile")
        return self

    def resolve(self) -> ModelProfile:
        """Construct trusted endpoints; no arbitrary URL/model override is accepted."""
        return ModelProfile(
            name=self.profile,
            provider="groq" if self.profile == "groq_free" else "azure",
            model="openai/gpt-oss-20b" if self.profile == "groq_free" else self.azure_deployment,
            base_url=(
                "https://api.groq.com/openai/v1"
                if self.profile == "groq_free"
                else f"https://{self.azure_resource}.openai.azure.com/openai/v1"
            ),
            temperature=0.6 if self.profile == "groq_free" else None,
            reasoning_effort="low" if self.profile == "groq_free" else None,
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
            concurrency=self.concurrency,
            run_input_budget=self.run_input_budget,
            run_output_budget=self.run_output_budget,
            max_steps=self.max_steps,
            run_timeout_seconds=self.run_timeout_seconds,
            tool_timeout_seconds=self.tool_timeout_seconds,
            retry_max_delay_seconds=self.retry_max_delay_seconds,
        )


def load_model_settings(*, env_file: str | Path | None = None) -> ModelSettings:
    """Environment overrides an explicitly selected file. Never expose raw errors."""
    try:
        if env_file is not None and not Path(env_file).is_file():
            raise OSError("Unavailable configuration file")
        return ModelSettings(_env_file=env_file, _env_file_encoding="utf-8")
    except (ValidationError, SettingsError, OSError, UnicodeError):
        pass
    # Outside the handler: no retained validation error/input in traceback context.
    raise ConfigurationError("Invalid model configuration; check profile and secret settings")
