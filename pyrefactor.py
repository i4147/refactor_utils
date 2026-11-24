#!/usr/bin/env python3
"""pyrefactor -- consolidate a Python package into one dependency-ordered file,
or pack it into a self-extracting zip bundle (--bundle).

Single-file edition of ``refactor_utils`` 4.0.1.  Requires ``libcst``
(``pip install libcst``); optional extras: ``tomli`` (py<3.11), ``tqdm``,
``black`` / ``ruff``.
"""

from __future__ import annotations

import argparse
import ast
import base64
import difflib
import fnmatch
import hashlib
import heapq
import io
import keyword
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tokenize
import unicodedata
import zipfile
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import (
    BrokenExecutor,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
)
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import libcst as cst
import libcst.metadata as md

__version__ = "4.0.1"

_RECURSION_LIMIT = 20000


class ConsolidationError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_FMT = "%(asctime)s | %(levelname)-8s | %(message)s"
_DATEFMT = "%H:%M:%S"
_LEVELS = {
    "TRACE": 5,
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "SUCCESS": logging.INFO + 5,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}
logging.addLevelName(5, "TRACE")
logging.addLevelName(logging.INFO + 5, "SUCCESS")
_ROOT_LOGGER = logging.getLogger("pyrefactor")
_ROOT_LOGGER.addHandler(logging.NullHandler())


def _fmt_message(message: object, args: tuple) -> str:
    if not args:
        return str(message)
    try:
        return str(message).format(*args)
    except Exception:
        try:
            return str(message) + " " + " ".join(str(a) for a in args)
        except Exception:
            return str(message)


class _ShimLogger:
    def __init__(self, inner: logging.Logger) -> None:
        self._inner = inner

    def _log(self, level: int, message: object, args: tuple, exc_info: bool = False) -> None:
        self._inner.log(level, _fmt_message(message, args), exc_info=exc_info)

    def trace(self, message: object, *args: object) -> None:
        self._log(5, message, args)

    def debug(self, message: object, *args: object) -> None:
        self._log(logging.DEBUG, message, args)

    def info(self, message: object, *args: object) -> None:
        self._log(logging.INFO, message, args)

    def success(self, message: object, *args: object) -> None:
        self._log(logging.INFO + 5, message, args)

    def warning(self, message: object, *args: object) -> None:
        self._log(logging.WARNING, message, args)

    def error(self, message: object, *args: object) -> None:
        self._log(logging.ERROR, message, args)

    def critical(self, message: object, *args: object) -> None:
        self._log(logging.CRITICAL, message, args)

    def exception(self, message: object, *args: object) -> None:
        self._log(logging.ERROR, message, args, exc_info=True)

    def remove(self) -> None:
        for handler in list(self._inner.handlers):
            if not isinstance(handler, logging.NullHandler):
                self._inner.removeHandler(handler)

    def add(self, sink, level: str = "DEBUG", rotation=None, enqueue=False, format=None):
        if isinstance(sink, (str, bytes)) or hasattr(sink, "__fspath__"):
            handler: logging.Handler = logging.FileHandler(sink, encoding="utf-8")
        else:
            handler = logging.StreamHandler(sink)
        handler.setLevel(_LEVELS.get(str(level).upper(), logging.DEBUG))
        handler.setFormatter(logging.Formatter(format or _FMT, datefmt=_DATEFMT))
        self._inner.addHandler(handler)
        return handler

    def set_level(self, level: str) -> None:
        self._inner.setLevel(_LEVELS.get(str(level).upper(), logging.INFO))


logger = _ShimLogger(_ROOT_LOGGER)


def setup_logging(level: str = "INFO", log_file=None) -> None:
    logger.remove()
    try:
        sys.stderr.reconfigure(line_buffering=True)
    except Exception:
        pass
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))
    requested = _LEVELS.get(str(level).upper(), logging.INFO)
    stream.setLevel(requested)
    logger._inner.setLevel(min(requested, 5))
    logger._inner.addHandler(stream)
    if log_file is not None:
        logger.add(log_file, level="DEBUG")


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

CONST = "const"
CLASS = "class"
FUNC = "func"
MISC = "misc"
MAIN = "main"


@dataclass(frozen=True)
class ImportSpec:
    module: str
    name: str | None = None
    alias: str | None = None
    is_from: bool = False
    star: bool = False
    relative: bool = False
    type_checking: bool = False

    @property
    def local_name(self) -> str | None:
        if self.star:
            return None
        if self.is_from:
            return self.alias or self.name
        return self.alias or self.module.split(".")[0]


@dataclass
class Ref:
    name: str
    kind: str
    eager: bool = True
    obj: str = ""
    specs: tuple[ImportSpec, ...] = ()


@dataclass(eq=False)
class Block:
    module: str
    kind: str
    names: list[str]
    nodes: list = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    order: int = 0
    bid: int = -1
    orig_path: str = ""
    start_line: int = 0
    end_line: int = 0

    @property
    def primary(self) -> str:
        return self.names[0] if self.names else ""

    def code(self) -> str:
        if not self.nodes:
            return ""
        return cst.Module(body=list(self.nodes)).code

    @property
    def source(self) -> bytes:
        return self.code().encode("utf-8")

    def merge(self, other: Block) -> None:
        self.nodes.extend(other.nodes)
        self.refs.extend(other.refs)
        for name in other.names:
            if name not in self.names:
                self.names.append(name)


@dataclass
class GuardBlock:
    text: str
    specs: list[ImportSpec] = field(default_factory=list)
    bound_names: list[str] = field(default_factory=list)
    conditional: bool = False


@dataclass
class ModuleInfo:
    module: str
    path: str
    is_pkg: bool = False
    imports: list[ImportSpec] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    guards: list[GuardBlock] = field(default_factory=list)
    declared_all: list[str] | None = None
    future_annotations: bool = False
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SourceFile:
    path: Path
    module: str
    is_pkg: bool = False


@dataclass(frozen=True)
class DataFile:
    path: Path
    relative: str
    kind: str = "data"


# ---------------------------------------------------------------------------
# Import section rendering
# ---------------------------------------------------------------------------

_STDLIB = getattr(sys, "stdlib_module_names", frozenset())
_LINE = 88


def _group(module: str) -> int:
    return 0 if module.split(".")[0] in _STDLIB else 1


def sort_key(name: str) -> tuple:
    stripped = name.lstrip("_")
    return (0 if name.startswith("_") else 1, stripped.lower(), name)


def _item_key(item: str) -> tuple:
    return sort_key(item.split(" as ")[0])


def _wrap(module: str, items: list[str]) -> str:
    line = f"from {module} import {', '.join(items)}"
    if len(line) <= _LINE:
        return line
    body = "".join(f"    {i},\n" for i in items)
    return f"from {module} import (\n{body})"


def _dedupe_aliases(items: Iterable[str]) -> set[str]:
    items = list(items)
    plain = {i for i in items if " as " not in i}
    aliased = {i for i in items if " as " in i}
    return {i if i.split(" as ", 1)[1] != i.split(" as ", 1)[0] else i.split(" as ", 1)[0] for i in aliased} | plain


def future_section(names: Iterable[str]) -> str:
    names = sorted(set(names))
    return f"from __future__ import {', '.join(names)}" if names else ""


def build_import_section(keys: set, ext_final: dict) -> str:
    plain: dict[int, set[tuple[str, str | None]]] = defaultdict(set)
    froms: dict[int, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for key in keys:
        kind = key[0]
        if kind == "import":
            _, mod, alias = key
            final = ext_final.get(key, alias)
            plain[_group(mod)].add((mod, final))
        elif kind == "from":
            _, mod, name, alias = key
            final = ext_final.get(key, alias or name)
            froms[_group(mod)][mod].add(name if final == name else f"{name} as {final}")
        elif kind == "star":
            froms[_group(key[1])][key[1]].add("*")
    chunks = []
    for grp in (0, 1):
        lines = []
        entries = plain.get(grp, set())
        dotted_tops = {m.split(".")[0] for m, a in entries if a is None and "." in m}
        for mod, alias in sorted(entries, key=lambda e: (e[0].lower(), e[1] or "")):
            if alias is None and "." not in mod and mod in dotted_tops:
                continue
            lines.append(f"import {mod}" + (f" as {alias}" if alias else ""))
        for mod in sorted(froms.get(grp, {}), key=str.lower):
            items = froms[grp][mod]
            if "*" in items:
                lines.append(f"from {mod} import *")
                items = items - {"*"}
            items = _dedupe_aliases(items)
            if items:
                lines.append(_wrap(mod, sorted(items, key=_item_key)))
        if lines:
            chunks.append("\n".join(lines))
    return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------


def topo_order(
    nodes: Iterable[Block],
    deps: Mapping[int, set[int]],
    rank: Callable[[Block], int],
    key: Callable[[Block], tuple],
) -> list[Block]:
    nodes = list(nodes)
    by_id = {b.bid: b for b in nodes}
    pending = {b.bid: {d for d in deps.get(b.bid, ()) if d in by_id and d != b.bid} for b in nodes}
    name_key = {b.bid: (sort_key(b.primary) if b.primary else (1, "", ""), b.bid) for b in nodes}
    dependents: dict[int, list[int]] = defaultdict(list)
    for bid, ds in pending.items():
        for d in ds:
            dependents[d].append(bid)
    heaps: dict[int, list] = defaultdict(list)
    queued: set[int] = set()
    done: set[int] = set()
    out: list[Block] = []

    def push(bid: int) -> None:
        b = by_id[bid]
        heapq.heappush(heaps[rank(b)], (key(b), name_key[bid], bid))
        queued.add(bid)

    for bid, ds in pending.items():
        if not ds:
            push(bid)
    cur = 0
    while len(done) < len(nodes):
        ranks = [r for r, h in heaps.items() if h]
        if not ranks:
            candidates = [i for i in pending if i not in done and i not in queued]
            if not candidates:
                candidates = [i for i in pending if i not in done]
                if not candidates:
                    break
            cand = min(
                candidates,
                key=lambda i: (len(pending[i]), rank(by_id[i]), key(by_id[i]), name_key[i]),
            )
            blocked = ", ".join(sorted(by_id[d].primary or f"<{by_id[d].kind}>" for d in pending[cand]))
            logger.warning(
                "Circular import-time dependency: emitting {} ({}) before {}",
                by_id[cand].primary or f"<{by_id[cand].kind}>",
                by_id[cand].module,
                blocked,
            )
            push(cand)
            continue
        r = cur if heaps.get(cur) else min(ranks)
        cur = r
        bid = heapq.heappop(heaps[r])[-1]
        done.add(bid)
        out.append(by_id[bid])
        for dep in dependents[bid]:
            pending[dep].discard(bid)
            if not pending[dep] and dep not in queued and dep not in done:
                push(dep)
    return out


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

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
    extra_globs = tuple(extra_globs)
    include = tuple(include)
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
            yield from _walk(entry, skip, extra_globs, include, follow_symlinks, base, visited_dirs)
            visited_dirs.discard(resolved_key)
        elif entry.suffix == ".py":
            posix = entry.as_posix()
            if _is_excluded(entry, posix, entry.name, extra_globs, include, rel):
                continue
            yield entry


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
    exclude = tuple(exclude)
    include = tuple(include)
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
    except ImportError:
        try:
            import tomli as tomllib
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


# ---------------------------------------------------------------------------
# Merged-file input (`# File: <relpath>` sections)
# ---------------------------------------------------------------------------

HEADER_RE = re.compile(r"^#[ \t]*File:[ \t]*(.+?)[ \t]*$", re.MULTILINE)


def parse_merged_file(text: str) -> list[tuple[str, str]]:
    matches = list(HEADER_RE.finditer(text))
    if not matches:
        raise ValueError(
            "no '# File: <path>' headers found; use a directory/package source for loose .py files instead"
        )
    out: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        rel = m.group(1).strip()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[m.end() : end]
        if body.startswith("\r\n"):
            body = body[2:]
        elif body.startswith("\n"):
            body = body[1:]
        out.append((rel, body))
    return out


def _derive_module_layout(pairs: list[tuple[str, str]], stem: str) -> tuple[str, list[PurePosixPath]]:
    rels = [PurePosixPath(rel.replace("\\", "/")) for rel, _ in pairs]
    # Strip container directories (e.g. ``src/``) that are not packages themselves.
    while rels and all(len(p.parts) >= 2 for p in rels):
        first = rels[0].parts[0]
        if any(p.parts[0] != first for p in rels):
            break
        if PurePosixPath(first, "__init__.py") in rels:
            break
        if not any(len(p.parts) == 3 and p.parts[2] == "__init__.py" for p in rels):
            break
        rels = [PurePosixPath(*p.parts[1:]) for p in rels]
    top = stem
    rel_paths = list(rels)
    if rels and all(len(p.parts) >= 2 for p in rels):
        common = rels[0].parts[0]
        if common.isidentifier() and all(p.parts[0] == common for p in rels):
            top = common
            rel_paths = [PurePosixPath(*p.parts[1:]) for p in rels]
    if not top.isidentifier():
        top = re.sub(r"\W+", "_", top).strip("_") or "merged"
    return top, rel_paths


def load_merged_sources(path: Path | str) -> tuple[list[SourceFile], Path]:
    src_path = Path(path)
    if not src_path.is_file():
        raise FileNotFoundError(f"merged file not found: {src_path}")
    text = src_path.read_text(encoding="utf-8")
    pairs = parse_merged_file(text)
    py_pairs = []
    for rel, body in pairs:
        if rel.endswith(".py"):
            py_pairs.append((rel, body))
        else:
            logger.warning("merged file entry {!r} is not a .py file; skipping", rel)
    if not py_pairs:
        raise ValueError(f"no python modules found inside merged file {src_path}")
    top, rel_paths = _derive_module_layout(py_pairs, src_path.stem)
    root = Path(tempfile.mkdtemp(prefix="pyrefactor_merged_"))
    pkg_root = root / top
    files: list[SourceFile] = []
    for (rel, body), rel_path in zip(py_pairs, rel_paths):
        target = pkg_root.joinpath(*rel_path.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        parts = [p for p in rel_path.with_suffix("").parts if p not in (".", "..")]
        is_pkg = bool(parts) and parts[-1] == "__init__"
        if is_pkg:
            parts = parts[:-1]
        module = top if not parts else ".".join([top, *parts])
        files.append(SourceFile(target, module, is_pkg))
    logger.info(
        "Loaded {} module(s) from merged file {} (staged under {})",
        len(files),
        src_path,
        root,
    )
    return files, root


# ---------------------------------------------------------------------------
# Modernization passes (six -> py3, os.path -> pathlib)
# ---------------------------------------------------------------------------

SIX_ATTR_MAP: dict[str, str] = {
    "text_type": "str",
    "binary_type": "bytes",
    "class_types": "type",
    "integer_types": "int",
    "string_types": "str",
}
SIX_MOVES_MAP: dict[str, str] = {
    "six.moves.urllib.request": "urllib.request",
    "six.moves.urllib.error": "urllib.error",
    "six.moves.urllib.parse": "urllib.parse",
    "six.moves.urllib.response": "urllib.response",
    "six.moves.urllib": "urllib",
    "six.moves.http_client": "http.client",
    "six.moves.cPickle": "pickle",
    "six.moves.copyreg": "copyreg",
    "six.moves.queue": "queue",
    "six.moves.range": "builtins",
    "six.moves.map": "builtins",
    "six.moves.zip": "builtins",
    "six.moves.input": "builtins",
    "six.moves": "builtins",
}
OSPATH_PROPERTY_MAP: dict[str, str] = {"dirname": "parent", "basename": "name"}
OSPATH_METHOD_MAP: dict[str, str] = {
    "exists": "exists",
    "isdir": "is_dir",
    "isfile": "is_file",
    "islink": "is_symlink",
    "abspath": "resolve",
    "realpath": "resolve",
    "expanduser": "expanduser",
    "remove": "unlink",
    "unlink": "unlink",
    "stat": "stat",
    "mkdir": "mkdir",
}
OSPATH_MAKEDIRS = "makedirs"
OSPATH_GETSIZE = "getsize"
_PASSES = ("six", "ospath")


def _dotted(node) -> str | None:
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        left = _dotted(node.value)
        return f"{left}.{node.attr.value}" if left else None
    return None


def _name(value: str) -> cst.Name:
    return cst.Name(value=value)


def _attr(value: cst.BaseExpression, name: str) -> cst.Attribute:
    return cst.Attribute(value=value, attr=_name(name))


def _call(func: cst.BaseExpression, args: list[cst.Arg] | None = None) -> cst.Call:
    return cst.Call(func=func, args=args or [])


def _arg(value: cst.BaseExpression) -> cst.Arg:
    return cst.Arg(value=value)


def _wrap_path(arg: cst.BaseExpression) -> cst.Call:
    return _call(_name("Path"), [_arg(arg)])


def _is_os_path(node: cst.BaseExpression, bare_path: bool) -> bool:
    if isinstance(node, cst.Attribute) and isinstance(node.value, cst.Name):
        if node.value.value == "os" and node.attr.value == "path":
            return True
        if bare_path and node.value.value == "path":
            return True
    return False


def _module_bare_path(tree: cst.Module) -> bool:
    for stmt in tree.body:
        if not isinstance(stmt, cst.SimpleStatementLine):
            continue
        for small in stmt.body:
            if isinstance(small, cst.ImportFrom) and not small.relative and _dotted(small.module) == "os":
                if isinstance(small.names, cst.ImportStar):
                    continue
                for alias in small.names:
                    if isinstance(alias, cst.ImportAlias) and alias.name.value == "path":
                        return True
    return False


def _has_path_import(tree: cst.Module) -> bool:
    for stmt in tree.body:
        if not isinstance(stmt, cst.SimpleStatementLine):
            continue
        for small in stmt.body:
            if isinstance(small, cst.ImportFrom) and not small.relative and _dotted(small.module) == "pathlib":
                if isinstance(small.names, cst.ImportStar):
                    return True
                for alias in small.names:
                    if isinstance(alias, cst.ImportAlias) and alias.name.value == "Path" and alias.asname is None:
                        return True
    return False


def _ensure_path_import(tree: cst.Module) -> cst.Module:
    if "Path(" not in tree.code or _has_path_import(tree):
        return tree
    body = list(tree.body)
    idx = 0
    if body and isinstance(body[0], cst.SimpleStatementLine) and len(body[0].body) == 1:
        first = body[0].body[0]
        if isinstance(first, cst.Expr) and isinstance(first.value, (cst.SimpleString, cst.ConcatenatedString)):
            idx = 1
    while idx < len(body):
        stmt = body[idx]
        if (
            isinstance(stmt, cst.SimpleStatementLine)
            and len(stmt.body) == 1
            and isinstance(stmt.body[0], cst.ImportFrom)
            and _dotted(stmt.body[0].module) == "__future__"
        ):
            idx += 1
            continue
        break
    body.insert(idx, cst.parse_statement("from pathlib import Path\n"))
    return tree.with_changes(body=body)


class _SixImportRewriter(cst.CSTTransformer):
    def leave_ImportFrom(self, original_node, updated_node):
        module = _dotted(updated_node.module) if updated_node.module else ""
        if updated_node.relative:
            return updated_node
        if module in SIX_MOVES_MAP:
            mapped = SIX_MOVES_MAP[module]
            if mapped == "builtins":
                return cst.RemoveFromParent()
            return updated_node.with_changes(module=cst.parse_expression(mapped))
        if module == "six":
            return cst.RemoveFromParent()
        return updated_node

    def leave_Import(self, original_node, updated_node):
        names = list(updated_node.names)
        kept = [a for a in names if _dotted(a.name) != "six"]
        if not kept:
            return cst.RemoveFromParent()
        if len(kept) == len(names):
            return updated_node
        kept[-1] = kept[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)
        return updated_node.with_changes(names=tuple(kept))


class _SixTransformer(cst.CSTTransformer):
    def leave_Attribute(self, original_node, updated_node):
        if isinstance(updated_node.value, cst.Name) and updated_node.value.value == "six":
            attr = updated_node.attr.value
            if attr == "PY2":
                return _name("False")
            if attr == "PY3":
                return _name("True")
            if attr in SIX_ATTR_MAP:
                return _name(SIX_ATTR_MAP[attr])
        return updated_node

    def leave_Call(self, original_node, updated_node):
        func = updated_node.func
        if not (isinstance(func, cst.Attribute) and isinstance(func.value, cst.Name) and func.value.value == "six"):
            return updated_node
        method = func.attr.value
        args = list(updated_node.args)
        if method in ("iteritems", "iterkeys", "itervalues") and len(args) == 1:
            mapped = {"iteritems": "items", "iterkeys": "keys", "itervalues": "values"}[method]
            return _call(_attr(args[0].value, mapped))
        if method == "u" and len(args) == 1:
            return args[0].value
        return updated_node


class _OsPathTransformer(cst.CSTTransformer):
    def __init__(self, bare_path: bool = False) -> None:
        self.bare_path = bare_path

    def leave_Call(self, original_node, updated_node):
        func = updated_node.func
        if not isinstance(func, cst.Attribute) or not _is_os_path(func.value, self.bare_path):
            return updated_node
        method = func.attr.value
        args = list(updated_node.args)
        if any(a.keyword is not None or a.star for a in args):
            return updated_node
        if method == "join" and len(args) >= 1:
            base = _wrap_path(args[0].value)
            if len(args) == 1:
                return base
            return _call(_attr(base, "joinpath"), [_arg(a.value) for a in args[1:]])
        if method in OSPATH_PROPERTY_MAP and len(args) == 1:
            return _attr(_wrap_path(args[0].value), OSPATH_PROPERTY_MAP[method])
        if method in OSPATH_METHOD_MAP and len(args) == 1:
            return _call(_attr(_wrap_path(args[0].value), OSPATH_METHOD_MAP[method]))
        if method == OSPATH_MAKEDIRS and len(args) == 1:
            return _call(
                _attr(_wrap_path(args[0].value), "mkdir"),
                [cst.Arg(value=_name("True"), keyword=_name("parents"))],
            )
        if method == OSPATH_GETSIZE and len(args) == 1:
            return _attr(_call(_attr(_wrap_path(args[0].value), "stat")), "st_size")
        return updated_node

    def leave_Subscript(self, original_node, updated_node):
        slices = list(updated_node.slice)
        if len(slices) != 1:
            return updated_node
        index = slices[0].slice
        if not (isinstance(index, cst.Index) and isinstance(index.value, cst.Integer) and index.value.value == "1"):
            return updated_node
        call = updated_node.value
        if not (isinstance(call, cst.Call) and len(call.args) == 1):
            return updated_node
        if not (
            isinstance(call.func, cst.Attribute)
            and call.func.attr.value == "splitext"
            and _is_os_path(call.func.value, self.bare_path)
        ):
            return updated_node
        return _attr(_wrap_path(call.args[0].value), "suffix")


@dataclass
class ModernizeReport:
    applied: tuple[str, ...]
    changed: bool


def modernize_tree(tree: cst.Module, passes) -> tuple[cst.Module, ModernizeReport]:
    if isinstance(passes, str):
        wanted = [p.strip() for p in passes.split(",") if p.strip()]
    else:
        wanted = list(passes)
    bad = [p for p in wanted if p not in _PASSES]
    if bad:
        raise ValueError(f"unknown modernize pass(es): {bad!r}; valid: {list(_PASSES)}")
    if not wanted:
        return tree, ModernizeReport(applied=(), changed=False)
    before = tree.code
    current = tree
    if "ospath" in wanted:
        bare = _module_bare_path(current)
        rewritten = current.visit(_OsPathTransformer(bare_path=bare))
        if rewritten.code != current.code:
            rewritten = _ensure_path_import(rewritten)
        current = rewritten
    if "six" in wanted:
        current = current.visit(_SixImportRewriter())
        current = current.visit(_SixTransformer())
    changed = current.code != before
    if changed:
        logger.info("Applied modernization pass(es): {}", ", ".join(wanted))
    return current, ModernizeReport(applied=tuple(wanted), changed=changed)


# ---------------------------------------------------------------------------
# Embedded resources
# ---------------------------------------------------------------------------

TEXT_RESOURCE_EXTENSIONS = {
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
    ".xml",
    ".html",
    ".htm",
    ".css",
    ".js",
    ".md",
    ".rst",
    ".sql",
    ".graphql",
    ".proto",
    ".env",
    ".example",
    ".log",
    ".sh",
    ".bat",
    ".pem",
    ".key",
    ".crt",
    ".svg",
}
BINARY_RESOURCE_EXTENSIONS = {
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
    ".icns",
    ".bmp",
    ".webp",
    ".pdf",
    ".zip",
    ".gz",
    ".bz2",
    ".xz",
    ".tar",
    ".7z",
    ".rar",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".pickle",
    ".pkl",
    ".bin",
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".pyd",
    ".class",
    ".jar",
    ".mp3",
    ".wav",
    ".ogg",
    ".flac",
    ".mp4",
    ".avi",
    ".mov",
}

_ACCESSOR_TEMPLATE = '''\
# Embedded at consolidation time: text files as plain text, binary as base64.
import base64 as _rs_base64
import contextlib as _rs_contextlib
import shutil as _rs_shutil
import tempfile as _rs_tempfile
from pathlib import Path as _rs_Path

_RESOURCES_TEXT = {{
{text_entries}}}

_RESOURCES_BINARY = {{
{binary_entries}}}


def {get_name}(name, *, encoding=None):
    """Return an embedded resource by name.

    Text resources are returned as ``str`` (pass ``encoding`` to get
    ``bytes``); binary resources are returned as decoded ``bytes`` (pass
    ``encoding`` to get ``str``).
    """
    if name in _RESOURCES_TEXT:
        text = _RESOURCES_TEXT[name]
        return text.encode(encoding) if encoding else text
    if name in _RESOURCES_BINARY:
        raw = _rs_base64.b64decode(_RESOURCES_BINARY[name])
        return raw.decode(encoding) if encoding else raw
    available = sorted(set(_RESOURCES_TEXT) | set(_RESOURCES_BINARY))
    raise KeyError(
        "resource not embedded: %r (available: %s)" % (name, ", ".join(available) or "none")
    )


def {list_name}():
    """Sorted names of all resources embedded in this file."""
    return sorted(set(_RESOURCES_TEXT) | set(_RESOURCES_BINARY))


@_rs_contextlib.contextmanager
def {path_name}(name):
    """Materialise an embedded resource as a real temporary file path."""
    data = {get_name}(name)
    if isinstance(data, str):
        data = data.encode("utf-8")
    tmpdir = _rs_tempfile.mkdtemp(prefix="consolidated_res_")
    path = _rs_Path(tmpdir) / (_rs_Path(name).name or "resource.bin")
    try:
        with open(path, "wb") as fh:
            fh.write(data)
        yield path
    finally:
        _rs_shutil.rmtree(tmpdir, ignore_errors=True)
'''


def classify_resource(path: Path, raw: bytes | None = None) -> str:
    ext = path.suffix.lower()
    if ext in BINARY_RESOURCE_EXTENSIONS:
        return "binary"
    if ext in TEXT_RESOURCE_EXTENSIONS:
        return "text"
    if raw is None:
        try:
            raw = path.read_bytes()[:4096]
        except OSError:
            return "binary"
    if b"\x00" in raw:
        return "binary"
    try:
        raw.decode("utf-8")
        return "text"
    except UnicodeDecodeError:
        return "binary"


def _chunk_literal(value: str, chunk: int = 100) -> str:
    pieces = [value[i : i + chunk] for i in range(0, len(value), chunk)] or [""]
    rendered = [repr(p) for p in pieces]
    if len(rendered) == 1:
        return rendered[0]
    return "(\n    " + "\n    ".join(rendered) + "\n)"


def _text_literal(text: str) -> str:
    lines = text.split("\n")
    if len(lines) <= 1 and len(text) <= 100:
        return repr(text)
    rendered = [repr(line + "\n") for line in lines[:-1]]
    if lines[-1]:
        rendered.append(repr(lines[-1]))
    if not rendered:
        rendered = [repr("")]
    if len(rendered) == 1:
        return rendered[0]
    return "(\n    " + "\n    ".join(rendered) + "\n)"


def build_resources_code(
    payloads: Sequence[tuple[str, bytes]],
    taken_names: Iterable[str] = (),
    get_name: str = "get_resource",
    path_name: str = "resource_path",
    list_name: str = "list_resources",
) -> str:
    taken = set(taken_names)
    base_for = {
        "get_resource": ("consolidated_get_resource", "_get_resource"),
        "resource_path": ("consolidated_resource_path", "_resource_path"),
        "list_resources": ("consolidated_list_resources", "_list_resources"),
    }

    def resolve(name: str) -> str:
        if name not in taken:
            taken.add(name)
            return name
        primary, fallback = base_for[name]
        cand = primary if primary not in taken else fallback
        n = 2
        while cand in taken:
            cand = f"{fallback}_{n}"
            n += 1
        taken.add(cand)
        return cand

    get_name = resolve(get_name)
    path_name = resolve(path_name)
    list_name = resolve(list_name)
    text_entries: list[str] = []
    binary_entries: list[str] = []
    for name, raw in sorted(payloads):
        kind = classify_resource(Path(name), raw)
        if kind == "text":
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                kind = "binary"
            else:
                text_entries.append(f"    {name!r}: {_text_literal(text)},")
                continue
        encoded = base64.b64encode(raw).decode("ascii")
        binary_entries.append(f"    {name!r}: {_chunk_literal(encoded)},")
    return _ACCESSOR_TEMPLATE.format(
        text_entries="\n".join(text_entries) if text_entries else "",
        binary_entries="\n".join(binary_entries) if binary_entries else "",
        get_name=get_name,
        path_name=path_name,
        list_name=list_name,
    )


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_MAIN_RE = re.compile(r"""^(?:__name__\s*==\s*(['"])__main__\1|(['"])__main__\2\s*==\s*__name__)$""")
_TC_NAMES = {"TYPE_CHECKING", "typing.TYPE_CHECKING", "t.TYPE_CHECKING", "typing_extensions.TYPE_CHECKING"}
_ALL_METHODS = {"append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse"}
_DYNAMIC_PATTERNS = {
    "importlib": re.compile(r"\bimportlib\b"),
    "__import__": re.compile(r"\b__import__\b"),
    "pkgutil": re.compile(r"\bpkgutil\.(?:iter_modules|walk_packages)\b"),
    "globals": re.compile(r"\bglobals\s*\("),
    "locals": re.compile(r"\blocals\s*\("),
    "eval": re.compile(r"\beval\s*\("),
    "exec": re.compile(r"\bexec\s*\("),
    "getattr": re.compile(r"\bgetattr\s*\("),
    "__getattr__": re.compile(r"__getattr__"),
    "__dir__": re.compile(r"__dir__"),
    "monkeypatch": re.compile(r"monkeypatch"),
}
_COMPOUND = (cst.If, cst.Try, cst.For, cst.While, cst.With, cst.Match)


def render_stmt(node: cst.CSTNode) -> str:
    return cst.Module(body=[node]).code


def render_expr(node) -> str:
    return render_stmt(cst.SimpleStatementLine(body=[cst.Expr(value=node)])).strip()


def _resolve_relative(current: str, is_pkg: bool, level: int, rest: str) -> str:
    parts = current.split(".") if is_pkg else current.split(".")[:-1]
    strip = level - 1
    base = [] if strip > len(parts) else parts[: len(parts) - strip]
    return ".".join(base + ([rest] if rest else []))


def parse_import(stmt: cst.CSTNode, module: str, is_pkg: bool, type_checking: bool = False) -> list[ImportSpec]:
    if isinstance(stmt, cst.Import):
        specs = []
        for alias in stmt.names:
            name = _dotted(alias.name) or ""
            alias_str = alias.asname.name.value if alias.asname else None
            specs.append(ImportSpec(name, alias=alias_str, type_checking=type_checking))
        return specs
    if isinstance(stmt, cst.ImportFrom):
        level = len(stmt.relative)
        rest = _dotted(stmt.module) if stmt.module is not None else ""
        mod = _resolve_relative(module, is_pkg, level, rest or "") if level else (rest or "")
        if isinstance(stmt.names, cst.ImportStar):
            return [ImportSpec(mod, is_from=True, star=True, relative=bool(level), type_checking=type_checking)]
        specs = []
        for alias in stmt.names:
            name = _dotted(alias.name) or ""
            alias_str = alias.asname.name.value if alias.asname else None
            specs.append(
                ImportSpec(
                    mod,
                    name=name,
                    alias=alias_str,
                    is_from=True,
                    relative=bool(level),
                    type_checking=type_checking,
                )
            )
        return specs
    return []


def _is_bare_string(value) -> bool:
    if isinstance(value, cst.SimpleString):
        return True
    if isinstance(value, cst.FormattedString):
        return not any(isinstance(p, cst.FormattedStringExpression) for p in value.parts)
    if isinstance(value, cst.ConcatenatedString):
        return _is_bare_string(value.left) and _is_bare_string(value.right)
    return False


def _param_names(params: cst.Parameters) -> set[str]:
    names: set[str] = set()
    for group in (params.params, params.posonly_params, params.kwonly_params):
        for p in group:
            names.add(p.name.value)
    for p in (params.star_arg, params.star_kwarg):
        if isinstance(p, cst.Param):
            names.add(p.name.value)
    return names


def _lhs_names(node) -> list[str] | None:
    if isinstance(node, cst.Name):
        return [node.value]
    if isinstance(node, (cst.Tuple, cst.List)):
        out: list[str] = []
        for element in node.elements:
            inner = element.value if isinstance(element, cst.Element) else element
            got = _lhs_names(inner)
            if got is None:
                return None
            out += got
        return out
    if isinstance(node, cst.StarredElement):
        return _lhs_names(node.value)
    return None


def _target_names(stmt) -> list[str] | None:
    if isinstance(stmt, cst.Assign):
        names: list[str] = []
        for target in stmt.targets:
            got = _lhs_names(target.target)
            if got is None:
                return None
            names += got
        return names
    if isinstance(stmt, (cst.AugAssign, cst.AnnAssign)):
        return _lhs_names(stmt.target)
    return None


def _name_ids(node) -> set[int]:
    out: set[int] = set()
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, cst.Name):
            out.add(id(cur))
        for child in cur.children:
            stack.append(child)
    return out


def _match_binding_ids(pattern) -> set[int]:
    out: set[int] = set()
    stack = [pattern]
    while stack:
        cur = stack.pop()
        if isinstance(cur, (cst.MatchAs, cst.MatchStar)):
            if cur.name is not None:
                out.add(id(cur.name))
        elif isinstance(cur, cst.MatchMapping):
            if cur.rest is not None:
                out.add(id(cur.rest))
        for child in cur.children:
            stack.append(child)
    return out


def _scan_imports_defs(node) -> tuple[list, bool]:
    imports: list = []
    has_def = False
    stack = [node]
    while stack:
        cur = stack.pop()
        for child in cur.children:
            if isinstance(child, (cst.Import, cst.ImportFrom)):
                imports.append(child)
            elif isinstance(child, (cst.FunctionDef, cst.ClassDef)):
                has_def = True
            else:
                stack.append(child)
    return imports, has_def


def _bound_names(node) -> list[str]:
    names: list[str] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        for child in cur.children:
            if isinstance(child, (cst.FunctionDef, cst.ClassDef)):
                names.append(child.name.value)
            elif isinstance(child, (cst.Assign, cst.AugAssign, cst.AnnAssign)):
                got = _target_names(child)
                if got:
                    names += got
            else:
                stack.append(child)
    return list(dict.fromkeys(names))


def _string_literals(node) -> list[str]:
    found: list[str] = []
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, cst.SimpleString):
            try:
                lit = ast.literal_eval(cur.value)
                if isinstance(lit, str):
                    found.append(lit)
            except (ValueError, SyntaxError):
                pass
        for child in cur.children:
            stack.append(child)
    return found


def _collect_all_stmt(small, info: ModuleInfo) -> bool:
    if isinstance(small, cst.Assign) and len(small.targets) == 1:
        target = small.targets[0].target
        if isinstance(target, cst.Name) and target.value == "__all__":
            info.declared_all = (info.declared_all or []) + _string_literals(small.value)
            return True
        return False
    if isinstance(small, cst.Expr) and isinstance(small.value, cst.Call):
        func = small.value.func
        if isinstance(func, cst.Attribute) and func.attr.value in _ALL_METHODS:
            if _dotted(func.value) == "__all__":
                found: list[str] = []
                for arg in small.value.args:
                    found += _string_literals(arg.value)
                info.declared_all = (info.declared_all or []) + found
                info.warnings.append(
                    f"{info.module}: mutated __all__ ('{func.attr.value}') — static analysis "
                    "may be incomplete; exported names are recomputed from definitions"
                )
                return True
    return False


class _DocstringStripper(cst.CSTTransformer):
    @staticmethod
    def _is_doc_line(stmt) -> bool:
        return (
            isinstance(stmt, cst.SimpleStatementLine)
            and len(stmt.body) == 1
            and isinstance(stmt.body[0], cst.Expr)
            and _is_bare_string(stmt.body[0].value)
        )

    def _strip_stmts(self, stmts):
        if stmts and self._is_doc_line(stmts[0]):
            return stmts[1:]
        return stmts

    def leave_Module(self, original_node, updated_node):
        return updated_node.with_changes(body=self._strip_stmts(updated_node.body))

    def _leave_suite_owner(self, updated_node):
        body = updated_node.body
        if isinstance(body, cst.IndentedBlock):
            stmts = self._strip_stmts(body.body)
            if stmts is not body.body:
                if not stmts:
                    stmts = [cst.SimpleStatementLine(body=[cst.Pass()])]
                return updated_node.with_changes(body=body.with_changes(body=stmts))
        elif isinstance(body, cst.SimpleStatementSuite):
            smalls = body.body
            if smalls and isinstance(smalls[0], cst.Expr) and _is_bare_string(smalls[0].value):
                smalls = smalls[1:] or [cst.Pass()]
                return updated_node.with_changes(body=body.with_changes(body=smalls))
        return updated_node

    def leave_ClassDef(self, original_node, updated_node):
        return self._leave_suite_owner(updated_node)

    def leave_FunctionDef(self, original_node, updated_node):
        return self._leave_suite_owner(updated_node)


class _CommentStripper(cst.CSTTransformer):
    def leave_EmptyLine(self, original_node, updated_node):
        if updated_node.comment is not None:
            return cst.RemovalSentinel.REMOVE
        return updated_node

    def leave_TrailingWhitespace(self, original_node, updated_node):
        if updated_node.comment is not None:
            return updated_node.with_changes(comment=None, whitespace=cst.SimpleWhitespace(""))
        return updated_node


class _Ctx:
    __slots__ = ("lazy", "shadow", "cls")

    def __init__(self, lazy: bool, shadow: frozenset, cls: bool):
        self.lazy = lazy
        self.shadow = shadow
        self.cls = cls


class RefWalker(cst.CSTTransformer):
    def __init__(
        self,
        module: str,
        is_pkg: bool,
        collect: bool,
        info: ModuleInfo | None = None,
        resolver=None,
        env: dict | None = None,
        warned: set | None = None,
    ):
        self.module = module
        self.is_pkg = is_pkg
        self.collect = collect
        self.info = info
        self.resolver = resolver
        self.env = env or {}
        self.warned = warned if warned is not None else set()
        self.refs: list[Ref] = []
        self._skip: set[int] = set()
        self._ctxs: list[_Ctx] = [_Ctx(False, frozenset(), False)]
        self._frames: list[dict] = []
        self._cls_pops: list[int] = []

    def visit_FunctionDef(self, node) -> bool:
        self._skip.add(id(node.name))
        self._frames.append({"kind": "func", "params": None, "scope": False, "base_shadow": self._ctxs[-1].shadow})
        return True

    def _maybe_rename_def_name(self, original_node, updated_node):
        if self.collect:
            return updated_node
        name = original_node.name.value
        target = self.env.get(name)
        if target is None:
            return updated_node
        final = self.resolver._target_name(target, name)
        if final is None or final == name:
            return updated_node
        return updated_node.with_changes(name=cst.Name(final))

    def leave_FunctionDef(self, original_node, updated_node):
        frame = self._frames.pop()
        if frame["scope"]:
            self._ctxs.pop()
        return self._maybe_rename_def_name(original_node, updated_node)

    def visit_Parameters(self, node) -> bool:
        if self._frames and self._frames[-1]["kind"] == "func":
            frame = self._frames[-1]
            if frame["params"] is None:
                frame["params"] = frozenset(_param_names(node))
        for group in (node.params, node.posonly_params, node.kwonly_params):
            for p in group:
                self._skip.add(id(p.name))
        for p in (node.star_arg, node.star_kwarg):
            if isinstance(p, cst.Param):
                self._skip.add(id(p.name))
        return True

    def _push_func_scope(self) -> bool:
        if self._frames and self._frames[-1]["kind"] == "func":
            frame = self._frames[-1]
            if not frame["scope"]:
                shadow = frame["base_shadow"] | (frame["params"] or frozenset())
                self._ctxs.append(_Ctx(True, frozenset(shadow), False))
                frame["scope"] = True
                return True
        return False

    def visit_IndentedBlock(self, node) -> bool:
        self._push_func_scope()
        return True

    def visit_SimpleStatementSuite(self, node) -> bool:
        self._push_func_scope()
        return True

    def visit_ClassDef(self, node) -> bool:
        self._skip.add(id(node.name))
        cur = self._ctxs[-1]
        self._ctxs.append(_Ctx(cur.lazy, cur.shadow, True))
        self._cls_pops.append(id(node))
        return True

    def leave_ClassDef(self, original_node, updated_node):
        if self._cls_pops and self._cls_pops[-1] == id(original_node):
            self._cls_pops.pop()
            self._ctxs.pop()
        return self._maybe_rename_def_name(original_node, updated_node)

    def visit_Lambda(self, node) -> bool:
        cur = self._ctxs[-1]
        shadow = cur.shadow | frozenset(_param_names(node.params))
        self._ctxs.append(_Ctx(True, frozenset(shadow), False))
        self._frames.append({"kind": "lambda", "scope": True})
        return True

    def leave_Lambda(self, original_node, updated_node):
        self._frames.pop()
        self._ctxs.pop()
        return updated_node

    def visit_ExceptHandler(self, node) -> bool:
        if node.name is not None:
            self._skip.add(id(node.name))
        return True

    def visit_For(self, node) -> bool:
        self._skip |= _name_ids(node.target)
        return True

    def visit_With(self, node) -> bool:
        for item in node.items:
            if item.asname is not None:
                self._skip.add(id(item.asname.name))
        return True

    def visit_NamedExpr(self, node) -> bool:
        self._skip.add(id(node.target))
        return True

    def visit_MatchCase(self, node) -> bool:
        self._skip |= _match_binding_ids(node.pattern)
        return True

    def visit_Assign(self, node) -> bool:
        if self._ctxs[-1].cls:
            for target in node.targets:
                self._skip |= _name_ids(target.target)
        return True

    def visit_AnnAssign(self, node) -> bool:
        if self._ctxs[-1].cls:
            self._skip |= _name_ids(node.target)
        return True

    visit_AugAssign = visit_AnnAssign

    def visit_Attribute(self, node) -> bool:
        self._skip.add(id(node.attr))
        return True

    def visit_Arg(self, node) -> bool:
        if node.keyword is not None:
            self._skip.add(id(node.keyword))
        return True

    def visit_Import(self, node) -> bool:
        self._skip |= _name_ids(node)
        return True

    def visit_ImportFrom(self, node) -> bool:
        self._skip |= _name_ids(node)
        return True

    def leave_Name(self, original_node, updated_node):
        if id(original_node) in self._skip:
            return updated_node
        name = original_node.value
        ctx = self._ctxs[-1]
        if name in ctx.shadow:
            return updated_node
        if name == "__file__" and self.collect and "file" not in self.warned:
            self.warned.add("file")
            self.info.warnings.append(f"{self.module}: uses __file__, which now points at the consolidated file")
        if self.collect:
            self.refs.append(Ref(name, "name", eager=not ctx.lazy))
            return updated_node
        target = self.env.get(name)
        if target is None:
            return updated_node
        final = self.resolver._target_name(target, name)
        if final is None or final == name:
            return updated_node
        return updated_node.with_changes(value=final)

    def leave_Attribute(self, original_node, updated_node):
        chain = _dotted(original_node.value)
        if chain is None:
            return updated_node
        ctx = self._ctxs[-1]
        if chain.split(".")[0] in ctx.shadow:
            return updated_node
        attr = original_node.attr.value
        if self.collect:
            self.refs.append(Ref(attr, "attr", eager=not ctx.lazy, obj=chain))
            return updated_node
        target = self.resolver._resolve_dotted(self.module, chain, attr)
        if target is None:
            return updated_node
        final = self.resolver._target_name(target, None)
        if final is None:
            return updated_node
        # Replace the whole ``module.attr`` expression: the internal module
        # import is removed from the output, so keeping ``module.`` would leave
        # an unbound name behind.
        return cst.Name(final, lpar=updated_node.lpar, rpar=updated_node.rpar)

    def _nested_ref(self, original_node) -> None:
        specs = tuple(parse_import(original_node, self.module, self.is_pkg))
        self.refs.append(Ref("", "nested_import", specs=specs))

    def leave_Import(self, original_node, updated_node):
        if self.collect:
            self._nested_ref(original_node)
            return updated_node
        specs = parse_import(original_node, self.module, self.is_pkg)
        if not any(self.resolver._is_internal(s) for s in specs):
            return updated_node
        kept = [alias for alias, spec in zip(updated_node.names, specs) if not self.resolver._is_internal(spec)]
        if not kept:
            return cst.Pass()
        kept[-1] = kept[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)
        return cst.Import(names=kept)

    def leave_ImportFrom(self, original_node, updated_node):
        if self.collect:
            self._nested_ref(original_node)
            return updated_node
        specs = parse_import(original_node, self.module, self.is_pkg)
        if not any(self.resolver._is_internal(s) for s in specs):
            return updated_node
        return cst.Pass()


def _detect_dynamic_patterns(text: str, info: ModuleInfo) -> None:
    for name, pat in _DYNAMIC_PATTERNS.items():
        if pat.search(text):
            if name in ("importlib", "__import__"):
                info.warnings.append(
                    f"{info.module}: uses {name} for dynamic imports — consolidation may miss dependencies"
                )
            elif name == "pkgutil":
                info.warnings.append(
                    f"{info.module}: uses pkgutil module discovery — plugin auto-discovery cannot work "
                    "inside a consolidated file; use --bundle for this package"
                )
            elif name in ("globals", "locals", "eval", "exec", "getattr"):
                info.warnings.append(f"{info.module}: uses {name}() — name resolution may be incomplete")
            elif name in ("__getattr__", "__dir__", "monkeypatch"):
                info.warnings.append(
                    f"{info.module}: defines/uses {name} — module-level attribute access may change semantics"
                )


def _collect_block_refs(info, node, warned) -> list[Ref]:
    walker = RefWalker(info.module, info.is_pkg, collect=True, info=info, warned=warned)
    node.visit(walker)
    return walker.refs


def _make_block(info, by_name, warned, kind, names, node, start, end) -> None:
    block = Block(info.module, kind, list(names), [node])
    block.order = len(info.blocks)
    block.orig_path = info.path
    block.start_line = start
    block.end_line = end
    block.refs = _collect_block_refs(info, node, warned)
    target = next((by_name[n] for n in names if n in by_name), None)
    if target is not None:
        target.merge(block)
        for n in names:
            by_name.setdefault(n, target)
        return
    info.blocks.append(block)
    for n in names:
        by_name[n] = block


def _add_compound(info, by_name, warned, stmt, start, end) -> None:
    code = render_stmt(stmt)
    imports, has_def = _scan_imports_defs(stmt)
    if imports and not has_def:
        specs = [spec for imp in imports for spec in parse_import(imp, info.module, info.is_pkg)]
        info.guards.append(GuardBlock(code, specs, _bound_names(stmt), True))
        return
    info.warnings.append(
        f"{info.module}: module-level {type(stmt).__name__} "
        "combines imports and definitions — emitted verbatim, "
        "names bound there are not rewritten"
    )
    _make_block(info, by_name, warned, MISC, _bound_names(stmt), stmt, start, end)


def _add_simple(info, by_name, warned, small, start, end, lead, trailing) -> None:
    if isinstance(small, (cst.Import, cst.ImportFrom)):
        info.imports.extend(parse_import(small, info.module, info.is_pkg))
        return
    if isinstance(small, cst.Expr) and _collect_all_stmt(small, info):
        return
    if isinstance(small, (cst.Assign, cst.AugAssign, cst.AnnAssign)):
        if _collect_all_stmt(small, info):
            return
        names = _target_names(small)
        if names:
            node = cst.SimpleStatementLine(body=[small], leading_lines=lead, trailing_whitespace=trailing)
            _make_block(info, by_name, warned, CONST, names, node, start, end)
            return
    if isinstance(small, cst.Expr) and _is_bare_string(small.value):
        return
    if isinstance(small, cst.Pass):
        return
    node = cst.SimpleStatementLine(body=[small], leading_lines=lead, trailing_whitespace=trailing)
    _make_block(info, by_name, warned, MISC, [], node, start, end)


def _add_type_checking(info, by_name, warned, node, positions) -> None:
    if isinstance(node.body, cst.IndentedBlock):
        body_start = positions[node.body].start.line
        body_stmts = list(node.body.body)
    else:
        body_start = positions[node].start.line
        body_stmts = [cst.SimpleStatementLine(body=[s]) for s in node.body.body]
    for s in body_stmts:
        code = render_stmt(s)
        nlines = code.count("\n")
        start = body_start
        body_start += nlines
        end = body_start - 1
        smalls = s.body if isinstance(s, cst.SimpleStatementLine) else None
        if smalls is not None:
            for small in smalls:
                if isinstance(small, (cst.Import, cst.ImportFrom)):
                    info.imports.extend(parse_import(small, info.module, info.is_pkg, type_checking=True))
                elif isinstance(small, cst.Pass):
                    continue
                elif _collect_all_stmt(small, info):
                    continue
                elif isinstance(small, cst.Expr) and _is_bare_string(small.value):
                    continue
                else:
                    names = _target_names(small) or []
                    kind = CONST if names else MISC
                    wrapped = cst.SimpleStatementLine(
                        body=[small],
                        leading_lines=s.leading_lines,
                        trailing_whitespace=s.trailing_whitespace,
                    )
                    _make_block(info, by_name, warned, kind, names, wrapped, start, end)
            continue
        if isinstance(s, (cst.FunctionDef, cst.ClassDef)):
            kind = CLASS if isinstance(s, cst.ClassDef) else FUNC
            _make_block(info, by_name, warned, kind, [s.name.value], s, start, end)
            continue
        imports, has_def = _scan_imports_defs(s)
        if imports and not has_def:
            specs = [
                spec for imp in imports for spec in parse_import(imp, info.module, info.is_pkg, type_checking=True)
            ]
            info.guards.append(GuardBlock(code, specs, _bound_names(s), True))
            continue
        _make_block(info, by_name, warned, MISC, _bound_names(s), s, start, end)


def _extract(info: ModuleInfo, tree: cst.Module, positions: dict) -> None:
    body = list(tree.body)
    starts = {id(s): positions[s].start.line for s in body}
    if tree.header and body:
        first = body[0]
        body[0] = first.with_changes(leading_lines=(*tree.header, *first.leading_lines))
        starts[id(body[0])] = starts[id(first)]
    by_name: dict[str, Block] = {}
    warned: set = set()
    for stmt in body:
        start = starts[id(stmt)]
        nlines = render_stmt(stmt).count("\n")
        end = start + nlines - 1
        if isinstance(stmt, cst.SimpleStatementLine):
            smalls = stmt.body
            if len(smalls) == 1:
                _add_simple(info, by_name, warned, smalls[0], start, end, stmt.leading_lines, stmt.trailing_whitespace)
            else:
                for i, small in enumerate(smalls):
                    _add_simple(
                        info,
                        by_name,
                        warned,
                        small,
                        start,
                        end,
                        stmt.leading_lines if i == 0 else (),
                        stmt.trailing_whitespace if i == len(smalls) - 1 else None,
                    )
        elif isinstance(stmt, (cst.FunctionDef, cst.ClassDef)):
            kind = CLASS if isinstance(stmt, cst.ClassDef) else FUNC
            _make_block(info, by_name, warned, kind, [stmt.name.value], stmt, start, end)
        elif isinstance(stmt, cst.If):
            cond = render_expr(stmt.test)
            if _MAIN_RE.match(cond):
                _make_block(info, by_name, warned, MAIN, [], stmt, start, end)
                continue
            if cond in _TC_NAMES:
                _add_type_checking(info, by_name, warned, stmt, positions)
                continue
            if "sys.platform" in cond or "sys.version" in cond or "TYPE_CHECKING" in cond:
                info.warnings.append(f"{info.module}: conditional import guarded by '{cond}' kept verbatim")
            _add_compound(info, by_name, warned, stmt, start, end)
        elif isinstance(stmt, _COMPOUND):
            _add_compound(info, by_name, warned, stmt, start, end)
        elif isinstance(stmt, cst.SimpleStatementSuite):
            for small in stmt.body:
                _add_simple(info, by_name, warned, small, start, end, (), None)
        else:
            _make_block(info, by_name, warned, MISC, [], stmt, start, end)
    _detect_dynamic_patterns(tree.code, info)
    _detect_runtime_semantics(info, tree.code)


def _detect_runtime_semantics(info: ModuleInfo, text: str) -> None:
    seen: set[str] = set()
    for block in info.blocks:
        if block.kind == MAIN:
            continue
        for ref in block.refs:
            if ref.kind == "name" and ref.name in ("__name__", "__package__", "__path__"):
                if ref.name not in seen:
                    seen.add(ref.name)
                    info.warnings.append(
                        f"{info.module}: uses {ref.name} — semantics change in consolidated file "
                        "(it now points at the consolidated module)"
                    )
    if "importlib.resources" in text or "pkg_resources" in text or ("open(" in text and "__file__" in text):
        info.warnings.append(
            f"{info.module}: does resource loading relative to __file__ — verify output "
            "handles bundled resources (see --embed-data)"
        )


def parse_module_file(
    path_str: str,
    module: str,
    is_pkg: bool,
    keep_docstrings: bool = True,
    keep_comments: bool = True,
) -> ModuleInfo:
    info = ModuleInfo(module=module, path=path_str, is_pkg=is_pkg)
    try:
        src = Path(path_str).read_bytes()
    except OSError as exc:
        info.errors.append(f"cannot read {path_str}: {exc}")
        return info
    if src.startswith(b"\xef\xbb\xbf"):
        src = src[3:]
    if b"\r\n" in src:
        info.warnings.append(f"{path_str}: contains CRLF line endings — normalized to LF in output")
        src = src.replace(b"\r\n", b"\n")
    try:
        src.decode("utf-8")
    except UnicodeDecodeError:
        info.warnings.append(f"{path_str}: non-UTF8 encoding detected — decoded with replacement characters")
    text = src.decode("utf-8", "replace")
    try:
        tree = cst.parse_module(text)
    except cst.ParserSyntaxError as exc:
        detail = getattr(exc, "message", None) or str(exc)
        info.errors.append(f"{path_str}: syntax error: {detail}")
        return info
    except Exception as exc:
        info.errors.append(f"{path_str}: parse failed: {exc!r}")
        return info
    try:
        if not keep_comments:
            tree = tree.visit(_CommentStripper())
        if not keep_docstrings:
            tree = tree.visit(_DocstringStripper())
        wrapper = md.MetadataWrapper(tree, unsafe_skip_copy=True)
        positions = wrapper.resolve(md.PositionProvider)
        _extract(info, tree, positions)
    except Exception as exc:
        info.errors.append(f"{path_str}: extraction failed: {exc!r}")
    return info


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

Target = tuple
_RANK = {CONST: 0, MISC: 0, CLASS: 1, FUNC: 2}
_SECTION = {CONST: "const", MISC: "const", CLASS: "class", FUNC: "func"}


@dataclass
class RenderedBlock:
    section: str
    kind: str
    text: str
    origin: str = ""
    module: str = ""


@dataclass
class Plan:
    exports: dict = field(default_factory=lambda: {"constants": [], "classes": [], "functions": []})
    renamed: list = field(default_factory=list)
    deduped: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    future_names: list = field(default_factory=list)
    imports_text: str = ""
    guards: list = field(default_factory=list)
    blocks: list = field(default_factory=list)
    resources_code: str = ""
    all_names: list = field(default_factory=list)


def _ext_local(key: tuple) -> str | None:
    if key[0] == "import":
        return key[2] or key[1].split(".")[0]
    if key[0] == "from":
        return key[3] or key[2]
    return None


def _ext_identity(key: tuple) -> tuple:
    if key[0] == "import":
        return ("modalias", key[1]) if key[2] else ("mod", key[1].split(".")[0])
    return ("from", key[1], key[2])


def _sanitize(text: str) -> str:
    return re.sub(r"\W", "_", text).strip("_")


def is_valid_identifier(name: str) -> bool:
    return name.isidentifier() and not keyword.iskeyword(name)


def make_identifier(text: str, fallback: str = "mod") -> str:
    text = unicodedata.normalize("NFKC", text)
    out = "".join(ch if (ch == "_" or ch.isalnum()) and f"a{ch}".isidentifier() else "_" for ch in text)
    if not out.strip("_"):
        out = fallback
    if not out[0].isidentifier():
        out = f"m{out}"
    if keyword.iskeyword(out):
        out += "_"
    return out


def _content_hash(block: Block) -> str:
    normalised = "\n".join(line.rstrip() for line in block.code().strip().splitlines() if line.strip())
    return hashlib.sha1(normalised.encode("utf-8")).hexdigest()


def _guard_body(text: str) -> list[str]:
    lines = text.splitlines()
    header = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("if ") and "__main__" in stripped:
            header = i
            break
    body = lines[header + 1 :] if header is not None else lines
    indents = [len(ln) - len(ln.lstrip()) for ln in body if ln.strip()]
    cut = min(indents) if indents else 0
    return [ln[cut:] if ln.strip() else "" for ln in body]


class Resolver:
    def __init__(
        self,
        modules: dict[str, ModuleInfo],
        *,
        entry: tuple = (),
        workers: int = 1,
        imports_mode: str = "prune",
        entry_keep_all: bool = False,
        main_name: str | None = None,
        generate_guard: bool = False,
        resources: list[tuple[str, bytes]] | None = None,
    ):
        self.modules = dict(sorted(modules.items()))
        self.entry = tuple(entry)
        self.workers = max(1, workers)
        self.imports_mode = imports_mode
        self.entry_keep_all = entry_keep_all
        self.main_name = main_name
        self.generate_guard = generate_guard
        self.resources = resources
        self.known = set(self.modules)
        for m in list(self.modules):
            parts = m.split(".")
            for i in range(1, len(parts)):
                self.known.add(".".join(parts[:i]))
        self.tops = {m.split(".")[0] for m in self.modules}
        self.mod_idx = {m: i for i, m in enumerate(self.modules)}
        self.single_top = next(iter(self.tops)) if len(self.tops) == 1 else None
        self.blocks: list[Block] = []
        for mi in self.modules.values():
            for b in mi.blocks:
                if b.kind == MAIN and self.entry and not self._entry_match(b.module):
                    if not self.entry_keep_all:
                        continue
                b.bid = len(self.blocks)
                self.blocks.append(b)
        self.def_block: dict[tuple[str, str], Block] = {}
        for b in self.blocks:
            for n in b.names:
                self.def_block[(b.module, n)] = b
        self.future_names: set[str] = set()
        self.import_table: dict[str, dict] = {}
        self.star_specs: dict[str, list] = {}
        for m, mi in self.modules.items():
            table: dict = {}
            stars: list = []
            for spec in mi.imports:
                if spec.module == "__future__":
                    if spec.name:
                        self.future_names.add(spec.name)
                    continue
                if spec.star:
                    stars.append(spec)
                else:
                    table.setdefault(spec.local_name, spec)
            self.import_table[m] = table
            self.star_specs[m] = stars
        self.env: dict[str, dict] = {}
        self.eager: dict[int, set] = {}
        self.proj_refs: dict[int, set] = {}
        self.ext_keys: dict[int, set] = {}
        self.ext_refs: dict[int, frozenset] = {}
        self.canon: dict[int, Block] = {}
        self.final: dict[tuple[int, str], str] = {}
        self.ext_final: dict[tuple, str] = {}
        self.star_keys: set = set()
        self.cond_specs: dict[str, list] = {}
        self.nested_locals: dict[str, set] = {}
        self._warned: set = set()
        self._renamed_log: list = []
        self._deduped_log: list = []
        self._taken: set = set()

    def _entry_match(self, module: str) -> bool:
        return any(module == e or module.endswith("." + e) for e in self.entry)

    def _warn_once(self, msg: str) -> None:
        if msg not in self._warned:
            self._warned.add(msg)
            logger.warning(msg)

    def _is_internal(self, spec) -> bool:
        if spec.relative:
            return True
        return bool(spec.module) and (spec.module in self.known or spec.module.split(".")[0] in self.tops)

    def _canon(self, b: Block) -> Block:
        return self.canon.get(b.bid, b)

    def _prefix(self, module: str) -> str:
        parts = module.split(".")
        if self.single_top and len(parts) > 1 and parts[0] == self.single_top:
            parts = parts[1:]
        return make_identifier(_sanitize("_".join(parts)) or "mod")

    def _star_names(self, module: str) -> list[str]:
        mi = self.modules.get(module)
        if mi is None:
            return []
        if mi.declared_all is not None:
            return list(mi.declared_all)
        names = [n for (m, n) in self.def_block if m == module and not n.startswith("_")]
        names += [n for n in self.import_table.get(module, {}) if not n.startswith("_")]
        return names

    def _spec_target(self, spec, seen: set | None = None):
        internal = self._is_internal(spec)
        if not spec.is_from:
            if internal:
                return ("mod", spec.module) if spec.alias else ("mod", spec.module.split(".")[0])
            return ("ext", ("import", spec.module, spec.alias))
        if internal:
            tgt = self._resolve_symbol(spec.module, spec.name, seen)
            if tgt is None and f"{spec.module}.{spec.name}" in self.known:
                tgt = ("mod", f"{spec.module}.{spec.name}")
            return tgt
        return ("ext", ("from", spec.module, spec.name, spec.alias))

    def _resolve_symbol(self, module: str, name: str, seen: set | None = None):
        blk = self.def_block.get((module, name))
        if blk is not None:
            return ("def", blk, name)
        seen = set() if seen is None else seen
        if (module, name) in seen:
            return None
        seen.add((module, name))
        spec = self.import_table.get(module, {}).get(name)
        if spec is not None:
            tgt = self._spec_target(spec, seen)
            if tgt is not None:
                return tgt
        for star in self.star_specs.get(module, ()):
            if self._is_internal(star) and name in self._star_names(star.module):
                tgt = self._resolve_symbol(star.module, name, seen)
                if tgt is not None:
                    return tgt
        sub = f"{module}.{name}"
        if sub in self.known:
            return ("mod", sub)
        return None

    def _resolve_dotted(self, module: str, obj: str, attr: str):
        parts = obj.split(".")
        tgt = self.env[module].get(parts[0])
        if not tgt or tgt[0] != "mod":
            return None
        mod = tgt[1]
        for seg in parts[1:]:
            cand = f"{mod}.{seg}"
            if cand not in self.known:
                return None
            mod = cand
        res = self._resolve_symbol(mod, attr)
        return res if res is not None and res[0] in ("def", "ext") else None

    def build(self) -> Plan:
        self._build_envs()
        self._analyse()
        self._dedupe()
        kept = [b for b in self.blocks if b.bid not in self.canon]
        used = set().union(*(self.ext_keys[b.bid] for b in kept)) if kept else set()
        emit_keys = set(used)
        if self.imports_mode == "keep":
            for env in self.env.values():
                emit_keys |= {t[1] for t in env.values() if t[0] == "ext"}
        elif self.imports_mode == "strict":
            all_ext = {t[1] for env in self.env.values() for t in env.values() if t[0] == "ext"}
            for k in sorted(all_ext - used, key=repr):
                self._warn_once(f"Strict imports: unused import {k} would be pruned")
        emit_keys |= self.star_keys
        emit_keys = self._drop_guarded_keys(emit_keys)
        self._assign_names(kept, emit_keys)
        self._resolve_ext_conflicts(emit_keys)
        rendered = self._render_all(kept)
        return self._assemble(kept, rendered, emit_keys)

    def _guard_identity(self, spec) -> tuple:
        if spec.star:
            return ("star", spec.module)
        if not spec.is_from:
            return ("import", spec.module, spec.alias)
        return ("from", spec.module, spec.name, spec.alias)

    def _drop_guarded_keys(self, emit_keys: set) -> set:
        provided = {self._guard_identity(spec) for specs in self.cond_specs.values() for spec in specs}
        if not provided:
            return emit_keys
        kept = set()
        for key in emit_keys:
            if key[0] == "star":
                ident = ("star", key[1])
            elif key[0] == "import":
                ident = ("import", key[1], key[2])
            else:
                ident = ("from", key[1], key[2], key[3])
            if ident in provided:
                logger.debug("Import {} already provided by a guard", key)
                continue
            kept.add(key)
        return kept

    def _build_envs(self) -> None:
        defs_by_module: dict[str, dict[str, Block]] = defaultdict(dict)
        for (mm, n), blk in self.def_block.items():
            defs_by_module[mm][n] = blk
        for m, mi in self.modules.items():
            env: dict = {n: ("def", blk, n) for n, blk in defs_by_module.get(m, {}).items()}
            specs = [s for s in mi.imports if s.module != "__future__"]
            nested: list = []
            for b in mi.blocks:
                for ref in b.refs:
                    if ref.kind == "nested_import":
                        nested.extend(s for s in ref.specs if s.module != "__future__")
            nested_set = set(nested)
            nested_locals = self.nested_locals.setdefault(m, set())
            for spec in specs + nested:
                is_nested = spec in nested_set
                if is_nested and spec.local_name in env:
                    continue
                if spec.star:
                    if self._is_internal(spec):
                        for name in self._star_names(spec.module):
                            tgt = self._resolve_symbol(spec.module, name)
                            if tgt is not None:
                                env.setdefault(name, tgt)
                        self._warn_once(
                            f"{m}: local star import from {spec.module} expanded to "
                            f"{len(self._star_names(spec.module))} names"
                        )
                    else:
                        self._warn_once(f"{m}: external star import from {spec.module} — kept as is")
                        self.star_keys.add(("star", spec.module))
                    continue
                tgt = self._spec_target(spec, set())
                if tgt is None:
                    if self._is_internal(spec):
                        self._warn_once(
                            f"{m}: cannot resolve internal import '{spec.module}"
                            f"{'.' + spec.name if spec.name else ''}' (module skipped, missing or excluded?)"
                        )
                    continue
                if spec.local_name in env:
                    continue
                env[spec.local_name] = tgt
                if is_nested and tgt[0] == "ext":
                    nested_locals.add(spec.local_name)
            if mi.guards:
                self.cond_specs[m] = [s for g in mi.guards for s in g.specs]
            for g in mi.guards:
                for spec in g.specs:
                    if self._is_internal(spec):
                        self._warn_once(
                            f"{m}: guarded import of internal module '{spec.module}' is kept "
                            "verbatim and will not resolve"
                        )
                    if "ImportError" in g.text or "try:" in g.text:
                        self._warn_once(f"{m}: conditional/optional import guard kept verbatim (may have side effects)")
            self.env[m] = env

    def _analyse(self) -> None:
        for b in self.blocks:
            env = self.env[b.module]
            eager: set = set()
            proj: set = set()
            ext_keys: set = set()
            ext_refs: set = set()
            for ref in b.refs:
                if ref.kind == "name":
                    tgt = env.get(ref.name)
                    label = ref.name
                elif ref.kind == "attr":
                    tgt = self._resolve_dotted(b.module, ref.obj, ref.name)
                    label = f"{ref.obj}.{ref.name}"
                else:
                    continue
                if tgt is None:
                    continue
                if tgt[0] == "def":
                    if tgt[1] is not b:
                        proj.add(tgt[1])
                        if ref.eager:
                            eager.add(tgt[1])
                elif tgt[0] == "ext":
                    if ref.kind == "name" and ref.name in self.nested_locals.get(b.module, ()):
                        continue
                    ext_keys.add(tgt[1])
                    ext_refs.add((label, tgt[1]))
            self.eager[b.bid], self.proj_refs[b.bid] = eager, proj
            self.ext_keys[b.bid], self.ext_refs[b.bid] = ext_keys, frozenset(ext_refs)

    def _dedupe(self) -> None:
        groups: dict = defaultdict(list)
        for b in self.blocks:
            if not b.code().strip():
                continue
            groups[(b.kind, tuple(b.names), _content_hash(b))].append(b)
        for key, blocks in groups.items():
            if len(blocks) < 2:
                continue
            blocks.sort(key=lambda x: (x.module.count("."), x.module, x.order))
            for dup in blocks[1:]:
                self.canon[dup.bid] = blocks[0]
                self._deduped_log.append((dup.module, dup.primary or dup.kind))
                logger.info(
                    "Dropping duplicate {} '{}' from {} (identical to {})",
                    dup.kind,
                    dup.primary,
                    dup.module,
                    blocks[0].module,
                )

    def _assign_names(self, kept: list, emit_keys: set) -> None:
        ext_loc_modules: dict = defaultdict(set)
        for m, env in self.env.items():
            for local, tgt in env.items():
                if tgt[0] == "ext" and tgt[1] in emit_keys:
                    ext_loc_modules[local].add(m)
        ext_locals = {_ext_local(k) for k in emit_keys if _ext_local(k)}
        guard_names = {
            name
            for mi in self.modules.values()
            for g in mi.guards
            for name in ([s.local_name for s in g.specs] + list(g.bound_names))
            if name
        }
        group: dict = defaultdict(list)
        for b in kept:
            for n in b.names:
                group[n].append(b)
        taken: set = set(ext_locals) | guard_names
        main_override = self.main_name
        for name in sorted(group):
            blocks = sorted(group[name], key=lambda x: (x.module.count("."), x.module, x.order))
            is_main_override = name == "main" and main_override is not None
            for i, b in enumerate(blocks):
                clash = bool(ext_loc_modules.get(name, set()) - {b.module}) or name in guard_names
                if is_main_override:
                    if i == 0 and not clash and main_override not in taken:
                        cand = main_override
                    else:
                        base_name = main_override
                        cand = make_identifier(f"{base_name}_{self._prefix(b.module)}")
                        base, n = cand, 2
                        while cand in taken or cand in group:
                            cand, n = f"{base}_{n}", n + 1
                        logger.info("Renaming {} 'main' in {} -> '{}' (main-name override)", b.kind, b.module, cand)
                    taken.add(cand)
                    self.final[(b.bid, name)] = cand
                    if cand != name:
                        self._renamed_log.append((b.module, name, cand))
                    continue
                if i == 0 and not clash and name not in taken:
                    cand = name
                else:
                    lead = name[: len(name) - len(name.lstrip("_"))]
                    raw = name.lstrip("_")
                    cand = make_identifier(f"{lead}{self._prefix(b.module)}_{raw}")
                    base, n = cand, 2
                    while cand in taken or cand in group:
                        cand, n = f"{base}_{n}", n + 1
                    logger.info("Renaming {} '{}' in {} -> '{}'", b.kind, name, b.module, cand)
                taken.add(cand)
                self.final[(b.bid, name)] = cand
                if cand != name:
                    self._renamed_log.append((b.module, name, cand))
        self._taken = taken

    def _resolve_ext_conflicts(self, emit_keys: set) -> None:
        by_local: dict = defaultdict(list)
        for key in sorted(emit_keys, key=repr):
            loc = _ext_local(key)
            if loc:
                by_local[loc].append(key)
        taken = self._taken
        for loc, keys in by_local.items():
            if len({_ext_identity(k) for k in keys}) <= 1:
                continue
            keys.sort(key=lambda k: (not (k[0] == "import" and k[2] is None), repr(k)))
            keep = _ext_identity(keys[0])
            aliased: dict = {}
            for k in keys:
                ident = _ext_identity(k)
                if ident == keep:
                    continue
                if ident in aliased:
                    self.ext_final[k] = aliased[ident]
                    continue
                base = make_identifier(_sanitize(k[1]) + (f"_{k[2]}" if k[0] == "from" else ""))
                cand, n = base, 2
                while cand in taken:
                    cand, n = f"{base}_{n}", n + 1
                taken.add(cand)
                aliased[ident] = cand
                self.ext_final[k] = cand
                logger.info("Import conflict on '{}': aliasing {} as '{}'", loc, k[1:], cand)

    def _target_name(self, tgt, local: str | None) -> str | None:
        if tgt is None:
            return None
        if tgt[0] == "def":
            blk = self._canon(tgt[1])
            return self.final.get((blk.bid, tgt[2]))
        if tgt[0] == "ext":
            fallback = local if local is not None else _ext_local(tgt[1])
            return self.ext_final.get(tgt[1], fallback)
        return None

    def _render_block(self, b: Block) -> str:
        walker = RefWalker(
            b.module,
            self.modules[b.module].is_pkg,
            collect=False,
            resolver=self,
            env=self.env[b.module],
        )
        tree = cst.Module(body=list(b.nodes))
        rewritten = tree.visit(walker)
        return rewritten.code.strip("\n").rstrip()

    def _render_all(self, kept: list) -> dict[int, str]:
        if self.workers > 1 and len(kept) > 32:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                texts = list(pool.map(self._render_block, kept))
        else:
            texts = [self._render_block(b) for b in kept]
        return {b.bid: t for b, t in zip(kept, texts)}

    def _merge_mains(self, mains: list, texts: dict[int, str]) -> str | None:
        sections: list = []
        seen: set = set()
        for b in mains:
            body = _guard_body(texts[b.bid])
            key_body = "\n".join(line.rstrip() for line in body).strip()
            if not key_body:
                continue
            if key_body in seen:
                continue
            seen.add(key_body)
            sections.append((b.module, body))
        if not sections:
            return None
        indent = "    "
        out = ['if __name__ == "__main__":']
        for i, (module, body) in enumerate(sections):
            if i:
                out.append("")
            out.append(f"{indent}# from {module}")
            for line in body:
                out.append(f"{indent}{line}" if line.strip() else "")
        return "\n".join(out).rstrip()

    def _assemble(self, kept: list, texts: dict[int, str], emit_keys: set) -> Plan:
        keyfn = lambda b: (self.mod_idx[b.module], b.order)  # noqa: E731
        body = [b for b in kept if b.kind != MAIN]
        mains = sorted((b for b in kept if b.kind == MAIN), key=keyfn)
        deps = {b.bid: {self._canon(d).bid for d in self.eager[b.bid]} - {b.bid} for b in body}
        dependents: dict = defaultdict(set)
        for bid, ds in deps.items():
            for d in ds:
                dependents[d].add(bid)
        tail = [b for b in body if b.kind == FUNC and b.primary == "main" and not dependents[b.bid]]
        tail_ids = {b.bid for b in tail}
        head = [b for b in body if b.bid not in tail_ids]
        ordered = topo_order(head, deps, lambda b: _RANK[b.kind], lambda b: (_RANK[b.kind], *keyfn(b)))
        tail_ordered = topo_order(tail, deps, lambda b: 0, keyfn)
        rendered: list[RenderedBlock] = []
        for b in ordered:
            origin = f"{b.orig_path}:{b.start_line}-{b.end_line}" if b.orig_path else ""
            rendered.append(RenderedBlock(_SECTION[b.kind], b.kind, texts[b.bid], origin=origin, module=b.module))
        for b in tail_ordered:
            origin = f"{b.orig_path}:{b.start_line}-{b.end_line}" if b.orig_path else ""
            rendered.append(RenderedBlock("entry", b.kind, texts[b.bid], origin=origin, module=b.module))
        merged_guard = self._merge_mains(mains, texts)
        if merged_guard is not None:
            modules = ", ".join(b.module for b in mains)
            rendered.append(
                RenderedBlock("entry", MAIN, merged_guard, origin=f"merged from {modules}", module=mains[0].module)
            )
        if self.generate_guard and not mains:
            mains_funcs = [b for b in kept if b.kind == FUNC and b.primary in ("main", self.main_name or "main")]
            if mains_funcs:
                guard_text = (
                    'if __name__ == "__main__":\n    main()'
                    if not self.main_name
                    else f'if __name__ == "__main__":\n    {self.main_name}()'
                )
                rendered.append(RenderedBlock("entry", MAIN, guard_text, origin="generated", module="__main__"))
                logger.info("Auto-generated __main__ guard")
        if len(mains) > 1:
            logger.info(
                "Merged {} '__main__' guards into a single entry block (from {})",
                len(mains),
                ", ".join(b.module for b in mains),
            )
            logger.warning(
                "{} modules define their own entry point; only one can run at a "
                "time. Use --entry MODULE to keep a single one.",
                len(mains),
            )
        guards: list[str] = []
        for mi in self.modules.values():
            for g in mi.guards:
                if g.text not in guards:
                    guards.append(g.text)
        plan = Plan(
            future_names=sorted(self.future_names),
            imports_text=build_import_section(emit_keys, self.ext_final),
            guards=guards,
            blocks=rendered,
            renamed=self._renamed_log,
            deduped=self._deduped_log,
            all_names=self._module_all_names(),
        )
        if self.resources:
            plan.resources_code = self._build_resources()
        self._exports(plan, kept)
        return plan

    def _module_all_names(self) -> list[str]:
        candidates = [(m, mi) for m, mi in self.modules.items() if mi.declared_all is not None]
        if not candidates:
            return []
        candidates.sort(key=lambda item: item[0].count("."))
        m, mi = candidates[0]
        env = self.env[m]
        out: list[str] = []
        for name in mi.declared_all:
            tgt = env.get(name)
            fin = self._target_name(tgt, name)
            out.append(fin or name)
        seen: set = set()
        result: list[str] = []
        for n in out:
            if n not in seen:
                seen.add(n)
                result.append(n)
        return result

    def _build_resources(self) -> str:
        taken = set(self._taken or set())
        for b in self.blocks:
            for n in b.names:
                taken.add(self.final.get((b.bid, n), n))
        taken -= {None}
        return build_resources_code(self.resources or [], taken_names=taken)

    def _exports(self, plan: Plan, kept: list) -> None:
        groups = {CONST: "constants", MISC: "constants", CLASS: "classes", FUNC: "functions"}
        seen: set = set()
        for b in kept:
            if b.kind == MAIN:
                continue
            for n in b.names:
                fin = self.final.get((b.bid, n))
                if fin is None or fin in seen:
                    continue
                if fin.startswith("_"):
                    continue
                seen.add(fin)
                plan.exports[groups.get(b.kind, "constants")].append(fin)
        for names in plan.exports.values():
            names.sort(key=sort_key)


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

_RULE = "# " + "-" * 75
_TITLES = {
    "resources": "Bundled resources",
    "const": "Constants",
    "class": "Classes",
    "func": "Functions",
    "entry": "Entry points",
}


def _sep(prev_kind: str, cur_kind: str, prev_text: str, cur_text: str) -> str:
    simple = ("const", "misc")
    if prev_kind in simple and cur_kind in simple:
        return "\n" if "\n" not in prev_text and "\n" not in cur_text else "\n\n"
    return "\n\n\n"


def _format_with_black(text: str) -> str:
    try:
        import black
    except ImportError:
        logger.warning("--format: black is not installed; leaving output unformatted")
        return text
    try:
        return black.format_str(text, mode=black.Mode())
    except Exception as exc:
        logger.warning("--format: black failed ({}); leaving output unformatted", exc)
        return text


def _format_with_ruff(text: str) -> str:
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as tmp:
            tmp.write(text)
            tmp_path = tmp.name
        proc = subprocess.run(["ruff", "format", tmp_path], capture_output=True, timeout=30)
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", "replace").strip()
            logger.warning(
                "--format: ruff exited {}{}; leaving output unformatted",
                proc.returncode,
                f" ({stderr})" if stderr else "",
            )
        return Path(tmp_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("--format: ruff is not installed; leaving output unformatted")
        return text
    except Exception as exc:
        logger.warning("--format: ruff failed ({}); leaving output unformatted", exc)
        return text
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _all_section(names: list) -> str:
    items = ", ".join(repr(n) for n in names)
    if len(names) <= 5 and len(items) <= 72:
        return f"__all__ = [{items}]"
    body = "".join(f"    {n!r},\n" for n in names)
    return f"__all__ = [\n{body}]"


def render_consolidated(plan: Plan, source_name: str, do_format: bool = False, formatter: str = "black") -> str:
    parts = [f'"""Consolidated from ``{source_name}`` by pyrefactor (generated file)."""']
    head = []
    if plan.future_names:
        head.append(future_section(plan.future_names))
    if plan.imports_text:
        head.append(plan.imports_text)
    if plan.guards:
        head.append("\n\n".join(plan.guards))
    if plan.all_names:
        head.append(_all_section(plan.all_names))
    if head:
        parts.append("\n\n".join(head))
    body = []
    prev = None
    seen_sections: set = set()
    blocks = list(plan.blocks)
    if plan.resources_code:
        blocks.insert(0, RenderedBlock("resources", "resources", plan.resources_code))
    for blk in blocks:
        piece = ""
        first_of_section = blk.section not in seen_sections
        if prev is None or blk.section != prev.section:
            if first_of_section:
                piece += f"{_RULE}\n# {_TITLES.get(blk.section, blk.section)}\n{_RULE}\n\n\n"
            sep = "\n\n\n" if prev is not None else ""
        else:
            sep = _sep(prev.kind, blk.kind, prev.text, blk.text)
        seen_sections.add(blk.section)
        piece += blk.text
        body.append(sep + piece)
        prev = blk
    parts.append("".join(body))
    head_text = "\n\n\n".join(p for p in parts[1:] if p)
    text = parts[0] + "\n\n" + head_text
    text = text.rstrip() + "\n"
    if do_format:
        if formatter == "ruff":
            text = _format_with_ruff(text)
        else:
            formatted = _format_with_black(text)
            if formatted == text:
                formatted = _format_with_ruff(text)
            text = formatted
    return text


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ConsolidationResult:
    output_dir: Path
    files: list
    modules: int
    skipped: list
    exports: int
    warnings: list
    diff: str
    stdout_text: str
    copied_data_files: list
    validated: bool = False
    dry_run: bool = False
    plan: Plan | None = None
    embedded_data_files: list = field(default_factory=list)


def _init_worker() -> None:
    sys.setrecursionlimit(max(sys.getrecursionlimit(), _RECURSION_LIMIT))


def _parse_worker(args: tuple):
    path_str, module, is_pkg, keep_docstrings, keep_comments = args
    info = parse_module_file(path_str, module, is_pkg, keep_docstrings=keep_docstrings, keep_comments=keep_comments)
    return module, info


def _failed_info(f: SourceFile, exc: BaseException) -> ModuleInfo:
    return ModuleInfo(
        module=f.module,
        path=str(f.path),
        is_pkg=f.is_pkg,
        errors=[f"{f.path}: parse failed: {exc!r}"],
    )


def _parse_all(
    files,
    workers: int,
    executor: str,
    keep_docstrings: bool = True,
    keep_comments: bool = True,
    show_progress: bool = False,
) -> dict:
    results: dict = {}
    failed: list = []

    def _do_one(f):
        return parse_module_file(
            str(f.path), f.module, f.is_pkg, keep_docstrings=keep_docstrings, keep_comments=keep_comments
        )

    def _progress(iterator, total=None):
        if not show_progress:
            return iterator
        try:
            from tqdm import tqdm

            return tqdm(iterator, total=total, desc="Parsing")
        except ImportError:
            logger.info("Progress bar requested but tqdm not installed — falling back to log")
            return iterator

    def run_pool(pool_cls, todo) -> None:
        kwargs = {"initializer": _init_worker} if pool_cls is ProcessPoolExecutor else {}
        with pool_cls(max_workers=workers, **kwargs) as pool:
            if pool_cls is ProcessPoolExecutor:
                futures = {
                    pool.submit(_parse_worker, (str(f.path), f.module, f.is_pkg, keep_docstrings, keep_comments)): f
                    for f in todo
                }
            else:
                futures = {pool.submit(_do_one, f): f for f in todo}
            for fut in _progress(as_completed(futures), len(futures)):
                src = futures[fut]
                try:
                    res = fut.result()
                    if isinstance(res, tuple) and len(res) == 2 and isinstance(res[1], ModuleInfo):
                        results[res[0]] = res[1]
                    else:
                        results[src.module] = res
                except BrokenExecutor:
                    raise
                except Exception as exc:
                    logger.warning("Parallel parse of {} failed ({!r}); retrying in the main process", src.path, exc)
                    failed.append(src)

    if workers <= 1 or len(files) <= 1:
        for f in _progress(files, len(files)):
            try:
                results[f.module] = _do_one(f)
            except Exception as exc:
                logger.exception("Failed to parse {}", f.path)
                results[f.module] = _failed_info(f, exc)
        return results
    pool_cls = ProcessPoolExecutor if executor == "process" else ThreadPoolExecutor
    try:
        run_pool(pool_cls, files)
    except BrokenExecutor:
        logger.warning("Process pool broke; retrying remaining files with threads")
        pending = [f for f in files if f.module not in results and f not in failed]
        run_pool(ThreadPoolExecutor, pending)
    # Anything that failed in a worker (e.g. result could not be pickled back
    # because of deep trees) is retried serially; a second failure is recorded
    # as an error so the build aborts instead of silently dropping the module.
    for f in failed:
        if f.module in results:
            continue
        try:
            results[f.module] = _do_one(f)
        except Exception as exc:
            logger.exception("Failed to parse {}", f.path)
            results[f.module] = _failed_info(f, exc)
    return results


def _load_config_file(config_path, source) -> dict:
    if config_path is None:
        return detect_pyproject_config(source)
    config_path = Path(config_path)
    if not config_path.is_file():
        logger.warning("Config file not found: {}", config_path)
        return {}
    tool = read_pyproject_section(config_path)
    if tool:
        logger.info("Loaded config from {}", config_path)
    return tool


def _config_list(cfg: dict, key: str) -> tuple:
    value = cfg.get(key)
    if not value:
        return ()
    return tuple(value) if isinstance(value, list) else (value,)


def validate_syntax(text: str, filename: str = "<consolidated>") -> ast.Module:
    try:
        return ast.parse(text, filename=filename)
    except SyntaxError as exc:
        line = (exc.text or "").strip()
        if not line and exc.lineno:
            lines = text.splitlines()
            if 0 < exc.lineno <= len(lines):
                line = lines[exc.lineno - 1].strip()
        where = f"line {exc.lineno}" if exc.lineno else "unknown line"
        detail = f": {line!r}" if line else ""
        raise ConsolidationError(f"generated {filename} is not valid python: {exc.msg} ({where}){detail}") from exc
    except ValueError as exc:
        raise ConsolidationError(f"generated {filename} is not valid python: {exc}") from exc


def _normalize_resource_name(relative: str) -> str | None:
    raw = str(relative).replace("\\", "/")
    pure = PurePosixPath(raw)
    if pure.is_absolute() or raw.startswith("/"):
        return None
    parts = [part for part in pure.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    return "/".join(parts)


def _collect_embedded_resources(data_files, *, max_embed_bytes: int):
    if not data_files:
        return None, []
    resources: list[tuple[str, bytes]] = []
    embedded_names: list[str] = []
    seen: dict[str, str] = {}
    for df in data_files:
        normalized = _normalize_resource_name(df.relative)
        if normalized is None:
            logger.warning(
                "Skipping unsafe resource path {!r}; it must be relative and cannot contain '..'", df.relative
            )
            continue
        try:
            size = df.path.stat().st_size
        except OSError as exc:
            logger.error("Cannot stat data file {}: {}", df.path, exc)
            continue
        if max_embed_bytes >= 0 and size > max_embed_bytes:
            logger.warning(
                "Skipping data file {}: {} bytes exceeds --max-embed-size ({} bytes)", df.path, size, max_embed_bytes
            )
            continue
        try:
            payload = df.path.read_bytes()
        except OSError as exc:
            logger.error("Cannot read data file {}: {}", df.path, exc)
            continue
        key = normalized.lower()
        previous = seen.get(key)
        if previous is not None:
            logger.warning(
                "Resource path collision: {!r} and {!r} both map to {!r}; keeping the first",
                previous,
                df.path,
                normalized,
            )
            continue
        seen[key] = str(df.path)
        resources.append((normalized, payload))
        embedded_names.append(normalized)
    return (resources if resources else None), embedded_names


def consolidate(
    source,
    output,
    *,
    filename: str | None = None,
    workers: int | None = None,
    executor: str = "process",
    exclude: tuple = (),
    include: tuple = (),
    imports_mode: str = "prune",
    entry: tuple = (),
    dry_run: bool = False,
    check: bool = False,
    stdout: bool = False,
    config: Path | None = None,
    do_format: bool = False,
    formatter: str = "black",
    diff: bool = False,
    force: bool = False,
    keep_docstrings: bool = True,
    keep_comments: bool = True,
    progress: bool = False,
    copy_data: bool = True,
    embed_data: bool = True,
    max_embed_bytes: int = 10 * 1024 * 1024,
    data_glob: tuple = (),
    entry_keep_all: bool = False,
    main_name: str | None = None,
    generate_guard: bool = False,
    modernize: str | None = None,
    merged_file: Path | str | None = None,
) -> ConsolidationResult:
    sys.setrecursionlimit(max(sys.getrecursionlimit(), _RECURSION_LIMIT))
    cfg = _load_config_file(config, source)
    if cfg:
        if not exclude:
            exclude = _config_list(cfg, "exclude")
        if not include:
            include = _config_list(cfg, "include")
        if not entry:
            entry = _config_list(cfg, "entry")
        if cfg.get("imports"):
            imports_mode = str(cfg["imports"])
        if cfg.get("format") and not do_format:
            do_format = bool(cfg["format"])
        if cfg.get("formatter"):
            formatter = str(cfg["formatter"])
        if cfg.get("copy_data") is not None:
            copy_data = bool(cfg["copy_data"])
        if cfg.get("embed_data") is not None:
            embed_data = bool(cfg["embed_data"])
        if cfg.get("max_embed_bytes") is not None:
            try:
                max_embed_bytes = int(cfg["max_embed_bytes"])
            except (TypeError, ValueError):
                logger.warning("Ignoring invalid max_embed_bytes value: {!r}", cfg["max_embed_bytes"])
        if cfg.get("data_glob"):
            data_glob = _config_list(cfg, "data_glob")
        if not modernize and cfg.get("modernize"):
            modernize = str(cfg["modernize"])
    if imports_mode not in ("prune", "keep", "strict"):
        raise ConsolidationError(f"imports_mode must be one of prune/keep/strict, got {imports_mode!r}")
    if main_name is not None and not is_valid_identifier(main_name):
        raise ConsolidationError(f"--main-name {main_name!r} is not a valid python identifier")
    if max_embed_bytes < 0:
        raise ConsolidationError("max_embed_bytes must not be negative")
    source, output = Path(source), Path(output)
    merged_path: Path | None = Path(merged_file) if merged_file else None
    if merged_path is not None:
        if not merged_path.is_file():
            raise ConsolidationError(f"merged file does not exist: {merged_path}")
    elif not source.exists():
        raise ConsolidationError(f"source does not exist: {source}")
    workers = workers or os.cpu_count() or 1
    if workers < 1:
        raise ConsolidationError("workers must be at least 1")
    if merged_path is not None:
        source_name = merged_path.stem
    else:
        source_name = source.resolve().name if source.is_dir() else source.stem
    filename = filename or source_name
    if not filename.endswith(".py"):
        filename = f"{filename}.py"
    if merged_path is not None:
        try:
            files, _staged_root = load_merged_sources(merged_path)
        except (ValueError, FileNotFoundError) as exc:
            raise ConsolidationError(str(exc)) from exc
        data_files = []
    else:
        files = discover(source, exclude=exclude, include=include, skip_dirs=[output])
        data_files = discover_data_files(
            source,
            exclude=exclude,
            include=include,
            skip_dirs=[output],
            copy_data=copy_data,
            collect=copy_data or embed_data,
        )
        if data_glob:
            patterns = [pattern for pattern in data_glob if pattern]
            if patterns:
                data_files = [
                    df
                    for df in data_files
                    if any(
                        fnmatch.fnmatch(df.relative, pattern) or fnmatch.fnmatch(Path(df.path).name, pattern)
                        for pattern in patterns
                    )
                ]
    if not files:
        raise ConsolidationError(f"no python files found under {source}")
    logger.info("Parsing {} files with {} {} worker(s)", len(files), workers, executor)
    if data_files:
        logger.info("Found {} data file(s) to preserve", len(data_files))
    parsed = _parse_all(
        files,
        workers,
        executor,
        keep_docstrings=keep_docstrings,
        keep_comments=keep_comments,
        show_progress=progress,
    )
    modules: dict = {}
    skipped: list = []
    warnings_acc: list = []
    for name, info in parsed.items():
        for w in info.warnings:
            logger.warning(w)
            warnings_acc.append(w)
        if info.errors:
            for e in info.errors:
                logger.error(e)
            skipped.append(info.path)
            continue
        modules[name] = info
    missing = [str(f.path) for f in files if f.module not in parsed]
    skipped.extend(missing)
    if skipped:
        raise ConsolidationError(
            f"{len(skipped)} file(s) could not be parsed; refusing to emit a "
            f"partial consolidation: {', '.join(skipped)}"
        )
    if not modules:
        raise ConsolidationError("no module could be parsed")
    resources, embedded_names = _collect_embedded_resources(data_files, max_embed_bytes=max_embed_bytes)
    resolver = Resolver(
        modules,
        entry=entry,
        workers=workers,
        imports_mode=imports_mode,
        entry_keep_all=entry_keep_all,
        main_name=main_name,
        generate_guard=generate_guard,
        resources=resources,
    )
    plan = resolver.build()
    for mod, orig, final in plan.renamed:
        logger.info("Renamed {}:{} -> {}", mod, orig, final)
    for mod, name in plan.deduped:
        logger.info("Deduped {} from {}", name, mod)
    mod_text = render_consolidated(plan, source_name, do_format=do_format, formatter=formatter)
    exports = sum(len(v) for v in plan.exports.values())
    if modernize:
        try:
            tree = cst.parse_module(mod_text)
            tree, report = modernize_tree(tree, modernize)
            if report.changed:
                mod_text = tree.code
        except Exception as exc:
            raise ConsolidationError(f"modernize pass failed on consolidated output: {exc}") from exc
    validate_syntax(mod_text, filename)
    diff_text = ""
    target = output / filename
    if diff or check:
        if target.is_file():
            old_text = target.read_text(encoding="utf-8")
            if old_text != mod_text:
                diff_text = "".join(
                    difflib.unified_diff(
                        old_text.splitlines(keepends=True),
                        mod_text.splitlines(keepends=True),
                        fromfile=f"a/{filename}",
                        tofile=f"b/{filename}",
                    )
                )
            else:
                logger.info("No diff for {}", filename)
        else:
            logger.info("No existing {} to diff", target)
        if diff_text:
            logger.info("Diff ({} chars):", len(diff_text))
            sys.stdout.write(diff_text)
        if check:
            if diff_text or not target.is_file():
                raise ConsolidationError("check failed: output would change (diff not empty or output missing)")
            logger.success("Check passed: output up to date")
            return ConsolidationResult(
                output,
                [],
                len(modules),
                skipped,
                exports,
                warnings_acc,
                "",
                "",
                [],
                validated=True,
                dry_run=True,
                plan=plan,
            )
    if dry_run:
        logger.info("Dry-run: would write {} to {}", filename, output)
        logger.info(
            "Would write {} ({} bytes, {} lines)", filename, len(mod_text.encode("utf-8")), mod_text.count("\n") + 1
        )
        logger.info(
            "Plan: {} blocks, {} exports, {} deduped, {} renamed",
            len(plan.blocks),
            exports,
            len(plan.deduped),
            len(plan.renamed),
        )
        if resources:
            logger.info("Would embed {} data file(s)", len(resources))
        if copy_data:
            for df in data_files:
                logger.info("Would copy data file {} -> {}/{}", df.path, output, df.relative)
        return ConsolidationResult(
            output,
            [],
            len(modules),
            skipped,
            exports,
            warnings_acc,
            "",
            mod_text if stdout else "",
            [],
            validated=True,
            dry_run=True,
            plan=plan,
            embedded_data_files=embedded_names,
        )
    if stdout:
        sys.stdout.write(mod_text)
        return ConsolidationResult(
            output,
            [],
            len(modules),
            skipped,
            exports,
            warnings_acc,
            diff_text,
            mod_text,
            [],
            validated=True,
            plan=plan,
            embedded_data_files=embedded_names,
        )
    if target.exists() and not force:
        logger.warning("{} exists — overwriting (use --force to silence)", target)
    output.mkdir(parents=True, exist_ok=True)
    tmp_target = target.with_suffix(target.suffix + ".tmp")
    tmp_target.write_text(mod_text, encoding="utf-8")
    tmp_target.replace(target)
    written = [target]
    copied: list = []
    if copy_data:
        for df in data_files:
            dest = output / df.relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(df.path, dest)
                copied.append(dest)
                logger.debug("Copied data file {} -> {}", df.path, dest)
            except OSError as exc:
                logger.error("Failed to copy data file {}: {}", df.path, exc)
    if resources:
        logger.info("Embedded {} data file(s) into {}", len(resources), target)
    return ConsolidationResult(
        output,
        written,
        len(modules),
        skipped,
        exports,
        warnings_acc,
        diff_text,
        "",
        copied,
        validated=True,
        plan=plan,
        embedded_data_files=embedded_names,
    )


# ---------------------------------------------------------------------------
# Bundle mode (self-extracting zip)
# ---------------------------------------------------------------------------

_DETERMINISTIC_TIMESTAMP: tuple[int, int, int, int, int, int] = (1980, 1, 1, 0, 0, 0)
_DEFAULT_DATA_EXCLUDES: tuple[str, ...] = (
    "**/__pycache__/**",
    "**/*.pyc",
    "**/*.pyo",
    "**/*.pyd",
    "**/*.egg-info/**",
    "**/*.dist-info/**",
)
_COMPRESSIONS: dict[str, int] = {
    "stored": zipfile.ZIP_STORED,
    "deflate": zipfile.ZIP_DEFLATED,
    "bzip2": zipfile.ZIP_BZIP2,
    "lzma": zipfile.ZIP_LZMA,
}
_BINARY_SUFFIXES: frozenset[str] = frozenset({".so", ".pyd", ".dylib"})
_IGNORED_TOPS: frozenset[str] = frozenset({"__main__", "__builtins__", "builtins", "__future__"})
_STRICT_WARNING_KINDS: frozenset[str] = frozenset({"unresolved", "broken"})
_ENTRY_CANDIDATES: tuple[str, ...] = ("main", "cli", "app", "run")


@dataclass(frozen=True, slots=True)
class PlanWarning:
    kind: str
    message: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.message}"


@dataclass(frozen=True, slots=True)
class BundledModule:
    name: str
    source: Path
    is_package: bool

    @property
    def archive_path(self) -> str:
        base = self.name.replace(".", "/")
        return f"{base}/__init__.py" if self.is_package else f"{base}.py"


@dataclass(frozen=True, slots=True)
class _Located:
    name: str
    file: Path | None
    is_package: bool
    top_package_dir: Path | None
    namespace_dirs: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class _ThirdPartyPackage:
    name: str
    path: Path
    is_dir: bool


@dataclass
class ConsolidationPlan:
    entry: Path
    entry_module: str | None
    entry_archive: str | None
    search_paths: tuple[Path, ...]
    modules: dict[str, BundledModule]
    data_files: dict[str, Path]
    package_dirs: dict[str, tuple[Path, ...]]
    warnings: list[PlanWarning]
    synthetic_files: dict[str, bytes] = field(default_factory=dict)

    def summary(self) -> str:
        if self.entry_module is not None:
            entry_desc = f"{self.entry_module} (python -m emulation)"
        else:
            entry_desc = f"{self.entry} -> bundle:/{self.entry_archive}"
        lines = [f"entry     : {entry_desc}"]
        lines.append("search    : " + (", ".join(str(p) for p in self.search_paths) or "-"))
        modules = sorted(self.modules)
        head = ", ".join(modules[:20])
        if len(modules) > 20:
            head += f", ... +{len(modules) - 20} more"
        lines.append(f"modules   : {len(modules)} [{head}]")
        if self.synthetic_files:
            lines.append("synthetic : " + ", ".join(sorted(self.synthetic_files)))
        if self.package_dirs:
            lines.append("packages  : " + ", ".join(sorted(self.package_dirs)))
        data = sorted(self.data_files)
        head = ", ".join(data[:12])
        if len(data) > 12:
            head += f", ... +{len(data) - 12} more"
        lines.append(f"data files: {len(data)} [{head}]")
        if self.warnings:
            lines.append("warnings  :")
            lines.extend(f"  - {warning}" for warning in self.warnings)
        return "\n".join(lines)


def _module_excluded(name: str, patterns: Sequence[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) or name.startswith(pattern + ".") for pattern in patterns)


def _validate_dotted(name: str, what: str) -> str:
    if not name or not all(part.isidentifier() for part in name.split(".")):
        raise ConsolidationError(f"invalid dotted name for {what}: {name!r}")
    return name


def _flatten_search_paths(values: Iterable) -> list[Path]:
    out: list[Path] = []
    for value in values:
        if value is None:
            continue
        for part in str(value).split(os.pathsep):
            if not part:
                continue
            path = Path(part).resolve()
            if path not in out:
                out.append(path)
    return out


_GLOB_CACHE: dict[str, re.Pattern[str]] = {}


def _glob_match(pattern: str, rel_posix: str) -> bool:
    regex = _GLOB_CACHE.get(pattern)
    if regex is None:
        out: list[str] = []
        i, size = 0, len(pattern)
        while i < size:
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
            elif pattern.startswith("**", i):
                out.append(".*")
                i += 2
            elif pattern[i] == "*":
                out.append("[^/]*")
                i += 1
            elif pattern[i] == "?":
                out.append("[^/]")
                i += 1
            else:
                out.append(re.escape(pattern[i]))
                i += 1
        regex = re.compile("".join(out) + r"\Z")
        _GLOB_CACHE[pattern] = regex
    return regex.match(rel_posix) is not None


def _walk_tree(
    root: Path,
    *,
    prune_dir: Callable[[tuple[str, ...]], bool] | None = None,
    prune_file: Callable[[tuple[str, ...]], bool] | None = None,
) -> Iterator[tuple[Path, tuple[str, ...]]]:
    def recurse(directory: Path, seen: set[str]) -> Iterator[tuple[Path, tuple[str, ...]]]:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            logger.warning("cannot list directory {}: {}", directory, exc)
            return
        for entry in entries:
            name = entry.name
            if name.startswith("."):
                continue
            if name == "__pycache__" or name.endswith((".egg-info", ".dist-info")):
                continue
            path = Path(entry.path)
            if entry.is_dir():
                rel = path.relative_to(root).parts
                if prune_dir is not None and prune_dir(rel):
                    continue
                real = str(Path(path).resolve())
                if real in seen:
                    logger.warning("symlink loop detected at {}; skipping", path)
                    continue
                seen.add(real)
                yield from recurse(path, seen)
                seen.discard(real)
            elif entry.is_file():
                rel = path.relative_to(root).parts
                if prune_file is not None and prune_file(rel):
                    continue
                yield path, rel

    yield from recurse(root, {str(Path(root).resolve())})


def _module_name_for(top: str, rel_parts: tuple[str, ...]) -> tuple[str, bool] | None:
    parts = list(rel_parts)
    filename = parts.pop()
    if not filename.endswith(".py"):
        return None
    stem = filename[: -len(".py")]
    if stem == "__init__":
        if not all(part.isidentifier() for part in parts):
            return None
        return (top, True) if not parts else (f"{top}.{'.'.join(parts)}", True)
    names = [*parts, stem]
    if not all(part.isidentifier() for part in names):
        return None
    return f"{top}.{'.'.join(names)}", False


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ConsolidationError(f"cannot read {path}: {exc}") from exc


def _human_size(size: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024 or unit == "GiB":
            return f"{size:.0f} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


@dataclass(slots=True)
class _ScanResult:
    imports: set[tuple[str, bool]]
    notes: list[PlanWarning]


def _dynamic_import_target(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        if func.id == "__import__":
            return "__import__"
        if func.id == "import_module":
            return "import_module"
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "import_module"
        and isinstance(func.value, ast.Name)
        and func.value.id == "importlib"
    ):
        return "importlib.import_module"
    return None


class _ImportScanner(ast.NodeVisitor):
    def __init__(self, module_name: str, is_package: bool, filename: str) -> None:
        self.filename = filename
        self.package = module_name if is_package else module_name.rpartition(".")[0]
        self.imports: set[tuple[str, bool]] = set()
        self.notes: list[PlanWarning] = []

    def _add_strict_chain(self, name: str) -> None:
        parts = name.split(".")
        for index in range(1, len(parts) + 1):
            self.imports.add((".".join(parts[:index]), True))

    def _add_loose(self, name: str) -> None:
        self.imports.add((name, False))

    def _absolute_base(self, level: int, module: str | None) -> str | None:
        if level == 0:
            return module
        if not self.package:
            self.notes.append(PlanWarning("dynamic", f"relative import outside a package in {self.filename}"))
            return None
        parts = self.package.split(".")
        if level > 1 and level - 1 >= len(parts):
            self.notes.append(PlanWarning("dynamic", f"relative import beyond top-level package in {self.filename}"))
            return None
        base = parts if level == 1 else parts[: -(level - 1)]
        if module:
            base = [*base, *module.split(".")]
        return ".".join(base)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add_strict_chain(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = self._absolute_base(node.level, node.module)
        if base is None:
            return
        if base:
            self._add_strict_chain(base)
        for alias in node.names:
            if alias.name == "*":
                continue
            candidate = f"{base}.{alias.name}" if base else alias.name
            if not candidate.isidentifier() and "." not in candidate:
                continue
            self._add_loose(candidate)

    def visit_Call(self, node: ast.Call) -> None:
        target = _dynamic_import_target(node.func)
        if target is not None:
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                self._add_strict_chain(arg.value)
            else:
                self.notes.append(
                    PlanWarning(
                        "dynamic",
                        f"{target}() with non-literal module name in "
                        f"{self.filename}; static analysis cannot see it "
                        "(whole-package embedding covers dynamic imports "
                        "within bundled packages)",
                    )
                )
        self.generic_visit(node)


def _scan_module(module_name: str, path: Path, is_package: bool) -> _ScanResult:
    try:
        raw = path.read_bytes()
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        tree = ast.parse(raw.decode(encoding), filename=str(path))
    except (OSError, SyntaxError, UnicodeDecodeError, LookupError, ValueError, RecursionError) as exc:
        return _ScanResult(set(), [PlanWarning("parse", f"could not parse {path}: {exc}")])
    scanner = _ImportScanner(module_name, is_package, str(path))
    scanner.visit(tree)
    return _ScanResult(scanner.imports, scanner.notes)


def _descend_package(pkg_dir: Path, rest: Sequence[str]) -> tuple[str, Path | None]:
    current = pkg_dir
    for index, part in enumerate(rest):
        last = index == len(rest) - 1
        sub = current / part
        if (sub / "__init__.py").is_file():
            if last:
                return "package", sub / "__init__.py"
            current = sub
            continue
        if last:
            if (current / f"{part}.py").is_file():
                return "module", current / f"{part}.py"
            if sub.is_dir():
                return "namespace", None
            return "", None
        if sub.is_dir():
            current = sub
            continue
        return "", None
    return "", None


def _locate(name: str, search_paths: Sequence[Path]) -> _Located | None:
    parts = name.split(".")
    top = parts[0]
    top_dir: Path | None = None
    ns_dirs: list[Path] = []
    for root in search_paths:
        pkg_dir = root / top
        if (pkg_dir / "__init__.py").is_file():
            top_dir = pkg_dir
            break
        module_file = root / f"{top}.py"
        if module_file.is_file():
            if len(parts) == 1:
                return _Located(name, module_file, False, None, ())
            continue
        if pkg_dir.is_dir():
            ns_dirs.append(pkg_dir)
    if top_dir is not None:
        if len(parts) == 1:
            return _Located(name, top_dir / "__init__.py", True, top_dir, ())
        kind, file = _descend_package(top_dir, parts[1:])
        if not kind:
            return None
        return _Located(name, file, kind != "module", top_dir, ())
    if ns_dirs:
        if len(parts) == 1:
            return _Located(name, None, True, None, tuple(ns_dirs))
        for ns_root in ns_dirs:
            kind, file = _descend_package(ns_root, parts[1:])
            if kind:
                return _Located(name, file, kind != "module", None, tuple(ns_dirs))
    return None


def _site_dirs(extra: Sequence[Path]) -> list[Path]:
    dirs: list[Path] = [path for path in extra if path.is_dir()]
    try:
        import site

        candidates = [*site.getsitepackages(), site.getusersitepackages()]
    except Exception:
        candidates = []
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_dir() and path not in dirs:
            dirs.append(path)
    return dirs


def _resolve_thirdparty(requests: Sequence, site_paths: Sequence) -> dict[str, _ThirdPartyPackage]:
    resolved: dict[str, _ThirdPartyPackage] = {}
    if not requests:
        return resolved
    dirs = _site_dirs(_flatten_search_paths(site_paths))
    for request in requests:
        request_path = Path(str(request))
        if request_path.is_dir() or request_path.is_file():
            name = request_path.stem if request_path.is_file() else request_path.resolve().name
            _validate_dotted(name, f"package path {request_path}")
            resolved[name] = _ThirdPartyPackage(name, request_path, request_path.is_dir())
            continue
        name = _validate_dotted(str(request), "--include-package entry")
        if name in resolved:
            continue
        found: _ThirdPartyPackage | None = None
        for directory in dirs:
            pkg_dir = directory / name
            if (pkg_dir / "__init__.py").is_file():
                found = _ThirdPartyPackage(name, pkg_dir, True)
                break
            if (directory / f"{name}.py").is_file():
                found = _ThirdPartyPackage(name, directory / f"{name}.py", False)
                break
            if pkg_dir.is_dir():
                found = _ThirdPartyPackage(name, pkg_dir, True)
                break
        if found is None:
            searched = ", ".join(str(d) for d in dirs) or "<no site directories>"
            raise ConsolidationError(f"--include-package {name!r}: package not found in: {searched}")
        resolved[name] = found
    return resolved


def _find_package_dir(directory: Path) -> Path:
    resolved = Path(directory).resolve()
    if (resolved / "__init__.py").is_file():
        return resolved
    candidates = sorted(
        child
        for child in resolved.iterdir()
        if child.is_dir()
        and (child / "__init__.py").is_file()
        and not child.name.startswith(".")
        and child.name != "__pycache__"
        and not child.name.endswith((".egg-info", ".dist-info"))
    )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ConsolidationError(f"{directory} is not a package and contains no package")
    names = ", ".join(child.name for child in candidates)
    raise ConsolidationError(f"{directory} contains multiple packages ({names}); pass one explicitly")


def _has_main_function(init_path: Path) -> bool:
    try:
        raw = init_path.read_bytes()
        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
        tree = ast.parse(raw.decode(encoding), filename=str(init_path))
    except (OSError, SyntaxError, UnicodeDecodeError, LookupError, ValueError):
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main":
            return True
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (alias.asname or alias.name) == "main":
                    return True
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "main":
                    return True
    return False


def _runpy_source(target: str) -> str:
    return f"import runpy as _runpy\n_runpy.run_module({target!r}, run_name='__main__', alter_sys=True)\n"


def _synthesize_main(pkg_name: str, pkg_dir: Path, override: str | None) -> tuple[str, list[str]]:
    if override is not None:
        parts = override.split(".")
        rel = "/".join(parts)
        module_file = pkg_dir / f"{rel}.py"
        package_init = pkg_dir / rel / "__init__.py"
        if not module_file.is_file() and not package_init.is_file():
            raise ConsolidationError(
                f"--main-module {override!r}: not found in package {pkg_name!r} "
                f"(tried {module_file} and {package_init})"
            )
        target = f"{pkg_name}.{override}"
        return _runpy_source(target), [target]
    for candidate in _ENTRY_CANDIDATES:
        if (pkg_dir / f"{candidate}.py").is_file():
            target = f"{pkg_name}.{candidate}"
            return _runpy_source(target), [target]
    init = pkg_dir / "__init__.py"
    if _has_main_function(init):
        source = (
            "import importlib as _importlib\n"
            f"_mod = _importlib.import_module({pkg_name!r})\n"
            "_main = getattr(_mod, 'main', None)\n"
            "if _main is None:\n"
            f"    raise SystemExit('package {pkg_name} has no callable main()')\n"
            "_main()\n"
        )
        return source, [pkg_name]
    tried = ", ".join(f"{candidate}.py" for candidate in _ENTRY_CANDIDATES)
    raise ConsolidationError(
        f"package {pkg_name!r} has no __main__.py and no obvious entry point "
        f"(tried {tried}, and {pkg_name}.main()); add a __main__.py, define "
        "a main() in __init__.py, or pass --main-module"
    )


def analyze_bundle(
    entry,
    *,
    add_python_path: Sequence = (),
    module_mode: bool = False,
    include_packages: Sequence = (),
    site_paths: Sequence = (),
    exclude_modules: Sequence[str] = (),
    include_data: Sequence[str] = (),
    include_package_data: Sequence[str] = (),
    exclude_data: Sequence[str] = (),
    whole_package: bool = True,
    strict: bool = False,
    main_module: str | None = None,
) -> ConsolidationPlan:
    extra_roots = _flatten_search_paths(add_python_path)
    entry_path = Path(str(entry))
    entry_file: Path | None = None
    entry_module: str | None = None
    entry_archive: str | None = None
    entry_pkg_dir: Path | None = None
    if module_mode or entry_path.is_dir():
        if entry_path.is_dir():
            pkg_dir = _find_package_dir(entry_path)
            entry_module = _validate_dotted(pkg_dir.name, "package directory")
            entry_pkg_dir = pkg_dir
            data_roots = [entry_pkg_dir.parent, *extra_roots]
            roots = [*data_roots, Path.cwd()]
        else:
            entry_module = _validate_dotted(str(entry), "module entry (-m)")
            data_roots = list(extra_roots)
            roots = [Path.cwd(), *extra_roots]
    else:
        if not entry_path.is_file():
            raise ConsolidationError(f"entry script not found: {entry}")
        entry_file = Path(entry_path).resolve()
        entry_archive = entry_file.name
        data_roots = [entry_file.parent, *extra_roots]
        roots = list(data_roots)
    search_paths: list[Path] = []
    for root in roots:
        if root not in search_paths:
            search_paths.append(root)
    data_roots = [root for root in data_roots if root in search_paths]
    modules: dict[str, BundledModule] = {}
    package_dirs: dict[str, tuple[Path, ...]] = {}
    warnings: list[PlanWarning] = []
    synthetic_files: dict[str, bytes] = {}
    scanned: set[str] = set()
    scan_queue: deque[str] = deque()
    thirdparty = _resolve_thirdparty(include_packages, site_paths)
    embedded_thirdparty: set[str] = set()

    def warn(kind: str, message: str) -> None:
        warning = PlanWarning(kind, message)
        if warning not in warnings:
            warnings.append(warning)

    def embed_module(name: str, file: Path, is_package: bool) -> None:
        existing = modules.get(name)
        if existing is not None:
            if existing.source != file:
                warn("conflict", f"module {name!r}: keeping {existing.source}, ignoring duplicate at {file}")
            return
        modules[name] = BundledModule(name, file, is_package)
        scan_queue.append(name)

    def embed_tree(top_name: str, dirs: Sequence[Path]) -> None:
        offenders: list[Path] = []

        def dir_excluded(rel_parts: tuple[str, ...]) -> bool:
            return _module_excluded(".".join((top_name, *rel_parts)), exclude_modules)

        for base in dirs:
            for file, rel in _walk_tree(base, prune_dir=dir_excluded):
                if file.suffix in _BINARY_SUFFIXES:
                    offenders.append(file)
                    continue
                if file.suffix != ".py":
                    continue
                mapped = _module_name_for(top_name, rel)
                if mapped is None:
                    warn("note", f"skipping non-importable file {file}")
                    continue
                name, is_package = mapped
                if _module_excluded(name, exclude_modules):
                    continue
                embed_module(name, file, is_package)
        merged = (
            *package_dirs.get(top_name, ()),
            *[d for d in dirs if d not in package_dirs.get(top_name, ())],
        )
        package_dirs[top_name] = tuple(dict.fromkeys(merged))
        if offenders:
            listing = ", ".join(str(p) for p in offenders[:5])
            raise ConsolidationError(
                f"package {top_name!r} contains compiled extension(s): "
                f"{listing}. pyrefactor --bundle packs pure-Python packages only; "
                "exclude the offending subtree with --exclude-module if it "
                "is unused."
            )

    def embed_thirdparty(top_name: str, package: _ThirdPartyPackage) -> None:
        if top_name in embedded_thirdparty:
            return
        embedded_thirdparty.add(top_name)
        if package.is_dir:
            embed_tree(top_name, [package.path])
        else:
            embed_module(top_name, package.path, False)

    def embed_located(located: _Located, name: str) -> None:
        top = name.split(".", maxsplit=1)[0]
        if whole_package and located.namespace_dirs:
            embed_tree(top, located.namespace_dirs)
        elif whole_package and located.top_package_dir is not None:
            embed_tree(top, [located.top_package_dir])
        elif located.file is not None:
            embed_module(name, located.file, located.is_package)

    def handle_import(name: str, strict_flag: bool, importer: str) -> None:
        top = name.split(".", maxsplit=1)[0]
        if name in modules:
            return
        if strict_flag and _module_excluded(name, exclude_modules):
            warn(
                "excluded",
                f"{name!r} matches an exclusion pattern but is imported by "
                f"{importer}; the bundle will fail if that import executes",
            )
            return
        located = _locate(name, search_paths)
        if located is not None:
            if _module_excluded(name, exclude_modules):
                warn(
                    "excluded",
                    f"{name!r} matches an exclusion pattern but is imported "
                    f"by {importer}; the bundle will fail if that import "
                    "executes",
                )
                return
            embed_located(located, name)
            return
        if top in _IGNORED_TOPS:
            return
        if top in sys.stdlib_module_names:
            return
        if top in thirdparty:
            embed_thirdparty(top, thirdparty[top])
            return
        if not strict_flag:
            return
        if name == top:
            warn(
                "unresolved",
                f"cannot resolve import {name!r} (imported by {importer}); "
                "not found in the search paths, not stdlib and not bundled. "
                "Add its directory with --add-python-path, or use "
                f"--include-package {top} if it is a third-party dependency.",
            )
        elif top in modules:
            warn(
                "broken",
                f"import chain {name!r} (imported by {importer}): {name!r} not found inside bundled package {top!r}",
            )

    def process_scan(result: _ScanResult, importer: str) -> None:
        for note in result.notes:
            if note not in warnings:
                warnings.append(note)
        for name, strict_flag in sorted(result.imports):
            handle_import(name, strict_flag, importer)

    def ensure_main_module(pkg_dir: Path, pkg_module_name: str) -> None:
        archive = f"{pkg_module_name.replace('.', '/')}/__main__.py"
        real_main = pkg_dir / "__main__.py"
        if main_module is not None:
            source, extra = _synthesize_main(pkg_module_name, pkg_dir, main_module)
            synthetic_files[archive] = source.encode("utf-8")
            for dep in extra:
                handle_import(dep, True, f"<synthesized {pkg_module_name}.__main__>")
            return
        if real_main.is_file():
            embed_module(f"{pkg_module_name}.__main__", real_main, False)
            return
        source, extra = _synthesize_main(pkg_module_name, pkg_dir, None)
        synthetic_files[archive] = source.encode("utf-8")
        for dep in extra:
            handle_import(dep, True, f"<synthesized {pkg_module_name}.__main__>")

    if entry_module is not None:
        if entry_pkg_dir is not None:
            if whole_package:
                embed_tree(entry_module, [entry_pkg_dir])
            else:
                embed_module(entry_module, entry_pkg_dir / "__init__.py", True)
            ensure_main_module(entry_pkg_dir, entry_module)
        else:
            located = _locate(entry_module, search_paths)
            if located is None:
                raise ConsolidationError(
                    f"module {entry_module!r} not found in: {', '.join(str(p) for p in search_paths)}"
                )
            embed_located(located, entry_module)
            if located.is_package:
                if located.file is not None:
                    ensure_main_module(located.file.parent, entry_module)
                elif f"{entry_module}.__main__" not in modules:
                    raise ConsolidationError(
                        f"namespace package {entry_module!r} has no "
                        "__main__.py; cannot synthesize one for namespace "
                        "packages — add a __main__.py to one of its portions"
                    )
    else:
        assert entry_file is not None
        stem = entry_file.stem if entry_file.stem.isidentifier() else "entry"
        entry_scan = _scan_module(stem, entry_file, False)
        scanned.add(stem)
        process_scan(entry_scan, f"<entry {entry_file.name}>")
    while scan_queue:
        name = scan_queue.popleft()
        if name in scanned:
            continue
        scanned.add(name)
        module = modules.get(name)
        if module is None or not module.source.is_file():
            continue
        process_scan(_scan_module(name, module.source, module.is_package), name)
    for top_name, package in sorted(thirdparty.items()):
        if top_name in embedded_thirdparty:
            continue
        if any(m == top_name or m.startswith(top_name + ".") for m in modules):
            warn(
                "note",
                f"--include-package {top_name}: a local copy was already bundled; the third-party entry was ignored",
            )
            continue
        embed_thirdparty(top_name, package)
    data_files = _collect_data(
        specs=include_data,
        package_data=include_package_data,
        data_excludes=exclude_data,
        exclude_modules=exclude_modules,
        data_roots=tuple(data_roots),
        package_dirs=package_dirs,
        modules=modules,
        warn=warn,
    )
    warnings.sort(key=lambda warning: (warning.kind, warning.message))
    if strict:
        failures = [w for w in warnings if w.kind in _STRICT_WARNING_KINDS]
        if failures:
            detail = "\n  ".join(str(w) for w in failures)
            raise ConsolidationError(f"strict mode: {len(failures)} unresolved import(s):\n  {detail}")
    return ConsolidationPlan(
        entry=entry_file or entry_pkg_dir or Path(str(entry)).resolve(),
        entry_module=entry_module,
        entry_archive=entry_archive,
        search_paths=tuple(search_paths),
        modules=modules,
        data_files=data_files,
        package_dirs=package_dirs,
        warnings=warnings,
        synthetic_files=synthetic_files,
    )


def _parse_data_spec(spec, data_roots: Sequence[Path], package_dirs: dict, warn: Callable[[str, str], None]):
    spec = str(spec)
    if ":" in spec and not (len(spec) > 1 and spec[1] == ":" and spec[0].isalpha() and "\\" in spec):
        prefix, pattern = spec.split(":", 1)
        pattern = pattern or "**"
    else:
        prefix, pattern = None, spec
    if prefix is None:
        path = Path(spec)
        if path.is_dir():
            return [(path, path.name)], "**"
        if path.is_file():
            return [(path.parent, "")], path.name
        return [(root, "") for root in data_roots], spec
    if prefix in package_dirs:
        return [(d, prefix) for d in package_dirs[prefix]], pattern
    for root in data_roots:
        if (root / prefix).is_dir():
            return [(root / prefix, prefix)], pattern
    path = Path(prefix)
    if path.is_dir():
        return [(path, path.name)], pattern
    warn("note", f"--include-data {spec!r}: {prefix!r} is neither a bundled package nor an existing directory")
    return None


def _collect_data(
    *,
    specs: Sequence[str],
    package_data: Sequence[str],
    data_excludes: Sequence[str],
    exclude_modules: Sequence[str],
    data_roots: tuple[Path, ...],
    package_dirs: dict[str, tuple[Path, ...]],
    modules: dict[str, BundledModule],
    warn: Callable[[str, str], None],
) -> dict[str, Path]:
    out: dict[str, Path] = {}
    module_archives = {module.archive_path for module in modules.values()}
    all_excludes = [*_DEFAULT_DATA_EXCLUDES, *data_excludes]

    def excluded(archive: str) -> bool:
        return any(_glob_match(pattern, archive) for pattern in all_excludes)

    def add(archive: str, source: Path) -> None:
        if excluded(archive):
            return
        if archive in module_archives:
            warn("conflict", f"data file {source} collides with bundled module {archive}; the module wins")
            return
        existing = out.get(archive)
        if existing is not None:
            if existing != source:
                warn("conflict", f"bundle path {archive!r} already provided by {existing}; ignoring {source}")
            return
        out[archive] = source

    if "*" in package_data:
        targets = sorted(package_dirs.items())
    else:
        targets = sorted((name, package_dirs[name]) for name in set(package_data) if name in package_dirs)
        for name in package_data:
            if name != "*" and name not in package_dirs:
                warn("note", f"--include-package-data {name!r}: not a bundled package directory")
    for pkg_name, dirs in targets:
        for base in dirs:

            def prune_pkg_dir(rel_parts: tuple[str, ...], _pkg: str = pkg_name) -> bool:
                return _module_excluded(".".join((_pkg, *rel_parts)), exclude_modules)

            for file, rel in _walk_tree(base, prune_dir=prune_pkg_dir):
                if file.suffix == ".py":
                    continue
                add(f"{pkg_name}/{'/'.join(rel)}", file)
    for spec in specs:
        parsed = _parse_data_spec(spec, data_roots, package_dirs, warn)
        if parsed is None:
            continue
        base_dirs, pattern = parsed
        for base, arc_prefix in base_dirs:

            def prune_dir(rel_parts: tuple[str, ...], _base: Path = base) -> bool:
                if rel_parts and rel_parts[0] in package_dirs:
                    return _module_excluded(".".join(rel_parts), exclude_modules)
                return False

            for file, rel in _walk_tree(base, prune_dir=prune_dir):
                if file.suffix == ".py":
                    continue
                rel_posix = "/".join(rel)
                if not _glob_match(pattern, rel_posix):
                    continue
                archive = f"{arc_prefix}/{rel_posix}" if arc_prefix else rel_posix
                add(archive, file)
    return out


_BOOTSTRAP_TEMPLATE = """\
import atexit
import base64
import io
import os
import runpy
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

_BUNDLE = (
@@BUNDLE@@
)

_ENTRY_PATH = @@ENTRY_PATH@@
_ENTRY_MODULE = @@ENTRY_MODULE@@

_KEEP = @@KEEP@@ or os.environ.get("PYREFACTOR_BUNDLE_KEEP") == "1"
_DEBUG = @@DEBUG@@ or os.environ.get("PYREFACTOR_BUNDLE_DEBUG") == "1"

_IS_FORKED_CHILD = False


def _mark_forked_child() -> None:
    global _IS_FORKED_CHILD
    _IS_FORKED_CHILD = True


try:
    os.register_at_fork(after_in_child=_mark_forked_child)
except (AttributeError, NotImplementedError):
    pass


class BundleError(RuntimeError):
    pass


def _validate_member(name: str) -> str:
    name = name.rstrip("/")
    if not name or name.startswith("/") or "\\x00" in name:
        raise BundleError(f"unsafe bundle member {name!r}")
    for part in name.split("/"):
        if part in ("", ".", ".."):
            raise BundleError(f"unsafe bundle member {name!r}")
    return name


def _extract_bundle() -> str:
    root = tempfile.mkdtemp(prefix="pyrefactor-bundle-")
    try:
        raw = base64.b64decode(_BUNDLE)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for info in archive.infolist():
                name = _validate_member(info.filename)
                target = str(Path(root).joinpath(*name.split("/")))
                if info.is_dir():
                    os.makedirs(target, exist_ok=True)
                    continue
                parent = str(Path(target).parent)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with archive.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                mode = (info.external_attr >> 16) & 0o777
                os.chmod(target, mode if mode & 0o400 else 0o644)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return root


def _cleanup(root: str) -> None:
    if not _IS_FORKED_CHILD:
        shutil.rmtree(root, ignore_errors=True)


def _bootstrap() -> str:
    root = _extract_bundle()
    sys.path.insert(0, root)
    if _DEBUG:
        print(f"pyrefactor-bundle: extracted to {root}", file=sys.stderr)
    if not _KEEP:
        atexit.register(_cleanup, root)
    return root


if __name__ in ("__main__", "__mp_main__"):
    _CONSOLIDATOR_ROOT = _bootstrap()

if __name__ == "__main__":
    if _ENTRY_MODULE is not None:
        runpy.run_module(_ENTRY_MODULE, run_name="__main__", alter_sys=True)
    else:
        runpy.run_path(
            str(Path(_CONSOLIDATOR_ROOT) / _ENTRY_PATH), run_name="__main__"
        )
"""


def _zip_timestamp(deterministic: bool) -> tuple[int, int, int, int, int, int]:
    epoch = os.environ.get("SOURCE_DATE_EPOCH", "")
    if epoch.isdigit():
        moment = time.gmtime(int(epoch))
        return (max(moment.tm_year, 1980), moment.tm_mon, moment.tm_mday, moment.tm_hour, moment.tm_min, moment.tm_sec)
    if deterministic:
        return _DETERMINISTIC_TIMESTAMP
    moment = time.localtime()
    return (moment.tm_year, moment.tm_mon, moment.tm_mday, moment.tm_hour, moment.tm_min, moment.tm_sec)


def _archive_members(plan: ConsolidationPlan) -> dict[str, Path | bytes]:
    members: dict[str, Path | bytes] = {}
    if plan.entry_module is None:
        assert plan.entry_archive is not None
        members[plan.entry_archive] = plan.entry
    for module in sorted(plan.modules.values(), key=lambda m: m.archive_path):
        archive = module.archive_path
        existing = members.get(archive)
        if existing is not None and existing != module.source:
            raise ConsolidationError(
                f"internal error: archive path {archive!r} maps to both {existing} and {module.source}"
            )
        members[archive] = module.source
    for archive, source in sorted(plan.synthetic_files.items()):
        members[archive] = source
    for archive, source in sorted(plan.data_files.items()):
        if archive in members:
            continue
        members[archive] = source
    return members


def _render_header(plan: ConsolidationPlan, *, compression: str, deterministic: bool) -> str:
    top_names = sorted({module.name.split(".")[0] for module in plan.modules.values()})
    lines = [
        "# " + "=" * 74,
        "# pyrefactor self-contained bundle",
        f"# entry    : {plan.entry_module or plan.entry_archive or plan.entry}",
        f"# modules  : {len(plan.modules)} (top-level: {', '.join(top_names) or '-'})",
        f"# data     : {len(plan.data_files)} file(s)",
        f"# built by : pyrefactor --bundle (python {platform.python_version()}, {compression})",
    ]
    if plan.synthetic_files:
        lines.append("# synthetic: " + ", ".join(sorted(plan.synthetic_files)))
    epoch = os.environ.get("SOURCE_DATE_EPOCH", "")
    if not deterministic:
        lines.append(f"# built at : {time.strftime('%Y-%m-%dT%H:%M:%S%z')}")
    elif epoch:
        lines.append(f"# built at : SOURCE_DATE_EPOCH={epoch}")
    lines += [
        "#",
        "# Running this file extracts the bundle into a private temp dir,",
        "# prepends it to sys.path and executes the entry point unchanged.",
        "# Debug: PYREFACTOR_BUNDLE_DEBUG=1   Keep files: PYREFACTOR_BUNDLE_KEEP=1",
        "# " + "=" * 74,
    ]
    return "\n".join(lines)


def render_bundle(
    plan: ConsolidationPlan,
    *,
    python: str | None = None,
    compression: str = "deflate",
    runtime_debug: bool = False,
    keep_tmpdir: bool = False,
    deterministic: bool = True,
) -> str:
    if compression not in _COMPRESSIONS:
        raise ConsolidationError(f"unknown compression {compression!r}; expected one of {sorted(_COMPRESSIONS)}")
    interpreter = python or sys.executable or "python3"
    members = _archive_members(plan)
    timestamp = _zip_timestamp(deterministic)
    compress_type = _COMPRESSIONS[compression]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compress_type) as archive:
        for archive_path, source in members.items():
            info = zipfile.ZipInfo(archive_path, date_time=timestamp)
            info.create_system = 3
            if isinstance(source, bytes):
                data = source
                mode = 0o644
            else:
                data = _read_bytes(source)
                mode = 0o755 if os.access(source, os.X_OK) else 0o644
            info.external_attr = mode << 16
            info.compress_type = compress_type
            archive.writestr(info, data)
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")
    chunks = "\n".join(f'    "{payload[index : index + 76]}"' for index in range(0, len(payload), 76))
    if not chunks:
        chunks = '    ""'
    body = (
        _BOOTSTRAP_TEMPLATE.replace("@@BUNDLE@@", chunks)
        .replace("@@ENTRY_PATH@@", repr(plan.entry_archive))
        .replace("@@ENTRY_MODULE@@", repr(plan.entry_module))
        .replace("@@DEBUG@@", "True" if runtime_debug else "False")
        .replace("@@KEEP@@", "True" if keep_tmpdir else "False")
    )
    header = _render_header(plan, compression=compression, deterministic=deterministic)
    return f"#!{interpreter}\n{header}\n{body}"


def write_bundle_output(text: str, output) -> Path | None:
    if output is None or str(output) == "-":
        sys.stdout.write(text)
        return None
    path = Path(output)
    if path.parent and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    if text.startswith("#!"):
        os.chmod(path, 0o755)
    return path


def consolidate_bundle(
    entry,
    output=None,
    *,
    add_python_path: Sequence = (),
    module_mode: bool = False,
    include_packages: Sequence = (),
    site_paths: Sequence = (),
    exclude_modules: Sequence[str] = (),
    include_data: Sequence[str] = (),
    include_package_data: Sequence[str] = (),
    exclude_data: Sequence[str] = (),
    whole_package: bool = True,
    strict: bool = False,
    main_module: str | None = None,
    python: str | None = None,
    compression: str = "deflate",
    runtime_debug: bool = False,
    keep_tmpdir: bool = False,
    deterministic: bool = True,
    dry_run: bool = False,
) -> Path | None:
    plan = analyze_bundle(
        entry,
        add_python_path=add_python_path,
        module_mode=module_mode,
        include_packages=include_packages,
        site_paths=site_paths,
        exclude_modules=exclude_modules,
        include_data=include_data,
        include_package_data=include_package_data,
        exclude_data=exclude_data,
        whole_package=whole_package,
        strict=strict,
        main_module=main_module,
    )
    for warning in plan.warnings:
        logger.warning("{}", warning)
    if dry_run:
        sys.stdout.write(plan.summary() + "\n")
        return None
    text = render_bundle(
        plan,
        python=python,
        compression=compression,
        runtime_debug=runtime_debug,
        keep_tmpdir=keep_tmpdir,
        deterministic=deterministic,
    )
    path = write_bundle_output(text, output)
    if path is not None:
        stem = path.stem
        if stem in sys.stdlib_module_names:
            logger.warning(
                "output file {} shadows the standard-library module {!r}; "
                "imports of {!r} inside the bundle may pick up the bundle "
                "itself — pick another file name",
                path.name,
                stem,
                stem,
            )
        logger.info(
            "wrote {} ({}, {} modules, {} synthetic, {} data files)",
            path,
            _human_size(len(text.encode("utf-8"))),
            len(plan.modules),
            len(plan.synthetic_files),
            len(plan.data_files),
        )
    return path


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pyrefactor",
        description=(
            "Consolidate a python module/package/project into a single, dependency-ordered file,\n"
            "or pack it into a self-extracting zip bundle (--bundle)."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
    )
    p.add_argument(
        "source",
        nargs="?",
        type=str,
        help=(
            "package directory, project root or single .py file\n"
            "(optional with --merged-file; treated as a module name with -m/--module)"
        ),
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("output"),
        help=(
            "output directory for source mode (default: ./output);\n"
            "output file for bundle mode (default: ./output/<name>_standalone.py)"
        ),
    )
    p.add_argument(
        "--name", default=None, metavar="NAME", help="name of the generated file\n(default: the source name)"
    )
    p.add_argument("-j", "--workers", type=int, default=None, help="parallel workers (default: CPU count)")
    p.add_argument(
        "--executor",
        choices=["process", "thread"],
        default="process",
        help="executor type for parallel parsing (default: process)",
    )
    p.add_argument("--progress", action="store_true", help="show progress bar during parsing")
    p.add_argument(
        "-x",
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="extra path glob to skip (repeatable)\nprecedence: default excludes < --include < --exclude",
    )
    p.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="GLOB",
        help="glob to re-include files that would otherwise be excluded",
    )
    p.add_argument(
        "--copy-data",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="copy data files (json, yaml, csv, txt, ...) to output (default: on)",
    )
    p.add_argument(
        "--embed-data",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "embed data files into the consolidated file so it is fully\n"
            "self-contained: text files (json, ini, ...) as plain text,\n"
            "binary files (fonts, ...) as base64\n"
            "(the module then exposes get_resource()/resource_path())\n"
            "(default: on; use --no-embed-data to disable)"
        ),
    )
    p.add_argument(
        "--max-embed-size",
        type=int,
        default=10 * 1024 * 1024,
        metavar="BYTES",
        help=(
            "maximum size in bytes for a single embedded data file\n"
            "(default: 10485760; larger files are skipped with a warning)"
        ),
    )
    p.add_argument(
        "--data-glob",
        action="append",
        default=[],
        metavar="GLOB",
        help="only copy/embed data files matching this glob (repeatable)",
    )
    p.add_argument(
        "--entry",
        action="append",
        default=[],
        metavar="MODULE",
        help="only keep the __main__ guard of this module (repeatable)",
    )
    p.add_argument(
        "--entry-keep-all", action="store_true", help="keep all __main__ guards instead of filtering to --entry"
    )
    p.add_argument(
        "--main-name",
        type=str,
        default=None,
        metavar="NAME",
        help="rename `main` function to NAME (e.g., --main-name cli_main)",
    )
    p.add_argument(
        "--generate-guard",
        action="store_true",
        help='auto-generate `if __name__ == "__main__": main()` if no guard exists',
    )
    p.add_argument(
        "--imports",
        choices=["prune", "keep", "strict"],
        default="prune",
        help="import handling: prune (default, drop unused), keep (keep all),\nstrict (prune and warn on unused)",
    )
    p.add_argument(
        "--keep-docstrings", action=argparse.BooleanOptionalAction, default=True, help="keep docstrings (default: on)"
    )
    p.add_argument(
        "--keep-comments", action=argparse.BooleanOptionalAction, default=True, help="keep comments (default: on)"
    )
    p.add_argument("--dry-run", action="store_true", help="show the plan and stats without writing files")
    p.add_argument(
        "--check", action="store_true", help="verify no changes are needed (fails if the output would change)"
    )
    p.add_argument("--stdout", action="store_true", help="write consolidated output to stdout instead of files")
    p.add_argument("--diff", action="store_true", help="emit a unified diff against the existing output (if present)")
    p.add_argument("--force", action="store_true", help="always write, even if the output directory already exists")
    p.add_argument(
        "--modernize",
        default=None,
        metavar="PASSES",
        help=(
            "comma-separated modernization passes to apply to the output:\n"
            "six (six -> python3), ospath (os.path -> pathlib)\n"
            "e.g. --modernize six,ospath"
        ),
    )
    p.add_argument(
        "-f",
        "--merged-file",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "read source from a single merged file using `# File: <relpath>`\n"
            "section headers (the ru.txt format) instead of a directory"
        ),
    )
    bundle = p.add_argument_group("bundle mode (--bundle)")
    bundle.add_argument(
        "--bundle",
        action="store_true",
        help=(
            "pack the package verbatim into a self-extracting zip bundle\n"
            "(embeds a bootstrap that extracts to a temp dir at runtime and\n"
            "runs the entry via runpy; preserves dynamic features intact)"
        ),
    )
    bundle.add_argument(
        "-m", "--module", action="store_true", help="treat SOURCE as a module name, emulating `python -m SOURCE`"
    )
    bundle.add_argument(
        "--main-module",
        default=None,
        metavar="MOD",
        help=(
            "module inside the entry package to run as its __main__\n"
            "(dotted, relative to the package; auto-detected if omitted)"
        ),
    )
    bundle.add_argument(
        "--include-package",
        action="append",
        default=[],
        metavar="PKG",
        help="pure-Python third-party package to bundle (name or path; repeatable)",
    )
    bundle.add_argument(
        "--site-path",
        action="append",
        default=[],
        metavar="DIR",
        help="extra directories searched for --include-package names (repeatable)",
    )
    bundle.add_argument(
        "-p",
        "--add-python-path",
        action="append",
        default=[],
        metavar="DIR",
        help="extra directories to search for local packages (repeatable;\n':'-separated lists allowed)",
    )
    bundle.add_argument(
        "--exclude-module",
        action="append",
        default=[],
        metavar="PAT",
        help="exclude modules/packages (fnmatch on dotted names;\na bare name excludes its whole subtree; repeatable)",
    )
    bundle.add_argument(
        "--include-data",
        action="append",
        default=[],
        metavar="SPEC",
        help=(
            "embed data files: 'PKG:GLOB', 'GLOB' (relative to entry dir and\n"
            "--add-python-path roots), or an existing dir/file path (repeatable)"
        ),
    )
    bundle.add_argument(
        "--include-package-data",
        action="append",
        default=[],
        metavar="PKG",
        help="embed data files of a bundled package ('*' for all; repeatable)",
    )
    bundle.add_argument(
        "--exclude-data",
        action="append",
        default=[],
        metavar="GLOB",
        help="exclude data files (glob matched against bundle-relative paths)",
    )
    bundle.add_argument(
        "--no-whole-package",
        action="store_true",
        help="embed only statically reachable modules instead of whole packages",
    )
    bundle.add_argument(
        "--compression",
        choices=["stored", "deflate", "bzip2", "lzma"],
        default="deflate",
        help="payload compression for --bundle (default: deflate)",
    )
    bundle.add_argument(
        "--python",
        default=None,
        metavar="INTERP",
        help="shebang interpreter for the bundle (default: current python)",
    )
    bundle.add_argument(
        "--bundle-runtime-debug",
        action="store_true",
        help="the generated bundle prints its extraction directory to stderr",
    )
    bundle.add_argument(
        "--bundle-keep-tmpdir",
        action="store_true",
        help="the generated bundle keeps its extraction directory",
    )
    bundle.add_argument("--bundle-strict", action="store_true", help="fail the bundle build on unresolved imports")
    bundle.add_argument(
        "--no-deterministic",
        action="store_true",
        help="embed the build date in the bundle instead of a fixed timestamp",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="path to pyproject.toml containing a [tool.pyrefactor] section",
    )
    p.add_argument("--format", action="store_true", help="run the configured formatter on the generated file")
    p.add_argument(
        "--formatter",
        choices=["black", "ruff"],
        default="black",
        help="formatter used by --format (default: black)",
    )
    p.add_argument(
        "--log-level",
        default="INFO",
        choices=["TRACE", "DEBUG", "INFO", "WARNING", "ERROR"],
        help="log level (default: INFO)",
    )
    p.add_argument("--log-file", type=Path, default=None, help="also log to this file")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _bundle_default_output(source: str | None, requested: Path) -> Path:
    stem = "bundle"
    if source:
        pp = Path(source)
        candidate = pp.stem if pp.stem and pp.suffix == ".py" else pp.name
        if candidate:
            stem = candidate
    if requested.suffix == ".py":
        return requested
    return requested / f"{stem}_standalone.py"


def _run_bundle(args) -> int:
    if args.source is None:
        logger.error("--bundle requires a SOURCE (entry script, package dir, or module name)")
        return 2
    output = None if args.stdout else _bundle_default_output(args.source, args.output)
    consolidate_bundle(
        args.source,
        output=output,
        add_python_path=tuple(args.add_python_path),
        module_mode=args.module,
        include_packages=tuple(args.include_package),
        site_paths=tuple(args.site_path),
        exclude_modules=tuple(args.exclude_module),
        include_data=tuple(args.include_data),
        include_package_data=tuple(args.include_package_data),
        exclude_data=tuple(args.exclude_data),
        whole_package=not args.no_whole_package,
        strict=args.bundle_strict,
        main_module=args.main_module,
        python=args.python,
        compression=args.compression,
        runtime_debug=args.bundle_runtime_debug,
        keep_tmpdir=args.bundle_keep_tmpdir,
        deterministic=not args.no_deterministic,
        dry_run=args.dry_run,
    )
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, args.log_file)
    if args.merged_file is None and args.source is None:
        logger.error("provide a SOURCE, or use --merged-file PATH")
        return 2
    if args.bundle and args.merged_file is not None:
        logger.error("--bundle cannot be combined with --merged-file")
        return 2
    if args.workers is not None and args.workers < 1:
        logger.error("--workers must be at least 1")
        return 2
    if args.max_embed_size < 0:
        logger.error("--max-embed-size must not be negative")
        return 2
    try:
        if args.bundle:
            return _run_bundle(args)
        result = consolidate(
            args.source if args.source is not None else str(args.merged_file),
            args.output,
            filename=args.name,
            workers=args.workers,
            executor=args.executor,
            exclude=tuple(args.exclude),
            include=tuple(args.include),
            imports_mode=args.imports,
            entry=tuple(args.entry),
            dry_run=args.dry_run,
            check=args.check,
            stdout=args.stdout,
            config=args.config,
            do_format=args.format,
            formatter=args.formatter,
            diff=args.diff,
            force=args.force,
            keep_docstrings=args.keep_docstrings,
            keep_comments=args.keep_comments,
            progress=args.progress,
            copy_data=args.copy_data,
            embed_data=args.embed_data,
            max_embed_bytes=args.max_embed_size,
            data_glob=tuple(args.data_glob),
            entry_keep_all=args.entry_keep_all,
            main_name=args.main_name,
            generate_guard=args.generate_guard,
            modernize=args.modernize,
            merged_file=args.merged_file,
        )
    except ConsolidationError as exc:
        logger.error("{}", exc)
        return 2
    except Exception:
        logger.exception("Unexpected failure")
        return 1
    if result.dry_run:
        logger.info(
            "{}: {} modules, {} skipped, {} exports, {} warnings",
            "Check" if args.check else "Dry-run",
            result.modules,
            len(result.skipped),
            result.exports,
            len(result.warnings),
        )
        return 0
    if result.files:
        logger.success("{} modules -> {} ({} exported names)", result.modules, result.output_dir, result.exports)
    elif args.stdout:
        logger.success("{} modules -> stdout ({} exported names)", result.modules, result.exports)
    if result.copied_data_files:
        logger.info("Copied {} data file(s)", len(result.copied_data_files))
    if result.embedded_data_files:
        logger.info(
            "Embedded {} data file(s): {}",
            len(result.embedded_data_files),
            ", ".join(result.embedded_data_files),
        )
    if result.skipped:
        logger.error("{} file(s) skipped because of errors", len(result.skipped))
    return 0


def _run_with_big_stack(func, *args):
    """Run ``func`` in a thread with a large stack so deeply nested CSTs parse."""
    try:
        previous = threading.stack_size(128 * 1024 * 1024)
    except (ValueError, RuntimeError):
        return func(*args)
    box: dict = {}

    def target() -> None:
        try:
            threading.stack_size(previous)  # don't give pool threads huge stacks
        except (ValueError, RuntimeError):
            pass
        try:
            box["value"] = func(*args)
        except BaseException as exc:  # re-raised in the caller
            box["error"] = exc

    thread = threading.Thread(target=target, name="pyrefactor-main")
    try:
        thread.start()
    except RuntimeError:
        threading.stack_size(previous)
        return func(*args)
    thread.join()
    threading.stack_size(previous)
    if "error" in box:
        raise box["error"]
    return box["value"]


def _entry() -> int:
    sys.setrecursionlimit(max(sys.getrecursionlimit(), _RECURSION_LIMIT))
    return _run_with_big_stack(main)


__all__ = [
    "ConsolidationError",
    "ConsolidationPlan",
    "ConsolidationResult",
    "Plan",
    "consolidate",
    "consolidate_bundle",
    "validate_syntax",
    "main",
    "__version__",
]


if __name__ == "__main__":
    sys.exit(_entry())
