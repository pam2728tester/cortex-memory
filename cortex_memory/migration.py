"""Token Savior -> Cortex migration: export to JSONL, validate, idempotent import.

Safety rules:
  * the source database is opened read-only and never modified; a consistent copy
    is taken with SQLite's backup API (so WAL contents are included);
  * the exact schema is discovered at run time (columns vary between releases);
  * every row carries its original id, timestamps and archived state;
  * an `import_ledger` makes re-runs idempotent;
  * private observations and anything that looks like a secret are NOT imported
    (and are counted, never stored);
  * a manifest with row counts and a SHA-256 makes the export verifiable.

    python -m cortex_memory.migration export   [--db PATH] --out FILE.jsonl
    python -m cortex_memory.migration import   --in FILE.jsonl [--dry-run]
    python -m cortex_memory.migration validate --in FILE.jsonl
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from . import capture, db, namespaces
from .extract import _h
from .privacy import looks_like_injection, redact_secrets

SOURCE_SYSTEM = "token-savior"
DEFAULT_SOURCE_DB = Path.home() / ".local" / "share" / "token-savior" / "memory.db"
MAX_CONTENT = 10_000

TYPE_MAP = {
    "decision": "decision", "command": "command", "bugfix": "error_fix", "error_pattern": "error_fix",
    "user": "preference", "feedback": "preference", "project": "project",
}


def map_type(original: str | None) -> str:
    return TYPE_MAP.get((original or "").lower(), "note")


# --------------------------------------------------------------------- export
def _consistent_copy(src_path: Path) -> sqlite3.Connection:
    """Read-only open + backup into memory: includes WAL data, never touches the source."""
    if not src_path.exists():
        raise FileNotFoundError(f"Token Savior database not found: {src_path}")
    src = sqlite3.connect(f"file:{src_path.as_posix()}?mode=ro", uri=True)
    mem = sqlite3.connect(":memory:")
    try:
        src.backup(mem)
    finally:
        src.close()
    mem.row_factory = sqlite3.Row
    return mem


def export_observations(db_path: Path, out_path: Path) -> dict:
    mem = _consistent_copy(Path(db_path))
    try:
        cols = [r["name"] for r in mem.execute("PRAGMA table_info(observations)")]
        if not cols:
            raise ValueError("source database has no 'observations' table; unsupported Token Savior version")
        for needed in ("id", "content"):
            if needed not in cols:
                raise ValueError(f"observations table lacks required column '{needed}'")
        rows = mem.execute("SELECT * FROM observations ORDER BY id").fetchall()
    finally:
        mem.close()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    counts = {"rows": 0, "archived": 0, "private": 0, "global": 0}
    h = hashlib.sha256()
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            d = dict(r)
            rec = {
                "source_system": SOURCE_SYSTEM, "source_id": str(d["id"]),
                "original_type": d.get("type"), "memory_type": map_type(d.get("type")),
                "title": d.get("title"), "content": d.get("content") or "",
                "why": d.get("why"), "how_to_apply": d.get("how_to_apply"),
                "project": d.get("project_root"), "is_global": bool(d.get("is_global")),
                "private": bool(d.get("private")), "archived": bool(d.get("archived")),
                "importance": d.get("importance"), "tags": d.get("tags"),
                "created_at": d.get("created_at"), "updated_at": d.get("updated_at"),
                "metadata": {k: d[k] for k in ("symbol", "file_path", "agent_id", "access_count", "superseded_by")
                             if d.get(k) not in (None, "")},
            }
            line = json.dumps(rec, ensure_ascii=False, sort_keys=True)
            fh.write(line + "\n")
            h.update(line.encode("utf-8") + b"\n")
            counts["rows"] += 1
            counts["archived"] += rec["archived"]
            counts["private"] += rec["private"]
            counts["global"] += rec["is_global"]
    manifest = {"source_system": SOURCE_SYSTEM, "source_db": str(db_path), "sha256": h.hexdigest(), **counts}
    Path(str(out_path) + ".manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest


# --------------------------------------------------------------------- import
def _compose(rec: dict) -> str:
    title, body = (rec.get("title") or "").strip(), (rec.get("content") or "").strip()
    text = body if (not title or title.lower() in body.lower()) else f"{title}: {body}"
    for label, key in (("Why", "why"), ("How to apply", "how_to_apply")):
        if rec.get(key):
            text += f"\n{label}: {rec[key].strip()}"
    return text[:MAX_CONTENT]


def _tags(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(t)[:64] for t in raw][:20]
    try:
        v = json.loads(raw)
        if isinstance(v, list):
            return [str(t)[:64] for t in v][:20]
    except (TypeError, ValueError):
        pass
    return [t.strip()[:64] for t in str(raw).split(",") if t.strip()][:20]


def read_export(path: Path):
    """Yield (record|None, line_no). None marks a corrupt line (reported, never fatal)."""
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
                if not isinstance(rec, dict) or "source_id" not in rec:
                    raise ValueError
                yield rec, n
            except ValueError:
                yield None, n


def import_observations(in_path: Path, dry_run: bool = False) -> dict:
    rep = {"read": 0, "imported": 0, "already_imported": 0, "skipped_private": 0, "rejected_secret": 0,
           "rejected_injection": 0, "empty": 0, "corrupt_lines": [], "archived_preserved": 0, "dry_run": dry_run}
    conn = db.connect()
    try:
        glob = db.ensure_default_namespace(conn)
        for rec, line_no in read_export(Path(in_path)):
            if rec is None:
                rep["corrupt_lines"].append(line_no)
                continue
            rep["read"] += 1
            sid = str(rec["source_id"])
            if conn.execute("SELECT 1 FROM import_ledger WHERE source_system=? AND source_id=?",
                            (SOURCE_SYSTEM, sid)).fetchone():
                rep["already_imported"] += 1
                continue
            if rec.get("private"):
                rep["skipped_private"] += 1
                continue
            content = _compose(rec)
            if not content.strip():
                rep["empty"] += 1
                continue
            if redact_secrets(content) != content:
                rep["rejected_secret"] += 1
                continue
            if looks_like_injection(content):
                rep["rejected_injection"] += 1
                continue
            if dry_run:
                rep["imported"] += 1
                rep["archived_preserved"] += bool(rec.get("archived"))
                continue

            mtype = rec.get("memory_type") or map_type(rec.get("original_type"))
            if rec.get("is_global") or mtype == "preference" or not rec.get("project"):
                ns = glob
            else:
                ns = db.get_or_create_namespace(conn, namespaces.project_name(rec["project"]) or "global:user")
            created = rec.get("created_at")
            conn.execute("BEGIN")
            try:
                eid, _ = capture.capture_episode(conn, ns, "import", content, source_type="agent_observed")
                fid, _op = capture.merge_fact(conn, ns, capture.CandidateFact(
                    subject="user" if mtype == "preference" else "project", predicate=f"{mtype}:{_h(content)}",
                    object=content, confidence=0.8, source_episode_id=eid))
                imp = rec.get("importance")
                importance = min(1.0, max(0.0, float(imp) / 10)) if isinstance(imp, (int, float)) else 0.5
                conn.execute("UPDATE facts SET importance=? WHERE id=?", (importance, fid))
                if created:      # keep the original timeline, not the migration time
                    conn.execute("UPDATE facts SET recorded_at=?, valid_from=? WHERE id=?", (created, created, fid))
                    conn.execute("UPDATE episodes SET occurred_at=?, recorded_at=? WHERE id=?", (created, created, eid))
                if rec.get("archived"):
                    conn.execute("UPDATE facts SET status='superseded', superseded_at=datetime('now') WHERE id=?", (fid,))
                    rep["archived_preserved"] += 1
                meta = {"original_type": rec.get("original_type"), "source_id": sid, **(rec.get("metadata") or {})}
                conn.execute(
                    "INSERT INTO fact_meta (fact_id, tags_json, metadata_json, source) VALUES (?, ?, ?, 'import') "
                    "ON CONFLICT(fact_id) DO UPDATE SET metadata_json=excluded.metadata_json",
                    (fid, json.dumps(_tags(rec.get("tags"))), json.dumps(meta)))
                conn.execute(
                    "INSERT INTO import_ledger (source_system, source_id, target_memory_id, imported_at, content_hash) "
                    "VALUES (?, ?, ?, datetime('now'), ?)",
                    (SOURCE_SYSTEM, sid, fid, hashlib.sha256(content.encode()).hexdigest()))
                conn.execute("COMMIT")
                rep["imported"] += 1
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return rep
    finally:
        conn.close()


# ------------------------------------------------------------------- validate
def validate(in_path: Path) -> dict:
    """Check the export against its manifest and the ledger against the Cortex database."""
    in_path = Path(in_path)
    man_path = Path(str(in_path) + ".manifest.json")
    problems = []
    manifest = json.loads(man_path.read_text()) if man_path.exists() else None
    if manifest is None:
        problems.append("manifest missing")
    elif hashlib.sha256(in_path.read_bytes()).hexdigest() != manifest["sha256"]:
        problems.append("export file checksum does not match manifest (modified or truncated)")
    recs = [r for r, _ in read_export(in_path) if r is not None]
    if manifest and len(recs) != manifest["rows"]:
        problems.append(f"row count {len(recs)} != manifest {manifest['rows']}")
    conn = db.connect()
    try:
        in_ledger = {r["source_id"] for r in conn.execute(
            "SELECT source_id FROM import_ledger WHERE source_system=?", (SOURCE_SYSTEM,))}
        expected = {str(r["source_id"]) for r in recs if not r.get("private")}
        missing = expected - in_ledger
        archived_ok = 0
        for r in recs:
            if r.get("archived") and str(r["source_id"]) in in_ledger:
                st = conn.execute(
                    "SELECT f.status FROM import_ledger l JOIN facts f ON f.id=l.target_memory_id "
                    "WHERE l.source_system=? AND l.source_id=?", (SOURCE_SYSTEM, str(r["source_id"]))).fetchone()
                if st and st["status"] == "active":
                    problems.append(f"archived observation {r['source_id']} was reactivated")
                else:
                    archived_ok += 1
        return {"ok": not problems and not (missing - _unimportable(recs, missing)), "problems": problems,
                "exported": len(recs), "in_ledger": len(in_ledger & expected),
                "not_imported": sorted(missing)[:20], "archived_preserved": archived_ok}
    finally:
        conn.close()


def _unimportable(recs, ids) -> set:
    """Rows that import intentionally rejects (secret/injection/empty) are not validation failures."""
    out = set()
    for r in recs:
        if str(r["source_id"]) in ids:
            c = _compose(r)
            if not c.strip() or redact_secrets(c) != c or looks_like_injection(c):
                out.add(str(r["source_id"]))
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="cortex_memory.migration")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--db", default=str(DEFAULT_SOURCE_DB))
    e.add_argument("--out", required=True)
    i = sub.add_parser("import")
    i.add_argument("--in", dest="inp", required=True)
    i.add_argument("--dry-run", action="store_true")
    v = sub.add_parser("validate")
    v.add_argument("--in", dest="inp", required=True)
    a = p.parse_args(argv)
    if a.cmd == "export":
        out = export_observations(Path(a.db), Path(a.out))
    elif a.cmd == "import":
        out = import_observations(Path(a.inp), dry_run=a.dry_run)
    else:
        out = validate(Path(a.inp))
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
