"""Check that spec/ is self-consistent and agrees with docs/03-format-spec.md.

Usage (from the repo root):
    python -m pip install "jsonschema>=4.18"      # dev-only dependency
    python spec/check_spec.py

Checks:
  1. Every JSON Schema in spec/schemas is a valid draft 2020-12 schema.
  2. Every file in spec/examples validates against its schema. JSONL files also
     follow 03 section 1.1: UTF-8, no BOM, LF endings, a header first and only first,
     canonical JSON on every line (sorted keys, no spaces, non-ASCII unescaped),
     and the per-format ordering rules (cache records sorted by name key,
     log seq strictly increasing, record machine_id equal to the header's).
  3. Negative cases: mutations of valid records that the schemas MUST reject.
  4. spec/schema.sql equals the SQL block in docs/03 section 9, and loads into SQLite.
Exit status 0 when everything passes.
"""
from __future__ import annotations

import copy
import json
import pathlib
import re
import sqlite3
import sys
import tomllib
import unicodedata

try:
    import jsonschema
    from referencing import Registry, Resource
except ImportError:
    sys.exit('Needs the dev-only package jsonschema>=4.18:  python -m pip install "jsonschema>=4.18"')

SPEC = pathlib.Path(__file__).resolve().parent
REPO = SPEC.parent
SCHEMAS = SPEC / "schemas"
EXAMPLES = SPEC / "examples"
DOC = REPO / "docs" / "03-format-spec.md"

problems: list[str] = []


def fail(msg: str) -> None:
    problems.append(msg)
    print("  FAIL " + msg)


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def name_key(name: str) -> tuple[str, str]:
    return (unicodedata.normalize("NFC", name).casefold(), name)


# --- schemas -----------------------------------------------------------------

def load_schemas() -> tuple[dict[str, dict], Registry]:
    schemas, resources = {}, []
    for p in sorted(SCHEMAS.glob("*.schema.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(s)
        schemas[p.name] = s
        resources.append((s["$id"], Resource.from_contents(s)))
    return schemas, Registry().with_resources(resources)


def validator(schemas, registry, name):
    return jsonschema.Draft202012Validator(schemas[name], registry=registry,
                                           format_checker=jsonschema.FormatChecker())


def errors_of(v, obj) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in v.iter_errors(obj)]


# --- examples ----------------------------------------------------------------

def schema_for(rel: str) -> str | None:
    name = rel.rsplit("/", 1)[-1]
    rules = [
        (r"^library\.json$", "library.schema.json"),
        (r"^\.photohaven-root\.json$", "root-marker.schema.json"),
        (r"^machine\.json$", "machine.schema.json"),
        (r"^machines/[0-9a-f-]{36}\.json$", "machine-registry.schema.json"),
        (r"^library\.toml$", "library-toml.schema.json"),
        (r"^machine\.toml$", "machine-toml.schema.json"),
        (r"^\.photohaven-cache\.[0-9a-f]{12}\.jsonl$", "folder-cache.schema.json"),
        (r"^decisions\.[0-9a-f]{12}\.jsonl$", "decision-log.schema.json"),
        (r"^decisions-[0-9a-f-]{36}\.jsonl$", "decision-export.schema.json"),
        (r"^quarantine\.[0-9a-f]{12}\.jsonl$", "quarantine-log.schema.json"),
        (r"^manifest\.jsonl$", "quarantine-manifest.schema.json"),
    ]
    target = rel if rel.startswith("machines/") else name
    for pat, schema in rules:
        if re.match(pat, target):
            return schema
    return None


def check_jsonl(rel: str, raw: bytes, v) -> list[dict]:
    if raw.startswith(b"\xef\xbb\xbf"):
        fail(f"{rel}: has a BOM")
    if b"\r" in raw:
        fail(f"{rel}: has CR characters (LF line endings only)")
    if not raw.endswith(b"\n"):
        fail(f"{rel}: last line has no newline")
    lines = raw.decode("utf-8").split("\n")[:-1]
    recs = []
    for i, line in enumerate(lines, 1):
        obj = json.loads(line)
        recs.append(obj)
        if canon(obj) != line:
            fail(f"{rel}:{i}: not canonical JSON")
        for e in errors_of(v, obj):
            fail(f"{rel}:{i}: {e}")
        if (obj.get("record") == "header") != (i == 1):
            fail(f"{rel}:{i}: header must be the first line, and only the first")
    if not recs:
        fail(f"{rel}: empty")
        return recs
    header, body = recs[0], recs[1:]
    m = re.search(r"\.([0-9a-f]{12})\.jsonl$", rel)
    if m:
        if header.get("machine_id", "").replace("-", "")[:12] != m.group(1):
            fail(f"{rel}: file name machine_short does not match header machine_id")
    if header.get("kind") == "folder-cache":
        names = [r["name"] for r in body]
        if names != sorted(names, key=name_key):
            fail(f"{rel}: file records not sorted by casefold(NFC(name)), then raw name")
    seqs = [r["seq"] for r in body if "seq" in r]
    if seqs and any(b <= a for a, b in zip(seqs, seqs[1:])):
        fail(f"{rel}: seq not strictly increasing")
    for r in body:
        if "machine_id" in r and "machine_id" in header and r["machine_id"] != header["machine_id"]:
            fail(f"{rel}: record machine_id differs from header (one writer per file, 03 4)")
        if "batch_id" in header and r.get("batch_id") not in (None, header["batch_id"]):
            fail(f"{rel}: item batch_id differs from header")
        if "qpath" in r and not r["qpath"].endswith("/" + r["path"]):
            fail(f"{rel}: qpath does not end with the original relpath")
    return recs


def check_examples(schemas, registry) -> dict[str, list]:
    print("Examples")
    loaded = {}
    files = sorted(p for p in EXAMPLES.rglob("*") if p.is_file())
    covered = set()
    for p in files:
        rel = p.relative_to(EXAMPLES).as_posix()
        sname = schema_for(rel)
        if not sname:
            fail(f"{rel}: no schema mapping")
            continue
        covered.add(sname)
        v = validator(schemas, registry, sname)
        raw = p.read_bytes()
        if rel.endswith(".jsonl"):
            loaded[rel] = check_jsonl(rel, raw, v)
        else:
            obj = tomllib.loads(raw.decode("utf-8")) if rel.endswith(".toml") else json.loads(raw)
            loaded[rel] = [obj]
            for e in errors_of(v, obj):
                fail(f"{rel}: {e}")
        print(f"  ok   {rel}  ({sname})" if not any(rel in m for m in problems) else f"  ...  {rel}")
    missing = set(schemas) - covered - {"common.schema.json"}
    for m in sorted(missing):
        fail(f"no example for {m}")
    return loaded


# --- negative cases ----------------------------------------------------------

def check_negative(schemas, registry, loaded) -> None:
    print("Negative cases (each must be rejected)")
    cache = loaded[next(k for k in loaded if k.startswith(".photohaven-cache"))]
    dlog = loaded[next(k for k in loaded if re.match(r"decisions\.[0-9a-f]{12}", k))]
    qlog = loaded[next(k for k in loaded if k.startswith("quarantine."))]
    heic = next(r for r in cache if r.get("content_sha256"))
    damaged = next(r for r in cache if r.get("meta", {}).get("damaged"))
    skipped = next(r for r in cache if r.get("skip"))
    folder_dec = next(r for r in dlog if r.get("target", {}).get("kind") == "folder")
    file_dec = next(r for r in dlog if r.get("target", {}).get("kind") == "file")
    moved = next(r for r in qlog if r.get("event") == "moved")

    def mut(rec, fn):
        r = copy.deepcopy(rec)
        fn(r)
        return r

    cases = [
        ("folder-cache.schema.json", "uppercase sha256", mut(heic, lambda r: r.update(sha256=r["sha256"].upper()))),
        ("folder-cache.schema.json", "timestamp with 3 fractional digits", mut(heic, lambda r: r.update(observed_at="2026-11-10T03:20:40.902Z"))),
        ("folder-cache.schema.json", "damaged file with a content hash", mut(damaged, lambda r: r.update(content_sha256=heic["content_sha256"], content_hash_impl=heic["content_hash_impl"]))),
        ("folder-cache.schema.json", "skip set but sha256 present", mut(skipped, lambda r: r.update(sha256=heic["sha256"]))),
        ("folder-cache.schema.json", "no skip and null sha256", mut(heic, lambda r: (r.update(sha256=None), r.pop("content_sha256"), r.pop("content_hash_impl"), r.pop("meta"), r.pop("meta_impl")))),
        ("folder-cache.schema.json", "content hash without impl", mut(heic, lambda r: r.pop("content_hash_impl"))),
        ("folder-cache.schema.json", "explicit null for an optional field", mut(heic, lambda r: r.update(skip=None))),
        ("folder-cache.schema.json", "name with a slash", mut(heic, lambda r: r.update(name="sub/IMG.HEIC"))),
        ("folder-cache.schema.json", "damaged: false written out", mut(heic, lambda r: r["meta"].update(damaged=False))),
        ("folder-cache.schema.json", "orientation 9", mut(heic, lambda r: r["meta"].update(orientation=9))),
        ("folder-cache.schema.json", "unknown media kind", mut(heic, lambda r: r.update(media="photo"))),
        ("folder-cache.schema.json", "unknown field", mut(heic, lambda r: r.update(colour="red"))),
        ("decision-log.schema.json", "file decision without sha256", mut(file_dec, lambda r: r["target"].pop("sha256"))),
        ("decision-log.schema.json", "folder decision without scope", mut(folder_dec, lambda r: r["target"].pop("scope"))),
        ("decision-log.schema.json", "folder decision with sha256", mut(folder_dec, lambda r: r["target"].update(sha256=heic["sha256"], size=1))),
        ("decision-log.schema.json", "path with ..", mut(folder_dec, lambda r: r["target"].update(path="Backup 2019/../Pictures"))),
        ("decision-log.schema.json", "path with backslash", mut(folder_dec, lambda r: r["target"].update(path="Backup 2019\\DCIM"))),
        ("decision-log.schema.json", "path with trailing slash", mut(folder_dec, lambda r: r["target"].update(path="Backup 2019/"))),
        ("decision-log.schema.json", "id is UUIDv4, not v7", mut(folder_dec, lambda r: r.update(id="5b2a7c1e-8d4f-4a3b-9c6e-1f0d2e3a4b5c"))),
        ("decision-log.schema.json", "unknown action", mut(folder_dec, lambda r: r.update(action="delete"))),
        ("decision-log.schema.json", "seq 0", mut(folder_dec, lambda r: r.update(seq=0))),
        ("quarantine-log.schema.json", "moved event without keeper", mut(moved, lambda r: r.pop("keeper"))),
        ("quarantine-log.schema.json", "qpath outside a batch", mut(moved, lambda r: r.update(qpath="files/" + r["path"]))),
        ("quarantine-log.schema.json", "unknown event", mut(moved, lambda r: r.update(event="deleted"))),
        ("quarantine-manifest.schema.json", "manifest item that is not a move", mut(dict(moved, record="item"), lambda r: r.update(event="verified"))),
    ]
    for sname, label, obj in cases:
        if not errors_of(validator(schemas, registry, sname), obj):
            fail(f"accepted but must be rejected: {label} ({sname})")
        else:
            print(f"  ok   rejected: {label}")


# --- SQL -----------------------------------------------------------------------

def check_sql() -> None:
    print("SQL")
    doc = DOC.read_text(encoding="utf-8")
    sec = doc[doc.index("## 9. SQLite database"):doc.index("## 10. Changelog")]
    m = re.search(r"```sql\n(.*?)```", sec, re.S)
    doc_sql = m.group(1) if m else ""
    file_sql = (SPEC / "schema.sql").read_text(encoding="utf-8")
    body = file_sql[file_sql.find("PRAGMA user_version"):]   # drop the comment header
    if body != doc_sql:
        fail("spec/schema.sql differs from the SQL block in docs/03 section 9")
    else:
        print("  ok   schema.sql matches docs/03 section 9")
    con = sqlite3.connect(":memory:")
    con.execute("PRAGMA foreign_keys=ON")
    try:
        con.executescript(file_sql)
    except sqlite3.Error as e:
        fail(f"schema.sql does not load: {e}")
        return
    uv = con.execute("PRAGMA user_version").fetchone()[0]
    n = con.execute("SELECT count(*) FROM sqlite_master WHERE type IN ('table','view')").fetchone()[0]
    if uv != 1:
        fail(f"user_version is {uv}, expected 1")
    print(f"  ok   loads into SQLite {sqlite3.sqlite_version}: {n} tables and views, user_version {uv}")


def main() -> int:
    print("Schemas")
    schemas, registry = load_schemas()
    for n in schemas:
        print(f"  ok   {n}")
    loaded = check_examples(schemas, registry)
    check_negative(schemas, registry, loaded)
    check_sql()
    print()
    if problems:
        print(f"{len(problems)} problem(s)")
        return 1
    print("spec/ is consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
