"""Restore (docs/08 §8): put files from a DELETE-folder batch back.

Works from the batch manifest alone (I-6): it needs only this machine's
machine.toml (for the root paths) and the manifest. When the project home
and DB are there, it also logs `restored` events and updates the DB; when
they're gone, it says so and still restores.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import pathlib

from . import plan as planner, quarantine
from .config import ConfigError, MachineConfig, default_state_dir
from .formats import FormatError, QuarantineEvent
from .util import long_path, name_key, rel_to_path, split_rel, stem_of

CHUNK = 1 << 20


@dataclasses.dataclass
class RestoreResult:
    batch_id: str
    restored: list[str] = dataclasses.field(default_factory=list)
    beside: list[tuple[str, str]] = dataclasses.field(default_factory=list)       # (path, restored as)
    already: list[str] = dataclasses.field(default_factory=list)
    refused: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    notes: list[str] = dataclasses.field(default_factory=list)


def _sha(path: str) -> tuple[str, int]:
    h, n = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def find_batch(mcfg: MachineConfig, batch_id: str) -> pathlib.Path:
    """The batch folder: in the override folder, or in a root's DELETE folder."""
    cands = []
    if mcfg.quarantine_root:
        cands.append(pathlib.Path(mcfg.quarantine_root) / batch_id)
    for p in mcfg.roots.values():
        cands.append(planner.holding_root(p, None) / batch_id)
    for c in cands:
        if os.path.exists(long_path(c / "manifest.jsonl")):
            return c
    raise ConfigError(f"batch {batch_id} not found: looked for manifest.jsonl in " +
                      ", ".join(str(c) for c in cands))


def _free_name(dst: str) -> str:
    folder, name = os.path.split(dst)
    stem, ext = stem_of(name), name[len(stem_of(name)):]
    n = 1
    while True:
        cand = os.path.join(folder, f"{stem} (restored{'' if n == 1 else f' {n}'}){ext}")
        if not os.path.exists(cand):
            return cand
        n += 1


def restore(state_dir: pathlib.Path | None, batch_id: str, path: str | None = None, *, env=None) -> RestoreResult:
    state_dir = pathlib.Path(state_dir or default_state_dir())
    mcfg = env.mcfg if env else MachineConfig.load(state_dir)
    batch_dir = find_batch(mcfg, batch_id)
    header, items, warnings = quarantine.read_manifest(batch_dir / "manifest.jsonl")
    res = RestoreResult(batch_id, notes=list(warnings))
    journal = None
    if env is not None:
        if env.library.apply_machine_id != env.machine.machine_id:
            raise ConfigError(f"this machine is not the library's apply machine; restore runs there (D-17)")
        if header.get("library_id") != env.library.library_id:
            raise FormatError(f"batch {batch_id} belongs to library {header.get('library_id')}")
        journal = quarantine.Journal(env.project_home, env.library.library_id, env.machine.machine_id)
    else:
        res.notes.append("project home or database not available: restoring from the manifest only; run restore "
                         "again once they're back so the logs record it (it skips files already restored)")
    want = name_key(path.strip("/")) if path else None
    done_events = {(e.root_id, e.path) for e in (journal.events if journal else [])
                   if e.batch_id == batch_id and e.event in ("restored", "purged")}
    for it in items:
        k = name_key(it.path)
        if want and not (k == want or k.startswith(want + "/")):
            continue
        if (it.root_id, it.path) in done_events:
            continue
        base = mcfg.roots.get(it.root_id)
        if not base:
            res.refused.append((it.path, f"root {it.root_id} is not mapped on this machine (photohaven root map)"))
            continue
        q = long_path(rel_to_path(batch_dir.parent, it.qpath))
        dst = long_path(rel_to_path(base, it.path))
        detail = None
        if not os.path.exists(q):
            if os.path.exists(dst) and _sha(dst) == (it.sha256, it.size):
                res.already.append(it.path)
                _record(journal, env, it, "already_present", dst)
                continue
            res.refused.append((it.path, "no longer in the DELETE folder (deleted or purged)"))
            continue
        if _sha(q) != (it.sha256, it.size):
            res.refused.append((it.path, "the copy in the DELETE folder no longer matches its sha256"))
            continue
        if os.path.exists(dst):
            if _sha(dst) == (it.sha256, it.size):
                # the same bytes are back already: the extra copy goes, after a byte compare
                from .apply import _purge_file, same_bytes
                if same_bytes(q, dst):
                    _purge_file(q)
                res.already.append(it.path)
                _record(journal, env, it, "already_present", dst)
                continue
            dst = _free_name(dst)
            detail = f"restored beside a different file, as {os.path.basename(dst)}"
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if planner.same_volume(q, os.path.dirname(dst)):
            os.rename(q, dst)
        else:
            partial = dst + ".partial"
            from .apply import _copy_hashing
            if _copy_hashing(q, partial) != (it.sha256, it.size):
                os.remove(partial)
                res.refused.append((it.path, "the copy back did not match its sha256"))
                continue
            os.rename(partial, dst)
            os.remove(q)                    # Photo Haven's own copy; the verified original is back
        if detail:
            res.beside.append((it.path, os.path.basename(dst)))
        else:
            res.restored.append(it.path)
        _record(journal, env, it, detail, dst)
    from .apply import _remove_empty_dirs
    _remove_empty_dirs(batch_dir / "files")
    return res


def _record(journal, env, it: QuarantineEvent, detail: str | None, dst: str) -> None:
    if journal is None:
        return
    ev = journal.new_event(batch_id=it.batch_id, event="restored", root_id=it.root_id, path=it.path,
                           sha256=it.sha256, size=it.size, keeper_root_id=it.keeper_root_id,
                           keeper_path=it.keeper_path, qpath=it.qpath, detail=detail)
    journal.append(ev)
    c = env.conn
    c.execute("UPDATE quarantine_items SET state = 'restored', last_error = ? WHERE batch_id = ? AND root_id = ? "
              "AND relpath = ?", (detail, it.batch_id, it.root_id, it.path))
    if detail is None or detail == "already_present":
        c.execute("UPDATE files SET state = 'present' WHERE root_id = ? AND relpath = ? AND state IN "
                  "('quarantined', 'missing')", (it.root_id, it.path))
    left = c.execute("SELECT count(*) FROM quarantine_items WHERE batch_id = ? AND state NOT IN ('restored', 'purged')",
                     (it.batch_id,)).fetchone()[0]
    if not left:
        c.execute("UPDATE batches SET state = 'restored' WHERE batch_id = ?", (it.batch_id,))
