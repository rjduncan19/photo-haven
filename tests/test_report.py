"""Report generation (07): sections, detail shards, and the export format
(the browser's PH_EXPORT is checked against the schema in the manual run;
here the Python side and the files it writes)."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from photohaven import app
from photohaven.scan import ScanOptions

from fixtures.gen import build_library, copy_of, raw_bytes


def load_data(path):
    t = (path / "data.js").read_text(encoding="utf-8")
    return json.loads(t[t.index("{"):t.rindex("}") + 1])


def test_report_sections_and_shards(lib, fake):
    env, root, tmp = lib
    build_library(root, {
        "2015-07 Hawaii trip": ["a.jpg", "b.jpg"],
        "Backup": [copy_of("2015-07 Hawaii trip/a.jpg"), copy_of("2015-07 Hawaii trip/b.jpg")],
        "Mixed": [copy_of("2015-07 Hawaii trip/a.jpg"), "u.jpg"],
        "Same": ["s.jpg", copy_of("Same/s.jpg", name="s - Copy.jpg")],
        "Docs": [raw_bytes("x.txt", b"x")],
    })
    app.scan(env, ScanOptions(), describer=fake)
    res = app.make_report(env, out=tmp / "rep", thumbs=False)
    D = load_data(res.path)
    F = D["folders"]
    path = lambda i: F[str(i)]["path"]
    assert [path(i) for i in D["sections"]["contained"]] == ["Backup"]
    assert [path(i) for i in D["sections"]["internal"]] == ["Same"]
    assert D["summary"]["dupGroups"] == 3
    for name in ("index.html", "app.js", "style.css", "data.js"):
        assert (res.path / name).exists()
    same = next(i for i in F if F[i]["path"] == "Same")
    det = (res.path / "detail" / f"{same}.js").read_text(encoding="utf-8")
    d = json.loads(det[det.index("{"):det.rindex("}") + 1])
    sugg = {x["name"]: x.get("sugg", {}).get("a") for x in d["files"]}
    assert sugg == {"s - Copy.jpg": "remove", "s.jpg": None}
    meta = json.loads((env.state_dir / "reports" / res.report_id / "report.json").read_text())
    assert meta["report_id"] == res.report_id


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_export_is_canonical(lib, fake, tmp_path):
    env, root, tmp = lib
    build_library(root, {"A": ["a.jpg"], "B": [copy_of("A/a.jpg")]})
    app.scan(env, ScanOptions(), describer=fake)
    res = app.make_report(env, out=tmp / "rep", thumbs=False)
    js = (res.path / "app.js").read_text(encoding="utf-8")
    start = js.index("function canonical")
    end = js.index("function exportText")
    script = js[start:end] + "console.log(canonical({b:1,a:{d:'é',c:[1,2]}}));"
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8").stdout.strip()
    assert out == '{"a":{"c":[1,2],"d":"é"},"b":1}'
