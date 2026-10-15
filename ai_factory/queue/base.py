"""Queue contract. Any backend (SQLite, Azure Service Bus + Azure SQL, ...) must honour it.

Semantics:
- ``claim`` is atomic: a task is handed to at most one worker at a time.
- A claim grants a *lease*. If the worker dies, the lease expires and the task
  becomes claimable again (at-least-once delivery, like Service Bus peek-lock).
- ``attempts`` is incremented on every claim, so crashed attempts still count
  toward ``max_attempts``.
- ``complete`` / ``retry`` / ``fail`` only succeed for the worker holding the
  lease; otherwise they raise ``LeaseLostError``.
"""

from __future__ import annotations

from typing import Protocol

from ai_factory.models import AttemptRecord, Task, TaskSpec, TaskStatus, Usage


class LeaseLostError(RuntimeError):
    """The worker no longer owns the task (lease expired and was re-claimed, or task finished)."""


class TaskQueue(Protocol):
    def enqueue(self, spec: TaskSpec) -> Task: ...

    def claim(self, worker_id: str, lease_seconds: float) -> Task | None: ...

    def complete(self, task_id: str, worker_id: str, result: str) -> None: ...

    def retry(self, task_id: str, worker_id: str, error: str, available_at: float) -> None: ...

    def fail(self, task_id: str, worker_id: str, error: str) -> None: ...

    def record_attempt(self, record: AttemptRecord) -> None: ...

    def get(self, task_id: str) -> Task | None: ...

    def list_tasks(self, status: TaskStatus | None = None, limit: int = 100) -> list[Task]: ...

    def attempts(self, task_id: str) -> list[AttemptRecord]: ...

    def usage_since(self, since: float) -> Usage: ...
