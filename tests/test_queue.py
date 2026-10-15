from __future__ import annotations

import threading

import pytest

from ai_factory.models import AttemptRecord, TaskSpec, TaskStatus, Usage
from ai_factory.queue.base import LeaseLostError
from ai_factory.queue.sqlite_queue import SqliteTaskQueue


def spec(**overrides) -> TaskSpec:
    return TaskSpec(**({"prompt": "do it", "workspace": "proj"} | overrides))


def test_enqueue_persists_across_instances(tmp_path, clock):
    db = tmp_path / "q.db"
    task = SqliteTaskQueue(db, clock=clock).enqueue(spec(test_command=("pytest", "-q"), budget_usd=1.5))

    reopened = SqliteTaskQueue(db, clock=clock).get(task.id)

    assert reopened is not None
    assert reopened.status is TaskStatus.PENDING
    assert reopened.test_command == ("pytest", "-q")
    assert reopened.budget_usd == 1.5
    assert reopened.attempts == 0


def test_claim_respects_priority_then_fifo(queue, clock):
    low = queue.enqueue(spec(prompt="low"))
    clock.advance(1)
    high = queue.enqueue(spec(prompt="high", priority=5))
    clock.advance(1)
    low2 = queue.enqueue(spec(prompt="low2"))

    order = [queue.claim("w", 60).id for _ in range(3)]

    assert order == [high.id, low.id, low2.id]
    assert queue.claim("w", 60) is None


def test_claim_is_exclusive_and_increments_attempts(queue):
    task = queue.enqueue(spec())

    claimed = queue.claim("w1", 60)

    assert claimed.id == task.id
    assert claimed.status is TaskStatus.RUNNING
    assert claimed.attempts == 1
    assert claimed.worker_id == "w1"
    assert queue.claim("w2", 60) is None


def test_concurrent_claims_never_hand_out_a_task_twice(queue):
    ids = {queue.enqueue(spec(prompt=f"t{i}")).id for i in range(20)}
    claimed: list[str] = []
    lock = threading.Lock()

    def worker(n: int) -> None:
        while (task := queue.claim(f"w{n}", 60)) is not None:
            with lock:
                claimed.append(task.id)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(claimed) == sorted(ids)


def test_retry_waits_until_available_at(queue, clock):
    task = queue.enqueue(spec())
    queue.claim("w", 60)

    queue.retry(task.id, "w", "boom", available_at=clock() + 30)

    assert queue.get(task.id).status is TaskStatus.PENDING
    assert queue.get(task.id).last_error == "boom"
    assert queue.claim("w", 60) is None
    clock.advance(31)
    again = queue.claim("w", 60)
    assert again.id == task.id and again.attempts == 2


def test_expired_lease_is_reclaimed_and_old_worker_loses_ownership(queue, clock):
    task = queue.enqueue(spec(max_attempts=3))
    queue.claim("dead-worker", 60)
    clock.advance(61)

    rescued = queue.claim("w2", 60)

    assert rescued.id == task.id
    assert rescued.worker_id == "w2"
    assert rescued.attempts == 2
    with pytest.raises(LeaseLostError):
        queue.complete(task.id, "dead-worker", "late result")
    queue.complete(task.id, "w2", "ok")
    assert queue.get(task.id).status is TaskStatus.SUCCEEDED


def test_expired_lease_on_final_attempt_marks_failed(queue, clock):
    task = queue.enqueue(spec(max_attempts=1))
    queue.claim("dead-worker", 60)
    clock.advance(61)

    assert queue.claim("w2", 60) is None
    failed = queue.get(task.id)
    assert failed.status is TaskStatus.FAILED
    assert "lease expired" in failed.last_error


def test_record_attempt_accumulates_usage(queue, clock):
    task = queue.enqueue(spec())
    for attempt, cost in ((1, 0.25), (2, 0.5)):
        queue.record_attempt(
            AttemptRecord(
                task_id=task.id,
                attempt=attempt,
                provider="mock",
                started_at=clock(),
                finished_at=clock(),
                provider_success=True,
                tests_passed=False,
                usage=Usage(100, 50, cost),
            )
        )

    stored = queue.get(task.id)
    assert stored.usage == Usage(200, 100, 0.75)
    assert [a.attempt for a in queue.attempts(task.id)] == [1, 2]
    assert queue.usage_since(clock() - 1).cost_usd == pytest.approx(0.75)
    assert queue.usage_since(clock() + 1).cost_usd == 0


def test_finish_requires_running_state(queue):
    task = queue.enqueue(spec())
    with pytest.raises(LeaseLostError):
        queue.complete(task.id, "w", "never claimed")


@pytest.mark.parametrize(
    "overrides",
    [{"prompt": "  "}, {"max_attempts": 0}, {"budget_usd": -1}, {"test_command": ()}, {"workspace": ""}],
)
def test_task_spec_validation(overrides):
    with pytest.raises(ValueError):
        spec(**overrides)
