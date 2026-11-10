"""M0 step 0: check this machine is ready for the spikes.

Usage:
    python check_env.py [--exiftool PATH] [--path DIR ...]

Checks Python version, ExifTool presence and version (>= 12.62 for HEIC
ImageDataHash), Windows long-path support, raw SHA-256 speed, and, for each
--path, which volume it is on and whether it looks like it's inside OneDrive.
Read-only. Writes nothing except its results file.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import pathlib
import sys
import time

import phspike as ph

MIN_EXIFTOOL = (12, 62)


def check_long_paths() -> str:
    if not ph.IS_WINDOWS:
        return "n/a (not Windows)"
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            val, _ = winreg.QueryValueEx(k, "LongPathsEnabled")
            return "enabled" if val == 1 else "DISABLED (set LongPathsEnabled=1, or the product must use \\\\?\\ paths)"
    except OSError:
        return "unknown (registry value missing: treat as disabled)"


def sha_speed() -> float:
    buf = os.urandom(64 * 1024 * 1024)
    t0 = time.perf_counter()
    h = hashlib.sha256()
    for _ in range(4):
        h.update(buf)
    dt = time.perf_counter() - t0
    return (4 * len(buf)) / dt


def onedrive_roots() -> list[pathlib.Path]:
    roots = []
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        v = os.environ.get(var)
        if v and pathlib.Path(v).exists():
            roots.append(pathlib.Path(v))
    return list(dict.fromkeys(roots))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exiftool", default="exiftool")
    ap.add_argument("--path", action="append", default=[], help="a folder you plan to use (test corpus, OneDrive probe, quarantine)")
    ap.add_argument("--results")
    a = ap.parse_args()

    out = {"env": ph.env_info(), "checks": {}}
    ok = True

    py = sys.version_info
    out["checks"]["python"] = f"{py.major}.{py.minor}.{py.micro}"
    if py < (3, 12):
        ok = False
        print(f"[FAIL] Python {py.major}.{py.minor}: need 3.12+")
    else:
        print(f"[ok]   Python {py.major}.{py.minor}.{py.micro}")

    try:
        with ph.ExifTool(a.exiftool) as et:
            v = et.version
            out["checks"]["exiftool"] = {"path": et.exe, "version": v}
            if ph.version_tuple(v) < MIN_EXIFTOOL:
                ok = False
                print(f"[FAIL] ExifTool {v} at {et.exe}: need >= 12.62 for HEIC ImageDataHash")
            else:
                print(f"[ok]   ExifTool {v} at {et.exe}")
    except SystemExit as e:
        ok = False
        out["checks"]["exiftool"] = str(e)
        print(f"[FAIL] {e}")

    lp = check_long_paths()
    out["checks"]["long_paths"] = lp
    print(f"[{'ok' if lp in ('enabled', 'n/a (not Windows)') else 'warn'}]   Long paths: {lp}")

    speed = sha_speed()
    out["checks"]["sha256_bytes_per_s"] = speed
    print(f"[ok]   SHA-256 in memory: {ph.human_bytes(speed)}/s per core (disk will be the real limit)")

    ods = onedrive_roots()
    out["checks"]["onedrive_roots"] = [str(p) for p in ods]
    print(f"[info] OneDrive folders from environment: {', '.join(map(str, ods)) or 'none found'}")

    for p in a.path:
        pp = pathlib.Path(p)
        info = {"exists": pp.exists()}
        if pp.exists():
            info["volume"] = os.path.splitdrive(str(pp.resolve()))[0] or "/"
            info["inside_onedrive"] = any(ph.is_inside(pp, r) for r in ods)
            try:
                import shutil
                info["free_bytes"] = shutil.disk_usage(pp).free
            except OSError:
                pass
        out["checks"].setdefault("paths", {})[p] = info
        print(f"[info] {p}: {info}")

    res = ph.results_dir(a.results, "env")
    ph.write_json(res / "env.json", out)
    print(f"\nResults: {res / 'env.json'}")
    print("READY" if ok else "NOT READY: fix the FAIL items above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
