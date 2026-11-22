"""MemoryService: the one place memory operations are implemented.

Hooks, the CLI and the MCP server all go through here, so validation, privacy
checks and namespace rules cannot diverge between interfaces. MCP types are an
interface-level classification mapped onto the existing fact model:

    memory_type  ->  predicate prefix   subject
    preference       preference:<hash>  user      (stored in global:user by default)
    decision         decision:<hash>    project
    command          command:<hash>     project
    error_fix        error_fix:<hash>   project
    note / fact / project              project

All SQL is parameterised; nothing here exposes raw SQL or the filesystem.
"""

from __future__ import annotations

import json
import os
import re

from . import capture, db, namespaces, retrieve, snapshot
from .extract import _h
from .privacy import looks_like_injection, redact_secrets

MEMORY_TYPES = ("fact", "decision", "preference", "command", "error_fix", "project", "note")
SOURCES = ("explicit", "hook", "import", "llm")
MAX_CONTENT = 10_000
MAX_QUERY = 2_000
MAX_TAGS = 20
MAX_METADATA_BYTES = 8_192
_NS_NAME = re.compile(r"^(?:global:user|project:.{1,500})$")
_TYPE_ALIAS = {"stack": "project", "uses": "project", "file": "fact", "error": "fact"}


class MemoryError(ValueError):  # noqa: A001 - intentional, mirrors the public tool contract
    """A caller mistake (bad input). Never wraps internal failures."""


def _check_unit(name: str, value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0.0 <= float(value) <= 1.0:
        raise MemoryError(f"{name} must be a number between 0 and 1")
    return float(value)


def memory_type_of(predicate: str) -> str:
    prefix = predicate.split(":", 1)[0]
    if prefix in MEMORY_TYPES:
        return prefix
    return _TYPE_ALIAS.get(prefix, "fact")


def _content_of(row) -> str:
    pred = row["predicate"]
    if pred.startswith("uses:"):
        return f"project uses {row['object']} as the {pred.split(':', 1)[1]}"
    return row["object"]


class MemoryService:
    def __init__(self, cwd: str | None = None):
        self.cwd = cwd or os.environ.get("CORTEX_PROJECT_DIR") or os.getcwd()

    # ------------------------------------------------------------------ scope
    def _scope(self, conn, namespace=None, project=None, memory_type=None) -> dict:
        """Return {'primary': id, 'global': id, 'names': {id: name}}."""
        glob = db.ensure_default_namespace(conn, namespaces.GLOBAL_NAME)
        if namespace is not None:
            if not isinstance(namespace, str) or not _NS_NAME.match(namespace):
                raise MemoryError("namespace must be 'global:user' or 'project:<path>'")
            primary = glob if namespace == namespaces.GLOBAL_NAME else db.get_or_create_namespace(conn, namespace)
            name = namespace
        elif project is not None:
            if not isinstance(project, str) or not project.strip():
                raise MemoryError("project must be a non-empty path")
            name = namespaces.project_name(project)
            if name is None:
                raise MemoryError("project path could not be resolved")
            primary = db.get_or_create_namespace(conn, name)
        elif memory_type == "preference":
            primary, name = glob, namespaces.GLOBAL_NAME
        else:
            name = namespaces.project_name(self.cwd)
            primary = db.get_or_create_namespace(conn, name) if name else glob
            name = name or namespaces.GLOBAL_NAME
        return {"primary": primary, "global": glob, "names": {primary: name, glob: namespaces.GLOBAL_NAME}}

    # ------------------------------------------------------------------- save
    def save(self, content, memory_type="note", namespace=None, project=None, tags=None, source="explicit",
             confidence=0.9, importance=0.5, metadata=None) -> dict:
        if not isinstance(content, str) or not content.strip():
            raise MemoryError("content is required")
        content = content.strip()
        if len(content) > MAX_CONTENT:
            raise MemoryError(f"content exceeds {MAX_CONTENT} characters")
        if memory_type not in MEMORY_TYPES:
            raise MemoryError(f"memory_type must be one of {', '.join(MEMORY_TYPES)}")
        if source not in SOURCES:
            raise MemoryError(f"source must be one of {', '.join(SOURCES)}")
        confidence, importance = _check_unit("confidence", confidence), _check_unit("importance", importance)
        tags = tags or []
        if not isinstance(tags, list) or len(tags) > MAX_TAGS or not all(isinstance(t, str) and 0 < len(t) <= 64 for t in tags):
            raise MemoryError(f"tags must be a list of at most {MAX_TAGS} short strings")
        metadata = metadata or {}
        if not isinstance(metadata, dict):
            raise MemoryError("metadata must be an object")
        try:
            meta_json = json.dumps(metadata)
        except (TypeError, ValueError):
            raise MemoryError("metadata must be JSON-serialisable") from None
        if len(meta_json.encode()) > MAX_METADATA_BYTES:
            raise MemoryError(f"metadata exceeds {MAX_METADATA_BYTES} bytes")
        if redact_secrets(content) != content or redact_secrets(meta_json) != meta_json:
            raise MemoryError("content appears to contain a secret; refusing to store it")
        if looks_like_injection(content):
            raise MemoryError("content looks like a prompt-injection attempt; refusing to store it")

        conn = db.connect()
        try:
            scope = self._scope(conn, namespace, project, memory_type)
            ns = scope["primary"]
            conn.execute("BEGIN")
            try:
                eid, _ = capture.capture_episode(
                    conn, ns, "memory_save", content, source_type="user_provided" if source == "explicit" else "agent_observed")
                fid, op = capture.merge_fact(conn, ns, capture.CandidateFact(
                    subject="user" if memory_type == "preference" else "project",
                    predicate=f"{memory_type}:{_h(content)}", object=content,
                    confidence=confidence, source_episode_id=eid))
                conn.execute("UPDATE facts SET importance=? WHERE id=?", (importance, fid))
                prev = conn.execute("SELECT tags_json FROM fact_meta WHERE fact_id=?", (fid,)).fetchone()
                merged = sorted(set(json.loads(prev["tags_json"]) if prev else []) | set(tags))[:MAX_TAGS]
                conn.execute(
                    "INSERT INTO fact_meta (fact_id, tags_json, metadata_json, source) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(fact_id) DO UPDATE SET tags_json=excluded.tags_json, "
                    "metadata_json=excluded.metadata_json, source=excluded.source",
                    (fid, json.dumps(merged), meta_json, source))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return {"success": True, "memory_id": fid, "created": op == "insert", "deduplicated": op == "reinforce",
                    "memory_type": memory_type, "namespace": scope["names"][ns]}
        finally:
            conn.close()

    # ------------------------------------------------------------------ query
    def query(self, query, namespace=None, project=None, memory_types=None, limit=10, include_global=True,
              include_archived=False, min_confidence=0.3) -> dict:
        if not isinstance(query, str) or not query.strip():
            raise MemoryError("query is required")
        if len(query) > MAX_QUERY:
            raise MemoryError(f"query exceeds {MAX_QUERY} characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise MemoryError("limit must be an integer between 1 and 50")
        memory_types = memory_types or []
        if not isinstance(memory_types, list) or any(t not in MEMORY_TYPES for t in memory_types):
            raise MemoryError(f"memory_types must be a list drawn from {', '.join(MEMORY_TYPES)}")
        min_confidence = _check_unit("min_confidence", min_confidence)

        conn = db.connect()
        try:
            scope = self._scope(conn, namespace, project)
            scored, _eps = retrieve.search(
                conn, scope["primary"], query, limit=limit * 4 if memory_types else limit,
                global_namespace_id=scope["global"], min_confidence=min_confidence,
                include_global=include_global, include_superseded=include_archived)
            results = []
            for score, row, label in scored:
                mtype = memory_type_of(row["predicate"])
                if memory_types and mtype not in memory_types:
                    continue
                ns_id = scope["primary"] if label == "project" else scope["global"]
                meta = conn.execute("SELECT tags_json FROM fact_meta WHERE fact_id=?", (row["id"],)).fetchone()
                results.append({
                    "memory_id": row["id"], "content": _content_of(row), "memory_type": mtype,
                    "namespace": scope["names"].get(ns_id, namespaces.GLOBAL_NAME),
                    "confidence": row["confidence"], "importance": row["importance"], "score": round(score, 4),
                    "tags": json.loads(meta["tags_json"]) if meta else [],
                    "status": row["status"], "created_at": row["recorded_at"]})
                if len(results) == limit:
                    break
            for r in results:
                conn.execute("UPDATE facts SET last_accessed_at=datetime('now'), access_count=access_count+1 WHERE id=?",
                             (r["memory_id"],))
            searched = [scope["names"][scope["primary"]]]
            if include_global and scope["global"] != scope["primary"]:
                searched.append(namespaces.GLOBAL_NAME)
            return {"results": results, "total": len(results), "namespaces_searched": searched}
        finally:
            conn.close()

    # ----------------------------------------------------------------- forget
    def forget(self, memory_id, namespace=None, reason=None, permanent=False) -> dict:
        if not isinstance(memory_id, str) or not memory_id.strip():
            raise MemoryError("memory_id is required")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
            raise MemoryError("reason must be a string of at most 500 characters")
        conn = db.connect()
        try:
            row = conn.execute("SELECT id, namespace_id, status FROM facts WHERE id=?", (memory_id,)).fetchone()
            if row is None or row["status"] == "deleted":
                raise MemoryError("memory not found")
            scope = self._scope(conn, namespace)
            allowed = {scope["primary"], scope["global"]} if namespace is None else {scope["primary"]}
            if row["namespace_id"] not in allowed:
                raise MemoryError("memory does not belong to the requested namespace")
            if permanent:
                out = snapshot.forget(row["namespace_id"], fact_id=memory_id)
                return {"success": True, "memory_id": memory_id, "deleted": out["scrubbed"] == 1, "permanent": True,
                        "episodes_scrubbed": out["episodes_scrubbed"], "snapshots_scrubbed": out["snapshots_scrubbed"]}
            conn.execute("UPDATE facts SET status='deleted' WHERE id=?", (memory_id,))
            # Audit records the event, never the deleted content.
            conn.execute(
                "INSERT INTO audit_log (id, ts, operation, memory_type, memory_id, detail_json) "
                "VALUES (?, datetime('now'), 'soft_delete', 'fact', ?, ?)",
                (db.new_id(), memory_id, json.dumps({"reason": (reason or "")[:200]})))
            return {"success": True, "memory_id": memory_id, "deleted": True, "permanent": False}
        finally:
            conn.close()

    # ----------------------------------------------------------------- status
    def status(self, include_namespaces=True, include_recent=False) -> dict:
        conn = db.connect()
        try:
            one = lambda sql, *p: conn.execute(sql, p).fetchone()[0]  # noqa: E731
            out = {
                "status": "healthy", "database": "connected",
                "schema_version": one("SELECT MAX(version) FROM schema_migrations"),
                "total_memories": one("SELECT COUNT(*) FROM facts WHERE status != 'deleted'"),
                "active_memories": one("SELECT COUNT(*) FROM facts WHERE status='active'"),
                "archived_memories": one("SELECT COUNT(*) FROM facts WHERE status='superseded'"),
                "episodes": one("SELECT COUNT(*) FROM episodes WHERE status='active'"),
                "last_capture": one("SELECT MAX(recorded_at) FROM episodes"),
            }
            try:
                conn.execute("SELECT 1 FROM facts_fts LIMIT 1").fetchone()
                out["fts5"] = True
            except Exception:
                out["fts5"] = False
                out["status"] = "degraded"
            if include_namespaces:
                out["namespaces"] = [
                    {"name": r["name"], "count": r["n"]} for r in conn.execute(
                        "SELECT n.name, (SELECT COUNT(*) FROM facts f WHERE f.namespace_id=n.id AND f.status='active') AS n "
                        "FROM namespaces n ORDER BY n DESC, n.name")]
            if include_recent:
                out["recent"] = [
                    {"memory_id": r["id"], "memory_type": memory_type_of(r["predicate"]), "content": _content_of(r)[:80],
                     "created_at": r["recorded_at"]} for r in conn.execute(
                        "SELECT id, predicate, object, recorded_at FROM facts WHERE status='active' "
                        "ORDER BY recorded_at DESC LIMIT 5")]
            return out
        finally:
            conn.close()
