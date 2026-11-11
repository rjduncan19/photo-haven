"""Synthetic fixtures (docs/10 §1). Never real photos.

make_jpeg() draws its index into a small JPEG with controlled EXIF.
build_library() turns a declarative description into a folder tree:

    build_library(tmp, {
        "Camera Roll/2015/07": ["a.jpg", "b.jpg"],
        "Backup 2019/DCIM":    [copy_of("Camera Roll/2015/07/a.jpg"), "unique.jpg"],
    })
"""
from __future__ import annotations

import dataclasses
import io
import os
import pathlib
import shutil

from PIL import Image, ImageDraw

from photohaven.util import long_path

EXIF_IFD = 0x8769
GPS_IFD = 0x8825


def jpeg_bytes(index: int, *, taken: str | None = "2015:07:04 13:45:12", offset: str | None = None,
               model: str = "TestCam 1", make: str = "Synthetic", orientation: int = 1,
               gps: tuple[float, float] | None = None, size: tuple[int, int] = (64, 48)) -> bytes:
    img = Image.new("RGB", size, ((index * 47) % 256, (index * 89) % 256, (index * 131) % 256))
    ImageDraw.Draw(img).text((4, 4), str(index), fill=(255, 255, 255))
    exif = Image.Exif()
    exif[0x010F] = make
    exif[0x0110] = model
    exif[0x0112] = orientation
    ifd = exif.get_ifd(EXIF_IFD)
    if taken:
        ifd[0x9003] = taken          # DateTimeOriginal
        ifd[0x9004] = taken          # CreateDate (DateTimeDigitized)
    if offset:
        ifd[0x9011] = offset         # OffsetTimeOriginal
    if gps:
        lat, lon = gps
        g = exif.get_ifd(GPS_IFD)
        g[1] = "N" if lat >= 0 else "S"
        g[2] = _dms(abs(lat))
        g[3] = "E" if lon >= 0 else "W"
        g[4] = _dms(abs(lon))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85, exif=exif)
    return buf.getvalue()


def _dms(x: float):
    d = int(x)
    m = int((x - d) * 60)
    s = round(((x - d) * 60 - m) * 60, 2)
    return (float(d), float(m), float(s))


@dataclasses.dataclass
class copy_of:
    src: str                       # relpath of the file to copy (must be built earlier)
    name: str | None = None        # new name (default: the source's)
    mtime: float | None = None     # set mtime (seconds), else keep the source's


@dataclasses.dataclass
class photo:
    name: str
    index: int | None = None
    kwargs: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class raw_bytes:
    name: str
    data: bytes


def build_library(root: pathlib.Path, spec: dict[str, list], *, start_index: int = 1) -> dict[str, pathlib.Path]:
    """Create the tree; returns relpath -> absolute path for every file made."""
    made: dict[str, pathlib.Path] = {}
    idx = start_index
    for folder, items in spec.items():
        d = root.joinpath(*folder.split("/")) if folder else root
        os.makedirs(long_path(d), exist_ok=True)
        for it in items:
            if isinstance(it, str):
                it = photo(it)
            if isinstance(it, photo):
                n = it.index if it.index is not None else idx
                idx += 1
                p = d / it.name
                _write(p, jpeg_bytes(n, **it.kwargs))
            elif isinstance(it, copy_of):
                src = made[it.src]
                p = d / (it.name or src.name)
                shutil.copy2(long_path(src), long_path(p))
                if it.mtime is not None:
                    os.utime(long_path(p), (it.mtime, it.mtime))
            elif isinstance(it, raw_bytes):
                p = d / it.name
                _write(p, it.data)
            else:
                raise TypeError(it)
            made[f"{folder}/{p.name}" if folder else p.name] = p
    return made


def _write(p: pathlib.Path, data: bytes) -> None:
    with open(long_path(p), "wb") as f:
        f.write(data)
