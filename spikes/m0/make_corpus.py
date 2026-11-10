"""M0 step 1: build a small test corpus by COPYING a sample out of a photo folder.

Usage:
    python make_corpus.py --source DIR --dest DIR [--per-type 25] [--max-bytes 5GB] [--seed 1]
    python make_corpus.py --synthetic 50 --dest DIR      # generated JPEGs only (needs Pillow)

The source is only read, never written. Files are copied (timestamps kept)
into --dest, keeping their relative folder paths, so Live Photo, raw+JPEG,
and iOS edit pairs stay side by side. Companions are pulled in automatically:
picking IMG_1234.HEIC also copies IMG_1234.MOV, IMG_E1234.HEIC, IMG_1234.AAE
from the same folder if they exist.

OneDrive cloud-only placeholders are skipped (reading them would download
them) unless --hydrate is given.

A manifest.json in --dest lists every copied file with size and SHA-256.
"""
from __future__ import annotations

import argparse
import collections
import os
import pathlib
import random
import re
import shutil
import sys

import phspike as ph

PICK_CATEGORIES = ("heic", "jpeg", "raw", "video", "png")


def parse_size(s: str) -> int:
    m = re.fullmatch(r"\s*([\d.]+)\s*([KMGT]?B?)\s*", s, re.I)
    if not m:
        raise argparse.ArgumentTypeError(f"bad size {s!r}")
    mult = {"": 1, "B": 1, "K": 1 << 10, "KB": 1 << 10, "M": 1 << 20, "MB": 1 << 20,
            "G": 1 << 30, "GB": 1 << 30, "T": 1 << 40, "TB": 1 << 40}[m.group(2).upper()]
    return int(float(m.group(1)) * mult)


def companion_keys(stem: str) -> set[str]:
    """Stems that belong with this one: IMG_1234 <-> IMG_E1234."""
    s = stem.casefold()
    keys = {s}
    m = re.fullmatch(r"(img_)(e?)(\d+)", s)
    if m:
        keys |= {f"img_{m.group(3)}", f"img_e{m.group(3)}"}
    return keys


def build_from_source(a) -> int:
    src = pathlib.Path(a.source).resolve()
    dest = pathlib.Path(a.dest).resolve()
    if not src.is_dir():
        sys.exit(f"--source {src} is not a folder")
    if ph.is_inside(dest, src):
        sys.exit("--dest must not be inside --source")
    if dest.exists() and any(dest.iterdir()):
        sys.exit(f"--dest {dest} exists and is not empty; pick a new folder")

    print(f"Listing {src} (read-only) ...")
    by_cat: dict[str, list[pathlib.Path]] = collections.defaultdict(list)
    folder_files: dict[pathlib.Path, list[pathlib.Path]] = collections.defaultdict(list)
    n = 0
    for root, dirs, files in os.walk(src):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        rp = pathlib.Path(root)
        for f in sorted(files):
            if f.startswith(".") or f.lower() in ("desktop.ini", "thumbs.db"):
                continue
            p = rp / f
            folder_files[rp].append(p)
            c = ph.category(p)
            if c in PICK_CATEGORIES:
                by_cat[c].append(p)
            n += 1
    print(f"  {n:,} files in {len(folder_files):,} folders: " +
          ", ".join(f"{c}={len(v):,}" for c, v in sorted(by_cat.items())))

    rng = random.Random(a.seed)
    picked: list[pathlib.Path] = []
    for c in PICK_CATEGORIES:
        cands = by_cat.get(c, [])[:]
        rng.shuffle(cands)
        per_folder = collections.Counter()
        count = 0
        for p in cands:
            if count >= a.per_type:
                break
            if per_folder[p.parent] >= a.max_per_folder:
                continue
            picked.append(p)
            per_folder[p.parent] += 1
            count += 1

    # Pull in companions from the same folder.
    chosen: dict[pathlib.Path, str] = {}
    for p in picked:
        chosen.setdefault(p, "picked")
        keys = companion_keys(p.stem)
        for q in folder_files[p.parent]:
            if q != p and q.stem.casefold() in keys:
                chosen.setdefault(q, "companion")

    manifest = {"env": ph.env_info(), "mode": "sample", "seed": a.seed, "files": [], "skipped": []}
    total = 0
    for p in sorted(chosen):
        st = p.stat()
        rel = p.relative_to(src).as_posix()
        if ph.is_placeholder(st) and not a.hydrate:
            manifest["skipped"].append({"relpath": rel, "reason": "cloud_only"})
            continue
        if total + st.st_size > a.max_bytes:
            manifest["skipped"].append({"relpath": rel, "reason": "max_bytes"})
            continue
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, out)
        sha = ph.sha256_file(out)
        total += st.st_size
        manifest["files"].append({
            "relpath": rel, "role": chosen[p], "category": ph.category(p) or "other",
            "size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": sha,
        })
    ph.write_json(dest / "manifest.json", manifest)
    cats = collections.Counter(f["category"] for f in manifest["files"])
    print(f"Copied {len(manifest['files'])} files ({ph.human_bytes(total)}) to {dest}")
    print("  by type: " + ", ".join(f"{k}={v}" for k, v in sorted(cats.items())))
    if manifest["skipped"]:
        print(f"  skipped {len(manifest['skipped'])} (see manifest.json)")
    missing = [c for c in PICK_CATEGORIES if cats.get(c, 0) == 0]
    if missing:
        print(f"  NOTE: no {', '.join(missing)} files found; those spike checks will have nothing to test")
    return 0


def build_synthetic(a) -> int:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        sys.exit("--synthetic needs Pillow: pip install pillow")
    dest = pathlib.Path(a.dest)
    if dest.exists() and any(dest.iterdir()):
        sys.exit(f"--dest {dest} exists and is not empty; pick a new folder")
    out_dir = dest / "synthetic"
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(a.seed)
    manifest = {"env": ph.env_info(), "mode": "synthetic", "seed": a.seed, "files": [], "skipped": []}
    for i in range(a.synthetic):
        img = Image.new("RGB", (1600, 1200), tuple(rng.randrange(256) for _ in range(3)))
        d = ImageDraw.Draw(img)
        for _ in range(40):  # noise so files aren't trivially compressible or identical
            x, y = rng.randrange(1600), rng.randrange(1200)
            d.rectangle([x, y, x + rng.randrange(200), y + rng.randrange(200)],
                        fill=tuple(rng.randrange(256) for _ in range(3)))
        d.text((40, 40), f"PHOTO HAVEN TEST {i:04d}", fill=(255, 255, 255))
        exif = Image.Exif()
        ifd = exif.get_ifd(0x8769)
        ifd[0x9003] = f"2015:07:{1 + i % 28:02d} 12:{i % 60:02d}:00"  # DateTimeOriginal
        exif[0x010F] = "PhotoHavenTest"   # Make
        exif[0x0110] = "Synthetic"        # Model
        p = out_dir / f"IMG_{9000 + i:04d}.JPG"
        img.save(p, "JPEG", quality=90, exif=exif)
        st = p.stat()
        manifest["files"].append({"relpath": p.relative_to(dest).as_posix(), "role": "synthetic",
                                  "category": "jpeg", "size": st.st_size, "mtime_ns": st.st_mtime_ns,
                                  "sha256": ph.sha256_file(p)})
    ph.write_json(dest / "manifest.json", manifest)
    print(f"Generated {a.synthetic} JPEGs in {out_dir}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", help="photo folder to sample from (read-only)")
    ap.add_argument("--dest", required=True, help="new, empty folder for the corpus (outside --source)")
    ap.add_argument("--per-type", type=int, default=25, help="files to pick per type (heic, jpeg, raw, video, png)")
    ap.add_argument("--max-per-folder", type=int, default=3, help="spread picks across folders")
    ap.add_argument("--max-bytes", type=parse_size, default=parse_size("5GB"))
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--hydrate", action="store_true", help="allow reading OneDrive cloud-only files (downloads them)")
    ap.add_argument("--synthetic", type=int, metavar="N", help="generate N JPEGs instead of sampling")
    a = ap.parse_args()
    if a.synthetic:
        return build_synthetic(a)
    if not a.source:
        ap.error("--source is required unless --synthetic is given")
    return build_from_source(a)


if __name__ == "__main__":
    sys.exit(main())
