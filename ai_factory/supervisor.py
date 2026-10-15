"""Supervisor: claims tasks, drives a provider, verifies with tests, retries, enforces budgets.

Lifecycle of one attempt::

    claim -> budget check -> resolve provider/workspace -> provider.run
          -> (tests) -> record attempt -> complete | retry (backoff) | fail

The supervisor depends only on the ``TaskQueue`` and ``AIProvider`` contracts,
so swapping SQLite for Azure Service Bus, or Claude for GPT/Gemini, does not
touch this module.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ai_factory.budget import BudgetGuard
from ai_factory.models import AttemptRecord, Task, Usage
from ai_factory.providers.base import ProviderError, ProviderRequest
from ai_factory.providers.registry import ProviderRegistry, UnknownProviderError
from ai_factory.queue.base import LeaseLostError, TaskQueue
from ai_factory.test_runner import TestRunResult

logger = logging.getLogger(__name__)


class TestRunnerLike(Protocol):
    def run(self, command: Sequence[str], cwd: Path) -> TestRunResult: ...


class WorkspaceError(ValueError):
    pass


@dataclass(frozen=True)
class SupervisorConfig:
    workspace_root: Path
    provider_timeout_s: float = 900.0
    lease_seconds: float = 1620.0
    backoff_base_s: float = 30.0
    backoff_max_s: float = 900.0
    poll_interval_s: float = 5.0
    max_result_chars: int = 20_000
    max_feedback_chars: int = 4_000


def default_worker_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def _tail(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else "...[truncated]...\n" + text[-max_chars:]


@dataclass
class _AttemptOutcome:
    usage: Usage
    output: str = ""
    provider_success: bool = False
    tests_passed: bool | None = None
    test_output: str = ""
    error: str | None = None
    retryable: bool = True


class Supervisor:
    def __init__(
        self,
        queue: TaskQueue,
        registry: ProviderRegistry,
        test_runner: TestRunnerLike,
        budget: BudgetGuard,
        config: SupervisorConfig,
        *,
        worker_id: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.queue = queue
        self.registry = registry
        self.test_runner = test_runner
        self.budget = budget
        self.config = config
        self.worker_id = worker_id or default_worker_id()
        self._clock = clock
        self._workspace_root = Path(config.workspace_root).resolve()

    # ------------------------------------------------------------------ loops
    def run_once(self) -> bool:
        """Claim and process at most one task. Returns True if a task was processed."""
        if self.budget.daily_exhausted():
            logger.warning(
                "daily budget exhausted; not claiming new tasks",
                extra={
                    "worker_id": self.worker_id,
                    "spent_usd": round(self.budget.daily_spent_usd(), 6),
                    "limit_usd": self.budget.policy.daily_usd,
                },
            )
            return False
        task = self.queue.claim(self.worker_id, self.config.lease_seconds)
        if task is None:
            return False
        try:
            self._process(task)
        except LeaseLostError:
            logger.warning("lease lost; another worker owns the task now", extra=self._ctx(task))
        except Exception:
            # Last line of defence: never let one task kill the worker loop.
            logger.exception("unexpected supervisor error", extra=self._ctx(task))
            self._safe_settle(task, "internal supervisor error", retryable=True)
        return True

    def drain(self, max_tasks: int | None = None) -> int:
        """Process tasks until none is currently available. Returns how many were processed."""
        processed = 0
        while max_tasks is None or processed < max_tasks:
            if not self.run_once():
                break
            processed += 1
        return processed

    def run_forever(self, stop: threading.Event | None = None, max_tasks: int | None = None) -> int:
        stop = stop or threading.Event()
        processed = 0
        logger.info("supervisor started", extra={"worker_id": self.worker_id})
        while not stop.is_set() and (max_tasks is None or processed < max_tasks):
            if self.run_once():
                processed += 1
            else:
                stop.wait(self.config.poll_interval_s)
        logger.info("supervisor stopped", extra={"worker_id": self.worker_id, "processed": processed})
        return processed

    # --------------------------------------------------------------- one task
    def _ctx(self, task: Task, **extra: object) -> dict[str, object]:
        return {
            "task_id": task.id,
            "attempt": task.attempts,
            "max_attempts": task.max_attempts,
            "provider": task.provider,
            "worker_id": self.worker_id,
            **extra,
        }

    def _process(self, task: Task) -> None:
        logger.info("task claimed", extra=self._ctx(task))

        reason = self.budget.task_exhausted_reason(task)
        if reason:
            self._fail(task, reason)
            return
        try:
            provider = self.registry.get(task.provider)
        except UnknownProviderError:
            self._fail(task, f"unknown provider {task.provider!r}; known: {self.registry.names()}")
            return
        try:
            workspace = self._resolve_workspace(task.workspace)
        except WorkspaceError as exc:
            self._fail(task, str(exc))
            return

        started = self._clock()
        outcome = self._attempt(task, provider, workspace)
        finished = self._clock()

        self.queue.record_attempt(
            AttemptRecord(
                task_id=task.id,
                attempt=task.attempts,
                provider=task.provider,
                worker_id=self.worker_id,
                started_at=started,
                finished_at=finished,
                provider_success=outcome.provider_success,
                tests_passed=outcome.tests_passed,
                usage=outcome.usage,
                output=_tail(outcome.output, self.config.max_result_chars),
                test_output=_tail(outcome.test_output, self.config.max_result_chars),
                error=outcome.error,
            )
        )
        usage_extra = {
            "cost_usd": outcome.usage.cost_usd,
            "input_tokens": outcome.usage.input_tokens,
            "output_tokens": outcome.usage.output_tokens,
            "duration_s": round(finished - started, 3),
            "tests_passed": outcome.tests_passed,
        }

        if outcome.error is None:
            self.queue.complete(task.id, self.worker_id, _tail(outcome.output, self.config.max_result_chars))
            logger.info("task succeeded", extra=self._ctx(task, **usage_extra))
            return

        feedback = outcome.error
        if outcome.test_output:
            feedback += "\n" + _tail(outcome.test_output, self.config.max_feedback_chars)
        logger.warning("attempt failed", extra=self._ctx(task, error=outcome.error, **usage_extra))

        updated = self.queue.get(task.id) or task  # usage now includes this attempt
        reason = self.budget.task_exhausted_reason(updated)
        if reason:
            self._fail(task, f"{reason}; last error: {feedback}")
        elif outcome.retryable and task.attempts < task.max_attempts:
            delay = self._backoff(task.attempts)
            self.queue.retry(task.id, self.worker_id, feedback, self._clock() + delay)
            logger.info("task scheduled for retry", extra=self._ctx(task, retry_in_s=delay))
        else:
            why = "non-retryable error" if not outcome.retryable else "max attempts reached"
            self._fail(task, f"{why}: {feedback}")

    def _attempt(self, task: Task, provider, workspace: Path) -> _AttemptOutcome:
        request = ProviderRequest(
            task_id=task.id,
            attempt=task.attempts,
            prompt=self._build_prompt(task),
            workspace=workspace,
            timeout_s=self.config.provider_timeout_s,
            max_budget_usd=self.budget.remaining_task_usd(task),
        )
        try:
            result = provider.run(request)
        except ProviderError as exc:
            return _AttemptOutcome(usage=exc.usage, error=f"provider error: {exc}", retryable=exc.retryable)
        except Exception as exc:  # buggy provider must not crash the supervisor
            logger.exception("provider raised unexpected exception", extra=self._ctx(task))
            return _AttemptOutcome(usage=Usage(), error=f"unexpected provider exception: {exc!r}")

        outcome = _AttemptOutcome(usage=result.usage, output=result.output, provider_success=True)
        if task.test_command:
            test = self.test_runner.run(task.test_command, workspace)
            outcome.tests_passed = test.passed
            outcome.test_output = test.output
            if not test.passed:
                outcome.error = (
                    f"tests timed out after {test.duration_s:.0f}s"
                    if test.timed_out
                    else f"tests failed (exit code {test.exit_code})"
                )
        return outcome

    # ---------------------------------------------------------------- helpers
    def _build_prompt(self, task: Task) -> str:
        if task.attempts <= 1 or not task.last_error:
            return task.prompt
        return (
            f"{task.prompt}\n\n"
            f"---\n"
            f"This is attempt {task.attempts} of {task.max_attempts}. The previous attempt failed.\n"
            f"Fix the problems below; the task is only accepted when the verification passes.\n\n"
            f"{_tail(task.last_error, self.config.max_feedback_chars)}"
        )

    def _resolve_workspace(self, workspace: str) -> Path:
        path = Path(workspace)
        if not path.is_absolute():
            path = self._workspace_root / path
        path = path.resolve()
        if not path.is_relative_to(self._workspace_root):
            raise WorkspaceError(f"workspace {path} is outside the allowed root {self._workspace_root}")
        if not path.is_dir():
            raise WorkspaceError(f"workspace {path} does not exist or is not a directory")
        return path

    def _backoff(self, attempt: int) -> float:
        return min(self.config.backoff_base_s * (2 ** max(attempt - 1, 0)), self.config.backoff_max_s)

    def _fail(self, task: Task, error: str) -> None:
        self.queue.fail(task.id, self.worker_id, error)
        logger.error("task failed", extra=self._ctx(task, error=error[:500]))

    def _safe_settle(self, task: Task, error: str, retryable: bool) -> None:
        try:
            if retryable and task.attempts < task.max_attempts:
                self.queue.retry(task.id, self.worker_id, error, self._clock() + self._backoff(task.attempts))
            else:
                self.queue.fail(task.id, self.worker_id, error)
        except LeaseLostError:
            pass
        except Exception:
            logger.exception("could not settle task after internal error", extra=self._ctx(task))
