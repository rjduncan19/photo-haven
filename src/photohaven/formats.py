"""Readers and writers for every persisted record (docs/03).

This is the only module that knows the byte layout of anything on disk
other than the database (docs/02, "Rule"). Everything else works with the
typed records defined here.
"""
from __future__ import annotations

import dataclasses
import decimal
import json
import math
import os
import pathlib
from typing import Any, Iterable

from . import SPEC_VERSION
from .util import long_path, sort_key

CACHE_PREFIX = ".photohaven-cache."
CACHE_SUFFIX = ".jsonl"
ROOT_MARKER = ".photohaven-root.json"
OWN_FILE_PREFIX = ".photohaven-"
MEDIA_KINDS = ("image", "raw", "video", "sidecar", "other")
SKIP_REASONS = ("cloud_only", "unreadable", "locked", "too_large")


class FormatError(Exception):
    """A file doesn't follow the spec."""


class NewerSchemaError(FormatError):
    """§1.8: refuse files written by a newer spec version; never guess."""


# ------------------------------------------------------------ canonical JSON

def _num(x: float) -> str:
    if math.isnan(x) or math.isinf(x):
        raise ValueError("NaN and infinity are not JSON")
    if x == int(x) and abs(x) < 2**53:
        return str(int(x))
    r = repr(x)  # shortest round-trip form
    if "e" in r or "E" in r:
        r = format(decimal.Decimal(r), "f")
    return r


def canonical(obj: Any) -> str:
    """§1.1 canonical JSON: keys sorted by code point, no spaces, non-ASCII
    unescaped, integral numbers without a decimal point, no exponents."""
    if obj is None:
        return "null"
    if obj is True:
        return "true"
    if obj is False:
        return "false"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, float):
        return _num(obj)
    if isinstance(obj, str):
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, dict):
        for k in obj:
            if not isinstance(k, str):
                raise TypeError(f"non-string key {k!r}")
        return "{" + ",".join(json.dumps(k, ensure_ascii=False) + ":" + canonical(obj[k])
                              for k in sorted(obj)) + "}"
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(canonical(v) for v in obj) + "]"
    raise TypeError(f"not serializable: {type(obj).__name__}")


def prune(d: dict) -> dict:
    """Absent, not null; false booleans that default to false are left out."""
    return {k: v for k, v in d.items() if v is not None and v is not False}


def check_schema_version(obj: dict, where: str) -> None:
    v = obj.get("schema")
    if not isinstance(v, int):
        raise FormatError(f"{where}: missing or bad 'schema'")
    if v > SPEC_VERSION:
        raise NewerSchemaError(
            f"{where}: written by spec version {v}; this Photo Haven understands {SPEC_VERSION}. "
            "Upgrade Photo Haven on this machine.")


# ------------------------------------------------------------- JSON documents

def read_json_doc(path: pathlib.Path | str, kind: str) -> dict:
    """library.json, root marker, machine.json, machines/<id>.json."""
    with open(long_path(path), "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise FormatError(f"{path}: not valid JSON ({e})") from None
    if not isinstance(obj, dict):
        raise FormatError(f"{path}: not a JSON object ({kind})")
    check_schema_version(obj, str(path))
    return obj


def json_doc_bytes(obj: dict) -> bytes:
    return (json.dumps(prune(obj), indent=2, ensure_ascii=False) + "\n").encode("utf-8")


# ------------------------------------------------------------------- JSONL

@dataclasses.dataclass
class JsonlFile:
    header: dict
    records: list[dict]
    warnings: list[str]


def read_jsonl(path: pathlib.Path | str, kind: str, *, append_only: bool = False) -> JsonlFile:
    """Read a JSONL file whose first line is a header of `kind`.

    A last line that fails to parse is ignored with a warning (§1.1, crash
    mid-append). In an append-only log, a torn line stays in the middle once
    the next writer appends after it on a fresh line, so there any bad line
    after the header is skipped with a warning. Elsewhere it is a FormatError."""
    with open(long_path(path), "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    lines = raw.decode("utf-8", errors="strict").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    warnings: list[str] = []
    objs: list[dict] = []
    for i, line in enumerate(lines):
        line = line.rstrip("\r")
        try:
            obj = json.loads(line)
            if not isinstance(obj, dict):
                raise ValueError("not an object")
        except ValueError as e:
            if i == len(lines) - 1 or (append_only and i > 0):
                warnings.append(f"{path}:{i + 1}: ignored unreadable line ({e})")
                continue
            raise FormatError(f"{path}:{i + 1}: bad line ({e})") from None
        objs.append(obj)
    if not objs or objs[0].get("record") != "header":
        raise FormatError(f"{path}: first line is not a header")
    header = objs[0]
    check_schema_version(header, str(path))
    if header.get("kind") != kind:
        raise FormatError(f"{path}: header kind {header.get('kind')!r}, expected {kind!r}")
    return JsonlFile(header, objs[1:], warnings)


def jsonl_bytes(header: dict, records: Iterable[dict]) -> bytes:
    return "".join(canonical(r) + "\n" for r in [header, *records]).encode("utf-8")


# ---------------------------------------------------------- folder cache (§3)

_FACT_FIELDS = ("name", "size", "mtime_us", "ctime_us", "media", "sha256",
                "content_sha256", "content_hash_impl", "meta", "meta_impl", "skip")


@dataclasses.dataclass
class FileFacts:
    """One §3.2 file record."""
    name: str
    size: int
    mtime_us: int
    media: str
    sha256: str | None
    observed_at: str
    ctime_us: int | None = None
    content_sha256: str | None = None
    content_hash_impl: str | None = None
    meta: dict | None = None
    meta_impl: str | None = None
    skip: str | None = None
    extra: dict = dataclasses.field(default_factory=dict)   # unknown fields, preserved

    def to_record(self) -> dict:
        d = {"record": "file", "name": self.name, "size": self.size, "mtime_us": self.mtime_us,
             "ctime_us": self.ctime_us, "media": self.media, "sha256": self.sha256,
             "content_sha256": self.content_sha256, "content_hash_impl": self.content_hash_impl,
             "meta": prune(self.meta) if self.meta else None, "meta_impl": self.meta_impl,
             "observed_at": self.observed_at, "skip": self.skip}
        out = {**self.extra, **prune(d)}
        out["sha256"] = self.sha256          # required-but-nullable (§3.2)
        if out.get("meta") == {}:
            del out["meta"]
        return out

    def facts_line(self) -> str:
        """Canonical record without provenance (observed_at): two machines
        holding the same facts produce the same string (§3.2 'Refinement')."""
        r = self.to_record()
        r.pop("observed_at", None)
        return canonical(r)

    @classmethod
    def from_record(cls, d: dict) -> "FileFacts":
        try:
            name, size, mtime = d["name"], d["size"], d["mtime_us"]
            media, sha, observed = d["media"], d.get("sha256"), d["observed_at"]
        except KeyError as e:
            raise FormatError(f"file record missing {e}") from None
        if not isinstance(name, str) or not name or "/" in name:
            raise FormatError(f"bad name {name!r}")
        if not isinstance(size, int) or not isinstance(mtime, int):
            raise FormatError(f"{name}: bad size or mtime_us")
        if sha is not None and (not isinstance(sha, str) or len(sha) != 64):
            raise FormatError(f"{name}: bad sha256")
        skip = d.get("skip")
        if (sha is None) != (skip is not None):
            raise FormatError(f"{name}: sha256 must be null exactly when skip is set")
        known = set(_FACT_FIELDS) | {"record", "observed_at"}
        meta = d.get("meta")
        return cls(name=name, size=size, mtime_us=mtime, media=media, sha256=sha,
                   observed_at=observed, ctime_us=d.get("ctime_us"),
                   content_sha256=d.get("content_sha256"), content_hash_impl=d.get("content_hash_impl"),
                   meta=dict(meta) if isinstance(meta, dict) else None, meta_impl=d.get("meta_impl"),
                   skip=skip, extra={k: v for k, v in d.items() if k not in known})


@dataclasses.dataclass
class CacheHeader:
    library_id: str
    root_id: str
    folder: str
    machine_id: str
    written_at: str
    tool: str
    extra: dict = dataclasses.field(default_factory=dict)

    def to_record(self) -> dict:
        return {**self.extra, "record": "header", "schema": SPEC_VERSION, "kind": "folder-cache",
                "library_id": self.library_id, "root_id": self.root_id, "folder": self.folder,
                "machine_id": self.machine_id, "written_at": self.written_at, "tool": self.tool}

    @classmethod
    def from_record(cls, d: dict) -> "CacheHeader":
        try:
            known = {"record", "schema", "kind", "library_id", "root_id", "folder", "machine_id",
                     "written_at", "tool"}
            return cls(d["library_id"], d["root_id"], d["folder"], d["machine_id"], d["written_at"],
                       d["tool"], {k: v for k, v in d.items() if k not in known})
        except KeyError as e:
            raise FormatError(f"cache header missing {e}") from None


@dataclasses.dataclass
class CacheFile:
    path: str
    header: CacheHeader
    records: list[FileFacts]
    warnings: list[str]


def is_cache_file_name(name: str) -> bool:
    """Any `.photohaven-cache.*.jsonl`, including OneDrive conflict copies
    such as `.photohaven-cache.<m>-TOMAHAWK.jsonl` (06 §5)."""
    return name.startswith(CACHE_PREFIX) and name.endswith(CACHE_SUFFIX) and len(name) > 24


def cache_file_name(machine_short: str) -> str:
    return f"{CACHE_PREFIX}{machine_short}{CACHE_SUFFIX}"


def read_cache(path: pathlib.Path | str) -> CacheFile:
    j = read_jsonl(path, "folder-cache")
    header = CacheHeader.from_record(j.header)
    recs, warnings = [], list(j.warnings)
    for i, r in enumerate(j.records, 2):
        if r.get("record") != "file":
            continue   # unknown record types are ignored (§1.1)
        try:
            recs.append(FileFacts.from_record(r))
        except FormatError as e:
            warnings.append(f"{path}:{i}: ignored record ({e})")
    return CacheFile(os.fspath(path), header, recs, warnings)


def sorted_facts(records: Iterable[FileFacts]) -> list[FileFacts]:
    return sorted(records, key=lambda r: sort_key(r.name))


def cache_bytes(header: CacheHeader, records: Iterable[FileFacts]) -> bytes:
    return jsonl_bytes(header.to_record(), [r.to_record() for r in sorted_facts(records)])


def facts_equal(a: Iterable[FileFacts], b: Iterable[FileFacts]) -> bool:
    return [r.facts_line() for r in sorted_facts(a)] == [r.facts_line() for r in sorted_facts(b)]


# ------------------------------------------------------------- root marker

@dataclasses.dataclass
class RootMarker:
    library_id: str
    root_id: str
    name: str
    created_at: str

    def to_bytes(self) -> bytes:
        return json_doc_bytes({"schema": SPEC_VERSION, "library_id": self.library_id,
                               "root_id": self.root_id, "name": self.name, "created_at": self.created_at})

    @classmethod
    def read(cls, path: pathlib.Path | str) -> "RootMarker":
        d = read_json_doc(path, "root marker")
        try:
            return cls(d["library_id"], d["root_id"], d["name"], d["created_at"])
        except KeyError as e:
            raise FormatError(f"{path}: root marker missing {e}") from None


# ------------------------------------------------------------ decisions (§5)

DECISION_ACTIONS = ("keep", "remove", "undecided")


@dataclasses.dataclass
class Target:
    kind: str                      # folder | file
    root_id: str
    path: str
    scope: str | None = None       # folders: folder | subtree
    sha256: str | None = None      # files
    size: int | None = None

    def to_json(self) -> dict:
        return prune({"kind": self.kind, "root_id": self.root_id, "path": self.path, "scope": self.scope,
                      "sha256": self.sha256, "size": self.size})

    @classmethod
    def from_json(cls, d: dict) -> "Target":
        if not isinstance(d, dict):
            raise FormatError("target is not an object")
        kind, path = d.get("kind"), d.get("path")
        if kind not in ("folder", "file") or not isinstance(d.get("root_id"), str) or not isinstance(path, str):
            raise FormatError(f"bad target {d!r}")
        from .util import validate_relpath
        try:
            validate_relpath(path)
        except ValueError as e:
            raise FormatError(str(e)) from None
        if kind == "folder":
            if d.get("scope") not in ("folder", "subtree") or "sha256" in d or "size" in d:
                raise FormatError(f"folder target needs a scope and no sha256/size: {d!r}")
        else:
            sha = d.get("sha256")
            if not path or not isinstance(sha, str) or len(sha) != 64 or not isinstance(d.get("size"), int) \
                    or "scope" in d:
                raise FormatError(f"file target needs path, sha256 and size and no scope: {d!r}")
        return cls(kind, d["root_id"], path, d.get("scope"), d.get("sha256"), d.get("size"))

    def key(self) -> str:
        return canonical(self.to_json())


@dataclasses.dataclass
class Decision:
    """A §5.2 decision record. In an export (§5.3), id/machine_id/seq are None."""
    at: str
    phase: str
    target: Target
    action: str
    origin: str
    id: str | None = None
    machine_id: str | None = None
    seq: int | None = None
    report_id: str | None = None
    note: str | None = None
    extra: dict = dataclasses.field(default_factory=dict)

    def to_record(self) -> dict:
        return {**self.extra, **prune({
            "record": "decision", "id": self.id, "at": self.at, "machine_id": self.machine_id, "seq": self.seq,
            "phase": self.phase, "target": self.target.to_json(), "action": self.action, "origin": self.origin,
            "report_id": self.report_id, "note": self.note})}

    @classmethod
    def from_record(cls, d: dict) -> "Decision":
        known = {"record", "id", "at", "machine_id", "seq", "phase", "target", "action", "origin", "report_id", "note"}
        if d.get("action") not in DECISION_ACTIONS:
            raise FormatError(f"bad action {d.get('action')!r}")
        if d.get("origin") not in ("report", "cli", "import"):
            raise FormatError(f"bad origin {d.get('origin')!r}")
        if not isinstance(d.get("at"), str) or not isinstance(d.get("phase"), str):
            raise FormatError("decision needs 'at' and 'phase'")
        return cls(d["at"], d["phase"], Target.from_json(d.get("target")), d["action"], d["origin"], d.get("id"),
                   d.get("machine_id"), d.get("seq"), d.get("report_id"), d.get("note") or None,
                   {k: v for k, v in d.items() if k not in known})


def read_decision_log(path) -> tuple[dict, list[Decision], list[str]]:
    j = read_jsonl(path, "decision-log", append_only=True)
    out, warnings = [], list(j.warnings)
    for i, r in enumerate(j.records, 2):
        if r.get("record") != "decision":
            continue
        try:
            d = Decision.from_record(r)
            if not (d.id and d.machine_id and isinstance(d.seq, int)):
                raise FormatError("log record needs id, machine_id and seq")
            out.append(d)
        except FormatError as e:
            warnings.append(f"{path}:{i}: ignored decision ({e})")
    return j.header, out, warnings


def read_decision_export(path) -> tuple[dict, list[Decision]]:
    j = read_jsonl(path, "decision-export")
    for k in ("library_id", "report_id", "generated_at"):
        if k not in j.header:
            raise FormatError(f"{path}: export header missing {k}")
    out = []
    for i, r in enumerate(j.records, 2):
        if r.get("record") != "decision":
            continue
        try:
            out.append(Decision.from_record(r))
        except FormatError as e:
            raise FormatError(f"{path}:{i}: {e}") from None
    return j.header, out


def decision_log_header(library_id: str, machine_id: str, created_at: str) -> dict:
    return {"record": "header", "schema": SPEC_VERSION, "kind": "decision-log", "library_id": library_id,
            "machine_id": machine_id, "created_at": created_at}


def append_jsonl(path, header: dict, records: list[dict]) -> None:
    """Append records to an append-only log, creating it with `header`.
    A torn last line (crash mid-append) is left behind and the new records
    start on a fresh line (§1.1)."""
    p = long_path(path)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    new = not os.path.exists(p) or os.path.getsize(p) == 0
    with open(p, "ab+") as f:
        prefix = b""
        if new:
            prefix = (canonical(header) + "\n").encode("utf-8")
        else:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                prefix = b"\n"
        f.seek(0, os.SEEK_END)
        f.write(prefix + "".join(canonical(r) + "\n" for r in records).encode("utf-8"))
        f.flush()
        os.fsync(f.fileno())


# ------------------------------------------------------- quarantine (§6)

QUARANTINE_EVENTS = ("moved", "verified", "verify_failed", "restored", "purge_refused", "purged")


@dataclasses.dataclass
class QuarantineEvent:
    """A §6.1 quarantine log event; the same fields make a §6.2 manifest item."""
    id: str
    at: str
    machine_id: str
    seq: int
    batch_id: str
    event: str
    root_id: str
    path: str
    sha256: str
    size: int
    keeper_root_id: str
    keeper_path: str
    qpath: str
    detail: str | None = None
    extra: dict = dataclasses.field(default_factory=dict)

    def to_record(self, record: str = "quarantine") -> dict:
        return {**self.extra, **prune({
            "record": record, "id": self.id, "at": self.at, "machine_id": self.machine_id, "seq": self.seq,
            "batch_id": self.batch_id, "event": self.event, "root_id": self.root_id, "path": self.path,
            "sha256": self.sha256, "size": self.size,
            "keeper": {"root_id": self.keeper_root_id, "path": self.keeper_path},
            "qpath": self.qpath, "detail": self.detail})}

    @classmethod
    def from_record(cls, d: dict) -> "QuarantineEvent":
        known = {"record", "id", "at", "machine_id", "seq", "batch_id", "event", "root_id", "path", "sha256", "size",
                 "keeper", "qpath", "detail"}
        try:
            k = d["keeper"]
            ev = cls(d["id"], d["at"], d["machine_id"], d["seq"], d["batch_id"], d["event"], d["root_id"], d["path"],
                     d["sha256"], d["size"], k["root_id"], k["path"], d["qpath"], d.get("detail"),
                     {x: v for x, v in d.items() if x not in known})
        except (KeyError, TypeError) as e:
            raise FormatError(f"quarantine record missing {e}") from None
        if ev.event not in QUARANTINE_EVENTS:
            raise FormatError(f"unknown quarantine event {ev.event!r}")
        return ev


def read_quarantine_log(path) -> tuple[dict, list[QuarantineEvent], list[str]]:
    j = read_jsonl(path, "quarantine-log", append_only=True)
    out, warnings = [], list(j.warnings)
    for i, r in enumerate(j.records, 2):
        if r.get("record") != "quarantine":
            continue
        try:
            out.append(QuarantineEvent.from_record(r))
        except FormatError as e:
            warnings.append(f"{path}:{i}: ignored event ({e})")
    return j.header, out, warnings


def read_manifest(path) -> tuple[dict, list[QuarantineEvent], list[str]]:
    j = read_jsonl(path, "quarantine-manifest", append_only=True)
    out, warnings = [], list(j.warnings)
    for i, r in enumerate(j.records, 2):
        if r.get("record") != "item":
            continue
        try:
            out.append(QuarantineEvent.from_record(r))
        except FormatError as e:
            warnings.append(f"{path}:{i}: ignored item ({e})")
    return j.header, out, warnings


def quarantine_log_header(library_id: str, machine_id: str, created_at: str) -> dict:
    return {"record": "header", "schema": SPEC_VERSION, "kind": "quarantine-log", "library_id": library_id,
            "machine_id": machine_id, "created_at": created_at}


def manifest_header(batch_id: str, library_id: str, machine_id: str, plan_id: str, created_at: str) -> dict:
    return {"record": "header", "schema": SPEC_VERSION, "kind": "quarantine-manifest", "batch_id": batch_id,
            "library_id": library_id, "machine_id": machine_id, "plan_id": plan_id, "created_at": created_at}
