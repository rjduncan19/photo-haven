# M0 spikes: runbook

Throwaway scripts that settle the assumptions the format spec rests on, using a small test set, before any product code exists. Stdlib-only Python 3.12+, plus ExifTool. Nothing here touches the real library: the corpus is a *copy*, and the OneDrive probe works only on files it creates itself.

| Script | Question it answers | Doc / decision |
|---|---|---|
| `check_env.py` | Is this machine ready (Python, ExifTool ≥ 12.62, long paths)? | 02 |
| `make_corpus.py` | Build the small test set (a copied sample, or synthetic JPEGs) | 10 §7 |
| `spike_content_hash.py` | **A.** Is ExifTool `ImageDataHash` unchanged by rating/XMP/EXIF writes, per format? Can our XMP fields be written and read back? | D-05, D-06 |
| `spike_apple_tags.py` | **B.** Exact tag names for Live Photo and burst IDs; how well does ID pairing agree with name pairing? | 03 §3.3, 05 §4 |
| `make_dupe_corpus.py` | Planted duplicate cases with known answers (`expected.json`), self-verified; rescan mutations. The acceptance set for M1/M2 | 04, 05, 10 |
| `onedrive_probe.py` | **C.** Are mtimes identical across machines? Do hidden dotfiles sync? What do placeholders look like? Is a move inside OneDrive a real move, or delete + re-upload? | 06 §3, D-08, D-10 |

Results go to `spike-results/<timestamp>-<name>/` under the current folder (git-ignored). They contain file names from your corpus, but no photos.

## 0. Setup (each machine)

1. Python 3.12+ (`py -3.12 --version`).
2. ExifTool for Windows from exiftool.org: unzip, rename `exiftool(-k).exe` to `exiftool.exe`, and keep the `exiftool_files` folder next to it. Put it on `PATH` or pass `--exiftool C:\path\exiftool.exe` to every script.
3. `git clone https://github.com/rjduncan19/photo-haven` and `cd photo-haven\spikes\m0`.
4. `py check_env.py --path D:\PH-test` (add any folder you plan to use). Fix any `FAIL`.

## 1. Build the test corpus

Pick a scratch location **outside OneDrive**, for example `D:\PH-test\corpus`. Then either:

```
py make_corpus.py --source "<a photo folder you choose>" --dest D:\PH-test\corpus --per-type 25
```

The source is only read. It samples up to 25 each of HEIC, JPEG, raw, video, and PNG, spread across folders, and pulls in companions (the MOV of a Live Photo, `IMG_E####` edits, `.AAE`, raw+JPEG partners). Cloud-only files are skipped. Run it more than once with different `--source` folders and the same `--dest` parent (for example `corpus\iphone`, `corpus\dslr`) to cover each kind of source. For spike B to be useful, include at least one camera-roll month with Live Photos and a burst or two.

Or, with no real photos at all: `py make_corpus.py --synthetic 50 --dest D:\PH-test\synthetic` (needs `pip install pillow`). Synthetic files are fine for spike C but tell us little for A and B.

## 2. Spike A: content hash

```
py spike_content_hash.py --corpus D:\PH-test\corpus
```

Per file, on a scratch copy: hash, write a rating plus `XMP-et:OriginalImageHash` plus `XMP-phv:IngestFileSHA256` in one write, re-rate, edit an EXIF field, truncate a copy. **What we want:** every type is all `PASS` (image-data hash stable, file hash changed). Also note:

- `phv_ok` below `n` → our custom namespace config didn't take; tell me the `write1_msg` from `content_hash.csv`.
- `WFAIL` → ExifTool couldn't write that format (expected for some raws). Those types need sidecar `.xmp` files for ratings.
- `same-size` above 0 → a metadata write left the file size unchanged, so the cache can't treat "same size" as "same file" after we start writing ratings.
- `idh MB/s` → content-hash throughput, for the full-scan time estimate.

## 2b. Duplicate-case corpus (for M1/M2)

```
py make_dupe_corpus.py build  --source D:\PH-test\corpus --dest D:\PH-test\dupecases
py make_dupe_corpus.py verify --dest D:\PH-test\dupecases
```

Plants about 57 files under `dupecases\library`, built from 7 base photos (camera JPEGs, an iPhone JPEG, HEICs, a MOV, a PNG). There are exact copies (renamed, moved, `- Copy`, ` (1)`, extension case and spelling, new mtime, fresh times, hidden, read-only, NFC/NFD names, a path over 260 characters, fully and partly redundant backup folders, a photo whose only copies are in backups). There are also content-equivalent edits (rating, rating with mtime kept, keywords, date shift, orientation, GPS, stripped metadata) and different or damaged files (one byte flipped at the same size, a name collision, truncated, zero bytes, a stray `.txt`). `expected.json` holds the answer for every file. `verify` recomputes SHA-256, ImageDataHash and damage, and checks them against it.

For rescan tests, after the product has scanned the library once:

```
py make_dupe_corpus.py mutate --dest D:\PH-test\dupecases
py make_dupe_corpus.py verify --dest D:\PH-test\dupecases
```

This moves, renames, touches, rates in place, silently corrupts (the size and mtime stay the same), deletes and adds files. It also records an `expect_rescan` hint for each changed file. Mutate runs only once; to start over, build into a new folder (the read-only file needs its attribute cleared before you delete the old one).

## 3. Spike B: Apple tags

```
py spike_apple_tags.py --corpus D:\PH-test\corpus
```

Read-only. Prints every identifier-like tag found per file type (for example `Apple:ContentIdentifier` vs `MakerNotes:ContentIdentifier` vs `Keys:ContentIdentifier`), Live Photo pairs by ID versus by folder+name, orphans, and burst groups. `summary.json` lists the disagreements.

## 4. Spike C: OneDrive behavior (two machines)

Use a scratch folder inside OneDrive that isn't in Pictures, for example `%OneDrive%\PH-probe-area`. Let OneDrive finish syncing between steps (tray icon shows up to date).

**C0. Background noise.** On the machine that will do the moves: `py onedrive_probe.py watch-upload --seconds 120`. Note the upload rate while idle.

**C1. Create the probe (tomahawk).**
```
py onedrive_probe.py prepare "%OneDrive%\PH-probe-area" --count 200 --size-kb 1024
```
200 MB of random files with distinct sub-second mtimes, plus a hidden `.photohaven-cache.probe.jsonl`. Wait for sync, then:
```
py onedrive_probe.py snapshot "%OneDrive%\PH-probe-area\PhotoHaven-probe-<id>" --label tomahawk
```

**C2. Second machine (txuleta).** After it syncs, take a snapshot there (`--label txuleta`). Copy both snapshot JSONs to one machine and compare them:
```
py onedrive_probe.py compare snapshot-<id>-tomahawk.json snapshot-<id>-txuleta.json
```
This answers: are mtimes identical (sets `mtime_tolerance_us`)? Did the dotfile sync, and is it still hidden? Are the files placeholders on txuleta?

**C3. Placeholder attributes (txuleta).** Right-click the probe folder → *Free up space*, snapshot again with `--label txuleta-freed`, and note `attr_names` in the JSON. Then *Always keep on this device*, snapshot again (`--label txuleta-pinned`).

**C4. Move inside OneDrive (tomahawk).** This is the quarantine-in-OneDrive question.
```
py onedrive_probe.py move "<probe>" --to "%OneDrive%\PH-quarantine-test\q1" --watch 300
```
Then check, and write down:
- the script's verdict (excess upload ÷ moved bytes; a true move should be near 0),
- **OneDrive on the web → Recycle bin**: do the probe files appear there? (A true move leaves nothing; delete + add does.)
- On the web, open a moved file → *Version history*: is it the same item (history kept)?
- On txuleta: do the files show up in `q1` without being downloaded again (placeholders stay placeholders)?

Then `py onedrive_probe.py move-back "<probe>"`.

**C5. Move outside OneDrive (tomahawk).** The handoff's original quarantine design, for comparison:
```
py onedrive_probe.py move "<probe>" --to D:\PH-test\quarantine-out --watch 120
```
Note any OneDrive notification about deleted files and what it offers, and whether the files land in the web Recycle bin. Then `move-back` (expect a full re-upload this time).

**C6. Optional repeat with real photos:** `prepare ... --from-corpus D:\PH-test\corpus` and redo C4, in case OneDrive treats photo files differently (thumbnails, People processing).

**Cleanup:** `py onedrive_probe.py cleanup "<probe>"`, and empty the test folders from the web Recycle bin if you like.

## 5. Report back

Fill in [`docs/spikes/m0-results.md`](../../docs/spikes/m0-results.md), or just send me the `summary.json`, `compare-*.json`, and `move-*.json` files plus your notes from C4 and C5, and I'll write it up and update the spec.
