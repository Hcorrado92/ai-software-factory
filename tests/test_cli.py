from __future__ import annotations

import json
import sys

import pytest

from ai_factory.cli import main, parse_test_command


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "workspaces"
    (root / "demo").mkdir(parents=True)
    monkeypatch.setenv("ASF_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("ASF_WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("ASF_LOG_FORMAT", "text")
    monkeypatch.setenv("ASF_LOG_LEVEL", "WARNING")
    return root


def run_cli(capsys, *argv) -> tuple[int, object]:
    code = main(list(argv))
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip() else None)


def test_enqueue_run_status_roundtrip_with_mock_provider(env, capsys):
    test_cmd = json.dumps([sys.executable, "-c", "print('tests ok')"])
    code, task = run_cli(
        capsys, "enqueue", "--prompt", "hello", "--workspace", "demo", "--provider", "mock", "--test-cmd", test_cmd
    )
    assert code == 0 and task["status"] == "pending"

    code, summary = run_cli(capsys, "run", "--once")
    assert code == 0
    assert summary == {"processed": 1, "status_counts": {"succeeded": 1}}

    code, status = run_cli(capsys, "status", task["id"])
    assert status["status"] == "succeeded"
    assert status["attempt_log"][0]["tests_passed"] is True
    assert "tests ok" in status["attempt_log"][0]["test_output"]

    code, listed = run_cli(capsys, "list", "--status", "succeeded")
    assert [t["id"] for t in listed] == [task["id"]]

    code, usage = run_cli(capsys, "usage")
    assert usage["cost_usd"] == 0 and usage["daily_budget_exhausted"] is False


def test_status_of_unknown_task(env, capsys):
    assert main(["status", "nope"]) == 1


def test_enqueue_rejects_invalid_spec(env, capsys):
    assert main(["enqueue", "--prompt", "x", "--workspace", "demo", "--max-attempts", "-1"]) == 2


def test_enqueue_reports_mangled_json_test_command(env, capsys):
    # What PowerShell 5.1 delivers for '["python","-c","print(1)"]'
    assert main(["enqueue", "--prompt", "x", "--workspace", "demo", "--test-cmd", "[python,-c,print(1)]"]) == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_parse_test_command_forms():
    assert parse_test_command('["python", "-m", "pytest"]') == ("python", "-m", "pytest")
    assert parse_test_command("python -m pytest -q") == ("python", "-m", "pytest", "-q")
