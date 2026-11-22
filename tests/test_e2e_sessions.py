"""Three consecutive 'sessions' driven only through the real hook scripts."""

import json

from test_hooks import run_hook


def injected(proc) -> str:
    if not proc.stdout.strip():
        return ""
    return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


def test_three_session_scenario(tmp_path):
    data = tmp_path / "data"
    a, b = tmp_path / "cortex", tmp_path / "other"
    for d in (a, b):
        (d / ".git").mkdir(parents=True)
        (d / "src").mkdir()

    # Session 1 (project A): user states a decision and a preference; tests fail, file edited, tests pass.
    run_hook("user_prompt_submit.py", {"cwd": str(a / "src"),
             "prompt": "We decided to use SQLite for Cortex. Always run pytest before committing."}, data)
    run_hook("post_tool_use.py", {"cwd": str(a), "tool_name": "Bash", "tool_input": {"command": "pytest -q"},
             "tool_response": {"stdout": "Traceback (most recent call last):\nModuleNotFoundError: No module named 'foo'"}}, data)
    run_hook("post_tool_use.py", {"cwd": str(a), "tool_name": "Edit", "tool_input": {"file_path": str(a / "app.py")},
             "tool_response": {}}, data)
    run_hook("post_tool_use.py", {"cwd": str(a), "tool_name": "Bash", "tool_input": {"command": "pytest -q"},
             "tool_response": {"stdout": "25 passed in 2s"}}, data)
    run_hook("session_end.py", {"cwd": str(a)}, data)

    # Session 2 (project B): preference follows the user; project A's decision must not leak.
    s2 = injected(run_hook("session_start.py", {"cwd": str(b)}, data))
    assert "pytest before committing" in s2 and "(global)" in s2
    q = injected(run_hook("user_prompt_submit.py", {"cwd": str(b), "prompt": "what database sqlite should we use"}, data))
    assert "SQLite" not in q

    # Session 3 (project A, subdirectory): decision and the error->fix are recalled, no stale error.
    q3 = injected(run_hook("user_prompt_submit.py", {"cwd": str(a / "src"),
                  "prompt": "which database sqlite and why did pytest fail with ModuleNotFoundError"}, data))
    assert "decided to use SQLite" in q3
    assert "[error-fix]" in q3 and "app.py" in q3
    assert "[error] `pytest -q` failed" not in q3
