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
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
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


def safe_namespaces(cwd: str | None) -> dict:
    """{'project': id|None, 'global': id} resolved from the session cwd (git root, not raw cwd)."""
    from cortex_memory import namespaces
    conn = db.connect()
    try:
        return namespaces.resolve(conn, cwd)
    finally:
        conn.close()


def safe_namespace(cwd: str | None) -> str:
    """Primary namespace id for a cwd: its project, or the global one when there is no cwd."""
    ns = safe_namespaces(cwd)
    return ns["project"] or ns["global"]


def run_safely(main) -> None:
    """Entry-point guard: a hook may never fail the agent, whatever stdin held."""
    try:
        main()
    except SystemExit:
        pass
    except BaseException:
        pass
    sys.exit(0)
