"""Consumption control: global daily spend cap plus per-task cost/token caps."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from ai_factory.models import Task
from ai_factory.queue.base import TaskQueue


@dataclass(frozen=True)
class BudgetPolicy:
    daily_usd: float | None = None
    default_task_usd: float | None = None
    max_tokens_per_task: int | None = None


class BudgetGuard:
    def __init__(
        self, policy: BudgetPolicy, queue: TaskQueue, *, clock: Callable[[], float] = time.time
    ) -> None:
        self.policy = policy
        self._queue = queue
        self._clock = clock

    def _day_start(self) -> float:
        now = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
        return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    def daily_spent_usd(self) -> float:
        return self._queue.usage_since(self._day_start()).cost_usd

    def daily_exhausted(self) -> bool:
        limit = self.policy.daily_usd
        return limit is not None and self.daily_spent_usd() >= limit

    def task_limit_usd(self, task: Task) -> float | None:
        return task.budget_usd if task.budget_usd is not None else self.policy.default_task_usd

    def remaining_task_usd(self, task: Task) -> float | None:
        limit = self.task_limit_usd(task)
        return None if limit is None else max(limit - task.usage.cost_usd, 0.0)

    def task_exhausted_reason(self, task: Task) -> str | None:
        """Why the task may not spend more, or ``None`` if it may."""
        limit = self.task_limit_usd(task)
        if limit is not None and task.usage.cost_usd >= limit:
            return f"task budget exhausted: spent ${task.usage.cost_usd:.4f} of ${limit:.4f}"
        max_tokens = self.policy.max_tokens_per_task
        if max_tokens is not None and task.usage.total_tokens >= max_tokens:
            return f"task token cap exhausted: used {task.usage.total_tokens} of {max_tokens}"
        return None
