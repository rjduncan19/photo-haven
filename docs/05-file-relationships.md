# 05. File relationships

Files relate to each other in several distinct ways. They must stay separate in the data model, because each one implies a different action. Only the first one is acted on in phase 1.

| # | Relationship | Detected by | Stored as | Phase-1 action |
|---|---|---|---|---|
| 1 | **Exact duplicate** | Same `sha256` | Shared `files.sha256` (`dup_groups` view) | Quarantine redundant instances |
| 2 | **Content-equivalent** (metadata-only difference) | Same `content_sha256`, different `sha256` | `links` kind `content_equiv` | Report only |
| 3 | **Raw + JPEG pair** | Stem, folder, time | `links` kind `raw_pair` | Keep together; never treat as duplicates |
| 4 | **Live Photo** (still + MOV) | Apple ContentIdentifier; fallback stem, folder, time | `links` kind `live_photo` | Keep together; flag orphans |
| 5 | **Edited derivative** (iOS `IMG_E####`, `.AAE`) | Naming convention, folder | `links` kind `edit` | Report only |
| 6 | **Burst / near-duplicate** | Apple BurstUUID; time clustering + perceptual hash | `links` kind `burst` | Parked (phase 4) |

**Refinement:** relationship 5 is new compared to the handoff. iOS exports and some sync paths produce `IMG_E1234.HEIC` (the edited version) alongside `IMG_1234.HEIC` (the original), plus `IMG_1234.AAE` (the edit recipe). These look like near-duplicates but are deliberate edits; the edited one is usually the one worth keeping.

## 1. Exact duplicates

- **Definition:** two instances with the same `sha256` (and therefore the same size).
- **Group ID:** the `sha256` itself. No separate group table is needed.
- **Only present instances count.** Quarantined, purged, missing, and cloud-only-unhashed instances are excluded from `dup_groups`.
- **Folder names follow the content.** Every folder an instance has lived in is recorded in `content_folder_names`, even after that instance is purged. When the keeper sits in `Camera Roll/2015/07` and the purged copy sat in `2015-07 Hawaii trip`, the keeper still knows it belongs to the "Hawaii trip" event. Phase 2 uses this.

Keeper selection and the last-copy invariant are defined in [08](08-apply-quarantine-verify.md#2-choosing-keepers).

## 2. Content-equivalent files

Same image data, different metadata: for example, a copy whose EXIF was stripped by an upload service, or one that some app re-tagged.

- **Detected by** grouping `contents` on `content_sha256` where more than one `sha256` shares it.
- **Phase 1:** reported as "same picture, different metadata" groups with a per-group diff of the key metadata fields (date taken, GPS, camera). Not removed by folder decisions, because the last-copy invariant is defined on byte-identical copies.
- **Later:** an explicit file-level action `remove_equivalent` could quarantine the poorer-metadata copy, with purge verifying `content_sha256` equality (and that the keeper has at least the same date and GPS fields). Deferred until the exact-duplicate flow has earned trust.

## 3. Raw + JPEG pairs

The raw is an attachment to its JPEG, never a duplicate of it.

**Candidate rule:** within one folder, a `raw` file and an `image` file share `stem_key`.

| Evidence | Confidence |
|---|---|
| Same stem, both have `taken_local` within 2 s, same `model` | `high` |
| Same stem, times within 2 s, model missing on one side | `medium` |
| Same stem, no comparable times | `low` |
| Same stem, times differ by > 2 s | not linked (flagged `stem_collision`; DSLR counters wrap at 9999) |

Variants to handle:

- **Sibling folders.** Some import tools split `RAW/` and `JPG/` subfolders. Second pass: link across sibling folders of a common parent when stem, time (≤ 2 s), and model all match, `confidence = medium`.
- **Duplicated pairs.** If folder A holds `DSC_0042.JPG` + `DSC_0042.NEF` and folder B holds only a copy of the JPEG, then A's JPEG is a duplicate of B's, but A's NEF is unique. Removing folder A would strand the NEF. The last-copy invariant already blocks the NEF from being quarantined; the report additionally shows a "pair would be split" warning on folder A. A later `consolidate` action could move the NEF next to the keeper JPEG.
- **Policy:** raws only matter for keepers. Phase 4 culling of a JPEG proposes culling its raw too.

## 4. Live Photos

An iPhone Live Photo is a HEIC (or JPEG) still plus a short MOV, stored as two files.

**Primary link:** equal `apple_content_id`. The still carries it in Apple maker notes (`MakerNotes:ContentIdentifier`); the MOV carries it in QuickTime metadata (`Keys:ContentIdentifier`). Read via ExifTool. Matched across the whole library, not just one folder, since some exports separate them. `method = content_id`, `confidence = high`.

**Fallback link** when one side lacks the identifier: same folder, same `stem_key`, the video is ≤ 4 s, and capture times are within 2 s. `method = name_time`, `confidence = medium`.

**Flags:**

| Flag | Condition | Meaning |
|---|---|---|
| `orphan_live_video` | Video ≤ 4 s with an `apple_content_id` but no linked still; or ≤ 4 s MOV whose stem matches nothing | The still was lost or exported without its video. Usually low value on its own. |
| `orphan_live_still` | Still with an `apple_content_id` and no video anywhere | Fine; most exports drop the video. Informational. |
| `live_pair_split` | A linked pair's members are in different folders | Informational. |

**Duplicated pairs** follow the same logic as raw pairs: each file is deduplicated by its own `sha256`, and the report warns when a folder removal would leave a still's only video, or a video's only still, stranded with no partner among the keepers.

## 5. Edited derivatives

- `IMG_E1234.<ext>` alongside `IMG_1234.<ext>` in the same folder → `edit` link, roles `original` and `edited`. `confidence = high` if capture times match, else `medium`.
- `IMG_1234.AAE` → linked to `IMG_1234.*` as role `aae`. AAE files are small XML edit recipes; they are only meaningful next to their original.
- Phase 1: reported and kept. A later phase may prefer the edited version as the "display" image and fold the original in as an attachment.

## 6. Bursts and near-duplicates (parked)

Different shots of the same moment, sharing no bytes.

- **Apple bursts:** `apple_burst_id` (`MakerNotes:BurstUUID`) groups them directly, `method = apple_burst`, `confidence = high`.
- **Others:** cluster by `taken_local` within ~1–2 s and same `model`; confirm with a perceptual hash (pHash or dHash, Hamming distance threshold) or image-embedding cosine similarity. `method = time_cluster`.
- **Format conversions** (HEIC exported to JPEG, resized copies, re-encoded uploads) also land here: different `content_sha256`, near-identical perceptual hash.
- Phase 4 proposes the best shot by quality score; the user confirms.

## 7. Folder names and event guesses

Folder names are the best event information the library has. Each folder gets a cleaned `event_guess`, or null if it's generic.

**Generic** (`is_generic = 1`, `event_guess = null`) if the name, after trimming, case-insensitively matches `library.toml [folders] generic_names` or a `generic_patterns` regex (`100APPLE`, `2015`, `07`, `2015-07`), or if it becomes empty after cleanup.

**Cleanup** for non-generic names:

1. Strip leading date-like prefixes: `2015-07-04`, `2015_07_04`, `20150704`, `2015-07`, `07-04-2015`, `July 2015`, followed by separators (` `, `-`, `_`, `.`).
2. Strip trailing copy markers: ` (1)`, ` - Copy`, ` copy 2`.
3. Strip IMG-style junk tokens (`IMG`, `DSC`, `DCIM`) and bare sequence numbers.
4. Replace `_` and runs of separators with single spaces; trim.
5. If what's left is empty or generic, `event_guess = null`.

| Folder name | `event_guess` |
|---|---|
| `2015-07-04 Hawaii trip` | `Hawaii trip` |
| `20180812_Fiona_soccer_tournament` | `Fiona soccer tournament` |
| `Backup 2019` | null (generic) |
| `100CANON` | null (generic pattern) |
| `Christmas 2012 (1)` | `Christmas 2012` |

A date stripped from a folder name is kept as a hint (`detail` in the report) and can confirm or question a file's `taken_local` in phase 2.

**Most descriptive name** for a content with several folder names: prefer non-generic over generic, then the longest `event_guess` in tokens, then the one whose stripped date agrees with `taken_local`, then the deepest folder. Phase 2 uses this to name the keeper's event.
