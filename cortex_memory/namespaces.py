"""Namespace resolution: one namespace per git project, plus a shared global one.

  global:user                  cross-project knowledge (preferences, habits)
  project:<canonical-root>     everything specific to one repository

The old scheme used the raw cwd, so /repo, /repo/src and /repo/tests became
three separate memories. Here the project root is found by walking up to the
nearest `.git` (a directory, or a file for worktrees/submodules), which:
  * collapses every subdirectory of a repo into one namespace,
  * keeps linked worktrees separate (their branches evolve independently),
  * picks the innermost repo for nested repositories,
  * needs no `git` executable and no subprocess,
  * falls back to the directory itself outside any repository.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import db

GLOBAL_NAME = "global:user"
# Retrieval weights: project memory outranks global memory of equal relevance.
WEIGHT_PROJECT_FTS = 1.00
WEIGHT_GLOBAL_FTS = 0.65
WEIGHT_PROJECT_GRAPH = 0.75
WEIGHT_GLOBAL_GRAPH = 0.45


def canonical_root(cwd: str | os.PathLike | None) -> str | None:
    """Canonical project root for `cwd`, or None when cwd is missing/unusable."""
    if not cwd:
        return None
    try:
        p = Path(cwd).expanduser()
        p = Path(os.path.realpath(p))          # resolves symlinks, normalises `..`
        start = p if p.is_dir() else p.parent
        node = start
        for candidate in (node, *node.parents):
            if (candidate / ".git").exists():
                node = candidate
                break
        else:
            node = start
        root = node.as_posix()
        if os.name == "nt":                     # case-insensitive filesystem
            root = root.lower()
        return root
    except (OSError, ValueError):
        return None


def project_name(cwd) -> str | None:
    root = canonical_root(cwd)
    return f"project:{root}" if root else None


def resolve(conn, cwd) -> dict:
    """{'project': ns_id | None, 'global': ns_id, 'project_name': str | None}."""
    name = project_name(cwd)
    return {
        "project": db.get_or_create_namespace(conn, name, "project") if name else None,
        "global": db.ensure_default_namespace(conn, GLOBAL_NAME),
        "project_name": name,
    }


def promote(conn, fact_id: str, to_name: str = GLOBAL_NAME) -> tuple[str, str]:
    """Copy an active fact into another namespace (default: global), keeping provenance.

    Idempotent: promoting the same fact twice reinforces instead of duplicating.
    Returns (new_or_existing_fact_id, operation). The original stays in place.
    """
    from .capture import CandidateFact, _audit, merge_fact

    row = conn.execute("SELECT * FROM facts WHERE id=? AND status='active'", (fact_id,)).fetchone()
    if row is None:
        raise ValueError(f"no active fact {fact_id}")
    target = (db.ensure_default_namespace(conn, to_name) if to_name == GLOBAL_NAME
              else db.get_or_create_namespace(conn, to_name, "project"))
    if target == row["namespace_id"]:
        return fact_id, "noop"
    src = conn.execute(
        "SELECT episode_id FROM memory_sources WHERE memory_type='fact' AND memory_id=? LIMIT 1", (fact_id,)
    ).fetchone()
    new_id, op = merge_fact(conn, target, CandidateFact(
        subject=row["subject"], predicate=row["predicate"], object=row["object"],
        confidence=row["confidence"] or 0.6, source_episode_id=src["episode_id"] if src else None))
    _audit(conn, "promote", "fact", new_id, {"from_fact": fact_id, "to": to_name})
    return new_id, op


def consolidate_legacy(conn) -> dict:
    """Merge fragmented `project:<raw cwd>` namespaces into their canonical project root.

    Safe to re-run. Rows whose dedup key would collide are left in the old
    namespace (nothing is deleted), and a namespace is only removed once empty.
    """
    moved = {"namespaces_merged": 0, "rows_moved": 0}
    tables = ("episodes", "facts", "procedures", "entities")
    for ns in conn.execute("SELECT id, name FROM namespaces WHERE name LIKE 'project:%'").fetchall():
        raw = ns["name"][len("project:"):]
        canon = project_name(raw)
        if not canon or canon == ns["name"]:
            continue
        target = db.get_or_create_namespace(conn, canon, "project")
        if target == ns["id"]:
            continue
        for t in tables:
            cur = conn.execute(f"UPDATE OR IGNORE {t} SET namespace_id=? WHERE namespace_id=?", (target, ns["id"]))
            moved["rows_moved"] += cur.rowcount
        left = sum(conn.execute(f"SELECT COUNT(*) FROM {t} WHERE namespace_id=?", (ns["id"],)).fetchone()[0]
                   for t in tables)
        if left == 0:
            conn.execute("DELETE FROM namespaces WHERE id=?", (ns["id"],))
            moved["namespaces_merged"] += 1
    return moved
