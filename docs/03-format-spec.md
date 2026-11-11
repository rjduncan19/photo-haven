# 03. Format spec (the contract)

**Spec version:** 1. **Status:** binding as of 2026-11-10 (end of milestone M0). Changes follow §1.8: additive changes as notes in §10, breaking changes as a version bump with a migration. One M0 question is still open and could force a change: spike C (OneDrive mtime precision, dotfile sync, quarantine location). Raw files and Apple identifiers are milestone [R1](roadmap.md#r1-live-photos-and-raw); nothing in phase 1 depends on them.

This document defines every persisted format Photo Haven reads or writes. It is language-neutral: a Rust, Go, or C++ implementation that follows it must interoperate with the Python prototype on the same library, and either one must be able to rebuild the database from the files the other wrote.

Key words: **MUST**, **SHOULD**, **MAY** as in RFC 2119.

Machine-checkable versions live in `spec/`: the DDL in `spec/schema.sql` (identical to §9), a JSON Schema per file format in `spec/schemas/`, and one valid example per format in `spec/examples/`. `python spec/check_spec.py` checks them against each other and against this document. The schemas describe exactly what v1 writers emit (unknown fields are rejected there); readers still MUST ignore unknown fields (§1.1). If this doc and `spec/` disagree, `spec/` wins and this doc gets fixed.

---

## 1. General conventions

### 1.1 Encoding and text files

- All text files are UTF-8 without BOM, with LF line endings.
- **JSON Lines (JSONL):** one JSON object per line. The first line of every JSONL file is a `header` record. Every record has a `"record"` field naming its type.
- **Canonical JSON:** writers MUST emit keys in sorted order (by Unicode code point), with separators `,` and `:` (no spaces) and non-ASCII characters unescaped. Two writers holding the same facts therefore produce byte-identical lines, which lets the scanner skip rewriting a cache file that hasn't changed. To make that hold:
  - **Absent, not null.** An optional field with no value is left out, never written as `null`. `null` appears only in fields the spec marks as required-but-nullable (for example a cache record's `sha256` when `skip` is set). Readers treat a missing optional field and `null` the same.
  - **Booleans that default to false** (for example `meta.damaged`) are written only when true.
  - **Numbers:** integers without a decimal point; non-integers in the shortest decimal form that round-trips the IEEE 754 double, without an exponent (`-123.184013888889`, not `-1.23184013888889e2`).
  - `spec/examples/` holds canonical lines, and `spec/check_spec.py` checks them.
- **Readers MUST ignore unknown fields** and MUST preserve them when rewriting a record they don't own (they never rewrite one they don't own, see §4).
- **Append-only logs** (§5, §6): if the last line fails to parse (a crash mid-write), readers MUST ignore it and log a warning. The next writer MUST start on a fresh line.

### 1.2 Event timestamps

Wall-clock event times (when something was hashed, decided, moved) are UTC in exactly this form:

```
YYYY-MM-DDTHH:MM:SS.ffffffZ        e.g. 2026-11-09T22:14:35.904318Z
```

Always six fractional digits, always `Z`. Lexical order equals chronological order.

### 1.3 Filesystem times

File modification and creation times are **integer microseconds since the Unix epoch (UTC)**, in fields ending `_us` (`mtime_us`, `ctime_us`).

*Why not ISO strings:* these are compared for equality, not displayed. *Why microseconds, not nanoseconds:* nanosecond values exceed 2^53 and lose precision in JSON parsers that use doubles (JavaScript, among others). NTFS stores 100 ns ticks, so the value is truncated toward zero.

Equality rule for "has this file changed": see [06 §3](06-multi-machine.md#3-reusing-cached-facts) (tolerance is configurable via `mtime_tolerance_us`).

### 1.4 Capture times

Photo capture time is stored as up to three fields, because most cameras record local time with no time zone:

| Field | Form | Meaning |
|---|---|---|
| `taken_local` | `YYYY-MM-DDTHH:MM:SS[.f…]` | Local wall-clock time as recorded, with 1–6 fractional digits only if the file records sub-seconds (as recorded, not padded). No zone. |
| `taken_offset` | `+HH:MM` / `-HH:MM` or null | UTC offset, if the file records one (iOS does; most older DSLRs don't). |
| `taken_utc` | §1.2 format or null | Only if derivable (offset known, or source is a UTC field such as QuickTime `CreateDate`). |
| `taken_source` | enum | `exif_original`, `exif_digitized`, `quicktime_creationdate`, `quicktime_createdate`, `xmp_datecreated`, `filename`, `mtime`. `filename` and `mtime` are per-instance fallbacks: they never appear in a cache record's `meta` or in `contents` (which describe bytes shared by every copy), only in per-file views from M2 on. |

Year/month organization (phase 2) uses `taken_local`, because a photo taken at 11 pm on December 31 belongs to that December wherever it was taken.

### 1.5 Hashes

- Lowercase hex, no prefix, no separators.
- The field name states the algorithm: `sha256`, `content_sha256`. A future algorithm gets a new field, not a new meaning for an old one.
- `content_sha256` is defined in §8.1 and is always accompanied by `content_hash_impl`.

### 1.6 Paths

- Every library path is a pair **`(root_id, relpath)`**.
- `relpath` uses `/` separators, has no leading or trailing `/`, contains no empty, `.`, or `..` components, and preserves the case and Unicode form the filesystem returned. The root folder itself has `relpath` `""`.
- **`path_key`** = `casefold(NFC(relpath))`. It is used for lookups and for matching decisions to paths, never for display or filesystem calls. (Windows is case-insensitive; macOS-originated exports may arrive NFD.)
- **`path_key` is not unique.** NTFS stores names as given, so `Café.jpg` in NFC and in NFD form can sit side by side in one folder as two files with the same `path_key` (seen in M0, [m0-results](spikes/m0-results.md#duplicate-case-corpus-make_dupe_corpuspy)). Uniqueness is on the raw `(root_id, relpath)`. Where a lookup by `path_key` finds more than one instance, each is flagged `path_key_collision`. A file-level decision still applies only to the instance whose `sha256` matches; a folder-level decision applies to every colliding folder, and the report lists them.
- Quarantine paths are relative to the machine's quarantine root and follow the same rules.
- Absolute local paths appear only in `machine.toml` and the local DB `roots.local_path`, never in synced files.

### 1.7 Identifiers

| ID | Form | Example |
|---|---|---|
| `library_id`, `root_id`, `machine_id` | UUIDv4, lowercase, hyphenated | `3f1c…-…` |
| `machine_short` | First 12 hex digits of `machine_id` with hyphens removed | `3f1c9a0b22de` |
| Log record `id` | UUIDv7, lowercase, hyphenated (time-ordered) | |
| `batch_id` | `YYYYMMDD-HHMMSS-` + 4 random hex digits (UTC) | `20261110-031522-a91f` |
| `plan_id`, `report_id` | UUIDv7 | |

### 1.8 Versioning

- Every JSON/JSONL file's header carries `"schema": <int>`. The DB carries `PRAGMA user_version` and `meta.schema_version`.
- **Additive** changes (new optional fields) don't bump the version. **Breaking** changes (renamed or removed fields, changed meaning) bump it and come with a migration.
- Readers MUST refuse files with a `schema` newer than they understand and say so clearly. They MUST NOT guess.
- Each schema version has a migration in `photohaven.db.migrations` and a note in this document's changelog (§10).

---

## 2. Identity and configuration files

### 2.1 `library.json` (project home)

```json
{
  "schema": 1,
  "library_id": "…uuid…",
  "name": "Duncan family photos",
  "created_at": "2026-11-10T03:15:22.000000Z",
  "apply_machine_id": "…uuid…",
  "roots": [
    {"root_id": "…", "name": "rick-pictures", "kind": "onedrive", "intake_folders": ["Camera Roll"], "protected_folders": ["Camera Roll"]},
    {"root_id": "…", "name": "karen-pictures", "kind": "onedrive", "intake_folders": ["Camera Roll"], "protected_folders": ["Camera Roll"]}
  ]
}
```

Rarely edited. `doctor` detects OneDrive conflict copies of it (`library-<PCNAME>.json`). `apply_machine_id` names the only machine allowed to run `apply`, `restore`, and `purge` ([D-17](decisions.md#d-17-single-apply-machine)). Folder entries in `intake_folders` / `protected_folders` are relpaths; they cover the whole subtree.

### 2.2 Root marker: `<root>/.photohaven-root.json`

```json
{"schema": 1, "library_id": "…", "root_id": "…", "name": "rick-pictures", "created_at": "…"}
```

Written once by `init` / `root add`. The scanner refuses to scan a configured path whose marker is missing or names a different `root_id`, unless run with `--adopt-root`.

### 2.3 `machine.json` (local state, never synced)

```json
{"schema": 1, "machine_id": "…uuid…", "name": "tomahawk", "created_at": "…"}
```

Created on first run. **Never copy it to another machine.** A cloned disk image that keeps it would make two machines write the same cache filenames. `doctor` warns if the hostname changed since `created_at`.

### 2.4 `machines/<machine_id>.json` (project home)

```json
{"schema": 1, "machine_id": "…", "machine_short": "3f1c9a0b22de", "name": "tomahawk", "hostname": "TOMAHAWK", "last_seen": "…", "tool": "photohaven/0.1.0"}
```

Each machine writes only its own file. Used for display names in reports and to detect `machine_short` clashes. `machine retire` adds `"retired_at": "<timestamp>"` ([06 §5](06-multi-machine.md#5-scenarios)).

### 2.5 `library.toml` (project home, shared settings)

```toml
schema = 1

[scan]
mtime_tolerance_us = 1_000_000        # see 06 §3
hash_scope = "all"                    # "candidates" | "all"
ignore_names = ["desktop.ini", "Thumbs.db", ".DS_Store", "*.tmp", "~$*"]

[media]                               # lowercase extensions, no dot
image = ["jpg", "jpeg", "heic", "heif", "png", "gif", "tif", "tiff", "webp", "bmp"]
raw = ["cr2", "cr3", "crw", "nef", "nrw", "arw", "srf", "sr2", "dng", "orf", "rw2", "raf", "pef", "srw", "x3f"]
video = ["mov", "mp4", "m4v", "avi", "3gp", "mts", "m2ts", "mpg"]
sidecar = ["xmp", "aae", "thm"]

[folders]
generic_names = ["Camera Roll", "Backup", "Backups", "Imports", "Import", "Pictures", "Photos", "DCIM",
                 "New folder", "Untitled", "Export", "Exports", "Desktop", "Saved Pictures", "Screenshots",
                 "Copy", "Misc", "Unsorted", "OneDrive"]
generic_patterns = ['^\d{3}[A-Z_]{5}$', '^\d{4}$', '^\d{2}$', '^(19|20)\d{2}[-_ ]?\d{2}$']   # 100APPLE, 2015, 07, 2015-07
```

### 2.6 `machine.toml` (local state)

```toml
schema = 1
name = "tomahawk"
project_home = 'C:\Users\rick\OneDrive\PhotoHaven-data'
quarantine_root = 'D:\PhotoHavenQuarantine'
exiftool = 'C:\Tools\exiftool\exiftool.exe'
hash_threads = 4
exiftool_processes = 2
cache_placement = "in_folder"          # "in_folder" | "local_only" (see 06 §2)

[roots]
"…root_id…" = 'C:\Users\rick\OneDrive\Pictures'
```

---

## 3. Folder cache file

**Location:** `<folder>/.photohaven-cache.<machine_short>.jsonl`, one per machine per folder, with the Windows hidden attribute set. With `cache_placement = "local_only"`, the same file is written under local state at `caches/<root_id>/<relpath>/.photohaven-cache.<machine_short>.jsonl` instead (used for a read-only first pass, see [04](04-scanner.md#first-run-read-only-mode)).

**Content:** facts about the files *directly* in that folder (not subfolders). Never decisions.

**Write rule:** the file is rewritten whole by its owning machine with an atomic replace (write `…jsonl.tmp`, flush, `os.replace`). It is only rewritten if the sorted set of `file` records differs from what's on disk, ignoring the header. A folder with no media files gets no cache file.

### 3.1 Header

```json
{"record":"header","schema":1,"kind":"folder-cache","library_id":"…","root_id":"…","folder":"2015/Hawaii","machine_id":"…","written_at":"…","tool":"photohaven/0.1.0"}
```

`folder` is informational: it records where the folder was when written. Cache files travel with a folder when it's renamed or moved, and entries stay valid because they're keyed by file name within the folder.

### 3.2 File record

One per file in the folder that has either a `sha256` or a `skip` reason, sorted by `name` (by `casefold(NFC(name))`, ties broken by raw name, both compared by code point). A file still waiting for the `all` hash pass ([04](04-scanner.md#hash-scope)), or an `other` file that is never hashed, has no record yet: there are no facts to share about it, and the walk records it in the DB anyway.

```json
{
  "record": "file",
  "name": "IMG_1234.HEIC",
  "size": 2381337,
  "mtime_us": 1436045112000000,
  "ctime_us": 1436045112000000,
  "media": "image",
  "sha256": "9f86d0…",
  "content_sha256": "1b4f0e…",
  "content_hash_impl": "exiftool/13.10/ImageDataHash",
  "meta": { "taken_local": "2015-07-04T13:45:12.123", "taken_offset": "-10:00", "...": "see 3.3" },
  "meta_impl": "exiftool/13.10;ph-meta/1",
  "observed_at": "2026-11-10T03:15:22.000000Z"
}
```

(Shown pretty-printed; on disk each record is one canonical line. A skipped file has `"sha256": null`, a `skip` value, and no hash or metadata fields.)

| Field | Notes |
|---|---|
| `name` | File name exactly as the filesystem returned it. |
| `size`, `mtime_us`, `ctime_us` | From `stat` at the time `sha256` was computed. |
| `media` | `image`, `raw`, `video`, `sidecar`, `other` (by extension, per `library.toml`). |
| `sha256` | Full-file SHA-256. Required; `null` exactly when `skip` is set. |
| `content_sha256` | §8.1. Null for non-image types ExifTool can't hash, if not yet computed, or if the file is damaged (`meta.damaged`). |
| `meta` | §3.3. Facts derived deterministically from the bytes. |
| `meta_impl` | Versions of ExifTool and of Photo Haven's normalization rules. A reader MAY recompute metadata when `ph-meta` is older than its own. |
| `observed_at` | When this machine established these facts. **Provenance, not a fact**: excluded from the "did facts change" comparison. |
| `skip` | Absent, or why the file wasn't hashed: `cloud_only`, `unreadable`, `locked`, `too_large`. |

**Refinement:** the handoff said two machines scanning the same folder "should write identical entries." They write identical *facts*; `observed_at`, `machine_id`, and `written_at` differ by design.

### 3.3 `meta` object

All fields optional; absent and `null` mean the same thing. Values are normalized (numbers are numbers, not ExifTool display strings; ExifTool run with `-n`).

| Field | Type | Source (ExifTool tag, in priority order) |
|---|---|---|
| `taken_local`, `taken_offset`, `taken_utc`, `taken_source` | §1.4 | `EXIF:DateTimeOriginal` + `SubSecTimeOriginal` + `OffsetTimeOriginal`; `EXIF:CreateDate`; `QuickTime:CreationDate` (has zone); `QuickTime:CreateDate` (UTC: `taken_local` then holds the UTC wall clock, with no `taken_offset`); `XMP:DateCreated` |
| `make`, `model`, `lens` | string | `EXIF:Make`, `EXIF:Model`, `EXIF:LensModel` / `Composite:LensID` |
| `width`, `height` | int | `Composite:ImageSize` (pre-rotation pixel dimensions) |
| `orientation` | int 1–8 | `EXIF:Orientation` |
| `gps_lat`, `gps_lon`, `gps_alt` | float (degrees, metres) | `Composite:GPSLatitude`, `Composite:GPSLongitude`, `Composite:GPSAltitude` |
| `duration_ms` | int | `QuickTime:Duration` (video) |
| `mime` | string | `File:MIMEType` |
| `apple_content_id` | string (as recorded) | `MakerNotes:ContentIdentifier` (HEIC/JPEG), `Keys:ContentIdentifier` / `QuickTime:ContentIdentifier` (MOV). *Unconfirmed; see note.* |
| `apple_burst_id` | string | `MakerNotes:BurstUUID`. *Unconfirmed; see note.* |
| `xmp_original_image_hash` | hex | `XMP-et:OriginalImageHash` if present and `XMP-et:OriginalImageHashType` is `SHA256` |
| `xmp_ingest_sha256` | hex | `XMP-phv:IngestFileSHA256` if present (§8.2) |
| `damaged` | bool | True if ExifTool reported damage while reading the file (§8.1, "Damage"). Absent means false. |
| `exiftool_messages` | array of strings | Every ExifTool `Error` and every `Warning` not tagged `[minor]`, in the order reported. Absent if none. |

Tag sources are written as *family 0 group : tag* (`MakerNotes`, `QuickTime`, `EXIF`, `XMP`). ExifTool's `-G1` output names the family 1 group instead (for example `Apple:ContentIdentifier` for `MakerNotes:ContentIdentifier`), and the implementation maps between them. The exact tag list and fallbacks are pinned in code (`photohaven.scan.metadata`) and versioned by `ph-meta/<n>`.

*Apple identifiers:* in M0 none of 189 OneDrive-uploaded iPhone 17 HEICs carried a ContentIdentifier, and no file carried a BurstUUID. The fields are reserved and the scanner records them when present, but nothing in phase 1 depends on them. Confirming the tags is roadmap milestone [R1](roadmap.md#r1-live-photos-and-raw).

---

## 4. Ownership rules

| File | Written by | Others may |
|---|---|---|
| `.photohaven-cache.<m>.jsonl` | machine `m` only | read |
| `logs/decisions.<m>.jsonl` | machine `m` only, append only | read |
| `logs/quarantine.<m>.jsonl` | machine `m` only, append only | read |
| `machines/<id>.json` | that machine only | read |
| `library.json`, `library.toml` | any machine, rarely, by explicit command | read |
| Root marker | `init` / `root add` only | read |

No synced file is ever edited by more than one machine. That is the entire strategy for avoiding OneDrive conflicts.

---

## 5. Decision log

**Location:** `<project home>/logs/decisions.<machine_short>.jsonl`. Append only.

### 5.1 Header

```json
{"record":"header","schema":1,"kind":"decision-log","library_id":"…","machine_id":"…","created_at":"…"}
```

### 5.2 Decision record

```json
{
  "record": "decision",
  "id": "…uuidv7…",
  "at": "2026-11-12T05:01:44.120000Z",
  "machine_id": "…",
  "seq": 42,
  "phase": "dedupe",
  "target": {"kind": "folder", "root_id": "…", "path": "Backup 2019", "scope": "subtree"},
  "action": "remove",
  "origin": "report",
  "report_id": "…",
  "note": null
}
```

| Field | Values |
|---|---|
| `seq` | Per-machine counter, strictly increasing within one log. |
| `phase` | `dedupe` in v1. Later phases add their own (`cull`, `restructure`, …) with their own action vocabularies. |
| `target.kind` | `folder` or `file`. |
| `target.scope` | Folders only: `folder` (files directly in it) or `subtree` (it and everything below). |
| `target.sha256`, `target.size` | Files only, **required**: the content the decision was made about. If the file at that path now has different bytes, the decision does not apply to it. |
| `action` | `keep`, `remove`, `undecided`. `undecided` withdraws earlier decisions on the same target. |
| `origin` | `report`, `cli`, `import`. |

Resolution (which decision is effective for an instance) is defined in [07 §5](07-report-and-decisions.md#5-resolving-effective-decisions). Ordering across machines is by `(at, machine_id, seq)`.

### 5.3 Decisions export file (from the report)

The report's Export button produces `decisions-<report_id>.jsonl`: a header with `"kind":"decision-export"`, `library_id`, `report_id`, `scan_id`, and `generated_at`, then `decision` records **without** `id`, `machine_id`, or `seq`. `decisions import` refuses an export whose `library_id` is not this library's. `decisions import` assigns those, sets `origin` to `import`, and appends to the importing machine's log. Re-importing the same export is idempotent: a record whose `(report_id, target, action)` matches an existing record from the same report is skipped.

---

## 6. Quarantine log and batch manifest

### 6.1 Quarantine log (synced)

**Location:** `<project home>/logs/quarantine.<machine_short>.jsonl`. Append only. Lets every machine know which instances left the library on purpose, and where they are.

Header:

```json
{"record":"header","schema":1,"kind":"quarantine-log","library_id":"…","machine_id":"…","created_at":"…"}
```

Event record:

```json
{
  "record": "quarantine",
  "id": "…uuidv7…", "at": "…", "machine_id": "…", "seq": 7,
  "batch_id": "20261110-031522-a91f",
  "event": "moved",
  "root_id": "…", "path": "Backup 2019/DCIM/IMG_1234.HEIC",
  "sha256": "…", "size": 2381337,
  "keeper": {"root_id": "…", "path": "Camera Roll/2015/07/IMG_1234.HEIC"},
  "qpath": "20261110-031522-a91f/files/<root_id>/Backup 2019/DCIM/IMG_1234.HEIC",
  "detail": null
}
```

`event` ∈ `moved`, `verified`, `verify_failed`, `restored`, `purge_refused`, `purged`. `detail` holds a short reason string for failures.

### 6.2 Batch manifest (in quarantine, local)

**Location:** `<quarantine_root>/<batch_id>/manifest.jsonl`. Header (`"kind":"quarantine-manifest"`, `batch_id`, `library_id`, `machine_id`, `plan_id`, `created_at`) followed by one `item` record per moved file with the same fields as a `moved` event. Written incrementally as moves complete.

The manifest makes a quarantine batch **self-describing**: `restore` works from the manifest alone even if the project home and DB are both lost.

### 6.3 Quarantine layout

```
<quarantine_root>/
  <batch_id>/
    manifest.jsonl
    files/<root_id>/<relpath of the original>
```

---

## 7. Reports

Report output is a disposable, regenerable artifact and **not** part of the contract, except for the export file in §5.3. It lives under local state `reports/<report_id>/` by default.

---

## 8. Embedded metadata (XMP)

Dedupe never writes into originals. These fields are written into originals only by the gated **rating step**, and into **curated copies** at export (phase 2+) ([D-06](decisions.md#d-06-originals-are-not-modified-metadata-goes-to-the-db-and-to-curated-copies), [09](09-later-phases.md#rating-step-writing-into-originals)). Readers recognize them in any file.

### 8.1 Content hash

`content_sha256` is defined as **the value of ExifTool's `ImageDataHash` tag computed with `-api ImageHashType=SHA256`**. ExifTool hashes the image (or audio/video) data alone, excluding metadata, for JPEG, TIFF-based raws, HEIC (ExifTool ≥ 12.62), PNG, MOV/MP4, and other formats. Because it hashes the *encoded* data rather than decoded pixels, it is exact and reproducible: no decoder differences between libraries or versions.

- **Embedded as:** `XMP-et:OriginalImageHash` = hex value, `XMP-et:OriginalImageHashType` = `SHA256`. These are ExifTool's own XMP tags, created for exactly this purpose, so no custom namespace is needed for it.
- **Implementation tag:** `content_hash_impl` = `exiftool/<version>/ImageDataHash`. If a future ExifTool changes what it hashes for some format, `content_hash_impl` tells us which values are comparable. A reimplementation that computes the same bytes-in-scope MUST report its own impl string and MUST pass the conformance fixtures (`spec/fixtures/content-hash/`).
- **Limits:** re-encoding (export at a different quality, HEIC→JPEG conversion) changes the content hash. Those are near-duplicates, found by perceptual methods in phase 4.
- **Equal content is not the same photo.** Two different exposures can have identical image data (M0 found two blank frames shot four days apart). Content equivalence is reported, never acted on by itself.

**Damage.** ExifTool still returns an `ImageDataHash` for most damaged files: in M0, 93 of 93 truncated stills and 7 of 11 truncated videos got a (different) hash. It reports the damage only as a warning. So:

- The implementation MUST collect *every* ExifTool `Warning` and `Error` for the file. With JSON output, a repeated tag is kept only once unless copies are numbered, so the request MUST use `-a` together with group family 4 (for example `-a -G1:4`, which yields `ExifTool:Warning`, `ExifTool:Copy1:Warning`, …). M0 found a `[minor]` warning hiding `JPEG format error` when only the first warning was read.
- A file is **damaged** if ExifTool reports any `Error`, or any `Warning` (not tagged `[minor]`) that matches the damage patterns, case-insensitive: `format error`, `error reading`, `truncated`, `corrupt`, `unexpected end`, `bad … header`/`bad … data`, `missing … data`. The pattern list is versioned with `ph-meta`.
- Other warnings not tagged `[minor]` (for example `IPTCDigest is not current`) are recorded in `meta.exiftool_messages` but don't make the file damaged.
- For a damaged file, `content_sha256` and `content_hash_impl` MUST be null, `meta.damaged` is true, and the instance gets a `damaged` flag. Its `sha256` is unaffected: exact-duplicate grouping still works.
- Damage detection is best effort. A single flipped byte in JPEG entropy data raises no warning at all. Only SHA-256 and the byte-for-byte compare at purge are guarantees.

**Refinement:** the handoff said "hash of image data excluding metadata" without choosing how. Decoded-pixel hashing was rejected because JPEG and HEIC decoders don't produce bit-identical output across libraries, which would break the language-neutral contract ([D-05](decisions.md#d-05-content-hash--exiftool-imagedatahash-sha-256)).

### 8.2 Provenance namespace

Custom namespace for fields ExifTool doesn't already define:

- **Prefix:** `phv`
- **URI:** `urn:rjduncan19:photo-provenance:1.0` (brand-neutral on purpose; see [D-14](decisions.md#d-14-working-name-and-brand-neutral-identifiers))

| Field | Type | Meaning |
|---|---|---|
| `phv:IngestFileSHA256` | hex | Full-file SHA-256 of the original at ingest. Expected to differ from the curated copy's own file hash. |
| `phv:IngestedAt` | §1.2 | When the original was first seen. |
| `phv:LibraryID` | UUID | Library the copy was exported from. |
| `phv:SchemaVersion` | int | This namespace's version. |

Written through an ExifTool config file (`spec/exiftool/photohaven.config`) that defines the namespace as user-defined tags.

**Never embed the current full-file hash.** A file cannot contain its own hash; the current value lives in the DB and caches and is recomputed to verify.

### 8.3 Ratings, tags, people (phase 2+; listed so names are reserved)

Standard fields only: `XMP-xmp:Rating`, `XMP-dc:Subject`, `XMP-lr:HierarchicalSubject`, `XMP-iptcExt:PersonInImage`, MWG face regions (`XMP-mwg-rs:RegionInfo`). The DB is the source of truth; these are written at export.

HEIC and some raw formats are less reliable targets than JPEG. Export MUST test-write on a temp copy and fall back to a sidecar `<name>.xmp` when the write fails or ExifTool reports the format as unwritable.

---

## 9. SQLite database

**Location:** local state `photohaven.db`. WAL mode, `foreign_keys=ON`. Derived and rebuildable (see [06 §6](06-multi-machine.md#6-rebuilding-the-database)), except for operational tables noted below, whose source of truth is the quarantine log and manifests.

**Refinement:** the handoff listed every field "per file." The schema splits **instances** (`files`: a path, a size, an mtime) from **contents** (`contents`: one row per unique SHA-256 holding metadata and content hash), because metadata is a property of the bytes. Duplicates then share one metadata row automatically, and ExifTool runs once per unique content ([D-16](decisions.md#d-16-content-hash-and-metadata-computed-once-per-unique-sha-256)). A view `file_view` presents the flat "per file" picture.

```sql
PRAGMA user_version = 1;

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL                -- schema_version, library_id, created_at, last_rebuild_at
);

CREATE TABLE machines (
  machine_id    TEXT PRIMARY KEY,
  machine_short TEXT NOT NULL UNIQUE,
  name          TEXT,
  last_seen     TEXT
);

CREATE TABLE roots (
  root_id     TEXT PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  kind        TEXT NOT NULL,          -- onedrive | local | removable
  local_path  TEXT                    -- this machine's mapping; NULL if unmapped here
);

CREATE TABLE scans (
  scan_id        INTEGER PRIMARY KEY,
  machine_id     TEXT NOT NULL,
  started_at     TEXT NOT NULL,
  finished_at    TEXT,
  status         TEXT NOT NULL,       -- running | completed | interrupted | failed
  options_json   TEXT,
  folders_seen   INTEGER DEFAULT 0,
  files_seen     INTEGER DEFAULT 0,
  files_hashed   INTEGER DEFAULT 0,
  bytes_hashed   INTEGER DEFAULT 0,
  errors         INTEGER DEFAULT 0
);

CREATE TABLE folders (
  folder_id     INTEGER PRIMARY KEY,
  root_id       TEXT NOT NULL REFERENCES roots(root_id),
  relpath       TEXT NOT NULL,
  path_key      TEXT NOT NULL,
  parent_id     INTEGER REFERENCES folders(folder_id),
  depth         INTEGER NOT NULL,      -- root = 0
  name          TEXT NOT NULL,
  event_guess   TEXT,                  -- cleaned name, NULL if generic (05 §7)
  is_generic    INTEGER NOT NULL DEFAULT 0,
  is_intake     INTEGER NOT NULL DEFAULT 0,
  is_protected  INTEGER NOT NULL DEFAULT 0,
  source        TEXT,                  -- camera_roll | dslr_import | backup | other | <root name>
  present       INTEGER NOT NULL DEFAULT 1,
  last_scan_id  INTEGER REFERENCES scans(scan_id),
  UNIQUE (root_id, relpath)            -- raw path; path_key can collide (§1.6)
);
CREATE INDEX folders_path_key ON folders(root_id, path_key);

-- Every level of the folder path, so "anything under a folder named Hawaii" is one indexed query.
CREATE TABLE folder_levels (
  folder_id  INTEGER NOT NULL REFERENCES folders(folder_id),
  level      INTEGER NOT NULL,         -- 1 = top-level folder under the root
  name       TEXT NOT NULL,
  name_key   TEXT NOT NULL,            -- casefold(NFC(name))
  PRIMARY KEY (folder_id, level)
);
CREATE INDEX folder_levels_name ON folder_levels(name_key);

CREATE TABLE contents (
  sha256             TEXT PRIMARY KEY,
  size               INTEGER NOT NULL,
  media              TEXT NOT NULL,
  content_sha256     TEXT,
  content_hash_impl  TEXT,
  mime               TEXT,
  taken_local        TEXT,
  taken_offset       TEXT,
  taken_utc          TEXT,
  taken_source       TEXT,
  make               TEXT,
  model              TEXT,
  lens               TEXT,
  width              INTEGER,
  height             INTEGER,
  orientation        INTEGER,
  gps_lat            REAL,
  gps_lon            REAL,
  gps_alt            REAL,
  duration_ms        INTEGER,
  apple_content_id   TEXT,
  apple_burst_id     TEXT,
  xmp_original_image_hash TEXT,
  xmp_ingest_sha256  TEXT,
  damaged            INTEGER NOT NULL DEFAULT 0,   -- §8.1 "Damage"; content_sha256 is NULL when 1
  exiftool_messages  TEXT,                         -- JSON array of strings, NULL if none
  meta_impl          TEXT,
  first_seen         TEXT NOT NULL
);
CREATE INDEX contents_content_hash ON contents(content_sha256);
CREATE INDEX contents_taken ON contents(taken_local);
CREATE INDEX contents_apple_cid ON contents(apple_content_id);
CREATE INDEX contents_apple_burst ON contents(apple_burst_id);

CREATE TABLE files (
  file_id       INTEGER PRIMARY KEY,
  root_id       TEXT NOT NULL REFERENCES roots(root_id),
  folder_id     INTEGER NOT NULL REFERENCES folders(folder_id),
  relpath       TEXT NOT NULL,
  path_key      TEXT NOT NULL,
  name          TEXT NOT NULL,
  stem_key      TEXT NOT NULL,         -- casefold(NFC(name without last extension))
  ext           TEXT NOT NULL,         -- lowercase, no dot; '' if none
  media         TEXT NOT NULL,
  size          INTEGER NOT NULL,
  mtime_us      INTEGER NOT NULL,
  ctime_us      INTEGER,
  sha256        TEXT REFERENCES contents(sha256),   -- NULL until hashed
  hashed_at     TEXT,
  hashed_by     TEXT,                  -- machine_id whose facts were used
  state         TEXT NOT NULL,         -- present | cloud_only | unreadable | missing | quarantined | purged
  skip_reason   TEXT,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  last_scan_id  INTEGER REFERENCES scans(scan_id),
  UNIQUE (root_id, relpath)            -- raw path; path_key can collide (§1.6)
);
CREATE INDEX files_path_key ON files(root_id, path_key);
CREATE INDEX files_sha256 ON files(sha256);
CREATE INDEX files_size ON files(size);
CREATE INDEX files_folder ON files(folder_id);
CREATE INDEX files_stem ON files(folder_id, stem_key);

-- Every folder a content has ever lived in, kept even after the instance is quarantined or purged,
-- so the keeper inherits the most descriptive event name.
CREATE TABLE content_folder_names (
  sha256        TEXT NOT NULL REFERENCES contents(sha256),
  root_id       TEXT NOT NULL,
  folder_relpath TEXT NOT NULL,
  folder_name   TEXT NOT NULL,
  event_guess   TEXT,
  first_seen    TEXT NOT NULL,
  PRIMARY KEY (sha256, root_id, folder_relpath)
);

-- Relationships other than exact duplicates (exact duplicate group = shared sha256).
CREATE TABLE links (
  link_id     INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL,           -- raw_pair | live_photo | edit | content_equiv | burst
  method      TEXT NOT NULL,           -- content_id | name_time | name | apple_burst | time_cluster | content_hash
  confidence  TEXT NOT NULL,           -- high | medium | low
  scan_id     INTEGER REFERENCES scans(scan_id),
  detail_json TEXT
);
CREATE TABLE link_members (
  link_id  INTEGER NOT NULL REFERENCES links(link_id) ON DELETE CASCADE,
  file_id  INTEGER NOT NULL REFERENCES files(file_id),
  role     TEXT NOT NULL,              -- primary | raw | still | video | original | edited | aae | member
  PRIMARY KEY (link_id, file_id)
);
CREATE INDEX link_members_file ON link_members(file_id);

-- Flags for orphans and anomalies (orphan Live Photo video, unreadable file, ...).
CREATE TABLE flags (
  file_id  INTEGER NOT NULL REFERENCES files(file_id),
  flag     TEXT NOT NULL,              -- damaged | exiftool_error | path_key_collision | cache_conflict | unreadable | zero_bytes | orphan_live_video | orphan_live_still | ...
  detail   TEXT,
  scan_id  INTEGER REFERENCES scans(scan_id),
  PRIMARY KEY (file_id, flag)
);

-- Materialized from all machines' decision logs.
CREATE TABLE decisions (
  id          TEXT PRIMARY KEY,
  at          TEXT NOT NULL,
  machine_id  TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  phase       TEXT NOT NULL,
  target_kind TEXT NOT NULL,           -- folder | file
  root_id     TEXT NOT NULL,
  path        TEXT NOT NULL,
  path_key    TEXT NOT NULL,
  scope       TEXT,                    -- folder | subtree (folders only)
  sha256      TEXT,                    -- files only
  size        INTEGER,
  action      TEXT NOT NULL,
  origin      TEXT NOT NULL,
  report_id   TEXT,
  note        TEXT
);
CREATE INDEX decisions_target ON decisions(phase, root_id, path_key);

-- Operational tables. Source of truth: quarantine log + manifests (rebuildable from them).
CREATE TABLE plans (
  plan_id        TEXT PRIMARY KEY,
  created_at     TEXT NOT NULL,
  scan_id        INTEGER REFERENCES scans(scan_id),
  decisions_digest TEXT NOT NULL,      -- sha256 of the sorted effective-decision set used
  summary_json   TEXT NOT NULL
);
CREATE TABLE plan_items (
  plan_id        TEXT NOT NULL REFERENCES plans(plan_id),
  seq            INTEGER NOT NULL,
  file_id        INTEGER NOT NULL REFERENCES files(file_id),
  sha256         TEXT NOT NULL,
  size           INTEGER NOT NULL,
  mtime_us       INTEGER NOT NULL,
  keeper_file_id INTEGER NOT NULL REFERENCES files(file_id),
  PRIMARY KEY (plan_id, seq)
);
CREATE TABLE batches (
  batch_id        TEXT PRIMARY KEY,
  plan_id         TEXT REFERENCES plans(plan_id),
  machine_id      TEXT NOT NULL,
  quarantine_root TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  state           TEXT NOT NULL        -- applying | applied | verified | partially_purged | purged | restored
);
CREATE TABLE quarantine_items (
  batch_id        TEXT NOT NULL REFERENCES batches(batch_id),
  root_id         TEXT NOT NULL,
  relpath         TEXT NOT NULL,
  sha256          TEXT NOT NULL,
  size            INTEGER NOT NULL,
  keeper_root_id  TEXT NOT NULL,
  keeper_relpath  TEXT NOT NULL,
  qpath           TEXT NOT NULL,
  state           TEXT NOT NULL,       -- moving | moved | verified | verify_failed | restored | purge_refused | purged
  moved_at        TEXT,
  verified_at     TEXT,
  purged_at       TEXT,
  last_error      TEXT,
  PRIMARY KEY (batch_id, root_id, relpath)
);

-- Flat "per file" view matching the handoff's field list.
CREATE VIEW file_view AS
SELECT f.*, c.content_sha256, c.taken_local, c.taken_offset, c.make, c.model, c.width, c.height,
       c.orientation, c.gps_lat, c.gps_lon, c.apple_content_id, c.apple_burst_id,
       fo.relpath AS folder_relpath, fo.event_guess, fo.is_generic, fo.source
FROM files f
JOIN folders fo ON fo.folder_id = f.folder_id
LEFT JOIN contents c ON c.sha256 = f.sha256;

CREATE VIEW dup_groups AS
SELECT sha256, MIN(size) AS size, COUNT(*) AS instances, (COUNT(*) - 1) * MIN(size) AS reclaimable
FROM files
WHERE state = 'present' AND sha256 IS NOT NULL
GROUP BY sha256
HAVING COUNT(*) > 1;
```

**Not in v1** (reserved for later phases, added by migration): `contents.phash`, `contents.blur_score`, `contents.exposure_score`, `contents.aesthetic_score`, face tables, `events` and `event_members`, `event_name_suggestions(confidence, model, confirmed)`.

---

## 10. Changelog

| Spec version | Date | Change |
|---|---|---|
| 1 (draft) | 2026-11-09 | Initial draft from the planning handoff. |
| 1 | 2026-11-10 | Binding. From the M0 spikes: damage rules and `meta.damaged` / `meta.exiftool_messages` (§3.3, §8.1; `contents.damaged`, `contents.exiftool_messages`); `path_key` is not unique, so uniqueness moves to raw `(root_id, relpath)` (§1.6, §9); family-0 tag groups and unconfirmed Apple IDs (§3.3). Gaps closed while writing `spec/`: canonical rules for nulls, booleans and numbers (§1.1); cache records only for hashed or skipped files (§3.2); quarantine log header (§6.1); `library_id` in the export header (§5.3); `retired_at` in the machine registry (§2.4); fractional digits in `taken_local` (§1.4). |
| 1 | 2026-11-10 | Clarifications from M1, no format change: `filename`/`mtime` capture-time sources are per instance and never appear in `meta` (§1.4, §3.3); `QuickTime:CreateDate` fills `taken_local` with UTC wall-clock time and no offset (§3.3). |
