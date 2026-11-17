import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def write_tree(root: Path, files: dict) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return root


def load_module_from_file(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def make_project(tmp_path):
    """Factory building an on-disk package from a {relpath: content} map.

    Paths are relative to the created root (default ``tmp_path/pkg``), i.e.
    keys like ``"core.py"`` and ``"data/settings.json"`` live directly in the
    package directory.
    """

    def _make(files: dict, root_name: str = "pkg") -> Path:
        root = tmp_path / root_name
        root.mkdir(parents=True, exist_ok=True)
        return write_tree(root, files)

    return _make


@pytest.fixture()
def demo_pkg_path():
    """The example package shipped under examples/."""
    return ROOT / "examples" / "demo_project" / "demo_pkg"
