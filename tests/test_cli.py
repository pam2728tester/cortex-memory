import json
import os
import subprocess
import sys


def cortex(tmp_path, *args):
    env = {**os.environ, "CORTEX_MEMORY_DIR": str(tmp_path / "data")}
    return subprocess.run([sys.executable, "-m", "cortex_memory.cli", *args],
                          capture_output=True, text=True, env=env, timeout=60)


def test_capture_json_serialisable_and_query_cwd(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "src").mkdir()
    p = cortex(tmp_path, "capture", "user_message", "We decided to use SQLite.", "--source", "user_provided",
               "--namespace", "project:" + (repo.as_posix().lower() if os.name == "nt" else repo.as_posix()))
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["stats"]["accepted"] == 1
    q = cortex(tmp_path, "query", "sqlite database", "--cwd", str(repo / "src"))
    assert q.returncode == 0, q.stderr
    assert "SQLite" in q.stdout


def test_consolidate_command_runs(tmp_path):
    assert cortex(tmp_path, "init").returncode == 0
    p = cortex(tmp_path, "consolidate-namespaces", "--no-snapshot")
    assert p.returncode == 0 and "namespaces_merged" in p.stdout
