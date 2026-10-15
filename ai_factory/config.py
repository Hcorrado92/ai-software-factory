"""Settings loaded from environment variables (12-factor; maps 1:1 to Azure App Settings)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ai_factory.providers.claude_code import DEFAULT_ALLOWED_TOOLS

_NONE = {"", "none", "null", "off"}


def _opt_float(env: Mapping[str, str], key: str, default: float | None) -> float | None:
    raw = env.get(key)
    if raw is None:
        return default
    return None if raw.strip().lower() in _NONE else float(raw)


def _opt_int(env: Mapping[str, str], key: str, default: int | None) -> int | None:
    raw = env.get(key)
    if raw is None:
        return default
    return None if raw.strip().lower() in _NONE else int(raw)


@dataclass(frozen=True)
class Settings:
    db_path: Path = Path("data/factory.db")
    workspace_root: Path = Path("workspaces")
    claude_binary: str = "claude"
    claude_allowed_tools: tuple[str, ...] = DEFAULT_ALLOWED_TOOLS
    claude_max_turns: int = 20
    claude_model: str | None = None
    provider_timeout_s: float = 900.0
    test_timeout_s: float = 600.0
    max_attempts: int = 3
    backoff_base_s: float = 30.0
    backoff_max_s: float = 900.0
    poll_interval_s: float = 5.0
    daily_budget_usd: float | None = 20.0
    default_task_budget_usd: float | None = 2.0
    max_tokens_per_task: int | None = None
    log_level: str = "INFO"
    log_format: str = "json"

    @property
    def lease_seconds(self) -> float:
        # Lease must outlive a full attempt (provider + tests) so live work is never re-claimed.
        return self.provider_timeout_s + self.test_timeout_s + 120.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        d = cls()
        tools = env.get("ASF_CLAUDE_ALLOWED_TOOLS")
        return cls(
            db_path=Path(env.get("ASF_DB_PATH", str(d.db_path))),
            workspace_root=Path(env.get("ASF_WORKSPACE_ROOT", str(d.workspace_root))),
            claude_binary=env.get("ASF_CLAUDE_BIN", d.claude_binary),
            claude_allowed_tools=(
                tuple(t.strip() for t in tools.split(",") if t.strip()) if tools else d.claude_allowed_tools
            ),
            claude_max_turns=int(env.get("ASF_CLAUDE_MAX_TURNS", d.claude_max_turns)),
            claude_model=env.get("ASF_CLAUDE_MODEL") or None,
            provider_timeout_s=float(env.get("ASF_PROVIDER_TIMEOUT_S", d.provider_timeout_s)),
            test_timeout_s=float(env.get("ASF_TEST_TIMEOUT_S", d.test_timeout_s)),
            max_attempts=int(env.get("ASF_MAX_ATTEMPTS", d.max_attempts)),
            backoff_base_s=float(env.get("ASF_BACKOFF_BASE_S", d.backoff_base_s)),
            backoff_max_s=float(env.get("ASF_BACKOFF_MAX_S", d.backoff_max_s)),
            poll_interval_s=float(env.get("ASF_POLL_INTERVAL_S", d.poll_interval_s)),
            daily_budget_usd=_opt_float(env, "ASF_DAILY_BUDGET_USD", d.daily_budget_usd),
            default_task_budget_usd=_opt_float(env, "ASF_TASK_BUDGET_USD", d.default_task_budget_usd),
            max_tokens_per_task=_opt_int(env, "ASF_MAX_TOKENS_PER_TASK", d.max_tokens_per_task),
            log_level=env.get("ASF_LOG_LEVEL", d.log_level),
            log_format=env.get("ASF_LOG_FORMAT", d.log_format),
        )
