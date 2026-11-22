"""Write path: capture -> privacy filter -> classify -> extract -> merge -> commit.

Extractor/merger split is the adeoluo/agent-memory pattern: the extractor only
proposes candidate facts, it never touches existing rows. The merger decides
whether a candidate reinforces, contradicts (supersedes), or is new -- and is
the only code path allowed to write to `facts`.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field

from . import db
from .privacy import sanitize_for_capture

# ---------------------------------------------------------------------------
# Step 1-2: capture + privacy/trust filter
# ---------------------------------------------------------------------------

def content_hash(content: str) -> str:
    return hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


def capture_episode(conn, namespace_id, event_type, content, summary=None,
                     source_type="agent_observed", session_id=None, occurred_at=None,
                     importance=0.5, confidence=1.0):
    """Store an immutable observation. Idempotent on (namespace, content_hash)."""
    clean, trust_level, quarantined = sanitize_for_capture(content, source_type)
    h = content_hash(clean)
    existing = conn.execute(
        "SELECT id FROM episodes WHERE namespace_id=? AND content_hash=?", (namespace_id, h)
    ).fetchone()
    if existing:
        return existing["id"], False  # (episode_id, was_new)

    eid = db.new_id()
    conn.execute(
        """INSERT INTO episodes
           (id, namespace_id, session_id, event_type, content, summary, source_type,
            occurred_at, recorded_at, importance, confidence, trust_level, is_quarantined, content_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), ?, ?, ?, ?, ?)""",
        (eid, namespace_id, session_id, event_type, clean, summary, source_type,
         importance, confidence, trust_level, int(quarantined), h),
    )
    _audit(conn, "insert", "episode", eid, {"event_type": event_type, "quarantined": quarantined})
    return eid, True


# ---------------------------------------------------------------------------
# Step 3-4: classify + extract candidate facts (deterministic rules).
#
# No LLM dependency for v1 -- a handful of high-precision patterns cover the
# common "remember this" cases. This is an explicit extension point: swap in
# an LLM-backed extractor later without touching the merger.
# ---------------------------------------------------------------------------

@dataclass
class CandidateFact:
    subject: str
    predicate: str
    object: str
    confidence: float = 0.6
    source_episode_id: str | None = None


RULES = [
    # "I prefer X" / "please always answer in X"
    (re.compile(r"(?i)\bi prefer ([a-z0-9 _\-]{2,40})\b"), "user", "preference"),
    (re.compile(r"(?i)\bplease always ([a-z0-9 _\-]{2,60})\b"), "user", "instruction"),
    (re.compile(r"(?i)\balways ([a-z0-9 _\-]{2,60}) before\b"), "project", "rule"),
    # "the project uses X" / "we use X for Y"
    (re.compile(r"(?i)\bthe project uses ([a-z0-9 _\-\.]{2,40})\b"), "project", "uses"),
    (re.compile(r"(?i)\buses? ([a-z0-9 _\-\.]{2,40}) as (?:the|its) (database|cache|queue|test framework)\b"),
     "project", "uses_component"),
]

# A question is not evidence of its implied statement. Never extract from these.
QUESTION_PATTERN = re.compile(r"\?\s*$")


def extract_candidates(content: str, episode_id: str) -> list[CandidateFact]:
    if QUESTION_PATTERN.search(content.strip()):
        return []
    out = []
    for pattern, subject, predicate in RULES:
        for m in pattern.finditer(content):
            obj = m.group(1).strip().rstrip(".")
            if not obj:
                continue
            out.append(CandidateFact(subject=subject, predicate=predicate, object=obj,
                                      source_episode_id=episode_id))
    return out


# ---------------------------------------------------------------------------
# Step 5-6: merger. Sole writer to `facts`. Reinforce / supersede / insert.
# ---------------------------------------------------------------------------

def merge_fact(conn, namespace_id, candidate: CandidateFact) -> tuple[str, str]:
    """Apply one candidate fact. Returns (fact_id, operation)."""
    existing = conn.execute(
        """SELECT * FROM facts WHERE namespace_id=? AND subject=? AND predicate=? AND status='active'
           ORDER BY version DESC LIMIT 1""",
        (namespace_id, candidate.subject, candidate.predicate),
    ).fetchone()

    if existing is None:
        fid = db.new_id()
        conn.execute(
            """INSERT INTO facts (id, namespace_id, subject, predicate, object, confidence,
               recorded_at, valid_from, version)
               VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), 1)""",
            (fid, namespace_id, candidate.subject, candidate.predicate, candidate.object, candidate.confidence),
        )
        _link_source(conn, "fact", fid, candidate.source_episode_id, "derived_from")
        _audit(conn, "insert", "fact", fid, {"subject": candidate.subject, "predicate": candidate.predicate})
        return fid, "insert"

    if existing["object"].strip().lower() == candidate.object.strip().lower():
        # Reinforcement: same belief, independent evidence.
        conn.execute(
            """UPDATE facts SET confidence = MIN(1.0, confidence + 0.05), source_count = source_count + 1,
               last_accessed_at = datetime('now') WHERE id = ?""",
            (existing["id"],),
        )
        _link_source(conn, "fact", existing["id"], candidate.source_episode_id, "supports")
        _audit(conn, "reinforce", "fact", existing["id"], {})
        return existing["id"], "reinforce"

    # Contradiction: close old version, open a new one. Never delete history.
    conn.execute(
        "UPDATE facts SET status='superseded', superseded_at=datetime('now'), valid_to=datetime('now') WHERE id=?",
        (existing["id"],),
    )
    fid = db.new_id()
    conn.execute(
        """INSERT INTO facts (id, namespace_id, subject, predicate, object, confidence,
           recorded_at, valid_from, version, supersedes_id)
           VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), ?, ?)""",
        (fid, namespace_id, candidate.subject, candidate.predicate, candidate.object,
         candidate.confidence, existing["version"] + 1, existing["id"]),
    )
    _link_source(conn, "fact", fid, candidate.source_episode_id, "derived_from")
    _link_source(conn, "fact", existing["id"], candidate.source_episode_id, "contradicts")
    _audit(conn, "supersede", "fact", fid, {"supersedes_id": existing["id"]})
    return fid, "supersede"


def ingest(conn, namespace_id, event_type, content, **kwargs) -> dict:
    """Full write-path entry point: capture an episode, then extract+merge facts."""
    episode_id, was_new = capture_episode(conn, namespace_id, event_type, content, **kwargs)
    results = {"episode_id": episode_id, "was_new": was_new, "facts": []}
    if not was_new:
        return results
    quarantined = conn.execute("SELECT is_quarantined FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if quarantined and quarantined["is_quarantined"]:
        # Untrusted content: keep as evidence, never promote to a fact automatically.
        return results
    for candidate in extract_candidates(content, episode_id):
        fid, op = merge_fact(conn, namespace_id, candidate)
        results["facts"].append({"fact_id": fid, "op": op, "predicate": candidate.predicate})
    return results


def _link_source(conn, memory_type, memory_id, episode_id, contribution_type):
    if not episode_id:
        return
    conn.execute(
        """INSERT INTO memory_sources (id, memory_type, memory_id, episode_id, contribution_type, created_at)
           VALUES (?, ?, ?, ?, ?, datetime('now'))""",
        (db.new_id(), memory_type, memory_id, episode_id, contribution_type),
    )


def _audit(conn, operation, memory_type, memory_id, detail: dict):
    conn.execute(
        "INSERT INTO audit_log (id, ts, operation, memory_type, memory_id, detail_json) VALUES (?, datetime('now'), ?, ?, ?, ?)",
        (db.new_id(), operation, memory_type, memory_id, json.dumps(detail)),
    )
