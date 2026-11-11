"""Plan and apply (08), with the safety tests of 10 §3 and the crash
injection of 10 §4. Fixtures are synthetic; quarantine is a temp folder."""
from __future__ import annotations

import hashlib
import inspect
import json
import os
import pathlib
import random
import shutil
import subprocess
import sys

import pytest

from photohaven import app, apply as applier, decisions as dec, formats, plan as planner
from photohaven.formats import Decision, Target
from photohaven.scan import ScanOptions
from photohaven.util import is_inside, utc_now

from conftest import REPO, join_machine
from fixtures.gen import build_library, copy_of, photo, raw_bytes

LIB = {
    "Camera Roll": ["a.jpg", "b.jpg", "c.jpg"],
    "Backup 2019": [copy_of("Camera Roll/a.jpg"), copy_of("Camera Roll/b.jpg"), "only-here.jpg"],
    "Backup 2019/DCIM": [copy_of("Camera Roll/c.jpg"), copy_of("Camera Roll/a.jpg", name="a (1).jpg")],
    "Trip": [copy_of("Camera Roll/a.jpg")],
}


def decide(env, path: str, action: str, *, subtree=True, n=1):
    rid = env.library.roots[0].root_id
    d = Decision(f"2026-11-12T05:00:{n:02d}.000000Z", "dedupe",
                 Target("folder", rid, path, scope="subtree" if subtree else "folder"), action, "cli")
    dec.append(env.conn, env.project_home, env.library.library_id, env.machine.machine_id, [d])


def contents(root: pathlib.Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for p in root.rglob("*"):
        if p.is_file() and not p.name.startswith(".photohaven-"):
            out.setdefault(hashlib.sha256(p.read_bytes()).hexdigest(), []).append(p.relative_to(root).as_posix())
    return out


def quarantined(q: pathlib.Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if q.exists():
        for p in q.rglob("*"):
            if p.is_file() and p.name != "manifest.jsonl":
                out.setdefault(hashlib.sha256(p.read_bytes()).hexdigest(), []).append(p.as_posix())
    return out


@pytest.fixture
def ready(lib, fake):
    env, root, tmp = lib
    build_library(root, LIB)
    app.scan(env, ScanOptions(), describer=fake)
    return env, root, tmp, pathlib.Path(env.mcfg.quarantine_root)


def plan_and_apply(env, **kw):
    app.make_plan(env)
    return app.apply_execute(env, rescan=kw.pop("rescan", False), **kw)


# ------------------------------------------------------------------ flows

@pytest.mark.parametrize("force_copy", [False, True], ids=["same-volume", "cross-volume"])
def test_flow_moves_duplicates_and_keeps_keepers(ready, monkeypatch, force_copy):
    env, root, tmp, q = ready
    if force_copy:
        monkeypatch.setenv(applier.FORCE_COPY_ENV, "1")
    before = contents(root)
    decide(env, "Backup 2019", "remove")
    p, _ = app.make_plan(env)
    assert len(p.items) == 4 and [r for _, r in p.blocked] == ["Backup 2019/only-here.jpg"]
    res = app.apply_execute(env, rescan=True)
    assert res.moved == 4 and res.verified == 4 and not res.skipped and not res.verify_failed
    after = contents(root)
    assert set(after) == set(before)                       # every content still in the library (I-2)
    assert "Backup 2019/only-here.jpg" in sum(after.values(), [])
    assert not any(x.startswith("Backup 2019/") and x != "Backup 2019/only-here.jpg" for x in sum(after.values(), []))
    assert sum(len(v) for v in quarantined(q).values()) == 4
    assert not list(q.rglob("*.partial"))
    st = app.status(env)
    assert st["quarantine"]["verified"]["files"] == 4
    # the light rescan rewrote the folder cache without the moved files
    cache = formats.read_cache(root / "Backup 2019" / formats.cache_file_name(env.machine.short))
    assert [r.name for r in cache.records] == ["only-here.jpg"]
    # nothing left to do
    p2, _ = app.make_plan(env)
    assert not p2.items


def test_log_and_manifest_match_the_schemas(ready):
    jsonschema = pytest.importorskip("jsonschema")
    from referencing import Registry, Resource
    env, root, tmp, q = ready
    decide(env, "Backup 2019", "remove")
    plan_and_apply(env)
    schemas, res = {}, []
    for p in (REPO / "spec" / "schemas").glob("*.json"):
        s = json.loads(p.read_text(encoding="utf-8"))
        schemas[p.name] = s
        res.append((s["$id"], Resource.from_contents(s)))
    reg = Registry().with_resources(res)
    for name, files in (("quarantine-log.schema.json", list((env.project_home / "logs").glob("quarantine.*.jsonl"))),
                        ("quarantine-manifest.schema.json", list(q.rglob("manifest.jsonl")))):
        assert files
        v = jsonschema.Draft202012Validator(schemas[name], registry=reg)
        for f in files:
            for line in f.read_text(encoding="utf-8").splitlines():
                obj = json.loads(line)
                assert formats.canonical(obj) == line
                assert not list(v.iter_errors(obj)), line


def test_apply_needs_a_reviewed_unchanged_plan(ready):
    env, root, tmp, q = ready
    decide(env, "Backup 2019", "remove")
    with pytest.raises(applier.ApplyError, match="no saved plan"):
        app.apply_execute(env)
    app.make_plan(env)
    decide(env, "Trip", "remove", n=2)                    # decisions changed after review
    with pytest.raises(applier.ApplyError, match="changed since you last reviewed"):
        app.apply_execute(env)
    assert not quarantined(q)


def test_batch_limit_rounds(ready):
    env, root, tmp, q = ready
    decide(env, "Backup 2019", "remove")
    app.make_plan(env)
    r1 = app.apply_execute(env, batch_limit=1, rescan=False)
    assert (r1.moved, r1.remaining) == (1, 3)
    r2 = app.apply_execute(env, rescan=False)              # the rest of the same reviewed plan
    assert (r2.moved, r2.remaining) == (3, 0)


def test_only_the_apply_machine_applies(ready):
    env, root, tmp, q = ready
    decide(env, "Backup 2019", "remove")
    b = join_machine(tmp, root)
    try:
        b.mcfg.quarantine_root = str(tmp / "qb")
        app.make_plan(b)
        with pytest.raises(applier.ApplyError, match="not the library's apply machine"):
            app.apply_execute(b)
    finally:
        b.close()


# --------------------------------------------------------- safety (10 §3)

def test_I1_no_deletes_inside_roots(ready, monkeypatch):
    """Every delete inside a root must come from the final step of a verified
    cross-volume move."""
    env, root, tmp, q = ready
    monkeypatch.setenv(applier.FORCE_COPY_ENV, "1")
    real = {n: getattr(os, n) for n in ("remove", "unlink", "rmdir")}
    real_rmtree = shutil.rmtree
    allowed = []

    def guard(name):
        def f(path, *a, **k):
            if is_inside(os.fspath(path).removeprefix("\\\\?\\"), root):
                caller = inspect.stack()[1].function
                if caller != "_complete_move":
                    raise AssertionError(f"{name}({path}) inside a root from {caller}")
                allowed.append(path)
            return real[name](path, *a, **k)
        return f
    for n in real:
        monkeypatch.setattr(os, n, guard(n))
    monkeypatch.setattr(shutil, "rmtree", lambda p, *a, **k: (_ for _ in ()).throw(AssertionError(f"rmtree {p}"))
                        if is_inside(p, root) else real_rmtree(p, *a, **k))
    decide(env, "", "remove")                              # remove everything: worst case
    res = plan_and_apply(env, rescan=True)
    assert res.moved and len(allowed) == res.moved
    applier.tidy(env, execute=False)                       # dry run touches nothing


def test_I2_last_copy_when_everything_is_removed(ready):
    env, root, tmp, q = ready
    before = contents(root)
    decide(env, "", "remove")
    p, _ = app.make_plan(env)
    assert len(p.conflicts) == 3                           # a, b, c: every copy marked remove
    plan_and_apply(env)
    after = contents(root)
    assert set(after) == set(before)
    assert all(len(v) == 1 for v in after.values())


@pytest.mark.parametrize("seed", range(6))
def test_I2_property_random_decisions(lib, fake, seed):
    rnd = random.Random(seed)
    env, root, tmp = lib
    folders = ["A", "B", "C", "A/x", "B/y", "Backup", "Backup/z"]
    made: list[pathlib.Path] = []
    for i in range(14):                                    # created in order, so copies follow their sources
        d = root.joinpath(*rnd.choice(folders).split("/"))
        d.mkdir(parents=True, exist_ok=True)
        if made and rnd.random() < 0.6:
            dst = d / f"c{i}.jpg"
            shutil.copy2(rnd.choice(made), dst)
        else:
            dst = d / f"p{i}.jpg"
            build_library(d, {"": [photo(dst.name, index=i)]})
        made.append(dst)
    app.scan(env, ScanOptions(), describer=fake)
    before = contents(root)
    for n, f in enumerate(rnd.sample(folders + [""], 4), 1):
        decide(env, f, rnd.choice(["keep", "remove", "remove", "undecided"]), subtree=rnd.random() < 0.5, n=n)
    plan_and_apply(env)
    assert set(contents(root)) == set(before)              # every content still in the library


def test_I3_keeper_listed_in_another_machines_quarantine(ready):
    env, root, tmp, q = ready
    decide(env, "Trip", "remove")                          # Trip/a.jpg would keep Camera Roll/a.jpg (or others)
    rid = env.library.roots[0].root_id
    sha_a = env.conn.execute("SELECT sha256 FROM files WHERE relpath = 'Trip/a.jpg'").fetchone()[0]
    others = [r[0] for r in env.conn.execute("SELECT relpath FROM files WHERE sha256 = ? AND relpath != 'Trip/a.jpg'",
                                             (sha_a,))]
    # machine B's log says every other copy of a.jpg was moved out (sync lag, 06 §5)
    jb = applier.quarantine.Journal(env.project_home, env.library.library_id, "7a6b5c4d-3e2f-4a1b-9c8d-7e6f5a4b3c2d")
    for path in others:
        jb.append(jb.new_event(batch_id="20261112-051500-a91f", event="moved", root_id=rid, path=path, sha256=sha_a,
                               size=1, keeper_root_id=rid, keeper_path="Trip/a.jpg",
                               qpath=f"20261112-051500-a91f/files/{rid}/{path}"))
    p, _ = app.make_plan(env)
    assert not any(i.sha256 == sha_a for i in p.items)      # no copy of a.jpg moves...
    assert "Trip/a.jpg" in [r for _, _, r in p.conflicts]   # ...and the only eligible copy is kept


def test_I4_changed_source_or_keeper_is_skipped(ready, monkeypatch):
    env, root, tmp, q = ready
    decide(env, "Backup 2019", "remove")
    app.make_plan(env)
    src = root / "Backup 2019" / "a.jpg"
    with open(src, "ab") as f:
        f.write(b"edited")                                 # size changes
    keeper_of_b = env.conn.execute(
        "SELECT f.relpath FROM plan_items p JOIN files f ON f.file_id = p.keeper_file_id JOIN files s "
        "ON s.file_id = p.file_id WHERE s.relpath = 'Backup 2019/b.jpg'").fetchone()[0]
    k = root / keeper_of_b
    st = k.stat()
    os.utime(k, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    res = app.apply_execute(env, rescan=False)
    reasons = dict(res.skipped)
    assert "changed since the plan" in reasons["Backup 2019/a.jpg"]
    assert "keeper" in reasons["Backup 2019/b.jpg"]
    assert src.exists() and (root / "Backup 2019" / "b.jpg").exists()


@pytest.mark.parametrize("force_copy", [False, True], ids=["same-volume", "cross-volume"])
def test_I4_same_size_same_mtime_edit_is_caught(ready, monkeypatch, force_copy):
    env, root, tmp, q = ready
    if force_copy:
        monkeypatch.setenv(applier.FORCE_COPY_ENV, "1")
    decide(env, "Trip", "remove")
    app.make_plan(env)
    src = root / "Trip" / "a.jpg"
    st = src.stat()
    data = bytearray(src.read_bytes())
    data[-20] ^= 0xFF
    src.write_bytes(bytes(data))
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))
    res = app.apply_execute(env, rescan=False)
    assert res.moved == 0 and res.skipped
    assert src.read_bytes() == bytes(data)                 # still in place, untouched


# ------------------------------------------------------ crash injection (10 §4)

POINTS = [("after_moving_commit", False), ("after_rename", False), ("before_journal", False),
          ("after_moving_commit", True), ("after_partial_write", True), ("after_rename_partial", True),
          ("after_source_delete", True), ("before_journal", True)]


@pytest.mark.parametrize("point,force_copy", POINTS, ids=[f"{p}-{'copy' if c else 'rename'}" for p, c in POINTS])
def test_crash_injection(ready, point, force_copy):
    env, root, tmp, q = ready
    before = contents(root)
    decide(env, "Backup 2019", "remove")
    app.make_plan(env)
    state = env.state_dir
    env.close()                                            # release the process lock for the subprocess
    child_env = {**os.environ, applier.CRASH_ENV: point, "PYTHONIOENCODING": "utf-8"}
    if force_copy:
        child_env[applier.FORCE_COPY_ENV] = "1"
    r = subprocess.run([sys.executable, "-m", "photohaven.cli", "--state", str(state), "apply", "--execute",
                        "--quiet", "--no-rescan"], env=child_env, capture_output=True, text=True)
    assert r.returncode == 77, r.stderr
    env2 = app.open_env(state)
    try:
        if force_copy:
            os.environ[applier.FORCE_COPY_ENV] = "1"
        res = app.apply_execute(env2, rescan=False)        # recovers, then finishes the plan
        assert res.recovered
        lib_now, q_now = contents(root), quarantined(q)
        assert set(before) <= set(lib_now) | set(q_now)  # every content in the library or quarantine
        assert set(lib_now) == set(before)                # and still in the library (I-2)
        assert not list(q.rglob("*.partial"))
        moved = {(r[0], r[1]) for r in env2.conn.execute(
            "SELECT batch_id, relpath FROM quarantine_items WHERE state IN ('moved', 'verified')")}
        man = {(i.batch_id, i.path) for m in q.rglob("manifest.jsonl") for i in applier.quarantine.manifest_items(m)}
        log = {(e.batch_id, e.path) for e in applier.quarantine.Journal(
            env2.project_home, env2.library.library_id, env2.machine.machine_id).events if e.event == "moved"}
        assert moved == man == log and len(moved) == 4
        p, _ = app.make_plan(env2)
        assert not p.items                                 # a clean rerun changes nothing
    finally:
        os.environ.pop(applier.FORCE_COPY_ENV, None)
        env2.close()


def test_recovery_impossible_row_stops_loudly(ready):
    env, root, tmp, q = ready
    decide(env, "Trip", "remove")
    app.make_plan(env)
    env.conn.execute("INSERT INTO batches(batch_id, plan_id, machine_id, quarantine_root, created_at, state) "
                     "VALUES('20261112-000000-dead', NULL, ?, ?, ?, 'applying')",
                     (env.machine.machine_id, str(q), utc_now()))
    rid = env.library.roots[0].root_id
    env.conn.execute("INSERT INTO quarantine_items(batch_id, root_id, relpath, sha256, size, keeper_root_id, "
                     "keeper_relpath, qpath, state) VALUES('20261112-000000-dead', ?, 'Trip/gone.jpg', ?, 1, ?, "
                     "'Camera Roll/a.jpg', ?, 'moving')",
                     (rid, "0" * 64, rid, f"20261112-000000-dead/files/{rid}/Trip/gone.jpg"))
    with pytest.raises(applier.RecoveryError, match="should be impossible"):
        app.apply_execute(env)


# ------------------------------------------------------------------- tidy

def test_tidy_empty_folders(ready):
    env, root, tmp, q = ready
    decide(env, "Backup 2019/DCIM", "remove")
    plan_and_apply(env, rescan=True)
    would = applier.tidy(env, execute=False)
    assert [pathlib.Path(d.removeprefix("\\\\?\\")).name for d in would] == ["DCIM"]
    assert (root / "Backup 2019" / "DCIM").exists()
    applier.tidy(env, execute=True)
    assert not (root / "Backup 2019" / "DCIM").exists()
    assert (root / "Backup 2019" / "only-here.jpg").exists()


def test_os_error_mid_move_skips_that_file(ready, monkeypatch):
    env, root, tmp, q = ready
    decide(env, "Backup 2019", "remove")
    app.make_plan(env)
    real = os.rename

    def flaky(a, b, *k):
        if str(a).endswith("b.jpg"):
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return real(a, b, *k)
    monkeypatch.setattr(os, "rename", flaky)
    res = app.apply_execute(env, rescan=False)
    assert res.moved == 3 and [p for p, _ in res.skipped] == ["Backup 2019/b.jpg"]
    assert (root / "Backup 2019" / "b.jpg").exists()
    assert not env.conn.execute("SELECT count(*) FROM quarantine_items WHERE state = 'moving'").fetchone()[0]
