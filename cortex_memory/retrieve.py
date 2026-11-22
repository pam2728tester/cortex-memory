"""Read path: BM25 + graph expansion -> Reciprocal Rank Fusion -> context assembly.

No mandatory vector index for v1 (keeps the system LLM-API-key-free and fully
offline). Vector search is an optional later addition keyed by memory id and
model version -- plug it into `_vector_candidates` without touching the rest
of the pipeline.
"""

import math

RRF_K = 60
DEFAULT_TOKEN_BUDGET = 1900


def _bm25_facts(conn, namespace_id, query_text, limit=20):
    rows = conn.execute(
        """SELECT f.id, f.subject, f.predicate, f.object, f.confidence, f.importance,
                  f.last_accessed_at, f.access_count, bm25(facts_fts) AS score
           FROM facts_fts JOIN facts f ON f.rowid = facts_fts.rowid
           WHERE facts_fts MATCH ? AND f.namespace_id = ? AND f.status = 'active'
           ORDER BY score LIMIT ?""",
        (query_text, namespace_id, limit),
    ).fetchall()
    return rows


def _bm25_episodes(conn, namespace_id, query_text, limit=20):
    rows = conn.execute(
        """SELECT e.id, e.content, e.summary, e.importance, e.confidence, e.occurred_at,
                  bm25(episodes_fts) AS score
           FROM episodes_fts JOIN episodes e ON e.rowid = episodes_fts.rowid
           WHERE episodes_fts MATCH ? AND e.namespace_id = ? AND e.status = 'active' AND e.is_quarantined = 0
           ORDER BY score LIMIT ?""",
        (query_text, namespace_id, limit),
    ).fetchall()
    return rows


def _graph_expand(conn, namespace_id, query_text, hops=1, limit=20):
    """Expand from entities whose canonical_name appears in the query text."""
    terms = [t for t in query_text.lower().split() if len(t) > 2]
    if not terms:
        return []
    entity_rows = conn.execute(
        "SELECT id, canonical_name FROM entities WHERE namespace_id = ?", (namespace_id,)
    ).fetchall()
    matched = [e for e in entity_rows if e["canonical_name"].lower() in terms
               or any(t in e["canonical_name"].lower() for t in terms)]
    if not matched:
        return []
    matched_ids = [e["id"] for e in matched]
    placeholders = ",".join("?" * len(matched_ids))
    rel_rows = conn.execute(
        f"""SELECT DISTINCT fe.fact_id FROM fact_entities fe
            WHERE fe.entity_id IN ({placeholders})""",
        matched_ids,
    ).fetchall()
    fact_ids = [r["fact_id"] for r in rel_rows][:limit]
    if not fact_ids:
        return []
    placeholders2 = ",".join("?" * len(fact_ids))
    return conn.execute(
        f"""SELECT id, subject, predicate, object, confidence, importance, last_accessed_at, access_count
            FROM facts WHERE id IN ({placeholders2}) AND status='active'""",
        fact_ids,
    ).fetchall()


def _fts_escape(text: str) -> str:
    """Build a permissive OR query from free text, safe for FTS5 MATCH."""
    terms = [t.strip('"') for t in text.split() if t.strip()]
    terms = [t for t in terms if t.replace("_", "").isalnum()]
    if not terms:
        return '""'
    return " OR ".join(f'"{t}"' for t in terms)


def _rrf_merge(*ranked_lists, weights=None):
    """Reciprocal Rank Fusion across independent retrieval methods. Returns {key: rrf_score}."""
    weights = weights or [1.0] * len(ranked_lists)
    scores: dict[str, float] = {}
    for lst, w in zip(ranked_lists, weights):
        for rank, item in enumerate(lst, start=1):
            key = item["id"]
            scores[key] = scores.get(key, 0.0) + w / (RRF_K + rank)
    return scores


def query(conn, namespace_id, text, limit=10, token_budget=DEFAULT_TOKEN_BUDGET):
    """Full read-path entry point. Returns an assembled context dict."""
    fts_query = _fts_escape(text)

    bm25_facts = _bm25_facts(conn, namespace_id, fts_query)
    bm25_episodes = _bm25_episodes(conn, namespace_id, fts_query)
    graph_facts = _graph_expand(conn, namespace_id, text)

    fact_rows = {r["id"]: r for r in list(bm25_facts) + list(graph_facts)}
    fused = _rrf_merge(list(bm25_facts), list(graph_facts), weights=[1.0, 0.7])

    scored = []
    for fid, rrf_score in fused.items():
        row = fact_rows[fid]
        confidence = row["confidence"] or 0.5
        importance = row["importance"] or 0.5
        temporal = _recency_score(row["last_accessed_at"])
        final = 0.6 * rrf_score + 0.15 * confidence + 0.15 * importance + 0.10 * temporal
        scored.append((final, row))
    scored.sort(key=lambda x: x[0], reverse=True)

    for _, row in scored[:limit]:
        conn.execute(
            "UPDATE facts SET last_accessed_at = datetime('now'), access_count = access_count + 1 WHERE id = ?",
            (row["id"],),
        )

    return _assemble_context(namespace_id, scored[:limit], list(bm25_episodes)[:5], token_budget)


def _recency_score(last_accessed_at) -> float:
    if not last_accessed_at:
        return 0.3
    return 0.8  # placeholder weighting; refined by decay.py's importance recalculation


def _assemble_context(namespace_id, scored_facts, episodes, token_budget):
    lines = ["=== CORTEX MEMORY CONTEXT ==="]
    if scored_facts:
        lines.append("KNOWLEDGE:")
        for _, row in scored_facts:
            lines.append(f"- {row['subject']} {row['predicate']} {row['object']} "
                         f"(confidence={row['confidence']:.2f})  [fact:{row['id'][:8]}]")
    if episodes:
        lines.append("RELEVANT HISTORY:")
        for row in episodes:
            summary = row["summary"] or row["content"][:160]
            lines.append(f"- {summary}  [episode:{row['id'][:8]}]")
    lines.append("=== END CORTEX MEMORY CONTEXT ===")
    text = "\n".join(lines)
    # Hard char-budget cap as a stand-in for a real tokenizer; keeps worst case bounded.
    char_budget = token_budget * 4
    if len(text) > char_budget:
        text = text[:char_budget] + "\n[... truncated to fit token budget ...]"
    return {"text": text, "facts": [r["id"] for _, r in scored_facts], "episodes": [r["id"] for r in episodes]}
