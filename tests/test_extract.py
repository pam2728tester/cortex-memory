import pytest

from cortex_memory import extract


def texts(s):
    return list(extract.extract_text(s))


# ---- must extract ---------------------------------------------------------
POSITIVE = [
    ("I prefer pytest over unittest for all tests.", "preference"),
    ("Always run pytest before committing.", "preference"),
    ("Never commit directly to main.", "preference"),
    ("Don't use print statements for logging.", "preference"),
    ("From now on, answer in short sentences.", "preference"),
    ("My preferred editor is neovim.", "preference"),
    ("Remember that the staging DB resets every night.", "note"),
    ("We decided to use SQLite instead of PostgreSQL.", "decision"),
    ("Let's go with FastAPI for the service layer.", "decision"),
    ("We'll use ruff for linting.", "decision"),
    ("Decision: keep snapshots under 50 MB.", "decision"),
    ("We decided NOT to use Redis.", "decision"),
    ("This project uses FastAPI and SQLite.", "stack"),
    ("We use Postgres for the billing data.", "stack"),
    ("`db.py` contains the schema and migration code.", "file"),
]


@pytest.mark.parametrize("text,kind", POSITIVE)
def test_extracts(text, kind):
    cands = texts(text)
    assert cands, f"nothing extracted from: {text}"
    assert cands[0].kind == kind


def test_negation_preserved():
    (c,) = texts("We decided NOT to use Redis.")
    assert "NOT" in c.object and "use Redis" in c.object


def test_single_valued_component():
    (c,) = [c for c in texts("The project uses SQLite as the database.") if c.single_valued]
    assert c.predicate == "uses:database" and c.object == "SQLite"


# ---- must NOT extract -----------------------------------------------------
NEGATIVE = [
    "What database should we use?",
    "Should we decide to use Redis?",
    "If we decide to use Redis, we will need a server.",
    "I don't know why this fails.",
    "Do you prefer tabs or spaces?",
    "Please look at the failing test.",
    "",
    "ok",
    "```python\nalways = 1\n```",
    "The assistant suggested we could use Kafka.",
]


@pytest.mark.parametrize("text", NEGATIVE)
def test_no_false_positive(text):
    assert texts(text) == []


def test_secret_dropped_not_stored():
    s = extract.Stats()
    out = extract.extract_text("Always export API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456 first.", s)
    assert out == []
    assert s.rejected_secret == 1


def test_oversized_skipped():
    assert extract.extract_text("Always x. " * 20000) == []


def test_malformed_input_never_crashes():
    assert extract.extract_text(None) == []
    assert extract.extract_tool("Bash", None, None) == []
    assert extract.extract_tool("Bash", {"command": 5}, {"stdout": None}) == []


def test_duplicate_sentence_deduped():
    assert len(texts("Always run pytest first. Always run pytest first.")) == 1


# ---- tool results ---------------------------------------------------------
def bash(cmd, out):
    return extract.extract_tool("Bash", {"command": cmd}, {"stdout": out, "stderr": ""})


def test_passing_tests_become_command_fact():
    (c,) = bash("cd /repo && pytest -q", "464 passed in 101.2s")
    assert c.kind == "command" and "`pytest -q`" in c.object and c.confidence >= 0.9


def test_failing_tests_are_error_not_success():
    (c,) = bash("pytest -q", "FAILED tests/x.py::t - assert 1 == 2\n2 failed, 5 passed in 1s")
    assert c.kind == "error"


def test_traceback_error_line_captured():
    (c,) = bash("pytest", "Traceback (most recent call last):\n  File x\nModuleNotFoundError: No module named 'foo'")
    assert c.kind == "error" and "ModuleNotFoundError" in c.object


def test_non_test_command_ignored():
    assert bash("ls -la", "total 0") == []


def test_no_evidence_no_fact():
    assert bash("pytest", "") == []


def test_secret_in_command_dropped():
    assert bash("pytest --token sk-abcdefghijklmnopqrstuvwxyz123456", "3 passed") == []


def test_other_tools_ignored():
    assert extract.extract_tool("Edit", {"file_path": "a.py"}, {"ok": True}) == []


def test_llm_hook_validated():
    extract.register_llm_extractor(lambda t: [
        {"kind": "decision", "object": "Use WAL mode for SQLite", "confidence": 0.9},
        {"kind": "bogus", "object": "x" * 20, "confidence": 0.9},
        {"kind": "note", "object": "low confidence item here", "confidence": 0.2},
        {"kind": "note", "object": "key sk-abcdefghijklmnopqrstuvwxyz123456 leak", "confidence": 0.9},
    ])
    try:
        out = extract.extract_llm("anything")
        assert [c.object for c in out] == ["Use WAL mode for SQLite"]
    finally:
        extract.register_llm_extractor(None)
