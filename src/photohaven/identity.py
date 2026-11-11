"""Machine identity and the per-machine process lock (docs/06 §7, docs/02).
"""
from __future__ import annotations

import dataclasses
import os
import pathlib
import platform

from . import SPEC_VERSION, TOOL
from .formats import FormatError, json_doc_bytes, read_json_doc
from .util import atomic_write_bytes, long_path, machine_short, utc_now, uuid4

MACHINE_JSON = "machine.json"
LOCK_FILE = "lock"


@dataclasses.dataclass
class Machine:
    machine_id: str
    name: str
    created_at: str
    hostname: str | None = None   # from the registry, not machine.json

    @property
    def short(self) -> str:
        return machine_short(self.machine_id)


def load_machine(state_dir: pathlib.Path) -> Machine | None:
    p = state_dir / MACHINE_JSON
    if not os.path.exists(long_path(p)):
        return None
    d = read_json_doc(p, "machine.json")
    try:
        return Machine(d["machine_id"], d.get("name", ""), d["created_at"])
    except KeyError as e:
        raise FormatError(f"{p}: missing {e}") from None


def create_machine(state_dir: pathlib.Path, name: str, taken_shorts: set[str] = frozenset()) -> Machine:
    """New identity; regenerate if machine_short clashes with the registry (06 §7)."""
    while True:
        mid = uuid4()
        if machine_short(mid) not in taken_shorts:
            break
    m = Machine(mid, name, utc_now())
    atomic_write_bytes(state_dir / MACHINE_JSON, json_doc_bytes(
        {"schema": SPEC_VERSION, "machine_id": m.machine_id, "name": m.name, "created_at": m.created_at}))
    return m


def registry_dir(project_home: pathlib.Path) -> pathlib.Path:
    return project_home / "machines"


def read_registry(project_home: pathlib.Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    d = registry_dir(project_home)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            rec = read_json_doc(p, "machine registry")
        except FormatError:
            continue
        if isinstance(rec.get("machine_id"), str):
            out[rec["machine_id"]] = rec
    return out


def write_registry(project_home: pathlib.Path, m: Machine, *, force: bool = False) -> bool:
    """Write this machine's registry file. `last_seen` is updated at most once
    a day unless forced, to avoid needless sync traffic (06 §7)."""
    p = registry_dir(project_home) / f"{m.machine_id}.json"
    now = utc_now()
    old: dict = {}
    if os.path.exists(long_path(p)):
        try:
            old = read_json_doc(p, "machine registry")
        except FormatError:
            old = {}
    rec = {"schema": SPEC_VERSION, "machine_id": m.machine_id, "machine_short": m.short, "name": m.name,
           "hostname": platform.node(), "last_seen": now, "tool": TOOL}
    if "retired_at" in old:
        rec["retired_at"] = old["retired_at"]
    if not force and old and old.get("last_seen", "")[:10] == now[:10] and all(
            old.get(k) == rec[k] for k in ("name", "hostname", "tool", "machine_short")):
        return False
    atomic_write_bytes(p, json_doc_bytes(rec))
    return True


class LockError(Exception):
    pass


class ProcessLock:
    """One Photo Haven process per machine at a time (docs/02 "Concurrency").

    An OS-level lock on a file in local state, released when the process exits
    even if it crashes."""

    def __init__(self, state_dir: pathlib.Path):
        self.path = state_dir / LOCK_FILE
        self._f = None

    def acquire(self) -> "ProcessLock":
        os.makedirs(long_path(self.path.parent), exist_ok=True)
        f = open(long_path(self.path), "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.close()
            raise LockError(f"another Photo Haven process is running on this machine (lock: {self.path})") from None
        self._f = f
        return self

    def release(self) -> None:
        if self._f is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._f.seek(0)
                try:
                    msvcrt.locking(self._f.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            self._f.close()
            self._f = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
