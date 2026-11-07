"""Worker command failures stay fixed and do not expose internal diagnostics."""

from statistics_agent import worker


def test_command_reports_fixed_error_without_private_detail(monkeypatch, capsys):
    async def failed(env_file):
        raise RuntimeError("private startup diagnostic")

    monkeypatch.setattr(worker, "serve", failed)
    assert worker.main([]) == 1
    captured = capsys.readouterr()
    assert captured.out == "Agent worker stopped; check configuration and service availability\n"
    assert not captured.err


def test_command_uses_explicit_env_file_and_returns_cleanly(monkeypatch):
    called = []

    async def stopped(env_file):
        called.append(env_file)

    monkeypatch.setattr(worker, "serve", stopped)
    assert worker.main(["--env-file", "worker-test.env"]) == 0
    assert called == ["worker-test.env"]
