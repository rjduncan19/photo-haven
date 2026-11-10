"""M0 spike A: is ExifTool's ImageDataHash a stable content hash? (decision D-05)

Usage:
    python spike_content_hash.py --corpus DIR [--exiftool PATH] [--results DIR] [--limit N]

For every image/video in the corpus, on a scratch COPY (the corpus itself is
never modified):

  1. Compute the file SHA-256 and ImageDataHash (SHA-256); repeat the latter to check it's deterministic.
  2. Write a rating plus the provenance fields we'd embed (XMP-xmp:Rating,
     XMP-et:OriginalImageHash/Type, XMP-phv:IngestFileSHA256) in ONE ExifTool write.
  3. Change the rating again (a second edit).
  4. Change an EXIF field (ImageDescription).
  After each edit: the file SHA-256 should change; ImageDataHash should NOT.
  5. Truncate a copy to half its size and record what ImageDataHash does.

Also measures throughput, and whether file size can stay the same after a
metadata write. That matters for the cache's "size + mtime unchanged" rule.

Outputs content_hash.csv and summary.json in the results folder.
"""
from __future__ import annotations

import argparse
import collections
import csv
import pathlib
import shutil
import sys
import time

import phspike as ph

HERE = pathlib.Path(__file__).resolve().parent
CONFIG = HERE.parent.parent / "spec" / "exiftool" / "photohaven.config"
TESTED = ("heic", "jpeg", "raw", "video", "png")


def write_tags(et: ph.ExifTool, path: pathlib.Path, tags: dict[str, str]) -> tuple[bool, str]:
    args = [f"-{k}={v}" for k, v in tags.items()] + ["-overwrite_original", str(path)]
    out = et.run(args)
    ok = "1 image files updated" in out
    return ok, " ".join(out.split())


def read_back(et: ph.ExifTool, path: pathlib.Path) -> dict:
    res = et.json(["-XMP-xmp:Rating", "-XMP-et:OriginalImageHash", "-XMP-et:OriginalImageHashType",
                   "-XMP-phv:IngestFileSHA256", "-G1", "-n", str(path)])
    return res[0] if res else {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--exiftool", default="exiftool")
    ap.add_argument("--results")
    ap.add_argument("--limit", type=int, default=0, help="max files per type (0 = all)")
    a = ap.parse_args()

    corpus = pathlib.Path(a.corpus).resolve()
    res = ph.results_dir(a.results, "content-hash")
    work = res / "work"
    work.mkdir(exist_ok=True)

    files = []
    per_cat = collections.Counter()
    for p in sorted(corpus.rglob("*")):
        c = ph.category(p) if p.is_file() else None
        if c in TESTED and (a.limit == 0 or per_cat[c] < a.limit):
            files.append((p, c))
            per_cat[c] += 1
    if not files:
        sys.exit(f"No image/video files under {corpus}")
    print(f"{len(files)} files: " + ", ".join(f"{k}={v}" for k, v in sorted(per_cat.items())))

    rows = []
    with ph.ExifTool(a.exiftool, config=str(CONFIG)) as et:
        print(f"ExifTool {et.version}, config {CONFIG.name}")
        if ph.version_tuple(et.version) < (12, 62):
            print("WARNING: ExifTool < 12.62 cannot hash HEIC image data")
        for i, (src, cat) in enumerate(files, 1):
            w = work / f"{i:04d}_{src.name}"
            shutil.copy2(src, w)
            r = {"file": src.relative_to(corpus).as_posix(), "category": cat, "size": w.stat().st_size}

            t0 = time.perf_counter()
            r["sha_before"] = ph.sha256_file(w)
            r["sha_s"] = time.perf_counter() - t0
            idh, dt, warn = et.image_data_hash(w)
            idh_again, _, _ = et.image_data_hash(w)
            r.update(idh_before=idh, idh_s=dt, idh_warning=warn, idh_deterministic=(idh == idh_again))

            # Edit 1: one write carrying the rating and the provenance fields.
            ok1, msg1 = write_tags(et, w, {
                "XMP-xmp:Rating": "4",
                "XMP-et:OriginalImageHash": idh or "",
                "XMP-et:OriginalImageHashType": "SHA256",
                "XMP-phv:IngestFileSHA256": r["sha_before"],
            })
            r.update(write1_ok=ok1, write1_msg=msg1 if not ok1 else "")
            r["size_after_write1"] = w.stat().st_size
            r["sha_after1"] = ph.sha256_file(w)
            r["idh_after1"], _, _ = et.image_data_hash(w)
            rb = read_back(et, w)
            r["readback_rating"] = rb.get("XMP-xmp:Rating")
            r["readback_et_hash_ok"] = rb.get("XMP-et:OriginalImageHash") == idh and idh is not None
            r["readback_phv_ok"] = rb.get("XMP-phv:IngestFileSHA256") == r["sha_before"]

            # Edit 2: change the rating.
            ok2, _ = write_tags(et, w, {"XMP-xmp:Rating": "2"})
            r["write2_ok"] = ok2
            r["idh_after2"], _, _ = et.image_data_hash(w)

            # Edit 3: change an EXIF field.
            ok3, msg3 = write_tags(et, w, {"EXIF:ImageDescription": "Photo Haven spike"})
            r.update(write3_ok=ok3, write3_msg=msg3 if not ok3 else "")
            r["idh_after3"], _, _ = et.image_data_hash(w)

            # Truncation: what happens on a damaged file?
            t = work / f"{i:04d}_trunc_{src.name}"
            shutil.copy2(src, t)
            with open(t, "r+b") as f:
                f.truncate(max(1, r["size"] // 2))
            r["idh_truncated"], _, r["truncated_warning"] = et.image_data_hash(t)
            t.unlink()

            idhs = [r["idh_before"], r["idh_after1"]] + ([r["idh_after2"]] if ok2 else []) + ([r["idh_after3"]] if ok3 else [])
            r["idh_stable"] = r["idh_before"] is not None and len(set(idhs)) == 1
            r["sha_changed"] = r["sha_after1"] != r["sha_before"]
            r["size_unchanged_after_write"] = ok1 and r["size_after_write1"] == r["size"]
            r["verdict"] = (
                "NO_HASH" if r["idh_before"] is None else
                "WRITE_FAILED" if not ok1 else
                "PASS" if (r["idh_stable"] and r["sha_changed"] and r["idh_deterministic"]) else
                "FAIL"
            )
            rows.append(r)
            w.unlink()
            print(f"  [{r['verdict']:<12}] {cat:<6} {r['file']}")

    with open(res / "content_hash.csv", "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wr.writeheader()
        wr.writerows(rows)

    summary = {"env": ph.env_info(), "exiftool": et.version, "by_category": {}}
    print("\nSummary by type")
    print(f"  {'type':<6} {'n':>4} {'PASS':>5} {'FAIL':>5} {'NOHASH':>6} {'WFAIL':>5} {'phv_ok':>6} {'same-size':>9} {'sha MB/s':>9} {'idh MB/s':>9}")
    for cat in TESTED:
        rs = [r for r in rows if r["category"] == cat]
        if not rs:
            continue
        mb = sum(r["size"] for r in rs) / 1e6
        s = {
            "n": len(rs),
            "verdicts": dict(collections.Counter(r["verdict"] for r in rs)),
            "phv_readback_ok": sum(bool(r["readback_phv_ok"]) for r in rs),
            "et_readback_ok": sum(bool(r["readback_et_hash_ok"]) for r in rs),
            "size_unchanged_after_write": sum(bool(r["size_unchanged_after_write"]) for r in rs),
            "truncated_returned_hash": sum(r["idh_truncated"] is not None for r in rs),
            "sha_mb_s": mb / max(1e-9, sum(r["sha_s"] for r in rs)),
            "idh_mb_s": mb / max(1e-9, sum(r["idh_s"] for r in rs)),
        }
        summary["by_category"][cat] = s
        v = s["verdicts"]
        print(f"  {cat:<6} {s['n']:>4} {v.get('PASS', 0):>5} {v.get('FAIL', 0):>5} {v.get('NO_HASH', 0):>6} "
              f"{v.get('WRITE_FAILED', 0):>5} {s['phv_readback_ok']:>6} {s['size_unchanged_after_write']:>9} "
              f"{s['sha_mb_s']:>9.0f} {s['idh_mb_s']:>9.0f}")
    ph.write_json(res / "summary.json", summary)
    shutil.rmtree(work, ignore_errors=True)
    print(f"\nResults: {res}")
    print("D-05 holds if every type you care about is all PASS. Any 'same-size' count > 0 means a "
          "metadata write can leave file size unchanged (the cache must not rely on size alone).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
