"""Cortex Memory: unified local-first agent memory system.

Frankenstein design combining:
  - OctavianTocan/agent-memory  -> multi-agent hook integration pattern
  - adeoluo/agent-memory        -> extractor/merger consolidation pipeline, fact versioning
  - rohitg00/agentmemory        -> supersession, relationship graph, Ebbinghaus decay, RRF fusion, snapshots
  - TAIPANBOX/engram            -> bitemporal facts, GDPR-style forget

Single SQLite database. No mandatory external services, no mandatory LLM API key.
"""

__version__ = "0.1.0"
