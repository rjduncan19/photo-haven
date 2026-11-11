"""The planner (docs/08 §2–3): effective decisions -> keepers -> plan items.

Enforces the last-copy invariant (I-2) and refuses keepers that any
machine's quarantine log lists as moved (I-3). A plan is saved so that
`apply --execute` runs exactly what was reviewed.
"""
from __future__ import annotations

import collections
import dataclasses
import hashlib
import json
import os
import pathlib
import sqlite3

from . import analysis, quarantine
from .config import Library, folder_flag
from .decisions import PHASE, Resolver, ingest
from .formats import DELETE_FOLDER
from .util import atomic_write_text, long_path, name_key, rel_to_path, utc_now, uuid7


def holding_root(root_path: str | os.PathLike, override: str | None) -> pathlib.Path:
    """Where moved duplicates of this root go (D-10): `<root>/PhotoHaven-DELETE`,
    unless machine.toml sets an explicit quarantine_root for every root."""
    return pathlib.Path(override) if override else pathlib.Path(root_path) / DELETE_FOLDER


@dataclasses.dataclass(frozen=True)
class Item:
    file_id: int
    root_id: str
    relpath: str
    sha256: str
    size: int
    mtime_us: int
    keeper_file_id: int
    keeper_root_id: str
    keeper_relpath: str
    keeper_size: int
    keeper_mtime_us: int

    def key(self) -> tuple:
        return (self.file_id, self.sha256, self.size, self.mtime_us, self.keeper_file_id)


@dataclasses.dataclass
class Plan:
    plan_id: str
    created_at: str
    scan_id: int | None
    digest: str
    items: list[Item]
    kept: int = 0                                  # duplicate instances left by keep/undecided
    blocked: list[tuple[str, str]] = dataclasses.field(default_factory=list)       # (root_id, relpath)
    conflicts: list[tuple[str, str, str]] = dataclasses.field(default_factory=list)  # (sha, root_id, relpath kept)
    skipped: list[tuple[str, str, str]] = dataclasses.field(default_factory=list)    # (root_id, relpath, reason)
    ignored_protected: int = 0
    warnings: list[str] = dataclasses.field(default_factory=list)

    @property
    def bytes(self) -> int:
        return sum(i.size for i in self.items)

    def summary(self) -> dict:
        return {"items": len(self.items), "bytes": self.bytes, "kept": self.kept, "blocked": len(self.blocked),
                "conflicts": len(self.conflicts), "skipped": len(self.skipped),
                "ignored_protected": self.ignored_protected}


def decisions_digest(conn: sqlite3.Connection) -> str:
    ids = [r[0] for r in conn.execute("SELECT id FROM decisions WHERE phase = ? ORDER BY id", (PHASE,))]
    return hashlib.sha256("\n".join(ids).encode()).hexdigest()


def _existing(p: str | os.PathLike) -> str:
    """p, or its nearest existing ancestor (the quarantine root may not exist yet)."""
    cur = pathlib.Path(os.path.abspath(p))
    while not os.path.exists(long_path(cur)) and cur.parent != cur:
        cur = cur.parent
    return long_path(cur)


def same_volume(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    try:
        return os.stat(_existing(a)).st_dev == os.stat(_existing(b)).st_dev
    except OSError:
        return False


def make_plan(conn: sqlite3.Connection, library: Library, project_home: pathlib.Path,
              root_paths: dict[str, str], quarantine_root: str | None) -> Plan:
    warnings = ingest(conn, project_home, library.library_id)
    events, w = quarantine.read_all(project_home, library.library_id)
    warnings += w
    out = quarantine.out_of_library(events)
    resolver = Resolver(conn, library)
    an = analysis.analyze(conn, library)
    scan = conn.execute("SELECT max(scan_id) FROM scans WHERE status = 'completed'").fetchone()[0]
    plan = Plan(uuid7(), utc_now(), scan, decisions_digest(conn), [], warnings=warnings)

    rows = conn.execute("SELECT file_id, root_id, folder_id, relpath, sha256, size, mtime_us, state FROM files "
                        "WHERE sha256 IS NOT NULL AND state IN ('present', 'cloud_only') "
                        "ORDER BY sha256, root_id, relpath").fetchall()
    groups: dict[str, list[sqlite3.Row]] = collections.defaultdict(list)
    for r in rows:
        groups[r["sha256"]].append(r)
    q_same = {rid: same_volume(p, holding_root(p, quarantine_root)) for rid, p in root_paths.items()}

    def rank(r, eff):
        root = library.root(r["root_id"])
        folder = r["relpath"].rsplit("/", 1)[0] if "/" in r["relpath"] else ""
        intake = folder_flag(folder, root.intake_folders) if folder else ("" in root.intake_folders)
        desc = analysis.path_desc(an, r["folder_id"]) if r["folder_id"] in an.folders else (0, 0, 0, 0)
        generic = an.folders[r["folder_id"]].is_generic if r["folder_id"] in an.folders else True
        return (eff.action != "keep", not intake, generic, tuple(-x for x in desc), len(r["relpath"]), r["relpath"])

    for sha, inst in groups.items():
        effs = {r["file_id"]: resolver.effective(r["root_id"], r["relpath"], sha) for r in inst}
        for r in inst:
            e = effs[r["file_id"]]
            if any(s.note and s.note.startswith("ignored: protected") for s in e.chain):
                plan.ignored_protected += 1
        R = [r for r in inst if effs[r["file_id"]].action == "remove"]
        if len(inst) == 1:
            if R:
                plan.blocked.append((R[0]["root_id"], R[0]["relpath"]))
            continue
        if not R:
            continue
        K = [r for r in inst if effs[r["file_id"]].action != "remove"]
        plan.kept += len(K)

        def eligible(r) -> bool:
            return sha not in out.get((r["root_id"], name_key(r["relpath"])), set())
        K = [r for r in K if eligible(r)]
        if not K:
            candidates = [r for r in R if eligible(r) and r["state"] == "present"] or [r for r in R if eligible(r)]
            if not candidates:
                plan.warnings.append(f"{sha}: every copy is listed in a quarantine log; nothing planned")
                continue
            promoted = min(candidates, key=lambda r: rank(r, effs[r["file_id"]]))
            plan.conflicts.append((sha, promoted["root_id"], promoted["relpath"]))
            R = [r for r in R if r["file_id"] != promoted["file_id"]]
            K = [promoted]
        keeper = min(K, key=lambda r: rank(r, effs[r["file_id"]]))
        for r in R:
            if not eligible(r):
                continue        # already out of the library per some log
            if r["state"] == "cloud_only" and not q_same.get(r["root_id"]):
                plan.skipped.append((r["root_id"], r["relpath"], "cloud-only (moving it off the volume "
                                     "would download it first)"))
                continue
            plan.items.append(Item(r["file_id"], r["root_id"], r["relpath"], sha, r["size"], r["mtime_us"],
                                   keeper["file_id"], keeper["root_id"], keeper["relpath"], keeper["size"],
                                   keeper["mtime_us"]))
    plan.items.sort(key=lambda i: (i.root_id, [name_key(p) for p in i.relpath.split("/")]))
    return plan


def save(conn: sqlite3.Connection, plan: Plan, state_dir: pathlib.Path, roots: dict[str, str]) -> pathlib.Path:
    conn.execute("BEGIN")
    try:
        conn.execute("INSERT INTO plans(plan_id, created_at, scan_id, decisions_digest, summary_json) VALUES(?,?,?,?,?)",
                     (plan.plan_id, plan.created_at, plan.scan_id, plan.digest, json.dumps(plan.summary())))
        conn.executemany("INSERT INTO plan_items(plan_id, seq, file_id, sha256, size, mtime_us, keeper_file_id) "
                         "VALUES(?,?,?,?,?,?,?)",
                         [(plan.plan_id, n, i.file_id, i.sha256, i.size, i.mtime_us, i.keeper_file_id)
                          for n, i in enumerate(plan.items, 1)])
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    lines = [f"Plan {plan.plan_id} ({plan.created_at}), {len(plan.items)} files, {plan.bytes} bytes", ""]
    for i in plan.items:
        lines.append(f"MOVE  {roots.get(i.root_id, i.root_id)}:{i.relpath}\n      same as {roots.get(i.keeper_root_id, i.keeper_root_id)}:{i.keeper_relpath}")
    for rid, rel in plan.blocked:
        lines.append(f"STAY  {roots.get(rid, rid)}:{rel}  (last copy)")
    for sha, rid, rel in plan.conflicts:
        lines.append(f"KEPT  {roots.get(rid, rid)}:{rel}  (every copy was marked remove)")
    for rid, rel, why in plan.skipped:
        lines.append(f"SKIP  {roots.get(rid, rid)}:{rel}  ({why})")
    p = pathlib.Path(state_dir) / "plans" / f"{plan.plan_id}.txt"
    atomic_write_text(p, "\n".join(lines) + "\n")
    return p


def latest_saved(conn: sqlite3.Connection) -> tuple[str, str, set[tuple]] | None:
    r = conn.execute("SELECT plan_id, decisions_digest FROM plans ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchone()
    if r is None:
        return None
    items = {(x[0], x[1], x[2], x[3], x[4]) for x in conn.execute(
        "SELECT file_id, sha256, size, mtime_us, keeper_file_id FROM plan_items WHERE plan_id = ?", (r[0],))}
    return r[0], r[1], items


def top_folders(plan: Plan, n: int = 10) -> list[tuple[str, str, int, int]]:
    agg: dict[tuple[str, str], list[int]] = collections.defaultdict(lambda: [0, 0])
    for i in plan.items:
        folder = i.relpath.rsplit("/", 1)[0] if "/" in i.relpath else ""
        agg[(i.root_id, folder)][0] += 1
        agg[(i.root_id, folder)][1] += i.size
    return sorted(((r, f, c, b) for (r, f), (c, b) in agg.items()), key=lambda x: -x[3])[:n]
