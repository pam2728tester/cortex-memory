"""Git-free snapshot/rollback (SQLite backup API) and GDPR-style forget.

Snapshot is for "undo a bad consolidation run." Forget is for "permanently
remove this data," including scrubbing it out of FTS indexes. These are
deliberately separate code paths -- supersession keeps history, forget erases it.
"""

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from . import db


def create(reason: str = "manual") -> dict:
    src = db.connect()
    try:
        ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        dest_path = db.data_dir() / "snapshots" / f"{ts}.db"
        dest = sqlite3.connect(dest_path)
        src.backup(dest)
        dest.close()

        checksum = hashlib.sha256(dest_path.read_bytes()).hexdigest()
        sid = db.new_id()
        manifest = {
            "snapshot_id": sid, "created_at": ts, "schema_version": db.SCHEMA_VERSION,
            "reason": reason, "checksum": checksum, "file": dest_path.name,
        }
        (db.data_dir() / "snapshots" / f"{ts}.manifest.json").write_text(json.dumps(manifest, indent=2))
        src.execute(
            "INSERT INTO snapshots (id, created_at, reason, file_path, schema_version, checksum) VALUES (?, datetime('now'), ?, ?, ?, ?)",
            (sid, reason, str(dest_path), db.SCHEMA_VERSION, checksum),
        )
        return manifest
    finally:
        src.close()


def list_snapshots() -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM snapshots ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def restore(snapshot_id: str) -> dict:
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        if not row:
            raise ValueError(f"unknown snapshot: {snapshot_id}")
        snap_path = Path(row["file_path"])
        if not snap_path.exists():
            raise FileNotFoundError(f"snapshot file missing: {snap_path}")
        actual = hashlib.sha256(snap_path.read_bytes()).hexdigest()
        if actual != row["checksum"]:
            raise ValueError("snapshot checksum mismatch -- refusing to restore a corrupted backup")
    finally:
        conn.close()

    # Safety: always snapshot current state before overwriting it.
    pre_restore = create(reason=f"pre_restore_before_{snapshot_id}")

    live_path = db.db_path()
    backup_src = sqlite3.connect(snap_path)
    live_dest = sqlite3.connect(live_path)
    try:
        backup_src.backup(live_dest)
    finally:
        backup_src.close()
        live_dest.close()
    return {"restored": snapshot_id, "safety_snapshot": pre_restore["snapshot_id"]}


def forget(namespace_id: str, subject: str | None = None, fact_id: str | None = None) -> dict:
    """Permanently scrub a fact (or all facts matching a subject) from the database."""
    conn = db.connect()
    try:
        if fact_id:
            where, params = "id = ?", (fact_id,)
        elif subject:
            where, params = "namespace_id = ? AND subject = ?", (namespace_id, subject)
        else:
            raise ValueError("forget() requires fact_id or subject")

        rows = conn.execute(f"SELECT id FROM facts WHERE {where}", params).fetchall()
        rid = db.new_id()
        conn.execute(
            "INSERT INTO forget_requests (id, namespace_id, target_type, target_id, subject, requested_at, status) "
            "VALUES (?, ?, 'fact', ?, ?, datetime('now'), 'completed')",
            (rid, namespace_id, fact_id, subject),
        )
        for row in rows:
            conn.execute(
                "UPDATE facts SET subject='[FORGOTTEN]', predicate='[FORGOTTEN]', object='[FORGOTTEN]', status='deleted' WHERE id = ?",
                (row["id"],),
            )
            conn.execute("DELETE FROM memory_sources WHERE memory_type='fact' AND memory_id = ?", (row["id"],))
            conn.execute(
                "INSERT INTO audit_log (id, ts, operation, memory_type, memory_id, detail_json) VALUES (?, datetime('now'), 'forget', 'fact', ?, '{}')",
                (db.new_id(), row["id"]),
            )
        return {"request_id": rid, "scrubbed": len(rows)}
    finally:
        conn.close()
