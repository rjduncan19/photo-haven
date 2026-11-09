# Photo Haven design docs

These docs expand the original planning handoff into a buildable design. They are written to be read in order the first time, then used as reference.

| # | Doc | Read it when |
|---|---|---|
| 01 | [Vision and scope](01-vision-and-scope.md) | You want the why, the goals, and what's out of scope |
| 02 | [Architecture](02-architecture.md) | You need the component map, storage locations, or CLI |
| 03 | [Format spec](03-format-spec.md) | You read or write *any* persisted data. **This is the contract.** |
| 04 | [Scanner](04-scanner.md) | You work on walking, hashing, metadata, or incremental rescans |
| 05 | [File relationships](05-file-relationships.md) | You work on duplicate groups, raw pairs, Live Photos, edits, bursts |
| 06 | [Multi-machine](06-multi-machine.md) | You touch caches, logs, merge rules, or DB rebuild |
| 07 | [Report and decisions](07-report-and-decisions.md) | You work on the HTML review or suggestion heuristics |
| 08 | [Apply, quarantine, verify](08-apply-quarantine-verify.md) | You touch anything that moves or deletes files |
| 09 | [Later phases](09-later-phases.md) | You plan restructure, events, culling, faces, retrieval |
| 10 | [Testing](10-testing.md) | You write tests or fixtures |
| — | [Decisions](decisions.md) | You want to know why something is the way it is |
| — | [Roadmap](roadmap.md) | You want to know what to build next |
| — | [Open questions](open-questions.md) | You hit something undecided |

Where these docs refine or depart from the original handoff, the change is called out with **Refinement:** and recorded in [decisions.md](decisions.md).

## Glossary

| Term | Meaning |
|---|---|
| **Library** | Everything Photo Haven manages, identified by a `library_id`. Contains one or more roots. |
| **Root** | A top-level folder tree in the library, such as Rick's OneDrive `Pictures` or Karen's OneDrive `Pictures`. Identified by a `root_id`; mapped to a local path separately on each machine. |
| **Relpath** | A file or folder path relative to its root, using forward slashes. |
| **Instance** | One file at one `(root_id, relpath)`. Several instances can hold identical bytes. |
| **Content** | The unique bytes, identified by `sha256`. A duplicate group is all instances that share one `sha256`. |
| **Content hash** | SHA-256 of the image data only, excluding metadata (`content_sha256`). Stays the same when ratings or tags are written into the file. |
| **Keeper** | The instance of a duplicate group that stays in the library. |
| **Quarantine** | A local folder outside OneDrive where removed instances are moved. Nothing leaves quarantine except by `restore` or a verified `purge`. |
| **Project home** | A synced folder (outside every root) that holds shared config and the append-only logs. |
| **Local state** | Per-machine, unsynced folder holding the SQLite DB, machine identity, thumbnails, and app logs. |
| **Folder cache** | A hidden JSONL file inside each library folder recording facts (size, mtime, hashes, metadata) about that folder's files. One file per machine. |
| **Decision** | A human choice (keep / remove / undecided) about a folder or file, stored as an append-only log record. |
| **Plan** | The concrete list of moves computed from decisions plus current state, shown as a dry run before apply. |
| **Intake** | A source that keeps receiving new files (the OneDrive camera roll). Protected from folder-level removal. |
