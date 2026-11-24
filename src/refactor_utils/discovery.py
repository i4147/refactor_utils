from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Iterable, Iterator

from .logging_setup import logger
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

EXCLUDED_FILE_GLOBS = (
    "__main__.py",
    ".cache.db",
    ".cache.json",
    "conftest.py",
    "test_*.py",
    "*_tests.py",
    "*_test.py",
    "setup.py",
    ".cache.sqlite3",
    "_consolidated.py",
)

DATA_EXTENSIONS = {
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".txt",
    ".csv",
    ".tsv",
    ".dat",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pickle",
    ".pkl",
    ".xml",
    ".html",
    ".css",
    ".js",
    ".md",
    ".rst",
    ".env",
    ".example",
    ".pem",
    ".key",
    ".crt",
    ".sql",
    ".graphql",
    ".proto",
    ".ttf",
    ".otf",
    ".woff",
    ".woff2",
    ".eot",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".bmp",
    ".webp",
    ".pdf",
    ".bin",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".pyd",
    ".zip",
    ".gz",
}

DATA_FILENAMES = {
    "requirements.txt",
    "requirements-dev.txt",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "setup.cfg",
    "pyproject.toml",
    "tox.ini",
    ".env",
    "MANIFEST.in",
    "LICENSE",
    "LICENCE",
    "COPYING",
}


def detect_base(root: Path) -> Path:
    if (root / "__init__.py").is_file():
        return root.parent
    src = root / "src"
    if src.is_dir() and any(p.is_dir() and (p / "__init__.py").is_file() for p in src.iterdir()):
        return src
    return root


def _matches(rel: str, posix: str, name: str, patterns: Iterable[str]) -> bool:
    for pat in patterns:
        if not pat:
            continue
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(posix, pat):
            return True
        if fnmatch.fnmatch(name, pat):
            return True
        if fnmatch.fnmatch(f"*/{rel}", f"*/{pat}"):
            return True
        if rel.endswith("/" + pat.lstrip("*/")) and pat.startswith("*/"):
            return True
    return False


def _dir_included(rel: str, name: str, include: Iterable[str]) -> bool:
    for pat in include:
        if not pat:
            continue
        if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat):
            return True
        if pat.startswith(rel.rstrip("/") + "/"):
            return True
        if fnmatch.fnmatch(rel + "/dummy", pat):
            return True
    return False


def _is_excluded(
    path: Path,
    posix: str,
    name: str,
    exclude: Iterable[str],
    include: Iterable[str],
    rel: str = "",
) -> bool:
    rel = rel or posix
    if exclude and _matches(rel, posix, name, exclude):
        return True

    default_hit = any(fnmatch.fnmatch(name, g) for g in EXCLUDED_FILE_GLOBS)
    if default_hit:
        if include and _matches(rel, posix, name, include):
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
    if path.suffix == "" and not path.name.startswith(".") and path.is_file():
        return True
    return False


def _walk(
    directory: Path,
    skip: set,
    extra_globs: Iterable[str],
    include: Iterable[str] = (),
    follow_symlinks: bool = False,
    base: Path | None = None,
    visited_dirs: set[str] | None = None,
) -> Iterator[Path]:
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        logger.error("Cannot list {}: {}", directory, exc)
        return

    base = base or directory
    if visited_dirs is None:
        visited_dirs = set()

    for entry in entries:
        try:
            rel = entry.relative_to(base).as_posix()
        except ValueError:
            rel = entry.as_posix()

        is_symlink = entry.is_symlink()
        if is_symlink and not follow_symlinks:
            try:
                target = entry.resolve()
                if target in skip or any(str(target).startswith(str(s) + os.sep) for s in skip):
                    continue
            except OSError:
                logger.warning("Broken symlink skipped: {}", entry)
                continue

            if entry.suffix == ".py":
                posix = entry.as_posix()
                if _is_excluded(entry, posix, entry.name, extra_globs, include, rel):
                    continue
                logger.info("Including symlinked file {}", entry)
                yield entry
            continue

        if entry.is_dir():
            if entry.name in EXCLUDED_DIRS or entry.name.endswith(".egg-info"):
                if not _dir_included(rel, entry.name, include):
                    continue

            try:
                resolved = entry.resolve()
            except OSError:
                continue
            if resolved in skip:
                continue

            resolved_key = str(resolved)
            if resolved_key in visited_dirs:
                logger.warning("Symlink loop detected at {}; skipping", entry)
                continue
            visited_dirs.add(resolved_key)

            yield from _walk(
                entry,
                skip,
                extra_globs,
                include,
                follow_symlinks,
                base,
                visited_dirs,
            )

            visited_dirs.discard(resolved_key)
        elif entry.suffix == ".py":
            posix = entry.as_posix()
            if _is_excluded(entry, posix, entry.name, extra_globs, include, rel):
                continue
            yield entry
        else:
            continue


def discover(
    root: Path | str,
    exclude: Iterable[str] = (),
    skip_dirs: Iterable[Path] = (),
    include: Iterable[str] = (),
    follow_symlinks: bool = False,
) -> list[SourceFile]:
    root = Path(root).resolve()
    if root.is_file():
        module = root.stem
        return [SourceFile(root, module, False)] if root.suffix == ".py" else []

    base = detect_base(root)
    walk_root = base if base != root.parent else root
    skip = {Path(p).resolve() for p in skip_dirs if Path(p).exists()}

    files = []
    for path in _walk(walk_root, skip, exclude, include, follow_symlinks, base):
        try:
            parts = list(path.relative_to(base).with_suffix("").parts)
        except ValueError:
            parts = [path.stem]

        is_pkg = parts[-1] == "__init__"
        if is_pkg:
            parts = parts[:-1]
        if not parts:
            parts = [base.name]

        files.append(SourceFile(path, ".".join(parts), is_pkg))

    if not files:
        logger.warning("No python files discovered under {} (base={})", root, base)
    return files


def discover_data_files(
    root: Path | str,
    exclude: Iterable[str] = (),
    include: Iterable[str] = (),
    skip_dirs: Iterable[Path] = (),
    copy_data: bool = True,
    collect: bool | None = None,
) -> list[DataFile]:
    collect = copy_data if collect is None else collect
    if not collect:
        return []

    root = Path(root).resolve()
    if root.is_file():
        return []

    base = detect_base(root)
    walk_root = base if base != root.parent else root
    skip = {Path(p).resolve() for p in skip_dirs if Path(p).exists()}

    data_files = []
    stack = [walk_root]
    visited: set[Path] = set()

    while stack:
        cur = stack.pop()
        try:
            resolved_cur = cur.resolve()
        except OSError:
            continue
        if resolved_cur in visited:
            continue
        visited.add(resolved_cur)

        try:
            entries = sorted(cur.iterdir(), key=lambda p: p.name)
        except OSError as exc:
            logger.error("Cannot list {}: {}", cur, exc)
            continue

        for entry in entries:
            if entry.is_dir():
                if entry.name in EXCLUDED_DIRS or entry.name.endswith(".egg-info"):
                    try:
                        rel = entry.relative_to(walk_root).as_posix()
                    except ValueError:
                        rel = entry.as_posix()
                    if not _dir_included(rel, entry.name, include):
                        continue

                try:
                    resolved = entry.resolve()
                except OSError:
                    continue
                if resolved in skip or resolved in visited:
                    continue
                stack.append(entry)
            elif entry.is_file() and not entry.is_symlink():
                if entry.suffix == ".py":
                    continue

                try:
                    rel = entry.relative_to(walk_root).as_posix()
                except ValueError:
                    rel = entry.name

                if exclude and _matches(rel, entry.as_posix(), entry.name, exclude):
                    continue
                if any(fnmatch.fnmatch(entry.name, g) for g in EXCLUDED_FILE_GLOBS):
                    if not (include and _matches(rel, entry.as_posix(), entry.name, include)):
                        continue

                if _is_data_file(entry):
                    data_files.append(DataFile(entry, rel))
                    logger.debug("Found data file: {} -> {}", entry, rel)

    data_files.sort(key=lambda d: d.relative)
    if data_files:
        logger.info("Discovered {} data file(s)", len(data_files))
    return data_files


def read_pyproject_section(config_path: Path | str, section: str = "pyrefactor") -> dict:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - py<3.11
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            logger.warning("tomllib/tomli not available — cannot read {}", config_path)
            return {}

    try:
        with open(config_path, "rb") as f:
            data = tomllib.load(f)
        tool = data.get("tool", {}).get(section, {})
        return tool if isinstance(tool, dict) else {}
    except Exception as exc:
        logger.warning("Failed to parse config {}: {}", config_path, exc)
        return {}


def detect_pyproject_config(root: Path | str) -> dict:
    cur = Path(root).resolve() if Path(root).exists() else Path.cwd()
    if cur.is_file():
        cur = cur.parent

    for parent in [cur, *cur.parents]:
        pp = parent / "pyproject.toml"
        if pp.is_file():
            tool = read_pyproject_section(pp)
            if tool:
                logger.info("Loaded config from {}: {}", pp, tool)
            return tool

    for parent in [cur, *cur.parents]:
        if (parent / "setup.cfg").is_file() or (parent / "setup.py").is_file():
            logger.debug("Found setup.cfg/setup.py at {}", parent)
            break
    return {}
