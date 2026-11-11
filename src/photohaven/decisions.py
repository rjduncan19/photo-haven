"""Decision logs, import of report exports, and effective-decision
resolution (docs/03 §5, docs/07 §5–6).
"""
from __future__ import annotations

import dataclasses
import glob
import json
import os
import pathlib
import sqlite3

from .config import Library, folder_flag
from .formats import (Decision, FormatError, Target, append_jsonl, decision_log_header, read_decision_export,
                      read_decision_log)
from .util import long_path, machine_short, name_key, split_rel, utc_now, uuid7

PHASE = "dedupe"


def log_dir(project_home: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(project_home) / "logs"


def log_path(project_home: pathlib.Path, machine_id: str) -> pathlib.Path:
    return log_dir(project_home) / f"decisions.{machine_short(machine_id)}.jsonl"


# ------------------------------------------------------------------ ingest

def ingest(conn: sqlite3.Connection, project_home: pathlib.Path, library_id: str) -> list[str]:
    """Materialize every machine's decision log into the `decisions` table
    (06 §4). Idempotent: records are keyed by id."""
    warnings: list[str] = []
    rows = []
    for p in sorted(glob.glob(os.path.join(glob.escape(str(log_dir(project_home))), "decisions.*.jsonl"))):
        try:
            header, recs, w = read_decision_log(p)
        except (FormatError, OSError, UnicodeDecodeError) as e:
            warnings.append(f"ignoring {p} ({e})")
            continue
        warnings += w
        if header.get("library_id") != library_id:
            warnings.append(f"ignoring {p}: it belongs to another library")
            continue
        for d in recs:
            t = d.target
            rows.append((d.id, d.at, d.machine_id, d.seq, d.phase, t.kind, t.root_id, t.path, name_key(t.path),
                         t.scope, t.sha256, t.size, d.action, d.origin, d.report_id, d.note))
    conn.execute("BEGIN")
    conn.executemany("INSERT OR IGNORE INTO decisions(id, at, machine_id, seq, phase, target_kind, root_id, path, "
                     "path_key, scope, sha256, size, action, origin, report_id, note) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.execute("COMMIT")
    return warnings


def _next_seq(path: pathlib.Path) -> int:
    if not os.path.exists(long_path(path)):
        return 1
    _, recs, _ = read_decision_log(path)
    return max((d.seq for d in recs), default=0) + 1


def append(conn: sqlite3.Connection, project_home: pathlib.Path, library_id: str, machine_id: str,
           decisions: list[Decision]) -> list[Decision]:
    """Assign id, machine_id and seq; append to this machine's log; ingest."""
    if not decisions:
        return []
    p = log_path(project_home, machine_id)
    seq = _next_seq(p)
    out = []
    for d in decisions:
        out.append(dataclasses.replace(d, id=uuid7(), machine_id=machine_id, seq=seq))
        seq += 1
    append_jsonl(p, decision_log_header(library_id, machine_id, utc_now()), [d.to_record() for d in out])
    ingest(conn, project_home, library_id)
    return out


# ------------------------------------------------------------------ import

@dataclasses.dataclass
class ImportResult:
    added: int
    skipped: int
    warnings: list[str]
    report_id: str


def import_export(conn: sqlite3.Connection, project_home: pathlib.Path, state_dir: pathlib.Path,
                  library: Library, machine_id: str, path: pathlib.Path) -> ImportResult:
    header, recs = read_decision_export(path)
    if header["library_id"] != library.library_id:
        raise FormatError(f"{path} is from another library ({header['library_id']}), not {library.library_id}")
    report_id = header["report_id"]
    warnings = ingest(conn, project_home, library.library_id)
    meta_p = pathlib.Path(state_dir) / "reports" / report_id / "report.json"
    if not meta_p.exists():
        warnings.append(f"report {report_id} was not generated on this machine; importing anyway")
    scan_id = header.get("scan_id")
    if isinstance(scan_id, int):
        newer = conn.execute("SELECT count(*) FROM scans WHERE scan_id > ? AND status = 'completed'",
                             (scan_id,)).fetchone()[0]
        if newer:
            warnings.append(f"{newer} scan(s) ran after this report: the library may have changed since you "
                            "reviewed it (every move re-checks the disk, so this is safe, but check the plan)")
    roots = {r.root_id for r in library.roots}
    existing = {(r["report_id"], _target_key_row(r), r["action"]) for r in conn.execute(
        "SELECT * FROM decisions WHERE report_id = ?", (report_id,))}
    todo, skipped = [], 0
    for d in recs:
        if d.target.root_id not in roots:
            raise FormatError(f"{path}: decision for unknown root {d.target.root_id}")
        if d.phase != PHASE:
            raise FormatError(f"{path}: unknown phase {d.phase!r}")
        key = (report_id, d.target.key(), d.action)
        if key in existing:
            skipped += 1
            continue
        existing.add(key)
        todo.append(dataclasses.replace(d, origin="import", report_id=report_id, id=None, machine_id=None, seq=None))
    append(conn, project_home, library.library_id, machine_id, todo)
    return ImportResult(len(todo), skipped, warnings, report_id)


def _target_key_row(r: sqlite3.Row) -> str:
    return Target(r["target_kind"], r["root_id"], r["path"], r["scope"], r["sha256"], r["size"]).key()


# -------------------------------------------------------------- resolution

@dataclasses.dataclass
class Step:
    level: str                 # file | folder | subtree
    path: str
    action: str
    at: str
    machine_id: str
    used: bool = False
    note: str | None = None    # why ignored / overridden


@dataclasses.dataclass
class Effective:
    action: str                # keep | remove | undecided
    level: str | None          # which level decided, None if undecided
    chain: list[Step]


def _latest(rows: list[sqlite3.Row]) -> sqlite3.Row | None:
    return max(rows, key=lambda r: (r["at"], r["machine_id"], r["seq"])) if rows else None


def is_protected(library: Library, root_id: str, folder_relpath: str) -> bool:
    try:
        r = library.root(root_id)
    except Exception:
        return False
    return folder_flag(folder_relpath, r.intake_folders) or folder_flag(folder_relpath, r.protected_folders)


def effective(conn: sqlite3.Connection, library: Library, root_id: str, relpath: str,
              sha256: str | None) -> Effective:
    """07 §5: file decision (same bytes) > folder decision on its folder >
    nearest subtree decision > undecided. Latest wins within a level;
    `undecided` there falls through. Folder/subtree removes are ignored
    inside protected/intake folders."""
    folder, _ = split_rel(relpath)
    protected = is_protected(library, root_id, folder)
    chain: list[Step] = []

    def level(kind: str, path: str, rows: list[sqlite3.Row]) -> Effective | None:
        r = _latest(rows)
        if r is None:
            return None
        s = Step(kind, path, r["action"], r["at"], r["machine_id"])
        chain.append(s)
        if r["action"] == "undecided":
            s.note = "undecided here: falls through"
            return None
        if protected and kind != "file" and r["action"] == "remove":
            s.note = "ignored: protected/intake folder (only a file decision can remove here)"
            return None
        s.used = True
        return Effective(r["action"], kind, chain)

    q = "SELECT * FROM decisions WHERE phase = ? AND root_id = ? AND path_key = ? AND target_kind = ?"
    if sha256:
        rows = [r for r in conn.execute(q, (PHASE, root_id, name_key(relpath), "file")) if r["sha256"] == sha256]
        other = [r for r in conn.execute(q, (PHASE, root_id, name_key(relpath), "file")) if r["sha256"] != sha256]
        for r in other:
            chain.append(Step("file", relpath, r["action"], r["at"], r["machine_id"],
                              note="ignored: made about different bytes"))
        if (e := level("file", relpath, rows)):
            return e
    rows = [r for r in conn.execute(q, (PHASE, root_id, name_key(folder), "folder")) if r["scope"] == "folder"]
    if (e := level("folder", folder, rows)):
        return e
    parts = folder.split("/") if folder else []
    for n in range(len(parts), -1, -1):
        anc = "/".join(parts[:n])
        rows = [r for r in conn.execute(q, (PHASE, root_id, name_key(anc), "folder")) if r["scope"] == "subtree"]
        if (e := level("subtree", anc, rows)):
            return e
    return Effective("undecided", None, chain)


def list_decisions(conn: sqlite3.Connection, target: str | None = None) -> list[sqlite3.Row]:
    if target:
        k = name_key(target.strip("/"))
        return conn.execute("SELECT * FROM decisions WHERE path_key = ? OR path_key LIKE ? ESCAPE '\\' "
                            "ORDER BY at, machine_id, seq",
                            (k, k.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "/%")).fetchall()
    return conn.execute("SELECT * FROM decisions ORDER BY at, machine_id, seq").fetchall()


def orphaned(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Decisions whose target no longer exists (07 §3 Warnings)."""
    out = []
    for r in conn.execute("SELECT * FROM decisions WHERE action != 'undecided'"):
        if r["target_kind"] == "file":
            hit = conn.execute("SELECT 1 FROM files WHERE root_id = ? AND path_key = ? AND sha256 = ? "
                               "AND state IN ('present','cloud_only')", (r["root_id"], r["path_key"], r["sha256"])).fetchone()
        else:
            hit = conn.execute("SELECT 1 FROM folders WHERE root_id = ? AND path_key = ? AND present = 1",
                               (r["root_id"], r["path_key"])).fetchone()
        if not hit:
            out.append(r)
    return out
