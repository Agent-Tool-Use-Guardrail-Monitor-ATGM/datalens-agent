"""Shared definitions and numerical tools must not initialize the LLM framework."""

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module,forbidden",
    [
        (
            "statistics_agent.agent.tool_schema",
            ["crewai", "sqlalchemy", "statistics_agent.agent.statistics"],
        ),
        (
            "statistics_agent.contracts.sessions",
            ["crewai", "sqlalchemy", "statistics_agent.persistence.auth"],
        ),
        (
            "statistics_agent.agent.prompts",
            ["crewai", "sqlalchemy", "statistics_agent.agent.statistics"],
        ),
        ("statistics_agent.tools.statistics", ["crewai", "statistics_agent.agent.statistics"]),
    ],
)
def test_imports_keep_framework_and_environment_out_of_shared_modules(module, forbidden):
    code = """
import importlib, os, sys
module, *forbidden = sys.argv[1:]
before = {key: os.environ.get(key) for key in ("PYTHON_DOTENV_DISABLED", "OTEL_SDK_DISABLED")}
importlib.import_module(module)
assert not any(
    name == prefix or name.startswith(prefix + ".")
    for name in sys.modules for prefix in forbidden
)
assert before == {key: os.environ.get(key) for key in before}
"""
    environment = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LOCALAPPDATA")
        if key in os.environ
    }
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code, module, *forbidden],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_existing_import_paths_keep_the_same_shared_types():
    from statistics_agent.agent import provider, statistics, tool_schema
    from statistics_agent.agent.errors import AdapterError
    from statistics_agent.contracts.agent import StatisticsAnswer
    from statistics_agent.contracts.sessions import Credentials
    from statistics_agent.persistence import auth

    assert provider.AdapterError is AdapterError
    assert statistics.ToolBinding is tool_schema.ToolBinding
    assert statistics.StatisticsAnswer is StatisticsAnswer
    assert auth.Credentials is Credentials
