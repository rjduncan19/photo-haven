# 01. Vision and scope

## The problem

The library holds roughly 300–400 GB of family photos and videos from more than 20 years. Most are JPEG and iOS HEIC, plus a few raw files and many short MOV clips from Live Photos. None of it has ever been organized:

- The OneDrive camera roll (iOS uploads, `year/month` folders) keeps growing.
- Scattered folders in the OneDrive Pictures library include DSLR import folders, some imported more than once, and one large backup folder that is probably mostly duplicates.
- Karen's OneDrive library is a second, separate source to add later.

The whole OneDrive library has been set to *Always keep on this device*, so the working copy is on local disk, and that disk is also the live OneDrive sync folder. Any file Photo Haven moves out of that folder is deleted from the cloud by OneDrive.

## Value principles

| Principle | What it means in practice |
|---|---|
| **Safe** | Scan → report → review → act. Removal is a move to quarantine. Permanent delete only after a fresh byte compare proves a keeper exists. Originals are never edited. |
| **Easy** | Point it at the folders. Decisions are made per folder where possible. Suggestions come pre-filled. |
| **Intelligent** | Organizes by people, places, and events. Eventually answers natural-language queries. |

Tagline: *bring it all in, lose nothing, find anything.*

## Goals by phase

| Phase | Goal | Status |
|---|---|---|
| **1. Dedupe** | Find exact duplicates, review per folder, quarantine redundant copies, verify, purge. | **Designing now** |
| 2. Restructure | Build a new library alongside the old one, organized `year/month[ event]` by capture date. | Designed at outline level |
| 3. Events | Cluster by time and GPS, name clusters with a local vision-language model, human approves. | Parked |
| 4. Cull and rate | Blur/exposure checks, aesthetic scoring, best-of-burst, ratings. | Parked |
| 5. Browse | People (face recognition across ages), places, trips, likely through Immich. | Parked |
| 6. Retrieve | Embeddings plus per-photo descriptions plus filters, with a chat-style query. Stories, albums, anniversaries, shared family access. | Vision |

Phase 1 must be complete and trusted before phase 2 begins. The dedupe-first ordering is deliberate: exact duplicates are resolved while every file is still pristine, before any metadata is written anywhere.

## Who it's for

Primarily Rick: a ~30-year software engineer (expert C++, comfortable with Python, Docker, local servers). He wants the process to be interactive and reviewable, not a blind script. Karen is a second library owner and possibly a reviewer later. Whether Photo Haven becomes a product is undecided (see [open questions](open-questions.md)). The design keeps that door open by treating the data formats as the product (see [decisions D-01](decisions.md#d-01-data-formats-are-the-product)).

## Non-goals (for now)

- **No photo editing.** Photo Haven organizes; it doesn't develop raws or retouch.
- **No cloud service.** Everything runs locally. OneDrive is storage, not a compute platform.
- **No replacement for OneDrive Photos or Immich viewing.** Photo Haven feeds a browsing layer rather than becoming one, at least until phase 6.
- **No automatic deletion of unique photos in phase 1.** Phase 1 only removes byte-identical redundant copies. Culling unique bad photos is phase 4 and needs explicit, per-file confirmation.
- **Not tied to the yearbook deadline.** The grad ad due November 20, 2026 is a separate manual sprint using OneDrive People. Nothing here should block on it.

## Success criteria for phase 1

1. A full scan of the real library completes, is resumable after interruption, and a rescan with no changes finishes in minutes, not hours.
2. The report shows reclaimable space per folder, lists fully-contained folders first, and the decisions made in it round-trip into the apply step.
3. The apply dry run lists every move and the space saved. Apply moves files into quarantine with no file lost, even when interrupted mid-run.
4. Purge byte-compares every quarantined file against its keeper and refuses any that fail.
5. Two machines scanning the same synced folder converge on the same facts without OneDrive conflict copies.
6. Every safety invariant in [CLAUDE.md](../CLAUDE.md) has a test.
