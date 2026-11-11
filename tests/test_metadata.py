from photohaven.scan.metadata import classify, normalize, parse_date, stale

HASH = "ab" * 32


def test_iphone_heic():
    d = normalize({
        "SourceFile": "x.HEIC",
        "EXIF:ExifIFD:DateTimeOriginal": "2026:10:29 16:23:09",
        "Composite:SubSecDateTimeOriginal": "2026:10:29 16:23:09.144-07:00",
        "EXIF:ExifIFD:OffsetTimeOriginal": "-07:00",
        "EXIF:IFD0:Make": "Apple", "EXIF:IFD0:Model": "iPhone 17",
        "Composite:ImageSize": "5712 4284", "EXIF:IFD0:Orientation": 6,
        "EXIF:GPS:Copy1:GPSLatitude": 47.66, "Composite:GPSLatitude": 47.66,
        "Composite:GPSLongitude": -122.34, "Composite:GPSAltitude": 59.4,
        "File:MIMEType": "image/heic", "File:ImageDataHash": HASH.upper(),
    })
    m = d.meta
    assert m["taken_local"] == "2026-10-29T16:23:09.144"
    assert m["taken_offset"] == "-07:00"
    assert m["taken_utc"] == "2026-10-29T23:23:09.144000Z"
    assert m["taken_source"] == "exif_original"
    assert (m["width"], m["height"], m["orientation"]) == (5712, 4284, 6)
    assert m["gps_lon"] == -122.34
    assert d.content_sha256 == HASH and not d.damaged


def test_offset_from_separate_tag():
    m = normalize({"EXIF:ExifIFD:DateTimeOriginal": "2015:07:04 13:45:12",
                   "EXIF:ExifIFD:OffsetTimeOriginal": "-10:00"}).meta
    assert m["taken_local"] == "2015-07-04T13:45:12" and m["taken_offset"] == "-10:00"
    assert m["taken_utc"] == "2015-07-04T23:45:12.000000Z"


def test_quicktime():
    m = normalize({"QuickTime:CreateDate": "2026:10:12 23:54:05",
                   "QuickTime:Keys:CreationDate": "2026:10:12 16:54:05-07:00",
                   "QuickTime:Duration": 18.565, "QuickTime:Keys:Make": "Apple"}).meta
    assert m["taken_source"] == "quicktime_creationdate"
    assert m["taken_utc"] == "2026-10-12T23:54:05.000000Z"
    assert m["duration_ms"] == 18565 and m["make"] == "Apple"
    m2 = normalize({"QuickTime:CreateDate": "2026:10:12 23:54:05"}).meta
    assert m2["taken_source"] == "quicktime_createdate" and "taken_offset" not in m2
    assert normalize({"QuickTime:CreateDate": "0000:00:00 00:00:00"}).meta == {}


def test_damage_rules():
    # a [minor] first warning must not hide a later one (03 §8.1)
    d = normalize({"ExifTool:ExifTool:Warning": "[minor] Skipped unknown 1 bytes after JPEG APP1 segment",
                   "ExifTool:ExifTool:Copy1:Warning": "Truncated 'mdat' data at offset 0x1234",
                   "File:ImageDataHash": HASH})
    assert d.damaged and d.content_sha256 is None
    assert d.meta["damaged"] is True and d.meta["exiftool_messages"] == ["Truncated 'mdat' data at offset 0x1234"]
    # ph-meta/2: "JPEG format error" alone is suspect, not damaged (55 of 61 decoded fine in the real library)
    j = normalize({"ExifTool:ExifTool:Warning": "JPEG format error", "File:ImageDataHash": HASH})
    assert not j.damaged and j.content_sha256 == HASH
    assert classify(j.meta["exiftool_messages"], False) == (False, True)
    s = normalize({"ExifTool:ExifTool:Warning": "IPTCDigest is not current", "File:ImageDataHash": HASH})
    assert not s.damaged and s.content_sha256 == HASH and s.meta["exiftool_messages"]
    e = normalize({"ExifTool:ExifTool:Error": "File is empty"})
    assert e.damaged and e.meta["exiftool_messages"] == ["File is empty"]
    minor = normalize({"ExifTool:Track4:Warning": "[minor] The ExtractEmbedded option may find more tags"})
    assert not minor.damaged and "exiftool_messages" not in minor.meta


def test_parse_date_edges():
    assert parse_date("2020:08:25 15:48:46.39") == ("2020-08-25T15:48:46.39", None, None)
    assert parse_date("2020:08:25 15:48:46Z")[1] == "+00:00"
    assert parse_date("not a date") is None and parse_date(5) is None


def test_numbers_and_strings_are_cleaned():
    m = normalize({"EXIF:IFD0:Model": "  ", "EXIF:IFD0:Orientation": 9, "Composite:GPSLatitude": 200,
                   "Composite:GPSLongitude": 10, "File:MIMEType": "Image/JPEG"}).meta
    assert "model" not in m and "orientation" not in m and "gps_lat" not in m and "gps_lon" not in m
    assert m["mime"] == "image/jpeg"


def test_metadata_structure_warnings_are_not_damage():
    """The two false alarms of the first full-library run (ph-meta/1)."""
    interop = normalize({"ExifTool:ExifTool:Warning": "Bad format (51651) for InteropIFD entry 0",
                         "ExifTool:ExifTool:Copy1:Warning": "First entry for InteropIFD has an invalid format. "
                                                            "Ignoring possibly corrupted IFD",
                         "File:ImageDataHash": HASH})
    trailer = normalize({"ExifTool:ExifTool:Warning": "Unknown trailer with truncated '\\x12\\x00' data at offset 0x21aa3a4",
                         "File:ImageDataHash": HASH})
    for d in (interop, trailer):
        assert not d.damaged and d.content_sha256 == HASH and d.meta["exiftool_messages"]
        assert classify(d.meta["exiftool_messages"], False) == (False, False)


def test_stale_facts_are_described_again():
    old = "exiftool/13.61;ph-meta/1"
    assert stale(old, {"damaged": True, "exiftool_messages": ["Bad InteropIFD directory"]})
    assert stale(old, {"exiftool_messages": ["JPEG format error"]})
    assert not stale(old, {"make": "Canon"})                       # nothing to re-judge: keep it
    assert not stale("exiftool/13.61;ph-meta/2", {"damaged": True})
