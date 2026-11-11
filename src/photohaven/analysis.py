"""Folder statistics, containment, and the suggestion engine (docs/07 §2, §4).

Pure functions of DB state. Suggestions are never decisions: they pre-fill
the report and nothing applies until a human exports a decision.
"""
from __future__ import annotations

import collections
import dataclasses
import itertools
import re
import sqlite3

from .config import Library, folder_flag
from .link import descriptiveness  # noqa: F401  (re-exported for callers)

# instances that count toward duplicate statistics (05 §1: present only;
# cloud-only files with facts from another machine also exist in the library)
COUNTED_STATES = ("present", "cloud_only")
COPY_MARKER = re.compile(r"(\s*\(\d+\)|\s*-\s*copy(\s*\(\d+\))?|\s+copy(\s*\d+)?)$", re.IGNORECASE)


@dataclasses.dataclass
class FolderStat:
    folder_id: int
    root_id: str
    relpath: str
    name: str
    depth: int
    parent_id: int | None
    event_guess: str | None
    is_generic: bool
    locked: bool                  # intake or protected: folder-level remove is ignored (07 §5)
    files: int = 0
    bytes: int = 0
    unhashed: int = 0
    other_files: int = 0          # non-media files (never hashed by default), part of `unhashed`
    dup_files: int = 0            # files with a copy outside this folder
    dup_bytes: int = 0
    unique_files: int = 0
    internal_extra: int = 0       # extra copies whose only other copies are in this folder
    internal_extra_bytes: int = 0
    contents: dict = dataclasses.field(default_factory=dict)   # sha256 -> size (distinct)

    @property
    def redundant(self) -> bool:
        return self.files > 0 and self.unhashed == 0 and self.dup_files == self.files

    @property
    def reclaimable(self) -> int:
        return self.dup_bytes + self.internal_extra_bytes


@dataclasses.dataclass
class SubtreeStat:
    folder_id: int
    folders_with_files: int = 0
    files: int = 0
    bytes: int = 0
    unhashed: int = 0
    dup_files: int = 0            # copy outside the subtree
    dup_bytes: int = 0

    @property
    def redundant(self) -> bool:
        return self.files > 0 and self.unhashed == 0 and self.dup_files == self.files


@dataclasses.dataclass
class Pair:
    a: int
    b: int
    shared: int
    shared_bytes: int
    a_in_b: float
    b_in_a: float
    a_complete: bool = True       # every file of a is hashed
    b_complete: bool = True

    @property
    def contained(self) -> bool:
        return self.a_complete and self.a_in_b == 1.0

    @property
    def identical(self) -> bool:
        return self.contained and self.b_complete and self.b_in_a == 1.0


@dataclasses.dataclass
class Suggestion:
    action: str                   # keep | remove | undecided
    reason: str
    scope: str = "folder"         # folder | subtree
    locked: bool = False
    other: int | None = None      # the folder this is compared with


@dataclasses.dataclass
class Analysis:
    folders: dict[int, FolderStat]
    subtrees: dict[int, SubtreeStat]
    pairs: dict[tuple[int, int], Pair]      # (a, b): a's view of b
    instances: dict[str, list[tuple[int, str, int]]]   # sha -> [(folder_id, name, file_id)]
    sizes: dict[str, int]
    _sugg: dict[int, "Suggestion"] | None = None
    _roles: dict | None = None

    @property
    def roles(self) -> dict:
        """Guessed roles of folders holding copies (photohaven.roles)."""
        if self._roles is None:
            from . import roles
            self._roles = roles.classify(self)
        return self._roles

    def path(self, fid: int) -> str:
        return self.folders[fid].relpath


def analyze(conn: sqlite3.Connection, library: Library) -> Analysis:
    roots = {r.root_id: r for r in library.roots}
    folders: dict[int, FolderStat] = {}
    for r in conn.execute("SELECT folder_id, root_id, relpath, name, depth, parent_id, event_guess, is_generic "
                          "FROM folders WHERE present = 1"):
        root = roots.get(r["root_id"])
        locked = bool(root) and bool(r["relpath"]) and (folder_flag(r["relpath"], root.intake_folders)
                                                        or folder_flag(r["relpath"], root.protected_folders))
        if root and not r["relpath"] and ("" in root.intake_folders or "" in root.protected_folders):
            locked = True
        folders[r["folder_id"]] = FolderStat(r["folder_id"], r["root_id"], r["relpath"], r["name"], r["depth"],
                                             r["parent_id"], r["event_guess"], bool(r["is_generic"]), locked)
    instances: dict[str, list[tuple[int, str, int]]] = collections.defaultdict(list)
    sizes: dict[str, int] = {}
    q = ("SELECT file_id, folder_id, name, size, sha256, state, media FROM files WHERE state IN "
         "('present', 'cloud_only', 'unreadable')")
    for r in conn.execute(q):
        f = folders.get(r["folder_id"])
        if f is None:
            continue
        f.files += 1
        f.bytes += r["size"]
        if r["sha256"] is None or r["state"] not in COUNTED_STATES:
            f.unhashed += 1
            if r["media"] == "other":
                f.other_files += 1
            continue
        instances[r["sha256"]].append((r["folder_id"], r["name"], r["file_id"]))
        sizes[r["sha256"]] = r["size"]
        f.contents[r["sha256"]] = r["size"]

    # per-folder duplicate stats
    for sha, inst in instances.items():
        per = collections.Counter(fid for fid, _, _ in inst)
        total = len(inst)
        size = sizes[sha]
        for fid, k in per.items():
            f = folders[fid]
            if total > k:
                f.dup_files += k
                f.dup_bytes += k * size
            else:
                f.unique_files += 1           # its only copies are here
                if k > 1:                     # one of them stays (last-copy invariant)
                    f.internal_extra += k - 1
                    f.internal_extra_bytes += (k - 1) * size

    # subtrees: add each file's sha count to all ancestors
    subtrees = {fid: SubtreeStat(fid) for fid in folders}
    ancestors: dict[int, list[int]] = {}
    for fid in folders:
        chain, cur = [], fid
        while cur is not None and cur in folders:
            chain.append(cur)
            cur = folders[cur].parent_id
        ancestors[fid] = chain
    sub_counts: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
    for sha, inst in instances.items():
        for fid, _, _ in inst:
            for a in ancestors[fid]:
                sub_counts[a][sha] += 1
    for fid, f in folders.items():
        for a in ancestors[fid]:
            s = subtrees[a]
            s.files += f.files
            s.bytes += f.bytes
            s.unhashed += f.unhashed
            if f.files:
                s.folders_with_files += 1
    for a, cnt in sub_counts.items():
        s = subtrees[a]
        for sha, k in cnt.items():
            if len(instances[sha]) > k:
                s.dup_files += k
                s.dup_bytes += k * sizes[sha]

    # pairs via the inverted index sha -> folders (07 §2)
    shared: dict[tuple[int, int], list[int]] = collections.defaultdict(lambda: [0, 0])
    for sha, inst in instances.items():
        fids = sorted({fid for fid, _, _ in inst})
        if len(fids) < 2:
            continue
        for a, b in itertools.combinations(fids, 2):
            v = shared[(a, b)]
            v[0] += 1
            v[1] += sizes[sha]
    pairs: dict[tuple[int, int], Pair] = {}
    for (a, b), (n, nb) in shared.items():
        fa, fb = folders[a], folders[b]
        a_in_b, b_in_a = n / len(fa.contents), n / len(fb.contents)
        ca, cb = fa.unhashed == 0, fb.unhashed == 0
        pairs[(a, b)] = Pair(a, b, n, nb, a_in_b, b_in_a, ca, cb)
        pairs[(b, a)] = Pair(b, a, n, nb, b_in_a, a_in_b, cb, ca)
    return Analysis(folders, subtrees, pairs, instances, sizes)


# --------------------------------------------------------------- suggestions

def _own_desc(f: FolderStat) -> tuple:
    return descriptiveness(f.event_guess)


def path_desc(an: Analysis, fid: int) -> tuple:
    """How descriptive a folder's path is: the best name on the way down from
    the root (Pictures/iPhone/2026-10 is described by 'iPhone'), then its own."""
    best, cur = (0, 0), fid
    while cur is not None and cur in an.folders:
        best = max(best, _own_desc(an.folders[cur]))
        cur = an.folders[cur].parent_id
    return best + _own_desc(an.folders[fid])


def best_container(an: Analysis, fid: int) -> tuple[int | None, int]:
    """The folder that contains fid that is the most natural keeper, and how
    many containers there are in all."""
    cands = [p.b for (a, _), p in an.pairs.items() if a == fid and p.contained]
    if not cands:
        return None, 0
    def rank(b):
        fb = an.folders[b]
        return (not fb.locked, tuple(-x for x in path_desc(an, b)), -len(fb.contents), fb.depth, fb.relpath)
    cands.sort(key=rank)
    return cands[0], len(cands)


def suggest(an: Analysis, fid: int) -> Suggestion:
    """The coherent suggestion for a folder (see suggest_all)."""
    if an._sugg is None:
        an._sugg = suggest_all(an)
    return an._sugg[fid]


def suggest_all(an: Analysis) -> dict[int, Suggestion]:
    """07 §4 per folder, then a coherence pass: if every folder holding some
    content is suggested for removal, the best-ranked of them is suggested
    for keeping instead, so accepting every suggestion never asks to remove
    every copy. (The planner enforces the last-copy invariant regardless,
    08 §2; this keeps the suggestions themselves honest.)"""
    out = {fid: _suggest_one(an, fid) for fid in an.folders}
    def rank(fid):
        f = an.folders[fid]
        return (tuple(-x for x in path_desc(an, fid)), -len(f.contents), f.depth, f.relpath)
    changed = True
    while changed:
        changed = False
        for sha, inst in an.instances.items():
            fids = {fid for fid, _, _ in inst}
            if all(out[fid].action == "remove" for fid in fids):
                keep = min(fids, key=rank)
                out[keep] = Suggestion("keep", "Kept so that one copy of every file stays: the other folders "
                                       "holding these files are all suggested for removal")
                changed = True
    return out


def _suggest_one(an: Analysis, fid: int) -> Suggestion:
    """07 §4, for the files directly in a folder."""
    f = an.folders[fid]
    if f.locked:
        return Suggestion("keep", f"{f.relpath or 'the root'} is protected intake", locked=True)
    if f.files == 0:
        return Suggestion("undecided", "no files directly in this folder")
    if f.unhashed:
        pending = f.unhashed - f.other_files
        if pending:
            return Suggestion("undecided", f"{pending} file(s) not hashed yet (cloud-only, skipped, or waiting for "
                              "the second hash pass)")
        dup_note = ("every photo here has a copy elsewhere" if f.dup_files == f.files - f.unhashed
                    else f"{f.unique_files} photo(s) are unique to this folder")
        return Suggestion("undecided", f"{f.other_files} non-photo file(s) here are not checked for copies; "
                          f"{dup_note}. A remove decision moves only the duplicates.")
    from . import roles as R
    role = an.roles.get(fid)
    if role and role.role in (R.CURATION, R.PURPOSE, R.RECEIVED):
        return Suggestion("keep", R.describe(role) + ". Its copies carry that meaning; keep it unless you're sure")
    if role and role.role in (R.SHARED, R.REFILED):
        return Suggestion("undecided", R.describe(role))
    b, n = best_container(an, fid)
    if b is not None:
        fb = an.folders[b]
        others = f" (and {n - 1} other folder{'s' if n > 2 else ''})" if n > 1 else ""
        if fb.locked:
            return Suggestion("remove", f"Every file is already in {fb.relpath}{others}, which is intake", other=b)
        mine, theirs = path_desc(an, fid), path_desc(an, b)
        if an.pairs[(fid, b)].identical:
            if mine < theirs:
                return Suggestion("remove", f"Identical to {fb.relpath}, which has the more descriptive name", other=b)
            if mine > theirs:
                return Suggestion("keep", f"Identical to {fb.relpath}; this one has the more descriptive name", other=b)
            if (f.depth, f.relpath) < (fb.depth, fb.relpath):
                return Suggestion("keep", f"Identical to {fb.relpath}; keeping the one at {f.relpath}", other=b)
            return Suggestion("remove", f"Identical; keeping the one at {fb.relpath}", other=b)
        if mine > theirs:
            return Suggestion("keep", f"Every file is also in {fb.relpath}{others}, but this folder's name says more; "
                              f"consider the copies there instead", other=b)
        if f.is_generic and not fb.is_generic:
            return Suggestion("remove", f"Every file is in {fb.relpath}{others}; this folder's name is generic", other=b)
        return Suggestion("remove", f"Every file is also in {fb.relpath}{others}", other=b)
    if f.redundant:
        return Suggestion("remove", "Every file exists elsewhere")
    if f.dup_files:
        return Suggestion("undecided", f"{f.unique_files} file(s) are unique to this folder")
    return Suggestion("keep", "")


def suggest_subtree(an: Analysis, fid: int) -> Suggestion | None:
    """A whole redundant tree (several folders with files) -> remove the
    subtree, but only when every folder with files in it would itself be
    suggested for removal, so the two levels never disagree."""
    s = an.subtrees[fid]
    f = an.folders[fid]
    if not s.redundant or s.folders_with_files < 2:
        return None
    members = [fid, *_descendants(an, fid)]
    if any(an.folders[d].locked for d in members):
        return None
    if any(an.folders[d].files and suggest(an, d).action != "remove" for d in members):
        return None
    return Suggestion("remove", f"All {s.files} files in this tree exist elsewhere", scope="subtree")


def _descendants(an: Analysis, fid: int) -> list[int]:
    pre = an.folders[fid].relpath
    root = an.folders[fid].root_id
    return [d for d, g in an.folders.items() if g.root_id == root and d != fid
            and (pre == "" or g.relpath.startswith(pre + "/"))]


def copy_rank(name: str) -> tuple:
    """Which of several same-bytes files in one folder to keep: no copy
    marker, then shortest name, then name order."""
    stem = name.rsplit(".", 1)[0]
    return (bool(COPY_MARKER.search(stem)), len(name), name)
