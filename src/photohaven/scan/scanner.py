"""The scanner (docs/04): folders of files -> facts.

Stages per root: verify the root marker, walk (discover + reuse cached
facts), hash in two passes (candidates, then all), describe each new unique
SHA-256 once with ExifTool, persist per folder (cache file first, then the
DB commit). Read-only with respect to photos: the only things written into a
root are this machine's cache files (and the root marker with --adopt-root).
"""
from __future__ import annotations

import collections
import concurrent.futures as cf
import dataclasses
import hashlib
import os
import pathlib
import random
import sqlite3
import sys
import time
from typing import Callable, Protocol

from .. import TOOL, db
from ..config import Library, LibrarySettings, MachineConfig, folder_flag
from ..formats import (DELETE_FOLDER, OWN_FILE_PREFIX, ROOT_MARKER, CacheFile, CacheHeader, FileFacts, FormatError,
                       RootMarker, cache_bytes, cache_file_name, facts_equal, is_cache_file_name, read_cache)
from ..identity import Machine
from ..util import (IS_WINDOWS, atomic_write_bytes, file_attributes, FILE_ATTRIBUTE_REPARSE_POINT, is_inside,
                    is_placeholder, join_rel, long_path, name_key, rel_to_path, sort_key, stat_ctime_us,
                    stat_mtime_us, utc_now)
from .metadata import PH_META, classify, normalize, stale

CHUNK = 1 << 20
DESCRIBE_BATCH = 50
DESCRIBED_MEDIA = ("image", "raw", "video")
SCANNER_FLAGS = ("damaged", "suspect", "exiftool_error", "path_key_collision", "cache_conflict", "unreadable",
                 "zero_bytes")


class Describer(Protocol):
    version: str

    def describe(self, paths: list[str]) -> list[dict | None]: ...


@dataclasses.dataclass
class ScanOptions:
    roots: list[str] | None = None        # names or ids; None = every root mapped on this machine
    folder: str | None = None             # limit to this relpath subtree (needs exactly one root)
    no_folder_cache: bool = False         # write caches under local state (04 "First-run read-only mode")
    hash_scope: str | None = None         # override library.toml
    rehash: bool = False                  # ignore cached facts
    audit: int = 0                        # rehash N random reused files and compare
    adopt_root: bool = False              # write a missing root marker
    hashing: bool = True                  # False: reuse facts only (rebuild-db)
    hash_threads: int | None = None


@dataclasses.dataclass
class Entry:
    name: str
    path: str                 # long-path form, for Python I/O
    size: int
    mtime_us: int
    ctime_us: int | None
    media: str
    placeholder: bool
    facts: FileFacts | None = None
    source_machine: str | None = None
    flags: dict = dataclasses.field(default_factory=dict)
    queued: bool = False
    audit_sha: str | None = None
    vanished: bool = False


@dataclasses.dataclass
class Folder:
    root_id: str
    relpath: str
    path: str
    entries: list[Entry]
    own_cache: CacheFile | None
    is_generic: bool
    is_intake: bool
    is_protected: bool


@dataclasses.dataclass
class ScanResult:
    scan_id: int
    status: str = "completed"
    folders_seen: int = 0
    files_seen: int = 0
    files_hashed: int = 0
    bytes_hashed: int = 0
    reused: int = 0
    described: int = 0
    cache_files_written: int = 0
    cloud_only: int = 0
    missing: int = 0
    pending: int = 0
    errors: int = 0
    audit_mismatches: int = 0
    warnings: list[str] = dataclasses.field(default_factory=list)
    failed_roots: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class _Hashed:
    sha256: str | None = None
    size: int = 0
    mtime_us: int = 0
    ctime_us: int | None = None
    error: str | None = None      # locked | unreadable | changing | vanished
    detail: str | None = None


def hash_file(path: str) -> _Hashed:
    """SHA-256 with an fstat before and a stat after; a file that changes
    while being read is retried once, then reported as changing (04 stage 3)."""
    for attempt in range(2):
        try:
            f = open(path, "rb")
        except FileNotFoundError:
            return _Hashed(error="vanished")
        except PermissionError as e:
            if getattr(e, "winerror", None) in (32, 33) and attempt == 0:   # sharing / lock violation
                time.sleep(0.5)
                continue
            return _Hashed(error="locked" if getattr(e, "winerror", None) in (32, 33) else "unreadable",
                           detail=str(e))
        except OSError as e:
            return _Hashed(error="unreadable", detail=str(e))
        try:
            with f:
                st0 = os.fstat(f.fileno())
                if is_placeholder(st0):
                    # it became cloud-only after the walk: reading would download it (rule 4)
                    return _Hashed(error="cloud_only", detail="became cloud-only during the scan")
                h = hashlib.sha256()
                n = 0
                while chunk := f.read(CHUNK):
                    h.update(chunk)
                    n += len(chunk)
            st1 = os.stat(path)
        except OSError as e:
            return _Hashed(error="unreadable", detail=str(e))
        if n == st0.st_size and (st0.st_size, st0.st_mtime_ns) == (st1.st_size, st1.st_mtime_ns):
            return _Hashed(h.hexdigest(), st0.st_size, stat_mtime_us(st0), stat_ctime_us(st0))
    return _Hashed(error="changing", detail="file changed while it was being read")


class Scanner:
    def __init__(self, *, conn: sqlite3.Connection, state_dir: pathlib.Path, project_home: pathlib.Path,
                 library: Library, settings: LibrarySettings, mcfg: MachineConfig, machine: Machine,
                 describer: Describer | None, progress: Callable[[str], None] | None = None):
        self.conn, self.state_dir, self.project_home = conn, state_dir, project_home
        self.library, self.settings, self.mcfg, self.machine = library, settings, mcfg, machine
        self.describer = describer
        self.progress = progress or (lambda s: None)
        self.known: dict[str, dict] = {}   # sha256 -> content facts (03 §3.2 content fields)

    # ------------------------------------------------------------------ run
    def run(self, opts: ScanOptions) -> ScanResult:
        roots = self._select_roots(opts)
        scan_id = db.start_scan(self.conn, self.machine.machine_id, {
            k: v for k, v in dataclasses.asdict(opts).items() if v not in (None, False, 0)})
        res = ScanResult(scan_id)
        self._res = res
        self._scan_id = scan_id
        self._now = utc_now()
        db.upsert_machine(self.conn, self.machine.machine_id, self.machine.short, self.machine.name, self._now)
        for r in self.library.roots:
            db.upsert_root(self.conn, r.root_id, r.name, r.kind, self.mcfg.roots.get(r.root_id))
        threads = opts.hash_threads or self.mcfg.hash_threads or 4
        try:
            with cf.ThreadPoolExecutor(max_workers=max(1, threads), thread_name_prefix="hash") as pool:
                self._pool = pool
                for root in roots:
                    self._scan_root(root, opts)
        except KeyboardInterrupt:
            res.status = "interrupted"
            raise
        except BaseException:
            res.status = "failed"
            raise
        finally:
            if self.conn.in_transaction:
                self.conn.rollback()
            if res.status == "completed" and res.failed_roots:
                res.status = "failed" if len(res.failed_roots) == len(roots) else "completed"
            db.finish_scan(self.conn, scan_id, res.status, folders_seen=res.folders_seen,
                           files_seen=res.files_seen, files_hashed=res.files_hashed,
                           bytes_hashed=res.bytes_hashed, errors=res.errors)
        return res

    def _select_roots(self, opts: ScanOptions):
        if opts.roots:
            roots = [self.library.root(k) for k in opts.roots]
        else:
            roots = [r for r in self.library.roots if r.root_id in self.mcfg.roots]
        if opts.folder is not None and len(roots) != 1:
            raise ValueError("--folder needs exactly one --root")
        return roots

    def _warn(self, msg: str) -> None:
        self._res.warnings.append(msg)
        self.progress("warning: " + msg)

    # ----------------------------------------------------------------- root
    def _scan_root(self, root, opts: ScanOptions) -> None:
        res = self._res
        local = self.mcfg.roots.get(root.root_id)
        if not local:
            self._warn(f"root {root.name} is not mapped on this machine (photohaven root map)")
            res.failed_roots.append(root.name)
            return
        base = pathlib.Path(local)
        if not os.path.isdir(long_path(base)):
            self._warn(f"root {root.name}: {base} is not a folder")
            res.failed_roots.append(root.name)
            return
        if not self._check_marker(root, base, opts):
            res.failed_roots.append(root.name)
            return
        start_rel = (opts.folder or "").strip("/")
        folders, unlisted = self._walk(root, base, start_rel, opts)
        res.folders_seen += len(folders)

        # hash scope: candidates first (sizes shared anywhere in the library), then the rest
        sizes = collections.Counter(e.size for f in folders for e in f.entries if e.media != "other")
        others = self.conn.execute(
            "SELECT size, count(*) FROM files WHERE root_id != ? AND state IN ('present','cloud_only','unreadable') "
            "GROUP BY size", (root.root_id,)).fetchall()
        for size, n in others:
            sizes[size] += n
        scope = opts.hash_scope or self.settings.hash_scope
        pending = [e for f in folders for e in f.entries if e.queued]
        if opts.audit and opts.hashing:
            reused = [e for f in folders for e in f.entries if e.facts and e.facts.sha256 and not e.placeholder
                      and not e.queued]
            for e in random.sample(reused, min(opts.audit, len(reused))):
                e.audit_sha, e.queued = e.facts.sha256, True
        pass1 = {id(e) for e in pending if sizes[e.size] > 1} | {
            id(e) for f in folders for e in f.entries if e.audit_sha}
        passes = [pass1]
        if scope == "all":
            passes.append({id(e) for e in pending if id(e) not in pass1})
        for n, ids in enumerate(passes):
            only_touched = n > 0
            self._run_pass(folders, ids, opts, only_touched)
        for f in folders:
            res.pending += sum(1 for e in f.entries if e.facts is None and e.media != "other"
                               and not e.vanished)
        res.missing += self._mark_missing(root.root_id, start_rel, unlisted)

    def _check_marker(self, root, base: pathlib.Path, opts: ScanOptions) -> bool:
        p = base / ROOT_MARKER
        if os.path.exists(long_path(p)):
            try:
                m = RootMarker.read(p)
            except FormatError as e:
                self._warn(f"root {root.name}: unreadable root marker ({e})")
                return False
            if m.root_id != root.root_id or m.library_id != self.library.library_id:
                self._warn(f"root {root.name}: {base} is marked as a different root ({m.name}, {m.root_id}); "
                           "refusing to scan it. Check the mapping in machine.toml.")
                return False
            return True
        if opts.adopt_root:
            atomic_write_bytes(p, RootMarker(self.library.library_id, root.root_id, root.name, utc_now()).to_bytes(),
                               hidden=True)
            self._warn(f"root {root.name}: wrote a new root marker at {p}")
            return True
        if opts.no_folder_cache or self.mcfg.cache_placement == "local_only":
            # read-only scans write nothing into the root, so they don't need its marker;
            # apply still does (it checks before moving anything)
            self._warn(f"root {root.name}: no root marker at {p}; read-only scan continues")
            return True
        self._warn(f"root {root.name}: no root marker at {p}; refusing to scan (use --adopt-root if this "
                   "really is the root)")
        return False

    # ----------------------------------------------------------------- walk
    def _walk(self, root, base: pathlib.Path, start_rel: str, opts: ScanOptions):
        folders: list[Folder] = []
        unlisted: list[str] = []
        excluded = [p for p in (self.project_home, self.mcfg.quarantine_root, self.state_dir) if p]
        stack = [start_rel]
        while stack:
            rel = stack.pop()
            abs_dir = rel_to_path(base, rel)
            try:
                with os.scandir(long_path(abs_dir)) as it:
                    items = list(it)
            except OSError as e:
                self._res.errors += 1
                self._warn(f"can't list {root.name}:{rel or '/'} ({e.strerror or e})")
                unlisted.append(rel)
                continue
            files, subdirs, caches = [], [], []
            for de in items:
                name = de.name
                if is_cache_file_name(name):
                    caches.append(de.path)
                    continue
                if name.startswith(OWN_FILE_PREFIX) or self.settings.ignored(name):
                    continue
                try:
                    st = de.stat(follow_symlinks=False)
                except OSError as e:
                    self._res.errors += 1
                    self._warn(f"can't stat {root.name}:{join_rel(rel, name)} ({e.strerror or e})")
                    continue
                if de.is_symlink() or (file_attributes(st) & FILE_ATTRIBUTE_REPARSE_POINT
                                       and not is_placeholder(st) and de.is_dir(follow_symlinks=False)):
                    self._warn(f"not following link {root.name}:{join_rel(rel, name)}")
                    continue
                if de.is_dir(follow_symlinks=False):
                    if rel == "" and name.casefold() == DELETE_FOLDER.casefold():
                        continue        # Photo Haven's own delete folder: never library content
                    sub = rel_to_path(base, join_rel(rel, name))
                    if any(is_inside(sub, ex) for ex in excluded):
                        self._warn(f"skipping {sub}: Photo Haven's own data is inside a root (photohaven doctor)")
                        continue
                    subdirs.append(join_rel(rel, name))
                else:
                    files.append((name, de.path, st))
            for s in sorted(subdirs, key=lambda r: sort_key(r.rsplit("/", 1)[-1]), reverse=True):
                stack.append(s)
            folders.append(self._make_folder(root, rel, str(abs_dir), files, caches, opts))
            self.progress(f"walk {root.name}: {len(folders)} folders")
        folders.sort(key=lambda f: [sort_key(p) for p in f.relpath.split("/")] if f.relpath else [])
        return folders, unlisted

    def _local_cache_dir(self, root_id: str, rel: str) -> pathlib.Path:
        return rel_to_path(self.state_dir / "caches" / root_id, rel)

    def _make_folder(self, root, rel: str, abs_dir: str, files, cache_paths: list[str], opts) -> Folder:
        own_name = cache_file_name(self.machine.short)
        local_dir = self._local_cache_dir(root.root_id, rel)
        try:
            for n in os.listdir(long_path(local_dir)):
                if is_cache_file_name(n):
                    cache_paths.append(os.path.join(long_path(local_dir), n))
        except OSError:
            pass
        caches: list[CacheFile] = []
        own: CacheFile | None = None
        own_path = self._own_cache_path(root.root_id, rel, abs_dir, opts)
        for cp in cache_paths:
            try:
                c = read_cache(cp)
            except (FormatError, OSError, UnicodeDecodeError) as e:
                self._warn(f"ignoring cache file {cp} ({e})")
                continue
            for w in c.warnings:
                self._warn(w)
            if c.header.library_id != self.library.library_id:
                self._warn(f"ignoring cache file {cp}: it belongs to another library")
                continue
            caches.append(c)
            if os.path.normcase(os.path.abspath(cp)) == os.path.normcase(os.path.abspath(own_path)):
                own = c
        is_intake = folder_flag(rel, root.intake_folders) if rel else False
        is_protected = folder_flag(rel, root.protected_folders) if rel else False
        name = rel.rsplit("/", 1)[-1] if rel else ""
        f = Folder(root.root_id, rel, abs_dir, [], own, bool(name) and self.settings.is_generic(name),
                   is_intake, is_protected)
        keys = collections.Counter(name_key(n) for n, _, _ in files)
        records: dict[str, list[tuple[FileFacts, str]]] = collections.defaultdict(list)
        by_key: dict[str, list[tuple[FileFacts, str]]] = collections.defaultdict(list)
        for c in caches:
            for r in c.records:
                records[r.name].append((r, c.header.machine_id))
                by_key[name_key(r.name)].append((r, c.header.machine_id))
        tol = self.settings.mtime_tolerance_us
        for name, path, st in sorted(files, key=lambda x: sort_key(x[0])):
            e = Entry(name, long_path(path), st.st_size, stat_mtime_us(st), stat_ctime_us(st),
                      self.settings.media_of(name), is_placeholder(st))
            if keys[name_key(name)] > 1:
                e.flags["path_key_collision"] = None
            if e.size == 0:
                e.flags["zero_bytes"] = None
            self._res.files_seen += 1
            if e.media != "other":
                cands = records.get(name) or (by_key.get(name_key(name), []) if keys[name_key(name)] == 1 else [])
                usable = [(r, m) for r, m in cands
                          if r.sha256 and r.size == e.size and abs(r.mtime_us - e.mtime_us) <= tol]
                shas = {r.sha256 for r, _ in usable}
                if len(shas) > 1:
                    e.flags["cache_conflict"] = "cached records disagree on sha256: " + ", ".join(sorted(shas))
                elif usable and not opts.rehash:
                    r, m = max(usable, key=lambda x: x[0].observed_at)
                    e.facts, e.source_machine = dataclasses.replace(
                        r, name=name, size=e.size, mtime_us=e.mtime_us, ctime_us=e.ctime_us, media=e.media,
                        skip=None, extra=dict(r.extra)), m
                    self._res.reused += 1
                    self._learn(r)
                if e.facts is None:
                    if e.placeholder:
                        e.facts = FileFacts(name=name, size=e.size, mtime_us=e.mtime_us, media=e.media,
                                            sha256=None, observed_at=self._now, ctime_us=e.ctime_us,
                                            skip="cloud_only")
                        self._res.cloud_only += 1
                    else:
                        e.queued = True
            f.entries.append(e)
        return f

    def _own_cache_path(self, root_id: str, rel: str, abs_dir: str, opts) -> str:
        name = cache_file_name(self.machine.short)
        if opts.no_folder_cache or self.mcfg.cache_placement == "local_only":
            return os.path.join(long_path(self._local_cache_dir(root_id, rel)), name)
        return os.path.join(long_path(abs_dir), name)

    def _learn(self, r: FileFacts) -> None:
        """Remember content facts carried by a cache record (with metadata)."""
        if r.sha256 and r.meta_impl and r.sha256 not in self.known and not stale(r.meta_impl, r.meta):
            self.known[r.sha256] = {"content_sha256": r.content_sha256, "content_hash_impl": r.content_hash_impl,
                                    "meta": r.meta, "meta_impl": r.meta_impl}

    # ----------------------------------------------------------------- hash
    def _run_pass(self, folders: list[Folder], ids: set[int], opts: ScanOptions, only_touched: bool) -> None:
        jobs: dict[int, cf.Future] = {}
        if opts.hashing:
            for f in folders:
                for e in f.entries:
                    if id(e) in ids:
                        jobs[id(e)] = self._pool.submit(hash_file, e.path)
        for f in folders:
            mine = [e for e in f.entries if id(e) in jobs]
            if only_touched and not mine:
                continue
            for e in mine:
                self._apply_hash(f, e, jobs[id(e)].result())
            self._describe(f)
            self._persist(f, opts)
            self.progress(f"{f.relpath or '/'}: hashed {self._res.files_hashed}, "
                          f"{self._res.bytes_hashed / 1e9:.2f} GB")

    def _apply_hash(self, f: Folder, e: Entry, h: _Hashed) -> None:
        res = self._res
        e.queued = False
        if h.error == "vanished":
            e.vanished = True
            return
        if h.error == "cloud_only":
            e.placeholder = True
            e.facts = FileFacts(name=e.name, size=e.size, mtime_us=e.mtime_us, media=e.media, sha256=None,
                                observed_at=self._now, ctime_us=e.ctime_us, skip="cloud_only")
            res.cloud_only += 1
            return
        if h.error:
            res.errors += 1
            skip = "locked" if h.error == "locked" else "unreadable"
            e.flags["unreadable"] = f"{h.error}: {h.detail}" if h.detail else h.error
            if e.audit_sha:    # keep the reused facts; the audit just couldn't read it
                return
            e.facts = FileFacts(name=e.name, size=e.size, mtime_us=e.mtime_us, media=e.media, sha256=None,
                                observed_at=self._now, ctime_us=e.ctime_us, skip=skip)
            return
        res.files_hashed += 1
        res.bytes_hashed += h.size
        e.size, e.mtime_us, e.ctime_us = h.size, h.mtime_us, h.ctime_us
        if e.audit_sha:
            if h.sha256 != e.audit_sha:
                res.audit_mismatches += 1
                e.flags["cache_conflict"] = f"audit: cached {e.audit_sha}, now {h.sha256}"
                self._warn(f"audit mismatch: {f.relpath}/{e.name}")
            else:
                return
        e.facts = FileFacts(name=e.name, size=h.size, mtime_us=h.mtime_us, media=e.media, sha256=h.sha256,
                            observed_at=utc_now(), ctime_us=h.ctime_us)
        e.source_machine = self.machine.machine_id

    # ------------------------------------------------------------- describe
    def _content_for(self, sha: str) -> dict | None:
        if sha in self.known:
            return self.known[sha]
        row = db.content_row(self.conn, sha)
        if row is not None and row["meta_impl"]:
            facts = db.facts_from_content(row)
            if stale(facts.get("meta_impl"), facts.get("meta")):
                return None             # described again with the current rules
            self.known[sha] = facts
            return self.known[sha]
        return None

    def _describe(self, f: Folder) -> None:
        """Fill content facts; run ExifTool once per new unique sha256 (04 stage 4)."""
        todo: dict[str, Entry] = {}
        for e in f.entries:
            if not e.facts or not e.facts.sha256:
                continue
            c = self._content_for(e.facts.sha256)
            if c is None and e.media in DESCRIBED_MEDIA and self.describer and not e.placeholder:
                try:
                    if is_placeholder(os.stat(e.path)):
                        continue        # became cloud-only since it was hashed: describe it later
                except OSError:
                    continue
                todo.setdefault(e.facts.sha256, e)
        items = list(todo.items())
        for i in range(0, len(items), DESCRIBE_BATCH):
            batch = items[i:i + DESCRIBE_BATCH]
            try:
                raws = self.describer.describe([e.path for _, e in batch])
            except Exception as ex:   # an ExifTool crash must not fail the scan
                self._warn(f"ExifTool failed on {f.relpath or '/'} ({ex})")
                raws = [None] * len(batch)
            for (sha, e), raw in zip(batch, raws):
                impl = f"exiftool/{self.describer.version}"
                if raw is None:
                    e.flags["exiftool_error"] = "ExifTool returned nothing"
                    self._res.errors += 1
                    continue
                d = normalize(raw)
                self.known[sha] = {
                    "content_sha256": d.content_sha256,
                    "content_hash_impl": f"{impl}/ImageDataHash" if d.content_sha256 else None,
                    "meta": d.meta or None, "meta_impl": f"{impl};ph-meta/{PH_META}"}
                self._res.described += 1
        for e in f.entries:
            if e.facts and e.facts.sha256:
                c = self.known.get(e.facts.sha256)
                if c:
                    e.facts = dataclasses.replace(e.facts, **c)
                if e.facts.meta and e.facts.meta.get("damaged"):
                    e.flags["damaged"] = "; ".join(e.facts.meta.get("exiftool_messages", [])) or None
                elif e.facts.meta and e.facts.meta.get("exiftool_messages"):
                    _, suspect = classify(e.facts.meta["exiftool_messages"], False)
                    if suspect:
                        e.flags["suspect"] = "; ".join(e.facts.meta["exiftool_messages"])

    # -------------------------------------------------------------- persist
    def _persist(self, f: Folder, opts: ScanOptions) -> None:
        res = self._res
        records = [e.facts for e in f.entries if e.facts and not e.vanished]
        # 1. the cache file (the durable fact store) before the DB commit
        if opts.hashing and records:
            path = self._own_cache_path(f.root_id, f.relpath, f.path, opts)
            if f.own_cache is None or not facts_equal(f.own_cache.records, records):
                header = CacheHeader(self.library.library_id, f.root_id, f.relpath, self.machine.machine_id,
                                     utc_now(), TOOL)
                in_folder = not (opts.no_folder_cache or self.mcfg.cache_placement == "local_only")
                try:
                    atomic_write_bytes(path, cache_bytes(header, records), hidden=in_folder)
                    f.own_cache = CacheFile(path, header, list(records), [])
                    res.cache_files_written += 1
                except OSError as ex:
                    res.errors += 1
                    self._warn(f"can't write cache file {path} ({ex})")
        # 2. the DB
        c = self.conn
        now = self._now
        c.execute("BEGIN")
        try:
            fid = db.upsert_folder(c, f.root_id, f.relpath, self._scan_id, is_generic=f.is_generic,
                                   is_intake=f.is_intake, is_protected=f.is_protected)
            for e in f.entries:
                if e.vanished:
                    continue
                facts = e.facts
                sha = facts.sha256 if facts else None
                if sha:
                    db.upsert_content(c, facts, now)
                    db.add_folder_name(c, sha, f.root_id, f.relpath, now)
                skip = facts.skip if facts else None
                state = {"cloud_only": "cloud_only", "unreadable": "unreadable"}.get(skip or "", "present")
                if e.placeholder and state == "present":
                    state = "cloud_only"
                file_id = db.upsert_file(
                    c, root_id=f.root_id, folder_id=fid, relpath=join_rel(f.relpath, e.name), media=e.media,
                    size=e.size, mtime_us=e.mtime_us, ctime_us=e.ctime_us, sha256=sha,
                    hashed_at=facts.observed_at if sha else None, hashed_by=e.source_machine if sha else None,
                    state=state, skip_reason=skip, scan_id=self._scan_id, now=now)
                db.set_flags(c, file_id, e.flags, self._scan_id, SCANNER_FLAGS)
            res.missing += db.mark_missing_in_folder(c, fid, self._scan_id)
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise

    def _mark_missing(self, root_id: str, start_rel: str, unlisted: list[str]) -> int:
        """Folders under the scanned subtree that this scan didn't see."""
        c = self.conn
        n = 0
        rows = c.execute("SELECT folder_id, relpath FROM folders WHERE root_id = ? AND present = 1 "
                         "AND last_scan_id IS NOT ?", (root_id, self._scan_id)).fetchall()
        def under(rel: str, top: str) -> bool:
            return top == "" or rel == top or rel.startswith(top + "/")
        c.execute("BEGIN")
        try:
            for fid, rel in rows:
                if not under(rel, start_rel) or any(under(rel, u) for u in unlisted):
                    continue
                c.execute("UPDATE folders SET present = 0 WHERE folder_id = ?", (fid,))
                n += db.mark_missing_in_folder(c, fid, self._scan_id)
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        return n
