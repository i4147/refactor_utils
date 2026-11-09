"""Find the python files of a project and derive their dotted module names."""

from __future__ import annotations
import fnmatch
import os
from pathlib import Path
from typing import Iterable
from loguru import logger
from .models import DataFile, SourceFile

EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".env",
    "build",
    "dist",
    "node_modules",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".eggs",
    "site-packages",
    "tests",
    "test",
    "testing",
    "docs",
    "doc",
    ".idea",
    ".vscode",
}
EXCLUDED_FILE_GLOBS = ("__main__.py", "conftest.py", "test_*.py", "*_test.py", "setup.py", "iwgfyrkugf.py", "_consolidated.py")

# Data files that are commonly part of a Python project and should be preserved
DATA_EXTENSIONS = {
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".txt", ".csv", ".tsv", ".dat", ".db", ".sqlite", ".pickle", ".pkl",
    ".xml", ".html", ".css", ".js", ".md", ".rst", ".env", ".example",
    ".pem", ".key", ".crt", ".sql", ".graphql", ".proto",
}
# Also treat files without extension but common data names as data
DATA_FILENAMES = {
    "requirements.txt", "requirements-dev.txt", "Pipfile", "Pipfile.lock",
    "poetry.lock", "setup.cfg", "pyproject.toml", "tox.ini", ".env",
    "MANIFEST.in", "LICENSE", "LICENCE", "COPYING",
}


def detect_base(root: Path) -> Path:
    if (root / "__init__.py").is_file():
        return root.parent
    src = root / "src"
    if src.is_dir() and any(p.is_dir() and (p / "__init__.py").is_file() for p in src.iterdir()):
        return src
    return root


def _is_excluded(path: Path, posix: str, name: str, exclude: tuple[str, ...], include: tuple[str, ...]) -> bool:
    """Precedence: default excludes < --include < --exclude.

    - If path matches a default glob, it is excluded unless it matches --include.
    - If path matches an explicit --exclude pattern, it is always excluded.
    - --include can re-include files that would otherwise be excluded by defaults,
      but cannot override explicit --exclude.
    """
    # Check explicit exclude first (highest priority)
    if exclude and any(fnmatch.fnmatch(posix, pat) or fnmatch.fnmatch(name, pat) for pat in exclude):
        return True
    # Check default globs
    default_hit = any(fnmatch.fnmatch(name, g) for g in EXCLUDED_FILE_GLOBS)
    if default_hit:
        # Allow include to rescue it
        if include and any(fnmatch.fnmatch(posix, pat) or fnmatch.fnmatch(name, pat) for pat in include):
            logger.debug("Re-included {} via --include (overrode default exclude)", path)
            return False
        logger.debug("Skipping {} (default exclude)", path)
        return True
    return False


def _is_data_file(path: Path) -> bool:
    if path.suffix.lower() in DATA_EXTENSIONS:
        return True
    if path.name in DATA_FILENAMES:
        return True
    # Files without extension that are not python and not hidden cache are often data
    if path.suffix == "" and not path.name.startswith(".") and path.is_file():
        # Heuristic: small text files without extension like "VERSION", "CHANGELOG"
        return True
    return False


def _walk(
    directory: Path,
    skip: set[Path],
    extra_globs: tuple[str, ...],
    include: tuple[str, ...] = (),
    follow_symlinks: bool = False,
) -> Iterable[Path]:
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        logger.error("Cannot list {}: {}", directory, exc)
        return
    for entry in entries:
        # Symlink handling: optionally follow, but avoid cycles via skip set
        is_symlink = entry.is_symlink()
        if is_symlink and not follow_symlinks:
            # If symlink points inside tree and is a python file, include it; otherwise skip
            try:
                target = entry.resolve()
                # Avoid output-inside-source infinite loops
                if target in skip or any(str(target).startswith(str(s) + os.sep) for s in skip):
                    continue
            except OSError:
                logger.warning("Broken symlink skipped: {}", entry)
                continue
            # For symlinks to files, treat like regular file but log
            if entry.suffix == ".py":
                posix = entry.as_posix()
                if _is_excluded(entry, posix, entry.name, extra_globs, include):
                    continue
                logger.info("Including symlinked file {}", entry)
                yield entry
            continue
        if entry.is_dir():
            if entry.name in EXCLUDED_DIRS or entry.name.endswith(".egg-info"):
                # But allow if explicitly included via --include
                if include and any(fnmatch.fnmatch(entry.as_posix(), pat) or fnmatch.fnmatch(entry.name, pat) for pat in include):
                    pass
                else:
                    continue
            try:
                resolved = entry.resolve()
            except OSError:
                continue
            if resolved in skip:
                continue
            yield from _walk(entry, skip, extra_globs, include, follow_symlinks)
        elif entry.suffix == ".py":
            posix = entry.as_posix()
            if _is_excluded(entry, posix, entry.name, extra_globs, include):
                continue
            # Also check extra_globs as full path patterns (already in _is_excluded for explicit exclude)
            yield entry
        else:
            # Non-python files are not yielded here; see discover_data_files
            continue


def discover(
    root: Path,
    exclude: tuple[str, ...] = (),
    skip_dirs: Iterable[Path] = (),
    include: tuple[str, ...] = (),
    follow_symlinks: bool = False,
) -> list[SourceFile]:
    root = root.resolve()
    if root.is_file():
        module = root.stem
        return [SourceFile(root, module, False)] if root.suffix == ".py" else []
    base = detect_base(root)
    walk_root = base if base != root.parent else root
    skip = {p.resolve() for p in skip_dirs if p.exists()}
    # Also skip output inside source tree already handled via skip_dirs
    files: list[SourceFile] = []
    for path in _walk(walk_root, skip, exclude, include, follow_symlinks):
        try:
            parts = list(path.relative_to(base).with_suffix("").parts)
        except ValueError:
            # Path not under base (e.g., symlink outside); use stem
            parts = [path.stem]
        is_pkg = parts[-1] == "__init__"
        if is_pkg:
            parts = parts[:-1]
        if not parts:
            parts = [base.name]
        files.append(SourceFile(path, ".".join(parts), is_pkg))
    # Handle namespace packages: directories without __init__.py but with submodules
    # Already covered because _walk recurses regardless of __init__.py presence.
    # Log if we found namespace-like roots
    if not files:
        logger.warning("No python files discovered under {} (base={})", root, base)
    return files


def discover_data_files(
    root: Path,
    exclude: tuple[str, ...] = (),
    include: tuple[str, ...] = (),
    skip_dirs: Iterable[Path] = (),
    copy_data: bool = True,
) -> list[DataFile]:
    """Discover non-Python data files that should be preserved.

    This respects the same exclude/include precedence as :func:`discover`
    but looks for data extensions and well-known data filenames.

    If copy_data is False, returns empty list (feature disabled).
    """
    if not copy_data:
        return []
    root = root.resolve()
    if root.is_file():
        return []
    base = detect_base(root)
    walk_root = base if base != root.parent else root
    skip = {p.resolve() for p in skip_dirs if p.exists()}
    data_files: list[DataFile] = []
    # Walk again but collect data files
    stack = [walk_root]
    visited: set[Path] = set()
    while stack:
        cur = stack.pop()
        if cur.resolve() in visited:
            continue
        visited.add(cur.resolve())
        try:
            entries = sorted(cur.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            logger.error("Cannot list {}: {}", cur, exc)
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name in EXCLUDED_DIRS or entry.name.endswith(".egg-info"):
                    if include and any(fnmatch.fnmatch(entry.as_posix(), pat) for pat in include):
                        pass
                    else:
                        continue
                try:
                    resolved = entry.resolve()
                except OSError:
                    continue
                if resolved in skip:
                    continue
                # Avoid descending into output if it lives inside source
                stack.append(entry)
            elif entry.is_file():
                # Skip python files (handled separately) and default excluded globs
                if entry.suffix == ".py":
                    continue
                posix = entry.as_posix()
                # Apply same exclusion logic
                if exclude and any(fnmatch.fnmatch(posix, pat) or fnmatch.fnmatch(entry.name, pat) for pat in exclude):
                    continue
                if entry.name in EXCLUDED_FILE_GLOBS and not (include and any(fnmatch.fnmatch(posix, pat) for pat in include)):
                    continue
                # Identify as data file
                if _is_data_file(entry):
                    try:
                        rel = entry.relative_to(base).as_posix()
                    except ValueError:
                        rel = entry.name
                    data_files.append(DataFile(entry, rel))
                    logger.debug("Found data file: {} -> {}", entry, rel)
    if data_files:
        logger.info("Discovered {} data file(s) to copy", len(data_files))
    return data_files


def detect_pyproject_config(root: Path) -> dict:
    """Read [tool.pyrefactor] from pyproject.toml if present, for --pyproject awareness.

    Returns a dict of config values (exclude, include, entry, etc.) or empty.
    """
    # Search upwards from root for pyproject.toml and setup.cfg
    candidates = []
    cur = root.resolve() if root.exists() else Path.cwd()
    if cur.is_file():
        cur = cur.parent
    for parent in [cur] + list(cur.parents):
        pp = parent / "pyproject.toml"
        if pp.is_file():
            candidates.append(pp)
            break
    if not candidates:
        # Also check setup.cfg/setup.py discovery
        for parent in [cur] + list(cur.parents):
            if (parent / "setup.cfg").is_file() or (parent / "setup.py").is_file():
                logger.debug("Found setup.cfg/setup.py at {}", parent)
                break
        return {}
    pp = candidates[0]
    try:
        # Python 3.11+ has tomllib
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore
        with open(pp, "rb") as f:
            data = tomllib.load(f)
        tool = data.get("tool", {}).get("pyrefactor", {})
        if tool:
            logger.info("Loaded config from {}: {}", pp, tool)
            return tool
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to read pyproject config {}: {}", pp, exc)
    return {}
