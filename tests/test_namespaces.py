import os
import subprocess

import pytest

from cortex_memory import capture, db, namespaces, retrieve


def mkrepo(path):
    (path / ".git").mkdir(parents=True)
    return path


# ---- resolution -----------------------------------------------------------
def test_subdirectories_resolve_to_one_project(tmp_path):
    repo = mkrepo(tmp_path / "repo")
    for sub in ("src", "tests", "docs", ".claude", "src/deep/er"):
        (repo / sub).mkdir(parents=True, exist_ok=True)
    names = {namespaces.project_name(repo / s) for s in ("", "src", "tests", "docs", ".claude", "src/deep/er")}
    assert len(names) == 1


def test_unrelated_repos_differ(tmp_path):
    a, b = mkrepo(tmp_path / "a"), mkrepo(tmp_path / "b")
    assert namespaces.project_name(a) != namespaces.project_name(b)


def test_nested_repo_uses_innermost(tmp_path):
    outer = mkrepo(tmp_path / "outer")
    inner = mkrepo(outer / "vendor" / "inner")
    assert namespaces.project_name(inner / ".git" / "..") == namespaces.project_name(inner)
    assert namespaces.project_name(inner) != namespaces.project_name(outer)


def test_worktree_git_file_is_its_own_project(tmp_path):
    main = mkrepo(tmp_path / "main")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: ../main/.git/worktrees/wt")
    assert namespaces.project_name(wt) != namespaces.project_name(main)
    (wt / "src").mkdir()
    assert namespaces.project_name(wt / "src") == namespaces.project_name(wt)


def test_non_git_directory_uses_itself(tmp_path):
    d = tmp_path / "scratch" / "x"
    d.mkdir(parents=True)
    assert namespaces.project_name(d).endswith("/scratch/x") or namespaces.project_name(d).endswith("scratch/x")


def test_missing_or_empty_cwd():
    assert namespaces.project_name(None) is None
    assert namespaces.project_name("") is None


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_symlinked_repo_resolves_to_real_root(tmp_path):
    repo = mkrepo(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    assert namespaces.project_name(link) == namespaces.project_name(repo)


def test_matches_real_git_toplevel(tmp_path):
    try:
        subprocess.run(["git", "init", "-q", str(tmp_path / "g")], check=True, capture_output=True)
    except Exception:
        pytest.skip("git unavailable")
    (tmp_path / "g" / "sub").mkdir()
    top = subprocess.run(["git", "-C", str(tmp_path / "g" / "sub"), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True).stdout.strip()
    assert namespaces.project_name(tmp_path / "g" / "sub") == namespaces.project_name(top)


# ---- cross-namespace retrieval ---------------------------------------------
def say(conn, cwd, text):
    scope = namespaces.resolve(conn, cwd)
    ns = scope["project"] or scope["global"]
    return capture.ingest(conn, ns, "user_message", text, source_type="user_provided"), scope


def ask(conn, cwd, text, **kw):
    scope = namespaces.resolve(conn, cwd)
    ns = scope["project"] or scope["global"]
    return retrieve.query(conn, ns, text, global_namespace_id=scope["global"], **kw)


def texts(conn, result):
    return [conn.execute("SELECT object FROM facts WHERE id=?", (f,)).fetchone()["object"] for f in result["facts"]]


def test_end_to_end_cross_project_memory(conn, tmp_path):
    """Oracle's acceptance scenario: preference follows the user, decision stays in its project."""
    cortex = mkrepo(tmp_path / "cortex")
    other = mkrepo(tmp_path / "other")
    (cortex / "src").mkdir()

    r, _ = say(conn, cortex, "We decided to use SQLite for Cortex storage. Always run pytest before committing.")
    assert sorted(f["kind"] for f in r["facts"]) == ["decision", "preference"]

    # Session 2: different project, different directory.
    pref = ask(conn, other, "which testing practice pytest do I follow before committing")
    assert any("pytest" in t for t in texts(conn, pref))
    assert set(pref["scopes"].values()) == {"global"}

    # The project-specific decision must NOT leak into the other project.
    leak = ask(conn, other, "which database should we use SQLite storage")
    assert not any("SQLite" in t for t in texts(conn, leak))

    # Same project, from a subdirectory: decision is found.
    back = ask(conn, cortex / "src", "which database should we use SQLite storage")
    assert any("SQLite" in t for t in texts(conn, back))
    assert "project" in set(back["scopes"].values())


def test_include_global_false_is_project_only(conn, tmp_path):
    a = mkrepo(tmp_path / "a")
    say(conn, a, "Always run pytest before committing.")
    res = ask(conn, a, "pytest committing", include_global=False)
    assert res["facts"] == []


def test_project_outranks_global_of_equal_relevance(conn, tmp_path):
    a = mkrepo(tmp_path / "a")
    scope = namespaces.resolve(conn, a)
    for ns, obj in ((scope["global"], "Use ruff for linting everywhere"),
                    (scope["project"], "Use ruff for linting in this repo")):
        capture.merge_fact(conn, ns, capture.CandidateFact("project", "decision:" + obj[:6], obj, 0.9))
    res = ask(conn, a, "ruff linting")
    assert res["scopes"][res["facts"][0]] == "project"


def test_large_global_namespace_cannot_drown_project(conn, tmp_path):
    a = mkrepo(tmp_path / "a")
    scope = namespaces.resolve(conn, a)
    for i in range(60):
        capture.merge_fact(conn, scope["global"],
                           capture.CandidateFact("user", f"note:{i}", f"deploy notes variant {i} for kubernetes", 0.9))
    capture.merge_fact(conn, scope["project"],
                       capture.CandidateFact("project", "decision:k", "kubernetes deploy uses helm here", 0.9))
    res = ask(conn, a, "kubernetes deploy", limit=3)
    assert "project" in set(res["scopes"].values())


def test_min_confidence_filters(conn, tmp_path):
    a = mkrepo(tmp_path / "a")
    scope = namespaces.resolve(conn, a)
    capture.merge_fact(conn, scope["project"], capture.CandidateFact("project", "note:x", "flaky guess about redis", 0.2))
    assert ask(conn, a, "redis guess", min_confidence=0.3)["facts"] == []
    assert ask(conn, a, "redis guess")["facts"] != []


def test_superseded_fact_not_returned_as_current(conn, tmp_path):
    a = mkrepo(tmp_path / "a")
    say(conn, a, "The project uses SQLite as the database.")
    say(conn, a, "The project uses PostgreSQL as the database.")
    res = ask(conn, a, "database")
    ts = texts(conn, res)
    assert "PostgreSQL" in ts and "SQLite" not in ts


def test_prompt_episode_excluded_from_its_own_context(conn, tmp_path):
    a = mkrepo(tmp_path / "a")
    r, scope = say(conn, a, "database storage question about sqlite")
    res = retrieve.query(conn, scope["project"], "database storage sqlite", global_namespace_id=scope["global"],
                         exclude_episode_ids={r["episode_id"]})
    assert r["episode_id"] not in res["episodes"]


# ---- promotion and legacy consolidation -------------------------------------
def test_promote_is_idempotent_and_keeps_original(conn, tmp_path):
    a = mkrepo(tmp_path / "a")
    r, scope = say(conn, a, "We decided to use ruff for linting.")
    fid = r["facts"][0]["fact_id"]
    g1, op1 = namespaces.promote(conn, fid)
    g2, op2 = namespaces.promote(conn, fid)
    assert op1 == "insert" and op2 == "reinforce" and g1 == g2
    assert conn.execute("SELECT status FROM facts WHERE id=?", (fid,)).fetchone()["status"] == "active"
    assert conn.execute("SELECT COUNT(*) FROM facts WHERE namespace_id=?", (scope["global"],)).fetchone()[0] == 1


def test_consolidate_legacy_merges_fragments(conn, tmp_path):
    repo = mkrepo(tmp_path / "repo")
    (repo / "src").mkdir()
    n_root = db.get_or_create_namespace(conn, f"project:{repo}", "project")
    n_sub = db.get_or_create_namespace(conn, f"project:{repo / 'src'}", "project")
    capture.merge_fact(conn, n_root, capture.CandidateFact("project", "decision:a", "We use A", 0.9))
    capture.merge_fact(conn, n_sub, capture.CandidateFact("project", "decision:b", "We use B", 0.9))
    out = namespaces.consolidate_legacy(conn)
    assert out["namespaces_merged"] == 2
    canon = db.get_or_create_namespace(conn, namespaces.project_name(repo))
    assert conn.execute("SELECT COUNT(*) FROM facts WHERE namespace_id=?", (canon,)).fetchone()[0] == 2
    assert namespaces.consolidate_legacy(conn) == {"namespaces_merged": 0, "rows_moved": 0}
