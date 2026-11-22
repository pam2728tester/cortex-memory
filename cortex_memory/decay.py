"""Ebbinghaus-curve importance decay. Decay affects retrieval priority, never
historical truth -- a decayed fact is still in the table, just deprioritized.
Supersession (handled in capture.merge_fact) is a separate mechanism from decay
and from permanent deletion (forget.py): never conflate the three.
"""

import math

HALF_LIFE_DAYS = {
    "fact": 90,
    "procedure": 180,
    "episode": 30,
}


def _lambda_for(memory_type: str) -> float:
    return math.log(2) / HALF_LIFE_DAYS.get(memory_type, 60)


def recalc_importance(conn, namespace_id=None):
    """Recompute importance for facts and procedures based on days since last access."""
    where = "WHERE namespace_id = ?" if namespace_id else ""
    params = (namespace_id,) if namespace_id else ()

    rows = conn.execute(
        f"""SELECT id, importance, last_accessed_at, recorded_at FROM facts
            {where} {"AND" if where else "WHERE"} status='active'""",
        params,
    ).fetchall()
    lam = _lambda_for("fact")
    updated = 0
    for row in rows:
        anchor = row["last_accessed_at"] or row["recorded_at"]
        days = conn.execute("SELECT (julianday('now') - julianday(?)) AS d", (anchor,)).fetchone()["d"] or 0
        retention = math.exp(-lam * max(days, 0))
        new_importance = max(0.05, (row["importance"] or 0.5) * retention)
        conn.execute("UPDATE facts SET importance = ? WHERE id = ?", (new_importance, row["id"]))
        updated += 1
    return updated


def reinforce_procedure(conn, procedure_id, success: bool):
    """Track success/failure independently -- never reinforce merely because it ran."""
    col = "success_count" if success else "failure_count"
    conn.execute(f"UPDATE procedures SET {col} = {col} + 1, last_used_at = datetime('now') WHERE id = ?",
                 (procedure_id,))
    row = conn.execute("SELECT success_count, failure_count FROM procedures WHERE id = ?",
                        (procedure_id,)).fetchone()
    total = row["success_count"] + row["failure_count"]
    confidence = row["success_count"] / total if total else 0.5
    conn.execute("UPDATE procedures SET confidence = ? WHERE id = ?", (confidence, procedure_id))
