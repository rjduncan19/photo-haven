"""Configuration: library.json, library.toml, machine.toml (docs/03 §2).

Loads and validates them, resolves root paths, and writes them with a tiny
TOML emitter (tomllib only reads).
"""
from __future__ import annotations

import copy
import dataclasses
import fnmatch
import os
import pathlib
import re
import tomllib
from typing import Any

from . import SPEC_VERSION
from .formats import FormatError, check_schema_version, json_doc_bytes, read_json_doc
from .util import atomic_write_bytes, ext_of, long_path, name_key, path_key

LIBRARY_JSON = "library.json"
LIBRARY_TOML = "library.toml"
MACHINE_TOML = "machine.toml"
STATE_ENV = "PHOTOHAVEN_STATE"

DEFAULT_LIBRARY_TOML: dict[str, Any] = {
    "schema": SPEC_VERSION,
    "scan": {
        "mtime_tolerance_us": 1_000_000,
        "hash_scope": "all",
        "ignore_names": ["desktop.ini", "Thumbs.db", ".DS_Store", "*.tmp", "~$*"],
    },
    "media": {
        "image": ["jpg", "jpeg", "heic", "heif", "png", "gif", "tif", "tiff", "webp", "bmp"],
        "raw": ["cr2", "cr3", "crw", "nef", "nrw", "arw", "srf", "sr2", "dng", "orf", "rw2", "raf",
                "pef", "srw", "x3f"],
        "video": ["mov", "mp4", "m4v", "avi", "3gp", "mts", "m2ts", "mpg"],
        "sidecar": ["xmp", "aae", "thm"],
    },
    "folders": {
        "generic_names": ["Camera Roll", "Backup", "Backups", "Imports", "Import", "Pictures", "Photos",
                          "DCIM", "New folder", "Untitled", "Export", "Exports", "Desktop",
                          "Saved Pictures", "Screenshots", "Copy", "Misc", "Unsorted", "OneDrive"],
        "generic_patterns": [r"^\d{3}[A-Z_]{5}$", r"^\d{4}$", r"^\d{2}$", r"^(19|20)\d{2}[-_ ]?\d{2}$"],
    },
}


class ConfigError(Exception):
    pass


def default_state_dir() -> pathlib.Path:
    """Local state (docs/02): %LOCALAPPDATA%\\PhotoHaven, or $PHOTOHAVEN_STATE."""
    env = os.environ.get(STATE_ENV)
    if env:
        return pathlib.Path(env)
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return pathlib.Path(base) / "PhotoHaven"
    return pathlib.Path.home() / ".local" / "state" / "photohaven"


# ---------------------------------------------------------------- TOML out

def _toml_str(s: str) -> str:
    # Literal strings keep Windows paths readable; fall back to basic strings.
    if "'" not in s and "\n" not in s and all(ord(c) >= 0x20 or c == "\t" for c in s):
        return f"'{s}'"
    out = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    return f'"{out}"'


def _toml_key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else '"' + k.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _toml_val(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return f"{v:_}" if abs(v) >= 100_000 else str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, str):
        return _toml_str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_val(x) for x in v) + "]"
    raise TypeError(f"can't write {type(v).__name__} to TOML")


def toml_dumps(d: dict) -> str:
    """Scalars first, then one [table] level. Enough for Photo Haven's files."""
    lines = [f"{_toml_key(k)} = {_toml_val(v)}" for k, v in d.items() if not isinstance(v, dict)]
    for k, v in d.items():
        if isinstance(v, dict):
            lines += ["", f"[{_toml_key(k)}]"]
            for k2, v2 in v.items():
                if isinstance(v2, dict):
                    raise TypeError("nested tables not supported")
                lines.append(f"{_toml_key(k2)} = {_toml_val(v2)}")
    return "\n".join(lines) + "\n"


def read_toml(path: pathlib.Path | str) -> dict:
    try:
        with open(long_path(path), "rb") as f:
            d = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: {e}") from None
    check_schema_version(d, str(path))
    return d


# ------------------------------------------------------------- library.json

@dataclasses.dataclass
class RootInfo:
    root_id: str
    name: str
    kind: str = "onedrive"
    intake_folders: list[str] = dataclasses.field(default_factory=list)
    protected_folders: list[str] = dataclasses.field(default_factory=list)

    def to_json(self) -> dict:
        return {"root_id": self.root_id, "name": self.name, "kind": self.kind,
                "intake_folders": self.intake_folders or None,
                "protected_folders": self.protected_folders or None}


@dataclasses.dataclass
class Library:
    library_id: str
    name: str
    created_at: str
    apply_machine_id: str | None
    roots: list[RootInfo]
    extra: dict = dataclasses.field(default_factory=dict)

    def root(self, key: str) -> RootInfo:
        for r in self.roots:
            if r.root_id == key or r.name == key:
                return r
        raise ConfigError(f"no root named {key!r} in this library")

    def to_bytes(self) -> bytes:
        return json_doc_bytes({"schema": SPEC_VERSION, "library_id": self.library_id, "name": self.name,
                               "created_at": self.created_at, "apply_machine_id": self.apply_machine_id,
                               "roots": [r.to_json() for r in self.roots], **self.extra})

    @classmethod
    def load(cls, project_home: pathlib.Path) -> "Library":
        p = project_home / LIBRARY_JSON
        if not os.path.exists(long_path(p)):
            raise ConfigError(f"{p} not found: is the project home right? (run 'photohaven init')")
        d = read_json_doc(p, "library.json")
        known = {"schema", "library_id", "name", "created_at", "apply_machine_id", "roots"}
        try:
            roots = [RootInfo(r["root_id"], r["name"], r.get("kind", "onedrive"),
                              list(r.get("intake_folders") or []), list(r.get("protected_folders") or []))
                     for r in d["roots"]]
            return cls(d["library_id"], d.get("name", ""), d["created_at"], d.get("apply_machine_id"),
                       roots, {k: v for k, v in d.items() if k not in known})
        except (KeyError, TypeError) as e:
            raise ConfigError(f"{p}: malformed ({e})") from None

    def save(self, project_home: pathlib.Path) -> None:
        atomic_write_bytes(project_home / LIBRARY_JSON, self.to_bytes())


# ------------------------------------------------------------- library.toml

@dataclasses.dataclass
class LibrarySettings:
    raw: dict

    @classmethod
    def load(cls, project_home: pathlib.Path) -> "LibrarySettings":
        p = project_home / LIBRARY_TOML
        merged = copy.deepcopy(DEFAULT_LIBRARY_TOML)
        if os.path.exists(long_path(p)):
            for k, v in read_toml(p).items():
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k].update(v)
                else:
                    merged[k] = v
        s = cls(merged)
        s._prepare()
        return s

    def _prepare(self) -> None:
        self._ext_media: dict[str, str] = {}
        for kind in ("image", "raw", "video", "sidecar"):
            for e in self.raw["media"].get(kind, []):
                self._ext_media[e.lower()] = kind
        self._ignore = [p.casefold() for p in self.raw["scan"]["ignore_names"]]
        self._generic = {name_key(n) for n in self.raw["folders"]["generic_names"]}
        self._generic_re = [re.compile(p) for p in self.raw["folders"]["generic_patterns"]]
        if self.hash_scope not in ("candidates", "all"):
            raise ConfigError(f"library.toml: hash_scope must be 'candidates' or 'all'")

    @property
    def mtime_tolerance_us(self) -> int:
        return int(self.raw["scan"]["mtime_tolerance_us"])

    @property
    def hash_scope(self) -> str:
        return self.raw["scan"]["hash_scope"]

    def media_of(self, name: str) -> str:
        return self._ext_media.get(ext_of(name), "other")

    def ignored(self, name: str) -> bool:
        n = name.casefold()
        return any(fnmatch.fnmatchcase(n, p) for p in self._ignore)

    def is_generic(self, folder_name: str) -> bool:
        return name_key(folder_name) in self._generic or any(r.search(folder_name) for r in self._generic_re)

    @staticmethod
    def write_default(project_home: pathlib.Path) -> None:
        atomic_write_bytes(project_home / LIBRARY_TOML, toml_dumps(DEFAULT_LIBRARY_TOML).encode("utf-8"))


# ------------------------------------------------------------- machine.toml

@dataclasses.dataclass
class MachineConfig:
    name: str
    project_home: str
    quarantine_root: str | None = None
    exiftool: str | None = None
    hash_threads: int = 4
    exiftool_processes: int = 2
    cache_placement: str = "in_folder"
    roots: dict[str, str] = dataclasses.field(default_factory=dict)
    extra: dict = dataclasses.field(default_factory=dict)

    def to_toml(self) -> str:
        d: dict[str, Any] = {"schema": SPEC_VERSION, "name": self.name, "project_home": self.project_home}
        if self.quarantine_root:
            d["quarantine_root"] = self.quarantine_root
        if self.exiftool:
            d["exiftool"] = self.exiftool
        d.update(hash_threads=self.hash_threads, exiftool_processes=self.exiftool_processes,
                 cache_placement=self.cache_placement, **self.extra, roots=dict(self.roots))
        return toml_dumps(d)

    @classmethod
    def load(cls, state_dir: pathlib.Path) -> "MachineConfig":
        p = state_dir / MACHINE_TOML
        if not os.path.exists(long_path(p)):
            raise ConfigError(f"{p} not found: this machine isn't set up (run 'photohaven init')")
        d = read_toml(p)
        known = {"schema", "name", "project_home", "quarantine_root", "exiftool", "hash_threads",
                 "exiftool_processes", "cache_placement", "roots"}
        try:
            mc = cls(d["name"], d["project_home"], d.get("quarantine_root"), d.get("exiftool"),
                     int(d.get("hash_threads", 4)), int(d.get("exiftool_processes", 2)),
                     d.get("cache_placement", "in_folder"), dict(d.get("roots", {})),
                     {k: v for k, v in d.items() if k not in known})
        except KeyError as e:
            raise ConfigError(f"{p}: missing {e}") from None
        if mc.cache_placement not in ("in_folder", "local_only"):
            raise ConfigError(f"{p}: cache_placement must be 'in_folder' or 'local_only'")
        return mc

    def save(self, state_dir: pathlib.Path) -> None:
        atomic_write_bytes(state_dir / MACHINE_TOML, self.to_toml().encode("utf-8"))


def folder_flag(relpath: str, folders: list[str]) -> bool:
    """True if relpath is one of `folders` or under one (they cover the subtree)."""
    k = path_key(relpath)
    for f in folders:
        fk = path_key(f.strip("/"))
        if fk == "" or k == fk or k.startswith(fk + "/"):
            return True
    return False


__all__ = ["ConfigError", "FormatError", "Library", "LibrarySettings", "MachineConfig", "RootInfo",
           "default_state_dir", "folder_flag", "toml_dumps", "read_toml"]
