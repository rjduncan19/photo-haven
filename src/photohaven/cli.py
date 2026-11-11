"""Command-line entry point: `photohaven ...` (docs/02 "CLI").

Commands: init, machine, root, scan, report, decisions, status, doctor, rebuild-db.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

from . import __version__, app
from .config import ConfigError
from .formats import FormatError
from .identity import LockError
from .apply import ApplyError
from .scan.exiftool import ExifToolError
from .scan.scanner import ScanOptions


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} TB"


class _Progress:
    def __init__(self, quiet: bool):
        self.quiet = quiet or not sys.stderr.isatty()
        self.last = 0.0

    def __call__(self, msg: str) -> None:
        if msg.startswith("warning: "):
            if not self.quiet:
                sys.stderr.write("\r\x1b[K")
            sys.stderr.write(msg + "\n")
            return
        if self.quiet:
            return
        now = time.monotonic()
        if now - self.last >= 0.5:
            self.last = now
            sys.stderr.write("\r\x1b[K" + msg[:110])
            sys.stderr.flush()

    def done(self) -> None:
        if not self.quiet:
            sys.stderr.write("\r\x1b[K")


def _root_arg(s: str) -> tuple[str, pathlib.Path]:
    name, eq, path = s.partition("=")
    if not eq or not name or not path:
        raise argparse.ArgumentTypeError("expected NAME=PATH")
    return name, pathlib.Path(path)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="photohaven", description="Photo Haven: family photo library organizer.")
    p.add_argument("--version", action="version", version=f"photohaven {__version__}")
    p.add_argument("--state", type=pathlib.Path, help="local state folder (default %%LOCALAPPDATA%%\\PhotoHaven, "
                   "or $PHOTOHAVEN_STATE)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="set up this machine; create or join a library")
    s.add_argument("--project-home", type=pathlib.Path, required=True)
    s.add_argument("--root", type=_root_arg, action="append", default=[], metavar="NAME=PATH")
    s.add_argument("--quarantine", help="override: one folder for moved duplicates instead of each root's "
                   "PhotoHaven-DELETE (must be outside every root)")
    s.add_argument("--name", default="", help="library name (new libraries)")
    s.add_argument("--machine-name")
    s.add_argument("--exiftool", help="path to exiftool(.exe)")
    s.add_argument("--read-only", action="store_true",
                   help="write nothing into the roots: no root markers, folder caches kept in local state")

    s = sub.add_parser("machine", help="this machine's identity")
    ms = s.add_subparsers(dest="sub", required=True)
    ms.add_parser("show")
    r = ms.add_parser("rename")
    r.add_argument("name")

    s = sub.add_parser("root", help="library roots")
    rs = s.add_subparsers(dest="sub", required=True)
    r = rs.add_parser("add")
    r.add_argument("name")
    r.add_argument("path", type=pathlib.Path)
    r.add_argument("--intake", action="store_true")
    r.add_argument("--kind", choices=["onedrive", "local", "removable"])
    r = rs.add_parser("map")
    r.add_argument("root")
    r.add_argument("path", type=pathlib.Path)
    rs.add_parser("list")

    s = sub.add_parser("scan", help="scan roots into facts (read-only for photos)")
    s.add_argument("--root", action="append", dest="roots")
    s.add_argument("--folder", help="limit to this folder (relative to the root) and below")
    s.add_argument("--no-folder-cache", action="store_true", help="write caches under local state, not into folders")
    s.add_argument("--hash-scope", choices=["candidates", "all"])
    s.add_argument("--rehash", action="store_true", help="ignore cached facts")
    s.add_argument("--audit", type=int, default=0, metavar="N", help="rehash N random cached files and compare")
    s.add_argument("--adopt-root", action="store_true", help="write a missing root marker")
    s.add_argument("--no-exiftool", action="store_true", help="hash only; describe later")
    s.add_argument("--threads", type=int)
    s.add_argument("--quiet", action="store_true")
    s.add_argument("--json", action="store_true", help="print the result as JSON")

    s = sub.add_parser("report", help="generate the static HTML report")
    s.add_argument("--out", type=pathlib.Path, help="report folder (default: local state reports/<report_id>)")
    s.add_argument("--open", action="store_true", help="open it in the default browser")
    s.add_argument("--no-thumbs", action="store_true")
    s.add_argument("--thumb-limit", type=int, default=5000, help="new thumbnails to extract this run")

    s = sub.add_parser("protect", help="protect a folder tree (e.g. shared with others): folder removes are ignored there")
    s.add_argument("path", help="ROOT:relpath (or relpath with a single root)")
    s.add_argument("--off", action="store_true", help="remove the protection")

    s = sub.add_parser("decisions", help="decision logs")
    ds = s.add_subparsers(dest="sub", required=True)
    d = ds.add_parser("import", help="append a report export to this machine's log")
    d.add_argument("file", type=pathlib.Path)
    d = ds.add_parser("list")
    d.add_argument("--target", help="only decisions on this path or below")
    d = ds.add_parser("show-effective", help="how the decision for a file is resolved")
    d.add_argument("path", help="ROOT:relpath (or relpath with a single root)")
    d = ds.add_parser("add", help="record a decision from the command line")
    d.add_argument("action", choices=["keep", "remove", "undecided"])
    d.add_argument("path", help="ROOT:relpath of a folder or file")
    d.add_argument("--subtree", action="store_true", help="folder: apply to it and everything below")
    d.add_argument("--note")

    s = sub.add_parser("plan", help="dry run: what apply would move, and what stays")
    s.add_argument("--decisions", type=pathlib.Path, help="import this report export first")
    s.add_argument("--top", type=int, default=10)
    s = sub.add_parser("apply", help="move planned duplicates into PhotoHaven-DELETE (dry run unless --execute)")
    s.add_argument("--decisions", type=pathlib.Path, help="import this report export first")
    s.add_argument("--execute", action="store_true", help="carry out the last plan you reviewed")
    s.add_argument("--batch-limit", type=int, help="move at most N files this run")
    s.add_argument("--no-rescan", action="store_true", help="skip the light rescan after moving")
    s.add_argument("--top", type=int, default=10)
    s.add_argument("--quiet", action="store_true")
    s = sub.add_parser("verify", help="re-hash moved files and check their kept copies")
    s.add_argument("--batch")
    s.add_argument("--deep", action="store_true", help="also re-hash the kept copies, and re-check verified items")
    s = sub.add_parser("purge", help="permanently delete a verified batch from its DELETE folder (dry run unless --execute)")
    s.add_argument("--batch", required=True)
    s.add_argument("--execute", action="store_true")
    s.add_argument("--min-age", type=float, default=14, metavar="DAYS", help="hold period (default 14 days)")
    s = sub.add_parser("restore", help="put a batch (or part of it) back where it came from")
    s.add_argument("--batch", required=True)
    s.add_argument("--path", help="only this file or folder (relative to its root)")
    s = sub.add_parser("batches", aliases=["quarantine"], help="batches of moved duplicates (PhotoHaven-DELETE)")
    qs = s.add_subparsers(dest="sub", required=True)
    qs.add_parser("list")
    q = qs.add_parser("show")
    q.add_argument("batch")
    s = sub.add_parser("tidy", help="remove folders that hold only Photo Haven cache files")
    s.add_argument("--empty-folders", action="store_true", required=True)
    s.add_argument("--execute", action="store_true")

    s = sub.add_parser("status", help="counts, duplicates, last scan")
    s.add_argument("--json", action="store_true")
    sub.add_parser("doctor", help="check this machine's setup")
    s = sub.add_parser("rebuild-db", help="rebuild the database from caches (hashes nothing)")
    s.add_argument("--quiet", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _dispatch(args)
    except (ConfigError, FormatError, LockError, ExifToolError, ApplyError, ValueError) as e:
        print(f"photohaven: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted: every folder finished so far is saved; run scan again to continue", file=sys.stderr)
        return 130


def _dispatch(args) -> int:
    from .config import default_state_dir
    state = args.state or default_state_dir()
    if args.cmd == "init":
        env, notes = app.init(state, args.project_home, args.root, quarantine=args.quarantine,
                              library_name=args.name, machine_name=args.machine_name, exiftool=args.exiftool,
                              read_only=args.read_only)
        for n in notes:
            print(n)
        print(f"machine {env.machine.name} ({env.machine.short}); local state {env.state_dir}")
        env.close()
        return 0
    if args.cmd == "restore":
        from .restore import restore
        try:
            env = app.open_env(state)
        except (ConfigError, FormatError) as e:
            print(f"note: {e}", file=sys.stderr)
            env = None
        try:
            r = restore(state, args.batch, args.path, env=env)
        finally:
            if env:
                env.close()
        for n in r.notes:
            print("note: " + n)
        print(f"batch {r.batch_id}: restored {len(r.restored)}, beside a different file {len(r.beside)}, "
              f"already there {len(r.already)}, refused {len(r.refused)}")
        for p, new in r.beside:
            print(f"  {p} -> {new}")
        for p, why in r.refused:
            print(f"  refused {p}: {why}")
        if r.restored or r.beside:
            print("Run 'photohaven scan' so the database and caches see the restored files.")
        return 1 if r.refused else 0
    if args.cmd == "doctor":
        checks = app.doctor(state)
        for c in checks:
            print(f"[{c.level.upper():4}] {c.what}: {c.detail}")
        return 1 if any(c.level == "fail" for c in checks) else 0
    env = app.open_env(state)
    try:
        if args.cmd == "machine":
            if args.sub == "rename":
                env.mcfg.name = env.machine.name = args.name
                env.mcfg.save(env.state_dir)
                from .identity import write_registry
                write_registry(env.project_home, env.machine, force=True)
            print(json.dumps({"machine_id": env.machine.machine_id, "machine_short": env.machine.short,
                              "name": env.machine.name, "created_at": env.machine.created_at,
                              "apply_machine": env.library.apply_machine_id == env.machine.machine_id}, indent=2))
        elif args.cmd == "root":
            if args.sub == "add":
                r = app.root_add(env, args.name, args.path, intake=args.intake, kind=args.kind)
                print(f"added root {r.name} ({r.root_id}) at {args.path}")
            elif args.sub == "map":
                r = app.root_map(env, args.root, args.path)
                print(f"mapped root {r.name} to {args.path}")
            else:
                for r in env.library.roots:
                    print(f"{r.name:20} {r.kind:9} {r.root_id}  {env.mcfg.roots.get(r.root_id, '(not mapped here)')}")
        elif args.cmd == "scan":
            prog = _Progress(args.quiet)
            opts = ScanOptions(roots=args.roots, folder=args.folder, no_folder_cache=args.no_folder_cache,
                               hash_scope=args.hash_scope, rehash=args.rehash, audit=args.audit,
                               adopt_root=args.adopt_root, hash_threads=args.threads)
            res = app.scan(env, opts, use_exiftool=not args.no_exiftool, progress=prog)
            prog.done()
            _print_scan(res, args.json)
            return 0 if not res.failed_roots else 1
        elif args.cmd == "report":
            res = app.make_report(env, out=args.out, thumbs=not args.no_thumbs, thumb_limit=args.thumb_limit)
            index = res.path / "index.html"
            print(f"report {res.report_id}: {index}")
            print("  " + ", ".join(f"{k} {v}" for k, v in res.sections.items()) + f"; {res.thumbs_made} thumbnails made")
            if args.open:
                import webbrowser
                webbrowser.open(index.as_uri())
        elif args.cmd == "decisions":
            return _decisions(env, args)
        elif args.cmd == "protect":
            root_id, rel = app.parse_target_path(env, args.path)
            r = app.protect(env, root_id, rel, on=not args.off)
            print(("unprotected " if args.off else "protected ") + f"{r.name}:{rel or '/'}; protected now: "
                  + (", ".join(r.protected_folders) or "none"))
        elif args.cmd == "plan" or (args.cmd == "apply" and not args.execute):
            p, listing = app.make_plan(env, args.decisions)
            _print_plan(env, p, listing, args.top)
        elif args.cmd == "apply":
            if args.decisions:
                print("apply --execute runs the plan you reviewed; import decisions with 'plan --decisions' first",
                      file=sys.stderr)
                return 2
            prog = _Progress(args.quiet)
            res = app.apply_execute(env, batch_limit=args.batch_limit, rescan=not args.no_rescan, progress=prog)
            prog.done()
            _print_apply(res)
            return 1 if res.verify_failed else 0
        elif args.cmd == "verify":
            from .apply import Applier
            a = Applier(env)
            a.check_machine()
            for m in a.recover():
                print("recovered: " + m)
            ok, failed = a.verify(args.batch, deep=args.deep)
            print(f"verified {ok}; failed {len(failed)}")
            for p, why in failed:
                print(f"  FAILED {p}: {why}")
            return 1 if failed else 0
        elif args.cmd == "purge":
            from .apply import Applier, purge
            from . import db
            if args.execute:
                db.backup(env.conn, env.state_dir)
            r = purge(Applier(env), args.batch, execute=args.execute, min_age_days=args.min_age)
            verb = "deleted" if args.execute else "would delete"
            print(f"batch {r.batch_id}: {verb} {len(r.deleted or r.would_delete)} files ({_human(r.bytes)}) after a "
                  f"byte-for-byte compare with each kept copy; refused {len(r.refused)}")
            if r.cloud_keepers:
                print(f"  {r.cloud_keepers} kept copies were cloud-only; comparing them downloaded them")
            for p, why in r.refused:
                print(f"  refused {p}: {why}")
            if not args.execute and (r.would_delete or r.refused):
                print("Run again with --execute to delete.")
            return 1 if r.refused else 0
        elif args.cmd in ("batches", "quarantine"):
            return _quarantine(env, args)
        elif args.cmd == "tidy":
            from .apply import tidy
            done = tidy(env, execute=args.execute)
            for d in done:
                print(("removed " if args.execute else "would remove ") + d)
            if not args.execute and done:
                print(f"{len(done)} folder(s); run again with --execute to remove them")
            if not done:
                print("no folders hold only Photo Haven cache files")
        elif args.cmd == "status":
            st = app.status(env)
            if args.json:
                print(json.dumps(st, indent=2))
            else:
                _print_status(st)
        elif args.cmd == "rebuild-db":
            prog = _Progress(args.quiet)
            res = app.rebuild_db(env, progress=prog)
            prog.done()
            print(f"rebuilt from caches: {res.files_seen} files in {res.folders_seen} folders, "
                  f"{res.reused} with facts, {res.pending} to hash on the next scan")
        return 0
    finally:
        env.close()


def _decisions(env, args) -> int:
    from . import decisions as dec
    from .formats import Decision, Target
    from .util import utc_now
    if args.sub == "import":
        r = dec.import_export(env.conn, env.project_home, env.state_dir, env.library, env.machine.machine_id, args.file)
        for w in r.warnings:
            print("warning: " + w, file=sys.stderr)
        print(f"imported {r.added} decision(s) from report {r.report_id}; {r.skipped} already in the log")
        return 0
    dec.ingest(env.conn, env.project_home, env.library.library_id)
    roots = {r.root_id: r.name for r in env.library.roots}
    machines = {r[0]: r[1] for r in env.conn.execute("SELECT machine_id, name FROM machines")}
    if args.sub == "list":
        target = None
        if args.target:
            _, target = app.parse_target_path(env, args.target)
        for r in dec.list_decisions(env.conn, target):
            scope = r["scope"] or (r["sha256"][:12] if r["sha256"] else "")
            print(f"{r['at']}  {machines.get(r['machine_id'], r['machine_id'][:8]):10} {r['action']:9} {r['target_kind']:6} "
                  f"{scope:12} {roots.get(r['root_id'], '?')}:{r['path'] or '/'}" + (f"  # {r['note']}" if r["note"] else ""))
        return 0
    if args.sub == "show-effective":
        root_id, rel = app.parse_target_path(env, args.path)
        row = env.conn.execute("SELECT sha256 FROM files WHERE root_id = ? AND relpath = ?", (root_id, rel)).fetchone()
        if row is None:
            print(f"no file {args.path} in the database (scan first?)", file=sys.stderr)
            return 2
        e = dec.effective(env.conn, env.library, root_id, rel, row["sha256"])
        for s in e.chain:
            mark = "->" if s.used else "  "
            print(f"{mark} {s.level:7} {s.action:9} {s.path or '/'}  ({s.at}, {machines.get(s.machine_id, s.machine_id[:8])})"
                  + (f"  {s.note}" if s.note else ""))
        print(f"effective: {e.action}" + (f" (from the {e.level} decision)" if e.level else " (no decision applies)"))
        return 0
    if args.sub == "add":
        root_id, rel = app.parse_target_path(env, args.path)
        f = env.conn.execute("SELECT sha256, size FROM files WHERE root_id = ? AND relpath = ? AND state = 'present'",
                             (root_id, rel)).fetchone()
        if f is not None:
            if not f["sha256"]:
                print("that file is not hashed yet; scan first", file=sys.stderr)
                return 2
            if args.subtree:
                print("--subtree is for folders", file=sys.stderr)
                return 2
            t = Target("file", root_id, rel, sha256=f["sha256"], size=f["size"])
        else:
            if not env.conn.execute("SELECT 1 FROM folders WHERE root_id = ? AND relpath = ? AND present = 1",
                                    (root_id, rel)).fetchone():
                print(f"no folder or file {args.path} in the database (scan first?)", file=sys.stderr)
                return 2
            t = Target("folder", root_id, rel, scope="subtree" if args.subtree else "folder")
        d = Decision(utc_now(), dec.PHASE, t, args.action, "cli", note=args.note)
        dec.append(env.conn, env.project_home, env.library.library_id, env.machine.machine_id, [d])
        print(f"recorded: {args.action} {t.kind} {args.path}" + (f" ({t.scope})" if t.scope else ""))
        return 0
    return 2


def _print_plan(env, p, listing, top: int) -> None:
    from . import plan as planner
    roots = {r.root_id: r.name for r in env.library.roots}
    machines = {r[0]: r[1] for r in env.conn.execute("SELECT machine_id, name FROM machines")}
    newest = env.conn.execute("SELECT max(at) FROM decisions").fetchone()[0]
    others = env.conn.execute("SELECT machine_id, count(*) FROM decisions WHERE machine_id != ? GROUP BY machine_id",
                              (env.machine.machine_id,)).fetchall()
    extra = "".join(f" incl. {n} from {machines.get(m, m[:8])}" for m, n in others)
    print(f"Plan {p.plan_id} (from scan {p.scan_id}, decisions through {newest or 'none'}{extra})")
    print()
    print(f"  Will move to DELETE folder: {len(p.items):,} files   {_human(p.bytes)}")
    if p.items:
        qs = {}
        for i in p.items:
            q = planner.holding_root(env.mcfg.roots[i.root_id], env.mcfg.quarantine_root or None)
            qs.setdefault(str(q), planner.same_volume(env.mcfg.roots[i.root_id], q))
        for q, same in qs.items():
            how = "same drive: instant moves" if same else "another drive: copy, verify, then remove"
            print(f"  Moved to:                  {q}   ({how})")
    print(f"  Kept (undecided/keep):     {p.kept:,} duplicate instances left in place by your decisions")
    print(f"  Blocked (last copy):       {len(p.blocked):,} files in remove folders have no other copy and stay")
    print(f"  Conflicts:                 {len(p.conflicts):,} groups where every copy was marked remove; kept one of each")
    print(f"  Skipped:                   {len(p.skipped):,}")
    if p.ignored_protected:
        print(f"  Protected:                 {p.ignored_protected:,} folder-level removes ignored in intake/protected folders")
    for w in p.warnings:
        print(f"  warning: {w}")
    if p.items:
        print()
        print("  Top folders:")
        for rid, folder, n, b in planner.top_folders(p, top):
            print(f"    {roots.get(rid, '?')}:{folder or '/'}  {n:,} files  {_human(b)}")
        print()
        print("Run 'photohaven apply --execute' to carry out this plan.")
    print(f"Full list: {listing}")


def _print_apply(res) -> None:
    for m in res.recovered:
        print("recovered: " + m)
    if res.batch_id is None:
        print("nothing to move")
        return
    print(f"batch {', '.join(res.batch_ids)}: moved {res.moved} files ({_human(res.moved_bytes)}) to "
          f"PhotoHaven-DELETE; verified {res.verified}"
          + (f"; {res.remaining} left for later runs" if res.remaining else ""))
    for p, why in res.skipped:
        print(f"  skipped {p}: {why}")
    for p, why in res.verify_failed:
        print(f"  VERIFY FAILED {p}: {why}  (restoring is the next step)")


def _quarantine(env, args) -> int:
    if args.sub == "list":
        for b in env.conn.execute(
                "SELECT b.batch_id, b.created_at, b.state, b.quarantine_root, count(q.relpath), "
                "coalesce(sum(q.size), 0) FROM batches b LEFT JOIN quarantine_items q USING(batch_id) "
                "GROUP BY b.batch_id ORDER BY b.created_at"):
            print(f"{b[0]}  {b[1][:19]}  {b[2]:10} {b[4]:6} files {_human(b[5]):>10}  {b[3]}")
        return 0
    rows = env.conn.execute("SELECT * FROM quarantine_items WHERE batch_id = ? ORDER BY relpath",
                            (args.batch,)).fetchall()
    if not rows:
        print(f"no batch {args.batch}", file=sys.stderr)
        return 2
    for r in rows:
        err = f"  {r['last_error']}" if r["last_error"] else ""
        print(f"{r['state']:13} {r['relpath']}  (keeper {r['keeper_relpath']}){err}")
    return 0


def _print_scan(res, as_json: bool) -> None:
    import dataclasses
    if as_json:
        print(json.dumps(dataclasses.asdict(res), indent=2))
        return
    print(f"scan {res.scan_id} {res.status}: {res.folders_seen} folders, {res.files_seen} files")
    print(f"  hashed {res.files_hashed} ({_human(res.bytes_hashed)}), reused {res.reused}, "
          f"described {res.described}, cache files written {res.cache_files_written}")
    print(f"  cloud-only {res.cloud_only}, not yet hashed {res.pending}, missing {res.missing}, errors {res.errors}"
          + (f", audit mismatches {res.audit_mismatches}" if res.audit_mismatches else ""))
    for r in res.failed_roots:
        print(f"  root {r} was not scanned (see warnings)")


def _print_status(st: dict) -> None:
    print(f"library {st['library']} on {st['machine']}")
    for r in st["roots"]:
        print(f"  root {r['name']}: {r['mapped'] or '(not mapped here)'}")
        for s, v in sorted(r["states"].items()):
            print(f"    {s:12} {v['files']:>8} files {_human(v['bytes']):>10}  hashed {v['hashed']}")
    d = st["dup_groups"]
    print(f"  exact duplicates: {d['groups']} groups, {d['instances']} files, "
          f"{_human(d['reclaimable_bytes'])} reclaimable")
    c = st["content_equivalent_groups"]
    print(f"  same image, different bytes: {c['groups']} groups ({c['contents']} contents), report only")
    print(f"  not yet hashed: {st['pending_hash']}")
    if st.get("quarantine"):
        print("  moved to DELETE folders: " + ", ".join(f"{k} {v['files']} ({_human(v['bytes'])})"
                                          for k, v in sorted(st["quarantine"].items())))
    if st["flags"]:
        print("  flags: " + ", ".join(f"{k} {v}" for k, v in sorted(st["flags"].items())))
    ls = st["last_scan"]
    if ls:
        print(f"  last scan {ls['scan_id']}: {ls['status']} at {ls['finished_at'] or ls['started_at']}, "
              f"{ls['files_seen']} files, {ls['files_hashed']} hashed, {ls['errors']} errors")


if __name__ == "__main__":
    sys.exit(main())
