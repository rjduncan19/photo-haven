"""Folder roles (B) and sharing protection (C), on fixture libraries shaped
like the patterns found in the first full-library run."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from photohaven import analysis, app, cli, roles
from photohaven.scan import ScanOptions

from fixtures.gen import build_library, copy_of, photo


def run(lib, fake, spec):
    env, root, tmp = lib
    build_library(root, spec)
    app.scan(env, ScanOptions(), describer=fake)
    an = analysis.analyze(env.conn, env.library)
    by = {f.relpath: fid for fid, f in an.folders.items()}
    role = lambda p: an.roles[by[p]].role if by[p] in an.roles else None
    sugg = lambda p: analysis.suggest(an, by[p])
    return an, role, sugg


def events(n_events=6, per=6):
    spec = {f"20{10 + i} Event {i}": [f"e{i}_{j}.jpg" for j in range(per)] for i in range(n_events)}
    return spec


def test_best_of_drawn_from_many_events_is_curation_and_kept(lib, fake):
    spec = events()
    spec["BestOf"] = [copy_of(f"20{10 + i} Event {i}/e{i}_0.jpg") for i in range(6)]
    an, role, sugg = run(lib, fake, spec)
    assert role("BestOf") == roles.CURATION
    s = sugg("BestOf")
    assert s.action == "keep" and "curated selection" in s.reason
    assert an.roles[[f for f, x in an.folders.items() if x.relpath == "BestOf"][0]].ask_shared


def test_picks_inside_their_source_and_star_folders(lib, fake):
    spec = {"Ward Summer 2016": [f"w{j}.jpg" for j in range(20)]}
    spec["Ward Summer 2016/Picks"] = [copy_of(f"Ward Summer 2016/w{j}.jpg") for j in range(5)]
    spec["5 Star Pictures"] = [copy_of("Ward Summer 2016/w7.jpg")]
    an, role, sugg = run(lib, fake, spec)
    assert role("Ward Summer 2016/Picks") == roles.CURATION        # name and a selection inside its source
    assert role("5 Star Pictures") == roles.CURATION               # name alone, even for one photo
    assert sugg("5 Star Pictures").action == "keep"


def test_backup_of_one_folder_is_still_a_backup(lib, fake):
    spec = events(1, 8)
    spec["Backup 2019"] = [copy_of(f"2010 Event 0/e0_{j}.jpg") for j in range(8)]
    spec["Backup 2020"] = [copy_of(f"2010 Event 0/e0_{j}.jpg") for j in range(8)]
    an, role, sugg = run(lib, fake, spec)
    assert role("Backup 2019") == roles.BACKUP and sugg("Backup 2019").action == "remove"
    # other backups holding the same photos don't make it look like a best-of
    assert role("2010 Event 0") != roles.CURATION


def test_purpose_received_and_shared_names(lib, fake):
    spec = events(1, 8)
    spec["Craigslist"] = [copy_of("2010 Event 0/e0_1.jpg"), photo("couch.jpg")]
    spec["Hong Kong From Jacky 2012"] = [copy_of("2010 Event 0/e0_2.jpg")]
    spec["From Rick's Nokia 920"] = [copy_of("2010 Event 0/e0_3.jpg")]
    spec["Shared Album"] = [copy_of("2010 Event 0/e0_4.jpg")]
    spec["For Grandma"] = [copy_of("2010 Event 0/e0_5.jpg")]
    an, role, sugg = run(lib, fake, spec)
    assert role("Craigslist") == roles.PURPOSE and sugg("Craigslist").action == "keep"
    assert role("Hong Kong From Jacky 2012") == roles.RECEIVED and sugg("Hong Kong From Jacky 2012").action == "keep"
    assert role("From Rick's Nokia 920") != roles.RECEIVED        # a device, not a person
    assert role("Shared Album") == roles.SHARED and sugg("Shared Album").action == "undecided"
    assert role("For Grandma") == roles.SHARED


def test_same_shoot_filed_twice_under_other_names(lib, fake):
    spec = {"2011-05-02 Welcome Tallulah": [f"t{j}.jpg" for j in range(8)]}
    spec["2011-05-10"] = [copy_of(f"2011-05-02 Welcome Tallulah/t{j}.jpg", name=f"DSC_{100 + j}.jpg")
                          for j in range(6)] + ["other.jpg"]
    an, role, sugg = run(lib, fake, spec)
    assert role("2011-05-10") == roles.REFILED and sugg("2011-05-10").action == "undecided"


def test_copy_markers_are_not_renames():
    assert roles.base_name("IMG_0506 (1).JPG") == roles.base_name("img_0506.jpeg") == "img_0506"
    assert roles.base_name("IMG_0506 - Copy.jpg") == "img_0506"
    assert roles.words("BestOf_2015 Favs") == ["best", "of", "bestof", "2015", "favs"]


def test_suggestions_never_remove_every_copy_with_roles(lib, fake):
    spec = events(3, 5)
    spec["BestOf"] = [copy_of(f"20{10 + i} Event {i}/e{i}_0.jpg") for i in range(3)]
    spec["Backup"] = [copy_of(f"20{10 + i} Event {i}/e{i}_{j}.jpg") for i in range(3) for j in range(5)]
    an, role, sugg = run(lib, fake, spec)
    s = analysis.suggest_all(an)
    for sha, inst in an.instances.items():
        assert any(s[fid].action != "remove" for fid, _, _ in inst)


def test_protect_command_ignores_folder_removes(lib, fake, capsys):
    env, root, tmp = lib
    spec = events(1, 4)
    spec["Shared With Mom"] = [copy_of(f"2010 Event 0/e0_{j}.jpg") for j in range(4)]
    build_library(root, spec)
    app.scan(env, ScanOptions(), describer=fake)
    state = env.state_dir
    env.close()
    st = ["--state", str(state)]
    assert cli.main([*st, "decisions", "add", "remove", "Shared With Mom", "--subtree"]) == 0
    assert cli.main([*st, "protect", "Shared With Mom"]) == 0
    env2 = app.open_env(state)
    try:
        p, _ = app.make_plan(env2)
        assert not p.items and p.ignored_protected == 4      # the remove is ignored inside a protected folder
        assert "Shared With Mom" in env2.library.roots[0].protected_folders
    finally:
        env2.close()
    assert cli.main([*st, "protect", "Shared With Mom", "--off"]) == 0
    env3 = app.open_env(state)
    try:
        p, _ = app.make_plan(env3)
        assert len(p.items) == 4
    finally:
        env3.close()


def test_report_lists_special_folders_with_the_sharing_question(lib, fake):
    spec = events()
    spec["BestOf"] = [copy_of(f"20{10 + i} Event {i}/e{i}_0.jpg") for i in range(6)]
    env, root, tmp = lib
    build_library(root, spec)
    app.scan(env, ScanOptions(), describer=fake)
    res = app.make_report(env, out=tmp / "rep", thumbs=False)
    t = (res.path / "data.js").read_text(encoding="utf-8")
    D = json.loads(t[t.index("{"):t.rindex("}") + 1])
    best = next(i for i, f in D["folders"].items() if f["path"] == "BestOf")
    assert D["sections"]["special"] == [int(best)]
    assert D["folders"][best]["role"]["r"] == "curation" and D["folders"][best]["role"]["q"] is True
    assert int(best) not in D["sections"]["redundant"] and int(best) not in D["sections"]["contained"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_exports_the_shared_note(lib, fake, tmp_path):
    """Answering 'Yes, shared' exports a subtree keep with note 'shared with others'."""
    spec = events()
    spec["BestOf"] = [copy_of(f"20{10 + i} Event {i}/e{i}_0.jpg") for i in range(6)]
    env, root, tmp = lib
    build_library(root, spec)
    app.scan(env, ScanOptions(), describer=fake)
    res = app.make_report(env, out=tmp / "rep", thumbs=False)
    js = (res.path / "app.js").read_text(encoding="utf-8")
    data = (res.path / "data.js").read_text(encoding="utf-8")
    harness = """
    const store = {}; const listeners = {};
    const el = () => ({ set textContent(v) {}, set innerHTML(v) {}, set onclick(v) {}, classList: { add() {}, remove() {} },
                         querySelector: () => null, appendChild() {} });
    global.window = {}; global.localStorage = { getItem: (k) => store[k] || null, setItem: (k, v) => { store[k] = v; } };
    global.CSS = { escape: (s) => s };
    global.document = { querySelector: () => el(), querySelectorAll: () => [], createElement: () => el(),
                        body: el(), addEventListener: (t, f) => { listeners[t] = f; } };
    """ + data.replace("window.PH_DATA", "global.window.PH_DATA") + """
    global.PH_DATA = window.PH_DATA;
    """ + js.replace("window.PH_EXPORT", "global.PH_EXPORT").replace("const D = window.PH_DATA", "const D = global.PH_DATA") + """
    const D = global.PH_DATA; const id = Object.keys(D.folders).find((k) => D.folders[k].path === "BestOf");
    const f = D.folders[id];
    const key = ["F", f.root, f.path, "subtree"].join("|");
    listeners.change({ target: { tagName: "SELECT", value: "yes", dataset: { shared: key,
      target: JSON.stringify({ kind: "folder", root_id: f.root, path: f.path, scope: "subtree" }) } } });
    console.log(global.PH_EXPORT().split("\\n")[1]);
    """
    script = tmp_path / "h.js"
    script.write_text(harness, encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    rec = json.loads(out.stdout.strip())
    assert rec["action"] == "keep" and rec["note"] == "shared with others" and rec["target"]["scope"] == "subtree"
