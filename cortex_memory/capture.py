"""Write path: capture -> privacy filter -> classify -> extract -> merge -> commit.

Extractor/merger split is the adeoluo/agent-memory pattern: the extractor only
proposes candidate facts, it never touches existing rows. The merger decides
whether a candidate reinforces, contradicts (supersedes), or is new -- and is
the only code path allowed to write to `facts`.
"""

import hashlib
import json
import os
import re
from dataclasses import dataclass, field

from . import db, extract
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


# A question is not evidence of its implied statement. Kept for back-compat;
# extract.py also rejects questions sentence by sentence.
QUESTION_PATTERN = re.compile(r"\?\s*$")

TOOL_EVENT_TYPES = {"tool_result", "tool_failure"}


def extract_candidates(content: str, episode_id: str) -> list[CandidateFact]:
    """User-text extraction (see extract.py for the rule set)."""
    return [_to_fact(c, episode_id) for c in extract.extract_text(content)]


def _to_fact(c: "extract.Candidate", episode_id: str) -> CandidateFact:
    return CandidateFact(subject=c.subject, predicate=c.predicate, object=c.object,
                         confidence=c.confidence, source_episode_id=episode_id)


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


def ingest(conn, namespace_id, event_type, content, tool=None, **kwargs) -> dict:
    """Full write-path entry point: capture an episode, then extract+merge facts.

    `tool` = (tool_name, tool_input, tool_response) for tool events: facts are
    then read from structured evidence rather than from the flattened content.
    Returns stats (counts only, never content) so a silent pipeline can be diagnosed.
    """
    stats = extract.Stats()
    episode_id, was_new = capture_episode(conn, namespace_id, event_type, content, **kwargs)
    results = {"episode_id": episode_id, "was_new": was_new, "facts": [], "stats": stats}
    if not was_new:
        return results
    quarantined = conn.execute("SELECT is_quarantined FROM episodes WHERE id=?", (episode_id,)).fetchone()
    if quarantined and quarantined["is_quarantined"]:
        # Untrusted content: keep as evidence, never promote to a fact automatically.
        return results
    if tool is not None:
        cands = extract.extract_tool(*tool, stats=stats)
    elif event_type in TOOL_EVENT_TYPES:
        cands = []   # never mine facts out of flattened tool output
    else:
        cands = extract.extract_text(content, stats)
        cands += extract.extract_llm(content, stats)
    for cand in cands:
        if cand.kind == "command":
            _pair_error_fix(conn, namespace_id, cand, episode_id, results)
        fid, op = merge_fact(conn, namespace_id, _to_fact(cand, episode_id))
        results["facts"].append({"fact_id": fid, "op": op, "predicate": cand.predicate, "kind": cand.kind})
    _log_stats(event_type, stats, len(results["facts"]))
    return results


def _pair_error_fix(conn, namespace_id, success: "extract.Candidate", episode_id, results) -> None:
    """A passing command that previously failed, with edits in between, becomes one error_fix fact."""
    err_pred = "error:" + success.predicate.split(":", 1)[1]
    err = conn.execute(
        "SELECT id, object, recorded_at FROM facts WHERE namespace_id=? AND predicate=? AND status='active'",
        (namespace_id, err_pred),
    ).fetchone()
    if not err:
        return
    edited = []
    for row in conn.execute(
        """SELECT content FROM episodes WHERE namespace_id=? AND event_type='tool_result'
           AND recorded_at >= ? AND (content LIKE 'Edit succeeded%' OR content LIKE 'Write succeeded%')
           ORDER BY recorded_at""",
        (namespace_id, err["recorded_at"]),
    ):
        m = re.search(r'"file_path":\s*"((?:[^"\\]|\\.)*)"', row["content"])
        if m:
            # json.dumps escaped Windows backslashes; normalise to "/" and keep the basename.
            name = m.group(1).replace("\\\\", "/").replace("\\", "/").rsplit("/", 1)[-1]
            if name not in edited:
                edited.append(name)
    if not edited:
        return   # no evidence of a fix: leave the error standing
    detail = err["object"].split(": ", 1)[-1][:120]
    cmd = success.object.split("`")[1]
    obj = f"`{cmd}` failed with '{detail}' and passed after editing {', '.join(edited[:4])}"
    fid, op = merge_fact(conn, namespace_id, CandidateFact(
        subject="project", predicate="error_fix:" + success.predicate.split(":", 1)[1],
        object=obj, confidence=0.85, source_episode_id=episode_id))
    conn.execute("UPDATE facts SET status='superseded', superseded_at=datetime('now') WHERE id=?", (err["id"],))
    results["facts"].append({"fact_id": fid, "op": op, "predicate": "error_fix", "kind": "error_fix"})


def _log_stats(event_type: str, stats: "extract.Stats", saved: int) -> None:
    """Opt-in (CORTEX_DEBUG=1) counters-only JSONL log; never writes message content."""
    if os.environ.get("CORTEX_DEBUG") != "1":
        return
    try:
        path = os.path.join(str(db.DEFAULT_DATA_DIR), "capture_stats.jsonl")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event_type": event_type, "saved": saved, **stats.as_dict()}) + "\n")
    except Exception:
        pass


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
