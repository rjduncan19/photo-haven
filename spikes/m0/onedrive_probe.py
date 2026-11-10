"""M0 spike C: how does OneDrive treat our files and our moves?

Works only on a dedicated probe folder that this script creates, and only
ever moves or deletes files listed in that probe's own probe.json.

Subcommands (run `onedrive_probe.py <cmd> -h` for options):

  prepare  PARENT        Create PARENT/PhotoHaven-probe-<id>/ with N test files (random
                         bytes, or copies from a corpus), sub-second mtimes, and a hidden
                         dotfile like our cache files. PARENT should be inside OneDrive.
  snapshot PROBE         Record size, mtime (ns), attributes, and placeholder state of every
                         probe file. Run on machine A, then on machine B after sync.
  compare  A.json B.json Compare two snapshots: are mtimes identical across machines?
                         Did the hidden dotfile sync, and is it still hidden?
  move     PROBE --to DIR Move the probe files to DIR while sampling network upload. Inside
                         OneDrive: is a move a server-side move, or delete + re-upload?
                         Outside OneDrive: what does the client do with a mass removal?
  move-back PROBE        Move the files back from wherever `move` put them.
  watch-upload           Just sample upload bytes for N seconds (to measure background noise).
  cleanup  PROBE         Delete the probe's own files and folder (asks first).

What each question needs and how to read the results is in spikes/m0/README.md.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import random
import shutil
import subprocess
import sys
import time
import uuid

import phspike as ph

PROBE_PREFIX = "PhotoHaven-probe-"
DOTFILE = ".photohaven-cache.probe.jsonl"


# ---------------------------------------------------------------- helpers

def load_probe(probe: pathlib.Path) -> dict:
    pj = probe / "probe.json"
    if not pj.exists() or not probe.name.startswith(PROBE_PREFIX):
        sys.exit(f"{probe} is not a probe folder created by `prepare` (no probe.json)")
    return json.loads(pj.read_text(encoding="utf-8"))


def save_probe(probe: pathlib.Path, data: dict) -> None:
    ph.write_json(probe / "probe.json", data)


def sent_bytes() -> int | None:
    """Total bytes sent on all network adapters (Windows only)."""
    if not ph.IS_WINDOWS:
        return None
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-NetAdapterStatistics | Measure-Object -Property SentBytes -Sum).Sum"],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        return int(out)
    except Exception:
        return None


def sample_upload(seconds: int, interval: int = 5, label: str = "") -> dict:
    """Sample cumulative sent bytes; returns the series and the total."""
    start = sent_bytes()
    if start is None:
        print("  (upload sampling needs Windows PowerShell Get-NetAdapterStatistics; skipped)")
        return {"supported": False}
    series = [(0.0, 0)]
    t0 = time.monotonic()
    while (el := time.monotonic() - t0) < seconds:
        time.sleep(min(interval, max(0.5, seconds - el)))
        cur = sent_bytes()
        if cur is None:
            break
        d = cur - start
        series.append((round(time.monotonic() - t0, 1), d))
        print(f"\r  {label}{series[-1][0]:>6.0f}s  uploaded {ph.human_bytes(d):>12}", end="", flush=True)
    print()
    total = series[-1][1]
    dur = series[-1][0] or 1
    return {"supported": True, "seconds": dur, "bytes": total, "bytes_per_s": total / dur, "series": series}


def list_files(probe: pathlib.Path, data: dict) -> list[pathlib.Path]:
    return [probe / f["relpath"] for f in data["files"]]


def same_volume(a: pathlib.Path, b: pathlib.Path) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


# ---------------------------------------------------------------- commands

def cmd_prepare(a) -> int:
    parent = pathlib.Path(a.parent).resolve()
    if not parent.is_dir():
        sys.exit(f"{parent} is not a folder")
    probe_id = uuid.uuid4().hex[:8]
    probe = parent / f"{PROBE_PREFIX}{probe_id}"
    files_dir = probe / "files"
    files_dir.mkdir(parents=True)
    rng = random.Random(probe_id)

    sources = []
    if a.from_corpus:
        corpus = pathlib.Path(a.from_corpus)
        sources = [p for p in sorted(corpus.rglob("*")) if p.is_file() and ph.category(p)][: a.count]
        if not sources:
            sys.exit(f"No media files in {corpus}")

    now_ns = time.time_ns()
    files = []
    count = len(sources) if sources else a.count
    for i in range(count):
        if sources:
            src = sources[i]
            dst = files_dir / f"{i:04d}_{src.name}"
            shutil.copyfile(src, dst)
        else:
            dst = files_dir / f"probe_{i:04d}.bin"
            with open(dst, "wb") as f:
                f.write(rng.randbytes(a.size_kb * 1024))
        # Distinct sub-second mtimes, so lost precision is visible in `compare`.
        mt = now_ns - (count - i) * 1_000_000_000 - rng.randrange(1, 999_999_999)
        os.utime(dst, ns=(mt, mt))
        st = dst.stat()
        files.append({"relpath": dst.relative_to(probe).as_posix(), "size": st.st_size,
                      "mtime_ns": st.st_mtime_ns, "sha256": ph.sha256_file(dst)})

    dot = probe / DOTFILE
    dot.write_text(json.dumps({"record": "header", "schema": 1, "kind": "probe"}) + "\n", encoding="utf-8")
    hidden = ph.set_hidden(dot)

    data = {"probe_id": probe_id, "created": ph.env_info(), "files": files,
            "dotfile": DOTFILE, "dotfile_hidden_set": hidden, "moves": []}
    save_probe(probe, data)
    total = sum(f["size"] for f in files)
    print(f"Created {probe}")
    print(f"  {len(files)} files, {ph.human_bytes(total)}; hidden dotfile set: {hidden}")
    print("Next: wait until OneDrive shows the folder as synced, then run `snapshot` here and on the other machine.")
    return 0


def snapshot_dir(probe: pathlib.Path, do_hash: bool) -> list[dict]:
    out = []
    for p in sorted(probe.rglob("*")):
        if not p.is_file() or p.name.endswith(".tmp"):
            continue
        st = p.stat()
        attrs = ph.file_attrs(st)
        rec = {"relpath": p.relative_to(probe).as_posix(), "size": st.st_size,
               "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns,
               "attrs": attrs, "attr_names": ph.attr_names(attrs), "placeholder": ph.is_placeholder(st)}
        if do_hash and not rec["placeholder"] and p.name != "probe.json":
            rec["sha256"] = ph.sha256_file(p)
        out.append(rec)
    return out


def cmd_snapshot(a) -> int:
    probe = pathlib.Path(a.probe).resolve()
    data = load_probe(probe)
    label = a.label or ph.env_info()["machine"]
    snap = {"probe_id": data["probe_id"], "label": label, "env": ph.env_info(),
            "files": snapshot_dir(probe, a.hash)}
    res = ph.results_dir(a.results, "onedrive")
    path = res / f"snapshot-{data['probe_id']}-{label}.json"
    ph.write_json(path, snap)
    ph_count = sum(r["placeholder"] for r in snap["files"])
    dot = next((r for r in snap["files"] if r["relpath"] == DOTFILE), None)
    print(f"Snapshot of {len(snap['files'])} files ({ph_count} cloud-only placeholders) -> {path}")
    print(f"  dotfile present: {dot is not None}; hidden: {bool(dot and 'hidden' in dot['attr_names'])}")
    return 0


def cmd_compare(a) -> int:
    A = json.loads(pathlib.Path(a.a).read_text(encoding="utf-8"))
    B = json.loads(pathlib.Path(a.b).read_text(encoding="utf-8"))
    if A["probe_id"] != B["probe_id"]:
        sys.exit("Snapshots are of different probes")
    am = {r["relpath"]: r for r in A["files"]}
    bm = {r["relpath"]: r for r in B["files"]}
    common = sorted(set(am) & set(bm) - {"probe.json"})
    buckets = {"identical_ns": 0, "lt_1us": 0, "lt_1ms": 0, "lt_1s": 0, "lt_2s": 0, "ge_2s": 0}
    size_diff, worst = 0, 0
    for k in common:
        d = abs(am[k]["mtime_ns"] - bm[k]["mtime_ns"])
        worst = max(worst, d)
        if d == 0:
            buckets["identical_ns"] += 1
        elif d < 1_000:
            buckets["lt_1us"] += 1
        elif d < 1_000_000:
            buckets["lt_1ms"] += 1
        elif d < 1_000_000_000:
            buckets["lt_1s"] += 1
        elif d < 2_000_000_000:
            buckets["lt_2s"] += 1
        else:
            buckets["ge_2s"] += 1
        size_diff += am[k]["size"] != bm[k]["size"]
    hashed = [k for k in common if "sha256" in am[k] and "sha256" in bm[k]]
    hash_diff = sum(am[k]["sha256"] != bm[k]["sha256"] for k in hashed)
    dot_b = bm.get(DOTFILE)
    report = {
        "a": A["label"], "b": B["label"], "common_files": len(common),
        "only_in_a": sorted(set(am) - set(bm)), "only_in_b": sorted(set(bm) - set(am)),
        "mtime_delta_buckets": buckets, "worst_delta_ns": worst,
        "size_differences": size_diff, "hash_compared": len(hashed), "hash_differences": hash_diff,
        "dotfile_in_b": dot_b is not None, "dotfile_hidden_in_b": bool(dot_b and "hidden" in dot_b["attr_names"]),
        "placeholders_in_b": sum(r["placeholder"] for r in B["files"]),
    }
    out = pathlib.Path(a.b).with_name(f"compare-{A['label']}-vs-{B['label']}.json")
    ph.write_json(out, report)
    print(f"{A['label']} vs {B['label']}: {len(common)} common files")
    print(f"  mtime deltas: {buckets}  (worst {worst / 1e9:.6f}s)")
    print(f"  size differences: {size_diff}; hash differences: {hash_diff} of {len(hashed)} compared")
    print(f"  only in {A['label']}: {len(report['only_in_a'])}; only in {B['label']}: {len(report['only_in_b'])}")
    print(f"  hidden dotfile synced to {B['label']}: {report['dotfile_in_b']}, still hidden: {report['dotfile_hidden_in_b']}")
    print(f"  placeholders on {B['label']}: {report['placeholders_in_b']}")
    verdict = ("mtime_tolerance_us = 0 is safe" if buckets["identical_ns"] == len(common) else
               "sub-microsecond loss only: microsecond storage is enough" if worst < 1_000 else
               "keep the 1 s tolerance" if worst < 1_000_000_000 else
               "mtimes differ by >= 1 s: the cache-reuse rule needs rethinking")
    print(f"  => {verdict}\nResults: {out}")
    return 0


def do_moves(pairs: list[tuple[pathlib.Path, pathlib.Path]]) -> float:
    t0 = time.perf_counter()
    for src, dst in pairs:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            sys.exit(f"Refusing to overwrite {dst}")
        if same_volume(src.parent, dst.parent):
            os.replace(src, dst)
        else:
            shutil.copy2(src, dst)
            if ph.sha256_file(dst) != ph.sha256_file(src):
                sys.exit(f"Copy verification failed for {src}; source left in place")
            os.remove(src)
    return time.perf_counter() - t0


def cmd_move(a) -> int:
    probe = pathlib.Path(a.probe).resolve()
    data = load_probe(probe)
    if data["moves"] and data["moves"][-1].get("state") == "moved":
        sys.exit("Files are already moved; run `move-back` first")
    dest = pathlib.Path(a.to).resolve()
    if ph.is_inside(dest, probe):
        sys.exit("--to must be outside the probe folder")
    if dest.exists() and any(dest.iterdir()):
        sys.exit(f"--to {dest} must be new or empty")
    srcs = list_files(probe, data)
    missing = [s for s in srcs if not s.exists()]
    if missing:
        sys.exit(f"{len(missing)} probe files are missing; is the probe fully synced here?")
    pairs = [(s, dest / s.name) for s in srcs]
    total = sum(f["size"] for f in data["files"])
    vol = "same volume (rename)" if same_volume(probe, dest.parent if not dest.exists() else dest) else "different volume (copy+delete)"
    print(f"Moving {len(pairs)} files ({ph.human_bytes(total)}) to {dest}  [{vol}]")

    base = sample_upload(a.baseline, label="baseline ") if a.baseline else {"supported": False}
    secs = do_moves(pairs)
    print(f"  moved in {secs:.2f}s; now watching upload for {a.watch}s (leave OneDrive running)")
    watch = sample_upload(a.watch, label="after move ") if a.watch else {"supported": False}

    rec = {"at": ph.utc_now(), "machine": ph.env_info()["machine"], "to": str(dest), "volume": vol,
           "files": len(pairs), "bytes": total, "seconds": secs, "baseline": base, "watch": watch,
           "state": "moved"}
    data["moves"].append(rec)
    save_probe(probe, data)
    res = ph.results_dir(a.results, "onedrive")
    ph.write_json(res / f"move-{data['probe_id']}-{len(data['moves'])}.json", rec)

    if watch.get("supported"):
        noise = base.get("bytes_per_s", 0) * watch["seconds"] if base.get("supported") else 0
        excess = watch["bytes"] - noise
        ratio = excess / total if total else 0
        print(f"  upload during watch: {ph.human_bytes(watch['bytes'])}, background estimate {ph.human_bytes(noise)}")
        print(f"  excess upload / moved bytes = {ratio:.2f}")
        print("  => " + ("looks like delete + RE-UPLOAD" if ratio > 0.5 else
                         "looks like a server-side move (no re-upload)" if ratio < 0.1 else
                         "unclear; rerun with more/larger files or a longer --watch"))
    print("Also check in OneDrive on the web: does the Recycle bin contain the probe files? "
          "(A true move leaves nothing there; delete + add does.)")
    return 0


def cmd_move_back(a) -> int:
    probe = pathlib.Path(a.probe).resolve()
    data = load_probe(probe)
    if not data["moves"] or data["moves"][-1].get("state") != "moved":
        sys.exit("Nothing to move back")
    last = data["moves"][-1]
    dest = pathlib.Path(last["to"])
    pairs = [(dest / s.name, s) for s in list_files(probe, data)]
    missing = [p for p, _ in pairs if not p.exists()]
    if missing:
        sys.exit(f"{len(missing)} files are not at {dest}; not moving anything")
    print(f"Moving {len(pairs)} files back into {probe / 'files'}")
    base = sample_upload(a.baseline, label="baseline ") if a.baseline else {"supported": False}
    secs = do_moves(pairs)
    watch = sample_upload(a.watch, label="after move-back ") if a.watch else {"supported": False}
    last["state"] = "moved_back"
    last["move_back"] = {"at": ph.utc_now(), "seconds": secs, "baseline": base, "watch": watch}
    save_probe(probe, data)
    try:
        dest.rmdir()
    except OSError:
        pass
    if watch.get("supported"):
        print(f"  upload during watch: {ph.human_bytes(watch['bytes'])} (moved {ph.human_bytes(sum(f['size'] for f in data['files']))})")
    return 0


def cmd_watch(a) -> int:
    r = sample_upload(a.seconds, label="")
    if r.get("supported"):
        print(f"{ph.human_bytes(r['bytes'])} in {r['seconds']:.0f}s ({ph.human_bytes(r['bytes_per_s'])}/s)")
    return 0


def cmd_cleanup(a) -> int:
    probe = pathlib.Path(a.probe).resolve()
    data = load_probe(probe)
    if data["moves"] and data["moves"][-1].get("state") == "moved":
        sys.exit(f"Files are still moved to {data['moves'][-1]['to']}; run `move-back` first")
    targets = list_files(probe, data) + [probe / DOTFILE, probe / "probe.json"]
    print(f"Will delete {len(targets)} probe files and the folder {probe}")
    if not a.yes and input("Type 'delete' to confirm: ").strip() != "delete":
        print("Cancelled")
        return 1
    for t in targets:
        if t.exists():
            t.unlink()
    for d in sorted({t.parent for t in targets}, key=lambda p: -len(p.parts)):
        try:
            d.rmdir()
        except OSError:
            pass
    print("Done (OneDrive will move them to its recycle bin)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare"); p.add_argument("parent")
    p.add_argument("--count", type=int, default=200)
    p.add_argument("--size-kb", type=int, default=1024)
    p.add_argument("--from-corpus", help="copy media from this corpus instead of random bytes")
    p.set_defaults(fn=cmd_prepare)

    p = sub.add_parser("snapshot"); p.add_argument("probe")
    p.add_argument("--label", help="machine label (default: hostname)")
    p.add_argument("--hash", action="store_true", help="also hash files that are local (not placeholders)")
    p.add_argument("--results"); p.set_defaults(fn=cmd_snapshot)

    p = sub.add_parser("compare"); p.add_argument("a"); p.add_argument("b"); p.set_defaults(fn=cmd_compare)

    p = sub.add_parser("move"); p.add_argument("probe"); p.add_argument("--to", required=True)
    p.add_argument("--baseline", type=int, default=30, help="seconds of upload sampling before the move")
    p.add_argument("--watch", type=int, default=300, help="seconds of upload sampling after the move")
    p.add_argument("--results"); p.set_defaults(fn=cmd_move)

    p = sub.add_parser("move-back"); p.add_argument("probe")
    p.add_argument("--baseline", type=int, default=0); p.add_argument("--watch", type=int, default=120)
    p.set_defaults(fn=cmd_move_back)

    p = sub.add_parser("watch-upload"); p.add_argument("--seconds", type=int, default=60); p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("cleanup"); p.add_argument("probe"); p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_cleanup)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
