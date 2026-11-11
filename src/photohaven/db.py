"""The SQLite index (docs/03 §9): creation, migrations, upserts, queries.

Derived and rebuildable from folder caches and logs (docs/06 §6). Besides
formats.py, this is the only module that knows a persisted layout.
"""
from __future__ import annotations

import importlib.resources
import json
import pathlib
import sqlite3
from typing import Iterable

from . import SPEC_VERSION
from .formats import FileFacts, NewerSchemaError, prune
from .util import long_path, name_key, split_rel, stem_of, ext_of, utc_now

# meta keys stored as plain columns of `contents` (03 §3.3 ↔ §9)
META_COLUMNS = ("mime", "taken_local", "taken_offset", "taken_utc", "taken_source", "make", "model", "lens",
                "width", "height", "orientation", "gps_lat", "gps_lon", "gps_alt", "duration_ms",
                "apple_content_id", "apple_burst_id", "xmp_original_image_hash", "xmp_ingest_sha256")

# schema version -> function migrating a DB at that version to the next one.
MIGRATIONS: dict[int, "callable"] = {}


def schema_sql() -> str:
    return importlib.resources.files("photohaven").joinpath("schema_v1.sql").read_text(encoding="utf-8")


def connect(path: pathlib.Path | str, *, create: bool = True) -> sqlite3.Connection:
    p = pathlib.Path(path)
    if not create and not p.exists():
        raise FileNotFoundError(f"no database at {p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(long_path(p) if p.is_absolute() else str(p), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    v = conn.execute("PRAGMA user_version").fetchone()[0]
    if v == 0:
        if conn.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]:
            raise RuntimeError(f"{p}: not a Photo Haven database (user_version 0 but has tables)")
        conn.executescript("BEGIN;" + schema_sql() + "COMMIT;")
        set_meta(conn, "schema_version", str(SPEC_VERSION))
        set_meta(conn, "created_at", utc_now())
    elif v > SPEC_VERSION:
        conn.close()
        raise NewerSchemaError(f"{p}: database schema {v} is newer than this Photo Haven ({SPEC_VERSION})")
    else:
        while v < SPEC_VERSION:
            MIGRATIONS[v](conn)
            v = conn.execute("PRAGMA user_version").fetchone()[0]
    return conn


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    r = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return r[0] if r else None


# ------------------------------------------------------------------ roots etc.

def upsert_machine(conn, machine_id: str, short: str, name: str | None, last_seen: str | None) -> None:
    conn.execute(
        "INSERT INTO machines(machine_id, machine_short, name, last_seen) VALUES(?,?,?,?) "
        "ON CONFLICT(machine_id) DO UPDATE SET name = excluded.name, "
        "last_seen = max(coalesce(machines.last_seen, ''), coalesce(excluded.last_seen, ''))",
        (machine_id, short, name, last_seen))


def upsert_root(conn, root_id: str, name: str, kind: str, local_path: str | None) -> None:
    conn.execute(
        "INSERT INTO roots(root_id, name, kind, local_path) VALUES(?,?,?,?) "
        "ON CONFLICT(root_id) DO UPDATE SET name = excluded.name, kind = excluded.kind, "
        "local_path = excluded.local_path", (root_id, name, kind, local_path))


def start_scan(conn, machine_id: str, options: dict) -> int:
    cur = conn.execute("INSERT INTO scans(machine_id, started_at, status, options_json) VALUES(?,?,?,?)",
                       (machine_id, utc_now(), "running", json.dumps(options, sort_keys=True)))
    return cur.lastrowid


def finish_scan(conn, scan_id: int, status: str, **counters: int) -> None:
    sets = ", ".join(f"{k} = ?" for k in counters)
    conn.execute(f"UPDATE scans SET finished_at = ?, status = ?{', ' + sets if sets else ''} WHERE scan_id = ?",
                 (utc_now(), status, *counters.values(), scan_id))


# ---------------------------------------------------------------- folders

def upsert_folder(conn, root_id: str, relpath: str, scan_id: int, *, is_generic: bool,
                  is_intake: bool, is_protected: bool) -> int:
    """Insert or refresh a folder row (and its parents), returning folder_id."""
    r = conn.execute("SELECT folder_id FROM folders WHERE root_id = ? AND relpath = ?", (root_id, relpath)).fetchone()
    if r:
        conn.execute("UPDATE folders SET present = 1, last_scan_id = ?, is_generic = ?, is_intake = ?, "
                     "is_protected = ? WHERE folder_id = ?",
                     (scan_id, int(is_generic), int(is_intake), int(is_protected), r[0]))
        return r[0]
    parent_id = None
    parts = relpath.split("/") if relpath else []
    if parts:
        parent_rel = "/".join(parts[:-1])
        pr = conn.execute("SELECT folder_id FROM folders WHERE root_id = ? AND relpath = ?",
                          (root_id, parent_rel)).fetchone()
        parent_id = pr[0] if pr else upsert_folder(conn, root_id, parent_rel, scan_id, is_generic=False,
                                                   is_intake=is_intake, is_protected=is_protected)
    name = parts[-1] if parts else ""
    cur = conn.execute(
        "INSERT INTO folders(root_id, relpath, path_key, parent_id, depth, name, is_generic, is_intake, "
        "is_protected, present, last_scan_id) VALUES(?,?,?,?,?,?,?,?,?,1,?)",
        (root_id, relpath, name_key(relpath), parent_id, len(parts), name, int(is_generic), int(is_intake),
         int(is_protected), scan_id))
    fid = cur.lastrowid
    conn.executemany("INSERT INTO folder_levels(folder_id, level, name, name_key) VALUES(?,?,?,?)",
                     [(fid, i, p, name_key(p)) for i, p in enumerate(parts, 1)])
    return fid


# ---------------------------------------------------------------- contents

def content_row(conn, sha256: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM contents WHERE sha256 = ?", (sha256,)).fetchone()


def facts_from_content(row: sqlite3.Row) -> dict:
    """The content-derived parts of a cache record, from a `contents` row."""
    meta = {k: row[k] for k in META_COLUMNS if row[k] is not None}
    if row["damaged"]:
        meta["damaged"] = True
    if row["exiftool_messages"]:
        meta["exiftool_messages"] = json.loads(row["exiftool_messages"])
    return prune({"content_sha256": row["content_sha256"], "content_hash_impl": row["content_hash_impl"],
                  "meta": meta or None, "meta_impl": row["meta_impl"]})


def upsert_content(conn, f: FileFacts, now: str) -> None:
    """Insert the content for f.sha256, or fill in metadata if the stored row
    has none yet (or an older ph-meta)."""
    assert f.sha256
    meta = f.meta or {}
    cols = {k: meta.get(k) for k in META_COLUMNS}
    cols.update(content_sha256=f.content_sha256, content_hash_impl=f.content_hash_impl,
                damaged=1 if meta.get("damaged") else 0,
                exiftool_messages=json.dumps(meta["exiftool_messages"], ensure_ascii=False)
                if meta.get("exiftool_messages") else None,
                meta_impl=f.meta_impl)
    old = content_row(conn, f.sha256)
    if old is None:
        names = ["sha256", "size", "media", *cols, "first_seen"]
        conn.execute(f"INSERT INTO contents({', '.join(names)}) VALUES({', '.join('?' * len(names))})",
                     (f.sha256, f.size, f.media, *cols.values(), now))
    elif f.meta_impl and _meta_newer(f.meta_impl, old["meta_impl"]):
        conn.execute(f"UPDATE contents SET {', '.join(f'{k} = ?' for k in cols)} WHERE sha256 = ?",
                     (*cols.values(), f.sha256))


def _ph_meta(impl: str | None) -> int:
    if not impl:
        return -1
    for part in impl.split(";"):
        if part.startswith("ph-meta/"):
            try:
                return int(part[8:])
            except ValueError:
                return -1
    return -1


def _meta_newer(new: str, old: str | None) -> bool:
    return old is None or _ph_meta(new) > _ph_meta(old)


# ---------------------------------------------------------------- files

def upsert_file(conn, *, root_id: str, folder_id: int, relpath: str, media: str, size: int, mtime_us: int,
                ctime_us: int | None, sha256: str | None, hashed_at: str | None, hashed_by: str | None,
                state: str, skip_reason: str | None, scan_id: int, now: str) -> int:
    _, name = split_rel(relpath)
    r = conn.execute("SELECT file_id FROM files WHERE root_id = ? AND relpath = ?", (root_id, relpath)).fetchone()
    vals = dict(folder_id=folder_id, path_key=name_key(relpath), name=name, stem_key=name_key(stem_of(name)),
                ext=ext_of(name), media=media, size=size, mtime_us=mtime_us, ctime_us=ctime_us, sha256=sha256,
                hashed_at=hashed_at, hashed_by=hashed_by, state=state, skip_reason=skip_reason, last_seen=now,
                last_scan_id=scan_id)
    if r:
        conn.execute(f"UPDATE files SET {', '.join(f'{k} = ?' for k in vals)} WHERE file_id = ?",
                     (*vals.values(), r[0]))
        return r[0]
    names = ["root_id", "relpath", *vals, "first_seen"]
    cur = conn.execute(f"INSERT INTO files({', '.join(names)}) VALUES({', '.join('?' * len(names))})",
                       (root_id, relpath, *vals.values(), now))
    return cur.lastrowid


def set_flags(conn, file_id: int, flags: dict[str, str | None], scan_id: int,
              owned: Iterable[str]) -> None:
    """Replace the scanner-owned flags of a file with `flags`."""
    owned = tuple(owned)
    conn.execute(f"DELETE FROM flags WHERE file_id = ? AND flag IN ({', '.join('?' * len(owned))})",
                 (file_id, *owned))
    conn.executemany("INSERT INTO flags(file_id, flag, detail, scan_id) VALUES(?,?,?,?)",
                     [(file_id, k, v, scan_id) for k, v in flags.items()])


def add_folder_name(conn, sha256: str, root_id: str, folder_relpath: str, now: str) -> None:
    _, name = split_rel(folder_relpath)
    conn.execute("INSERT OR IGNORE INTO content_folder_names(sha256, root_id, folder_relpath, folder_name, first_seen) "
                 "VALUES(?,?,?,?,?)", (sha256, root_id, folder_relpath, name, now))


def mark_missing_in_folder(conn, folder_id: int, scan_id: int) -> int:
    cur = conn.execute("UPDATE files SET state = 'missing' WHERE folder_id = ? AND last_scan_id IS NOT ? "
                       "AND state IN ('present', 'cloud_only', 'unreadable')", (folder_id, scan_id))
    return cur.rowcount


def mark_missing_under(conn, root_id: str, under_relpath: str, scan_id: int) -> int:
    """Folders (and their files) under `under_relpath` that this scan didn't see."""
    like = "%" if under_relpath == "" else under_relpath.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%"
    sel = ("SELECT folder_id FROM folders WHERE root_id = ? AND last_scan_id IS NOT ? AND present = 1 "
           "AND (relpath = ? OR relpath LIKE ? ESCAPE '\\')")
    ids = [r[0] for r in conn.execute(sel, (root_id, scan_id, under_relpath, like))]
    n = 0
    for fid in ids:
        conn.execute("UPDATE folders SET present = 0 WHERE folder_id = ?", (fid,))
        n += mark_missing_in_folder(conn, fid, scan_id)
    return n


def folder_sizes(conn) -> dict[int, int]:
    """size -> number of present-ish instances in the DB, for the candidates pass."""
    return {r[0]: r[1] for r in conn.execute(
        "SELECT size, count(*) FROM files WHERE state IN ('present', 'cloud_only', 'unreadable') GROUP BY size")}
