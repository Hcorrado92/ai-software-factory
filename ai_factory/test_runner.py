"""Runs a task's verification command (e.g. ``pytest -q``) inside its workspace."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TestRunResult:
    __test__ = False  # not a pytest test class

    passed: bool
    exit_code: int | None
    output: str
    duration_s: float
    timed_out: bool = False


def _tail(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else "...[truncated]...\n" + text[-max_chars:]


class TestCommandRunner:
    """Executes the command without a shell (argv list), captures output, enforces a timeout."""

    __test__ = False

    def __init__(
        self,
        timeout_s: float = 600,
        max_output_chars: int = 20_000,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ) -> None:
        self.timeout_s = timeout_s
        self.max_output_chars = max_output_chars
        self._runner = runner or subprocess.run

    def run(self, command: Sequence[str], cwd: Path) -> TestRunResult:
        started = time.monotonic()
        try:
            proc = self._runner(
                list(command),
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            partial = _to_text(exc.stdout) + _to_text(exc.stderr)
            return TestRunResult(
                passed=False,
                exit_code=None,
                output=_tail(f"{partial}\n[timed out after {self.timeout_s}s]", self.max_output_chars),
                duration_s=time.monotonic() - started,
                timed_out=True,
            )
        except (FileNotFoundError, PermissionError) as exc:
            return TestRunResult(
                passed=False,
                exit_code=None,
                output=f"could not start test command {list(command)!r}: {exc}",
                duration_s=time.monotonic() - started,
            )
        output = (proc.stdout or "") + (proc.stderr or "")
        return TestRunResult(
            passed=proc.returncode == 0,
            exit_code=proc.returncode,
            output=_tail(output, self.max_output_chars),
            duration_s=time.monotonic() - started,
        )


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
