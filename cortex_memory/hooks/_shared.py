"""Shared helpers for Claude Code hook scripts.

Design rule carried over from the blueprint: a memory-system outage must never
block the agent. Every hook here reads stdin defensively and always exits 0 --
worst case, memory just isn't consulted/recorded for that turn.
"""

import json
import sys

sys.path.insert(0, str(__file__).rsplit("cortex_memory", 1)[0])

from cortex_memory import capture, db, retrieve  # noqa: E402


def read_hook_input() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:
        return {}


def additional_context(hook_event_name: str, text: str) -> None:
    """Emit the Claude Code hookSpecificOutput contract for context injection.

    Validate this payload shape against the installed Claude Code version --
    Oracle's blueprint flagged this exact caveat; the schema has moved before.
    """
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": hook_event_name,
            "additionalContext": text,
        }
    }))


def safe_namespace(cwd: str | None) -> str:
    conn = db.connect()
    try:
        name = f"project:{cwd}" if cwd else "global:user"
        return db.get_or_create_namespace(conn, name, "project" if cwd else "global")
    finally:
        conn.close()
