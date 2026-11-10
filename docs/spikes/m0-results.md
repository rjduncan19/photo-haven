# M0 spike results

*Fill in as the spikes in [spikes/m0](../../spikes/m0/README.md) are run. Each finding links back to the decision it confirms or changes.*

**Machines:** tomahawk (desktop) · txuleta (laptop). **ExifTool version:** … **Date:** …

## Test corpus

| Source kind | Files | Size | Notes |
|---|---|---|---|
| iPhone camera roll (HEIC + Live MOV) | | | |
| DSLR import (JPEG + raw) | | | raw formats: |
| Other (PNG, downloads, exports) | | | |

## A. Content hash (D-05, D-06)

| Type | n | PASS | FAIL | No hash | Write failed | phv read back | Same size after write | Image-hash MB/s |
|---|---|---|---|---|---|---|---|---|
| HEIC | | | | | | | | |
| JPEG | | | | | | | | |
| Raw (…) | | | | | | | | |
| MOV | | | | | | | | |
| PNG | | | | | | | | |

Truncated files: …
**Conclusion:** D-05 confirmed / changed because …

## B. Apple tags (03 §3.3, 05 §4)

| Purpose | Tag(s) found | In which types |
|---|---|---|
| Live Photo link | | |
| Burst | | |

Pairs by ID: … · by folder+name: … · disagreements: … · orphan stills/videos: …
**Conclusion:** …

## C. OneDrive

| Question | Result | Consequence |
|---|---|---|
| mtimes identical across machines? | buckets: … worst: … | `mtime_tolerance_us` = … |
| Hidden dotfile syncs and stays hidden? | | D-08 |
| Placeholder attributes when freed / pinned | | 04 "OneDrive placeholders" |
| Move inside OneDrive: excess upload ÷ moved | | |
| … files in web Recycle bin? | | |
| … version history kept? | | |
| … other machine re-downloads? | | |
| Move out of OneDrive: client prompt? | | |
| … files in web Recycle bin? | | |

**Conclusion on quarantine location (D-10):** …
