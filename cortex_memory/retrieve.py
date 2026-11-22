"""Read path: BM25 + graph expansion -> Reciprocal Rank Fusion -> context assembly.

No mandatory vector index for v1 (keeps the system LLM-API-key-free and fully
offline). Vector search is an optional later addition keyed by memory id and
model version -- plug it into `_vector_candidates` without touching the rest
of the pipeline.
"""

import math

RRF_K = 60
DEFAULT_TOKEN_BUDGET = 1900


def _bm25_facts(conn, namespace_id, query_text, limit=20, include_superseded=False):
    statuses = "('active','superseded')" if include_superseded else "('active')"
    rows = conn.execute(
        f"""SELECT f.id, f.subject, f.predicate, f.object, f.confidence, f.importance, f.status,
                   f.recorded_at, f.last_accessed_at, f.access_count, bm25(facts_fts) AS score
            FROM facts_fts JOIN facts f ON f.rowid = facts_fts.rowid
            WHERE facts_fts MATCH ? AND f.namespace_id = ? AND f.status IN {statuses}
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


def _graph_expand(conn, namespace_id, query_text, hops=1, limit=20):  # noqa: ARG001
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
        f"""SELECT id, subject, predicate, object, confidence, importance, status, recorded_at,
                   last_accessed_at, access_count
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


def query(conn, namespace_id, text, limit=10, token_budget=DEFAULT_TOKEN_BUDGET, **kwargs):
    """Full read-path entry point. Returns an assembled context dict (see `search` for kwargs)."""
    scored, episodes = search(conn, namespace_id, text, limit=limit, **kwargs)
    return _assemble_context(namespace_id, scored, episodes, token_budget)


def search(conn, namespace_id, text, limit=10, global_namespace_id=None, min_confidence=0.0,
           exclude_episode_ids=(), include_global=True, include_superseded=False):
    """Ranked retrieval. Returns ([(score, fact_row, scope_label)], [episode_rows]).

    Searches the project namespace and (by default) the global one. Each
    namespace gets its OWN BM25 ranking (raw BM25 scores are not comparable
    across indexes), then all ranked lists are fused with weighted RRF so a
    large namespace cannot drown a small, more relevant one. Project hits
    outrank global hits of equal relevance (weights in namespaces.py).
    """
    from .namespaces import (WEIGHT_GLOBAL_FTS, WEIGHT_GLOBAL_GRAPH,
                             WEIGHT_PROJECT_FTS, WEIGHT_PROJECT_GRAPH)

    fts_query = _fts_escape(text)
    scopes = []   # (namespace_id, fts_weight, graph_weight, label)
    if namespace_id:
        scopes.append((namespace_id, WEIGHT_PROJECT_FTS, WEIGHT_PROJECT_GRAPH, "project"))
    if include_global and global_namespace_id and global_namespace_id != namespace_id:
        scopes.append((global_namespace_id, WEIGHT_GLOBAL_FTS, WEIGHT_GLOBAL_GRAPH, "global"))

    ranked, weights, fact_rows, ns_of = [], [], {}, {}
    episodes = []
    for ns_id, w_fts, w_graph, label in scopes:
        bm25 = list(_bm25_facts(conn, ns_id, fts_query, include_superseded=include_superseded))
        graph = list(_graph_expand(conn, ns_id, text))
        ranked += [bm25, graph]
        weights += [w_fts, w_graph]
        for r in bm25 + graph:
            fact_rows[r["id"]] = r
            ns_of[r["id"]] = label
        episodes += [(label, e) for e in _bm25_episodes(conn, ns_id, fts_query)
                     if e["id"] not in exclude_episode_ids]

    fused = _rrf_merge(*ranked, weights=weights) if ranked else {}
    top = max(fused.values()) if fused else 1.0
    scored = []
    for fid, rrf_score in fused.items():
        row = fact_rows[fid]
        confidence = row["confidence"] if row["confidence"] is not None else 0.5
        if confidence < min_confidence:
            continue
        importance = row["importance"] if row["importance"] is not None else 0.5
        # Relevance dominates; confidence/importance only nudge (+-10%).
        quality = 0.5 * confidence + 0.5 * importance
        final = (rrf_score / top) * (0.9 + 0.2 * quality)
        scored.append((final, row, ns_of[fid]))
    scored.sort(key=lambda x: x[0], reverse=True)
    scored = scored[:limit]

    for _, row, _ in scored:
        conn.execute(
            "UPDATE facts SET last_accessed_at = datetime('now'), access_count = access_count + 1 WHERE id = ?",
            (row["id"],),
        )
    episodes.sort(key=lambda le: (le[0] != "project",))   # project history first (stable)
    return scored, [e for _, e in episodes][:5]


def _recency_score(last_accessed_at) -> float:
    if not last_accessed_at:
        return 0.3
    return 0.8  # placeholder weighting; refined by decay.py's importance recalculation


def _display(row) -> str:
    """Human line for a fact: multi-valued predicates are `kind:hash` -> show `[kind] object`."""
    pred = row["predicate"]
    if ":" in pred:
        kind, tail = pred.split(":", 1)
        if kind in ("preference", "decision", "stack", "file", "note", "command", "error", "error_fix"):
            return f"[{kind.replace('_', '-')}] {row['object']}"
        if kind == "uses":
            return f"[stack] project uses {row['object']} as the {tail}"
    return f"{row['subject']} {pred} {row['object']}"


def _assemble_context(namespace_id, scored_facts, episodes, token_budget):
    lines = ["=== CORTEX MEMORY CONTEXT ==="]
    if scored_facts:
        lines.append("KNOWLEDGE:")
        for _, row, label in scored_facts:
            scope = "" if label == "project" else " (global)"
            lines.append(f"- {_display(row)}{scope}  [fact:{row['id'][:8]}]")
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
    return {"text": text, "facts": [r["id"] for _, r, _ in scored_facts], "episodes": [r["id"] for r in episodes],
            "scopes": {r["id"]: label for _, r, label in scored_facts}}
