# 04. Scanner

The scanner turns folders of files into facts: size, mtime, SHA-256, content hash, metadata. It is incremental (an unchanged folder costs a directory listing and a cache read), resumable (Ctrl-C loses at most the folder in progress), and read-only with respect to photos.

## Pipeline

```
for each root:
  verify root marker
  walk folders (iterative, sorted)            ── stage 1: discover
    read every machine's cache file in folder
    stat every entry; classify by extension
    resolve facts from caches (06 §3)         ── stage 2: reuse
    queue files needing hashing
  hash pool: SHA-256 of queued files          ── stage 3: hash
  for each new unique sha256:
    ExifTool: metadata + ImageDataHash        ── stage 4: describe
  write this machine's cache file per folder  ── stage 5: persist
  upsert DB; mark missing files
  link pass (05)                              ── stage 6: relate
```

Stages run as a pipeline per folder batch rather than strictly one after another, so hashing starts while the walk continues. The DB commit unit is one folder: after a crash, every committed folder is complete, and the cache file for that folder is written *before* its DB commit (the cache is the durable fact store; the DB is derived).

## Stage 1: discover

- Iterative walk with `os.scandir`, folders sorted by `path_key` for deterministic order and readable progress.
- **Skip:** the reserved `PhotoHaven-DELETE` folder at the top of each root (moved duplicates, D-10), Photo Haven's own files (`.photohaven-*`), names matching `library.toml [scan] ignore_names`, the project home and quarantine root if they were mistakenly placed inside a root (and `doctor` complains), and reparse points (junctions, symlinks), which are recorded but not followed. That avoids cycles and double counting.
- **Classify** each file by lowercase extension into `image`, `raw`, `video`, `sidecar`, `other`. `other` files are recorded (so a folder's "is it fully redundant" answer accounts for them) but not hashed by default. A folder with a stray `.pdf` is not fully redundant.
- Record folder rows with depth, levels, `is_generic`, `event_guess` (see [05 §7](05-file-relationships.md#7-folder-names-and-event-guesses)), `is_intake`, and `is_protected` from `library.json`.

## OneDrive placeholders

The library is set to *Always keep on this device*, but files can still be dehydrated (by Storage Sense, a reset, or a new machine that hasn't finished downloading), and Karen's library may be online-only. Reading a placeholder triggers a download, which for a whole-library scan could mean hundreds of GB.

- On Windows, check `st_file_attributes` for `FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS` (0x00400000), `FILE_ATTRIBUTE_RECALL_ON_OPEN` (0x00040000), or `FILE_ATTRIBUTE_OFFLINE` (0x00001000).
- If set: don't open the file. Record `state = cloud_only`, `skip = "cloud_only"`, with size and mtime (available without hydration). Reuse cached facts from another machine if size and mtime match (another machine may have hashed it).
- `scan --hydrate` opts in to reading placeholders, with a byte budget (`--hydrate-limit 50GB`).
- `status` and the report show cloud-only counts prominently. A folder with unhashed cloud-only files can never be marked fully redundant.

## Stage 2: reuse cached facts

For each file, choose facts from the folder's cache files (any machine) according to [06 §3](06-multi-machine.md#3-reusing-cached-facts). Files with usable cached facts skip stages 3–4. Everything else is queued.

`scan --rehash` ignores caches and recomputes everything (for audits). `scan --audit N` rehashes a random sample of N cached files and reports any mismatch, which would indicate silent corruption or a caching bug. Running it after every few scans is cheap insurance.

## Stage 3: hash

- Read in 1 MiB chunks, `hashlib.sha256`, thread pool (`hash_threads`, default 4; hashlib releases the GIL). Throughput is bounded by disk, not CPU: SHA-256 with SHA-NI runs at 1–2 GB/s per core.
- Record `size` and `mtime_us` from an `fstat` on the open handle *before* reading, and `stat` again after. If they differ, the file changed during hashing: discard the result and requeue once, then flag `unreadable` with reason `changing`.
- Open files with share modes that allow other readers and writers (Python's default on Windows is fine). A sharing violation (OneDrive mid-sync, Photos app holding a lock) gets a short retry, then `skip = "locked"`.

### Hash scope

The handoff says to hash only files that share a size. That is the right *priority*, but not a complete answer: every file needs a SHA-256 eventually, because

- a new camera-roll upload next month may match an old file that was unique by size today,
- verification and the "is this content already known?" check for intake depend on it, and
- restructure (phase 2) copies by content.

So the scanner works in two passes:

1. **`candidates` pass:** hash only files whose size matches another file's size anywhere in the library (across roots). This gives a complete duplicate report fastest. Files with a unique size *cannot* be exact duplicates.
2. **`all` pass:** hash everything else. Runs right after pass 1 by default (`hash_scope = "all"`), or later with `scan --hash-scope all`.

The report says which pass has completed. A "fully redundant" verdict only needs pass 1 for that folder's files, since a unique-size file is by definition not redundant.

An optional partial-hash prefilter (first and last 64 KiB) for same-size groups is not worth it here: in an untouched library, same-size-but-different JPEG/HEIC files are rare, so almost every same-size group really is duplicates. Size is only a way to choose what to hash first, never evidence of equality: metadata edits and single-byte damage can land on the same size (the M0 duplicate corpus has 5 such sizes in 57 files).

### Time estimate

400 GB is read once for SHA-256. Content hashing reads only unique content a second time (stage 4). On an NVMe SSD at a sustained ~1 GB/s, expect roughly 10–15 minutes of reading for SHA-256 plus ExifTool time. On a SATA SSD (~500 MB/s), roughly 15–25 minutes plus ExifTool. ExifTool metadata extraction runs at several hundred to a few thousand files per minute per process depending on format; with 2 processes and ~150k unique files, budget about an hour. The first full scan is a one-time cost of an hour or two; rescans take minutes.

## Stage 4: describe (ExifTool)

**Once per unique SHA-256, not once per file.** Metadata and the content hash are functions of the bytes, so all instances of a duplicate group share them. ExifTool runs on one representative instance per new `sha256`. On a library heavy with duplicates, this saves a lot of reading ([D-16](decisions.md#d-16-content-hash-and-metadata-computed-once-per-unique-sha-256)).

- Long-lived processes: `exiftool -stay_open True -@ -`, each request ending with `-execute<N>` so responses can be matched up. Pool size `exiftool_processes` (default 2).
- Per request: `-json -n -a -G0:1:4 -struct -api ImageHashType=SHA256 -api WindowsLongPath=1` plus an explicit tag list including `-ImageDataHash -Warning -Error` (pinned in `photohaven.scan.exiftool`, [03 §3.3](03-format-spec.md#33-meta-object)). Keys come back as `family0:family1[:CopyN]:Tag` (`EXIF:ExifIFD:DateTimeOriginal`, `ExifTool:ExifTool:Copy1:Warning`), which gives the family 0 group the spec names and, with `-a` and family 4, every warning; JSON output otherwise keeps only the first ([03 §8.1](03-format-spec.md#81-content-hash), "Damage"). Batch about 50 files per request. Pass paths via the argument file, UTF-8 (`-charset filename=utf8`). On Windows pass plain absolute paths and rely on `WindowsLongPath` (ExifTool 13.61 opened a 330-character path in M0); Python's own file I/O uses the `\\?\` form for long paths.
- Normalize in Python (`photohaven.scan.metadata`): parse dates into §1.4 fields, pick the first available source in priority order, coerce numbers, drop empty strings. `ph-meta/<n>` is bumped whenever these rules change; a later scan MAY re-derive metadata for contents with an older `ph-meta`.
- Sub-seconds come from `Composite:SubSecDateTimeOriginal` / `SubSecCreateDate`, which are text. With `-n`, the bare `SubSecTimeOriginal` tag is a number, so `030` would read as `30`.
- **Date fallbacks are per instance, not per content (M2).** `meta` describes the bytes and is shared by every copy of one SHA-256 (`contents`), but a file name and an mtime belong to one instance. So the scanner never writes `filename` or `mtime` as a `taken_source`; the link and report pass (M2) derives them per file when a content has no embedded date.
- **Filename date fallback** patterns: `IMG_20150704_134512`, `20150704_134512`, `2015-07-04 13.45.12`, `PXL_20150704_134512123`, `Screenshot_2015-07-04-13-45-12`. Used only when no embedded date exists; `taken_source = "filename"`.
- **mtime fallback:** last resort, `taken_source = "mtime"`, flagged low confidence in reports. Copying often resets mtime, so this is the least trustworthy source.
- ExifTool messages are classified per [03 §8.1](03-format-spec.md#81-content-hash): a damaged file gets `meta.damaged = true`, a null `content_sha256`, and a `damaged` flag; other non-minor warnings go to `meta.exiftool_messages`; an ExifTool failure that yields no output at all is an `exiftool_error` flag. None of these stop the scan.
- `content_sha256` is null for types where `ImageDataHash` is not produced. That's fine; it's an enhancement, not an identity.

## Stage 5: persist

1. Build this machine's file records for the folder (including records reused from other machines' caches, so this machine's file is complete on its own).
2. If the canonical serialization of the file records equals what's already in this machine's cache file, don't touch it. This matters: rewriting thousands of unchanged cache files would make OneDrive re-upload all of them on every scan.
3. Otherwise write atomically and set the hidden attribute. On Windows the attribute must be set again after every replace (the new file doesn't inherit it), and a hidden file can't be overwritten in place (`CREATE_ALWAYS` on a hidden file fails), so always write via replace.
4. Upsert `folders`, `files`, `contents`, `content_folder_names`. Mark files in the DB but not on disk as `missing`, unless the quarantine log says they were moved, in which case `quarantined`.
5. Commit.

## First-run read-only mode

`scan --no-folder-cache` (equivalent to `cache_placement = "local_only"` for one run) writes cache files under local state instead of into library folders. The very first pass over the real library then changes nothing at all inside OneDrive. After reviewing the results, a normal scan copies the facts into folder caches without rehashing, because local-only caches are read as another source of cached facts.

## Stage 6: relate

After the walk, the link pass rebuilds duplicate statistics, folder overlap, raw pairs, Live Photo pairs, edit links, and flags. See [05](05-file-relationships.md). It's a set of SQL queries plus a little Python and runs in seconds to a minute at this library's size.

## Progress and output

- One status line: folders done/total, files hashed, GB read, current rate, ETA, errors.
- An end-of-scan summary: new files, changed files, missing, cloud-only, new duplicate groups, reclaimable bytes, and pointers to `photohaven report`.
- A detailed app log in local state `logs/scan-<scan_id>.log`.

## Error handling

No single file can fail a scan. Errors are classified (`unreadable`, `locked`, `changing`, `exiftool_error`, `damaged`, `path_too_long`, `permission`), stored in `flags`, counted in `scans.errors`, and listed in the report. A root that can't be opened, or whose marker doesn't match, fails that root and moves on to the next.
