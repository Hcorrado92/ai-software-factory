"""Claude Code CLI provider (headless ``claude -p``).

Safety policy enforced here, not left to callers:
- Never passes ``--dangerously-skip-permissions`` or ``bypassPermissions``.
- Tools are an explicit allow-list; bare ``Bash`` (unrestricted shell) is rejected,
  only scoped patterns such as ``Bash(pytest:*)`` are accepted.
- The prompt goes through stdin, never through argv (no shell/argument injection).
- Refuses to start when already running inside a Claude Code session
  (``CLAUDECODE`` env var) unless explicitly allowed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ai_factory.models import Usage
from ai_factory.providers.base import AIProvider, ProviderError, ProviderRequest, ProviderResult

DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = ("Read", "Edit", "Write", "Glob", "Grep")

_FORBIDDEN_ARGS = frozenset(
    {
        "--dangerously-skip-permissions",
        "--allow-dangerously-skip-permissions",
        "bypassPermissions",
    }
)

Runner = Callable[..., subprocess.CompletedProcess]


def _default_runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(*args, **kwargs)


class ClaudeCodeProvider(AIProvider):
    name = "claude-code"

    def __init__(
        self,
        *,
        binary: str = "claude",
        allowed_tools: Sequence[str] = DEFAULT_ALLOWED_TOOLS,
        max_turns: int = 20,
        model: str | None = None,
        extra_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
        runner: Runner | None = None,
        allow_nested: bool = False,
    ) -> None:
        for arg in extra_args:
            if any(forbidden in arg for forbidden in _FORBIDDEN_ARGS):
                raise ValueError(f"forbidden Claude Code argument: {arg!r}")
        for tool in allowed_tools:
            if tool.strip() == "Bash" or tool.strip() == "Bash(*)":
                raise ValueError("unrestricted Bash is not allowed; use a scoped pattern like Bash(pytest:*)")
        if max_turns < 1:
            raise ValueError("max_turns must be >= 1")
        self.binary = binary
        self.allowed_tools = tuple(allowed_tools)
        self.max_turns = max_turns
        self.model = model
        self.extra_args = tuple(extra_args)
        self._env = dict(env) if env is not None else None
        self._runner = runner
        self.allow_nested = allow_nested

    def build_command(self) -> list[str]:
        cmd = [self.binary, "-p", "--output-format", "json", "--max-turns", str(self.max_turns)]
        if self.allowed_tools:
            cmd += ["--allowedTools", ",".join(self.allowed_tools)]
        if self.model:
            cmd += ["--model", self.model]
        cmd += list(self.extra_args)
        return cmd

    def run(self, request: ProviderRequest) -> ProviderResult:
        if not self.allow_nested and os.environ.get("CLAUDECODE"):
            raise ProviderError(
                "refusing to launch Claude Code from inside a Claude Code session (CLAUDECODE is set)",
                retryable=False,
            )
        cmd = self.build_command()
        cmd[0] = shutil.which(cmd[0]) or cmd[0]
        runner = self._runner or _default_runner
        try:
            proc = runner(
                cmd,
                input=request.prompt,
                cwd=str(request.workspace),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=request.timeout_s,
                check=False,
                env=self._env,
            )
        except FileNotFoundError as exc:
            raise ProviderError(f"Claude Code binary not found: {self.binary}", retryable=False) from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"Claude Code timed out after {request.timeout_s}s", retryable=True) from exc

        payload = _parse_json_output(proc.stdout or "")
        usage = _parse_usage(payload) if payload else Usage()

        if proc.returncode != 0 or payload is None or payload.get("is_error"):
            detail = (payload or {}).get("result") or (proc.stderr or proc.stdout or "").strip()[-2000:]
            if payload is None and proc.returncode == 0:
                detail = f"could not parse JSON output: {(proc.stdout or '')[-500:]!r}"
            raise ProviderError(
                f"Claude Code failed (exit={proc.returncode}, "
                f"subtype={(payload or {}).get('subtype')}): {detail}",
                retryable=True,
                usage=usage,
            )

        return ProviderResult(
            output=str(payload.get("result", "")),
            usage=usage,
            metadata={
                "session_id": payload.get("session_id"),
                "num_turns": payload.get("num_turns"),
                "duration_ms": payload.get("duration_ms"),
            },
        )


def _parse_json_output(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    candidates = [text] + [line for line in reversed(text.splitlines()) if line.strip().startswith("{")]
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _parse_usage(payload: Mapping[str, Any]) -> Usage:
    raw = payload.get("usage") or {}
    input_tokens = sum(
        int(raw.get(key) or 0)
        for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    cost = payload.get("total_cost_usd", payload.get("cost_usd", 0.0)) or 0.0
    return Usage(
        input_tokens=input_tokens,
        output_tokens=int(raw.get("output_tokens") or 0),
        cost_usd=float(cost),
    )
