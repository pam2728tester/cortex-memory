#!/usr/bin/env python3
"""UserPromptSubmit hook: record user intent as an episode, retrieve relevant memories."""

import sys

from _shared import additional_context, read_hook_input, safe_namespace

sys.path.insert(0, str(__file__).rsplit("cortex_memory", 1)[0])
from cortex_memory import capture, db, retrieve  # noqa: E402


def main():
    payload = read_hook_input()
    prompt = payload.get("prompt", "")
    cwd = payload.get("cwd")
    if not prompt:
        sys.exit(0)
    try:
        ns = safe_namespace(cwd)
        conn = db.connect()
        try:
            capture.ingest(conn, ns, "user_message", prompt, source_type="user_provided")
            result = retrieve.query(conn, ns, prompt, limit=8)
        finally:
            conn.close()
        if result["facts"] or result["episodes"]:
            additional_context("UserPromptSubmit", result["text"])
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
