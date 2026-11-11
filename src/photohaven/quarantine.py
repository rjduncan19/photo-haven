"""Quarantine logs (03 §6.1): reading every machine's log, and appending to
this machine's. The batch manifest (03 §6.2) is written by apply.
"""
from __future__ import annotations

import dataclasses
import glob
import os
import pathlib

from .formats import (FormatError, QuarantineEvent, append_jsonl, quarantine_log_header, read_manifest,
                      read_quarantine_log)
from .util import long_path, machine_short, name_key, utc_now, uuid7

OUT_EVENTS = ("moved", "verified", "verify_failed", "purge_refused", "purged")


def log_path(project_home: pathlib.Path, machine_id: str) -> pathlib.Path:
    return pathlib.Path(project_home) / "logs" / f"quarantine.{machine_short(machine_id)}.jsonl"


def read_all(project_home: pathlib.Path, library_id: str) -> tuple[list[QuarantineEvent], list[str]]:
    events, warnings = [], []
    d = pathlib.Path(project_home) / "logs"
    for p in sorted(glob.glob(os.path.join(glob.escape(str(d)), "quarantine.*.jsonl"))):
        try:
            header, evs, w = read_quarantine_log(p)
        except (FormatError, OSError, UnicodeDecodeError) as e:
            warnings.append(f"ignoring {p} ({e})")
            continue
        if header.get("library_id") != library_id:
            warnings.append(f"ignoring {p}: it belongs to another library")
            continue
        warnings += w
        events += evs
    return events, warnings


def out_of_library(events: list[QuarantineEvent]) -> dict[tuple[str, str], set[str]]:
    """(root_id, path_key) -> sha256s that some machine moved out and never
    restored (06 §5 layer 2). Keyed on content too, so a new file that later
    appears at the same path is not blocked."""
    last: dict[tuple[str, str, str], QuarantineEvent] = {}
    for e in sorted(events, key=lambda e: (e.at, e.machine_id, e.seq)):
        last[(e.batch_id, e.root_id, e.path)] = e
    out: dict[tuple[str, str], set[str]] = {}
    for e in last.values():
        if e.event in OUT_EVENTS:
            out.setdefault((e.root_id, name_key(e.path)), set()).add(e.sha256)
    return out


class Journal:
    """Appends events to this machine's quarantine log, numbering them."""

    def __init__(self, project_home: pathlib.Path, library_id: str, machine_id: str):
        self.path = log_path(project_home, machine_id)
        self.library_id, self.machine_id = library_id, machine_id
        self.events: list[QuarantineEvent] = []
        if os.path.exists(long_path(self.path)):
            _, self.events, _ = read_quarantine_log(self.path)
        self.seq = max((e.seq for e in self.events), default=0)

    def has(self, batch_id: str, root_id: str, path: str, event: str) -> QuarantineEvent | None:
        for e in self.events:
            if (e.batch_id, e.root_id, e.path, e.event) == (batch_id, root_id, path, event):
                return e
        return None

    def new_event(self, *, batch_id: str, event: str, root_id: str, path: str, sha256: str, size: int,
                  keeper_root_id: str, keeper_path: str, qpath: str, detail: str | None = None) -> QuarantineEvent:
        self.seq += 1
        return QuarantineEvent(uuid7(), utc_now(), self.machine_id, self.seq, batch_id, event, root_id, path, sha256,
                               size, keeper_root_id, keeper_path, qpath, detail)

    def append(self, ev: QuarantineEvent) -> None:
        append_jsonl(self.path, quarantine_log_header(self.library_id, self.machine_id, utc_now()), [ev.to_record()])
        self.events.append(ev)


def manifest_items(manifest: pathlib.Path) -> list[QuarantineEvent]:
    if not os.path.exists(long_path(manifest)):
        return []
    _, items, _ = read_manifest(manifest)
    return items
