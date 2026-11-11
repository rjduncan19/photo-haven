"""M1 acceptance: scan a copy of the M0 duplicate-case corpus and reproduce
its expected.json labels (spikes/m0/make_dupe_corpus.py).

The corpus is built from real photos, so it is never committed. Point
PHOTOHAVEN_DUPECASES at a corpus folder (the one holding expected.json and
library/); the test is skipped when there is none.
"""
from __future__ import annotations

import collections
import json
import os
import pathlib
import shutil

import pytest

from photohaven import app
from photohaven.scan import ScanOptions
from photohaven.util import long_path

DEFAULT = pathlib.Path(r"C:\PH-test\dupecases")


def corpus() -> pathlib.Path | None:
    p = pathlib.Path(os.environ.get("PHOTOHAVEN_DUPECASES", DEFAULT))
    return p if (p / "expected.json").exists() and (p / "library").is_dir() else None


def partition(pairs) -> set[frozenset]:
    groups = collections.defaultdict(set)
    for path, label in pairs:
        groups[label].add(path)
    return {frozenset(g) for g in groups.values()}


@pytest.mark.dupecases
@pytest.mark.exiftool
def test_dupecases(tmp_path, exiftool_exe):
    src = corpus()
    if src is None:
        pytest.skip("no duplicate-case corpus (set PHOTOHAVEN_DUPECASES)")
    expected = json.loads((src / "expected.json").read_text(encoding="utf-8"))
    if expected.get("phase") != "build":
        pytest.skip("corpus has been mutated; rebuild it")
    lib = tmp_path / "library"
    shutil.copytree(long_path(src / "library"), long_path(lib))   # copy2: keeps mtimes
    env, _ = app.init(tmp_path / "state", tmp_path / "home", [("dupecases", lib)], machine_name="accept",
                      exiftool=exiftool_exe)
    try:
        res = app.scan(env, ScanOptions())
        assert res.errors == 0 and not res.failed_roots, res.warnings
        rows = {r["relpath"]: r for r in env.conn.execute(
            "SELECT f.relpath, f.media, f.sha256, c.content_sha256, c.damaged FROM files f "
            "LEFT JOIN contents c USING(sha256)")}
        files = expected["files"]
        assert {f["path"] for f in files} == set(rows)
        hashed = [f for f in files if rows[f["path"]]["media"] != "other"]
        assert all(rows[f["path"]]["sha256"] for f in hashed)
        # exact-duplicate groups = shared sha256
        assert partition((f["path"], rows[f["path"]]["sha256"]) for f in hashed) == \
            partition((f["path"], f["exact"]) for f in hashed)
        # content-equivalent groups = shared content_sha256 (damaged files excluded)
        ok = [f for f in hashed if f["content"] and not f["damaged"] and rows[f["path"]]["content_sha256"]]
        assert partition((f["path"], rows[f["path"]]["content_sha256"]) for f in ok) == \
            partition((f["path"], f["content"]) for f in ok)
        missing_content = [f["path"] for f in hashed if f["content"] and not f["damaged"]
                           and not rows[f["path"]]["content_sha256"] and f["case"] != "zero_bytes"]
        assert not missing_content
        # damage
        for f in hashed:
            if f["damaged"] is True:
                assert rows[f["path"]]["damaged"] == 1, f["path"]
            elif f["damaged"] is False:
                assert rows[f["path"]]["damaged"] == 0, f["path"]
        # a rescan hashes nothing; a rebuild reproduces the facts
        assert app.scan(env, ScanOptions()).files_hashed == 0
        before = sorted(tuple(r) for r in env.conn.execute("SELECT relpath, sha256, state FROM files"))
        app.rebuild_db(env)
        assert sorted(tuple(r) for r in env.conn.execute("SELECT relpath, sha256, state FROM files")) == before
    finally:
        env.close()
