"""The suggestion engine (07 §4) on small fixture libraries, one scenario
per row of the table, plus the coherence rule."""
from __future__ import annotations

import pytest

from photohaven import analysis, app
from photohaven.scan import ScanOptions

from fixtures.gen import build_library, copy_of, photo, raw_bytes


def run(lib, fake, spec, *, intake=None):
    env, root, tmp = lib
    if intake:
        env.library.roots[0].intake_folders = [intake]
        env.library.save(env.project_home)
    build_library(root, spec)
    app.scan(env, ScanOptions(), describer=fake)
    an = analysis.analyze(env.conn, env.library)
    by_path = {f.relpath: fid for fid, f in an.folders.items()}
    return an, (lambda p: analysis.suggest(an, by_path[p])), by_path


def test_contained_generic_in_descriptive(lib, fake):
    an, s, _ = run(lib, fake, {"2015-07 Hawaii trip": ["a.jpg", "b.jpg", "c.jpg"],
                               "Backup": [copy_of("2015-07 Hawaii trip/a.jpg"), copy_of("2015-07 Hawaii trip/b.jpg")]})
    assert s("Backup").action == "remove" and "generic" in s("Backup").reason
    assert s("2015-07 Hawaii trip").action == "undecided"     # 1 unique file


def test_contained_in_intake(lib, fake):
    an, s, _ = run(lib, fake, {"Camera Roll": ["a.jpg", "b.jpg"], "Vacation": [copy_of("Camera Roll/a.jpg")]},
                   intake="Camera Roll")
    assert s("Vacation").action == "remove" and "intake" in s("Vacation").reason
    assert s("Camera Roll").action == "keep" and s("Camera Roll").locked


def test_identical_prefers_descriptive_name(lib, fake):
    an, s, _ = run(lib, fake, {"2019-08 Fiona soccer": ["a.jpg", "b.jpg"],
                               "Import (1)": [copy_of("2019-08 Fiona soccer/a.jpg"), copy_of("2019-08 Fiona soccer/b.jpg")]})
    assert s("Import (1)").action == "remove" and "more descriptive" in s("Import (1)").reason
    assert s("2019-08 Fiona soccer").action == "keep"


def test_identical_equal_names_keeps_shallower(lib, fake):
    an, s, _ = run(lib, fake, {"Backup": ["a.jpg"], "Pictures/Backup": [copy_of("Backup/a.jpg")]})
    assert s("Backup").action == "keep" and s("Pictures/Backup").action == "remove"


def test_descriptive_subset_of_generic_is_kept(lib, fake):
    an, s, _ = run(lib, fake, {"Backup 2019": ["a.jpg", "b.jpg", "c.jpg"],
                               "2015 Hawaii trip": [copy_of("Backup 2019/a.jpg")]})
    assert s("2015 Hawaii trip").action == "keep"
    assert s("Backup 2019").action == "undecided"


def test_redundant_spread(lib, fake):
    an, s, _ = run(lib, fake, {"Trip A": ["a.jpg", "x.jpg"], "Trip B": ["b.jpg", "y.jpg"],
                               "Mixed": [copy_of("Trip A/a.jpg"), copy_of("Trip B/b.jpg")]})
    assert s("Mixed").action == "remove" and s("Mixed").reason == "Every file exists elsewhere"


def test_unique_only_is_kept_and_partial_undecided(lib, fake):
    an, s, _ = run(lib, fake, {"Solo": ["a.jpg"], "Half": ["b.jpg", "c.jpg"], "Other": [copy_of("Half/b.jpg"), "d.jpg"]})
    assert s("Solo").action == "keep"
    assert s("Half").action == "undecided" and "unique" in s("Half").reason


def test_non_photo_file_blocks_and_explains(lib, fake):
    an, s, _ = run(lib, fake, {"Home": ["a.jpg"], "Backup": [copy_of("Home/a.jpg"), raw_bytes("notes.txt", b"x")]})
    assert s("Backup").action == "undecided" and "non-photo" in s("Backup").reason


def test_subtree_only_when_every_folder_agrees(lib, fake):
    an, s, by = run(lib, fake, {"2015 Hawaii": ["a.jpg", "b.jpg"], "2016 Paris": ["c.jpg"],
                                "Old Backup/2015": [copy_of("2015 Hawaii/a.jpg"), copy_of("2015 Hawaii/b.jpg")],
                                "Old Backup/2016": [copy_of("2016 Paris/c.jpg")]})
    sub = analysis.suggest_subtree(an, by["Old Backup"])
    assert sub is not None and sub.action == "remove" and sub.scope == "subtree"
    assert analysis.suggest_subtree(an, by[""]) is None


def test_suggestions_never_remove_every_copy(lib, fake):
    # chains of identical and contained generic folders: accepting every suggestion
    # must still leave one copy of each content
    an, s, by = run(lib, fake, {
        "Backup": ["a.jpg", "b.jpg"],
        "Backup (1)": [copy_of("Backup/a.jpg"), copy_of("Backup/b.jpg")],
        "Copy": [copy_of("Backup/a.jpg")],
        "Imports": [copy_of("Backup/b.jpg")],
    })
    sugg = analysis.suggest_all(an)
    for sha, inst in an.instances.items():
        assert any(sugg[fid].action != "remove" for fid, _, _ in inst), sha
