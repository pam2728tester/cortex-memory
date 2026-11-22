# Changelog

## 0.2.0 - 2026-11-22
### Added
- Deterministic extraction engine with source-role gating, negation-safe whole-sentence facts, evidence-based
  command results, error->fix pairing, optional validated LLM hook, counters-only debug stats.
- Git-root namespace resolution, `global:user` fallback, weighted cross-namespace RRF, `promote`,
  `consolidate-namespaces`, `query --cwd`.
- MCP server with `memory_save`, `memory_query`, `memory_forget`, `memory_status`; `MemoryService` shared by all interfaces.
- Token Savior exporter/importer/validator with import ledger.
- Schema v2 (`fact_meta`, `import_ledger`) with in-place upgrade; `secure_delete` enabled.
- 130+ tests including real MCP-client and hook-driven three-session scenarios.
### Fixed
- Hooks silently recorded nothing on a database without a schema; non-object JSON on stdin crashed hooks.
- Restating a fact with different punctuation/case superseded instead of reinforcing it.
- Retrieval scoring let confidence/importance swamp relevance; a prompt echoed into its own context.
- `forget` left source episodes and old snapshots containing the deleted text; CLI forget passed a name as an id.
- Non-existent project paths collapsed into their parent directory.

## 0.1.0
- Initial release.
