"""Command line entry point: ``asf`` / ``python -m ai_factory``."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from ai_factory.budget import BudgetGuard, BudgetPolicy
from ai_factory.config import Settings
from ai_factory.logging_config import setup_logging
from ai_factory.models import Task, TaskSpec, TaskStatus
from ai_factory.providers.claude_code import ClaudeCodeProvider
from ai_factory.providers.mock import MockProvider
from ai_factory.providers.registry import ProviderRegistry
from ai_factory.queue.sqlite_queue import SqliteTaskQueue
from ai_factory.supervisor import Supervisor, SupervisorConfig
from ai_factory.test_runner import TestCommandRunner


def build_default_registry(settings: Settings) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(
        "claude-code",
        lambda: ClaudeCodeProvider(
            binary=settings.claude_binary,
            allowed_tools=settings.claude_allowed_tools,
            max_turns=settings.claude_max_turns,
            model=settings.claude_model,
        ),
    )
    registry.register("mock", lambda: MockProvider())
    # Future: registry.register("openai", lambda: OpenAIProvider(...)), "gemini", ...
    return registry


def build_supervisor(
    settings: Settings,
    queue: SqliteTaskQueue,
    registry: ProviderRegistry,
    worker_id: str | None = None,
) -> Supervisor:
    budget = BudgetGuard(
        BudgetPolicy(
            daily_usd=settings.daily_budget_usd,
            default_task_usd=settings.default_task_budget_usd,
            max_tokens_per_task=settings.max_tokens_per_task,
        ),
        queue,
    )
    config = SupervisorConfig(
        workspace_root=settings.workspace_root,
        provider_timeout_s=settings.provider_timeout_s,
        lease_seconds=settings.lease_seconds,
        backoff_base_s=settings.backoff_base_s,
        backoff_max_s=settings.backoff_max_s,
        poll_interval_s=settings.poll_interval_s,
    )
    return Supervisor(
        queue,
        registry,
        TestCommandRunner(timeout_s=settings.test_timeout_s),
        budget,
        config,
        worker_id=worker_id,
    )


def parse_test_command(raw: str) -> tuple[str, ...]:
    """Accept a JSON array (exact argv) or a shell-like string (split without invoking a shell)."""
    raw = raw.strip()
    if raw.startswith("["):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--test-cmd is not valid JSON ({exc}); received {raw!r}") from None
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError("--test-cmd JSON must be an array of strings")
        return tuple(value)
    if os.name == "nt":
        return tuple(part.strip('"') for part in shlex.split(raw, posix=False))
    return tuple(shlex.split(raw))


def _iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _task_json(task: Task, *, full: bool = False) -> dict:
    data = {
        "id": task.id,
        "status": task.status.value,
        "provider": task.provider,
        "attempts": task.attempts,
        "max_attempts": task.max_attempts,
        "priority": task.priority,
        "cost_usd": round(task.usage.cost_usd, 6),
        "tokens": task.usage.total_tokens,
        "created_at": _iso(task.created_at),
        "updated_at": _iso(task.updated_at),
    }
    if full:
        data |= {
            "prompt": task.prompt,
            "workspace": task.workspace,
            "test_command": list(task.test_command) if task.test_command else None,
            "budget_usd": task.budget_usd,
            "available_at": _iso(task.available_at),
            "worker_id": task.worker_id,
            "last_error": task.last_error,
            "result": task.result,
        }
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asf", description="AI Software Factory supervisor")
    parser.add_argument("--db", help="SQLite path (default: $ASF_DB_PATH or data/factory.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    enq = sub.add_parser("enqueue", help="add a task to the queue")
    group = enq.add_mutually_exclusive_group(required=True)
    group.add_argument("--prompt")
    group.add_argument("--prompt-file", type=Path)
    enq.add_argument("--workspace", required=True, help="directory (inside the workspace root) to work in")
    enq.add_argument("--provider", default="claude-code")
    enq.add_argument("--test-cmd", help='verification command, e.g. "python -m pytest -q" or JSON argv')
    enq.add_argument("--max-attempts", type=int)
    enq.add_argument("--budget-usd", type=float)
    enq.add_argument("--priority", type=int, default=0)

    run = sub.add_parser("run", help="start the supervisor")
    run.add_argument("--once", action="store_true", help="drain currently available tasks and exit")
    run.add_argument("--max-tasks", type=int)
    run.add_argument("--worker-id")

    status = sub.add_parser("status", help="show one task with its attempts")
    status.add_argument("task_id")

    lst = sub.add_parser("list", help="list tasks")
    lst.add_argument("--status", choices=[s.value for s in TaskStatus])
    lst.add_argument("--limit", type=int, default=50)

    sub.add_parser("usage", help="consumption summary for today (UTC)")
    return parser


def main(argv: list[str] | None = None, *, registry: ProviderRegistry | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    setup_logging(settings.log_level, settings.log_format)
    queue = SqliteTaskQueue(args.db or settings.db_path)

    if args.command == "enqueue":
        prompt = args.prompt if args.prompt is not None else args.prompt_file.read_text(encoding="utf-8")
        try:
            spec = TaskSpec(
                prompt=prompt,
                workspace=args.workspace,
                provider=args.provider,
                test_command=parse_test_command(args.test_cmd) if args.test_cmd else None,
                max_attempts=settings.max_attempts if args.max_attempts is None else args.max_attempts,
                budget_usd=args.budget_usd,
                priority=args.priority,
            )
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(_task_json(queue.enqueue(spec)), indent=2))
        return 0

    if args.command == "run":
        supervisor = build_supervisor(
            settings, queue, registry or build_default_registry(settings), worker_id=args.worker_id
        )
        try:
            if args.once:
                processed = supervisor.drain(max_tasks=args.max_tasks)
            else:
                processed = supervisor.run_forever(max_tasks=args.max_tasks)
        except KeyboardInterrupt:
            return 130
        print(json.dumps({"processed": processed, "status_counts": queue.status_counts()}, indent=2))
        return 0

    if args.command == "status":
        task = queue.get(args.task_id)
        if task is None:
            print(f"error: task {args.task_id} not found", file=sys.stderr)
            return 1
        data = _task_json(task, full=True)
        data["attempt_log"] = [
            asdict(a) | {"started_at": _iso(a.started_at), "finished_at": _iso(a.finished_at)}
            for a in queue.attempts(task.id)
        ]
        print(json.dumps(data, indent=2, default=str))
        return 0

    if args.command == "list":
        status = TaskStatus(args.status) if args.status else None
        print(json.dumps([_task_json(t) for t in queue.list_tasks(status, args.limit)], indent=2))
        return 0

    if args.command == "usage":
        budget = BudgetGuard(BudgetPolicy(daily_usd=settings.daily_budget_usd), queue)
        day_start = datetime.fromtimestamp(time.time(), tz=timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        usage = queue.usage_since(day_start.timestamp())
        print(
            json.dumps(
                {
                    "since": day_start.isoformat(),
                    "cost_usd": round(usage.cost_usd, 6),
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                    "daily_budget_usd": settings.daily_budget_usd,
                    "daily_budget_exhausted": budget.daily_exhausted(),
                    "status_counts": queue.status_counts(),
                },
                indent=2,
            )
        )
        return 0

    return 2  # unreachable: argparse enforces a command
