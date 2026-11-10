"""M0 spike B: which Apple tags link Live Photos and bursts, and what are they called?

Usage:
    python spike_apple_tags.py --corpus DIR [--exiftool PATH] [--results DIR]

Read-only. For every HEIC/JPEG/MOV in the corpus, extracts all tags and keeps
those whose names look like identifiers (ContentIdentifier, BurstUUID,
MediaGroupUUID, ...), plus the date tags the scanner will use. Then:

  - lists the exact group:tag names found, per file type, which is what
    docs/03 §3.3 needs pinned;
  - pairs stills and videos by ContentIdentifier and reports pairs, orphan
    stills, and orphan videos;
  - compares that against pairing by folder + file stem (the fallback);
  - groups bursts by BurstUUID.
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sys

import phspike as ph

ID_PATTERN = re.compile(r"(ContentIdentifier|BurstUUID|MediaGroupUUID|LivePhoto|ImageUniqueID|UUID|Identifier)", re.I)
DATE_TAGS = ("DateTimeOriginal", "OffsetTimeOriginal", "SubSecTimeOriginal", "CreateDate", "CreationDate", "Duration")
TYPES = ("heic", "jpeg", "video")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--exiftool", default="exiftool")
    ap.add_argument("--results")
    a = ap.parse_args()

    corpus = pathlib.Path(a.corpus).resolve()
    res = ph.results_dir(a.results, "apple-tags")
    files = [p for p in sorted(corpus.rglob("*")) if p.is_file() and ph.category(p) in TYPES]
    if not files:
        sys.exit(f"No HEIC/JPEG/MOV files under {corpus}")

    records = []
    tag_names = collections.defaultdict(collections.Counter)  # type -> Counter(group:tag)
    with ph.ExifTool(a.exiftool) as et:
        print(f"ExifTool {et.version}; reading {len(files)} files")
        for i in range(0, len(files), 50):
            batch = files[i:i + 50]
            out = et.json(["-a", "-G1", "-n", "-api", "LargeFileSupport=1", *map(str, batch)])
            by_path = {}
            for rec in out:
                if "SourceFile" in rec:
                    by_path[pathlib.Path(rec["SourceFile"]).resolve()] = rec
            for p in batch:
                rec = by_path.get(p.resolve())
                if rec is None:
                    print(f"  (no ExifTool output for {p.name})")
                    continue
                cat = ph.category(p)
                ids = {k: v for k, v in rec.items() if ID_PATTERN.search(k.split(":")[-1])}
                dates = {k: v for k, v in rec.items() if k.split(":")[-1] in DATE_TAGS}
                for k in ids:
                    tag_names[cat][k] += 1
                cid = next((v for k, v in ids.items() if k.split(":")[-1] == "ContentIdentifier"), None)
                burst = next((v for k, v in ids.items() if k.split(":")[-1] == "BurstUUID"), None)
                records.append({
                    "relpath": p.relative_to(corpus).as_posix(), "category": cat,
                    "make": rec.get("IFD0:Make") or rec.get("QuickTime:Make") or rec.get("Keys:Make"),
                    "model": rec.get("IFD0:Model") or rec.get("QuickTime:Model") or rec.get("Keys:Model"),
                    "ids": ids, "dates": dates, "content_id": cid, "burst_id": burst,
                })

    # Pair by ContentIdentifier.
    stills = [r for r in records if r["category"] in ("heic", "jpeg")]
    videos = [r for r in records if r["category"] == "video"]
    vid_by_cid = collections.defaultdict(list)
    for v in videos:
        if v["content_id"]:
            vid_by_cid[v["content_id"]].append(v)
    pairs, orphan_stills = [], []
    matched_videos = set()
    for s in stills:
        if not s["content_id"]:
            continue
        vs = vid_by_cid.get(s["content_id"], [])
        if vs:
            pairs.append({"still": s["relpath"], "videos": [v["relpath"] for v in vs],
                          "same_folder": all(pathlib.PurePosixPath(v["relpath"]).parent == pathlib.PurePosixPath(s["relpath"]).parent for v in vs)})
            matched_videos.update(v["relpath"] for v in vs)
        else:
            orphan_stills.append(s["relpath"])
    orphan_videos = [v["relpath"] for v in videos if v["content_id"] and v["relpath"] not in matched_videos]
    videos_without_cid = [v["relpath"] for v in videos if not v["content_id"]]

    # Fallback: folder + stem.
    def key(r):
        p = pathlib.PurePosixPath(r["relpath"])
        return (str(p.parent).casefold(), p.stem.casefold())
    vid_by_stem = {key(v): v for v in videos}
    stem_pairs = {(s["relpath"], vid_by_stem[key(s)]["relpath"]) for s in stills if key(s) in vid_by_stem}
    cid_pairs = {(p["still"], v) for p in pairs for v in p["videos"]}

    bursts = collections.defaultdict(list)
    for r in records:
        if r["burst_id"]:
            bursts[r["burst_id"]].append(r["relpath"])

    summary = {
        "env": ph.env_info(),
        "files": len(records),
        "identifier_tags_by_type": {k: dict(v) for k, v in tag_names.items()},
        "live_pairs_by_content_id": len(pairs),
        "live_pairs_by_stem": len(stem_pairs),
        "stem_pairs_not_confirmed_by_content_id": sorted(map(list, stem_pairs - cid_pairs)),
        "content_id_pairs_not_found_by_stem": sorted(map(list, cid_pairs - stem_pairs)),
        "orphan_stills_with_content_id": orphan_stills,
        "orphan_videos_with_content_id": orphan_videos,
        "videos_without_content_id": videos_without_cid,
        "bursts": {k: v for k, v in bursts.items()},
        "pairs": pairs,
    }
    ph.write_json(res / "summary.json", summary)
    ph.write_json(res / "records.json", records)

    print("\nIdentifier-like tags found (group:tag -> files):")
    for cat, ctr in sorted(tag_names.items()):
        print(f"  {cat}:")
        for k, n in ctr.most_common():
            print(f"    {k:<45} {n}")
    print(f"\nLive Photo pairs by ContentIdentifier: {len(pairs)}  (by folder+stem: {len(stem_pairs)})")
    print(f"  stem pairs NOT confirmed by ContentIdentifier: {len(summary['stem_pairs_not_confirmed_by_content_id'])}")
    print(f"  ContentIdentifier pairs the stem rule would miss: {len(summary['content_id_pairs_not_found_by_stem'])}")
    print(f"  orphan stills (have ID, no video): {len(orphan_stills)}")
    print(f"  orphan videos (have ID, no still): {len(orphan_videos)}")
    print(f"  videos with no ContentIdentifier: {len(videos_without_cid)}")
    print(f"Bursts by BurstUUID: {len(bursts)} groups, {sum(len(v) for v in bursts.values())} files")
    print(f"\nResults: {res}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
