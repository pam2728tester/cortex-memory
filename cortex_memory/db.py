"""SQLite schema and connection management for Cortex Memory."""

import os
import sqlite3
import uuid
from pathlib import Path

DEFAULT_DATA_DIR = Path(os.environ.get("CORTEX_MEMORY_DIR", os.path.expanduser("~/.cortex-memory")))
SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS namespaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    parent_id TEXT,
    created_at TEXT NOT NULL,
    settings_json TEXT DEFAULT '{}',
    FOREIGN KEY(parent_id) REFERENCES namespaces(id)
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    namespace_id TEXT NOT NULL,
    agent_name TEXT NOT NULL,
    project_path TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    summary TEXT,
    status TEXT DEFAULT 'active',
    FOREIGN KEY(namespace_id) REFERENCES namespaces(id)
);

-- L2: Episodic memory. Immutable evidence layer.
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    namespace_id TEXT NOT NULL,
    session_id TEXT,
    event_type TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,
    source_type TEXT NOT NULL DEFAULT 'agent_observed',
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    importance REAL DEFAULT 0.5,
    confidence REAL DEFAULT 1.0,
    trust_level TEXT DEFAULT 'agent_observed',
    is_quarantined INTEGER DEFAULT 0,
    content_hash TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    FOREIGN KEY(namespace_id) REFERENCES namespaces(id),
    FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_episodes_ns ON episodes(namespace_id, occurred_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_episodes_dedup ON episodes(namespace_id, content_hash);

-- L3: Semantic memory. Versioned, bitemporal facts. Never overwritten in place.
CREATE TABLE IF NOT EXISTS facts (
    id TEXT PRIMARY KEY,
    namespace_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    importance REAL DEFAULT 0.5,
    status TEXT DEFAULT 'active',          -- active | superseded | contested | expired | deleted
    valid_from TEXT,
    valid_to TEXT,
    recorded_at TEXT NOT NULL,
    superseded_at TEXT,
    version INTEGER DEFAULT 1,
    supersedes_id TEXT,
    source_type TEXT,
    source_count INTEGER DEFAULT 1,
    last_accessed_at TEXT,
    access_count INTEGER DEFAULT 0,
    expires_at TEXT,
    FOREIGN KEY(namespace_id) REFERENCES namespaces(id),
    FOREIGN KEY(supersedes_id) REFERENCES facts(id)
);
CREATE INDEX IF NOT EXISTS idx_facts_lookup ON facts(namespace_id, subject, predicate, status);

-- L4: Procedural memory. Reusable workflows backed by verified episodes.
CREATE TABLE IF NOT EXISTS procedures (
    id TEXT PRIMARY KEY,
    namespace_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    trigger_conditions TEXT,
    steps_json TEXT NOT NULL,
    confidence REAL DEFAULT 0.5,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    version INTEGER DEFAULT 1,
    supersedes_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT,
    FOREIGN KEY(namespace_id) REFERENCES namespaces(id)
);
CREATE INDEX IF NOT EXISTS idx_procedures_ns ON procedures(namespace_id, status);

-- Entity graph, for relationship-aware retrieval beyond shared vocabulary.
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY,
    namespace_id TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    importance REAL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(namespace_id) REFERENCES namespaces(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entities_name ON entities(namespace_id, canonical_name);

CREATE TABLE IF NOT EXISTS relationships (
    id TEXT PRIMARY KEY,
    namespace_id TEXT NOT NULL,
    source_entity_id TEXT NOT NULL,
    target_entity_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    weight REAL DEFAULT 0.5,
    created_at TEXT NOT NULL,
    last_activated_at TEXT,
    access_count INTEGER DEFAULT 0,
    FOREIGN KEY(source_entity_id) REFERENCES entities(id),
    FOREIGN KEY(target_entity_id) REFERENCES entities(id)
);
CREATE INDEX IF NOT EXISTS idx_rel_source ON relationships(source_entity_id);
CREATE INDEX IF NOT EXISTS idx_rel_target ON relationships(target_entity_id);

CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    PRIMARY KEY (fact_id, entity_id)
);

-- Provenance: which episode(s) support/contradict a derived memory.
CREATE TABLE IF NOT EXISTS memory_sources (
    id TEXT PRIMARY KEY,
    memory_type TEXT NOT NULL,     -- fact | procedure
    memory_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    contribution_type TEXT DEFAULT 'supports',   -- supports | contradicts | derived_from
    created_at TEXT NOT NULL,
    FOREIGN KEY(episode_id) REFERENCES episodes(id)
);
CREATE INDEX IF NOT EXISTS idx_sources_memory ON memory_sources(memory_type, memory_id);

-- Append-only audit trail. Every write operation, never deleted.
CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    operation TEXT NOT NULL,       -- insert | reinforce | supersede | contest | forget | restore
    memory_type TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    detail_json TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS forget_requests (
    id TEXT PRIMARY KEY,
    namespace_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT,
    subject TEXT,
    requested_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT DEFAULT 'pending'
);

CREATE TABLE IF NOT EXISTS snapshots (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    reason TEXT NOT NULL,
    file_path TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    checksum TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    content, summary, content='episodes', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS episodes_ai AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, content, summary) VALUES (new.rowid, new.content, new.summary);
END;
CREATE TRIGGER IF NOT EXISTS episodes_ad AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, content, summary) VALUES ('delete', old.rowid, old.content, old.summary);
END;
CREATE TRIGGER IF NOT EXISTS episodes_au AFTER UPDATE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, content, summary) VALUES ('delete', old.rowid, old.content, old.summary);
    INSERT INTO episodes_fts(rowid, content, summary) VALUES (new.rowid, new.content, new.summary);
END;

CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts USING fts5(
    subject, predicate, object, content='facts', content_rowid='rowid'
);
CREATE TRIGGER IF NOT EXISTS facts_ai AFTER INSERT ON facts BEGIN
    INSERT INTO facts_fts(rowid, subject, predicate, object) VALUES (new.rowid, new.subject, new.predicate, new.object);
END;
CREATE TRIGGER IF NOT EXISTS facts_ad AFTER DELETE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, subject, predicate, object) VALUES ('delete', old.rowid, old.subject, old.predicate, old.object);
END;
CREATE TRIGGER IF NOT EXISTS facts_au AFTER UPDATE ON facts BEGIN
    INSERT INTO facts_fts(facts_fts, rowid, subject, predicate, object) VALUES ('delete', old.rowid, old.subject, old.predicate, old.object);
    INSERT INTO facts_fts(rowid, subject, predicate, object) VALUES (new.rowid, new.subject, new.predicate, new.object);
END;
"""


def new_id() -> str:
    return uuid.uuid4().hex


def data_dir() -> Path:
    DEFAULT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DEFAULT_DATA_DIR / "snapshots").mkdir(exist_ok=True)
    return DEFAULT_DATA_DIR


def db_path() -> Path:
    return data_dir() / "cortex.db"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), isolation_level=None)  # autocommit; callers wrap explicit transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
        if row["v"] is None:
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, datetime('now'))",
                (SCHEMA_VERSION,),
            )
        ensure_default_namespace(conn)
    finally:
        conn.close()


def ensure_default_namespace(conn: sqlite3.Connection, name: str = "global:user") -> str:
    row = conn.execute("SELECT id FROM namespaces WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    nid = new_id()
    conn.execute(
        "INSERT INTO namespaces (id, name, type, created_at) VALUES (?, ?, 'global', datetime('now'))",
        (nid, name),
    )
    return nid


def get_or_create_namespace(conn: sqlite3.Connection, name: str, ns_type: str = "project") -> str:
    row = conn.execute("SELECT id FROM namespaces WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    nid = new_id()
    conn.execute(
        "INSERT INTO namespaces (id, name, type, created_at) VALUES (?, ?, ?, datetime('now'))",
        (nid, name, ns_type),
    )
    return nid
