from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from pathlib import Path

import pytest

import ai_factory.providers.claude_code as claude_code
from ai_factory.budget import BudgetGuard, BudgetPolicy
from ai_factory.providers.registry import ProviderRegistry
from ai_factory.queue.sqlite_queue import SqliteTaskQueue
from ai_factory.supervisor import Supervisor, SupervisorConfig
from ai_factory.test_runner import TestRunResult


@pytest.fixture(autouse=True)
def forbid_real_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee no test ever spawns a real (nested) Claude Code process."""

    def _boom(*args, **kwargs):
        raise AssertionError("tests must inject a fake runner; real Claude Code execution is forbidden")

    monkeypatch.setattr(claude_code, "_default_runner", _boom)


class FakeClock:
    def __init__(self, start: float = 1_760_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeTestRunner:
    """Returns scripted TestRunResults; records every invocation."""

    def __init__(self, results: Sequence[bool | TestRunResult] = ()) -> None:
        self._results = deque(results)
        self.calls: list[tuple[tuple[str, ...], Path]] = []

    def run(self, command: Sequence[str], cwd: Path) -> TestRunResult:
        self.calls.append((tuple(command), cwd))
        item = self._results.popleft() if self._results else True
        if isinstance(item, TestRunResult):
            return item
        return TestRunResult(
            passed=item,
            exit_code=0 if item else 1,
            output="1 passed" if item else "FAILED test_x.py::test_feature - AssertionError",
            duration_s=0.1,
        )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def queue(tmp_path: Path, clock: FakeClock) -> SqliteTaskQueue:
    return SqliteTaskQueue(tmp_path / "queue.db", clock=clock)


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspaces"
    (root / "proj").mkdir(parents=True)
    return root


@pytest.fixture
def make_supervisor(queue: SqliteTaskQueue, clock: FakeClock, workspace_root: Path):
    def _make(
        *providers,
        test_runner=None,
        policy: BudgetPolicy | None = None,
        **config_overrides,
    ) -> Supervisor:
        registry = ProviderRegistry()
        for provider in providers:
            registry.register_instance(provider)
        defaults = {
            "workspace_root": workspace_root,
            "provider_timeout_s": 30,
            "lease_seconds": 60,
            "backoff_base_s": 10,
            "backoff_max_s": 100,
            "poll_interval_s": 0.01,
        }
        config = SupervisorConfig(**(defaults | config_overrides))
        return Supervisor(
            queue,
            registry,
            test_runner or FakeTestRunner(),
            BudgetGuard(policy or BudgetPolicy(), queue, clock=clock),
            config,
            worker_id="worker-test",
            clock=clock,
        )

    return _make
