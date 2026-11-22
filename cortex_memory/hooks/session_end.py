#!/usr/bin/env python3
"""Stop/SessionEnd hook: write a handoff summary, run decay, snapshot periodically."""

import sys

from _shared import read_hook_input, safe_namespace

sys.path.insert(0, str(__file__).rsplit("cortex_memory", 1)[0])
from cortex_memory import capture, db, decay, snapshot  # noqa: E402


def main():
    payload = read_hook_input()
    cwd = payload.get("cwd")
    try:
        ns = safe_namespace(cwd)
        conn = db.connect()
        try:
            decay.recalc_importance(conn, ns)
        finally:
            conn.close()
        # Cheap safety net: snapshot on every session end. For a heavily-used
        # install, move this to a daily cron/scheduled task instead.
        snapshot.create(reason="session_end")
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
