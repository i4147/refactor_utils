"""Find the python files of a project and derive their dotted module names."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Iterable

from loguru import logger

from .models import SourceFile

EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env", ".env", "build", "dist",
    "node_modules", ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".eggs",
    "site-packages", "tests", "test", "testing", "docs", "doc", ".idea", ".vscode",
}
EXCLUDED_FILE_GLOBS = ("__main__.py", "conftest.py", "test_*.py", "*_test.py", "setup.py")


def detect_base(root: Path) -> Path:
    """Directory that module names are computed relative to."""
    if (root / "__init__.py").is_file():
        return root.parent
    src = root / "src"
    if src.is_dir() and any(p.is_dir() and (p / "__init__.py").is_file() for p in src.iterdir()):
        return src
    return root


def _walk(directory: Path, skip: set[Path], extra_globs: tuple[str, ...]) -> Iterable[Path]:
    try:
        entries = sorted(directory.iterdir())
    except OSError as exc:
        logger.error("Cannot list {}: {}", directory, exc)
        return
    for entry in entries:
        if entry.is_dir():
            if entry.name in EXCLUDED_DIRS or entry.name.endswith(".egg-info"):
                continue
            if entry.resolve() in skip:
                continue
            yield from _walk(entry, skip, extra_globs)
        elif entry.suffix == ".py":
            if any(fnmatch.fnmatch(entry.name, g) for g in EXCLUDED_FILE_GLOBS):
                logger.debug("Skipping {}", entry)
                continue
            if any(fnmatch.fnmatch(entry.as_posix(), g) for g in extra_globs):
                logger.debug("Excluded by pattern: {}", entry)
                continue
            yield entry


def discover(root: Path, exclude: tuple[str, ...] = (), skip_dirs: Iterable[Path] = ()) -> list[SourceFile]:
    root = root.resolve()
    if root.is_file():
        module = root.stem
        return [SourceFile(root, module, False)] if root.suffix == ".py" else []
    base = detect_base(root)
    walk_root = base if base != root.parent else root
    skip = {p.resolve() for p in skip_dirs}
    files: list[SourceFile] = []
    for path in _walk(walk_root, skip, exclude):
        parts = list(path.relative_to(base).with_suffix("").parts)
        is_pkg = parts[-1] == "__init__"
        if is_pkg:
            parts = parts[:-1]
        if not parts:
            parts = [base.name]
        files.append(SourceFile(path, ".".join(parts), is_pkg))
    return files
