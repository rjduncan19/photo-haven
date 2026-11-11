import pathlib

import pytest

from photohaven import app
from photohaven.config import LibrarySettings
from photohaven.link import clean_folder_name
from photohaven.scan import ScanOptions

from fixtures.gen import build_library, copy_of, jpeg_bytes, photo, raw_bytes

SETTINGS = LibrarySettings.load(pathlib.Path("does-not-exist"))


@pytest.mark.parametrize("name,guess", [
    ("2015-07-04 Hawaii trip", "Hawaii trip"),           # 05 §7 table
    ("20180812_Fiona_soccer_tournament", "Fiona soccer tournament"),
    ("Backup 2019", None),
    ("100CANON", None),
    ("Christmas 2012 (1)", "Christmas 2012"),
    ("Backup 2026 (phone)", None),
    ("My Pictures", None),
    ("2026-10", None),
    ("July 2015 Lake house", "Lake house"),
    ("Camping - Copy", "Camping"),
    ("DSC 4412 Beach", "Beach"),
    ("Old Laptop", "Old Laptop"),
])
def test_clean_folder_name(name, guess):
    assert clean_folder_name(name, SETTINGS)[0] == guess


def test_links_content_equiv_and_edits(lib, fake):
    env, root, _ = lib
    made = build_library(root, {"a": ["IMG_1234.jpg", photo("IMG_E1234.jpg", index=50), raw_bytes("IMG_1234.AAE", b"<x/>")]})
    # same pixels, different metadata: the fake describer hashes bytes after the first 64
    data = bytearray(made["a/IMG_1234.jpg"].read_bytes())
    data[30] ^= 1
    (root / "b").mkdir()
    (root / "b" / "IMG_1234 edited meta.jpg").write_bytes(bytes(data))
    app.scan(env, ScanOptions(), describer=fake)
    kinds = {(r[0], r[1]) for r in env.conn.execute("SELECT l.kind, m.role FROM links l JOIN link_members m USING(link_id)")}
    assert ("content_equiv", "member") in kinds
    assert ("edit", "edited") in kinds and ("edit", "original") in kinds and ("edit", "aae") in kinds
    ev = dict(env.conn.execute("SELECT relpath, event_guess FROM folders").fetchall())
    assert ev["a"] is None or ev["a"] == "a"
