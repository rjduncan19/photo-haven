# CLAUDE.md: guidance for working in this repo

Photo Haven organizes a large family photo library (about 300–400 GB, 20+ years, JPEG/HEIC/raw, synced through OneDrive on Windows). Phase 1 is exact-duplicate removal with quarantine and verification. Read `docs/README.md` for the map of the design.

## Ground rules (non-negotiable)

These protect irreplaceable family photos. Any code that could violate them is a bug, even if a test passes.

1. **Never delete a library file.** Removal is always a *move to quarantine*. Only `purge` deletes, only from quarantine, and only after a fresh byte-for-byte compare against a surviving keeper (docs/08).
2. **Last-copy invariant.** Never quarantine a file unless a byte-identical copy (same SHA-256 *and* byte compare at purge) remains in the library under a non-remove decision. If every instance is marked remove, keep one and report the conflict.
3. **Never modify originals, except in the rating step.** Dedupe writes nothing inside a library root but its own hidden cache files (`.photohaven-cache.*.jsonl`) and the root marker. No EXIF/XMP writes, renames, or touch-ups to photos. The only code allowed to write into an original is the gated rating flush (docs/09, "Rating step"): settled contents only, one write per file, verified by `ImageDataHash` afterward.
4. **Don't trigger mass downloads.** Skip OneDrive cloud-only placeholders when hashing; record them as `cloud_only` (docs/04).
5. **Every destructive step has a dry run** that prints exactly what will happen, and acts only on explicit confirmation.
6. **Every action carries its expectation.** A move records the expected SHA-256 and size. If the file on disk no longer matches, skip it and report; never "do it anyway."

## The contract

`docs/03-format-spec.md` is authoritative for every on-disk format: SQLite schema, JSONL cache/decision/quarantine records, XMP fields, timestamp/path/hash conventions. If code and spec disagree, fix the code or propose a spec change with a schema version bump. Do not invent fields ad hoc. `spec/` holds the machine-checkable contract: `schema.sql` (identical to 03 §9), JSON Schemas in `spec/schemas/`, one example per format in `spec/examples/`. After any change to 03 or `spec/`, run `python spec/check_spec.py` (needs the dev-only `jsonschema>=4.18`; a `.venv` works).

## Conventions (summary; spec has the details)

- Timestamps: UTC, `YYYY-MM-DDTHH:MM:SS.ffffffZ`.
- Hashes: lowercase hex, no prefix, in fields whose name says the algorithm (`sha256`, `content_sha256`).
- Paths: `(root_id, relpath)`, forward slashes, relative to the root, case preserved; compare using NFC + casefold.
- IDs: UUIDv4 for library/root/machine; UUIDv7 for log records.
- Each file carries a `schema` version.

## Environment

- Target OS is **Windows** (NTFS, OneDrive Files On-Demand, long paths). Code must work there first; Linux/macOS are for development and CI only.
- Python 3.12+. Keep runtime dependencies minimal (stdlib preferred). ExifTool is an external executable invoked in `-stay_open` mode.
- GPU work (later phases) targets Intel Arc via OpenVINO / PyTorch XPU. No CUDA.

## Testing

- Never commit real family photos. Fixtures are synthetic (generated with Pillow) or small public-domain samples.
- Safety invariants (rules 1–6) have dedicated tests, including crash injection during apply and a simulation of two machines scanning the same folder (docs/10).
- Run the full flow on a disposable *copy* of a few folders before ever pointing at the real library.

## Layout (planned)

```
src/photohaven/      package (see docs/02-architecture.md for modules)
spec/                schema.sql, JSON Schemas, example records
docs/                design docs
tests/               pytest suite and fixture generators
```
