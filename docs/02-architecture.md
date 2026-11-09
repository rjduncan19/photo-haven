# 02. Architecture

## Overview

```
                ┌──────────────────────── synced via OneDrive ────────────────────────┐
                │                                                                     │
  Library roots │  Pictures/…/IMG_1234.HEIC                                           │
  (photos)      │  Pictures/…/.photohaven-cache.<machine>.jsonl   ◄── facts, per machine
                │  Pictures/.photohaven-root.json                 ◄── root identity    │
                │                                                                     │
  Project home  │  library.json  library.toml                                         │
  (shared data) │  logs/decisions.<machine>.jsonl                 ◄── append-only      │
                │  logs/quarantine.<machine>.jsonl                ◄── append-only      │
                │  machines/<machine_id>.json                                         │
                └─────────────────────────────────────────────────────────────────────┘

                ┌──────────────────────── local to each machine (not synced) ─────────┐
  Local state   │  %LOCALAPPDATA%\PhotoHaven\                                          │
                │    machine.json  machine.toml  photohaven.db  thumbs\  logs\  lock   │
  Quarantine    │  D:\PhotoHavenQuarantine\<batch_id>\<root_id>\<relpath>              │
                └─────────────────────────────────────────────────────────────────────┘
```

The **SQLite database is a derived index**, never the only copy of anything important. It can be rebuilt from folder caches (facts) plus the logs (decisions, quarantine events). See [06-multi-machine](06-multi-machine.md).

## Storage locations

| Location | Synced? | Holds | Written by |
|---|---|---|---|
| **Library roots** | Yes (OneDrive) | Photos; per-folder cache files; root marker | Scanner (cache files and root marker only); apply (moves out to quarantine); restore (moves back) |
| **Project home** | Yes, but outside every root | `library.json` (identity, roots), `library.toml` (shared settings), per-machine logs, machine registry | `init`, report import, apply, verify, purge |
| **Local state** | No | Machine identity, machine config (root paths, quarantine path, ExifTool path), SQLite DB, thumbnails, app logs, process lock | Every command |
| **Quarantine** | No, and must be outside OneDrive | Moved instances, per-batch manifests | apply, restore, purge |

**Refinement:** the handoff said decision logs are append-only but didn't say where they live. They go in the *project home*, one file per machine, so decisions made on one machine are visible on another without OneDrive ever merging edits to one file. The database stays on local disk because SQLite files in a synced folder risk corruption and conflict copies ([D-07](decisions.md#d-07-the-sqlite-database-lives-on-local-disk-never-in-a-synced-folder)).

**Default project home:** `<OneDrive>\PhotoHaven-data\`. It must not sit inside any library root, so scans never see it.

## Library and roots

**Refinement:** the handoff described paths as "relative to a library root" (singular). A library actually needs several roots: Rick's OneDrive Pictures, Karen's OneDrive Pictures (which syncs to a different folder or a different machine), and possibly memory cards or external drives later. So every path is a `(root_id, relpath)` pair ([D-11](decisions.md#d-11-a-library-has-multiple-roots-paths-are-root_id-relpath)).

- `library.json` lists roots with a `root_id`, display name, kind (`onedrive`, `local`, `removable`), and flags such as `intake` and `protected_folders`.
- Each machine's `machine.toml` maps `root_id` to its local path. Paths differ per machine and per user account.
- A **root marker** file `.photohaven-root.json` at the top of each root holds `library_id` and `root_id`. Before scanning, the scanner checks that the configured path really is the root it thinks it is. This guards against pointing the tool at the wrong folder after a drive letter or account change.

## Components

```
cli ──► config / identity ──► scan ──► link ──► report ──► (human) ──► apply ──► verify ──► purge
                                  │        │         ▲                     │
                                  ▼        ▼         │                     ▼
                           folder caches   DB ◄──────┴───── logs (decisions, quarantine)
```

| Module | Responsibility | Doc |
|---|---|---|
| `photohaven.cli` | Command-line entry points, dry-run/confirm UX, progress output | below |
| `photohaven.config` | Load `library.json`, `library.toml`, `machine.toml`; validate; resolve root paths | 03 |
| `photohaven.identity` | Machine ID creation and lookup, process lock | 06 |
| `photohaven.formats` | Readers and writers for every persisted record (cache, decision, quarantine, manifest, root marker). The *only* code that touches on-disk formats. | 03 |
| `photohaven.db` | Schema creation and migrations, upserts, queries | 03 |
| `photohaven.scan` | Walker, placeholder detection, hasher pool, ExifTool bridge, metadata normalization, cache merge | 04 |
| `photohaven.link` | Duplicate groups, content-equivalent groups, raw pairs, Live Photo pairs, edit derivatives, folder-name cleanup | 05 |
| `photohaven.report` | Folder statistics, containment, suggestions, HTML generation | 07 |
| `photohaven.apply` | Decision resolution, planning, quarantine moves, journal, verify, purge, restore | 08 |
| `photohaven.util` | Path normalization, timestamp formatting, UUIDv7, Windows file attributes, atomic writes | 03 |

**Rule:** only `photohaven.formats` and `photohaven.db` know the byte layout of anything persisted. Every other module works with typed in-memory records. This is what makes a later rewrite in another language a reimplementation of `formats` + `db` against the spec, not an archaeology project.

## Data flow for phase 1

1. **Scan.** For each root and folder: read all machines' cache files, `stat` every file, reuse facts where size and mtime still match, hash and ExifTool what's new or changed, write this machine's cache file (only if its facts changed), and upsert into the DB.
2. **Link.** Recompute duplicate groups, folder overlaps, and relationships in the DB. This is cheap and runs at the end of every scan.
3. **Report.** Generate a static HTML report directory from the DB. The human reviews, sets keep/remove per folder (and per file for leftovers), and exports a decisions file.
4. **Import decisions.** `photohaven decisions import <file>` validates the records and appends them to this machine's decision log. `apply` runs the import automatically if passed a file.
5. **Plan.** Resolve decisions plus current state into concrete moves, enforcing the last-copy invariant. Show the dry run.
6. **Apply.** On confirmation, move each planned instance into a quarantine batch, journaling every step.
7. **Verify.** Immediately after apply, re-hash each quarantined file and confirm the keeper still exists with the same size.
8. **Purge** (later, explicitly). A fresh byte-for-byte compare of each quarantined file against its keeper, then delete from quarantine. Failures stay put and are reported.

## CLI

All commands take `--library <project-home>` (or read it from `machine.toml`) and print what they're going to do. Destructive commands default to dry run.

```
photohaven init --project-home PATH --root NAME=PATH [--root NAME=PATH ...] --quarantine PATH
photohaven machine show | rename NAME | retire ID | reset
photohaven root add NAME PATH [--intake] | map ROOT_ID PATH | list
photohaven scan [--root NAME] [--folder RELPATH] [--no-folder-cache] [--hash-scope candidates|all]
                [--rehash] [--audit N] [--hydrate --hydrate-limit SIZE] [--adopt-root]
photohaven status                         # counts, last scan, pending decisions, quarantine totals
photohaven report [--out DIR] [--open]
photohaven decisions import FILE | list [--target PATH] | show-effective PATH
photohaven plan [--decisions FILE]        # dry run: what would move, space saved, blocked items
photohaven apply [--decisions FILE] [--execute] [--batch-limit N]
photohaven verify [--batch ID]
photohaven quarantine list | show BATCH
photohaven restore --batch ID [--path RELPATH]
photohaven purge --batch ID [--execute] [--min-age DAYS]
photohaven tidy --empty-folders [--execute]   # folders holding only Photo Haven cache files
photohaven cache prune --machine ID [--execute]   # retired machines only
photohaven rebuild-db                     # from caches + logs
photohaven doctor                         # checks ExifTool, long paths, root markers, clock skew, placeholder counts
```

`apply` without `--execute` is identical to `plan`. `purge` without `--execute` prints the byte-compare results it *would* act on, after running the compares.

## Concurrency model

- **One Photo Haven process per machine at a time.** A lock file in local state is held for the life of the process. The DB uses WAL mode, so the report generator can read while a scan runs if that's ever needed.
- **Many machines at a time are fine** (see [06](06-multi-machine.md)). Each writes only its own cache files and logs.
- Inside one process: a thread pool for hashing (hashlib releases the GIL), one long-lived ExifTool process (or a small pool of them), one DB writer.

## Technology choices

| Concern | Choice | Notes |
|---|---|---|
| Language | Python 3.12+ | Prototype. Stdlib-heavy so the logic ports cleanly ([D-13](decisions.md#d-13-python-312-prototype-with-minimal-runtime-dependencies)). |
| Metadata | ExifTool, `-stay_open` batch mode | Pinned minimum version ≥ 12.62 (HEIC image-data hashing). Path configured per machine. |
| DB | SQLite via stdlib `sqlite3`, WAL | |
| Hashing | `hashlib.sha256`, 1 MiB chunks | |
| Config | TOML (`tomllib` to read; written by Photo Haven with a tiny emitter) | |
| Report | Static HTML + vanilla JS, no build step | |
| Tests | pytest; Pillow and pillow-heif for fixtures (dev-only) | |
| Packaging | `pyproject.toml`, `src/` layout, console script `photohaven` | |

## Windows specifics

- **Long paths.** Use `\\?\`-prefixed absolute paths internally for all filesystem calls, or require the `LongPathsEnabled` registry setting. `doctor` checks it. Event-named DSLR folders nested a few levels deep can exceed 260 characters.
- **Hidden files.** Cache files start with a dot *and* get `FILE_ATTRIBUTE_HIDDEN` set, since Explorer ignores the dot convention.
- **Placeholders.** `os.stat().st_file_attributes` exposes `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS` (0x400000), `FILE_ATTRIBUTE_RECALL_ON_OPEN` (0x40000), and `FILE_ATTRIBUTE_OFFLINE` (0x1000). Any of these means opening the file may trigger a download. See [04](04-scanner.md#onedrive-placeholders).
- **Case-insensitive, case-preserving names.** Store names as the filesystem returns them; compare with NFC + casefold.
- **Moving across volumes** is a copy-then-delete, not a rename. The quarantine path decides which one applies (see [08](08-apply-quarantine-verify.md#4-the-move-protocol)).
