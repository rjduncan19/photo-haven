"""What a scan does to a library, proven rather than promised.

Before the first run over the real library: a read-only setup must write
nothing at all inside a root, a normal scan must write only its own cache
files, and ExifTool must only ever be asked to read.
"""
from __future__ import annotations

import builtins
import io
import os
import pathlib
import shutil
import stat

import pytest

from photohaven import app, apply as applier, formats, util
from photohaven.scan import ScanOptions
from photohaven.scan import exiftool as et_mod
from photohaven.util import is_inside

from fixtures.gen import build_library, copy_of, raw_bytes

LIB = {
    "Camera Roll": ["a.jpg", "b.jpg"],
    "Backup 2019": [copy_of("Camera Roll/a.jpg"), copy_of("Camera Roll/b.jpg"), raw_bytes("notes.txt", b"x")],
    "Backup 2019/Deep": [copy_of("Camera Roll/a.jpg", name="a (1).jpg")],
    "Empty": [],
}


def tree(root: pathlib.Path) -> dict[str, tuple]:
    """Every entry under root, with everything a write could change."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for n in dirnames + filenames:
            p = os.path.join(dirpath, n)
            st = os.stat(p)
            data = None
            if stat.S_ISREG(st.st_mode):
                with open(p, "rb") as f:
                    data = f.read()
            out[os.path.relpath(p, root)] = (stat.S_ISDIR(st.st_mode), st.st_size, st.st_mtime_ns,
                                             getattr(st, "st_file_attributes", st.st_mode), data)
    return out


class WriteGuard:
    """Fails the test on any write inside `root`, except to paths `allow` accepts."""

    def __init__(self, monkeypatch, root: pathlib.Path, allow=lambda p: False):
        self.root, self.allow, self.hits = root, allow, []
        real_open, real_os_open = builtins.open, os.open

        def inside(p) -> bool:
            if isinstance(p, int):
                return False
            s = os.fspath(p)
            s = s.decode() if isinstance(s, bytes) else s
            if s.startswith("\\\\?\\UNC\\"):
                s = "\\\\" + s[8:]
            s = s.removeprefix("\\\\?\\")
            return is_inside(s, root) and not self.allow(s)

        def check(op, p):
            if inside(p):
                self.hits.append((op, os.fspath(p)))
                raise AssertionError(f"{op} inside the library: {p}")

        def g_open(file, mode="r", *a, **k):
            if any(c in mode for c in "wax+"):
                check(f"open({mode})", file)
            return real_open(file, mode, *a, **k)

        def g_os_open(path, flags, *a, **k):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                check("os.open(write)", path)
            return real_os_open(path, flags, *a, **k)

        monkeypatch.setattr(builtins, "open", g_open)
        monkeypatch.setattr(io, "open", g_open)
        monkeypatch.setattr(os, "open", g_os_open)
        for name in ("remove", "unlink", "rmdir", "mkdir", "makedirs", "utime", "chmod"):
            real = getattr(os, name)
            monkeypatch.setattr(os, name, (lambda real, name: lambda p, *a, **k: (check(name, p), real(p, *a, **k))[1])(real, name))
        for name in ("rename", "replace"):
            real = getattr(os, name)
            monkeypatch.setattr(os, name, (lambda real, name: lambda s, d, *a, **k: (check(name, s), check(name, d),
                                                                                    real(s, d, *a, **k))[2])(real, name))
        real_hidden = util.set_hidden
        monkeypatch.setattr(util, "set_hidden", lambda p: (check("set_hidden", p), real_hidden(p))[1])
        import sqlite3
        import subprocess
        real_connect = sqlite3.connect
        monkeypatch.setattr(sqlite3, "connect", lambda db, *a, **k: (check("sqlite3.connect", db) if db != ":memory:"
                                                                     else None, real_connect(db, *a, **k))[1])
        real_popen = subprocess.Popen.__init__

        def popen(this, args, *a, **k):
            if k.get("cwd"):
                check("subprocess cwd", k["cwd"])        # a child process started inside a root
            return real_popen(this, args, *a, **k)
        monkeypatch.setattr(subprocess.Popen, "__init__", popen)


def _session(env, describer, *, decisions=True):
    """Everything a reviewer does before deciding to apply."""
    app.scan(env, ScanOptions(), describer=describer)
    app.scan(env, ScanOptions(audit=2), describer=describer)
    app.make_report(env, out=env.state_dir / "report", thumbs=describer is not None and hasattr(describer, "embedded_preview"))
    if decisions:
        from photohaven import decisions as dec
        from photohaven.formats import Decision, Target
        rid = env.library.roots[0].root_id
        dec.append(env.conn, env.project_home, env.library.library_id, env.machine.machine_id,
                   [Decision("2026-11-12T05:00:00.000000Z", "dedupe", Target("folder", rid, "Backup 2019", scope="subtree"),
                             "remove", "cli")])
        p, _ = app.make_plan(env)
        assert p.items
    app.status(env)
    app.rebuild_db(env)


def test_read_only_setup_writes_nothing_in_the_root(tmp_path, fake, monkeypatch):
    root = tmp_path / "Pictures"
    build_library(root, LIB)
    before = tree(root)
    guard = WriteGuard(monkeypatch, root)
    env, notes = app.init(tmp_path / "state", tmp_path / "home", [("pics", root)], machine_name="ro", read_only=True)
    try:
        assert any("read-only" in n for n in notes)
        _session(env, fake)
        app.make_plan(env)                                  # rebuild-db cleared the saved plan
        with pytest.raises(applier.ApplyError, match="root marker"):
            app.apply_execute(env)                          # apply refuses without the marker
        assert app.status(env)["dup_groups"]["groups"] == 2
        assert list((tmp_path / "state" / "caches").rglob(".photohaven-cache.*"))
    finally:
        env.close()
    assert not guard.hits
    assert tree(root) == before                             # not one byte, name, time or attribute changed


def test_default_scan_writes_only_its_own_cache_files(lib, fake, monkeypatch):
    env, root, tmp = lib
    build_library(root, LIB)
    before = tree(root)
    own = formats.cache_file_name(env.machine.short)
    allowed = lambda p: os.path.basename(p) in (own, own + ".tmp")
    guard = WriteGuard(monkeypatch, root, allowed)
    _session(env, fake)
    assert not guard.hits
    after = tree(root)
    new = set(after) - set(before)
    assert new and all(os.path.basename(p) == own for p in new)
    assert {k: v for k, v in after.items() if k in before and not after[k][0]} == \
        {k: v for k, v in before.items() if not v[0]}       # every existing file untouched


@pytest.mark.exiftool
def test_exiftool_is_only_asked_to_read(tmp_path, exiftool_exe, monkeypatch):
    sent: list[list[str]] = []
    real = et_mod.ExifTool._run_bytes
    monkeypatch.setattr(et_mod.ExifTool, "_run_bytes", lambda self, args: (sent.append(list(args)), real(self, args))[1])
    root = tmp_path / "Pictures"
    build_library(root, LIB)
    before = tree(root)
    env, _ = app.init(tmp_path / "state", tmp_path / "home", [("pics", root)], machine_name="ro", read_only=True,
                      exiftool=exiftool_exe)
    try:
        app.scan(env, ScanOptions())
        app.make_report(env, out=tmp_path / "report", thumbs=True)
    finally:
        env.close()
    assert sent
    write_opts = {"-overwrite_original", "-overwrite_original_in_place", "-o", "-out", "-tagsfromfile",
                  "-delete_original", "-delete_original!", "-geotag", "-w", "-w!", "-w+", "-W", "-W!", "-W+",
                  "-srcfile", "-csv=", "-json="}
    for args in sent:
        for i, a in enumerate(args):
            assert a.lower() not in write_opts, (a, args)
            if a.startswith("-") and "=" in a:              # a tag assignment, unless it's an -api option
                assert i > 0 and args[i - 1] == "-api", (a, args)
    assert tree(root) == before


@pytest.mark.exiftool
@pytest.mark.parametrize("fmt", ["png", "heic"])
def test_content_hash_and_damage_for_png_and_heic(lib, exiftool_exe, fmt):
    """The M0 spike-A result, kept in CI: ImageDataHash survives a metadata
    write, and a truncated copy is flagged damaged with no content hash."""
    from PIL import Image
    if fmt == "heic":
        pillow_heif = pytest.importorskip("pillow_heif")
        pillow_heif.register_heif_opener()
    env, root, tmp = lib
    env.mcfg.exiftool = exiftool_exe
    d = root / "x"
    d.mkdir()
    img = Image.new("RGB", (64, 48), (200, 40, 90))
    for x in range(64):
        img.putpixel((x, x % 48), (x * 4, 0, 255 - x))
    buf = io.BytesIO()
    img.save(buf, "PNG" if fmt == "png" else "HEIF")
    data = buf.getvalue()
    (d / f"a.{fmt}").write_bytes(data)
    (d / f"a_rated.{fmt}").write_bytes(data)
    import subprocess
    subprocess.run([exiftool_exe, "-q", "-overwrite_original", "-XMP-xmp:Rating=5", str(d / f"a_rated.{fmt}")], check=True)
    (d / f"a_trunc.{fmt}").write_bytes(data[: len(data) * 2 // 3])
    app.scan(env, ScanOptions())
    rows = {r[0]: r for r in env.conn.execute(
        "SELECT f.name, f.sha256, c.content_sha256, c.damaged FROM files f JOIN contents c USING(sha256)")}
    a, rated, trunc = rows[f"a.{fmt}"], rows[f"a_rated.{fmt}"], rows[f"a_trunc.{fmt}"]
    assert a[1] != rated[1] and a[2] and a[2] == rated[2]
    assert trunc[3] == 1 and trunc[2] is None


def test_larger_library_rescan_and_rebuild(lib, fake):
    """A few thousand files in a few hundred folders: rescan hashes nothing,
    rebuild reproduces the DB, and the report builds."""
    env, root, tmp = lib
    spec: dict[str, list] = {}
    for i in range(150):
        spec[f"{2000 + i // 12}/{i:03d} Event"] = [f"p{i}_{j}.jpg" for j in range(10)]
    for i in range(0, 150, 5):
        src = f"{2000 + i // 12}/{i:03d} Event"
        spec[f"Backup/{i:03d}"] = [copy_of(f"{src}/p{i}_{j}.jpg") for j in range(10)]
    build_library(root, spec)
    res = app.scan(env, ScanOptions(), describer=fake)
    assert res.files_seen == 1800 and res.errors == 0
    assert app.scan(env, ScanOptions(), describer=fake).files_hashed == 0
    dump = lambda: sorted(tuple(r) for r in env.conn.execute("SELECT relpath, sha256, state FROM files"))
    before = dump()
    app.rebuild_db(env)
    assert dump() == before
    rep = app.make_report(env, out=tmp / "rep", thumbs=False)
    assert rep.sections["contained"] == 30


@pytest.mark.skipif(os.name != "nt", reason="case-insensitive NTFS")
def test_case_only_folder_rename(lib, fake):
    env, root, tmp = lib
    build_library(root, {"Trip": ["a.jpg"], "Backup": [copy_of("Trip/a.jpg")]})
    app.scan(env, ScanOptions(), describer=fake)
    os.rename(root / "Backup", root / "BACKUP")
    res = app.scan(env, ScanOptions(), describer=fake)
    assert res.files_hashed == 0
    states = dict(env.conn.execute("SELECT relpath, state FROM files").fetchall())
    assert states["BACKUP/a.jpg"] == "present" and states["Backup/a.jpg"] == "missing"
    assert app.status(env)["dup_groups"]["groups"] == 1


# ------------------------------------------------- after the independent audit

@pytest.mark.exiftool
def test_whole_read_only_session_through_the_cli(tmp_path, exiftool_exe, monkeypatch, capsys):
    """Every command a reviewer runs, through the real CLI and real ExifTool
    (thumbnails included), under the write guard. Only apply is expected to
    refuse."""
    from photohaven import cli
    import subprocess
    from fixtures.gen import jpeg_bytes
    root = tmp_path / "Pictures"
    build_library(root, LIB)
    (tmp_path / "thumb.jpg").write_bytes(jpeg_bytes(99, size=(32, 24)))
    subprocess.run([exiftool_exe, "-q", "-overwrite_original", f"-ThumbnailImage<={tmp_path / 'thumb.jpg'}",
                    str(root / "Camera Roll" / "b.jpg")], check=True)   # a JPEG with an embedded preview
    before = tree(root)
    sent: list[str] = []
    real_payload = et_mod.ExifTool._payload
    monkeypatch.setattr(et_mod.ExifTool, "_payload",
                        staticmethod(lambda args, n: (lambda s: (sent.append(s), s)[1])(real_payload(args, n))))
    guard = WriteGuard(monkeypatch, root)
    st = ["--state", str(tmp_path / "state")]
    run = lambda *a: cli.main([*st, *a])
    assert run("init", "--read-only", "--project-home", str(tmp_path / "home"), "--root", f"pics={root}",
               "--machine-name", "ro", "--exiftool", exiftool_exe) == 0
    assert run("scan", "--quiet") == 0
    assert run("scan", "--quiet", "--rehash") == 0
    assert run("scan", "--quiet", "--hash-scope", "all", "--audit", "3") == 0
    assert run("report") == 0
    assert run("decisions", "add", "remove", "pics:Backup 2019", "--subtree") == 0
    assert run("decisions", "list") == 0
    assert run("decisions", "show-effective", "pics:Backup 2019/a.jpg") == 0
    assert run("plan") == 0
    assert run("apply") == 0                                # without --execute it is the plan again
    assert run("apply", "--execute") == 2                   # refused: no root markers in read-only mode
    assert run("verify") == 0
    assert run("batches", "list") == 0
    assert run("status") == 0
    run("doctor")
    assert run("rebuild-db", "--quiet") == 0
    assert not guard.hits
    assert tree(root) == before
    thumbs = list((tmp_path / "state" / "thumbs").rglob("*.jpg"))
    assert thumbs                                           # the thumbnail path really ran
    assert sent
    for payload in sent:                                    # what actually reached ExifTool
        lines = payload.split("\n")
        for i, a in enumerate(lines):
            assert a.lower() not in ("-overwrite_original", "-overwrite_original_in_place", "-o", "-out",
                                     "-tagsfromfile", "-w", "-w!", "-w+", "-geotag", "-srcfile"), payload
            if a.startswith("-") and "=" in a:
                assert lines[i - 1] == "-api", payload


def test_tampered_cache_record_cannot_steer_writes(lib, fake, tmp_path):
    """A cache file planted in a root (another machine, a sync accident, an
    attacker) with a path-like 'sha256' is ignored; nothing is written
    outside local state."""
    env, root, tmp = lib
    build_library(root, {"x": ["a.jpg"]})
    f = root / "x" / "a.jpg"
    st = f.stat()
    evil = {"record": "file", "name": "a.jpg", "size": st.st_size, "mtime_us": st.st_mtime_ns // 1000,
            "media": "image", "observed_at": "2026-11-11T00:00:00.000000Z", "meta_impl": "exiftool/1;ph-meta/1",
            "meta": {}, "sha256": "..\\..\\..\\..\\" + "a" * 52}
    header = {"record": "header", "schema": 1, "kind": "folder-cache", "library_id": env.library.library_id,
              "root_id": env.library.roots[0].root_id, "folder": "x",
              "machine_id": "7a6b5c4d-3e2f-4a1b-9c8d-7e6f5a4b3c2d", "written_at": "2026-11-11T00:00:00.000000Z",
              "tool": "photohaven/0.1.0"}
    (root / "x" / ".photohaven-cache.7a6b5c4d3e2f.jsonl").write_text(
        formats.canonical(header) + "\n" + formats.canonical(evil) + "\n", encoding="utf-8")
    res = app.scan(env, ScanOptions(), describer=fake)
    assert res.files_hashed == 1 and any("bad sha256" in w for w in res.warnings)
    sha = env.conn.execute("SELECT sha256 FROM files WHERE relpath = 'x/a.jpg'").fetchone()[0]
    assert formats.is_sha256(sha)
    before = sorted(p for p in tmp_path.rglob("*") if "state-a" not in p.parts)
    app.make_report(env, out=tmp / "rep", thumbs=False)
    after = sorted(p for p in tmp_path.rglob("*") if "state-a" not in p.parts and "rep" not in p.parts)
    assert after == [p for p in before if "rep" not in p.parts]


@pytest.mark.skipif(os.name == "nt", reason="NTFS forbids line breaks in names")
@pytest.mark.exiftool
def test_filename_with_line_breaks_never_reaches_exiftool(lib, exiftool_exe):
    env, root, tmp = lib
    env.mcfg.exiftool = exiftool_exe
    build_library(root, {"x": ["a.jpg"]})
    evil = root / "x" / "b\n-overwrite_original\n-Comment=pwned\n.jpg"
    from fixtures.gen import jpeg_bytes
    evil.write_bytes(jpeg_bytes(77))              # its own content, so ExifTool would have to be asked about it
    data_a, data_e = (root / "x" / "a.jpg").read_bytes(), evil.read_bytes()
    app.scan(env, ScanOptions())
    assert (root / "x" / "a.jpg").read_bytes() == data_a and evil.read_bytes() == data_e
    flags = {r[0] for r in env.conn.execute("SELECT g.flag FROM flags g JOIN files f USING(file_id) "
                                            "WHERE f.name LIKE 'b%'")}
    assert "exiftool_error" in flags
    assert not et_mod.safe_arg_path("C:/x/a\nb.jpg") and not et_mod.safe_arg_path(" /x/a.jpg")


def test_state_dir_or_report_inside_a_root_is_refused(tmp_path, fake):
    root = tmp_path / "Pictures"
    build_library(root, {"x": ["a.jpg"]})
    with pytest.raises(app.ConfigError, match="overlaps"):
        app.init(root / "state", tmp_path / "home", [("pics", root)], read_only=True)
    env, _ = app.init(tmp_path / "state", tmp_path / "home", [("pics", root)], read_only=True)
    try:
        app.scan(env, ScanOptions(), describer=fake)
        with pytest.raises(ValueError, match="inside the library root"):
            app.make_report(env, out=root / "report", thumbs=False)
        assert not (root / "report").exists()
        extra = tmp_path / "More"
        extra.mkdir()
        app.root_add(env, "more", extra)
        assert not (extra / formats.ROOT_MARKER).exists()   # read-only: no marker from root add either
    finally:
        env.close()


def test_files_that_go_cloud_only_mid_session_are_not_read(lib, fake, monkeypatch, tmp_path):
    from photohaven import report as report_mod
    from photohaven.scan import scanner as scanner_mod
    env, root, tmp = lib
    build_library(root, {"x": ["a.jpg"]})
    # between the walk and the read, OneDrive dehydrates the file
    monkeypatch.setattr(scanner_mod, "is_placeholder", lambda st: True)
    h = scanner_mod.hash_file(str(root / "x" / "a.jpg"))
    assert h.error == "cloud_only" and h.sha256 is None
    monkeypatch.undo()
    app.scan(env, ScanOptions(), describer=fake)
    # between the scan and the report, too: no thumbnail is extracted
    calls = []

    class Spy:
        def embedded_preview(self, p):
            calls.append(p)
            return None
    monkeypatch.setattr(report_mod, "is_placeholder", lambda st: True)
    from photohaven import report
    report.build(env.conn, env.library, state_dir=env.state_dir, project_home=env.project_home, machine_name="m",
                 out=tmp / "rep", exiftool=Spy())
    assert calls == []
