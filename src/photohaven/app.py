"""Wiring: open this machine's environment, init/join a library, roots,
status, doctor, rebuild-db. The CLI is a thin layer over these functions,
and tests call them directly.
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import platform
import re
import sqlite3

from . import apply as applier, db, decisions, link, plan as planner, report
from .config import (ConfigError, Library, LibrarySettings, MachineConfig, RootInfo, default_state_dir,
                     LIBRARY_JSON, LIBRARY_TOML)
from .formats import ROOT_MARKER, FormatError, RootMarker, is_cache_file_name
from .identity import (Machine, ProcessLock, create_machine, load_machine, read_registry, write_registry)
from .scan.exiftool import MIN_VERSION, ExifTool, ExifToolError, find_exiftool, parse_version
from .scan.scanner import ScanOptions, ScanResult, Scanner
from .util import IS_WINDOWS, atomic_write_bytes, is_inside, long_path, name_key, utc_now, uuid4

DB_NAME = "photohaven.db"


@dataclasses.dataclass
class Env:
    state_dir: pathlib.Path
    mcfg: MachineConfig
    machine: Machine
    project_home: pathlib.Path
    library: Library
    settings: LibrarySettings
    conn: sqlite3.Connection
    lock: ProcessLock | None = None

    def close(self) -> None:
        self.conn.close()
        if self.lock:
            self.lock.release()


def open_env(state_dir: pathlib.Path | None = None, *, lock: bool = True) -> Env:
    state_dir = pathlib.Path(state_dir or default_state_dir())
    lk = ProcessLock(state_dir).acquire() if lock else None
    try:
        mcfg = MachineConfig.load(state_dir)
        machine = load_machine(state_dir)
        if machine is None:
            raise ConfigError(f"{state_dir / 'machine.json'} is missing: run 'photohaven init' "
                              "(or 'photohaven machine reset')")
        machine.name = mcfg.name or machine.name
        home = pathlib.Path(mcfg.project_home)
        for rid, p in mcfg.roots.items():
            if is_inside(state_dir, p) or is_inside(p, state_dir):
                raise ConfigError(f"the local state folder {state_dir} overlaps the library root {p}; it must be "
                                  "outside every root (set --state or PHOTOHAVEN_STATE)")
        library = Library.load(home)
        settings = LibrarySettings.load(home)
        conn = db.connect(state_dir / DB_NAME)
        lib_in_db = db.get_meta(conn, "library_id")
        if lib_in_db is None:
            db.set_meta(conn, "library_id", library.library_id)
        elif lib_in_db != library.library_id:
            conn.close()
            raise ConfigError(f"{state_dir / DB_NAME} belongs to library {lib_in_db}, not "
                              f"{library.library_id}. Move it aside or run 'photohaven rebuild-db'.")
        write_registry(home, machine)
    except BaseException:
        if lk:
            lk.release()
        raise
    return Env(state_dir, mcfg, machine, home, library, settings, conn, lk)


def _check_placement(home: pathlib.Path, roots: list[tuple[str, pathlib.Path]], quarantine: str | None) -> None:
    for name, p in roots:
        if is_inside(home, p):
            raise ConfigError(f"the project home {home} is inside root {name} ({p}); put it outside every root")
        if quarantine and (is_inside(quarantine, p) or is_inside(p, quarantine)):
            raise ConfigError(f"quarantine_root {quarantine} overlaps root {name} ({p}); leave it unset to use "
                              f"each root's PhotoHaven-DELETE folder")
        if is_inside(p, home):
            raise ConfigError(f"root {name} ({p}) is inside the project home {home}")


def _root_kind(path: pathlib.Path) -> str:
    od = [os.environ.get(k) for k in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")]
    return "onedrive" if any(o and is_inside(path, o) for o in od) or "onedrive" in str(path).lower() else "local"


def init(state_dir: pathlib.Path, project_home: pathlib.Path, roots: list[tuple[str, pathlib.Path]], *,
         quarantine: str | None = None, library_name: str = "", machine_name: str | None = None,
         exiftool: str | None = None, read_only: bool = False) -> tuple[Env, list[str]]:
    """Set up this machine; create the library if the project home has none,
    otherwise join it and map the named roots. Returns (env, notes).

    `read_only` writes nothing into the roots: no root markers, and folder
    caches under local state (cache_placement = "local_only"). Scans and
    reports work; apply refuses until the roots are marked."""
    state_dir = pathlib.Path(state_dir)
    notes: list[str] = []
    if (state_dir / "machine.toml").exists():
        raise ConfigError(f"this machine is already set up ({state_dir / 'machine.toml'})")
    project_home = pathlib.Path(project_home).absolute()
    roots = [(n, pathlib.Path(p).absolute()) for n, p in roots]
    for n, p in roots:
        if not p.is_dir():
            raise ConfigError(f"root {n}: {p} is not a folder")
    _check_placement(project_home, roots, quarantine)
    for n, p in roots:
        if is_inside(state_dir, p) or is_inside(p, state_dir):
            raise ConfigError(f"the local state folder {state_dir} overlaps root {n} ({p})")
    state_dir.mkdir(parents=True, exist_ok=True)
    project_home.mkdir(parents=True, exist_ok=True)
    joining = (project_home / LIBRARY_JSON).exists()
    registry = read_registry(project_home)
    machine = load_machine(state_dir) or create_machine(
        state_dir, machine_name or platform.node().lower(), {r.get("machine_short", "") for r in registry.values()})
    if machine_name:
        machine.name = machine_name
    mapping: dict[str, str] = {}
    if joining:
        library = Library.load(project_home)
        for n, p in roots:
            r = library.root(n)
            if not (read_only and not (p / ROOT_MARKER).exists()):
                _verify_marker(library, r, p)
            mapping[r.root_id] = str(p)
        notes.append(f"joined library {library.name or library.library_id}")
    else:
        library = Library(uuid4(), library_name, utc_now(), machine.machine_id, [])
        for n, p in roots:
            r = RootInfo(uuid4(), n, _root_kind(p))
            if not read_only:
                _write_marker(library, r, p)
            library.roots.append(r)
            mapping[r.root_id] = str(p)
        library.save(project_home)
        if not (project_home / LIBRARY_TOML).exists():
            LibrarySettings.write_default(project_home)
        notes.append(f"created library {library.library_id} with {len(roots)} root(s); "
                     f"this machine is the apply machine")
    exe = find_exiftool(exiftool)
    if not exe:
        notes.append("ExifTool not found: pass --exiftool PATH or set exiftool in machine.toml")
    MachineConfig(machine.name, str(project_home), quarantine, exe, roots=mapping,
                  cache_placement="local_only" if read_only else "in_folder").save(state_dir)
    if read_only:
        notes.append("read-only: nothing will be written into the roots (no root markers; folder caches stay in "
                     "local state). Apply needs root markers: 'photohaven scan --adopt-root' writes them later.")
    (project_home / "logs").mkdir(exist_ok=True)
    env = open_env(state_dir)
    write_registry(project_home, machine, force=True)
    return env, notes


def _write_marker(library: Library, r: RootInfo, path: pathlib.Path) -> None:
    p = path / ROOT_MARKER
    if p.exists():
        m = RootMarker.read(p)
        if m.library_id != library.library_id:
            raise ConfigError(f"{path} is already a root of another library ({m.library_id})")
    atomic_write_bytes(p, RootMarker(library.library_id, r.root_id, r.name, utc_now()).to_bytes(), hidden=True)


def _verify_marker(library: Library, r: RootInfo, path: pathlib.Path) -> None:
    p = path / ROOT_MARKER
    if not p.exists():
        raise ConfigError(f"{path} has no root marker; is it really root {r.name}? "
                          "(scan --adopt-root writes one)")
    m = RootMarker.read(p)
    if m.root_id != r.root_id or m.library_id != library.library_id:
        raise ConfigError(f"{path} is marked as root {m.name} ({m.root_id}), not {r.name}")


def root_add(env: Env, name: str, path: pathlib.Path, *, intake: bool = False, kind: str | None = None) -> RootInfo:
    path = pathlib.Path(path).absolute()
    if not path.is_dir():
        raise ConfigError(f"{path} is not a folder")
    if any(r.name == name for r in env.library.roots):
        raise ConfigError(f"there is already a root named {name}")
    _check_placement(env.project_home, [(name, path)], env.mcfg.quarantine_root)
    r = RootInfo(uuid4(), name, kind or _root_kind(path), [""] if intake else [])
    if env.mcfg.cache_placement != "local_only":     # read-only setups write nothing into roots
        _write_marker(env.library, r, path)
    env.library.roots.append(r)
    env.library.save(env.project_home)
    env.mcfg.roots[r.root_id] = str(path)
    env.mcfg.save(env.state_dir)
    return r


def protect(env: Env, root_id: str, relpath: str, on: bool = True) -> RootInfo:
    """C: mark a folder tree protected (library.json protected_folders), e.g.
    because it is shared with other people. Folder and subtree removes are
    then ignored inside it; only a file-level decision can remove there."""
    r = env.library.root(root_id)
    rel = relpath.strip("/")
    keys = {name_key(p): p for p in r.protected_folders}
    if on:
        if name_key(rel) not in keys:
            r.protected_folders.append(rel)
    else:
        r.protected_folders = [p for p in r.protected_folders if name_key(p) != name_key(rel)]
    env.library.save(env.project_home)
    return r


def root_map(env: Env, key: str, path: pathlib.Path) -> RootInfo:
    path = pathlib.Path(path).absolute()
    r = env.library.root(key)
    _verify_marker(env.library, r, path)
    env.mcfg.roots[r.root_id] = str(path)
    env.mcfg.save(env.state_dir)
    return r


def open_exiftool(env: Env) -> ExifTool:
    exe = find_exiftool(env.mcfg.exiftool)
    if not exe:
        raise ExifToolError("ExifTool not found: set exiftool in machine.toml, or scan with --no-exiftool")
    return ExifTool(exe)


def scan(env: Env, opts: ScanOptions, *, describer=None, use_exiftool: bool = True, progress=None) -> ScanResult:
    et = None
    if describer is None and use_exiftool and opts.hashing:
        et = describer = open_exiftool(env)
    try:
        res = Scanner(conn=env.conn, state_dir=env.state_dir, project_home=env.project_home, library=env.library,
                      settings=env.settings, mcfg=env.mcfg, machine=env.machine, describer=describer,
                      progress=progress).run(opts)
        link.run(env.conn, env.settings, res.scan_id)       # stage 6: relate (04)
        res.warnings += decisions.ingest(env.conn, env.project_home, env.library.library_id)
        return res
    finally:
        if et:
            et.close()


def rebuild_db(env: Env, progress=None) -> ScanResult:
    """06 §6: a fresh DB from durable sources, hashing nothing: folder caches
    (by a scan that reuses facts only), decision logs (ingested by scan), and
    this machine's quarantine log and batch manifests."""
    env.conn.close()
    p = env.state_dir / DB_NAME
    for suffix in ("", "-wal", "-shm"):
        q = pathlib.Path(str(p) + suffix)
        if q.exists():
            os.replace(q, str(q) + ".old")
    env.conn = db.connect(p)
    db.set_meta(env.conn, "library_id", env.library.library_id)
    db.set_meta(env.conn, "last_rebuild_at", utc_now())
    res = scan(env, ScanOptions(hashing=False), use_exiftool=False, progress=progress)
    res.warnings += ingest_quarantine(env)
    return res


def ingest_quarantine(env: Env) -> list[str]:
    """06 §6 step 5: rebuild `batches` and `quarantine_items` for this machine
    from its quarantine log, cross-checked against the manifests on disk."""
    from . import quarantine
    from .restore import find_batch
    events, warnings = quarantine.read_all(env.project_home, env.library.library_id)
    mine = sorted((e for e in events if e.machine_id == env.machine.machine_id), key=lambda e: (e.at, e.seq))
    last: dict[tuple[str, str, str], object] = {}
    first: dict[str, str] = {}
    for e in mine:
        last[(e.batch_id, e.root_id, e.path)] = e
        first.setdefault(e.batch_id, e.at)
    c = env.conn
    c.execute("BEGIN")
    try:
        for batch_id, created in first.items():
            try:
                qroot = str(find_batch(env.mcfg, batch_id).parent)
            except ConfigError:
                qroot = ""
                warnings.append(f"batch {batch_id}: manifest not found on this machine")
            c.execute("INSERT OR IGNORE INTO batches(batch_id, plan_id, machine_id, quarantine_root, created_at, state) "
                      "VALUES(?, NULL, ?, ?, ?, 'applied')", (batch_id, env.machine.machine_id, qroot, created))
        for (batch_id, root_id, path), e in last.items():
            c.execute("INSERT OR REPLACE INTO quarantine_items(batch_id, root_id, relpath, sha256, size, keeper_root_id, "
                      "keeper_relpath, qpath, state, moved_at, last_error) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (batch_id, root_id, path, e.sha256, e.size, e.keeper_root_id, e.keeper_path, e.qpath, e.event,
                       e.at if e.event == "moved" else None, e.detail))
        for batch_id in first:
            states = {r[0] for r in c.execute("SELECT state FROM quarantine_items WHERE batch_id = ?", (batch_id,))}
            state = ("purged" if states <= {"purged"} else "restored" if states <= {"restored", "purged"}
                     else "verified" if states <= {"verified", "purged", "restored"} else "applied")
            c.execute("UPDATE batches SET state = ? WHERE batch_id = ?", (state, batch_id))
        c.execute("COMMIT")
    except BaseException:
        c.execute("ROLLBACK")
        raise
    return warnings


def make_report(env: Env, *, out: pathlib.Path | None = None, thumbs: bool = True, thumb_limit: int = 5000):
    et = None
    if thumbs:
        try:
            et = open_exiftool(env)
        except ExifToolError:
            et = None
    try:
        return report.build(env.conn, env.library, state_dir=env.state_dir, project_home=env.project_home,
                            machine_name=env.machine.name, out=out, exiftool=et, thumb_limit=thumb_limit)
    finally:
        if et:
            et.close()


def make_plan(env: Env, decisions_file: pathlib.Path | None = None):
    """08 §3: import decisions if given, plan, save. Returns (plan, list file)."""
    if decisions_file:
        decisions.import_export(env.conn, env.project_home, env.state_dir, env.library, env.machine.machine_id,
                                decisions_file)
    if env.library.apply_machine_id == env.machine.machine_id:
        applier.Applier(env).recover()
    p = planner.make_plan(env.conn, env.library, env.project_home, dict(env.mcfg.roots),
                          env.mcfg.quarantine_root or None)
    path = planner.save(env.conn, p, env.state_dir, {r.root_id: r.name for r in env.library.roots})
    return p, path


def apply_execute(env: Env, *, batch_limit: int | None = None, rescan: bool = True, progress=None):
    """08 §4: carry out the last reviewed plan, verify, then a light rescan of
    the affected roots so this machine's cache files reflect the moves."""
    db.backup(env.conn, env.state_dir)
    res = applier.Applier(env).apply(batch_limit=batch_limit, progress=progress)
    if rescan and res.moved:
        roots = {r[0] for b in res.batch_ids for r in env.conn.execute(
            "SELECT DISTINCT root_id FROM quarantine_items WHERE batch_id = ?", (b,))}
        names = [r.name for r in env.library.roots if r.root_id in roots]
        scan(env, ScanOptions(roots=names), use_exiftool=False, progress=progress)
    return res


def parse_target_path(env: Env, s: str) -> tuple[str, str]:
    """'ROOT:relpath' or, with a single root, 'relpath' -> (root_id, relpath)."""
    if ":" in s and not (len(s) > 1 and s[1] == ":" and s[0].isalpha()):
        root, _, rel = s.partition(":")
        r = env.library.root(root)
    else:
        if len(env.library.roots) != 1:
            raise ConfigError("this library has several roots: write ROOT:path")
        r, rel = env.library.roots[0], s
    rel = rel.replace("\\", "/").strip("/")
    return r.root_id, rel


# ------------------------------------------------------------------ status

def status(env: Env) -> dict:
    c = env.conn
    q = lambda sql, *a: c.execute(sql, a).fetchone()
    out: dict = {"library": env.library.name or env.library.library_id, "machine": env.machine.name, "roots": []}
    for r in env.library.roots:
        rows = c.execute("SELECT state, count(*), sum(size), sum(sha256 IS NOT NULL) FROM files WHERE root_id = ? "
                         "GROUP BY state", (r.root_id,)).fetchall()
        out["roots"].append({"name": r.name, "mapped": env.mcfg.roots.get(r.root_id),
                             "states": {s: {"files": n, "bytes": b or 0, "hashed": h or 0} for s, n, b, h in rows}})
    pend = q("SELECT count(*) FROM files WHERE state IN ('present') AND sha256 IS NULL AND media != 'other'")[0]
    dup = q("SELECT count(*), coalesce(sum(instances), 0), coalesce(sum(reclaimable), 0) FROM dup_groups")
    ceq = q("SELECT count(*), coalesce(sum(n), 0) FROM (SELECT c.content_sha256, count(DISTINCT c.sha256) AS n "
            "FROM contents c JOIN files f ON f.sha256 = c.sha256 AND f.state = 'present' "
            "WHERE c.content_sha256 IS NOT NULL GROUP BY c.content_sha256 HAVING count(DISTINCT c.sha256) > 1)")
    out["pending_hash"] = pend
    out["dup_groups"] = {"groups": dup[0], "instances": dup[1], "reclaimable_bytes": dup[2]}
    out["content_equivalent_groups"] = {"groups": ceq[0], "contents": ceq[1]}
    out["flags"] = {f: n for f, n in c.execute("SELECT flag, count(*) FROM flags GROUP BY flag")}
    out["quarantine"] = {s: {"files": n, "bytes": b or 0} for s, n, b in c.execute(
        "SELECT state, count(*), sum(size) FROM quarantine_items GROUP BY state")}
    last = q("SELECT scan_id, started_at, finished_at, status, files_seen, files_hashed, errors FROM scans "
             "ORDER BY scan_id DESC LIMIT 1")
    out["last_scan"] = dict(last) if last else None
    return out


# ------------------------------------------------------------------ doctor

@dataclasses.dataclass
class Check:
    level: str     # ok | warn | fail
    what: str
    detail: str = ""


def doctor(state_dir: pathlib.Path | None = None) -> list[Check]:
    state_dir = pathlib.Path(state_dir or default_state_dir())
    out: list[Check] = []
    try:
        env = open_env(state_dir, lock=False)
    except (ConfigError, FormatError, OSError) as e:
        return [Check("fail", "configuration", str(e))]
    try:
        out.append(Check("ok", "machine", f"{env.machine.name} ({env.machine.machine_id})"))
        # ExifTool
        exe = find_exiftool(env.mcfg.exiftool)
        if not exe:
            out.append(Check("fail", "ExifTool", "not found; set exiftool in machine.toml"))
        else:
            try:
                with ExifTool(exe) as et:
                    out.append(Check("ok", "ExifTool", f"{et.version} at {exe}"))
            except ExifToolError as e:
                out.append(Check("fail", "ExifTool", str(e)))
        # long paths
        if IS_WINDOWS:
            out.append(_long_paths_check())
        # roots and markers
        for r in env.library.roots:
            p = env.mcfg.roots.get(r.root_id)
            if not p:
                out.append(Check("warn", f"root {r.name}", "not mapped on this machine"))
                continue
            try:
                if env.mcfg.cache_placement == "local_only" and not (pathlib.Path(p) / ROOT_MARKER).exists():
                    out.append(Check("ok", f"root {r.name}", f"{p} (read-only: no root marker yet)"))
                else:
                    _verify_marker(env.library, r, pathlib.Path(p))
                    out.append(Check("ok", f"root {r.name}", p))
            except (ConfigError, FormatError) as e:
                out.append(Check("fail", f"root {r.name}", str(e)))
            if is_inside(env.project_home, p):
                out.append(Check("fail", "project home", f"inside root {r.name}"))
            if env.mcfg.quarantine_root and is_inside(env.mcfg.quarantine_root, p):
                out.append(Check("fail", "quarantine_root", f"inside root {r.name}; leave it unset to use "
                                 "PhotoHaven-DELETE"))
        # identity: registry, clones, short clashes, clock skew
        reg = read_registry(env.project_home)
        mine = reg.get(env.machine.machine_id, {})
        if mine.get("hostname") and mine["hostname"] != platform.node():
            out.append(Check("warn", "identity", f"registry hostname {mine['hostname']} differs from "
                             f"{platform.node()}: a cloned disk? ('photohaven machine reset' makes a new identity)"))
        shorts = [r.get("machine_short") for r in reg.values()]
        if len(shorts) != len(set(shorts)):
            out.append(Check("fail", "identity", "two machines share a machine_short"))
        newest = max((r.get("last_seen", "") for mid, r in reg.items() if mid != env.machine.machine_id), default="")
        if newest and newest > utc_now():
            out.append(Check("warn", "clock", f"another machine was seen at {newest}, after this machine's "
                             "clock: check time sync"))
        # conflict copies
        conflicts = [n for n in os.listdir(env.project_home) if re.match(r"library-.+\.json$", n)]
        if conflicts:
            out.append(Check("warn", "conflict copies", ", ".join(conflicts)))
        cc =_cache_conflict_copies(env)
        if cc:
            out.append(Check("warn", "cache conflict copies", f"{len(cc)} found, e.g. {cc[0]}"))
        # placeholders
        n = env.conn.execute("SELECT count(*), coalesce(sum(sha256 IS NULL), 0) FROM files "
                             "WHERE state = 'cloud_only'").fetchone()
        if n[0]:
            out.append(Check("warn", "cloud-only files", f"{n[0]} ({n[1]} without facts from any machine)"))
        if env.mcfg.quarantine_root:
            out.append(Check("warn", "delete folder", f"machine.toml overrides it: moved duplicates go to "
                             f"{env.mcfg.quarantine_root} instead of each root's PhotoHaven-DELETE (D-10)"))
        else:
            out.append(Check("ok", "delete folder", "PhotoHaven-DELETE at the top of each root"))
        if env.mcfg.cache_placement == "local_only":
            out.append(Check("ok", "read-only", "cache_placement = local_only: scans write nothing into the roots"))
    finally:
        env.close()
    return out


def _long_paths_check() -> Check:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            v, _ = winreg.QueryValueEx(k, "LongPathsEnabled")
    except OSError:
        v = 0
    if v == 1:
        return Check("ok", "long paths", "LongPathsEnabled = 1")
    return Check("warn", "long paths", "LongPathsEnabled = 0. Photo Haven copes (\\\\?\\ paths, ExifTool "
                 "WindowsLongPath), but other tools may not open paths over 260 characters")


def _cache_conflict_copies(env: Env) -> list[str]:
    out = []
    for fid, root_id, rel in env.conn.execute("SELECT folder_id, root_id, relpath FROM folders WHERE present = 1"):
        base = env.mcfg.roots.get(root_id)
        if not base:
            continue
        d = os.path.join(long_path(base), *rel.split("/")) if rel else long_path(base)
        try:
            names = os.listdir(d)
        except OSError:
            continue
        out += [f"{rel}/{n}" for n in names if is_cache_file_name(n) and not re.fullmatch(
            r"\.photohaven-cache\.[0-9a-f]{12}\.jsonl", n)]
        if len(out) > 20:
            break
    return out
