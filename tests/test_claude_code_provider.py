"""ClaudeCodeProvider tests. The CLI is never executed: a fake runner stands in for subprocess.run."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ai_factory.models import Usage
from ai_factory.providers.base import ProviderError, ProviderRequest
from ai_factory.providers.claude_code import ClaudeCodeProvider


class FakeRunner:
    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0, exc: Exception | None = None):
        self.stdout, self.stderr, self.returncode, self.exc = stdout, stderr, returncode, exc
        self.calls: list[tuple[list[str], dict]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, self.stderr)


def result_json(**overrides) -> str:
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "Implemented the feature.",
        "session_id": "sess-123",
        "num_turns": 4,
        "duration_ms": 12000,
        "total_cost_usd": 0.0421,
        "usage": {
            "input_tokens": 1200,
            "cache_creation_input_tokens": 300,
            "cache_read_input_tokens": 500,
            "output_tokens": 800,
        },
    }
    return json.dumps(payload | overrides)


@pytest.fixture(autouse=True)
def not_nested(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)


@pytest.fixture
def request_(tmp_path: Path) -> ProviderRequest:
    return ProviderRequest(task_id="t1", attempt=1, prompt="Add a hello() function", workspace=tmp_path, timeout_s=42)


def test_command_is_headless_json_with_allowlisted_tools():
    provider = ClaudeCodeProvider(allowed_tools=("Read", "Edit", "Bash(pytest:*)"), max_turns=7, model="sonnet")

    cmd = provider.build_command()

    assert cmd[:4] == ["claude", "-p", "--output-format", "json"]
    assert cmd[cmd.index("--max-turns") + 1] == "7"
    assert cmd[cmd.index("--allowedTools") + 1] == "Read,Edit,Bash(pytest:*)"
    assert cmd[cmd.index("--model") + 1] == "sonnet"
    assert not any("dangerously" in part or "bypassPermissions" in part for part in cmd)


def test_successful_run_parses_output_and_usage(request_):
    runner = FakeRunner(stdout=result_json())
    provider = ClaudeCodeProvider(runner=runner)

    result = provider.run(request_)

    assert result.output == "Implemented the feature."
    assert result.usage == Usage(input_tokens=2000, output_tokens=800, cost_usd=0.0421)
    assert result.metadata["session_id"] == "sess-123"
    cmd, kwargs = runner.calls[0]
    assert kwargs["input"] == "Add a hello() function"  # prompt via stdin...
    assert "Add a hello() function" not in cmd  # ...never via argv
    assert kwargs["cwd"] == str(request_.workspace)
    assert kwargs["timeout"] == 42
    assert "shell" not in kwargs


def test_is_error_payload_raises_retryable_with_usage(request_):
    runner = FakeRunner(stdout=result_json(is_error=True, subtype="error_max_turns", result="ran out of turns"))

    with pytest.raises(ProviderError) as info:
        ClaudeCodeProvider(runner=runner).run(request_)

    assert info.value.retryable is True
    assert info.value.usage.cost_usd == pytest.approx(0.0421)
    assert "error_max_turns" in str(info.value)


def test_nonzero_exit_raises_with_stderr(request_):
    runner = FakeRunner(stdout="", stderr="API overloaded", returncode=1)

    with pytest.raises(ProviderError, match="API overloaded") as info:
        ClaudeCodeProvider(runner=runner).run(request_)
    assert info.value.retryable is True


def test_unparseable_output_raises(request_):
    with pytest.raises(ProviderError, match="could not parse JSON"):
        ClaudeCodeProvider(runner=FakeRunner(stdout="not json at all")).run(request_)


def test_json_on_last_line_is_tolerated(request_):
    runner = FakeRunner(stdout="some warning\n" + result_json())
    assert ClaudeCodeProvider(runner=runner).run(request_).output == "Implemented the feature."


def test_timeout_is_retryable(request_):
    runner = FakeRunner(exc=subprocess.TimeoutExpired(cmd="claude", timeout=42))

    with pytest.raises(ProviderError, match="timed out") as info:
        ClaudeCodeProvider(runner=runner).run(request_)
    assert info.value.retryable is True


def test_missing_binary_is_not_retryable(request_):
    runner = FakeRunner(exc=FileNotFoundError("claude"))

    with pytest.raises(ProviderError, match="not found") as info:
        ClaudeCodeProvider(runner=runner).run(request_)
    assert info.value.retryable is False


def test_refuses_nested_session(request_, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    runner = FakeRunner(stdout=result_json())

    with pytest.raises(ProviderError, match="inside a Claude Code session") as info:
        ClaudeCodeProvider(runner=runner).run(request_)
    assert info.value.retryable is False
    assert runner.calls == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"extra_args": ["--dangerously-skip-permissions"]},
        {"extra_args": ["--permission-mode", "bypassPermissions"]},
        {"allowed_tools": ["Read", "Bash"]},
        {"allowed_tools": ["Bash(*)"]},
        {"max_turns": 0},
    ],
)
def test_unsafe_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        ClaudeCodeProvider(**kwargs)


def test_default_runner_is_blocked_in_tests(request_):
    """Sanity check of the conftest guard: without an injected runner nothing real is spawned."""
    with pytest.raises(AssertionError, match="forbidden"):
        ClaudeCodeProvider().run(request_)
