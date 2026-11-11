"""Apply, recovery, verify, tidy (docs/08). The only code that changes a
library root, and then only by moving files into its PhotoHaven-DELETE
folder (D-10; "quarantine" in the spec's record names).

Invariants (08 §1): no deletes in a root except the last step of a
verified cross-volume move (I-1); a keeper stays for every move (I-2,
planned and re-checked here); keepers are never in any quarantine log
(I-3); nothing moves unless it still matches the plan (I-4); every move is
in the batch manifest (I-6).
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import pathlib
import shutil
import sqlite3
import stat as stat_mod

from . import plan as planner, quarantine
from .config import ConfigError, Library
from .formats import DELETE_FOLDER, DELETE_README, append_jsonl, manifest_header
from .util import (is_inside, is_placeholder, long_path, name_key, new_batch_id, rel_to_path, stat_mtime_us,
                   utc_now)

CHUNK = 1 << 20
CRASH_ENV = "PHOTOHAVEN_CRASH_AT"          # test hook: crash-injection points (10 §4)
FORCE_COPY_ENV = "PHOTOHAVEN_FORCE_COPY"   # test hook: use the cross-volume protocol on one volume


class ApplyError(Exception):
    pass


class RecoveryError(ApplyError):
    """08 §5 'should be impossible' row: stop and report loudly."""


def crash_point(name: str) -> None:
    if os.environ.get(CRASH_ENV) == name:
        os._exit(77)


README_TEXT = """\
Photo Haven moved duplicate photos and videos here.

Every file in this folder had a byte-identical copy that was kept in your
library when it was moved, and the move was verified. The kept copy's path
is recorded in each batch's manifest.jsonl.

  * To free space, delete this folder's contents (or run `photohaven purge`,
    which compares every file with its kept copy once more before deleting).
  * To put files back, run `photohaven restore --batch <folder name>`.

Photo Haven never scans this folder as part of your library.
"""


@dataclasses.dataclass
class ApplyResult:
    batch_id: str | None
    batch_ids: list[str] = dataclasses.field(default_factory=list)
    plan_id: str | None = None
    moved: int = 0
    moved_bytes: int = 0
    skipped: list[tuple[str, str]] = dataclasses.field(default_factory=list)   # (path, reason)
    recovered: list[str] = dataclasses.field(default_factory=list)
    verified: int = 0
    verify_failed: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    remaining: int = 0


def _sha256(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def _copy_hashing(src: str, dst: str) -> tuple[str, int]:
    """Copy src to dst computing SHA-256; flush and fsync; copy timestamps."""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    h = hashlib.sha256()
    n = 0
    with open(src, "rb") as fi, open(dst, "xb") as fo:
        while chunk := fi.read(CHUNK):
            h.update(chunk)
            fo.write(chunk)
            n += len(chunk)
        fo.flush()
        os.fsync(fo.fileno())
    shutil.copystat(src, dst)
    return h.hexdigest(), n


def _complete_move(src: str) -> None:
    """Final step of a cross-volume move (08 §4 step 5): the verified copy is
    already in quarantine. The one place apply removes a file from a root."""
    try:
        os.remove(src)
    except PermissionError:
        os.chmod(src, stat_mod.S_IWRITE)      # read-only files can't be removed on Windows
        os.remove(src)


class Applier:
    def __init__(self, env):
        self.env = env
        self.conn: sqlite3.Connection = env.conn
        self.library: Library = env.library
        self.roots = {rid: p for rid, p in env.mcfg.roots.items()}
        self.override = env.mcfg.quarantine_root or None
        self.journal = quarantine.Journal(env.project_home, self.library.library_id, env.machine.machine_id)

    # ------------------------------------------------------------- checks
    def check_machine(self) -> None:
        if self.library.apply_machine_id != self.env.machine.machine_id:
            raise ApplyError(f"this machine ({self.env.machine.name}) is not the library's apply machine; only "
                             f"{self.library.apply_machine_id} may apply, restore or purge (D-17)")

    def holding(self, root_id: str) -> pathlib.Path:
        """The delete folder for a root: `<root>/PhotoHaven-DELETE`, or the
        machine-wide override (D-10)."""
        base = self.roots.get(root_id)
        if not base:
            raise ApplyError(f"root {root_id} is not mapped on this machine")
        return planner.holding_root(base, self.override)

    def check_override(self) -> None:
        if not self.override:
            return
        for r in self.library.roots:
            p = self.roots.get(r.root_id)
            if p and (is_inside(self.override, p) or is_inside(p, self.override)):
                raise ConfigError(f"quarantine_root {self.override} overlaps root {r.name} ({p}); leave it unset "
                                  f"to use each root's {DELETE_FOLDER} folder")
        if is_inside(self.override, self.env.project_home):
            raise ConfigError(f"quarantine_root {self.override} is inside the project home")



    def abs_path(self, root_id: str, relpath: str) -> str:
        base = self.roots.get(root_id)
        if not base:
            name = next((r.name for r in self.library.roots if r.root_id == root_id), root_id)
            raise ApplyError(f"root {name} is not mapped on this machine")
        return long_path(rel_to_path(base, relpath))

    def qpath_abs(self, qroot: str | pathlib.Path, qpath: str) -> str:
        return long_path(rel_to_path(qroot, qpath))

    # ------------------------------------------------------------- recover
    def recover(self) -> list[str]:
        """Reconcile items left `moving` by an interrupted run (08 §5)."""
        msgs: list[str] = []
        rows = self.conn.execute(
            "SELECT q.*, b.quarantine_root, b.plan_id FROM quarantine_items q JOIN batches b USING(batch_id) "
            "WHERE q.state = 'moving'").fetchall()
        for r in rows:
            src = self.abs_path(r["root_id"], r["relpath"])
            dst = self.qpath_abs(r["quarantine_root"], r["qpath"])
            partial = dst + ".partial"
            src_ok = os.path.exists(src)
            dst_ok = os.path.exists(dst)
            dst_good = dst_ok and _sha256(dst) == (r["sha256"], r["size"])
            label = f"{r['relpath']} (batch {r['batch_id']})"
            if os.path.exists(partial):
                os.remove(partial)                  # Photo Haven's own temp file in quarantine
            if src_ok and not dst_ok:
                self._forget(r)
                msgs.append(f"{label}: interrupted before the copy finished; back to planned")
            elif src_ok and dst_good:
                if _sha256(src) != (r["sha256"], r["size"]):
                    raise RecoveryError(f"{label}: source changed while a move was in progress; the quarantine "
                                        f"copy at {dst} holds the planned bytes. Stopping; check by hand.")
                _complete_move(src)
                self._journal_moved(r)
                msgs.append(f"{label}: finished an interrupted move")
            elif not src_ok and dst_good:
                self._journal_moved(r)
                msgs.append(f"{label}: move had completed; journaled it")
            elif src_ok and dst_ok:
                bad = dst + ".bad"
                os.replace(dst, bad)
                self._forget(r)
                msgs.append(f"{label}: quarantine copy did not match; set aside as {bad}; back to planned")
            else:
                raise RecoveryError(
                    f"{label}: neither the original ({src}) nor the quarantine copy ({dst}) exists. This should be "
                    f"impossible and indicates a bug. The keeper {r['keeper_relpath']} holds the same bytes "
                    f"(sha256 {r['sha256']}), so no content is lost. Stopping.")
        return msgs

    def _forget(self, r) -> None:
        self.conn.execute("DELETE FROM quarantine_items WHERE batch_id = ? AND root_id = ? AND relpath = ?",
                          (r["batch_id"], r["root_id"], r["relpath"]))

    def _journal_moved(self, r) -> None:
        """Manifest item, then log event, then DB; each step idempotent so a
        crash between them is finished by the next run."""
        batch_dir = rel_to_path(r["quarantine_root"], r["batch_id"])
        manifest = batch_dir / "manifest.jsonl"
        ev = self.journal.has(r["batch_id"], r["root_id"], r["relpath"], "moved")
        if ev is None:
            ev = self.journal.new_event(batch_id=r["batch_id"], event="moved", root_id=r["root_id"],
                                        path=r["relpath"], sha256=r["sha256"], size=r["size"],
                                        keeper_root_id=r["keeper_root_id"], keeper_path=r["keeper_relpath"],
                                        qpath=r["qpath"])
        if not any(i.root_id == r["root_id"] and i.path == r["relpath"] for i in quarantine.manifest_items(manifest)):
            append_jsonl(manifest, manifest_header(r["batch_id"], self.library.library_id,
                                                   self.env.machine.machine_id, r["plan_id"] or "", utc_now()),
                         [ev.to_record("item")])
        if not self.journal.has(r["batch_id"], r["root_id"], r["relpath"], "moved"):
            self.journal.append(ev)
        self.conn.execute("BEGIN")
        self.conn.execute("UPDATE quarantine_items SET state = 'moved', moved_at = ? WHERE batch_id = ? AND "
                          "root_id = ? AND relpath = ?", (ev.at, r["batch_id"], r["root_id"], r["relpath"]))
        self.conn.execute("UPDATE files SET state = 'quarantined' WHERE root_id = ? AND relpath = ?",
                          (r["root_id"], r["relpath"]))
        self.conn.execute("COMMIT")

    # --------------------------------------------------------------- apply
    def apply(self, *, batch_limit: int | None = None, progress=None) -> ApplyResult:
        progress = progress or (lambda s: None)
        self.check_machine()
        self.check_override()
        res = ApplyResult(None)
        res.recovered = self.recover()
        saved = planner.latest_saved(self.conn)
        if saved is None:
            raise ApplyError("no saved plan: run 'photohaven plan' and review it first")
        plan = planner.make_plan(self.conn, self.library, self.env.project_home, self.roots, self.override)
        saved_id, saved_digest, saved_items = saved
        if plan.digest != saved_digest or not {i.key() for i in plan.items} <= saved_items:
            raise ApplyError("the plan has changed since you last reviewed it (decisions or files changed). "
                             "Run 'photohaven plan' again and review it.")
        res.plan_id = saved_id
        items = plan.items[:batch_limit] if batch_limit else plan.items
        res.remaining = len(plan.items) - len(items)
        if not items:
            return res
        force_copy = os.environ.get(FORCE_COPY_ENV) == "1"
        need: dict[str, int] = {}
        for i in items:
            q = self.holding(i.root_id)
            if force_copy or not planner.same_volume(self.roots[i.root_id], q):
                need[str(q)] = need.get(str(q), 0) + i.size
        for q, n in need.items():
            free = shutil.disk_usage(planner._existing(q)).free
            if n > free:
                raise ApplyError(f"{q} has {free:,} bytes free; this run needs {n:,}. Free space, or use --batch-limit.")
        out = quarantine.out_of_library(quarantine.read_all(self.env.project_home, self.library.library_id)[0])
        batches: dict[str, tuple[str, pathlib.Path]] = {}      # one batch per delete folder
        for n, it in enumerate(items, 1):
            q = self.holding(it.root_id)
            if str(q) not in batches:
                batches[str(q)] = (self._new_batch(q, saved_id), q)
            batch_id, qroot = batches[str(q)]
            progress(f"moving {n}/{len(items)}: {it.relpath}")
            reason = self._move_one(it, batch_id, qroot, force_copy, out)
            if reason:
                res.skipped.append((it.relpath, reason))
            else:
                res.moved += 1
                res.moved_bytes += it.size
        res.batch_ids = [b for b, _ in batches.values()]
        res.batch_id = res.batch_ids[0] if res.batch_ids else None
        for b in res.batch_ids:
            self.conn.execute("UPDATE batches SET state = 'applied' WHERE batch_id = ?", (b,))
            ok, failed = self.verify(b)
            res.verified += ok
            res.verify_failed += failed
        return res

    def _new_batch(self, qroot: pathlib.Path, plan_id: str) -> str:
        self.ensure_holding_at(qroot)
        while True:
            batch_id = new_batch_id()
            if not self.conn.execute("SELECT 1 FROM batches WHERE batch_id = ?", (batch_id,)).fetchone():
                break
        manifest = qroot / batch_id / "manifest.jsonl"
        os.makedirs(long_path(manifest.parent), exist_ok=True)
        append_jsonl(manifest, manifest_header(batch_id, self.library.library_id, self.env.machine.machine_id,
                                               plan_id, utc_now()), [])
        self.conn.execute("INSERT INTO batches(batch_id, plan_id, machine_id, quarantine_root, created_at, state) "
                          "VALUES(?,?,?,?,?, 'applying')",
                          (batch_id, plan_id, self.env.machine.machine_id, str(qroot), utc_now()))
        return batch_id

    def ensure_holding_at(self, q: pathlib.Path) -> None:
        os.makedirs(long_path(q), exist_ok=True)
        readme = q / DELETE_README
        if not os.path.exists(long_path(readme)):
            with open(long_path(readme), "w", encoding="utf-8", newline="\r\n") as f:
                f.write(README_TEXT)

    def _move_one(self, it: planner.Item, batch_id: str, qroot: pathlib.Path, force_copy: bool,
                  out: dict) -> str | None:
        """Returns None when moved, else why it was skipped (08 §4)."""
        src = self.abs_path(it.root_id, it.relpath)
        keeper = self.abs_path(it.keeper_root_id, it.keeper_relpath)
        # pre-move checks: skip and report, never "do it anyway" (I-4)
        try:
            st = os.stat(src)
        except FileNotFoundError:
            return "source is gone"
        if is_placeholder(st) and (force_copy or not planner.same_volume(src, qroot)):
            return "source is cloud-only"
        if st.st_size != it.size or stat_mtime_us(st) != it.mtime_us:
            return "source changed since the plan (size or mtime)"
        try:
            kst = os.stat(keeper)
        except FileNotFoundError:
            return f"keeper {it.keeper_relpath} is gone"
        if kst.st_size != it.keeper_size or stat_mtime_us(kst) != it.keeper_mtime_us:
            return f"keeper {it.keeper_relpath} changed since the plan"
        if it.sha256 in out.get((it.keeper_root_id, name_key(it.keeper_relpath)), set()):
            return f"keeper {it.keeper_relpath} is listed as moved in a quarantine log"
        qpath = f"{batch_id}/files/{it.root_id}/{it.relpath}"
        dst = self.qpath_abs(qroot, qpath)
        if os.path.exists(dst) or os.path.exists(dst + ".partial"):
            return "quarantine destination already exists"
        # journal 'moving' before touching the filesystem (08 §5)
        self.conn.execute("INSERT INTO quarantine_items(batch_id, root_id, relpath, sha256, size, keeper_root_id, "
                          "keeper_relpath, qpath, state) VALUES(?,?,?,?,?,?,?,?, 'moving')",
                          (batch_id, it.root_id, it.relpath, it.sha256, it.size, it.keeper_root_id,
                           it.keeper_relpath, qpath))
        crash_point("after_moving_commit")
        row = self.conn.execute("SELECT q.*, b.quarantine_root, b.plan_id FROM quarantine_items q JOIN batches b "
                                "USING(batch_id) WHERE batch_id = ? AND root_id = ? AND relpath = ?",
                                (batch_id, it.root_id, it.relpath)).fetchone()
        try:
            reason = self._move_files(it, row, src, dst, st, qroot, force_copy)
        except OSError as e:
            # a locked or unreadable file: reconcile this item now (08 §5) and carry on
            self.recover()
            return f"could not move ({e.strerror or e})"
        if reason:
            return reason
        crash_point("before_journal")
        self._journal_moved(row)
        return None

    def _move_files(self, it, row, src: str, dst: str, st: os.stat_result, qroot: pathlib.Path,
                    force_copy: bool) -> str | None:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if not force_copy and planner.same_volume(src, qroot):
            # same volume: hash first (rule 6: the action carries its expectation), then an atomic rename
            if _sha256(src) != (it.sha256, it.size):
                self._forget(row)
                return "source bytes changed since the plan"
            os.rename(src, dst)
            crash_point("after_rename")
        else:
            partial = dst + ".partial"
            digest = _copy_hashing(src, partial)
            crash_point("after_partial_write")
            if digest != (it.sha256, it.size):
                os.remove(partial)
                self._forget(row)
                return "the copy did not match the planned sha256"
            os.rename(partial, dst)
            crash_point("after_rename_partial")
            st2 = os.stat(src)
            if (st2.st_size, st2.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
                os.remove(dst)                  # our own verified copy; the source changed under us
                self._forget(row)
                return "source changed during the copy"
            _complete_move(src)
            crash_point("after_source_delete")
        return None

    def note_hand_deleted(self, batch_id: str | None = None) -> int:
        """Verified files that someone deleted from the delete folder by hand
        (which D-10 allows) are logged as purged, once."""
        q = ("SELECT q.*, b.quarantine_root FROM quarantine_items q JOIN batches b USING(batch_id) "
             "WHERE q.state = 'verified'" + (" AND q.batch_id = ?" if batch_id else ""))
        n = 0
        for r in self.conn.execute(q, (batch_id,) if batch_id else ()).fetchall():
            if os.path.exists(self.qpath_abs(r["quarantine_root"], r["qpath"])):
                continue
            ev = self.journal.new_event(batch_id=r["batch_id"], event="purged", root_id=r["root_id"], path=r["relpath"],
                                        sha256=r["sha256"], size=r["size"], keeper_root_id=r["keeper_root_id"],
                                        keeper_path=r["keeper_relpath"], qpath=r["qpath"],
                                        detail="deleted outside Photo Haven")
            self.journal.append(ev)
            self.conn.execute("UPDATE quarantine_items SET state = 'purged', purged_at = ? WHERE batch_id = ? AND "
                              "root_id = ? AND relpath = ?", (ev.at, r["batch_id"], r["root_id"], r["relpath"]))
            n += 1
        return n

    # --------------------------------------------------------------- verify
    def verify(self, batch_id: str | None = None, *, deep: bool = False) -> tuple[int, list[tuple[str, str]]]:
        """08 §6: every moved file re-hashes to its sha256, and every keeper
        still exists with the size and mtime it had at plan time. `deep`
        also re-hashes the keepers, and re-checks items verified before."""
        self.note_hand_deleted(batch_id)
        states = "('moved', 'verified', 'verify_failed')" if deep else "('moved')"
        q = ("SELECT q.*, b.quarantine_root FROM quarantine_items q JOIN batches b USING(batch_id) "
             f"WHERE q.state IN {states}" + (" AND q.batch_id = ?" if batch_id else ""))
        rows = self.conn.execute(q, (batch_id,) if batch_id else ()).fetchall()
        ok, failed = 0, []
        for r in rows:
            detail = None
            dst = self.qpath_abs(r["quarantine_root"], r["qpath"])
            if not os.path.exists(dst):
                detail = "quarantined file is missing"
            elif _sha256(dst) != (r["sha256"], r["size"]):
                detail = "quarantined file no longer matches its sha256"
            else:
                k = self.conn.execute("SELECT size, mtime_us FROM files WHERE root_id = ? AND relpath = ?",
                                      (r["keeper_root_id"], r["keeper_relpath"])).fetchone()
                try:
                    kpath = self.abs_path(r["keeper_root_id"], r["keeper_relpath"])
                    kst = os.stat(kpath)
                    if k is None or kst.st_size != k["size"] or stat_mtime_us(kst) != k["mtime_us"]:
                        detail = "keeper changed since the plan"
                    elif deep and _sha256(kpath) != (r["sha256"], r["size"]):
                        detail = "keeper no longer has the same bytes"
                except FileNotFoundError:
                    detail = "keeper is missing"
            event = "verify_failed" if detail else "verified"
            ev = self.journal.new_event(batch_id=r["batch_id"], event=event, root_id=r["root_id"], path=r["relpath"],
                                        sha256=r["sha256"], size=r["size"], keeper_root_id=r["keeper_root_id"],
                                        keeper_path=r["keeper_relpath"], qpath=r["qpath"], detail=detail)
            self.journal.append(ev)
            self.conn.execute("UPDATE quarantine_items SET state = ?, verified_at = ?, last_error = ? WHERE "
                              "batch_id = ? AND root_id = ? AND relpath = ?",
                              (event, ev.at, detail, r["batch_id"], r["root_id"], r["relpath"]))
            if detail:
                failed.append((r["relpath"], detail))
            else:
                ok += 1
        for b in {r["batch_id"] for r in rows}:
            bad = self.conn.execute("SELECT count(*) FROM quarantine_items WHERE batch_id = ? AND state NOT IN "
                                    "('verified', 'purged')", (b,)).fetchone()[0]
            if not bad:
                self.conn.execute("UPDATE batches SET state = 'verified' WHERE batch_id = ?", (b,))
        return ok, failed


# ------------------------------------------------------------------ purge

@dataclasses.dataclass
class PurgeResult:
    batch_id: str
    executed: bool
    deleted: list[tuple[str, int]] = dataclasses.field(default_factory=list)       # (path, size)
    would_delete: list[tuple[str, int]] = dataclasses.field(default_factory=list)
    refused: list[tuple[str, str]] = dataclasses.field(default_factory=list)       # (path, why)
    cloud_keepers: int = 0

    @property
    def bytes(self) -> int:
        return sum(s for _, s in (self.deleted or self.would_delete))


def same_bytes(a: str, b: str) -> bool:
    """08 §7 step 2: stream both files and compare every byte. Raises OSError
    if either can't be read (which refuses the purge)."""
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            x, y = fa.read(CHUNK), fb.read(CHUNK)
            if x != y:
                return False
            if not x:
                return True


def _purge_file(path: str) -> None:
    """The only removal of a photo anywhere in Photo Haven (08 §7 step 3):
    a file in a DELETE folder that was just compared byte for byte with a
    readable kept copy."""
    try:
        os.remove(path)
    except PermissionError:
        os.chmod(path, stat_mod.S_IWRITE)
        os.remove(path)


def purge(applier: "Applier", batch_id: str, *, execute: bool, min_age_days: float = 14) -> PurgeResult:
    import datetime
    from .util import parse_ts
    a = applier
    a.check_machine()
    b = a.conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
    if b is None:
        raise ApplyError(f"no batch {batch_id} on this machine ('photohaven batches list')")
    a.note_hand_deleted(batch_id)
    rows = a.conn.execute("SELECT * FROM quarantine_items WHERE batch_id = ? ORDER BY relpath", (batch_id,)).fetchall()
    open_states = [r for r in rows if r["state"] not in ("verified", "purged", "purge_refused", "restored")]
    if open_states:
        raise ApplyError(f"batch {batch_id} has {len(open_states)} item(s) that are not verified "
                         f"(e.g. {open_states[0]['relpath']}: {open_states[0]['state']}); run 'photohaven verify' first")
    age = datetime.datetime.now(datetime.timezone.utc) - parse_ts(b["created_at"])
    if age < datetime.timedelta(days=min_age_days):
        days = age.total_seconds() / 86400
        raise ApplyError(f"batch {batch_id} is {days:.1f} days old; the hold period is {min_age_days:g} days "
                         f"(--min-age to change it)")
    res = PurgeResult(batch_id, execute)
    out = quarantine.out_of_library(quarantine.read_all(a.env.project_home, a.library.library_id)[0])
    for r in rows:
        if r["state"] not in ("verified", "purge_refused"):
            continue
        qfile = a.qpath_abs(b["quarantine_root"], r["qpath"])
        why = None
        try:
            keeper = a.abs_path(r["keeper_root_id"], r["keeper_relpath"])
        except ApplyError as e:
            keeper, why = None, str(e)
        if why is None:
            if not os.path.exists(keeper):
                why = "the kept copy is missing"
            elif r["sha256"] in out.get((r["keeper_root_id"], name_key(r["keeper_relpath"])), set()):
                why = "the kept copy is listed as moved in a quarantine log"
            elif not os.path.exists(qfile):
                why = "the file is no longer in the DELETE folder"
            else:
                try:
                    if is_placeholder(os.stat(keeper)):
                        res.cloud_keepers += 1          # comparing will download it
                    if not same_bytes(qfile, keeper):
                        why = "the kept copy no longer has the same bytes"
                except OSError as e:
                    why = f"could not read for the compare ({e.strerror or e})"
        if why:
            res.refused.append((r["relpath"], why))
            if execute:
                ev = a.journal.new_event(batch_id=batch_id, event="purge_refused", root_id=r["root_id"],
                                         path=r["relpath"], sha256=r["sha256"], size=r["size"],
                                         keeper_root_id=r["keeper_root_id"], keeper_path=r["keeper_relpath"],
                                         qpath=r["qpath"], detail=why)
                a.journal.append(ev)
                a.conn.execute("UPDATE quarantine_items SET state = 'purge_refused', last_error = ? WHERE batch_id = ? "
                               "AND root_id = ? AND relpath = ?", (why, batch_id, r["root_id"], r["relpath"]))
            continue
        if not execute:
            res.would_delete.append((r["relpath"], r["size"]))
            continue
        _purge_file(qfile)
        ev = a.journal.new_event(batch_id=batch_id, event="purged", root_id=r["root_id"], path=r["relpath"],
                                 sha256=r["sha256"], size=r["size"], keeper_root_id=r["keeper_root_id"],
                                 keeper_path=r["keeper_relpath"], qpath=r["qpath"])
        a.journal.append(ev)
        a.conn.execute("UPDATE quarantine_items SET state = 'purged', purged_at = ?, last_error = NULL WHERE "
                       "batch_id = ? AND root_id = ? AND relpath = ?", (ev.at, batch_id, r["root_id"], r["relpath"]))
        a.conn.execute("UPDATE files SET state = 'purged' WHERE root_id = ? AND relpath = ? AND state = 'quarantined'",
                       (r["root_id"], r["relpath"]))
        res.deleted.append((r["relpath"], r["size"]))
    if execute:
        left = a.conn.execute("SELECT count(*) FROM quarantine_items WHERE batch_id = ? AND state NOT IN "
                              "('purged', 'restored')", (batch_id,)).fetchone()[0]
        a.conn.execute("UPDATE batches SET state = ? WHERE batch_id = ?",
                       ("purged" if not left else "partially_purged", batch_id))
        _remove_empty_dirs(pathlib.Path(b["quarantine_root"]) / batch_id / "files")
    return res


def _remove_empty_dirs(top: pathlib.Path) -> None:
    """Empty folders left inside a batch's files/ tree (Photo Haven's own).
    The manifest is kept as a permanent record (08 §7)."""
    t = long_path(top)
    if not os.path.isdir(t):
        return
    for dirpath, dirnames, filenames in os.walk(t, topdown=False):
        if not filenames and not os.listdir(dirpath):
            os.rmdir(dirpath)


# ------------------------------------------------------------------- tidy

def empty_folders(env) -> list[tuple[str, str, list[str]]]:
    """Folders (never a root) holding nothing but Photo Haven cache files,
    bottom-up, as (root_id, abs_path, cache files)."""
    out = []
    for r in env.library.roots:
        base = env.mcfg.roots.get(r.root_id)
        if not base:
            continue
        removable: set[str] = set()
        holding = os.path.normcase(os.path.join(long_path(base), DELETE_FOLDER))
        for dirpath, dirnames, filenames in os.walk(long_path(base), topdown=False):
            if os.path.normcase(dirpath) == os.path.normcase(long_path(base)):
                continue
            if os.path.normcase(dirpath) == holding or os.path.normcase(dirpath).startswith(holding + os.sep):
                continue
            subdirs_ok = all(os.path.join(dirpath, d) in removable for d in dirnames)
            only_cache = all(f.startswith(".photohaven-cache.") and f.endswith(".jsonl") for f in filenames)
            if subdirs_ok and only_cache:
                removable.add(dirpath)
                out.append((r.root_id, dirpath, filenames))
    return out


def tidy(env, *, execute: bool) -> list[str]:
    done = []
    for root_id, d, caches in empty_folders(env):
        if execute:
            for f in caches:
                os.remove(os.path.join(d, f))   # Photo Haven's own cache files only
            os.rmdir(d)                          # fails (safely) if anything appeared meanwhile
        done.append(d)
    return done
