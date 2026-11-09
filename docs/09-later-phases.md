# 09. Later phases

Outline-level designs for what comes after dedupe. Enough detail to keep phase-1 formats compatible with them; not yet enough to build from. Each phase gets its own detailed doc before work starts.

## Phase 2: Restructure

**Goal:** a new, uniform library built *alongside* the old one, with originals untouched until Rick is satisfied.

- **Target layout:** `<curated root>/<YYYY>/<YYYY-MM>[ <event>]/<file>`, for example `2015/2015-07 Hawaii trip/IMG_1234.HEIC`. Year and month come from `taken_local` ([03 §1.4](03-format-spec.md#14-capture-times)), falling back to `taken_utc`, then mtime (flagged). The event comes from the most descriptive folder name ([05 §7](05-file-relationships.md#7-folder-names-and-event-guesses)) or, once phase 3 exists, a confirmed event name.
- **Copy, don't move.** Restructure copies each keeper's content once into the curated tree (hash verified during the copy), and writes the destination's cache record directly, since the hash is known. Attachments travel with their primary: raw next to its JPEG, MOV next to its Live Photo still, AAE next to its original.
- **Name collisions** (two different `IMG_1234.JPG` in one month): append a short content-hash suffix, `IMG_1234~9f86d0.JPG`. Deterministic, so reruns produce the same names.
- **Metadata embedding happens here, on the copies only:** `XMP-et:OriginalImageHash`, `phv:IngestFileSHA256`, ratings and tags ([03 §8](03-format-spec.md#8-embedded-metadata-xmp)). HEIC and raw writes are tested on a temp copy first, with sidecar fallback.
- **Cutover:** once the curated tree is reviewed, the old tree's instances become redundant copies of curated ones and flow through the same quarantine → verify → purge machinery from phase 1. The camera roll stays as untouched intake.
- **Space:** during the transition the curated tree doubles the space used by keepers. Plan it per year, or per source, if OneDrive quota is tight.

## Phase 3: Events

- **Clustering (no model):** sort by `taken_utc` (or `taken_local`); start a new cluster at a time gap over a threshold (say 6 hours, tunable), or at a location jump over a threshold (say 50 km between consecutive GPS fixes). Merge clusters that share a non-generic folder name.
- **Naming (local model):** for each cluster, feed a local vision-language model the date range, a reverse-geocoded place name, the folder names in the cluster, and a few sample thumbnails. It proposes a name and a confidence. A 7B–8B quantized VLM fits in the Arc A770M's 16 GB, run through OpenVINO or an IPEX-LLM / llama.cpp SYCL backend.
- **Human approves or edits** in a report like phase 1's. Suggestions never overwrite confirmed names; confirmed names are decisions in a log (`phase = "events"`).
- **Reverse geocoding:** offline, from a GeoNames-style dataset, so no photo locations leave the machine.
- **Run only after dedupe,** when the library is smaller and the model's work is cheaper.

## Phase 4: Cull and rate

Tiered, cheapest first:

1. **Technical checks on everything (CPU):** Laplacian-variance blur score, exposure histogram (clipped highlights and shadows), near-black frames (thumb over lens, pocket shots). Computed on downscaled previews.
2. **Aesthetic score on survivors:** LAION aesthetic predictor on CLIP embeddings, or NIMA. MUSIQ is a technical-quality alternative. Choose when we get there. The CLIP embeddings are reusable for retrieval (phase 6).
3. **Local VLM only on borderline cases** and for picking the best shot of a burst ([05 §6](05-file-relationships.md#6-bursts-and-near-duplicates-parked)).

Culling **unique** photos is a different safety class from removing duplicates: there is no other copy. So cull decisions are always per file (never per folder), always shown with the image, and go through quarantine with a longer default hold period. Purge for culled files can't byte-compare against a keeper; it requires the file's decision to still be `cull` and the batch to be past its hold period.

Ratings live in the DB (decision log, `phase = "rate"`) and are written into curated copies at export as `XMP-xmp:Rating`.

## Phase 5: Browse (people, places, trips)

- **Immich** in Docker (it has an OpenVINO build for Intel GPUs) for face recognition across ages, map view, and albums. Pointed at the curated tree, read-only (external library).
- Face labels live in Immich's database: back it up. Long-term, export confirmed person names back into Photo Haven's DB (`XMP-iptcExt:PersonInImage`, MWG regions in curated copies) so they're not locked inside one app.
- OneDrive's built-in People grouping is a free bonus, not the main tool.

## Phase 6: Retrieve and tell stories

- Per-photo **embeddings** (CLIP-family, already computed in phase 4) plus a short **text description** per photo from the local VLM, stored in the DB (or a sidecar vector index such as sqlite-vec).
- **Query** = vector search over descriptions and embeddings, filtered by person, place, and date: "show me pictures of Fiona at the beach" → person = Fiona, scene ≈ beach.
- A chat-style interface on top. Later: story and album building, anniversary surfacing ("ten years ago this week"), shared family access.

## Ongoing intake

- The camera roll is an untouched intake. A periodic scan picks up new files (size/mtime against caches makes this cheap), hashes them, and routes them:
  - exact duplicate of a known content → nothing to do (or a file-level remove suggestion for repeated uploads),
  - new content → queued for restructure into the curated tree.
- Karen's library is registered as another root ([06 §5](06-multi-machine.md#5-scenarios) has the cross-account question).
- Memory cards and external drives can be added as `removable` roots for one-off imports; their files are copied into the library, then the card is treated as redundant.

## Productization path (if it has legs)

- **Compiled core** (Rust, or modern C++) reimplementing the scanner, hasher, caches, logs, and quarantine against [the format spec](03-format-spec.md). It can be adopted on an existing library with no migration, because the formats are the contract.
- **Desktop shell:** Tauri-style app, or a local web UI on the `serve` design ([07 §1](07-report-and-decisions.md#1-form)).
- **AI features** as an optional Python or ONNX sidecar process.
- Rebrand before any public release ([D-14](decisions.md#d-14-working-name-and-brand-neutral-identifiers)).
