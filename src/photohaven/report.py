"""Static HTML report (docs/07). A disposable directory under local state
`reports/<report_id>/`: index.html + data.js + detail/<folder_id>.js shards,
opened from disk. Decisions live in localStorage until Export writes
`decisions-<report_id>.jsonl` (03 §5.3), the only contract part of a report.
"""
from __future__ import annotations

import dataclasses
import importlib.resources
import json
import os
import pathlib
import sqlite3

from . import TOOL, analysis, decisions
from .analysis import Analysis, copy_rank, suggest, suggest_subtree
from .config import Library
from .formats import is_sha256
from .util import atomic_write_bytes, atomic_write_text, is_inside, is_placeholder, long_path, utc_now, uuid7

THUMBS_PER_ROW = 6
OVERLAP_MIN = 0.5
SECTION_LIMIT = 2000         # rows per section in the main page; the tree has everything


@dataclasses.dataclass
class ReportResult:
    report_id: str
    path: pathlib.Path
    sections: dict[str, int]
    thumbs_made: int


def _js(name: str, obj) -> str:
    return f"{name}({json.dumps(obj, ensure_ascii=False, separators=(',', ':'))});\n"


def _sugg(s: analysis.Suggestion | None) -> dict | None:
    if s is None:
        return None
    return {k: v for k, v in {"a": s.action, "r": s.reason, "s": s.scope, "l": s.locked or None,
                              "o": s.other}.items() if v is not None}


def build(conn: sqlite3.Connection, library: Library, *, state_dir: pathlib.Path, project_home: pathlib.Path,
          machine_name: str, out: pathlib.Path | None = None, exiftool=None, thumb_limit: int = 5000) -> ReportResult:
    warnings = decisions.ingest(conn, project_home, library.library_id)
    an = analysis.analyze(conn, library)
    report_id = uuid7()
    out = pathlib.Path(out) if out else pathlib.Path(state_dir) / "reports" / report_id
    for rid, p in conn.execute("SELECT root_id, local_path FROM roots WHERE local_path IS NOT NULL"):
        if is_inside(out, p) or is_inside(p, out):
            raise ValueError(f"the report folder {out} would be inside the library root {p}; choose another --out")
    os.makedirs(long_path(out / "detail"), exist_ok=True)
    thumbs_dir = pathlib.Path(state_dir) / "thumbs"
    roots = {r.root_id: r.name for r in library.roots}
    scan = conn.execute("SELECT * FROM scans WHERE status = 'completed' ORDER BY scan_id DESC LIMIT 1").fetchone()

    files_by_folder: dict[int, list[sqlite3.Row]] = {}
    for r in conn.execute("SELECT f.file_id, f.folder_id, f.name, f.size, f.sha256, f.media, f.state, "
                          "c.taken_local, c.model, c.mime, c.content_sha256, c.damaged FROM files f "
                          "LEFT JOIN contents c USING(sha256) WHERE f.state IN ('present', 'cloud_only', 'unreadable') "
                          "ORDER BY f.folder_id, f.name"):
        files_by_folder.setdefault(r["folder_id"], []).append(r)
    flags_by_file: dict[int, list[str]] = {}
    for fid, flag, detail in conn.execute("SELECT file_id, flag, detail FROM flags"):
        flags_by_file.setdefault(fid, []).append(flag)

    # ---- thumbnails (embedded previews only; local state, never OneDrive)
    thumb_rel = os.path.relpath(thumbs_dir, out) if os.path.splitdrive(str(thumbs_dir))[0].lower() == \
        os.path.splitdrive(str(out))[0].lower() else thumbs_dir.as_uri()
    made = 0
    have: set[str] = set()

    def thumb(sha: str, path: str | None, media: str) -> str | None:
        nonlocal made
        if media not in ("image", "raw") or not is_sha256(sha):
            return None
        p = thumbs_dir / sha[:2] / f"{sha}.jpg"
        if sha in have or p.exists():
            have.add(sha)
            return f"{thumb_rel}/{sha[:2]}/{sha}.jpg".replace("\\", "/")
        if exiftool is None or path is None or made >= thumb_limit:
            return None
        try:
            if is_placeholder(os.stat(long_path(path))):
                return None             # became cloud-only since the scan: reading would download it (rule 4)
            data = exiftool.embedded_preview(path)
        except Exception:
            data = None
        made += 1
        if not data or not is_inside(p, thumbs_dir):      # hash-named, so always inside; checked before writing
            return None
        atomic_write_bytes(p, data)
        have.add(sha)
        return f"{thumb_rel}/{sha[:2]}/{sha}.jpg".replace("\\", "/")

    root_paths = {}
    for r in conn.execute("SELECT root_id, local_path FROM roots"):
        root_paths[r[0]] = r[1]

    def abs_path(fid: int, name: str) -> str | None:
        f = an.folders[fid]
        base = root_paths.get(f.root_id)
        if not base:
            return None
        return os.path.join(base, *f.relpath.split("/"), name) if f.relpath else os.path.join(base, name)

    # ---- folders
    folders_out: dict[int, dict] = {}
    for fid, f in an.folders.items():
        s = an.subtrees[fid]
        sg = suggest(an, fid)
        ssg = suggest_subtree(an, fid)
        files = files_by_folder.get(fid, [])
        thumbs = []
        for r in files:
            if len(thumbs) >= THUMBS_PER_ROW:
                break
            if r["sha256"] and r["state"] == "present":
                t = thumb(r["sha256"], abs_path(fid, r["name"]), r["media"])
                if t:
                    thumbs.append(t)
        copies = _copies_live_in(an, fid) if f.redundant else []
        folders_out[fid] = {k: v for k, v in {
            "id": fid, "root": f.root_id, "path": f.relpath, "name": f.name or roots.get(f.root_id, ""),
            "depth": f.depth, "parent": f.parent_id, "event": f.event_guess, "generic": f.is_generic or None,
            "locked": f.locked or None, "files": f.files, "bytes": f.bytes, "unhashed": f.unhashed or None,
            "dup": f.dup_files, "unique": f.unique_files, "internal": f.internal_extra or None,
            "reclaim": f.reclaimable, "redundant": f.redundant or None,
            "sub": {"files": s.files, "bytes": s.bytes, "dup": s.dup_files, "dupBytes": s.dup_bytes,
                    "unhashed": s.unhashed, "n": s.folders_with_files, "redundant": s.redundant or None},
            "sugg": _sugg(sg), "subsugg": _sugg(ssg), "thumbs": thumbs or None, "copies": copies or None,
        }.items() if v is not None}

    # ---- sections (07 §3)
    contained, redundant_spread, internal, trees = [], [], [], []
    for fid, f in an.folders.items():
        if not f.files:
            continue
        b, _ = analysis.best_container(an, fid)
        if b is not None:
            if suggest(an, fid).action == "remove":    # contained folders we suggest keeping stay out of "wins"
                contained.append(fid)
        elif f.redundant:
            redundant_spread.append(fid)
        if f.internal_extra:
            internal.append(fid)
    by_reclaim = lambda fid: (-an.folders[fid].reclaimable, an.folders[fid].relpath)
    contained.sort(key=by_reclaim)
    redundant_spread.sort(key=by_reclaim)
    internal.sort(key=lambda fid: (-an.folders[fid].internal_extra_bytes, an.folders[fid].relpath))
    for fid, f in an.folders.items():
        if suggest_subtree(an, fid) is None:
            continue
        parent = f.parent_id
        if parent is not None and parent in an.folders and suggest_subtree(an, parent) is not None:
            continue        # only the top-most redundant tree
        trees.append(fid)
    trees.sort(key=lambda fid: (-an.subtrees[fid].dup_bytes, an.folders[fid].relpath))
    overlap = []
    for (a, b), p in an.pairs.items():
        if p.contained or (p.identical) or p.a_in_b < OVERLAP_MIN:
            continue
        if a > b and (b, a) in an.pairs and an.pairs[(b, a)].a_in_b >= OVERLAP_MIN and not an.pairs[(b, a)].contained:
            continue        # show each pair once
        overlap.append([a, b, p.shared, round(p.a_in_b, 3), round(p.b_in_a, 3), p.shared_bytes])
    overlap.sort(key=lambda x: (-x[5], x[0], x[1]))

    content_equiv = _content_groups(conn, roots)
    warn = _warnings(conn, warnings)
    orphan = [{"path": r["path"], "root": r["root_id"], "kind": r["target_kind"], "action": r["action"],
               "at": r["at"]} for r in decisions.orphaned(conn)]

    # ---- summary
    q = lambda sql, *a: conn.execute(sql, a).fetchone()
    dup = q("SELECT count(*), coalesce(sum(instances), 0), coalesce(sum(reclaimable), 0) FROM dup_groups")
    tot = q("SELECT count(*), coalesce(sum(size), 0) FROM files WHERE state IN ('present', 'cloud_only')")
    cloud = q("SELECT count(*), coalesce(sum(sha256 IS NULL), 0) FROM files WHERE state = 'cloud_only'")
    pending = q("SELECT count(*) FROM files WHERE state = 'present' AND sha256 IS NULL AND media != 'other'")[0]
    machines = [r[0] or r[1] for r in conn.execute(
        "SELECT DISTINCT m.name, f.hashed_by FROM files f LEFT JOIN machines m ON m.machine_id = f.hashed_by "
        "WHERE f.hashed_by IS NOT NULL")]
    summary = {"files": tot[0], "bytes": tot[1], "dupGroups": dup[0], "dupInstances": dup[1], "reclaimable": dup[2],
               "cloudOnly": cloud[0], "cloudOnlyUnhashed": cloud[1], "unhashed": pending,
               "hashPass": "all" if pending == 0 else "candidates",
               "scanAt": scan["finished_at"] if scan else None, "machines": sorted(set(machines)),
               "contentGroups": len(content_equiv)}

    data = {"reportId": report_id, "generatedAt": utc_now(), "tool": TOOL, "libraryId": library.library_id,
            "libraryName": library.name, "scanId": scan["scan_id"] if scan else None, "machine": machine_name,
            "roots": roots, "summary": summary, "folders": folders_out,
            "sections": {"contained": contained[:SECTION_LIMIT], "redundant": redundant_spread[:SECTION_LIMIT],
                         "trees": trees[:SECTION_LIMIT], "overlap": overlap[:SECTION_LIMIT],
                         "internal": internal[:SECTION_LIMIT], "contentEquiv": content_equiv[:SECTION_LIMIT],
                         "warnings": warn[:SECTION_LIMIT], "orphaned": orphan}}

    # ---- detail shards
    for fid, f in an.folders.items():
        rows = files_by_folder.get(fid, [])
        if not rows:
            continue
        internal_keep = _internal_keepers(an, fid, rows)
        det = []
        for r in rows:
            sha = r["sha256"]
            others = []
            if sha:
                for ofid, oname, ofile in an.instances.get(sha, []):
                    if ofile != r["file_id"]:
                        others.append(f"{roots.get(an.folders[ofid].root_id, '?')}:{_join(an.folders[ofid].relpath, oname)}")
            item = {"id": r["file_id"], "name": r["name"], "size": r["size"], "sha": sha, "media": r["media"],
                    "state": r["state"], "taken": r["taken_local"], "model": r["model"],
                    "thumb": thumb(sha, abs_path(fid, r["name"]), r["media"]) if sha and r["state"] == "present" else None,
                    "copies": others[:8], "ncopies": len(others), "flags": flags_by_file.get(r["file_id"]),
                    "damaged": bool(r["damaged"]) or None}
            if sha in internal_keep and internal_keep[sha] != r["file_id"]:
                item["sugg"] = {"a": "remove", "r": "Same bytes as another file in this folder"}
            det.append({k: v for k, v in item.items() if v not in (None, [], "")})
        atomic_write_text(out / "detail" / f"{fid}.js", _js("PH_DETAIL", {"id": fid, "files": det}))

    # ---- write
    atomic_write_text(out / "data.js", "window.PH_DATA = " + json.dumps(data, ensure_ascii=False,
                                                                       separators=(",", ":")) + ";\n")
    assets = importlib.resources.files("photohaven").joinpath("report_assets")
    for name in ("index.html", "app.js", "style.css"):
        atomic_write_text(out / name, assets.joinpath(name).read_text(encoding="utf-8"))
    meta = {"report_id": report_id, "generated_at": data["generatedAt"], "scan_id": data["scanId"],
            "library_id": library.library_id, "path": str(out)}
    atomic_write_text(pathlib.Path(state_dir) / "reports" / report_id / "report.json", json.dumps(meta, indent=2))
    sections = {k: len(v) for k, v in data["sections"].items()}
    return ReportResult(report_id, out, sections, made)


def _join(rel: str, name: str) -> str:
    return f"{rel}/{name}" if rel else name


def _copies_live_in(an: Analysis, fid: int) -> list[list]:
    """For a redundant folder: where its copies live, as [folder_id, share]."""
    f = an.folders[fid]
    cnt: dict[int, int] = {}
    for sha in f.contents:
        for ofid in {o for o, _, _ in an.instances[sha] if o != fid}:
            cnt[ofid] = cnt.get(ofid, 0) + 1
    n = max(1, len(f.contents))
    return [[o, round(c / n, 3)] for o, c in sorted(cnt.items(), key=lambda x: -x[1])[:4]]


def _internal_keepers(an: Analysis, fid: int, rows) -> dict[str, int]:
    """sha -> file_id to keep, for contents whose only copies are in this folder."""
    by_sha: dict[str, list] = {}
    for r in rows:
        if r["sha256"]:
            by_sha.setdefault(r["sha256"], []).append(r)
    out = {}
    for sha, rs in by_sha.items():
        if len(rs) > 1 and len(an.instances.get(sha, [])) == len(rs):
            out[sha] = min(rs, key=lambda r: copy_rank(r["name"]))["file_id"]
    return out


def _content_groups(conn, roots) -> list[dict]:
    groups: dict[int, list] = {}
    for r in conn.execute(
            "SELECT l.link_id, f.root_id, f.relpath, f.size, f.sha256, c.taken_local, c.taken_offset, c.make, "
            "c.model, c.gps_lat, c.gps_lon, c.width, c.height FROM links l JOIN link_members m USING(link_id) "
            "JOIN files f USING(file_id) JOIN contents c ON c.sha256 = f.sha256 WHERE l.kind = 'content_equiv' "
            "ORDER BY l.link_id, f.relpath"):
        groups.setdefault(r["link_id"], []).append({
            "root": r["root_id"], "path": r["relpath"], "size": r["size"], "sha": r["sha256"],
            "taken": r["taken_local"], "offset": r["taken_offset"], "camera": " ".join(x for x in (r["make"], r["model"]) if x) or None,
            "gps": f"{r['gps_lat']:.5f}, {r['gps_lon']:.5f}" if r["gps_lat"] is not None else None})
    out = []
    for members in groups.values():
        diff = [k for k in ("taken", "offset", "camera", "gps") if len({m.get(k) for m in members}) > 1]
        same_time = len({(m.get("taken"), m.get("offset")) for m in members}) == 1
        out.append({"members": members, "differs": diff, "sameTime": same_time})
    return out


def _warnings(conn, ingest_warnings: list[str]) -> list[dict]:
    out = []
    labels = {"damaged": "Damaged file (ExifTool)", "cache_conflict": "Cached facts disagreed; rehashed",
              "path_key_collision": "Two names that differ only by case or Unicode form",
              "exiftool_error": "ExifTool could not read the file", "unreadable": "Could not read the file",
              "zero_bytes": "Empty file"}
    for r in conn.execute("SELECT g.flag, g.detail, f.root_id, f.relpath FROM flags g JOIN files f USING(file_id) "
                          "WHERE f.state IN ('present', 'cloud_only', 'unreadable') ORDER BY g.flag, f.relpath"):
        out.append({"kind": r["flag"], "label": labels.get(r["flag"], r["flag"]), "root": r["root_id"],
                    "path": r["relpath"], "detail": r["detail"]})
    for w in ingest_warnings:
        out.append({"kind": "log", "label": "Decision log", "detail": w})
    return out
