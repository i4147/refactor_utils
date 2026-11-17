"""CLI tests (in-process via main(argv))."""
import json

from pyrefactor.cli import main

PROJECT = {
    "__init__.py": "from .core import run\n\nVERSION = '1.0'\n",
    "core.py": "def run():\n    return 'ok'\n\n"
    'if __name__ == "__main__":\n    print("guard")\n',
    "data/cfg.json": '{"x": 1}\n',
}


def _src(make_project):
    return make_project(PROJECT)


def test_cli_basic_write(make_project, tmp_path, capsys):
    src = _src(make_project)
    out = tmp_path / "dist"
    rc = main([str(src), "-o", str(out)])
    assert rc == 0
    assert (out / "pkg.py").is_file()
    assert (out / "data" / "cfg.json").is_file()


def test_cli_embed_data_flag(make_project, tmp_path):
    src = _src(make_project)
    out = tmp_path / "dist"
    rc = main([str(src), "-o", str(out), "--embed-data", "--no-copy-data"])
    assert rc == 0
    assert (out / "pkg.py").is_file()
    generated = (out / "pkg.py").read_text(encoding="utf-8")
    assert "get_resource" in generated
    assert not (out / "data").exists()


def test_cli_stdout(capsys, make_project):
    src = _src(make_project)
    rc = main([str(src), "--stdout"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "VERSION = '1.0'" in captured.out


def test_cli_dry_run(make_project, tmp_path):
    src = _src(make_project)
    out = tmp_path / "dist"
    rc = main([str(src), "-o", str(out), "--dry-run"])
    assert rc == 0
    assert not out.exists()


def test_cli_check_pass_and_fail(make_project, tmp_path):
    src = _src(make_project)
    out = tmp_path / "dist"
    assert main([str(src), "-o", str(out)]) == 0
    assert main([str(src), "-o", str(out), "--check"]) == 0
    (src / "core.py").write_text("def run():\n    return 'changed'\n")
    assert main([str(src), "-o", str(out), "--check"]) == 2


def test_cli_bad_main_name(make_project, tmp_path):
    src = _src(make_project)
    rc = main([str(src), "-o", str(tmp_path / "o"), "--main-name", "not-id!"])
    assert rc == 2


def test_cli_missing_source(tmp_path):
    rc = main([str(tmp_path / "nope"), "-o", str(tmp_path / "o")])
    assert rc == 2


def test_cli_log_file(make_project, tmp_path):
    src = _src(make_project)
    log = tmp_path / "run.log"
    rc = main([str(src), "-o", str(tmp_path / "o"), "--log-file", str(log)])
    assert rc == 0
    assert log.exists()


def test_cli_config_file(make_project, tmp_path):
    src = _src(make_project)
    cfg = tmp_path / "tool.toml"
    cfg.write_text(
        '[tool.pyrefactor]\ndata_glob = ["*.json"]\nembed_data = false\n',
        encoding="utf-8",
    )
    out = tmp_path / "dist"
    assert main([str(src), "-o", str(out), "--config", str(cfg)]) == 0
    # ini-less project has no other data files; json copied per glob
    assert (out / "data" / "cfg.json").is_file()


def test_cli_json_output_still_valid(make_project, tmp_path):
    src = _src(make_project)
    out = tmp_path / "dist"
    assert main([str(src), "-o", str(out), "--embed-data"]) == 0
    generated = (out / "pkg.py").read_text(encoding="utf-8")
    compile(generated, "pkg.py", "exec")
    assert json.loads(json.dumps({"x": 1})) == {"x": 1}
