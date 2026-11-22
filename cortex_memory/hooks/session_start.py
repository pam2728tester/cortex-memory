#!/usr/bin/env python3
"""SessionStart hook: inject project profile + unfinished-task context."""

import sys

from _shared import additional_context, read_hook_input, run_safely, safe_namespaces

sys.path.insert(0, str(__file__).rsplit("cortex_memory", 1)[0])
from cortex_memory import db, retrieve  # noqa: E402


def main():
    payload = read_hook_input()
    cwd = payload.get("cwd")
    try:
        db.init_db()
        scope = safe_namespaces(cwd)
        ns = scope["project"] or scope["global"]
        conn = db.connect()
        try:
            result = retrieve.query(conn, ns, "project profile current task unfinished preference decision stack",
                                    limit=8, global_namespace_id=scope["global"])
        finally:
            conn.close()
        if result["facts"] or result["episodes"]:
            additional_context("SessionStart", result["text"])
    except Exception:
        pass  # memory outage must never block session start
    sys.exit(0)


if __name__ == "__main__":
    run_safely(main)
