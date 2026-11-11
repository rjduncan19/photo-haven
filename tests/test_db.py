import sqlite3

import pytest

from photohaven import db
from photohaven.formats import NewerSchemaError

from conftest import REPO


def test_packaged_schema_matches_spec():
    assert db.schema_sql() == (REPO / "spec" / "schema.sql").read_text(encoding="utf-8")


def test_create_and_reopen(tmp_path):
    p = tmp_path / "x.db"
    c = db.connect(p)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 1
    assert db.get_meta(c, "schema_version") == "1"
    c.close()
    c = db.connect(p)
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c.close()


def test_refuses_newer(tmp_path):
    p = tmp_path / "x.db"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE t(x)")
    c.execute("PRAGMA user_version = 9")
    c.commit()
    c.close()
    with pytest.raises(NewerSchemaError):
        db.connect(p)
