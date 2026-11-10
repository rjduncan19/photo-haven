# 08. Apply, quarantine, verify, purge

This is the only part of Photo Haven that changes the library. Every step is designed so that an interruption at any instant leaves every photo in at least one place, and so that the system can tell which.

## 1. Invariants

| # | Invariant | Enforced by |
|---|---|---|
| I-1 | Photo Haven never deletes a file inside a library root. Instances leave only by a move to quarantine. | Apply has no delete call for library paths; the cross-volume move deletes the source only after a verified copy exists (§4). |
| I-2 | **Last copy.** An instance is quarantined only if another present instance with the same `sha256` stays in the library as its keeper. | Planner (§2), re-checked at move time (§4) |
| I-3 | Keepers are never in any machine's quarantine. | Planner checks all quarantine logs; single apply machine ([06 §5](06-multi-machine.md#5-scenarios)) |
| I-4 | Nothing is moved unless it still matches what was planned (size, mtime). | Pre-move checks (§4) |
| I-5 | Nothing is permanently deleted from quarantine without a fresh byte-for-byte match against a readable keeper. | Purge (§7) |
| I-6 | Every quarantined file can be restored from its batch manifest alone. | Manifest written per move (§5) |

## 2. Choosing keepers

For each duplicate group (one `sha256`, ≥ 2 present instances):

1. Resolve the effective action of every instance ([07 §5](07-report-and-decisions.md#5-resolving-effective-decisions)). Partition into **R** (`remove`) and **K** (`keep` or `undecided`). Undecided instances are never quarantined.
2. Drop from K any instance that is not eligible as a keeper: not `present`, unhashed, or listed as moved-and-not-restored in any quarantine log.
3. If **K is empty** (every instance is marked remove, or the only others are ineligible): promote the best instance of R to keeper and report an `all_remove_conflict` for the group. Never quarantine every copy.
4. Pick the **reference keeper** from K, recorded in the plan and manifest as "the copy this one duplicates": explicit `keep` first, then intake, then non-generic folder, then the most descriptive folder name, then shortest path.
5. Every instance in R (minus any promoted keeper) becomes a plan item pointing at the reference keeper.

**Cloud-only keepers** are allowed (they exist in OneDrive). Purge will hydrate that one keeper file to byte-compare it, and says so.

**Cloud-only instances in R** are skipped in `local` quarantine mode (moving a placeholder out of OneDrive would force a download first) and listed in the plan as skipped. In `onedrive` mode they can be moved as-is.

## 3. Plan and dry run

`photohaven plan` (or `apply` without `--execute`) ingests all logs, resolves decisions, chooses keepers, and saves the plan (`plans`, `plan_items`) with a `decisions_digest`. Output:

```
Plan 01932f6e-… (from scan 14, decisions through 2026-11-12T05:01:44Z incl. 3 from txuleta)

  Will move to quarantine:   18,432 files   112.6 GB   from 41 folders
  Quarantine:                D:\PhotoHavenQuarantine   (cross-volume copy; needs 112.6 GB, 640 GB free)
  Kept (undecided/keep):     2,104 duplicate instances left in place by your decisions
  Blocked (last copy):       317 files in "remove" folders have no other copy and stay
  Conflicts:                 4 groups where every copy was marked remove; kept one of each
  Skipped (cloud-only):      12 files
  Warnings:                  9 raw/JPEG pairs would be split; 23 Live Photo pairs would be split

  Top folders:
    Backup 2019/                    9,812 files   61.0 GB   all copies in Camera Roll/
    Pictures/Import 2016-03 (1)/      822 files    4.4 GB   identical to Import 2016-03/
    …
Run `photohaven apply --execute` to carry out this plan.
Full list: %LOCALAPPDATA%\PhotoHaven\plans\01932f6e-….txt
```

`apply --execute` re-plans and refuses to proceed if the result differs from the last plan shown (a different `decisions_digest` or item set). What runs is exactly what was reviewed. `--batch-limit N` caps one run at N files, for working in rounds.

## 4. The move protocol

For each plan item, in folder order:

**Pre-move checks** (skip the item and report it on any failure, never "do it anyway"):

- The source exists, isn't a placeholder, and its size and mtime equal the plan's.
- The reference keeper exists with the size and mtime recorded at plan time.
- No other quarantine log lists the keeper as moved.
- The destination `qpath` doesn't exist (or exists as a finished copy with the right hash, from an interrupted earlier run; see §5).

**Same volume** (the quarantine root is on the same NTFS volume as the root): `os.rename(source, qpath)`. Atomic: the file is in exactly one place at every instant.

**Cross volume** (the normal case, quarantine on another drive):

1. Copy the source to `qpath + ".partial"`, computing SHA-256 while copying.
2. Flush and fsync the copy. Copy timestamps (`shutil.copystat`).
3. Require: copy hash == planned `sha256`, and copy size == planned size. Otherwise delete the `.partial` (Photo Haven's own temp file), mark the item failed, continue.
4. Rename `.partial` → `qpath`.
5. Re-stat the source: it must be unchanged since step 1. Then delete the source. *This is the move completing, not a delete in the sense of I-1: a verified copy already exists in quarantine.*

After each move: append the item to the batch manifest, append a `moved` event to the quarantine log, set the DB state to `moved`, and commit.

**Folders left empty** are not deleted by apply. `photohaven tidy --empty-folders` (explicit, with dry run) removes folders that contain nothing but Photo Haven cache files. After apply, the affected folders get a light rescan so this machine's cache files reflect the moves.

## 5. Journal and crash recovery

The DB row for an item is written as `moving` and committed *before* the filesystem operation starts. On the next `apply`, `verify`, or `status`, any `moving` items are reconciled:

| Found on disk | Meaning | Action |
|---|---|---|
| Source present, no quarantine file | Crashed before copy finished | Delete any `.partial`; item back to planned |
| Source present, `.partial` present | Crashed mid-copy | Delete `.partial`; item back to planned |
| Source present, `qpath` present with the right hash | Crashed between steps 4 and 5 | Resume at step 5 |
| Source absent, `qpath` present with the right hash | Crashed after step 5, before journaling | Write manifest/log entries; mark `moved` |
| Source absent, `qpath` absent | **Should be impossible** | Stop the run. Report loudly with the path, `sha256`, and keeper. The keeper still holds the same bytes (I-2), so no content is lost, but this indicates a bug. |

Crash-injection tests cover every row ([10](10-testing.md)).

## 6. Verify (right after apply)

`photohaven verify [--batch ID]` runs automatically at the end of `apply --execute`:

- Each quarantined file exists and re-hashes to its `sha256`.
- Each reference keeper still exists with the planned size and mtime. `--deep` re-hashes keepers too.

Pass → `verified` event. Fail → `verify_failed` with detail; `status` shows it prominently, and the suggested next step is `restore`.

## 7. Purge (permanent delete, from quarantine only)

`photohaven purge --batch ID [--execute] [--min-age DAYS]`

Preconditions for the batch: every item is `verified`, and the batch is at least `min_age` days old (default **14**, configurable). The hold period gives time for second thoughts and for OneDrive's own recycle bin window to remain a second net while the quarantine is still intact.

Per item, immediately before deleting:

1. The keeper exists and isn't listed as moved in any quarantine log.
2. **Byte-for-byte compare** the quarantined file against the keeper, streaming both in 1 MiB chunks. A hash match is not enough here; the compare also proves the keeper is readable right now.
3. Match → delete the quarantined file (the only `os.remove` on a photo anywhere in Photo Haven), log `purged`.
4. Mismatch, unreadable, or keeper missing → `purge_refused` with detail. The file stays in quarantine.

Without `--execute`, purge runs all the compares and reports what it would delete and what it would refuse.

When a batch is fully purged, its manifest is kept (it's tiny) as a permanent record.

## 8. Restore

`photohaven restore --batch ID [--path RELPATH]` works from the manifest:

- Original path free → move back (same protocol as §4, reversed); log `restored`.
- Original path holds the same `sha256` already → nothing to do; log `restored` with detail `already_present`.
- Original path holds different content → restore beside it as `<stem> (restored)<.ext>` and flag it.
- Root not mapped on this machine → refuse, naming the root.

## 9. OneDrive considerations

These apply to `local` quarantine mode. In `onedrive` mode, apply is a set of moves within OneDrive, and the deletions (with their recycle-bin and quota effects) happen only at purge.

- **Moving a file out of the OneDrive folder deletes it from OneDrive** (and from every synced device). OneDrive keeps it in its recycle bin for a limited time (30 days for personal accounts). That's a useful second safety net, but Photo Haven's quarantine is the primary one.
- **Mass deletions** may make the OneDrive client show a notice or confirmation about many files being removed. Read it carefully. The expected outcome is that the deletion stands, because the files are safe in quarantine.
- Work in rounds (`--batch-limit`, or one folder group at a time) so OneDrive's sync queue stays manageable and each round can be verified before the next.
- Space in OneDrive frees up as OneDrive processes the deletions (subject to its recycle bin rules). Space on the quarantine drive frees up only at purge.

## 10. Quarantine location

**Open decision** ([D-10](decisions.md#d-10-quarantine-location)): inside OneDrive (outside every root) or on local disk outside OneDrive. Spike C4 decides whether OneDrive syncs an in-OneDrive move as a true move. Either way:

- MUST be outside every library root and the project home, so scans never see quarantined files as library files. `init` and `doctor` check this.
- In `onedrive` mode, the quarantine root is a top-level OneDrive folder such as `<OneDrive>\PhotoHaven-quarantine\`. Moves are same-volume renames (§4), cloud-only placeholders can be quarantined without hydrating, and purge deletes go through OneDrive's recycle bin, which is one more safety net.
- In `local` mode, the following rules apply.
- SHOULD be on a different physical drive from the library if one is available, so a single drive failure can't take both. On the same volume, moves are instant renames and need no extra space; across volumes, the plan checks free space first.
- Recorded per machine in `machine.toml` and per batch in the manifest, so a batch remains findable even if the setting changes later.
