import datetime as dt
import os
import unicodedata

import pytest

from photohaven import util


def test_timestamps():
    t = util.format_ts(dt.datetime(2026, 11, 9, 22, 14, 35, 904318, tzinfo=dt.timezone.utc))
    assert t == "2026-11-09T22:14:35.904318Z"
    assert util.parse_ts(t).microsecond == 904318
    assert util.us_to_ts(0) == "1970-01-01T00:00:00.000000Z"
    with pytest.raises(ValueError):
        util.format_ts(dt.datetime(2026, 1, 1))


def test_uuid7_is_ordered_and_versioned():
    ids = [util.uuid7() for _ in range(5000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    for u in ids[:10]:
        assert u[14] == "7" and u[19] in "89ab"


def test_machine_short():
    assert util.machine_short("3f1c9a0b-22de-4f6a-8b7c-9d0e1f2a3b4c") == "3f1c9a0b22de"


def test_path_key_nfc_casefold():
    nfc = unicodedata.normalize("NFC", "Café.JPG")
    nfd = unicodedata.normalize("NFD", "Café.JPG")
    assert nfc != nfd
    assert util.path_key(nfc) == util.path_key(nfd) == "café.jpg"
    assert util.sort_key("B.jpg") > util.sort_key("a.jpg")


@pytest.mark.parametrize("bad", ["/a", "a/", "a//b", "a/./b", "../a", "a\\b"])
def test_relpath_rejects(bad):
    with pytest.raises(ValueError):
        util.validate_relpath(bad)


def test_relpath_ok():
    assert util.validate_relpath("") == ""
    assert util.validate_relpath("2015/Hawaii/IMG_1.JPG")
    assert util.join_rel("", "a") == "a" and util.join_rel("x/y", "a") == "x/y/a"
    assert util.split_rel("x/y/a.jpg") == ("x/y", "a.jpg") and util.split_rel("a.jpg") == ("", "a.jpg")


def test_ext_and_stem():
    assert util.ext_of("IMG_1.JPG") == "jpg"
    assert util.ext_of(".hidden") == "" and util.ext_of("noext") == ""
    assert util.stem_of("a.b.jpg") == "a.b"


@pytest.mark.skipif(os.name != "nt", reason="Windows long paths")
def test_long_path_windows():
    assert util.long_path("C:\\x\\y").startswith("\\\\?\\C:\\")
    assert util.long_path("\\\\server\\share\\a") == "\\\\?\\UNC\\server\\share\\a"
    assert util.plain_path(util.long_path("C:\\x")) == "C:\\x"


def test_atomic_write(tmp_path):
    p = tmp_path / "d" / "f.txt"
    util.atomic_write_text(p, "one")
    util.atomic_write_text(p, "two", hidden=True)
    assert p.read_text() == "two"
    assert not (tmp_path / "d" / "f.txt.tmp").exists()
    if os.name == "nt":
        assert os.stat(p).st_file_attributes & util.FILE_ATTRIBUTE_HIDDEN
