# M0 spike results

*Fill in as the spikes in [spikes/m0](../../spikes/m0/README.md) are run. Each finding links back to the decision it confirms or changes.*

**Machines:** tomahawk (desktop, Windows 11 Pro 26300, Python 3.12.10). txuleta not used yet. **ExifTool version:** 13.61 (Oliver Betz Windows build). **Date:** 2026-11-10.

**Run so far:** environment check, spike A (rerun after the warning fix), spike B, and the duplicate-case corpus. Spike C (OneDrive) was skipped for this round. Raw and Live Photo/burst questions moved to roadmap [R1](../roadmap.md#r1-live-photos-and-raw).

## Environment (`check_env.py`)

- Python 3.12.10 and ExifTool 13.61: ok.
- **Long paths are disabled** on tomahawk (`LongPathsEnabled=0`). Either enable them or have the product use `\\?\` paths everywhere (see [open questions](../open-questions.md)).
- SHA-256 in memory: 1.9 GB/s per core, so disk is the limit.

## Test corpus

Source: `C:\TestPics` (879 files, read-only). Corpus: `C:\PH-test\corpus2`, built with `make_corpus.py --per-type 40 --max-per-folder 40` (the default `--max-per-folder 3` picked only 3 HEICs, because they all sit in one folder).

| Source kind | Files | Size | Notes |
|---|---|---|---|
| iPhone 17, iOS 26.6.2, OneDrive camera upload (`YYYYMMDD_HHMMSSmmm_iOS.*`) | 71 | 331 MB | 40 HEIC, 9 JPEG, 11 PNG, 3 MOV, 8 MP4 |
| Older camera JPEGs (2003–2007 events, 2020, 2025 incl. Canon) | 33 | 243 MB | JPEG only |
| DSLR raw | 0 | – | **No raw files available.** Raw formats: none tested |

Spike B was run on all of `C:\TestPics` (867 stills/videos), since it only reads.

## A. Content hash (D-05, D-06)

Each file: a rating plus `XMP-et:OriginalImageHash` plus `XMP-phv:IngestFileSHA256` in one write, then a re-rate, an EXIF edit, and a truncated copy.

| Type | n | PASS | FAIL | No hash | Write failed | phv read back | Same size after write | Image-hash MB/s |
|---|---|---|---|---|---|---|---|---|
| HEIC | 40 | 40 | 0 | 0 | 0 | 40 | 0 | 121 |
| JPEG | 42 | 42 | 0 | 0 | 0 | 42 | 0 | 92 |
| Raw | – | – | – | – | – | – | – | *not tested* |
| Video (MOV/MP4) | 11 | 11 | 0 | 0 | 0 | 11 | 0 | 301 |
| PNG | 11 | 11 | 0 | 0 | 0 | 11 | 0 | 30 |

- The image-data hash was deterministic and stayed the same across all writes; the file SHA-256 always changed; the file size always changed.
- For the 11 videos, the EXIF-edit step changed nothing ("0 image files updated"), so videos were tested with XMP/rating writes only.
- Throughput: about 100 MB/s for stills on one ExifTool process, so a 350 GB library needs about 1 h of ExifTool time per process (more if the disk is slower). PNG is slow (30 MB/s) but small.

**Truncated files (important):** a copy truncated to half its size **still returns an `ImageDataHash`** (a different value) for 93 of 93 stills and 7 of 11 videos. ExifTool flags the damage only in a warning (`JPEG format error`, `Error reading hvc1 image data`, `Truncated data`, `Error reading vide data`). On the first run the spike read `-Warning` without `-a`, and JSON output keeps only the first warning. On `IMG_1557.JPG` (Canon) that was `[minor] Adjusted MakerNotes base by 456`, which hid the format error.

**Fixed and rerun (2026-11-10):** `phspike.ExifTool.hash_check` now requests `-a -G4`, which numbers repeated tags (`Copy1:Warning`, `Copy2:Warning`, …), so every warning comes back. Each warning is classed as one of:
- **damaged:** any Error, or a warning matching truncation/format/read-error patterns. The hash must not be trusted.
- **suspect:** any other warning that isn't `[minor]`. It's recorded, and the hash is still used. Example: `IPTCDigest is not current. XMP may be out of sync` on an intact iPhone JPEG (`20261007_010551690_iOS.JPG`). A blanket "any non-minor warning = damaged" rule flagged that file wrongly.
- **minor:** ignored.

Rerun result: **104 of 104 truncated copies are flagged damaged, and 0 of 104 originals.** A single flipped byte in JPEG entropy data, at the same size, is **not** noticed by ExifTool: it gives a different `ImageDataHash` with no warning (see the duplicate corpus below).

Consequences for the scanner (docs/04, stage 4):
- Request `-a -G4` (or parse every warning some other way). docs/04 currently says `-G1` without `-a`, which loses warnings.
- Classify warnings as above, and store `flags: exiftool_error` for damaged files. A damaged file's `content_sha256` never counts as proof of equivalence.
- Never treat "a hash was returned" as "the file is intact."

**Conclusion:** D-05 is **confirmed for HEIC, JPEG, PNG and MOV/MP4**. It is still **open for raw** (no samples). The truncation result adds a rule for reading warnings.

## Duplicates already in `C:\TestPics`

Read-only survey of all 879 files (SHA-256 plus ImageDataHash):
- **No byte-identical duplicates at all.** `black/Gnome Hopping 2004` is empty, so the folder name overlaps `home1/Gnome Hopping 2004` but no files do.
- **One content-equal pair that is *not* a duplicate:** `2020-August RV/IMG_0293.JPG` and `IMG_0561.JPG` are two different exposures four days apart (ISO 6400, 1/25 s), both blank uniform frames, so their image data is identical. This supports keeping content-equivalent groups **report only** (05 §2). A future `remove_equivalent` must also require equal capture time, not only equal `content_sha256` plus "at least as much metadata".

Because the real set has no exact duplicates, the duplicate cases are planted instead (next section).

## Duplicate-case corpus (`make_dupe_corpus.py`)

Built at `C:\PH-test\dupecases` from 7 base photos in `corpus2`: 57 files, 7 exact-duplicate groups (38 files, 31 redundant copies), 3 content-equivalent groups, and 5 sizes shared by different content. `verify` passes: every planted case gets the expected SHA-256 group, content-hash group and damage flag. That includes the NFC/NFD name pair, the hidden and read-only copies, and the 330-character path (ExifTool 13.61 opened it; the script also passes `-api WindowsLongPath=1`). `mutate` (move, rename, touch, in-place rating, silent corruption with size and mtime restored, delete, new copy) also verifies. The full case list is in the [runbook](../../spikes/m0/README.md#2b-duplicate-case-corpus-for-m1m2).

Findings from building it:
- **Same size, different content is not rare after edits.** Five sizes are shared by different content in a 57-file set (a flipped byte, plus metadata edits that happen to land on the same size). docs/04 calls same-size-but-different files rare, which is fine for prioritizing which files to hash first, but grouping by size must never be taken as a sign of equality.
- **A flipped byte is invisible to ExifTool** (no warning). Only SHA-256 and the byte compare at purge catch it. With size and mtime restored, as `mutate` does, a normal rescan reuses the stale cached hash. Only `scan --audit` / `--rehash` finds it, which supports keeping the byte-for-byte compare in apply and purge (rules 2 and 6).
- **NFC and NFD forms of `Café.jpg` coexist in one NTFS folder** as two files. The spec's `NFC + casefold` path key makes them collide. `path_key` is "used for uniqueness" (03 §1.6), so the DB needs a tiebreak on the raw name, as cache files already have.

## B. Apple tags (03 §3.3, 05 §4)

| Purpose | Tag(s) found | In which types |
|---|---|---|
| Live Photo link | `Apple:ContentIdentifier` (family 0: `MakerNotes`) found in **1 JPEG only**. **Not found in any of the 189 HEICs**, which instead carry `Apple:PhotoIdentifier` (unique per photo) and `Apple:LivePhotoVideoIndex` (repeated across many photos, so it is not a link). No video had any ContentIdentifier. | JPEG (1) |
| Burst | none (`BurstUUID` absent everywhere) | – |

Pairs by ID: 0 · by folder+name: 1 · disagreements: 1 (`20261016_080654000_iOS.jpg` + `.mp4` is a **false match**: a 1600×1200 still next to an unrelated 848×480, 7.6 s clip that shares its timestamp name) · orphan stills: 1 · orphan videos: 0 · videos with no ID: 11 (3 MOVs are normal iPhone 17 clips, the MP4s are received or exported clips).

**Conclusion: not settled.** The set has no Live Photo videos. Every iPhone file has OneDrive camera-upload names, and none of the HEICs has a ContentIdentifier. Either OneDrive's camera upload drops the Live Photo video and the ContentIdentifier, or this camera roll has no Live Photos. To settle it, we need a few Live Photos and a burst copied **straight from the iPhone** (USB / Windows Photos import, or "Export unmodified original"), next to the same photos as OneDrive uploaded them.

**Update 2026-11-10 (Rick, by hand on the iPhone):** OneDrive camera upload does not keep Live Photos. Fresh Live Photos upload as the HEIC still only; no `.MOV` reaches the PC, and the OneDrive web client doesn't play them as live. This is a personal OneDrive (M365) account with *Include Videos* on, and ordinary videos upload fine. So the OneDrive-synced library holds stills only: there are no Live Photo pairs in it to split, and the motion exists only on the phone (and in iCloud, if enabled). Paired copies can only come from iCloud for Windows or a USB import ("Keep Originals"). When such a copy meets the OneDrive still, it is a content-equivalent but fuller version; R1 must prefer the paired copy as keeper.

Already clear for the spec:
- ExifTool `-G1` reports the group as `Apple`; `-G0` reports `MakerNotes`. §3.3 should say which group family it means.
- The folder+name fallback (05 §4) creates false pairs with OneDrive's timestamp names. It needs a guard, for example the clip must be a short MOV (not MP4) taken by the same model, or a lower confidence with no automatic keep-together.
- If OneDrive uploads really drop the video and the ID, Live Photo linking can't rely on IDs for OneDrive-uploaded libraries.

## C. OneDrive

Run 2026-11-11 on tomahawk (personal OneDrive, M365; Windows 11; one internal drive, C:). Probe: 200 files of 1 MB random bytes plus a hidden `.photohaven-cache.probe.jsonl`, in `OneDrive\PH-probe-area`. The second-machine steps (C2, C3) are still to do.

| Step | Result |
|---|---|
| C0. Idle upload | 1.7–2.4 KB/s. Negligible next to the moves measured. |
| C1. Create and sync | 200 MB uploaded in about 90 s (211 MB on the wire). Locally the dotfile stays hidden; no placeholders (the library is kept on the device). |
| Dotfiles | **Synced.** `.photohaven-cache.probe.jsonl` shows on OneDrive on the web. So in-folder cache files reach other machines (06 §2). Whether they stay hidden there is C2. |
| C4. Move inside OneDrive (`PH-probe-area` → `PH-quarantine-test\q1`) | **A true server-side move.** 1.2 MB uploaded in 4 minutes against about 3.6 MB of background traffic over the same time. On the web, nothing in the Recycle bin, and a moved file still has a single version 1.0 (checked by Rick). Moving back was the same: 1.2 MB. |
| C5. Move out of OneDrive (to `C:\PH-test\quarantine-out`) | Nothing uploaded, as expected for a delete. Moving the files back in re-uploaded **209 MB**, a full re-upload. OneDrive showed a notice asking whether to delete the 200 files, and all 200 went to the web Recycle bin (checked by Rick). |

**What this settles:**

- **Q-2 / D-10 (quarantine location): decided, inside OneDrive.** An apply that moves duplicates to a folder inside OneDrive is a set of server-side moves: no upload, no recycle-bin churn, and the folder is visible to every machine. Cloud space is freed only when it is emptied. Rick chose a `PhotoHaven-DELETE` folder at the top of each root ([D-10](../decisions.md#d-10-quarantine-location)).
- **On this machine, both modes are same-volume renames.** tomahawk has a single drive, so a local quarantine outside OneDrive (C:\PhotoHavenQuarantine) would also be an instant rename. The difference is only what OneDrive does: a delete (cloud space freed, files in the recycle bin, a full re-upload if restored), or a move.
- **Restore cost.** With a quarantine outside OneDrive, every restore is a full re-upload (C5's move back). Inside OneDrive, a restore is another server-side move.

**Still open:** C2/C3 on txuleta (cross-machine mtime precision, Q-4; dotfile still hidden there; placeholder attributes after *Free up space*), and whether moved files arrive on txuleta without a download. The probe is left in `OneDrive\PH-probe-area\PhotoHaven-probe-b2a78bc4` for that; `onedrive_probe.py cleanup` removes it afterwards.

## Follow-ups

1. Spec and doc changes from these results, made as part of the M0 spec pass: docs/04 stage 4 (`-a -G4`, warning classes, size is not equality), 03 §1.6 (NFC/NFD collisions in `path_key`), 05 §2 (capture time for any future `remove_equivalent`).
2. Spike C (OneDrive): C0, C1, C4, C5 done on tomahawk (above). C2/C3 need the second machine.
3. Raw, Live Photo and burst checks: [R1](../roadmap.md#r1-live-photos-and-raw).
