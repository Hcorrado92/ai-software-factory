"""Domain models shared by the queue, the supervisor and the providers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


TERMINAL_STATUSES = frozenset({TaskStatus.SUCCEEDED, TaskStatus.FAILED})


@dataclass(frozen=True)
class Usage:
    """Resource consumption of one or more provider calls."""

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
        )


@dataclass(frozen=True)
class TaskSpec:
    """What a client submits to the factory."""

    prompt: str
    workspace: str
    provider: str = "claude-code"
    test_command: tuple[str, ...] | None = None
    max_attempts: int = 3
    budget_usd: float | None = None
    priority: int = 0

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise ValueError("prompt must not be empty")
        if not self.workspace:
            raise ValueError("workspace must not be empty")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.budget_usd is not None and self.budget_usd < 0:
            raise ValueError("budget_usd must be >= 0")
        if self.test_command is not None and len(self.test_command) == 0:
            raise ValueError("test_command must not be an empty sequence")


@dataclass(frozen=True)
class Task:
    """A task as persisted in the queue."""

    id: str
    prompt: str
    workspace: str
    provider: str
    test_command: tuple[str, ...] | None
    max_attempts: int
    budget_usd: float | None
    priority: int
    status: TaskStatus
    attempts: int
    created_at: float
    updated_at: float
    available_at: float
    lease_expires_at: float | None = None
    worker_id: str | None = None
    last_error: str | None = None
    result: str | None = None
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True)
class AttemptRecord:
    """Audit record of a single attempt (provider call + optional test run)."""

    task_id: str
    attempt: int
    provider: str
    started_at: float
    finished_at: float
    provider_success: bool
    tests_passed: bool | None
    usage: Usage
    output: str = ""
    test_output: str = ""
    error: str | None = None
    worker_id: str | None = None
