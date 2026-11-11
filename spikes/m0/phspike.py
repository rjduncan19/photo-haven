"""Shared helpers for the M0 spike scripts. Stdlib only.

These scripts are throwaway experiments, not the product. They exist to settle
assumptions in docs/03-format-spec.md before any product code depends on them.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import time

IS_WINDOWS = os.name == "nt"

# Windows file attribute bits (see docs/04-scanner.md, "OneDrive placeholders")
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_PINNED = 0x80000
FILE_ATTRIBUTE_UNPINNED = 0x100000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
PLACEHOLDER_MASK = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

MEDIA = {
    "heic": {"heic", "heif"},
    "jpeg": {"jpg", "jpeg"},
    "raw": {"cr2", "cr3", "crw", "nef", "nrw", "arw", "srf", "sr2", "dng", "orf", "rw2", "raf", "pef", "srw", "x3f"},
    "video": {"mov", "mp4", "m4v"},
    "png": {"png"},
    "sidecar": {"aae", "xmp"},
}

CHUNK = 1024 * 1024

# ExifTool warnings that mean the file's bytes are damaged (seen in spike A on
# truncated JPEG, HEIC, PNG, MOV/MP4 copies), not merely odd metadata.
DAMAGE_PATTERNS = re.compile(
    r"format error|error reading|truncated|corrupt|unexpected end|bad .*(header|data)|missing .*data",
    re.IGNORECASE,
)


def utc_now() -> str:
    """Event timestamp in the spec's fixed format (docs/03 §1.2)."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


def category(path: pathlib.Path) -> str | None:
    ext = path.suffix.lower().lstrip(".")
    for cat, exts in MEDIA.items():
        if ext in exts:
            return cat
    return None


def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def file_attrs(st: os.stat_result) -> int:
    return getattr(st, "st_file_attributes", 0)


def is_placeholder(st: os.stat_result) -> bool:
    return bool(file_attrs(st) & PLACEHOLDER_MASK)


def attr_names(attrs: int) -> list[str]:
    names = {
        FILE_ATTRIBUTE_HIDDEN: "hidden",
        FILE_ATTRIBUTE_OFFLINE: "offline",
        FILE_ATTRIBUTE_RECALL_ON_OPEN: "recall_on_open",
        FILE_ATTRIBUTE_PINNED: "pinned",
        FILE_ATTRIBUTE_UNPINNED: "unpinned",
        FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS: "recall_on_data_access",
    }
    return [n for bit, n in names.items() if attrs & bit]


def set_hidden(path: pathlib.Path) -> bool:
    """Set the Windows hidden attribute. Returns False on non-Windows."""
    if not IS_WINDOWS:
        return False
    import ctypes
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    cur = k32.GetFileAttributesW(str(path))
    if cur == 0xFFFFFFFF:
        return False
    return bool(k32.SetFileAttributesW(str(path), cur | FILE_ATTRIBUTE_HIDDEN))


def is_inside(child: pathlib.Path, parent: pathlib.Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def write_json(path: pathlib.Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def results_dir(arg: str | None, name: str) -> pathlib.Path:
    d = pathlib.Path(arg) if arg else pathlib.Path("spike-results") / f"{stamp()}-{name}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def env_info() -> dict:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.node(),
        "at": utc_now(),
    }


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} TB"


# --------------------------------------------------------------------------
# ExifTool bridge (stay_open mode, as the product will use; docs/04 stage 4)
# --------------------------------------------------------------------------

class ExifTool:
    """Minimal persistent ExifTool process.

    Each request is a list of arguments; the response is ExifTool's stdout
    text for that request. -config must be given at launch, not per request.
    """

    def __init__(self, exe: str = "exiftool", config: str | None = None):
        found = shutil.which(exe) or (exe if pathlib.Path(exe).exists() else None)
        if not found:
            raise SystemExit(
                f"ExifTool not found ({exe!r}). Install it and pass --exiftool PATH. "
                "On Windows, rename 'exiftool(-k).exe' to 'exiftool.exe'."
            )
        cmd = [found]
        if config:
            cmd += ["-config", config]
        cmd += ["-stay_open", "True", "-@", "-"]
        # stderr goes to a temp file so a flood of warnings can never fill a pipe and deadlock us.
        import tempfile
        self._stderr = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._stderr,
            bufsize=0,
        )
        self.n = 0
        self.exe = found
        self.version = self.run(["-ver"]).strip()

    def run(self, args: list[str]) -> str:
        self.n += 1
        # WindowsLongPath lets ExifTool open paths over 260 characters (pass the plain path, no \\?\ prefix).
        long_path = ["-api", "WindowsLongPath=1"] if IS_WINDOWS else []
        payload = "\n".join(["-charset", "filename=utf8", *long_path, *args, f"-execute{self.n}", ""])
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(payload.encode("utf-8"))
        self.proc.stdin.flush()
        sentinel = f"{{ready{self.n}}}".encode()
        buf = bytearray()
        fd = self.proc.stdout.fileno()
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                raise RuntimeError("ExifTool exited unexpectedly")
            buf += chunk
            idx = buf.find(sentinel)
            if idx != -1:
                out = bytes(buf[:idx])
                return out.decode("utf-8", errors="replace")

    def json(self, args: list[str]) -> list[dict]:
        out = self.run(["-json", *args])
        out = out.strip()
        if not out:
            return []
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            # ExifTool prints non-JSON text for some errors; surface it.
            return [{"_raw": out}]

    def image_data_hash(self, path: pathlib.Path) -> tuple[str | None, float, str | None]:
        """Return (sha256 of image data, seconds, all warnings/errors joined by ' | ')."""
        h = self.hash_check(path)
        return h["hash"], h["seconds"], (" | ".join(h["messages"]) or None)

    def hash_check(self, path: pathlib.Path) -> dict:
        """ImageDataHash plus EVERY ExifTool warning/error, and whether the file looks damaged.

        -a with -G4 is required: JSON output otherwise keeps only the first
        Warning, and a harmless "[minor]" warning can hide "JPEG format error"
        (see docs/spikes/m0-results.md, spike A). Warnings are sorted three ways:
          damaged: any Error, or a Warning matching DAMAGE_PATTERNS (truncation,
                   format errors, unreadable image data). The hash must not be trusted.
          suspect: any other Warning not tagged "[minor]" (e.g. "IPTCDigest is not
                   current", seen on an intact iPhone JPEG). Recorded; hash still used.
          minor:   "[minor]" warnings. Ignored.
        A damaged file may still return a hash (spike A: 93 of 93 truncated stills did).
        """
        t0 = time.perf_counter()
        res = self.json(["-a", "-G4", "-api", "ImageHashType=SHA256",
                         "-ImageDataHash", "-Warning", "-Error", str(path)])
        dt = time.perf_counter() - t0
        rec = res[0] if res else {}
        tag = lambda k: k.rsplit(":", 1)[-1]
        warnings = [v for k, v in rec.items() if tag(k) == "Warning"]
        errors = [v for k, v in rec.items() if tag(k) == "Error"]
        if "_raw" in rec:
            errors.append(rec["_raw"])
        idh = next((v for k, v in rec.items() if tag(k) == "ImageDataHash"), None)
        major = [str(w) for w in warnings if not str(w).startswith("[minor]")]
        serious = [str(e) for e in errors] + [w for w in major if DAMAGE_PATTERNS.search(w)]
        suspect = [w for w in major if not DAMAGE_PATTERNS.search(w)]
        return {"hash": idh, "seconds": dt, "messages": [str(m) for m in errors + warnings],
                "damaged": bool(serious), "serious": serious, "suspect": suspect}

    def close(self) -> None:
        try:
            assert self.proc.stdin
            self.proc.stdin.write(b"-stay_open\nFalse\n")
            self.proc.stdin.flush()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def version_tuple(v: str) -> tuple[int, ...]:
    out = []
    for part in v.split("."):
        digits = "".join(ch for ch in part if ch.isdigit())
        out.append(int(digits or 0))
    return tuple(out)
