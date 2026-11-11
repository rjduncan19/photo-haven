"""ExifTool bridge: one long-lived `-stay_open` process (docs/04 stage 4).
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import threading

from ..util import IS_WINDOWS, plain_path

MIN_VERSION = (12, 62)   # HEIC ImageDataHash

# Per-request arguments (docs/04 stage 4). -G0:1:4 gives keys of the form
# family0:family1[:CopyN]:Tag, so every warning comes back (-a + family 4)
# and the family 0 group the spec names (03 §3.3) is the first part.
BASE_ARGS = ["-json", "-n", "-a", "-G0:1:4", "-struct", "-api", "ImageHashType=SHA256"]
TAGS = ["-SubSecDateTimeOriginal", "-DateTimeOriginal", "-OffsetTimeOriginal",
        "-SubSecCreateDate", "-CreateDate", "-OffsetTimeDigitized",
        "-CreationDate", "-DateCreated",
        "-Make", "-Model", "-LensModel", "-LensID", "-ImageSize", "-Orientation",
        "-GPSLatitude", "-GPSLongitude", "-GPSAltitude", "-Duration", "-MIMEType",
        "-ContentIdentifier", "-BurstUUID", "-OriginalImageHash", "-OriginalImageHashType",
        "-Warning", "-Error", "-ImageDataHash"]


class ExifToolError(Exception):
    pass


def safe_arg_path(p: str) -> bool:
    """Paths go to ExifTool one per line through its -@ argument file, which
    trims surrounding whitespace. A path with a line break or surrounding
    whitespace could be read as extra arguments (or a different file), so it
    is never sent. (NTFS forbids CR/LF in names; other filesystems don't.)"""
    return "\n" not in p and "\r" not in p and p == p.strip() and not p.startswith("-")


def find_exiftool(configured: str | None) -> str | None:
    # search PATH only: on Windows a bare which() also looks in the current
    # folder first, and that could be a photo folder
    search = os.environ.get("PATH", "")
    if configured:
        return configured if os.path.isabs(configured) and os.path.exists(configured) else \
            shutil.which(configured, path=search)
    found = shutil.which("exiftool", path=search)
    if found:
        return found
    if IS_WINDOWS:
        cand = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "ExifTool", "exiftool.exe")
        if os.path.exists(cand):
            return cand
    return None


def parse_version(s: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in s.strip().split("."))
    except ValueError:
        return (0,)


class ExifTool:
    """A persistent ExifTool process. Thread-safe (requests are serialized)."""

    def __init__(self, exe: str):
        self.exe = exe
        self._stderr = tempfile.TemporaryFile()   # never a pipe: a flood of warnings can't deadlock us
        flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
        self.proc = subprocess.Popen([exe, "-stay_open", "True", "-@", "-"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=self._stderr, bufsize=0,
                                     creationflags=flags)
        self._n = 0
        self._lock = threading.Lock()
        self.version = self._run(["-ver"]).strip()
        if parse_version(self.version) < MIN_VERSION:
            self.close()
            raise ExifToolError(f"ExifTool {self.version} is too old; need {'.'.join(map(str, MIN_VERSION))}+")

    def _run(self, args: list[str]) -> str:
        return self._run_bytes(args).decode("utf-8", errors="replace")

    @staticmethod
    def _payload(args: list[str], n: int) -> str:
        """Exactly what is written to ExifTool's -@ argument stream, one
        argument per line (tests inspect this)."""
        extra = ["-api", "WindowsLongPath=1"] if IS_WINDOWS else []
        return "\n".join(["-charset", "filename=utf8", *extra, *args, f"-execute{n}", ""])

    def _run_bytes(self, args: list[str]) -> bytes:
        with self._lock:
            self._n += 1
            n = self._n
            payload = self._payload(args, n)
            if self.proc.poll() is not None:
                raise ExifToolError("ExifTool is not running")
            assert self.proc.stdin and self.proc.stdout
            self.proc.stdin.write(payload.encode("utf-8"))
            self.proc.stdin.flush()
            sentinel = f"{{ready{n}}}".encode()
            buf = bytearray()
            fd = self.proc.stdout.fileno()
            while True:
                chunk = os.read(fd, 1 << 16)
                if not chunk:
                    raise ExifToolError("ExifTool exited unexpectedly")
                buf += chunk
                i = buf.find(sentinel, max(0, len(buf) - len(chunk) - len(sentinel)))
                if i != -1:
                    return bytes(buf[:i])

    def describe(self, paths: list[str]) -> list[dict | None]:
        """Raw ExifTool JSON for each path (in order); None where ExifTool
        returned nothing for a file."""
        if not paths:
            return []
        plain = [os.path.abspath(plain_path(p)) for p in paths]
        if not all(safe_arg_path(p) for p in plain):
            if len(plain) == 1:
                return [None]           # reported as exiftool_error; the file is still hashed
            return [self.describe([p])[0] for p in paths]
        out = self._run([*BASE_ARGS, *TAGS, *plain])
        try:
            arr = json.loads(out) if out.strip() else []
        except json.JSONDecodeError:
            arr = []
        by_path: dict[str, dict] = {}
        for obj in arr:
            sf = obj.get("SourceFile")
            if isinstance(sf, str):
                by_path[_norm(sf)] = obj
        results = [by_path.get(_norm(p)) for p in plain]
        if len(paths) > 1 and any(r is None for r in results):
            # Couldn't match some outputs to inputs: ask again one at a time.
            results = [r if r is not None else self.describe([p])[0] for p, r in zip(paths, results)]
        elif len(paths) == 1 and results[0] is None and len(arr) == 1:
            results = [arr[0]]
        return results

    def embedded_preview(self, path: str) -> bytes | None:
        """The embedded JPEG thumbnail (or raw preview) for report thumbnails
        (07 §1). No decoding: bytes as stored in the file."""
        p = os.path.abspath(plain_path(path))
        if not safe_arg_path(p):
            return None
        for tag in ("-ThumbnailImage", "-PreviewImage"):
            data = self._run_bytes(["-b", tag, p])
            if data[:2] == bytes((0xFF, 0xD8)):
                return data
        return None

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                assert self.proc.stdin
                self.proc.stdin.write(b"-stay_open\nFalse\n")
                self.proc.stdin.flush()
                self.proc.wait(timeout=10)
            except Exception:
                self.proc.kill()
        self._stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p.replace("/", os.sep)))
