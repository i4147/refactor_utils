"""Data-embedding tests: text as plain text, binary as base64."""
import base64
import json

from conftest import load_module_from_file

from pyrefactor import consolidate
from pyrefactor.resources import build_resources_code, classify_resource

PROJECT = {
    "__init__.py": "from .app import config\n",
    "app.py": (
        "import json\n\n"
        "def config():\n"
        "    try:\n"
        "        return json.loads(get_resource('data/cfg.json'))\n"
        "    except NameError:\n"
        "        from pathlib import Path\n"
        "        return json.loads((Path(__file__).parent / 'data' / 'cfg.json').read_text())\n"
    ),
    "data/cfg.json": '{"answer": 42}\n',
    "data/table.csv": "id,name\n1,alpha\n",
    "assets/font.ttf": bytes(range(256)) * 8,  # 2048 binary bytes
}


def test_classify_text_and_binary(tmp_path):
    j = tmp_path / "x.json"
    j.write_text("{}", encoding="utf-8")
    assert classify_resource(j) == "text"
    f = tmp_path / "f.ttf"
    f.write_bytes(b"\x00\x01\x02")
    assert classify_resource(f) == "binary"
    unk = tmp_path / "blob.weird"
    unk.write_bytes(b"plain text")
    assert classify_resource(unk) == "text"
    unk2 = tmp_path / "blob2.weird"
    unk2.write_bytes(b"no\x00text")
    assert classify_resource(unk2) == "binary"


def test_build_resources_code_text_and_binary():
    code = build_resources_code(
        [("data/cfg.json", b'{"a": 1}\n'), ("f.ttf", b"\x00\x01\x02")]
    )
    assert "'data/cfg.json'" in code
    # text embedded as plain text, not base64
    assert '"a": 1' in code
    # binary embedded as base64 literal
    b64 = base64.b64encode(b"\x00\x01\x02").decode()
    assert b64 in code
    ns = {}
    exec(compile(code, "<resources>", "exec"), ns)
    assert ns["get_resource"]("data/cfg.json") == '{"a": 1}\n'
    assert ns["get_resource"]("f.ttf") == b"\x00\x01\x02"
    assert ns["get_resource"]("f.ttf", encoding="utf-8") == "\x00\x01\x02"
    assert ns["list_resources"]() == ["data/cfg.json", "f.ttf"]


def test_accessor_collision_rename():
    code = build_resources_code([("a.txt", b"x")], taken_names={"get_resource"})
    assert "def get_resource(" not in code
    assert "def consolidated_get_resource(" in code


def test_embed_data_single_self_contained_file(make_project, tmp_path):
    src = make_project(PROJECT)
    out = tmp_path / "out"
    result = consolidate(src, out, embed_data=True, copy_data=False, force=True)
    files = [p for p in out.iterdir()]
    assert [p.name for p in files] == ["pkg.py"]  # nothing else written
    mod = load_module_from_file(result.files[0], "consolidated_embed")
    assert mod.config() == {"answer": 42}
    assert sorted(mod.list_resources()) == [
        "assets/font.ttf",
        "data/cfg.json",
        "data/table.csv",
    ]
    font = mod.get_resource("assets/font.ttf")
    assert font == bytes(range(256)) * 8
    text = mod.get_resource("data/cfg.json")
    assert isinstance(text, str)
    assert json.loads(text)["answer"] == 42
    # csv stays readable plain text in the file itself
    generated = result.files[0].read_text(encoding="utf-8")
    assert "id,name" in generated and "1,alpha" in generated
    # the binary is only present as base64
    assert base64.b64encode(bytes(range(256))).decode()[:40] in generated


def test_embed_with_resource_path(make_project, tmp_path):
    src = make_project(PROJECT)
    result = consolidate(
        src, tmp_path / "out", embed_data=True, copy_data=False, force=True
    )
    mod = load_module_from_file(result.files[0], "consolidated_rp")
    with mod.resource_path("assets/font.ttf") as path:
        assert path.read_bytes() == bytes(range(256)) * 8
    import pytest

    with pytest.raises(KeyError):
        mod.get_resource("missing.bin")


def test_embed_and_copy_coexist(make_project, tmp_path):
    src = make_project(PROJECT)
    out = tmp_path / "out"
    consolidate(src, out, embed_data=True, copy_data=True, force=True)
    assert (out / "pkg.py").is_file()
    assert (out / "data" / "cfg.json").is_file()  # copied as well as embedded


def test_no_embed_keeps_default_behaviour(make_project, tmp_path):
    src = make_project(PROJECT)
    out = tmp_path / "out"
    result = consolidate(src, out, force=True)
    generated = result.files[0].read_text(encoding="utf-8")
    assert "def get_resource" not in generated
    assert "_RESOURCES_TEXT" not in generated
    assert (out / "data" / "cfg.json").is_file()
