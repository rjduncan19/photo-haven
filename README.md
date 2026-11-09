# Photo Haven

> **Bring it all in, lose nothing, find anything.**

Photo Haven is a tool for bringing order to a large family photo library that has never been organized: 20+ years of JPEG, iOS HEIC, a few raw files, repeated imports, and backup folders full of duplicates. It finds duplicates, moves redundant copies into a verified quarantine, and later organizes what's left by date, event, people, and place.

*Photo Haven is a working name. See [docs/decisions.md](docs/decisions.md#d-14-working-name-and-brand-neutral-identifiers).*

## Principles

- **Safe.** Nothing is ever deleted first. Photo Haven scans and reports, you review, and only then does it act. "Remove" means *move to quarantine*. Before anything is deleted for good, a byte-for-byte comparison proves that an identical copy still exists.
- **Easy.** Point it at the pile of folders and it does the heavy lifting. You make decisions per folder ("this whole backup folder is redundant"), not per file.
- **Intelligent.** In later phases it organizes photos by people, places, and events, and eventually you can search by asking ("pictures of Fiona at the beach").

## Status

**Design phase.** Nothing is implemented yet. The design docs are the current deliverable, and the [format spec](docs/03-format-spec.md) is the contract that every implementation must follow.

## Documentation

Start with the [docs index](docs/README.md). The key docs:

| Doc | What it covers |
|---|---|
| [Vision and scope](docs/01-vision-and-scope.md) | Goals, non-goals, phases |
| [Architecture](docs/02-architecture.md) | Components, storage locations, data flow, CLI |
| [Format spec](docs/03-format-spec.md) | **The contract:** SQLite schema, JSONL records, XMP fields, conventions |
| [Scanner](docs/04-scanner.md) | Walk, hash, metadata, incremental rescans |
| [File relationships](docs/05-file-relationships.md) | Duplicates, raw+JPEG pairs, Live Photos, edits, bursts |
| [Multi-machine](docs/06-multi-machine.md) | Per-machine caches, merge rules, decision logs |
| [Report and decisions](docs/07-report-and-decisions.md) | The folder-centric HTML review |
| [Apply, quarantine, verify](docs/08-apply-quarantine-verify.md) | Safe removal and permanent delete |
| [Later phases](docs/09-later-phases.md) | Restructure, events, culling, faces, retrieval |
| [Testing](docs/10-testing.md) | Fixtures, safety invariants, multi-machine simulation |
| [Decisions](docs/decisions.md) | Design decision log with rationale |
| [Roadmap](docs/roadmap.md) | Milestones and acceptance criteria |
| [Open questions](docs/open-questions.md) | What still needs answers |

## Planned stack

Python 3.12+ prototype on Windows, with [ExifTool](https://exiftool.org/) for metadata and SQLite for the working database. If it proves out, a compiled core (Rust or modern C++) may replace the Python implementation of the same documented formats.
