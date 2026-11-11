"""The link pass (docs/05): relationships other than exact duplicates, and
folder-name cleanup. Runs at the end of every scan; cheap and idempotent.

Exact duplicates need no work here: a group is a shared `files.sha256`
(the `dup_groups` view). Raw pairs and Live Photos are milestone R1.
"""
from __future__ import annotations

import collections
import json
import re
import sqlite3

from .config import LibrarySettings
from .util import name_key

# 05 §7 cleanup
_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december|" \
          "jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
_DATE_PREFIXES = [
    r"(19|20)\d{2}[-_.](0[1-9]|1[0-2])[-_.](0[1-9]|[12]\d|3[01])",     # 2015-07-04
    r"(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])",             # 20150704
    r"(0[1-9]|1[0-2])[-_.](0[1-9]|[12]\d|3[01])[-_.](19|20)\d{2}",   # 07-04-2015
    r"(19|20)\d{2}[-_.](0[1-9]|1[0-2])",                             # 2015-07
    rf"({_MONTHS})\s+(19|20)\d{{2}}",                                # July 2015
]
_PREFIX_RE = re.compile(r"^(" + "|".join(_DATE_PREFIXES) + r")(?=$|[\s\-_.,])[\s\-_.,]*", re.IGNORECASE)
_COPY_RE = re.compile(r"(\s*\(\d+\)|\s*-\s*copy(\s*\(\d+\))?|\s+copy(\s*\d+)?)$", re.IGNORECASE)
_JUNK_RE = re.compile(r"\b(img|dsc|dscn|dcim)\b|\b(?!(?:19|20)\d{2}\b)\d{3,}\b", re.IGNORECASE)
_DATES_ANYWHERE = re.compile(r"\b(19|20)\d{2}([-_.]\d{2}){0,2}\b")


def clean_folder_name(name: str, settings: LibrarySettings) -> tuple[str | None, str | None]:
    """(event_guess, stripped_date) for a folder name; event_guess is None
    when the name is generic (05 §7)."""
    n = name.strip()
    if not n or settings.is_generic(n):
        return None, None
    date = None
    m = _PREFIX_RE.match(n)
    if m:
        date = m.group(1)
        n = n[m.end():]
    prev = None
    while prev != n:
        prev = n
        n = _COPY_RE.sub("", n).strip()
    n = _JUNK_RE.sub(" ", n)
    n = re.sub(r"[_\s\-.]+", " ", n).strip(" -_.,")
    # "Backup 2019", "Backup 2026 (phone)", "My Pictures": only generic words,
    # dates and parentheticals are left, so the name says nothing about an event
    bare = _PAREN.sub(" ", _DATES_ANYWHERE.sub(" ", n))
    words = [w for w in re.split(r"[\s\-_.,()]+", bare.lower()) if w and not w.isdigit()]
    if (not n or not words or settings.is_generic(n) or settings.is_generic(" ".join(words))
            or all(w in _generic_words(settings) for w in words)):
        return None, date
    return n, date


_PAREN = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_FILLER = {"my", "old", "new", "from", "of", "the", "and", "all", "pics", "pix", "images", "image", "photo", "picture",
           "files", "file", "folder", "stuff", "temp", "tmp", "phone", "camera", "upload", "uploads", "imported",
           "copied", "synced", "saved", "downloads", "download"}


def _generic_words(settings: LibrarySettings) -> set[str]:
    words = set(_FILLER)
    for n in settings.raw["folders"]["generic_names"]:
        words.update(w for w in n.lower().split() if w)
    return words


def descriptiveness(event_guess: str | None) -> tuple[int, int]:
    """Sort key: non-generic first, then more tokens (05 §7 "Most descriptive")."""
    if not event_guess:
        return (0, 0)
    return (1, len(event_guess.split()))


EDIT_RE = re.compile(r"^(IMG_)E(\d{4})$", re.IGNORECASE)


def run(conn: sqlite3.Connection, settings: LibrarySettings, scan_id: int | None = None) -> dict:
    """Recompute folder names, content-equivalent groups and edit links."""
    stats: dict[str, int] = {}
    conn.execute("BEGIN")
    try:
        # folder event guesses (and generic flags, which the scanner also sets)
        rows = conn.execute("SELECT folder_id, name FROM folders").fetchall()
        upd = []
        for fid, name in rows:
            guess, _ = clean_folder_name(name, settings) if name else (None, None)
            upd.append((guess, int(bool(name) and guess is None), fid))
        conn.executemany("UPDATE folders SET event_guess = ?, is_generic = ? WHERE folder_id = ?", upd)
        conn.execute("UPDATE content_folder_names SET event_guess = (SELECT f.event_guess FROM folders f "
                     "WHERE f.root_id = content_folder_names.root_id AND f.relpath = content_folder_names.folder_relpath)")

        conn.execute("DELETE FROM links WHERE kind IN ('content_equiv', 'edit')")
        # content-equivalent: same content_sha256, different sha256, present instances (05 §2)
        groups = collections.defaultdict(list)
        for ch, fid, sha in conn.execute(
                "SELECT c.content_sha256, f.file_id, f.sha256 FROM files f JOIN contents c USING(sha256) "
                "WHERE f.state = 'present' AND c.content_sha256 IS NOT NULL AND c.damaged = 0 "
                "ORDER BY c.content_sha256, f.file_id"):
            groups[ch].append((fid, sha))
        n = 0
        for ch, members in groups.items():
            if len({s for _, s in members}) < 2:
                continue
            lid = conn.execute("INSERT INTO links(kind, method, confidence, scan_id, detail_json) VALUES "
                               "('content_equiv', 'content_hash', 'high', ?, ?)",
                               (scan_id, json.dumps({"content_sha256": ch}))).lastrowid
            conn.executemany("INSERT INTO link_members(link_id, file_id, role) VALUES(?, ?, 'member')",
                             [(lid, fid) for fid, _ in members])
            n += 1
        stats["content_equiv"] = n

        # edited derivatives (05 §5): IMG_E1234 + IMG_1234, and IMG_1234.AAE
        by_folder = collections.defaultdict(list)
        for r in conn.execute("SELECT f.file_id, f.folder_id, f.name, f.stem_key, f.ext, c.taken_local FROM files f "
                              "LEFT JOIN contents c USING(sha256) WHERE f.state = 'present'"):
            by_folder[r["folder_id"]].append(r)
        n = 0
        for files in by_folder.values():
            by_stem = collections.defaultdict(list)
            for r in files:
                by_stem[r["stem_key"]].append(r)
            for stem, members in by_stem.items():
                m = EDIT_RE.match(stem)
                if m:
                    orig_stem = name_key(m.group(1) + m.group(2))
                    for e in members:
                        if e["ext"] == "aae":
                            continue
                        for o in by_stem.get(orig_stem, []):
                            if o["ext"] == "aae":
                                continue
                            conf = "high" if e["taken_local"] and e["taken_local"] == o["taken_local"] else "medium"
                            _link(conn, "edit", "name_time" if conf == "high" else "name", conf, scan_id,
                                  [(o["file_id"], "original"), (e["file_id"], "edited")])
                            n += 1
                aae = [r for r in members if r["ext"] == "aae"]
                others = [r for r in members if r["ext"] != "aae"]
                for a in aae:
                    for o in others:
                        _link(conn, "edit", "name", "high", scan_id, [(o["file_id"], "original"), (a["file_id"], "aae")])
                        n += 1
        stats["edit"] = n
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return stats


def _link(conn, kind, method, confidence, scan_id, members) -> None:
    lid = conn.execute("INSERT INTO links(kind, method, confidence, scan_id) VALUES(?,?,?,?)",
                       (kind, method, confidence, scan_id)).lastrowid
    conn.executemany("INSERT INTO link_members(link_id, file_id, role) VALUES(?,?,?)",
                     [(lid, f, r) for f, r in members])
