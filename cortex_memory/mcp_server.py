"""Cortex Memory MCP server (stdio).

Run:  python -m cortex_memory.mcp_server

Four tools: memory_save, memory_query, memory_forget, memory_status. They are
thin wrappers over MemoryService, so validation and privacy rules are the same
as for hooks and the CLI.

stdout is the JSON-RPC transport. Nothing here may print to it; diagnostics go
to stderr only. Caller mistakes come back as {"success": false, "error": ...};
unexpected failures are logged to stderr and reported without internals.
"""

from __future__ import annotations

import logging
import sys
from typing import Literal

from mcp.server.fastmcp import FastMCP

from .service import MemoryError, MemoryService

logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="cortex-mcp %(levelname)s %(message)s")
log = logging.getLogger("cortex-mcp")

MemoryType = Literal["fact", "decision", "preference", "command", "error_fix", "project", "note"]

mcp = FastMCP(
    "cortex-memory",
    instructions=(
        "Persistent local memory. Use memory_query before answering questions about the user's preferences, "
        "past decisions or project setup. Use memory_save for durable facts the user states or confirms "
        "(preferences, decisions, working commands, error fixes); never save secrets. Preferences are shared "
        "across projects, everything else is scoped to the current git project."),
)


def _run(fn, *args, **kwargs) -> dict:
    try:
        return fn(*args, **kwargs)
    except MemoryError as exc:
        return {"success": False, "error": str(exc)}
    except Exception:
        log.exception("tool failed")
        return {"success": False, "error": "internal error (see server stderr)"}


_service = MemoryService()


@mcp.tool()
def memory_save(
    content: str,
    memory_type: MemoryType = "note",
    namespace: str | None = None,
    project: str | None = None,
    tags: list[str] | None = None,
    source: Literal["explicit", "hook", "import", "llm"] = "explicit",
    confidence: float = 0.9,
    importance: float = 0.5,
    metadata: dict | None = None,
) -> dict:
    """Save a durable memory. Identical content in the same namespace is deduplicated.

    Preferences go to the global namespace (shared by all projects); other types go to the
    current project's namespace unless `namespace` ('global:user' or 'project:<path>') or
    `project` (a directory) is given. Content containing secrets is rejected.
    """
    return _run(_service.save, content, memory_type, namespace, project, tags, source, confidence, importance, metadata)


@mcp.tool()
def memory_query(
    query: str,
    namespace: str | None = None,
    project: str | None = None,
    memory_types: list[MemoryType] | None = None,
    limit: int = 10,
    include_global: bool = True,
    include_archived: bool = False,
    min_confidence: float = 0.3,
) -> dict:
    """Search memory. Searches the current project plus global preferences, ranked by relevance
    (project results outrank global ones of equal relevance). Superseded facts are hidden unless
    include_archived is true."""
    return _run(_service.query, query, namespace, project, memory_types, limit, include_global,
                include_archived, min_confidence)


@mcp.tool()
def memory_forget(
    memory_id: str,
    namespace: str | None = None,
    reason: str | None = None,
    permanent: bool = False,
) -> dict:
    """Forget a memory by id. Default is a soft delete (hidden from queries, audit kept without
    content). permanent=true erases the content, its source text and any copy in old snapshots."""
    return _run(_service.forget, memory_id, namespace, reason, permanent)


@mcp.tool()
def memory_status(include_namespaces: bool = True, include_recent: bool = False) -> dict:
    """Health and inventory: schema version, memory counts, namespaces, FTS5 availability, last capture."""
    return _run(_service.status, include_namespaces, include_recent)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
