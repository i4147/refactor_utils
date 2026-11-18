"""Read a source tree from a single concatenated file using `# File:` headers.

This is the format produced by ``pyrefactor --stdout`` style dumps and by the
``ru.txt``-style concatenations: each module is preceded by a ``# File: <relpath>``
marker line and the bodies follow verbatim.  Loading such a file lets pyrefactor
re-consolidate an already-merged bundle (round-trip), and it is the format used
by this project's own source archive.

Ported from ``consolidate.py``'s ``load_merged`` and adapted to produce the
``SourceFile`` records that the rest of pyrefactor expects.
"""

from __future__ import annotations

import re
from pathlib import Path

from .logging_setup import logger
from .models import SourceFile

HEADER_RE = re.compile(r"^#\s*File:\s*(.+?)\s*$", re.MULTILINE)


def parse_merged_file(text: str) -> list[tuple[str, str]]:
    """Split *text* on ``# File:`` headers into ``(relative_path, body)`` pairs."""
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


def _derive_module_layout(pairs: list[tuple[str, str]], stem: str) -> tuple[str, list[tuple[Path, str]]]:
    """Pick a top-level package name and per-entry relative paths.

    Two header styles are supported:

    * **prefixed** — every header shares the same first path component, e.g.
      ``# File: mypkg/cli.py``. That component is the package name and is
      stripped from each entry.
    * **bare** — headers are flat filenames, e.g. ``# File: cli.py`` (the
      ``ru.txt`` format). The merged file's *stem* is used as a synthetic
      package name and each module is nested under it, so ``from .core import
      X`` in ``cli`` resolves to ``core`` exactly as it would for a real
      ``<stem>/`` directory.
    """
    rels = [Path(rel) for rel, _ in pairs]
    top: str
    rel_paths: list[Path]
    if rels and all(len(p.parts) >= 2 for p in rels):
        common = rels[0].parts[0]
        if common.isidentifier() and all(p.parts[0] == common for p in rels):
            top = common
            rel_paths = [Path(*p.parts[1:]) for p in rels]
        else:
            top = stem
            rel_paths = list(rels)
    else:
        top = stem
        rel_paths = list(rels)
    if not top.isidentifier():
        # fall back to a safe identifier if the stem is not one
        import re

        top = re.sub(r"\W+", "_", top).strip("_") or "merged"
    return top, rel_paths


def load_merged_sources(path: Path | str) -> tuple[list[SourceFile], Path]:
    """Return ``(source_files, root)`` for a merged ``# File:`` document.

    The files are materialised under a throwaway ``root`` directory (nested
    inside a synthetic package directory named after the merged file's stem, or
    after the shared prefix when the headers are already prefixed) so the rest
    of the pipeline — which expects real paths and package-relative imports —
    behaves exactly like directory mode.
    """
    import tempfile

    src_path = Path(path)
    if not src_path.is_file():
        raise FileNotFoundError(f"merged file not found: {src_path}")
    text = src_path.read_text(encoding="utf-8")
    pairs = parse_merged_file(text)
    stem = src_path.stem
    top, rel_paths = _derive_module_layout(pairs, stem)
    root = Path(tempfile.mkdtemp(prefix="pyrefactor_merged_"))
    pkg_root = root / top
    files: list[SourceFile] = []
    for (rel, body), rel_path in zip(pairs, rel_paths):
        if rel_path.suffix != ".py":
            logger.warning("merged file entry {!r} is not a .py file; skipping", rel)
            continue
        target = pkg_root / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        parts = [p for p in rel_path.with_suffix("").parts if p not in (".", "..")]
        is_pkg = bool(parts) and parts[-1] == "__init__"
        if is_pkg:
            parts = parts[:-1]
        module = top if not parts else ".".join([top, *parts])
        files.append(SourceFile(target, module, is_pkg))
    if not files:
        raise ValueError(f"no python modules found inside merged file {src_path}")
    logger.info(
        "Loaded {} module(s) from merged file {} (staged under {})",
        len(files),
        src_path,
        root,
    )
    return files, root
