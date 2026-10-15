from __future__ import annotations

import sys

from ai_factory.test_runner import TestCommandRunner


def test_passing_command(tmp_path):
    result = TestCommandRunner().run([sys.executable, "-c", "print('ok')"], tmp_path)
    assert result.passed and result.exit_code == 0 and "ok" in result.output


def test_failing_command_captures_stderr(tmp_path):
    result = TestCommandRunner().run([sys.executable, "-c", "import sys; sys.stderr.write('bad'); sys.exit(3)"], tmp_path)
    assert not result.passed and result.exit_code == 3 and "bad" in result.output


def test_timeout(tmp_path):
    result = TestCommandRunner(timeout_s=0.5).run([sys.executable, "-c", "import time; time.sleep(10)"], tmp_path)
    assert not result.passed and result.timed_out and result.exit_code is None


def test_missing_executable(tmp_path):
    result = TestCommandRunner().run(["definitely-not-a-real-binary-xyz"], tmp_path)
    assert not result.passed and "could not start" in result.output


def test_output_is_truncated_keeping_the_tail(tmp_path):
    script = "print('A' * 5000); print('THE-END')"
    result = TestCommandRunner(max_output_chars=100).run([sys.executable, "-c", script], tmp_path)
    assert "truncated" in result.output and "THE-END" in result.output
    assert len(result.output) < 200


def test_runs_in_workspace_directory(tmp_path):
    (tmp_path / "marker.txt").write_text("x", encoding="utf-8")
    check = "import os; assert os.path.exists('marker.txt')"
    assert TestCommandRunner().run([sys.executable, "-c", check], tmp_path).passed
