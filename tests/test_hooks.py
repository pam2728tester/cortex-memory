"""Run the real hook scripts as subprocesses with realistic Claude Code payloads."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "cortex_memory" / "hooks"


def run_hook(name, payload, data_dir, raw=None):
    env = {**os.environ, "CORTEX_MEMORY_DIR": str(data_dir), "CORTEX_DEBUG": "1"}
    return subprocess.run(
        [sys.executable, str(HOOKS / name)],
        input=raw if raw is not None else json.dumps(payload),
        capture_output=True, text=True, env=env, timeout=60,
    )


def rows(data_dir, sql):
    c = sqlite3.connect(str(data_dir / "cortex.db"))
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


def test_user_prompt_hook_extracts_fact(tmp_path):
    p = run_hook("user_prompt_submit.py", {
        "hook_event_name": "UserPromptSubmit", "cwd": str(tmp_path),
        "prompt": "We decided to use SQLite for storage. Always run pytest before committing.",
    }, tmp_path)
    assert p.returncode == 0
    preds = {r[0].split(":")[0] for r in rows(tmp_path, "select predicate from facts where status='active'")}
    assert preds == {"decision", "preference"}


def test_post_tool_use_hook_records_test_pass(tmp_path):
    p = run_hook("post_tool_use.py", {
        "hook_event_name": "PostToolUse", "cwd": str(tmp_path), "tool_name": "Bash",
        "tool_input": {"command": "pytest -q"},
        "tool_response": {"stdout": "25 passed in 3.1s", "stderr": "", "interrupted": False},
    }, tmp_path)
    assert p.returncode == 0
    objs = [r[0] for r in rows(tmp_path, "select object from facts")]
    assert objs == ["`pytest -q` runs the tests successfully"]


def test_hooks_survive_garbage_stdin(tmp_path):
    for name in ("user_prompt_submit.py", "post_tool_use.py", "session_start.py", "session_end.py"):
        for raw in ("", "not json", "[]", "null", '{"prompt": 5}', '{"tool_name": "Bash", "tool_input": "x"}'):
            p = run_hook(name, None, tmp_path, raw=raw)
            assert p.returncode == 0, (name, raw, p.stderr)


def test_noise_prompt_not_recorded(tmp_path):
    run_hook("user_prompt_submit.py", {"cwd": str(tmp_path),
             "prompt": "<task-notification>x</task-notification> We decided to use Redis."}, tmp_path)
    assert not (tmp_path / "cortex.db").exists() or rows(tmp_path, "select count(*) from episodes")[0][0] == 0


def test_debug_log_written_by_hook(tmp_path):
    run_hook("user_prompt_submit.py", {"cwd": str(tmp_path), "prompt": "We decided to use SQLite."}, tmp_path)
    log = (tmp_path / "capture_stats.jsonl").read_text()
    assert '"accepted": 1' in log and "SQLite" not in log
