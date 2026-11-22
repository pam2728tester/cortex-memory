"""Drive the server with a real MCP client over stdio. A single stray print() to
stdout would corrupt JSON-RPC framing and fail these tests."""

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

mcp_client = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def run(tmp_path, scenario, cwd=None):
    repo = cwd or tmp_path / "proj"
    (repo / ".git").mkdir(parents=True, exist_ok=True)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "cortex_memory.mcp_server"], cwd=str(ROOT),
        env={**os.environ, "CORTEX_MEMORY_DIR": str(tmp_path / "data"), "CORTEX_PROJECT_DIR": str(repo)})

    async def go():
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                return await scenario(session)

    return asyncio.run(asyncio.wait_for(go(), 60))


async def call(session, name, **args):
    res = await session.call_tool(name, args)
    assert not res.isError, res
    return json.loads(res.content[0].text)


def test_lists_exactly_four_tools_with_schemas(tmp_path):
    async def scenario(s):
        return (await s.list_tools()).tools
    tools = {t.name: t for t in run(tmp_path, scenario)}
    assert set(tools) == {"memory_save", "memory_query", "memory_forget", "memory_status"}
    assert tools["memory_save"].inputSchema["required"] == ["content"]
    assert set(tools["memory_save"].inputSchema["properties"]["memory_type"]["enum"]) == {
        "fact", "decision", "preference", "command", "error_fix", "project", "note"}
    assert tools["memory_query"].inputSchema["required"] == ["query"]


def test_full_lifecycle_over_the_wire(tmp_path):
    async def scenario(s):
        saved = await call(s, "memory_save", content="We decided to use SQLite for Cortex", memory_type="decision",
                           tags=["db"])
        dup = await call(s, "memory_save", content="We decided to use SQLite for Cortex", memory_type="decision")
        pref = await call(s, "memory_save", content="Always run pytest before committing", memory_type="preference")
        found = await call(s, "memory_query", query="which database sqlite")
        status = await call(s, "memory_status", include_recent=True)
        soft = await call(s, "memory_forget", memory_id=saved["memory_id"], reason="test")
        gone = await call(s, "memory_query", query="which database sqlite")
        perm = await call(s, "memory_forget", memory_id=pref["memory_id"], permanent=True)
        return saved, dup, found, status, soft, gone, perm

    saved, dup, found, status, soft, gone, perm = run(tmp_path, scenario)
    assert saved["success"] and saved["created"] and dup["deduplicated"]
    assert found["results"][0]["memory_id"] == saved["memory_id"] and found["total"] >= 1
    assert status["active_memories"] == 2 and status["fts5"]
    assert soft["deleted"] and not soft["permanent"]
    assert gone["results"] == []
    assert perm["permanent"] and perm["deleted"]


def test_errors_are_structured_not_crashes(tmp_path):
    async def scenario(s):
        bad = await call(s, "memory_save", content="")
        secret = await call(s, "memory_save", content="key sk-abcdefghijklmnopqrstuvwxyz123456")
        nf = await call(s, "memory_forget", memory_id="nope")
        ok = await call(s, "memory_status")           # server still alive afterwards
        return bad, secret, nf, ok

    bad, secret, nf, ok = run(tmp_path, scenario)
    assert bad["success"] is False and "content" in bad["error"]
    assert secret["success"] is False and "secret" in secret["error"]
    assert nf["success"] is False
    assert ok["status"] == "healthy"


def test_cross_project_via_two_server_sessions(tmp_path):
    async def save(s):
        a = await call(s, "memory_save", content="Prefer small commits", memory_type="preference")
        b = await call(s, "memory_save", content="Billing service runs on port 9000", memory_type="fact")
        return a, b

    run(tmp_path, save, cwd=tmp_path / "A")

    async def ask(s):
        return await call(s, "memory_query", query="small commits billing port")

    res = run(tmp_path, ask, cwd=tmp_path / "B")            # different project, same data dir
    contents = [r["content"] for r in res["results"]]
    assert "Prefer small commits" in contents
    assert "Billing service runs on port 9000" not in contents


def test_concurrent_writers_do_not_corrupt(tmp_path):
    async def scenario(s):
        outs = await asyncio.gather(*[
            call(s, "memory_save", content=f"parallel memory number {i}", memory_type="note") for i in range(20)])
        return outs, await call(s, "memory_status")

    outs, status = run(tmp_path, scenario)
    assert all(o["success"] for o in outs) and status["active_memories"] == 20
