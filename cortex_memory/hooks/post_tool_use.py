#!/usr/bin/env python3
"""PostToolUse hook: capture verified tool outcomes as episodic evidence.

Only Bash/Edit/Write get captured by default -- most tool calls are noise, and
capturing every single one would defeat the "do not persist every token"
design rule. Extend TRACKED_TOOLS if more signal is needed.
"""

import json
import sys

from _shared import read_hook_input, safe_namespace, run_safely

sys.path.insert(0, str(__file__).rsplit("cortex_memory", 1)[0])
from cortex_memory import capture, db  # noqa: E402

TRACKED_TOOLS = {"Bash", "Edit", "Write"}
MAX_CONTENT_CHARS = 2000


def main():
    payload = read_hook_input()
    tool_name = payload.get("tool_name")
    if tool_name not in TRACKED_TOOLS:
        sys.exit(0)
    cwd = payload.get("cwd")
    tool_input = payload.get("tool_input", {})
    tool_response = payload.get("tool_response", {})

    is_error = bool(isinstance(tool_response, dict) and tool_response.get("error"))
    summary = f"{tool_name} {'failed' if is_error else 'succeeded'}: " \
              f"{json.dumps(tool_input)[:200]}"
    content = summary + " -- " + json.dumps(tool_response)[:MAX_CONTENT_CHARS]

    try:
        ns = safe_namespace(cwd)
        conn = db.connect()
        try:
            capture.ingest(
                conn, ns, "tool_failure" if is_error else "tool_result", content,
                summary=summary, source_type="tool_observed",
                tool=(tool_name, tool_input, tool_response),
                importance=0.6 if is_error else 0.4,
            )
        finally:
            conn.close()
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    run_safely(main)
