"""Verify --deep, purge (08 §7) and restore (08 §8), with the safety tests
I-5 and I-6 of 10 §3, and the full flow end to end (M4 'done when')."""
from __future__ import annotations

import json
import os
import pathlib
import shutil

import pytest

from photohaven import app, apply as applier, cli, formats
from photohaven.restore import restore

from test_apply import LIB, contents, decide, plan_and_apply
from fixtures.gen import build_library
from photohaven.formats import DELETE_FOLDER, DELETE_README
from photohaven.scan import ScanOptions


@pytest.fixture
def applied(lib, fake):
    env, root, tmp = lib
    build_library(root, LIB)
    app.scan(env, ScanOptions(), describer=fake)
    decide(env, "Backup 2019", "remove")
    res = plan_and_apply(env)
    assert res.moved == 4
    return env, root, tmp, root / DELETE_FOLDER, res.batch_id


def moved_files(q: pathlib.Path) -> list[pathlib.Path]:
    return [p for p in q.rglob("*") if p.is_file() and p.name not in ("manifest.jsonl", DELETE_README)]


def keeper_of(env, relpath):
    return env.conn.execute("SELECT keeper_relpath FROM quarantine_items WHERE relpath = ?", (relpath,)).fetchone()[0]


# --------------------------------------------------------------------- purge

def test_purge_dry_run_then_execute(applied):
    env, root, tmp, q, batch = applied
    a = applier.Applier(env)
    with pytest.raises(applier.ApplyError, match="hold period"):
        applier.purge(a, batch, execute=False)              # default 14 days
    dry = applier.purge(a, batch, execute=False, min_age_days=0)
    assert len(dry.would_delete) == 4 and not dry.deleted and len(moved_files(q)) == 4
    r = applier.purge(a, batch, execute=True, min_age_days=0)
    assert len(r.deleted) == 4 and not r.refused
    assert not moved_files(q)
    assert (q / batch / "manifest.jsonl").exists()          # kept as a permanent record
    assert env.conn.execute("SELECT state FROM batches WHERE batch_id = ?", (batch,)).fetchone()[0] == "purged"
    events = [e.event for e in a.journal.events]
    assert events.count("purged") == 4


def test_purge_needs_verified_items(applied):
    env, root, tmp, q, batch = applied
    env.conn.execute("UPDATE quarantine_items SET state = 'moved' WHERE batch_id = ?", (batch,))
    with pytest.raises(applier.ApplyError, match="not verified"):
        applier.purge(applier.Applier(env), batch, execute=True, min_age_days=0)


def test_I5_changed_keeper_refuses_purge(applied):
    env, root, tmp, q, batch = applied
    k = root / keeper_of(env, "Backup 2019/a.jpg")
    st = k.stat()
    data = bytearray(k.read_bytes())
    data[-30] ^= 0xFF                                       # same size, mtime put back
    k.write_bytes(bytes(data))
    os.utime(k, ns=(st.st_atime_ns, st.st_mtime_ns))
    (root / keeper_of(env, "Backup 2019/b.jpg")).unlink()  # a missing keeper
    a = applier.Applier(env)
    r = applier.purge(a, batch, execute=True, min_age_days=0)
    refused = dict(r.refused)
    assert "same bytes" in refused["Backup 2019/a.jpg"]
    assert "same bytes" in refused["Backup 2019/DCIM/a (1).jpg"]   # same photo, same kept copy
    assert "missing" in refused["Backup 2019/b.jpg"]
    assert [p for p, _ in r.deleted] == ["Backup 2019/DCIM/c.jpg"]
    left = {p.name for p in moved_files(q)}
    assert left == {"a.jpg", "a (1).jpg", "b.jpg"}          # refused files stay in the DELETE folder
    assert {e.event for e in a.journal.events if e.path in refused} >= {"purge_refused"}


def test_verify_deep_catches_what_stat_cannot(applied):
    env, root, tmp, q, batch = applied
    k = root / keeper_of(env, "Backup 2019/a.jpg")
    st = k.stat()
    data = bytearray(k.read_bytes())
    data[-30] ^= 0xFF
    k.write_bytes(bytes(data))
    os.utime(k, ns=(st.st_atime_ns, st.st_mtime_ns))
    a = applier.Applier(env)
    assert a.verify(batch) == (0, [])                        # nothing new to verify
    ok, failed = a.verify(batch, deep=True)
    assert sorted(p for p, _ in failed) == ["Backup 2019/DCIM/a (1).jpg", "Backup 2019/a.jpg"] and ok == 2


# ------------------------------------------------------------------- restore

def test_restore_puts_files_back(applied):
    env, root, tmp, q, batch = applied
    before = {p.name for p in moved_files(q)}
    r = restore(env.state_dir, batch, env=env)
    assert sorted(r.restored) == ["Backup 2019/DCIM/a (1).jpg", "Backup 2019/DCIM/c.jpg", "Backup 2019/a.jpg",
                                  "Backup 2019/b.jpg"]
    assert not moved_files(q)
    assert (root / "Backup 2019" / "a.jpg").exists()
    assert env.conn.execute("SELECT state FROM batches WHERE batch_id = ?", (batch,)).fetchone()[0] == "restored"
    # the restored files are keepers again: a new plan would move them again
    p, _ = app.make_plan(env)
    assert len(p.items) == 4
    # and a second restore has nothing left to do
    r2 = restore(env.state_dir, batch, env=env)
    assert not (r2.restored or r2.refused)


def test_restore_path_filter_and_collisions(applied):
    env, root, tmp, q, batch = applied
    # someone put the same bytes back by hand at one path, and different bytes at another
    a_src = root / keeper_of(env, "Backup 2019/a.jpg")
    shutil.copy2(a_src, root / "Backup 2019" / "a.jpg")
    (root / "Backup 2019" / "b.jpg").write_bytes(b"something else")
    r = restore(env.state_dir, batch, "Backup 2019/a.jpg", env=env)
    assert r.already == ["Backup 2019/a.jpg"] and not r.restored
    r = restore(env.state_dir, batch, "Backup 2019/b.jpg", env=env)
    assert r.beside == [("Backup 2019/b.jpg", "b (restored).jpg")]
    assert (root / "Backup 2019" / "b (restored).jpg").exists()
    assert (root / "Backup 2019" / "b.jpg").read_bytes() == b"something else"
    names = {p.name for p in moved_files(q)}
    assert names == {"c.jpg", "a (1).jpg"}                   # the DCIM files are still waiting


def test_restore_refuses_unmapped_root(applied):
    env, root, tmp, q, batch = applied
    rid = env.library.roots[0].root_id
    shutil.copytree(q, tmp / "elsewhere")                    # the batch, seen from a machine without the root
    env.mcfg.roots.pop(rid)
    env.mcfg.quarantine_root = str(tmp / "elsewhere")
    r = restore(env.state_dir, batch, env=env)
    assert len(r.refused) == 4 and "not mapped" in r.refused[0][1]


def test_I6_restore_from_the_manifest_alone(applied):
    env, root, tmp, q, batch = applied
    state = env.state_dir
    env.close()
    shutil.rmtree(tmp / "home")                              # project home gone
    for p in state.glob("photohaven.db*"):
        p.unlink()                                           # database gone
    assert cli.main(["--state", str(state), "restore", "--batch", batch]) == 0
    assert (root / "Backup 2019" / "a.jpg").exists() and (root / "Backup 2019" / "DCIM" / "c.jpg").exists()
    assert not moved_files(q)


def test_rebuild_db_restores_batch_records(applied):
    env, root, tmp, q, batch = applied
    applier.purge(applier.Applier(env), batch, execute=False, min_age_days=0)
    rows = lambda: sorted(tuple(r) for r in env.conn.execute(
        "SELECT batch_id, root_id, relpath, sha256, size, keeper_relpath, qpath, state FROM quarantine_items"))
    before = rows()
    app.rebuild_db(env)
    assert rows() == before
    assert env.conn.execute("SELECT state, quarantine_root FROM batches").fetchone()[0] == "verified"
    p, _ = app.make_plan(env)
    assert not p.items                                       # moved files stay out of the plan


# --------------------------------------------------------------------- flow

def test_full_flow_through_the_cli(tmp_path, capsys):
    """M4 done-when: init -> scan -> report -> (export) -> plan -> apply -> verify -> purge -> restore."""
    root = tmp_path / "Pictures"
    build_library(root, LIB)
    st = ["--state", str(tmp_path / "state")]
    run = lambda *a: cli.main([*st, *a])
    assert run("init", "--project-home", str(tmp_path / "home"), "--root", f"pics={root}", "--machine-name", "m") == 0
    assert run("scan", "--no-exiftool", "--quiet") == 0
    assert run("report", "--no-thumbs", "--out", str(tmp_path / "report")) == 0
    # the browser's Export, done by hand: remove the two backup folders
    data = (tmp_path / "report" / "data.js").read_text(encoding="utf-8")
    D = json.loads(data[data.index("{"):data.rindex("}") + 1])
    rid = next(iter(D["roots"]))
    lines = [{"record": "header", "schema": 1, "kind": "decision-export", "library_id": D["libraryId"],
              "report_id": D["reportId"], "scan_id": D["scanId"], "generated_at": "2026-11-12T05:00:00.000000Z"}]
    for i, path in enumerate(["Backup 2019", "Backup 2019/DCIM"]):
        lines.append({"record": "decision", "at": f"2026-11-12T05:00:0{i + 1}.000000Z", "phase": "dedupe",
                      "target": {"kind": "folder", "root_id": rid, "path": path, "scope": "folder"},
                      "action": "remove", "origin": "report", "report_id": D["reportId"]})
    export = tmp_path / f"decisions-{D['reportId']}.jsonl"
    export.write_text("".join(formats.canonical(l) + "\n" for l in lines), encoding="utf-8")
    assert run("plan", "--decisions", str(export)) == 0
    assert run("apply", "--execute", "--batch-limit", "2", "--quiet") == 0      # round 1
    assert run("apply", "--execute", "--quiet") == 0                            # round 2
    assert run("verify", "--deep") == 0
    capsys.readouterr()
    assert run("batches", "list") == 0
    batches = [l.split()[0] for l in capsys.readouterr().out.splitlines()]
    assert len(batches) == 2
    lib_before = set(contents(root))
    assert run("purge", "--batch", batches[0], "--min-age", "0", "--execute") == 0
    assert run("restore", "--batch", batches[1]) == 0
    assert run("scan", "--no-exiftool", "--quiet") == 0
    assert set(contents(root)) == lib_before                 # every content still in the library
    capsys.readouterr()
    assert run("status", "--json") == 0
    out = capsys.readouterr().out
    s = json.loads(out[out.index("{"):])
    assert s["quarantine"]["purged"]["files"] == 2 and s["quarantine"]["restored"]["files"] == 2
