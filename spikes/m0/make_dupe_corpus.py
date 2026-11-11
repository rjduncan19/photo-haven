"""Build a duplicate-case test library with known answers, for M1 (scanner) and M2 (link/report).

Usage:
    python make_dupe_corpus.py build  --source DIR --dest DIR [--exiftool PATH] [--seed 1]
    python make_dupe_corpus.py verify --dest DIR [--exiftool PATH]
    python make_dupe_corpus.py mutate --dest DIR [--exiftool PATH]     # after a first scan
    python make_dupe_corpus.py verify --dest DIR                       # checks the mutated state

`build` picks a handful of base photos from --source (read-only; use a corpus
COPY such as the one make_corpus.py writes), and plants copies of them in a
messy-looking library under DEST/library:

  * exact duplicates: same folder "- Copy", other folder, renamed+moved,
    extension case/spelling, mtime changed, fresh ctime, hidden, read-only,
    NFC vs NFD names, a path longer than 260 characters, a fully redundant
    backup folder, a partially overlapping folder, a photo whose only copies
    are in backup folders (last-copy invariant)
  * content-equivalent (same ImageDataHash, different bytes): rating changed
    (with and without keeping mtime), keywords, DateTimeOriginal shifted,
    orientation, GPS added, all metadata stripped
  * different content: one byte flipped (same size!), same name as a
    different photo, truncated, zero bytes
  * a stray non-photo file in an otherwise redundant folder

Every file gets an entry in DEST/expected.json:
    path          relpath under library/ (forward slashes)
    case          case id (see CASES)
    base          which base photo it derives from (null for unrelated files)
    exact         label; files with the same label MUST have equal SHA-256,
                  different labels MUST differ
    content       label; same rule for ImageDataHash (null = no hash expected)
    damaged       true / false / null (null = either is acceptable)
    note          what the case is testing

`verify` recomputes SHA-256, ImageDataHash and damage for every file and
checks them against expected.json. It is the reference answer the product's
scanner must also reproduce.

`mutate` applies rescan cases in place (move, rename, touch, rating edit,
silent corruption with mtime restored, delete, new copy) and rewrites
expected.json, including an `expect_rescan` hint per changed file.

Stdlib only, plus ExifTool. Windows first (hidden/read-only attributes and the
long path use Windows APIs; they are skipped elsewhere).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import random
import shutil
import stat
import sys
import unicodedata

import phspike as ph

LONG_PREFIX = "\\\\?\\" if ph.IS_WINDOWS else ""

CASES = {
    # exact duplicates
    "base": "the original, in its 'home' folder",
    "copy_same_folder": "Explorer-style '- Copy' in the same folder",
    "copy_paren": "browser/download-style ' (1)' suffix",
    "copy_other_folder": "same name, other folder (a backup or re-import)",
    "renamed_moved": "renamed to a date-based name and moved (Photos-app style import)",
    "ext_case": "extension case changed (.JPG -> .jpg)",
    "ext_spelling": "extension spelling changed (.jpg -> .jpeg)",
    "mtime_changed": "same bytes, mtime set years later (copied by a tool that doesn't keep times)",
    "fresh_times": "copied without keeping times (ctime and mtime = now)",
    "hidden": "hidden attribute set",
    "readonly": "read-only attribute set (apply must still be able to move it)",
    "nfc_name": "name with é in NFC form",
    "nfd_name": "same name with é in NFD form, same folder: a distinct NTFS name that collides after NFC+casefold",
    "long_path": "path longer than 260 characters",
    "backup_full": "inside a backup folder that is fully redundant",
    "backup_partial": "inside a folder that is only partly redundant",
    "last_copy": "every copy of this photo is in a backup folder: one must be kept",
    # content-equivalent
    "rating": "XMP rating set (mtime updated)",
    "rating_keep_mtime": "XMP rating set with mtime preserved (-P): size differs, mtime equal to the base's",
    "keywords": "XMP keywords added",
    "date_shift": "DateTimeOriginal shifted +1h (time zone fix): same pixels, different capture time",
    "orientation": "Orientation changed: displays rotated, same encoded image data",
    "gps_added": "GPS added",
    "stripped": "all metadata stripped",
    # different content / damage
    "byte_flip": "one byte flipped in the image data: same size and name, different content",
    "name_collision": "same file name as the base, but a different photo",
    "truncated": "truncated to half: ExifTool still returns a hash, must be flagged damaged",
    "zero_bytes": "zero-byte file with a photo name",
    "stray_other": "a non-photo file that keeps its folder from being fully redundant",
}


def sh(path: pathlib.Path) -> str:
    """Path string usable for long paths on Windows."""
    p = str(path.resolve())
    return LONG_PREFIX + p if LONG_PREFIX and not p.startswith(LONG_PREFIX) else p


def copy(src: pathlib.Path, dst: pathlib.Path, keep_times: bool = True) -> None:
    os.makedirs(sh(dst.parent), exist_ok=True)
    (shutil.copy2 if keep_times else shutil.copyfile)(sh(src), sh(dst))


def set_attr(path: pathlib.Path, readonly=False, hidden=False) -> None:
    if readonly:
        os.chmod(sh(path), stat.S_IREAD)
    if hidden:
        ph.set_hidden(pathlib.Path(sh(path)))


def et_write(et: ph.ExifTool, path: pathlib.Path, args: list[str], keep_mtime=False) -> None:
    out = et.run([*args, *(["-P"] if keep_mtime else []), "-overwrite_original", str(path)])
    if "1 image files updated" not in out:
        raise SystemExit(f"ExifTool write failed on {path}: {' '.join(out.split())}")


def pick_bases(source: pathlib.Path, seed: int) -> dict[str, pathlib.Path]:
    """Choose base photos: two older camera JPEGs, an iPhone JPEG, two HEICs, a video, a PNG."""
    rng = random.Random(seed)
    files = [p for p in sorted(source.rglob("*")) if p.is_file() and p.stat().st_size > 0]
    by = lambda cat, pred=lambda p: True: [p for p in files if ph.category(p) == cat and pred(p)]
    iphone = lambda p: "_iOS" in p.name
    groups = {
        "jpg_a": by("jpeg", lambda p: not iphone(p)),
        "jpg_b": by("jpeg", lambda p: not iphone(p)),
        "jpg_c": by("jpeg", iphone),
        "heic_a": by("heic"),
        "heic_b": by("heic"),
        "mov": [p for p in by("video") if p.suffix.lower() == ".mov"] or by("video"),
        "png": by("png"),
    }
    chosen: dict[str, pathlib.Path] = {}
    used: set[pathlib.Path] = set()
    for key, pool in groups.items():
        pool = [p for p in pool if p not in used and p.stat().st_size < 60_000_000]
        if not pool:
            print(f"  note: no source file for {key}; its cases are skipped")
            continue
        chosen[key] = rng.choice(pool)
        used.add(chosen[key])
    return chosen


class Builder:
    def __init__(self, lib: pathlib.Path, et: ph.ExifTool):
        self.lib, self.et, self.entries, self.n_variant = lib, et, [], 0

    def add(self, rel: str, case: str, base: str | None, exact: str | None, content: str | None,
            damaged: bool | None = False, note: str | None = None) -> pathlib.Path:
        self.entries.append({"path": rel, "case": case, "base": base, "exact": exact,
                             "content": content, "damaged": damaged, "note": note or CASES[case]})
        return self.lib / rel

    def variant(self, base: str) -> str:
        self.n_variant += 1
        return f"X-{base}-v{self.n_variant}"


def build(a) -> int:
    source, dest = pathlib.Path(a.source).resolve(), pathlib.Path(a.dest).resolve()
    if dest.exists() and any(dest.iterdir()):
        sys.exit(f"{dest} is not empty; pick a new folder")
    if ph.is_inside(dest, source):
        sys.exit("--dest must be outside --source")
    lib = dest / "library"
    lib.mkdir(parents=True)

    bases = pick_bases(source, a.seed)
    print("Base photos:")
    for k, p in bases.items():
        print(f"  {k:<7} {p.relative_to(source)}")

    with ph.ExifTool(a.exiftool) as et:
        b = Builder(lib, et)
        home = {
            "jpg_a": "Pictures/Camera/Event A",
            "jpg_b": "Pictures/Camera/Event B",
            "jpg_c": "Pictures/iPhone/2026-10",
            "heic_a": "Pictures/iPhone/2026-10",
            "heic_b": "Pictures/iPhone/2026-10",
            "mov": "Pictures/iPhone/2026-10",
            "png": "Pictures/Screenshots",
        }
        orig: dict[str, pathlib.Path] = {}

        # --- originals -------------------------------------------------------
        for k, src in bases.items():
            if k == "jpg_b":
                continue  # jpg_b's only copies live in backups (last_copy), planted below
            rel = f"{home[k]}/{src.name}"
            copy(src, b.add(rel, "base", k, f"X-{k}", f"C-{k}"))
            orig[k] = lib / rel

        def exact(k, rel, case, keep_times=True, **attrs):
            src = orig.get(k) or bases[k]
            p = b.add(rel, case, k, f"X-{k}", f"C-{k}")
            copy(src, p, keep_times=keep_times)
            if attrs:
                set_attr(p, **attrs)
            return p

        # --- exact duplicates -------------------------------------------------
        for k in ("jpg_a", "heic_a", "png"):
            if k not in orig:
                continue
            n, stem, ext = orig[k].name, orig[k].stem, orig[k].suffix
            exact(k, f"{home[k]}/{stem} - Copy{ext}", "copy_same_folder")
            exact(k, f"Downloads/{stem} (1){ext}", "copy_paren")
            exact(k, f"Old Laptop/My Pictures/{n}", "copy_other_folder")
            exact(k, f"Imported 2021-03-15/{k} import 0001{ext.lower()}", "renamed_moved")
            exact(k, f"Misc/{stem}{ext.swapcase()}", "ext_case")

        if "jpg_a" in orig:
            stem = orig["jpg_a"].stem
            exact("jpg_a", f"Misc/{stem}.jpeg", "ext_spelling")
            p = exact("jpg_a", f"Synced From Phone/{orig['jpg_a'].name}", "mtime_changed")
            t = dt.datetime(2019, 6, 1, 12, 0, 0, 123456).timestamp()
            os.utime(sh(p), (t, t))
            exact("jpg_a", f"Email Attachments/{orig['jpg_a'].name}", "fresh_times", keep_times=False)
            exact("jpg_a", f"Hidden Stuff/{orig['jpg_a'].name}", "hidden", hidden=True)
            exact("jpg_a", f"Read Only/{orig['jpg_a'].name}", "readonly", readonly=True)
            exact("jpg_a", f"Unicode/{unicodedata.normalize('NFC', 'Café')}.jpg", "nfc_name")
            exact("jpg_a", f"Unicode/{unicodedata.normalize('NFD', 'Café')}.jpg", "nfd_name")
            deep = "/".join(["A very long folder name that someone made by exporting albums"] * 5)
            exact("jpg_a", f"Deep/{deep}/{orig['jpg_a'].name}", "long_path")

        # Fully redundant backup: every file in the iPhone folder, some renamed.
        for i, k in enumerate(("jpg_c", "heic_a", "heic_b", "mov")):
            if k in orig:
                name = orig[k].name if i % 2 == 0 else f"IMG_{1000 + i}{orig[k].suffix}"
                exact(k, f"Backup 2026 (phone)/{name}", "backup_full")

        # Partly redundant: two copies plus one unique variant.
        for k in ("jpg_c", "heic_b"):
            if k in orig:
                exact(k, f"Shared Album/{orig[k].name}", "backup_partial")

        # Last copy: jpg_b exists only in two backup folders.
        if "jpg_b" in bases:
            for rel in (f"Backup 2019/{bases['jpg_b'].name}", f"Backup 2020/Pictures/{bases['jpg_b'].name}"):
                p = b.add(rel, "last_copy", "jpg_b", "X-jpg_b", "C-jpg_b")
                copy(bases["jpg_b"], p)

        # Stray non-photo file in the otherwise fully redundant backup.
        stray = b.add("Backup 2026 (phone)/notes.txt", "stray_other", None, "X-stray", None, damaged=None)
        stray.write_text("trip notes\n", encoding="utf-8")

        # --- content-equivalent ----------------------------------------------
        meta_cases = [
            ("rating", ["-XMP-xmp:Rating=5"], False),
            ("rating_keep_mtime", ["-XMP-xmp:Rating=3"], True),
            ("keywords", ["-XMP-dc:Subject=family", "-XMP-dc:Subject=gnomes"], False),
            ("date_shift", ["-AllDates+=1"], False),
            ("orientation", ["-Orientation#=6"], False),
            ("gps_added", ["-GPSLatitude=47.6205", "-GPSLatitudeRef=N", "-GPSLongitude=122.3493", "-GPSLongitudeRef=W"], False),
            ("stripped", ["-all="], False),
        ]
        for k in ("jpg_a", "heic_a", "mov"):
            if k not in orig:
                continue
            for case, args, keep in meta_cases:
                if k == "mov" and case not in ("rating", "keywords"):
                    continue  # EXIF-style date/orientation/GPS/strip don't map cleanly onto QuickTime; keep it small
                if k == "heic_a" and case == "stripped":
                    continue  # -all= on HEIC removes required boxes on some files; not a realistic edit
                p = b.add(f"Edited/{k}/{case}{orig[k].suffix}", case, k, b.variant(k), f"C-{k}")
                copy(orig[k], p)
                et_write(et, p, args, keep_mtime=keep)

        # --- different content / damage --------------------------------------
        if "jpg_a" in orig:
            src = orig["jpg_a"]
            p = b.add(f"Damaged/flip/{src.name}", "byte_flip", "jpg_a", b.variant("jpg_a"), "C-jpg_a-flip",
                      damaged=None, note=CASES["byte_flip"] + " (ExifTool may or may not notice)")
            copy(src, p)
            with open(sh(p), "r+b") as f:
                size = os.fstat(f.fileno()).st_size
                f.seek(int(size * 0.6))
                c = f.read(1)
                f.seek(-1, 1)
                f.write(bytes([c[0] ^ 0x01]))
            st = src.stat()
            os.utime(sh(p), ns=(st.st_atime_ns, st.st_mtime_ns))

            p = b.add(f"Damaged/{src.stem}_half{src.suffix}", "truncated", "jpg_a", b.variant("jpg_a"),
                      "C-jpg_a-trunc", damaged=True)
            copy(src, p)
            with open(sh(p), "r+b") as f:
                f.truncate(src.stat().st_size // 2)

            p = b.add(f"Damaged/{src.name}", "zero_bytes", "jpg_a", "X-empty", None, damaged=None)
            os.makedirs(sh(p.parent), exist_ok=True)
            open(sh(p), "wb").close()

        if "jpg_a" in orig and "jpg_c" in orig:
            # A different photo carrying jpg_a's file name.
            p = b.add(f"Other Camera/{orig['jpg_a'].name}", "name_collision", "jpg_c", "X-jpg_c", "C-jpg_c")
            copy(orig["jpg_c"], p)

    manifest = {"schema": "ph-dupe-corpus/1", "created": ph.utc_now(), "source": str(source),
                "seed": a.seed, "bases": {k: str(v.relative_to(source).as_posix()) for k, v in bases.items()},
                "cases": CASES, "phase": "build", "files": sorted(b.entries, key=lambda e: e["path"])}
    ph.write_json(dest / "expected.json", manifest)
    print(f"Planted {len(b.entries)} files in {lib}")
    print(f"Expected answers: {dest / 'expected.json'}")
    print("Next: python make_dupe_corpus.py verify --dest " + str(dest))
    return 0


def walk(lib: pathlib.Path) -> list[str]:
    out = []
    root = sh(lib)
    for d, _, fs in os.walk(root):
        for f in fs:
            full = os.path.join(d, f)
            out.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return sorted(out)


def verify(a) -> int:
    dest = pathlib.Path(a.dest).resolve()
    lib = dest / "library"
    exp = json.loads((dest / "expected.json").read_text(encoding="utf-8"))
    entries = {e["path"]: e for e in exp["files"]}
    on_disk = walk(lib)
    problems = []
    missing = sorted(set(entries) - set(on_disk))
    extra = sorted(set(on_disk) - set(entries))
    problems += [f"missing on disk: {p}" for p in missing] + [f"not in expected.json: {p}" for p in extra]

    actual = {}
    with ph.ExifTool(a.exiftool) as et:
        for rel in sorted(set(entries) & set(on_disk)):
            p = pathlib.Path(sh(lib / rel))           # \?\ form for Python file I/O
            is_media = ph.category(lib / rel) not in (None, "sidecar")
            size = os.stat(p).st_size
            # ExifTool gets the plain path; long paths go through -api WindowsLongPath (see phspike.ExifTool.run).
            h = et.hash_check(lib / rel) if (is_media and size) else {"hash": None, "damaged": False, "serious": [], "suspect": []}
            actual[rel] = {"sha": ph.sha256_file(p), "idh": h["hash"], "damaged": h["damaged"],
                           "serious": h["serious"], "size": size}

    def check_labels(field, val):
        lab_to_vals, val_to_labs = {}, {}
        for rel, e in entries.items():
            if rel not in actual or e[field] is None:
                continue
            v = actual[rel][val]
            if v is None:
                problems.append(f"{rel}: expected a {val} for label {e[field]}, got none")
                continue
            lab_to_vals.setdefault(e[field], set()).add(v)
            val_to_labs.setdefault(v, set()).add(e[field])
        for lab, vs in lab_to_vals.items():
            if len(vs) > 1:
                problems.append(f"{field} label {lab}: members differ ({len(vs)} distinct {val} values)")
        for v, labs in val_to_labs.items():
            if len(labs) > 1:
                problems.append(f"{val} {v[:12]}... shared by labels {sorted(labs)}")

    check_labels("exact", "sha")
    check_labels("content", "idh")
    for rel, e in entries.items():
        if rel in actual:
            if e["content"] is None and actual[rel]["idh"] and e["case"] != "stray_other":
                problems.append(f"{rel}: expected no content hash, got one")
            if e["damaged"] is not None and actual[rel]["damaged"] != e["damaged"]:
                problems.append(f"{rel}: damaged={actual[rel]['damaged']} expected {e['damaged']} {actual[rel]['serious']}")

    # Facts a scanner test can assert directly.
    sizes = {}
    for rel, r in actual.items():
        sizes.setdefault(r["size"], set()).add(r["sha"])
    same_size_diff = sum(1 for s in sizes.values() if len(s) > 1)
    exact_groups = {}
    for rel, r in actual.items():
        exact_groups.setdefault(r["sha"], []).append(rel)
    dup_groups = [g for g in exact_groups.values() if len(g) > 1]
    content_groups = {}
    for rel, r in actual.items():
        if r["idh"] and not r["damaged"]:
            content_groups.setdefault(r["idh"], set()).add(r["sha"])
    equiv = sum(1 for s in content_groups.values() if len(s) > 1)
    flip = [r for r, e in entries.items() if e["case"] == "byte_flip" and r in actual]
    print(f"Files: {len(on_disk)} on disk, {len(entries)} expected")
    print(f"Exact-duplicate groups: {len(dup_groups)} ({sum(len(g) for g in dup_groups)} files, "
          f"{sum(len(g) - 1 for g in dup_groups)} redundant copies)")
    print(f"Content-equivalent groups (same image data, different bytes): {equiv}")
    print(f"Sizes shared by different content: {same_size_diff}")
    for r in flip:
        print(f"byte_flip damaged by ExifTool's account: {actual[r]['damaged']} {actual[r]['serious']}")
    ph.write_json(dest / "actual.json", {"checked": ph.utc_now(), "phase": exp.get("phase"), "files": actual})
    if problems:
        print(f"\n{len(problems)} PROBLEM(S):")
        for p in problems:
            print("  " + p)
        return 1
    print("\nOK: every expected group, content group and damage flag matches.")
    return 0


def mutate(a) -> int:
    """Rescan cases: change the library in place after a first scan."""
    dest = pathlib.Path(a.dest).resolve()
    lib = dest / "library"
    exp = json.loads((dest / "expected.json").read_text(encoding="utf-8"))
    if exp.get("phase") != "build":
        sys.exit("expected.json is not in the 'build' phase; mutate only once (rebuild to start over)")
    entries = {e["path"]: e for e in exp["files"]}
    by_case = {}
    for e in exp["files"]:
        by_case.setdefault((e["case"], e["base"]), e)

    def first(case, base=None):
        for (c, bk), e in by_case.items():
            if c == case and (base is None or bk == base):
                return e
        return None

    log = []

    def note(e, hint):
        e["expect_rescan"] = hint
        log.append(f"  {e['case']:<18} {e['path']}  ->  {hint}")

    with ph.ExifTool(a.exiftool) as et:
        # 1. Move a whole file to a new folder (same name).
        e = first("copy_other_folder", "jpg_a")
        if e:
            new = "Moved Later/" + pathlib.PurePosixPath(e["path"]).name
            os.makedirs(sh((lib / new).parent), exist_ok=True)
            os.rename(sh(lib / e["path"]), sh(lib / new))
            entries.pop(e["path"]); e["path"] = new; entries[new] = e
            note(e, "old path missing, new path found; same size+mtime, so SHA-256 can be matched by move detection or rehashed")
        # 2. Rename in place.
        e = first("copy_paren", "heic_a")
        if e:
            p = pathlib.PurePosixPath(e["path"])
            new = str(p.with_name("renamed later" + p.suffix))
            os.rename(sh(lib / e["path"]), sh(lib / new))
            entries.pop(e["path"]); e["path"] = new; entries[new] = e
            note(e, "rename: old path missing, new path found, same sha256")
        # 3. Touch: mtime changes, bytes don't.
        e = first("copy_same_folder", "jpg_a")
        if e:
            os.utime(sh(lib / e["path"]), None)
            note(e, "mtime changed only: cache miss, rehash gives the same sha256 (no new content)")
        # 4. Rating written in place (as Windows Explorer would): bytes change, content doesn't.
        e = first("copy_same_folder", "heic_a")
        if e:
            et_write(et, lib / e["path"], ["-XMP-xmp:Rating=4"])
            e["exact"] = f"X-heic_a-mut-rating"; e["case_after"] = "rating_in_place"
            note(e, "size+mtime changed: rehash; new sha256, same content_sha256 (moves from exact group to content-equivalent)")
        # 5. Silent corruption: flip a byte, restore size and mtime exactly.
        e = first("ext_case", "jpg_a")
        if e:
            p = lib / e["path"]
            st = os.stat(sh(p))
            with open(sh(p), "r+b") as f:
                f.seek(int(st.st_size * 0.7)); c = f.read(1); f.seek(-1, 1); f.write(bytes([c[0] ^ 0x01]))
            os.utime(sh(p), ns=(st.st_atime_ns, st.st_mtime_ns))
            e["exact"] = "X-jpg_a-mut-rot"; e["content"] = "C-jpg_a-mut-rot"; e["damaged"] = None
            e["case_after"] = "silent_corruption"
            note(e, "size AND mtime unchanged: a normal rescan reuses the stale cache (still claims the old sha256); "
                    "only scan --audit/--rehash finds it. Apply/purge byte compares must catch it.")
        # 6. Delete one copy (as the user might in Explorer).
        e = first("copy_paren", "png")
        if e:
            os.remove(sh(lib / e["path"]))
            entries.pop(e["path"])
            log.append(f"  deleted            {e['path']}  ->  rescan marks it missing; group shrinks by one")
        # 7. A new copy appears.
        src = first("base", "heic_b")
        if src:
            new = "New Since Last Scan/" + pathlib.PurePosixPath(src["path"]).name
            copy(lib / src["path"], lib / new)
            e = dict(src, path=new, case="new_copy", note="new file since last scan")
            entries[new] = e
            note(e, "new path: hashed, joins heic_b's exact group")

    exp["files"] = sorted(entries.values(), key=lambda e: e["path"])
    exp["phase"] = "mutated"
    exp["mutated"] = ph.utc_now()
    ph.write_json(dest / "expected.json", exp)
    print("Mutations applied:")
    print("\n".join(log))
    print("\nexpected.json updated (phase = mutated). Run verify again for the new ground truth.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("build", "verify", "mutate"):
        s = sub.add_parser(name)
        s.add_argument("--dest", required=True)
        s.add_argument("--exiftool", default="exiftool")
        if name == "build":
            s.add_argument("--source", required=True)
            s.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    return {"build": build, "verify": verify, "mutate": mutate}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
