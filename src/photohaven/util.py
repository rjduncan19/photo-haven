"""Small pure helpers shared by every module (docs/03 §1).

Path normalization, timestamps, identifiers, atomic writes, Windows file
attributes and long paths. Nothing here knows any persisted format.
"""
from __future__ import annotations

import datetime as _dt
import os
import pathlib
import secrets
import threading
import time
import unicodedata
import uuid

IS_WINDOWS = os.name == "nt"

# Windows file attribute bits (docs/02 "Windows specifics", docs/04 "OneDrive placeholders")
FILE_ATTRIBUTE_READONLY = 0x1
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
PLACEHOLDER_MASK = FILE_ATTRIBUTE_OFFLINE | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS


# ---------------------------------------------------------------- timestamps

def format_ts(dt: _dt.datetime) -> str:
    """Event timestamp, §1.2: UTC, six fractional digits, 'Z'."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def utc_now() -> str:
    return format_ts(_dt.datetime.now(_dt.timezone.utc))


def parse_ts(s: str) -> _dt.datetime:
    if len(s) != 27 or not s.endswith("Z"):
        raise ValueError(f"not a §1.2 timestamp: {s!r}")
    return _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=_dt.timezone.utc)


def us_to_ts(us: int) -> str:
    return format_ts(_dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc) + _dt.timedelta(microseconds=us))


def stat_mtime_us(st: os.stat_result) -> int:
    """§1.3: integer microseconds since the epoch, truncated toward zero."""
    return st.st_mtime_ns // 1000


def stat_ctime_us(st: os.stat_result) -> int | None:
    """Creation time where the OS has one (Windows, macOS); None elsewhere."""
    ns = getattr(st, "st_birthtime_ns", None)
    return ns // 1000 if ns is not None else None


# --------------------------------------------------------------- identifiers

def uuid4() -> str:
    return str(uuid.uuid4())


_u7_lock = threading.Lock()
_u7_last = (0, 0)


def uuid7() -> str:
    """UUIDv7 (RFC 9562): 48-bit ms timestamp, then a 12-bit counter seeded
    randomly each millisecond, so ids made in one process sort in creation order."""
    global _u7_last
    with _u7_lock:
        ms = time.time_ns() // 1_000_000
        last_ms, last_ctr = _u7_last
        if ms <= last_ms:
            ms, ctr = last_ms, last_ctr + 1
            if ctr > 0xFFF:          # counter exhausted: borrow the next millisecond
                ms, ctr = ms + 1, secrets.randbits(11)
        else:
            ctr = secrets.randbits(11)   # top bit clear leaves room to count
        _u7_last = (ms, ctr)
    rand_b = secrets.randbits(62)
    n = (ms << 80) | (0x7 << 76) | (ctr << 64) | (0b10 << 62) | rand_b
    return str(uuid.UUID(int=n))


def machine_short(machine_id: str) -> str:
    """§1.7: first 12 hex digits of the machine id, hyphens removed."""
    return machine_id.replace("-", "")[:12]


def new_batch_id() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)


# --------------------------------------------------------------------- paths

def name_key(s: str) -> str:
    """casefold(NFC(s)): the comparison key for names and paths (§1.6)."""
    return unicodedata.normalize("NFC", s).casefold()


path_key = name_key


def sort_key(name: str) -> tuple[str, str]:
    """Sort order for names in cache files (§3.2): name key, ties by raw name."""
    return (name_key(name), name)


def validate_relpath(relpath: str) -> str:
    """Raise ValueError unless relpath follows §1.6. Returns it unchanged."""
    if relpath == "":
        return relpath
    if "\\" in relpath or relpath.startswith("/") or relpath.endswith("/"):
        raise ValueError(f"bad relpath {relpath!r}")
    for part in relpath.split("/"):
        if part in ("", ".", ".."):
            raise ValueError(f"bad relpath {relpath!r}")
    return relpath


def join_rel(parent: str, name: str) -> str:
    return f"{parent}/{name}" if parent else name


def split_rel(relpath: str) -> tuple[str, str]:
    """('a/b', 'c.jpg') for 'a/b/c.jpg'; ('', 'c.jpg') at the root."""
    head, _, tail = relpath.rpartition("/")
    return head, tail


def rel_to_path(root: pathlib.Path | str, relpath: str) -> pathlib.Path:
    p = pathlib.Path(root)
    return p.joinpath(*relpath.split("/")) if relpath else p


def ext_of(name: str) -> str:
    """Lowercase extension without the dot; '' if none (a leading dot is not an extension)."""
    stem, dot, ext = name.rpartition(".")
    return ext.lower() if dot and stem else ""


def stem_of(name: str) -> str:
    stem, dot, ext = name.rpartition(".")
    return stem if dot and stem else name


def long_path(p: pathlib.Path | str) -> str:
    """Path string for Python's own file I/O. On Windows, an absolute path in
    the \\\\?\\ form, which lifts the 260-character limit whatever the
    LongPathsEnabled setting. Elsewhere, the plain path."""
    s = os.fspath(p)
    if IS_WINDOWS and s.startswith("\\\\?\\"):
        return s
    s = os.path.abspath(s)
    if not IS_WINDOWS:
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def plain_path(s: str) -> str:
    """Undo long_path() for display and for ExifTool (which wants plain paths)."""
    if s.startswith("\\\\?\\UNC\\"):
        return "\\\\" + s[8:]
    if s.startswith("\\\\?\\"):
        return s[4:]
    return s


def is_inside(child: pathlib.Path | str, parent: pathlib.Path | str) -> bool:
    c = os.path.normcase(os.path.abspath(os.fspath(child)))
    p = os.path.normcase(os.path.abspath(os.fspath(parent)))
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


# --------------------------------------------------------- file attributes

def file_attributes(st: os.stat_result) -> int:
    return getattr(st, "st_file_attributes", 0)


def is_placeholder(st: os.stat_result) -> bool:
    """True if reading the file may make OneDrive download it (docs/04)."""
    return bool(file_attributes(st) & PLACEHOLDER_MASK)


def set_hidden(path: pathlib.Path | str) -> bool:
    """Set FILE_ATTRIBUTE_HIDDEN (Explorer ignores the dot convention).
    No-op returning False on other systems."""
    if not IS_WINDOWS:
        return False
    import ctypes
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    s = long_path(path)
    cur = k32.GetFileAttributesW(s)
    if cur == 0xFFFFFFFF:
        return False
    if cur & FILE_ATTRIBUTE_HIDDEN:
        return True
    return bool(k32.SetFileAttributesW(s, cur | FILE_ATTRIBUTE_HIDDEN))


# ------------------------------------------------------------- atomic write

def atomic_write_bytes(path: pathlib.Path | str, data: bytes, *, hidden: bool = False) -> None:
    """Write `path` atomically: <path>.tmp, flush, fsync, os.replace.

    The replacement file does not inherit the old one's hidden attribute, so
    it is set again afterwards when asked."""
    target = long_path(path)
    tmp = target + ".tmp"
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, target)
    if hidden:
        set_hidden(target)


def atomic_write_text(path: pathlib.Path | str, text: str, *, hidden: bool = False) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), hidden=hidden)
