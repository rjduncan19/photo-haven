"""Decision logs, import, and effective resolution (03 §5, 07 §5)."""
from __future__ import annotations

import json

import pytest

from photohaven import app, decisions as dec, formats
from photohaven.formats import Decision, Target, canonical
from photohaven.scan import ScanOptions

from conftest import join_machine
from fixtures.gen import build_library, copy_of


def at(n: int) -> str:
    return f"2026-11-12T05:00:{n:02d}.000000Z"


def add(env, target: Target, action: str, n: int, machine=None):
    d = Decision(at(n), "dedupe", target, action, "cli")
    return dec.append(env.conn, env.project_home, env.library.library_id,
                      (machine or env).machine.machine_id, [d])


@pytest.fixture
def scanned(lib, fake):
    env, root, tmp = lib
    build_library(root, {"Trip": ["a.jpg"], "Backup/2015": [copy_of("Trip/a.jpg")], "Camera Roll": ["c.jpg"]})
    app.scan(env, ScanOptions(), describer=fake)
    rid = env.library.roots[0].root_id
    sha = env.conn.execute("SELECT sha256 FROM files WHERE relpath = 'Backup/2015/a.jpg'").fetchone()[0]
    return env, root, tmp, rid, sha


def eff(env, rid, path, sha):
    return dec.effective(env.conn, env.library, rid, path, sha)


def test_resolution_levels(scanned):
    env, root, tmp, rid, sha = scanned
    p = "Backup/2015/a.jpg"
    assert eff(env, rid, p, sha).action == "undecided"
    add(env, Target("folder", rid, "Backup", scope="subtree"), "remove", 1)
    e = eff(env, rid, p, sha)
    assert (e.action, e.level) == ("remove", "subtree")
    add(env, Target("folder", rid, "Backup/2015", scope="folder"), "keep", 2)
    assert (eff(env, rid, p, sha).action, eff(env, rid, p, sha).level) == ("keep", "folder")
    add(env, Target("file", rid, p, sha256=sha, size=1), "remove", 3)
    assert eff(env, rid, p, sha).level == "file"
    # a file decision about different bytes doesn't apply
    assert eff(env, rid, p, "0" * 64).level == "folder"
    # 'undecided' at a level falls through to the next one
    add(env, Target("file", rid, p, sha256=sha, size=1), "undecided", 4)
    add(env, Target("folder", rid, "Backup/2015", scope="folder"), "undecided", 5)
    assert (eff(env, rid, p, sha).action, eff(env, rid, p, sha).level) == ("remove", "subtree")


def test_latest_wins_across_machines(scanned):
    env, root, tmp, rid, sha = scanned
    b = join_machine(tmp, root)
    try:
        t = Target("folder", rid, "Backup", scope="subtree")
        add(env, t, "remove", 10)
        add(b, t, "keep", 20, machine=b)
        add(env, t, "remove", 15)
        dec.ingest(env.conn, env.project_home, env.library.library_id)
        assert eff(env, rid, "Backup/2015/a.jpg", sha).action == "keep"
    finally:
        b.close()


def test_protected_folder_ignores_folder_removes(scanned):
    env, root, tmp, rid, sha = scanned
    env.library.roots[0].intake_folders = ["Camera Roll"]
    csha = env.conn.execute("SELECT sha256 FROM files WHERE relpath = 'Camera Roll/c.jpg'").fetchone()[0]
    add(env, Target("folder", rid, "", scope="subtree"), "remove", 1)
    e = eff(env, rid, "Camera Roll/c.jpg", csha)
    assert e.action == "undecided" and "protected" in e.chain[-1].note
    add(env, Target("file", rid, "Camera Roll/c.jpg", sha256=csha, size=1), "remove", 2)
    assert eff(env, rid, "Camera Roll/c.jpg", csha).action == "remove"


def test_log_seq_and_torn_line(scanned):
    env, root, tmp, rid, sha = scanned
    add(env, Target("folder", rid, "Trip", scope="folder"), "keep", 1)
    p = dec.log_path(env.project_home, env.machine.machine_id)
    with open(p, "ab") as f:
        f.write(b'{"record":"decision","id":"tor')          # crash mid-append
    add(env, Target("folder", rid, "Backup", scope="folder"), "remove", 2)
    header, recs, warnings = formats.read_decision_log(p)
    assert [d.seq for d in recs] == [1, 2] and warnings
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.startswith('{"record":"decision","id":"tor'):
            continue
        assert canonical(json.loads(line)) == line


def _export(env, rid, records, library_id=None, report_id="0193b5e2-7c1a-7d4e-8f2a-3b4c5d6e7f80"):
    lines = [{"record": "header", "schema": 1, "kind": "decision-export", "library_id": library_id or env.library.library_id,
              "report_id": report_id, "scan_id": 1, "generated_at": at(0)}]
    for t, a in records:
        lines.append({"record": "decision", "at": at(1), "phase": "dedupe", "target": t, "action": a,
                      "origin": "report", "report_id": report_id})
    return "".join(canonical(l) + "\n" for l in lines)


def test_import_round_trip_and_idempotence(scanned):
    env, root, tmp, rid, sha = scanned
    f = tmp / "decisions-x.jsonl"
    f.write_text(_export(env, rid, [({"kind": "folder", "root_id": rid, "path": "Backup", "scope": "subtree"}, "remove")]),
                 encoding="utf-8")
    r = dec.import_export(env.conn, env.project_home, env.state_dir, env.library, env.machine.machine_id, f)
    assert (r.added, r.skipped) == (1, 0)
    r2 = dec.import_export(env.conn, env.project_home, env.state_dir, env.library, env.machine.machine_id, f)
    assert (r2.added, r2.skipped) == (0, 1)
    row = env.conn.execute("SELECT origin, report_id FROM decisions").fetchone()
    assert row["origin"] == "import" and row["report_id"] == r.report_id
    assert eff(env, rid, "Backup/2015/a.jpg", sha).action == "remove"


def test_import_refuses_other_library(scanned):
    env, root, tmp, rid, sha = scanned
    f = tmp / "decisions-y.jsonl"
    f.write_text(_export(env, rid, [], library_id="00000000-0000-4000-8000-000000000000"), encoding="utf-8")
    with pytest.raises(formats.FormatError):
        dec.import_export(env.conn, env.project_home, env.state_dir, env.library, env.machine.machine_id, f)


def test_orphaned_decisions(scanned):
    env, root, tmp, rid, sha = scanned
    add(env, Target("folder", rid, "Gone", scope="folder"), "remove", 1)
    add(env, Target("folder", rid, "Trip", scope="folder"), "keep", 2)
    assert [r["path"] for r in dec.orphaned(env.conn)] == ["Gone"]
