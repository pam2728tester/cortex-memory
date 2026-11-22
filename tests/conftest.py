import pytest

from cortex_memory import db


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """Fresh isolated Cortex DB per test."""
    monkeypatch.setattr(db, "DEFAULT_DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "db_path", lambda: str(tmp_path / "cortex.db"))
    db.init_db()
    c = db.connect()
    yield c
    c.close()


@pytest.fixture
def ns(conn):
    return db.get_or_create_namespace(conn, "project:/repo/demo", "project")
