from __future__ import annotations

import hashlib
import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from photohaven import app                                  # noqa: E402
from photohaven.scan.exiftool import find_exiftool         # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent


class FakeDescriber:
    """Stands in for ExifTool: a 'content hash' of everything after the
    first 64 bytes, so tests of the scan pipeline run without ExifTool."""
    version = "0.0"

    def __init__(self):
        self.calls: list[str] = []

    def describe(self, paths):
        out = []
        for p in paths:
            self.calls.append(p)
            with open(p, "rb") as f:
                data = f.read()
            out.append({"SourceFile": p, "File:MIMEType": "image/jpeg",
                        "File:ImageDataHash": hashlib.sha256(data[64:]).hexdigest()})
        return out


@pytest.fixture
def fake():
    return FakeDescriber()


@pytest.fixture(scope="session")
def exiftool_exe():
    exe = find_exiftool(os.environ.get("PHOTOHAVEN_EXIFTOOL"))
    if not exe:
        pytest.skip("ExifTool not found")
    return exe


@pytest.fixture
def lib(tmp_path):
    """An empty library: one root, project home, state for machine 'alpha'."""
    root = tmp_path / "Pictures"
    root.mkdir()
    env, _ = app.init(tmp_path / "state-a", tmp_path / "home", [("pics", root)], quarantine=str(tmp_path / "q"),
                      library_name="test", machine_name="alpha")
    yield env, root, tmp_path
    try:
        env.close()
    except Exception:
        pass


def join_machine(tmp_path, root, name="beta"):
    env, _ = app.init(tmp_path / f"state-{name}", tmp_path / "home", [("pics", root)], machine_name=name)
    return env
