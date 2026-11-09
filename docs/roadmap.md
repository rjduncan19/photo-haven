# Roadmap

Phase 1 (dedupe) is broken into milestones, each ending in something runnable and testable. Later phases get their own breakdown once phase 1 is in use.

## M0: Spec and spikes

Turn [03-format-spec](03-format-spec.md) from a draft into a binding contract, and settle the assumptions it rests on.

- [ ] `spec/schema.sql` (DDL from 03 §9), `spec/schemas/*.schema.json` for every record type, `spec/examples/` with one valid file per format.
- [ ] `spec/exiftool/photohaven.config` defining the `phv` namespace.
- [ ] **Spike: content hash** ([D-05](decisions.md#d-05-content-hash--exiftool-imagedatahash-sha-256)). On copies of real JPEG, HEIC, one raw, and one MOV: confirm `ImageDataHash` (SHA-256) is unchanged after writing XMP/EXIF; measure throughput on the Windows machine.
- [ ] **Spike: OneDrive behavior.** On a scratch folder inside OneDrive, on two machines: are mtimes preserved exactly across machines? What does the client do when ~1,000 files are moved out at once? Are hidden dotfiles synced? Which placeholder attributes appear on a dehydrated file?
- [ ] **Spike: Apple metadata.** Confirm the exact ExifTool tag names for ContentIdentifier (HEIC and MOV) and BurstUUID on a few real iPhone files.
- [ ] Answer the blocking [open questions](open-questions.md) (paths, ExifTool location).

**Done when:** the spec is marked version 1 (binding), the spikes are written up in `docs/spikes/`, and any spec changes they force are made.

## M1: Foundations and scanner

- [ ] Package skeleton (`pyproject.toml`, `src/photohaven`, console script), CI on Windows and Linux.
- [ ] `util`: path normalization, timestamps, UUIDv7, atomic writes, Windows attributes, long paths.
- [ ] `config` + `identity`: `init`, `machine`, `root add/map/list`, root markers, lock file.
- [ ] `formats`: cache file reader/writer with canonical JSON; round-trip tests against `spec/examples`.
- [ ] `db`: schema creation, migrations scaffold.
- [ ] `scan`: walk, placeholder detection, size grouping, hash pool, two hash passes, ExifTool bridge, metadata normalization, cache reuse, persist, missing-file handling, `--no-folder-cache`, `--audit`.
- [ ] `status`, `doctor`.
- [ ] Fixture generator and library builder.

**Done when:** a fixture library scans correctly; a rescan with no changes hashes nothing; a second simulated machine hashes nothing; `rebuild-db` reproduces the scanned DB.

## M2: Link pass and report

- [ ] `link`: dup groups, content-equivalent groups, raw pairs, Live Photos (UUID and fallback), edit links, flags, folder-name cleanup, `content_folder_names`.
- [ ] Folder and pair statistics; containment; suggestion engine.
- [ ] Static report: summary, easiest wins, fully redundant, overlap, warnings, tree; detail shards; thumbnails from embedded previews; localStorage; Export.
- [ ] `decisions import`, `decisions list`, `decisions show-effective`.

**Done when:** on a fixture library with planted relationships, the report shows each one in the right section with the right suggestion, and an export round-trips into the log.

## M3: Plan and apply

- [ ] Decision resolution; keeper choice; last-copy enforcement; conflict and blocked reporting; pair-split warnings.
- [ ] `plan` output and saved plans; `apply --execute` with plan-equality check, `--batch-limit`.
- [ ] Move protocol (same-volume and cross-volume), manifest, quarantine log, journal, crash recovery.
- [ ] Single-apply-machine enforcement.
- [ ] Post-apply light rescan of affected folders; `tidy --empty-folders`.

**Done when:** every safety test in [10 §3](10-testing.md#3-safety-tests-required-one-per-invariant) and every crash-injection case in [10 §4](10-testing.md#4-crash-injection) passes.

## M4: Verify, purge, restore

- [ ] `verify` (auto after apply; `--deep`).
- [ ] `purge` with hold period, byte compare, refusals, dry run.
- [ ] `restore` from manifest, including with DB and project home gone.
- [ ] `quarantine list/show`; DB backup before apply and purge.

**Done when:** the full flow init → scan → report → plan → apply → verify → purge → restore passes end to end on fixtures, on Windows.

## M5: First real run

Following the checklist in [10 §7](10-testing.md#7-before-touching-the-real-library):

- [ ] Disposable copy of 3–5 real folders, full flow.
- [ ] Read-only scan of the real library; review the report; note surprises; fix.
- [ ] First small real batch; verify; observe OneDrive; restore; reapply.
- [ ] Rounds: easiest wins (fully contained generic folders) first, then fully redundant, then overlap, then file-level leftovers.
- [ ] After the hold period: purge the first batches.

**Done when:** the backup folder and repeated imports are resolved, and reclaimed space matches the report's estimate.

## After phase 1

In rough order: phase 2 restructure, ongoing intake, Karen's library, phase 3 events, phase 4 culling, phase 5 Immich, phase 6 retrieval. See [09](09-later-phases.md).
