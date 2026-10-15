"""SQLite-backed persistent task queue (local/MVP backend).

Each operation opens its own connection, so the queue is safe to share across
threads and processes. Writes use ``BEGIN IMMEDIATE`` to serialize claims.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from ai_factory.models import AttemptRecord, Task, TaskSpec, TaskStatus, Usage
from ai_factory.queue.base import LeaseLostError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id               TEXT PRIMARY KEY,
    prompt           TEXT    NOT NULL,
    workspace        TEXT    NOT NULL,
    provider         TEXT    NOT NULL,
    test_command     TEXT,
    max_attempts     INTEGER NOT NULL,
    budget_usd       REAL,
    priority         INTEGER NOT NULL DEFAULT 0,
    status           TEXT    NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    created_at       REAL    NOT NULL,
    updated_at       REAL    NOT NULL,
    available_at     REAL    NOT NULL,
    lease_expires_at REAL,
    worker_id        TEXT,
    last_error       TEXT,
    result           TEXT,
    input_tokens     INTEGER NOT NULL DEFAULT 0,
    output_tokens    INTEGER NOT NULL DEFAULT 0,
    cost_usd         REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_claim ON tasks (status, available_at, priority);

CREATE TABLE IF NOT EXISTS attempts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          TEXT    NOT NULL REFERENCES tasks (id),
    attempt          INTEGER NOT NULL,
    provider         TEXT    NOT NULL,
    worker_id        TEXT,
    started_at       REAL    NOT NULL,
    finished_at      REAL    NOT NULL,
    provider_success INTEGER NOT NULL,
    tests_passed     INTEGER,
    input_tokens     INTEGER NOT NULL DEFAULT 0,
    output_tokens    INTEGER NOT NULL DEFAULT 0,
    cost_usd         REAL    NOT NULL DEFAULT 0,
    output           TEXT,
    test_output      TEXT,
    error            TEXT
);
CREATE INDEX IF NOT EXISTS idx_attempts_task ON attempts (task_id);
CREATE INDEX IF NOT EXISTS idx_attempts_finished ON attempts (finished_at);
"""


class SqliteTaskQueue:
    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    @property
    def path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------ helpers
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> Task:
        test_command = json.loads(row["test_command"]) if row["test_command"] else None
        return Task(
            id=row["id"],
            prompt=row["prompt"],
            workspace=row["workspace"],
            provider=row["provider"],
            test_command=tuple(test_command) if test_command else None,
            max_attempts=row["max_attempts"],
            budget_usd=row["budget_usd"],
            priority=row["priority"],
            status=TaskStatus(row["status"]),
            attempts=row["attempts"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            available_at=row["available_at"],
            lease_expires_at=row["lease_expires_at"],
            worker_id=row["worker_id"],
            last_error=row["last_error"],
            result=row["result"],
            usage=Usage(row["input_tokens"], row["output_tokens"], row["cost_usd"]),
        )

    @staticmethod
    def _require_owned(cursor: sqlite3.Cursor, task_id: str, worker_id: str) -> None:
        if cursor.rowcount == 0:
            raise LeaseLostError(f"worker {worker_id} does not hold the lease on task {task_id}")

    # --------------------------------------------------------------- operations
    def enqueue(self, spec: TaskSpec) -> Task:
        now = self._clock()
        task_id = uuid.uuid4().hex
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO tasks (id, prompt, workspace, provider, test_command, max_attempts,
                                   budget_usd, priority, status, attempts, created_at, updated_at,
                                   available_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    task_id,
                    spec.prompt,
                    spec.workspace,
                    spec.provider,
                    json.dumps(list(spec.test_command)) if spec.test_command else None,
                    spec.max_attempts,
                    spec.budget_usd,
                    spec.priority,
                    TaskStatus.PENDING.value,
                    now,
                    now,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._row_to_task(row)

    def claim(self, worker_id: str, lease_seconds: float) -> Task | None:
        now = self._clock()
        with self._transaction() as conn:
            # Tasks whose worker died on the final attempt can never be retried.
            conn.execute(
                """
                UPDATE tasks
                   SET status = ?, lease_expires_at = NULL, updated_at = ?,
                       last_error = COALESCE(last_error || char(10), '') ||
                                    'lease expired during final attempt (worker lost)'
                 WHERE status = ? AND lease_expires_at < ? AND attempts >= max_attempts
                """,
                (TaskStatus.FAILED.value, now, TaskStatus.RUNNING.value, now),
            )
            row = conn.execute(
                """
                SELECT id FROM tasks
                 WHERE (status = ? AND available_at <= ?)
                    OR (status = ? AND lease_expires_at < ?)
                 ORDER BY priority DESC, created_at ASC
                 LIMIT 1
                """,
                (TaskStatus.PENDING.value, now, TaskStatus.RUNNING.value, now),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE tasks
                   SET status = ?, attempts = attempts + 1, worker_id = ?,
                       lease_expires_at = ?, updated_at = ?
                 WHERE id = ?
                """,
                (TaskStatus.RUNNING.value, worker_id, now + lease_seconds, now, row["id"]),
            )
            claimed = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()
        return self._row_to_task(claimed)

    def complete(self, task_id: str, worker_id: str, result: str) -> None:
        self._finish(task_id, worker_id, TaskStatus.SUCCEEDED, result=result, error=None)

    def fail(self, task_id: str, worker_id: str, error: str) -> None:
        self._finish(task_id, worker_id, TaskStatus.FAILED, result=None, error=error)

    def _finish(
        self, task_id: str, worker_id: str, status: TaskStatus, result: str | None, error: str | None
    ) -> None:
        with self._transaction() as conn:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status = ?, result = COALESCE(?, result), last_error = COALESCE(?, last_error),
                       lease_expires_at = NULL, updated_at = ?
                 WHERE id = ? AND worker_id = ? AND status = ?
                """,
                (status.value, result, error, self._clock(), task_id, worker_id, TaskStatus.RUNNING.value),
            )
            self._require_owned(cur, task_id, worker_id)

    def retry(self, task_id: str, worker_id: str, error: str, available_at: float) -> None:
        with self._transaction() as conn:
            cur = conn.execute(
                """
                UPDATE tasks
                   SET status = ?, last_error = ?, available_at = ?,
                       lease_expires_at = NULL, updated_at = ?
                 WHERE id = ? AND worker_id = ? AND status = ?
                """,
                (
                    TaskStatus.PENDING.value,
                    error,
                    available_at,
                    self._clock(),
                    task_id,
                    worker_id,
                    TaskStatus.RUNNING.value,
                ),
            )
            self._require_owned(cur, task_id, worker_id)

    def record_attempt(self, record: AttemptRecord) -> None:
        u = record.usage
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO attempts (task_id, attempt, provider, worker_id, started_at, finished_at,
                                      provider_success, tests_passed, input_tokens, output_tokens,
                                      cost_usd, output, test_output, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.task_id,
                    record.attempt,
                    record.provider,
                    record.worker_id,
                    record.started_at,
                    record.finished_at,
                    int(record.provider_success),
                    None if record.tests_passed is None else int(record.tests_passed),
                    u.input_tokens,
                    u.output_tokens,
                    u.cost_usd,
                    record.output,
                    record.test_output,
                    record.error,
                ),
            )
            conn.execute(
                """
                UPDATE tasks
                   SET input_tokens = input_tokens + ?, output_tokens = output_tokens + ?,
                       cost_usd = cost_usd + ?, updated_at = ?
                 WHERE id = ?
                """,
                (u.input_tokens, u.output_tokens, u.cost_usd, self._clock(), record.task_id),
            )

    # ------------------------------------------------------------------ queries
    def get(self, task_id: str) -> Task | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def list_tasks(self, status: TaskStatus | None = None, limit: int = 100) -> list[Task]:
        with self._connect() as conn:
            if status is None:
                rows = conn.execute(
                    "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status.value, limit),
                ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def attempts(self, task_id: str) -> list[AttemptRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM attempts WHERE task_id = ? ORDER BY attempt ASC, id ASC", (task_id,)
            ).fetchall()
        return [
            AttemptRecord(
                task_id=r["task_id"],
                attempt=r["attempt"],
                provider=r["provider"],
                worker_id=r["worker_id"],
                started_at=r["started_at"],
                finished_at=r["finished_at"],
                provider_success=bool(r["provider_success"]),
                tests_passed=None if r["tests_passed"] is None else bool(r["tests_passed"]),
                usage=Usage(r["input_tokens"], r["output_tokens"], r["cost_usd"]),
                output=r["output"] or "",
                test_output=r["test_output"] or "",
                error=r["error"],
            )
            for r in rows
        ]

    def usage_since(self, since: float) -> Usage:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),
                       COALESCE(SUM(cost_usd), 0)
                  FROM attempts WHERE finished_at >= ?
                """,
                (since,),
            ).fetchone()
        return Usage(int(row[0]), int(row[1]), float(row[2]))

    def status_counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status").fetchall()
        return {r[0]: r[1] for r in rows}
