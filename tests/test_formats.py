import json
import pathlib

import pytest

from photohaven import formats
from photohaven.formats import CacheHeader, FileFacts, canonical

from conftest import REPO

EXAMPLES = REPO / "spec" / "examples"


def jsonl_examples():
    return sorted(p for p in EXAMPLES.rglob("*.jsonl"))


@pytest.mark.parametrize("path", jsonl_examples(), ids=lambda p: p.name)
def test_examples_round_trip_byte_identical(path: pathlib.Path):
    raw = path.read_bytes()
    lines = raw.decode("utf-8").split("\n")[:-1]
    assert "".join(canonical(json.loads(l)) + "\n" for l in lines).encode() == raw


def test_cache_example_reads_and_rewrites_identically():
    p = next(EXAMPLES.glob(".photohaven-cache.*.jsonl"))
    c = formats.read_cache(p)
    assert c.header.machine_id.startswith("3f1c9a0b")
    assert formats.cache_bytes(c.header, c.records) == p.read_bytes()


def test_canonical_numbers():
    assert canonical({"b": 1, "a": 2.5}) == '{"a":2.5,"b":1}'
    assert canonical(59.0) == "59"
    assert canonical(1e-05) == "0.00001"
    assert canonical(-123.184013888889) == "-123.184013888889"
    assert canonical(1e20) == "100000000000000000000"
    assert canonical("Café") == '"Café"'
    with pytest.raises(ValueError):
        canonical(float("nan"))


def _facts(**kw):
    d = dict(name="a.jpg", size=3, mtime_us=10, media="image", sha256="0" * 64,
             observed_at="2026-11-10T00:00:00.000000Z")
    d.update(kw)
    return FileFacts(**d)


def test_absent_not_null_and_false_booleans():
    r = _facts(meta={"damaged": False, "make": None, "model": "X"}, meta_impl="exiftool/1;ph-meta/1").to_record()
    assert r["meta"] == {"model": "X"}
    assert "content_sha256" not in r and "skip" not in r and "ctime_us" not in r
    s = _facts(sha256=None, skip="cloud_only").to_record()
    assert s["sha256"] is None and s["skip"] == "cloud_only"


def test_facts_ignore_provenance():
    a = _facts()
    b = _facts(observed_at="2027-01-01T00:00:00.000000Z")
    assert formats.facts_equal([a], [b])
    assert not formats.facts_equal([a], [_facts(size=4)])


def test_unknown_fields_preserved():
    rec = _facts().to_record() | {"future_field": 7}
    f = FileFacts.from_record(rec)
    assert f.to_record()["future_field"] == 7


def test_sort_order():
    h = CacheHeader("l", "r", "", "m", "2026-11-10T00:00:00.000000Z", "photohaven/0.1.0")
    data = formats.cache_bytes(h, [_facts(name="b.jpg"), _facts(name="A.jpg"), _facts(name="a.jpg")])
    names = [json.loads(l).get("name") for l in data.decode().splitlines()[1:]]
    assert names == ["A.jpg", "a.jpg", "b.jpg"]


def test_newer_schema_refused(tmp_path):
    p = tmp_path / ".photohaven-cache.000000000000.jsonl"
    p.write_text(canonical({"record": "header", "schema": 2, "kind": "folder-cache"}) + "\n")
    with pytest.raises(formats.NewerSchemaError):
        formats.read_cache(p)


def test_truncated_last_line_ignored(tmp_path):
    p = tmp_path / "decisions.000000000000.jsonl"
    p.write_text(canonical({"record": "header", "schema": 1, "kind": "decision-log"}) + "\n"
                 + canonical({"record": "decision", "seq": 1}) + "\n" + '{"record":"deci')
    j = formats.read_jsonl(p, "decision-log")
    assert len(j.records) == 1 and j.warnings


def test_bad_middle_line_is_an_error(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text(canonical({"record": "header", "schema": 1, "kind": "k"}) + "\n{bad\n" + canonical({"a": 1}) + "\n")
    with pytest.raises(formats.FormatError):
        formats.read_jsonl(p, "k")


def test_cache_file_names():
    assert formats.is_cache_file_name(".photohaven-cache.3f1c9a0b22de.jsonl")
    assert formats.is_cache_file_name(".photohaven-cache.3f1c9a0b22de-TOMAHAWK.jsonl")
    assert not formats.is_cache_file_name(".photohaven-root.json")
