import hashlib
import json
import sqlite3

import pytest

from cortex_memory import db, migration
from cortex_memory.service import MemoryService

OBS_SCHEMA = """CREATE TABLE observations (
 id INTEGER PRIMARY KEY, session_id INTEGER, project_root TEXT, type TEXT, title TEXT, content TEXT, why TEXT,
 how_to_apply TEXT, symbol TEXT, file_path TEXT, tags TEXT, private INTEGER DEFAULT 0, importance INTEGER,
 relevance_score REAL, access_count INTEGER DEFAULT 0, content_hash TEXT, last_accessed_at TEXT, created_at TEXT,
 created_at_epoch INTEGER, updated_at TEXT, archived INTEGER DEFAULT 0, agent_id TEXT, narrative TEXT, facts TEXT,
 concepts TEXT, decay_immune INTEGER DEFAULT 0, last_accessed_epoch INTEGER, is_global INTEGER DEFAULT 0,
 context TEXT, expires_at_epoch INTEGER, superseded_by INTEGER)"""

ROWS = [
    # id, type, title, content, project, private, archived, is_global, importance, tags, created_at
    (1, "decision", "DB choice", "Use SQLite for storage", "/work/app", 0, 0, 0, 8, '["db","arch"]', "2026-10-01 10:00:00"),
    (2, "feedback", "Testing", "Always run pytest before committing", "/work/app", 0, 0, 1, 9, "testing,python", "2026-10-02 11:00:00"),
    (3, "bugfix", "Old bug", "Fixed by pinning playwright to 1.58", "/work/app", 0, 1, 0, 5, None, "2026-09-01 09:00:00"),
    (4, "note", "Private thing", "my private diary entry", None, 1, 0, 0, 5, None, "2026-10-03 12:00:00"),
    (5, "config", "Leaky", "token sk-abcdefghijklmnopqrstuvwxyz123456 for prod", "/work/app", 0, 0, 0, 5, None, "2026-10-04 12:00:00"),
    (6, "weird_new_type", "Mystery", "Some uncategorised observation", "/work/other", 0, 0, 0, None, None, "2026-10-05 12:00:00"),
    (7, "note", "", "", "/work/app", 0, 0, 0, 5, None, "2026-10-06 12:00:00"),
    (8, "command", "Run", "pytest -q runs the suite", "/work/app", 0, 0, 0, 7, None, "2026-10-07 12:00:00"),
]


@pytest.fixture
def ts_db(tmp_path):
    p = tmp_path / "ts" / "memory.db"
    p.parent.mkdir()
    c = sqlite3.connect(p)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(OBS_SCHEMA)
    for r in ROWS:
        c.execute("INSERT INTO observations (id,type,title,content,project_root,private,archived,is_global,importance,"
                  "tags,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (*r, r[10]))
    c.commit()          # leave the connection open: rows may still live in the -wal file
    yield p, c
    c.close()


def counts(conn):
    return tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                 for t in ("facts", "episodes", "import_ledger", "fact_meta"))


def test_export_preserves_everything_and_never_touches_source(ts_db, tmp_path):
    p, _c = ts_db
    before = hashlib.sha256(p.read_bytes()).hexdigest()
    out = tmp_path / "export.jsonl"
    m = migration.export_observations(p, out)
    assert m["rows"] == 8 and m["archived"] == 1 and m["private"] == 1 and m["global"] == 1
    assert hashlib.sha256(p.read_bytes()).hexdigest() == before          # source file byte-identical
    recs = [json.loads(l) for l in out.read_text(encoding="utf-8").splitlines()]
    assert [r["source_id"] for r in recs] == [str(i) for i in range(1, 9)]
    assert recs[2]["archived"] is True and recs[0]["created_at"] == "2026-10-01 10:00:00"
    assert recs[5]["memory_type"] == "note" and recs[5]["original_type"] == "weird_new_type"
    assert recs[2]["memory_type"] == "error_fix" and recs[1]["memory_type"] == "preference"
    manifest = json.loads((tmp_path / "export.jsonl.manifest.json").read_text())
    assert manifest["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()


def test_export_missing_or_unsupported_db(tmp_path):
    with pytest.raises(FileNotFoundError):
        migration.export_observations(tmp_path / "nope.db", tmp_path / "o.jsonl")
    p = tmp_path / "x.db"
    sqlite3.connect(p).close()
    with pytest.raises(ValueError, match="no 'observations' table"):
        migration.export_observations(p, tmp_path / "o.jsonl")


def exported(ts_db, tmp_path):
    out = tmp_path / "export.jsonl"
    migration.export_observations(ts_db[0], out)
    return out


def test_dry_run_mutates_nothing(conn, ts_db, tmp_path):
    out = exported(ts_db, tmp_path)
    before = counts(conn)
    rep = migration.import_observations(out, dry_run=True)
    assert rep["dry_run"] and rep["imported"] == 5 and counts(conn) == before


def test_import_maps_filters_and_preserves(conn, ts_db, tmp_path):
    out = exported(ts_db, tmp_path)
    rep = migration.import_observations(out)
    assert rep == {**rep, "read": 8, "imported": 5, "skipped_private": 1, "rejected_secret": 1, "empty": 1,
                   "archived_preserved": 1, "already_imported": 0, "corrupt_lines": []}
    # timestamps preserved, not "now"
    row = conn.execute("SELECT f.recorded_at, f.importance, f.status FROM import_ledger l JOIN facts f "
                       "ON f.id=l.target_memory_id WHERE l.source_id='1'").fetchone()
    assert row["recorded_at"] == "2026-10-01 10:00:00" and row["importance"] == 0.8 and row["status"] == "active"
    # archived stays archived
    st = conn.execute("SELECT f.status FROM import_ledger l JOIN facts f ON f.id=l.target_memory_id "
                      "WHERE l.source_id='3'").fetchone()[0]
    assert st == "superseded"
    # secret and private never stored anywhere
    raw = b"".join(p.read_bytes() for p in (tmp_path.glob("cortex.db*")))
    assert b"sk-abcdefghijklmnop" not in raw and b"private diary" not in raw
    # original id and type kept in metadata, tags parsed from both formats
    meta = conn.execute("SELECT m.metadata_json, m.tags_json FROM import_ledger l JOIN fact_meta m "
                        "ON m.fact_id=l.target_memory_id WHERE l.source_id='2'").fetchone()
    assert json.loads(meta["metadata_json"])["original_type"] == "feedback"
    assert json.loads(meta["tags_json"]) == ["testing", "python"]


def test_reimport_is_idempotent(conn, ts_db, tmp_path):
    out = exported(ts_db, tmp_path)
    migration.import_observations(out)
    after_first = counts(conn)
    rep = migration.import_observations(out)
    assert rep["imported"] == 0 and rep["already_imported"] == 5
    assert counts(conn) == after_first


def test_namespace_routing(conn, ts_db, tmp_path):
    migration.import_observations(exported(ts_db, tmp_path))
    ns = lambda sid: conn.execute(  # noqa: E731
        "SELECT n.name FROM import_ledger l JOIN facts f ON f.id=l.target_memory_id JOIN namespaces n "
        "ON n.id=f.namespace_id WHERE l.source_id=?", (sid,)).fetchone()[0]
    assert ns("2") == "global:user"                 # preference/global
    assert ns("1").endswith("/work/app") and ns("6").endswith("/work/other")


def test_imported_memories_are_queryable_via_service(conn, ts_db, tmp_path):
    migration.import_observations(exported(ts_db, tmp_path))
    svc = MemoryService(cwd=str(tmp_path / "anywhere"))
    pref = svc.query("pytest before committing")["results"]
    assert pref and pref[0]["memory_type"] == "preference" and pref[0]["namespace"] == "global:user"
    proj = svc.query("SQLite storage", project="/work/app")["results"]
    assert any("SQLite" in r["content"] and "DB choice" in r["content"] for r in proj)
    assert svc.query("playwright pinning", project="/work/app")["results"] == []       # archived hidden
    assert svc.query("playwright pinning", project="/work/app", include_archived=True)["results"]


def test_corrupt_jsonl_rows_reported_others_processed(conn, ts_db, tmp_path):
    out = exported(ts_db, tmp_path)
    lines = out.read_text(encoding="utf-8").splitlines()
    lines.insert(2, "{not json")
    lines.insert(4, '"just a string"')
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    rep = migration.import_observations(out)
    assert rep["corrupt_lines"] == [3, 5] and rep["imported"] == 5


def test_validate_passes_then_detects_tampering(conn, ts_db, tmp_path):
    out = exported(ts_db, tmp_path)
    migration.import_observations(out)
    v = migration.validate(out)
    assert v["ok"] and v["archived_preserved"] == 1 and v["in_ledger"] == 5 and v["problems"] == []
    out.write_text(out.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert not migration.validate(out)["ok"]


def test_validate_detects_missing_import(conn, ts_db, tmp_path):
    out = exported(ts_db, tmp_path)
    v = migration.validate(out)               # nothing imported yet
    assert not v["ok"] and v["not_imported"]


def test_empty_source_database_is_a_clean_noop(conn, tmp_path):
    p = tmp_path / "empty.db"
    c = sqlite3.connect(p)
    c.execute(OBS_SCHEMA)
    c.commit()
    c.close()
    out = tmp_path / "e.jsonl"
    assert migration.export_observations(p, out)["rows"] == 0
    rep = migration.import_observations(out)
    assert rep["read"] == 0 and rep["imported"] == 0
    assert migration.validate(out)["ok"]


def test_cli_entry_point(conn, ts_db, tmp_path, capsys):
    out = tmp_path / "cli.jsonl"
    assert migration.main(["export", "--db", str(ts_db[0]), "--out", str(out)]) == 0
    assert migration.main(["import", "--in", str(out), "--dry-run"]) == 0
    assert migration.main(["import", "--in", str(out)]) == 0
    assert migration.main(["validate", "--in", str(out)]) == 0
