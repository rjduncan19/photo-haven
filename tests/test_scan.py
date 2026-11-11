"""Scanner component tests (docs/10 §2, §5, §6) on generated fixture libraries.

Most use a fake describer so they run without ExifTool; the ones marked
`exiftool` use the real thing.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import unicodedata

import pytest

from photohaven import app, formats
from photohaven.util import atomic_write_text, long_path
from photohaven.scan import ScanOptions
from photohaven.scan import scanner as scanner_mod

from conftest import REPO, join_machine
from fixtures.gen import build_library, copy_of, jpeg_bytes, photo, raw_bytes

BASIC = {
    "Camera Roll/2015/07": ["a.jpg", "b.jpg", "c.jpg"],
    "Backup 2019/DCIM": [copy_of("Camera Roll/2015/07/a.jpg"), copy_of("Camera Roll/2015/07/b.jpg"),
                         photo("unique.jpg", kwargs={"size": (80, 60)})],
    "Import (1)": [copy_of("Camera Roll/2015/07/a.jpg", name="a - Copy.jpg"), raw_bytes("notes.txt", b"hi")],
}


def scan(env, fake, **kw):
    return app.scan(env, ScanOptions(**kw), describer=fake)


def snapshot(root: pathlib.Path) -> dict[str, tuple[str, int]]:
    """Every non-Photo-Haven file: (sha256, mtime_ns)."""
    out = {}
    for p in root.rglob("*"):
        if p.is_file() and not p.name.startswith(".photohaven-"):
            out[p.relative_to(root).as_posix()] = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
    return out


def files(env) -> dict[str, dict]:
    return {r["relpath"]: dict(r) for r in env.conn.execute("SELECT * FROM files")}


def flags(env) -> set[tuple[str, str]]:
    return {(r[0], r[1]) for r in env.conn.execute(
        "SELECT f.relpath, g.flag FROM flags g JOIN files f USING(file_id)")}


def test_first_scan_groups_and_reads_only(lib, fake):
    env, root, _ = lib
    build_library(root, BASIC)
    before = snapshot(root)
    res = scan(env, fake)
    assert snapshot(root) == before                       # photos untouched (rule 3)
    assert res.files_hashed == 7 and res.pending == 0      # the .txt is 'other': recorded, not hashed
    f = files(env)
    assert f["Import (1)/notes.txt"]["sha256"] is None and f["Import (1)/notes.txt"]["media"] == "other"
    groups = env.conn.execute("SELECT instances FROM dup_groups ORDER BY instances").fetchall()
    assert [g[0] for g in groups] == [2, 3]
    st = app.status(env)
    assert st["dup_groups"]["groups"] == 2
    # ExifTool ran once per unique content, not per file
    assert len(fake.calls) == 4
    # only Photo Haven's own files appeared in the root
    extra = {p.name for p in root.rglob(".photohaven-*")}
    assert extra == {".photohaven-root.json", formats.cache_file_name(env.machine.short)}


def test_rescan_hashes_nothing(lib, fake):
    env, root, _ = lib
    build_library(root, BASIC)
    scan(env, fake)
    caches = {p: p.read_bytes() for p in root.rglob(".photohaven-cache.*")}
    res = scan(env, fake)
    assert res.files_hashed == 0 and res.cache_files_written == 0 and res.reused == 7
    assert {p: p.read_bytes() for p in root.rglob(".photohaven-cache.*")} == caches


def test_second_machine_hashes_nothing_and_writes_same_facts(lib, fake):
    env, root, tmp = lib
    build_library(root, BASIC)
    scan(env, fake)
    b = join_machine(tmp, root)
    try:
        res = scan(b, fake)
        assert res.files_hashed == 0 and res.reused == 7
        for d in {p.parent for p in root.rglob(".photohaven-cache.*")}:
            a_file = formats.read_cache(d / formats.cache_file_name(env.machine.short))
            b_file = formats.read_cache(d / formats.cache_file_name(b.machine.short))
            assert formats.facts_equal(a_file.records, b_file.records)
            assert a_file.header.machine_id != b_file.header.machine_id
    finally:
        b.close()


def test_mtime_tolerance(lib, fake):
    env, root, tmp = lib
    made = build_library(root, {"x": ["a.jpg"]})
    scan(env, fake)
    st = made["x/a.jpg"].stat()
    os.utime(made["x/a.jpg"], ns=(st.st_atime_ns, st.st_mtime_ns + 500_000_000))
    assert scan(env, fake).files_hashed == 0              # within the 1 s default
    (tmp / "home" / "library.toml").write_text("schema = 1\n[scan]\nmtime_tolerance_us = 0\n")
    env.settings = type(env.settings).load(tmp / "home")
    os.utime(made["x/a.jpg"], ns=(st.st_atime_ns, st.st_mtime_ns + 900_000_000))
    assert scan(env, fake).files_hashed == 1


def test_cache_conflict_rehashes_and_flags(lib, fake):
    env, root, tmp = lib
    build_library(root, {"x": ["a.jpg"]})
    scan(env, fake)
    b = join_machine(tmp, root)
    try:
        scan(b, fake)
        p = root / "x" / formats.cache_file_name(b.machine.short)
        lines = p.read_text(encoding="utf-8").splitlines()
        rec = json.loads(lines[1])
        rec["sha256"] = "f" * 64
        atomic_write_text(p, lines[0] + "\n" + formats.canonical(rec) + "\n")
    finally:
        b.close()
    res = scan(env, fake)
    assert res.files_hashed == 1
    assert ("x/a.jpg", "cache_conflict") in flags(env)


def test_folder_rename_rehashes_nothing(lib, fake):
    env, root, _ = lib
    build_library(root, BASIC)
    scan(env, fake)
    os.rename(root / "Backup 2019", root / "Backup 2019 renamed")
    res = scan(env, fake)
    assert res.files_hashed == 0
    f = files(env)
    assert f["Backup 2019/DCIM/a.jpg"]["state"] == "missing"
    assert f["Backup 2019 renamed/DCIM/a.jpg"]["state"] == "present"
    gone = env.conn.execute("SELECT present FROM folders WHERE relpath = 'Backup 2019/DCIM'").fetchone()[0]
    assert gone == 0


def test_file_rename_rehashes_only_that_file(lib, fake):
    env, root, _ = lib
    build_library(root, BASIC)
    scan(env, fake)
    os.rename(root / "Camera Roll/2015/07/c.jpg", root / "Camera Roll/2015/07/c2.jpg")
    res = scan(env, fake)
    assert res.files_hashed == 1 and res.missing == 1


def test_changed_file_is_rehashed(lib, fake):
    env, root, _ = lib
    made = build_library(root, {"x": ["a.jpg"]})
    scan(env, fake)
    made["x/a.jpg"].write_bytes(jpeg_bytes(99))
    res = scan(env, fake)
    assert res.files_hashed == 1


def test_placeholders_are_never_opened(lib, fake, monkeypatch):
    env, root, _ = lib
    build_library(root, {"x": ["a.jpg", "cloud.jpg"]})
    real_is_placeholder = scanner_mod.is_placeholder
    monkeypatch.setattr(scanner_mod, "is_placeholder", lambda st: st.st_size == (root / "x/cloud.jpg").stat().st_size
                        or real_is_placeholder(st))
    real_hash = scanner_mod.hash_file

    def guarded(path):
        assert not path.endswith("cloud.jpg"), "opened a cloud-only placeholder"
        return real_hash(path)
    monkeypatch.setattr(scanner_mod, "hash_file", guarded)
    res = scan(env, fake)
    assert res.cloud_only == 1
    f = files(env)["x/cloud.jpg"]
    assert f["state"] == "cloud_only" and f["skip_reason"] == "cloud_only" and f["sha256"] is None
    rec = {r.name: r for r in formats.read_cache(root / "x" / formats.cache_file_name(env.machine.short)).records}
    assert rec["cloud.jpg"].skip == "cloud_only" and rec["cloud.jpg"].sha256 is None


def test_nfc_nfd_names_are_both_kept_and_flagged(lib, fake):
    env, root, _ = lib
    d = root / "x"
    d.mkdir()
    nfc, nfd = unicodedata.normalize("NFC", "Café.jpg"), unicodedata.normalize("NFD", "Café.jpg")
    (d / nfc).write_bytes(jpeg_bytes(1))
    (d / nfd).write_bytes(jpeg_bytes(1))
    if len(os.listdir(d)) == 1:
        pytest.skip("this filesystem normalizes names")
    res = scan(env, fake)
    assert res.files_hashed == 2
    fl = flags(env)
    assert (f"x/{nfc}", "path_key_collision") in fl and (f"x/{nfd}", "path_key_collision") in fl
    assert scan(env, fake).files_hashed == 0             # exact-name cache match, no fallback


def test_candidates_scope_hashes_shared_sizes_only(lib, fake):
    env, root, _ = lib
    build_library(root, BASIC)
    res = scan(env, fake, hash_scope="candidates")
    f = files(env)
    assert f["Backup 2019/DCIM/unique.jpg"]["sha256"] is None
    assert f["Backup 2019/DCIM/a.jpg"]["sha256"] is not None
    assert res.pending >= 1
    assert scan(env, fake, hash_scope="all").files_hashed == res.pending


def test_no_folder_cache_writes_nothing_into_the_root(lib, fake):
    env, root, tmp = lib
    build_library(root, BASIC)
    scan(env, fake, no_folder_cache=True)
    assert not list(root.rglob(".photohaven-cache.*"))
    assert list((tmp / "state-a" / "caches").rglob(".photohaven-cache.*"))
    # a later normal scan copies the facts into the folders without rehashing (04)
    res = scan(env, fake)
    assert res.files_hashed == 0 and res.cache_files_written > 0
    assert list(root.rglob(".photohaven-cache.*"))


def test_audit_finds_silent_corruption(lib, fake):
    env, root, _ = lib
    made = build_library(root, {"x": ["a.jpg"]})
    scan(env, fake)
    p = made["x/a.jpg"]
    st = p.stat()
    data = bytearray(p.read_bytes())
    data[-10] ^= 0xFF
    p.write_bytes(bytes(data))
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))      # same size and mtime: invisible to a normal scan
    assert scan(env, fake).files_hashed == 0
    res = scan(env, fake, audit=5)
    assert res.audit_mismatches == 1
    assert ("x/a.jpg", "cache_conflict") in flags(env)


def test_missing_marker_refuses_root(lib, fake):
    env, root, _ = lib
    build_library(root, {"x": ["a.jpg"]})
    (root / ".photohaven-root.json").unlink()
    res = scan(env, fake)
    assert res.failed_roots == ["pics"] and res.files_seen == 0
    assert scan(env, fake, adopt_root=True).files_seen == 1


def test_wrong_marker_refuses_root(lib, fake, tmp_path):
    env, root, _ = lib
    other = tmp_path / "other"
    other.mkdir()
    shutil.copy(root / ".photohaven-root.json", other / "m.json")
    marker = json.loads((root / ".photohaven-root.json").read_text())
    marker["root_id"] = "00000000-0000-4000-8000-000000000000"
    atomic_write_text(root / ".photohaven-root.json", json.dumps(marker))
    assert scan(env, fake).failed_roots == ["pics"]


def test_rebuild_db_matches_scanned_db(lib, fake):
    env, root, _ = lib
    build_library(root, BASIC)
    scan(env, fake)

    def dump(e):
        fl = sorted(tuple(r) for r in e.conn.execute(
            "SELECT root_id, relpath, sha256, size, mtime_us, state, skip_reason, media FROM files"))
        ct = sorted(tuple(r) for r in e.conn.execute(
            "SELECT sha256, size, media, content_sha256, content_hash_impl, mime, damaged, meta_impl FROM contents"))
        fg = sorted(flags(e))
        fo = sorted(tuple(r) for r in e.conn.execute("SELECT root_id, relpath, depth, is_generic, present FROM folders"))
        return fl, ct, fg, fo
    before = dump(env)
    res = app.rebuild_db(env)
    assert res.files_hashed == 0
    assert dump(env) == before


def test_cache_files_match_the_json_schema(lib, fake):
    jsonschema = pytest.importorskip("jsonschema")
    from referencing import Registry, Resource
    env, root, _ = lib
    build_library(root, BASIC)
    scan(env, fake)
    schemas, res = {}, []
    for p in (REPO / "spec" / "schemas").glob("*.json"):
        s = json.loads(p.read_text(encoding="utf-8"))
        schemas[p.name] = s
        res.append((s["$id"], Resource.from_contents(s)))
    v = jsonschema.Draft202012Validator(schemas["folder-cache.schema.json"], registry=Registry().with_resources(res))
    n = 0
    for p in root.rglob(".photohaven-cache.*.jsonl"):
        raw = p.read_bytes()
        assert b"\r" not in raw and not raw.startswith(b"\xef\xbb\xbf")
        for line in raw.decode().splitlines():
            obj = json.loads(line)
            assert formats.canonical(obj) == line
            assert not list(v.iter_errors(obj)), line
            n += 1
    assert n > 6


@pytest.mark.skipif(os.name != "nt", reason="Windows attributes")
def test_cache_files_hidden_on_windows(lib, fake):
    env, root, _ = lib
    build_library(root, {"x": ["a.jpg"]})
    scan(env, fake)
    scan(env, fake)
    p = root / "x" / formats.cache_file_name(env.machine.short)
    assert os.stat(p).st_file_attributes & 0x2


@pytest.mark.skipif(os.name != "nt", reason="Windows long paths")
def test_long_path(lib, fake):
    env, root, _ = lib
    deep = "/".join(["a folder with a fairly long descriptive name"] * 7)
    made = build_library(root, {deep: ["a.jpg"], "short": [copy_of(f"{deep}/a.jpg")]})
    assert len(str(made[f"{deep}/a.jpg"])) > 260
    res = scan(env, fake)
    assert res.errors == 0 and res.files_hashed == 2
    assert app.status(env)["dup_groups"]["groups"] == 1


# ---------------------------------------------------------------- real ExifTool

@pytest.mark.exiftool
def test_real_exiftool_content_hash_and_damage(lib, exiftool_exe):
    env, root, _ = lib
    env.mcfg.exiftool = exiftool_exe
    made = build_library(root, {"x": [photo("a.jpg", kwargs={"offset": "-07:00", "gps": (47.6, -122.3)})]})
    a = made["x/a.jpg"]
    (root / "x" / "a_rated.jpg").write_bytes(a.read_bytes())
    subprocess.run([exiftool_exe, "-q", "-overwrite_original", "-XMP-xmp:Rating=4", str(root / "x" / "a_rated.jpg")],
                   check=True)
    data = a.read_bytes()
    (root / "x" / "a_trunc.jpg").write_bytes(data[: len(data) // 2])
    res = app.scan(env, ScanOptions())
    assert res.described == 3
    rows = {r["relpath"]: r for r in env.conn.execute(
        "SELECT f.relpath, f.sha256, c.content_sha256, c.damaged, c.taken_local, c.taken_offset, c.taken_utc, "
        "c.gps_lon, c.width FROM files f JOIN contents c USING(sha256)")}
    assert rows["x/a.jpg"]["sha256"] != rows["x/a_rated.jpg"]["sha256"]
    assert rows["x/a.jpg"]["content_sha256"] == rows["x/a_rated.jpg"]["content_sha256"] is not None
    assert rows["x/a_trunc.jpg"]["damaged"] == 1 and rows["x/a_trunc.jpg"]["content_sha256"] is None
    assert rows["x/a.jpg"]["taken_offset"] == "-07:00"
    assert rows["x/a.jpg"]["taken_utc"] == "2015-07-04T20:45:12.000000Z"
    assert rows["x/a.jpg"]["width"] == 64 and abs(rows["x/a.jpg"]["gps_lon"] + 122.3) < 1e-3
    assert ("x/a_trunc.jpg", "damaged") in flags(env)
    assert app.status(env)["content_equivalent_groups"]["groups"] == 1
