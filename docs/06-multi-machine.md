# 06. Multi-machine design

The same OneDrive-synced library may be scanned from the desktop (tomahawk), the laptop (txuleta), and later Karen's machine. Size isn't the problem: a few hundred thousand files is tens to hundreds of MB of SQLite. The goals are:

1. **No OneDrive conflicts.** OneDrive never sees two machines editing the same file.
2. **Convergence.** Machines scanning the same files arrive at the same facts.
3. **No wasted work.** A machine reuses facts another machine already computed, when they're provably still valid.
4. **Resilience.** Losing the DB, or a whole machine, loses no decisions and requires no full rehash.
5. **Safety.** Lagging sync between machines can never cause both copies of a photo to be removed.

## 1. What lives where

| Data | Location | Per machine? | Synced? | Role |
|---|---|---|---|---|
| Facts about files | Folder cache files | Yes, one file per machine per folder | Yes | Durable fact store |
| Decisions | `logs/decisions.<m>.jsonl` | Yes | Yes | Durable decision store |
| Quarantine events | `logs/quarantine.<m>.jsonl` + batch manifests | Yes | Log yes, manifests no | Durable record of what left the library |
| DB | Local state | Yes | No | Derived index, rebuildable |

The rule that makes this work: **every synced file has exactly one writer** ([03 §4](03-format-spec.md#4-ownership-rules)).

## 2. Cache placement

**Default: in-folder** (`.photohaven-cache.<machine_short>.jsonl` beside the photos). Why:

- The cache **travels with the folder.** Rename `2015 trip` to `2015-07 Hawaii` on any machine, and the entries remain valid, because they're keyed by file name within the folder. A central cache keyed by path would go stale on every rename.
- A rescan of an unchanged folder needs one small read, not a lookup across a large central file.
- Another machine gets the facts by normal OneDrive sync, with no extra mechanism.

Costs, accepted: one small hidden file per machine per media folder (a few thousand files at most), visible in OneDrive web and mobile views; and when a folder is copied (not moved) elsewhere, its cache file is copied too. That's harmless: copied entries are only reused if size and mtime match the copies, which they will if the copy preserved timestamps, and the facts are then correct anyway, since they describe identical bytes.

**Alternative: `local_only`.** Cache files go under local state, mirroring the tree. Used for the read-only first run ([04](04-scanner.md#first-run-read-only-mode)) and for roots where the machine has no write access (a shared folder from another account). Local-only caches are always read as an extra source alongside in-folder ones.

## 3. Reusing cached facts

For a file on disk with current `size` *S* and `mtime_us` *M*, the scanner collects every cache record in that folder, from every machine's cache file (in-folder and local-only), whose `name` equals the file's name exactly. If there are none, it falls back to records with the same name key (`casefold(NFC(name))`), but only when exactly one file on disk in that folder has that key; with an NFC/NFD or case collision ([03 §1.6](03-format-spec.md#16-paths)) there is no fallback. A record is **usable** if:

- `size == S`, **and**
- `|mtime_us − M| ≤ mtime_tolerance_us`, **and**
- `sha256` is not null.

Then:

1. No usable records → hash the file.
2. Usable records that all agree on `sha256` → use the facts from the one with the newest `observed_at` (it has the newest `meta_impl`). No hashing.
3. Usable records that **disagree** on `sha256` → trust none, hash the file, and flag `cache_conflict`. This should never happen; if it does, it points to a bug or a file that was replaced with identical size and timestamp.

**Tolerance.** Default `mtime_tolerance_us = 1_000_000` (1 s). OneDrive generally preserves modification times across machines, but whether every client preserves sub-second precision is unverified (see [open questions](open-questions.md)). A one-second tolerance avoids a full rehash on the second machine if it doesn't. The risk it accepts, a same-size different-content replacement within one second, is negligible for photo files; `scan --audit` samples and rechecks cached hashes to catch anything systematic. Set it to 0 for paranoid mode.

**Moves and renames of individual files.** A file moved into a new folder has no cache record there, so it's rehashed. That's acceptable: moves made by hand are rare, and moves made by Photo Haven (restore, phase 2 copies) know the hash already and write the destination's cache record directly. An optional `trust_move_hints` setting (off in phase 1) would reuse facts when a file with the same name, size, and mtime vanished from another folder in the same scan.

## 4. Logs and ordering

- Decision and quarantine logs are append-only, one per machine, with records ordered by `(at, machine_id, seq)`.
- **Latest wins** among decisions on the *same* target. Different targets combine by specificity ([07 §5](07-report-and-decisions.md#5-resolving-effective-decisions)).
- **Clock skew.** Ordering relies on wall clocks. Decisions are made at human pace, so seconds of skew rarely matter, but minutes could reorder a quick correction made on another machine. `doctor` warns when this machine's clock is behind the newest `at` seen in another machine's log, which is a tell-tale of skew. Windows time sync normally keeps this well under a second.
- At the start of `report`, `plan`, `apply`, `verify`, and `purge`, the DB re-ingests all logs (cheap: they're small and append-only, so ingest is incremental by byte offset per log file).

## 5. Scenarios

**Two machines scan the same folder at the same time.** Each writes only its own cache file. Their facts match. No conflict.

**Decisions on the laptop, apply on the desktop.** The laptop's export is imported into the laptop's decision log, which syncs. The desktop's `plan` ingests all logs and shows "includes N decisions from txuleta". Works as long as the desktop has synced the laptop's log; `plan` prints the newest record time per machine so a stale view is obvious.

**Sync lag during apply (the dangerous one).** Machine A quarantines copy Y, keeping X. Before OneDrive deletes Y from machine B's disk, B plans to quarantine X, keeping Y. If both ran, the library would lose both copies (each would be safe in a different quarantine, but the library would have neither).

Prevention, in layers:

1. **Single apply machine.** `library.json` names an `apply_machine_id`. Only that machine may run `apply`, `restore`, and `purge`. Other machines can scan, report, and decide. This alone eliminates the race. Changing the apply machine is an explicit command that requires the current one's quarantine to be fully verified.
2. **Keeper check against all quarantine logs.** The planner refuses any keeper that appears in *any* machine's quarantine log as moved and not restored.
3. **On-disk check at move time.** Immediately before each move, apply re-stats the keeper and checks size and mtime against the plan.
4. **Purge re-check.** Purge byte-compares against the keeper, so a missing keeper blocks the permanent delete even if everything else failed.

**A machine dies or is reinstalled.** The new install gets a new `machine_id`. The dead machine's cache files remain and are still read; stale entries simply fail the size/mtime check. Its logs remain valid history. `machine retire <id>` marks it inactive in the registry; `cache prune --machine <id>` may then delete that retired machine's cache files (Photo Haven's own metadata only, never photos). This is the one sanctioned exception to "only the owner writes." If the dead machine was the apply machine and held a quarantine, that quarantine is on its disk: the quarantine log says what's there, and `restore` can run from the manifests if the disk is recoverable.

**OneDrive creates a conflict copy anyway** (for example `.photohaven-cache.3f1c9a0b22de-TOMAHAWK.jsonl`, or a conflict copy of `library.json`). Readers treat any file matching `.photohaven-cache.*.jsonl` as a cache source, using the `machine_id` in its header. `doctor` lists conflict copies and offers to merge and remove them.

**Folder renamed on another machine.** The cache files move with it. On the next scan, the old folder is `missing`, the new one appears with valid cached facts for every file, and nothing is rehashed. Decisions targeting the old path no longer match anything; the report lists such *orphaned decisions* so they can be re-pointed or dropped.

**Karen's library.** If Karen's Pictures syncs only to her machine, a scan from Rick's machine can't see it, and the DB on Rick's machine can't dedupe across the two libraries. Options are an open question: share her folder into Rick's OneDrive (it then appears as a root on Rick's machine), or run Photo Haven on her machine with the same project home, where her caches and the shared logs give Rick's DB her facts. The second option needs the DB to accept facts for a root this machine can't open, which the format allows (facts come from caches, not from local stat) but the scanner doesn't do in v1.

## 6. Rebuilding the database

`photohaven rebuild-db` reconstructs the DB from durable sources, without hashing anything:

1. Create an empty DB at the current schema.
2. Load `library.json` and the machine registry.
3. Walk each mapped root (directory listing and `stat` only). For each folder, read all cache files and resolve facts by §3. Files without usable facts are marked `sha256 = NULL` and will be hashed on the next scan.
4. Ingest every decision log.
5. Ingest every quarantine log; for batches on this machine, cross-check against manifests on disk.
6. Run the link pass.

Rebuild is also the integration test for the format spec: a DB built by scanning and a DB rebuilt from the scan's caches must be identical, apart from scan bookkeeping.

**Back up the DB anyway.** `photohaven` makes a copy of the DB in local state (`backups/photohaven-<date>.db`, keeping the last 7) before every `apply` and `purge`. The durable sources are the real backup, but restoring a copy is faster than a rebuild.

## 7. Machine identity lifecycle

| Event | What happens |
|---|---|
| First run on a machine | Create `machine.json` with a new UUIDv4; write `machines/<id>.json`; check `machine_short` for a clash with the registry (regenerate if so). |
| Every run | Update `last_seen` in this machine's registry file at most once per day, to avoid needless sync traffic. |
| Hostname changed | `doctor` notes it; identity is unchanged. |
| Disk cloned to a new machine | Two machines now share an identity. `doctor` detects this when the registry's `hostname` differs from the local one *and* both are active; fix with `machine reset`, which makes a new identity. |
| Retire | `machine retire <id>` marks it inactive. Its files remain readable. |
