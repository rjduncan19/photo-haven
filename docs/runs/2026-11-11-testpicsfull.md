# First full-library run: `C:\testpicsfull` (2026-11-11)

A full copy of the family library (Rick made it with Explorer; 340 GB) on tomahawk's only drive, C:. Photo Haven ran against the copy only. State and project home were in `C:\PH-test\full`, outside the copy and outside OneDrive.

## The library

| | Files | Size |
|---|---|---|
| JPEG (`.jpg`, `.jpeg`) | 45,789 | 174.5 GB |
| HEIC | 6,812 | 18.2 GB |
| Canon raw (`.cr2`) | 2,803 | 58.6 GB |
| Video (`.mov`, `.mp4`, `.avi`) | 1,679 | 111.0 GB |
| PNG, GIF, BMP, WebP, TIFF | 1,131 | 2.1 GB |
| Other (PSD, `.pi2`, `.thm`, PDF, HTML, `.exe`, `.zip`, AVIF, …) | 318 | 0.4 GB |
| **Total** | **58,532** in 484 folders | **340 GB** |

There were no paths over 260 characters, no non-ASCII or NFD names, no empty files, and 3 hidden system files (`desktop.ini`, `Thumbs.db`, ignored by name). Two top-level trees: `Pictures` (33,304 files) and `SkyDrive camera roll` (25,228).

## Read-only scan (`init --read-only`)

- **Nothing changed.** A stat snapshot of all 59,016 entries (size, mtime, attributes, folder times included) was identical before and after the scan. No marker, no cache file, nothing written inside the copy.
- **First scan: 51 minutes.** 58,282 files and 364 GB hashed (about 118 MB/s including ExifTool), 56,941 unique contents described, 0 errors. The 247 non-media files were recorded but not hashed, by design.
- **Rescan: 85 seconds,** hashing nothing.
- **Report: 27 seconds,** with 3,000 thumbnails extracted.

**Findings:**

- **Exact duplicates:** 1,078 groups, 2,351 files, 8.8 GB reclaimable. Same picture with different bytes: 331 groups.
- **Content hash coverage.** Every HEIC, CR2, MOV and PNG got an `ImageDataHash`. Missing: 56 GIF and 14 BMP (ExifTool doesn't hash those formats), 36 MP4 and 1 damaged MP4 (see below), and the JPEGs the first damage rule misjudged.
- **The damage rule (ph-meta/1) was far too eager:** 302 files flagged damaged. Checked by decoding with Pillow and by looking at the messages:
  - 206 phone JPEGs: "Bad format … for InteropIFD" / "possibly corrupted IFD". A broken metadata directory; **all 206 decode fine**.
  - 34 Samsung MP4s: "Unknown trailer with truncated … data". Extra data after the video; the video is intact.
  - 61 JPEGs: "JPEG format error". **55 decode fine** (53 of them simply have no end-of-image marker); **6 really are cut short**. ExifTool says the same thing for both kinds, and no cheap structural check tells them apart.
  - 1 MP4: "Truncated 'mdat' data". Really damaged.
- **Fix, ph-meta/2:**
  - Warnings about metadata structures (IFD, MakerNotes, trailer, XMP, IPTC, …) never count as damage.
  - "JPEG format error" alone makes a file *suspect* (a report warning) but not damaged.
  - Real truncation and read errors stay damaged.
  - The next scan re-described only the 1,578 contents whose stored facts the old rule could have misjudged.
  - Result: 1 damaged and 61 suspect.
- **Capture dates:** 3,988 JPEGs, 180 PNGs, all 98 AVIs and 139 MP4s have no embedded date. The filename/mtime fallbacks (deferred to per-instance views) will matter for phase 2.

## Suggestions on real folders

**Mostly right:**

- `Pictures/Fiona Senior` is an older copy of `Pictures/2026 Fiona Senior Portraits`, so removing the older copy was suggested.
- Identical Reya portrait folders: one kept.
- Identical `resources` folders of school-photo web pages: one kept.

**Questionable: curated selections.** `BestOf` (131 files), `Ward Summer 2016/Favs`, `Portraits Girls Feb 2017/best` and `home1/Self Portraits/AA Final` are picks from bigger folders, so every file in them "exists elsewhere" and they were suggested for removal. The bytes are safe and `content_folder_names` remembers the folder, but removing the folder loses the curation as a visible album. Open: should such names (best, favs, favorites, picks, select, final, star ratings) default to keep or undecided?

**Not yet handled:** within-folder copies in the camera roll (repeated uploads, Q-9): 23 folders, 72 extra copies.

## Full flow on the copy

| Step | Result |
|---|---|
| `scan --adopt-root` | Wrote the root marker; hashed nothing. |
| Accept 15 suggestions (contained, trees, fully redundant) | |
| `plan` | 533 files, 3.2 GB; 596 duplicate instances kept; 0 blocked; 0 conflicts. |
| `apply --execute` | 32 s: 533 moved (same-drive renames) to `PhotoHaven-DELETE`, all verified. |
| `verify --deep` | 11 s, 533 verified, kept copies re-hashed. |
| `purge` (dry run only) | 533 would be deleted after a byte compare; 0 refused. |
| `restore` | 533 restored; the rescan hashed nothing. |

After the round trip, all 58,532 original files have the same size, mtime and attributes, and none is missing. The only additions are the root marker and `PhotoHaven-DELETE` (README plus the batch manifest). 14 folder mtimes changed, from the moves out and back.
