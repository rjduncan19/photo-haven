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
