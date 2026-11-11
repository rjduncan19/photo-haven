"""Turn raw ExifTool JSON into the §3.3 `meta` object (docs/04 stage 4).

The tag list and fallbacks are pinned here and versioned by PH_META: bump it
whenever these rules change, so a later scan can re-derive older metadata.
Everything here is a pure function of ExifTool's output for the file's
bytes, so all copies of one SHA-256 get the same facts.

Not done here, on purpose: the filename and mtime date fallbacks (04 stage
4). They depend on the instance, not the bytes, and `meta` is per content
(03 §9 `contents`). They belong to the link/report pass, per instance.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import re

from ..util import format_ts

PH_META = 1

# 03 §8.1 "Damage". Versioned with PH_META.
DAMAGE_PATTERNS = re.compile(
    r"format error|error reading|truncated|corrupt|unexpected end|bad .*(header|data)|missing .*data",
    re.IGNORECASE)

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_MIME = re.compile(r"^[a-z0-9.+-]+/[a-z0-9.+-]+$")
_DATE = re.compile(r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?(?:\.(\d+))?\s*(Z|[+-]\d{2}:?\d{2})?$")


@dataclasses.dataclass
class Described:
    meta: dict
    content_sha256: str | None
    damaged: bool
    messages: list[str]


class _Tags:
    """Index of ExifTool's `family0:family1[:CopyN]:Tag` keys."""

    def __init__(self, raw: dict):
        self.items: list[tuple[str, str, int, object]] = []   # (family0, tag, copy, value)
        for key, val in raw.items():
            if key == "SourceFile":
                continue
            parts = key.split(":")
            if len(parts) < 2:
                continue
            copy = 0
            for p in parts[1:-1]:
                if p.startswith("Copy") and p[4:].isdigit():
                    copy = int(p[4:])
            self.items.append((parts[0], parts[-1], copy, val))

    def get(self, family0: str, tag: str):
        best = None
        for g, t, c, v in self.items:
            if g == family0 and t == tag and (best is None or c < best[0]):
                best = (c, v)
        return None if best is None else best[1]

    def all(self, family0: str, tag: str) -> list:
        return [v for g, t, c, v in sorted((x for x in self.items if x[0] == family0 and x[1] == tag),
                                          key=lambda x: x[2])]


def parse_date(value) -> tuple[str, str | None, str | None] | None:
    """ExifTool date string -> (taken_local, taken_offset, taken_utc)."""
    if not isinstance(value, str):
        return None
    m = _DATE.match(value.strip())
    if not m:
        return None
    y, mo, d, h, mi, s, frac, tz = m.groups()
    try:
        base = _dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0))
    except ValueError:
        return None   # 0000:00:00 and friends
    frac = (frac or "")[:6]
    local = base.strftime("%Y-%m-%dT%H:%M:%S") + (f".{frac}" if frac else "")
    offset = utc = None
    if tz:
        if tz == "Z":
            offset = "+00:00"
        else:
            tz = tz.replace(":", "")
            hh, mm = int(tz[1:3]), int(tz[3:5])
            if hh > 23 or mm > 59:
                return (local, None, None)
            offset = f"{tz[0]}{hh:02d}:{mm:02d}"
        sign = 1 if offset[0] == "+" else -1
        delta = _dt.timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6])) * sign
        exact = base + _dt.timedelta(microseconds=int(frac.ljust(6, "0")) if frac else 0)
        utc = format_ts((exact - delta).replace(tzinfo=_dt.timezone.utc))
    return local, offset, utc


def _offset(value) -> str | None:
    if isinstance(value, str) and re.fullmatch(r"[+-]\d{2}:\d{2}", value.strip()):
        return value.strip()
    return None


def _taken(t: _Tags) -> dict:
    # 1. EXIF DateTimeOriginal (Composite:SubSecDateTimeOriginal carries the
    #    sub-seconds as text; with -n the bare SubSecTimeOriginal tag would lose leading zeros).
    if t.get("EXIF", "DateTimeOriginal") is not None:
        p = parse_date(t.get("Composite", "SubSecDateTimeOriginal")) or parse_date(t.get("EXIF", "DateTimeOriginal"))
        if p:
            return _with_offset(p, t.get("EXIF", "OffsetTimeOriginal"), "exif_original")
    # 2. EXIF CreateDate (DateTimeDigitized)
    if t.get("EXIF", "CreateDate") is not None:
        p = parse_date(t.get("Composite", "SubSecCreateDate")) or parse_date(t.get("EXIF", "CreateDate"))
        if p:
            return _with_offset(p, t.get("EXIF", "OffsetTimeDigitized"), "exif_digitized")
    # 3. QuickTime CreationDate (has a zone)
    p = parse_date(t.get("QuickTime", "CreationDate"))
    if p:
        return _fields(p, "quicktime_creationdate")
    # 4. QuickTime CreateDate (UTC by the QuickTime spec). The wall clock where
    #    it was taken is unknown, so taken_local holds the UTC time with no offset.
    p = parse_date(t.get("QuickTime", "CreateDate"))
    if p:
        local, _, _ = p
        exact = _local_dt(local).replace(tzinfo=_dt.timezone.utc)
        return {"taken_local": local, "taken_utc": format_ts(exact), "taken_source": "quicktime_createdate"}
    # 5. XMP DateCreated
    p = parse_date(t.get("XMP", "DateCreated"))
    if p:
        return _fields(p, "xmp_datecreated")
    return {}


def _local_dt(local: str) -> _dt.datetime:
    whole, _, frac = local.partition(".")
    return _dt.datetime.strptime(whole, "%Y-%m-%dT%H:%M:%S") + _dt.timedelta(
        microseconds=int(frac.ljust(6, "0")) if frac else 0)


def _with_offset(p, offset_tag, source: str) -> dict:
    local, off, utc = p
    if off is None:
        off = _offset(offset_tag)
        if off:
            sign = 1 if off[0] == "+" else -1
            delta = _dt.timedelta(hours=int(off[1:3]), minutes=int(off[4:6])) * sign
            utc = format_ts((_local_dt(local) - delta).replace(tzinfo=_dt.timezone.utc))
    return _fields((local, off, utc), source)


def _fields(p, source: str) -> dict:
    local, off, utc = p
    return {"taken_local": local, "taken_offset": off, "taken_utc": utc, "taken_source": source}


def _str(v) -> str | None:
    if v is None or isinstance(v, (dict, list, bool)):
        return None
    s = str(v).strip()
    return s or None


def _int(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v == int(v):
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _float(v, lo: float | None = None, hi: float | None = None) -> float | None:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        try:
            v = float(str(v))
        except (TypeError, ValueError):
            return None
    v = float(v)
    if v != v or (lo is not None and v < lo) or (hi is not None and v > hi):
        return None
    return v


def messages_of(t: _Tags) -> tuple[list[str], bool]:
    """(non-minor messages in the order reported, damaged?) per 03 §8.1."""
    errors = [str(v) for v in t.all("ExifTool", "Error")]
    warnings = [str(v) for v in t.all("ExifTool", "Warning")]
    serious = [w for w in warnings if not w.lstrip().lower().startswith("[minor]")]
    damaged = bool(errors) or any(DAMAGE_PATTERNS.search(w) for w in serious)
    return errors + serious, damaged


def normalize(raw: dict) -> Described:
    t = _Tags(raw)
    meta: dict = {}
    meta.update(_taken(t))
    meta["make"] = _str(t.get("EXIF", "Make") or t.get("QuickTime", "Make"))
    meta["model"] = _str(t.get("EXIF", "Model") or t.get("QuickTime", "Model"))
    meta["lens"] = _str(t.get("EXIF", "LensModel") or t.get("Composite", "LensID"))
    size = t.get("Composite", "ImageSize")
    if isinstance(size, str):
        parts = re.split(r"[x ]", size.strip())
        if len(parts) == 2 and all(p.isdigit() and int(p) > 0 for p in parts):
            meta["width"], meta["height"] = int(parts[0]), int(parts[1])
    o = _int(t.get("EXIF", "Orientation"))
    meta["orientation"] = o if o is not None and 1 <= o <= 8 else None
    meta["gps_lat"] = _float(t.get("Composite", "GPSLatitude"), -90, 90)
    meta["gps_lon"] = _float(t.get("Composite", "GPSLongitude"), -180, 180)
    meta["gps_alt"] = _float(t.get("Composite", "GPSAltitude")) if t.get("Composite", "GPSAltitude") is not None else None
    if meta["gps_lat"] is None or meta["gps_lon"] is None:
        meta["gps_lat"] = meta["gps_lon"] = None
    d = _float(t.get("QuickTime", "Duration"), 0)
    meta["duration_ms"] = round(d * 1000) if d is not None else None
    mime = _str(t.get("File", "MIMEType"))
    meta["mime"] = mime.lower() if mime and _MIME.match(mime.lower()) else None
    meta["apple_content_id"] = _str(t.get("MakerNotes", "ContentIdentifier") or t.get("QuickTime", "ContentIdentifier"))
    meta["apple_burst_id"] = _str(t.get("MakerNotes", "BurstUUID"))
    oih = _str(t.get("XMP", "OriginalImageHash"))
    if oih and _HEX64.match(oih) and str(t.get("XMP", "OriginalImageHashType") or "").upper() == "SHA256":
        meta["xmp_original_image_hash"] = oih.lower()
    msgs, damaged = messages_of(t)
    if damaged:
        meta["damaged"] = True
    if msgs:
        meta["exiftool_messages"] = msgs
    h = _str(t.get("File", "ImageDataHash"))
    content = h.lower() if h and _HEX64.match(h) and not damaged else None
    meta = {k: v for k, v in meta.items() if v is not None}
    return Described(meta, content, damaged, msgs)
