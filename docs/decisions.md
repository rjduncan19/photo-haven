# Decision log

Short records of design decisions: what was decided, why, and what was rejected. **Source** is *handoff* for decisions made in the original planning conversation, *design* for decisions made while writing these docs (open to revision until M0 closes). Status is **accepted** or **proposed**.

---

### D-01: Data formats are the product

*Source: handoff. Status: accepted.*

The SQLite schema, JSONL records, and XMP fields are documented, versioned, and language-neutral ([03](03-format-spec.md)). Code is replaceable; data isn't. A Rust or C++ rewrite is a new implementation of the same contract, and can adopt an existing library with no migration. Only `photohaven.formats` and `photohaven.db` touch on-disk layouts.

### D-02: Never delete first; removal means quarantine

*Source: handoff. Status: accepted.*

Scan → report → review → apply. Apply moves to a quarantine; only purge deletes, only from quarantine. Rejected: deleting directly into the OneDrive recycle bin (a 30-day net with no verification step, and not under our control).

### D-03: Last-copy invariant on byte-identical content

*Source: design (formalizes the handoff's "verify before delete"). Status: proposed.*

An instance may be quarantined only if a byte-identical instance remains as its keeper. Folder-level "remove" therefore never strands a unique file; those are reported as blocked. Removing near-identical but not byte-identical files (metadata-only differences, re-encodes, bursts) needs a different, explicit, per-file action in a later phase.

### D-04: SHA-256 for file identity

*Source: handoff. Status: accepted.*

Group by size first, then SHA-256. Collisions are not a practical concern. Rejected: MD5/SHA-1 (no meaningful speed gain at disk-bound rates; weaker), BLAKE3 (faster on CPU, but disk is the bottleneck and SHA-256 is universal across tools and languages).

### D-05: Content hash = ExifTool ImageDataHash (SHA-256)

*Source: design. Status: proposed — confirm in the M0 spike.*

`content_sha256` is ExifTool's `ImageDataHash` with `-api ImageHashType=SHA256` (HEIC support since ExifTool 12.62), embedded in curated copies as `XMP-et:OriginalImageHash` / `OriginalImageHashType`. It hashes encoded image data, excluding metadata, so it is exact and reproducible.

Rejected: hashing decoded pixels. JPEG/HEIC decoders differ in output across libraries and versions, which breaks the language-neutral contract, and decoding 400 GB is slow. Rejected: our own segment-stripping parser per format. That reinvents what ExifTool already does, and it would need maintenance for every format.

Spike checks: the hash is stable when XMP/EXIF fields are added or changed (JPEG, HEIC, one raw, MOV); its throughput on the Windows machine; its behavior on truncated or corrupt files.

### D-06: Originals are not modified; metadata goes to the DB and to curated copies

*Source: handoff; revised 2026-11-09 after Rick's review. Status: proposed.*

Dedupe never writes into photo files. The only files Photo Haven writes inside library roots during dedupe are its cache files and the root marker.

**Revision:** Rick is OK with writing **ratings into originals**, as a deliberate, separate **rating step** ([09](09-later-phases.md#rating-step-writing-into-originals)), because a write changes the file's SHA-256 and can upset dedupe. So:

- A content is rated into its original only once it is **settled**: one present instance, not the keeper of any unpurged quarantine item, no pending dedupe decision. Work proceeds incrementally, folder by folder or year by year.
- Each original is written **once per flush**, in a single ExifTool write that carries the rating *and* the two provenance hashes: `XMP-et:OriginalImageHash` (content hash; stays valid through later edits) and `XMP-phv:IngestFileSHA256` (the file's SHA-256 before its first write). The current file hash is never embedded (a file can't contain its own hash).
- The ingest hash lets a later scan recognize a rated original as "the same file" as an unrated copy that turns up elsewhere (for example from Karen's library), even though their SHA-256 values now differ.

The DB remains the source of truth for ratings; curated copies (phase 2) get them as well.

### D-07: The SQLite database lives on local disk, never in a synced folder

*Source: handoff ("separate local SQLite file, not inside the photo tree"), extended in design. Status: accepted.*

SQLite under OneDrive risks corruption (sync mid-transaction) and conflict copies. The DB is a derived index rebuilt from caches plus logs, backed up locally before every apply and purge.

### D-08: Per-folder, per-machine cache files, in-folder by default

*Source: handoff. Status: accepted.*

One hidden `.photohaven-cache.<machine_short>.jsonl` per machine per folder, holding facts only. Travels with folder renames. Each machine writes only its own. Rewritten only when facts change, to avoid sync churn. A `local_only` placement exists for read-only runs and roots without write access ([06 §2](06-multi-machine.md#2-cache-placement)).

### D-09: Append-only per-machine logs in a synced project home

*Source: handoff (append-only decision log), location decided in design. Status: proposed.*

Decision and quarantine logs live in `<project home>/logs/`, one file per machine, so every machine sees every decision without OneDrive ever merging edits. Latest wins per target, ordered by `(at, machine_id, seq)`. The project home is outside every library root.

### D-10: Quarantine location

*Source: handoff; reopened 2026-11-09. Status: **open** pending spike C4.*

The handoff put the quarantine on local disk outside OneDrive, because a quarantine inside OneDrive frees no cloud space until purge. Rick prefers a quarantine **inside OneDrive** (outside every root), which works only if the OneDrive client syncs a local move within the same OneDrive as a server-side move rather than a delete plus re-upload. Reports about cut-and-paste between *separately synced SharePoint libraries* say the client doesn't treat that as a move. Moves within one OneDrive are untested, so spike C4 measures it.

| | Inside OneDrive (`quarantine_mode = "onedrive"`) | Local, outside OneDrive (`"local"`) |
|---|---|---|
| Move cost | Instant rename on the same volume, if OneDrive syncs it as a move | Cross-volume copy + verify + delete, unless on the same volume |
| Cloud space | Freed only at purge | Freed as OneDrive processes the deletions (minus its recycle bin) |
| Visible from other machines | Yes, so any machine can verify, restore, or purge | Only on the machine that holds it |
| Placeholders | Can be quarantined without downloading | Must be downloaded first, or skipped |
| Mass-deletion prompts | None (it's a move) | Possible |
| Stays in OneDrive Photos / People views | Probably, until purged | No |
| Single apply machine (D-17) | Still useful against the sync-lag race, but the race can no longer strand a quarantine on one machine's disk | Required |

Both modes share the batch layout and manifests ([08 §10](08-apply-quarantine-verify.md#10-quarantine-location)). Decide after spike C4.

### D-11: A library has multiple roots; paths are (root_id, relpath)

*Source: design. Status: proposed.*

The handoff assumed a single library root. Rick's Pictures, Karen's Pictures, and future removable media are separate trees mapped to different local paths on different machines. Every path is relative to a root identified by a UUID, and each root carries a marker file so a misconfigured path is caught before scanning.

### D-12: Static HTML report with export (v1)

*Source: handoff. Status: accepted; revisit for v2.*

Static report directory, decisions in `localStorage`, Export to a JSONL file that `decisions import` appends to the log. Detail shards loaded via `<script>` tags to work over `file://`. A localhost `serve` mode is the likely v2 ([07 §1](07-report-and-decisions.md#1-form)).

### D-13: Python 3.12 prototype with minimal runtime dependencies

*Source: handoff (Python first), dependency policy from design. Status: proposed.*

Stdlib for hashing, SQLite, TOML reading, threads, CLI (`argparse`). ExifTool as an external process. Test-only dependencies: pytest, hypothesis, Pillow, pillow-heif. Fewer dependencies means less to port and less to break on Windows.

### D-14: Working name and brand-neutral identifiers

*Source: handoff (name), design (identifiers). Status: proposed.*

"Photo Haven" is a working name; similarly named products exist, so a rebrand is likely before any public release. Renaming the code costs almost nothing, but names embedded in *data* are forever. So: the XMP provenance namespace is `urn:rjduncan19:photo-provenance:1.0` (prefix `phv`), not brand-based. On-disk file names (`.photohaven-cache.*`, `.photohaven-root.json`) do carry the working name; a rebrand would keep reading them and could optionally rename them by migration.

### D-15: The camera roll is protected intake

*Source: handoff ("the camera roll stays an untouched intake"), enforcement from design. Status: proposed.*

Folder and subtree `remove` decisions are ignored inside intake/protected folders. Only explicit file-level decisions can quarantine an instance there (for example, repeated uploads within the camera roll). Intake folders are preferred as keepers.

### D-16: Content hash and metadata computed once per unique SHA-256

*Source: design. Status: proposed.*

Metadata and content hash are functions of the bytes, so the DB stores them per content (`contents`), not per instance (`files`), and ExifTool runs once per new `sha256`. On a duplicate-heavy library this avoids re-reading every duplicate a second time.

### D-17: Single apply machine

*Source: design. Status: proposed.*

Only the machine named `apply_machine_id` in `library.json` may run `apply`, `restore`, and `purge`. Any machine may scan, report, and decide. This removes the sync-lag race where two machines each quarantine a different copy of the same photo ([06 §5](06-multi-machine.md#5-scenarios)).

### D-18: Purge hold period

*Source: design. Status: proposed.*

Default 14 days between apply and the earliest permanent delete, configurable per run. Purge also requires every item in the batch to be verified, and it byte-compares each item at purge time.
