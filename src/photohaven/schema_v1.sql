-- Photo Haven database schema, spec version 1.
-- Must match docs/03-format-spec.md section 9 byte for byte below this header (checked by spec/check_spec.py).
-- Edit both together.

PRAGMA user_version = 1;

CREATE TABLE meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL                -- schema_version, library_id, created_at, last_rebuild_at
);

CREATE TABLE machines (
  machine_id    TEXT PRIMARY KEY,
  machine_short TEXT NOT NULL UNIQUE,
  name          TEXT,
  last_seen     TEXT
);

CREATE TABLE roots (
  root_id     TEXT PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  kind        TEXT NOT NULL,          -- onedrive | local | removable
  local_path  TEXT                    -- this machine's mapping; NULL if unmapped here
);

CREATE TABLE scans (
  scan_id        INTEGER PRIMARY KEY,
  machine_id     TEXT NOT NULL,
  started_at     TEXT NOT NULL,
  finished_at    TEXT,
  status         TEXT NOT NULL,       -- running | completed | interrupted | failed
  options_json   TEXT,
  folders_seen   INTEGER DEFAULT 0,
  files_seen     INTEGER DEFAULT 0,
  files_hashed   INTEGER DEFAULT 0,
  bytes_hashed   INTEGER DEFAULT 0,
  errors         INTEGER DEFAULT 0
);

CREATE TABLE folders (
  folder_id     INTEGER PRIMARY KEY,
  root_id       TEXT NOT NULL REFERENCES roots(root_id),
  relpath       TEXT NOT NULL,
  path_key      TEXT NOT NULL,
  parent_id     INTEGER REFERENCES folders(folder_id),
  depth         INTEGER NOT NULL,      -- root = 0
  name          TEXT NOT NULL,
  event_guess   TEXT,                  -- cleaned name, NULL if generic (05 §7)
  is_generic    INTEGER NOT NULL DEFAULT 0,
  is_intake     INTEGER NOT NULL DEFAULT 0,
  is_protected  INTEGER NOT NULL DEFAULT 0,
  source        TEXT,                  -- camera_roll | dslr_import | backup | other | <root name>
  present       INTEGER NOT NULL DEFAULT 1,
  last_scan_id  INTEGER REFERENCES scans(scan_id),
  UNIQUE (root_id, relpath)            -- raw path; path_key can collide (§1.6)
);
CREATE INDEX folders_path_key ON folders(root_id, path_key);

-- Every level of the folder path, so "anything under a folder named Hawaii" is one indexed query.
CREATE TABLE folder_levels (
  folder_id  INTEGER NOT NULL REFERENCES folders(folder_id),
  level      INTEGER NOT NULL,         -- 1 = top-level folder under the root
  name       TEXT NOT NULL,
  name_key   TEXT NOT NULL,            -- casefold(NFC(name))
  PRIMARY KEY (folder_id, level)
);
CREATE INDEX folder_levels_name ON folder_levels(name_key);

CREATE TABLE contents (
  sha256             TEXT PRIMARY KEY,
  size               INTEGER NOT NULL,
  media              TEXT NOT NULL,
  content_sha256     TEXT,
  content_hash_impl  TEXT,
  mime               TEXT,
  taken_local        TEXT,
  taken_offset       TEXT,
  taken_utc          TEXT,
  taken_source       TEXT,
  make               TEXT,
  model              TEXT,
  lens               TEXT,
  width              INTEGER,
  height             INTEGER,
  orientation        INTEGER,
  gps_lat            REAL,
  gps_lon            REAL,
  gps_alt            REAL,
  duration_ms        INTEGER,
  apple_content_id   TEXT,
  apple_burst_id     TEXT,
  xmp_original_image_hash TEXT,
  xmp_ingest_sha256  TEXT,
  damaged            INTEGER NOT NULL DEFAULT 0,   -- §8.1 "Damage"; content_sha256 is NULL when 1
  exiftool_messages  TEXT,                         -- JSON array of strings, NULL if none
  meta_impl          TEXT,
  first_seen         TEXT NOT NULL
);
CREATE INDEX contents_content_hash ON contents(content_sha256);
CREATE INDEX contents_taken ON contents(taken_local);
CREATE INDEX contents_apple_cid ON contents(apple_content_id);
CREATE INDEX contents_apple_burst ON contents(apple_burst_id);

CREATE TABLE files (
  file_id       INTEGER PRIMARY KEY,
  root_id       TEXT NOT NULL REFERENCES roots(root_id),
  folder_id     INTEGER NOT NULL REFERENCES folders(folder_id),
  relpath       TEXT NOT NULL,
  path_key      TEXT NOT NULL,
  name          TEXT NOT NULL,
  stem_key      TEXT NOT NULL,         -- casefold(NFC(name without last extension))
  ext           TEXT NOT NULL,         -- lowercase, no dot; '' if none
  media         TEXT NOT NULL,
  size          INTEGER NOT NULL,
  mtime_us      INTEGER NOT NULL,
  ctime_us      INTEGER,
  sha256        TEXT REFERENCES contents(sha256),   -- NULL until hashed
  hashed_at     TEXT,
  hashed_by     TEXT,                  -- machine_id whose facts were used
  state         TEXT NOT NULL,         -- present | cloud_only | unreadable | missing | quarantined | purged
  skip_reason   TEXT,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  last_scan_id  INTEGER REFERENCES scans(scan_id),
  UNIQUE (root_id, relpath)            -- raw path; path_key can collide (§1.6)
);
CREATE INDEX files_path_key ON files(root_id, path_key);
CREATE INDEX files_sha256 ON files(sha256);
CREATE INDEX files_size ON files(size);
CREATE INDEX files_folder ON files(folder_id);
CREATE INDEX files_stem ON files(folder_id, stem_key);

-- Every folder a content has ever lived in, kept even after the instance is quarantined or purged,
-- so the keeper inherits the most descriptive event name.
CREATE TABLE content_folder_names (
  sha256        TEXT NOT NULL REFERENCES contents(sha256),
  root_id       TEXT NOT NULL,
  folder_relpath TEXT NOT NULL,
  folder_name   TEXT NOT NULL,
  event_guess   TEXT,
  first_seen    TEXT NOT NULL,
  PRIMARY KEY (sha256, root_id, folder_relpath)
);

-- Relationships other than exact duplicates (exact duplicate group = shared sha256).
CREATE TABLE links (
  link_id     INTEGER PRIMARY KEY,
  kind        TEXT NOT NULL,           -- raw_pair | live_photo | edit | content_equiv | burst
  method      TEXT NOT NULL,           -- content_id | name_time | name | apple_burst | time_cluster | content_hash
  confidence  TEXT NOT NULL,           -- high | medium | low
  scan_id     INTEGER REFERENCES scans(scan_id),
  detail_json TEXT
);
CREATE TABLE link_members (
  link_id  INTEGER NOT NULL REFERENCES links(link_id) ON DELETE CASCADE,
  file_id  INTEGER NOT NULL REFERENCES files(file_id),
  role     TEXT NOT NULL,              -- primary | raw | still | video | original | edited | aae | member
  PRIMARY KEY (link_id, file_id)
);
CREATE INDEX link_members_file ON link_members(file_id);

-- Flags for orphans and anomalies (orphan Live Photo video, unreadable file, ...).
CREATE TABLE flags (
  file_id  INTEGER NOT NULL REFERENCES files(file_id),
  flag     TEXT NOT NULL,              -- damaged | exiftool_error | path_key_collision | cache_conflict | unreadable | zero_bytes | orphan_live_video | orphan_live_still | ...
  detail   TEXT,
  scan_id  INTEGER REFERENCES scans(scan_id),
  PRIMARY KEY (file_id, flag)
);

-- Materialized from all machines' decision logs.
CREATE TABLE decisions (
  id          TEXT PRIMARY KEY,
  at          TEXT NOT NULL,
  machine_id  TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  phase       TEXT NOT NULL,
  target_kind TEXT NOT NULL,           -- folder | file
  root_id     TEXT NOT NULL,
  path        TEXT NOT NULL,
  path_key    TEXT NOT NULL,
  scope       TEXT,                    -- folder | subtree (folders only)
  sha256      TEXT,                    -- files only
  size        INTEGER,
  action      TEXT NOT NULL,
  origin      TEXT NOT NULL,
  report_id   TEXT,
  note        TEXT
);
CREATE INDEX decisions_target ON decisions(phase, root_id, path_key);

-- Operational tables. Source of truth: quarantine log + manifests (rebuildable from them).
CREATE TABLE plans (
  plan_id        TEXT PRIMARY KEY,
  created_at     TEXT NOT NULL,
  scan_id        INTEGER REFERENCES scans(scan_id),
  decisions_digest TEXT NOT NULL,      -- sha256 of the sorted effective-decision set used
  summary_json   TEXT NOT NULL
);
CREATE TABLE plan_items (
  plan_id        TEXT NOT NULL REFERENCES plans(plan_id),
  seq            INTEGER NOT NULL,
  file_id        INTEGER NOT NULL REFERENCES files(file_id),
  sha256         TEXT NOT NULL,
  size           INTEGER NOT NULL,
  mtime_us       INTEGER NOT NULL,
  keeper_file_id INTEGER NOT NULL REFERENCES files(file_id),
  PRIMARY KEY (plan_id, seq)
);
CREATE TABLE batches (
  batch_id        TEXT PRIMARY KEY,
  plan_id         TEXT REFERENCES plans(plan_id),
  machine_id      TEXT NOT NULL,
  quarantine_root TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  state           TEXT NOT NULL        -- applying | applied | verified | partially_purged | purged | restored
);
CREATE TABLE quarantine_items (
  batch_id        TEXT NOT NULL REFERENCES batches(batch_id),
  root_id         TEXT NOT NULL,
  relpath         TEXT NOT NULL,
  sha256          TEXT NOT NULL,
  size            INTEGER NOT NULL,
  keeper_root_id  TEXT NOT NULL,
  keeper_relpath  TEXT NOT NULL,
  qpath           TEXT NOT NULL,
  state           TEXT NOT NULL,       -- moving | moved | verified | verify_failed | restored | purge_refused | purged
  moved_at        TEXT,
  verified_at     TEXT,
  purged_at       TEXT,
  last_error      TEXT,
  PRIMARY KEY (batch_id, root_id, relpath)
);

-- Flat "per file" view matching the handoff's field list.
CREATE VIEW file_view AS
SELECT f.*, c.content_sha256, c.taken_local, c.taken_offset, c.make, c.model, c.width, c.height,
       c.orientation, c.gps_lat, c.gps_lon, c.apple_content_id, c.apple_burst_id,
       fo.relpath AS folder_relpath, fo.event_guess, fo.is_generic, fo.source
FROM files f
JOIN folders fo ON fo.folder_id = f.folder_id
LEFT JOIN contents c ON c.sha256 = f.sha256;

CREATE VIEW dup_groups AS
SELECT sha256, MIN(size) AS size, COUNT(*) AS instances, (COUNT(*) - 1) * MIN(size) AS reclaimable
FROM files
WHERE state = 'present' AND sha256 IS NOT NULL
GROUP BY sha256
HAVING COUNT(*) > 1;
