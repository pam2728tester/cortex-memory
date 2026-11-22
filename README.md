# Cortex Memory

A unified, local-first, SQLite-backed memory system for AI coding agents (Claude Code, and anything else that can shell out to a CLI or wire in lifecycle hooks). No mandatory LLM API key, no mandatory external services, runs fully offline.

It stores four memory layers on one database — working, episodic, semantic, procedural — with versioned/bitemporal facts, a relationship graph, Reciprocal Rank Fusion retrieval (BM25 + graph), Ebbinghaus-curve importance decay, snapshot/rollback, and GDPR-style forgetting.

## Why this exists

Naive RAG over chat logs answers "what's semantically similar to this query?" That's the wrong question for a coding agent. Cortex Memory instead tracks: what's currently true, what happened before, why a decision was made, and whether that's still valid — and never lets a superseded fact silently resurface.

## Architecture

```
Agent event -> privacy/trust filter -> extractor (candidate facts)
            -> merger (reinforce / supersede / insert) -> SQLite (source of truth)

Query -> BM25 (FTS5) + graph expansion -> Reciprocal Rank Fusion
      -> confidence/importance/recency scoring -> token-budgeted context
```

See `cortex_memory/db.py` for the full schema, `capture.py` for the write path, `retrieve.py` for the read path, `decay.py` for the forgetting curve, and `snapshot.py` for rollback/GDPR-forget.

## Install

```bash
pip install -e .
python -m cortex_memory.cli init
```

Wire it into Claude Code (non-destructive — backs up `settings.json` first, skips hooks already present):

```powershell
pwsh ./install.ps1
```

## Usage

```bash
cortex capture decision "The project uses PostgreSQL as the database."
cortex query "what database does the project use"
cortex decay                 # recalculate importance (Ebbinghaus curve)
cortex snapshot create --reason manual
cortex snapshot list
cortex snapshot restore <id>
cortex forget --subject project   # GDPR-style permanent scrub
```

## What's new in 0.2.0

- **Real fact extraction** (`extract.py`): role-gated, precision-first rules for preferences, decisions, stack,
  file facts, and evidence-based command success/failure read from tool output. Error + edit + passing re-run
  becomes one `error-fix` memory. No LLM or API key needed; an optional, validated LLM hook exists
  (`register_llm_extractor`). Set `CORTEX_DEBUG=1` for a counters-only diagnostics log (never message content).
- **Git-root namespaces + global fallback**: every subdirectory of a repo is one project; personal preferences live in
  `global:user` and follow you across projects; retrieval fuses per-namespace BM25 rankings with weighted RRF.
- **MCP server** (`python -m cortex_memory.mcp_server`): `memory_save`, `memory_query`, `memory_forget`, `memory_status`.
- **Hardened forget**: permanent forget also erases the source episodes and scrubs existing snapshots.
- **Token Savior migration** (`python -m cortex_memory.migration export|import|validate`): idempotent, read-only source.
- Schema v2 upgrades existing databases automatically; hooks can no longer fail silently on a fresh install.

### MCP setup

```bash
pip install -e ".[mcp]"
claude mcp add --scope user cortex-memory -- python -m cortex_memory.mcp_server
```

### Migrating from Token Savior

```bash
python -m cortex_memory.migration export --out ts.jsonl      # source DB opened read-only
python -m cortex_memory.migration import --in ts.jsonl --dry-run
python -m cortex_memory.migration import --in ts.jsonl
python -m cortex_memory.migration validate --in ts.jsonl
```

Then remove Token Savior's `memory-*.sh` hook entries from `settings.json` (`TS_MEMORY_DISABLE=1` only reaches hooks
that inherit it from Claude Code's environment, so removing the entries is the reliable switch).

### Tests

```bash
pip install -e ".[test]" && pytest -q
```

## Credits — this is a Frankenstein build

This project doesn't claim original research. It's a synthesis: concrete design ideas pulled from four existing open-source agent-memory projects, combined into one coherent system and implemented from scratch (no code copied from any of them).

| Idea adopted | Source |
|---|---|
| Multi-agent hook integration pattern (Claude Code / Cline / Codex / Aider / Cursor / Windsurf) | [OctavianTocan/agent-memory](https://github.com/OctavianTocan/agent-memory) |
| Extractor + merger consolidation pipeline, fact versioning | [adeoluo/agent-memory](https://github.com/adeoluo/agent-memory) |
| Supersession, relationship graph, Ebbinghaus decay, BM25+vector+graph RRF fusion, git-style snapshots | [rohitg00/agentmemory](https://github.com/rohitg00/agentmemory) |
| Bitemporal facts, GDPR-style forget | [TAIPANBOX/engram](https://github.com/TAIPANBOX/engram) |

The unified architecture spec ("Cortex Memory") that this repo implements was synthesized by **OpenAI ChatGPT**, prompted and directed through a custom browser-automation CLI (`oracle`), to combine the four source designs above into one concrete, implementable blueprint (schema, write path, read path, decay/supersession, Claude Code hook wiring).

The actual implementation — every file in this repo — was written by **Claude (Anthropic)**, acting as the engineering "Frankenstein," turning that blueprint into working, tested Python.

None of the source repos' code was copied or redistributed; only their publicly documented design ideas were used.

## License

MIT. See `LICENSE`.
