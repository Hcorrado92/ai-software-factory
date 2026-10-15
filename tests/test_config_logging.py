from __future__ import annotations

import io
import json
import logging
from pathlib import Path

from ai_factory.config import Settings
from ai_factory.logging_config import setup_logging


def test_settings_defaults():
    s = Settings.from_env({})
    assert s.db_path == Path("data/factory.db")
    assert s.daily_budget_usd == 20.0
    assert s.lease_seconds > s.provider_timeout_s + s.test_timeout_s


def test_settings_from_env():
    s = Settings.from_env(
        {
            "ASF_DB_PATH": "/tmp/x.db",
            "ASF_DAILY_BUDGET_USD": "none",
            "ASF_TASK_BUDGET_USD": "0.75",
            "ASF_MAX_TOKENS_PER_TASK": "50000",
            "ASF_CLAUDE_ALLOWED_TOOLS": "Read, Edit ,Bash(pytest:*)",
            "ASF_MAX_ATTEMPTS": "5",
        }
    )
    assert s.db_path == Path("/tmp/x.db")
    assert s.daily_budget_usd is None
    assert s.default_task_budget_usd == 0.75
    assert s.max_tokens_per_task == 50000
    assert s.claude_allowed_tools == ("Read", "Edit", "Bash(pytest:*)")
    assert s.max_attempts == 5


def test_json_logs_include_context_fields():
    stream = io.StringIO()
    setup_logging("INFO", "json", stream=stream)
    setup_logging("INFO", "json", stream=stream)  # idempotent: no duplicate handler

    logging.getLogger("ai_factory.test").info("task claimed", extra={"task_id": "abc", "attempt": 2})

    lines = stream.getvalue().strip().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["msg"] == "task claimed"
    assert record["task_id"] == "abc"
    assert record["attempt"] == 2
    assert record["level"] == "INFO"


def test_text_logs():
    stream = io.StringIO()
    setup_logging("DEBUG", "text", stream=stream)
    logging.getLogger("ai_factory.test").debug("hello", extra={"task_id": "t"})
    assert "hello" in stream.getvalue() and "'task_id': 't'" in stream.getvalue()
