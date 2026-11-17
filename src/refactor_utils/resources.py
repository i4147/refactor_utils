"""Embedding data files into the consolidated module.

New functionality: data files discovered next to the Python sources can be
embedded directly into the generated single file so it is fully
self-contained:

* text data files (json, ini, yaml, csv, ...) are embedded as **plain text**
  string literals, human-readable in the output;
* non-text data files (fonts, sqlite, pickle, images, ...) are embedded as
  **base64** string literals and decoded on access.

The consolidated module exposes:

* ``get_resource(name, *, encoding=None)`` — returns ``str`` for text
  resources (or ``bytes`` with ``encoding=...``) and ``bytes`` for binary
  resources (or ``str`` with ``encoding=...``);
* ``resource_path(name)`` — a context manager that materialises a resource
  into a temporary file (for APIs that require a real path, e.g. font
  loaders);
* ``list_resources()`` — sorted list of embedded resource names.
"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Iterable, Sequence

# Extensions that are definitely text (embedded verbatim).
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
# Extensions that are definitely binary (embedded as base64).
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
    ".woff",
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
    """Return ``"text"`` or ``"binary"`` for a data file."""
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
    if raw is None:
        return "binary"
    if b"\x00" in raw:
        return "binary"
    try:
        raw.decode("utf-8")
        return "text"
    except UnicodeDecodeError:
        return "binary"


def _chunk_literal(value: str, chunk: int = 100) -> str:
    """Render *value* as one or more adjacent string literals, short lines."""
    pieces = [value[i : i + chunk] for i in range(0, len(value), chunk)] or [""]
    rendered = [repr(p) for p in pieces]
    if len(rendered) == 1:
        return rendered[0]
    return "(\n    " + "\n    ".join(rendered) + "\n)"


def _text_literal(text: str) -> str:
    """Readable Python string literal for a text resource.

    Keeps the original line structure visible: each line of the file becomes
    its own (implicitly concatenated) literal.
    """
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
    """Build the python source for the bundled-resources section.

    ``payloads`` is a sequence of ``(resource_name, raw_bytes)`` pairs where
    ``resource_name`` is the path relative to the consolidated file (e.g.
    ``"data/settings.json"``).
    """
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
