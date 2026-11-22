#!/usr/bin/env python3
"""UserPromptSubmit hook: record user intent as an episode, retrieve relevant memories."""

import sys

from _shared import additional_context, read_hook_input, safe_namespace, run_safely

sys.path.insert(0, str(__file__).rsplit("cortex_memory", 1)[0])
from cortex_memory import capture, db, retrieve  # noqa: E402

# Harness-injected noise, not real user intent: background-task notifications
# and system reminders get relayed through the same UserPromptSubmit channel.
# None of this is something a user typed, so none of it belongs in memory.
NOISE_MARKERS = (
    "<task-notification>",
    "<system-reminder>",
    "SYSTEM NOTIFICATION",
    "This is an automated background-task event",
)


def is_noise(prompt: str) -> bool:
    stripped = prompt.strip()
    if not stripped:
        return True
    if any(marker in stripped for marker in NOISE_MARKERS):
        return True
    # A real user prompt doesn't start as a raw XML/tag blob.
    return stripped.startswith("<") and stripped.count("<") > 2


def main():
    payload = read_hook_input()
    prompt = payload.get("prompt", "")
    cwd = payload.get("cwd")
    if not prompt or is_noise(prompt):
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
    run_safely(main)
