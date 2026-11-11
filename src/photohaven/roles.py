"""Why does this folder hold copies? (design direction B, 2026-11-11)

Copies are not all clutter. From the first full-library run: backups and
re-imports are, but `BestOf` (131 picks from 21 folders), `4 Star Pictures`,
`Favs`, `Card`, `Craigslist`, `Hong Kong From Jacky` and a shoot filed both by
date and by event are information stored as copies: a rating, an album, a
purpose, a provenance, a second way in, and sometimes a subset shared with
different people (which nothing on disk records).

Each folder with duplicated files gets a guessed role, with the evidence for
it. The suggestion engine follows the role, and the report shows the
evidence and asks about sharing. Roles are advisory: the human's keep or
remove decision is what applies. The word lists and thresholds are pinned
here and versioned with ROLES_VERSION.
"""
from __future__ import annotations

import collections
import dataclasses
import re

ROLES_VERSION = 1

BACKUP, CURATION, PURPOSE, RECEIVED, SHARED, REFILED = (
    "backup", "curation", "purpose", "received", "shared", "refiled")
ASKS_ABOUT_SHARING = (CURATION, PURPOSE, RECEIVED, SHARED)

CURATION_WORDS = {"best", "bestof", "fav", "favs", "fave", "faves", "favorite", "favorites", "favourite",
                  "favourites", "star", "stars", "top", "picks", "pick", "picked", "select", "selects", "selected",
                  "selection", "selections", "final", "finals", "keepers", "keeper", "highlights", "winners"}
PURPOSE_WORDS = {"card", "cards", "craigslist", "ebay", "marketplace", "yearbook", "print", "prints", "printing",
                 "wallpaper", "wallpapers", "slideshow", "presentation", "frame", "frames", "calendar", "invitation",
                 "invitations", "attachments", "attachment", "evidence", "insurance", "listing", "listings",
                 "memorial", "collage", "poster", "book"}
SHARED_WORDS = {"shared", "share", "sharing", "public"}
ALBUM_WORDS = {"album", "albums"}     # only in short names ("Shared Album", "Album 2"), not "…exporting albums"
MIN_STRUCTURAL = 5                    # size-based signals need at least this many shared photos
DEVICE_WORDS = {"phone", "iphone", "android", "nokia", "samsung", "pixel", "camera", "cam", "laptop", "pc",
                "computer", "desktop", "tablet", "ipad", "sd", "card", "drive", "disk", "backup", "lumia", "galaxy",
                "my", "old", "the"}


@dataclasses.dataclass
class Role:
    role: str
    evidence: list[str]
    ask_shared: bool = False


def words(name: str) -> list[str]:
    """'BestOf_2015-07 Favs' -> ['best', 'of', 'bestof', '2015', '07', 'favs']."""
    parts = re.split(r"[^A-Za-z0-9]+", name)
    out: list[str] = []
    for p in parts:
        if not p:
            continue
        camel = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", p)
        out += [c.lower() for c in camel]
        if len(camel) > 1:
            out.append(p.lower())
    return out


_COPY_MARK = re.compile(r"(\s*\(\d+\)|\s*-\s*copy(\s*\(\d+\))?|\s+copy(\s*\d+)?|\s+\d)$", re.IGNORECASE)


def base_name(name: str) -> str:
    """A file name without download/copy markers or extension spelling:
    'IMG_0506 (1).JPG', 'IMG_0506 - Copy.jpeg' and 'img_0506.jpg' are one name."""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    prev = None
    while prev != stem:
        prev, stem = stem, _COPY_MARK.sub("", stem)
    return stem.casefold()


def name_hint(name: str) -> tuple[str | None, str]:
    """A role suggested by the folder name alone, and the words that did it."""
    w = words(name)
    low = name.strip().lower()
    hit = [x for x in w if x in CURATION_WORDS]
    if hit:
        return CURATION, ", ".join(hit)
    m = re.match(r"(?:.*\s)?from\s+(.+)$", low)
    if m:
        rest = words(m.group(1))
        if rest and not any(x in DEVICE_WORDS for x in rest):
            return RECEIVED, f"from {m.group(1).strip()}"
    if re.match(r"for\s+\w", low):
        return SHARED, low.split()[0] + " " + low.split()[1]
    hit = [x for x in w if x in SHARED_WORDS]
    if not hit and len(name.split()) <= 3:
        hit = [x for x in w if x in ALBUM_WORDS]
    if hit:
        return SHARED, ", ".join(hit)
    hit = [x for x in w if x in PURPOSE_WORDS]
    if hit:
        return PURPOSE, ", ".join(hit)
    return None, ""


def classify(an) -> dict[int, Role]:
    """Roles for folders whose files are (mostly) also elsewhere."""
    out: dict[int, Role] = {}
    for fid, f in an.folders.items():
        hashed = f.files - f.unhashed
        if hashed <= 0 or f.dup_files == 0:
            continue
        frac = f.dup_files / hashed
        # where the copies live, and whether they kept their names
        src: collections.Counter = collections.Counter()
        renamed = 0
        mine = {sha: [n for ofid, n, _ in an.instances[sha] if ofid == fid] for sha in f.contents}
        for sha, names in mine.items():
            others = [(ofid, n) for ofid, n, _ in an.instances[sha] if ofid != fid]
            if not others:
                continue
            for o in {ofid for ofid, _ in others}:
                src[o] += 1
            own = {base_name(n) for n in names}
            if not own & {base_name(n) for _, n in others}:
                renamed += 1
        if not src:
            continue
        # how many source folders it takes to cover this folder's copies (greedy):
        # about 21 for a best-of drawn from 21 events, 1 for a backup of one folder,
        # however many other backups also happen to hold the same photos
        holders = {sha: {o for o, _, _ in an.instances[sha] if o != fid} for sha in mine}
        uncovered = {sha for sha, h in holders.items() if h}
        cover = 0
        while uncovered:
            best = max(src, key=lambda o: sum(1 for sha in uncovered if o in holders[sha]))
            uncovered = {sha for sha in uncovered if best not in holders[sha]}
            cover += 1
        main, shared = src.most_common(1)[0]
        main_f = an.folders[main]
        share_of_main = shared / max(1, len(main_f.contents))
        inside_source = bool(f.relpath) and (main_f.relpath == "" or f.relpath.startswith(main_f.relpath + "/"))
        dup_contents = sum(1 for sha in mine if any(o != fid for o, _, _ in an.instances[sha]))
        renamed_frac = renamed / max(1, dup_contents)
        hint, why = name_hint(f.name)
        ev: list[str] = []
        big = dup_contents >= MIN_STRUCTURAL
        many = big and cover >= 4
        inside = big and inside_source
        slice_ = big and share_of_main < 0.4
        if hint == CURATION or (frac >= 0.9 and (many or inside or slice_)):
            if why:
                ev.append(f'name says "{why}"')
            if many:
                ev.append(f"{dup_contents} photos drawn from {cover} different folders")
            if inside:
                ev.append(f"a selection inside its own source, {main_f.relpath}")
            elif slice_:
                ev.append(f"{shared} of the {len(main_f.contents)} photos in {main_f.relpath or 'the root'}")
            role = hint if hint in (PURPOSE, RECEIVED, SHARED) else CURATION
            out[fid] = Role(role, ev, role in ASKS_ABOUT_SHARING)
        elif hint in (PURPOSE, RECEIVED, SHARED):
            out[fid] = Role(hint, [f'name says "{why}"',
                                   f"{f.dup_files} of {hashed} files also in {main_f.relpath or 'the root'}"], True)
        elif renamed_frac >= 0.5 and big:
            out[fid] = Role(REFILED, [f"{renamed} of {dup_contents} shared photos have different file names in "
                                      f"{main_f.relpath or 'the root'}"])
        elif frac >= 0.9 and share_of_main >= 0.8:
            out[fid] = Role(BACKUP, [f"holds {share_of_main:.0%} of {main_f.relpath or 'the root'}"])
    return out


def describe(r: Role) -> str:
    label = {CURATION: "Looks like a curated selection", PURPOSE: "Looks like a set made for a purpose",
             RECEIVED: "Looks like photos received from someone", SHARED: "Looks like a shared album",
             REFILED: "The same photos filed under other names", BACKUP: "Looks like a backup or re-import"}[r.role]
    return f"{label}: " + "; ".join(r.evidence) if r.evidence else label
