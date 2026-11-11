import json

from photohaven import cli

from fixtures.gen import build_library, copy_of


def test_cli_flow(tmp_path, capsys):
    root = tmp_path / "Pictures"
    build_library(root, {"a": ["x.jpg"], "b": [copy_of("a/x.jpg")]})
    st = ["--state", str(tmp_path / "state")]
    assert cli.main([*st, "init", "--project-home", str(tmp_path / "home"), "--root", f"pics={root}",
                     "--machine-name", "cli"]) == 0
    assert cli.main([*st, "scan", "--no-exiftool", "--quiet"]) == 0
    assert cli.main([*st, "status", "--json"]) == 0
    out = capsys.readouterr().out
    status = json.loads(out[out.index("{\n"):])
    assert status["dup_groups"]["groups"] == 1
    assert cli.main([*st, "root", "list"]) == 0
    assert cli.main([*st, "machine", "show"]) == 0
    assert cli.main([*st, "rebuild-db", "--quiet"]) == 0
    # a second init on the same machine is refused cleanly
    assert cli.main([*st, "init", "--project-home", str(tmp_path / "home"), "--root", f"pics={root}"]) == 2


def test_cli_refuses_unknown_root(tmp_path, capsys):
    root = tmp_path / "Pictures"
    root.mkdir()
    st = ["--state", str(tmp_path / "state")]
    cli.main([*st, "init", "--project-home", str(tmp_path / "home"), "--root", f"pics={root}"])
    assert cli.main([*st, "scan", "--root", "nope", "--no-exiftool"]) == 2
    assert "no root named" in capsys.readouterr().err
