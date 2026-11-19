"""Retention command boundaries and safe diagnostics; no live services."""

import pytest

from statistics_agent import cleanup
from statistics_agent.persistence.outbox import cleanup_published
from support.database import run


@pytest.mark.parametrize(
    "options",
    [
        {"limit": 0},
        {"limit": 1001},
        {"limit": True},
        {"retention_days": 0},
        {"retention_days": 366},
        {"retention_days": 1.5},
    ],
)
def test_retention_rejects_invalid_bounds_before_database_access(options):
    with pytest.raises(ValueError):
        run(cleanup_published(None, **options))


def test_cleanup_command_defaults_and_overrides(monkeypatch, capsys):
    calls = []

    async def clean(env_file, **options):
        calls.append((env_file, options))
        return 3

    monkeypatch.setattr(cleanup, "clean", clean)
    assert cleanup.main([]) == 0
    assert cleanup.main(["--env-file", "job.env", "--retention-days", "14", "--limit", "50"]) == 0
    assert calls == [
        (None, {"retention_days": 7, "limit": 1000}),
        ("job.env", {"retention_days": 14, "limit": 50}),
    ]
    assert capsys.readouterr().out.count("Published outbox events removed: 3") == 2


def test_cleanup_failure_is_nonzero_and_does_not_expose_credentials(monkeypatch, capsys):
    async def failed(*args, **kwargs):
        raise RuntimeError("private database password")

    monkeypatch.setattr(cleanup, "clean", failed)
    assert cleanup.main([]) == 1
    assert capsys.readouterr().out == (
        "Outbox cleanup failed; check configuration and database availability\n"
    )


@pytest.mark.parametrize("arguments", [["--limit", "0"], ["--retention-days", "0"]])
def test_cleanup_cli_rejects_invalid_bounds(arguments):
    with pytest.raises(SystemExit) as error:
        cleanup.main(arguments)
    assert error.value.code == 2


def test_cleanup_rejects_identity_database_before_opening_connection(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:secret@localhost/identity_db")
    with pytest.raises(ValueError, match="agent-owned"):
        run(cleanup.clean())
