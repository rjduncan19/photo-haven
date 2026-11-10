# Roadmap

Phase 1 (dedupe) is broken into milestones, each ending in something runnable and testable. Later phases get their own breakdown once phase 1 is in use.

## M0: Spec and spikes

Turn [03-format-spec](03-format-spec.md) from a draft into a binding contract, and settle the assumptions it rests on.

- [x] `spec/schema.sql` (DDL from 03 §9), `spec/schemas/*.schema.json` for every record type, `spec/examples/` with one valid file per format, and `spec/check_spec.py` to keep them consistent with each other and with 03.
- [x] `spec/exiftool/photohaven.config` defining the `phv` namespace (validated by spike A).
- [x] Spike scripts and runbook: [spikes/m0](../spikes/m0/README.md). Results go in [docs/spikes/m0-results.md](spikes/m0-results.md).
- [x] Build the small test corpus (`make_corpus.py`). All M0–M4 work uses test data; the real library waits for M5.
- [x] Duplicate-case corpus (`make_dupe_corpus.py`): planted copies, moves, renames, date and metadata changes, damage, with an expected-groups manifest.
- [x] **Spike: content hash** ([D-05](decisions.md#d-05-content-hash--exiftool-imagedatahash-sha-256)). On copies of real JPEG, HEIC, PNG, and MOV/MP4: confirm `ImageDataHash` (SHA-256) is unchanged after writing XMP/EXIF; measure throughput on the Windows machine. Raw moved to [R1](#r1-live-photos-and-raw).
- [ ] **Spike: OneDrive behavior.** On a scratch folder inside OneDrive, on two machines: are mtimes preserved exactly across machines? Is a move *within* OneDrive synced as a true move (decides [D-10](decisions.md#d-10-quarantine-location))? What does the client do when many files are moved out? Are hidden dotfiles synced? Which placeholder attributes appear on a dehydrated file?
- Spike: Apple metadata: moved to [R1](#r1-live-photos-and-raw).
- [x] Answer the blocking [open questions](open-questions.md): ExifTool answered (Q-3); real paths deferred to M5 by choice (Q-1); Q-2, Q-4, Q-5 wait on spike C.

**Status (2026-11-10):** spec version 1 is binding. Only spike C (OneDrive) is open; it decides the quarantine location before M3 and may adjust `mtime_tolerance_us`, so M1 can start now.

**Done when:** the spec is marked version 1 (binding), the spikes are written up in `docs/spikes/`, and any spec changes they force are made. Live Photo, burst and raw questions don't block M0; they belong to R1.

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

- [ ] `link`: dup groups, content-equivalent groups, edit links, flags, folder-name cleanup, `content_folder_names`. (Raw pairs and Live Photos: R1.)
- [ ] Folder and pair statistics; containment; suggestion engine.
- [ ] Static report: summary, easiest wins, fully redundant, overlap, warnings, tree; detail shards; thumbnails from embedded previews; localStorage; Export.
- [ ] `decisions import`, `decisions list`, `decisions show-effective`.

**Done when:** on a fixture library with planted relationships, the report shows each one in the right section with the right suggestion, and an export round-trips into the log.

## M3: Plan and apply

- [ ] Decision resolution; keeper choice; last-copy enforcement; conflict and blocked reporting. (Pair-split warnings: R1.)
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

## R1: Live Photos and raw

Deferred from M0/M2/M3 so the base duplicate flow comes first. Exact duplicates of a still, a MOV, or a raw are still handled by M1–M4 like any other file; R1 adds the *relationships* between different files.

- [ ] **Spike: raw.** Spike A on at least one sample per raw format in the library (`ImageDataHash` stable; whether ExifTool can write ratings or a sidecar `.xmp` is needed).
- [ ] **Spike: Apple metadata.** Live Photos and bursts copied straight from the iPhone, next to the same photos uploaded by OneDrive: exact tag names (ContentIdentifier for HEIC and MOV, BurstUUID), and whether the OneDrive upload drops them. See [m0-results B](spikes/m0-results.md#b-apple-tags-03-33-05-4).
- [ ] `link`: raw pairs, Live Photos (ID, then a guarded name fallback that rejects false pairs such as a JPEG next to an unrelated MP4).
- [ ] Pair-split warnings in plan; report sections for pairs.

**Done when:** on fixtures with planted pairs, plan never splits a pair silently, and the report shows each pair. R1 must be done before M5 touches folders that contain iPhone camera rolls or raw files; M5 rounds on other folders can go first.

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
