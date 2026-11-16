"""Helpers for embedding data files inside the consolidated output."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Dict, Tuple

from loguru import logger

# Warn when a single embedded file exceeds this size.
_WARN_SIZE = 500 * 1024
# Hard warn threshold for a single file.
_HARD_WARN_SIZE = 5 * 1024 * 1024


def classify_and_encode(path: Path) -> Tuple[str, str]:
    """Return ("text", str) or ("binary", base64-str) for a data file."""
    raw = path.read_bytes()
    if len(raw) > _HARD_WARN_SIZE:
        logger.warning(
            "Data file {} is very large ({} bytes); embedding will bloat the output",
            path,
            len(raw),
        )
    elif len(raw) > _WARN_SIZE:
        logger.info("Embedding data file {} ({} bytes)", path, len(raw))

    if b"\x00" in raw:
        return "binary", base64.b64encode(raw).decode("ascii")
    try:
        return "text", raw.decode("utf-8")
    except UnicodeDecodeError:
        return "binary", base64.b64encode(raw).decode("ascii")


def collect_embedded_data(data_files) -> Dict[str, Tuple[str, str]]:
    """Map relative path -> (kind, content) for every data file."""
    out: Dict[str, Tuple[str, str]] = {}
    for df in data_files:
        kind, content = classify_and_encode(df.path)
        out[df.relative] = (kind, content)
    return out
