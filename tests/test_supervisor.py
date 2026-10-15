from __future__ import annotations

import sys
import threading

from ai_factory.budget import BudgetPolicy
from ai_factory.models import TaskSpec, TaskStatus, Usage
from ai_factory.providers.base import ProviderError, ProviderResult
from ai_factory.providers.mock import MockProvider
from ai_factory.test_runner import TestCommandRunner

from tests.conftest import FakeTestRunner


def enqueue(queue, **overrides):
    return queue.enqueue(TaskSpec(**({"prompt": "implement feature", "workspace": "proj", "provider": "mock"} | overrides)))


def ok(cost: float = 0.1, text: str = "done") -> ProviderResult:
    return ProviderResult(output=text, usage=Usage(1000, 200, cost))


def test_success_without_tests(queue, make_supervisor, workspace_root):
    provider = MockProvider(responses=[ok(text="all good")])
    task = enqueue(queue)

    assert make_supervisor(provider).run_once() is True

    done = queue.get(task.id)
    assert done.status is TaskStatus.SUCCEEDED
    assert done.result == "all good"
    assert done.usage.cost_usd == 0.1
    assert provider.calls[0].workspace == (workspace_root / "proj").resolve()
    assert provider.calls[0].prompt == "implement feature"


def test_success_with_passing_tests(queue, make_supervisor):
    runner = FakeTestRunner([True])
    task = enqueue(queue, test_command=("pytest", "-q"))

    make_supervisor(MockProvider(responses=[ok()]), test_runner=runner).run_once()

    assert queue.get(task.id).status is TaskStatus.SUCCEEDED
    assert runner.calls[0][0] == ("pytest", "-q")
    assert queue.attempts(task.id)[0].tests_passed is True


def test_failing_tests_are_retried_with_feedback_then_succeed(queue, make_supervisor, clock):
    provider = MockProvider(responses=[ok(0.2), ok(0.3)])
    task = enqueue(queue, test_command=("pytest",), max_attempts=3)
    sup = make_supervisor(provider, test_runner=FakeTestRunner([False, True]))

    sup.run_once()
    after_first = queue.get(task.id)
    assert after_first.status is TaskStatus.PENDING
    assert "tests failed" in after_first.last_error
    assert after_first.available_at == clock() + 10  # backoff_base_s * 2**0

    assert sup.run_once() is False  # still backing off
    clock.advance(10)
    sup.run_once()

    final = queue.get(task.id)
    assert final.status is TaskStatus.SUCCEEDED
    assert final.attempts == 2
    assert final.usage.cost_usd == 0.5
    retry_prompt = provider.calls[1].prompt
    assert "attempt 2 of 3" in retry_prompt
    assert "FAILED test_x.py::test_feature" in retry_prompt
    attempts = queue.attempts(task.id)
    assert [a.tests_passed for a in attempts] == [False, True]


def test_exponential_backoff_is_capped(queue, make_supervisor, clock):
    task = enqueue(queue, max_attempts=10)
    sup = make_supervisor(MockProvider(responses=[ProviderError("flaky")] * 10), backoff_max_s=25)

    delays = []
    for _ in range(4):
        sup.run_once()
        delays.append(queue.get(task.id).available_at - clock())
        clock.advance(1000)

    assert delays == [10, 20, 25, 25]


def test_retryable_errors_exhaust_max_attempts(queue, make_supervisor, clock):
    task = enqueue(queue, max_attempts=2)
    sup = make_supervisor(MockProvider(responses=[ProviderError("timeout"), ProviderError("timeout")]))

    sup.run_once()
    clock.advance(1000)
    sup.run_once()

    failed = queue.get(task.id)
    assert failed.status is TaskStatus.FAILED
    assert failed.attempts == 2
    assert "max attempts reached" in failed.last_error


def test_non_retryable_error_fails_immediately(queue, make_supervisor):
    task = enqueue(queue, max_attempts=5)

    make_supervisor(MockProvider(responses=[ProviderError("binary missing", retryable=False)])).run_once()

    failed = queue.get(task.id)
    assert failed.status is TaskStatus.FAILED
    assert failed.attempts == 1
    assert "non-retryable" in failed.last_error


def test_provider_error_usage_is_still_accounted(queue, make_supervisor):
    task = enqueue(queue, max_attempts=1)

    make_supervisor(MockProvider(responses=[ProviderError("api 500", usage=Usage(10, 5, 0.07))])).run_once()

    assert queue.get(task.id).usage == Usage(10, 5, 0.07)


def test_unexpected_provider_exception_does_not_kill_worker(queue, make_supervisor):
    task = enqueue(queue, max_attempts=2)
    sup = make_supervisor(MockProvider(responses=[RuntimeError("provider bug")]))

    assert sup.run_once() is True

    retried = queue.get(task.id)
    assert retried.status is TaskStatus.PENDING
    assert "provider bug" in retried.last_error


def test_task_budget_exceeded_stops_retries(queue, make_supervisor):
    task = enqueue(queue, budget_usd=0.5, max_attempts=5, test_command=("pytest",))
    sup = make_supervisor(MockProvider(responses=[ok(0.6)]), test_runner=FakeTestRunner([False]))

    sup.run_once()

    failed = queue.get(task.id)
    assert failed.status is TaskStatus.FAILED
    assert "task budget exhausted" in failed.last_error
    assert failed.attempts == 1


def test_default_task_budget_applies_when_task_has_none(queue, make_supervisor, clock):
    task = enqueue(queue, max_attempts=5)
    sup = make_supervisor(
        MockProvider(responses=[ProviderError("x", usage=Usage(0, 0, 0.3))] * 5),
        policy=BudgetPolicy(default_task_usd=0.5),
    )

    sup.run_once()
    clock.advance(1000)
    sup.run_once()

    failed = queue.get(task.id)
    assert failed.status is TaskStatus.FAILED
    assert failed.attempts == 2
    assert "task budget exhausted" in failed.last_error


def test_token_cap_per_task(queue, make_supervisor):
    task = enqueue(queue, max_attempts=5)
    sup = make_supervisor(
        MockProvider(responses=[ProviderError("x", usage=Usage(900, 200, 0))]),
        policy=BudgetPolicy(max_tokens_per_task=1000),
    )

    sup.run_once()

    assert "token cap" in queue.get(task.id).last_error


def test_daily_budget_pauses_claiming(queue, make_supervisor):
    first = enqueue(queue)
    second = enqueue(queue)
    sup = make_supervisor(MockProvider(responses=[ok(1.0), ok(1.0)]), policy=BudgetPolicy(daily_usd=1.0))

    assert sup.run_once() is True
    assert sup.run_once() is False

    assert queue.get(first.id).status is TaskStatus.SUCCEEDED
    assert queue.get(second.id).status is TaskStatus.PENDING
    assert queue.get(second.id).attempts == 0


def test_unknown_provider_fails_task(queue, make_supervisor):
    task = enqueue(queue, provider="gpt-9")

    make_supervisor(MockProvider()).run_once()

    failed = queue.get(task.id)
    assert failed.status is TaskStatus.FAILED
    assert "unknown provider 'gpt-9'" in failed.last_error


def test_workspace_outside_root_is_rejected(queue, make_supervisor, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    provider = MockProvider()
    escaping = enqueue(queue, workspace="../outside")
    absolute = enqueue(queue, workspace=str(outside))

    sup = make_supervisor(provider)
    sup.run_once()
    sup.run_once()

    for task in (escaping, absolute):
        failed = queue.get(task.id)
        assert failed.status is TaskStatus.FAILED
        assert "outside the allowed root" in failed.last_error
    assert provider.calls == []


def test_missing_workspace_is_rejected(queue, make_supervisor):
    task = enqueue(queue, workspace="does-not-exist")

    make_supervisor(MockProvider()).run_once()

    assert "does not exist" in queue.get(task.id).last_error


def test_supervisor_routes_tasks_to_their_provider(queue, make_supervisor):
    claude_like = MockProvider(name="claude-code")
    gpt_like = MockProvider(name="openai")
    t1 = enqueue(queue, provider="claude-code")
    t2 = enqueue(queue, provider="openai")

    assert make_supervisor(claude_like, gpt_like).drain() == 2

    assert [c.task_id for c in claude_like.calls] == [t1.id]
    assert [c.task_id for c in gpt_like.calls] == [t2.id]


def test_run_forever_stops_on_event(queue, make_supervisor):
    enqueue(queue)
    stop = threading.Event()
    sup = make_supervisor(MockProvider())
    original = sup.run_once

    def run_once_then_stop():
        processed = original()
        if not processed:
            stop.set()
        return processed

    sup.run_once = run_once_then_stop
    assert sup.run_forever(stop) == 1


def test_end_to_end_with_real_test_command(queue, make_supervisor, workspace_root):
    """Mock 'agent' writes code; the real test command verifies it in the workspace."""

    def write_code(request):
        (request.workspace / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        return ok(0.05)

    check = "import calc; assert calc.add(2, 3) == 5; print('verified')"
    task = enqueue(queue, test_command=(sys.executable, "-c", check))

    make_supervisor(
        MockProvider(responses=[write_code]), test_runner=TestCommandRunner(timeout_s=60)
    ).run_once()

    done = queue.get(task.id)
    assert done.status is TaskStatus.SUCCEEDED, done.last_error
    assert "verified" in queue.attempts(task.id)[0].test_output
