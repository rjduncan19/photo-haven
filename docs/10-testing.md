# 10. Testing

The tests have one job above all others: prove that no sequence of commands, crashes, or concurrent machines can lose a photo.

## 1. Fixtures

**Never commit real family photos.** All fixtures are synthetic or public domain.

- **Generated at test time** (`tests/fixtures/gen.py`): Pillow creates small JPEGs with controlled EXIF (dates, offsets, camera model, GPS, orientation); pillow-heif creates HEICs. Each image draws its own index as text so a failing test is easy to inspect.
- **Library builder:** a declarative description of a library becomes a temp directory tree:

  ```python
  lib = build_library(tmp_path, {
      "Camera Roll/2015/07": ["a.heic", "b.heic", live("c", still="heic")],
      "Backup 2019/DCIM":    [copy_of("a.heic"), copy_of("b.heic"), "unique.jpg"],
      "Import 2016-03":      [pair("DSC_0042", raw="nef")],
      "Import 2016-03 (1)":  [copy_of("Import 2016-03/DSC_0042.jpg")],
  })
  ```

- **Apple metadata fixtures** (ContentIdentifier, BurstUUID in maker notes) can't be produced by Pillow. Options: small public-domain sample files under `tests/fixtures/public/` with their license recorded; or files written with ExifTool from a fixture template. Decide in M1.
- **Content-hash conformance fixtures** (`spec/fixtures/content-hash/`): pairs of files that differ only in metadata, with expected `sha256` and `content_sha256`. Every implementation must reproduce them.

## 2. Test layers

| Layer | What | Examples |
|---|---|---|
| Unit | Pure functions | Path normalization (NFC, casefold, separators); timestamp formatting; UUIDv7 ordering; folder-name cleanup table ([05 §7](05-file-relationships.md#7-folder-names-and-event-guesses)); date parsing priority; suggestion engine scenarios ([07 §4](07-report-and-decisions.md#4-suggestions)); decision resolution ([07 §5](07-report-and-decisions.md#5-resolving-effective-decisions)) |
| Format | Read/write round trips; JSON Schema validation | Every record type round-trips byte-identically through canonical JSON; unknown fields ignored on read; newer `schema` rejected; truncated last log line ignored |
| Component | One module against a fixture library | Scanner on a fixture tree; ExifTool bridge (real ExifTool); link pass finds every planted relationship |
| Flow | Whole commands, end to end | init → scan → report → (simulated export) → plan → apply → verify → purge, asserting the final library and quarantine |
| Property | Randomized libraries and decisions (hypothesis) | For any library and any decision set: after apply, every `sha256` present before is still present in the library at least once |

## 3. Safety tests (required, one per invariant)

| Invariant | Test |
|---|---|
| I-1 no deletes in roots | Patch `os.remove`/`unlink`/`rmtree` to fail if the path is inside a root (except the cross-volume move's final step, which is checked separately); run the full flow. |
| I-2 last copy | Mark every folder `remove`; assert one instance of each group remains and conflicts are reported. Property test over random decisions. |
| I-3 keepers not quarantined | Two simulated machines' quarantine logs plan opposite removals; assert the planner refuses. |
| I-4 match before move | Modify a source or keeper between plan and apply; assert skip-and-report. |
| I-5 byte compare before purge | Corrupt one byte of a keeper (same size, mtime restored); assert purge refuses that item. Delete a keeper; assert refusal. |
| I-6 restore from manifest | Delete the DB and project home; restore a batch using only the manifest. |

## 4. Crash injection

The move protocol ([08 §4](08-apply-quarantine-verify.md#4-the-move-protocol)) is instrumented with named crash points (`after_partial_write`, `after_rename_partial`, `after_source_delete`, `before_journal`). A test harness runs apply in a subprocess, kills it at each crash point, reruns apply, and asserts:

- every content is present in the library or quarantine,
- no `.partial` files remain,
- the DB, manifest, and log agree,
- a second clean run produces no further changes.

Every row of the recovery table in [08 §5](08-apply-quarantine-verify.md#5-journal-and-crash-recovery) has a test.

## 5. Multi-machine simulation

Two (or three) machine identities in separate local-state dirs share one fixture library and project home.

- Both scan → each writes its own cache file; facts are identical (compare records excluding provenance).
- Machine B scans after A → B hashes nothing (all facts reused).
- Facts from A with mtime shifted by 0.5 s → reused under default tolerance; rehashed with tolerance 0.
- Conflicting `sha256` in two caches for one file → rehash and `cache_conflict` flag.
- Decisions on A, plan on B → B's plan includes A's decisions.
- Non-apply machine runs `apply --execute` → refused.
- `rebuild-db` on each machine → DB equals a freshly scanned DB (excluding scan bookkeeping).
- A conflict-copy cache file name (`…-PCNAME.jsonl`) → read as a source and reported by `doctor`.

## 6. Windows-specific

CI runs on `windows-latest` as well as Linux. Windows-only tests cover: hidden attribute on cache files; long paths over 260 characters; case-only renames; placeholder attribute handling (simulated by patching `st_file_attributes`, since CI has no OneDrive); same-volume vs cross-volume moves (temp dirs on two drives where the runner allows, otherwise simulated).

ExifTool is installed in CI from the official Windows zip / Linux package at a pinned version.

## 7. Before touching the real library

A manual checklist, kept in the roadmap's M5:

1. `doctor` passes on the machine.
2. Run the whole flow on a disposable copy of 3–5 real folders (including the backup folder's smallest subfolder and one camera-roll month), with the quarantine on a scratch drive.
3. First real scan with `--no-folder-cache` (read-only).
4. Review the report; apply one small batch (`--batch-limit 200`); verify; check OneDrive's behavior; restore it; apply again.
5. Only then work in larger rounds.
