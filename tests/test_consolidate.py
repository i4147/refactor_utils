"""End-to-end consolidation tests: the consolidated file must be a working
drop-in replacement for the original package."""
import json
import subprocess
import sys
from pathlib import Path

from conftest import load_module_from_file

from pyrefactor import consolidate, validate_syntax
from pyrefactor.core import ConsolidationError

MULTI = {
    "__init__.py": (
        "from .core import Person, greet\n"
        "from .helpers import helper, UTIL\n"
        "\nVERSION = '1.0'\n"
        '__all__ = ["Person", "greet", "helper", "UTIL", "VERSION"]\n'
    ),
    "core.py": (
        "import json\nfrom pathlib import Path\nfrom .helpers import helper, UTIL\n"
        "\nDEFAULT = 'world'\n\n"
        "def load():\n"
        "    p = Path(__file__).parent / 'data' / 'cfg.json'\n"
        "    return json.loads(p.read_text())\n\n"
        "class Person:\n"
        "    def __init__(self, name=DEFAULT):\n        self.name = name\n"
        "    def hi(self):\n        return f'hi {self.name} ({helper(self.name)})'\n\n"
        "def greet(name=DEFAULT):\n"
        "    return load()['greeting'].format(name=name) + str(UTIL)\n\n"
        "def main(argv=None):\n    print('main', greet())\n    return 0\n\n"
        'if __name__ == "__main__":\n    main()\n'
    ),
    "helpers.py": (
        "UTIL = 7\n\ndef helper(name):\n    return name.upper()\n\n"
        'if __name__ == "__main__":\n    print("helpers guard")\n'
    ),
    "data/cfg.json": '{"greeting": "Greetings, {name}!"}\n',
}


def test_consolidated_file_is_drop_in_replacement(make_project, tmp_path):
    src = make_project(MULTI)
    result = consolidate(src, tmp_path / "out", force=True)
    assert result.validated and result.files
    target = result.files[0]
    assert target.name == "pkg.py"
    validate_syntax(target.read_text(encoding="utf-8"), target.name)

    # import the consolidated file like a normal module
    sys.path.insert(0, str(src.parent))
    import pkg as original

    consolidated = load_module_from_file(target, "consolidated_pkg")
    assert consolidated.VERSION == original.VERSION == "1.0"
    assert consolidated.greet("Ada") == original.greet("Ada")
    assert consolidated.Person("Ada").hi() == original.Person("Ada").hi()
    assert consolidated.helper("x") == original.helper("x")
    assert consolidated.UTIL == original.UTIL
    assert sorted(consolidated.__all__) == sorted(original.__all__)


def test_data_files_copied_next_to_output(make_project, tmp_path):
    src = make_project(MULTI)
    result = consolidate(src, tmp_path / "out", force=True)
    copied = {Path(p).name: Path(p).read_text() for p in result.copied_data_files}
    assert "cfg.json" in copied
    assert json.loads(copied["cfg.json"])["greeting"] == "Greetings, {name}!"


def test_run_consolidated_file_as_script(make_project, tmp_path):
    src = make_project(MULTI)
    result = consolidate(src, tmp_path / "out", force=True)
    proc = subprocess.run(
        [sys.executable, str(result.files[0])], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert "main" in proc.stdout


def test_imports_pruned_to_what_is_used(make_project, tmp_path):
    src = make_project({**MULTI, "extra.py": "import os\nimport sys\n\nUSED = os.sep\n"})
    result = consolidate(src, tmp_path / "out", force=True)
    text = result.files[0].read_text(encoding="utf-8")
    assert "import os" in text
    assert "import sys" not in text


def test_name_collision_renamed_consistently(make_project, tmp_path):
    src = make_project(
        {
            "__init__.py": "from .a import thing as a_thing\nfrom .b import thing as b_thing\n",
            "a.py": "thing = 'a'\ndef get():\n    return thing\n",
            "b.py": "thing = 'b'\ndef get():\n    return thing\n",
        }
    )
    result = consolidate(src, tmp_path / "out", force=True)
    text = result.files[0].read_text(encoding="utf-8")
    assert "thing = 'a'" in text and "thing = 'b'" in text
    consolidated = load_module_from_file(result.files[0], "consolidated_col")
    # each module's get() must still see its own thing
    values = {v for v in consolidated.__dict__.values() if v in ("a", "b")}
    assert values == {"a", "b"}


def test_duplicated_identical_function_deduped(make_project, tmp_path):
    src = make_project(
        {
            "__init__.py": "from .a import dup\n",
            "a.py": "def dup():\n    return 42\n",
            "b.py": "def dup():\n    return 42\n",
        }
    )
    result = consolidate(src, tmp_path / "out", force=True)
    assert result.plan.deduped, "expected the duplicate to be dropped"
    consolidated = load_module_from_file(result.files[0], "consolidated_dup")
    assert consolidated.dup() == 42


def test_merged_main_guards(make_project, tmp_path):
    src = make_project(MULTI)
    result = consolidate(src, tmp_path / "out", force=True)
    text = result.files[0].read_text(encoding="utf-8")
    assert text.count('if __name__ == "__main__":') == 1
    assert "# from pkg.cli" not in text
    assert "# from pkg.core" in text or "# from pkg.helpers" in text


def test_entry_filtering(make_project, tmp_path):
    src = make_project(MULTI)
    result = consolidate(src, tmp_path / "out", entry=("core",), force=True)
    text = result.files[0].read_text(encoding="utf-8")
    assert 'if __name__ == "__main__":' in text
    assert "helpers guard" not in text


def test_check_mode_fails_when_output_would_change(make_project, tmp_path):
    src = make_project(MULTI)
    out = tmp_path / "out"
    consolidate(src, out, force=True)
    # up to date: passes
    result = consolidate(src, out, check=True, force=True)
    assert result.dry_run
    # source changed: raises
    (src / "helpers.py").write_text("UTIL = 8\n\nX = 1\n")
    try:
        consolidate(src, out, check=True, force=True)
    except ConsolidationError as exc:
        assert "would change" in str(exc)
    else:
        raise AssertionError("check should have failed")


def test_dry_run_writes_nothing(make_project, tmp_path):
    src = make_project(MULTI)
    out = tmp_path / "out"
    result = consolidate(src, out, dry_run=True)
    assert result.dry_run
    assert not out.exists()


def test_no_valid_python_emitted_on_docstring_strip(make_project, tmp_path):
    """--no-keep-docstrings must still produce byte-accurate rewrites."""
    src = make_project(
        {
            "__init__.py": "from .m import run\n",
            "m.py": (
                'def run():\n    """Doc.\n\n    More doc.\n    """\n'
                "    return MAGIC\n\nMAGIC = 5\n"
            ),
        }
    )
    result = consolidate(src, tmp_path / "out", keep_docstrings=False, force=True)
    consolidated = load_module_from_file(result.files[0], "consolidated_doc")
    assert consolidated.run() == 5
    assert "Doc." not in result.files[0].read_text(encoding="utf-8")


def test_self_consolidation(tmp_path):
    """Consolidate pyrefactor itself and check the single file works."""
    src = Path(__file__).resolve().parents[1] / "src" / "pyrefactor"
    result = consolidate(src, tmp_path / "out", force=True)
    consolidated = load_module_from_file(result.files[0], "consolidated_pyrefactor")
    assert consolidated.__version__
    # the consolidated single file can still consolidate a tiny package
    little = tmp_path / "little"
    little.mkdir()
    (little / "m.py").write_text("V = 2\n", encoding="utf-8")
    r2 = consolidated.consolidate(little, tmp_path / "out2", force=True)
    assert r2.files[0].read_text().strip().endswith("V = 2")
