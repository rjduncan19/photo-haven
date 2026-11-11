# Open questions

Questions that still need an answer, roughly in the order they block work. Move each one to [decisions.md](decisions.md) (or into the relevant doc) once it's answered.

## Blocking M0 or M1

| # | Question | Why it matters | Default if unanswered |
|---|---|---|---|
| Q-1 | Local path of the main photos folder on each machine (tomahawk, txuleta). | Root mapping and the root marker. | **Deferred by Rick:** all early work runs on a small test corpus ([spikes/m0](../spikes/m0/README.md)); the real paths come later. |
| Q-2 | ~~Quarantine location?~~ **Answered 2026-11-11:** a `PhotoHaven-DELETE` folder at the top of each root, inside OneDrive ([D-10](decisions.md#d-10-quarantine-location)). | | |
| Q-3 | ~~Where is ExifTool installed, and which version?~~ **Answered 2026-11-10:** ExifTool 13.61 (Oliver Betz build, via winget) at `%LOCALAPPDATA%\Programs\ExifTool\exiftool.exe` on tomahawk. | Minimum 12.62 for HEIC `ImageDataHash`. | Configured in `machine.toml`; `doctor` checks the version. |
| Q-4 | ~~Does OneDrive preserve mtimes exactly across machines?~~ **Answered 2026-11-11 (C2):** no, the other machine gets them truncated to whole seconds. The 1 s `mtime_tolerance_us` default is required. | | |
| Q-5 | ~~How does OneDrive react to moving many files out?~~ **Answered 2026-11-11 (C5):** it asks whether to delete them (200 files) and puts them in its recycle bin. Moot for apply now that the DELETE folder is inside OneDrive; it applies when the user empties that folder. | | |
| Q-6 | Where should the project home live? | Needs to be synced but outside every root. | `<OneDrive>\PhotoHaven-data\`. |

## Needed before M3 (apply)

| # | Question | Default if unanswered |
|---|---|---|
| Q-7 | Which machine is the apply machine? | The desktop (tomahawk), since it has the photos and the GPU. |
| Q-8 | Purge hold period: is 14 days right? | 14 days. |
| Q-9 | Should within-camera-roll duplicates (repeated uploads such as `IMG_1234 1.HEIC`) get automatic file-level suggestions? | Suggest, never auto-decide. |

## Needed later

| # | Question | Notes |
|---|---|---|
| Q-10 | Karen's library: share her Pictures folder into Rick's OneDrive, or run Photo Haven on her machine with the shared project home? | See [06 §5](06-multi-machine.md#5-scenarios). Affects whether cross-library dedupe works from one machine. |
| Q-11 | Personal tool or product? | Affects how early to invest in packaging, installer, and a GUI. Formats are product-ready either way. |
| Q-12 | Final name, before any public release. | [D-14](decisions.md#d-14-working-name-and-brand-neutral-identifiers). Data identifiers are already brand-neutral. |
| Q-13 | Curated library location: a new top-level folder in the same OneDrive, or a different one? | Phase 2. Watch OneDrive quota during the copy-then-cutover period. |
| Q-14 | Static report vs a localhost `serve` UI for v2? | [07 §1](07-report-and-decisions.md#1-form). |
| Q-15 | Quality and aesthetic models for culling. | Parked by choice; choose in phase 4. |

## Answered (moved out of the handoff's open questions)

- *Which ad size and shortlist for the yearbook:* handled separately; doesn't affect this tool.
